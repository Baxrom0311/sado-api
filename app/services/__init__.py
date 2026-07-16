"""Service layer entry points."""

from app.services.audio_processor import process_recording
from app.services.auth import AuthService, IssuedTokens, get_deny_list
from app.services.gamification import (
    XPAwardOutcome,
    award_xp,
    cumulative_xp_for_level,
    level_for_xp,
    on_assessment_completed,
    on_exercise_completed,
    xp_for_assessment_completion,
    xp_for_exercise_completion,
    xp_progress_for_level,
)
from app.services.ml_scorer import RiskPrediction, aggregate_risk, predict_risk
from app.services.phoneme_mastery import (
    MASTERY_THRESHOLD,
    get_mastery_rows,
    update_mastery_from_assessment,
)
from app.services.practice_plan import (
    generate_plan_from_assessment,
    load_plan_with_items,
    serialize_plan,
)
from app.services.practice_plan import (
    serialize_item as serialize_plan_item,
)
from app.services.real_audio_features import (
    RealAnalyzerUnavailableError,
    extract_real_features,
)
from app.services.real_audio_features import (
    is_available as real_audio_available,
)
from app.services.recommendations import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    build_recommendations,
)
from app.services.speech_analyzer import SpeechFeatures, extract_features
from app.services.storage import (
    AudioStorage,
    LocalAudioStorage,
    StoredObject,
    build_recording_key,
    get_audio_storage,
    reset_audio_storage,
)
from app.services.voice_quality import (
    HNR_HEALTHY_MIN_DB,
    JITTER_HEALTHY_MAX_PCT,
    SHIMMER_HEALTHY_MAX_PCT,
    SPEECH_RATE_NORMAL_HIGH_WPM,
    SPEECH_RATE_NORMAL_LOW_WPM,
    compute_voice_quality_mock,
    compute_voice_quality_real,
)

__all__ = [
    "DEFAULT_LOCALE",
    "AudioStorage",
    "AuthService",
    "HNR_HEALTHY_MIN_DB",
    "IssuedTokens",
    "JITTER_HEALTHY_MAX_PCT",
    "LocalAudioStorage",
    "MASTERY_THRESHOLD",
    "RealAnalyzerUnavailableError",
    "RiskPrediction",
    "SHIMMER_HEALTHY_MAX_PCT",
    "SPEECH_RATE_NORMAL_HIGH_WPM",
    "SPEECH_RATE_NORMAL_LOW_WPM",
    "SUPPORTED_LOCALES",
    "SpeechFeatures",
    "StoredObject",
    "XPAwardOutcome",
    "aggregate_risk",
    "award_xp",
    "build_recommendations",
    "build_recording_key",
    "compute_voice_quality_mock",
    "compute_voice_quality_real",
    "cumulative_xp_for_level",
    "extract_features",
    "extract_real_features",
    "generate_plan_from_assessment",
    "get_audio_storage",
    "get_deny_list",
    "get_mastery_rows",
    "level_for_xp",
    "load_plan_with_items",
    "on_assessment_completed",
    "on_exercise_completed",
    "predict_risk",
    "process_recording",
    "real_audio_available",
    "reset_audio_storage",
    "serialize_plan",
    "serialize_plan_item",
    "update_mastery_from_assessment",
    "xp_for_assessment_completion",
    "xp_for_exercise_completion",
    "xp_progress_for_level",
]
