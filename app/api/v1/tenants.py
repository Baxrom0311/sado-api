"""Tenant management endpoints (multi-tenant architecture).

A "tenant" in SADO is a kindergarten — these endpoints expose the
super-admin tenant management surface and the per-tenant settings &
stats consumed by the tenant admin dashboard.

Authorisation:

* ``POST /tenants`` — super_admin only
* ``GET /tenants`` — super_admin only (list all)
* ``GET /tenants/{id}`` — super_admin or admin of that tenant
* ``GET /tenants/{id}/stats`` — super_admin, admin/teacher/therapist
  of that tenant
* ``PUT /tenants/{id}/settings`` — super_admin or admin of that tenant
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy import and_, func, or_, select

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
from app.models.assessment import Assessment, RiskLevel
from app.models.child import Child
from app.models.exercise import Exercise
from app.models.kindergarten import Kindergarten
from app.models.region import Region
from app.models.tenant import SubscriptionPlan, TenantSettings
from app.models.user import User, UserRole
from app.schemas.tenant import (
    TenantCreate,
    TenantPublic,
    TenantSettingsPublic,
    TenantSettingsUpdate,
    TenantStats,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# --------------------------------------------------------------- Helpers


async def _load_kg_or_404(session: DBSession, tenant_id: str) -> Kindergarten:
    kg = await session.get(Kindergarten, tenant_id)
    if kg is None:
        raise NotFoundError("Tenant not found", code="TENANT_NOT_FOUND")
    return kg


async def _load_settings(
    session: DBSession, tenant_id: str
) -> TenantSettings | None:
    """Return the :class:`TenantSettings` for a kindergarten, if any."""

    result = await session.execute(
        select(TenantSettings).where(TenantSettings.tenant_id == tenant_id)
    )
    return result.scalar_one_or_none()


def _can_view_tenant(user: User, tenant_id: str) -> bool:
    """Super-admin sees all; admins/teachers see only their tenant."""

    if user.role == UserRole.SUPER_ADMIN.value:
        return True
    if user.tenant_id is None:
        return False
    return user.tenant_id == tenant_id


def _can_admin_tenant(user: User, tenant_id: str) -> bool:
    """Super-admin or this tenant's admin may mutate tenant config."""

    if user.role == UserRole.SUPER_ADMIN.value:
        return True
    if user.role != UserRole.ADMIN.value:
        return False
    return user.tenant_id == tenant_id


def _to_tenant_public(
    kg: Kindergarten, settings: TenantSettings | None
) -> TenantPublic:
    return TenantPublic(
        id=kg.id,
        name=kg.name,
        address=kg.address,
        phone=kg.phone,
        region_id=kg.region_id,
        settings=(
            TenantSettingsPublic.model_validate(settings)
            if settings is not None
            else None
        ),
        created_at=kg.created_at,
        updated_at=kg.updated_at,
    )


# --------------------------------------------------------------- Endpoints


@router.post(
    "/tenants",
    response_model=TenantPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Create a tenant (super_admin only)",
    dependencies=[Depends(require_roles(UserRole.SUPER_ADMIN))],
)
async def create_tenant(
    payload: TenantCreate,
    session: DBSession,
) -> TenantPublic:
    """Create a tenant.

    Two modes:

    * ``kindergarten_id`` supplied — bind settings to an existing
      kindergarten (no kindergarten row created).
    * ``name`` supplied — create a new kindergarten and bind settings.

    Idempotent on the settings side: if a settings row already exists
    for the tenant, a 409 is returned.
    """

    if not payload.kindergarten_id and not payload.name:
        raise ValidationError(
            "Either kindergarten_id or name must be provided.",
            code="TENANT_REQUIRES_NAME_OR_ID",
        )

    if payload.kindergarten_id:
        kg = await session.get(Kindergarten, payload.kindergarten_id)
        if kg is None:
            raise NotFoundError(
                "Kindergarten not found", code="KINDERGARTEN_NOT_FOUND"
            )
        existing = await _load_settings(session, kg.id)
        if existing is not None:
            raise ConflictError(
                "Tenant settings already exist for this kindergarten.",
                code="TENANT_EXISTS",
            )
    else:
        if payload.region_id:
            region = await session.get(Region, payload.region_id)
            if region is None:
                raise ValidationError(
                    "region_id does not reference an existing region.",
                    code="REGION_NOT_FOUND",
                )
        kg = Kindergarten(
            name=payload.name,  # validated non-empty above
            address=payload.address,
            phone=payload.phone,
            region_id=payload.region_id,
        )
        session.add(kg)
        await session.flush()

    settings = TenantSettings(
        tenant_id=kg.id,
        max_children=payload.max_children,
        max_users=payload.max_users,
        ai_analysis_enabled=payload.ai_analysis_enabled,
        custom_exercises_enabled=payload.custom_exercises_enabled,
        subscription_plan=payload.subscription_plan,
    )
    session.add(settings)
    await session.commit()
    await session.refresh(kg)
    await session.refresh(settings)
    return _to_tenant_public(kg, settings)


