"""Authenticated, business-scoped content management and generation."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request, status

from app.api.deps import DbSession, OwnedBusiness
from app.core.errors import AIGenerationError
from app.core.rate_limit import AI_LIMIT, limit_route
from app.models.enums import ContentStatus, ContentType
from app.schemas.content import (
    ContentCreate,
    ContentGenerate,
    ContentListOut,
    ContentOut,
    ContentScoreOut,
    ContentUpdate,
)
from app.services import business as business_service
from app.services import content as content_service
from app.services import opportunities as opportunity_service
from app.services.gemini import GeminiGenerationError, GeminiService

router = APIRouter(prefix="/businesses/{business_id}/content", tags=["content"])


@router.get("", response_model=ContentListOut, summary="List content drafts")
def list_content(
    business: OwnedBusiness,
    session: DbSession,
    content_status: ContentStatus | None = Query(default=None, alias="status"),
    content_type: ContentType | None = Query(default=None),
) -> ContentListOut:
    items, total = content_service.list_content(
        session,
        business,
        status=content_status.value if content_status else None,
        content_type=content_type.value if content_type else None,
    )
    return ContentListOut(items=[ContentOut.model_validate(i) for i in items], total=total)


@router.post("", response_model=ContentOut, status_code=status.HTTP_201_CREATED)
def create_content(payload: ContentCreate, business: OwnedBusiness, session: DbSession) -> ContentOut:
    item = content_service.create_content(session, business, payload)
    session.commit()
    session.refresh(item)
    return ContentOut.model_validate(item)


@router.post("/generate", response_model=ContentOut, status_code=status.HTTP_201_CREATED)
@limit_route(AI_LIMIT)
def generate_content(
    request: Request, payload: ContentGenerate, business: OwnedBusiness, session: DbSession
) -> ContentOut:
    """Generate new or rewrite existing content using business-grounded context."""
    profile = business_service.get_profile(session, business)
    if payload.opportunity_id is not None:
        opportunity = opportunity_service.get_opportunity(session, business, payload.opportunity_id)
        topic = payload.topic or opportunity.title
        if not payload.target_keywords:
            payload.target_keywords = list(opportunity.target_keywords or [])
        if not payload.title:
            payload.title = opportunity.title
    else:
        opportunity = None
        topic = payload.topic or payload.title or "Business content"
    if payload.body:
        topic = payload.title or topic

    context = {
        "business_name": business.name,
        "industry": business.industry,
        "description": business.description,
        "location": business.location,
        "profile": profile.prompt_context() if profile else {},
        "opportunity": (
            {
                "title": opportunity.title,
                "rationale": opportunity.rationale,
                "outline": opportunity.suggested_outline,
            }
            if opportunity
            else None
        ),
    }
    service = GeminiService()
    try:
        if payload.body:
            result = service.rewrite(payload.title or topic, payload.body, payload.tone)
            title = result.title
            body = result.body
            excerpt = None
            meta_title = title[:200]
            meta_description = result.meta_description
            keywords = result.suggested_keywords or payload.target_keywords
            demo = result.demo
        else:
            result = service.generate_content(
                content_type=payload.content_type.value,
                topic=topic,
                business_context=context,
                tone=payload.tone,
                target_keywords=payload.target_keywords,
                audience=payload.audience,
                call_to_action=payload.call_to_action,
            )
            title = result.title
            body = result.body
            excerpt = result.excerpt
            meta_title = result.meta_title
            meta_description = result.meta_description
            keywords = result.suggested_keywords or payload.target_keywords
            demo = result.demo
    except GeminiGenerationError as exc:
        raise AIGenerationError(str(exc)) from exc

    item = content_service.create_generated_content(
        session,
        business,
        ContentCreate(
            content_type=payload.content_type,
            title=title,
            body=body,
            excerpt=excerpt,
            meta_title=meta_title,
            meta_description=meta_description,
            target_keywords=keywords,
        ),
        title=title,
        body=body,
        excerpt=excerpt,
        meta_title=meta_title,
        meta_description=meta_description,
        keywords=keywords,
        demo=demo,
        model=service.model,
        generation_params=payload.model_dump(mode="json"),
    )
    if opportunity is not None:
        opportunity.status = "accepted"
        opportunity.content_id = item.id
    session.commit()
    session.refresh(item)
    return ContentOut.model_validate(item)


@router.get("/{content_id}", response_model=ContentOut)
def get_content(content_id: uuid.UUID, business: OwnedBusiness, session: DbSession) -> ContentOut:
    return ContentOut.model_validate(content_service.get_content(session, business, content_id))


@router.post("/{content_id}/optimize", response_model=ContentScoreOut)
def optimize_content(content_id: uuid.UUID, business: OwnedBusiness, session: DbSession) -> ContentScoreOut:
    item = content_service.get_content(session, business, content_id)
    score = content_service.optimize_content(session, business, item, persist=True)
    session.commit()
    session.refresh(score)
    return ContentScoreOut.model_validate(score)


@router.get("/{content_id}/score", response_model=ContentScoreOut)
def get_content_score(content_id: uuid.UUID, business: OwnedBusiness, session: DbSession) -> ContentScoreOut:
    item = content_service.get_content(session, business, content_id)
    score = content_service.latest_score(session, business, item)
    if score is None:
        score = content_service.optimize_content(session, business, item, persist=True)
        session.commit()
        session.refresh(score)
    return ContentScoreOut.model_validate(score)


@router.patch("/{content_id}", response_model=ContentOut)
def update_content(
    content_id: uuid.UUID,
    payload: ContentUpdate,
    business: OwnedBusiness,
    session: DbSession,
) -> ContentOut:
    item = content_service.get_content(session, business, content_id)
    content_service.update_content(session, business, item, payload)
    session.commit()
    session.refresh(item)
    return ContentOut.model_validate(item)
