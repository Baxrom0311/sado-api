"""Health check endpoints.

* ``GET /health`` — liveness probe, always returns 200 quickly.
* ``GET /health/detailed`` — checks DB / Redis / MinIO connectivity and
  reports a per-service status. Designed to be safe to call without
  authentication during local development; in later milestones it will
  be guarded for admin-only access.

The detailed probe deliberately never raises; degraded dependencies are
reported via the ``services`` map so monitoring can alert without the
endpoint going hard-down.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app import __version__
from app.config import Settings, get_settings

router = APIRouter()


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
    version: str
    environment: str
    timestamp: datetime


class ServiceStatus(BaseModel):
    name: str
    status: Literal["ok", "degraded", "down", "skipped"]
    detail: str | None = None
    latency_ms: float | None = None


class DetailedHealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    service: str
    version: str
    environment: str
    timestamp: datetime
    services: list[ServiceStatus]


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
    description="Returns 200 OK whenever the API process is responsive.",
)
async def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        service=settings.app_name,
        version=__version__,
        environment=settings.app_env,
        timestamp=datetime.now(UTC),
    )


async def _check_database(settings: Settings) -> ServiceStatus:
    """Best-effort database connectivity check.

    The check is intentionally lazy — if the SQLAlchemy engine cannot be
    constructed (missing driver in test envs, etc.) we report ``skipped``
    rather than failing the whole probe.
    """

    started = time.perf_counter()
    try:
        # Local import keeps health endpoint cheap when DB layer is absent.
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(settings.database_url, future=True)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()
    except ModuleNotFoundError as exc:
        return ServiceStatus(name="database", status="skipped", detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — health probe must never raise
        return ServiceStatus(
            name="database",
            status="down",
            detail=f"{type(exc).__name__}: {exc}",
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    return ServiceStatus(
        name="database",
        status="ok",
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )


async def _check_redis(settings: Settings) -> ServiceStatus:
    if not settings.redis_url:
        return ServiceStatus(name="redis", status="skipped", detail="REDIS_URL not set")

    started = time.perf_counter()
    try:
        import redis.asyncio as redis_async  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        return ServiceStatus(name="redis", status="skipped", detail=str(exc))

    client = redis_async.from_url(settings.redis_url, socket_connect_timeout=1.0)
    try:
        await asyncio.wait_for(client.ping(), timeout=1.0)
    except Exception as exc:  # noqa: BLE001
        return ServiceStatus(
            name="redis",
            status="down",
            detail=f"{type(exc).__name__}: {exc}",
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass

    return ServiceStatus(
        name="redis",
        status="ok",
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )


async def _check_storage(settings: Settings) -> ServiceStatus:
    """Lightweight MinIO/S3 reachability check.

    We don't authenticate or list buckets here — just confirm the
    endpoint resolves and accepts a TCP connection. This avoids
    depending on credentials being valid for the health probe.
    """

    if not settings.minio_endpoint:
        return ServiceStatus(name="storage", status="skipped", detail="MINIO_ENDPOINT not set")

    started = time.perf_counter()
    try:
        from urllib.parse import urlparse

        parsed = urlparse(settings.minio_endpoint)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=1.0)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        del reader
    except Exception as exc:  # noqa: BLE001
        return ServiceStatus(
            name="storage",
            status="down",
            detail=f"{type(exc).__name__}: {exc}",
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    return ServiceStatus(
        name="storage",
        status="ok",
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )


@router.get(
    "/health/detailed",
    response_model=DetailedHealthResponse,
    summary="Detailed readiness probe",
    description=(
        "Reports per-dependency status (database, redis, storage). "
        "Returns 200 even when a dependency is degraded — the per-service "
        "status field carries the truth, so monitoring can alert without "
        "the endpoint going hard-down."
    ),
)
async def health_detailed() -> DetailedHealthResponse:
    settings = get_settings()
    services = await asyncio.gather(
        _check_database(settings),
        _check_redis(settings),
        _check_storage(settings),
    )
    overall: Literal["ok", "degraded"] = (
        "degraded" if any(s.status == "down" for s in services) else "ok"
    )
    return DetailedHealthResponse(
        status=overall,
        service=settings.app_name,
        version=__version__,
        environment=settings.app_env,
        timestamp=datetime.now(UTC),
        services=list(services),
    )
