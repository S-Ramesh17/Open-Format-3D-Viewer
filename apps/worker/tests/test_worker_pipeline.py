"""
Worker-internal pipeline tests.

These tests import from app.tasks.* (apps/worker/app/tasks/).
Run from apps/worker/:
    cd apps/worker && poetry run pytest tests/ -v
"""
from __future__ import annotations

import json
import socket
import uuid
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Issue 1: Scan retry on ClamAV connection failure
# ---------------------------------------------------------------------------

class TestScanFlow:
    def _make_model_row(self, model_id: str = None, file_format: str = "ifc") -> dict:
        return {
            "id": model_id or str(uuid.uuid4()),
            "uploaded_by": str(uuid.uuid4()),
            "raw_s3_key": "user/model/test.ifc",
            "processed_s3_prefix": None,
            "status": "processing",
            "format": file_format,
        }

    def test_clean_scan_dispatches_processing_task(self, tmp_path):
        model_id = str(uuid.uuid4())
        model_row = self._make_model_row(model_id)

        with patch("app.tasks.scan.get_sync_engine"), \
             patch("app.tasks.scan.get_model_row", return_value=model_row), \
             patch("app.tasks.scan.update_model_status"), \
             patch("app.tasks.scan._download_to_temp") as mock_dl, \
             patch("app.tasks.scan._clamd_scan", return_value=(True, "stream: OK")), \
             patch("app.tasks.scan._dispatch_processing_task") as mock_dispatch, \
             patch("app.config.settings.STORAGE_PROVIDER", "local"), \
             patch("app.config.settings.LOCAL_STORAGE_PATH", str(tmp_path)):

            fake_file = str(tmp_path / "fake.bin")
            (tmp_path / "fake.bin").write_bytes(b"ISO-10303-21;")
            mock_dl.return_value = fake_file

            from app.tasks.scan import scan_file
            result = scan_file.apply(args=[model_id, "user/model/test.ifc"]).get()

        assert result["status"] == "clean"
        mock_dispatch.assert_called_once_with(model_id, "ifc")

    def test_infected_file_fails_model_and_deletes(self, tmp_path):
        model_id = str(uuid.uuid4())
        model_row = self._make_model_row(model_id)

        with patch("app.tasks.scan.get_sync_engine"), \
             patch("app.tasks.scan.get_model_row", return_value=model_row), \
             patch("app.tasks.scan.update_model_status") as mock_update, \
             patch("app.tasks.scan._download_to_temp") as mock_dl, \
             patch("app.tasks.scan._clamd_scan", return_value=(False, "stream: Eicar-Test-Signature FOUND")), \
             patch("app.tasks.scan._delete_s3_object") as mock_delete, \
             patch("app.tasks.scan._publish_scan_failure") as mock_pub, \
             patch("app.config.settings.STORAGE_PROVIDER", "local"), \
             patch("app.config.settings.LOCAL_STORAGE_PATH", str(tmp_path)):

            fake_file = str(tmp_path / "fake.bin")
            (tmp_path / "fake.bin").write_bytes(b"EICAR")
            mock_dl.return_value = fake_file

            from app.tasks.scan import scan_file
            result = scan_file.apply(args=[model_id, "user/model/test.ifc"]).get()

        assert result["status"] == "infected"
        assert "Eicar" in result["virus"]
        mock_update.assert_called_once()
        mock_delete.assert_called_once()
        mock_pub.assert_called_once()

    def test_clamd_unavailable_retries(self, tmp_path):
        """
        Issue 1: socket.error from _clamd_scan must trigger self.retry(), not propagate.
        scan_file catches (socket.error, ConnectionRefusedError, OSError) and calls
        self.retry(exc=exc, countdown=...). In test mode Celery raises celery.exceptions.Retry.
        """
        model_id = str(uuid.uuid4())
        model_row = self._make_model_row(model_id)

        with patch("app.tasks.scan.get_sync_engine"), \
             patch("app.tasks.scan.get_model_row", return_value=model_row), \
             patch("app.tasks.scan._download_to_temp") as mock_dl, \
             patch("app.tasks.scan._clamd_scan", side_effect=socket.error("Connection refused")):

            fake_file = str(tmp_path / "fake.bin")
            (tmp_path / "fake.bin").write_bytes(b"data")
            mock_dl.return_value = fake_file

            from app.tasks.scan import scan_file
            from celery.exceptions import Retry

            # task.apply() with CELERY_TASK_ALWAYS_EAGER=True will raise Retry
            # when self.retry() is called, confirming retry logic is reached.
            with pytest.raises(Retry):
                scan_file.apply(args=[model_id, "user/model/test.ifc"]).get()


