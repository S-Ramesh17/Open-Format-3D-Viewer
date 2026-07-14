# apps/api/app/config.py
import math
from typing import Literal

from pydantic_settings import BaseSettings
from pydantic import AliasChoices, Field, computed_field, field_validator, model_validator

# Placeholder values that must never be used as a real secret. Checked
# case-insensitively so ".env.example" style values are always rejected.
_PLACEHOLDER_SECRET_KEYS = {
    "changeme",
    "change_me_to_a_random_64_char_hex_string",
    "secret",
    "changethis",
    "your-secret-key-here",
    "dev-secret-change-in-prod",
}

# Production-only floor. Random hex (secrets.token_hex) is exactly 4.0
# bits/char; random base64 is ~6.0. 3.0 comfortably accepts either while
# still rejecting low-diversity strings like "a"*32 (0 bits/char).
# Entropy alone doesn't catch every weak pattern — e.g. a 10-digit
# sequential cycle repeated to 32+ chars scores ~3.3 bits/char, above
# this floor — so it's paired with a distinct-character-count check
# below (>=16, matching the hex alphabet a real generated secret uses).
_PRODUCTION_MIN_ENTROPY_BITS_PER_CHAR = 3.0


def _shannon_entropy_bits_per_char(value: str) -> float:
    """Shannon entropy of the character distribution, in bits/char.
    Used only as a floor to catch obviously-weak secrets (all one
    character, tiny alphabet) — not a substitute for actually generating
    a secret with `secrets.token_hex`/`secrets.token_urlsafe`."""
    if not value:
        return 0.0
    freq: dict[str, int] = {}
    for ch in value:
        freq[ch] = freq.get(ch, 0) + 1
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in freq.values())

class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str = Field(validation_alias=AliasChoices("JWT_SECRET", "SECRET_KEY"))
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    
    WORKER_METRICS_PORT: int = Field(
    default=9090,
    validation_alias="WORKER_METRICS_PORT")

    WS_METRICS_PORT: int = Field(
    default=9091,
    validation_alias="WS_METRICS_PORT")

    @field_validator("SECRET_KEY")
    @classmethod
    def validate_secret_key(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError(
                f"SECRET_KEY must be at least 32 characters long, got {len(v)}. "
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        normalised = v.strip().lower().replace("-", "_")
        if normalised in _PLACEHOLDER_SECRET_KEYS:
            raise ValueError(
                "SECRET_KEY is a known placeholder value. Generate a real secret with: "
                "python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        return v

    @model_validator(mode="after")
    def validate_secret_key_strength_in_production(self) -> "Settings":
        """
        Length + placeholder-list checks (above) apply everywhere, so any
        environment already needs an explicit, non-trivial secret — this
        satisfies "development: allow local testing with explicit secret"
        without a separate carve-out.

        This second check is production-only and stricter: it rejects
        low-diversity secrets (e.g. "a"*32, or a short repeating pattern)
        that pass the length check but aren't actually random. A
        hand-typed dev secret that's merely "explicit" (not
        cryptographically generated) is fine for local testing and would
        fail this bar — that's the intended dev/prod distinction.
        """
        if self.ENVIRONMENT != "production":
            return self

        entropy = _shannon_entropy_bits_per_char(self.SECRET_KEY)
        distinct_chars = len(set(self.SECRET_KEY))

        if entropy < _PRODUCTION_MIN_ENTROPY_BITS_PER_CHAR or distinct_chars < 16:
            raise ValueError(
                "SECRET_KEY does not have enough entropy for production "
                f"(entropy={entropy:.2f} bits/char, distinct_chars={distinct_chars}; "
                f"require entropy>={_PRODUCTION_MIN_ENTROPY_BITS_PER_CHAR} and distinct_chars>=16). "
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        return self

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def normalise_database_url(cls, v: str) -> str:

        if v.startswith("postgresql+asyncpg://"):
            return v
       
        if v.startswith("postgresql://") or v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql+asyncpg://", 1)\
                     .replace("postgresql://", "postgresql+asyncpg://", 1)
        raise ValueError(
            f"DATABASE_URL must start with postgresql:// or postgresql+asyncpg://, got: {v!r}"
        )

    @computed_field
    @property
    def DATABASE_URL_SYNC(self) -> str:
        """
        psycopg2-compatible URL for Alembic migrations (sync engine).
        Always derived from DATABASE_URL — never independently configured.
        """
        return self.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

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
    
    # Storage provider
    # TEMP LOCAL STORAGE — set to "s3" once AWS credentials are available
    STORAGE_PROVIDER: str = "s3"  # "s3" | "local"
    LOCAL_STORAGE_PATH: str = "/tmp/openformat_uploads"

    # Observability
    SENTRY_DSN: str = ""
    APP_VERSION: str = "0.1.0"

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    model_config = {
    "env_file": ".env",
    "env_file_encoding": "utf-8",
    "extra": "ignore",
    "populate_by_name": True,
}


settings = Settings()