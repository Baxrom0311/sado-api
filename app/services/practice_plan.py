"""Practice-plan authoring + generation service.

The generator turns the deterministic acoustic analysis on an
:class:`Assessment` into a structured :class:`PracticePlan`. It walks
the recordings, picks the strongest signals (voice-quality flags +
weakest phonemes) and matches each one to the most appropriate
exercise from the catalogue.

Design goals:

* **Backward compatible** — older assessments without
  ``voice_quality`` / ``phoneme_scores`` still produce a sensible
  fallback plan.
* **Deterministic** — given the same assessment and exercise catalogue
  the generator returns the same plan, which keeps tests stable.
* **Locale-aware** — the plan title and item notes are rendered in the
  same locales used by :mod:`app.services.recommendations`.
* **No diagnosis** — every plan is advisory; recommendation language
  mirrors what therapists would suggest as homework, not a clinical
  conclusion.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.assessment import (
    AnalysisResult,
    Assessment,
    AudioRecording,
)
from app.models.child import Child
from app.models.exercise import (
    Exercise,
    ExerciseCategory,
)
from app.models.practice_plan import (
    PracticePlan,
    PracticePlanItem,
    PracticePlanStatus,
)
from app.services.recommendations import DEFAULT_LOCALE, SUPPORTED_LOCALES

logger = logging.getLogger(__name__)


Locale = Literal["uz", "ru", "en"]


# Voice-quality flag → exercise category. Each maps the clinical
# observation onto the homework area most likely to help.
_FLAG_TO_CATEGORY: dict[str, str] = {
    "high_jitter": ExerciseCategory.BREATHING.value,
    "high_shimmer": ExerciseCategory.BREATHING.value,
    "low_hnr": ExerciseCategory.BREATHING.value,
    "slow_speech_rate": ExerciseCategory.FLUENCY.value,
    "fast_speech_rate": ExerciseCategory.FLUENCY.value,
}

# Default priority per flag — lower value = higher priority. Red flags
# (HNR) edge out yellow flags (jitter / shimmer / speech rate).
_FLAG_PRIORITY: dict[str, int] = {
    "low_hnr": 1,
    "high_jitter": 2,
    "high_shimmer": 2,
    "slow_speech_rate": 3,
    "fast_speech_rate": 3,
}

# Default per-item target counts. Plain integers so the mobile app can
# render "0/N" progress bars without doing math.
_DEFAULT_TARGET_COUNT_PHONEME = 5
_DEFAULT_TARGET_COUNT_FLAG = 3
_DEFAULT_TARGET_COUNT_FALLBACK = 3


# Localised title templates for generated plans.
_PLAN_TITLES: dict[str, dict[str, str]] = {
    "uz": {
        "personalised": "{child} uchun mashq rejasi",
        "fallback": "Kundalik nutq mashqlari rejasi",
    },
    "ru": {
        "personalised": "План занятий для {child}",
        "fallback": "Ежедневный план речевых упражнений",
    },
    "en": {
        "personalised": "Practice plan for {child}",
        "fallback": "Daily speech-practice plan",
    },
}

# Localised item-note templates. ``{phoneme}`` / ``{flag}`` placeholders
# are filled from the matched signal.
_ITEM_NOTES: dict[str, dict[str, str]] = {
    "uz": {
        "phoneme": "“{phoneme}” tovushi mashqi.",
        "flag_high_jitter": "Ovoz titrog‘ini kamaytirish uchun mashq.",
        "flag_high_shimmer": "Ovoz balandligini barqarorlashtirish uchun mashq.",
        "flag_low_hnr": "Ovoz tiniqligini yaxshilash uchun nafas mashqi.",
        "flag_slow_speech_rate": "Nutq tezligini oshirish uchun mashq.",
        "flag_fast_speech_rate": "Nutqni sekinlashtirib, aniq talaffuz qilish mashqi.",
        "fallback": "Kundalik nutq mashqi.",
    },
    "ru": {
        "phoneme": "Упражнение на звук «{phoneme}».",
        "flag_high_jitter": "Упражнение для уменьшения дрожания голоса.",
        "flag_high_shimmer": "Упражнение для стабилизации громкости голоса.",
        "flag_low_hnr": "Дыхательное упражнение для чистоты голоса.",
        "flag_slow_speech_rate": "Упражнение для увеличения темпа речи.",
        "flag_fast_speech_rate": "Упражнение для замедления и чёткости речи.",
        "fallback": "Ежедневное речевое упражнение.",
    },
    "en": {
        "phoneme": "Drill for the '{phoneme}' sound.",
        "flag_high_jitter": "Drill to reduce pitch jitter.",
        "flag_high_shimmer": "Drill to stabilise loudness.",
        "flag_low_hnr": "Breathing drill to improve voice clarity.",
        "flag_slow_speech_rate": "Drill to increase speaking rate.",
        "flag_fast_speech_rate": "Drill to slow down and articulate clearly.",
        "fallback": "Daily speech practice drill.",
    },
}


def _normalise_locale(locale: str | None) -> Locale:
    """Match :func:`recommendations._normalize_locale` so the two stay aligned."""

    if not locale:
        return DEFAULT_LOCALE
    candidate = locale.lower().split("-")[0]
    if candidate in SUPPORTED_LOCALES:
        return candidate  # type: ignore[return-value]
    return DEFAULT_LOCALE


def _localised_title(child_name: str | None, locale: Locale) -> str:
    titles = _PLAN_TITLES.get(locale, _PLAN_TITLES[DEFAULT_LOCALE])
    name = (child_name or "").strip()
    if name:
        return titles["personalised"].format(child=name)
    return titles["fallback"]


def _localised_phoneme_note(phoneme: str, locale: Locale) -> str:
    notes = _ITEM_NOTES.get(locale, _ITEM_NOTES[DEFAULT_LOCALE])
    return notes["phoneme"].format(phoneme=phoneme)


def _localised_flag_note(flag: str, locale: Locale) -> str:
    notes = _ITEM_NOTES.get(locale, _ITEM_NOTES[DEFAULT_LOCALE])
    key = f"flag_{flag}"
    return notes.get(key, notes["fallback"])


def _localised_fallback_note(locale: Locale) -> str:
    notes = _ITEM_NOTES.get(locale, _ITEM_NOTES[DEFAULT_LOCALE])
    return notes["fallback"]


# --------------------------------------------------------------- Helpers


def _collect_voice_quality_flags(analyses: list[AnalysisResult]) -> list[str]:
    """Return distinct VQ flag codes across an assessment's analyses."""

    flags: list[str] = []
    seen: set[str] = set()
    for analysis in analyses:
        vq = analysis.voice_quality or {}
        if not isinstance(vq, dict):
            continue
        raw = vq.get("flags") or []
        if not isinstance(raw, list):
            continue
        for flag in raw:
            flag_str = str(flag).strip()
            if flag_str and flag_str not in seen:
                seen.add(flag_str)
                flags.append(flag_str)
    return flags


