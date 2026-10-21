"""Continuous-improvement comparison endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.deps import DbSession, OwnedBusiness
from app.schemas.improvement import ImprovementCompareIn, ImprovementRecordOut
from app.services import business as business_service
from app.services import improvement as improvement_service

router = APIRouter(
    prefix="/businesses/{business_id}/websites/{website_id}/improvements",
    tags=["improvements"],
)


@router.post("", response_model=ImprovementRecordOut)
@router.post("/compare", response_model=ImprovementRecordOut)
def compare_scans(
    website_id: uuid.UUID,
    payload: ImprovementCompareIn,
    business: OwnedBusiness,
    session: DbSession,
) -> ImprovementRecordOut:
    website = business_service.get_owned_website(session, business, website_id)
    record = improvement_service.compare_scans(
        session,
        business,
        website,
        payload.previous_scan_id,
        payload.current_scan_id,
    )
    session.commit()
    session.refresh(record)
    return ImprovementRecordOut.model_validate(record)


@router.get("", response_model=list[ImprovementRecordOut])
def list_improvements(
    website_id: uuid.UUID, business: OwnedBusiness, session: DbSession
) -> list[ImprovementRecordOut]:
    website = business_service.get_owned_website(session, business, website_id)
    return [
        ImprovementRecordOut.model_validate(record)
        for record in improvement_service.list_records(session, business, website)
    ]


@router.get("/{record_id}", response_model=ImprovementRecordOut)
def get_improvement(
    website_id: uuid.UUID,
    record_id: uuid.UUID,
    business: OwnedBusiness,
    session: DbSession,
) -> ImprovementRecordOut:
    website = business_service.get_owned_website(session, business, website_id)
    record = improvement_service.get_record(session, business, website, record_id)
    return ImprovementRecordOut.model_validate(record)
