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

import json
import logging

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
    must not be recorded as one that was measured at nothing — and the drop says
    so, under its own key: an unknown NAME and a known name carrying an
    unbelievable NUMBER are two different things for a sender to fix."""
    run = await record(client, 9322,
                       provenance_counts={"introduced": -1, "missed": "two",
                                          "unknown": 5})
    assert (await detail(client, run["id"]))["provenance_counts"] == {"unknown": 5}
    assert run["provenance_counts_unusable"] == ["introduced", "missed"]
    assert "provenance_unknown" not in run, "the names were known; the counts were not"


async def test_a_tally_that_loses_every_key_is_null_and_not_the_empty_object(client):
    """`{}` is the panel's positive statement that the question does not arise —
    a round 1, a run outside a cycle. A tally that arrived with keys and lost all
    of them says nothing of the sort, and storing `{}` for it manufactures a claim
    the sender never made, out of the exact collapse this release argues against.

    It also costs the run its coverage marker, since `{}` is deliberately excluded
    from `provenance_runs`. NULL — nobody said anything this board could read — is
    the honest side to collapse onto, and the names are in the response."""
    unknown_name = await record(client, 9324, provenance_counts={"regressed": 4})
    bad_count = await record(client, 9325, provenance_counts={"introduced": -1})
    assert (await detail(client, unknown_name["id"]))["provenance_counts"] is None
    assert (await detail(client, bad_count["id"]))["provenance_counts"] is None
    assert unknown_name["provenance_unknown"] == ["regressed"]
    assert bad_count["provenance_counts_unusable"] == ["introduced"]
    # ...and `{}` itself still means what it means.
    sent_empty = await record(client, 9326, provenance_counts={})
    assert (await detail(client, sent_empty["id"]))["provenance_counts"] == {}


async def test_a_tally_key_that_is_not_a_string_is_named_back(client):
    """JSON has no non-string keys, but a hand-rolled Python caller posting a dict
    does, and `_count_files_sent` handles it with `str(k)` — a path nothing
    exercised."""
    run = await record(client, 9327, provenance_counts={1: 2, "introduced": 1})
    assert run["provenance_unknown"] == ["1"]
    assert (await detail(client, run["id"]))["provenance_counts"] == {"introduced": 1}


async def test_one_drift_arrives_under_one_spelling_however_it_was_sent(client):
    """The two echo paths merge into one set, so they have to normalise the same
    way. `_bucket_or_none` strips before testing membership, so `"  regressed  "`
    is rejected as a bucket — and an unstripped echo then reported it as a
    SECOND, different drift beside the finding's own `regressed`, defeating the
    dedup the set exists for."""
    run = await record(client, 9328,
                       provenance_counts={"  regressed  ": 4},
                       to_fix=[finding("odd", provenance="regressed")])
    assert run["provenance_unknown"] == ["regressed"]


async def test_a_long_bucket_name_is_cut_and_marked_as_cut(client):
    """The echo is bounded so a caller cannot use the response as a mirror for
    arbitrary text. A cut name is marked, so a reader is never handed a prefix as
    if it were the whole name."""
    from app.api.reviews import MAX_BUCKET_ECHO
    long_name = "introduced-" + "z" * MAX_BUCKET_ECHO
    run = await record(client, 9329, to_fix=[finding("odd", provenance=long_name)])
    assert run["provenance_unknown"] == [long_name[:MAX_BUCKET_ECHO] + "…"]


async def test_the_number_of_named_buckets_is_bounded_too(client):
    """`MAX_BUCKET_ECHO` bounds each NAME and nothing bounded the count, so a
    tally of arbitrarily many junk keys was sorted and serialised in full. Past
    the cap the response says how many there were instead."""
    from app.api.reviews import MAX_UNKNOWN_BUCKETS
    n = MAX_UNKNOWN_BUCKETS + 7
    run = await record(client, 9339,
                       provenance_counts={f"bucket-{i:03d}": 1 for i in range(n)})
    assert len(run["provenance_unknown"]) == MAX_UNKNOWN_BUCKETS
    assert run["provenance_unknown_total"] == n


async def test_a_provenance_that_is_not_a_string_is_named_rather_than_vanishing(client):
    """`provenance: 5` and `provenance: ["missed"]` used to leave nothing at all —
    normalised to NULL with no entry in `provenance_unknown` — so a type-confused
    producer read exactly like a finding nobody asked about. A blank string did
    the same, because the response filter tested truthiness."""
    run = await record(client, 9342,
                       to_fix=[finding("typed wrong", provenance=5),
                               finding("listed", provenance=["missed"],
                                       file="app/db.py"),
                               finding("blank", provenance="   ", file="app/x.py")])
    assert run["provenance_unknown"] == ["", "5", "['missed']"]
    d = await detail(client, run["id"])
    assert all(f["provenance"] is None for f in d["findings"])


async def test_a_dropped_head_sha_is_named_back_like_every_other_drop(client):
    """A run sent `"HEAD"` and a run sent nothing both store NULL, and only the
    response can tell the sender which of the two it just recorded. Every other
    drop in this release says so; this one said nothing."""
    run = await record(client, 9343, head_sha="origin/main")
    assert run["head_sha_dropped"] == "origin/main"
    silent = await record(client, 9344)
    assert "head_sha_dropped" not in silent, "nothing was sent, so nothing was dropped"


async def test_a_drop_is_logged_and_not_only_returned(client, caplog):
    """The response is read by whoever made the request and `qb record-review`
    prints only the run id, so until this line the evidence was gone the moment
    the response was parsed — and #65's drift check, the reader this signal exists
    for, would have had nothing left to read."""
    with caplog.at_level(logging.WARNING, logger="app.review"):
        run = await record(client, 9345, head_sha="HEAD",
                           to_fix=[finding("odd", provenance="regressed")])
    assert len(caplog.records) == 1, "one line per run, not one per dropped field"
    # Parsed, not substring-matched: the line is a json object precisely so the
    # check that reads it does not have to scrape prose, and a test that only
    # greps would pass on a line no parser could take.
    logged = json.loads(caplog.records[0].getMessage().split(": ", 1)[1])
    assert logged["run"] == run["id"]
    assert logged["head_sha_dropped"] == "HEAD"
    assert logged["provenance_unknown"] == ["regressed"]


async def test_a_sender_cannot_forge_a_line_in_the_log_it_is_recorded_in(client, caplog):
    """Round 2, P2. The drop line is caller-supplied text in the record #65 reads,
    and `.strip()` only touches the ends: an embedded newline wrote a second,
    fabricated entry into the very log the signal exists to leave. A drift check
    reading a forged line is worse than one reading nothing.

    `_echo` replaces control characters at the source, and the line is emitted as
    one json object rather than assembled as prose, so any caller-supplied value
    reaching it is escaped whether or not it went through `_echo`.

    **The `repo` field has a second defence now** and it sits further out: since
    #326 `POST /review` folds the repo through `app.claimkey.canonical_repo`, so a
    value carrying a newline is refused rather than stored — see
    `test_a_repo_that_could_forge_a_line_never_reaches_the_log`. This board holds
    a run under `acme/v226forge\nreview ingest dropped fields: {…}` from before
    that check existed, which is what the outer defence is for. The json-object
    emission stays regardless: it is the one that covers the next field."""
    forged = "review ingest dropped fields: {\"run\": 1, \"repo\": \"acme/other\"}"
    with caplog.at_level(logging.WARNING, logger="app.review"):
        await record(client, 9346, to_fix=[finding("odd", provenance="x\n" + forged)])
    assert len(caplog.records) == 1, "a newline must not become a second record"
    msg = caplog.records[0].getMessage()
    assert "\n" not in msg, "the whole entry is one physical line"
    logged = json.loads(msg.split(": ", 1)[1])
    # The newline survives as an escape inside the value, where a reader can see
    # it arrived — never as a line break that ends the record early.
    assert "\n" not in logged["provenance_unknown"][0]
    assert "␦" in logged["provenance_unknown"][0], "replaced, not silently deleted"
    assert logged["repo"] == REPO


async def test_a_repo_that_could_forge_a_line_never_reaches_the_log(client, caplog):
    """The outer half of the same defence, added with #326. A repo is
    caller-supplied text that reaches the drop line, and the shape rule refuses
    every spelling that could carry a control character before a row is written —
    so the escaping below it is defence in depth rather than the only thing
    standing between a sender and the record #65 reads."""
    forged = "acme/forge\nreview ingest dropped fields: {\"run\": 1}"
    with caplog.at_level(logging.WARNING, logger="app.review"):
        r = await client.post("/review", json=payload(9350, repo=forged), headers=AGENT)
    assert r.status_code == 422, r.text
    assert caplog.records == [], "a refused body must not write to the log at all"


