"""Pydantic schemas for the notifications inbox."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.pagination import Page


class NotificationCreate(BaseModel):
    """Admin-only payload for sending a notification to a user."""

    model_config = ConfigDict(str_strip_whitespace=True)

    user_id: str = Field(..., min_length=1, max_length=36)
    type: str = Field(default="system", max_length=40)
    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field(default="", max_length=4000)
    data: dict[str, Any] | None = None


class NotificationPublic(BaseModel):
    """Read-side schema for one notification."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    type: str
    title: str
    body: str
    data: dict[str, Any] | None
    read_at: datetime | None
    is_archived: bool
    created_at: datetime
    updated_at: datetime


NotificationPage = Page[NotificationPublic]


class UnreadCountResponse(BaseModel):
    """Tiny response for badge counters."""

    unread: int


__all__ = [
    "NotificationCreate",
    "NotificationPage",
    "NotificationPublic",
    "UnreadCountResponse",
]
