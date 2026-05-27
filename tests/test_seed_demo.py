"""Tests for the demo seed script (``app.scripts.seed_demo``).

The seed script must be **idempotent** — running it five times on the
same database must leave exactly the same rows as running it once. We
also verify that the demo accounts can authenticate via the public auth
endpoints, since that's what makes the seed useful for manual demos.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.database import get_sessionmaker
from app.models import (
    AnalysisResult,
    Assessment,
    AudioRecording,
    Child,
    Exercise,
    Kindergarten,
    Notification,
    Region,
    User,
)
from app.scripts.seed_demo import DEMO_PASSWORD, SeedReport, _seed_id, seed_demo


@pytest.mark.asyncio
async def test_seed_demo_first_run_creates_expected_counts(app) -> None:
    """First invocation creates a known set of rows."""

    report = await seed_demo()

    assert isinstance(report, SeedReport)
    assert report.regions_created == 3
    assert report.users_created == 4
    assert report.kindergartens_created == 1
    assert report.children_created == 2
    assert report.exercises_created == 6
    # 1 assessment + 1 recording + 1 analysis per child = 3 × 2 = 6.
    assert report.assessments_created == 6
    assert report.notifications_created == 3
    assert report.total_created == 25

    factory = get_sessionmaker()
    async with factory() as session:
        for model, expected in (
            (Region, 3),
            (User, 4),
            (Kindergarten, 1),
            (Child, 2),
            (Exercise, 6),
            (Assessment, 2),
            (AudioRecording, 2),
            (AnalysisResult, 2),
            (Notification, 3),
        ):
            count = await session.scalar(select(func.count()).select_from(model))
            assert count == expected, f"{model.__name__}: expected {expected}, got {count}"


@pytest.mark.asyncio
async def test_seed_demo_is_idempotent(app) -> None:
    """Running 5× must not change row counts after the first run."""

    first = await seed_demo()
    assert first.total_created > 0

    factory = get_sessionmaker()
    async with factory() as session:
        baseline_users = await session.scalar(select(func.count()).select_from(User))
        baseline_exercises = await session.scalar(select(func.count()).select_from(Exercise))

    for _ in range(5):
        report = await seed_demo()
        assert report.total_created == 0, "re-run should not insert anything"

    async with factory() as session:
        users_now = await session.scalar(select(func.count()).select_from(User))
        exercises_now = await session.scalar(select(func.count()).select_from(Exercise))

    assert users_now == baseline_users
    assert exercises_now == baseline_exercises


@pytest.mark.asyncio
async def test_seed_demo_preserves_user_modifications(app) -> None:
    """Existing rows must never be wiped or overwritten by re-seeding."""

    await seed_demo()

    factory = get_sessionmaker()
    parent_id = _seed_id("user", "parent")

    async with factory() as session:
        parent = await session.get(User, parent_id)
        assert parent is not None
        parent.full_name = "Custom Parent Name"
        await session.commit()

    # Re-run the seeder and verify the manual edit survives untouched.
    report = await seed_demo()
    assert report.total_created == 0

    async with factory() as session:
        parent = await session.get(User, parent_id)
        assert parent is not None
        assert parent.full_name == "Custom Parent Name"


@pytest.mark.asyncio
async def test_seed_demo_accounts_can_login(app, client) -> None:
    """Every demo account should authenticate with the documented password."""

    await seed_demo()

    for email in (
        "admin@sado.uz",
        "therapist@sado.uz",
        "teacher@sado.uz",
        "parent@sado.uz",
    ):
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": DEMO_PASSWORD},
        )
        assert response.status_code == 200, f"{email}: {response.text}"
        body = response.json()
        assert "access_token" in body
        assert "refresh_token" in body


@pytest.mark.asyncio
async def test_seed_id_is_stable() -> None:
    """``_seed_id`` must be a pure function of its inputs."""

    assert _seed_id("user", "admin") == _seed_id("user", "admin")
    assert _seed_id("user", "admin") != _seed_id("user", "parent")
    assert _seed_id("user", "admin") != _seed_id("region", "admin")
