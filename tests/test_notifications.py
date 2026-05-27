"""Tests for the /notifications inbox endpoints."""

from __future__ import annotations

from datetime import UTC

import pytest

pytestmark = pytest.mark.asyncio


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


async def _seed_notification(
    user_id: str,
    *,
    title: str = "Hello",
    type: str = "system",
    body: str = "",
    read: bool = False,
    archived: bool = False,
) -> str:
    from datetime import datetime

    from app.database import get_sessionmaker
    from app.models.notification import Notification

    factory = get_sessionmaker()
    async with factory() as session:
        notif = Notification(
            user_id=user_id,
            type=type,
            title=title,
            body=body,
            read_at=datetime.now(UTC) if read else None,
            is_archived=archived,
        )
        session.add(notif)
        await session.commit()
        await session.refresh(notif)
        return notif.id


# ------------------------------------------------------------------ Tests


async def test_list_notifications_empty(client) -> None:
    _, headers = await _register_login(client, "p@sado.uz")
    response = await client.get("/api/v1/notifications", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["has_more"] is False


async def test_list_only_my_notifications(client) -> None:
    user_a, headers_a = await _register_login(client, "a@sado.uz")
    user_b, headers_b = await _register_login(client, "b@sado.uz")

    await _seed_notification(user_a["id"], title="For A")
    await _seed_notification(user_b["id"], title="For B")

    response = await client.get("/api/v1/notifications", headers=headers_a)
    assert response.status_code == 200
    titles = [n["title"] for n in response.json()["items"]]
    assert titles == ["For A"]


async def test_unread_count_and_mark_read(client) -> None:
    user, headers = await _register_login(client, "u@sado.uz")
    n1 = await _seed_notification(user["id"], title="One")
    n2 = await _seed_notification(user["id"], title="Two")
    await _seed_notification(user["id"], title="Read", read=True)

    count = await client.get("/api/v1/notifications/unread-count", headers=headers)
    assert count.status_code == 200
    assert count.json()["unread"] == 2

    marked = await client.put(
        f"/api/v1/notifications/{n1}/read", headers=headers
    )
    assert marked.status_code == 200, marked.text
    assert marked.json()["read_at"] is not None

    # Idempotent — second call doesn't error.
    again = await client.put(
        f"/api/v1/notifications/{n1}/read", headers=headers
    )
    assert again.status_code == 200

    count2 = await client.get("/api/v1/notifications/unread-count", headers=headers)
    assert count2.json()["unread"] == 1
    assert n2  # still unread


async def test_mark_all_read(client) -> None:
    user, headers = await _register_login(client, "all@sado.uz")
    await _seed_notification(user["id"], title="One")
    await _seed_notification(user["id"], title="Two")
    await _seed_notification(user["id"], title="Three")

    response = await client.post(
        "/api/v1/notifications/read-all", headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["unread"] == 3

    after = await client.get("/api/v1/notifications/unread-count", headers=headers)
    assert after.json()["unread"] == 0


async def test_archive_notification(client) -> None:
    user, headers = await _register_login(client, "arc@sado.uz")
    n_id = await _seed_notification(user["id"], title="Old")

    response = await client.delete(
        f"/api/v1/notifications/{n_id}", headers=headers
    )
    assert response.status_code == 204

    # Default list excludes archived
    listing = await client.get("/api/v1/notifications", headers=headers)
    assert listing.json()["items"] == []

    # include_archived=true brings it back
    full = await client.get(
        "/api/v1/notifications?include_archived=true", headers=headers
    )
    assert full.status_code == 200
    assert len(full.json()["items"]) == 1
    assert full.json()["items"][0]["is_archived"] is True


async def test_cannot_modify_other_user_notification(client) -> None:
    user_a, headers_a = await _register_login(client, "x@sado.uz")
    user_b, _ = await _register_login(client, "y@sado.uz")
    notif = await _seed_notification(user_b["id"], title="B only")

    forbidden = await client.put(
        f"/api/v1/notifications/{notif}/read", headers=headers_a
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "NOTIFICATION_FORBIDDEN"


async def test_admin_create_notification(client) -> None:
    admin = await _admin_headers(client)
    target, target_headers = await _register_login(client, "target@sado.uz")

    response = await client.post(
        "/api/v1/notifications",
        json={
            "user_id": target["id"],
            "type": "assessment_completed",
            "title": "Sizning natijangiz tayyor",
            "body": "Bolangiz uchun yangi natija mavjud.",
            "data": {"assessment_id": "abc"},
        },
        headers=admin,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user_id"] == target["id"]
    assert body["type"] == "assessment_completed"
    assert body["data"] == {"assessment_id": "abc"}

    listing = await client.get("/api/v1/notifications", headers=target_headers)
    titles = [n["title"] for n in listing.json()["items"]]
    assert "Sizning natijangiz tayyor" in titles


async def test_admin_create_notification_validation(client) -> None:
    admin = await _admin_headers(client)
    target, _ = await _register_login(client, "v@sado.uz")

    bad = await client.post(
        "/api/v1/notifications",
        json={
            "user_id": target["id"],
            "type": "not-a-real-type",
            "title": "x",
        },
        headers=admin,
    )
    assert bad.status_code == 422
    assert bad.json()["code"] == "INVALID_NOTIFICATION_TYPE"


async def test_non_admin_cannot_push_notification(client) -> None:
    _, parent = await _register_login(client, "pp@sado.uz")
    target, _ = await _register_login(client, "tt@sado.uz")
    response = await client.post(
        "/api/v1/notifications",
        json={
            "user_id": target["id"],
            "type": "system",
            "title": "Try me",
        },
        headers=parent,
    )
    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


async def test_unread_only_filter(client) -> None:
    user, headers = await _register_login(client, "f@sado.uz")
    await _seed_notification(user["id"], title="A", read=True)
    await _seed_notification(user["id"], title="B")

    response = await client.get(
        "/api/v1/notifications?unread_only=true", headers=headers
    )
    assert response.status_code == 200
    titles = [n["title"] for n in response.json()["items"]]
    assert titles == ["B"]


async def test_notification_not_found(client) -> None:
    _, headers = await _register_login(client, "nf@sado.uz")
    response = await client.put(
        "/api/v1/notifications/00000000-0000-0000-0000-000000000000/read",
        headers=headers,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "NOTIFICATION_NOT_FOUND"
