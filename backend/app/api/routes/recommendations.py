"""Recommendation endpoints (Module 8): the prioritised action list for a business.

Recommendations are owned by the *business*, not a single scan -- they outlive the
scan that raised them and carry a lifecycle (pending -> in progress -> completed,
or dismissed) -- so they hang off ``/businesses/{business_id}`` rather than under a
website's scans. The ``OwnedBusiness`` dependency puts ``owner_id`` in the WHERE
clause, so the whole collection is tenant-scoped; a recommendation id from another
account returns the same 404 as one that never existed, never a 403 (a 403 on a
real id is an existence oracle -- the same choice the rest of the API makes).

There is no ``POST`` to create one by hand: a recommendation is *derived* by
reconciling a scan's SEO and AI-visibility findings, never authored. The scan
worker generates them automatically after scoring; ``POST /regenerate`` re-runs
that reconcile against the primary website's latest scored scan on demand. The
only user-authored change is a status transition (``PATCH``).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query

from app.api.deps import DbSession, OwnedBusiness
from app.core.errors import ConflictError
from app.core.logging import get_logger
from app.models.enums import (
    PriorityBucket,
    RecommendationCategory,
    RecommendationStatus,
)
from app.schemas.recommendation import (
    RecommendationListOut,
    RecommendationOut,
    RecommendationStatusUpdate,
    RecommendationSummary,
)
from app.services import recommendation as recommendation_service

log = get_logger(__name__)

router = APIRouter(
    prefix="/businesses/{business_id}/recommendations",
    tags=["recommendations"],
)


def _list_response(
    session: DbSession,
    business: OwnedBusiness,
    *,
    status: str | None = None,
    category: str | None = None,
    bucket: str | None = None,
) -> RecommendationListOut:
    """Build the list payload: filtered items, plus a summary over *all* rows.

    The items honour the filters; the summary is deliberately computed over the
    whole set so the header counts (and the recoverable-points estimate) don't
    shift as the user narrows the list.
    """
    items = recommendation_service.list_recommendations(
        session, business, status=status, category=category, bucket=bucket
    )
    summary = recommendation_service.summarize(session, business)
    return RecommendationListOut(
        items=[RecommendationOut.model_validate(rec) for rec in items],
        summary=RecommendationSummary.model_validate(summary),
    )


@router.get(
    "",
    response_model=RecommendationListOut,
    summary="The business's recommendations, most actionable first",
)
def list_recommendations(
    business: OwnedBusiness,
    session: DbSession,
    status: RecommendationStatus | None = Query(  # noqa: B008 - FastAPI dependency marker
        default=None, description="Only recommendations in this status."
    ),
    category: RecommendationCategory | None = Query(  # noqa: B008 - FastAPI dependency marker
        default=None, description="Only recommendations in this category."
    ),
    bucket: PriorityBucket | None = Query(  # noqa: B008 - FastAPI dependency marker
        default=None, description="Only recommendations in this impact x effort bucket."
    ),
) -> RecommendationListOut:
    """The prioritised action list, ordered by priority then estimated gain then id.

    Filters combine and are all optional. Invalid filter values are rejected with a
    422 (the allowed values are the enum members shown in the schema), never
    silently ignored. The ``summary`` counts every recommendation the business has,
    including completed and dismissed ones, so progress stays visible.
    """
    return _list_response(
        session,
        business,
        status=status.value if status is not None else None,
        category=category.value if category is not None else None,
        bucket=bucket.value if bucket is not None else None,
    )


@router.post(
    "/regenerate",
    response_model=RecommendationListOut,
    summary="Re-derive recommendations from the primary website's latest scored scan",
)
def regenerate_recommendations(
    business: OwnedBusiness, session: DbSession
) -> RecommendationListOut:
    """Reconcile the action list against the most recent scan that produced a report.

    Declared before ``/{rec_id}`` so the literal path is not parsed as a scan id.
    Returns 409 if the primary website has no scored scan yet -- there is nothing to
    derive from, and we do not invent an empty result that would read as "all
    clear". Otherwise it re-runs the same reconcile the scan worker runs: new
    findings appear, fixed ones are marked completed, and a user's dismissals and
    in-progress states are preserved.
    """
    scan = recommendation_service.latest_scored_scan(session, business)
    if scan is None:
        raise ConflictError(
            "There is no scored scan to generate recommendations from yet.",
            code="no_scored_scan",
            hint=(
                "Add a primary website and run a scan; recommendations are derived "
                "once its SEO or AI-visibility report exists."
            ),
        )
    recommendation_service.generate_for_scan(session, scan, business)
    session.commit()
    log.info(
        "recommendations_regenerated",
        business_id=str(business.id),
        scan_id=str(scan.id),
    )
    return _list_response(session, business)


@router.get(
    "/{rec_id}",
    response_model=RecommendationOut,
    summary="One recommendation with its full evidence",
)
def get_recommendation(
    rec_id: uuid.UUID, business: OwnedBusiness, session: DbSession
) -> RecommendationOut:
    rec = recommendation_service.get_recommendation(session, business, rec_id)
    return RecommendationOut.model_validate(rec)


@router.patch(
    "/{rec_id}",
    response_model=RecommendationOut,
    summary="Change a recommendation's status",
)
def update_recommendation(
    rec_id: uuid.UUID,
    payload: RecommendationStatusUpdate,
    business: OwnedBusiness,
    session: DbSession,
) -> RecommendationOut:
    """Apply a user-driven status change (start, complete, dismiss, or reopen).

    Marking a recommendation completed here is a self-report and deliberately does
    *not* stamp ``resolved_by_scan_id``: that field records a fix a later scan
    actually observed, which is what the improvement loop credits. A
    ``dismissed_reason`` is accepted only alongside ``status="dismissed"`` (enforced
    by the request schema) and is kept only while the row stays dismissed.
    """
    rec = recommendation_service.get_recommendation(session, business, rec_id)
    recommendation_service.update_status(
        session,
        rec,
        status=payload.status,
        dismissed_reason=payload.dismissed_reason,
    )
    session.commit()
    session.refresh(rec)
    log.info(
        "recommendation_status_changed",
        business_id=str(business.id),
        recommendation_id=str(rec.id),
        status=rec.status,
    )
    return RecommendationOut.model_validate(rec)
