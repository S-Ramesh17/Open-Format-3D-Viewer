import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


# ── Request schemas ──────────────────────────────────────────────────────────

class ModelUploadRequest(BaseModel):
    project_id: uuid.UUID = Field(..., description="Target project for this model.")
    filename: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Original filename including extension.",
        examples=["tower_structural.ifc"],
    )
    content_type: str = Field(
        ...,
        max_length=255,
        description="MIME type of the file as reported by the client.",
        examples=["application/octet-stream"],
    )
    size_bytes: int = Field(
        ...,
        gt=0,
        le=500 * 1024 * 1024,
        description="File size in bytes. Max 500MB.",
    )
    name: str | None = Field(
        default=None,
        max_length=500,
        description="Optional display name; defaults to filename if omitted.",
        examples=["Main Tower — Structural"],
    )

    @field_validator("filename")
    @classmethod
    def filename_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Filename cannot be blank")
        return v


# ── Response schemas ─────────────────────────────────────────────────────────

class ModelUploadResponse(BaseModel):
    model_id: uuid.UUID
    upload_url: str
    upload_fields: dict = {}  # S3 POST form fields; empty for local mode
    storage_key: str

class ModelResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    uploaded_by: uuid.UUID
    original_filename: str
    name: str | None = None
    format: str
    status: str
    file_size_bytes: int | None
    error_message: str | None
    element_count: int | None = None
    bounds_min_xyz: list[float] | None = None
    bounds_max_xyz: list[float] | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ModelElementResponse(BaseModel):
    id: uuid.UUID
    model_id: uuid.UUID
    guid: str
    element_type: str | None
    name: str | None
    properties: dict | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ModelTreeResponse(BaseModel):
    model_id: uuid.UUID
    tree: dict | None


class ModelChunksResponse(BaseModel):
    model_id: uuid.UUID
    chunks: list[str]