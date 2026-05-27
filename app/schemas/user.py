"""User-facing schemas for profile and admin user listing."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.models.user import UserLanguage, UserRole

NameStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
PasswordStr = Annotated[str, StringConstraints(min_length=8, max_length=128)]

_PHONE_RE = re.compile(r"^\+?[0-9]{8,20}$")


def _normalize_phone(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().replace(" ", "").replace("-", "")
    if not cleaned:
        return None
    if not _PHONE_RE.match(cleaned):
        raise ValueError("Phone must be 8-20 digits, optionally starting with '+'.")
    return cleaned


class UserPublic(BaseModel):
    """Safe user representation — never includes the password hash."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str | None
    phone: str | None
    full_name: str
    role: UserRole
    language: UserLanguage
    is_active: bool
    is_verified: bool
    region_id: str | None
    created_at: datetime
    updated_at: datetime


class UserUpdate(BaseModel):
    """Patch payload for ``PUT /users/me``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    full_name: NameStr | None = None
    language: UserLanguage | None = None
    email: EmailStr | None = None
    region_id: str | None = Field(default=None, max_length=36)


class UserCreate(BaseModel):
    """Admin payload for ``POST /users``.

    Unlike ``RegisterRequest``, this is intentionally permissive about
    role: an authenticated admin may create users at any privilege
    level, including therapist and admin.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr | None = Field(default=None, description="Email or phone is required.")
    phone: str | None = Field(default=None, description="Email or phone is required.")
    password: PasswordStr
    full_name: NameStr
    role: UserRole = UserRole.PARENT
    language: UserLanguage = UserLanguage.UZ
    is_active: bool = True
    is_verified: bool = False
    region_id: str | None = Field(default=None, max_length=36)

    @field_validator("phone", mode="before")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return _normalize_phone(value)

    @model_validator(mode="after")
    def _require_identifier(self) -> UserCreate:
        if not self.email and not self.phone:
            raise ValueError("Either email or phone must be provided.")
        return self
