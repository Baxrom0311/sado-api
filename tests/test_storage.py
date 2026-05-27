"""Unit tests for ``app.services.storage`` (LocalAudioStorage + helpers)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_local_storage_put_get_delete(tmp_path):
    from app.services.storage import LocalAudioStorage

    storage = LocalAudioStorage(str(tmp_path))
    payload = b"\x00\x01\x02hello world"

    stored = await storage.put_object(
        key="audio/sess-1/rec-1.wav", data=payload, content_type="audio/wav"
    )
    assert stored.storage_key == "audio/sess-1/rec-1.wav"
    assert stored.size_bytes == len(payload)
    assert stored.content_type == "audio/wav"

    fetched = await storage.get_object("audio/sess-1/rec-1.wav")
    assert fetched == payload

    url = await storage.presigned_get_url("audio/sess-1/rec-1.wav", expires_in=120)
    assert url.startswith("file://")

    await storage.delete_object("audio/sess-1/rec-1.wav")
    # Deleting a missing key is idempotent — should not raise.
    await storage.delete_object("audio/sess-1/rec-1.wav")

    with pytest.raises(FileNotFoundError):
        await storage.get_object("audio/sess-1/rec-1.wav")


async def test_local_storage_rejects_path_traversal(tmp_path):
    from app.services.storage import LocalAudioStorage

    storage = LocalAudioStorage(str(tmp_path))
    # Even with a traversal attempt, the implementation should normalise
    # ``..`` to ``_`` and contain the write inside the base dir.
    stored = await storage.put_object(
        key="../../etc/evil.wav", data=b"x", content_type="audio/wav"
    )
    assert stored.size_bytes == 1
    fetched = await storage.get_object("../../etc/evil.wav")
    assert fetched == b"x"
    # The actual file lives somewhere under the base dir, never above it.
    written = list(tmp_path.rglob("evil.wav"))
    assert written and all(str(p).startswith(str(tmp_path)) for p in written)


async def test_extension_for_content_type():
    from app.services.storage import _extension_for_content_type

    assert _extension_for_content_type("audio/wav") == "wav"
    assert _extension_for_content_type("audio/mpeg") == "mp3"
    assert _extension_for_content_type("AUDIO/MP3") == "mp3"
    assert _extension_for_content_type("audio/x-m4a") == "m4a"
    assert _extension_for_content_type("audio/ogg") == "ogg"
    assert _extension_for_content_type("audio/webm") == "webm"
    assert _extension_for_content_type("audio/flac") == "flac"
    assert _extension_for_content_type("application/octet-stream") == "bin"


async def test_build_recording_key_is_deterministic_and_safe():
    from app.services.storage import build_recording_key

    key = build_recording_key(
        assessment_id="abc-123", recording_id="rec-9", content_type="audio/wav"
    )
    assert key == "audio/abc-123/rec-9.wav"

    # Special characters are URL-encoded so the key is safe in any backend.
    risky = build_recording_key(
        assessment_id="a/b",
        recording_id="r c",
        content_type="audio/mpeg",
    )
    assert "/" not in risky.split("/", 2)[2]  # only one user-controlled slash level
    assert risky.endswith(".mp3")


async def test_get_audio_storage_falls_back_to_local(tmp_path, monkeypatch):
    """Production+test gating: when ``is_test`` is true, must use local backend."""

    from app.config import get_settings
    from app.services.storage import (
        LocalAudioStorage,
        get_audio_storage,
        reset_audio_storage,
    )

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    reset_audio_storage()

    backend = get_audio_storage()
    assert isinstance(backend, LocalAudioStorage)

    # Singleton — second call returns the same instance.
    assert get_audio_storage() is backend

    reset_audio_storage()
    get_settings.cache_clear()
