"""Celery tasks — async audio processing.

The task simply delegates to :func:`app.services.audio_processor.process_recording`
inside a fresh asyncio event loop so the same orchestration code runs
identically in eager-mode tests, single-node dev, and a real broker.
"""

from __future__ import annotations

import asyncio
import logging

from app.database import get_sessionmaker
from app.services.audio_processor import process_recording
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def _run(recording_id: str) -> str | None:
    factory = get_sessionmaker()
    async with factory() as session:
        analysis = await process_recording(session, recording_id)
        return analysis.id if analysis is not None else None


@celery_app.task(
    name="app.workers.tasks.process_recording_task",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    max_retries=3,
)
def process_recording_task(self, recording_id: str) -> str | None:  # noqa: ANN001
    """Process one recording end-to-end."""

    logger.info("Worker processing recording %s (try %s)", recording_id, self.request.retries)
    return asyncio.run(_run(recording_id))


__all__ = ["process_recording_task"]
