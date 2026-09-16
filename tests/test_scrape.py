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
    parse_fixtures_page,
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
                return [self._fixture()], []
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
# The Full-Time table — the markup both the fixtures and results pages use
# ---------------------------------------------------------------------------

_TABLE_HEADER = (
    "<tr><th>Type</th><th>Date</th><th>Home Team</th><th></th><th></th>"
    "<th></th><th>Away Team</th><th>Venue</th><th>Competition</th>"
    "<th>Notes</th></tr>"
)


def _table_row(date, home, score, away, venue, competition):
    """One Full-Time row: date and time run together, venue before competition."""
    return f"""<tr>
      <td class="color-dark-grey bold cell-divider">L</td>
      <td class="left cell-divider">{date}</td>
      <td class="home-team">{home}</td>
      <td class="team-logo"></td>
      <td class="score">{score}</td>
      <td class="team-logo"></td>
      <td class="road-team">{away}</td>
      <td class="left cell-divider">{venue}</td>
      <td class="left cell-divider">{competition}</td>
      <td class="status-notes"></td>
    </tr>"""


def _table_page(*rows):
    return f"<html><body><table>{_TABLE_HEADER}{''.join(rows)}</table></body></html>"


class TestParseResultsTableLayout:
    """The results page is the same table as the fixtures page."""

    def test_venue_and_division_not_swapped(self):
        """Bug: the table orders venue then competition, and both were read
        the other way round — putting the ground in `division` and losing the
        age group that decides whether a match may be published at all."""
        html = _table_page(_table_row(
            "13/09/2610:00", "Arnold Town Blue U14", "3 - 1", "Opponent FC U14",
            "EAST LEAKE PLAYING FIELDS LE12 6LY", "U14 Sunday Division 1",
        ))
        results = parse_results(html)
        assert len(results) == 1
        assert results[0].venue == "EAST LEAKE PLAYING FIELDS LE12 6LY"
        assert results[0].division_label == "U14 Sunday Division 1"

    def test_reads_every_row_of_a_multi_row_table(self):
        html = _table_page(
            _table_row("13/09/2610:00", "A FC", "3 - 1", "B FC", "Ground A", "Div 1"),
            _table_row("13/09/2612:00", "C FC", "0 - 0", "D FC", "Ground B", "Div 2"),
            _table_row("13/09/2614:00", "E FC", "2 - 5", "F FC", "Ground C", "Div 3"),
        )
        assert [(r.home_score, r.away_score) for r in parse_results(html)] == [
            (3, 1), (0, 0), (2, 5),
        ]

    def test_withheld_score_survives_as_a_scoreless_result(self):
        """X - X means the match was played and the score is not published."""
        html = _table_page(_table_row(
            "13/09/2610:00", "Arnold Town Blue U9", "X - X", "Opponent FC U9",
            "The Ground", "U9 Sunday",
        ))
        results = parse_results(html)
        assert len(results) == 1
        assert results[0].home_score is None and results[0].away_score is None

    def test_unplayed_row_is_not_a_result(self):
        html = _table_page(_table_row(
            "20/09/2615:00", "A FC", "VS", "B FC", "The Ground", "Division One",
        ))
        assert parse_results(html) == []

    def test_header_row_is_not_a_result(self):
        assert parse_results(_table_page()) == []


class TestParseFixturesPage:
    """A played match left on the fixture list is a result, not a fixture."""

    def test_unplayed_rows_are_fixtures(self):
        html = _table_page(_table_row(
            "20/09/2615:00", "Cotgrave Reserves", "VS", "East Leake Robins",
            "COTGRAVE WELFARE CLUB #1", "Division One",
        ))
        fixtures, played = parse_fixtures_page(html)
        assert played == []
        assert len(fixtures) == 1
        assert fixtures[0].venue == "COTGRAVE WELFARE CLUB #1"
        assert fixtures[0].division_label == "Division One"

    def test_scored_row_comes_back_as_a_result(self):
        html = _table_page(_table_row(
            "13/09/2610:00", "A FC U14", "3 - 1", "B FC U14", "The Ground", "U14 Div 1",
        ))
        fixtures, played = parse_fixtures_page(html)
        assert fixtures == []
        assert [(r.home_score, r.away_score) for r in played] == [(3, 1)]

    def test_withheld_score_comes_back_as_a_played_match(self):
        """At U11 and below the score is never published, so a league may
        leave the match on its fixture list showing X - X forever."""
        html = _table_page(_table_row(
            "13/09/2610:00", "A FC U9", "X - X", "B FC U9", "The Ground", "U9 Sunday",
        ))
        fixtures, played = parse_fixtures_page(html)
        assert fixtures == []
        assert len(played) == 1
        assert played[0].home_score is None and played[0].away_score is None

    def test_numbers_elsewhere_in_the_row_do_not_make_a_match_played(self):
        """Only the score cell decides — a venue or division with a dash in it
        must not take a real fixture out of the calendar."""
        html = _table_page(_table_row(
            "20/09/2615:00", "A FC", "VS", "B FC",
            "Pitch 3-4, Meadow Lane", "Div 1 (2025-26)",
        ))
        fixtures, played = parse_fixtures_page(html)
        assert played == []
        assert len(fixtures) == 1

    def test_parse_fixtures_returns_only_upcoming(self):
        html = _table_page(
            _table_row("13/09/2610:00", "A FC", "3 - 1", "B FC", "Ground", "Div 1"),
            _table_row("20/09/2610:00", "C FC", "VS", "D FC", "Ground", "Div 1"),
        )
        assert [f.home_team for f in parse_fixtures(html)] == ["C FC"]


