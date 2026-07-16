"""Payme Merchant API integration.

Implements the JSON-RPC subset Payme calls into our backend after the
user pays in their checkout page:

* ``CheckPerformTransaction`` — pre-validate amount + account.
* ``CreateTransaction`` — open a provider transaction.
* ``PerformTransaction`` — finalise: mark order paid, provision
  subscription.
* ``CancelTransaction`` — refund / abort.
* ``CheckTransaction`` — return current state.
* ``GetStatement`` — list performed transactions in an interval.

Auth: HTTP Basic with username ``Paycom`` and password = merchant key.

Errors follow Payme's spec — any failure is returned as ``200 OK``
with an ``error`` envelope so Payme can show the user the right
message. The numeric codes used here are documented in
https://developer.help.paycom.uz/.
"""

from __future__ import annotations

import base64
import binascii
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


# --------------------------------------------------------------- Error codes


# Payme expects these exact integers — do not rename without updating
# the merchant integration.
ERR_INVALID_AUTH = -32504
ERR_METHOD_NOT_FOUND = -32601
ERR_PARSE = -32700
ERR_INVALID_AMOUNT = -31001
ERR_TRANSACTION_NOT_FOUND = -31003
ERR_CANNOT_PERFORM = -31008
ERR_CANNOT_CANCEL = -31007
ERR_INVALID_ACCOUNT = -31050  # account.* missing / order not found
ERR_PENDING = -31050           # generic pending; reused for clarity


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _epoch_ms(dt: datetime | None) -> int:
    if dt is None:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _from_epoch_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _err(
    code: int,
    message: str,
    *,
    data: str | None = None,
    request_id: int | str | None = None,
) -> dict[str, Any]:
    """Build a Payme JSON-RPC error envelope.

    Payme expects ``message`` as a localised dict. We use the same
    string for all three locales since human-readable messages here
    are mostly diagnostic — Payme's UI only shows them on developer
    sandboxes.
    """

    payload: dict[str, Any] = {
        "id": request_id,
        "error": {
            "code": code,
            "message": {"uz": message, "ru": message, "en": message},
        },
    }
    if data is not None:
        payload["error"]["data"] = data
    return payload


def _ok(result: dict[str, Any], request_id: int | str | None) -> dict[str, Any]:
    return {"id": request_id, "result": result}


# ----------------------------------------------------------- Authorisation


