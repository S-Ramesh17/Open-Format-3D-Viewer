from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.api.responses import envelope
from app.config import settings
from app.core.error_handlers import register_exception_handlers
from app.core.redis import close_redis, get_redis
from app.db.engine import AsyncSessionLocal
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_id import RequestIDMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.routers import auth as auth_router
from app.routers import projects as projects_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = await get_redis()
    await redis.ping()
    yield
    await close_redis()


app = FastAPI(
    title="Open Format 3D Viewer API",
    version="0.1.0",
    lifespan=lifespan,
)

register_exception_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    expose_headers=[
        "X-Request-ID",
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "X-RateLimit-Reset",
    ],
    max_age=600,
)

app.add_middleware(RateLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY,
)

app.include_router(auth_router.router)
app.include_router(projects_router.router)


@app.get("/health")
async def health():
    """
    Health check endpoint.
    Verifies PostgreSQL and Redis connectivity.
    Returns HTTP 503 if either dependency is unavailable.
    """
    db_status = "ok"
    redis_status = "ok"
    http_status = 200

    # Check PostgreSQL
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
    except Exception:
        db_status = "unavailable"
        http_status = 503

    # Check Redis
    try:
        redis = await get_redis()
        await redis.ping()
    except Exception:
        redis_status = "unavailable"
        http_status = 503

    return envelope(
        {"status": "ok" if http_status == 200 else "degraded",
         "db": db_status,
         "redis": redis_status},
        status_code=http_status,
    )