# ---------------------------------------------------------------------------
# Fetching — URLs and fallbacks
# ---------------------------------------------------------------------------

class TestResultsUrl:

    def test_asks_for_the_whole_season(self):
        """Without a date filter the results page answers for one period only,
        which is how a season of played matches comes back empty."""
        url = scrape._results_url("918978398")
        assert "selectedSeason=918978398" in url
        assert "selectedDateCode=all" in url

    def test_date_code_can_be_dropped(self):
        assert "selectedDateCode" not in scrape._results_url("123", date_code="")


class TestFetchResultsFallbacks:
    """Each way of asking is tried until one produces rows."""

    _RESULT_PAGE = _table_page(_table_row(
        "13/09/2610:00", "A FC U14", "3 - 1", "B FC U14", "The Ground", "U14 Div 1",
    ))

    def test_stops_at_the_first_attempt_that_works(self, monkeypatch):
        calls = []

        def fake_static(url, label):
            calls.append(url)
            return self._RESULT_PAGE

        monkeypatch.setattr(scrape, "_fetch_page", fake_static)
        monkeypatch.setattr(scrape, "_fetch_page_js", lambda url, label: pytest.fail(
            "browser render should not run when the static fetch has rows"
        ))

        assert len(scrape.fetch_results("123", "League A")) == 1
        assert len(calls) == 1

    def test_falls_back_to_a_browser_when_the_static_page_has_no_rows(self, monkeypatch):
        """Full-Time renders some pages client-side: a static fetch of one is
        not an error, it is a page with no match rows in it."""
        rendered = []

        monkeypatch.setattr(scrape, "_fetch_page", lambda url, label: "<html><body></body></html>")

        def fake_js(url, label):
            rendered.append(url)
            return self._RESULT_PAGE

        monkeypatch.setattr(scrape, "_fetch_page_js", fake_js)

        assert len(scrape.fetch_results("123", "League A")) == 1
        assert rendered, "browser render was never tried"

    def test_a_failing_fetch_does_not_abandon_the_remaining_attempts(self, monkeypatch):
        def fake_static(url, label):
            raise RuntimeError("HTTP Error 403")

        monkeypatch.setattr(scrape, "_fetch_page", fake_static)
        monkeypatch.setattr(scrape, "_fetch_page_js", lambda url, label: self._RESULT_PAGE)

        assert len(scrape.fetch_results("123", "League A")) == 1

    def test_returns_empty_when_nothing_works(self, monkeypatch):
        monkeypatch.setattr(scrape, "_fetch_page", lambda url, label: "")
        monkeypatch.setattr(scrape, "_fetch_page_js", lambda url, label: "")

        assert scrape.fetch_results("123", "League A") == []


class TestFetchFixturesFallback:

    def test_falls_back_to_a_browser_when_the_static_page_has_no_rows(self, monkeypatch):
        page = _table_page(_table_row(
            "20/09/2615:00", "A FC", "VS", "B FC", "The Ground", "Div 1",
        ))
        monkeypatch.setattr(scrape, "_fetch_page", lambda url, label: "<html></html>")
        monkeypatch.setattr(scrape, "_fetch_page_js", lambda url, label: page)

        fixtures, played = scrape.fetch_fixtures("123", "League A")
        assert len(fixtures) == 1 and played == []


class TestMergeResults:

    @staticmethod
    def _result(home, score=1):
        return scrape.Result(
            date="13/09/26", time="10:00", home_team=home, away_team="B FC",
            home_score=score, away_score=0, venue="Ground", division_label="Div 1",
        )

    def test_keeps_both_sources(self):
        merged = scrape.merge_results([self._result("A FC")], [self._result("C FC")])
        assert [r.home_team for r in merged] == ["A FC", "C FC"]

    def test_results_page_wins_a_duplicate(self):
        merged = scrape.merge_results([self._result("A FC", 3)], [self._result("A FC", 9)])
        assert [r.home_score for r in merged] == [3]


# ---------------------------------------------------------------------------
# main() — a played match has to reach the feeds
# ---------------------------------------------------------------------------

