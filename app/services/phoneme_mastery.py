"""Phoneme-mastery aggregation service.

Updates :class:`app.models.phoneme_mastery.PhonemeMastery` rows from
the per-recording phoneme scores produced by the speech analyser.

The public entry point :func:`update_mastery_from_assessment` is
called from :mod:`app.services.audio_processor` after an assessment
finalises. It is wrapped in a defensive try/except by the caller so a
mastery-update failure can never break finalisation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assessment import AnalysisResult, AudioRecording
from app.models.child import Child
from app.models.phoneme_mastery import MASTERY_THRESHOLD, PhonemeMastery

logger = logging.getLogger(__name__)


def _coerce_score(raw: Any) -> float | None:
    """Coerce a raw phoneme score into a float in ``[0.0, 1.0]``.

    Returns ``None`` for non-numeric or out-of-range values so the
    caller can skip them without raising. Scores >1 are clamped (some
    pipelines emit 0–100); scores <0 are dropped (likely corruption).
    """

    if isinstance(raw, bool):
        # ``bool`` is a subtype of ``int`` in Python; treat it as junk.
        return None
    if not isinstance(raw, int | float):
        return None
    value = float(raw)
    if value < 0.0:
        return None
    if value > 1.0:
        # Tolerate 0–100 percentage scales transparently.
        if value <= 100.0:
            value = value / 100.0
        else:
            return None
    return value


def _extract_phoneme_scores(
    payload: dict[str, Any] | None,
) -> dict[str, float]:
    """Return ``{phoneme: score}`` from an :class:`AnalysisResult` payload.

    The analyser stores the canonical ``scores`` dict alongside
    ``weakest`` / ``strongest`` lists. We accept both shapes plus the
    legacy ``{phoneme: score}`` flat map for forward / backward compat.
    """

    if not isinstance(payload, dict):
        return {}

    scores: dict[str, float] = {}

    raw_scores = payload.get("scores")
    if isinstance(raw_scores, dict):
        for phoneme, value in raw_scores.items():
            score = _coerce_score(value)
            if score is None or not isinstance(phoneme, str) or not phoneme:
                continue
            scores[phoneme] = score
        if scores:
            return scores

    # Fallback: payload itself is ``{phoneme: score}`` (older format).
    for phoneme, value in payload.items():
        if phoneme in {"scores", "weakest", "strongest"}:
            continue
        score = _coerce_score(value)
        if score is None or not isinstance(phoneme, str) or not phoneme:
            continue
        scores[phoneme] = score
    return scores


async def _load_or_create(
    session: AsyncSession,
    *,
    child_id: str,
    phoneme: str,
    language: str,
) -> PhonemeMastery:
    """Return the mastery row for ``(child, phoneme, language)``, creating it on miss."""

    stmt = (
        select(PhonemeMastery)
        .where(PhonemeMastery.child_id == child_id)
        .where(PhonemeMastery.phoneme == phoneme)
        .where(PhonemeMastery.language == language)
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        row = PhonemeMastery(
            child_id=child_id,
            phoneme=phoneme,
            language=language,
            total_attempts=0,
            successful_attempts=0,
            average_score=0.0,
            best_score=0.0,
        )
        session.add(row)
    return row


def _apply_score(
    row: PhonemeMastery, score: float, *, when: datetime
) -> None:
    """Fold a single attempt into the running aggregates on ``row``."""

    n = row.total_attempts
    row.average_score = (row.average_score * n + score) / (n + 1)
    row.total_attempts = n + 1
    if score >= MASTERY_THRESHOLD:
        row.successful_attempts += 1
    if score > row.best_score:
        row.best_score = score
    row.last_assessed_at = when
    if (
        row.mastered_at is None
        and row.average_score >= MASTERY_THRESHOLD
        and row.total_attempts >= 2
    ):
        # Require at least two attempts before declaring mastery so
        # the badge does not fire on a single high-scoring recording.
        row.mastered_at = when


async def update_mastery_from_assessment(
    session: AsyncSession,
    *,
    assessment_id: str,
) -> int:
    """Roll an assessment's phoneme scores into per-child mastery rows.

    Walks every :class:`AnalysisResult` attached to the assessment,
    coerces / normalises the per-recording phoneme scores, and applies
    them to (or upserts) the child's :class:`PhonemeMastery` rows.

    Returns the number of mastery rows touched (created or updated).
    Idempotent within one transaction — if the same assessment is
    re-finalised the caller is responsible for not calling this twice.
    The caller (:mod:`app.services.audio_processor`) guards on the
    "was already completed" flag, which is sufficient.
    """

    stmt = (
        select(AudioRecording)
        .where(AudioRecording.assessment_id == assessment_id)
    )
    result = await session.execute(stmt)
    recordings = list(result.scalars().all())
    if not recordings:
        return 0

    recording_ids = [r.id for r in recordings]
    analyses_q = await session.execute(
        select(AnalysisResult).where(
            AnalysisResult.recording_id.in_(recording_ids)
        )
    )
    analyses = list(analyses_q.scalars().all())
    if not analyses:
        return 0

    # All recordings share one child / language. Load the assessment
    # and child to discover the language used for these scores.
    from app.models.assessment import Assessment

    assessment = await session.get(Assessment, assessment_id)
    if assessment is None:
        return 0
    child = await session.get(Child, assessment.child_id)
    if child is None:
        return 0
    language = (child.language or "uz").lower()

    when = datetime.now(UTC)
    touched: set[tuple[str, str]] = set()

    for analysis in analyses:
        scores = _extract_phoneme_scores(analysis.phoneme_scores)
        if not scores:
            continue
        for phoneme, score in scores.items():
            normalised = phoneme.strip().lower()
            if not normalised:
                continue
            row = await _load_or_create(
                session,
                child_id=child.id,
                phoneme=normalised,
                language=language,
            )
            _apply_score(row, score, when=when)
            touched.add((normalised, language))

    if touched:
        await session.flush()

    return len(touched)


async def get_mastery_rows(
    session: AsyncSession,
    *,
    child_id: str,
    language: str | None = None,
) -> list[PhonemeMastery]:
    """Return mastery rows for ``child_id``, optionally filtered by language.

    Sorted by ``average_score`` ascending so callers (UI, weakest-first
    heuristics) get a stable order without re-sorting.
    """

    stmt = select(PhonemeMastery).where(PhonemeMastery.child_id == child_id)
    if language:
        stmt = stmt.where(PhonemeMastery.language == language.lower())
    stmt = stmt.order_by(
        PhonemeMastery.average_score.asc(), PhonemeMastery.phoneme.asc()
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


__all__ = [
    "MASTERY_THRESHOLD",
    "get_mastery_rows",
    "update_mastery_from_assessment",
]
