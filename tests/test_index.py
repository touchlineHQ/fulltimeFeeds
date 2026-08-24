"""Unit tests for scraper/index.py — scan-based root index builder."""

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from index import build_index, club_entries, league_dirs, write_index  # noqa: E402


def _write_teams(feeds_dir: Path, slug: str, league: str, teams: list[dict]) -> None:
    league_dir = feeds_dir / slug
    league_dir.mkdir(parents=True, exist_ok=True)
    (league_dir / "teams.json").write_text(
        json.dumps({"league": league, "teams": teams}), encoding="utf-8"
    )


def _write_club(
    feeds_dir: Path,
    slug: str,
    club: str,
    fixtures: list[dict] | None = None,
    results: list[dict] | None = None,
) -> None:
    clubs_dir = feeds_dir / "clubs"
    clubs_dir.mkdir(parents=True, exist_ok=True)
    (clubs_dir / f"{slug}.json").write_text(
        json.dumps({"club": club, "fixtures": fixtures or [], "results": results or []}),
        encoding="utf-8",
    )


class TestLeagueEntries:
    def test_builds_envelope_with_nested_teams(self, tmp_path):
        _write_teams(tmp_path, "league-a", "League A", [
            {"name": "Team One", "slug": "team-one"},
            {"name": "Team Two", "slug": "team-two"},
        ])
        _write_teams(tmp_path, "league-b", "League B", [
            {"name": "Team Three", "slug": "team-three"},
        ])

        index = build_index(tmp_path, generated="2026-01-01T00:00:00Z")

        assert set(index) == {"generated", "from_cache", "leagues", "clubs"}
        assert index["generated"] == "2026-01-01T00:00:00Z"
        assert index["from_cache"] is False
        assert index["leagues"] == [
            {
                "name": "League A",
                "slug": "league-a",
                "teams": [
                    {"name": "Team One", "slug": "team-one"},
                    {"name": "Team Two", "slug": "team-two"},
                ],
            },
            {
                "name": "League B",
                "slug": "league-b",
                "teams": [{"name": "Team Three", "slug": "team-three"}],
            },
        ]
        assert index["clubs"] == []

    def test_generated_defaults_to_now_iso(self, tmp_path):
        _write_teams(tmp_path, "aa-league", "Alpha League", [])

        generated = build_index(tmp_path)["generated"]

        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", generated)

    def test_leagues_ordered_by_name(self, tmp_path):
        _write_teams(tmp_path, "zz-league", "Zed League", [])
        _write_teams(tmp_path, "aa-league", "Alpha League", [])

        names = [e["name"] for e in build_index(tmp_path)["leagues"]]
        assert names == ["Alpha League", "Zed League"]

    def test_clubs_dir_excluded_from_leagues(self, tmp_path):
        _write_teams(tmp_path, "some-league", "Some League", [])
        _write_club(tmp_path, "demo-fc", "Demo FC")

        assert [p.name for p in league_dirs(tmp_path)] == ["some-league"]
        assert [e["slug"] for e in build_index(tmp_path)["leagues"]] == ["some-league"]

    def test_missing_or_broken_teams_json_with_no_team_feeds_omitted(self, tmp_path):
        _write_teams(tmp_path, "ok-league", "OK League", [])
        (tmp_path / "no-teams").mkdir()
        empty_dir = tmp_path / "empty-teams"
        empty_dir.mkdir()
        (empty_dir / "teams.json").write_text("not json", encoding="utf-8")

        assert [e["slug"] for e in build_index(tmp_path)["leagues"]] == ["ok-league"]

    def test_teams_from_team_feeds_when_teams_json_missing(self, tmp_path):
        team_dir = tmp_path / "yel-sunday" / "teams"
        team_dir.mkdir(parents=True)
        (team_dir / "east-leake-bantams-green-u12.json").write_text(json.dumps({
            "team": "East Leake Bantams Green^ U12",
            "league": "YEL East Midlands Sunday 26/27",
        }), encoding="utf-8")
        (team_dir / "cotgrave-blue-u9.json").write_text(json.dumps({
            "team": "Cotgrave Blue U9",
            "league": "YEL East Midlands Sunday 26/27",
        }), encoding="utf-8")

        leagues = build_index(tmp_path)["leagues"]

        assert leagues == [{
            "name": "YEL East Midlands Sunday 26/27",
            "slug": "yel-sunday",
            "teams": [
                {"name": "Cotgrave Blue U9", "slug": "cotgrave-blue-u9"},
                {"name": "East Leake Bantams Green^ U12", "slug": "east-leake-bantams-green-u12"},
            ],
        }]

    def test_team_feed_league_name_falls_back_to_slug(self, tmp_path):
        team_dir = tmp_path / "mystery-league" / "teams"
        team_dir.mkdir(parents=True)
        (team_dir / "team-one.json").write_text(json.dumps({"team": "Team One"}), encoding="utf-8")

        leagues = build_index(tmp_path)["leagues"]

        assert leagues == [{
            "name": "mystery-league",
            "slug": "mystery-league",
            "teams": [{"name": "Team One", "slug": "team-one"}],
        }]

    def test_league_name_falls_back_to_slug(self, tmp_path):
        league_dir = tmp_path / "mystery-league"
        league_dir.mkdir()
        (league_dir / "teams.json").write_text(
            json.dumps({"teams": []}), encoding="utf-8"
        )

        leagues = build_index(tmp_path)["leagues"]
        assert leagues == [{"name": "mystery-league", "slug": "mystery-league", "teams": []}]

    def test_missing_feeds_dir_gives_empty_index(self, tmp_path):
        index = build_index(tmp_path / "nope", generated="2026-01-01T00:00:00Z")
        assert index == {
            "generated": "2026-01-01T00:00:00Z",
            "from_cache": False,
            "leagues": [],
            "clubs": [],
        }

    def test_teams_passed_through_as_stored(self, tmp_path):
        _write_teams(tmp_path, "lg", "LG", [
            {"name": "B Team", "slug": "b-team"},
            {"name": "A Team", "slug": "a-team"},
        ])

        leagues = build_index(tmp_path)["leagues"]
        assert leagues[0]["teams"] == [
            {"name": "B Team", "slug": "b-team"},
            {"name": "A Team", "slug": "a-team"},
        ]


