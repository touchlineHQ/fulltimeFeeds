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
import json
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
# Chrome does not load a tab opened in the background until it is visited, so an
# unvisited tab answers with an empty document: <html><head></head><body></body>
# </html> and nothing else. That is a different problem from a page that loaded
# and held nothing, and needs different advice, so match the shell itself rather
# than guess from the length.
_SHELL_RE = re.compile(r"</?(?:html|head|body)[^>]*>|\s+", re.IGNORECASE)


def is_unloaded(html: str) -> bool:
    """True for the empty document a tab that was never visited returns."""
    return not _SHELL_RE.sub("", html)
# How often to say what is going on when nothing is ready. Silence and "still
# starting up" look identical otherwise.
STATUS_EVERY = 30.0
_last_status = 0.0
_passes = 0

# Playwright waits 30s by default before giving up on a page. Across seven tabs
# that is minutes of silence before anything is printed, which reads as a hang.
# A tab that cannot be read in a few seconds is not ready anyway.
TAB_TIMEOUT_MS = 5_000

# Playwright's connect_over_cdp adopts the whole browser: it attaches to every
# target and waits for each to answer. A tab sitting on a challenge does not
# answer, so one unresponsive tab stalls the connection to all of them — which
# is what "ws connected, then Timeout 15000ms exceeded" was.
#
# Reading one page needs none of that. The debug port lists its tabs over plain
# HTTP, and each tab has its own websocket, so a tab that will not answer costs
# only its own timeout.
TAB_TIMEOUT = 5.0


def _safe_targets(endpoint: str) -> list[dict]:
    """Targets, or an empty list — used where a failure must not stop a pass."""
    try:
        return _targets(endpoint)
    except Exception:
        return []


def _targets(endpoint: str) -> list[dict]:
    """Every tab the browser has open, from the debug port's HTTP listing."""
    import urllib.request

    with urllib.request.urlopen(f"{endpoint}/json/list", timeout=15) as response:
        return [t for t in json.loads(response.read()) if t.get("type") == "page"]


