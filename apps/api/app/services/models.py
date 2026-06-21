import base64
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException, ValidationException
from app.models.model import Model
from app.models.model_element import ModelElement
from app.models.model_metadata import ModelMetadata
from app.schemas.models import (
    ModelElementResponse,
    ModelResponse,
    ModelUploadRequest,
)
from app.services.storage import (
    build_storage_key,
    fetch_object_header_bytes,
    generate_presigned_upload_url,
    trigger_clamav_scan,
    validate_file_size,
    validate_filename,
    validate_mime_type_declared,
    validate_mime_type_from_bytes,
    verify_object_exists,
)

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


async def initiate_upload(
    data: ModelUploadRequest,
    user_id: uuid.UUID,
    db: AsyncSession,
) -> tuple[uuid.UUID, str, str]:
    safe_filename = validate_filename(data.filename)
    validate_file_size(data.size_bytes)
    validate_mime_type_declared(data.content_type)

    ext = "." + safe_filename.rsplit(".", 1)[-1].lower()
    file_format = EXT_TO_FORMAT.get(ext)
    if not file_format:
        raise ValidationException(f"Unsupported file format: {ext}")

    model_id = uuid.uuid4()
    storage_key = build_storage_key(user_id, model_id, safe_filename)

    model = Model(
        id=model_id,
        project_id=data.project_id,
        uploaded_by=user_id,
        original_filename=safe_filename,
        file_format=file_format,
        s3_raw_key=storage_key,
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

    s3_meta = verify_object_exists(model.s3_raw_key)

    if model.file_size_bytes and abs(s3_meta["size_bytes"] - model.file_size_bytes) > 1024:
        model.status = "failed"
        model.error_message = "Uploaded file size does not match declared size"
        await db.commit()
        raise ValidationException("Uploaded file size mismatch")

    # Authoritative MIME validation against actual bytes
    try:
        header_bytes = fetch_object_header_bytes(model.s3_raw_key)
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

    # ClamAV scan dispatched first (scan queue), processing task dispatched after.
    # passes (Celery chain/signature); scaffolded as parallel dispatch here.
    trigger_clamav_scan(model.s3_raw_key)
    _enqueue_processing_task(model)

    from app.core.redis import publish_model_event
    from app.services.webhooks import dispatch_event

    await publish_model_event(
        str(model.uploaded_by), "model:sync", {"model_id": str(model.id), "status": "processing"}
    )
    await dispatch_event(
        "model.ready", {"model_id": str(model.id)}, model.uploaded_by, db
    )

    return ModelResponse.model_validate(model)


def _enqueue_processing_task(model: Model) -> None:
    from celery import Celery
    from app.config import settings as api_settings

    celery_client = Celery(broker=api_settings.REDIS_URL)

    task_name = (
        "app.tasks.ifc.process_model"
        if model.file_format == "ifc"
        else "app.tasks.mesh.generate_chunks"
    )
    celery_client.send_task(task_name, args=[str(model.id)])


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
    from app.services.storage import build_cdn_url

    result = await db.execute(select(Model).where(Model.id == model_id))
    model = result.scalar_one_or_none()
    if not model:
        raise NotFoundException("Model not found")
    if not model.s3_processed_prefix:
        return []

    return [build_cdn_url(model.s3_processed_prefix)]