"""Voice-quality analysis — jitter, shimmer, HNR, speech rate.

These four metrics are the clinical workhorses used by speech-language
pathologists to triage voice disorders, dysphonia, and articulation
issues:

* ``jitter_local`` — cycle-to-cycle pitch perturbation (%). High values
  flag pitch instability typical of vocal fold pathology. Healthy
  adult speech sits below ~1.04%.
* ``shimmer_local`` — cycle-to-cycle amplitude perturbation (%). High
  values flag breathiness / weak vocal closure. Healthy speech
  typically lives below ~3.81%.
* ``hnr_db`` — Harmonics-to-Noise Ratio (dB). Lower numbers mean a
  noisier, hoarser signal. Healthy adult voices usually land above
  ~20 dB; sustained-vowel tasks above ~7 dB are clinically
  significant.
* ``speech_rate_wpm`` — speaking rate in words-per-minute, derived
  from the transcript word count and duration. The clinical normal
  band for fluent child speech is roughly 100–180 WPM.

Two implementations are provided:

* :func:`compute_voice_quality_real` runs Praat (parselmouth) for the
  high-precision metrics and falls back to librosa-only estimates when
  parselmouth is unavailable.
* :func:`compute_voice_quality_mock` derives a deterministic, plausible
  payload from a hash of the audio bytes so the mock pipeline stays
  reproducible and shape-compatible with the real one.

Both functions emit the **same** dictionary contract so consumers
(persistence, API schemas, recommendation engine, dashboards) are
backend-agnostic.
"""

from __future__ import annotations

import hashlib
import logging
import math
import random
from typing import Any

logger = logging.getLogger(__name__)


# Clinical reference ranges. These are deliberately broad — they're
# heuristics for flagging, not diagnostic thresholds.
JITTER_HEALTHY_MAX_PCT = 1.04
SHIMMER_HEALTHY_MAX_PCT = 3.81
HNR_HEALTHY_MIN_DB = 20.0
SPEECH_RATE_NORMAL_LOW_WPM = 100.0
SPEECH_RATE_NORMAL_HIGH_WPM = 180.0


def _empty_payload() -> dict[str, Any]:
    """Return the default voice-quality payload with neutral metrics."""

    return {
        "jitter_local_pct": 0.0,
        "shimmer_local_pct": 0.0,
        "hnr_db": 0.0,
        "speech_rate_wpm": 0.0,
        "voiced_seconds": 0.0,
        "flags": [],
        "backend": "unknown",
    }


def _flags_for(
    *,
    jitter_pct: float,
    shimmer_pct: float,
    hnr_db: float,
    speech_rate_wpm: float,
) -> list[str]:
    """Tag each metric that falls outside clinical reference ranges.

    The flags are stable string codes the recommendations engine and
    the therapist UI both use; they are *not* diagnostic conclusions.
    """

    flags: list[str] = []
    if jitter_pct > JITTER_HEALTHY_MAX_PCT:
        flags.append("high_jitter")
    if shimmer_pct > SHIMMER_HEALTHY_MAX_PCT:
        flags.append("high_shimmer")
    if 0 < hnr_db < HNR_HEALTHY_MIN_DB:
        flags.append("low_hnr")
    if speech_rate_wpm and speech_rate_wpm < SPEECH_RATE_NORMAL_LOW_WPM:
        flags.append("slow_speech_rate")
    elif speech_rate_wpm > SPEECH_RATE_NORMAL_HIGH_WPM:
        flags.append("fast_speech_rate")
    return flags


def _word_count(transcript: str | None) -> int:
    if not transcript:
        return 0
    return len([w for w in transcript.split() if w.strip()])


