"""Schemas for the Kindergartens resource."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.pagination import Page

NameStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]
AddressStr = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=500)
]
PhoneStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=4, max_length=32)
]


class KindergartenBase(BaseModel):
    """Fields common to create / update payloads."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: NameStr
    address: AddressStr | None = None
    phone: PhoneStr | None = None
    teacher_count: int = Field(default=0, ge=0, le=10_000)
    child_count: int = Field(default=0, ge=0, le=100_000)
    region_id: str | None = Field(default=None, max_length=36)


class KindergartenCreate(KindergartenBase):
    """Payload for ``POST /kindergartens``."""


class KindergartenUpdate(BaseModel):
    """Patch payload for ``PUT /kindergartens/:id`` — every field optional."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: NameStr | None = None
    address: AddressStr | None = None
    phone: PhoneStr | None = None
    teacher_count: int | None = Field(default=None, ge=0, le=10_000)
    child_count: int | None = Field(default=None, ge=0, le=100_000)
    region_id: str | None = Field(default=None, max_length=36)


class KindergartenPublic(BaseModel):
    """Read-side schema for kindergartens."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    address: str | None
    phone: str | None
    teacher_count: int
    child_count: int
    region_id: str | None
    created_at: datetime
    updated_at: datetime


class KindergartenStats(BaseModel):
    """Aggregated risk distribution + counts for a kindergarten."""

    kindergarten_id: str
    name: str
    total_children: int
    risk_green: int
    risk_yellow: int
    risk_red: int
    assessed_children: int


KindergartenPage = Page[KindergartenPublic]
