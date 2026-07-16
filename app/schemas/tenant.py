"""Pydantic schemas for the multi-tenant API.

Tenants in SADO are kindergartens — :class:`TenantPublic` mirrors the
:class:`KindergartenPublic` payload but is shaped for the tenant
admin dashboard (which cares about quotas + plan rather than
counts/region). :class:`TenantSettingsPublic` carries the per-tenant
feature flags.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.core.pagination import Page
from app.models.tenant import SubscriptionPlan

NameStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]
AddressStr = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=500)
]
PhoneStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=4, max_length=32)
]


_VALID_PLANS = {p.value for p in SubscriptionPlan}


class TenantSettingsPublic(BaseModel):
    """Read-side per-tenant feature flags + quotas."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    max_children: int
    max_users: int
    ai_analysis_enabled: bool
    custom_exercises_enabled: bool
    subscription_plan: str
    created_at: datetime
    updated_at: datetime


class TenantSettingsUpdate(BaseModel):
    """Patch payload for ``PUT /tenants/{id}/settings``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    max_children: int | None = Field(default=None, ge=1, le=100_000)
    max_users: int | None = Field(default=None, ge=1, le=10_000)
    ai_analysis_enabled: bool | None = None
    custom_exercises_enabled: bool | None = None
    subscription_plan: str | None = Field(default=None, max_length=20)

    @field_validator("subscription_plan")
    @classmethod
    def _check_plan(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().lower()
        if cleaned not in _VALID_PLANS:
            raise ValueError(
                f"subscription_plan must be one of {sorted(_VALID_PLANS)}"
            )
        return cleaned


class TenantCreate(BaseModel):
    """Payload for ``POST /tenants`` (super_admin only).

    Creates a new tenant by either creating a fresh kindergarten OR
    binding settings to an existing one (when ``kindergarten_id`` is
    supplied). Either ``name`` or ``kindergarten_id`` must be given.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    name: NameStr | None = None
    address: AddressStr | None = None
    phone: PhoneStr | None = None
    region_id: str | None = Field(default=None, max_length=36)
    kindergarten_id: str | None = Field(default=None, max_length=36)
    max_children: int = Field(default=100, ge=1, le=100_000)
    max_users: int = Field(default=20, ge=1, le=10_000)
    ai_analysis_enabled: bool = True
    custom_exercises_enabled: bool = True
    subscription_plan: str = Field(default=SubscriptionPlan.FREE.value, max_length=20)

    @field_validator("subscription_plan")
    @classmethod
    def _check_plan(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in _VALID_PLANS:
            raise ValueError(
                f"subscription_plan must be one of {sorted(_VALID_PLANS)}"
            )
        return cleaned


class TenantPublic(BaseModel):
    """Read-side tenant payload — kindergarten + settings combined."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    address: str | None
    phone: str | None
    region_id: str | None
    settings: TenantSettingsPublic | None = None
    created_at: datetime
    updated_at: datetime


class TenantStats(BaseModel):
    """Aggregate counts for a tenant — used by the tenant dashboard."""

    tenant_id: str
    name: str
    total_users: int
    total_children: int
    total_assessments: int
    total_exercises: int
    risk_green: int
    risk_yellow: int
    risk_red: int
    subscription_plan: str | None


TenantPage = Page[TenantPublic]


__all__ = [
    "TenantCreate",
    "TenantPage",
    "TenantPublic",
    "TenantSettingsPublic",
    "TenantSettingsUpdate",
    "TenantStats",
]
