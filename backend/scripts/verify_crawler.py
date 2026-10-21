"""End-to-end verification of the crawler + scan API (Module 4 / spec section 21).

Like the other ``verify_*`` scripts this is an *executable proof*, not a unit
test: it runs the real Alembic migrations, the real ASGI app through
``TestClient``, the real ``SafeFetcher``/``Crawler`` against fixtures served by
``respx`` (so no byte ever leaves the machine), and the real ``execute_scan``
worker path via ``asyncio.run``. It then asserts the *honesty* claims Module 4
makes, the same way the earlier stages did:

* the SSRF guard actually refuses loopback / link-local / private / metadata
  hosts, *and* re-checks every redirect hop -- a 302 to the cloud metadata IP is
  blocked, not followed (spec section 21);
* the crawler stays on the registrable domain -- an off-site link and a
  robots-disallowed path are discovered but never fetched;
* a scan that cannot proceed is recorded honestly -- robots.txt 403 is a
  ``FAILED`` scan flagged ``robots_disallowed``, a homepage timeout is
  ``website_timeout``, a 5xx homepage is ``website_unreachable``, a mixed run is
  ``PARTIAL`` -- never a fabricated success;
* the aggregates a client reads back are reproducible from the per-page rows
  (section 24/25): the site-level image totals equal the sum of the page image
  counts, and the sitemap URL count equals the number of ``<loc>`` entries;
* the tenant boundary holds -- another account gets a 404 (never a 403) for a
  scan it does not own -- and every scan route is behind the auth wall.

Run it:

    cd D:\\AURA\\backend
    ./.venv/Scripts/python.exe scripts/verify_crawler.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
import uuid
from pathlib import Path

# --------------------------------------------------------------------------- env
# A throwaway on-disk SQLite database, wired up *before* any app import so the
# module-level engine in app.core.database binds to it. On disk rather than
# in-memory because the crawl worker opens its own SessionLocal connection and
# an in-memory database is per-connection.
_SCRATCH_DIR = Path(tempfile.mkdtemp(prefix="aura-verify-crawler-"))
_SCRATCH_DB = _SCRATCH_DIR / "crawler.db"

os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{_SCRATCH_DB.as_posix()}"
os.environ["AUTH_PROVIDER"] = "local"
os.environ["APP_ENV"] = "development"
# Politeness delay to zero so the crawl fixtures run instantly; the delay logic
# itself is exercised by its own clamp test in the fetcher, not here.
os.environ["CRAWLER_DELAY_SECONDS"] = "0"

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import asyncio  # noqa: E402

import httpx  # noqa: E402
import respx  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# App imports come after the env is set (config reads the environment on import).
from app.core.database import SessionLocal  # noqa: E402
from app.core.errors import BlockedURLError, InvalidURLError  # noqa: E402
from app.crawler.crawler import Crawler  # noqa: E402
from app.crawler.fetcher import SafeFetcher  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.enums import PageFetchStatus, ScanStatus, ScanTrigger  # noqa: E402
from app.models.website import Website, WebsitePage, WebsiteScan  # noqa: E402
from app.services import scan as scan_service  # noqa: E402
from app.utils.urls import is_public_ip, normalize_url  # noqa: E402

API = "/api/v1"
PASSWORD = "correct-horse-battery-staple"
DUMMY_ID = "00000000-0000-0000-0000-000000000000"

_failures: list[str] = []
_passes = 0


def check(label: str):
    """Run a check immediately; record and print PASS/FAIL. Mirrors verify_business."""

    def decorator(fn):
        global _passes
        try:
            detail = fn()
            suffix = f" -- {detail}" if detail else ""
            print(f"[PASS] {label}{suffix}")
            _passes += 1
        except Exception:  # noqa: BLE001 - a failed check must not abort the suite
            _failures.append(label)
            print(f"[FAIL] {label}")
            traceback.print_exc()
        return fn

    return decorator


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# =============================================================================
# HTML / robots / sitemap fixtures (all served from example.com by respx).
#
# example.com and example.net are RFC 2606 reserved domains: they parse to a
# real public suffix and pass the SSRF guard, yet can never be registered by a
# third party. The homepage links to four same-site pages, one robots-blocked
# path (/private/pricing), and one off-site host (example.net) -- so the crawl
# can prove it fetches the first set and refuses the last two.
# =============================================================================
HOME_HTML = """<!doctype html>
<html lang="en">
<head>
  <title>Rye &amp; Co Bakery &mdash; Fresh Sourdough in Pune</title>
  <meta name="description" content="Rye and Co is a neighbourhood bakery in Pune serving fresh sourdough, croissants, and custom celebration cakes baked daily.">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script type="application/ld+json">
  {"@context":"https://schema.org","@type":"Organization","name":"Rye & Co Bakery","url":"http://example.com/","telephone":"+91 20 1234 5678"}
  </script>
