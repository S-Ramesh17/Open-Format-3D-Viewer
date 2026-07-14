from redis.asyncio import Redis
from app.config import settings

import json as _json

_redis_client: Redis | None = None


async def get_redis() -> Redis:
    """Return the shared async Redis client. Creates it on first call."""
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
    return _redis_client


async def close_redis() -> None:
    """Close the Redis connection. Called on application shutdown."""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


# ── Refresh token operations ─────────────────────────────────────────────────

REFRESH_TOKEN_PREFIX = "refresh:"


async def store_refresh_token(token: str, user_id: str, expire_seconds: int) -> None:
    """Store a refresh token in Redis with TTL."""
    redis = await get_redis()
    key = f"{REFRESH_TOKEN_PREFIX}{token}"
    await redis.set(key, user_id, ex=expire_seconds)


async def get_refresh_token_user(token: str) -> str | None:
    """
    Return the user_id associated with a refresh token.
    Returns None if token does not exist or has expired.
    """
    redis = await get_redis()
    key = f"{REFRESH_TOKEN_PREFIX}{token}"
    return await redis.get(key)


async def consume_refresh_token(token: str) -> str | None:
    """Atomically get and delete a refresh token to prevent race conditions."""
    redis = await get_redis()
    key = f"{REFRESH_TOKEN_PREFIX}{token}"
    return await redis.execute_command("GETDEL", key)

async def delete_refresh_token(token: str) -> None:
    """Delete a refresh token. Called on logout."""
    redis = await get_redis()
    key = f"{REFRESH_TOKEN_PREFIX}{token}"
    await redis.delete(key)


async def delete_all_user_refresh_tokens(user_id: str) -> None:
    """
    Delete ALL refresh tokens for a user.
    Used when password changes or account is compromised.
    Scans for all refresh: keys and filters by value.
    """
    redis = await get_redis()
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match=f"{REFRESH_TOKEN_PREFIX}*", count=100)
        for key in keys:
            val = await redis.get(key)
            if val == user_id:
                await redis.delete(key)
        if cursor == 0:
            break




async def publish_model_event(user_id: str, event: str, data: dict) -> None:
    """
    Publish a real-time event to a user's WebSocket channel.
    Consumed by apps/ws-server's per-connection Redis subscriber.

    Failures are logged but never propagated — a Redis publish error must
    not cause the calling HTTP request (e.g. confirm_upload) to fail.
    """
    try:
        redis = await get_redis()
        channel = f"model_events:{user_id}"
        payload = _json.dumps({"event": event, "data": data})
        await redis.publish(channel, payload)
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).error(
            "publish_model_event failed user_id=%s event=%s: %s",
            user_id, event, exc,
        )