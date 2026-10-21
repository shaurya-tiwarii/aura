"""The API router tree.

Every route module is mounted here and nowhere else, so ``app.main`` stays a
single ``include_router`` call and there is one place to check what the API
actually exposes.

Health probes are mounted at the root rather than under ``/api/v1`` because
orchestrators, uptime monitors and platform health checks expect ``/health``
without a version prefix, and that path must stay stable across API versions.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import (
    analytics,
    assistant,
    auth,
    businesses,
    content,
    gemini,
    health,
    improvements,
    opportunities,
    meta,
    monitoring,
    publishing,
    recommendations,
    scans,
    system,
)

API_V1_PREFIX = "/api/v1"

# Unversioned: infrastructure-facing, must not move when the API version does.
root_router = APIRouter()
root_router.include_router(health.router)

# Versioned: everything the frontend calls.
api_router = APIRouter(prefix=API_V1_PREFIX)
api_router.include_router(meta.router)
api_router.include_router(auth.router)
api_router.include_router(businesses.router)
api_router.include_router(content.router)
api_router.include_router(scans.router)
api_router.include_router(recommendations.router)
api_router.include_router(improvements.router)
api_router.include_router(opportunities.router)
api_router.include_router(gemini.router)
api_router.include_router(publishing.router)
api_router.include_router(monitoring.router)
api_router.include_router(system.router)
api_router.include_router(analytics.router)
api_router.include_router(assistant.router)

__all__ = ["API_V1_PREFIX", "api_router", "root_router"]
