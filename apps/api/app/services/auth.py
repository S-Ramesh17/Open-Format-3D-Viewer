import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import (
    AuthenticationException,
    ConflictException,
    NotFoundException,
)
from app.core.redis import (
    delete_refresh_token,
    get_refresh_token_user,
    store_refresh_token,
)
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse


async def register_user(
    data: RegisterRequest, db: AsyncSession
) -> tuple[TokenResponse, User]:
    result = await db.execute(select(User).where(User.email == data.email))
    if result.scalar_one_or_none():
        raise ConflictException("Email already registered")

    user = User(
        id=uuid.uuid4(),
        email=data.email,
        password_hash=hash_password(data.password),
        full_name=data.full_name,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    tokens = await _issue_tokens(str(user.id))
    return tokens, user


async def login_user(
    data: LoginRequest, db: AsyncSession
) -> tuple[TokenResponse, User]:
    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar_one_or_none()

    if not user or not user.password_hash:
        raise AuthenticationException("Invalid credentials")

    if not verify_password(data.password, user.password_hash):
        raise AuthenticationException("Invalid credentials")

    tokens = await _issue_tokens(str(user.id))
    return tokens, user


async def refresh_access_token(
    refresh_token: str, db: AsyncSession
) -> tuple[TokenResponse, User]:
    from jose import JWTError

    try:
        payload = decode_token(refresh_token)
    except JWTError:
        raise AuthenticationException("Invalid refresh token")

    if payload.get("type") != "refresh":
        raise AuthenticationException("Invalid token type")

    user_id = payload.get("sub")
    if not user_id:
        raise AuthenticationException("Invalid refresh token")

    stored_user_id = await get_refresh_token_user(refresh_token)
    if not stored_user_id or stored_user_id != user_id:
        raise AuthenticationException("Refresh token revoked or expired")

    await delete_refresh_token(refresh_token)
    tokens = await _issue_tokens(user_id)
    user = await get_user_by_id(user_id, db)
    return tokens, user


async def logout_user(refresh_token: str) -> None:
    await delete_refresh_token(refresh_token)


async def get_user_by_id(user_id: str, db: AsyncSession) -> User:
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise AuthenticationException("Invalid token subject")

    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if not user:
        raise NotFoundException("User not found")
    return user


async def _issue_tokens(user_id: str) -> TokenResponse:
    access_token = create_access_token(subject=user_id)
    refresh_token = create_refresh_token(subject=user_id)

    expire_seconds = int(
        timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS).total_seconds()
    )
    await store_refresh_token(refresh_token, user_id, expire_seconds)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
    )