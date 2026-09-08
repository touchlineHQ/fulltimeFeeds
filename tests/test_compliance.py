"""
Unit tests for the U11-and-below publication rules.

Covers scraper/compliance.py directly, plus the four places its output reaches
a public URL: league feeds, team feeds, club feeds and .ics calendars.

Run with: pytest tests/
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from compliance import (
    OPPONENT_LABEL,
    age_group,
    compliance_meta,
    is_restricted,
    is_row_restricted,
    participation_record,
    redact_fixture,
    safe_fixtures,
    safe_results,
    split_results,
)
from scrape import (
    Fixture,
    Result,
    fixtures_to_ics,
    write_club_feed,
    write_league_feed,
    write_team_feed,
)
import scrape


GENERATED = "2026-09-08T06:00:00Z"


# ---------------------------------------------------------------------------
# age_group / is_restricted
# ---------------------------------------------------------------------------

class TestAgeGroup:
    """Reading the age group out of team and division names."""

    @pytest.mark.parametrize("text, expected", [
        ("Arnold Town Blue U11", 11),
        ("Demo FC U7", 7),
        ("Quorn U10s", 10),
        ("U9 Girls", 9),
        ("Under 10 Division 1", 10),
        ("Under-8 Sunday", 8),
        ("AFC Chellaston Rapids U13", 13),
        ("Demo FC U18", 18),
        # No age token at all — adult football.
        ("Demo FC", None),
        ("Euro Soccer Nottinghamshire Senior League", None),
        ("Westfield United Reserves", None),
        ("", None),
        (None, None),
    ])
    def test_single_text(self, text, expected):
        assert age_group(text) == expected

    def test_u1_does_not_swallow_u11(self):
        """\\b alone would let 'U1' match the front of 'U11'."""
        assert age_group("Team U11") == 11

    def test_youngest_wins_across_texts(self):
        """A disagreement between name and division resolves to the safer age."""
        assert age_group("Demo FC U18", "U11 Division 1") == 11

    def test_ordinary_club_names_are_not_ages(self):
        assert age_group("Underwood Villa") is None
        assert age_group("Southbrook Colts") is None


class TestIsRestricted:
    """U11 and below is restricted; U12 and above, and adults, are not."""

    @pytest.mark.parametrize("age_token", ["U6", "U7", "U8", "U9", "U10", "U11"])
    def test_u11_and_below(self, age_token):
        assert is_restricted(f"Demo FC {age_token}") is True

    @pytest.mark.parametrize("age_token", ["U12", "U13", "U15", "U18"])
    def test_u12_and_above(self, age_token):
        assert is_restricted(f"Demo FC {age_token}") is False

    def test_adult_team(self):
        assert is_restricted("Demo FC", "Senior Men's League") is False

    def test_restricted_if_either_side_is_young(self):
        """A U12 side in a U11 competition is still protected."""
        assert is_restricted("Demo FC U12", "Opponent U11", "U11 Cup") is True


# ---------------------------------------------------------------------------
# Row-level redaction
# ---------------------------------------------------------------------------

def _fixture_row(**overrides) -> dict:
    row = {
        "id": "abc123",
        "date": "2026-09-13",
        "time": "10:30",
        "home_team": "Demo FC U10",
        "away_team": "Riverside Rangers U10",
        "venue": "Demo FC Ground",
        "division": "U10 Sunday",
        "team": "Demo FC U10",
        "home_away": "home",
        "opponent": "Riverside Rangers U10",
    }
    row.update(overrides)
    return row


def _result_row(**overrides) -> dict:
    row = _fixture_row()
    row.update({
        "home_score": 4,
        "away_score": 2,
        "goals_for": 4,
        "goals_against": 2,
    })
    row.update(overrides)
    return row


class TestRedactFixture:

    def test_keeps_own_team_and_drops_opposition(self):
        out = redact_fixture(_fixture_row(), subject_team="Demo FC U10")
        assert out["home_team"] == "Demo FC U10"
        assert out["away_team"] == OPPONENT_LABEL
        assert out["opponent"] == OPPONENT_LABEL

    def test_keeps_own_team_when_away(self):
        row = _fixture_row(
            home_team="Riverside Rangers U10",
            away_team="Demo FC U10",
            home_away="away",
            opponent="Riverside Rangers U10",
        )
        out = redact_fixture(row, subject_team="Demo FC U10")
        assert out["home_team"] == OPPONENT_LABEL
        assert out["away_team"] == "Demo FC U10"

    def test_drops_venue(self):
        assert redact_fixture(_fixture_row(), "Demo FC U10")["venue"] == ""

    def test_keeps_date_time_and_division(self):
        out = redact_fixture(_fixture_row(), "Demo FC U10")
        assert out["date"] == "2026-09-13"
        assert out["time"] == "10:30"
        assert out["division"] == "U10 Sunday"
        assert out["home_away"] == "home"

    def test_flags_the_row(self):
        assert redact_fixture(_fixture_row(), "Demo FC U10")["publication_restricted"] is True

    def test_strips_any_score_fields(self):
        """Scores must not survive even if a result row is handed in by mistake."""
        out = redact_fixture(_result_row(), "Demo FC U10")
        for field in ("home_score", "away_score", "goals_for", "goals_against"):
            assert field not in out

    def test_anonymises_both_sides_without_a_subject_team(self):
        out = redact_fixture(_fixture_row(), subject_team=None)
        assert out["home_team"] == OPPONENT_LABEL
        assert out["away_team"] == OPPONENT_LABEL

    def test_does_not_mutate_the_input(self):
        row = _fixture_row()
        redact_fixture(row, "Demo FC U10")
        assert row["away_team"] == "Riverside Rangers U10"
        assert row["venue"] == "Demo FC Ground"


class TestSafeFixturesAndResults:

    def test_open_age_fixture_passes_through_untouched(self):
        row = _fixture_row(
            home_team="Demo FC U14", away_team="Riverside Rangers U14",
            division="U14 Sunday", team="Demo FC U14", opponent="Riverside Rangers U14",
        )
        assert safe_fixtures([row], "Demo FC U14") == [row]

    def test_restricted_fixture_is_redacted(self):
        out = safe_fixtures([_fixture_row()], "Demo FC U10")[0]
        assert out["opponent"] == OPPONENT_LABEL

    def test_restricted_results_are_withheld_not_redacted(self):
        rows, withheld = safe_results([_result_row()])
        assert rows == []
        assert withheld == 1

    def test_open_age_results_are_published(self):
        row = _result_row(
            home_team="Demo FC U14", away_team="Riverside Rangers U14",
            division="U14 Sunday", team="Demo FC U14", opponent="Riverside Rangers U14",
        )
        rows, withheld = safe_results([row])
        assert rows == [row]
        assert withheld == 0

    def test_is_row_restricted_reads_every_name_field(self):
        assert is_row_restricted(_fixture_row()) is True
        assert is_row_restricted({"division": "U11 Division 1"}) is True
        assert is_row_restricted({"opponent": "Riverside Rangers U9"}) is True
        assert is_row_restricted({"home_team": "Demo FC", "away_team": "Westfield United"}) is False

    def test_league_name_does_not_restrict_a_whole_league(self):
        """A league titled after one age group must not silence its older teams."""
        row = _fixture_row(
            home_team="Demo FC U16", away_team="Riverside Rangers U16",
            division="U16 Sunday", team="Demo FC U16", opponent="Riverside Rangers U16",
            league="U11 and Under Development League",
        )
        assert is_row_restricted(row) is False


class TestParticipationRecords:
    """Withholding a result is not the same as pretending the match never
    happened — the club may still say its U10s played."""

    def test_keeps_when_and_whether_it_was_home(self):
        entry = participation_record(_result_row())
        assert entry["date"] == "2026-09-13"
        assert entry["time"] == "10:30"
        assert entry["team"] == "Demo FC U10"
        assert entry["home_away"] == "home"
        assert entry["division"] == "U10 Sunday"
        assert entry["age_group"] == "U10"
        assert entry["played"] is True
        assert entry["publication_restricted"] is True

    def test_drops_everything_that_says_how_it_went(self):
        entry = participation_record(_result_row())
        for field in ("home_score", "away_score", "goals_for", "goals_against",
                      "home_team", "away_team", "opponent", "venue"):
            assert field not in entry

    def test_split_results_routes_each_row_to_one_lane(self):
        young = _result_row()
        open_age = _result_row(
            home_team="Demo FC U14", away_team="Riverside Rangers U14",
            division="U14 Sunday", team="Demo FC U14", opponent="Riverside Rangers U14",
        )
        results, participation = split_results([young, open_age])
        assert results == [open_age]
        assert [p["team"] for p in participation] == ["Demo FC U10"]

    def test_safe_results_still_reports_the_same_count(self):
        """safe_results is the older, count-only view of the same split."""
        rows = [_result_row(), _result_row()]
        publishable, withheld = safe_results(rows)
        results, participation = split_results(rows)
        assert publishable == results
        assert withheld == len(participation) == 2

    def test_does_not_mutate_the_input(self):
        row = _result_row()
        participation_record(row)
        assert row["home_score"] == 4
        assert row["opponent"] == "Riverside Rangers U10"


class TestComplianceMeta:

    def test_reports_the_threshold_and_withheld_count(self):
        meta = compliance_meta(7)
        assert meta["restricted_max_age_group"] == "U11"
        assert meta["results_withheld"] == 7
        assert "U11 and below" in meta["policy"]


# ---------------------------------------------------------------------------
# Feed writers
# ---------------------------------------------------------------------------

U10_FIXTURE = Fixture(
    date="13/09/26", time="10:30",
    home_team="Demo FC U10", away_team="Riverside Rangers U10",
    venue="Demo FC Ground", division_label="U10 Sunday",
)
U10_RESULT = Result(
    date="06/09/26", time="10:30",
    home_team="Demo FC U10", away_team="Riverside Rangers U10",
    home_score=4, away_score=2,
    venue="Demo FC Ground", division_label="U10 Sunday",
)
U14_FIXTURE = Fixture(
    date="13/09/26", time="11:00",
    home_team="Demo FC U14", away_team="Riverside Rangers U14",
    venue="Demo FC Ground", division_label="U14 Sunday",
)
U14_RESULT = Result(
    date="06/09/26", time="11:00",
    home_team="Demo FC U14", away_team="Riverside Rangers U14",
    home_score=1, away_score=3,
    venue="Demo FC Ground", division_label="U14 Sunday",
)


@pytest.fixture
def feeds_dir(tmp_path, monkeypatch):
    target = tmp_path / "feeds"
    monkeypatch.setattr(scrape, "FEEDS_DIR", target)
    return target


class TestWriteLeagueFeed:
    """A league-wide listing names two clubs per row, so restricted matches
    are withheld from it entirely rather than redacted."""

    def _write(self, feeds_dir):
        write_league_feed(
            "Demo League", "demo-league",
            [U10_FIXTURE, U14_FIXTURE], [U10_RESULT, U14_RESULT], GENERATED,
        )
        return (
            json.loads((feeds_dir / "demo-league" / "fixtures.json").read_text()),
            json.loads((feeds_dir / "demo-league" / "results.json").read_text()),
        )

    def test_restricted_fixture_withheld(self, feeds_dir):
        fixtures, _ = self._write(feeds_dir)
        assert [f["home_team"] for f in fixtures["fixtures"]] == ["Demo FC U14"]
        assert fixtures["compliance"]["fixtures_withheld"] == 1

    def test_restricted_result_withheld(self, feeds_dir):
        _, results = self._write(feeds_dir)
        assert [r["home_team"] for r in results["results"]] == ["Demo FC U14"]
        assert results["compliance"]["results_withheld"] == 1

    def test_open_age_result_keeps_its_score(self, feeds_dir):
        _, results = self._write(feeds_dir)
        assert results["results"][0]["home_score"] == 1
        assert results["results"][0]["away_score"] == 3

    def test_no_young_age_venue_or_opponent_anywhere(self, feeds_dir):
        fixtures, results = self._write(feeds_dir)
        blob = json.dumps(fixtures) + json.dumps(results)
        assert "Riverside Rangers U10" not in blob
        assert "U10" not in blob


class TestWriteTeamFeed:

    def _write(self, feeds_dir, team, fixture, result):
        write_team_feed(
            team, "team-slug", "Demo League", "demo-league",
            [fixture], [result], GENERATED,
        )
        return json.loads(
            (feeds_dir / "demo-league" / "teams" / "team-slug.json").read_text()
        )

    def test_restricted_team_publishes_no_results(self, feeds_dir):
        payload = self._write(feeds_dir, "Demo FC U10", U10_FIXTURE, U10_RESULT)
        assert payload["results"] == []
        assert payload["compliance"]["results_withheld"] == 1

    def test_withheld_result_survives_as_participation(self, feeds_dir):
        """Consumers list these instead of the result, so a young team's season
        does not simply vanish."""
        payload = self._write(feeds_dir, "Demo FC U10", U10_FIXTURE, U10_RESULT)
        assert len(payload["participation"]) == 1
        entry = payload["participation"][0]
        assert entry["team"] == "Demo FC U10"
        assert entry["date"] == "2026-09-06"
        assert entry["home_away"] == "home"
        assert entry["age_group"] == "U10"
        assert entry["played"] is True
        for field in ("home_score", "away_score", "opponent", "venue"):
            assert field not in entry

    def test_open_age_team_has_no_participation_entries(self, feeds_dir):
        payload = self._write(feeds_dir, "Demo FC U14", U14_FIXTURE, U14_RESULT)
        assert payload["participation"] == []

    def test_restricted_team_keeps_its_own_fixture_details(self, feeds_dir):
        payload = self._write(feeds_dir, "Demo FC U10", U10_FIXTURE, U10_RESULT)
        fixture = payload["fixtures"][0]
        assert fixture["home_team"] == "Demo FC U10"
        assert fixture["away_team"] == OPPONENT_LABEL
        assert fixture["opponent"] == OPPONENT_LABEL
        assert fixture["venue"] == ""
        assert fixture["date"] == "2026-09-13"
        assert fixture["time"] == "10:30"
        assert fixture["home_away"] == "home"

    def test_open_age_team_is_published_in_full(self, feeds_dir):
        payload = self._write(feeds_dir, "Demo FC U14", U14_FIXTURE, U14_RESULT)
        assert payload["fixtures"][0]["away_team"] == "Riverside Rangers U14"
        assert payload["fixtures"][0]["venue"] == "Demo FC Ground"
        assert payload["results"][0]["goals_for"] == 1
        assert payload["compliance"]["results_withheld"] == 0


class TestWriteClubFeed:
    """Club feed rows are already scoped to one of the club's own teams."""

    def _rows(self):
        fixture = {
            "id": "f1", "date": "2026-09-13", "time": "10:30",
            "home_team": "Demo FC U10", "away_team": "Riverside Rangers U10",
            "venue": "Demo FC Ground", "division": "U10 Sunday",
            "league": "Demo League", "team": "Demo FC U10",
            "home_away": "home", "opponent": "Riverside Rangers U10",
        }
        result = {**fixture, "id": "r1", "date": "2026-09-06",
                  "home_score": 4, "away_score": 2,
                  "goals_for": 4, "goals_against": 2}
        return [fixture], [result]

    def test_redacts_fixtures_and_withholds_results(self, feeds_dir):
        fixtures, results = self._rows()
        write_club_feed("Demo FC", "demo-fc", fixtures, results, GENERATED)
        payload = json.loads((feeds_dir / "clubs" / "demo-fc.json").read_text())

        assert payload["results"] == []
        assert payload["compliance"]["results_withheld"] == 1
        assert payload["fixtures"][0]["team"] == "Demo FC U10"
        assert payload["fixtures"][0]["away_team"] == OPPONENT_LABEL
        assert payload["fixtures"][0]["venue"] == ""

    def test_withheld_result_survives_as_participation(self, feeds_dir):
        fixtures, results = self._rows()
        write_club_feed("Demo FC", "demo-fc", fixtures, results, GENERATED)
        payload = json.loads((feeds_dir / "clubs" / "demo-fc.json").read_text())

        assert [p["team"] for p in payload["participation"]] == ["Demo FC U10"]
        assert payload["participation"][0]["played"] is True

    def test_does_not_mutate_the_caller_rows(self, feeds_dir):
        fixtures, results = self._rows()
        write_club_feed("Demo FC", "demo-fc", fixtures, results, GENERATED)
        assert fixtures[0]["away_team"] == "Riverside Rangers U10"


