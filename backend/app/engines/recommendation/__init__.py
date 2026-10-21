"""The recommendation engine (Module 8): SEO issues + AI-visibility gaps -> actions.

Pure application logic, like its siblings :mod:`app.engines.seo` and
:mod:`app.engines.ai_visibility`: no database session, no language model, no
wall-clock reads. It takes the *already-scored* findings of one scan -- the SEO
issues that fired and the AI-visibility signals a site is missing -- and folds
them into a single prioritised, deduplicated action list.

Each action carries the ``rule_id`` (SEO) or ``signal_id`` (AI-visibility) that
produced it, so the improvement loop (Module 20) can later credit a fix when that
exact issue disappears from a subsequent scan. Ranking is impact x effort and is
entirely deterministic; the only thing this stage does *not* do is write prose --
generating an explanation or a rewrite is the Gemini stage that follows, and this
engine never imports it.
"""

from __future__ import annotations

from app.engines.recommendation.catalog import ENGINE_VERSION
from app.engines.recommendation.engine import (
    AiSignalGapInput,
    RecommendationDraft,
    SeoIssueInput,
    build_recommendations,
)

__all__ = [
    "ENGINE_VERSION",
    "AiSignalGapInput",
    "RecommendationDraft",
    "SeoIssueInput",
    "build_recommendations",
]
