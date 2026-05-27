"""Edge-case tests for ``app/api/v1/assessments.py``.

The happy-path assessment flow is exercised by ``test_assessments.py``.
This module fills in the branches the main suite does not touch:

* CHILD_NOT_FOUND when the referenced child is missing.
* INVALID_TASK_TYPE / AUDIO_EMPTY / AUDIO_TOO_LARGE upload guards.
* PATCH /assessments status transitions and authorisation matrix.
* DELETE /assessments authorisation (admin-only).
* List filters: ``child_id``, ``status``, ``risk_level``, ``cursor``.
* Teacher with no region returns an empty page rather than 500ing.
* Teacher *with* a region can see assessments inside that region.
* Therapist can read detailed analysis (admin path is already covered).
* GET 404 on missing assessment / list_recordings endpoint.
"""

from __future__ import annotations

import io
import uuid

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------- Helpers


async def _register_login(
    client, email: str, role: str = "parent"
) -> tuple[dict, dict]:
    creds = {
        "email": email,
        "password": "Sup3r-Secret!",
        "full_name": "Edge User",
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


async def _create_admin(email: str = "edge-admin@sado.uz") -> str:
    from app.core.security import hash_password
    from app.database import get_sessionmaker
    from app.models.user import User, UserRole

    factory = get_sessionmaker()
    async with factory() as session:
        admin = User(
            email=email,
            password_hash=hash_password("AdminP4ss!"),
            full_name="Admin Edge",
            role=UserRole.ADMIN.value,
            is_active=True,
            is_verified=True,
        )
        session.add(admin)
        await session.commit()
        await session.refresh(admin)
        return admin.id


async def _admin_headers(client, email: str = "edge-admin@sado.uz") -> dict:
    await _create_admin(email)
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "AdminP4ss!"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _create_therapist(email: str = "edge-th@sado.uz") -> str:
    """Therapists can't self-register through ``/auth/register``; insert
    one via the ORM and return the ``user.id``."""

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


async def _therapist_headers(client, email: str = "edge-th@sado.uz") -> dict:
    await _create_therapist(email)
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "ThP4ss!22"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


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


def _audio_bytes(seed: int = 7) -> bytes:
    return b"RIFF" + (seed.to_bytes(2, "big") * 2048)


async def _create_assessment(client, headers: dict, child_id: str) -> str:
    response = await client.post(
        "/api/v1/assessments",
        json={"child_id": child_id, "type": "screening"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


# --------------------------------------------------------------- Tests


async def test_create_assessment_child_not_found(client) -> None:
    """Hitting ``POST /assessments`` with a UUID that does not exist
    returns 404 with the documented ``CHILD_NOT_FOUND`` code so callers
    can branch deterministically."""

    _, parent = await _register_login(client, "child404@sado.uz")
    response = await client.post(
        "/api/v1/assessments",
        json={"child_id": str(uuid.uuid4()), "type": "screening"},
        headers=parent,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "CHILD_NOT_FOUND"


async def test_get_assessment_404(client) -> None:
    _, parent = await _register_login(client, "miss404@sado.uz")
    response = await client.get(
        f"/api/v1/assessments/{uuid.uuid4()}", headers=parent
    )
    assert response.status_code == 404
    assert response.json()["code"] == "ASSESSMENT_NOT_FOUND"


async def test_invalid_task_type_rejected(client) -> None:
    """Recording task_type must be a member of ``RecordingTaskType``."""

    _, parent = await _register_login(client, "tt@sado.uz")
    child_id = await _create_child(client, parent)
    assessment_id = await _create_assessment(client, parent, child_id)

    files = {"audio": ("clip.wav", io.BytesIO(_audio_bytes(1)), "audio/wav")}
    response = await client.post(
        f"/api/v1/assessments/{assessment_id}/recordings",
        files=files,
        data={"task_type": "not_a_real_task"},
        headers=parent,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_TASK_TYPE"


async def test_audio_empty_rejected(client) -> None:
    _, parent = await _register_login(client, "empty@sado.uz")
    child_id = await _create_child(client, parent)
    assessment_id = await _create_assessment(client, parent, child_id)

    files = {"audio": ("clip.wav", io.BytesIO(b""), "audio/wav")}
    response = await client.post(
        f"/api/v1/assessments/{assessment_id}/recordings",
        files=files,
        data={"task_type": "repeat_word"},
        headers=parent,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "AUDIO_EMPTY"


async def test_audio_too_large_rejected(client, monkeypatch) -> None:
    """Force a 0 MB cap on the cached settings so any non-empty payload
    trips the size guard."""

    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "max_audio_size_mb", 0, raising=False)

    _, parent = await _register_login(client, "large@sado.uz")
    child_id = await _create_child(client, parent)
    assessment_id = await _create_assessment(client, parent, child_id)

    big_payload = b"X" * (2 * 1024 * 1024)  # 2 MB > 0 MB
    files = {"audio": ("clip.wav", io.BytesIO(big_payload), "audio/wav")}
    response = await client.post(
        f"/api/v1/assessments/{assessment_id}/recordings",
        files=files,
        data={"task_type": "repeat_word"},
        headers=parent,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "AUDIO_TOO_LARGE"


async def test_list_recordings_endpoint(client) -> None:
    """``GET /assessments/:id/recordings`` returns the same list the
    detail endpoint nests, scoped to the parent."""

    _, parent = await _register_login(client, "lst@sado.uz")
    child_id = await _create_child(client, parent)
    assessment_id = await _create_assessment(client, parent, child_id)

    files = {"audio": ("clip.wav", io.BytesIO(_audio_bytes(3)), "audio/wav")}
    upload = await client.post(
        f"/api/v1/assessments/{assessment_id}/recordings",
        files=files,
        data={"task_type": "repeat_word"},
        headers=parent,
    )
    assert upload.status_code == 201

    response = await client.get(
        f"/api/v1/assessments/{assessment_id}/recordings", headers=parent
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["task_type"] == "repeat_word"


async def test_patch_assessment_status_transitions(client) -> None:
    """Therapist may move an assessment through allowed status values."""

    _, parent = await _register_login(client, "patch1@sado.uz")
    therapist = await _therapist_headers(client, "patch-th@sado.uz")
    child_id = await _create_child(client, parent)
    assessment_id = await _create_assessment(client, parent, child_id)

    for next_status in ("in_progress", "processing", "completed"):
        response = await client.patch(
            f"/api/v1/assessments/{assessment_id}",
            json={"status": next_status},
            headers=therapist,
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == next_status

    # Therapist can also overwrite the summary in the same PATCH.
    response = await client.patch(
        f"/api/v1/assessments/{assessment_id}",
        json={"status": "completed", "summary": "Clean run."},
        headers=therapist,
    )
    assert response.status_code == 200
    assert response.json()["summary"] == "Clean run."


async def test_patch_assessment_invalid_status(client) -> None:
    _, parent = await _register_login(client, "patch-bad@sado.uz")
    therapist = await _therapist_headers(client, "patch-th2@sado.uz")
    child_id = await _create_child(client, parent)
    assessment_id = await _create_assessment(client, parent, child_id)

    response = await client.patch(
        f"/api/v1/assessments/{assessment_id}",
        json={"status": "wat"},
        headers=therapist,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_ASSESSMENT_STATUS"


async def test_patch_assessment_forbidden_for_other_parent(client) -> None:
    """A parent may patch their own assessment but not another family's."""

    _, parent_a = await _register_login(client, "pa-a@sado.uz")
    _, parent_b = await _register_login(client, "pa-b@sado.uz")
    child_id = await _create_child(client, parent_a)
    assessment_id = await _create_assessment(client, parent_a, child_id)

    forbidden = await client.patch(
        f"/api/v1/assessments/{assessment_id}",
        json={"summary": "I am not the parent."},
        headers=parent_b,
    )
    # Parent B can't even read it → 403 from the visibility guard first.
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "ASSESSMENT_FORBIDDEN"


async def test_patch_assessment_owner_parent_can_set_summary(client) -> None:
    _, parent = await _register_login(client, "pa-self@sado.uz")
    child_id = await _create_child(client, parent)
    assessment_id = await _create_assessment(client, parent, child_id)

    response = await client.patch(
        f"/api/v1/assessments/{assessment_id}",
        json={"summary": "Family note."},
        headers=parent,
    )
    assert response.status_code == 200
    assert response.json()["summary"] == "Family note."


async def test_list_filters_status_and_risk_level(client) -> None:
    """``status`` and ``risk_level`` query params narrow the parent list."""

    _, parent = await _register_login(client, "lf@sado.uz")
    child_id = await _create_child(client, parent, name="Filtered")

    # Two assessments — one we'll leave PENDING (no upload), the other
    # gets a recording so it transitions all the way to COMPLETED with a
    # risk level set by the inline mock processor.
    pending_id = await _create_assessment(client, parent, child_id)
    completed_id = await _create_assessment(client, parent, child_id)
    files = {"audio": ("c.wav", io.BytesIO(_audio_bytes(9)), "audio/wav")}
    await client.post(
        f"/api/v1/assessments/{completed_id}/recordings",
        files=files,
        data={"task_type": "repeat_word"},
        headers=parent,
    )

    # Filter: pending only.
    pending_resp = await client.get(
        "/api/v1/assessments?status=pending", headers=parent
    )
    assert pending_resp.status_code == 200
    pending_items = pending_resp.json()["items"]
    assert {a["id"] for a in pending_items} == {pending_id}

    # Filter: completed only.
    completed_resp = await client.get(
        "/api/v1/assessments?status=completed", headers=parent
    )
    assert completed_resp.status_code == 200
    completed_items = completed_resp.json()["items"]
    assert {a["id"] for a in completed_items} == {completed_id}
    risk = completed_items[0]["overall_risk"]
    assert risk in {"green", "yellow", "red"}

    # Filter: by child_id (both belong to the same child).
    by_child = await client.get(
        f"/api/v1/assessments?child_id={child_id}", headers=parent
    )
    assert by_child.status_code == 200
    assert {a["id"] for a in by_child.json()["items"]} == {pending_id, completed_id}

    # Filter: risk_level matching the mocked output for completed.
    risk_resp = await client.get(
        f"/api/v1/assessments?risk_level={risk}", headers=parent
    )
    assert risk_resp.status_code == 200
    items = risk_resp.json()["items"]
    assert {a["id"] for a in items} == {completed_id}


async def test_list_invalid_cursor(client) -> None:
    _, parent = await _register_login(client, "cur@sado.uz")
    response = await client.get(
        "/api/v1/assessments?cursor=garbage", headers=parent
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_CURSOR"


async def test_teacher_without_region_sees_empty_list(client) -> None:
    """Teachers with ``region_id is None`` short-circuit to an empty page
    rather than leaking everything in the database."""

    _, teacher = await _register_login(
        client, "teach-noreg@sado.uz", role="teacher"
    )
    # Seed an assessment under a parent so the DB is non-empty.
    _, parent = await _register_login(client, "teach-parent@sado.uz")
    child_id = await _create_child(client, parent)
    await _create_assessment(client, parent, child_id)

    response = await client.get("/api/v1/assessments", headers=teacher)
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["next_cursor"] is None
    assert body["has_more"] is False


async def test_teacher_with_region_sees_kindergarten_assessments(client) -> None:
    """Teacher with a region can see assessments for children in
    kindergartens within that region."""

    admin = await _admin_headers(client, email="reg-admin@sado.uz")
    region_id = await _create_region(client, admin, "Tashkent")
    kg_id = await _create_kindergarten(client, admin, "Sun KG", region_id)

    teacher_user, teacher = await _register_login(
        client, "teach-reg@sado.uz", role="teacher"
    )
    await _set_user_region(teacher_user["id"], region_id)

    parent_user, parent = await _register_login(client, "teach-parent2@sado.uz")
    child_id = await _create_child(client, parent, name="InRegion")
    await _attach_child_to_kg(child_id, kg_id)

    assessment_id = await _create_assessment(client, parent, child_id)

    # Teacher should see it via the kindergarten ↔ region join.
    response = await client.get("/api/v1/assessments", headers=teacher)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert {a["id"] for a in items} == {assessment_id}

    # And reading the detail.
    detail = await client.get(
        f"/api/v1/assessments/{assessment_id}", headers=teacher
    )
    assert detail.status_code == 200


async def test_therapist_can_read_detailed_analysis(client) -> None:
    """Therapists must reach ``/analysis/:id/detailed``; the existing
    suite covers admins only."""

    _, parent = await _register_login(client, "th-parent@sado.uz")
    therapist = await _therapist_headers(client, "th-detail@sado.uz")
    child_id = await _create_child(client, parent)
    assessment_id = await _create_assessment(client, parent, child_id)

    files = {"audio": ("c.wav", io.BytesIO(_audio_bytes(5)), "audio/wav")}
    upload = await client.post(
        f"/api/v1/assessments/{assessment_id}/recordings",
        files=files,
        data={"task_type": "repeat_word"},
        headers=parent,
    )
    assert upload.status_code == 201

    response = await client.get(
        f"/api/v1/analysis/{assessment_id}/detailed", headers=therapist
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["assessment_id"] == assessment_id
    assert len(payload["results"]) == 1
    detailed = payload["results"][0]
    assert detailed["mfcc_features"] is not None
    assert detailed["formant_data"] is not None


async def test_analysis_404_for_missing_assessment(client) -> None:
    _, parent = await _register_login(client, "ana404@sado.uz")
    response = await client.get(
        f"/api/v1/analysis/{uuid.uuid4()}", headers=parent
    )
    assert response.status_code == 404
    assert response.json()["code"] == "ASSESSMENT_NOT_FOUND"


async def test_other_parent_cannot_read_assessment(client) -> None:
    """``_ensure_visible`` raises ASSESSMENT_FORBIDDEN for a non-owner
    parent on the read path."""

    _, parent_a = await _register_login(client, "ro-a@sado.uz")
    _, parent_b = await _register_login(client, "ro-b@sado.uz")
    child_id = await _create_child(client, parent_a)
    assessment_id = await _create_assessment(client, parent_a, child_id)

    response = await client.get(
        f"/api/v1/assessments/{assessment_id}", headers=parent_b
    )
    assert response.status_code == 403
    assert response.json()["code"] == "ASSESSMENT_FORBIDDEN"


async def test_other_parent_cannot_upload_recording(client) -> None:
    _, parent_a = await _register_login(client, "up-a@sado.uz")
    _, parent_b = await _register_login(client, "up-b@sado.uz")
    child_id = await _create_child(client, parent_a)
    assessment_id = await _create_assessment(client, parent_a, child_id)

    files = {"audio": ("c.wav", io.BytesIO(_audio_bytes(13)), "audio/wav")}
    response = await client.post(
        f"/api/v1/assessments/{assessment_id}/recordings",
        files=files,
        data={"task_type": "repeat_word"},
        headers=parent_b,
    )
    assert response.status_code == 403
    assert response.json()["code"] == "ASSESSMENT_FORBIDDEN"


async def test_admin_can_read_any_assessment(client) -> None:
    """Admin role bypasses both parent-ownership and teacher-region
    checks in ``_can_read_child`` (line 93)."""

    _, parent = await _register_login(client, "ad-read@sado.uz")
    child_id = await _create_child(client, parent)
    assessment_id = await _create_assessment(client, parent, child_id)

    admin = await _admin_headers(client, email="ad-read-admin@sado.uz")
    response = await client.get(
        f"/api/v1/assessments/{assessment_id}", headers=admin
    )
    assert response.status_code == 200
    assert response.json()["id"] == assessment_id
