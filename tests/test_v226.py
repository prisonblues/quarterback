"""v2.26: provenance reaches the board — did the last fix cause it, or miss it?

v2.24 taught the panel to answer that and gave the answer nowhere to go. Four
fields joined ``panel.py --json`` — ``head_sha``, ``unread_files``,
``provenance_counts`` and a per-finding ``provenance`` — and ``ReviewIn`` is
``populate_by_name=True`` with no ``extra=``, so pydantic v2's default
``extra="ignore"`` applied: all four were POSTed, all four were discarded, and
nothing said so (#93). The per-finding one is the irreplaceable half — it cannot
be reconstructed later from anything the board keeps.

Four properties carry the feature, and the tests below are grouped by them:

* **The fields survive the round trip, per finding included.** The point of the
  release. If `provenance` does not reach `review_findings`, #48's axis cannot be
  built from anything else the board holds.
* **Null means NOT RECORDED, never "no provenance".** Three states, all distinct
  and all reachable: NULL (nobody said), ``{}``/``None`` per finding (the question
  does not arise — a round 1, a run outside a cycle, a repeat), and ``"unknown"``
  (asked, and unplaceable). Collapsing any pair of them is how a measurement stops
  meaning anything, and this file pins each pair apart.
* **A dropped field says so.** An unrecognised bucket is normalised away — the
  ``pr_state`` rule — and named back in the response. Shipping a quieter version
  of #93 as the fix for #93 is the failure this guards.
* **Every malformed shape records rather than 422s.** This module's standing rule.
  A garbled head sha or a mistyped file list must never cost a run its findings,
  its scorecards and its accounts.

The stats axis is tested at both grains, because they answer different questions
and one is not derivable from the other: ``by_model`` is per reviewer (who catches
regressions vs who finds what has been sitting there), ``by_provenance`` is per
finding across the window (how much of what the loop found did it inflict on
itself) — the second counts a finding two seats agreed on once, the first twice.
"""

from __future__ import annotations

from .conftest import LAPTOP

REPO = "acme/v226repo"
AGENT = {**LAPTOP, "X-Agent-Instance": "cc33dd"}

SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


def finding(title: str, **over) -> dict:
    f = {"title": title, "severity": "P2", "file": "app/api/reviews.py",
         "line": 10, "reviewers": ["claude"]}
    return {**f, **over}


