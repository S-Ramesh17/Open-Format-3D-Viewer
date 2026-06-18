from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str
    DATABASE_URL_SYNC: str
    SECRET_KEY: str
    ENVIRONMENT: str = "development"

    # JWT
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Google OAuth
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/auth/google/callback"

    # CORS
    ALLOWED_ORIGINS: str = "http://localhost:3000"

    # Rate limiting
    RATE_LIMIT_FREE_PER_HOUR: int = 100
    RATE_LIMIT_PRO_PER_HOUR: int = 10000

    @property
    def allowed_origins_list(self) -> list[str]:
        """Parse comma-separated origins into a list."""
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()

@property
def effective_database_url_sync(self) -> str:
    """
    Derive sync URL from async URL if DATABASE_URL_SYNC not explicitly set.
    Render only provides one connection string — this handles that case.
    """
    if self.DATABASE_URL_SYNC:
        return self.DATABASE_URL_SYNC
    # Convert asyncpg URL to psycopg2 URL
    return self.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")