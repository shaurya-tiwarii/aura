"""Executable proof for the recommendation engine (Module 8 / Stage 7).

This is an integration proof, not a collection of mocks.  It migrates a fresh
SQLite database, scores a crawler fixture through the real worker path, derives
recommendations from the stored reports, and calls the real ASGI API.  It proves
that recommendations are deterministic, trace to their source rule/signal,
respect the impact-by-effort ranking, retain a business-owned lifecycle across
scans, and cannot cross a tenant boundary.

Run from ``backend/``:

    ./.venv/Scripts/python.exe scripts/verify_recommendation.py
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile
import traceback
import uuid
from pathlib import Path

_SCRATCH_DIR = Path(tempfile.mkdtemp(prefix="aura-verify-rec-"))
_SCRATCH_DB = _SCRATCH_DIR / "recommendation.db"
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
from sqlalchemy import inspect, select  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from app.core.database import SessionLocal, engine  # noqa: E402
from app.engines.ai_visibility.signals import SIGNALS  # noqa: E402
from app.engines.recommendation import (  # noqa: E402
    AiSignalGapInput,
    SeoIssueInput,
    build_recommendations,
)
from app.engines.recommendation.catalog import (  # noqa: E402
    AI_REMEDIATION,
    EFFORT_BY_RULE,
)
from app.engines.seo.rules import RULES  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.ai_visibility import AiVisibilityReport  # noqa: E402
from app.models.enums import (  # noqa: E402
    RecommendationCategory,
    RecommendationStatus,
    ScanStatus,
    ScanTrigger,
)
from app.models.recommendation import Recommendation  # noqa: E402
from app.models.seo import SeoCategoryScore, SeoIssue, SeoReport  # noqa: E402
from app.models.website import Website, WebsiteScan  # noqa: E402
from app.services import recommendation as recommendation_service  # noqa: E402
from app.services import scan as scan_service  # noqa: E402

API = "/api/v1"
PASSWORD = "correct-horse-battery-staple"
DUMMY_ID = "00000000-0000-0000-0000-000000000000"

_failures: list[str] = []
_passes = 0


def check(label: str):
    """Record each independent proof without hiding later failures."""

    def decorator(fn):
        global _passes
        try:
            detail = fn()
            print(f"[PASS] {label}{f' -- {detail}' if detail else ''}")
            _passes += 1
        except Exception:  # noqa: BLE001 - a proof failure must not abort the suite
            _failures.append(label)
            print(f"[FAIL] {label}")
            traceback.print_exc()
        return fn

    return decorator


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _site_mock() -> respx.MockRouter:
    """A deliberately imperfect two-page site, served entirely by respx."""
    robots = "User-agent: *\nAllow: /\nSitemap: http://example.com/sitemap.xml\n"
    sitemap = (
        '<?xml version="1.0"?><urlset '
        'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>http://example.com/</loc></url>"
        "<url><loc>http://example.com/about</loc></url></urlset>"
    )
    home = """
        <html><head><title>Rye Bakery</title></head><body>
        <h1>Rye Bakery</h1><p>Fresh bread.</p><a href="/about">About</a>
        </body></html>
    """
    about = """
        <html><head><title>About Rye Bakery</title></head><body>
        <h1>About</h1><p>We bake daily in Pune.</p><a href="/">Home</a>
        </body></html>
    """
    router = respx.mock(assert_all_called=False)
    router.get("http://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text=robots)
    )
    router.get("http://example.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=sitemap)
    )
    # html= (not text=): the crawler only analyses real HTML content types,
    # and httpx's text= shortcut serves text/plain.
    router.get("http://example.com/").mock(return_value=httpx.Response(200, html=home))
    router.get("http://example.com/about").mock(
        return_value=httpx.Response(200, html=about)
    )
    return router


def _new_business(client: TestClient, token: str) -> tuple[str, str]:
    response = client.post(
        f"{API}/businesses",
        json={
            "name": f"Rye Bakery {uuid.uuid4().hex[:8]}",
            "primary_website_url": "http://example.com",
        },
        headers=_bearer(token),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["id"], body["websites"][0]["id"]


def main() -> int:  # noqa: C901 - intentional linear end-to-end proof
    print(f"(scratch database: {_SCRATCH_DB})")
    state: dict[str, object] = {}

    @check("every SEO rule and AI signal has exactly one remediation and effort")
    def _catalog_coverage() -> str:
        rule_ids = {rule.rule_id for rule in RULES}
        signal_ids = {signal.signal_id for signal in SIGNALS}
        assert rule_ids == set(EFFORT_BY_RULE), rule_ids ^ set(EFFORT_BY_RULE)
        assert signal_ids == set(AI_REMEDIATION), signal_ids ^ set(AI_REMEDIATION)
        assert (
            not rule_ids & signal_ids
        ), "an id collision would merge unrelated actions"
        return f"{len(rule_ids)} rules and {len(signal_ids)} signals"

    @check("the recommendation engine imports no language-model client (section 36)")
    def _no_llm() -> str:
        forbidden = ("genai", "gemini", "generativeai", "openai", "anthropic")
        scanned = 0
        for path in sorted(
            (BACKEND_DIR / "app" / "engines" / "recommendation").glob("*.py")
        ):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = [
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            ]
            imports.extend(
                node.module or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            )
            assert not any(
                token in name.lower() for name in imports for token in forbidden
            )
            scanned += 1
        assert scanned >= 3
        return f"{scanned} pure engine files"

    @check(
        "ranking is deterministic and classifies impact x effort into priority buckets"
    )
    def _ranking() -> str:
        seo = SeoIssueInput(
            rule_id="metadata.title_missing",
            category="metadata",
            severity="high",
            title="Title missing",
            description="No title.",
            suggested_fix="Write a title.",
            points_deducted=0.18,
            category_points_available=0.18,
            category_weight=0.18,
        )
        ai = AiSignalGapInput(
            signal_id="entity_clarity.language_declared",
            category="entity_clarity",
            points=0.0,
            max_points=0.04,
            category_points_available=0.04,
            category_weight=0.12,
        )
        first = build_recommendations([seo], [ai])
        second = build_recommendations([seo], [ai])
        assert first == second
        title = next(item for item in first if item.rule_id == seo.rule_id)
        language = next(item for item in first if item.rule_id == ai.signal_id)
        assert title.estimated_score_gain == 18.0
        assert title.priority_bucket == "quick_win", title
        assert language.estimated_score_gain == 12.0
        assert language.category == RecommendationCategory.AI_VISIBILITY.value
        return "stable order; SEO and AI gains reconstruct from category arithmetic"

    @check("alembic head creates the recommendations table with its deduplication key")
    def _migrate() -> str:
        from alembic import command
        from alembic.config import Config

        # No alembic.ini is shipped on purpose (DB URL comes from app settings,
        # never from a tracked file); run with a bare Config plus the script
        # location instead.
        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        command.upgrade(cfg, "head")
        tables = set(inspect(engine).get_table_names())
        assert "recommendations" in tables
        uniques = inspect(engine).get_unique_constraints("recommendations")
        names = {item["name"] for item in uniques}
        assert "uq_recommendations_business_rule" in names, names
        return "recommendations + uq_recommendations_business_rule"

    app = create_app()
    client = TestClient(app, base_url="http://testserver")

    @check("register an owner and an unrelated account")
    def _accounts() -> str:
        for key, email in (
            ("owner_token", "owner@rye.example"),
            ("other_token", "other@rival.example"),
        ):
            response = client.post(
                f"{API}/auth/register",
                json={"email": email, "password": PASSWORD, "full_name": key},
            )
            assert response.status_code == 201, response.text
            state[key] = response.json()["tokens"]["access_token"]
        return "owner + stranger"

    @check("the scan worker derives recommendations beside its two scored reports")
    def _pipeline() -> str:
        business_id, website_id = _new_business(client, state["owner_token"])  # type: ignore[arg-type]
        with _site_mock(), SessionLocal() as session:
            website = session.get(Website, uuid.UUID(website_id))
            assert website is not None
            scan = scan_service.create_scan(
                session, website, trigger=ScanTrigger.MANUAL
            )
            session.commit()
            session.refresh(scan)
            scan_id = scan.id
            scan_service.execute_scan(session, scan, allow_private_hosts=True)

        with SessionLocal() as session:
            recs = list(
                session.scalars(
                    select(Recommendation).where(
                        Recommendation.business_id == uuid.UUID(business_id)
                    )
                )
            )
            assert recs, "an imperfect real scan should create real actions"
            assert all(rec.origin_scan_id == scan_id for rec in recs)
            assert all(rec.status == RecommendationStatus.PENDING.value for rec in recs)
            assert all(rec.rule_id and rec.evidence is not None for rec in recs)
            stored = session.get(WebsiteScan, scan_id)
            assert stored is not None and "recommendation" in (
                stored.progress_message or ""
            )
        state.update(
            {"business_id": business_id, "website_id": website_id, "scan_id": scan_id}
        )
        return f"{len(recs)} derived actions, no hand-authored rows"

    @check("estimated gains trace exactly to the stored SEO and AI report deficits")
    def _gain_reconciliation() -> str:
        with SessionLocal() as session:
            scan_id = state["scan_id"]
            seo = session.scalars(
                select(SeoReport).where(SeoReport.scan_id == scan_id)
            ).one()
            ai = session.scalars(
                select(AiVisibilityReport).where(AiVisibilityReport.scan_id == scan_id)
            ).one()
            recs = list(
                session.scalars(
                    select(Recommendation).where(
                        Recommendation.business_id == uuid.UUID(state["business_id"])
                    )
                )
            )
            seo_gain = sum(
                rec.estimated_score_gain or 0.0
                for rec in recs
                if rec.category != RecommendationCategory.AI_VISIBILITY.value
            )
            ai_gain = sum(
                rec.estimated_score_gain or 0.0
                for rec in recs
                if rec.category == RecommendationCategory.AI_VISIBILITY.value
            )
            assert abs(seo_gain - (100.0 - seo.overall_score)) <= 0.11
            assert abs(ai_gain - (100.0 - ai.overall_score)) <= 0.11
        return (
            "per-source gains equal each source score deficit (rounding tolerance 0.11)"
        )

    @check("the database rejects duplicate business/rule pairs")
    def _dedup_constraint() -> str:
        with SessionLocal() as session:
            original = session.scalars(
                select(Recommendation).where(
                    Recommendation.business_id == uuid.UUID(state["business_id"])
                )
            ).first()
            assert original is not None
            session.add(
                Recommendation(
                    business_id=original.business_id,
                    rule_id=original.rule_id,
                    title="Duplicate",
                    description="Must be rejected.",
                    category=original.category,
                    severity="low",
                    impact="low",
                    effort="low",
                    priority_score=0.1,
                    priority_bucket="fill_in",
                    suggested_action="Do nothing.",
                    affected_page_count=0,
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
            else:
                raise AssertionError("duplicate (business_id, rule_id) was accepted")
        return "unique key enforces the reconcile invariant"

    @check("GET recommendations is ordered, explainable, and summary-backed")
    def _list_api() -> str:
        response = client.get(
            f"{API}/businesses/{state['business_id']}/recommendations",
            headers=_bearer(state["owner_token"]),  # type: ignore[arg-type]
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["summary"]["open"] == len(body["items"])
        order = [
            (item["priority_score"], item["estimated_score_gain"], item["rule_id"])
            for item in body["items"]
        ]
        assert order == sorted(order, key=lambda item: (-item[0], -item[1], item[2]))
        assert all(item["source"] in {"seo", "ai_visibility"} for item in body["items"])
        state["recommendation_id"] = body["items"][0]["id"]
        return f"{len(body['items'])} ordered items with summary"

    @check("manual regeneration is idempotent and does not duplicate action history")
    def _regenerate() -> str:
        url = f"{API}/businesses/{state['business_id']}/recommendations/regenerate"
        before = client.get(
            url.removesuffix("/regenerate"), headers=_bearer(state["owner_token"])
        ).json()  # type: ignore[arg-type]
        response = client.post(url, headers=_bearer(state["owner_token"]))  # type: ignore[arg-type]
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == len(before["items"])
        return "same latest scan leaves one row per rule"

    @check("a user status transition is persisted and filters remain tenant-scoped")
    def _status_and_filter() -> str:
        rec_id = state["recommendation_id"]
        base = f"{API}/businesses/{state['business_id']}/recommendations"
        response = client.patch(
            f"{base}/{rec_id}",
            json={"status": "in_progress"},
            headers=_bearer(state["owner_token"]),  # type: ignore[arg-type]
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == RecommendationStatus.IN_PROGRESS.value
        filtered = client.get(
            f"{base}?status=in_progress",
            headers=_bearer(state["owner_token"]),  # type: ignore[arg-type]
        )
        assert filtered.status_code == 200
        assert {item["status"] for item in filtered.json()["items"]} == {"in_progress"}
        return "PATCH + enum filter preserve the lifecycle state"

    @check("a later measured absence completes an open SEO recommendation")
    def _resolution() -> str:
        with SessionLocal() as session:
            business_id = uuid.UUID(state["business_id"])
            website = session.get(Website, uuid.UUID(state["website_id"]))
            assert website is not None
            candidate = session.scalars(
                select(Recommendation).where(
                    Recommendation.business_id == business_id,
                    Recommendation.category
                    != RecommendationCategory.AI_VISIBILITY.value,
                    Recommendation.status.in_(["pending", "in_progress"]),
                )
            ).first()
            assert candidate is not None, "fixture must yield an open SEO action"
            scan = scan_service.create_scan(session, website)
            session.flush()
            session.add(
                SeoReport(
                    scan_id=scan.id,
                    overall_score=100.0,
                    grade="excellent",
                    pages_analyzed=1,
                    issue_count=0,
                )
            )
            # SessionLocal runs with autoflush=False: the report must be flushed
            # before generate_for_scan queries for it, or its absence reads as
            # "not measured" instead of "measured perfect".
            session.flush()
            recommendation_service.generate_for_scan(session, scan, website.business)
            session.commit()
            session.refresh(candidate)
            assert candidate.status == RecommendationStatus.COMPLETED.value
            assert candidate.resolved_by_scan_id == scan.id
            state["resolved_rule_id"] = candidate.rule_id
            # The synthetic scan's work is done; close it so the next check can
            # queue a fresh scan for the same website.
            scan.status = ScanStatus.COMPLETED.value
            session.commit()
        return "only an observed, source-measured absence marks it completed"

    @check("a recurring measured finding reopens a completed recommendation")
    def _regression() -> str:
        with SessionLocal() as session:
            business_id = uuid.UUID(state["business_id"])
            website = session.get(Website, uuid.UUID(state["website_id"]))
            assert website is not None
            rec = session.scalars(
                select(Recommendation).where(
                    Recommendation.business_id == business_id,
                    Recommendation.rule_id == state["resolved_rule_id"],
                )
            ).one()
            scan = scan_service.create_scan(session, website)
            session.flush()
            report = SeoReport(
                scan_id=scan.id,
                overall_score=90.0,
                grade="good",
                pages_analyzed=1,
                issue_count=1,
            )
            report.category_scores.append(
                SeoCategoryScore(
                    category=rec.category,
                    points_earned=0.0,
                    points_available=1.0,
                    normalized_score=0.0,
                    weight=0.1,
                    issue_count=1,
                )
            )
            report.issues.append(
                SeoIssue(
                    rule_id=rec.rule_id,
                    category=rec.category,
                    severity=rec.severity,
                    title=rec.title,
                    description=rec.description,
                    suggested_fix=rec.suggested_action,
                    points_deducted=1.0,
                    affected_page_count=1,
                )
            )
            session.add(report)
            session.flush()
            recommendation_service.generate_for_scan(session, scan, website.business)
            session.commit()
            session.refresh(rec)
            assert rec.status == RecommendationStatus.PENDING.value
            assert rec.resolved_by_scan_id is None
        return "a regression reopens instead of fabricating continued resolution"

    @check(
        "recommendation routes require authentication and hide another tenant's rows"
    )
    def _tenant_boundary() -> str:
        # The collection route plus sub-routes (regenerate, summary, item) make
        # up the recommendations API; exactly one path ends at the collection.
        routes = [
            route
            for route in app.routes
            if isinstance(route, APIRoute) and "/recommendations" in route.path
        ]
        assert any(
            route.path.endswith("/recommendations") for route in routes
        ), [route.path for route in routes]
        assert len(routes) >= 2, [route.path for route in routes]
        unauthenticated = client.get(f"{API}/businesses/{DUMMY_ID}/recommendations")
        assert unauthenticated.status_code == 401
        other = client.get(
            f"{API}/businesses/{state['business_id']}/recommendations",
            headers=_bearer(state["other_token"]),  # type: ignore[arg-type]
        )
        assert other.status_code == 404, other.text
        return "401 without token; stranger gets 404, never an existence oracle"

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
