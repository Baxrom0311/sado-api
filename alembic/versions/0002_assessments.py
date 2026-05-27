"""Assessment domain — assessments, audio_recordings, analysis_results.

Revision ID: 0002_assessments
Revises: 0001_initial
Create Date: 2026-05-27
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_assessments"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---------------------------------------------------------- assessments
    op.create_table(
        "assessments",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "child_id",
            sa.String(length=36),
            sa.ForeignKey("children.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("type", sa.String(length=20), nullable=False, server_default="screening"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("overall_risk", sa.String(length=10), nullable=True),
        sa.Column("overall_confidence", sa.Float(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_assessments_child_id", "assessments", ["child_id"])
    op.create_index("ix_assessments_created_by_id", "assessments", ["created_by_id"])
    op.create_index("ix_assessments_status", "assessments", ["status"])

    # ----------------------------------------------------- audio_recordings
    op.create_table(
        "audio_recordings",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "assessment_id",
            sa.String(length=36),
            sa.ForeignKey("assessments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_type", sa.String(length=32), nullable=False, server_default="repeat_word"),
        sa.Column("prompt", sa.String(length=500), nullable=True),
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False, server_default="audio/wav"),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_sec", sa.Float(), nullable=True),
        sa.Column("sample_rate", sa.Integer(), nullable=True),
        sa.Column("processed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("processing_error", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_audio_recordings_assessment_id", "audio_recordings", ["assessment_id"]
    )

    # ---------------------------------------------------- analysis_results
    op.create_table(
        "analysis_results",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "recording_id",
            sa.String(length=36),
            sa.ForeignKey("audio_recordings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("risk_level", sa.String(length=10), nullable=False, server_default="green"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("mfcc_features", sa.JSON(), nullable=True),
        sa.Column("pitch_data", sa.JSON(), nullable=True),
        sa.Column("formant_data", sa.JSON(), nullable=True),
        sa.Column("phoneme_scores", sa.JSON(), nullable=True),
        sa.Column("feature_summary", sa.JSON(), nullable=True),
        sa.Column("model_name", sa.String(length=100), nullable=False, server_default="mock-xgb-v1"),
        sa.Column("model_version", sa.String(length=40), nullable=False, server_default="0.1.0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("recording_id", name="uq_analysis_results_recording_id"),
    )
    op.create_index(
        "ix_analysis_results_recording_id", "analysis_results", ["recording_id"]
    )
    op.create_index(
        "ix_analysis_results_risk_level", "analysis_results", ["risk_level"]
    )


def downgrade() -> None:
    op.drop_index("ix_analysis_results_risk_level", table_name="analysis_results")
    op.drop_index("ix_analysis_results_recording_id", table_name="analysis_results")
    op.drop_table("analysis_results")

    op.drop_index("ix_audio_recordings_assessment_id", table_name="audio_recordings")
    op.drop_table("audio_recordings")

    op.drop_index("ix_assessments_status", table_name="assessments")
    op.drop_index("ix_assessments_created_by_id", table_name="assessments")
    op.drop_index("ix_assessments_child_id", table_name="assessments")
    op.drop_table("assessments")
