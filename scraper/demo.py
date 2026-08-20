"""
Demo FC fixture + results generator.

Generates rolling fixtures (next 4 weeks) and past results (last 3 match days)
for a fictional Demo FC club. Covers:
  - Youth U7–U18, including multi-squad ages and girls teams
  - Senior Men's, Senior Men's Reserves, Senior Women's

Called after the main scraper so demo data is always present in feeds/.
"""

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

FEEDS_DIR = Path(__file__).parent.parent / "feeds"

LEAGUE_NAME = "Demo FC"
LEAGUE_SLUG = "demo-fc"
CLUB_NAME = "Demo FC"
CLUB_SLUG = "demo-fc"
VENUE = "Demo FC Ground"

# ---------------------------------------------------------------------------
# Opponent pools
# ---------------------------------------------------------------------------

_YOUTH = [
    "Riverside Rangers",
    "City Athletic",
    "Northgate Youth",
    "Valley United",
    "Castleton Juniors",
    "Westfield Youth",
    "Southbrook Colts",
]

_GIRLS = [
    "Riverside Rangers Girls",
    "City Athletic Girls",
    "Northgate Girls",
    "Valley United Girls",
    "Castleton Girls",
    "Westfield Girls",
    "Southbrook Girls",
]

_SENIOR = [
    "Westfield United",
    "Northgate Athletic",
    "Castleton Rovers",
    "Valley Town",
    "Riverside Town",
    "Southbrook FC",
    "Millfield City",
]

_RESERVES = [
    "Westfield United Reserves",
    "Northgate Athletic Reserves",
    "Castleton Rovers Reserves",
    "Valley Town Reserves",
    "Riverside Town Reserves",
    "Southbrook FC Reserves",
    "Millfield City Reserves",
]

_WOMEN = [
    "Westfield Women",
    "Northgate Ladies",
    "Castleton Women",
    "Valley Town Women",
    "Riverside Ladies",
    "Southbrook Women",
    "Millfield City Women",
]

# ---------------------------------------------------------------------------
# Team definitions
# (name, slug, division, weekday[5=Sat,6=Sun], kick_off, start_home, max_goals, pool, opp_suffix)
# ---------------------------------------------------------------------------

class TeamDef(NamedTuple):
    name: str
    slug: str
    division: str
    weekday: int       # 5 = Saturday, 6 = Sunday
    kick_off: str
    start_home: bool
    max_goals: int
    pool: list
    opp_suffix: str | None  # appended to opponent base name, e.g. "U11" → "Riverside Rangers U11"


