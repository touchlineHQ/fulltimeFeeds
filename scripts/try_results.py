#!/usr/bin/env python3
"""Fetch one league's results through the production path, and report.

A full run scrapes thousands of fixtures across seven leagues before it says
whether results worked. This calls the same fetch_results, with the same
BrowserSession, for one league — so what it proves is what the next real run
will do, not an approximation of it.

    docker compose run --rm -v "$PWD/scripts:/app/scripts" \\
        scraper python scripts/try_results.py

Any configured league by name fragment or season id:

    ... python scripts/try_results.py --league veterans
    ... python scripts/try_results.py --season 918978398
"""

import argparse
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scraper"))

import scrape  # noqa: E402


def pick(league: str | None, season: str | None) -> tuple[str, str]:
    if season:
        for season_id, name in scrape.LEAGUES:
            if season_id == season:
                return season_id, name
        return season, f"season {season}"
    if league:
        matches = [
            (sid, name) for sid, name in scrape.LEAGUES
            if league.lower() in name.lower()
        ]
        if not matches:
            names = "\n  ".join(name for _, name in scrape.LEAGUES)
            raise SystemExit(f"No league matching {league!r}. Configured:\n  {names}")
        return matches[0]
    # Default to a senior league: it has played matches by now, so an empty
    # result means refused rather than "nothing to report yet".
    return scrape.LEAGUES[2]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--league", help="match a configured league by name fragment")
    ap.add_argument("--season", help="a selectedSeason id")
    ap.add_argument("--no-browser", action="store_true",
                    help="plain fetch only, to see what happens without a browser")
    args = ap.parse_args()

    logging.getLogger().setLevel(logging.INFO)
    season_id, league_name = pick(args.league, args.season)
    print(f"league: {league_name}")
    print(f"url:    {scrape.RESULTS_URL}?selectedSeason={season_id}"
          f"&selectedFixtureGroupKey=\n")

    # Must go through _results_browser(), not construct one: RESULTS_SESSION
    # picks the fetcher, and building BrowserSession here silently ignored it.
    browser = None if args.no_browser else scrape._results_browser()
    try:
        results = scrape.fetch_results(season_id, league_name, browser=browser)
    except scrape.ResultsUnavailable as e:
        print(f"\nREFUSED: {e}")
        print("\nThe feeds would carry results_unavailable for this league.")
        return 1
    finally:
        if browser:
            used = getattr(browser, "fetches", 0)
            browser.close()
            print(f"\n(browser served {used} page(s) this run)")

    print(f"\nparsed {len(results)} result(s)")
    for r in results[:5]:
        home = "X" if r.home_score is None else r.home_score
        away = "X" if r.away_score is None else r.away_score
        print(f"  {r.date} {r.home_team} {home}-{away} {r.away_team}  [{r.division_label}]")
    if not results:
        print("  (the page answered but held no rows — this league may not have played)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
