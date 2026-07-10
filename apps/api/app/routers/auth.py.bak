import uuid

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.responses import envelope, envelope_model
from app.config import settings
from app.core.cookies import (
    REFRESH_TOKEN_COOKIE,
    clear_auth_cookies,
    set_auth_cookies,
)
from app.core.dependencies import get_current_user
from app.core.exceptions import AuthenticationException
from app.db.engine import get_db
from app.models.user import User
from app.schemas.auth import (
    ApiKeyCreatedResponse,
    ApiKeyResponse,
    AuthResponse,
    CreateApiKeyRequest,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    UserResponse,
)
from app.services.api_key import create_api_key, list_api_keys, revoke_api_key
from app.services.auth import (
    login_user,
    logout_user,
    refresh_access_token,
    register_user,
)
from app.services.oauth import get_google_authorization_url, handle_google_callback

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.post("/register", status_code=201)
async def register(
    data: RegisterRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    tokens, user = await register_user(data, db)
    auth_response = AuthResponse(
        user=UserResponse.model_validate(user),
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
    )
    envelope_response = envelope_model(auth_response, status_code=201)
    set_auth_cookies(envelope_response, tokens.access_token, tokens.refresh_token)
    return envelope_response


@router.post("/login")
async def login(
    data: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    tokens, user = await login_user(data, db)
    auth_response = AuthResponse(
        user=UserResponse.model_validate(user),
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
    )
    envelope_response = envelope_model(auth_response)
    set_auth_cookies(envelope_response, tokens.access_token, tokens.refresh_token)
    return envelope_response


@router.post("/refresh")
async def refresh(
    request: Request,
    data: RefreshRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    # PRD 5.3 specifies a body {refresh_token} for API clients; browser
    # clients rely on the httpOnly cookie instead. Prefer the body when
    # present, fall back to the cookie.
    refresh_token = (data.refresh_token if data else None) or request.cookies.get(
        REFRESH_TOKEN_COOKIE
    )
    if not refresh_token:
        raise AuthenticationException("Refresh token missing")

    tokens, user = await refresh_access_token(refresh_token, db)
    auth_response = AuthResponse(
        user=UserResponse.model_validate(user),
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
    )
    envelope_response = envelope_model(auth_response)
    set_auth_cookies(envelope_response, tokens.access_token, tokens.refresh_token)
    return envelope_response


@router.post("/logout", status_code=204)
async def logout(request: Request, data: LogoutRequest | None = None) -> Response:
    refresh_token = (data.refresh_token if data else None) or request.cookies.get(
        REFRESH_TOKEN_COOKIE
    )
    if refresh_token:
        await logout_user(refresh_token)

    response = Response(status_code=204)
    clear_auth_cookies(response)
    return response


@router.get("/me")
async def me(
    current_user: User = Depends(get_current_user),
) -> JSONResponse:
    response = UserResponse.model_validate(current_user)
    return envelope_model(response)


@router.post("/keys", status_code=201)
async def create_key(
    data: CreateApiKeyRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
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
    keys = await list_api_keys(current_user, db)
    return envelope([ApiKeyResponse.model_validate(k).model_dump(mode="json") for k in keys])


@router.delete("/keys/{key_id}", status_code=204)
async def delete_key(
    key_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await revoke_api_key(key_id, current_user, db)


@router.get("/google", response_class=RedirectResponse)
async def google_login(request: Request) -> RedirectResponse:
    """Initiate Google OAuth2 flow — always redirects to Google."""
    return await get_google_authorization_url(request)


@router.get("/google/callback", response_class=RedirectResponse)
async def google_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """
    Google OAuth2 callback — ALWAYS returns a RedirectResponse, never JSON.
    On success: redirect to /dashboard with cookies set.
    On failure: redirect to /login?error=oauth_failed.
    """
    try:
        tokens, _user = await handle_google_callback(request, db)
    except Exception:
        # Catch everything — OAuthCallbackError, DB errors, anything.
        # Never let the global JSON error handler intercept this route.
        return RedirectResponse(
            url=f"{settings.FRONTEND_URL}/login?error=oauth_failed",
            status_code=302,
        )

    redirect = RedirectResponse(
        url=f"{settings.FRONTEND_URL}/dashboard",
        status_code=302,
    )
    set_auth_cookies(redirect, tokens.access_token, tokens.refresh_token)
    return redirect