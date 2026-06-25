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

import boto3
from botocore.config import Config as BotoConfig
from sqlalchemy import create_engine

from app.config import settings

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
    """Download a file from the raw S3 bucket to local disk."""
    s3 = _s3_client()
    logger.info("Downloading s3://%s/%s → %s", settings.S3_RAW_BUCKET, s3_key, dest_path)
    s3.download_file(settings.S3_RAW_BUCKET, s3_key, dest_path)


def upload_processed_file(
    local_path: str,
    s3_key: str,
    content_type: str = "application/octet-stream",
) -> None:
    """Upload a processed output file to the processed S3 bucket."""
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

sync_engine = None

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
) -> None:
    set_clauses = "status = :status, updated_at = now()"
    params: dict = {"status": status, "model_id": model_id}
    if s3_processed_prefix is not None:
        set_clauses += ", s3_processed_prefix = :prefix"
        params["prefix"] = s3_processed_prefix
    if error_message is not None:
        set_clauses += ", error_message = :err"
        params["err"] = error_message[:2000]
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
                    "SET properties = :props::jsonb, spatial_tree = :tree::jsonb, "
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
                    "VALUES (:id, :mid, :props::jsonb, :tree::jsonb, now(), now())"
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
