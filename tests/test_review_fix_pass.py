"""#624: the fix pass is on the row, and it is deliberately not a leaderboard.

Everything else this table records is about a ROUND — what it read, under which
dials, with which seats, produced by which harness, ending in which verdict — or
about a reviewer, or about a finding. The actor *between* two rounds, the fix pass
that writes the code producing the next round's findings, had a paragraph in a
markdown brief and nothing here. On ``prisonblues/lexray#1780`` its four passes ran
to +850/-314 across 11 files, +322/-49 across 9, +356/-41 across 12 (seven of them
files no round had read) and +142/-31 across 7, and every one of those numbers was
reconstructed from ``git`` by hand, afterwards, in order to file the issue.

Two columns: ``fix_pass``, the record verbatim and deferred, and
``fix_pass_counts``, its own integer summary lifted out so a run *list* can carry
the numbers without the path lists riding with them — the cut ``unread_files_count``
and the three tallies already make.

**The second half of this file is the absence of a leaderboard**, and it is a
requirement of the feature rather than a gap in it. #624's own second opinion: every
obvious ratio over a fix pass is gameable in a direction worse than the disease —
lines per finding cleared rewards compressed and superficial fixes, findings
introduced per pass rewards weakening tests and avoiding the files most likely to be
read, new files opened rewards refusing a cross-file repair that is genuinely
required (a P1 left unfixed to protect a metric), and share of fixes still standing
a round later is invalid under increment scope because the later round may never
have re-read the repair.

That constraint is pinned rather than intended: no actor column, no ratio, nothing
aggregated or ordered by anywhere in ``app/``, and ``GET /review/stats`` — the
leaderboard this very table already feeds, and the one the ``ReviewFindingOutcome``
docstring records a real failure of — left untouched.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from sqlalchemy import inspect as sa_inspect

# The module, not the names off it, on `test_review_harness_identity.py`'s reason:
# the bounds arrive with this feature, so a `from ... import` of one turns the red
# half of every other test in this file into a collection error.
from app.api import reviews
from app.models.review import ReviewRun

from .conftest import LAPTOP

REPO = "acme/fixpass624"
AGENT = {**LAPTOP, "X-Agent-Instance": "d624d1"}

APP = Path(reviews.__file__).resolve().parent.parent

#: One record, spelled the way `panel_rounds.fix_pass_record` spells it. Written out
#: rather than imported: `harness/loops` is installed without `app/` beside it, so
#: this suite cannot import the producer, which is the same reason
#: `tests/test_payload_key_drift.py` reads the panel with `ast`.
RECORD = {
    "read_round": 2,
    "cycle": "cyc-624",
    "scope": "increment",
    "range": {"base": "a" * 40, "head": "b" * 40,
              "span": "aaaaaaaa..bbbbbbbb", "kind": "ok", "why": None,
              "spans": 1, "commits": 3, "merges": 0, "source": "compare"},
    "brief": {"round": 1, "findings": 3, "placed": 2, "why": "one was mandatory"},
    "churn": {"production": 10, "test": 5, "prose": 0, "churn": 15},
    "surface": {"files": ["app/sync.py", "nginx/site.conf"],
                "new_files": ["nginx/site.conf"], "count": 1, "prior_files": 7},
    "cleared": [{"key": "k1", "severity": "P2"}],
    "still_open": [{"key": "k2", "severity": "P1"}],
    "introduced": 4,
    "declared": {"narrowed": ["k9"], "declined": ["k3"], "escalated": ["k4"]},
    "counts": {"briefed": 3, "placed": 2, "cleared": 1, "still_open": 1,
               "production": 10, "test": 5, "prose": 0, "churn": 15,
               "files": 2, "new_files": 1, "introduced": 4},
    "gaps": ["no per-finding outcome: needs #623", "no actor"],
}


def payload(pr: int, **over) -> dict:
    body = {
        "repo": REPO,
        "pr": pr,
        "pr_title": f"feat: thing {pr}",
        "base": "main",
        "reviewed": True,
        "judged": True,
        "judge_model": "opus",
        "round": 2,
        "cycle": "cyc-624",
        "reviewers_selected": ["claude"],
        "reviewers": {"claude": {"model": "sonnet", "ran": True}},
        "fix_pass": RECORD,
    }
    return {**body, **over}


async def record(client, pr: int, **over) -> dict:
    r = await client.post("/review", json=payload(pr, **over), headers=AGENT)
    assert r.status_code == 201, r.text
    return r.json()


async def detail(client, run_id: int) -> dict:
    r = await client.get(f"/review/{run_id}", headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


async def runs(client) -> list[dict]:
    r = await client.get("/reviews", params={"repo": REPO}, headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------ the round trip

async def test_the_record_survives_the_round_trip_VERBATIM(client):
    """The whole point: the pass that produced a round's findings is readable off the
    round afterwards, without a `git` archaeology session.

    Verbatim, and asserted as a whole-object equality rather than field by field. Every
    value in it was derived by the panel from the diff, the commits and the payload the
    pass was given; a board that reshaped one of them would be a second implementation
    of the measurement, free to disagree with the panel about the same pass — which is
    what `m6bc45ff1` refuses for `converged` and what #305 was filed over."""
    got = await record(client, 1)
    assert (await detail(client, got["id"]))["fix_pass"] == RECORD


