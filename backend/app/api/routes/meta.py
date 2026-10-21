"""Capability discovery.

This endpoint is the mechanism behind rule §46 ("no fake functionality"). The
frontend does not assume a feature works -- it asks. Anything reported as
``demo`` names the exact environment variable that would make it live, so the UI
can say "LinkedIn publishing is simulated; set LINKEDIN_CLIENT_ID and
LINKEDIN_CLIENT_SECRET to enable it" instead of rendering a button that quietly
does nothing.

No secret value is ever returned -- only whether one is present, and its name.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import AuthProvider
from app.core.config import settings

router = APIRouter(prefix="/meta", tags=["meta"])


class CapabilityReportOut(BaseModel):
    """What this installation can genuinely do right now."""

    app: str
    version: str
    environment: str
    auth_provider: str
    database: str
    # The nested blocks come straight from ``Settings.capability_report()``, which
    # is the single source of truth for the real-vs-demo decision made by the
    # services themselves. Duplicating that logic here would let the UI and the
    # backend disagree about what is real.
    ai: dict[str, Any]
    token_encryption: dict[str, Any]
    connectors: dict[str, Any]
    scheduler: dict[str, Any]
    auth: dict[str, Any]


@router.get(
    "/capabilities",
    response_model=CapabilityReportOut,
    summary="Which features are live, which are demo, and what would enable them",
)
def capabilities(backend: AuthProvider) -> CapabilityReportOut:
    report = settings.capability_report()
    return CapabilityReportOut(
        app=settings.app_name,
        version=settings.app_version,
        environment=report["environment"],
        auth_provider=report["auth_provider"],
        database=report["database"],
        ai=report["ai"],
        token_encryption=report["token_encryption"],
        connectors=report["connectors"],
        scheduler=report["scheduler"],
        auth=backend.capabilities.as_dict(),
    )
