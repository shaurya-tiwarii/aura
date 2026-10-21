"""Run every AURA executable verification script plus the pytest regression suite.

This is the ONE documented validation command (see the project README):

    cd backend
    python scripts/run_all_checks.py

It runs, in order:

1. every ``scripts/verify_*.py`` executable proof (12 scripts; ``demo_live.py``
   is intentionally excluded -- it drives a *live* server, not a scratch DB),
2. the pytest regression suite (``pytest -q``).

Each script is run as a subprocess with this interpreter, from the ``backend/``
directory, exactly as the README documents the individual commands. Some
scripts return ``main()``'s exit code via ``SystemExit``; others simply exit 0
on success and raise on failure -- both conventions are handled by reading the
process exit code.

Summary and exit code
---------------------
The runner prints a per-script result line, a total of the reported
``N/M checks passed`` figures (where a script prints one), and a pytest line.
It exits 0 only if **everything** passed; any failing script or pytest run
produces a non-zero exit and the tail of the failing output is echoed so the
cause is visible immediately.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Ordered the way the README documents them; verify_foundation takes --scratch
# so it always runs against a throwaway SQLite file instead of the dev DB.
SCRIPTS: list[tuple[str, list[str]]] = [
    ("verify_foundation.py", ["--scratch"]),
    ("verify_migrations.py", []),
    ("verify_auth.py", []),
    ("verify_business.py", []),
    ("verify_crawler.py", []),
    ("verify_seo.py", []),
    ("verify_ai_visibility.py", []),
    ("verify_recommendation.py", []),
    ("verify_analytics.py", []),
    ("verify_assistant.py", []),
    ("verify_improvement.py", []),
    ("verify_system.py", []),
]

_CHECK_LINE = re.compile(r"(?m)^(\d+)/(\d+) checks passed\s*$")
_TIMEOUT_SECONDS = 30 * 60  # a hung script should fail, not hang the gate


def _run_script(name: str, args: list[str], *, dry_run: bool) -> dict:
    """Run one verification script. Returns a result dict."""
    script = BACKEND_DIR / "scripts" / name
    if not script.is_file():
        return {"name": name, "status": "MISSING", "passed": None, "total": None}

    if dry_run:
        return {"name": name, "status": "would-run", "passed": None, "total": None}

    cmd = [sys.executable, str(script), *args]
    print(f"\n{'=' * 70}\n>>> {name} {' '.join(args)}\n{'=' * 70}")
    try:
        proc = subprocess.run(
            cmd,
            cwd=BACKEND_DIR,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(f"!!! {name} timed out after {_TIMEOUT_SECONDS}s")
        return {"name": name, "status": "TIMEOUT", "passed": None, "total": None}

    # Stream the script's own output so a failure is inspectable; the terminal
    # summary is parsed afterwards.
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0 and proc.stderr:
        sys.stdout.write(proc.stderr)

    passed: int | None = None
    total: int | None = None
    match = _CHECK_LINE.search(proc.stdout)
    if match:
        passed, total = int(match.group(1)), int(match.group(2))

    status = "PASS" if proc.returncode == 0 else "FAIL"
    if status == "FAIL" and proc.stderr and not proc.stdout:
        sys.stdout.write(proc.stderr)
    return {"name": name, "status": status, "passed": passed, "total": total}


def _run_pytest(*, dry_run: bool) -> dict:
    if dry_run:
        return {"name": "pytest -q", "status": "would-run", "passed": None, "total": None}
    print(f"\n{'=' * 70}\n>>> pytest -q\n{'=' * 70}")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=BACKEND_DIR,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print("!!! pytest timed out")
        return {"name": "pytest -q", "status": "TIMEOUT", "passed": None, "total": None}

    sys.stdout.write(proc.stdout)
    sys.stdout.write(proc.stderr)
    # pytest -q prints a final line like "11 passed in 3.21s" or "2 failed, 9 passed".
    match = re.search(r"(?m)^(?:(\d+) failed,\s*)?(\d+) passed", proc.stdout)
    status = "PASS" if proc.returncode == 0 else "FAIL"
    result = {"name": "pytest -q", "status": status, "passed": None, "total": None}
    if match:
        passed = int(match.group(2))
        failed = int(match.group(1)) if match.group(1) else 0
        result["passed"] = passed
        result["total"] = passed + failed
    return result


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    results = [
        _run_script(name, args, dry_run=dry_run) for name, args in SCRIPTS
    ]
    results.append(_run_pytest(dry_run=dry_run))

    failed = [r for r in results if r["status"] not in ("PASS", "would-run")]
    check_passed = sum(r["passed"] for r in results if r["passed"] is not None)
    check_total = sum(r["total"] for r in results if r["total"] is not None)

    print(f"\n{'=' * 70}\nVERIFICATION SUMMARY\n{'=' * 70}")
    for r in results:
        counts = (
            f" ({r['passed']}/{r['total']} checks)"
            if r["passed"] is not None
            else ""
        )
        print(f"  [{r['status']:>8}] {r['name']}{counts}")
    if check_total:
        print(f"\nReported check total: {check_passed}/{check_total} checks passed")
    if dry_run:
        print("(dry run: nothing was executed)")
        return 0
    if failed:
        print("\nFAILED:")
        for r in failed:
            print(f"  - {r['name']} ({r['status']})")
        return 1
    print("\nALL CHECKS GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
