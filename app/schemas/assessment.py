"""Pydantic schemas for assessments and audio recordings."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.pagination import Page

PromptStr = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=500)
]


class AssessmentCreate(BaseModel):
    """Payload for ``POST /assessments``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    child_id: str = Field(..., min_length=1, max_length=36)
    type: str = Field(default="screening", max_length=20)


class AssessmentUpdate(BaseModel):
    """Patch payload — currently only status / cancellation."""

    model_config = ConfigDict(str_strip_whitespace=True)

    status: str | None = Field(default=None, max_length=20)
    summary: str | None = Field(default=None, max_length=2000)


class AudioRecordingPublic(BaseModel):
    """Read-side schema for one recording."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    assessment_id: str
    task_type: str
    prompt: str | None
    storage_key: str
    content_type: str
    size_bytes: int
    duration_sec: float | None
    sample_rate: int | None
    processed: bool
    processing_error: str | None
    processed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AssessmentPublic(BaseModel):
    """Read-side schema for an assessment."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    child_id: str
    created_by_id: str | None
    type: str
    status: str
    overall_risk: str | None
    overall_confidence: float | None
    summary: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    recordings: list[AudioRecordingPublic] = Field(default_factory=list)

    @classmethod
    def from_model(cls, assessment: Any) -> AssessmentPublic:
        # ``recordings`` may not be loaded for freshly-created or
        # eager-loaded objects across async session boundaries. Detect
        # that and substitute an empty list rather than triggering an
        # implicit lazy load (which fails under ``MissingGreenlet``).
        from sqlalchemy import inspect as sa_inspect

        try:
            state = sa_inspect(assessment)
            if "recordings" in state.unloaded:
                recordings = []
            else:
                recordings = list(getattr(assessment, "recordings", []) or [])
        except Exception:
            recordings = list(getattr(assessment, "recordings", []) or [])

        return cls(
            id=assessment.id,
            child_id=assessment.child_id,
            created_by_id=assessment.created_by_id,
            type=assessment.type,
            status=assessment.status,
            overall_risk=assessment.overall_risk,
            overall_confidence=assessment.overall_confidence,
            summary=assessment.summary,
            started_at=assessment.started_at,
            completed_at=assessment.completed_at,
            created_at=assessment.created_at,
            updated_at=assessment.updated_at,
            recordings=[
                AudioRecordingPublic.model_validate(r) for r in recordings
            ],
        )


AssessmentPage = Page[AssessmentPublic]


class AnalysisPublic(BaseModel):
    """Result returned from ``GET /analysis/{assessment_id}``."""

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    recording_id: str
    risk_level: str
    confidence: float
    transcript: str | None
    feature_summary: dict[str, Any] | None
    model_name: str
    model_version: str
    created_at: datetime


class AnalysisDetailedPublic(AnalysisPublic):
    """Therapist-only view including raw acoustic features."""

    mfcc_features: dict[str, Any] | None = None
    pitch_data: dict[str, Any] | None = None
    formant_data: dict[str, Any] | None = None
    phoneme_scores: dict[str, Any] | None = None


class AssessmentAnalysisResponse(BaseModel):
    """Top-level analysis payload returned per assessment."""

    assessment_id: str
    overall_risk: str | None
    overall_confidence: float | None
    status: str
    completed_at: datetime | None
    results: list[AnalysisPublic]


class AssessmentDetailedAnalysisResponse(BaseModel):
    """Therapist-grade payload (full features included)."""

    assessment_id: str
    overall_risk: str | None
    overall_confidence: float | None
    status: str
    completed_at: datetime | None
    results: list[AnalysisDetailedPublic]


__all__ = [
    "AnalysisDetailedPublic",
    "AnalysisPublic",
    "AssessmentAnalysisResponse",
    "AssessmentCreate",
    "AssessmentDetailedAnalysisResponse",
    "AssessmentPage",
    "AssessmentPublic",
    "AssessmentUpdate",
    "AudioRecordingPublic",
    "PromptStr",
]
