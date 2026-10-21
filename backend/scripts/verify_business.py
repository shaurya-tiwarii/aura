"""End-to-end verification of the business API stage (Modules 2 and 3).

Drives the real HTTP surface with FastAPI's ``TestClient`` against a throwaway
SQLite database built by the *actual Alembic migrations*, so this proves the
shipped schema and the shipped routes agree.

The emphasis is on the claims that would otherwise be taken on faith:

* **The tenant boundary actually holds.** A second account's business, profile,
  and websites are unreachable through every business-scoped route, and the
  stranger's rows are still intact afterwards. This is the check that matters
  most: an authorisation bug here exposes one customer's data to another.
* **404, never 403, for someone else's row.** A 403 on an existing-but-not-yours
  id is an existence oracle -- it tells an attacker which ids are real. Missing
  and forbidden must be indistinguishable, and the auth stage makes the same
  choice for sessions.
* **The SSRF guard is real.** ``127.0.0.1``, ``169.254.169.254``, ``[::1]``, and
  an IPv4-mapped IPv6 address are all refused through the HTTP layer, not just by
  a unit call to the URL parser.
* **URL normalisation is deterministic.** Two spellings of the same address
  produce one ``normalized_url``, which is what stops a business accumulating
  duplicate scan histories for one site.
* **The onboarding step cannot go backwards**, and finishing twice keeps the
  original completion timestamp.
* **A forged ``onboarding_step`` is rejected by the database.** Alembic cannot
  diff CHECK constraints, so ``alembic check`` reports no drift whether or not the
  constraint exists. The only honest proof is an INSERT that the database refuses.
* **Completeness is checkable, not asserted.** The percentage is recomputed here
  from ``missing_fields`` and must agree with what the API returned (spec s24).
* **Every business route requires authentication.** Enumerated from the app's own
  route table rather than a hand-written list, so a route added later without auth
  fails this check instead of slipping through.

Usage
-----
    python scripts/verify_business.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Must happen before any app import: settings is a cached singleton and the
# database engine is built from it at module import time. Pointing at a scratch
# file keeps the developer's aura.db untouched.
_SCRATCH_DIR = Path(tempfile.mkdtemp(prefix="aura-verify-business-"))
_SCRATCH_DB = _SCRATCH_DIR / "business.db"
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{_SCRATCH_DB.as_posix()}"
os.environ["AUTH_PROVIDER"] = "local"
os.environ["APP_ENV"] = "development"

_failures: list[str] = []


def check(label: str):
    """Run a check immediately; record pass/fail without aborting the run."""

    def decorator(fn):
        try:
            detail = fn()
        except Exception:  # noqa: BLE001 - this IS the reporting boundary
            # Blind on purpose: one failing check must not hide the others.
            _failures.append(label)
            print(f"  [FAIL] {label}")
            traceback.print_exc()
        else:
            suffix = f" -- {detail}" if detail else ""
            print(f"  [PASS] {label}{suffix}")
        return fn

    return decorator


PASSWORD = "correct-horse-battery-staple"

# Two reserved-domain rules pull in opposite directions here, and getting them
# backwards makes the fixtures fail against correct application code:
#
# * **Emails must use ``.example``.** email-validator rejects the special-use TLDs
#   (arpa, invalid, local, localhost, onion, test) outright, so ``@x.test`` 422s at
#   registration. See the note in verify_auth.py.
# * **Website URLs must NOT use ``.example``.** The SSRF guard blocks that whole
#   TLD -- along with .test, .invalid, .local, .internal, .corp and friends --
#   because a crawl target there can only resolve to something on the local
#   network. A ``.example`` URL is refused with ``blocked_url`` before it is stored.
#
# So the URL fixtures use the RFC 2606 *second-level* reservations instead:
# example.com / example.net are permanently unregistrable, parse to a real public
# suffix, and pass the guard. Nothing in this stage fetches them.
OWNER_EMAIL = "owner@bakery.example"
STRANGER_EMAIL = "stranger@rival.example"

PRIMARY_SITE = "Example.com/?utm_source=flyer"  # -> https://example.com/
PRIMARY_SITE_ALT = "https://EXAMPLE.com"  # same site, different spelling
PRIMARY_HOST = "https://example.com/"
PRIMARY_DOMAIN = "example.com"
SECOND_SITE = "https://shop.example.net"
# What the normaliser stores: the root path is made explicit.
SECOND_SITE_STORED = "https://shop.example.net/"
SECOND_DOMAIN = "example.net"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def main() -> int:  # noqa: C901 - a linear script of checks reads better flat
    print(f"(scratch database: {_SCRATCH_DB})")

    # ---------------------------------------------------------------- schema
    print("\n== 1. schema from real migrations ==")

    @check("alembic upgrade head builds the business schema")
    def _migrate():
        from alembic import command
        from alembic.config import Config

        # No alembic.ini is shipped on purpose (DB URL comes from app settings,
        # never from a tracked file); run with a bare Config plus the script
        # location instead.
        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        command.upgrade(cfg, "head")

        from sqlalchemy import inspect

        from app.core.database import engine

        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        for required in ("businesses", "business_profiles", "websites"):
            assert required in tables, f"{required} missing after upgrade"
        return f"{len(tables)} tables"

    @check("head revision is the competitor-removal migration")
    def _head():
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        # No alembic.ini is shipped on purpose (DB URL comes from app settings,
        # never from a tracked file); run with a bare Config plus the script
        # location instead.
        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        heads = ScriptDirectory.from_config(cfg).get_heads()
        assert len(heads) == 1, f"expected a single head, found {heads}"
        assert heads[0] == "397896607f5c", f"unexpected head {heads[0]}"
        return heads[0]

    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    client = TestClient(app, base_url="http://testserver")
    api = "/api/v1"

    # ------------------------------------------------------------- accounts
    print("\n== 2. two independent accounts ==")

    tokens: dict[str, str] = {}

    @check("both accounts register and receive tokens")
    def _register():
        for label, email in (("owner", OWNER_EMAIL), ("stranger", STRANGER_EMAIL)):
            r = client.post(
                f"{api}/auth/register",
                json={"email": email, "password": PASSWORD, "full_name": label},
            )
            assert r.status_code == 201, r.text
            tokens[label] = r.json()["tokens"]["access_token"]
        return "owner + stranger"

    owner = _bearer(tokens["owner"])
    stranger = _bearer(tokens["stranger"])

    # -------------------------------------------------------------- CRUD
    print("\n== 3. business CRUD ==")

    ids: dict[str, str] = {}

    @check("create returns 201 with the first business defaulted")
    def _create():
        r = client.post(
            f"{api}/businesses",
            json={
                "name": "Rye & Co Bakery",
                "industry": "Food & Beverage",
                "location": "Pune, India",
                "primary_website_url": PRIMARY_SITE,
            },
            headers=owner,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        ids["bakery"] = body["id"]
        assert body["is_default"] is True, "the first business must be the default"
        assert body["is_onboarded"] is False, "a new business is not onboarded"
        assert body["onboarding_step"] == "website", body["onboarding_step"]
        assert body["profile"] is None, "no profile should be invented on create"
        assert len(body["websites"]) == 1, body["websites"]
        return f"step={body['onboarding_step']} default={body['is_default']}"

    @check("the supplied URL is normalised before storage")
    def _normalised():
        site = client.get(f"{api}/businesses/{ids['bakery']}", headers=owner).json()[
            "websites"
        ][0]
        assert site["url"] == PRIMARY_HOST, site["url"]
        assert site["normalized_url"] == PRIMARY_HOST, site
        assert site["registrable_domain"] == PRIMARY_DOMAIN, site
        assert site["is_primary"] is True, site
        # Crawl observations must be null until a scan runs, never False: the UI
        # would otherwise render a "no sitemap" warning for an unscanned site.
        assert site["robots_txt_found"] is None, site
        assert site["sitemap_found"] is None, site
        assert site["last_scan_at"] is None, site
        return f"{site['url']} (tracking param stripped, scheme added)"

    @check("a second business is not promoted to default")
    def _second():
        r = client.post(
            f"{api}/businesses", json={"name": "Rye Wholesale"}, headers=owner
        )
        assert r.status_code == 201, r.text
        ids["wholesale"] = r.json()["id"]
        assert r.json()["is_default"] is False, "only the first should auto-default"
        assert r.json()["onboarding_step"] == "business_basics", r.json()
        return "is_default=False"

    @check("duplicate name for the same owner is a clean 409, not a 500")
    def _duplicate():
        r = client.post(
            f"{api}/businesses",
            json={"name": "rye & co bakery"},  # differs only in case
            headers=owner,
        )
        assert r.status_code == 409, f"{r.status_code}: {r.text}"
        assert r.json()["error"]["code"] == "business_name_taken", r.json()
        return "case-insensitive match rejected"

    @check("the same name IS allowed for a different owner")
    def _same_name_other_owner():
        r = client.post(
            f"{api}/businesses", json={"name": "Rye & Co Bakery"}, headers=stranger
        )
        assert r.status_code == 201, r.text
        ids["stranger_business"] = r.json()["id"]
        return "uniqueness is per-owner, not global"

    @check("promoting a business demotes the previous default")
    def _promote():
        r = client.post(f"{api}/businesses/{ids['wholesale']}/default", headers=owner)
        assert r.status_code == 200, r.text
        assert r.json()["is_default"] is True, r.json()
        listing = client.get(f"{api}/businesses", headers=owner).json()
        defaults = [b["name"] for b in listing if b["is_default"]]
        assert defaults == ["Rye Wholesale"], defaults
        # Default sorts first, so the dashboard's landing business is predictable.
        assert listing[0]["is_default"] is True, listing
        return "exactly one default remains"

    @check("PATCH updates only the fields sent")
    def _patch():
        r = client.patch(
            f"{api}/businesses/{ids['bakery']}",
            json={"industry": "Artisan Bakery"},
            headers=owner,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["industry"] == "Artisan Bakery", body
        assert body["location"] == "Pune, India", "an unsent field was modified"
        assert body["name"] == "Rye & Co Bakery", body
        return "industry changed, location untouched"

    @check("PATCH with an explicit null clears the field")
    def _patch_null():
        r = client.patch(
            f"{api}/businesses/{ids['bakery']}", json={"location": None}, headers=owner
        )
        assert r.status_code == 200, r.text
        assert r.json()["location"] is None, r.json()
        assert r.json()["industry"] == "Artisan Bakery", "clearing leaked to a sibling"
        return "null clears, absent does not"

    @check("PATCH with an empty body is refused")
    def _patch_empty():
        r = client.patch(f"{api}/businesses/{ids['bakery']}", json={}, headers=owner)
        assert r.status_code == 422, f"{r.status_code}: {r.text}"
        return "422 rather than a silent no-op save"

    @check("an unknown field is rejected rather than ignored")
    def _patch_extra():
        r = client.patch(
            f"{api}/businesses/{ids['bakery']}",
            json={"owner_id": str(uuid.uuid4())},
            headers=owner,
        )
        assert r.status_code == 422, f"{r.status_code}: {r.text}"
        return "extra='forbid' blocks owner_id injection"

    # ------------------------------------------------------- tenant boundary
    print("\n== 4. tenant isolation (the check that matters most) ==")

    @check("every business-scoped route 404s on another owner's id")
    def _isolation():
        target = ids["stranger_business"]
        attempts = [
            ("GET", f"{api}/businesses/{target}", None),
            ("PATCH", f"{api}/businesses/{target}", {"name": "Hijacked"}),
            ("DELETE", f"{api}/businesses/{target}", None),
            ("POST", f"{api}/businesses/{target}/default", None),
            ("PATCH", f"{api}/businesses/{target}/onboarding", {"completed": True}),
            ("GET", f"{api}/businesses/{target}/profile", None),
            ("PATCH", f"{api}/businesses/{target}/profile", {"brand_voice": "x"}),
            ("GET", f"{api}/businesses/{target}/websites", None),
            ("POST", f"{api}/businesses/{target}/websites", {"url": "evil.example"}),
        ]
        for method, url, payload in attempts:
            r = client.request(method, url, json=payload, headers=owner)
            assert r.status_code == 404, f"{method} {url} -> {r.status_code} {r.text}"
            # 403 would confirm the id exists. Missing and forbidden must look
            # identical from outside.
            assert r.json()["error"]["code"] == "not_found", r.json()
        return f"{len(attempts)} routes, all 404 (never 403)"

    @check("a nonexistent id is indistinguishable from someone else's")
    def _oracle():
        ghost = client.get(f"{api}/businesses/{uuid.uuid4()}", headers=owner)
        real = client.get(f"{api}/businesses/{ids['stranger_business']}", headers=owner)
        assert ghost.status_code == real.status_code == 404
        # request_id is a per-request correlation id and is expected to differ; it
        # is the *error* object that must not distinguish the two cases.
        assert (
            ghost.json()["error"] == real.json()["error"]
        ), "the error envelopes differ, leaking existence"
        return "identical error objects"

    @check("the stranger's data is untouched by those attempts")
    def _intact():
        r = client.get(f"{api}/businesses/{ids['stranger_business']}", headers=stranger)
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "Rye & Co Bakery", r.json()
        listing = client.get(f"{api}/businesses", headers=stranger).json()
        assert len(listing) == 1, listing
        return "still present, unmodified"

    @check("the owner's list never contains another account's business")
    def _list_scoped():
        for label, headers in (("owner", owner), ("stranger", stranger)):
            listing = client.get(f"{api}/businesses", headers=headers).json()
            names = {b["id"] for b in listing}
            foreign = (
                {ids["stranger_business"]}
                if label == "owner"
                else {ids["bakery"], ids["wholesale"]}
            )
            assert not (
                names & foreign
            ), f"{label} sees foreign rows: {names & foreign}"
        return "owner sees 2, stranger sees 1"

    @check("a website id from another business 404s")
    def _cross_business_website():
        site = client.get(f"{api}/businesses/{ids['bakery']}/websites", headers=owner)
        website_id = site.json()[0]["id"]
        # Correct owner, correct auth -- but the website belongs to a different
        # business of theirs. The business_id predicate must still reject it.
        r = client.get(
            f"{api}/businesses/{ids['wholesale']}/websites/{website_id}", headers=owner
        )
        assert r.status_code == 404, f"{r.status_code}: {r.text}"
        return "the business_id predicate is enforced, not just owner_id"

    # ----------------------------------------------------------- URL safety
    print("\n== 5. URL normalisation and the SSRF guard ==")

    @check("private, loopback, and metadata addresses are refused over HTTP")
    def _ssrf_http():
        blocked = [
            "http://127.0.0.1:8000",
            "http://localhost:3000",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
            "http://10.0.0.5/",
            "http://192.168.1.1/",
            "http://172.16.0.1/",
            "http://[::ffff:127.0.0.1]/",
            "http://metadata.google.internal/",
            "http://0.0.0.0/",
            "javascript:alert(1)",
            "file:///etc/passwd",
            "http://user:pass@example.com/",
            "http://example.com:22/",
        ]
        for raw in blocked:
            r = client.post(
                f"{api}/businesses/{ids['bakery']}/websites",
                json={"url": raw},
                headers=owner,
            )
            assert r.status_code == 400, f"{raw} -> {r.status_code} {r.text}"
            code = r.json()["error"]["code"]
            assert code in {
                "blocked_url",
                "invalid_url",
                "private_address",
                "blocked_port",
                "unsupported_scheme",
                "url_contains_credentials",
                "unrecognised_domain",
            }, f"{raw} -> unexpected code {code}"
        return f"{len(blocked)} hostile URLs rejected, none stored"

    @check("no website row was created by any blocked attempt")
    def _no_partial_write():
        sites = client.get(
            f"{api}/businesses/{ids['bakery']}/websites", headers=owner
        ).json()
        assert len(sites) == 1, f"expected only the original site, got {len(sites)}"
        assert sites[0]["registrable_domain"] == PRIMARY_DOMAIN, sites
        return "1 website, the legitimate one"

    @check("two spellings of one address collapse to a single normalized_url")
    def _dedupe():
        from app.utils.urls import normalize_url

        a = normalize_url("HTTPS://Example.COM:443/path/?b=2&a=1&utm_source=x")
        b = normalize_url("example.com/path?a=1&b=2")
        assert a.normalized == b.normalized, f"{a.normalized} != {b.normalized}"
        # The fetchable form keeps the user's query order; only the dedup key sorts.
        assert a.url != a.normalized or "?" not in a.url, (a.url, a.normalized)
        return a.normalized

    @check("registering the same site twice is a 409, not a duplicate row")
    def _dupe_website():
        r = client.post(
            f"{api}/businesses/{ids['bakery']}/websites",
            json={"url": PRIMARY_SITE_ALT},
            headers=owner,
        )
        assert r.status_code == 409, f"{r.status_code}: {r.text}"
        assert r.json()["error"]["code"] == "website_already_registered", r.json()
        return "dedup key matched a different spelling"

    @check("a public IP literal and an IDN are both accepted")
    def _accepts_valid():
        from app.utils.urls import normalize_url

        ip = normalize_url("http://93.184.216.34/")
        assert ip.is_ip_literal is True and ip.host == "93.184.216.34", ip
        idn = normalize_url("https://пример.com/")
        assert idn.host == "xn--e1afmkfd.com", idn.host
        return f"{ip.host} and {idn.host} (punycode)"

    # -------------------------------------------------------------- profile
    print("\n== 6. profile upsert and completeness ==")

    @check("GET profile 404s before one exists, rather than inventing an empty one")
    def _no_profile():
        r = client.get(f"{api}/businesses/{ids['wholesale']}/profile", headers=owner)
        assert r.status_code == 404, f"{r.status_code}: {r.text}"
        return "'no profile' and 'empty profile' stay distinct states"

    @check("PATCH creates the profile, tidying and de-duplicating lists")
    def _create_profile():
        r = client.patch(
            f"{api}/businesses/{ids['bakery']}/profile",
            json={
                "services": ["  Sourdough  ", "sourdough", "Celebration Cakes", ""],
                "brand_voice": "Warm   and    neighbourly",
                "contact_email": "hello@bakery.example",
                "social_links": {"Instagram": "https://instagram.com/ryeco"},
            },
            headers=owner,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["services"] == ["Sourdough", "Celebration Cakes"], body["services"]
        assert body["brand_voice"] == "Warm and neighbourly", body["brand_voice"]
        assert body["social_links"] == {
            "instagram": "https://instagram.com/ryeco"
        }, body["social_links"]
        return f"case-insensitive dedupe, whitespace collapsed, {len(body['services'])} services"

    @check("completeness_percent is reproducible from missing_fields")
    def _completeness():
        from app.services.business import PROFILE_COMPLETENESS_FIELDS

        body = client.get(
            f"{api}/businesses/{ids['bakery']}/profile", headers=owner
        ).json()
        total = len(PROFILE_COMPLETENESS_FIELDS)
        filled = total - len(body["missing_fields"])
        expected = round(filled * 100 / total)
        assert (
            body["completeness_percent"] == expected
        ), f"reported {body['completeness_percent']}%, recomputes to {expected}%"
        # Spec s24: the number must be explainable, so the empty fields are named.
        for field in body["missing_fields"]:
            assert field in PROFILE_COMPLETENESS_FIELDS, field
            assert not body[field], f"{field} listed as missing but has a value"
        return f"{body['completeness_percent']}% = {filled}/{total}, all names valid"

    @check("a second PATCH merges instead of replacing")
    def _merge_profile():
        r = client.patch(
            f"{api}/businesses/{ids['bakery']}/profile",
            json={"keywords": ["artisan bread", "pune bakery"]},
            headers=owner,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["keywords"] == ["artisan bread", "pune bakery"], body["keywords"]
        assert body["services"] == [
            "Sourdough",
            "Celebration Cakes",
        ], "an earlier onboarding step was erased by a later one"
        return "services survived a keywords-only update"

    @check("clearing a list writes an empty list, not null")
    def _clear_list():
        r = client.patch(
            f"{api}/businesses/{ids['bakery']}/profile",
            json={"keywords": None},
            headers=owner,
        )
        assert r.status_code == 200, r.text
        assert r.json()["keywords"] == [], r.json()["keywords"]
        return "one spelling of 'nothing here', so consumers need no null check"

    @check("a javascript: social link is rejected (stored XSS)")
    def _xss_social():
        r = client.patch(
            f"{api}/businesses/{ids['bakery']}/profile",
            json={"social_links": {"x": "javascript:alert(document.cookie)"}},
            headers=owner,
        )
        assert r.status_code == 422, f"{r.status_code}: {r.text}"
        return "422 -- these render as href attributes"

    @check("an implausible founding year is refused")
    def _founded_year():
        r = client.patch(
            f"{api}/businesses/{ids['bakery']}/profile",
            json={"founded_year": 3999},
            headers=owner,
        )
        assert r.status_code == 422, f"{r.status_code}: {r.text}"
        return "future year rejected against a real clock"

    # ------------------------------------------------------------ onboarding
    print("\n== 7. onboarding progress ==")

    @check("the step moves forward but never backward")
    def _monotonic():
        b = ids["bakery"]
        r = client.patch(
            f"{api}/businesses/{b}/onboarding", json={"step": "profile"}, headers=owner
        )
        assert r.status_code == 200 and r.json()["onboarding_step"] == "profile", r.text
        r = client.patch(
            f"{api}/businesses/{b}/onboarding",
            json={"step": "business_basics"},
            headers=owner,
        )
        assert r.status_code == 200, r.text
        assert (
            r.json()["onboarding_step"] == "profile"
        ), f"step went backwards to {r.json()['onboarding_step']}"
        return "profile held against a business_basics rewind"

    @check("completing is idempotent and keeps the original timestamp")
    def _complete():
        b = ids["bakery"]
        first = client.patch(
            f"{api}/businesses/{b}/onboarding", json={"completed": True}, headers=owner
        ).json()
        assert first["onboarding_step"] == "complete", first
        assert first["onboarding_completed_at"] is not None, first
        assert first["is_onboarded"] is True, first
        second = client.patch(
            f"{api}/businesses/{b}/onboarding", json={"completed": True}, headers=owner
        ).json()
        assert (
            second["onboarding_completed_at"] == first["onboarding_completed_at"]
        ), "re-finishing overwrote the completion time"
        return "timestamp stable across repeat calls"

    @check("an invalid step value is rejected by the schema")
    def _bad_step():
        r = client.patch(
            f"{api}/businesses/{ids['bakery']}/onboarding",
            json={"step": "totally_made_up"},
            headers=owner,
        )
        assert r.status_code == 422, f"{r.status_code}: {r.text}"
        return "422 at the edge, before the database sees it"

    @check("a forged onboarding_step is rejected by the DATABASE")
    def _check_constraint():
        # Alembic does not diff CHECK constraints, so `alembic check` reports no
        # drift whether or not this constraint exists. A rejected INSERT is the
        # only honest proof that it is really there.
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        from app.core.database import engine

        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT id FROM businesses LIMIT 1")
            ).fetchone()
            assert row is not None, "no business row to copy"
            try:
                connection.execute(
                    text(
                        "UPDATE businesses SET onboarding_step = 'not_a_real_step' "
                        "WHERE id = :id"
                    ),
                    {"id": row[0]},
                )
                connection.commit()
            except IntegrityError:
                return "ck_businesses_valid_onboarding_step rejected the write"
            raise AssertionError("the database accepted an invalid onboarding_step")

    # -------------------------------------------------------------- websites
    print("\n== 8. website management ==")

    @check("a second website is added without stealing primary")
    def _second_website():
        r = client.post(
            f"{api}/businesses/{ids['bakery']}/websites",
            json={"url": SECOND_SITE},
            headers=owner,
        )
        assert r.status_code == 201, r.text
        ids["shop"] = r.json()["id"]
        assert r.json()["is_primary"] is False, "a later website must not auto-promote"
        return "is_primary=False"

    @check("promoting a website demotes the previous primary")
    def _promote_website():
        r = client.patch(
            f"{api}/businesses/{ids['bakery']}/websites/{ids['shop']}",
            json={"is_primary": True},
            headers=owner,
        )
        assert r.status_code == 200 and r.json()["is_primary"] is True, r.text
        sites = client.get(
            f"{api}/businesses/{ids['bakery']}/websites", headers=owner
        ).json()
        primaries = [s["url"] for s in sites if s["is_primary"]]
        assert primaries == [SECOND_SITE_STORED], primaries
        business = client.get(f"{api}/businesses/{ids['bakery']}", headers=owner).json()
        assert (
            business["primary_website_url"] == primaries[0]
        ), "the business's displayed URL disagrees with its primary website"
        return "exactly one primary, mirrored onto the business"

    @check("deleting the primary promotes a replacement")
    def _delete_primary():
        r = client.delete(
            f"{api}/businesses/{ids['bakery']}/websites/{ids['shop']}", headers=owner
        )
        assert r.status_code == 204, r.text
        sites = client.get(
            f"{api}/businesses/{ids['bakery']}/websites", headers=owner
        ).json()
        assert len(sites) == 1 and sites[0]["is_primary"] is True, sites
        business = client.get(f"{api}/businesses/{ids['bakery']}", headers=owner).json()
        assert business["primary_website_url"] == sites[0]["url"], business
        return "never left with websites but no primary"

    @check("demoting the only website is refused with a reason")
    def _demote_only():
        sites = client.get(
            f"{api}/businesses/{ids['bakery']}/websites", headers=owner
        ).json()
        r = client.patch(
            f"{api}/businesses/{ids['bakery']}/websites/{sites[0]['id']}",
            json={"is_primary": False},
            headers=owner,
        )
        assert r.status_code == 422, f"{r.status_code}: {r.text}"
        assert r.json()["error"]["code"] == "primary_website_required", r.json()
        assert r.json()["error"].get("hint"), "a refusal should say what to do instead"
        return "422 with a hint, not a silent no-op"

    @check("the website URL is immutable")
    def _immutable_url():
        sites = client.get(
            f"{api}/businesses/{ids['bakery']}/websites", headers=owner
        ).json()
        r = client.patch(
            f"{api}/businesses/{ids['bakery']}/websites/{sites[0]['id']}",
            json={"url": "https://somewhere-else.example.org"},
            headers=owner,
        )
        assert r.status_code == 422, f"{r.status_code}: {r.text}"
        return "editing it would re-attribute scan history to another site"

    # -------------------------------------------------------------- cascade
    print("\n== 9. delete cascade and default repair ==")

    @check("deleting a business removes its profile and websites")
    def _cascade():
        from sqlalchemy import text

        from app.core.database import engine

        target = ids["bakery"]
        r = client.delete(f"{api}/businesses/{target}", headers=owner)
        assert r.status_code == 204, r.text
        with engine.connect() as connection:
            for table in ("business_profiles", "websites"):
                remaining = connection.execute(
                    text(f"SELECT COUNT(*) FROM {table} WHERE business_id = :id"),  # noqa: S608
                    {"id": target},
                ).scalar()
                assert remaining == 0, f"{remaining} orphaned rows in {table}"
        assert (
            client.get(f"{api}/businesses/{target}", headers=owner).status_code == 404
        )
        return "no orphans in business_profiles or websites"

    @check("deleting the default promotes another business")
    def _default_repair():
        # 'wholesale' is currently the default; delete it and check the remaining
        # business is promoted, so the dashboard always has something to open.
        r = client.post(f"{api}/businesses", json={"name": "Rye Cafe"}, headers=owner)
        assert r.status_code == 201, r.text
        cafe = r.json()["id"]
        client.post(f"{api}/businesses/{ids['wholesale']}/default", headers=owner)
        assert (
            client.delete(
                f"{api}/businesses/{ids['wholesale']}", headers=owner
            ).status_code
            == 204
        )
        listing = client.get(f"{api}/businesses", headers=owner).json()
        assert len(listing) == 1 and listing[0]["id"] == cafe, listing
        assert (
            listing[0]["is_default"] is True
        ), "the account has a business but no default"
        return "the survivor became the default"

    # ------------------------------------------------------------- auth wall
    print("\n== 10. every business route requires authentication ==")

    @check("no business route is reachable without a token")
    def _auth_required():
        # Enumerated from the app's own route table, so a route added later
        # without the dependency fails here instead of shipping open. The nested
        # /scans sub-tree is owned by verify_crawler.py's own auth-wall check
        # (it needs {scan_id}/{page_id} substitution this loop does not do), so
        # it is excluded here rather than counted twice.
        routes = [
            route
            for route in app.routes
            if getattr(route, "path", "").startswith(f"{api}/businesses")
            and "/scans" not in getattr(route, "path", "")
        ]
        tested = 0
        for route in routes:
            path = route.path.replace("{business_id}", str(uuid.uuid4())).replace(
                "{website_id}", str(uuid.uuid4())
            )
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                r = client.request(method, path, json={})
                assert (
                    r.status_code == 401
                ), f"{method} {route.path} -> {r.status_code} without a token"
                assert r.json()["error"]["code"] == "not_authenticated", r.json()
                tested += 1
        return f"{tested} method/path pairs, all 401"

    @check("a revoked token cannot reach business data")
    def _revoked():
        r = client.post(
            f"{api}/auth/login", json={"email": OWNER_EMAIL, "password": PASSWORD}
        )
        assert r.status_code == 200, r.text
        fresh = _bearer(r.json()["tokens"]["access_token"])
        assert client.get(f"{api}/businesses", headers=fresh).status_code == 200
        assert client.post(f"{api}/auth/logout", headers=fresh).status_code in (
            200,
            204,
        )
        after = client.get(f"{api}/businesses", headers=fresh)
        assert after.status_code == 401, f"{after.status_code}: {after.text}"
        return "logout invalidates on the very next business request"

    print()
    if _failures:
        print(f"RESULT: {len(_failures)} CHECK(S) FAILED")
        for label in _failures:
            print(f"  - {label}")
        return 1
    print("RESULT: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
