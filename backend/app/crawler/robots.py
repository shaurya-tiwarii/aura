"""A robots.txt parser following RFC 9309.

Why this is hand-written instead of using :mod:`urllib.robotparser`
------------------------------------------------------------------
Three reasons, in descending order of how much they matter:

1. **The stdlib parser fetches the URL itself.** ``RobotFileParser.read()`` calls
   ``urllib.request.urlopen``, which knows nothing about AURA's SSRF guard,
   timeouts, byte caps, or user agent. Handing it a URL would open a second,
   completely unguarded egress path out of the application -- the exact hole
   :mod:`app.utils.urls` exists to close. This parser takes *text* and never
   performs I/O, so every byte still arrives through
   :class:`~app.crawler.fetcher.SafeFetcher`.
2. **Its rule precedence is wrong per RFC 9309.** CPython's ``Entry.allowance``
   returns the *first* matching rule in file order. The RFC (section 2.2.2)
   requires the *most specific* -- longest -- matching path to win, with ``Allow``
   breaking ties. For a file like::

       Disallow: /blog
       Allow: /blog/public

   the stdlib says ``/blog/public`` is disallowed; every real crawler, and this
   one, says it is allowed. Getting that backwards means silently skipping pages
   the site owner explicitly published.
3. **Crawl-delay is not standard and it still matters.** It is absent from RFC
   9309 but widely deployed, and honouring it is the difference between a polite
   crawler and an impolite one (spec section 21). We read it, clamp it to a sane
   ceiling so a hostile or fat-fingered ``Crawl-delay: 86400`` cannot wedge a
   scan, and report what we did.

What is deliberately *not* supported
------------------------------------
``Host``, ``Clean-param``, and other vendor extensions are ignored, which is what
RFC 9309 section 2.2.4 requires of unrecognised fields. ``noindex`` in robots.txt
was never standard and Google dropped it in 2019; page-level ``meta robots`` is
read by the extractor instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

# RFC 9309 section 2.2.1: a crawler must not read an unbounded robots.txt. 500 KiB
# is Google's documented limit and is far more than any real file needs; anything
# past it is either a mistake or an attempt to make the parser the bottleneck.
MAX_ROBOTS_BYTES = 500 * 1024

# Crawl-delay ceiling. A site asking for 30s between requests is asking for a
# 20-page scan to take 12 minutes, which no user will wait for. We cap the delay
# and record ``delay_clamped`` so the scan can say honestly that it went faster
# than requested rather than pretending it complied.
MAX_CRAWL_DELAY_SECONDS = 5.0

_FIELD_RE = re.compile(r"^([A-Za-z-]+)\s*:\s*(.*)$")


@dataclass(frozen=True, slots=True)
class _Rule:
    """One Allow or Disallow line, pre-compiled for matching."""

    allow: bool
    pattern: str
    # Length of the raw pattern, which is RFC 9309's specificity measure. Computed
    # once here because it is compared on every path test.
    specificity: int
    regex: re.Pattern[str] | None  # only when the pattern uses * or $


@dataclass(slots=True)
class _Group:
    """A consecutive block of user-agent lines plus the rules that follow them."""

    agents: list[str] = field(default_factory=list)
    rules: list[_Rule] = field(default_factory=list)
    crawl_delay: float | None = None


def _compile(pattern: str, *, allow: bool) -> _Rule | None:
    """Turn one Allow/Disallow value into a matcher.

    Returns ``None`` for an empty Disallow value, which RFC 9309 section 2.2.2
    defines as "no restriction" -- a very common way to say "crawl everything",
    and a rule that must not be stored as a prefix match on ``""`` (which would
    match every path and disallow the whole site).
    """
    raw = pattern.strip()
    if not raw:
        return None
    # A path that does not start with "/" is malformed. Sites write
    # "Disallow: admin" meaning "/admin", so repair it rather than dropping the
    # rule -- dropping it would crawl something the owner asked us not to.
    if not raw.startswith("/"):
        raw = "/" + raw

    specificity = len(raw)
    regex: re.Pattern[str] | None = None
    if "*" in raw or raw.endswith("$"):
        anchored_end = raw.endswith("$")
        body = raw[:-1] if anchored_end else raw
        # Escape everything, then re-open the one metacharacter robots.txt
        # defines. Building the regex by hand instead would let a path containing
        # "(" or "+" become an invalid or, worse, an unintended pattern.
        expr = re.escape(body).replace(r"\*", ".*")
        regex = re.compile("^" + expr + ("$" if anchored_end else ""))
    return _Rule(allow=allow, pattern=raw, specificity=specificity, regex=regex)


def _normalise_path(path: str) -> str:
    """Percent-decode for comparison, keeping ``/`` and ``%`` intact.

    RFC 9309 section 2.2.2 compares paths after normalising percent-encoding, so
    ``/caf%C3%A9`` and ``/café`` are one page. ``%2F`` is left encoded on purpose:
    decoding it would invent a path separator that the server does not see.
    """
    if not path:
        return "/"
    placeholder = "\x00"
    guarded = path.replace("%2F", placeholder).replace("%2f", placeholder)
    decoded = unquote(guarded)
    return decoded.replace(placeholder, "%2F")


class RobotsFile:
    """The parsed contents of one robots.txt, or a permissive stand-in.

    Construct via :meth:`parse` for real content, or :meth:`allow_all` /
    :meth:`disallow_all` for the cases where no content was retrieved. Those two
    are separate constructors rather than a flag because the *reason* differs and
    RFC 9309 section 2.3.1 assigns them opposite meanings: a 404 means "no
    restrictions", a 401/403 means "you are not welcome here".
    """

    __slots__ = ("_groups", "_matched_group", "sitemaps", "source")

    def __init__(
        self,
        groups: list[_Group],
        sitemaps: list[str],
        source: str,
        user_agent: str,
    ) -> None:
        self._groups = groups
        self.sitemaps = sitemaps
        self.source = source
        self._matched_group = _select_group(groups, user_agent)

    # ---------------------------------------------------------------- builders
    @classmethod
    def parse(cls, text: str, *, user_agent: str) -> RobotsFile:
        return cls(*_parse(text), source="fetched", user_agent=user_agent)

    @classmethod
    def allow_all(cls, *, reason: str) -> RobotsFile:
        """No robots.txt, or one that could not be read -- crawl normally."""
        return cls([], [], source=reason, user_agent="*")

    @classmethod
    def disallow_all(cls, *, reason: str) -> RobotsFile:
        """Access to robots.txt was refused; treat the whole site as off limits."""
        group = _Group(agents=["*"], rules=[_Rule(False, "/", 1, None)])
        return cls([group], [], source=reason, user_agent="*")

    # ----------------------------------------------------------------- queries
    @property
    def crawl_delay(self) -> float | None:
        """The requested delay, clamped. ``None`` when the site did not ask."""
        if self._matched_group is None or self._matched_group.crawl_delay is None:
            return None
        return min(self._matched_group.crawl_delay, MAX_CRAWL_DELAY_SECONDS)

    @property
    def delay_clamped(self) -> bool:
        """True when the site asked for a longer delay than we honour."""
        if self._matched_group is None or self._matched_group.crawl_delay is None:
            return False
        return self._matched_group.crawl_delay > MAX_CRAWL_DELAY_SECONDS

    @property
    def has_rules(self) -> bool:
        return self._matched_group is not None and bool(self._matched_group.rules)

    def can_fetch(self, url: str) -> bool:
        """Whether the configured user agent may fetch *url*.

        Only the path and query are considered, per RFC 9309 section 2.2.2 -- the
        caller is responsible for having fetched the robots.txt belonging to this
        URL's own origin.
        """
        if self._matched_group is None:
            return True

        parts = urlsplit(url)
        target = _normalise_path(parts.path) or "/"
        if parts.query:
            target = f"{target}?{parts.query}"

        best: _Rule | None = None
        for rule in self._matched_group.rules:
            if not _matches(rule, target):
                continue
            # Longest pattern wins; on an exact tie Allow wins (RFC 9309 2.2.2).
            if (
                best is None
                or rule.specificity > best.specificity
                or (rule.specificity == best.specificity and rule.allow)
            ):
                best = rule
        return True if best is None else best.allow

    def matched_agent(self) -> str | None:
        """Which user-agent group applied, for the scan's audit trail."""
        if self._matched_group is None or not self._matched_group.agents:
            return None
        return self._matched_group.agents[0]


