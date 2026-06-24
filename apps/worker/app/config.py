from pydantic_settings import BaseSettings


class WorkerSettings(BaseSettings):
    REDIS_URL: str = "redis://localhost:6379/0"
    DATABASE_URL: str
    AWS_REGION: str = "us-east-1"
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    S3_RAW_BUCKET: str = ""
    S3_PROCESSED_BUCKET: str = ""
    CDN_BASE_URL: str = ""
    ENVIRONMENT: str = "development"
    SENTRY_DSN: str = ""

    # XKT conversion: path to @xeokit/xeokit-convert CLI or node script
    XEOKIT_CONVERT_BIN: str = "xeokit-convert"
    # Chunk size limit in bytes (16 MB)
    XKT_CHUNK_MAX_BYTES: int = 16 * 1024 * 1024

     # GLTF/GLB pipeline tools
    GLTF_PIPELINE_BIN: str = "gltf-pipeline"   # npm install -g gltf-pipeline
    GLTF_VALIDATOR_BIN: str = "gltf-validator"  # npm install -g gltf-validator

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = WorkerSettings()
