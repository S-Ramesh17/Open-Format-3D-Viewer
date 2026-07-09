import logging
import uuid

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.config import Config
from starlette.requests import Request

from app.config import settings
from app.models.user import User
from app.schemas.auth import TokenResponse
from app.services.auth import _issue_tokens

logger = logging.getLogger(__name__)

# ── OAuth client setup ───────────────────────────────────────────────────────
config = Config(environ={
    "GOOGLE_CLIENT_ID": settings.GOOGLE_CLIENT_ID,
    "GOOGLE_CLIENT_SECRET": settings.GOOGLE_CLIENT_SECRET,
})

oauth = OAuth(config)
oauth.register(
    name="google",
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


async def get_google_authorization_url(request: Request) -> RedirectResponse:
    """Redirect browser to Google OAuth2 consent screen."""
    redirect_uri = settings.GOOGLE_REDIRECT_URI
    return await oauth.google.authorize_redirect(request, redirect_uri)


async def handle_google_callback(
    request: Request,
    db: AsyncSession,
) -> tuple[TokenResponse, User]:
    """
    Handle Google OAuth2 callback.
    Exchange code for token, fetch profile, find or create user.
    Returns (TokenResponse, User) — router builds the redirect + cookies.
    Raises OAuthCallbackError on any failure so the router can redirect
    to an error page instead of returning JSON.
    """
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as exc:
        logger.error("Google OAuth token exchange failed: %s", exc)
        raise OAuthCallbackError(f"OAuth token exchange failed: {exc}")
    except Exception as exc:
        logger.error("Unexpected error during Google OAuth: %s", exc)
        raise OAuthCallbackError("OAuth failed unexpectedly")

    user_info = token.get("userinfo")
    if not user_info:
        raise OAuthCallbackError("Could not fetch user info from Google")

    google_sub = user_info.get("sub")
    email = user_info.get("email")
    full_name = user_info.get("name", "")

    if not google_sub or not email:
        raise OAuthCallbackError("Incomplete user info from Google")

    result = await db.execute(
        select(User).where(
            User.provider == "google",
            User.provider_user_id == google_sub,
        )
    )
    user = result.scalar_one_or_none()

    if not user:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

        if user:
            user.provider = "google"
            user.provider_user_id = google_sub
            # Invalidate the existing password on merge: once an account is
            # linked to Google, it must only be reachable via Google-verified
            # login. Leaving the old password_hash active would let anyone
            # who knows/guesses the original password bypass Google's
            # identity verification entirely.
            if user.password_hash is not None:
                logger.info(
                    "Invalidating existing password for user %s on Google account link",
                    user.id,
                )
                user.password_hash = None
            await db.commit()
            await db.refresh(user)
        else:
            user = User(
                id=uuid.uuid4(),
                email=email,
                full_name=full_name,
                provider="google",
                provider_user_id=google_sub,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)

    tokens = await _issue_tokens(str(user.id))
    return tokens, user


class OAuthCallbackError(Exception):
    """Raised when Google OAuth callback fails. Router must redirect, never return JSON."""
    pass