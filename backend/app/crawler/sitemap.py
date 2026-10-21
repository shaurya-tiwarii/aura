"""Sitemap discovery and parsing.

A sitemap is the site owner telling us which pages matter, so honouring it makes
a 20-page crawl land on 20 *useful* pages instead of 20 pagination stubs. It is
also a real SEO signal in its own right -- ``sitemap_found`` and
``sitemap_url_count`` are recorded on the website row and scored later.

Three hazards this handles explicitly:

* **A sitemap index is a tree.** ``<sitemapindex>`` points at more sitemaps, which
  may point at more. Depth and total-document counts are capped, or one hostile
  file can make the crawler walk forever without ever fetching a page.
* **Sitemaps are routinely gzipped**, and just as routinely served with the wrong
  ``Content-Type``. Detection is by magic bytes, not by header or file extension.
* **XML from an untrusted source is an attack surface.** ``lxml`` is configured
  with entity resolution and network access off, which is what blocks XXE and the
  billion-laughs expansion. Parsing untrusted XML with default settings is the
  single most common way a crawler becomes an SSRF vector for the *parser* rather
  than the fetcher.
"""

from __future__ import annotations

import gzip
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin

from lxml import etree

from app.core.logging import get_logger
from app.utils.urls import NormalizedURL, normalize_url

log = get_logger(__name__)

# A sitemap index may nest, but not deeply -- two levels is what the protocol
# describes and what real sites use.
MAX_SITEMAP_DEPTH = 2
# Ceiling on documents fetched while walking an index, independent of depth.
MAX_SITEMAP_DOCUMENTS = 10
# Ceiling on URLs collected. The crawl cap is far lower (25 by default), so this
# only exists to bound memory when a site publishes a 50,000-entry sitemap.
MAX_SITEMAP_URLS = 5000

# Conventional locations to try when robots.txt names none.
DEFAULT_SITEMAP_PATHS = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/wp-sitemap.xml",  # WordPress 5.5+ core sitemaps
    "/sitemap.xml.gz",
)

_GZIP_MAGIC = b"\x1f\x8b"
_LASTMOD_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


@dataclass(frozen=True, slots=True)
class SitemapEntry:
    url: NormalizedURL
    lastmod: datetime | None = None


@dataclass(slots=True)
class SitemapResult:
    """What sitemap discovery found, including how it found it.

    ``documents`` and ``errors`` are kept so the scan can report "we read your
    sitemap index and two of the three child sitemaps returned 404" rather than
    silently under-counting.
    """

    found: bool = False
    root_url: str | None = None
    entries: list[SitemapEntry] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def url_count(self) -> int:
        return len(self.entries)


def _strip_namespace(tag: object) -> str:
    """``{http://…/sitemap/0.9}loc`` -> ``loc``.

    Sitemaps in the wild use the correct namespace, the wrong namespace, and no
    namespace. Matching on the local name accepts all three; matching on the fully
    qualified name would silently return zero URLs for the last two.
    """
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _decompress(body: bytes) -> bytes:
    """Gunzip when the bytes are gzip, whatever the server claimed."""
    if not body.startswith(_GZIP_MAGIC):
        return body
    try:
        return gzip.decompress(body)
    except (OSError, EOFError, gzip.BadGzipFile):
        # Truncated by the fetcher's byte cap, most likely. Returning the raw
        # bytes lets the XML parser fail with a clearer message than a gzip error.
        return body


def _parse_lastmod(raw: str | None) -> datetime | None:
    """Read the date part of a W3C datetime, ignoring time and offset.

    Only the date is used: sitemap ``lastmod`` values are frequently the build
    timestamp rather than a real content change, so pretending to minute precision
    would overstate what we know.
    """
    if not raw:
        return None
    match = _LASTMOD_RE.match(raw.strip())
    if match is None:
        return None
    try:
        return datetime(
            int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=UTC
        )
    except ValueError:
        return None


def _xml_parser() -> etree.XMLParser:
    """A parser that cannot be turned against us.

    ``resolve_entities=False`` stops XXE (``<!ENTITY x SYSTEM "file:///etc/passwd">``)
    and blocks the billion-laughs expansion; ``no_network=True`` stops the parser
    from making its own HTTP requests, which would bypass
    :class:`~app.crawler.fetcher.SafeFetcher` entirely; ``huge_tree=False`` keeps
    the depth and node-count limits on. ``recover=True`` is on because real
    sitemaps have stray ampersands and we want the 400 valid entries rather than a
    parse error.
    """
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        huge_tree=False,
        recover=True,
        load_dtd=False,
        dtd_validation=False,
    )


def _element_loc_lastmod(element: object) -> tuple[str | None, str | None]:
    """Pull ``<loc>`` and ``<lastmod>`` text out of one ``<url>``/``<sitemap>``."""
    loc: str | None = None
    lastmod: str | None = None
    for child in element:  # type: ignore[attr-defined]
        child_name = _strip_namespace(child.tag)
        if child_name == "loc" and child.text:
            loc = child.text.strip()
        elif child_name == "lastmod" and child.text:
            lastmod = child.text.strip()
    return loc, lastmod


def parse_sitemap_document(
    body: bytes, *, base_url: str, allow_private_hosts: bool = False
) -> tuple[list[SitemapEntry], list[str]]:
    """Parse one sitemap document.

    Returns ``(page entries, child sitemap URLs)``. Exactly one of the two is
    normally non-empty: a ``<urlset>`` yields pages, a ``<sitemapindex>`` yields
    children. Both are returned rather than branching on the root tag, because
    some generators emit a hybrid and the caller can handle whatever came back.
    """
    payload = _decompress(body)
    if not payload.strip():
        return [], []

    try:
        root = etree.fromstring(payload, parser=_xml_parser())  # noqa: S320
    except etree.XMLSyntaxError as exc:
        raise ValueError(f"Not valid XML: {exc.msg}") from exc
    if root is None:
        raise ValueError("Sitemap document was empty.")

    root_name = _strip_namespace(root.tag)
    entries: list[SitemapEntry] = []
    children: list[str] = []

    for element in root:
        name = _strip_namespace(element.tag)
        if name not in {"url", "sitemap"}:
            continue

        loc, lastmod = _element_loc_lastmod(element)
        if not loc:
            continue
        # <loc> is required to be absolute, but generators do emit relative paths.
        # Resolving against the document's own URL costs nothing and recovers those
        # entries instead of discarding them below as unparseable.
        absolute = urljoin(base_url, loc)
        if name == "sitemap":
            children.append(absolute)
            continue

        try:
            normalized = normalize_url(
                absolute, allow_private_hosts=allow_private_hosts
            )
        except Exception:  # noqa: BLE001, S112 - one bad <loc> must not void the map
            # Sitemaps contain stale absolute URLs, mailto links, and occasionally
            # a private hostname. Skipping the entry is right; failing the document
            # would discard every valid URL alongside it.
            continue
        entries.append(SitemapEntry(url=normalized, lastmod=_parse_lastmod(lastmod)))

    if root_name not in {"urlset", "sitemapindex"} and not entries and not children:
        raise ValueError(f"Root element is <{root_name}>, not a sitemap.")
    return entries, children


__all__ = [
    "DEFAULT_SITEMAP_PATHS",
    "MAX_SITEMAP_DEPTH",
    "MAX_SITEMAP_DOCUMENTS",
    "MAX_SITEMAP_URLS",
    "SitemapEntry",
    "SitemapResult",
    "parse_sitemap_document",
]
