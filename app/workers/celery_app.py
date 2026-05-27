"""Celery application factory."""

from __future__ import annotations

from celery import Celery

from app.config import get_settings


def create_celery_app() -> Celery:
    """Build and configure the Celery app from settings."""

    settings = get_settings()

    celery_app = Celery(
        "sado",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend,
        include=["app.workers.tasks"],
    )

    celery_app.conf.update(
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        timezone="UTC",
        enable_utc=True,
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        task_default_queue="sado.default",
        task_routes={
            "app.workers.tasks.process_recording_task": {"queue": "sado.audio"},
        },
        task_always_eager=settings.celery_task_always_eager,
        task_eager_propagates=settings.celery_task_always_eager,
    )

    return celery_app


celery_app = create_celery_app()


__all__ = ["celery_app", "create_celery_app"]
