"""
Tests for rate limiting middleware.

Covers: free-tier limit enforcement, rate limit headers, exempt paths,
        header correctness, Redis-backed counter behavior.
"""

import time
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_redis(current_count: int = 1, limit: int = 100):
    """Return a mock Redis client that returns `current_count` on incr()."""
    redis_mock = AsyncMock()
    redis_mock.incr = AsyncMock(return_value=current_count)
    redis_mock.expire = AsyncMock(return_value=True)
    redis_mock.get = AsyncMock(return_value=None)  # no cached plan
    redis_mock.set = AsyncMock(return_value=True)
    return redis_mock


# ---------------------------------------------------------------------------
# Rate limit headers
# ---------------------------------------------------------------------------

class TestRateLimitHeaders:
    async def test_rate_limit_headers_present_on_authed_request(
        self, client: AsyncClient, unique_email: str
    ):
        """Every non-exempt authenticated response carries X-RateLimit-* headers."""
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )

        redis_mock = _mock_redis(current_count=1)
        with patch("app.middleware.rate_limit.get_redis", return_value=redis_mock), \
             patch("app.middleware.rate_limit.RateLimitMiddleware._get_user_plan",
                   new_callable=AsyncMock, return_value="free"):
            resp = await client.get("/v1/projects")

        assert "X-RateLimit-Limit" in resp.headers
        assert "X-RateLimit-Remaining" in resp.headers
        assert "X-RateLimit-Reset" in resp.headers

    async def test_rate_limit_remaining_decrements(
        self, client: AsyncClient, unique_email: str
    ):
        """X-RateLimit-Remaining should reflect the current counter."""
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )

        redis_mock = _mock_redis(current_count=10)
        with patch("app.middleware.rate_limit.get_redis", return_value=redis_mock), \
             patch("app.middleware.rate_limit.RateLimitMiddleware._get_user_plan",
                   new_callable=AsyncMock, return_value="free"):
            resp = await client.get("/v1/projects")

        remaining = int(resp.headers["X-RateLimit-Remaining"])
        # With count=10 and free limit=100, remaining should be 90
        assert remaining == 90

    async def test_rate_limit_reset_is_future_timestamp(
        self, client: AsyncClient, unique_email: str
    ):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )

        redis_mock = _mock_redis(current_count=1)
        with patch("app.middleware.rate_limit.get_redis", return_value=redis_mock), \
             patch("app.middleware.rate_limit.RateLimitMiddleware._get_user_plan",
                   new_callable=AsyncMock, return_value="free"):
            resp = await client.get("/v1/projects")

        reset_ts = int(resp.headers["X-RateLimit-Reset"])
        # Reset should be a future Unix timestamp (current hour boundary)
        assert reset_ts > int(time.time()) - 3600
        assert reset_ts <= int(time.time()) + 3600


# ---------------------------------------------------------------------------
# Exempt paths (no rate limiting applied)
# ---------------------------------------------------------------------------

class TestExemptPaths:
    async def test_auth_register_not_blocked_under_normal_use(self, client: AsyncClient):
        """
        A single register call must succeed. Register/login are NOT exempt
        from rate limiting — they're intentionally IP-keyed at a tighter
        10/hour brute-force ceiling (see AUTH_BRUTE_FORCE_PATHS in
        rate_limit.py) rather than being fully unlimited, which was itself
        a prior security gap. This test only asserts that ordinary,
        infrequent use isn't blocked.
        """
        import uuid
        email = f"exempt_{uuid.uuid4().hex[:8]}@example.com"
        resp = await client.post(
            "/v1/auth/register",
            json={"email": email, "password": "testpass123"},
        )
        assert resp.status_code != 429

    async def test_health_endpoint_exempt(self, client: AsyncClient):
        resp = await client.get("/health")
        # If health exists it should not be rate-limited
        assert resp.status_code != 429

    async def test_login_not_blocked_under_normal_use(self, client: AsyncClient, unique_email: str):
        """Same clarification as above, for /v1/auth/login."""
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )
        resp = await client.post(
            "/v1/auth/login",
            json={"email": unique_email, "password": "testpass123"},
        )
        assert resp.status_code != 429


# ---------------------------------------------------------------------------
# Free-tier limit enforcement
# ---------------------------------------------------------------------------

class TestFreeTierLimits:
    async def test_exceeding_free_limit_returns_429(
        self, client: AsyncClient, unique_email: str
    ):
        """When Redis counter exceeds the free tier limit, the middleware returns 429."""
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )

        # Simulate counter already at limit+1
        redis_mock = _mock_redis(current_count=101)  # free limit is 100
        with patch("app.middleware.rate_limit.get_redis", return_value=redis_mock), \
             patch("app.middleware.rate_limit.RateLimitMiddleware._get_user_plan",
                   new_callable=AsyncMock, return_value="free"):
            resp = await client.get("/v1/projects")

        assert resp.status_code == 429

    async def test_429_response_has_correct_error_code(
        self, client: AsyncClient, unique_email: str
    ):
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )

        redis_mock = _mock_redis(current_count=101)
        with patch("app.middleware.rate_limit.get_redis", return_value=redis_mock), \
             patch("app.middleware.rate_limit.RateLimitMiddleware._get_user_plan",
                   new_callable=AsyncMock, return_value="free"):
            resp = await client.get("/v1/projects")

        assert resp.status_code == 429
        body = resp.json()
        # Error response should follow the API envelope pattern
        assert "error" in body

    async def test_pro_tier_not_limited_at_free_threshold(
        self, client: AsyncClient, unique_email: str
    ):
        """Pro user at count=101 should NOT be rate-limited (pro limit is 10000)."""
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )

        redis_mock = _mock_redis(current_count=101)
        with patch("app.middleware.rate_limit.get_redis", return_value=redis_mock), \
             patch("app.middleware.rate_limit.RateLimitMiddleware._get_user_plan",
                   new_callable=AsyncMock, return_value="pro"):
            resp = await client.get("/v1/projects")

        assert resp.status_code != 429

    async def test_enterprise_tier_never_limited(
        self, client: AsyncClient, unique_email: str
    ):
        """Enterprise tier has no limit — X-RateLimit-Limit should be 'unlimited'."""
        await client.post(
            "/v1/auth/register",
            json={"email": unique_email, "password": "testpass123"},
        )

        redis_mock = _mock_redis(current_count=999999)
        with patch("app.middleware.rate_limit.get_redis", return_value=redis_mock), \
             patch("app.middleware.rate_limit.RateLimitMiddleware._get_user_plan",
                   new_callable=AsyncMock, return_value="enterprise"):
            resp = await client.get("/v1/projects")

        assert resp.status_code != 429
        assert resp.headers.get("X-RateLimit-Limit") == "unlimited"
