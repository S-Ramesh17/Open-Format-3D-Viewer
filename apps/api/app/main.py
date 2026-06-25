from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.api.responses import envelope
from app.config import settings
from app.core.error_handlers import register_exception_handlers
from app.core.redis import close_redis, get_redis
from app.core.sentry import init_sentry
init_sentry()
from app.db.engine import AsyncSessionLocal
from app.middleware.auth_middleware import AuthMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_id import RequestIDMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.routers import auth as auth_router
from app.routers import models as models_router
from app.routers import annotations as annotations_router
from app.routers import webhooks as webhooks_router
from app.routers import projects as projects_router
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware




@asynccontextmanager
async def lifespan(app: FastAPI):
     # Validate Redis
     redis = await get_redis()
     await redis.ping()
     # Validate DB
     async with AsyncSessionLocal() as db:
         await db.execute(text("SELECT 1"))
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

# Order matters — outermost first.
# AuthMiddleware must run AFTER RateLimitMiddleware so rate-limit headers
# are present even on 401 responses, and AFTER RequestIDMiddleware so
# request_id is available for the 401 error body.
app.add_middleware(RateLimitMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(ProxyHeadersMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY,
)

app.include_router(auth_router.router)
app.include_router(projects_router.router)
app.include_router(models_router.router)
app.include_router(annotations_router.router)
app.include_router(webhooks_router.router)


@app.get("/health")
async def health():
    db_status = "ok"
    redis_status = "ok"
    http_status = 200

    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
    except Exception:
        db_status = "unavailable"
        http_status = 503

    try:
        redis = await get_redis()
        await redis.ping()
    except Exception:
        redis_status = "unavailable"
        http_status = 503

    return envelope(
        {
            "status": "ok" if http_status == 200 else "degraded",
            "db": db_status,
            "redis": redis_status,
        },
        status_code=http_status,
    )