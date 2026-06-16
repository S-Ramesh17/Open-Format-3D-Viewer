import uuid
from starlette.requests import Request
from app.services.oauth import get_google_authorization_url, handle_google_callback

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user
from app.db.engine import get_db
from app.models.user import User
from app.schemas.auth import (
    ApiKeyCreatedResponse,
    ApiKeyResponse,
    CreateApiKeyRequest,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.services.auth import (
    login_user,
    logout_user,
    refresh_access_token,
    register_user,
)
from app.services.api_key import (
    create_api_key,
    list_api_keys,
    revoke_api_key,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(
    data: RegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    return await register_user(data, db)


@router.post("/login", response_model=TokenResponse)
async def login(
    data: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    return await login_user(data, db)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(data: RefreshRequest):
    return await refresh_access_token(data.refresh_token)


@router.post("/logout", status_code=204)
async def logout(data: LogoutRequest):
    await logout_user(data.refresh_token)


@router.get("/me", response_model=UserResponse)
async def me(
    current_user: User = Depends(get_current_user),
):
    return current_user


@router.post("/keys", response_model=ApiKeyCreatedResponse, status_code=201)
async def create_key(
    data: CreateApiKeyRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    raw_key, api_key = await create_api_key(data.name, current_user, db)
    return ApiKeyCreatedResponse(
        key=raw_key,
        id=api_key.id,
        name=api_key.name,
        created_at=api_key.created_at,
    )


@router.get("/keys", response_model=list[ApiKeyResponse])
async def get_keys(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await list_api_keys(current_user, db)


@router.delete("/keys/{key_id}", status_code=204)
async def delete_key(
    key_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await revoke_api_key(key_id, current_user, db)

@router.get("/google")
async def google_login(request: Request):
    return await get_google_authorization_url(request)


@router.get("/google/callback", response_model=TokenResponse)
async def google_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    return await handle_google_callback(request, db)