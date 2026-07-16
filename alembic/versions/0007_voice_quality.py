"""Voice-quality + recommendations columns on analysis_results.

Adds two nullable JSON columns:

* ``voice_quality`` — clinical metrics (jitter, shimmer, HNR, speech
  rate) computed by :mod:`app.services.voice_quality`.
* ``recommendations`` — list of localised therapist-style hints
  produced by :mod:`app.services.recommendations` based on the
  acoustic features and risk level.

Both columns are nullable so existing :class:`AnalysisResult` rows
remain valid (backwards-compatible upgrade).

Revision ID: 0007_voice_quality
Revises: 0006_multi_tenant
Create Date: 2026-06-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_voice_quality"
down_revision: str | None = "0006_multi_tenant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``voice_quality`` and ``recommendations`` columns."""

    with op.batch_alter_table("analysis_results", recreate="auto") as batch:
        batch.add_column(
            sa.Column("voice_quality", sa.JSON(), nullable=True)
        )
        batch.add_column(
            sa.Column("recommendations", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    """Drop the new columns."""

    with op.batch_alter_table("analysis_results", recreate="auto") as batch:
        batch.drop_column("recommendations")
        batch.drop_column("voice_quality")
