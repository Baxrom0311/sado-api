"""Exercise catalogue + child assignments.

Revision ID: 0003_exercises
Revises: 0002_assessments
Create Date: 2026-05-27
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_exercises"
down_revision: str | None = "0002_assessments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---------------------------------------------------------- exercises
    op.create_table(
        "exercises",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(length=32), nullable=False, server_default="articulation"),
        sa.Column("age_group", sa.String(length=16), nullable=False, server_default="4-5"),
        sa.Column("difficulty", sa.String(length=16), nullable=False, server_default="easy"),
        sa.Column("language", sa.String(length=8), nullable=False, server_default="uz"),
        sa.Column("duration_minutes", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("audio_example_path", sa.String(length=500), nullable=True),
        sa.Column("image_path", sa.String(length=500), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("target_phonemes", sa.String(length=200), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_by_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_exercises_category", "exercises", ["category"])
    op.create_index("ix_exercises_age_group", "exercises", ["age_group"])
    op.create_index("ix_exercises_difficulty", "exercises", ["difficulty"])
    op.create_index("ix_exercises_language", "exercises", ["language"])
    op.create_index("ix_exercises_is_active", "exercises", ["is_active"])
    op.create_index("ix_exercises_created_by_id", "exercises", ["created_by_id"])

    # ---------------------------------------------------- exercise_assignments
    op.create_table(
        "exercise_assignments",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "child_id",
            sa.String(length=36),
            sa.ForeignKey("children.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "exercise_id",
            sa.String(length=36),
            sa.ForeignKey("exercises.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "assigned_by_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("due_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "child_id",
            "exercise_id",
            "due_date",
            name="uq_exercise_assignment_child_ex_due",
        ),
    )
    op.create_index(
        "ix_exercise_assignments_child_id", "exercise_assignments", ["child_id"]
    )
    op.create_index(
        "ix_exercise_assignments_exercise_id",
        "exercise_assignments",
        ["exercise_id"],
    )
    op.create_index(
        "ix_exercise_assignments_assigned_by_id",
        "exercise_assignments",
        ["assigned_by_id"],
    )
    op.create_index(
        "ix_exercise_assignments_status", "exercise_assignments", ["status"]
    )
    op.create_index(
        "ix_exercise_assignments_due_date",
        "exercise_assignments",
        ["due_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_exercise_assignments_due_date", table_name="exercise_assignments"
    )
    op.drop_index(
        "ix_exercise_assignments_status", table_name="exercise_assignments"
    )
    op.drop_index(
        "ix_exercise_assignments_assigned_by_id",
        table_name="exercise_assignments",
    )
    op.drop_index(
        "ix_exercise_assignments_exercise_id",
        table_name="exercise_assignments",
    )
    op.drop_index(
        "ix_exercise_assignments_child_id", table_name="exercise_assignments"
    )
    op.drop_table("exercise_assignments")

    op.drop_index("ix_exercises_created_by_id", table_name="exercises")
    op.drop_index("ix_exercises_is_active", table_name="exercises")
    op.drop_index("ix_exercises_language", table_name="exercises")
    op.drop_index("ix_exercises_difficulty", table_name="exercises")
    op.drop_index("ix_exercises_age_group", table_name="exercises")
    op.drop_index("ix_exercises_category", table_name="exercises")
    op.drop_table("exercises")
