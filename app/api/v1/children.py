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
from typing import Annotated

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
from app.models.child import Child
from app.models.kindergarten import Kindergarten
from app.models.user import User, UserRole
from app.schemas.child import ChildCreate, ChildPublic, ChildUpdate

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

    child = Child(
        name=payload.name,
        birth_date=payload.birth_date,
        gender=payload.gender,
        language=payload.language,
        notes=payload.notes,
        parent_id=parent_id,
        kindergarten_id=payload.kindergarten_id,
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
