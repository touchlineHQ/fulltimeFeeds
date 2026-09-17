"""Unit tests for scraper/browser.py — the parts that need no real browser."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from browser import BrowserSession, BrowserUnavailable, is_challenge_page


class TestIsChallengePage:

    @pytest.mark.parametrize("html", [
        "<html><title>Attention Required!</title></html>",
        "<html><title>Just a moment...</title></html>",
        "<html><body>Checking your browser before accessing</body></html>",
        "<html><div class='cf-challenge'></div></html>",
    ])
    def test_interstitials_are_recognised(self, html):
        assert is_challenge_page(html) is True

    @pytest.mark.parametrize("html", [
        "<html><td class='home-team'>Arnold Town</td></html>",
        "",
        "<html><body>No results found for this division</body></html>",
    ])
    def test_real_pages_are_not(self, html):
        assert is_challenge_page(html) is False

    def test_only_the_start_of_the_page_is_considered(self):
        # A results page that happens to name a team "Just a moment" deep in
        # the body is not an interstitial.
        from browser import INSPECT_CHARS

        html = ("<html><td class='home-team'>x</td>"
                + ("y" * (INSPECT_CHARS + 1000)) + "just a moment</html>")

        assert is_challenge_page(html) is False

    @pytest.mark.parametrize("marker", [
        '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1"></script>',
        '<input type="hidden" name="__cf_chl_tk" value="abc">',
    ])
    def test_cloudflares_own_markers_are_recognised(self, marker):
        # The visible "Just a moment" title is not always near the top of the
        # document; these are on every challenge page.
        html = "<html><head>" + ("<!-- padding -->" * 100) + marker + "</head></html>"

        assert is_challenge_page(html) is True


class TestStartDisplay:

    def test_existing_display_is_left_alone(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        session = BrowserSession()

        session._start_display()

        assert session._xvfb is None

    def test_no_display_and_no_xvfb_is_reported(self, monkeypatch):
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setattr("browser.shutil.which", lambda name: None)

        with pytest.raises(BrowserUnavailable, match="no Xvfb"):
            BrowserSession()._start_display()


class TestClose:

    def test_close_is_safe_before_anything_started(self):
        BrowserSession().close()          # must not raise

    def test_close_runs_every_step_even_when_one_fails(self):
        session = BrowserSession()
        stopped = []

        class _Boom:
            def close(self):
                raise RuntimeError("browser already gone")

        class _Proc:
            def terminate(self):
                stopped.append("proc")

        session._browser = _Boom()
        session._proc = _Proc()
        session._xvfb = _Proc()

        session.close()

        assert stopped == ["proc", "proc"]
        assert session._proc is None
