"""Billing service entry points (plans, subscriptions, providers)."""

from app.services.billing.click import (
    build_click_payment_url,
    handle_click_request,
)
from app.services.billing.payme import (
    build_payment_url,
    handle_payme_request,
    verify_basic_auth,
)
from app.services.billing.plans import (
    DEFAULT_PLANS,
    ensure_default_plans,
    get_plan_by_code,
)
from app.services.billing.quotas import (
    FREE_PLAN_CODE,
    get_feature_value,
    get_user_features,
    is_feature_enabled,
    quotas_enforced,
    resolve_plan_for_user,
)
from app.services.billing.subscriptions import (
    activate_subscription,
    cancel_subscription_auto_renew,
    days_remaining,
    effective_plan_code,
    get_active_subscription,
    is_subscription_active,
)
from app.services.billing.usage import (
    PlanLimitExceededError,
    enforce_quota,
    get_current_count,
    get_usage_snapshot,
    increment_usage,
    period_key_for,
)

__all__ = [
    "DEFAULT_PLANS",
    "FREE_PLAN_CODE",
    "PlanLimitExceededError",
    "activate_subscription",
    "build_click_payment_url",
    "build_payment_url",
    "cancel_subscription_auto_renew",
    "days_remaining",
    "effective_plan_code",
    "enforce_quota",
    "ensure_default_plans",
    "get_active_subscription",
    "get_current_count",
    "get_feature_value",
    "get_plan_by_code",
    "get_usage_snapshot",
    "get_user_features",
    "handle_click_request",
    "handle_payme_request",
    "increment_usage",
    "is_feature_enabled",
    "is_subscription_active",
    "period_key_for",
    "quotas_enforced",
    "resolve_plan_for_user",
    "verify_basic_auth",
]
