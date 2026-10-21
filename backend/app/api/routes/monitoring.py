"""Recurring website monitoring endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.deps import DbSession, OwnedBusiness
from app.schemas.monitoring import MonitoringOut, MonitoringUpdate
from app.services import business as business_service
from app.services import monitoring as monitoring_service
from app.scheduler.service import register_monitor, unregister_monitor

router = APIRouter(
    prefix="/businesses/{business_id}/websites/{website_id}/monitoring",
    tags=["monitoring"],
)


@router.get("", response_model=MonitoringOut)
def get_monitoring(
    website_id: uuid.UUID, business: OwnedBusiness, session: DbSession
) -> MonitoringOut:
    website = business_service.get_owned_website(session, business, website_id)
    return MonitoringOut.model_validate(website)


@router.patch("", response_model=MonitoringOut)
def update_monitoring(
    website_id: uuid.UUID,
    payload: MonitoringUpdate,
    business: OwnedBusiness,
    session: DbSession,
) -> MonitoringOut:
    website = business_service.get_owned_website(session, business, website_id)
    if website.monitor_job_id:
        unregister_monitor(website)
    monitoring_service.update_monitoring(session, website, payload)
    if payload.enabled:
        register_monitor(website)
    session.commit()
    session.refresh(website)
    return MonitoringOut.model_validate(website)
