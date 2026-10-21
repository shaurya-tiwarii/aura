"""Tenant-safe assistant persistence and evidence assembly."""

from __future__ import annotations

import json
import time
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import NotFoundError
from app.models.ai_visibility import AiVisibilityReport
from app.models.assistant import AiConversation, AiMessage
from app.models.business import Business
from app.models.enums import MessageRole
from app.models.recommendation import Recommendation
from app.models.seo import SeoReport
from app.models.website import Website, WebsiteScan
from app.services.gemini import GeminiGenerationError, GeminiService


def list_conversations(session: Session, business: Business, user_id: uuid.UUID):
    return list(
        session.scalars(
            select(AiConversation)
            .where(
                AiConversation.business_id == business.id,
                AiConversation.user_id == user_id,
            )
            .order_by(AiConversation.updated_at.desc())
        )
    )


def get_conversation(
    session: Session, business: Business, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> AiConversation:
    conversation = session.scalar(
        select(AiConversation)
        .options(selectinload(AiConversation.messages))
        .where(
            AiConversation.id == conversation_id,
            AiConversation.business_id == business.id,
            AiConversation.user_id == user_id,
        )
    )
    if conversation is None:
        raise NotFoundError("We could not find that assistant conversation.")
    return conversation


def create_conversation(
    session: Session,
    business: Business,
    user_id: uuid.UUID,
    title: str = "New conversation",
) -> AiConversation:
    conversation = AiConversation(
        business_id=business.id,
        user_id=user_id,
        title=title[:300] or "New conversation",
    )
    session.add(conversation)
    session.flush()
    return conversation


def _grounding(session: Session, business: Business) -> dict[str, object]:
    """Build bounded context strictly through the owned business foreign key."""
    scans = list(
        session.scalars(
            select(WebsiteScan)
            .join(Website, Website.id == WebsiteScan.website_id)
            .where(Website.business_id == business.id)
            .options(
                selectinload(WebsiteScan.seo_report).selectinload(
                    SeoReport.category_scores
                ),
                selectinload(WebsiteScan.ai_visibility_report).selectinload(
                    AiVisibilityReport.category_scores
                ),
            )
            .order_by(WebsiteScan.created_at.desc())
            .limit(3)
        )
    )
    recs = list(
        session.scalars(
            select(Recommendation)
            .where(
                Recommendation.business_id == business.id,
                Recommendation.status.in_(("pending", "in_progress")),
            )
            .order_by(Recommendation.priority_score.desc())
            .limit(10)
        )
    )
    return {
        "business": {
            "id": str(business.id),
            "name": business.name,
            "industry": business.industry,
        },
        "scans": [
            {
                "id": str(scan.id),
                "status": scan.status,
                "sequence": scan.sequence,
                "seo": (
                    {
                        "score": scan.seo_report.overall_score,
                        "grade": scan.seo_report.grade,
                        "categories": [
                            {
                                "category": category.category,
                                "score": category.normalized_score,
                            }
                            for category in scan.seo_report.category_scores
                        ],
                    }
                    if scan.seo_report
                    else None
                ),
                "ai_visibility": (
                    {
                        "score": scan.ai_visibility_report.overall_score,
                        "grade": scan.ai_visibility_report.grade,
                        "categories": [
                            {
                                "category": category.category,
                                "score": category.normalized_score,
                            }
                            for category in scan.ai_visibility_report.category_scores
                        ],
                    }
                    if scan.ai_visibility_report
                    else None
                ),
            }
            for scan in scans
        ],
        "recommendations": [
            {
                "id": str(rec.id),
                "title": rec.title,
                "description": rec.description,
                "priority": rec.priority_bucket,
                "status": rec.status,
            }
            for rec in recs
        ],
    }


def add_message(
    session: Session,
    conversation: AiConversation,
    content: str,
    business: Business,
    *,
    gemini: GeminiService | None = None,
) -> AiMessage:
    grounding = _grounding(session, business)
    user_message = AiMessage(
        conversation_id=conversation.id,
        role=MessageRole.USER.value,
        content=content,
        grounding=grounding,
        suggested_actions=[],
    )
    session.add(user_message)
    session.flush()
    history = "\n".join(
        f"{message.role}: {message.content[:1000]}"
        for message in conversation.messages[-8:]
    )
    started = time.perf_counter()
    try:
        output = (gemini or GeminiService()).chat(
            content, json.dumps(grounding, default=str), history=history
        )
        answer = output.answer
        actions = output.suggested_actions
        model = "demo" if output.demo else (gemini or GeminiService()).model
        error = None
    except GeminiGenerationError as exc:
        answer = "The assistant could not generate an answer for this question."
        actions = []
        model = None
        error = str(exc)
    assistant_message = AiMessage(
        conversation_id=conversation.id,
        role=MessageRole.ASSISTANT.value,
        content=answer,
        grounding=grounding,
        suggested_actions=actions,
        model=model,
        latency_ms=round((time.perf_counter() - started) * 1000),
        error_message=error,
    )
    session.add(assistant_message)
    conversation.title = (
        content[:300]
        if conversation.title == "New conversation"
        else conversation.title
    )
    conversation.message_count = (conversation.message_count or 0) + 2
    session.flush()
    return assistant_message
