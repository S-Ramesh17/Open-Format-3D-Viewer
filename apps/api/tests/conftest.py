import asyncio
from collections.abc import AsyncGenerator
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import AsyncSessionLocal
from app.main import app


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Provides a DB session for tests that need direct DB access.
    Reuses the existing AsyncSessionLocal — assumes tests run against
    the same dev database configured in .env. No test-DB isolation
    is implemented here; tests that create rows should clean up after
    themselves or use unique identifiers (see unique_email fixture).
    """
    async with AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """
    httpx AsyncClient wired directly to the FastAPI ASGI app —
    no real network/socket needed. Reuses the app's actual middleware
    stack (CORS, rate limiting, auth, security headers) exactly as
    deployed, so integration tests exercise the real request pipeline.

    base_url MUST be https:// — auth cookies are set with Secure=True
    (required for SameSite=None cross-site cookies in production).
    httpx's cookie jar enforces the Secure attribute exactly like a
    browser: it will not store or resend Secure cookies over http://,
    so an http://testserver base_url silently breaks cookie persistence
    between requests in tests, even though the server-side Set-Cookie
    logic is correct.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as ac:
        yield ac

@pytest.fixture
def unique_email() -> str:
    """Generates a unique email per test run to avoid 409 CONFLICT collisions."""
    return f"test_{uuid.uuid4().hex[:12]}@example.com"


@pytest_asyncio.fixture(autouse=True)
async def _reset_rate_limits() -> AsyncGenerator[None, None]:
    """
    Flush rate-limiter Redis keys before every test.

    httpx's ASGITransport gives every request the same synthetic client
    host, so the IP-keyed auth brute-force limiter (10 req/hour on
    /v1/auth/register and /v1/auth/login — see rate_limit.py) shares a
    single bucket across the *entire* test session. Without a reset,
    roughly the 11th test that registers a user gets 429, and every test
    after that cascades into 401s and empty-envelope KeyErrors. This does
    not weaken the production limiter in any way — it only clears the
    test process's own Redis state so each test starts with a fresh quota.
    """
    from app.core.redis import get_redis

    redis = await get_redis()
    async for key in redis.scan_iter(match="ratelimit:*"):
        await redis.delete(key)
    yield