"""
Shared helpers for all Celery processing tasks.

Extracted from app.tasks.ifc so STEP, GLTF/GLB, and future tasks
can reuse DB, S3, and Redis primitives without circular imports.

IMPORTANT: Import from this module only, never from app.tasks.ifc directly.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from app.celery_app import celery_app

import boto3
from botocore.config import Config as BotoConfig
from sqlalchemy import create_engine

from app.config import settings
import os  # needed for local storage path operations
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------

def _s3_client():
    return boto3.client(
        "s3",
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID or None,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY or None,
        config=BotoConfig(signature_version="s3v4"),
    )


def download_raw_file(s3_key: str, dest_path: str) -> None:
    """Download a file from the raw S3 bucket (or local storage) to local disk."""
    # TEMP LOCAL STORAGE
    if settings.STORAGE_PROVIDER == "local":
        import shutil
        local_src = os.path.join(settings.LOCAL_STORAGE_PATH, "raw", s3_key)
        logger.info("[LOCAL] Copying %s → %s", local_src, dest_path)
        if not os.path.exists(local_src):
            raise FileNotFoundError(
                f"Local raw file not found: {local_src}. "
                "Place your test file at this path before triggering the task."
            )
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        shutil.copy2(local_src, dest_path)
        return
    # END TEMP LOCAL STORAGE

    s3 = _s3_client()
    logger.info("Downloading s3://%s/%s → %s", settings.S3_RAW_BUCKET, s3_key, dest_path)
    s3.download_file(settings.S3_RAW_BUCKET, s3_key, dest_path)


def upload_processed_file(
    local_path: str,
    s3_key: str,
    content_type: str = "application/octet-stream",
) -> None:
    """Upload a processed output file to the processed S3 bucket (or local storage)."""
    # TEMP LOCAL STORAGE
    if settings.STORAGE_PROVIDER == "local":
        import shutil
        local_dest = os.path.join(settings.LOCAL_STORAGE_PATH, "processed", s3_key)
        os.makedirs(os.path.dirname(local_dest), exist_ok=True)
        shutil.copy2(local_path, local_dest)
        logger.info("[LOCAL] Saved processed file → %s", local_dest)
        return
    # END TEMP LOCAL STORAGE

    s3 = _s3_client()
    logger.info(
        "Uploading processed file → s3://%s/%s",
        settings.S3_PROCESSED_BUCKET,
        s3_key,
    )
    s3.upload_file(
        local_path,
        settings.S3_PROCESSED_BUCKET,
        s3_key,
        ExtraArgs={"ContentType": content_type},
    )

# ---------------------------------------------------------------------------
# SQLAlchemy (sync — Celery tasks are synchronous)
# ---------------------------------------------------------------------------

def _raw_sql(sql: str):
    from sqlalchemy import text
    return text(sql)

_sync_engine = None

def get_sync_engine():
        global _sync_engine
        if _sync_engine is None:
            url = settings.DATABASE_URL
            url = url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
            url = url.replace("postgresql+aiopg://", "postgresql+psycopg2://")
            _sync_engine = create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=0)
        return _sync_engine

def get_model_row(engine, model_id: str) -> dict | None:
    """Fetch model row as a plain dict. Returns None if not found."""
    with engine.connect() as conn:
        row = conn.execute(
            _raw_sql(
                "SELECT id, uploaded_by, s3_raw_key, s3_processed_prefix, "
                "status, file_format "
                "FROM models WHERE id = :mid"
            ),
            {"mid": model_id},
        ).fetchone()
    if row is None:
        return None
    return dict(row._mapping)


def update_model_status(
    engine,
    model_id: str,
    status: str,
    s3_processed_prefix: str | None = None,
    error_message: str | None = None,
    element_count: int | None = None,
    bounds_min_xyz: list[float] | None = None,
    bounds_max_xyz: list[float] | None = None,
) -> None:
    set_clauses = "status = :status, updated_at = now()"
    params: dict = {"status": status, "model_id": model_id}
    if s3_processed_prefix is not None:
        set_clauses += ", s3_processed_prefix = :prefix"
        params["prefix"] = s3_processed_prefix
    if error_message is not None:
        set_clauses += ", error_message = :err"
        params["err"] = error_message[:2000]
    if element_count is not None:
        set_clauses += ", element_count = :element_count"
        params["element_count"] = element_count
    if bounds_min_xyz is not None:
        set_clauses += ", bounds_min_xyz = CAST(:bounds_min AS jsonb)"
        params["bounds_min"] = json.dumps(bounds_min_xyz)
    if bounds_max_xyz is not None:
        set_clauses += ", bounds_max_xyz = CAST(:bounds_max AS jsonb)"
        params["bounds_max"] = json.dumps(bounds_max_xyz)
    with engine.begin() as conn:
        conn.execute(
            _raw_sql(f"UPDATE models SET {set_clauses} WHERE id = :model_id"),
            params,
        )


def upsert_model_metadata(
    engine,
    model_id: str,
    properties: dict,
    spatial_tree: dict,
) -> None:
    with engine.begin() as conn:
        existing = conn.execute(
            _raw_sql("SELECT id FROM model_metadata WHERE model_id = :mid"),
            {"mid": model_id},
        ).fetchone()
        if existing:
            conn.execute(
                _raw_sql(
                    "UPDATE model_metadata "
                    "SET properties = CAST(:props AS jsonb), spatial_tree = CAST(:tree AS jsonb), "
                    "updated_at = now() "
                    "WHERE model_id = :mid"
                ),
                {
                    "props": json.dumps(properties),
                    "tree": json.dumps(spatial_tree),
                    "mid": model_id,
                },
            )
        else:
            conn.execute(
                _raw_sql(
                    "INSERT INTO model_metadata "
                    "(id, model_id, properties, spatial_tree, created_at, updated_at) "
                    "VALUES (:id, :mid, CAST(:props AS jsonb), CAST(:tree AS jsonb), now(), now())"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "mid": model_id,
                    "props": json.dumps(properties),
                    "tree": json.dumps(spatial_tree),
                },
            )


# ---------------------------------------------------------------------------
# Redis pub/sub
# ---------------------------------------------------------------------------

def publish_model_ready(user_id: str, model_id: str) -> None:
    """Publish model:ready event to the ws-server relay channel."""
    import redis as redis_lib

    channel = f"model_events:{user_id}"
    payload = json.dumps(
        {"event": "model:ready", "data": {"model_id": model_id, "status": "ready"}}
    )
    try:
        r = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
        r.publish(channel, payload)
        r.close()
        logger.info("Published model:ready → %s", channel)
    except Exception as exc:
        logger.error("Failed to publish Redis event: %s", exc)


def publish_model_failed(user_id: str, model_id: str, error: str) -> None:
    """Publish model:failed event to the ws-server relay channel."""
    import redis as redis_lib

    try:
        r = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
        r.publish(
            f"model_events:{user_id}",
            json.dumps(
                {
                    "event": "model:failed",
                    "data": {"model_id": model_id, "error": error[:500]},
                }
            ),
        )
        r.close()
    except Exception as exc:
        logger.error("Failed to publish model:failed event: %s", exc)


# ---------------------------------------------------------------------------
# Node.js subprocess helpers (gltf-pipeline, gltf-validator)
# ---------------------------------------------------------------------------

import subprocess  # noqa: E402  (placed after logger setup intentionally)
def publish_model_progress(user_id: str, model_id: str, percent: int, stage: str = "") -> None:
    """
    Publish a model:progress event to the ws-server relay channel.
    percent: 0–100
    stage: human-readable stage name e.g. "download", "convert", "upload"
    """
    import redis as redis_lib

    channel = f"model_events:{user_id}"
    payload = json.dumps({
        "event": "model:progress",
        "data": {
            "model_id": model_id,
            "percent": percent,
            "stage": stage,
        },
    })
    try:
        r = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
        r.publish(channel, payload)
        r.close()
        logger.debug("[PROGRESS] model_id=%s %d%% stage=%s", model_id, percent, stage)
    except Exception as exc:
        logger.warning("[PROGRESS] Failed to publish progress event: %s", exc)

def run_node_tool(
    cmd: list[str],
    timeout: int = 300,
    tool_name: str = "node tool",
) -> str:
    """
    Run an arbitrary Node.js CLI tool as a subprocess.
    Returns stdout. Raises RuntimeError on non-zero exit or timeout.
    Raises FileNotFoundError if the binary is missing (caller handles gracefully).
    """
    logger.info("Running %s: %s", tool_name, " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"{tool_name} exited {result.returncode}: {result.stderr[:600]}"
        )
    if result.stdout:
        logger.debug("%s stdout: %s", tool_name, result.stdout[:400])
    return result.stdout


# ---------------------------------------------------------------------------
# GLTF chunk splitting (32 MB limit per PRD)
# ---------------------------------------------------------------------------

GLTF_CHUNK_MAX_BYTES = 32 * 1024 * 1024  # 32 MB


def split_binary_chunks(
    source_path: str,
    output_dir: str,
    stem: str,
    ext: str,
    max_bytes: int = GLTF_CHUNK_MAX_BYTES,
) -> list[str]:
    """
    Binary-split a file into chunks of at most max_bytes each.
    Returns list of chunk paths. If the file fits in one chunk, returns [source_path].
    """
    size = Path(source_path).stat().st_size
    if size <= max_bytes:
        return [source_path]

    logger.info(
        "Splitting %s (%d bytes) into %d-byte chunks", source_path, size, max_bytes
    )
    chunks: list[str] = []
    with open(source_path, "rb") as fh:
        part = 0
        while True:
            data = fh.read(max_bytes)
            if not data:
                break
            chunk_path = Path(output_dir) / f"{stem}_part{part}{ext}"
            chunk_path.write_bytes(data)
            chunks.append(str(chunk_path))
            part += 1
    return chunks


# ---------------------------------------------------------------------------
# Idempotency guard — Week 3 Day 2
# ---------------------------------------------------------------------------
#
# acks_late=True + reject_on_worker_lost=True means a worker crash mid-task
# causes Celery to redeliver the same task to another worker. Without a
# guard, the redelivered task reprocesses the file from scratch, re-uploads
# chunks, and can double-publish a model:ready Redis event to the client.
#
# This guard is intentionally lightweight (a single status read) rather
# than a distributed lock — false negatives (allowing a duplicate run) are
# tolerable since downstream writes are idempotent UPSERTs; the goal is to
# skip the expensive case (model already "ready") cheaply.

_TERMINAL_STATUSES = {"ready", "failed"}


def is_already_processed(engine, model_id: str) -> bool:
    """
    Returns True if the model has already reached a terminal status
    ("ready" or "failed") — meaning a previous delivery of this task
    already completed the work.

    Call this immediately after get_model_row() and before any download,
    parse, or upload work begins. If True, the caller should return early
    without re-publishing events or re-uploading files.
    """
    row = get_model_row(engine, model_id)
    if row is None:
        return False
    return row.get("status") in _TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# Redis task lock — Week 3 Day 2
# ---------------------------------------------------------------------------

_TASK_LOCK_TTL = 2100  # 35 min — longer than IFC task time_limit=1800s


def acquire_task_lock(model_id: str, task_name: str) -> bool:
    """
    Try to acquire a per-model processing lock in Redis using SET NX EX.
    Returns True if this worker holds the lock, False if a duplicate.
    Fails open (returns True) if Redis is unavailable.
    """
    import redis as redis_lib

    lock_key = f"task_lock:{task_name}:{model_id}"
    try:
        r = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
        acquired = r.set(lock_key, "1", nx=True, ex=_TASK_LOCK_TTL)
        r.close()
        if not acquired:
            logger.warning(
                "[LOCK] Duplicate task — another worker holds lock "
                "model_id=%s task=%s. Skipping.", model_id, task_name,
            )
        return bool(acquired)
    except Exception as exc:
        logger.error("[LOCK] Lock acquisition failed model_id=%s: %s — continuing", model_id, exc)
        return True  # fail-open


def release_task_lock(model_id: str, task_name: str) -> None:
    """Release the processing lock on task completion or permanent failure."""
    import redis as redis_lib

    lock_key = f"task_lock:{task_name}:{model_id}"
    try:
        r = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
        r.delete(lock_key)
        r.close()
    except Exception as exc:
        logger.warning("[LOCK] Failed to release lock model_id=%s: %s", model_id, exc)


# ---------------------------------------------------------------------------
# Webhook fan-out (sync) — Week 3 Day 2
# ---------------------------------------------------------------------------
#
# The API's async dispatch_event() handles webhook fan-out for events
# triggered by HTTP requests. Pipeline tasks run synchronously inside
# Celery and need the equivalent fan-out without an asyncio event loop.
# This mirrors app.services.webhooks.dispatch_event's query/filter logic
# using the same sync engine pattern as the rest of this module.

def dispatch_webhook_event(engine, event: str, payload: dict, user_id: str) -> None:
    """
    Find all active webhooks for a user subscribed to `event` and enqueue
    delivery via Celery. Called by pipeline tasks on terminal completion
    (e.g. "model.ready", "model.failed") — never called from the API layer
    for processing-pipeline events, since the worker is the only component
    that knows when processing has actually finished.
    """
    from celery import Celery

    with engine.connect() as conn:
        rows = conn.execute(
            _raw_sql(
                "SELECT id, events FROM webhooks "
                "WHERE user_id = :uid AND is_active = true"
            ),
            {"uid": user_id},
        ).fetchall()

    celery_client = Celery(broker=settings.REDIS_URL)

    for row in rows:
        webhook_id, events = row[0], row[1]
        if events and event in events:
            celery_client.send_task(
                "app.tasks.webhook.dispatch_webhook",
                args=[str(webhook_id), event, payload],
                queue="webhook",
            )

# ---------------------------------------------------------------------------
# Sweeper Task
# ---------------------------------------------------------------------------

@celery_app.task(name="app.tasks.common.cleanup_abandoned_uploads")
def cleanup_abandoned_uploads():
    """Find 'pending' models > 24h, mark 'failed', delete from S3/local."""
    engine = get_sync_engine()
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    
    with engine.connect() as conn:
        rows = conn.execute(
            _raw_sql("SELECT id, s3_raw_key FROM models WHERE status = 'pending' AND created_at < :cutoff"),
            {"cutoff": cutoff}
        ).fetchall()
        
    for row in rows:
        model_id, s3_key = str(row[0]), row[1]
        logger.info("[SWEEPER] Cleaning up abandoned upload model_id=%s", model_id)
        update_model_status(
            engine,
            model_id,
            "failed",
            error_message="Upload expired — not confirmed within 24 hours"
        )
        try:
            from app.tasks.scan import _delete_s3_object
            _delete_s3_object(s3_key)
        except Exception as exc:
            logger.error("[SWEEPER] Failed to delete abandoned object %s: %s", s3_key, exc)