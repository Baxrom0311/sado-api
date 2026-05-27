"""Kindergarten model — institutions where children are screened."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._base import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:  # pragma: no cover
    from app.models.child import Child
    from app.models.region import Region


class Kindergarten(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A kindergarten / preschool institution."""

    __tablename__ = "kindergartens"

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)

    teacher_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    child_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    region_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("regions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    region: Mapped[Region | None] = relationship(
        "Region", back_populates="kindergartens", lazy="joined"
    )
    children: Mapped[list[Child]] = relationship(
        "Child", back_populates="kindergarten"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"<Kindergarten {self.name}>"