def _call(connection, call_id: int, method: str, params: dict, timeout: float) -> dict:
    """Send one CDP call and return its reply, stepping over events."""
    connection.send(json.dumps({"id": call_id, "method": method, "params": params}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        message = json.loads(connection.recv())
        if message.get("id") == call_id:
            return message
    raise TimeoutError(f"no reply to {method}")


def _page_html(ws_url: str, timeout: float = TAB_TIMEOUT) -> str:
    """Ask one tab for its rendered HTML, over its own websocket.

    Nothing is navigated or reloaded: this reads the document already on
    screen, which is what the person driving the browser fetched.

    Two ways of asking, because they fail differently. Runtime.evaluate runs in
    the page's own world and can come back empty while a page is mid
    navigation; DOM.getOuterHTML goes through the inspector's own view of the
    document and answers when the first does not.
    """
    import websocket  # websocket-client

    # Chrome rejects a debug websocket carrying a browser Origin header.
    connection = websocket.create_connection(
        ws_url, timeout=timeout, suppress_origin=True
    )
    try:
        reply = _call(connection, 1, "Runtime.evaluate", {
            "expression": "document.documentElement.outerHTML",
            "returnByValue": True,
        }, timeout)
        result = reply.get("result", {})
        html = result.get("result", {}).get("value") or ""
        if result.get("exceptionDetails"):
            log.debug(f"  evaluate raised: {result['exceptionDetails'].get('text')}")

        if not html or is_unloaded(html):
            document = _call(connection, 2, "DOM.getDocument",
                             {"depth": 0}, timeout)
            node_id = document.get("result", {}).get("root", {}).get("nodeId")
            if node_id:
                outer = _call(connection, 3, "DOM.getOuterHTML",
                              {"nodeId": node_id}, timeout)
                via_dom = outer.get("result", {}).get("outerHTML") or ""
                if via_dom and not is_unloaded(via_dom):
                    log.debug("  read via DOM.getOuterHTML")
                    return via_dom
        return html
    finally:
        try:
            connection.close()
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
    targets = _targets(endpoint)

    for target in targets:
        url = target.get("url", "")
        if "/results/" not in url:
            continue
        season = SEASON_RE.search(url)
        if not season:
            continue
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            continue
        try:
            html = _page_html(ws_url)
        except ImportError as e:
            # A missing dependency is not a busy tab. Swallowed as a per-tab
            # debug line it looks exactly like "nothing to save", forever.
            raise RuntimeError(
                f"{e} — rebuild the image (`docker compose build`) so "
                f"websocket-client is installed"
            ) from e
        except Exception as e:                  # still loading, or busy
            log.debug(f"  could not read {url}: {e}")
            continue
        if html:
            found.append((season.group(1), url, html))

    if not found:
        _report_idle(len(targets))
    return found


def _report_idle(open_tabs: int) -> None:
    """Say why nothing is being saved.

    Every pass to begin with, so there is feedback immediately, then
    occasionally — at a five second poll, saying it every time would be twelve
    lines a minute of nothing new.
    """
    global _last_status
    now = time.time()
    if _passes > 3 and now - _last_status < STATUS_EVERY:
        return
    _last_status = now
    if open_tabs:
        log.info(f"  waiting — {open_tabs} tab(s) open, none of them a loaded "
                 f"results page yet")
    else:
        log.info("  waiting — no tabs open in the browser yet")


def _activate(ws_url: str | None) -> None:
    """Bring a tab to the front, so Chrome loads it.

    Chrome does not load a tab opened in the background until it is visited,
    and a VNC session on a phone is an awkward place to visit seven of them.
    This asks for the tab the script itself opened to be shown; it chooses no
    address and fetches nothing that was not already asked for.
    """
    if not ws_url:
        return
    try:
        import websocket

        connection = websocket.create_connection(
            ws_url, timeout=TAB_TIMEOUT, suppress_origin=True
        )
        try:
            connection.send(json.dumps({"id": 1, "method": "Page.bringToFront"}))
            connection.recv()
        finally:
            connection.close()
    except Exception as e:
        log.debug(f"  could not bring a tab to the front: {e}")


def save_ready_tabs(endpoint: str, out_dir: pathlib.Path, saved: set[str]) -> int:
    """Write every results tab that has finished loading. Returns how many."""
    global _passes
    _passes += 1
    names = league_names()
    written = 0
    ws_by_season = {
        SEASON_RE.search(t.get("url", "")).group(1): t.get("webSocketDebuggerUrl")
        for t in _safe_targets(endpoint)
        if SEASON_RE.search(t.get("url", "")) and "/results/" in t.get("url", "")
    }

    for season_id, url, html in open_results_tabs(endpoint):
        if season_id in saved:
            continue
        label = names.get(season_id, f"season {season_id}")

        if _passes <= 2:
            preview = " ".join(html[:110].split())
            log.info(f"  {label}: {len(html)} bytes, starts: {preview!r}")

        if is_unloaded(html):
            log.info(f"  {label}: tab not loaded yet — bringing it to the front")
            _activate(ws_by_season.get(season_id))
            continue
        # Rows first, deliberately. A page holding a results table is a results
        # page whatever else is on it, and Cloudflare's scripts appear on
        # ordinary pages too — checking for a challenge first threw away pages
        # that had loaded perfectly well.
        if ROW_MARKER not in html:
            if is_challenge_page(html):
                log.info(f"  {label}: still showing a challenge — solve it in the browser")
            else:
                log.info(f"  {label}: loaded, but holds no results table "
                         f"(this league may not have played yet)")
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
    misses = 0
    total_leagues = len(scrape.LEAGUES)
    log.info(f"Watching for {total_leagues} league(s) at {args.endpoint}")

    try:
        while True:
            try:
                save_ready_tabs(args.endpoint, out_dir, saved)
                misses = 0
            except Exception as e:
                # A browser loading seven pages is sometimes too busy to answer
                # its own debug port. Worth saying once, not every pass.
                misses += 1
                if misses == 1 or misses % 12 == 0:
                    log.warning(
                        f"Could not read the browser at {args.endpoint}: {e}"
                        + (" (still trying)" if args.watch else "")
                    )
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

    log.info(f"\n{len(saved)} of {total_leagues} league(s) saved to {out_dir}")
    if saved:
        log.info("The next scrape will parse them.")
    return 0 if saved else 1


if __name__ == "__main__":
    sys.exit(main())
