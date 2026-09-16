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
import shutil
import subprocess
import sys
import time
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scraper"))

import scrape  # noqa: E402

DEFAULT_SEASON = "918978398"
ROW_SELECTOR = "td.home-team"
# How long to let Cloudflare's interstitial clear itself before giving up.
SETTLE_SECONDS = 60


def start_virtual_display() -> subprocess.Popen | None:
    """Start Xvfb and point DISPLAY at it, so headed modes can run headless-ly.

    xvfb-run would do this, but it needs xauth, which the slim image does not
    carry. Driving Xvfb directly needs neither.
    """
    if os.environ.get("DISPLAY"):
        return None
    if not shutil.which("Xvfb"):
        print("No DISPLAY and no Xvfb — headed modes will be skipped.\n")
        return None

    proc = subprocess.Popen(
        ["Xvfb", ":99", "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    socket = pathlib.Path("/tmp/.X11-unix/X99")
    for _ in range(50):                       # up to 5s for the socket to appear
        if socket.exists():
            os.environ["DISPLAY"] = ":99"
            print("Started Xvfb on :99 for the headed modes.\n")
            return proc
        if proc.poll() is not None:
            print("Xvfb exited immediately — headed modes will be skipped.\n")
            return None
        time.sleep(0.1)

    proc.terminate()
    print("Xvfb never opened its socket — headed modes will be skipped.\n")
    return None


# Cloudflare shows two different pages, and they mean opposite things.
# "Attention Required!" is a refusal. "Just a moment..." is a challenge being
# offered — a browser is expected to solve it and be given a cf_clearance
# cookie. Reaching the second from the first is progress, not failure.
BLOCK_MARKERS = ("attention required",)
CHALLENGE_MARKERS = ("just a moment", "checking your browser", "cf-challenge")


def challenge_state(html: str) -> str | None:
    head = html[:4000].lower()
    if any(m in head for m in BLOCK_MARKERS):
        return "BLOCKED (refused outright)"
    if any(m in head for m in CHALLENGE_MARKERS):
        return "CHALLENGE (offered, unsolved)"
    return None


def verdict(html: str, cookies: list[dict] | None = None) -> str:
    if not html:
        return "nothing returned"

    cleared = any(c["name"] == "cf_clearance" for c in cookies or [])
    suffix = ", cf_clearance held" if cleared else ""

    state = challenge_state(html)
    if state:
        return f"{state} ({len(html):,} bytes){suffix}"

    rows = len(scrape.parse_results(html))
    if rows:
        return f"OK — {rows} result(s) parsed ({len(html):,} bytes){suffix}"
    return f"no challenge, but 0 rows parsed ({len(html):,} bytes){suffix}"


def settle(page, seconds: int = SETTLE_SECONDS) -> str:
    """Wait for a challenge to clear, not just for the rows to appear.

    Cloudflare's interstitial reloads itself once solved, which can take much
    longer than a selector wait allows for; polling until the challenge markers
    are gone distinguishes "never solved it" from "was not given time".
    """
    deadline = time.time() + seconds
    html = page.content()
    while time.time() < deadline:
        if not challenge_state(html) or page.query_selector(ROW_SELECTOR):
            return page.content()
        time.sleep(3)
        html = page.content()
    return html


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


def via_cdp(url: str, endpoint: str):
    """Drive a browser that is ALREADY running, rather than launching one.

    Every launched mode is refused, and launching is the thing they have in
    common: Playwright starts a browser with --enable-automation and sets
    navigator.webdriver. Attaching to a browser you started yourself does
    neither — it is the same session you read the page in by hand.

    Start one first:

        google-chrome --remote-debugging-port=9222 \
            --user-data-dir="$HOME/.fulltime-chrome"

    Visit the results page in it once, so anything Cloudflare wants to set is
    set, then run this.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            html = settle(page)
            cookies = context.cookies()
        finally:
            # Close only the tab we opened; the browser is the operator's.
            page.close()

    return verdict(html, cookies), cookies


def via_browser(url: str, *, headless, channel=None, profile=None, args=None):
    """Return (verdict, cookies). Cookies come back so they can be replayed."""
    from playwright.sync_api import sync_playwright

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
            html = settle(page)
            cookies = context.cookies()
        finally:
            context.close()
            if browser:
                browser.close()

    return verdict(html, cookies), cookies


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", default=DEFAULT_SEASON)
    ap.add_argument("--profile", default="/tmp/fulltime-profile",
                    help="persistent browser profile directory")
    ap.add_argument("--cdp", default="http://localhost:9222",
                    help="a browser you started yourself, with --remote-debugging-port")
    args = ap.parse_args()

    url = f"{scrape.RESULTS_URL}?selectedSeason={args.season}&selectedFixtureGroupKey="
    print(f"target: {url}")
    xvfb = start_virtual_display()
    display = os.environ.get("DISPLAY")
    print(f"DISPLAY={display or '(unset — headed modes skipped)'}\n")

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

    run(f"attach to a browser already running at {args.cdp}",
        lambda: via_cdp(url, args.cdp))

    if good_cookies:
        names = sorted({c["name"] for c in good_cookies})
        print(f"--- replaying {len(good_cookies)} cookie(s) through curl_cffi: {names}")
        print(f"    {via_curl(url, good_cookies)}\n")

    print("=== summary ===")
    for label, out in results:
        state = out.split(" (")[0].split(" —")[0]
        print(f"  {state:28} {label}")
    print("\nBuild on whichever mode reports OK.")
    if xvfb:
        xvfb.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
