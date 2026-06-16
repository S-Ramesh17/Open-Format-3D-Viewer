import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, field_validator


# ── Request schemas ──────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str | None = None

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class CreateApiKeyRequest(BaseModel):
    name: str


class RevokeApiKeyRequest(BaseModel):
    key_id: uuid.UUID


# ── Response schemas ─────────────────────────────────────────────────────────

class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str | None
    plan: str
    provider: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ApiKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ApiKeyCreatedResponse(BaseModel):
    key: str  # plaintext — shown once only
    id: uuid.UUID
    name: str
    created_at: datetime

    model_config = {"from_attributes": True}