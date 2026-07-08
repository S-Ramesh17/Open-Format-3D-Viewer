"""
BCF 2.1 export task — generates BCF archive, uploads to S3, publishes completion event.

Flow:
  1. Fetch model row + all annotations + comments from PostgreSQL
  2. Build BCF 2.1 ZIP archive (bcf.version + per-topic markup.bcf)
  3. Upload archive to S3 processed bucket under processed/{model_id}/export.bcf
  4. Publish Redis event bcf_events:{user_id} with a presigned download URL
  5. Celery retries on transient S3/Redis errors (up to 3 times)
"""

from __future__ import annotations

import io
import json
import logging
import os
import uuid as uuid_module
import zipfile
from xml.etree.ElementTree import Element, SubElement, tostring

import boto3
from botocore.config import Config as BotoConfig
from celery import Task

from app.celery_app import celery_app
from app.config import settings
from app.tasks.common import get_sync_engine, _raw_sql

logger = logging.getLogger(__name__)

BCF_VERSION_XML = '<?xml version="1.0" encoding="UTF-8"?>\n<Version VersionId="2.1" xsi:noNamespaceSchemaLocation="version.xsd" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><DetailedVersion>2.1</DetailedVersion></Version>'


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------

def _s3_client():
    return boto3.client(
        "s3",
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID or None,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY or None,
        config=BotoConfig(signature_version="s3v4"),
    )


def _upload_bcf(archive_bytes: bytes, s3_key: str) -> None:
    """Upload BCF ZIP archive to processed storage (S3 or local)."""
    # TEMP LOCAL STORAGE
    if settings.STORAGE_PROVIDER == "local":
        local_dest = os.path.join(settings.LOCAL_STORAGE_PATH, "processed", s3_key)
        os.makedirs(os.path.dirname(local_dest), exist_ok=True)
        with open(local_dest, "wb") as f:
            f.write(archive_bytes)
        logger.info("[BCF][LOCAL] Saved BCF → %s (%d bytes)", local_dest, len(archive_bytes))
        return
    # END TEMP LOCAL STORAGE

    s3 = _s3_client()
    logger.info("[BCF] Uploading %d bytes → s3://%s/%s", len(archive_bytes), settings.S3_PROCESSED_BUCKET, s3_key)
    s3.put_object(
        Bucket=settings.S3_PROCESSED_BUCKET,
        Key=s3_key,
        Body=archive_bytes,
        ContentType="application/zip",
    )

def _presigned_download_url(s3_key: str, expires_in: int = 3600) -> str:
    """Generate a download URL for the BCF archive."""
    # TEMP LOCAL STORAGE — API serves local files via /files/ endpoint
    if settings.STORAGE_PROVIDER == "local":
        return f"/files/{s3_key}"
    # END TEMP LOCAL STORAGE

    s3 = _s3_client()
    try:
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.S3_PROCESSED_BUCKET, "Key": s3_key},
            ExpiresIn=expires_in,
        )
    except Exception as exc:
        logger.error("[BCF] Failed to generate presigned URL: %s", exc)
        return f"{settings.CDN_BASE_URL.rstrip('/')}/{s3_key}"

# ---------------------------------------------------------------------------
# PostgreSQL helpers (sync)
# ---------------------------------------------------------------------------

def _get_model_and_user(engine, model_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            _raw_sql(
                "SELECT m.id, m.project_id, m.uploaded_by, m.original_filename "
                "FROM models m WHERE m.id = :mid"
            ),
            {"mid": model_id},
        ).fetchone()
    return dict(row._mapping) if row else None


def _get_annotations(engine, model_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            _raw_sql(
                "SELECT id, title, body, status, position, created_at, updated_at "
                "FROM annotations WHERE model_id = :mid ORDER BY created_at ASC"
            ),
            {"mid": model_id},
        ).fetchall()
    return [dict(r._mapping) for r in rows]


def _get_comments(engine, annotation_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            _raw_sql(
                "SELECT id, author_id, body, created_at "
                "FROM annotation_comments WHERE annotation_id = :aid ORDER BY created_at ASC"
            ),
            {"aid": annotation_id},
        ).fetchall()
    return [dict(r._mapping) for r in rows]


