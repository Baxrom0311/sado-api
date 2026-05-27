"""Tests for the database layer — engine creation, model registration,
schema bootstrap, and the initial Alembic migration.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.database import Base, create_all, drop_all, get_sessionmaker, reset_engine
from app.models import Child, Kindergarten, Region, RegionType, User, UserRole


@pytest.fixture(autouse=True)
async def _fresh_db() -> None:
    await reset_engine()
    await create_all()
    yield
    await drop_all()
    await reset_engine()


def test_models_register_on_metadata() -> None:
    table_names = set(Base.metadata.tables)
    assert {"users", "regions", "kindergartens", "children"}.issubset(table_names)


async def test_can_persist_full_object_graph() -> None:
    factory = get_sessionmaker()
    async with factory() as session:
        country = Region(name="Uzbekistan", type=RegionType.COUNTRY.value, code="UZ")
        viloyat = Region(name="Toshkent shahri", type=RegionType.REGION.value, parent=country)
        kg = Kindergarten(name="DMTI 1", region=viloyat, address="Amir Temur 1")
        parent = User(
            email="parent@example.com",
            password_hash="hash",
            full_name="Test Parent",
            role=UserRole.PARENT.value,
            region=viloyat,
        )
        kid = Child(
            name="Aliya",
            birth_date=date(2020, 5, 1),
            gender="female",
            language="uz",
            parent=parent,
            kindergarten=kg,
        )
        session.add_all([country, viloyat, kg, parent, kid])
        await session.commit()

    async with factory() as session:
        rows = (
            await session.execute(
                select(Child).where(Child.name == "Aliya")
            )
        ).scalars().all()
        assert len(rows) == 1
        loaded = rows[0]
        assert loaded.parent_id == parent.id
        assert loaded.kindergarten_id == kg.id


async def test_alembic_migration_applies_on_sqlite(tmp_path, monkeypatch) -> None:
    """The hand-written 0001 migration runs cleanly on a fresh DB."""

    db_path = tmp_path / "alembic.db"
    url = f"sqlite+aiosqlite:///{db_path}"

    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("JWT_SECRET", "test-secret-which-is-long-enough-1234567890")

    from app.config import get_settings

    get_settings.cache_clear()
    await reset_engine()

    # Run the migration through Alembic's Python API.
    import os

    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")

    # After upgrade, the engine should see the same tables we declare.
    from sqlalchemy import inspect

    from app.database import get_engine

    def _list(connection):  # type: ignore[no-untyped-def]
        return inspect(connection).get_table_names()

    engine = get_engine()
    async with engine.connect() as conn:
        names = await conn.run_sync(_list)
    assert {"users", "regions", "kindergartens", "children"}.issubset(set(names))

    command.downgrade(cfg, "base")
    async with engine.connect() as conn:
        names = await conn.run_sync(_list)
    assert {"users", "regions", "kindergartens", "children"}.isdisjoint(set(names))

    await reset_engine()
