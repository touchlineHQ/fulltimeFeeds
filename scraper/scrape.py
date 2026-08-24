"""
YEL East Midlands — Full-Time fixture scraper
Generates one .ics file per team and JSON feeds across all configured leagues/seasons.
"""

import json
import os
import random
import re
import sys
import time
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import NamedTuple

from curl_cffi import requests as curl_requests
from bs4 import BeautifulSoup

from index import write_index

logging.basicConfig(
    level=logging.DEBUG if os.environ.get("DEBUG") else logging.INFO,
    format="%(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FIXTURES_URL = "https://fulltime.thefa.com/fixtures/1/100000.html"
RESULTS_URL = "https://fulltime.thefa.com/results/1/100000.html"

# Each league is identified by its selectedSeason parameter on Full-Time.
# Update these season IDs at the start of each new season.
LEAGUES: list[tuple[str, str]] = [
    #("909330396", "YEL East Midlands Sunday 25/26"),
    ("876713597", "YEL East Midlands Sunday 26/27"),
    #("161954265", "YEL East Midlands Saturday 25/26"),
    ("773286682", "YEL East Midlands Saturday 26/27"),
    #("355008724", "Euro Soccer Nottinghamshire Senior League 25/26"),
    ("918978398", "Euro Soccer Nottinghamshire Senior League 26/27"),
    #("258824685", "Nottinghamshire Girls and Ladies Football League 25/26"),
    ("179857386", "Nottinghamshire Girls and Ladies Football League 26/27"),
]

OUTPUT_DIR = Path(__file__).parent.parent / "calendars"
OUTPUT_DIR.mkdir(exist_ok=True)

FEEDS_DIR = Path(__file__).parent.parent / "feeds"
FEEDS_DIR.mkdir(exist_ok=True)

# Retry configuration for HTTP requests
HTTP_RETRIES = 5
HTTP_BACKOFF_FACTOR = 2  # waits 2s, 4s, 8s, 16s, 32s between retries (+ jitter)
HTTP_TIMEOUT = 90  # seconds

# Explicit browser headers sent on every fetch. Full-Time's WAF rejects
# non-browser User-Agents with HTTP 403 (observed from 2026-08-23) and also
# blocks intermittently, so we make each request look as browser-like as
# possible on top of curl_cffi's TLS impersonation.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Referer": "https://fulltime.thefa.com/",
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class Fixture(NamedTuple):
    date: str          # e.g. "22/03/26" (DD/MM/YY)
    time: str          # e.g. "10:00" or "" if TBC
    home_team: str
    away_team: str
    venue: str         # home team's ground, if listed
    division_label: str


class Result(NamedTuple):
    date: str
    time: str
    home_team: str
    away_team: str
    home_score: int | None
    away_score: int | None
    venue: str
    division_label: str


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def _fetch_page(url: str, label: str) -> str:
    """Fetch a URL with retries and browser impersonation. Returns response text."""
    last_err: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            with curl_requests.Session(impersonate="chrome") as session:
                session.headers.update(BROWSER_HEADERS)
                resp = session.get(url, timeout=HTTP_TIMEOUT)
                resp.raise_for_status()
                return resp.text
        except Exception as e:
            last_err = e
            if attempt < HTTP_RETRIES:
                # Jittered exponential backoff so repeated daily runs don't
                # present an identical, bot-shaped retry cadence to the WAF.
                wait = HTTP_BACKOFF_FACTOR * (2 ** (attempt - 1))
                wait *= 1 + random.uniform(0, 0.25)
                log.warning(
                    f"{label} attempt {attempt}/{HTTP_RETRIES} failed: {e} "
                    f"— retrying in {wait:.1f}s"
                )
                time.sleep(wait)
            else:
                log.error(f"{label} all {HTTP_RETRIES} attempts failed: {e}")

    raise last_err  # type: ignore[misc]


def fetch_fixtures(season_id: str, league_name: str) -> list[Fixture]:
    """Fetch all upcoming fixtures for a given season/league."""
    url = f"{FIXTURES_URL}?selectedSeason={season_id}&selectedFixtureGroupKey="
    log.info(f"Fetching fixtures for {league_name} ...")
    return parse_fixtures(_fetch_page(url, f"fixtures/{league_name}"))


def _fetch_page_js(url: str, label: str) -> str:
    """Fetch a JavaScript-rendered page using Playwright (headless Chromium).

    Waits for the results table to appear (up to 20 s) before returning the
    fully-rendered HTML.  Falls back to an empty string on any error so that
    callers can degrade gracefully.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    except ImportError:
        log.error("playwright not installed — cannot fetch JS-rendered results page")
        return ""

    last_err: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    context = browser.new_context()
                    # Pre-accept OneTrust consent as a belt-and-suspenders measure
                    context.add_cookies([
                        {
                            "name": "OptanonAlertBoxClosed",
                            "value": "2024-01-01T00:00:00.000Z",
                            "domain": "fulltime.thefa.com",
                            "path": "/",
                        },
                        {
                            "name": "OptanonConsent",
                            "value": "isGpcEnabled=0&datestamp=Mon+Jan+01+2024&version=202401.1.0&isIABGlobal=false&hosts=&consentId=scraper&interactionCount=1&landingPath=NotLandingPage&groups=C0001%3A1%2CC0002%3A1%2CC0003%3A1%2CC0004%3A1",
                            "domain": "fulltime.thefa.com",
                            "path": "/",
                        },
                    ])
                    page = context.new_page()

                    # Block OneTrust's script-blocker so no data-loading scripts
                    # are suppressed while consent is being evaluated.
                    page.route("**/*OtAutoBlock*", lambda route: route.abort())
                    page.route("**/*otSDKStub*", lambda route: route.abort())

                    # Log every XHR/fetch request in DEBUG mode so we can see
                    # what data endpoints the page calls.
                    if log.isEnabledFor(logging.DEBUG):
                        def _log_req(req):
                            if req.resource_type in ("xhr", "fetch"):
                                log.debug(f"  XHR/fetch → {req.method} {req.url}")
                        page.on("request", _log_req)

                    page.goto(url, wait_until="networkidle", timeout=HTTP_TIMEOUT * 1000)
                    try:
                        page.wait_for_selector("td.home-team", timeout=30_000)
                    except PWTimeoutError:
                        log.warning(f"{label}: timed out waiting for td.home-team — returning partial HTML")
                    html = page.content()
                finally:
                    browser.close()
            return html
        except Exception as e:
            last_err = e
            if attempt < HTTP_RETRIES:
                wait = HTTP_BACKOFF_FACTOR * (2 ** (attempt - 1))
                log.warning(
                    f"{label} attempt {attempt}/{HTTP_RETRIES} failed: {e} "
                    f"— retrying in {wait}s"
                )
                time.sleep(wait)
            else:
                log.error(f"{label} all {HTTP_RETRIES} attempts failed: {e}")

    return ""


def fetch_results(season_id: str, league_name: str) -> list[Result]:
    """Fetch all results for a given season/league."""
    url = f"{RESULTS_URL}?selectedSeason={season_id}&selectedFixtureGroupKey="
    log.info(f"Fetching results for {league_name} ...")
    html = _fetch_page(url, f"results/{league_name}")
    log.debug(f"Results HTML length: {len(html)}, tables: {html.count('<table')}")
    return parse_results(html)


def _find_fixture_table(soup: BeautifulSoup, context: str) -> object | None:
    """Return the first table containing fixture/result rows.

    Tries two strategies:
    1. A table that has a "Home Team" header (fixtures page).
    2. A table that contains at least one cell with class "home-team" (results
       page may omit the header or use a different label).
    """
    tables = soup.find_all("table")
    # Strategy 1: header text
    for t in tables:
        if t.find(string=re.compile(r"Home Team", re.I)):
            return t
    # Strategy 2: data rows
    for t in tables:
        if t.find("td", class_="home-team"):
            return t
    log.warning(f"No fixture/result table found for {context} — page structure may have changed.")
    return None


def parse_fixtures(html: str) -> list[Fixture]:
    """Parse the Full-Time fixtures table using CSS classes.

    Each data row has 10 cells:
      [0] type          (class: color-dark-grey bold cell-divider)
      [1] date+time     (class: left cell-divider) — e.g. "22/03/2610:00"
      [2] home team     (class: home-team)
      [3] home logo     (class: team-logo) — empty text
      [4] VS / score    (class: score)
      [5] away logo     (class: team-logo) — empty text
      [6] away team     (class: road-team)
      [7] venue         (class: left cell-divider)
      [8] competition   (class: left cell-divider)
      [9] status        (class: status-notes)
    """
    soup = BeautifulSoup(html, "html.parser")
    fixtures: list[Fixture] = []

    fixture_table = _find_fixture_table(soup, "fixtures")
    if not fixture_table:
        return []

    # Parse data rows (skip header)
    for row in fixture_table.find_all("tr")[1:]:
        home_td = row.find("td", class_="home-team")
        away_td = row.find("td", class_="road-team")
        if not home_td or not away_td:
            continue

        home = clean_team_name(home_td.get_text(strip=True))
        away = clean_team_name(away_td.get_text(strip=True))
        if not home or not away:
            continue

        # Date+time: find the first cell containing a date pattern (DD/MM/YY).
        # Can't use class_="cell-divider" — the first cell-divider is the type
        # cell ("L"), not the date cell. Scanning for the pattern is more robust.
        date_str = ""
        time_str = ""
        for td in row.find_all("td"):
            cell_text = td.get_text(strip=True)
            dm = re.search(r"(\d{2}/\d{2}/\d{2})", cell_text)
            if dm:
                date_str = dm.group(1)
                tm = re.search(r"(\d{1,2}:\d{2})", cell_text)
                if tm:
                    time_str = tm.group(1)
                break

        # Venue and competition: cells after the away team
        venue = ""
        competition = ""
        for td in away_td.find_next_siblings("td"):
            classes = td.get("class", [])
            if "status-notes" in classes:
                break
            text = td.get_text(strip=True)
            if not text:
                continue
            if not venue:
                venue = text
            elif not competition:
                competition = text

        if date_str:
            fixtures.append(Fixture(
                date=date_str,
                time=time_str,
                home_team=home,
                away_team=away,
                venue=venue,
                division_label=competition or "Unknown Division",
            ))

    log.info(f"  Found {len(fixtures)} fixtures")
    return fixtures


_REDACTED_SCORE_RE = re.compile(r"\bX\s*[-–—]\s*X\b", re.IGNORECASE)
_SCORE_RE = re.compile(r"(?<![0-9-])(\d{1,2})\s*[-–—]\s*(\d{1,2})(?![0-9])")


def _parse_score(row) -> tuple[int | None, int | None] | None:
    """Extract (home_score, away_score) from a results row.

    Returns:
      (int, int)      — numeric score found
      (None, None)    — score is redacted (X-X)
      None            — no score element found at all (skip the row)

    Uses \\d{1,2} (1–2 digit numbers only) to avoid false matches on:
      - season notation like '2025-26'
      - pagination text like '1-100 of 2847'
      - ISO-style dates like '2025-11-15'
    Football scores for youth teams fit comfortably within 0-99.
    Colon is excluded as a separator to avoid matching kick-off times.
    """
    # Preferred: dedicated score element (class contains "score")
    score_el = row.find(True, class_=re.compile(r"\bscore\b"))
    if score_el:
        text = score_el.get_text(strip=True)
        if _REDACTED_SCORE_RE.search(text):
            return (None, None)
        m = _SCORE_RE.search(text)
        if m:
            return int(m.group(1)), int(m.group(2))

    # Fallback: scan all descendant elements (dash only — no colon)
    for el in row.find_all(True):
        text = el.get_text(strip=True)
        if _REDACTED_SCORE_RE.search(text):
            return (None, None)
        m = _SCORE_RE.search(text)
        if m:
            return int(m.group(1)), int(m.group(2))

    return None


def parse_results(html: str) -> list[Result]:
    """Parse the Full-Time results page.

    The results page renders rows as <div> elements with classes like
    "home-team-col" and "road-team-col".  Regex class matching picks up
    all variations (home-team, home-team-col, etc.).
    Header cells (text == "Home Team") are skipped automatically.
    """
    parser = "lxml" if _lxml_available() else "html.parser"
    log.info(f"  Parsing {len(html) // 1024}KB of results HTML with {parser} ...")
    soup = BeautifulSoup(html, parser)

    # Match any element whose class list contains a token starting with "home-team"
    home_cells = soup.find_all(True, class_=re.compile(r"\bhome-team"))
    # Strip header cells — they contain exactly the label text, not a team name
    home_cells = [
        c for c in home_cells
        if c.get_text(strip=True).lower() not in ("home team", "home", "")
    ]
    log.info(f"  home-team* elements (data rows): {len(home_cells)}")

    if not home_cells:
        idx = html.find("home-team")
        context = html[max(0, idx - 100):idx + 100] if idx != -1 else "<not found>"
        log.warning(f"  No home-team data cells found. Raw context: ...{context}...")
        return []

    first = home_cells[0]
    log.info(
        f"  First home cell: <{first.name} class={first.get('class')}> "
        f"parent=<{first.parent.name}> text={first.get_text(strip=True)!r}"
    )

    results: list[Result] = []
    seen: set[str] = set()

    _road_re = re.compile(r"\broad-team")

    for home_cell in home_cells:
        away_cell = None
        row = home_cell.parent  # default row context

        # Strategy 1: road-team is a direct sibling (flat / single-level layout).
        # find_next_sibling pairs the away cell for THIS row only.
        sib = home_cell.find_next_sibling(True, class_=_road_re)
        if sib:
            away_cell = sib
            # row stays as home_cell.parent (they share the same parent)

        if not away_cell:
            # Strategy 2: nested layout — walk up to the tightest ancestor that
            # contains exactly one road-team element (the paired one).
            candidate = home_cell.parent
            for _ in range(8):
                if candidate is None:
                    break
                road_cells = candidate.find_all(True, class_=_road_re)
                if len(road_cells) == 1:
                    away_cell = road_cells[0]
                    row = candidate
                    break
                if len(road_cells) > 1:
                    # Ancestor has multiple rows — fall back to next-in-document
                    away_cell = home_cell.find_next(True, class_=_road_re)
                    break
                candidate = getattr(candidate, "parent", None)

        if not away_cell:
            continue
        if away_cell.get_text(strip=True).lower() in ("away team", "road team", "away", ""):
            continue

        home = clean_team_name(home_cell.get_text(strip=True))
        away = clean_team_name(away_cell.get_text(strip=True))
        if not home or not away:
            continue

        # --- Score: look for a .score* sibling between home and away cells ---
        # score is a (home, away) tuple where either value may be None (redacted).
        # If score itself is None the match has no score element — skip it.
        score: tuple[int | None, int | None] | None = None
        el = home_cell.find_next_sibling(True)
        while el and el is not away_cell:
            if any("score" in c for c in el.get("class", [])):
                text = el.get_text(strip=True)
                if _REDACTED_SCORE_RE.search(text):
                    score = (None, None)
                    break
                m = _SCORE_RE.search(text)
                if m:
                    score = (int(m.group(1)), int(m.group(2)))
                    break
            el = el.find_next_sibling(True)
        if score is None:
            # Nested layout fallback: search within the row container
            score = _parse_score(row)
        if score is None:
            log.debug(f"No score for {home} v {away} — skipping (postponed?)")
            continue
        home_score, away_score = score

        # --- Date/time: look for a sibling before home_cell ---
        date_str = ""
        time_str = ""
        el = home_cell.find_previous_sibling(True)
        while el:
            text = el.get_text(strip=True)
            dm = re.search(r"(\d{2}/\d{2}/\d{2})", text)
            if dm:
                date_str = dm.group(1)
                tm = re.search(r"(\d{1,2}:\d{2})", text)
                if tm:
                    time_str = tm.group(1)
                break
            el = el.find_previous_sibling(True)
        if not date_str:
            # Nested layout fallback: scan all descendants of row
            for el in row.find_all(True):
                text = el.get_text(strip=True)
                dm = re.search(r"(\d{2}/\d{2}/\d{2})", text)
                if dm:
                    date_str = dm.group(1)
                    tm = re.search(r"(\d{1,2}:\d{2})", text)
                    if tm:
                        time_str = tm.group(1)
                    break

        # --- Venue / competition: siblings after away_cell ---
        # On the results page the competition/division label appears first,
        # followed by the venue (which may be absent).
        venue = ""
        competition = ""
        for el in away_cell.find_next_siblings():
            classes = " ".join(el.get("class", []))
            if "status" in classes:
                break
            text = el.get_text(strip=True)
            if not text:
                continue
            if not competition:
                competition = text
            elif not venue:
                venue = text
                break

        if not date_str:
            continue

        key = f"{date_str}|{home}|{away}"
        if key in seen:
            continue
        seen.add(key)

        results.append(Result(
            date=date_str,
            time=time_str,
            home_team=home,
            away_team=away,
            home_score=home_score,
            away_score=away_score,
            venue=venue,
            division_label=competition or "Unknown Division",
        ))

    log.info(f"  Found {len(results)} results")
    if results:
        r = results[0]
        hs = "X" if r.home_score is None else r.home_score
        as_ = "X" if r.away_score is None else r.away_score
        log.info(f"  Sample: {r.date} {r.home_team} {hs}-{as_} {r.away_team} [{r.division_label}]")
    return results


def _lxml_available() -> bool:
    try:
        import lxml  # noqa: F401
        return True
    except ImportError:
        return False


def clean_team_name(name: str) -> str:
    """Normalise team names for use as filenames and calendar titles.

    The Full-Time results feed sometimes prepends a season token to team
    names while the fixtures feed omits it.  Strip all known variants so
    both pages resolve to the same team name key:
      - "(25/26) Team Name"  — parenthesised leading prefix
      - "25/26 Team Name"    — bare leading prefix
      - "Team Name 25-26"    — trailing suffix (hyphen or slash separator)
    """
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"^\(\d{2,4}[/-]\d{2,4}\)\s*", "", name)  # (25/26) prefix
    name = re.sub(r"^\d{2,4}[/-]\d{2,4}\s+", "", name)       # 25/26 prefix
    name = re.sub(r"\s+\d{2,4}[/-]\d{2,4}$", "", name)        # trailing 25-26
    return name


def slug(name: str) -> str:
    """Convert a team name to a safe filename slug."""
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


# ---------------------------------------------------------------------------
# ICS generation
# ---------------------------------------------------------------------------

VCALENDAR_HEADER = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//YEL East Midlands//Fixture Scraper//EN
CALSCALE:GREGORIAN
METHOD:PUBLISH
X-WR-CALNAME:{cal_name}
X-WR-CALDESC:Fixtures for {cal_name} — YEL East Midlands
X-WR-TIMEZONE:Europe/London
"""

VCALENDAR_FOOTER = "END:VCALENDAR\n"

VEVENT_TEMPLATE = """\
BEGIN:VEVENT
UID:{uid}
DTSTAMP:{dtstamp}
DTSTART;TZID=Europe/London:{dtstart}
DTEND;TZID=Europe/London:{dtend}
SUMMARY:{summary}
DESCRIPTION:{description}
LOCATION:{location}
END:VEVENT
"""


def parse_dt(date_str: str, time_str: str) -> datetime | None:
    """Parse a Full-Time date string into a datetime. Returns None on failure."""
    # Supports DD/MM/YY and DD/MM/YYYY
    for fmt in ("%d/%m/%y", "%d/%m/%Y"):
        try:
            d = datetime.strptime(date_str.strip(), fmt)
            break
        except ValueError:
            continue
    else:
        log.warning(f"Could not parse date: '{date_str}'")
        return None

    if time_str and re.match(r"\d{1,2}:\d{2}", time_str):
        h, m = map(int, time_str.split(":"))
        return d.replace(hour=h, minute=m)
    else:
        # Default to 10:00 KO if no time listed (common for youth Sunday football)
        return d.replace(hour=10, minute=0)


def make_uid(fixture: Fixture) -> str:
    key = f"{fixture.date}|{fixture.home_team}|{fixture.away_team}"
    return hashlib.md5(key.encode()).hexdigest() + "@yel-calendar"


def fixtures_to_ics(team_name: str, fixtures: list[Fixture]) -> str:
    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [VCALENDAR_HEADER.format(cal_name=team_name)]

    for f in fixtures:
        dt_start = parse_dt(f.date, f.time)
        if not dt_start:
            continue
        dt_end = dt_start + timedelta(minutes=60)  # assume 60min slot
        is_home = f.home_team == team_name
        opponent = f.away_team if is_home else f.home_team
        home_away = "Home" if is_home else "Away"

        summary = f"{'⚽'} {team_name} vs {opponent} ({home_away})"
        description = (
            f"Division: {f.division_label}\\n"
            f"{f.home_team} v {f.away_team}\\n"
            f"KO: {f.time or 'TBC'}"
        )

        event = VEVENT_TEMPLATE.format(
            uid=make_uid(f),
            dtstamp=dtstamp,
            dtstart=dt_start.strftime("%Y%m%dT%H%M%S"),
            dtend=dt_end.strftime("%Y%m%dT%H%M%S"),
            summary=summary,
            description=description,
            location=f.venue or "",
        )
        lines.append(event)

    lines.append(VCALENDAR_FOOTER)
    return "".join(lines)


# ---------------------------------------------------------------------------
# JSON feed generation
# ---------------------------------------------------------------------------

def fixture_to_iso_date(date_str: str) -> str:
    """Convert DD/MM/YY or DD/MM/YYYY to ISO 8601 YYYY-MM-DD. Returns raw string on failure."""
    for fmt in ("%d/%m/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return date_str


def fixture_to_dict(fixture: Fixture) -> dict:
    """Serialise a Fixture to a plain dict for JSON output."""
    return {
        "id": hashlib.md5(f"{fixture.date}|{fixture.home_team}|{fixture.away_team}".encode()).hexdigest(),
        "date": fixture_to_iso_date(fixture.date),
        "time": fixture.time or "10:00",
        "home_team": fixture.home_team,
        "away_team": fixture.away_team,
        "venue": fixture.venue,
        "division": fixture.division_label,
    }


def result_to_dict(result: Result) -> dict:
    """Serialise a Result to a plain dict for JSON output."""
    return {
        "id": hashlib.md5(f"{result.date}|{result.home_team}|{result.away_team}".encode()).hexdigest(),
        "date": fixture_to_iso_date(result.date),
        "time": result.time or "10:00",
        "home_team": result.home_team,
        "away_team": result.away_team,
        "home_score": result.home_score,
        "away_score": result.away_score,
        "venue": result.venue,
        "division": result.division_label,
    }


def write_league_feed(
    league_name: str,
    league_slug: str,
    fixtures: list[Fixture],
    results: list[Result],
    generated: str,
) -> None:
    """Write fixtures.json and results.json for a league."""
    league_dir = FEEDS_DIR / league_slug
    league_dir.mkdir(parents=True, exist_ok=True)

    fixtures_payload = {
        "league": league_name,
        "generated": generated,
        "fixtures": sorted(
            [fixture_to_dict(f) for f in fixtures],
            key=lambda x: (x["date"], x["time"]),
        ),
    }
    out_f = league_dir / "fixtures.json"
    out_f.write_text(json.dumps(fixtures_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"  Written {out_f} ({len(fixtures)} fixtures)")

    results_payload = {
        "league": league_name,
        "generated": generated,
        "results": sorted(
            [result_to_dict(r) for r in results],
            key=lambda x: (x["date"], x["time"]),
            reverse=True,  # most recent first
        ),
    }
    out_r = league_dir / "results.json"
    out_r.write_text(json.dumps(results_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"  Written {out_r} ({len(results)} results)")


def write_league_teams(
    league_name: str,
    league_slug: str,
    teams: list[dict],
    generated: str,
) -> None:
    """Write teams.json for a league, listing its teams with name/slug pairs."""
    league_dir = FEEDS_DIR / league_slug
    league_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "league": league_name,
        "generated": generated,
        "teams": sorted(teams, key=lambda t: t["name"]),
    }
    out = league_dir / "teams.json"
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"  Written {out} ({len(payload['teams'])} teams)")


def write_team_feed(
    team_name: str,
    team_slug: str,
    league_name: str,
    league_slug: str,
    fixtures: list[Fixture],
    results: list[Result],
    generated: str,
) -> None:
    """Write a JSON file with fixtures and results relevant to a single team."""
    team_dir = FEEDS_DIR / league_slug / "teams"
    team_dir.mkdir(parents=True, exist_ok=True)

    team_fixtures = []
    for f in fixtures:
        is_home = f.home_team == team_name
        d = fixture_to_dict(f)
        d["home_away"] = "home" if is_home else "away"
        d["opponent"] = f.away_team if is_home else f.home_team
        team_fixtures.append(d)
    team_fixtures.sort(key=lambda x: (x["date"], x["time"]))

    team_results = []
    for r in results:
        is_home = r.home_team == team_name
        d = result_to_dict(r)
        d["home_away"] = "home" if is_home else "away"
        d["opponent"] = r.away_team if is_home else r.home_team
        d["goals_for"] = r.home_score if is_home else r.away_score
        d["goals_against"] = r.away_score if is_home else r.home_score
        team_results.append(d)
    team_results.sort(key=lambda x: (x["date"], x["time"]), reverse=True)

    payload = {
        "team": team_name,
        "league": league_name,
        "generated": generated,
        "fixtures": team_fixtures,
        "results": team_results,
    }
    out = team_dir / f"{team_slug}.json"
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


# Matches tokens that are purely punctuation/symbols (e.g. "&", "/").
# A prefix must never end on one of these — it produces a bad club name.
_PUNCT_ONLY_RE = re.compile(r"^[^a-zA-Z0-9]+$")

# All-uppercase tokens of ≥ 4 characters are treated as standalone club
# abbreviations (e.g. DLFC, ASFC, WBYFC).  Three-letter codes like AFC or
# CFC are intentionally excluded because they act as generic prefixes shared
# across many unrelated clubs (AFC Chellaston, AFC Warriors, …).
_CLUB_ABBREV_RE = re.compile(r"^[A-Z]{4,}$")
_GENERIC_PREFIX_RE = re.compile(r"^[A-Z]{3}$")

# Team designators that should be stripped from the end of team names for club grouping
# Singular colors, squad names, gender/age terms that are team-specific, not club names.
# Plural colors (e.g., "Reds", "Blues") are kept as they're often club nicknames.
_COLOR_DESIGNATORS = {"blue", "red", "green", "yellow", "black", "white", "orange", "purple", "gold", "silver"}
_SQUAD_DESIGNATORS = {"lion", "eagle", "wolf", "tiger", "panther", "dragon", "shark", "spider", "diamond",
                      "bantam", "robin", "swift", "bulldog", "rover", "colt", "warrior", "titan", "ranger",
                      "cougar", "jaguar", "cavalier", "phoenix", "rocket", "saxon", "viper", "lioness"}
_GENDER_AGE_DESIGNATORS = {"girl", "boy", "woman", "man", "lady", "youth", "junior", "reserve", 
                           "development", "academy", "senior"}
_FOOTBALL_DESIGNATORS = {"fc", "first"}
_TEAM_DESIGNATORS_SEED = _COLOR_DESIGNATORS | _SQUAD_DESIGNATORS | _GENDER_AGE_DESIGNATORS | _FOOTBALL_DESIGNATORS

# Irregular plurals mapping (plural -> singular)
_IRREGULAR_PLURALS = {"women": "woman", "men": "man", "ladies": "lady"}

# Cache for club inference results (team name -> club name)
_club_cache: dict[str, str] = {}
# Cache for computed designator tokens (set once per run)
_designator_cache: set[str] | None = None
# Cache for variable tokens only (tokens that vary across prefixes)
_variable_tokens_cache: set[str] | None = None


def _normalise_for_grouping(name: str) -> str:
    """Return a normalised copy of a team name used only for club grouping.

    Strips dots from uppercase-letter abbreviation patterns so that
    punctuation variants of the same club resolve to the same prefix key
    (e.g. 'A.C. United F.C.' and 'AC United FC' both become 'AC United FC').
    The original name is never modified — this is purely for comparison.
    """
    return re.sub(r"([A-Z])\.", r"\1", name).strip()


def _compute_designator_tokens(team_names: list[str]) -> tuple[set[str], set[str]]:
    """Compute variable designator tokens from a list of team names.
    
    Returns a tuple of (designators, variable_tokens):
    - designators: full set of lowercase designator tokens (seed + variable tokens that match seed set)
    - variable_tokens: all tokens that vary across teams with the same prefix
    Only variable tokens that match the seed set (or their singular/plural forms)
    are added to designators, protecting club name components like 'Chellaston'.
    Includes seed set and handles plurals.
    """
    # Step 1: Remove age groups and normalise
    stripped_names = []
    for name in team_names:
        norm = _normalise_for_grouping(name)
        # Remove age group tokens
        cleaned = re.sub(r"\bU\d{1,2}\b", "", norm, flags=re.IGNORECASE)
        cleaned = " ".join(cleaned.split())
        stripped_names.append(cleaned)
    
    # Step 2: Build prefix -> following token mapping
    # For each name, for each prefix length, record the token that follows (if any)
    prefix_to_next_tokens = {}
    for name in stripped_names:
        if not name:
            continue
        tokens = name.split()
        # For each position i (0 to len(tokens)), prefix is tokens[:i]
        # following token is tokens[i] if i < len(tokens), else empty string (end of name)
        for prefix_len in range(len(tokens) + 1):
            prefix = tuple(tokens[:prefix_len])
            next_token = tokens[prefix_len].lower() if prefix_len < len(tokens) else ""
            prefix_to_next_tokens.setdefault(prefix, set()).add(next_token)
    
    # Step 3: Collect variable tokens (those that appear as different options for same prefix)
    variable_tokens = set()
    for prefix, next_tokens in prefix_to_next_tokens.items():
        if len(next_tokens) >= 2:
            for token in next_tokens:
                if token:  # Skip empty string
                    variable_tokens.add(token)
    
    # Step 4: Start with seed set and add variable tokens that are designator-like
    designators = set(_TEAM_DESIGNATORS_SEED)
    # Build set of stripped team names for prefix existence check
    stripped_names_set = {name.lower() for name in stripped_names if name}
    
    # First, add variable tokens that match seed set (including singular/plural forms)
    for token in variable_tokens:
        token_lower = token.lower()
        # Check if token itself is in seed set
        if token_lower in _TEAM_DESIGNATORS_SEED:
            designators.add(token_lower)
            continue
        # Check if token is a regular plural of a seed token
        if token_lower.endswith('s'):
            singular = token_lower[:-1]
            if singular in _TEAM_DESIGNATORS_SEED:
                designators.add(token_lower)
                designators.add(singular)
                continue
        # Check if token is an irregular plural
        if token_lower in _IRREGULAR_PLURALS:
            singular = _IRREGULAR_PLURALS[token_lower]
            if singular in _TEAM_DESIGNATORS_SEED:
                designators.add(token_lower)
                designators.add(singular)
                continue
    
    # Second, add variable tokens where the prefix exists as a standalone team
    # This handles cases like "Keyworth United" where "United" varies but "Keyworth" exists
    for prefix, next_tokens in prefix_to_next_tokens.items():
        if len(next_tokens) < 2:
            continue
        # Convert prefix tuple to string
        prefix_str = " ".join(prefix).lower()
        if not prefix_str:
            continue
        # Skip prefixes that end with a generic 3-letter abbreviation (e.g., "AFC", "CFC")
        if prefix and _GENERIC_PREFIX_RE.match(prefix[-1]):
            continue
        # Skip prefixes that end with punctuation-only tokens
        if prefix and _PUNCT_ONLY_RE.match(prefix[-1]):
            continue
        # Check if prefix exists as a standalone team name
        if prefix_str in stripped_names_set:
            for token in next_tokens:
                token_lower = token.lower()
                # Skip empty tokens and purely numeric tokens
                if not token_lower or token_lower.isdigit():
                    continue
                # Skip tokens already in designators
                if token_lower in designators:
                    continue
                # Skip plural colors (e.g., "Reds", "Blues") - they're club nicknames
                if token_lower.endswith('s') and token_lower[:-1] in _COLOR_DESIGNATORS:
                    continue
                # Skip uppercase abbreviations (≥4 letters) like DLFC, ASFC
                if _CLUB_ABBREV_RE.match(token):
                    continue
                # Add the token as a designator
                designators.add(token_lower)
    
    # Add singular forms for regular plurals in seed set (already covered but safe)
    for token in list(designators):
        if token.endswith('s') and token[:-1] in _TEAM_DESIGNATORS_SEED:
            designators.add(token[:-1])
    
    # Add irregular plurals
    for plural, singular in _IRREGULAR_PLURALS.items():
        if plural in designators or singular in designators:
            designators.add(plural)
            designators.add(singular)
    
    # Ensure we don't include uppercase abbreviations (≥4 letters) as designators
    # They are protected in is_strippable
    return designators, variable_tokens


def _remove_age_group_tokens(name: str, designators: set[str] | None = None) -> str:
    """Remove age group tokens (U\\d+) and team designators from anywhere in the name."""
    # Use provided designators, cached designators, or fall back to seed set
    if designators is not None:
        designator_set = designators
        # If designators explicitly provided, treat all as variable (for testing)
        variable_token_set = designator_set
    elif _designator_cache is not None:
        designator_set = _designator_cache
        variable_token_set = _variable_tokens_cache if _variable_tokens_cache is not None else designator_set
    else:
        designator_set = _TEAM_DESIGNATORS_SEED
        variable_token_set = designator_set  # seed designators treated as variable
    
    # Remove age group tokens and collapse multiple spaces
    cleaned = re.sub(r"\bU\d{1,2}\b", "", name, flags=re.IGNORECASE)
    cleaned = " ".join(cleaned.split())
    
    # Remove team designators from the end
    words = cleaned.split()
    
    # Helper to check if a word should be stripped
    def is_strippable(word: str) -> bool:
        word_lower = word.lower()
        # Don't strip plural colors (e.g., "Reds", "Blues") - they're club nicknames
        if word_lower.endswith('s') and word_lower[:-1] in _COLOR_DESIGNATORS:
            return False
        # Don't strip uppercase abbreviations (≥4 letters) like DLFC, ASFC
        if _CLUB_ABBREV_RE.match(word):
            return False
        # Check if word (or its singular/plural form) is a designator
        if word_lower in designator_set:
            return True
        # Regular plural (ends with 's')
        if word_lower.endswith('s'):
            singular = word_lower[:-1]
            if singular in designator_set:
                return True
        # Irregular plural
        if word_lower in _IRREGULAR_PLURALS:
            singular = _IRREGULAR_PLURALS[word_lower]
            if singular in designator_set:
                return True
        return False
    
    # Helper to check if a word is a variable token (considering singular/plural forms)
    def is_variable_token(word: str) -> bool:
        word_lower = word.lower()
        if word_lower in variable_token_set:
            return True
        # Regular plural (ends with 's')
        if word_lower.endswith('s'):
            singular = word_lower[:-1]
            if singular in variable_token_set:
                return True
        # Irregular plural
        if word_lower in _IRREGULAR_PLURALS:
            singular = _IRREGULAR_PLURALS[word_lower]
            if singular in variable_token_set:
                return True
        return False
    
    # Determine if we can strip the last word
    def can_strip_last() -> bool:
        if not words or not is_strippable(words[-1]):
            return False
        # If we have more than 2 words, always allow stripping
        if len(words) > 2:
            return True
        # For 2-word names, allow stripping if:
        # 1. First word is a club abbreviation (≥4 letters), OR
        # 2. Last word is a variable token AND first word is not a generic prefix
        if len(words) == 2:
            first_word = words[0]
            last_word = words[-1]
            # Allow stripping if first word is a club abbreviation (DLFC, ASFC, etc.)
            if _CLUB_ABBREV_RE.match(first_word):
                return True
            # Do not strip if first word is a generic 3-letter abbreviation (e.g., "AFC", "CFC")
            if _GENERIC_PREFIX_RE.match(first_word):
                return False
            # Allow stripping if last word is a variable token
            if is_variable_token(last_word):
                return True
            # Otherwise do not strip (protect club names like "AC United")
            return False
        # For 1-word names, never strip (shouldn't reach here)
        return False
    
    # Strip trailing designators while allowed
    while can_strip_last():
        words.pop()
    
    return " ".join(words) if words else cleaned







def infer_club_name(team_name: str, prefix_counts: dict[str, int]) -> str:
    """Return the inferred club name for a team using pre-computed prefix counts.

    If club cache is populated (by build_prefix_counts), returns cached value.
    Otherwise falls back to longest matching prefix algorithm.
    """
    # First check cache (populated by build_prefix_counts)
    if team_name in _club_cache:
        return _club_cache[team_name]
    
    # Fallback algorithm (should only happen in tests that call infer_club_name directly)
    norm = _normalise_for_grouping(team_name)
    stripped = _remove_age_group_tokens(norm).strip()
    words = stripped.split()
    # Try longest prefix first
    for length in range(len(words), 0, -1):
        if _PUNCT_ONLY_RE.match(words[length - 1]):
            continue
        prefix = " ".join(words[:length])
        if prefix_counts.get(prefix, 0) >= 2:
            return prefix
    return stripped


def build_prefix_counts(team_names: list[str]) -> dict[str, int]:
    """Count how many team names share each word-prefix (age group stripped).

    Uses the same normalisation and filtering rules as infer_club_name so
    that prefix keys are consistent between the two functions.
    """
    # Clear previous cache
    _club_cache.clear()
    # Compute designator tokens from all team names and cache globally
    global _designator_cache, _variable_tokens_cache
    designators, variable_tokens = _compute_designator_tokens(team_names)
    _designator_cache = designators
    _variable_tokens_cache = variable_tokens
    
    # Build prefix counts with age group removal anywhere
    counts: dict[str, int] = {}
    for name in team_names:
        norm = _normalise_for_grouping(name)
        stripped = _remove_age_group_tokens(norm).strip()
        words = stripped.split()
        for length in range(1, len(words) + 1):
            # Skip prefixes that end with punctuation-only tokens
            if _PUNCT_ONLY_RE.match(words[length - 1]):
                continue
            # Skip prefixes that end with generic 3-letter abbreviations (e.g., "AFC", "CFC")
            if _GENERIC_PREFIX_RE.match(words[length - 1]):
                continue
            prefix = " ".join(words[:length])
            counts[prefix] = counts.get(prefix, 0) + 1
    
    # Compute club name for each team using longest matching prefix
    for name in team_names:
        norm = _normalise_for_grouping(name)
        stripped = _remove_age_group_tokens(norm).strip()
        words = stripped.split()
        club_name = stripped  # default fallback
        # Try longest prefix first
        for length in range(len(words), 0, -1):
            if _PUNCT_ONLY_RE.match(words[length - 1]):
                continue
            # Skip prefixes that end with generic 3-letter abbreviations (e.g., "AFC", "CFC")
            if _GENERIC_PREFIX_RE.match(words[length - 1]):
                continue
            prefix = " ".join(words[:length])
            if counts.get(prefix, 0) >= 2:
                club_name = prefix
                break
        _club_cache[name] = club_name
    
    return counts


def write_club_feed(
    club_name: str,
    club_slug: str,
    team_fixtures: list[dict],
    team_results: list[dict],
    generated: str,
) -> None:
    """Write feeds/clubs/<slug>.json aggregating all teams in a club across leagues."""
    clubs_dir = FEEDS_DIR / "clubs"
    clubs_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "club": club_name,
        "generated": generated,
        "fixtures": sorted(team_fixtures, key=lambda x: (x["date"], x["time"])),
        "results": sorted(team_results, key=lambda x: (x["date"], x["time"]), reverse=True),
    }
    out = clubs_dir / f"{club_slug}.json"
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _s3_client() -> object | None:
    """Build an R2 S3 client from environment variables, or None when not configured."""
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not (account_id and access_key and secret_key):
        return None
    import boto3
    from botocore.config import Config

    return boto3.client(
        service_name="s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            s3={"payload_signing_enabled": False},
        ),
    )


