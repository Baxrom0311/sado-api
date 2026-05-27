"""Schemas for the Children resource.

Children are the primary subject of every assessment, so the schemas
need to support both the parent-app (small, role-restricted payloads)
and the admin dashboard (richer filter/sort, cursor pagination).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.core.pagination import Page

NameStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]
NotesStr = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=1000)
]


VALID_GENDERS = {"male", "female", "unknown"}
VALID_LANGUAGES = {"uz", "ru", "kk", "en"}


def _normalise_gender(value: str | None) -> str:
    if value is None:
        return "unknown"
    cleaned = value.strip().lower()
    if cleaned not in VALID_GENDERS:
        raise ValueError(
            f"gender must be one of {sorted(VALID_GENDERS)}, got {value!r}"
        )
    return cleaned


def _normalise_language(value: str | None) -> str:
    if value is None:
        return "uz"
    cleaned = value.strip().lower()
    if cleaned not in VALID_LANGUAGES:
        raise ValueError(
            f"language must be one of {sorted(VALID_LANGUAGES)}, got {value!r}"
        )
    return cleaned


def _validate_birth_date(value: date) -> date:
    """Ensure the child is plausibly aged for the platform.

    SADO targets ages 2–12, so birth dates are clamped to a generous
    range (0–18 years in the past). We don't reject ages 0–1 — the data
    model still accepts them and the assessment UI handles age gating.
    """

    today = date.today()
    if value > today:
        raise ValueError("birth_date cannot be in the future")
    age_years = today.year - value.year - (
        (today.month, today.day) < (value.month, value.day)
    )
    if age_years > 18:
        raise ValueError("birth_date implies an age greater than 18 years")
    return value


class ChildBase(BaseModel):
    """Fields common to create / update payloads."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: NameStr
    birth_date: date
    gender: str = "unknown"
    language: str = "uz"
    notes: NotesStr | None = None
    kindergarten_id: str | None = Field(default=None, max_length=36)

    @field_validator("birth_date")
    @classmethod
    def _check_birth_date(cls, value: date) -> date:
        return _validate_birth_date(value)

    @field_validator("gender", mode="before")
    @classmethod
    def _gender(cls, value: str | None) -> str:
        return _normalise_gender(value)

    @field_validator("language", mode="before")
    @classmethod
    def _language(cls, value: str | None) -> str:
        return _normalise_language(value)


class ChildCreate(ChildBase):
    """Payload for ``POST /children``.

    Parents always create children under their own account, so
    ``parent_id`` is ignored when present. Admin/teacher tooling can
    supply ``parent_id`` to register children for another parent.
    """

    parent_id: str | None = Field(default=None, max_length=36)


class ChildUpdate(BaseModel):
    """Patch payload for ``PUT /children/:id`` — every field optional."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: NameStr | None = None
    birth_date: date | None = None
    gender: str | None = None
    language: str | None = None
    notes: NotesStr | None = None
    kindergarten_id: str | None = Field(default=None, max_length=36)

    @field_validator("birth_date")
    @classmethod
    def _check_birth_date(cls, value: date | None) -> date | None:
        if value is None:
            return None
        return _validate_birth_date(value)

    @field_validator("gender", mode="before")
    @classmethod
    def _gender(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalise_gender(value)

    @field_validator("language", mode="before")
    @classmethod
    def _language(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalise_language(value)


class ChildPublic(BaseModel):
    """Read-side schema returned to API clients."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    birth_date: date
    gender: str
    language: str
    notes: str | None
    parent_id: str
    kindergarten_id: str | None
    created_at: datetime
    updated_at: datetime
    age_years: int = Field(
        default=0, description="Convenience: years since birth_date as of today."
    )

    @classmethod
    def from_model(cls, child: object) -> ChildPublic:
        """Build from an ORM ``Child`` instance and compute ``age_years``."""

        today = date.today()
        bdate: date = child.birth_date
        age = today.year - bdate.year - (
            (today.month, today.day) < (bdate.month, bdate.day)
        )
        return cls(
            id=child.id,
            name=child.name,
            birth_date=bdate,
            gender=child.gender,
            language=child.language,
            notes=child.notes,
            parent_id=child.parent_id,
            kindergarten_id=child.kindergarten_id,
            created_at=child.created_at,
            updated_at=child.updated_at,
            age_years=max(0, age),
        )


ChildPage = Page[ChildPublic]
