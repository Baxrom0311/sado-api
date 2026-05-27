"""End-to-end tests for assessments, audio uploads, and analysis."""

from __future__ import annotations

import io

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


async def _create_admin(email: str = "admin@sado.uz"):
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


async def _admin_headers(client, email: str = "admin@sado.uz"):
    await _create_admin(email)
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "AdminP4ss!"},
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


def _audio_bytes(seed: int = 7) -> bytes:
    """Synthetic deterministic byte payload that masquerades as audio."""

    return b"RIFF" + (seed.to_bytes(2, "big") * 2048)


# ----------------------------------------------------------- Tests


async def test_create_assessment_requires_owned_child(client) -> None:
    _, parent_a = await _register_login(client, "a@sado.uz")
    _, parent_b = await _register_login(client, "b@sado.uz")

    child_id = await _create_child(client, parent_a)

    forbidden = await client.post(
        "/api/v1/assessments",
        json={"child_id": child_id, "type": "screening"},
        headers=parent_b,
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "ASSESSMENT_FORBIDDEN"


async def test_create_assessment_invalid_type(client) -> None:
    _, parent = await _register_login(client, "p1@sado.uz")
    child_id = await _create_child(client, parent)

    response = await client.post(
        "/api/v1/assessments",
        json={"child_id": child_id, "type": "wat"},
        headers=parent,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_ASSESSMENT_TYPE"


async def test_full_assessment_flow_inline(client) -> None:
    _, parent = await _register_login(client, "p2@sado.uz")
    child_id = await _create_child(client, parent, name="Malika")

    create = await client.post(
        "/api/v1/assessments",
        json={"child_id": child_id, "type": "screening"},
        headers=parent,
    )
    assert create.status_code == 201, create.text
    assessment_id = create.json()["id"]
    assert create.json()["status"] == "pending"

    # Upload one recording — eager mode triggers inline processing.
    files = {
        "audio": ("clip.wav", io.BytesIO(_audio_bytes(7)), "audio/wav"),
    }
    data = {"task_type": "repeat_word", "prompt": "olma"}
    upload = await client.post(
        f"/api/v1/assessments/{assessment_id}/recordings",
        files=files,
        data=data,
        headers=parent,
    )
    assert upload.status_code == 201, upload.text
    rec = upload.json()
    assert rec["task_type"] == "repeat_word"
    assert rec["size_bytes"] > 0

    # Assessment should now be COMPLETED with a risk level set.
    detail = await client.get(
        f"/api/v1/assessments/{assessment_id}", headers=parent
    )
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "completed"
    assert body["overall_risk"] in {"green", "yellow", "red"}
    assert isinstance(body["overall_confidence"], float)
    assert len(body["recordings"]) == 1
    assert body["recordings"][0]["processed"] is True

    # Parent-safe analysis view.
    analysis = await client.get(
        f"/api/v1/analysis/{assessment_id}", headers=parent
    )
    assert analysis.status_code == 200
    payload = analysis.json()
    assert payload["assessment_id"] == assessment_id
    assert payload["overall_risk"] in {"green", "yellow", "red"}
    assert len(payload["results"]) == 1
    result = payload["results"][0]
    assert result["risk_level"] in {"green", "yellow", "red"}
    # Parent view must NOT include raw acoustic features.
    assert "mfcc_features" not in result


async def test_detailed_analysis_requires_therapist_or_admin(client) -> None:
    _, parent = await _register_login(client, "p3@sado.uz")
    child_id = await _create_child(client, parent, name="Sevinch")

    create = await client.post(
        "/api/v1/assessments",
        json={"child_id": child_id, "type": "screening"},
        headers=parent,
    )
    assessment_id = create.json()["id"]

    files = {"audio": ("clip.wav", io.BytesIO(_audio_bytes(11)), "audio/wav")}
    upload = await client.post(
        f"/api/v1/assessments/{assessment_id}/recordings",
        files=files,
        data={"task_type": "repeat_word"},
        headers=parent,
    )
    assert upload.status_code == 201

    forbidden = await client.get(
        f"/api/v1/analysis/{assessment_id}/detailed", headers=parent
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "DETAILED_FORBIDDEN"

    admin = await _admin_headers(client)
    detailed = await client.get(
        f"/api/v1/analysis/{assessment_id}/detailed", headers=admin
    )
    assert detailed.status_code == 200, detailed.text
    payload = detailed.json()
    assert len(payload["results"]) == 1
    result = payload["results"][0]
    assert "mfcc_features" in result and result["mfcc_features"] is not None
    assert "phoneme_scores" in result and result["phoneme_scores"] is not None
    assert "tracks" in result["formant_data"]


async def test_invalid_audio_type_rejected(client) -> None:
    _, parent = await _register_login(client, "p4@sado.uz")
    child_id = await _create_child(client, parent)

    create = await client.post(
        "/api/v1/assessments",
        json={"child_id": child_id, "type": "screening"},
        headers=parent,
    )
    assessment_id = create.json()["id"]

    files = {"audio": ("clip.txt", io.BytesIO(b"hello"), "text/plain")}
    response = await client.post(
        f"/api/v1/assessments/{assessment_id}/recordings",
        files=files,
        data={"task_type": "repeat_word"},
        headers=parent,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_AUDIO_TYPE"


async def test_audio_too_long_rejected(client) -> None:
    _, parent = await _register_login(client, "p5@sado.uz")
    child_id = await _create_child(client, parent)

    create = await client.post(
        "/api/v1/assessments",
        json={"child_id": child_id, "type": "screening"},
        headers=parent,
    )
    assessment_id = create.json()["id"]

    files = {"audio": ("clip.wav", io.BytesIO(_audio_bytes(2)), "audio/wav")}
    response = await client.post(
        f"/api/v1/assessments/{assessment_id}/recordings",
        files=files,
        data={"task_type": "repeat_word", "duration_sec": "120"},
        headers=parent,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "AUDIO_TOO_LONG"


async def test_list_assessments_scoped_to_parent(client) -> None:
    _, parent_a = await _register_login(client, "lp1@sado.uz")
    _, parent_b = await _register_login(client, "lp2@sado.uz")

    child_a = await _create_child(client, parent_a, name="Anvar")
    child_b = await _create_child(client, parent_b, name="Bek")

    for hdr, child_id in [(parent_a, child_a), (parent_b, child_b)]:
        await client.post(
            "/api/v1/assessments",
            json={"child_id": child_id, "type": "screening"},
            headers=hdr,
        )

    list_a = await client.get("/api/v1/assessments", headers=parent_a)
    assert list_a.status_code == 200
    items_a = list_a.json()["items"]
    assert len(items_a) == 1
    assert items_a[0]["child_id"] == child_a

    list_b = await client.get("/api/v1/assessments", headers=parent_b)
    assert list_b.status_code == 200
    assert len(list_b.json()["items"]) == 1
    assert list_b.json()["items"][0]["child_id"] == child_b


async def test_admin_can_delete_assessment(client) -> None:
    _, parent = await _register_login(client, "p6@sado.uz")
    child_id = await _create_child(client, parent)
    create = await client.post(
        "/api/v1/assessments",
        json={"child_id": child_id, "type": "screening"},
        headers=parent,
    )
    assessment_id = create.json()["id"]

    bad = await client.delete(
        f"/api/v1/assessments/{assessment_id}", headers=parent
    )
    assert bad.status_code == 403

    admin = await _admin_headers(client)
    ok = await client.delete(
        f"/api/v1/assessments/{assessment_id}", headers=admin
    )
    assert ok.status_code == 204

    missing = await client.get(
        f"/api/v1/assessments/{assessment_id}", headers=admin
    )
    assert missing.status_code == 404


async def test_recording_processing_is_deterministic(client) -> None:
    """Same audio bytes should produce same risk classification."""

    _, parent = await _register_login(client, "p7@sado.uz")
    child_id = await _create_child(client, parent)

    risk_levels = []
    for _ in range(2):
        create = await client.post(
            "/api/v1/assessments",
            json={"child_id": child_id, "type": "screening"},
            headers=parent,
        )
        assessment_id = create.json()["id"]
        files = {
            "audio": ("clip.wav", io.BytesIO(_audio_bytes(42)), "audio/wav"),
        }
        upload = await client.post(
            f"/api/v1/assessments/{assessment_id}/recordings",
            files=files,
            data={"task_type": "repeat_word"},
            headers=parent,
        )
        assert upload.status_code == 201
        detail = await client.get(
            f"/api/v1/assessments/{assessment_id}", headers=parent
        )
        risk_levels.append(detail.json()["overall_risk"])

    assert risk_levels[0] == risk_levels[1]
    assert risk_levels[0] in {"green", "yellow", "red"}



# --------------------------------------------------------------------------
# N+1 regression: /analysis/:id/detailed must not issue one query per
# recording for the analysis_results table. We attach a SQLAlchemy event
# listener that records every SELECT against analysis_results during the
# request, then assert there is exactly one such query regardless of the
# number of recordings (selectinload uses a single IN-clause SELECT).
# --------------------------------------------------------------------------


async def _seed_assessment_with_recordings(
    client, parent_headers: dict, child_id: str, n_recordings: int
) -> str:
    create = await client.post(
        "/api/v1/assessments",
        json={"child_id": child_id, "type": "screening"},
        headers=parent_headers,
    )
    assert create.status_code == 201, create.text
    assessment_id = create.json()["id"]

    for idx in range(n_recordings):
        files = {
            "audio": (
                f"clip{idx}.wav",
                io.BytesIO(_audio_bytes(idx + 1)),
                "audio/wav",
            ),
        }
        upload = await client.post(
            f"/api/v1/assessments/{assessment_id}/recordings",
            files=files,
            data={"task_type": "repeat_word", "prompt": f"prompt-{idx}"},
            headers=parent_headers,
        )
        assert upload.status_code == 201, upload.text

    return assessment_id


def _attach_query_recorder(table_name: str) -> tuple[list[str], callable]:
    """Subscribe to ``before_cursor_execute`` on the active async engine.

    Captures every SELECT statement that mentions ``table_name``. The
    cleanup callable removes the listener so adjacent tests aren't
    polluted with stale subscribers.
    """

    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import AsyncEngine

    from app.database import get_engine

    captured: list[str] = []

    def _listener(_conn, _cursor, statement, _params, _context, _executemany):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select") and table_name in normalized:
            captured.append(statement)

    async_engine: AsyncEngine = get_engine()
    event.listen(async_engine.sync_engine, "before_cursor_execute", _listener)

    def _cleanup() -> None:
        try:
            event.remove(
                async_engine.sync_engine, "before_cursor_execute", _listener
            )
        except Exception:
            pass

    return captured, _cleanup


async def test_detailed_analysis_no_n_plus_one_on_analysis_results(
    client,
) -> None:
    """``/analysis/:id/detailed`` must issue exactly one analysis_results SELECT.

    SQLAlchemy's ``selectinload`` collapses related-row loads into a
    single IN-clause query. If a future refactor reverts to per-recording
    fetches the count below will jump to N and this test fails.
    """

    _, parent = await _register_login(client, "n_plus_one@sado.uz")
    child_id = await _create_child(client, parent, name="QueryCounter")
    admin = await _admin_headers(client)

    # ---- Baseline: 3 recordings ----
    assessment_id_3 = await _seed_assessment_with_recordings(
        client, parent, child_id, n_recordings=3
    )
    captured_3, cleanup_3 = _attach_query_recorder("analysis_results")
    try:
        response = await client.get(
            f"/api/v1/analysis/{assessment_id_3}/detailed", headers=admin
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["results"]) == 3
    finally:
        cleanup_3()

    # ---- Stress: 6 recordings ----
    assessment_id_6 = await _seed_assessment_with_recordings(
        client, parent, child_id, n_recordings=6
    )
    captured_6, cleanup_6 = _attach_query_recorder("analysis_results")
    try:
        response = await client.get(
            f"/api/v1/analysis/{assessment_id_6}/detailed", headers=admin
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["results"]) == 6
    finally:
        cleanup_6()

    # The selectinload chain issues ONE select against analysis_results
    # regardless of N. We intentionally pin to 1 so a regression to
    # per-recording fetches (which would yield N) is caught immediately.
    assert (
        len(captured_3) == 1
    ), f"baseline: expected 1 analysis_results SELECT, got {len(captured_3)}\n" + "\n".join(
        captured_3
    )
    assert (
        len(captured_6) == 1
    ), f"stress: expected 1 analysis_results SELECT, got {len(captured_6)}\n" + "\n".join(
        captured_6
    )
    # And the count must NOT scale with N — same shape for 3 vs 6.
    assert len(captured_6) == len(captured_3)


async def test_parent_analysis_no_n_plus_one_on_analysis_results(
    client,
) -> None:
    """The parent-safe ``/analysis/:id`` view shares the same eager-load path."""

    _, parent = await _register_login(client, "parent_analysis@sado.uz")
    child_id = await _create_child(client, parent, name="ParentView")

    assessment_id = await _seed_assessment_with_recordings(
        client, parent, child_id, n_recordings=4
    )
    captured, cleanup = _attach_query_recorder("analysis_results")
    try:
        response = await client.get(
            f"/api/v1/analysis/{assessment_id}", headers=parent
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["results"]) == 4
    finally:
        cleanup()

    assert (
        len(captured) == 1
    ), f"expected 1 analysis_results SELECT, got {len(captured)}\n" + "\n".join(
        captured
    )
