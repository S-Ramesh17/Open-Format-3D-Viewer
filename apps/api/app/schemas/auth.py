import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator


# ── Request schemas ──────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr = Field(
        ...,
        description="Valid email address. Used as login identifier.",
        examples=["user@example.com"],
    )
    password: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="Password must be 8-128 characters.",
        examples=["securepassword123"],
    )
    full_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Display name shown in the UI.",
        examples=["Jane Smith"],
    )

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if v.isdigit():
            raise ValueError("Password cannot be all numbers")
        return v

    @field_validator("email")
    @classmethod
    def email_lowercase(cls, v: str) -> str:
        return v.lower().strip()


class LoginRequest(BaseModel):
    email: EmailStr = Field(
        ...,
        description="Registered email address.",
        examples=["user@example.com"],
    )
    password: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Account password.",
        examples=["securepassword123"],
    )

    @field_validator("email")
    @classmethod
    def email_lowercase(cls, v: str) -> str:
        return v.lower().strip()


class RefreshRequest(BaseModel):
    refresh_token: str = Field(
        ...,
        min_length=10,
        max_length=1000,
        description="Valid refresh token issued at login.",
    )


class LogoutRequest(BaseModel):
    refresh_token: str = Field(
        ...,
        min_length=10,
        max_length=1000,
        description="Refresh token to revoke.",
    )


class CreateApiKeyRequest(BaseModel):
    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Human-readable label for this API key.",
        examples=["production-integration"],
    )

    @field_validator("name")
    @classmethod
    def name_no_whitespace_only(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Name cannot be blank")
        return v.strip()


class RevokeApiKeyRequest(BaseModel):
    key_id: uuid.UUID = Field(
        ...,
        description="UUID of the API key to revoke.",
    )


# ── Response schemas ─────────────────────────────────────────────────────────

class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    name: str | None = Field(default=None, validation_alias="full_name")
    plan: str
    provider: str | None
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class AuthResponse(BaseModel):
    """
    Per PRD 5.3: register/login/refresh return access_token + refresh_token
    in the body (in addition to the httpOnly cookies set for browser
    clients, which remain the primary mechanism for the web app).
    """
    user: UserResponse
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


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
    key: str
    id: uuid.UUID
    name: str
    created_at: datetime

    model_config = {"from_attributes": True}