async def test_the_counts_are_LIFTED_out_of_the_record_and_never_recomputed(client):
    """One producer, one derivation. The panel computes `counts` as a projection of the
    record it sits inside and this board reads the named sub-object — so the two halves
    on the row cannot describe two different passes.

    Asserted against the record's OWN block rather than against a literal, which is
    what makes this a statement about the lift rather than about today's numbers."""
    got = await record(client, 2)
    row = next(r for r in await runs(client) if r["id"] == got["id"])
    assert row["fix_pass_counts"] == RECORD["counts"]


async def test_the_counts_ride_the_LIST_and_the_record_does_not(client):
    """#112's grouping-key / locator cut, one field over. #624's own instruction is to
    "calibrate against real cycles before anything is scored", and a calibration reads
    a POPULATION: "how big were the passes on rounds that then attributed nothing to
    them" is a question about thousands of rows, and detail-only would have made it one
    fetch per run to ask.

    The record itself carries the file list and the finding keys, so it stays off the
    list on `rules`' argument — `GET /reviews?limit=500` would have Postgres ship five
    hundred of them to serialise none."""
    got = await record(client, 3)
    row = next(r for r in await runs(client) if r["id"] == got["id"])
    assert row["fix_pass_counts"] is not None
    assert "fix_pass" not in row
    assert (await detail(client, got["id"]))["fix_pass"] == RECORD


async def test_a_round_with_no_pass_records_NULL_on_both_columns(client):
    """`null` means there was no pass to record: round 1, a run outside a cycle, and
    every skip. It is also every row recorded before these columns, which is why the
    read path leaves it unmasked rather than folding it into an empty object — a
    consumer must not have to tell "there was no pass" from "this row predates the
    field"."""
    got = await record(client, 4, fix_pass=None, round=1, cycle=None)
    row = next(r for r in await runs(client) if r["id"] == got["id"])
    assert row["fix_pass_counts"] is None
    assert (await detail(client, got["id"]))["fix_pass"] is None


async def test_a_pass_that_could_not_be_READ_is_a_record_with_nulls_INSIDE_it(client):
    """The distinction the record turns on, asserted where a consumer meets it. A
    rewritten branch or an API refusal is a pass that exists and cannot be seen, and
    "it opened no file and churned no line" is the flattering direction on every claim
    this record makes. So the absence is INSIDE the record — `range.kind` says which —
    and the column is not null, which is what keeps it apart from round 1."""
    blind = {**RECORD,
             "range": {**RECORD["range"], "kind": "rewritten",
                       "why": "the branch was rewritten between rounds",
                       "base": None, "head": None, "span": None},
             "churn": {"production": None, "test": None, "prose": None,
                       "churn": None},
             "surface": None,
             "counts": {**RECORD["counts"], "production": None, "test": None,
                        "prose": None, "churn": None, "files": None,
                        "new_files": None}}
    got = await record(client, 5, fix_pass=blind)
    row = next(r for r in await runs(client) if r["id"] == got["id"])
    assert (await detail(client, got["id"]))["fix_pass"] == blind
    # A count that cannot be believed drops WITH its key rather than becoming 0 —
    # `_tally_or_none`'s standing rule, and the reason it matters here is that zero is
    # a claim about a fix pass.
    assert "churn" not in row["fix_pass_counts"]
    assert row["fix_pass_counts"]["briefed"] == 3


