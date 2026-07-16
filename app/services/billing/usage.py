"""Quota usage tracking — increment counters and compute usage snapshots.

Every action that consumes a paid-plan quota goes through one of two
paths:

* :func:`increment_usage` — bumps the counter for the user's current
  period and returns the new total. Designed to be called *after* the
  business action commits successfully so we never charge against a
  failed write.
* :func:`enforce_quota` — atomic check-and-increment used when
  :func:`app.services.billing.quotas.quotas_enforced` returns ``True``.
  Raises :class:`PlanLimitExceededError` (402) when the next increment
  would breach the plan's hard cap.

Counters are bucketed per metric:

* daily metrics use ``YYYY-MM-DD`` UTC period keys
  (``assessments_per_day``);
* monthly metrics use ``YYYY-MM`` (``ai_analyses_per_month``);
* lifetime/total metrics use the literal ``"total"``
  (``children_total``).

This deliberately avoids a global per-period reaper — old buckets stay
in the table for analytics but are never read by the API after the
period closes. The unique constraint
``uq_billing_usage_user_metric_period`` ensures the same key is never
double-inserted under load (the worst case is the upsert path retries).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import SadoAPIError
from app.models.billing import BillingUsageMetric, BillingUsageRecord
from app.services.billing.quotas import (
    get_feature_value,
    quotas_enforced,
    resolve_plan_for_user,
)

# --------------------------------------------------------------- Errors


class PlanLimitExceededError(SadoAPIError):
    """402 Payment Required — caller hit a plan-imposed quota.

    The ``extra`` payload carries machine-readable context for the
    mobile client to render an upgrade prompt::

        {
            "metric": "assessments_per_day",
            "limit": 3,
            "current": 3,
            "plan_code": "free",
            "upgrade_url": "/billing/plans"
        }
    """

    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "PLAN_LIMIT_EXCEEDED"
    default_message = (
        "You've reached your plan's limit. Upgrade to keep going."
    )


# ---------------------------------------------------------------- Time


def _utcnow() -> datetime:
    return datetime.now(UTC)


def period_key_for(metric: str, *, when: datetime | None = None) -> str:
    """Return the period bucket key for ``metric`` at ``when`` (UTC)."""

    moment = when or _utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    moment_utc = moment.astimezone(UTC)

    if metric == BillingUsageMetric.ASSESSMENTS_PER_DAY.value:
        return moment_utc.date().isoformat()  # YYYY-MM-DD
    if metric == BillingUsageMetric.AI_ANALYSES_PER_MONTH.value:
        return moment_utc.strftime("%Y-%m")
    if metric == BillingUsageMetric.CHILDREN_TOTAL.value:
        return "total"
    # Defensive default — daily granularity for unknown metrics so they
    # at least reset on a sensible cadence.
    return moment_utc.date().isoformat()


def _feature_key_for(metric: str) -> str | None:
    """Map a usage metric to the feature flag that holds its limit."""

    return {
        BillingUsageMetric.ASSESSMENTS_PER_DAY.value: "max_assessments_per_day",
        BillingUsageMetric.AI_ANALYSES_PER_MONTH.value: "ai_analyses_per_month",
        BillingUsageMetric.CHILDREN_TOTAL.value: "max_children",
    }.get(metric)


def _resolve_limit(features: dict[str, Any], metric: str) -> int | None:
    """Return the int cap for ``metric`` or ``None`` for unlimited."""

    feature_key = _feature_key_for(metric)
    if feature_key is None:
        return None
    raw = get_feature_value(features, feature_key, None)
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        # Convention used in the brief — negative means unlimited.
        return None
    return value


# --------------------------------------------------------------- Reads


async def get_current_count(
    session: AsyncSession,
    *,
    user_id: str,
    metric: str,
    when: datetime | None = None,
) -> int:
    """Return how many ``metric`` events have happened in the current period."""

    period_key = period_key_for(metric, when=when)
    result = await session.execute(
        select(BillingUsageRecord).where(
            BillingUsageRecord.user_id == user_id,
            BillingUsageRecord.metric == metric,
            BillingUsageRecord.period_key == period_key,
        )
    )
    row = result.scalar_one_or_none()
    return int(row.count) if row is not None else 0


async def get_usage_snapshot(
    session: AsyncSession,
    *,
    user_id: str,
    when: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return per-metric usage + remaining quota for the caller.

    Used by ``GET /billing/usage`` to render the "X of Y remaining"
    badges on the mobile home screen. Always returns one entry per
    known metric so the UI doesn't have to special-case missing keys.
    """

    plan_code, features = await resolve_plan_for_user(session, user_id)
    snapshot: list[dict[str, Any]] = []
    for metric in (
        BillingUsageMetric.ASSESSMENTS_PER_DAY.value,
        BillingUsageMetric.AI_ANALYSES_PER_MONTH.value,
        BillingUsageMetric.CHILDREN_TOTAL.value,
    ):
        limit = _resolve_limit(features, metric)
        used = await get_current_count(
            session, user_id=user_id, metric=metric, when=when
        )
        remaining: int | None
        if limit is None:
            remaining = None
        else:
            remaining = max(limit - used, 0)
        snapshot.append(
            {
                "metric": metric,
                "period_key": period_key_for(metric, when=when),
                "limit": limit,
                "used": used,
                "remaining": remaining,
                "plan_code": plan_code,
            }
        )
    return snapshot