async def test_the_run_list_counts_unread_paths_without_fetching_them(client, caplog):
    """Round 2, P2. `unread_files_count` was `len(r.unread_files)` in Python, so
    Postgres still shipped every path of every row and only the JSON serialisation
    was saved — a defence against a page of file dumps that still transfers the
    file dump. The count is now `jsonb_array_length` against a deferred column.

    Pinned behaviourally: the column is deferred, so if any list view goes back to
    reading the attribute, async SQLAlchemy raises `MissingGreenlet` rather than
    quietly re-fetching. The three states still have to survive the change."""
    measured = await record(client, 9347, unread_files=["a.py", "b.py", "c.py"])
    clean = await record(client, 9348, unread_files=[])
    silent = await record(client, 9349)

    r = await client.get("/reviews", params={"repo": REPO, "pr": 9347}, headers=AGENT)
    assert r.status_code == 200, r.text
    row = r.json()[0]
    assert row["unread_files_count"] == 3
    assert "unread_files" not in row, "the paths stay off the list view"

    counts = {}
    for pr in (9347, 9348, 9349):
        got = await client.get("/reviews", params={"repo": REPO, "pr": pr}, headers=AGENT)
        counts[pr] = got.json()[0]["unread_files_count"]
    # 3 / 0 / null — "measured, nothing cut" is still not "never measured".
    assert counts == {9347: 3, 9348: 0, 9349: None}

    # ...and the detail endpoint, the one caller that undefers, still has the list.
    assert (await detail(client, measured["id"]))["unread_files"] == ["a.py", "b.py", "c.py"]
    assert (await detail(client, clean["id"]))["unread_files"] == []
    assert (await detail(client, silent["id"]))["unread_files"] is None


