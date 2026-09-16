"""
Unit tests for scraper/scrape.py — name normalisation and club grouping.

Run with: pytest tests/
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from scrape import (
    Fixture,
    build_prefix_counts,
    clean_team_name,
    infer_club_name,
    _normalise_for_grouping,
    fixtures_to_ics,
    parse_results,
    parse_fixtures,
    restore_league_from_bucket,
)
import scrape


def _pipeline(raw_names):
    """Mirror the real scraper pipeline: clean names first, then group."""
    names = [clean_team_name(n) for n in raw_names]
    counts = build_prefix_counts(names)
    return {n: infer_club_name(n, counts) for n in names}


# ---------------------------------------------------------------------------
# clean_team_name
# ---------------------------------------------------------------------------

class TestCleanTeamName:
    """Season prefix/suffix stripping."""

    @pytest.mark.parametrize("raw, expected", [
        # Bare leading prefix (slash separator)
        ("25/26 AFC Chellaston Rapids U13", "AFC Chellaston Rapids U13"),
        ("2025/26 Team Name U10",           "Team Name U10"),
        # Bare leading prefix (hyphen separator)
        ("25-26 Team Name U10",             "Team Name U10"),
        # Parenthesised leading prefix
        ("(25/26) Ravenshead Reds U13",     "Ravenshead Reds U13"),
        ("(2025/26) Ravenshead Reds U13",   "Ravenshead Reds U13"),
        # Trailing suffix
        ("Arnold Town U10 Whites 25-26",    "Arnold Town U10 Whites"),
        ("Arnold Town U10 Whites 2025-26",  "Arnold Town U10 Whites"),
        ("Arnold Town U10 Whites 25/26",    "Arnold Town U10 Whites"),
        # No change
        ("Arnold Town U10 Whites",          "Arnold Town U10 Whites"),
        # Whitespace collapsed
        ("Arnold  Town   U10",              "Arnold Town U10"),
        # Mid-name numbers must NOT be stripped
        ("Team 1-2 FC U10",                 "Team 1-2 FC U10"),
    ])
    def test_season_tokens(self, raw, expected):
        assert clean_team_name(raw) == expected


# ---------------------------------------------------------------------------
# _normalise_for_grouping
# ---------------------------------------------------------------------------

class TestNormaliseForGrouping:
    @pytest.mark.parametrize("raw, expected", [
        ("A.C. United F.C. U13", "AC United FC U13"),
        ("A.C. United",          "AC United"),
        ("AC United",            "AC United"),
        ("Arnold Town U10",      "Arnold Town U10"),
        ("G3A FC Juve",          "G3A FC Juve"),  # digit — no change
    ])
    def test_dot_stripping(self, raw, expected):
        assert _normalise_for_grouping(raw) == expected


# ---------------------------------------------------------------------------
# build_prefix_counts + infer_club_name — integration
# ---------------------------------------------------------------------------

class TestClubGrouping:

    # --- Bug 1: (25/26) prefix ---
    def test_parenthesised_season_prefix_stripped(self):
        """(25/26) Ravenshead Reds U13 must join the Ravenshead Reds club.

        clean_team_name strips the prefix at parse time, so by the time
        build_prefix_counts / infer_club_name are called the raw prefix is gone.
        The _pipeline helper mirrors that real scraper flow.
        """
        result = _pipeline([
            "(25/26) Ravenshead Reds U13",
            "Ravenshead Reds U10",
            "Ravenshead Reds U11",
        ])
        assert set(result.values()) == {"Ravenshead Reds"}, result

    # --- Bug 2: A.C. vs AC splitting ---
    def test_punctuation_variants_same_club(self):
        """A.C. United and AC United must map to the same club."""
        names = [
            "A.C. United F.C. U13",
            "A.C. United U8",
            "A.C. United U9",
            "AC United U10",
            "AC United U11",
        ]
        counts = build_prefix_counts(names)
        clubs = {infer_club_name(n, counts) for n in names}
        assert len(clubs) == 1, f"Expected 1 club, got: {clubs}"
        assert clubs == {"AC United"}, clubs

    # --- Bug 3: & truncation ---
    def test_ampersand_not_treated_as_word(self):
        """Club names must not end with bare '&'."""
        names = [
            "Allexton & New Parks Magpies U12",
            "Allexton & New Parks Junior U14",
        ]
        counts = build_prefix_counts(names)
        for n in names:
            club = infer_club_name(n, counts)
            assert not club.endswith("&"), f"Truncated club name: {club!r}"

    def test_ampersand_club_newton(self):
        """Newton & Blackwell teams should be grouped under 'Newton & Blackwell'."""
        names = [
            "Newton & Blackwell Cosmos U13",
            "Newton & Blackwell Storm U11",
            "Newton & Blackwell U12",
        ]
        counts = build_prefix_counts(names)
        clubs = {infer_club_name(n, counts) for n in names}
        assert clubs == {"Newton & Blackwell"}, clubs

    def test_ampersand_club_aslockton(self):
        names = [
            "Aslockton & Orston Black U13",
            "Aslockton & Orston Blue U10",
            "Aslockton & Orston Red U13",
        ]
        counts = build_prefix_counts(names)
        clubs = {infer_club_name(n, counts) for n in names}
        assert clubs == {"Aslockton & Orston"}, clubs

    # --- Bug 4: single-word abbreviation clubs ---
    def test_abbreviation_club_with_colour_suffix(self):
        """DLFC Eagles and DLFC Lions must both map to 'DLFC'."""
        names = ["DLFC Eagles U10", "DLFC Lions U12"]
        counts = build_prefix_counts(names)
        clubs = {infer_club_name(n, counts) for n in names}
        assert clubs == {"DLFC"}, clubs

    def test_abbreviation_club_asfc(self):
        names = ["ASFC Gold U10", "ASFC Wolves U12"]
        counts = build_prefix_counts(names)
        clubs = {infer_club_name(n, counts) for n in names}
        assert clubs == {"ASFC"}, clubs

    def test_abbreviation_club_bare_names(self):
        """ASFC U12 / ASFC U14 — no colour suffix, must still group."""
        names = ["ASFC U12", "ASFC U14"]
        counts = build_prefix_counts(names)
        clubs = {infer_club_name(n, counts) for n in names}
        assert clubs == {"ASFC"}, clubs

    # --- Regression: 3-letter generic prefixes must NOT collapse clubs ---
    def test_afc_clubs_not_collapsed(self):
        """AFC Chellaston and AFC Warriors must remain separate clubs."""
        names = [
            "AFC Chellaston Raiders U12",
            "AFC Chellaston Gladiators U13",
            "AFC Warriors Knights U11",
            "AFC Warriors Vikings U11",
        ]
        counts = build_prefix_counts(names)
        assert infer_club_name("AFC Chellaston Raiders U12", counts) == "AFC Chellaston"
        assert infer_club_name("AFC Warriors Knights U11", counts) == "AFC Warriors"

    def test_fc_prefix_not_collapsed(self):
        """FC-prefixed clubs with different second words must stay separate."""
        names = [
            "FC United Reds U10",
            "FC United Blues U10",
            "FC City Yellows U10",
            "FC City Greens U10",
        ]
        counts = build_prefix_counts(names)
        assert infer_club_name("FC United Reds U10", counts) == "FC United"
        assert infer_club_name("FC City Yellows U10", counts) == "FC City"

    # --- Regression: standard clubs still grouped correctly ---
    def test_standard_club_grouping(self):
        names = [
            "Arnold Town Blue U12",
            "Arnold Town Red U12",
            "Arnold Town U11",
        ]
        counts = build_prefix_counts(names)
        clubs = {infer_club_name(n, counts) for n in names}
        assert clubs == {"Arnold Town"}, clubs

    def test_singleton_club(self):
        """A team with no sharing partners falls back to its own stripped name."""
        names = ["Unique FC Eagles U10"]
        counts = build_prefix_counts(names)
        club = infer_club_name("Unique FC Eagles U10", counts)
        # "Eagles" is stripped as a squad designator, leaving "Unique FC"
        # "FC" is not stripped because it's a 2-word name (protection rule)
        assert club == "Unique FC"

    def test_color_suffix_grouping(self):
        """Teams with same color suffix across age groups should group under club name."""
        # Clifton All Whites Blue case - multiple age groups with same color
        result = _pipeline([
            "Clifton All Whites Blue U8",
            "Clifton All Whites Blue U10",
            "Clifton All Whites Blue U13",
        ])
        assert set(result.values()) == {"Clifton All Whites"}, result
        
        # Mixed colors should still group under club
        result = _pipeline([
            "Club FC Red U8",
            "Club FC Blue U10",
            "Club FC Green U12",
        ])
        assert set(result.values()) == {"Club FC"}, result
        
        # Plural colors (Reds, Blues) should be kept as part of club name
        result = _pipeline([
            "Ravenshead Reds U10",
            "Ravenshead Reds U12",
        ])
        assert set(result.values()) == {"Ravenshead Reds"}, result
        
        # Uppercase abbreviations (DLFC) should not be stripped
        result = _pipeline([
            "DLFC Blue U10",
            "DLFC Blue U12",
        ])
        assert set(result.values()) == {"DLFC"}, result
        
        # Mixed designators (color + squad) should still group
        result = _pipeline([
            "Town FC Blue Lions U10",
            "Town FC Blue Tigers U12",
        ])
        assert set(result.values()) == {"Town FC"}, result

    def test_age_group_infix(self):
        """Teams with age groups in the middle (e.g., 'U7 Blue') should group correctly."""
        # Bottesford case
        result = _pipeline([
            "Bottesford U7 Blue",
            "Bottesford U14 Girls",
        ])
        assert set(result.values()) == {"Bottesford"}, result
        
        # More complex case with color suffix after age group
        result = _pipeline([
            "Clubname U8 Red",
            "Clubname U9 Blue",
            "Clubname U10 Green",
        ])
        assert set(result.values()) == {"Clubname"}, result
        
        # Age group infix but no color suffix
        result = _pipeline([
            "Town U12 Lions",
            "Town U14 Tigers",
        ])
        assert set(result.values()) == {"Town"}, result

    def test_east_leake_grouping(self):
        """East Leake variants should all group under 'East Leake'."""
        result = _pipeline([
            "East Leake",
            "East Leake Bantams",
            "East Leake FC",
            "East Leake FC Bantams",
            "East Leake Robins",
        ])
        assert set(result.values()) == {"East Leake"}, result

    def test_bottesford_grouping(self):
        """Bottesford variants should all group under 'Bottesford'."""
        result = _pipeline([
            "Bottesford",
            "Bottesford Blue",
            "Bottesford Girls",
            "Bottesford Yellow",
            "Bottesford U7 Blue",
            "Bottesford U14 Girls",
        ])
        assert set(result.values()) == {"Bottesford"}, result

    def test_keyworth_grouping(self):
        """Keyworth variants should all group under 'Keyworth'."""
        result = _pipeline([
            "Keyworth",
            "Keyworth United",
            "Keyworth United FC",
        ])
        assert set(result.values()) == {"Keyworth"}, result


# ---------------------------------------------------------------------------
# parse_results — venue / division field ordering
# ---------------------------------------------------------------------------

_RESULTS_HTML_TEMPLATE = """<html><body>
<div class="date">22/03/26 12:30</div>
<div class="home-team">{home}</div>
<div class="score">2 - 1</div>
<div class="road-team">{away}</div>
<div class="competition">{division}</div>
<div class="venue">{venue}</div>
</body></html>"""

_RESULTS_HTML_NO_VENUE = """<html><body>
<div class="date">22/03/26 10:00</div>
<div class="home-team">{home}</div>
<div class="score">1 - 0</div>
<div class="road-team">{away}</div>
<div class="competition">{division}</div>
</body></html>"""


class TestParseResultsVenueDivision:
    """Regression tests for venue/division field ordering in results."""

    def test_division_not_placed_in_venue_field(self):
        """The competition label must appear in division, not venue."""
        html = _RESULTS_HTML_TEMPLATE.format(
            home="Home FC U10",
            away="Away FC U10",
            division="U10 Sun Spring Div 3 Red",
            venue="Meadow Lane NG2 3HJ",
        )
        results = parse_results(html)
        assert results, "Expected at least one result"
        r = results[0]
        assert r.division_label == "U10 Sun Spring Div 3 Red", (
            f"division_label was {r.division_label!r} — division placed in wrong field"
        )
        assert r.venue == "Meadow Lane NG2 3HJ", (
            f"venue was {r.venue!r}"
        )

    def test_unknown_division_not_returned_when_division_present(self):
        """Unknown Division must not appear when a competition label exists."""
        html = _RESULTS_HTML_NO_VENUE.format(
            home="Home FC U10",
            away="Away FC U10",
            division="U10 Sun Spring Div 3 Red",
        )
        results = parse_results(html)
        assert results, "Expected at least one result"
        assert results[0].division_label != "Unknown Division", (
            "Got 'Unknown Division' even though division was present in HTML"
        )


# ---------------------------------------------------------------------------
# fixtures_to_ics — DTEND must honour a full 60-min slot
# ---------------------------------------------------------------------------

def _make_fixture(ko: str = "") -> Fixture:
    return Fixture(
        date="22/03/26",
        time=ko,
        home_team="Home FC U10",
        away_team="Away FC U10",
        venue="Meadow Lane NG2 3HJ",
        division_label="U10 Sun Spring Div 3 Red",
    )


def _event_times(ko: str = "") -> tuple[datetime, datetime]:
    ics = fixtures_to_ics("Home FC U10", [_make_fixture(ko)])
    start = re.search(r"DTSTART;TZID=Europe/London:(\d{8}T\d{6})", ics)
    end = re.search(r"DTEND;TZID=Europe/London:(\d{8}T\d{6})", ics)
    assert start and end, f"DTSTART/DTEND missing from ICS:\n{ics}"
    return (
        datetime.strptime(start.group(1), "%Y%m%dT%H%M%S"),
        datetime.strptime(end.group(1), "%Y%m%dT%H%M%S"),
    )


class TestFixturesToIcsDuration:
    """A fixture's DTEND must be exactly 60 minutes after its KO time."""

    def test_off_hour_ko_keeps_full_duration(self):
        """Bug: 10:15 KO must end at 11:15, not snap to 11:00."""
        start, end = _event_times("10:15")
        assert start == datetime(2026, 3, 22, 10, 15)
        assert end == datetime(2026, 3, 22, 11, 15)

    def test_on_the_hour_ko(self):
        start, end = _event_times("10:00")
        assert start == datetime(2026, 3, 22, 10, 0)
        assert end == datetime(2026, 3, 22, 11, 0)

    def test_hour_rollover(self):
        start, end = _event_times("11:45")
        assert start == datetime(2026, 3, 22, 11, 45)
        assert end == datetime(2026, 3, 22, 12, 45)

    def test_midnight_rollover(self):
        start, end = _event_times("23:30")
        assert start == datetime(2026, 3, 22, 23, 30)
        assert end == datetime(2026, 3, 23, 0, 30)

    def test_tbc_defaults_to_10am(self):
        start, end = _event_times("")
        assert start == datetime(2026, 3, 22, 10, 0)
        assert end == datetime(2026, 3, 22, 11, 0)


