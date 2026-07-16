"""Real-audio feature extractor — librosa + soundfile + parselmouth.

This module is the production counterpart to
:mod:`app.services.speech_analyzer`, which only emits deterministic
mock features. When the optional native audio libraries are installed
this module decodes the raw bytes and computes:

* MFCC matrix (librosa)
* fundamental frequency / pitch contour (librosa.pyin)
* formant trajectories F1–F3 (parselmouth → Praat)
* phoneme-quality proxy scores (spectral-band energy heuristic)

Backwards compatibility is paramount — the returned
:class:`~app.services.speech_analyzer.SpeechFeatures` object has the
exact same shape as the mock pipeline, so every API consumer
(``/analysis/*`` endpoints, the ML scorer, the audio_processor
finalizer) continues to work without changes.

The dependencies are imported lazily so the rest of the application
keeps booting even when librosa / parselmouth are absent (e.g. unit
tests, lightweight dev shells, the CI runner).
"""

from __future__ import annotations

import io
import logging
import math
from typing import TYPE_CHECKING, Any

from app.services.speech_analyzer import SpeechFeatures

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

logger = logging.getLogger(__name__)


class RealAnalyzerUnavailableError(RuntimeError):
    """Raised when the real-audio libraries are not importable."""


# A lightweight Uzbek vocabulary — used to seed the pseudo-transcript
# emitted alongside real acoustic features. Real ASR (Whisper) would
# replace this; the API contract only requires *some* string.
_UZ_PHRASES = (
    "olma",
    "non",
    "ona",
    "ota",
    "kitob",
    "maktab",
    "salom",
    "rahmat",
    "qush uchadi",
    "bola o'qiydi",
)

# Phoneme groups whose energy we probe for the phoneme-score proxy.
# Each entry maps a phoneme symbol to a (low_hz, high_hz) frequency
# band that captures most of its acoustic energy in child speech.
_PHONEME_BANDS: dict[str, tuple[float, float]] = {
    "a": (700.0, 1400.0),
    "e": (450.0, 2200.0),
    "i": (250.0, 2700.0),
    "o": (450.0, 1000.0),
    "u": (300.0, 900.0),
    "k": (1500.0, 4000.0),
    "l": (300.0, 2500.0),
    "m": (200.0, 1200.0),
    "n": (200.0, 2500.0),
    "p": (500.0, 2500.0),
    "r": (1200.0, 3500.0),
    "s": (4000.0, 8000.0),
    "t": (2500.0, 6000.0),
}


def is_available() -> bool:
    """Return ``True`` only if the core real-audio stack imports.

    ``parselmouth`` is treated as optional inside the real pipeline; we
    fall back to a librosa-only formant estimate when it is absent.
    """

    try:
        import librosa  # noqa: F401
        import numpy  # noqa: F401
        import soundfile  # noqa: F401
    except Exception:  # pragma: no cover - exercised when libs missing
        return False
    return True


def _has_parselmouth() -> bool:
    try:
        import parselmouth  # noqa: F401
    except Exception:  # pragma: no cover - import-time check
        return False
    return True


def _decode_audio(audio_bytes: bytes) -> tuple[Any, int]:
    """Decode ``audio_bytes`` into a mono float waveform + sample rate.

    Uses ``soundfile`` for the heavy lifting. Stereo recordings are
    averaged down to mono so downstream features (MFCC, pitch) are
    well-defined on a single channel.
    """

    import numpy as np
    import soundfile as sf

    buf = io.BytesIO(audio_bytes)
    try:
        waveform, sample_rate = sf.read(buf, always_2d=False)
    except Exception as exc:  # pragma: no cover - depends on libsndfile
        raise RealAnalyzerUnavailableError(f"failed to decode audio: {exc}") from exc

    waveform = np.asarray(waveform, dtype=np.float32)
    if waveform.ndim > 1:
        waveform = waveform.mean(axis=1).astype(np.float32)
    if waveform.size == 0:
        raise ValueError("empty audio payload")
    return waveform, int(sample_rate)


