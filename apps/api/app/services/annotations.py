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
        position=data.position,
        status="open",
    )
    db.add(annotation)
    await db.commit()
    await db.refresh(annotation)

    

    await publish_model_event(
        str(user_id),
        "annotation:update",
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
) -> list[AnnotationResponse]:
    query = select(Annotation).where(Annotation.model_id == model_id)
    if status_filter:
        query = query.where(Annotation.status == status_filter)
    query = query.order_by(Annotation.created_at.desc())

    result = await db.execute(query)
    rows = result.scalars().all()
    return [AnnotationResponse.model_validate(r) for r in rows]


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