"""#568: a decayed claim still knows which worktree the work is in — now readable.

The mechanism was already built and nothing could read it. `create-worktree`
writes `--note "worktree $branch on $host"` on **every** claim it takes, which is
the standard pickup path rather than a convention somebody follows, so a claim on
issue N carries the tree that was made for issue N. `_sweep_lapsed` retires that
row with `lapsed=True` instead of deleting it, and says in its own words why the
flag exists: "the holder let go" and "the holder vanished" are different facts.
Then every query on this table filtered `released_at IS NULL`, so a decayed claim
was invisible to every consumer and the `lapsed` column was written and never
read.

So the properties under test are the ones that make a *lookup* out of that:

* **Lapsed is not released, and only lapsed is worth reading.** A released claim
  means the holder finished; pointing the next agent at merged work is noise. A
  lapsed one means they stopped answering and their tree is still on a disk.
* **The `lapsed` column alone is not the population.** The sweep is passive — it
  runs only when somebody asks for that exact key — so a claim nobody ever
  re-claimed sits past its expiry with `lapsed=false` for ever. Measured on the
  live board the day this shipped: 8 rows carried the flag, 73 more were past
  `expires_at` with `released_at` still NULL, and one of the 73 was #196's, the
  case the feature exists for. A lookup that read the flag would answer "nothing"
  about its own headline example.
* **The default listing does not move.** "What is claimed right now" has a
  correct answer and many callers; the residue is a sibling endpoint.
* **It redirects rather than warns**, at the moment of pickup, on both pickup
  paths — `POST /claim` (which is `create-worktree`) and `POST /plan/item/claim`
  (which is `get-involved`).
* **It never refuses.** The claim is taken; the redirect rides along with it.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from .conftest import DESKTOP, LAPTOP


@pytest.fixture
async def repo():
    """A repo nothing else has claimed in, whose plan rows are cleaned up after.

    The cleanup is not tidiness. A claim on an issue also writes a plan item
    (#427), `GET /plan` answers with a **page** of the fleet's list, and
    `qbdata.PLAN_LIMIT` asks for 200 — so a suite that leaves forty rows behind
    pushes somebody else's row off the end of that page. It did exactly that on
    the first CI run of this branch: `test_plans.py`'s co-tenant dashboard test
    looked for its own item, did not find it, and `next()` raised StopIteration
    inside an async test, which surfaces as `coroutine raised StopIteration`. The
    schema is built once per session and these tests take about forty claims, so
    the rows they write outlive them unless this removes them.
    """
    from sqlalchemy import delete

    from app.db import async_session
    from app.models.plan_item import PlanItem

    name = f"acme/lapsed-{uuid.uuid4().hex[:10]}"
    yield name
    async with async_session() as s:
        await s.execute(delete(PlanItem).where(PlanItem.repo == name))
        await s.commit()


async def take(client, repo: str, number: int, headers=LAPTOP, **over) -> dict:
    r = await client.post("/claim", headers=headers, json={
        "ref": {"kind": "issue", "repo": repo, "value": str(number)}, **over})
    assert r.status_code == 200, r.text
    return r.json()


async def lapsed(client, repo: str, number: int | None = None, headers=LAPTOP,
                 **params) -> dict:
    if number is not None:
        params |= {"ref_kind": "issue", "ref_value": str(number), "repo": repo}
    r = await client.get("/claims/lapsed", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def vanish(client, repo: str, number: int, **over) -> dict:
    """A claim whose holder stopped answering: taken with a 1s TTL and left alone.

    Left alone is the point — nothing sweeps it, because nothing asks for the key
    again. That is the shape 73 of the 75 unreleased rows on the live board were
    in, and the shape a lookup on the `lapsed` column cannot see.
    """
    claim = await take(client, repo, number, ttl=1, **over)
    await asyncio.sleep(1.1)
    return claim


# ------------------------------------------------- lapsed, released, and neither

async def test_a_claim_whose_holder_vanished_is_returned(client, repo):
    await vanish(client, repo, 196, note="worktree feat/qb-dash-buttons on zeus")
    rows = (await lapsed(client, repo, 196))["claims"]
    assert len(rows) == 1
    assert rows[0]["key"] == f"{repo}#196"
    assert rows[0]["worktree"] == {"branch": "feat/qb-dash-buttons", "host": "zeus"}


async def test_a_claim_the_holder_RELEASED_is_not_returned(client, repo):
    """The distinction the whole design rests on. They said they were done: the
    work landed, the branch merged, and redirecting a new agent there is noise."""
    claim = await take(client, repo, 197, note="worktree feat/issue-197 on zeus")
    r = await client.post("/claim/release", headers=LAPTOP,
                          json={"claim_id": claim["claim_id"]})
    assert r.status_code == 200, r.text
    assert (await lapsed(client, repo, 197))["claims"] == []


async def test_a_live_claim_is_not_returned_either(client, repo):
    """It is not history and nobody vanished — `GET /claims` is that question."""
    await take(client, repo, 198)
    assert (await lapsed(client, repo, 198))["claims"] == []


async def test_a_key_with_no_history_answers_empty_rather_than_erroring(client, repo):
    """The common case at pickup, and it has to be cheap and quiet."""
    assert (await lapsed(client, repo, 999))["claims"] == []


async def test_an_unswept_row_counts_because_the_sweep_is_PASSIVE(client, repo):
    """The finding that decides the predicate.

    `_sweep_lapsed` runs only when somebody asks for that exact key. Nothing has
    asked here, so the row still reads `released_at IS NULL, lapsed=false` — and
    it is nonetheless a claim whose holder stopped answering hours ago. A lookup
    on the column would report nothing; the row says `swept: false` instead, which
    is a fact about this board rather than about the holder.
    """
    await vanish(client, repo, 200, note="worktree feat/issue-200 on hermes")
    row = (await lapsed(client, repo, 200))["claims"][0]
    assert row["swept"] is False, "nothing has asked for this key since it expired"
    assert row["released"] is None
    assert row["stopped_answering"] == row["expires"]


async def test_a_swept_row_counts_too_and_says_it_was_swept(client, repo):
    """The other half of the same population. Asking for the key again sweeps it,
    and `released_at` then records when somebody *next asked* — which can be days
    later and says nothing about the work, so `stopped_answering` is the expiry."""
    await vanish(client, repo, 201, note="worktree feat/issue-201 on zeus")
    await take(client, repo, 201, headers=DESKTOP)     # this is what sweeps it
    row = next(r for r in (await lapsed(client, repo, 201))["claims"])
    assert row["swept"] is True
    assert row["released"] is not None
    assert row["stopped_answering"] == row["expires"] < row["released"]


# ------------------------------------------------------- what a redirect says

async def test_the_redirect_names_the_branch_the_host_and_the_date(client, repo):
    """Not "possible duplicate" — a place to go and look. That is the difference
    between a warning somebody dismisses and an instruction they can act on."""
    await vanish(client, repo, 196, note="worktree feat/qb-dash-buttons on zeus")
    row = (await lapsed(client, repo, 196))["claims"][0]
    assert "feat/qb-dash-buttons" in row["redirect"]
    assert "zeus" in row["redirect"]
    assert row["acquired"][:10] in row["redirect"]
    assert "not a refusal" in row["redirect"], "advisory, and it has to say so"


async def test_a_claim_with_no_worktree_in_its_note_reports_the_note(client, repo):
    """#196's real claim on the live board. Its note is the plan's sentence, not
    `create-worktree`'s — so the branch is not recorded anywhere on the row, and
    the honest answer is what the holder actually said they were doing. The
    branch/commit search is the client's fallback from here, not the board's."""
    await vanish(client, repo, 202, note="plan: qb-dash OPEN PRs, panel review state")
    row = (await lapsed(client, repo, 202))["claims"][0]
    assert row["worktree"] is None
    assert "recorded no worktree" in row["redirect"]
    assert "qb-dash OPEN PRs" in row["redirect"]


async def test_a_holder_with_no_session_is_not_somebody_to_talk_to(client, repo):
    """#156's territory, said out loud rather than implied. `create-worktree`
    takes its claim before the agent that will use the tree exists, so the row
    names a machine; "go and ask the holder" would resolve to a whole box."""
    await vanish(client, repo, 203, note="worktree feat/issue-203 on hermes")
    row = (await lapsed(client, repo, 203))["claims"][0]
    assert row["session"] is None
    assert "not an agent you can address" in row["redirect"]


async def test_the_worktree_is_reported_as_RECORDED_not_as_observed(client, repo):
    """`prune-worktrees` and `/drop-worktree` delete trees, and the board makes no
    outbound calls — so asserting a live path it cannot see would be worse than
    saying nothing. It reports what was written down and says that is what it is."""
    await vanish(client, repo, 204, note="worktree feat/issue-204 on zeus")
    answer = await lapsed(client, repo, 204)
    assert "RECORDED" in answer["note_on_worktree"]
    assert "cannot see another machine" in answer["note_on_worktree"]


# ------------------------------------------- the default listing does not move

async def test_GET_claims_still_answers_only_about_live_claims(client, repo):
    """The reason this is a sibling endpoint. Every caller of `GET /claims` — the
    pickup gate, the dashboards, the in-flight count — is right to be told about
    live claims only, and widening that listing for one new consumer would change
    what all of them see."""
    await vanish(client, repo, 205, note="worktree feat/issue-205 on zeus")
    r = await client.get("/claims", params={"key": f"{repo}#205", "kind": "work"},
                         headers=LAPTOP)
    assert r.status_code == 200
    assert r.json()["claims"] == []
    both = await client.get("/claims", headers=LAPTOP, params={
        "key": f"{repo}#205", "kind": "work", "include_released": "true"})
    assert len(both.json()["claims"]) == 1, "history is still where it always was"


async def test_the_two_endpoints_derive_the_same_key_from_the_same_ref(client, repo):
    """One `_read_filter` for both, so a lookup cannot miss a row by spelling
    (#172). Asked by composed pair and by ref, the answer is the same row."""
    await vanish(client, repo, 206, note="worktree feat/issue-206 on zeus")
    by_ref = (await lapsed(client, repo, 206))["claims"]
    by_pair = (await lapsed(client, repo, kind="issue", key=f"{repo}#206"))["claims"]
    assert [c["claim_id"] for c in by_ref] == [c["claim_id"] for c in by_pair]


async def test_asking_by_ref_AND_by_pair_is_refused_rather_than_guessed_at(client, repo):
    r = await client.get("/claims/lapsed", headers=LAPTOP, params={
        "ref_kind": "issue", "ref_value": "1", "repo": repo, "kind": "work"})
    assert r.status_code == 422


# ------------------------------------------------------- the pickup, both paths

async def test_taking_a_key_somebody_abandoned_answers_previously(client, repo):
    """`create-worktree`'s path. The claim is composed by the tool that is about
    to start the work, so nothing has to be searched for and no client has to
    remember to ask."""
    await vanish(client, repo, 207, note="worktree feat/issue-207 on zeus")
    taken = await take(client, repo, 207, headers=DESKTOP)
    assert taken["claimed"] is True, "it redirects; it does not refuse"
    prior = taken["previously"]
    assert prior["worktree"] == {"branch": "feat/issue-207", "host": "zeus"}
    assert "feat/issue-207" in prior["redirect"]


async def test_previously_is_absent_when_the_predecessor_released(client, repo):
    """The distinction, at the moment it costs something. A finished predecessor
    must not send the next agent to read a merged branch."""
    claim = await take(client, repo, 208, note="worktree feat/issue-208 on zeus")
    await client.post("/claim/release", headers=LAPTOP,
                      json={"claim_id": claim["claim_id"]})
    assert "previously" not in await take(client, repo, 208, headers=DESKTOP)


async def test_previously_is_absent_on_a_renew(client, repo):
    """A renew is the same worker carrying on, and it saw this on the way in.
    Repeating it on every heartbeat is how an advisory becomes noise people
    filter out."""
    await vanish(client, repo, 209, note="worktree feat/issue-209 on zeus")
    first = await take(client, repo, 209, headers=DESKTOP, session="s-1")
    assert "previously" in first
    again = await take(client, repo, 209, headers=DESKTOP, session="s-1")
    assert again["renewed"] is True
    assert "previously" not in again


async def test_a_fresh_key_carries_no_previously_at_all(client, repo):
    """The narrowness that makes it worth firing on every pickup: an exact key
    lookup can only speak when somebody genuinely was here before."""
    assert "previously" not in await take(client, repo, 210)


async def test_claiming_the_PLAN_ITEM_redirects_too(client, repo):
    """`get-involved`'s path, which does not go through `POST /claim`. It writes
    the same row on the same key, so the redirect hangs off the claim both take —
    otherwise the plan route silently loses it."""
    first = await vanish(client, repo, 211, note="worktree feat/issue-211 on hermes")
    item_id = first["plan_item"]["item_id"]
    r = await client.post("/plan/item/claim", headers=DESKTOP,
                          json={"item_id": item_id, "session": "s-getinvolved"})
    assert r.status_code == 200, r.text
    taken = r.json()
    assert taken["claimed"] is True
    assert taken["previously"]["worktree"] == {"branch": "feat/issue-211",
                                               "host": "hermes"}


async def test_the_older_lapses_are_counted_not_listed(client, repo):
    """One redirect, because the most recent abandoned claim is the one whose tree
    is still on a disk. The rest are a number and an endpoint to ask."""
    await vanish(client, repo, 212, note="worktree feat/issue-212 on zeus")
    await vanish(client, repo, 212, headers=DESKTOP,
                 note="worktree feat/issue-212-again on hermes")
    taken = await take(client, repo, 212, headers=LAPTOP)
    assert taken["previously"]["worktree"]["host"] == "hermes", "the most recent"
    assert taken["previously"]["also_lapsed"] == 1


# ---------------------------------------- what the CLIENT adds: is any of it here?
#
# The board says what was recorded. Only the box the tree was recorded on can say
# whether it is still there, and `qbdata.lapsed_redirect` is the half that asks —
# shared by `qb-claim` (so `create-worktree`) and `qb-next` (so `get-involved`), so
# the two pickup paths cannot describe one row differently. The could-not-check
# case is kept apart from the nothing-to-report case, as `qb-doctor` keeps them
# apart around its edge probe: a tree on another machine is not a tree that is
# gone, and conflating them would send somebody to redo work sitting on hermes.


def _qbdata():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "harness" / "bin"))
    import qbdata as qd
    return qd


