import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


# ── Request schemas ──────────────────────────────────────────────────────────

class ProjectCreate(BaseModel):
    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Project name.",
        examples=["City Center Tower"],
    )
    description: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional project description.",
        examples=["Main structural model for the tower project."],
    )

    @field_validator("name")
    @classmethod
    def name_strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name cannot be blank")
        return v

    @field_validator("description")
    @classmethod
    def description_strip(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            return v if v else None
        return None


class ProjectUpdate(BaseModel):
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Updated project name.",
    )
    description: str | None = Field(
        default=None,
        max_length=2000,
        description="Updated project description.",
    )

    @field_validator("name")
    @classmethod
    def name_strip(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("Name cannot be blank")
        return v

    @field_validator("description")
    @classmethod
    def description_strip(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            return v if v else None
        return None


class ProjectMemberInvite(BaseModel):
    email: str = Field(
        ...,
        description="Email of an existing registered user to add to the project.",
        examples=["colleague@example.com"],
    )
    role: str = Field(
        default="viewer",
        description="Initial role for the invited member.",
        examples=["viewer"],
    )

    @field_validator("email")
    @classmethod
    def email_normalise(cls, v: str) -> str:
        v = v.strip().lower()
        if not v or "@" not in v:
            raise ValueError("A valid email is required")
        return v

    @field_validator("role")
    @classmethod
    def role_valid(cls, v: str) -> str:
        if v not in {"viewer", "editor", "admin"}:
            raise ValueError("role must be one of: viewer, editor, admin")
        return v


class ProjectMemberRoleUpdate(BaseModel):
    role: str = Field(..., description="New role for the member.", examples=["editor"])

    @field_validator("role")
    @classmethod
    def role_valid(cls, v: str) -> str:
        if v not in {"viewer", "editor", "admin"}:
            raise ValueError("role must be one of: viewer, editor, admin")
        return v


# ── Response schemas ─────────────────────────────────────────────────────────

class ProjectMemberResponse(BaseModel):
    user_id: uuid.UUID
    role: str
    email: str
    name: str | None

    model_config = {"from_attributes": True}


class ProjectResponse(BaseModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime
    role: str | None = None  # current user's role in this project

    model_config = {"from_attributes": True}