def payload(pr: int, **over) -> dict:
    body = {
        "repo": REPO,
        "pr": pr,
        "judged": True,
        "judge_model": "opus",
        "reviewers_selected": ["claude", "codex"],
        "reviewers": {"claude": {"model": "opus", "ran": True},
                      "codex": {"model": "gpt-5", "ran": True}},
        "to_fix": [],
        "dismissed": [],
        "sonar_findings": [],
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


async def stats(client, repo: str = REPO, **q) -> dict:
    """Always scoped to ONE repo. `/review/stats` has no `pr` filter — it is a
    window over runs — so two tests sharing a repo would read each other's
    findings into their own aggregate."""
    r = await client.get("/review/stats", params={"repo": repo, **q}, headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


def card(d: dict, name: str) -> dict:
    return next(c for c in d["reviewers"] if c["name"] == name)


def model_row(s: dict, name: str) -> dict:
    return next(m for m in s["by_model"] if m["reviewer"] == name)


# --------------------------------------------- the fields survive the round trip

async def test_all_four_dropped_fields_reach_the_board(client):
    """#93 in one assertion: the panel sends four, ingest kept none, and the
    per-finding one is the half nothing else could reconstruct."""
    run = await record(
        client, 9301,
        head_sha=SHA,
        unread_files=["harness/loops/panel.py", "migrations/versions/0016_x.py"],
        provenance_counts={"introduced": 2, "missed": 1,
                           "missed-unread": 0, "unknown": 0},
        to_fix=[finding("a null deref the fix pass wrote", provenance="introduced"),
                finding("an old off-by-one nobody saw", provenance="missed",
                        file="app/sync.py")],
    )
    d = await detail(client, run["id"])
    assert d["head_sha"] == SHA
    assert d["unread_files"] == ["harness/loops/panel.py",
                                 "migrations/versions/0016_x.py"]
    assert d["provenance_counts"] == {"introduced": 2, "missed": 1,
                                      "missed-unread": 0, "unknown": 0}
    assert sorted(f["provenance"] for f in d["findings"]) == ["introduced", "missed"]


async def test_a_head_sha_is_stored_lowercase_so_it_joins(client):
    """The column exists to be resolved later — against the repo, against the next
    round's baseline, against #98's base end. A sha that differs from itself by
    case joins nothing."""
    run = await record(client, 9302, head_sha="  " + SHA.upper() + "  ")
    assert (await detail(client, run["id"]))["head_sha"] == SHA


async def test_provenance_rides_along_the_finding_history(client):
    """`GET /review/findings` is where a defect's chain is read. A round's
    provenance belongs on the observation, not just on the run."""
    run = await record(client, 9303, head_sha=SHA,
                       to_fix=[finding("regression", provenance="introduced")])
    r = await client.get("/review/findings", params={"repo": REPO, "pr": 9303},
                         headers=AGENT)
    assert r.status_code == 200, r.text
    body = r.json()
    obs = body["findings"][0]["observations"][0]
    assert obs["run_id"] == run["id"]
    assert obs["provenance"] == "introduced"
    # ...and the round says which commit it read, so the trace can be replayed
    # against the repo rather than against a branch name that has since moved.
    assert body["runs"][0]["head_sha"] == SHA


# ------------------------------------- null is NOT RECORDED, never no provenance

async def test_a_pre_v226_run_records_nulls_and_not_empties(client):
    """Every run before this release said nothing about any of it. Reading that as
    "read everything, attributed nothing" is the collapse this release exists to
    prevent."""
    run = await record(client, 9310, to_fix=[finding("something")])
    d = await detail(client, run["id"])
    assert d["head_sha"] is None
    assert d["unread_files"] is None
    assert d["provenance_counts"] is None
    assert d["findings"][0]["provenance"] is None


async def test_an_empty_unread_list_is_not_the_same_as_no_list(client):
    """[] is "the round measured its coverage and nothing was cut". NULL is
    "nobody measured". A consumer must be able to tell them apart."""
    run = await record(client, 9311, unread_files=[])
    assert (await detail(client, run["id"]))["unread_files"] == []


async def test_an_empty_tally_is_the_question_not_arising_not_a_zero_result(client):
    """A round 1 has no earlier round to attribute against, so the panel sends
    ``{}``. All-zero is a different claim — attribution ran and found nothing —
    and NULL is a third. All three survive."""
    empty = await record(client, 9312, round=1, provenance_counts={})
    zeros = await record(client, 9313, round=2, cycle="c1",
                         provenance_counts={b: 0 for b in
                                            ("introduced", "missed",
                                             "missed-unread", "unknown")})
    silent = await record(client, 9314)
    assert (await detail(client, empty["id"]))["provenance_counts"] == {}
    assert (await detail(client, zeros["id"]))["provenance_counts"] == {
        "introduced": 0, "missed": 0, "missed-unread": 0, "unknown": 0}
    assert (await detail(client, silent["id"]))["provenance_counts"] is None


async def test_unknown_is_a_real_bucket_and_not_a_missing_value(client):
    """"The fix range could not be read" is an answer. "Nobody asked" is not, and
    it is null. Storing the first as the second would make an unreadable range
    indistinguishable from a round 1."""
    run = await record(client, 9315, head_sha=SHA,
                       to_fix=[finding("unplaceable", provenance="unknown"),
                               finding("not asked", file="app/db.py")])
    d = await detail(client, run["id"])
    got = {f["title"]: f["provenance"] for f in d["findings"]}
    assert got["unplaceable"] == "unknown"
    assert got["not asked"] is None


# ------------------------------------------------------ a dropped field says so

async def test_an_unrecognised_finding_bucket_is_dropped_and_named(client):
    """The `pr_state` rule — a value a consumer filters on is never stored
    verbatim when it is not one this board knows — plus the half #93 is actually
    about: the drop is reported instead of being silent."""
    run = await record(client, 9320, head_sha=SHA,
                       to_fix=[finding("odd", provenance="introduced-by-a-deletion")])
    assert run["provenance_unknown"] == ["introduced-by-a-deletion"]
    assert (await detail(client, run["id"]))["findings"][0]["provenance"] is None


async def test_an_unrecognised_tally_key_is_dropped_and_named(client):
    """A published tally must not carry a key no consumer can interpret — and the
    key's disappearance must not be the only record of it."""
    run = await record(client, 9321,
                       provenance_counts={"introduced": 3, "regressed": 4})
    assert run["provenance_unknown"] == ["regressed"]
    assert (await detail(client, run["id"]))["provenance_counts"] == {"introduced": 3}


async def test_an_unbelievable_count_drops_with_its_key_rather_than_becoming_zero(client):
    """Zero is a claim everywhere in this feature. A count that cannot be believed
    must not be recorded as one that was measured at nothing."""
    run = await record(client, 9322,
                       provenance_counts={"introduced": -1, "missed": "two",
                                          "unknown": 5})
    assert (await detail(client, run["id"]))["provenance_counts"] == {"unknown": 5}


async def test_an_ordinary_run_keeps_the_response_shape_callers_already_parse(client):
    run = await record(client, 9323, head_sha=SHA, unread_files=["a.py"],
                       to_fix=[finding("fine", provenance="missed")])
    assert set(run) == {"id", "recorded", "findings", "accounts", "changed_files"}


# ------------------------------------------- malformed records rather than 422s

async def test_a_garbled_head_sha_costs_the_sha_and_nothing_else(client):
    """Recording is best-effort. A branch name where a commit id was promised is
    "nobody said", which is a state the column already has."""
    for bad in ("HEAD", "origin/main", "zzzz", "abc", 12345, None,
                {"oid": SHA}, SHA + SHA):
        run = await record(client, 9330, head_sha=bad, to_fix=[finding("still here")])
        d = await detail(client, run["id"])
        assert d["head_sha"] is None, bad
        assert len(d["findings"]) == 1, bad


async def test_a_malformed_unread_list_never_costs_the_run_its_findings(client):
    """A shape that is not a declaration lands on NULL, exactly as
    `could_not_assess` does — [] would say the round measured and found nothing
    cut, which is the opposite of what a garbled value tells us."""
    for bad, want in (
        ("harness/loops/panel.py", ["harness/loops/panel.py"]),  # one path, spelled bare
        ([" a.py ", "a.py", ""], ["a.py"]),                      # trimmed, deduped, blanks gone
        ([{"path": "a.py"}, 7, None], []),                       # no usable item
        ({"a.py": True}, None),                                  # not a declaration at all
        (42, None),
    ):
        run = await record(client, 9331, unread_files=bad,
                           to_fix=[finding("survives")])
        d = await detail(client, run["id"])
        assert d["unread_files"] == want, bad
        assert len(d["findings"]) == 1, bad


async def test_a_malformed_tally_never_costs_the_run_its_findings(client):
    for bad in (["introduced", 1], "introduced=1", 3):
        run = await record(client, 9332, provenance_counts=bad,
                           to_fix=[finding("survives")])
        d = await detail(client, run["id"])
        assert d["provenance_counts"] is None, bad
        assert len(d["findings"]) == 1, bad


async def test_a_folded_path_is_not_reported_as_a_loss(client):
    """A blank or repeated path is folded, not lost, so the drop signal must stay
    quiet — a warning that fires on a payload nothing went missing from teaches a
    reader to ignore it. Same silence `changed_files` keeps over its own dedup."""
    run = await record(client, 9334, unread_files=[" a.py ", "a.py", "", "b.py"])
    assert "unread_files_dropped" not in run
    assert (await detail(client, run["id"]))["unread_files"] == ["a.py", "b.py"]


async def test_an_oversized_unread_list_is_truncated_and_reported(client):
    """An authenticated sender is not a bounded one, and one request must not be
    able to push a million strings into a single JSONB value. What is trimmed is
    announced — a truncation the sender cannot see reads as a complete list."""
    run = await record(client, 9333,
                       unread_files=[f"f{i}.py" for i in range(5200)])
    assert run["unread_files_dropped"] == 200
    assert len((await detail(client, run["id"]))["unread_files"]) == 5000


async def test_the_reported_truncation_is_what_was_actually_ignored(client):
    """The cap is applied to the RAW list, before the dedup — so the reported
    number is exactly "this many entries were never looked at".

    Dedup first would make the response lie in the direction that matters: 6,000
    entries holding 3,000 distinct paths would store every one of them and still
    announce 1,000 missing. Here the 1,000 past the cap really are unexamined,
    which the short stored list is the evidence for.

    The trade taken with it: a caller sending an oversized-but-repetitive list
    keeps fewer distinct paths than a dedup-first bound would have. `unread_files`
    is a subset of a PR's diff and GitHub caps a PR's file list at 3,000, so this
    ceiling is a defence against an unbounded sender rather than a regime anything
    real operates in — and matching what `changed_files` does one field over is
    worth more than optimising a payload that cannot occur."""
    paired = [p for i in range(3000) for p in (f"g{i}.py", f"g{i}.py")]
    run = await record(client, 9335, unread_files=paired)
    assert run["unread_files_dropped"] == 1000, "6000 sent, 5000 considered"
    # The first 5,000 entries are 2,500 paths seen twice each.
    assert len((await detail(client, run["id"]))["unread_files"]) == 2500


# ---------------------------------------------------------- #48's axis, per seat

async def test_the_scorecard_splits_what_a_member_found_by_cause(client):
    """The axis #48 was filed for: finding a regression somebody just wrote and
    finding a defect that has sat there for months are different competencies."""
    run = await record(
        client, 9340,
        head_sha=SHA, provenance_counts={"introduced": 1, "missed": 2},
        to_fix=[
            finding("fresh", provenance="introduced", reviewers=["claude"]),
            finding("old", provenance="missed", reviewers=["claude", "codex"],
                    file="app/db.py"),
            finding("unreadable file", provenance="missed-unread",
                    reviewers=["codex"], file="app/sync.py"),
        ],
    )
    d = await detail(client, run["id"])
    assert card(d, "claude")["provenance"] == {
        "introduced": 1, "missed": 1, "missed-unread": 0, "unknown": 0}
    assert card(d, "codex")["provenance"] == {
        "introduced": 0, "missed": 1, "missed-unread": 1, "unknown": 0}


async def test_a_dismissed_finding_is_not_attributed_to_a_reviewer(client):
    """Confirmed only, like `p1`..`p4` and `solo` beside it. A dismissed finding
    was not a defect, so asking what caused it would credit a reviewer with the
    provenance of something that was never there.

    This is why the scorecard counters are deliberately narrower than the run's
    own `provenance_counts`, which the panel computes over everything still
    outstanding — the two are allowed to disagree, and do here."""
    run = await record(
        client, 9341, head_sha=SHA,
        to_fix=[finding("real", provenance="introduced")],
        dismissed=[finding("not real", provenance="introduced", file="app/db.py")],
    )
    d = await detail(client, run["id"])
    assert card(d, "claude")["provenance"]["introduced"] == 1
    # ...and the finding still records its own bucket, unchanged.
    assert {f["title"]: f["provenance"] for f in d["findings"]}["not real"] == "introduced"


async def test_the_leaderboard_sums_the_split_and_says_what_it_covers(client):
    """The counters are NOT NULL, so a bare zero cannot say "never measured".
    `provenance_runs` is what stops a window of older runs reading as a panel that
    never once caught a regression — the job `token_runs` does for tokens."""
    repo = "acme/v226lb"
    await record(client, 9350, repo=repo, head_sha=SHA,
                 provenance_counts={"introduced": 1, "missed": 0},
                 to_fix=[finding("fresh", provenance="introduced",
                                 reviewers=["claude"])])
    # Attributable, and nothing of claude's to attribute.
    await record(client, 9351, repo=repo, head_sha=SHA,
                 provenance_counts={"introduced": 0, "missed": 1},
                 to_fix=[finding("old", provenance="missed", reviewers=["codex"],
                                 file="app/db.py")])
    # A round 1: the question does not arise, so this run must NOT count as
    # coverage — it is not a run that found nothing, it is one never asked.
    await record(client, 9352, repo=repo, provenance_counts={},
                 to_fix=[finding("first look", reviewers=["claude"])])
    # And a run that predates the measurement entirely.
    await record(client, 9353, repo=repo,
                 to_fix=[finding("ancient", reviewers=["claude"])])

    s = await stats(client, repo=repo)
    claude = model_row(s, "claude")
    assert claude["provenance"]["introduced"] == 1
    assert claude["provenance"]["missed"] == 0
    assert claude["provenance_runs"] == 2, "the {} round and the silent one are not coverage"
    assert model_row(s, "codex")["provenance"]["missed"] == 1


async def test_coverage_is_never_zero_beside_a_non_zero_split(client):
    """`provenance_runs` gates whether the page shows the split at all, so a
    marker that reads 0 next to sums that do not hides real data behind a claim
    that nothing was measured.

    Reachable without a malicious caller: the run tally is a separate field, and
    a payload that attributes its findings while sending no tally — or one whose
    tally is emptied by the validator for holding unbelievable numbers — used to
    land exactly there."""
    repo = "acme/v226cover"
    await record(client, 9370, repo=repo, head_sha=SHA,
                 provenance_counts={"introduced": -1},   # emptied by the validator
                 to_fix=[finding("attributed anyway", provenance="introduced",
                                 reviewers=["claude"])])
    s = await stats(client, repo=repo)
    claude = model_row(s, "claude")
    assert claude["provenance"]["introduced"] == 1
    assert claude["provenance_runs"] >= 1


# ----------------------------------------------------- #48's axis, per finding

async def test_the_window_split_counts_each_finding_once(client):
    """`by_provenance` is not a sum over `by_model`: a finding two seats agreed on
    is one defect, and the leaderboard would count its cause twice."""
    repo = "acme/v226once"
    await record(client, 9360, repo=repo, head_sha=SHA,
                 to_fix=[finding("agreed", provenance="introduced",
                                 reviewers=["claude", "codex"])])
    s = await stats(client, repo=repo)
    assert s["by_provenance"]["introduced"] == 1
    assert (model_row(s, "claude")["provenance"]["introduced"]
            + model_row(s, "codex")["provenance"]["introduced"]) == 2


async def test_the_window_split_names_what_it_never_asked_about(client):
    """Four buckets are usually a small part of a window, so reporting them alone
    invites the reader to treat their sum as the total. `not_attributed` is every
    finding the question never reached, and it is deliberately NOT `unknown` —
    that one was asked."""
    repo = "acme/v226split"
    await record(client, 9361, repo=repo, head_sha=SHA,
                 to_fix=[finding("a", provenance="introduced"),
                         finding("b", provenance="unknown", file="app/db.py"),
                         finding("c", file="app/sync.py")])
    s = await stats(client, repo=repo)
    assert s["by_provenance"] == {"introduced": 1, "missed": 0, "missed-unread": 0,
                                  "unknown": 1, "not_attributed": 1}


async def test_every_bucket_is_reported_even_when_the_window_is_empty(client):
    """A bucket that vanishes from the object leaves a client guessing whether it
    was zero or unsupported."""
    s = await stats(client, repo="acme/v226nothing")
    assert s["by_provenance"] == {"introduced": 0, "missed": 0, "missed-unread": 0,
                                  "unknown": 0, "not_attributed": 0}