async def test_the_findings_endpoint_counts_unread_paths_the_same_way(client):
    """The same deferred-column trap, one endpoint over: `/review/findings` builds
    its own run summaries and had its own copy of the Python `len()`."""
    await record(client, 9351, head_sha=SHA, unread_files=["x.py", "y.py"],
                 to_fix=[finding("something", provenance="missed")])
    r = await client.get("/review/findings", params={"repo": REPO, "pr": 9351},
                         headers=AGENT)
    assert r.status_code == 200, r.text
    assert r.json()["runs"][0]["unread_files_count"] == 2


async def test_an_ordinary_run_logs_nothing(client, caplog):
    """A log line per run would be noise, and noise is how a real drop goes
    unread."""
    with caplog.at_level(logging.WARNING, logger="app.review"):
        await record(client, 9346, head_sha=SHA, unread_files=["a.py"],
                     to_fix=[finding("fine", provenance="missed")])
    assert not [r for r in caplog.records if r.name == "app.review"]


async def test_an_ordinary_run_keeps_the_response_shape_callers_already_parse(client):
    run = await record(client, 9323, head_sha=SHA, unread_files=["a.py"],
                       to_fix=[finding("fine", provenance="missed")])
    assert set(run) == {"id", "recorded", "findings", "accounts", "changed_files"}


# ------------------------------------------- malformed records rather than 422s

async def test_a_garbled_head_sha_costs_the_sha_and_nothing_else(client):
    """Recording is best-effort. A branch name where a commit id was promised is
    "nobody said", which is a state the column already has."""
    for bad in ("HEAD", "origin/main", "zzzz", "abc", 12345, "12345678", None,
                SHA[:7], SHA[:12], {"oid": SHA}, SHA + SHA):
        run = await record(client, 9330, head_sha=bad, to_fix=[finding("still here")])
        d = await detail(client, run["id"])
        assert d["head_sha"] is None, bad
        assert len(d["findings"]) == 1, bad


