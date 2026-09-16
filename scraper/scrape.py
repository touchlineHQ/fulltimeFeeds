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

from compliance import (
    compliance_meta,
    is_restricted,
    is_row_restricted,
    redact_fixture,
    restricted_record_id,
    played_fixtures,
    safe_fixtures,
    safe_results,
    split_results,
)
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
    ("204486042", "Nottinghamshire Football League Saturday Youth 26/27"),
    ("134665924", "Nottinghamshire Football League Sunday Youth 26/27"),
    ("71450136", "East Midlands Veterans League 26/27"),
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


def _fixtures_url(season_id: str) -> str:
    """Full-Time's fixture list for a whole season."""
    return f"{FIXTURES_URL}?selectedSeason={season_id}&selectedFixtureGroupKey="


def _results_url(season_id: str, date_code: str = "all") -> str:
    """Full-Time's results page for a whole season.

    The results page filters by date.  Asked for without a
    ``selectedDateCode`` it answers for one period only — often one that has
    no matches in it — which is how a season full of played matches comes back
    as an empty results list.  ``all`` asks for the season.
    """
    params = [f"selectedSeason={season_id}"]
    if date_code:
        params.append(f"selectedDateCode={date_code}")
    params += [
        "selectedFixtureGroupAgeGroupId=",
        "selectedFixtureGroupKey=",
        "selectedRelation=",
        "selectedClub=",
        "selectedTeam=",
    ]
    return f"{RESULTS_URL}?" + "&".join(params)


def fetch_fixtures(season_id: str, league_name: str) -> tuple[list[Fixture], list[Result]]:
    """Fetch a season's fixture list, split into (still to come, already played).

    Full-Time leaves a played match on the fixture list until its league
    enters the result, and at U11 and below — where no result is ever
    published — some leagues never do.  Such a row still carries a score
    ("3 - 1", or "X - X" where the score is withheld), so it comes back as a
    result rather than being advertised as an upcoming match for the rest of
    the season.
    """
    url = _fixtures_url(season_id)
    label = f"fixtures/{league_name}"
    log.info(f"Fetching fixtures for {league_name} ...")

    fixtures, played = parse_fixtures_page(_fetch_page(url, label))
    if not fixtures and not played:
        log.warning(f"  {league_name}: no fixture rows in the static page — retrying in a browser")
        fixtures, played = parse_fixtures_page(_fetch_page_js(url, label))

    log.info(f"  Found {len(fixtures)} fixtures ({len(played)} already played)")
    return fixtures, played


def fetch_results(season_id: str, league_name: str) -> list[Result]:
    """Fetch a season's results, trying each way the page will give them up.

    Cheapest first: the whole-season URL fetched statically, then the bare URL
    without a date filter, then a real browser.  Full-Time renders some pages
    client-side, and a static fetch of one of those is not an error — it is a
    page with no match rows in it at all, which is indistinguishable from a
    league that has not played yet unless something else is tried.
    """
    label = f"results/{league_name}"
    log.info(f"Fetching results for {league_name} ...")

    attempts = (
        ("whole-season URL", True, lambda: _fetch_page(_results_url(season_id), label)),
        ("no date filter", True, lambda: _fetch_page(_results_url(season_id, date_code=""), label)),
        ("browser render", False, lambda: _fetch_page_js(_results_url(season_id), label)),
    )

    static_fetch_failed = False
    for description, is_static, fetch in attempts:
        # A static fetch that failed outright failed at the transport — the WAF,
        # or the network. Asking the same way again with a different query
        # string only leans on a host that is already refusing us.
        if is_static and static_fetch_failed:
            log.debug(f"  {label}: skipping {description} — a static fetch already failed")
            continue
        try:
            html = fetch()
        except Exception as e:
            static_fetch_failed = static_fetch_failed or is_static
            log.warning(f"  {label}: {description} failed: {e}")
            continue
        log.debug(f"  {description}: {len(html)} bytes, {html.count('<table')} tables")
        results = parse_results(html)
        if results:
            log.info(f"  {league_name}: {len(results)} results via {description}")
            return results
        log.warning(f"  {league_name}: no results via {description}")

    log.error(f"  {league_name}: no results found by any method")
    return []


