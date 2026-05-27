"""Custom exceptions and a consistent error response shape.

All API errors should subclass :class:`SadoAPIError` so the error
handler can produce a unified payload::

    {"detail": "human readable message", "code": "ERROR_CODE"}
"""

from __future__ import annotations

from typing import Any

from fastapi import status


class SadoAPIError(Exception):
    """Base class for application-level API errors."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "BAD_REQUEST"
    default_message: str = "Bad request"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or self.default_message)
        self.message = message or self.default_message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.extra = extra or {}


class NotFoundError(SadoAPIError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "NOT_FOUND"
    default_message = "Resource not found"


class UnauthorizedError(SadoAPIError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "UNAUTHORIZED"
    default_message = "Authentication required"


class ForbiddenError(SadoAPIError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "FORBIDDEN"
    default_message = "You do not have access to this resource"


class ConflictError(SadoAPIError):
    status_code = status.HTTP_409_CONFLICT
    code = "CONFLICT"
    default_message = "Resource conflict"


class ValidationError(SadoAPIError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "VALIDATION_ERROR"
    default_message = "Validation failed"


class RateLimitError(SadoAPIError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "RATE_LIMITED"
    default_message = "Too many requests, please slow down"
