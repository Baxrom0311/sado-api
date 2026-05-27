"""User model — parents, teachers, therapists, and admins."""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._base import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:  # pragma: no cover
    from app.models.child import Child
    from app.models.region import Region


class UserRole(str, enum.Enum):
    """All supported principal roles. Stored as text for portability."""

    PARENT = "parent"
    TEACHER = "teacher"
    THERAPIST = "therapist"
    ADMIN = "admin"


class UserLanguage(str, enum.Enum):
    """User-preferred UI language."""

    UZ = "uz"
    RU = "ru"
    KK = "kk"
    EN = "en"


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A registered user of the platform."""

    __tablename__ = "users"

    email: Mapped[str | None] = mapped_column(
        String(255),
        unique=True,
        nullable=True,
        index=True,
    )
    phone: Mapped[str | None] = mapped_column(
        String(32),
        unique=True,
        nullable=True,
        index=True,
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=UserRole.PARENT.value,
        index=True,
    )
    language: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default=UserLanguage.UZ.value,
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    region_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("regions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    region: Mapped[Region | None] = relationship(
        "Region", back_populates="users", lazy="joined"
    )
    children: Mapped[list[Child]] = relationship(
        "Child",
        back_populates="parent",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        ident = self.email or self.phone or self.id
        return f"<User {ident} role={self.role}>"
