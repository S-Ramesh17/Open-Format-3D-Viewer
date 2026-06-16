from fastapi import Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.db.engine import get_db
from app.models.user import User
from app.services.auth import get_user_by_id
from app.services.api_key import validate_api_key

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Accepts either:
    - JWT access token: Authorization: Bearer eyJ...
    - API key:          Authorization: Bearer ofv_...
    Raises HTTP 401 if neither is valid.
    """
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Authorization header missing",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # ── Try API key first (ofv_ prefix) ─────────────────────────────────────
    if token.startswith("ofv_"):
        user = await validate_api_key(token, db)
        if not user:
            raise HTTPException(
                status_code=401,
                detail="Invalid or revoked API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return user

    # ── Try JWT ──────────────────────────────────────────────────────────────
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