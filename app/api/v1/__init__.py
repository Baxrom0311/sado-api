"""API v1 package."""

from fastapi import APIRouter

from app.api.v1 import (
    assessments,
    auth,
    billing,
    children,
    exercises,
    gamification,
    health,
    kindergartens,
    notifications,
    practice_plans,
    regions,
    stats,
    tenants,
    users,
)

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, tags=["auth"])
api_router.include_router(users.router, tags=["users"])
api_router.include_router(children.router, tags=["children"])
api_router.include_router(regions.router, tags=["regions"])
api_router.include_router(kindergartens.router, tags=["kindergartens"])
api_router.include_router(assessments.router, tags=["assessments"])
api_router.include_router(exercises.router, tags=["exercises"])
api_router.include_router(stats.router, tags=["stats"])
api_router.include_router(notifications.router, tags=["notifications"])
api_router.include_router(gamification.router, tags=["gamification"])
api_router.include_router(tenants.router, tags=["tenants"])
api_router.include_router(practice_plans.router, tags=["practice-plans"])
api_router.include_router(billing.router, tags=["billing"])

__all__ = ["api_router"]
