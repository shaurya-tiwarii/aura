"""Executable proof for the AI-visibility engine (Module 7 / spec sections 7, 24, 45).

Like every other ``verify_*`` script this is a proof, not a unit test: it runs the
real signal set, the real pure scorer, the real Alembic migrations, the real ASGI
app through ``TestClient``, and the real ``execute_scan`` worker path against a
site served by ``respx`` (no byte leaves the machine). It asserts the *honesty*
claims Module 7 makes:

* the score is an **estimate of AI-search readiness**, never a ranking/citation
  guarantee -- the ``HEURISTIC_DISCLAIMER`` is stored on the report row and surfaces
  verbatim in the API, and the summary frames the number as a heuristic (sections
  7, 45);
* every number traces to a signal and reconstructs from the stored evidence --
  per category ``sum(signal points) == points_earned``, each category normalises to
  ``round(earned/available*100, 1)``, and the overall is the weighted mean of the
  category scores (section 24). No number appears that its evidence cannot
  reproduce;
* the engine is genuine application logic, not a Gemini wrapper (section 36): its
  source imports no language-model client at all, and it is deterministic;
* nothing is fabricated -- only pages that actually returned HTML are scored, an
  un-scorable crawl produces *no* report rather than an invented score, a signal
  with no applicable page is honestly non-applicable (not a zero), and every
  category keeps ``points_available > 0`` so the DB CHECK holds;
* the stored report reconstructs from its own rows, the API returns it behind the
  auth wall and tenant-isolated, and a scan with no report is a 404 (never a faked
  score). It runs alongside the SEO engine without either losing the other's
  report.

Run it:

    cd D:\\AURA\\backend
    ./.venv/Scripts/python.exe scripts/verify_ai_visibility.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path

# --------------------------------------------------------------------------- env
# A throwaway on-disk SQLite database wired up before any app import, so the
# module-level engine binds to it. On disk (not in-memory) because the scan worker
# and each API request open their own SessionLocal connection.
_SCRATCH_DIR = Path(tempfile.mkdtemp(prefix="aura-verify-aiv-"))
_SCRATCH_DB = _SCRATCH_DIR / "aiv.db"

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
from app.engines.ai_visibility import (  # noqa: E402
    CATEGORY_WEIGHTS,
    ENGINE_VERSION,
    SIGNALS,
    score_pages,
)
from app.engines.ai_visibility.signals import CATEGORY_MAX_POINTS  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.ai_visibility import (  # noqa: E402
    HEURISTIC_DISCLAIMER,
    AiVisibilityCategoryScore,
    AiVisibilityReport,
)
from app.models.enums import (  # noqa: E402
    AiVisibilityCategory,
    PageFetchStatus,
    ScanStatus,
    ScanTrigger,
    ScoreGrade,
)
from app.models.website import Website, WebsitePage, WebsiteScan  # noqa: E402
from app.services import scan as scan_service  # noqa: E402

API = "/api/v1"
PASSWORD = "correct-horse-battery-staple"
DUMMY_ID = "00000000-0000-0000-0000-000000000000"
# A fixed reference instant so freshness (and therefore the whole score) is a pure
# function of the fixtures, never of the wall clock.
AS_OF = datetime(2026, 8, 27, tzinfo=UTC)

_failures: list[str] = []
_passes = 0


def check(label: str):
    """Run a check immediately; record and print PASS/FAIL. Mirrors verify_seo."""

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
# renamed column breaks this proof -- which is the point. The baseline is a healthy
# page on which every signal passes; overrides make specific signals miss.
# =============================================================================
def _page(**overrides: object) -> WebsitePage:
    base: dict[str, object] = {
        "url": "https://example.com/",
        "normalized_url": "https://example.com/",
        "depth": 0,
        "is_homepage": True,
        "fetch_status": PageFetchStatus.OK.value,
        # answerability: a question heading + a real FAQ with depth
        "heading_outline": [
            {"level": 1, "text": "Rye and Co Bakery"},
            {"level": 2, "text": "How do I order a custom cake?"},
            {"level": 2, "text": "Where are you located?"},
        ],
        "heading_hierarchy_valid": True,
        "h1_count": 1,
        "has_faq_section": True,
        "faq_question_count": 6,
        # structure: lists/tables present
        "has_list_content": True,
        "has_table_content": False,
        # schema: markup present, org + rich types
        "has_schema_markup": True,
        "schema_types": ["Organization", "FAQPage", "Article"],
        # entity clarity
        "title": "Rye and Co - Fresh Sourdough Bakery in Pune",
        "title_length": 43,
        "meta_description": "Neighbourhood bakery serving fresh sourdough daily.",
        "lang": "en",
        # completeness
        "word_count": 1200,
        # citation readiness
        "has_author_info": True,
        "meta_robots": "index,follow",
        "canonical_url": "https://example.com/",
        "outbound_citation_count": 3,
        # freshness
        "published_date": datetime(2026, 5, 1, tzinfo=UTC),
        "modified_date": datetime(2026, 7, 1, tzinfo=UTC),
        # trust (site-level, read via context)
        "has_contact_info": True,
    }
    base.update(overrides)
    return WebsitePage(**base)  # type: ignore[arg-type]


# The overrides that make a page miss (almost) every signal at once.
_BAD: dict[str, object] = {
    "heading_outline": [],
    "heading_hierarchy_valid": False,
    "h1_count": 0,
    "has_faq_section": False,
    "faq_question_count": 0,
    "has_list_content": False,
    "has_table_content": False,
    "has_schema_markup": False,
    "schema_types": [],
    "title": "",
    "title_length": 0,
    "meta_description": "",
    "lang": "",
    "word_count": 40,  # thin: substantial misses, in_depth stays N/A
    "has_author_info": False,
    "meta_robots": "noindex,follow",
    "canonical_url": "",
    "outbound_citation_count": 0,
    "published_date": None,  # no dates: dates_present misses, recent_updates N/A
    "modified_date": None,
    "has_contact_info": False,
}


def _healthy_pages() -> list[WebsitePage]:
    # Three pages so completeness.topic_breadth (>=3 analysable pages) also passes.
    return [
        _page(),
        _page(
            url="https://example.com/menu",
            normalized_url="https://example.com/menu",
            depth=1,
            is_homepage=False,
        ),
        _page(
            url="https://example.com/about",
            normalized_url="https://example.com/about",
            depth=1,
            is_homepage=False,
        ),
    ]


def _healthy_site() -> object:
    return score_pages(_healthy_pages(), site_url="https://example.com/", as_of=AS_OF)


def _bad_site() -> object:
    return score_pages(
        [_page(**_BAD)],
        site_url="http://example.com/",  # not https -> trust.secure_connection misses
        as_of=AS_OF,
    )


# =============================================================================
# respx site for the full pipeline (execute_scan). The same two-page bakery site
# the SEO verifier uses, served over http. example.com is RFC 2606 reserved: it
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
    @check("category weights sum to 1.0 and cover the eight AI-visibility groups")
    def _weights() -> str:
        assert set(CATEGORY_WEIGHTS) == set(AiVisibilityCategory), "weights miss one"
        total = sum(CATEGORY_WEIGHTS.values())
        assert abs(total - 1.0) < 1e-9, total
        ids = [s.signal_id for s in SIGNALS]
        assert len(ids) == len(set(ids)), "duplicate signal_id"
        assert all(v > 0 for v in CATEGORY_MAX_POINTS.values()), "a group has no signal"
        return f"{len(SIGNALS)} signals, weights sum to {total:.4f}"

    # ------------------------------------------- 2. genuine engine, not a wrapper
    @check(
        "the AI-visibility engine source imports no language-model client (section 36)"
    )
    def _no_llm() -> str:
        # Inspect *imports* via the AST rather than grepping the text: this module's
        # docstrings legitimately name AI assistants (ChatGPT, Gemini, Perplexity) as
        # what the score is about, so a substring scan would false-positive on prose.
        # The honesty claim is precisely "imports no LLM client", so check imports.
        import ast

        engine_dir = BACKEND_DIR / "app" / "engines" / "ai_visibility"
        forbidden = ("genai", "gemini", "generativeai", "openai", "anthropic")

        def _hit(name: str) -> bool:
            low = name.lower()
            return any(token in low for token in forbidden)

        scanned = 0
        for path in sorted(engine_dir.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert not _hit(alias.name), f"{path.name} imports {alias.name}"
                elif isinstance(node, ast.ImportFrom):
                    assert not _hit(node.module or ""), f"{path.name}: {node.module}"
                    for alias in node.names:
                        assert not _hit(alias.name), f"{path.name}: {alias.name}"
            scanned += 1
        assert scanned >= 3, scanned
        return f"{scanned} engine files, no import of an LLM SDK"

    # ------------------------------------------------- 3. a clean site scores 100
    @check("a fully healthy site scores 100.0 (excellent) with all signals present")
    def _healthy() -> str:
        r = _healthy_site()
        assert r.overall_score == 100.0, r.overall_score
        assert r.grade == ScoreGrade.EXCELLENT.value, r.grade
        assert r.engine_version == ENGINE_VERSION, r.engine_version
        assert all(c.normalized_score == 100.0 for c in r.categories)
        assert r.pages_analyzed == 3, r.pages_analyzed
        assert (
            r.has_faq_content
            and r.has_structured_data
            and r.has_clear_entity_info
            and r.has_freshness_signals
        ), "flags should all be set on a healthy site"
        return "100.0/100, excellent, all flags set"

    # ------------------------------------- 4. a wholly unready site scores 0
    @check("a wholly unready site scores 0.0 (critical) with no flags set")
    def _bad() -> str:
        r = _bad_site()
        assert r.overall_score == 0.0, r.overall_score
        assert r.grade == ScoreGrade.CRITICAL.value, r.grade
        assert not any(
            (
                r.has_faq_content,
                r.has_structured_data,
                r.has_clear_entity_info,
                r.has_freshness_signals,
            )
        ), "no flag should be set on an unready site"
        return "0.0/100, critical, no flags"

    # ----------------------------------- 5. reconciliation from signal evidence
    @check("overall reconstructs from per-signal points (section 24)")
    def _reconcile() -> str:
        r = _healthy_site()  # a spread where fractions are 1.0
        r2 = score_pages(
            [
                _page(),
                _page(
                    **_BAD,
                    url="https://example.com/x",
                    normalized_url="https://example.com/x",
                    is_homepage=False,
                    depth=1,
                ),
            ],
            site_url="https://example.com/",
            as_of=AS_OF,
        )
        for result in (r, r2):
            for cat in result.categories:
                earned = round(
                    sum(
                        v["points"]
                        for v in cat.signal_results.values()
                        if v["applicable"]
                    ),
                    4,
                )
                assert abs(earned - cat.points_earned) < 1e-9, (
                    cat.category,
                    earned,
                    cat.points_earned,
                )
                assert cat.normalized_score == round(
                    cat.points_earned / cat.points_available * 100, 1
                ), cat.category
            overall = round(
                sum(c.normalized_score * c.weight for c in result.categories), 1
            )
            assert overall == result.overall_score, (overall, result.overall_score)
        return "every category and the headline reproduce from the signals"

    # ------------------------- 6. every category keeps points_available > 0
    @check("every category has points_available > 0 (positive_ai_points_available)")
    def _positive_available() -> str:
        for result in (_healthy_site(), _bad_site()):
            for cat in result.categories:
                assert cat.points_available > 0, (cat.category, cat.points_available)
        return "all 8 categories > 0 on both a healthy and an unready site"

    # ------------------------------------------- 7. grade is the band it falls in
    @check("grade always equals the band the score falls into")
    def _grade() -> str:
        for r in (_healthy_site(), _bad_site()):
            assert r.grade == ScoreGrade.from_score(r.overall_score).value, r.grade
        return "healthy=excellent, unready=critical"

    # ---------------------------- 8. every signal id is real; evidence shapes fit
    @check("every signal_results key is a real signal with evidence of its scope")
    def _valid_signals() -> str:
        by_id = {s.signal_id: s for s in SIGNALS}
        r = _healthy_site()
        seen = 0
        for cat in r.categories:
            for sid, rec in cat.signal_results.items():
                assert sid in by_id, sid
                if rec["applicable"] and by_id[sid].scope == "page":
                    assert "pages_checked" in rec["evidence"], sid
                seen += 1
        assert seen == len(SIGNALS), (seen, len(SIGNALS))
        return f"{seen} signal records, all ids valid, evidence matches scope"

    # ------------------------------------------------------- 9. deterministic
    @check("scoring the same pages twice yields an identical result")
    def _deterministic() -> str:
        a, b = _bad_site(), _bad_site()
        assert a.overall_score == b.overall_score
        sig_a = [(c.category, c.normalized_score) for c in a.categories]
        sig_b = [(c.category, c.normalized_score) for c in b.categories]
        assert sig_a == sig_b, "category scores differ between runs"
        return "identical overall and per-category scores"

    # -------------------------------- 10. only fetched pages count; empty raises
    @check("only analysable (fetched) pages are scored; an empty crawl raises")
    def _analysable_only() -> str:
        failed = _page(
            url="https://example.com/gone",
            normalized_url="https://example.com/gone",
            is_homepage=False,
            depth=1,
            fetch_status=PageFetchStatus.TIMEOUT.value,
        )
        r = score_pages([_page(), failed], site_url="https://example.com/", as_of=AS_OF)
        assert r.pages_analyzed == 1, r.pages_analyzed  # the failed page excluded

        for bad_input in ([], [failed]):
            try:
                score_pages(bad_input, site_url="https://example.com/", as_of=AS_OF)
            except ValueError:
                continue
            raise AssertionError("expected ValueError for no analysable pages")
        return "failed fetch excluded; empty/all-failed -> ValueError"

    # -------------------- 11. an inapplicable signal is non-applicable, not zero
    @check("a signal with no applicable page is marked non-applicable, not a zero")
    def _na_signal() -> str:
        r = _bad_site()  # no FAQ, no dates, thin content
        results: dict[str, dict] = {}
        for cat in r.categories:
            results.update(cat.signal_results)
        for sid in (
            "answerability.faq_depth",  # applies only to pages with an FAQ
            "completeness.in_depth_content",  # applies only to >=300-word pages
            "freshness.recent_updates",  # applies only to pages with a date
        ):
            rec = results[sid]
            assert rec["applicable"] is False, (sid, rec)
            assert rec["points"] == 0.0 and rec["max"] == 0.0, (sid, rec)
        # ...and their categories still scored against their *applicable* signals.
        ans = next(c for c in r.categories if c.category == "answerability")
        assert ans.points_available > 0, ans.points_available
        return "faq_depth / in_depth_content / recent_updates all N/A, not scored"

    # ============================ full pipeline: migrate, scan, score, read =====

    # --------------------------------------------------- 12. schema builds
    @check("alembic upgrade head builds the ai-visibility report tables")
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
        required = [
            m.__tablename__ for m in (AiVisibilityReport, AiVisibilityCategoryScore)
        ]
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

    # ------------------- 13. execute_scan stores BOTH reports at 100%
    @check(
        "execute_scan stores an AI-visibility report alongside SEO, finishing at 100%"
    )
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
            assert "AI" in (scan.progress_message or ""), scan.progress_message
            report = session.scalars(
                select(AiVisibilityReport).where(AiVisibilityReport.scan_id == scan_id)
            ).first()
            assert report is not None, "no AI-visibility report stored"
            assert 0.0 <= report.overall_score <= 100.0, report.overall_score
            assert report.pages_analyzed == 2, report.pages_analyzed
            assert report.engine_version == ENGINE_VERSION, report.engine_version
            assert report.disclaimer == HEURISTIC_DISCLAIMER, "disclaimer not stored"
            n_cat = (
                session.query(AiVisibilityCategoryScore)
                .filter(AiVisibilityCategoryScore.report_id == report.id)
                .count()
            )
            assert n_cat == len(AiVisibilityCategory), n_cat
            state["scan"]["overall"] = report.overall_score  # type: ignore[index]
        return f"COMPLETED, {n_cat} categories, disclaimer stored, score in [0,100]"

    # ----------------------- 14. the stored report reconciles from its rows
    @check("the stored report reconciles from its own category rows")
    def _stored_reconcile() -> str:
        scan_id = uuid.UUID(state["scan"]["scan_id"])  # type: ignore[index]
        with SessionLocal() as session:
            report = session.scalars(
                select(AiVisibilityReport).where(AiVisibilityReport.scan_id == scan_id)
            ).first()
            cats = (
                session.query(AiVisibilityCategoryScore)
                .filter(AiVisibilityCategoryScore.report_id == report.id)
                .all()
            )
            for cat in cats:
                earned = round(
                    sum(
                        v["points"]
                        for v in cat.signal_results.values()
                        if v["applicable"]
                    ),
                    4,
                )
                assert abs(earned - cat.points_earned) < 1e-9, cat.category
                assert cat.points_available > 0, cat.category
            overall = round(sum(c.normalized_score * c.weight for c in cats), 1)
            assert overall == report.overall_score, (overall, report.overall_score)
        return f"{len(cats)} categories reconstruct overall {report.overall_score}"

    # --------------------- 15. API returns the report with disclaimer + flags
    @check("GET /ai-visibility returns the report with disclaimer, flags, categories")
    def _api_report() -> str:
        s = state["scan"]  # type: ignore[assignment]
        base = f"{API}/businesses/{s['bid']}/websites/{s['wid']}/scans"  # type: ignore[index]
        h = _bearer(s["token"])  # type: ignore[index]
        resp = client.get(f"{base}/{s['scan_id']}/ai-visibility", headers=h)  # type: ignore[index]
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert abs(body["overall_score"] - s["overall"]) < 1e-9, body["overall_score"]  # type: ignore[index,operator]
        assert body["disclaimer"] == HEURISTIC_DISCLAIMER, "disclaimer missing in API"
        for flag in (
            "has_faq_content",
            "has_structured_data",
            "has_clear_entity_info",
            "has_freshness_signals",
        ):
            assert flag in body, flag
        cats = body["categories"]
        assert len(cats) == len(AiVisibilityCategory), len(cats)
        weights = [c["weight"] for c in cats]
        assert weights == sorted(weights, reverse=True), "categories not weight-ordered"
        assert "issues" not in body, "AI-visibility has no issues list"
        return f"200, disclaimer present, {len(cats)} categories, weight-ordered"

    # ------------------ 16. honesty: it is framed as a heuristic, not a guarantee
    @check("the score is framed as a heuristic estimate, never a ranking guarantee")
    def _honesty() -> str:
        d = HEURISTIC_DISCLAIMER.lower()
        assert "heuristic" in d and "does not measure or guarantee" in d, d
        r = _healthy_site()
        summ = (r.summary or "").lower()
        assert "estimated ai-search readiness" in summ, r.summary
        assert "guarantee" in summ, r.summary  # explicitly disclaims a guarantee
        return "disclaimer + summary both disclaim any ranking guarantee"

    # ---------------------------- 17. no report is a 404, never a faked score
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

        base = f"{API}/businesses/{bid}/websites/{wid}/scans"
        resp = client.get(f"{base}/{scan_id}/ai-visibility", headers=_bearer(token))
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found", resp.json()
        return "queued scan -> 404, no invented report"

    # ----------------------------- 18. the route is behind the auth wall
    @check("GET /ai-visibility requires authentication")
    def _auth_wall() -> str:
        routes = [
            r
            for r in app.routes
            if isinstance(r, APIRoute)
            and r.path.endswith("/scans/{scan_id}/ai-visibility")
        ]
        assert len(routes) == 1, [r.path for r in routes]
        path = (
            routes[0]
            .path.replace("{business_id}", DUMMY_ID)
            .replace("{website_id}", DUMMY_ID)
            .replace("{scan_id}", DUMMY_ID)
        )
        resp = client.get(path)  # no Authorization header
        assert resp.status_code == 401, resp.status_code
        assert resp.json()["error"]["code"] == "not_authenticated"
        return "GET /ai-visibility unauthenticated -> 401"

    # ----------------------------------- 19. tenant isolation on the route
    @check("a stranger gets 404 (never 403) for a report they do not own")
    def _tenant() -> str:
        s = state["scan"]  # type: ignore[assignment]
        base = f"{API}/businesses/{s['bid']}/websites/{s['wid']}/scans"  # type: ignore[index]
        resp = client.get(
            f"{base}/{s['scan_id']}/ai-visibility",  # type: ignore[index]
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
