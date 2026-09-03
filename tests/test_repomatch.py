"""The repo-spelling rule itself, in units (#714).

:mod:`app.repomatch` is the one place this board decides *which repository is the
caller asking about*, for the four reads that take a ``repo`` filter. Three of
them had written their own answer and the fourth — ``GET /active``, whose entire
job is collision detection — had written none, so a lease reporting the checkout
basename was invisible to the qualified spelling every keyed surface here teaches,
and the pre-flight call agents are told to make answered "the coast is clear" for
a repo with three agents in it.

The endpoint behaviour is in ``tests/test_collision_repo_scope.py``. What is here
is the rule the endpoints share, exercised directly, because the two renderings of
it — a SQL clause and a Python predicate — have to agree and the cheapest place to
say what they must both mean is here.

These are ``N-A`` for red/green by construction: the module is new, so there is no
pre-fix behaviour to run them against.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.repomatch import asked_repo, fold_repo, name_matches

#: Spellings that are neither ``owner/name`` nor a bare repository name — refused
#: rather than parsed, because that input domain is open and PR #152 was closed
#: after three review rounds found three more holes in the enumeration.
NEITHER = [
    "https://github.com/prisonblues/quarterback",
    "git@github.com:prisonblues/quarterback.git",
    "prisonblues/quarterback.git",
    "/etc/passwd",
    "a/b/c",
    "quarterback.git",
    "quarterback/",
    "/quarterback",
    "quarter back",
]


@pytest.mark.parametrize(
    ("asked", "qualified", "name"),
    [
        ("prisonblues/lexray", "prisonblues/lexray", "lexray"),
        ("PrisonBlues/LexRay", "prisonblues/lexray", "lexray"),
        ("  prisonblues/lexray  ", "prisonblues/lexray", "lexray"),
        ("lexray", None, "lexray"),
        ("LexRay", None, "lexray"),
    ],
)
def test_asked_repo_splits_the_two_tiers(asked, qualified, name):
    """`qualified` is None exactly when a bare name arrived — the whole of the
    two-tier rule, decided once so no call site re-derives it — and `name` is
    always present because it is the only thing a column holding either shape can
    be matched on."""
    got = asked_repo(asked)
    assert (got.qualified, got.name) == (qualified, name)
    assert got.asked == asked, "the refusal payload must echo what the caller typed"


@pytest.mark.parametrize("value", NEITHER)
def test_asked_repo_refuses_everything_else(value):
    """A 422, never a sentinel the caller can turn into an empty answer: the empty
    answer is the defect, on every one of the four endpoints this serves."""
    with pytest.raises(HTTPException) as excinfo:
        asked_repo(value)
    assert excinfo.value.status_code == 422
    assert "owner/name" in excinfo.value.detail["error"]


@pytest.mark.parametrize(
    ("stored", "value"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("PrisonBlues/LexRay", "prisonblues/lexray"),
        ("  prisonblues/lexray  ", "prisonblues/lexray"),
        ("lexray", "lexray"),
        ("  lexray  ", "lexray"),
        # The open half passes through: this module does not guess at a spelling it
        # does not recognise, which is what PR #152 was closed for trying.
        ("git@github.com:prisonblues/lexray.git", "git@github.com:prisonblues/lexray.git"),
    ],
)
def test_fold_repo_folds_the_qualified_half_and_only_that(stored, value):
    """The write-path normalisation, and the argument for its narrowness is in the
    function: refusing the un-qualified half would take a heartbeat's whole lease —
    the agent's entire presence on the board — away over an optional field."""
    assert fold_repo(stored) == value


@pytest.mark.parametrize(
    ("stored", "asked", "matches"),
    [
        ("lexray", "prisonblues/lexray", True),
        ("prisonblues/lexray", "lexray", True),
        ("LexRay", "prisonblues/lexray", True),
        ("other/lexray", "prisonblues/lexray", True),   # wide, and disclosed
        ("nix-fleet", "prisonblues/lexray", False),
        (None, "prisonblues/lexray", False),
    ],
)
def test_name_matches_agrees_with_the_sql_it_mirrors(stored, asked, matches):
    """The Python rendering, used for a sub-agent's parent lease. ``/active``
    answers one question out of two queries, so the two renderings have to agree —
    `test_a_subagent_is_attributed_by_either_spelling_of_its_parents_repo` is the
    same claim end to end, over HTTP."""
    assert name_matches(stored, asked_repo(asked)) is matches
