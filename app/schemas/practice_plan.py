"""Pydantic schemas for practice plans and plan items.

Mirrors the model shape but normalises enum-style fields to lowercase
strings and clamps numeric ranges so the API is forgiving about
whatever the mobile client sends.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.core.pagination import Page
from app.models.practice_plan import PracticePlanItemStatus, PracticePlanStatus
from app.services.recommendations import SUPPORTED_LOCALES

VALID_PLAN_STATUSES = {s.value for s in PracticePlanStatus}
VALID_ITEM_STATUSES = {s.value for s in PracticePlanItemStatus}
VALID_LOCALES = set(SUPPORTED_LOCALES)


TitleStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
SummaryStr = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=2000)
]
NotesStr = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=2000)
]
FocusCodeStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
]


def _normalise_choice(
    value: Any, *, allowed: set[str], field: str, default: str | None = None
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


# --------------------------------------------------------------- Items


class PracticePlanItemBase(BaseModel):
    """Fields shared by item create / update payloads."""

    model_config = ConfigDict(str_strip_whitespace=True)

    exercise_id: str = Field(..., min_length=1, max_length=36)
    priority: int = Field(default=3, ge=1, le=5)
    target_count: int = Field(default=1, ge=1, le=1000)
    focus_code: FocusCodeStr | None = None
    notes: NotesStr | None = None


class PracticePlanItemCreate(PracticePlanItemBase):
    """Body for ``POST /practice-plans/:id/items``."""


class PracticePlanItemUpdate(BaseModel):
    """Patch body for ``PUT /practice-plans/:id/items/:item_id``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    priority: int | None = Field(default=None, ge=1, le=5)
    target_count: int | None = Field(default=None, ge=1, le=1000)
    completed_count: int | None = Field(default=None, ge=0, le=1000)
    status: str | None = None
    focus_code: FocusCodeStr | None = None
    notes: NotesStr | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _normalise_choice(
            value, allowed=VALID_ITEM_STATUSES, field="status"
        )


class PracticePlanItemComplete(BaseModel):
    """Body for ``POST /practice-plans/:id/items/:item_id/complete``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    increment: int = Field(
        default=1,
        ge=1,
        le=1000,
        description=(
            "How many additional repetitions to record. Capped at "
            "``target_count`` server-side; once reached the item moves "
            "to ``status=completed``."
        ),
    )
    notes: NotesStr | None = None


class PracticePlanItemPublic(BaseModel):
    """Read-side schema for a single plan item."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    plan_id: str
    exercise_id: str
    status: str
    priority: int
    target_count: int
    completed_count: int
    focus_code: str | None
    notes: str | None
    completed_at: datetime | None
    metadata_json: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime

    # Embedded so the mobile app can render the item without a second
    # round trip per exercise.
    exercise_title: str | None = None
    exercise_category: str | None = None
    exercise_difficulty: str | None = None


# --------------------------------------------------------------- Plans


class PracticePlanCreate(BaseModel):
    """Body for ``POST /practice-plans`` (manual authoring)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    child_id: str = Field(..., min_length=1, max_length=36)
    title: TitleStr
    summary: SummaryStr | None = None
    locale: str = "uz"
    status: str = PracticePlanStatus.DRAFT.value
    focus_areas: list[FocusCodeStr] | None = None
    start_date: date | None = None
    end_date: date | None = None
    assessment_id: str | None = Field(default=None, max_length=36)
    items: list[PracticePlanItemCreate] = Field(default_factory=list)

    @field_validator("locale", mode="before")
    @classmethod
    def _locale(cls, value: Any) -> str:
        return _normalise_choice(
            value, allowed=VALID_LOCALES, field="locale", default="uz"
        )

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, value: Any) -> str:
        return _normalise_choice(
            value,
            allowed=VALID_PLAN_STATUSES,
            field="status",
            default=PracticePlanStatus.DRAFT.value,
        )


class PracticePlanUpdate(BaseModel):
    """Patch body for ``PUT /practice-plans/:id``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: TitleStr | None = None
    summary: SummaryStr | None = None
    locale: str | None = None
    status: str | None = None
    focus_areas: list[FocusCodeStr] | None = None
    start_date: date | None = None
    end_date: date | None = None

    @field_validator("locale", mode="before")
    @classmethod
    def _locale(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _normalise_choice(
            value, allowed=VALID_LOCALES, field="locale"
        )

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _normalise_choice(
            value, allowed=VALID_PLAN_STATUSES, field="status"
        )


class PracticePlanGenerateRequest(BaseModel):
    """Body for ``POST /practice-plans/generate``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    assessment_id: str = Field(..., min_length=1, max_length=36)
    locale: str | None = None
    max_items: int = Field(default=5, ge=1, le=10)
    title: TitleStr | None = None
    activate: bool = Field(
        default=False,
        description=(
            "When ``true`` the generated plan is created in ``active`` "
            "status; otherwise it stays in ``draft`` for therapist review."
        ),
    )

    @field_validator("locale", mode="before")
    @classmethod
    def _locale(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _normalise_choice(
            value, allowed=VALID_LOCALES, field="locale"
        )


class PracticePlanPublic(BaseModel):
    """Read-side schema for a practice plan (without items)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    child_id: str
    assessment_id: str | None
    created_by_id: str | None
    tenant_id: str | None
    title: str
    summary: str | None
    status: str
    locale: str
    focus_areas: list[str] | None = None
    start_date: date | None
    end_date: date | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    item_count: int = 0
    completed_item_count: int = 0


class PracticePlanDetail(PracticePlanPublic):
    """Plan with embedded item list — returned by the detail endpoint."""

    items: list[PracticePlanItemPublic] = Field(default_factory=list)


PracticePlanPage = Page[PracticePlanPublic]


__all__ = [
    "PracticePlanCreate",
    "PracticePlanDetail",
    "PracticePlanGenerateRequest",
    "PracticePlanItemComplete",
    "PracticePlanItemCreate",
    "PracticePlanItemPublic",
    "PracticePlanItemUpdate",
    "PracticePlanPage",
    "PracticePlanPublic",
    "PracticePlanUpdate",
    "VALID_ITEM_STATUSES",
    "VALID_LOCALES",
    "VALID_PLAN_STATUSES",
]