def merge_results(from_results_page: list[Result], from_fixture_list: list[Result]) -> list[Result]:
    """Combine result rows from both pages, keeping one row per match.

    The results page is authoritative — a row there has a confirmed score — so
    rows recovered from the fixture list only fill matches it does not cover.
    """
    merged = list(from_results_page)
    seen = {(r.date, r.home_team, r.away_team) for r in merged}
    for result in from_fixture_list:
        key = (result.date, result.home_team, result.away_team)
        if key in seen:
            continue
        seen.add(key)
        merged.append(result)
    return merged


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# Full-Time names the two team cells "home-team" and "road-team", sometimes
# with a suffix ("home-team-col"), so both are matched on their prefix.
_HOME_CELL_RE = re.compile(r"\bhome-team")
_ROAD_CELL_RE = re.compile(r"\broad-team")

# A header cell carries the column's label where a data row carries a team.
_HEADER_LABELS = {"", "home team", "home", "away team", "road team", "away"}

# Cells that name what they hold. The table layout labels neither, and is read
# positionally instead — see _venue_and_competition.
_VENUE_CLASS_RE = re.compile(r"venue|location|ground", re.IGNORECASE)
_COMPETITION_CLASS_RE = re.compile(r"competition|division|league", re.IGNORECASE)


def _find_fixture_table(soup: BeautifulSoup, context: str, warn: bool = True) -> object | None:
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
        if t.find("td", class_=_HOME_CELL_RE):
            return t
    if warn:
        log.warning(f"No fixture/result table found for {context} — page structure may have changed.")
    return None


def _is_header_cell(cell) -> bool:
    """True when a cell holds a column label rather than a team name."""
    return cell.get_text(strip=True).lower() in _HEADER_LABELS


def _venue_and_competition(cells) -> tuple[str, str]:
    """Split the cells following the away team into (venue, competition).

    Full-Time's table labels neither cell, and orders them venue then
    competition; the div layout labels both and orders them the other way
    round.  Labelled cells are therefore read by name and the rest
    positionally, so the division lands in `division` either way — which
    matters beyond tidiness, because the division label is where a match's
    age group is often the only thing that names it.
    """
    venue = ""
    competition = ""
    unlabelled: list[str] = []

    for cell in cells:
        classes = " ".join(cell.get("class", []))
        if "status" in classes.lower():
            break
        text = cell.get_text(strip=True)
        if not text:
            continue
        if _VENUE_CLASS_RE.search(classes):
            venue = venue or text
        elif _COMPETITION_CLASS_RE.search(classes):
            competition = competition or text
        else:
            unlabelled.append(text)

    for text in unlabelled:
        if not venue:
            venue = text
        elif not competition:
            competition = text
        else:
            break

    return venue, competition


def _trailing_cells(row, away_cell) -> list:
    """The cells after the away team, which is where venue and competition sit.

    They are siblings of the away cell in the table layout.  Where a layout
    wraps the team name in something, the away cell has no siblings of its own
    and the wrapper's are read instead, so a nested row still gets a division
    rather than "Unknown Division".
    """
    cell = away_cell
    while cell is not None and cell is not row:
        siblings = cell.find_next_siblings()
        if siblings:
            return siblings
        cell = cell.parent
    return []


def _row_date_time(row, home_cell) -> tuple[str, str]:
    """Return (DD/MM/YY, HH:MM) for a match row, either part possibly empty.

    The date sits before the home team, in a cell that may also hold the
    kick-off time ("22/03/2610:00").  Cells before the home team are read
    first so a date elsewhere in the row cannot win; the whole row is only
    scanned when that finds nothing, for layouts that nest the two.
    """
    def _read(text: str) -> tuple[str, str] | None:
        dm = re.search(r"(\d{2}/\d{2}/\d{2})", text)
        if not dm:
            return None
        tm = re.search(r"(\d{1,2}:\d{2})", text)
        return dm.group(1), tm.group(1) if tm else ""

    cell = home_cell.find_previous_sibling(True)
    while cell is not None:
        found = _read(cell.get_text(strip=True))
        if found:
            return found
        cell = cell.find_previous_sibling(True)

    for cell in row.find_all(True):
        found = _read(cell.get_text(strip=True))
        if found:
            return found

    return "", ""


