"""The crawl orchestrator: robots.txt, sitemaps, then a bounded BFS walk.

This is the piece that turns one website URL into a set of analysed pages plus a
site-level picture. It owns sequencing and nothing else -- fetching is
:mod:`app.crawler.fetcher`, scoping is :mod:`app.crawler.frontier`, parsing is
:mod:`app.crawler.extract` -- so the order of operations can be read in one place
and each part stays testable on its own.

Sequence, and why it is this order
----------------------------------
1. **robots.txt first, always.** Before any page request. A crawler that fetches
   the homepage and *then* checks permission has already been impolite (spec
   section 21). It also yields the ``Sitemap:`` directives for step 2 and the
   ``Crawl-delay`` for the whole run, so doing it first is free.
2. **Sitemaps next.** The owner's own list of pages that matter beats link
   discovery for a 20-page budget. Conventional paths are only tried when
   robots.txt names none, so a site with a declared sitemap costs one request
   rather than five 404s.
3. **BFS from the homepage**, with sitemap URLs seeded at depth 0.

Nothing here raises on a site problem. A 500, a timeout, a disallowed path, and a
missing sitemap are all *findings* recorded on the result. The only exceptions that
escape are programming errors and a URL AURA must refuse to fetch at all.

Concurrency, honestly
---------------------
Pages are fetched in waves of ``max_concurrency`` rather than through a worker
pool. Waves are strictly breadth-first and deterministic, which makes a scan
reproducible; a pool would finish marginally sooner on a site with a few slow
pages. The cost is small because :meth:`SafeFetcher._wait_turn` already spaces
request *starts* by the politeness delay, so the wave's requests are staggered
regardless. HTML parsing runs inline on the event loop: lxml handles a typical
page in single-digit milliseconds, which is well under the delay between requests,
so moving it to a thread would add complexity and buy nothing at this scale.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.crawler.extract import PageExtraction, extract_page
from app.crawler.fetcher import (
    XML_CONTENT_TYPES,
    FetchResult,
    LinkCheckResult,
    SafeFetcher,
)
from app.crawler.frontier import Frontier, FrontierItem
from app.crawler.robots import MAX_ROBOTS_BYTES, RobotsFile
from app.crawler.sitemap import (
    DEFAULT_SITEMAP_PATHS,
    MAX_SITEMAP_DEPTH,
    MAX_SITEMAP_DOCUMENTS,
    MAX_SITEMAP_URLS,
    SitemapEntry,
    SitemapResult,
    parse_sitemap_document,
)
from app.models.enums import PageFetchStatus
from app.utils.urls import NormalizedURL, normalize_url

log = get_logger(__name__)

# Progress is reported as a percentage so the frontend can show the staged loading
# states from spec section 27. The crawl owns 0-70; scoring stages own the rest.
# Link verification runs after the last page and reports at the crawl budget's
# end so the crawler never spends the scoring stages' percentage points.
PROGRESS_ROBOTS = 5
PROGRESS_SITEMAP = 12
PROGRESS_CRAWL_START = 15
PROGRESS_CRAWL_END = 70
PROGRESS_LINKS = 70

# A progress callback that may be async or sync, or absent.
ProgressCallback = Callable[[int, str], Awaitable[None] | None]


@dataclass(slots=True)
class RobotsRecord:
    """What robots.txt said, kept for the scan's audit trail."""

    found: bool = False
    http_status: int | None = None
    source: str = "not_fetched"
    matched_agent: str | None = None
    crawl_delay: float | None = None
    delay_clamped: bool = False
    sitemap_urls: list[str] = field(default_factory=list)
    homepage_allowed: bool = True
    respected: bool = True
    truncated: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "http_status": self.http_status,
            "source": self.source,
            "matched_agent": self.matched_agent,
            "crawl_delay": self.crawl_delay,
            "delay_clamped": self.delay_clamped,
            "sitemap_urls": self.sitemap_urls[:20],
            "homepage_allowed": self.homepage_allowed,
            "respected": self.respected,
            "truncated": self.truncated,
            "error": self.error,
        }