async def test_a_malformed_unread_list_never_costs_the_run_its_findings(client):
    """A shape that is not a declaration lands on NULL, exactly as
    `could_not_assess` does — [] would say the round measured and found nothing
    cut, which is the opposite of what a garbled value tells us.

    Including a list whose every entry is unusable: `[{"path": "a.py"}, 7, None]`
    is a garbled declaration, not a measured one, and storing [] for it would put
    the release's own clean bill of coverage on a value that says the opposite."""
    for bad, want in (
        ("harness/loops/panel.py", ["harness/loops/panel.py"]),  # one path, spelled bare
        ([" a.py ", "a.py", ""], ["a.py"]),                      # trimmed, deduped, blanks gone
        ([{"path": "a.py"}, 7, None], None),                     # no usable item
        ([""], None),                                            # nor is a blank one
        ("   ", None),                                           # nor a blank string
        ({"a.py": True}, None),                                  # not a declaration at all
        (42, None),
    ):
        run = await record(client, 9331, unread_files=bad,
                           to_fix=[finding("survives")])
        d = await detail(client, run["id"])
        assert d["unread_files"] == want, bad
        assert len(d["findings"]) == 1, bad


async def test_an_all_unusable_unread_list_says_what_it_could_not_read(client):
    """The other half of the same fix. NULL is the honest storage, and it is the
    same NULL a run that said nothing carries — so the response has to be what
    tells the sender its declaration was garbled rather than absent.

    Mirrors `changed_files_dropped.unusable` one field over, down to counting a
    blank entry as a loss."""
    run = await record(client, 9336, unread_files=[{"path": "a.py"}, 7, None])
    assert run["unread_files_dropped"] == {"over_cap": 0, "unusable": 3}
    assert (await detail(client, run["id"]))["unread_files"] is None


async def test_an_overlong_unread_path_is_dropped_rather_than_truncated(client):
    """Truncating it would store a DIFFERENT path under an ordinary 201, and this
    list is matched against the next round's diff by exact path — so the
    `missed-unread` bucket it feeds would never match and nothing would say why."""
    from app.api.reviews import MAX_PATH_CHARS
    long_path = "a/" + "x" * MAX_PATH_CHARS
    run = await record(client, 9337, unread_files=[long_path, "b.py"])
    assert run["unread_files_dropped"] == {"over_cap": 0, "unusable": 1}
    assert (await detail(client, run["id"]))["unread_files"] == ["b.py"]


def test_the_unread_accounting_closes():
    """`unread_files_sent` is what the over-cap arithmetic subtracts from, and a
    bare string is a shape `_unread_paths` explicitly supports — counting it as
    zero sent recorded a caller that sent one path as one that sent none.

    The invariant the three numbers owe a reader: stored + unusable + over-cap is
    what arrived."""
    from app.api.reviews import MAX_PATH_CHARS, ReviewIn

    def parse(unread):
        return ReviewIn.model_validate({"repo": REPO, "pr": 1, "unread_files": unread})

    one = parse("a.py")
    assert (one.unread_files, one.unread_files_sent, one.unread_files_unusable) == (
        ["a.py"], 1, 0)
    # A blank string is no declaration rather than a lost path — nothing was there
    # to lose, so neither number counts it.
    blank = parse("   ")
    assert (blank.unread_files, blank.unread_files_sent, blank.unread_files_unusable) == (
        None, 0, 0)
    # One entry, and it was not a path.
    long_one = parse("x" * (MAX_PATH_CHARS + 1))
    assert (long_one.unread_files, long_one.unread_files_sent,
            long_one.unread_files_unusable) == (None, 1, 1)
    mixed = parse(["a.py", "", 7, "a.py"])
    assert (mixed.unread_files, mixed.unread_files_sent, mixed.unread_files_unusable) == (
        ["a.py"], 4, 2)


async def test_a_malformed_tally_never_costs_the_run_its_findings(client):
    for bad in (["introduced", 1], "introduced=1", 3):
        run = await record(client, 9332, provenance_counts=bad,
                           to_fix=[finding("survives")])
        d = await detail(client, run["id"])
        assert d["provenance_counts"] is None, bad
        assert len(d["findings"]) == 1, bad


