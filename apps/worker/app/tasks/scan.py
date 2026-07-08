"""
ClamAV scan task — streams S3 object through clamd, quarantines on infection.

Flow:
  1. Download raw file from S3 to a temp file
  2. Stream file bytes through clamd TCP socket (INSTREAM command)
  3. If clean    → dispatch the IFC/mesh processing task (gates processing
                   on scan result — see Week 3 Day 3 race condition fix)
  4. If infected → update model status → "failed", delete S3 object,
                   publish Redis failure event
  5. Celery retries on transient clamd / S3 errors (up to 3 times)
"""

from __future__ import annotations

import json
import logging
import os
import socket
import struct
import tempfile

import boto3
from botocore.config import Config as BotoConfig

from app.celery_app import celery_app
from app.config import settings
from app.tasks.common import (
    get_model_row,
    get_sync_engine,
    update_model_status
)

logger = logging.getLogger(__name__)

# ClamAV INSTREAM chunk size — clamd protocol maximum per chunk is 4 GB,
# but 64 KB chunks keep memory low and allow progress visibility.
_CHUNK_SIZE = 65536


# ---------------------------------------------------------------------------
# S3 helpers (local to this module — no circular import with common.py)
# ---------------------------------------------------------------------------

def _s3_client():
    return boto3.client(
        "s3",
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID or None,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY or None,
        config=BotoConfig(signature_version="s3v4"),
    )


def _download_to_temp(s3_key: str) -> str:
    """Download raw file (S3 or local) to a temp file. Returns the local path."""
    # TEMP LOCAL STORAGE
    if settings.STORAGE_PROVIDER == "local":
        import shutil
        local_src = os.path.join(settings.LOCAL_STORAGE_PATH, "raw", s3_key)
        logger.info("[SCAN][LOCAL] Copying %s for scan", local_src)
        if not os.path.exists(local_src):
            raise FileNotFoundError(
                f"Local raw file not found for scan: {local_src}"
            )
        fd, path = tempfile.mkstemp(prefix="scan_", suffix=".bin")
        os.close(fd)
        shutil.copy2(local_src, path)
        return path
    # END TEMP LOCAL STORAGE

    s3 = _s3_client()
    fd, path = tempfile.mkstemp(prefix="scan_", suffix=".bin")
    os.close(fd)
    logger.info("[SCAN] Downloading s3://%s/%s → %s", settings.S3_RAW_BUCKET, s3_key, path)
    s3.download_file(settings.S3_RAW_BUCKET, s3_key, path)
    return path

def _delete_s3_object(s3_key: str) -> None:
    """Delete an infected object from raw storage (S3 or local)."""
    # TEMP LOCAL STORAGE
    if settings.STORAGE_PROVIDER == "local":
        local_path = os.path.join(settings.LOCAL_STORAGE_PATH, "raw", s3_key)
        try:
            if os.path.exists(local_path):
                os.remove(local_path)
                logger.warning("[SCAN][LOCAL] Deleted infected local file: %s", local_path)
        except OSError as exc:
            logger.error("[SCAN][LOCAL] Failed to delete infected file %s: %s", local_path, exc)
        return
    # END TEMP LOCAL STORAGE

    s3 = _s3_client()
    try:
        s3.delete_object(Bucket=settings.S3_RAW_BUCKET, Key=s3_key)
        logger.warning("[SCAN] Deleted infected S3 object: %s", s3_key)
    except Exception as exc:
        logger.error("[SCAN] Failed to delete infected object %s: %s", s3_key, exc)
# ---------------------------------------------------------------------------
# ClamAV INSTREAM protocol
# ---------------------------------------------------------------------------

def _clamd_scan(file_path: str) -> tuple[bool, str]:
    """
    Stream a file to clamd via TCP using the INSTREAM command.

    Protocol (ClamAV docs):
        Client sends:  b"zINSTREAM\\0"
        Then in a loop:
            <4-byte big-endian chunk length> + <chunk bytes>
        Terminates with a zero-length chunk: b"\\x00\\x00\\x00\\x00"
        clamd responds: b"stream: OK\\0"  (clean)
                    or: b"stream: <virus name> FOUND\\0" (infected)

    Returns (is_clean, result_string).
    Raises socket.error / ConnectionRefusedError on connectivity issues.
    """
    host = settings.CLAMD_HOST
    port = settings.CLAMD_PORT
    timeout = settings.CLAMD_TIMEOUT

    file_size = os.path.getsize(file_path)
    logger.info(
        "[SCAN] Connecting to clamd %s:%d to scan %.1f MB",
        host, port, file_size / 1024 / 1024,
    )

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(b"zINSTREAM\0")

        with open(file_path, "rb") as fh:
            while True:
                chunk = fh.read(_CHUNK_SIZE)
                if not chunk:
                    break
                # Send 4-byte big-endian length prefix followed by data
                sock.sendall(struct.pack("!I", len(chunk)) + chunk)

        # Terminate with zero-length chunk
        sock.sendall(struct.pack("!I", 0))

        # Read response (clamd sends a null-terminated string)
        response = b""
        while True:
            data = sock.recv(1024)
            if not data:
                break
            response += data
            if b"\0" in response or b"\n" in response:
                break

    result = response.rstrip(b"\0\n").decode("utf-8", errors="replace")
    logger.info("[SCAN] clamd response: %r", result)

    # clamd returns "stream: OK" for clean files
    is_clean = result.strip().endswith("OK")
    return is_clean, result


