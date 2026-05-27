"""Assessment + audio-recording endpoints.

Contract:

* ``POST /assessments`` — create an assessment session for a child.
* ``GET /assessments`` — cursor-paginated list, scoped by role.
* ``GET /assessments/:id`` — detail view including recordings.
* ``POST /assessments/:id/recordings`` — multipart audio upload.
* ``GET /analysis/:assessment_id`` — risk results (parent-safe view).
* ``GET /analysis/:assessment_id/detailed`` — therapist-only deep view.

Audio uploads are validated for size (max 10 MB by default), content
type (audio/* allow-list) and duration (max 60 s). On a successful
upload we either dispatch a Celery task or process inline (eager mode
in tests / dev) so the result is visible immediately.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, File, Form, Path, Query, Response, UploadFile, status
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DBSession
from app.config import get_settings
from app.core.exceptions import (
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.core.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Page,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)
from app.models.assessment import (
    Assessment,
    AssessmentStatus,
    AssessmentType,
    AudioRecording,
    RecordingTaskType,
)
from app.models.child import Child
from app.models.kindergarten import Kindergarten
from app.models.user import User, UserRole
from app.schemas.assessment import (
    AnalysisDetailedPublic,
    AnalysisPublic,
    AssessmentAnalysisResponse,
    AssessmentCreate,
    AssessmentDetailedAnalysisResponse,
    AssessmentPublic,
    AssessmentUpdate,
    AudioRecordingPublic,
)
from app.services.audio_processor import process_recording
from app.services.storage import build_recording_key, get_audio_storage

logger = logging.getLogger(__name__)
router = APIRouter()

ALLOWED_CONTENT_TYPES = {
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "audio/mpeg",
    "audio/mp3",
    "audio/mp4",
    "audio/x-m4a",
    "audio/aac",
    "audio/ogg",
    "audio/webm",
    "audio/flac",
}

ALLOWED_TASK_TYPES = {t.value for t in RecordingTaskType}
ALLOWED_ASSESSMENT_TYPES = {t.value for t in AssessmentType}


# --------------------------------------------------------------- Helpers


def _can_read_child(user: User, child: Child) -> bool:
    if user.role == UserRole.ADMIN.value:
        return True
    if user.role == UserRole.THERAPIST.value:
        return True
    if user.role == UserRole.PARENT.value:
        return child.parent_id == user.id
    if user.role == UserRole.TEACHER.value:
        if user.region_id is None:
            return False
        kg = child.kindergarten
        return kg is not None and kg.region_id == user.region_id
    return False


def _can_create_assessment(user: User, child: Child) -> bool:
    if user.role == UserRole.ADMIN.value:
        return True
    if user.role == UserRole.PARENT.value:
        return child.parent_id == user.id
    if user.role == UserRole.TEACHER.value:
        if user.region_id is None:
            return False
        kg = child.kindergarten
        return kg is not None and kg.region_id == user.region_id
    if user.role == UserRole.THERAPIST.value:
        return True
    return False


async def _load_assessment_or_404(
    session: DBSession,
    assessment_id: str,
    *,
    with_recordings: bool = True,
    with_analysis: bool = False,
) -> Assessment:
    """Fetch one assessment with the relationships the caller needs.

    ``with_analysis`` chains ``selectinload(AudioRecording.analysis)`` onto
    the recordings load so analysis endpoints can serialise results
    without issuing a second query per recording (no N+1).
    """

    stmt = select(Assessment).where(Assessment.id == assessment_id)
    if with_recordings:
        recordings_load = selectinload(Assessment.recordings)
        if with_analysis:
            recordings_load = recordings_load.selectinload(AudioRecording.analysis)
        stmt = stmt.options(
            recordings_load,
            selectinload(Assessment.child).selectinload(Child.kindergarten),
        )
    else:
        stmt = stmt.options(
            selectinload(Assessment.child).selectinload(Child.kindergarten),
        )
    result = await session.execute(stmt)
    assessment = result.scalar_one_or_none()
    if assessment is None:
        raise NotFoundError("Assessment not found", code="ASSESSMENT_NOT_FOUND")
    return assessment


def _ensure_visible(user: User, assessment: Assessment) -> None:
    child = assessment.child
    if not _can_read_child(user, child):
        raise ForbiddenError(
            "You do not have access to this assessment.",
            code="ASSESSMENT_FORBIDDEN",
        )


# --------------------------------------------------------------- Endpoints


@router.post(
    "/assessments",
    response_model=AssessmentPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Start an assessment session for a child",
)
async def create_assessment(
    payload: AssessmentCreate,
    user: CurrentUser,
    session: DBSession,
) -> AssessmentPublic:
    if payload.type not in ALLOWED_ASSESSMENT_TYPES:
        raise ValidationError(
            f"type must be one of {sorted(ALLOWED_ASSESSMENT_TYPES)}",
            code="INVALID_ASSESSMENT_TYPE",
        )

    # Eager-load kindergarten so the permission check doesn't trigger
    # an implicit async lazy load.
    child_q = await session.execute(
        select(Child)
        .options(selectinload(Child.kindergarten))
        .where(Child.id == payload.child_id)
    )
    child = child_q.scalar_one_or_none()
    if child is None:
        raise NotFoundError("Child not found", code="CHILD_NOT_FOUND")

    if not _can_create_assessment(user, child):
        raise ForbiddenError(
            "You may not start assessments for this child.",
            code="ASSESSMENT_FORBIDDEN",
        )

    assessment = Assessment(
        child_id=child.id,
        created_by_id=user.id,
        type=payload.type,
        status=AssessmentStatus.PENDING.value,
        started_at=datetime.now(UTC),
    )
    session.add(assessment)
    await session.commit()
    await session.refresh(assessment)
    # No recordings yet — return a clean payload.
    return AssessmentPublic.from_model(assessment)


@router.get(
    "/assessments",
    response_model=Page[AssessmentPublic],
    summary="List assessments visible to the caller",
)
async def list_assessments(
    user: CurrentUser,
    session: DBSession,
    cursor: Annotated[str | None, Query(description="Pagination cursor")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    child_id: Annotated[str | None, Query(description="Filter by child")] = None,
    status_filter: Annotated[
        str | None,
        Query(alias="status", description="Filter by status"),
    ] = None,
    risk_level: Annotated[
        str | None, Query(description="Filter by overall_risk")
    ] = None,
) -> Page[AssessmentPublic]:
    page_size = clamp_limit(limit)

    stmt = select(Assessment).options(
        selectinload(Assessment.recordings),
        selectinload(Assessment.child).selectinload(Child.kindergarten),
    )

    if user.role == UserRole.PARENT.value:
        stmt = stmt.join(Child, Child.id == Assessment.child_id).where(
            Child.parent_id == user.id
        )
    elif user.role == UserRole.TEACHER.value:
        if user.region_id is None:
            return Page[AssessmentPublic](items=[], next_cursor=None, has_more=False)
        stmt = (
            stmt.join(Child, Child.id == Assessment.child_id)
            .join(Kindergarten, Kindergarten.id == Child.kindergarten_id)
            .where(Kindergarten.region_id == user.region_id)
        )

    if child_id:
        stmt = stmt.where(Assessment.child_id == child_id)
    if status_filter:
        stmt = stmt.where(Assessment.status == status_filter)
    if risk_level:
        stmt = stmt.where(Assessment.overall_risk == risk_level)

    if cursor:
        try:
            cursor_ts, cursor_id = decode_cursor(cursor)
        except ValueError as exc:
            raise ValidationError(str(exc), code="INVALID_CURSOR") from exc
        stmt = stmt.where(
            or_(
                Assessment.created_at < cursor_ts,
                and_(
                    Assessment.created_at == cursor_ts,
                    Assessment.id < cursor_id,
                ),
            )
        )

    stmt = stmt.order_by(Assessment.created_at.desc(), Assessment.id.desc()).limit(
        page_size + 1
    )

    result = await session.execute(stmt)
    rows = list(result.scalars().unique().all())

    has_more = len(rows) > page_size
    page_items = rows[:page_size]
    next_cursor: str | None = None
    if has_more and page_items:
        last = page_items[-1]
        next_cursor = encode_cursor(last.created_at, last.id)

    return Page[AssessmentPublic](
        items=[AssessmentPublic.from_model(a) for a in page_items],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get(
    "/assessments/{assessment_id}",
    response_model=AssessmentPublic,
    summary="Read one assessment",
)
async def get_assessment(
    user: CurrentUser,
    session: DBSession,
    assessment_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> AssessmentPublic:
    assessment = await _load_assessment_or_404(session, assessment_id)
    _ensure_visible(user, assessment)
    return AssessmentPublic.from_model(assessment)


@router.patch(
    "/assessments/{assessment_id}",
    response_model=AssessmentPublic,
    summary="Update assessment status / summary",
)
async def update_assessment(
    user: CurrentUser,
    session: DBSession,
    payload: AssessmentUpdate,
    assessment_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> AssessmentPublic:
    assessment = await _load_assessment_or_404(session, assessment_id)
    _ensure_visible(user, assessment)

    # Only therapist/admin/teacher may update status; parents only own.
    is_owner_parent = (
        user.role == UserRole.PARENT.value
        and assessment.child.parent_id == user.id
    )
    if user.role not in {
        UserRole.ADMIN.value,
        UserRole.THERAPIST.value,
        UserRole.TEACHER.value,
    } and not is_owner_parent:
        raise ForbiddenError(
            "You may not modify this assessment.",
            code="ASSESSMENT_FORBIDDEN",
        )

    data = payload.model_dump(exclude_unset=True)
    if "status" in data and data["status"] is not None:
        if data["status"] not in {s.value for s in AssessmentStatus}:
            raise ValidationError(
                "invalid status", code="INVALID_ASSESSMENT_STATUS"
            )
        assessment.status = data["status"]
    if "summary" in data:
        assessment.summary = data["summary"]

    await session.commit()
    await session.refresh(assessment)
    return AssessmentPublic.from_model(assessment)


@router.delete(
    "/assessments/{assessment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete an assessment (admin only)",
)
async def delete_assessment(
    user: CurrentUser,
    session: DBSession,
    assessment_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> Response:
    if user.role != UserRole.ADMIN.value:
        raise ForbiddenError(
            "Only admins can delete assessments.", code="INSUFFICIENT_ROLE"
        )
    assessment = await _load_assessment_or_404(session, assessment_id)
    await session.delete(assessment)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------- Audio recordings


@router.post(
    "/assessments/{assessment_id}/recordings",
    response_model=AudioRecordingPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Upload an audio recording for one task in the assessment",
)
async def upload_recording(
    user: CurrentUser,
    session: DBSession,
    assessment_id: Annotated[str, Path(min_length=1, max_length=36)],
    audio: Annotated[UploadFile, File(description="Audio file (wav/mp3/m4a/ogg)")],
    task_type: Annotated[str, Form(description="Recording task type")] = (
        RecordingTaskType.REPEAT_WORD.value
    ),
    prompt: Annotated[str | None, Form(description="Optional prompt text")] = None,
    duration_sec: Annotated[float | None, Form(description="Client-measured duration")] = None,
) -> AudioRecordingPublic:
    settings = get_settings()
    assessment = await _load_assessment_or_404(session, assessment_id)
    _ensure_visible(user, assessment)

    if not _can_create_assessment(user, assessment.child):
        raise ForbiddenError(
            "You may not upload recordings for this assessment.",
            code="ASSESSMENT_FORBIDDEN",
        )

    if task_type not in ALLOWED_TASK_TYPES:
        raise ValidationError(
            f"task_type must be one of {sorted(ALLOWED_TASK_TYPES)}",
            code="INVALID_TASK_TYPE",
        )

    content_type = (audio.content_type or "").lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ValidationError(
            f"content_type {content_type or 'unknown'!r} is not an accepted audio mime type",
            code="INVALID_AUDIO_TYPE",
        )

    if duration_sec is not None and duration_sec > settings.max_audio_duration_sec:
        raise ValidationError(
            f"recording duration {duration_sec:.2f}s exceeds limit "
            f"{settings.max_audio_duration_sec}s",
            code="AUDIO_TOO_LONG",
        )

    raw = await audio.read()
    max_bytes = settings.max_audio_size_mb * 1024 * 1024
    if not raw:
        raise ValidationError("audio file is empty", code="AUDIO_EMPTY")
    if len(raw) > max_bytes:
        raise ValidationError(
            f"audio file is {len(raw) / (1024 * 1024):.2f} MB — "
            f"limit is {settings.max_audio_size_mb} MB",
            code="AUDIO_TOO_LARGE",
        )

    recording_id = str(uuid.uuid4())
    storage_key = build_recording_key(
        assessment_id=assessment.id,
        recording_id=recording_id,
        content_type=content_type,
    )

    storage = get_audio_storage()
    stored = await storage.put_object(
        key=storage_key, data=raw, content_type=content_type
    )

    recording = AudioRecording(
        id=recording_id,
        assessment_id=assessment.id,
        task_type=task_type,
        prompt=prompt,
        storage_key=stored.storage_key,
        content_type=stored.content_type,
        size_bytes=stored.size_bytes,
        duration_sec=duration_sec,
    )
    session.add(recording)

    if assessment.status == AssessmentStatus.PENDING.value:
        assessment.status = AssessmentStatus.IN_PROGRESS.value
    await session.commit()
    await session.refresh(recording)

    # Dispatch processing — eager mode runs inline; otherwise enqueue.
    if settings.celery_task_always_eager:
        try:
            await process_recording(session, recording.id)
        except Exception:  # pragma: no cover - inline failure surfaces in tests
            logger.exception("Inline processing failed for %s", recording.id)
        await session.refresh(recording)
    else:  # pragma: no cover - exercised by integration only
        try:
            from app.workers.tasks import process_recording_task

            process_recording_task.delay(recording.id)
            assessment.status = AssessmentStatus.PROCESSING.value
            await session.commit()
        except Exception:
            logger.exception("Failed to enqueue processing for %s", recording.id)

    return AudioRecordingPublic.model_validate(recording)


@router.get(
    "/assessments/{assessment_id}/recordings",
    response_model=list[AudioRecordingPublic],
    summary="List recordings attached to an assessment",
)
async def list_recordings(
    user: CurrentUser,
    session: DBSession,
    assessment_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> list[AudioRecordingPublic]:
    assessment = await _load_assessment_or_404(session, assessment_id)
    _ensure_visible(user, assessment)
    return [AudioRecordingPublic.model_validate(r) for r in assessment.recordings]


# --------------------------------------------------------------- Analysis


@router.get(
    "/analysis/{assessment_id}",
    response_model=AssessmentAnalysisResponse,
    summary="Risk-level analysis for an assessment (parent-safe view)",
)
async def get_analysis(
    user: CurrentUser,
    session: DBSession,
    assessment_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> AssessmentAnalysisResponse:
    assessment = await _load_assessment_or_404(
        session, assessment_id, with_analysis=True
    )
    _ensure_visible(user, assessment)

    # Recordings + their analyses are already eager-loaded; iterate in
    # memory rather than issuing a second SELECT against analysis_results.
    analyses = [r.analysis for r in assessment.recordings if r.analysis is not None]

    return AssessmentAnalysisResponse(
        assessment_id=assessment.id,
        overall_risk=assessment.overall_risk,
        overall_confidence=assessment.overall_confidence,
        status=assessment.status,
        completed_at=assessment.completed_at,
        results=[
            AnalysisPublic(
                recording_id=a.recording_id,
                risk_level=a.risk_level,
                confidence=a.confidence,
                transcript=a.transcript,
                feature_summary=a.feature_summary,
                model_name=a.model_name,
                model_version=a.model_version,
                created_at=a.created_at,
            )
            for a in analyses
        ],
    )


@router.get(
    "/analysis/{assessment_id}/detailed",
    response_model=AssessmentDetailedAnalysisResponse,
    summary="Therapist / admin deep view including raw acoustic features",
)
async def get_analysis_detailed(
    user: CurrentUser,
    session: DBSession,
    assessment_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> AssessmentDetailedAnalysisResponse:
    if user.role not in {UserRole.THERAPIST.value, UserRole.ADMIN.value}:
        raise ForbiddenError(
            "Detailed analysis is available to therapists and admins only.",
            code="DETAILED_FORBIDDEN",
        )
    assessment = await _load_assessment_or_404(
        session, assessment_id, with_analysis=True
    )

    # Eager-loaded relation removes the need for a separate
    # ``select(AnalysisResult)`` round-trip per recording.
    analyses = [r.analysis for r in assessment.recordings if r.analysis is not None]

    return AssessmentDetailedAnalysisResponse(
        assessment_id=assessment.id,
        overall_risk=assessment.overall_risk,
        overall_confidence=assessment.overall_confidence,
        status=assessment.status,
        completed_at=assessment.completed_at,
        results=[
            AnalysisDetailedPublic(
                recording_id=a.recording_id,
                risk_level=a.risk_level,
                confidence=a.confidence,
                transcript=a.transcript,
                feature_summary=a.feature_summary,
                model_name=a.model_name,
                model_version=a.model_version,
                created_at=a.created_at,
                mfcc_features=a.mfcc_features,
                pitch_data=a.pitch_data,
                formant_data=a.formant_data,
                phoneme_scores=a.phoneme_scores,
            )
            for a in analyses
        ],
    )


__all__ = ["router"]
