import base64
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException
from app.core.sanitize import sanitize_text
from app.models.annotation import Annotation
from app.models.annotation_comment import AnnotationComment
from app.schemas.annotations import (
    AnnotationCreate,
    AnnotationResponse,
    AnnotationUpdate,
    CommentCreate,
    CommentResponse,
)
from app.core.redis import publish_model_event
from app.services.webhooks import dispatch_event


# ── Cursor helpers (identical pattern to services/projects.py) ──────────────

def _encode_cursor(created_at: datetime, annotation_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{annotation_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_str, id_str = raw.split("|")
        return datetime.fromisoformat(ts_str), uuid.UUID(id_str)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("Invalid pagination cursor") from exc


async def create_annotation(
    model_id: uuid.UUID,
    data: AnnotationCreate,
    user_id: uuid.UUID,
    db: AsyncSession,
) -> AnnotationResponse:
    annotation = Annotation(
        id=uuid.uuid4(),
        model_id=model_id,
        created_by=user_id,
        title=sanitize_text(data.title),
        body=sanitize_text(data.body) if data.body else None,
        position=data.position.model_dump(),
        status="open",
    )
    db.add(annotation)
    await db.commit()
    await db.refresh(annotation)

    

    await publish_model_event(
        str(user_id),
        "ANNOTATION_CREATED",
        {"annotation_id": str(annotation.id), "model_id": str(model_id), "action": "created"},
    )
    await dispatch_event(
        "annotation.created", {"annotation_id": str(annotation.id)}, user_id, db
    )

    return AnnotationResponse.model_validate(annotation)

async def list_annotations(
    model_id: uuid.UUID,
    db: AsyncSession,
    status_filter: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> tuple[list[AnnotationResponse], str | None]:
    """
    List annotations for a model with cursor pagination.
    Returns (annotations, next_cursor). next_cursor is None when exhausted.
    Ordered by created_at DESC, id DESC for stable pagination — identical
    pattern to services/projects.py::list_projects.
    """
    query = select(Annotation).where(Annotation.model_id == model_id)
    if status_filter:
        query = query.where(Annotation.status == status_filter)

    if cursor:
        cursor_ts, cursor_id = _decode_cursor(cursor)
        query = query.where(
            (Annotation.created_at < cursor_ts)
            | (
                (Annotation.created_at == cursor_ts)
                & (Annotation.id < cursor_id)
            )
        )

    query = query.order_by(
        Annotation.created_at.desc(),
        Annotation.id.desc(),
    ).limit(limit + 1)

    result = await db.execute(query)
    rows = result.scalars().all()

    has_more = len(rows) > limit
    rows = rows[:limit]

    annotations = [AnnotationResponse.model_validate(r) for r in rows]

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)

    return annotations, next_cursor


async def get_annotation(annotation_id: uuid.UUID, db: AsyncSession) -> Annotation:
    """Returns the ORM object (not response schema) for internal use by update/comments."""
    result = await db.execute(select(Annotation).where(Annotation.id == annotation_id))
    annotation = result.scalar_one_or_none()
    if not annotation:
        raise NotFoundException("Annotation not found")
    return annotation


async def update_annotation(
    annotation_id: uuid.UUID,
    data: AnnotationUpdate,
    db: AsyncSession,
) -> AnnotationResponse:
    annotation = await get_annotation(annotation_id, db)

    if data.title is not None:
        annotation.title = sanitize_text(data.title)
    if data.body is not None:
        annotation.body = sanitize_text(data.body)
    if data.status is not None:
        annotation.status = data.status

    annotation.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(annotation)

    await publish_model_event(
        str(annotation.created_by),
        "ANNOTATION_UPDATED",
        {"annotation_id": str(annotation.id), "model_id": str(annotation.model_id), "action": "updated", "status": annotation.status},
    )

    return AnnotationResponse.model_validate(annotation)


async def create_comment(
    annotation_id: uuid.UUID,
    data: CommentCreate,
    user_id: uuid.UUID,
    db: AsyncSession,
) -> CommentResponse:
    # Verify annotation exists (also confirms model_id chain for authorization upstream)
    await get_annotation(annotation_id, db)

    comment = AnnotationComment(
        id=uuid.uuid4(),
        annotation_id=annotation_id,
        author_id=user_id,
        body=sanitize_text(data.body),
    )
    db.add(comment)
    await db.commit()
    await db.refresh(comment)
    return CommentResponse.model_validate(comment)


async def list_comments(
    annotation_id: uuid.UUID, db: AsyncSession
) -> list[CommentResponse]:
    result = await db.execute(
        select(AnnotationComment)
        .where(AnnotationComment.annotation_id == annotation_id)
        .order_by(AnnotationComment.created_at.asc())
    )
    rows = result.scalars().all()
    return [CommentResponse.model_validate(r) for r in rows]