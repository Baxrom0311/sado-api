"""Subscription helpers — read current state, activate after payment.

The two key functions are:

* :func:`get_active_subscription` — returns the row that should drive
  the mobile client's gating, treating expiry / cancellation as
  effectively "free tier".
* :func:`activate_subscription` — called from the Payme/Click webhook
  after a successful PerformTransaction. Idempotent: re-running it
  with the same paid order won't duplicate rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import (
    BillingPlanCode,
    PaymentOrder,
    Subscription,
    SubscriptionStatus,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def get_active_subscription(
    session: AsyncSession, user_id: str
) -> Subscription | None:
    """Return the user's currently effective subscription, if any."""

    result = await session.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.expires_at.desc(), Subscription.created_at.desc())
    )
    rows = list(result.scalars().all())
    if not rows:
        return None

    now = _utcnow()
    for sub in rows:
        if sub.status == SubscriptionStatus.CANCELLED.value:
            continue
        # Treat as expired if the row says so OR the deadline has passed.
        expires_at = sub.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at is None or expires_at >= now:
            if sub.status == SubscriptionStatus.EXPIRED.value:
                # Status is stale but the deadline is fine; trust deadline.
                continue
            return sub

    return None


def is_subscription_active(sub: Subscription | None) -> bool:
    if sub is None:
        return False
    if sub.status == SubscriptionStatus.CANCELLED.value:
        return False
    expires_at = sub.expires_at
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at >= _utcnow()


def days_remaining(sub: Subscription | None) -> int | None:
    if sub is None or sub.expires_at is None:
        return None
    expires_at = sub.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    delta = expires_at - _utcnow()
    if delta.total_seconds() <= 0:
        return 0
    return int(delta.total_seconds() // 86400) + (
        1 if delta.total_seconds() % 86400 else 0
    )


async def activate_subscription(
    session: AsyncSession,
    *,
    user_id: str,
    plan_code: str,
    duration_days: int,
    order: PaymentOrder | None = None,
    starts_at: datetime | None = None,
) -> Subscription:
    """Provision (or extend) a paid subscription for ``user_id``.

    Idempotent on the order id — if a subscription already references
    this paid order, that row is returned untouched.
    """

    if order is not None:
        existing = await session.execute(
            select(Subscription).where(
                Subscription.last_order_id == order.id,
                Subscription.plan_code == plan_code,
            )
        )
        already = existing.scalar_one_or_none()
        if already is not None:
            return already

    now = _utcnow()
    starts = starts_at or now
    # If the user already has an active subscription on this plan,
    # extend its expires_at; otherwise create a fresh row.
    current = await get_active_subscription(session, user_id)
    if (
        current is not None
        and current.plan_code == plan_code
        and current.status
        in {SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIALING.value}
    ):
        base_expiry = current.expires_at
        if base_expiry is not None and base_expiry.tzinfo is None:
            base_expiry = base_expiry.replace(tzinfo=UTC)
        if base_expiry is None or base_expiry < now:
            base_expiry = now
        current.expires_at = base_expiry + timedelta(days=duration_days)
        current.status = SubscriptionStatus.ACTIVE.value
        if order is not None:
            current.last_order_id = order.id
        await session.flush()
        return current

    sub = Subscription(
        user_id=user_id,
        plan_code=plan_code,
        status=SubscriptionStatus.ACTIVE.value,
        starts_at=starts,
        expires_at=starts + timedelta(days=duration_days),
        auto_renew=False,
        last_order_id=order.id if order is not None else None,
    )
    session.add(sub)
    await session.flush()
    return sub


def effective_plan_code(sub: Subscription | None) -> str:
    """Return the plan code that drives the mobile client.

    A missing / cancelled / expired subscription resolves to
    :attr:`BillingPlanCode.FREE` so callers never need to special-case
    ``None``.
    """

    if not is_subscription_active(sub):
        return BillingPlanCode.FREE.value
    assert sub is not None
    return sub.plan_code


async def cancel_subscription_auto_renew(
    session: AsyncSession, *, user_id: str
) -> Subscription | None:
    """Disable auto-renew on the user's active subscription.

    The subscription remains ``active`` until ``expires_at`` so the
    user keeps the features they paid for through the end of the
    period. Returns the updated row, or ``None`` if there's nothing
    to cancel (free user — the API surface treats this as a no-op
    success).
    """

    sub = await get_active_subscription(session, user_id)
    if sub is None:
        return None
    sub.auto_renew = False
    if sub.cancelled_at is None:
        sub.cancelled_at = _utcnow()
    await session.flush()
    return sub


__all__ = [
    "activate_subscription",
    "cancel_subscription_auto_renew",
    "days_remaining",
    "effective_plan_code",
    "get_active_subscription",
    "is_subscription_active",
]