def _repo_with(tmp_path, commits: list[str], branch: str = "feat/qb-dash-buttons"):
    """A checkout with commits on a branch and no remote — so nothing is pushed."""
    import subprocess
    root = tmp_path / "checkout"
    root.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True,  # noqa: E731
                                    capture_output=True, text=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True,
                   capture_output=True)
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (root / "f").write_text("x")
    run("add", "f")
    run("commit", "-qm", "base")
    run("checkout", "-qb", branch)
    for i, subject in enumerate(commits):
        (root / "f").write_text(f"x{i}")
        run("commit", "-qam", subject)
    run("checkout", "-q", "main")
    return root


def test_a_tree_recorded_on_another_box_is_unknown_here_not_gone(tmp_path):
    qd = _qbdata()
    lines = "\n".join(qd.lapsed_redirect(
        {"redirect": "…", "worktree": {"branch": "feat/issue-563", "host": "hermes"}},
        str(tmp_path)))
    assert "hermes" in lines and qd.this_host() in lines
    assert "cannot" in lines, "could-not-check is not nothing-to-report"
    assert "gone" not in lines and "pruned" not in lines


def test_a_tree_that_was_pruned_still_points_at_the_branch_that_has_the_work(tmp_path):
    """The case the issue asks to decide. The tree is gone — `prune-worktrees` and
    `/drop-worktree` remove them — but the commits are on the branch, so the
    redirect survives the tree it named."""
    qd = _qbdata()
    root = _repo_with(tmp_path, ["feat: half of it", "feat: the other half"])
    lines = "\n".join(qd.lapsed_redirect(
        {"redirect": "…",
         "worktree": {"branch": "feat/qb-dash-buttons", "host": qd.this_host()}},
        str(root)))
    assert "worktree is gone" in lines
    # Three, not two: this checkout has no remote at all, so the base commit is
    # unpushed as well. The count is "on no remote", which is the honest question
    # — a branch nobody pushed is exactly the state the issue is about.
    assert "feat/qb-dash-buttons" in lines and "3 commit(s) on no remote" in lines


