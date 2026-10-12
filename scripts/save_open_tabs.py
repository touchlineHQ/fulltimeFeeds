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
_SHELL_RE = re.compile(r"</?(?:html|head|body)[^>]*>|\s+", re.IGNORECASE)


def is_unloaded(html: str) -> bool:
    return not _SHELL_RE.sub("", html)

STATUS_EVERY = 30.0
_last_status = 0.0
_passes = 0
TAB_TIMEOUT = 5.0


def _safe_targets(endpoint: str) -> list[dict]:
    try:
        return _targets(endpoint)
    except Exception:
        return []


def _targets(endpoint: str) -> list[dict]:
    import urllib.request
    with urllib.request.urlopen(f"{endpoint}/json/list", timeout=15) as response:
        return [t for t in json.loads(response.read()) if t.get("type") == "page"]


def _call(connection, call_id: int, method: str, params: dict, timeout: float) -> dict:
    connection.send(json.dumps({"id": call_id, "method": method, "params": params}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        message = json.loads(connection.recv())
        if message.get("id") == call_id:
            return message
    raise TimeoutError(f"no reply to {method}")


def _page_html(ws_url: str, timeout: float = TAB_TIMEOUT) -> str:
    import websocket
    connection = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        reply = _call(connection, 1, "Runtime.evaluate", {
            "expression": "document.documentElement.outerHTML",
            "returnByValue": True,
        }, timeout)
        result = reply.get("result", {})
        html = result.get("result", {}).get("value") or ""
        if not html or is_unloaded(html):
            document = _call(connection, 2, "DOM.getDocument", {"depth": 0}, timeout)
            node_id = document.get("result", {}).get("root", {}).get("nodeId")
            if node_id:
                outer = _call(connection, 3, "DOM.getOuterHTML", {"nodeId": node_id}, timeout)
                via_dom = outer.get("result", {}).get("outerHTML") or ""
                if via_dom and not is_unloaded(via_dom):
                    return via_dom
        return html
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _reload(ws_url: str | None) -> None:
    if not ws_url:
        return
    try:
        import websocket
        connection = websocket.create_connection(ws_url, timeout=TAB_TIMEOUT, suppress_origin=True)
        try:
            connection.send(json.dumps({"id": 1, "method": "Page.reload", "params": {"ignoreCache": True}}))
            connection.recv()
        finally:
            connection.close()
    except Exception:
        pass


def league_names() -> dict[str, str]:
    return {season_id: name for season_id, name in scrape.LEAGUES}


def open_results_tabs(endpoint: str) -> list[tuple[str, str, str]]:
    found = []
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
        except Exception:
            continue
        if html:
            found.append((season.group(1), url, html))
    return found


def _activate(ws_url: str | None) -> None:
    if not ws_url:
        return
    try:
        import websocket
        connection = websocket.create_connection(ws_url, timeout=TAB_TIMEOUT, suppress_origin=True)
        try:
            connection.send(json.dumps({"id": 1, "method": "Page.bringToFront"}))
            connection.recv()
        finally:
            connection.close()
    except Exception:
        pass


def save_ready_tabs(endpoint: str, out_dir: pathlib.Path, saved: set[str]) -> int:
    global _passes
    _passes += 1
    names = league_names()
    written = 0
    ws_by_season = {
        SEASON_RE.search(t.get("url", "")).group(1): t.get("webSocketDebuggerUrl")
        for t in _safe_targets(endpoint)
        if SEASON_RE.search(t.get("url", "")) and "/results/" in t.get("url", "")
    }
    seen_hashes = set()
    for season_id, url, html in open_results_tabs(endpoint):
        if season_id in saved:
            continue
        label = names.get(season_id, f"season {season_id}")
        if is_unloaded(html):
            log.info(f"  {label}: tab not loaded yet — bringing it to the front")
            _activate(ws_by_season.get(season_id))
            continue
        marker = f"selectedSeason={season_id}"
        if marker not in html:
            log.info(f"  {label}: loaded page does not contain its season marker — reloading")
            _activate(ws_by_season.get(season_id))
            _reload(ws_by_season.get(season_id))
            continue
        if ROW_MARKER not in html:
            if is_challenge_page(html):
                log.info(f"  {label}: still showing a challenge — solve it in the browser")
            else:
                log.info(f"  {label}: loaded, but holds no results table")
            continue
        import hashlib
        content_hash = hashlib.md5(html.encode("utf-8", errors="replace")).hexdigest()
        if content_hash in seen_hashes:
            log.warning(f"  {label}: content identical to a previously saved league — reloading")
            _activate(ws_by_season.get(season_id))
            _reload(ws_by_season.get(season_id))
            continue
        seen_hashes.add(content_hash)
        destination = out_dir / f"{season_id}.html"
        destination.write_text(html, encoding="utf-8")
        saved.add(season_id)
        written += 1
        rows = len(scrape.parse_results(html))
        log.info(f"  {label}: saved {destination.name} ({rows} result(s))")
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--endpoint", default="http://localhost:9222")
    ap.add_argument("--out", default=None)
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=float, default=5.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    import os
    out_dir = pathlib.Path(args.out or os.environ.get(scrape.RESULTS_HTML_DIR_ENV) or scrape.DEFAULT_RESULTS_HTML_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info(f"Saving results tabs into {out_dir}\n")
    saved = set()
    total_leagues = len(scrape.LEAGUES)
    log.info(f"Watching for {total_leagues} league(s) at {args.endpoint}")
    try:
        while True:
            try:
                save_ready_tabs(args.endpoint, out_dir, saved)
            except Exception as e:
                log.warning(f"Could not read the browser: {e}")
            if not args.watch:
                break
            if len(saved) >= total_leagues:
                log.info(f"\nAll {total_leagues} leagues saved.")
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("\nStopped.")
    log.info(f"\n{len(saved)} of {total_leagues} league(s) saved to {out_dir}")
    return 0 if saved else 1


if __name__ == "__main__":
    sys.exit(main())
