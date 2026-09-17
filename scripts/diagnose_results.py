#!/usr/bin/env python3
"""Find where results can actually be scraped from.

Full-Time answers /results/1/100000.html with a Cloudflare challenge — for a
plain fetch and for headless Chromium alike — while /fixtures/1/100000.html
serves 1000+ rows to the same client seconds earlier.  So results have to come
from somewhere else.  Two candidates, both checked here:

  1. The fixtures page already carries a score cell (class "score") that
     parse_fixtures ignores.  If played matches show a score there, the page
     that already works is the whole answer.
  2. The block may be about the URL rather than the path — a smaller page size,
     or a different route.  Each variant is tried once, and its status reported.

Run where the scraper runs:

    docker compose run --rm -v "$PWD/scripts:/app/scripts" \\
        scraper python scripts/diagnose_results.py
"""

import argparse
import collections
import pathlib
import re
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scraper"))

import scrape  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402
from curl_cffi import requests as curl_requests  # noqa: E402

DEFAULT_SEASON = "918978398"  # Euro Soccer Nottinghamshire Senior League 26/27
_DATE_RE = re.compile(r"(\d{2}/\d{2}/\d{2})")


def _try_once(session, url: str) -> str:
    """Fetch once, reporting the status rather than raising and retrying."""
    try:
        resp = session.get(url, timeout=45)
    except Exception as e:
        return f"FAILED {e!r}"
    body = f"{len(resp.text):,} bytes"
    if "attention required" in resp.text[:4000].lower():
        body += ", Cloudflare challenge"
    elif resp.text.count("home-team"):
        body += f", {resp.text.count('home-team')}x home-team"
    return f"HTTP {resp.status_code}, {body}"


def probe_results_urls(season: str) -> None:
    """Is the block about the path, the page size, or the client?"""
    print("\n=== results URL variants (one attempt each) ===")
    query = f"?selectedSeason={season}&selectedFixtureGroupKey="
    variants = [
        ("current: /results/1/100000", f"https://fulltime.thefa.com/results/1/100000.html{query}"),
        ("smaller page: /results/1/500", f"https://fulltime.thefa.com/results/1/500.html{query}"),
        ("smaller page: /results/1/50", f"https://fulltime.thefa.com/results/1/50.html{query}"),
        ("no page segment: /results.html", f"https://fulltime.thefa.com/results.html{query}"),
        ("control: /fixtures/1/50", f"https://fulltime.thefa.com/fixtures/1/50.html{query}"),
    ]

    for label, url in variants:
        with curl_requests.Session(impersonate="chrome") as session:
            session.headers.update(scrape.BROWSER_HEADERS)
            print(f"  {label:32} {_try_once(session, url)}")

    # Every fetch in scrape.py opens a fresh session, so no cookie Full-Time
    # sets on the way in is ever sent back. Does browsing in first help?
    print("\n=== warmed session (homepage, then fixtures, then results) ===")
    with curl_requests.Session(impersonate="chrome") as session:
        session.headers.update(scrape.BROWSER_HEADERS)
        print(f"  homepage                         {_try_once(session, 'https://fulltime.thefa.com/')}")
        print(f"  fixtures                         {_try_once(session, f'https://fulltime.thefa.com/fixtures/1/100000.html{query}')}")
        session.headers.update({
            "Referer": f"https://fulltime.thefa.com/fixtures/1/100000.html{query}",
            "Sec-Fetch-Site": "same-origin",
        })
        print(f"  results (same session + referer)  {_try_once(session, f'https://fulltime.thefa.com/results/1/100000.html{query}')}")


def probe_fixture_scores(season: str, out_dir: pathlib.Path) -> None:
    """Do played matches carry a score on the fixtures page?"""
    print("\n=== score cells on the fixtures page ===")
    url = f"{scrape.FIXTURES_URL}?selectedSeason={season}&selectedFixtureGroupKey="
    try:
        html = scrape._fetch_page(url, "fixtures")
    except Exception as e:
        print(f"  fixtures fetch FAILED: {e!r}")
        return

    dest = out_dir / "fixtures.html"
    dest.write_text(html, encoding="utf-8")
    print(f"  saved {dest} ({len(html):,} bytes)")

    soup = BeautifulSoup(html, "lxml" if scrape._lxml_available() else "html.parser")
    table = scrape._find_fixture_table(soup, "fixtures")
    if table is None:
        print("  no fixture table found")
        return

    today = datetime.now()
    texts = collections.Counter()
    past_with_score = []
    past_rows = future_rows = 0

    for row in table.find_all("tr")[1:]:
        home = row.find("td", class_="home-team")
        away = row.find("td", class_="road-team")
        if not home or not away:
            continue

        date_str = ""
        for td in row.find_all("td"):
            m = _DATE_RE.search(td.get_text(strip=True))
            if m:
                date_str = m.group(1)
                break
        if not date_str:
            continue
        try:
            played = datetime.strptime(date_str, "%d/%m/%y") < today
        except ValueError:
            continue

        score_cell = row.find("td", class_=re.compile(r"\bscore"))
        text = score_cell.get_text(strip=True) if score_cell else "<no score cell>"

        if played:
            past_rows += 1
            texts[text] += 1
            if re.search(r"\d+\s*[-–—]\s*\d+", text) and len(past_with_score) < 8:
                past_with_score.append(
                    (date_str, home.get_text(strip=True), text, away.get_text(strip=True))
                )
        else:
            future_rows += 1

    print(f"  rows: {past_rows} dated before today, {future_rows} upcoming")
    print("  score cell contents on past-dated rows:")
    for text, count in texts.most_common(10):
        print(f"    {count:5}x  {text!r}")

    if past_with_score:
        print("\n  played matches carrying a score:")
        for date_str, home, score, away in past_with_score:
            print(f"    {date_str}  {home}  {score}  {away}")
        print("\n  => results can be parsed from the fixtures page.")
    else:
        print("\n  => no scores on the fixtures page; results must come from elsewhere.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", default=DEFAULT_SEASON, help="selectedSeason ID")
    ap.add_argument("--out", default="/tmp/fulltime-diagnosis", help="where to save pages")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    probe_fixture_scores(args.season, out_dir)
    probe_results_urls(args.season)
    return 0


if __name__ == "__main__":
    sys.exit(main())
