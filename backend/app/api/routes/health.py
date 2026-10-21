"""Liveness and readiness probes.

Two distinct questions, deliberately two endpoints:

* ``/health/live``  -- is the process up? Never touches the database, so a
  database outage does not cause an orchestrator to kill and restart otherwise
  healthy application containers.
* ``/health/ready`` -- can the process actually serve traffic? Runs a real
  ``SELECT 1``. Returns 503 when it cannot, so a load balancer stops sending
  requests instead of routing them into failures.

``/health`` is an alias for the readiness view, because that is what a human
checking "is AURA up?" actually means.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from app.core.config import settings
from app.core.database import check_database_connection

router = APIRouter(tags=["health"])


class LivenessOut(BaseModel):
    status: Literal["ok"]
    app: str
    version: str
    environment: str


class ReadinessOut(BaseModel):
    status: Literal["ok", "degraded"]
    app: str
    version: str
    environment: str
    # Named so the reported value cannot be mistaken for a guess: it is the
    # result of an actual query, not an assumption from configuration.
    database: Literal["connected", "unavailable"]
    database_backend: Literal["postgresql", "sqlite"]


def _liveness() -> LivenessOut:
    return LivenessOut(
        status="ok",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
    )


@router.get(
    "/health/live",
    response_model=LivenessOut,
    summary="Process liveness (no dependencies checked)",
)
def liveness() -> LivenessOut:
    return _liveness()


def _readiness(response: Response) -> ReadinessOut:
    connected = check_database_connection()
    if not connected:
        # 503 rather than 200-with-a-flag: a caller that only looks at the status
        # code must not conclude the service is healthy.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessOut(
        status="ok" if connected else "degraded",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        database="connected" if connected else "unavailable",
        database_backend="sqlite" if settings.is_sqlite else "postgresql",
    )


@router.get(
    "/health/ready",
    response_model=ReadinessOut,
    summary="Readiness: verifies the database with a real query",
)
def readiness(response: Response) -> ReadinessOut:
    return _readiness(response)


@router.get("/health", response_model=ReadinessOut, summary="Service health")
def health(response: Response) -> ReadinessOut:
    return _readiness(response)
