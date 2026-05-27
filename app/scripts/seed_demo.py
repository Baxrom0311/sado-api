"""Idempotent demo-data seed script for the SADO API.

Run with::

    python -m app.scripts.seed_demo

The script populates the database with a small, realistic dataset for
manual exploration of the dashboard and mobile clients:

* 1 country + 2 regions (Tashkent, Samarqand)
* 4 users (admin, therapist, teacher, parent) — password ``demo1234``
* 1 kindergarten in Tashkent, owned by the teacher
* 2 children registered to the parent
* 6 exercises spanning multiple categories / age groups
* 1 completed screening assessment per child (with mock analysis)
* 3 in-app notifications

**Idempotency.** Every entity has a deterministic UUID (``uuid5``) so
re-running the script never creates duplicates and never wipes data —
the script issues an ``INSERT`` only when ``session.get(Model, id)``
returns ``None``. Running ``seed_demo`` five times leaves the database
in the same state as running it once.

The script is safe to import (no side-effects at import time) and
exposes :func:`seed_demo` for use from tests.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.database import create_all, get_sessionmaker
from app.models import (
    AnalysisResult,
    Assessment,
    AssessmentStatus,
    AssessmentType,
    AudioRecording,
    Child,
    Exercise,
    ExerciseAgeGroup,
    ExerciseCategory,
    ExerciseDifficulty,
    Kindergarten,
    Notification,
    NotificationType,
    RecordingTaskType,
    Region,
    RegionType,
    RiskLevel,
    User,
    UserLanguage,
    UserRole,
)

logger = logging.getLogger(__name__)

# A single fixed namespace keeps every generated UUID deterministic so
# re-runs produce the exact same identifiers.
_SEED_NAMESPACE = uuid.UUID("11111111-2222-3333-4444-555555555555")

# Default password used for every demo account.
DEMO_PASSWORD = "demo1234"  # noqa: S105 - documented demo credential


def _seed_id(*parts: str) -> str:
    """Return a stable UUID5 derived from ``parts``."""

    return str(uuid.uuid5(_SEED_NAMESPACE, "::".join(parts)))


@dataclass(frozen=True)
class SeedReport:
    """Counters returned by :func:`seed_demo` so callers can verify."""

    regions_created: int = 0
    users_created: int = 0
    kindergartens_created: int = 0
    children_created: int = 0
    exercises_created: int = 0
    assessments_created: int = 0
    notifications_created: int = 0

    @property
    def total_created(self) -> int:
        return (
            self.regions_created
            + self.users_created
            + self.kindergartens_created
            + self.children_created
            + self.exercises_created
            + self.assessments_created
            + self.notifications_created
        )


async def _ensure(session: AsyncSession, model: type, pk: str, **values: Any) -> tuple[Any, bool]:
    """Fetch ``model`` by primary key or insert it.

    Returns ``(instance, created)`` where ``created`` is ``True`` only
    when a new row was added. The function is idempotent — repeat
    invocations with the same ``pk`` always return the existing row.
    """

    existing = await session.get(model, pk)
    if existing is not None:
        return existing, False
    instance = model(id=pk, **values)
    session.add(instance)
    return instance, True


# --------------------------------------------------------------- Regions


async def _seed_regions(session: AsyncSession) -> tuple[dict[str, Region], int]:
    """Insert country + 2 regions; return mapping by slug + created count."""

    country_id = _seed_id("region", "uz")
    tashkent_id = _seed_id("region", "tashkent")
    samarqand_id = _seed_id("region", "samarqand")

    created = 0
    country, was_new = await _ensure(
        session,
        Region,
        country_id,
        name="O'zbekiston",
        code="UZ",
        type=RegionType.COUNTRY.value,
        parent_id=None,
    )
    created += int(was_new)

    tashkent, was_new = await _ensure(
        session,
        Region,
        tashkent_id,
        name="Toshkent",
        code="TSH",
        type=RegionType.REGION.value,
        parent_id=country_id,
    )
    created += int(was_new)

    samarqand, was_new = await _ensure(
        session,
        Region,
        samarqand_id,
        name="Samarqand",
        code="SAM",
        type=RegionType.REGION.value,
        parent_id=country_id,
    )
    created += int(was_new)

    return (
        {"country": country, "tashkent": tashkent, "samarqand": samarqand},
        created,
    )


# ----------------------------------------------------------------- Users


async def _seed_users(
    session: AsyncSession, regions: dict[str, Region]
) -> tuple[dict[str, User], int]:
    """Insert 4 demo users with deterministic ids and a shared password."""

    # Hashing each password adds noticeable wall-clock time. Only hash
    # when at least one user is missing — repeat runs skip this entirely.
    user_specs = [
        ("admin", "admin@sado.uz", UserRole.ADMIN, "SADO Admin", regions["country"].id),
        (
            "therapist",
            "therapist@sado.uz",
            UserRole.THERAPIST,
            "Dilnoza Karimova",
            regions["tashkent"].id,
        ),
        (
            "teacher",
            "teacher@sado.uz",
            UserRole.TEACHER,
            "Aziza Rahimova",
            regions["tashkent"].id,
        ),
        (
            "parent",
            "parent@sado.uz",
            UserRole.PARENT,
            "Nodir Yusupov",
            regions["samarqand"].id,
        ),
    ]

    ids = {slug: _seed_id("user", slug) for slug, *_ in user_specs}
    missing = [spec for spec in user_specs if await session.get(User, ids[spec[0]]) is None]

    password_hash = hash_password(DEMO_PASSWORD) if missing else ""

    users: dict[str, User] = {}
    created = 0
    for slug, email, role, full_name, region_id in user_specs:
        user, was_new = await _ensure(
            session,
            User,
            ids[slug],
            email=email,
            phone=None,
            password_hash=password_hash,
            full_name=full_name,
            role=role.value,
            language=UserLanguage.UZ.value,
            is_active=True,
            is_verified=True,
            region_id=region_id,
        )
        created += int(was_new)
        users[slug] = user
    return users, created


# ----------------------------------------------------------- Kindergartens


async def _seed_kindergartens(
    session: AsyncSession, regions: dict[str, Region]
) -> tuple[dict[str, Kindergarten], int]:
    kg_id = _seed_id("kindergarten", "tashkent-1")
    kindergarten, was_new = await _ensure(
        session,
        Kindergarten,
        kg_id,
        name="MTM № 1 — Toshkent",
        address="Amir Temur ko'chasi, 12",
        phone="+998711234567",
        teacher_count=8,
        child_count=64,
        region_id=regions["tashkent"].id,
    )
    return {"tashkent_1": kindergarten}, int(was_new)


# -------------------------------------------------------------- Children


async def _seed_children(
    session: AsyncSession,
    users: dict[str, User],
    kindergartens: dict[str, Kindergarten],
) -> tuple[dict[str, Child], int]:
    parent_id = users["parent"].id
    kg_id = kindergartens["tashkent_1"].id

    specs = [
        ("alisher", "Alisher Yusupov", date(2020, 4, 15), "male"),
        ("madina", "Madina Yusupova", date(2018, 9, 3), "female"),
    ]
    children: dict[str, Child] = {}
    created = 0
    for slug, name, birth, gender in specs:
        child_id = _seed_id("child", slug)
        child, was_new = await _ensure(
            session,
            Child,
            child_id,
            name=name,
            birth_date=birth,
            gender=gender,
            language="uz",
            notes=None,
            parent_id=parent_id,
            kindergarten_id=kg_id,
        )
        created += int(was_new)
        children[slug] = child
    return children, created


# ------------------------------------------------------------- Exercises


_EXERCISE_SPECS: list[dict[str, Any]] = [
    {
        "slug": "artic-r",
        "title": "‘R’ tovushini mashq qilamiz",
        "description": "Tilning aniq harakatlari bilan ‘r’ tovushini takrorlash mashqi.",
        "category": ExerciseCategory.ARTICULATION,
        "age_group": ExerciseAgeGroup.PRESCHOOL,
        "difficulty": ExerciseDifficulty.EASY,
        "duration_minutes": 5,
        "target_phonemes": "r",
    },
    {
        "slug": "vocab-animals",
        "title": "Hayvonlarni nomlaymiz",
        "description": "Rasmlardagi hayvonlarni ko'rib, ularni ovoz chiqarib aytamiz.",
        "category": ExerciseCategory.VOCABULARY,
        "age_group": ExerciseAgeGroup.TODDLER,
        "difficulty": ExerciseDifficulty.EASY,
        "duration_minutes": 4,
        "target_phonemes": None,
    },
    {
        "slug": "phonemic-rhyme",
        "title": "Qofiyali so'zlar o'yini",
        "description": "Bola eshitgan so'zga qofiyali so'z topadi.",
        "category": ExerciseCategory.PHONEMIC_AWARENESS,
        "age_group": ExerciseAgeGroup.EARLY_PRIMARY,
        "difficulty": ExerciseDifficulty.MEDIUM,
        "duration_minutes": 6,
        "target_phonemes": None,
    },
    {
        "slug": "fluency-story",
        "title": "Hikoyani ravon aytamiz",
        "description": "Qisqa hikoyani to'xtamasdan, ravon o'qish mashqi.",
        "category": ExerciseCategory.FLUENCY,
        "age_group": ExerciseAgeGroup.PRIMARY,
        "difficulty": ExerciseDifficulty.HARD,
        "duration_minutes": 8,
        "target_phonemes": None,
    },
    {
        "slug": "listen-instructions",
        "title": "Ko'rsatmalarni tinglaymiz",
        "description": "Ikki bosqichli ko'rsatmalarni eshitib, bajarish.",
        "category": ExerciseCategory.LISTENING,
        "age_group": ExerciseAgeGroup.PRESCHOOL,
        "difficulty": ExerciseDifficulty.MEDIUM,
        "duration_minutes": 5,
        "target_phonemes": None,
    },
    {
        "slug": "breathing-balloon",
        "title": "Sharsharak — nafas mashqi",
        "description": "Sharni puflash kabi uzun, bir tekis nafas chiqarish mashqi.",
        "category": ExerciseCategory.BREATHING,
        "age_group": ExerciseAgeGroup.TODDLER,
        "difficulty": ExerciseDifficulty.EASY,
        "duration_minutes": 3,
        "target_phonemes": None,
    },
]


async def _seed_exercises(
    session: AsyncSession, users: dict[str, User]
) -> tuple[dict[str, Exercise], int]:
    therapist_id = users["therapist"].id
    exercises: dict[str, Exercise] = {}
    created = 0
    for spec in _EXERCISE_SPECS:
        ex_id = _seed_id("exercise", spec["slug"])
        ex, was_new = await _ensure(
            session,
            Exercise,
            ex_id,
            title=spec["title"],
            description=spec["description"],
            category=spec["category"].value,
            age_group=spec["age_group"].value,
            difficulty=spec["difficulty"].value,
            language="uz",
            duration_minutes=spec["duration_minutes"],
            audio_example_path=None,
            image_path=None,
            instructions=None,
            target_phonemes=spec["target_phonemes"],
            is_active=True,
            created_by_id=therapist_id,
        )
        created += int(was_new)
        exercises[spec["slug"]] = ex
    return exercises, created


# ---------------------------------------------------------- Assessments


async def _seed_assessments(
    session: AsyncSession,
    users: dict[str, User],
    children: dict[str, Child],
) -> int:
    """Create one completed screening per child with mock analysis."""

    # Use a fixed UTC anchor so ``created_at`` / ``completed_at`` stay
    # deterministic across re-runs (seeded data should not drift in time).
    anchor = datetime(2024, 6, 1, 9, 0, tzinfo=UTC)
    created_total = 0

    plan = [
        # (child slug, risk, confidence, transcript, offset days)
        ("alisher", RiskLevel.YELLOW, 0.71, "Quyon o'rmonda yashaydi.", 14),
        ("madina", RiskLevel.GREEN, 0.92, "Bizning bog'cha juda chiroyli.", 7),
    ]

    for slug, risk, confidence, transcript, days_ago in plan:
        child = children[slug]
        when = anchor - timedelta(days=days_ago)
        assessment_id = _seed_id("assessment", slug)
        assessment, was_new = await _ensure(
            session,
            Assessment,
            assessment_id,
            child_id=child.id,
            created_by_id=users["therapist"].id,
            type=AssessmentType.SCREENING.value,
            status=AssessmentStatus.COMPLETED.value,
            overall_risk=risk.value,
            overall_confidence=confidence,
            summary=(
                "Yaxshi natijalar. Diqqat: ‘r’ tovushi"
                if risk is RiskLevel.YELLOW
                else "Mustahkam natijalar, bola yoshiga mos rivojlangan."
            ),
            started_at=when,
            completed_at=when + timedelta(minutes=12),
        )
        created_total += int(was_new)

        recording_id = _seed_id("recording", slug)
        recording, was_new = await _ensure(
            session,
            AudioRecording,
            recording_id,
            assessment_id=assessment_id,
            task_type=RecordingTaskType.REPEAT_SENTENCE.value,
            prompt="Quyon o'rmonda yashaydi.",
            storage_key=f"demo/{slug}/repeat-sentence.wav",
            content_type="audio/wav",
            size_bytes=128_000,
            duration_sec=4.5,
            sample_rate=16_000,
            processed=True,
            processing_error=None,
            processed_at=when + timedelta(minutes=11),
        )
        created_total += int(was_new)

        analysis_id = _seed_id("analysis", slug)
        _, was_new = await _ensure(
            session,
            AnalysisResult,
            analysis_id,
            recording_id=recording_id,
            risk_level=risk.value,
            confidence=confidence,
            transcript=transcript,
            mfcc_features={"mean": [0.12, -0.04, 0.31], "std": [0.42, 0.38, 0.40]},
            pitch_data={"mean_hz": 248.0, "std_hz": 32.5},
            formant_data={"f1": 720.0, "f2": 1820.0, "f3": 2680.0},
            phoneme_scores={"r": 0.62, "s": 0.88, "sh": 0.81},
            feature_summary={"snr_db": 22.4, "voiced_ratio": 0.78},
            model_name="mock-xgb-v1",
            model_version="0.1.0",
        )
        created_total += int(was_new)

    return created_total


# ------------------------------------------------------- Notifications


async def _seed_notifications(
    session: AsyncSession,
    users: dict[str, User],
    children: dict[str, Child],
) -> int:
    parent_id = users["parent"].id
    therapist_id = users["therapist"].id

    plan = [
        (
            "n1",
            parent_id,
            NotificationType.ASSESSMENT_COMPLETED,
            "Skrining yakunlandi",
            "Alisherning skrining natijalari tayyor.",
            {"assessment_id": _seed_id("assessment", "alisher")},
            False,
        ),
        (
            "n2",
            parent_id,
            NotificationType.EXERCISE_ASSIGNED,
            "Yangi mashq",
            "Sizga ‘R’ tovushi bo'yicha mashq tayinlandi.",
            {"exercise_id": _seed_id("exercise", "artic-r")},
            False,
        ),
        (
            "n3",
            therapist_id,
            NotificationType.REFERRAL,
            "Yangi yo'naltirish",
            "Yangi qizil belgili bola tahlil qilishga yo'naltirildi.",
            {"child_id": children["alisher"].id},
            True,
        ),
    ]

    created = 0
    for slug, user_id, ntype, title, body, data, is_read in plan:
        nid = _seed_id("notification", slug)
        _, was_new = await _ensure(
            session,
            Notification,
            nid,
            user_id=user_id,
            type=ntype.value,
            title=title,
            body=body,
            data=data,
            read_at=datetime(2024, 6, 1, 12, 0, tzinfo=UTC) if is_read else None,
            is_archived=False,
        )
        created += int(was_new)
    return created


# ------------------------------------------------------------- Public API


async def seed_demo(session: AsyncSession | None = None) -> SeedReport:
    """Seed demo data; safe to invoke repeatedly.

    When ``session`` is ``None`` the function opens a session from the
    application-wide sessionmaker, commits on success, and rolls back on
    error. Tests pass an explicit session so they can wrap the call in
    their own transaction.
    """

    own_session = session is None
    if own_session:
        factory = get_sessionmaker()
        session = factory()

    assert session is not None  # mypy / runtime guard
    try:
        regions, regions_created = await _seed_regions(session)
        await session.flush()

        users, users_created = await _seed_users(session, regions)
        await session.flush()

        kindergartens, kg_created = await _seed_kindergartens(session, regions)
        await session.flush()

        children, children_created = await _seed_children(session, users, kindergartens)
        await session.flush()

        exercises, exercises_created = await _seed_exercises(session, users)
        await session.flush()

        # exercises is consumed indirectly via assignments-free flow; keep
        # the variable usage so static analysers see it.
        _ = exercises

        assessments_created = await _seed_assessments(session, users, children)
        await session.flush()

        notifications_created = await _seed_notifications(session, users, children)

        if own_session:
            await session.commit()
    except Exception:
        if own_session:
            await session.rollback()
        raise
    finally:
        if own_session:
            await session.close()

    report = SeedReport(
        regions_created=regions_created,
        users_created=users_created,
        kindergartens_created=kg_created,
        children_created=children_created,
        exercises_created=exercises_created,
        assessments_created=assessments_created,
        notifications_created=notifications_created,
    )
    logger.info(
        "seed_demo done: regions=%d users=%d kindergartens=%d children=%d "
        "exercises=%d assessments=%d notifications=%d",
        report.regions_created,
        report.users_created,
        report.kindergartens_created,
        report.children_created,
        report.exercises_created,
        report.assessments_created,
        report.notifications_created,
    )
    return report


async def _main() -> None:
    """CLI entry point — ensures schema exists, then seeds."""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    await create_all()
    report = await seed_demo()
    print(  # noqa: T201 - CLI feedback
        f"Seed complete. Newly created rows: {report.total_created}. "
        "Existing rows were left untouched."
    )


if __name__ == "__main__":  # pragma: no cover - CLI guard
    asyncio.run(_main())