def test_a_pruned_tree_whose_branch_is_fully_pushed_says_so_instead(tmp_path):
    """Nothing is stranded there, and a count of zero unpushed commits reads as a
    number rather than as an answer. The redirect is still worth printing — the
    work exists — but it is no longer about work only this disk has."""
    import subprocess
    qd = _qbdata()
    root = _repo_with(tmp_path, ["feat: work"])
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True,
                   capture_output=True)
    for a in (["remote", "add", "origin", str(bare)],
              ["push", "-q", "origin", "feat/qb-dash-buttons"]):
        subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)
    lines = "\n".join(qd.lapsed_redirect(
        {"redirect": "…",
         "worktree": {"branch": "feat/qb-dash-buttons", "host": qd.this_host()}},
        str(root)))
    assert "everything on it is pushed" in lines
    assert "commit(s) on no remote" not in lines


def test_a_tree_that_is_still_here_is_reported_by_path(tmp_path):
    import subprocess
    qd = _qbdata()
    root = _repo_with(tmp_path, ["feat: work"])
    tree = tmp_path / "wt"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", str(tree),
                    "feat/qb-dash-buttons"], check=True, capture_output=True)
    lines = "\n".join(qd.lapsed_redirect(
        {"redirect": "…",
         "worktree": {"branch": "feat/qb-dash-buttons", "host": qd.this_host()}},
        str(root)))
    assert "still on this box" in lines and str(tree) in lines