# ---------------------------------------------------------------------------
# Issue 2: Failure propagation — update_model_status must be called
# ---------------------------------------------------------------------------

class TestFailurePropagation:
    def test_storage_failure_propagates_to_redis(self):
        """handle_task_failure must call update_model_status exactly once."""
        model_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        engine_mock = MagicMock()

        with patch("app.tasks.error_handler.update_model_status") as mock_update, \
             patch("app.tasks.error_handler.publish_model_failed") as mock_pub, \
             patch("app.tasks.common.dispatch_webhook_event"):

            from app.tasks.error_handler import handle_task_failure
            result = handle_task_failure(
                engine=engine_mock,
                model_id=model_id,
                user_id=user_id,
                stage="download",
                exc=OSError("No such file"),
            )

        assert result["status"] == "failed"
        assert result["stage"] == "download"
        mock_update.assert_called_once_with(
            engine_mock,
            model_id,
            "failed",
            error_message="[download] OSError: No such file",
        )
        mock_pub.assert_called_once()

    def test_webhook_failure_does_not_block_model(self):
        """dispatch_webhook_event failure must not prevent model status update."""
        model_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        engine_mock = MagicMock()

        with patch("app.tasks.error_handler.update_model_status") as mock_update, \
             patch("app.tasks.error_handler.publish_model_failed"), \
             patch("app.tasks.common.dispatch_webhook_event", side_effect=Exception("Redis unavailable")):

            from app.tasks.error_handler import handle_task_failure
            result = handle_task_failure(
                engine=engine_mock,
                model_id=model_id,
                user_id=user_id,
                stage="upload",
                exc=ConnectionError("S3 timeout"),
            )

        mock_update.assert_called_once()
        assert result["status"] == "failed"

    def test_redis_unavailable_failure_logged_not_raised(self):
        """publish_model_failed swallows Redis errors — must not raise."""
        with patch("redis.from_url", side_effect=Exception("Redis connection refused")):
            from app.tasks.common import publish_model_failed
            publish_model_failed("user-123", "model-456", "test error")  # must not raise

    def test_is_retryable_classification(self):
        from app.tasks.error_handler import is_retryable

        assert is_retryable(OSError("disk full")) is True
        assert is_retryable(ConnectionError("refused")) is True
        assert is_retryable(TimeoutError("timed out")) is True

        assert is_retryable(ValueError("bad value")) is False
        assert is_retryable(RuntimeError("parse error")) is False
        assert is_retryable(ImportError("missing module")) is False


# ---------------------------------------------------------------------------
# Issue 3: Webhook 5xx must trigger Retry
# ---------------------------------------------------------------------------