def verify_basic_auth(authorization_header: str | None) -> bool:
    """Validate the HTTP ``Authorization: Basic ...`` header.

    Returns ``True`` if the supplied password matches the configured
    Payme merchant key. The username is conventionally ``Paycom`` but
    we accept anything — Payme only signs with the password.
    """

    settings = get_settings()
    expected = settings.payme_merchant_key
    if not expected:
        # Without a configured key we deny all calls so production
        # webhooks fail loudly instead of silently accepting payments.
        return False
    if not authorization_header:
        return False
    parts = authorization_header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        return False
    try:
        decoded = base64.b64decode(parts[1], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    if ":" not in decoded:
        return False
    _username, password = decoded.split(":", 1)
    # Constant-time compare to keep brute-force resistance.
    return _safe_eq(password, expected)


def _safe_eq(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b, strict=True):
        result |= ord(x) ^ ord(y)
    return result == 0


# --------------------------------------------------------- Method handlers


async def _load_order(
    session: AsyncSession, order_id: str | None
) -> PaymentOrder | None:
    if not order_id:
        return None
    return await session.get(PaymentOrder, order_id)


async def _load_tx_by_provider_id(
    session: AsyncSession, provider_tx_id: str
) -> PaymentTransaction | None:
    result = await session.execute(
        select(PaymentTransaction).where(
            PaymentTransaction.provider == PaymentProvider.PAYME.value,
            PaymentTransaction.provider_tx_id == provider_tx_id,
        )
    )
    return result.scalar_one_or_none()


async def _check_perform(
    session: AsyncSession,
    params: dict[str, Any],
    request_id: int | str | None,
) -> dict[str, Any]:
    amount = params.get("amount")
    account = params.get("account") or {}
    order_id = account.get("order_id")
    order = await _load_order(session, order_id)
    if order is None:
        return _err(
            ERR_INVALID_ACCOUNT,
            "Order not found",
            data="order_id",
            request_id=request_id,
        )
    if order.state == PaymentOrderState.PAID.value:
        return _err(
            ERR_CANNOT_PERFORM,
            "Order already paid",
            request_id=request_id,
        )
    if not isinstance(amount, int) or amount != order.amount_tiyin:
        return _err(
            ERR_INVALID_AMOUNT,
            "Amount mismatch",
            data="amount",
            request_id=request_id,
        )
    return _ok({"allow": True}, request_id)


async def _create_transaction(
    session: AsyncSession,
    params: dict[str, Any],
    request_id: int | str | None,
) -> dict[str, Any]:
    payme_tx_id = params.get("id")
    create_time_ms = params.get("time")
    amount = params.get("amount")
    account = params.get("account") or {}
    order_id = account.get("order_id")

    if not isinstance(payme_tx_id, str) or not payme_tx_id:
        return _err(ERR_PARSE, "Missing id", request_id=request_id)

    existing = await _load_tx_by_provider_id(session, payme_tx_id)
    if existing is not None:
        if existing.state != PaymentTransactionState.CREATED.value:
            return _err(
                ERR_CANNOT_PERFORM,
                "Transaction in terminal state",
                request_id=request_id,
            )
        return _ok(
            {
                "create_time": existing.create_time_ms or 0,
                "transaction": existing.id,
                "state": existing.state,
            },
            request_id,
        )

    order = await _load_order(session, order_id)
    if order is None:
        return _err(
            ERR_INVALID_ACCOUNT,
            "Order not found",
            data="order_id",
            request_id=request_id,
        )
    if order.state == PaymentOrderState.PAID.value:
        return _err(
            ERR_CANNOT_PERFORM,
            "Order already paid",
            request_id=request_id,
        )
    if not isinstance(amount, int) or amount != order.amount_tiyin:
        return _err(
            ERR_INVALID_AMOUNT,
            "Amount mismatch",
            data="amount",
            request_id=request_id,
        )

    # Block parallel transactions against the same order.
    parallel = await session.execute(
        select(PaymentTransaction).where(
            PaymentTransaction.order_id == order.id,
            PaymentTransaction.state == PaymentTransactionState.CREATED.value,
        )
    )
    if parallel.scalar_one_or_none() is not None:
        return _err(
            ERR_PENDING,
            "Another transaction is already pending for this order",
            request_id=request_id,
        )

    tx = PaymentTransaction(
        order_id=order.id,
        provider=PaymentProvider.PAYME.value,
        provider_tx_id=payme_tx_id,
        state=PaymentTransactionState.CREATED.value,
        amount_tiyin=int(amount),
        create_time_ms=int(create_time_ms) if create_time_ms else _epoch_ms(_utcnow()),
        raw_payload=params,
    )
    session.add(tx)
    order.state = PaymentOrderState.PENDING.value
    order.provider = PaymentProvider.PAYME.value
    await session.commit()
    await session.refresh(tx)

    return _ok(
        {
            "create_time": tx.create_time_ms or 0,
            "transaction": tx.id,
            "state": tx.state,
        },
        request_id,
    )


async def _perform_transaction(
    session: AsyncSession,
    params: dict[str, Any],
    request_id: int | str | None,
) -> dict[str, Any]:
    payme_tx_id = params.get("id")
    if not isinstance(payme_tx_id, str):
        return _err(ERR_PARSE, "Missing id", request_id=request_id)

    tx = await _load_tx_by_provider_id(session, payme_tx_id)
    if tx is None:
        return _err(
            ERR_TRANSACTION_NOT_FOUND,
            "Transaction not found",
            request_id=request_id,
        )
    if tx.state == PaymentTransactionState.PERFORMED.value:
        return _ok(
            {
                "transaction": tx.id,
                "perform_time": tx.perform_time_ms or 0,
                "state": tx.state,
            },
            request_id,
        )
    if tx.state != PaymentTransactionState.CREATED.value:
        return _err(
            ERR_CANNOT_PERFORM,
            "Transaction is not in a performable state",
            request_id=request_id,
        )

    order = await session.get(PaymentOrder, tx.order_id)
    if order is None:
        return _err(
            ERR_TRANSACTION_NOT_FOUND,
            "Order missing",
            request_id=request_id,
        )

    now = _utcnow()
    tx.state = PaymentTransactionState.PERFORMED.value
    tx.perform_time_ms = _epoch_ms(now)
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
    return _ok(
        {
            "transaction": tx.id,
            "perform_time": tx.perform_time_ms or 0,
            "state": tx.state,
        },
        request_id,
    )


async def _cancel_transaction(
    session: AsyncSession,
    params: dict[str, Any],
    request_id: int | str | None,
) -> dict[str, Any]:
    payme_tx_id = params.get("id")
    reason = params.get("reason")
    if not isinstance(payme_tx_id, str):
        return _err(ERR_PARSE, "Missing id", request_id=request_id)

    tx = await _load_tx_by_provider_id(session, payme_tx_id)
    if tx is None:
        return _err(
            ERR_TRANSACTION_NOT_FOUND,
            "Transaction not found",
            request_id=request_id,
        )

    now = _utcnow()
    if tx.state == PaymentTransactionState.CREATED.value:
        tx.state = PaymentTransactionState.CANCELLED_PENDING.value
    elif tx.state == PaymentTransactionState.PERFORMED.value:
        tx.state = PaymentTransactionState.CANCELLED_PERFORMED.value
    elif tx.state in (
        PaymentTransactionState.CANCELLED_PENDING.value,
        PaymentTransactionState.CANCELLED_PERFORMED.value,
    ):
        return _ok(
            {
                "transaction": tx.id,
                "cancel_time": tx.cancel_time_ms or 0,
                "state": tx.state,
            },
            request_id,
        )
    else:
        return _err(
            ERR_CANNOT_CANCEL,
            "Transaction cannot be cancelled",
            request_id=request_id,
        )

    tx.cancel_time_ms = _epoch_ms(now)
    if isinstance(reason, int):
        tx.cancel_reason = reason

    order = await session.get(PaymentOrder, tx.order_id)
    if order is not None:
        order.state = PaymentOrderState.CANCELLED.value
        order.cancelled_at = now
    await session.commit()
    await session.refresh(tx)
    return _ok(
        {
            "transaction": tx.id,
            "cancel_time": tx.cancel_time_ms or 0,
            "state": tx.state,
        },
        request_id,
    )


async def _check_transaction(
    session: AsyncSession,
    params: dict[str, Any],
    request_id: int | str | None,
) -> dict[str, Any]:
    payme_tx_id = params.get("id")
    if not isinstance(payme_tx_id, str):
        return _err(ERR_PARSE, "Missing id", request_id=request_id)

    tx = await _load_tx_by_provider_id(session, payme_tx_id)
    if tx is None:
        return _err(
            ERR_TRANSACTION_NOT_FOUND,
            "Transaction not found",
            request_id=request_id,
        )
    return _ok(
        {
            "create_time": tx.create_time_ms or 0,
            "perform_time": tx.perform_time_ms or 0,
            "cancel_time": tx.cancel_time_ms or 0,
            "transaction": tx.id,
            "state": tx.state,
            "reason": tx.cancel_reason,
        },
        request_id,
    )


async def _get_statement(
    session: AsyncSession,
    params: dict[str, Any],
    request_id: int | str | None,
) -> dict[str, Any]:
    start = params.get("from")
    end = params.get("to")
    if not isinstance(start, int) or not isinstance(end, int):
        return _err(ERR_PARSE, "Missing from/to", request_id=request_id)

    result = await session.execute(
        select(PaymentTransaction).where(
            PaymentTransaction.provider == PaymentProvider.PAYME.value,
            PaymentTransaction.create_time_ms >= start,
            PaymentTransaction.create_time_ms <= end,
        )
    )
    rows = list(result.scalars().all())
    transactions = [
        {
            "id": tx.provider_tx_id,
            "time": tx.create_time_ms or 0,
            "amount": tx.amount_tiyin,
            "account": {"order_id": tx.order_id},
            "create_time": tx.create_time_ms or 0,
            "perform_time": tx.perform_time_ms or 0,
            "cancel_time": tx.cancel_time_ms or 0,
            "transaction": tx.id,
            "state": tx.state,
            "reason": tx.cancel_reason,
        }
        for tx in rows
    ]
    return _ok({"transactions": transactions}, request_id)


# ---------------------------------------------------------------- Dispatch


async def handle_payme_request(
    session: AsyncSession,
    body: dict[str, Any],
    *,
    authorization: str | None,
) -> dict[str, Any]:
    """Dispatch a Payme JSON-RPC request to the right handler.

    Returns a dict ready to be JSON-encoded with HTTP 200.
    """

    request_id = body.get("id") if isinstance(body, dict) else None
    if not verify_basic_auth(authorization):
        return _err(
            ERR_INVALID_AUTH,
            "Authorization required",
            request_id=request_id,
        )

    if not isinstance(body, dict):
        return _err(ERR_PARSE, "Invalid JSON-RPC envelope")

    method = body.get("method")
    params = body.get("params") or {}
    if not isinstance(params, dict):
        return _err(ERR_PARSE, "Invalid params", request_id=request_id)

    handlers = {
        "CheckPerformTransaction": _check_perform,
        "CreateTransaction": _create_transaction,
        "PerformTransaction": _perform_transaction,
        "CancelTransaction": _cancel_transaction,
        "CheckTransaction": _check_transaction,
        "GetStatement": _get_statement,
    }
    handler = handlers.get(method)
    if handler is None:
        return _err(
            ERR_METHOD_NOT_FOUND,
            f"Unknown method: {method}",
            request_id=request_id,
        )

    try:
        return await handler(session, params, request_id)
    except Exception:  # noqa: BLE001 — Payme expects 200 OK with error
        logger.exception("Payme handler crashed for method %s", method)
        await session.rollback()
        return _err(
            ERR_PARSE,
            "Internal billing error",
            request_id=request_id,
        )


def build_payment_url(order_id: str, amount_tiyin: int) -> str:
    """Construct the Payme checkout URL the mobile client opens.

    Payme expects a base64-encoded ``m=<merchant_id>;ac.order_id=<id>;a=<amount>``
    string appended to the checkout host.
    """

    settings = get_settings()
    merchant_id = settings.payme_merchant_id or "test_merchant"
    raw = f"m={merchant_id};ac.order_id={order_id};a={amount_tiyin}"
    encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    base = settings.payme_checkout_url.rstrip("/")
    return f"{base}/{encoded}"


__all__ = [
    "build_payment_url",
    "handle_payme_request",
    "verify_basic_auth",
]