def test_neither_tree_nor_branch_reads_as_landed_rather_than_as_missing(tmp_path):
    qd = _qbdata()
    root = _repo_with(tmp_path, ["feat: work"], branch="feat/something-else")
    lines = "\n".join(qd.lapsed_redirect(
        {"redirect": "…", "worktree": {"branch": "feat/deleted", "host": qd.this_host()}},
        str(root)))
    assert "neither" in lines and "landed" in lines


def test_the_fallback_finds_unpushed_commits_that_CITE_the_issue(tmp_path):
    """#196 exactly: a lapsed claim whose note names no worktree, and five
    commits on `feat/qb-dash-buttons` that nobody pushed. The search is the fuzzy
    path and it stays the fallback — it runs only because an exact claim already
    said somebody was here, so it can never become the check that fires on every
    issue."""
    qd = _qbdata()
    root = _repo_with(tmp_path, ["feat(qbdata): read panel review state, for #196",
                                 "feat(qb-dash): buttons"])
    lines = "\n".join(qd.lapsed_redirect(
        {"redirect": "…", "worktree": None, "key": "prisonblues/quarterback#196"},
        str(root)))
    assert "no worktree was recorded, but" in lines
    assert "for #196" in lines and "feat/qb-dash-buttons" in lines


def test_the_fallback_says_plainly_when_this_box_has_nothing(tmp_path):
    """"Nothing here" is an answer. It must not read like the search not running."""
    qd = _qbdata()
    root = _repo_with(tmp_path, ["feat: unrelated"])
    lines = "\n".join(qd.lapsed_redirect(
        {"redirect": "…", "worktree": None, "key": "prisonblues/quarterback#196"},
        str(root)))
    assert "no unpushed commit on this box cites #196" in lines