# ------------------------------------------------------- what ingest refuses, loudly

async def test_a_record_that_is_not_an_object_is_refused_and_SAID(client):
    """A wrong shape would otherwise land on the NULL that means "there was no pass to
    record" — a true statement about round 1 and a false one about a round that
    measured one and sent it. That is #93's own failure mode, and the whole reason this
    field earns a named drop signal instead of a silence."""
    r = await client.post("/review", json=payload(6, fix_pass="a pass happened"),
                          headers=AGENT)
    assert r.status_code == 201, r.text
    assert "fix_pass" in r.json()["unreadable_fields"]
    assert (await detail(client, r.json()["id"]))["fix_pass"] is None


async def test_a_record_too_big_to_store_is_refused_WHOLE_and_says_why(client):
    """Refused whole rather than trimmed, on `_opaque_or_none`'s rule with a reason of
    its own: half a dial set is a policy no round ran under, and half a fix-pass record
    is a DIFFERENT PASS — drop the new-file list and it reads as a pass that opened
    nothing, which is the flattering direction on the one claim #624 was filed to
    make."""
    huge = {**RECORD, "surface": {**RECORD["surface"],
                                  "files": [f"app/f{i}.py" for i in range(20000)]}}
    got = await record(client, 7, fix_pass=huge)
    assert "fix_pass_dropped" in got
    assert "refused whole rather than trimmed" in got["fix_pass_dropped"]
    assert "reads as having opened nothing" in got["fix_pass_dropped"]
    assert (await detail(client, got["id"]))["fix_pass"] is None
    # …AND THE ELEVEN INTEGERS SURVIVE IT, which is why the lift reads the value as
    # sent rather than the field after coercion. A pass wide enough to blow the cap is
    # wide because its file list is long, and that is exactly the runaway #619 and #624
    # exist to make visible — so coupling the two would have left the one row a
    # population most needs as the one row with no numbers on it, and the loss would
    # read as a round with no pass.
    row = next(r for r in await runs(client) if r["id"] == got["id"])
    assert row["fix_pass_counts"] == RECORD["counts"]


async def test_the_counts_survive_a_record_of_the_wrong_SHAPE_as_far_as_they_can(
        client):
    """The other half of that decoupling, and its honest limit. A `fix_pass` that is
    not an object carries no `counts` to lift, so both columns are null and only the
    drop signal says a value arrived — which is the same posture the three opaque
    policy records take. What the decoupling buys is the case where the record is
    well-formed and merely too big, not the case where there was nothing to read."""
    got = await record(client, 11, fix_pass=["a pass"])
    row = next(r for r in await runs(client) if r["id"] == got["id"])
    assert row["fix_pass_counts"] is None
    assert "fix_pass" in got["unreadable_fields"]


async def test_a_sender_cannot_write_the_counts_itself(client):
    """`fix_pass_counts` is DERIVED at ingest and is not a key the panel sends, so a
    payload spelling it has its own account overwritten — `FindingIn._keep_provenance_sent`'s
    rule, which is that evidence the sender can write is not evidence. Here it is the
    difference between a summary of the stored record and a number beside it that
    nothing checks."""
    got = await record(client, 12, fix_pass_counts={"churn": 999999})
    row = next(r for r in await runs(client) if r["id"] == got["id"])
    assert row["fix_pass_counts"] == RECORD["counts"]


