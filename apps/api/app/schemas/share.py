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