class TestClubEntries:
    def test_builds_clubs_from_club_feed_files(self, tmp_path):
        _write_club(tmp_path, "demo-fc", "Demo FC", fixtures=[
            {"league": "Demo FC", "team": "Demo FC U10"},
        ])
        _write_club(tmp_path, "arnold-town", "Arnold Town", results=[
            {"league": "Euro Soccer 26/27", "team": "Arnold Town"},
        ])

        assert club_entries(tmp_path) == [
            {"name": "Arnold Town", "slug": "arnold-town", "league": "Euro Soccer 26/27"},
            {"name": "Demo FC", "slug": "demo-fc", "league": "Demo FC"},
        ]

    def test_club_spanning_multiple_leagues_gives_one_entry_per_league(self, tmp_path):
        _write_club(tmp_path, "demo-fc", "Demo FC", fixtures=[
            {"league": "Youth League", "team": "Demo FC U10"},
            {"league": "Senior League", "team": "Demo FC Reserves"},
        ])

        assert club_entries(tmp_path) == [
            {"name": "Demo FC", "slug": "demo-fc", "league": "Senior League"},
            {"name": "Demo FC", "slug": "demo-fc", "league": "Youth League"},
        ]

    def test_club_without_leagues_falls_back_to_empty_league(self, tmp_path):
        _write_club(tmp_path, "lonely-club", "Lonely Club", fixtures=[
            {"team": "First Team"},
        ])

        assert club_entries(tmp_path) == [
            {"name": "Lonely Club", "slug": "lonely-club", "league": ""},
        ]

    def test_unreadable_club_file_skipped(self, tmp_path):
        _write_club(tmp_path, "ok-club", "OK Club", fixtures=[{"league": "L1"}])
        (tmp_path / "clubs" / "broken.json").write_text("not json", encoding="utf-8")

        assert club_entries(tmp_path) == [{"name": "OK Club", "slug": "ok-club", "league": "L1"}]

    def test_missing_clubs_dir_gives_empty(self, tmp_path):
        assert club_entries(tmp_path) == []

    def test_clubs_included_in_index(self, tmp_path):
        _write_teams(tmp_path, "demo-fc", "Demo FC", [{"name": "Demo FC U7", "slug": "demo-fc-u7"}])
        _write_club(tmp_path, "demo-fc", "Demo FC", fixtures=[{"league": "Demo FC"}])

        index = build_index(tmp_path, generated="2026-01-01T00:00:00Z")

        assert index["clubs"] == [{"name": "Demo FC", "slug": "demo-fc", "league": "Demo FC"}]


