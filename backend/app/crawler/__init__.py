"""The crawler: AURA's own website-analysis engine.

This package is deliberately AI-free (spec sections 36/37). Everything here is
fetching, parsing, counting, and applying documented rules; Gemini is not involved
at any point. That matters because the crawler's output is the *evidence* every
later score is computed from, and evidence produced by a language model would make
those scores unverifiable.

Layout, in dependency order:

* :mod:`app.crawler.robots` -- RFC 9309 robots.txt parsing. No I/O.
* :mod:`app.crawler.fetcher` -- :class:`~app.crawler.fetcher.SafeFetcher`, the one
  guarded egress point for every outbound request.
* :mod:`app.crawler.sitemap` -- sitemap and sitemap-index parsing, XXE-hardened.
* :mod:`app.crawler.extract` -- one HTML document to one page's worth of facts.
* :mod:`app.crawler.frontier` -- scope, dedup, ordering, and the page/depth caps.
* :mod:`app.crawler.crawler` -- :class:`~app.crawler.crawler.Crawler`, which
  sequences the above.

Only the last one is normally called from outside; the rest are imported here so
that ``from app.crawler import Crawler, SafeFetcher`` works and so the modules are
registered when the package is imported.
"""

from app.crawler.crawler import CrawledPage, Crawler, CrawlResult, RobotsRecord
from app.crawler.extract import DiscoveredLink, PageExtraction, extract_page
from app.crawler.fetcher import FetchResult, SafeFetcher
from app.crawler.frontier import Frontier, FrontierItem, SkipReason
from app.crawler.robots import RobotsFile
from app.crawler.sitemap import SitemapEntry, SitemapResult, parse_sitemap_document

__all__ = [
    "CrawlResult",
    "CrawledPage",
    "Crawler",
    "DiscoveredLink",
    "FetchResult",
    "Frontier",
    "FrontierItem",
    "PageExtraction",
    "RobotsFile",
    "RobotsRecord",
    "SafeFetcher",
    "SitemapEntry",
    "SitemapResult",
    "SkipReason",
    "extract_page",
    "parse_sitemap_document",
]
