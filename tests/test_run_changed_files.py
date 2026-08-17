"""v2.23: which FILES a PR touched — the datum collision ordering needs.

A run recorded ``changed_lines: 2032`` and no paths. The only paths the board
held were the ones findings happened to name — nine, for a run whose PR touched
more — so the question integration cost actually turns on was unanswerable:
*which other PRs does landing this one disturb?* #73's disjointness from #62 was
discovered by trying, because nothing recorded it (#82).

Four properties carry the feature, and the tests below are grouped by them:

* **The PR's files, not the round's.** Under #41 a later round reviews only the
  increment; a collision surface that narrowed with it would report two PRs as no
  longer colliding because one stopped RE-READING a file it still changes.
* **``changed_files_total`` is GitHub's count, never ``len(changed_files)``.**
  GitHub caps a PR's file list at 3,000, so the two are allowed to disagree —
  and their disagreement is the only evidence the stored list is a prefix.
* **No list is not an empty list.** Every pre-v2.23 run has no file list at all.
  Reading that as "this PR touches nothing" makes every one of them disjoint from
  everything, which is the most dangerous possible wrong answer here.
* **Every malformed shape records rather than 422s.** Recording is best-effort
  throughout this module: losing a run's findings, scorecards and accounts over
  the shape of its file list is the wrong trade by a wide margin.

Reading the datum back as a collision query is NOT in this release, and the tests
for it are not here. Two full panel rounds put the same defect in that endpoint
twice — a filter composed in front of the newest-run selection, resurrecting a
stale run behind a confident answer, the second instance introduced by the fix
for the first. It ships separately (#101) with the rounds that history says it
needs.
"""

from __future__ import annotations

import json

from .conftest import LAPTOP

REPO = "acme/v223repo"
AGENT = {**LAPTOP, "X-Agent-Instance": "aa11bb"}


