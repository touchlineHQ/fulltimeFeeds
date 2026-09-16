#!/usr/bin/env python3
"""Find a browser mode that can read the results page from this machine.

A real browser on this machine reads the results page fine; curl_cffi and
headless Chromium both get a Cloudflare challenge. The difference is the JS
challenge a real browser solves, and the clearance cookie it is then given. So
the question is which automated mode Cloudflare treats as a real browser here.

Each mode below is tried against the results page and reported. Whichever
answers is the one to build on — and if a browser mode works, its cookies are
replayed through curl_cffi too, since a cheap fetch with a warmed cookie jar
would beat launching a browser per league.

Run natively on your machine (a window may open — that is the point):

    pip install playwright curl_cffi beautifulsoup4 lxml
    playwright install chromium
    python scripts/probe_browser_modes.py

Or inside the container, where headed modes need a virtual display:

    docker compose run --rm -v "$PWD/scripts:/app/scripts" \\
        scraper sh -c "xvfb-run -a python scripts/probe_browser_modes.py"
"""

import argparse
import os
import pathlib
import sys
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scraper"))

import scrape  # noqa: E402

DEFAULT_SEASON = "918978398"
ROW_SELECTOR = "td.home-team"
# Cloudflare's interstitial can take several seconds to clear itself.
SETTLE_MS = 25_000


def verdict(html: str) -> str:
    if not html:
        return "nothing returned"
    if "attention required" in html[:4000].lower():
        return f"CHALLENGE ({len(html):,} bytes)"
    rows = len(scrape.parse_results(html))
    if rows:
        return f"OK — {rows} result(s) parsed ({len(html):,} bytes)"
    return f"no challenge, but 0 rows parsed ({len(html):,} bytes)"


def via_curl(url: str, cookies: list[dict] | None = None) -> str:
    from curl_cffi import requests as curl_requests

    with curl_requests.Session(impersonate="chrome") as session:
        session.headers.update(scrape.BROWSER_HEADERS)
        if cookies:
            for c in cookies:
                session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
        try:
            resp = session.get(url, timeout=60)
        except Exception as e:
            return f"failed: {e!r}"
        return verdict(resp.text)


def via_browser(url: str, *, headless, channel=None, profile=None, args=None):
    """Return (verdict, cookies). Cookies come back so they can be replayed."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    with sync_playwright() as pw:
        launch: dict = {"headless": headless}
        if channel:
            launch["channel"] = channel
        if args:
            launch["args"] = args

        if profile:
            context = pw.chromium.launch_persistent_context(str(profile), **launch)
            browser = None
        else:
            browser = pw.chromium.launch(**launch)
            context = browser.new_context()

        try:
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            try:
                page.wait_for_selector(ROW_SELECTOR, timeout=SETTLE_MS)
            except PWTimeout:
                pass
            html = page.content()
            cookies = context.cookies()
        finally:
            context.close()
            if browser:
                browser.close()

    return verdict(html), cookies


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", default=DEFAULT_SEASON)
    ap.add_argument("--profile", default="/tmp/fulltime-profile",
                    help="persistent browser profile directory")
    args = ap.parse_args()

    url = f"{scrape.RESULTS_URL}?selectedSeason={args.season}&selectedFixtureGroupKey="
    print(f"target: {url}")
    display = os.environ.get("DISPLAY")
    print(f"DISPLAY={display or '(unset — headed modes will fail; use xvfb-run)'}\n")

    results: list[tuple[str, str]] = []
    good_cookies: list[dict] | None = None

    def run(label, fn):
        nonlocal good_cookies
        print(f"--- {label}")
        try:
            out = fn()
        except Exception as e:
            out = f"failed: {e.__class__.__name__}: {e}"
            if os.environ.get("DEBUG"):
                traceback.print_exc()
        if isinstance(out, tuple):
            out, cookies = out
            if out.startswith("OK") and good_cookies is None:
                good_cookies = cookies
        print(f"    {out}\n")
        results.append((label, out))

    run("curl_cffi (what the scraper does today)", lambda: via_curl(url))
    run("chromium headless", lambda: via_browser(url, headless=True))
    run("chromium --headless=new",
        lambda: via_browser(url, headless=False, args=["--headless=new"]))

    if display:
        run("chromium headed", lambda: via_browser(url, headless=False))
        run("chromium headed + persistent profile (1st visit)",
            lambda: via_browser(url, headless=False, profile=args.profile))
        run("chromium headed + persistent profile (2nd visit)",
            lambda: via_browser(url, headless=False, profile=args.profile))
        run("real Chrome headed + persistent profile",
            lambda: via_browser(url, headless=False, channel="chrome", profile=args.profile))
    else:
        print("--- headed modes skipped: no DISPLAY\n")

    if good_cookies:
        names = sorted({c["name"] for c in good_cookies})
        print(f"--- replaying {len(good_cookies)} cookie(s) through curl_cffi: {names}")
        print(f"    {via_curl(url, good_cookies)}\n")

    print("=== summary ===")
    for label, out in results:
        print(f"  {out.split(' ')[0]:12} {label}")
    print("\nBuild on whichever mode reports OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
