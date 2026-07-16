"""Integration tests for the ``/api/v1/gamification`` HTTP endpoints."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------- Helpers


async def _register_and_login(client, idx: int = 1, role: str = "parent"):
    creds = {
        "email": f"gam-user{idx}@example.com",
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
    return user, {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _make_admin(email: str = "admin@example.com"):
    """Insert an admin via the ORM and return its id."""

    from app.core.security import hash_password
    from app.database import get_sessionmaker
    from app.models.user import User, UserRole

    factory = get_sessionmaker()
    async with factory() as session:
        admin = User(
            email=email,
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


async def _login_admin(client, email: str = "admin@example.com"):
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "AdminP4ss!"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _child_payload(name: str = "Aziza"):
    today = date.today()
    return {
        "name": name,
        "birth_date": (today - timedelta(days=365 * 5)).isoformat(),
        "gender": "female",
        "language": "uz",
    }


async def _create_child(client, headers, name: str = "Aziza") -> str:
    response = await client.post(
        "/api/v1/children", json=_child_payload(name), headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


# ---------------------------------------------------------- Status endpoint


async def test_get_gamification_creates_default_record(client) -> None:
    """First call lazily inserts a fresh gamification row at level 1."""

    _, headers = await _register_and_login(client, idx=1)
    child_id = await _create_child(client, headers)

    response = await client.get(
        f"/api/v1/children/{child_id}/gamification", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["child_id"] == child_id
    assert body["total_xp"] == 0
    assert body["level"] == 1
    assert body["xp_into_level"] == 0
    assert body["xp_for_next_level"] == 100
    assert body["streak_days"] == 0
    assert body["badges_earned"] == 0


async def test_get_gamification_forbidden_for_other_parent(client) -> None:
    """Parents may only view their own children's gamification."""

    _, parent_a = await _register_and_login(client, idx=1)
    child_id = await _create_child(client, parent_a)
    _, parent_b = await _register_and_login(client, idx=2)

    response = await client.get(
        f"/api/v1/children/{child_id}/gamification", headers=parent_b
    )
    assert response.status_code == 403


async def test_get_gamification_404_for_missing_child(client) -> None:
    _, headers = await _register_and_login(client, idx=1)
    response = await client.get(
        "/api/v1/children/00000000-0000-0000-0000-000000000000/gamification",
        headers=headers,
    )
    assert response.status_code == 404


# ----------------------------------------------------------- Admin XP award


async def test_admin_can_award_xp_and_unlocks_badge(client, app) -> None:
    """A 100 XP award levels the child up to 2 and unlocks the 100 XP badge."""

    _, parent = await _register_and_login(client, idx=1)
    child_id = await _create_child(client, parent)

    await _make_admin()
    admin_headers = await _login_admin(client)

    # Seed a couple of badges (mimicking the real catalogue).
    badge_ids: list[str] = []
    for code, threshold, rtype in (
        ("xp_100", 100, "xp"),
        ("level_2", 2, "level"),
    ):
        response = await client.post(
            "/api/v1/badges",
            headers=admin_headers,
            json={
                "code": code,
                "title_uz": code,
                "threshold": threshold,
                "requirement_type": rtype,
            },
        )
        assert response.status_code == 201, response.text
        badge_ids.append(response.json()["id"])

    award = await client.post(
        f"/api/v1/children/{child_id}/gamification/award",
        headers=admin_headers,
        json={"amount": 100, "reason": "stellar week"},
    )
    assert award.status_code == 200, award.text
    body = award.json()
    assert body["xp_added"] == 100
    assert body["leveled_up"] is True
    assert body["previous_level"] == 1
    assert body["new_level"] == 2
    earned_codes = {b["code"] for b in body["newly_earned_badges"]}
    assert earned_codes == {"xp_100", "level_2"}
    assert body["gamification"]["total_xp"] == 100
    assert body["gamification"]["level"] == 2
    assert body["gamification"]["badges_earned"] == 2


async def test_non_admin_cannot_award_xp(client) -> None:
    _, parent = await _register_and_login(client, idx=1)
    child_id = await _create_child(client, parent)

    response = await client.post(
        f"/api/v1/children/{child_id}/gamification/award",
        headers=parent,
        json={"amount": 50, "reason": "self-award"},
    )
    assert response.status_code == 403


async def test_award_xp_validates_payload(client) -> None:
    _, parent = await _register_and_login(client, idx=1)
    child_id = await _create_child(client, parent)
    await _make_admin()
    admin_headers = await _login_admin(client)

    response = await client.post(
        f"/api/v1/children/{child_id}/gamification/award",
        headers=admin_headers,
        json={"amount": 0, "reason": "noop"},
    )
    assert response.status_code == 422


# ----------------------------------------------------------- Earned badges


async def test_list_earned_badges_returns_unlocks(client) -> None:
    _, parent = await _register_and_login(client, idx=1)
    child_id = await _create_child(client, parent)
    await _make_admin()
    admin_headers = await _login_admin(client)

    create_badge = await client.post(
        "/api/v1/badges",
        headers=admin_headers,
        json={
            "code": "first_steps",
            "title_uz": "Birinchi qadamlar",
            "threshold": 10,
            "requirement_type": "xp",
            "icon": "👣",
        },
    )
    assert create_badge.status_code == 201

    await client.post(
        f"/api/v1/children/{child_id}/gamification/award",
        headers=admin_headers,
        json={"amount": 10, "reason": "kickoff"},
    )

    response = await client.get(
        f"/api/v1/children/{child_id}/gamification/badges", headers=parent
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload) == 1
    item = payload[0]
    assert item["badge"]["code"] == "first_steps"
    assert item["badge"]["icon"] == "👣"
    assert item["earned_at"]