@dataclass(slots=True)
class _SitemapDoc:
    """The outcome of reading one sitemap URL, so the caller branches once.

    ``parsed`` is the single flag that says "this produced usable content";
    everything else is detail. Kept private to this module -- it exists only to
    keep :meth:`Crawler._load_sitemaps` under the complexity budget.
    """

    final_url: str | None = None
    entries: list[SitemapEntry] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    error: str | None = None
    parsed: bool = False


@dataclass(slots=True)
class CrawledPage:
    """One fetched URL and, when it returned HTML, what was extracted from it."""

    url: NormalizedURL
    depth: int
    is_homepage: bool
    discovered_via: str
    fetch: FetchResult
    extraction: PageExtraction | None = None

    @property
    def ok(self) -> bool:
        return self.extraction is not None


@dataclass(slots=True)
class CrawlResult:
    """Everything one crawl produced. Consumed by :mod:`app.services.scan`."""

    website_url: NormalizedURL
    site_domain: str
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    pages: list[CrawledPage] = field(default_factory=list)
    robots: RobotsRecord = field(default_factory=RobotsRecord)
    sitemap: SitemapResult = field(default_factory=SitemapResult)
    frontier_report: dict[str, Any] = field(default_factory=dict)
    detected_cms: str | None = None
    hit_page_limit: bool = False
    errors: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------- summaries
    @property
    def pages_crawled(self) -> int:
        """Pages that returned parseable HTML. The denominator for every average."""
        return sum(1 for page in self.pages if page.ok)

    @property
    def pages_failed(self) -> int:
        return sum(1 for page in self.pages if not page.ok)

    @property
    def homepage(self) -> CrawledPage | None:
        for page in self.pages:
            if page.is_homepage:
                return page
        return None

    @property
    def blocked_by_robots(self) -> bool:
        """True when robots.txt forbade the homepage, so nothing was crawled."""
        return not self.robots.homepage_allowed

    def site_summary(self) -> dict[str, Any]:
        """Site-level aggregate stored on ``WebsiteScan.site_analysis``.

        Every average is over pages that actually returned HTML, and
        ``pages_analysed`` is included so the denominator is visible rather than
        assumed -- an average over 3 of 20 pages is a different claim from an
        average over all 25 (spec section 24).
        """
        analysed = [page for page in self.pages if page.extraction is not None]
        extractions = [page.extraction for page in analysed if page.extraction]

        def average(values: list[float]) -> float | None:
            return round(sum(values) / len(values), 1) if values else None

        readability = [e.readability_score for e in extractions if e.readability_score]
        grades = [e.reading_grade_level for e in extractions if e.reading_grade_level]
        word_counts = [e.word_count for e in extractions]

        schema_types: dict[str, int] = {}
        for extraction in extractions:
            for name in extraction.schema_types:
                schema_types[name] = schema_types.get(name, 0) + 1

        status_counts: dict[str, int] = {}
        for page in self.pages:
            key = page.fetch.status.value
            status_counts[key] = status_counts.get(key, 0) + 1

        return {
            "pages_analysed": len(analysed),
            "pages_attempted": len(self.pages),
            "fetch_status_counts": dict(sorted(status_counts.items())),
            "total_words": sum(word_counts),
            "avg_words_per_page": average([float(v) for v in word_counts]),
            "avg_readability": average([float(v) for v in readability]),
            "avg_reading_grade": average([float(v) for v in grades]),
            "pages_with_title": sum(1 for e in extractions if e.title),
            "pages_missing_title": sum(1 for e in extractions if not e.title),
            "pages_with_meta_description": sum(
                1 for e in extractions if e.meta_description
            ),
            "pages_missing_meta_description": sum(
                1 for e in extractions if not e.meta_description
            ),
            "pages_with_h1": sum(1 for e in extractions if e.h1_count == 1),
            "pages_with_multiple_h1": sum(1 for e in extractions if e.h1_count > 1),
            "pages_missing_h1": sum(1 for e in extractions if e.h1_count == 0),
            "pages_with_schema": sum(1 for e in extractions if e.has_schema_markup),
            "pages_with_faq": sum(1 for e in extractions if e.has_faq_section),
            "pages_with_author": sum(1 for e in extractions if e.has_author_info),
            "pages_with_contact": sum(1 for e in extractions if e.has_contact_info),
            "pages_noindex": sum(1 for e in extractions if e.noindex),
            "total_images": sum(e.image_count for e in extractions),
            "images_missing_alt": sum(e.images_missing_alt for e in extractions),
            "total_internal_links": sum(e.internal_link_count for e in extractions),
            "total_external_links": sum(e.external_link_count for e in extractions),
            "outbound_citation_domains": sorted(
                self.frontier_report.get("external_domains", [])  # type: ignore[arg-type]
            )[:50],
            "schema_type_counts": dict(sorted(schema_types.items())),
            "detected_cms": self.detected_cms,
            "robots": self.robots.as_dict(),
            "sitemap": {
                "found": self.sitemap.found,
                "root_url": self.sitemap.root_url,
                "url_count": self.sitemap.url_count,
                "documents": self.sitemap.documents[:20],
                "errors": self.sitemap.errors[:20],
                "truncated": self.sitemap.truncated,
            },
            "frontier": self.frontier_report,
            "hit_page_limit": self.hit_page_limit,
            "crawl_errors": self.errors[:20],
        }


