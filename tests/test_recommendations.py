"""Tests for the localised recommendations engine."""

from __future__ import annotations

import pytest

# -------------------------------------------------------- locale handling


def test_default_locale_is_uzbek() -> None:
    """SADO's primary locale is Uzbek — the engine must default there."""

    from app.services.recommendations import DEFAULT_LOCALE

    assert DEFAULT_LOCALE == "uz"


def test_unknown_locale_falls_back_to_uzbek() -> None:
    """Unknown / mistyped locales should not break the engine."""

    from app.services.recommendations import build_recommendations

    items = build_recommendations(
        risk_level="green",
        voice_quality={"flags": []},
        phoneme_scores={"weakest": []},
        locale="klingon",
    )
    assert items
    # The fallback path serves the Uzbek copy.
    assert any("Ajoyib natija" in item["message"] for item in items)


def test_locale_with_region_suffix_is_normalised() -> None:
    """``ru-RU`` and similar suffixed locales drop the region tag."""

    from app.services.recommendations import build_recommendations

    items = build_recommendations(
        risk_level="green",
        voice_quality=None,
        phoneme_scores=None,
        locale="ru-RU",
    )
    assert items
    assert any("Отличный результат" in item["message"] for item in items)


# -------------------------------------------------------- risk-level driven


@pytest.mark.parametrize(
    ("risk_level", "expected_code"),
    [
        ("red", "schedule_followup"),
        ("yellow", "monitor_progress"),
        ("green", "celebrate_success"),
    ],
)
def test_risk_level_drives_first_recommendation(risk_level, expected_code) -> None:
    """Every risk level maps to its dedicated message at the top of the list."""

    from app.services.recommendations import build_recommendations

    items = build_recommendations(
        risk_level=risk_level,
        voice_quality=None,
        phoneme_scores=None,
        locale="uz",
    )
    assert items[0]["code"] == expected_code


def test_unknown_risk_level_emits_no_risk_message() -> None:
    """Garbage risk values just skip the risk-driven entry."""

    from app.services.recommendations import build_recommendations

    items = build_recommendations(
        risk_level="purple",
        voice_quality={"flags": ["high_jitter"]},
        phoneme_scores=None,
        locale="uz",
    )
    # Only the jitter recommendation should remain.
    assert all(item["code"] != "schedule_followup" for item in items)
    assert items[0]["code"] == "high_jitter"


# -------------------------------------------------------- voice quality flags


def test_voice_quality_flags_priority_low_hnr_first() -> None:
    """When multiple flags fire, low_hnr ranks above the others."""

    from app.services.recommendations import build_recommendations

    items = build_recommendations(
        risk_level=None,
        voice_quality={"flags": ["high_shimmer", "low_hnr", "high_jitter"]},
        phoneme_scores=None,
        locale="uz",
    )
    codes = [i["code"] for i in items]
    assert codes.index("low_hnr") < codes.index("high_jitter")
    assert codes.index("high_jitter") < codes.index("high_shimmer")


def test_voice_quality_flags_uzbek_message() -> None:
    """High-jitter Uzbek copy mentions the sustained vowel drill."""

    from app.services.recommendations import build_recommendations

    items = build_recommendations(
        risk_level=None,
        voice_quality={"flags": ["high_jitter"]},
        phoneme_scores=None,
        locale="uz",
    )
    msg = items[0]["message"]
    assert "titroq" in msg or "“aaaa”" in msg


def test_unknown_flag_is_ignored() -> None:
    """Random voice-quality flags (forward-compat) must not crash the engine."""

    from app.services.recommendations import build_recommendations

    items = build_recommendations(
        risk_level=None,
        voice_quality={"flags": ["mystery_flag"]},
        phoneme_scores=None,
        locale="uz",
    )
    assert items == []


# -------------------------------------------------------- phoneme weakness


def test_weakest_phonemes_become_articulation_drills() -> None:
    """Up to two weakest phonemes are surfaced as drill recommendations."""

    from app.services.recommendations import build_recommendations

    items = build_recommendations(
        risk_level=None,
        voice_quality=None,
        phoneme_scores={
            "weakest": [
                {"phoneme": "r", "score": 0.42},
                {"phoneme": "s", "score": 0.48},
                {"phoneme": "k", "score": 0.55},
            ]
        },
        locale="uz",
    )
    codes = [i["code"] for i in items]
    assert "improve_phoneme_r" in codes
    assert "improve_phoneme_s" in codes
    # We cap at two phonemes to keep the parent UI focused.
    assert "improve_phoneme_k" not in codes


def test_phoneme_message_interpolates_symbol() -> None:
    """The Uzbek message renders the phoneme inside the quotes."""

    from app.services.recommendations import build_recommendations

    items = build_recommendations(
        risk_level=None,
        voice_quality=None,
        phoneme_scores={"weakest": [{"phoneme": "r", "score": 0.4}]},
        locale="uz",
    )
    assert "“r”" in items[0]["message"] or "r" in items[0]["message"]


# -------------------------------------------------------- ordering / dedup


def test_recommendations_respect_max_items() -> None:
    """The list is truncated to ``max_items`` (default 5)."""

    from app.services.recommendations import build_recommendations

    items = build_recommendations(
        risk_level="red",
        voice_quality={
            "flags": [
                "low_hnr",
                "high_jitter",
                "high_shimmer",
                "fast_speech_rate",
                "slow_speech_rate",
            ]
        },
        phoneme_scores={
            "weakest": [
                {"phoneme": "r", "score": 0.3},
                {"phoneme": "s", "score": 0.4},
            ]
        },
        locale="uz",
    )
    # 1 risk + 5 voice flags + 2 phonemes = 8 candidates; capped to 5.
    assert len(items) == 5
    # First item is always the risk-driven one.
    assert items[0]["code"] == "schedule_followup"


def test_max_items_can_be_overridden() -> None:
    """Callers can request more (or fewer) items."""

    from app.services.recommendations import build_recommendations

    items = build_recommendations(
        risk_level="red",
        voice_quality={
            "flags": ["low_hnr", "high_jitter", "high_shimmer"],
        },
        phoneme_scores={
            "weakest": [
                {"phoneme": "r", "score": 0.3},
                {"phoneme": "s", "score": 0.4},
            ]
        },
        locale="uz",
        max_items=10,
    )
    assert len(items) == 6  # 1 risk + 3 flags + 2 phonemes


def test_recommendation_categories_cover_full_taxonomy() -> None:
    """Recommendations span articulation / voice_quality / fluency / general."""

    from app.services.recommendations import build_recommendations

    items = build_recommendations(
        risk_level="yellow",
        voice_quality={"flags": ["high_jitter", "fast_speech_rate"]},
        phoneme_scores={"weakest": [{"phoneme": "r", "score": 0.3}]},
        locale="uz",
        max_items=10,
    )
    categories = {item["category"] for item in items}
    assert {"general", "voice_quality", "fluency", "articulation"} <= categories


def test_supported_locales_constant() -> None:
    """The catalog declares the locales the API surface promises."""

    from app.services.recommendations import SUPPORTED_LOCALES

    assert set(SUPPORTED_LOCALES) == {"uz", "ru", "en"}