# ---------------------------------------------------------------------------
# restore_league_from_bucket — falls back to last published R2 state
# ---------------------------------------------------------------------------

class _FakePaginator:
    def __init__(self, objects):
        self._objects = objects

    def paginate(self, **kwargs):
        prefix = kwargs.get("Prefix", "")
        yield {"Contents": [{"Key": k} for k in self._objects if k.startswith(prefix)]}


class _FakeS3:
    def __init__(self, objects):
        self._objects = objects

    def get_paginator(self, name):
        return _FakePaginator(self._objects)

    def get_object(self, **kwargs):
        key = kwargs["Key"]

        class _Body:
            def read(self):
                return ('{"league": "YEL"}' + f" //{key}").encode("utf-8")

        return {"Body": _Body()}


class TestRestoreLeagueFromBucket:

    def test_restores_feeds_and_calendars(self, tmp_path, monkeypatch):
        scrape.FEEDS_DIR = tmp_path / "feeds"
        scrape.OUTPUT_DIR = tmp_path / "calendars"
        monkeypatch.setenv("R2_BUCKET_NAME", "test-bucket")
        monkeypatch.setattr(
            scrape, "_s3_client",
            lambda: _FakeS3([
                "feeds/yel-sunday/teams.json",
                "feeds/yel-sunday/teams/east-leake-bantams-green-u12.json",
                "feeds/yel-sunday/fixtures.json",
                "calendars/yel-sunday/east-leake-bantams-green-u12.ics",
            ]),
        )

        restored = restore_league_from_bucket("YEL Sunday", "yel-sunday")

        assert restored == 4
        teams_file = tmp_path / "feeds" / "yel-sunday" / "teams.json"
        assert teams_file.is_file()
        assert (tmp_path / "feeds" / "yel-sunday" / "teams" / "east-leake-bantams-green-u12.json").is_file()
        assert (tmp_path / "calendars" / "yel-sunday" / "east-leake-bantams-green-u12.ics").is_file()
        assert teams_file.read_text(encoding="utf-8").startswith('{"league": "YEL"}')

    def test_skips_already_present_files(self, tmp_path, monkeypatch):
        scrape.FEEDS_DIR = tmp_path / "feeds"
        scrape.OUTPUT_DIR = tmp_path / "calendars"
        monkeypatch.setenv("R2_BUCKET_NAME", "test-bucket")
        existing = tmp_path / "feeds" / "yel-sunday" / "teams.json"
        existing.parent.mkdir(parents=True)
        existing.write_text("already here", encoding="utf-8")
        monkeypatch.setattr(
            scrape, "_s3_client",
            lambda: _FakeS3(["feeds/yel-sunday/teams.json"]),
        )

        assert restore_league_from_bucket("YEL Sunday", "yel-sunday") == 0
        assert existing.read_text(encoding="utf-8") == "already here"

    def test_returns_zero_without_bucket_configured(self, tmp_path, monkeypatch):
        scrape.FEEDS_DIR = tmp_path / "feeds"
        scrape.OUTPUT_DIR = tmp_path / "calendars"
        monkeypatch.setenv("R2_BUCKET_NAME", "")
        monkeypatch.setattr(scrape, "_s3_client", lambda: None)

        assert restore_league_from_bucket("YEL Sunday", "yel-sunday") == 0


