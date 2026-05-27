"""Mock XGBoost risk scorer.

The PROJECT_BRIEF non-goals say we do **not** train a real model. This
module reads the deterministic features produced by the speech
analyzer and applies a hand-crafted heuristic to bucket each recording
into Green / Yellow / Red. The function shape (and the
``model_name`` / ``model_version`` metadata) matches what a real
XGBoost predictor would return, so the call site never has to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.assessment import RiskLevel
from app.services.speech_analyzer import SpeechFeatures

MODEL_NAME = "mock-xgb-v1"
MODEL_VERSION = "0.1.0"


@dataclass(slots=True)
class RiskPrediction:
    """ML output bundled with provenance metadata."""

    risk_level: str
    confidence: float
    model_name: str
    model_version: str
    rationale: dict[str, Any]


def _normalize(value: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


def predict_risk(features: SpeechFeatures) -> RiskPrediction:
    """Score a single recording into Green / Yellow / Red.

    Heuristic (loosely calibrated to be plausible, not clinically
    accurate):

    * weak phoneme accuracy → push toward Red
    * low voiced-ratio → push toward Red (sustained-vowel struggles)
    * extreme pitch (very low or very high) → push toward Yellow
    * short duration with low transcript count → Yellow
    """

    # Phoneme weakness — use the bottom-three average.
    weakest = features.phoneme_scores.get("weakest", [])
    if weakest:
        weak_avg = sum(item["score"] for item in weakest) / len(weakest)
    else:
        weak_avg = 1.0
    weakness_score = 1.0 - weak_avg  # 0=strong, 1=very weak

    voiced_ratio = float(features.pitch_data.get("voiced_ratio", 0.8))
    voicing_penalty = max(0.0, 0.7 - voiced_ratio)  # 0 if voiced_ratio>=0.7

    f0_mean = float(features.pitch_data.get("f0_mean", 250.0))
    pitch_extremity = max(
        _normalize(abs(f0_mean - 250.0), 60.0, 150.0),
        0.0,
    )

    duration = max(0.5, features.duration_sec)
    duration_penalty = _normalize(5.0 - duration, 0.0, 4.5)

    raw = (
        0.45 * weakness_score
        + 0.25 * voicing_penalty
        + 0.15 * pitch_extremity
        + 0.15 * duration_penalty
    )

    if raw >= 0.55:
        level = RiskLevel.RED.value
    elif raw >= 0.30:
        level = RiskLevel.YELLOW.value
    else:
        level = RiskLevel.GREEN.value

    # Confidence is highest when raw is far from the boundaries.
    distance = min(abs(raw - 0.30), abs(raw - 0.55), abs(raw), abs(raw - 1.0))
    confidence = round(0.55 + min(0.4, distance * 1.5), 3)

    return RiskPrediction(
        risk_level=level,
        confidence=confidence,
        model_name=MODEL_NAME,
        model_version=MODEL_VERSION,
        rationale={
            "raw_score": round(raw, 4),
            "weakness_score": round(weakness_score, 4),
            "voicing_penalty": round(voicing_penalty, 4),
            "pitch_extremity": round(pitch_extremity, 4),
            "duration_penalty": round(duration_penalty, 4),
        },
    )


def aggregate_risk(predictions: list[RiskPrediction]) -> RiskPrediction:
    """Reduce per-recording predictions into a single assessment-level call.

    Rule: any RED → RED, else any YELLOW → YELLOW, else GREEN.
    Confidence becomes the mean of contributing predictions.
    """

    if not predictions:
        return RiskPrediction(
            risk_level=RiskLevel.GREEN.value,
            confidence=0.0,
            model_name=MODEL_NAME,
            model_version=MODEL_VERSION,
            rationale={"reason": "no recordings"},
        )

    levels = {p.risk_level for p in predictions}
    if RiskLevel.RED.value in levels:
        level = RiskLevel.RED.value
    elif RiskLevel.YELLOW.value in levels:
        level = RiskLevel.YELLOW.value
    else:
        level = RiskLevel.GREEN.value

    confidence = round(sum(p.confidence for p in predictions) / len(predictions), 3)

    return RiskPrediction(
        risk_level=level,
        confidence=confidence,
        model_name=MODEL_NAME,
        model_version=MODEL_VERSION,
        rationale={
            "n_recordings": len(predictions),
            "levels": [p.risk_level for p in predictions],
        },
    )


__all__ = [
    "MODEL_NAME",
    "MODEL_VERSION",
    "RiskPrediction",
    "aggregate_risk",
    "predict_risk",
]
