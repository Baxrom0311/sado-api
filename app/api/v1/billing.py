"""Billing endpoints — plans, subscriptions, orders, provider webhooks.

Public surface:

* ``GET  /billing/plans``               — public, list active plans.
* ``GET  /billing/subscription``        — auth, current user's status.
* ``POST /billing/orders``              — auth, create a payment intent.
* ``GET  /billing/orders``              — auth, list user's orders.
* ``POST /billing/webhooks/payme``      — Payme JSON-RPC entry point.
* ``POST /billing/webhooks/click``      — Click form-encoded webhook.

Free users with no subscription continue to function exactly as before
— no quota enforcement is added in this commit (grace period). The
plan and subscription endpoints simply expose the data the mobile UI
needs to display upgrade prompts.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request, status
from sqlalchemy import and_, or_, select

from app.api.deps import CurrentUser, DBSession
from app.core.exceptions import NotFoundError, ValidationError
from app.core.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Page,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)
from app.models.billing import (
    BillingPlan,
    PaymentOrder,
    PaymentOrderState,
    PaymentProvider,
)
from app.schemas.billing import (
    BillingPlanPublic,
    OrderCreateRequest,
    OrderCreateResponse,
    OrderPublic,
    PlanFeaturesPublic,
    SubscriptionPublic,
    UsageMetricPublic,
    UsagePublic,
)
from app.services.billing import (
    build_click_payment_url,
    build_payment_url,
    cancel_subscription_auto_renew,
    days_remaining,
    ensure_default_plans,
    get_active_subscription,
    get_plan_by_code,
    get_usage_snapshot,
    handle_click_request,
    handle_payme_request,
    is_subscription_active,
    quotas_enforced,
    resolve_plan_for_user,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------- Helpers


def _plan_to_public(plan: BillingPlan) -> BillingPlanPublic:
    return BillingPlanPublic(
        id=plan.id,
        code=plan.code,
        name_uz=plan.name_uz,
        name_ru=plan.name_ru,
        description_uz=plan.description_uz,
        description_ru=plan.description_ru,
        price_tiyin=plan.price_tiyin,
        price_uzs=plan.price_tiyin // 100,
        duration_days=plan.duration_days,
        features=plan.features,
        is_active=plan.is_active,
        sort_order=plan.sort_order,
    )


def _order_to_public(order: PaymentOrder) -> OrderPublic:
    return OrderPublic(
        id=order.id,
        user_id=order.user_id,
        plan_code=order.plan_code,
        amount_tiyin=order.amount_tiyin,
        amount_uzs=order.amount_tiyin // 100,
        state=order.state,
        provider=order.provider,
        paid_at=order.paid_at,
        cancelled_at=order.cancelled_at,
        created_at=order.created_at,
        updated_at=order.updated_at,
    )


# ---------------------------------------------------------------- Plans


@router.get(
    "/billing/plans",
    response_model=list[BillingPlanPublic],
    summary="List active billing plans (public)",
)
async def list_plans(session: DBSession) -> list[BillingPlanPublic]:
    """Return the active plan catalogue.

    Public on purpose — the mobile login screen renders this before
    the user logs in. Defaults are seeded lazily on first request.
    """

    plans = await ensure_default_plans(session)
    return [_plan_to_public(p) for p in plans]


# --------------------------------------------------------- Subscription


@router.get(
    "/billing/subscription",
    response_model=SubscriptionPublic,
    summary="Get the current user's subscription status",
)
async def get_my_subscription(
    user: CurrentUser, session: DBSession
) -> SubscriptionPublic:
    sub = await get_active_subscription(session, user.id)
    plan_code, features = await resolve_plan_for_user(session, user.id)
    if sub is None:
        # Free tier — synthesize a response so the mobile client has
        # the same shape regardless of whether a row exists.
        return SubscriptionPublic(
            user_id=user.id,
            plan_code=plan_code,
            status="active",
            is_active=True,
            days_remaining=None,
            features=features,
        )
    return SubscriptionPublic(
        id=sub.id,
        user_id=sub.user_id,
        plan_code=sub.plan_code,
        status=sub.status,
        starts_at=sub.starts_at,
        expires_at=sub.expires_at,
        auto_renew=sub.auto_renew,
        cancelled_at=sub.cancelled_at,
        is_active=is_subscription_active(sub),
        days_remaining=days_remaining(sub),
        features=features,
    )


@router.get(
    "/billing/me/features",
    response_model=PlanFeaturesPublic,
    summary="Resolved feature flags / quotas for the caller's plan",
)
async def get_my_features(
    user: CurrentUser, session: DBSession
) -> PlanFeaturesPublic:
    """Return the feature dict the mobile client uses to gate UI.

    Resolves to the ``free`` plan for users without an active
    subscription so the response shape is stable regardless of payment
    status. ``quotas_enforced`` lets the client know whether the
    backend will currently reject quota-exceeding requests.
    """

    sub = await get_active_subscription(session, user.id)
    plan_code, features = await resolve_plan_for_user(session, user.id)
    return PlanFeaturesPublic(
        plan_code=plan_code,
        is_active=is_subscription_active(sub) or plan_code == "free",
        features=features,
        quotas_enforced=quotas_enforced(),
    )


@router.post(
    "/billing/subscription/cancel",
    response_model=SubscriptionPublic,
    summary="Disable auto-renew on the caller's subscription",
)
async def cancel_my_subscription(
    user: CurrentUser, session: DBSession
) -> SubscriptionPublic:
    """Turn off auto-renew while keeping access through ``expires_at``.

    Free users (no active row) get a 200 with their synthesized free
    response — this is intentionally a no-op so the mobile client can
    call it unconditionally without checking the plan first.
    """

    sub = await cancel_subscription_auto_renew(session, user_id=user.id)
    if sub is not None:
        await session.commit()
        await session.refresh(sub)
    plan_code, features = await resolve_plan_for_user(session, user.id)
    if sub is None:
        return SubscriptionPublic(
            user_id=user.id,
            plan_code=plan_code,
            status="active",
            is_active=True,
            days_remaining=None,
            features=features,
        )
    return SubscriptionPublic(
        id=sub.id,
        user_id=sub.user_id,
        plan_code=sub.plan_code,
        status=sub.status,
        starts_at=sub.starts_at,
        expires_at=sub.expires_at,
        auto_renew=sub.auto_renew,
        cancelled_at=sub.cancelled_at,
        is_active=is_subscription_active(sub),
        days_remaining=days_remaining(sub),
        features=features,
    )


@router.get(
    "/billing/usage",
    response_model=UsagePublic,
    summary="Per-metric quota usage for the caller's current period",
)
async def get_my_usage(
    user: CurrentUser, session: DBSession
) -> UsagePublic:
    """Return how much of each quota the caller has used.

    Drives the "X of Y remaining" badges on the mobile home screen.
    Always returns one row per known metric (``assessments_per_day``,
    ``ai_analyses_per_month``, ``children_total``) so the UI never has
    to handle a missing key. ``limit``/``remaining`` are ``null`` for
    unlimited tiers.
    """

    plan_code, _features = await resolve_plan_for_user(session, user.id)
    snapshot = await get_usage_snapshot(session, user_id=user.id)
    metrics = [UsageMetricPublic(**row) for row in snapshot]
    return UsagePublic(
        plan_code=plan_code,
        quotas_enforced=quotas_enforced(),
        metrics=metrics,
    )


# -------------------------------------------------------------- Orders


@router.post(
    "/billing/orders",
    response_model=OrderCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a payment order for a plan",
)
async def create_order(
    payload: OrderCreateRequest,
    user: CurrentUser,
    session: DBSession,
) -> OrderCreateResponse:
    plan = await get_plan_by_code(session, payload.plan_code)
    if plan is None or not plan.is_active:
        raise NotFoundError("Plan not found", code="PLAN_NOT_FOUND")
    if plan.price_tiyin <= 0:
        raise ValidationError(
            "Free plans do not require payment.",
            code="PLAN_NOT_PAYABLE",
        )

    order = PaymentOrder(
        user_id=user.id,
        plan_code=plan.code,
        amount_tiyin=plan.price_tiyin,
        state=PaymentOrderState.CREATED.value,
        provider=payload.provider,
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)

    if payload.provider == PaymentProvider.CLICK.value:
        url = build_click_payment_url(order.id, order.amount_tiyin)
    else:
        url = build_payment_url(order.id, order.amount_tiyin)

    return OrderCreateResponse(
        order=_order_to_public(order),
        payment_url=url,
    )


@router.get(
    "/billing/orders",
    response_model=Page[OrderPublic],
    summary="List the current user's payment orders",
)
async def list_orders(
    user: CurrentUser,
    session: DBSession,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> Page[OrderPublic]:
    page_size = clamp_limit(limit)
    stmt = select(PaymentOrder).where(PaymentOrder.user_id == user.id)

    if cursor:
        try:
            cursor_ts, cursor_id = decode_cursor(cursor)
        except ValueError as exc:
            raise ValidationError(str(exc), code="INVALID_CURSOR") from exc
        stmt = stmt.where(
            or_(
                PaymentOrder.created_at < cursor_ts,
                and_(
                    PaymentOrder.created_at == cursor_ts,
                    PaymentOrder.id < cursor_id,
                ),
            )
        )

    stmt = stmt.order_by(
        PaymentOrder.created_at.desc(), PaymentOrder.id.desc()
    ).limit(page_size + 1)

    rows = list((await session.execute(stmt)).scalars().all())
    has_more = len(rows) > page_size
    items = rows[:page_size]
    next_cursor: str | None = None
    if has_more and items:
        last = items[-1]
        last_ts: datetime = last.created_at
        next_cursor = encode_cursor(last_ts, last.id)

    return Page[OrderPublic](
        items=[_order_to_public(o) for o in items],
        next_cursor=next_cursor,
        has_more=has_more,
    )


# ------------------------------------------------------------ Webhooks


@router.post(
    "/billing/webhooks/payme",
    summary="Payme JSON-RPC webhook (Merchant API)",
    include_in_schema=False,
)
async def payme_webhook(
    request: Request,
    session: DBSession,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — Payme expects a 200 with an error
        body = {}
    if not isinstance(body, dict):
        body = {}
    return await handle_payme_request(session, body, authorization=authorization)


@router.post(
    "/billing/webhooks/click",
    summary="Click webhook (Prepare/Complete)",
    include_in_schema=False,
)
async def click_webhook(
    request: Request, session: DBSession
) -> dict[str, Any]:
    # Click sends application/x-www-form-urlencoded by default but
    # also tolerates JSON in some integrations. Try both.
    form_data: dict[str, str] = {}
    try:
        raw_form = await request.form()
        form_data = {k: str(v) for k, v in raw_form.items()}
    except Exception:  # noqa: BLE001
        form_data = {}
    if not form_data:
        try:
            body = await request.json()
            if isinstance(body, dict):
                form_data = {k: str(v) for k, v in body.items()}
        except Exception:  # noqa: BLE001
            form_data = {}
    return await handle_click_request(session, form_data)


__all__ = ["router"]