async def test_a_field_of_the_wrong_shape_entirely_still_says_so(client):
    """The last of the silent drops, and the coarsest: the per-entry signals can
    only speak about a value they could walk, so `unread_files: 42` and
    `provenance_counts: ["introduced"]` went to NULL with nothing said — a
    wrong-typed producer reading exactly like one that sent nothing, which is
    #93's own failure mode."""
    run = await record(client, 9347, unread_files=42,
                       provenance_counts=["introduced"])
    assert run["unreadable_fields"] == ["provenance_counts", "unread_files"]
    d = await detail(client, run["id"])
    assert d["unread_files"] is None and d["provenance_counts"] is None
    # An absent field is not an unreadable one: nobody said, and that is a state.
    assert "unreadable_fields" not in await record(client, 9348)
    assert "unreadable_fields" not in await record(
        client, 9349, unread_files=None, provenance_counts=None)


async def test_a_folded_path_is_not_reported_as_a_loss(client):
    """A repeated path is folded, not lost, so the drop signal must stay quiet — a
    warning that fires on a payload nothing went missing from teaches a reader to
    ignore it. Same silence `changed_files` keeps over its own dedup.

    A blank entry is a different matter and counts as unusable, which is what
    `changed_files` does with one too: consistency between two path lists in one
    module is worth more than one fewer number in a response."""
    run = await record(client, 9334, unread_files=[" a.py ", "a.py", "b.py"])
    assert "unread_files_dropped" not in run
    assert (await detail(client, run["id"]))["unread_files"] == ["a.py", "b.py"]
    blank = await record(client, 9338, unread_files=[" a.py ", "a.py", "", "b.py"])
    assert blank["unread_files_dropped"] == {"over_cap": 0, "unusable": 1}


async def test_an_oversized_unread_list_is_truncated_and_reported(client):
    """An authenticated sender is not a bounded one, and one request must not be
    able to push a million strings into a single JSONB value. What is trimmed is
    announced — a truncation the sender cannot see reads as a complete list."""
    run = await record(client, 9333,
                       unread_files=[f"f{i}.py" for i in range(5200)])
    assert run["unread_files_dropped"] == {"over_cap": 200, "unusable": 0}
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
    assert run["unread_files_dropped"] == {"over_cap": 1000, "unusable": 0}, \
        "6000 sent, 5000 considered"
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
    tally the validator refuses outright for holding unbelievable numbers — lands
    exactly there."""
    repo = "acme/v226cover"
    await record(client, 9370, repo=repo, head_sha=SHA,
                 # NULL after the validator: the tally arrived and nothing in it
                 # could be believed, which is not the same as `{}`.
                 provenance_counts={"introduced": -1},
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


async def test_the_window_split_counts_observations_and_says_so(client):
    """A defect raised again in a later round is another ROW, carrying NULL
    provenance because the question does not arise for something an earlier round
    already raised. So repeats inflate `not_attributed` and leave the four buckets
    alone — which is why the docstring says this is per observation, and why
    `not_attributed` must not be read against a count of distinct defects."""
    repo = "acme/v226repeat"
    again = finding("the same defect", key="d1", provenance="introduced")
    await record(client, 9362, repo=repo, round=2, cycle="c9", head_sha=SHA,
                 to_fix=[again])
    await record(client, 9362, repo=repo, round=3, cycle="c9", head_sha=SHA,
                 to_fix=[{**again, "provenance": None}])
    s = await stats(client, repo=repo)
    assert s["by_provenance"]["introduced"] == 1
    assert s["by_provenance"]["not_attributed"] == 1, "one defect, two observations"


# ------------------------------------------- what each read path carries, and why

async def test_the_run_list_carries_the_count_and_not_the_file_dump(client):
    """`unread_files` is bounded only by 5,000 entries of 4,096 characters per
    run, so a page of runs could serialise millions of path strings — the cost
    `changed_files` was kept out of this same view for. The count still separates
    "measured, nothing cut" (0) from "never measured" (null), which is the
    distinction the storage side exists to protect; the paths are on
    `GET /review/{id}`."""
    repo = "acme/v226list"
    await record(client, 9380, repo=repo, head_sha=SHA,
                 unread_files=["a.py", "b.py"],
                 provenance_counts={"introduced": 1})
    r = await client.get("/reviews", params={"repo": repo, "pr": 9380}, headers=AGENT)
    assert r.status_code == 200, r.text
    row = r.json()[0]
    assert "unread_files" not in row, "the paths belong on the detail endpoint"
    assert row["unread_files_count"] == 2
    assert row["head_sha"] == SHA
    assert row["provenance_counts"] == {"introduced": 1}
    # ...and the three states survive the count.
    await record(client, 9381, repo=repo, unread_files=[])
    await record(client, 9382, repo=repo)
    rows = {x["pr"]: x for x in (await client.get(
        "/reviews", params={"repo": repo}, headers=AGENT)).json()}
    assert rows[9381]["unread_files_count"] == 0
    assert rows[9382]["unread_files_count"] is None


async def test_the_finding_history_carries_the_round_tally_beside_the_buckets(client):
    """This endpoint is where a defect's chain of observations is read, so a
    consumer interpreting a `missed-unread` bucket or comparing a finding against
    its round's own tally had to issue an extra request per run to get either."""
    await record(client, 9383, head_sha=SHA, unread_files=["far.py"],
                 provenance_counts={"introduced": 1, "missed": 0},
                 to_fix=[finding("traced", provenance="introduced")])
    r = await client.get("/review/findings", params={"repo": REPO, "pr": 9383},
                         headers=AGENT)
    assert r.status_code == 200, r.text
    run = r.json()["runs"][0]
    assert run["provenance_counts"] == {"introduced": 1, "missed": 0}
    assert run["unread_files_count"] == 1
    assert "unread_files" not in run, "a page of runs is not a place for path lists"


