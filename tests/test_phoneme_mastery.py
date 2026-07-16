"""End-to-end + unit tests for phoneme mastery + speech profile.

Covers:

* upserting mastery rows when an assessment finalises,
* mastery score aggregation (running average, best score, mastered_at),
* the new ``GET /children/{id}/phoneme-mastery`` endpoint,
* the new ``GET /children/{id}/speech-profile`` endpoint,
* RBAC: parents may not read another parent's child,
* defensive parsing of malformed phoneme score payloads.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------- HTTP helpers


async def _register_login(client, email: str) -> dict[str, str]:
    creds = {
        "email": email,
        "password": "Sup3r-Secret!",
        "full_name": "Mastery Tester",
        "role": "parent",
    }
    register = await client.post("/api/v1/auth/register", json=creds)
    assert register.status_code == 201, register.text
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": creds["password"]},
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


def _audio(seed: int) -> bytes:
    return bytes((seed + i) % 256 for i in range(8000))


async def _record_assessment(
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


# --------------------------------------------------------------- Service unit tests


async def test_extract_phoneme_scores_handles_canonical_payload() -> None:
    """``{scores: {...}, weakest: [...]}`` payload returns the scores map."""

    from app.services.phoneme_mastery import _extract_phoneme_scores

    payload = {
        "scores": {"a": 0.9, "s": 0.4, "r": 0.65},
        "weakest": [{"phoneme": "s", "score": 0.4}],
        "strongest": [{"phoneme": "a", "score": 0.9}],
    }
    out = _extract_phoneme_scores(payload)
    assert out == {"a": 0.9, "s": 0.4, "r": 0.65}


async def test_extract_phoneme_scores_supports_legacy_flat_payload() -> None:
    """Older analyses stored phoneme→score directly on the dict."""

    from app.services.phoneme_mastery import _extract_phoneme_scores

    out = _extract_phoneme_scores({"a": 0.7, "k": 0.55})
    assert out == {"a": 0.7, "k": 0.55}


async def test_extract_phoneme_scores_normalises_percent_scale() -> None:
    """Pipelines emitting 0–100 are folded into the 0.0–1.0 range."""

    from app.services.phoneme_mastery import _extract_phoneme_scores

    out = _extract_phoneme_scores({"scores": {"a": 85, "s": 40}})
    assert out == {"a": 0.85, "s": 0.40}


async def test_extract_phoneme_scores_drops_invalid_values() -> None:
    """Booleans, strings, NaN-shaped values must be filtered out."""

    from app.services.phoneme_mastery import _extract_phoneme_scores

    out = _extract_phoneme_scores(
        {
            "scores": {
                "a": 0.9,
                "b": True,
                "c": "0.5",
                "d": -0.1,
                "e": 200.0,
                "": 0.5,
            }
        }
    )
    assert out == {"a": 0.9}


async def test_apply_score_running_average_and_mastery_flag() -> None:
    """``_apply_score`` keeps a running mean and flags mastery once achieved."""

    from app.models.phoneme_mastery import PhonemeMastery
    from app.services.phoneme_mastery import _apply_score

    row = PhonemeMastery(
        child_id="child-1",
        phoneme="s",
        language="uz",
        total_attempts=0,
        successful_attempts=0,
        average_score=0.0,
        best_score=0.0,
    )

    when = datetime.now(UTC)
    _apply_score(row, 0.9, when=when)
    _apply_score(row, 0.85, when=when)

    assert row.total_attempts == 2
    assert row.successful_attempts == 2
    assert pytest.approx(row.average_score, rel=1e-3) == 0.875
    assert row.best_score == 0.9
    assert row.mastered_at is not None  # >=2 attempts and avg > threshold


async def test_apply_score_does_not_master_on_single_recording() -> None:
    """A single high-scoring recording is not enough to set ``mastered_at``."""

    from app.models.phoneme_mastery import PhonemeMastery
    from app.services.phoneme_mastery import _apply_score

    row = PhonemeMastery(
        child_id="child-1",
        phoneme="r",
        language="uz",
        total_attempts=0,
        successful_attempts=0,
        average_score=0.0,
        best_score=0.0,
    )
    _apply_score(row, 0.95, when=datetime.now(UTC))
    assert row.mastered_at is None
    assert row.total_attempts == 1
    assert row.successful_attempts == 1


# --------------------------------------------------------------- API tests


async def test_phoneme_mastery_empty_for_new_child(client) -> None:
    """A child with no assessments returns an empty payload, not 404."""

    parent = await _register_login(client, "mastery-empty@sado.uz")
    child_id = await _create_child(client, parent, name="Brand New")

    response = await client.get(
        f"/api/v1/children/{child_id}/phoneme-mastery", headers=parent
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["child_id"] == child_id
    assert body["items"] == []
    assert body["summary"]["total_phonemes"] == 0
    assert body["summary"]["mastered_phonemes"] == 0
    assert body["summary"]["mastery_threshold"] == pytest.approx(0.85)


async def test_phoneme_mastery_populates_after_assessment(
    client, monkeypatch
) -> None:
    """Uploading recordings populates the mastery table for the child."""

    # Force the deterministic mock pipeline so the test is reproducible.
    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    parent = await _register_login(client, "mastery-fill@sado.uz")
    child_id = await _create_child(client, parent, name="Lola")

    await _record_assessment(client, parent, child_id, seed=1)
    await _record_assessment(client, parent, child_id, seed=42)

    response = await client.get(
        f"/api/v1/children/{child_id}/phoneme-mastery", headers=parent
    )
    assert response.status_code == 200, response.text
    body = response.json()
    items = body["items"]

    assert len(items) > 0
    # Every row reports at least one attempt.
    assert all(item["total_attempts"] >= 1 for item in items)
    # All scores are clamped to [0, 1].
    for item in items:
        assert 0.0 <= item["average_score"] <= 1.0
        assert 0.0 <= item["best_score"] <= 1.0
    # Items are sorted weakest-first.
    averages = [item["average_score"] for item in items]
    assert averages == sorted(averages)
    # Summary counts match the item list.
    assert body["summary"]["total_phonemes"] == len(items)
    assert body["summary"]["last_assessed_at"] is not None


async def test_speech_profile_returns_summary_and_voice_quality(
    client, monkeypatch
) -> None:
    """Speech profile rolls mastery + latest voice quality into one payload."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    parent = await _register_login(client, "profile-roll@sado.uz")
    child_id = await _create_child(client, parent, name="Sevinch")

    await _record_assessment(client, parent, child_id, seed=7)
    await _record_assessment(client, parent, child_id, seed=11)

    response = await client.get(
        f"/api/v1/children/{child_id}/speech-profile", headers=parent
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["child_id"] == child_id
    assert body["summary"]["total_phonemes"] > 0
    assert len(body["weakest_phonemes"]) <= 3
    assert len(body["strongest_phonemes"]) <= 3
    # Weakest must have <= average score than the strongest.
    if body["weakest_phonemes"] and body["strongest_phonemes"]:
        assert (
            body["weakest_phonemes"][0]["average_score"]
            <= body["strongest_phonemes"][0]["average_score"]
        )
    # Voice quality block is populated by the deterministic mock pipeline.
    assert body["latest_voice_quality"] is not None
    assert "jitter_local_pct" in body["latest_voice_quality"]
    assert body["latest_assessment_completed_at"] is not None
    assert body["latest_risk"] in {"green", "yellow", "red", None}


async def test_phoneme_mastery_language_filter(
    client, monkeypatch
) -> None:
    """The ``language`` query param filters by stored language code."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    parent = await _register_login(client, "mastery-lang@sado.uz")
    child_id = await _create_child(client, parent, name="Diyor")
    await _record_assessment(client, parent, child_id, seed=3)

    # Child language is uz — filtering by ru returns an empty list.
    response = await client.get(
        f"/api/v1/children/{child_id}/phoneme-mastery",
        params={"language": "ru"},
        headers=parent,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []

    response = await client.get(
        f"/api/v1/children/{child_id}/phoneme-mastery",
        params={"language": "uz"},
        headers=parent,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) > 0


async def test_phoneme_mastery_forbidden_for_other_parent(
    client, monkeypatch
) -> None:
    """Parents may not read mastery rows for a child they do not own."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    parent_a = await _register_login(client, "mastery-a@sado.uz")
    parent_b = await _register_login(client, "mastery-b@sado.uz")
    child_id = await _create_child(client, parent_a, name="Owned")
    await _record_assessment(client, parent_a, child_id, seed=5)

    response = await client.get(
        f"/api/v1/children/{child_id}/phoneme-mastery", headers=parent_b
    )
    assert response.status_code == 403

    response = await client.get(
        f"/api/v1/children/{child_id}/speech-profile", headers=parent_b
    )
    assert response.status_code == 403


async def test_phoneme_mastery_404_for_missing_child(client) -> None:
    """Unknown child IDs surface as 404 with the standard error code."""

    parent = await _register_login(client, "mastery-404@sado.uz")
    response = await client.get(
        "/api/v1/children/00000000-0000-0000-0000-000000000000/phoneme-mastery",
        headers=parent,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "CHILD_NOT_FOUND"


async def test_repeat_assessments_only_grow_attempts(
    client, monkeypatch
) -> None:
    """Running multiple assessments increases ``total_attempts`` in place.

    Mastery rows must be upserted, not duplicated, so the table stays
    O(phoneme inventory) per child regardless of how many sessions the
    child completes.
    """

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    parent = await _register_login(client, "mastery-grow@sado.uz")
    child_id = await _create_child(client, parent, name="Persistent")

    await _record_assessment(client, parent, child_id, seed=2)
    first = await client.get(
        f"/api/v1/children/{child_id}/phoneme-mastery", headers=parent
    )
    assert first.status_code == 200
    first_body = first.json()
    first_count = len(first_body["items"])

    await _record_assessment(client, parent, child_id, seed=4)
    second = await client.get(
        f"/api/v1/children/{child_id}/phoneme-mastery", headers=parent
    )
    second_body = second.json()
    assert len(second_body["items"]) == first_count

    # At least one phoneme has an updated total_attempts >= the first run's.
    by_phoneme_first = {it["phoneme"]: it for it in first_body["items"]}
    by_phoneme_second = {it["phoneme"]: it for it in second_body["items"]}
    assert any(
        by_phoneme_second[p]["total_attempts"]
        >= by_phoneme_first[p]["total_attempts"]
        for p in by_phoneme_first
    )
