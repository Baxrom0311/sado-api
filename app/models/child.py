"""Child model — every assessment is owned by a child profile."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._base import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:  # pragma: no cover
    from app.models.kindergarten import Kindergarten
    from app.models.user import User


class Child(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A child registered by a parent or kindergarten."""

    __tablename__ = "children"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    birth_date: Mapped[date] = mapped_column(Date, nullable=False)
    gender: Mapped[str] = mapped_column(String(10), nullable=False, default="unknown")
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="uz")
    notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    parent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kindergarten_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("kindergartens.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    parent: Mapped[User] = relationship("User", back_populates="children")
    kindergarten: Mapped[Kindergarten | None] = relationship(
        "Kindergarten", back_populates="children"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"<Child {self.name} dob={self.birth_date}>"
