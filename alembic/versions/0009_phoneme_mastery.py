"""Add the phoneme_mastery aggregation table.

The table stores one row per ``(child_id, phoneme, language)`` and is
populated incrementally by
:func:`app.services.phoneme_mastery.update_mastery_from_assessment`
whenever an assessment finalises. It powers the new
``GET /children/{id}/phoneme-mastery`` and
``GET /children/{id}/speech-profile`` endpoints.

Backwards compatible: existing assessments / analyses are unaffected
and the mastery table simply starts empty until the next assessment
finalises.

Revision ID: 0009_phoneme_mastery
Revises: 0008_practice_plans
Create Date: 2026-06-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_phoneme_mastery"
down_revision: str | None = "0008_practice_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``phoneme_mastery`` table."""

    op.create_table(
        "phoneme_mastery",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "child_id",
            sa.String(length=36),
            sa.ForeignKey("children.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("phoneme", sa.String(length=8), nullable=False),
        sa.Column(
            "language",
            sa.String(length=8),
            nullable=False,
            server_default="uz",
        ),
        sa.Column(
            "total_attempts", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "successful_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "average_score",
            sa.Float(),
            nullable=False,
            server_default="0.0",
        ),
        sa.Column(
            "best_score",
            sa.Float(),
            nullable=False,
            server_default="0.0",
        ),
        sa.Column(
            "last_assessed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "mastered_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "child_id",
            "phoneme",
            "language",
            name="uq_phoneme_mastery_child_phoneme_lang",
        ),
    )
    op.create_index(
        "ix_phoneme_mastery_child_id",
        "phoneme_mastery",
        ["child_id"],
    )
    op.create_index(
        "ix_phoneme_mastery_phoneme",
        "phoneme_mastery",
        ["phoneme"],
    )
    op.create_index(
        "ix_phoneme_mastery_language",
        "phoneme_mastery",
        ["language"],
    )


def downgrade() -> None:
    """Drop the ``phoneme_mastery`` table."""

    op.drop_index("ix_phoneme_mastery_language", table_name="phoneme_mastery")
    op.drop_index("ix_phoneme_mastery_phoneme", table_name="phoneme_mastery")
    op.drop_index("ix_phoneme_mastery_child_id", table_name="phoneme_mastery")
    op.drop_table("phoneme_mastery")
