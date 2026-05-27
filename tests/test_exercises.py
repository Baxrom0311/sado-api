"""End-to-end tests for ``/api/v1/exercises`` and assignments."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------- Helpers


async def _register_and_login(
    client, idx: int = 1, role: str = "parent", region_id: str | None = None
):
    """Register a user via the public endpoint then log them in.

    Returns ``(user, headers)``.
    """

    creds = {
        "email": f"user{idx}@example.com",
        "password": "Sup3r-Secret!",
        "full_name": f"User {idx}",
        "role": role,
    }
    register = await client.post("/api/v1/auth/register", json=creds)
    assert register.status_code == 201, register.text
    user = register.json()

    if region_id is not None:
        # Patch the user's region directly so teachers can see kids in it.
        from app.database import get_sessionmaker
        from app.models.user import User

        factory = get_sessionmaker()
        async with factory() as session:
            db_user = await session.get(User, user["id"])
            assert db_user is not None
            db_user.region_id = region_id
            await session.commit()

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": creds["email"], "password": creds["password"]},
    )
    assert login.status_code == 200, login.text
    tokens = login.json()
    return user, {"Authorization": f"Bearer {tokens['access_token']}"}


async def _create_admin(app, email: str = "admin@example.com"):
    """Insert an admin user via the ORM."""

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


async def _create_therapist(app, email: str = "therapist@example.com"):
    """Insert a therapist user via the ORM."""

    from app.core.security import hash_password
    from app.database import get_sessionmaker
    from app.models.user import User, UserRole

    factory = get_sessionmaker()
    async with factory() as session:
        therapist = User(
            email=email,
            password_hash=hash_password("TheraP4ss!"),
            full_name="Therapist Tina",
            role=UserRole.THERAPIST.value,
            is_active=True,
            is_verified=True,
        )
        session.add(therapist)
        await session.commit()
        await session.refresh(therapist)
        return therapist.id


async def _login_therapist(client, email: str = "therapist@example.com"):
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "TheraP4ss!"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _create_child_for(parent_id: str, name: str = "Ali") -> str:
    """Insert a child via the ORM and return its id."""

    from datetime import date

    from app.database import get_sessionmaker
    from app.models.child import Child

    factory = get_sessionmaker()
    async with factory() as session:
        child = Child(
            name=name,
            birth_date=date(2020, 1, 15),
            gender="male",
            language="uz",
            parent_id=parent_id,
        )
        session.add(child)
        await session.commit()
        await session.refresh(child)
        return child.id


# ----------------------------------------------------------- Catalogue


async def test_create_exercise_as_therapist(client, app) -> None:
    await _create_therapist(app)
    headers = await _login_therapist(client)

    response = await client.post(
        "/api/v1/exercises",
        json={
            "title": "S sound — initial position",
            "description": "Repeat: salom, sariq, soat",
            "category": "articulation",
            "age_group": "4-5",
            "difficulty": "easy",
            "language": "uz",
            "duration_minutes": 5,
            "target_phonemes": "s",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["title"] == "S sound — initial position"
    assert payload["category"] == "articulation"
    assert payload["is_active"] is True
    assert payload["created_by_id"] is not None


async def test_create_exercise_as_parent_forbidden(client, app) -> None:
    _, headers = await _register_and_login(client, idx=1, role="parent")
    response = await client.post(
        "/api/v1/exercises",
        json={"title": "Trying", "category": "articulation"},
        headers=headers,
    )
    assert response.status_code == 403


async def test_invalid_category_rejected(client, app) -> None:
    await _create_therapist(app)
    headers = await _login_therapist(client)
    response = await client.post(
        "/api/v1/exercises",
        json={"title": "Test", "category": "not-a-category"},
        headers=headers,
    )
    assert response.status_code == 422


async def test_list_exercises_filters_and_pagination(client, app) -> None:
    await _create_therapist(app)
    headers = await _login_therapist(client)

    # Seed multiple exercises across categories.
    for i in range(5):
        category = "articulation" if i % 2 == 0 else "vocabulary"
        difficulty = "easy" if i < 2 else "medium"
        await client.post(
            "/api/v1/exercises",
            json={
                "title": f"Exercise {i}",
                "category": category,
                "difficulty": difficulty,
                "age_group": "4-5",
                "language": "uz",
            },
            headers=headers,
        )

    # Filter by category.
    art = await client.get(
        "/api/v1/exercises?category=articulation", headers=headers
    )
    assert art.status_code == 200, art.text
    items = art.json()["items"]
    assert all(it["category"] == "articulation" for it in items)
    assert len(items) >= 3

    # Filter by difficulty.
    medium = await client.get(
        "/api/v1/exercises?difficulty=medium", headers=headers
    )
    assert medium.status_code == 200
    assert all(it["difficulty"] == "medium" for it in medium.json()["items"])

    # Search by title.
    s = await client.get("/api/v1/exercises?search=Exercise%200", headers=headers)
    assert s.status_code == 200
    titles = [it["title"] for it in s.json()["items"]]
    assert "Exercise 0" in titles

    # Pagination cursor.
    page = await client.get("/api/v1/exercises?limit=2", headers=headers)
    assert page.status_code == 200
    body = page.json()
    assert len(body["items"]) == 2
    if body["has_more"]:
        nxt = await client.get(
            f"/api/v1/exercises?limit=2&cursor={body['next_cursor']}",
            headers=headers,
        )
        assert nxt.status_code == 200


async def test_inactive_exercise_hidden_from_parents(client, app) -> None:
    await _create_therapist(app)
    t_headers = await _login_therapist(client)
    create = await client.post(
        "/api/v1/exercises",
        json={"title": "Hidden", "is_active": False},
        headers=t_headers,
    )
    assert create.status_code == 201
    ex_id = create.json()["id"]

    # Parent cannot see it.
    _, p_headers = await _register_and_login(client, idx=2, role="parent")
    parent_view = await client.get(f"/api/v1/exercises/{ex_id}", headers=p_headers)
    assert parent_view.status_code == 404

    # Parent list does not include it.
    listing = await client.get("/api/v1/exercises", headers=p_headers)
    assert listing.status_code == 200
    assert all(it["id"] != ex_id for it in listing.json()["items"])

    # Therapist can include inactive when explicitly asked.
    incl = await client.get(
        "/api/v1/exercises?include_inactive=true", headers=t_headers
    )
    assert incl.status_code == 200
    assert any(it["id"] == ex_id for it in incl.json()["items"])


async def test_update_and_delete_exercise(client, app) -> None:
    admin_id = await _create_admin(app)
    a_headers = await _login_admin(client)
    await _create_therapist(app)
    t_headers = await _login_therapist(client)

    create = await client.post(
        "/api/v1/exercises",
        json={"title": "First Draft"},
        headers=t_headers,
    )
    assert create.status_code == 201
    ex_id = create.json()["id"]

    upd = await client.put(
        f"/api/v1/exercises/{ex_id}",
        json={"title": "Polished", "difficulty": "hard"},
        headers=t_headers,
    )
    assert upd.status_code == 200
    assert upd.json()["title"] == "Polished"
    assert upd.json()["difficulty"] == "hard"

    # Therapist cannot delete; admin can.
    no_delete = await client.delete(
        f"/api/v1/exercises/{ex_id}", headers=t_headers
    )
    assert no_delete.status_code == 403

    deleted = await client.delete(
        f"/api/v1/exercises/{ex_id}", headers=a_headers
    )
    assert deleted.status_code == 204
    missing = await client.get(f"/api/v1/exercises/{ex_id}", headers=a_headers)
    assert missing.status_code == 404
    # Reference admin_id to keep the linter happy.
    assert admin_id


# ----------------------------------------------------------- Assignments


async def test_assign_exercise_and_list_for_child(client, app) -> None:
    await _create_therapist(app)
    t_headers = await _login_therapist(client)

    # Create exercise.
    create = await client.post(
        "/api/v1/exercises",
        json={"title": "Daily Sound Drill"},
        headers=t_headers,
    )
    ex_id = create.json()["id"]

    # Parent + child.
    parent, p_headers = await _register_and_login(client, idx=10, role="parent")
    child_id = await _create_child_for(parent["id"])

    # Therapist assigns.
    assign = await client.post(
        f"/api/v1/exercises/{child_id}/assign",
        json={"exercise_id": ex_id, "notes": "Practice 3 times this week"},
        headers=t_headers,
    )
    assert assign.status_code == 201, assign.text
    assignment = assign.json()
    assert assignment["status"] == "pending"
    assert assignment["child_id"] == child_id
    assert assignment["exercise_id"] == ex_id
    assert assignment["exercise"]["title"] == "Daily Sound Drill"

    # Parent can see their child's assignments.
    listing = await client.get(
        f"/api/v1/exercises/{child_id}/assignments", headers=p_headers
    )
    assert listing.status_code == 200
    assert len(listing.json()["items"]) == 1

    # /me endpoint also returns it.
    me = await client.get(
        "/api/v1/exercises/assignments/me", headers=p_headers
    )
    assert me.status_code == 200
    assert len(me.json()["items"]) == 1


async def test_parent_can_self_assign_for_own_child(client, app) -> None:
    await _create_therapist(app)
    t_headers = await _login_therapist(client)
    create = await client.post(
        "/api/v1/exercises",
        json={"title": "Self-pick"},
        headers=t_headers,
    )
    ex_id = create.json()["id"]

    parent, p_headers = await _register_and_login(client, idx=11, role="parent")
    child_id = await _create_child_for(parent["id"])

    response = await client.post(
        f"/api/v1/exercises/{child_id}/assign",
        json={"exercise_id": ex_id},
        headers=p_headers,
    )
    assert response.status_code == 201


async def test_parent_cannot_assign_for_others_child(client, app) -> None:
    await _create_therapist(app)
    t_headers = await _login_therapist(client)
    create = await client.post(
        "/api/v1/exercises",
        json={"title": "Restricted"},
        headers=t_headers,
    )
    ex_id = create.json()["id"]

    parent_a, _ = await _register_and_login(client, idx=20, role="parent")
    _, parent_b_headers = await _register_and_login(client, idx=21, role="parent")
    child_id = await _create_child_for(parent_a["id"])

    response = await client.post(
        f"/api/v1/exercises/{child_id}/assign",
        json={"exercise_id": ex_id},
        headers=parent_b_headers,
    )
    assert response.status_code == 403


async def test_complete_assignment_sets_status_and_score(client, app) -> None:
    await _create_therapist(app)
    t_headers = await _login_therapist(client)
    create = await client.post(
        "/api/v1/exercises", json={"title": "Done"}, headers=t_headers
    )
    ex_id = create.json()["id"]

    parent, p_headers = await _register_and_login(client, idx=30, role="parent")
    child_id = await _create_child_for(parent["id"])

    assign = await client.post(
        f"/api/v1/exercises/{child_id}/assign",
        json={"exercise_id": ex_id},
        headers=p_headers,
    )
    assignment_id = assign.json()["id"]

    completed = await client.put(
        f"/api/v1/exercises/assignments/{assignment_id}/complete",
        json={"score": 92.5, "notes": "All done!"},
        headers=p_headers,
    )
    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["status"] == "completed"
    assert body["score"] == pytest.approx(92.5)
    assert body["completed_at"] is not None


async def test_update_assignment_reopen_clears_completed_at(client, app) -> None:
    await _create_therapist(app)
    t_headers = await _login_therapist(client)
    create = await client.post(
        "/api/v1/exercises", json={"title": "Reopen"}, headers=t_headers
    )
    ex_id = create.json()["id"]

    parent, p_headers = await _register_and_login(client, idx=40, role="parent")
    child_id = await _create_child_for(parent["id"])
    assign = await client.post(
        f"/api/v1/exercises/{child_id}/assign",
        json={"exercise_id": ex_id},
        headers=p_headers,
    )
    aid = assign.json()["id"]

    await client.put(
        f"/api/v1/exercises/assignments/{aid}/complete",
        json={"score": 80},
        headers=p_headers,
    )
    reopen = await client.put(
        f"/api/v1/exercises/assignments/{aid}",
        json={"status": "in_progress"},
        headers=p_headers,
    )
    assert reopen.status_code == 200
    assert reopen.json()["status"] == "in_progress"
    assert reopen.json()["completed_at"] is None


async def test_invalid_status_rejected(client, app) -> None:
    await _create_therapist(app)
    t_headers = await _login_therapist(client)
    create = await client.post(
        "/api/v1/exercises", json={"title": "Status check"}, headers=t_headers
    )
    ex_id = create.json()["id"]
    parent, p_headers = await _register_and_login(client, idx=50, role="parent")
    child_id = await _create_child_for(parent["id"])
    assign = await client.post(
        f"/api/v1/exercises/{child_id}/assign",
        json={"exercise_id": ex_id},
        headers=p_headers,
    )
    aid = assign.json()["id"]

    response = await client.put(
        f"/api/v1/exercises/assignments/{aid}",
        json={"status": "totally-bogus"},
        headers=p_headers,
    )
    assert response.status_code == 422


async def test_other_parent_cannot_view_assignment(client, app) -> None:
    await _create_therapist(app)
    t_headers = await _login_therapist(client)
    create = await client.post(
        "/api/v1/exercises", json={"title": "Private"}, headers=t_headers
    )
    ex_id = create.json()["id"]

    parent_a, p_a_headers = await _register_and_login(client, idx=60, role="parent")
    _, p_b_headers = await _register_and_login(client, idx=61, role="parent")
    child_id = await _create_child_for(parent_a["id"])

    assign = await client.post(
        f"/api/v1/exercises/{child_id}/assign",
        json={"exercise_id": ex_id},
        headers=p_a_headers,
    )
    aid = assign.json()["id"]

    forbidden = await client.get(
        f"/api/v1/exercises/assignments/{aid}", headers=p_b_headers
    )
    assert forbidden.status_code == 403

    # Listing also forbidden.
    forbidden_list = await client.get(
        f"/api/v1/exercises/{child_id}/assignments", headers=p_b_headers
    )
    assert forbidden_list.status_code == 403


async def test_assigning_unknown_exercise_returns_404(client, app) -> None:
    parent, p_headers = await _register_and_login(client, idx=70, role="parent")
    child_id = await _create_child_for(parent["id"])
    response = await client.post(
        f"/api/v1/exercises/{child_id}/assign",
        json={"exercise_id": "00000000-0000-0000-0000-000000000000"},
        headers=p_headers,
    )
    assert response.status_code == 404


async def test_unauthorized_requests_rejected(client) -> None:
    listing = await client.get("/api/v1/exercises")
    assert listing.status_code == 401


async def test_admin_can_filter_inactive_and_delete(client, app) -> None:
    await _create_admin(app)
    a_headers = await _login_admin(client)
    create = await client.post(
        "/api/v1/exercises",
        json={"title": "Admin's", "is_active": False},
        headers=a_headers,
    )
    ex_id = create.json()["id"]
    incl = await client.get(
        "/api/v1/exercises?include_inactive=true", headers=a_headers
    )
    assert any(it["id"] == ex_id for it in incl.json()["items"])

    deleted = await client.delete(
        f"/api/v1/exercises/{ex_id}", headers=a_headers
    )
    assert deleted.status_code == 204


# ----------------------------------------------------------- Asset upload


async def _create_exercise_for_assets(client, headers) -> str:
    response = await client.post(
        "/api/v1/exercises",
        json={
            "title": "Asset upload target",
            "category": "articulation",
            "age_group": "4-5",
            "difficulty": "easy",
            "language": "uz",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_upload_audio_asset_as_therapist(client, app) -> None:
    await _create_therapist(app)
    headers = await _login_therapist(client)
    ex_id = await _create_exercise_for_assets(client, headers)

    files = {"file": ("hello.mp3", b"\x49\x44\x33fake-audio", "audio/mpeg")}
    response = await client.post(
        f"/api/v1/exercises/{ex_id}/assets",
        data={"asset_type": "audio"},
        files=files,
        headers=headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["audio_example_path"] is not None
    assert payload["audio_example_path"].startswith(f"exercises/{ex_id}/audio/")
    assert payload["audio_example_path"].endswith(".mp3")
    # Image slot must remain untouched.
    assert payload["image_path"] is None


async def test_upload_image_asset_replaces_previous(client, app) -> None:
    await _create_therapist(app)
    headers = await _login_therapist(client)
    ex_id = await _create_exercise_for_assets(client, headers)

    first = await client.post(
        f"/api/v1/exercises/{ex_id}/assets",
        data={"asset_type": "image"},
        files={"file": ("a.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
        headers=headers,
    )
    assert first.status_code == 200
    first_key = first.json()["image_path"]
    assert first_key is not None

    second = await client.post(
        f"/api/v1/exercises/{ex_id}/assets",
        data={"asset_type": "image"},
        files={"file": ("b.jpg", b"\xff\xd8\xff\xe0jpeg-fake", "image/jpeg")},
        headers=headers,
    )
    assert second.status_code == 200
    second_key = second.json()["image_path"]
    assert second_key is not None
    assert second_key != first_key
    assert second_key.endswith(".jpg")


async def test_upload_asset_rejects_invalid_content_type(client, app) -> None:
    await _create_therapist(app)
    headers = await _login_therapist(client)
    ex_id = await _create_exercise_for_assets(client, headers)

    response = await client.post(
        f"/api/v1/exercises/{ex_id}/assets",
        data={"asset_type": "audio"},
        files={"file": ("payload.txt", b"hello", "text/plain")},
        headers=headers,
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "INVALID_ASSET_TYPE"


async def test_upload_asset_rejects_empty_file(client, app) -> None:
    await _create_therapist(app)
    headers = await _login_therapist(client)
    ex_id = await _create_exercise_for_assets(client, headers)

    response = await client.post(
        f"/api/v1/exercises/{ex_id}/assets",
        data={"asset_type": "image"},
        files={"file": ("empty.png", b"", "image/png")},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "ASSET_EMPTY"


async def test_upload_asset_forbidden_for_parent(client, app) -> None:
    # First, create the exercise with an admin so it exists.
    await _create_admin(app)
    a_headers = await _login_admin(client)
    ex_id = await _create_exercise_for_assets(client, a_headers)

    _, p_headers = await _register_and_login(client, idx=99, role="parent")
    response = await client.post(
        f"/api/v1/exercises/{ex_id}/assets",
        data={"asset_type": "audio"},
        files={"file": ("x.mp3", b"data", "audio/mpeg")},
        headers=p_headers,
    )
    assert response.status_code == 403


async def test_upload_asset_404_for_unknown_exercise(client, app) -> None:
    await _create_therapist(app)
    headers = await _login_therapist(client)
    response = await client.post(
        "/api/v1/exercises/00000000-0000-0000-0000-000000000000/assets",
        data={"asset_type": "audio"},
        files={"file": ("x.mp3", b"data", "audio/mpeg")},
        headers=headers,
    )
    assert response.status_code == 404


async def test_delete_asset_clears_path(client, app) -> None:
    await _create_therapist(app)
    headers = await _login_therapist(client)
    ex_id = await _create_exercise_for_assets(client, headers)

    upload = await client.post(
        f"/api/v1/exercises/{ex_id}/assets",
        data={"asset_type": "audio"},
        files={"file": ("a.mp3", b"\x49\x44\x33fake", "audio/mpeg")},
        headers=headers,
    )
    assert upload.status_code == 200
    assert upload.json()["audio_example_path"] is not None

    response = await client.delete(
        f"/api/v1/exercises/{ex_id}/assets/audio", headers=headers
    )
    assert response.status_code == 200
    assert response.json()["audio_example_path"] is None


async def test_delete_asset_idempotent_when_missing(client, app) -> None:
    await _create_therapist(app)
    headers = await _login_therapist(client)
    ex_id = await _create_exercise_for_assets(client, headers)

    response = await client.delete(
        f"/api/v1/exercises/{ex_id}/assets/image", headers=headers
    )
    assert response.status_code == 200
    assert response.json()["image_path"] is None
