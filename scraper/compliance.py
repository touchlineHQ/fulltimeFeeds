"""
Publication rules for youth football data.

The FA prohibits publishing match results and league tables for teams playing
at Under-11 and below, and the league's guidance extends that to naming the
opposition or the venue in anything published online.  Everything this project
writes to ``feeds/`` and ``calendars/`` ends up on a public URL, so the rules
are applied here, at the point the data is serialised, rather than left to each
consumer to remember.

For a match at U11 or below:

  - results (and therefore scores) are withheld entirely
  - the opposition is not named
  - the venue is not named

Fixtures themselves are still published — date, kick-off time, the club's own
team name, and whether the match is home or away — so parents can still plan
around them.  A played match survives as a *participation record* carrying the
same fields, so a club can say its U10s played on Sunday without saying how it
went; see `participation_record`.  Consumers should list those matches rather
than hide them — withholding the score is the point, not erasing the fixture.
U12 and above, and adult football, are published in full.

Age groups are read from the team names and the division label ("U10",
"Under 10", "U10 Division 1", ...).  A match with no age token anywhere is
treated as adult football and published in full; where tokens disagree, the
youngest one wins, so a U18 side fielding a U11 fixture is still protected.
"""

import hashlib
import re

# The FA's threshold: teams at this age group and below must not have results
# or league tables published.
RESTRICTED_MAX_AGE = 11

# What replaces an identifying value in a restricted match.
OPPONENT_LABEL = "Opposition"
VENUE_LABEL = ""

# "U10", "U 10", "Under 10", "Under-10". The trailing (?!\d) stops "U1" from
# matching the front of "U11"; \b alone would not, since the regex is happy to
# stop mid-number.
_AGE_RE = re.compile(r"\b(?:u|under)[\s-]?(\d{1,2})(?!\d)", re.IGNORECASE)

# Fields that can carry a team or division name on a feed row. The league name
# is deliberately not consulted: it covers every age group at once, so a league
# whose title happens to mention U11 would drag its U18 teams down with it.
_NAME_FIELDS = ("home_team", "away_team", "team", "opponent", "division")


def age_group(*texts: str | None) -> int | None:
    """Return the youngest age group named across *texts*, or None if none is.

    >>> age_group("Arnold Town Blue U11", "U11 Division 1")
    11
    >>> age_group("Demo FC", "Senior Men's League") is None
    True
    """
    ages = [
        int(m.group(1))
        for text in texts
        if text
        for m in _AGE_RE.finditer(str(text))
    ]
    return min(ages) if ages else None


def is_restricted(*texts: str | None) -> bool:
    """True when the named age group is U11 or below (so publication is limited)."""
    age = age_group(*texts)
    return age is not None and age <= RESTRICTED_MAX_AGE


def row_age_group(row: dict) -> int | None:
    """Youngest age group named anywhere on a fixture/result row."""
    return age_group(*(row.get(field) for field in _NAME_FIELDS))


def is_row_restricted(row: dict) -> bool:
    """True when a fixture/result row belongs to a U11-or-below match."""
    age = row_age_group(row)
    return age is not None and age <= RESTRICTED_MAX_AGE


def restricted_record_id(
    row: dict,
    subject_team: str | None = None,
    record_kind: str = "record",
) -> str:
    """Return a stable ID built only from fields safe to publish.

    Restricted feeds must not retain the normal match ID because that ID is
    derived from both raw team names.  Including the subject team is safe in
    its own feed and ensures the two teams receive different public IDs for
    the same match.
    """
    public_parts = (
        record_kind,
        row.get("date", ""),
        row.get("time", ""),
        subject_team or row.get("team", ""),
        row.get("home_away", ""),
        row.get("division", ""),
    )
    public_key = "\x1f".join(str(part) for part in public_parts)
    return hashlib.sha256(public_key.encode()).hexdigest()[:32]


def redact_fixture(row: dict, subject_team: str | None = None) -> dict:
    """Return a publication-safe copy of a restricted fixture row.

    *subject_team* is the team whose feed this row is being written into; its
    name is kept, because a club naming its own team is fine — it is the
    opposition that must not be identified.  Without a subject team (a
    league-wide listing, where every row names two clubs to each other) both
    sides are anonymised; callers generally drop such rows instead.
    """
    out = dict(row)

    if subject_team and out.get("home_team") == subject_team:
        out["away_team"] = OPPONENT_LABEL
    elif subject_team and out.get("away_team") == subject_team:
        out["home_team"] = OPPONENT_LABEL
    else:
        if "home_team" in out:
            out["home_team"] = OPPONENT_LABEL
        if "away_team" in out:
            out["away_team"] = OPPONENT_LABEL

    if "opponent" in out:
        out["opponent"] = OPPONENT_LABEL
    if "venue" in out:
        out["venue"] = VENUE_LABEL

    # Scores never survive into a public feed, even if a caller hands us a
    # result row by mistake.
    for score_field in ("home_score", "away_score", "goals_for", "goals_against"):
        out.pop(score_field, None)

    if "id" in out:
        out["id"] = restricted_record_id(out, subject_team, "fixture")
    out["publication_restricted"] = True
    return out