# --------------------------------------------------------------- Writes


async def increment_usage(
    session: AsyncSession,
    *,
    user_id: str,
    metric: str,
    delta: int = 1,
    when: datetime | None = None,
) -> int:
    """Atomically increment the (user, metric, period) counter.

    Idempotency is **not** the caller's responsibility — pair this
    with the business write inside the same transaction.
    """

    if delta == 0:
        return await get_current_count(
            session, user_id=user_id, metric=metric, when=when
        )

    period_key = period_key_for(metric, when=when)
    # Try to find an existing row and update.
    existing_q = await session.execute(
        select(BillingUsageRecord).where(
            BillingUsageRecord.user_id == user_id,
            BillingUsageRecord.metric == metric,
            BillingUsageRecord.period_key == period_key,
        )
    )
    existing = existing_q.scalar_one_or_none()
    if existing is not None:
        existing.count = int(existing.count) + delta
        await session.flush()
        return int(existing.count)

    record = BillingUsageRecord(
        user_id=user_id,
        metric=metric,
        period_key=period_key,
        count=delta,
    )
    session.add(record)
    try:
        await session.flush()
    except IntegrityError:
        # Lost a race with another writer — refetch and increment.
        await session.rollback()
        existing_q = await session.execute(
            select(BillingUsageRecord).where(
                BillingUsageRecord.user_id == user_id,
                BillingUsageRecord.metric == metric,
                BillingUsageRecord.period_key == period_key,
            )
        )
        existing = existing_q.scalar_one_or_none()
        if existing is None:
            # Should be unreachable; surface the original error rather
            # than silently dropping the increment.
            raise
        existing.count = int(existing.count) + delta
        await session.flush()
        return int(existing.count)
    return int(record.count)


async def enforce_quota(
    session: AsyncSession,
    *,
    user_id: str,
    metric: str,
    when: datetime | None = None,
    delta: int = 1,
) -> int:
    """Increment quota usage, raising 402 if the cap would be breached.

    During the rollout grace period (``BILLING_ENFORCE_QUOTAS=False``)
    this falls back to :func:`increment_usage` so we *track* usage
    without ever rejecting requests. Flipping the flag to ``True``
    turns enforcement on with zero code changes elsewhere.

    Returns the new counter value on success.
    """

    plan_code, features = await resolve_plan_for_user(session, user_id)
    limit = _resolve_limit(features, metric)
    current = await get_current_count(
        session, user_id=user_id, metric=metric, when=when
    )
    if limit is not None and quotas_enforced() and current + delta > limit:
        raise PlanLimitExceededError(
            extra={
                "metric": metric,
                "limit": limit,
                "current": current,
                "plan_code": plan_code,
                "upgrade_url": "/billing/plans",
            },
        )
    return await increment_usage(
        session,
        user_id=user_id,
        metric=metric,
        delta=delta,
        when=when,
    )


__all__ = [
    "PlanLimitExceededError",
    "enforce_quota",
    "get_current_count",
    "get_usage_snapshot",
    "increment_usage",
    "period_key_for",
]
