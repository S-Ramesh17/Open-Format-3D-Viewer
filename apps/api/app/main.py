from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.core.redis import close_redis, get_redis
from app.routers import auth as auth_router


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

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY,
)

app.include_router(auth_router.router)


@app.get("/health")
async def health():
    return {"status": "ok"}