"""Authentication business logic.

The service encapsulates all of the auth flows so endpoint handlers
are thin glue between Pydantic schemas and SQLAlchemy.

Token revocation uses an in-memory deny-list (jti) keyed by token ID
with TTL = remaining lifetime. In production this should be backed by
Redis; the in-memory implementation is enough for tests and local dev.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.security import (
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.user import User, UserLanguage, UserRole
from app.schemas.auth import LoginRequest, RegisterRequest

# ----------------------------------------------------------------- Revocation


class _TokenDenyList:
    """A tiny in-process jti deny-list with TTL eviction.

    Concurrency-safe via an asyncio lock; survives only in-process. For
    multi-worker deployments this should be replaced with Redis SETEX.
    """

    def __init__(self) -> None:
        self._items: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def revoke(self, jti: str, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        expiry = time.time() + ttl_seconds
        async with self._lock:
            self._items[jti] = expiry
            self._evict_expired_locked()

    async def is_revoked(self, jti: str) -> bool:
        async with self._lock:
            self._evict_expired_locked()
            return jti in self._items

    def _evict_expired_locked(self) -> None:
        now = time.time()
        expired = [k for k, v in self._items.items() if v <= now]
        for key in expired:
            self._items.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._items.clear()


_deny_list = _TokenDenyList()


def get_deny_list() -> _TokenDenyList:
    return _deny_list


# ----------------------------------------------------------------- Service


@dataclass(slots=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int


class AuthService:
    """Stateless wrapper that takes an :class:`AsyncSession` per call."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ----- Registration --------------------------------------------------

    async def register(self, payload: RegisterRequest) -> User:
        existing = await self._find_by_identifier(payload.email, payload.phone)
        if existing is not None:
            raise ConflictError(
                "An account with that email or phone already exists.",
                code="USER_EXISTS",
            )

        user = User(
            email=payload.email,
            phone=payload.phone,
            password_hash=hash_password(payload.password),
            full_name=payload.full_name,
            role=payload.role.value if isinstance(payload.role, UserRole) else str(payload.role),
            language=(
                payload.language.value
                if isinstance(payload.language, UserLanguage)
                else str(payload.language)
            ),
            is_active=True,
            is_verified=False,
        )
        self.session.add(user)
        try:
            await self.session.commit()
        except IntegrityError as exc:  # pragma: no cover - race on unique
            await self.session.rollback()
            raise ConflictError(
                "An account with that email or phone already exists.",
                code="USER_EXISTS",
            ) from exc
        await self.session.refresh(user)
        return user

    # ----- Login ---------------------------------------------------------

    async def authenticate(self, payload: LoginRequest) -> User:
        user = await self._find_by_identifier(payload.email, payload.phone)
        if user is None or not verify_password(payload.password, user.password_hash):
            # Use the same message regardless to avoid user enumeration.
            raise UnauthorizedError(
                "Invalid credentials.", code="INVALID_CREDENTIALS"
            )
        if not user.is_active:
            raise UnauthorizedError(
                "This account has been disabled.", code="ACCOUNT_DISABLED"
            )
        return user

    # ----- Tokens --------------------------------------------------------

    def issue_tokens(self, user: User) -> IssuedTokens:
        settings = get_settings()
        access, _ = create_access_token(subject=user.id, role=user.role)
        refresh, _ = create_refresh_token(subject=user.id, role=user.role)
        return IssuedTokens(
            access_token=access,
            refresh_token=refresh,
            expires_in=settings.access_token_expires_min * 60,
        )

    async def refresh_tokens(self, refresh_token: str) -> tuple[User, IssuedTokens]:
        payload = decode_token(refresh_token, expected_type=TokenType.REFRESH)
        if await _deny_list.is_revoked(payload.jti):
            raise UnauthorizedError(
                "Refresh token has been revoked.", code="TOKEN_REVOKED"
            )

        user = await self.session.get(User, payload.sub)
        if user is None or not user.is_active:
            raise UnauthorizedError("Account not found or disabled.", code="ACCOUNT_INVALID")

        # Rotate: revoke old refresh jti and mint a fresh pair.
        ttl = max(0, payload.exp - int(time.time()))
        await _deny_list.revoke(payload.jti, ttl_seconds=ttl)

        return user, self.issue_tokens(user)

    async def revoke_token(self, token: str) -> None:
        """Revoke a token by its jti. Accepts both access and refresh."""

        payload = decode_token(token)
        ttl = max(0, payload.exp - int(time.time()))
        await _deny_list.revoke(payload.jti, ttl_seconds=ttl)

    # ----- Helpers -------------------------------------------------------

    async def _find_by_identifier(
        self, email: str | None, phone: str | None
    ) -> User | None:
        if not email and not phone:
            return None
        stmt = select(User)
        clauses = []
        if email:
            clauses.append(User.email == email)
        if phone:
            clauses.append(User.phone == phone)
        stmt = stmt.where(or_(*clauses))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
