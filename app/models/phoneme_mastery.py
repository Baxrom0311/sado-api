"""Per-child phoneme mastery model.

Each row aggregates a child's pronunciation accuracy for one phoneme
in one language across every analysed recording. It is the source of
truth for:

* radar / heatmap visualisations on the mobile app
  (``GET /children/{id}/phoneme-mastery``),
* the speech-profile endpoint that surfaces strongest / weakest
  phonemes alongside voice-quality trends
  (``GET /children/{id}/speech-profile``),
* the practice-plan generator when picking phonemes that need work.

Design notes:

* Composite uniqueness on ``(child_id, phoneme, language)`` means we
  *upsert* on every assessment finalisation rather than appending new
  rows — keeping the table O(phoneme inventory) per child.
* ``mastered_at`` is set the first time ``average_score`` crosses the
  :data:`MASTERY_THRESHOLD` so the parent UI can pop a badge exactly
  once and stop showing the phoneme as "needs work".
* The model is fully optional: legacy assessments and tenants that do
  not record phoneme scores simply produce zero rows.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Float,
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


# Score (0.0–1.0) that a phoneme must average over to count as
# "mastered". The threshold is intentionally on the strict side
# (matches the practice-plan generator's exit criteria) so a single
# lucky recording does not flip the badge.
MASTERY_THRESHOLD: float = 0.85


class PhonemeMastery(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Aggregated pronunciation stats for one ``(child, phoneme, language)``."""

    __tablename__ = "phoneme_mastery"
    __table_args__ = (
        UniqueConstraint(
            "child_id",
            "phoneme",
            "language",
            name="uq_phoneme_mastery_child_phoneme_lang",
        ),
    )

    child_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("children.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    phoneme: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    language: Mapped[str] = mapped_column(
        String(8), nullable=False, default="uz", index=True
    )

    # Total recordings that contributed a score for this phoneme. Used
    # both to compute ``average_score`` and to surface confidence
    # ("based on 12 attempts") in the UI.
    total_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # Recordings that scored at or above :data:`MASTERY_THRESHOLD`.
    successful_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # Running average across every attempt — incrementally maintained
    # via the (n*avg + new) / (n+1) formula so we never need to scan
    # historical recordings.
    average_score: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    # Best single-recording score we have ever seen for this phoneme.
    best_score: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )

    last_assessed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    mastered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    child: Mapped[Child] = relationship("Child", lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return (
            f"<PhonemeMastery child={self.child_id} phoneme={self.phoneme} "
            f"avg={self.average_score:.2f}>"
        )


__all__ = ["MASTERY_THRESHOLD", "PhonemeMastery"]
