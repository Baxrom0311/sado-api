"""Unit tests for the gamification service (XP curve, streaks, badges)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services import gamification as gam

# --------------------------------------------------------- XP curve


def test_cumulative_xp_for_level_known_anchors() -> None:
    """``50 * L * (L+1)`` is the cumulative XP needed to clear level L."""

    assert gam.cumulative_xp_for_level(0) == 0
    assert gam.cumulative_xp_for_level(1) == 100
    assert gam.cumulative_xp_for_level(2) == 300
    assert gam.cumulative_xp_for_level(3) == 600
    assert gam.cumulative_xp_for_level(4) == 1000
    assert gam.cumulative_xp_for_level(5) == 1500


def test_cumulative_xp_for_level_clamps_negative() -> None:
    assert gam.cumulative_xp_for_level(-3) == 0


def test_level_for_xp_boundaries() -> None:
    """A child at exactly ``cumulative_xp_for_level(L)`` is on level L+1."""

    assert gam.level_for_xp(0) == 1
    assert gam.level_for_xp(50) == 1
    assert gam.level_for_xp(99) == 1
    assert gam.level_for_xp(100) == 2
    assert gam.level_for_xp(299) == 2
    assert gam.level_for_xp(300) == 3
    assert gam.level_for_xp(1000) == 5


def test_xp_progress_for_level_breakdown() -> None:
    level, into, span = gam.xp_progress_for_level(150)
    assert level == 2
    # Level 2 spans 100→300, child has 50 xp into it.
    assert into == 50
    assert span == 200

    level, into, span = gam.xp_progress_for_level(0)
    assert level == 1
    assert into == 0
    assert span == 100


# --------------------------------------------------------- XP recipes


def test_xp_for_exercise_completion_no_score() -> None:
    assert gam.xp_for_exercise_completion(None) == gam.XP_PER_EXERCISE_BASE


def test_xp_for_exercise_completion_perfect_score() -> None:
    assert (
        gam.xp_for_exercise_completion(100.0)
        == gam.XP_PER_EXERCISE_BASE + gam.XP_EXERCISE_SCORE_MAX_BONUS
    )


def test_xp_for_exercise_completion_clamps_out_of_range() -> None:
    assert gam.xp_for_exercise_completion(500.0) == (
        gam.XP_PER_EXERCISE_BASE + gam.XP_EXERCISE_SCORE_MAX_BONUS
    )
    assert gam.xp_for_exercise_completion(-10.0) == gam.XP_PER_EXERCISE_BASE


def test_xp_for_assessment_completion_uses_risk_bonus() -> None:
    assert gam.xp_for_assessment_completion(None) == gam.XP_PER_ASSESSMENT_BASE
    assert gam.xp_for_assessment_completion("red") == gam.XP_PER_ASSESSMENT_BASE
    assert (
        gam.xp_for_assessment_completion("yellow")
        == gam.XP_PER_ASSESSMENT_BASE + gam.XP_ASSESSMENT_YELLOW_BONUS
    )
    assert (
        gam.xp_for_assessment_completion("green")
        == gam.XP_PER_ASSESSMENT_BASE + gam.XP_ASSESSMENT_GREEN_BONUS
    )


# --------------------------------------------------------- Streak logic
#
# ``_apply_streak`` is a pure function over the Gamification record so we
# can test it without a live database.


class _StubRecord:
    """Lightweight stand-in for the Gamification ORM row."""

    def __init__(self) -> None:
        self.streak_days = 0
        self.longest_streak = 0
        self.last_activity_date: date | None = None


def test_streak_starts_fresh_on_first_activity() -> None:
    rec = _StubRecord()
    gam._apply_streak(rec, date(2024, 6, 1))
    assert rec.streak_days == 1
    assert rec.longest_streak == 1
    assert rec.last_activity_date == date(2024, 6, 1)


def test_streak_increments_on_consecutive_days() -> None:
    rec = _StubRecord()
    today = date(2024, 6, 1)
    for offset in range(5):
        gam._apply_streak(rec, today + timedelta(days=offset))
    assert rec.streak_days == 5
    assert rec.longest_streak == 5


def test_streak_holds_when_called_twice_same_day() -> None:
    rec = _StubRecord()
    gam._apply_streak(rec, date(2024, 6, 1))
    gam._apply_streak(rec, date(2024, 6, 1))
    assert rec.streak_days == 1
    assert rec.longest_streak == 1


def test_streak_resets_after_gap_but_keeps_longest() -> None:
    rec = _StubRecord()
    gam._apply_streak(rec, date(2024, 6, 1))
    gam._apply_streak(rec, date(2024, 6, 2))
    gam._apply_streak(rec, date(2024, 6, 3))
    # Three-day gap.
    gam._apply_streak(rec, date(2024, 6, 7))
    assert rec.streak_days == 1
    assert rec.longest_streak == 3
    assert rec.last_activity_date == date(2024, 6, 7)


async def test_award_xp_rejects_negative_amount() -> None:
    """Negative XP awards are a programmer error and should fail loudly."""

    with pytest.raises(ValueError):
        await gam.award_xp(None, "any-child-id", -5)  # type: ignore[arg-type]
