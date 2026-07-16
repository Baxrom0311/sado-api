"""Tests for the real-audio feature pipeline and the dispatch logic.

The native libraries (librosa / soundfile / parselmouth) are optional.
These tests cover three layers without requiring the libs to be
installed on the test runner:

1. Pure dispatch — :func:`app.services.speech_analyzer.extract_features`
   honours the ``audio_analysis_backend`` setting.
2. Availability detection — :func:`real_audio_features.is_available`
   reflects what's importable.
3. Fallback semantics — when the real backend errors and ``"auto"``
   is configured, the mock pipeline must take over transparently.
"""

from __future__ import annotations

from typing import Any

import pytest

# ---------------------------------------------------------- is_available


def test_is_available_returns_bool() -> None:
    """The detector must return a boolean and never raise."""

    from app.services.real_audio_features import is_available

    value = is_available()
    assert isinstance(value, bool)


def test_extract_real_features_raises_without_libs(monkeypatch) -> None:
    """The strict entry-point must fail loudly when libs are missing."""

    from app.services import real_audio_features as raf

    monkeypatch.setattr(raf, "is_available", lambda: False)

    with pytest.raises(raf.RealAnalyzerUnavailableError):
        raf.extract_real_features(b"\x00" * 32)


def test_extract_real_features_rejects_empty_payload() -> None:
    """Empty audio bytes are a programmer error, not a runtime fallback."""

    from app.services import real_audio_features as raf

    with pytest.raises(ValueError):
        raf.extract_real_features(b"")


# ---------------------------------------------------------- dispatch logic


def test_extract_features_uses_mock_when_backend_mock(monkeypatch) -> None:
    """``backend="mock"`` always runs the deterministic synthetic pipeline."""

    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "mock")

    features = sa.extract_features(b"some-audio-bytes" * 64, declared_duration_sec=4.0)

    assert features.feature_summary["backend"] == "mock"
    assert features.duration_sec == 4.0
    # Mock pipeline emits the standard mock contract keys.
    assert "tracks" in features.formant_data
    assert features.phoneme_scores["weakest"]
    assert features.mfcc_features["n_frames"] >= 20


def test_extract_features_real_backend_propagates_unavailable(monkeypatch) -> None:
    """``backend="real"`` must surface the missing-libs error to operators."""

    from app.services import real_audio_features as raf
    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "real")
    monkeypatch.setattr(raf, "is_available", lambda: False)

    with pytest.raises(raf.RealAnalyzerUnavailableError):
        sa.extract_features(b"x" * 100, declared_duration_sec=2.0)


def test_extract_features_auto_falls_back_to_mock_when_real_unavailable(
    monkeypatch,
) -> None:
    """``"auto"`` + missing libs ⇒ silent mock fallback."""

    from app.services import real_audio_features as raf
    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "auto")
    monkeypatch.setattr(raf, "is_available", lambda: False)

    features = sa.extract_features(b"x" * 200, declared_duration_sec=3.0)
    assert features.feature_summary["backend"] == "mock"


def test_extract_features_auto_falls_back_when_real_raises(monkeypatch) -> None:
    """``"auto"`` swallows real-backend explosions and serves the mock."""

    from app.services import real_audio_features as raf
    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "auto")
    monkeypatch.setattr(raf, "is_available", lambda: True)

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise raf.RealAnalyzerUnavailableError("decoding failed")

    monkeypatch.setattr(raf, "extract_real_features", boom)

    features = sa.extract_features(b"x" * 200, declared_duration_sec=2.5)
    assert features.feature_summary["backend"] == "mock"