class TestWriteIndex:
    def test_write_index_round_trip(self, tmp_path):
        _write_teams(tmp_path, "abc", "ABC League", [{"name": "ABC U10", "slug": "abc-u10"}])

        out = write_index(tmp_path, generated="2026-01-01T00:00:00Z")

        assert out == tmp_path / "index.json"
        assert json.loads(out.read_text()) == build_index(tmp_path, generated="2026-01-01T00:00:00Z")
        assert json.loads(out.read_text()) == {
            "generated": "2026-01-01T00:00:00Z",
            "from_cache": False,
            "leagues": [
                {
                    "name": "ABC League",
                    "slug": "abc",
                    "teams": [{"name": "ABC U10", "slug": "abc-u10"}],
                }
            ],
            "clubs": [],
        }


class TestFromCache:
    def test_no_cache_map_means_fresh_envelope_and_leagues(self, tmp_path):
        _write_teams(tmp_path, "abc", "ABC League", [{"name": "ABC U10", "slug": "abc-u10"}])

        index = build_index(tmp_path, generated="2026-01-01T00:00:00Z")

        assert index["from_cache"] is False
        assert all("from_cache" not in e for e in index["leagues"])

    def test_restored_league_flagged_per_entry_and_at_top_level(self, tmp_path):
        _write_teams(tmp_path, "abc", "ABC League", [{"name": "ABC U10", "slug": "abc-u10"}])
        _write_teams(tmp_path, "xyz", "XYZ League", [{"name": "XYZ U11", "slug": "xyz-u11"}])

        index = build_index(
            tmp_path, generated="2026-01-01T00:00:00Z", from_cache={"abc": True}
        )

        assert index["from_cache"] is True
        by_slug = {e["slug"]: e for e in index["leagues"]}
        assert by_slug["abc"]["from_cache"] is True
        assert "from_cache" not in by_slug["xyz"]

    def test_all_fresh_map_leaves_everything_unflagged(self, tmp_path):
        _write_teams(tmp_path, "abc", "ABC League", [{"name": "ABC U10", "slug": "abc-u10"}])

        index = build_index(
            tmp_path, generated="2026-01-01T00:00:00Z", from_cache={"abc": False}
        )

        assert index["from_cache"] is False
        assert all("from_cache" not in e for e in index["leagues"])

    def test_write_index_persists_from_cache(self, tmp_path):
        _write_teams(tmp_path, "abc", "ABC League", [{"name": "ABC U10", "slug": "abc-u10"}])

        out = write_index(
            tmp_path, generated="2026-01-01T00:00:00Z", from_cache={"abc": True}
        )

        payload = json.loads(out.read_text())
        assert payload["from_cache"] is True
        assert payload["leagues"][0]["from_cache"] is True