def _collect_weakest_phonemes(
    analyses: list[AnalysisResult], *, limit: int = 3
) -> list[dict[str, Any]]:
    """Return up to ``limit`` weakest phonemes from the worst recording.

    Mirrors the algorithm used by ``children.py`` so the practice plan
    targets the same phonemes the progress timeline highlights.
    """

    best_candidate: tuple[float, list[dict[str, Any]]] | None = None
    for analysis in analyses:
        scores_payload = analysis.phoneme_scores or {}
        if not isinstance(scores_payload, dict):
            continue
        scores = scores_payload.get("scores")
        weakest = scores_payload.get("weakest")
        if not isinstance(scores, dict) or not scores:
            continue
        numeric_scores = [
            float(v) for v in scores.values() if isinstance(v, int | float)
        ]
        if not numeric_scores:
            continue
        mean_score = sum(numeric_scores) / len(numeric_scores)
        if best_candidate is None or mean_score < best_candidate[0]:
            cleaned: list[dict[str, Any]] = []
            if isinstance(weakest, list):
                for item in weakest[:limit]:
                    if isinstance(item, dict) and "phoneme" in item:
                        cleaned.append(
                            {
                                "phoneme": str(item["phoneme"]),
                                "score": float(item.get("score") or 0.0),
                            }
                        )
            best_candidate = (mean_score, cleaned)
    return best_candidate[1] if best_candidate else []


