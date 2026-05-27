"""Shared pytest fixtures for the sado-api test-suite.

Tests run against the FastAPI app with the SQLite + in-memory defaults,
so no external services (Postgres, Redis, MinIO) are required.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio

# Set test-friendly defaults BEFORE the app module is imported. Settings
# are cached via lru_cache so this must happen at import time.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("JWT_SECRET", "test-secret-which-is-long-enough-1234567890")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_sado.db")
os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "true")
# Loose rate limit so each test can issue plenty of /auth requests.
os.environ.setdefault("RATE_LIMIT_AUTH_PER_MINUTE", "1000")
# Keep the storage backend isolated per test session.
_STORAGE_DIR = tempfile.mkdtemp(prefix="sado-storage-")
os.environ.setdefault("LOCAL_STORAGE_DIR", _STORAGE_DIR)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest_asyncio.fixture()
async def app():
    """Return a fresh FastAPI app instance with a clean schema."""

    from app.config import get_settings
    from app.core.rate_limit import reset_auth_rate_limiter
    from app.database import create_all, drop_all, reset_engine
    from app.main import create_app
    from app.services.auth import get_deny_list
    from app.services.storage import reset_audio_storage

    get_settings.cache_clear()
    await reset_engine()
    await reset_auth_rate_limiter()
    await get_deny_list().clear()
    reset_audio_storage()
    await create_all()
    try:
        yield create_app()
    finally:
        await drop_all()
        await reset_engine()
        reset_audio_storage()


@pytest_asyncio.fixture()
async def client(app) -> AsyncIterator:
    """Async HTTP client wired to the in-process FastAPI app."""

    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _cleanup_test_db() -> Iterator[None]:
    """Remove the SQLite test database file after each test session run."""

    yield
    for path in ("./test_sado.db", "./test_sado.db-journal"):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    # Wipe storage between tests to keep keys isolated.
    if os.path.isdir(_STORAGE_DIR):
        for entry in os.listdir(_STORAGE_DIR):
            full = os.path.join(_STORAGE_DIR, entry)
            try:
                if os.path.isdir(full):
                    shutil.rmtree(full)
                else:
                    os.remove(full)
            except FileNotFoundError:
                pass
