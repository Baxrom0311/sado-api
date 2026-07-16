"""End-to-end tests for locale-aware analysis responses.

These tests exercise the ``?locale=`` query param, the
``Accept-Language`` header fallback, and the user-preference fallback
on both the parent-safe and therapist-detailed analysis endpoints.
"""

from __future__ import annotations

import io

import pytest

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------- Helpers


async def _register_login(
    client, email: str, role: str = "parent", language: str = "uz"
) -> tuple[dict, dict]:
    creds = {
        "email": email,
        "password": "Sup3r-Secret!",
        "full_name": "Test User",
        "role": role,
        "language": language,
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


async def _create_admin(email: str = "loc-admin@sado.uz", language: str = "uz"):
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
            language=language,
            is_active=True,
            is_verified=True,
        )
        session.add(admin)
        await session.commit()


async def _admin_headers(client, email: str = "loc-admin@sado.uz", language: str = "uz"):
    await _create_admin(email, language=language)
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

    files = {"audio": ("clip.wav", io.BytesIO(_audio_bytes(11)), "audio/wav")}
    upload = await client.post(
        f"/api/v1/assessments/{assessment_id}/recordings",
        files=files,
        data={"task_type": "repeat_word", "duration_sec": "5.0"},
        headers=parent_headers,
    )
    assert upload.status_code == 201, upload.text
    return assessment_id


def _has_marker(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


# Distinctive locale markers — strings that only appear in one locale's
# message catalog and never collide with the others.
UZBEK_MARKERS = ("ovoz", "tovush", "logoped", "mashq", "ajoyib", "natija")
RUSSIAN_MARKERS = ("голос", "звук", "ребён", "результат", "упражн", "логопед", "отлично")
ENGLISH_MARKERS = ("voice", "sound", "speech-language", "great job", "results", "practice")


# --------------------------------------------------------------- Tests


async def test_locale_query_param_renders_russian(client, monkeypatch) -> None:
    """``?locale=ru`` re-renders the recommendations payload in Russian."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    _, parent = await _register_login(client, "loc-ru-parent@sado.uz")
    assessment_id = await _create_assessment_with_recording(
        client, parent, child_name="Sevinch"
    )

    response = await client.get(
        f"/api/v1/analysis/{assessment_id}?locale=ru", headers=parent
    )
    assert response.status_code == 200, response.text
    recs = response.json()["results"][0]["recommendations"]
    assert recs, "expected at least one recommendation"
    blob = " ".join(r["message"] for r in recs)
    assert _has_marker(blob, RUSSIAN_MARKERS), (
        f"recommendations should contain Russian markers, got: {blob}"
    )
    # Should NOT contain Uzbek-only markers any more.
    assert not _has_marker(
        blob, ("logoped", "ovoz", "ajoyib", "natija")
    ), f"unexpected Uzbek leakage in Russian payload: {blob}"


async def test_locale_query_param_renders_english(client, monkeypatch) -> None:
    """``?locale=en`` re-renders in English even when user prefers Uzbek."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    _, parent = await _register_login(client, "loc-en-parent@sado.uz")
    assessment_id = await _create_assessment_with_recording(
        client, parent, child_name="Madina"
    )

    response = await client.get(
        f"/api/v1/analysis/{assessment_id}?locale=en", headers=parent
    )
    assert response.status_code == 200, response.text
    recs = response.json()["results"][0]["recommendations"]
    assert recs
    blob = " ".join(r["message"] for r in recs)
    assert _has_marker(blob, ENGLISH_MARKERS), (
        f"recommendations should contain English markers, got: {blob}"
    )


async def test_locale_falls_back_to_accept_language_header(client, monkeypatch) -> None:
    """An ``Accept-Language: ru`` header switches the response to Russian."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    _, parent = await _register_login(client, "loc-accept-parent@sado.uz")
    assessment_id = await _create_assessment_with_recording(
        client, parent, child_name="Bobur"
    )

    headers = {**parent, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5"}
    response = await client.get(
        f"/api/v1/analysis/{assessment_id}", headers=headers
    )
    assert response.status_code == 200, response.text
    recs = response.json()["results"][0]["recommendations"]
    assert recs
    blob = " ".join(r["message"] for r in recs)
    assert _has_marker(blob, RUSSIAN_MARKERS), (
        f"Accept-Language header should drive locale, got: {blob}"
    )


async def test_locale_falls_back_to_user_preference(client, monkeypatch) -> None:
    """Without a query param or header, the user's saved language wins."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    _, parent = await _register_login(
        client, "loc-pref-parent@sado.uz", language="ru"
    )
    assessment_id = await _create_assessment_with_recording(
        client, parent, child_name="Dilnoza"
    )

    response = await client.get(f"/api/v1/analysis/{assessment_id}", headers=parent)
    assert response.status_code == 200, response.text
    recs = response.json()["results"][0]["recommendations"]
    assert recs
    blob = " ".join(r["message"] for r in recs)
    assert _has_marker(blob, RUSSIAN_MARKERS), (
        f"user.language='ru' should switch the default locale, got: {blob}"
    )


