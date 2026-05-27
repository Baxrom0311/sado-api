"""Tests for the health check endpoints.

Covers both the happy path (liveness + service metadata) and the
"degraded" branches of ``/health/detailed`` — the per-service probes
must classify Redis / MinIO / database failures as ``skipped`` (when
unconfigured) or ``down`` (when unreachable) without raising.
"""

from __future__ import annotations

import pytest

# ``JWT_SECRET`` is set by conftest so the in-test ``Settings()`` calls
# below succeed even when we override unrelated fields.
_JWT_SECRET = "test-secret-which-is-long-enough-1234567890"

# A port nothing should be listening on. Picked at the reserved low end
# so a stray dev service is unlikely to clash.
_CLOSED_PORT = 1


@pytest.mark.asyncio
async def test_root_returns_service_metadata(client) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["service"]
    assert body["version"]
    assert body["docs"] == "/docs"
    assert body["health"].endswith("/health")


@pytest.mark.asyncio
async def test_health_liveness_ok(client) -> None:
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"]
    assert body["version"]
    assert body["environment"] == "test"
    assert "timestamp" in body


@pytest.mark.asyncio
async def test_health_response_includes_request_id_header(client) -> None:
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.headers.get("X-Request-ID")


@pytest.mark.asyncio
async def test_health_detailed_returns_service_breakdown(client) -> None:
    response = await client.get("/api/v1/health/detailed")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"ok", "degraded"}
    names = {svc["name"] for svc in body["services"]}
    assert {"database", "redis", "storage"}.issubset(names)
    for svc in body["services"]:
        assert svc["status"] in {"ok", "degraded", "down", "skipped"}


@pytest.mark.asyncio
async def test_unknown_route_returns_consistent_error(client) -> None:
    response = await client.get("/api/v1/this-route-does-not-exist")
    assert response.status_code == 404
    body = response.json()
    # FastAPI's default 404 keeps {"detail": ...} which our error
    # contract is compatible with.
    assert "detail" in body


@pytest.mark.asyncio
async def test_openapi_schema_lists_health(client) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    paths = schema.get("paths", {})
    assert "/api/v1/health" in paths
    assert "/api/v1/health/detailed" in paths


# --------------------------------------------------------------------------
# Per-probe degraded scenarios
#
# These tests exercise ``_check_database`` / ``_check_redis`` /
# ``_check_storage`` directly with crafted ``Settings`` objects so we can
# observe the failure branches without standing up real services.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_database_down_when_unreachable() -> None:
    """An invalid Postgres URL must be reported as ``down``, never raised."""

    from app.api.v1.health import _check_database
    from app.config import Settings

    settings = Settings(
        database_url=f"postgresql+asyncpg://sado:sado@127.0.0.1:{_CLOSED_PORT}/sado",
        jwt_secret=_JWT_SECRET,
    )
    result = await _check_database(settings)
    assert result.name == "database"
    assert result.status == "down"
    assert result.detail
    # Latency must be recorded so dashboards can plot probe duration.
    assert result.latency_ms is not None
    assert result.latency_ms >= 0


@pytest.mark.asyncio
async def test_check_redis_skipped_when_url_unset() -> None:
    """When ``REDIS_URL`` is not configured the probe is skipped, not failed."""

    from app.api.v1.health import _check_redis
    from app.config import Settings

    settings = Settings(redis_url=None, jwt_secret=_JWT_SECRET)
    result = await _check_redis(settings)
    assert result.name == "redis"
    assert result.status == "skipped"
    assert "REDIS_URL" in (result.detail or "")
    assert result.latency_ms is None


@pytest.mark.asyncio
async def test_check_redis_down_when_unreachable() -> None:
    """A closed port must be reported as ``down`` with a recorded latency."""

    from app.api.v1.health import _check_redis
    from app.config import Settings

    settings = Settings(
        redis_url=f"redis://127.0.0.1:{_CLOSED_PORT}/0",
        jwt_secret=_JWT_SECRET,
    )
    result = await _check_redis(settings)
    assert result.name == "redis"
    assert result.status == "down"
    assert result.detail
    assert result.latency_ms is not None


