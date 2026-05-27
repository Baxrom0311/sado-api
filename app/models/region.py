"""Geographic region model — country / region (viloyat) / district."""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._base import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:  # pragma: no cover
    from app.models.kindergarten import Kindergarten
    from app.models.user import User


class RegionType(str, enum.Enum):
    COUNTRY = "country"
    REGION = "region"
    DISTRICT = "district"


class Region(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A hierarchical geographic region."""

    __tablename__ = "regions"

    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    code: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=RegionType.REGION.value,
    )
    parent_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("regions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    parent: Mapped[Region | None] = relationship(
        "Region",
        remote_side="Region.id",
        back_populates="children",
    )
    children: Mapped[list[Region]] = relationship(
        "Region",
        back_populates="parent",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    users: Mapped[list[User]] = relationship("User", back_populates="region")
    kindergartens: Mapped[list[Kindergarten]] = relationship(
        "Kindergarten", back_populates="region"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"<Region {self.name} type={self.type}>"