def _compute_mfcc(waveform: Any, sample_rate: int, n_mfcc: int = 13) -> dict[str, Any]:
    """Return MFCCs in the shape used by the mock pipeline."""

    import librosa
    import numpy as np

    matrix = librosa.feature.mfcc(y=waveform, sr=sample_rate, n_mfcc=n_mfcc)
    # librosa shape is (n_mfcc, n_frames); transpose for row-per-frame.
    matrix_t = np.asarray(matrix, dtype=np.float64).T
    n_frames = int(matrix_t.shape[0])

    if n_frames == 0:
        return {
            "n_mfcc": n_mfcc,
            "n_frames": 0,
            "matrix": [],
            "mean": [],
            "std": [],
            "min": 0.0,
            "max": 0.0,
        }

    rounded = np.round(matrix_t, 3).tolist()
    mean = np.round(matrix_t.mean(axis=0), 3).tolist()
    std = np.round(matrix_t.std(axis=0), 3).tolist()
    return {
        "n_mfcc": n_mfcc,
        "n_frames": n_frames,
        "matrix": rounded,
        "mean": mean,
        "std": std,
        "min": float(np.round(matrix_t.min(), 3)),
        "max": float(np.round(matrix_t.max(), 3)),
    }


def _compute_pitch(waveform: Any, sample_rate: int) -> dict[str, Any]:
    """F0 contour using ``librosa.pyin`` with kid-speech ranges."""

    import librosa
    import numpy as np

    f0, voiced_flag, _voiced_prob = librosa.pyin(
        waveform,
        fmin=float(librosa.note_to_hz("C2")),
        fmax=float(librosa.note_to_hz("C6")),
        sr=sample_rate,
    )
    f0 = np.asarray(f0, dtype=np.float64)
    voiced = np.asarray(voiced_flag, dtype=bool)

    # Replace NaNs (unvoiced frames) with 0 for transport, but compute
    # statistics from voiced frames only.
    series = np.nan_to_num(f0, nan=0.0)
    voiced_values = f0[voiced & ~np.isnan(f0)]
    if voiced_values.size == 0:
        f0_mean = 0.0
        f0_min = 0.0
        f0_max = 0.0
    else:
        f0_mean = float(np.round(voiced_values.mean(), 2))
        f0_min = float(np.round(voiced_values.min(), 2))
        f0_max = float(np.round(voiced_values.max(), 2))

    voiced_ratio = (
        float(np.round(voiced.sum() / voiced.size, 3)) if voiced.size else 0.0
    )

    return {
        "f0_hz": [float(round(v, 2)) for v in series.tolist()],
        "f0_mean": f0_mean,
        "f0_min": f0_min,
        "f0_max": f0_max,
        "voiced_ratio": voiced_ratio,
    }


def _compute_formants_parselmouth(
    waveform: Any, sample_rate: int
) -> dict[str, Any] | None:
    """Praat-style formant extraction. Returns ``None`` on failure."""

    try:
        import numpy as np
        import parselmouth
    except Exception:  # pragma: no cover - exercised when lib absent
        return None

    try:
        sound = parselmouth.Sound(waveform.astype(np.float64), sampling_frequency=sample_rate)
        formant = sound.to_formant_burg(time_step=0.025, max_number_of_formants=5)
    except Exception:  # pragma: no cover - native errors hard to provoke
        logger.exception("parselmouth formant extraction failed")
        return None

    duration = sound.get_total_duration()
    if duration <= 0:
        return None
    n_steps = max(1, int(duration / 0.025))
    times = [(i + 0.5) * (duration / n_steps) for i in range(n_steps)]

    tracks: dict[str, list[float]] = {}
    for idx in (1, 2, 3):
        track: list[float] = []
        for t in times:
            try:
                value = formant.get_value_at_time(idx, t)
            except Exception:  # pragma: no cover - native failure
                value = float("nan")
            if value is None or math.isnan(value):
                value = 0.0
            track.append(round(float(value), 1))
        tracks[f"f{idx}"] = track

    def _mean(track: list[float]) -> float:
        non_zero = [v for v in track if v > 0]
        if not non_zero:
            return 0.0
        return round(sum(non_zero) / len(non_zero), 1)

    return {
        "tracks": tracks,
        "f1_mean": _mean(tracks["f1"]),
        "f2_mean": _mean(tracks["f2"]),
        "f3_mean": _mean(tracks["f3"]),
    }


