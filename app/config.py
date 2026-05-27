"""Application configuration loaded from environment variables.

Settings use pydantic-settings v2 and a ``.env`` file loader so the
service runs in three modes:

* development — SQLite fallback, in-memory rate limiter, local storage
* test — fully in-memory (no Redis / MinIO required)
* production — Postgres + Redis + MinIO

The defaults intentionally make ``pytest`` and ``uvicorn app.main:app``
work out of the box without any external services.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly typed application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------------------------------------------------------------- App
    app_name: str = "SADO API"
    app_env: Literal["development", "test", "staging", "production"] = "development"
    app_debug: bool = True
    api_v1_prefix: str = "/api/v1"
    log_level: str = "INFO"

    host: str = "0.0.0.0"
    port: int = 8000

    # ----------------------------------------------------------- Database
    database_url: str = "sqlite+aiosqlite:///./sado.db"

    # -------------------------------------------------------------- Redis
    redis_url: str | None = "redis://localhost:6379/0"

    # ---------------------------------------------------------------- JWT
    jwt_secret: str = Field(
        default="dev-only-secret-change-me-in-production-please",
        min_length=16,
    )
    jwt_algorithm: str = "HS256"
    access_token_expires_min: int = 15
    refresh_token_expires_days: int = 7

    # --------------------------------------------------------------- CORS
    cors_origins: str = "http://localhost:5173,http://localhost:8081,http://localhost:19006"

    # ------------------------------------------------------- MinIO / S3
    minio_endpoint: str = "http://localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "sado-audio"
    minio_region: str = "us-east-1"

    # Local fallback when MinIO is unavailable.
    local_storage_dir: str = "./storage"

    # ------------------------------------------------------------- Celery
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"
    celery_task_always_eager: bool = False

    # -------------------------------------------------------- Rate limit
    rate_limit_auth_per_minute: int = 10

    # ------------------------------------------------------------- Audio
    max_audio_duration_sec: int = 60
    max_audio_size_mb: int = 10

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _normalize_cors(cls, value: object) -> str:
        if isinstance(value, list):
            return ",".join(str(v) for v in value)
        return str(value) if value is not None else ""

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor — keeps env parsing cheap."""

    return Settings()
