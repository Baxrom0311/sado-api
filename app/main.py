"""FastAPI application factory.

The factory pattern lets tests build a fresh app per ``TestClient`` and
keeps import side-effects out of module load.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.v1 import api_router as api_v1_router
from app.config import get_settings
from app.core.middleware import RequestIDMiddleware, register_exception_handlers


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )


OPENAPI_TAGS = [
    {"name": "health", "description": "Liveness and readiness probes."},
    {"name": "auth", "description": "Registration, login, token refresh, logout."},
    {"name": "users", "description": "User profile and admin user management."},
    {"name": "children", "description": "Child profiles managed by parents and staff."},
    {"name": "assessments", "description": "Speech assessments and audio recordings."},
    {"name": "analysis", "description": "Risk-scoring results from the ML pipeline."},
    {"name": "exercises", "description": "Speech therapy exercises and assignments."},
    {"name": "kindergartens", "description": "Kindergartens and group screenings."},
    {"name": "regions", "description": "Geographic regions of Uzbekistan."},
    {"name": "stats", "description": "Aggregated statistics for dashboards."},
    {"name": "notifications", "description": "User notifications inbox."},
]


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""

    settings = get_settings()
    _configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Backend REST API for the SADO speech-therapy platform. "
            "Provides authentication, child profiles, gamified speech "
            "assessments, ML-based risk scoring, and therapist tooling."
        ),
        openapi_tags=OPENAPI_TAGS,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # CORS — admin dashboard and Expo dev clients.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    # Request ID stamping.
    app.add_middleware(RequestIDMiddleware)

    # Consistent error responses.
    register_exception_handlers(app)

    # Mount routers.
    app.include_router(api_v1_router, prefix=settings.api_v1_prefix)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": settings.app_name,
            "version": __version__,
            "docs": "/docs",
            "health": f"{settings.api_v1_prefix}/health",
        }

    return app


app = create_app()