# ---------------------------------------------------------------------------
# main() — exit codes and from_cache bookkeeping
# ---------------------------------------------------------------------------

class TestMainExitCodes:

    @staticmethod
    def _fixture():
        return Fixture(
            date="22/08/26", time="10:00",
            home_team="Arnold Town Blue U11", away_team="Opponent FC U11",
            venue="The Ground", division_label="U11 Division 1",
        )

    def _run(self, monkeypatch, tmp_path, outcomes):
        """outcomes: {league_name: "fresh" | "stale"}; returns (rc, index_payload)."""
        monkeypatch.setattr(scrape, "FEEDS_DIR", tmp_path / "feeds")
        monkeypatch.setattr(scrape, "OUTPUT_DIR", tmp_path / "calendars")
        monkeypatch.setattr(
            scrape, "LEAGUES",
            [("111", "League A"), ("222", "League B")],
        )

        def fake_restore(name, slug):
            # Mirror the real restorer: previously published feeds reappear
            # on disk so the league stays in the disk-derived index.
            league_dir = tmp_path / "feeds" / slug
            league_dir.mkdir(parents=True, exist_ok=True)
            (league_dir / "teams.json").write_text(
                json.dumps({"league": name, "teams": []}), encoding="utf-8"
            )
            return 4

        monkeypatch.setattr(scrape, "restore_league_from_bucket", fake_restore)

        def fake_fetch_fixtures(season_id, league_name):
            if outcomes[league_name] == "fresh":
                return [self._fixture()]
            raise RuntimeError("HTTP Error 403")

        def fake_fetch_results(season_id, league_name):
            return []

        monkeypatch.setattr(scrape, "fetch_fixtures", fake_fetch_fixtures)
        monkeypatch.setattr(scrape, "fetch_results", fake_fetch_results)

        rc = scrape.main()

        index_file = tmp_path / "feeds" / "index.json"
        payload = json.loads(index_file.read_text(encoding="utf-8"))
        return rc, payload

    def test_all_fresh_returns_zero_and_unflagged_index(self, tmp_path, monkeypatch):
        rc, payload = self._run(
            monkeypatch, tmp_path, {"League A": "fresh", "League B": "fresh"}
        )

        assert rc == 0
        assert payload["from_cache"] is False
        assert all("from_cache" not in e for e in payload["leagues"])

    def test_any_fallback_returns_nonzero_and_flags_index(self, tmp_path, monkeypatch):
        rc, payload = self._run(
            monkeypatch, tmp_path, {"League A": "fresh", "League B": "stale"}
        )

        assert rc == 1
        assert payload["from_cache"] is True
        by_slug = {e["slug"]: e for e in payload["leagues"]}
        assert by_slug["league-b"]["from_cache"] is True
        assert "from_cache" not in by_slug["league-a"]


