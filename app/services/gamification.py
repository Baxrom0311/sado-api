"""Gamification service — XP, levels, streaks, and badge unlocks.

The service is the single place that mutates :class:`Gamification`
rows. Endpoints (``/exercises/.../complete``, ``/assessments/.../``)
call into it through small wrapper helpers so business rules stay in
one place and tests can assert on a single API.

Design notes
------------

* **Idempotent.** Awarding XP twice for the same activity is a caller
  concern; the service simply applies the deltas it is told to apply.
* **Level curve.** Cumulative XP for level ``L`` is ``50 * L * (L+1)``
  (100, 300, 600, 1000, 1500, …). The curve is derivative of the
  triangular numbers and feels right for kids: early levels come fast,
  later levels feel earned.
* **Streaks.** Activity on a calendar day extends the streak by 1 if
  the last activity was *yesterday*, keeps it stable if it was today,
  and resets to 1 otherwise.
* **Badges.** Every badge has a ``(requirement_type, threshold)`` pair
  evaluated against the child's stats; a badge is "earned" the first
  time the requirement is met.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.gamification import (
    Badge,
    BadgeEarning,
    BadgeRequirementType,
    Gamification,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------- Levels


def cumulative_xp_for_level(level: int) -> int:
    """Return the total XP needed to *complete* ``level``.

    Level 1 finishes at 100 XP, level 2 at 300, level 3 at 600, etc.
    Levels below 1 are clamped to 0.
    """

    if level < 1:
        return 0
    return 50 * level * (level + 1)


def level_for_xp(total_xp: int) -> int:
    """Return the (1-indexed) level a child has reached at ``total_xp``.

    Solves ``50 * L * (L+1) <= xp`` for the largest ``L``. Always
    returns at least 1.
    """

    if total_xp <= 0:
        return 1
    # Quadratic: 50*L^2 + 50*L - xp <= 0  →  L <= (-50 + sqrt(2500 + 200*xp)) / 100
    discriminant = 2500.0 + 200.0 * float(total_xp)
    raw = (-50.0 + math.sqrt(discriminant)) / 100.0
    level = int(math.floor(raw))
    return max(1, level + 1) if level >= 0 else 1


def xp_progress_for_level(total_xp: int) -> tuple[int, int, int]:
    """Return ``(level, xp_into_level, xp_for_next_level)``.

    ``xp_into_level`` counts how much of the current level the child
    has cleared. ``xp_for_next_level`` is the size of the bar; together
    they let the UI render a percentage with one division.
    """

    level = level_for_xp(total_xp)
    floor_xp = cumulative_xp_for_level(level - 1)
    next_xp = cumulative_xp_for_level(level)
    span = max(1, next_xp - floor_xp)
    into = max(0, total_xp - floor_xp)
    # Cap the visible "into" value at the bar size so a momentary
    # over-shoot never renders a negative remainder.
    return level, min(into, span), span


# --------------------------------------------------------------- State


@dataclass(slots=True)
class XPAwardOutcome:
    """The summary of a single :func:`award_xp` call."""

    gamification: Gamification
    xp_added: int
    leveled_up: bool
    previous_level: int
    new_level: int
    newly_earned_badges: list[Badge]


async def get_or_create(session: AsyncSession, child_id: str) -> Gamification:
    """Fetch the gamification row for ``child_id``, creating it on demand."""

    record = await session.get(Gamification, child_id)
    if record is None:
        record = Gamification(child_id=child_id)
        session.add(record)
        try:
            await session.flush()
        except IntegrityError:
            # Concurrent insert — fall back to the existing row.
            await session.rollback()
            record = await session.get(Gamification, child_id)
            assert record is not None  # noqa: S101 - SQLAlchemy invariant
    return record


def _today_utc() -> date:
    """Return today's calendar date in UTC.

    Wrapped in a helper so tests can monkey-patch when needed.
    """

    return datetime.now(UTC).date()


def _apply_streak(record: Gamification, activity_date: date) -> None:
    """Update streak fields based on a fresh activity date."""

    last = record.last_activity_date
    if last is None:
        record.streak_days = 1
    elif activity_date == last:
        # Already counted today.
        record.streak_days = max(1, record.streak_days)
    elif (activity_date - last).days == 1:
        record.streak_days += 1
    else:
        # Gap of 2+ days resets the streak; the new activity counts as
        # day 1 of the next streak rather than 0.
        record.streak_days = 1
    record.longest_streak = max(record.longest_streak, record.streak_days)
    record.last_activity_date = activity_date


# --------------------------------------------------------------- Badges


async def _list_active_badges(session: AsyncSession) -> list[Badge]:
    stmt = select(Badge).where(Badge.is_active.is_(True))
    return list((await session.execute(stmt)).scalars().all())


async def _earned_badge_ids(session: AsyncSession, child_id: str) -> set[str]:
    stmt = select(BadgeEarning.badge_id).where(BadgeEarning.child_id == child_id)
    rows = (await session.execute(stmt)).scalars().all()
    return set(rows)


def _badge_is_satisfied(badge: Badge, record: Gamification) -> bool:
    """Return ``True`` if ``record`` meets the badge's requirement."""

    rtype = badge.requirement_type
    threshold = badge.threshold
    if rtype == BadgeRequirementType.XP.value:
        return record.total_xp >= threshold
    if rtype == BadgeRequirementType.LEVEL.value:
        return record.level >= threshold
    if rtype == BadgeRequirementType.STREAK.value:
        return record.longest_streak >= threshold
    if rtype == BadgeRequirementType.EXERCISES_COMPLETED.value:
        return record.total_exercises_completed >= threshold
    if rtype == BadgeRequirementType.ASSESSMENTS_COMPLETED.value:
        return record.total_assessments_completed >= threshold
    logger.warning("Unknown badge requirement type: %s", rtype)
    return False