_REDACTED_SCORE_RE = re.compile(r"\bX\s*[-–—]\s*X\b", re.IGNORECASE)
_SCORE_RE = re.compile(r"(?<![0-9-])(\d{1,2})\s*[-–—]\s*(\d{1,2})(?![0-9])")


def _read_score(text: str) -> tuple[int | None, int | None] | None:
    """Read a score out of a cell's text.

    Returns (int, int) for a score, (None, None) where it is withheld as
    "X - X", and None where the text holds no score at all ("VS" on a match
    still to be played).
    """
    if _REDACTED_SCORE_RE.search(text):
        return (None, None)
    m = _SCORE_RE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _cell_score(home_cell, away_cell) -> tuple[int | None, int | None] | None:
    """Read the score from the dedicated score cell between the two teams.

    Only a cell that says it is the score counts, so nothing else in the row —
    a venue with a number in it, a season in a competition name — can be read
    as one.
    """
    cell = home_cell.find_next_sibling(True)
    while cell is not None and cell is not away_cell:
        if any("score" in c for c in cell.get("class", [])):
            score = _read_score(cell.get_text(strip=True))
            if score is not None:
                return score
        cell = cell.find_next_sibling(True)
    return None


def _row_score(row, home_cell, away_cell) -> tuple[int | None, int | None] | None:
    """Extract (home_score, away_score) from a match row.

    Returns:
      (int, int)      — numeric score found
      (None, None)    — score is redacted (X-X)
      None            — no score anywhere in the row (skip it — postponed, or
                        a result the league has not entered)

    Read in narrowing order of confidence: the score cell between the two
    teams, then any cell in the row that says it is the score, then anything
    at all sitting between the two teams.  The row is never searched beyond
    that, because the cells outside it hold a venue and a competition name —
    "Pitch 3-4" and "Div 1 (2025-26)" both read as scores otherwise, and an
    upcoming fixture would vanish from the calendar as a match already played.

    _SCORE_RE uses \\d{1,2} (1–2 digit numbers only) to avoid false matches on:
      - season notation like '2025-26'
      - pagination text like '1-100 of 2847'
      - ISO-style dates like '2025-11-15'
    Football scores for youth teams fit comfortably within 0-99.
    Colon is excluded as a separator to avoid matching kick-off times.
    """
    # Preferred: the dedicated score cell between the two team cells.
    score = _cell_score(home_cell, away_cell)
    if score is not None:
        return score

    # Nested layouts put it elsewhere in the row, but still name it.
    score_el = row.find(True, class_=re.compile(r"\bscore\b"))
    if score_el is not None:
        score = _read_score(score_el.get_text(strip=True))
        if score is not None:
            return score

    # Last resort: anything between the two teams, named or not. The walk is
    # bounded by the row so a page of two thousand matches is not re-scanned
    # from each row to its end.
    between = False
    for el in row.find_all(True):
        if el is away_cell:
            break
        if el is home_cell:
            between = True
            continue
        if not between:
            continue
        score = _read_score(el.get_text(strip=True))
        if score is not None:
            return score

    return None


def _iter_match_cells(soup: BeautifulSoup, context: str):
    """Yield (row, home_cell, away_cell) for every match on a Full-Time page.

    Both pages normally render one <tr> per match, with the away cell a
    sibling of the home cell.  The results page has also been seen rendering
    each match as nested divs, where it is not, so each home cell is paired
    with its own away cell by walking outwards to the tightest element that
    contains exactly one.
    """
    table = _find_fixture_table(soup, context, warn=False)
    if table is not None:
        for row in table.find_all("tr"):
            home_cell = row.find(True, class_=_HOME_CELL_RE)
            away_cell = row.find(True, class_=_ROAD_CELL_RE)
            if not home_cell or not away_cell:
                continue
            if _is_header_cell(home_cell) or _is_header_cell(away_cell):
                continue
            yield row, home_cell, away_cell
        return

    home_cells = [
        cell for cell in soup.find_all(True, class_=_HOME_CELL_RE)
        if not _is_header_cell(cell)
    ]
    if not home_cells:
        log.warning(f"No fixture/result rows found for {context} — page structure may have changed.")
        return

    first = home_cells[0]
    log.debug(
        f"  First home cell: <{first.name} class={first.get('class')}> "
        f"parent=<{first.parent.name}> text={first.get_text(strip=True)!r}"
    )

    for home_cell in home_cells:
        row = home_cell.parent
        away_cell = home_cell.find_next_sibling(True, class_=_ROAD_CELL_RE)

        if not away_cell:
            candidate = home_cell.parent
            for _ in range(8):
                if candidate is None:
                    break
                road_cells = candidate.find_all(True, class_=_ROAD_CELL_RE)
                if len(road_cells) == 1:
                    away_cell = road_cells[0]
                    row = candidate
                    break
                if len(road_cells) > 1:
                    # Ancestor has multiple rows — fall back to next-in-document
                    away_cell = home_cell.find_next(True, class_=_ROAD_CELL_RE)
                    break
                candidate = getattr(candidate, "parent", None)

        if not away_cell or _is_header_cell(away_cell):
            continue
        yield row, home_cell, away_cell