</head>
<body>
  <header><nav>
    <a href="/">Home</a>
    <a href="/about">About</a>
    <a href="/services">Services</a>
  </nav></header>
  <main>
    <h1>Fresh Sourdough, Baked in Pune Every Morning</h1>
    <p>Rye and Co is a small neighbourhood bakery in Pune. We bake naturally leavened
       sourdough, buttery croissants, and celebration cakes to order, using organic
       stone-milled flour and a fifty year old starter that we feed every single day.</p>
    <p>Every loaf is proved slowly overnight and baked fresh before the shop opens, so
       the bread you carry home in the morning was shaped by hand the night before by
       the same bakers who serve you at the counter.</p>
    <img src="/img/loaf.jpg" alt="A golden sourdough loaf resting on a wooden board">
    <img src="/img/oven.jpg">
    <p>Read our <a href="/about">story</a>, browse the full <a href="/services">list of
       services</a>, or read the latest <a href="/blog/post-1">entry in our baking
       journal</a>. Staff sign in through the <a href="/private/pricing">private
       portal</a>.</p>
    <p>We are proudly independent &mdash; say hello on
       <a href="https://example.net/ryeco">our community page</a> or email
       <a href="mailto:hello@example.com">hello@example.com</a>.</p>
  </main>
  <footer><p>Rye &amp; Co Bakery, Pune. Call +91 20 1234 5678.</p></footer>
</body>
</html>
"""


def _simple_page(title: str, body: str) -> str:
    return (
        f'<!doctype html><html lang="en"><head><title>{title}</title>'
        f'<meta name="description" content="{title} at Rye and Co Bakery in Pune.">'
        f"</head><body><main><h1>{title}</h1>{body}</main></body></html>"
    )


ABOUT_HTML = _simple_page(
    "About Rye & Co",
    "<p>Rye and Co began in a home kitchen in Pune in the year twenty sixteen and grew"
    " into the small corner bakery it is today, still run by the same two founders who"
    " bake every loaf themselves each morning without fail.</p>",
)
SERVICES_HTML = _simple_page(
    "Our Services",
    "<p>We bake sourdough, sandwich loaves, croissants, and made-to-order celebration"
    " cakes for weddings and birthdays, and we supply fresh bread wholesale to three"
    " neighbourhood cafes right across the city of Pune.</p>",
)
BLOG_HTML = _simple_page(
    "Why Slow Fermentation Matters",
    "<p>Slow fermentation gives sourdough its flavour and makes it easier to digest. In"
    " this journal entry we walk through the full overnight schedule we follow in our"
    " own bakery, hour by hour, from mixing to bake.</p>",
)

ROBOTS_TXT = (
    "User-agent: *\nDisallow: /private\nSitemap: http://example.com/sitemap.xml\n"
)

SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>http://example.com/</loc></url>
  <url><loc>http://example.com/about</loc></url>
  <url><loc>http://example.com/services</loc></url>
  <url><loc>http://example.com/blog/post-1</loc></url>
</urlset>
"""