class TestPlayedMatchesReachTheFeeds:
    """The whole point of scraping results: a season that has started must not
    read as a season that has not."""

    LEAGUE = "YEL East Midlands Sunday 26/27"

    def _run(self, monkeypatch, tmp_path, *, results, fixtures, played):
        monkeypatch.setattr(scrape, "FEEDS_DIR", tmp_path / "feeds")
        monkeypatch.setattr(scrape, "OUTPUT_DIR", tmp_path / "calendars")
        monkeypatch.setattr(scrape, "LEAGUES", [("111", self.LEAGUE)])
        monkeypatch.setattr(scrape, "restore_league_from_bucket", lambda n, s: 0)
        monkeypatch.setattr(scrape, "fetch_fixtures", lambda s, l: (fixtures, played))
        monkeypatch.setattr(scrape, "fetch_results", lambda s, l: results)
        scrape.main()
        return json.loads(
            (tmp_path / "feeds" / "clubs" / "east-leake.json").read_text(encoding="utf-8")
        )

    @staticmethod
    def _result(home, away, home_score, away_score, division, date="13/09/26"):
        return scrape.Result(
            date=date, time="10:00", home_team=home, away_team=away,
            home_score=home_score, away_score=away_score,
            venue="East Leake Playing Fields", division_label=division,
        )

    @staticmethod
    def _fixture(home, away, division, date="20/09/26"):
        return Fixture(
            date=date, time="15:00", home_team=home, away_team=away,
            venue="Cotgrave Welfare", division_label=division,
        )

    def test_open_age_result_is_published_with_its_score(self, tmp_path, monkeypatch):
        club = self._run(
            monkeypatch, tmp_path,
            results=[self._result("East Leake Robins", "Cotgrave Reserves", 2, 1, "Division One")],
            fixtures=[self._fixture("East Leake Robins B", "Cotgrave Reserves", "Division Two")],
            played=[],
        )
        assert [(r["goals_for"], r["goals_against"]) for r in club["results"]] == [(2, 1)]

    def test_restricted_result_becomes_a_participation_record(self, tmp_path, monkeypatch):
        club = self._run(
            monkeypatch, tmp_path,
            results=[self._result("East Leake Bantams Black U9", "Priory Celtic U9", None, None, "U9 Sunday")],
            fixtures=[self._fixture("East Leake Robins", "Cotgrave Reserves", "Division One")],
            played=[],
        )
        assert club["results"] == []
        assert len(club["participation"]) == 1
        record = club["participation"][0]
        assert record["team"] == "East Leake Bantams Black U9"
        assert record["age_group"] == "U9"
        assert record["played"] is True
        assert record["league"] == self.LEAGUE
        # Nothing that says who it was against, where, or how it went.
        assert not {"opponent", "venue", "home_score", "away_score"} & set(record)

    def test_match_played_but_left_on_the_fixture_list_still_counts(self, tmp_path, monkeypatch):
        """A league that never moves a U10 match onto its results page used to
        leave it advertised as an upcoming fixture for the rest of the season."""
        club = self._run(
            monkeypatch, tmp_path,
            results=[],
            fixtures=[self._fixture("East Leake Robins", "Cotgrave Reserves", "Division One")],
            played=[self._result(
                "East Leake Bantams Orange U10", "Someone U10", None, None,
                "U10 Sunday", date="06/09/26",
            )],
        )
        assert [f["date"] for f in club["fixtures"]] == ["2026-09-20"]
        assert [p["date"] for p in club["participation"]] == ["2026-09-06"]

    def test_a_league_with_no_results_is_reported(self, tmp_path, monkeypatch, caplog):
        with caplog.at_level("ERROR"):
            self._run(
                monkeypatch, tmp_path,
                results=[],
                fixtures=[self._fixture("East Leake Robins", "Cotgrave Reserves", "Division One")],
                played=[],
            )
        assert any("NO RESULTS SCRAPED" in r.message for r in caplog.records)


class TestScoreIsNotReadFromTheRestOfTheRow:
    """A dash in a venue or a division name is not a score."""

    def test_unplayed_row_with_dashes_around_it_is_not_a_result(self):
        html = _table_page(_table_row(
            "20/09/2615:00", "A FC", "VS", "B FC",
            "Pitch 3-4, Meadow Lane", "Div 1 (2025-26)",
        ))
        assert parse_results(html) == []

    def test_score_is_still_found_when_the_cell_is_not_a_sibling(self):
        """Nested layouts keep working — the score is named, just not adjacent."""
        html = """<html><body><div class="fixture">
          <div class="date">22/03/26 12:30</div>
          <div class="teams"><span class="home-team">A FC</span></div>
          <div class="result"><span class="score">4 - 2</span></div>
          <div class="teams"><span class="road-team">B FC</span></div>
          <div class="competition">Div 1</div>
        </div></body></html>"""
        results = parse_results(html)
        assert len(results) == 1
        assert (results[0].home_score, results[0].away_score) == (4, 2)
        # The division is one level out from the away cell, not beside it.
        assert results[0].division_label == "Div 1"
