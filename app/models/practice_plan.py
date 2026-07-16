"""Practice plan + plan-item models.

A :class:`PracticePlan` is a curated set of exercises prescribed for a
single child. It is the bridge between deterministic recommendations
(text advice produced by :mod:`app.services.recommendations`) and the
concrete :class:`app.models.exercise.Exercise` catalogue: each plan
item references a real exercise and carries target / completion
counters so the parent UI can render progress bars.

Plans may be:

* **Generated** automatically from an :class:`Assessment` by
  :func:`app.services.practice_plan.generate_plan_from_assessment` —
  this maps voice-quality flags and weakest phonemes onto the best
  matching exercises.
* **Authored manually** by a therapist or admin who handpicks items.

Tenancy follows the child: when the child has a ``tenant_id`` the plan
inherits it so multi-tenant scope filters keep working.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._base import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:  # pragma: no cover
    from app.models.assessment import Assessment
    from app.models.child import Child
    from app.models.exercise import Exercise
    from app.models.user import User


class PracticePlanStatus(str, enum.Enum):
    """Lifecycle of a :class:`PracticePlan`."""

    DRAFT = "draft"          # being authored, not yet active
    ACTIVE = "active"        # parent / child should follow it
    COMPLETED = "completed"  # all items done or therapist closed it
    ARCHIVED = "archived"    # paused / superseded by a newer plan


class PracticePlanItemStatus(str, enum.Enum):
    """Lifecycle of a single plan item."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class PracticePlan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A curated practice plan prescribed for a child."""

    __tablename__ = "practice_plans"

    child_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("children.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Optional anchor to the assessment that produced this plan. ``NULL``
    # for manually authored plans.
    assessment_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("assessments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_by_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    tenant_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("kindergartens.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=PracticePlanStatus.DRAFT.value,
        index=True,
    )
    locale: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default="uz",
    )

    # Stable focus codes (e.g. ``"phoneme:r"``, ``"voice_quality:high_jitter"``,
    # ``"fluency:slow_speech_rate"``) so the UI can render badges /
    # filter without re-parsing item titles.
    focus_areas: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True
    )

    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    child: Mapped[Child] = relationship("Child", lazy="joined")
    assessment: Mapped[Assessment | None] = relationship(
        "Assessment", lazy="joined"
    )
    created_by: Mapped[User | None] = relationship("User", lazy="joined")
    items: Mapped[list[PracticePlanItem]] = relationship(
        "PracticePlanItem",
        back_populates="plan",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="PracticePlanItem.priority.asc(), PracticePlanItem.created_at.asc()",
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return (
            f"<PracticePlan {self.id} child={self.child_id} "
            f"status={self.status}>"
        )


class PracticePlanItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single exercise prescription inside a :class:`PracticePlan`."""

    __tablename__ = "practice_plan_items"

    plan_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("practice_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    exercise_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("exercises.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=PracticePlanItemStatus.PENDING.value,
        index=True,
    )
    # 1 = highest priority, 5 = lowest. Mirrors the standard "P1..P5"
    # bug-tracker convention so it's intuitive for therapists.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    target_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    focus_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Optional rich-context blob the generator stores on each item so
    # the UI can show *why* this exercise was prescribed (matched
    # phoneme, matched flag, score, etc.).
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )

    plan: Mapped[PracticePlan] = relationship(
        "PracticePlan", back_populates="items"
    )
    exercise: Mapped[Exercise] = relationship("Exercise", lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return (
            f"<PracticePlanItem {self.id} plan={self.plan_id} "
            f"exercise={self.exercise_id} status={self.status}>"
        )


__all__ = [
    "PracticePlan",
    "PracticePlanItem",
    "PracticePlanItemStatus",
    "PracticePlanStatus",
]
