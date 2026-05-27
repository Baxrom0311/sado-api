"""Shared SQLAlchemy column helpers and base mixins.

We keep this lightweight — UUIDv4 primary keys (stored as strings for
SQLite portability), created/updated timestamps, and a soft-delete
flag are reused across every table.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column


def _uuid4_str() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class UUIDPrimaryKeyMixin:
    """36-char UUIDv4 primary key — Postgres + SQLite portable."""

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_uuid4_str,
    )


class TimestampMixin:
    """``created_at`` / ``updated_at`` columns set in application code."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )
