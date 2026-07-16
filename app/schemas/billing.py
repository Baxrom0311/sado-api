"""Pydantic schemas for the billing endpoints + provider webhooks.

These mirror the wire format of the public ``/billing/*`` endpoints
and Payme's JSON-RPC payloads. Provider webhooks are loose by design
(Payme errors are returned with ``200 OK``) so the schemas here only
cover request bodies — responses are emitted as plain dicts in the
service layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.pagination import Page

# --------------------------------------------------------------------- Plan


class BillingPlanPublic(BaseModel):
    """Read-side plan payload returned by ``GET /billing/plans``."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    code: str
    name_uz: str
    name_ru: str
    description_uz: str | None = None
    description_ru: str | None = None
    price_tiyin: int
    price_uzs: int = Field(
        description="Price in UZS (whole soum) — convenience for the UI."
    )
    duration_days: int
    features: dict[str, Any] | None = None
    is_active: bool
    sort_order: int


# --------------------------------------------------------------- Subscription


class SubscriptionPublic(BaseModel):
    """Read-side subscription payload."""

    model_config = ConfigDict(from_attributes=True)

    id: str | None = None
    user_id: str
    plan_code: str
    status: str
    starts_at: datetime | None = None
    expires_at: datetime | None = None
    auto_renew: bool = False
    cancelled_at: datetime | None = None
    is_active: bool = True
    days_remaining: int | None = None
    features: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Feature flags / quotas for the effective plan. Empty dict "
            "for plans that don't define any features. The mobile UI "
            "uses this to gate UI elements (e.g. premium exercises, "
            "AI analysis) without making a second round-trip."
        ),
    )


# ----------------------------------------------------------------- Order


class OrderCreateRequest(BaseModel):
    """Body for ``POST /billing/orders``."""

    plan_code: str = Field(min_length=1, max_length=32)
    provider: Literal["payme", "click"] = "payme"


class OrderPublic(BaseModel):
    """Read-side order payload."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    plan_code: str
    amount_tiyin: int
    amount_uzs: int = Field(
        description="Amount in UZS (whole soum) — convenience for the UI."
    )
    state: str
    provider: str | None = None
    paid_at: datetime | None = None
    cancelled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class OrderCreateResponse(BaseModel):
    """``POST /billing/orders`` response — order + provider redirect."""

    order: OrderPublic
    payment_url: str = Field(
        description=(
            "Provider checkout URL. The mobile client opens this in an "
            "in-app browser; the server is notified through the webhook."
        )
    )


OrderPage = Page[OrderPublic]


# ----------------------------------------------------------- Features


class PlanFeaturesPublic(BaseModel):
    """Resolved feature/quota set for the caller's effective plan.

    Returned by ``GET /billing/me/features``. The mobile client uses
    this to render gating UI (upgrade prompts, premium-only badges)
    without re-fetching the whole plan catalogue.
    """

    plan_code: str
    is_active: bool
    features: dict[str, Any] = Field(default_factory=dict)
    quotas_enforced: bool = Field(
        description=(
            "True once the backend starts enforcing quotas (post grace "
            "period). False during rollout — clients may show upgrade "
            "prompts without seeing 4xx errors."
        ),
    )


# -------------------------------------------------------- Payme JSON-RPC


class PaymeRequest(BaseModel):
    """Loose JSON-RPC envelope for Payme webhooks."""

    model_config = ConfigDict(extra="allow")

    id: int | str | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


# ----------------------------------------------------------- Usage


class UsageMetricPublic(BaseModel):
    """One metric row in ``GET /billing/usage``."""

    metric: str
    period_key: str = Field(
        description=(
            "Bucket key. ``YYYY-MM-DD`` for daily metrics, "
            "``YYYY-MM`` for monthly, ``\"total\"`` for lifetime."
        )
    )
    limit: int | None = Field(
        default=None,
        description="Hard cap for the period; ``null`` means unlimited.",
    )
    used: int = Field(ge=0)
    remaining: int | None = Field(
        default=None,
        description="``null`` when ``limit`` is unlimited.",
    )
    plan_code: str


class UsagePublic(BaseModel):
    """Body of ``GET /billing/usage``."""

    plan_code: str
    quotas_enforced: bool
    metrics: list[UsageMetricPublic]


__all__ = [
    "BillingPlanPublic",
    "OrderCreateRequest",
    "OrderCreateResponse",
    "OrderPage",
    "OrderPublic",
    "PaymeRequest",
    "PlanFeaturesPublic",
    "SubscriptionPublic",
    "UsageMetricPublic",
    "UsagePublic",
]
