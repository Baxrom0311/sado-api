"""Children CRUD endpoints.

Authorisation rules:

* ``parent`` — sees and mutates only the children attached to their
  own ``user_id``.
* ``teacher`` — read-only access scoped to children in their
  kindergarten (best-effort: matched via the user's ``region_id`` + the
  child's kindergarten when present).
* ``therapist`` and ``admin`` — full read access; ``admin`` can mutate
  on behalf of any parent.

Listing is cursor-paginated by ``(created_at desc, id desc)`` to give
stable, append-friendly pagination.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Response, status
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, DBSession
from app.core.exceptions import (
    ConflictError,
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
    AnalysisResult,
    Assessment,
    AudioRecording,
    RiskLevel,
)
from app.models.child import Child
from app.models.kindergarten import Kindergarten
from app.models.phoneme_mastery import MASTERY_THRESHOLD, PhonemeMastery
from app.models.user import User, UserRole
from app.schemas.child import ChildCreate, ChildPublic, ChildUpdate
from app.schemas.phoneme_mastery import (
    PhonemeMasteryPublic,
    PhonemeMasteryResponse,
    PhonemeMasterySummary,
    SpeechProfileResponse,
)
from app.schemas.progress import (
    ChildProgressResponse,
    ChildProgressSummary,
    ProgressPoint,
)
from app.services.phoneme_mastery import get_mastery_rows

router = APIRouter()


# --------------------------------------------------------------- Helpers


def _is_staff(user: User) -> bool:
    return user.role in {
        UserRole.ADMIN.value,
        UserRole.THERAPIST.value,
        UserRole.TEACHER.value,
    }


def _can_mutate(user: User, child: Child) -> bool:
    if user.role == UserRole.ADMIN.value:
        return True
    if user.role == UserRole.PARENT.value:
        return child.parent_id == user.id
    # Teachers/therapists are read-only on children for now — they
    # interact via assessments and exercise assignments.
    return False


def _can_read(user: User, child: Child) -> bool:
    if user.role == UserRole.ADMIN.value:
        return True
    if user.role == UserRole.PARENT.value:
        return child.parent_id == user.id
    if user.role == UserRole.THERAPIST.value:
        return True
    if user.role == UserRole.TEACHER.value:
        # Teachers can see children in their region (kindergarten link
        # not yet wired through user.kindergarten_id; region_id is the
        # closest available scope).
        if user.region_id is None:
            return False
        kg = child.kindergarten
        return kg is not None and kg.region_id == user.region_id
    return False


async def _resolve_parent_id(
    session: DBSession, user: User, requested_parent_id: str | None
) -> str:
    """Return the ``parent_id`` to attach to a new child.

    Parents may only ever create children for themselves. Admins may
    pass an explicit ``parent_id`` and we verify the user exists and
    actually has the ``parent`` role.
    """

    if user.role == UserRole.PARENT.value:
        if requested_parent_id and requested_parent_id != user.id:
            raise ForbiddenError(
                "Parents may only register their own children.",
                code="PARENT_SCOPE_VIOLATION",
            )
        return user.id

    if user.role == UserRole.ADMIN.value:
        if not requested_parent_id:
            raise ValidationError(
                "Admins must supply parent_id when creating a child.",
                code="PARENT_ID_REQUIRED",
            )
        target = await session.get(User, requested_parent_id)
        if target is None:
            raise NotFoundError("Parent user not found", code="PARENT_NOT_FOUND")
        if target.role != UserRole.PARENT.value:
            raise ValidationError(
                "Target user is not a parent.", code="PARENT_ROLE_MISMATCH"
            )
        return target.id

    raise ForbiddenError(
        "You do not have permission to register children.",
        code="INSUFFICIENT_ROLE",
    )


async def _validate_kindergarten(
    session: DBSession, kindergarten_id: str | None
) -> None:
    if kindergarten_id is None:
        return
    kg = await session.get(Kindergarten, kindergarten_id)
    if kg is None:
        raise NotFoundError(
            "Kindergarten not found", code="KINDERGARTEN_NOT_FOUND"
        )


async def _load_child_or_404(session: DBSession, child_id: str) -> Child:
    child = await session.get(Child, child_id)
    if child is None:
        raise NotFoundError("Child not found", code="CHILD_NOT_FOUND")
    return child


# --------------------------------------------------------------- Endpoints


@router.post(
    "/children",
    response_model=ChildPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new child",
)
async def create_child(
    payload: ChildCreate,
    user: CurrentUser,
    session: DBSession,
) -> ChildPublic:
    parent_id = await _resolve_parent_id(session, user, payload.parent_id)
    await _validate_kindergarten(session, payload.kindergarten_id)

    # Multi-tenant: derive tenant from kindergarten_id if provided,
    # otherwise inherit from the actor's own tenant. This makes the
    # creation flow zero-friction for tenant-bound parents/teachers.
    tenant_id = payload.kindergarten_id or user.tenant_id

    child = Child(
        name=payload.name,
        birth_date=payload.birth_date,
        gender=payload.gender,
        language=payload.language,
        notes=payload.notes,
        parent_id=parent_id,
        kindergarten_id=payload.kindergarten_id,
        tenant_id=tenant_id,
    )
    session.add(child)
    try:
        await session.commit()
    except IntegrityError as exc:  # pragma: no cover - rare race
        await session.rollback()
        raise ConflictError(
            "Could not save child due to a conflicting reference.",
            code="CHILD_CONFLICT",
        ) from exc
    await session.refresh(child)
    return ChildPublic.from_model(child)


@router.get(
    "/children",
    response_model=Page[ChildPublic],
    summary="List children visible to the caller",
)
async def list_children(
    user: CurrentUser,
    session: DBSession,
    cursor: Annotated[str | None, Query(description="Opaque pagination cursor")] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_PAGE_SIZE, description="Page size"),
    ] = DEFAULT_PAGE_SIZE,
    parent_id: Annotated[str | None, Query(description="Filter by parent id (admin/therapist only)")] = None,
    kindergarten_id: Annotated[str | None, Query(description="Filter by kindergarten id")] = None,
    search: Annotated[
        str | None,
        Query(min_length=1, max_length=120, description="Case-insensitive name match"),
    ] = None,
) -> Page[ChildPublic]:
    page_size = clamp_limit(limit)

    stmt = select(Child)

    # Role-based scope.
    if user.role == UserRole.PARENT.value:
        stmt = stmt.where(Child.parent_id == user.id)
        if parent_id and parent_id != user.id:
            raise ForbiddenError(
                "Parents may not filter by another parent.",
                code="PARENT_SCOPE_VIOLATION",
            )
    elif user.role == UserRole.TEACHER.value:
        if user.region_id is None:
            return Page[ChildPublic](items=[], next_cursor=None, has_more=False)
        stmt = stmt.join(
            Kindergarten,
            Kindergarten.id == Child.kindergarten_id,
        ).where(Kindergarten.region_id == user.region_id)
        if parent_id:
            stmt = stmt.where(Child.parent_id == parent_id)
    else:
        # admin / therapist
        if parent_id:
            stmt = stmt.where(Child.parent_id == parent_id)

    if kindergarten_id:
        stmt = stmt.where(Child.kindergarten_id == kindergarten_id)
    if search:
        like = f"%{search.lower()}%"
        stmt = stmt.where(Child.name.ilike(like))

    # Apply cursor (created_at, id) descending.
    if cursor:
        try:
            cursor_ts, cursor_id = decode_cursor(cursor)
        except ValueError as exc:
            raise ValidationError(str(exc), code="INVALID_CURSOR") from exc
        stmt = stmt.where(
            or_(
                Child.created_at < cursor_ts,
                and_(Child.created_at == cursor_ts, Child.id < cursor_id),
            )
        )

    stmt = stmt.order_by(Child.created_at.desc(), Child.id.desc()).limit(page_size + 1)

    result = await session.execute(stmt)
    rows: list[Child] = list(result.scalars().all())

    has_more = len(rows) > page_size
    page_items = rows[:page_size]
    next_cursor: str | None = None
    if has_more and page_items:
        last = page_items[-1]
        last_ts: datetime = last.created_at
        next_cursor = encode_cursor(last_ts, last.id)

    return Page[ChildPublic](
        items=[ChildPublic.from_model(c) for c in page_items],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get(
    "/children/{child_id}",
    response_model=ChildPublic,
    summary="Read a single child profile",
)
async def get_child(
    user: CurrentUser,
    session: DBSession,
    child_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> ChildPublic:
    child = await _load_child_or_404(session, child_id)
    if not _can_read(user, child):
        raise ForbiddenError(
            "You do not have access to this child.", code="CHILD_FORBIDDEN"
        )
    return ChildPublic.from_model(child)


@router.put(
    "/children/{child_id}",
    response_model=ChildPublic,
    summary="Update a child profile",
)
async def update_child(
    user: CurrentUser,
    session: DBSession,
    payload: ChildUpdate,
    child_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> ChildPublic:
    child = await _load_child_or_404(session, child_id)
    if not _can_mutate(user, child):
        raise ForbiddenError(
            "You do not have permission to modify this child.",
            code="CHILD_FORBIDDEN",
        )

    data = payload.model_dump(exclude_unset=True)

    if "kindergarten_id" in data:
        await _validate_kindergarten(session, data["kindergarten_id"])
        child.kindergarten_id = data["kindergarten_id"]
        # Re-derive tenant when the kindergarten changes — the child's
        # tenant follows their kindergarten so that scope filters stay
        # correct after a transfer between institutions.
        if data["kindergarten_id"] is not None:
            child.tenant_id = data["kindergarten_id"]
    if data.get("name") is not None:
        child.name = data["name"]
    if data.get("birth_date") is not None:
        child.birth_date = data["birth_date"]
    if data.get("gender") is not None:
        child.gender = data["gender"]
    if data.get("language") is not None:
        child.language = data["language"]
    if "notes" in data:
        child.notes = data["notes"]

    await session.commit()
    await session.refresh(child)
    return ChildPublic.from_model(child)


@router.delete(
    "/children/{child_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a child profile",
)
async def delete_child(
    user: CurrentUser,
    session: DBSession,
    child_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> Response:
    child = await _load_child_or_404(session, child_id)
    if not _can_mutate(user, child):
        raise ForbiddenError(
            "You do not have permission to delete this child.",
            code="CHILD_FORBIDDEN",
        )

    await session.delete(child)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------- Progress timeline


def _mean(values: list[float]) -> float | None:
    """Return the arithmetic mean, or ``None`` for an empty / NaN-only list."""

    cleaned = [v for v in values if v is not None]
    if not cleaned:
        return None
    return round(sum(cleaned) / len(cleaned), 3)


def _aggregate_voice_quality(
    analyses: list[AnalysisResult],
) -> tuple[float | None, float | None, float | None, float | None, list[str]]:
    """Roll voice-quality metrics + flag set across an assessment's analyses.

    Returns ``(jitter, shimmer, hnr, speech_rate, distinct_flags)`` where
    each numeric value is the mean of the available per-recording values
    and ``distinct_flags`` is the de-duplicated union of ``flags`` lists.
    """

    jitters: list[float] = []
    shimmers: list[float] = []
    hnrs: list[float] = []
    rates: list[float] = []
    flag_set: set[str] = set()

    for analysis in analyses:
        vq = analysis.voice_quality or {}
        if not isinstance(vq, dict):
            continue
        for key, sink in (
            ("jitter_local_pct", jitters),
            ("shimmer_local_pct", shimmers),
            ("hnr_db", hnrs),
            ("speech_rate_wpm", rates),
        ):
            value = vq.get(key)
            if isinstance(value, int | float) and value > 0:
                sink.append(float(value))
        flags = vq.get("flags")
        if isinstance(flags, list):
            flag_set.update(str(f) for f in flags if f)

    return (
        _mean(jitters),
        _mean(shimmers),
        _mean(hnrs),
        _mean(rates),
        sorted(flag_set),
    )


def _weakest_phonemes(analyses: list[AnalysisResult]) -> list[dict[str, Any]]:
    """Return up to 3 weakest phonemes from the lowest-mean-score recording."""

    best_candidate: tuple[float, list[dict[str, Any]]] | None = None
    for analysis in analyses:
        scores_payload = analysis.phoneme_scores or {}
        if not isinstance(scores_payload, dict):
            continue
        scores = scores_payload.get("scores")
        weakest = scores_payload.get("weakest")
        if not isinstance(scores, dict) or not scores:
            continue
        numeric_scores = [
            float(v) for v in scores.values() if isinstance(v, int | float)
        ]
        if not numeric_scores:
            continue
        mean_score = sum(numeric_scores) / len(numeric_scores)
        if best_candidate is None or mean_score < best_candidate[0]:
            if isinstance(weakest, list):
                cleaned: list[dict[str, Any]] = []
                for item in weakest[:3]:
                    if isinstance(item, dict) and "phoneme" in item:
                        cleaned.append(
                            {
                                "phoneme": str(item["phoneme"]),
                                "score": float(item.get("score") or 0.0),
                            }
                        )
                best_candidate = (mean_score, cleaned)
            else:
                best_candidate = (mean_score, [])
    return best_candidate[1] if best_candidate else []


def _build_progress_point(
    assessment: Assessment, analyses: list[AnalysisResult]
) -> ProgressPoint:
    """Compose a single :class:`ProgressPoint` from an assessment + its analyses."""

    jitter, shimmer, hnr, rate, flags = _aggregate_voice_quality(analyses)

    return ProgressPoint(
        assessment_id=assessment.id,
        assessment_type=assessment.type,
        status=assessment.status,
        completed_at=assessment.completed_at,
        created_at=assessment.created_at,
        overall_risk=assessment.overall_risk,
        overall_confidence=assessment.overall_confidence,
        jitter_local_pct=jitter,
        shimmer_local_pct=shimmer,
        hnr_db=hnr,
        speech_rate_wpm=rate,
        voice_quality_flags=flags,
        weakest_phonemes=_weakest_phonemes(analyses),
        recording_count=len(assessment.recordings),
    )


def _build_summary(points: list[ProgressPoint]) -> ChildProgressSummary:
    """Roll the timeline up into a single high-level summary block."""

    completed = [p for p in points if p.completed_at is not None]
    risk_counts: dict[str, int] = {level.value: 0 for level in RiskLevel}
    for point in points:
        if point.overall_risk and point.overall_risk in risk_counts:
            risk_counts[point.overall_risk] += 1

    latest = max(completed, key=lambda p: p.completed_at) if completed else None

    return ChildProgressSummary(
        total_assessments=len(points),
        completed_assessments=len(completed),
        last_completed_at=latest.completed_at if latest else None,
        risk_distribution=risk_counts,
        latest_risk=latest.overall_risk if latest else None,
        latest_confidence=latest.overall_confidence if latest else None,
    )


@router.get(
    "/children/{child_id}/progress",
    response_model=ChildProgressResponse,
    summary="Chronological progress timeline for a child",
)
async def get_child_progress(
    user: CurrentUser,
    session: DBSession,
    child_id: Annotated[str, Path(min_length=1, max_length=36)],
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_PAGE_SIZE,
            description=(
                "Maximum number of timeline points to return. Sorted "
                "oldest → newest by ``created_at`` so the mobile chart "
                "can render left-to-right without re-sorting."
            ),
        ),
    ] = MAX_PAGE_SIZE,
) -> ChildProgressResponse:
    """Return a chronological list of assessment data points for trend charts.

    Each point aggregates the assessment's recordings into a compact
    payload (overall risk, mean voice-quality metrics, weakest phonemes).
    Authorisation reuses the same role rules as the child detail endpoint.
    """

    child = await _load_child_or_404(session, child_id)
    if not _can_read(user, child):
        raise ForbiddenError(
            "You do not have access to this child.", code="CHILD_FORBIDDEN"
        )

    from sqlalchemy.orm import selectinload

    stmt = (
        select(Assessment)
        .where(Assessment.child_id == child.id)
        .options(
            selectinload(Assessment.recordings).selectinload(
                AudioRecording.analysis
            )
        )
        .order_by(Assessment.created_at.asc(), Assessment.id.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    assessments = list(result.scalars().unique().all())

    points: list[ProgressPoint] = []
    for assessment in assessments:
        analyses = [
            r.analysis for r in assessment.recordings if r.analysis is not None
        ]
        points.append(_build_progress_point(assessment, analyses))

    return ChildProgressResponse(
        child_id=child.id,
        summary=_build_summary(points),
        points=points,
    )


# --------------------------------------------------------------- Phoneme mastery


def _build_mastery_summary(rows: list[PhonemeMastery]) -> PhonemeMasterySummary:
    """Roll a list of mastery rows into the summary block."""

    if not rows:
        return PhonemeMasterySummary()

    total = len(rows)
    mastered = sum(1 for r in rows if r.mastered_at is not None)
    avg = sum(r.average_score for r in rows) / total
    last_seen: datetime | None = None
    for r in rows:
        if r.last_assessed_at is None:
            continue
        if last_seen is None or r.last_assessed_at > last_seen:
            last_seen = r.last_assessed_at
    return PhonemeMasterySummary(
        total_phonemes=total,
        mastered_phonemes=mastered,
        average_score=round(avg, 4),
        last_assessed_at=last_seen,
        mastery_threshold=MASTERY_THRESHOLD,
    )


@router.get(
    "/children/{child_id}/phoneme-mastery",
    response_model=PhonemeMasteryResponse,
    summary="Per-phoneme mastery stats for a child",
)
async def get_phoneme_mastery(
    user: CurrentUser,
    session: DBSession,
    child_id: Annotated[str, Path(min_length=1, max_length=36)],
    language: Annotated[
        str | None,
        Query(
            min_length=2,
            max_length=8,
            description=(
                "Filter by language code (e.g. ``uz``, ``ru``). "
                "Defaults to all languages."
            ),
        ),
    ] = None,
) -> PhonemeMasteryResponse:
    """Return aggregated phoneme-mastery rows for the given child.

    Each row exposes total / successful attempts, running average and
    best scores, and a ``mastered_at`` timestamp once the child crosses
    the mastery threshold. Authorisation reuses the same role rules
    as the child-detail endpoint.
    """

    child = await _load_child_or_404(session, child_id)
    if not _can_read(user, child):
        raise ForbiddenError(
            "You do not have access to this child.", code="CHILD_FORBIDDEN"
        )

    rows = await get_mastery_rows(session, child_id=child.id, language=language)

    return PhonemeMasteryResponse(
        child_id=child.id,
        summary=_build_mastery_summary(rows),
        items=[PhonemeMasteryPublic.from_model(r) for r in rows],
    )


def _latest_completed_assessment_voice_quality(
    session_assessments: list[Assessment],
) -> tuple[
    dict[str, Any] | None,
    str | None,
    datetime | None,
]:
    """Return ``(voice_quality, risk, completed_at)`` from the latest assessment.

    Walks completed assessments newest-first and returns the voice-quality
    snapshot from the first analysed recording. Returns ``(None, None, None)``
    when no completed assessment exists.
    """

    completed = [
        a for a in session_assessments if a.completed_at is not None
    ]
    if not completed:
        return None, None, None

    latest = max(completed, key=lambda a: a.completed_at)
    voice: dict[str, Any] | None = None
    for recording in latest.recordings:
        analysis = recording.analysis
        if analysis is None:
            continue
        if isinstance(analysis.voice_quality, dict) and analysis.voice_quality:
            voice = analysis.voice_quality
            break

    return voice, latest.overall_risk, latest.completed_at


@router.get(
    "/children/{child_id}/speech-profile",
    response_model=SpeechProfileResponse,
    summary="Aggregated speech profile (mastery + latest voice quality)",
)
async def get_speech_profile(
    user: CurrentUser,
    session: DBSession,
    child_id: Annotated[str, Path(min_length=1, max_length=36)],
    language: Annotated[
        str | None,
        Query(
            min_length=2,
            max_length=8,
            description="Filter mastery by language code.",
        ),
    ] = None,
    weakest_limit: Annotated[
        int,
        Query(
            ge=1,
            le=20,
            description="Maximum number of weakest phonemes to return.",
        ),
    ] = 3,
    strongest_limit: Annotated[
        int,
        Query(
            ge=1,
            le=20,
            description="Maximum number of strongest phonemes to return.",
        ),
    ] = 3,
) -> SpeechProfileResponse:
    """Return a one-shot speech overview for the mobile app.

    Combines phoneme-mastery aggregates with the latest assessment's
    voice-quality block so the screen can render its summary cards
    without making three round-trips. The endpoint is intentionally
    cheap: it issues at most two queries (mastery rows + latest
    assessments) and reuses the gamified risk_level.
    """

    child = await _load_child_or_404(session, child_id)
    if not _can_read(user, child):
        raise ForbiddenError(
            "You do not have access to this child.", code="CHILD_FORBIDDEN"
        )

    rows = await get_mastery_rows(
        session, child_id=child.id, language=language
    )
    summary = _build_mastery_summary(rows)

    # ``rows`` is already sorted ascending by average_score in the
    # service layer, so weakest = first N, strongest = last N reversed.
    weakest = [PhonemeMasteryPublic.from_model(r) for r in rows[:weakest_limit]]
    strongest = [
        PhonemeMasteryPublic.from_model(r)
        for r in list(reversed(rows))[:strongest_limit]
    ]
    mastered = sorted(
        [r for r in rows if r.mastered_at is not None],
        key=lambda r: r.mastered_at,
        reverse=True,
    )
    mastered_payload = [PhonemeMasteryPublic.from_model(r) for r in mastered]

    # Pull the latest completed assessment + its voice-quality block.
    from sqlalchemy.orm import selectinload

    stmt = (
        select(Assessment)
        .where(Assessment.child_id == child.id)
        .options(
            selectinload(Assessment.recordings).selectinload(
                AudioRecording.analysis
            )
        )
        .order_by(Assessment.created_at.desc(), Assessment.id.desc())
        .limit(20)
    )
    result = await session.execute(stmt)
    latest_assessments = list(result.scalars().unique().all())

    voice_quality, latest_risk, latest_completed = (
        _latest_completed_assessment_voice_quality(latest_assessments)
    )

    return SpeechProfileResponse(
        child_id=child.id,
        summary=summary,
        weakest_phonemes=weakest,
        strongest_phonemes=strongest,
        mastered_phonemes=mastered_payload,
        latest_voice_quality=voice_quality,
        latest_risk=latest_risk,
        latest_assessment_completed_at=latest_completed,
    )
