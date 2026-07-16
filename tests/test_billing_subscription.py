"""Tests for ``POST /billing/subscription/cancel`` + ``GET /billing/usage``.

These exercise the subscription self-service surface added in
0011_billing_usage. The API uses an in-process ASGI transport so no
real Payme/Click calls are made — the happy path runs the same JSON-RPC
sequence as ``test_billing.py`` to provision a paid subscription.
"""

from __future__ import annotations

import base64

import pytest

from app.config import get_settings

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------------- Helpers


async def _register_and_login(client, idx: int = 1) -> tuple[dict, dict[str, str]]:
    creds = {
        "email": f"sub-cancel-user{idx}@example.com",
        "password": "Sup3r-Secret!",
        "full_name": f"Sub Cancel User {idx}",
        "role": "parent",
    }
    register = await client.post("/api/v1/auth/register", json=creds)
    assert register.status_code == 201, register.text
    user = register.json()
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": creds["email"], "password": creds["password"]},
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    return user, headers


def _payme_basic_auth() -> str:
    settings = get_settings()
    raw = f"Paycom:{settings.payme_merchant_key}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


async def _provision_basic_subscription(client, headers, *, tx_id: str) -> dict:
    """Drive a Payme happy path so the user lands on the basic plan."""

    await client.get("/api/v1/billing/plans")
    create_order = await client.post(
        "/api/v1/billing/orders",
        json={"plan_code": "basic", "provider": "payme"},
        headers=headers,
    )
    assert create_order.status_code == 201, create_order.text
    order = create_order.json()["order"]

    auth = {"Authorization": _payme_basic_auth()}
    await client.post(
        "/api/v1/billing/webhooks/payme",
        json={
            "id": 1,
            "method": "CreateTransaction",
            "params": {
                "id": tx_id,
                "time": 1_700_000_000_000,
                "amount": order["amount_tiyin"],
                "account": {"order_id": order["id"]},
            },
        },
        headers=auth,
    )
    perform = await client.post(
        "/api/v1/billing/webhooks/payme",
        json={
            "id": 2,
            "method": "PerformTransaction",
            "params": {"id": tx_id},
        },
        headers=auth,
    )
    assert perform.json()["result"]["state"] == 2
    return order


# --------------------------------------------------- subscription/cancel


async def test_cancel_subscription_requires_auth(client) -> None:
    response = await client.post("/api/v1/billing/subscription/cancel")
    assert response.status_code == 401


