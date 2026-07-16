"""Default plan catalogue + idempotent seeder.

The default plans are hard-coded so a fresh database always exposes the
B2C, professional and B2B tiers to the mobile client. Admins can
override prices / names through the database — the seeder only inserts
plans that are missing.

Pricing aligns with the example in the task brief:

* ``basic``       — 39 000 UZS = 3 900 000 tiyin (matches the Payme
  webhook ``amount`` of ``3900000`` shown in the spec).
* ``premium``     — 99 000 UZS for unlimited B2C parents.
* ``logoped_pro`` — 149 000 UZS for therapists with a 50-patient cap.
* ``clinic``      — request-quote tier (price 0 = not directly payable;
  surfaced for marketing only).

Quotas exposed in ``features`` are the source of truth the mobile UI
and the optional :func:`enforce_quota` helper read. ``None`` means
unlimited; an integer is the hard cap.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import BillingPlan, BillingPlanCode

# In tiyin (1 UZS = 100 tiyin).
_BASIC_PRICE_TIYIN = 39_000 * 100        # 3,900,000
_PREMIUM_PRICE_TIYIN = 99_000 * 100      # 9,900,000
_LOGOPED_PRO_PRICE_TIYIN = 149_000 * 100  # 14,900,000
# Clinic tier is sales-led: price 0 disables direct order creation
# (POST /billing/orders returns PLAN_NOT_PAYABLE) but the row is still
# returned by GET /billing/plans for marketing purposes.
_CLINIC_PRICE_TIYIN = 0


DEFAULT_PLANS: list[dict[str, Any]] = [
    {
        "code": BillingPlanCode.FREE.value,
        "name_uz": "Bepul",
        "name_ru": "Бесплатно",
        "description_uz": (
            "1 ta bola, kuniga 3 ta baholash, asosiy mashqlar."
        ),
        "description_ru": (
            "1 ребёнок, 3 оценки в день, базовые упражнения."
        ),
        "price_tiyin": 0,
        "duration_days": 30,
        "features": {
            "max_children": 1,
            "max_assessments_per_day": 3,
            "ai_analyses_per_month": 5,
            "ai_analysis": False,
            "premium_exercises": False,
            "patient_management": False,
            "max_patients": 0,
            "export_pdf": False,
        },
        "is_active": True,
        "sort_order": 0,
    },
    {
        "code": BillingPlanCode.BASIC.value,
        "name_uz": "Asosiy",
        "name_ru": "Базовый",
        "description_uz": (
            "3 tagacha bola, kuniga 5 ta baholash, AI tahlili."
        ),
        "description_ru": (
            "До 3 детей, 5 оценок в день, AI-анализ."
        ),
        "price_tiyin": _BASIC_PRICE_TIYIN,
        "duration_days": 30,
        "features": {
            "max_children": 3,
            "max_assessments_per_day": 5,
            "ai_analyses_per_month": None,
            "ai_analysis": True,
            "premium_exercises": False,
            "patient_management": False,
            "max_patients": 0,
            "export_pdf": True,
        },
        "is_active": True,
        "sort_order": 1,
    },
    {
        "code": BillingPlanCode.PREMIUM.value,
        "name_uz": "Premium",
        "name_ru": "Премиум",
        "description_uz": (
            "Cheksiz bolalar, cheksiz baholashlar, barcha mashqlar."
        ),
        "description_ru": (
            "Безлимит детей и оценок, все упражнения."
        ),
        "price_tiyin": _PREMIUM_PRICE_TIYIN,
        "duration_days": 30,
        "features": {
            "max_children": None,
            "max_assessments_per_day": None,
            "ai_analyses_per_month": None,
            "ai_analysis": True,
            "premium_exercises": True,
            "patient_management": False,
            "max_patients": 0,
            "export_pdf": True,
        },
        "is_active": True,
        "sort_order": 2,
    },
    {
        "code": BillingPlanCode.LOGOPED_PRO.value,
        "name_uz": "Logoped Pro",
        "name_ru": "Логопед Pro",
        "description_uz": (
            "Logopedlar uchun: 50 tagacha bemor, mashq tayinlash, "
            "terapiya rejasi va analitika."
        ),
        "description_ru": (
            "Для логопедов: до 50 пациентов, назначение упражнений, "
            "терапевтический план и аналитика."
        ),
        "price_tiyin": _LOGOPED_PRO_PRICE_TIYIN,
        "duration_days": 30,
        "features": {
            "max_children": None,
            "max_assessments_per_day": None,
            "ai_analyses_per_month": None,
            "ai_analysis": True,
            "premium_exercises": True,
            "patient_management": True,
            "max_patients": 50,
            "export_pdf": True,
            "screening_battery": True,
            "referral_pdf": True,
            "analytics": True,
        },
        "is_active": True,
        "sort_order": 3,
    },
    {
        "code": BillingPlanCode.CLINIC.value,
        "name_uz": "Klinika",
        "name_ru": "Клиника",
        "description_uz": (
            "Bog'cha va klinikalar uchun: ko'p foydalanuvchili "
            "tenant, hisobotlar, prioritet qo'llab-quvvatlash. "
            "Narx — sotuv jamoasi bilan kelishiladi."
        ),
        "description_ru": (
            "Для садиков и клиник: мультитенант с многими "
            "пользователями, отчёты, приоритетная поддержка. "
            "Цена согласовывается отделом продаж."
        ),
        "price_tiyin": _CLINIC_PRICE_TIYIN,
        "duration_days": 30,
        "features": {
            "max_children": None,
            "max_assessments_per_day": None,
            "ai_analyses_per_month": None,
            "ai_analysis": True,
            "premium_exercises": True,
            "patient_management": True,
            "max_patients": None,
            "max_users": 10,
            "export_pdf": True,
            "screening_battery": True,
            "referral_pdf": True,
            "analytics": True,
            "tenant_admin": True,
            "priority_support": True,
        },
        "is_active": True,
        "sort_order": 4,
    },
]


async def ensure_default_plans(session: AsyncSession) -> list[BillingPlan]:
    """Insert any missing default plans. Idempotent.

    Returns the full set of active plans (existing + just-inserted).
    """

    result = await session.execute(select(BillingPlan))
    existing = {p.code: p for p in result.scalars().all()}

    created = False
    for spec in DEFAULT_PLANS:
        if spec["code"] in existing:
            continue
        plan = BillingPlan(**spec)
        session.add(plan)
        created = True

    if created:
        await session.commit()

    result = await session.execute(
        select(BillingPlan)
        .where(BillingPlan.is_active.is_(True))
        .order_by(BillingPlan.sort_order)
    )
    return list(result.scalars().all())


async def get_plan_by_code(
    session: AsyncSession, code: str
) -> BillingPlan | None:
    """Fetch one plan by stable code, or ``None`` if missing/inactive."""

    result = await session.execute(
        select(BillingPlan).where(BillingPlan.code == code)
    )
    return result.scalar_one_or_none()


__all__ = [
    "DEFAULT_PLANS",
    "ensure_default_plans",
    "get_plan_by_code",
]