def _matches(rule: _Rule, target: str) -> bool:
    if rule.regex is not None:
        return rule.regex.match(target) is not None
    return target.startswith(_normalise_path(rule.pattern))


def _product_token(user_agent: str) -> str:
    """Reduce a full UA string to the token robots.txt groups are keyed on.

    ``AURABot/1.0 (+https://…)`` is matched against a file that says
    ``User-agent: aurabot``, so the version and the comment have to come off.
    """
    token = user_agent.strip().split("/")[0].split()[0] if user_agent.strip() else "*"
    return token.lower()


def _select_group(groups: list[_Group], user_agent: str) -> _Group | None:
    """Pick the single applicable group: most specific name match, else ``*``.

    RFC 9309 section 2.2.1 is explicit that exactly one group applies -- rules are
    *not* unioned across groups. A file that disallows everything for ``*`` and
    allows a path for ``AURABot`` must leave AURABot with only the second group's
    rules, or the specific permission is meaningless.
    """
    token = _product_token(user_agent)
    best: _Group | None = None
    best_len = -1
    wildcard: _Group | None = None

    for group in groups:
        for agent in group.agents:
            if agent == "*":
                # First wildcard group wins; later ones are duplicate records.
                if wildcard is None:
                    wildcard = group
                continue
            # Substring rather than equality: real files use "googlebot-news"
            # and "AURABot/1.0", and both should match the AURABot group.
            if (agent in token or token in agent) and len(agent) > best_len:
                best, best_len = group, len(agent)

    return best or wildcard


