import base64
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException, UnsupportedFormatException, ValidationException
from app.models.model import Model
from app.models.user import User
from app.models.model_element import ModelElement
from app.models.model_metadata import ModelMetadata
from app.schemas.models import (
    ModelElementResponse,
    ModelResponse,
    ModelUploadRequest,
)

import app.services.storage as _storage_svc

from app.services.storage import (
    build_storage_key,
    fetch_object_header_bytes,
    generate_presigned_upload_url,
    validate_file_size,
    validate_filename,
    validate_mime_type_declared,
    validate_mime_type_from_bytes,
    verify_object_exists,
)

from app.core.redis import publish_model_event

logger = logging.getLogger(__name__)

EXT_TO_FORMAT = {
    ".ifc": "ifc",
    ".gltf": "gltf",
    ".glb": "glb",
    ".step": "step",
    ".stp": "stp",
    ".obj": "obj",
    ".stl": "stl",
}


def _encode_cursor(created_at: datetime, element_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{element_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_str, id_str = raw.split("|", 1)
        return datetime.fromisoformat(ts_str), uuid.UUID(id_str)
    except Exception:
        raise ValidationException("Invalid pagination cursor")


async def list_models(
    project_id: uuid.UUID,
    db: AsyncSession,
    limit: int = 20,
    cursor: str | None = None,
) -> tuple[list[ModelResponse], str | None]:
    """
    List models belonging to a project, ordered by created_at DESC, id DESC.
    Cursor-paginated. Project membership is enforced by the router before this call.
    """
    query = select(Model).where(Model.project_id == project_id)

    if cursor:
        cursor_ts, cursor_id = _decode_cursor(cursor)
        query = query.where(
            (Model.created_at < cursor_ts)
            | ((Model.created_at == cursor_ts) & (Model.id < cursor_id))
        )

    query = query.order_by(Model.created_at.desc(), Model.id.desc()).limit(limit + 1)

    result = await db.execute(query)
    rows = result.scalars().all()

    has_more = len(rows) > limit
    rows = rows[:limit]

    models = [ModelResponse.model_validate(r) for r in rows]

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)

    return models, next_cursor


PLAN_DAILY_UPLOAD_LIMITS = {
    "free": 5,
    "pro": 100,
    "enterprise": float("inf")
}

async def initiate_upload(
    data: ModelUploadRequest,
    user: User,
    db: AsyncSession,
) -> tuple[uuid.UUID, str, str]:
    safe_filename = validate_filename(data.filename)
    validate_file_size(data.size_bytes, user.plan)
    validate_mime_type_declared(data.content_type)
    
    limit = PLAN_DAILY_UPLOAD_LIMITS.get(user.plan, PLAN_DAILY_UPLOAD_LIMITS["free"])
    if limit < float("inf"):
        import redis
        from app.config import settings
        r = redis.from_url(settings.REDIS_URL, decode_responses=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        redis_key = f"uploads:{user.id}:{today}"
        try:
            count = int(r.get(redis_key) or 0)
            if count >= limit:
                r.close()
                raise ValidationException(f"Daily upload limit of {limit} reached for {user.plan} plan.")
            r.incr(redis_key)
            if count == 0:
                r.expire(redis_key, 86400 * 2)
        except redis.RedisError:
            pass
        finally:
            r.close()

    ext = "." + safe_filename.rsplit(".", 1)[-1].lower()
    file_format = EXT_TO_FORMAT.get(ext)
    if not file_format:
        raise UnsupportedFormatException(f"Unsupported file format: {ext}")

    model_id = uuid.uuid4()
    storage_key = build_storage_key(user.id, model_id, safe_filename)

    model = Model(
        id=model_id,
        project_id=data.project_id,
        uploaded_by=user.id,
        original_filename=safe_filename,
        name=data.name or safe_filename,
        format=file_format,
        raw_s3_key=storage_key,
        file_size_bytes=data.size_bytes,
        status="pending",
    )
    db.add(model)
    await db.commit()

    upload_url = generate_presigned_upload_url(
        storage_key=storage_key,
        content_type=data.content_type,
        size_bytes=data.size_bytes,
    )

    return model_id, upload_url, storage_key


async def confirm_upload(model_id: uuid.UUID, db: AsyncSession) -> ModelResponse:
    result = await db.execute(select(Model).where(Model.id == model_id))
    model = result.scalar_one_or_none()
    if not model:
        raise NotFoundException("Model not found")

    if model.status != "pending":
        raise ValidationException(f"Model is not in pending state (current: {model.status})")

    s3_meta = verify_object_exists(model.raw_s3_key)

    if model.file_size_bytes and abs(s3_meta["size_bytes"] - model.file_size_bytes) > 1024:
        model.status = "failed"
        model.error_message = "Uploaded file size does not match declared size"
        await db.commit()
        raise ValidationException("Uploaded file size mismatch")

    # Authoritative MIME validation against actual bytes
    try:
        header_bytes = fetch_object_header_bytes(model.raw_s3_key)
        validate_mime_type_from_bytes(header_bytes, model.original_filename)
    except ValidationException as exc:
        model.status = "failed"
        model.error_message = str(exc.message)
        await db.commit()
        raise

    model.status = "processing"
    model.file_size_bytes = s3_meta["size_bytes"]
    model.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(model)
    
    _storage_svc.trigger_clamav_scan(str(model.id), model.raw_s3_key)

    await publish_model_event(
        str(model.uploaded_by),
        "MODEL_SYNC",
        {"model_id": str(model.id), "status": "processing"},
    )

    return ModelResponse.model_validate(model)


def _enqueue_processing_task(model: "Model") -> None:  # noqa: F821
    """
    Patchable seam used by tests to intercept processing dispatch.
    In production this function is never called directly — the scan task
    (app.tasks.scan.scan_file) dispatches the processing task after
    confirming the file is clean. Kept here so tests can patch it at
    ``app.services.models._enqueue_processing_task`` without error.
    """
    # No-op in production — dispatch happens in scan task.
    pass


async def get_model(model_id: uuid.UUID, db: AsyncSession) -> ModelResponse:
    result = await db.execute(select(Model).where(Model.id == model_id))
    model = result.scalar_one_or_none()
    if not model:
        raise NotFoundException("Model not found")
    return ModelResponse.model_validate(model)


async def delete_model(model_id: uuid.UUID, db: AsyncSession) -> None:
    result = await db.execute(select(Model).where(Model.id == model_id))
    model = result.scalar_one_or_none()
    if not model:
        raise NotFoundException("Model not found")

    # Best-effort storage cleanup: a transient S3 error here shouldn't block
    # the user's explicit delete request (the DB row staying vs. not is the
    # part the user is actually waiting on) — log loudly instead so an
    # orphaned object can be found and swept later, same as the worker's
    # existing cleanup_abandoned_uploads sweeper does for abandoned uploads.
    try:
        _storage_svc.delete_raw_object(model.raw_s3_key)
    except Exception:
        logger.exception(
            "Failed to delete raw storage object for model_id=%s (raw_s3_key=%s) — "
            "orphaned object requires manual cleanup",
            model_id, model.raw_s3_key,
        )
    try:
        _storage_svc.delete_processed_objects(model.processed_s3_prefix)
    except Exception:
        logger.exception(
            "Failed to delete processed storage objects for model_id=%s (prefix=%s) — "
            "orphaned objects require manual cleanup",
            model_id, model.processed_s3_prefix,
        )

    await db.delete(model)
    await db.commit()


async def list_elements(
    model_id: uuid.UUID,
    db: AsyncSession,
    limit: int = 50,
    cursor: str | None = None,
    ifc_type: str | None = None,
    search: str | None = None,
) -> tuple[list[ModelElementResponse], str | None]:
    query = select(ModelElement).where(ModelElement.model_id == model_id)

    if ifc_type:
        query = query.where(ModelElement.element_type == ifc_type)

    if search:
        query = query.where(ModelElement.name.ilike(f"%{search}%"))

    if cursor:
        cursor_ts, cursor_id = _decode_cursor(cursor)
        query = query.where(
            (ModelElement.created_at < cursor_ts)
            | ((ModelElement.created_at == cursor_ts) & (ModelElement.id < cursor_id))
        )

    query = query.order_by(ModelElement.created_at.desc(), ModelElement.id.desc()).limit(limit + 1)

    result = await db.execute(query)
    rows = result.scalars().all()

    has_more = len(rows) > limit
    rows = rows[:limit]

    elements = [ModelElementResponse.model_validate(r) for r in rows]

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)

    return elements, next_cursor


async def get_element_by_guid(
    model_id: uuid.UUID, guid: str, db: AsyncSession
) -> ModelElementResponse:
    result = await db.execute(
        select(ModelElement).where(
            ModelElement.model_id == model_id,
            ModelElement.guid == guid,
        )
    )
    element = result.scalar_one_or_none()
    if not element:
        raise NotFoundException("Element not found")
    return ModelElementResponse.model_validate(element)


async def get_tree(model_id: uuid.UUID, db: AsyncSession) -> dict | None:
    result = await db.execute(
        select(ModelMetadata).where(ModelMetadata.model_id == model_id)
    )
    metadata = result.scalar_one_or_none()
    if not metadata:
        raise NotFoundException("Model metadata not found")
    return metadata.spatial_tree


async def get_chunks(model_id: uuid.UUID, db: AsyncSession) -> list[str]:
    """
    Return CDN URLs for all processed chunk files for a model.

    For IFC models: chunks are stored as model_metadata.properties.xkt_chunks
    For STEP/GLTF models: stored as model_metadata.properties.processed_keys
    Falls back to processed_s3_prefix if metadata has not been written yet.
    """
    from app.services.storage import build_cdn_url

    result = await db.execute(select(Model).where(Model.id == model_id))
    model = result.scalar_one_or_none()
    if not model:
        raise NotFoundException("Model not found")

    # Try to read explicit processed_keys from model_metadata first
    result = await db.execute(
        select(ModelMetadata).where(ModelMetadata.model_id == model_id)
    )
    metadata = result.scalar_one_or_none()

    if metadata and metadata.properties:
        props = metadata.properties

        # STEP/GLTF pipeline stores processed_keys list
        processed_keys = props.get("processed_keys")
        if processed_keys and isinstance(processed_keys, list):
            return [build_cdn_url(key) for key in processed_keys]

        # IFC pipeline stores xkt_chunks list
        xkt_chunks = props.get("xkt_chunks")
        if xkt_chunks and isinstance(xkt_chunks, list):
            return [build_cdn_url(key) for key in xkt_chunks]

    # Fallback: no explicit keys in metadata, prefix only
    if not model.processed_s3_prefix:
        return []

    return [build_cdn_url(model.processed_s3_prefix)]