async def test_a_NUL_inside_the_record_is_refused_rather_than_500ing_the_round(client):
    """Postgres holds a NUL in no `JSONB` string and `json.loads` accepts one, so the
    refusal used to happen at the INSERT — a 500 on a round that passed every check
    this module makes, which the panel records as "the board did not answer" and
    nothing distinguishes from a board that was down (#646).

    The record is in `_OPAQUE_FIELDS`, so the whole-body normalisation deliberately
    does not reach inside it: a marked path inside a fix-pass record would be a
    different path, sitting in the column looking like data."""
    bad = {**RECORD, "surface": {**RECORD["surface"], "files": ["app/sy\x00nc.py"]}}
    got = await record(client, 8, fix_pass=bad)
    assert "NUL" in got.get("fix_pass_dropped", "")
    assert (await detail(client, got["id"]))["fix_pass"] is None


async def test_a_refusal_of_the_record_does_not_cost_the_round_or_its_neighbours(
        client):
    """This module's standing rule: nothing is a 422, because refusing the request
    loses the findings, the scorecards and the accounts along with the byte."""
    got = await record(client, 9, fix_pass=42, provenance_counts={"introduced": 4})
    row = next(r for r in await runs(client) if r["id"] == got["id"])
    assert row["provenance_counts"] == {"introduced": 4}
    assert row["round"] == 2 and row["reviewers"][0]["name"] == "claude"


async def test_a_count_this_board_has_no_bucket_for_is_dropped_with_its_key(client):
    """The vocabulary is the no-ratio rule expressed as a schema. A producer that
    started sending `lines_per_finding` would have it dropped rather than stored, which
    is the direction that matters: the record itself is opaque and keeps whatever the
    panel put in it, and this is the one place a board-side decision about the shape of
    these numbers is taken."""
    got = await record(client, 10, fix_pass={
        **RECORD, "counts": {**RECORD["counts"], "lines_per_finding": 7}})
    row = next(r for r in await runs(client) if r["id"] == got["id"])
    assert "lines_per_finding" not in row["fix_pass_counts"]
    assert row["fix_pass_counts"] == RECORD["counts"]
    # …and the record is unedited, because it is opaque: what the panel sent is what a
    # reader gets, and only the lifted tally is held to a vocabulary.
    assert "lines_per_finding" in (
        await detail(client, got["id"]))["fix_pass"]["counts"]


@pytest.mark.parametrize("counts", [None, {}, "three", [1, 2], 7])
async def test_a_record_with_no_readable_counts_lifts_NOTHING(client, counts):
    """`None` rather than `{}`, because an empty tally would say a pass was measured and
    every answer came back zero. The record is still stored — it is opaque, and a
    producer this board has not met is still a producer whose measurement is worth
    keeping — so the two halves disagree only in the direction that says "this board
    could not read the summary"."""
    got = await record(client, 20 + abs(hash(str(counts))) % 900,
                       fix_pass={**RECORD, "counts": counts})
    row = next(r for r in await runs(client) if r["id"] == got["id"])
    assert row["fix_pass_counts"] is None
    assert (await detail(client, got["id"]))["fix_pass"] is not None


async def test_the_lifted_vocabulary_matches_the_records_own_eleven_keys(client):
    """The drift check, and this suite is where it belongs: `harness/loops` is
    installed without `app/` beside it, so these two halves are readable at once
    nowhere else — the position `tests/test_payload_key_drift.py` and
    `tests/test_needs_human_drift.py` are both in.

    A key the panel adds and this vocabulary does not name is stored in the record and
    dropped from the tally, in silence, which is the shape `converged` had."""
    assert set(RECORD["counts"]) == set(reviews.FIX_PASS_COUNTS)


# --------------------------------------------------------- and it is NOT a leaderboard

def test_there_is_no_ACTOR_column_and_no_ratio_column():
    """The strongest of the four guarantees. A table with no actor key cannot be
    aggregated into a ranking of fixers at all, which is worth more than a policy of
    not writing that query — and it is why the record names the pass by its commit
    range and the round that briefed it.

    Read off the mapper rather than off a migration, so a column added later trips
    this."""
    cols = {c.key for c in sa_inspect(ReviewRun).columns}
    assert {"fix_pass", "fix_pass_counts"} <= cols
    forbidden = ("fixer", "pass_author", "pass_agent", "pass_model", "pass_session",
                 "fix_pass_score", "fix_pass_rank", "fix_pass_ratio",
                 "fix_pass_share", "fix_pass_rate")
    assert not [c for c in cols if c in forbidden]
    assert not [c for c in cols
                if c.startswith("fix_pass") and c not in ("fix_pass",
                                                          "fix_pass_counts")]