# ---------------------------------------------------------------------------
# Consolidation — one row per match in a club feed
# ---------------------------------------------------------------------------

def _row(match_id, team, home_away, **extra):
    """A club-feed row: one per (team, match), as the aggregator builds them."""
    row = {
        "id": match_id,
        "date": "2026-10-11",
        "time": "10:00",
        "home_team": "East Leake Orange U12",
        "away_team": "East Leake Red U12",
        "venue": "Costock Road",
        "division": "U12 Division 6",
        "league": "YEL Sunday 26/27",
        "team": team,
        "home_away": home_away,
        "opponent": "East Leake Red U12" if home_away == "home" else "East Leake Orange U12",
    }
    row.update(extra)
    return row


class TestConsolidateMatches:

    def test_derby_collapses_to_the_home_side(self):
        rows = [
            _row("m1", "East Leake Orange U12", "home"),
            _row("m1", "East Leake Red U12", "away"),
        ]

        out = scrape.consolidate_matches(rows)

        assert len(out) == 1
        assert out[0]["team"] == "East Leake Orange U12"
        assert out[0]["home_away"] == "home"
        assert out[0]["derby"] is True

    def test_home_side_wins_even_when_the_away_row_came_first(self):
        rows = [
            _row("m1", "East Leake Red U12", "away"),
            _row("m1", "East Leake Orange U12", "home"),
        ]

        out = scrape.consolidate_matches(rows)

        assert [r["team"] for r in out] == ["East Leake Orange U12"]
        assert out[0]["derby"] is True

    def test_same_team_twice_is_deduplicated_without_a_derby_flag(self):
        rows = [
            _row("m1", "East Leake Orange U12", "home"),
            _row("m1", "East Leake Orange U12", "home"),
        ]

        out = scrape.consolidate_matches(rows)

        assert len(out) == 1
        assert "derby" not in out[0]

    def test_distinct_matches_are_left_alone_and_keep_their_order(self):
        rows = [
            _row("m1", "East Leake Orange U12", "home"),
            _row("m2", "East Leake Red U12", "away"),
        ]

        out = scrape.consolidate_matches(rows)

        assert [r["id"] for r in out] == ["m1", "m2"]
        assert all("derby" not in r for r in out)

    def test_source_rows_are_not_mutated(self):
        rows = [
            _row("m1", "East Leake Orange U12", "home"),
            _row("m1", "East Leake Red U12", "away"),
        ]

        scrape.consolidate_matches(rows)

        assert all("derby" not in row for row in rows)


class TestDropSettledFixtures:

    def test_fixture_with_a_result_is_dropped(self):
        fixtures = [_row("m1", "East Leake Robins", "home"), _row("m2", "East Leake Robins", "away")]
        results = [_row("m1", "East Leake Robins", "home", home_score=3, away_score=1)]

        out = scrape.drop_settled_fixtures(fixtures, results)

        assert [r["id"] for r in out] == ["m2"]

    def test_no_results_leaves_the_fixture_list_intact(self):
        fixtures = [_row("m1", "East Leake Robins", "home")]

        assert scrape.drop_settled_fixtures(fixtures, []) == fixtures


class TestDedupeTeamRows:

    def test_same_match_and_team_across_two_competitions_is_kept_once(self):
        rows = [
            _row("m1", "East Leake Robins", "home", league="League A"),
            _row("m1", "East Leake Robins", "home", league="League B"),
        ]

        out = scrape.dedupe_team_rows(rows)

        assert len(out) == 1
        assert out[0]["league"] == "League A"

    def test_two_teams_in_the_same_match_both_survive(self):
        rows = [
            _row("m1", "East Leake Orange U12", "home"),
            _row("m1", "East Leake Red U12", "away"),
        ]

        assert len(scrape.dedupe_team_rows(rows)) == 2


