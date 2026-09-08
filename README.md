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

Restricted fixture rows carry `"publication_restricted": true`. Every feed also
carries a `compliance` block:

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
