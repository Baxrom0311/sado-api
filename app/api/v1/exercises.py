"""Exercise and exercise-assignment endpoints.

Authorisation summary:

* ``parent`` — read-only on the exercise catalogue, full access to
  assignments for their own children. Parents may also self-assign an
  exercise to one of their children (e.g. picking from the daily list).
* ``teacher`` — read-only on the catalogue, may assign exercises to
  children in their region.
* ``therapist`` — full access: can create / update exercises and
  assign them to any child.
* ``admin`` — full access (including delete).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Path, Query, Response, UploadFile, status
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DBSession
from app.config import get_settings
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
from app.models.child import Child
from app.models.exercise import (
    AssignmentStatus,
    Exercise,
    ExerciseAgeGroup,
    ExerciseAssignment,
    ExerciseCategory,
    ExerciseDifficulty,
)
from app.models.kindergarten import Kindergarten
from app.models.user import User, UserRole
from app.schemas.exercise import (
    ExerciseAssignmentComplete,
    ExerciseAssignmentCreate,
    ExerciseAssignmentPublic,
    ExerciseAssignmentUpdate,
    ExerciseCreate,
    ExercisePublic,
    ExerciseUpdate,
)
from app.services.storage import get_audio_storage

router = APIRouter()


VALID_CATEGORIES = {c.value for c in ExerciseCategory}
VALID_AGE_GROUPS = {a.value for a in ExerciseAgeGroup}
VALID_DIFFICULTIES = {d.value for d in ExerciseDifficulty}


# --------------------------------------------------------------- Helpers


def _can_manage_exercises(user: User) -> bool:
    return user.role in {UserRole.ADMIN.value, UserRole.THERAPIST.value}


def _can_delete_exercises(user: User) -> bool:
    return user.role == UserRole.ADMIN.value


def _can_assign_to_child(user: User, child: Child) -> bool:
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


def _can_view_assignment(user: User, assignment: ExerciseAssignment) -> bool:
    if user.role in {UserRole.ADMIN.value, UserRole.THERAPIST.value}:
        return True
    child = assignment.child
    if user.role == UserRole.PARENT.value:
        return child.parent_id == user.id
    if user.role == UserRole.TEACHER.value:
        if user.region_id is None:
            return False
        kg = child.kindergarten
        return kg is not None and kg.region_id == user.region_id
    return False


def _can_modify_assignment(user: User, assignment: ExerciseAssignment) -> bool:
    if user.role == UserRole.ADMIN.value:
        return True
    if user.role == UserRole.THERAPIST.value:
        return True
    if user.role == UserRole.PARENT.value:
        return assignment.child.parent_id == user.id
    if user.role == UserRole.TEACHER.value:
        if user.region_id is None:
            return False
        kg = assignment.child.kindergarten
        return kg is not None and kg.region_id == user.region_id
    return False


async def _load_exercise_or_404(session: DBSession, exercise_id: str) -> Exercise:
    exercise = await session.get(Exercise, exercise_id)
    if exercise is None:
        raise NotFoundError("Exercise not found", code="EXERCISE_NOT_FOUND")
    return exercise


async def _load_child_or_404(session: DBSession, child_id: str) -> Child:
    stmt = (
        select(Child)
        .options(selectinload(Child.kindergarten))
        .where(Child.id == child_id)
    )
    child = (await session.execute(stmt)).scalar_one_or_none()
    if child is None:
        raise NotFoundError("Child not found", code="CHILD_NOT_FOUND")
    return child


async def _load_assignment_or_404(
    session: DBSession, assignment_id: str
) -> ExerciseAssignment:
    stmt = (
        select(ExerciseAssignment)
        .options(
            selectinload(ExerciseAssignment.exercise),
            selectinload(ExerciseAssignment.child).selectinload(Child.kindergarten),
        )
        .where(ExerciseAssignment.id == assignment_id)
    )
    assignment = (await session.execute(stmt)).scalar_one_or_none()
    if assignment is None:
        raise NotFoundError(
            "Assignment not found", code="ASSIGNMENT_NOT_FOUND"
        )
    return assignment


# ----------------------------------------------------- Exercise endpoints


@router.get(
    "/exercises",
    response_model=Page[ExercisePublic],
    summary="List exercises in the catalogue",
)
async def list_exercises(
    user: CurrentUser,
    session: DBSession,
    cursor: Annotated[str | None, Query(description="Opaque pagination cursor")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    category: Annotated[str | None, Query()] = None,
    age_group: Annotated[str | None, Query()] = None,
    difficulty: Annotated[str | None, Query()] = None,
    language: Annotated[str | None, Query(min_length=2, max_length=8)] = None,
    search: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    include_inactive: Annotated[bool, Query()] = False,
) -> Page[ExercisePublic]:
    page_size = clamp_limit(limit)

    if category is not None and category not in VALID_CATEGORIES:
        raise ValidationError(
            f"category must be one of {sorted(VALID_CATEGORIES)}",
            code="INVALID_CATEGORY",
        )
    if age_group is not None and age_group not in VALID_AGE_GROUPS:
        raise ValidationError(
            f"age_group must be one of {sorted(VALID_AGE_GROUPS)}",
            code="INVALID_AGE_GROUP",
        )
    if difficulty is not None and difficulty not in VALID_DIFFICULTIES:
        raise ValidationError(
            f"difficulty must be one of {sorted(VALID_DIFFICULTIES)}",
            code="INVALID_DIFFICULTY",
        )

    stmt = select(Exercise)
    if category:
        stmt = stmt.where(Exercise.category == category)
    if age_group:
        stmt = stmt.where(Exercise.age_group == age_group)
    if difficulty:
        stmt = stmt.where(Exercise.difficulty == difficulty)
    if language:
        stmt = stmt.where(Exercise.language == language.lower())
    if search:
        like = f"%{search.lower()}%"
        stmt = stmt.where(Exercise.title.ilike(like))
    if not include_inactive or not _can_manage_exercises(user):
        stmt = stmt.where(Exercise.is_active.is_(True))

    if cursor:
        try:
            cursor_ts, cursor_id = decode_cursor(cursor)
        except ValueError as exc:
            raise ValidationError(str(exc), code="INVALID_CURSOR") from exc
        stmt = stmt.where(
            or_(
                Exercise.created_at < cursor_ts,
                and_(
                    Exercise.created_at == cursor_ts,
                    Exercise.id < cursor_id,
                ),
            )
        )

    stmt = stmt.order_by(Exercise.created_at.desc(), Exercise.id.desc()).limit(
        page_size + 1
    )

    rows = list((await session.execute(stmt)).scalars().all())
    has_more = len(rows) > page_size
    page_items = rows[:page_size]
    next_cursor: str | None = None
    if has_more and page_items:
        last = page_items[-1]
        next_cursor = encode_cursor(last.created_at, last.id)

    return Page[ExercisePublic](
        items=[ExercisePublic.model_validate(e) for e in page_items],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.post(
    "/exercises",
    response_model=ExercisePublic,
    status_code=status.HTTP_201_CREATED,
    summary="Create an exercise (therapist or admin)",
)
async def create_exercise(
    payload: ExerciseCreate,
    user: CurrentUser,
    session: DBSession,
) -> ExercisePublic:
    if not _can_manage_exercises(user):
        raise ForbiddenError(
            "Only therapists and admins may create exercises.",
            code="EXERCISE_FORBIDDEN",
        )

    exercise = Exercise(
        title=payload.title,
        description=payload.description,
        category=payload.category,
        age_group=payload.age_group,
        difficulty=payload.difficulty,
        language=payload.language,
        duration_minutes=payload.duration_minutes,
        audio_example_path=payload.audio_example_path,
        image_path=payload.image_path,
        instructions=payload.instructions,
        target_phonemes=payload.target_phonemes,
        is_active=payload.is_active,
        created_by_id=user.id,
    )
    session.add(exercise)
    try:
        await session.commit()
    except IntegrityError as exc:  # pragma: no cover
        await session.rollback()
        raise ConflictError(
            "Could not create exercise.", code="EXERCISE_CONFLICT"
        ) from exc
    await session.refresh(exercise)
    return ExercisePublic.model_validate(exercise)


@router.get(
    "/exercises/{exercise_id}",
    response_model=ExercisePublic,
    summary="Read a single exercise",
)
async def get_exercise(
    user: CurrentUser,
    session: DBSession,
    exercise_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> ExercisePublic:
    exercise = await _load_exercise_or_404(session, exercise_id)
    if not exercise.is_active and not _can_manage_exercises(user):
        raise NotFoundError("Exercise not found", code="EXERCISE_NOT_FOUND")
    return ExercisePublic.model_validate(exercise)


@router.put(
    "/exercises/{exercise_id}",
    response_model=ExercisePublic,
    summary="Update an exercise (therapist or admin)",
)
async def update_exercise(
    payload: ExerciseUpdate,
    user: CurrentUser,
    session: DBSession,
    exercise_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> ExercisePublic:
    if not _can_manage_exercises(user):
        raise ForbiddenError(
            "Only therapists and admins may update exercises.",
            code="EXERCISE_FORBIDDEN",
        )
    exercise = await _load_exercise_or_404(session, exercise_id)
    data = payload.model_dump(exclude_unset=True)
    for field in (
        "title",
        "description",
        "category",
        "age_group",
        "difficulty",
        "language",
        "duration_minutes",
        "audio_example_path",
        "image_path",
        "instructions",
        "target_phonemes",
        "is_active",
    ):
        if field in data:
            setattr(exercise, field, data[field])

    await session.commit()
    await session.refresh(exercise)
    return ExercisePublic.model_validate(exercise)


@router.delete(
    "/exercises/{exercise_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete an exercise (admin only)",
)
async def delete_exercise(
    user: CurrentUser,
    session: DBSession,
    exercise_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> Response:
    if not _can_delete_exercises(user):
        raise ForbiddenError(
            "Only admins may delete exercises.", code="EXERCISE_FORBIDDEN"
        )
    exercise = await _load_exercise_or_404(session, exercise_id)
    await session.delete(exercise)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------- Asset upload

logger = logging.getLogger(__name__)

# What we'll accept on the asset upload endpoint. The storage layer
# itself doesn't care about content types, but we limit them here so a
# user can't paste a binary blob and call it an "audio example".
ALLOWED_AUDIO_TYPES = {
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
ALLOWED_IMAGE_TYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/gif",
    "image/svg+xml",
}

# Per-file size limits — keep audio examples small so the catalogue
# doesn't bloat MinIO storage.
MAX_AUDIO_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024

_AUDIO_EXTENSIONS = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/ogg": "ogg",
    "audio/webm": "webm",
    "audio/flac": "flac",
}
_IMAGE_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/svg+xml": "svg",
}


def _build_asset_key(
    *, exercise_id: str, asset_type: str, asset_id: str, content_type: str
) -> str:
    """Deterministic storage key per asset.

    Layout: ``exercises/{id}/{type}/{asset_id}.{ext}``.
    """

    if asset_type == "audio":
        ext = _AUDIO_EXTENSIONS.get(content_type, "bin")
    else:
        ext = _IMAGE_EXTENSIONS.get(content_type, "bin")
    safe_exercise = quote(exercise_id, safe="")
    safe_asset = quote(asset_id, safe="")
    return f"exercises/{safe_exercise}/{asset_type}/{safe_asset}.{ext}"


@router.post(
    "/exercises/{exercise_id}/assets",
    response_model=ExercisePublic,
    summary="Upload an audio or image example for an exercise",
)
async def upload_exercise_asset(
    user: CurrentUser,
    session: DBSession,
    exercise_id: Annotated[str, Path(min_length=1, max_length=36)],
    file: Annotated[UploadFile, File(description="Audio or image file")],
    asset_type: Annotated[
        Literal["audio", "image"],
        Form(description="Which slot to update — 'audio' or 'image'"),
    ],
) -> ExercisePublic:
    """Persist a multipart audio/image asset for an exercise.

    The previous file (if any) is best-effort deleted so the bucket
    doesn't accumulate orphaned objects. Storage failures during the
    cleanup are logged but never block the upload.
    """

    if not _can_manage_exercises(user):
        raise ForbiddenError(
            "Only therapists and admins may upload exercise assets.",
            code="EXERCISE_FORBIDDEN",
        )

    exercise = await _load_exercise_or_404(session, exercise_id)

    settings = get_settings()
    content_type = (file.content_type or "").lower().strip()
    if asset_type == "audio":
        allowed = ALLOWED_AUDIO_TYPES
        max_bytes = MAX_AUDIO_BYTES
        slot = "audio_example_path"
    else:
        allowed = ALLOWED_IMAGE_TYPES
        max_bytes = MAX_IMAGE_BYTES
        slot = "image_path"

    if content_type not in allowed:
        raise ValidationError(
            f"content_type {content_type or 'unknown'!r} is not accepted for "
            f"asset_type={asset_type!r}",
            code="INVALID_ASSET_TYPE",
        )

    raw = await file.read()
    if not raw:
        raise ValidationError(
            "uploaded file is empty", code="ASSET_EMPTY"
        )
    if len(raw) > max_bytes:
        raise ValidationError(
            f"{asset_type} asset is {len(raw) / (1024 * 1024):.2f} MB — "
            f"limit is {max_bytes // (1024 * 1024)} MB",
            code="ASSET_TOO_LARGE",
        )

    asset_id = str(uuid.uuid4())
    storage_key = _build_asset_key(
        exercise_id=exercise.id,
        asset_type=asset_type,
        asset_id=asset_id,
        content_type=content_type,
    )

    storage = get_audio_storage()
    stored = await storage.put_object(
        key=storage_key, data=raw, content_type=content_type
    )

    previous_key = getattr(exercise, slot)
    setattr(exercise, slot, stored.storage_key)
    await session.commit()
    await session.refresh(exercise)

    if previous_key and previous_key != stored.storage_key:
        try:
            await storage.delete_object(previous_key)
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning(
                "Could not delete previous asset %s for exercise %s: %s",
                previous_key,
                exercise.id,
                exc,
            )

    # Mark settings as referenced so the linter doesn't flag the
    # import — `get_settings()` is also kept available for future
    # per-asset configuration.
    _ = settings
    return ExercisePublic.model_validate(exercise)


@router.delete(
    "/exercises/{exercise_id}/assets/{asset_type}",
    response_model=ExercisePublic,
    summary="Remove an audio or image asset from an exercise",
)
async def delete_exercise_asset(
    user: CurrentUser,
    session: DBSession,
    exercise_id: Annotated[str, Path(min_length=1, max_length=36)],
    asset_type: Annotated[Literal["audio", "image"], Path()],
) -> ExercisePublic:
    if not _can_manage_exercises(user):
        raise ForbiddenError(
            "Only therapists and admins may modify exercise assets.",
            code="EXERCISE_FORBIDDEN",
        )

    exercise = await _load_exercise_or_404(session, exercise_id)
    slot = "audio_example_path" if asset_type == "audio" else "image_path"
    previous_key = getattr(exercise, slot)
    if previous_key is None:
        return ExercisePublic.model_validate(exercise)

    setattr(exercise, slot, None)
    await session.commit()
    await session.refresh(exercise)

    try:
        await get_audio_storage().delete_object(previous_key)
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning(
            "Could not delete asset %s for exercise %s: %s",
            previous_key,
            exercise.id,
            exc,
        )

    return ExercisePublic.model_validate(exercise)


# ---------------------------------------------------- Assignment endpoints


@router.post(
    "/exercises/{child_id}/assign",
    response_model=ExerciseAssignmentPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Assign an exercise to a child",
)
async def assign_exercise(
    payload: ExerciseAssignmentCreate,
    user: CurrentUser,
    session: DBSession,
    child_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> ExerciseAssignmentPublic:
    child = await _load_child_or_404(session, child_id)
    if not _can_assign_to_child(user, child):
        raise ForbiddenError(
            "You may not assign exercises to this child.",
            code="ASSIGNMENT_FORBIDDEN",
        )

    exercise = await _load_exercise_or_404(session, payload.exercise_id)
    if not exercise.is_active and user.role not in {
        UserRole.ADMIN.value,
        UserRole.THERAPIST.value,
    }:
        raise ValidationError(
            "Exercise is not active.", code="EXERCISE_INACTIVE"
        )

    assignment = ExerciseAssignment(
        child_id=child.id,
        exercise_id=exercise.id,
        assigned_by_id=user.id,
        status=AssignmentStatus.PENDING.value,
        due_date=payload.due_date,
        notes=payload.notes,
    )
    session.add(assignment)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            "An assignment for this child + exercise + due date already exists.",
            code="ASSIGNMENT_DUPLICATE",
        ) from exc
    await session.refresh(assignment)

    # Reload with eager relationships for the response payload.
    loaded = await _load_assignment_or_404(session, assignment.id)
    return ExerciseAssignmentPublic.model_validate(loaded)


@router.get(
    "/exercises/{child_id}/assignments",
    response_model=Page[ExerciseAssignmentPublic],
    summary="List exercise assignments for a child",
)
async def list_child_assignments(
    user: CurrentUser,
    session: DBSession,
    child_id: Annotated[str, Path(min_length=1, max_length=36)],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    status_filter: Annotated[
        str | None, Query(alias="status", description="Filter by assignment status")
    ] = None,
) -> Page[ExerciseAssignmentPublic]:
    page_size = clamp_limit(limit)
    child = await _load_child_or_404(session, child_id)

    # Re-use the can-assign check as can-read; therapists/admins always
    # see assignments, parents see their own kids, teachers see their
    # region.
    if not _can_assign_to_child(user, child):
        raise ForbiddenError(
            "You may not view assignments for this child.",
            code="ASSIGNMENT_FORBIDDEN",
        )

    if status_filter is not None and status_filter not in {
        s.value for s in AssignmentStatus
    }:
        raise ValidationError(
            "Invalid assignment status.", code="INVALID_ASSIGNMENT_STATUS"
        )

    stmt = (
        select(ExerciseAssignment)
        .options(
            selectinload(ExerciseAssignment.exercise),
            selectinload(ExerciseAssignment.child).selectinload(Child.kindergarten),
        )
        .where(ExerciseAssignment.child_id == child.id)
    )
    if status_filter:
        stmt = stmt.where(ExerciseAssignment.status == status_filter)

    if cursor:
        try:
            cursor_ts, cursor_id = decode_cursor(cursor)
        except ValueError as exc:
            raise ValidationError(str(exc), code="INVALID_CURSOR") from exc
        stmt = stmt.where(
            or_(
                ExerciseAssignment.created_at < cursor_ts,
                and_(
                    ExerciseAssignment.created_at == cursor_ts,
                    ExerciseAssignment.id < cursor_id,
                ),
            )
        )

    stmt = stmt.order_by(
        ExerciseAssignment.created_at.desc(), ExerciseAssignment.id.desc()
    ).limit(page_size + 1)

    rows = list((await session.execute(stmt)).scalars().unique().all())
    has_more = len(rows) > page_size
    page_items = rows[:page_size]
    next_cursor: str | None = None
    if has_more and page_items:
        last = page_items[-1]
        next_cursor = encode_cursor(last.created_at, last.id)

    return Page[ExerciseAssignmentPublic](
        items=[ExerciseAssignmentPublic.model_validate(a) for a in page_items],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get(
    "/exercises/assignments/me",
    response_model=Page[ExerciseAssignmentPublic],
    summary="List assignments for the caller's own children (parent helper)",
)
async def list_my_assignments(
    user: CurrentUser,
    session: DBSession,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> Page[ExerciseAssignmentPublic]:
    page_size = clamp_limit(limit)

    stmt = (
        select(ExerciseAssignment)
        .join(Child, Child.id == ExerciseAssignment.child_id)
        .options(
            selectinload(ExerciseAssignment.exercise),
            selectinload(ExerciseAssignment.child).selectinload(Child.kindergarten),
        )
    )

    if user.role == UserRole.PARENT.value:
        stmt = stmt.where(Child.parent_id == user.id)
    elif user.role == UserRole.TEACHER.value:
        if user.region_id is None:
            return Page[ExerciseAssignmentPublic](
                items=[], next_cursor=None, has_more=False
            )
        stmt = stmt.join(
            Kindergarten, Kindergarten.id == Child.kindergarten_id
        ).where(Kindergarten.region_id == user.region_id)
    # admins / therapists see everything

    if status_filter:
        if status_filter not in {s.value for s in AssignmentStatus}:
            raise ValidationError(
                "Invalid assignment status.", code="INVALID_ASSIGNMENT_STATUS"
            )
        stmt = stmt.where(ExerciseAssignment.status == status_filter)

    if cursor:
        try:
            cursor_ts, cursor_id = decode_cursor(cursor)
        except ValueError as exc:
            raise ValidationError(str(exc), code="INVALID_CURSOR") from exc
        stmt = stmt.where(
            or_(
                ExerciseAssignment.created_at < cursor_ts,
                and_(
                    ExerciseAssignment.created_at == cursor_ts,
                    ExerciseAssignment.id < cursor_id,
                ),
            )
        )

    stmt = stmt.order_by(
        ExerciseAssignment.created_at.desc(), ExerciseAssignment.id.desc()
    ).limit(page_size + 1)

    rows = list((await session.execute(stmt)).scalars().unique().all())
    has_more = len(rows) > page_size
    page_items = rows[:page_size]
    next_cursor: str | None = None
    if has_more and page_items:
        last = page_items[-1]
        next_cursor = encode_cursor(last.created_at, last.id)

    return Page[ExerciseAssignmentPublic](
        items=[ExerciseAssignmentPublic.model_validate(a) for a in page_items],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.put(
    "/exercises/assignments/{assignment_id}",
    response_model=ExerciseAssignmentPublic,
    summary="Update an exercise assignment (status, due date, notes, score)",
)
async def update_assignment(
    payload: ExerciseAssignmentUpdate,
    user: CurrentUser,
    session: DBSession,
    assignment_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> ExerciseAssignmentPublic:
    assignment = await _load_assignment_or_404(session, assignment_id)
    if not _can_modify_assignment(user, assignment):
        raise ForbiddenError(
            "You may not modify this assignment.",
            code="ASSIGNMENT_FORBIDDEN",
        )

    data = payload.model_dump(exclude_unset=True)
    if "status" in data and data["status"] is not None:
        new_status = data["status"]
        assignment.status = new_status
        if new_status == AssignmentStatus.COMPLETED.value and assignment.completed_at is None:
            assignment.completed_at = datetime.now(UTC)
        if new_status != AssignmentStatus.COMPLETED.value and assignment.completed_at is not None:
            # Re-opening an assignment clears completion timestamp.
            assignment.completed_at = None
    if "due_date" in data:
        assignment.due_date = data["due_date"]
    if "notes" in data:
        assignment.notes = data["notes"]
    if "score" in data:
        assignment.score = data["score"]

    await session.commit()
    await session.refresh(assignment)
    return ExerciseAssignmentPublic.model_validate(assignment)


@router.put(
    "/exercises/assignments/{assignment_id}/complete",
    response_model=ExerciseAssignmentPublic,
    summary="Mark an assignment complete with an optional score",
)
async def complete_assignment(
    payload: ExerciseAssignmentComplete,
    user: CurrentUser,
    session: DBSession,
    assignment_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> ExerciseAssignmentPublic:
    assignment = await _load_assignment_or_404(session, assignment_id)
    if not _can_modify_assignment(user, assignment):
        raise ForbiddenError(
            "You may not complete this assignment.",
            code="ASSIGNMENT_FORBIDDEN",
        )

    assignment.status = AssignmentStatus.COMPLETED.value
    assignment.completed_at = datetime.now(UTC)
    if payload.score is not None:
        assignment.score = payload.score
    if payload.notes is not None:
        assignment.notes = payload.notes

    await session.commit()
    await session.refresh(assignment)
    return ExerciseAssignmentPublic.model_validate(assignment)


@router.get(
    "/exercises/assignments/{assignment_id}",
    response_model=ExerciseAssignmentPublic,
    summary="Read one assignment",
)
async def get_assignment(
    user: CurrentUser,
    session: DBSession,
    assignment_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> ExerciseAssignmentPublic:
    assignment = await _load_assignment_or_404(session, assignment_id)
    if not _can_view_assignment(user, assignment):
        raise ForbiddenError(
            "You may not view this assignment.",
            code="ASSIGNMENT_FORBIDDEN",
        )
    return ExerciseAssignmentPublic.model_validate(assignment)


@router.delete(
    "/exercises/assignments/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete an assignment",
)
async def delete_assignment(
    user: CurrentUser,
    session: DBSession,
    assignment_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> Response:
    assignment = await _load_assignment_or_404(session, assignment_id)
    if not _can_modify_assignment(user, assignment):
        raise ForbiddenError(
            "You may not delete this assignment.",
            code="ASSIGNMENT_FORBIDDEN",
        )
    await session.delete(assignment)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
