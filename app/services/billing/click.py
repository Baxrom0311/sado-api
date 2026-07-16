"""Click Merchant API integration (Prepare + Complete protocol).

Click uses two webhooks signed with MD5(click_trans_id + service_id +
secret_key + merchant_trans_id + amount + action + sign_time):

* ``Prepare`` (action=0) — validate that we recognise the order.
* ``Complete`` (action=1) — finalise: mark order paid, provision
  subscription.

The webhook accepts form-encoded fields. We respond with JSON Click
expects.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.billing import (
    PaymentOrder,
    PaymentOrderState,
    PaymentProvider,
    PaymentTransaction,
    PaymentTransactionState,
)
from app.services.billing.plans import get_plan_by_code
from app.services.billing.subscriptions import activate_subscription

logger = logging.getLogger(__name__)


# Click error codes (subset that we surface).
CLICK_OK = 0
CLICK_ERR_SIGN = -1
CLICK_ERR_INVALID_AMOUNT = -2
CLICK_ERR_ACTION_NOT_FOUND = -3
CLICK_ERR_ALREADY_PAID = -4
CLICK_ERR_USER_NOT_FOUND = -5
CLICK_ERR_TRANSACTION_NOT_FOUND = -6
CLICK_ERR_FAILED_TO_PERFORM = -9

ACTION_PREPARE = 0
ACTION_COMPLETE = 1


def _utcnow() -> datetime:
    return datetime.now(UTC)


def verify_signature(
    *,
    click_trans_id: str,
    service_id: str,
    secret_key: str,
    merchant_trans_id: str,
    amount: str,
    action: str,
    sign_time: str,
    sign_string: str,
    merchant_prepare_id: str | None = None,
) -> bool:
    """Validate Click's MD5 signature.

    For ``Complete`` requests, ``merchant_prepare_id`` is included in
    the digest after ``merchant_trans_id``.
    """

    if str(action) == str(ACTION_COMPLETE):
        digest_input = (
            f"{click_trans_id}{service_id}{secret_key}{merchant_trans_id}"
            f"{merchant_prepare_id or ''}{amount}{action}{sign_time}"
        )
    else:
        digest_input = (
            f"{click_trans_id}{service_id}{secret_key}{merchant_trans_id}"
            f"{amount}{action}{sign_time}"
        )
    expected = hashlib.md5(digest_input.encode("utf-8")).hexdigest()
    return _safe_eq(expected, sign_string.lower())


def _safe_eq(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b, strict=True):
        result |= ord(x) ^ ord(y)
    return result == 0


def _amount_to_tiyin(amount: str) -> int:
    """Click sends amounts in UZS as decimal strings (e.g. ``39000.00``).

    Convert to integer tiyin without floating-point drift.
    """

    cleaned = amount.strip()
    if "." in cleaned:
        whole, frac = cleaned.split(".", 1)
        frac = (frac + "00")[:2]
    else:
        whole, frac = cleaned, "00"
    return int(whole) * 100 + int(frac)


async def _load_tx(
    session: AsyncSession, click_trans_id: str
) -> PaymentTransaction | None:
    result = await session.execute(
        select(PaymentTransaction).where(
            PaymentTransaction.provider == PaymentProvider.CLICK.value,
            PaymentTransaction.provider_tx_id == click_trans_id,
        )
    )
    return result.scalar_one_or_none()


async def handle_click_request(
    session: AsyncSession, form: dict[str, str]
) -> dict[str, Any]:
    """Dispatch a Click webhook (Prepare or Complete)."""

    settings = get_settings()
    secret_key = settings.click_secret_key or ""
    service_id = str(settings.click_service_id or "")

    click_trans_id = str(form.get("click_trans_id", ""))
    posted_service_id = str(form.get("service_id", ""))
    merchant_trans_id = str(form.get("merchant_trans_id", ""))
    amount = str(form.get("amount", "0"))
    action = str(form.get("action", ""))
    sign_time = str(form.get("sign_time", ""))
    sign_string = str(form.get("sign_string", "")).lower()
    merchant_prepare_id = form.get("merchant_prepare_id")

    base_response: dict[str, Any] = {
        "click_trans_id": click_trans_id,
        "merchant_trans_id": merchant_trans_id,
    }

    if not secret_key or service_id != posted_service_id:
        return {
            **base_response,
            "error": CLICK_ERR_SIGN,
            "error_note": "Invalid service id",
        }

    if not verify_signature(
        click_trans_id=click_trans_id,
        service_id=posted_service_id,
        secret_key=secret_key,
        merchant_trans_id=merchant_trans_id,
        amount=amount,
        action=action,
        sign_time=sign_time,
        sign_string=sign_string,
        merchant_prepare_id=merchant_prepare_id,
    ):
        return {
            **base_response,
            "error": CLICK_ERR_SIGN,
            "error_note": "Invalid signature",
        }

    order = await session.get(PaymentOrder, merchant_trans_id)
    if order is None:
        return {
            **base_response,
            "error": CLICK_ERR_USER_NOT_FOUND,
            "error_note": "Order not found",
        }

    expected_amount = order.amount_tiyin
    received_tiyin = _amount_to_tiyin(amount)
    if received_tiyin != expected_amount:
        return {
            **base_response,
            "error": CLICK_ERR_INVALID_AMOUNT,
            "error_note": "Amount mismatch",
        }

    if action == str(ACTION_PREPARE):
        return await _click_prepare(session, order, click_trans_id, base_response)
    if action == str(ACTION_COMPLETE):
        return await _click_complete(
            session, order, click_trans_id, base_response, merchant_prepare_id
        )

    return {
        **base_response,
        "error": CLICK_ERR_ACTION_NOT_FOUND,
        "error_note": "Unknown action",
    }


async def _click_prepare(
    session: AsyncSession,
    order: PaymentOrder,
    click_trans_id: str,
    base: dict[str, Any],
) -> dict[str, Any]:
    if order.state == PaymentOrderState.PAID.value:
        return {
            **base,
            "error": CLICK_ERR_ALREADY_PAID,
            "error_note": "Order already paid",
        }

    existing = await _load_tx(session, click_trans_id)
    if existing is None:
        tx = PaymentTransaction(
            order_id=order.id,
            provider=PaymentProvider.CLICK.value,
            provider_tx_id=click_trans_id,
            state=PaymentTransactionState.CREATED.value,
            amount_tiyin=order.amount_tiyin,
            create_time_ms=int(_utcnow().timestamp() * 1000),
        )
        session.add(tx)
        order.state = PaymentOrderState.PENDING.value
        order.provider = PaymentProvider.CLICK.value
        await session.commit()
        await session.refresh(tx)
    else:
        tx = existing

    return {
        **base,
        "merchant_prepare_id": tx.id,
        "error": CLICK_OK,
        "error_note": "Success",
    }


async def _click_complete(
    session: AsyncSession,
    order: PaymentOrder,
    click_trans_id: str,
    base: dict[str, Any],
    merchant_prepare_id: str | None,
) -> dict[str, Any]:
    tx = await _load_tx(session, click_trans_id)
    if tx is None:
        return {
            **base,
            "error": CLICK_ERR_TRANSACTION_NOT_FOUND,
            "error_note": "Transaction not found",
        }
    if merchant_prepare_id and tx.id != merchant_prepare_id:
        return {
            **base,
            "error": CLICK_ERR_TRANSACTION_NOT_FOUND,
            "error_note": "Prepare id mismatch",
        }
    if order.state == PaymentOrderState.PAID.value:
        return {
            **base,
            "merchant_prepare_id": tx.id,
            "merchant_confirm_id": tx.id,
            "error": CLICK_ERR_ALREADY_PAID,
            "error_note": "Order already paid",
        }

    now = _utcnow()
    tx.state = PaymentTransactionState.PERFORMED.value
    tx.perform_time_ms = int(now.timestamp() * 1000)
    order.state = PaymentOrderState.PAID.value
    order.paid_at = now
    await session.flush()

    plan = await get_plan_by_code(session, order.plan_code)
    if plan is not None:
        await activate_subscription(
            session,
            user_id=order.user_id,
            plan_code=plan.code,
            duration_days=plan.duration_days,
            order=order,
        )
    await session.commit()
    await session.refresh(tx)

    return {
        **base,
        "merchant_prepare_id": tx.id,
        "merchant_confirm_id": tx.id,
        "error": CLICK_OK,
        "error_note": "Success",
    }


def build_click_payment_url(order_id: str, amount_tiyin: int) -> str:
    """Build a Click checkout URL.

    Click expects an ``amount`` in soum (UZS); we pass tiyin/100.
    """

    settings = get_settings()
    base = settings.click_checkout_url.rstrip("/")
    service_id = settings.click_service_id or "0"
    merchant_id = settings.click_merchant_id or "0"
    amount_uzs = amount_tiyin // 100
    return (
        f"{base}/services/pay"
        f"?service_id={service_id}"
        f"&merchant_id={merchant_id}"
        f"&amount={amount_uzs}"
        f"&transaction_param={order_id}"
    )


__all__ = [
    "ACTION_COMPLETE",
    "ACTION_PREPARE",
    "build_click_payment_url",
    "handle_click_request",
    "verify_signature",
]
