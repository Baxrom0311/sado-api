"""Pydantic schemas for the gamification feature."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.core.pagination import Page
from app.models.gamification import BadgeCategory, BadgeRequirementType

CodeStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
]
TitleStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]
DescriptionStr = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=500)
]
IconStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
]


VALID_REQUIREMENT_TYPES = {t.value for t in BadgeRequirementType}
VALID_CATEGORIES = {c.value for c in BadgeCategory}


def _normalise_choice(
    value: Any,
    *,
    allowed: set[str],
    field: str,
    default: str | None = None,
) -> str:
    if value is None:
        if default is None:
            raise ValueError(f"{field} is required")
        return default
    if hasattr(value, "value"):
        value = value.value
    cleaned = str(value).strip().lower()
    if cleaned not in allowed:
        raise ValueError(
            f"{field} must be one of {sorted(allowed)}, got {value!r}"
        )
    return cleaned


# --------------------------------------------------------------- Badges


class BadgeBase(BaseModel):
    """Shared fields for badge create/update payloads."""

    model_config = ConfigDict(str_strip_whitespace=True)

    code: CodeStr
    title_uz: TitleStr
    title_ru: str = ""
    description_uz: DescriptionStr = ""
    description_ru: DescriptionStr = ""
    icon: IconStr = "🏅"
    category: str = BadgeCategory.MILESTONE.value
    requirement_type: str = BadgeRequirementType.XP.value
    threshold: int = Field(default=0, ge=0, le=1_000_000)
    sort_order: int = Field(default=0, ge=0, le=10_000)
    is_active: bool = True

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, value: Any) -> str:
        return _normalise_choice(
            value,
            allowed=VALID_CATEGORIES,
            field="category",
            default=BadgeCategory.MILESTONE.value,
        )

    @field_validator("requirement_type", mode="before")
    @classmethod
    def _requirement_type(cls, value: Any) -> str:
        return _normalise_choice(
            value,
            allowed=VALID_REQUIREMENT_TYPES,
            field="requirement_type",
            default=BadgeRequirementType.XP.value,
        )


class BadgeCreate(BadgeBase):
    """Payload for ``POST /badges``."""


class BadgeUpdate(BaseModel):
    """Patch payload for ``PUT /badges/:id``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title_uz: TitleStr | None = None
    title_ru: str | None = None
    description_uz: DescriptionStr | None = None
    description_ru: DescriptionStr | None = None
    icon: IconStr | None = None
    category: str | None = None
    requirement_type: str | None = None
    threshold: int | None = Field(default=None, ge=0, le=1_000_000)
    sort_order: int | None = Field(default=None, ge=0, le=10_000)
    is_active: bool | None = None

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _normalise_choice(
            value, allowed=VALID_CATEGORIES, field="category"
        )

    @field_validator("requirement_type", mode="before")
    @classmethod
    def _requirement_type(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _normalise_choice(
            value, allowed=VALID_REQUIREMENT_TYPES, field="requirement_type"
        )


class BadgePublic(BaseModel):
    """Read-side schema for badges."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    code: str
    title_uz: str
    title_ru: str
    description_uz: str
    description_ru: str
    icon: str
    category: str
    requirement_type: str
    threshold: int
    sort_order: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class BadgeEarningPublic(BaseModel):
    """A child's unlocked badge with the unlock timestamp + badge detail."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    child_id: str
    badge_id: str
    earned_at: datetime
    badge: BadgePublic | None = None


# ---------------------------------------------------------- Gamification


class GamificationPublic(BaseModel):
    """A child's gamification status, including derived progress fields.

    ``xp_into_level`` and ``xp_for_next_level`` describe the progress
    bar the mobile UI draws under the level number.
    """

    model_config = ConfigDict(from_attributes=True)

    child_id: str
    total_xp: int
    level: int
    xp_into_level: int
    xp_for_next_level: int
    streak_days: int
    longest_streak: int
    last_activity_date: date | None
    total_exercises_completed: int
    total_assessments_completed: int
    badges_earned: int
    created_at: datetime
    updated_at: datetime


class XPAwardRequest(BaseModel):
    """Body for ``POST /children/:id/gamification/award`` (admin only)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    amount: int = Field(..., ge=1, le=10_000)
    reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
    ]


class XPAwardResponse(BaseModel):
    """Response describing the effect of an XP award.

    ``newly_earned_badges`` is empty unless this award caused at least
    one badge requirement to flip from unmet to met.
    """

    gamification: GamificationPublic
    xp_added: int
    leveled_up: bool
    previous_level: int
    new_level: int
    newly_earned_badges: list[BadgePublic] = Field(default_factory=list)


class LeaderboardEntry(BaseModel):
    """One row of a leaderboard response."""

    model_config = ConfigDict(from_attributes=True)

    rank: int
    child_id: str
    child_name: str
    total_xp: int
    level: int
    streak_days: int


class LeaderboardResponse(BaseModel):
    """Top-N XP leaderboard scoped by parent / kindergarten / global."""

    scope: str
    entries: list[LeaderboardEntry]


BadgePage = Page[BadgePublic]
BadgeEarningPage = Page[BadgeEarningPublic]


__all__ = [
    "BadgeCreate",
    "BadgeEarningPage",
    "BadgeEarningPublic",
    "BadgePage",
    "BadgePublic",
    "BadgeUpdate",
    "GamificationPublic",
    "LeaderboardEntry",
    "LeaderboardResponse",
    "XPAwardRequest",
    "XPAwardResponse",
]
