"""Unit tests for scripts/save_open_tabs.py, with the browser stubbed.

What matters is which tabs get written and which are left alone: a page still
showing a challenge, or one with no rows yet, must not be saved over a good one.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scraper"))

MODULE_PATH = pathlib.Path(__file__).parent.parent / "scripts" / "save_open_tabs.py"

CHALLENGE = "<html><head><title>Just a moment...</title></head></html>"
EMPTY = "<html><body><p>No results found</p></body></html>"


def _page(season_id, rows=2):
    body = "".join(
        f"<tr><td class='left'>1{i}/09/26 15:00</td>"
        f"<td class='home-team'>East Leake Robins</td>"
        f"<td class='score'>{i} - 0</td>"
        f"<td class='road-team'>Cotgrave</td>"
        f"<td class='left'>Division One</td></tr>"
        for i in range(1, rows + 1)
    )
    return (f"<html><body><a href='?selectedSeason={season_id}'>x</a>"
            f"<table>{body}</table></body></html>")


@pytest.fixture
def module():
    namespace = {"__name__": "save_open_tabs", "__file__": str(MODULE_PATH)}
    exec(compile(MODULE_PATH.read_text(), str(MODULE_PATH), "exec"), namespace)
    return namespace


def _url(season_id):
    return (f"https://fulltime.thefa.com/results/1/100000.html"
            f"?selectedSeason={season_id}&selectedFixtureGroupKey=")


class TestSaveReadyTabs:

    def test_a_loaded_tab_is_written_under_its_season(self, module, tmp_path, monkeypatch):
        monkeypatch.setitem(
            module, "open_results_tabs",
            lambda endpoint: [("918978398", _url("918978398"), _page("918978398"))],
        )
        saved = set()

        written = module["save_ready_tabs"]("http://x", tmp_path, saved)

        assert written == 1
        assert saved == {"918978398"}
        assert (tmp_path / "918978398.html").is_file()

    def test_a_challenge_page_is_not_saved(self, module, tmp_path, monkeypatch):
        # Saving one would publish an interstitial as though it were results.
        monkeypatch.setitem(
            module, "open_results_tabs",
            lambda endpoint: [("918978398", _url("918978398"), CHALLENGE)],
        )
        saved = set()

        assert module["save_ready_tabs"]("http://x", tmp_path, saved) == 0
        assert list(tmp_path.iterdir()) == []

    def test_a_page_without_rows_is_not_saved(self, module, tmp_path, monkeypatch):
        monkeypatch.setitem(
            module, "open_results_tabs",
            lambda endpoint: [("918978398", _url("918978398"), EMPTY)],
        )
        saved = set()

        assert module["save_ready_tabs"]("http://x", tmp_path, saved) == 0
        assert list(tmp_path.iterdir()) == []

    def test_an_already_saved_league_is_not_rewritten(self, module, tmp_path, monkeypatch):
        monkeypatch.setitem(
            module, "open_results_tabs",
            lambda endpoint: [("918978398", _url("918978398"), _page("918978398"))],
        )
        saved = {"918978398"}

        assert module["save_ready_tabs"]("http://x", tmp_path, saved) == 0

    def test_each_league_lands_in_its_own_file(self, module, tmp_path, monkeypatch):
        monkeypatch.setitem(
            module, "open_results_tabs",
            lambda endpoint: [
                ("918978398", _url("918978398"), _page("918978398")),
                ("71450136", _url("71450136"), _page("71450136", rows=3)),
            ],
        )
        saved = set()

        assert module["save_ready_tabs"]("http://x", tmp_path, saved) == 2
        assert {p.name for p in tmp_path.iterdir()} == {"918978398.html", "71450136.html"}

    def test_a_mixed_screen_saves_only_what_is_ready(self, module, tmp_path, monkeypatch):
        monkeypatch.setitem(
            module, "open_results_tabs",
            lambda endpoint: [
                ("918978398", _url("918978398"), _page("918978398")),
                ("71450136", _url("71450136"), CHALLENGE),
            ],
        )
        saved = set()

        assert module["save_ready_tabs"]("http://x", tmp_path, saved) == 1
        assert saved == {"918978398"}


class TestSavedPagesFeedTheScraper:

    def test_what_is_written_is_what_the_scraper_looks_for(self, module, tmp_path, monkeypatch):
        # The two halves have to agree: the scraper finds saved pages by the
        # season named in the page, so a saved tab must carry it.
        import scrape

        monkeypatch.setitem(
            module, "open_results_tabs",
            lambda endpoint: [("918978398", _url("918978398"), _page("918978398"))],
        )
        module["save_ready_tabs"]("http://x", tmp_path, set())
        monkeypatch.setenv(scrape.RESULTS_HTML_DIR_ENV, str(tmp_path))

        found = scrape._saved_results_page("918978398")

        assert found is not None
        assert len(scrape.parse_results(found[0])) == 2


class TestIdleReporting:
    """Silence and "nothing happening yet" must not look the same."""

    def test_idle_says_how_many_tabs_are_open(self, module, caplog):
        module["_last_status"] = 0.0

        with caplog.at_level("INFO", logger="save_open_tabs"):
            module["_report_idle"](7)

        assert "7 tab(s) open" in caplog.text

    def test_idle_with_no_tabs_says_so(self, module, caplog):
        module["_last_status"] = 0.0

        with caplog.at_level("INFO", logger="save_open_tabs"):
            module["_report_idle"](0)

        assert "no tabs open" in caplog.text

    def test_the_first_few_passes_report_immediately(self, module, caplog):
        # Waiting thirty seconds for the first word is indistinguishable from a
        # hang, which is the thing this exists to rule out.
        module["_last_status"] = 0.0
        module["_passes"] = 0

        with caplog.at_level("INFO", logger="save_open_tabs"):
            module["_report_idle"](7)
            module["_report_idle"](7)
            module["_report_idle"](7)

        assert caplog.text.count("tab(s) open") == 3

    def test_then_it_settles_down(self, module, caplog):
        # At a five second poll, every pass would be twelve lines a minute of
        # nothing new.
        module["_last_status"] = 0.0
        module["_passes"] = 0

        with caplog.at_level("INFO", logger="save_open_tabs"):
            for _ in range(10):
                module["_report_idle"](7)

        assert caplog.text.count("tab(s) open") == 3


class TestDirectCdp:
    """Reading one tab at a time, so an unresponsive tab costs only itself."""

    @staticmethod
    def _install_websocket(monkeypatch, replies):
        """Stub websocket-client. `replies` maps ws url -> list of messages."""
        import json as json_mod

        class _Connection:
            def __init__(self, url):
                self.url = url
                self._queue = list(replies.get(url, []))

            def send(self, payload):
                assert "Runtime.evaluate" in payload

            def recv(self):
                if not self._queue:
                    raise TimeoutError("timed out")
                item = self._queue.pop(0)
                return item if isinstance(item, str) else json_mod.dumps(item)

            def close(self):
                pass

        module = type(sys)("websocket")
        module.create_connection = lambda url, timeout=None, suppress_origin=None: (
            _Connection(url)
        )
        monkeypatch.setitem(sys.modules, "websocket", module)

    def test_html_is_read_from_the_evaluate_reply(self, module, monkeypatch):
        self._install_websocket(monkeypatch, {
            "ws://tab-1": [{"id": 1, "result": {"result": {"value": "<html>hi</html>"}}}],
        })

        assert module["_page_html"]("ws://tab-1") == "<html>hi</html>"

    def test_events_arriving_first_are_stepped_over(self, monkeypatch, module):
        # A busy tab emits console and network events; the reply we want is the
        # one carrying our id.
        self._install_websocket(monkeypatch, {
            "ws://tab-1": [
                {"method": "Network.requestWillBeSent", "params": {}},
                {"method": "Runtime.consoleAPICalled", "params": {}},
                {"id": 1, "result": {"result": {"value": "<html>ok</html>"}}},
            ],
        })

        assert module["_page_html"]("ws://tab-1") == "<html>ok</html>"

    def test_a_silent_tab_raises_rather_than_hanging_the_pass(self, monkeypatch, module):
        self._install_websocket(monkeypatch, {"ws://tab-1": []})

        with pytest.raises(Exception):
            module["_page_html"]("ws://tab-1")

    def test_one_dead_tab_does_not_stop_the_others(self, module, monkeypatch):
        targets = [
            {"type": "page", "url": _url("918978398"), "webSocketDebuggerUrl": "ws://ok"},
            {"type": "page", "url": _url("71450136"), "webSocketDebuggerUrl": "ws://dead"},
        ]
        monkeypatch.setitem(module, "_targets", lambda endpoint: targets)

        def read(ws_url, timeout=None):
            if ws_url == "ws://dead":
                raise TimeoutError("timed out")
            return _page("918978398")

        monkeypatch.setitem(module, "_page_html", read)

        found = module["open_results_tabs"]("http://localhost:9222")

        assert [season for season, _, _ in found] == ["918978398"]

    def test_non_results_tabs_are_ignored(self, module, monkeypatch):
        monkeypatch.setitem(module, "_targets", lambda endpoint: [
            {"type": "page", "url": "https://api.ipify.org", "webSocketDebuggerUrl": "ws://a"},
            {"type": "page", "url": "about:blank", "webSocketDebuggerUrl": "ws://b"},
        ])
        monkeypatch.setitem(module, "_page_html", lambda ws, timeout=None: "<html></html>")

        assert module["open_results_tabs"]("http://localhost:9222") == []
