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
* **``/review/collisions`` describes the overlap and does not rank it.** Ordering
  PRs by collision is #80's job and needs a policy about what a collision COSTS.
  What was missing was the datum.
"""

from __future__ import annotations

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


async def collisions(client, pr: int, **q) -> dict:
    r = await client.get("/review/collisions",
                         params={"repo": REPO, "pr": pr, **q}, headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


def files(*paths: str) -> list[dict]:
    return [{"path": p, "additions": 10, "deletions": 2} for p in paths]


# ---- the round trip --------------------------------------------------------

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


# ---- the count is not derived from the list --------------------------------

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


# ---- the collision query ---------------------------------------------------

async def test_a_pr_finds_the_other_prs_that_touch_its_files(client):
    await record(client, 8210, changed_files=files("panel.py", "reviews.py"),
                 changed_files_total=2)
    await record(client, 8211, changed_files=files("panel.py", "epic.py"),
                 changed_files_total=2)
    await record(client, 8212, changed_files=files("lander.py"),
                 changed_files_total=1)
    c = await collisions(client, 8210)
    assert [h["pr"] for h in c["collides"]] == [8211]
    assert c["collides"][0]["files"] == ["panel.py"]
    assert 8212 not in [h["pr"] for h in c["collides"]]


async def test_more_shared_files_sorts_first(client):
    """A description of the overlap, in the order a reader wants to see it. Not a
    recommendation about which to land — that is #80's, and it needs a policy
    about what a collision costs that this endpoint does not have."""
    await record(client, 8220, changed_files=files("o/a.py", "o/b.py", "o/c.py"),
                 changed_files_total=3)
    await record(client, 8221, changed_files=files("o/a.py"), changed_files_total=1)
    await record(client, 8222, changed_files=files("o/a.py", "o/b.py"),
                 changed_files_total=2)
    c = await collisions(client, 8220)
    assert [(h["pr"], h["files"]) for h in c["collides"]] == [
        (8222, ["o/a.py", "o/b.py"]), (8221, ["o/a.py"])]


async def test_a_pr_is_represented_by_its_newest_run_that_recorded_files(client):
    """A PR's file set grows while it is open. Round 2 is the current answer, and
    a round that recorded no list at all must not erase the round that did."""
    await record(client, 8230, changed_files=files("target.py"), changed_files_total=1)
    await record(client, 8231, round=1, changed_files=files("nothing-shared.py"),
                 changed_files_total=1)
    await record(client, 8231, round=2, changed_files=files("target.py", "more.py"),
                 changed_files_total=2)
    c = await collisions(client, 8230)
    assert [(h["pr"], h["files"]) for h in c["collides"]] == [(8231, ["target.py"])]

    # And the round that said nothing does not un-say round 2.
    await record(client, 8231, round=3)
    c = await collisions(client, 8230)
    assert [h["pr"] for h in c["collides"]] == [8231]


async def test_a_pr_with_no_file_list_is_unknown_and_never_disjoint(client):
    """The half that matters most. An unanswered PR reported silently absent
    makes an empty `collides` read as "safe to land" — a shortfall presenting as
    a clean result, which is the failure this codebase keeps finding in itself."""
    await record(client, 8240, changed_files=files("shared.py"), changed_files_total=1)
    await record(client, 8241)  # a pre-v2.23 run: no list at all
    c = await collisions(client, 8240)
    assert c["collides"] == []
    assert 8241 in c["unknown"]


async def test_the_subject_reports_the_run_it_answered_from(client):
    """So a caller can see how stale the answer is. The board is told about
    panels, not about pushes — a PR's files are as current as its last round."""
    run = await record(client, 8250, changed_files=files("x.py"), changed_files_total=1)
    c = await collisions(client, 8250)
    assert c["run_id"] == run["id"]
    assert c["ts"] and c["files"] == ["x.py"]
    assert c["changed_files_total"] == 1


async def test_a_pr_that_never_recorded_a_file_list_is_a_404_not_an_empty_answer(client):
    """"Nothing to compare" and "compared, found nothing" are different facts,
    and returning the second for the first is how a caller lands a colliding PR
    believing the board cleared it."""
    await record(client, 8260)
    r = await client.get("/review/collisions", params={"repo": REPO, "pr": 8260},
                         headers=AGENT)
    assert r.status_code == 404
    assert "changed-file list" in r.json()["detail"]


async def test_the_window_bounds_which_rival_prs_answer(client):
    """A repo accumulates PRs forever, and a collision with one merged months ago
    is not a fact about landing this one."""
    await record(client, 8270, changed_files=files("hot.py"), changed_files_total=1)
    await record(client, 8271, changed_files=files("hot.py"), changed_files_total=1)
    assert [h["pr"] for h in (await collisions(client, 8270, days=30))["collides"]] == [8271]
    far = await collisions(client, 8270, since="2099-01-01T00:00:00Z")
    assert far["collides"] == [] and far["unknown"] == []


async def test_a_pr_never_collides_with_itself(client):
    await record(client, 8280, changed_files=files("solo.py"), changed_files_total=1)
    await record(client, 8280, round=2, changed_files=files("solo.py"),
                 changed_files_total=1)
    c = await collisions(client, 8280)
    assert c["collides"] == [] and 8280 not in c["unknown"]
