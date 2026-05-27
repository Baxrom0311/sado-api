"""Tests for the audio_processor error/edge paths and Celery worker glue."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


async def _make_assessment_with_recording(
    *,
    storage_key: str = "audio/missing/rec.wav",
    duration_sec: float | None = 2.0,
) -> tuple[str, str]:
    """Insert one assessment + one recording, return (assessment_id, recording_id).

    The assessment + child + parent are freshly created per call so tests
    stay independent.  ``storage_key`` defaults to a key that does NOT
    exist in the storage backend so the storage-error branch is exercised.
    """

    from app.core.security import hash_password
    from app.database import get_sessionmaker
    from app.models.assessment import (
        Assessment,
        AssessmentStatus,
        AssessmentType,
        AudioRecording,
    )
    from app.models.child import Child
    from app.models.user import User, UserRole

    factory = get_sessionmaker()
    async with factory() as session:
        u = User(
            email=f"errpath+{uuid.uuid4().hex[:8]}@sado.uz",
            password_hash=hash_password("Sup3r-Secret!"),
            full_name="Errpath",
            role=UserRole.PARENT.value,
            is_active=True,
        )
        session.add(u)
        await session.commit()
        await session.refresh(u)

        c = Child(
            parent_id=u.id,
            name="ErrChild",
            birth_date=date(2020, 6, 1),
            gender="female",
            language="uz",
        )
        session.add(c)
        await session.commit()
        await session.refresh(c)

        a = Assessment(
            child_id=c.id,
            type=AssessmentType.SCREENING.value,
            status=AssessmentStatus.PENDING.value,
        )
        session.add(a)
        await session.commit()
        await session.refresh(a)

        rec_id = str(uuid.uuid4())
        r = AudioRecording(
            id=rec_id,
            assessment_id=a.id,
            task_type="repeat_word",
            storage_key=storage_key,
            content_type="audio/wav",
            size_bytes=10,
            duration_sec=duration_sec,
        )
        session.add(r)
        await session.commit()

    return a.id, rec_id


async def test_process_recording_handles_storage_error(app: Any) -> None:
    """If storage.get_object raises, the recording is flagged as failed."""

    from app.database import get_sessionmaker
    from app.models.assessment import (
        Assessment,
        AssessmentStatus,
        AudioRecording,
    )
    from app.services.audio_processor import process_recording

    assessment_id, recording_id = await _make_assessment_with_recording(
        storage_key="audio/missing/does-not-exist.wav"
    )

    factory = get_sessionmaker()
    async with factory() as session:
        result = await process_recording(session, recording_id)
        assert result is None

    async with factory() as session:
        rec = await session.get(AudioRecording, recording_id)
        assert rec is not None
        assert rec.processed is False
        assert rec.processing_error is not None
        assert rec.processing_error.startswith("storage_error:")

        a = await session.get(Assessment, assessment_id)
        assert a is not None
        assert a.status == AssessmentStatus.FAILED.value


async def test_process_recording_returns_none_for_missing_id(app: Any) -> None:
    """A missing recording id should produce a clean ``None`` (no crash)."""

    from app.database import get_sessionmaker
    from app.services.audio_processor import process_recording

    factory = get_sessionmaker()
    async with factory() as session:
        result = await process_recording(session, "does-not-exist")
        assert result is None


async def test_process_recording_handles_feature_extraction_error(
    app: Any, monkeypatch
) -> None:
    """If the feature extractor blows up, the recording is failed cleanly."""

    from app.database import get_sessionmaker
    from app.models.assessment import AudioRecording
    from app.services import audio_processor as ap
    from app.services.storage import build_recording_key, get_audio_storage

    assessment_id, recording_id = await _make_assessment_with_recording(
        storage_key=build_recording_key(
            assessment_id="errf-a",
            recording_id="errf-r",
            content_type="audio/wav",
        )
    )

    storage = get_audio_storage()
    factory = get_sessionmaker()
    async with factory() as session:
        rec = await session.get(AudioRecording, recording_id)
        assert rec is not None
        # Put a real object so the storage call succeeds; we'll fail later.
        await storage.put_object(
            key=rec.storage_key, data=b"\x00" * 100, content_type="audio/wav"
        )

    def boom(*_args, **_kwargs):
        raise RuntimeError("synthetic feature failure")

    monkeypatch.setattr(ap, "extract_features", boom)

    async with factory() as session:
        result = await ap.process_recording(session, recording_id)
        assert result is None

    async with factory() as session:
        rec = await session.get(AudioRecording, recording_id)
        assert rec is not None
        assert rec.processed is False
        assert rec.processing_error is not None
        assert rec.processing_error.startswith("feature_error:")
        # And the assessment aggregate is rolled to FAILED.
        from app.models.assessment import Assessment, AssessmentStatus

        a = await session.get(Assessment, assessment_id)
        assert a is not None
        assert a.status == AssessmentStatus.FAILED.value


async def test_celery_task_module_imports_cleanly() -> None:
    """The worker module must import without side effects so CI can build it."""

    from app.workers import celery_app, process_recording_task
    from app.workers.celery_app import create_celery_app

    # Real Celery instance with the task registered under the documented name.
    assert celery_app is not None
    assert process_recording_task.name == "app.workers.tasks.process_recording_task"
    # Task is routed to the dedicated audio queue.
    routes = celery_app.conf.task_routes
    assert "app.workers.tasks.process_recording_task" in routes
    assert routes["app.workers.tasks.process_recording_task"]["queue"] == "sado.audio"

    # Factory is idempotent and produces a fresh app each time.
    other = create_celery_app()
    assert other is not celery_app
    assert other.main == "sado"
