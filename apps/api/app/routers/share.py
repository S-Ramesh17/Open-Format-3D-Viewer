"""
Public share link endpoints.

POST /v1/share              — create a share link (auth required)
GET  /v1/share/{token}      — resolve a share link (public, no auth)
DELETE /v1/share/{link_id}  — revoke a share link (auth required, owner only)
GET  /v1/share/model/{model_id} — list active share links for a model (auth required)
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.responses import Envelope, envelope
from app.core.authorization import get_project_member, require_role_for_project
from app.core.dependencies import get_current_user
from app.db.engine import get_db
from app.models.model import Model
from app.models.user import User
from app.schemas.share import PublicModelResponse, ShareLinkCreateRequest, ShareLinkResponse
from app.services import share as share_svc
from sqlalchemy import select

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/share", tags=["share"])


class ShareLinkResolveResponse(BaseModel):
    """Documentation-only shape of GET /{token}'s combined payload."""

    link: ShareLinkResponse
    model: PublicModelResponse


@router.post(
    "",
    status_code=201,
    summary="Create a public share link for a model",
    responses={201: {"model": Envelope[ShareLinkResponse]}},
)
async def create_share_link(
    body: ShareLinkCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a public, unauthenticated share link for a model. Requires editor+ role."""
    # Verify model exists and requester is a project member
    result = await db.execute(select(Model).where(Model.id == body.model_id))
    model = result.scalar_one_or_none()
    if not model:
        from app.core.exceptions import NotFoundException
        raise NotFoundException("Model not found")
    await require_role_for_project(model.project_id, "editor", current_user, db)

    link = await share_svc.create_share_link(
        model_id=body.model_id,
        user_id=current_user.id,
        expires_at=body.expires_at,
        db=db,
    )
    logger.info(
        "share.created",
        extra={"user_id": str(current_user.id), "model_id": str(body.model_id), "share_link_id": str(link.id)},
    )
    return envelope(ShareLinkResponse.model_validate(link).model_dump(mode="json"), status_code=201)


@router.get(
    "/{token}",
    summary="Resolve a public share link (no auth required)",
    responses={200: {"model": Envelope[ShareLinkResolveResponse]}},
)
async def resolve_share_link(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    Public endpoint — no authentication required.
    Returns the model data accessible via this share link.
    """
    link, public_model_response = await share_svc.get_share_link(token, db)
    return envelope({
        "link": ShareLinkResponse.model_validate(link).model_dump(mode="json"),
        "model": public_model_response.model_dump(mode="json"),
    })


@router.delete(
    "/{link_id}",
    status_code=204,
    summary="Revoke a share link",
    responses={204: {"description": "Share link revoked"}},
)
async def revoke_share_link(
    link_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke a share link. Only the link's creator may revoke it."""
    await share_svc.revoke_share_link(link_id, current_user.id, db)
    logger.info(
        "share.revoked",
        extra={"user_id": str(current_user.id), "share_link_id": str(link_id)},
    )


@router.get(
    "/model/{model_id}",
    summary="List active share links for a model",
    responses={200: {"model": Envelope[list[ShareLinkResponse]]}},
)
async def list_share_links(
    model_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List active (non-revoked, non-expired) share links for a model."""
    result = await db.execute(select(Model).where(Model.id == model_id))
    model = result.scalar_one_or_none()
    if not model:
        from app.core.exceptions import NotFoundException
        raise NotFoundException("Model not found")
    await get_project_member(model.project_id, current_user, db)

    links = await share_svc.list_share_links(model_id, db)
    return envelope([ShareLinkResponse.model_validate(link).model_dump(mode="json") for link in links])
