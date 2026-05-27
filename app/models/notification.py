"""User notification model.

Notifications power the in-app inbox shown to parents, teachers and
therapists. A small ``type`` discriminator keeps the schema flat while
letting clients render contextual icons.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._base import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:  # pragma: no cover
    from app.models.user import User


class NotificationType(str, enum.Enum):
    """Top-level notification categories shown in the inbox."""

    ASSESSMENT_COMPLETED = "assessment_completed"
    ASSESSMENT_SCHEDULED = "assessment_scheduled"
    EXERCISE_ASSIGNED = "exercise_assigned"
    EXERCISE_DUE = "exercise_due"
    REFERRAL = "referral"
    SYSTEM = "system"


class Notification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One in-app notification targeted at a single user."""

    __tablename__ = "notifications"

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default=NotificationType.SYSTEM.value,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Free-form payload for client-side routing (e.g. {"assessment_id": ...}).
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    is_archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )

    user: Mapped[User] = relationship("User")

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"<Notification {self.id} type={self.type} user={self.user_id}>"


__all__ = ["Notification", "NotificationType"]