@pytest.mark.asyncio
async def test_check_storage_skipped_when_endpoint_unset() -> None:
    """An empty ``MINIO_ENDPOINT`` skips the probe gracefully."""

    from app.api.v1.health import _check_storage
    from app.config import Settings

    settings = Settings(minio_endpoint="", jwt_secret=_JWT_SECRET)
    result = await _check_storage(settings)
    assert result.name == "storage"
    assert result.status == "skipped"
    assert "MINIO_ENDPOINT" in (result.detail or "")
    assert result.latency_ms is None


@pytest.mark.asyncio
async def test_check_storage_down_when_unreachable() -> None:
    """A closed TCP port for MinIO is reported as ``down``."""

    from app.api.v1.health import _check_storage
    from app.config import Settings

    settings = Settings(
        minio_endpoint=f"http://127.0.0.1:{_CLOSED_PORT}",
        jwt_secret=_JWT_SECRET,
    )
    result = await _check_storage(settings)
    assert result.name == "storage"
    assert result.status == "down"
    assert result.detail
    assert result.latency_ms is not None


@pytest.mark.asyncio
async def test_check_redis_skipped_when_module_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the ``redis`` library is unavailable the probe is ``skipped``."""

    import sys

    from app.api.v1.health import _check_redis
    from app.config import Settings

    # Force ``import redis.asyncio`` inside the probe to raise
    # ModuleNotFoundError without uninstalling the package.
    monkeypatch.setitem(sys.modules, "redis", None)
    monkeypatch.setitem(sys.modules, "redis.asyncio", None)

    settings = Settings(
        redis_url="redis://127.0.0.1:6379/0", jwt_secret=_JWT_SECRET
    )
    result = await _check_redis(settings)
    assert result.name == "redis"
    assert result.status == "skipped"
    assert result.detail


@pytest.mark.asyncio
async def test_check_database_skipped_when_driver_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing SQLAlchemy driver is reported as ``skipped`` (not raised)."""

    import sys

    from app.api.v1.health import _check_database
    from app.config import Settings

    monkeypatch.setitem(sys.modules, "sqlalchemy", None)
    monkeypatch.setitem(sys.modules, "sqlalchemy.ext.asyncio", None)

    settings = Settings(
        database_url="sqlite+aiosqlite:///./_unused.db", jwt_secret=_JWT_SECRET
    )
    result = await _check_database(settings)
    assert result.name == "database"
    assert result.status == "skipped"
    assert result.detail


@pytest.mark.asyncio
async def test_check_storage_ok_against_local_listener(
    unused_tcp_port_factory=None,
) -> None:
    """A reachable TCP endpoint is reported as ``ok`` with a latency."""

    import asyncio as _asyncio
    import socket

    from app.api.v1.health import _check_storage
    from app.config import Settings

    # Pick a free port and stand up a no-op TCP server so the probe
    # exercises the success cleanup path (writer.close/wait_closed).
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    async def _handler(_reader, writer):  # type: ignore[no-untyped-def]
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    server = await _asyncio.start_server(_handler, "127.0.0.1", port)

    try:
        settings = Settings(
            minio_endpoint=f"http://127.0.0.1:{port}", jwt_secret=_JWT_SECRET
        )
        result = await _check_storage(settings)
        assert result.name == "storage"
        assert result.status == "ok"
        assert result.latency_ms is not None
        assert result.detail is None
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_check_storage_defaults_when_url_missing_scheme() -> None:
    """A bare host/port string still resolves through ``urlparse`` defaults."""

    from app.api.v1.health import _check_storage
    from app.config import Settings

    # No scheme — ``urlparse`` returns hostname=None so the probe falls
    # back to ``localhost`` and default port 80. We expect ``down`` since
    # nothing is listening locally on 80 in CI, but the call must not raise.
    settings = Settings(minio_endpoint="://", jwt_secret=_JWT_SECRET)
    result = await _check_storage(settings)
    assert result.name == "storage"
    assert result.status in {"down", "ok"}


