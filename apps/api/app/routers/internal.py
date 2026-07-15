# apps/api/app/routers/internal.py
"""
Internal, service-to-service endpoints — NOT under /v1, NOT exposed to end
users, NOT documented for API consumers. Authenticated by a shared secret
header (X-Internal-Service-Key), not JWT/cookie/API-key.

Used by ws-server to authorize JOIN_MODEL requests: a WebSocket client can
request access to a model_id, and ws-server has no DB access of its own (by
design — see apps/ws-server), so it calls back here to ask "does this user
have access to this model?" using the exact same authorization rule every
other model endpoint already uses. The response also carries the user's
display name, since ws-server already needs a DB round-trip here and the
alternative (a second call just for USER_JOINED's name field) would be pure
waste.
"""
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.authorization import get_project_member
from app.core.exceptions import AuthenticationException, AuthorizationException, NotFoundException
from app.db.engine import get_db
from app.services.auth import get_user_by_id
from app.services.models import get_model

router = APIRouter(prefix="/internal", tags=["internal"])


async def _require_internal_service_key(
    x_internal_service_key: str | None = Header(default=None),
) -> None:
    # Fails closed: an unconfigured (empty) key never matches anything,
    # including an empty header — there is no "auth disabled" state.
    if not settings.INTERNAL_SERVICE_KEY or x_internal_service_key != settings.INTERNAL_SERVICE_KEY:
        raise HTTPException(status_code=401, detail="Invalid internal service key")


@router.get("/models/{model_id}/authorize")
async def authorize_model_access(
    model_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_internal_service_key),
) -> dict:
    """
    Returns {"authorized": true, "role": "...", "user": {"name": "..."}}
    or {"authorized": false}. Always 200 — authorization is a data field
    here, not an HTTP status, since the caller (ws-server) needs to
    distinguish "not authorized" from "authorization service unreachable"
    (network error / non-200), which it treats as fail-closed the same as
    an explicit denial.

    Reuses get_model() + get_project_member() — the identical functions
    GET /v1/models/{id}, /chunks, /tree etc. already use. No separate
    permission logic to keep in sync.
    """
    try:
        model = await get_model(model_id, db)
        user = await get_user_by_id(str(user_id), db)
        member = await get_project_member(model.project_id, user, db)
    except (NotFoundException, AuthorizationException, AuthenticationException):
        return {"authorized": False}

    return {
        "authorized": True,
        "role": member.role,
        "user": {"name": user.name},
    }