async def _evaluate_badges(
    session: AsyncSession,
    record: Gamification,
    *,
    awarded_at: datetime,
) -> list[Badge]:
    """Insert ``BadgeEarning`` rows for any newly satisfied badges."""

    candidates = await _list_active_badges(session)
    if not candidates:
        return []

    already = await _earned_badge_ids(session, record.child_id)
    newly: list[Badge] = []
    for badge in candidates:
        if badge.id in already:
            continue
        if _badge_is_satisfied(badge, record):
            session.add(
                BadgeEarning(
                    child_id=record.child_id,
                    badge_id=badge.id,
                    earned_at=awarded_at,
                )
            )
            newly.append(badge)
    if newly:
        try:
            await session.flush()
        except IntegrityError:  # pragma: no cover - belt + braces
            await session.rollback()
            return []
    return newly


# ------------------------------------------------------------- Public API


async def award_xp(
    session: AsyncSession,
    child_id: str,
    amount: int,
    *,
    activity_date: date | None = None,
    exercises_completed_delta: int = 0,
    assessments_completed_delta: int = 0,
    reason: str = "",
) -> XPAwardOutcome:
    """Add ``amount`` XP to ``child_id`` and update derived counters.

    Side-effects:

    * creates the gamification row if missing,
    * extends the streak if ``activity_date`` is provided,
    * recomputes :attr:`Gamification.level`,
    * unlocks any badges whose requirement is now met.

    The session is *not* committed — the caller is responsible so the
    XP award stays in the same transaction as whatever business event
    triggered it (e.g. assignment completion).
    """

    if amount < 0:
        raise ValueError("XP amount must be non-negative")

    record = await get_or_create(session, child_id)
    previous_level = record.level

    record.total_xp = max(0, record.total_xp + amount)
    if exercises_completed_delta:
        record.total_exercises_completed = max(
            0, record.total_exercises_completed + exercises_completed_delta
        )
    if assessments_completed_delta:
        record.total_assessments_completed = max(
            0, record.total_assessments_completed + assessments_completed_delta
        )

    if activity_date is not None and amount > 0:
        _apply_streak(record, activity_date)

    record.level = level_for_xp(record.total_xp)
    leveled_up = record.level > previous_level

    awarded_at = datetime.now(UTC)
    newly_earned = await _evaluate_badges(session, record, awarded_at=awarded_at)

    if reason:
        logger.debug(
            "Awarded %s XP to child %s (reason=%s, level %s→%s, badges=%s)",
            amount,
            child_id,
            reason,
            previous_level,
            record.level,
            [b.code for b in newly_earned],
        )

    await session.flush()

    return XPAwardOutcome(
        gamification=record,
        xp_added=amount,
        leveled_up=leveled_up,
        previous_level=previous_level,
        new_level=record.level,
        newly_earned_badges=newly_earned,
    )


# --- XP recipes ---------------------------------------------------------


