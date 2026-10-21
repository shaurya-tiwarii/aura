"""Deep self-check of the AURA foundation.

Run this after a fresh clone / dependency install, and after any change to the
data model. ``create_all()`` on its own is a weak test: SQLAlchemy resolves
relationship strings and builds join conditions lazily, so a typo in a
cross-file ``relationship("Foo", back_populates="bar")`` only surfaces the first
time a mapper is actually used. This script forces all of it eagerly.

What it proves
--------------
1. Every ``app.core`` and ``app.models`` module imports.
2. ``configure_mappers()`` succeeds -> every relationship target and join
   condition resolves.
3. Every ``ForeignKey`` points at a column that exists.
4. DDL emits on SQLite *and* compiles for PostgreSQL.
5. The honesty CHECK constraints from the spec are present in the metadata.
6. A real ORM insert + relationship traversal round-trips.
7. Password hashing, secret-redacting logging, and the capability report work.

Usage
-----
    python scripts/verify_foundation.py            # uses the configured DB
    python scripts/verify_foundation.py --scratch  # uses a throwaway SQLite file
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import traceback
import uuid
from pathlib import Path

# Allow running as `python scripts/verify_foundation.py` from backend/.
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


_failures: list[str] = []


def check(label: str):
    """Decorator that runs a check immediately and records pass/fail."""

    def decorator(fn):
        try:
            detail = fn()
        except Exception:  # noqa: BLE001 - this IS the reporting boundary
            # Blind on purpose: one failing check must not abort the other 15.
            _failures.append(label)
            print(f"  [FAIL] {label}")
            traceback.print_exc()
        else:
            suffix = f" -- {detail}" if detail else ""
            print(f"  [PASS] {label}{suffix}")
        return fn

    return decorator


def main() -> int:  # noqa: C901 - a linear script of checks reads better flat
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scratch",
        action="store_true",
        help="run DDL against a throwaway SQLite file instead of the configured DB",
    )
    args = parser.parse_args()

    if args.scratch:
        scratch = Path(tempfile.mkdtemp(prefix="aura-verify-")) / "verify.db"
        os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{scratch.as_posix()}"
        print(f"(scratch database: {scratch})")

    print("\n== 1. module imports ==")

    @check("import app.core.{config,logging,errors,database,middleware,security}")
    def _core_imports():
        import app.core.config
        import app.core.database
        import app.core.errors
        import app.core.logging
        import app.core.middleware
        import app.core.security  # noqa: F401

    @check("import app.models (every model module)")
    def _model_imports():
        # only runs once this check has passed. Ruff sees a redefinition; removing
        # either one would delete the thing being verified.
        import app.models  # noqa: F401, F811

    if _failures:
        print("\nRESULT: imports failed -- cannot continue.")
        return 1

    import app.models  # noqa: F401
    from app.models.base import Base

    print("\n== 2. mapper configuration ==")

    @check("configure_mappers() resolves every relationship string")
    def _configure():
        from sqlalchemy.orm import configure_mappers

        configure_mappers()

    @check("relationship targets + join conditions compile")
    def _relationships():
        count = 0
        for mapper in Base.registry.mappers:
            for rel in mapper.relationships:
                _ = rel.mapper.class_  # forces target class resolution
                _ = rel.primaryjoin  # forces join-condition compilation
                if rel.secondaryjoin is not None:
                    _ = rel.secondaryjoin
                count += 1
        return f"{count} relationships"

    @check("every ForeignKey target column exists")
    def _foreign_keys():
        unresolved = []
        total = 0
        for table in Base.metadata.tables.values():
            for fk in table.foreign_keys:
                total += 1
                try:
                    # Not a useless expression: resolving `.column` is what
                    # forces SQLAlchemy to look the FK target up, which is the
                    # whole check. Assigning it would imply the value matters.
                    fk.column  # noqa: B018
                except Exception as exc:  # noqa: BLE001
                    unresolved.append(f"{table.name}.{fk.parent.name} -> {exc}")
        if unresolved:
            raise AssertionError("unresolved FKs: " + "; ".join(unresolved))
        return f"{total} foreign keys"

    @check("every table has a primary key")
    def _primary_keys():
        missing = [t.name for t in Base.metadata.tables.values() if not t.primary_key]
        if missing:
            raise AssertionError(f"tables without a primary key: {missing}")
        return f"{len(Base.metadata.tables)} tables"

    print("\n== 3. schema honesty constraints ==")

    @check("spec CHECK constraints present (simulated-as-real is unforgeable)")
    def _honesty_constraints():
        from sqlalchemy import CheckConstraint

        # Base applies a naming convention, so the emitted name is
        # ``ck_<table>_<declared name>``. Match on the declared suffix.
        named = {
            c.name
            for table in Base.metadata.tables.values()
            for c in table.constraints
            if isinstance(c, CheckConstraint) and c.name
        }
        required = {
            "live_connection_requires_token": "platform_connections",
            "published_live_post_requires_remote_id": "posts",
        }
        missing = [
            declared
            for declared, table in required.items()
            if f"ck_{table}_{declared}" not in named and declared not in named
        ]
        if missing:
            raise AssertionError(f"missing: {missing} (present: {sorted(named)})")
        return f"{len(named)} CHECK constraints; both honesty constraints present"

    print("\n== 4. DDL ==")

    @check("create_all() against the configured database")
    def _create_all():
        from sqlalchemy import inspect

        from app.core.database import engine

        Base.metadata.create_all(engine)
        actual = set(inspect(engine).get_table_names())
        missing = set(Base.metadata.tables) - actual
        if missing:
            raise AssertionError(f"tables not created: {sorted(missing)}")
        return f"{len(Base.metadata.tables)} tables created"

    @check("DDL compiles for postgresql+psycopg (no server required)")
    def _pg_ddl():
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateIndex, CreateTable

        dialect = postgresql.dialect(paramstyle="pyformat")
        for table in Base.metadata.sorted_tables:
            str(CreateTable(table).compile(dialect=dialect))
            for index in table.indexes:
                str(CreateIndex(index).compile(dialect=dialect))
        return f"{len(Base.metadata.sorted_tables)} tables"

    print("\n== 5. ORM round-trip ==")

    @check("insert user -> business -> profile -> website -> scan -> page")
    def _round_trip():
        from app.core.database import session_scope
        from app.core.security import hash_password
        from app.models.business import Business, BusinessProfile
        from app.models.user import User
        from app.models.website import Website, WebsitePage, WebsiteScan

        suffix = uuid.uuid4().hex[:8]
        with session_scope() as session:
            user = User(
                email=f"verify-{suffix}@example.test",
                full_name="Foundation Verifier",
                password_hash=hash_password(f"verify-{suffix}"),
            )
            session.add(user)
            session.flush()

            business = Business(owner_id=user.id, name=f"Verify Co {suffix}")
            session.add(business)
            session.flush()

            session.add(
                BusinessProfile(
                    business_id=business.id,
                    services=["SEO audits"],
                    keywords=["local seo"],
                )
            )
            website = Website(
                business_id=business.id,
                url="https://example.test/",
                normalized_url="https://example.test",
                registrable_domain="example.test",
            )
            session.add(website)
            session.flush()

            scan = WebsiteScan(website_id=website.id, sequence=1)
            session.add(scan)
            session.flush()

            session.add(
                WebsitePage(
                    scan_id=scan.id,
                    url="https://example.test/",
                    normalized_url="https://example.test",
                    is_homepage=True,
                    title="Example",
                    word_count=120,
                )
            )
            user_id = user.id

        with session_scope() as session:
            user = session.get(User, user_id)
            assert user is not None, "user not persisted"
            assert len(user.businesses) == 1, f"user.businesses={user.businesses}"
            biz = user.businesses[0]
            assert biz.profile is not None, "business.profile not loaded"
            assert biz.profile.services == ["SEO audits"], "JSON column round-trip"
            assert len(biz.websites) == 1, f"business.websites={biz.websites}"
            site = biz.websites[0]
            assert len(site.scans) == 1, f"website.scans={site.scans}"
            assert len(site.scans[0].pages) == 1
            # reverse direction: page -> scan -> website -> business -> owner
            page = site.scans[0].pages[0]
            assert page.scan.website.business.owner.id == user_id
            assert page.was_fetched is True
        return f"user_id={user_id}"

    @check("cascade delete removes the whole tree")
    def _cascade():
        from sqlalchemy import func, select

        from app.core.database import session_scope
        from app.core.security import hash_password
        from app.models.business import Business
        from app.models.user import User
        from app.models.website import Website, WebsiteScan

        suffix = uuid.uuid4().hex[:8]
        with session_scope() as session:
            user = User(
                email=f"cascade-{suffix}@example.test",
                password_hash=hash_password(suffix),
            )
            session.add(user)
            session.flush()
            business = Business(owner_id=user.id, name=f"Cascade Co {suffix}")
            session.add(business)
            session.flush()
            website = Website(
                business_id=business.id,
                url="https://cascade.test/",
                normalized_url="https://cascade.test",
                registrable_domain="cascade.test",
            )
            session.add(website)
            session.flush()
            session.add(WebsiteScan(website_id=website.id, sequence=1))
            user_id, scan_owner = user.id, website.id

        with session_scope() as session:
            session.delete(session.get(User, user_id))

        with session_scope() as session:
            leftover = session.scalar(
                select(func.count())
                .select_from(WebsiteScan)
                .where(WebsiteScan.website_id == scan_owner)
            )
            assert leftover == 0, f"{leftover} orphaned scan rows survived"
        return "user delete cascaded to scans"

    @check("FK enforcement is active on SQLite (PRAGMA foreign_keys=ON)")
    def _fk_enforced():
        from sqlalchemy.exc import IntegrityError

        from app.core.config import get_settings
        from app.core.database import SessionLocal
        from app.models.business import Business

        if not get_settings().is_sqlite:
            return "skipped (not SQLite)"
        session = SessionLocal()
        try:
            session.add(Business(owner_id=uuid.uuid4(), name="Orphan Co"))
            try:
                session.commit()
            except IntegrityError:
                return "rejected orphan FK as expected"
            raise AssertionError(
                "inserted a Business with a non-existent owner_id -- "
                "SQLite FK enforcement is NOT active"
            )
        finally:
            session.rollback()
            session.close()

    @check("the honesty constraints actually reject a forged 'live' row")
    def _honesty_enforced():
        from sqlalchemy.exc import IntegrityError

        from app.core.database import SessionLocal
        from app.core.security import hash_password
        from app.models.business import Business
        from app.models.publishing import PlatformConnection
        from app.models.user import User

        suffix = uuid.uuid4().hex[:8]
        session = SessionLocal()
        try:
            user = User(
                email=f"honesty-{suffix}@example.test",
                password_hash=hash_password(suffix),
            )
            session.add(user)
            session.flush()
            business = Business(owner_id=user.id, name=f"Honesty Co {suffix}")
            session.add(business)
            session.flush()

            # A connection claiming mode="live" with no stored token is exactly
            # the "pretend the integration is real" case the spec forbids.
            session.add(
                PlatformConnection(
                    business_id=business.id,
                    platform="linkedin",
                    mode="live",
                    status="connected",
                    access_token_encrypted=None,
                )
            )
            try:
                session.flush()
            except IntegrityError:
                return "database rejected mode='live' with no token"
            raise AssertionError(
                "stored a live PlatformConnection with no access token -- the "
                "live_connection_requires_token CHECK is not being enforced"
            )
        finally:
            session.rollback()
            session.close()

    print("\n== 6. core services ==")

    @check("Argon2id hash + verify")
    def _passwords():
        from app.core.security import hash_password, verify_password

        digest = hash_password("correct horse battery staple")
        assert digest.startswith("$argon2id$"), f"unexpected hash format: {digest[:20]}"
        assert verify_password("correct horse battery staple", digest)
        assert not verify_password("wrong password", digest)
        return "argon2id"

    @check("logging redacts secrets at depth and inline in text")
    def _logging():
        # The logger writes straight to sys.stderr via WriteLoggerFactory, and
        # caches that stream on first use, so capturing emitted output is
        # unreliable. Exercise the redaction processor itself instead -- that is
        # the component the guarantee actually rests on.
        from app.core.logging import REDACTED, _redact_processor, configure_logging

        configure_logging()  # must not raise
        event = {
            "event": "redaction_probe",
            "gemini_api_key": "LEAK_SENTINEL_A",
            "nested": {"deep": {"password": "LEAK_SENTINEL_B"}},
            "items": [{"refresh_token": "LEAK_SENTINEL_C"}],
            "message": "called with Authorization: Bearer LEAK_SENTINEL_D_abcdefgh",
            "trace": "key AQ.LEAK_SENTINEL_E_aaaaaaaaaaaaaaaaaaaa rejected",
            "legacy": "key AIzaLEAK_SENTINEL_F_aaaaaaaaaa rejected",
            "keep": "not-a-secret",
        }
        scrubbed = _redact_processor(None, "info", event)
        rendered = repr(scrubbed)
        leaked = [
            sentinel
            for sentinel in (
                "LEAK_SENTINEL_A",
                "LEAK_SENTINEL_B",
                "LEAK_SENTINEL_C",
                "LEAK_SENTINEL_D",
                "LEAK_SENTINEL_E",
                "LEAK_SENTINEL_F",
            )
            if sentinel in rendered
        ]
        if leaked:
            raise AssertionError(f"secrets survived redaction: {leaked}\n{rendered}")
        assert scrubbed["gemini_api_key"] == REDACTED
        assert scrubbed["keep"] == "not-a-secret", "over-redacted a safe field"
        return "nested dict, list, Bearer, AQ. and AIza keys all redacted"

    @check("the configured Gemini key never appears in a log event")
    def _real_key_not_loggable():
        from app.core.config import get_settings
        from app.core.logging import _redact_processor

        key = get_settings().gemini_api_key
        if not key:
            return "skipped (no GEMINI_API_KEY configured)"
        event = {
            "event": "ai_call_failed",
            "gemini_api_key": key,
            "detail": f"request rejected for key {key}",
        }
        rendered = repr(_redact_processor(None, "error", event))
        if key in rendered:
            raise AssertionError(
                "the real GEMINI_API_KEY passed through the redaction processor "
                "unmasked -- inline pattern does not cover this key format"
            )
        return "real key redacted both as a field and inline"

    @check("errors serialise to the documented envelope, without internals")
    def _errors():
        from app.core.errors import AuraError, WebsiteUnreachableError

        payload = AuraError("plain message").to_payload()
        assert set(payload) == {"error"}, f"unexpected envelope keys: {sorted(payload)}"
        assert payload["error"]["code"] == "error"
        assert payload["error"]["message"] == "plain message"

        rich = WebsiteUnreachableError(detail="Connection timed out after 15s")
        body = rich.to_payload()["error"]
        assert body["code"] == "website_unreachable"
        assert body["hint"], "class-level hint was dropped"
        assert rich.status_code == 502, f"status_code={rich.status_code}"

        # A traceback must never be part of the client payload.
        assert "traceback" not in repr(payload).lower()
        return "AuraError + subclass envelopes correct"

    @check("capability_report() reflects the real environment")
    def _capabilities():
        from app.core.config import get_settings

        report = get_settings().capability_report()
        connectors = ", ".join(
            f"{name}={info['mode']}" for name, info in report["connectors"].items()
        )
        return (
            f"env={report['environment']} db={report['database']} "
            f"auth={report['auth_provider']} ai={report['ai']['enabled']} "
            f"| {connectors}"
        )

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