def test_no_previous_holder_prints_absolutely_nothing(tmp_path):
    """The common case, and the one that decides whether anybody reads the others."""
    assert _qbdata().lapsed_redirect(None, str(tmp_path)) == []


# ------------------------------------- boundaries the review asked about (codex)

async def test_a_holder_who_comes_back_LATE_and_releases_is_not_lapsed(client, repo):
    """The TTL only ever inferred that they had gone. An explicit release is the
    holder saying what happened — later, and better informed — so it wins, and a
    redirect nobody should read stops being printed."""
    claim = await vanish(client, repo, 213, note="worktree feat/issue-213 on zeus")
    assert (await lapsed(client, repo, 213))["claims"], "abandoned until they say"
    r = await client.post("/claim/release", headers=LAPTOP,
                          json={"claim_id": claim["claim_id"]})
    assert r.status_code == 200, r.text
    assert (await lapsed(client, repo, 213))["claims"] == []
    assert "previously" not in await take(client, repo, 213, headers=DESKTOP)


async def test_a_lookup_that_fails_costs_the_advice_and_not_the_claim(client, repo,
                                                                     monkeypatch):
    """The claim is committed before the redirect is looked up, so a read that
    raised would 500 a request whose write had already succeeded — and the caller
    would hold a claim whose id it never learnt. `_plan_item_for` makes this
    argument about the plan write; the same one applies to a read that cannot
    fail quietly."""
    import app.api.claims as mod

    async def boom(*a, **kw):
        raise RuntimeError("the claims table is on fire")

    monkeypatch.setattr(mod, "previous_lapse", boom)
    taken = await take(client, repo, 214, note="worktree feat/issue-214 on zeus")
    assert taken["claimed"] is True and taken["claim_id"]
    assert taken["previously"] is None
    assert "on fire" in taken["previously_error"], "said, not swallowed"


async def test_older_lapses_are_COUNTED_rather_than_measured_off_a_page(client, repo):
    """A scan window that fetched 25 rows and reported `len - 1` would answer
    "24 more" for ever once a key passed 25, which is a number that has stopped
    being a number. Seeded directly, because thirty real TTLs is thirty seconds."""
    from datetime import UTC, datetime, timedelta

    from app.db import async_session
    from app.models.resource_lease import ResourceLease

    now = datetime.now(UTC)
    async with async_session() as s:
        for i in range(30):
            s.add(ResourceLease(
                kind="work", key=f"{repo}#215", holder="zeus", session=None,
                note=f"worktree feat/issue-215-take{i} on zeus", ttl_seconds=60,
                acquired_at=now - timedelta(hours=40 - i),
                expires_at=now - timedelta(hours=39 - i),
                released_at=now - timedelta(hours=39 - i), lapsed=True))
        await s.commit()
    taken = await take(client, repo, 215, headers=DESKTOP)
    assert taken["previously"]["also_lapsed"] == 29
    assert taken["previously"]["worktree"]["branch"] == "feat/issue-215-take29", \
        "the most recent, which is the one whose tree may still be on a disk"