def compute_voice_quality_mock(
    audio_bytes: bytes,
    *,
    transcript: str | None,
    duration_sec: float,
    voiced_ratio: float,
) -> dict[str, Any]:
    """Deterministic voice-quality metrics for the mock backend.

    Seeded from the SHA-256 of the audio bytes so the same input always
    produces the same output (snapshot-test friendly). The numbers are
    sampled from realistic distributions so downstream UIs and
    recommendation rules behave like they do in production.
    """

    digest = hashlib.sha256(audio_bytes).digest()
    seed = int.from_bytes(digest[8:16], "big", signed=False)
    rng = random.Random(seed)

    jitter_pct = round(rng.uniform(0.3, 2.5), 3)
    shimmer_pct = round(rng.uniform(1.5, 6.5), 3)
    hnr_db = round(rng.uniform(8.0, 25.0), 2)
    voiced_seconds = round(max(0.0, duration_sec * max(0.0, min(1.0, voiced_ratio))), 2)

    words = _word_count(transcript)
    if duration_sec > 0:
        speech_rate_wpm = round(words / (duration_sec / 60.0), 2)
    else:
        speech_rate_wpm = 0.0

    payload = {
        "jitter_local_pct": jitter_pct,
        "shimmer_local_pct": shimmer_pct,
        "hnr_db": hnr_db,
        "speech_rate_wpm": speech_rate_wpm,
        "voiced_seconds": voiced_seconds,
        "flags": _flags_for(
            jitter_pct=jitter_pct,
            shimmer_pct=shimmer_pct,
            hnr_db=hnr_db,
            speech_rate_wpm=speech_rate_wpm,
        ),
        "backend": "mock",
    }
    return payload


def compute_voice_quality_real(
    waveform: Any,
    sample_rate: int,
    *,
    transcript: str | None,
    duration_sec: float,
    voiced_ratio: float,
) -> dict[str, Any]:
    """Production voice-quality metrics computed from a decoded waveform.

    Uses Praat (via parselmouth) for jitter/shimmer/HNR when available
    and falls back to a librosa-only estimator otherwise. Returns the
    same shape as :func:`compute_voice_quality_mock` so callers can
    swap backends without changing schemas.
    """

    try:
        import numpy as np
    except Exception:  # pragma: no cover - numpy is a hard dep
        return _empty_payload()

    waveform = np.asarray(waveform, dtype=np.float64)
    voiced_seconds = round(
        max(0.0, duration_sec * max(0.0, min(1.0, voiced_ratio))), 2
    )

    jitter_pct = 0.0
    shimmer_pct = 0.0
    hnr_db = 0.0
    backend = "real-librosa"

    try:
        import parselmouth

        sound = parselmouth.Sound(waveform, sampling_frequency=sample_rate)
        # Pitch object covering kid-speech range; matches the F0 contour
        # extractor in :mod:`app.services.real_audio_features`.
        pitch = sound.to_pitch(time_step=0.01, pitch_floor=75.0, pitch_ceiling=600.0)
        point_process = parselmouth.praat.call(
            [sound, pitch], "To PointProcess (cc)"
        )

        # Jitter (local, %) — Praat emits a fraction; multiply by 100.
        try:
            jitter = parselmouth.praat.call(
                point_process,
                "Get jitter (local)",
                0.0, 0.0, 0.0001, 0.02, 1.3,
            )
            if jitter is not None and not math.isnan(jitter):
                jitter_pct = round(float(jitter) * 100.0, 3)
        except Exception:  # pragma: no cover - praat numerical edge
            logger.debug("jitter extraction failed", exc_info=True)

        # Shimmer (local, %)
        try:
            shimmer = parselmouth.praat.call(
                [sound, point_process],
                "Get shimmer (local)",
                0.0, 0.0, 0.0001, 0.02, 1.3, 1.6,
            )
            if shimmer is not None and not math.isnan(shimmer):
                shimmer_pct = round(float(shimmer) * 100.0, 3)
        except Exception:  # pragma: no cover - praat numerical edge
            logger.debug("shimmer extraction failed", exc_info=True)

        # HNR (dB)
        try:
            harmonicity = sound.to_harmonicity()
            hnr_value = parselmouth.praat.call(
                harmonicity, "Get mean", 0.0, 0.0
            )
            if hnr_value is not None and not math.isnan(hnr_value):
                hnr_db = round(float(hnr_value), 2)
        except Exception:  # pragma: no cover - praat numerical edge
            logger.debug("HNR extraction failed", exc_info=True)

        backend = "real-praat"
    except Exception:
        # parselmouth missing or blew up — fall back to librosa heuristics.
        logger.debug(
            "parselmouth unavailable for voice quality; using librosa fallback",
            exc_info=True,
        )
        jitter_pct, shimmer_pct, hnr_db = _voice_quality_librosa(
            waveform, sample_rate
        )
        backend = "real-librosa"

    words = _word_count(transcript)
    if duration_sec > 0:
        speech_rate_wpm = round(words / (duration_sec / 60.0), 2)
    else:
        speech_rate_wpm = 0.0

    flags = _flags_for(
        jitter_pct=jitter_pct,
        shimmer_pct=shimmer_pct,
        hnr_db=hnr_db,
        speech_rate_wpm=speech_rate_wpm,
    )

    return {
        "jitter_local_pct": jitter_pct,
        "shimmer_local_pct": shimmer_pct,
        "hnr_db": hnr_db,
        "speech_rate_wpm": speech_rate_wpm,
        "voiced_seconds": voiced_seconds,
        "flags": flags,
        "backend": backend,
    }


