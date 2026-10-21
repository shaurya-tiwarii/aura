"""AI-visibility persistence: run the engine over a scan and store the report.

The seam between the pure scorer (:mod:`app.engines.ai_visibility`) and the
database, exactly parallel to :mod:`app.services.seo`. The engine knows nothing
about SQLAlchemy; this module maps its :class:`AiVisibilityResult` onto
``ai_visibility_reports`` / ``ai_visibility_scores`` rows and nothing else does.
There is no issues table -- AI-visibility records signals, not deductions.

Like the other request-cycle services it does not commit -- the caller owns the
transaction. Its one caller today is the scan worker
(:func:`app.services.scan.execute_scan`), which commits after scoring so the
report lands atomically with the scan's terminal status.

Scoring is idempotent: any existing report for the scan is deleted first, so
re-running the engine (a signal-set change, or the improvement loop re-scoring an
old scan) replaces the report rather than colliding on the ``unique(scan_id)``
constraint or leaving two behind.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.engines.ai_visibility import score_pages
from app.engines.ai_visibility.engine import AiVisibilityResult
from app.models.ai_visibility import AiVisibilityCategoryScore, AiVisibilityReport
from app.models.website import Website, WebsitePage, WebsiteScan

log = get_logger(__name__)


def get_report(session: Session, scan: WebsiteScan) -> AiVisibilityReport | None:
    """The AI-visibility report for a scan, or ``None`` if it has not been scored."""
    return session.scalars(
        select(AiVisibilityReport).where(AiVisibilityReport.scan_id == scan.id)
    ).first()


def score_and_store(
    session: Session,
    scan: WebsiteScan,
    website: Website,
    *,
    as_of: datetime | None = None,
) -> AiVisibilityReport:
    """Score *scan*'s pages and persist the report and its category scores.

    Reads the scan's crawled pages, runs
    :func:`app.engines.ai_visibility.score_pages`, and writes the result. Deletes
    any prior report for the scan first. Flushes so the new rows have ids, but does
    not commit -- the caller does.

    ``as_of`` (the freshness reference instant) defaults to the scan's completion
    time, falling back to its creation time, so a re-score of an old scan yields
    the same freshness verdict it did originally rather than drifting with the
    clock.
    """
    pages = list(
        session.scalars(select(WebsitePage).where(WebsitePage.scan_id == scan.id))
    )

    result = score_pages(
        pages,
        site_url=website.normalized_url,
        as_of=as_of or scan.completed_at or scan.created_at,
    )

    # Idempotent replace: drop the previous report (cascade removes its category
    # scores) before writing the new one.
    existing = get_report(session, scan)
    if existing is not None:
        session.delete(existing)
        session.flush()

    report = _persist_result(session, scan, result)

    log.info(
        "ai_visibility_report_stored",
        scan_id=str(scan.id),
        overall_score=result.overall_score,
        grade=result.grade,
        pages_analyzed=result.pages_analyzed,
        engine_version=result.engine_version,
    )
    return report


def _persist_result(
    session: Session,
    scan: WebsiteScan,
    result: AiVisibilityResult,
) -> AiVisibilityReport:
    report = AiVisibilityReport(
        scan_id=scan.id,
        overall_score=result.overall_score,
        grade=result.grade,
        points_earned=result.points_earned,
        points_available=result.points_available,
        pages_analyzed=result.pages_analyzed,
        engine_version=result.engine_version,
        summary=result.summary,
        # disclaimer is left to the column default (HEURISTIC_DISCLAIMER) so the
        # honesty caveat is attached by the data model, not by this call site.
        has_faq_content=result.has_faq_content,
        has_structured_data=result.has_structured_data,
        has_clear_entity_info=result.has_clear_entity_info,
        has_freshness_signals=result.has_freshness_signals,
    )
    session.add(report)
    session.flush()  # assign report.id before the children reference it

    for category in result.categories:
        session.add(
            AiVisibilityCategoryScore(
                report_id=report.id,
                category=category.category,
                points_earned=category.points_earned,
                points_available=category.points_available,
                normalized_score=category.normalized_score,
                weight=category.weight,
                explanation=category.explanation,
                signal_results=category.signal_results,
            )
        )

    session.flush()
    return report


__all__ = ["get_report", "score_and_store"]
