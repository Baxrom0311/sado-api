"""Tests for the admin user-listing endpoints."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def _register_and_login(client, idx: int = 1, role: str = "parent"):
    creds = {
        "email": f"user{idx}@example.com",
        "password": "Sup3r-Secret!",
        "full_name": f"User {idx}",
        "role": role,
    }
    register = await client.post("/api/v1/auth/register", json=creds)
    assert register.status_code == 201, register.text
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": creds["email"], "password": creds["password"]},
    )
    assert login.status_code == 200, login.text
    tokens = login.json()
    return register.json(), {"Authorization": f"Bearer {tokens['access_token']}"}


async def _seed_user(app, *, email: str, role: str, full_name: str | None = None):
    """Insert a user (any role) directly via the ORM."""

    from app.core.security import hash_password
    from app.database import get_sessionmaker
    from app.models.user import User

    factory = get_sessionmaker()
    async with factory() as session:
        user = User(
            email=email,
            password_hash=hash_password("Sup3r-Secret!"),
            full_name=full_name or email.split("@")[0],
            role=role,
            is_active=True,
            is_verified=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user.id


async def _create_admin(app, email: str = "admin@example.com"):
    return await _seed_user(app, email=email, role="admin", full_name="Admin Root")


async def _login_admin(client, email: str = "admin@example.com"):
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "Sup3r-Secret!"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_users_list_requires_admin(client, app) -> None:
    _, parent_h = await _register_and_login(client, idx=1)

    response = await client.get("/api/v1/users", headers=parent_h)
    assert response.status_code == 403


async def test_users_list_returns_paginated_results(client, app) -> None:
    # Seed several users — therapist/admin must be created via ORM.
    await _register_and_login(client, idx=1)
    await _register_and_login(client, idx=2, role="teacher")
    await _seed_user(app, email="therapist@example.com", role="therapist")

    await _create_admin(app)
    admin_h = await _login_admin(client)

    response = await client.get("/api/v1/users?limit=2", headers=admin_h)
    assert response.status_code == 200, response.text
    page = response.json()
    assert "items" in page
    assert isinstance(page["items"], list)
    assert len(page["items"]) <= 2
    # 4 users seeded total → there must be more results.
    assert page["has_more"] is True
    assert page["next_cursor"] is not None

    # Check public schema does not leak the password hash.
    for u in page["items"]:
        assert "password_hash" not in u
        assert {"id", "role", "is_active", "full_name"} <= set(u)


async def test_users_list_filters_by_role(client, app) -> None:
    await _register_and_login(client, idx=1)
    await _register_and_login(client, idx=2, role="teacher")
    await _seed_user(app, email="therapist@example.com", role="therapist")

    await _create_admin(app)
    admin_h = await _login_admin(client)

    response = await client.get(
        "/api/v1/users?role=teacher", headers=admin_h
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["role"] == "teacher"


async def test_users_list_search(client, app) -> None:
    await _register_and_login(client, idx=1)
    await _register_and_login(client, idx=2)
    await _create_admin(app)
    admin_h = await _login_admin(client)

    response = await client.get(
        "/api/v1/users?search=user2", headers=admin_h
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) >= 1
    assert all("user2" in (u["email"] or "") for u in items)


async def test_users_get_one(client, app) -> None:
    user, _ = await _register_and_login(client, idx=1)
    await _create_admin(app)
    admin_h = await _login_admin(client)

    response = await client.get(
        f"/api/v1/users/{user['id']}", headers=admin_h
    )
    assert response.status_code == 200
    assert response.json()["id"] == user["id"]


async def test_users_set_active(client, app) -> None:
    user, _ = await _register_and_login(client, idx=1)
    await _create_admin(app)
    admin_h = await _login_admin(client)

    deactivate = await client.put(
        f"/api/v1/users/{user['id']}/active?is_active=false",
        headers=admin_h,
    )
    assert deactivate.status_code == 200, deactivate.text
    assert deactivate.json()["is_active"] is False


async def test_admin_cannot_self_deactivate(client, app) -> None:
    admin_id = await _create_admin(app)
    admin_h = await _login_admin(client)

    response = await client.put(
        f"/api/v1/users/{admin_id}/active?is_active=false",
        headers=admin_h,
    )
    # ValidationError → 422 in this codebase, with a SELF_DEACTIVATION code.
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "SELF_DEACTIVATION"



async def test_admin_can_create_user_with_any_role(client, app) -> None:
    """``POST /users`` lets an admin provision staff accounts.

    Unlike ``/auth/register`` which forbids admin/therapist roles,
    the admin endpoint must allow them.
    """

    await _create_admin(app)
    admin_h = await _login_admin(client)

    payload = {
        "email": "new.therapist@sado.uz",
        "password": "Sup3r-Secret!",
        "full_name": "Created Therapist",
        "role": "therapist",
        "language": "ru",
    }
    response = await client.post("/api/v1/users", json=payload, headers=admin_h)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["email"] == "new.therapist@sado.uz"
    assert body["role"] == "therapist"
    assert body["language"] == "ru"
    assert body["is_active"] is True


async def test_admin_create_user_rejects_duplicate_email(client, app) -> None:
    user, _ = await _register_and_login(client, idx=1)
    await _create_admin(app)
    admin_h = await _login_admin(client)

    response = await client.post(
        "/api/v1/users",
        json={
            "email": user["email"],
            "password": "Sup3r-Secret!",
            "full_name": "Duplicate",
            "role": "parent",
        },
        headers=admin_h,
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "USER_EXISTS"


async def test_admin_create_user_requires_email_or_phone(client, app) -> None:
    await _create_admin(app)
    admin_h = await _login_admin(client)

    response = await client.post(
        "/api/v1/users",
        json={
            "password": "Sup3r-Secret!",
            "full_name": "No Identifier",
            "role": "parent",
        },
        headers=admin_h,
    )
    # Pydantic validation rejects the payload before it reaches the handler.
    assert response.status_code == 422, response.text


async def test_non_admin_cannot_create_user(client, app) -> None:
    _, parent_h = await _register_and_login(client, idx=1)

    response = await client.post(
        "/api/v1/users",
        json={
            "email": "another@example.com",
            "password": "Sup3r-Secret!",
            "full_name": "Another",
            "role": "parent",
        },
        headers=parent_h,
    )
    assert response.status_code == 403


async def test_admin_create_user_with_phone_only(client, app) -> None:
    await _create_admin(app)
    admin_h = await _login_admin(client)

    response = await client.post(
        "/api/v1/users",
        json={
            "phone": "+998901112233",
            "password": "Sup3r-Secret!",
            "full_name": "Phone User",
            "role": "teacher",
        },
        headers=admin_h,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["phone"] == "+998901112233"
    assert body["email"] is None
    assert body["role"] == "teacher"