async def test_cancel_subscription_no_op_for_free_user(client) -> None:
    """Free users have no row to cancel — endpoint is idempotent OK."""

    _, headers = await _register_and_login(client, idx=1)
    response = await client.post(
        "/api/v1/billing/subscription/cancel", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plan_code"] == "free"
    assert body["is_active"] is True
    # No active row → no auto_renew flag flipped.
    assert body["auto_renew"] is False


async def test_cancel_subscription_disables_auto_renew_keeps_access(
    client,
) -> None:
    _, headers = await _register_and_login(client, idx=2)
    await _provision_basic_subscription(client, headers, tx_id="payme-cancel-1")

    # Pre-cancel: subscription is active on basic.
    pre = await client.get("/api/v1/billing/subscription", headers=headers)
    assert pre.status_code == 200
    pre_body = pre.json()
    assert pre_body["plan_code"] == "basic"
    assert pre_body["is_active"] is True

    cancel = await client.post(
        "/api/v1/billing/subscription/cancel", headers=headers
    )
    assert cancel.status_code == 200, cancel.text
    cancel_body = cancel.json()
    assert cancel_body["plan_code"] == "basic"
    # Access continues until expires_at.
    assert cancel_body["is_active"] is True
    assert cancel_body["auto_renew"] is False
    assert cancel_body["cancelled_at"] is not None
    # Days remaining must still be positive — we don't shorten the term.
    assert cancel_body["days_remaining"] is not None
    assert cancel_body["days_remaining"] >= 1

    # GET /subscription mirrors the new state.
    after = await client.get(
        "/api/v1/billing/subscription", headers=headers
    )
    after_body = after.json()
    assert after_body["auto_renew"] is False
    assert after_body["is_active"] is True


async def test_cancel_subscription_is_idempotent(client) -> None:
    _, headers = await _register_and_login(client, idx=3)
    await _provision_basic_subscription(client, headers, tx_id="payme-cancel-2")

    first = await client.post(
        "/api/v1/billing/subscription/cancel", headers=headers
    )
    second = await client.post(
        "/api/v1/billing/subscription/cancel", headers=headers
    )
    assert first.status_code == 200
    assert second.status_code == 200
    # Both responses agree the user is still on basic and still active
    # through the paid period — calling cancel twice doesn't change
    # anything beyond the initial flip.
    assert first.json()["plan_code"] == "basic"
    assert second.json()["plan_code"] == "basic"
    assert second.json()["auto_renew"] is False
    # cancelled_at is set on the first call and stays put on the second.
    assert first.json()["cancelled_at"] == second.json()["cancelled_at"]


# ------------------------------------------------------------- usage


async def test_usage_requires_auth(client) -> None:
    response = await client.get("/api/v1/billing/usage")
    assert response.status_code == 401


async def test_usage_for_new_free_user_is_zero(client) -> None:
    _, headers = await _register_and_login(client, idx=10)
    response = await client.get("/api/v1/billing/usage", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plan_code"] == "free"
    # Grace period — quotas not enforced yet, but usage is still
    # reported so the UI can render "X of Y" badges accurately.
    assert body["quotas_enforced"] is False

    metrics = {m["metric"]: m for m in body["metrics"]}
    assert {"assessments_per_day", "ai_analyses_per_month", "children_total"} == set(
        metrics
    )

    daily = metrics["assessments_per_day"]
    assert daily["used"] == 0
    assert daily["limit"] == 3  # free tier was bumped from 1 → 3
    assert daily["remaining"] == 3
    assert daily["plan_code"] == "free"

    monthly = metrics["ai_analyses_per_month"]
    assert monthly["used"] == 0
    assert monthly["limit"] == 5
    assert monthly["remaining"] == 5

    total = metrics["children_total"]
    assert total["used"] == 0
    assert total["limit"] == 1  # free tier max_children
    assert total["remaining"] == 1


async def test_usage_reflects_unlimited_for_premium(client) -> None:
    """Once on a paid unlimited tier, ``limit``/``remaining`` are null."""

    _, headers = await _register_and_login(client, idx=11)
    # Basic plan has limited assessments_per_day=5 but unlimited
    # ai_analyses_per_month and max_children=3, so we use it as a
    # proxy for "limit is set" + "limit is unlimited" coverage.
    await _provision_basic_subscription(client, headers, tx_id="payme-usage-1")

    response = await client.get("/api/v1/billing/usage", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["plan_code"] == "basic"
    metrics = {m["metric"]: m for m in body["metrics"]}

    # Basic: assessments_per_day = 5
    assert metrics["assessments_per_day"]["limit"] == 5
    assert metrics["assessments_per_day"]["remaining"] == 5

    # Basic: ai_analyses_per_month = unlimited.
    assert metrics["ai_analyses_per_month"]["limit"] is None
    assert metrics["ai_analyses_per_month"]["remaining"] is None

    # Basic: max_children = 3.
    assert metrics["children_total"]["limit"] == 3
    assert metrics["children_total"]["remaining"] == 3


async def test_usage_increments_after_recording_calls(client) -> None:
    """Direct service call to verify usage records persist across requests."""

    from app.database import get_sessionmaker
    from app.services.billing.usage import increment_usage

    user, headers = await _register_and_login(client, idx=12)
    factory = get_sessionmaker()
    async with factory() as session:
        new_count = await increment_usage(
            session, user_id=user["id"], metric="assessments_per_day"
        )
        await session.commit()
    assert new_count == 1

    response = await client.get("/api/v1/billing/usage", headers=headers)
    body = response.json()
    metrics = {m["metric"]: m for m in body["metrics"]}
    assert metrics["assessments_per_day"]["used"] == 1
    # Free tier limit 3, used 1 → remaining 2.
    assert metrics["assessments_per_day"]["remaining"] == 2

    # Bumping again accumulates within the same period bucket.
    async with factory() as session:
        new_count = await increment_usage(
            session, user_id=user["id"], metric="assessments_per_day"
        )
        await session.commit()
    assert new_count == 2

    follow_up = await client.get(
        "/api/v1/billing/usage", headers=headers
    )
    follow_metrics = {m["metric"]: m for m in follow_up.json()["metrics"]}
    assert follow_metrics["assessments_per_day"]["used"] == 2
    assert follow_metrics["assessments_per_day"]["remaining"] == 1


# ------------------------------------------------- enforce_quota helper


async def test_enforce_quota_grace_period_never_raises(client) -> None:
    """With ``BILLING_ENFORCE_QUOTAS=False`` we *track* but never reject."""

    from app.database import get_sessionmaker
    from app.services.billing.usage import (
        PlanLimitExceededError,
        enforce_quota,
    )

    user, _headers = await _register_and_login(client, idx=20)
    factory = get_sessionmaker()

    # Free plan caps assessments at 3/day. Push 5 calls — none should
    # raise during the grace period.
    async with factory() as session:
        for _ in range(5):
            await enforce_quota(
                session,
                user_id=user["id"],
                metric="assessments_per_day",
            )
        await session.commit()

    # Sanity: ensure that when enforcement is *on* the same overflow
    # would have raised. We can't flip the global setting cheaply
    # mid-test (it's lru_cached), so we verify the helper exposes the
    # right error type for downstream wiring.
    assert issubclass(PlanLimitExceededError, Exception)


async def test_enforce_quota_raises_when_setting_flag_on(
    client, monkeypatch
) -> None:
    """Toggling the in-memory flag forces enforcement immediately."""

    from app.database import get_sessionmaker
    from app.services.billing import usage as usage_module
    from app.services.billing.usage import (
        PlanLimitExceededError,
        enforce_quota,
    )

    user, _headers = await _register_and_login(client, idx=21)
    factory = get_sessionmaker()

    # ``enforce_quota`` calls ``quotas_enforced()`` from this module's
    # namespace (imported at module load time), so we patch it there
    # rather than on the source module.
    monkeypatch.setattr(usage_module, "quotas_enforced", lambda: True)

    async with factory() as session:
        # Free tier — max_assessments_per_day == 3. The 4th call must
        # raise PLAN_LIMIT_EXCEEDED with the upgrade payload.
        for _ in range(3):
            await enforce_quota(
                session,
                user_id=user["id"],
                metric="assessments_per_day",
            )
        with pytest.raises(PlanLimitExceededError) as exc:
            await enforce_quota(
                session,
                user_id=user["id"],
                metric="assessments_per_day",
            )
        await session.rollback()

    assert exc.value.status_code == 402
    assert exc.value.code == "PLAN_LIMIT_EXCEEDED"
    payload = exc.value.extra
    assert payload["metric"] == "assessments_per_day"
    assert payload["limit"] == 3
    assert payload["plan_code"] == "free"
    assert payload["upgrade_url"] == "/billing/plans"
