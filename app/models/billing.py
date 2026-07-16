"""Billing models — subscription plans, user subscriptions, payment orders + transactions.

The billing layer is intentionally additive: existing data and code
paths must keep working even when no plan / subscription exists for a
user. The "free" plan is a logical default — a user without a row in
``subscriptions`` is treated as a free-tier customer and no quotas are
enforced (grace period).

Currency: all amounts are stored as integer **tiyin** (1 UZS = 100
tiyin), the same unit Payme and Click use on the wire. The mobile UI
divides by 100 for display.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._base import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:  # pragma: no cover
    from app.models.user import User


# --------------------------------------------------------------------- Enums


class BillingPlanCode(str, enum.Enum):
    """Stable codes used over the wire and by the mobile client."""

    FREE = "free"
    BASIC = "basic"
    PREMIUM = "premium"
    # B2B2C tiers added in 0011_billing_extensions:
    LOGOPED_PRO = "logoped_pro"
    CLINIC = "clinic"


class SubscriptionStatus(str, enum.Enum):
    """Lifecycle of a :class:`Subscription` row."""

    TRIALING = "trialing"
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class PaymentProvider(str, enum.Enum):
    """Supported payment processors."""

    PAYME = "payme"
    CLICK = "click"


class PaymentOrderState(str, enum.Enum):
    """State machine for an order.

    ``CREATED`` → user clicked "pay" but no provider transaction yet.
    ``PENDING`` → provider opened a transaction (Payme CreateTransaction).
    ``PAID``    → terminal success (PerformTransaction / Click Complete).
    ``CANCELLED`` → terminal failure / refund.
    """

    CREATED = "created"
    PENDING = "pending"
    PAID = "paid"
    CANCELLED = "cancelled"


class PaymentTransactionState(int, enum.Enum):
    """Payme-aligned transaction state codes.

    Click is mapped onto the same set so the rest of the app does not
    care which provider produced the row.
    """

    CREATED = 1     # transaction created, awaiting payment
    PERFORMED = 2   # transaction performed (paid)
    CANCELLED_PENDING = -1   # cancelled before perform
    CANCELLED_PERFORMED = -2  # cancelled after perform (refund)


# --------------------------------------------------------------------- Plan


class BillingPlan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A purchasable subscription tier.

    Plans are managed by admins; the rows are seeded by the
    :func:`app.services.billing.plans.ensure_default_plans` helper on
    application boot / first request so a fresh database always has
    free/basic/premium available.
    """

    __tablename__ = "billing_plans"

    code: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        unique=True,
        index=True,
    )
    name_uz: Mapped[str] = mapped_column(String(120), nullable=False)
    name_ru: Mapped[str] = mapped_column(String(120), nullable=False)
    description_uz: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_ru: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Price in tiyin (1 UZS = 100 tiyin). Free plan stores 0.
    price_tiyin: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    duration_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30
    )
    # Free-form feature flags / quotas the mobile app inspects to
    # decide what to show. Examples: ``{"max_children": 1,
    # "ai_analysis": true}``.
    features: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"<BillingPlan {self.code} {self.price_tiyin}t>"


# --------------------------------------------------------------- Subscription


class Subscription(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A user's current subscription.

    Exactly one *active* row per user is enforced at the application
    layer (we don't add a partial unique index because SQLite shares
    the same migration). Historical rows remain in the table so
    we can audit upgrades / churn.
    """

    __tablename__ = "subscriptions"

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plan_code: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=SubscriptionStatus.ACTIVE.value,
        index=True,
    )
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    auto_renew: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_order_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("payment_orders.id", ondelete="SET NULL"),
        nullable=True,
    )

    user: Mapped[User] = relationship("User", lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return (
            f"<Subscription user={self.user_id} "
            f"plan={self.plan_code} status={self.status}>"
        )


# ---------------------------------------------------------------- Payment


class PaymentOrder(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single billing intent — "user X wants to pay for plan Y".

    Created by ``POST /billing/orders`` before redirecting to a
    provider. Finalised by the webhook (Payme PerformTransaction or
    Click Complete) which flips ``state`` to ``PAID`` and provisions
    a subscription via
    :func:`app.services.billing.subscriptions.activate_subscription`.
    """

    __tablename__ = "payment_orders"

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plan_code: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    amount_tiyin: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=PaymentOrderState.CREATED.value,
        index=True,
    )
    provider: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship("User", lazy="joined")
    transactions: Mapped[list[PaymentTransaction]] = relationship(
        "PaymentTransaction",
        back_populates="order",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return (
            f"<PaymentOrder {self.id} {self.plan_code} "
            f"{self.amount_tiyin}t state={self.state}>"
        )


class PaymentTransaction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A provider-side transaction tied to a :class:`PaymentOrder`.

    The provider's transaction id (Payme ``id`` or Click
    ``click_trans_id``) is stored verbatim in ``provider_tx_id`` so we
    can reconcile statements. ``raw_payload`` stores the most recent
    request body for forensics.
    """

    __tablename__ = "payment_transactions"

    order_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("payment_orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True
    )
    provider_tx_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    state: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=PaymentTransactionState.CREATED.value,
        index=True,
    )
    amount_tiyin: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Provider epoch milliseconds — Payme uses ms in its API.
    create_time_ms: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    perform_time_ms: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    cancel_time_ms: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    cancel_reason: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )

    order: Mapped[PaymentOrder] = relationship(
        "PaymentOrder", back_populates="transactions", lazy="joined"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return (
            f"<PaymentTransaction {self.provider}:{self.provider_tx_id} "
            f"state={self.state}>"
        )


# ----------------------------------------------------------- Usage records


class BillingUsageMetric(str, enum.Enum):
    """Quota metrics tracked per user / billing period.

    Period granularity differs by metric:

    * ``ASSESSMENTS_PER_DAY`` — period key is ``YYYY-MM-DD`` (UTC).
    * ``AI_ANALYSES_PER_MONTH`` — period key is ``YYYY-MM``.
    * ``CHILDREN_TOTAL`` — period key is the literal ``"total"`` (no
      reset; counts the lifetime number of children created).
    """

    ASSESSMENTS_PER_DAY = "assessments_per_day"
    AI_ANALYSES_PER_MONTH = "ai_analyses_per_month"
    CHILDREN_TOTAL = "children_total"


class BillingUsageRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Counter row for a (user, metric, period_key) tuple.

    Rows are upserted via INSERT … ON CONFLICT in
    :func:`app.services.billing.usage.increment_usage`, so the unique
    constraint below is critical to prevent duplicate counters under
    load.
    """

    __tablename__ = "billing_usage_records"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "metric",
            "period_key",
            name="uq_billing_usage_user_metric_period",
        ),
    )

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    metric: Mapped[str] = mapped_column(
        String(40), nullable=False, index=True
    )
    # ``YYYY-MM-DD`` for daily metrics, ``YYYY-MM`` for monthly,
    # ``"total"`` for non-resetting metrics.
    period_key: Mapped[str] = mapped_column(
        String(16), nullable=False, index=True
    )
    count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    user: Mapped[User] = relationship("User", lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return (
            f"<BillingUsageRecord user={self.user_id} "
            f"metric={self.metric} period={self.period_key} "
            f"count={self.count}>"
        )


__all__ = [
    "BillingPlan",
    "BillingPlanCode",
    "BillingUsageMetric",
    "BillingUsageRecord",
    "PaymentOrder",
    "PaymentOrderState",
    "PaymentProvider",
    "PaymentTransaction",
    "PaymentTransactionState",
    "Subscription",
    "SubscriptionStatus",
]
