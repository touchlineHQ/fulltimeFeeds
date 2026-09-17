"""End-to-end: a real browser, real tabs, saved pages, parsed results.

Every bug in the tab saver so far — signal handling, buffering, per-tab
timeouts, Playwright's whole-browser attach — passed the unit tests and failed
against an actual browser. This drives one: a local server shaped like
Full-Time's results page, seven tabs, one of them showing a challenge.

Skipped when there is no Chromium or no display to run it on.
"""

import http.server
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

import browser as browser_mod  # noqa: E402
import scrape  # noqa: E402

# Reading a tab needs a websocket client; without one there is nothing to test.
pytest.importorskip("websocket", reason="websocket-client is not installed")

MODULE_PATH = Path(__file__).parent.parent / "scripts" / "save_open_tabs.py"

ROW = (
    '<div class="fixture-row flex">'
    '<div class="date-col">{date} 15:00</div>'
    '<div class="home-team-col flex middle right">East Leake Robins</div>'
    '<div class="score-col">{n} - 0</div>'
    '<div class="road-team-col flex middle left">Opponent {n} FC</div>'
    '<div class="comp-col">Division One</div>'
    '<div class="venue-col">Some Ground</div>'
    "</div>"
)

# Shaped like the real interstitial: what identifies it sits below a lot of
# inline script rather than in a title at the top.
CHALLENGE_PAGE = (
    "<!DOCTYPE html><html><head>" + ("<!-- pad -->" * 400)
    + '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1"></script>'
    "<title>Just a moment...</title></head><body>Verifying...</body></html>"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _results_page(season: str, rows: int) -> str:
    body = "".join(ROW.format(date=f"{11 + n:02d}/09/26", n=n) for n in range(1, rows + 1))
    return (
        "<!DOCTYPE html><html><head><title>Full-Time Results</title></head><body>"
        f'<a href="/results/1/100000.html?selectedSeason={season}">this season</a>'
        f'<div class="results">{body}</div></body></html>'
    )


@pytest.fixture
def module():
    namespace = {"__name__": "save_open_tabs", "__file__": str(MODULE_PATH)}
    exec(compile(MODULE_PATH.read_text(), str(MODULE_PATH), "exec"), namespace)
    return namespace


@pytest.fixture
def site():
    """A server that can be told to challenge particular seasons."""
    challenged: set[str] = set()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            import urllib.parse

            query = urllib.parse.urlparse(self.path).query
            season = urllib.parse.parse_qs(query).get("selectedSeason", [""])[0]
            html = (CHALLENGE_PAGE if season in challenged
                    else _results_page(season, 3))
            data = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    port = _free_port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", challenged
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def live_browser(site):
    """A real browser with one tab per league, and its debugging endpoint."""
    base, _ = site
    try:
        executable = browser_mod.find_chromium()
        xvfb = browser_mod.ensure_display()
    except browser_mod.BrowserUnavailable as e:
        pytest.skip(f"no browser to drive: {e}")

    port = _free_port()
    profile = f"/tmp/pytest-tabs-{port}"
    urls = [
        f"{base}/results/1/100000.html?selectedSeason={season}"
        for season, _ in scrape.LEAGUES
    ]
    proc = subprocess.Popen(
        [executable, f"--user-data-dir={profile}", "--no-first-run",
         "--no-default-browser-check", "--no-sandbox",
         f"--remote-debugging-port={port}", *urls],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    endpoint = f"http://localhost:{port}"
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{endpoint}/json/version", timeout=1).read()
                break
            except Exception:
                if proc.poll() is not None:
                    pytest.skip("browser exited before its debug port opened")
                time.sleep(0.5)
        else:
            pytest.skip("browser never opened its debug port")

        # Give the tabs a moment to finish loading before reading them.
        deadline = time.time() + 30
        while time.time() < deadline:
            targets = json.loads(
                urllib.request.urlopen(f"{endpoint}/json/list", timeout=5).read()
            )
            if len([t for t in targets if t.get("type") == "page"]) >= len(urls):
                break
            time.sleep(0.5)
        yield endpoint
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if xvfb:
            xvfb.terminate()


class TestAgainstARealBrowser:

    def test_every_open_tab_is_saved_and_parses(self, module, live_browser, tmp_path):
        saved: set[str] = set()

        for _ in range(10):                     # tabs settle at their own pace
            module["save_ready_tabs"](live_browser, tmp_path, saved)
            if len(saved) == len(scrape.LEAGUES):
                break
            time.sleep(1)

        assert len(saved) == len(scrape.LEAGUES)
        for season, _ in scrape.LEAGUES:
            page = tmp_path / f"{season}.html"
            assert page.is_file()
            assert len(scrape.parse_results(page.read_text(encoding="utf-8"))) == 3

    def test_a_challenged_tab_is_not_saved_then_is_once_cleared(
        self, module, site, live_browser, tmp_path, monkeypatch
    ):
        base, challenged = site
        blocked_season = scrape.LEAGUES[2][0]
        challenged.add(blocked_season)

        # Reload that tab so it picks up the challenge, as the site would serve it.
        _reload(live_browser, blocked_season)
        saved: set[str] = set()
        for _ in range(10):
            module["save_ready_tabs"](live_browser, tmp_path, saved)
            if len(saved) >= len(scrape.LEAGUES) - 1:
                break
            time.sleep(1)

        assert blocked_season not in saved
        assert not (tmp_path / f"{blocked_season}.html").exists()

        # The person clears it; the next pass picks it up.
        challenged.discard(blocked_season)
        _reload(live_browser, blocked_season)
        for _ in range(10):
            module["save_ready_tabs"](live_browser, tmp_path, saved)
            if blocked_season in saved:
                break
            time.sleep(1)

        assert blocked_season in saved
        assert (tmp_path / f"{blocked_season}.html").is_file()

    def test_a_tab_can_be_brought_to_the_front(self, module, live_browser):
        # What makes the session usable when the VNC view cannot be clicked:
        # Chrome loads a background tab when it is shown, and this asks for the
        # tab the script itself opened to be shown.
        targets = json.loads(
            urllib.request.urlopen(f"{live_browser}/json/list", timeout=5).read()
        )
        pages = [t for t in targets if t.get("type") == "page"]
        assert pages

        before = pages[-1]["webSocketDebuggerUrl"]
        module["_activate"](before)             # must not raise

        # And the browser is still answering afterwards.
        after = json.loads(
            urllib.request.urlopen(f"{live_browser}/json/list", timeout=5).read()
        )
        assert len([t for t in after if t.get("type") == "page"]) == len(pages)

    def test_saved_pages_feed_the_scraper(self, module, live_browser, tmp_path, monkeypatch):
        saved: set[str] = set()
        for _ in range(10):
            module["save_ready_tabs"](live_browser, tmp_path, saved)
            if saved:
                break
            time.sleep(1)
        assert saved

        monkeypatch.setenv(scrape.RESULTS_HTML_DIR_ENV, str(tmp_path))
        monkeypatch.setattr(
            scrape, "_fetch_page",
            lambda url, label: (_ for _ in ()).throw(RuntimeError("HTTP Error 403")),
        )
        season, name = scrape.LEAGUES[0]

        results = scrape.fetch_results(season, name, browser=None)

        assert len(results) == 3
        assert "saved page" in scrape.LAST_SOURCE


def _reload(endpoint: str, season: str) -> None:
    """Reload one tab, standing in for the person clearing a challenge."""
    import websocket

    targets = json.loads(urllib.request.urlopen(f"{endpoint}/json/list", timeout=5).read())
    target = next(
        t for t in targets
        if t.get("type") == "page" and f"selectedSeason={season}" in t.get("url", "")
    )
    connection = websocket.create_connection(
        target["webSocketDebuggerUrl"], timeout=10, suppress_origin=True
    )
    try:
        connection.send(json.dumps({"id": 1, "method": "Page.reload",
                                    "params": {"ignoreCache": True}}))
        deadline = time.time() + 10
        while time.time() < deadline:
            if json.loads(connection.recv()).get("id") == 1:
                break
    finally:
        connection.close()
    time.sleep(1)


class TestReadingMechanisms:
    """Both ways of asking a tab for its HTML must work against a real browser."""

    def test_dom_fallback_returns_the_same_document(self, module, live_browser):
        import websocket

        targets = json.loads(
            urllib.request.urlopen(f"{live_browser}/json/list", timeout=5).read()
        )
        page = next(t for t in targets if "/results/" in t.get("url", ""))
        ws_url = page["webSocketDebuggerUrl"]

        via_evaluate = module["_page_html"](ws_url)
        assert "home-team-col" in via_evaluate

        connection = websocket.create_connection(ws_url, timeout=10, suppress_origin=True)
        try:
            document = module["_call"](connection, 2, "DOM.getDocument", {"depth": 0}, 10)
            node_id = document["result"]["root"]["nodeId"]
            outer = module["_call"](
                connection, 3, "DOM.getOuterHTML", {"nodeId": node_id}, 10
            )
            via_dom = outer["result"]["outerHTML"]
        finally:
            connection.close()

        assert "home-team-col" in via_dom
        assert len(scrape.parse_results(via_dom)) == len(scrape.parse_results(via_evaluate))
