"""A stock browser, started directly and driven over CDP.

Full-Time refuses an automated client on the pages carrying scores, and a
browser Playwright launches is an automated client: launching sets
``navigator.webdriver`` and passes ``--enable-automation``, which a page can
see.  Measured on the same binary, ``navigator.webdriver`` is ``true`` when
Playwright launches it and ``false`` when the binary is started directly and
attached to over CDP.  This module does the latter — an ordinary browser,
driven from outside, with nothing about it disguised.

One session serves every league in a run.  Starting a browser per league would
cost far more than the pages themselves do, and the session is started lazily,
so a run whose plain fetches succeed never launches one at all.
"""

import logging
import os
import pathlib
import shutil
import subprocess
import time
import urllib.request

log = logging.getLogger(__name__)

# Cloudflare shows two pages and they mean opposite things: a refusal, and a
# challenge it expects a browser to solve.
BLOCK_MARKERS = ("attention required",)
# "just a moment" is the visible title, but it is not always near the top of a
# challenge page; the challenge-platform script tag and the __cf_chl token are
# on every one of them and are what make this reliable.
CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-challenge",
    "cdn-cgi/challenge-platform",
    "__cf_chl",
)
# How much of a page to look at. A challenge page can carry a lot of inline
# script before anything identifying it.
INSPECT_CHARS = 8_000

DEFAULT_PORT = 9222
DEFAULT_PROFILE = "/tmp/fulltime-browser"
# How long to let a challenge clear itself before giving up on a page.
SETTLE_SECONDS = 60


class BrowserUnavailable(RuntimeError):
    """No browser could be started — Playwright missing, or no binary."""


def ensure_display() -> subprocess.Popen | None:
    """Make sure there is a display to draw on, starting Xvfb if there is not.

    Returns the Xvfb process when one was started, so the caller can stop it,
    or None when a display was already set. xvfb-run would do this but needs
    xauth, which the slim image does not carry; driving Xvfb directly needs
    neither.
    """
    if os.environ.get("DISPLAY"):
        return None
    if not shutil.which("Xvfb"):
        raise BrowserUnavailable("no DISPLAY and no Xvfb to start one")

    proc = subprocess.Popen(
        ["Xvfb", ":99", "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    socket = pathlib.Path("/tmp/.X11-unix/X99")
    for _ in range(50):
        if socket.exists():
            os.environ["DISPLAY"] = ":99"
            log.info("  browser: started Xvfb on :99")
            return proc
        if proc.poll() is not None:
            raise BrowserUnavailable("Xvfb exited before opening its socket")
        time.sleep(0.1)
    proc.terminate()
    raise BrowserUnavailable("Xvfb never opened its socket")


def find_chromium() -> str:
    """Path to a bundled Chromium, whatever build this image happens to carry."""
    roots = [
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH"),
        str(pathlib.Path.home() / ".cache/ms-playwright"),
        "/root/.cache/ms-playwright",
        "/opt/pw-browsers",
    ]
    for root in roots:
        if not root:
            continue
        base = pathlib.Path(root)
        if not base.is_dir():
            continue
        found = sorted(base.glob("chromium-*/chrome-linux*/chrome"))
        if found:
            return str(found[-1])
    raise BrowserUnavailable("no chromium binary found in this image")


def is_challenge_page(html: str) -> bool:
    """True when a response is an interstitial rather than the page asked for."""
    head = html[:INSPECT_CHARS].lower()
    return any(m in head for m in BLOCK_MARKERS + CHALLENGE_MARKERS)


class BrowserSession:
    """Lazily-started browser, reused across every fetch in a run."""

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        profile: str = DEFAULT_PROFILE,
        settle_seconds: int = SETTLE_SECONDS,
    ) -> None:
        self.port = port
        self.profile = profile
        self.settle_seconds = settle_seconds
        self._proc: subprocess.Popen | None = None
        self._xvfb: subprocess.Popen | None = None
        self._playwright = None
        self._browser = None
        self._context = None
        # How many pages this session served, so a run can report which
        # leagues needed it.
        self.fetches = 0

    # -- startup ---------------------------------------------------------

    def _executable(self) -> str:
        """The bundled Chromium, whatever build the image happens to carry."""
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        try:
            candidate = pw.chromium.executable_path
        finally:
            pw.stop()

        if pathlib.Path(candidate).exists():
            return candidate

        root = pathlib.Path(
            os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
            or pathlib.Path.home() / ".cache/ms-playwright"
        )
        found = sorted(root.glob("chromium-*/chrome-linux*/chrome")) if root.is_dir() else []
        if not found:
            raise BrowserUnavailable(f"no chromium binary (looked for {candidate})")
        return str(found[-1])

    def _start_display(self) -> None:
        self._xvfb = ensure_display()

    def _debug_port_open(self) -> bool:
        try:
            urllib.request.urlopen(
                f"http://localhost:{self.port}/json/version", timeout=2
            ).read()
            return True
        except Exception:
            return False

    def start(self) -> None:
        if self._context is not None:
            return

        executable = self._executable()
        self._start_display()

        log.info(f"  browser: starting {executable} on port {self.port}")
        self._proc = subprocess.Popen(
            [
                executable,
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.profile}",
                "--no-first-run",
                "--no-default-browser-check",
                # Required as root in a container; not visible to the page.
                "--no-sandbox",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        for _ in range(60):                       # up to 30s
            if self._debug_port_open():
                break
            if self._proc.poll() is not None:
                raise BrowserUnavailable("browser exited before its debug port opened")
            time.sleep(0.5)
        else:
            raise BrowserUnavailable(f"debug port {self.port} never opened")

        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.connect_over_cdp(
            f"http://localhost:{self.port}"
        )
        self._context = (
            self._browser.contexts[0]
            if self._browser.contexts
            else self._browser.new_context()
        )
        log.info("  browser: attached")

    # -- use -------------------------------------------------------------

    def fetch(self, url: str, wait_selector: str | None = None) -> str:
        """Return the page's HTML, giving a challenge time to clear itself."""
        self.start()
        self.fetches += 1
        page = self._context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            deadline = time.time() + self.settle_seconds
            html = page.content()
            while time.time() < deadline and is_challenge_page(html):
                if wait_selector and page.query_selector(wait_selector):
                    break
                time.sleep(3)
                html = page.content()
            return page.content()
        finally:
            page.close()

    # -- teardown --------------------------------------------------------

    def close(self) -> None:
        for shut in (
            lambda: self._browser and self._browser.close(),
            lambda: self._playwright and self._playwright.stop(),
            lambda: self._proc and self._proc.terminate(),
            lambda: self._xvfb and self._xvfb.terminate(),
        ):
            try:
                shut()
            except Exception as e:
                log.debug(f"  browser: shutdown step failed ({e})")
        self._browser = self._playwright = self._context = None
        self._proc = self._xvfb = None
