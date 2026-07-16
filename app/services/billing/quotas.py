"""Quota helpers — read the feature flags for a user's effective plan.

This module exposes the feature dict the mobile client needs to gate
UI elements (e.g. "max_children", "ai_analysis", "premium_exercises")
without inventing new business rules. It also provides the tooling the
backend will use *later* to enforce hard limits — gated behind
``Settings.billing_enforce_quotas`` so the grace period stays untouched
until the flag is flipped.

Design notes:

* Reads use the existing :func:`get_active_subscription` →
  :func:`effective_plan_code` chain, so a user without any subscription
  row resolves to ``free`` exactly as before.
* When a plan is missing from the database (defensive — should never
  happen because :func:`ensure_default_plans` is idempotent), we fall
  back to the in-memory :data:`DEFAULT_PLANS` catalogue so callers
  always get a consistent feature shape.
* No enforcement code path is currently wired into the API. Callers can
  use :func:`is_feature_enabled` / :func:`get_feature_value` from any
  endpoint when we lift the grace period.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.billing import BillingPlanCode
from app.services.billing.plans import DEFAULT_PLANS, get_plan_by_code
from app.services.billing.subscriptions import (
    effective_plan_code,
    get_active_subscription,
)

# Canonical fallback so callers always get a non-``None`` features dict.
_DEFAULT_FEATURES_BY_CODE: dict[str, dict[str, Any]] = {
    spec["code"]: dict(spec.get("features") or {}) for spec in DEFAULT_PLANS
}


async def resolve_plan_for_user(
    session: AsyncSession, user_id: str
) -> tuple[str, dict[str, Any]]:
    """Return ``(plan_code, features)`` for a user's effective plan.

    Free / cancelled / expired users resolve to the ``free`` plan with
    the corresponding feature dict.
    """

    sub = await get_active_subscription(session, user_id)
    code = effective_plan_code(sub)
    plan = await get_plan_by_code(session, code)
    if plan is not None and plan.features is not None:
        return code, dict(plan.features)
    # Defensive fallback to the in-memory default catalogue.
    return code, dict(_DEFAULT_FEATURES_BY_CODE.get(code, {}))


def is_feature_enabled(features: dict[str, Any], key: str) -> bool:
    """Return ``True`` when ``features[key]`` is a truthy boolean.

    Missing keys default to ``False`` so a forgotten flag fails closed
    once enforcement is enabled.
    """

    value = features.get(key, False)
    return bool(value)


def get_feature_value(
    features: dict[str, Any], key: str, default: Any = None
) -> Any:
    """Return ``features[key]`` or ``default`` if the key is missing.

    ``None`` is treated as "unlimited" by convention.
    """

    if key not in features:
        return default
    return features[key]


def quotas_enforced() -> bool:
    """Return ``True`` when the platform should enforce paid-plan quotas.

    During the rollout grace period this returns ``False`` so free users
    keep their pre-billing behaviour. Flip the
    ``BILLING_ENFORCE_QUOTAS`` env var to turn enforcement on without a
    code change.
    """

    return bool(get_settings().billing_enforce_quotas)


async def get_user_features(
    session: AsyncSession, user_id: str
) -> dict[str, Any]:
    """Convenience wrapper that returns just the feature dict."""

    _code, features = await resolve_plan_for_user(session, user_id)
    return features


# Stable defaults the rest of the app can import. Used by tests and any
# future enforcement layer to pick a sensible "unlimited" sentinel.
FREE_PLAN_CODE = BillingPlanCode.FREE.value


__all__ = [
    "FREE_PLAN_CODE",
    "get_feature_value",
    "get_user_features",
    "is_feature_enabled",
    "quotas_enforced",
    "resolve_plan_for_user",
]
