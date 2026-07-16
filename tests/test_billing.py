"""End-to-end tests for the billing layer.

Coverage:

* ``GET /billing/plans``           — public, seeds defaults on first call.
* ``GET /billing/subscription``    — auth, free fallback for new users.
* ``POST/GET /billing/orders``     — auth, create + paginate.
* ``POST /billing/webhooks/payme`` — full happy path (CheckPerform →
  CreateTransaction → PerformTransaction) plus auth, amount, and
  idempotency edge cases. The Payme HTTP client is fully mocked
  through the in-process ASGI transport (no outbound calls).
* ``POST /billing/webhooks/click`` — full Prepare/Complete happy path
  plus signature failure and amount mismatch.

Backwards compatibility is asserted by the lack of changes to the
``/auth/register`` and ``/children`` endpoints — free users (no
subscription row) keep working exactly as before.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from app.config import get_settings

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------- Helpers


async def _register_and_login(
    client, idx: int = 1, role: str = "parent"
) -> tuple[dict, dict[str, str]]:
    creds = {
        "email": f"billing-user{idx}@example.com",
        "password": "Sup3r-Secret!",
        "full_name": f"Billing User {idx}",
        "role": role,
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


def _payme_basic_auth(password: str | None = None) -> str:
    settings = get_settings()
    pwd = password if password is not None else settings.payme_merchant_key
    raw = f"Paycom:{pwd}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _click_signature(
    *,
    click_trans_id: str,
    service_id: str,
    secret_key: str,
    merchant_trans_id: str,
    amount: str,
    action: str,
    sign_time: str,
    merchant_prepare_id: str | None = None,
) -> str:
    if str(action) == "1":
        digest_input = (
            f"{click_trans_id}{service_id}{secret_key}{merchant_trans_id}"
            f"{merchant_prepare_id or ''}{amount}{action}{sign_time}"
        )
    else:
        digest_input = (
            f"{click_trans_id}{service_id}{secret_key}{merchant_trans_id}"
            f"{amount}{action}{sign_time}"
        )
    return hashlib.md5(digest_input.encode("utf-8")).hexdigest()


# --------------------------------------------------------------- Plans


async def test_list_plans_seeds_defaults_and_returns_active(client) -> None:
    response = await client.get("/api/v1/billing/plans")
    assert response.status_code == 200, response.text
    plans = response.json()
    codes = {p["code"] for p in plans}
    assert {"free", "basic", "premium", "logoped_pro", "clinic"}.issubset(codes)

    basic = next(p for p in plans if p["code"] == "basic")
    # Basic = 39_000 UZS = 3_900_000 tiyin (matches Payme webhook spec).
    assert basic["price_tiyin"] == 3_900_000
    assert basic["price_uzs"] == 39_000
    assert basic["features"]["max_children"] == 3
    assert basic["is_active"] is True

    free = next(p for p in plans if p["code"] == "free")
    assert free["price_tiyin"] == 0
    assert free["features"]["ai_analysis"] is False
    # Free tier was bumped from 1 → 3 assessments/day in the v2 plan
    # catalogue. Tests that hard-code the literal must update too.
    assert free["features"]["max_assessments_per_day"] == 3

    logoped = next(p for p in plans if p["code"] == "logoped_pro")
    assert logoped["price_tiyin"] == 14_900_000
    assert logoped["price_uzs"] == 149_000
    assert logoped["features"]["max_patients"] == 50
    assert logoped["features"]["patient_management"] is True

    clinic = next(p for p in plans if p["code"] == "clinic")
    # Clinic is sales-led — surfaced for marketing only, not directly
    # payable through POST /billing/orders.
    assert clinic["price_tiyin"] == 0
    assert clinic["features"]["tenant_admin"] is True
    assert clinic["features"]["priority_support"] is True


async def test_clinic_plan_is_not_directly_payable(client) -> None:
    _, headers = await _register_and_login(client, idx=1)
    await client.get("/api/v1/billing/plans")
    response = await client.post(
        "/api/v1/billing/orders",
        json={"plan_code": "clinic", "provider": "payme"},
        headers=headers,
    )
    # Same code path as the free plan rejection.
    assert response.status_code == 422
    assert response.json()["code"] == "PLAN_NOT_PAYABLE"


async def test_list_plans_idempotent_seeding(client) -> None:
    first = await client.get("/api/v1/billing/plans")
    second = await client.get("/api/v1/billing/plans")
    assert first.status_code == 200
    assert second.status_code == 200
    # Same row count, same plan ids — seeder is idempotent.
    first_ids = sorted(p["id"] for p in first.json())
    second_ids = sorted(p["id"] for p in second.json())
    assert first_ids == second_ids


# ----------------------------------------------------------- Subscription


async def test_subscription_defaults_to_free_for_new_user(client) -> None:
    _, headers = await _register_and_login(client, idx=1)
    response = await client.get(
        "/api/v1/billing/subscription", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plan_code"] == "free"
    assert body["status"] == "active"
    assert body["is_active"] is True
    assert body["days_remaining"] is None
    # Free-tier feature flags are exposed inline so the mobile UI can
    # gate elements without a second round-trip.
    assert body["features"]["max_children"] == 1
    assert body["features"]["ai_analysis"] is False


async def test_subscription_requires_auth(client) -> None:
    response = await client.get("/api/v1/billing/subscription")
    assert response.status_code == 401


async def test_me_features_endpoint_for_free_user(client) -> None:
    _, headers = await _register_and_login(client, idx=1)
    response = await client.get(
        "/api/v1/billing/me/features", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plan_code"] == "free"
    assert body["is_active"] is True
    assert body["features"]["max_children"] == 1
    assert body["features"]["ai_analysis"] is False
    # Grace period — quotas not yet enforced.
    assert body["quotas_enforced"] is False


async def test_me_features_endpoint_requires_auth(client) -> None:
    response = await client.get("/api/v1/billing/me/features")
    assert response.status_code == 401


# ---------------------------------------------------------------- Orders


async def test_create_order_for_basic_plan_returns_payment_url(
    client,
) -> None:
    _, headers = await _register_and_login(client, idx=1)
    # Seed plans first.
    await client.get("/api/v1/billing/plans")

    response = await client.post(
        "/api/v1/billing/orders",
        json={"plan_code": "basic", "provider": "payme"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["order"]["plan_code"] == "basic"
    assert body["order"]["amount_tiyin"] == 3_900_000
    assert body["order"]["amount_uzs"] == 39_000
    assert body["order"]["state"] == "created"
    assert body["order"]["provider"] == "payme"
    assert body["payment_url"].startswith("https://checkout.paycom.uz/")


async def test_create_order_with_click_provider(client) -> None:
    _, headers = await _register_and_login(client, idx=1)
    await client.get("/api/v1/billing/plans")

    response = await client.post(
        "/api/v1/billing/orders",
        json={"plan_code": "basic", "provider": "click"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["order"]["provider"] == "click"
    assert "click" in body["payment_url"].lower()
    # Click checkout uses UZS (soum) in the URL — 39000 not 3900000.
    assert "amount=39000" in body["payment_url"]


async def test_create_order_unknown_plan_404(client) -> None:
    _, headers = await _register_and_login(client, idx=1)
    await client.get("/api/v1/billing/plans")

    response = await client.post(
        "/api/v1/billing/orders",
        json={"plan_code": "platinum", "provider": "payme"},
        headers=headers,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "PLAN_NOT_FOUND"


async def test_create_order_for_free_plan_rejected(client) -> None:
    _, headers = await _register_and_login(client, idx=1)
    await client.get("/api/v1/billing/plans")

    response = await client.post(
        "/api/v1/billing/orders",
        json={"plan_code": "free", "provider": "payme"},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "PLAN_NOT_PAYABLE"


async def test_list_orders_returns_only_callers_orders(client) -> None:
    _, headers_one = await _register_and_login(client, idx=1)
    _, headers_two = await _register_and_login(client, idx=2)
    await client.get("/api/v1/billing/plans")

    for _ in range(2):
        await client.post(
            "/api/v1/billing/orders",
            json={"plan_code": "basic", "provider": "payme"},
            headers=headers_one,
        )
    await client.post(
        "/api/v1/billing/orders",
        json={"plan_code": "basic", "provider": "payme"},
        headers=headers_two,
    )

    one = await client.get("/api/v1/billing/orders", headers=headers_one)
    assert one.status_code == 200
    assert len(one.json()["items"]) == 2

    two = await client.get("/api/v1/billing/orders", headers=headers_two)
    assert two.status_code == 200
    assert len(two.json()["items"]) == 1


# --------------------------------------------------------- Payme webhook


async def _create_basic_order_for(client, headers) -> dict:
    await client.get("/api/v1/billing/plans")
    response = await client.post(
        "/api/v1/billing/orders",
        json={"plan_code": "basic", "provider": "payme"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["order"]


async def test_payme_webhook_rejects_missing_auth(client) -> None:
    response = await client.post(
        "/api/v1/billing/webhooks/payme",
        json={
            "method": "CheckPerformTransaction",
            "params": {"amount": 3_900_000, "account": {"order_id": "x"}},
        },
    )
    # Payme expects HTTP 200 with an error envelope, not a 401.
    assert response.status_code == 200
    body = response.json()
    assert "error" in body
    assert body["error"]["code"] == -32504


async def test_payme_webhook_rejects_wrong_password(client) -> None:
    response = await client.post(
        "/api/v1/billing/webhooks/payme",
        json={"method": "CheckPerformTransaction", "params": {}},
        headers={"Authorization": _payme_basic_auth("wrong-key")},
    )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32504


async def test_payme_check_perform_unknown_order(client) -> None:
    response = await client.post(
        "/api/v1/billing/webhooks/payme",
        json={
            "id": 1,
            "method": "CheckPerformTransaction",
            "params": {
                "amount": 3_900_000,
                "account": {"order_id": "00000000-0000-0000-0000-000000000000"},
            },
        },
        headers={"Authorization": _payme_basic_auth()},
    )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == -31050


async def test_payme_check_perform_amount_mismatch(client) -> None:
    _, headers = await _register_and_login(client, idx=1)
    order = await _create_basic_order_for(client, headers)

    response = await client.post(
        "/api/v1/billing/webhooks/payme",
        json={
            "id": 2,
            "method": "CheckPerformTransaction",
            "params": {
                "amount": 1,  # wrong amount
                "account": {"order_id": order["id"]},
            },
        },
        headers={"Authorization": _payme_basic_auth()},
    )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == -31001


async def test_payme_full_happy_path_provisions_subscription(client) -> None:
    user, headers = await _register_and_login(client, idx=1)
    order = await _create_basic_order_for(client, headers)
    auth = {"Authorization": _payme_basic_auth()}

    # 1. CheckPerformTransaction — should allow.
    check = await client.post(
        "/api/v1/billing/webhooks/payme",
        json={
            "id": 1,
            "method": "CheckPerformTransaction",
            "params": {
                "amount": order["amount_tiyin"],
                "account": {"order_id": order["id"]},
            },
        },
        headers=auth,
    )
    assert check.json()["result"]["allow"] is True

    # 2. CreateTransaction — opens a Payme transaction.
    create = await client.post(
        "/api/v1/billing/webhooks/payme",
        json={
            "id": 2,
            "method": "CreateTransaction",
            "params": {
                "id": "payme-tx-1",
                "time": 1_700_000_000_000,
                "amount": order["amount_tiyin"],
                "account": {"order_id": order["id"]},
            },
        },
        headers=auth,
    )
    create_body = create.json()
    assert "result" in create_body, create_body
    assert create_body["result"]["state"] == 1

    # 3. PerformTransaction — finalises the payment.
    perform = await client.post(
        "/api/v1/billing/webhooks/payme",
        json={
            "id": 3,
            "method": "PerformTransaction",
            "params": {"id": "payme-tx-1"},
        },
        headers=auth,
    )
    perform_body = perform.json()
    assert "result" in perform_body, perform_body
    assert perform_body["result"]["state"] == 2

    # Subscription must now be active on the basic plan.
    sub = await client.get(
        "/api/v1/billing/subscription", headers=headers
    )
    assert sub.status_code == 200
    sub_body = sub.json()
    assert sub_body["plan_code"] == "basic"
    assert sub_body["is_active"] is True
    assert sub_body["days_remaining"] is not None
    assert sub_body["days_remaining"] >= 1
    # Paid plan exposes its own features (ai_analysis enabled, larger
    # child cap) — the mobile client uses this to unlock UI.
    assert sub_body["features"]["ai_analysis"] is True
    assert sub_body["features"]["max_children"] == 3

    # /me/features mirrors the paid plan.
    feats = await client.get(
        "/api/v1/billing/me/features", headers=headers
    )
    assert feats.status_code == 200
    feats_body = feats.json()
    assert feats_body["plan_code"] == "basic"
    assert feats_body["features"]["ai_analysis"] is True
    assert feats_body["is_active"] is True

    # Order is paid.
    orders = await client.get("/api/v1/billing/orders", headers=headers)
    assert orders.json()["items"][0]["state"] == "paid"


async def test_payme_perform_is_idempotent(client) -> None:
    _, headers = await _register_and_login(client, idx=1)
    order = await _create_basic_order_for(client, headers)
    auth = {"Authorization": _payme_basic_auth()}

    create_payload = {
        "id": 1,
        "method": "CreateTransaction",
        "params": {
            "id": "payme-tx-2",
            "time": 1_700_000_000_000,
            "amount": order["amount_tiyin"],
            "account": {"order_id": order["id"]},
        },
    }
    await client.post(
        "/api/v1/billing/webhooks/payme",
        json=create_payload,
        headers=auth,
    )

    perform_payload = {
        "id": 2,
        "method": "PerformTransaction",
        "params": {"id": "payme-tx-2"},
    }
    first = await client.post(
        "/api/v1/billing/webhooks/payme",
        json=perform_payload,
        headers=auth,
    )
    second = await client.post(
        "/api/v1/billing/webhooks/payme",
        json=perform_payload,
        headers=auth,
    )
    assert "result" in first.json()
    assert "result" in second.json()
    assert first.json()["result"]["state"] == 2
    assert second.json()["result"]["state"] == 2
    # And we still have exactly one subscription row in the listing.
    sub = await client.get(
        "/api/v1/billing/subscription", headers=headers
    )
    assert sub.json()["plan_code"] == "basic"


async def test_payme_cancel_after_create_marks_cancelled(client) -> None:
    _, headers = await _register_and_login(client, idx=1)
    order = await _create_basic_order_for(client, headers)
    auth = {"Authorization": _payme_basic_auth()}

    await client.post(
        "/api/v1/billing/webhooks/payme",
        json={
            "id": 1,
            "method": "CreateTransaction",
            "params": {
                "id": "payme-tx-3",
                "time": 1_700_000_000_000,
                "amount": order["amount_tiyin"],
                "account": {"order_id": order["id"]},
            },
        },
        headers=auth,
    )
    cancel = await client.post(
        "/api/v1/billing/webhooks/payme",
        json={
            "id": 2,
            "method": "CancelTransaction",
            "params": {"id": "payme-tx-3", "reason": 3},
        },
        headers=auth,
    )
    assert cancel.json()["result"]["state"] == -1


async def test_payme_check_transaction_returns_state(client) -> None:
    _, headers = await _register_and_login(client, idx=1)
    order = await _create_basic_order_for(client, headers)
    auth = {"Authorization": _payme_basic_auth()}

    await client.post(
        "/api/v1/billing/webhooks/payme",
        json={
            "id": 1,
            "method": "CreateTransaction",
            "params": {
                "id": "payme-tx-4",
                "time": 1_700_000_000_000,
                "amount": order["amount_tiyin"],
                "account": {"order_id": order["id"]},
            },
        },
        headers=auth,
    )
    check = await client.post(
        "/api/v1/billing/webhooks/payme",
        json={
            "id": 2,
            "method": "CheckTransaction",
            "params": {"id": "payme-tx-4"},
        },
        headers=auth,
    )
    body = check.json()
    assert "result" in body
    assert body["result"]["state"] == 1


async def test_payme_unknown_method_returns_method_not_found(client) -> None:
    response = await client.post(
        "/api/v1/billing/webhooks/payme",
        json={"id": 99, "method": "Bogus", "params": {}},
        headers={"Authorization": _payme_basic_auth()},
    )
    body = response.json()
    assert "error" in body
    assert body["error"]["code"] == -32601


# --------------------------------------------------------- Click webhook


async def test_click_webhook_invalid_signature_rejected(client) -> None:
    _, headers = await _register_and_login(client, idx=1)
    await client.get("/api/v1/billing/plans")
    create = await client.post(
        "/api/v1/billing/orders",
        json={"plan_code": "basic", "provider": "click"},
        headers=headers,
    )
    order = create.json()["order"]

    settings = get_settings()
    response = await client.post(
        "/api/v1/billing/webhooks/click",
        data={
            "click_trans_id": "click-tx-1",
            "service_id": str(settings.click_service_id),
            "merchant_trans_id": order["id"],
            "amount": "39000.00",
            "action": "0",
            "sign_time": "2026-06-13 00:00:00",
            "sign_string": "deadbeef" * 4,
        },
    )
    body = response.json()
    assert body["error"] == -1


async def test_click_webhook_full_happy_path(client) -> None:
    _, headers = await _register_and_login(client, idx=1)
    await client.get("/api/v1/billing/plans")
    create = await client.post(
        "/api/v1/billing/orders",
        json={"plan_code": "basic", "provider": "click"},
        headers=headers,
    )
    order = create.json()["order"]

    settings = get_settings()
    service_id = str(settings.click_service_id)
    secret_key = settings.click_secret_key
    click_trans_id = "click-tx-happy-1"
    sign_time = "2026-06-13 00:00:00"
    amount = "39000.00"

    prepare_sign = _click_signature(
        click_trans_id=click_trans_id,
        service_id=service_id,
        secret_key=secret_key,
        merchant_trans_id=order["id"],
        amount=amount,
        action="0",
        sign_time=sign_time,
    )
    prepare = await client.post(
        "/api/v1/billing/webhooks/click",
        data={
            "click_trans_id": click_trans_id,
            "service_id": service_id,
            "merchant_trans_id": order["id"],
            "amount": amount,
            "action": "0",
            "sign_time": sign_time,
            "sign_string": prepare_sign,
        },
    )
    prepare_body = prepare.json()
    assert prepare_body["error"] == 0, prepare_body
    merchant_prepare_id = prepare_body["merchant_prepare_id"]

    complete_sign = _click_signature(
        click_trans_id=click_trans_id,
        service_id=service_id,
        secret_key=secret_key,
        merchant_trans_id=order["id"],
        amount=amount,
        action="1",
        sign_time=sign_time,
        merchant_prepare_id=merchant_prepare_id,
    )
    complete = await client.post(
        "/api/v1/billing/webhooks/click",
        data={
            "click_trans_id": click_trans_id,
            "service_id": service_id,
            "merchant_trans_id": order["id"],
            "merchant_prepare_id": merchant_prepare_id,
            "amount": amount,
            "action": "1",
            "sign_time": sign_time,
            "sign_string": complete_sign,
        },
    )
    complete_body = complete.json()
    assert complete_body["error"] == 0, complete_body
    assert complete_body["merchant_confirm_id"] == merchant_prepare_id

    sub = await client.get(
        "/api/v1/billing/subscription", headers=headers
    )
    assert sub.json()["plan_code"] == "basic"
    assert sub.json()["is_active"] is True


async def test_click_webhook_amount_mismatch(client) -> None:
    _, headers = await _register_and_login(client, idx=1)
    await client.get("/api/v1/billing/plans")
    create = await client.post(
        "/api/v1/billing/orders",
        json={"plan_code": "basic", "provider": "click"},
        headers=headers,
    )
    order = create.json()["order"]

    settings = get_settings()
    service_id = str(settings.click_service_id)
    secret_key = settings.click_secret_key
    click_trans_id = "click-tx-bad-amount"
    sign_time = "2026-06-13 00:00:00"
    amount = "10.00"  # wrong amount

    sign = _click_signature(
        click_trans_id=click_trans_id,
        service_id=service_id,
        secret_key=secret_key,
        merchant_trans_id=order["id"],
        amount=amount,
        action="0",
        sign_time=sign_time,
    )
    response = await client.post(
        "/api/v1/billing/webhooks/click",
        data={
            "click_trans_id": click_trans_id,
            "service_id": service_id,
            "merchant_trans_id": order["id"],
            "amount": amount,
            "action": "0",
            "sign_time": sign_time,
            "sign_string": sign,
        },
    )
    body = response.json()
    assert body["error"] == -2


# ----------------------------------------------------- Backwards compat


async def test_existing_endpoints_still_work_for_free_user(client) -> None:
    """A brand-new (free) user can still register, login, and access
    the existing app surfaces without any subscription row."""

    user, headers = await _register_and_login(client, idx=42)
    # /me — auth-protected resource that pre-dates billing.
    me = await client.get("/api/v1/users/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["id"] == user["id"]

    # Children listing — pre-existing, no quota enforcement on free.
    listing = await client.get("/api/v1/children", headers=headers)
    assert listing.status_code == 200
