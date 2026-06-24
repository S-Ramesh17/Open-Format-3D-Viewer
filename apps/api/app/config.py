from pydantic_settings import BaseSettings
from pydantic import computed_field


class Settings(BaseSettings):
    # Only one source of truth for the DB connection
    DATABASE_URL: str          # must start with postgresql+asyncpg://
    SECRET_KEY: str
    ENVIRONMENT: str = "development"

    @computed_field
    @property
    def DATABASE_URL_SYNC(self) -> str:
        """
        Derive the sync URL (psycopg2) from the async URL (asyncpg).
        Alembic uses this. It is always consistent with DATABASE_URL.
        """
        return self.DATABASE_URL.replace(
            "postgresql+asyncpg://", "postgresql://"
        )

    # JWT
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Google OAuth
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/v1/auth/google/callback"

    # CORS
    ALLOWED_ORIGINS: str = "http://localhost:3000"

    # Frontend redirect target (OAuth callback)
    FRONTEND_URL: str = "http://localhost:3000"

    # Rate limiting
    RATE_LIMIT_FREE_PER_HOUR: int = 100
    RATE_LIMIT_PRO_PER_HOUR: int = 10000

    # AWS / S3
    AWS_REGION: str = "us-east-1"
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    S3_RAW_BUCKET: str = ""
    S3_PROCESSED_BUCKET: str = ""
    CDN_BASE_URL: str = ""

    # Upload limits
    MAX_UPLOAD_SIZE_BYTES: int = 500 * 1024 * 1024  # 500MB

    # Observability
    SENTRY_DSN: str = ""
    APP_VERSION: str = "0.1.0"

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()