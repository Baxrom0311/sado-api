"""Add practice_plans + practice_plan_items tables.

These tables back the new /practice-plans endpoints. They are fully
independent of existing assessments / exercises / children rows
(only FKs in, never FKs out from those existing tables) so the
migration is non-destructive and backwards compatible.

Revision ID: 0008_practice_plans
Revises: 0007_voice_quality
Create Date: 2026-06-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_practice_plans"
down_revision: str | None = "0007_voice_quality"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the practice_plans + practice_plan_items tables."""

    op.create_table(
        "practice_plans",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "child_id",
            sa.String(length=36),
            sa.ForeignKey("children.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "assessment_id",
            sa.String(length=36),
            sa.ForeignKey("assessments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_by_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "tenant_id",
            sa.String(length=36),
            sa.ForeignKey("kindergartens.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="draft",
        ),
        sa.Column(
            "locale", sa.String(length=8), nullable=False, server_default="uz"
        ),
        sa.Column("focus_areas", sa.JSON(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_practice_plans_child_id", "practice_plans", ["child_id"]
    )
    op.create_index(
        "ix_practice_plans_assessment_id",
        "practice_plans",
        ["assessment_id"],
    )
    op.create_index(
        "ix_practice_plans_created_by_id",
        "practice_plans",
        ["created_by_id"],
    )
    op.create_index(
        "ix_practice_plans_tenant_id", "practice_plans", ["tenant_id"]
    )
    op.create_index(
        "ix_practice_plans_status", "practice_plans", ["status"]
    )

    op.create_table(
        "practice_plan_items",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(length=36),
            sa.ForeignKey("practice_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "exercise_id",
            sa.String(length=36),
            sa.ForeignKey("exercises.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "priority", sa.Integer(), nullable=False, server_default="3"
        ),
        sa.Column(
            "target_count", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column(
            "completed_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("focus_code", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_practice_plan_items_plan_id",
        "practice_plan_items",
        ["plan_id"],
    )
    op.create_index(
        "ix_practice_plan_items_exercise_id",
        "practice_plan_items",
        ["exercise_id"],
    )
    op.create_index(
        "ix_practice_plan_items_status",
        "practice_plan_items",
        ["status"],
    )


def downgrade() -> None:
    """Drop the practice-plan tables."""

    op.drop_index(
        "ix_practice_plan_items_status", table_name="practice_plan_items"
    )
    op.drop_index(
        "ix_practice_plan_items_exercise_id",
        table_name="practice_plan_items",
    )
    op.drop_index(
        "ix_practice_plan_items_plan_id",
        table_name="practice_plan_items",
    )
    op.drop_table("practice_plan_items")

    op.drop_index("ix_practice_plans_status", table_name="practice_plans")
    op.drop_index("ix_practice_plans_tenant_id", table_name="practice_plans")
    op.drop_index(
        "ix_practice_plans_created_by_id", table_name="practice_plans"
    )
    op.drop_index(
        "ix_practice_plans_assessment_id", table_name="practice_plans"
    )
    op.drop_index("ix_practice_plans_child_id", table_name="practice_plans")
    op.drop_table("practice_plans")