@router.get(
    "/tenants",
    response_model=Page[TenantPublic],
    summary="List tenants (super_admin only)",
    dependencies=[Depends(require_roles(UserRole.SUPER_ADMIN))],
)
async def list_tenants(
    session: DBSession,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    only_with_settings: Annotated[
        bool,
        Query(
            description=(
                "If true, restrict results to kindergartens that already "
                "have a tenant_settings row."
            )
        ),
    ] = False,
) -> Page[TenantPublic]:
    page_size = clamp_limit(limit)

    stmt = select(Kindergarten)
    if only_with_settings:
        stmt = stmt.join(
            TenantSettings, TenantSettings.tenant_id == Kindergarten.id
        )

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

    # Batch-load settings to avoid N+1.
    ids = [k.id for k in page_items]
    settings_by_tenant: dict[str, TenantSettings] = {}
    if ids:
        result = await session.execute(
            select(TenantSettings).where(TenantSettings.tenant_id.in_(ids))
        )
        settings_by_tenant = {s.tenant_id: s for s in result.scalars().all()}

    next_cursor: str | None = None
    if has_more and page_items:
        last = page_items[-1]
        last_ts: datetime = last.created_at
        next_cursor = encode_cursor(last_ts, last.id)

    return Page[TenantPublic](
        items=[
            _to_tenant_public(kg, settings_by_tenant.get(kg.id))
            for kg in page_items
        ],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get(
    "/tenants/{tenant_id}",
    response_model=TenantPublic,
    summary="Read one tenant (super_admin or tenant admin)",
)
async def get_tenant(
    user: CurrentUser,
    session: DBSession,
    tenant_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> TenantPublic:
    if not _can_view_tenant(user, tenant_id):
        raise ForbiddenError(
            "You do not have access to this tenant.",
            code="TENANT_FORBIDDEN",
        )
    kg = await _load_kg_or_404(session, tenant_id)
    settings = await _load_settings(session, tenant_id)
    return _to_tenant_public(kg, settings)


@router.get(
    "/tenants/{tenant_id}/stats",
    response_model=TenantStats,
    summary="Aggregate counts for a tenant",
)
async def get_tenant_stats(
    user: CurrentUser,
    session: DBSession,
    tenant_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> TenantStats:
    if not _can_view_tenant(user, tenant_id):
        raise ForbiddenError(
            "You do not have access to this tenant.",
            code="TENANT_FORBIDDEN",
        )
    kg = await _load_kg_or_404(session, tenant_id)
    settings = await _load_settings(session, tenant_id)

    total_users = (
        await session.execute(
            select(func.count(User.id)).where(User.tenant_id == tenant_id)
        )
    ).scalar_one()
    total_children = (
        await session.execute(
            select(func.count(Child.id)).where(Child.tenant_id == tenant_id)
        )
    ).scalar_one()
    total_assessments = (
        await session.execute(
            select(func.count(Assessment.id)).where(
                Assessment.tenant_id == tenant_id
            )
        )
    ).scalar_one()
    total_exercises = (
        await session.execute(
            select(func.count(Exercise.id)).where(
                Exercise.tenant_id == tenant_id
            )
        )
    ).scalar_one()

    # Risk distribution across the tenant's completed assessments.
    risk_rows = (
        await session.execute(
            select(Assessment.overall_risk, func.count(Assessment.id))
            .where(Assessment.tenant_id == tenant_id)
            .group_by(Assessment.overall_risk)
        )
    ).all()
    risk_counts = {risk: count for risk, count in risk_rows}

    return TenantStats(
        tenant_id=kg.id,
        name=kg.name,
        total_users=int(total_users or 0),
        total_children=int(total_children or 0),
        total_assessments=int(total_assessments or 0),
        total_exercises=int(total_exercises or 0),
        risk_green=int(risk_counts.get(RiskLevel.GREEN.value, 0) or 0),
        risk_yellow=int(risk_counts.get(RiskLevel.YELLOW.value, 0) or 0),
        risk_red=int(risk_counts.get(RiskLevel.RED.value, 0) or 0),
        subscription_plan=(
            settings.subscription_plan if settings is not None else None
        ),
    )


@router.put(
    "/tenants/{tenant_id}/settings",
    response_model=TenantSettingsPublic,
    summary="Update tenant settings (super_admin or tenant admin)",
)
async def update_tenant_settings(
    user: CurrentUser,
    session: DBSession,
    payload: TenantSettingsUpdate,
    tenant_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> TenantSettingsPublic:
    if not _can_admin_tenant(user, tenant_id):
        raise ForbiddenError(
            "Only a super_admin or this tenant's admin may modify settings.",
            code="TENANT_FORBIDDEN",
        )
    await _load_kg_or_404(session, tenant_id)

    settings = await _load_settings(session, tenant_id)
    if settings is None:
        # Auto-provision settings on first update so the dashboard does
        # not need a separate creation step. Defaults match the model.
        settings = TenantSettings(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            subscription_plan=SubscriptionPlan.FREE.value,
        )
        session.add(settings)
        await session.flush()

    data = payload.model_dump(exclude_unset=True)
    if "max_children" in data and data["max_children"] is not None:
        settings.max_children = data["max_children"]
    if "max_users" in data and data["max_users"] is not None:
        settings.max_users = data["max_users"]
    if "ai_analysis_enabled" in data and data["ai_analysis_enabled"] is not None:
        settings.ai_analysis_enabled = data["ai_analysis_enabled"]
    if (
        "custom_exercises_enabled" in data
        and data["custom_exercises_enabled"] is not None
    ):
        settings.custom_exercises_enabled = data["custom_exercises_enabled"]
    if "subscription_plan" in data and data["subscription_plan"] is not None:
        settings.subscription_plan = data["subscription_plan"]

    await session.commit()
    await session.refresh(settings)
    return TenantSettingsPublic.model_validate(settings)


__all__ = ["router"]
