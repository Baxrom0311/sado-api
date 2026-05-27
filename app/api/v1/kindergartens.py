"""Kindergartens API endpoints.

Authorization model:

* ``parent`` — read-only. Useful for picking a kindergarten when
  registering their child.
* ``teacher`` — read-only, scoped to their region. Future milestones
  may scope this to a specific kindergarten via ``user.kindergarten_id``.
* ``therapist`` — read-only across all kindergartens.
* ``admin`` — full CRUD.

Stats endpoint computes per-kindergarten risk distribution. Until the
``analysis_results`` table exists the counts default to zero, but the
contract is shaped so the dashboard can integrate immediately.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, DBSession, require_roles
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
from app.models.region import Region
from app.models.user import User, UserRole
from app.schemas.kindergarten import (
    KindergartenCreate,
    KindergartenPublic,
    KindergartenStats,
    KindergartenUpdate,
)

router = APIRouter()


async def _load_kg_or_404(session: DBSession, kg_id: str) -> Kindergarten:
    kg = await session.get(Kindergarten, kg_id)
    if kg is None:
        raise NotFoundError(
            "Kindergarten not found", code="KINDERGARTEN_NOT_FOUND"
        )
    return kg


async def _validate_region(session: DBSession, region_id: str | None) -> None:
    if region_id is None:
        return
    region = await session.get(Region, region_id)
    if region is None:
        raise ValidationError(
            "region_id does not reference an existing region.",
            code="REGION_NOT_FOUND",
        )


def _can_read_kg(user: User, kg: Kindergarten) -> bool:
    if user.role in {UserRole.ADMIN.value, UserRole.THERAPIST.value, UserRole.PARENT.value}:
        return True
    if user.role == UserRole.TEACHER.value:
        if user.region_id is None:
            return False
        return kg.region_id == user.region_id
    return False


@router.get(
    "/kindergartens",
    response_model=Page[KindergartenPublic],
    summary="List kindergartens visible to the caller",
)
async def list_kindergartens(
    user: CurrentUser,
    session: DBSession,
    cursor: Annotated[str | None, Query(description="Opaque pagination cursor")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    region_id: Annotated[str | None, Query()] = None,
    search: Annotated[
        str | None,
        Query(min_length=1, max_length=255, description="Case-insensitive name match"),
    ] = None,
) -> Page[KindergartenPublic]:
    page_size = clamp_limit(limit)

    stmt = select(Kindergarten)
    if user.role == UserRole.TEACHER.value:
        if user.region_id is None:
            return Page[KindergartenPublic](items=[], next_cursor=None, has_more=False)
        stmt = stmt.where(Kindergarten.region_id == user.region_id)
    if region_id:
        stmt = stmt.where(Kindergarten.region_id == region_id)
    if search:
        stmt = stmt.where(Kindergarten.name.ilike(f"%{search.lower()}%"))

    if cursor:
        try:
            cursor_ts, cursor_id = decode_cursor(cursor)
        except ValueError as exc:
            raise ValidationError(str(exc), code="INVALID_CURSOR") from exc
        stmt = stmt.where(
            or_(
                Kindergarten.created_at < cursor_ts,
                and_(
                    Kindergarten.created_at == cursor_ts,
                    Kindergarten.id < cursor_id,
                ),
            )
        )

    stmt = stmt.order_by(
        Kindergarten.created_at.desc(), Kindergarten.id.desc()
    ).limit(page_size + 1)
    rows = list((await session.execute(stmt)).scalars().all())

    has_more = len(rows) > page_size
    page_items = rows[:page_size]
    next_cursor: str | None = None
    if has_more and page_items:
        last = page_items[-1]
        last_ts: datetime = last.created_at
        next_cursor = encode_cursor(last_ts, last.id)

    return Page[KindergartenPublic](
        items=[KindergartenPublic.model_validate(k) for k in page_items],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get(
    "/kindergartens/{kindergarten_id}",
    response_model=KindergartenPublic,
    summary="Read a kindergarten",
)
async def get_kindergarten(
    user: CurrentUser,
    session: DBSession,
    kindergarten_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> KindergartenPublic:
    kg = await _load_kg_or_404(session, kindergarten_id)
    if not _can_read_kg(user, kg):
        raise ForbiddenError(
            "You do not have access to this kindergarten.",
            code="KINDERGARTEN_FORBIDDEN",
        )
    return KindergartenPublic.model_validate(kg)


@router.post(
    "/kindergartens",
    response_model=KindergartenPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Create a kindergarten (admin only)",
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def create_kindergarten(
    payload: KindergartenCreate,
    session: DBSession,
) -> KindergartenPublic:
    await _validate_region(session, payload.region_id)

    kg = Kindergarten(
        name=payload.name,
        address=payload.address,
        phone=payload.phone,
        teacher_count=payload.teacher_count,
        child_count=payload.child_count,
        region_id=payload.region_id,
    )
    session.add(kg)
    try:
        await session.commit()
    except IntegrityError as exc:  # pragma: no cover
        await session.rollback()
        raise ConflictError(
            "Could not create kindergarten.", code="KINDERGARTEN_CONFLICT"
        ) from exc
    await session.refresh(kg)
    return KindergartenPublic.model_validate(kg)


@router.put(
    "/kindergartens/{kindergarten_id}",
    response_model=KindergartenPublic,
    summary="Update a kindergarten (admin only)",
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def update_kindergarten(
    payload: KindergartenUpdate,
    session: DBSession,
    kindergarten_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> KindergartenPublic:
    kg = await _load_kg_or_404(session, kindergarten_id)
    data = payload.model_dump(exclude_unset=True)

    if "region_id" in data:
        await _validate_region(session, data["region_id"])
        kg.region_id = data["region_id"]
    if data.get("name") is not None:
        kg.name = data["name"]
    if "address" in data:
        kg.address = data["address"]
    if "phone" in data:
        kg.phone = data["phone"]
    if data.get("teacher_count") is not None:
        kg.teacher_count = data["teacher_count"]
    if data.get("child_count") is not None:
        kg.child_count = data["child_count"]

    await session.commit()
    await session.refresh(kg)
    return KindergartenPublic.model_validate(kg)


@router.delete(
    "/kindergartens/{kindergarten_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a kindergarten (admin only)",
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def delete_kindergarten(
    session: DBSession,
    kindergarten_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> Response:
    kg = await _load_kg_or_404(session, kindergarten_id)
    await session.delete(kg)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/kindergartens/{kindergarten_id}/stats",
    response_model=KindergartenStats,
    summary="Risk distribution and counts for a kindergarten",
)
async def kindergarten_stats(
    user: CurrentUser,
    session: DBSession,
    kindergarten_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> KindergartenStats:
    kg = await _load_kg_or_404(session, kindergarten_id)
    if not _can_read_kg(user, kg):
        raise ForbiddenError(
            "You do not have access to this kindergarten.",
            code="KINDERGARTEN_FORBIDDEN",
        )

    total = (
        await session.execute(
            select(func.count(Child.id)).where(Child.kindergarten_id == kg.id)
        )
    ).scalar_one()

    # Risk counts will be wired up once analysis_results lands. Until
    # then we report zeros so the contract is stable for the dashboard.
    return KindergartenStats(
        kindergarten_id=kg.id,
        name=kg.name,
        total_children=int(total or 0),
        risk_green=0,
        risk_yellow=0,
        risk_red=0,
        assessed_children=0,
    )
