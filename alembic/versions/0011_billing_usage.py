"""Add billing usage records table for quota tracking.

Adds the ``billing_usage_records`` table used by
:mod:`app.services.billing.usage` to count metric usage per
``(user, metric, period_key)`` tuple. The table is purely additive —
no existing data is touched and nothing in the legacy code path reads
or writes it. Quota enforcement is gated behind the
``BILLING_ENFORCE_QUOTAS`` flag, so adding the table is a no-op for
free users until enforcement is flipped on.

Revision ID: 0011_billing_usage
Revises: 0010_billing
Create Date: 2026-06-13
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_billing_usage"
down_revision: str | None = "0010_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "billing_usage_records",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("metric", sa.String(length=40), nullable=False),
        sa.Column("period_key", sa.String(length=16), nullable=False),
        sa.Column(
            "count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "metric",
            "period_key",
            name="uq_billing_usage_user_metric_period",
        ),
    )
    op.create_index(
        "ix_billing_usage_records_user_id",
        "billing_usage_records",
        ["user_id"],
    )
    op.create_index(
        "ix_billing_usage_records_metric",
        "billing_usage_records",
        ["metric"],
    )
    op.create_index(
        "ix_billing_usage_records_period_key",
        "billing_usage_records",
        ["period_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_billing_usage_records_period_key",
        table_name="billing_usage_records",
    )
    op.drop_index(
        "ix_billing_usage_records_metric",
        table_name="billing_usage_records",
    )
    op.drop_index(
        "ix_billing_usage_records_user_id",
        table_name="billing_usage_records",
    )
    op.drop_table("billing_usage_records")
