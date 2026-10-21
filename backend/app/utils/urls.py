"""URL canonicalisation, registrable-domain extraction, and the SSRF guard.

Every URL that AURA stores or fetches passes through :func:`normalize_url`. It
lives here rather than in a route or in the crawler because three different
modules need to agree on the answer:

* **Website registration** (Module 2) stores ``normalized_url`` as the per-business
  uniqueness key, so ``example.com``, ``https://EXAMPLE.com/``, and
  ``https://example.com/?utm_source=x`` must all collapse to one row.
* **The crawler** (Module 4) refuses to leave ``registrable_domain``, and
  deduplicates discovered links by their normalised form.
* **The SSRF guard** (spec section 21) has to reject the same set of hosts in both
  places. A guard that only runs in one of them is not a guard.

Two design notes worth keeping
------------------------------
**The public suffix list is used offline.** ``tldextract`` fetches a fresh list
over the network on first use by default and caches it to disk. Both are wrong
here: a first request would block on a network call, an offline machine would
behave differently from an online one, and the zero-cost rule (spec section 8)
means no runtime dependency on a third-party service. The extractor below is
pinned to the snapshot bundled with the package, so results are deterministic and
no network or cache directory is touched. The trade-off is that the list ages
with the pinned dependency, which is the correct trade for a suffix list that
changes a few times a year.

**Private PSL entries count as registrable domains.** With
``include_psl_private_domains=True``, ``alice.github.io`` has the registrable
domain ``alice.github.io`` rather than ``github.io``. That distinction is a safety
control, not a nicety: with the default setting the crawler's same-domain check
would treat every other user's ``*.github.io`` site as in-scope and happily crawl
strangers' sites.

This module performs *syntactic* SSRF checks only -- no DNS resolution, so
registering a website never depends on the network and never blocks a request on
a resolver timeout. The complementary check (resolve, then verify every returned
address is public) belongs at fetch time and is the crawler's responsibility;
the two together are what defeat DNS rebinding.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import SplitResult, parse_qsl, quote, urlencode, urlsplit, urlunsplit

import tldextract

from app.core.config import settings
from app.core.errors import BlockedURLError, InvalidURLError

# Matches the ``String(2048)`` columns that store URLs. Enforced here as well as
# in the schemas so a service-layer caller cannot exceed the column width.
MAX_URL_LENGTH = 2048
MAX_HOST_LENGTH = 253
MAX_LABEL_LENGTH = 63

ALLOWED_SCHEMES = frozenset({"http", "https"})

# RFC 6761 special-use names plus the conventional intranet suffixes. None of
# these can belong to a real business website, and all of them are classic SSRF
# targets on a machine with a split-horizon resolver.
BLOCKED_HOST_SUFFIXES: tuple[str, ...] = (
    ".localhost",
    ".local",
    ".localdomain",
    ".test",
    ".example",
    ".invalid",
    ".internal",
    ".intranet",
    ".lan",
    ".corp",
    ".home",
    ".home.arpa",
    ".in-addr.arpa",
    ".ip6.arpa",
)
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        # Cloud instance-metadata endpoints. The IP forms are already caught by
        # the private-address check; these are the DNS aliases for them.
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }
)

# Ports that do not speak HTTP. Fetching them is never useful and is how an
# SSRF is turned into a port scan or a protocol-smuggling attempt.
BLOCKED_PORTS = frozenset(
    {
        22,  # ssh
        23,  # telnet
        25,  # smtp
        110,  # pop3
        135,  # msrpc
        137,  # netbios
        138,
        139,
        143,  # imap
        445,  # smb
        1433,  # mssql
        1521,  # oracle
        2375,  # docker api
        2376,
        3306,  # mysql
        3389,  # rdp
        5432,  # postgres
        5672,  # amqp
        6379,  # redis
        9200,  # elasticsearch
        11211,  # memcached
        27017,  # mongodb
    }
)

# Analytics and ad-click parameters. Stripping them is what makes the dedup key
# stable: the same page shared from three campaigns is one page.
TRACKING_PARAMS = frozenset(
    {
        "gclid",
        "dclid",
        "fbclid",
        "msclkid",
        "yclid",
        "twclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
        "referrer",
        "s_kwcid",
        "_ga",
        "_gl",
        "gad_source",
        "gbraid",
        "wbraid",
        "spm",
        "trk",
    }
)
TRACKING_PARAM_PREFIXES = ("utm_", "hsa_", "vero_", "_hs", "pk_", "mtm_", "matomo_")

_HOST_CHARS = re.compile(r"^[a-z0-9._-]+$")
# A leading ``digits`` run before the first path separator means the text after a
# colon is a port, not the body of a ``scheme:`` URL. This is what tells
# ``example.com:8080/x`` apart from ``javascript:alert(1)``.
_PORT_PREFIX = re.compile(r"^\d{1,5}(?:[/?#]|$)")
_SCHEME_PREFIX = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*):(?P<rest>.*)$")

_extract = tldextract.TLDExtract(
    # Empty tuple = never fetch. ``fallback_to_snapshot`` then uses the list
    # bundled in the installed package.
    suffix_list_urls=(),
    fallback_to_snapshot=True,
    cache_dir=None,
    include_psl_private_domains=True,
)


@dataclass(frozen=True, slots=True)
class NormalizedURL:
    """The canonical forms of one URL.

    ``url`` is what AURA fetches and displays; ``normalized`` is what it compares
    and deduplicates by. They differ deliberately:

    * ``url`` preserves query-parameter order, because some servers are sensitive
      to it and the point of this value is that requesting it works.
    * ``normalized`` sorts the query and drops a trailing slash from non-root
      paths, because the point of *that* value is that two spellings of one page
      produce one key.
    """

    given: str
    url: str
    normalized: str
    scheme: str
    host: str
    port: int | None
    path: str
    query: str
    registrable_domain: str
    is_ip_literal: bool

    @property
    def origin(self) -> str:
        """Scheme and authority only -- the base for resolving relative links."""
        netloc = f"{self.host}:{self.port}" if self.port else self.host
        return f"{self.scheme}://{netloc}"


def normalize_url(
    raw: str, *, allow_private_hosts: bool | None = None
) -> NormalizedURL:
    """Canonicalise ``raw`` and refuse anything unsafe to fetch.

    Raises :class:`~app.core.errors.InvalidURLError` when the text is not a usable
    http(s) URL, and :class:`~app.core.errors.BlockedURLError` when it is well
    formed but points somewhere AURA must not go. The two are distinct so the UI
    can say "that doesn't look like a web address" rather than implying the user's
    own site is malicious.

    ``allow_private_hosts`` defaults to ``settings.crawler_allow_private_hosts``,
    which is false everywhere except a test that deliberately points at a local
    fixture server.
    """
    if allow_private_hosts is None:
        allow_private_hosts = settings.crawler_allow_private_hosts

    candidate = (raw or "").strip()
    # Users paste from address bars and documents; a stray control character or a
    # zero-width space would otherwise end up inside the stored hostname.
    candidate = "".join(ch for ch in candidate if ch.isprintable() and not ch.isspace())

    if not candidate:
        raise InvalidURLError(
            "Please enter a website address.", hint="For example: example.com"
        )
    if len(candidate) > MAX_URL_LENGTH:
        raise InvalidURLError(
            f"That address is too long (limit {MAX_URL_LENGTH} characters).",
            code="url_too_long",
        )

    candidate = _ensure_scheme(candidate)
    parts = _split(candidate)

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidURLError(
            f"AURA can only analyse http and https addresses, not '{scheme}'.",
            code="unsupported_scheme",
        )
    if "@" in parts.netloc:
        # Credentials in a URL are a phishing and credential-leak vector, and
        # ``user@evil.example`` reads as a hostname to a human skimming a form.
        raise InvalidURLError(
            "Remove the username and password from the address.",
            code="url_contains_credentials",
        )

    host = _clean_host(parts)
    port = _clean_port(parts)
    _guard_host(host, allow_private_hosts=allow_private_hosts)
    if port is not None and port in BLOCKED_PORTS and not allow_private_hosts:
        raise BlockedURLError(
            f"Port {port} is not a web port, so AURA will not connect to it.",
            code="blocked_port",
        )

    is_ip_literal = _as_ip_address(host) is not None
    registrable = "" if is_ip_literal else _registrable_domain(host)
    if not is_ip_literal and not registrable:
        raise BlockedURLError(
            "That host has no recognised public domain suffix.",
            detail=host,
            hint="Enter a public address such as https://example.com.",
            code="unrecognised_domain",
        )

    path = _clean_path(parts.path)
    kept = _strip_tracking(parts.query)

    netloc = f"{host}:{port}" if port is not None else host
    url = urlunsplit((scheme, netloc, path, urlencode(kept, doseq=True), ""))
    normalized = urlunsplit(
        (
            scheme,
            netloc,
            path.rstrip("/") or "/",
            urlencode(sorted(kept), doseq=True),
            "",
        )
    )

    if len(url) > MAX_URL_LENGTH or len(normalized) > MAX_URL_LENGTH:
        raise InvalidURLError(
            f"That address is too long (limit {MAX_URL_LENGTH} characters).",
            code="url_too_long",
        )

    return NormalizedURL(
        given=raw.strip(),
        url=url,
        normalized=normalized,
        scheme=scheme,
        host=host,
        port=port,
        path=path,
        query=urlencode(kept, doseq=True),
        registrable_domain=registrable,
        is_ip_literal=is_ip_literal,
    )


def same_registrable_domain(left: NormalizedURL, right: NormalizedURL) -> bool:
    """The crawler's same-site test.

    Compares registrable domains, so ``blog.example.com`` and ``www.example.com``
    are the same site while ``example.com.evil.test`` is not -- which a naive
    ``endswith`` check would get wrong.
    """
    if left.is_ip_literal or right.is_ip_literal:
        return left.host == right.host
    return bool(left.registrable_domain) and (
        left.registrable_domain == right.registrable_domain
    )


# --------------------------------------------------------------------- internals


def _ensure_scheme(candidate: str) -> str:
    """Add ``https://`` when the user typed a bare domain.

    People type ``example.com``, and rejecting that would be pedantic. The care
    needed is in *not* treating ``javascript:...`` as a bare domain: anything with
    a scheme-shaped prefix that is not a ``host:port`` is passed through so the
    scheme allowlist can reject it by name.
    """
    if "://" in candidate:
        return candidate
    match = _SCHEME_PREFIX.match(candidate)
    if match and not _PORT_PREFIX.match(match.group("rest")):
        return candidate  # keep the scheme so the allowlist rejects it explicitly
    return f"https://{candidate}"


def _split(candidate: str) -> SplitResult:
    try:
        return urlsplit(candidate)
    except ValueError as exc:  # malformed IPv6 literal, stray brackets
        raise InvalidURLError(
            "That address could not be parsed.", detail=str(exc)
        ) from exc


def _clean_host(parts: SplitResult) -> str:
    """Lowercase, IDNA-encode, and structurally validate the hostname."""
    try:
        host = parts.hostname  # lowercases, strips IPv6 brackets and userinfo
    except ValueError as exc:
        raise InvalidURLError(
            "That address could not be parsed.", detail=str(exc)
        ) from exc

    if not host:
        raise InvalidURLError(
            "That address is missing a domain name.",
            hint="For example: https://example.com",
        )

    # A single trailing dot is the DNS root form and is equivalent; drop it so it
    # cannot create a duplicate row.
    host = host.rstrip(".")
    if not host:
        raise InvalidURLError("That address is missing a domain name.")

    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise InvalidURLError(
                "That domain name could not be converted to a standard form.",
                detail=str(exc),
                code="invalid_idn",
            ) from exc

    if len(host) > MAX_HOST_LENGTH:
        raise InvalidURLError("That domain name is too long.", code="host_too_long")

    if _as_ip_address(host) is None:
        if not _HOST_CHARS.match(host):
            raise InvalidURLError(
                "That domain name contains characters that are not allowed.",
                code="invalid_host",
            )
        labels = host.split(".")
        if any(not label or len(label) > MAX_LABEL_LENGTH for label in labels):
            raise InvalidURLError(
                "That domain name is not structured correctly.", code="invalid_host"
            )
        if any(label.startswith("-") or label.endswith("-") for label in labels):
            raise InvalidURLError(
                "That domain name is not structured correctly.", code="invalid_host"
            )
    return host


def _clean_port(parts: SplitResult) -> int | None:
    """Return the explicit port, dropping the scheme's default."""
    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidURLError(
            "That address has an invalid port number.", code="invalid_port"
        ) from exc
    if port is None:
        return None
    if not 1 <= port <= 65535:
        raise InvalidURLError(
            "That address has an invalid port number.", code="invalid_port"
        )
    default = 443 if parts.scheme.lower() == "https" else 80
    return None if port == default else port


