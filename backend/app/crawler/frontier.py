"""The crawl frontier: which URLs are in scope, in what order, and how many.

Separated from the crawler so the scoping rules can be tested without any I/O at
all, and so the reason a URL was skipped is a value rather than a log line -- the
scan report tells the user "12 links were off-site and 3 were blocked by your
robots.txt", which requires counting the skips, not discarding them.

Order is breadth-first, with one deliberate exception. A site's most important
pages sit near the root, so BFS spends a 20-page budget far better than DFS, which
would disappear into one blog category. The exception is that sitemap-derived URLs
are seeded at depth 0 regardless of where they sit in the link graph: the site
owner listing a page in their sitemap is a stronger statement about its importance
than its click distance from the homepage.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

from app.utils.urls import NormalizedURL

# Extensions that are never an HTML page. Skipping them by extension avoids a
# wasted request; the fetcher's Content-Type check is still the authority, because
# a URL with no extension can serve anything.
NON_PAGE_EXTENSIONS = frozenset(
    {
        # documents
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "rtf",
        # archives
        "zip", "gz", "tar", "tgz", "bz2", "7z", "rar", "xz",
        # images
        "jpg", "jpeg", "png", "gif", "webp", "svg", "bmp", "ico", "tiff", "avif",
        # audio / video
        "mp3", "mp4", "wav", "avi", "mov", "wmv", "flv", "webm", "ogg", "m4a", "m4v",
        # code / data assets
        "css", "js", "mjs", "json", "xml", "rss", "atom", "map", "wasm",
        # fonts
        "woff", "woff2", "ttf", "eot", "otf",
        # misc
        "exe", "dmg", "apk", "iso", "bin", "swf",
    }
)  # fmt: skip

# Path fragments that are functionally infinite or generate no content worth
# scoring: calendars, faceted search, login walls, cart state. Crawling these is
# how a polite crawler accidentally becomes an impolite one.
NOISE_PATH_MARKERS = (
    "/wp-admin/",
    "/wp-login",
    "/xmlrpc.php",
    "/wp-json/",
    "/cart",
    "/checkout",
    "/basket",
    "/my-account",
    "/login",
    "/signin",
    "/sign-in",
    "/logout",
    "/signout",
    "/register",
    "/password",
    "/admin/",
    "/administrator/",
    "/feed/",
    "/comments/feed",
    "/trackback/",
    "/print/",
    "/cdn-cgi/",
    "/search",
    "?add-to-cart",
    "?replytocom",
)


class SkipReason(StrEnum):
    """Why a discovered URL never became a request.

    Reported per-scan so the page count is explainable (spec section 24): a scan
    that fetched 8 of 40 discovered links should say which rule dropped the other
    32, not leave the user guessing.
    """

    DUPLICATE = "duplicate"
    OFF_SITE = "off_site"
    ROBOTS_DISALLOWED = "robots_disallowed"
    DEPTH_LIMIT = "depth_limit"
    PAGE_LIMIT = "page_limit"
    NON_PAGE_ASSET = "non_page_asset"
    NOISE_PATH = "noise_path"
    NOFOLLOW = "nofollow"


@dataclass(frozen=True, slots=True)
class FrontierItem:
    url: NormalizedURL
    depth: int
    # How this URL was found, kept for the scan's audit trail: "seed",
    # "sitemap", or the normalised URL of the page that linked to it.
    discovered_via: str


@dataclass(slots=True)
class Frontier:
    """A bounded, deduplicating BFS queue restricted to one registrable domain.

    *site_domain* is the registrable domain (``example.com``), so ``blog.example.com``
    and ``www.example.com`` are both in scope while ``example.org`` is not. This
    matches how a business actually structures a site and is the same definition
    :mod:`app.utils.urls` uses, so scope cannot drift between the two.
    """

    site_domain: str
    max_pages: int
    max_depth: int
    respect_nofollow: bool = True

    _queue: deque[FrontierItem] = field(default_factory=deque, init=False)
    _seen: set[str] = field(default_factory=set, init=False)
    _dispatched: int = field(default=0, init=False)
    skips: dict[str, int] = field(default_factory=dict, init=False)
    # Off-site destinations, deduplicated. Not crawled, but the *count* of distinct
    # external domains a site links out to is a real AI-visibility signal, so the
    # information is kept rather than thrown away with the URL.
    external_domains: set[str] = field(default_factory=set, init=False)

    # ------------------------------------------------------------------ counters
    @property
    def dispatched(self) -> int:
        """URLs handed out for fetching so far."""
        return self._dispatched

    @property
    def queued(self) -> int:
        return len(self._queue)

    @property
    def discovered(self) -> int:
        """Distinct in-scope URLs ever accepted, whether fetched yet or not."""
        return len(self._seen)

    @property
    def budget_remaining(self) -> int:
        return max(0, self.max_pages - self._dispatched)

    def _skip(self, reason: SkipReason) -> bool:
        self.skips[reason.value] = self.skips.get(reason.value, 0) + 1
        return False

    # ------------------------------------------------------------------- filters
    def _is_non_page(self, url: NormalizedURL) -> bool:
        tail = url.path.rsplit("/", 1)[-1]
        if "." not in tail:
            return False
        return tail.rsplit(".", 1)[-1].lower() in NON_PAGE_EXTENSIONS

    def _is_noise(self, url: NormalizedURL) -> bool:
        # Compared against path + query so "?add-to-cart" and "?replytocom" match.
        target = url.path.lower()
        if url.query:
            target = f"{target}?{url.query.lower()}"
        return any(marker in target for marker in NOISE_PATH_MARKERS)

    # ---------------------------------------------------------------------- adds
    def add(
        self,
        url: NormalizedURL,
        *,
        depth: int,
        discovered_via: str,
        is_nofollow: bool = False,
        robots_allows: bool = True,
    ) -> bool:
        """Offer one URL to the frontier. Returns whether it was accepted.

        Filter order is deliberate and cheapest-first, but more importantly it
        makes the skip counters meaningful: a URL is attributed to the *first*
        rule that excluded it, so the totals sum to the number offered instead of
        double-counting a duplicate that is also off-site.
        """
        key = url.normalized

        if key in self._seen:
            return self._skip(SkipReason.DUPLICATE)

        if url.registrable_domain != self.site_domain:
            self.external_domains.add(url.registrable_domain)
            return self._skip(SkipReason.OFF_SITE)

        if self._is_non_page(url):
            self._seen.add(key)  # remember, so a repeated PDF link is not re-judged
            return self._skip(SkipReason.NON_PAGE_ASSET)

        if self._is_noise(url):
            self._seen.add(key)
            return self._skip(SkipReason.NOISE_PATH)

        if self.respect_nofollow and is_nofollow:
            # Not marked seen: the same URL reached by a followable link elsewhere
            # on the site is still fair game, and rel=nofollow is a statement about
            # that one link, not about the destination.
            return self._skip(SkipReason.NOFOLLOW)

        if not robots_allows:
            self._seen.add(key)
            return self._skip(SkipReason.ROBOTS_DISALLOWED)

        if depth > self.max_depth:
            return self._skip(SkipReason.DEPTH_LIMIT)

        # Counted against URLs already accepted, not against those already fetched,
        # so the queue never grows past the budget and the cap is honoured even when
        # every accepted URL is still in flight.
        if len(self._seen) >= self.max_pages:
            return self._skip(SkipReason.PAGE_LIMIT)

        self._seen.add(key)
        self._queue.append(
            FrontierItem(url=url, depth=depth, discovered_via=discovered_via)
        )
        return True

    def seed(self, url: NormalizedURL) -> bool:
        """Add the starting URL, exempt from the robots and noise filters.

        The homepage is what the user asked us to analyse. If robots.txt disallows
        it the crawler reports that as a finding and stops -- it does not silently
        return an empty scan, and it does not crawl it anyway.
        """
        key = url.normalized
        if key in self._seen:
            return self._skip(SkipReason.DUPLICATE)
        self._seen.add(key)
        self._queue.append(FrontierItem(url=url, depth=0, discovered_via="seed"))
        return True

    # --------------------------------------------------------------------- reads
    def next_item(self) -> FrontierItem | None:
        """Pop the next URL, or ``None`` when the queue is empty or spent."""
        if self._dispatched >= self.max_pages:
            return None
        if not self._queue:
            return None
        self._dispatched += 1
        return self._queue.popleft()

    def report(self) -> dict[str, object]:
        """A summary the scan can store verbatim and the UI can display."""
        return {
            "discovered": self.discovered,
            "dispatched": self._dispatched,
            "queued_unvisited": len(self._queue),
            "max_pages": self.max_pages,
            "max_depth": self.max_depth,
            "skipped": dict(sorted(self.skips.items())),
            "external_domains": sorted(d for d in self.external_domains if d),
        }


__all__ = [
    "NOISE_PATH_MARKERS",
    "NON_PAGE_EXTENSIONS",
    "Frontier",
    "FrontierItem",
    "SkipReason",
]
