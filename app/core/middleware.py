"""Global exception handlers and request middleware.

Wires up:

* :class:`SadoAPIError` → consistent ``{"detail", "code"}`` payload.
* FastAPI ``RequestValidationError`` → 422 with ``VALIDATION_ERROR`` code.
* Unhandled ``Exception`` → 500 ``INTERNAL_ERROR`` (no stack trace leak).
* A small request-id middleware that stamps every response with
  ``X-Request-ID``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.core.exceptions import SadoAPIError

logger = logging.getLogger(__name__)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Attach a request id header to every response for traceability."""

    header_name = "X-Request-ID"

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get(self.header_name) or uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[self.header_name] = request_id
        return response


def _error_payload(
    detail: str,
    code: str,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"detail": detail, "code": code}
    if extra:
        payload["extra"] = extra
    return payload


def register_exception_handlers(app: FastAPI) -> None:
    """Register handlers on the given ``FastAPI`` instance."""

    @app.exception_handler(SadoAPIError)
    async def _sado_error(_: Request, exc: SadoAPIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(exc.message, exc.code, exc.extra or None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # ``exc.errors()`` may contain non-JSON-serializable objects (the
        # original ``ValueError`` from a Pydantic ``model_validator`` shows
        # up under ``ctx.error``). ``jsonable_encoder`` converts those to
        # repr strings.
        errors = jsonable_encoder(exc.errors())
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_error_payload(
                "Request validation failed",
                "VALIDATION_ERROR",
                {"errors": errors},
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "unknown")
        logger.exception(
            "unhandled error in request %s: %s",
            request_id,
            exc,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_payload(
                "Internal server error",
                "INTERNAL_ERROR",
                {"request_id": request_id},
            ),
        )
