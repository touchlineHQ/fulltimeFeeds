"""
Shared root index builder for feeds/.

index.json is derived purely from what is on disk: it scans feeds/ for league
directories (excluding clubs/) and uses each league's teams.json to build the
league entries, plus feeds/clubs/ for the club entries.  This keeps the index
in sync regardless of whether scrape.py, demo.py, or both have run, and avoids
the brittle read-modify-merge approach.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

FEEDS_DIR = Path(__file__).parent.parent / "feeds"
CLUBS_DIR_NAME = "clubs"


def league_dirs(feeds_dir: Path = FEEDS_DIR) -> list[Path]:
    """Sorted league directories under feeds_dir, excluding the clubs dir."""
    if not feeds_dir.is_dir():
        return []
    return sorted(
        (
            p for p in feeds_dir.iterdir()
            if p.is_dir() and p.name != CLUBS_DIR_NAME
        ),
        key=lambda p: p.name,
    )


def teams_from_team_feeds(league_dir: Path) -> tuple[str, list[dict]] | None:
    """Synthesize (league_name, teams) from a league's per-team feeds.

    Used when teams.json is missing or unreadable: each teams/<slug>.json feed
    carries a top-level "team" name, and the league name falls back to the
    first feed's "league" field.  Returns None when there are no team feeds.
    """
    teams_dir = league_dir / "teams"
    if not teams_dir.is_dir():
        return None

    league_name: str | None = None
    teams: list[dict] = []
    for payload_path in sorted(teams_dir.glob("*.json")):
        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning(f"Could not read {payload_path} — skipping team feed")
            continue
        if league_name is None:
            league_name = payload.get("league")
        teams.append({"name": payload.get("team") or payload_path.stem, "slug": payload_path.stem})

    if not teams:
        return None
    return league_name or league_dir.name, teams


def get_league_teams(league_dir: Path) -> tuple[str, list[dict]] | None:
    """Return (league_name, teams) for a league directory.

    Prefers teams.json (authoritative).  Falls back to the league's per-team
    feeds when teams.json is missing or unreadable, so a league is only dropped
    from the index when nothing for it exists on disk.  Uses the directory name
    as a last-resort league name when neither source provides one.
    """
    teams_file = league_dir / "teams.json"
    if not teams_file.is_file():
        return teams_from_team_feeds(league_dir)
    try:
        payload = json.loads(teams_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(f"Could not read {teams_file} — falling back to team feeds ({exc})")
        return teams_from_team_feeds(league_dir)
    return payload.get("league", league_dir.name), payload.get("teams", [])


def league_entries(feeds_dir: Path = FEEDS_DIR) -> list[dict]:
    """Build the league entries: {"name", "slug", "teams"}, by league name."""
    entries: list[dict] = []
    for league_dir in league_dirs(feeds_dir):
        league_teams = get_league_teams(league_dir)
        if league_teams is None:
            continue
        name, teams = league_teams
        entries.append({"name": name, "slug": league_dir.name, "teams": teams})
    entries.sort(key=lambda e: (e["name"].lower(), e["slug"]))
    return entries


def club_entries(feeds_dir: Path = FEEDS_DIR) -> list[dict]:
    """Build club entries from feeds/clubs/*.json.

    Each club feed is read for its "club" name; one {"name", "slug", "league"}
    entry is emitted per distinct league found across its fixtures/results, so
    clubs spanning several leagues have an entry per league.
    """
    clubs_dir = feeds_dir / CLUBS_DIR_NAME
    if not clubs_dir.is_dir():
        return []

    entries: list[dict] = []
    for payload_path in sorted(clubs_dir.glob("*.json")):
        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning(f"Could not read {payload_path} — skipping club")
            continue

        slug_name = payload_path.stem
        name = payload.get("club") or slug_name
        leagues = sorted({
            row["league"] for row in payload.get("fixtures", []) + payload.get("results", [])
            if row.get("league")
        }) or [""]
        for league in leagues:
            entries.append({"name": name, "slug": slug_name, "league": league})

    entries.sort(key=lambda e: (e["name"].lower(), e["slug"], e["league"]))
    return entries


def build_index(
    feeds_dir: Path = FEEDS_DIR,
    generated: str | None = None,
    from_cache: dict[str, bool] | None = None,
) -> dict:
    """Build the root index: {"generated", "from_cache", "leagues", "clubs"}.

    `from_cache` maps league slug -> True for leagues whose published data was
    restored from the last good run rather than freshly scraped. Each affected
    league entry gets `"from_cache": true`, and the envelope's top-level
    `"from_cache"` is true when any league was restored.
    """
    cache_map = from_cache or {}
    leagues = league_entries(feeds_dir)
    for entry in leagues:
        if cache_map.get(entry["slug"]):
            entry["from_cache"] = True
    return {
        "generated": generated or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "from_cache": any(cache_map.values()),
        "leagues": leagues,
        "clubs": club_entries(feeds_dir),
    }


def write_index(
    feeds_dir: Path = FEEDS_DIR,
    generated: str | None = None,
    from_cache: dict[str, bool] | None = None,
) -> Path:
    """Write index.json (the {generated, from_cache, leagues, clubs} envelope) and return its path."""
    payload = build_index(feeds_dir, generated, from_cache)
    out = feeds_dir / "index.json"
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log.info(f"  Written {out} ({len(payload['leagues'])} leagues, {len(payload['clubs'])} clubs)")
    return out