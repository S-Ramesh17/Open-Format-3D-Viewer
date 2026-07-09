"""
Public share link endpoints.

POST /v1/share              — create a share link (auth required)
GET  /v1/share/{token}      — resolve a share link (public, no auth)
DELETE /v1/share/{link_id}  — revoke a share link (auth required, owner only)
GET  /v1/share/model/{model_id} — list active share links for a model (auth required)
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.responses import envelope
from app.core.authorization import get_project_member, require_role_for_project
from app.core.dependencies import get_current_user
from app.db.engine import get_db
from app.models.model import Model
from app.models.user import User
from app.schemas.share import ShareLinkCreateRequest, ShareLinkResponse
from app.services import share as share_svc
from sqlalchemy import select

router = APIRouter(prefix="/v1/share", tags=["share"])


@router.post("", status_code=201)
async def create_share_link(
    body: ShareLinkCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
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
    return envelope(ShareLinkResponse.model_validate(link).model_dump(mode="json"), status_code=201)


@router.get("/{token}")
async def resolve_share_link(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Public endpoint — no authentication required.
    Returns the model data accessible via this share link.
    """
    link, model_response = await share_svc.get_share_link(token, db)
    return envelope({
        "link": ShareLinkResponse.model_validate(link).model_dump(mode="json"),
        "model": model_response.model_dump(mode="json"),
    })


@router.delete("/{link_id}", status_code=204)
async def revoke_share_link(
    link_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await share_svc.revoke_share_link(link_id, current_user.id, db)


@router.get("/model/{model_id}")
async def list_share_links(
    model_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Model).where(Model.id == model_id))
    model = result.scalar_one_or_none()
    if not model:
        from app.core.exceptions import NotFoundException
        raise NotFoundException("Model not found")
    await get_project_member(model.project_id, current_user, db)

    links = await share_svc.list_share_links(model_id, db)
    return envelope([ShareLinkResponse.model_validate(link).model_dump(mode="json") for link in links])