def _compute_formants_librosa(waveform: Any, sample_rate: int) -> dict[str, Any]:
    """LPC-based formant fallback when parselmouth is unavailable.

    Less accurate than Praat but keeps the response shape stable.
    """

    import librosa
    import numpy as np

    frame_length = max(256, int(0.025 * sample_rate))
    hop_length = max(128, int(0.0125 * sample_rate))
    if waveform.size < frame_length:
        # pad with zeros so we still emit a valid (empty-ish) track
        waveform = np.pad(waveform, (0, frame_length - waveform.size))

    # Pre-emphasis sharpens the higher formants.
    emphasised = np.append(waveform[0:1], waveform[1:] - 0.97 * waveform[:-1])
    frames = librosa.util.frame(
        emphasised, frame_length=frame_length, hop_length=hop_length
    ).T

    nyquist = sample_rate / 2.0
    tracks: dict[str, list[float]] = {"f1": [], "f2": [], "f3": []}

    for frame in frames:
        if not np.any(frame):
            for key in tracks:
                tracks[key].append(0.0)
            continue
        try:
            order = 2 + int(sample_rate / 1000)
            coefficients = librosa.lpc(frame.astype(np.float64), order=order)
        except Exception:  # pragma: no cover - numerical edge
            for key in tracks:
                tracks[key].append(0.0)
            continue
        roots = np.roots(coefficients)
        roots = roots[np.imag(roots) > 0]
        if roots.size == 0:
            for key in tracks:
                tracks[key].append(0.0)
            continue
        angles = np.arctan2(np.imag(roots), np.real(roots))
        freqs = sorted(angles * (nyquist / np.pi))
        for idx, key in enumerate(("f1", "f2", "f3")):
            value = freqs[idx] if idx < len(freqs) else 0.0
            tracks[key].append(round(float(max(0.0, value)), 1))

    def _mean(track: list[float]) -> float:
        non_zero = [v for v in track if v > 0]
        if not non_zero:
            return 0.0
        return round(sum(non_zero) / len(non_zero), 1)

    return {
        "tracks": tracks,
        "f1_mean": _mean(tracks["f1"]),
        "f2_mean": _mean(tracks["f2"]),
        "f3_mean": _mean(tracks["f3"]),
    }


