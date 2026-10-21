"""Drive the LIVE running server through a full user journey and print it.

Not a test -- a narrated walkthrough against http://127.0.0.1:8000 so a human can
see the real API do real work: register -> business -> website -> a real crawl of
example.com (IANA's reserved documentation domain) -> read the analysed pages.

Run the server first (uvicorn app.main:app), then: python scripts/demo_live.py
"""

from __future__ import annotations

import sys
import time

import httpx

BASE = "http://127.0.0.1:8000"
API = f"{BASE}/api/v1"
# Unique each run so re-running never collides on the email unique constraint.
EMAIL = f"demo-{int(time.time())}@bakery.example"
PASSWORD = "correct-horse-battery-staple"


def line(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def show(label: str, resp: httpx.Response) -> dict:
    body = resp.json() if resp.content else {}
    print(f"  {label}: HTTP {resp.status_code}")
    return body


def main() -> int:
    c = httpx.Client(base_url=API, timeout=30.0)

    line("1. Register a new account (Argon2id password hashing)")
    r = c.post("/auth/register", json={"email": EMAIL, "password": PASSWORD})
    body = show(f"POST /auth/register  ({EMAIL})", r)
    token = body["tokens"]["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    print(f"     access token issued, {len(token)} chars; refresh token rotates on use")

    line("2. Create a business")
    r = c.post(
        "/businesses",
        json={"name": "Rise & Crumb Bakery", "industry": "Food & Beverage"},
        headers=auth,
    )
    biz = show("POST /businesses", r)
    bid = biz["id"]
    print(f"     business_id={bid}  onboarding_step={biz.get('onboarding_step')}")

    line("3. Register the business website (SSRF-guarded at write time)")
    r = c.post(
        f"/businesses/{bid}/websites",
        json={"url": "http://example.com"},
        headers=auth,
    )
    site = show("POST /businesses/{id}/websites", r)
    wid = site["id"]
    print(f"     website_id={wid}")
    print(f"     url={site['url']}  normalized_url={site['normalized_url']}")
    print(f"     is_primary={site['is_primary']}")

    line("4. Start a REAL scan -- the crawler fetches example.com over HTTP")
    r = c.post(f"/businesses/{bid}/websites/{wid}/scans", headers=auth)
    scan = show("POST .../scans", r)
    sid = scan["id"]
    print(
        f"     scan_id={sid}  status={scan['status']}  (202 Accepted, runs in background)"
    )

    line("5. Poll the scan to completion (crawler owns progress 0-70%)")
    final = scan
    for _ in range(40):
        time.sleep(0.5)
        r = c.get(f"/businesses/{bid}/websites/{wid}/scans/{sid}", headers=auth)
        final = r.json()
        print(
            f"  status={final['status']:<10} "
            f"progress={final['progress_percent']:>3}%  "
            f"{final.get('progress_message') or ''}"
        )
        if final["is_finished"]:
            break

    line("6. Terminal scan result (every number traces to the crawled rows)")
    print(f"  status            : {final['status']}")
    print(f"  pages_discovered  : {final['pages_discovered']}")
    print(f"  pages_crawled     : {final['pages_crawled']}")
    print(f"  pages_failed      : {final['pages_failed']}")
    print(f"  duration_ms       : {final.get('duration_ms')}")
    print(f"  robots_disallowed : {final['robots_disallowed']}")
    if final.get("error_code"):
        print(f"  error_code        : {final['error_code']}")
        print(f"  error_message     : {final['error_message']}")
    sa = final.get("site_analysis") or {}
    if sa:
        print(f"  site_analysis     : {sa}")

    if final["pages_crawled"]:
        line("7. Read the analysed pages back (the evidence behind future scores)")
        r = c.get(f"/businesses/{bid}/websites/{wid}/scans/{sid}/pages", headers=auth)
        pages = r.json()
        for p in pages:
            print(
                f"  - {p['url']}\n"
                f"      title={p['title']!r}  words={p['word_count']}  "
                f"h1={p['h1_count']}  images={p['image_count']} "
                f"(missing alt: {p['images_missing_alt']})  schema={p['has_schema_markup']}"
            )
    else:
        line("7. No pages analysed")
        print("  The crawl reached no analysable page. The scan recorded WHY, honestly")
        print("  (see error_code above) -- it did not invent a result. If this machine")
        print(
            "  has no outbound network access, that is the expected, truthful outcome."
        )

    line("DONE -- this was the live server, real Argon2, real crawler, real DB rows")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except httpx.ConnectError:
        print("Could not reach the server. Start it first: uvicorn app.main:app")
        sys.exit(1)
