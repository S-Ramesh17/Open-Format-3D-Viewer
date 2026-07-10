"""
Share link service — create, resolve, revoke public read-only model links.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException, ValidationException
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
    # Verify model exists and belongs to user's project (membership checked in router)
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
    Resolve a share token. Raises NotFoundException if invalid, expired, or revoked.
    Returns (link, public_model_response) for read-only access.
    """
    result = await db.execute(
        select(ShareLink).where(ShareLink.token == token)
    )
    link = result.scalar_one_or_none()

    if not link or link.revoked:
        raise NotFoundException("Share link not found or has been revoked")

    if link.expires_at and link.expires_at < datetime.now(timezone.utc):
        raise ValidationException("Share link has expired")

    model_result = await db.execute(select(Model).where(Model.id == link.model_id))
    model = model_result.scalar_one_or_none()
    if not model:
        raise NotFoundException("Model not found")

    meta_result = await db.execute(select(ModelMetadata).where(ModelMetadata.model_id == model.id))
    metadata = meta_result.scalar_one_or_none()
    
    chunk_urls = []
    if metadata and metadata.properties:
        keys = metadata.properties.get("xkt_chunks") or metadata.properties.get("processed_keys") or []
        base_cdn = settings.CDN_BASE_URL.rstrip("/")
        chunk_urls = [f"{base_cdn}/{k}" for k in keys]

    # Convert to response dictionary to satisfy pydantic since model doesn't natively have chunk_urls
    model_data = {
        "id": model.id,
        "name": model.name,
        "file_format": model.file_format,
        "status": model.status,
        "created_at": model.created_at,
        "updated_at": model.updated_at,
        "chunk_urls": chunk_urls,
    }

    return link, PublicModelResponse.model_validate(model_data)


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
        from app.core.exceptions import AuthorizationException
        raise AuthorizationException("You do not have permission to revoke this share link")

    link.revoked = True
    await db.commit()


async def list_share_links(
    model_id: uuid.UUID,
    db: AsyncSession,
) -> list[ShareLink]:
    result = await db.execute(
        select(ShareLink)
        .where(ShareLink.model_id == model_id, ShareLink.revoked is False)
        .order_by(ShareLink.created_at.desc())
    )
    return list(result.scalars().all())