"""
Structured error handling for all Celery processing tasks — Week 2 Day 3.

Provides a single entry point for task failure that:
  - Sets model.status = "failed" with stage-tagged error_message
  - Publishes model:failed Redis event to ws-server
  - Returns a structured failure response dict
  - Determines whether the exception warrants a Celery retry

Retry policy (PRD):
  - RETRY:  S3 errors (botocore), Redis errors, transient OS/IO errors
  - NO RETRY: parsing errors, mesh corruption, validation errors,
               file-too-large, import errors (permanent failures)
"""

from __future__ import annotations

import logging
from typing import Any

import botocore.exceptions  # type: ignore

from app.config import settings
from app.tasks.common import publish_model_failed, update_model_status

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exception classification
# ---------------------------------------------------------------------------

# Exception types that indicate transient infrastructure failure → retry
_RETRYABLE_BASES = (
    ConnectionError,
    OSError,
    TimeoutError,
    botocore.exceptions.EndpointConnectionError,
    botocore.exceptions.ConnectionError,
)

# Names of exception classes that are NEVER retried regardless of base class
_PERMANENT_NAMES = {
    "GltfValidationError",
    "ValidationError",
    "FileTooLargeError",
    "SoftTimeLimitExceeded",
    "ImportError",
    "ModuleNotFoundError",
    "RuntimeError",
    "ValueError",
    "TypeError",
    "AttributeError",
}


def is_retryable(exc: BaseException) -> bool:
    """
    Returns True only for transient infrastructure errors.
    Parsing/mesh/validation errors always return False.
    """
    exc_type_name = type(exc).__name__
    if exc_type_name in _PERMANENT_NAMES:
        return False
    return isinstance(exc, _RETRYABLE_BASES)


# ---------------------------------------------------------------------------
# Custom exception types
# ---------------------------------------------------------------------------

class FileTooLargeError(Exception):
    """Raised before processing starts when the source file exceeds the size limit."""


# ---------------------------------------------------------------------------
# Main failure handler
# ---------------------------------------------------------------------------

def handle_task_failure(
    engine: Any,
    model_id: str,
    user_id: str,
    stage: str,
    exc: BaseException,
) -> dict:
    """
    Centralised failure handler called from every task's except block.

    Parameters
    ----------
    engine   : SQLAlchemy sync engine
    model_id : model UUID string
    user_id  : owner UUID string (for Redis channel)
    stage    : human label for the pipeline stage that failed
               e.g. "download", "parse", "mesh", "export", "compress", "upload"
    exc      : the caught exception

    Returns a structured dict compatible with Celery task return values.
    """
    exc_type = type(exc).__name__
    error_detail = str(exc)[:400]
    error_message = f"[{stage}] {exc_type}: {error_detail}"

    logger.error(
        "Task failure | model_id=%s stage=%s exc_type=%s: %s",
        model_id,
        stage,
        exc_type,
        error_detail,
    )

    try:
        update_model_status(engine, model_id, "failed", error_message=error_message)
    except Exception as db_exc:
        logger.error(
            "Failed to update model status to failed (model_id=%s): %s",
            model_id,
            db_exc,
        )

    try:
        publish_model_failed(user_id, model_id, error_message)
    except Exception as redis_exc:
        logger.error(
            "Failed to publish model:failed event (model_id=%s): %s",
            model_id,
            redis_exc,
        )

    try:
        from app.tasks.common import dispatch_webhook_event
        dispatch_webhook_event(
            engine, "model.failed", {"model_id": model_id, "error": error_detail}, user_id
        )
    except Exception as webhook_exc:
        logger.error(
            "Failed to dispatch model.failed webhook event (model_id=%s): %s",
            model_id,
            webhook_exc,
        )

    return {
        "model_id": model_id,
        "status": "failed",
        "stage": stage,
        "error_type": exc_type,
        "error": error_detail,
    }


# ---------------------------------------------------------------------------
# File size guard — call before any processing
# ---------------------------------------------------------------------------

MAX_FILE_BYTES = settings.MAX_UPLOAD_SIZE_BYTES


def assert_file_size(file_path: str, max_bytes: int = MAX_FILE_BYTES) -> None:
    """
    Raise FileTooLargeError if the file exceeds max_bytes.
    Must be called after download, before any parse/mesh step.
    """
    import os
    size = os.path.getsize(file_path)
    if size > max_bytes:
        raise FileTooLargeError(
            f"File size {size / 1024 / 1024:.1f} MB exceeds "
            f"limit of {max_bytes / 1024 / 1024:.0f} MB"
        )
    logger.debug("File size OK: %.1f MB (%s)", size / 1024 / 1024, file_path)