class TestWriteClubFeed:
    """End-to-end shape of feeds/clubs/<slug>.json."""

    @staticmethod
    def _write(tmp_path, monkeypatch, fixtures, results, generated="2026-09-16T12:00:00Z"):
        monkeypatch.setattr(scrape, "FEEDS_DIR", tmp_path / "feeds")
        scrape.write_club_feed("East Leake", "east-leake", fixtures, results, generated)
        return json.loads(
            (tmp_path / "feeds" / "clubs" / "east-leake.json").read_text(encoding="utf-8")
        )

    def test_open_age_derby_is_listed_once(self, tmp_path, monkeypatch):
        fixtures = [
            _row("m1", "East Leake Robins", "home",
                 home_team="East Leake Robins", away_team="East Leake Robins Reserves",
                 opponent="East Leake Robins Reserves", division="Division One"),
            _row("m1", "East Leake Robins Reserves", "away",
                 home_team="East Leake Robins", away_team="East Leake Robins Reserves",
                 opponent="East Leake Robins", division="Division One"),
        ]

        payload = self._write(tmp_path, monkeypatch, fixtures, [])

        assert len(payload["fixtures"]) == 1
        assert payload["fixtures"][0]["derby"] is True
        assert payload["fixtures"][0]["team"] == "East Leake Robins"

    def test_played_match_appears_only_in_results(self, tmp_path, monkeypatch):
        played = _row(
            "m1", "East Leake Robins", "home",
            date="2026-09-12", home_team="East Leake Robins", away_team="Awsworth Villa",
            opponent="Awsworth Villa", division="Division One",
        )
        result = dict(played, home_score=3, away_score=1, goals_for=3, goals_against=1)

        payload = self._write(tmp_path, monkeypatch, [played], [result])

        assert payload["fixtures"] == []
        assert [r["id"] for r in payload["results"]] == ["m1"]

    def test_no_match_id_is_listed_in_two_arrays(self, tmp_path, monkeypatch):
        fixtures = [
            _row("m1", "East Leake Robins", "home", date="2026-09-12",
                 home_team="East Leake Robins", away_team="Awsworth Villa",
                 opponent="Awsworth Villa", division="Division One"),
            _row("m2", "East Leake Robins", "away", date="2026-10-03",
                 home_team="Cotgrave", away_team="East Leake Robins",
                 opponent="Cotgrave", division="Division One"),
        ]
        results = [
            dict(fixtures[0], home_score=3, away_score=1, goals_for=3, goals_against=1),
        ]

        payload = self._write(tmp_path, monkeypatch, fixtures, results)

        fixture_ids = {r["id"] for r in payload["fixtures"]}
        result_ids = {r["id"] for r in payload["results"]}
        assert fixture_ids.isdisjoint(result_ids)
        assert fixture_ids == {"m2"}

    def test_restricted_derby_gives_both_teams_a_participation_record(self, tmp_path, monkeypatch):
        # Played last Sunday, and never moved onto the results page — the
        # league does not publish U12-and-below results.
        fixtures = [
            _row("m1", "East Leake Orange U10", "home", date="2026-09-13",
                 home_team="East Leake Orange U10", away_team="East Leake Red U10",
                 opponent="East Leake Red U10", division="U10 Division 1"),
            _row("m1", "East Leake Red U10", "away", date="2026-09-13",
                 home_team="East Leake Orange U10", away_team="East Leake Red U10",
                 opponent="East Leake Orange U10", division="U10 Division 1"),
        ]

        payload = self._write(tmp_path, monkeypatch, fixtures, [])

        assert payload["fixtures"] == []
        assert {r["team"] for r in payload["participation"]} == {
            "East Leake Orange U10", "East Leake Red U10",
        }
        assert all(r["played"] is True for r in payload["participation"])
        assert len({r["id"] for r in payload["participation"]}) == 2

    def test_restricted_upcoming_derby_is_listed_once_and_redacted(self, tmp_path, monkeypatch):
        fixtures = [
            _row("m1", "East Leake Orange U10", "home", date="2026-11-08",
                 home_team="East Leake Orange U10", away_team="East Leake Red U10",
                 opponent="East Leake Red U10", division="U10 Division 1"),
            _row("m1", "East Leake Red U10", "away", date="2026-11-08",
                 home_team="East Leake Orange U10", away_team="East Leake Red U10",
                 opponent="East Leake Orange U10", division="U10 Division 1"),
        ]

        payload = self._write(tmp_path, monkeypatch, fixtures, [])

        assert len(payload["fixtures"]) == 1
        row = payload["fixtures"][0]
        assert row["team"] == "East Leake Orange U10"
        assert row["opponent"] == "Opposition"
        assert row["away_team"] == "Opposition"
        assert row["venue"] == ""
        assert row["publication_restricted"] is True

    def test_restricted_result_becomes_one_participation_record_per_team(self, tmp_path, monkeypatch):
        results = [
            _row("m1", "East Leake Orange U10", "home", date="2026-09-13",
                 division="U10 Division 1", goals_for=None, goals_against=None),
            _row("m1", "East Leake Red U10", "away", date="2026-09-13",
                 division="U10 Division 1", goals_for=None, goals_against=None),
        ]

        payload = self._write(tmp_path, monkeypatch, [], results)

        assert payload["results"] == []
        assert len(payload["participation"]) == 2
        assert payload["compliance"]["results_withheld"] == 2
        assert all("goals_for" not in r for r in payload["participation"])


# ---------------------------------------------------------------------------
# fetch_results — a refused page is reported, not published as "no results"
# ---------------------------------------------------------------------------

class TestFetchResults:

    @staticmethod
    def _result():
        return scrape.Result(
            date="12/09/26", time="15:00",
            home_team="East Leake Robins", away_team="Awsworth Villa",
            home_score=3, away_score=1,
            venue="Costock Road", division_label="Division One",
        )

    def test_rows_are_returned_when_the_page_answers(self, monkeypatch):
        monkeypatch.setattr(scrape, "_fetch_page", lambda url, label: "<html>rows</html>")
        monkeypatch.setattr(scrape, "parse_results", lambda html: [self._result()])

        assert len(scrape.fetch_results("123", "League A")) == 1

    def test_refused_fetch_raises_results_unavailable(self, monkeypatch):
        def blocked(url, label):
            raise RuntimeError("HTTP Error 403")

        monkeypatch.setattr(scrape, "_fetch_page", blocked)

        with pytest.raises(scrape.ResultsUnavailable, match="League A"):
            scrape.fetch_results("123", "League A")

    def test_challenge_page_served_as_200_raises_too(self, monkeypatch):
        # Cloudflare answers the results path with an interstitial; a 200
        # carrying one is not data, and must not read as "no matches played".
        challenge = "<html><head><title>Attention Required!</title></head></html>"
        monkeypatch.setattr(scrape, "_fetch_page", lambda url, label: challenge)

        with pytest.raises(scrape.ResultsUnavailable):
            scrape.fetch_results("123", "League A")

    def test_a_genuinely_empty_league_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(scrape, "_fetch_page", lambda url, label: "<html>no rows yet</html>")
        monkeypatch.setattr(scrape, "parse_results", lambda html: [])

        assert scrape.fetch_results("123", "League A") == []

    def test_no_browser_render_is_attempted(self, monkeypatch):
        # Headless Chromium receives the same challenge page, so rendering only
        # costs a browser launch per league per run.
        rendered = []
        monkeypatch.setattr(scrape, "_fetch_page", lambda url, label: "<html>rows</html>")
        monkeypatch.setattr(scrape, "parse_results", lambda html: [self._result()])
        monkeypatch.setattr(
            scrape, "_fetch_page_js", lambda url, label: rendered.append(url) or "",
        )

        scrape.fetch_results("123", "League A")

        assert rendered == []


