import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator
class AnnotationCreate(BaseModel):
    message: str | None = Field(default=None, max_length=5000)
    position_xyz: list[float] = Field(..., min_length=3, max_length=3)
    normal_xyz: list[float] = Field(..., min_length=3, max_length=3)


class AnnotationUpdate(BaseModel):
    message: str | None = Field(default=None, max_length=5000)
    status: str | None = Field(default=None)

    @field_validator("status")
    @classmethod
    def status_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in ("open", "in_review", "resolved"):
            raise ValueError("status must be 'open', 'in_review', or 'resolved'")
        return v


class AnnotationResponse(BaseModel):
    id: uuid.UUID
    model_id: uuid.UUID
    author_id: uuid.UUID
    message: str | None
    position_xyz: list[float] | None
    normal_xyz: list[float] | None
    bcf_guid: str
    status: str
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class CommentCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=3000)

    @field_validator("body")
    @classmethod
    def body_strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Comment cannot be blank")
        return v


class CommentResponse(BaseModel):
    id: uuid.UUID
    annotation_id: uuid.UUID
    author_id: uuid.UUID
    body: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}