"""Initial schema — users, regions, kindergartens, children.

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-26
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ----------------------------------------------------------- regions
    op.create_table(
        "regions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=True),
        sa.Column("type", sa.String(length=20), nullable=False, server_default="region"),
        sa.Column(
            "parent_id",
            sa.String(length=36),
            sa.ForeignKey("regions.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_regions_name", "regions", ["name"])
    op.create_index("ix_regions_code", "regions", ["code"])
    op.create_index("ix_regions_parent_id", "regions", ["parent_id"])

    # ------------------------------------------------------------- users
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("role", sa.String(length=20), nullable=False, server_default="parent"),
        sa.Column("language", sa.String(length=8), nullable=False, server_default="uz"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "region_id",
            sa.String(length=36),
            sa.ForeignKey("regions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("phone", name="uq_users_phone"),
    )
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("ix_users_phone", "users", ["phone"])
    op.create_index("ix_users_role", "users", ["role"])
    op.create_index("ix_users_region_id", "users", ["region_id"])

    # ----------------------------------------------------- kindergartens
    op.create_table(
        "kindergartens",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("address", sa.String(length=500), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("teacher_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("child_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "region_id",
            sa.String(length=36),
            sa.ForeignKey("regions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_kindergartens_name", "kindergartens", ["name"])
    op.create_index("ix_kindergartens_region_id", "kindergartens", ["region_id"])

    # ---------------------------------------------------------- children
    op.create_table(
        "children",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("birth_date", sa.Date(), nullable=False),
        sa.Column("gender", sa.String(length=10), nullable=False, server_default="unknown"),
        sa.Column("language", sa.String(length=8), nullable=False, server_default="uz"),
        sa.Column("notes", sa.String(length=1000), nullable=True),
        sa.Column(
            "parent_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kindergarten_id",
            sa.String(length=36),
            sa.ForeignKey("kindergartens.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_children_parent_id", "children", ["parent_id"])
    op.create_index("ix_children_kindergarten_id", "children", ["kindergarten_id"])


def downgrade() -> None:
    op.drop_index("ix_children_kindergarten_id", table_name="children")
    op.drop_index("ix_children_parent_id", table_name="children")
    op.drop_table("children")

    op.drop_index("ix_kindergartens_region_id", table_name="kindergartens")
    op.drop_index("ix_kindergartens_name", table_name="kindergartens")
    op.drop_table("kindergartens")

    op.drop_index("ix_users_region_id", table_name="users")
    op.drop_index("ix_users_role", table_name="users")
    op.drop_index("ix_users_phone", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")

    op.drop_index("ix_regions_parent_id", table_name="regions")
    op.drop_index("ix_regions_code", table_name="regions")
    op.drop_index("ix_regions_name", table_name="regions")
    op.drop_table("regions")
