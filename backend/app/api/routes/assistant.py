"""Tenant-scoped AI assistant conversations."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession, OwnedBusiness
from app.models.assistant import AiConversation
from app.schemas.assistant import (
    AssistantConversationListOut,
    AssistantConversationOut,
    AssistantMessageCreate,
    AssistantMessageOut,
)
from app.services import assistant as assistant_service

router = APIRouter(
    prefix="/businesses/{business_id}/assistant",
    tags=["assistant"],
)


@router.get("/conversations", response_model=AssistantConversationListOut)
def list_conversations(
    business: OwnedBusiness, user: CurrentUser, session: DbSession
) -> AssistantConversationListOut:
    return AssistantConversationListOut(
        items=assistant_service.list_conversations(session, business, user.id)
    )


@router.post("/conversations", response_model=AssistantConversationOut, status_code=201)
def create_conversation(
    business: OwnedBusiness, user: CurrentUser, session: DbSession
) -> AiConversation:
    conversation = assistant_service.create_conversation(session, business, user.id)
    session.commit()
    session.refresh(conversation)
    return conversation


@router.get("/conversations/{conversation_id}", response_model=AssistantConversationOut)
def get_conversation(
    conversation_id: uuid.UUID,
    business: OwnedBusiness,
    user: CurrentUser,
    session: DbSession,
) -> AiConversation:
    return assistant_service.get_conversation(
        session, business, user.id, conversation_id
    )


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=AssistantMessageOut,
)
def send_message(
    conversation_id: uuid.UUID,
    payload: AssistantMessageCreate,
    business: OwnedBusiness,
    user: CurrentUser,
    session: DbSession,
) -> AssistantMessageOut:
    conversation = assistant_service.get_conversation(
        session, business, user.id, conversation_id
    )
    message = assistant_service.add_message(
        session, conversation, payload.content, business
    )
    session.commit()
    session.refresh(message)
    return message


@router.post("/chat", response_model=AssistantMessageOut)
def chat(
    payload: AssistantMessageCreate,
    business: OwnedBusiness,
    user: CurrentUser,
    session: DbSession,
) -> AssistantMessageOut:
    """Convenience endpoint that creates a conversation and answers one question."""
    conversation = assistant_service.create_conversation(session, business, user.id)
    message = assistant_service.add_message(
        session, conversation, payload.content, business
    )
    session.commit()
    session.refresh(message)
    return message
