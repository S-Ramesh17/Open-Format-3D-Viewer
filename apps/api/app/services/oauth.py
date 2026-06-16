import uuid

from authlib.integrations.starlette_client import OAuth
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.config import Config
from starlette.requests import Request

from app.config import settings
from app.models.user import User
from app.schemas.auth import TokenResponse
from app.services.auth import _issue_tokens

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


async def get_google_authorization_url(request: Request) -> str:
    """Generate Google OAuth2 authorization URL and return it."""
    redirect_uri = settings.GOOGLE_REDIRECT_URI
    return await oauth.google.authorize_redirect(request, redirect_uri)


async def handle_google_callback(
    request: Request,
    db: AsyncSession,
) -> TokenResponse:
    """
    Handle Google OAuth2 callback.
    Exchange code for token, fetch profile, find or create user.
    """
    from fastapi import HTTPException

    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception:
        raise HTTPException(status_code=400, detail="Google OAuth failed")

    user_info = token.get("userinfo")
    if not user_info:
        raise HTTPException(status_code=400, detail="Could not fetch user info from Google")

    google_sub = user_info.get("sub")
    email = user_info.get("email")
    full_name = user_info.get("name")

    if not google_sub or not email:
        raise HTTPException(status_code=400, detail="Incomplete user info from Google")

    # Find existing user by provider_user_id
    result = await db.execute(
        select(User).where(
            User.provider == "google",
            User.provider_user_id == google_sub,
        )
    )
    user = result.scalar_one_or_none()

    if not user:
        # Check if email already registered via password
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

        if user:
            # Link Google to existing account
            user.provider = "google"
            user.provider_user_id = google_sub
            await db.commit()
            await db.refresh(user)
        else:
            # Create new user
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

    return await _issue_tokens(str(user.id))