def safe_fixtures(rows: list[dict], subject_team: str | None = None) -> list[dict]:
    """Redact every restricted fixture row, leaving the rest untouched."""
    return [
        redact_fixture(row, subject_team) if is_row_restricted(row) else row
        for row in rows
    ]


# What survives of a restricted match: enough to say it happened, nothing that
# says how it went or who it was against.
_PARTICIPATION_FIELDS = ("date", "time", "team", "league", "home_away", "division")


def participation_record(row: dict) -> dict:
    """Reduce a restricted match to an attendance record.

    Withholding a result is not the same as pretending the match never
    happened — a club may say its U10s played on Sunday, at home, in their
    division.  That is the FA's own worked example of an acceptable post, and
    it lets a site list the match rather than leave a young team's season
    looking empty.  No score, no opposition, no venue survives.
    """
    out = {field: row[field] for field in _PARTICIPATION_FIELDS if field in row}
    if "id" in row:
        out["id"] = restricted_record_id(out, record_kind="participation")
    age = row_age_group(row)
    out["age_group"] = f"U{age}" if age is not None else None
    out["played"] = True
    out["publication_restricted"] = True
    return out


def split_results(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split result rows into (publishable results, participation records).

    A result stripped of its score is still a published result, so restricted
    rows are not redacted into the results array — they come back as
    participation records instead.
    """
    publishable: list[dict] = []
    participation: list[dict] = []
    for row in rows:
        if is_row_restricted(row):
            participation.append(participation_record(row))
        else:
            publishable.append(row)
    return publishable, participation


def played_fixtures(
    rows: list[dict],
    today: str,
    *,
    subject_team: str | None = None,
    league: str | None = None,
    existing_ids: frozenset[str] | set[str] = frozenset(),
) -> tuple[list[dict], list[dict]]:
    """Split fixture rows into (still to come, participation records).

    At U11 and below no result is ever published, so some leagues never move a
    played match onto their results page at all — it simply stays in the fixture
    list.  Left alone it would sit there for the rest of the season and the game
    would appear nowhere, which is the opposite of what participation records
    are for.

    A restricted fixture dated before *today* has therefore been played, and
    becomes a participation record like any other restricted match.  Open-age
    fixtures are never moved: there a missing result more likely means the
    league has not entered it yet, or the match was postponed, and calling it
    played would assert something we do not know.

    *today* is the run date ("YYYY-MM-DD"), so a match is only moved on the run
    after it was played and never on the strength of a clock mid-fixture.
    *existing_ids* are participation ids already built from result rows; a match
    present in both is recorded once.
    """
    upcoming: list[dict] = []
    records: list[dict] = []
    seen = set(existing_ids)

    for row in rows:
        if not (is_row_restricted(row) and row.get("date", "") < today):
            upcoming.append(row)
            continue
        # A fixture row carries neither of these in a team feed, where the team
        # is implied by the file it lives in; a participation record has to name
        # its own team to be worth anything.
        source = dict(row)
        if subject_team and not source.get("team"):
            source["team"] = subject_team
        if league and not source.get("league"):
            source["league"] = league
        record = participation_record(source)
        if record["id"] in seen:
            continue
        seen.add(record["id"])
        records.append(record)

    return upcoming, records


def safe_results(rows: list[dict]) -> tuple[list[dict], int]:
    """Drop restricted result rows.

    Returns (publishable rows, number withheld).  Use `split_results` where the
    withheld matches are still wanted as participation records.
    """
    publishable, participation = split_results(rows)
    return publishable, len(participation)


POLICY = (
    "Results and league tables are not published for teams at U11 and below, "
    "and the opposition and venue are not named for those fixtures, following "
    "The FA's youth football guidance."
)


def compliance_meta(results_withheld: int = 0) -> dict:
    """The compliance block embedded in every published feed."""
    return {
        "policy": POLICY,
        "restricted_max_age_group": f"U{RESTRICTED_MAX_AGE}",
        "results_withheld": results_withheld,
    }