async def test_locale_default_is_uzbek_when_no_signal(client, monkeypatch) -> None:
    """No query, no header, no preference → fallback to Uzbek default."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    _, parent = await _register_login(client, "loc-default-parent@sado.uz")
    assessment_id = await _create_assessment_with_recording(
        client, parent, child_name="Otabek"
    )

    response = await client.get(f"/api/v1/analysis/{assessment_id}", headers=parent)
    assert response.status_code == 200, response.text
    recs = response.json()["results"][0]["recommendations"]
    assert recs
    blob = " ".join(r["message"] for r in recs)
    assert _has_marker(blob, UZBEK_MARKERS), (
        f"default locale should be Uzbek, got: {blob}"
    )


async def test_invalid_locale_falls_back_to_default(client, monkeypatch) -> None:
    """Unsupported codes (``zh``, ``de``…) silently fall back rather than 4xx."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    _, parent = await _register_login(client, "loc-bad-parent@sado.uz")
    assessment_id = await _create_assessment_with_recording(
        client, parent, child_name="Karim"
    )

    response = await client.get(
        f"/api/v1/analysis/{assessment_id}?locale=zh", headers=parent
    )
    assert response.status_code == 200, response.text
    recs = response.json()["results"][0]["recommendations"]
    assert recs
    # zh is not supported → falls back to user.language ('uz' default).
    blob = " ".join(r["message"] for r in recs)
    assert _has_marker(blob, UZBEK_MARKERS)


async def test_detailed_endpoint_supports_locale(client, monkeypatch) -> None:
    """The therapist deep view honours ``?locale=`` too."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    _, parent = await _register_login(client, "loc-detailed-parent@sado.uz")
    assessment_id = await _create_assessment_with_recording(
        client, parent, child_name="Komron"
    )

    admin = await _admin_headers(client, "loc-admin-detailed@sado.uz")
    response = await client.get(
        f"/api/v1/analysis/{assessment_id}/detailed?locale=en", headers=admin
    )
    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["voice_quality"] is not None
    recs = result["recommendations"]
    assert recs
    blob = " ".join(r["message"] for r in recs)
    assert _has_marker(blob, ENGLISH_MARKERS)


async def test_locale_query_takes_priority_over_header_and_user(
    client, monkeypatch
) -> None:
    """``?locale=`` wins over ``Accept-Language`` and over user preference."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    _, parent = await _register_login(
        client, "loc-priority-parent@sado.uz", language="uz"
    )
    assessment_id = await _create_assessment_with_recording(
        client, parent, child_name="Nigora"
    )

    headers = {**parent, "Accept-Language": "ru"}
    response = await client.get(
        f"/api/v1/analysis/{assessment_id}?locale=en", headers=headers
    )
    assert response.status_code == 200, response.text
    blob = " ".join(
        r["message"] for r in response.json()["results"][0]["recommendations"]
    )
    assert _has_marker(blob, ENGLISH_MARKERS)
    assert not _has_marker(blob, RUSSIAN_MARKERS)
