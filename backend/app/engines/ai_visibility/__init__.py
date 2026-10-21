"""The AI-visibility scoring engine (spec sections 7 and 45): an explainable
heuristic for "Estimated AI-search readiness."

Public surface:

* :func:`~app.engines.ai_visibility.engine.score_pages` -- score a scan's pages
  into an :class:`~app.engines.ai_visibility.engine.AiVisibilityResult`.
* :data:`~app.engines.ai_visibility.signals.SIGNALS` /
  :data:`~app.engines.ai_visibility.signals.CATEGORY_WEIGHTS` -- the declarative
  signal set every point traces back to.
* :data:`~app.engines.ai_visibility.signals.ENGINE_VERSION` -- stamped on every
  report so the improvement loop never compares scores across a signal-set change.

The score estimates readiness from on-page signals; it never measures or
guarantees how any AI system ranks or cites the site.
"""

from __future__ import annotations

from app.engines.ai_visibility.engine import (
    AiCategoryResult,
    AiVisibilityResult,
    score_pages,
)
from app.engines.ai_visibility.signals import (
    CATEGORY_WEIGHTS,
    ENGINE_VERSION,
    SIGNALS,
    AiSiteContext,
    Signal,
)

__all__ = [
    "CATEGORY_WEIGHTS",
    "ENGINE_VERSION",
    "SIGNALS",
    "AiCategoryResult",
    "AiSiteContext",
    "AiVisibilityResult",
    "Signal",
    "score_pages",
]
