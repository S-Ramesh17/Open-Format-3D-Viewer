import asyncio
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
async def db_session() -> AsyncSession:
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
async def client() -> AsyncClient:
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