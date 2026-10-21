"""HTML extraction: one fetched page in, one row's worth of facts out.

This is application logic, not an AI call (spec sections 36/37). Gemini never sees
a page here; every field below is parsed, counted, or computed from a documented
rule, so any value can be re-derived by hand from the HTML.

Two scoping decisions that shape every number, stated up front because they are
easy to get silently wrong:

* **Structural counts cover the whole document.** Headings, images, links, and
  schema blocks are counted across the full page, because that is what a search
  engine or an AI crawler sees. Excluding a nav or a footer would understate the
  real link graph.
* **Text metrics cover the content root only.** ``word_count``,
  ``paragraph_count``, and both readability scores are computed over ``<main>`` /
  ``<article>`` / ``[role=main]`` when one exists, otherwise ``<body>`` with
  ``<nav>``, ``<header>``, ``<footer>``, and ``<aside>`` removed. Site-wide
  boilerplate is identical on every page: leaving it in makes a 60-word service
  page and a 900-word guide look far closer in length than they are, and on short
  pages the footer's address block dominates the readability score outright.
  ``text_excerpt`` holds exactly the text that was measured, so the numbers stay
  checkable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from app.utils.text import (
    flesch_kincaid_grade,
    flesch_reading_ease,
    keyword_occurrences,
    normalize_whitespace,
    word_count,
)
from app.utils.urls import NormalizedURL, normalize_url

# Caps on the JSON detail columns. A page can legitimately carry 400 links; storing
# every one of them for every page of every scan is how a JSON column becomes the
# largest table in the database. The counts above them stay exact -- only the
# per-item detail is capped, and ``*_truncated`` says so.
MAX_STORED_LINKS = 150
MAX_STORED_IMAGES = 100
MAX_STORED_SCHEMA_BLOCKS = 25
MAX_HEADINGS = 100
MAX_EXCERPT_CHARS = 20_000

# Elements that contribute no readable text. ``template`` holds inert markup and
# ``svg`` holds path data that would otherwise be counted as words.
_NON_TEXT_TAGS = ("script", "style", "noscript", "template", "svg", "canvas")
_BOILERPLATE_TAGS = ("nav", "header", "footer", "aside")

_PHONE_RE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Straight plus typographic apostrophe, written as an escape rather than a literal
# so the two are distinguishable in a diff (ruff RUF001).
_APOSTROPHES = "'’"  # noqa: RUF001 - both apostrophe forms are intended here
_BY_LINE_RE = re.compile(
    rf"\b(?:by|written by|author)\s*:?\s+[A-Z][\w{_APOSTROPHES}.-]+", re.I
)

# Social and sharing hosts. An outbound link to one of these is distribution, not
# a citation -- counting them would let a row of share buttons masquerade as
# sourcing, which is exactly the signal AI-visibility scoring is trying to measure.
_SOCIAL_HOSTS = frozenset(
    {
        "facebook.com",
        "twitter.com",
        "x.com",
        "instagram.com",
        "linkedin.com",
        "pinterest.com",
        "tiktok.com",
        "youtube.com",
        "youtu.be",
        "reddit.com",
        "whatsapp.com",
        "t.me",
        "telegram.me",
        "threads.net",
        "snapchat.com",
    }
)

# CMS fingerprints, checked in order. Each is a marker the platform emits itself,
# not a guess from URL shape.
_CMS_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("WordPress", ("/wp-content/", "/wp-includes/", "wp-json")),
    ("Shopify", ("cdn.shopify.com", "shopify-features", "/cdn/shop/")),
    ("Wix", ("static.wixstatic.com", "wix-code", "_wixCssStates")),
    (
        "Squarespace",
        ("static1.squarespace.com", "squarespace-cdn", "Static.SQUARESPACE"),
    ),
    ("Webflow", ("webflow.js", "assets.website-files.com", "wf-domain")),
    ("Drupal", ("/sites/default/files/", "drupal-settings-json", "Drupal.settings")),
    ("Joomla", ("/media/jui/", "joomla-script-options")),
    ("Ghost", ("ghost-sdk", "/ghost/api/")),
    ("HubSpot", ("hs-scripts.com", "hubspot")),
    ("Magento", ("/static/version", "Magento_")),
)


@dataclass(slots=True)
class DiscoveredLink:
    """One href found on the page, already resolved and classified."""

    url: NormalizedURL
    anchor_text: str
    rel: str
    is_internal: bool
    is_nofollow: bool


@dataclass(slots=True)
class PageExtraction:
    """Everything one page contributes to a scan.

    Field names match :class:`~app.models.website.WebsitePage` columns so the
    service layer is a straight copy rather than a mapping table that can drift.
    """

    # metadata
    title: str | None = None
    title_length: int | None = None
    meta_description: str | None = None
    meta_description_length: int | None = None
    canonical_url: str | None = None
    meta_robots: str | None = None
    lang: str | None = None
    has_open_graph: bool = False
    has_twitter_card: bool = False
    has_viewport_meta: bool = False

    # structure
    h1_count: int = 0
    h2_count: int = 0
    h3_count: int = 0
    heading_hierarchy_valid: bool = True
    heading_outline: list[dict[str, Any]] = field(default_factory=list)

    # content
    word_count: int = 0
    paragraph_count: int = 0
    readability_score: float | None = None
    reading_grade_level: float | None = None
    text_to_html_ratio: float | None = None
    published_date: datetime | None = None
    modified_date: datetime | None = None

    # AI-visibility signals
    has_faq_section: bool = False
    faq_question_count: int = 0
    has_list_content: bool = False
    has_table_content: bool = False
    has_author_info: bool = False
    has_contact_info: bool = False
    outbound_citation_count: int = 0

    # images
    image_count: int = 0
    images_missing_alt: int = 0
    images: list[dict[str, Any]] = field(default_factory=list)

    # links
    internal_link_count: int = 0
    external_link_count: int = 0
    broken_link_count: int = 0
    links: list[dict[str, Any]] = field(default_factory=list)

    # schema
    has_schema_markup: bool = False
    schema_types: list[str] = field(default_factory=list)
    schema_blocks: list[dict[str, Any]] = field(default_factory=list)

    keyword_usage: dict[str, Any] = field(default_factory=dict)
    text_excerpt: str | None = None

    # Not stored on the page row: consumed by the crawler's frontier and by the
    # site-level aggregate respectively.
    discovered_links: list[DiscoveredLink] = field(default_factory=list)
    detected_cms: str | None = None
    noindex: bool = False
    nofollow_page: bool = False


def _text_of(tag: Tag | None) -> str:
    if tag is None:
        return ""
    return normalize_whitespace(tag.get_text(" ", strip=True))


def _attr(tag: Tag | None, name: str) -> str | None:
    if tag is None:
        return None
    value = tag.get(name)
    if isinstance(value, list):  # bs4 splits rel/class into lists
        value = " ".join(str(item) for item in value)
    if value is None:
        return None
    cleaned = normalize_whitespace(str(value))
    return cleaned or None


def _meta_content(soup: BeautifulSoup, **criteria: str) -> str | None:
    for key, value in criteria.items():
        # `name` collides with bs4's own tag-name argument, so it is passed via
        # attrs rather than as a keyword.
        tag = soup.find("meta", attrs={key: re.compile(rf"^{re.escape(value)}$", re.I)})
        if isinstance(tag, Tag):
            content = _attr(tag, "content")
            if content:
                return content
    return None


def _parse_date(raw: str | None) -> datetime | None:
    """Parse the date formats that actually appear in ``datetime`` attributes.

    ISO 8601 covers ``article:published_time`` and ``<time datetime>``; a bare
    ``YYYY-MM-DD`` is accepted too. Anything else returns ``None`` rather than a
    guess -- a wrong publication date would later be scored as stale content.
    """
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
        if match is None:
            return None
        try:
            return datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                tzinfo=UTC,
            )
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _content_root(soup: BeautifulSoup) -> Tag:
    """Pick the element whose text represents this page's own content.

    Preference order is ``<main>``, ``<article>``, ``[role=main]``, then ``<body>``
    with site boilerplate removed. See the module docstring for why boilerplate is
    excluded from text metrics but not from structural counts.
    """
    for finder in (
        lambda: soup.find("main"),
        lambda: soup.find("article"),
        lambda: soup.find(attrs={"role": "main"}),
    ):
        candidate = finder()
        if isinstance(candidate, Tag) and word_count(_text_of(candidate)) >= 25:
            # The 25-word floor guards against an empty <main> wrapper around a
            # JS-rendered app, which would otherwise report a 0-word page while
            # the real copy sits in <body>.
            return candidate
    body = soup.body if isinstance(soup.body, Tag) else soup
    for tag_name in _BOILERPLATE_TAGS:
        for element in body.find_all(tag_name):
            element.decompose()
    return body


def _record_schema_types(node: Any, types: list[str]) -> None:
    """Walk a parsed JSON-LD value, appending every distinct ``@type`` seen."""
    if isinstance(node, dict):
        raw = node.get("@type")
        for item in raw if isinstance(raw, list) else [raw]:
            if isinstance(item, str) and item and item not in types:
                types.append(item)
        for value in node.values():
            _record_schema_types(value, types)
    elif isinstance(node, list):
        for item in node:
            _record_schema_types(item, types)


def _extract_jsonld(
    soup: BeautifulSoup, types: list[str], blocks: list[dict[str, Any]]
) -> None:
    for script in soup.find_all(
        "script", attrs={"type": re.compile("ld\\+json", re.I)}
    ):
        raw = script.string or script.get_text()
        if not raw or len(raw) > 200_000:
            continue
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            continue
        _record_schema_types(parsed, types)
        if len(blocks) < MAX_STORED_SCHEMA_BLOCKS:
            blocks.append({"format": "json-ld", "data": parsed})


def _extract_microdata(
    soup: BeautifulSoup, types: list[str], blocks: list[dict[str, Any]]
) -> None:
    for element in soup.find_all(attrs={"itemtype": True}):
        itemtype = _attr(element, "itemtype") or ""
        # schema.org/Recipe -> Recipe
        name = itemtype.rstrip("/").rsplit("/", 1)[-1]
        if name and name not in types:
            types.append(name)
            if len(blocks) < MAX_STORED_SCHEMA_BLOCKS:
                blocks.append({"format": "microdata", "data": {"@type": name}})


def _extract_schema(soup: BeautifulSoup) -> tuple[list[str], list[dict[str, Any]]]:
    """Parse JSON-LD plus microdata ``itemtype`` values.

    JSON-LD from a third party is untrusted input: it is size-capped, parsed with
    ``json.loads`` only, and never evaluated. A malformed block is skipped rather
    than failing the page -- broken JSON-LD is extremely common and the rest of the
    page is still analysable.
    """
    types: list[str] = []
    blocks: list[dict[str, Any]] = []
    _extract_jsonld(soup, types, blocks)
    _extract_microdata(soup, types, blocks)
    return types, blocks


def _count_faq(soup: BeautifulSoup, schema_types: list[str]) -> tuple[bool, int]:
    """Detect a Q&A section and count its questions.

    Three independent signals, because sites express FAQs in three different ways
    and any single check misses most of them:

    1. ``FAQPage`` / ``QAPage`` schema, which is explicit.
    2. ``<details><summary>`` accordions, the modern semantic form.
    3. Headings that end in a question mark, the plain-HTML form.

    The count is the largest of the three rather than their sum: a well-marked-up
    FAQ satisfies all three at once, and adding them would triple-count it.
    """
    schema_lower = {item.lower() for item in schema_types}
    has_schema = bool(schema_lower & {"faqpage", "qapage"})

    schema_questions = 0
    if has_schema:
        for script in soup.find_all(
            "script", attrs={"type": re.compile("ld\\+json", re.I)}
        ):
            raw = script.string or script.get_text() or ""
            schema_questions += len(re.findall(r'"@type"\s*:\s*"Question"', raw))

    summaries = [
        summary
        for summary in soup.find_all("summary")
        if "?" in _text_of(summary) or len(_text_of(summary)) > 8
    ]

    heading_questions = [
        heading
        for heading in soup.find_all(["h2", "h3", "h4"])
        if _text_of(heading).endswith("?")
    ]

    # An accordion alone is not proof of an FAQ -- it is also how sites build
    # product tabs -- so it only counts when paired with question-shaped text.
    accordion_questions = len([s for s in summaries if "?" in _text_of(s)])

    count = max(schema_questions, accordion_questions, len(heading_questions))
    found = has_schema or count >= 2
    return found, count if found else 0


def _detect_cms(html: str, soup: BeautifulSoup) -> str | None:
    generator = _meta_content(soup, name="generator")
    if generator:
        lowered = generator.lower()
        for name, _markers in _CMS_MARKERS:
            if name.lower() in lowered:
                return name
        # A generator we do not have a fingerprint for is still the site's own
        # claim about itself, so report it rather than "unknown".
        return generator[:80]
    haystack = html[:400_000]
    for name, markers in _CMS_MARKERS:
        if any(marker in haystack for marker in markers):
            return name
    return None


def _heading_outline(soup: BeautifulSoup) -> tuple[list[dict[str, Any]], bool]:
    """Build the heading tree and judge whether the hierarchy is sound.

    Invalid means any of: no ``h1``, more than one ``h1``, or a level skipped on
    the way down (``h2`` straight to ``h4``). Each of those is a real
    accessibility and machine-readability problem, and each maps to its own
    ``rule_id`` in the SEO engine -- this function only reports the boolean, the
    scoring stage attributes the deduction.
    """
    outline: list[dict[str, Any]] = []
    levels: list[int] = []
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        level = int(heading.name[1])
        text = _text_of(heading)
        levels.append(level)
        if len(outline) < MAX_HEADINGS:
            outline.append({"level": level, "text": text[:300]})

    h1_total = levels.count(1)
    valid = h1_total == 1
    previous = 0
    for level in levels:
        if previous and level > previous + 1:
            valid = False
            break
        previous = level
    return outline, valid


def _extract_metadata(
    soup: BeautifulSoup, result: PageExtraction, page_url: NormalizedURL
) -> None:
    """Title, meta description, canonical, meta-robots, lang, and social cards."""
    title_tag = soup.find("title")
    title = _text_of(title_tag) if isinstance(title_tag, Tag) else ""
    if title:
        result.title = title[:1024]
        result.title_length = len(title)

    description = _meta_content(soup, name="description") or _meta_content(
        soup, property="og:description"
    )
    if description:
        result.meta_description = description
        result.meta_description_length = len(description)

    canonical = soup.find("link", attrs={"rel": re.compile("^canonical$", re.I)})
    href = _attr(canonical, "href") if isinstance(canonical, Tag) else None
    if href:
        result.canonical_url = urljoin(page_url.url, href)[:2048]

    robots = _meta_content(soup, name="robots")
    if robots:
        result.meta_robots = robots[:200]
        directives = {part.strip().lower() for part in robots.split(",")}
        result.noindex = "noindex" in directives or "none" in directives
        result.nofollow_page = "nofollow" in directives or "none" in directives

    html_tag = soup.find("html")
    lang = _attr(html_tag, "lang") if isinstance(html_tag, Tag) else None
    result.lang = lang[:20] if lang else None

    result.has_open_graph = (
        soup.find("meta", attrs={"property": re.compile("^og:", re.I)}) is not None
    )
    result.has_twitter_card = (
        soup.find("meta", attrs={"name": re.compile("^twitter:", re.I)}) is not None
    )
    result.has_viewport_meta = (
        soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)}) is not None
    )


def _extract_dates(soup: BeautifulSoup) -> tuple[datetime | None, datetime | None]:
    """Published and modified dates from meta tags, ``<time>``, then JSON-LD.

    The order is a confidence order: an explicit ``article:published_time`` is
    more trustworthy than a date scraped out of a JSON-LD blob, so the meta tags
    win and JSON-LD only fills a gap they left.
    """
    published = _parse_date(
        _meta_content(soup, property="article:published_time")
        or _meta_content(soup, name="date")
        or _meta_content(soup, itemprop="datePublished")
    )
    modified = _parse_date(
        _meta_content(soup, property="article:modified_time")
        or _meta_content(soup, itemprop="dateModified")
    )
    if published is None:
        time_tag = soup.find("time", attrs={"datetime": True})
        if isinstance(time_tag, Tag):
            published = _parse_date(_attr(time_tag, "datetime"))
    if published is not None and modified is not None:
        return published, modified

    for script in soup.find_all(
        "script", attrs={"type": re.compile("ld\\+json", re.I)}
    ):
        raw = script.string or script.get_text() or ""
        if published is None:
            match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', raw)
            published = _parse_date(match.group(1) if match else None)
        if modified is None:
            match = re.search(r'"dateModified"\s*:\s*"([^"]+)"', raw)
            modified = _parse_date(match.group(1) if match else None)
    return published, modified


def _extract_images(
    soup: BeautifulSoup, result: PageExtraction, page_url: NormalizedURL
) -> None:
    """Count images and record which are missing an ``alt`` attribute."""
    image_tags = soup.find_all("img")
    result.image_count = len(image_tags)
    for image in image_tags:
        src = _attr(image, "src") or _attr(image, "data-src")
        alt_raw = image.get("alt")
        # Absent alt and empty alt are different: empty is the correct markup for
        # a decorative image, absent means nobody considered the question. Only
        # absent counts as missing.
        missing = alt_raw is None
        if missing:
            result.images_missing_alt += 1
        if len(result.images) < MAX_STORED_IMAGES:
            result.images.append(
                {
                    "src": urljoin(page_url.url, src)[:2048] if src else None,
                    "alt": normalize_whitespace(str(alt_raw))[:300]
                    if alt_raw is not None
                    else None,
                    "missing_alt": missing,
                    "loading": _attr(image, "loading"),
                    "width": _attr(image, "width"),
                    "height": _attr(image, "height"),
                }
            )


def _extract_links(
    soup: BeautifulSoup,
    result: PageExtraction,
    *,
    page_url: NormalizedURL,
    site_domain: str,
    allow_private_hosts: bool,
) -> None:
    """Resolve every anchor, classify internal/external, and count citations."""
    seen_internal: set[str] = set()
    seen_external: set[str] = set()
    citation_domains: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        raw_href = _attr(anchor, "href")
        if not raw_href:
            continue
        # Not navigation: in-page anchors and non-http schemes.
        if raw_href.lower().startswith(
            ("#", "mailto:", "tel:", "javascript:", "data:", "sms:")
        ):
            continue
        try:
            resolved = normalize_url(
                urljoin(page_url.url, raw_href), allow_private_hosts=allow_private_hosts
            )
        except Exception:  # noqa: BLE001, S112 - a broken href is a page fact
            continue

        rel = (_attr(anchor, "rel") or "").lower()
        is_nofollow = "nofollow" in rel
        is_internal = bool(site_domain) and resolved.registrable_domain == site_domain

        if is_internal:
            seen_internal.add(resolved.normalized)
        else:
            seen_external.add(resolved.normalized)
            host_domain = resolved.registrable_domain
            if (
                host_domain
                and host_domain not in _SOCIAL_HOSTS
                and not is_nofollow
                and "sponsored" not in rel
                and "ugc" not in rel
            ):
                citation_domains.add(host_domain)

        link = DiscoveredLink(
            url=resolved,
            anchor_text=_text_of(anchor)[:300],
            rel=rel,
            is_internal=is_internal,
            is_nofollow=is_nofollow,
        )
        result.discovered_links.append(link)
        if len(result.links) < MAX_STORED_LINKS:
            result.links.append(
                {
                    "url": resolved.url,
                    "anchor": link.anchor_text,
                    "internal": is_internal,
                    "nofollow": is_nofollow,
                    "rel": rel or None,
                }
            )

    # Distinct destinations, not raw anchor count: a nav repeated in a mobile menu
    # would otherwise double every internal link total on the site.
    result.internal_link_count = len(seen_internal)
    result.external_link_count = len(seen_external)
    # Distinct *domains*, so ten footnotes to one source count as one citation.
    result.outbound_citation_count = len(citation_domains)


def _extract_ai_signals(soup: BeautifulSoup, result: PageExtraction) -> None:
    """FAQ, lists, tables, author, and contact signals used by AI-visibility."""
    result.has_faq_section, result.faq_question_count = _count_faq(
        soup, result.schema_types
    )
    # Two list items is a list; one is a stray bullet.
    result.has_list_content = any(
        len(container.find_all("li", recursive=False)) >= 2
        for container in soup.find_all(["ul", "ol"])
    )
    result.has_table_content = any(
        table.find("tr") is not None for table in soup.find_all("table")
    )

    schema_lower = {item.lower() for item in result.schema_types}
    page_text_full = soup.get_text(" ", strip=True)
    result.has_author_info = bool(
        {"person", "author"} & schema_lower
        or soup.find(attrs={"rel": re.compile("author", re.I)})
        or soup.find(attrs={"itemprop": re.compile("^author$", re.I)})
        or soup.find(class_=re.compile(r"\bauthor\b|\bbyline\b", re.I))
        or _BY_LINE_RE.search(page_text_full[:5000])
    )
    result.has_contact_info = bool(
        soup.find("a", href=re.compile("^mailto:", re.I))
        or soup.find("a", href=re.compile("^tel:", re.I))
        or {"contactpoint", "postaladdress", "localbusiness", "organization"}
        & schema_lower
        or _EMAIL_RE.search(page_text_full)
        or _PHONE_RE.search(page_text_full)
    )


def extract_page(
    html: str,
    *,
    page_url: NormalizedURL,
    site_domain: str,
    keywords: list[str] | None = None,
    allow_private_hosts: bool = False,
) -> PageExtraction:
    """Parse one HTML document into a :class:`PageExtraction`.

    *site_domain* is the registrable domain of the website being scanned, which is
    what makes a link internal or external -- ``blog.example.com`` counts as
    internal for ``example.com``, matching the crawler's own same-site rule.
    """
    # lxml, not html.parser: real pages have unclosed tags, stray `</p>`, and
    # mis-nested tables, and html.parser silently drops content around them.
    soup = BeautifulSoup(html, "lxml")
    result = PageExtraction()

    _extract_metadata(soup, result, page_url)

    # schema first: dates and AI signals both read the parsed @type list.
    result.schema_types, result.schema_blocks = _extract_schema(soup)
    result.has_schema_markup = bool(result.schema_types)

    result.published_date, result.modified_date = _extract_dates(soup)

    result.heading_outline, result.heading_hierarchy_valid = _heading_outline(soup)
    result.h1_count = len(soup.find_all("h1"))
    result.h2_count = len(soup.find_all("h2"))
    result.h3_count = len(soup.find_all("h3"))

    _extract_images(soup, result, page_url)
    _extract_links(
        soup,
        result,
        page_url=page_url,
        site_domain=site_domain,
        allow_private_hosts=allow_private_hosts,
    )
    _extract_ai_signals(soup, result)

    result.detected_cms = _detect_cms(html, soup)

    # ------------------------------------------------------------- text
    # Done last: _content_root mutates the tree by removing boilerplate, so every
    # whole-document count above must already have been taken.
    root = _content_root(soup)
    for tag_name in _NON_TEXT_TAGS:
        for element in root.find_all(tag_name):
            element.decompose()

    content_text = normalize_whitespace(root.get_text(" ", strip=True))
    result.word_count = word_count(content_text)
    result.paragraph_count = len(
        [p for p in root.find_all("p") if word_count(_text_of(p)) >= 3]
    )
    result.readability_score = flesch_reading_ease(content_text)
    result.reading_grade_level = flesch_kincaid_grade(content_text)
    if html:
        # Share of the payload that is actual copy. A very low ratio means a
        # JS-heavy page an AI crawler will read as almost empty.
        result.text_to_html_ratio = round(len(content_text) / len(html), 4)
    result.text_excerpt = content_text[:MAX_EXCERPT_CHARS] or None
    result.keyword_usage = keyword_occurrences(content_text, keywords or [])

    return result


__all__ = [
    "MAX_EXCERPT_CHARS",
    "MAX_STORED_IMAGES",
    "MAX_STORED_LINKS",
    "DiscoveredLink",
    "PageExtraction",
    "extract_page",
]