# ---------------------------------------------------------------------------
# results_unavailable — an empty results array that says why
# ---------------------------------------------------------------------------

class TestResultsUnavailableFlag:

    def test_club_feed_flags_a_league_that_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scrape, "FEEDS_DIR", tmp_path / "feeds")
        scrape.write_club_feed(
            "East Leake", "east-leake",
            [_row("m1", "East Leake Robins", "home")], [],
            "2026-09-16T12:00:00Z", results_unavailable=True,
        )

        payload = json.loads(
            (tmp_path / "feeds" / "clubs" / "east-leake.json").read_text(encoding="utf-8")
        )
        assert payload["results_unavailable"] is True
        assert payload["results"] == []

    def test_absent_when_results_were_reachable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scrape, "FEEDS_DIR", tmp_path / "feeds")
        scrape.write_club_feed(
            "East Leake", "east-leake",
            [_row("m1", "East Leake Robins", "home")], [],
            "2026-09-16T12:00:00Z",
        )

        payload = json.loads(
            (tmp_path / "feeds" / "clubs" / "east-leake.json").read_text(encoding="utf-8")
        )
        assert "results_unavailable" not in payload

    def test_main_flags_every_feed_when_the_results_page_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scrape, "FEEDS_DIR", tmp_path / "feeds")
        monkeypatch.setattr(scrape, "OUTPUT_DIR", tmp_path / "calendars")
        monkeypatch.setattr(scrape, "LEAGUES", [("111", "League A")])
        monkeypatch.setattr(
            scrape, "fetch_fixtures",
            lambda season, league: [
                Fixture("11/10/26", "15:00", "East Leake Robins",
                        "East Leake Robins Reserves", "Costock Road", "Division One"),
            ],
        )

        def refused(season, league, browser=None):
            raise scrape.ResultsUnavailable(f"{league}: results page refused")

        monkeypatch.setattr(scrape, "fetch_results", refused)

        assert scrape.main() == 0

        club = json.loads(
            (tmp_path / "feeds" / "clubs" / "east-leake.json").read_text(encoding="utf-8")
        )
        league = json.loads(
            (tmp_path / "feeds" / "league-a" / "results.json").read_text(encoding="utf-8")
        )
        team = json.loads(
            (tmp_path / "feeds" / "league-a" / "teams" / "east-leake-robins.json").read_text(
                encoding="utf-8"
            )
        )
        assert club["results_unavailable"] is True
        assert league["results_unavailable"] is True
        assert team["results_unavailable"] is True


# ---------------------------------------------------------------------------
# Borrowing the operator's own browser session
# ---------------------------------------------------------------------------

class TestSessionCookies:

    def test_parses_a_cookie_header_as_a_browser_sends_it(self, monkeypatch):
        monkeypatch.setenv(
            scrape.COOKIE_ENV, "cf_clearance=abc123; JSESSIONID=xyz; spaced = value "
        )

        assert scrape._session_cookies() == {
            "cf_clearance": "abc123", "JSESSIONID": "xyz", "spaced": "value",
        }

    def test_unset_means_no_cookies(self, monkeypatch):
        monkeypatch.delenv(scrape.COOKIE_ENV, raising=False)

        assert scrape._session_cookies() == {}

    def test_blank_and_malformed_parts_are_skipped(self, monkeypatch):
        monkeypatch.setenv(scrape.COOKIE_ENV, "; novalue; =orphan; good=yes;")

        assert scrape._session_cookies() == {"good": "yes"}


# ---------------------------------------------------------------------------
# fetch_results — falling back to a browser session
# ---------------------------------------------------------------------------

class _FakeBrowser:
    """Stands in for BrowserSession, recording whether it was ever used."""

    def __init__(self, html="", raises=None):
        self.html = html
        self.raises = raises
        self.calls = []

    def fetch(self, url, wait_selector=None):
        self.calls.append(url)
        if self.raises:
            raise self.raises
        return self.html


CHALLENGE = "<html><head><title>Just a moment...</title></head></html>"
BLOCK = "<html><head><title>Attention Required!</title></head></html>"
ROWS = "<html><td class='home-team'>A</td></html>"


class TestFetchResultsViaBrowser:

    @staticmethod
    def _result():
        return scrape.Result(
            date="12/09/26", time="15:00", home_team="East Leake Robins",
            away_team="Awsworth Villa", home_score=3, away_score=1,
            venue="Costock Road", division_label="Division One",
        )

    def test_plain_fetch_working_leaves_the_browser_untouched(self, monkeypatch):
        # The expensive path must stay unused when the cheap one answers —
        # that is what makes the session lazy rather than a launch per league.
        monkeypatch.setattr(scrape, "_fetch_page", lambda url, label: ROWS)
        monkeypatch.setattr(scrape, "parse_results", lambda html: [self._result()])
        browser = _FakeBrowser(html=ROWS)

        assert len(scrape.fetch_results("123", "League A", browser=browser)) == 1
        assert browser.calls == []

    def test_empty_league_is_not_a_refusal(self, monkeypatch):
        monkeypatch.setattr(scrape, "_fetch_page", lambda url, label: "<html>no rows</html>")
        monkeypatch.setattr(scrape, "parse_results", lambda html: [])
        browser = _FakeBrowser()

        assert scrape.fetch_results("123", "League A", browser=browser) == []
        assert browser.calls == []

    def test_challenge_page_falls_back_to_the_browser(self, monkeypatch):
        monkeypatch.setattr(scrape, "_fetch_page", lambda url, label: CHALLENGE)
        monkeypatch.setattr(
            scrape, "parse_results",
            lambda html: [self._result()] if "home-team" in html else [],
        )
        browser = _FakeBrowser(html=ROWS)

        results = scrape.fetch_results("123", "League A", browser=browser)

        assert len(results) == 1
        assert len(browser.calls) == 1
        assert "selectedSeason=123" in browser.calls[0]

    def test_refused_fetch_falls_back_to_the_browser(self, monkeypatch):
        def blocked(url, label):
            raise RuntimeError("HTTP Error 403")

        monkeypatch.setattr(scrape, "_fetch_page", blocked)
        monkeypatch.setattr(
            scrape, "parse_results",
            lambda html: [self._result()] if "home-team" in html else [],
        )
        browser = _FakeBrowser(html=ROWS)

        assert len(scrape.fetch_results("123", "League A", browser=browser)) == 1
        assert len(browser.calls) == 1

    def test_browser_challenged_too_raises_results_unavailable(self, monkeypatch):
        monkeypatch.setattr(scrape, "_fetch_page", lambda url, label: BLOCK)
        browser = _FakeBrowser(html=CHALLENGE)

        with pytest.raises(scrape.ResultsUnavailable, match="no saved page was found"):
            scrape.fetch_results("123", "League A", browser=browser)

    def test_no_browser_available_raises_results_unavailable(self, monkeypatch):
        monkeypatch.setattr(scrape, "_fetch_page", lambda url, label: BLOCK)

        with pytest.raises(scrape.ResultsUnavailable, match="no saved page was found"):
            scrape.fetch_results("123", "League A", browser=None)

    def test_unstartable_browser_raises_results_unavailable(self, monkeypatch):
        from browser import BrowserUnavailable

        monkeypatch.setattr(scrape, "_fetch_page", lambda url, label: BLOCK)
        browser = _FakeBrowser(raises=BrowserUnavailable("no Xvfb"))

        with pytest.raises(scrape.ResultsUnavailable, match="no saved page was found"):
            scrape.fetch_results("123", "League A", browser=browser)


