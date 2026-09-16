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