class TestWebhookDelivery:
    def _make_webhook_row(self, webhook_id: str = None) -> dict:
        return {
            "id": webhook_id or str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
            "url": "https://example.com/webhook",
            "secret": "test-secret-key",
            "events": ["model.ready", "model.failed"],
            "is_active": True,
        }

    def test_webhook_delivery_success(self):
        webhook_id = str(uuid.uuid4())
        webhook_row = self._make_webhook_row(webhook_id)
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("app.tasks.webhook.get_sync_engine"), \
             patch("app.tasks.webhook._get_webhook_row", return_value=webhook_row), \
             patch("app.tasks.webhook._log_delivery"), \
             patch("requests.post", return_value=mock_response):

            from app.tasks.webhook import dispatch_webhook
            result = dispatch_webhook.apply(
                args=[webhook_id, "model.ready", {"model_id": "abc"}]
            ).get()

        assert result["status"] == "delivered"
        assert result["status_code"] == 200

    def test_webhook_skips_inactive(self):
        webhook_id = str(uuid.uuid4())
        webhook_row = self._make_webhook_row(webhook_id)
        webhook_row["is_active"] = False

        with patch("app.tasks.webhook.get_sync_engine"), \
             patch("app.tasks.webhook._get_webhook_row", return_value=webhook_row), \
             patch("requests.post") as mock_post:

            from app.tasks.webhook import dispatch_webhook
            result = dispatch_webhook.apply(args=[webhook_id, "model.ready", {}]).get()

        assert result["status"] == "inactive"
        mock_post.assert_not_called()

    def test_webhook_4xx_no_retry(self):
        webhook_id = str(uuid.uuid4())
        webhook_row = self._make_webhook_row(webhook_id)
        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch("app.tasks.webhook.get_sync_engine"), \
             patch("app.tasks.webhook._get_webhook_row", return_value=webhook_row), \
             patch("app.tasks.webhook._log_delivery"), \
             patch("requests.post", return_value=mock_response):

            from app.tasks.webhook import dispatch_webhook
            result = dispatch_webhook.apply(args=[webhook_id, "model.ready", {}]).get()

        assert result["status"] == "failed"
        assert result["status_code"] == 404

    def test_webhook_5xx_retries(self):
        """
        Issue 3: HTTP 5xx must call self.retry(), raising celery.exceptions.Retry.
        webhook.py raises self.retry(exc=Exception(f"HTTP {status_code}"), countdown=delay)
        when status_code >= 500 and not success.
        """
        webhook_id = str(uuid.uuid4())
        webhook_row = self._make_webhook_row(webhook_id)
        mock_response = MagicMock()
        mock_response.status_code = 503

        with patch("app.tasks.webhook.get_sync_engine"), \
             patch("app.tasks.webhook._get_webhook_row", return_value=webhook_row), \
             patch("app.tasks.webhook._log_delivery"), \
             patch("requests.post", return_value=mock_response):

            from app.tasks.webhook import dispatch_webhook
            from celery.exceptions import Retry

            with pytest.raises(Retry):
                dispatch_webhook.apply(args=[webhook_id, "model.ready", {}]).get()

    def test_webhook_hmac_signature(self):
        import hashlib
        import hmac as hmac_lib
        from app.tasks.webhook import _build_signature

        secret = "my-webhook-secret"
        payload = b'{"event":"model.ready"}'
        expected = "sha256=" + hmac_lib.new(
            secret.encode(), payload, hashlib.sha256
        ).hexdigest()

        assert _build_signature(payload, secret) == expected


# ---------------------------------------------------------------------------
# Redis event flow
# ---------------------------------------------------------------------------

