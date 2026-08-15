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


async def test_a_rival_is_represented_by_its_newest_run_outright(client):
    """A PR's file set grows while it is open, so the newest round is the current
    answer — and a later round that recorded NO list makes the PR unanswerable
    rather than handing back the older round's paths.

    This test asserted the opposite until round 1 of #88's panel caught it (F06).
    Preferring the newest *file-bearing* run answers a stale question in a
    confident voice: the PR has been panelled since, that round said nothing
    about files, and quoting the earlier paths hides exactly that. The CHANGELOG
    promised "a PR whose newest run has no list is neither disjoint nor colliding
    but unanswered" while the code and this test both did something else."""
    await record(client, 8230, changed_files=files("target.py"), changed_files_total=1)
    await record(client, 8231, round=1, changed_files=files("nothing-shared.py"),
                 changed_files_total=1)
    await record(client, 8231, round=2, changed_files=files("target.py", "more.py"),
                 changed_files_total=2)
    c = await collisions(client, 8230)
    assert [(h["pr"], h["files"]) for h in c["collides"]] == [(8231, ["target.py"])]

    # A later round that recorded nothing moves the rival to `unknown` — it does
    # NOT leave round 2's answer standing.
    await record(client, 8231, round=3)
    c = await collisions(client, 8230)
    assert [h["pr"] for h in c["collides"]] == []
    assert 8231 in [u["pr"] for u in c["unknown"]]


async def test_a_pr_with_no_file_list_is_unknown_and_never_disjoint(client):
    """The half that matters most. An unanswered PR reported silently absent
    makes an empty `collides` read as "safe to land" — a shortfall presenting as
    a clean result, which is the failure this codebase keeps finding in itself."""
    await record(client, 8240, changed_files=files("shared.py"), changed_files_total=1)
    await record(client, 8241)  # a pre-v2.23 run: no list at all
    c = await collisions(client, 8240)
    assert c["collides"] == []
    assert 8241 in [u["pr"] for u in c["unknown"]]


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
    assert c["collides"] == []
    assert 8280 not in [u["pr"] for u in c["unknown"]]


# ---- what round 1 of #88's own panel found missing (F28) --------------------
#
# Every test below corresponds to a behaviour the panel ruled on. The suite as
# first written locked in the wrong answer for three of them, which is why they
# are grouped and labelled rather than scattered: the point is not that the cases
# exist, it is that a reviewer had to find them.

async def test_a_known_empty_file_list_is_answered_not_unknown(client):
    """F04. A PR that genuinely changed zero files is *knowledge*: it is disjoint
    from everything. Keying "has a file list" on the presence of child rows made
    it indistinguishable from a run that recorded nothing — 404 as a subject,
    `unknown` as a rival — which is the release's own three-ways distinction
    broken by the code enforcing it."""
    r = await record(client, 8300, changed_files=[], changed_files_total=0)
    assert r["recorded"] is True
    c = await collisions(client, 8300)
    assert c["files"] == [] and c["changed_files_total"] == 0
    assert c["collides"] == []


async def test_a_known_empty_rival_is_disjoint_rather_than_unanswered(client):
    """F04, the rival half — it must be absent from BOTH lists, not `unknown`."""
    await record(client, 8301, changed_files=files("e/x.py"), changed_files_total=1)
    await record(client, 8302, changed_files=[], changed_files_total=0)
    c = await collisions(client, 8301)
    assert 8302 not in [h["pr"] for h in c["collides"]]
    assert 8302 not in [u["pr"] for u in c["unknown"]]


async def test_a_rivals_truncation_is_visible_so_disjoint_can_be_doubted(client):
    """F08. A rival holding 2 of 2,500 files can share paths it never reported,
    so a caller must be able to see that its "no overlap" came from a prefix.
    The subject's own truncation was surfaced from the start; the rivals' was
    not, which is the same shortfall-as-clean-result one level out."""
    await record(client, 8310, changed_files=files("p/one.py"), changed_files_total=1)
    await record(client, 8311, changed_files=files("p/one.py", "p/two.py"),
                 changed_files_total=2500)
    [hit] = (await collisions(client, 8310))["collides"]
    assert hit["pr"] == 8311
    assert hit["changed_files_total"] == 2500
    assert len(hit["files"]) == 1  # a floor on the overlap, not the whole of it