def restore_league_from_bucket(league_name: str, league_slug: str) -> int:
    """Download last-published league files from R2 back into local feeds/ and calendars/.

    Used when a league produced no data this run (e.g. a transient fetch failure) so the
    league's previously published feeds survive into the next index build and upload.
    Returns the number of files restored; 0 when there was nothing to restore.
    """
    bucket = os.environ.get("R2_BUCKET_NAME")
    s3 = _s3_client()
    if s3 is None or not bucket:
        log.warning(
            f"  {league_name}: no fresh data and R2 not configured — league left unpublished"
        )
        return 0

    restored = 0
    roots = ((FEEDS_DIR, "feeds"), (OUTPUT_DIR, "calendars"))
    for local_root, remote_root in roots:
        prefix = f"{remote_root}/{league_slug}/"
        try:
            for obj in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
                for item in obj.get("Contents", []):
                    key = item["Key"]
                    if key.endswith("/"):
                        continue
                    dest = local_root / league_slug / Path(key).relative_to(prefix)
                    if dest.exists():
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
                    dest.write_bytes(body.read())
                    restored += 1
        except Exception as e:
            log.warning(f"  {league_name}: failed to restore '{prefix}': {e}")

    if restored:
        log.warning(
            f"  {league_name}: restored {restored} file(s) from last published bucket"
        )
    return restored


