"""Scan endpoints (Module 4): start a crawl, poll it, read its results.

Nested under a website so the tenant boundary is inherited: the ``OwnedBusiness``
dependency resolves ``{business_id}`` with ``owner_id`` in the WHERE clause, and
:func:`app.services.business.get_owned_website` additionally predicates on
``business_id``. A scan is therefore reachable only through a website the caller
owns; a scan id from anyone else's site returns the same 404 as one that never
existed, never a 403 (a 403 on a real id is an existence oracle).

Starting a scan is asynchronous. ``POST`` writes a ``QUEUED`` row, commits, hands
the id to a background worker, and returns ``202 Accepted`` with the scan so the
client has something to poll immediately -- it does not block for the length of a
crawl. Progress then arrives by polling ``GET /{scan_id}``; the worker commits
each progress update as it goes (see :func:`app.services.scan.execute_scan`).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, Response, status

from app.api.deps import DbSession, OwnedBusiness
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.rate_limit import ANALYSIS_LIMIT, limit_route
from app.models.enums import ScanTrigger, Severity
from app.schemas.ai_visibility import (
    AiVisibilityCategoryScoreOut,
    AiVisibilityReportDetailOut,
    AiVisibilityReportOut,
)
from app.schemas.scan import (
    ScanDetailOut,
    ScanOut,
    WebsitePageDetailOut,
    WebsitePageOut,
)
from app.schemas.seo import (
    SeoCategoryScoreOut,
    SeoIssueOut,
    SeoReportDetailOut,
    SeoReportOut,
)
from app.services import ai_visibility as ai_visibility_service
from app.services import business as service_business
from app.services import scan as scan_service
from app.services import seo as seo_service

log = get_logger(__name__)

router = APIRouter(
    prefix="/businesses/{business_id}/websites/{website_id}/scans",
    tags=["scans"],
)


@router.post(
    "",
    response_model=ScanOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a scan of this website",
)
@limit_route(ANALYSIS_LIMIT)
def start_scan(
    request: Request, website_id: uuid.UUID, business: OwnedBusiness, session: DbSession
) -> ScanOut:
    """Queue a crawl and dispatch it to a background worker.

    Returns 202 with the queued scan; poll ``GET /{scan_id}`` for progress. If a
    scan of this website is already in flight, returns 409 rather than starting a
    second concurrent crawl of the same site.

    Keywords are taken from the business profile so the crawler can measure how the
    site's own target terms are used on the page -- they are not a client input.
    """
    website = service_business.get_owned_website(session, business, website_id)

    scan = scan_service.create_scan(session, website, trigger=ScanTrigger.MANUAL)
    session.commit()
    session.refresh(scan)

    profile = service_business.get_profile(session, business)
    keywords = list(profile.keywords) if profile and profile.keywords else None

    # Dispatch only after the QUEUED row is committed: the worker opens its own
    # session and would not see an uncommitted row.
    scan_service.dispatch_scan(scan.id, keywords=keywords)

    log.info("scan_started", scan_id=str(scan.id), website_id=str(website.id))
    return ScanOut.model_validate(scan)


@router.get(
    "",
    response_model=list[ScanOut],
    summary="Scan history for this website, newest first",
)
def list_scans(
    website_id: uuid.UUID, business: OwnedBusiness, session: DbSession
) -> list[ScanOut]:
    website = service_business.get_owned_website(session, business, website_id)
    return [
        ScanOut.model_validate(scan)
        for scan in scan_service.list_scans(session, website)
    ]


@router.get(
    "/latest",
    response_model=ScanDetailOut,
    summary="The most recent scan of this website",
)
def get_latest_scan(
    website_id: uuid.UUID, business: OwnedBusiness, session: DbSession
) -> ScanDetailOut:
    """Declared before ``/{scan_id}`` so the literal path wins over the UUID
    converter -- otherwise ``latest`` would be parsed as a (failing) scan id."""
    website = service_business.get_owned_website(session, business, website_id)
    scan = scan_service.latest_scan(session, website)
    if scan is None:
        raise NotFoundError(
            "This website has not been scanned yet.",
            hint="Start one with POST to this website's scans collection.",
        )
    return ScanDetailOut.model_validate(scan)


@router.get(
    "/{scan_id}",
    response_model=ScanDetailOut,
    summary="One scan, with progress, config, and the site-level rollup",
)
def get_scan(
    website_id: uuid.UUID,
    scan_id: uuid.UUID,
    business: OwnedBusiness,
    session: DbSession,
) -> ScanDetailOut:
    website = service_business.get_owned_website(session, business, website_id)
    scan = scan_service.get_scan(session, website, scan_id)
    return ScanDetailOut.model_validate(scan)


@router.get(
    "/{scan_id}/seo",
    response_model=SeoReportDetailOut,
    summary="The SEO report for a scan: headline score, categories, and issues",
)
def get_scan_seo(
    website_id: uuid.UUID,
    scan_id: uuid.UUID,
    business: OwnedBusiness,
    session: DbSession,
) -> SeoReportDetailOut:
    """The stored SEO report for a scan.

    404 until the scan has been scored -- a still-running scan, or one whose crawl
    failed (nothing analysable) or whose scoring errored, simply has no report. We
    never invent one, so the absence is the honest signal that there is nothing to
    show yet, not a fabricated 0 or 100.

    Issues are returned most-severe first, then by points lost, so the highest-
    impact fixes lead. Every number here traces back to a ``rule_id`` in the issue
    list (spec section 24): summing ``points_deducted`` per category reconstructs
    that category's score, and the weighted category scores reconstruct the
    headline.
    """
    website = service_business.get_owned_website(session, business, website_id)
    scan = scan_service.get_scan(session, website, scan_id)
    report = seo_service.get_report(session, scan)
    if report is None:
        raise NotFoundError(
            "This scan has no SEO report yet.",
            hint=(
                "Scoring runs after the crawl finishes; poll the scan until its "
                "status is 'completed' or 'partial'."
            ),
        )

    issues = sorted(
        report.issues,
        key=lambda i: (-Severity(i.severity).rank, -i.points_deducted),
    )
    return SeoReportDetailOut(
        **SeoReportOut.model_validate(report).model_dump(),
        categories=[
            SeoCategoryScoreOut.model_validate(c) for c in report.category_scores
        ],
        issues=[SeoIssueOut.model_validate(i) for i in issues],
    )


@router.get(
    "/{scan_id}/ai-visibility",
    response_model=AiVisibilityReportDetailOut,
    summary="The AI-visibility report for a scan: readiness score and signals",
)
def get_scan_ai_visibility(
    website_id: uuid.UUID,
    scan_id: uuid.UUID,
    business: OwnedBusiness,
    session: DbSession,
) -> AiVisibilityReportDetailOut:
    """The stored AI-visibility report for a scan.

    The headline is an *estimate of AI-search readiness*, not a claim about how any
    AI system ranks or cites the site -- ``disclaimer`` carries that caveat verbatim
    from the row (spec sections 7 and 45). Every number traces to a signal: each
    category's ``signal_results`` reconstructs its ``normalized_score``, and the
    weighted category scores reconstruct the headline (spec section 24).

    404 until the scan has been scored -- a still-running scan, or one whose crawl
    failed (nothing analysable) or whose scoring errored, simply has no report. We
    never invent one, so the absence is the honest signal that there is nothing to
    show yet, not a fabricated score. Categories are returned by descending weight.
    """
    website = service_business.get_owned_website(session, business, website_id)
    scan = scan_service.get_scan(session, website, scan_id)
    report = ai_visibility_service.get_report(session, scan)
    if report is None:
        raise NotFoundError(
            "This scan has no AI-visibility report yet.",
            hint=(
                "Scoring runs after the crawl finishes; poll the scan until its "
                "status is 'completed' or 'partial'."
            ),
        )

    categories = sorted(report.category_scores, key=lambda c: -c.weight)
    return AiVisibilityReportDetailOut(
        **AiVisibilityReportOut.model_validate(report).model_dump(),
        categories=[AiVisibilityCategoryScoreOut.model_validate(c) for c in categories],
    )


@router.delete(
    "/{scan_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a scan and its pages",
)
def delete_scan(
    website_id: uuid.UUID,
    scan_id: uuid.UUID,
    business: OwnedBusiness,
    session: DbSession,
) -> Response:
    """Refuses while the scan is still running.

    The page cascade is declared on the foreign key, so deleting the scan deletes
    its pages at the database level rather than in a loop here.
    """
    website = service_business.get_owned_website(session, business, website_id)
    scan = scan_service.get_scan(session, website, scan_id)
    if not scan_service.can_delete(scan):
        raise ConflictError(
            "This scan is still running and cannot be deleted yet.",
            code="scan_in_progress",
            hint="Wait for it to finish, then delete it.",
        )
    session.delete(scan)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{scan_id}/pages",
    response_model=list[WebsitePageOut],
    summary="Pages captured by a scan",
)
def list_scan_pages(
    website_id: uuid.UUID,
    scan_id: uuid.UUID,
    business: OwnedBusiness,
    session: DbSession,
) -> list[WebsitePageOut]:
    website = service_business.get_owned_website(session, business, website_id)
    scan = scan_service.get_scan(session, website, scan_id)
    return [
        WebsitePageOut.model_validate(page)
        for page in scan_service.list_pages(session, scan)
    ]


@router.get(
    "/{scan_id}/pages/{page_id}",
    response_model=WebsitePageDetailOut,
    summary="One page with its full extracted evidence",
)
def get_scan_page(
    website_id: uuid.UUID,
    scan_id: uuid.UUID,
    page_id: uuid.UUID,
    business: OwnedBusiness,
    session: DbSession,
) -> WebsitePageDetailOut:
    website = service_business.get_owned_website(session, business, website_id)
    scan = scan_service.get_scan(session, website, scan_id)
    page = scan_service.get_page(session, scan, page_id)
    return WebsitePageDetailOut.model_validate(page)
