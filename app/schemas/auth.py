"""Authentication request/response schemas (Pydantic v2).

Either ``email`` or ``phone`` may be supplied as the principal
identifier for register/login; we validate one-of in the model itself
so endpoints stay thin.
"""

from __future__ import annotations

import re
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

# Phones in this market are typically +998 followed by 9 digits.
# We keep the validator generic — accept E.164-ish strings of 8–20 chars.
PHONE_RE = re.compile(r"^\+?[0-9]{8,20}$")

PasswordStr = Annotated[str, StringConstraints(min_length=8, max_length=128)]
NameStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


def _normalize_phone(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().replace(" ", "").replace("-", "")
    if not cleaned:
        return None
    if not PHONE_RE.match(cleaned):
        raise ValueError("Phone must be 8-20 digits, optionally starting with '+'.")
    return cleaned


class RegisterRequest(BaseModel):
    """Payload for ``POST /auth/register``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr | None = Field(default=None, description="Email or phone is required.")
    phone: str | None = Field(default=None, description="Email or phone is required.")
    password: PasswordStr
    full_name: NameStr
    role: UserRole = UserRole.PARENT
    language: UserLanguage = UserLanguage.UZ

    @field_validator("phone", mode="before")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return _normalize_phone(value)

    @model_validator(mode="after")
    def _require_identifier(self) -> RegisterRequest:
        if not self.email and not self.phone:
            raise ValueError("Either email or phone must be provided.")
        # Self-service registration is restricted to non-staff roles to
        # keep privilege escalation off the public endpoint. Staff users
        # must be created by an admin.
        if self.role in {UserRole.ADMIN, UserRole.THERAPIST}:
            raise ValueError("Admin or therapist accounts cannot self-register.")
        return self


class LoginRequest(BaseModel):
    """Payload for ``POST /auth/login``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr | None = None
    phone: str | None = None
    password: PasswordStr

    @field_validator("phone", mode="before")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return _normalize_phone(value)

    @model_validator(mode="after")
    def _require_identifier(self) -> LoginRequest:
        if not self.email and not self.phone:
            raise ValueError("Either email or phone must be provided.")
        return self


class RefreshRequest(BaseModel):
    """Payload for ``POST /auth/refresh``."""

    refresh_token: str = Field(min_length=10)


class TokenPair(BaseModel):
    """Response carrying both access and refresh tokens."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(
        description="Seconds until ``access_token`` expires.",
    )


class LogoutResponse(BaseModel):
    """Confirms a successful logout."""

    detail: str = "Logged out"