class Crawler:
    """Crawls one website. Not reusable across sites -- construct one per scan."""

    __slots__ = (
        "_allow_private",
        "_delay",
        "_max_concurrency",
        "_max_depth",
        "_max_pages",
        "_progress",
        "_respect_robots",
        "_timeout",
        "_use_sitemap",
        "_user_agent",
    )

    def __init__(
        self,
        *,
        max_pages: int | None = None,
        max_depth: int | None = None,
        timeout_seconds: float | None = None,
        delay_seconds: float | None = None,
        max_concurrency: int | None = None,
        respect_robots: bool | None = None,
        use_sitemap: bool = True,
        user_agent: str | None = None,
        allow_private_hosts: bool | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        self._max_pages = max_pages or settings.crawler_max_pages
        self._max_depth = settings.crawler_max_depth if max_depth is None else max_depth
        self._timeout = timeout_seconds
        self._delay = delay_seconds
        self._max_concurrency = max_concurrency or settings.crawler_max_concurrency
        self._respect_robots = (
            settings.crawler_respect_robots
            if respect_robots is None
            else respect_robots
        )
        self._use_sitemap = use_sitemap
        self._user_agent = user_agent or settings.crawler_user_agent
        self._allow_private = (
            settings.crawler_allow_private_hosts
            if allow_private_hosts is None
            else allow_private_hosts
        )
        self._progress = progress

    # ------------------------------------------------------------------ helpers
    async def _report(self, percent: int, message: str) -> None:
        if self._progress is None:
            return
        outcome = self._progress(percent, message)
        if outcome is not None:
            await outcome

    def _config(self) -> dict[str, Any]:
        """The settings this run used, stored on the scan row.

        Recorded because a page count is only meaningful next to the cap that
        produced it: "12 pages" from a 20-page budget means the site is small,
        while "20 pages" means the crawl was truncated.
        """
        return {
            "max_pages": self._max_pages,
            "max_depth": self._max_depth,
            "max_concurrency": self._max_concurrency,
            "delay_seconds": (
                settings.crawler_delay_seconds if self._delay is None else self._delay
            ),
            "timeout_seconds": (
                settings.crawler_timeout_seconds
                if self._timeout is None
                else self._timeout
            ),
            "respect_robots": self._respect_robots,
            "use_sitemap": self._use_sitemap,
            "user_agent": self._user_agent,
            "engine": "aura-crawler/1",
        }

    # -------------------------------------------------------------------- robots
    async def _load_robots(
        self, fetcher: SafeFetcher, origin: str
    ) -> tuple[RobotsFile, RobotsRecord]:
        record = RobotsRecord()
        if not self._respect_robots:
            # Only reachable when an operator explicitly disables it in settings.
            # Recorded on the scan so the report never implies robots.txt was
            # honoured when it was not.
            record.source = "disabled_by_configuration"
            record.respected = False
            return RobotsFile.allow_all(reason="disabled"), record

        robots_url = f"{origin}/robots.txt"
        # accept_types=() because servers serve robots.txt as text/plain,
        # application/octet-stream, and occasionally text/html; the content type
        # tells us nothing useful here.
        result = await fetcher.fetch(robots_url, accept_types=())
        record.http_status = result.http_status

        if result.status is PageFetchStatus.OK or result.status is (
            PageFetchStatus.REDIRECTED
        ):
            body = result.body[:MAX_ROBOTS_BYTES]
            record.truncated = len(result.body) > MAX_ROBOTS_BYTES or result.truncated
            robots = RobotsFile.parse(
                body.decode("utf-8", errors="replace"), user_agent=self._user_agent
            )
            record.found = True
            record.source = "fetched"
        elif result.status is PageFetchStatus.NOT_FOUND:
            # RFC 9309 section 2.3.1.2: 404 means unrestricted access.
            robots = RobotsFile.allow_all(reason="not_found")
            record.source = "not_found"
        elif result.status is PageFetchStatus.BLOCKED:
            # RFC 9309 section 2.3.1.3: 401/403 means the whole site is off limits.
            robots = RobotsFile.disallow_all(reason="forbidden")
            record.source = "forbidden"
            record.error = result.error
        else:
            # Timeout, connection error, or 5xx. RFC 9309 says a crawler MAY treat
            # this as a full disallow. We do not, deliberately: this scan was
            # started by the site's own owner, and returning an empty report
            # because their server hiccuped on one file would be useless to them.
            # The deviation is recorded rather than hidden.
            robots = RobotsFile.allow_all(reason="unavailable")
            record.source = "unavailable"
            record.error = result.error

        record.matched_agent = robots.matched_agent()
        record.crawl_delay = robots.crawl_delay
        record.delay_clamped = robots.delay_clamped
        record.sitemap_urls = list(robots.sitemaps)
        return robots, record

    # ------------------------------------------------------------------ sitemaps
    async def _load_sitemaps(
        self,
        fetcher: SafeFetcher,
        *,
        origin: str,
        declared: list[str],
        site_domain: str,
    ) -> SitemapResult:
        result = SitemapResult()
        if not self._use_sitemap:
            return result

        # robots.txt declarations first; conventional paths only as a fallback, so a
        # site that declares its sitemap is not probed five more times.
        candidates: list[tuple[str, int]] = [(url, 0) for url in declared[:5]]
        if not candidates:
            candidates = [(f"{origin}{path}", 0) for path in DEFAULT_SITEMAP_PATHS]

        seen: set[str] = set()
        documents_fetched = 0
        queue = list(candidates)
        probing_defaults = not declared

        while queue and documents_fetched < MAX_SITEMAP_DOCUMENTS:
            raw_url, depth = queue.pop(0)
            if depth > MAX_SITEMAP_DEPTH:
                result.errors.append(f"Nesting deeper than {MAX_SITEMAP_DEPTH} levels.")
                continue

            doc = await self._read_sitemap_document(
                fetcher,
                raw_url,
                seen=seen,
                site_domain=site_domain,
                probing_defaults=probing_defaults,
            )
            if doc.error:
                result.errors.append(doc.error)
            if not doc.parsed:
                continue
            documents_fetched += 1
            queue, probing_defaults = _merge_sitemap_doc(
                result, doc, raw_url, queue, depth, probing_defaults=probing_defaults
            )

        if documents_fetched >= MAX_SITEMAP_DOCUMENTS and queue:
            result.truncated = True
            result.errors.append(
                f"Stopped after {MAX_SITEMAP_DOCUMENTS} sitemap documents."
            )
        return result

    async def _read_sitemap_document(
        self,
        fetcher: SafeFetcher,
        raw_url: str,
        *,
        seen: set[str],
        site_domain: str,
        probing_defaults: bool,
    ) -> _SitemapDoc:
        """Normalise, fetch, and parse one sitemap URL. Never raises.

        ``parsed`` is False for every skip -- a duplicate, an off-site URL, a fetch
        failure, or invalid XML -- so the caller can treat "nothing usable came
        back" uniformly. A 404 while probing conventional paths is silent; every
        other failure carries an ``error`` string for the scan report.
        """
        try:
            candidate = normalize_url(raw_url, allow_private_hosts=self._allow_private)
        except Exception as exc:  # noqa: BLE001 - a bad declaration is a finding
            return _SitemapDoc(error=f"Unusable sitemap URL: {type(exc).__name__}")
        if candidate.normalized in seen:
            return _SitemapDoc()
        seen.add(candidate.normalized)

        # A sitemap on another domain is not this site's sitemap. Following it would
        # let any site point our crawler at a third party.
        if candidate.registrable_domain != site_domain:
            return _SitemapDoc(error=f"Off-site sitemap ignored: {candidate.host}")

        fetched = await fetcher.fetch(
            candidate, accept_types=XML_CONTENT_TYPES | {"text/plain", ""}
        )
        if not fetched.ok:
            # A 404 while probing conventional paths is expected, not worth showing.
            if probing_defaults and fetched.status is PageFetchStatus.NOT_FOUND:
                return _SitemapDoc()
            return _SitemapDoc(
                error=f"{candidate.url}: {fetched.error or fetched.status.value}"
            )

        try:
            entries, children = parse_sitemap_document(
                fetched.body,
                base_url=fetched.url.url,
                allow_private_hosts=self._allow_private,
            )
        except ValueError as exc:
            return _SitemapDoc(error=f"{candidate.url}: {exc}")

        return _SitemapDoc(
            final_url=fetched.url.url,
            entries=entries,
            children=children,
            parsed=True,
        )

    # --------------------------------------------------------------------- crawl
    async def crawl(
        self,
        website_url: str | NormalizedURL,
        *,
        keywords: list[str] | None = None,
    ) -> CrawlResult:
        """Crawl one website and return everything found.

        *keywords* are the business's target keywords; per-page occurrence counts
        are recorded for the SEO stage. Passing none simply leaves
        ``keyword_usage`` empty.
        """
        started_at = datetime.now(UTC)
        started_perf = time.perf_counter()

        seed = (
            website_url
            if isinstance(website_url, NormalizedURL)
            else normalize_url(website_url, allow_private_hosts=self._allow_private)
        )
        site_domain = seed.registrable_domain
        result = CrawlResult(
            website_url=seed,
            site_domain=site_domain,
            started_at=started_at,
            completed_at=started_at,
            duration_ms=0,
            config=self._config(),
        )

        async with SafeFetcher(
            timeout_seconds=self._timeout,
            delay_seconds=self._delay,
            max_concurrency=self._max_concurrency,
            user_agent=self._user_agent,
            allow_private_hosts=self._allow_private,
        ) as fetcher:
            await self._report(PROGRESS_ROBOTS, "Checking robots.txt")
            robots, robots_record = await self._load_robots(fetcher, seed.origin)
            result.robots = robots_record

            # A site-requested delay overrides ours, but only upward: honouring a
            # Crawl-delay of 0.1s would make us *faster* than our own politeness
            # floor, which is not what the directive is for. set_delay clamps it.
            if robots.crawl_delay is not None:
                fetcher.set_delay(robots.crawl_delay)
                result.config["delay_seconds"] = max(
                    robots.crawl_delay,
                    settings.crawler_delay_seconds
                    if self._delay is None
                    else self._delay,
                )
                result.config["delay_from_robots"] = True

            robots_record.homepage_allowed = robots.can_fetch(seed.url)
            if not robots_record.homepage_allowed:
                # Stop here rather than returning an empty scan that looks like a
                # broken site. The scan row records robots_disallowed=True and the
                # UI explains what to change.
                result.errors.append(
                    "robots.txt disallows crawling this URL for our user agent."
                )
                await self._report(PROGRESS_CRAWL_END, "Blocked by robots.txt")
                return _finish(result, started_perf)

            await self._report(PROGRESS_SITEMAP, "Looking for a sitemap")
            if self._use_sitemap:
                result.sitemap = await self._load_sitemaps(
                    fetcher,
                    origin=seed.origin,
                    declared=robots_record.sitemap_urls,
                    site_domain=site_domain,
                )

            frontier = Frontier(
                site_domain=site_domain,
                max_pages=self._max_pages,
                max_depth=self._max_depth,
            )
            frontier.seed(seed)

            # Sitemap URLs enter at depth 0: being listed by the owner is a
            # stronger importance signal than click distance from the homepage.
            for entry in result.sitemap.entries:
                if frontier.budget_remaining <= 0:
                    break
                frontier.add(
                    entry.url,
                    depth=0,
                    discovered_via="sitemap",
                    robots_allows=robots.can_fetch(entry.url.url),
                )

            homepage_key = seed.normalized
            await self._report(PROGRESS_CRAWL_START, "Crawling pages")
            await self._run_frontier(
                fetcher,
                frontier,
                robots,
                result,
                homepage_key=homepage_key,
                site_domain=site_domain,
                keywords=keywords,
            )

            result.frontier_report = frontier.report()
            result.hit_page_limit = (
                frontier.skips.get("page_limit", 0) > 0
                or frontier.queued > 0
                or frontier.dispatched >= self._max_pages
            )

            await self._report(PROGRESS_LINKS, "Checking outbound links")
            await self._verify_external_links(result)

        log.info(
            "crawl_finished",
            site=site_domain,
            pages_crawled=result.pages_crawled,
            pages_failed=result.pages_failed,
            sitemap_found=result.sitemap.found,
            robots_source=result.robots.source,
        )
        return _finish(result, started_perf)

    async def _run_frontier(
        self,
        fetcher: SafeFetcher,
        frontier: Frontier,
        robots: RobotsFile,
        result: CrawlResult,
        *,
        homepage_key: str,
        site_domain: str,
        keywords: list[str] | None,
    ) -> None:
        """Drain the frontier in breadth-first waves of ``max_concurrency``."""
        while True:
            wave: list[FrontierItem] = []
            while len(wave) < self._max_concurrency:
                item = frontier.next_item()
                if item is None:
                    break
                wave.append(item)
            if not wave:
                break

            fetched = await asyncio.gather(
                *(fetcher.fetch(item.url) for item in wave),
                return_exceptions=True,
            )
            for item, outcome in zip(wave, fetched, strict=True):
                self._process_outcome(
                    item,
                    outcome,
                    frontier,
                    robots,
                    result,
                    homepage_key=homepage_key,
                    site_domain=site_domain,
                    keywords=keywords,
                )

            span = PROGRESS_CRAWL_END - PROGRESS_CRAWL_START
            done = len(result.pages)
            total = max(done, frontier.discovered, 1)
            await self._report(
                PROGRESS_CRAWL_START + int(span * min(1.0, done / total)),
                f"Crawled {done} page{'s' if done != 1 else ''}",
            )

    async def _verify_external_links(self, result: CrawlResult) -> None:
        """Populate ``PageExtraction.broken_link_count`` for crawled pages.

        Collects distinct external link targets across all pages (page order,
        link order -- deterministic), probes at most
        ``settings.crawler_max_link_checks`` of them with
        :meth:`SafeFetcher.probe` (HEAD, no body, same SSRF guards as the
        crawl), and attributes the broken ones back to each page. Links beyond
        the cap are not checked and do not count -- bounded cost, honest
        numbers. ``crawler_max_link_checks=0`` disables the phase entirely.
        """
        cap = settings.crawler_max_link_checks
        if cap <= 0:
            return
        page_links, to_check = self._collect_link_check_targets(result, cap)
        if not to_check:
            return
        # A dedicated fetcher with a short timeout: link probes must not hold
        # the scan hostage behind slow third-party servers.
        async with SafeFetcher(timeout_seconds=10) as probes:
            outcomes = await asyncio.gather(
                *(probes.probe(target) for _, target in to_check),
                return_exceptions=True,
            )
        broken = self._broken_link_keys(to_check, outcomes)
        for extraction, urls in page_links:
            extraction.broken_link_count = sum(1 for url in urls if url in broken)
        log.info(
            "link_check_finished",
            checked=len(to_check),
            broken=len(broken),
        )

    @staticmethod
    def _collect_link_check_targets(
        result: CrawlResult, cap: int
    ) -> tuple[list[tuple[PageExtraction, list[str]]], list[tuple[str, NormalizedURL]]]:
        """Deduplicate external link targets across pages (deterministic order)."""
        page_links: list[tuple[PageExtraction, list[str]]] = []
        seen: set[str] = set()
        to_check: list[tuple[str, NormalizedURL]] = []
        for crawled in result.pages:
            extraction = crawled.extraction
            if extraction is None:
                continue
            urls: list[str] = []
            for link in extraction.discovered_links:
                if link.is_internal:
                    continue
                key = link.url.normalized
                if key not in urls:
                    urls.append(key)
                if key not in seen and len(to_check) < cap:
                    seen.add(key)
                    to_check.append((key, link.url))
            page_links.append((extraction, urls))
        return page_links, to_check

    @staticmethod
    def _broken_link_keys(
        to_check: list[tuple[str, NormalizedURL]], outcomes: list[LinkCheckResult | BaseException]
    ) -> set[str]:
        """Return the normalized URLs whose probe affirmatively failed."""
        broken: set[str] = set()
        for (key, _), outcome in zip(to_check, outcomes, strict=True):
            if isinstance(outcome, BaseException):  # pragma: no cover - defensive
                log.warning(
                    "link_probe_error", url=key, error_type=type(outcome).__name__
                )
                continue
            if outcome.checked and not outcome.ok:
                broken.add(key)
        return broken

    def _process_outcome(
        self,
        item: FrontierItem,
        outcome: FetchResult | BaseException,
        frontier: Frontier,
        robots: RobotsFile,
        result: CrawlResult,
        *,
        homepage_key: str,
        site_domain: str,
        keywords: list[str] | None,
    ) -> None:
        """Record one fetched page and feed its links back to the frontier."""
        page = _to_page(item, outcome, homepage_key=homepage_key)
        result.pages.append(page)
        if isinstance(outcome, BaseException):
            result.errors.append(f"{item.url.url}: {type(outcome).__name__}")
            return
        if not outcome.ok or not outcome.text:
            return

        extraction = extract_page(
            outcome.text,
            page_url=outcome.url,
            site_domain=site_domain,
            keywords=keywords,
            allow_private_hosts=self._allow_private,
        )
        page.extraction = extraction
        if result.detected_cms is None and extraction.detected_cms:
            result.detected_cms = extraction.detected_cms

        # meta robots nofollow suppresses this page's outgoing links, exactly as
        # rel=nofollow suppresses one link.
        if extraction.nofollow_page:
            return
        for link in extraction.discovered_links:
            frontier.add(
                link.url,
                depth=item.depth + 1,
                discovered_via=item.url.normalized,
                is_nofollow=link.is_nofollow,
                robots_allows=robots.can_fetch(link.url.url),
            )


def _merge_sitemap_doc(
    result: SitemapResult,
    doc: _SitemapDoc,
    raw_url: str,
    queue: list[tuple[str, int]],
    depth: int,
    *,
    probing_defaults: bool,
) -> tuple[list[tuple[str, int]], bool]:
    """Fold one parsed sitemap document into the running result.

    Returns the (possibly pruned) queue and the updated ``probing_defaults`` flag.
    The prune matters: once a conventional-path probe finds a real sitemap, the
    remaining default paths are dropped so a site is not hit with four more 404s.
    """
    result.documents.append(doc.final_url or raw_url)
    if not result.found:
        result.found = True
        result.root_url = doc.final_url
        if probing_defaults:
            queue = [item for item in queue if item[1] > 0]
            probing_defaults = False

    for entry in doc.entries:
        if len(result.entries) >= MAX_SITEMAP_URLS:
            result.truncated = True
            break
        result.entries.append(entry)
    queue.extend((child, depth + 1) for child in doc.children)
    return queue, probing_defaults


def _to_page(
    item: FrontierItem, outcome: FetchResult | BaseException, *, homepage_key: str
) -> CrawledPage:
    """Wrap a fetch outcome as a page row, including when the fetch itself blew up."""
    is_homepage = item.url.normalized == homepage_key
    if isinstance(outcome, BaseException):
        outcome = FetchResult(
            requested=item.url,
            status=PageFetchStatus.CONNECTION_ERROR,
            error=f"{type(outcome).__name__}: {outcome}",
        )
    return CrawledPage(
        url=item.url,
        depth=item.depth,
        is_homepage=is_homepage,
        discovered_via=item.discovered_via,
        fetch=outcome,
    )


def _finish(result: CrawlResult, started_perf: float) -> CrawlResult:
    result.completed_at = datetime.now(UTC)
    result.duration_ms = max(0, int((time.perf_counter() - started_perf) * 1000))
    return result


__all__ = [
    "PROGRESS_CRAWL_END",
    "PROGRESS_CRAWL_START",
    "CrawlResult",
    "CrawledPage",
    "Crawler",
    "RobotsRecord",
]
