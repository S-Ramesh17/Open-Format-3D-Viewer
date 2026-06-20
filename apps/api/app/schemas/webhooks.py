import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

ALLOWED_EVENTS = {
    "model.ready",
    "model.failed",
    "annotation.created",
    "annotation.resolved",
    "comment.created",
}


class WebhookCreate(BaseModel):
    url: str = Field(..., min_length=1, max_length=2000)
    events: list[str] = Field(..., min_length=1)

    @field_validator("url")
    @classmethod
    def url_https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("Webhook URL must use https://")
        return v

    @field_validator("events")
    @classmethod
    def events_valid(cls, v: list[str]) -> list[str]:
        invalid = set(v) - ALLOWED_EVENTS
        if invalid:
            raise ValueError(f"Invalid events: {invalid}. Allowed: {ALLOWED_EVENTS}")
        return v


class WebhookUpdate(BaseModel):
    url: str | None = Field(default=None, max_length=2000)
    events: list[str] | None = Field(default=None)
    is_active: bool | None = Field(default=None)

    @field_validator("events")
    @classmethod
    def events_valid(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            invalid = set(v) - ALLOWED_EVENTS
            if invalid:
                raise ValueError(f"Invalid events: {invalid}. Allowed: {ALLOWED_EVENTS}")
        return v


class WebhookResponse(BaseModel):
    id: uuid.UUID
    url: str
    events: list[str] | None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}