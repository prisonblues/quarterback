"""#94: a run that reviewed nothing is recorded, and says which it is.

`panel.py` declines to review a merge, a promote or a format-the-world commit —
those titles match `skip_title_patterns` and an LLM round on them buys nothing —
and it used to return before telling the board anything. The board therefore held
no changed-file list for precisely the pull requests that touch the most files,
collide with the most work and get merged unattended most often, and
`GET /review/collisions` saw a skipped PR as **neither subject nor rival**.

The early return was right on its own terms and is not what changed: no review
happened, and a non-event recorded as an event is a disease this board has spent
a dozen fixes on. What was missing was a column able to say so. `review_runs`
gains `reviewed` and `skip_reason` — both fields the panel has been sending on
every exit for releases, and the board discarding.

Three groups here, and the second is the one that would otherwise have shipped a
new false all-clear:

* **the blind spot** — a skipped PR is a subject and a rival like any other, and
  the collision handler tests `reviewed` nowhere at all;
* **the consumers** — making a skipped run visible also makes it the *newest* run
  for its PR, and several readers take the newest run to mean the state of
  review. `GET /review/findings` would have flipped every outstanding finding to
  `gone`, and the review queue would have counted a merge toward its round cap.
  Both were caught by an independent second opinion rather than by the author;
* **the three states** — `NULL` is every run recorded before the column, and every
  query means `IS NOT FALSE` rather than `IS TRUE`, so nothing already published
  moves.
"""

from __future__ import annotations

import re

import pytest

from .conftest import LAPTOP

AGENT = {**LAPTOP, "X-Agent-Instance": "cc94dd"}


@pytest.fixture
def repo(request) -> str:
    """A repo unique to this test — the schema is per session, so runs recorded
    by one test are rivals for the next and every count here is over a
    population."""
    return f"acme/{re.sub(r'[^a-zA-Z0-9_]', '-', request.node.name)}"


def files(*paths: str) -> list[dict]:
    return [{"path": p, "additions": 10, "deletions": 2} for p in paths]


def reviewed_payload(repo: str, pr: int, *paths: str, **over) -> dict:
    """What the panel sends when a panel actually ran."""
    return {"repo": repo, "pr": pr, "judged": True, "judge_model": "opus",
            "reviewed": True,
            "reviewers_selected": ["claude"],
            "reviewers": {"claude": {"model": "opus", "ran": True}},
            "changed_files": files(*paths), "changed_files_total": len(paths),
            "pr_state": "OPEN", "to_fix": [], "dismissed": [], "sonar_findings": [],
            **over}


def skipped_payload(repo: str, pr: int, *paths: str, **over) -> dict:
    """What `panel.py`'s title-pattern skip branch builds, verbatim: no reviewers,
    no findings, a complete file list, and `reviewed: false` with the reason."""
    return {"repo": repo, "pr": pr, "title": "Merge branch 'main' into feat/x",
            "reviewed": False,
            "skip_reason": "title matches skip pattern /^Merge /",
            "changed_files": files(*paths), "changed_files_total": len(paths),
            "pr_state": "OPEN", "round": 1,
            "to_fix": [], "dismissed": [], "sonar_findings": [], **over}


async def record(client, body: dict) -> dict:
    r = await client.post("/review", json=body, headers=AGENT)
    assert r.status_code == 201, r.text
    return r.json()


