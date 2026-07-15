from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.db.engine import AsyncSessionLocal
from app.middleware.auth_middleware import AuthMiddleware
from app.middleware.csrf_middleware import CSRFMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_id import RequestIDMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.routers import auth as auth_router
from app.routers import models as models_router
from app.routers import annotations as annotations_router
import asyncio

from app.routers import webhooks as webhooks_router
from app.routers import projects as projects_router
from app.routers import share as share_router
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

# Prometheus metrics — optional; present when poetry install has run
try:
    from prometheus_fastapi_instrumentator import Instrumentator
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    from fastapi.responses import Response as _PrometheusResponse
    _PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PROMETHEUS_AVAILABLE = False
    Instrumentator = None  # type: ignore[assignment,misc]
    CONTENT_TYPE_LATEST = "text/plain"
    generate_latest = lambda: b""  # noqa: E731
    _PrometheusResponse = None

# TEMP LOCAL STORAGE — static file serving for local processed outputs
# REMOVE AFTER S3 CREDENTIALS AVAILABLE
import os as _os
from fastapi.responses import FileResponse

from app.api.responses import envelope
from app.config import settings
from app.core.error_handlers import register_exception_handlers
from app.core.logging import configure_logging
from app.core.redis import close_redis, get_redis
from app.core.sentry import init_sentry
configure_logging(settings.ENVIRONMENT)
init_sentry()



@asynccontextmanager
async def lifespan(app: FastAPI):
    # Validate Redis
    redis = await get_redis()
    await redis.ping()
    # Validate DB
    async with AsyncSessionLocal() as db:
        await db.execute(text("SELECT 1"))

    # Start background gauge collector
    async def _gauge_loop():
        from app.core.metrics_collector import collect_db_gauges
        while True:
            await collect_db_gauges(AsyncSessionLocal)
            await asyncio.sleep(15)

    _bg_task = asyncio.create_task(_gauge_loop())

    yield

    _bg_task.cancel()
    try:
        await _bg_task
    except asyncio.CancelledError:
        pass
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
# Wire prometheus-fastapi-instrumentator — auto-tracks all HTTP routes
if _PROMETHEUS_AVAILABLE:
    Instrumentator(
        should_group_status_codes=False,
        should_ignore_untemplated=True,
        excluded_handlers=["/health", "/metrics"],
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
    
# Order matters — Starlette wraps in LIFO order: the LAST middleware
# added via add_middleware() becomes the OUTERMOST layer and therefore
# runs FIRST on every request. To make RateLimitMiddleware wrap (and run
# before) AuthMiddleware — so failed/blocked auth attempts are still
# counted against the per-IP rate limit — Auth must be added first and
# RateLimit added after it. CSRFMiddleware is added even before
# AuthMiddleware so it becomes the innermost layer of all, running
# immediately after AuthMiddleware has resolved request.state.auth_via_cookie
# and right before the route itself — it needs that flag to distinguish a
# cookie-authenticated browser request (CSRF applies) from a Bearer-token
# request (CSRF bypassed entirely).
app.add_middleware(CSRFMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(RateLimitMiddleware)
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
app.include_router(share_router.router)

# TEMP LOCAL STORAGE — serve locally-stored processed files
# REMOVE AFTER S3 CREDENTIALS AVAILABLE
@app.get("/files/{file_path:path}")
async def serve_local_file(file_path: str):
    """
    Serve processed output files stored locally.
    Only active when STORAGE_PROVIDER=local.
    In production this route is never hit — CDN_BASE_URL handles delivery.
    """
    if settings.STORAGE_PROVIDER != "local":
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Not found")
    # Early reject on obvious traversal patterns
    if ".." in file_path or file_path.startswith("/"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid file path")

    # Resolve both base and target with realpath to catch symlink escapes
    base = _os.path.realpath(_os.path.join(settings.LOCAL_STORAGE_PATH, "processed"))
    full_path = _os.path.realpath(_os.path.join(base, file_path))

    # Confirm resolved path is still inside the processed/ directory
    if not full_path.startswith(base + _os.sep) and full_path != base:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid file path")

    if not _os.path.exists(full_path) or not _os.path.isfile(full_path):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(full_path)
# END TEMP LOCAL STORAGE


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