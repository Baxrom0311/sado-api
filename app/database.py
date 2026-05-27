"""Async SQLAlchemy 2.0 engine, session factory, and ``Base`` class.

The module exposes:

* :data:`Base` — declarative base every ORM model inherits from.
* :func:`get_engine` / :func:`get_sessionmaker` — cached factories that
  read the URL from :class:`app.config.Settings`.
* :func:`get_session` — FastAPI dependency yielding an ``AsyncSession``.
* :func:`reset_engine` — used by tests to dispose and recreate the
  engine after settings change.

The default URL points at a SQLite file so the API runs without
Postgres. Production deployments override ``DATABASE_URL``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import Settings, get_settings


class Base(DeclarativeBase):
    """Declarative base shared by every model."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _engine_kwargs(settings: Settings) -> dict[str, Any]:
    """Driver-specific tuning for the async engine."""

    kwargs: dict[str, Any] = {
        "echo": False,
        "future": True,
        "pool_pre_ping": True,
    }
    # SQLite cannot share connections across threads safely; disable
    # pooling so each request gets a fresh connection.
    if settings.database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return kwargs


def get_engine() -> AsyncEngine:
    """Return the singleton async engine, creating it if necessary."""

    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(settings.database_url, **_engine_kwargs(settings))
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the singleton async session factory."""

    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
            autocommit=False,
        )
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a transactional ``AsyncSession``."""

    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def reset_engine() -> None:
    """Dispose engine + session factory. Used by tests after env changes."""

    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def create_all() -> None:
    """Create every table declared on :data:`Base`. Used in tests."""

    # Importing here forces all models to register on Base.metadata.
    from app import models  # noqa: F401

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_all() -> None:
    """Drop every table declared on :data:`Base`. Used in tests."""

    from app import models  # noqa: F401

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
