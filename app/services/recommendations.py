"""Therapist-style recommendations driven by acoustic features.

The recommendations engine reads the deterministic outputs of the
speech analyzer (phoneme scores, voice-quality metrics, risk level)
and produces a short, prioritised list of action items in the user's
locale.

Each recommendation has the shape::

    {
        "code": "improve_phoneme_r",      # stable identifier
        "severity": "yellow",             # green | yellow | red
        "category": "articulation",       # articulation | voice_quality | fluency | general
        "message": "<localised string>",  # rendered for the requested locale
    }

Recommendations are *advisory* — never a clinical diagnosis. The
catalog below is hand-translated; ``uz`` (Uzbek) is the primary locale
used by the SADO product, ``ru`` (Russian) is included as a fallback
for bilingual families, and ``en`` (English) is the developer-friendly
copy used by tests and admin tooling.
"""

from __future__ import annotations

from typing import Any, Literal

from app.models.assessment import RiskLevel

Locale = Literal["uz", "ru", "en"]
DEFAULT_LOCALE: Locale = "uz"
SUPPORTED_LOCALES: tuple[Locale, ...] = ("uz", "ru", "en")


# Catalog of recommendation templates. Keys are stable identifiers
# referenced by tests and the therapist UI.
_TEMPLATES: dict[str, dict[str, Any]] = {
    # ----------------------------------------------- voice quality
    "high_jitter": {
        "category": "voice_quality",
        "severity": "yellow",
        "messages": {
            "uz": (
                "Ovoz tovushida titroq sezildi. Bolaga sokin muhitda "
                "“aaaa” unlisini 5 soniya cho‘zib aytishni mashq qildiring."
            ),
            "ru": (
                "Замечена нестабильность высоты голоса. Попросите ребёнка "
                "тянуть звук «а-а-а» в течение 5 секунд в тихой обстановке."
            ),
            "en": (
                "Pitch instability detected. Practice sustained 'aaaa' "
                "vowels for 5 seconds in a quiet room."
            ),
        },
    },
    "high_shimmer": {
        "category": "voice_quality",
        "severity": "yellow",
        "messages": {
            "uz": (
                "Ovoz balandligi notekis. Nafas mashqlari bilan boshlang: "
                "burun orqali nafas oling, og‘iz orqali sekin chiqaring."
            ),
            "ru": (
                "Громкость голоса нестабильна. Начните с дыхательных "
                "упражнений: вдох носом, медленный выдох ртом."
            ),
            "en": (
                "Loudness is unstable. Start with breathing drills: inhale "
                "through the nose, exhale slowly through the mouth."
            ),
        },
    },
    "low_hnr": {
        "category": "voice_quality",
        "severity": "red",
        "messages": {
            "uz": (
                "Ovoz xirildoq eshitildi. Iltimos, bola juda ko‘p baqirgan "
                "yoki shamollagan bo‘lmasin. Bir hafta kuzatib, qayta yozib oling."
            ),
            "ru": (
                "Голос звучит хрипло. Убедитесь, что ребёнок не перекричал "
                "или не простужен. Понаблюдайте неделю и запишите снова."
            ),
            "en": (
                "Voice sounds hoarse. Make sure the child has not been "
                "shouting or is not unwell. Re-record after a week of rest."
            ),
        },
    },
    "slow_speech_rate": {
        "category": "fluency",
        "severity": "yellow",
        "messages": {
            "uz": (
                "Nutq tezligi pastroq. Bolaga qo‘shiq aytish va ritmik "
                "she’rlar bilan tezligini oshirishga yordam bering."
            ),
            "ru": (
                "Темп речи замедлен. Помогите ребёнку песнями и ритмичными "
                "стихами увеличить скорость речи."
            ),
            "en": (
                "Speaking rate is slow. Use songs and rhythmic poems to "
                "encourage a faster, more fluent rate."
            ),
        },
    },
    "fast_speech_rate": {
        "category": "fluency",
        "severity": "yellow",
        "messages": {
            "uz": (
                "Nutq tezligi yuqori — so‘zlar tushunarsiz bo‘lishi mumkin. "
                "Har bir gapdan keyin to‘xtab nafas olishni mashq qiling."
            ),
            "ru": (
                "Темп речи слишком высокий — слова могут быть неразборчивы. "
                "Учите делать паузу и вдох после каждого предложения."
            ),
            "en": (
                "Speaking rate is too fast — words can be unclear. Practice "
                "pausing and inhaling after each sentence."
            ),
        },
    },
    # ----------------------------------------------- articulation (phonemes)
    "improve_phoneme": {
        "category": "articulation",
        "severity": "yellow",
        "messages": {
            "uz": (
                "“{phoneme}” tovushini aniq talaffuz qilish ustida ishlang. "
                "Kuniga 5 daqiqalik mashqdan boshlang."
            ),
            "ru": (
                "Поработайте над чёткой артикуляцией звука «{phoneme}». "
                "Начните с 5 минут упражнений в день."
            ),
            "en": (
                "Work on the '{phoneme}' sound. Start with 5-minute daily "
                "articulation drills."
            ),
        },
    },
    # ----------------------------------------------- general (risk-level)
    "schedule_followup": {
        "category": "general",
        "severity": "red",
        "messages": {
            "uz": (
                "Natijalar yuqori xavf darajasini ko‘rsatmoqda. Iltimos, "
                "logoped bilan uchrashuvni rejalashtiring."
            ),
            "ru": (
                "Результаты указывают на высокий риск. Пожалуйста, "
                "запланируйте консультацию с логопедом."
            ),
            "en": (
                "Results show a high-risk profile. Please schedule a "
                "follow-up with a speech-language pathologist."
            ),
        },
    },
    "monitor_progress": {
        "category": "general",
        "severity": "yellow",
        "messages": {
            "uz": (
                "Bola yaxshi natija ko‘rsatmoqda, lekin diqqat talab "
                "qiluvchi nuqtalar bor. Haftasiga 2–3 marta mashq qiling."
            ),
            "ru": (
                "Ребёнок показывает хороший результат, но есть моменты, "
                "требующие внимания. Занимайтесь 2–3 раза в неделю."
            ),
            "en": (
                "The child is doing well but a few areas need attention. "
                "Practice 2–3 times per week."
            ),
        },
    },
    "celebrate_success": {
        "category": "general",
        "severity": "green",
        "messages": {
            "uz": (
                "Ajoyib natija! Bolani maqtang va sevimli mashqlarni davom "
                "ettiring."
            ),
            "ru": (
                "Отличный результат! Похвалите ребёнка и продолжайте "
                "любимые упражнения."
            ),
            "en": (
                "Great job! Praise the child and keep doing the favourite "
                "exercises."
            ),
        },
    },
}