PARTIAL_HOME_HTML = _simple_page(
    "Corner Bakery",
    "<p>We are a tiny bakery and this page links onward to a section of the site that"
    " is currently returning a server error, so a crawl of us should end up partial"
    ' rather than either clean or wholly failed.</p><a href="/broken">our menu</a>',
)


def _html(body: str) -> httpx.Response:
    return httpx.Response(200, html=body)


def _text(body: str, content_type: str) -> httpx.Response:
    return httpx.Response(
        200, content=body.encode(), headers={"content-type": content_type}
    )


def _mount_full_site(router: respx.MockRouter) -> None:
    """The happy-path four-page site with robots.txt and a sitemap."""
    router.get("http://example.com/robots.txt").mock(
        return_value=_text(ROBOTS_TXT, "text/plain; charset=utf-8")
    )
    router.get("http://example.com/sitemap.xml").mock(
        return_value=_text(SITEMAP_XML, "application/xml")
    )
    router.get("http://example.com/").mock(return_value=_html(HOME_HTML))
    router.get("http://example.com/about").mock(return_value=_html(ABOUT_HTML))
    router.get("http://example.com/services").mock(return_value=_html(SERVICES_HTML))
    router.get("http://example.com/blog/post-1").mock(return_value=_html(BLOG_HTML))


def _full_site_mock() -> respx.MockRouter:
    router = respx.mock(assert_all_called=False)
    _mount_full_site(router)
    return router