def _compute_phoneme_scores(waveform: Any, sample_rate: int) -> dict[str, Any]:
    """Cheap proxy: per-phoneme score = energy in its expected band.

    Without a real ASR / forced aligner we cannot score actual phoneme
    productions, but the band-energy proxy gives a stable, comparable
    number per recording so downstream visualisations work and the
    weakest/strongest ranking is meaningful.
    """

    import librosa
    import numpy as np

    n_fft = 1024
    spec = np.abs(librosa.stft(waveform, n_fft=n_fft, hop_length=n_fft // 2))
    if spec.size == 0:
        return {
            "scores": {p: 0.0 for p in _PHONEME_BANDS},
            "weakest": [],
            "strongest": [],
        }

    freqs = librosa.fft_frequencies(sr=sample_rate, n_fft=n_fft)
    total_energy = float(spec.sum()) or 1.0

    raw: dict[str, float] = {}
    for phoneme, (lo, hi) in _PHONEME_BANDS.items():
        band = spec[(freqs >= lo) & (freqs <= hi)]
        if band.size == 0:
            raw[phoneme] = 0.0
            continue
        raw[phoneme] = float(band.sum() / total_energy)

    # Normalise so scores live in [0.4, 1.0] like the mock pipeline,
    # making it easy to swap backends without changing UI.
    max_raw = max(raw.values()) or 1.0
    scores = {
        p: round(0.4 + 0.6 * (v / max_raw), 3) for p, v in raw.items()
    }
    sorted_items = sorted(scores.items(), key=lambda kv: kv[1])
    return {
        "scores": scores,
        "weakest": [{"phoneme": p, "score": s} for p, s in sorted_items[:3]],
        "strongest": [{"phoneme": p, "score": s} for p, s in sorted_items[-3:]],
    }


def _pseudo_transcript(audio_bytes: bytes) -> str:
    """Stable pseudo-transcript derived from the audio hash.

    A real Whisper integration would replace this; until then we keep
    the field deterministic so snapshot tests stay green.
    """

    import hashlib

    digest = hashlib.sha256(audio_bytes).digest()
    seed = int.from_bytes(digest[:4], "big")
    n_words = (seed % 3) + 1
    chosen = []
    for i in range(n_words):
        chosen.append(_UZ_PHRASES[(seed >> (i * 4)) % len(_UZ_PHRASES)])
    return " ".join(chosen)


def extract_real_features(
    audio_bytes: bytes,
    *,
    declared_duration_sec: float | None = None,
) -> SpeechFeatures:
    """Run the production audio pipeline and return ``SpeechFeatures``.

    Raises :class:`RealAnalyzerUnavailableError` if the native libraries are
    missing or decoding fails — callers in ``"auto"`` mode catch this
    and fall back to the mock backend.
    """

    if not audio_bytes:
        raise ValueError("empty audio payload")
    if not is_available():
        raise RealAnalyzerUnavailableError(
            "librosa / soundfile / numpy not installed — install the "
            "'audio' extras to enable the real backend."
        )

    waveform, sample_rate = _decode_audio(audio_bytes)
    duration_sec = float(len(waveform) / sample_rate) if sample_rate else 0.0
    if declared_duration_sec and declared_duration_sec > 0:
        # Trust the client-reported duration when it's plausible — keeps
        # the response stable even if the file's header is truncated.
        if abs(declared_duration_sec - duration_sec) < max(1.0, duration_sec * 0.5):
            duration_sec = float(declared_duration_sec)

    mfcc = _compute_mfcc(waveform, sample_rate)
    pitch = _compute_pitch(waveform, sample_rate)
    formants = (
        _compute_formants_parselmouth(waveform, sample_rate)
        if _has_parselmouth()
        else None
    )
    if formants is None:
        formants = _compute_formants_librosa(waveform, sample_rate)
    phonemes = _compute_phoneme_scores(waveform, sample_rate)

    transcript = _pseudo_transcript(audio_bytes)
    confidence = _confidence_from_voicing(pitch.get("voiced_ratio", 0.0))

    from app.services.voice_quality import compute_voice_quality_real

    voice_quality = compute_voice_quality_real(
        waveform,
        sample_rate,
        transcript=transcript,
        duration_sec=duration_sec,
        voiced_ratio=pitch.get("voiced_ratio", 0.0),
    )

    summary = {
        "duration_sec": round(duration_sec, 2),
        "sample_rate": int(sample_rate),
        "n_frames": mfcc.get("n_frames", 0),
        "transcript_word_count": len(transcript.split()),
        "voiced_ratio": pitch.get("voiced_ratio", 0.0),
        "f0_mean": pitch.get("f0_mean", 0.0),
        "f1_mean": formants.get("f1_mean", 0.0),
        "f2_mean": formants.get("f2_mean", 0.0),
        "weakest_phonemes": [item["phoneme"] for item in phonemes["weakest"]],
        "backend": "real",
        "jitter_local_pct": voice_quality["jitter_local_pct"],
        "shimmer_local_pct": voice_quality["shimmer_local_pct"],
        "hnr_db": voice_quality["hnr_db"],
        "speech_rate_wpm": voice_quality["speech_rate_wpm"],
    }

    return SpeechFeatures(
        transcript=transcript,
        duration_sec=round(duration_sec, 2),
        sample_rate=int(sample_rate),
        confidence=confidence,
        mfcc_features=mfcc,
        pitch_data=pitch,
        formant_data=formants,
        phoneme_scores=phonemes,
        feature_summary=summary,
        voice_quality=voice_quality,
    )


def _confidence_from_voicing(voiced_ratio: float) -> float:
    """Map voiced-frame ratio to a 0.55–0.95 confidence band.

    Higher voicing → more confident analysis (we have more pitch
    information to work with). Mirrors the mock module's range so the
    risk scorer's calibration stays consistent.
    """

    voiced_ratio = max(0.0, min(1.0, float(voiced_ratio)))
    return round(0.55 + 0.4 * voiced_ratio, 3)


__all__ = [
    "RealAnalyzerUnavailableError",
    "extract_real_features",
    "is_available",
]
