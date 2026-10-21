"""Deterministic content-opportunity lifecycle derived from real findings."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.business import Business
from app.models.enums import (
    ContentOpportunityStatus,
    ContentType,
)
from app.models.recommendation import ContentOpportunity, Recommendation
from app.models.website import WebsiteScan


def list_opportunities(
    session: Session, business: Business, *, status: str | None = None
) -> list[ContentOpportunity]:
    stmt = select(ContentOpportunity).where(ContentOpportunity.business_id == business.id)
    if status:
        stmt = stmt.where(ContentOpportunity.status == status)
    return list(
        session.scalars(
            stmt.order_by(
                ContentOpportunity.priority_score.desc(),
                ContentOpportunity.created_at.desc(),
            )
        )
    )


def generate_from_recommendations(
    session: Session, business: Business, scan: WebsiteScan
) -> list[ContentOpportunity]:
    """Turn content-shaped findings into deduplicated, user-actionable ideas."""
    existing = {
        item.title.casefold(): item
        for item in session.scalars(
            select(ContentOpportunity).where(ContentOpportunity.business_id == business.id)
        )
    }
    recs = list(
        session.scalars(
            select(Recommendation)
            .where(Recommendation.business_id == business.id)
            .order_by(Recommendation.priority_score.desc())
        )
    )
    created: list[ContentOpportunity] = []
    for rec in recs:
        title = rec.title.strip()
        if not title or rec.status == "dismissed":
            continue
        if not (
            rec.category in {"content", "ai_visibility"}
            or rec.rule_id.startswith("content.")
            or rec.rule_id.startswith("answerability.")
        ):
            continue
        if title.casefold() in existing:
            continue
        keywords = []
        evidence = rec.evidence or {}
        if isinstance(evidence.get("keywords"), list):
            keywords = [str(k) for k in evidence["keywords"]][:10]
        opportunity = ContentOpportunity(
            business_id=business.id,
            title=title,
            suggested_content_type=(
                ContentType.FAQ.value
                if "faq" in rec.rule_id.casefold()
                else ContentType.BLOG_ARTICLE.value
            ),
            description=rec.description,
            rationale=f"Derived from scan #{scan.sequence}: {rec.description}",
            source="recommendation",
            target_keywords=keywords,
            suggested_outline=[
                "Direct answer or introduction",
                "Key supporting points",
                "Business-specific details",
                "Clear next step",
            ],
            impact=rec.impact,
            effort=rec.effort,
            priority_score=rec.priority_score,
            status=ContentOpportunityStatus.SUGGESTED.value,
        )
        session.add(opportunity)
        created.append(opportunity)
        existing[title.casefold()] = opportunity
    session.flush()
    return created


def get_opportunity(
    session: Session, business: Business, opportunity_id: uuid.UUID
) -> ContentOpportunity:
    item = session.scalar(
        select(ContentOpportunity).where(
            ContentOpportunity.id == opportunity_id,
            ContentOpportunity.business_id == business.id,
        )
    )
    if item is None:
        raise NotFoundError("That content opportunity could not be found.")
    return item


def dismiss(session: Session, business: Business, opportunity_id: uuid.UUID) -> ContentOpportunity:
    item = get_opportunity(session, business, opportunity_id)
    item.status = ContentOpportunityStatus.DISMISSED.value
    session.flush()
    return item


def accept(session: Session, business: Business, opportunity_id: uuid.UUID) -> ContentOpportunity:
    item = get_opportunity(session, business, opportunity_id)
    item.status = ContentOpportunityStatus.ACCEPTED.value
    session.flush()
    return item
