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

    # ----------------------------------------------------------- Billing
    # Payme Merchant API credentials. The merchant key is used as the
    # HTTP Basic password Payme signs every webhook with — the test
    # default keeps webhook tests deterministic without leaking the
    # production key. Override in production via env vars.
    payme_merchant_id: str = "test_merchant"
    payme_merchant_key: str = "test-payme-key"
    payme_test_key: str = "test-payme-test-key"
    payme_checkout_url: str = "https://checkout.paycom.uz"

    # Click Merchant API credentials.
    click_merchant_id: str = "0"
    click_service_id: str = "0"
    click_secret_key: str = "test-click-secret"
    click_checkout_url: str = "https://my.click.uz"

    # Feature flag — when ``False`` no quota enforcement is applied to
    # free users (grace period default during the billing rollout).
    billing_enforce_quotas: bool = False

    # Which speech-feature backend ``app.services.speech_analyzer`` uses:
    #
    # * ``"mock"`` — deterministic synthetic features (default for tests
    #   and when no native audio libs are installed).
    # * ``"real"`` — strict real-audio pipeline using librosa /
    #   soundfile / parselmouth. Raises if libraries are missing or
    #   extraction fails so the failure is visible to operators.
    # * ``"auto"`` — try real first, fall back to mock if the libs are
    #   not installed or extraction blows up. Best for production where
    #   we want to opportunistically run the real pipeline.
    audio_analysis_backend: Literal["mock", "real", "auto"] = "auto"

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
