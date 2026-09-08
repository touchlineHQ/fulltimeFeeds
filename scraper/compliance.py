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
around them.  U12 and above, and adult football, are published in full.

Age groups are read from the team names and the division label ("U10",
"Under 10", "U10 Division 1", ...).  A match with no age token anywhere is
treated as adult football and published in full; where tokens disagree, the
youngest one wins, so a U18 side fielding a U11 fixture is still protected.
"""

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

    out["publication_restricted"] = True
    return out


def safe_fixtures(rows: list[dict], subject_team: str | None = None) -> list[dict]:
    """Redact every restricted fixture row, leaving the rest untouched."""
    return [
        redact_fixture(row, subject_team) if is_row_restricted(row) else row
        for row in rows
    ]


def safe_results(rows: list[dict]) -> tuple[list[dict], int]:
    """Drop restricted result rows.

    Returns (publishable rows, number withheld).  A result stripped of its
    score is still a published result, so these are removed rather than
    redacted.
    """
    publishable = [row for row in rows if not is_row_restricted(row)]
    return publishable, len(rows) - len(publishable)


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