def _localise(template_key: str, locale: Locale, **fmt: object) -> str:
    """Return the localised message for ``template_key``.

    Falls back to :data:`DEFAULT_LOCALE` when the requested locale is
    missing, which keeps the engine resilient to typos coming from
    user-facing settings.
    """

    template = _TEMPLATES[template_key]
    messages = template["messages"]
    raw = messages.get(locale) or messages[DEFAULT_LOCALE]
    return raw.format(**fmt) if fmt else raw


def _make(
    code: str,
    template_key: str,
    locale: Locale,
    **fmt: object,
) -> dict[str, Any]:
    template = _TEMPLATES[template_key]
    return {
        "code": code,
        "category": template["category"],
        "severity": template["severity"],
        "message": _localise(template_key, locale, **fmt),
    }


def _normalize_locale(locale: str | None) -> Locale:
    if not locale:
        return DEFAULT_LOCALE
    candidate = locale.lower().split("-")[0]
    if candidate in SUPPORTED_LOCALES:
        return candidate  # type: ignore[return-value]
    return DEFAULT_LOCALE


def build_recommendations(
    *,
    risk_level: str | None,
    voice_quality: dict[str, Any] | None,
    phoneme_scores: dict[str, Any] | None,
    locale: str | None = None,
    max_items: int = 5,
) -> list[dict[str, Any]]:
    """Produce a prioritised, localised recommendation list.

    Ordering rules:

    1. Risk-level-driven recommendation first (RED → schedule follow-up,
       YELLOW → monitor progress, GREEN → celebrate).
    2. Voice-quality flags next (jitter / shimmer / HNR / speech rate).
    3. Up to two weakest phonemes after that.

    The list is then truncated to ``max_items`` so we never spam the
    parent UI.
    """

    locale_resolved = _normalize_locale(locale)
    items: list[dict[str, Any]] = []
    seen_codes: set[str] = set()

    def push(item: dict[str, Any]) -> None:
        if item["code"] in seen_codes:
            return
        seen_codes.add(item["code"])
        items.append(item)

    # ---- 1. Risk-level driven --------------------------------------------------
    if risk_level == RiskLevel.RED.value:
        push(_make("schedule_followup", "schedule_followup", locale_resolved))
    elif risk_level == RiskLevel.YELLOW.value:
        push(_make("monitor_progress", "monitor_progress", locale_resolved))
    elif risk_level == RiskLevel.GREEN.value:
        push(_make("celebrate_success", "celebrate_success", locale_resolved))

    # ---- 2. Voice-quality flags ------------------------------------------------
    flag_priority = ("low_hnr", "high_jitter", "high_shimmer", "fast_speech_rate", "slow_speech_rate")
    flags = list((voice_quality or {}).get("flags") or [])
    for flag in flag_priority:
        if flag in flags and flag in _TEMPLATES:
            push(_make(flag, flag, locale_resolved))

    # ---- 3. Phoneme weaknesses -------------------------------------------------
    weakest = (phoneme_scores or {}).get("weakest") or []
    for entry in weakest[:2]:
        phoneme = entry.get("phoneme") if isinstance(entry, dict) else None
        if not phoneme:
            continue
        push(
            _make(
                f"improve_phoneme_{phoneme}",
                "improve_phoneme",
                locale_resolved,
                phoneme=phoneme,
            )
        )

    return items[:max_items]


__all__ = [
    "DEFAULT_LOCALE",
    "SUPPORTED_LOCALES",
    "build_recommendations",
]
