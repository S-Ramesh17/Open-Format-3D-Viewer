"""
Converter pipeline reliability tests.

Covers the Redis task-lock dedup and missing-model handling that Phase 8
applied uniformly across all five format converters (ifc, gltf, obj, step,
stl). Each converter must:
  1. Be callable / correctly registered as a Celery task.
  2. Skip with status="skipped", reason="duplicate_task" when
     acquire_task_lock() reports the lock is already held (i.e. a redelivery
     or concurrent duplicate dispatch).
  3. Return an error result — and release its lock — when the model row
     can't be found, rather than raising.

Uses the same eager-mode + `redis.from_url` patching convention as
test_worker_pipeline.py's TestWorkerReliability so these run in-process
without a real broker or Redis instance.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.tasks.gltf import process_gltf
from app.tasks.ifc import process_model as process_ifc
from app.tasks.obj import process_obj
from app.tasks.step import process_step
from app.tasks.stl import process_stl

# (task, module path used for get_model_row/get_sync_engine patches)
CONVERTERS = [
    (process_ifc, "app.tasks.ifc"),
    (process_gltf, "app.tasks.gltf"),
    (process_obj, "app.tasks.obj"),
    (process_step, "app.tasks.step"),
    (process_stl, "app.tasks.stl"),
]


def _locked_redis() -> MagicMock:
    """A Redis mock whose SET NX fails — i.e. another worker already holds the lock."""
    mock_redis = MagicMock()
    mock_redis.set.return_value = None
    mock_redis.close.return_value = None
    return mock_redis


class TestConverterCallable:
    """Sanity check: every converter is registered and directly callable."""

    @pytest.mark.parametrize("task,_mod", CONVERTERS, ids=[m for _, m in CONVERTERS])
    def test_task_is_callable(self, task, _mod):
        assert callable(task)


class TestConverterDuplicateLockHandling:
    """
    Regression test for the Phase 8 fix: previously only ifc.py acquired a
    Redis task lock, so a redelivered gltf/obj/step/stl message could be
    processed twice concurrently (double S3 writes, wasted CPU, potential
    inconsistent chunk state). All five now must short-circuit identically.
    """

    @pytest.mark.parametrize("task,mod", CONVERTERS, ids=[m for _, m in CONVERTERS])
    def test_duplicate_task_is_skipped(self, task, mod):
        model_id = str(uuid.uuid4())

        with patch(f"{mod}.get_sync_engine"), \
             patch("redis.from_url", return_value=_locked_redis()):
            result = task.apply(args=[model_id]).get()

        assert result["status"] == "skipped"
        assert result["reason"] == "duplicate_task"
        assert result["model_id"] == model_id


class TestConverterMissingModelHandling:
    """
    Every converter must fail gracefully (not raise) when the model row is
    gone by the time the task runs, and must release the lock it just
    acquired so a legitimate retry isn't blocked by its own stale lock.
    """

    @pytest.mark.parametrize("task,mod", CONVERTERS, ids=[m for _, m in CONVERTERS])
    def test_missing_model_returns_error_and_releases_lock(self, task, mod):
        model_id = str(uuid.uuid4())
        mock_redis = MagicMock()
        mock_redis.set.return_value = True  # lock acquired successfully
        mock_redis.delete.return_value = 1  # release_task_lock uses DEL
        mock_redis.close.return_value = None

        with patch(f"{mod}.get_sync_engine"), \
             patch(f"{mod}.get_model_row", return_value=None), \
             patch("redis.from_url", return_value=mock_redis):
            result = task.apply(args=[model_id]).get()

        assert result["error"] == "model_not_found"
        assert result["model_id"] == model_id
        # release_task_lock() must have run so a follow-up retry can reacquire.
        mock_redis.delete.assert_called()


class TestConverterTimeoutHandling:
    """
    Regression coverage for the soft_time_limit=1500 configured on every
    converter task. Celery raises SoftTimeLimitExceeded inside the running
    task when a job overruns its soft limit; each converter wraps its
    pipeline in `except SoftTimeLimitExceeded: return handle_task_failure(...)`
    so a hung/slow conversion (e.g. a pathological mesh) ends the model in
    "failed" with a clear reason instead of being silently killed by Celery
    with no DB/WS record of what happened. Previously untested for all five
    converters.
    """

    def _unlocked_ready_redis(self) -> MagicMock:
        mock_redis = MagicMock()
        mock_redis.set.return_value = True  # lock acquired
        mock_redis.delete.return_value = 1
        mock_redis.close.return_value = None
        return mock_redis

    def _model_row(self, model_id: str) -> dict:
        return {
            "id": model_id,
            "uploaded_by": str(uuid.uuid4()),
            "raw_s3_key": "user/model/test.bin",
            "processed_s3_prefix": None,
            "status": "processing",
            "format": "test",
        }

    @pytest.mark.parametrize("task,mod", CONVERTERS, ids=[m for _, m in CONVERTERS])
    def test_soft_time_limit_exceeded_marks_failed_not_raised(self, task, mod):
        from celery.exceptions import SoftTimeLimitExceeded

        model_id = str(uuid.uuid4())
        mock_handle_failure = MagicMock(
            return_value={"model_id": model_id, "status": "failed", "error": "soft time limit"}
        )

        with patch(f"{mod}.get_sync_engine"), \
             patch(f"{mod}.get_model_row", return_value=self._model_row(model_id)), \
             patch(f"{mod}.download_raw_file", side_effect=SoftTimeLimitExceeded("soft time limit")), \
             patch(f"{mod}.handle_task_failure", mock_handle_failure), \
             patch("redis.from_url", return_value=self._unlocked_ready_redis()):
            result = task.apply(args=[model_id]).get()

        mock_handle_failure.assert_called_once()
        call_args = mock_handle_failure.call_args.args
        # (engine, model_id, user_id, stage, exc) — exc must be the timeout.
        assert call_args[1] == model_id
        assert isinstance(call_args[4], SoftTimeLimitExceeded)
        assert result == mock_handle_failure.return_value


class TestConverterPlanFileSizeLimit:
    """
    Per-plan upload size enforcement (PRD §3.1: free=50MB, pro=500MB,
    enterprise=5GB), applied uniformly across all five converters.

    Two checks exist:
      1. Pre-download: model["file_size_bytes"] (recorded at upload time)
         vs get_plan_max_bytes(plan) — rejects before ever touching S3.
      2. Post-download: assert_file_size(local_path, max_bytes=...) — a
         second, on-disk check after the actual download, catching any
         mismatch between the DB-recorded size and the real file.

    Both raise FileTooLargeError uncaught (verified directly against the
    source — the pre-download raise happens outside any try/except in these
    functions, so Celery's eager .get() re-raises it rather than returning
    a result dict). NOTE: neither check calls update_model_status() before
    raising, so a rejected upload's DB row is never marked "failed" — the
    task just dies with an unhandled exception. That's a real gap (worth
    fixing so rejected models don't get stuck in "processing" forever), but
    it doesn't stop the guard itself from correctly rejecting oversized
    files, so it's out of scope for this test suite and flagged separately.
    """

    def _unlocked_ready_redis(self) -> MagicMock:
        mock_redis = MagicMock()
        mock_redis.set.return_value = True  # lock acquired
        mock_redis.delete.return_value = 1
        mock_redis.close.return_value = None
        return mock_redis

    def _model_row(self, model_id: str, plan: str, file_size_bytes: int) -> dict:
        return {
            "id": model_id,
            "uploaded_by": str(uuid.uuid4()),
            "raw_s3_key": "user/model/test.bin",
            "processed_s3_prefix": None,
            "status": "processing",
            "format": "test",
            "plan": plan,
            "file_size_bytes": file_size_bytes,
        }

    # ------------------------------------------------------------------
    # Pre-download check
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("task,mod", CONVERTERS, ids=[m for _, m in CONVERTERS])
    def test_free_plan_rejected_above_50mb(self, task, mod):
        from app.tasks.error_handler import FileTooLargeError

        model_id = str(uuid.uuid4())
        row = self._model_row(model_id, plan="free", file_size_bytes=60 * 1024 * 1024)

        with patch(f"{mod}.get_sync_engine"), \
             patch(f"{mod}.get_model_row", return_value=row), \
             patch(f"{mod}.download_raw_file") as mock_download, \
             patch("redis.from_url", return_value=self._unlocked_ready_redis()):
            with pytest.raises(FileTooLargeError):
                task.apply(args=[model_id]).get()

        mock_download.assert_not_called()

    @pytest.mark.parametrize("task,mod", CONVERTERS, ids=[m for _, m in CONVERTERS])
    def test_pro_plan_rejected_above_500mb(self, task, mod):
        from app.tasks.error_handler import FileTooLargeError

        model_id = str(uuid.uuid4())
        row = self._model_row(model_id, plan="pro", file_size_bytes=600 * 1024 * 1024)

        with patch(f"{mod}.get_sync_engine"), \
             patch(f"{mod}.get_model_row", return_value=row), \
             patch(f"{mod}.download_raw_file") as mock_download, \
             patch("redis.from_url", return_value=self._unlocked_ready_redis()):
            with pytest.raises(FileTooLargeError):
                task.apply(args=[model_id]).get()

        mock_download.assert_not_called()

    @pytest.mark.parametrize("task,mod", CONVERTERS, ids=[m for _, m in CONVERTERS])
    def test_pro_plan_499mb_allowed(self, task, mod):
        """download_raw_file is patched with a side_effect exception purely
        as a checkpoint marker — it's caught by the pipeline's own
        try/except (stage="size_check") and converted into a normal failed
        result, so we assert on the mock call, not a raised exception."""
        model_id = str(uuid.uuid4())
        row = self._model_row(model_id, plan="pro", file_size_bytes=499 * 1024 * 1024)
        sentinel = RuntimeError("reached download — pre-check passed")

        with patch(f"{mod}.get_sync_engine"), \
             patch(f"{mod}.get_model_row", return_value=row), \
             patch(f"{mod}.download_raw_file", side_effect=sentinel) as mock_download, \
             patch("redis.from_url", return_value=self._unlocked_ready_redis()):
            task.apply(args=[model_id]).get()

        mock_download.assert_called_once()

    @pytest.mark.parametrize("task,mod", CONVERTERS, ids=[m for _, m in CONVERTERS])
    def test_enterprise_file_size_passes_500mb_guard(self, task, mod):
        """A 1GB file is above the pro limit but within the enterprise
        limit (5GB) — must pass the pre-download guard and reach download."""
        model_id = str(uuid.uuid4())
        row = self._model_row(model_id, plan="enterprise", file_size_bytes=1 * 1024 * 1024 * 1024)
        sentinel = RuntimeError("reached download — pre-check passed")

        with patch(f"{mod}.get_sync_engine"), \
             patch(f"{mod}.get_model_row", return_value=row), \
             patch(f"{mod}.download_raw_file", side_effect=sentinel) as mock_download, \
             patch("redis.from_url", return_value=self._unlocked_ready_redis()):
            task.apply(args=[model_id]).get()

        mock_download.assert_called_once()

    @pytest.mark.parametrize("task,mod", CONVERTERS, ids=[m for _, m in CONVERTERS])
    def test_unknown_plan_defaults_to_free(self, task, mod):
        """An unrecognized plan value must fall back to the most
        restrictive (free) limit, not the most permissive."""
        from app.tasks.error_handler import FileTooLargeError

        model_id = str(uuid.uuid4())
        row = self._model_row(model_id, plan="not_a_real_plan", file_size_bytes=60 * 1024 * 1024)

        with patch(f"{mod}.get_sync_engine"), \
             patch(f"{mod}.get_model_row", return_value=row), \
             patch(f"{mod}.download_raw_file") as mock_download, \
             patch("redis.from_url", return_value=self._unlocked_ready_redis()):
            with pytest.raises(FileTooLargeError):
                task.apply(args=[model_id]).get()

        mock_download.assert_not_called()

    # ------------------------------------------------------------------
    # Post-download check
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("task,mod", CONVERTERS, ids=[m for _, m in CONVERTERS])
    def test_enterprise_on_disk_limit(self, task, mod):
        """After download, assert_file_size() must be called with the
        caller's actual plan limit (enterprise=5GB) — not the module-level
        MAX_FILE_BYTES default — so a DB/on-disk size mismatch is still
        checked against the right ceiling for this specific upload."""
        model_id = str(uuid.uuid4())
        row = self._model_row(model_id, plan="enterprise", file_size_bytes=1 * 1024 * 1024 * 1024)
        enterprise_limit = 5 * 1024 * 1024 * 1024
        sentinel = RuntimeError("reached post-download checkpoint")

        with patch(f"{mod}.get_sync_engine"), \
             patch(f"{mod}.get_model_row", return_value=row), \
             patch(f"{mod}.download_raw_file"), \
             patch(f"{mod}.assert_file_size", side_effect=sentinel) as mock_assert, \
             patch("redis.from_url", return_value=self._unlocked_ready_redis()):
            task.apply(args=[model_id]).get()

        mock_assert.assert_called_once()
        assert mock_assert.call_args.kwargs.get("max_bytes") == enterprise_limit