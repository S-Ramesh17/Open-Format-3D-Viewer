from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cookies import ACCESS_TOKEN_COOKIE
from app.core.security import decode_token
from app.db.engine import get_db
from app.models.user import User
from app.services.api_key import validate_api_key
from app.services.auth import get_user_by_id

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Resolve the authenticated user from, in priority order:
    1. Authorization: Bearer ofv_... (API key — header only, never cookie)
    2. Authorization: Bearer <jwt>   (explicit header — used by API clients/tools)
    3. access_token cookie           (browser sessions)
    Raises HTTP 401 if none are valid.
    """
    token: str | None = None
    is_api_key = False

    if credentials:
        token = credentials.credentials
        is_api_key = token.startswith("ofv_")

    if not token:
        token = request.cookies.get(ACCESS_TOKEN_COOKIE)

    if not token:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # ── API key path ─────────────────────────────────────────────────────────
    if is_api_key:
        user = await validate_api_key(token, db)
        if not user:
            raise HTTPException(
                status_code=401,
                detail="Invalid or revoked API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return user

    # ── JWT path (header or cookie) ─────────────────────────────────────────
    try:
        payload = decode_token(token)
    except JWTError:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if payload.get("type") != "access":
        raise HTTPException(
            status_code=401,
            detail="Invalid token type",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Invalid token subject",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return await get_user_by_id(user_id, db)