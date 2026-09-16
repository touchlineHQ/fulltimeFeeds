#!/usr/bin/env python3
"""Save the results pages already open in the VNC browser.

A person opens the tabs and clears anything Full-Time asks of them; this writes
what is on screen to disk, so the scraper can parse it. It is Ctrl+S on each
tab without doing it seven times by hand.

It never navigates, reloads or opens anything. Every page it saves was fetched
by the person driving the browser, and reading a rendered page makes no request
at all — which is why this works where driving the browser does not.

Run it in the terminal inside the VNC session, or let vnc_browser.sh start it:

    python3 /app/scripts/save_open_tabs.py --watch
"""

import argparse
import logging
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scraper"))

import scrape  # noqa: E402
from browser import is_challenge_page  # noqa: E402

log = logging.getLogger("save_open_tabs")

SEASON_RE = re.compile(r"selectedSeason=(\d+)")
ROW_MARKER = "home-team"
# How often to say what is going on when nothing is ready. Silence and "still
# starting up" look identical otherwise.
STATUS_EVERY = 30.0
_last_status = 0.0
_passes = 0

# Playwright waits 30s by default before giving up on a page. Across seven tabs
# that is minutes of silence before anything is printed, which reads as a hang.
# A tab that cannot be read in a few seconds is not ready anyway.
TAB_TIMEOUT_MS = 5_000

# One connection, reused: starting a driver and attaching for every poll costs
# seconds a time and is the slowest part of the loop by far.
_connection = None


def _connected(endpoint: str):
    """The browser at *endpoint*, reconnecting if the connection has dropped."""
    global _connection
    if _connection is not None:
        _, browser = _connection
        try:
            if browser.is_connected():
                return browser
        except Exception:
            pass
        _disconnect()

    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(endpoint, timeout=15_000)
    except Exception:
        pw.stop()
        raise
    _connection = (pw, browser)
    return browser


def _disconnect() -> None:
    global _connection
    if _connection is None:
        return
    pw, browser = _connection
    _connection = None
    for shut in (browser.close, pw.stop):
        try:
            shut()
        except Exception:
            pass


def league_names() -> dict[str, str]:
    return {season_id: name for season_id, name in scrape.LEAGUES}


def open_results_tabs(endpoint: str) -> list[tuple[str, str, str]]:
    """Return (season_id, url, html) for each results tab currently open.

    Reads only. Playwright is used to talk to the browser already running, not
    to start or steer one.
    """
    found: list[tuple[str, str, str]] = []
    browser = _connected(endpoint)
    total = 0

    for context in browser.contexts:
        for page in context.pages:
            total += 1
            url = page.url
            if "/results/" not in url:
                continue
            season = SEASON_RE.search(url)
            if not season:
                continue
            try:
                page.set_default_timeout(TAB_TIMEOUT_MS)
                html = page.content()
            except Exception as e:              # still loading, or mid-navigation
                log.debug(f"  could not read {url}: {e}")
                continue
            found.append((season.group(1), url, html))

    if not found:
        _report_idle(total)
    return found


def _report_idle(open_tabs: int) -> None:
    """Say why nothing is being saved.

    Every pass to begin with, so there is feedback immediately, then
    occasionally — at a five second poll, saying it every time would be twelve
    lines a minute of nothing new.
    """
    global _last_status, _passes
    _passes += 1
    now = time.time()
    if _passes > 3 and now - _last_status < STATUS_EVERY:
        return
    _last_status = now
    if open_tabs:
        log.info(f"  waiting — {open_tabs} tab(s) open, none of them a loaded "
                 f"results page yet")
    else:
        log.info("  waiting — no tabs open in the browser yet")


def save_ready_tabs(endpoint: str, out_dir: pathlib.Path, saved: set[str]) -> int:
    """Write every results tab that has finished loading. Returns how many."""
    names = league_names()
    written = 0

    for season_id, url, html in open_results_tabs(endpoint):
        if season_id in saved:
            continue
        label = names.get(season_id, f"season {season_id}")

        if is_challenge_page(html):
            log.info(f"  {label}: still showing a challenge — solve it in the browser")
            continue
        if ROW_MARKER not in html:
            log.info(f"  {label}: no results on the page yet")
            continue

        destination = out_dir / f"{season_id}.html"
        destination.write_text(html, encoding="utf-8")
        saved.add(season_id)
        written += 1
        rows = len(scrape.parse_results(html))
        log.info(f"  {label}: saved {destination.name} ({rows} result(s))")

    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--endpoint", default="http://localhost:9222",
                    help="the VNC browser's debugging endpoint")
    ap.add_argument("--out", default=None, help="where to write (RESULTS_HTML_DIR)")
    ap.add_argument("--watch", action="store_true",
                    help="keep watching, saving each tab as it finishes loading")
    ap.add_argument("--interval", type=float, default=5.0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    import os

    out_dir = pathlib.Path(
        args.out or os.environ.get(scrape.RESULTS_HTML_DIR_ENV)
        or scrape.DEFAULT_RESULTS_HTML_DIR
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info(f"Saving results tabs into {out_dir}\n")

    saved: set[str] = set()
    total_leagues = len(scrape.LEAGUES)

    try:
        while True:
            try:
                save_ready_tabs(args.endpoint, out_dir, saved)
            except Exception as e:
                log.warning(f"Could not read the browser at {args.endpoint}: {e}")
                if not args.watch:
                    return 1

            if not args.watch:
                break
            if len(saved) >= total_leagues:
                log.info(f"\nAll {total_leagues} leagues saved.")
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("\nStopped.")
    finally:
        _disconnect()

    log.info(f"\n{len(saved)} of {total_leagues} league(s) saved to {out_dir}")
    if saved:
        log.info("The next scrape will parse them.")
    return 0 if saved else 1


if __name__ == "__main__":
    sys.exit(main())
