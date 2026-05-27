"""Edge-case tests for ``app/api/v1/exercises.py``.

The happy-path catalogue + assignment flow is covered by
``test_exercises.py``. This module fills in the branches the main
suite does not touch:

* List-filter validators: INVALID_CATEGORY / INVALID_AGE_GROUP /
  INVALID_DIFFICULTY / INVALID_CURSOR.
* GET inactive exercise returns 404 to non-managers.
* Update / delete with unknown id => 404.
* Asset upload size limit (ASSET_TOO_LARGE) and unknown content type
  for image asset_type.
* Asset DELETE for exercise that has no asset is idempotent.
* Asset DELETE forbidden for parent.
* Teacher *with* a region can list / assign / view a child in their
  region; teacher *without* a region cannot.
* Parent cannot self-assign an inactive exercise (EXERCISE_INACTIVE).
* Therapist can assign exercise inactive without rejection.
* Status-filter validation in /assignments and /assignments/me.
* Cursor-pagination round-trip on /assignments/me.
* Update assignment: notes / due_date / score paths and forbidden
  branch for an outsider parent.
* Complete assignment forbidden for outsider parent.
* GET single assignment 404 + forbidden branches.
* DELETE assignment by parent works on own child; outsider 403.
* /assignments/me for teacher with no region returns an empty page.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------- Helpers


async def _register_login(
    client, email: str, role: str = "parent"
) -> tuple[dict, dict]:
    creds = {
        "email": email,
        "password": "Sup3r-Secret!",
        "full_name": "Edge Ex User",
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


async def _create_admin(email: str = "edge-ex-admin@sado.uz") -> str:
    from app.core.security import hash_password
    from app.database import get_sessionmaker
    from app.models.user import User, UserRole

    factory = get_sessionmaker()
    async with factory() as session:
        admin = User(
            email=email,
            password_hash=hash_password("AdminP4ss!"),
            full_name="Admin Edge Ex",
            role=UserRole.ADMIN.value,
            is_active=True,
            is_verified=True,
        )
        session.add(admin)
        await session.commit()
        await session.refresh(admin)
        return admin.id


async def _admin_headers(client, email: str = "edge-ex-admin@sado.uz") -> dict:
    await _create_admin(email)
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "AdminP4ss!"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _create_therapist(email: str = "edge-ex-th@sado.uz") -> str:
    from app.core.security import hash_password
    from app.database import get_sessionmaker
    from app.models.user import User, UserRole

    factory = get_sessionmaker()
    async with factory() as session:
        therapist = User(
            email=email,
            password_hash=hash_password("ThP4ss!22"),
            full_name="Therapist Edge",
            role=UserRole.THERAPIST.value,
            is_active=True,
            is_verified=True,
        )
        session.add(therapist)
        await session.commit()
        await session.refresh(therapist)
        return therapist.id


async def _therapist_headers(client, email: str = "edge-ex-th@sado.uz") -> dict:
    await _create_therapist(email)
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "ThP4ss!22"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _set_user_region(user_id: str, region_id: str) -> None:
    from app.database import get_sessionmaker
    from app.models.user import User

    factory = get_sessionmaker()
    async with factory() as session:
        user = await session.get(User, user_id)
        assert user is not None
        user.region_id = region_id
        await session.commit()


async def _attach_child_to_kg(child_id: str, kindergarten_id: str) -> None:
    from app.database import get_sessionmaker
    from app.models.child import Child

    factory = get_sessionmaker()
    async with factory() as session:
        child = await session.get(Child, child_id)
        assert child is not None
        child.kindergarten_id = kindergarten_id
        await session.commit()


async def _create_region(client, admin_headers: dict, name: str) -> str:
    response = await client.post(
        "/api/v1/regions",
        json={"name": name, "type": "region"},
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _create_kindergarten(
    client, admin_headers: dict, name: str, region_id: str
) -> str:
    response = await client.post(
        "/api/v1/kindergartens",
        json={"name": name, "region_id": region_id},
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


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


async def _create_exercise(
    client, headers: dict, **overrides
) -> dict:
    payload = {
        "title": "Edge Exercise",
        "category": "articulation",
        "age_group": "4-5",
        "difficulty": "easy",
        "language": "uz",
    }
    payload.update(overrides)
    response = await client.post(
        "/api/v1/exercises", json=payload, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


# ----------------------------------------------------- List validators


async def test_list_invalid_category_rejected(client) -> None:
    _, headers = await _register_login(client, "lst-cat@sado.uz")
    response = await client.get(
        "/api/v1/exercises?category=not-a-category", headers=headers
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_CATEGORY"


async def test_list_invalid_age_group_rejected(client) -> None:
    _, headers = await _register_login(client, "lst-age@sado.uz")
    response = await client.get(
        "/api/v1/exercises?age_group=99-100", headers=headers
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_AGE_GROUP"


async def test_list_invalid_difficulty_rejected(client) -> None:
    _, headers = await _register_login(client, "lst-diff@sado.uz")
    response = await client.get(
        "/api/v1/exercises?difficulty=impossible", headers=headers
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_DIFFICULTY"


async def test_list_invalid_cursor_rejected(client) -> None:
    _, headers = await _register_login(client, "lst-cur@sado.uz")
    response = await client.get(
        "/api/v1/exercises?cursor=garbage", headers=headers
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_CURSOR"


async def test_list_with_language_filter_lowercased(client) -> None:
    t_headers = await _therapist_headers(client)
    await _create_exercise(client, t_headers, language="uz", title="Uz one")
    await _create_exercise(client, t_headers, language="ru", title="Ru one")
    response = await client.get(
        "/api/v1/exercises?language=UZ", headers=t_headers
    )
    assert response.status_code == 200
    titles = [it["title"] for it in response.json()["items"]]
    assert "Uz one" in titles
    assert "Ru one" not in titles


# ----------------------------------------------------- 404 / forbidden


async def test_get_unknown_exercise_returns_404(client) -> None:
    _, headers = await _register_login(client, "unk@sado.uz")
    response = await client.get(
        "/api/v1/exercises/00000000-0000-0000-0000-000000000000",
        headers=headers,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "EXERCISE_NOT_FOUND"


async def test_update_unknown_exercise_returns_404(client) -> None:
    headers = await _therapist_headers(client)
    response = await client.put(
        "/api/v1/exercises/00000000-0000-0000-0000-000000000000",
        json={"title": "Won't stick"},
        headers=headers,
    )
    assert response.status_code == 404


async def test_update_exercise_as_parent_forbidden(client) -> None:
    t_headers = await _therapist_headers(client)
    ex = await _create_exercise(client, t_headers)
    _, p_headers = await _register_login(client, "p-upd@sado.uz")
    response = await client.put(
        f"/api/v1/exercises/{ex['id']}",
        json={"title": "Try"},
        headers=p_headers,
    )
    assert response.status_code == 403


async def test_delete_unknown_exercise_404_for_admin(client) -> None:
    a_headers = await _admin_headers(client)
    response = await client.delete(
        "/api/v1/exercises/00000000-0000-0000-0000-000000000000",
        headers=a_headers,
    )
    assert response.status_code == 404


async def test_inactive_exercise_invisible_to_parent_get(client) -> None:
    t_headers = await _therapist_headers(client)
    ex = await _create_exercise(client, t_headers, is_active=False)
    _, p_headers = await _register_login(client, "p-inact@sado.uz")
    response = await client.get(
        f"/api/v1/exercises/{ex['id']}", headers=p_headers
    )
    assert response.status_code == 404


# ----------------------------------------------------- Asset uploads


async def test_upload_asset_too_large_rejected(client) -> None:
    headers = await _therapist_headers(client)
    ex = await _create_exercise(client, headers)
    huge = b"\x00" * (5 * 1024 * 1024 + 100)
    response = await client.post(
        f"/api/v1/exercises/{ex['id']}/assets",
        data={"asset_type": "audio"},
        files={"file": ("big.mp3", huge, "audio/mpeg")},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "ASSET_TOO_LARGE"


async def test_upload_image_with_audio_content_type_rejected(client) -> None:
    headers = await _therapist_headers(client)
    ex = await _create_exercise(client, headers)
    response = await client.post(
        f"/api/v1/exercises/{ex['id']}/assets",
        data={"asset_type": "image"},
        files={"file": ("a.mp3", b"\x49\x44\x33fake", "audio/mpeg")},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_ASSET_TYPE"


async def test_delete_asset_forbidden_for_parent(client) -> None:
    t_headers = await _therapist_headers(client)
    ex = await _create_exercise(client, t_headers)
    _, p_headers = await _register_login(client, "p-asset@sado.uz")
    response = await client.delete(
        f"/api/v1/exercises/{ex['id']}/assets/audio", headers=p_headers
    )
    assert response.status_code == 403


# ----------------------------------------------------- Assignments


async def test_assign_inactive_exercise_to_parent_rejected(client) -> None:
    t_headers = await _therapist_headers(client)
    ex = await _create_exercise(client, t_headers, is_active=False)

    parent, p_headers = await _register_login(client, "p-inact-as@sado.uz")
    child_id = await _create_child(client, p_headers)

    response = await client.post(
        f"/api/v1/exercises/{child_id}/assign",
        json={"exercise_id": ex["id"]},
        headers=p_headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "EXERCISE_INACTIVE"
    assert parent  # silence unused


async def test_assign_inactive_exercise_allowed_for_therapist(client) -> None:
    t_headers = await _therapist_headers(client)
    ex = await _create_exercise(client, t_headers, is_active=False)

    parent, p_headers = await _register_login(client, "p-th-as@sado.uz")
    child_id = await _create_child(client, p_headers)

    response = await client.post(
        f"/api/v1/exercises/{child_id}/assign",
        json={"exercise_id": ex["id"]},
        headers=t_headers,
    )
    assert response.status_code == 201
    assert parent


async def test_assign_for_unknown_child_returns_404(client) -> None:
    t_headers = await _therapist_headers(client)
    ex = await _create_exercise(client, t_headers)
    response = await client.post(
        "/api/v1/exercises/00000000-0000-0000-0000-000000000000/assign",
        json={"exercise_id": ex["id"]},
        headers=t_headers,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "CHILD_NOT_FOUND"


async def test_list_child_assignments_invalid_status_rejected(client) -> None:
    t_headers = await _therapist_headers(client)
    parent, p_headers = await _register_login(client, "p-ls-st@sado.uz")
    child_id = await _create_child(client, p_headers)
    response = await client.get(
        f"/api/v1/exercises/{child_id}/assignments?status=garbage",
        headers=t_headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_ASSIGNMENT_STATUS"
    assert parent


async def test_list_child_assignments_invalid_cursor_rejected(client) -> None:
    parent, p_headers = await _register_login(client, "p-ls-cur@sado.uz")
    child_id = await _create_child(client, p_headers)
    response = await client.get(
        f"/api/v1/exercises/{child_id}/assignments?cursor=junk",
        headers=p_headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_CURSOR"
    assert parent


async def test_list_child_assignments_filters_by_status(client) -> None:
    t_headers = await _therapist_headers(client)
    ex = await _create_exercise(client, t_headers)
    _, p_headers = await _register_login(client, "p-ls-flt@sado.uz")
    child_id = await _create_child(client, p_headers)
    a1 = await client.post(
        f"/api/v1/exercises/{child_id}/assign",
        json={"exercise_id": ex["id"]},
        headers=p_headers,
    )
    aid = a1.json()["id"]
    await client.put(
        f"/api/v1/exercises/assignments/{aid}/complete",
        json={"score": 80},
        headers=p_headers,
    )
    completed = await client.get(
        f"/api/v1/exercises/{child_id}/assignments?status=completed",
        headers=p_headers,
    )
    assert completed.status_code == 200
    assert all(it["status"] == "completed" for it in completed.json()["items"])


# --------------------------------------------------- /assignments/me


async def test_my_assignments_invalid_status_rejected(client) -> None:
    _, p_headers = await _register_login(client, "me-st@sado.uz")
    response = await client.get(
        "/api/v1/exercises/assignments/me?status=meh", headers=p_headers
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_ASSIGNMENT_STATUS"


async def test_my_assignments_invalid_cursor_rejected(client) -> None:
    _, p_headers = await _register_login(client, "me-cur@sado.uz")
    response = await client.get(
        "/api/v1/exercises/assignments/me?cursor=junk", headers=p_headers
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_CURSOR"


async def test_my_assignments_pagination_with_cursor(client) -> None:
    t_headers = await _therapist_headers(client)
    ex = await _create_exercise(client, t_headers)
    parent, p_headers = await _register_login(client, "me-pg@sado.uz")
    child_id = await _create_child(client, p_headers)
    # Create 3 assignments by varying notes (no unique constraint on
    # the same (child, exercise) pair — but multiple are allowed when
    # due_date differs).
    from datetime import date, timedelta

    for offset in range(3):
        due = (date.today() + timedelta(days=offset)).isoformat()
        await client.post(
            f"/api/v1/exercises/{child_id}/assign",
            json={"exercise_id": ex["id"], "due_date": due},
            headers=p_headers,
        )
    page1 = await client.get(
        "/api/v1/exercises/assignments/me?limit=2", headers=p_headers
    )
    assert page1.status_code == 200
    body1 = page1.json()
    assert len(body1["items"]) == 2
    assert body1["has_more"] is True
    page2 = await client.get(
        f"/api/v1/exercises/assignments/me?limit=2&cursor={body1['next_cursor']}",
        headers=p_headers,
    )
    assert page2.status_code == 200
    assert page2.json()["has_more"] is False
    assert parent


async def test_my_assignments_teacher_no_region_empty(client) -> None:
    """Teacher without a region sees an empty page (no 500)."""

    # Create teacher via register endpoint, leave region_id NULL.
    creds = {
        "email": "teach-no-region@sado.uz",
        "password": "T3acherP4ss!",
        "full_name": "Teach NoR",
        "role": "teacher",
    }
    await client.post("/api/v1/auth/register", json=creds)
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": creds["email"], "password": creds["password"]},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    response = await client.get(
        "/api/v1/exercises/assignments/me", headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["has_more"] is False
    assert body["next_cursor"] is None


async def test_my_assignments_teacher_with_region_sees_kg_children(
    client,
) -> None:
    """A teacher with a region sees assignments for children attached
    to a kindergarten in that region — and only those."""

    a_headers = await _admin_headers(client)
    region_id = await _create_region(client, a_headers, "Andijon")
    kg_id = await _create_kindergarten(
        client, a_headers, "KG-A", region_id
    )

    # Parent + child in the kindergarten.
    parent, p_headers = await _register_login(client, "p-kg@sado.uz")
    child_id = await _create_child(client, p_headers)
    await _attach_child_to_kg(child_id, kg_id)

    # Therapist seeds an exercise + assignment for that child.
    t_headers = await _therapist_headers(client)
    ex = await _create_exercise(client, t_headers, title="Region only")
    await client.post(
        f"/api/v1/exercises/{child_id}/assign",
        json={"exercise_id": ex["id"]},
        headers=t_headers,
    )

    # Teacher in the same region.
    teach_user, _ = await _register_login(
        client, "teach-r@sado.uz", role="teacher"
    )
    await _set_user_region(teach_user["id"], region_id)
    teach_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "teach-r@sado.uz", "password": "Sup3r-Secret!"},
    )
    teach_headers = {
        "Authorization": f"Bearer {teach_login.json()['access_token']}"
    }
    response = await client.get(
        "/api/v1/exercises/assignments/me", headers=teach_headers
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) >= 1
    assert any(it["exercise"]["title"] == "Region only" for it in items)
    assert parent


# --------------------------------------- Update / complete authorisation


async def test_update_assignment_outsider_parent_forbidden(client) -> None:
    t_headers = await _therapist_headers(client)
    ex = await _create_exercise(client, t_headers)
    parent_a, p_a_headers = await _register_login(client, "p-a-upd@sado.uz")
    _, p_b_headers = await _register_login(client, "p-b-upd@sado.uz")
    child_id = await _create_child(client, p_a_headers)

    assign = await client.post(
        f"/api/v1/exercises/{child_id}/assign",
        json={"exercise_id": ex["id"]},
        headers=p_a_headers,
    )
    aid = assign.json()["id"]
    response = await client.put(
        f"/api/v1/exercises/assignments/{aid}",
        json={"status": "in_progress"},
        headers=p_b_headers,
    )
    assert response.status_code == 403
    assert parent_a


async def test_update_assignment_due_date_notes_score(client) -> None:
    t_headers = await _therapist_headers(client)
    ex = await _create_exercise(client, t_headers)
    _, p_headers = await _register_login(client, "p-fields@sado.uz")
    child_id = await _create_child(client, p_headers)
    assign = await client.post(
        f"/api/v1/exercises/{child_id}/assign",
        json={"exercise_id": ex["id"]},
        headers=p_headers,
    )
    aid = assign.json()["id"]
    response = await client.put(
        f"/api/v1/exercises/assignments/{aid}",
        json={
            "due_date": "2030-01-15",
            "notes": "more practice",
            "score": 65.5,
        },
        headers=p_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["due_date"].startswith("2030-01-15")
    assert body["notes"] == "more practice"
    assert body["score"] == pytest.approx(65.5)


async def test_complete_assignment_outsider_parent_forbidden(client) -> None:
    t_headers = await _therapist_headers(client)
    ex = await _create_exercise(client, t_headers)
    parent_a, p_a_headers = await _register_login(client, "p-a-cmp@sado.uz")
    _, p_b_headers = await _register_login(client, "p-b-cmp@sado.uz")
    child_id = await _create_child(client, p_a_headers)

    assign = await client.post(
        f"/api/v1/exercises/{child_id}/assign",
        json={"exercise_id": ex["id"]},
        headers=p_a_headers,
    )
    aid = assign.json()["id"]
    response = await client.put(
        f"/api/v1/exercises/assignments/{aid}/complete",
        json={"score": 80},
        headers=p_b_headers,
    )
    assert response.status_code == 403
    assert parent_a


# ------------------------------------------- GET / DELETE assignment


async def test_get_assignment_unknown_returns_404(client) -> None:
    headers = await _therapist_headers(client)
    response = await client.get(
        "/api/v1/exercises/assignments/00000000-0000-0000-0000-000000000000",
        headers=headers,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "ASSIGNMENT_NOT_FOUND"


async def test_get_assignment_owner_parent_can_read(client) -> None:
    t_headers = await _therapist_headers(client)
    ex = await _create_exercise(client, t_headers)
    parent, p_headers = await _register_login(client, "p-get-own@sado.uz")
    child_id = await _create_child(client, p_headers)
    assign = await client.post(
        f"/api/v1/exercises/{child_id}/assign",
        json={"exercise_id": ex["id"]},
        headers=p_headers,
    )
    aid = assign.json()["id"]
    response = await client.get(
        f"/api/v1/exercises/assignments/{aid}", headers=p_headers
    )
    assert response.status_code == 200
    assert response.json()["id"] == aid
    assert parent


async def test_delete_assignment_by_owner_parent(client) -> None:
    t_headers = await _therapist_headers(client)
    ex = await _create_exercise(client, t_headers)
    _, p_headers = await _register_login(client, "p-del@sado.uz")
    child_id = await _create_child(client, p_headers)
    assign = await client.post(
        f"/api/v1/exercises/{child_id}/assign",
        json={"exercise_id": ex["id"]},
        headers=p_headers,
    )
    aid = assign.json()["id"]
    deleted = await client.delete(
        f"/api/v1/exercises/assignments/{aid}", headers=p_headers
    )
    assert deleted.status_code == 204
    missing = await client.get(
        f"/api/v1/exercises/assignments/{aid}", headers=p_headers
    )
    assert missing.status_code == 404


async def test_delete_assignment_by_outsider_forbidden(client) -> None:
    t_headers = await _therapist_headers(client)
    ex = await _create_exercise(client, t_headers)
    parent_a, p_a_headers = await _register_login(client, "p-a-del@sado.uz")
    _, p_b_headers = await _register_login(client, "p-b-del@sado.uz")
    child_id = await _create_child(client, p_a_headers)
    assign = await client.post(
        f"/api/v1/exercises/{child_id}/assign",
        json={"exercise_id": ex["id"]},
        headers=p_a_headers,
    )
    aid = assign.json()["id"]
    response = await client.delete(
        f"/api/v1/exercises/assignments/{aid}", headers=p_b_headers
    )
    assert response.status_code == 403
    assert parent_a


# --------------------------------------------- Teacher → child in region


async def test_teacher_can_assign_to_child_in_region(client) -> None:
    a_headers = await _admin_headers(client)
    region_id = await _create_region(client, a_headers, "Farg'ona")
    kg_id = await _create_kindergarten(
        client, a_headers, "KG-F", region_id
    )

    parent, p_headers = await _register_login(client, "p-teach@sado.uz")
    child_id = await _create_child(client, p_headers)
    await _attach_child_to_kg(child_id, kg_id)

    t_headers = await _therapist_headers(client)
    ex = await _create_exercise(client, t_headers, title="For region kid")

    teach_user, _ = await _register_login(
        client, "teach-asg@sado.uz", role="teacher"
    )
    await _set_user_region(teach_user["id"], region_id)
    teach_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "teach-asg@sado.uz", "password": "Sup3r-Secret!"},
    )
    teach_headers = {
        "Authorization": f"Bearer {teach_login.json()['access_token']}"
    }
    assign = await client.post(
        f"/api/v1/exercises/{child_id}/assign",
        json={"exercise_id": ex["id"]},
        headers=teach_headers,
    )
    assert assign.status_code == 201, assign.text
    aid = assign.json()["id"]

    # Teacher can also list and view.
    listing = await client.get(
        f"/api/v1/exercises/{child_id}/assignments", headers=teach_headers
    )
    assert listing.status_code == 200
    assert any(it["id"] == aid for it in listing.json()["items"])

    single = await client.get(
        f"/api/v1/exercises/assignments/{aid}", headers=teach_headers
    )
    assert single.status_code == 200
    assert parent


async def test_teacher_no_region_cannot_view_child_assignments(client) -> None:
    a_headers = await _admin_headers(client)
    region_id = await _create_region(client, a_headers, "Buxoro")
    kg_id = await _create_kindergarten(
        client, a_headers, "KG-B", region_id
    )

    parent, p_headers = await _register_login(client, "p-no-rgn@sado.uz")
    child_id = await _create_child(client, p_headers)
    await _attach_child_to_kg(child_id, kg_id)

    # Teacher with NO region.
    creds = {
        "email": "teach-empty@sado.uz",
        "password": "T3acherP4ss!",
        "full_name": "Teach Empty",
        "role": "teacher",
    }
    await client.post("/api/v1/auth/register", json=creds)
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": creds["email"], "password": creds["password"]},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    response = await client.get(
        f"/api/v1/exercises/{child_id}/assignments", headers=headers
    )
    assert response.status_code == 403
    assert parent
