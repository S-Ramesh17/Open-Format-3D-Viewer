"""
Share link service — create, resolve, revoke public read-only model links.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthorizationException, NotFoundException, ValidationException
from app.models.share_link import ShareLink
from app.models.model import Model
from app.models.model_metadata import ModelMetadata
from app.schemas.share import PublicModelResponse
from app.config import settings


def _generate_token() -> str:
    """Generate a 48-character URL-safe random token (288 bits of entropy)."""
    return secrets.token_urlsafe(36)


async def create_share_link(
    model_id: uuid.UUID,
    user_id: uuid.UUID,
    expires_at: datetime | None,
    db: AsyncSession,
) -> ShareLink:
    # Verify model exists (membership already checked in router before this call)
    result = await db.execute(select(Model).where(Model.id == model_id))
    if not result.scalar_one_or_none():
        raise NotFoundException("Model not found")

    link = ShareLink(
        id=uuid.uuid4(),
        model_id=model_id,
        created_by=user_id,
        token=_generate_token(),
        expires_at=expires_at,
        revoked=False,
    )
    db.add(link)
    await db.commit()
    await db.refresh(link)
    return link


async def get_share_link(token: str, db: AsyncSession) -> tuple[ShareLink, PublicModelResponse]:
    """
    Resolve a share token.
    Raises NotFoundException if invalid, expired, or revoked.
    Returns (link, public_model_response) for read-only access.
    """
    result = await db.execute(
        select(ShareLink).where(ShareLink.token == token)
    )
    link = result.scalar_one_or_none()

    if not link or link.revoked:
        raise NotFoundException("Share link not found or has been revoked")

    if link.expires_at and link.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise ValidationException("Share link has expired")

    model_result = await db.execute(select(Model).where(Model.id == link.model_id))
    model = model_result.scalar_one_or_none()
    if not model:
        raise NotFoundException("Model not found")

    meta_result = await db.execute(
        select(ModelMetadata).where(ModelMetadata.model_id == model.id)
    )
    metadata = meta_result.scalar_one_or_none()

    chunk_urls: list[str] = []
    if metadata and metadata.properties:
        keys = (
            metadata.properties.get("xkt_chunks")
            or metadata.properties.get("processed_keys")
            or []
        )
        if settings.STORAGE_PROVIDER == "local":
            chunk_urls = [f"/files/{k}" for k in keys]
        else:
            base_cdn = settings.CDN_BASE_URL.rstrip("/")
            chunk_urls = [f"{base_cdn}/{k}" for k in keys]

    public = PublicModelResponse(
        id=model.id,
        name=model.name or model.original_filename,
        format=model.format,
        status=model.status,
        created_at=model.created_at,
        updated_at=model.updated_at,
        chunk_urls=chunk_urls,
    )

    return link, public


async def revoke_share_link(
    link_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession,
) -> None:
    result = await db.execute(select(ShareLink).where(ShareLink.id == link_id))
    link = result.scalar_one_or_none()

    if not link:
        raise NotFoundException("Share link not found")

    if link.created_by != user_id:
        raise AuthorizationException("You do not have permission to revoke this share link")

    link.revoked = True
    await db.commit()


async def list_share_links(
    model_id: uuid.UUID,
    db: AsyncSession,
) -> list[ShareLink]:
    # BUG FIX: use SQLAlchemy == operator, not Python `is` operator.
    # `ShareLink.revoked is False` is always False in Python (Column is not
    # the same object as False), so the WHERE clause was never applied and
    # revoked links were being returned.
    result = await db.execute(
        select(ShareLink)
        .where(
            ShareLink.model_id == model_id,
            ShareLink.revoked == False,  # noqa: E712 — SQLAlchemy requires ==, not `is`
        )
        .order_by(ShareLink.created_at.desc())
    )
    return list(result.scalars().all())