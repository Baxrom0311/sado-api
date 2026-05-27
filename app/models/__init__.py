"""ORM models — every module imports here so ``Base.metadata`` is full.

Importing :mod:`app.models` registers all tables on the declarative
base, which is what Alembic and ``Base.metadata.create_all()`` depend on.
"""

from __future__ import annotations

from app.models.assessment import (
    AnalysisResult,
    Assessment,
    AssessmentStatus,
    AssessmentType,
    AudioRecording,
    RecordingTaskType,
    RiskLevel,
)
from app.models.child import Child
from app.models.exercise import (
    AssignmentStatus,
    Exercise,
    ExerciseAgeGroup,
    ExerciseAssignment,
    ExerciseCategory,
    ExerciseDifficulty,
)
from app.models.kindergarten import Kindergarten
from app.models.notification import Notification, NotificationType
from app.models.region import Region, RegionType
from app.models.user import User, UserLanguage, UserRole

__all__ = [
    "AnalysisResult",
    "Assessment",
    "AssessmentStatus",
    "AssessmentType",
    "AssignmentStatus",
    "AudioRecording",
    "Child",
    "Exercise",
    "ExerciseAgeGroup",
    "ExerciseAssignment",
    "ExerciseCategory",
    "ExerciseDifficulty",
    "Kindergarten",
    "Notification",
    "NotificationType",
    "RecordingTaskType",
    "Region",
    "RegionType",
    "RiskLevel",
    "User",
    "UserLanguage",
    "UserRole",
]