def _apply_rule_line(group: _Group, field_name: str, value: str) -> None:
    """Apply one Allow/Disallow/Crawl-delay line to the group being built."""
    if field_name in {"disallow", "allow"}:
        rule = _compile(value, allow=field_name == "allow")
        if rule is not None:
            group.rules.append(rule)
        elif field_name == "disallow":
            # "Disallow:" with an empty value means "nothing is disallowed". Any
            # earlier Disallow in the same group was presumably a mistake the author
            # then corrected, so clear the restrictions.
            group.rules = [r for r in group.rules if r.allow]
        return

    if field_name == "crawl-delay":
        try:
            delay = float(value)
        except ValueError:
            return
        if delay >= 0:
            group.crawl_delay = delay


def _parse(text: str) -> tuple[list[_Group], list[str]]:
    """Split robots.txt into groups plus the site-level Sitemap directives."""
    groups: list[_Group] = []
    sitemaps: list[str] = []
    current: _Group | None = None
    # A user-agent line directly after a rule line starts a *new* group; one after
    # another user-agent line extends the same group. This flag is what tells the
    # two apart, and getting it wrong merges unrelated rule sets together.
    accepting_agents = False

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        match = _FIELD_RE.match(line)
        if match is None:
            continue

        field_name = match.group(1).strip().lower()
        value = match.group(2).strip()

        if field_name == "user-agent":
            if not value:
                continue
            if current is None or not accepting_agents:
                current = _Group()
                groups.append(current)
                accepting_agents = True
            current.agents.append(value.lower())
            continue

        if field_name == "sitemap":
            # Site-level, not part of any group: valid anywhere in the file.
            if value:
                sitemaps.append(value)
            continue

        if current is None:
            # A rule before any user-agent line belongs to no group. RFC 9309
            # section 2.2.1 says to ignore it rather than guess at "*".
            continue

        accepting_agents = False
        _apply_rule_line(current, field_name, value)

    return groups, sitemaps


__all__ = [
    "MAX_CRAWL_DELAY_SECONDS",
    "MAX_ROBOTS_BYTES",
    "RobotsFile",
]