def parse_fixtures_page(html: str) -> tuple[list[Fixture], list[Result]]:
    """Parse the Full-Time fixtures table into (still to come, already played).

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

    Cell [4] is what separates the two lists: "VS" on a match still to be
    played, a score on one the league has not moved to its results page yet.
    Only that cell is read, so nothing else in the row can be mistaken for a
    score and drop a real fixture out of the calendar.
    """
    soup = BeautifulSoup(html, "html.parser")
    fixtures: list[Fixture] = []
    played: list[Result] = []

    for row, home_cell, away_cell in _iter_match_cells(soup, "fixtures"):
        home = clean_team_name(home_cell.get_text(strip=True))
        away = clean_team_name(away_cell.get_text(strip=True))
        if not home or not away:
            continue

        date_str, time_str = _row_date_time(row, home_cell)
        if not date_str:
            continue

        venue, competition = _venue_and_competition(_trailing_cells(row, away_cell))
        score = _cell_score(home_cell, away_cell)

        if score is None:
            fixtures.append(Fixture(
                date=date_str,
                time=time_str,
                home_team=home,
                away_team=away,
                venue=venue,
                division_label=competition or "Unknown Division",
            ))
        else:
            played.append(Result(
                date=date_str,
                time=time_str,
                home_team=home,
                away_team=away,
                home_score=score[0],
                away_score=score[1],
                venue=venue,
                division_label=competition or "Unknown Division",
            ))

    return fixtures, played


def parse_fixtures(html: str) -> list[Fixture]:
    """Parse the Full-Time fixtures table, returning matches still to be played."""
    fixtures, _played = parse_fixtures_page(html)
    log.info(f"  Found {len(fixtures)} fixtures")
    return fixtures


