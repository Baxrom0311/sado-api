"""Gamification models — XP, levels, streaks, and badges.

The mobile app rewards kids with XP, level-ups, daily streaks, and
collectable badges every time they finish an exercise or assessment.
This module persists the per-child progress and the badge catalogue.

* :class:`Gamification` — 1:1 with :class:`~app.models.child.Child`. Holds
  the running totals (XP, level, streak, last activity).
* :class:`Badge` — global catalogue of earnable badges. Each row carries
  bilingual (uz + ru) title/description and the *requirement* the
  service layer checks against.
* :class:`BadgeEarning` — junction table recording which badges a child
  has unlocked and when.

Storing the requirement as ``(requirement_type, threshold)`` keeps the
catalogue editable from the admin UI without a code change.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._base import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:  # pragma: no cover
    from app.models.child import Child


class BadgeRequirementType(str, enum.Enum):
    """How the gamification service decides whether a badge is earned."""

    XP = "xp"
    LEVEL = "level"
    STREAK = "streak"
    EXERCISES_COMPLETED = "exercises_completed"
    ASSESSMENTS_COMPLETED = "assessments_completed"


class BadgeCategory(str, enum.Enum):
    """Coarse grouping used by the mobile UI for the badge wall."""

    MILESTONE = "milestone"
    STREAK = "streak"
    LEVEL = "level"
    PRACTICE = "practice"
    SPECIAL = "special"


class Gamification(TimestampMixin, Base):
    """Running gamification stats for a single child.

    The child id doubles as the primary key — each child has exactly
    one progress row, created lazily the first time XP is awarded.
    """

    __tablename__ = "gamification"

    child_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("children.id", ondelete="CASCADE"),
        primary_key=True,
    )

    total_xp: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, index=True
    )
    level: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, index=True
    )
    streak_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    longest_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_activity_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, index=True
    )

    total_exercises_completed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    total_assessments_completed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    child: Mapped[Child] = relationship("Child", lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return (
            f"<Gamification child={self.child_id} xp={self.total_xp} "
            f"level={self.level} streak={self.streak_days}>"
        )


class Badge(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A badge that children can earn through gameplay."""

    __tablename__ = "badges"

    code: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    title_uz: Mapped[str] = mapped_column(String(120), nullable=False)
    title_ru: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    description_uz: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    description_ru: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    icon: Mapped[str] = mapped_column(String(64), nullable=False, default="🏅")

    category: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=BadgeCategory.MILESTONE.value,
        index=True,
    )
    requirement_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=BadgeRequirementType.XP.value,
        index=True,
    )
    threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True
    )

    earnings: Mapped[list[BadgeEarning]] = relationship(
        "BadgeEarning",
        back_populates="badge",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"<Badge {self.code} ({self.requirement_type}>={self.threshold})>"


class BadgeEarning(UUIDPrimaryKeyMixin, Base):
    """One row per ``(child, badge)`` pair, stamped with the unlock time."""

    __tablename__ = "badge_earnings"
    __table_args__ = (
        UniqueConstraint("child_id", "badge_id", name="uq_badge_earnings_child_badge"),
    )

    child_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("children.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    badge_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("badges.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    earned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    badge: Mapped[Badge] = relationship(
        "Badge", back_populates="earnings", lazy="joined"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"<BadgeEarning child={self.child_id} badge={self.badge_id}>"


__all__ = [
    "Badge",
    "BadgeCategory",
    "BadgeEarning",
    "BadgeRequirementType",
    "Gamification",
]
