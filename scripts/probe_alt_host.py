#!/usr/bin/env python3
"""Probe full-time.thefa.com — the other Full-Time host — and its API calls.

Route discovery turned up two links to `full-time.thefa.com` (hyphenated) on the
fixtures page, alongside the `fulltime.thefa.com` (unhyphenated) pages we have
been refused by. A second host is worth a look on its own terms: it may be a
newer app, and a newer app usually has a JSON API behind it, which would beat
parsing 2.6MB of HTML regardless of how the WAF is configured.

This loads the host in a browser and records every XHR/fetch it makes — that is
how its API names itself — then retries the JSON-looking ones with a plain
fetch, to see which are reachable without a browser at all.

    docker compose run --rm -v "$PWD/scripts:/app/scripts" \\
        scraper python scripts/probe_alt_host.py
"""

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scraper"))

import scrape  # noqa: E402

ALT_HOST = "https://full-time.thefa.com/"


def describe(status, text, content_type=""):
    if "attention required" in text[:4000].lower():
        return f"HTTP {status}, Cloudflare challenge"
    note = f"HTTP {status}, {len(text):,} bytes, {content_type or '?'}"
    if "json" in content_type.lower():
        try:
            data = json.loads(text)
        except ValueError:
            return note + ", unparseable JSON"
        if isinstance(data, dict):
            note += f", keys={sorted(data)[:8]}"
        elif isinstance(data, list):
            note += f", {len(data)} item(s)"
    elif text.count("home-team"):
        note += f", {text.count('home-team')}x home-team"
    return note


def plain_get(url: str) -> str:
    from curl_cffi import requests as curl_requests

    with curl_requests.Session(impersonate="chrome") as session:
        session.headers.update(scrape.BROWSER_HEADERS)
        for name, value in scrape._session_cookies().items():
            session.cookies.set(name, value, domain=".thefa.com")
        try:
            resp = session.get(url, timeout=60)
        except Exception as e:
            return f"failed: {e!r}"
    return describe(resp.status_code, resp.text, resp.headers.get("content-type", ""))


def record_api_calls(url: str, out_dir: pathlib.Path) -> list[str]:
    """Load the page in a browser and report every XHR/fetch it makes."""
    from playwright.sync_api import sync_playwright

    seen: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_context().new_page()

            def note(response):
                if response.request.resource_type in ("xhr", "fetch"):
                    seen.append(f"{response.status} {response.request.method} {response.url}")

            page.on("response", note)
            page.goto(url, wait_until="networkidle", timeout=90_000)
            html = page.content()
            (out_dir / "alt-host.html").write_text(html, encoding="utf-8")
            print(f"  landed on {page.url}")
            print(f"  page: {describe('—', html)}")
            print(f"  saved {out_dir / 'alt-host.html'}")
        finally:
            browser.close()
    return seen


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=ALT_HOST)
    ap.add_argument("--out", default="/tmp/fulltime-diagnosis")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== plain fetch of {args.url} ===")
    print(f"  {plain_get(args.url)}")

    print(f"\n=== loading {args.url} in a browser, recording its API calls ===")
    try:
        calls = record_api_calls(args.url, out_dir)
    except Exception as e:
        print(f"  browser load failed: {e!r}")
        calls = []

    if not calls:
        print("  no XHR/fetch calls recorded")
    for call in calls:
        print(f"  {call}")

    api_urls = [
        c.split(" ", 2)[2] for c in calls
        if any(hint in c.lower() for hint in ("api", "json", "graphql", "/v1", "/v2"))
    ]
    if api_urls:
        print(f"\n=== retrying {len(api_urls)} API call(s) without a browser ===")
        for url in dict.fromkeys(api_urls):
            print(f"  {url}")
            print(f"    {plain_get(url)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