# --------------------------------------------------------------------------
# Endpoint-level degraded behaviour
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_detailed_reports_degraded_when_dep_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Endpoint must aggregate per-service status into ``degraded`` overall."""

    from app.api.v1 import health as health_module
    from app.api.v1.health import ServiceStatus

    async def _fake_db(_settings):  # type: ignore[no-untyped-def]
        return ServiceStatus(name="database", status="ok", latency_ms=1.0)

    async def _fake_redis(_settings):  # type: ignore[no-untyped-def]
        return ServiceStatus(
            name="redis",
            status="down",
            detail="ConnectionRefusedError: simulated",
            latency_ms=2.0,
        )

    async def _fake_storage(_settings):  # type: ignore[no-untyped-def]
        return ServiceStatus(name="storage", status="ok", latency_ms=3.0)

    monkeypatch.setattr(health_module, "_check_database", _fake_db)
    monkeypatch.setattr(health_module, "_check_redis", _fake_redis)
    monkeypatch.setattr(health_module, "_check_storage", _fake_storage)

    # Build a fresh app/client now that the helpers are patched.
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        response = await ac.get("/api/v1/health/detailed")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    by_name = {s["name"]: s for s in body["services"]}
    assert by_name["redis"]["status"] == "down"
    assert by_name["database"]["status"] == "ok"
    assert by_name["storage"]["status"] == "ok"


@pytest.mark.asyncio
async def test_health_detailed_reports_ok_when_all_services_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All probes ok → overall status is ``ok``."""

    from app.api.v1 import health as health_module
    from app.api.v1.health import ServiceStatus

    async def _ok_db(_settings):  # type: ignore[no-untyped-def]
        return ServiceStatus(name="database", status="ok", latency_ms=0.5)

    async def _ok_redis(_settings):  # type: ignore[no-untyped-def]
        return ServiceStatus(name="redis", status="ok", latency_ms=0.5)

    async def _ok_storage(_settings):  # type: ignore[no-untyped-def]
        return ServiceStatus(name="storage", status="ok", latency_ms=0.5)

    monkeypatch.setattr(health_module, "_check_database", _ok_db)
    monkeypatch.setattr(health_module, "_check_redis", _ok_redis)
    monkeypatch.setattr(health_module, "_check_storage", _ok_storage)

    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        response = await ac.get("/api/v1/health/detailed")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert all(s["status"] == "ok" for s in body["services"])


@pytest.mark.asyncio
async def test_health_detailed_skipped_services_do_not_degrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``skipped`` dependency must not flip overall status to ``degraded``."""

    from app.api.v1 import health as health_module
    from app.api.v1.health import ServiceStatus

    async def _db(_settings):  # type: ignore[no-untyped-def]
        return ServiceStatus(name="database", status="ok", latency_ms=1.0)

    async def _redis(_settings):  # type: ignore[no-untyped-def]
        return ServiceStatus(name="redis", status="skipped", detail="REDIS_URL not set")

    async def _storage(_settings):  # type: ignore[no-untyped-def]
        return ServiceStatus(
            name="storage", status="skipped", detail="MINIO_ENDPOINT not set"
        )

    monkeypatch.setattr(health_module, "_check_database", _db)
    monkeypatch.setattr(health_module, "_check_redis", _redis)
    monkeypatch.setattr(health_module, "_check_storage", _storage)

    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        response = await ac.get("/api/v1/health/detailed")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    statuses = {s["name"]: s["status"] for s in body["services"]}
    assert statuses["redis"] == "skipped"
    assert statuses["storage"] == "skipped"
