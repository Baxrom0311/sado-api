"""Gamification endpoints — XP, levels, streaks, badges, leaderboard.

Routes are mounted under ``/api/v1`` so the canonical paths are::

    GET    /children/{child_id}/gamification           # status
    GET    /children/{child_id}/gamification/badges    # earned badges
    POST   /children/{child_id}/gamification/award     # admin XP award
    GET    /children/{child_id}/gamification/leaderboard
    GET    /badges                                     # catalogue
    POST   /badges                                     # admin create
    GET    /badges/{badge_id}
    PUT    /badges/{badge_id}
    DELETE /badges/{badge_id}

Authorisation:

* parents see their own children's stats and badges,
* teachers see children whose kindergarten lives in their region,
* therapists + admins see everyone,
* only admins can mutate the badge catalogue or push XP awards.
"""

from __future__ import annotations

from typing import Annotated

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
from app.models.child import Child
from app.models.gamification import Badge, Gamification
from app.models.user import User, UserRole
from app.schemas.gamification import (
    BadgeCreate,
    BadgeEarningPublic,
    BadgePublic,
    BadgeUpdate,
    GamificationPublic,
    LeaderboardEntry,
    LeaderboardResponse,
    XPAwardRequest,
    XPAwardResponse,
)
from app.services import gamification as gam_service

router = APIRouter()


# ----------------------------------------------------------- Authorization


def _can_manage_badges(user: User) -> bool:
    return user.role == UserRole.ADMIN.value


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


def _can_view_child(user: User, child: Child) -> bool:
    """Is ``user`` allowed to read gamification data for ``child``?"""

    if user.role in {UserRole.ADMIN.value, UserRole.THERAPIST.value}:
        return True
    if user.role == UserRole.PARENT.value:
        return child.parent_id == user.id
    if user.role == UserRole.TEACHER.value:
        if user.region_id is None:
            return False
        kg = child.kindergarten
        return kg is not None and kg.region_id == user.region_id
    return False


# ---------------------------------------------------------------- Helpers


