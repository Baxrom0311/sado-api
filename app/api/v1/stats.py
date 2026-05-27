"""Aggregated statistics endpoints.

The dashboard and the Regional Statistics page consume these. Heavy
queries are tuned for SQLite (used in tests) and Postgres (production)
by avoiding window functions and relying on simple ``GROUP BY``.

Authorization:

* ``GET /stats/system`` — admin only.
* ``GET /stats/regional`` — admin only.
* ``GET /stats/kindergartens/{id}`` — admin or a teacher whose region
  matches the kindergarten's region.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DBSession, require_roles
from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.assessment import (
    Assessment,
    AssessmentStatus,
    RiskLevel,
)
from app.models.child import Child
from app.models.kindergarten import Kindergarten
from app.models.region import Region
from app.models.user import User, UserRole
from app.schemas.stats import (
    DailyAssessmentPoint,
    KindergartenStatRow,
    RegionalStatsResponse,
    RegionStat,
    RiskDistribution,
    RolePopulation,
    SystemStatsResponse,
)

router = APIRouter()

WEEK_DAYS = 7


# --------------------------------------------------------------- Helpers


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _start_of_day(dt: datetime) -> datetime:
    return datetime.combine(dt.date(), time.min, tzinfo=UTC)


def _coerce_risk_bucket(value: str | None) -> str:
    if value == RiskLevel.GREEN.value:
        return "green"
    if value == RiskLevel.YELLOW.value:
        return "yellow"
    if value == RiskLevel.RED.value:
        return "red"
    return "unknown"


async def _risk_distribution(session: DBSession) -> RiskDistribution:
    """Count completed assessments grouped by ``overall_risk``."""

    stmt = (
        select(Assessment.overall_risk, func.count(Assessment.id))
        .where(Assessment.status == AssessmentStatus.COMPLETED.value)
        .group_by(Assessment.overall_risk)
    )
    counts = {"green": 0, "yellow": 0, "red": 0, "unknown": 0}
    for risk, count in (await session.execute(stmt)).all():
        counts[_coerce_risk_bucket(risk)] += int(count or 0)
    return RiskDistribution(**counts)


async def _user_role_population(session: DBSession) -> RolePopulation:
    stmt = (
        select(User.role, func.count(User.id))
        .where(User.is_active.is_(True))
        .group_by(User.role)
    )
    pop = {"parent": 0, "teacher": 0, "therapist": 0, "admin": 0}
    for role, count in (await session.execute(stmt)).all():
        if role in pop:
            pop[role] = int(count or 0)
    return RolePopulation(**pop)


async def _daily_assessments(
    session: DBSession, *, days: int = WEEK_DAYS
) -> list[DailyAssessmentPoint]:
    """Return assessment counts per day for the last ``days`` UTC days."""

    today = _start_of_day(_utc_now())
    start = today - timedelta(days=days - 1)

    stmt = select(Assessment.created_at).where(Assessment.created_at >= start)
    rows = (await session.execute(stmt)).scalars().all()

    bucket: dict[str, int] = defaultdict(int)
    for created_at in rows:
        if created_at is None:
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        bucket[created_at.date().isoformat()] += 1

    points: list[DailyAssessmentPoint] = []
    for offset in range(days):
        day = (start + timedelta(days=offset)).date()
        points.append(
            DailyAssessmentPoint(date=day, count=bucket.get(day.isoformat(), 0))
        )
    return points


async def _scalar_count(session: DBSession, model) -> int:
    stmt = select(func.count()).select_from(model)
    return int((await session.execute(stmt)).scalar_one() or 0)


# --------------------------------------------------------------- System


@router.get(
    "/stats/system",
    response_model=SystemStatsResponse,
    summary="Top-level platform metrics for the admin dashboard",
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def system_stats(session: DBSession) -> SystemStatsResponse:
    total_children = await _scalar_count(session, Child)
    total_users = await _scalar_count(session, User)
    total_kindergartens = await _scalar_count(session, Kindergarten)
    total_regions = await _scalar_count(session, Region)
    total_assessments = await _scalar_count(session, Assessment)

    completed_stmt = select(func.count(Assessment.id)).where(
        Assessment.status == AssessmentStatus.COMPLETED.value
    )
    completed_assessments = int(
        (await session.execute(completed_stmt)).scalar_one() or 0
    )

    today_start = _start_of_day(_utc_now())
    today_stmt = select(func.count(Assessment.id)).where(
        Assessment.created_at >= today_start
    )
    assessments_today = int(
        (await session.execute(today_stmt)).scalar_one() or 0
    )

    therapists_stmt = select(func.count(User.id)).where(
        User.role == UserRole.THERAPIST.value, User.is_active.is_(True)
    )
    active_therapists = int(
        (await session.execute(therapists_stmt)).scalar_one() or 0
    )

    distribution = await _risk_distribution(session)
    red_pct = 0.0
    if distribution.total:
        red_pct = round((distribution.red / distribution.total) * 100, 2)

    user_roles = await _user_role_population(session)
    weekly = await _daily_assessments(session)

    return SystemStatsResponse(
        total_children=total_children,
        total_users=total_users,
        total_kindergartens=total_kindergartens,
        total_regions=total_regions,
        total_assessments=total_assessments,
        completed_assessments=completed_assessments,
        assessments_today=assessments_today,
        active_therapists=active_therapists,
        red_risk_percentage=red_pct,
        risk_distribution=distribution,
        user_roles=user_roles,
        weekly_assessments=weekly,
    )


# --------------------------------------------------------------- Regional


def _empty_dist() -> dict[str, int]:
    return {"green": 0, "yellow": 0, "red": 0, "unknown": 0}


async def _region_breakdown(session: DBSession) -> list[RegionStat]:
    """Children + assessments per region (including 'Unassigned')."""

    # Region map: id -> name. We include a synthetic "Unassigned" row.
    region_rows = (await session.execute(select(Region))).scalars().all()
    name_by_id: dict[str, str] = {r.id: r.name for r in region_rows}

    children_count: dict[str | None, int] = defaultdict(int)
    children_stmt = (
        select(Kindergarten.region_id, func.count(Child.id))
        .select_from(Child)
        .outerjoin(Kindergarten, Kindergarten.id == Child.kindergarten_id)
        .group_by(Kindergarten.region_id)
    )
    for region_id, count in (await session.execute(children_stmt)).all():
        children_count[region_id] = int(count or 0)

    # Assessments + risk per region (joined through child.kindergarten).
    assessments_stmt = (
        select(
            Kindergarten.region_id,
            Assessment.overall_risk,
            Assessment.status,
            func.count(Assessment.id),
        )
        .select_from(Assessment)
        .join(Child, Child.id == Assessment.child_id)
        .outerjoin(Kindergarten, Kindergarten.id == Child.kindergarten_id)
        .group_by(
            Kindergarten.region_id,
            Assessment.overall_risk,
            Assessment.status,
        )
    )
    assess_count: dict[str | None, int] = defaultdict(int)
    risk_count: dict[str | None, dict[str, int]] = defaultdict(_empty_dist)
    for region_id, risk, status_, count in (
        await session.execute(assessments_stmt)
    ).all():
        n = int(count or 0)
        assess_count[region_id] += n
        if status_ == AssessmentStatus.COMPLETED.value:
            risk_count[region_id][_coerce_risk_bucket(risk)] += n

    region_ids = (
        set(name_by_id.keys())
        | set(children_count.keys())
        | set(assess_count.keys())
    )
    rows: list[RegionStat] = []
    for rid in region_ids:
        if rid is None:
            label = "Unassigned"
        else:
            label = name_by_id.get(rid, "Unknown")
        rows.append(
            RegionStat(
                region_id=rid,
                region_name=label,
                children=children_count.get(rid, 0),
                assessments=assess_count.get(rid, 0),
                risk_distribution=RiskDistribution(**risk_count.get(rid, _empty_dist())),
            )
        )
    rows.sort(key=lambda r: (-r.children, r.region_name))
    return rows


async def _kindergarten_leaderboard(
    session: DBSession, *, limit: int
) -> list[KindergartenStatRow]:
    """Top kindergartens by completed-assessment count."""

    stmt = (
        select(
            Kindergarten.id,
            Kindergarten.name,
            Kindergarten.region_id,
            Region.name,
            func.count(Child.id.distinct()),
        )
        .select_from(Kindergarten)
        .outerjoin(Region, Region.id == Kindergarten.region_id)
        .outerjoin(Child, Child.kindergarten_id == Kindergarten.id)
        .group_by(
            Kindergarten.id,
            Kindergarten.name,
            Kindergarten.region_id,
            Region.name,
        )
    )
    base_rows = (await session.execute(stmt)).all()

    # Risk counts per kindergarten.
    risk_stmt = (
        select(
            Child.kindergarten_id,
            Assessment.overall_risk,
            func.count(Assessment.id),
        )
        .select_from(Assessment)
        .join(Child, Child.id == Assessment.child_id)
        .where(Assessment.status == AssessmentStatus.COMPLETED.value)
        .group_by(Child.kindergarten_id, Assessment.overall_risk)
    )
    risk_map: dict[str | None, dict[str, int]] = defaultdict(_empty_dist)
    for kg_id, risk, count in (await session.execute(risk_stmt)).all():
        risk_map[kg_id][_coerce_risk_bucket(risk)] += int(count or 0)

    rows: list[KindergartenStatRow] = []
    for kg_id, kg_name, region_id, region_name, child_count in base_rows:
        bucket = risk_map.get(kg_id, _empty_dist())
        rows.append(
            KindergartenStatRow(
                kindergarten_id=kg_id,
                name=kg_name,
                region_id=region_id,
                region_name=region_name,
                child_count=int(child_count or 0),
                assessments=sum(bucket.values()),
                red_count=bucket["red"],
                yellow_count=bucket["yellow"],
                green_count=bucket["green"],
            )
        )

    rows.sort(key=lambda r: (-r.assessments, -r.child_count, r.name))
    return rows[:limit]


@router.get(
    "/stats/regional",
    response_model=RegionalStatsResponse,
    summary="Region + kindergarten breakdown for analytics page",
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def regional_stats(
    session: DBSession,
    days: Annotated[
        int,
        Query(ge=1, le=90, description="How many days of trend data to return"),
    ] = WEEK_DAYS,
    leaderboard_limit: Annotated[
        int,
        Query(ge=1, le=50, description="Top-N kindergartens by assessment volume"),
    ] = 10,
) -> RegionalStatsResponse:
    regions = await _region_breakdown(session)
    kindergartens = await _kindergarten_leaderboard(
        session, limit=leaderboard_limit
    )
    daily_trend = await _daily_assessments(session, days=days)
    return RegionalStatsResponse(
        regions=regions,
        kindergartens=kindergartens,
        daily_trend=daily_trend,
    )


# --------------------------------------------------- Per-kindergarten


@router.get(
    "/stats/kindergartens/{kindergarten_id}",
    response_model=KindergartenStatRow,
    summary="Risk + assessment counts for one kindergarten",
)
async def kindergarten_stats(
    user: CurrentUser,
    session: DBSession,
    kindergarten_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> KindergartenStatRow:
    kg = await session.get(Kindergarten, kindergarten_id)
    if kg is None:
        raise NotFoundError(
            "Kindergarten not found", code="KINDERGARTEN_NOT_FOUND"
        )

    # RBAC: admin/therapist see anything; teachers only their region.
    if user.role == UserRole.PARENT.value:
        raise ForbiddenError(
            "Parents may not read kindergarten statistics.",
            code="STATS_FORBIDDEN",
        )
    if user.role == UserRole.TEACHER.value and (
        user.region_id is None or user.region_id != kg.region_id
    ):
        raise ForbiddenError(
            "You may only read stats for kindergartens in your region.",
            code="STATS_FORBIDDEN",
        )

    region_name: str | None = None
    if kg.region_id is not None:
        region = await session.get(Region, kg.region_id)
        region_name = region.name if region is not None else None

    child_count_stmt = select(func.count(Child.id)).where(
        Child.kindergarten_id == kg.id
    )
    child_count = int((await session.execute(child_count_stmt)).scalar_one() or 0)

    risk_stmt = (
        select(Assessment.overall_risk, func.count(Assessment.id))
        .select_from(Assessment)
        .join(Child, Child.id == Assessment.child_id)
        .where(
            Child.kindergarten_id == kg.id,
            Assessment.status == AssessmentStatus.COMPLETED.value,
        )
        .group_by(Assessment.overall_risk)
    )
    bucket = _empty_dist()
    for risk, count in (await session.execute(risk_stmt)).all():
        bucket[_coerce_risk_bucket(risk)] += int(count or 0)

    return KindergartenStatRow(
        kindergarten_id=kg.id,
        name=kg.name,
        region_id=kg.region_id,
        region_name=region_name,
        child_count=child_count,
        assessments=sum(bucket.values()),
        red_count=bucket["red"],
        yellow_count=bucket["yellow"],
        green_count=bucket["green"],
    )


__all__ = ["router"]