# ---------------------------------------------------------------------------
# BCF 2.1 archive builder
# ---------------------------------------------------------------------------

def _build_markup_xml(annotation: dict, comments: list[dict]) -> bytes:
    """Build a BCF 2.1 markup.bcf XML document for a single topic."""
    root = Element("Markup")
    root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")

    topic_status = "Closed" if annotation.get("status") == "resolved" else "Open"
    topic = SubElement(root, "Topic", {
        "Guid": str(annotation["id"]),
        "TopicType": "Issue",
        "TopicStatus": topic_status,
    })

    title_el = SubElement(topic, "Title")
    title_el.text = annotation.get("title") or ""

    if annotation.get("body"):
        desc_el = SubElement(topic, "Description")
        desc_el.text = annotation["body"]

    creation_date = SubElement(topic, "CreationDate")
    creation_date_val = annotation.get("created_at")
    creation_date.text = (
        creation_date_val.isoformat()
        if hasattr(creation_date_val, "isoformat")
        else str(creation_date_val)
    )

    modified_date = SubElement(topic, "ModifiedDate")
    modified_val = annotation.get("updated_at")
    modified_date.text = (
        modified_val.isoformat()
        if hasattr(modified_val, "isoformat")
        else str(modified_val)
    )

    # Viewpoint reference (position data as BCFViewpoint)
    position = annotation.get("position")
    if position and isinstance(position, dict):
        viewpoint_el = SubElement(root, "Viewpoints", {"Guid": str(uuid_module.uuid4())})
        vp_ref = SubElement(viewpoint_el, "Viewpoint")
        vp_ref.text = "viewpoint.bcfv"

    # Comments
    for comment in comments:
        comment_el = SubElement(root, "Comment", {"Guid": str(comment["id"])})
        date_el = SubElement(comment_el, "Date")
        comment_date = comment.get("created_at")
        date_el.text = (
            comment_date.isoformat()
            if hasattr(comment_date, "isoformat")
            else str(comment_date)
        )
        comment_text_el = SubElement(comment_el, "Comment")
        comment_text_el.text = comment.get("body") or ""
        SubElement(comment_el, "Topic", {"Guid": str(annotation["id"])})

    return tostring(root, encoding="utf-8", xml_declaration=True)


def _build_viewpoint_xml(position: dict) -> bytes:
    """Build a minimal BCFViewpoint XML from a position dict."""
    root = Element("VisualizationInfo")
    root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")
    root.set("Guid", str(uuid_module.uuid4()))

    camera = SubElement(root, "PerspectiveCamera")
    cp = SubElement(camera, "CameraViewPoint")
    SubElement(cp, "X").text = str(position.get("x", 0))
    SubElement(cp, "Y").text = str(position.get("y", 0))
    SubElement(cp, "Z").text = str(position.get("z", 0))

    cd = SubElement(camera, "CameraDirection")
    SubElement(cd, "X").text = str(position.get("normal_x", 0))
    SubElement(cd, "Y").text = str(position.get("normal_y", 0))
    SubElement(cd, "Z").text = str(position.get("normal_z", -1))

    cu = SubElement(camera, "CameraUpVector")
    SubElement(cu, "X").text = "0"
    SubElement(cu, "Y").text = "1"
    SubElement(cu, "Z").text = "0"

    SubElement(camera, "FieldOfView").text = "60"

    return tostring(root, encoding="utf-8", xml_declaration=True)