async def _to_public(
    session: DBSession, record: Gamification
) -> GamificationPublic:
    """Build the ``GamificationPublic`` payload from a stored record."""

    level, into, span = gam_service.xp_progress_for_level(record.total_xp)
    badges_count = await gam_service.count_badges(session, record.child_id)
    return GamificationPublic(
        child_id=record.child_id,
        total_xp=record.total_xp,
        level=level,
        xp_into_level=into,
        xp_for_next_level=span,
        streak_days=record.streak_days,
        longest_streak=record.longest_streak,
        last_activity_date=record.last_activity_date,
        total_exercises_completed=record.total_exercises_completed,
        total_assessments_completed=record.total_assessments_completed,
        badges_earned=badges_count,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


# --------------------------------------------------------- Status & badges


@router.get(
    "/children/{child_id}/gamification",
    response_model=GamificationPublic,
    summary="Get gamification status for a child",
)
async def get_child_gamification(
    user: CurrentUser,
    session: DBSession,
    child_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> GamificationPublic:
    child = await _load_child_or_404(session, child_id)
    if not _can_view_child(user, child):
        raise ForbiddenError(
            "You may not view this child's gamification status.",
            code="GAMIFICATION_FORBIDDEN",
        )
    record = await gam_service.get_or_create(session, child.id)
    await session.commit()
    await session.refresh(record)
    return await _to_public(session, record)


@router.get(
    "/children/{child_id}/gamification/badges",
    response_model=list[BadgeEarningPublic],
    summary="List the badges a child has earned",
)
async def list_child_badges(
    user: CurrentUser,
    session: DBSession,
    child_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> list[BadgeEarningPublic]:
    child = await _load_child_or_404(session, child_id)
    if not _can_view_child(user, child):
        raise ForbiddenError(
            "You may not view this child's badges.",
            code="GAMIFICATION_FORBIDDEN",
        )
    earnings = await gam_service.list_earned_badges(session, child.id)
    return [BadgeEarningPublic.model_validate(e) for e in earnings]


@router.post(
    "/children/{child_id}/gamification/award",
    response_model=XPAwardResponse,
    status_code=status.HTTP_200_OK,
    summary="Manually award XP to a child (admin only)",
)
async def award_xp_to_child(
    payload: XPAwardRequest,
    user: CurrentUser,
    session: DBSession,
    child_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> XPAwardResponse:
    """Push XP onto a child's gamification record.

    Used by the admin dashboard for manual rewards (e.g. "good behaviour
    in class today"). The endpoint also fires badge unlocks the same
    way regular activity hooks do.
    """

    if user.role != UserRole.ADMIN.value:
        raise ForbiddenError(
            "Only admins may award XP manually.",
            code="GAMIFICATION_FORBIDDEN",
        )

    child = await _load_child_or_404(session, child_id)
    outcome = await gam_service.award_xp(
        session,
        child.id,
        payload.amount,
        activity_date=gam_service._today_utc(),
        reason=f"admin:{payload.reason}",
    )
    await session.commit()
    await session.refresh(outcome.gamification)

    public = await _to_public(session, outcome.gamification)
    return XPAwardResponse(
        gamification=public,
        xp_added=outcome.xp_added,
        leveled_up=outcome.leveled_up,
        previous_level=outcome.previous_level,
        new_level=outcome.new_level,
        newly_earned_badges=[
            BadgePublic.model_validate(b) for b in outcome.newly_earned_badges
        ],
    )


# -------------------------------------------------------------- Leaderboard


@router.get(
    "/children/{child_id}/gamification/leaderboard",
    response_model=LeaderboardResponse,
    summary="Top-N XP leaderboard relative to a child's peer group",
)
async def child_leaderboard(
    user: CurrentUser,
    session: DBSession,
    child_id: Annotated[str, Path(min_length=1, max_length=36)],
    scope: Annotated[
        str,
        Query(
            description=(
                "Leaderboard scope. 'family' (siblings under same parent), "
                "'kindergarten' (same kindergarten), or 'global' (all children)."
            ),
            pattern="^(family|kindergarten|global)$",
        ),
    ] = "family",
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> LeaderboardResponse:
    child = await _load_child_or_404(session, child_id)
    if not _can_view_child(user, child):
        raise ForbiddenError(
            "You may not view leaderboards for this child.",
            code="GAMIFICATION_FORBIDDEN",
        )

    if scope == "global" and user.role not in {
        UserRole.ADMIN.value,
        UserRole.THERAPIST.value,
    }:
        raise ForbiddenError(
            "Only therapists and admins may view the global leaderboard.",
            code="GAMIFICATION_FORBIDDEN",
        )

    stmt = (
        select(Gamification, Child)
        .join(Child, Child.id == Gamification.child_id)
    )
    if scope == "family":
        stmt = stmt.where(Child.parent_id == child.parent_id)
    elif scope == "kindergarten":
        if child.kindergarten_id is None:
            return LeaderboardResponse(scope=scope, entries=[])
        stmt = stmt.where(Child.kindergarten_id == child.kindergarten_id)
    # global: no extra filter

    stmt = stmt.order_by(
        Gamification.total_xp.desc(),
        Gamification.level.desc(),
        Child.name.asc(),
    ).limit(limit)

    rows = (await session.execute(stmt)).all()
    entries = [
        LeaderboardEntry(
            rank=idx + 1,
            child_id=record.child_id,
            child_name=ch.name,
            total_xp=record.total_xp,
            level=record.level,
            streak_days=record.streak_days,
        )
        for idx, (record, ch) in enumerate(rows)
    ]
    return LeaderboardResponse(scope=scope, entries=entries)


# ----------------------------------------------------------- Badge catalogue


@router.get(
    "/badges",
    response_model=Page[BadgePublic],
    summary="List badges in the catalogue",
)
async def list_badges(
    user: CurrentUser,
    session: DBSession,
    cursor: Annotated[str | None, Query(description="Pagination cursor")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    category: Annotated[str | None, Query()] = None,
    include_inactive: Annotated[bool, Query()] = False,
) -> Page[BadgePublic]:
    page_size = clamp_limit(limit)

    stmt = select(Badge)
    if category:
        stmt = stmt.where(Badge.category == category.lower().strip())
    if not include_inactive or not _can_manage_badges(user):
        stmt = stmt.where(Badge.is_active.is_(True))

    if cursor:
        try:
            cursor_ts, cursor_id = decode_cursor(cursor)
        except ValueError as exc:
            raise ValidationError(str(exc), code="INVALID_CURSOR") from exc
        stmt = stmt.where(
            or_(
                Badge.created_at < cursor_ts,
                and_(Badge.created_at == cursor_ts, Badge.id < cursor_id),
            )
        )

    stmt = stmt.order_by(
        Badge.sort_order.asc(),
        Badge.created_at.desc(),
        Badge.id.desc(),
    ).limit(page_size + 1)

    rows = list((await session.execute(stmt)).scalars().all())
    has_more = len(rows) > page_size
    page_items = rows[:page_size]
    next_cursor: str | None = None
    if has_more and page_items:
        last = page_items[-1]
        next_cursor = encode_cursor(last.created_at, last.id)

    return Page[BadgePublic](
        items=[BadgePublic.model_validate(b) for b in page_items],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.post(
    "/badges",
    response_model=BadgePublic,
    status_code=status.HTTP_201_CREATED,
    summary="Create a badge (admin only)",
)
async def create_badge(
    payload: BadgeCreate,
    user: CurrentUser,
    session: DBSession,
) -> BadgePublic:
    if not _can_manage_badges(user):
        raise ForbiddenError(
            "Only admins may create badges.", code="BADGE_FORBIDDEN"
        )
    badge = Badge(
        code=payload.code,
        title_uz=payload.title_uz,
        title_ru=payload.title_ru,
        description_uz=payload.description_uz,
        description_ru=payload.description_ru,
        icon=payload.icon,
        category=payload.category,
        requirement_type=payload.requirement_type,
        threshold=payload.threshold,
        sort_order=payload.sort_order,
        is_active=payload.is_active,
    )
    session.add(badge)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"Badge with code {payload.code!r} already exists.",
            code="BADGE_DUPLICATE",
        ) from exc
    await session.refresh(badge)
    return BadgePublic.model_validate(badge)


@router.get(
    "/badges/{badge_id}",
    response_model=BadgePublic,
    summary="Read a single badge",
)
async def get_badge(
    user: CurrentUser,
    session: DBSession,
    badge_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> BadgePublic:
    badge = await session.get(Badge, badge_id)
    if badge is None:
        raise NotFoundError("Badge not found", code="BADGE_NOT_FOUND")
    if not badge.is_active and not _can_manage_badges(user):
        raise NotFoundError("Badge not found", code="BADGE_NOT_FOUND")
    return BadgePublic.model_validate(badge)


@router.put(
    "/badges/{badge_id}",
    response_model=BadgePublic,
    summary="Update a badge (admin only)",
)
async def update_badge(
    payload: BadgeUpdate,
    user: CurrentUser,
    session: DBSession,
    badge_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> BadgePublic:
    if not _can_manage_badges(user):
        raise ForbiddenError(
            "Only admins may update badges.", code="BADGE_FORBIDDEN"
        )
    badge = await session.get(Badge, badge_id)
    if badge is None:
        raise NotFoundError("Badge not found", code="BADGE_NOT_FOUND")

    data = payload.model_dump(exclude_unset=True)
    for field in (
        "title_uz",
        "title_ru",
        "description_uz",
        "description_ru",
        "icon",
        "category",
        "requirement_type",
        "threshold",
        "sort_order",
        "is_active",
    ):
        if field in data:
            setattr(badge, field, data[field])
    await session.commit()
    await session.refresh(badge)
    return BadgePublic.model_validate(badge)


@router.delete(
    "/badges/{badge_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a badge (admin only)",
)
async def delete_badge(
    user: CurrentUser,
    session: DBSession,
    badge_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> Response:
    if not _can_manage_badges(user):
        raise ForbiddenError(
            "Only admins may delete badges.", code="BADGE_FORBIDDEN"
        )
    badge = await session.get(Badge, badge_id)
    if badge is None:
        raise NotFoundError("Badge not found", code="BADGE_NOT_FOUND")
    await session.delete(badge)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