# ---------------------------------------------------------------------------
# ICS calendars
# ---------------------------------------------------------------------------

class TestRestrictedIcs:
    """Calendar files sit on public URLs too, so they follow the same rules."""

    def test_restricted_event_names_no_opponent(self):
        ics = fixtures_to_ics("Demo FC U10", [U10_FIXTURE])
        assert "SUMMARY:⚽ Demo FC U10 (Home)" in ics
        assert "Riverside Rangers U10" not in ics

    def test_restricted_event_has_no_location(self):
        ics = fixtures_to_ics("Demo FC U10", [U10_FIXTURE])
        assert "LOCATION:\n" in ics
        assert "Demo FC Ground" not in ics

    def test_restricted_event_keeps_kick_off_and_division(self):
        ics = fixtures_to_ics("Demo FC U10", [U10_FIXTURE])
        assert "DTSTART;TZID=Europe/London:20260913T103000" in ics
        assert "Division: U10 Sunday" in ics
        assert "KO: 10:30" in ics

    def test_open_age_event_is_unchanged(self):
        ics = fixtures_to_ics("Demo FC U14", [U14_FIXTURE])
        assert "SUMMARY:⚽ Demo FC U14 vs Riverside Rangers U14 (Home)" in ics
        assert "LOCATION:Demo FC Ground" in ics

    def test_away_restricted_event(self):
        away = U10_FIXTURE._replace(
            home_team="Riverside Rangers U10", away_team="Demo FC U10",
            venue="Riverside Park",
        )
        ics = fixtures_to_ics("Demo FC U10", [away])
        assert "SUMMARY:⚽ Demo FC U10 (Away)" in ics
        assert "Riverside" not in ics