def _build_bcf_archive(annotations: list[dict], comments_by_annotation: dict) -> bytes:
    """
    Construct a BCF 2.1 ZIP archive in memory.
    Returns the raw bytes of the ZIP file.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # Root-level version file
        zf.writestr("bcf.version", BCF_VERSION_XML)

        for annotation in annotations:
            topic_guid = str(annotation["id"])
            comments = comments_by_annotation.get(topic_guid, [])

            markup_bytes = _build_markup_xml(annotation, comments)
            zf.writestr(f"{topic_guid}/markup.bcf", markup_bytes)

            # Optional viewpoint
            position = annotation.get("position")
            if position and isinstance(position, dict):
                try:
                    vp_bytes = _build_viewpoint_xml(position)
                    zf.writestr(f"{topic_guid}/viewpoint.bcfv", vp_bytes)
                except Exception as exc:
                    logger.debug("[BCF] Skipping viewpoint for %s: %s", topic_guid, exc)

    buffer.seek(0)
    return buffer.read()


# ---------------------------------------------------------------------------
# Redis publish
# ---------------------------------------------------------------------------

def _publish_bcf_ready(user_id: str, model_id: str, download_url: str) -> None:
    import redis as redis_lib

    try:
        r = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
        r.publish(
            f"model_events:{user_id}",
            json.dumps({
                "event": "bcf:ready",
                "data": {
                    "model_id": model_id,
                    "download_url": download_url,
                },
            }),
        )
        r.close()
        logger.info("[BCF] Published bcf:ready event for model_id=%s", model_id)
    except Exception as exc:
        logger.error("[BCF] Failed to publish bcf:ready event: %s", exc)


def _publish_bcf_failed(user_id: str, model_id: str, error: str) -> None:
    import redis as redis_lib

    try:
        r = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
        r.publish(
            f"model_events:{user_id}",
            json.dumps({
                "event": "bcf:failed",
                "data": {"model_id": model_id, "error": error[:500]},
            }),
        )
        r.close()
    except Exception as exc:
        logger.error("[BCF] Failed to publish bcf:failed event: %s", exc)


# ---------------------------------------------------------------------------
# Main Celery task
# ---------------------------------------------------------------------------

@celery_app.task(
    name="app.tasks.bcf.export_bcf",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="bcf",
)
def export_bcf(self: Task, model_id: str) -> dict:
    """
    Generate a BCF 2.1 export for all annotations on a model.

    Parameters
    ----------
    model_id : str — model UUID string
    """
    logger.info("[BCF] Starting BCF export for model_id=%s", model_id)

    engine = get_sync_engine()

    model = _get_model_and_user(engine, model_id)
    if model is None:
        logger.error("[BCF] Model %s not found — aborting", model_id)
        return {"model_id": model_id, "status": "not_found"}

    user_id = str(model["uploaded_by"])

    try:
        # ── 1. Fetch annotations ────────────────────────────────────────
        annotations = _get_annotations(engine, model_id)
        logger.info("[BCF] Found %d annotations for model_id=%s", len(annotations), model_id)

        # ── 2. Fetch comments per annotation ────────────────────────────
        comments_by_annotation: dict[str, list[dict]] = {}
        for annotation in annotations:
            topic_guid = str(annotation["id"])
            comments_by_annotation[topic_guid] = _get_comments(engine, topic_guid)

        # ── 3. Build BCF archive in memory ──────────────────────────────
        archive_bytes = _build_bcf_archive(annotations, comments_by_annotation)
        logger.info("[BCF] Archive built: %d bytes", len(archive_bytes))

        # ── 4. Upload to S3 ─────────────────────────────────────────────
        s3_key = f"processed/{model_id}/export.bcf"
        _upload_bcf(archive_bytes, s3_key)

        # ── 5. Generate presigned download URL ───────────────────────────
        download_url = _presigned_download_url(s3_key, expires_in=3600)

        # ── 6. Publish Redis event ───────────────────────────────────────
        _publish_bcf_ready(user_id, model_id, download_url)

        logger.info("[BCF] Export complete for model_id=%s → %s", model_id, s3_key)
        return {
            "model_id": model_id,
            "status": "exported",
            "s3_key": s3_key,
            "annotation_count": len(annotations),
            "archive_bytes": len(archive_bytes),
        }

    except Exception as exc:
        logger.exception("[BCF] Export failed for model_id=%s: %s", model_id, exc)

        import botocore.exceptions
        if isinstance(exc, (ConnectionError, OSError, botocore.exceptions.EndpointConnectionError)):
            raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1))

        _publish_bcf_failed(user_id, model_id, str(exc))
        return {
            "model_id": model_id,
            "status": "failed",
            "error": str(exc)[:400],
        }
