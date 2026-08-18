"""What a holder is doing, and how old that answer is.

`state` exists for a human looking at a wall of panes, and every one of its
failure modes reads the same way from out there — the pane looks fine. So these
cases are mostly about the negative space: that a state does not survive its
holder saying nothing new, that the board never invents one, and that the one
value nobody is allowed to report stays unreportable.
"""

from __future__ import annotations

from datetime import datetime

from .conftest import LAPTOP, SERVER


async def _lease(client, session, device, headers, **kw):
    return await client.post(
        "/lease", json={"session": session, "device": device, **kw}, headers=headers
    )


async def test_state_rides_the_lease_to_active(client):
    await _lease(client, "s-state-1", "server", SERVER, cwd="/src/q", state="waiting")

    agents = (await client.get("/active", params={"cwd": "/src/q"}, headers=SERVER)).json()["agents"]
    me = next(a for a in agents if a["session"] == "s-state-1")
    assert me["state"] == "waiting"
    # The pair, always: a state with no age cannot be judged stale, and staleness
    # is the whole reason a reader looks at this field.
    assert me["state_at"] is not None


async def test_state_at_moves_with_the_state(client):
    await _lease(client, "s-state-2", "server", SERVER, cwd="/src/q", state="working")
    first = (await client.get("/active", params={"cwd": "/src/q"}, headers=SERVER)).json()
    t1 = next(a for a in first["agents"] if a["session"] == "s-state-2")["state_at"]

    await _lease(client, "s-state-2", "server", SERVER, cwd="/src/q", state="waiting")
    second = (await client.get("/active", params={"cwd": "/src/q"}, headers=SERVER)).json()
    me = next(a for a in second["agents"] if a["session"] == "s-state-2")

    assert me["state"] == "waiting"
    assert me["state_at"] >= t1


async def test_a_renewal_that_says_nothing_leaves_the_state_alone(client):
    """A heartbeat is not a state report.

    The hook leases on several events and only some of them know a state. If a
    silent renewal cleared the field, a session would flicker between working and
    unknown for no reason a reader could see; if it *refreshed* `state_at`, a
    dead pane would look freshly alive on every heartbeat, which is worse — that
    is precisely the signal staleness is computed from.
    """
    await _lease(client, "s-state-3", "server", SERVER, cwd="/src/q", state="working")
    before = next(
        a for a in (await client.get("/active", params={"cwd": "/src/q"}, headers=SERVER)).json()["agents"]
        if a["session"] == "s-state-3"
    )

    await _lease(client, "s-state-3", "server", SERVER, cwd="/src/q")  # no state
    after = next(
        a for a in (await client.get("/active", params={"cwd": "/src/q"}, headers=SERVER)).json()["agents"]
        if a["session"] == "s-state-3"
    )

    assert after["state"] == "working"
    assert after["state_at"] == before["state_at"]


async def test_a_lease_that_never_reported_one_says_so(client):
    """None, not a guess. Every lease on the board predates this field."""
    await _lease(client, "s-state-4", "server", SERVER, cwd="/src/q")
    me = next(
        a for a in (await client.get("/active", params={"cwd": "/src/q"}, headers=SERVER)).json()["agents"]
        if a["session"] == "s-state-4"
    )
    assert me["state"] is None
    assert me["state_at"] is None


async def test_stalled_is_not_reportable(client):
    """Nobody says they are stalled — a reader concludes it from `state_at`.

    Accepting the word would let a holder assert a state it cannot know it is
    in, and would put two sources of the same conclusion on the board.
    """
    r = await _lease(client, "s-state-5", "server", SERVER, cwd="/src/q", state="stalled")
    assert r.status_code == 422


async def test_an_unknown_state_is_refused_not_stored(client):
    """The vocabulary is closed at the edge.

    It is rendered as a word in a footer and a colour in a dashboard; an unknown
    value reaches a human as a blank or a crash, and no reader can do anything
    useful with a fourth spelling.
    """
    r = await _lease(client, "s-state-6", "server", SERVER, cwd="/src/q", state="compacting")
    assert r.status_code == 422


async def test_overlap_carries_it_too(client):
    """/overlap is what an agent asks about a *task*; /active about a directory.

    A peer's state belongs in both — "somebody else is on this and waiting on a
    human" is a different answer from "somebody else is on this and moving".
    """
    await _lease(
        client, "s-state-7", "laptop", LAPTOP,
        cwd="/src/q", repo="q", title="mangled acronyms", state="input",
    )
    peers = (await client.get(
        "/overlap", params={"mine": "s-mine", "repo": "q"}, headers=SERVER
    )).json()["peers"]
    me = next(p for p in peers if p["session"] == "s-state-7")
    assert me["state"] == "input"
    assert datetime.fromisoformat(me["state_at"]).tzinfo is not None
