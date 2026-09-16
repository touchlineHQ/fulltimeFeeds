#!/usr/bin/env python3
"""Try the results URLs Full-Time's own pages link to.

The scraper has only ever asked for /results/1/100000.html with a season and an
empty fixture-group key, and every shape of that is refused.  But the fixtures
page links to results itself, with parameters we never send:

    /results.html?league=...&selectedSeason=...&selectedDivision=...
                 &selectedCompetition=0&selectedFixtureGroupKey=1_...

A rule that refuses an unscoped, whole-season results request while allowing the
per-division link a visitor clicks would explain the 403 exactly.  This tries
those links verbatim, then drops one parameter at a time from the first that
answers, so the parameter that matters is identified rather than guessed.

It also tries a displayFixture.html page — one per match, 9000+ of them linked
from the fixtures page — to see whether a played match carries its score there.

    docker compose run --rm -v "$PWD/scripts:/app/scripts" \\
        scraper python scripts/probe_real_results.py
"""

import argparse
import pathlib
import re
import sys
from urllib.parse import urlencode, urljoin, urlparse, parse_qsl

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scraper"))

import scrape  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402
from curl_cffi import requests as curl_requests  # noqa: E402

BASE = "https://fulltime.thefa.com/"
SCORE_RE = re.compile(r"\b\d{1,2}\s*[-–—]\s*\d{1,2}\b")


def fetch(url: str) -> tuple[int | None, str]:
    with curl_requests.Session(impersonate="chrome") as session:
        session.headers.update(scrape.BROWSER_HEADERS)
        session.headers.update({
            "Referer": f"{scrape.FIXTURES_URL}",
            "Sec-Fetch-Site": "same-origin",
        })
        try:
            resp = session.get(url, timeout=45)
        except Exception as e:
            print(f"    FAILED {e!r}")
            return None, ""
    return resp.status_code, resp.text


def describe(status: int | None, html: str) -> str:
    if status is None:
        return "no response"
    if "attention required" in html[:4000].lower():
        return f"HTTP {status}, {len(html):,} bytes, Cloudflare challenge"
    note = f"HTTP {status}, {len(html):,} bytes, {html.count('home-team')}x home-team"
    if status == 200 and html.count("home-team"):
        rows = scrape.parse_results(html)
        note += f", parse_results -> {len(rows)} result(s)"
        if rows:
            r = rows[0]
            note += f" e.g. {r.date} {r.home_team} {r.home_score}-{r.away_score} {r.away_team}"
    return note


def results_links(html: str) -> list[str]:
    """Every distinct results URL the page links to, richest first."""
    soup = BeautifulSoup(html, "lxml" if scrape._lxml_available() else "html.parser")
    seen: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(BASE, a["href"].strip())
        parsed = urlparse(url)
        if "fulltime.thefa.com" not in parsed.netloc or "result" not in parsed.path.lower():
            continue
        seen.setdefault(urlencode(sorted(parse_qsl(parsed.query))), url)
    return sorted(seen.values(), key=lambda u: -len(parse_qsl(urlparse(u).query)))


def fixture_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml" if scrape._lxml_available() else "html.parser")
    seen: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(BASE, a["href"].strip())
        if "displayFixture" in url:
            seen.setdefault(urlparse(url).query, url)
    return list(seen.values())


def ablate(url: str) -> None:
    """Drop one parameter at a time, to find which one the block turns on."""
    params = parse_qsl(urlparse(url).query)
    base = url.split("?")[0]
    print(f"\n=== dropping one parameter at a time from {base} ===")
    for name, _ in params:
        reduced = [(k, v) for k, v in params if k != name]
        status, html = fetch(f"{base}?{urlencode(reduced)}")
        print(f"  without {name:28} {describe(status, html)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--page", default="/tmp/fulltime-diagnosis/fixtures.html")
    ap.add_argument("--season", default="918978398")
    ap.add_argument("--probe", type=int, default=4, help="how many results links to try")
    args = ap.parse_args()

    page = pathlib.Path(args.page)
    if page.is_file():
        html = page.read_text(encoding="utf-8", errors="replace")
    else:
        url = f"{scrape.FIXTURES_URL}?selectedSeason={args.season}&selectedFixtureGroupKey="
        print("Fetching the fixtures page ...")
        try:
            html = scrape._fetch_page(url, "fixtures")
        except Exception as e:
            print(f"  FAILED: {e!r}")
            return 1
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(html, encoding="utf-8")

    links = results_links(html)
    print(f"=== {len(links)} distinct results link(s) on the fixtures page ===")
    print("    (that count is also how many requests a full scrape would cost)")

    answered: str | None = None
    for url in links[: args.probe]:
        print(f"\n  {url}")
        status, body = fetch(url)
        note = describe(status, body)
        print(f"    {note}")
        if status == 200 and "challenge" not in note and answered is None:
            answered = url
            pathlib.Path(page.parent / "results-answered.html").write_text(body, encoding="utf-8")
            print(f"    saved {page.parent / 'results-answered.html'}")

    if answered:
        ablate(answered)
    else:
        print("\n  none of the site's own results links answered either.")

    fixtures = fixture_links(html)
    print(f"\n=== displayFixture.html — {len(fixtures)} distinct match page(s) linked ===")
    for url in fixtures[:2]:
        print(f"  {url}")
        status, body = fetch(url)
        print(f"    {describe(status, body)}")
        if status == 200 and "attention required" not in body[:4000].lower():
            text = BeautifulSoup(body, "html.parser").get_text(" ", strip=True)
            found = SCORE_RE.findall(text)
            print(f"    score-shaped values: {found[:6] or 'none'}")
            pathlib.Path(page.parent / "displayFixture.html").write_text(body, encoding="utf-8")

    return 0


if __name__ == "__main__":
    sys.exit(main())
