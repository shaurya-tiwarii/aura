"""SEO scoring persistence: run the engine over a scan and store the report.

The seam between the pure scorer (:mod:`app.engines.seo`) and the database, in the
same spirit as :mod:`app.services.scan` is the seam for the crawler. The engine
knows nothing about SQLAlchemy; this module maps its :class:`SeoScoreResult` onto
``seo_reports`` / ``seo_scores`` / ``seo_issues`` rows and nothing else does.

Like the other request-cycle services it does not commit -- the caller owns the
transaction. Its one caller today is the scan worker
(:func:`app.services.scan.execute_scan`), which commits after scoring so the
report lands atomically with the scan's terminal status.

Scoring a scan is idempotent: an existing report for the scan is deleted first, so
re-running the engine (a ruleset change, or the improvement loop re-scoring an old
scan) replaces the report rather than colliding on the ``unique(scan_id)``
constraint or leaving two reports behind.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.engines.seo import score_pages
from app.engines.seo.engine import SeoScoreResult
from app.models.seo import SeoCategoryScore, SeoIssue, SeoReport
from app.models.website import Website, WebsitePage, WebsiteScan

log = get_logger(__name__)


_TITLE_MAX = 200
_URL_MAX = 2048
_RULE_ID_MAX = 80


def _site_flag(scan: WebsiteScan, group: str) -> bool:
    """Read a boolean like ``site_analysis['robots']['found']`` defensively.

    The scan's own (immutable) site analysis is the source of truth rather than
    the website's cached flags, because the improvement loop re-scores *old* scans
    whose facts differ from the website's current state.
    """
    section = scan.site_analysis.get(group) if scan.site_analysis else None
    if isinstance(section, dict):
        return bool(section.get("found"))
    return False


def get_report(session: Session, scan: WebsiteScan) -> SeoReport | None:
    """The SEO report for a scan, or ``None`` if it has not been scored."""
    return session.scalars(
        select(SeoReport).where(SeoReport.scan_id == scan.id)
    ).first()


def score_and_store(
    session: Session,
    scan: WebsiteScan,
    website: Website,
    *,
    keywords: list[str] | None = None,
) -> SeoReport:
    """Score *scan*'s pages and persist the report, categories, and issues.

    Reads the scan's crawled pages, runs :func:`app.engines.seo.score_pages`, and
    writes the result. Deletes any prior report for the scan first. Flushes so the
    new rows have ids, but does not commit -- the caller does.
    """
    pages = list(
        session.scalars(select(WebsitePage).where(WebsitePage.scan_id == scan.id))
    )

    result = score_pages(
        pages,
        site_url=website.normalized_url,
        robots_found=_site_flag(scan, "robots"),
        sitemap_found=_site_flag(scan, "sitemap"),
        keywords=keywords,
    )

    existing = get_report(session, scan)
    if existing is not None:
        session.delete(existing)
        session.flush()

    report = _persist_result(session, scan, pages, result)

    log.info(
        "seo_report_stored",
        scan_id=str(scan.id),
        overall_score=result.overall_score,
        grade=result.grade,
        issue_count=result.issue_count,
        engine_version=result.engine_version,
    )
    return report


def _persist_result(
    session: Session,
    scan: WebsiteScan,
    pages: list[WebsitePage],
    result: SeoScoreResult,
) -> SeoReport:
    report = SeoReport(
        scan_id=scan.id,
        overall_score=result.overall_score,
        grade=result.grade,
        points_earned=result.points_earned,
        points_available=result.points_available,
        pages_analyzed=result.pages_analyzed,
        issue_count=result.issue_count,
        critical_count=result.critical_count,
        high_count=result.high_count,
        medium_count=result.medium_count,
        low_count=result.low_count,
        engine_version=result.engine_version,
        summary=result.summary,
    )
    session.add(report)
    session.flush()

    for category in result.categories:
        session.add(
            SeoCategoryScore(
                report_id=report.id,
                category=category.category,
                points_earned=category.points_earned,
                points_available=category.points_available,
                normalized_score=category.normalized_score,
                weight=category.weight,
                issue_count=category.issue_count,
                check_results=category.check_results,
            )
        )

    page_id_by_url = {p.normalized_url: p.id for p in pages}
    for issue in result.issues:
        page_id = (
            page_id_by_url.get(issue.anchor_normalized_url)
            if issue.anchor_normalized_url
            else None
        )
        session.add(
            SeoIssue(
                report_id=report.id,
                page_id=page_id,
                rule_id=issue.rule_id[:_RULE_ID_MAX],
                category=issue.category,
                severity=issue.severity,
                title=issue.title[:_TITLE_MAX],
                description=issue.description,
                suggested_fix=issue.suggested_fix,
                points_deducted=issue.points_deducted,
                evidence=issue.evidence,
                page_url=issue.page_url[:_URL_MAX] if issue.page_url else None,
                affected_page_count=issue.affected_page_count,
            )
        )

    session.flush()
    return report


__all__ = ["get_report", "score_and_store"]
