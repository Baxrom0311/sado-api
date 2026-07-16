"""Gamification — XP, levels, streaks, and badges.

Revision ID: 0005_gamification
Revises: 0004_notifications
Create Date: 2026-06-11
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_gamification"
down_revision: str | None = "0004_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---------------------------------------------------- gamification
    op.create_table(
        "gamification",
        sa.Column(
            "child_id",
            sa.String(length=36),
            sa.ForeignKey("children.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("total_xp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("level", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("streak_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("longest_streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_activity_date", sa.Date(), nullable=True),
        sa.Column(
            "total_exercises_completed",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "total_assessments_completed",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_gamification_total_xp", "gamification", ["total_xp"])
    op.create_index("ix_gamification_level", "gamification", ["level"])
    op.create_index(
        "ix_gamification_last_activity_date", "gamification", ["last_activity_date"]
    )

    # --------------------------------------------------------- badges
    op.create_table(
        "badges",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("title_uz", sa.String(length=120), nullable=False),
        sa.Column("title_ru", sa.String(length=120), nullable=False, server_default=""),
        sa.Column(
            "description_uz", sa.String(length=500), nullable=False, server_default=""
        ),
        sa.Column(
            "description_ru", sa.String(length=500), nullable=False, server_default=""
        ),
        sa.Column("icon", sa.String(length=64), nullable=False, server_default="🏅"),
        sa.Column(
            "category", sa.String(length=20), nullable=False, server_default="milestone"
        ),
        sa.Column(
            "requirement_type",
            sa.String(length=32),
            nullable=False,
            server_default="xp",
        ),
        sa.Column("threshold", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("code", name="uq_badges_code"),
    )
    op.create_index("ix_badges_code", "badges", ["code"])
    op.create_index("ix_badges_category", "badges", ["category"])
    op.create_index(
        "ix_badges_requirement_type", "badges", ["requirement_type"]
    )
    op.create_index("ix_badges_is_active", "badges", ["is_active"])

    # ------------------------------------------------- badge_earnings
    op.create_table(
        "badge_earnings",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "child_id",
            sa.String(length=36),
            sa.ForeignKey("children.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "badge_id",
            sa.String(length=36),
            sa.ForeignKey("badges.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("earned_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "child_id", "badge_id", name="uq_badge_earnings_child_badge"
        ),
    )
    op.create_index(
        "ix_badge_earnings_child_id", "badge_earnings", ["child_id"]
    )
    op.create_index(
        "ix_badge_earnings_badge_id", "badge_earnings", ["badge_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_badge_earnings_badge_id", table_name="badge_earnings")
    op.drop_index("ix_badge_earnings_child_id", table_name="badge_earnings")
    op.drop_table("badge_earnings")

    op.drop_index("ix_badges_is_active", table_name="badges")
    op.drop_index("ix_badges_requirement_type", table_name="badges")
    op.drop_index("ix_badges_category", table_name="badges")
    op.drop_index("ix_badges_code", table_name="badges")
    op.drop_table("badges")

    op.drop_index(
        "ix_gamification_last_activity_date", table_name="gamification"
    )
    op.drop_index("ix_gamification_level", table_name="gamification")
    op.drop_index("ix_gamification_total_xp", table_name="gamification")
    op.drop_table("gamification")
