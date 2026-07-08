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
from app.schemas.models import ModelResponse


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


async def get_share_link(token: str, db: AsyncSession) -> tuple[ShareLink, ModelResponse]:
    """
    Resolve a share token. Raises NotFoundException if invalid, expired, or revoked.
    Returns (link, model_response) for read-only access.
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

    return link, ModelResponse.model_validate(model)


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
        .where(ShareLink.model_id == model_id, ShareLink.revoked == False)
        .order_by(ShareLink.created_at.desc())
    )
    return list(result.scalars().all())