def parse_results(html: str) -> list[Result]:
    """Parse the Full-Time results page.

    Normally the same table as the fixtures page; where it renders as nested
    divs instead ("home-team-col" and friends) the rows are walked apart
    instead.  Rows with no score at all are matches that were never played —
    postponed, or awaiting entry — and are left out.
    """
    parser = "lxml" if _lxml_available() else "html.parser"
    log.info(f"  Parsing {len(html) // 1024}KB of results HTML with {parser} ...")
    soup = BeautifulSoup(html, parser)

    results: list[Result] = []
    seen: set[str] = set()

    for row, home_cell, away_cell in _iter_match_cells(soup, "results"):
        home = clean_team_name(home_cell.get_text(strip=True))
        away = clean_team_name(away_cell.get_text(strip=True))
        if not home or not away:
            continue

        score = _row_score(row, home_cell, away_cell)
        if score is None:
            log.debug(f"No score for {home} v {away} — skipping (postponed?)")
            continue

        date_str, time_str = _row_date_time(row, home_cell)
        if not date_str:
            continue

        key = f"{date_str}|{home}|{away}"
        if key in seen:
            continue
        seen.add(key)

        venue, competition = _venue_and_competition(_trailing_cells(row, away_cell))
        results.append(Result(
            date=date_str,
            time=time_str,
            home_team=home,
            away_team=away,
            home_score=score[0],
            away_score=score[1],
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


def make_restricted_uid(
    fixture: Fixture,
    team_name: str,
    home_away: str,
) -> str:
    """Build a calendar UID without using the restricted opposition name."""
    public_event = {
        "date": fixture.date,
        "time": fixture.time or "10:00",
        "home_away": home_away.lower(),
        "division": fixture.division_label,
    }
    return (
        restricted_record_id(public_event, team_name, "calendar")
        + "@yel-calendar"
    )


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

        # Calendars are published on public URLs like everything else, so a
        # U11-or-below event carries only the club's own team, the kick-off and
        # whether it is home or away — no opposition, no venue.
        restricted = is_restricted(
            team_name, f.home_team, f.away_team, f.division_label
        )
        if restricted:
            uid = make_restricted_uid(f, team_name, home_away)
            summary = f"{'⚽'} {team_name} ({home_away})"
            description = (
                f"Division: {f.division_label}\\n"
                f"KO: {f.time or 'TBC'}\\n"
                f"Opposition and venue are not published at U11 and below."
            )
            location = ""
        else:
            uid = make_uid(f)
            summary = f"{'⚽'} {team_name} vs {opponent} ({home_away})"
            description = (
                f"Division: {f.division_label}\\n"
                f"{f.home_team} v {f.away_team}\\n"
                f"KO: {f.time or 'TBC'}"
            )
            location = f.venue or ""

        event = VEVENT_TEMPLATE.format(
            uid=uid,
            dtstamp=dtstamp,
            dtstart=dt_start.strftime("%Y%m%dT%H%M%S"),
            dtend=dt_end.strftime("%Y%m%dT%H%M%S"),
            summary=summary,
            description=description,
            location=location,
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
    """Write fixtures.json and results.json for a league.

    League-wide listings have no subject club: every row names two teams to
    each other, so a U11-or-below match cannot appear here without identifying
    someone's opposition.  Those matches are withheld from the league feed
    entirely and counted in `withheld`; they are still published, redacted, in
    the team and club feeds where a subject team is known.
    """
    league_dir = FEEDS_DIR / league_slug
    league_dir.mkdir(parents=True, exist_ok=True)

    all_fixture_rows = [fixture_to_dict(f) for f in fixtures]
    open_fixture_rows = [row for row in all_fixture_rows if not is_row_restricted(row)]
    fixtures_withheld = len(all_fixture_rows) - len(open_fixture_rows)

    fixtures_payload = {
        "league": league_name,
        "generated": generated,
        "compliance": {**compliance_meta(), "fixtures_withheld": fixtures_withheld},
        "fixtures": sorted(open_fixture_rows, key=lambda x: (x["date"], x["time"])),
    }
    out_f = league_dir / "fixtures.json"
    out_f.write_text(json.dumps(fixtures_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(
        f"  Written {out_f} ({len(open_fixture_rows)} fixtures, "
        f"{fixtures_withheld} withheld at U11 and below)"
    )

    open_result_rows, results_withheld = safe_results([result_to_dict(r) for r in results])
    results_payload = {
        "league": league_name,
        "generated": generated,
        "compliance": compliance_meta(results_withheld),
        "results": sorted(
            open_result_rows,
            key=lambda x: (x["date"], x["time"]),
            reverse=True,  # most recent first
        ),
    }
    out_r = league_dir / "results.json"
    out_r.write_text(json.dumps(results_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(
        f"  Written {out_r} ({len(open_result_rows)} results, "
        f"{results_withheld} withheld at U11 and below)"
    )


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
    """Write a JSON file with fixtures and results relevant to a single team.

    At U11 and below the team's own name stays — a club may name its own side —
    but the opposition and venue are removed from each fixture and the results
    array is left empty.  Those matches reappear in `participation`, which says
    only that they were played, so a site can still list the team's season.
    """
    team_dir = FEEDS_DIR / league_slug / "teams"
    team_dir.mkdir(parents=True, exist_ok=True)

    team_fixtures = []
    for f in fixtures:
        is_home = f.home_team == team_name
        d = fixture_to_dict(f)
        d["home_away"] = "home" if is_home else "away"
        d["opponent"] = f.away_team if is_home else f.home_team
        team_fixtures.append(d)
    team_results = []
    for r in results:
        is_home = r.home_team == team_name
        d = result_to_dict(r)
        # `team` names the subject of the row. A participation record keeps it
        # and drops both team names, so without it a consumer cannot tell which
        # side was ours and has to anonymise the match completely. `league`
        # rides along for the same reason: a participation record built here
        # would otherwise be the only one in a feed without it.
        d["team"] = team_name
        d["league"] = league_name
        d["home_away"] = "home" if is_home else "away"
        d["opponent"] = r.away_team if is_home else r.home_team
        d["goals_for"] = r.home_score if is_home else r.away_score
        d["goals_against"] = r.away_score if is_home else r.home_score
        team_results.append(d)
    team_results, team_participation = split_results(team_results)
    results_withheld = len(team_participation)

    # A restricted fixture the league never moved onto its results page was
    # still played once its date has passed; record it rather than leave it
    # sitting in the fixture list for the rest of the season.
    team_fixtures, played = played_fixtures(
        team_fixtures,
        generated[:10],
        subject_team=team_name,
        league=league_name,
        existing_ids={record["id"] for record in team_participation},
    )
    team_participation.extend(played)
    team_fixtures = safe_fixtures(team_fixtures, subject_team=team_name)
    team_fixtures.sort(key=lambda x: (x["date"], x["time"]))

    team_results.sort(key=lambda x: (x["date"], x["time"]), reverse=True)
    team_participation.sort(key=lambda x: (x["date"], x["time"]), reverse=True)

    payload = {
        "team": team_name,
        "league": league_name,
        "generated": generated,
        "compliance": compliance_meta(results_withheld),
        "fixtures": team_fixtures,
        "results": team_results,
        "participation": team_participation,
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
    """Write feeds/clubs/<slug>.json aggregating all teams in a club across leagues.

    Every row here is already scoped to one of the club's own teams (it carries
    `team` and `home_away`), so restricted fixtures keep that team's name and
    lose only the opposition and venue.  Restricted results are dropped, and
    reappear in `participation` as a record that the match was played.
    """
    clubs_dir = FEEDS_DIR / "clubs"
    clubs_dir.mkdir(parents=True, exist_ok=True)

    safe_club_results, club_participation = split_results(team_results)
    results_withheld = len(club_participation)

    # As in write_team_feed: a played restricted fixture with no results row
    # becomes a participation record instead of lingering as a fixture.
    club_fixtures, played = played_fixtures(
        team_fixtures,
        generated[:10],
        existing_ids={record["id"] for record in club_participation},
    )
    club_participation.extend(played)
    safe_club_fixtures = [
        redact_fixture(row, row.get("team")) if is_row_restricted(row) else row
        for row in club_fixtures
    ]

    payload = {
        "club": club_name,
        "generated": generated,
        "compliance": compliance_meta(results_withheld),
        "fixtures": sorted(safe_club_fixtures, key=lambda x: (x["date"], x["time"])),
        "results": sorted(safe_club_results, key=lambda x: (x["date"], x["time"]), reverse=True),
        "participation": sorted(club_participation, key=lambda x: (x["date"], x["time"]), reverse=True),
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

    # Leagues that gave up a fixture list but no results at all. Almost always
    # a scraping fault rather than a league that has genuinely played nothing,
    # and silent until it is said out loud: the feeds still publish, just with
    # an empty results array and a season that looks like it never started.
    leagues_without_results: list[str] = []

    for season_id, league_name in LEAGUES:
        try:
            fixtures, played = fetch_fixtures(season_id, league_name)
        except Exception as e:
            log.error(f"Failed to fetch fixtures for {league_name}: {e}")
            fixtures, played = [], []

        try:
            results = fetch_results(season_id, league_name)
        except Exception as e:
            log.error(f"Failed to fetch results for {league_name}: {e}")
            results = []

        # Matches the league left on its fixture list with a score against them
        # count as results too — see fetch_fixtures.
        results = merge_results(results, played)

        if fixtures and not results:
            log.error(
                f"  {league_name}: {len(fixtures)} fixtures but no results — "
                f"results.json and participation will be empty for this league"
            )
            leagues_without_results.append(league_name)

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

    if leagues_without_results:
        missing = ", ".join(leagues_without_results)
        log.error(
            f"NO RESULTS SCRAPED for: {missing}. Their fixtures published, but "
            f"results.json is empty and no participation records were built — "
            f"check the results page for those leagues."
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