async def collisions(client, repo: str, pr: int, **params) -> dict:
    r = await client.get("/review/collisions",
                         params={"repo": repo, "pr": pr, **params}, headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


# ---- the blind spot --------------------------------------------------------

async def test_a_skipped_pr_is_a_rival_like_any_other(client, repo):
    """THE REGRESSION. A merge commit touching the same file as an open PR used
    to be invisible in both directions: `considered: 0`, every class empty.

    It is classified by the same ladder as anything else, because for this
    endpoint's question a skipped run is worth exactly as much — the file list
    came off the PR's own metadata, which the skip path fetches and the diff
    never enters into."""
    await record(client, reviewed_payload(
        repo, 1, "app/api/reviews.py", "app/models/review.py"))
    await record(client, skipped_payload(repo, 7, "app/api/reviews.py", "docs/x.md"))

    out = await collisions(client, repo, 1)
    assert out["counts"]["considered"] == 1
    assert [h["pr"] for h in out["collides"]] == [7]
    hit = out["collides"][0]
    assert hit["files"] == ["app/api/reviews.py"]
    assert hit["shared"] == 1
    # The row says nobody read it — a fact about the PR, not about the overlap.
    assert hit["reviewed"] is False
    assert "skip pattern" in hit["skip_reason"]


async def test_a_skipped_pr_can_be_the_subject_too(client, repo):
    """The other direction, and the one an unattended merge actually asks. It
    used to 404: no run, so no subject."""
    await record(client, reviewed_payload(repo, 1, "app/api/reviews.py"))
    await record(client, skipped_payload(repo, 7, "app/api/reviews.py", "docs/x.md"))

    out = await collisions(client, repo, 7)
    assert out["reviewed"] is False and "skip pattern" in out["skip_reason"]
    assert out["files"] == ["app/api/reviews.py", "docs/x.md"]
    assert [h["pr"] for h in out["collides"]] == [1]


async def test_the_collision_handler_never_tests_reviewed(client, repo):
    """The design assertion, not an outcome one. `reviewed` appears in that
    handler's SELECT list and in no WHERE clause anywhere, so a skipped run
    cannot be filtered out of the population — and, just as importantly, cannot
    be filtered out of the *newest run* selection, which is the defect this
    endpoint was rewritten twice to remove.

    A skipped round recorded AFTER a reviewed one therefore answers for its PR,
    with its own newer file list, exactly as any later round would."""
    await record(client, reviewed_payload(repo, 1, "app/x.py"))
    await record(client, reviewed_payload(repo, 7, "app/other.py"))
    # …then the same PR gets a merge commit that touches the subject's file.
    await record(client, skipped_payload(repo, 7, "app/x.py", round=2))

    out = await collisions(client, repo, 1)
    assert out["counts"]["considered"] == 1
    assert [h["pr"] for h in out["collides"]] == [7], (
        "the newest run answers for the PR whether or not it reviewed anything")
    assert out["collides"][0]["reviewed"] is False


# ---- the consumers, and the false all-clear this could have shipped ---------

async def test_a_skipped_round_does_not_turn_open_findings_into_gone(client, repo):
    """The one that would have been worse than the bug. A defect is `open` while
    its last sighting is the latest run and `gone` once it is not, so a skipped
    run recorded after a real round would have reported every outstanding finding
    on that PR as no longer being found — with nobody having re-reviewed
    anything."""
    await record(client, reviewed_payload(
        repo, 5, "app/x.py",
        to_fix=[{"title": "a real defect", "file": "app/x.py", "severity": "P1",
                 "key": "a" * 12}]))
    await record(client, skipped_payload(repo, 5, "app/x.py", round=2))

    got = (await client.get("/review/findings",
                            params={"repo": repo, "pr": 5}, headers=AGENT)).json()
    statuses = {f["key"]: f["status"] for f in got["findings"]}
    assert statuses == {"a" * 12: "open"}


async def test_a_skipped_round_does_not_spend_the_findings_window(client, repo):
    """The same defect one layer out, and the one an after-the-fact filter would
    have missed. `limit` bounds what is FETCHED, so a PR collecting skipped merges
    would spend its window on rounds with no findings in them and the real rounds
    would fall off the end — their defects not `gone` but absent, which reads
    emptier still. `limit=1` after one skipped merge returned nothing at all."""
    await record(client, reviewed_payload(
        repo, 5, "app/x.py",
        to_fix=[{"title": "still open", "file": "app/x.py", "severity": "P1",
                 "key": "c" * 12}]))
    await record(client, skipped_payload(repo, 5, "app/x.py", round=2))

    got = (await client.get("/review/findings",
                            params={"repo": repo, "pr": 5, "limit": 1},
                            headers=AGENT)).json()
    assert [f["status"] for f in got["findings"]] == ["open"]
    assert got["rounds"] == 1, "a round that reviewed nothing is not a round here"


async def test_a_skipped_round_does_not_erase_a_cycles_ending(client, repo):
    """A skipped round inherits its cycle id from the baseline, so it qualifies as
    "the newest run of this cycle" and would have supplied the cycle's ending from
    stop fields no stopping rule ever set — reporting a converged cycle as one
    nobody ever ruled on. Same family as the `gone` bug: the record going quiet
    about a judgement that was actually made."""
    await record(client, reviewed_payload(
        repo, 5, "app/x.py", cycle="cyc-1",
        round_stop={"stop": True, "confident": True, "reason": "dry round"}))
    await record(client, skipped_payload(repo, 5, "app/x.py", round=2, cycle="cyc-1"))

    got = (await client.get("/review/findings",
                            params={"repo": repo, "pr": 5}, headers=AGENT)).json()
    assert got["stopped"] is True
    assert got["stop_confident"] is True
    assert got["stop_reason"] == "dry round"


async def test_a_skipped_round_is_not_a_round_the_review_queue_counts(client, repo):
    """`_cycle_rounds` counts a cap off the newest run's round number, so a merge
    commit landing on a PR mid-cycle could reach `max_rounds` and hold the review
    it is asking for. The queue answers off the newest run that reviewed."""
    await record(client, reviewed_payload(repo, 5, "app/x.py", round=1,
                                          head_sha="a" * 40))
    await record(client, skipped_payload(repo, 5, "app/x.py", round=2,
                                         head_sha="b" * 40))

    r = await client.post("/review-queue", headers=AGENT, json={
        "repo": repo, "max_rounds": 1,
        "prs": [{"number": 5, "headRefOid": "b" * 40, "mergeable": "MERGEABLE",
                 "title": "a real pr", "isDraft": False}]})
    assert r.status_code == 200, r.text
    entry = r.json()["entries"][0]
    assert entry["rounds"] == 1, "the skipped round is not a round of the cycle"
    assert "round-cap" not in [h["code"] for h in entry["holds"]]
    # And the PR is correctly stale: the last REVIEW was at an older commit.
    assert entry["last_run"]["head_sha"] == "a" * 40


async def test_a_skipped_run_is_not_counted_as_a_review(client, repo):
    """The over-count the issue was filed about. It contributes no scorecard and
    no finding either, so every per-reviewer number is untouched by construction
    rather than by a filter."""
    await record(client, reviewed_payload(repo, 1, "app/x.py"))
    await record(client, skipped_payload(repo, 7, "app/y.py"))

    stats = (await client.get("/review/stats", params={"repo": repo},
                              headers=AGENT)).json()
    assert stats["runs"] == 1
    assert stats["prs"] == 1

    spend = (await client.get("/review/spend", params={"repo": repo},
                              headers=AGENT)).json()
    assert spend["repo_window"]["runs"] == 1

    listed = (await client.get("/reviews", params={"repo": repo},
                               headers=AGENT)).json()
    assert [r["pr"] for r in listed] == [1]
    both = (await client.get("/reviews",
                             params={"repo": repo, "include_unreviewed": True},
                             headers=AGENT)).json()
    assert sorted(r["pr"] for r in both) == [1, 7]
    assert [r["skip_reason"] for r in both if r["pr"] == 7] == [
        "title matches skip pattern /^Merge /"]


# ---- the three states ------------------------------------------------------

async def test_a_payload_that_says_nothing_records_null_not_true(client, repo):
    """The value every run recorded before this column carries, and the reason
    the column is nullable. NULL is "nobody said" — not "reviewed", which would
    have made a brand-new column knowingly wrong about the pre-flight refusals
    already on the board.

    A NULL run counts, exactly as it has always counted: `IS NOT FALSE` is what
    every aggregate asks, so publishing this moved no number."""
    body = reviewed_payload(repo, 1, "app/x.py")
    del body["reviewed"]
    got = await record(client, body)

    run = (await client.get(f"/review/{got['id']}", headers=AGENT)).json()
    assert run["reviewed"] is None and run["skip_reason"] is None

    stats = (await client.get("/review/stats", params={"repo": repo},
                              headers=AGENT)).json()
    assert stats["runs"] == 1, "a legacy-shaped run still counts as a run"
    listed = (await client.get("/reviews", params={"repo": repo},
                               headers=AGENT)).json()
    assert [r["pr"] for r in listed] == [1]


async def test_findings_beside_reviewed_false_drop_the_flag_not_the_findings(
        client, repo):
    """A payload cannot claim both. The findings are concrete — reporters, a
    file, a judge's verdict — and the flag is one boolean, so the flag is what
    goes; and it goes to NULL rather than being corrected to True, because this
    board did not watch the run.

    Never a 422: refusing the payload would lose the findings, the scorecards and
    the accounts along with the bad value."""
    got = await record(client, skipped_payload(
        repo, 7, "app/x.py",
        to_fix=[{"title": "found something", "file": "app/x.py", "severity": "P2",
                 "key": "b" * 12}]))
    assert got["reviewed_dropped"] == "findings were sent with reviewed: false"

    run = (await client.get(f"/review/{got['id']}", headers=AGENT)).json()
    assert run["reviewed"] is None
    assert [f["title"] for f in run["findings"]] == ["found something"]


async def test_a_confident_stop_beside_reviewed_false_loses_the_confidence(
        client, repo):
    """The opposite half of the same rule, and the asymmetry is deliberate: here
    the flag stays and the confidence goes, because `stop_confident` is what
    `preland --require-earned-stop` reads and what the queue calls convergence.
    A run that read nothing certifying a PR as done is the one combination that
    must be unrepresentable — `ck_review_runs_unreviewed_not_confident` refuses
    it at the boundary too, so without this the row would 500 instead of being
    recorded with a note."""
    got = await record(client, skipped_payload(
        repo, 7, "app/x.py",
        round_stop={"stop": True, "confident": True, "reason": "nothing to do"}))
    assert got["stop_confidence_dropped"] == (
        "a confident stop was sent with reviewed: false")

    run = (await client.get(f"/review/{got['id']}", headers=AGENT)).json()
    assert run["reviewed"] is False
    assert run["stopped"] is True and run["stop_confident"] is False
