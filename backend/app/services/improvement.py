"""Deterministic continuous-improvement comparisons (Module 20)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.models.ai_visibility import AiVisibilityReport
from app.models.enums import RecommendationStatus
from app.models.improvement import ImprovementRecord
from app.models.recommendation import Recommendation
from app.models.seo import SeoReport
from app.models.website import Website, WebsiteScan


def _report(session: Session, model, scan: WebsiteScan):
    return session.scalars(select(model).where(model.scan_id == scan.id)).first()


def _category_deltas(previous, current) -> dict[str, dict[str, float]]:
    old = {row.category: row.normalized_score for row in previous.category_scores}
    new = {row.category: row.normalized_score for row in current.category_scores}
    return {
        category: {
            "previous": old.get(category, 100.0),
            "current": new.get(category, 100.0),
            "delta": round(new.get(category, 100.0) - old.get(category, 100.0), 1),
        }
        for category in sorted(set(old) | set(new))
    }


def compare_scans(
    session: Session,
    business,
    website: Website,
    previous_scan_id: uuid.UUID,
    current_scan_id: uuid.UUID,
) -> ImprovementRecord:
    """Compare two scored scans, crediting only rule-specific fixes.

    A missing report is an unmeasured value, never a zero. Comparisons therefore
    fail closed until both engines produced reports for both scans.
    """
    if previous_scan_id == current_scan_id:
        raise ConflictError("A scan cannot be compared with itself.", code="same_scan")
    scans = {
        scan.id: scan
        for scan in session.scalars(
            select(WebsiteScan).where(
                WebsiteScan.website_id == website.id,
                WebsiteScan.id.in_([previous_scan_id, current_scan_id]),
            )
        )
    }
    previous = scans.get(previous_scan_id)
    current = scans.get(current_scan_id)
    if previous is None or current is None:
        raise NotFoundError("Those scans could not be found.")
    if previous.sequence >= current.sequence:
        raise ConflictError(
            "The previous scan must be older than the current scan.",
            code="scan_order_invalid",
        )
    existing = session.scalars(
        select(ImprovementRecord).where(
            ImprovementRecord.previous_scan_id == previous.id,
            ImprovementRecord.current_scan_id == current.id,
        )
    ).first()
    if existing is not None:
        raise ConflictError(
            "Those scans have already been compared.",
            code="comparison_exists",
        )

    old_seo = _report(session, SeoReport, previous)
    new_seo = _report(session, SeoReport, current)
    old_ai = _report(session, AiVisibilityReport, previous)
    new_ai = _report(session, AiVisibilityReport, current)
    if not all((old_seo, new_seo, old_ai, new_ai)):
        raise ConflictError(
            "Both scans need SEO and AI-visibility reports before they can be compared.",
            code="scans_not_comparable",
            hint="Wait for scoring to finish, then compare the scans again.",
        )

    comparable = (
        old_seo.engine_version == new_seo.engine_version
        and old_ai.engine_version == new_ai.engine_version
    )
    old_rules = {issue.rule_id: issue for issue in old_seo.issues}
    new_rules = {issue.rule_id: issue for issue in new_seo.issues}
    old_signals = {
        signal_id
        for category in old_ai.category_scores
        for signal_id, value in (category.signal_results or {}).items()
        if isinstance(value, dict)
        and value.get("applicable")
        and value.get("points", 0) < value.get("max", 0)
    }
    new_signals = {
        signal_id
        for category in new_ai.category_scores
        for signal_id, value in (category.signal_results or {}).items()
        if isinstance(value, dict)
        and value.get("applicable")
        and value.get("points", 0) < value.get("max", 0)
    }
    old_findings = set(old_rules) | old_signals
    new_findings = set(new_rules) | new_signals
    resolved = old_findings - new_findings
    introduced = new_findings - old_findings

    recs = list(
        session.scalars(
            select(Recommendation).where(Recommendation.business_id == business.id)
        )
    )
    applied = []
    now = datetime.now(UTC)
    for rec in recs:
        origin = (
            session.get(WebsiteScan, rec.origin_scan_id)
            if rec.origin_scan_id is not None
            else None
        )
        if (
            not comparable
            or rec.rule_id not in resolved
            or origin is None
            or origin.website_id != website.id
            or origin.sequence > previous.sequence
        ):
            continue
        if rec.resolved_by_scan_id != current.id:
            rec.resolved_by_scan_id = current.id
            if rec.status in {
                RecommendationStatus.PENDING.value,
                RecommendationStatus.IN_PROGRESS.value,
            }:
                rec.status = RecommendationStatus.COMPLETED.value
                rec.status_changed_at = now
        source = old_rules.get(rec.rule_id)
        points = source.points_deducted if source else 0.0
        applied.append(
            {"recommendation_id": str(rec.id), "rule_id": rec.rule_id, "points": points}
        )

    new_issues = [
        {"rule_id": key, "category": new_rules[key].category}
        if key in new_rules
        else {"rule_id": key, "source": "ai_visibility"}
        for key in sorted(introduced)
    ]
    record = ImprovementRecord(
        business_id=business.id,
        website_id=website.id,
        previous_scan_id=previous.id,
        current_scan_id=current.id,
        previous_seo_score=old_seo.overall_score,
        current_seo_score=new_seo.overall_score,
        seo_delta=round(new_seo.overall_score - old_seo.overall_score, 1),
        previous_ai_visibility_score=old_ai.overall_score,
        current_ai_visibility_score=new_ai.overall_score,
        ai_visibility_delta=round(new_ai.overall_score - old_ai.overall_score, 1),
        recommendations_applied=len(applied),
        applied_details=applied,
        new_issues=new_issues,
        issues_resolved=len(resolved),
        issues_introduced=len(introduced),
        category_deltas={
            "seo": _category_deltas(old_seo, new_seo),
            "ai_visibility": _category_deltas(old_ai, new_ai),
        },
        is_comparable=comparable,
        incomparable_reason=(
            "Scoring engine versions differ."
            if old_seo.engine_version != new_seo.engine_version
            or old_ai.engine_version != new_ai.engine_version
            else None
        ),
        summary=(
            "Scores are comparable; this is a deterministic comparison of observed "
            "page findings, not a guarantee of search ranking."
        ),
    )
    session.add(record)
    session.flush()
    return record


def list_records(
    session: Session, business, website: Website
) -> list[ImprovementRecord]:
    return list(
        session.scalars(
            select(ImprovementRecord)
            .where(
                ImprovementRecord.business_id == business.id,
                ImprovementRecord.website_id == website.id,
            )
            .order_by(ImprovementRecord.created_at.desc())
        )
    )


def get_record(session: Session, business, website: Website, record_id: uuid.UUID):
    record = session.scalars(
        select(ImprovementRecord).where(
            ImprovementRecord.id == record_id,
            ImprovementRecord.business_id == business.id,
            ImprovementRecord.website_id == website.id,
        )
    ).first()
    if record is None:
        raise NotFoundError("That improvement record could not be found.")
    return record


__all__ = ["compare_scans", "get_record", "list_records"]
