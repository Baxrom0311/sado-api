"""End-to-end tests for ``/api/v1/auth/*`` and ``/api/v1/users/me``."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------- Helpers


def _credentials(idx: int = 1) -> dict[str, str]:
    return {
        "email": f"parent{idx}@example.com",
        "password": "Sup3r-Secret!",
        "full_name": f"Parent {idx}",
    }


async def _register(client, idx: int = 1, **overrides):  # type: ignore[no-untyped-def]
    payload = {**_credentials(idx), **overrides}
    return await client.post("/api/v1/auth/register", json=payload)


async def _login(client, idx: int = 1):  # type: ignore[no-untyped-def]
    creds = _credentials(idx)
    return await client.post(
        "/api/v1/auth/login",
        json={"email": creds["email"], "password": creds["password"]},
    )


# ------------------------------------------------------------- Tests


async def test_register_creates_user_and_returns_public_payload(client) -> None:
    response = await _register(client, idx=1)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["email"] == "parent1@example.com"
    assert body["full_name"] == "Parent 1"
    assert body["role"] == "parent"
    assert body["language"] == "uz"
    assert "password_hash" not in body
    assert "id" in body


async def test_register_rejects_duplicate(client) -> None:
    first = await _register(client, idx=1)
    assert first.status_code == 201
    duplicate = await _register(client, idx=1)
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "USER_EXISTS"


async def test_register_rejects_admin_self_signup(client) -> None:
    response = await _register(client, idx=1, role="admin")
    assert response.status_code == 422


async def test_register_requires_email_or_phone(client) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json={"password": "Sup3r-Secret!", "full_name": "Nobody"},
    )
    assert response.status_code == 422


async def test_register_normalises_phone_and_logs_in_with_it(client) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "phone": "+998 90 123-45-67",
            "password": "Sup3r-Secret!",
            "full_name": "Phone User",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["phone"] == "+998901234567"

    login = await client.post(
        "/api/v1/auth/login",
        json={"phone": "+998901234567", "password": "Sup3r-Secret!"},
    )
    assert login.status_code == 200


async def test_login_returns_token_pair(client) -> None:
    await _register(client, idx=1)
    response = await _login(client, idx=1)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["access_token"] != body["refresh_token"]


async def test_login_rejects_bad_password(client) -> None:
    await _register(client, idx=1)
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "parent1@example.com", "password": "wrong-password"},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_CREDENTIALS"


async def test_login_rejects_unknown_user(client) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "ghost@example.com", "password": "Sup3r-Secret!"},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_CREDENTIALS"


async def test_refresh_rotates_tokens_and_revokes_old(client) -> None:
    await _register(client, idx=1)
    login = (await _login(client, idx=1)).json()

    refreshed = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": login["refresh_token"]}
    )
    assert refreshed.status_code == 200, refreshed.text
    new_pair = refreshed.json()
    assert new_pair["access_token"] != login["access_token"]
    assert new_pair["refresh_token"] != login["refresh_token"]

    # The old refresh token should now be revoked.
    replay = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": login["refresh_token"]}
    )
    assert replay.status_code == 401
    assert replay.json()["code"] == "TOKEN_REVOKED"


async def test_refresh_rejects_access_token(client) -> None:
    await _register(client, idx=1)
    pair = (await _login(client, idx=1)).json()

    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": pair["access_token"]}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "WRONG_TOKEN_TYPE"


async def test_logout_revokes_refresh_token(client) -> None:
    await _register(client, idx=1)
    pair = (await _login(client, idx=1)).json()
    headers = {"Authorization": f"Bearer {pair['access_token']}"}

    response = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": pair["refresh_token"]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["detail"]

    # That refresh token can no longer be used.
    replay = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert replay.status_code == 401


async def test_logout_requires_authentication(client) -> None:
    await _register(client, idx=1)
    pair = (await _login(client, idx=1)).json()

    response = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": pair["refresh_token"]},
    )
    assert response.status_code == 401


async def test_users_me_returns_current_profile(client) -> None:
    await _register(client, idx=1)
    pair = (await _login(client, idx=1)).json()
    headers = {"Authorization": f"Bearer {pair['access_token']}"}

    response = await client.get("/api/v1/users/me", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == "parent1@example.com"
    assert body["role"] == "parent"


async def test_users_me_requires_bearer_token(client) -> None:
    response = await client.get("/api/v1/users/me")
    assert response.status_code == 401
    assert response.json()["code"] in {"MISSING_TOKEN", "UNAUTHORIZED"}


async def test_users_me_rejects_garbage_token(client) -> None:
    response = await client.get(
        "/api/v1/users/me", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert response.status_code == 401
    assert response.json()["code"] in {"INVALID_TOKEN", "UNAUTHORIZED"}


async def test_update_me_persists_changes(client) -> None:
    await _register(client, idx=1)
    pair = (await _login(client, idx=1)).json()
    headers = {"Authorization": f"Bearer {pair['access_token']}"}

    response = await client.put(
        "/api/v1/users/me",
        json={"full_name": "New Name", "language": "ru"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["full_name"] == "New Name"
    assert response.json()["language"] == "ru"

    again = await client.get("/api/v1/users/me", headers=headers)
    assert again.json()["full_name"] == "New Name"
    assert again.json()["language"] == "ru"


async def test_update_me_rejects_email_collision(client) -> None:
    await _register(client, idx=1)
    await _register(client, idx=2)
    pair = (await _login(client, idx=1)).json()
    headers = {"Authorization": f"Bearer {pair['access_token']}"}

    response = await client.put(
        "/api/v1/users/me",
        json={"email": "parent2@example.com"},
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "EMAIL_TAKEN"


async def test_auth_rate_limit_returns_429(monkeypatch, client) -> None:
    # Reload settings with a tight rate limit and reset the limiter.
    monkeypatch.setenv("RATE_LIMIT_AUTH_PER_MINUTE", "2")

    from app.config import get_settings
    from app.core.rate_limit import reset_auth_rate_limiter

    get_settings.cache_clear()
    await reset_auth_rate_limiter()

    # Burn the allowance with bad logins to avoid touching the DB.
    payload = {"email": "ghost@example.com", "password": "Sup3r-Secret!"}
    statuses = []
    for _ in range(5):
        resp = await client.post("/api/v1/auth/login", json=payload)
        statuses.append(resp.status_code)

    assert 429 in statuses
    # Reset for the next test.
    monkeypatch.setenv("RATE_LIMIT_AUTH_PER_MINUTE", "1000")
    get_settings.cache_clear()
    await reset_auth_rate_limiter()
    # Make ruff happy on unused import warnings in CI.
    assert os.environ["RATE_LIMIT_AUTH_PER_MINUTE"] == "1000"
