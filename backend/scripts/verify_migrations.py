"""Verify the Alembic migration chain end to end.

Run from ``backend/``::

    python scripts/verify_migrations.py

Why this exists as a script rather than a note in a README: a migration that
*applies* is not the same as a migration that is *correct*. This checks the four
properties that actually matter and that all fail silently otherwise:

* ``upgrade head`` builds every table the models declare, **and records the
  revision** (pysqlite autocommits DDL but not the ``alembic_version`` INSERT, so
  a mis-written ``env.py`` produces a full schema with no revision stamped).
* ``alembic check`` reports no drift -- the migration and the models agree.
* ``downgrade base`` genuinely reverses, leaving no orphan tables behind.
* The same script compiles to correct **PostgreSQL** DDL, preserving JSONB and
  native UUID, even though local development runs on SQLite.

Everything runs against a throwaway database; the developer's ``aura.db`` is
never touched.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

FAILURES: list[str] = []
PASSES = 0


def check(label: str):
    def decorator(fn):
        global PASSES
        try:
            detail = fn()
        except Exception as exc:  # noqa: BLE001 - this is the reporting boundary
            FAILURES.append(label)
            print(f"  [FAIL] {label}")
            for line in str(exc).splitlines()[:6]:
                print(f"         {line}")
        else:
            PASSES += 1
            print(f"  [PASS] {label}" + (f" -- {detail}" if detail else ""))
        return fn

    return decorator


# No alembic.ini is shipped on purpose (the DB URL comes from app settings,
# never from a tracked file), so the CLI is pointed at a throwaway config that
# only sets the script location. env.py reads the URL from settings either way.
_ALEMBIC_INI = Path(tempfile.mkdtemp(prefix="aura-alembic-ini-")) / "alembic.ini"
_ALEMBIC_INI.write_text(
    "[alembic]\n"
    f"script_location = {BACKEND_DIR / 'alembic'}\n"
    # env.py runs logging.config.fileConfig on the ini when one is present, so
    # the standard logging sections must exist even though nothing is tuned.
    "[loggers]\nkeys = root\n"
    "[handlers]\nkeys = console\n"
    "[formatters]\nkeys = generic\n"
    "[logger_root]\nlevel = WARN\nhandlers = console\n"
    "[handler_console]\nclass = StreamHandler\nargs = (sys.stderr,)\n"
    "level = NOTSET\nformatter = generic\n"
    "[formatter_generic]\nformat = %(levelname)-5.5s [%(name)s] %(message)s\n"
)


def alembic(*args: str, db_url: str, expect_ok: bool = True) -> str:
    """Invoke the Alembic CLI in a subprocess pinned to *db_url*."""
    env = {**os.environ, "DATABASE_URL": db_url}
    # S603 is suppressed below: the argv is `sys.executable -m alembic <literal
    # flags>` with no shell and no caller-supplied string; db_url travels in the
    # environment, never the command line.
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "-c", str(_ALEMBIC_INI), *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    output = proc.stdout + proc.stderr
    if expect_ok and proc.returncode != 0:
        raise AssertionError(
            f"`alembic {' '.join(args)}` exited {proc.returncode}\n{output}"
        )
    return output


def table_names(sqlite_path: Path) -> list[str]:
    from sqlalchemy import create_engine, inspect

    engine = create_engine(f"sqlite+pysqlite:///{sqlite_path.as_posix()}")
    try:
        return sorted(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def expected_tables() -> set[str]:
    import app.models  # noqa: F401
    from app.models.base import Base

    return set(Base.metadata.tables)


def main() -> int:  # noqa: C901 - a linear script of checks reads better flat
    workdir = Path(tempfile.mkdtemp(prefix="aura-migrate-"))
    db_path = workdir / "migrate.db"
    db_url = f"sqlite+pysqlite:///{db_path.as_posix()}"
    print(f"(scratch database: {db_path})")

    declared = expected_tables()

    print("\n== 1. forward migration ==")

    @check("upgrade head creates every declared table")
    def _upgrade():
        alembic("upgrade", "head", db_url=db_url)
        actual = set(table_names(db_path))
        missing = declared - actual
        if missing:
            raise AssertionError(f"tables not created: {sorted(missing)}")
        unexpected = actual - declared - {"alembic_version"}
        if unexpected:
            raise AssertionError(f"unexpected tables: {sorted(unexpected)}")
        return f"{len(declared)} tables + alembic_version"

    @check("the revision is actually recorded (not a silent half-apply)")
    def _stamped():
        raw = alembic("current", db_url=db_url)
        # Alembic logs to stderr; the revision itself is the only line matching
        # a 12-hex-digit id, so pick that out rather than trusting line order.
        revision = next(
            (
                line.strip()
                for line in raw.splitlines()
                if re.match(r"^[0-9a-f]{12}\b", line.strip())
            ),
            None,
        )
        if revision is None:
            raise AssertionError(
                "alembic_version is empty after `upgrade head`. The schema was "
                "created but the revision was never committed -- see the "
                "explicit connection.commit() in alembic/env.py.\n"
                f"output: {raw!r}"
            )
        if "(head)" not in revision:
            raise AssertionError(f"not at head: {revision!r}")
        return revision

    print("\n== 2. migration matches the models ==")

    @check("alembic check reports no drift")
    def _no_drift():
        output = alembic("check", db_url=db_url)
        if "No new upgrade operations detected" not in output:
            raise AssertionError(f"drift detected:\n{output}")
        return "models and migration agree"

    print("\n== 3. reverse migration ==")

    @check("downgrade base drops every table it created")
    def _downgrade():
        alembic("downgrade", "base", db_url=db_url)
        remaining = set(table_names(db_path)) - {"alembic_version"}
        if remaining:
            raise AssertionError(f"downgrade left orphan tables: {sorted(remaining)}")
        return "only alembic_version remains"

    @check("upgrade head is re-runnable after a full downgrade")
    def _reupgrade():
        alembic("upgrade", "head", db_url=db_url)
        actual = set(table_names(db_path))
        missing = declared - actual
        if missing:
            raise AssertionError(f"second upgrade incomplete: {sorted(missing)}")
        output = alembic("check", db_url=db_url)
        if "No new upgrade operations detected" not in output:
            raise AssertionError(f"drift after re-upgrade:\n{output}")
        return "full down/up cycle is clean"

    print("\n== 4. PostgreSQL target (offline SQL, no server needed) ==")

    @check("the same migration compiles to valid PostgreSQL DDL")
    def _pg():
        sql = alembic(
            "-x",
            "db_url=postgresql+psycopg://user:pass@localhost:5432/aura",
            "upgrade",
            "head",
            "--sql",
            db_url=db_url,
        )
        creates = sql.count("CREATE TABLE")
        if creates < len(declared):
            raise AssertionError(
                f"only {creates} CREATE TABLE for {len(declared)} tables"
            )
        return f"{creates} CREATE TABLE statements"

    @check("Postgres keeps JSONB and native UUID (variant not flattened)")
    def _pg_types():
        sql = alembic(
            "-x",
            "db_url=postgresql+psycopg://user:pass@localhost:5432/aura",
            "upgrade",
            "head",
            "--sql",
            db_url=db_url,
        )
        if "JSONB" not in sql:
            raise AssertionError(
                "no JSONB in Postgres DDL -- the JSON/JSONB variant was flattened "
                "to plain JSON, losing GIN-indexability. Check render_item in env.py."
            )
        if " UUID" not in sql:
            raise AssertionError("no native UUID columns in Postgres DDL")
        if "CURRENT_TIMESTAMP" in sql:
            raise AssertionError(
                "Postgres DDL contains SQLite's CURRENT_TIMESTAMP literal instead "
                "of now(); the server_default was frozen to one dialect."
            )
        return f"{sql.count('JSONB')} JSONB columns, now() defaults"

    @check("honesty CHECK constraints survive into Postgres DDL")
    def _pg_honesty():
        sql = alembic(
            "-x",
            "db_url=postgresql+psycopg://user:pass@localhost:5432/aura",
            "upgrade",
            "head",
            "--sql",
            db_url=db_url,
        )
        required = [
            "ck_platform_connections_live_connection_requires_token",
            "ck_posts_published_live_post_requires_remote_id",
        ]
        missing = [name for name in required if name not in sql]
        if missing:
            raise AssertionError(f"missing from Postgres DDL: {missing}")
        return "simulated-as-real stays unforgeable on Postgres"

    shutil.rmtree(workdir, ignore_errors=True)

    print()
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} CHECK(S) FAILED")
        for label in FAILURES:
            print(f"  - {label}")
        return 1
    print(f"RESULT: ALL {PASSES} CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
