from pydantic_settings import BaseSettings


class WorkerSettings(BaseSettings):
    REDIS_URL: str = "redis://localhost:6379/0"
    DATABASE_URL: str = "postgresql+asyncpg://openformat:devpassword@localhost:5432/openformat_dev"
    AWS_REGION: str = "us-east-1"
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    S3_RAW_BUCKET: str = ""
    S3_PROCESSED_BUCKET: str = ""
    CDN_BASE_URL: str = ""
    ENVIRONMENT: str = "development"
    SENTRY_DSN: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

settings = WorkerSettings()