"""Exercise + ExerciseAssignment models.

Exercises are the daily speech-therapy activities that parents and
therapists can assign to children. Each exercise belongs to a category
(e.g. ``vocabulary``, ``articulation``), targets an age group, and
ships with optional audio + image example assets stored alongside
audio recordings in MinIO / local storage.

Assignments are created when a therapist or parent prescribes an
exercise to a specific child. Completion captures a self-reported
score so the dashboard can plot adherence and progress.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._base import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:  # pragma: no cover
    from app.models.child import Child
    from app.models.user import User


class ExerciseCategory(str, enum.Enum):
    """Domain the exercise targets."""

    ARTICULATION = "articulation"
    VOCABULARY = "vocabulary"
    PHONEMIC_AWARENESS = "phonemic_awareness"
    FLUENCY = "fluency"
    LISTENING = "listening"
    GRAMMAR = "grammar"
    BREATHING = "breathing"


class ExerciseDifficulty(str, enum.Enum):
    """Self-reported difficulty band, 1–3 = easy / medium / hard."""

    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class ExerciseAgeGroup(str, enum.Enum):
    """Coarse age bucket — matches the ages SADO targets (2–12)."""

    TODDLER = "2-3"
    PRESCHOOL = "4-5"
    EARLY_PRIMARY = "6-7"
    PRIMARY = "8-10"
    LATE_PRIMARY = "11-12"


class AssignmentStatus(str, enum.Enum):
    """Lifecycle of an :class:`ExerciseAssignment`."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    EXPIRED = "expired"


class Exercise(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A reusable speech-therapy exercise template.

    Audio and image example paths are storage keys (relative to the
    bucket / local-storage root) rather than full URLs so the API can
    sign them on demand.
    """

    __tablename__ = "exercises"

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    category: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ExerciseCategory.ARTICULATION.value,
        index=True,
    )
    age_group: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ExerciseAgeGroup.PRESCHOOL.value,
        index=True,
    )
    difficulty: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ExerciseDifficulty.EASY.value,
        index=True,
    )
    language: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default="uz",
        index=True,
    )
    duration_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5
    )

    audio_example_path: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_phonemes: Mapped[str | None] = mapped_column(String(200), nullable=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True
    )
    created_by_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_by: Mapped[User | None] = relationship("User", lazy="joined")
    assignments: Mapped[list[ExerciseAssignment]] = relationship(
        "ExerciseAssignment",
        back_populates="exercise",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"<Exercise {self.title} {self.category}/{self.difficulty}>"


class ExerciseAssignment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Pairing between a child and an exercise template."""

    __tablename__ = "exercise_assignments"
    __table_args__ = (
        UniqueConstraint(
            "child_id",
            "exercise_id",
            "due_date",
            name="uq_exercise_assignment_child_ex_due",
        ),
    )

    child_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("children.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    exercise_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("exercises.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    assigned_by_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AssignmentStatus.PENDING.value,
        index=True,
    )
    due_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    child: Mapped[Child] = relationship("Child", lazy="joined")
    exercise: Mapped[Exercise] = relationship(
        "Exercise", back_populates="assignments", lazy="joined"
    )
    assigned_by: Mapped[User | None] = relationship("User", lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return (
            f"<ExerciseAssignment child={self.child_id} "
            f"exercise={self.exercise_id} status={self.status}>"
        )
