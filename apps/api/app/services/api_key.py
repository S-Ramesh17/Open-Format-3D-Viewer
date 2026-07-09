import hashlib
import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_key import ApiKey
from app.models.user import User

API_KEY_PREFIX = "ofv_"
API_KEY_BYTES = 32  # 32 random bytes = 64 hex chars after prefix


def _generate_raw_key() -> str:
    """Generate a cryptographically secure random API key with prefix."""
    random_part = secrets.token_hex(API_KEY_BYTES)
    return f"{API_KEY_PREFIX}{random_part}"


def _hash_key(raw_key: str) -> str:
    """SHA-256 hash of the full key including prefix."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def create_api_key(
    name: str,
    user: User,
    db: AsyncSession,
) -> tuple[str, ApiKey]:
    """
    Generate a new API key.
    Returns (plaintext_key, ApiKey record).
    The plaintext key is returned ONCE and never stored.
    """
    raw_key = _generate_raw_key()
    key_hash = _hash_key(raw_key)

    api_key = ApiKey(
        id=uuid.uuid4(),
        user_id=user.id,
        name=name,
        key_hash=key_hash,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    return raw_key, api_key


async def validate_api_key(raw_key: str, db: AsyncSession) -> User | None:
    """
    Validate an incoming API key.
    Returns the User if valid and not revoked, None otherwise.
    Uses secrets.compare_digest() to prevent timing attacks.
    """
    if not raw_key.startswith(API_KEY_PREFIX):
        return None

    incoming_hash = _hash_key(raw_key)

    # Indexed lookup on key_hash (unique + indexed column) instead of a
    # full-table scan. key_hash is a SHA-256 digest of a 256-bit random
    # secret, so an equality lookup on the digest does not reintroduce a
    # timing side-channel on the underlying key material.
    result = await db.execute(
        select(ApiKey).where(
            ApiKey.key_hash == incoming_hash,
            ApiKey.revoked_at.is_(None),
        )
    )
    matched_key = result.scalar_one_or_none()

    if not matched_key:
        return None

    # Load the user
    result = await db.execute(
        select(User).where(User.id == matched_key.user_id)
    )
    return result.scalar_one_or_none()


async def revoke_api_key(
    key_id: uuid.UUID,
    user: User,
    db: AsyncSession,
) -> bool:
    """
    Revoke an API key by ID.
    Returns True if revoked, False if not found or not owned by user.
    """
    result = await db.execute(
        select(ApiKey).where(
            ApiKey.id == key_id,
            ApiKey.user_id == user.id,
            ApiKey.revoked_at.is_(None),
        )
    )
    api_key = result.scalar_one_or_none()

    if not api_key:
        return False

    api_key.revoked_at = datetime.now(timezone.utc)
    await db.commit()
    return True


async def list_api_keys(user: User, db: AsyncSession) -> list[ApiKey]:
    """List all active API keys for a user."""
    result = await db.execute(
        select(ApiKey).where(
            ApiKey.user_id == user.id,
            ApiKey.revoked_at.is_(None),
        )
    )
    return list(result.scalars().all())