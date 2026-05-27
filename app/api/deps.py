"""Reusable FastAPI dependencies — DB session, current user, RBAC."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, RateLimitError, UnauthorizedError
from app.core.rate_limit import RateLimiter, get_auth_rate_limiter
from app.core.security import TokenType, decode_token
from app.database import get_session
from app.models.user import User, UserRole
from app.services.auth import get_deny_list

bearer_scheme = HTTPBearer(auto_error=False)

DBSession = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(
    session: DBSession,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ] = None,
) -> User:
    """Resolve the authenticated user from a Bearer access token."""

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise UnauthorizedError("Missing bearer token", code="MISSING_TOKEN")

    payload = decode_token(credentials.credentials, expected_type=TokenType.ACCESS)

    deny = get_deny_list()
    if await deny.is_revoked(payload.jti):
        raise UnauthorizedError("Token has been revoked", code="TOKEN_REVOKED")

    user = await session.get(User, payload.sub)
    if user is None or not user.is_active:
        raise UnauthorizedError("Account not found or disabled", code="ACCOUNT_INVALID")

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: UserRole):
    """Build a dependency that asserts the user holds any of the roles.

    Returns a closure rather than a class instance — FastAPI inspects
    dependency signatures with Pydantic's :class:`TypeAdapter`, which
    chokes on forward-referenced ``Annotated[User, ...]`` aliases on
    bound methods under ``from __future__ import annotations``. The
    function form sidesteps the issue entirely.
    """

    allowed = {r.value for r in roles}

    async def checker(user: User = Depends(get_current_user)) -> User:
        if allowed and user.role not in allowed:
            raise ForbiddenError(
                "You do not have permission to perform this action.",
                code="INSUFFICIENT_ROLE",
            )
        return user

    return checker


def _client_ip(request: Request, x_forwarded_for: str | None) -> str:
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


async def enforce_auth_rate_limit(
    request: Request,
    x_forwarded_for: Annotated[str | None, Header(alias="X-Forwarded-For")] = None,
    limiter: Annotated[RateLimiter, Depends(get_auth_rate_limiter)] = None,  # type: ignore[assignment]
) -> None:
    """Apply a per-client-IP rate limit on ``/auth/*`` endpoints."""

    key = f"auth:{_client_ip(request, x_forwarded_for)}:{request.url.path}"
    allowed = await limiter.acquire(key)
    if not allowed:
        raise RateLimitError(
            "Too many auth attempts — please try again in a minute.",
            code="AUTH_RATE_LIMITED",
        )


__all__ = [
    "CurrentUser",
    "DBSession",
    "enforce_auth_rate_limit",
    "get_current_user",
    "require_roles",
]
