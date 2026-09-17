"""Unit tests for scraper/nodriver_session.py, with nodriver stubbed.

nodriver is optional and not installed for tests; what matters here is the
wiring around it — lazy start, challenge polling, the counter the run summary
reads, and a close() that is safe whatever state the session is in.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

import browser as browser_mod  # noqa: E402

CHALLENGE = "<html><head><title>Just a moment...</title></head></html>"
ROWS = "<html><td class='home-team'>Arnold Town</td></html>"


class _FakeTab:
    def __init__(self, pages):
        self._pages = list(pages)

    async def get_content(self):
        return self._pages.pop(0) if len(self._pages) > 1 else self._pages[0]


class _FakeBrowser:
    def __init__(self, pages):
        self.pages = pages
        self.requested = []
        self.stopped = False

    async def get(self, url):
        self.requested.append(url)
        return _FakeTab(self.pages)

    def stop(self):
        self.stopped = True


@pytest.fixture
def nodriver_session(monkeypatch):
    """Import the module with nodriver and the display stubbed out."""
    started = {}

    async def fake_start(**kwargs):
        started.update(kwargs)
        return started["browser"]

    stub = MagicMock()
    stub.start = fake_start
    monkeypatch.setitem(sys.modules, "nodriver", stub)
    monkeypatch.setattr(browser_mod, "ensure_display", lambda: None)
    monkeypatch.setattr(browser_mod, "find_chromium", lambda: "/usr/bin/chromium")

    import nodriver_session as module

    monkeypatch.setattr(module, "ensure_display", lambda: None)
    monkeypatch.setattr(module, "find_chromium", lambda: "/usr/bin/chromium")
    return module, started


class TestFetch:

    def test_a_page_that_answers_is_returned(self, nodriver_session):
        module, started = nodriver_session
        started["browser"] = _FakeBrowser([ROWS])
        session = module.Session()

        try:
            assert session.fetch("https://example.test/results") == ROWS
            assert started["browser"].requested == ["https://example.test/results"]
        finally:
            session.close()

    def test_a_challenge_is_polled_until_it_clears(self, nodriver_session):
        module, started = nodriver_session
        started["browser"] = _FakeBrowser([CHALLENGE, CHALLENGE, ROWS])
        session = module.Session(settle_seconds=30, poll_seconds=0.01)

        try:
            assert session.fetch("https://example.test/results") == ROWS
        finally:
            session.close()

    def test_a_challenge_that_never_clears_is_returned_as_it_is(self, nodriver_session):
        # Returning the interstitial lets fetch_results raise ResultsUnavailable,
        # which is what puts the flag on the feed.
        module, started = nodriver_session
        started["browser"] = _FakeBrowser([CHALLENGE])
        session = module.Session(settle_seconds=0.2, poll_seconds=0.01)

        try:
            assert "just a moment" in session.fetch("https://example.test/r").lower()
        finally:
            session.close()

    def test_the_browser_is_started_once_and_reused(self, nodriver_session):
        module, started = nodriver_session
        started["browser"] = _FakeBrowser([ROWS])
        session = module.Session()

        try:
            session.fetch("https://example.test/a")
            session.fetch("https://example.test/b")
            assert len(started["browser"].requested) == 2
            assert session.fetches == 2
        finally:
            session.close()

    def test_nothing_starts_until_the_first_fetch(self, nodriver_session):
        module, started = nodriver_session
        started["browser"] = _FakeBrowser([ROWS])

        session = module.Session()

        assert session._browser is None
        session.close()


class TestMissingDependency:

    def test_absent_nodriver_is_reported_as_browser_unavailable(self, monkeypatch):
        import nodriver_session as module

        monkeypatch.setitem(sys.modules, "nodriver", None)
        session = module.Session()

        with pytest.raises(browser_mod.BrowserUnavailable, match="pip install nodriver"):
            session.fetch("https://example.test/results")
        session.close()


class TestClose:

    def test_close_before_any_fetch_is_safe(self):
        import nodriver_session as module

        module.Session().close()

    def test_close_stops_the_browser_and_is_repeatable(self, nodriver_session):
        module, started = nodriver_session
        browser = _FakeBrowser([ROWS])
        started["browser"] = browser
        session = module.Session()
        session.fetch("https://example.test/results")

        session.close()
        session.close()

        assert browser.stopped is True
        assert session._browser is None
