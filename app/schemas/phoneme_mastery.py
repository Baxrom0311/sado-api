"""Schemas for phoneme mastery + speech profile endpoints.

These payloads power the radar / heatmap charts on the mobile app.
They are intentionally compact (primitives + short lists) so the
parent UI can render them without post-processing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.phoneme_mastery import MASTERY_THRESHOLD, PhonemeMastery


class PhonemeMasteryPublic(BaseModel):
    """One ``(phoneme, language)`` row for a child."""

    model_config = ConfigDict(from_attributes=True)

    phoneme: str
    language: str
    total_attempts: int
    successful_attempts: int
    average_score: float = Field(ge=0.0, le=1.0)
    best_score: float = Field(ge=0.0, le=1.0)
    last_assessed_at: datetime | None = None
    mastered_at: datetime | None = None
    is_mastered: bool = Field(
        default=False,
        description=(
            "True when ``average_score`` is at or above the mastery "
            "threshold. Mirrors ``mastered_at is not None`` but is "
            "more convenient for filtering on the client."
        ),
    )

    @classmethod
    def from_model(cls, model: PhonemeMastery) -> PhonemeMasteryPublic:
        """Project an ORM row into the public payload shape."""

        return cls(
            phoneme=model.phoneme,
            language=model.language,
            total_attempts=model.total_attempts,
            successful_attempts=model.successful_attempts,
            average_score=round(model.average_score, 4),
            best_score=round(model.best_score, 4),
            last_assessed_at=model.last_assessed_at,
            mastered_at=model.mastered_at,
            is_mastered=model.mastered_at is not None,
        )


class PhonemeMasterySummary(BaseModel):
    """High-level rollup for the radar-chart header."""

    total_phonemes: int = 0
    mastered_phonemes: int = 0
    average_score: float = Field(default=0.0, ge=0.0, le=1.0)
    last_assessed_at: datetime | None = None
    mastery_threshold: float = Field(
        default=MASTERY_THRESHOLD,
        description="Score at or above which a phoneme is considered mastered.",
    )


class PhonemeMasteryResponse(BaseModel):
    """Payload for ``GET /children/{child_id}/phoneme-mastery``."""

    child_id: str
    summary: PhonemeMasterySummary
    items: list[PhonemeMasteryPublic] = Field(default_factory=list)


class SpeechProfileResponse(BaseModel):
    """Aggregated speech profile combining mastery + voice-quality trends.

    Used by ``GET /children/{child_id}/speech-profile`` to drive the
    "Speech overview" tab. Kept on its own schema so we can iterate
    without breaking the simpler phoneme-mastery endpoint.
    """

    child_id: str
    summary: PhonemeMasterySummary

    # Up to three weakest phonemes (lowest average score) — what the
    # mobile app should suggest practising next.
    weakest_phonemes: list[PhonemeMasteryPublic] = Field(default_factory=list)
    # Up to three strongest phonemes — for the "great work!" badge row.
    strongest_phonemes: list[PhonemeMasteryPublic] = Field(default_factory=list)
    # Phonemes whose ``mastered_at`` is set, sorted by most recent
    # mastery date. Capped client-side for the "Mastered" carousel.
    mastered_phonemes: list[PhonemeMasteryPublic] = Field(default_factory=list)

    # Latest voice-quality snapshot (jitter / shimmer / HNR / speech
    # rate + flag list) so the speech-profile screen can render trend
    # badges without a second request.
    latest_voice_quality: dict[str, Any] | None = None
    # Latest overall risk for context.
    latest_risk: str | None = None
    latest_assessment_completed_at: datetime | None = None


__all__ = [
    "PhonemeMasteryPublic",
    "PhonemeMasteryResponse",
    "PhonemeMasterySummary",
    "SpeechProfileResponse",
]
