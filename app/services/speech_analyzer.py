"""Mock speech analyser — Whisper STT + acoustic feature extraction.

Real Whisper / librosa pipelines need ~500MB of dependencies and a GPU
to be usable in production. The PROJECT_BRIEF explicitly calls out
mocking these for the demo, so this module produces *deterministic*
fake outputs derived from a hash of the input bytes.

That gives us:

* reproducible test runs (same audio → same features),
* realistic-shaped payloads (MFCC matrix, formant trajectories, etc.),
* zero external dependencies,
* a single seam to swap in real Whisper later.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from typing import Any

# Mock vocabularies — picked to be plausible for Uzbek speech therapy
# prompts. The transcript chosen is deterministic per audio hash.
_UZ_PHRASES = [
    "olma",            # apple
    "non",             # bread
    "ona",             # mother
    "ota",             # father
    "kitob",           # book
    "maktab",          # school
    "salom",           # hello
    "rahmat",          # thank you
    "qush uchadi",     # the bird flies
    "bola o'qiydi",   # the child is reading
]

_PHONEMES = ["a", "e", "i", "o", "u", "k", "l", "m", "n", "p", "r", "s", "t"]

# Acoustic envelope ranges for a typical 4-7 year-old vocal tract.
_F0_RANGE = (180.0, 320.0)         # Hz
_FORMANT_RANGES = [
    (650.0, 1100.0),   # F1 — vowel openness
    (1500.0, 2300.0),  # F2 — vowel frontness
    (2400.0, 3300.0),  # F3 — lip rounding
]


@dataclass(slots=True)
class SpeechFeatures:
    """All acoustic + textual features extracted from one recording."""

    transcript: str
    duration_sec: float
    sample_rate: int
    confidence: float
    mfcc_features: dict[str, Any] = field(default_factory=dict)
    pitch_data: dict[str, Any] = field(default_factory=dict)
    formant_data: dict[str, Any] = field(default_factory=dict)
    phoneme_scores: dict[str, Any] = field(default_factory=dict)
    feature_summary: dict[str, Any] = field(default_factory=dict)


def _seeded_rng(audio_bytes: bytes) -> random.Random:
    """Return a deterministic RNG seeded by the audio hash."""

    digest = hashlib.sha256(audio_bytes).digest()
    seed = int.from_bytes(digest[:8], "big", signed=False)
    return random.Random(seed)


def _gen_mfcc(rng: random.Random, n_frames: int, n_mfcc: int = 13) -> dict[str, Any]:
    """Plausible-looking MFCC matrix.

    We synthesise frames with a slow drift so visualisations look like
    a real heatmap rather than uniform noise.
    """

    matrix: list[list[float]] = []
    base = [rng.uniform(-30.0, 5.0) for _ in range(n_mfcc)]
    for _ in range(n_frames):
        frame = [
            round(base[i] + rng.gauss(0.0, 2.5), 3)
            for i in range(n_mfcc)
        ]
        matrix.append(frame)
        # Drift the base slightly to simulate phoneme transitions.
        for i in range(n_mfcc):
            base[i] += rng.gauss(0.0, 0.4)
    flat = [v for frame in matrix for v in frame]
    return {
        "n_mfcc": n_mfcc,
        "n_frames": n_frames,
        "matrix": matrix,
        "mean": [round(sum(c) / len(c), 3) for c in zip(*matrix, strict=False)] if matrix else [],
        "std": [
            round(
                math.sqrt(sum((x - sum(c) / len(c)) ** 2 for x in c) / len(c)),
                3,
            )
            for c in zip(*matrix, strict=False)
        ]
        if matrix
        else [],
        "min": round(min(flat), 3) if flat else 0.0,
        "max": round(max(flat), 3) if flat else 0.0,
    }


def _gen_pitch(rng: random.Random, n_frames: int) -> dict[str, Any]:
    base = rng.uniform(*_F0_RANGE)
    series = []
    for _ in range(n_frames):
        series.append(round(max(60.0, base + rng.gauss(0.0, 12.0)), 2))
        base += rng.gauss(0.0, 4.0)
        base = max(_F0_RANGE[0] - 30, min(_F0_RANGE[1] + 30, base))
    return {
        "f0_hz": series,
        "f0_mean": round(sum(series) / len(series), 2) if series else 0.0,
        "f0_min": round(min(series), 2) if series else 0.0,
        "f0_max": round(max(series), 2) if series else 0.0,
        "voiced_ratio": round(rng.uniform(0.55, 0.95), 3),
    }


def _gen_formants(rng: random.Random, n_frames: int) -> dict[str, Any]:
    tracks: dict[str, list[float]] = {}
    for idx, (lo, hi) in enumerate(_FORMANT_RANGES, start=1):
        base = rng.uniform(lo, hi)
        track = []
        for _ in range(n_frames):
            base += rng.gauss(0.0, (hi - lo) * 0.04)
            base = max(lo - 50, min(hi + 50, base))
            track.append(round(base, 1))
        tracks[f"f{idx}"] = track
    return {
        "tracks": tracks,
        "f1_mean": round(sum(tracks["f1"]) / len(tracks["f1"]), 1),
        "f2_mean": round(sum(tracks["f2"]) / len(tracks["f2"]), 1),
        "f3_mean": round(sum(tracks["f3"]) / len(tracks["f3"]), 1),
    }


def _gen_phoneme_scores(rng: random.Random) -> dict[str, Any]:
    scores: dict[str, float] = {
        ph: round(rng.uniform(0.4, 1.0), 3) for ph in _PHONEMES
    }
    sorted_items = sorted(scores.items(), key=lambda kv: kv[1])
    return {
        "scores": scores,
        "weakest": [{"phoneme": p, "score": s} for p, s in sorted_items[:3]],
        "strongest": [{"phoneme": p, "score": s} for p, s in sorted_items[-3:]],
    }


def extract_features(
    audio_bytes: bytes,
    *,
    declared_duration_sec: float | None = None,
) -> SpeechFeatures:
    """Run the mock pipeline over one recording and return features.

    The output is fully deterministic for a given byte payload so
    snapshot-style tests stay stable between runs.
    """

    if not audio_bytes:
        raise ValueError("empty audio payload")

    rng = _seeded_rng(audio_bytes)

    # Estimate duration from declared value, falling back to a hash-based
    # synthetic duration in the 3–15s range.
    if declared_duration_sec is not None and declared_duration_sec > 0:
        duration = float(declared_duration_sec)
    else:
        duration = round(rng.uniform(3.0, 15.0), 2)

    sample_rate = 16000
    # 25ms hop → 40 frames per second.
    n_frames = max(20, int(duration * 40))

    mfcc = _gen_mfcc(rng, n_frames=n_frames)
    pitch = _gen_pitch(rng, n_frames=n_frames)
    formants = _gen_formants(rng, n_frames=n_frames)
    phonemes = _gen_phoneme_scores(rng)

    transcript_words = rng.sample(_UZ_PHRASES, k=min(3, len(_UZ_PHRASES)))
    transcript = " ".join(transcript_words)

    confidence = round(rng.uniform(0.55, 0.95), 3)

    summary = {
        "duration_sec": round(duration, 2),
        "sample_rate": sample_rate,
        "n_frames": n_frames,
        "transcript_word_count": len(transcript_words),
        "voiced_ratio": pitch["voiced_ratio"],
        "f0_mean": pitch["f0_mean"],
        "f1_mean": formants["f1_mean"],
        "f2_mean": formants["f2_mean"],
        "weakest_phonemes": [item["phoneme"] for item in phonemes["weakest"]],
    }

    return SpeechFeatures(
        transcript=transcript,
        duration_sec=duration,
        sample_rate=sample_rate,
        confidence=confidence,
        mfcc_features=mfcc,
        pitch_data=pitch,
        formant_data=formants,
        phoneme_scores=phonemes,
        feature_summary=summary,
    )


__all__ = ["SpeechFeatures", "extract_features"]
