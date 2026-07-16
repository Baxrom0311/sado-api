"""End-to-end tests for voice-quality + recommendations on analysis responses."""

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


async def _create_admin(email: str = "vq-admin@sado.uz"):
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


async def _admin_headers(client, email: str = "vq-admin@sado.uz"):
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


def _audio_bytes(seed: int) -> bytes:
    """Stable pseudo-audio payload for a given seed."""

    return bytes((seed + i) % 256 for i in range(8000))


async def _create_assessment_with_recording(
    client, parent_headers: dict, *, child_name: str
) -> str:
    child_id = await _create_child(client, parent_headers, name=child_name)
    create = await client.post(
        "/api/v1/assessments",
        json={"child_id": child_id, "type": "screening"},
        headers=parent_headers,
    )
    assessment_id = create.json()["id"]

    files = {"audio": ("clip.wav", io.BytesIO(_audio_bytes(7)), "audio/wav")}
    upload = await client.post(
        f"/api/v1/assessments/{assessment_id}/recordings",
        files=files,
        data={"task_type": "repeat_word", "duration_sec": "5.0"},
        headers=parent_headers,
    )
    assert upload.status_code == 201, upload.text
    return assessment_id


# --------------------------------------------------------------- Tests


async def test_parent_analysis_response_includes_recommendations(client, monkeypatch) -> None:
    """Parent-safe view exposes localised recommendations but not raw features."""

    # Force the deterministic mock backend so the recommendation list is stable.
    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    _, parent = await _register_login(client, "vq-parent-1@sado.uz")
    assessment_id = await _create_assessment_with_recording(
        client, parent, child_name="Sevinch"
    )

    response = await client.get(
        f"/api/v1/analysis/{assessment_id}", headers=parent
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["results"], "expected at least one analysis"
    result = payload["results"][0]

    assert "recommendations" in result
    assert isinstance(result["recommendations"], list)
    assert result["recommendations"], "engine should produce at least one item"
    sample = result["recommendations"][0]
    assert {"code", "category", "severity", "message"} <= set(sample.keys())

    # Parent view must not leak the raw acoustic features.
    assert "voice_quality" not in result
    assert "mfcc_features" not in result


async def test_detailed_analysis_includes_voice_quality(client, monkeypatch) -> None:
    """Therapist/admin view includes voice_quality + recommendations."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    _, parent = await _register_login(client, "vq-parent-2@sado.uz")
    assessment_id = await _create_assessment_with_recording(
        client, parent, child_name="Madina"
    )

    admin = await _admin_headers(client)
    response = await client.get(
        f"/api/v1/analysis/{assessment_id}/detailed", headers=admin
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    result = payload["results"][0]

    # Voice quality must be a populated dict with the documented shape.
    vq = result["voice_quality"]
    assert vq is not None
    assert {"jitter_local_pct", "shimmer_local_pct", "hnr_db",
            "speech_rate_wpm", "voiced_seconds", "flags", "backend"} <= set(vq.keys())
    assert vq["backend"] == "mock"

    # Recommendations are present and localised in Uzbek (default).
    recs = result["recommendations"]
    assert recs is not None and len(recs) >= 1
    assert all(isinstance(r["message"], str) and r["message"] for r in recs)


async def test_recommendations_are_localised_uzbek(client, monkeypatch) -> None:
    """Default Uzbek copy ships in production responses."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    _, parent = await _register_login(client, "vq-parent-3@sado.uz")
    assessment_id = await _create_assessment_with_recording(
        client, parent, child_name="Bobur"
    )

    admin = await _admin_headers(client)
    response = await client.get(
        f"/api/v1/analysis/{assessment_id}/detailed", headers=admin
    )
    payload = response.json()
    recs = payload["results"][0]["recommendations"]

    # At least one recommendation should contain Uzbek-specific copy.
    text_blob = " ".join(r["message"] for r in recs)
    uz_markers = ("bola", "natija", "ovoz", "tovush", "logoped", "mashq", "ajoyib")
    assert any(marker in text_blob.lower() for marker in uz_markers), (
        f"recommendations should be in Uzbek, got: {text_blob}"
    )
