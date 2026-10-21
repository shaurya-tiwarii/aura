"""Tenant-scoped analytics normalisation and dashboard endpoints."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query, status

from app.api.deps import DbSession, OwnedBusiness
from app.models.enums import MetricSource
from app.schemas.analytics import (
    AnalyticsMetricIn,
    AnalyticsMetricOut,
    AnalyticsSnapshotOut,
    AnalyticsSyncOut,
)
from app.services import analytics as analytics_service

router = APIRouter(prefix="/businesses/{business_id}/analytics", tags=["analytics"])


@router.post(
    "/metrics",
    response_model=AnalyticsSyncOut,
    status_code=status.HTTP_201_CREATED,
    summary="Normalise a platform analytics payload",
)
def ingest_metrics(
    payload: AnalyticsMetricIn, business: OwnedBusiness, session: DbSession
) -> AnalyticsSyncOut:
    rows = analytics_service.ingest(session, business, payload)
    session.commit()
    for row in rows:
        session.refresh(row)
    return AnalyticsSyncOut(
        metrics=[AnalyticsMetricOut.model_validate(row) for row in rows],
        source=payload.source,
        demo=payload.source == MetricSource.DEMO,
    )


@router.get("/metrics", response_model=list[AnalyticsMetricOut])
def list_metrics(
    business: OwnedBusiness,
    session: DbSession,
    start: date | None = None,
    end: date | None = None,
    source: MetricSource | None = Query(default=None),  # noqa: B008
) -> list[AnalyticsMetricOut]:
    return [
        AnalyticsMetricOut.model_validate(row)
        for row in analytics_service.list_metrics(
            session,
            business,
            start=start,
            end=end,
            source=source.value if source else None,
        )
    ]


@router.post("/snapshots/rebuild", response_model=list[AnalyticsSnapshotOut])
def rebuild_snapshots(
    business: OwnedBusiness,
    session: DbSession,
    start: date | None = None,
    end: date | None = None,
) -> list[AnalyticsSnapshotOut]:
    rows = analytics_service.rebuild_snapshots(session, business, start=start, end=end)
    session.commit()
    return [AnalyticsSnapshotOut.model_validate(row) for row in rows]


@router.get("/snapshots", response_model=list[AnalyticsSnapshotOut])
def list_snapshots(
    business: OwnedBusiness,
    session: DbSession,
    start: date | None = None,
    end: date | None = None,
) -> list[AnalyticsSnapshotOut]:
    return [
        AnalyticsSnapshotOut.model_validate(row)
        for row in analytics_service.list_snapshots(
            session, business, start=start, end=end
        )
    ]
