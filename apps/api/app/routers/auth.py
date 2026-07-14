import logging
import uuid

from fastapi import APIRouter, Depends, Request, Response
from app.core.profiling import profile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.responses import Envelope, envelope, envelope_model
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@profile("auth_register")
@router.post(
    "/register",
    status_code=201,
    summary="Register a new account",
    responses={201: {"model": Envelope[AuthResponse]}},
)
async def register(
    data: RegisterRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a new user account and return an access/refresh token pair.
    Tokens are also set as httpOnly cookies."""
    tokens, user = await register_user(data, db)
    logger.info("auth.register.success", extra={"user_id": str(user.id)})
    auth_response = AuthResponse(
        user=UserResponse.model_validate(user),
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
    )
    envelope_response = envelope_model(auth_response, status_code=201)
    set_auth_cookies(envelope_response, tokens.access_token, tokens.refresh_token)
    return envelope_response


@profile("auth_login")
@router.post(
    "/login",
    summary="Log in with email and password",
    responses={200: {"model": Envelope[AuthResponse]}},
)
async def login(
    data: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Authenticate with email/password and return an access/refresh token
    pair. Tokens are also set as httpOnly cookies."""
    try:
        tokens, user = await login_user(data, db)
    except AuthenticationException:
        # Never log the submitted password or which check failed in detail
        # (user-not-found vs wrong-password) — only that an attempt failed,
        # to avoid both credential leakage and user enumeration via logs.
        logger.info("auth.login.failure", extra={"email": data.email})
        raise
    logger.info("auth.login.success", extra={"user_id": str(user.id)})
    auth_response = AuthResponse(
        user=UserResponse.model_validate(user),
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
    )
    envelope_response = envelope_model(auth_response)
    set_auth_cookies(envelope_response, tokens.access_token, tokens.refresh_token)
    return envelope_response


@router.post(
    "/refresh",
    summary="Rotate an access/refresh token pair",
    responses={200: {"model": Envelope[AuthResponse]}},
)
async def refresh(
    request: Request,
    data: RefreshRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    Exchange a valid refresh token for a new access/refresh token pair.
    The old refresh token is revoked as part of rotation.

    PRD 5.3 specifies a body {refresh_token} for API clients; browser
    clients rely on the httpOnly cookie instead. Prefer the body when
    present, fall back to the cookie.
    """
    refresh_token = (data.refresh_token if data else None) or request.cookies.get(
        REFRESH_TOKEN_COOKIE
    )
    if not refresh_token:
        raise AuthenticationException("Refresh token missing")

    tokens, user = await refresh_access_token(refresh_token, db)
    logger.info("auth.refresh.rotated", extra={"user_id": str(user.id)})
    auth_response = AuthResponse(
        user=UserResponse.model_validate(user),
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
    )
    envelope_response = envelope_model(auth_response)
    set_auth_cookies(envelope_response, tokens.access_token, tokens.refresh_token)
    return envelope_response


@router.post(
    "/logout",
    status_code=204,
    summary="Log out and revoke the refresh token",
    responses={204: {"description": "Logged out"}},
)
async def logout(request: Request, data: LogoutRequest | None = None) -> Response:
    """Revoke the caller's refresh token (server-side) and clear auth cookies."""
    refresh_token = (data.refresh_token if data else None) or request.cookies.get(
        REFRESH_TOKEN_COOKIE
    )
    if refresh_token:
        await logout_user(refresh_token)
        logger.info("auth.logout.success")

    response = Response(status_code=204)
    clear_auth_cookies(response)
    return response


@router.get(
    "/me",
    summary="Get the current authenticated user",
    responses={200: {"model": Envelope[UserResponse]}},
)
async def me(
    current_user: User = Depends(get_current_user),
) -> JSONResponse:
    """Return the profile of the currently authenticated user."""
    response = UserResponse.model_validate(current_user)
    return envelope_model(response)


@router.post(
    "/keys",
    status_code=201,
    summary="Create an API key",
    responses={201: {"model": Envelope[ApiKeyCreatedResponse]}},
)
async def create_key(
    data: CreateApiKeyRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    Create a new API key for the current user. The raw key is returned
    exactly once here — only its hash is stored, so it cannot be
    retrieved again after this response.
    """
    raw_key, api_key = await create_api_key(data.name, current_user, db)
    logger.info(
        "auth.api_key.created",
        extra={"user_id": str(current_user.id), "api_key_id": str(api_key.id)},
    )
    response = ApiKeyCreatedResponse(
        key=raw_key,
        id=api_key.id,
        name=api_key.name,
        created_at=api_key.created_at,
    )
    return envelope_model(response, status_code=201)


@router.get(
    "/keys",
    summary="List the current user's API keys",
    responses={200: {"model": Envelope[list[ApiKeyResponse]]}},
)
async def get_keys(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List API keys owned by the current user (hashes/secrets never included)."""
    keys = await list_api_keys(current_user, db)
    return envelope([ApiKeyResponse.model_validate(k).model_dump(mode="json") for k in keys])


@router.delete(
    "/keys/{key_id}",
    status_code=204,
    summary="Revoke an API key",
    responses={204: {"description": "API key revoked"}},
)
async def delete_key(
    key_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke an API key. Idempotent 404 if already revoked/missing."""
    await revoke_api_key(key_id, current_user, db)
    logger.info(
        "auth.api_key.revoked",
        extra={"user_id": str(current_user.id), "api_key_id": str(key_id)},
    )


@router.get("/google", response_class=RedirectResponse, summary="Start Google OAuth2 login")
async def google_login(request: Request) -> RedirectResponse:
    """Initiate Google OAuth2 flow — always redirects to Google."""
    return await get_google_authorization_url(request)


@router.get(
    "/google/callback",
    response_class=RedirectResponse,
    summary="Google OAuth2 callback",
)
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
        logger.warning("auth.oauth.google.failure")
        return RedirectResponse(
            url=f"{settings.FRONTEND_URL}/login?error=oauth_failed",
            status_code=302,
        )

    logger.info("auth.oauth.google.success", extra={"user_id": str(_user.id)})
    redirect = RedirectResponse(
        url=f"{settings.FRONTEND_URL}/dashboard",
        status_code=302,
    )
    set_auth_cookies(redirect, tokens.access_token, tokens.refresh_token)
    return redirect
