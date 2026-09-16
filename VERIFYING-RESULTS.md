# Verifying the results scrape

Published feeds had an empty `results` array and only a handful of
participation records, so a season well under way read as one that had never
started. See **Getting the results** in [README.md](README.md#getting-the-results)
for what the scraper now does; this file is how to confirm it works against the
live site.

Three faults were found, and the fix layers a fallback for each because the
first two could not be told apart without reaching Full-Time:

| Fault | Symptom |
|---|---|
| The results page was asked for without a date filter, so Full-Time answered for a single period — often one with no matches in it | `results` empty |
| `_fetch_page_js` (Playwright) was never called by anything, so a client-side-rendered results table came back as a page with no rows and no error | `results` empty |
| A match played but not yet moved off the fixture list was never read as played — at U11 and below some leagues never move it at all | `participation` thin, fixtures lingering all season |

A fourth, found while testing: `parse_results` read the two cells after the away
team as competition then venue, but Full-Time's table orders them venue then
competition, so every result came out with the ground in `division`.

## Running it

```bash
pip install curl-cffi beautifulsoup4 lxml playwright
playwright install chromium
DEBUG=1 python scraper/scrape.py 2>&1 | tee /tmp/scrape.log
```

Run it this way rather than through Docker for a first look: `docker compose up
--build` matches production but ends in `upload.py`, which publishes to R2.
Running the scraper directly writes to `./feeds/` and `./calendars/` and
publishes nothing, so the output can be inspected before anything goes live.

## Reading the log

```bash
grep -E "results via|no results|NO RESULTS SCRAPED|already played" /tmp/scrape.log
```

| Line | What it means |
|---|---|
| `N results via whole-season URL` | The missing `selectedDateCode=all` was the whole problem |
| `N results via no date filter` | The season needs asking for the old way — worth knowing, it means the two URLs are not interchangeable |
| `N results via browser render` | The results page is client-side, and the dead `_fetch_page_js` was the problem |
| `no results found by any method` | Neither theory was right — see below |
| `Found N fixtures (M already played)` | `M` is the count of played matches recovered from the fixture list, which is what was feeding the thin participation arrays |
| `NO RESULTS SCRAPED for: …` | That league published fixtures but no results at all |

`NO RESULTS SCRAPED` is legitimate in pre-season, which is why it does not
change the exit code. Mid-season it means something is still wrong.

## Checking the feeds

With results coming through, `feeds/clubs/east-leake.json` should show:

- a non-empty `results` array, with `goals_for` / `goals_against` on each row;
- `participation` entries for the U11-and-below sides going back to the start of
  the season, each carrying `date`, `team`, `age_group`, `division`,
  `home_away`, `league` and `played: true` — and **no** `opponent`, `venue` or
  score;
- `compliance.results_withheld` above 0;
- no fixture dated before `generated` that has actually been played.

```bash
python - <<'PY'
import json, pathlib
feed = json.loads(pathlib.Path("feeds/clubs/east-leake.json").read_text())
print("generated    ", feed["generated"])
print("fixtures     ", len(feed["fixtures"]))
print("results      ", len(feed["results"]))
print("participation", len(feed["participation"]))
print("withheld     ", feed["compliance"]["results_withheld"])
today = feed["generated"][:10]
stale = [f for f in feed["fixtures"] if f["date"] < today]
print("fixtures still listed in the past:", len(stale))
PY
```

Spot-check one restricted record by hand — nothing in it should identify the
opposition, the venue, or how the match went:

```bash
python -c "import json;print(json.dumps(json.load(open('feeds/clubs/east-leake.json'))['participation'][:2],indent=2))"
```

## If results are still empty

The `DEBUG=1` run logs the page size and table count for every attempt, and
`_fetch_page_js` logs every XHR/fetch the page makes. That output says whether
Full-Time served a page with no rows in it, or a page whose rows arrive by some
other request:

```bash
grep -E "bytes,|XHR/fetch|No fixture/result rows" /tmp/scrape.log
```

Capture the raw HTML for whichever league is failing and the parser can be
matched to it:

```bash
python - <<'PY'
import sys; sys.path.insert(0, "scraper")
from scrape import _fetch_page, _fetch_page_js, _results_url
season = "918978398"  # Euro Soccer Nottinghamshire Senior League 26/27
open("/tmp/results-static.html", "w").write(_fetch_page(_results_url(season), "probe"))
open("/tmp/results-rendered.html", "w").write(_fetch_page_js(_results_url(season), "probe"))
PY
grep -c "home-team" /tmp/results-static.html /tmp/results-rendered.html
```

A `home-team` count of 0 in the static file and non-zero in the rendered one
confirms the page is client-side. Both at 0 means the URL itself is wrong —
open the league's results page in a browser, copy the query string Full-Time
actually uses, and compare it against `_results_url` in `scraper/scrape.py`.

## Tests

The parsing and fallback behaviour is covered without touching the network:

```bash
python -m pytest tests -q
```
