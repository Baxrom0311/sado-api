"""Multi-tenant architecture — tenant_id columns + tenant_settings table.

Adds ``tenant_id`` (FK ``kindergartens.id``, nullable) to ``users``,
``children``, ``assessments``, and ``exercises`` so every record is
attributable to a specific kindergarten/organization. ``NULL`` is
preserved for backward compatibility — legacy data and global system
exercises remain visible to every tenant.

Also creates ``tenant_settings`` to hold per-tenant feature flags and
quotas (subscription plan, max children, AI-analysis toggle, etc.).

Revision ID: 0006_multi_tenant
Revises: 0005_gamification
Create Date: 2026-06-11
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_multi_tenant"
down_revision: str | None = "0005_gamification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_tenant_column(table: str, *, fk_name: str, ix_name: str) -> None:
    """Add ``tenant_id`` to ``table`` with FK + index in a batch op.

    Uses a batch operation so the migration works on SQLite (which does
    not support ``ALTER TABLE ADD COLUMN ... REFERENCES`` directly).
    """

    with op.batch_alter_table(table, recreate="auto") as batch:
        batch.add_column(
            sa.Column(
                "tenant_id",
                sa.String(length=36),
                sa.ForeignKey("kindergartens.id", ondelete="SET NULL", name=fk_name),
                nullable=True,
            )
        )
    op.create_index(ix_name, table, ["tenant_id"])


def _drop_tenant_column(table: str, *, ix_name: str) -> None:
    op.drop_index(ix_name, table_name=table)
    with op.batch_alter_table(table, recreate="auto") as batch:
        batch.drop_column("tenant_id")


def upgrade() -> None:
    # ----------------------------------------------------- tenant_id columns
    _add_tenant_column(
        "users",
        fk_name="fk_users_tenant_id_kindergartens",
        ix_name="ix_users_tenant_id",
    )
    _add_tenant_column(
        "children",
        fk_name="fk_children_tenant_id_kindergartens",
        ix_name="ix_children_tenant_id",
    )
    _add_tenant_column(
        "assessments",
        fk_name="fk_assessments_tenant_id_kindergartens",
        ix_name="ix_assessments_tenant_id",
    )
    _add_tenant_column(
        "exercises",
        fk_name="fk_exercises_tenant_id_kindergartens",
        ix_name="ix_exercises_tenant_id",
    )

    # ---------------------------------------------------- tenant_settings
    op.create_table(
        "tenant_settings",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(length=36),
            sa.ForeignKey("kindergartens.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "max_children", sa.Integer(), nullable=False, server_default="100"
        ),
        sa.Column(
            "max_users", sa.Integer(), nullable=False, server_default="20"
        ),
        sa.Column(
            "ai_analysis_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "custom_exercises_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "subscription_plan",
            sa.String(length=20),
            nullable=False,
            server_default="free",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", name="uq_tenant_settings_tenant_id"),
    )
    op.create_index(
        "ix_tenant_settings_tenant_id", "tenant_settings", ["tenant_id"]
    )
    op.create_index(
        "ix_tenant_settings_subscription_plan",
        "tenant_settings",
        ["subscription_plan"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tenant_settings_subscription_plan", table_name="tenant_settings"
    )
    op.drop_index(
        "ix_tenant_settings_tenant_id", table_name="tenant_settings"
    )
    op.drop_table("tenant_settings")

    _drop_tenant_column("exercises", ix_name="ix_exercises_tenant_id")
    _drop_tenant_column("assessments", ix_name="ix_assessments_tenant_id")
    _drop_tenant_column("children", ix_name="ix_children_tenant_id")
    _drop_tenant_column("users", ix_name="ix_users_tenant_id")