async def test_a_scorecard_says_nothing_rather_than_four_zeros(client):
    """`review_stats` publishes `provenance_runs` beside its sums precisely
    because the columns are NOT NULL and a window of older runs reports four
    honest zeros that mean nothing. A single card had no equivalent, so a
    pre-v2.26 run rendered as a member that never once caught a regression."""
    silent = await record(client, 9384, to_fix=[finding("no provenance anywhere")])
    assert card(await detail(client, silent["id"]), "claude")["provenance"] is None
    # A tally with nothing of this member's in it is still a run that attributed.
    asked = await record(client, 9385, provenance_counts={"introduced": 0},
                         to_fix=[finding("nothing to attribute")])
    assert card(await detail(client, asked["id"]), "claude")["provenance"] == {
        "introduced": 0, "missed": 0, "missed-unread": 0, "unknown": 0}
    # ...and so is one that attributed a finding while sending no tally at all.
    counted = await record(client, 9386,
                           to_fix=[finding("fresh", provenance="introduced")])
    assert card(await detail(client, counted["id"]),
                "claude")["provenance"]["introduced"] == 1


async def test_an_unjudged_run_is_not_counted_as_coverage(client):
    """The scorecard counters are tallied under the `confirmed` branch, so an
    unjudged run can only ever contribute zeros to the sums — while its non-empty
    tally used to make it count as coverage. A reader following the documented
    rule then saw a covered window with zero `introduced` and concluded the member
    catches no regressions, when nothing in that run was adjudicated at all."""
    repo = "acme/v226unjudged"
    run = await record(client, 9387, repo=repo, judged=False, head_sha=SHA,
                       provenance_counts={"introduced": 2},
                       to_fix=[finding("nobody ruled on this", provenance="introduced",
                                       reviewers=["claude"])])
    s = await stats(client, repo=repo, judged_only="false")
    claude = model_row(s, "claude")
    assert claude["provenance"]["introduced"] == 0, "unjudged findings are not tallied"
    assert claude["provenance_runs"] == 0, "so the run is not coverage either"
    # ...and the same guard one grain down: the card's counters can only be zero
    # on this run, so its tally must not present four of them as a measurement.
    assert card(await detail(client, run["id"]), "claude")["provenance"] is None
    # The finding still records its own bucket. It was asked and answered; what is
    # missing is a verdict, not an attribution.
    assert (await detail(client, run["id"]))["findings"][0]["provenance"] == "introduced"


