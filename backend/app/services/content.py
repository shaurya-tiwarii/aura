"""Tenant-scoped content persistence, generation and deterministic optimization."""

from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import NotFoundError
from app.models.business import Business
from app.models.content import Content, ContentScore, ContentVersion
from app.models.enums import ContentSource, ContentStatus, ScoreGrade
from app.schemas.content import ContentCreate, ContentUpdate
from app.utils.text import (
    flesch_kincaid_grade,
    flesch_reading_ease,
    keyword_occurrences,
    word_count,
    words,
)


def _word_count(text: str) -> int:
    return word_count(text)


def list_content(
    session: Session,
    business: Business,
    *,
    status: str | None = None,
    content_type: str | None = None,
) -> tuple[list[Content], int]:
    conditions = [Content.business_id == business.id]
    if status:
        conditions.append(Content.status == status)
    if content_type:
        conditions.append(Content.content_type == content_type)
    stmt = select(Content).where(*conditions).order_by(Content.updated_at.desc())
    items = list(session.scalars(stmt))
    total = session.scalar(select(func.count()).select_from(Content).where(*conditions)) or 0
    return items, total


def get_content(session: Session, business: Business, content_id: uuid.UUID) -> Content:
    item = session.scalar(
        select(Content)
        .options(selectinload(Content.versions), selectinload(Content.scores))
        .where(Content.id == content_id, Content.business_id == business.id)
    )
    if item is None:
        raise NotFoundError("That content draft could not be found.")
    return item


def _add_version(item: Content, reason: str, summary: str | None = None) -> None:
    item.versions.append(
        ContentVersion(
            version_number=item.current_version,
            title=item.title,
            body=item.body,
            meta_title=item.meta_title,
            meta_description=item.meta_description,
            word_count=item.word_count,
            change_reason=reason,
            change_summary=summary,
            score_at_version=item.latest_score,
        )
    )


def create_content(session: Session, business: Business, payload: ContentCreate) -> Content:
    data = payload.model_dump()
    data["content_type"] = payload.content_type.value
    item = Content(
        business_id=business.id,
        **data,
        source=ContentSource.USER_WRITTEN.value,
        word_count=_word_count(payload.body),
        generation_params={},
    )
    session.add(item)
    session.flush()
    _add_version(item, "user_edit", "Initial draft")
    optimize_content(session, business, item, persist=True)
    return item


def update_content(session: Session, business: Business, item: Content, payload: ContentUpdate) -> Content:
    data = payload.model_dump(exclude_unset=True)
    if not data:
        return item
    changed = any(
        getattr(item, field) != (value.value if hasattr(value, "value") else value)
        for field, value in data.items()
    )
    if not changed:
        return item
    _add_version(item, "user_edit", "Updated by user")
    for field, value in data.items():
        setattr(item, field, value.value if hasattr(value, "value") else value)
    if "body" in data or "title" in data or "meta_title" in data or "meta_description" in data:
        item.word_count = _word_count(item.body)
        item.edited_by_user = True
        if item.source == ContentSource.AI_GENERATED.value:
            item.source = ContentSource.AI_ASSISTED.value
    item.current_version += 1
    session.flush()
    optimize_content(session, business, item, persist=True)
    return item


def create_generated_content(
    session: Session,
    business: Business,
    payload: ContentCreate,
    *,
    title: str,
    body: str,
    excerpt: str | None,
    meta_title: str | None,
    meta_description: str | None,
    keywords: list[str],
    demo: bool,
    model: str | None,
    generation_params: dict | None = None,
) -> Content:
    item = Content(
        business_id=business.id,
        content_type=payload.content_type.value,
        status=ContentStatus.READY.value,
        source=ContentSource.AI_GENERATED.value,
        title=title,
        body=body,
        excerpt=excerpt,
        meta_title=meta_title,
        meta_description=meta_description,
        target_keywords=keywords,
        generation_params=generation_params or payload.model_dump(mode="json"),
        ai_model=model if not demo else "demo",
        generated_at=datetime.now(UTC),
        word_count=_word_count(body),
    )
    session.add(item)
    session.flush()
    _add_version(item, "generated", "Generated with guarded Gemini service" if not demo else "Generated in demo mode")
    optimize_content(session, business, item, persist=True)
    _notify_generated(session, business, item)
    return item


def _notify_generated(session: Session, business: Business, item: Content) -> None:
    """Fire the content-generated event. Additive only: a notification bug must
    never roll back a generation that actually happened."""
    try:
        from app.models.enums import (
            ActivityAction,
            NotificationLevel,
            NotificationType,
        )
        from app.services import system as system_service

        user = business.owner
        system_service.create_notification(
            session,
            user,
            notification_type=NotificationType.CONTENT_GENERATED,
            level=NotificationLevel.SUCCESS,
            title="Content generated",
            message=f"'{item.title}' is ready to review.",
            business_id=business.id,
            link_path="/dashboard?section=Content",
            context={"content_id": str(item.id)},
        )
        system_service.record_activity(
            session,
            user=user,
            action=ActivityAction.CONTENT_GENERATED,
            description=f"Content '{item.title}' was generated.",
            business_id=business.id,
            entity_type="content",
            entity_id=item.id,
        )
    except Exception:  # noqa: BLE001 - events must never break generation
        from app.core.logging import get_logger

        get_logger(__name__).error(
            "content_generated_notification_failed",
            content_id=str(item.id),
            exc_info=True,
        )


