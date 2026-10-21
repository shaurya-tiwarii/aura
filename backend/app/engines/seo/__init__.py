"""The SEO scoring engine (spec section 6): a genuine, explainable scorer.

Public surface:

* :func:`~app.engines.seo.engine.score_pages` -- score a scan's pages into a
  :class:`~app.engines.seo.engine.SeoScoreResult`.
* :data:`~app.engines.seo.rules.RULES` / :data:`~app.engines.seo.rules.CATEGORY_WEIGHTS`
  -- the declarative ruleset every deduction traces back to.
* :data:`~app.engines.seo.rules.ENGINE_VERSION` -- stamped on every report so the
  improvement loop never compares scores across a ruleset change.
"""

from __future__ import annotations

from app.engines.seo.engine import (
    CategoryResult,
    IssueResult,
    SeoScoreResult,
    score_pages,
)
from app.engines.seo.rules import (
    CATEGORY_WEIGHTS,
    ENGINE_VERSION,
    RULES,
    Rule,
    SiteContext,
)

__all__ = [
    "CATEGORY_WEIGHTS",
    "ENGINE_VERSION",
    "RULES",
    "CategoryResult",
    "IssueResult",
    "Rule",
    "SeoScoreResult",
    "SiteContext",
    "score_pages",
]
