import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.redis import delete_refresh_token, get_refresh_token_user, store_refresh_token
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse


async def register_user(data: RegisterRequest, db: AsyncSession) -> TokenResponse:
    """Register a new user and return tokens."""

    # Check email not already taken
    result = await db.execute(select(User).where(User.email == data.email))
    existing = result.scalar_one_or_none()
    if existing:
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail="Email already registered")

    # Create user
    user = User(
        id=uuid.uuid4(),
        email=data.email,
        password_hash=hash_password(data.password),
        full_name=data.full_name,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    # Issue tokens
    return await _issue_tokens(str(user.id))


async def login_user(data: LoginRequest, db: AsyncSession) -> TokenResponse:
    """Authenticate user and return tokens."""

    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar_one_or_none()

    # Use same error for wrong email or wrong password — prevents user enumeration
    if not user or not user.password_hash:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not verify_password(data.password, user.password_hash):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return await _issue_tokens(str(user.id))


async def refresh_access_token(refresh_token: str) -> TokenResponse:
    """Issue a new access token using a valid refresh token."""
    from fastapi import HTTPException
    from jose import JWTError

    # Verify JWT signature and expiry
    try:
        payload = decode_token(refresh_token)
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid token type")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    # Verify token exists in Redis (not revoked)
    stored_user_id = await get_refresh_token_user(refresh_token)
    if not stored_user_id or stored_user_id != user_id:
        raise HTTPException(status_code=401, detail="Refresh token revoked or expired")

    # Rotate refresh token — delete old, issue new
    await delete_refresh_token(refresh_token)
    return await _issue_tokens(user_id)


async def logout_user(refresh_token: str) -> None:
    """Revoke a refresh token."""
    await delete_refresh_token(refresh_token)


async def get_user_by_id(user_id: str, db: AsyncSession) -> User:
    """Fetch a user by UUID. Raises 404 if not found."""
    from fastapi import HTTPException

    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid token subject")

    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


# ── Internal helpers ─────────────────────────────────────────────────────────

async def _issue_tokens(user_id: str) -> TokenResponse:
    """Create access + refresh tokens and store refresh in Redis."""
    access_token = create_access_token(subject=user_id)
    refresh_token = create_refresh_token(subject=user_id)

    expire_seconds = int(timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS).total_seconds())
    await store_refresh_token(refresh_token, user_id, expire_seconds)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
    )