class TestRedisEventFlow:
    def test_publish_model_ready_sends_correct_payload(self):
        user_id = str(uuid.uuid4())
        model_id = str(uuid.uuid4())
        published_payloads = []

        mock_redis = MagicMock()
        mock_redis.publish.side_effect = lambda ch, msg: published_payloads.append(
            (ch, json.loads(msg))
        )
        mock_redis.close.return_value = None

        with patch("redis.from_url", return_value=mock_redis):
            from app.tasks.common import publish_model_ready
            publish_model_ready(user_id, model_id)

        assert len(published_payloads) == 1
        channel, payload = published_payloads[0]
        assert channel == f"model_events:{user_id}"
        assert payload["event"] == "MODEL_READY"
        assert payload["data"]["model_id"] == model_id

    def test_publish_model_failed_sends_correct_payload(self):
        user_id = str(uuid.uuid4())
        model_id = str(uuid.uuid4())
        published_payloads = []

        mock_redis = MagicMock()
        mock_redis.publish.side_effect = lambda ch, msg: published_payloads.append(
            (ch, json.loads(msg))
        )
        mock_redis.close.return_value = None

        with patch("redis.from_url", return_value=mock_redis):
            from app.tasks.common import publish_model_failed
            publish_model_failed(user_id, model_id, "conversion failed")

        assert len(published_payloads) == 1
        channel, payload = published_payloads[0]
        assert payload["event"] == "MODEL_FAILED"
        assert payload["data"]["error"] == "conversion failed"

    def test_redis_unavailable_does_not_raise(self):
        with patch("redis.from_url", side_effect=Exception("Redis down")):
            from app.tasks.common import publish_model_ready
            publish_model_ready("user-1", "model-1")  # must not raise

    def test_publish_model_progress_sends_model_processing_event(self):
        """MODEL_PROCESSING is emitted via publish_model_progress (percent +
        stage) — this event name/payload had no coverage before."""
        user_id = str(uuid.uuid4())
        model_id = str(uuid.uuid4())
        published_payloads = []

        mock_redis = MagicMock()
        mock_redis.publish.side_effect = lambda ch, msg: published_payloads.append(
            (ch, json.loads(msg))
        )
        mock_redis.close.return_value = None

        with patch("redis.from_url", return_value=mock_redis):
            from app.tasks.common import publish_model_progress
            publish_model_progress(user_id, model_id, 42, "convert")

        assert len(published_payloads) == 1
        channel, payload = published_payloads[0]
        assert channel == f"model_events:{user_id}"
        assert payload["event"] == "MODEL_PROCESSING"
        assert payload["data"]["model_id"] == model_id
        assert payload["data"]["progress_pct"] == 42
        assert payload["data"]["stage"] == "convert"

    def test_publish_model_progress_redis_unavailable_does_not_raise(self):
        with patch("redis.from_url", side_effect=Exception("Redis down")):
            from app.tasks.common import publish_model_progress
            publish_model_progress("user-1", "model-1", 50, "convert")  # must not raise


# ---------------------------------------------------------------------------
# Worker reliability — idempotency and locking
# ---------------------------------------------------------------------------

class TestWorkerReliability:
    def test_acquire_task_lock_returns_true_on_first_call(self):
        model_id = str(uuid.uuid4())
        mock_redis = MagicMock()
        mock_redis.set.return_value = True
        mock_redis.close.return_value = None

        with patch("redis.from_url", return_value=mock_redis):
            from app.tasks.common import acquire_task_lock
            result = acquire_task_lock(model_id, "ifc.process_model")
        assert result is True

    def test_acquire_task_lock_returns_false_on_duplicate(self):
        model_id = str(uuid.uuid4())
        mock_redis = MagicMock()
        mock_redis.set.return_value = None
        mock_redis.close.return_value = None

        with patch("redis.from_url", return_value=mock_redis):
            from app.tasks.common import acquire_task_lock
            result = acquire_task_lock(model_id, "ifc.process_model")
        assert result is False

    def test_lock_fails_open_on_redis_error(self):
        with patch("redis.from_url", side_effect=Exception("Redis unavailable")):
            from app.tasks.common import acquire_task_lock
            result = acquire_task_lock("model-1", "ifc.process_model")
        assert result is True

    def test_is_already_processed_true_for_ready_model(self):
        engine = MagicMock()
        with patch("app.tasks.common.get_model_row", return_value={"status": "ready"}):
            from app.tasks.common import is_already_processed
            assert is_already_processed(engine, "model-1") is True

    def test_is_already_processed_false_for_processing_model(self):
        engine = MagicMock()
        with patch("app.tasks.common.get_model_row", return_value={"status": "processing"}):
            from app.tasks.common import is_already_processed
            assert is_already_processed(engine, "model-1") is False

    def test_is_already_processed_false_for_missing_model(self):
        engine = MagicMock()
        with patch("app.tasks.common.get_model_row", return_value=None):
            from app.tasks.common import is_already_processed
            assert is_already_processed(engine, "model-1") is False


# ---------------------------------------------------------------------------
# Worker storage helpers
# ---------------------------------------------------------------------------

