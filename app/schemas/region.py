"""Schemas for the Regions resource.

Regions describe Uzbekistan's hierarchy: country → region (viloyat) →
district (tuman). They are referenced from users and kindergartens for
geographic scoping and aggregated dashboard statistics.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.core.pagination import Page
from app.models.region import RegionType

NameStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]
CodeStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)
]


VALID_TYPES = {t.value for t in RegionType}


def _normalise_type(value: str | None) -> str:
    if value is None:
        return RegionType.REGION.value
    cleaned = value.strip().lower()
    if cleaned not in VALID_TYPES:
        raise ValueError(
            f"type must be one of {sorted(VALID_TYPES)}, got {value!r}"
        )
    return cleaned


class RegionBase(BaseModel):
    """Fields common to create / update payloads."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: NameStr
    code: CodeStr | None = None
    type: str = RegionType.REGION.value
    parent_id: str | None = Field(default=None, max_length=36)

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, value: str | None) -> str:
        return _normalise_type(value)


class RegionCreate(RegionBase):
    """Payload for ``POST /regions``."""


class RegionUpdate(BaseModel):
    """Patch payload for ``PUT /regions/:id`` — every field optional."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: NameStr | None = None
    code: CodeStr | None = None
    type: str | None = None
    parent_id: str | None = Field(default=None, max_length=36)

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalise_type(value)


class RegionPublic(BaseModel):
    """Read-side schema for regions."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    code: str | None
    type: str
    parent_id: str | None
    created_at: datetime
    updated_at: datetime


RegionPage = Page[RegionPublic]
