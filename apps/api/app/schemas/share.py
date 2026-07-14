import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ShareLinkCreateRequest(BaseModel):
    model_id: uuid.UUID
    expires_at: datetime | None = Field(
        default=None,
        description="Optional ISO-8601 expiry datetime. Omit for no expiry.",
    )


class ShareLinkResponse(BaseModel):
    id: uuid.UUID
    model_id: uuid.UUID
    token: str
    expires_at: datetime | None
    revoked: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class PublicModelResponse(BaseModel):
    """
    Redacted representation of a model for public (unauthenticated) access
    via a share link. Deliberately excludes project_id and uploaded_by
    (internal identifiers that should not leak to anonymous viewers).
    """
    id: uuid.UUID
    name: str          # always populated in service layer via `name or original_filename`
    format: str
    status: str
    created_at: datetime
    updated_at: datetime
    chunk_urls: list[str]

    model_config = {"from_attributes": False}  # constructed manually in service, not from ORM