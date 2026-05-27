"""Regions API endpoints.

Authorization model:

* All authenticated users can list and read regions — they're a public
  reference dataset used for filters and dropdowns.
* Only ``admin`` may create, update, or delete regions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response, status
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, DBSession, require_roles
from app.core.exceptions import (
    ConflictError,
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
from app.models.region import Region
from app.models.user import UserRole
from app.schemas.region import (
    RegionCreate,
    RegionPublic,
    RegionUpdate,
)

router = APIRouter()


async def _load_region_or_404(session: DBSession, region_id: str) -> Region:
    region = await session.get(Region, region_id)
    if region is None:
        raise NotFoundError("Region not found", code="REGION_NOT_FOUND")
    return region


@router.get(
    "/regions",
    response_model=Page[RegionPublic],
    summary="List regions",
)
async def list_regions(
    user: CurrentUser,  # noqa: ARG001 — auth gate
    session: DBSession,
    cursor: Annotated[str | None, Query(description="Opaque pagination cursor")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    type: Annotated[str | None, Query(description="Filter by region type")] = None,
    parent_id: Annotated[str | None, Query(description="Filter by parent region id")] = None,
    search: Annotated[
        str | None,
        Query(min_length=1, max_length=120, description="Case-insensitive name match"),
    ] = None,
) -> Page[RegionPublic]:
    page_size = clamp_limit(limit)

    stmt = select(Region)
    if type:
        stmt = stmt.where(Region.type == type.lower())
    if parent_id:
        stmt = stmt.where(Region.parent_id == parent_id)
    if search:
        stmt = stmt.where(Region.name.ilike(f"%{search.lower()}%"))

    if cursor:
        try:
            cursor_ts, cursor_id = decode_cursor(cursor)
        except ValueError as exc:
            raise ValidationError(str(exc), code="INVALID_CURSOR") from exc
        stmt = stmt.where(
            or_(
                Region.created_at < cursor_ts,
                and_(Region.created_at == cursor_ts, Region.id < cursor_id),
            )
        )

    stmt = stmt.order_by(Region.created_at.desc(), Region.id.desc()).limit(page_size + 1)
    rows = list((await session.execute(stmt)).scalars().all())

    has_more = len(rows) > page_size
    page_items = rows[:page_size]
    next_cursor: str | None = None
    if has_more and page_items:
        last = page_items[-1]
        last_ts: datetime = last.created_at
        next_cursor = encode_cursor(last_ts, last.id)

    return Page[RegionPublic](
        items=[RegionPublic.model_validate(r) for r in page_items],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get(
    "/regions/{region_id}",
    response_model=RegionPublic,
    summary="Read a region",
)
async def get_region(
    user: CurrentUser,  # noqa: ARG001
    session: DBSession,
    region_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> RegionPublic:
    region = await _load_region_or_404(session, region_id)
    return RegionPublic.model_validate(region)


@router.post(
    "/regions",
    response_model=RegionPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Create a region (admin only)",
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def create_region(
    payload: RegionCreate,
    session: DBSession,
) -> RegionPublic:
    if payload.parent_id is not None:
        parent = await session.get(Region, payload.parent_id)
        if parent is None:
            raise ValidationError(
                "parent_id does not reference an existing region.",
                code="REGION_PARENT_NOT_FOUND",
            )

    region = Region(
        name=payload.name,
        code=payload.code,
        type=payload.type,
        parent_id=payload.parent_id,
    )
    session.add(region)
    try:
        await session.commit()
    except IntegrityError as exc:  # pragma: no cover - rare
        await session.rollback()
        raise ConflictError(
            "Could not create region.", code="REGION_CONFLICT"
        ) from exc
    await session.refresh(region)
    return RegionPublic.model_validate(region)


@router.put(
    "/regions/{region_id}",
    response_model=RegionPublic,
    summary="Update a region (admin only)",
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def update_region(
    payload: RegionUpdate,
    session: DBSession,
    region_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> RegionPublic:
    region = await _load_region_or_404(session, region_id)
    data = payload.model_dump(exclude_unset=True)

    if "parent_id" in data:
        new_parent = data["parent_id"]
        if new_parent is not None:
            if new_parent == region.id:
                raise ValidationError(
                    "A region cannot be its own parent.",
                    code="REGION_SELF_PARENT",
                )
            parent = await session.get(Region, new_parent)
            if parent is None:
                raise ValidationError(
                    "parent_id does not reference an existing region.",
                    code="REGION_PARENT_NOT_FOUND",
                )
        region.parent_id = new_parent
    if data.get("name") is not None:
        region.name = data["name"]
    if "code" in data:
        region.code = data["code"]
    if data.get("type") is not None:
        region.type = data["type"]

    await session.commit()
    await session.refresh(region)
    return RegionPublic.model_validate(region)


@router.delete(
    "/regions/{region_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a region (admin only)",
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def delete_region(
    session: DBSession,
    region_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> Response:
    region = await _load_region_or_404(session, region_id)
    await session.delete(region)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
