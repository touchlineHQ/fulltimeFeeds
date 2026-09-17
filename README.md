# Fulltime Feeds — Fixture Calendars & JSON Feeds

Automatically scrapes fixture data from [FA Full-Time](https://fulltime.thefa.com) and generates:

- **`.ics` calendar files** — one per team, for calendar app subscriptions
- **JSON feeds** — league, team, and club level, for embedding in static club websites

Updated daily by a local cron job.

## Subscribing to a team calendar

1. Find your team's `.ics` file under `calendars/<league>/`
2. Copy its URL (see below)
3. In Google Calendar: **+ Other calendars → From URL** → paste the URL

The calendar will auto-refresh (Google typically polls every 12–24 hours).

> **Tip:** The calendar URL looks like:
> `https://fixtures.touchlinehq.co.uk/calendars/yel-east-midlands-sunday-25-26/eastwood-athletic-atalanta-u10.ics`

## JSON feeds

All feeds are published under `feeds/` and can be fetched as R2 URLs. They update daily alongside the calendars.

### Feed structure

| File | Contents |
|---|---|
| `feeds/index.json` | All leagues and clubs, each with their slugs |
| `feeds/<league>/fixtures.json`, `results.json`, `teams.json` | League-level feeds |
| `feeds/<league>/teams/<slug>.json` | One team's fixtures, results and participation |
| `feeds/clubs/<slug>.json` | A club's teams across all leagues |

`feeds/index.json` is an envelope with a `generated` timestamp, a `leagues`
array, and a `clubs` array:

```json
{
  "generated": "2026-08-20T15:36:10Z",
  "from_cache": false,
  "leagues": [
    {
      "name": "Euro Soccer Nottinghamshire Senior League 26/27",
      "slug": "euro-soccer-nottinghamshire-senior-league-26-27",
      "teams": [
        { "name": "Arnold Town", "slug": "arnold-town" }
      ]
    }
  ],
  "clubs": [
    { "name": "Arnold Town", "slug": "arnold-town", "league": "Euro Soccer Nottinghamshire Senior League 26/27" }
  ]
}
```

### Staleness flag

If a league could not be scraped on a run (e.g. Full-Time blocking), its last
published files are restored and re-uploaded, and the affected league entry —
plus the top-level envelope — gets `"from_cache": true`. Consumers can check
`index.json.from_cache` / `league.from_cache` to detect stale data. Absent or
`false` means everything was freshly scraped. The scraper also exits non-zero
in that case so cron reports the failure.

### Club feeds

A club feed aggregates all teams belonging to the same club — including teams across different leagues (e.g. a club with both Saturday U14 and Sunday younger-age-group teams). Club names are inferred automatically by finding the shortest name prefix shared by two or more teams:

- `Arnold Town Blue U11` + `Arnold Town Maroon U12` → club `Arnold Town`
- `Alfreton Town Cobras U11` + `Alfreton Town All Stars U13` → club `Alfreton Town`
- `Attenborough Colts Black U10` + `Attenborough Colts Spartans U14` → club `Attenborough Colts`

### Object shapes

Fixture (in `fixtures.json` and `fixtures` arrays):

```json
{
  "id": "481ab77102acbc3a91144ddcffc10c26",
  "date": "2026-03-22",
  "time": "10:00",
  "home_team": "Arnold Town Blue U13",
  "away_team": "Opponent FC U13",
  "venue": "The Ground",
  "division": "U13 Division 1"
}
```

Result (in `results.json` and `results` arrays):

```json
{
  "id": "...",
  "date": "2026-03-15",
  "time": "10:00",
  "home_team": "Arnold Town Blue U13",
  "away_team": "Opponent FC U13",
  "home_score": 3,
  "away_score": 1,
  "venue": "The Ground",
  "division": "U13 Division 1"
}
```

Team and club feeds additionally include `league`, `team`, `home_away` (`"home"` or `"away"`), `opponent`, and (results only) `goals_for` and `goals_against`.

### One match, one row

Fixtures and results are scraped from two pages and grouped per team, so the
same match can arrive more than once. Feeds settle each match into exactly one
place:

- **A match that has a result is not also a fixture.** Full-Time keeps a played
  match on its fixtures page while the league enters the score, and after that
  in some competitions. Once the score is published the match appears in
  `results` only.
- **A club derby is listed once.** When both teams belong to the same club, the
  match is scraped for each side; the club feed keeps the home side's row —
  which already names both teams the right way round — and marks it
  `"derby": true`. The team feeds still carry a row each.
- **Participation stays per team.** A derby at U11 or below produces a
  participation record for each of the club's teams: both of them played.

`id` is stable across the fixtures and results pages (it is derived from the
date and both team names), so consumers can key on it.

### Publication rules for U11 and below

The FA prohibits publishing match results and league tables for teams playing at
Under-11 and below, and the league's guidance extends that to naming the
opposition or the venue in anything published online. Every file this project
writes lands on a public URL, so the rules are applied when the data is
serialised — see `scraper/compliance.py`. Consumers get feeds that are already
safe to render.

For a match at U11 or below:

| | Published |
|---|---|
| Date, kick-off time, division | ✅ |
| The team's own name, home or away | ✅ in team and club feeds |
| Opposition name | ❌ replaced with `"Opposition"` |
| Venue | ❌ emptied |
| Score / result | ❌ withheld from `results` entirely |
| League table | ❌ never generated |
| That the match was played | ✅ as a participation record — see below |

U12 to U18 and adult teams are published in full.

Age groups are read from the team names and the division label (`U10`,
`Under 10`, `U10 Division 1`, …). A match with no age token anywhere is treated
as adult football; where tokens disagree, the youngest wins, so a U12 side
playing a U11 cup tie is still protected.

Restricted fixture rows carry `"publication_restricted": true`. League fixture
and result feeds, plus team and club feeds, also carry a `compliance` block:

```json
{
  "compliance": {
    "policy": "Results and league tables are not published for teams at U11 and below, ...",
    "restricted_max_age_group": "U11",
    "results_withheld": 3
  }
}
```

Two consequences worth knowing about:

- **League feeds withhold restricted matches entirely.** A league-wide listing
  has no subject club — every row would name two teams to each other — so
  `<league>/fixtures.json` and `<league>/results.json` omit U11-and-below
  matches and report the count in `compliance.fixtures_withheld` /
  `compliance.results_withheld`. Use the team or club feed to get those
  fixtures, redacted.
- **Calendars follow the same rules.** A U11-and-below `.ics` event is titled
  `⚽ <Team> (Home)` with no opposition and an empty `LOCATION`.
- **Withheld results come back as participation records.** Team and club feeds
  carry a `participation` array saying that the match was played, when, and
  whether it was home or away:

  ```json
  { "date": "2026-09-06", "team": "Arnold Town Blue U11", "age_group": "U11",
    "home_away": "away", "division": "U11 Division 1", "played": true }
  ```

  Withholding a result is not the same as pretending the match never happened.
  Consumers should **list** these matches with the score, opposition and venue
  omitted, rather than hide them — a young team whose season simply vanishes
  tells a parent nothing and reads as a bug. They are absent from league feeds,
  where adjacent same-date records would let you pair two teams back into a
  fixture.

Scores are still scraped and are still submitted privately through the league's
own system where required — this only governs what is published.

### Using a feed on a static site

```js
const url = 'https://fixtures.touchlinehq.co.uk/feeds/clubs/arnold-town.json';
const { club, fixtures } = await fetch(url).then(r => r.json());
```

Use `feeds/index.json` to discover available league, team, and club slugs, and browse `feeds/clubs/` for club feeds.

`feeds/clubs/` is cross-league output, not a league, and is excluded from the
league scan that builds `index.json`.

## How it works

The scraper fetches all fixtures from Full-Time's fixtures page (`/fixtures/1/100000.html`) for each configured league season. All age groups within each league are included automatically — new teams and divisions appear as Full-Time updates.

### Results are currently unavailable

Full-Time serves the fixtures listing to the scraper and refuses every page that
carries a score. Measured against the live site, one request apart, in the same
session:

| Route | Response |
|---|---|
| `/fixtures/1/100000.html` | 200, 1030 matches |
| `/results/1/100000.html` (any page size, bare `/results.html`, warmed session) | 403 Cloudflare challenge |
| `/results.html` with the site's own `league`/`selectedDivision`/`selectedFixtureGroupKey` | 403 Cloudflare challenge |
| `/displayFixture.html?id=…` (single match page) | 403 Cloudflare challenge |

Headless Chromium receives the same challenge page, so rendering does not help —
it only costs a browser launch per league per run. `scripts/diagnose_results.py`,
`scripts/discover_routes.py` and `scripts/probe_real_results.py` reproduce the
above if the situation changes.

So `fetch_results` raises `ResultsUnavailable` when the page is refused, and
every feed for an affected league carries `"results_unavailable": true` beside
its `generated` timestamp:

```json
{
  "club": "East Leake",
  "generated": "2026-09-16T18:27:36Z",
  "results_unavailable": true,
  "results": []
}
```

**An empty `results` array on such a feed means the data was withheld from the
scraper, not that no match was played** — render "results unavailable" rather
than an empty results section, which reads as a broken page. The flag is absent
whenever results were reachable, so absent or `false` means the array is real.
`participation` is affected the same way: a U11-and-below match that never
appears on a reachable page cannot be recorded as played, so participation
covers only restricted fixtures whose date has passed while they sit on the
fixtures page.

Every Playwright mode is challenged too — headless, `--headless=new`, headed,
and headed with a persistent profile — because Playwright exposes
`navigator.webdriver` and CDP whatever the window mode. A browser you drive
yourself reads those pages fine, so the scraper can borrow that session rather
than pretend to be something it is not:

```bash
# from the browser you already read the results page in: DevTools ->
# Application -> Cookies, or Network -> any request -> Request Headers
export FULLTIME_COOKIE="cf_clearance=...; JSESSIONID=..."
export FULLTIME_USER_AGENT="Mozilla/5.0 (...) Chrome/... Safari/537.36"
```

A clearance cookie is bound to the IP and User-Agent that earned it, so run the
scraper on the same machine, set the User-Agent to match the browser exactly,
and refresh the cookie when Cloudflare expires it. Feeds carry
`results_unavailable` on any run where it has expired, so a stale cookie shows
up as a flagged feed rather than silently empty results.

What every refused mode has in common is that Playwright launched the browser,
and a launched browser says so — measured on the same binary:

| | `navigator.webdriver` |
|---|---|
| Playwright launched it | `true` |
| Started directly, attached over CDP | `false` |

So `fetch_results` does the same when a plain fetch is refused: it starts the
bundled Chromium directly, attaches over CDP, and reads the page through it.
Nothing about the browser is disguised — it is a stock binary that simply was
not launched by an automation framework.

The plain fetch is always tried first, because it is cheap and succeeds outright
whenever the client is not being challenged (with `FULLTIME_COOKIE` set, for
instance). The browser session starts lazily and is reused across every league
in a run, so a run whose fetches all succeed never launches one:

```
INFO Fetching results for Euro Soccer ... 
INFO   results/Euro Soccer: refused a plain fetch — retrying through the browser
INFO   browser: started Xvfb on :99
INFO   browser: attached
INFO   results/Euro Soccer: 143 result(s) via the browser
```

Set `RESULTS_BROWSER=0` to keep a run to plain fetches only. If the browser is
challenged too, `ResultsUnavailable` is raised as before and the feeds carry
`results_unavailable`.

Measured against the live site, that bundled browser is challenged too. What
that leaves is narrower than it looks, because the same image loads the page
perfectly when a person drives it over VNC: same container, same binary, same
virtual display, same public IP as a phone that also loads it. So the
container's rendering, its font set and its address are all ruled out.

The one difference between the run that works and the run that does not is that
the automated one launches with `--remote-debugging-port` and attaches over CDP.
Enabling the debug protocol is itself observable, and that is what the remaining
detection keys on. Closing it means defeating the detection rather than working
with it, which this project does not do — so results are unavailable to an
unattended run, and the feeds say so rather than publishing an empty array.

### A browser on the server

The scraper runs headless, so there is no browser on it to open a page in or
use developer tools with. `scripts/vnc_browser.sh` starts one on a virtual
display and serves it over VNC, so it can be driven from a laptop or phone on
the same network:

```bash
echo 'VNC_PASSWORD=something' >> .env
docker compose run --rm --service-ports scraper scripts/vnc_browser.sh
```

Then open `http://<this-host>:6080/vnc.html` in any browser, a phone's included.
The session comes up with a window manager, a terminal, and browser tabs already
open on the pages worth looking at, so nothing has to be typed into a remote
browser from a phone.
Port 5900 is there as well for a native VNC client, but it speaks RFB rather
than HTTP — a browser pointed straight at it sees nothing at all.

The profile lives in the `scraper_state` volume, so whatever is done in it
persists between runs. A password is required: this puts a browser on your
network, and an unauthenticated one is a browser anybody on that network can
drive. Bind it to localhost and tunnel over SSH if the network is not one you
control.


### Saving results by hand

The results pages load perfectly in a browser driven by hand — that is the whole
shape of the problem — so a page saved from one is ordinary HTML the parser is
happy with, obtained the way anyone reading the site obtains it. Nothing
expires, nothing is replayed, and no detection is involved.

```bash
docker compose run --rm --service-ports scraper scripts/vnc_browser.sh
```

That opens one tab per configured league and saves each one to
`/app/state/results` (the `scraper_state` volume, also `RESULTS_HTML_DIR`) as it
finishes loading. Clear anything Full-Time asks of you in a tab and it is picked
up on the next pass; the console says which league landed and how many results
it held:

```
  Euro Soccer Nottinghamshire Senior League 26/27: saved 918978398.html (143 result(s))
  East Midlands Veterans League 26/27: still showing a challenge — solve it in the browser
```

`scripts/save_open_tabs.py` does that, and can be run on its own from the
terminal in the VNC session. It reads the pages already on screen and writes
them out — it never navigates, reloads or opens anything, because every page it
saves was fetched by the person driving the browser. Reading a rendered page
makes no request, which is why this works where driving the browser does not.

It talks to the browser's debug port directly rather than through Playwright.
`connect_over_cdp` adopts the whole browser, attaching to every target and
waiting for each to answer, so a single tab sitting on a challenge stalls the
connection to all of them. Listing tabs over HTTP and reading each through its
own websocket means an unresponsive tab costs only its own five second timeout.

Set `VNC_AUTOSAVE=0` to turn it off and save tabs by hand with `Ctrl+S` instead;
filenames do not matter then, since each page names its own season in the links
it renders.

The scraper uses a saved page only when a live fetch and the browser have both
failed, so live data always wins when it is available. The run summary says
which league came from where and how old each saved page is, and a page over a
week old is called out:

```
Results per league:
  Euro Soccer Nottinghamshire Senior 26/27    143 via a saved page (2.1 days old)
  East Midlands Veterans League 26/27         REFUSED — published as unavailable
```

Results only change after matches are played, so a weekly pass is usually
enough.

### Running nodriver for results

`scraper/nodriver_session.py` is an optional fetcher, off unless named:

```bash
# .env
RESULTS_SESSION=nodriver_session:Session
```

```bash
docker compose run --rm -v "$PWD/scripts:/app/scripts" \
    scraper python scripts/try_results.py       # one league, the real path
docker compose up --build                       # a full run
```

nodriver drives Chrome without the attached-CDP pattern the challenge detects,
which is the one difference between the run that works by hand and the run that
does not. It needs a display, and starts Xvfb itself if there is none.

Two things come with that choice. Automated access is likely contrary to
Full-Time's terms, even though the data is public and readable by hand from the
same machine — that call belongs to whoever runs this. And it is an arms race:
expect it to stop working after a Cloudflare or Chrome update, without notice.
When it does, the league is published as `results_unavailable` and the feed says
so rather than quietly going empty.

### Supplying your own results fetcher

If you want to make that call yourself, `RESULTS_SESSION` takes any fetcher,
so it lives in your module rather than a fork of this one:

```bash
export RESULTS_SESSION="my_fetcher:Session"
```

```python
# my_fetcher.py
class Session:
    def fetch(self, url, wait_selector=None) -> str:
        """Return the page's HTML, or an interstitial if refused."""

    def close(self) -> None:
        """Release what the session holds. Always called, even on a crash."""
```

An optional `fetches` counter is reported in the run summary if present.
Anything raised from `fetch()` publishes that league as `results_unavailable`,
exactly as a refusal from the bundled session does — so a fetcher that stops
working degrades into a flagged feed rather than a silent one.

`scripts/try_results.py` exercises one league through the real path, which is
the quickest way to test a fetcher without a full run.


`scripts/probe_browser_modes.py` re-checks which automated modes are accepted,
and on a desktop `./scripts/start_browser.sh` starts a browser with a debugging
port for the probe to attach to (`--stop` when finished).

Each fixture row provides the date, time, home/away teams, venue, and competition (division) name. The scraper generates a `.ics` file and JSON feed per team, plus club-level and league-level JSON feeds, all organised under `calendars/` and `feeds/`.

Currently configured leagues:

| League | Season ID |
|---|---|
| YEL East Midlands Sunday 25/26 | `909330396` |
| YEL East Midlands Saturday 25/26 | `161954265` |
| Euro Soccer Nottinghamshire Senior League 25/26 | `355008724` |
| Nottinghamshire Girls and Ladies Football League 25/26 | `258824685` |

## Updating for a new season

At the start of each season, update the `LEAGUES` list in `scraper/scrape.py` with the new season IDs:

1. Go to [Full-Time](https://fulltime.thefa.com) and navigate to the league's fixture page
2. Copy the `selectedSeason` value from the URL
3. Update the ID in the `LEAGUES` list

## Running locally

The recommended way is Docker — it matches the production setup (fetches directly, runs Playwright, and uploads to R2):

```bash
docker compose up --build
# .ics files written to the 'scraper_calendars' volume (./calendars/<league>/)
# JSON feeds written to the 'scraper_feeds' volume (./feeds/)
# All files uploaded to Cloudflare R2 by upload.py
```

Feeds and calendars are stored in named Docker volumes, so previously published
data survives container restarts. If a league fails to fetch on a run, the scraper
restores that league's last published files from R2 so it stays in `index.json`.

To run the scraper directly on the host:

```bash
pip install curl-cffi beautifulsoup4
python scraper/scrape.py
# .ics files written to ./calendars/<league>/
# JSON feeds written to ./feeds/
```

## Scheduling

A local cron job runs the scraper daily at 06:00. The wrapper `scripts/run_scraper.sh`
runs `docker compose up --build` and then cleans up the container. Add this to your
`crontab -e` (adjust the repo path, log path, and timezone as needed):

```
0 6 * * * flock -n /var/lock/fulltime-feeds-scrape.lock /path/to/fulltimeFeeds/scripts/run_scraper.sh >> "$HOME/logs/fulltime-feeds.log" 2>&1
```

Run `scripts/run_scraper.sh` manually any time to force a refresh.

## Notes

- Kick-off times default to **10:00** if Full-Time doesn't list a time (common for youth Sunday football)
- Event duration is set to **60 minutes**
- Team names are taken verbatim from Full-Time, except where the U11-and-below
  rules above replace an opposition name
- The scraper uses `curl-cffi` with browser impersonation to fetch the page reliably (directly, no proxy)
- When a league produces no data on a run, its previously published feeds are restored from R2 rather than dropped
