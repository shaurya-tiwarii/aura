"""The recommendation catalog: the fixed lookup tables the engine ranks against.

Three things live here, and only here, so adding an SEO rule or an AI signal
forces a matching entry rather than silently producing an unrankable finding:

* :data:`EFFORT_BY_RULE` / :data:`EFFORT_BY_SIGNAL` -- how much work each fix is.
  Impact is *derived* from the score arithmetic (a bigger deduction is a bigger
  win), but effort is a human judgement about the change itself, so it is stated
  explicitly. Together they give the impact x effort priority the UI groups by.
* :data:`AI_REMEDIATION` -- the SEO engine attaches a ``fix`` to every rule, but
  the AI-visibility engine records *signals* (present/absent facts), not fixes,
  so a missing signal has no remediation text of its own. This table supplies the
  owner-facing title, the "why it matters for AI search", and the concrete next
  step for each of the 23 signals. It is fixed copy, written here, never generated.
* :data:`SEO_CATEGORY_MAP` -- maps an SEO category onto the shared
  :class:`RecommendationCategory` (identity for the eight SEO categories);
  everything from the AI-visibility engine is filed under ``AI_VISIBILITY``.

:func:`_validate` runs at import and raises if any rule or signal is missing an
effort, if the SEO and AI id spaces ever collide (the dedup key is the raw id, so
a collision would merge two unrelated findings), or if a category cannot be
mapped. A wrong table is a startup error, not a subtly mis-ranked list in
production.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.engines.ai_visibility.signals import SIGNALS
from app.engines.seo.rules import RULES
from app.models.enums import EffortLevel, RecommendationCategory, SeoCategory

# Bumped when the ranking logic or these tables change in a way that makes an old
# recommendation's priority incomparable with a new one. Informational today
# (recommendations are reconciled by id, not by version), but recorded in logs so
# a ranking change is traceable, exactly like the two scoring engines.
ENGINE_VERSION = "1.0"

# Score-gain thresholds that bucket a finding's *impact*. A gain is the estimated
# rise in the 0-100 headline if the fix is applied (see engine._score_gain), so
# these are in headline points: a fix worth >= 4 points is high impact, >= 1.5 is
# medium, the rest are low. Documented here rather than inlined so the one place
# that defines "high impact" is the one place the verifier reads it from.
IMPACT_HIGH_MIN = 4.0
IMPACT_MEDIUM_MIN = 1.5


# =============================================================================
# Effort: how much work each SEO fix is. Impact comes from the score arithmetic;
# this is the orthogonal axis. Keyed by the full dotted rule_id.
# =============================================================================
EFFORT_BY_RULE: dict[str, EffortLevel] = {
    # metadata -- edits to <head>, one field at a time
    "metadata.title_missing": EffortLevel.LOW,
    "metadata.title_length": EffortLevel.TRIVIAL,
    "metadata.description_missing": EffortLevel.LOW,
    "metadata.description_length": EffortLevel.TRIVIAL,
    "metadata.viewport_missing": EffortLevel.TRIVIAL,
    "metadata.open_graph_missing": EffortLevel.LOW,
    # content -- writing, the expensive axis
    "content.thin_content": EffortLevel.HIGH,
    "content.low_word_count": EffortLevel.MEDIUM,
    "content.no_target_keywords": EffortLevel.MEDIUM,
    # technical -- server/config or a template tag
    "technical.not_https": EffortLevel.MEDIUM,
    "technical.no_robots_txt": EffortLevel.TRIVIAL,
    "technical.no_sitemap": EffortLevel.LOW,
    "technical.noindex": EffortLevel.TRIVIAL,
    "technical.canonical_missing": EffortLevel.LOW,
    "technical.lang_missing": EffortLevel.TRIVIAL,
    "technical.slow_response": EffortLevel.HIGH,
    # structure -- headings, usually a template change
    "structure.h1_missing": EffortLevel.LOW,
    "structure.multiple_h1": EffortLevel.LOW,
    "structure.heading_hierarchy": EffortLevel.MEDIUM,
    "structure.no_subheadings": EffortLevel.MEDIUM,
    # links
    "links.broken_links": EffortLevel.MEDIUM,
    "links.no_internal_links": EffortLevel.MEDIUM,
    # schema -- hand-authored JSON-LD
    "schema.no_structured_data": EffortLevel.MEDIUM,
    "schema.no_organization": EffortLevel.LOW,
    # images
    "images.missing_alt": EffortLevel.LOW,
    # readability -- rewriting for clarity
    "readability.hard_to_read": EffortLevel.HIGH,
    "readability.grade_too_high": EffortLevel.MEDIUM,
}


@dataclass(frozen=True)
class AiRemediation:
    """Fixed, owner-facing remediation copy for one AI-visibility signal.

    Written here because the AI-visibility engine records the *absence* of a
    signal, not a fix for it. ``title`` is the action headline, ``description``
    explains what the gap is and why it matters for AI search (never a ranking
    promise -- spec sections 7 and 45), and ``action`` is the concrete next step.
    """

    title: str
    description: str
    action: str


# =============================================================================
# AI-visibility remediation: the title / why / how for each signal, plus its
# effort. Keyed by the full dotted signal_id. Disjoint from EFFORT_BY_RULE.
# =============================================================================
AI_REMEDIATION: dict[str, tuple[AiRemediation, EffortLevel]] = {
    "answerability.faq_present": (
        AiRemediation(
            title="Add an FAQ section",
            description=(
                "No FAQ content was found on the site. AI answer engines lift "
                "question-and-answer pairs directly, so an FAQ makes your pages "
                "much easier to quote."
            ),
            action=(
                "Create an FAQ page (or an FAQ block on key pages) answering the "
                "questions customers actually ask, one clear question per heading."
            ),
        ),
        EffortLevel.MEDIUM,
    ),
    "answerability.question_headings": (
        AiRemediation(
            title="Phrase some headings as questions",
            description=(
                "Few or no headings are written as questions. Question-shaped "
                "headings match how people prompt AI assistants and help those "
                "systems locate a direct answer on the page."
            ),
            action=(
                'Reword suitable headings into natural questions (e.g. "How much '
                'does X cost?") with the answer immediately below.'
            ),
        ),
        EffortLevel.LOW,
    ),
    "answerability.faq_depth": (
        AiRemediation(
            title="Deepen your FAQ answers",
            description=(
                "An FAQ was found but its answers are thin. Sparse answers give "
                "AI systems little to cite; a few substantive sentences per "
                "question are far more quotable."
            ),
            action=(
                "Expand each FAQ answer into a self-contained paragraph that "
                "fully answers the question without needing other context."
            ),
        ),
        EffortLevel.MEDIUM,
    ),
    "schema.structured_data_present": (
        AiRemediation(
            title="Add structured data (schema.org)",
            description=(
                "Pages lack schema.org markup. Structured data describes what a "
                "page is about in a machine-readable form, improving how reliably "
                "AI systems understand and attribute it."
            ),
            action=(
                "Add JSON-LD markup appropriate to each page (e.g. Article, "
                "Product, LocalBusiness) using schema.org types."
            ),
        ),
        EffortLevel.MEDIUM,
    ),
    "schema.organization_entity": (
        AiRemediation(
            title="Describe your business with Organization schema",
            description=(
                "No Organization or LocalBusiness entity markup was found. This "
                "is how AI systems tie your pages to a single, identifiable "
                "business."
            ),
            action=(
                "Add Organization (or LocalBusiness) JSON-LD on your homepage "
                "with name, URL, logo, and contact details."
            ),
        ),
        EffortLevel.LOW,
    ),
    "schema.answer_rich_types": (
        AiRemediation(
            title="Use answer-rich schema types",
            description=(
                "None of the answer-oriented schema types (FAQPage, HowTo, "
                "QAPage) were detected. These types map directly onto the answers "
                "AI engines assemble."
            ),
            action=(
                "Mark up FAQ or how-to content with FAQPage / HowTo JSON-LD so "
                "its question-and-answer structure is explicit."
            ),
        ),
        EffortLevel.MEDIUM,
    ),
    "structure.clear_headings": (
        AiRemediation(
            title="Add a clear heading structure",
            description=(
                "Some pages have little or no heading structure. Headings give "
                "AI systems the outline they use to find the relevant passage."
            ),
            action=(
                "Break content into sections with descriptive H2 and H3 headings "
                "that summarise what each section covers."
            ),
        ),
        EffortLevel.MEDIUM,
    ),
    "structure.scannable_blocks": (
        AiRemediation(
            title="Use lists and tables for scannable facts",
            description=(
                "Pages rely on unbroken prose. Lists and tables present facts in "
                "the compact, extractable form AI answers favour."
            ),
            action=(
                "Convert dense paragraphs of steps, features, or specifications "
                "into bulleted lists or tables."
            ),
        ),
        EffortLevel.LOW,
    ),
    "structure.single_main_heading": (
        AiRemediation(
            title="Give each page one clear H1",
            description=(
                "Some pages have no single main heading, or use several. One H1 "
                "states unambiguously what the page is about."
            ),
            action=(
                "Ensure each page has exactly one H1 naming its topic, with "
                "subsections under H2 and H3."
            ),
        ),
        EffortLevel.LOW,
    ),
    "entity_clarity.descriptive_titles": (
        AiRemediation(
            title="Write descriptive page titles",
            description=(
                "Some page titles are missing or generic. A specific title is "
                "the strongest single cue an AI system uses to identify a page's "
                "subject."
            ),
            action=(
                "Give each page a unique, descriptive <title> naming the topic "
                "and, where relevant, your business or location."
            ),
        ),
        EffortLevel.LOW,
    ),
    "entity_clarity.meta_descriptions": (
        AiRemediation(
            title="Add meta descriptions",
            description=(
                "Some pages lack a meta description. A concise summary helps AI "
                "systems and search engines state what the page offers."
            ),
            action=(
                "Write a one-to-two sentence meta description per page that "
                "accurately summarises its content."
            ),
        ),
        EffortLevel.LOW,
    ),
    "entity_clarity.language_declared": (
        AiRemediation(
            title="Declare the page language",
            description=(
                "The HTML language attribute is missing on some pages. Declaring "
                "it removes ambiguity for systems processing your text."
            ),
            action='Set <html lang="…"> (e.g. "en") on every page.',
        ),
        EffortLevel.TRIVIAL,
    ),
    "completeness.substantial_content": (
        AiRemediation(
            title="Add more substantial content",
            description=(
                "Some pages are too thin to be useful sources. AI systems prefer "
                "pages that cover a topic thoroughly enough to cite."
            ),
            action=(
                "Expand thin pages with genuinely useful detail -- aim for a "
                "complete treatment of the page's topic, not filler."
            ),
        ),
        EffortLevel.HIGH,
    ),
    "completeness.in_depth_content": (
        AiRemediation(
            title="Cover key topics in depth",
            description=(
                "Longer pages still lack depth on their topic. Comprehensive "
                "coverage is what makes a page the source an AI answer draws from."
            ),
            action=(
                "Add sections addressing related questions, edge cases, and the "
                "specifics a reader (or an AI) would expect on the topic."
            ),
        ),
        EffortLevel.HIGH,
    ),
    "completeness.topic_breadth": (
        AiRemediation(
            title="Broaden your topic coverage",
            description=(
                "The site covers a narrow range of topics. Breadth across the "
                "subjects customers ask about increases the chance of being a "
                "relevant source."
            ),
            action=(
                "Plan pages or articles for the main topics and questions in "
                "your field that the site does not yet address."
            ),
        ),
        EffortLevel.HIGH,
    ),
    "citation_readiness.authorship": (
        AiRemediation(
            title="Show authorship and credibility",
            description=(
                "No author or byline information was detected. Visible authorship "
                "is a trust signal AI systems weigh when deciding what to cite."
            ),
            action=(
                "Add an author name and short bio (or an About reference) to "
                "substantive pages and articles."
            ),
        ),
        EffortLevel.LOW,
    ),
    "citation_readiness.indexable": (
        AiRemediation(
            title="Let AI and search engines index the page",
            description=(
                "Some pages signal that they should not be indexed. A page "
                "excluded from indexes cannot be cited at all."
            ),
            action=(
                "Remove noindex from pages you want found, and confirm robots "
                "rules are not blocking them."
            ),
        ),
        EffortLevel.TRIVIAL,
    ),
    "citation_readiness.canonical_url": (
        AiRemediation(
            title="Set canonical URLs",
            description=(
                "Some pages have no canonical URL. A canonical tag consolidates "
                "signals onto one address so systems attribute content to the "
                "right page."
            ),
            action='Add <link rel="canonical"> pointing to each page\'s preferred URL.',
        ),
        EffortLevel.LOW,
    ),
    "citation_readiness.outbound_citations": (
        AiRemediation(
            title="Cite your sources",
            description=(
                "Pages include few outbound references. Linking to credible "
                "sources is an evidence signal that supports being cited in turn."
            ),
            action=(
                "Where you state facts or data, link to the reputable sources "
                "they came from."
            ),
        ),
        EffortLevel.LOW,
    ),
    "freshness.dates_present": (
        AiRemediation(
            title="Show published or updated dates",
            description=(
                "No dates were detected on the content. AI systems favour "
                "content whose recency they can establish."
            ),
            action=(
                "Display a visible published or last-updated date, and mark it "
                "up with datePublished / dateModified."
            ),
        ),
        EffortLevel.LOW,
    ),
    "freshness.recent_updates": (
        AiRemediation(
            title="Refresh stale content",
            description=(
                "Dated content has not been updated recently. Recency raises "
                "confidence that the information is still accurate."
            ),
            action=(
                "Review older pages and update them, refreshing the modified "
                "date when you make real changes."
            ),
        ),
        EffortLevel.MEDIUM,
    ),
    "trust.contact_info": (
        AiRemediation(
            title="Publish clear contact information",
            description=(
                "No contact information was found. Reachable, real-world contact "
                "details are a core trust signal for a legitimate business."
            ),
            action=(
                "Add a contact page or footer with email, phone, and address as "
                "appropriate."
            ),
        ),
        EffortLevel.LOW,
    ),
    "trust.secure_connection": (
        AiRemediation(
            title="Serve the site over HTTPS",
            description=(
                "The site is not served over a secure connection. HTTPS is a "
                "baseline trust and security signal."
            ),
            action=(
                "Install a TLS certificate and redirect all HTTP traffic to HTTPS."
            ),
        ),
        EffortLevel.MEDIUM,
    ),
}

# Effort-only view of the AI table, so the engine and _validate read one shape.
EFFORT_BY_SIGNAL: dict[str, EffortLevel] = {
    signal_id: effort for signal_id, (_copy, effort) in AI_REMEDIATION.items()
}


# =============================================================================
# Category mapping: SEO category -> the shared recommendation category. Identity
# for the eight SEO categories; the AI-visibility engine's findings are all filed
# under AI_VISIBILITY by the engine directly.
# =============================================================================
SEO_CATEGORY_MAP: dict[str, RecommendationCategory] = {
    SeoCategory.METADATA.value: RecommendationCategory.METADATA,
    SeoCategory.TECHNICAL.value: RecommendationCategory.TECHNICAL,
    SeoCategory.CONTENT.value: RecommendationCategory.CONTENT,
    SeoCategory.STRUCTURE.value: RecommendationCategory.STRUCTURE,
    SeoCategory.LINKS.value: RecommendationCategory.LINKS,
    SeoCategory.IMAGES.value: RecommendationCategory.IMAGES,
    SeoCategory.SCHEMA.value: RecommendationCategory.SCHEMA,
    SeoCategory.READABILITY.value: RecommendationCategory.READABILITY,
}


def _validate() -> None:
    """Fail-fast at import: the tables must cover every rule and signal exactly.

    Raised as a plain ``RuntimeError`` rather than ``assert`` so it survives
    ``python -O`` -- an unrankable finding must never reach production silently.
    """
    seo_ids = {rule.rule_id for rule in RULES}
    ai_ids = {signal.signal_id for signal in SIGNALS}

    problems: list[str] = []

    missing_effort = seo_ids - EFFORT_BY_RULE.keys()
    if missing_effort:
        problems.append(f"SEO rules with no effort: {sorted(missing_effort)}")
    stale_effort = EFFORT_BY_RULE.keys() - seo_ids
    if stale_effort:
        problems.append(
            f"EFFORT_BY_RULE entries for unknown rules: {sorted(stale_effort)}"
        )

    missing_ai = ai_ids - AI_REMEDIATION.keys()
    if missing_ai:
        problems.append(f"AI signals with no remediation: {sorted(missing_ai)}")
    stale_ai = AI_REMEDIATION.keys() - ai_ids
    if stale_ai:
        problems.append(
            f"AI_REMEDIATION entries for unknown signals: {sorted(stale_ai)}"
        )

    # The dedup key is the raw id; an SEO rule and an AI signal sharing one would
    # be merged into a single recommendation. They are disjoint today; keep them so.
    collision = EFFORT_BY_RULE.keys() & EFFORT_BY_SIGNAL.keys()
    if collision:
        problems.append(f"rule_id / signal_id collision: {sorted(collision)}")

    unmapped = {rule.category.value for rule in RULES} - SEO_CATEGORY_MAP.keys()
    if unmapped:
        problems.append(
            f"SEO categories with no recommendation category: {sorted(unmapped)}"
        )

    if problems:
        raise RuntimeError(
            "recommendation catalog is inconsistent with the engines: "
            + "; ".join(problems)
        )


_validate()


__all__ = [
    "AI_REMEDIATION",
    "EFFORT_BY_RULE",
    "EFFORT_BY_SIGNAL",
    "ENGINE_VERSION",
    "IMPACT_HIGH_MIN",
    "IMPACT_MEDIUM_MIN",
    "SEO_CATEGORY_MAP",
    "AiRemediation",
]
