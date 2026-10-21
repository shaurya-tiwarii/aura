"""Small API surface for the guarded Gemini generation service.

Only two generation endpoints are exposed by this route file and both are
strictly examples of the stage-8 interface: explanation and rewrite. All
ranking and scoring downstream remain deterministic application logic and
never depend on Gemini at runtime.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.core.rate_limit import AI_LIMIT, limit_route
from app.services.gemini import (
    ExplanationOutput,
    GeminiService,
    RewriteOutput,
)

router = APIRouter(prefix="/ai", tags=["gemini"])


class ExplainRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    context: str = Field(min_length=1, max_length=2000)
    tone: str = Field(default="friendly", min_length=1, max_length=50)


class RewriteRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=12000)
    tone: str = Field(default="clear", min_length=1, max_length=50)


@router.post("/explain", response_model=ExplanationOutput)
@limit_route(AI_LIMIT)
def explain(request: Request, payload: ExplainRequest) -> ExplanationOutput:
    """Return a structured explanation, or a marked demo object if no key exists."""
    return GeminiService().explain(payload.subject, payload.context, tone=payload.tone)


@router.post("/rewrite", response_model=RewriteOutput)
@limit_route(AI_LIMIT)
def rewrite(request: Request, payload: RewriteRequest) -> RewriteOutput:
    """Return a structured rewrite, or a marked demo object if no key exists."""
    return GeminiService().rewrite(payload.title, payload.body, tone=payload.tone)