class TestResultsBrowserSwitch:

    def test_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("RESULTS_BROWSER", raising=False)

        assert scrape._results_browser() is not None

    @pytest.mark.parametrize("value", ["0", "false", "no"])
    def test_can_be_turned_off(self, monkeypatch, value):
        monkeypatch.setenv("RESULTS_BROWSER", value)

        assert scrape._results_browser() is None

    def test_main_closes_the_session_even_when_the_run_fails(self, monkeypatch):
        closed = []

        class _Session:
            def close(self):
                closed.append(True)

        monkeypatch.setattr(scrape, "_results_browser", lambda: _Session())

        def explode(holder):
            holder.append(scrape._results_browser())
            raise RuntimeError("scrape blew up")

        monkeypatch.setattr(scrape, "_run", explode)

        with pytest.raises(RuntimeError, match="scrape blew up"):
            scrape.main()
        assert closed == [True]


# ---------------------------------------------------------------------------
# _fetch_page — a refusal is not retried like a transient failure
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code, text="body"):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP Error {self.status_code}: ")


class _FakeSession:
    """Mimics curl_cffi's Session for the retry loop only."""

    def __init__(self, responses, attempts):
        self._responses = responses
        self._attempts = attempts
        self.headers = {}
        self.cookies = type("Jar", (), {"set": lambda *a, **k: None})()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, timeout=None):
        self._attempts.append(url)
        index = min(len(self._attempts) - 1, len(self._responses) - 1)
        return self._responses[index]


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(scrape.time, "sleep", lambda s: None)


class TestFetchPageRetries:

    @staticmethod
    def _install(monkeypatch, responses, attempts):
        monkeypatch.setattr(
            scrape.curl_requests, "Session",
            lambda **kw: _FakeSession(responses, attempts),
        )

    def test_a_refusal_is_tried_twice_not_five_times(self, monkeypatch, no_sleep):
        attempts = []
        self._install(monkeypatch, [_FakeResponse(403)], attempts)

        with pytest.raises(RuntimeError, match="403"):
            scrape._fetch_page("https://example.test/results", "results")

        assert len(attempts) == scrape.REFUSAL_RETRIES == 2

    def test_a_transient_failure_still_gets_the_full_budget(self, monkeypatch, no_sleep):
        attempts = []
        self._install(monkeypatch, [_FakeResponse(503)], attempts)

        with pytest.raises(RuntimeError, match="503"):
            scrape._fetch_page("https://example.test/results", "results")

        assert len(attempts) == scrape.HTTP_RETRIES == 5

    def test_an_intermittent_refusal_is_still_recovered(self, monkeypatch, no_sleep):
        # One retry is the point of not cutting straight to a single attempt.
        attempts = []
        self._install(
            monkeypatch, [_FakeResponse(403), _FakeResponse(200, "rows")], attempts
        )

        assert scrape._fetch_page("https://example.test/results", "results") == "rows"
        assert len(attempts) == 2

    def test_success_first_time_makes_one_request(self, monkeypatch, no_sleep):
        attempts = []
        self._install(monkeypatch, [_FakeResponse(200, "rows")], attempts)

        assert scrape._fetch_page("https://example.test/f", "fixtures") == "rows"
        assert len(attempts) == 1


# ---------------------------------------------------------------------------
# RESULTS_SESSION — supplying your own fetcher
# ---------------------------------------------------------------------------

class _CustomSession:
    """A stand-in for a user-supplied fetcher, with no `fetches` counter."""

    built = 0

    def __init__(self):
        type(self).built += 1
        self.urls = []

    def fetch(self, url, wait_selector=None):
        self.urls.append(url)
        return ROWS

    def close(self):
        pass


