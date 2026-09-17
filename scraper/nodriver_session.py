"""A results fetcher built on nodriver, for people who choose to run one.

The bundled BrowserSession is refused: measured on this project's own server,
the same container's browser loads the results page when a person drives it and
is challenged when Playwright drives it, from the same address, same binary and
same display.  The difference is ``--remote-debugging-port`` and an attached CDP
session, which is observable from the page.

nodriver drives Chrome without that pattern, which is why it gets past a check
the bundled session does not.  That is a deliberate choice about someone else's
service, so it is opt-in and never the default:

    pip install nodriver
    export RESULTS_SESSION=nodriver_session:Session

Know what you are taking on.  Automated access is likely contrary to Full-Time's
terms even though the data is public and readable by hand from the same machine;
that call belongs to whoever runs this.  It is also an arms race — expect it to
stop working after a Cloudflare or Chrome update, without notice.  When it does,
`fetch` raises, the league is published as ``results_unavailable``, and the feed
says so rather than quietly going empty, which is the failure mode worth having.
"""

import asyncio
import logging
import subprocess

from browser import BrowserUnavailable, ensure_display, find_chromium, is_challenge_page

log = logging.getLogger(__name__)

SETTLE_SECONDS = 60
POLL_SECONDS = 3


class Session:
    """Satisfies the RESULTS_SESSION protocol: fetch(url, wait_selector), close()."""

    def __init__(
        self,
        settle_seconds: int = SETTLE_SECONDS,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.settle_seconds = settle_seconds
        self.poll_seconds = poll_seconds
        self.fetches = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._browser = None
        self._xvfb: subprocess.Popen | None = None

    # -- plumbing --------------------------------------------------------

    def _run(self, coro):
        """nodriver is asyncio; the scraper is not. One loop serves the run."""
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    async def _started(self):
        if self._browser is not None:
            return self._browser

        try:
            import nodriver
        except ImportError as e:
            raise BrowserUnavailable(
                "nodriver is not installed — `pip install nodriver`, or unset "
                "RESULTS_SESSION to fall back to the bundled browser"
            ) from e

        self._xvfb = ensure_display()
        log.info("  nodriver: starting a browser")
        self._browser = await nodriver.start(
            headless=False,
            browser_executable_path=find_chromium(),
            browser_args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        return self._browser

    # -- use -------------------------------------------------------------

    async def _fetch(self, url: str, wait_selector: str | None) -> str:
        browser = await self._started()
        tab = await browser.get(url)

        # A challenge clears by reloading itself, which takes longer than a
        # selector wait allows for; polling tells "never cleared" apart from
        # "was not given time".
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.settle_seconds
        html = await tab.get_content()
        while loop.time() < deadline and is_challenge_page(html):
            await asyncio.sleep(self.poll_seconds)
            html = await tab.get_content()
        return html

    def fetch(self, url: str, wait_selector: str | None = None) -> str:
        self.fetches += 1
        return self._run(self._fetch(url, wait_selector))

    # -- teardown --------------------------------------------------------

    def close(self) -> None:
        if self._browser is not None:
            try:
                stop = self._browser.stop()
                # nodriver's stop() is sync in some versions, a coroutine in others.
                if asyncio.iscoroutine(stop):
                    self._run(stop)
            except Exception as e:
                log.debug(f"  nodriver: stop failed ({e})")
            self._browser = None

        if self._loop is not None:
            try:
                self._loop.close()
            except Exception as e:
                log.debug(f"  nodriver: closing the loop failed ({e})")
            self._loop = None

        if self._xvfb is not None:
            self._xvfb.terminate()
            self._xvfb = None
