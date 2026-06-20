import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import settings
from app.core.exceptions import RateLimitException
from app.core.redis import get_redis

# Routes that do not require auth — skip rate limiting
EXEMPT_PATHS = {
    "/health",
    "/v1/auth/register",
    "/v1/auth/login",
    "/v1/auth/refresh",
    "/v1/auth/google",
    "/v1/auth/google/callback",
    "/docs",
    "/openapi.json",
    "/redoc",
}

PLAN_LIMITS = {
    "free": settings.RATE_LIMIT_FREE_PER_HOUR,
    "pro": settings.RATE_LIMIT_PRO_PER_HOUR,
    "enterprise": None,  # unlimited
}


def _get_hour_bucket() -> int:
    """Return the Unix timestamp of the start of the current hour."""
    return int(time.time() // 3600) * 3600


def _rate_limit_key(user_id: str, hour_bucket: int) -> str:
    return f"ratelimit:{user_id}:{hour_bucket}"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Redis-backed per-user rate limiting middleware.

    - Exempt paths bypass rate limiting entirely.
    - Authenticated requests are limited by user plan.
    - Unauthenticated requests to non-exempt paths are limited by IP.
    - Rate limit headers are added to every non-exempt response.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # Skip exempt paths
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        # Extract user identity from request state (set by auth dependency)
        # For middleware-level rate limiting we use JWT sub if available
        user_id, plan = await self._identify_request(request)
        limit = PLAN_LIMITS.get(plan, settings.RATE_LIMIT_FREE_PER_HOUR)

        # Enterprise = unlimited
        if limit is None:
            response = await call_next(request)
            response.headers["X-RateLimit-Limit"] = "unlimited"
            response.headers["X-RateLimit-Remaining"] = "unlimited"
            response.headers["X-RateLimit-Reset"] = "0"
            return response

        hour_bucket = _get_hour_bucket()
        redis_key = _rate_limit_key(user_id, hour_bucket)
        reset_time = hour_bucket + 3600

        redis = await get_redis()

        # Atomic increment
        count = await redis.incr(redis_key)

        # Set TTL on first request of this window
        if count == 1:
            await redis.expire(redis_key, 3600)

        remaining = max(0, limit - count)

        # Reject if over limit
        if count > limit:
            raise RateLimitException(
                message=f"Rate limit exceeded. Limit: {limit}/hour.",
                details={
                    "limit": limit,
                    "remaining": 0,
                    "reset": reset_time,
                },
            )

        response = await call_next(request)

        # Add rate limit headers to every response
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_time)

        return response

    async def _identify_request(self, request: Request) -> tuple[str, str]:
        """
        Extract user_id and plan from, in priority order:
        1. Authorization: Bearer ofv_...  (API key)
        2. Authorization: Bearer <jwt>    (explicit header)
        3. access_token cookie            (browser sessions)
        Falls back to IP address for unauthenticated requests.
        Returns (identifier, plan).
        """
        from jose import JWTError

        from app.core.cookies import ACCESS_TOKEN_COOKIE
        from app.core.security import decode_token

        auth_header = request.headers.get("Authorization", "")
        token: str | None = None

        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

            # API key path — identify by key prefix hash
            if token.startswith("ofv_"):
                import hashlib
                key_id = hashlib.sha256(token.encode()).hexdigest()[:16]
                return f"apikey:{key_id}", "free"
        else:
            token = request.cookies.get(ACCESS_TOKEN_COOKIE)

        if token:
            try:
                payload = decode_token(token)
                user_id = payload.get("sub", "")
                if user_id:
                    plan = await self._get_user_plan(user_id)
                    return f"user:{user_id}", plan
            except JWTError:
                pass

        # Unauthenticated — rate limit by IP
        client_ip = request.client.host if request.client else "unknown"
        return f"ip:{client_ip}", "free"

    async def _get_user_plan(self, user_id: str) -> str:
        """
        Fetch user plan from DB.
        Uses a short Redis cache to avoid DB hit on every request.
        """
        redis = await get_redis()
        cache_key = f"plan:{user_id}"

        cached = await redis.get(cache_key)
        if cached:
            return cached

        # Load from DB
        try:
            import uuid
            from sqlalchemy import select
            from app.db.engine import AsyncSessionLocal
            from app.models.user import User

            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(User.plan).where(User.id == uuid.UUID(user_id))
                )
                plan = result.scalar_one_or_none() or "free"

            # Cache for 5 minutes
            await redis.set(cache_key, plan, ex=300)
            return plan
        except Exception:
            return "free"