class TestResultsSessionPlugin:

    def test_named_session_is_built_instead_of_the_bundled_one(self, monkeypatch):
        monkeypatch.setenv("RESULTS_SESSION", f"{__name__}:_CustomSession")
        before = _CustomSession.built

        session = scrape._results_browser()

        assert isinstance(session, _CustomSession)
        assert _CustomSession.built == before + 1

    def test_bundled_session_is_the_default(self, monkeypatch):
        monkeypatch.delenv("RESULTS_SESSION", raising=False)
        monkeypatch.delenv("RESULTS_BROWSER", raising=False)

        from browser import BrowserSession

        assert isinstance(scrape._results_browser(), BrowserSession)

    def test_turning_the_browser_off_beats_a_named_session(self, monkeypatch):
        monkeypatch.setenv("RESULTS_SESSION", f"{__name__}:_CustomSession")
        monkeypatch.setenv("RESULTS_BROWSER", "0")

        assert scrape._results_browser() is None

    @pytest.mark.parametrize("spec", ["nocolon", ":missing_module", "module:"])
    def test_a_malformed_spec_is_rejected_clearly(self, spec):
        with pytest.raises(ValueError, match="module:attribute"):
            scrape._load_results_session(spec)

    def test_a_session_without_a_fetches_counter_still_works(self, monkeypatch):
        # The counter is only used for the run summary; requiring it would make
        # the protocol harder to satisfy than it needs to be.
        monkeypatch.setattr(scrape, "_fetch_page", lambda url, label: BLOCK)
        monkeypatch.setattr(
            scrape, "parse_results",
            lambda html: [scrape.Result("12/09/26", "15:00", "A", "B", 1, 0, "G", "D")],
        )
        session = _CustomSession()

        results = scrape.fetch_results("123", "League A", browser=session)

        assert len(results) == 1
        assert not hasattr(session, "fetches")

    def test_main_reports_a_custom_session_without_a_counter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scrape, "FEEDS_DIR", tmp_path / "feeds")
        monkeypatch.setattr(scrape, "OUTPUT_DIR", tmp_path / "calendars")
        monkeypatch.setattr(scrape, "LEAGUES", [("111", "League A")])
        monkeypatch.setattr(scrape, "_results_browser", lambda: _CustomSession())
        monkeypatch.setattr(
            scrape, "fetch_fixtures",
            lambda season, league: [
                Fixture("11/10/26", "15:00", "Arnold Town", "Cotgrave", "G", "Division One")
            ],
        )
        monkeypatch.setattr(
            scrape, "fetch_results",
            lambda season, league, browser=None: [
                scrape.Result("12/09/26", "15:00", "Arnold Town", "Cotgrave",
                              2, 1, "G", "Division One")
            ],
        )

        assert scrape.main() == 0
        payload = json.loads(
            (tmp_path / "feeds" / "league-a" / "results.json").read_text(encoding="utf-8")
        )
        assert len(payload["results"]) == 1


# ---------------------------------------------------------------------------
# Saved pages — parsing what a browser already loaded
# ---------------------------------------------------------------------------

def _saved_page(directory, season_id, rows=1, name="Results.html"):
    """A stand-in for a page saved from a browser, carrying its season marker."""
    body = "".join(
        f"<tr><td class='left'>1{i}/09/26 15:00</td>"
        f"<td class='home-team'>East Leake Robins</td>"
        f"<td class='score'>{i} - 0</td>"
        f"<td class='road-team'>Cotgrave</td>"
        f"<td class='left'>Division One</td></tr>"
        for i in range(1, rows + 1)
    )
    path = directory / name
    path.write_text(
        f"<html><body><a href='/results/1/100000.html?selectedSeason={season_id}'>next</a>"
        f"<table>{body}</table></body></html>",
        encoding="utf-8",
    )
    return path


class TestSavedResultsPage:

    def test_a_page_is_matched_by_its_season_not_its_filename(self, tmp_path, monkeypatch):
        # A browser names a saved page after its title, so the filename cannot
        # be relied on to say which league it is.
        monkeypatch.setenv(scrape.RESULTS_HTML_DIR_ENV, str(tmp_path))
        _saved_page(tmp_path, "918978398", name="Full-Time  Results.html")

        found = scrape._saved_results_page("918978398")

        assert found is not None
        assert "selectedSeason=918978398" in found[0]

    def test_another_league_is_not_matched(self, tmp_path, monkeypatch):
        monkeypatch.setenv(scrape.RESULTS_HTML_DIR_ENV, str(tmp_path))
        _saved_page(tmp_path, "918978398")

        assert scrape._saved_results_page("71450136") is None

    def test_missing_directory_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv(scrape.RESULTS_HTML_DIR_ENV, str(tmp_path / "nope"))

        assert scrape._saved_results_page("918978398") is None

    def test_the_freshest_of_several_saves_wins(self, tmp_path, monkeypatch):
        import os
        import time

        monkeypatch.setenv(scrape.RESULTS_HTML_DIR_ENV, str(tmp_path))
        old = _saved_page(tmp_path, "918978398", rows=1, name="old.html")
        new = _saved_page(tmp_path, "918978398", rows=3, name="new.html")
        week_ago = time.time() - 7 * 86400
        os.utime(old, (week_ago, week_ago))

        html, age_days = scrape._saved_results_page("918978398")

        assert len(scrape.parse_results(html)) == 3
        assert age_days < 1


class TestFetchResultsFromSavedPage:

    def test_a_saved_page_is_used_when_everything_else_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv(scrape.RESULTS_HTML_DIR_ENV, str(tmp_path))
        _saved_page(tmp_path, "918978398", rows=2)
        monkeypatch.setattr(scrape, "_fetch_page", lambda url, label: BLOCK)

        results = scrape.fetch_results("918978398", "Euro Soccer", browser=None)

        assert len(results) == 2
        assert "saved page" in scrape.LAST_SOURCE

    def test_a_working_fetch_is_preferred_over_a_saved_page(self, tmp_path, monkeypatch):
        # A saved page is a fallback, not a cache: live data is fresher.
        monkeypatch.setenv(scrape.RESULTS_HTML_DIR_ENV, str(tmp_path))
        _saved_page(tmp_path, "918978398", rows=9)
        monkeypatch.setattr(scrape, "_fetch_page", lambda url, label: ROWS)
        monkeypatch.setattr(
            scrape, "parse_results",
            lambda html: [scrape.Result("12/09/26", "15:00", "A", "B", 1, 0, "G", "D")],
        )

        results = scrape.fetch_results("918978398", "Euro Soccer", browser=None)

        assert len(results) == 1
        assert scrape.LAST_SOURCE == "a plain fetch"

    def test_the_browser_is_preferred_over_a_saved_page(self, tmp_path, monkeypatch):
        monkeypatch.setenv(scrape.RESULTS_HTML_DIR_ENV, str(tmp_path))
        _saved_page(tmp_path, "918978398", rows=9)
        monkeypatch.setattr(scrape, "_fetch_page", lambda url, label: BLOCK)
        browser = _FakeBrowser(html=ROWS)
        monkeypatch.setattr(
            scrape, "parse_results",
            lambda html: [scrape.Result("12/09/26", "15:00", "A", "B", 1, 0, "G", "D")],
        )

        results = scrape.fetch_results("918978398", "Euro Soccer", browser=browser)

        assert len(results) == 1
        assert scrape.LAST_SOURCE == "the browser"

    def test_no_saved_page_still_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv(scrape.RESULTS_HTML_DIR_ENV, str(tmp_path))
        monkeypatch.setattr(scrape, "_fetch_page", lambda url, label: BLOCK)

        with pytest.raises(scrape.ResultsUnavailable, match="no saved page was found"):
            scrape.fetch_results("918978398", "Euro Soccer", browser=None)
