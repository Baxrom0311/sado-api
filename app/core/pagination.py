"""Cursor-based pagination helpers.

We use base64-encoded ``(created_at_iso, id)`` tuples so cursors are
stable across servers, copy-paste safe, and don't leak DB ids in their
raw form. The encoding is intentionally simple and self-describing so
clients can debug it.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class Page(BaseModel, Generic[T]):
    """A single page of a cursor-paginated list."""

    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False
    total: int | None = Field(
        default=None,
        description="Optional total — populated when an exact count is cheap.",
    )


def encode_cursor(created_at: datetime, item_id: str) -> str:
    raw = json.dumps({"ts": created_at.isoformat(), "id": item_id})
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Return ``(created_at, id)`` decoded from an opaque cursor.

    Raises ``ValueError`` if the cursor is malformed so callers can map
    that to a 400 response.
    """

    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        data: dict[str, Any] = json.loads(raw)
        return datetime.fromisoformat(data["ts"]), str(data["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("Invalid pagination cursor") from exc


def clamp_limit(value: int | None) -> int:
    if value is None or value <= 0:
        return DEFAULT_PAGE_SIZE
    return min(value, MAX_PAGE_SIZE)