def test_extract_features_auto_uses_real_when_available(monkeypatch) -> None:
    """``"auto"`` + libs reported available ⇒ real pipeline is invoked."""

    from app.services import real_audio_features as raf
    from app.services import speech_analyzer as sa

    monkeypatch.setattr(sa, "_resolve_backend", lambda: "auto")
    monkeypatch.setattr(raf, "is_available", lambda: True)

    captured: dict[str, Any] = {}

    def fake_real(audio_bytes: bytes, *, declared_duration_sec=None):
        captured["audio_bytes"] = audio_bytes
        captured["declared_duration_sec"] = declared_duration_sec
        return sa.SpeechFeatures(
            transcript="real",
            duration_sec=2.5,
            sample_rate=16000,
            confidence=0.8,
            mfcc_features={"matrix": [], "n_mfcc": 13, "n_frames": 0},
            pitch_data={"voiced_ratio": 0.8, "f0_mean": 220.0},
            formant_data={"tracks": {"f1": [], "f2": [], "f3": []}, "f1_mean": 0, "f2_mean": 0, "f3_mean": 0},
            phoneme_scores={"scores": {}, "weakest": [], "strongest": []},
            feature_summary={"backend": "real"},
        )

    monkeypatch.setattr(raf, "extract_real_features", fake_real)

    features = sa.extract_features(b"abc" * 64, declared_duration_sec=2.5)
    assert features.feature_summary["backend"] == "real"
    assert captured["declared_duration_sec"] == 2.5
    assert captured["audio_bytes"] == b"abc" * 64


def test_extract_features_rejects_empty_payload() -> None:
    """The dispatcher itself enforces the empty-payload contract."""

    from app.services.speech_analyzer import extract_features

    with pytest.raises(ValueError):
        extract_features(b"")


# -------------------------------------------------------- settings binding


def test_resolve_backend_reads_from_settings(monkeypatch) -> None:
    """``_resolve_backend`` must reflect the live settings value."""

    from app.config import get_settings
    from app.services import speech_analyzer as sa

    get_settings.cache_clear()
    monkeypatch.setenv("AUDIO_ANALYSIS_BACKEND", "mock")
    try:
        assert sa._resolve_backend() == "mock"
    finally:
        get_settings.cache_clear()


def test_settings_default_backend_is_auto(monkeypatch) -> None:
    """Default value is ``"auto"`` so prod opts into real analysis when libs land."""

    from app.config import Settings, get_settings

    get_settings.cache_clear()
    monkeypatch.delenv("AUDIO_ANALYSIS_BACKEND", raising=False)
    try:
        s = Settings()
        assert s.audio_analysis_backend == "auto"
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------- real pipeline (lib-gated)
# These tests only run when the optional native stack is actually
# installed. They validate end-to-end shape compatibility with the
# mock pipeline so callers can swap backends without code changes.


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
def test_extract_real_features_smoke_against_synthetic_wav() -> None:
    """Real pipeline runs and emits the documented response shape."""

    import io

    import numpy as np
    import soundfile as sf

    from app.services.real_audio_features import extract_real_features

    # 1.5s 220 Hz tone @ 16 kHz mono — short enough to keep the test fast.
    sample_rate = 16000
    t = np.linspace(0, 1.5, int(1.5 * sample_rate), endpoint=False)
    waveform = (0.4 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)

    buf = io.BytesIO()
    sf.write(buf, waveform, sample_rate, format="WAV", subtype="PCM_16")
    payload = buf.getvalue()

    features = extract_real_features(payload, declared_duration_sec=1.5)

    assert features.sample_rate == sample_rate
    assert features.duration_sec == pytest.approx(1.5, abs=0.05)
    assert features.feature_summary["backend"] == "real"
    # Same response keys as the mock pipeline so the API contract holds.
    assert set(features.formant_data.keys()) >= {"tracks", "f1_mean", "f2_mean", "f3_mean"}
    assert set(features.pitch_data.keys()) >= {"f0_hz", "f0_mean", "voiced_ratio"}
    assert features.mfcc_features["n_frames"] > 0
    assert features.phoneme_scores["weakest"]
    assert 0.55 <= features.confidence <= 0.95


@_REQUIRES_AUDIO
def test_extract_real_features_handles_corrupt_audio() -> None:
    """Garbage bytes surface as ``RealAnalyzerUnavailableError`` not arbitrary errors."""

    from app.services.real_audio_features import (
        RealAnalyzerUnavailableError,
        extract_real_features,
    )

    with pytest.raises(RealAnalyzerUnavailableError):
        extract_real_features(b"\x00" * 256)
