"""End-to-end tests for the child-progress timeline endpoint."""

from __future__ import annotations

import io

import pytest

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------- Helpers


async def _register_login(
    client, email: str, role: str = "parent"
) -> tuple[dict, dict]:
    creds = {
        "email": email,
        "password": "Sup3r-Secret!",
        "full_name": "Progress Tester",
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


# --------------------------------------------------------------- Tests


async def test_progress_returns_empty_for_child_without_assessments(client) -> None:
    """A new child surfaces an empty timeline rather than 404."""

    _, parent = await _register_login(client, "prog-empty@sado.uz")
    child_id = await _create_child(client, parent, name="Newborn")

    response = await client.get(
        f"/api/v1/children/{child_id}/progress", headers=parent
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["child_id"] == child_id
    assert body["points"] == []
    assert body["summary"]["total_assessments"] == 0
    assert body["summary"]["completed_assessments"] == 0
    assert body["summary"]["latest_risk"] is None
    assert body["summary"]["risk_distribution"] == {
        "green": 0,
        "yellow": 0,
        "red": 0,
    }


async def test_progress_aggregates_completed_assessments(client, monkeypatch) -> None:
    """Each completed assessment becomes one chronological data point."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    _, parent = await _register_login(client, "prog-agg@sado.uz")
    child_id = await _create_child(client, parent, name="Lola")

    a1 = await _new_assessment_with_recording(client, parent, child_id, seed=1)
    a2 = await _new_assessment_with_recording(client, parent, child_id, seed=21)
    a3 = await _new_assessment_with_recording(client, parent, child_id, seed=53)

    response = await client.get(
        f"/api/v1/children/{child_id}/progress", headers=parent
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["summary"]["total_assessments"] == 3
    assert body["summary"]["completed_assessments"] == 3
    assert body["summary"]["last_completed_at"] is not None

    points = body["points"]
    assert len(points) == 3
    # Oldest first — IDs returned in creation order.
    assert [p["assessment_id"] for p in points] == [a1, a2, a3]

    sample = points[0]
    assert {
        "assessment_id",
        "assessment_type",
        "completed_at",
        "created_at",
        "status",
        "overall_risk",
        "overall_confidence",
        "jitter_local_pct",
        "shimmer_local_pct",
        "hnr_db",
        "speech_rate_wpm",
        "voice_quality_flags",
        "weakest_phonemes",
        "recording_count",
    } <= set(sample.keys())

    # Mock backend always emits voice-quality numbers, so the means are populated.
    assert isinstance(sample["jitter_local_pct"], int | float)
    assert sample["jitter_local_pct"] > 0
    assert isinstance(sample["voice_quality_flags"], list)
    assert isinstance(sample["weakest_phonemes"], list)
    assert sample["recording_count"] == 1


async def test_progress_summary_counts_risks(client, monkeypatch) -> None:
    """The risk_distribution rolls up the per-assessment risk levels."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    _, parent = await _register_login(client, "prog-risk@sado.uz")
    child_id = await _create_child(client, parent, name="Bobur")

    for seed in (3, 17, 41):
        await _new_assessment_with_recording(client, parent, child_id, seed=seed)

    response = await client.get(
        f"/api/v1/children/{child_id}/progress", headers=parent
    )
    assert response.status_code == 200
    summary = response.json()["summary"]
    risk_total = sum(summary["risk_distribution"].values())
    # Every completed assessment contributes exactly once.
    assert risk_total == summary["completed_assessments"]
    assert summary["latest_risk"] in {"green", "yellow", "red"}
    assert isinstance(summary["latest_confidence"], int | float)


async def test_progress_other_parent_is_forbidden(client) -> None:
    """A different parent cannot read another parent's child timeline."""

    _, owner = await _register_login(client, "prog-owner@sado.uz")
    child_id = await _create_child(client, owner, name="Sevinch")

    _, intruder = await _register_login(client, "prog-intruder@sado.uz")
    response = await client.get(
        f"/api/v1/children/{child_id}/progress", headers=intruder
    )
    assert response.status_code == 403, response.text


async def test_progress_unknown_child_returns_404(client) -> None:
    _, parent = await _register_login(client, "prog-404@sado.uz")
    response = await client.get(
        "/api/v1/children/00000000-0000-0000-0000-000000000000/progress",
        headers=parent,
    )
    assert response.status_code == 404


async def test_progress_limit_param_caps_points(client, monkeypatch) -> None:
    """The ``?limit=`` query param truncates the timeline length."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    _, parent = await _register_login(client, "prog-limit@sado.uz")
    child_id = await _create_child(client, parent, name="Karim")

    for seed in (5, 9, 13, 19):
        await _new_assessment_with_recording(client, parent, child_id, seed=seed)

    response = await client.get(
        f"/api/v1/children/{child_id}/progress?limit=2", headers=parent
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["points"]) == 2
    # ``total_assessments`` reflects the points returned, not the table count.
    assert body["summary"]["total_assessments"] == 2
