"""Executable proof for the SEO scoring engine (Module 6 / spec section 6, 24).

Like every other ``verify_*`` script this is a proof, not a unit test: it runs the
real ruleset, the real pure scorer, the real Alembic migrations, the real ASGI app
through ``TestClient``, and the real ``execute_scan`` worker path against a site
served by ``respx`` (no byte leaves the machine). It asserts the *honesty* claims
Module 6 makes:

* every deduction traces to a ``rule_id`` in the ruleset, and the headline score
  reconstructs exactly from the per-issue points -- within a category
  ``sum(points_deducted) == available - earned``, each category normalises to
  ``round(earned/available*100, 1)``, and the overall is the weighted mean of the
  category scores (spec section 24). No number appears that its evidence cannot
  reproduce;
* the score is deterministic -- the same pages score identically twice;
* the engine is genuine application logic, not a Gemini wrapper (spec section 36):
  its source imports no language-model client at all;
* nothing is fabricated -- only pages that actually returned HTML are scored, an
  un-scorable crawl produces *no* report rather than an invented 100, a category
  with no applicable evidence is honestly "nothing to fix" rather than a penalty,
  and the grade is exactly the band the number falls in;
* the stored report reconstructs from its own rows, the API returns issues
  most-severe-first, a scan with no report is a 404 (never a faked score), and the
  SEO route is behind the auth wall and tenant-isolated.

Run it:

    cd D:\\AURA\\backend
    ./.venv/Scripts/python.exe scripts/verify_seo.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
import uuid
from pathlib import Path

# --------------------------------------------------------------------------- env
# A throwaway on-disk SQLite database wired up before any app import, so the
# module-level engine binds to it. On disk (not in-memory) because the scan worker
# and each API request open their own SessionLocal connection.
_SCRATCH_DIR = Path(tempfile.mkdtemp(prefix="aura-verify-seo-"))
_SCRATCH_DB = _SCRATCH_DIR / "seo.db"

os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{_SCRATCH_DB.as_posix()}"
os.environ["AUTH_PROVIDER"] = "local"
os.environ["APP_ENV"] = "development"
os.environ["CRAWLER_DELAY_SECONDS"] = "0"

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import httpx  # noqa: E402
import respx  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

# App imports come after the env is set (config reads the environment on import).
from app.core.database import SessionLocal  # noqa: E402
from app.engines.seo import (  # noqa: E402
    CATEGORY_WEIGHTS,
    ENGINE_VERSION,
    RULES,
    score_pages,
)
from app.engines.seo.rules import CATEGORY_MAX_POINTS  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.enums import (  # noqa: E402
    PageFetchStatus,
    ScanStatus,
    ScanTrigger,
    ScoreGrade,
    SeoCategory,
    Severity,
)
from app.models.seo import SeoCategoryScore, SeoIssue, SeoReport  # noqa: E402
from app.models.website import Website, WebsitePage, WebsiteScan  # noqa: E402
from app.services import scan as scan_service  # noqa: E402

API = "/api/v1"
PASSWORD = "correct-horse-battery-staple"
DUMMY_ID = "00000000-0000-0000-0000-000000000000"

_failures: list[str] = []
_passes = 0


def check(label: str):
    """Run a check immediately; record and print PASS/FAIL. Mirrors verify_crawler."""

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
# Fixture pages, built directly as transient WebsitePage rows so every scored
# signal is controlled exactly. Using the real ORM class (not a stand-in) means a
# renamed column breaks this proof -- which is the point. The baseline is a
# healthy page on which every rule passes; overrides make specific rules fire.
# =============================================================================
def _page(**overrides: object) -> WebsitePage:
    base: dict[str, object] = {
        "url": "http://example.com/",
        "normalized_url": "http://example.com/",
        "depth": 0,
        "is_homepage": True,
        "fetch_status": PageFetchStatus.OK.value,
        "title": "Fresh Sourdough Bakery in Pune from Rye and Co",  # 46 chars, 30-60
        "title_length": 46,
        "meta_description": (
            "Rye and Co is a neighbourhood bakery in Pune serving fresh sourdough, "
            "croissants, and custom celebration cakes baked daily."  # ~122 chars
        ),
        "meta_description_length": 122,
        "has_viewport_meta": True,
        "has_open_graph": True,
        "word_count": 420,
        "keyword_usage": {"bakery": 4, "sourdough": 3},
        "meta_robots": "index,follow",
        "canonical_url": "http://example.com/",
        "lang": "en",
        "response_time_ms": 180,
        "h1_count": 1,
        "h2_count": 4,
        "heading_hierarchy_valid": True,
        "broken_link_count": 0,
        "internal_link_count": 6,
        "has_schema_markup": True,
        "schema_types": ["Organization", "WebPage"],
        "image_count": 4,
        "images_missing_alt": 0,
        "readability_score": 68.0,
        "reading_grade_level": 8.5,
    }
    base.update(overrides)
    return WebsitePage(**base)  # type: ignore[arg-type]


# The overrides that make a page fail (almost) every page-scoped rule at once.
_BAD: dict[str, object] = {
    "title": "",
    "title_length": 0,
    "meta_description": "",
    "meta_description_length": 0,
    "has_viewport_meta": False,
    "has_open_graph": False,
    "word_count": 50,  # thin (< 150): thin_content fires, low_word_count N/A
    "keyword_usage": {},
    "meta_robots": "noindex,follow",
    "canonical_url": "",
    "lang": "",
    "response_time_ms": 5000,
    "h1_count": 0,  # h1_missing fires; multiple_h1 / hierarchy N/A
    "h2_count": 0,
    "heading_hierarchy_valid": False,
    "broken_link_count": 2,
    "internal_link_count": 0,
    "has_schema_markup": False,
    "schema_types": [],
    "image_count": 3,
    "images_missing_alt": 3,
    "readability_score": 20.0,
    "reading_grade_level": 18.0,
}

# The rule_ids a two-page all-bad site on http with no robots/sitemap/schema must
# fire -- and, by omission, the six that must stay N/A (title_length,
# description_length, low_word_count, multiple_h1, heading_hierarchy,
# no_subheadings), each because a more specific rule already owns that failure or
# the page is too short to judge.
_EXPECTED_BAD_RULES = {
    "metadata.title_missing",
    "metadata.description_missing",
    "metadata.viewport_missing",
    "metadata.open_graph_missing",
    "content.thin_content",
    "content.no_target_keywords",
    "technical.not_https",
    "technical.no_robots_txt",
    "technical.no_sitemap",
    "technical.noindex",
    "technical.canonical_missing",
    "technical.lang_missing",
    "technical.slow_response",
    "structure.h1_missing",
    "links.broken_links",
    "links.no_internal_links",
    "schema.no_structured_data",
    "schema.no_organization",
    "images.missing_alt",
    "readability.hard_to_read",
    "readability.grade_too_high",
}


def _healthy_site() -> object:
    return score_pages(
        [_page()],
        site_url="https://example.com/",
        robots_found=True,
        sitemap_found=True,
        keywords=["bakery", "sourdough"],
    )


def _bad_site() -> object:
    home = _page(**_BAD)
    child = _page(
        url="http://example.com/menu",
        normalized_url="http://example.com/menu",
        depth=1,
        is_homepage=False,
        **_BAD,
    )
    return score_pages(
        [home, child],
        site_url="http://example.com/",  # not https
        robots_found=False,
        sitemap_found=False,
        keywords=["bakery"],
    )


# =============================================================================
# respx site for the full pipeline (execute_scan). A small two-page site, served
# over http, deliberately without Open Graph or JSON-LD so the stored report has
# a spread of issue severities to sort. example.com is RFC 2606 reserved: it
# parses to a real public suffix and passes the SSRF guard yet is unregistrable.
# =============================================================================
_LONG_PARA = (
    "<p>Rye and Co is a neighbourhood bakery in Pune baking fresh sourdough and "
    "celebration cakes every morning by hand, using organic stone-milled flour and "
    "a long slow overnight fermentation that develops real flavour and makes the "
    "bread easy to digest for the families who visit our small corner shop.</p>"
) * 2


def _pipe_html(path: str, title: str) -> str:
    return (
        f'<!doctype html><html lang="en"><head>'
        f"<title>{title}</title>"
        f'<meta name="description" content="Rye and Co is a neighbourhood bakery in '
        f"Pune serving fresh sourdough, croissants, and custom celebration cakes "
        f'baked fresh every day.">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<link rel="canonical" href="http://example.com{path}">'
        f"</head><body><main><h1>{title}</h1>{_LONG_PARA}"
        f"<h2>Our story</h2><p>We have baked here for years.</p>"
        f'<a href="/">Home</a> <a href="/about">About</a>'
        f"</main></body></html>"
    )


_ROBOTS = "User-agent: *\nAllow: /\nSitemap: http://example.com/sitemap.xml\n"
_SITEMAP = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>http://example.com/</loc></url>"
    "<url><loc>http://example.com/about</loc></url>"
    "</urlset>"
)


def _pipe_mock() -> respx.MockRouter:
    router = respx.mock(assert_all_called=False)
    router.get("http://example.com/robots.txt").mock(
        return_value=httpx.Response(
            200, content=_ROBOTS.encode(), headers={"content-type": "text/plain"}
        )
    )
    router.get("http://example.com/sitemap.xml").mock(
        return_value=httpx.Response(
            200, content=_SITEMAP.encode(), headers={"content-type": "application/xml"}
        )
    )
    router.get("http://example.com/").mock(
        return_value=httpx.Response(200, html=_pipe_html("/", "Rye and Co Bakery"))
    )
    router.get("http://example.com/about").mock(
        return_value=httpx.Response(200, html=_pipe_html("/about", "About Rye and Co"))
    )
    return router


# =============================================================================
def main() -> int:  # noqa: C901 - a linear suite of independent checks
    print(f"(scratch database: {_SCRATCH_DB})")
    state: dict[str, object] = {}

    # ------------------------------------------------ 1. weights are a partition
    @check("category weights sum to 1.0 and cover the eight SEO categories")
    def _weights() -> str:
        assert set(CATEGORY_WEIGHTS) == set(SeoCategory), "weights miss a category"
        total = sum(CATEGORY_WEIGHTS.values())
        assert abs(total - 1.0) < 1e-9, total
        assert len(RULES) == len({r.rule_id for r in RULES}), "duplicate rule_id"
        return f"{len(RULES)} rules, weights sum to {total:.4f}"

    # ------------------------------------------- 2. genuine engine, not a wrapper
    @check("the SEO engine source imports no language-model client (section 36)")
    def _no_llm() -> str:
        engine_dir = BACKEND_DIR / "app" / "engines" / "seo"
        forbidden = ("genai", "gemini", "generativeai", "openai", "anthropic")
        scanned = 0
        for path in sorted(engine_dir.glob("*.py")):
            text = path.read_text(encoding="utf-8").lower()
            for token in forbidden:
                assert token not in text, f"{path.name} mentions {token!r}"
            scanned += 1
        assert scanned >= 3, scanned
        return f"{scanned} engine files, none reference an LLM SDK"

    # ------------------------------------------------- 3. a clean site scores 100
    @check("a fully healthy page scores 100.0 (excellent) with zero issues")
    def _healthy() -> str:
        r = _healthy_site()
        assert r.overall_score == 100.0, r.overall_score
        assert r.grade == ScoreGrade.EXCELLENT.value, r.grade
        assert r.issue_count == 0 and not r.issues, r.issue_count
        assert r.engine_version == ENGINE_VERSION, r.engine_version
        assert all(c.normalized_score == 100.0 for c in r.categories)
        return "100.0/100, excellent, 0 issues"

    # ----------------------------------------- 4. known-bad fires exactly expected
    @check("known-bad pages fire exactly the expected rule_ids, no more no fewer")
    def _bad_rules() -> str:
        r = _bad_site()
        fired = {i.rule_id for i in r.issues}
        missing = _EXPECTED_BAD_RULES - fired
        extra = fired - _EXPECTED_BAD_RULES
        assert not missing, f"expected but not fired: {sorted(missing)}"
        assert not extra, f"fired but not expected: {sorted(extra)}"
        assert r.overall_score < 40.0, r.overall_score  # a wholly broken site
        assert r.grade == ScoreGrade.CRITICAL.value, r.grade
        return f"{len(fired)} rules fired; 6 correctly N/A; score {r.overall_score}"

    # ----------------------------------- 5. reconciliation from per-issue points
    @check("overall reconstructs from per-issue points_deducted (section 24)")
    def _reconcile() -> str:
        r = _bad_site()
        for cat in r.categories:
            deducted = round(
                sum(i.points_deducted for i in r.issues if i.category == cat.category),
                4,
            )
            assert abs((cat.points_available - cat.points_earned) - deducted) < 1e-9, (
                cat.category,
                cat.points_available,
                cat.points_earned,
                deducted,
            )
            assert cat.normalized_score == round(
                cat.points_earned / cat.points_available * 100, 1
            ), cat.category
        overall = round(sum(c.normalized_score * c.weight for c in r.categories), 1)
        assert overall == r.overall_score, (overall, r.overall_score)
        return "every category and the headline reproduce from the issues"

    # ------------------------------------------- 6. severity counts are honest
    @check("report severity counts match the issue list exactly")
    def _counts() -> str:
        r = _bad_site()
        by_sev = {s: 0 for s in ("critical", "high", "medium", "low")}
        for issue in r.issues:
            by_sev[issue.severity] += 1
        assert r.critical_count == by_sev["critical"], r.critical_count
        assert r.high_count == by_sev["high"], r.high_count
        assert r.medium_count == by_sev["medium"], r.medium_count
        assert r.low_count == by_sev["low"], r.low_count
        assert r.issue_count == len(r.issues), r.issue_count
        return f"c{r.critical_count}/h{r.high_count}/m{r.medium_count}/l{r.low_count}"

    # ------------------------------------------- 7. grade is the band it falls in
    @check("grade always equals the band the score falls into")
    def _grade() -> str:
        for r in (_healthy_site(), _bad_site()):
            assert r.grade == ScoreGrade.from_score(r.overall_score).value, r.grade
        return "healthy=excellent, broken=critical"

    # --------------------------------------------------- 8. every id is a real rule
    @check("every issue carries a rule_id that exists in the ruleset")
    def _valid_ids() -> str:
        valid = {r.rule_id for r in RULES}
        result = _bad_site()
        for issue in result.issues:
            assert issue.rule_id in valid, issue.rule_id
            # Site-scoped issues anchor to no page; page-scoped ones do.
            rule = next(r for r in RULES if r.rule_id == issue.rule_id)
            if rule.scope == "site":
                assert issue.anchor_normalized_url is None, issue.rule_id
            else:
                assert issue.anchor_normalized_url is not None, issue.rule_id
        return f"{len(result.issues)} issues, all rule_ids valid"

    # ------------------------------------------------------- 9. deterministic
    @check("scoring the same pages twice yields an identical result")
    def _deterministic() -> str:
        a, b = _bad_site(), _bad_site()
        assert a.overall_score == b.overall_score
        sig_a = [(i.rule_id, i.points_deducted, i.page_url) for i in a.issues]
        sig_b = [(i.rule_id, i.points_deducted, i.page_url) for i in b.issues]
        assert sig_a == sig_b, "issue lists differ between runs"
        return "identical overall and issue list"

    # -------------------------------- 10. only fetched pages count; empty raises
    @check("only analysable (fetched) pages are scored; an empty crawl raises")
    def _analysable_only() -> str:
        failed = _page(
            url="http://example.com/gone",
            normalized_url="http://example.com/gone",
            is_homepage=False,
            depth=1,
            fetch_status=PageFetchStatus.TIMEOUT.value,
        )
        r = score_pages(
            [_page(), failed],
            site_url="https://example.com/",
            robots_found=True,
            sitemap_found=True,
        )
        assert r.pages_analyzed == 1, r.pages_analyzed  # the failed page excluded

        for bad_input in ([], [failed]):
            try:
                score_pages(
                    bad_input,
                    site_url="https://example.com/",
                    robots_found=True,
                    sitemap_found=True,
                )
            except ValueError:
                continue
            raise AssertionError("expected ValueError for no analysable pages")
        return "failed fetch excluded; empty/all-failed -> ValueError"

    # ------------------------------------- 11. N/A category is "nothing to fix"
    @check("a category with no applicable evidence scores 100, not a penalty")
    def _na_category() -> str:
        page = _page(
            image_count=0,
            images_missing_alt=0,
            readability_score=None,
            reading_grade_level=None,
        )
        r = score_pages(
            [page],
            site_url="https://example.com/",
            robots_found=True,
            sitemap_found=True,
        )
        for name in ("images", "readability"):
            cat = next(c for c in r.categories if c.category == name)
            assert cat.normalized_score == 100.0, (name, cat.normalized_score)
            enum = SeoCategory(name)
            assert cat.points_available == CATEGORY_MAX_POINTS[enum], name
            assert all(
                v["applicable"] is False for v in cat.check_results.values()
            ), f"{name} checks should all be non-applicable"
        assert not any(i.category in {"images", "readability"} for i in r.issues)
        return "images + readability: full marks, marked non-applicable"

    # ============================ full pipeline: migrate, scan, score, read =====

    # --------------------------------------------------- 12. schema builds
    @check("alembic upgrade head builds the seo report tables")
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

        tables = set(inspect(engine).get_table_names())
        # Names read off the models, so a table rename can't quietly pass here.
        required = [m.__tablename__ for m in (SeoReport, SeoCategoryScore, SeoIssue)]
        for name in required:
            assert name in tables, f"{name} missing after upgrade head"
        return ", ".join(required)

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

    def _new_website(token: str) -> tuple[str, str]:
        resp = client.post(
            f"{API}/businesses",
            json={
                "name": f"Biz {uuid.uuid4().hex[:8]}",
                "primary_website_url": "http://example.com",
            },
            headers=_bearer(token),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        return body["id"], body["websites"][0]["id"]

    # ------------------------------- 13. execute_scan stores a report at 100%
    @check("execute_scan scores the crawl and finishes COMPLETED at 100%")
    def _pipeline() -> str:
        token = state["owner_token"]
        bid, wid = _new_website(token)
        with _pipe_mock(), SessionLocal() as session:
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
                keywords=["bakery", "sourdough"],
                allow_private_hosts=True,
            )
        state["scan"] = {
            "bid": bid,
            "wid": wid,
            "token": token,
            "scan_id": str(scan_id),
        }

        with SessionLocal() as session:
            scan = session.get(WebsiteScan, scan_id)
            assert scan.status == ScanStatus.COMPLETED.value, scan.status
            assert scan.progress_percent == 100, scan.progress_percent
            report = session.scalars(
                select(SeoReport).where(SeoReport.scan_id == scan_id)
            ).first()
            assert report is not None, "no report stored"
            assert 0.0 <= report.overall_score <= 100.0, report.overall_score
            assert report.pages_analyzed == 2, report.pages_analyzed
            assert report.engine_version == ENGINE_VERSION, report.engine_version
            n_cat = (
                session.query(SeoCategoryScore)
                .filter(SeoCategoryScore.report_id == report.id)
                .count()
            )
            assert n_cat == len(SeoCategory), n_cat
            state["scan"]["overall"] = report.overall_score  # type: ignore[index]
        return f"COMPLETED, {n_cat} categories, score in [0,100]"

    # ----------------------- 14. the stored report reconstructs from its rows
    @check("the stored report reconciles from its own category and issue rows")
    def _stored_reconcile() -> str:
        scan_id = uuid.UUID(state["scan"]["scan_id"])  # type: ignore[index]
        with SessionLocal() as session:
            report = session.scalars(
                select(SeoReport).where(SeoReport.scan_id == scan_id)
            ).first()
            cats = (
                session.query(SeoCategoryScore)
                .filter(SeoCategoryScore.report_id == report.id)
                .all()
            )
            issues = (
                session.query(SeoIssue).filter(SeoIssue.report_id == report.id).all()
            )
            for cat in cats:
                deducted = round(
                    sum(
                        i.points_deducted for i in issues if i.category == cat.category
                    ),
                    4,
                )
                assert (
                    abs((cat.points_available - cat.points_earned) - deducted) < 1e-9
                ), cat.category
            overall = round(sum(c.normalized_score * c.weight for c in cats), 1)
            assert overall == report.overall_score, (overall, report.overall_score)
            # Severity counts on the report match the stored issues.
            assert report.issue_count == len(issues), report.issue_count
        return f"{len(issues)} issues reconstruct overall {report.overall_score}"

    # --------------------------- 15. API returns the report, most-severe-first
    @check("GET /seo returns the report with issues ordered most-severe-first")
    def _api_report() -> str:
        s = state["scan"]  # type: ignore[assignment]
        base = f"{API}/businesses/{s['bid']}/websites/{s['wid']}/scans"  # type: ignore[index]
        h = _bearer(s["token"])  # type: ignore[index]
        resp = client.get(f"{base}/{s['scan_id']}/seo", headers=h)  # type: ignore[index]
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert abs(body["overall_score"] - s["overall"]) < 1e-9, body["overall_score"]  # type: ignore[index,operator]
        assert len(body["categories"]) == len(SeoCategory), len(body["categories"])
        assert body["issue_count"] == len(body["issues"]), body["issue_count"]

        issues = body["issues"]
        assert issues, "http site should surface at least one issue"
        keys = [(Severity(i["severity"]).rank, i["points_deducted"]) for i in issues]
        assert keys == sorted(keys, key=lambda k: (-k[0], -k[1])), "issues unsorted"
        # Every issue's rule_id is real and its evidence is present (section 24).
        valid = {r.rule_id for r in RULES}
        assert all(i["rule_id"] in valid for i in issues)
        return f"200, {len(issues)} issues, severity-ordered"

    # ---------------------------- 16. no report is a 404, never a faked score
    @check("a scan with no report returns 404 (no fabricated score)")
    def _not_scored() -> str:
        token = state["owner_token"]
        bid, wid = _new_website(token)
        with SessionLocal() as session:
            website = session.get(Website, uuid.UUID(wid))
            scan = scan_service.create_scan(session, website)  # QUEUED, never run
            session.commit()
            session.refresh(scan)
            scan_id = str(scan.id)
        state["unscored"] = {"bid": bid, "wid": wid, "scan_id": scan_id, "token": token}

        base = f"{API}/businesses/{bid}/websites/{wid}/scans"
        resp = client.get(f"{base}/{scan_id}/seo", headers=_bearer(token))
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found", resp.json()
        return "queued scan -> 404, no invented report"

    # ----------------------------- 17. the SEO route is behind the auth wall
    @check("GET /seo requires authentication")
    def _auth_wall() -> str:
        seo_routes = [
            r
            for r in app.routes
            if isinstance(r, APIRoute) and r.path.endswith("/scans/{scan_id}/seo")
        ]
        assert len(seo_routes) == 1, [r.path for r in seo_routes]
        path = (
            seo_routes[0]
            .path.replace("{business_id}", DUMMY_ID)
            .replace("{website_id}", DUMMY_ID)
            .replace("{scan_id}", DUMMY_ID)
        )
        resp = client.get(path)  # no Authorization header
        assert resp.status_code == 401, resp.status_code
        assert resp.json()["error"]["code"] == "not_authenticated"
        return "GET /seo unauthenticated -> 401"

    # ----------------------------------- 18. tenant isolation on the SEO route
    @check("a stranger gets 404 (never 403) for a report they do not own")
    def _tenant() -> str:
        s = state["scan"]  # type: ignore[assignment]
        base = f"{API}/businesses/{s['bid']}/websites/{s['wid']}/scans"  # type: ignore[index]
        resp = client.get(
            f"{base}/{s['scan_id']}/seo",  # type: ignore[index]
            headers=_bearer(state["stranger_token"]),
        )
        assert resp.status_code == 404, resp.status_code
        return "stranger -> 404 on a real, scored report"

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
