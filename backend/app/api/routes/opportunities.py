from __future__ import annotations

import uuid

from fastapi import APIRouter, Query

from app.api.deps import DbSession, OwnedBusiness
from app.core.errors import ConflictError
from app.schemas.opportunity import OpportunityOut, OpportunityStatusUpdate
from app.services import business as business_service
from app.services import opportunities as service
from app.services import scan as scan_service

router = APIRouter(prefix="/businesses/{business_id}/opportunities", tags=["opportunities"])


@router.get("", response_model=list[OpportunityOut])
def list_opportunities(
    business: OwnedBusiness,
    session: DbSession,
    status: str | None = Query(default=None),
) -> list[OpportunityOut]:
    return [OpportunityOut.model_validate(i) for i in service.list_opportunities(session, business, status=status)]


@router.post("/generate", response_model=list[OpportunityOut])
def generate_opportunities(business: OwnedBusiness, session: DbSession) -> list[OpportunityOut]:
    websites = business_service.list_websites(session, business)
    website = next((w for w in websites if w.is_primary), websites[0] if websites else None)
    scan = scan_service.latest_scan(session, website) if website else None
    if scan is None or not scan.is_finished:
        raise ConflictError(
            "A finished website scan is required before generating content opportunities.",
            code="no_finished_scan",
        )
    service.generate_from_recommendations(session, business, scan)
    session.commit()
    return [OpportunityOut.model_validate(i) for i in service.list_opportunities(session, business)]


@router.patch("/{opportunity_id}", response_model=OpportunityOut)
def update_opportunity(
    opportunity_id: uuid.UUID,
    payload: OpportunityStatusUpdate,
    business: OwnedBusiness,
    session: DbSession,
) -> OpportunityOut:
    item = service.get_opportunity(session, business, opportunity_id)
    if payload.status == "dismissed":
        item = service.dismiss(session, business, opportunity_id)
    elif payload.status == "accepted":
        item = service.accept(session, business, opportunity_id)
    else:
        item.status = "suggested"
        session.flush()
    session.commit()
    session.refresh(item)
    return OpportunityOut.model_validate(item)