# =============================================================================
def main() -> int:  # noqa: C901 - a linear suite of independent checks
    print(f"(scratch database: {_SCRATCH_DB})")
    state: dict[str, object] = {}

    # ---------------------------------------------------------------- 1. schema
    @check("alembic upgrade head builds the scan schema")
    def _migrate() -> str:
        from sqlalchemy import inspect

        from alembic import command
        from alembic.config import Config
        from app.core.database import engine

        # No alembic.ini is shipped on purpose (DB URL comes from app settings,
        # never from a tracked file); run with a bare Config plus the script
        # location instead.
        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        command.upgrade(cfg, "head")

        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        for required in ("websites", "website_scans", "website_pages"):
            assert required in tables, f"{required} missing after upgrade head"

        scan_cols = {c["name"] for c in inspector.get_columns("website_scans")}
        for col in (
            "robots_disallowed",
            "hit_page_limit",
            "crawl_config",
            "site_analysis",
        ):
            assert col in scan_cols, f"website_scans.{col} missing"
        page_cols = {c["name"] for c in inspector.get_columns("website_pages")}
        for col in (
            "image_count",
            "images_missing_alt",
            "has_schema_markup",
            "schema_types",
        ):
            assert col in page_cols, f"website_pages.{col} missing"
        return f"{len(tables)} tables"

    # ---------------------------------------------------------- 2. two accounts
    app = create_app()
    client = TestClient(app, base_url="http://testserver")

    @check("register an owner and a stranger account")
    def _accounts() -> str:
        owner = client.post(
            f"{API}/auth/register",
            json={
                "email": "owner@bakery.example",
                "password": PASSWORD,
                "full_name": "Owner",
            },
        )
        assert owner.status_code == 201, owner.text
        stranger = client.post(
            f"{API}/auth/register",
            json={
                "email": "stranger@rival.example",
                "password": PASSWORD,
                "full_name": "Nosy",
            },
        )
        assert stranger.status_code == 201, stranger.text
        state["owner_token"] = owner.json()["tokens"]["access_token"]
        state["stranger_token"] = stranger.json()["tokens"]["access_token"]
        return "owner + stranger"

    def _new_website(
        token: str, primary_url: str = "http://example.com"
    ) -> tuple[str, str]:
        """Create a fresh business (with its primary website) and return (bid, wid)."""
        resp = client.post(
            f"{API}/businesses",
            json={
                "name": f"Biz {uuid.uuid4().hex[:8]}",
                "primary_website_url": primary_url,
            },
            headers=_bearer(token),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        return body["id"], body["websites"][0]["id"]

    # -------------------------------------------------- 3. SSRF: normalize_url
    @check("normalize_url refuses hostile addresses (spec section 21)")
    def _ssrf_normalize() -> str:
        blocked = [
            "http://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
            "http://localhost/",
            "http://127.0.0.1/",
            "http://10.0.0.1/",
            "http://192.168.1.1/",
            "http://[::1]/",  # IPv6 loopback
            "http://[::ffff:127.0.0.1]/",  # IPv4-mapped IPv6 loopback
            "http://metadata.google.internal/",
            "http://0.0.0.0/",
            "http://example.com:22/",  # non-web port
        ]
        for url in blocked:
            try:
                normalize_url(url)  # allow_private defaults False in this env
            except BlockedURLError:
                continue
            raise AssertionError(f"expected BlockedURLError for {url!r}")

        invalid = [
            "javascript:alert(1)",
            "file:///etc/passwd",
            "http://user:pass@example.com/",
        ]
        for url in invalid:
            try:
                normalize_url(url)
            except InvalidURLError:
                continue
            raise AssertionError(f"expected InvalidURLError for {url!r}")
        return f"{len(blocked)} blocked, {len(invalid)} invalid"

    # ------------------------------------------------------ 4. SSRF: is_public_ip
    @check("is_public_ip accepts only globally routable addresses")
    def _public_ip() -> str:
        import ipaddress

        for good in ("93.184.216.34", "8.8.8.8"):
            assert is_public_ip(ipaddress.ip_address(good)), good
        for bad in (
            "169.254.169.254",
            "::1",
            "10.0.0.1",
            "127.0.0.1",
            "::ffff:10.0.0.1",
        ):
            assert not is_public_ip(ipaddress.ip_address(bad)), bad
        return "public accepted, private/loopback/mapped rejected"

    # ------------------------------------------- 5. SSRF re-checked on redirect
    @check("SafeFetcher blocks a 302 to the metadata endpoint (per-hop re-check)")
    def _ssrf_redirect() -> str:
        async def _probe() -> object:
            async with SafeFetcher(
                delay_seconds=0, timeout_seconds=5, allow_private_hosts=False
            ) as fetcher:
                # A public IP literal that a real DNS lookup is not needed for; it
                # 302s to the link-local metadata address, which must be refused.
                return await fetcher.fetch("http://93.184.216.34/")

        with respx.mock(assert_all_called=False) as router:
            router.get("http://93.184.216.34/").mock(
                return_value=httpx.Response(
                    302, headers={"location": "http://169.254.169.254/"}
                )
            )
            result = asyncio.run(_probe())

        assert result.status is PageFetchStatus.BLOCKED, result.status
        assert result.redirect_count == 1, result.redirect_count
        return "302 -> 169.254.169.254 refused, recorded as BLOCKED"

    # ------------------------------------------- 6. crawler stays on the domain
    @check("Crawler stays on-domain and honours robots.txt + sitemap")
    def _crawl() -> str:
        progress: list[int] = []

        def collect(percent: int, message: str) -> None:
            progress.append(percent)

        async def _run() -> object:
            crawler = Crawler(
                allow_private_hosts=True, delay_seconds=0, progress=collect
            )
            return await crawler.crawl(
                "http://example.com/", keywords=["sourdough", "bakery"]
            )

        with _full_site_mock():
            result = asyncio.run(_run())

        assert result.site_domain == "example.com", result.site_domain
        assert result.pages_crawled == 4, result.pages_crawled
        assert len(result.pages) == 4 and all(p.ok for p in result.pages)

        fetched = {p.url.normalized for p in result.pages}
        assert "http://example.com/" in fetched
        assert not any("/private" in u for u in fetched), fetched
        assert not any("example.net" in u for u in fetched), fetched

        assert result.robots.found is True
        assert result.sitemap.found is True
        assert result.sitemap.url_count == 4, result.sitemap.url_count
        assert result.hit_page_limit is False

        # Progress is monotonic and the crawl phase never exceeds its 0-70 budget.
        assert progress, "no progress reported"
        assert progress == sorted(progress), progress
        assert max(progress) <= 70, progress

        home = result.homepage
        assert home is not None and home.extraction is not None
        ext = home.extraction
        assert ext.image_count == 2, ext.image_count
        assert ext.images_missing_alt == 1, ext.images_missing_alt
        assert "Organization" in ext.schema_types, ext.schema_types
        assert ext.title, "homepage title empty"
        return "4 pages; /private + example.net skipped; sitemap=4"

    # --------------------------------------- 7. execute_scan persists the crawl
    @check("execute_scan persists a full crawl as COMPLETED")
    def _persist() -> str:
        token = state["owner_token"]
        bid, wid = _new_website(token)
        state["completed"] = {"bid": bid, "wid": wid, "token": token}

        with _full_site_mock(), SessionLocal() as session:
            website = session.get(Website, uuid.UUID(wid))
            scan = scan_service.create_scan(
                session, website, trigger=ScanTrigger.MANUAL
            )
            session.commit()
            session.refresh(scan)
            scan_id = scan.id
            scan_service.execute_scan(
                session,
                scan,
                keywords=["sourdough", "bakery"],
                allow_private_hosts=True,
            )
        state["completed"]["scan_id"] = str(scan_id)  # type: ignore[index]

        with SessionLocal() as session:
            scan = session.get(WebsiteScan, scan_id)
            assert scan.status == ScanStatus.COMPLETED.value, scan.status
            assert scan.sequence == 1, scan.sequence
            assert scan.pages_crawled == 4, scan.pages_crawled
            assert scan.pages_failed == 0, scan.pages_failed
            assert scan.robots_disallowed is False

            pages = (
                session.query(WebsitePage).filter(WebsitePage.scan_id == scan_id).all()
            )
            assert len(pages) == 4, len(pages)
            homepage = next(p for p in pages if p.is_homepage)
            assert homepage.title, "homepage title not persisted"
            assert "Organization" in homepage.schema_types, homepage.schema_types
            assert homepage.image_count == 2, homepage.image_count
            assert homepage.images_missing_alt == 1, homepage.images_missing_alt

            # /private and the off-site host were never persisted.
            urls = {p.normalized_url for p in pages}
            assert not any("/private" in u for u in urls), urls
            assert not any("example.net" in u for u in urls), urls

            sa = scan.site_analysis
            assert sa["pages_analysed"] == 4, sa.get("pages_analysed")
            assert sa["pages_attempted"] == 4, sa.get("pages_attempted")

            website = session.get(Website, uuid.UUID(wid))
            assert website.robots_txt_found is True
            assert website.sitemap_found is True
            assert website.sitemap_url_count == 4, website.sitemap_url_count
            assert website.last_scan_at is not None
        return "COMPLETED, 4 pages, website cache refreshed"

    # ------------------------------------- 8. aggregates are not fabricated
    @check("site-level aggregates reproduce from the per-page rows (section 25)")
    def _no_fabrication() -> str:
        completed = state["completed"]  # type: ignore[assignment]
        scan_id = uuid.UUID(completed["scan_id"])  # type: ignore[index]
        with SessionLocal() as session:
            scan = session.get(WebsiteScan, scan_id)
            pages = (
                session.query(WebsitePage).filter(WebsitePage.scan_id == scan_id).all()
            )
            sa = scan.site_analysis

            assert sum(p.image_count for p in pages) == sa["total_images"], sa[
                "total_images"
            ]
            assert (
                sum(p.images_missing_alt for p in pages) == sa["images_missing_alt"]
            ), sa["images_missing_alt"]
            assert sa["sitemap"]["url_count"] == 4, sa["sitemap"]
        return "image totals and sitemap count match the rows"

    # ----------------------------------------- 9. robots.txt 403 fails honestly
    @check("robots.txt 403 records a FAILED scan flagged robots_disallowed")
    def _robots_block() -> str:
        token = state["owner_token"]
        _bid, wid = _new_website(token, primary_url="http://example.net")

        with respx.mock(assert_all_called=False) as router:
            router.get("http://example.net/robots.txt").mock(
                return_value=httpx.Response(403)
            )
            router.get("http://example.net/").mock(return_value=_html(HOME_HTML))
            with SessionLocal() as session:
                website = session.get(Website, uuid.UUID(wid))
                scan = scan_service.create_scan(session, website)
                session.commit()
                session.refresh(scan)
                scan_id = scan.id
                scan_service.execute_scan(session, scan, allow_private_hosts=True)

        with SessionLocal() as session:
            scan = session.get(WebsiteScan, scan_id)
            assert scan.status == ScanStatus.FAILED.value, scan.status
            assert scan.robots_disallowed is True
            assert scan.error_code == "crawler_blocked", scan.error_code
            assert scan.pages_crawled == 0, scan.pages_crawled
            n_pages = (
                session.query(WebsitePage)
                .filter(WebsitePage.scan_id == scan_id)
                .count()
            )
            assert n_pages == 0, n_pages
        return "FAILED, robots_disallowed, 0 pages"

    def _run_scan_with(
        mock_router: respx.MockRouter, primary_url: str = "http://example.com"
    ):
        """Create a website, run a full execute_scan under *mock_router*, return the row."""
        token = state["owner_token"]
        _bid, wid = _new_website(token, primary_url=primary_url)
        with mock_router, SessionLocal() as session:
            website = session.get(Website, uuid.UUID(wid))
            scan = scan_service.create_scan(session, website)
            session.commit()
            session.refresh(scan)
            scan_id = scan.id
            scan_service.execute_scan(session, scan, allow_private_hosts=True)
        with SessionLocal() as session:
            return session.get(WebsiteScan, scan_id)

    def _mock_no_sitemap(router: respx.MockRouter) -> None:
        """robots.txt allows all but names no Sitemap, so the crawler probes every
        conventional path. All five must be mocked or respx (assert_all_mocked)
        raises an unmatched-request error that would escape crawl() and mask the
        homepage-failure code under test. 404 on each => 'site has no sitemap'."""
        router.get("http://example.com/robots.txt").mock(
            return_value=httpx.Response(404)
        )
        for path in (
            "/sitemap.xml",
            "/sitemap_index.xml",
            "/sitemap-index.xml",
            "/wp-sitemap.xml",
            "/sitemap.xml.gz",
        ):
            router.get(f"http://example.com{path}").mock(
                return_value=httpx.Response(404)
            )

    # ------------------------------------------ 10. homepage timeout -> FAILED
    @check("homepage timeout -> FAILED website_timeout")
    def _timeout() -> str:
        router = respx.mock(assert_all_called=False)
        _mock_no_sitemap(router)
        router.get("http://example.com/").mock(side_effect=httpx.ConnectTimeout("slow"))
        scan = _run_scan_with(router)
        assert scan.status == ScanStatus.FAILED.value, scan.status
        assert scan.error_code == "website_timeout", scan.error_code
        assert scan.pages_crawled == 0
        return scan.error_code

    # ------------------------------------- 11. homepage 5xx -> FAILED unreachable
    @check("homepage 5xx -> FAILED website_unreachable")
    def _server_error() -> str:
        router = respx.mock(assert_all_called=False)
        _mock_no_sitemap(router)
        router.get("http://example.com/").mock(return_value=httpx.Response(500))
        scan = _run_scan_with(router)
        assert scan.status == ScanStatus.FAILED.value, scan.status
        assert scan.error_code == "website_unreachable", scan.error_code
        return scan.error_code

    # ------------------------------------------- 12. one page fails -> PARTIAL
    @check("one page fails -> PARTIAL (some analysed, some failed)")
    def _partial() -> str:
        router = respx.mock(assert_all_called=False)
        _mock_no_sitemap(router)
        router.get("http://example.com/").mock(return_value=_html(PARTIAL_HOME_HTML))
        router.get("http://example.com/broken").mock(return_value=httpx.Response(500))
        scan = _run_scan_with(router)
        assert scan.status == ScanStatus.PARTIAL.value, scan.status
        assert scan.pages_crawled >= 1, scan.pages_crawled
        assert scan.pages_failed >= 1, scan.pages_failed
        return f"crawled={scan.pages_crawled} failed={scan.pages_failed}"

    # ------------------------------------------------- 13. routes + auth wall
    @check("scan API exposes 9 routes, every one behind the auth wall")
    def _auth_wall() -> str:
        scan_routes = [
            r for r in app.routes if isinstance(r, APIRoute) and "/scans" in r.path
        ]
        assert len(scan_routes) == 9, [r.path for r in scan_routes]

        checked = 0
        for route in scan_routes:
            path = (
                route.path.replace("{business_id}", DUMMY_ID)
                .replace("{website_id}", DUMMY_ID)
                .replace("{scan_id}", DUMMY_ID)
                .replace("{page_id}", DUMMY_ID)
            )
            for method in route.methods & {"GET", "POST", "DELETE", "PATCH", "PUT"}:
                resp = client.request(method, path)  # no Authorization header
                assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"
                assert resp.json()["error"]["code"] == "not_authenticated"
                checked += 1
        return f"8 routes, {checked} method/path pairs -> 401"

    # ---------------------------------------- 14. API reads reproduce the scan
    @check("scan API reads reproduce the stored scan")
    def _api_reads() -> str:
        c = state["completed"]  # type: ignore[assignment]
        bid, wid, token = c["bid"], c["wid"], c["token"]  # type: ignore[index]
        scan_id = c["scan_id"]  # type: ignore[index]
        base = f"{API}/businesses/{bid}/websites/{wid}/scans"
        h = _bearer(token)

        listed = client.get(base, headers=h)
        assert listed.status_code == 200 and len(listed.json()) >= 1

        latest = client.get(f"{base}/latest", headers=h)
        assert latest.status_code == 200, latest.text
        assert latest.json()["site_analysis"]["pages_analysed"] == 4

        detail = client.get(f"{base}/{scan_id}", headers=h)
        assert detail.status_code == 200
        assert detail.json()["status"] == "completed"

        pages = client.get(f"{base}/{scan_id}/pages", headers=h)
        assert pages.status_code == 200 and len(pages.json()) == 4
        home = next(p for p in pages.json() if p["is_homepage"])

        page_detail = client.get(f"{base}/{scan_id}/pages/{home['id']}", headers=h)
        assert page_detail.status_code == 200, page_detail.text
        pd = page_detail.json()
        assert pd["image_count"] == 2 and len(pd["images"]) == 2
        assert pd["heading_outline"], "empty heading outline"
        assert "Organization" in pd["schema_types"]
        return "list/latest/detail/pages/page-detail all reproduce"

    # ------------------------- 15. POST queues, sources keywords, dup -> 409
    @check("POST queues a scan with profile keywords; duplicate -> 409")
    def _post_scan() -> str:
        token = state["owner_token"]
        bid, wid = _new_website(token)
        # Keywords live on the profile, not the request body -- set them, then assert
        # the route reads them and hands them to the worker.
        prof = client.patch(
            f"{API}/businesses/{bid}/profile",
            json={"keywords": ["sourdough", "bakery"]},
            headers=_bearer(token),
        )
        assert prof.status_code == 200, prof.text

        recorded: dict[str, object] = {}
        original = scan_service.dispatch_scan

        def _fake_dispatch(scan_id, *, keywords=None):
            recorded["scan_id"] = scan_id
            recorded["keywords"] = keywords

        scan_service.dispatch_scan = _fake_dispatch  # type: ignore[assignment]
        try:
            base = f"{API}/businesses/{bid}/websites/{wid}/scans"
            first = client.post(base, headers=_bearer(token))
            assert first.status_code == 202, first.text
            assert first.json()["status"] == "queued", first.json()["status"]
            assert recorded["keywords"] == ["sourdough", "bakery"], recorded["keywords"]

            second = client.post(base, headers=_bearer(token))
            assert second.status_code == 409, second.text
            assert second.json()["error"]["code"] == "scan_already_running"
        finally:
            scan_service.dispatch_scan = original  # type: ignore[assignment]

        state["queued"] = {
            "bid": bid,
            "wid": wid,
            "scan_id": first.json()["id"],
            "token": token,
        }
        return "202 queued, keywords from profile, 2nd -> 409"

    # ----------------------------------------------- 16. tenant isolation (404)
    @check("another account gets 404 (never 403) for a scan it does not own")
    def _tenant_isolation() -> str:
        c = state["completed"]  # type: ignore[assignment]
        bid, wid, scan_id = c["bid"], c["wid"], c["scan_id"]  # type: ignore[index]
        h = _bearer(state["stranger_token"])
        base = f"{API}/businesses/{bid}/websites/{wid}/scans"
        for method, path in [
            ("GET", base),
            ("GET", f"{base}/latest"),
            ("GET", f"{base}/{scan_id}"),
            ("GET", f"{base}/{scan_id}/pages"),
            ("DELETE", f"{base}/{scan_id}"),
        ]:
            resp = client.request(method, path, headers=h)
            assert resp.status_code == 404, f"{method} {path} -> {resp.status_code}"
        return "5 routes -> 404 for the stranger"

    # ------------------------------------------------------- 17. delete rules
    @check("delete refuses a running scan (409) and cascades a finished one (204)")
    def _delete_rules() -> str:
        q = state["queued"]  # type: ignore[assignment]
        qh = _bearer(q["token"])  # type: ignore[index]
        qbase = f"{API}/businesses/{q['bid']}/websites/{q['wid']}/scans"  # type: ignore[index]
        blocked = client.delete(f"{qbase}/{q['scan_id']}", headers=qh)  # type: ignore[index]
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["error"]["code"] == "scan_in_progress"

        c = state["completed"]  # type: ignore[assignment]
        ch = _bearer(c["token"])  # type: ignore[index]
        cbase = f"{API}/businesses/{c['bid']}/websites/{c['wid']}/scans"  # type: ignore[index]
        deleted = client.delete(f"{cbase}/{c['scan_id']}", headers=ch)  # type: ignore[index]
        assert deleted.status_code == 204, deleted.text

        # The scan is gone, and its pages cascaded with it.
        gone = client.get(f"{cbase}/{c['scan_id']}", headers=ch)  # type: ignore[index]
        assert gone.status_code == 404
        pages_gone = client.get(f"{cbase}/{c['scan_id']}/pages", headers=ch)  # type: ignore[index]
        assert pages_gone.status_code == 404
        with SessionLocal() as session:
            remaining = (
                session.query(WebsitePage)
                .filter(WebsitePage.scan_id == uuid.UUID(c["scan_id"]))  # type: ignore[index]
                .count()
            )
            assert remaining == 0, remaining
        return "queued -> 409, completed -> 204, pages cascaded"

    total = _passes + len(_failures)
    print(f"\n{_passes}/{total} checks passed")
    if _failures:
        print("FAILED:")
        for label in _failures:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
