"""Schemas for the child-progress timeline.

The progress endpoint surfaces a flat, chronological list of analysis
data points so the mobile app can plot trend charts (risk over time,
voice-quality trajectories, weakest phonemes) without re-running ML.

The shape is intentionally compact — every field is a primitive or a
short list, suitable for chart libraries.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProgressPoint(BaseModel):
    """One assessment-level data point for trend charts."""

    model_config = ConfigDict(from_attributes=True)

    assessment_id: str
    assessment_type: str
    completed_at: datetime | None = Field(
        default=None,
        description=(
            "When the assessment finished processing. ``None`` for "
            "in-flight or failed assessments — sort callers by ``created_at``."
        ),
    )
    created_at: datetime
    status: str
    overall_risk: str | None = None
    overall_confidence: float | None = None

    # Aggregated voice-quality across the assessment's recordings (the
    # mean of the per-recording values). All four fields default to
    # ``None`` when no recording produced voice quality data, so the
    # mobile chart can simply skip the point.
    jitter_local_pct: float | None = None
    shimmer_local_pct: float | None = None
    hnr_db: float | None = None
    speech_rate_wpm: float | None = None

    # Distinct voice-quality flag codes raised across all recordings
    # in this assessment. Useful for severity badges in the timeline.
    voice_quality_flags: list[str] = Field(default_factory=list)

    # Three weakest phonemes by score, taken from the recording with
    # the lowest mean phoneme score. Each entry is ``{"phoneme": str,
    # "score": float}``.
    weakest_phonemes: list[dict[str, Any]] = Field(default_factory=list)

    # Number of recordings that contributed to this point — let the
    # client decide whether to grey out single-clip data points.
    recording_count: int = 0


class ChildProgressSummary(BaseModel):
    """High-level rollup so callers can render a heading without iterating."""

    total_assessments: int = 0
    completed_assessments: int = 0
    last_completed_at: datetime | None = None
    risk_distribution: dict[str, int] = Field(
        default_factory=dict,
        description="Counts keyed by ``green`` / ``yellow`` / ``red``.",
    )
    # Convenience metrics for the most recent completed assessment.
    latest_risk: str | None = None
    latest_confidence: float | None = None


class ChildProgressResponse(BaseModel):
    """Top-level payload for ``GET /children/{child_id}/progress``."""

    child_id: str
    summary: ChildProgressSummary
    points: list[ProgressPoint]


__all__ = [
    "ChildProgressResponse",
    "ChildProgressSummary",
    "ProgressPoint",
]
