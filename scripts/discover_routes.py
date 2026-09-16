#!/usr/bin/env python3
"""Mine the fixtures page for routes that might carry results.

/results is blocked by a rule scoped to that path — every page size, the bare
route, and a warmed session all get the same Cloudflare challenge, while
/fixtures serves 200 one request apart.  Before giving up on results, it is
worth knowing what other routes Full-Time actually exposes: the fixtures page
already fetched (2.6MB, saved by diagnose_results.py) links to them.

This reads that saved page — no new fetch needed for the discovery part — lists
the distinct routes it links to, then tries the few that plausibly carry scores
(team pages, match details, calendar or export endpoints) to see which answer.

    docker compose run --rm -v "$PWD/scripts:/app/scripts" \\
        scraper python scripts/discover_routes.py
"""

import argparse
import collections
import pathlib
import re
import sys
from urllib.parse import urljoin, urlparse, parse_qs

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scraper"))

import scrape  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402
from curl_cffi import requests as curl_requests  # noqa: E402

BASE = "https://fulltime.thefa.com/"
# Routes worth trying: anything that might list a played match with its score.
INTERESTING = re.compile(
    r"team|match|ical|\.ics|calendar|export|print|rss|feed|csv|report|fixture",
    re.IGNORECASE,
)
SCORE_RE = re.compile(r"\b\d{1,2}\s*[-–—]\s*\d{1,2}\b")
_NUMERIC_SEGMENT = re.compile(r"/\d+")


def routes_in(html: str) -> tuple[collections.Counter, dict[str, str]]:
    """Distinct link routes in the page, and one example URL for each."""
    soup = BeautifulSoup(html, "lxml" if scrape._lxml_available() else "html.parser")
    counts: collections.Counter = collections.Counter()
    examples: dict[str, str] = {}

    for a in soup.find_all(["a", "link"], href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = urljoin(BASE, href)
        parsed = urlparse(url)
        if "thefa.com" not in parsed.netloc:
            continue
        # Collapse /fixtures/1/100000.html -> /fixtures/<n>/<n>.html
        path = _NUMERIC_SEGMENT.sub("/<n>", parsed.path)
        route = parsed.netloc + path
        counts[route] += 1
        examples.setdefault(route, url)

    return counts, examples


def probe(url: str) -> str:
    with curl_requests.Session(impersonate="chrome") as session:
        session.headers.update(scrape.BROWSER_HEADERS)
        try:
            resp = session.get(url, timeout=45)
        except Exception as e:
            return f"FAILED {e!r}"

    text = resp.text
    note = f"HTTP {resp.status_code}, {len(text):,} bytes"
    if "attention required" in text[:4000].lower():
        return note + ", Cloudflare challenge"
    if resp.status_code == 200:
        scores = SCORE_RE.findall(BeautifulSoup(text, "html.parser").get_text(" ", strip=True))
        note += f", {text.count('home-team')}x home-team, {len(scores)} score-shaped values"
        if scores[:5]:
            note += f" e.g. {scores[:5]}"
    return note


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--page", default="/tmp/fulltime-diagnosis/fixtures.html",
                    help="the saved fixtures page to mine")
    ap.add_argument("--probe", type=int, default=8, help="how many routes to try")
    args = ap.parse_args()

    page = pathlib.Path(args.page)
    if not page.is_file():
        print(f"No saved page at {page} — run diagnose_results.py first.")
        return 1

    html = page.read_text(encoding="utf-8", errors="replace")
    counts, examples = routes_in(html)

    print(f"=== routes linked from {page.name} ({len(counts)} distinct) ===")
    for route, count in counts.most_common(40):
        mark = " *" if INTERESTING.search(route) else ""
        print(f"  {count:5}x  {route}{mark}")

    # Does the page link to results at all, and in what form?
    print("\n=== how the page refers to results ===")
    for route, url in examples.items():
        if "result" in route.lower():
            print(f"  {route}\n    {url}")
    soup = BeautifulSoup(html, "html.parser")
    for form in soup.find_all("form"):
        action = form.get("action") or ""
        if "result" in action.lower():
            print(f"  <form action={action!r} method={form.get('method')!r}>")

    candidates = [
        (route, url) for route, url in examples.items()
        if INTERESTING.search(route) and "/results" not in route
    ][: args.probe]

    print(f"\n=== probing {len(candidates)} candidate route(s) ===")
    for route, url in candidates:
        qs = parse_qs(urlparse(url).query)
        print(f"  {route}")
        print(f"    params: {sorted(qs)}")
        print(f"    {probe(url)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
