"""Tenant (multi-tenant) settings model.

In SADO, a ``tenant`` is a kindergarten / organization. Every user,
child, assessment, and (custom) exercise can be scoped to one. The
:class:`TenantSettings` row captures per-tenant feature flags and
quota — created on demand for any kindergarten that is treated as a
fully fledged tenant.

The ``Kindergarten`` table itself remains the source of truth for the
tenant identity (id, name, region). Settings sit in a side-table to
keep migrations cheap and to allow new flags without touching the
kindergartens schema.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._base import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:  # pragma: no cover
    from app.models.kindergarten import Kindergarten


class SubscriptionPlan(str, enum.Enum):
    """Pricing plan a tenant is on. Drives quotas and feature flags."""

    FREE = "free"
    BASIC = "basic"
    PREMIUM = "premium"


class TenantSettings(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per tenant (kindergarten) with feature flags + quotas."""

    __tablename__ = "tenant_settings"

    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("kindergartens.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    max_children: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100
    )
    max_users: Mapped[int] = mapped_column(
        Integer, nullable=False, default=20
    )
    ai_analysis_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    custom_exercises_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    subscription_plan: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=SubscriptionPlan.FREE.value,
        index=True,
    )

    tenant: Mapped[Kindergarten] = relationship(
        "Kindergarten", lazy="joined"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return (
            f"<TenantSettings tenant={self.tenant_id} "
            f"plan={self.subscription_plan}>"
        )


__all__ = ["SubscriptionPlan", "TenantSettings"]