def main() -> int:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total_teams = 0

    # league slug -> True when this run had to fall back to previously
    # published data instead of scraping fresh (surfaced in index.json).
    from_cache: dict[str, bool] = {}

    # Accumulate all team fixture/result dicts across leagues for club-level grouping.
    all_team_fixture_rows: list[dict] = []
    all_team_result_rows: list[dict] = []
    all_team_names: list[str] = []

    for season_id, league_name in LEAGUES:
        try:
            fixtures = fetch_fixtures(season_id, league_name)
        except Exception as e:
            log.error(f"Failed to fetch fixtures for {league_name}: {e}")
            fixtures = []

        try:
            results = fetch_results(season_id, league_name)
        except Exception as e:
            log.error(f"Failed to fetch results for {league_name}: {e}")
            results = []

        if not fixtures and not results:
            log.warning(f"No fresh data found for {league_name}")
            league_slug_name = slug(league_name)
            from_cache[league_slug_name] = True
            if restore_league_from_bucket(league_name, league_slug_name):
                log.warning(f"  {league_name}: kept previously published data")
            continue

        # Group fixtures/results by team name
        teams_fixtures: dict[str, list[Fixture]] = {}
        for f in fixtures:
            for team in (f.home_team, f.away_team):
                if team:
                    teams_fixtures.setdefault(team, []).append(f)

        teams_results: dict[str, list[Result]] = {}
        for r in results:
            for team in (r.home_team, r.away_team):
                if team:
                    teams_results.setdefault(team, []).append(r)

        all_teams = sorted(set(teams_fixtures) | set(teams_results))

        league_slug_name = slug(league_name)
        league_dir = OUTPUT_DIR / league_slug_name
        league_dir.mkdir(parents=True, exist_ok=True)

        log.info(f"  {len(all_teams)} teams, writing to {league_dir}/")

        # --- ICS calendars (fixtures only) ---
        for team_name in all_teams:
            team_fixtures = teams_fixtures.get(team_name, [])
            if team_fixtures:
                ics_content = fixtures_to_ics(team_name, team_fixtures)
                filename = league_dir / f"{slug(team_name)}.ics"
                filename.write_text(ics_content, encoding="utf-8")
                log.info(f"    {filename.name} ({len(team_fixtures)} fixtures)")

        # --- JSON feeds (league + team level) ---
        write_league_feed(league_name, league_slug_name, fixtures, results, generated)

        team_index_entries: list[dict] = []
        for team_name in all_teams:
            team_slug_name = slug(team_name)
            write_team_feed(
                team_name, team_slug_name, league_name, league_slug_name,
                teams_fixtures.get(team_name, []),
                teams_results.get(team_name, []),
                generated,
            )
            team_index_entries.append({"name": team_name, "slug": team_slug_name})

            # Collect enriched dicts for club-level aggregation
            all_team_names.append(team_name)
            for f in teams_fixtures.get(team_name, []):
                is_home = f.home_team == team_name
                d = fixture_to_dict(f)
                d["league"] = league_name
                d["team"] = team_name
                d["home_away"] = "home" if is_home else "away"
                d["opponent"] = f.away_team if is_home else f.home_team
                all_team_fixture_rows.append(d)

            for r in teams_results.get(team_name, []):
                is_home = r.home_team == team_name
                d = result_to_dict(r)
                d["league"] = league_name
                d["team"] = team_name
                d["home_away"] = "home" if is_home else "away"
                d["opponent"] = r.away_team if is_home else r.home_team
                d["goals_for"] = r.home_score if is_home else r.away_score
                d["goals_against"] = r.away_score if is_home else r.home_score
                all_team_result_rows.append(d)

        write_league_teams(league_name, league_slug_name, team_index_entries, generated)

        total_teams += len(all_teams)

    # --- JSON feeds (club level) ---
    prefix_counts = build_prefix_counts(all_team_names)

    club_fixtures: dict[str, list[dict]] = {}
    for row in all_team_fixture_rows:
        club_name = infer_club_name(row["team"], prefix_counts)
        club_fixtures.setdefault(club_name, []).append(row)

    club_results: dict[str, list[dict]] = {}
    for row in all_team_result_rows:
        club_name = infer_club_name(row["team"], prefix_counts)
        club_results.setdefault(club_name, []).append(row)

    all_clubs = sorted(set(club_fixtures) | set(club_results))

    for club_name in all_clubs:
        club_slug_name = slug(club_name)
        write_club_feed(
            club_name, club_slug_name,
            club_fixtures.get(club_name, []),
            club_results.get(club_name, []),
            generated,
        )
        teams_in_club = sorted(
            {r["team"] for r in club_fixtures.get(club_name, [])}
            | {r["team"] for r in club_results.get(club_name, [])}
        )
        log.info(f"  Club feed: {club_slug_name} ({len(teams_in_club)} teams)")

    write_index(feeds_dir=FEEDS_DIR, generated=generated, from_cache=from_cache)
    log.info(
        f"\nDone — {total_teams} team calendars, {len(all_clubs)} club feeds, "
        f"JSON feeds written across {len(LEAGUES)} leagues"
    )

    if any(from_cache.values()):
        failed = ", ".join(sorted(from_cache))
        log.error(
            f"STALE PUBLISHED DATA — no fresh scrape for: {failed}. "
            f"Previously published files were re-published for these leagues."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
