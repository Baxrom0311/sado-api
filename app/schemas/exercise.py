"""Pydantic schemas for exercises and exercise assignments."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.core.pagination import Page
from app.models.exercise import (
    AssignmentStatus,
    ExerciseAgeGroup,
    ExerciseCategory,
    ExerciseDifficulty,
)

TitleStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
DescriptionStr = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=5000)
]
PathStr = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=500)
]
PhonemesStr = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=200)
]
NotesStr = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=2000)
]


VALID_CATEGORIES = {c.value for c in ExerciseCategory}
VALID_DIFFICULTIES = {d.value for d in ExerciseDifficulty}
VALID_AGE_GROUPS = {a.value for a in ExerciseAgeGroup}
VALID_LANGUAGES = {"uz", "ru", "kk", "en"}
VALID_ASSIGNMENT_STATUSES = {s.value for s in AssignmentStatus}


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


# -------------------------------------------------------------- Exercises


class ExerciseBase(BaseModel):
    """Fields shared by create and update payloads."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: TitleStr
    description: DescriptionStr | None = None
    category: str = ExerciseCategory.ARTICULATION.value
    age_group: str = ExerciseAgeGroup.PRESCHOOL.value
    difficulty: str = ExerciseDifficulty.EASY.value
    language: str = "uz"
    duration_minutes: int = Field(default=5, ge=1, le=120)
    audio_example_path: PathStr | None = None
    image_path: PathStr | None = None
    instructions: DescriptionStr | None = None
    target_phonemes: PhonemesStr | None = None
    is_active: bool = True

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, value: Any) -> str:
        return _normalise_choice(
            value,
            allowed=VALID_CATEGORIES,
            field="category",
            default=ExerciseCategory.ARTICULATION.value,
        )

    @field_validator("age_group", mode="before")
    @classmethod
    def _age_group(cls, value: Any) -> str:
        return _normalise_choice(
            value,
            allowed=VALID_AGE_GROUPS,
            field="age_group",
            default=ExerciseAgeGroup.PRESCHOOL.value,
        )

    @field_validator("difficulty", mode="before")
    @classmethod
    def _difficulty(cls, value: Any) -> str:
        return _normalise_choice(
            value,
            allowed=VALID_DIFFICULTIES,
            field="difficulty",
            default=ExerciseDifficulty.EASY.value,
        )

    @field_validator("language", mode="before")
    @classmethod
    def _language(cls, value: Any) -> str:
        return _normalise_choice(
            value,
            allowed=VALID_LANGUAGES,
            field="language",
            default="uz",
        )


class ExerciseCreate(ExerciseBase):
    """Payload for ``POST /exercises``."""


class ExerciseUpdate(BaseModel):
    """Patch payload for ``PUT /exercises/:id`` — every field optional."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: TitleStr | None = None
    description: DescriptionStr | None = None
    category: str | None = None
    age_group: str | None = None
    difficulty: str | None = None
    language: str | None = None
    duration_minutes: int | None = Field(default=None, ge=1, le=120)
    audio_example_path: PathStr | None = None
    image_path: PathStr | None = None
    instructions: DescriptionStr | None = None
    target_phonemes: PhonemesStr | None = None
    is_active: bool | None = None

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _normalise_choice(
            value, allowed=VALID_CATEGORIES, field="category"
        )

    @field_validator("age_group", mode="before")
    @classmethod
    def _age_group(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _normalise_choice(
            value, allowed=VALID_AGE_GROUPS, field="age_group"
        )

    @field_validator("difficulty", mode="before")
    @classmethod
    def _difficulty(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _normalise_choice(
            value, allowed=VALID_DIFFICULTIES, field="difficulty"
        )

    @field_validator("language", mode="before")
    @classmethod
    def _language(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _normalise_choice(
            value, allowed=VALID_LANGUAGES, field="language"
        )


class ExercisePublic(BaseModel):
    """Read-side schema for exercises."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    description: str | None
    category: str
    age_group: str
    difficulty: str
    language: str
    duration_minutes: int
    audio_example_path: str | None
    image_path: str | None
    instructions: str | None
    target_phonemes: str | None
    is_active: bool
    created_by_id: str | None
    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------ Assignments


class ExerciseAssignmentCreate(BaseModel):
    """Body for ``POST /exercises/:child_id/assign``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    exercise_id: str = Field(..., min_length=1, max_length=36)
    due_date: datetime | None = None
    notes: NotesStr | None = None


class ExerciseAssignmentUpdate(BaseModel):
    """Body for ``PUT /exercises/assignments/:id``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    status: str | None = None
    due_date: datetime | None = None
    notes: NotesStr | None = None
    score: float | None = Field(default=None, ge=0.0, le=100.0)

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _normalise_choice(
            value,
            allowed=VALID_ASSIGNMENT_STATUSES,
            field="status",
        )


class ExerciseAssignmentComplete(BaseModel):
    """Body for ``PUT /exercises/assignments/:id/complete``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    score: float | None = Field(default=None, ge=0.0, le=100.0)
    notes: NotesStr | None = None


class ExerciseAssignmentPublic(BaseModel):
    """Read-side schema for assignments (with embedded exercise summary)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    child_id: str
    exercise_id: str
    assigned_by_id: str | None
    status: str
    due_date: datetime | None
    completed_at: datetime | None
    score: float | None
    notes: str | None
    created_at: datetime
    updated_at: datetime
    exercise: ExercisePublic | None = None


ExercisePage = Page[ExercisePublic]
ExerciseAssignmentPage = Page[ExerciseAssignmentPublic]
