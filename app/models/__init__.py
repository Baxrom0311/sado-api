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
from app.models.billing import (
    BillingPlan,
    BillingPlanCode,
    BillingUsageMetric,
    BillingUsageRecord,
    PaymentOrder,
    PaymentOrderState,
    PaymentProvider,
    PaymentTransaction,
    PaymentTransactionState,
    Subscription,
    SubscriptionStatus,
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
from app.models.gamification import (
    Badge,
    BadgeCategory,
    BadgeEarning,
    BadgeRequirementType,
    Gamification,
)
from app.models.kindergarten import Kindergarten
from app.models.notification import Notification, NotificationType
from app.models.phoneme_mastery import MASTERY_THRESHOLD, PhonemeMastery
from app.models.practice_plan import (
    PracticePlan,
    PracticePlanItem,
    PracticePlanItemStatus,
    PracticePlanStatus,
)
from app.models.region import Region, RegionType
from app.models.tenant import SubscriptionPlan, TenantSettings
from app.models.user import User, UserLanguage, UserRole

__all__ = [
    "AnalysisResult",
    "Assessment",
    "AssessmentStatus",
    "AssessmentType",
    "AssignmentStatus",
    "AudioRecording",
    "Badge",
    "BadgeCategory",
    "BadgeEarning",
    "BadgeRequirementType",
    "BillingPlan",
    "BillingPlanCode",
    "BillingUsageMetric",
    "BillingUsageRecord",
    "Child",
    "Exercise",
    "ExerciseAgeGroup",
    "ExerciseAssignment",
    "ExerciseCategory",
    "ExerciseDifficulty",
    "Gamification",
    "Kindergarten",
    "MASTERY_THRESHOLD",
    "Notification",
    "NotificationType",
    "PaymentOrder",
    "PaymentOrderState",
    "PaymentProvider",
    "PaymentTransaction",
    "PaymentTransactionState",
    "PhonemeMastery",
    "PracticePlan",
    "PracticePlanItem",
    "PracticePlanItemStatus",
    "PracticePlanStatus",
    "RecordingTaskType",
    "Region",
    "RegionType",
    "RiskLevel",
    "SubscriptionPlan",
    "Subscription",
    "SubscriptionStatus",
    "TenantSettings",
    "User",
    "UserLanguage",
    "UserRole",
]