# Tunable per-action XP rewards. Kept as module-level constants so
# tests + the admin UI can reference them.
XP_PER_EXERCISE_BASE = 10
XP_EXERCISE_SCORE_MAX_BONUS = 10
XP_PER_ASSESSMENT_BASE = 25
XP_ASSESSMENT_GREEN_BONUS = 15
XP_ASSESSMENT_YELLOW_BONUS = 5


def xp_for_exercise_completion(score: float | None) -> int:
    """Return the XP reward for completing an exercise.

    ``score`` is the 0–100 self-reported score; we award the base reward
    plus up to :data:`XP_EXERCISE_SCORE_MAX_BONUS` proportional to the
    score. Missing / out-of-range scores award the base only.
    """

    if score is None:
        return XP_PER_EXERCISE_BASE
    clamped = max(0.0, min(100.0, float(score)))
    bonus = int(round(XP_EXERCISE_SCORE_MAX_BONUS * (clamped / 100.0)))
    return XP_PER_EXERCISE_BASE + bonus


def xp_for_assessment_completion(risk_level: str | None) -> int:
    """Return the XP reward for finishing an assessment session."""

    if risk_level == "green":
        return XP_PER_ASSESSMENT_BASE + XP_ASSESSMENT_GREEN_BONUS
    if risk_level == "yellow":
        return XP_PER_ASSESSMENT_BASE + XP_ASSESSMENT_YELLOW_BONUS
    return XP_PER_ASSESSMENT_BASE


# --- High-level event hooks --------------------------------------------


async def on_exercise_completed(
    session: AsyncSession,
    child_id: str,
    *,
    score: float | None,
    activity_date: date | None = None,
) -> XPAwardOutcome:
    """Hook invoked when an exercise assignment transitions to COMPLETED."""

    return await award_xp(
        session,
        child_id,
        xp_for_exercise_completion(score),
        activity_date=activity_date or _today_utc(),
        exercises_completed_delta=1,
        reason="exercise_completed",
    )


async def on_assessment_completed(
    session: AsyncSession,
    child_id: str,
    *,
    risk_level: str | None,
    activity_date: date | None = None,
) -> XPAwardOutcome:
    """Hook invoked when an assessment transitions to COMPLETED."""

    return await award_xp(
        session,
        child_id,
        xp_for_assessment_completion(risk_level),
        activity_date=activity_date or _today_utc(),
        assessments_completed_delta=1,
        reason="assessment_completed",
    )


# --- Read-side helpers --------------------------------------------------


async def list_earned_badges(
    session: AsyncSession, child_id: str
) -> list[BadgeEarning]:
    """Return badges a child has earned, newest first, with badge eager-loaded."""

    stmt = (
        select(BadgeEarning)
        .where(BadgeEarning.child_id == child_id)
        .order_by(BadgeEarning.earned_at.desc(), BadgeEarning.id.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


async def count_badges(session: AsyncSession, child_id: str) -> int:
    """Cheap count of unlocked badges for a child (used by GamificationPublic)."""

    stmt = select(BadgeEarning.id).where(BadgeEarning.child_id == child_id)
    rows = (await session.execute(stmt)).scalars().all()
    return len(list(rows))


async def reconcile_badges(
    session: AsyncSession, child_id: str
) -> list[Badge]:
    """Award any badge whose requirement is already satisfied.

    Useful after seeding or admin overrides where ``award_xp`` was not
    invoked for the underlying activity. Idempotent.
    """

    record = await get_or_create(session, child_id)
    record.level = level_for_xp(record.total_xp)
    awarded_at = datetime.now(UTC)
    return await _evaluate_badges(session, record, awarded_at=awarded_at)


def filter_inactive(badges: Iterable[Badge]) -> list[Badge]:
    """Return only the active badges from ``badges``.

    Surfaced as a standalone helper so the API layer can re-use the
    same predicate when paginating the catalogue.
    """

    return [b for b in badges if b.is_active]


__all__ = [
    "XP_ASSESSMENT_GREEN_BONUS",
    "XP_ASSESSMENT_YELLOW_BONUS",
    "XP_EXERCISE_SCORE_MAX_BONUS",
    "XP_PER_ASSESSMENT_BASE",
    "XP_PER_EXERCISE_BASE",
    "XPAwardOutcome",
    "award_xp",
    "count_badges",
    "cumulative_xp_for_level",
    "filter_inactive",
    "get_or_create",
    "level_for_xp",
    "list_earned_badges",
    "on_assessment_completed",
    "on_exercise_completed",
    "reconcile_badges",
    "xp_for_assessment_completion",
    "xp_for_exercise_completion",
    "xp_progress_for_level",
]
