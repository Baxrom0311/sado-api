"""Tests for /stats endpoints (system + regional + per-kindergarten)."""

from __future__ import annotations

import io
from datetime import date

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------- Helpers


async def _register_login(
    client, email: str, role: str = "parent"
) -> tuple[dict, dict]:
    creds = {
        "email": email,
        "password": "Sup3r-Secret!",
        "full_name": "Test User",
        "role": role,
    }
    register = await client.post("/api/v1/auth/register", json=creds)
    assert register.status_code == 201, register.text
    user = register.json()
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": creds["password"]},
    )
    assert login.status_code == 200, login.text
    return user, {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _create_admin(email: str = "admin@sado.uz") -> str:
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


async def _admin_headers(client, email: str = "admin@sado.uz") -> dict:
    await _create_admin(email)
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "AdminP4ss!"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _create_therapist(client, email: str) -> dict:
    """Therapists cannot self-register — create directly via the DB
    to mimic an admin provisioning the account."""

    from app.core.security import hash_password
    from app.database import get_sessionmaker
    from app.models.user import User, UserRole

    factory = get_sessionmaker()
    async with factory() as session:
        therapist = User(
            email=email,
            password_hash=hash_password("Sup3r-Secret!"),
            full_name="Speech Therapist",
            role=UserRole.THERAPIST.value,
            is_active=True,
            is_verified=True,
        )
        session.add(therapist)
        await session.commit()
        await session.refresh(therapist)
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "Sup3r-Secret!"},
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _create_child(client, headers: dict, name: str = "Aziz") -> str:
    response = await client.post(
        "/api/v1/children",
        json={
            "name": name,
            "birth_date": "2020-01-15",
            "gender": "male",
            "language": "uz",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _audio_bytes(seed: int = 7) -> bytes:
    return b"RIFF" + (seed.to_bytes(2, "big") * 2048)


async def _run_assessment(client, headers: dict, child_id: str) -> dict:
    create = await client.post(
        "/api/v1/assessments",
        json={"child_id": child_id, "type": "screening"},
        headers=headers,
    )
    assert create.status_code == 201, create.text
    assessment = create.json()
    files = {"audio": ("clip.wav", io.BytesIO(_audio_bytes()), "audio/wav")}
    upload = await client.post(
        f"/api/v1/assessments/{assessment['id']}/recordings",
        files=files,
        data={"task_type": "repeat_word"},
        headers=headers,
    )
    assert upload.status_code == 201, upload.text
    return assessment


async def _create_region(client, admin_headers: dict, name: str) -> str:
    response = await client.post(
        "/api/v1/regions",
        json={"name": name, "type": "region"},
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _create_kindergarten(
    client, admin_headers: dict, name: str, region_id: str | None = None
) -> str:
    payload: dict = {"name": name}
    if region_id:
        payload["region_id"] = region_id
    response = await client.post(
        "/api/v1/kindergartens", json=payload, headers=admin_headers
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _attach_child_to_kg(child_id: str, kindergarten_id: str) -> None:
    """Direct-DB attach since the API doesn't expose ``kindergarten_id`` on
    child create for the parent role.
    """

    from app.database import get_sessionmaker
    from app.models.child import Child

    factory = get_sessionmaker()
    async with factory() as session:
        child = await session.get(Child, child_id)
        assert child is not None
        child.kindergarten_id = kindergarten_id
        await session.commit()


# ------------------------------------------------------------------ Tests


async def test_system_stats_requires_admin(client) -> None:
    _, parent = await _register_login(client, "p@sado.uz")
    response = await client.get("/api/v1/stats/system", headers=parent)
    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


async def test_system_stats_empty(client) -> None:
    admin = await _admin_headers(client)
    response = await client.get("/api/v1/stats/system", headers=admin)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_children"] == 0
    assert body["total_assessments"] == 0
    assert body["assessments_today"] == 0
    assert body["red_risk_percentage"] == 0.0
    assert body["risk_distribution"] == {
        "green": 0,
        "yellow": 0,
        "red": 0,
        "unknown": 0,
    }
    assert isinstance(body["weekly_assessments"], list)
    assert len(body["weekly_assessments"]) == 7


async def test_system_stats_counts_children_and_assessments(client) -> None:
    admin = await _admin_headers(client)
    _, parent = await _register_login(client, "p1@sado.uz")
    await _create_therapist(client, "t1@sado.uz")
    await _create_therapist(client, "t2@sado.uz")

    # Two children and one full assessment
    child_a = await _create_child(client, parent, "Aziz")
    child_b = await _create_child(client, parent, "Madina")
    await _run_assessment(client, parent, child_a)

    response = await client.get("/api/v1/stats/system", headers=admin)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["total_children"] == 2
    assert body["total_assessments"] == 1
    assert body["assessments_today"] == 1
    assert body["completed_assessments"] == 1
    assert body["active_therapists"] == 2
    distribution = body["risk_distribution"]
    assert distribution["green"] + distribution["yellow"] + distribution["red"] == 1
    # Today should be the last bucket of the weekly trend.
    weekly = body["weekly_assessments"]
    today_iso = date.today().isoformat()
    assert weekly[-1]["date"] == today_iso
    assert weekly[-1]["count"] == 1
    # The other point parent + therapists shouldn't change child totals.
    assert body["user_roles"]["therapist"] == 2
    assert body["user_roles"]["parent"] >= 1
    # Sanity: red percentage is normalized to 0..100.
    assert 0.0 <= body["red_risk_percentage"] <= 100.0
    # Unused child still counted.
    assert child_b


async def test_regional_stats_admin_only(client) -> None:
    _, parent = await _register_login(client, "rp@sado.uz")
    response = await client.get("/api/v1/stats/regional", headers=parent)
    assert response.status_code == 403


async def test_regional_stats_groups_by_region(client) -> None:
    admin = await _admin_headers(client)
    _, parent = await _register_login(client, "rp1@sado.uz")

    region_a = await _create_region(client, admin, "Tashkent")
    kg_a = await _create_kindergarten(client, admin, "KG Alpha", region_a)
    kg_b = await _create_kindergarten(client, admin, "KG Beta", region_a)

    child_a = await _create_child(client, parent, "Karim")
    child_b = await _create_child(client, parent, "Laylo")
    await _attach_child_to_kg(child_a, kg_a)
    await _attach_child_to_kg(child_b, kg_b)

    await _run_assessment(client, parent, child_a)
    await _run_assessment(client, parent, child_b)

    response = await client.get("/api/v1/stats/regional", headers=admin)
    assert response.status_code == 200, response.text
    body = response.json()

    region_names = {r["region_name"]: r for r in body["regions"]}
    assert "Tashkent" in region_names
    tashkent = region_names["Tashkent"]
    assert tashkent["children"] == 2
    assert tashkent["assessments"] == 2

    leaderboard_names = {row["name"] for row in body["kindergartens"]}
    assert {"KG Alpha", "KG Beta"} <= leaderboard_names

    # Weekly trend respects ?days=
    response = await client.get(
        "/api/v1/stats/regional?days=14&leaderboard_limit=5", headers=admin
    )
    assert response.status_code == 200
    assert len(response.json()["daily_trend"]) == 14


async def test_kindergarten_stats_visibility(client) -> None:
    admin = await _admin_headers(client)
    _, parent = await _register_login(client, "kp@sado.uz")

    region = await _create_region(client, admin, "Samarkand")
    kg_id = await _create_kindergarten(client, admin, "KG Solar", region)
    child = await _create_child(client, parent, "Sevinch")
    await _attach_child_to_kg(child, kg_id)
    await _run_assessment(client, parent, child)

    # Admin
    response = await client.get(
        f"/api/v1/stats/kindergartens/{kg_id}", headers=admin
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kindergarten_id"] == kg_id
    assert body["child_count"] == 1
    assert body["assessments"] == 1
    assert body["region_name"] == "Samarkand"

    # Parent forbidden
    forbidden = await client.get(
        f"/api/v1/stats/kindergartens/{kg_id}", headers=parent
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "STATS_FORBIDDEN"

    # Unknown id => 404
    missing = await client.get(
        "/api/v1/stats/kindergartens/00000000-0000-0000-0000-000000000000",
        headers=admin,
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "KINDERGARTEN_NOT_FOUND"


async def test_kindergarten_stats_teacher_region_restriction(client) -> None:
    admin = await _admin_headers(client)
    _, parent = await _register_login(client, "tp@sado.uz")

    region_x = await _create_region(client, admin, "Bukhara")
    region_y = await _create_region(client, admin, "Andijan")
    kg_x = await _create_kindergarten(client, admin, "KG X", region_x)

    child = await _create_child(client, parent, "Asror")
    await _attach_child_to_kg(child, kg_x)

    # Teacher in different region
    teacher_email = "teach@sado.uz"
    await _register_login(client, teacher_email, role="teacher")
    # Attach teacher to region_y directly.
    from sqlalchemy import select

    from app.database import get_sessionmaker
    from app.models.user import User

    factory = get_sessionmaker()
    async with factory() as session:
        result = await session.execute(
            select(User).where(User.email == teacher_email)
        )
        teacher = result.scalar_one()
        teacher.region_id = region_y
        await session.commit()

    teacher_login = await client.post(
        "/api/v1/auth/login",
        json={"email": teacher_email, "password": "Sup3r-Secret!"},
    )
    teacher_headers = {
        "Authorization": f"Bearer {teacher_login.json()['access_token']}"
    }

    response = await client.get(
        f"/api/v1/stats/kindergartens/{kg_x}", headers=teacher_headers
    )
    assert response.status_code == 403
    assert response.json()["code"] == "STATS_FORBIDDEN"
