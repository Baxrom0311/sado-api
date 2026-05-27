"""High-level orchestration for processing one audio recording.

Runs the full pipeline:

1. fetch the bytes from object storage,
2. extract acoustic + linguistic features (mock Whisper + librosa),
3. predict risk via the mock XGBoost scorer,
4. persist an :class:`AnalysisResult`,
5. update the parent :class:`Assessment` aggregate state.

This module is invoked synchronously from the assessment endpoint
(when Celery is in eager mode for tests / single-node dev) and from
``app.workers.tasks`` when Celery workers are running.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assessment import (
    AnalysisResult,
    Assessment,
    AssessmentStatus,
    AudioRecording,
)
from app.services.ml_scorer import aggregate_risk, predict_risk
from app.services.speech_analyzer import extract_features
from app.services.storage import get_audio_storage

logger = logging.getLogger(__name__)


async def process_recording(
    session: AsyncSession,
    recording_id: str,
) -> AnalysisResult | None:
    """Run STT + ML over one recording and persist the result.

    Returns the resulting :class:`AnalysisResult`, or ``None`` if the
    recording is missing (which can happen if the worker fires after a
    deletion). Errors are persisted on the recording so the assessment
    can be retried or failed cleanly.
    """

    recording = await session.get(AudioRecording, recording_id)
    if recording is None:
        logger.warning("Recording %s not found — skipping", recording_id)
        return None

    storage = get_audio_storage()
    try:
        audio_bytes = await storage.get_object(recording.storage_key)
    except Exception as exc:
        logger.exception("Failed to fetch audio %s", recording.storage_key)
        recording.processed = False
        recording.processing_error = f"storage_error: {exc}"
        await session.commit()
        await _maybe_finalize_assessment(session, recording.assessment_id)
        return None

    try:
        features = extract_features(
            audio_bytes,
            declared_duration_sec=recording.duration_sec,
        )
    except Exception as exc:
        logger.exception("Feature extraction failed for %s", recording_id)
        recording.processed = False
        recording.processing_error = f"feature_error: {exc}"
        await session.commit()
        await _maybe_finalize_assessment(session, recording.assessment_id)
        return None

    prediction = predict_risk(features)

    # Upsert analysis (delete-then-insert keeps the unique constraint clean).
    existing = await session.execute(
        select(AnalysisResult).where(AnalysisResult.recording_id == recording.id)
    )
    if (current := existing.scalar_one_or_none()) is not None:
        await session.delete(current)
        await session.flush()

    analysis = AnalysisResult(
        recording_id=recording.id,
        risk_level=prediction.risk_level,
        confidence=prediction.confidence,
        transcript=features.transcript,
        mfcc_features=features.mfcc_features,
        pitch_data=features.pitch_data,
        formant_data=features.formant_data,
        phoneme_scores=features.phoneme_scores,
        feature_summary={
            **features.feature_summary,
            "rationale": prediction.rationale,
        },
        model_name=prediction.model_name,
        model_version=prediction.model_version,
    )
    session.add(analysis)

    recording.processed = True
    recording.processing_error = None
    recording.processed_at = datetime.now(UTC)
    if recording.duration_sec is None:
        recording.duration_sec = features.duration_sec
    if recording.sample_rate is None:
        recording.sample_rate = features.sample_rate

    await session.commit()
    await session.refresh(analysis)

    await _maybe_finalize_assessment(session, recording.assessment_id)
    return analysis


async def _maybe_finalize_assessment(
    session: AsyncSession, assessment_id: str
) -> None:
    """Set assessment status + aggregate risk once every recording is done."""

    assessment = await session.get(Assessment, assessment_id)
    if assessment is None:
        return

    result = await session.execute(
        select(AudioRecording).where(AudioRecording.assessment_id == assessment_id)
    )
    recordings = list(result.scalars().all())
    if not recordings:
        return

    if any(r.processing_error for r in recordings):
        assessment.status = AssessmentStatus.FAILED.value
        await session.commit()
        return

    if not all(r.processed for r in recordings):
        assessment.status = AssessmentStatus.PROCESSING.value
        await session.commit()
        return

    # All processed — gather predictions to aggregate.
    analyses_q = await session.execute(
        select(AnalysisResult).where(
            AnalysisResult.recording_id.in_([r.id for r in recordings])
        )
    )
    analyses = list(analyses_q.scalars().all())

    from app.services.ml_scorer import RiskPrediction

    predictions = [
        RiskPrediction(
            risk_level=a.risk_level,
            confidence=a.confidence,
            model_name=a.model_name,
            model_version=a.model_version,
            rationale=(a.feature_summary or {}).get("rationale", {}),
        )
        for a in analyses
    ]
    aggregate = aggregate_risk(predictions)

    assessment.status = AssessmentStatus.COMPLETED.value
    assessment.overall_risk = aggregate.risk_level
    assessment.overall_confidence = aggregate.confidence
    assessment.completed_at = datetime.now(UTC)
    assessment.summary = (
        f"Aggregated risk={aggregate.risk_level} from {len(analyses)} recording(s)."
    )
    await session.commit()


__all__ = ["process_recording"]