def _publish_scan_failure(user_id: str, model_id: str, virus_name: str) -> None:
    """Publish a model:infected event to Redis for ws-server relay."""
    import redis as redis_lib

    try:
        r = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
        r.publish(
            f"model_events:{user_id}",
            json.dumps({
                "event": "model:failed",
                "data": {
                    "model_id": model_id,
                    "error": f"File rejected by antivirus scan: {virus_name}",
                    "reason": "infected",
                },
            }),
        )
        r.close()
        logger.warning("[SCAN] Published model:infected event for model_id=%s", model_id)
    except Exception as exc:
        logger.error("[SCAN] Failed to publish scan failure event: %s", exc)


def _dispatch_processing_task(model_id: str, file_format: str | None) -> None:
    """
    Dispatch the correct processing task once the scan confirms the file
    is clean. IFC goes to its dedicated task/queue; everything else routes
    through the mesh queue's format router (app.tasks.mesh.generate_chunks).

    This is the single gate for starting processing — no other code path
    in the API or worker should dispatch ifc.process_model or
    mesh.generate_chunks directly for a freshly uploaded model.
    """
    task_name = (
        "app.tasks.ifc.process_model"
        if file_format == "ifc"
        else "app.tasks.mesh.generate_chunks"
    )
    queue = "ifc" if file_format == "ifc" else "mesh"
    celery_app.send_task(task_name, args=[model_id], queue=queue)
    logger.info(
        "[SCAN] Clean scan — dispatched %s for model_id=%s (queue=%s)",
        task_name, model_id, queue,
    )


# ---------------------------------------------------------------------------
# Main Celery task
# ---------------------------------------------------------------------------

@celery_app.task(
    name="app.tasks.scan.scan_file",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="scan",
)
def scan_file(self, model_id: str, s3_key: str) -> dict:
    """
    ClamAV antivirus scan for an uploaded file.

    Called by the API layer (trigger_clamav_scan) immediately after upload
    confirmation. The processing task (ifc/step/mesh) runs in parallel —
    we do NOT block processing on the scan result, but we mark the model
    as failed and publish an event if an infection is detected.

    Parameters
    ----------
    model_id : str  — model UUID string
    s3_key   : str  — S3 key in the raw bucket
    """
    logger.info("[SCAN] Starting scan model_id=%s s3_key=%s", model_id, s3_key)

    engine = get_sync_engine()

    # Fetch model to get the owner for Redis events and file_format for routing
    model = get_model_row(engine, model_id)
    if model is None:
        logger.error("[SCAN] Model %s not found — skipping scan", model_id)
        return {"model_id": model_id, "status": "skipped", "reason": "model_not_found"}

    user_id = str(model["uploaded_by"])
    file_format = model.get("file_format")

    try:
        with tempfile.TemporaryDirectory(prefix="clamd_"):
            # Download file from S3 / local storage
            local_path = _download_to_temp(s3_key)

            try:
                is_clean, result_str = _clamd_scan(local_path)
            finally:
                # Always remove temp file regardless of outcome
                try:
                    os.unlink(local_path)
                except OSError:
                    pass

        if is_clean:
            logger.info("[SCAN] Clean scan for model_id=%s — dispatching processing", model_id)
            # GATE: dispatch processing only after confirmed clean scan.
            # Before Week 3 Day 3, the API dispatched both scan and processing
            # simultaneously, creating a race where infected files could reach
            # "ready" status with processed chunks in S3 before scan finished.
            _dispatch_processing_task(model_id, file_format)
            return {
                "model_id": model_id,
                "s3_key": s3_key,
                "status": "clean",
                "dispatched": file_format,
            }

        # ── INFECTED ──────────────────────────────────────────────────────
        # Extract virus name from: "stream: Eicar-Test-Signature FOUND"
        virus_name = result_str.replace("stream:", "").replace("FOUND", "").strip()
        logger.error(
            "[SCAN] INFECTED file detected: model_id=%s virus=%r s3_key=%s",
            model_id, virus_name, s3_key,
        )

        # Mark model as failed
        update_model_status(
            engine,
            model_id,
            "failed",
            error_message=f"Antivirus scan rejected file: {virus_name}",
        )

        # Delete infected object from S3
        _delete_s3_object(s3_key)

        # Publish failure event to ws-server
        _publish_scan_failure(user_id, model_id, virus_name)

        return {
            "model_id": model_id,
            "s3_key": s3_key,
            "status": "infected",
            "virus": virus_name,
        }

    except (socket.error, ConnectionRefusedError, OSError) as exc:
        logger.error("[SCAN] Transient error scanning model_id=%s: %s", model_id, exc)
        # Retry on connectivity issues
        raise self.retry(exc=exc, countdown=30 * (self.request.retries + 1))

    except Exception as exc:
        logger.exception("[SCAN] Unexpected error scanning model_id=%s: %s", model_id, exc)
        # Non-retryable — do not fail the model; log and move on
        # (scan infrastructure failure should not block legitimate uploads)
        return {
            "model_id": model_id,
            "s3_key": s3_key,
            "status": "error",
            "error": str(exc)[:400],
        }
