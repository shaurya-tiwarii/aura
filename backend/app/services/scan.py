"""Scan orchestration: queue a scan, run the crawler, persist the results.

This is the seam between the crawler engine (:mod:`app.crawler`) and the database.
The crawler knows nothing about SQLAlchemy and the models know nothing about
httpx; this module is the only place the two meet, so the mapping from a
``CrawlResult`` to rows lives in exactly one auditable place.

Two entry points, deliberately split:

* :func:`create_scan` runs inside a request. It allocates the per-website
  sequence number and writes a ``QUEUED`` row, and like every other service it
  does **not** commit -- the route owns that, so creating and dispatching a scan
  is one transaction.
* :func:`execute_scan` runs the crawl and writes pages. It is a *worker* entry
  point, not a request one: it owns its own commits (progress updates have to be
  visible to a polling client *while* the crawl is still running, so they cannot
  wait for one final commit at the end), exactly like the scheduler jobs that use
  :func:`~app.core.database.session_scope`. It never raises on a site problem --
  a timeout, a 500, or a robots.txt block are all recorded on the scan row as
  findings, matching the crawler's own contract.

The page mapping is a straight field copy, not a translation table. Every
analysis field on :class:`~app.crawler.extract.PageExtraction` is named to match
its :class:`~app.models.website.WebsitePage` column, so the copy is computed from
the *intersection of the two field sets* -- add a column and an extraction field
with the same name and it flows through with no edit here, and a name that exists
on only one side is simply never copied. That is what keeps this from silently
drifting out of sync with either side.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.errors import (
    BlockedURLError,
    ConflictError,
    InvalidURLError,
    NotFoundError,
)
from app.core.logging import get_logger
from app.crawler.crawler import Crawler, CrawlResult
from app.crawler.extract import PageExtraction
from app.crawler.fetcher import FetchResult
from app.models.enums import PageFetchStatus, ScanStatus, ScanTrigger
from app.models.website import Website, WebsitePage, WebsiteScan

log = get_logger(__name__)

# Statuses that mean "a crawl is in flight". A second scan of the same website is
# refused while one of these is present: two concurrent crawls of one site would
# double the load we put on it and race on the sequence number for no benefit.
_ACTIVE_STATUSES: tuple[str, ...] = (
    ScanStatus.QUEUED.value,
    ScanStatus.CRAWLING.value,
    ScanStatus.ANALYZING.value,
    ScanStatus.SCORING.value,
)

# How many scans may run at once across the whole process. Small on purpose: each
# scan is I/O-bound but writes to the database throughout, and on the SQLite
# fallback a large fan-out would just queue on the single writer. Two lets a user
# scan a second site without waiting while keeping write contention negligible.
_MAX_CONCURRENT_SCANS = 2

# Column widths borrowed from app.models.website so an over-long value is trimmed
# here rather than surfacing as a driver truncation mid-crawl.
_URL_MAX = 2048
_CONTENT_TYPE_MAX = 120
_ERROR_CODE_MAX = 60
_CMS_MAX = 80
_PROGRESS_MESSAGE_MAX = 200

# The straight-copy field set: names present on BOTH the ORM row and the
# extraction dataclass. Computed once at import. Excludes the extraction's
# frontier-only fields (discovered_links, detected_cms, noindex, nofollow_page,
# none of which are columns) and the row's fetch/identity columns (scan_id, url,
# depth, fetch_status, ... none of which are extraction fields) without naming
# either -- so neither list has to be maintained by hand.
_PAGE_COLUMN_NAMES = frozenset(WebsitePage.__table__.columns.keys())
_EXTRACTION_FIELD_NAMES = frozenset(f.name for f in dataclasses.fields(PageExtraction))
_COPIED_EXTRACTION_FIELDS: tuple[str, ...] = tuple(
    sorted(_PAGE_COLUMN_NAMES & _EXTRACTION_FIELD_NAMES)
)


def _now() -> datetime:
    return datetime.now(UTC)


# ============================================================================
# Reads (tenant-scoped: a scan is only ever reached through a website the caller
# already owns, so every query is predicated on website_id / scan_id)
# ============================================================================
def list_scans(session: Session, website: Website) -> list[WebsiteScan]:
    """Every scan of a website, newest sequence first."""
    return list(
        session.scalars(
            select(WebsiteScan)
            .where(WebsiteScan.website_id == website.id)
            .order_by(WebsiteScan.sequence.desc())
        )
    )


def get_scan(session: Session, website: Website, scan_id: uuid.UUID) -> WebsiteScan:
    """One scan, scoped to a website the caller owns.

    ``website_id`` is in the WHERE clause for the same reason it is on every other
    scoped lookup: a scan id from another account's site must be indistinguishable
    from one that never existed, so both return the same 404.
    """
    scan = session.scalars(
        select(WebsiteScan).where(
            WebsiteScan.id == scan_id, WebsiteScan.website_id == website.id
        )
    ).first()
    if scan is None:
        raise NotFoundError(
            "That scan could not be found.",
            hint="It may have been deleted, or it belongs to another website.",
        )
    return scan


def latest_scan(session: Session, website: Website) -> WebsiteScan | None:
    """The most recent scan of a website, or ``None`` if it has never been scanned."""
    return session.scalars(
        select(WebsiteScan)
        .where(WebsiteScan.website_id == website.id)
        .order_by(WebsiteScan.sequence.desc())
        .limit(1)
    ).first()


def list_pages(session: Session, scan: WebsiteScan) -> list[WebsitePage]:
    """Pages of a scan: homepage first, then shallowest, then discovery order."""
    return list(
        session.scalars(
            select(WebsitePage)
            .where(WebsitePage.scan_id == scan.id)
            .order_by(
                WebsitePage.is_homepage.desc(),
                WebsitePage.depth,
                WebsitePage.created_at,
            )
        )
    )


def get_page(session: Session, scan: WebsiteScan, page_id: uuid.UUID) -> WebsitePage:
    page = session.scalars(
        select(WebsitePage).where(
            WebsitePage.id == page_id, WebsitePage.scan_id == scan.id
        )
    ).first()
    if page is None:
        raise NotFoundError("That page could not be found.")
    return page


def create_scan(
    session: Session,
    website: Website,
    *,
    trigger: ScanTrigger = ScanTrigger.MANUAL,
) -> WebsiteScan:
    """Queue a scan for a website.

    Allocates the next per-website sequence number and writes a ``QUEUED`` row.
    Does not run anything -- the route commits this, then hands the id to
    :func:`dispatch_scan`. Refuses if a scan of this website is already in flight.
    """
    active = session.scalar(
        select(func.count())
        .select_from(WebsiteScan)
        .where(
            WebsiteScan.website_id == website.id,
            WebsiteScan.status.in_(_ACTIVE_STATUSES),
        )
    )
    if (active or 0) > 0:
        raise ConflictError(
            "A scan is already running for this website.",
            code="scan_already_running",
            hint="Wait for it to finish; poll the scan status for progress.",
        )

    highest = session.scalar(
        select(func.max(WebsiteScan.sequence)).where(
            WebsiteScan.website_id == website.id
        )
    )
    next_sequence = (highest or 0) + 1

    scan = WebsiteScan(
        website_id=website.id,
        status=ScanStatus.QUEUED.value,
        trigger=trigger.value,
        sequence=next_sequence,
        progress_percent=0,
        progress_message="Queued",
    )
    session.add(scan)
    try:
        # Surfaces the unique(website_id, sequence) constraint as a clean conflict
        # here, if two requests raced to the same sequence, rather than at commit.
        session.flush()
    except IntegrityError as exc:
        raise ConflictError(
            "Could not queue the scan. Please try again.",
            code="scan_sequence_conflict",
        ) from exc

    log.info(
        "scan_queued",
        scan_id=str(scan.id),
        website_id=str(website.id),
        sequence=next_sequence,
        trigger=trigger.value,
    )
    return scan


def can_delete(scan: WebsiteScan) -> bool:
    """A scan can be deleted once it is no longer in flight."""
    return scan.status not in _ACTIVE_STATUSES


# ============================================================================
# Execute (worker-cycle: owns its own commits, never raises on a site problem)
# ============================================================================
def execute_scan(
    session: Session,
    scan: WebsiteScan,
    *,
    keywords: list[str] | None = None,
    allow_private_hosts: bool | None = None,
) -> WebsiteScan:
    """Run the crawl for *scan* and persist everything it produced.

    Synchronous by design: it wraps the async crawler in :func:`asyncio.run` so it
    can be called from a plain worker thread (see :func:`dispatch_scan`) and, just
    as importantly, directly from a verification script with no event loop of its
    own. Commits as it goes so a polling client sees live progress, and again at
    the end. The only exceptions it lets escape are none: a crawl that blows up is
    recorded as a ``FAILED`` scan, because a background job that raises leaves the
    row stuck at ``CRAWLING`` forever.

    *allow_private_hosts* is threaded straight to the crawler and must stay
    ``None``/``False`` in production; it is the single switch a local-fixture test
    flips to reach a loopback server, and :class:`~app.core.config.Settings`
    refuses the private+production combination independently.
    """
    website = scan.website

    scan.status = ScanStatus.CRAWLING.value
    scan.started_at = _now()
    scan.progress_percent = 0
    scan.progress_message = "Starting scan"
    scan.error_code = None
    scan.error_message = None
    session.commit()

    def _on_progress(percent: int, message: str) -> None:
        # Monotonic: the crawler reports increasing percentages, and clamping keeps
        # a stray callback from making the bar jump backwards in the UI.
        scan.progress_percent = max(scan.progress_percent, min(int(percent), 100))
        scan.progress_message = message[:_PROGRESS_MESSAGE_MAX]
        session.commit()

    crawler = Crawler(allow_private_hosts=allow_private_hosts, progress=_on_progress)

    try:
        result = asyncio.run(crawler.crawl(website.url, keywords=keywords))
    except (BlockedURLError, InvalidURLError) as exc:
        # The stored address itself is one AURA must not fetch. This should be
        # impossible -- add_website SSRF-checks before storing -- so it means the
        # guard changed under a row that predates it. Record, do not crawl.
        log.warning("scan_seed_blocked", scan_id=str(scan.id), error=exc.message)
        return _mark_failed(
            session, scan, code=exc.code or "blocked_url", message=exc.message
        )
    except Exception:  # a crawl must never leave the row stuck
        log.error("scan_crawl_error", scan_id=str(scan.id), exc_info=True)
        return _mark_failed(
            session,
            scan,
            code="scan_failed",
            message="The scan could not be completed due to an unexpected error.",
        )

    _persist_pages(session, scan, result)
    terminal = _apply_result(session, scan, website, result)
    session.commit()

    # If the crawl produced analysable pages, score them (progress 70 -> 100). The
    # scan is left in the SCORING state by _apply_result until this finishes, so a
    # polling client never sees a finished status before the score exists.
    if terminal is not None:
        _score_scan(session, scan, website, keywords, terminal=terminal)

    log.info(
        "scan_finished",
        scan_id=str(scan.id),
        status=scan.status,
        pages_crawled=scan.pages_crawled,
        pages_failed=scan.pages_failed,
    )
    return scan


def _mark_failed(
    session: Session, scan: WebsiteScan, *, code: str, message: str
) -> WebsiteScan:
    scan.status = ScanStatus.FAILED.value
    scan.error_code = code[:_ERROR_CODE_MAX]
    scan.error_message = message
    scan.progress_percent = 100
    scan.progress_message = "Scan failed"
    scan.completed_at = _now()
    session.commit()
    return scan


def _persist_pages(session: Session, scan: WebsiteScan, result: CrawlResult) -> None:
    """Write one ``WebsitePage`` per crawled URL.

    Deduplicated by normalised URL as a belt-and-braces guard: the frontier already
    dedupes, but the ``unique(scan_id, normalized_url)`` constraint would abort the
    whole persist on a single collision, and dropping a duplicate is the right
    outcome anyway.
    """
    seen: set[str] = set()
    for crawled in result.pages:
        key = crawled.url.normalized
        if key in seen:
            continue
        seen.add(key)

        page = WebsitePage(
            scan_id=scan.id,
            url=crawled.url.url[:_URL_MAX],
            normalized_url=key[:_URL_MAX],
            depth=crawled.depth,
            is_homepage=crawled.is_homepage,
        )
        _apply_fetch(page, crawled.fetch)
        if crawled.extraction is not None:
            _apply_extraction(page, crawled.extraction)
        session.add(page)


def _apply_fetch(page: WebsitePage, fetch: FetchResult) -> None:
    """Copy the transport-level outcome onto the row."""
    page.fetch_status = fetch.status.value
    page.http_status = fetch.http_status
    page.final_url = fetch.final_url.url[:_URL_MAX] if fetch.final_url else None
    page.redirect_count = fetch.redirect_count
    page.response_time_ms = fetch.elapsed_ms
    page.content_bytes = fetch.content_bytes
    page.content_type = (
        fetch.content_type[:_CONTENT_TYPE_MAX] if fetch.content_type else None
    )
    page.fetch_error = fetch.error


def _apply_extraction(page: WebsitePage, extraction: PageExtraction) -> None:
    """Copy every analysis field the row and the extraction share, by name.

    See the module docstring: the field list is the intersection of the two
    schemas, so this cannot copy a field that is not a column, and it cannot miss
    one that both define.
    """
    for name in _COPIED_EXTRACTION_FIELDS:
        setattr(page, name, getattr(extraction, name))


def _apply_result(
    session: Session, scan: WebsiteScan, website: Website, result: CrawlResult
) -> tuple[str, str] | None:
    """Fold the crawl summary onto the scan row and refresh the website's cache.

    Returns the *pending* terminal ``(status, message)`` when the crawl produced
    analysable pages and scoring should follow -- in that case the scan is left in
    the ``SCORING`` state, not a finished one, so a poll during scoring never reads
    a completed status before the score is written. Returns ``None`` when the scan
    is already terminal (blocked by robots, or nothing could be fetched), in which
    case it is marked ``FAILED`` here and there is nothing to score.
    """
    crawled = result.pages_crawled
    failed = result.pages_failed

    scan.pages_crawled = crawled
    scan.pages_failed = failed
    scan.pages_discovered = int(
        result.frontier_report.get("discovered", len(result.pages))
    )
    scan.hit_page_limit = result.hit_page_limit
    scan.duration_ms = result.duration_ms
    scan.crawl_config = result.config
    scan.site_analysis = result.site_summary()
    scan.completed_at = result.completed_at

    # Cached site-level facts the website list shows without re-reading the scan.
    # Null before the first scan means "not checked yet"; these make it concrete.
    website.robots_txt_found = result.robots.found
    website.sitemap_found = result.sitemap.found
    website.sitemap_url = result.sitemap.root_url or None
    website.sitemap_url_count = result.sitemap.url_count
    website.detected_cms = (
        result.detected_cms[:_CMS_MAX] if result.detected_cms else None
    )
    website.last_scan_at = scan.completed_at or _now()

    # --- terminal decision --------------------------------------------------
    if result.blocked_by_robots:
        scan.status = ScanStatus.FAILED.value
        scan.robots_disallowed = True
        scan.error_code = "crawler_blocked"
        scan.error_message = (
            "robots.txt disallows crawling this site for our crawler, so no pages "
            "were analysed."
        )
        scan.progress_percent = 100
        scan.progress_message = "Blocked by robots.txt"
        return None

    if crawled == 0:
        scan.status = ScanStatus.FAILED.value
        code, message = _homepage_failure(result)
        scan.error_code = code
        scan.error_message = message
        scan.progress_percent = 100
        scan.progress_message = "Scan failed"
        return None

    if failed > 0:
        terminal_status = ScanStatus.PARTIAL.value
        terminal_message = f"Analysed {crawled} of {crawled + failed} pages"
    else:
        terminal_status = ScanStatus.COMPLETED.value
        terminal_message = f"Analysed {crawled} page{'s' if crawled != 1 else ''}"

    # Hand off to scoring: SCORING owns 70 -> 100. The crawler drove 0 -> 70.
    scan.status = ScanStatus.SCORING.value
    scan.progress_percent = 70
    scan.progress_message = "Scoring SEO readiness"
    return (terminal_status, terminal_message)


def _score_scan(
    session: Session,
    scan: WebsiteScan,
    website: Website,
    keywords: list[str] | None,
    *,
    terminal: tuple[str, str],
) -> None:
    """Run both scoring engines over the crawled pages and store their reports.

    The SEO engine and the AI-visibility engine are scored independently: each is
    wrapped in its own transaction so a bug in one can neither lose the other's
    report nor lose the crawl. Whatever succeeds is durable; the scan then moves to
    its terminal status (``completed``/``partial``) with each headline score that
    was produced appended to the progress message.

    If an engine fails, the crawl was still valuable: its report is simply absent
    rather than the scan being marked failed -- the missing report is the honest
    signal that scoring did not run for it, and it is never faked (spec section 46).
    """
    from app.services import ai_visibility as ai_visibility_service
    from app.services import recommendation as recommendation_service
    from app.services import seo as seo_service

    terminal_status, terminal_message = terminal

    seo_report = _score_with(
        session,
        scan,
        label="seo",
        score=lambda: seo_service.score_and_store(
            session, scan, website, keywords=keywords
        ),
    )
    ai_report = _score_with(
        session,
        scan,
        label="ai_visibility",
        score=lambda: ai_visibility_service.score_and_store(session, scan, website),
    )

    # Fold both engines' findings into the business's action list -- but only for
    # the primary website, since recommendations are business-owned and the
    # reconcile completes/reopens rows by rule_id: scoring a secondary site would
    # otherwise resolve findings against a different site's pages. Isolated in its
    # own transaction like the engines above, so a bug here cannot lose the reports
    # that already committed. Only runs when at least one report exists, so the
    # reconcile never mistakes a scoring failure for "everything fixed".
    recs = None
    if website.is_primary and (seo_report is not None or ai_report is not None):
        recs = _score_with(
            session,
            scan,
            label="recommendations",
            score=lambda: recommendation_service.generate_for_scan(
                session, scan, website.business
            ),
        )

    scan.status = terminal_status
    scan.progress_percent = 100
    parts = [terminal_message]
    if seo_report is not None:
        parts.append(f"SEO {seo_report.overall_score:.0f}/100 ({seo_report.grade})")
    if ai_report is not None:
        parts.append(f"AI {ai_report.overall_score:.0f}/100 ({ai_report.grade})")
    if recs is not None:
        parts.append(f"{len(recs)} recommendation{'s' if len(recs) != 1 else ''}")
    scan.progress_message = " · ".join(parts)[:_PROGRESS_MESSAGE_MAX]

    # Close the continuous-improvement loop automatically for the primary site.
    # This is deliberately performed only after both reports exist so a partial
    # scoring failure never becomes a misleading before/after comparison.
    improvement = None
    if website.is_primary and seo_report is not None and ai_report is not None:
        from app.services import improvement as improvement_service
        try:
            previous = session.scalars(
                select(WebsiteScan)
                .where(
                    WebsiteScan.website_id == website.id,
                    WebsiteScan.id != scan.id,
                    WebsiteScan.sequence < scan.sequence,
                    WebsiteScan.status.in_([ScanStatus.COMPLETED.value, ScanStatus.PARTIAL.value]),
                )
                .order_by(WebsiteScan.sequence.desc())
                .limit(1)
            ).first()
            if previous is not None:
                improvement = improvement_service.compare_scans(
                    session, website.business, website, previous.id, scan.id
                )
        except Exception:
            # Improvement comparison is additive. A comparison bug must never
            # roll back a valid scan/report/recommendation result.
            log.error(
                "scan_improvement_comparison_failed",
                scan_id=str(scan.id),
                exc_info=True,
            )
            session.rollback()

    # Wire the existing notification/activity infrastructure into the scan
    # lifecycle. All values are derived from stored records; nothing is invented.
    try:
        from app.models.enums import ActivityAction, NotificationLevel, NotificationType
        from app.services import system as system_service

        user = website.business.owner
        system_service.create_notification(
            session,
            user,
            notification_type=NotificationType.SCAN_COMPLETED if terminal_status != ScanStatus.FAILED.value else NotificationType.SCAN_FAILED,
            level=NotificationLevel.SUCCESS if terminal_status == ScanStatus.COMPLETED.value else NotificationLevel.WARNING,
            title="Website analysis completed" if terminal_status != ScanStatus.FAILED.value else "Website analysis failed",
            message=(
                f"Scan #{scan.sequence} finished for {website.normalized_url}. "
                + (f"SEO {seo_report.overall_score:.0f}/100." if seo_report else "No SEO report was produced.")
            ),
            business_id=website.business_id,
            link_path=f"/dashboard?section=Websites%20%26%20scans&scan={scan.id}",
            context={"scan_id": str(scan.id), "sequence": scan.sequence},
        )
        system_service.record_activity(
            session,
            user=user,
            action=ActivityAction.WEBSITE_ANALYZED,
            description=f"Website scan #{scan.sequence} completed for {website.normalized_url}.",
            business_id=website.business_id,
            entity_type="website_scan",
            entity_id=scan.id,
            metadata_payload={
                "scan_id": str(scan.id),
                "seo_score": seo_report.overall_score if seo_report else None,
                "ai_visibility_score": ai_report.overall_score if ai_report else None,
            },
        )
        if improvement is not None and improvement.is_comparable:
            notification_type = (
                NotificationType.SCORE_IMPROVED
                if improvement.seo_delta > 0
                else NotificationType.SCORE_DECLINED
                if improvement.seo_delta < 0
                else None
            )
            if notification_type is not None:
                system_service.create_notification(
                    session,
                    user,
                    notification_type=notification_type,
                    level=NotificationLevel.SUCCESS if improvement.seo_delta > 0 else NotificationLevel.WARNING,
                    title="Search visibility changed",
                    message=f"SEO changed by {improvement.seo_delta:+.1f} points since the previous scan.",
                    business_id=website.business_id,
                    link_path=f"/dashboard?section=Improvements",
                    context={"improvement_id": str(improvement.id), "seo_delta": improvement.seo_delta},
                )
    except Exception:
        log.error(
            "scan_event_recording_failed",
            scan_id=str(scan.id),
            exc_info=True,
        )

    session.commit()


def _score_with(session: Session, scan: WebsiteScan, *, label: str, score):
    """Run one scoring engine, commit its report on success, and return it or None.

    Each engine is isolated in its own transaction: a failure is logged, rolled
    back (so a half-written report never lands, and the *other* engine's committed
    report is untouched), and returns ``None``. Committing here rather than once at
    the end is what makes the two engines independent -- a later failure cannot
    discard an earlier success.
    """
    try:
        report = score()
        session.commit()
        return report
    except Exception:  # a scoring bug must not lose a good crawl
        log.error(
            "scan_scoring_error", scan_id=str(scan.id), engine=label, exc_info=True
        )
        session.rollback()
        return None


def _homepage_failure(result: CrawlResult) -> tuple[str, str]:
    """Map the homepage's fetch outcome to an (error_code, message) pair.

    ``error_code`` deliberately reuses the AuraError codes the live API would have
    returned, so the frontend can show the same friendly message for a failed
    background scan as it would for a synchronous fetch.
    """
    homepage = result.homepage
    status = homepage.fetch.status if homepage else None
    detail = homepage.fetch.error if homepage and homepage.fetch.error else None

    table: dict[PageFetchStatus, tuple[str, str]] = {
        PageFetchStatus.TIMEOUT: (
            "website_timeout",
            "The site did not respond in time.",
        ),
        PageFetchStatus.CONNECTION_ERROR: (
            "website_unreachable",
            "The site could not be reached.",
        ),
        PageFetchStatus.BLOCKED: (
            "crawler_blocked",
            "The site refused our crawler (HTTP 401/403/429).",
        ),
        PageFetchStatus.NOT_FOUND: (
            "website_unreachable",
            "The homepage returned 'not found' (HTTP 404).",
        ),
        PageFetchStatus.SERVER_ERROR: (
            "website_unreachable",
            "The homepage returned a server error (HTTP 5xx).",
        ),
        PageFetchStatus.SKIPPED: (
            "scan_failed",
            "The homepage was not an analysable web page.",
        ),
    }
    code, message = (
        table.get(status, ("scan_failed", "No pages could be analysed."))
        if status is not None
        else ("scan_failed", "No pages could be analysed.")
    )
    return code, f"{message} ({detail})" if detail else message


_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=_MAX_CONCURRENT_SCANS, thread_name_prefix="aura-scan"
            )
        return _executor


def dispatch_scan(scan_id: uuid.UUID, *, keywords: list[str] | None = None) -> None:
    """Run a queued scan in a background thread.

    The route calls this after committing the ``QUEUED`` row and returns 202
    immediately; the client then polls the scan for progress. Each job opens its
    own :class:`~sqlalchemy.orm.Session` -- the request's session is long gone by
    the time the job runs, and sessions are not shareable across threads.
    """
    _get_executor().submit(_run_scan_job, scan_id, keywords)


def _run_scan_job(scan_id: uuid.UUID, keywords: list[str] | None) -> None:
    session = SessionLocal()
    try:
        scan = session.get(WebsiteScan, scan_id)
        if scan is None:
            # Deleted between queueing and running. Nothing to do.
            log.warning("scan_job_missing", scan_id=str(scan_id))
            return
        execute_scan(session, scan, keywords=keywords)
    except Exception:  # the worker thread must not die silently
        log.error("scan_job_crashed", scan_id=str(scan_id), exc_info=True)
    finally:
        session.close()


def shutdown_scan_executor(*, wait: bool = False) -> None:
    """Stop the scan thread pool. Called from the app's lifespan shutdown."""
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=wait, cancel_futures=True)
            _executor = None


__all__ = [
    "can_delete",
    "create_scan",
    "dispatch_scan",
    "execute_scan",
    "get_page",
    "get_scan",
    "latest_scan",
    "list_pages",
    "list_scans",
    "shutdown_scan_executor",
]
