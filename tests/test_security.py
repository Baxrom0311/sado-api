"""Tests for ``app.core.security`` (hashing + JWT)."""

from __future__ import annotations

from datetime import UTC, timedelta

import pytest
from jose import jwt

from app.config import get_settings
from app.core.exceptions import UnauthorizedError
from app.core.security import (
    TokenType,
    create_access_token,
    create_refresh_token,
    create_token,
    decode_token,
    hash_password,
    verify_password,
)


def test_hash_password_round_trip() -> None:
    pw = "S3cur3-pass-Сало!"
    hashed = hash_password(pw)
    assert hashed != pw
    assert verify_password(pw, hashed)
    assert not verify_password("wrong", hashed)


def test_hash_password_rejects_empty() -> None:
    with pytest.raises(ValueError):
        hash_password("")


def test_verify_password_handles_garbage() -> None:
    assert not verify_password("anything", "not-a-real-hash")
    assert not verify_password("", "$2b$12$abcdefghijklmnopqrstuv")


def test_access_and_refresh_tokens_have_distinct_types() -> None:
    access, access_payload = create_access_token(subject="user-1", role="parent")
    refresh, refresh_payload = create_refresh_token(subject="user-1", role="parent")

    assert access_payload.type == TokenType.ACCESS.value
    assert refresh_payload.type == TokenType.REFRESH.value
    assert access != refresh
    assert access_payload.jti != refresh_payload.jti


def test_decode_access_token_round_trip() -> None:
    token, payload = create_access_token(subject="user-42", role="admin")
    decoded = decode_token(token, expected_type=TokenType.ACCESS)
    assert decoded.sub == "user-42"
    assert decoded.role == "admin"
    assert decoded.jti == payload.jti


def test_decode_rejects_wrong_token_type() -> None:
    refresh, _ = create_refresh_token(subject="u", role="parent")
    with pytest.raises(UnauthorizedError):
        decode_token(refresh, expected_type=TokenType.ACCESS)


def test_decode_rejects_garbage() -> None:
    with pytest.raises(UnauthorizedError):
        decode_token("not.a.jwt")


def test_decode_rejects_expired_token() -> None:
    settings = get_settings()
    # Manually craft an already-expired token signed with the same key.
    from datetime import datetime

    issued = datetime.now(UTC) - timedelta(hours=2)
    expired_at = issued + timedelta(minutes=1)
    claims = {
        "sub": "u",
        "role": "parent",
        "type": TokenType.ACCESS.value,
        "jti": "x",
        "iat": int(issued.timestamp()),
        "exp": int(expired_at.timestamp()),
    }
    token = jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    with pytest.raises(UnauthorizedError):
        decode_token(token, expected_type=TokenType.ACCESS)


def test_create_token_does_not_allow_overriding_reserved_claims() -> None:
    token, payload = create_token(
        subject="u",
        role="parent",
        token_type=TokenType.ACCESS,
        extra_claims={"sub": "hacker", "role": "admin", "custom": "ok"},
    )
    decoded = decode_token(token)
    assert decoded.sub == "u"
    assert decoded.role == "parent"
    # Custom claim survives.
    settings = get_settings()
    raw = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    assert raw["custom"] == "ok"
