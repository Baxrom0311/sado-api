"""Security primitives — password hashing and JWT token handling.

Why we don't use ``passlib.CryptContext`` directly: passlib's bcrypt
backend reads ``bcrypt.__about__.__version__``, which was removed in
``bcrypt>=4.1``. To stay compatible with current bcrypt wheels we wrap
the stdlib-friendly :mod:`bcrypt` module manually.

JWT helpers produce two token types:

* ``access`` — short lived (default 15 min), used for API auth.
* ``refresh`` — long lived (default 7 days), used to mint new access
  tokens. The token type is stamped in the payload so a refresh token
  can never be accepted as an access token (and vice versa).
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Literal

import bcrypt
from jose import JWTError, jwt
from pydantic import BaseModel

from app.config import get_settings
from app.core.exceptions import UnauthorizedError


class TokenType(str, Enum):
    ACCESS = "access"
    REFRESH = "refresh"


class TokenPayload(BaseModel):
    sub: str
    role: str
    type: Literal["access", "refresh"]
    jti: str
    iat: int
    exp: int


# ---------------------------------------------------------------- Hashing


def hash_password(password: str) -> str:
    """Return a bcrypt hash for the given plaintext password.

    Bcrypt has a hard 72-byte limit on input; we truncate explicitly so
    the failure mode is predictable rather than backend-dependent.
    """

    if not password:
        raise ValueError("Password must not be empty")
    pw_bytes = password.encode("utf-8")[:72]
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(pw_bytes, salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time comparison of plaintext against a bcrypt hash."""

    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(
            password.encode("utf-8")[:72],
            password_hash.encode("utf-8"),
        )
    except (ValueError, TypeError):
        return False


# -------------------------------------------------------------------- JWT


def _now() -> datetime:
    return datetime.now(UTC)


def _expiry_for(token_type: TokenType) -> datetime:
    settings = get_settings()
    if token_type is TokenType.ACCESS:
        return _now() + timedelta(minutes=settings.access_token_expires_min)
    return _now() + timedelta(days=settings.refresh_token_expires_days)


def create_token(
    *,
    subject: str,
    role: str,
    token_type: TokenType,
    jti: str | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, TokenPayload]:
    """Create a signed JWT and return ``(token, payload)``."""

    settings = get_settings()
    issued = _now()
    expires = _expiry_for(token_type)
    claims: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "type": token_type.value,
        "jti": jti or secrets.token_urlsafe(16),
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
    }
    if extra_claims:
        # Don't let callers overwrite reserved claims.
        for key, value in extra_claims.items():
            claims.setdefault(key, value)

    token = jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    payload = TokenPayload.model_validate(claims)
    return token, payload


def create_access_token(*, subject: str, role: str) -> tuple[str, TokenPayload]:
    return create_token(subject=subject, role=role, token_type=TokenType.ACCESS)


def create_refresh_token(*, subject: str, role: str) -> tuple[str, TokenPayload]:
    return create_token(subject=subject, role=role, token_type=TokenType.REFRESH)


def decode_token(token: str, *, expected_type: TokenType | None = None) -> TokenPayload:
    """Decode and validate a JWT.

    Raises :class:`UnauthorizedError` for any failure (expired, bad
    signature, wrong type) so the API can return a single 401 mapping.
    """

    settings = get_settings()
    try:
        raw = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub", "type"]},
        )
    except JWTError as exc:
        raise UnauthorizedError(
            "Invalid or expired token", code="INVALID_TOKEN"
        ) from exc

    try:
        payload = TokenPayload.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        raise UnauthorizedError(
            "Token payload is malformed", code="INVALID_TOKEN"
        ) from exc

    if expected_type is not None and payload.type != expected_type.value:
        raise UnauthorizedError(
            f"Expected {expected_type.value} token but got {payload.type}",
            code="WRONG_TOKEN_TYPE",
        )

    return payload


__all__ = [
    "TokenPayload",
    "TokenType",
    "create_access_token",
    "create_refresh_token",
    "create_token",
    "decode_token",
    "hash_password",
    "verify_password",
]
