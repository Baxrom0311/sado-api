"""End-to-end tests for the /practice-plans endpoints + generator."""

from __future__ import annotations

import io

import pytest

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------- Helpers


async def _register_login(
    client, email: str, role: str = "parent", *, full_name: str = "Plan Tester"
) -> tuple[dict, dict]:
    """Self-registration path — only valid for parent accounts."""

    creds = {
        "email": email,
        "password": "Sup3r-Secret!",
        "full_name": full_name,
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


async def _seed_therapist(
    email: str = "therapist@example.com", full_name: str = "Therapist Tina"
) -> str:
    """Insert a therapist user directly via the ORM (admin/therapist
    accounts cannot self-register through ``/auth/register``)."""

    from app.core.security import hash_password
    from app.database import get_sessionmaker
    from app.models.user import User, UserRole

    factory = get_sessionmaker()
    async with factory() as session:
        therapist = User(
            email=email,
            password_hash=hash_password("TheraP4ss!"),
            full_name=full_name,
            role=UserRole.THERAPIST.value,
            is_active=True,
            is_verified=True,
        )
        session.add(therapist)
        await session.commit()
        await session.refresh(therapist)
        return therapist.id


async def _login_therapist(client, email: str = "therapist@example.com") -> dict:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "TheraP4ss!"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _therapist_headers(client, email: str) -> dict:
    """Convenience: seed + login a therapist with a unique email."""

    await _seed_therapist(email=email)
    return await _login_therapist(client, email=email)


async def _create_child(
    client, headers: dict, name: str = "Aziz", language: str = "uz"
) -> str:
    response = await client.post(
        "/api/v1/children",
        json={
            "name": name,
            "birth_date": "2020-01-15",
            "gender": "male",
            "language": language,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _audio(seed: int) -> bytes:
    return bytes((seed + i) % 256 for i in range(8000))


async def _new_assessment_with_recording(
    client, headers: dict, child_id: str, *, seed: int
) -> str:
    create = await client.post(
        "/api/v1/assessments",
        json={"child_id": child_id, "type": "screening"},
        headers=headers,
    )
    assert create.status_code == 201, create.text
    assessment_id = create.json()["id"]

    files = {"audio": ("clip.wav", io.BytesIO(_audio(seed)), "audio/wav")}
    upload = await client.post(
        f"/api/v1/assessments/{assessment_id}/recordings",
        files=files,
        data={"task_type": "repeat_word", "duration_sec": "5.0"},
        headers=headers,
    )
    assert upload.status_code == 201, upload.text
    return assessment_id


async def _seed_global_exercise(
    client,
    therapist_headers: dict,
    *,
    title: str,
    category: str = "articulation",
    target_phonemes: str | None = None,
) -> str:
    """Therapist creates a global (tenant-less) exercise visible to everyone."""

    payload = {
        "title": title,
        "description": f"Test exercise: {title}",
        "category": category,
        "age_group": "4-5",
        "difficulty": "easy",
        "language": "uz",
        "duration_minutes": 5,
    }
    if target_phonemes is not None:
        payload["target_phonemes"] = target_phonemes
    response = await client.post(
        "/api/v1/exercises", json=payload, headers=therapist_headers
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _seed_default_catalogue(client, therapist_headers: dict) -> dict[str, str]:
    """Build a small global catalogue covering every category the
    generator may need so the auto-plan always has something to pick.
    """

    catalogue: dict[str, str] = {}
    catalogue["articulation_r"] = await _seed_global_exercise(
        client,
        therapist_headers,
        title="“R” tovushi mashqi",
        category="articulation",
        target_phonemes="R, r",
    )
    catalogue["articulation_s"] = await _seed_global_exercise(
        client,
        therapist_headers,
        title="“S” tovushi mashqi",
        category="articulation",
        target_phonemes="S, s",
    )
    catalogue["articulation_default"] = await _seed_global_exercise(
        client,
        therapist_headers,
        title="Umumiy artikulatsiya mashqi",
        category="articulation",
    )
    catalogue["breathing"] = await _seed_global_exercise(
        client,
        therapist_headers,
        title="Nafas mashqlari",
        category="breathing",
    )
    catalogue["fluency"] = await _seed_global_exercise(
        client,
        therapist_headers,
        title="Ravonlik mashqlari",
        category="fluency",
    )
    return catalogue


# --------------------------------------------------------------- Generator service unit tests


async def test_generator_returns_at_least_one_item_with_fallback(
    client, monkeypatch
) -> None:
    """Even with zero matched signals, the plan must contain a fallback item."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    therapist = await _therapist_headers(client, "plan-gen-fallback-th@sado.uz")
    _, parent = await _register_login(client, "plan-gen-fallback-p@sado.uz")
    catalogue = await _seed_default_catalogue(client, therapist)
    child_id = await _create_child(client, parent, name="Diyora")
    assessment_id = await _new_assessment_with_recording(
        client, parent, child_id, seed=2
    )

    response = await client.post(
        "/api/v1/practice-plans/generate",
        json={"assessment_id": assessment_id, "max_items": 5},
        headers=therapist,
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["child_id"] == child_id
    assert body["assessment_id"] == assessment_id
    assert body["status"] == "draft"
    assert body["locale"] == "uz"
    assert isinstance(body["items"], list)
    assert len(body["items"]) >= 1
    item_exercise_ids = {item["exercise_id"] for item in body["items"]}
    # The catalogue must have produced something the generator could
    # actually pick from.
    assert item_exercise_ids.issubset(set(catalogue.values()))


async def test_generator_targets_weakest_phonemes(client, monkeypatch) -> None:
    """When the analysis surfaces weak phonemes, items reference matching exercises."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    therapist = await _therapist_headers(client, "plan-gen-phon-th@sado.uz")
    _, parent = await _register_login(client, "plan-gen-phon-p@sado.uz")
    catalogue = await _seed_default_catalogue(client, therapist)
    child_id = await _create_child(client, parent, name="Sevinch")
    assessment_id = await _new_assessment_with_recording(
        client, parent, child_id, seed=11
    )

    response = await client.post(
        "/api/v1/practice-plans/generate",
        json={"assessment_id": assessment_id, "max_items": 5, "activate": True},
        headers=parent,  # parent can self-trigger generation
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["status"] == "active"
    assert isinstance(body["focus_areas"], list)

    # When focus_areas contain phoneme codes, at least one item should
    # reference the matching catalogue entry.
    phoneme_codes = [
        f for f in (body["focus_areas"] or []) if f.startswith("phoneme:")
    ]
    if phoneme_codes:
        item_codes = {item["focus_code"] for item in body["items"]}
        assert any(code in item_codes for code in phoneme_codes)
        # Sanity-check phoneme-targeted exercises were preferred when possible.
        targeted_ids = {
            catalogue["articulation_r"],
            catalogue["articulation_s"],
            catalogue["articulation_default"],
        }
        item_exercise_ids = {item["exercise_id"] for item in body["items"]}
        assert item_exercise_ids & targeted_ids


async def test_generator_404_on_missing_assessment(client) -> None:
    therapist = await _therapist_headers(client, "plan-gen-missing@sado.uz")

    response = await client.post(
        "/api/v1/practice-plans/generate",
        json={
            "assessment_id": "00000000-0000-0000-0000-000000000000",
            "max_items": 3,
        },
        headers=therapist,
    )
    assert response.status_code == 404


async def test_generator_forbidden_for_other_parent(client, monkeypatch) -> None:
    """A different parent cannot generate a plan from someone else's assessment."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    therapist = await _therapist_headers(client, "plan-gen-403-th@sado.uz")
    await _seed_default_catalogue(client, therapist)
    _, owner = await _register_login(client, "plan-gen-403-owner@sado.uz")
    child_id = await _create_child(client, owner, name="Sardor")
    assessment_id = await _new_assessment_with_recording(
        client, owner, child_id, seed=4
    )

    _, intruder = await _register_login(client, "plan-gen-403-intruder@sado.uz")
    response = await client.post(
        "/api/v1/practice-plans/generate",
        json={"assessment_id": assessment_id, "max_items": 3},
        headers=intruder,
    )
    assert response.status_code == 403, response.text


# --------------------------------------------------------------- CRUD tests


async def test_create_plan_requires_therapist_role(client) -> None:
    """Parents are explicitly prevented from authoring plans manually."""

    _, parent = await _register_login(client, "plan-create-parent@sado.uz")
    child_id = await _create_child(client, parent, name="Karim")

    response = await client.post(
        "/api/v1/practice-plans",
        json={
            "child_id": child_id,
            "title": "Mening rejam",
        },
        headers=parent,
    )
    assert response.status_code == 403, response.text


async def test_therapist_creates_plan_with_items(client) -> None:
    therapist = await _therapist_headers(client, "plan-create-th@sado.uz")
    _, parent = await _register_login(client, "plan-create-p@sado.uz")
    catalogue = await _seed_default_catalogue(client, therapist)
    child_id = await _create_child(client, parent, name="Lola")

    response = await client.post(
        "/api/v1/practice-plans",
        json={
            "child_id": child_id,
            "title": "Birinchi mashq rejasi",
            "summary": "Boshlang‘ich daraja",
            "locale": "uz",
            "status": "active",
            "focus_areas": ["phoneme:r", "voice_quality:high_jitter"],
            "items": [
                {
                    "exercise_id": catalogue["articulation_r"],
                    "priority": 1,
                    "target_count": 5,
                    "focus_code": "phoneme:r",
                    "notes": "Kuniga 5 marta",
                },
                {
                    "exercise_id": catalogue["breathing"],
                    "priority": 2,
                    "target_count": 3,
                    "focus_code": "voice_quality:high_jitter",
                },
            ],
        },
        headers=therapist,
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["title"] == "Birinchi mashq rejasi"
    assert body["status"] == "active"
    assert body["item_count"] == 2
    assert body["completed_item_count"] == 0
    assert body["focus_areas"] == ["phoneme:r", "voice_quality:high_jitter"]
    assert {item["exercise_id"] for item in body["items"]} == {
        catalogue["articulation_r"],
        catalogue["breathing"],
    }
    # Items are ordered by priority ascending.
    priorities = [item["priority"] for item in body["items"]]
    assert priorities == sorted(priorities)


async def test_create_plan_rejects_assessment_for_different_child(client) -> None:
    """Cross-child assessment references are rejected at create time."""

    therapist = await _therapist_headers(client, "plan-create-mismatch-th@sado.uz")
    _, parent_a = await _register_login(client, "plan-create-mismatch-a@sado.uz")
    _, parent_b = await _register_login(client, "plan-create-mismatch-b@sado.uz")
    child_a = await _create_child(client, parent_a, name="A")
    child_b = await _create_child(client, parent_b, name="B")

    create = await client.post(
        "/api/v1/assessments",
        json={"child_id": child_a, "type": "screening"},
        headers=therapist,
    )
    assert create.status_code == 201
    assessment_id = create.json()["id"]

    response = await client.post(
        "/api/v1/practice-plans",
        json={
            "child_id": child_b,
            "assessment_id": assessment_id,
            "title": "Wrong child plan",
        },
        headers=therapist,
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "ASSESSMENT_CHILD_MISMATCH"


async def test_list_plans_scoped_to_parent(client, monkeypatch) -> None:
    """Parents only see plans for their own children."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    therapist = await _therapist_headers(client, "plan-list-th@sado.uz")
    catalogue = await _seed_default_catalogue(client, therapist)

    _, parent_a = await _register_login(client, "plan-list-a@sado.uz")
    child_a = await _create_child(client, parent_a, name="A")
    assessment_a = await _new_assessment_with_recording(
        client, parent_a, child_a, seed=7
    )

    _, parent_b = await _register_login(client, "plan-list-b@sado.uz")
    child_b = await _create_child(client, parent_b, name="B")

    # Generate for A, manually author for B.
    plan_a = await client.post(
        "/api/v1/practice-plans/generate",
        json={"assessment_id": assessment_a, "max_items": 3},
        headers=parent_a,
    )
    assert plan_a.status_code == 201
    plan_b = await client.post(
        "/api/v1/practice-plans",
        json={
            "child_id": child_b,
            "title": "B uchun reja",
            "items": [
                {
                    "exercise_id": catalogue["articulation_default"],
                    "target_count": 1,
                }
            ],
        },
        headers=therapist,
    )
    assert plan_b.status_code == 201

    # Parent A sees only their plan.
    list_a = await client.get("/api/v1/practice-plans", headers=parent_a)
    assert list_a.status_code == 200
    ids_a = {p["id"] for p in list_a.json()["items"]}
    assert plan_a.json()["id"] in ids_a
    assert plan_b.json()["id"] not in ids_a

    # Therapist sees both.
    list_th = await client.get("/api/v1/practice-plans", headers=therapist)
    assert list_th.status_code == 200
    ids_th = {p["id"] for p in list_th.json()["items"]}
    assert {plan_a.json()["id"], plan_b.json()["id"]} <= ids_th


async def test_list_plans_status_filter(client) -> None:
    therapist = await _therapist_headers(client, "plan-list-status-th@sado.uz")
    _, parent = await _register_login(client, "plan-list-status-p@sado.uz")
    catalogue = await _seed_default_catalogue(client, therapist)
    child_id = await _create_child(client, parent, name="Filter")

    draft = await client.post(
        "/api/v1/practice-plans",
        json={
            "child_id": child_id,
            "title": "Draft",
            "status": "draft",
            "items": [
                {"exercise_id": catalogue["articulation_default"], "target_count": 1}
            ],
        },
        headers=therapist,
    )
    assert draft.status_code == 201
    active = await client.post(
        "/api/v1/practice-plans",
        json={
            "child_id": child_id,
            "title": "Active",
            "status": "active",
            "items": [
                {"exercise_id": catalogue["fluency"], "target_count": 1}
            ],
        },
        headers=therapist,
    )
    assert active.status_code == 201

    response = await client.get(
        "/api/v1/practice-plans?status=active", headers=therapist
    )
    assert response.status_code == 200
    body = response.json()
    statuses = {p["status"] for p in body["items"]}
    assert statuses == {"active"}

    invalid = await client.get(
        "/api/v1/practice-plans?status=bogus", headers=therapist
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "INVALID_PLAN_STATUS"


async def test_get_plan_detail_includes_items(client) -> None:
    therapist = await _therapist_headers(client, "plan-get-th@sado.uz")
    _, parent = await _register_login(client, "plan-get-p@sado.uz")
    catalogue = await _seed_default_catalogue(client, therapist)
    child_id = await _create_child(client, parent, name="Detail")

    create = await client.post(
        "/api/v1/practice-plans",
        json={
            "child_id": child_id,
            "title": "Detail plan",
            "items": [
                {"exercise_id": catalogue["articulation_default"], "target_count": 2},
                {"exercise_id": catalogue["breathing"], "target_count": 4},
            ],
        },
        headers=therapist,
    )
    plan_id = create.json()["id"]

    response = await client.get(
        f"/api/v1/practice-plans/{plan_id}", headers=parent
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == plan_id
    assert body["item_count"] == 2
    assert all("exercise_title" in item for item in body["items"])


async def test_update_plan_marks_completed_at(client) -> None:
    therapist = await _therapist_headers(client, "plan-update-th@sado.uz")
    _, parent = await _register_login(client, "plan-update-p@sado.uz")
    catalogue = await _seed_default_catalogue(client, therapist)
    child_id = await _create_child(client, parent, name="Update")

    create = await client.post(
        "/api/v1/practice-plans",
        json={
            "child_id": child_id,
            "title": "Update plan",
            "items": [
                {"exercise_id": catalogue["articulation_default"], "target_count": 1}
            ],
        },
        headers=therapist,
    )
    plan_id = create.json()["id"]
    assert create.json()["completed_at"] is None

    closed = await client.put(
        f"/api/v1/practice-plans/{plan_id}",
        json={"status": "completed"},
        headers=therapist,
    )
    assert closed.status_code == 200
    assert closed.json()["status"] == "completed"
    assert closed.json()["completed_at"] is not None

    reopened = await client.put(
        f"/api/v1/practice-plans/{plan_id}",
        json={"status": "active"},
        headers=therapist,
    )
    assert reopened.status_code == 200
    assert reopened.json()["completed_at"] is None


async def test_delete_plan_removes_items(client) -> None:
    therapist = await _therapist_headers(client, "plan-delete-th@sado.uz")
    _, parent = await _register_login(client, "plan-delete-p@sado.uz")
    catalogue = await _seed_default_catalogue(client, therapist)
    child_id = await _create_child(client, parent, name="Delete")

    create = await client.post(
        "/api/v1/practice-plans",
        json={
            "child_id": child_id,
            "title": "Delete plan",
            "items": [
                {"exercise_id": catalogue["fluency"], "target_count": 1}
            ],
        },
        headers=therapist,
    )
    plan_id = create.json()["id"]

    delete = await client.delete(
        f"/api/v1/practice-plans/{plan_id}", headers=therapist
    )
    assert delete.status_code == 204

    missing = await client.get(
        f"/api/v1/practice-plans/{plan_id}", headers=therapist
    )
    assert missing.status_code == 404


async def test_parent_cannot_delete_or_edit_plan(client) -> None:
    therapist = await _therapist_headers(client, "plan-perm-th@sado.uz")
    _, parent = await _register_login(client, "plan-perm-p@sado.uz")
    catalogue = await _seed_default_catalogue(client, therapist)
    child_id = await _create_child(client, parent, name="Perm")

    create = await client.post(
        "/api/v1/practice-plans",
        json={
            "child_id": child_id,
            "title": "Perm plan",
            "items": [
                {"exercise_id": catalogue["articulation_default"], "target_count": 1}
            ],
        },
        headers=therapist,
    )
    plan_id = create.json()["id"]

    edit = await client.put(
        f"/api/v1/practice-plans/{plan_id}",
        json={"title": "Hijacked"},
        headers=parent,
    )
    assert edit.status_code == 403

    delete = await client.delete(
        f"/api/v1/practice-plans/{plan_id}", headers=parent
    )
    assert delete.status_code == 403


# --------------------------------------------------------------- Item endpoints


async def test_add_and_remove_plan_item(client) -> None:
    therapist = await _therapist_headers(client, "plan-item-th@sado.uz")
    _, parent = await _register_login(client, "plan-item-p@sado.uz")
    catalogue = await _seed_default_catalogue(client, therapist)
    child_id = await _create_child(client, parent, name="Item")

    create = await client.post(
        "/api/v1/practice-plans",
        json={"child_id": child_id, "title": "Items"},
        headers=therapist,
    )
    plan_id = create.json()["id"]
    assert create.json()["item_count"] == 0

    add = await client.post(
        f"/api/v1/practice-plans/{plan_id}/items",
        json={
            "exercise_id": catalogue["breathing"],
            "priority": 2,
            "target_count": 5,
            "focus_code": "voice_quality:low_hnr",
            "notes": "Sokin xonada",
        },
        headers=therapist,
    )
    assert add.status_code == 201, add.text
    item_id = add.json()["id"]
    assert add.json()["exercise_title"] == "Nafas mashqlari"
    assert add.json()["priority"] == 2

    remove = await client.delete(
        f"/api/v1/practice-plans/{plan_id}/items/{item_id}",
        headers=therapist,
    )
    assert remove.status_code == 204

    detail = await client.get(
        f"/api/v1/practice-plans/{plan_id}", headers=therapist
    )
    assert detail.json()["item_count"] == 0


async def test_add_item_rejects_inactive_exercise(client) -> None:
    therapist = await _therapist_headers(client, "plan-item-inactive-th@sado.uz")
    _, parent = await _register_login(client, "plan-item-inactive-p@sado.uz")
    catalogue = await _seed_default_catalogue(client, therapist)
    child_id = await _create_child(client, parent, name="Inactive")

    # Disable an exercise.
    disabled = await client.put(
        f"/api/v1/exercises/{catalogue['fluency']}",
        json={"is_active": False},
        headers=therapist,
    )
    assert disabled.status_code == 200
    assert disabled.json()["is_active"] is False

    plan = await client.post(
        "/api/v1/practice-plans",
        json={"child_id": child_id, "title": "Inactive guard"},
        headers=therapist,
    )
    plan_id = plan.json()["id"]

    response = await client.post(
        f"/api/v1/practice-plans/{plan_id}/items",
        json={"exercise_id": catalogue["fluency"], "target_count": 1},
        headers=therapist,
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "EXERCISE_INACTIVE"


async def test_complete_item_increments_and_finishes(client) -> None:
    therapist = await _therapist_headers(client, "plan-complete-th@sado.uz")
    _, parent = await _register_login(client, "plan-complete-p@sado.uz")
    catalogue = await _seed_default_catalogue(client, therapist)
    child_id = await _create_child(client, parent, name="Complete")

    create = await client.post(
        "/api/v1/practice-plans",
        json={
            "child_id": child_id,
            "title": "Complete plan",
            "items": [
                {"exercise_id": catalogue["articulation_default"], "target_count": 3}
            ],
        },
        headers=therapist,
    )
    plan_id = create.json()["id"]
    item_id = create.json()["items"][0]["id"]

    # Parent advances progress for their own child.
    step1 = await client.post(
        f"/api/v1/practice-plans/{plan_id}/items/{item_id}/complete",
        json={"increment": 1, "notes": "1-kun"},
        headers=parent,
    )
    assert step1.status_code == 200
    assert step1.json()["completed_count"] == 1
    assert step1.json()["status"] == "in_progress"

    step2 = await client.post(
        f"/api/v1/practice-plans/{plan_id}/items/{item_id}/complete",
        json={"increment": 5},  # exceeds target; capped at 3
        headers=parent,
    )
    assert step2.status_code == 200
    assert step2.json()["completed_count"] == 3
    assert step2.json()["status"] == "completed"
    assert step2.json()["completed_at"] is not None
    assert "1-kun" in (step2.json()["notes"] or "")


async def test_complete_item_blocked_when_skipped(client) -> None:
    therapist = await _therapist_headers(client, "plan-skipped-th@sado.uz")
    _, parent = await _register_login(client, "plan-skipped-p@sado.uz")
    catalogue = await _seed_default_catalogue(client, therapist)
    child_id = await _create_child(client, parent, name="Skipped")

    create = await client.post(
        "/api/v1/practice-plans",
        json={
            "child_id": child_id,
            "title": "Skipped plan",
            "items": [
                {"exercise_id": catalogue["fluency"], "target_count": 2}
            ],
        },
        headers=therapist,
    )
    plan_id = create.json()["id"]
    item_id = create.json()["items"][0]["id"]

    skip = await client.put(
        f"/api/v1/practice-plans/{plan_id}/items/{item_id}",
        json={"status": "skipped"},
        headers=therapist,
    )
    assert skip.status_code == 200
    assert skip.json()["status"] == "skipped"

    response = await client.post(
        f"/api/v1/practice-plans/{plan_id}/items/{item_id}/complete",
        json={"increment": 1},
        headers=parent,
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "ITEM_SKIPPED"


async def test_other_parent_cannot_progress_plan(client) -> None:
    therapist = await _therapist_headers(client, "plan-cross-th@sado.uz")
    catalogue = await _seed_default_catalogue(client, therapist)
    _, owner = await _register_login(client, "plan-cross-owner@sado.uz")
    child_id = await _create_child(client, owner, name="Owner")

    create = await client.post(
        "/api/v1/practice-plans",
        json={
            "child_id": child_id,
            "title": "Owner plan",
            "items": [
                {"exercise_id": catalogue["articulation_default"], "target_count": 2}
            ],
        },
        headers=therapist,
    )
    plan_id = create.json()["id"]
    item_id = create.json()["items"][0]["id"]

    _, intruder = await _register_login(client, "plan-cross-intruder@sado.uz")

    detail = await client.get(
        f"/api/v1/practice-plans/{plan_id}", headers=intruder
    )
    assert detail.status_code == 403

    progress = await client.post(
        f"/api/v1/practice-plans/{plan_id}/items/{item_id}/complete",
        json={"increment": 1},
        headers=intruder,
    )
    assert progress.status_code == 403


async def test_update_item_capping(client) -> None:
    """``completed_count`` is capped at ``target_count`` even if the
    therapist tries to set a larger value via PUT."""

    therapist = await _therapist_headers(client, "plan-cap-th@sado.uz")
    _, parent = await _register_login(client, "plan-cap-p@sado.uz")
    catalogue = await _seed_default_catalogue(client, therapist)
    child_id = await _create_child(client, parent, name="Cap")

    create = await client.post(
        "/api/v1/practice-plans",
        json={
            "child_id": child_id,
            "title": "Cap plan",
            "items": [
                {"exercise_id": catalogue["breathing"], "target_count": 4}
            ],
        },
        headers=therapist,
    )
    plan_id = create.json()["id"]
    item_id = create.json()["items"][0]["id"]

    response = await client.put(
        f"/api/v1/practice-plans/{plan_id}/items/{item_id}",
        json={"completed_count": 99},
        headers=therapist,
    )
    assert response.status_code == 200
    assert response.json()["completed_count"] == 4


async def test_get_plan_404_for_missing_id(client) -> None:
    therapist = await _therapist_headers(client, "plan-missing-th@sado.uz")
    response = await client.get(
        "/api/v1/practice-plans/00000000-0000-0000-0000-000000000000",
        headers=therapist,
    )
    assert response.status_code == 404
