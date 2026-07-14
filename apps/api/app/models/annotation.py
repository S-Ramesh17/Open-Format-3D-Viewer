import uuid
from datetime import datetime

from sqlalchemy import String, Text, DateTime, Enum, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Annotation(Base):
    __tablename__ = "annotations"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    message: Mapped[str | None] = mapped_column(Text)
    position_xyz: Mapped[list | None] = mapped_column(JSONB)
    normal_xyz: Mapped[list | None] = mapped_column(JSONB)
    bcf_guid: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(
        Enum("open", "in_review", "resolved", name="annotation_status_enum"),
        default="open",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("ix_annotations_model_id", "model_id"),
        Index("ix_annotations_author_id", "author_id"),
        Index("ix_annotations_model_status", "model_id", "status"),
        Index(
            "ix_annotations_model_created_at",
            "model_id",
            "created_at",
            postgresql_ops={"created_at": "DESC"},
        ),
    )