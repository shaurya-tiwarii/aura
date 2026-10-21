"""The only way AURA makes an outbound request to a user-supplied address.

Everything the crawler touches -- pages, robots.txt, and sitemaps -- goes
through :class:`SafeFetcher`. Concentrating egress in
one class is what makes the spec section 21 guarantees checkable instead of
aspirational: there is a single place to audit, and a single place a limit can be
forgotten.

What this enforces
------------------
* **Redirects are followed manually.** ``follow_redirects=True`` would hand the
  SSRF decision to httpx, and a site that 302s to ``http://169.254.169.254/`` --
  the cloud metadata endpoint -- would be fetched without any further check. Every
  hop here is re-parsed by :func:`~app.utils.urls.normalize_url` and re-resolved,
  so hop *n* is guarded exactly as strictly as hop 0.
* **DNS answers are checked, not just hostnames.** ``normalize_url`` can reject
  ``localhost`` and IP literals, but not ``evil.example`` with an A record
  pointing at ``10.0.0.1``. Every address the host resolves to must satisfy
  :func:`~app.utils.urls.is_public_ip`; one bad answer fails the whole fetch,
  because a round-robin record with one internal address is the same attack.
* **Responses are read as a stream against a byte cap.** ``await response.aread()``
  on an endless or multi-gigabyte body is a memory-exhaustion bug with a friendly
  face. Reading incrementally and stopping at the cap bounds it.
* **Content types are gated before the body is parsed.** A 40 MB video served from
  a URL ending in ``.html`` is not worth downloading, let alone feeding to lxml.
* **A per-request delay and a concurrency cap.** Politeness is not optional for a
  crawler that visits sites it does not own.

Known limitation, stated rather than hidden
-------------------------------------------
The DNS check happens before the connection, so a determined attacker who controls
their own DNS with a near-zero TTL could answer with a public address for our
lookup and a private one for the OS's connect ("DNS rebinding"). Closing that
window entirely means connecting to a pinned IP and carrying the hostname only in
SNI plus the ``Host`` header, which breaks transparently on any redirect chain or
virtual-host edge case. It is not closed here. The exposure is bounded: AURA only
fetches addresses an authenticated user registered as *their own* business
website, so winning this race costs an attacker a signup and yields blind
requests to their own private network -- not a cross-tenant read. If AURA ever
crawls addresses supplied by one user *about* another (an open URL-preview
endpoint, say), this must be revisited first.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import TracebackType

import httpx

from app.core.config import settings
from app.core.errors import BlockedURLError, InvalidURLError
from app.core.logging import get_logger
from app.models.enums import PageFetchStatus
from app.utils.urls import NormalizedURL, is_public_ip, normalize_url

log = get_logger(__name__)

# Hard ceiling on redirect hops. Five is what browsers and every major crawler
# settle on; a chain longer than that is a loop or a tracking gauntlet.
MAX_REDIRECTS = 5

# Content types worth parsing as a web page. XHTML is included because older CMS
# themes still serve it, and a strict text/html check would silently skip them.
HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
XML_CONTENT_TYPES = frozenset(
    {
        "text/xml",
        "application/xml",
        "application/rss+xml",
        "application/atom+xml",
    }
)

# How much of the body to scan for a `<meta charset>` declaration. The HTML spec
# requires it inside the first 1024 bytes; 4 KiB is slack for sites that ignore
# that and put a licence comment first.
_CHARSET_SNIFF_BYTES = 4096


@dataclass(frozen=True, slots=True)
class FetchResult:
    """The outcome of one fetch attempt -- success or failure, never an exception.

    Failures are values rather than raised exceptions because a crawl of 20 pages
    routinely contains a 404 and a timeout, and those are *results* the user needs
    to see, not errors that should abandon the scan. The one thing that does raise
    is a URL AURA must not fetch at all; that is a caller mistake, not a site
    problem.
    """

    requested: NormalizedURL
    status: PageFetchStatus
    final_url: NormalizedURL | None = None
    http_status: int | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    text: str = ""
    content_type: str | None = None
    content_bytes: int = 0
    elapsed_ms: int = 0
    redirect_chain: tuple[str, ...] = ()
    truncated: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in {PageFetchStatus.OK, PageFetchStatus.REDIRECTED}

    @property
    def url(self) -> NormalizedURL:
        """Where the content actually came from, after any redirects."""
        return self.final_url or self.requested

    @property
    def redirect_count(self) -> int:
        return len(self.redirect_chain)


def _classify(http_status: int, *, redirected: bool) -> PageFetchStatus:
    """Map an HTTP status onto the stored page outcome.

    401/403/429 collapse to ``BLOCKED`` rather than an error: from the user's point
    of view "the site refused us" is one situation with one explanation, whether it
    was a login wall, a WAF, or rate limiting.
    """
    if 200 <= http_status < 300:
        return PageFetchStatus.REDIRECTED if redirected else PageFetchStatus.OK
    if http_status in {401, 403, 429}:
        return PageFetchStatus.BLOCKED
    if http_status in {404, 410}:
        return PageFetchStatus.NOT_FOUND
    if http_status >= 500:
        return PageFetchStatus.SERVER_ERROR
    # Anything else in the 4xx range is still a client-side refusal to serve.
    return PageFetchStatus.NOT_FOUND


def _media_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _charset(content_type: str | None) -> str | None:
    for part in (content_type or "").split(";")[1:]:
        key, _, value = part.partition("=")
        if key.strip().lower() == "charset":
            return value.strip().strip("\"'").lower() or None
    return None


def _sniff_charset(head: bytes) -> str | None:
    """Look for a ``<meta charset>`` when the server did not send one.

    Byte-level rather than parsed, because the whole point is that we do not yet
    know how to decode the document. Latin-1 round-trips every byte, so the search
    itself can never fail on invalid input.
    """
    window = head[:_CHARSET_SNIFF_BYTES].decode("latin-1", errors="replace").lower()
    for marker in ('charset="', "charset='", "charset="):
        index = window.find(marker)
        while index != -1:
            start = index + len(marker)
            end = start
            while end < len(window) and window[end] not in "\"'>; \t\r\n/":
                end += 1
            candidate = window[start:end].strip()
            if candidate:
                return candidate
            index = window.find(marker, start)
    return None


def _decode(body: bytes, content_type: str | None) -> str:
    """Best-effort text, never an exception.

    Order matters: the server's declared charset is authoritative, an in-document
    declaration is the documented fallback, and UTF-8 with replacement is the last
    resort. ``errors="replace"`` is deliberate -- a single bad byte in a footer
    must not cost us the whole page's analysis.
    """
    for encoding in (_charset(content_type), _sniff_charset(body), "utf-8"):
        if not encoding:
            continue
        try:
            return body.decode(encoding, errors="replace")
        except LookupError:
            # Unknown codec name, e.g. `charset=unicode` or a typo.
            continue
    return body.decode("utf-8", errors="replace")


@dataclass(slots=True)
class LinkCheckResult:
    """Outcome of one outbound-link verification probe.

    ``checked`` is False when *our own* guards refused the check (SSRF block,
    invalid URL, redirect loop) -- that is not the link's fault and must not
    count against the site's score. ``ok`` is False only when the link
    affirmatively failed: a 4xx/5xx status, a timeout, or a connection error.
    """

    url: str
    checked: bool
    ok: bool
    http_status: int | None = None
    error: str | None = None


class SafeFetcher:
    """A guarded async HTTP client for exactly one crawl run.

    Used as an async context manager so the underlying connection pool is always
    closed::

        async with SafeFetcher() as fetcher:
            result = await fetcher.fetch(url)
    """

    __slots__ = (
        "_allow_private",
        "_client",
        "_delay",
        "_dns_cache",
        "_last_request_at",
        "_lock",
        "_max_bytes",
        "_semaphore",
        "_timeout",
        "_user_agent",
    )

    def __init__(
        self,
        *,
        timeout_seconds: float | None = None,
        delay_seconds: float | None = None,
        max_response_bytes: int | None = None,
        max_concurrency: int | None = None,
        user_agent: str | None = None,
        allow_private_hosts: bool | None = None,
    ) -> None:
        self._timeout = (
            settings.crawler_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        self._delay = (
            settings.crawler_delay_seconds if delay_seconds is None else delay_seconds
        )
        self._max_bytes = (
            settings.crawler_max_response_bytes
            if max_response_bytes is None
            else max_response_bytes
        )
        self._user_agent = user_agent or settings.crawler_user_agent
        self._allow_private = (
            settings.crawler_allow_private_hosts
            if allow_private_hosts is None
            else allow_private_hosts
        )
        concurrency = (
            settings.crawler_max_concurrency
            if max_concurrency is None
            else max_concurrency
        )
        self._semaphore = asyncio.Semaphore(concurrency)
        # Serialises the *start* of each request so the politeness delay is
        # measured between requests globally, not per-task. Without this, four
        # concurrent workers each waiting 0.5s still produce four simultaneous
        # hits.
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._dns_cache: dict[tuple[str, int], bool] = {}
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> SafeFetcher:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout, connect=min(self._timeout, 10.0)),
            # See the module docstring: the SSRF re-check on each hop is ours to do.
            follow_redirects=False,
            headers={
                "User-Agent": self._user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en",
                # Identifies the crawler as a bot to servers that check, and keeps
                # us out of caches that would otherwise serve us a stale page.
                "Cache-Control": "no-cache",
            },
            # A crawl of one site benefits from keep-alive; the cap stops a
            # redirect chain across many hosts from opening an unbounded pool.
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            trust_env=False,  # never inherit a developer's proxy for a crawl
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ guards
    async def _check_dns(self, host: str, port: int) -> None:
        """Resolve *host* and refuse if any answer is not globally routable."""
        if self._allow_private:
            # Only ever true for a test pointed at a local fixture server;
            # ``Settings`` refuses this combination in production.
            return

        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            # An IP literal was already vetted by normalize_url; re-checking here
            # keeps this method's contract independent of the caller's diligence.
            if not is_public_ip(ip):
                raise BlockedURLError(
                    "That address points to a private or internal network.",
                    detail=host,
                    code="private_address",
                )
            return

        cache_key = (host, port)
        cached = self._dns_cache.get(cache_key)
        if cached is False:
            raise BlockedURLError(
                "That address resolves to a private or internal network.",
                detail=host,
                code="private_address",
            )
        if cached is True:
            return

        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            # Not a block: the name simply does not resolve. Surfaced as a fetch
            # failure so the scan reports "we could not reach this" rather than
            # implying the user's site is malicious.
            raise _Unreachable(f"DNS lookup failed for {host}") from exc

        addresses = _addresses_from(infos)
        if not addresses:
            raise _Unreachable(f"DNS returned no addresses for {host}")

        for address in addresses:
            if not is_public_ip(address):
                self._dns_cache[cache_key] = False
                log.warning(
                    "crawler_blocked_private_dns",
                    host=host,
                    resolved=str(address),
                )
                raise BlockedURLError(
                    "That address resolves to a private or internal network.",
                    detail=f"{host} -> {address}",
                    hint="Enter the public web address of the site instead.",
                    code="private_address",
                )
        self._dns_cache[cache_key] = True

    def set_delay(self, seconds: float) -> None:
        """Raise the politeness delay mid-run, never lower it.

        Exists because ``Crawl-delay`` is only known after robots.txt has been
        read, by which point the client and its connection pool are already open.
        Clamped to never *reduce* the configured floor: a site asking for 0.1s must
        not make AURA faster than its own politeness setting.
        """
        self._delay = max(self._delay, seconds)

    async def _wait_turn(self) -> None:
        """Hold back until the politeness delay since the last request has passed."""
        if self._delay <= 0:
            return
        async with self._lock:
            gap = time.monotonic() - self._last_request_at
            if gap < self._delay:
                await asyncio.sleep(self._delay - gap)
            self._last_request_at = time.monotonic()

    # ------------------------------------------------------------------- fetch
    async def fetch(
        self,
        target: str | NormalizedURL,
        *,
        accept_types: Iterable[str] | None = None,
    ) -> FetchResult:
        """Fetch one URL, following redirects with a full re-check on every hop.

        *accept_types* gates the response media type; the default is HTML. Pass an
        empty iterable to accept anything (used for robots.txt, which some servers
        mislabel as ``application/octet-stream``).
        """
        if self._client is None:  # pragma: no cover - misuse, not a site problem
            raise RuntimeError("SafeFetcher must be used as an async context manager.")

        requested = (
            target
            if isinstance(target, NormalizedURL)
            else normalize_url(target, allow_private_hosts=self._allow_private)
        )
        allowed = (
            HTML_CONTENT_TYPES if accept_types is None else frozenset(accept_types)
        )

        started = time.perf_counter()
        chain: list[str] = []
        current = requested

        try:
            for _hop in range(MAX_REDIRECTS + 1):
                await self._check_dns(
                    current.host, current.port or _default_port(current)
                )

                response, body, truncated = await self._request(current)
                content_type = response.headers.get("content-type")

                if response.is_redirect:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        # A 3xx with no Location is a broken server, not a hop.
                        return FetchResult(
                            requested=requested,
                            status=PageFetchStatus.SERVER_ERROR,
                            final_url=current,
                            http_status=response.status_code,
                            headers=dict(response.headers),
                            content_type=content_type,
                            elapsed_ms=_ms(started),
                            redirect_chain=tuple(chain),
                            error="Redirect response had no Location header.",
                        )
                    chain.append(current.url)
                    # Resolved against the *current* URL, so a relative Location
                    # on hop 2 lands where the browser would put it.
                    absolute = str(httpx.URL(current.url).join(location))
                    current = normalize_url(
                        absolute, allow_private_hosts=self._allow_private
                    )
                    continue

                status = _classify(response.status_code, redirected=bool(chain))
                media = _media_type(content_type)
                if status is not PageFetchStatus.OK and status is not (
                    PageFetchStatus.REDIRECTED
                ):
                    # Keep the body out of the result for error responses: a 404
                    # page's word count is not a fact about the user's site.
                    return FetchResult(
                        requested=requested,
                        status=status,
                        final_url=current,
                        http_status=response.status_code,
                        headers=dict(response.headers),
                        content_type=content_type,
                        content_bytes=len(body),
                        elapsed_ms=_ms(started),
                        redirect_chain=tuple(chain),
                        error=f"HTTP {response.status_code}",
                    )

                if allowed and media and media not in allowed:
                    return FetchResult(
                        requested=requested,
                        status=PageFetchStatus.SKIPPED,
                        final_url=current,
                        http_status=response.status_code,
                        headers=dict(response.headers),
                        content_type=content_type,
                        content_bytes=len(body),
                        elapsed_ms=_ms(started),
                        redirect_chain=tuple(chain),
                        error=f"Not a web page ({media or 'unknown content type'}).",
                    )

                return FetchResult(
                    requested=requested,
                    status=status,
                    final_url=current,
                    http_status=response.status_code,
                    headers=dict(response.headers),
                    body=body,
                    text=_decode(body, content_type),
                    content_type=content_type,
                    content_bytes=len(body),
                    elapsed_ms=_ms(started),
                    redirect_chain=tuple(chain),
                    truncated=truncated,
                )

            # Fell out of the loop: more hops than MAX_REDIRECTS.
            return FetchResult(
                requested=requested,
                status=PageFetchStatus.SKIPPED,
                final_url=current,
                elapsed_ms=_ms(started),
                redirect_chain=tuple(chain),
                error=f"More than {MAX_REDIRECTS} redirects.",
            )

        except (BlockedURLError, InvalidURLError) as exc:
            # A redirect walked somewhere we must not go, which is a property of
            # the site rather than of the user's request -- so it is a recorded
            # page outcome, not a raised API error.
            return FetchResult(
                requested=requested,
                status=PageFetchStatus.BLOCKED,
                final_url=current,
                elapsed_ms=_ms(started),
                redirect_chain=tuple(chain),
                error=exc.message,
            )
        except _Unreachable as exc:
            return FetchResult(
                requested=requested,
                status=PageFetchStatus.CONNECTION_ERROR,
                elapsed_ms=_ms(started),
                redirect_chain=tuple(chain),
                error=str(exc),
            )
        except httpx.TimeoutException:
            return FetchResult(
                requested=requested,
                status=PageFetchStatus.TIMEOUT,
                final_url=current,
                elapsed_ms=_ms(started),
                redirect_chain=tuple(chain),
                error=f"No response within {self._timeout:g}s.",
            )
        except httpx.HTTPError as exc:
            # Connection refused, TLS failure, malformed response. The class name
            # is included because the message alone is often empty, but the
            # exception is never re-raised: a site AURA cannot reach is a finding.
            return FetchResult(
                requested=requested,
                status=PageFetchStatus.CONNECTION_ERROR,
                final_url=current,
                elapsed_ms=_ms(started),
                redirect_chain=tuple(chain),
                error=f"{type(exc).__name__}: {exc}"
                if str(exc)
                else type(exc).__name__,
            )

    async def probe(self, target: str | NormalizedURL) -> LinkCheckResult:
        """Verify one outbound link without downloading its body.

        Uses the same guards as :meth:`fetch` -- URL normalization, per-hop DNS
        re-resolution against the SSRF policy, manual redirect following -- but
        issues ``HEAD`` requests (falling back to a bodyless ``GET`` when the
        server refuses ``HEAD``) so a link check costs one round trip, not a
        download. Shares the fetcher's politeness delay and concurrency cap.
        """
        if self._client is None:  # pragma: no cover - misuse, not a site problem
            raise RuntimeError("SafeFetcher must be used as an async context manager.")

        try:
            requested = (
                target
                if isinstance(target, NormalizedURL)
                else normalize_url(target, allow_private_hosts=self._allow_private)
            )
        except (BlockedURLError, InvalidURLError) as exc:
            return LinkCheckResult(
                url=str(target), checked=False, ok=False, error=exc.message
            )
        chain: list[str] = []
        current = requested
        try:
            for _hop in range(MAX_REDIRECTS + 1):
                await self._check_dns(
                    current.host, current.port or _default_port(current)
                )
                response = await self._probe_request(current)
                if response.is_redirect:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        return LinkCheckResult(
                            url=requested.url,
                            checked=True,
                            ok=False,
                            http_status=response.status_code,
                            error="Redirect response had no Location header.",
                        )
                    chain.append(current.url)
                    absolute = str(httpx.URL(current.url).join(location))
                    current = normalize_url(
                        absolute, allow_private_hosts=self._allow_private
                    )
                    continue
                status_code = response.status_code
                return LinkCheckResult(
                    url=requested.url,
                    checked=True,
                    ok=status_code < 400,
                    http_status=status_code,
                    error=None if status_code < 400 else f"HTTP {status_code}",
                )
            return LinkCheckResult(
                url=requested.url,
                checked=False,
                ok=False,
                error=f"More than {MAX_REDIRECTS} redirects.",
            )
        except (BlockedURLError, InvalidURLError) as exc:
            # Our own SSRF guard refused the check; not the link's fault.
            return LinkCheckResult(
                url=requested.url, checked=False, ok=False, error=exc.message
            )
        except _Unreachable as exc:
            return LinkCheckResult(
                url=requested.url, checked=True, ok=False, error=str(exc)
            )
        except httpx.TimeoutException:
            return LinkCheckResult(
                url=requested.url,
                checked=True,
                ok=False,
                error=f"No response within {self._timeout:g}s.",
            )
        except httpx.HTTPError as exc:
            return LinkCheckResult(
                url=requested.url,
                checked=True,
                ok=False,
                error=f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__,
            )

    async def _probe_request(self, url: NormalizedURL) -> httpx.Response:
        """One HEAD round trip for a link probe, body never read.

        Some servers reject ``HEAD``; on 405/501 the hop is retried with
        ``GET`` and the stream is closed without reading a single byte.
        """
        assert self._client is not None  # noqa: S101 - guarded by the caller
        async with self._semaphore:
            await self._wait_turn()
            response = await self._client.head(url.url)
            if response.status_code in {405, 501}:
                await response.aclose()
                async with self._client.stream("GET", url.url) as get_response:
                    # Status and headers are enough; the body is link-check
                    # ballast, so the stream is closed unread.
                    await get_response.aclose()
                    return get_response
            return response

    async def _request(self, url: NormalizedURL) -> tuple[httpx.Response, bytes, bool]:
        """One HTTP round trip, body read incrementally against the byte cap."""
        assert self._client is not None  # noqa: S101 - guarded by the caller
        async with self._semaphore:
            await self._wait_turn()
            async with self._client.stream("GET", url.url) as response:
                if response.is_redirect:
                    # No body needed, and reading one on a 301 wastes a round trip.
                    return response, b"", False

                declared = response.headers.get("content-length")
                if (
                    declared is not None
                    and declared.isdigit()
                    and (int(declared) > self._max_bytes)
                ):
                    # Refuse before transferring: the server has already told us it
                    # is too big.
                    await response.aclose()
                    return response, b"", True

                chunks: list[bytes] = []
                total = 0
                truncated = False
                async for chunk in response.aiter_bytes(chunk_size=32 * 1024):
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= self._max_bytes:
                        truncated = True
                        break
                body = b"".join(chunks)[: self._max_bytes]
                return response, body, truncated


class _Unreachable(Exception):
    """DNS-level failure. Internal: converted to a FetchResult before returning."""


def _default_port(url: NormalizedURL) -> int:
    return 443 if url.scheme == "https" else 80


def _ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _addresses_from(
    infos: Iterable[tuple[int, int, int, str, tuple[object, ...]]],
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        try:
            addresses.append(ipaddress.ip_address(str(sockaddr[0])))
        except ValueError:  # pragma: no cover - getaddrinfo returned a non-IP
            continue
    return addresses


__all__ = [
    "HTML_CONTENT_TYPES",
    "MAX_REDIRECTS",
    "XML_CONTENT_TYPES",
    "FetchResult",
    "LinkCheckResult",
    "SafeFetcher",
]
