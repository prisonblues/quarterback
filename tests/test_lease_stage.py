"""How far along the work is — the field a fleet view could not show (#262).

`repo`, `branch` and `title` read identically from the first cut to the third
review round, so a board holding only those cannot answer the one question you
have when you glance at eight panes. `stage` is the answer, and it is *said*
rather than derived: a round number is handed to `panel.py` as `--round <r>` and
never worked out, so nothing on the board could recover it.

Two things about this field can be wrong in ways nothing downstream recovers
from, and most of what is below is about them:

* **A stage nobody reported must not read as a stage.** NULL is the majority case
  — most sessions never call `qb-stage` at all — and a column that renders it the
  way it renders `F0` says something false about every one of those rows. That is
  the same class of lie as a panel filtering without saying so (#261), and it is
  checked here at the API and in `test_fleet_page.py` at the page.
* **A stale stage is worse than no stage.** A lease still advertising `R2` after
  the work landed sends a peer to a round that finished. Hence the clear path,
  and hence that clearing is a real report and not a no-op.

The vocabulary is deliberately open and the SHAPE is not, so the shape is pinned
here and pinned against `qb-stage`'s own copy of it — a producer that refuses
what the board accepts (or the reverse) puts the pane footer and the fleet view
into disagreement about the same session, which is the gap this closes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.api.leases import STAGE_RE

from .conftest import LAPTOP, SERVER

REPO_ROOT = Path(__file__).resolve().parent.parent
QB_STAGE = REPO_ROOT / "harness/bin/qb-stage"


async def _lease(client, session, device, headers, **kw):
    return await client.post(
        "/lease", json={"session": session, "device": device, **kw}, headers=headers
    )


async def _stage(client, session, stage, headers=SERVER):
    return await client.post(
        "/lease/stage", json={"session": session, "stage": stage}, headers=headers
    )


async def _agent(client, session, headers=SERVER, **params):
    agents = (await client.get("/active", params=params, headers=headers)).json()["agents"]
    return next(a for a in agents if a["session"] == session)


# ---- the fact travels -------------------------------------------------------


async def test_a_reported_stage_reaches_active(client):
    await _lease(client, "s-stage-1", "server", SERVER, cwd="/src/q")
    assert (await _stage(client, "s-stage-1", "R1F")).status_code == 200

    assert (await _agent(client, "s-stage-1", cwd="/src/q"))["stage"] == "R1F"


async def test_a_lease_that_never_reported_one_says_none_not_a_stage(client):
    """The majority case, and the one a renderer must not dress up.

    None is the whole distinction. A default of `F0` would assert that every
    session on the board is writing its first cut, which is exactly the
    confident-wrong answer this field exists to remove.
    """
    await _lease(client, "s-stage-2", "server", SERVER, cwd="/src/q")
    assert (await _agent(client, "s-stage-2", cwd="/src/q"))["stage"] is None


async def test_a_later_stage_replaces_the_earlier_one(client):
    """A session moves F0 -> R1 -> R1F; the board shows where it is, not a history."""
    await _lease(client, "s-stage-3", "server", SERVER, cwd="/src/q")
    for stage in ("F0", "R1", "R1F"):
        await _stage(client, "s-stage-3", stage)
    assert (await _agent(client, "s-stage-3", cwd="/src/q"))["stage"] == "R1F"


async def test_clearing_puts_the_lease_back_to_unreported(client):
    """`qb-stage --clear` and `/drop-worktree`: the work landed, the stage has not.

    A lease still advertising `R2` after the branch merged sends the next reader
    to a round that is over — worse than a blank cell, because it is confidently
    wrong rather than silent.
    """
    await _lease(client, "s-stage-4", "server", SERVER, cwd="/src/q")
    await _stage(client, "s-stage-4", "R2")
    assert (await _stage(client, "s-stage-4", None)).json()["changed"] is True
    assert (await _agent(client, "s-stage-4", cwd="/src/q"))["stage"] is None


async def test_a_heartbeat_does_not_disturb_the_stage(client):
    """The lease is renewed once per prompt and knows nothing about stages.

    Two reporters, two paths: if a plain renewal cleared this the field would
    survive for exactly one prompt, and if `POST /lease` carried it the caller
    would have to re-send on every beat a value it was never told.
    """
    await _lease(client, "s-stage-5", "server", SERVER, cwd="/src/q")
    await _stage(client, "s-stage-5", "R2")
    await _lease(client, "s-stage-5", "server", SERVER, cwd="/src/q", state="working")
    assert (await _agent(client, "s-stage-5", cwd="/src/q"))["stage"] == "R2"


async def test_overlap_carries_it_too(client):
    """The point of the field for an *agent* rather than for a human.

    `R2` on the PR you were about to review means the round you would duplicate
    is already running. Repo, branch and title — the rest of a peer payload —
    say the same thing whichever round it is on.
    """
    await _lease(client, "s-stage-peer", "server", SERVER, repo="quarterback",
                 title="the fleet view says which work an agent is on")
    await _stage(client, "s-stage-peer", "R2")

    peers = (await client.get(
        "/overlap",
        params={"mine": "s-stage-mine", "repo": "quarterback",
                "subject": "the fleet view says which work an agent is on"},
        headers=LAPTOP,
    )).json()["peers"]
    me = next(p for p in peers if p["session"] == "s-stage-peer")
    assert me["stage"] == "R2"


# ---- the shape, and only the shape ------------------------------------------


@pytest.mark.parametrize("stage", ["F0", "R1", "R1F", "R2F", "R10", "x", "ABC123"])
async def test_any_well_formed_token_is_accepted_whatever_it_means(client, stage):
    """A skill adding `R4F` must not need a server edit.

    The two failure modes are lopsided: an unknown-but-well-formed token renders
    as six harmless characters, where a rejected one stops a workflow to argue
    about a cosmetic field.
    """
    await _lease(client, f"s-shape-{stage}", "server", SERVER, cwd="/src/shape")
    assert (await _stage(client, f"s-shape-{stage}", stage)).status_code == 200


@pytest.mark.parametrize("stage", ["R1234567", "R1 F", "R1-F", "<b>x</b>", "", "R1\n"])
async def test_a_malformed_stage_is_refused_not_stored(client, stage):
    """This is rendered into a narrow column on four surfaces.

    Nothing here is a plausible typo of a stage; each is a token that would cost
    a renderer something — width, a line break, markup — for a field that is
    worth none of it.
    """
    await _lease(client, "s-shape-bad", "server", SERVER, cwd="/src/shape")
    assert (await _stage(client, "s-shape-bad", stage)).status_code == 422
    assert (await _agent(client, "s-shape-bad", cwd="/src/shape"))["stage"] is None


def test_the_board_and_qb_stage_check_the_same_shape():
    """One shape, two implementations — held together rather than hoped about.

    `qb-stage` validates before it writes its marker and the board validates
    before it stores; if those two ever disagreed, a session's pane footer and
    its row in the fleet view would say different things about the same work.
    """
    script = QB_STAGE.read_text(encoding="utf-8")
    found = re.search(r'"\$STAGE" =~ (\S+)', script)
    assert found, "qb-stage no longer contains a recognisable stage-shape check"
    assert found.group(1) == STAGE_RE


# ---- who may say it, and about what -----------------------------------------


async def test_a_stage_needs_a_live_lease_to_sit_on(client):
    """404 is a real answer, not a shrug: there is nowhere to put a stage.

    `qb-stage` treats this like every other failure — as nothing — but the board
    still has to say which nothing it is.
    """
    assert (await _stage(client, "s-stage-nobody", "R1")).status_code == 404


async def test_another_machine_cannot_stage_your_lease(client):
    """Same ownership rule as renew and release: a lease belongs to the box."""
    await _lease(client, "s-stage-mine-only", "server", SERVER, cwd="/src/q")
    assert (await _stage(client, "s-stage-mine-only", "R1", headers=LAPTOP)).status_code == 403
    assert (await _agent(client, "s-stage-mine-only", cwd="/src/q"))["stage"] is None


async def test_an_expired_lease_is_not_stageable(client):
    """Expiry is how a crashed holder's stage goes away, so it must not come back."""
    await _lease(client, "s-stage-gone", "server", SERVER, cwd="/src/q", ttl=1)
    lease = (await client.post(
        "/lease", json={"session": "s-stage-gone2", "device": "server", "ttl": 1},
        headers=SERVER)).json()
    assert lease["lease_id"]
    # Nothing sleeps: release is the deterministic way to make a lease inactive,
    # and `_active_lease` treats released and expired identically.
    await client.post("/lease/release", json={"lease_id": lease["lease_id"]}, headers=SERVER)
    assert (await _stage(client, "s-stage-gone2", "R1")).status_code == 404


