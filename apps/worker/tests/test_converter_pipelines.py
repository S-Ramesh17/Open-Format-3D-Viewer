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
