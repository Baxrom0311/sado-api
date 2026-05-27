"""Assessment, audio-recording, and analysis-result models.

A single ``Assessment`` represents a sitting where a child plays one or
more speech tasks. Each task produces an ``AudioRecording``; the
recording is processed asynchronously into an ``AnalysisResult``.

Storing ML output as JSON keeps the schema portable across SQLite (for
local tests) and Postgres (production) without losing fidelity.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._base import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:  # pragma: no cover
    from app.models.child import Child
    from app.models.user import User


class AssessmentType(str, enum.Enum):
    """High-level kind of assessment session."""

    SCREENING = "screening"
    DIAGNOSTIC = "diagnostic"
    FOLLOW_UP = "follow_up"
    PRACTICE = "practice"


class AssessmentStatus(str, enum.Enum):
    """Lifecycle states for an assessment session."""

    PENDING = "pending"          # created, no audio yet
    IN_PROGRESS = "in_progress"  # recordings uploading
    PROCESSING = "processing"    # at least one recording in worker queue
    COMPLETED = "completed"      # all recordings analysed
    FAILED = "failed"            # processing error
    CANCELLED = "cancelled"


class RiskLevel(str, enum.Enum):
    """Triage outcome — Green/Yellow/Red bucket."""

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class RecordingTaskType(str, enum.Enum):
    """The clinical task a single audio clip targets."""

    REPEAT_WORD = "repeat_word"
    REPEAT_SENTENCE = "repeat_sentence"
    NAME_PICTURE = "name_picture"
    FREE_SPEECH = "free_speech"
    SUSTAIN_VOWEL = "sustain_vowel"


class Assessment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One assessment session for a child."""

    __tablename__ = "assessments"

    child_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("children.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AssessmentType.SCREENING.value,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AssessmentStatus.PENDING.value,
        index=True,
    )

    overall_risk: Mapped[str | None] = mapped_column(String(10), nullable=True)
    overall_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    child: Mapped[Child] = relationship("Child", lazy="joined")
    created_by: Mapped[User | None] = relationship("User")
    recordings: Mapped[list[AudioRecording]] = relationship(
        "AudioRecording",
        back_populates="assessment",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="AudioRecording.created_at.asc()",
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"<Assessment {self.id} child={self.child_id} status={self.status}>"


class AudioRecording(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single audio clip captured during an assessment task."""

    __tablename__ = "audio_recordings"

    assessment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("assessments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    task_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=RecordingTaskType.REPEAT_WORD.value,
    )
    prompt: Mapped[str | None] = mapped_column(String(500), nullable=True)

    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False, default="audio/wav")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)

    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    assessment: Mapped[Assessment] = relationship(
        "Assessment", back_populates="recordings"
    )
    analysis: Mapped[AnalysisResult | None] = relationship(
        "AnalysisResult",
        back_populates="recording",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"<AudioRecording {self.id} task={self.task_type}>"


class AnalysisResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """ML output for a single recording.

    Heavy feature payloads (MFCC matrix, formant trajectories, phoneme
    confusion table) live in JSON columns so we can ship rich charts to
    the therapist UI without a separate column per metric.
    """

    __tablename__ = "analysis_results"

    recording_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("audio_recordings.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    risk_level: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default=RiskLevel.GREEN.value,
        index=True,
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)

    mfcc_features: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    pitch_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    formant_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    phoneme_scores: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    feature_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    model_name: Mapped[str] = mapped_column(String(100), nullable=False, default="mock-xgb-v1")
    model_version: Mapped[str] = mapped_column(String(40), nullable=False, default="0.1.0")

    recording: Mapped[AudioRecording] = relationship(
        "AudioRecording", back_populates="analysis"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"<AnalysisResult {self.id} risk={self.risk_level}>"
