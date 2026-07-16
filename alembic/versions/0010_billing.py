"""Add billing tables: plans, subscriptions, orders, transactions.

The migration is fully additive. Existing user / kindergarten / child
rows are untouched and free users (no subscription row) keep working
unchanged.

Revision ID: 0010_billing
Revises: 0009_phoneme_mastery
Create Date: 2026-06-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_billing"
down_revision: str | None = "0009_phoneme_mastery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "billing_plans",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name_uz", sa.String(length=120), nullable=False),
        sa.Column("name_ru", sa.String(length=120), nullable=False),
        sa.Column("description_uz", sa.Text(), nullable=True),
        sa.Column("description_ru", sa.Text(), nullable=True),
        sa.Column(
            "price_tiyin",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "duration_days",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
        sa.Column("features", sa.JSON(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "sort_order",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("code", name="uq_billing_plans_code"),
    )
    op.create_index("ix_billing_plans_code", "billing_plans", ["code"])

    op.create_table(
        "payment_orders",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("plan_code", sa.String(length=32), nullable=False),
        sa.Column("amount_tiyin", sa.BigInteger(), nullable=False),
        sa.Column(
            "state",
            sa.String(length=20),
            nullable=False,
            server_default="created",
        ),
        sa.Column("provider", sa.String(length=20), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_payment_orders_user_id", "payment_orders", ["user_id"]
    )
    op.create_index(
        "ix_payment_orders_plan_code", "payment_orders", ["plan_code"]
    )
    op.create_index(
        "ix_payment_orders_state", "payment_orders", ["state"]
    )
    op.create_index(
        "ix_payment_orders_provider", "payment_orders", ["provider"]
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("plan_code", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="active",
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "auto_renew",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_order_id",
            sa.String(length=36),
            sa.ForeignKey("payment_orders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_subscriptions_user_id", "subscriptions", ["user_id"]
    )
    op.create_index(
        "ix_subscriptions_plan_code", "subscriptions", ["plan_code"]
    )
    op.create_index(
        "ix_subscriptions_status", "subscriptions", ["status"]
    )
    op.create_index(
        "ix_subscriptions_expires_at", "subscriptions", ["expires_at"]
    )

    op.create_table(
        "payment_transactions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "order_id",
            sa.String(length=36),
            sa.ForeignKey("payment_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("provider_tx_id", sa.String(length=64), nullable=False),
        sa.Column(
            "state",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("amount_tiyin", sa.BigInteger(), nullable=False),
        sa.Column("create_time_ms", sa.BigInteger(), nullable=True),
        sa.Column("perform_time_ms", sa.BigInteger(), nullable=True),
        sa.Column("cancel_time_ms", sa.BigInteger(), nullable=True),
        sa.Column("cancel_reason", sa.Integer(), nullable=True),
        sa.Column("raw_payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_payment_transactions_order_id",
        "payment_transactions",
        ["order_id"],
    )
    op.create_index(
        "ix_payment_transactions_provider",
        "payment_transactions",
        ["provider"],
    )
    op.create_index(
        "ix_payment_transactions_provider_tx_id",
        "payment_transactions",
        ["provider_tx_id"],
    )
    op.create_index(
        "ix_payment_transactions_state",
        "payment_transactions",
        ["state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_payment_transactions_state", table_name="payment_transactions"
    )
    op.drop_index(
        "ix_payment_transactions_provider_tx_id",
        table_name="payment_transactions",
    )
    op.drop_index(
        "ix_payment_transactions_provider",
        table_name="payment_transactions",
    )
    op.drop_index(
        "ix_payment_transactions_order_id",
        table_name="payment_transactions",
    )
    op.drop_table("payment_transactions")

    op.drop_index(
        "ix_subscriptions_expires_at", table_name="subscriptions"
    )
    op.drop_index("ix_subscriptions_status", table_name="subscriptions")
    op.drop_index("ix_subscriptions_plan_code", table_name="subscriptions")
    op.drop_index("ix_subscriptions_user_id", table_name="subscriptions")
    op.drop_table("subscriptions")

    op.drop_index("ix_payment_orders_provider", table_name="payment_orders")
    op.drop_index("ix_payment_orders_state", table_name="payment_orders")
    op.drop_index("ix_payment_orders_plan_code", table_name="payment_orders")
    op.drop_index("ix_payment_orders_user_id", table_name="payment_orders")
    op.drop_table("payment_orders")

    op.drop_index("ix_billing_plans_code", table_name="billing_plans")
    op.drop_table("billing_plans")
