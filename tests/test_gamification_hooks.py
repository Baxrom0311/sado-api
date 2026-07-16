"""Tests for badge catalogue, leaderboard, and completion-hook XP award."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------- Helpers


async def _register_and_login(client, idx: int = 1, role: str = "parent"):
    creds = {
        "email": f"hooks-user{idx}@example.com",
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
        "/api/v1/auth/login", json={"email": email, "password": "AdminP4ss!"}
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


# -------------------------------------------------------- Badge catalogue


async def test_list_badges_returns_active_only_for_non_admin(client) -> None:
    _, parent = await _register_and_login(client, idx=1)
    await _make_admin()
    admin = await _login_admin(client)

    # Two badges: one active, one inactive.
    for code, active in (("active_one", True), ("hidden_one", False)):
        response = await client.post(
            "/api/v1/badges",
            headers=admin,
            json={
                "code": code,
                "title_uz": code,
                "threshold": 10,
                "requirement_type": "xp",
                "is_active": active,
            },
        )
        assert response.status_code == 201

    # Parent only sees the active one.
    parent_view = await client.get("/api/v1/badges", headers=parent)
    assert parent_view.status_code == 200
    codes = {b["code"] for b in parent_view.json()["items"]}
    assert codes == {"active_one"}

    # Admin can include inactive.
    admin_view = await client.get(
        "/api/v1/badges?include_inactive=true", headers=admin
    )
    assert admin_view.status_code == 200
    codes = {b["code"] for b in admin_view.json()["items"]}
    assert codes == {"active_one", "hidden_one"}


async def test_create_badge_rejects_duplicate_code(client) -> None:
    await _make_admin()
    admin = await _login_admin(client)

    payload = {
        "code": "unique_badge",
        "title_uz": "Test",
        "threshold": 5,
        "requirement_type": "xp",
    }
    first = await client.post("/api/v1/badges", headers=admin, json=payload)
    assert first.status_code == 201

    duplicate = await client.post("/api/v1/badges", headers=admin, json=payload)
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "BADGE_DUPLICATE"


async def test_update_and_delete_badge_admin_only(client) -> None:
    _, parent = await _register_and_login(client, idx=1)
    await _make_admin()
    admin = await _login_admin(client)

    create = await client.post(
        "/api/v1/badges",
        headers=admin,
        json={
            "code": "to_edit",
            "title_uz": "Boshlang'ich",
            "threshold": 5,
            "requirement_type": "xp",
        },
    )
    badge_id = create.json()["id"]

    # Parent cannot edit.
    forbidden = await client.put(
        f"/api/v1/badges/{badge_id}",
        headers=parent,
        json={"title_uz": "yangi"},
    )
    assert forbidden.status_code == 403

    # Admin can.
    update = await client.put(
        f"/api/v1/badges/{badge_id}",
        headers=admin,
        json={"title_uz": "Yangi nom", "threshold": 25},
    )
    assert update.status_code == 200
    assert update.json()["title_uz"] == "Yangi nom"
    assert update.json()["threshold"] == 25

    # Parent cannot delete.
    forbidden_delete = await client.delete(
        f"/api/v1/badges/{badge_id}", headers=parent
    )
    assert forbidden_delete.status_code == 403

    # Admin deletes successfully.
    delete = await client.delete(f"/api/v1/badges/{badge_id}", headers=admin)
    assert delete.status_code == 204

    missing = await client.get(f"/api/v1/badges/{badge_id}", headers=admin)
    assert missing.status_code == 404


# ------------------------------------------------------ Exercise hook


async def test_exercise_completion_awards_xp_and_streak(client) -> None:
    """Completing an exercise mutates the gamification record."""

    _, parent = await _register_and_login(client, idx=1)
    child_id = await _create_child(client, parent)

    await _make_admin()
    admin = await _login_admin(client)

    # Therapists / admins can create exercises.
    exercise = await client.post(
        "/api/v1/exercises",
        headers=admin,
        json={
            "title": "Test mashq",
            "category": "articulation",
            "age_group": "4-5",
            "difficulty": "easy",
            "language": "uz",
            "duration_minutes": 5,
        },
    )
    assert exercise.status_code == 201, exercise.text
    exercise_id = exercise.json()["id"]

    assign = await client.post(
        f"/api/v1/exercises/{child_id}/assign",
        headers=parent,
        json={"exercise_id": exercise_id},
    )
    assert assign.status_code == 201, assign.text
    assignment_id = assign.json()["id"]

    # Complete with a perfect score → 10 base + 10 bonus = 20 XP.
    complete = await client.put(
        f"/api/v1/exercises/assignments/{assignment_id}/complete",
        headers=parent,
        json={"score": 100, "notes": "Zo'r natija"},
    )
    assert complete.status_code == 200, complete.text

    status = await client.get(
        f"/api/v1/children/{child_id}/gamification", headers=parent
    )
    assert status.status_code == 200
    body = status.json()
    assert body["total_xp"] == 20
    assert body["total_exercises_completed"] == 1
    assert body["streak_days"] == 1
    assert body["level"] == 1


async def test_exercise_completion_idempotent_on_repeat_calls(client) -> None:
    """Re-completing the same assignment must not double-award XP."""

    _, parent = await _register_and_login(client, idx=1)
    child_id = await _create_child(client, parent)
    await _make_admin()
    admin = await _login_admin(client)

    exercise = await client.post(
        "/api/v1/exercises",
        headers=admin,
        json={
            "title": "Once mashq",
            "category": "articulation",
            "age_group": "4-5",
            "difficulty": "easy",
        },
    )
    exercise_id = exercise.json()["id"]
    assign = await client.post(
        f"/api/v1/exercises/{child_id}/assign",
        headers=parent,
        json={"exercise_id": exercise_id},
    )
    assignment_id = assign.json()["id"]

    for _ in range(3):
        response = await client.put(
            f"/api/v1/exercises/assignments/{assignment_id}/complete",
            headers=parent,
            json={"score": 50},
        )
        assert response.status_code == 200

    status = await client.get(
        f"/api/v1/children/{child_id}/gamification", headers=parent
    )
    body = status.json()
    # First complete awards 10 + 5 = 15 XP. Re-completes are no-ops.
    assert body["total_xp"] == 15
    assert body["total_exercises_completed"] == 1


# ------------------------------------------------------ Leaderboard


async def test_family_leaderboard_orders_by_xp(client) -> None:
    """A parent's leaderboard ranks their children by total XP."""

    _, parent = await _register_and_login(client, idx=1)
    child_a = await _create_child(client, parent, name="Ali")
    child_b = await _create_child(client, parent, name="Bek")

    await _make_admin()
    admin = await _login_admin(client)

    await client.post(
        f"/api/v1/children/{child_a}/gamification/award",
        headers=admin,
        json={"amount": 200, "reason": "demo"},
    )
    await client.post(
        f"/api/v1/children/{child_b}/gamification/award",
        headers=admin,
        json={"amount": 50, "reason": "demo"},
    )

    response = await client.get(
        f"/api/v1/children/{child_a}/gamification/leaderboard?scope=family",
        headers=parent,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"] == "family"
    assert [e["child_id"] for e in body["entries"]] == [child_a, child_b]
    assert body["entries"][0]["rank"] == 1
    assert body["entries"][0]["total_xp"] == 200
    assert body["entries"][1]["total_xp"] == 50


async def test_global_leaderboard_forbidden_for_parent(client) -> None:
    _, parent = await _register_and_login(client, idx=1)
    child_id = await _create_child(client, parent)

    response = await client.get(
        f"/api/v1/children/{child_id}/gamification/leaderboard?scope=global",
        headers=parent,
    )
    assert response.status_code == 403