def _keyword_density(body: str, keywords: list[str]) -> dict[str, float]:
    total = max(1, word_count(body))
    return {key: round(count / total * 100.0, 2) for key, count in keyword_occurrences(body, keywords).items()}


def _repeated_phrase_ratio(body: str) -> float:
    tokens = [t.casefold() for t in words(body)]
    if len(tokens) < 30:
        return 0.0
    trigrams = Counter(" ".join(tokens[i:i+3]) for i in range(len(tokens)-2))
    repeated = sum(max(0, count - 1) for count in trigrams.values())
    return repeated / max(1, len(tokens))


def optimize_content(session: Session, business: Business, item: Content, *, persist: bool = True) -> ContentScore:
    body = item.body or ""
    title_len = len((item.title or "").strip())
    wc = word_count(body)
    read = flesch_reading_ease(body)
    grade = flesch_kincaid_grade(body)
    keyword_counts = keyword_occurrences(body, list(item.target_keywords or []))
    coverage = sum(1 for value in keyword_counts.values() if value > 0) / max(1, len(item.target_keywords or []))
    repetition = _repeated_phrase_ratio(body)

    categories = {}
    issues: list[dict[str, object]] = []
    suggestions: list[dict[str, object]] = []

    # Title / metadata: 25
    title = 10 if 20 <= title_len <= 70 else 5 if title_len else 0
    meta = 10 if item.meta_description and 50 <= len(item.meta_description) <= 170 else 0 if not item.meta_description else 5
    categories["metadata"] = {"earned": title + meta, "available": 20, "checks": {"title": title, "meta_description": meta}}
    if title < 10:
        issues.append({"rule_id": "content.title_length", "severity": "medium", "title": "Improve the title", "description": "Use a concise descriptive title.", "points": 10 - title})
    if meta < 10:
        issues.append({"rule_id": "content.meta_description", "severity": "medium", "title": "Improve the meta description", "description": "Add or refine a 50–170 character meta description.", "points": 10 - meta})

    # Structure: 20
    heading_count = len(re.findall(r"^#{1,3}\s+", body, flags=re.M))
    structure = 20 if heading_count >= 3 else 12 if heading_count >= 1 else 6
    categories["structure"] = {"earned": structure, "available": 20, "checks": {"headings": heading_count}}
    if structure < 20:
        issues.append({"rule_id": "content.headings", "severity": "low", "title": "Add clearer section headings", "description": "Break the content into logical sections.", "points": 20 - structure})

    # Readability: 20
    if read is None:
        readability = 10 if wc >= 100 else 6
    else:
        readability = 20 if read >= 60 else 14 if read >= 45 else 8
    categories["readability"] = {"earned": readability, "available": 20, "checks": {"readability": read, "grade_level": grade}}
    if readability < 20:
        issues.append({"rule_id": "content.readability", "severity": "low", "title": "Improve readability", "description": "Shorten dense sentences and use clearer language.", "points": 20 - readability})

    # Relevance: heuristic, 20
    relevance = round(20 * coverage, 1) if item.target_keywords else 12
    if repetition > 0.10:
        relevance = max(0, relevance - 4)
    categories["topic_relevance"] = {"earned": relevance, "available": 20, "checks": {"keyword_coverage": coverage, "repeated_phrase_ratio": round(repetition, 3), "heuristic": True}}
    if not item.target_keywords:
        suggestions.append({"rule_id": "content.target_keywords", "severity": "medium", "title": "Add target topics", "description": "Attach the target search topics this content should address."})
    elif coverage < 0.6:
        suggestions.append({"rule_id": "content.keyword_coverage", "severity": "medium", "title": "Strengthen topic coverage", "description": "Cover more of the selected target topics naturally."})

    # Depth: 15
    depth = 15 if wc >= 700 else 12 if wc >= 400 else 8 if wc >= 200 else 3
    categories["depth"] = {"earned": depth, "available": 15, "checks": {"visible_word_count": wc, "heuristic": True}}
    if depth < 15:
        suggestions.append({"rule_id": "content.depth", "severity": "medium", "title": "Add useful depth", "description": "Expand the content with concrete, business-specific information."})

    total_earned = sum(float(v["earned"]) for v in categories.values())
    total_available = sum(float(v["available"]) for v in categories.values())
    overall = round(total_earned / total_available * 100.0, 1)
    score = ContentScore(
        content_id=item.id,
        version_number=item.current_version,
        overall_score=overall,
        grade=ScoreGrade.from_score(overall).value,
        category_scores=categories,
        issues=issues,
        suggestions=suggestions,
        readability_score=read,
        reading_grade_level=grade,
        keyword_density=_keyword_density(body, list(item.target_keywords or [])),
        engine_version="1.0",
    )
    if persist:
        session.add(score)
        item.latest_score = overall
        session.flush()
    return score


def latest_score(session: Session, business: Business, item: Content) -> ContentScore | None:
    return session.scalar(
        select(ContentScore)
        .where(ContentScore.content_id == item.id)
        .order_by(ContentScore.created_at.desc())
        .limit(1)
    )


__all__ = [
    "create_content",
    "create_generated_content",
    "get_content",
    "latest_score",
    "list_content",
    "optimize_content",
    "update_content",
]