def payload(pr: int, **over) -> dict:
    body = {
        "repo": REPO,
        "pr": pr,
        "judged": True,
        "judge_model": "opus",
        "reviewers_selected": ["claude"],
        "reviewers": {"claude": {"model": "opus", "ran": True}},
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


def files(*paths: str) -> list[dict]:
    return [{"path": p, "additions": 10, "deletions": 2} for p in paths]


async def test_the_paths_survive_the_round_trip_with_their_own_churn(client):
    run = await record(client, 8201,
                       changed_lines=124,
                       changed_files=[{"path": "app/api/reviews.py",
                                       "additions": 120, "deletions": 4},
                                      {"path": "harness/loops/panel.py",
                                       "additions": 8, "deletions": 1}],
                       changed_files_total=2)
    d = await detail(client, run["id"])
    assert d["changed_files"] == [
        {"path": "app/api/reviews.py", "additions": 120, "deletions": 4},
        {"path": "harness/loops/panel.py", "additions": 8, "deletions": 1},
    ]
    assert d["changed_files_total"] == 2
    assert d["changed_lines"] == 124

async def test_a_run_list_carries_the_count_without_dumping_every_path(client):
    """`GET /reviews` is a page of runs. A file list per row turns it into a
    file dump; the count is what a list needs, which is that a list exists."""
    await record(client, 8202, changed_files=files("r/a.py", "r/b.py", "r/c.py"),
                 changed_files_total=3)
    r = await client.get("/reviews", params={"repo": REPO, "pr": 8202},
                         headers=AGENT)
    assert r.status_code == 200, r.text
    [row] = r.json()
    assert row["changed_files_total"] == 3
    assert "changed_files" not in row

async def test_a_bare_path_list_records_rather_than_422ing_the_run_away(client):
    """The shape a hand-rolled caller reaches for first. Recording is best-effort
    throughout this module — losing a run's findings over the shape of its file
    list would be the wrong trade by a wide margin."""
    run = await record(client, 8203, changed_files=["x.py", "y.py"])
    d = await detail(client, run["id"])
    assert [f["path"] for f in d["changed_files"]] == ["x.py", "y.py"]
    assert d["changed_files"][0]["additions"] is None

async def test_a_repeated_path_is_stored_once(client):
    """The table's unique constraint would otherwise turn a sender bug into an
    IntegrityError costing the whole run — and a path counted twice doubles that
    file's weight in every collision count built on it."""
    run = await record(client, 8204, changed_files=files("dup.py", "dup.py", "other.py"))
    d = await detail(client, run["id"])
    assert [f["path"] for f in d["changed_files"]] == ["dup.py", "other.py"]

async def test_a_truncated_list_is_detectable_because_the_count_is_stored_apart(client):
    """The property the feature rests on. GitHub caps a PR's file list at 3,000.
    A consumer that could only read `len(changed_files)` cannot tell a 3,000-file
    prefix from a 3,000-file PR, and every collision answer built on the prefix is
    wrong in the direction of "no collision"."""
    run = await record(client, 8205, changed_files=files("t/a.py", "t/b.py"),
                       changed_files_total=2500)
    d = await detail(client, run["id"])
    assert len(d["changed_files"]) == 2
    assert d["changed_files_total"] == 2500

async def test_paths_without_a_count_leave_the_count_null_rather_than_backfilled(client):
    """Backfilling from the rows manufactures agreement between exactly the two
    numbers whose DISAGREEMENT is the evidence. NULL is the honest value: nobody
    said how many there were."""
    run = await record(client, 8206, changed_files=files("n/a.py"))
    assert (await detail(client, run["id"]))["changed_files_total"] is None

async def test_a_pre_v223_run_has_no_list_which_is_not_an_empty_pr(client):
    run = await record(client, 8207)
    d = await detail(client, run["id"])
    assert d["changed_files"] == []
    assert d["changed_files_total"] is None

async def test_pr_state_is_recorded_case_insensitively(client):
    run = await record(client, 8335, changed_files=files("c/x.py"),
                       changed_files_total=1, pr_state="open", is_draft=True)
    d = await detail(client, run["id"])
    assert d["pr_state"] == "OPEN" and d["is_draft"] is True

async def test_a_blank_or_padded_path_is_dropped_or_folded_server_side(client):
    """F27. The strip/dedup happens in `record_review`, and the harness tests
    cover `panel._changed_files` — a different component. A future refactor that
    drops the `.strip()` reintroduces both an IntegrityError risk and silent
    collision misses, and nothing would have caught it."""
    run = await record(client, 8360,
                       changed_files=[" pad.py", "pad.py", "   ", "", "real.py"])
    d = await detail(client, run["id"])
    assert [f["path"] for f in d["changed_files"]] == ["pad.py", "real.py"]

async def test_a_malformed_file_entry_never_costs_the_run_its_findings(client):
    """F23. The docstring claimed best-effort and delivered it for exactly one
    shape. A negative churn number, a null path, a bare int in the array and
    `changed_files: null` each 422'd the whole payload — findings included — for
    a reason the module's own rule says is not worth losing them for."""
    for bad in (
        [{"path": "neg.py", "additions": -1, "deletions": -5}],
        [{"path": None}],
        [12345],
        None,
        "not a list at all",
        [{"path": "junk.py", "additions": "loads"}],
    ):
        r = await client.post("/review", json=payload(8370, changed_files=bad),
                              headers=AGENT)
        assert r.status_code == 201, f"{bad!r} cost the run: {r.text}"

    # ...and a bad churn number lands as NULL — "nobody said" — rather than as a
    # number that cannot be true.
    run = await record(client, 8371,
                       changed_files=[{"path": "neg.py", "additions": -1, "deletions": 3}])
    [f] = (await detail(client, run["id"]))["changed_files"]
    assert f["additions"] is None and f["deletions"] == 3

async def test_an_oversized_file_list_is_truncated_and_not_refused(client):
    """F24. `POST /review` is authenticated, which is not the same as bounded: an
    unbounded list is one buggy sender away from a million rows in one
    transaction. Truncation keeps the run's findings; refusal would not."""
    from app.api.reviews import MAX_CHANGED_FILES
    run = await record(client, 8380,
                       changed_files=[f"big/f{i}.py" for i in range(MAX_CHANGED_FILES + 50)])
    assert len((await detail(client, run["id"]))["changed_files"]) == MAX_CHANGED_FILES


# ---- round 2's P2s against the half that lands ------------------------------

async def test_an_unrepresentable_churn_number_never_500s_the_request(client):
    """Round 2, F14. `_churn` caught TypeError/ValueError only, and JSON `1e309`
    parses to `inf` — which clears the isinstance gate and then raises
    OverflowError out of the validator, 500-ing the request. In the one model
    whose entire documented rule is that a malformed file list must not cost a
    run its findings."""
    # Posted as RAW text, not via `json=`: httpx's encoder refuses `inf`, while a
    # real body carries the literal `1e309` and `json.loads` turns it into `inf`
    # on the server. Serialising through the client would test the client.
    body = json.dumps(payload(8401)).rstrip("}")
    body += ', "changed_files": [{"path": "huge.py", "additions": 1e309, ' \
            '"deletions": -1e309}, {"path": "frac.py", "additions": 3.7, ' \
            '"deletions": 2.0}]}'
    r = await client.post("/review", content=body, headers={
        **AGENT, "Content-Type": "application/json"})
    assert r.status_code == 201, r.text
    by_path = {f["path"]: f for f in (await detail(client, r.json()["id"]))["changed_files"]}
    assert by_path["huge.py"]["additions"] is None
    assert by_path["huge.py"]["deletions"] is None
    # A non-integral count is "nobody said", not a silent truncation to 3 — every
    # other unrepresentable value in this validator becomes None rather than
    # quietly changing. An integral float is a plain number and survives.
    assert by_path["frac.py"]["additions"] is None
    assert by_path["frac.py"]["deletions"] == 2


async def test_an_unrecognised_pr_state_is_null_rather_than_stored_verbatim(client):
    """Round 2, F17. Stored as-is, a typo or a future GitHub state is worse than
    useless to a consumer filtering on it: anything `!= "OPEN"` silently
    reclassifies the PR, and in the direction that hides work. NULL is the value
    every consumer already handles, because every pre-v2.23 run carries it."""
    for bad in ("OPNE", "Opened", "DRAFT", "", 42, None, ["OPEN"]):
        run = await record(client, 8402, changed_files=files("st/x.py"), pr_state=bad)
        assert (await detail(client, run["id"]))["pr_state"] is None, bad
    for good, want in (("open", "OPEN"), ("MERGED", "MERGED"), (" closed ", "CLOSED")):
        run = await record(client, 8403, changed_files=files("st/y.py"), pr_state=good)
        assert (await detail(client, run["id"]))["pr_state"] == want


async def test_a_truncated_file_list_is_reported_back_to_the_sender(client):
    """Round 2, F16. The docstring promised "truncated with a note rather than
    refused, and never silently" and there was no channel through which any note
    could reach the sender — so a caller posting 6,000 paths got a run short by
    1,000 and read its own complete-looking list as evidence of no collision."""
    from app.api.reviews import MAX_CHANGED_FILES
    r = await client.post("/review", json=payload(
        8404, changed_files=[f"big/f{i}.py" for i in range(MAX_CHANGED_FILES + 40)],
    ), headers=AGENT)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["changed_files"] == MAX_CHANGED_FILES
    assert body["changed_files_dropped"] == {"over_cap": 40, "unusable": 0}


async def test_unusable_entries_are_reported_separately_from_the_cap(client):
    """Two different facts about why a list is short, and they have two different
    fixes — the same distinction the harness keeps between a truncated list and a
    dropped malformed row."""
    r = await client.post("/review", json=payload(
        8405, changed_files=["real.py", "  ", "", {"path": None}, "real.py"],
    ), headers=AGENT)
    assert r.status_code == 201, r.text
    assert r.json()["changed_files_dropped"] == {"over_cap": 0, "unusable": 3}
    # A repeated path is deduped, not "dropped" — it was usable and is stored.
    assert r.json()["changed_files"] == 1


async def test_an_ordinary_run_keeps_the_response_shape_callers_already_parse(client):
    r = await client.post("/review", json=payload(8406, changed_files=files("ok/x.py")),
                          headers=AGENT)
    assert "changed_files_dropped" not in r.json()
