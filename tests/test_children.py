"""End-to-end tests for ``/api/v1/children`` CRUD endpoints."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------- Helpers


async def _register_and_login(client, idx: int = 1, role: str = "parent"):
    creds = {
        "email": f"user{idx}@example.com",
        "password": "Sup3r-Secret!",
        "full_name": f"User {idx}",
        "role": role,
    }
    register = await client.post("/api/v1/auth/register", json=creds)
    assert register.status_code == 201, register.text
    user = register.json()
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": creds["email"], "password": creds["password"]},
    )
    assert login.status_code == 200, login.text
    tokens = login.json()
    return user, {"Authorization": f"Bearer {tokens['access_token']}"}


async def _create_admin_directly(app):
    """Insert an admin user via the ORM (self-signup of admins is blocked)."""

    from app.core.security import hash_password
    from app.database import get_sessionmaker
    from app.models.user import User, UserRole

    factory = get_sessionmaker()
    async with factory() as session:
        admin = User(
            email="admin@example.com",
            password_hash=hash_password("AdminP4ss!"),
            full_name="Admin Root",
            role=UserRole.ADMIN.value,
            is_active=True,
            is_verified=True,
        )
        session.add(admin)
        await session.commit()
        await session.refresh(admin)
        return admin.id


async def _login_as(client, email: str, password: str):
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    tokens = response.json()
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _child_payload(**overrides):
    today = date.today()
    payload = {
        "name": "Aziza",
        "birth_date": (today - timedelta(days=365 * 5)).isoformat(),
        "gender": "female",
        "language": "uz",
    }
    payload.update(overrides)
    return payload


# ------------------------------------------------------------- Tests


async def test_parent_creates_and_lists_their_children(client) -> None:
    _, headers = await _register_and_login(client, idx=1, role="parent")

    create = await client.post(
        "/api/v1/children", json=_child_payload(), headers=headers
    )
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["name"] == "Aziza"
    assert body["gender"] == "female"
    assert body["age_years"] >= 4
    assert body["parent_id"]

    listing = await client.get("/api/v1/children", headers=headers)
    assert listing.status_code == 200, listing.text
    page = listing.json()
    assert page["has_more"] is False
    assert len(page["items"]) == 1
    assert page["items"][0]["id"] == body["id"]


async def test_create_requires_authentication(client) -> None:
    response = await client.post("/api/v1/children", json=_child_payload())
    assert response.status_code == 401


async def test_parent_cannot_set_other_parent_id(client) -> None:
    _, headers_one = await _register_and_login(client, idx=1, role="parent")
    other_user, _ = await _register_and_login(client, idx=2, role="parent")

    response = await client.post(
        "/api/v1/children",
        json=_child_payload(parent_id=other_user["id"]),
        headers=headers_one,
    )
    assert response.status_code == 403
    assert response.json()["code"] == "PARENT_SCOPE_VIOLATION"


async def test_parent_only_sees_their_own_children(client) -> None:
    _, headers_one = await _register_and_login(client, idx=1, role="parent")
    _, headers_two = await _register_and_login(client, idx=2, role="parent")

    created_one = (
        await client.post("/api/v1/children", json=_child_payload(name="One"), headers=headers_one)
    ).json()
    created_two = (
        await client.post("/api/v1/children", json=_child_payload(name="Two"), headers=headers_two)
    ).json()

    listing = await client.get("/api/v1/children", headers=headers_one)
    items = listing.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == created_one["id"]

    # And cross-account reads are blocked.
    forbidden = await client.get(
        f"/api/v1/children/{created_two['id']}", headers=headers_one
    )
    assert forbidden.status_code == 403


async def test_get_returns_404_for_unknown_child(client) -> None:
    _, headers = await _register_and_login(client, idx=1, role="parent")
    response = await client.get(
        "/api/v1/children/00000000-0000-0000-0000-000000000000", headers=headers
    )
    assert response.status_code == 404
    assert response.json()["code"] == "CHILD_NOT_FOUND"


async def test_update_persists_and_recomputes_age(client) -> None:
    _, headers = await _register_and_login(client, idx=1, role="parent")
    child = (
        await client.post("/api/v1/children", json=_child_payload(), headers=headers)
    ).json()

    new_birth = (date.today() - timedelta(days=365 * 8)).isoformat()
    response = await client.put(
        f"/api/v1/children/{child['id']}",
        json={"name": "Aziza Karimova", "birth_date": new_birth, "notes": "Loves animals"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Aziza Karimova"
    assert body["birth_date"] == new_birth
    assert body["notes"] == "Loves animals"
    assert body["age_years"] >= 7


async def test_update_rejects_other_parents_child(client) -> None:
    _, headers_one = await _register_and_login(client, idx=1, role="parent")
    _, headers_two = await _register_and_login(client, idx=2, role="parent")

    child = (
        await client.post("/api/v1/children", json=_child_payload(), headers=headers_one)
    ).json()

    response = await client.put(
        f"/api/v1/children/{child['id']}",
        json={"name": "Hijack"},
        headers=headers_two,
    )
    assert response.status_code == 403


async def test_delete_removes_child(client) -> None:
    _, headers = await _register_and_login(client, idx=1, role="parent")
    child = (
        await client.post("/api/v1/children", json=_child_payload(), headers=headers)
    ).json()

    response = await client.delete(
        f"/api/v1/children/{child['id']}", headers=headers
    )
    assert response.status_code == 204
    assert response.text == ""

    follow_up = await client.get(
        f"/api/v1/children/{child['id']}", headers=headers
    )
    assert follow_up.status_code == 404


async def test_birth_date_in_future_is_rejected(client) -> None:
    _, headers = await _register_and_login(client, idx=1, role="parent")
    future = (date.today() + timedelta(days=10)).isoformat()
    response = await client.post(
        "/api/v1/children",
        json=_child_payload(birth_date=future),
        headers=headers,
    )
    assert response.status_code == 422


async def test_invalid_gender_is_rejected(client) -> None:
    _, headers = await _register_and_login(client, idx=1, role="parent")
    response = await client.post(
        "/api/v1/children",
        json=_child_payload(gender="other"),
        headers=headers,
    )
    assert response.status_code == 422


async def test_admin_can_create_child_for_any_parent(client, app) -> None:
    parent_user, _ = await _register_and_login(client, idx=1, role="parent")
    await _create_admin_directly(app)
    admin_headers = await _login_as(client, "admin@example.com", "AdminP4ss!")

    response = await client.post(
        "/api/v1/children",
        json=_child_payload(parent_id=parent_user["id"]),
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["parent_id"] == parent_user["id"]


async def test_admin_must_supply_parent_id(client, app) -> None:
    await _register_and_login(client, idx=1, role="parent")
    await _create_admin_directly(app)
    admin_headers = await _login_as(client, "admin@example.com", "AdminP4ss!")

    response = await client.post(
        "/api/v1/children", json=_child_payload(), headers=admin_headers
    )
    assert response.status_code == 422
    assert response.json()["code"] == "PARENT_ID_REQUIRED"


async def test_admin_sees_all_children(client, app) -> None:
    _, headers_one = await _register_and_login(client, idx=1, role="parent")
    _, headers_two = await _register_and_login(client, idx=2, role="parent")
    await client.post("/api/v1/children", json=_child_payload(name="One"), headers=headers_one)
    await client.post("/api/v1/children", json=_child_payload(name="Two"), headers=headers_two)

    await _create_admin_directly(app)
    admin_headers = await _login_as(client, "admin@example.com", "AdminP4ss!")

    response = await client.get("/api/v1/children", headers=admin_headers)
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 2


async def test_pagination_returns_cursor_when_more_pages(client) -> None:
    _, headers = await _register_and_login(client, idx=1, role="parent")
    for i in range(5):
        resp = await client.post(
            "/api/v1/children",
            json=_child_payload(name=f"Child {i}"),
            headers=headers,
        )
        assert resp.status_code == 201

    first_page = await client.get(
        "/api/v1/children?limit=2", headers=headers
    )
    assert first_page.status_code == 200
    page1 = first_page.json()
    assert len(page1["items"]) == 2
    assert page1["has_more"] is True
    assert page1["next_cursor"]

    second_page = await client.get(
        f"/api/v1/children?limit=2&cursor={page1['next_cursor']}",
        headers=headers,
    )
    assert second_page.status_code == 200
    page2 = second_page.json()
    assert len(page2["items"]) == 2
    # Items must not overlap.
    page1_ids = {c["id"] for c in page1["items"]}
    page2_ids = {c["id"] for c in page2["items"]}
    assert page1_ids.isdisjoint(page2_ids)


async def test_search_filter_matches_name(client) -> None:
    _, headers = await _register_and_login(client, idx=1, role="parent")
    await client.post("/api/v1/children", json=_child_payload(name="Aziza"), headers=headers)
    await client.post("/api/v1/children", json=_child_payload(name="Bobur"), headers=headers)

    response = await client.get("/api/v1/children?search=azi", headers=headers)
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "Aziza"


async def test_kindergarten_filter_only_matches(client) -> None:
    """Without a kindergarten attached children should be filtered out."""

    _, headers = await _register_and_login(client, idx=1, role="parent")
    await client.post("/api/v1/children", json=_child_payload(name="Aziza"), headers=headers)

    response = await client.get(
        "/api/v1/children?kindergarten_id=does-not-exist", headers=headers
    )
    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_invalid_cursor_returns_422(client) -> None:
    _, headers = await _register_and_login(client, idx=1, role="parent")
    response = await client.get(
        "/api/v1/children?cursor=not-a-real-cursor", headers=headers
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_CURSOR"
