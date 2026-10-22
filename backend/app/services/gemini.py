"""Guarded Gemini service layer for explanations and rewrites only.

This repository already uses a deterministic recommendation engine and an
explainable scoring stack for ranking and analysis. The Gemini layer is
explicitly limited to model-backed generation used for *explanation* and
*rewrite* assistance, and every reply is JSON-validated before it is returned
so untrusted model text never enters the application as a free-form object.

When the installation has no GEMINI_API_KEY configured the service stays
honest and returns a clearly labelled demo payload instead of pretending the
model generated real text.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from app.core.config import settings


class ExplanationOutput(BaseModel):
    """Structured response produced for recommendation explanations.

    A model reply that does not match this structure is rejected by the service
    before it reaches business logic or a route.
    """

    summary: str = Field(min_length=1, max_length=300)
    explanation: str = Field(min_length=1, max_length=2000)
    key_points: list[str] = Field(default_factory=list)
    demo: bool = False


class ContentGenerationOutput(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=30000)
    excerpt: str | None = Field(default=None, max_length=5000)
    meta_title: str | None = Field(default=None, max_length=200)
    meta_description: str | None = Field(default=None, max_length=5000)
    suggested_keywords: list[str] = Field(default_factory=list)
    suggested_hashtags: list[str] = Field(default_factory=list)
    demo: bool = False


class RewriteOutput(BaseModel):
    """Structured response produced for content rewrites.

    The shape mirrors the core content model's fields enough to keep the service
    generically useful without claiming another stage of the application is
    present.
    """

    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)
    meta_description: str | None = Field(default=None, max_length=280)
    suggested_keywords: list[str] = Field(default_factory=list)
    demo: bool = False


class AssistantOutput(BaseModel):
    """Validated answer returned by the tenant-scoped marketing assistant."""

    answer: str = Field(min_length=1, max_length=6000)
    suggested_actions: list[dict[str, str]] = Field(default_factory=list)
    demo: bool = False


def plain_text(value: str) -> str:
    """Reduce model prose to plain text for the assistant surfaces.

    The chat UI renders message text literally, so markdown markers would
    show up as raw asterisks. Assistant and explanation answers are therefore
    stored and displayed without markdown: emphasis markers, heading hashes,
    blockquote markers, and code backticks are removed, and em/en dashes are
    replaced (a comma for the spaced form, a hyphen otherwise) because the
    product copy standard bans dash separators. Content generation is NOT
    passed through here: generated articles legitimately use markdown
    headings, and the content scorer counts them.
    """
    text = value or ""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.S)
    text = re.sub(r"\*(.+?)\*", r"\1", text, flags=re.S)
    text = re.sub(r"^\s*\*\s+", "- ", text, flags=re.M)
    text = text.replace("*", "")
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"^>\s?", "", text, flags=re.M)
    text = text.replace("`", "")
    text = re.sub(r"^\s*[-_]{3,}\s*$", "", text, flags=re.M)
    text = text.replace(" \u2014 ", ", ").replace(" \u2013 ", ", ")
    text = text.replace("\u2014", "-").replace("\u2013", "-")
    return text.strip()


class GeminiServiceError(RuntimeError):
    """Local error type for the Gemini service without importing the app error envelope."""


class GeminiUnavailableError(GeminiServiceError):
    """Raised when no Gemini key is configured and the caller asked for a live model call."""


class GeminiGenerationError(GeminiServiceError):
    """Raised when the model call fails, returns invalid JSON, or emits the wrong schema."""


class GeminiService:
    """Thin adapter around the configured google-genai client.

    Generation is exposed for business-grounded content, plus explanation and rewrite.
    Recommendation ranking, effort and impact still remain deterministic app
    logic in the recommendation engine; Gemini performs only generation.
    """

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or settings.gemini_api_key
        self.model = model or settings.gemini_model
        self.enabled = bool(self.api_key)

    def _client(self) -> Any:
        if not self.enabled:
            raise GeminiUnavailableError(
                "AI features are not configured on this installation. Add GEMINI_API_KEY to enable structured generation."
            )
        from google import genai

        return genai.Client(api_key=self.api_key)


    def generate_content(
        self,
        *,
        content_type: str,
        topic: str,
        business_context: dict[str, Any],
        tone: str = "clear",
        target_keywords: list[str] | None = None,
        audience: str | None = None,
        call_to_action: str | None = None,
    ) -> ContentGenerationOutput:
        """Generate new content from owned business context only."""
        if not self.enabled:
            return self._demo_generation(content_type, topic, business_context, target_keywords or [])
        prompt = self._build_generation_prompt(
            content_type=content_type,
            topic=topic,
            business_context=business_context,
            tone=tone,
            target_keywords=target_keywords or [],
            audience=audience,
            call_to_action=call_to_action,
        )
        return self._generate_structured_json(prompt, ContentGenerationOutput)

    def explain(
        self,
        subject: str,
        context: str,
        tone: str = "friendly",
    ) -> ExplanationOutput:
        """Explain a recommendation or content problem in a business-safe tone.

        The model input is constrained to the same subject string and context the
        caller already owns; this service never calls the model with a free-form
        arbitrary prompt from the request object without schema normalization.
        """
        if not self.enabled:
            return self._demo_explanation(subject, context)

        prompt = self._build_explanation_prompt(subject, context, tone)
        typed = self._generate_structured_json(prompt, ExplanationOutput)
        return ExplanationOutput(
            summary=plain_text(typed.summary),
            explanation=plain_text(typed.explanation),
            key_points=[plain_text(point) for point in typed.key_points],
            demo=typed.demo,
        )

    def rewrite(self, title: str, body: str, tone: str = "clear") -> RewriteOutput:
        """Return a structured rewrite for an existing content draft.

        The rewrite service never invents facts that are not present in the
        supplied body. It is designed as a pure assistance layer for rewrite
        drafts, not a replacement for the user's content.
        """
        if not self.enabled:
            return self._demo_rewrite(title, body)

        prompt = self._build_rewrite_prompt(title, body, tone)
        return self._generate_structured_json(prompt, RewriteOutput)

    def chat(self, question: str, context: str, history: str = "") -> AssistantOutput:
        """Answer a grounded business question using only supplied evidence."""
        if not self.enabled:
            return AssistantOutput(
                answer=(
                    "Demo assistant response: Gemini is not configured, so no live "
                    "model call was made. The supplied business evidence is ready "
                    "for a live answer when GEMINI_API_KEY is added."
                ),
                suggested_actions=[],
                demo=True,
            )
        prompt = (
            "Return JSON only with fields answer, suggested_actions, and demo. "
            "The demo field must be boolean false. Answer only from the supplied "
            "business evidence; say when evidence is missing. Never claim to have "
            "taken an action. suggested_actions must be short objects with only "
            "title and description strings. Write the answer in plain text only: "
            "no markdown, no asterisks, no bold or italic markers, no headings, "
            "and no em dashes or en dashes. Use short lines; if you list points, "
            "put each point on its own line starting with its number. Justify "
            "every point with the evidence behind it (the score, count, or fact "
            "from the supplied evidence) instead of giving bare advice.\n"
            f"Question: {question}\nConversation history: {history}\n"
            f"Business evidence: {context}"
        )
        typed = self._generate_structured_json(prompt, AssistantOutput)
        return AssistantOutput(
            answer=plain_text(typed.answer),
            suggested_actions=[
                {
                    "title": plain_text(str(action.get("title", ""))),
                    "description": plain_text(str(action.get("description", ""))),
                }
                for action in typed.suggested_actions
            ],
            demo=typed.demo,
        )

    def _generate_structured_json(
        self,
        prompt: str,
        model_type: type[BaseModel],
    ) -> BaseModel:
        """Call the Gemini client and validate the returned JSON payload.

        The model output is treated as untrusted and is strictly parsed into the
        Pydantic schema before being surfaced.
        """
        try:
            client = self._client()
            response = client.models.generate_content(
                model=self.model,
                contents=prompt,
                config={
                    "temperature": 0.2,
                    "max_output_tokens": settings.gemini_max_output_tokens,
                    "response_mime_type": "application/json",
                },
            )
            text = (getattr(response, "text", None) or "").strip()
            if not text:
                raise GeminiGenerationError("The AI model returned an empty response.")
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise GeminiGenerationError(
                "The AI model returned an invalid structured response."
            ) from exc
        except GeminiGenerationError:
            raise
        except Exception as exc:
            raise GeminiGenerationError(
                "The AI model could not complete that request."
            ) from exc

        try:
            typed = TypeAdapter(model_type).validate_python(parsed)
        except ValidationError as exc:
            raise GeminiGenerationError(
                "The AI model returned a schema that does not match the expected fields."
            ) from exc
        return typed

    def _build_generation_prompt(
        self, *, content_type: str, topic: str, business_context: dict[str, Any],
        tone: str, target_keywords: list[str], audience: str | None, call_to_action: str | None,
    ) -> str:
        return (
            "Return JSON only with fields title, body, excerpt, meta_title, meta_description, "
            "suggested_keywords, suggested_hashtags, and demo. The demo field must be false "
            "for live generation. Generate original content grounded only in the supplied business "
            "context. Never invent prices, claims, certifications, customer numbers, locations, "
            "or other facts not present in the context. Adapt the format to the requested content type. "
            f"Content type: {content_type}\nTopic: {topic}\nTone: {tone}\n"
            f"Target keywords: {target_keywords}\nAudience: {audience or 'use the supplied target audience'}\n"
            f"Call to action: {call_to_action or 'use a natural CTA only when appropriate'}\n"
            f"Business context: {json.dumps(business_context, ensure_ascii=False)}"
        )

    def _demo_generation(
        self, content_type: str, topic: str, business_context: dict[str, Any], keywords: list[str]
    ) -> ContentGenerationOutput:
        name = str(business_context.get("business_name") or "your business")
        return ContentGenerationOutput(
            title=topic or f"{name}: {content_type.replace('_', ' ').title()}",
            body=(
                f"Demo content for {name}.\n\nTopic: {topic or 'General business content'}. "
                "No live Gemini call was made. Add GEMINI_API_KEY to enable real content generation."
            ),
            excerpt=f"Demo content for {name} about {topic or 'this topic'}.",
            meta_title=(topic or name)[:200],
            meta_description=f"Demo description for {topic or name}."[:5000],
            suggested_keywords=keywords[:10] or [content_type.replace('_', ' ')],
            suggested_hashtags=["#AURA", "#ContentMarketing"],
            demo=True,
        )

    def _build_explanation_prompt(self, subject: str, context: str, tone: str) -> str:
        return (
            "Return JSON only with the fields summary, explanation, key_points, and demo.\n"
            "The `demo` field must be a boolean `false` for a live Gemini answer.\n"
            f"Tone: {tone}\n"
            f"Subject: {subject}\n"
            f"Context: {context}\n"
            "Keep the explanation grounded in the evidence. Explain clearly to a non-technical business owner."
        )

    def _build_rewrite_prompt(self, title: str, body: str, tone: str) -> str:
        return (
            "Return JSON only with the fields title, body, meta_description, suggested_keywords, and demo.\n"
            "The `demo` field must be a boolean `false` for a live Gemini answer.\n"
            f"Tone: {tone}\n"
            f"Current title: {title}\n"
            f"Current body: {body}\n"
            "Preserve facts and intent from the source copy while improving clarity and readability."
        )

    def _demo_explanation(self, subject: str, context: str) -> ExplanationOutput:
        return ExplanationOutput(
            summary=f"Demo explanation for: {subject}",
            explanation=(
                "This is a clearly labelled demo explanation because the local "
                "installation does not have GEMINI_API_KEY configured. The service "
                "architecture is ready for a real Gemini response when credentials "
                "are added."
            ),
            key_points=[
                "No live model call was made.",
                context[:120] if context else "Evidence supplied by the application.",
            ],
            demo=True,
        )

    def _demo_rewrite(self, title: str, body: str) -> RewriteOutput:
        return RewriteOutput(
            title=title,
            body=(
                f"Demo rewrite for '{title}':\n\n"
                f"{body[:250]}\n\n"
                "This payload is generated locally in demo mode because no "
                "GEMINI_API_KEY is configured."
            ),
            meta_description="Demo description generated by the local rewrite service.",
            suggested_keywords=["demo", "aura", "content"],
            demo=True,
        )


# Convenience service functions for the rest of the app to import.
def explanations_for(
    subject: str, context: str, tone: str = "friendly"
) -> ExplanationOutput:
    """Return a structured explanation object for a recommendation or issue."""
    return GeminiService().explain(subject, context, tone=tone)


def rewrite_content(title: str, body: str, tone: str = "clear") -> RewriteOutput:
    """Return a structured content rewrite object in the same JSON envelope."""
    return GeminiService().rewrite(title, body, tone=tone)