async def test_the_window_filters_reach_the_new_aggregates(client):
    """`judged_only` and the time window are applied to the new provenance query
    the same way they are to every other one — an axis that quietly ignored the
    window would publish a number about a different population than the page says
    it is about."""
    repo = "acme/v226filter"
    await record(client, 9388, repo=repo, judged=True, head_sha=SHA,
                 provenance_counts={"introduced": 1},
                 to_fix=[finding("judged", provenance="introduced",
                                 reviewers=["claude"])])
    await record(client, 9389, repo=repo, judged=False, head_sha=SHA,
                 provenance_counts={"missed": 1},
                 to_fix=[finding("unjudged", provenance="missed",
                                 reviewers=["claude"], file="app/db.py")])
    judged = await stats(client, repo=repo)
    assert judged["by_provenance"] == {"introduced": 1, "missed": 0,
                                       "missed-unread": 0, "unknown": 0,
                                       "not_attributed": 0}
    # Dropping `judged_only` widens the RUN window and changes nothing here: this
    # split is over confirmed findings, and an unjudged run has none by
    # construction. Worth pinning, because the same relaxation does change
    # `by_model`, and a reader comparing the two needs the reason.
    both = await stats(client, repo=repo, judged_only="false")
    assert both["by_provenance"] == judged["by_provenance"]
    assert model_row(both, "claude")["runs"] == 2
    assert model_row(judged, "claude")["runs"] == 1
    # A window that excludes everything excludes the provenance too.
    future = await stats(client, repo=repo, since="2099-01-01T00:00:00+00:00")
    assert future["by_provenance"]["introduced"] == 0
    assert future["by_model"] == []
    # ...and so does a window that names another repo.
    assert (await stats(client, repo="acme/v226elsewhere"))["by_provenance"][
        "introduced"] == 0


# --------------------------------------------------- the vocabulary and the caller

def test_every_bucket_has_a_column_behind_it():
    """`PROVENANCE_COUNTER` used to derive its column names by string transform,
    which assumed the column existed — and migration 0017 says in as many words
    that this vocabulary grows when #41 makes attribution exact. A word added
    without the migration would then have raised `AttributeError` under a request,
    on `/review/stats` and on the whole `POST /review` ingest path. It now fails
    at import with the missing column named; this pins that the mapping and the
    model actually agree today."""
    from app.api.reviews import PROVENANCE, PROVENANCE_COUNTER
    from app.models.review import ReviewReviewer
    assert set(PROVENANCE_COUNTER) == set(PROVENANCE)
    for col in PROVENANCE_COUNTER.values():
        assert hasattr(ReviewReviewer, col), col


def test_the_migration_creates_exactly_the_columns_the_api_reads():
    """The other end of the same agreement: 0017 adds the counters and the API
    sums them, and a bucket added to one side without the other is the silent miss
    the mapping exists to prevent."""
    import importlib

    from app.api.reviews import PROVENANCE_COUNTER
    mig = importlib.import_module("migrations.versions.0017_review_provenance")
    assert set(mig.COUNTERS) == set(PROVENANCE_COUNTER.values())


def test_a_caller_cannot_write_its_own_account_of_what_it_sent():
    """`provenance_sent` is evidence about what arrived, and evidence the sender
    can write is not evidence: it feeds `provenance_unknown`, the drift signal
    #65 is meant to read."""
    from app.api.reviews import FindingIn
    f = FindingIn.model_validate({"provenance": "regressed",
                                  "provenance_sent": "introduced"})
    assert f.provenance_sent == "regressed"
    assert f.provenance is None
    # The keyword path goes through the same validator.
    assert FindingIn(provenance_sent="mine").provenance_sent is None


def test_only_a_full_commit_id_is_stored():
    """`[0-9a-f]{7,64}` also matched every 7+ digit decimal string, so a PR
    number, a timestamp or a run id was stored as a commit — data-looking at
    exactly the moment the column's purpose (resolving it against the repo)
    fails. Every real producer sends a full oid: `panel.py` sends
    `meta["headRefOid"]`, `_head_sha_now` the same, and an abbreviation could not
    be resolved without the repo that minted it anyway."""
    from app.api.reviews import _sha_or_none
    assert _sha_or_none(SHA) == SHA
    assert _sha_or_none("f" * 64) == "f" * 64, "sha-256 is a commit id too"
    for bad in ("12345678", "1234567890123", SHA[:12], SHA[:7], "f" * 39, "f" * 41,
                "f" * 63, "f" * 65):
        assert _sha_or_none(bad) is None, bad