async def test_a_merged_rival_is_not_a_live_collision(client):
    """F02. `review_runs` held no PR state at all, so every PR panelled inside the
    window read as a live rival — merged ones included. On a repo landing several
    a week that is most of the list, which is how an advisory endpoint stops being
    read."""
    await record(client, 8320, changed_files=files("m/shared.py"),
                 changed_files_total=1, pr_state="OPEN")
    await record(client, 8321, changed_files=files("m/shared.py"),
                 changed_files_total=1, pr_state="MERGED")
    assert (await collisions(client, 8320))["collides"] == []
    # ...and it is reachable when explicitly asked for, rather than unreachable.
    both = await collisions(client, 8320, include_closed=True)
    assert [h["pr"] for h in both["collides"]] == [8321]
    assert both["collides"][0]["pr_state"] == "MERGED"


async def test_a_rival_with_no_state_recorded_is_kept_not_filtered_away(client):
    """The NULL case, which decides whether this filter is safe to add at all.
    Every pre-v2.23 run states no PR state; dropping those would silently narrow
    the answer to PRs panelled since this release — a filter quietly becoming a
    cutoff."""
    await record(client, 8330, changed_files=files("s/shared.py"),
                 changed_files_total=1, pr_state="OPEN")
    await record(client, 8331, changed_files=files("s/shared.py"), changed_files_total=1)
    hits = (await collisions(client, 8330))["collides"]
    assert [h["pr"] for h in hits] == [8331]
    assert hits[0]["pr_state"] is None


async def test_pr_state_is_recorded_case_insensitively(client):
    run = await record(client, 8335, changed_files=files("c/x.py"),
                       changed_files_total=1, pr_state="open", is_draft=True)
    d = await detail(client, run["id"])
    assert d["pr_state"] == "OPEN" and d["is_draft"] is True


async def test_unknown_carries_enough_to_judge_why_it_is_unknown(client):
    """F09. `unknown` is argued to be the half that matters most, and it was a
    bare list of ints — a caller could not tell a month-old harness from an
    hour-old failure, nor render a title beside the number."""
    await record(client, 8340, changed_files=files("u/x.py"), changed_files_total=1)
    await record(client, 8341, pr_title="a PR nobody recorded files for")
    [u] = [u for u in (await collisions(client, 8340))["unknown"] if u["pr"] == 8341]
    assert u["pr_title"] == "a PR nobody recorded files for"
    assert u["run_id"] and u["ts"]


async def test_the_subject_falls_back_past_a_later_run_with_no_list(client):
    """F29. The subject's documented fallback had no test at all — and it is
    deliberately the one place where an older run may answer, because a caller
    naming a PR is asking about THAT PR. The asymmetry against rivals is the
    design, so it needs a test saying so."""
    first = await record(client, 8350, changed_files=files("f/x.py"), changed_files_total=1)
    await record(client, 8350, round=2)  # a later round that recorded nothing
    c = await collisions(client, 8350)
    assert c["run_id"] == first["id"] and c["files"] == ["f/x.py"]


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


async def test_collides_is_limited_and_says_how_much_it_dropped(client):
    """F12. Every sibling endpoint in this module takes a limit. And this repo's
    standing habit is that a cap which trims an answer announces itself — a
    silently trimmed list reads as the whole one."""
    await record(client, 8390, changed_files=files("l/hot.py"), changed_files_total=1)
    for pr in (8391, 8392, 8393):
        await record(client, pr, changed_files=files("l/hot.py"), changed_files_total=1)
    c = await collisions(client, 8390, limit=2)
    assert len(c["collides"]) == 2
    assert c["collides_dropped"] == 1


async def test_the_response_says_which_population_it_speaks_for(client):
    """F01, resolved by narrowing the claim rather than by pretending. A PR this
    board never panelled cannot appear in any list here, and that limit is not
    discoverable from the numbers — so it is stated in the response, not only in
    the docs."""
    await record(client, 8395, changed_files=files("sc/x.py"), changed_files_total=1)
    assert (await collisions(client, 8395))["scope"] == \
        "PRs this board has panelled within the window"
