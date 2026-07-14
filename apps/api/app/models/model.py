import uuid
from datetime import datetime

from sqlalchemy import String, Text, BigInteger, DateTime, Enum, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Model(Base):
    __tablename__ = "models"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    file_format: Mapped[str] = mapped_column(
        Enum("ifc", "gltf", "glb", "step", "stp", "obj", "stl", name="file_format_enum"),
        nullable=False,
    )
    s3_raw_key: Mapped[str | None] = mapped_column(String(1000))
    s3_processed_prefix: Mapped[str | None] = mapped_column(String(1000))
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(
        Enum("pending", "processing", "ready", "failed", name="model_status_enum"),
        default="pending",
        nullable=False,
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    element_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bounds_min_xyz: Mapped[list[float] | None] = mapped_column(JSONB, nullable=True)
    bounds_max_xyz: Mapped[list[float] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("ix_models_project_id", "project_id"),
        Index("ix_models_status", "status"),
        Index("ix_models_uploaded_by", "uploaded_by"),
        Index("ix_models_project_status", "project_id", "status"),
        Index(
            "ix_models_project_created_at",
            "project_id",
            "created_at",
            postgresql_ops={"created_at": "DESC"},
        ),
    )