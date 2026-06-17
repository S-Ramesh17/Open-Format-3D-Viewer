import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.responses import envelope, envelope_model
from app.core.dependencies import get_current_user
from app.db.engine import get_db
from app.models.user import User
from app.schemas.auth import (
    CreateApiKeyRequest,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
)
from app.services.api_key import (
    create_api_key,
    list_api_keys,
    revoke_api_key,
)
from app.services.auth import (
    login_user,
    logout_user,
    refresh_access_token,
    register_user,
)
from app.services.oauth import get_google_authorization_url, handle_google_callback
from starlette.requests import Request

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=201)
async def register(
    data: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    result = await register_user(data, db)
    return envelope_model(result, status_code=201)


@router.post("/login")
async def login(
    data: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    result = await login_user(data, db)
    return envelope_model(result)


@router.post("/refresh")
async def refresh(data: RefreshRequest) -> JSONResponse:
    result = await refresh_access_token(data.refresh_token)
    return envelope_model(result)


@router.post("/logout", status_code=204)
async def logout(data: LogoutRequest) -> None:
    await logout_user(data.refresh_token)


@router.get("/me")
async def me(
    current_user: User = Depends(get_current_user),
) -> JSONResponse:
    from app.schemas.auth import UserResponse
    response = UserResponse.model_validate(current_user)
    return envelope_model(response)


@router.post("/keys", status_code=201)
async def create_key(
    data: CreateApiKeyRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    from app.schemas.auth import ApiKeyCreatedResponse
    raw_key, api_key = await create_api_key(data.name, current_user, db)
    response = ApiKeyCreatedResponse(
        key=raw_key,
        id=api_key.id,
        name=api_key.name,
        created_at=api_key.created_at,
    )
    return envelope_model(response, status_code=201)


@router.get("/keys")
async def get_keys(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    from app.schemas.auth import ApiKeyResponse
    keys = await list_api_keys(current_user, db)
    return envelope([ApiKeyResponse.model_validate(k).model_dump(mode="json") for k in keys])


@router.delete("/keys/{key_id}", status_code=204)
async def delete_key(
    key_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await revoke_api_key(key_id, current_user, db)


@router.get("/google")
async def google_login(request: Request):
    return await get_google_authorization_url(request)


@router.get("/google/callback")
async def google_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    result = await handle_google_callback(request, db)
    return envelope_model(result)