async def _select_exercise_for_phoneme(
    session: AsyncSession,
    phoneme: str,
    *,
    tenant_id: str | None,
) -> Exercise | None:
    """Pick a deterministic exercise targeting ``phoneme``.

    Search order:

    1. Active exercises whose ``target_phonemes`` contains ``phoneme``,
       prefer the caller's tenant, fall back to global (``tenant_id IS NULL``).
    2. Any active articulation exercise (tenant first, then global).

    Within each tier we sort by ``(title, id)`` so the choice is stable.
    """

    needle = f"%{phoneme.strip()}%"

    base_filters = [
        Exercise.is_active.is_(True),
        Exercise.target_phonemes.is_not(None),
        Exercise.target_phonemes.ilike(needle),
    ]

    # Tier 1a — tenant-owned + phoneme match.
    if tenant_id is not None:
        stmt = (
            select(Exercise)
            .where(*base_filters, Exercise.tenant_id == tenant_id)
            .order_by(Exercise.title.asc(), Exercise.id.asc())
            .limit(1)
        )
        result = await session.execute(stmt)
        candidate = result.scalar_one_or_none()
        if candidate is not None:
            return candidate

    # Tier 1b — global + phoneme match.
    stmt = (
        select(Exercise)
        .where(*base_filters, Exercise.tenant_id.is_(None))
        .order_by(Exercise.title.asc(), Exercise.id.asc())
        .limit(1)
    )
    result = await session.execute(stmt)
    candidate = result.scalar_one_or_none()
    if candidate is not None:
        return candidate

    # Tier 2 — fallback to any articulation exercise visible to this tenant.
    return await _select_exercise_for_category(
        session,
        ExerciseCategory.ARTICULATION.value,
        tenant_id=tenant_id,
    )


async def _select_exercise_for_category(
    session: AsyncSession,
    category: str,
    *,
    tenant_id: str | None,
    exclude_ids: Iterable[str] | None = None,
) -> Exercise | None:
    """Pick a deterministic exercise for a category (tenant-aware)."""

    excluded = set(exclude_ids or ())

    # Tier 1 — tenant-owned.
    if tenant_id is not None:
        stmt = (
            select(Exercise)
            .where(
                Exercise.is_active.is_(True),
                Exercise.category == category,
                Exercise.tenant_id == tenant_id,
            )
            .order_by(Exercise.title.asc(), Exercise.id.asc())
        )
        result = await session.execute(stmt)
        for candidate in result.scalars().all():
            if candidate.id not in excluded:
                return candidate

    # Tier 2 — global.
    stmt = (
        select(Exercise)
        .where(
            Exercise.is_active.is_(True),
            Exercise.category == category,
            Exercise.tenant_id.is_(None),
        )
        .order_by(Exercise.title.asc(), Exercise.id.asc())
    )
    result = await session.execute(stmt)
    for candidate in result.scalars().all():
        if candidate.id not in excluded:
            return candidate

    return None


async def _select_fallback_exercise(
    session: AsyncSession,
    *,
    tenant_id: str | None,
    exclude_ids: Iterable[str] | None = None,
) -> Exercise | None:
    """Pick *any* active exercise so an empty-signal plan is never empty."""

    excluded = set(exclude_ids or ())

    stmt = (
        select(Exercise)
        .where(Exercise.is_active.is_(True))
        .where(
            or_(
                Exercise.tenant_id == tenant_id,
                Exercise.tenant_id.is_(None),
            )
        )
        .order_by(Exercise.title.asc(), Exercise.id.asc())
    )
    result = await session.execute(stmt)
    for candidate in result.scalars().all():
        if candidate.id not in excluded:
            return candidate
    return None


