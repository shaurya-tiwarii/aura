"""Recommendation persistence and lifecycle (Module 8).

The seam between the pure ranking engine (:mod:`app.engines.recommendation`) and
the database. The engine ranks findings; this module reads the stored SEO and
AI-visibility reports of a scan, normalises them into the engine's inputs, and
reconciles the resulting drafts against the *business's* existing recommendations.

Recommendations are owned by the business and outlive the scan that raised them,
so generation is a reconcile, not an insert (the dedup key is
``(business_id, rule_id)``):

* a finding present this scan with no existing row  -> new ``PENDING`` row,
  ``origin_scan_id`` = this scan;
* a finding present that already has an open row     -> refresh its fields, keep
  its status (a user working on it stays "in progress");
* a finding present whose row was ``COMPLETED``      -> a regression: reopen it
  as ``PENDING`` and clear ``resolved_by_scan_id``;
* a finding present whose row was ``DISMISSED``      -> refresh fields, stay
  dismissed (the user's decision is not overridden);
* a finding *absent* this scan whose row is open     -> ``COMPLETED``, stamped
  with ``resolved_by_scan_id`` = this scan -- **but only if that finding's source
  engine actually produced a report this scan**. If SEO scoring failed, the
  absence of an SEO finding means "not measured", not "fixed", so those rows are
  left untouched. That distinction is what lets the improvement loop (Module 20)
  credit a real fix rather than an engine hiccup.

Like the other request-cycle services it does not commit -- the caller owns the
transaction (the scan worker after scoring, or the regenerate route).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.engines.recommendation import (
    ENGINE_VERSION,
    AiSignalGapInput,
    SeoIssueInput,
    build_recommendations,
)
from app.engines.recommendation.catalog import AI_REMEDIATION
from app.engines.recommendation.engine import RecommendationDraft
from app.models.ai_visibility import AiVisibilityReport
from app.models.business import Business
from app.models.enums import RecommendationCategory, RecommendationStatus
from app.models.recommendation import Recommendation
from app.models.seo import SeoReport
from app.models.website import Website, WebsiteScan
from app.services import ai_visibility as ai_visibility_service
from app.services import seo as seo_service

log = get_logger(__name__)

# Column widths borrowed from app.models.recommendation so an over-long value is
# trimmed here rather than surfacing as a driver truncation at flush time.
_RULE_ID_MAX = 80
_TITLE_MAX = 200
_URL_MAX = 2048
_DISMISS_MAX = 300
_EXAMPLE_URLS_MAX = 10

# A signal counts as a gap only if it left more than this many points on the table,
# so float dust never manufactures a zero-value recommendation.
_EPS = 1e-6

_OPEN_STATUSES: tuple[str, ...] = (
    RecommendationStatus.PENDING.value,
    RecommendationStatus.IN_PROGRESS.value,
)


def _now() -> datetime:
    return datetime.now(UTC)


# =============================================================================
# Normalise stored reports into engine inputs
# =============================================================================
def _seo_inputs(report: SeoReport) -> list[SeoIssueInput]:
    """One :class:`SeoIssueInput` per stored issue, carrying its category's totals.

    The category's ``points_available`` and ``weight`` come from its
    ``SeoCategoryScore`` row, so the engine can compute the estimated score gain
    without re-scoring. An issue whose category is missing or has no available
    points is skipped rather than guessed.
    """
    by_category = {c.category: c for c in report.category_scores}
    inputs: list[SeoIssueInput] = []
    for issue in report.issues:
        cat = by_category.get(issue.category)
        if cat is None or cat.points_available <= 0:
            continue
        example_urls = tuple(u for u in (issue.page_url,) if u)
        inputs.append(
            SeoIssueInput(
                rule_id=issue.rule_id,
                category=issue.category,
                severity=issue.severity,
                title=issue.title,
                description=issue.description,
                suggested_fix=issue.suggested_fix,
                points_deducted=issue.points_deducted,
                category_points_available=cat.points_available,
                category_weight=cat.weight,
                affected_page_count=issue.affected_page_count,
                example_urls=example_urls,
                evidence=issue.evidence or {},
            )
        )
    return inputs


def _ai_inputs(report: AiVisibilityReport) -> list[AiSignalGapInput]:
    """One :class:`AiSignalGapInput` per *applicable, not-fully-earned* signal.

    Reads each category's ``signal_results`` (what the engine recorded per signal)
    and turns every gap -- applicable and ``points < max`` -- into an input. A
    fully-earned or not-applicable signal is not a gap and produces nothing. A
    signal id with no catalog remediation (a report predating a signal change) is
    skipped, never guessed.
    """
    inputs: list[AiSignalGapInput] = []
    for cat in report.category_scores:
        if cat.points_available <= 0:
            continue
        results = cat.signal_results or {}
        for signal_id, sig in results.items():
            if not isinstance(sig, dict) or not sig.get("applicable"):
                continue
            max_points = float(sig.get("max", 0.0) or 0.0)
            points = float(sig.get("points", 0.0) or 0.0)
            if max_points - points <= _EPS or signal_id not in AI_REMEDIATION:
                continue
            evidence = sig.get("evidence")
            evidence = evidence if isinstance(evidence, dict) else {}
            affected = 0
            if "pages_checked" in evidence:
                affected = int(evidence.get("pages_checked", 0)) - int(
                    evidence.get("pages_passing", 0)
                )
            inputs.append(
                AiSignalGapInput(
                    signal_id=signal_id,
                    category=cat.category,
                    points=points,
                    max_points=max_points,
                    category_points_available=cat.points_available,
                    category_weight=cat.weight,
                    detail=str(sig.get("detail", "")),
                    fraction=float(sig.get("fraction", 0.0) or 0.0),
                    affected_page_count=max(affected, 0),
                    evidence=evidence,
                )
            )
    return inputs


def _apply_draft(rec: Recommendation, draft: RecommendationDraft) -> None:
    """Copy a draft's descriptive and priority fields onto a row.

    Deliberately does **not** touch status, ``status_changed_at``,
    ``origin_scan_id``, ``resolved_by_scan_id`` or ``dismissed_reason`` -- those
    are lifecycle state the reconcile owns, not per-scan facts.
    """
    rec.title = draft.title[:_TITLE_MAX]
    rec.description = draft.description
    rec.category = draft.category
    rec.severity = draft.severity
    rec.impact = draft.impact
    rec.effort = draft.effort
    rec.priority_score = draft.priority_score
    rec.priority_bucket = draft.priority_bucket
    rec.suggested_action = draft.suggested_action
    rec.estimated_score_gain = draft.estimated_score_gain
    rec.affected_page_count = draft.affected_page_count
    rec.example_urls = [u[:_URL_MAX] for u in draft.example_urls[:_EXAMPLE_URLS_MAX]]
    rec.evidence = draft.evidence


# =============================================================================
# Generate / reconcile (caller commits)
# =============================================================================
def generate_for_scan(
    session: Session, scan: WebsiteScan, business: Business
) -> list[Recommendation]:
    """Reconcile the business's recommendations against *scan*'s reports.

    Reads whichever of the two reports exist for the scan, ranks their findings,
    and folds them into the business's recommendation set per the lifecycle in the
    module docstring. Flushes so new rows get ids, but does not commit. Returns the
    business's currently-open recommendations (highest priority first).
    """
    seo_report = seo_service.get_report(session, scan)
    ai_report = ai_visibility_service.get_report(session, scan)

    seo_inputs = _seo_inputs(seo_report) if seo_report is not None else []
    ai_inputs = _ai_inputs(ai_report) if ai_report is not None else []
    drafts = build_recommendations(seo_inputs, ai_inputs)
    draft_ids = {d.rule_id for d in drafts}

    existing = {
        rec.rule_id: rec
        for rec in session.scalars(
            select(Recommendation).where(Recommendation.business_id == business.id)
        )
    }
    now = _now()

    # --- findings present this scan ----------------------------------------
    for draft in drafts:
        rec = existing.get(draft.rule_id)
        if rec is None:
            rec = Recommendation(
                business_id=business.id,
                rule_id=draft.rule_id[:_RULE_ID_MAX],
                origin_scan_id=scan.id,
                status=RecommendationStatus.PENDING.value,
                status_changed_at=now,
            )
            session.add(rec)
        elif rec.status == RecommendationStatus.COMPLETED.value:
            # Fixed before, but the finding is back: reopen it honestly.
            rec.status = RecommendationStatus.PENDING.value
            rec.status_changed_at = now
            rec.resolved_by_scan_id = None
            if rec.origin_scan_id is None:
                rec.origin_scan_id = scan.id
        _apply_draft(rec, draft)

    # --- findings absent this scan -----------------------------------------
    for rule_id, rec in existing.items():
        if rule_id in draft_ids:
            continue
        is_ai = rec.category == RecommendationCategory.AI_VISIBILITY.value
        source_ran = (ai_report is not None) if is_ai else (seo_report is not None)
        if not source_ran:
            # The engine that would raise this finding produced no report this
            # scan, so its absence is "not measured", not "fixed". Leave it be.
            continue
        if rec.status in _OPEN_STATUSES:
            rec.status = RecommendationStatus.COMPLETED.value
            rec.status_changed_at = now
            rec.resolved_by_scan_id = scan.id
        elif (
            rec.status == RecommendationStatus.COMPLETED.value
            and rec.resolved_by_scan_id is None
        ):
            rec.resolved_by_scan_id = scan.id
        # DISMISSED rows are left exactly as the user set them.

    session.flush()

    open_recs = list_recommendations(session, business, open_only=True)
    log.info(
        "recommendations_generated",
        scan_id=str(scan.id),
        business_id=str(business.id),
        present=len(drafts),
        open=len(open_recs),
        engine_version=ENGINE_VERSION,
    )
    return open_recs


# =============================================================================
# Reads (tenant-scoped)
# =============================================================================
def list_recommendations(
    session: Session,
    business: Business,
    *,
    status: str | None = None,
    category: str | None = None,
    bucket: str | None = None,
    open_only: bool = False,
) -> list[Recommendation]:
    """A business's recommendations, most actionable first.

    Ordered by ``priority_score`` (impact / effort) then estimated gain then id,
    which is total and deterministic. All filters are optional and combine.
    """
    stmt = select(Recommendation).where(Recommendation.business_id == business.id)
    if open_only:
        stmt = stmt.where(Recommendation.status.in_(_OPEN_STATUSES))
    if status is not None:
        stmt = stmt.where(Recommendation.status == status)
    if category is not None:
        stmt = stmt.where(Recommendation.category == category)
    if bucket is not None:
        stmt = stmt.where(Recommendation.priority_bucket == bucket)
    stmt = stmt.order_by(
        Recommendation.priority_score.desc(),
        Recommendation.estimated_score_gain.desc(),
        Recommendation.rule_id.asc(),
    )
    return list(session.scalars(stmt))


def summarize(session: Session, business: Business) -> dict[str, object]:
    """Counts by status / bucket / category, plus the open recoverable points.

    Computed over *all* of a business's recommendations so completed and dismissed
    work stays counted, independent of any list filter. ``estimated_total_gain``
    sums the per-recommendation estimates over the open ones only -- a rough sense
    of the headline points still on the table, and a sum of estimates, never a
    promise (spec sections 7 and 45).
    """
    recs = list_recommendations(session, business)
    by_status: dict[str, int] = {}
    by_bucket: dict[str, int] = {}
    by_category: dict[str, int] = {}
    open_count = 0
    gain = 0.0
    for rec in recs:
        by_status[rec.status] = by_status.get(rec.status, 0) + 1
        by_bucket[rec.priority_bucket] = by_bucket.get(rec.priority_bucket, 0) + 1
        by_category[rec.category] = by_category.get(rec.category, 0) + 1
        if rec.is_open:
            open_count += 1
            gain += rec.estimated_score_gain or 0.0
    return {
        "total": len(recs),
        "open": open_count,
        "by_status": by_status,
        "by_bucket": by_bucket,
        "by_category": by_category,
        "estimated_total_gain": round(gain, 1),
    }


def get_recommendation(
    session: Session, business: Business, rec_id: uuid.UUID
) -> Recommendation:
    """One recommendation, scoped to the owning business.

    ``business_id`` is in the WHERE clause for the same reason it is everywhere
    else: an id from another account must be indistinguishable from one that never
    existed, so both raise the same 404, never a 403 (spec section 20).
    """
    rec = session.scalars(
        select(Recommendation).where(
            Recommendation.id == rec_id,
            Recommendation.business_id == business.id,
        )
    ).first()
    if rec is None:
        raise NotFoundError(
            "That recommendation could not be found.",
            hint="It may have been resolved, or it belongs to another business.",
        )
    return rec


# =============================================================================
# Status change (caller commits)
# =============================================================================
def update_status(
    session: Session,
    rec: Recommendation,
    *,
    status: RecommendationStatus,
    dismissed_reason: str | None = None,
) -> Recommendation:
    """Apply a user-driven status change and stamp ``status_changed_at``.

    A manual ``COMPLETED`` is a self-report, so it deliberately does **not** set
    ``resolved_by_scan_id`` -- that field records a fix *observed by a re-scan*,
    which is what the improvement loop credits. ``dismissed_reason`` is kept only
    while dismissed and cleared otherwise.
    """
    rec.status = status.value
    rec.status_changed_at = _now()
    if status == RecommendationStatus.DISMISSED:
        reason = (dismissed_reason or "").strip()
        rec.dismissed_reason = reason[:_DISMISS_MAX] or None
    else:
        rec.dismissed_reason = None
    return rec


# =============================================================================
# Helpers for the regenerate route
# =============================================================================
def _primary_website(session: Session, business: Business) -> Website | None:
    return session.scalars(
        select(Website).where(
            Website.business_id == business.id,
            Website.is_primary.is_(True),
        )
    ).first()


def latest_scored_scan(session: Session, business: Business) -> WebsiteScan | None:
    """The primary website's most recent scan that actually produced a report.

    Walks newest-first and returns the first scan with an SEO or AI-visibility
    report, so regenerating never reconciles against a failed crawl that has no
    findings (which would spuriously resolve everything).
    """
    website = _primary_website(session, business)
    if website is None:
        return None
    scans = session.scalars(
        select(WebsiteScan)
        .where(WebsiteScan.website_id == website.id)
        .order_by(WebsiteScan.sequence.desc())
    )
    for scan in scans:
        has_report = (
            seo_service.get_report(session, scan) is not None
            or ai_visibility_service.get_report(session, scan) is not None
        )
        if has_report:
            return scan
    return None


__all__ = [
    "generate_for_scan",
    "get_recommendation",
    "latest_scored_scan",
    "list_recommendations",
    "update_status",
]