TEAM_DEFS: list[TeamDef] = [
    TeamDef("Demo FC U7",        "demo-fc-u7",        "U7 Sunday",          6, "10:00", False, 7, _YOUTH, "U7"),
    TeamDef("Demo FC U8",        "demo-fc-u8",        "U8 Sunday",          6, "10:00", True,  7, _YOUTH, "U8"),
    TeamDef("Demo FC U9 Blues",  "demo-fc-u9-blues",  "U9 Sunday",          6, "10:00", False, 7, _YOUTH, "U9"),
    TeamDef("Demo FC U9 Reds",   "demo-fc-u9-reds",   "U9 Sunday",          6, "10:00", True,  7, _YOUTH, "U9"),
    TeamDef("Demo FC U10",       "demo-fc-u10",       "U10 Sunday",         6, "10:30", True,  7, _YOUTH, "U10"),
    TeamDef("Demo FC U11 Blues", "demo-fc-u11-blues", "U11 Sunday",         6, "10:30", False, 7, _YOUTH, "U11"),
    TeamDef("Demo FC U11 Reds",  "demo-fc-u11-reds",  "U11 Sunday",         6, "10:30", True,  7, _YOUTH, "U11"),
    TeamDef("Demo FC U11 Girls", "demo-fc-u11-girls", "U11 Girls Sunday",   6, "10:30", True,  7, _GIRLS, "U11"),
    TeamDef("Demo FC U12",       "demo-fc-u12",       "U12 Sunday",         6, "10:30", True,  7, _YOUTH, "U12"),
    TeamDef("Demo FC U13 Blues", "demo-fc-u13-blues", "U13 Sunday",         6, "10:30", False, 7, _YOUTH, "U13"),
    TeamDef("Demo FC U13 Reds",  "demo-fc-u13-reds",  "U13 Sunday",         6, "10:30", True,  7, _YOUTH, "U13"),
    TeamDef("Demo FC U14",       "demo-fc-u14",       "U14 Sunday",         6, "11:00", True,  7, _YOUTH, "U14"),
    TeamDef("Demo FC U15",       "demo-fc-u15",       "U15 Sunday",         6, "11:00", False, 7, _YOUTH, "U15"),
    TeamDef("Demo FC U15 Girls", "demo-fc-u15-girls", "U15 Girls Sunday",   6, "11:00", False, 7, _GIRLS, "U15"),
    TeamDef("Demo FC U16",       "demo-fc-u16",       "U16 Sunday",         6, "11:00", True,  7, _YOUTH, "U16"),
    TeamDef("Demo FC U17",       "demo-fc-u17",       "U17 Sunday",         6, "11:00", False, 7, _YOUTH, "U17"),
    TeamDef("Demo FC U18",       "demo-fc-u18",       "U18 Sunday",         6, "11:00", True,  7, _YOUTH, "U18"),
    TeamDef("Demo FC",           "demo-fc-seniors",   "Senior Men's League",    5, "15:00", True,  5, _SENIOR,   None),
    TeamDef("Demo FC Reserves",  "demo-fc-reserves",  "Senior Men's Reserves",  5, "15:00", False, 5, _RESERVES, None),
    TeamDef("Demo FC Women",     "demo-fc-women",     "Senior Women's League",  6, "11:00", False, 5, _WOMEN,    None),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fix_id(match_date: str, home: str, away: str) -> str:
    return hashlib.md5(f"{match_date}|{home}|{away}".encode()).hexdigest()


def _scores(fix_id: str, max_goals: int) -> tuple[int, int]:
    """Deterministic score from fixture ID — no randomness, stable across runs."""
    a = int(fix_id[0:2], 16) % (max_goals + 1)
    b = int(fix_id[2:4], 16) % (max_goals + 1)
    return a, b


def _weekday_dates(ref: date, weekday: int, count: int, direction: int) -> list[date]:
    """
    Return *count* dates spaced a week apart.
    direction=+1 → next occurrences after ref
    direction=-1 → most-recent past occurrences before ref
    """
    if direction == 1:
        days_delta = (weekday - ref.weekday()) % 7 or 7
        first = ref + timedelta(days=days_delta)
        return [first + timedelta(weeks=i) for i in range(count)]
    else:
        days_delta = (ref.weekday() - weekday) % 7 or 7
        first = ref - timedelta(days=days_delta)
        return [first - timedelta(weeks=i) for i in range(count)]


def _opponent_name(base: str, suffix: str | None) -> str:
    return f"{base} {suffix}" if suffix else base


def _build_fixture_entry(
    match_date: date,
    kick_off: str,
    home_team: str,
    away_team: str,
    division: str,
    demo_team: str,
) -> dict:
    date_str = match_date.isoformat()
    is_home = home_team == demo_team
    fid = _fix_id(date_str, home_team, away_team)
    return {
        "id": fid,
        "date": date_str,
        "time": kick_off,
        "home_team": home_team,
        "away_team": away_team,
        "venue": VENUE if is_home else "",
        "division": division,
        "home_away": "home" if is_home else "away",
        "opponent": away_team if is_home else home_team,
    }


def _build_result_entry(
    match_date: date,
    kick_off: str,
    home_team: str,
    away_team: str,
    division: str,
    demo_team: str,
    max_goals: int,
) -> dict:
    date_str = match_date.isoformat()
    is_home = home_team == demo_team
    fid = _fix_id(date_str, home_team, away_team)
    hs, as_ = _scores(fid, max_goals)
    return {
        "id": fid,
        "date": date_str,
        "time": kick_off,
        "home_team": home_team,
        "away_team": away_team,
        "home_score": hs,
        "away_score": as_,
        "venue": VENUE if is_home else "",
        "division": division,
        "home_away": "home" if is_home else "away",
        "opponent": away_team if is_home else home_team,
        "goals_for":     hs if is_home else as_,
        "goals_against": as_ if is_home else hs,
    }


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def generate(today: date | None = None) -> None:
    today = today or date.today()
    generated_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    league_dir = FEEDS_DIR / LEAGUE_SLUG
    teams_dir = league_dir / "teams"
    # Remove stale team files from previous runs before writing fresh ones
    if teams_dir.exists():
        known_slugs = {td.slug for td in TEAM_DEFS}
        for f in teams_dir.glob("*.json"):
            if f.stem not in known_slugs:
                f.unlink()
    teams_dir.mkdir(parents=True, exist_ok=True)

    index_teams: list[dict] = []
    club_fixtures: list[dict] = []
    club_results: list[dict] = []

    for td in TEAM_DEFS:
        future_dates = _weekday_dates(today, td.weekday, 4, direction=+1)
        past_dates   = _weekday_dates(today, td.weekday, 3, direction=-1)

        fixtures: list[dict] = []
        for i, d in enumerate(future_dates):
            is_home = (i % 2 == 0) if td.start_home else (i % 2 != 0)
            opp = _opponent_name(td.pool[i % len(td.pool)], td.opp_suffix)
            home, away = (td.name, opp) if is_home else (opp, td.name)
            fixtures.append(_build_fixture_entry(d, td.kick_off, home, away, td.division, td.name))

        results: list[dict] = []
        for i, d in enumerate(past_dates):
            # offset pool index so past opponents don't repeat upcoming ones
            pool_idx = (len(td.pool) - 1 - i) % len(td.pool)
            is_home = (i % 2 == 0) if not td.start_home else (i % 2 != 0)
            opp = _opponent_name(td.pool[pool_idx], td.opp_suffix)
            home, away = (td.name, opp) if is_home else (opp, td.name)
            results.append(_build_result_entry(d, td.kick_off, home, away, td.division, td.name, td.max_goals))

        team_feed = {
            "team": td.name,
            "league": LEAGUE_NAME,
            "generated": generated_ts,
            "fixtures": fixtures,
            "results": results,
        }
        (teams_dir / f"{td.slug}.json").write_text(json.dumps(team_feed, indent=2) + "\n")

        for fix in fixtures:
            club_fixtures.append({**fix, "league": LEAGUE_NAME, "team": td.name})
        for res in results:
            club_results.append({**res, "league": LEAGUE_NAME, "team": td.name})

        index_teams.append({"name": td.name, "slug": td.slug})

    # ------------------------------------------------------------------ club feed
    clubs_dir = FEEDS_DIR / "clubs"
    clubs_dir.mkdir(exist_ok=True)
    club_feed = {
        "club": CLUB_NAME,
        "generated": generated_ts,
        "fixtures": sorted(club_fixtures, key=lambda f: (f["date"], f["team"])),
        "results":  sorted(club_results,  key=lambda r: (r["date"], r["team"]), reverse=True),
    }
    (clubs_dir / f"{CLUB_SLUG}.json").write_text(json.dumps(club_feed, indent=2) + "\n")

    # -------------------------------------------------------------- index.json
    index_path = FEEDS_DIR / "index.json"
    if index_path.exists():
        index = json.loads(index_path.read_text())
    else:
        index = {"generated": generated_ts, "leagues": [], "clubs": []}

    league_entry = {
        "name": LEAGUE_NAME,
        "slug": LEAGUE_SLUG,
        "teams": index_teams,
    }
    index["leagues"] = [lg for lg in index.get("leagues", []) if lg.get("slug") != LEAGUE_SLUG]
    index["leagues"].append(league_entry)

    club_entry = {
        "name": CLUB_NAME,
        "slug": CLUB_SLUG,
        "teams": [t["name"] for t in index_teams],
    }
    index["clubs"] = [cl for cl in index.get("clubs", []) if cl.get("slug") != CLUB_SLUG]
    index["clubs"].append(club_entry)

    index_path.write_text(json.dumps(index, indent=2) + "\n")

    print(f"Demo FC: wrote {len(index_teams)} team feeds + club feed + updated index.json")


if __name__ == "__main__":
    generate()
