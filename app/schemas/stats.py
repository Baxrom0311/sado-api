"""Pydantic schemas for the analytics / stats endpoints.

Shapes follow the dashboard's chart contracts so the front-end can
render Recharts components without remapping.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class RiskDistribution(BaseModel):
    """Counts of completed assessments per risk bucket."""

    green: int = 0
    yellow: int = 0
    red: int = 0
    unknown: int = 0

    @property
    def total(self) -> int:
        return self.green + self.yellow + self.red + self.unknown


class DailyAssessmentPoint(BaseModel):
    """One bucket on the weekly assessments line chart."""

    date: date
    count: int


class RolePopulation(BaseModel):
    """User counts per role — used for therapist KPI."""

    parent: int = 0
    teacher: int = 0
    therapist: int = 0
    admin: int = 0


class SystemStatsResponse(BaseModel):
    """Top-level numbers powering the admin dashboard."""

    model_config = ConfigDict(from_attributes=True)

    total_children: int
    total_users: int
    total_kindergartens: int
    total_regions: int
    total_assessments: int
    completed_assessments: int
    assessments_today: int
    active_therapists: int
    red_risk_percentage: float = Field(
        ..., description="Share of completed assessments triaged as Red"
    )
    risk_distribution: RiskDistribution
    user_roles: RolePopulation
    weekly_assessments: list[DailyAssessmentPoint]


class RegionStat(BaseModel):
    """One region's aggregated counts."""

    region_id: str | None
    region_name: str
    children: int
    assessments: int
    risk_distribution: RiskDistribution


class KindergartenStatRow(BaseModel):
    """One row of the kindergarten leaderboard."""

    kindergarten_id: str
    name: str
    region_id: str | None
    region_name: str | None
    child_count: int
    assessments: int
    red_count: int
    yellow_count: int
    green_count: int


class RegionalStatsResponse(BaseModel):
    """Payload for the Regional Statistics page."""

    regions: list[RegionStat]
    kindergartens: list[KindergartenStatRow]
    daily_trend: list[DailyAssessmentPoint]


__all__ = [
    "DailyAssessmentPoint",
    "KindergartenStatRow",
    "RegionStat",
    "RegionalStatsResponse",
    "RiskDistribution",
    "RolePopulation",
    "SystemStatsResponse",
]
