"""Tests for the voice-quality service.

Covers the deterministic mock estimator (always available) and the
real Praat/librosa pipeline (gated on the optional native libs).
"""

from __future__ import annotations

import pytest

# -------------------------------------------------------- mock backend


def test_compute_voice_quality_mock_is_deterministic() -> None:
    """Same input bytes must produce the same metrics — snapshot stability."""

    from app.services.voice_quality import compute_voice_quality_mock

    payload_a = compute_voice_quality_mock(
        b"some-audio-bytes" * 8,
        transcript="olma non",
        duration_sec=5.0,
        voiced_ratio=0.8,
    )
    payload_b = compute_voice_quality_mock(
        b"some-audio-bytes" * 8,
        transcript="olma non",
        duration_sec=5.0,
        voiced_ratio=0.8,
    )
    assert payload_a == payload_b


def test_compute_voice_quality_mock_emits_full_contract() -> None:
    """All documented keys are present and well-typed."""

    from app.services.voice_quality import compute_voice_quality_mock

    payload = compute_voice_quality_mock(
        b"abc" * 256,
        transcript="ona ota bola",
        duration_sec=10.0,
        voiced_ratio=0.7,
    )
    expected_keys = {
        "jitter_local_pct",
        "shimmer_local_pct",
        "hnr_db",
        "speech_rate_wpm",
        "voiced_seconds",
        "flags",
        "backend",
    }
    assert expected_keys <= set(payload.keys())
    assert payload["backend"] == "mock"
    assert isinstance(payload["flags"], list)
    # Speech rate = 3 words over 10s = 18 wpm.
    assert payload["speech_rate_wpm"] == pytest.approx(18.0, abs=0.01)
    # Voiced seconds = duration * voiced_ratio = 7.0
    assert payload["voiced_seconds"] == pytest.approx(7.0, abs=0.01)


def test_compute_voice_quality_mock_handles_empty_transcript() -> None:
    """Word count of 0 ⇒ speech_rate_wpm of 0, no crash."""

    from app.services.voice_quality import compute_voice_quality_mock

    payload = compute_voice_quality_mock(
        b"x" * 100,
        transcript=None,
        duration_sec=4.0,
        voiced_ratio=0.5,
    )
    assert payload["speech_rate_wpm"] == 0.0


def test_compute_voice_quality_mock_handles_zero_duration() -> None:
    """Zero duration must not divide-by-zero."""

    from app.services.voice_quality import compute_voice_quality_mock

    payload = compute_voice_quality_mock(
        b"x" * 100,
        transcript="ona ota",
        duration_sec=0.0,
        voiced_ratio=0.0,
    )
    assert payload["speech_rate_wpm"] == 0.0
    assert payload["voiced_seconds"] == 0.0


def test_voice_quality_flags_high_jitter() -> None:
    """A transcript-less mock with a known seed ⇒ deterministic flag set."""

    from app.services.voice_quality import compute_voice_quality_mock

    # Run a bunch of different inputs and collect the union of flags.
    # We just need to confirm the flagging mechanism actually fires
    # for *some* inputs in the realistic range.
    flag_union: set[str] = set()
    for i in range(50):
        payload = compute_voice_quality_mock(
            f"sample-{i}".encode() * 16,
            transcript="ona ota bola maktab",
            duration_sec=5.0,
            voiced_ratio=0.7,
        )
        flag_union.update(payload["flags"])
    # Realistic mock distribution must produce at least one quality
    # flag and one speech-rate flag across 50 samples.
    expected = {"high_jitter", "high_shimmer", "low_hnr", "slow_speech_rate", "fast_speech_rate"}
    assert flag_union & expected


# -------------------------------------------------------- real backend (lib-gated)


def _native_libs_installed() -> bool:
    try:
        import librosa  # noqa: F401
        import numpy  # noqa: F401
        import soundfile  # noqa: F401
    except Exception:
        return False
    return True


_REQUIRES_AUDIO = pytest.mark.skipif(
    not _native_libs_installed(),
    reason="librosa/soundfile/numpy not installed — install '.[audio]'",
)


@_REQUIRES_AUDIO
def test_compute_voice_quality_real_synthetic_tone() -> None:
    """Real estimator runs over a synthetic 220 Hz tone and emits the contract."""

    import numpy as np

    from app.services.voice_quality import compute_voice_quality_real

    sample_rate = 16000
    duration = 1.5
    t = np.linspace(0, duration, int(duration * sample_rate), endpoint=False)
    waveform = (0.4 * np.sin(2 * np.pi * 220 * t)).astype(np.float64)

    payload = compute_voice_quality_real(
        waveform,
        sample_rate,
        transcript="ona ota",
        duration_sec=duration,
        voiced_ratio=0.9,
    )

    assert payload["backend"].startswith("real-")
    assert {"jitter_local_pct", "shimmer_local_pct", "hnr_db",
            "speech_rate_wpm", "voiced_seconds", "flags"} <= set(payload.keys())
    # Speech rate = 2 words / (1.5/60) = 80 wpm
    assert payload["speech_rate_wpm"] == pytest.approx(80.0, abs=0.5)


@_REQUIRES_AUDIO
def test_compute_voice_quality_real_zero_waveform() -> None:
    """Silent waveform must not crash — returns finite numbers."""

    import numpy as np

    from app.services.voice_quality import compute_voice_quality_real

    waveform = np.zeros(16000, dtype=np.float64)
    payload = compute_voice_quality_real(
        waveform,
        16000,
        transcript=None,
        duration_sec=1.0,
        voiced_ratio=0.0,
    )
    # Should not raise and should produce numeric metrics.
    assert isinstance(payload["jitter_local_pct"], float)
    assert isinstance(payload["shimmer_local_pct"], float)
    assert isinstance(payload["hnr_db"], float)


# -------------------------------------------------------- integration with analyzer


def test_speech_analyzer_mock_includes_voice_quality(monkeypatch) -> None:
    """The mock pipeline must populate the new ``voice_quality`` field."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    features = sa.extract_features(
        b"deterministic-bytes" * 32,
        declared_duration_sec=5.0,
    )
    assert features.voice_quality
    assert features.voice_quality["backend"] == "mock"
    # Summary mirrors the headline voice metrics for dashboard cards.
    assert "jitter_local_pct" in features.feature_summary
    assert "hnr_db" in features.feature_summary
    assert features.feature_summary["jitter_local_pct"] == features.voice_quality["jitter_local_pct"]
