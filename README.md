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
| `feeds/index.json` | All leagues, teams, and clubs with their slugs |
| `feeds/<league>/fixtures.json` | Every upcoming fixture in a league, sorted by date/time |
| `feeds/<league>/results.json` | Every result in a league, most recent first |
| `feeds/<league>/teams/<team>.json` | Fixtures and results for one team |
| `feeds/clubs/<club>.json` | Fixtures and results for every team in a club, across leagues |

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
  "home_team": "Arnold Town Blue U11",
  "away_team": "Opponent FC U11",
  "venue": "The Ground",
  "division": "U11 Division 1"
}
```

Result (in `results.json` and `results` arrays):

```json
{
  "id": "...",
  "date": "2026-03-15",
  "time": "10:00",
  "home_team": "Arnold Town Blue U11",
  "away_team": "Opponent FC U11",
  "home_score": 3,
  "away_score": 1,
  "venue": "The Ground",
  "division": "U11 Division 1"
}
```

Team and club feeds additionally include `league`, `team`, `home_away` (`"home"` or `"away"`), `opponent`, and (results only) `goals_for` and `goals_against`.

### Using a feed on a static site

```js
const url = 'https://fixtures.touchlinehq.co.uk/feeds/clubs/arnold-town.json';
const { club, fixtures } = await fetch(url).then(r => r.json());
```

Use `feeds/index.json` to discover available club and team slugs programmatically.

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

The recommended way is Docker — it matches the production setup (installs Tor, Playwright, and uploads to R2):

```bash
docker compose up --build
# .ics files written to ./calendars/<league>/ (inside the container)
# JSON feeds written to ./feeds/ (inside the container)
# All files uploaded to Cloudflare R2 by upload.py
```

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
- Team names are taken verbatim from Full-Time
- The scraper uses `curl-cffi` with browser impersonation to fetch the page reliably
- Stale calendars and feeds (removed teams/leagues) are automatically cleaned up on each run
