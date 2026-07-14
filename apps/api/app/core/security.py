from datetime import datetime, timedelta, timezone
from typing import Any
import uuid as _uuid

from jose import jwt
from passlib.context import CryptContext

from app.config import settings

# ── Password hashing ────────────────────────────────────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """Hash a plaintext password using bcrypt."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    return pwd_context.verify(plain_password, hashed_password)


# ── JWT ─────────────────────────────────────────────────────────────────────
ALGORITHM = "HS256"


def create_access_token(subject: str, extra_claims: dict[str, Any] | None = None) -> str:
    """
    Create a signed JWT access token.
    subject = user UUID as string.
    Expires in ACCESS_TOKEN_EXPIRE_MINUTES.
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": expire,
        "type": "access",
    }
    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(subject: str) -> str:
    """
    Create a signed JWT refresh token.
    Expires in REFRESH_TOKEN_EXPIRE_DAYS.

    A ``jti`` (JWT ID, RFC 7519 §4.1.7) claim containing a random UUID4 is
    included to guarantee that every issued token is unique — even when two
    tokens are issued for the same user within the same wall-clock second.
    Without ``jti``, ``iat`` and ``exp`` are integer seconds and two calls
    made within the same second produce identical JWT strings.  The rotation
    logic in ``refresh_access_token()`` deletes the old token from Redis and
    then stores the new one; if both strings are identical the store() call
    silently re-creates the key that delete() just removed, making the old
    token reusable and defeating token rotation entirely.
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": expire,
        "type": "refresh",
        "jti": str(_uuid.uuid4()),   # unique ID — prevents same-second collision
    }

    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    """
    Decode and verify a JWT token.
    Raises JWTError if invalid or expired.
    """
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])