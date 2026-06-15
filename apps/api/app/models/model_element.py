import uuid
from datetime import datetime

from sqlalchemy import String, DateTime, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ModelElement(Base):
    __tablename__ = "model_elements"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), nullable=False
    )
    guid: Mapped[str] = mapped_column(String(255), nullable=False)
    element_type: Mapped[str | None] = mapped_column(String(255))
    name: Mapped[str | None] = mapped_column(String(500))
    properties: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_model_elements_model_id", "model_id"),
        Index("ix_model_elements_model_guid", "model_id", "guid"),
    )