class TestWorkerStorage:
    def test_s3_worker_download_calls_boto3(self, tmp_path):
        with patch("app.config.settings.STORAGE_PROVIDER", "s3"), \
             patch("app.tasks.common._s3_client") as mock_s3:
            mock_s3.return_value.download_file = MagicMock()
            from app.tasks.common import download_raw_file
            download_raw_file("key/model.ifc", str(tmp_path / "out.ifc"))
            mock_s3.return_value.download_file.assert_called_once()

    def test_local_worker_download_copies_file(self, tmp_path):
        src = tmp_path / "raw" / "user" / "model" / "test.ifc"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"ISO-10303-21;")

        dest = tmp_path / "work" / "test.ifc"
        dest.parent.mkdir(parents=True)

        with patch("app.config.settings.STORAGE_PROVIDER", "local"), \
             patch("app.config.settings.LOCAL_STORAGE_PATH", str(tmp_path)):
            from app.tasks.common import download_raw_file
            download_raw_file("user/model/test.ifc", str(dest))

        assert dest.exists()
        assert dest.read_bytes() == b"ISO-10303-21;"

    def test_build_cdn_url_local_mode_returns_files_route(self):
        """MODEL_READY.chunk_urls must point at the API's /files/ route in
        local mode — previously all 5 converters built this URL from
        CDN_BASE_URL unconditionally, which is empty/placeholder in local
        dev and broke the client's ability to load a just-processed model."""
        with patch("app.config.settings.STORAGE_PROVIDER", "local"):
            from app.tasks.common import build_cdn_url
            assert build_cdn_url("processed/model-1/chunk_0.xkt") == "/files/processed/model-1/chunk_0.xkt"

    def test_build_cdn_url_s3_mode_uses_cdn_base_url(self):
        with patch("app.config.settings.STORAGE_PROVIDER", "s3"), \
             patch("app.config.settings.CDN_BASE_URL", "https://cdn.example.com/"):
            from app.tasks.common import build_cdn_url
            assert build_cdn_url("processed/model-1/chunk_0.xkt") == "https://cdn.example.com/processed/model-1/chunk_0.xkt"


# ---------------------------------------------------------------------------
# Abandoned-upload sweeper (Celery Beat, hourly) — previously untested
# ---------------------------------------------------------------------------

class TestAbandonedUploadSweeper:
    """
    cleanup_abandoned_uploads() finds models stuck in 'pending' (uploaded
    but never confirmed) for >24h, marks them 'failed', and deletes the
    orphaned raw object so it doesn't sit in storage forever. Covers the
    "cleanup" item from the worker reliability checklist.
    """

    def test_marks_stale_pending_models_failed_and_deletes_object(self):
        model_id = str(uuid.uuid4())
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            (model_id, "user/model/abandoned.ifc"),
        ]
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__.return_value = mock_conn

        with patch("app.tasks.common.get_sync_engine", return_value=mock_engine), \
             patch("app.tasks.common.update_model_status") as mock_update_status, \
             patch("app.tasks.scan._delete_s3_object") as mock_delete:
            from app.tasks.common import cleanup_abandoned_uploads
            cleanup_abandoned_uploads()

        mock_update_status.assert_called_once()
        call_args = mock_update_status.call_args
        assert call_args.args[1] == model_id
        assert call_args.args[2] == "failed"
        mock_delete.assert_called_once_with("user/model/abandoned.ifc")

    def test_no_stale_uploads_does_nothing(self):
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__.return_value = mock_conn

        with patch("app.tasks.common.get_sync_engine", return_value=mock_engine), \
             patch("app.tasks.common.update_model_status") as mock_update_status:
            from app.tasks.common import cleanup_abandoned_uploads
            cleanup_abandoned_uploads()

        mock_update_status.assert_not_called()

    def test_s3_delete_failure_does_not_raise_or_block_other_rows(self):
        """A storage delete failure for one abandoned upload must not stop
        the model from being marked failed, nor crash the sweeper task
        (it runs hourly via Celery Beat — an unhandled exception here would
        kill the whole periodic task, not just one row)."""
        model_id = str(uuid.uuid4())
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            (model_id, "user/model/abandoned.ifc"),
        ]
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__.return_value = mock_conn

        with patch("app.tasks.common.get_sync_engine", return_value=mock_engine), \
             patch("app.tasks.common.update_model_status") as mock_update_status, \
             patch("app.tasks.scan._delete_s3_object", side_effect=Exception("S3 unreachable")):
            from app.tasks.common import cleanup_abandoned_uploads
            cleanup_abandoned_uploads()  # must not raise

        mock_update_status.assert_called_once()