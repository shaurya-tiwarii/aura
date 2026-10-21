"""End-to-end verification of the authentication stage.

Drives the real HTTP surface with FastAPI's ``TestClient`` against a throwaway
SQLite database built by the *actual Alembic migrations*, so this proves the
shipped schema and the shipped routes agree.

The emphasis is on the claims that would otherwise be taken on faith:

* **Sign-out actually signs out.** The access token is rejected on the very next
  request, not when it eventually expires. Anything less would make the logout
  button decorative -- exactly the fake functionality the spec forbids.
* **Refresh tokens are single-use, and replay is detected.** Presenting a rotated
  token revokes the whole family, including the newest token.
* **Neither login failure mode is distinguishable.** A wrong password and an
  unregistered address return byte-identical responses.
* **Raw secrets are never stored.** The refresh token is checked to be absent
  from the database; only its SHA-256 hash is there.
* **The Supabase backend really verifies signatures**, using locally minted
  tokens, since no Supabase project exists yet. Wrong key, expired, wrong
  audience, and ``alg=none`` are all rejected.
* **Unsupported operations fail loudly**, with capabilities advertising them as
  unavailable beforehand.

Usage
-----
    python scripts/verify_auth.py
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

# Must happen before any app import: settings is a cached singleton, and the
# database engine is built from it at module import time. Pointing at a scratch
# file keeps the developer's aura.db untouched.
_SCRATCH_DIR = Path(tempfile.mkdtemp(prefix="aura-verify-auth-"))
_SCRATCH_DB = _SCRATCH_DIR / "auth.db"
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
            # Blind on purpose: one failing check must not abort the other 39.
            _failures.append(label)
            print(f"  [FAIL] {label}")
            traceback.print_exc()
        else:
            suffix = f" -- {detail}" if detail else ""
            print(f"  [PASS] {label}{suffix}")
        return fn

    return decorator


PASSWORD = "correct-horse-battery-staple"
NEW_PASSWORD = "an-entirely-different-passphrase"

# RFC 2606 reserves ``.example``, so these addresses can never belong to anyone.
# Note it is *not* ``.test``: email-validator rejects special-use TLDs (arpa,
# invalid, local, localhost, onion, test) outright, and rightly so -- accepting a
# sign-up at ``you@example.test`` would create an account that can never receive
# mail. This script uses a domain the real validator accepts, so it exercises the
# same path a genuine registration takes.
EMAIL = "founder@bakery.example"
OTHER_EMAIL = "colleague@bakery.example"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def main() -> int:  # noqa: C901 - a linear script of checks reads better flat
    print(f"(scratch database: {_SCRATCH_DB})")

    # ---------------------------------------------------------------- schema
    print("\n== 1. schema from real migrations ==")

    @check("alembic upgrade head builds the auth schema")
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

        tables = set(inspect(engine).get_table_names())
        for required in ("users", "user_sessions", "password_reset_tokens"):
            assert required in tables, f"{required} missing after upgrade"
        return f"{len(tables)} tables, user_sessions present"

    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app(), base_url="http://testserver")
    api = "/api/v1"

    # --------------------------------------------------------------- probes
    print("\n== 2. health and capability reporting ==")

    @check("GET /health/live answers without touching the database")
    def _live():
        r = client.get("/health/live")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ok"

    @check("GET /health/ready reports a real database probe")
    def _ready():
        r = client.get("/health/ready")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["database"] == "connected", body
        return f"backend={body['database_backend']}"

    @check("GET /meta/capabilities is honest and leaks no secret values")
    def _caps():
        r = client.get(f"{api}/meta/capabilities")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["auth_provider"] == "local"
        # Connectors have no credentials in this build; every one must say demo.
        modes = {name: cfg["mode"] for name, cfg in body["connectors"].items()}
        assert set(modes.values()) == {"demo"}, modes
        # The report may say *whether* a credential exists, never what it is.
        from app.core.config import settings

        blob = r.text
        for secret in (
            settings.gemini_api_key,
            settings.secret_key,
            settings.token_encryption_key,
        ):
            if secret:
                assert secret not in blob, "a secret value appeared in the report"
        return f"connectors={sorted(modes)} all demo"

    @check("GET /auth/capabilities advertises the local provider's real abilities")
    def _auth_caps():
        r = client.get(f"{api}/auth/capabilities")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["provider"] == "local"
        assert all(
            body[flag]
            for flag in (
                "can_register",
                "can_login_with_password",
                "can_refresh",
                "can_reset_password",
                "can_change_password",
                "can_revoke_sessions",
            )
        ), body
        # The note must disclose the missing SMTP transport rather than imply
        # reset emails are sent.
        assert "smtp" in body["notes"].lower()

    # ------------------------------------------------------------- register
    print("\n== 3. registration ==")

    state: dict[str, str] = {}

    @check("POST /auth/register creates an account and returns a usable session")
    def _register():
        r = client.post(
            f"{api}/auth/register",
            json={"email": EMAIL, "password": PASSWORD, "full_name": "  Ada Byron  "},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["user"]["email"] == EMAIL
        assert body["user"]["full_name"] == "Ada Byron", "name not trimmed"
        # No mail transport exists, so a claim of verification would be a lie.
        assert body["user"]["is_email_verified"] is False
        assert body["tokens"]["access_token"]
        assert body["tokens"]["refresh_token"]
        state["access"] = body["tokens"]["access_token"]
        state["refresh"] = body["tokens"]["refresh_token"]
        assert PASSWORD not in r.text, "password echoed in the response"
        assert "password_hash" not in r.text
        return f"expires_in={body['tokens']['expires_in']}s"

    @check("duplicate registration is refused")
    def _dupe():
        r = client.post(
            f"{api}/auth/register", json={"email": EMAIL, "password": PASSWORD}
        )
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "email_already_registered"

    @check("a short password is rejected by schema validation")
    def _short():
        r = client.post(
            f"{api}/auth/register",
            json={"email": "short-pw@aura-verify.example", "password": "short"},
        )
        assert r.status_code == 422, r.text

    @check("a long-but-common password is rejected by the strength policy")
    def _weak():
        r = client.post(
            f"{api}/auth/register",
            json={"email": "weak-pw@aura-verify.example", "password": "password1234"},
        )
        assert r.status_code == 422, r.text
        body = r.json()
        assert "password" in body["error"].get("fields", {}), body
        return body["error"]["fields"]["password"]

    @check("email case and surrounding whitespace are normalised")
    def _normalise():
        r = client.post(
            f"{api}/auth/login",
            json={"email": f"  {EMAIL.upper()}  ", "password": PASSWORD},
        )
        assert r.status_code == 200, r.text
        assert r.json()["user"]["email"] == EMAIL

    # ---------------------------------------------------------------- login
    print("\n== 4. login and token verification ==")

    @check("wrong password and unknown email are indistinguishable")
    def _no_enumeration():
        wrong = client.post(
            f"{api}/auth/login", json={"email": EMAIL, "password": "not-the-password"}
        )
        missing = client.post(
            f"{api}/auth/login",
            json={
                "email": "nobody@aura-verify.example",
                "password": "not-the-password",
            },
        )
        assert wrong.status_code == missing.status_code == 401, (
            wrong.status_code,
            missing.status_code,
        )
        a, b = wrong.json()["error"], missing.json()["error"]
        assert a["code"] == b["code"], (a["code"], b["code"])
        assert a["message"] == b["message"], "responses differ; account enumeration"
        return f"both -> {a['code']!r}"

    @check("GET /auth/me returns the signed-in account")
    def _me():
        r = client.get(f"{api}/auth/me", headers=_bearer(state["access"]))
        assert r.status_code == 200, r.text
        assert r.json()["email"] == EMAIL

    @check("GET /auth/me without a token is 401 not_authenticated")
    def _no_token():
        r = client.get(f"{api}/auth/me")
        assert r.status_code == 401, r.text
        assert r.json()["error"]["code"] == "not_authenticated"

    @check("a malformed token is rejected")
    def _garbage():
        r = client.get(f"{api}/auth/me", headers=_bearer("not.a.jwt"))
        assert r.status_code == 401, r.text
        assert r.json()["error"]["code"] == "token_invalid"

    @check("a token signed with the wrong key is rejected")
    def _wrong_key():
        import jwt

        from app.auth.tokens import ALGORITHM, AUDIENCE, ISSUER
        from app.core.security import generate_token

        forged = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "sid": str(uuid.uuid4()),
                "typ": "access",
                "iss": ISSUER,
                "aud": AUDIENCE,
                "iat": 1_800_000_000,
                "exp": 4_000_000_000,
            },
            generate_token(32),  # not settings.secret_key
            algorithm=ALGORITHM,
        )
        r = client.get(f"{api}/auth/me", headers=_bearer(forged))
        assert r.status_code == 401, r.text
        assert r.json()["error"]["code"] == "token_invalid"

    @check("an alg=none token is rejected (algorithm confusion)")
    def _alg_none():
        import jwt

        from app.auth.tokens import AUDIENCE, ISSUER

        forged = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "typ": "access",
                "iss": ISSUER,
                "aud": AUDIENCE,
                "iat": 1_800_000_000,
                "exp": 4_000_000_000,
            },
            key="",
            algorithm="none",
        )
        r = client.get(f"{api}/auth/me", headers=_bearer(forged))
        assert r.status_code == 401, r.text

    @check("a refresh token cannot be used as a bearer access token")
    def _wrong_type():
        r = client.get(f"{api}/auth/me", headers=_bearer(state["refresh"]))
        assert r.status_code == 401, r.text
        assert r.json()["error"]["code"] == "token_invalid"

    @check("an expired access token is rejected as expired")
    def _expired():
        from app.auth.tokens import mint_access_token

        token, _ = mint_access_token(
            user_id=uuid.uuid4(),
            email=EMAIL,
            session_id=uuid.uuid4(),
            ttl_minutes=5,
        )
        # Rewind the clock past both the TTL and the 30s leeway.
        import jwt

        from app.core.config import settings

        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=["HS256"],
            audience="aura-api",
            issuer="aura",
        )
        payload["iat"] = 1_700_000_000
        payload["exp"] = 1_700_000_600
        stale = jwt.encode(payload, settings.secret_key, algorithm="HS256")
        r = client.get(f"{api}/auth/me", headers=_bearer(stale))
        assert r.status_code == 401, r.text
        assert r.json()["error"]["code"] == "token_expired", r.text

    @check("the raw refresh token is never stored, only its hash")
    def _hash_only():
        from sqlalchemy import text

        from app.core.database import engine

        raw = state["refresh"]
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT refresh_token_hash FROM user_sessions"))
            stored = [row[0] for row in rows]
        assert stored, "no sessions recorded"
        assert raw not in stored, "raw refresh token found in the database"
        assert all(len(value) == 64 for value in stored), "not a SHA-256 hex digest"
        return f"{len(stored)} session rows, all 64-char digests"

    # -------------------------------------------------------------- refresh
    print("\n== 5. refresh rotation and replay detection ==")

    @check("POST /auth/refresh rotates the refresh token")
    def _rotate():
        r = client.post(f"{api}/auth/refresh", json={"refresh_token": state["refresh"]})
        assert r.status_code == 200, r.text
        rotated = r.json()["tokens"]["refresh_token"]
        assert rotated != state["refresh"], "refresh token was reused, not rotated"
        state["old_refresh"] = state["refresh"]
        state["refresh"] = rotated
        state["access"] = r.json()["tokens"]["access_token"]

    @check("the rotated-away refresh token no longer works")
    def _old_dead():
        r = client.post(
            f"{api}/auth/refresh", json={"refresh_token": state["old_refresh"]}
        )
        assert r.status_code == 401, r.text
        assert r.json()["error"]["code"] == "refresh_token_revoked", r.text

    @check("replaying a rotated token revokes the whole family, newest included")
    def _reuse_kills_family():
        # The previous check already replayed the old token, which is the theft
        # signal. The current, legitimate token must now be dead too.
        r = client.post(f"{api}/auth/refresh", json={"refresh_token": state["refresh"]})
        assert r.status_code == 401, r.text
        assert r.json()["error"]["code"] == "refresh_token_revoked", r.text
        # And so must the access token minted alongside it.
        me = client.get(f"{api}/auth/me", headers=_bearer(state["access"]))
        assert me.status_code == 401, me.text
        assert me.json()["error"]["code"] == "session_revoked", me.text
        return "family revoked; access token dead immediately"

    @check("an unknown refresh token is rejected")
    def _unknown_refresh():
        from app.core.security import generate_token

        r = client.post(
            f"{api}/auth/refresh", json={"refresh_token": generate_token(48)}
        )
        assert r.status_code == 401, r.text
        assert r.json()["error"]["code"] == "refresh_token_invalid"

    # --------------------------------------------------------------- logout
    print("\n== 6. sessions and revocation ==")

    def _login(password: str = PASSWORD, agent: str = "verify/1.0") -> tuple[str, str]:
        r = client.post(
            f"{api}/auth/login",
            json={"email": EMAIL, "password": password},
            headers={"User-Agent": agent},
        )
        assert r.status_code == 200, r.text
        tokens = r.json()["tokens"]
        return tokens["access_token"], tokens["refresh_token"]

    @check("POST /auth/logout invalidates the access token on the next request")
    def _logout_now():
        access, _ = _login()
        before = client.get(f"{api}/auth/me", headers=_bearer(access))
        assert before.status_code == 200, before.text

        out = client.post(f"{api}/auth/logout", headers=_bearer(access))
        assert out.status_code == 200, out.text
        assert out.json()["status"] == "signed_out", out.text

        after = client.get(f"{api}/auth/me", headers=_bearer(access))
        assert after.status_code == 401, "token still worked after logout"
        assert after.json()["error"]["code"] == "session_revoked", after.text
        return "revoked server-side, not just client-side"

    @check("GET /auth/sessions lists real rows and marks the current one")
    def _sessions():
        first, _ = _login(agent="device-one/1.0")
        _login(agent="device-two/1.0")
        r = client.get(f"{api}/auth/sessions", headers=_bearer(first))
        assert r.status_code == 200, r.text
        rows = r.json()
        assert len(rows) >= 2, rows
        current = [row for row in rows if row["is_current"]]
        assert (
            len(current) == 1
        ), f"expected exactly one current session, got {len(current)}"
        agents = {row["user_agent"] for row in rows}
        assert "device-one/1.0" in agents and "device-two/1.0" in agents, agents
        state["session_access"] = first
        return f"{len(rows)} active sessions, 1 marked current"

    @check("DELETE /auth/sessions/{id} kills that session and spares the caller")
    def _revoke_one():
        access = state["session_access"]
        rows = client.get(f"{api}/auth/sessions", headers=_bearer(access)).json()
        other = next(row for row in rows if not row["is_current"])

        r = client.delete(f"{api}/auth/sessions/{other['id']}", headers=_bearer(access))
        assert r.status_code == 200, r.text
        # The caller's own session must survive.
        assert client.get(f"{api}/auth/me", headers=_bearer(access)).status_code == 200
        remaining = client.get(f"{api}/auth/sessions", headers=_bearer(access)).json()
        assert other["id"] not in {row["id"] for row in remaining}

    @check("revoking a stranger's session is indistinguishable from a missing one")
    def _no_session_oracle():
        access = state["session_access"]
        # A real session belonging to somebody else.
        other = client.post(
            f"{api}/auth/register",
            json={"email": OTHER_EMAIL, "password": PASSWORD},
        )
        assert other.status_code == 201, other.text
        stranger_access = other.json()["tokens"]["access_token"]
        stranger_rows = client.get(
            f"{api}/auth/sessions", headers=_bearer(stranger_access)
        ).json()
        stranger_id = stranger_rows[0]["id"]

        theirs = client.delete(
            f"{api}/auth/sessions/{stranger_id}", headers=_bearer(access)
        )
        nowhere = client.delete(
            f"{api}/auth/sessions/{uuid.uuid4()}", headers=_bearer(access)
        )
        assert theirs.status_code == nowhere.status_code == 404, (
            theirs.status_code,
            nowhere.status_code,
        )
        assert theirs.json()["error"]["message"] == nowhere.json()["error"]["message"]
        # And it really was not revoked.
        assert (
            client.get(f"{api}/auth/me", headers=_bearer(stranger_access)).status_code
            == 200
        ), "another user's session was revoked"
        return "identical 404s; stranger's session untouched"

    @check("POST /auth/logout-all revokes every session for the account")
    def _logout_all():
        access, _ = _login()
        r = client.post(f"{api}/auth/logout-all", headers=_bearer(access))
        assert r.status_code == 200, r.text
        count = r.json()["detail"]["sessions_revoked"]
        assert count >= 1, r.text
        assert client.get(f"{api}/auth/me", headers=_bearer(access)).status_code == 401
        assert (
            client.get(f"{api}/auth/sessions", headers=_bearer(access)).status_code
            == 401
        )
        return f"{count} sessions revoked"

    # ------------------------------------------------------------ passwords
    print("\n== 7. password change and reset ==")

    @check("changing a password requires the current one")
    def _change_needs_current():
        access, _ = _login()
        r = client.post(
            f"{api}/auth/password/change",
            headers=_bearer(access),
            json={
                "current_password": "wrong-password-here",
                "new_password": NEW_PASSWORD,
            },
        )
        assert r.status_code == 401, r.text
        assert r.json()["error"]["code"] == "current_password_incorrect"

    @check("re-using the same password is refused")
    def _change_unchanged():
        access, _ = _login()
        r = client.post(
            f"{api}/auth/password/change",
            headers=_bearer(access),
            json={"current_password": PASSWORD, "new_password": PASSWORD},
        )
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "password_unchanged"

    @check("a successful change keeps this session and ends the others")
    def _change_ok():
        keep, _ = _login(agent="keeper/1.0")
        doomed, _ = _login(agent="doomed/1.0")

        r = client.post(
            f"{api}/auth/password/change",
            headers=_bearer(keep),
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        )
        assert r.status_code == 200, r.text
        revoked = r.json()["detail"]["other_sessions_revoked"]
        assert revoked >= 1, r.text

        assert client.get(f"{api}/auth/me", headers=_bearer(keep)).status_code == 200
        assert client.get(f"{api}/auth/me", headers=_bearer(doomed)).status_code == 401
        # The old password must no longer work.
        old = client.post(
            f"{api}/auth/login", json={"email": EMAIL, "password": PASSWORD}
        )
        assert old.status_code == 401, "old password still accepted"
        new = client.post(
            f"{api}/auth/login", json={"email": EMAIL, "password": NEW_PASSWORD}
        )
        assert new.status_code == 200, new.text
        return f"{revoked} other sessions ended"

    @check("forgot-password answers identically for known and unknown addresses")
    def _forgot_generic():
        known = client.post(f"{api}/auth/password/forgot", json={"email": EMAIL})
        unknown = client.post(
            f"{api}/auth/password/forgot", json={"email": "ghost@aura-verify.example"}
        )
        assert known.status_code == unknown.status_code == 200
        a, b = known.json(), unknown.json()
        assert a["status"] == b["status"] == "accepted"
        assert a["message"] == b["message"], "messages differ; account enumeration"
        # Delivery must not claim an email was sent -- no SMTP is configured.
        assert a["delivery"] == "not_configured" == b["delivery"]
        # Outside production the real link is returned so the flow is completable,
        # and only for an address that exists.
        assert a["reset_url"], "no reset link returned in development"
        assert b["reset_url"] is None, "a link was returned for an unknown address"
        state["reset_url"] = a["reset_url"]
        return "same body; delivery=not_configured"

    @check("the reset token resets the password and ends every session")
    def _reset():
        from urllib.parse import parse_qs, urlparse

        token = parse_qs(urlparse(state["reset_url"]).query)["token"][0]
        state["reset_token"] = token

        # A live session that must not survive a reset.
        live, _ = _login(password=NEW_PASSWORD)
        assert client.get(f"{api}/auth/me", headers=_bearer(live)).status_code == 200

        r = client.post(
            f"{api}/auth/password/reset",
            json={"token": token, "new_password": "third-distinct-passphrase"},
        )
        assert r.status_code == 200, r.text
        assert (
            client.get(f"{api}/auth/me", headers=_bearer(live)).status_code == 401
        ), "a session survived a password reset"
        assert (
            client.post(
                f"{api}/auth/login",
                json={"email": EMAIL, "password": "third-distinct-passphrase"},
            ).status_code
            == 200
        )
        return "all sessions ended, new password active"

    @check("a reset token is single-use")
    def _reset_replay():
        r = client.post(
            f"{api}/auth/password/reset",
            json={"token": state["reset_token"], "new_password": "fourth-passphrase-x"},
        )
        assert r.status_code == 404, r.text
        assert r.json()["error"]["code"] == "reset_token_invalid"

    @check("an unknown reset token is refused")
    def _reset_unknown():
        from app.core.security import generate_token

        r = client.post(
            f"{api}/auth/password/reset",
            json={"token": generate_token(32), "new_password": "fifth-passphrase-xy"},
        )
        assert r.status_code == 404, r.text

    # --------------------------------------------------------------- supabase
    print("\n== 8. supabase provider (offline JWT verification) ==")

    @check("a correctly signed Supabase token is accepted and mirrors the user")
    def _supabase_ok():
        import time

        import jwt

        from app.auth.supabase import SupabaseAuthBackend
        from app.core.config import settings
        from app.core.database import SessionLocal

        secret = "test-only-supabase-jwt-secret-value"
        original = settings.supabase_jwt_secret
        settings.supabase_jwt_secret = secret
        try:
            backend = SupabaseAuthBackend()
            subject = str(uuid.uuid4())
            token = jwt.encode(
                {
                    "sub": subject,
                    "email": "Supabase.User@Mirror.example",
                    "aud": settings.supabase_jwt_audience,
                    "iat": int(time.time()),
                    "exp": int(time.time()) + 3600,
                    "email_confirmed_at": "2026-01-01T00:00:00Z",
                    "user_metadata": {"full_name": "Grace Hopper"},
                },
                secret,
                algorithm="HS256",
            )
            claims = backend.verify_token(token)
            assert claims.subject == subject
            assert claims.session_id is None, "supabase has no local session row"

            with SessionLocal() as db:
                user = backend.resolve_user(db, claims)
                db.commit()
                assert user.email == "supabase.user@mirror.example", user.email
                assert user.full_name == "Grace Hopper"
                assert user.password_hash is None, "no password for a mirrored account"
                assert user.is_email_verified is True, "should trust the token's claim"
                mirrored = str(user.id)

            # Second call must find the same row, not create another.
            with SessionLocal() as db:
                again = backend.resolve_user(db, claims)
                assert str(again.id) == mirrored, "duplicate mirror created"
            return f"mirrored once as {mirrored[:8]}..."
        finally:
            settings.supabase_jwt_secret = original

    @check("Supabase tokens with a wrong key, expiry, or audience are rejected")
    def _supabase_rejects():
        import time

        import jwt

        from app.auth.supabase import SupabaseAuthBackend
        from app.core.config import settings
        from app.core.errors import AuthenticationError

        secret = "test-only-supabase-jwt-secret-value"
        original = settings.supabase_jwt_secret
        settings.supabase_jwt_secret = secret
        try:
            backend = SupabaseAuthBackend()
            now = int(time.time())
            base = {
                "sub": str(uuid.uuid4()),
                "email": "rejected@aura-verify.example",
                "aud": settings.supabase_jwt_audience,
                "iat": now,
                "exp": now + 3600,
            }
            cases = {
                "wrong_key": jwt.encode(base, "a-different-secret", algorithm="HS256"),
                "expired": jwt.encode(
                    {**base, "iat": now - 7200, "exp": now - 3600},
                    secret,
                    algorithm="HS256",
                ),
                "wrong_audience": jwt.encode(
                    {**base, "aud": "some-other-audience"}, secret, algorithm="HS256"
                ),
                "alg_none": jwt.encode(base, key="", algorithm="none"),
                "missing_sub": jwt.encode(
                    {k: v for k, v in base.items() if k != "sub"},
                    secret,
                    algorithm="HS256",
                ),
            }
            rejected = []
            for name, token in cases.items():
                try:
                    backend.verify_token(token)
                except AuthenticationError as exc:
                    rejected.append(f"{name}={exc.code}")
                else:
                    raise AssertionError(f"{name} was accepted")
            return ", ".join(rejected)
        finally:
            settings.supabase_jwt_secret = original

    @check("Supabase ES256 (JWT Signing Keys) tokens verify via JWKS")
    def _supabase_es256():
        """Newer Supabase projects sign with ES256, not the HS256 secret.

        Uses a throwaway P-256 key and a stubbed JWKS fetch, so the test is
        fully offline. Covers: valid token accepted, wrong issuer rejected,
        unknown kid rejected, tampered signature rejected.
        """
        import base64
        import time

        import jwt
        from cryptography.hazmat.primitives.asymmetric import ec
        from jwt import PyJWKClient

        from app.auth.supabase import SupabaseAuthBackend
        from app.core.config import settings
        from app.core.errors import AuthenticationError

        def _b64u(num: int) -> str:
            raw = num.to_bytes(32, "big")
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

        private_key = ec.generate_private_key(ec.SECP256R1())
        numbers = private_key.public_key().public_numbers()
        jwks = {
            "keys": [
                {
                    "kty": "EC",
                    "crv": "P-256",
                    "x": _b64u(numbers.x),
                    "y": _b64u(numbers.y),
                    "kid": "verify-test-kid-1",
                    "alg": "ES256",
                    "use": "sig",
                }
            ]
        }

        project = "https://verify-test.supabase.co"
        original_url = settings.supabase_url
        original_fetch = PyJWKClient.fetch_data
        PyJWKClient.fetch_data = lambda self: jwks
        settings.supabase_url = project
        try:
            backend = SupabaseAuthBackend()
            now = int(time.time())
            base = {
                "sub": str(uuid.uuid4()),
                "email": "es256@aura-verify.example",
                "aud": settings.supabase_jwt_audience,
                "iss": f"{project}/auth/v1",
                "iat": now,
                "exp": now + 3600,
            }

            def _mint(payload, kid="verify-test-kid-1", key=private_key):
                return jwt.encode(payload, key, algorithm="ES256", headers={"kid": kid})

            claims = backend.verify_token(_mint(base))
            assert claims.subject == base["sub"], "ES256 subject mismatch"
            assert claims.email == "es256@aura-verify.example"

            bad = {
                "wrong_issuer": _mint({**base, "iss": "https://evil.example/auth/v1"}),
                "unknown_kid": _mint(base, kid="not-in-jwks"),
                "tampered": _mint(base)[:-6] + "AAAAAA",
            }
            rejected = []
            for name, token in bad.items():
                try:
                    backend.verify_token(token)
                except AuthenticationError as exc:
                    rejected.append(f"{name}={exc.code}")
                else:
                    raise AssertionError(f"ES256 {name} was accepted")
            return f"ES256 accepted; {', '.join(rejected)}"
        finally:
            PyJWKClient.fetch_data = original_fetch
            settings.supabase_url = original_url

    @check("Supabase reports its real capabilities and refuses what it cannot do")
    def _supabase_caps():
        from app.auth.supabase import SupabaseAuthBackend
        from app.core.database import SessionLocal
        from app.core.errors import AuraError

        backend = SupabaseAuthBackend()
        caps = backend.capabilities
        assert not any(
            (
                caps.can_register,
                caps.can_login_with_password,
                caps.can_refresh,
                caps.can_reset_password,
                caps.can_change_password,
                caps.can_revoke_sessions,
            )
        ), "supabase must not advertise operations it does not implement"
        assert "supabase-js" in caps.notes

        with SessionLocal() as db:
            for label, call in (
                (
                    "register",
                    lambda: backend.register(
                        db, email="nope@aura-verify.example", password="x"
                    ),
                ),
                (
                    "login",
                    lambda: backend.login(
                        db, email="nope@aura-verify.example", password="x"
                    ),
                ),
                ("refresh", lambda: backend.refresh(db, refresh_token="x")),
            ):
                try:
                    call()
                except AuraError as exc:
                    assert exc.code == "auth_operation_unsupported", (label, exc.code)
                    assert exc.hint, f"{label} error gives no guidance"
                else:
                    raise AssertionError(f"{label} did not refuse")

            # Sessions are genuinely invisible here, and that is reported, not faked.
            from app.models.user import User

            any_user = db.query(User).first()
            assert backend.active_sessions(db, user=any_user) == []
        return "6 capabilities false, 3 operations refused with guidance"

    # -------------------------------------------------------------- logging
    print("\n== 9. no credential reaches the logs ==")

    @check("auth traffic logs no password, token, or hash")
    def _no_leaks():
        import io
        import logging as stdlib_logging

        from app.core.logging import configure_logging

        configure_logging()
        buffer = io.StringIO()
        handler = stdlib_logging.StreamHandler(buffer)
        root = stdlib_logging.getLogger()
        existing = root.handlers[0]
        handler.setFormatter(existing.formatter)
        root.handlers = [handler]
        try:
            r = client.post(
                f"{api}/auth/login",
                json={"email": EMAIL, "password": "third-distinct-passphrase"},
            )
            assert r.status_code == 200, r.text
            tokens = r.json()["tokens"]
            client.get(f"{api}/auth/me", headers=_bearer(tokens["access_token"]))
            client.post(
                f"{api}/auth/login", json={"email": EMAIL, "password": "wrong-password"}
            )
        finally:
            root.handlers = [existing]

        output = buffer.getvalue()
        assert output.strip(), "captured no log output; the check would be vacuous"
        for secret in (
            "third-distinct-passphrase",
            "wrong-password",
            tokens["access_token"],
            tokens["refresh_token"],
        ):
            assert secret not in output, "a credential appeared in the logs"
        return f"{len(output.splitlines())} log lines, no credential present"

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
