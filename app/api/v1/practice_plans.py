"""Practice-plan + plan-item endpoints.

Authorisation summary:

* ``parent``  — full read access on plans for their own children, may
  bump ``completed_count`` via the ``/complete`` endpoint, may NOT
  create new plans or delete existing ones (those flow from a
  therapist or the auto-generator).
* ``teacher`` — read-only on plans for children in their region.
* ``therapist`` — full CRUD on plans for any child they can see.
* ``admin`` — full CRUD.

The endpoints intentionally avoid leaking data across tenants by
deferring to the same ``_can_read_child`` / ``_can_mutate_child`` rules
used by :mod:`app.api.v1.children`. We co-locate them here rather than
importing private helpers to keep the API module self-contained.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Response, status
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

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
from app.models.assessment import Assessment
from app.models.child import Child
from app.models.exercise import Exercise
from app.models.kindergarten import Kindergarten
from app.models.practice_plan import (
    PracticePlan,
    PracticePlanItem,
    PracticePlanItemStatus,
    PracticePlanStatus,
)
from app.models.user import User, UserRole
from app.schemas.practice_plan import (
    PracticePlanCreate,
    PracticePlanDetail,
    PracticePlanGenerateRequest,
    PracticePlanItemComplete,
    PracticePlanItemCreate,
    PracticePlanItemPublic,
    PracticePlanItemUpdate,
    PracticePlanPublic,
    PracticePlanUpdate,
)
from app.services.practice_plan import (
    generate_plan_from_assessment,
    load_plan_with_items,
    serialize_item,
    serialize_plan,
)

router = APIRouter()


# --------------------------------------------------------------- Authorization helpers


def _can_read_child(user: User, child: Child) -> bool:
    if user.role in {UserRole.ADMIN.value, UserRole.SUPER_ADMIN.value}:
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


def _can_mutate_plan(user: User) -> bool:
    """Roles allowed to create / edit / delete plans wholesale."""

    return user.role in {
        UserRole.ADMIN.value,
        UserRole.SUPER_ADMIN.value,
        UserRole.THERAPIST.value,
    }


def _can_progress_plan(user: User, plan: PracticePlan) -> bool:
    """Roles allowed to advance ``completed_count`` on items.

    Parents can mark practice done for their own child even though they
    cannot author plans.
    """

    if _can_mutate_plan(user):
        return True
    if user.role == UserRole.PARENT.value:
        child = plan.child
        return child is not None and child.parent_id == user.id
    return False


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


async def _load_plan_or_404(
    session: DBSession, plan_id: str
) -> PracticePlan:
    plan = await load_plan_with_items(session, plan_id)
    if plan is None:
        raise NotFoundError(
            "Practice plan not found", code="PRACTICE_PLAN_NOT_FOUND"
        )
    return plan


async def _load_item_or_404(
    plan: PracticePlan, item_id: str
) -> PracticePlanItem:
    for item in plan.items:
        if item.id == item_id:
            return item
    raise NotFoundError(
        "Plan item not found", code="PRACTICE_PLAN_ITEM_NOT_FOUND"
    )


async def _validate_exercise_or_404(
    session: DBSession, exercise_id: str
) -> Exercise:
    exercise = await session.get(Exercise, exercise_id)
    if exercise is None:
        raise NotFoundError(
            "Exercise not found", code="EXERCISE_NOT_FOUND"
        )
    if not exercise.is_active:
        raise ValidationError(
            "Exercise is inactive and cannot be added to a plan.",
            code="EXERCISE_INACTIVE",
        )
    return exercise


def _ensure_can_read(user: User, plan: PracticePlan) -> None:
    if not _can_read_child(user, plan.child):
        raise ForbiddenError(
            "You do not have access to this practice plan.",
            code="PRACTICE_PLAN_FORBIDDEN",
        )


def _ensure_can_mutate(user: User, plan: PracticePlan) -> None:
    _ensure_can_read(user, plan)
    if not _can_mutate_plan(user):
        raise ForbiddenError(
            "You do not have permission to modify practice plans.",
            code="PRACTICE_PLAN_FORBIDDEN",
        )


def _ensure_can_progress(user: User, plan: PracticePlan) -> None:
    _ensure_can_read(user, plan)
    if not _can_progress_plan(user, plan):
        raise ForbiddenError(
            "You do not have permission to update plan progress.",
            code="PRACTICE_PLAN_FORBIDDEN",
        )


# --------------------------------------------------------------- Endpoints


@router.post(
    "/practice-plans",
    response_model=PracticePlanDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a practice plan manually",
)
async def create_practice_plan(
    payload: PracticePlanCreate,
    user: CurrentUser,
    session: DBSession,
) -> dict[str, Any]:
    """Author a plan by hand. Therapists / admins only.

    Items can be supplied in the same payload, or added afterwards via
    ``POST /practice-plans/{id}/items``.
    """

    if not _can_mutate_plan(user):
        raise ForbiddenError(
            "Only therapists or admins can create practice plans.",
            code="PRACTICE_PLAN_FORBIDDEN",
        )

    child = await _load_child_or_404(session, payload.child_id)
    if not _can_read_child(user, child):
        raise ForbiddenError(
            "You do not have access to this child.",
            code="CHILD_FORBIDDEN",
        )

    if payload.assessment_id is not None:
        assessment = await session.get(Assessment, payload.assessment_id)
        if assessment is None:
            raise NotFoundError(
                "Assessment not found", code="ASSESSMENT_NOT_FOUND"
            )
        if assessment.child_id != child.id:
            raise ValidationError(
                "Assessment belongs to a different child.",
                code="ASSESSMENT_CHILD_MISMATCH",
            )

    plan = PracticePlan(
        child_id=child.id,
        assessment_id=payload.assessment_id,
        created_by_id=user.id,
        tenant_id=child.tenant_id,
        title=payload.title,
        summary=payload.summary,
        status=payload.status,
        locale=payload.locale,
        focus_areas=list(payload.focus_areas) if payload.focus_areas else None,
        start_date=payload.start_date,
        end_date=payload.end_date,
    )
    session.add(plan)
    await session.flush()  # need plan.id for FK below

    for item_payload in payload.items:
        exercise = await _validate_exercise_or_404(
            session, item_payload.exercise_id
        )
        session.add(
            PracticePlanItem(
                plan_id=plan.id,
                exercise_id=exercise.id,
                priority=item_payload.priority,
                target_count=item_payload.target_count,
                focus_code=item_payload.focus_code,
                notes=item_payload.notes,
            )
        )

    try:
        await session.commit()
    except IntegrityError as exc:  # pragma: no cover - rare race
        await session.rollback()
        raise ConflictError(
            "Could not save practice plan due to a conflicting reference.",
            code="PRACTICE_PLAN_CONFLICT",
        ) from exc

    plan = await _load_plan_or_404(session, plan.id)
    return serialize_plan(plan, include_items=True)


@router.post(
    "/practice-plans/generate",
    response_model=PracticePlanDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a practice plan from an assessment",
)
async def generate_practice_plan(
    payload: PracticePlanGenerateRequest,
    user: CurrentUser,
    session: DBSession,
) -> dict[str, Any]:
    """Run the auto-generator and persist the resulting plan.

    Therapists, admins, and the assessment-owning parent are all
    permitted callers — parents get an instant plan when they finish
    an at-home assessment.
    """

    stmt = (
        select(Assessment)
        .where(Assessment.id == payload.assessment_id)
        .options(
            selectinload(Assessment.child).selectinload(Child.kindergarten),
        )
    )
    assessment = (await session.execute(stmt)).scalar_one_or_none()
    if assessment is None:
        raise NotFoundError(
            "Assessment not found", code="ASSESSMENT_NOT_FOUND"
        )

    if not _can_read_child(user, assessment.child):
        raise ForbiddenError(
            "You do not have access to this assessment.",
            code="ASSESSMENT_FORBIDDEN",
        )

    plan = await generate_plan_from_assessment(
        session,
        assessment_id=payload.assessment_id,
        locale=payload.locale,
        max_items=payload.max_items,
        activate=payload.activate,
        title_override=payload.title,
        created_by_id=user.id,
    )

    plan = await _load_plan_or_404(session, plan.id)
    return serialize_plan(plan, include_items=True)


@router.get(
    "/practice-plans",
    response_model=Page[PracticePlanPublic],
    summary="List practice plans visible to the caller",
)
async def list_practice_plans(
    user: CurrentUser,
    session: DBSession,
    cursor: Annotated[
        str | None,
        Query(description="Opaque pagination cursor"),
    ] = None,
    limit: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, description="Page size")
    ] = DEFAULT_PAGE_SIZE,
    child_id: Annotated[
        str | None,
        Query(description="Filter to plans for a single child"),
    ] = None,
    status_filter: Annotated[
        str | None,
        Query(
            alias="status",
            description="Filter by plan status (draft|active|completed|archived)",
        ),
    ] = None,
) -> Page[PracticePlanPublic]:
    page_size = clamp_limit(limit)

    stmt = select(PracticePlan).options(
        selectinload(PracticePlan.items),
        selectinload(PracticePlan.child).selectinload(Child.kindergarten),
    )

    # Role-based scope.
    if user.role == UserRole.PARENT.value:
        stmt = stmt.join(Child, Child.id == PracticePlan.child_id).where(
            Child.parent_id == user.id
        )
    elif user.role == UserRole.TEACHER.value:
        if user.region_id is None:
            return Page[PracticePlanPublic](items=[], next_cursor=None, has_more=False)
        stmt = stmt.join(Child, Child.id == PracticePlan.child_id).join(
            Kindergarten, Kindergarten.id == Child.kindergarten_id
        ).where(Kindergarten.region_id == user.region_id)
    # Therapist / Admin / SuperAdmin: no extra scope filter.

    if child_id is not None:
        stmt = stmt.where(PracticePlan.child_id == child_id)
    if status_filter is not None:
        valid = {s.value for s in PracticePlanStatus}
        if status_filter not in valid:
            raise ValidationError(
                f"status must be one of {sorted(valid)}",
                code="INVALID_PLAN_STATUS",
            )
        stmt = stmt.where(PracticePlan.status == status_filter)

    if cursor:
        try:
            cursor_ts, cursor_id = decode_cursor(cursor)
        except ValueError as exc:
            raise ValidationError(str(exc), code="INVALID_CURSOR") from exc
        stmt = stmt.where(
            or_(
                PracticePlan.created_at < cursor_ts,
                and_(
                    PracticePlan.created_at == cursor_ts,
                    PracticePlan.id < cursor_id,
                ),
            )
        )

    stmt = stmt.order_by(
        PracticePlan.created_at.desc(), PracticePlan.id.desc()
    ).limit(page_size + 1)

    result = await session.execute(stmt)
    rows: list[PracticePlan] = list(result.scalars().unique().all())

    has_more = len(rows) > page_size
    page_items = rows[:page_size]
    next_cursor: str | None = None
    if has_more and page_items:
        last = page_items[-1]
        next_cursor = encode_cursor(last.created_at, last.id)

    return Page[PracticePlanPublic](
        items=[
            PracticePlanPublic.model_validate(serialize_plan(p))
            for p in page_items
        ],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get(
    "/practice-plans/{plan_id}",
    response_model=PracticePlanDetail,
    summary="Read a practice plan with its items",
)
async def get_practice_plan(
    user: CurrentUser,
    session: DBSession,
    plan_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> dict[str, Any]:
    plan = await _load_plan_or_404(session, plan_id)
    _ensure_can_read(user, plan)
    return serialize_plan(plan, include_items=True)


@router.put(
    "/practice-plans/{plan_id}",
    response_model=PracticePlanDetail,
    summary="Update top-level fields of a practice plan",
)
async def update_practice_plan(
    user: CurrentUser,
    session: DBSession,
    payload: PracticePlanUpdate,
    plan_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> dict[str, Any]:
    plan = await _load_plan_or_404(session, plan_id)
    _ensure_can_mutate(user, plan)

    data = payload.model_dump(exclude_unset=True)
    if "title" in data:
        plan.title = data["title"]
    if "summary" in data:
        plan.summary = data["summary"]
    if "locale" in data and data["locale"] is not None:
        plan.locale = data["locale"]
    if "focus_areas" in data:
        plan.focus_areas = (
            list(data["focus_areas"]) if data["focus_areas"] else None
        )
    if "start_date" in data:
        plan.start_date = data["start_date"]
    if "end_date" in data:
        plan.end_date = data["end_date"]
    if "status" in data and data["status"] is not None:
        plan.status = data["status"]
        if plan.status == PracticePlanStatus.COMPLETED.value:
            plan.completed_at = datetime.now(UTC)
        elif plan.status != PracticePlanStatus.COMPLETED.value:
            # Reopening a completed plan clears the timestamp.
            plan.completed_at = None

    await session.commit()
    plan = await _load_plan_or_404(session, plan.id)
    return serialize_plan(plan, include_items=True)


@router.delete(
    "/practice-plans/{plan_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a practice plan",
)
async def delete_practice_plan(
    user: CurrentUser,
    session: DBSession,
    plan_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> Response:
    plan = await _load_plan_or_404(session, plan_id)
    _ensure_can_mutate(user, plan)

    await session.delete(plan)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------- Item endpoints


@router.post(
    "/practice-plans/{plan_id}/items",
    response_model=PracticePlanItemPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Add an item to a plan",
)
async def add_practice_plan_item(
    user: CurrentUser,
    session: DBSession,
    payload: PracticePlanItemCreate,
    plan_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> dict[str, Any]:
    plan = await _load_plan_or_404(session, plan_id)
    _ensure_can_mutate(user, plan)

    exercise = await _validate_exercise_or_404(session, payload.exercise_id)

    item = PracticePlanItem(
        plan_id=plan.id,
        exercise_id=exercise.id,
        priority=payload.priority,
        target_count=payload.target_count,
        focus_code=payload.focus_code,
        notes=payload.notes,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item, attribute_names=["exercise"])
    return serialize_item(item)


@router.put(
    "/practice-plans/{plan_id}/items/{item_id}",
    response_model=PracticePlanItemPublic,
    summary="Update a plan item",
)
async def update_practice_plan_item(
    user: CurrentUser,
    session: DBSession,
    payload: PracticePlanItemUpdate,
    plan_id: Annotated[str, Path(min_length=1, max_length=36)],
    item_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> dict[str, Any]:
    plan = await _load_plan_or_404(session, plan_id)
    _ensure_can_mutate(user, plan)
    item = await _load_item_or_404(plan, item_id)

    data = payload.model_dump(exclude_unset=True)
    if "priority" in data and data["priority"] is not None:
        item.priority = data["priority"]
    if "target_count" in data and data["target_count"] is not None:
        item.target_count = data["target_count"]
    if "completed_count" in data and data["completed_count"] is not None:
        item.completed_count = min(data["completed_count"], item.target_count)
    if "focus_code" in data:
        item.focus_code = data["focus_code"]
    if "notes" in data:
        item.notes = data["notes"]
    if "status" in data and data["status"] is not None:
        item.status = data["status"]
        if item.status == PracticePlanItemStatus.COMPLETED.value:
            item.completed_count = item.target_count
            item.completed_at = datetime.now(UTC)
        elif item.status != PracticePlanItemStatus.COMPLETED.value:
            item.completed_at = None

    await session.commit()
    await session.refresh(item, attribute_names=["exercise"])
    return serialize_item(item)


@router.delete(
    "/practice-plans/{plan_id}/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Remove an item from a plan",
)
async def delete_practice_plan_item(
    user: CurrentUser,
    session: DBSession,
    plan_id: Annotated[str, Path(min_length=1, max_length=36)],
    item_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> Response:
    plan = await _load_plan_or_404(session, plan_id)
    _ensure_can_mutate(user, plan)
    item = await _load_item_or_404(plan, item_id)

    await session.delete(item)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/practice-plans/{plan_id}/items/{item_id}/complete",
    response_model=PracticePlanItemPublic,
    summary="Record progress on a plan item",
)
async def complete_practice_plan_item(
    user: CurrentUser,
    session: DBSession,
    payload: PracticePlanItemComplete,
    plan_id: Annotated[str, Path(min_length=1, max_length=36)],
    item_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> dict[str, Any]:
    """Increment the item's completed count.

    When ``completed_count`` reaches ``target_count`` the item flips to
    ``completed`` and ``completed_at`` is stamped. Parents may call
    this endpoint for their own children's plans.
    """

    plan = await _load_plan_or_404(session, plan_id)
    _ensure_can_progress(user, plan)
    item = await _load_item_or_404(plan, item_id)

    if item.status == PracticePlanItemStatus.SKIPPED.value:
        raise ValidationError(
            "Cannot record progress on a skipped item.",
            code="ITEM_SKIPPED",
        )

    new_count = min(item.completed_count + payload.increment, item.target_count)
    item.completed_count = new_count
    if new_count >= item.target_count:
        item.status = PracticePlanItemStatus.COMPLETED.value
        item.completed_at = datetime.now(UTC)
    elif item.status == PracticePlanItemStatus.PENDING.value:
        item.status = PracticePlanItemStatus.IN_PROGRESS.value

    if payload.notes:
        # Append rather than overwrite so progress notes accumulate.
        existing = (item.notes or "").rstrip()
        suffix = payload.notes.strip()
        item.notes = f"{existing}\n{suffix}".strip() if existing else suffix

    await session.commit()
    await session.refresh(item, attribute_names=["exercise"])
    return serialize_item(item)


__all__ = ["router"]
