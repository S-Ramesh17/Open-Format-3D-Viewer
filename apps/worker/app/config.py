from typing import Literal

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
    # TEMP LOCAL STORAGE — set to "s3" once AWS credentials are available
    STORAGE_PROVIDER: str = "s3"  # "s3" | "local"
    LOCAL_STORAGE_PATH: str = "/tmp/openformat_uploads"
    # END TEMP LOCAL STORAGE
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    SENTRY_DSN: str = ""

    # Upload limits (mirrors apps/api/app/config.py::MAX_UPLOAD_SIZE_BYTES —
    # kept as a separate setting since worker and api are deployed/scaled
    # independently, but should be set to the same value operationally)
    MAX_UPLOAD_SIZE_BYTES: int = 500 * 1024 * 1024  # 500MB

    # Prometheus /metrics HTTP server port (multiprocess-aggregated)
    WORKER_METRICS_PORT: int = 9090
    WS_METRICS_PORT: int = 9091

    # XKT conversion: path to @xeokit/xeokit-convert CLI or node script
    XEOKIT_CONVERT_BIN: str = "xeokit-convert"
    # Chunk size limit in bytes (16 MB)
    XKT_CHUNK_MAX_BYTES: int = 16 * 1024 * 1024

     # GLTF/GLB pipeline tools
    GLTF_PIPELINE_BIN: str = "gltf-pipeline"   # npm install -g gltf-pipeline
    GLTF_VALIDATOR_BIN: str = "gltf-validator"  # npm install -g gltf-validator

    # ClamAV (TCP socket via clamd)
    CLAMD_HOST: str = "localhost"
    CLAMD_PORT: int = 3310
    CLAMD_TIMEOUT: int = 60

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = WorkerSettings()