def _voice_quality_librosa(waveform: Any, sample_rate: int) -> tuple[float, float, float]:
    """Librosa-only jitter/shimmer/HNR fallback when parselmouth is absent.

    These estimates are noticeably less accurate than Praat's but keep
    the response shape stable so the rest of the pipeline (schemas,
    flags, recommendations) does not have to special-case the
    fallback.
    """

    try:
        import librosa
        import numpy as np
    except Exception:  # pragma: no cover - hard deps for the real backend
        return 0.0, 0.0, 0.0

    waveform = np.asarray(waveform, dtype=np.float64)
    if waveform.size == 0:
        return 0.0, 0.0, 0.0

    # Pitch contour for jitter — pyin returns NaN for unvoiced frames.
    try:
        f0, voiced_flag, _vp = librosa.pyin(
            waveform.astype(np.float32),
            fmin=float(librosa.note_to_hz("C2")),
            fmax=float(librosa.note_to_hz("C6")),
            sr=sample_rate,
        )
    except Exception:  # pragma: no cover - librosa numerical edge
        return 0.0, 0.0, 0.0

    voiced_periods = 1.0 / f0[voiced_flag & ~np.isnan(f0)]
    jitter_pct = 0.0
    if voiced_periods.size >= 2:
        diffs = np.abs(np.diff(voiced_periods))
        mean_period = float(voiced_periods.mean()) or 1.0
        jitter_pct = round(float(diffs.mean() / mean_period) * 100.0, 3)

    # Frame-energy series for shimmer.
    rms = librosa.feature.rms(y=waveform.astype(np.float32))[0]
    shimmer_pct = 0.0
    if rms.size >= 2:
        rms_clipped = rms[rms > 0]
        if rms_clipped.size >= 2:
            diffs = np.abs(np.diff(rms_clipped))
            mean_amp = float(rms_clipped.mean()) or 1.0
            shimmer_pct = round(float(diffs.mean() / mean_amp) * 100.0, 3)

    # HNR estimate via harmonic/percussive separation.
    try:
        harmonic, percussive = librosa.effects.hpss(waveform.astype(np.float32))
        h_energy = float((harmonic ** 2).sum())
        p_energy = float((percussive ** 2).sum()) or 1e-9
        if h_energy > 0:
            hnr_db = round(10.0 * math.log10(h_energy / p_energy), 2)
        else:
            hnr_db = 0.0
    except Exception:  # pragma: no cover - librosa numerical edge
        hnr_db = 0.0

    return jitter_pct, shimmer_pct, hnr_db


__all__ = [
    "HNR_HEALTHY_MIN_DB",
    "JITTER_HEALTHY_MAX_PCT",
    "SHIMMER_HEALTHY_MAX_PCT",
    "SPEECH_RATE_NORMAL_HIGH_WPM",
    "SPEECH_RATE_NORMAL_LOW_WPM",
    "compute_voice_quality_mock",
    "compute_voice_quality_real",
]
