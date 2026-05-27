"""Alembic environment for async SQLAlchemy 2.0.

Reads the database URL from :class:`app.config.Settings` so the
migration tooling honours the same env file as the application.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app import models  # noqa: F401  -- ensures all models are registered
from app.config import get_settings
from app.database import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the runtime URL so alembic.ini can stay credential-free.
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without a live DB connection (emit SQL)."""

    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_migrations_online_async() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = config.get_main_option("sqlalchemy.url")
    engine = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        future=True,
    )
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    """Run migrations against a live async DB connection.

    When invoked from inside a running event loop (e.g. from a pytest
    coroutine), schedule the work on a worker thread so we don't hit
    ``asyncio.run() cannot be called from a running event loop``.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_run_migrations_online_async())
        return

    import threading

    error: list[BaseException] = []

    def _runner() -> None:
        try:
            asyncio.run(_run_migrations_online_async())
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
