#!/usr/bin/env python3
"""Report exactly what Full-Time's results page returns for one league.

Every league publishes an empty `results` array, and the fix for it cannot be
verified from a sandbox — Full-Time answers a datacentre IP with a Cloudflare
challenge.  Run this where the scraper actually runs:

    docker compose run --rm scraper python scripts/diagnose_results.py

It fetches the results page both ways scrape.py does — a plain HTTP fetch and a
headless browser render — saves each page, and reports what each one held.  The
rendered page's own filter controls are dumped too: if Full-Time is waiting on a
filter the scraper never sets, the control that does it is named there.

Defaults to the Euro Soccer senior league, which has played matches by now.
"""

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scraper"))

import scrape  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

DEFAULT_SEASON = "918978398"  # Euro Soccer Nottinghamshire Senior League 26/27


def summarise(label: str, html: str, out_dir: pathlib.Path) -> None:
    print(f"\n=== {label} ===")
    if not html:
        print("  (nothing returned)")
        return

    dest = out_dir / f"{label.replace(' ', '-')}.html"
    dest.write_text(html, encoding="utf-8")
    print(f"  saved            {dest}  ({len(html):,} bytes)")

    for marker in ("home-team", "road-team", "<table", "<tr"):
        print(f"  {marker!r:18} {html.count(marker)}")

    lowered = html.lower()
    for phrase in ("attention required", "cf-browser-verification", "just a moment",
                   "no results", "no fixtures", "select a", "sign in"):
        if phrase in lowered:
            print(f"  page says        {phrase!r}")

    try:
        parsed = scrape.parse_results(html)
    except Exception as e:                      # a parser crash is a finding too
        print(f"  parse_results    raised {e!r}")
        return
    print(f"  parse_results    {len(parsed)} result(s)")
    for r in parsed[:3]:
        print(f"                   {r.date} {r.home_team} {r.home_score}-{r.away_score} {r.away_team}")


def dump_controls(html: str) -> None:
    """List the page's own filter controls, with whatever each is set to.

    Full-Time drives its results table from these; one it expects and the
    scraper never sets would explain a table that renders empty.
    """
    print("\n=== filter controls on the rendered page ===")
    soup = BeautifulSoup(html, "html.parser")

    for form in soup.find_all("form"):
        print(f"  <form action={form.get('action')!r} method={form.get('method')!r}>")

    selects = soup.find_all("select")
    if not selects:
        print("  (no <select> elements — the page may not have rendered at all)")
    for sel in selects:
        options = sel.find_all("option")
        chosen = next(
            (o.get_text(strip=True) for o in options if o.has_attr("selected")), None
        )
        shown = ", ".join(o.get_text(strip=True) for o in options[:4])
        print(f"  select name={sel.get('name')!r} selected={chosen!r}")
        print(f"         {len(options)} option(s): {shown}{' ...' if len(options) > 4 else ''}")

    hidden = soup.find_all("input", {"type": "hidden"})
    for inp in hidden[:15]:
        print(f"  hidden name={inp.get('name')!r} value={inp.get('value')!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", default=DEFAULT_SEASON, help="selectedSeason ID")
    ap.add_argument("--out", default="/tmp/fulltime-diagnosis", help="where to save the pages")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    url = f"{scrape.RESULTS_URL}?selectedSeason={args.season}&selectedFixtureGroupKey="
    print(f"results URL      {url}")
    print(f"fixtures URL     {scrape.FIXTURES_URL}?selectedSeason={args.season}"
          f"&selectedFixtureGroupKey=")

    # The fixtures page is the control: it works today, so whatever differs
    # between the two is the problem.
    try:
        fixtures_html = scrape._fetch_page(
            f"{scrape.FIXTURES_URL}?selectedSeason={args.season}&selectedFixtureGroupKey=",
            "fixtures",
        )
        print(f"\nfixtures page    {len(fixtures_html):,} bytes, "
              f"{len(scrape.parse_fixtures(fixtures_html))} fixture(s) parsed")
    except Exception as e:
        print(f"\nfixtures page    FAILED: {e!r}")

    try:
        static_html = scrape._fetch_page(url, "results-static")
    except Exception as e:
        print(f"\nstatic fetch     FAILED: {e!r}")
        static_html = ""
    summarise("results static", static_html, out_dir)

    rendered = scrape._fetch_page_js(url, "results-rendered")
    summarise("results rendered", rendered, out_dir)

    if rendered:
        dump_controls(rendered)

    print(f"\nPages saved under {out_dir} — attach whichever one has no rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