# ---- the live stream --------------------------------------------------------


async def _stage_posts(client, session):
    posts = (await client.get(
        "/board", params={"session": session, "limit": 50}, headers=SERVER)).json()
    return sorted(
        (p for p in posts if p["summary"].startswith("stage ")), key=lambda p: p["id"]
    )


async def test_a_transition_is_announced_on_the_stream(client):
    """The only thing anyone follows live is the post stream.

    `/active` is polled every 20s by the fleet pane and every 4s by
    `qb-dash-tui`; a stage transition is a handful of events per session per day
    against the heartbeats that stream already carries, so it goes on it.
    """
    await _lease(client, "s-stage-post", "server", SERVER, repo="quarterback",
                 branch="feat/issue-262")
    await _stage(client, "s-stage-post", "R1")

    posts = await _stage_posts(client, "s-stage-post")
    assert len(posts) == 1
    # Where as well as what: `R1` alone says nothing to a follower who cannot see
    # which of eight sessions moved.
    assert posts[0]["summary"] == "stage R1 · quarterback · feat/issue-262"
    assert posts[0]["type"] == "status"


async def test_re_asserting_the_same_stage_says_nothing(client):
    """A skill that calls `qb-stage R1` twice must not post twice.

    "On change" is a comparison only the board can make — the caller's marker is
    its own machine's — and it is what keeps a high-signal event from becoming
    volume.
    """
    await _lease(client, "s-stage-quiet", "server", SERVER, repo="quarterback")
    first = await _stage(client, "s-stage-quiet", "R1")
    again = await _stage(client, "s-stage-quiet", "R1")

    assert first.json()["changed"] is True
    assert again.json()["changed"] is False
    assert len(await _stage_posts(client, "s-stage-quiet")) == 1


async def test_clearing_is_announced_as_a_clear_and_not_as_a_stage(client):
    """`stage cleared`, never an empty `stage ` — the wire has the same duty as a column."""
    await _lease(client, "s-stage-cleared", "server", SERVER, repo="quarterback")
    await _stage(client, "s-stage-cleared", "R2")
    await _stage(client, "s-stage-cleared", None)

    summaries = [p["summary"] for p in await _stage_posts(client, "s-stage-cleared")]
    assert summaries == ["stage R2 · quarterback", "stage cleared · quarterback"]


async def test_a_refused_stage_is_not_announced(client):
    """Nothing changed, so nothing happened — the post follows the write."""
    await _lease(client, "s-stage-refused", "server", SERVER, repo="quarterback")
    assert (await _stage(client, "s-stage-refused", "R1 F")).status_code == 422
    assert await _stage_posts(client, "s-stage-refused") == []