def test_the_lifted_vocabulary_holds_no_quotient():
    """#624's constraint as a vocabulary. The numerators and the denominators are all
    named and none of them is a division: a consumer that wants a ratio has to write it
    down in its own code, where somebody can argue with which of the four gameable ones
    it is."""
    forbidden = ("share", "ratio", "rate", "per_", "score", "rank", "index",
                 "average", "mean", "percent", "efficiency", "density")
    for name in reviews.FIX_PASS_COUNTS:
        assert not any(word in name for word in forbidden), name


def test_nothing_in_the_api_aggregates_or_orders_by_either_column():
    """The no-leaderboard rule where a leaderboard would be written, read by `ast` over
    every module in `app/`.

    A grep would not do: the point is not that the strings are absent — the storage and
    the two read paths name them — but that no `func.sum`/`avg`/`count`/`min`/`max`,
    no `order_by` and no `group_by` has one of them anywhere inside it. That is the
    shape a ranking takes in this codebase, and it is what `GET /review/stats` is built
    out of one endpoint over."""
    aggregates = {"sum", "avg", "count", "min", "max", "percentile_cont", "stddev"}
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            named = (fn.attr if isinstance(fn, ast.Attribute) else
                     fn.id if isinstance(fn, ast.Name) else "")
            if named not in aggregates | {"order_by", "group_by"}:
                continue
            inside = {n.attr for n in ast.walk(node)
                      if isinstance(n, ast.Attribute)} | {
                n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
                c.value for c in ast.walk(node)
                if isinstance(c, ast.Constant) and isinstance(c.value, str)}
            if inside & {"fix_pass", "fix_pass_counts"}:
                offenders.append(f"{path.name}:{node.lineno} {named}")
    assert not offenders, offenders


def test_the_leaderboard_endpoint_does_not_read_it():
    """`GET /review/stats` is the leaderboard this table already feeds, and the
    `ReviewFindingOutcome` docstring records what it cost when a measure reached it
    before it was ready: on PR #64 three of six judge-confirmed P2s were simply wrong,
    all three from a reviewer that had declared it could not assess the condition, and
    the ranking rewarded the confident wrong finding.

    #624 asks for the opposite order — "record the pass, report the numbers as
    diagnostics, and calibrate against real cycles before anything is scored, ranked
    or gated" — so the endpoint is asserted not to have been touched."""
    src = inspect.getsource(reviews.review_stats)
    assert "fix_pass" not in src


def test_neither_column_is_indexed_or_constrained():
    """No index, because every query on this table is already selective on `(repo, pr)`
    or `ts` and an index here would be a write cost paid by every round to make cheaper
    the aggregation this feature declines to offer.

    No CHECK, because this board has no proposition about these values to enforce: it
    does not know how many findings a brief may hold, and `churn: null` beside
    `files: 0` is not a contradiction — it is a fix range that was read and held no
    file. A constraint written against today's producer is a 500 waiting for
    tomorrow's, which is `mdef4716b`'s own conclusion for the harness columns."""
    named = {"fix_pass", "fix_pass_counts"}
    for index in ReviewRun.__table__.indexes:
        assert not (named & {c.key for c in index.columns}), index.name
    for constraint in ReviewRun.__table__.constraints:
        assert not (named & {c.key for c in getattr(constraint, "columns", [])})
    text = " ".join(str(c.sqltext) for c in ReviewRun.__table__.constraints
                    if hasattr(c, "sqltext"))
    assert "fix_pass" not in text


def test_the_record_is_deferred_and_the_counts_are_not():
    """The cut, asserted on the mapper rather than described in a comment. Async
    SQLAlchemy cannot lazy-load, so a deferred column read off a run the session did
    not undefer raises `MissingGreenlet` — the failure mode worth having, because it
    cannot go unnoticed — and `GET /review/{id}` is the one caller that asks for it."""
    mapper = sa_inspect(ReviewRun)
    assert mapper.attrs["fix_pass"].deferred is True
    assert mapper.attrs["fix_pass_counts"].deferred is False