async def _load_assessment_with_analyses(
    session: AsyncSession, assessment_id: str
) -> Assessment | None:
    stmt = (
        select(Assessment)
        .where(Assessment.id == assessment_id)
        .options(
            selectinload(Assessment.recordings).selectinload(
                AudioRecording.analysis
            ),
            selectinload(Assessment.child),
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# --------------------------------------------------------------- Public API


async def generate_plan_from_assessment(
    session: AsyncSession,
    *,
    assessment_id: str,
    locale: str | None = None,
    max_items: int = 5,
    activate: bool = False,
    title_override: str | None = None,
    created_by_id: str | None = None,
) -> PracticePlan:
    """Build a :class:`PracticePlan` from an assessment's ML output.

    The returned plan is **persisted** (committed) before being
    returned so downstream callers can refresh / re-query without
    additional plumbing. Caller is responsible for authorisation;
    this function trusts that the ``created_by_id`` user can read the
    assessment.

    Raises :class:`ValueError` when ``assessment_id`` does not exist.
    """

    assessment = await _load_assessment_with_analyses(session, assessment_id)
    if assessment is None:
        raise ValueError(f"Assessment {assessment_id} not found")

    locale_resolved = _normalise_locale(locale)
    child = assessment.child

    analyses: list[AnalysisResult] = [
        r.analysis for r in assessment.recordings if r.analysis is not None
    ]

    flags = _collect_voice_quality_flags(analyses)
    weakest_phonemes = _collect_weakest_phonemes(analyses, limit=max_items)

    # Tenant scope for exercise lookup follows the child's tenant.
    tenant_id = child.tenant_id

    items_to_create: list[tuple[Exercise, dict[str, Any]]] = []
    focus_codes: list[str] = []
    used_exercise_ids: set[str] = set()

    # ---- Phoneme-driven items (highest signal) ---------------------
    for entry in weakest_phonemes:
        if len(items_to_create) >= max_items:
            break
        phoneme = entry.get("phoneme")
        if not phoneme:
            continue
        exercise = await _select_exercise_for_phoneme(
            session, phoneme, tenant_id=tenant_id
        )
        if exercise is None or exercise.id in used_exercise_ids:
            continue
        used_exercise_ids.add(exercise.id)
        focus_code = f"phoneme:{phoneme}"
        focus_codes.append(focus_code)
        items_to_create.append(
            (
                exercise,
                {
                    "focus_code": focus_code,
                    "priority": 1,
                    "target_count": _DEFAULT_TARGET_COUNT_PHONEME,
                    "notes": _localised_phoneme_note(phoneme, locale_resolved),
                    "metadata": {
                        "matched_phoneme": phoneme,
                        "phoneme_score": entry.get("score"),
                    },
                },
            )
        )

    # ---- Voice-quality flag items ----------------------------------
    for flag in flags:
        if len(items_to_create) >= max_items:
            break
        category = _FLAG_TO_CATEGORY.get(flag)
        if not category:
            continue
        exercise = await _select_exercise_for_category(
            session,
            category,
            tenant_id=tenant_id,
            exclude_ids=used_exercise_ids,
        )
        if exercise is None:
            continue
        used_exercise_ids.add(exercise.id)
        focus_code = f"voice_quality:{flag}"
        focus_codes.append(focus_code)
        items_to_create.append(
            (
                exercise,
                {
                    "focus_code": focus_code,
                    "priority": _FLAG_PRIORITY.get(flag, 3),
                    "target_count": _DEFAULT_TARGET_COUNT_FLAG,
                    "notes": _localised_flag_note(flag, locale_resolved),
                    "metadata": {"matched_flag": flag},
                },
            )
        )

    # ---- Guarantee at least one fallback item ----------------------
    if not items_to_create:
        fallback = await _select_fallback_exercise(
            session, tenant_id=tenant_id, exclude_ids=used_exercise_ids
        )
        if fallback is not None:
            used_exercise_ids.add(fallback.id)
            focus_codes.append("general:practice")
            items_to_create.append(
                (
                    fallback,
                    {
                        "focus_code": "general:practice",
                        "priority": 3,
                        "target_count": _DEFAULT_TARGET_COUNT_FALLBACK,
                        "notes": _localised_fallback_note(locale_resolved),
                        "metadata": {"reason": "fallback"},
                    },
                )
            )

    title = title_override or _localised_title(child.name, locale_resolved)
    status = (
        PracticePlanStatus.ACTIVE.value
        if activate
        else PracticePlanStatus.DRAFT.value
    )

    plan = PracticePlan(
        child_id=child.id,
        assessment_id=assessment.id,
        created_by_id=created_by_id,
        tenant_id=tenant_id,
        title=title,
        status=status,
        locale=locale_resolved,
        focus_areas=focus_codes or None,
    )
    session.add(plan)
    await session.flush()  # populate plan.id for FK on items

    for exercise, item_data in items_to_create:
        item = PracticePlanItem(
            plan_id=plan.id,
            exercise_id=exercise.id,
            priority=item_data["priority"],
            target_count=item_data["target_count"],
            focus_code=item_data["focus_code"],
            notes=item_data["notes"],
            metadata_json=item_data["metadata"],
        )
        session.add(item)

    await session.commit()
    await session.refresh(plan)
    return plan


async def load_plan_with_items(
    session: AsyncSession, plan_id: str
) -> PracticePlan | None:
    """Eager-load a plan plus its items + exercise summaries."""

    stmt = (
        select(PracticePlan)
        .where(PracticePlan.id == plan_id)
        .options(
            selectinload(PracticePlan.items).selectinload(
                PracticePlanItem.exercise
            ),
            selectinload(PracticePlan.child).selectinload(Child.kindergarten),
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def serialize_item(item: PracticePlanItem) -> dict[str, Any]:
    """Helper that mirrors :class:`PracticePlanItemPublic`'s shape.

    We render here rather than relying on Pydantic's ``from_attributes``
    because the embedded ``exercise_*`` fields require a join we hand-shape.
    """

    exercise = item.exercise
    return {
        "id": item.id,
        "plan_id": item.plan_id,
        "exercise_id": item.exercise_id,
        "status": item.status,
        "priority": item.priority,
        "target_count": item.target_count,
        "completed_count": item.completed_count,
        "focus_code": item.focus_code,
        "notes": item.notes,
        "completed_at": item.completed_at,
        "metadata_json": item.metadata_json,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "exercise_title": exercise.title if exercise else None,
        "exercise_category": exercise.category if exercise else None,
        "exercise_difficulty": exercise.difficulty if exercise else None,
    }


def serialize_plan(
    plan: PracticePlan, *, include_items: bool = False
) -> dict[str, Any]:
    """Render a plan into a JSON-friendly dict (with optional items)."""

    items = list(plan.items) if plan.items is not None else []
    payload: dict[str, Any] = {
        "id": plan.id,
        "child_id": plan.child_id,
        "assessment_id": plan.assessment_id,
        "created_by_id": plan.created_by_id,
        "tenant_id": plan.tenant_id,
        "title": plan.title,
        "summary": plan.summary,
        "status": plan.status,
        "locale": plan.locale,
        "focus_areas": list(plan.focus_areas) if plan.focus_areas else None,
        "start_date": plan.start_date,
        "end_date": plan.end_date,
        "completed_at": plan.completed_at,
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
        "item_count": len(items),
        "completed_item_count": sum(
            1 for i in items if i.status == "completed"
        ),
    }
    if include_items:
        payload["items"] = [serialize_item(i) for i in items]
    return payload


__all__ = [
    "generate_plan_from_assessment",
    "load_plan_with_items",
    "serialize_item",
    "serialize_plan",
]