def _clean_path(path: str) -> str:
    """Normalise the path without changing which resource it names.

    Duplicate slashes are collapsed and an empty path becomes ``/``. Non-ASCII
    characters are percent-encoded so that a path pasted as ``/café`` and one
    pasted as ``/caf%C3%A9`` produce the same dedup key.

    Case is preserved: paths are case-sensitive on most servers, so lowercasing
    one would silently 404. ``%`` is in the safe set so existing escapes are left
    alone rather than double-encoded -- and deliberately *not* decoded first,
    because decoding ``%2F`` would turn an escaped character inside a segment into
    a real path separator and change which resource the URL names.
    """
    path = re.sub(r"/{2,}", "/", path or "")
    if not path.startswith("/"):
        path = f"/{path}"
    return quote(path, safe="/%:@!$&'()*+,;=~-._")


def _strip_tracking(query: str) -> list[tuple[str, str]]:
    """Drop analytics parameters, keep everything else.

    ``keep_blank_values`` is on because ``?print=`` can be meaningful, and
    dropping it would make two different pages share one key.
    """
    if not query:
        return []
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in TRACKING_PARAMS:
            continue
        if lowered.startswith(TRACKING_PARAM_PREFIXES):
            continue
        kept.append((key, value))
    return kept


def _as_ip_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _unwrap_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Resolve IPv6 transition formats to the address they actually reach.

    ``::ffff:127.0.0.1``, ``2002:7f00:1::``, and a Teredo address all carry an
    embedded IPv4 address. Checking the wrapper instead of the payload is how a
    loopback address gets past a naive guard.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        for embedded in (ip.ipv4_mapped, ip.sixtofour, (ip.teredo or (None, None))[1]):
            if embedded is not None:
                return embedded
    return ip


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for globally routable unicast addresses.

    Exported because the crawler applies the same test to every address a
    hostname resolves to, which is the half of the SSRF guard this module cannot
    do without touching DNS.
    """
    ip = _unwrap_ip(ip)
    if ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return False
    if ip.is_loopback or ip.is_link_local or ip.is_private:
        return False
    return ip.is_global


def _guard_host(host: str, *, allow_private_hosts: bool) -> None:
    """Reject hosts that must never be fetched (spec section 21)."""
    ip = _as_ip_address(host)
    if ip is not None:
        if allow_private_hosts or is_public_ip(ip):
            return
        raise BlockedURLError(
            "That address points to a private or internal network.",
            detail=host,
            hint="Enter the public web address of the site instead.",
            code="private_address",
        )

    if allow_private_hosts:
        return

    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_HOST_SUFFIXES):
        raise BlockedURLError(
            "That address is a local or reserved name, not a public website.",
            detail=host,
            hint="Enter the public web address of the site instead.",
            code="private_address",
        )
    if "." not in host:
        # A single-label name resolves through the machine's search domains, so
        # ``http://router`` or ``http://backend`` can reach an internal service.
        raise BlockedURLError(
            "That address is missing a domain suffix.",
            detail=host,
            hint="For example: https://example.com",
            code="private_address",
        )


def _registrable_domain(host: str) -> str:
    """``blog.example.co.uk`` -> ``example.co.uk``; unknown suffix -> ``""``."""
    return _extract(host).registered_domain.lower()


__all__ = [
    "ALLOWED_SCHEMES",
    "BLOCKED_HOSTNAMES",
    "BLOCKED_HOST_SUFFIXES",
    "BLOCKED_PORTS",
    "MAX_URL_LENGTH",
    "TRACKING_PARAMS",
    "NormalizedURL",
    "is_public_ip",
    "normalize_url",
    "same_registrable_domain",
]
