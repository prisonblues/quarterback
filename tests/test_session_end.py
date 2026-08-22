"""`POST /session/end` — the stop verb, and the reason it carries (#277).

The fleet had three ways to start a session and none to end one, so what stood in
for ending was expiry. Expiry is a floor and not a report: an expired lease says
*nobody renewed*, which is the identical row whether the work finished, the pane
was closed, or the agent is thinking hard (#252). And because nothing released a
claim except the agent that took it, a seat whose context was reset kept work it
had no memory of taking, renewing it from a fresh conversation where passive
expiry could never reach it, because nothing had died (#263).

So the properties here are about what the board can now TELL APART, and about the
blast radius of a release that happens on somebody else's say-so:

* an ended session and a lapsed one are different rows and read differently;
* ending releases the claims the SESSION took, and only those — not a co-tenant's
  on the same box, and not the machine-scoped one a checkout takes before the
  agent that will use it exists;
* the reason is a closed vocabulary, because it is branched on;
* it is idempotent, because a hook and a human on the ✕ race here by design.
"""

from __future__ import annotations

import asyncio
import hashlib

from .conftest import DESKTOP, LAPTOP, SERVER

REPO = "acme/endrepo"


async def lease(client, session, headers=LAPTOP, device="laptop", **over):
    r = await client.post("/lease",
                          json={"session": session, "device": device, **over},
                          headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def claim(client, key, session, headers=LAPTOP, kind="merge", **over):
    r = await client.post("/claim",
                          json={"kind": kind, "key": key, "session": session, **over},
                          headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def end(client, session, reason="finished", headers=LAPTOP):
    return await client.post("/session/end",
                             json={"session": session, "reason": reason},
                             headers=headers)


async def snapshot(client, session, headers=LAPTOP):
    """Give a session the durable `sessions` row a real one gets from its Stop
    hook. Without one there is nothing for `/sessions` to list and `/session/<k>`
    knows the session only by its leases."""
    body = f"{{\"session\": \"{session}\"}}\n".encode()
    sha = hashlib.sha256(body).hexdigest()
    assert (await client.put(f"/blob/{sha}", content=body,
                             headers=headers)).status_code == 200
    r = await client.post("/snapshot", json={"session": session, "blob": sha},
                          headers=headers)
    assert r.status_code == 200, r.text


async def held(client, key, kind="merge", headers=LAPTOP) -> list[dict]:
    r = await client.get("/claims", params={"kind": kind, "key": key}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["claims"]


# ------------------------------------------------------------- the lease half


async def test_ending_releases_the_lease_and_stamps_why(client):
    await lease(client, "s-end-1", cwd="/src/q")

    r = await end(client, "s-end-1", "finished")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ended"] is True
    assert body["lease_was"] == "released"
    assert body["lease"]["end_reason"] == "finished"
    assert body["lease"]["released"] is not None

    live = (await client.get("/active", params={"cwd": "/src/q"}, headers=LAPTOP)).json()
    assert not [a for a in live["agents"] if a["session"] == "s-end-1"]


async def test_an_ended_session_does_not_read_like_a_lapsed_one(client):
    """The whole point of the field, and the two rows it has to keep apart.

    Both sessions are gone from `/active` and neither can be renewed. The
    difference is that one of them was *reported*: something observed the ending
    and said which kind it was. The other is a lease nobody renewed, and the
    board genuinely does not know whether that agent finished or died — which is
    the honest answer, and the one it used to give about both.
    """
    await lease(client, "s-end-said", cwd="/src/q")
    await snapshot(client, "s-end-said")
    await end(client, "s-end-said", "finished")

    await lease(client, "s-end-lapsed", ttl=1, cwd="/src/q")
    await snapshot(client, "s-end-lapsed")
    await asyncio.sleep(1.1)

    said = (await client.get("/session/s-end-said", headers=LAPTOP)).json()
    lapsed = (await client.get("/session/s-end-lapsed", headers=LAPTOP)).json()

    assert said["ended"]["reason"] == "finished"
    assert said["ended"]["at"] is not None
    assert lapsed["ended"] is None
    assert lapsed["active_lease"] is None  # gone from the live view all the same


async def test_a_plain_release_is_not_an_ending(client):
    """`/lease/release` and `/handoff` say the lease is free; they do not say the
    session finished. Reading them as an ending would invent a report nobody made
    — a device handing a session to another device has not ended anything."""
    got = await lease(client, "s-end-handed", cwd="/src/q")
    await snapshot(client, "s-end-handed")
    r = await client.post("/lease/release", json={"lease_id": got["lease_id"]},
                          headers=LAPTOP)
    assert r.status_code == 200, r.text

    state = (await client.get("/session/s-end-handed", headers=LAPTOP)).json()
    assert state["ended"] is None

    again = await end(client, "s-end-handed", "finished")
    assert again.json()["lease_was"] == "already released"


async def test_the_sessions_list_carries_the_ending(client):
    """The list a fleet view renders, not just the single-session lookup."""
    await lease(client, "s-end-list", cwd="/src/q")
    await snapshot(client, "s-end-list")
    await end(client, "s-end-list", "killed")

    rows = (await client.get("/sessions", params={"limit": 500}, headers=LAPTOP)).json()
    row = next(s for s in rows if s["session"] == "s-end-list")
    assert row["live"] is False
    assert row["ended"]["reason"] == "killed"


async def test_a_live_session_reports_no_ending(client):
    """`ended` is present on every row, so a reader never has to work out which
    branch built it — and on a live one it is None rather than absent."""
    await lease(client, "s-end-live", cwd="/src/q")
    await snapshot(client, "s-end-live")
    rows = (await client.get("/sessions", params={"limit": 500}, headers=LAPTOP)).json()
    row = next(s for s in rows if s["session"] == "s-end-live")
    assert row["live"] is True and row["ended"] is None


# ------------------------------------------------------------- the claim half


async def test_ending_hands_back_the_claims_that_session_took(client):
    """#263: the claims went with the conversation, and nothing gave them back.

    Passive expiry could not reach them — the seat was alive and renewing — so a
    peer running `claims()` saw a live claim on a healthy agent and correctly
    concluded the work was covered. It was covered by nobody.
    """
    await lease(client, "s-end-claims", cwd="/src/q")
    await claim(client, f"{REPO}:main", "s-end-claims", note="landing #128")

    body = (await end(client, "s-end-claims", "finished")).json()
    assert [c["key"] for c in body["released_claims"]] == [f"{REPO}:main"]

    assert await held(client, f"{REPO}:main") == []
    every = (await client.get("/claims", params={"kind": "merge", "key": f"{REPO}:main",
                                                 "include_released": True},
                              headers=LAPTOP)).json()["claims"]
    # Released, not lapsed. The holder let go; nobody vanished — and `qbdata`
    # filters on exactly that column to tell an abandoned land from a finished one.
    assert every[0]["released"] is not None and every[0]["lapsed"] is False


async def test_a_co_tenants_claims_are_left_alone(client):
    """Two agents on one box are two agents — this module's own rule, applied to
    a release rather than to a mutation. Ending one session must not disarm the
    other, which is a worse failure than the one being fixed: it would hand a
    resource somebody is actively working to the next claimant."""
    await lease(client, "s-end-mine", cwd="/src/q")
    await claim(client, f"{REPO}:mine", "s-end-mine")
    await claim(client, f"{REPO}:theirs", "s-end-neighbour")

    body = (await end(client, "s-end-mine", "finished")).json()
    assert [c["key"] for c in body["released_claims"]] == [f"{REPO}:mine"]
    assert [c["session"] for c in await held(client, f"{REPO}:theirs")] == ["s-end-neighbour"]


async def test_a_claim_that_named_no_session_belongs_to_the_machine(client):
    """`create-worktree` takes one before the agent that will use the tree exists,
    so it names no session and belongs to the box. Sweeping it up when some
    unrelated session on that box ended would free a checkout's issue out from
    under whoever is about to pick it up."""
    await lease(client, "s-end-boxwide", cwd="/src/q")
    await claim(client, f"{REPO}:checkout", None)

    body = (await end(client, "s-end-boxwide", "finished")).json()
    assert body["released_claims"] == []
    assert len(await held(client, f"{REPO}:checkout")) == 1


async def test_another_machines_claim_on_this_session_key_is_reported_not_taken(client):
    """A session key is opaque to the board and two machines may spell one the
    same way. Releasing across that boundary would be this endpoint reaching onto
    a box it has no authority over, so it is refused per claim and SAID — reading
    `released_claims` as "everything is let go" is the mistake to prevent."""
    await lease(client, "s-end-shared", headers=LAPTOP, cwd="/src/q")
    await claim(client, f"{REPO}:elsewhere", "s-end-shared", headers=DESKTOP)

    body = (await end(client, "s-end-shared", "finished", headers=LAPTOP)).json()
    assert body["released_claims"] == []
    assert [c["key"] for c in body["refused_claims"]] == [f"{REPO}:elsewhere"]
    assert len(await held(client, f"{REPO}:elsewhere", headers=DESKTOP)) == 1


async def test_a_context_reset_hands_the_work_back_and_says_what_it_was(client):
    """#263's shape end to end: `/clear` gives the pane a fresh conversation with
    no memory of the previous one, and the previous one's claims used to stay
    live and keep renewing. Now they go, and the ending is labelled as the reset
    it was — not as a finish, which would say the work was done."""
    await lease(client, "s-end-reset", cwd="/src/q")
    await snapshot(client, "s-end-reset")
    await claim(client, f"{REPO}:reset", "s-end-reset")

    body = (await end(client, "s-end-reset", "context_reset")).json()
    assert body["reason"] == "context_reset"
    assert [c["key"] for c in body["released_claims"]] == [f"{REPO}:reset"]
    assert await held(client, f"{REPO}:reset") == []
    assert (await client.get("/session/s-end-reset",
                             headers=LAPTOP)).json()["ended"]["reason"] == "context_reset"


async def test_a_session_leased_again_reports_no_ending_while_it_is_live(client):
    """An ending belongs to a LEASE, not to a session key, and a key can be leased
    again — a resume, or a device taking a session over. Reporting the old ending
    beside `live: true` would be the board asserting two contradictory things
    about one row."""
    await lease(client, "s-end-again", cwd="/src/q")
    await snapshot(client, "s-end-again")
    await end(client, "s-end-again", "finished")
    assert (await client.get("/session/s-end-again",
                             headers=LAPTOP)).json()["ended"]["reason"] == "finished"

    await lease(client, "s-end-again", cwd="/src/q")  # resumed
    state = (await client.get("/session/s-end-again", headers=LAPTOP)).json()
    assert state["active_lease"] is not None
    assert state["ended"] is None

    row = next(r for r in (await client.get("/sessions", params={"limit": 500},
                                            headers=LAPTOP)).json()
               if r["session"] == "s-end-again")
    assert row["live"] is True and row["ended"] is None


async def test_an_ending_a_later_lease_has_superseded_is_not_reported(client):
    """And it stays quiet once that second lease lapses. The last thing that
    happened to this key is a lease nobody renewed, which says nothing — dressing
    it in an ending from two leases ago would be worse than the silence."""
    await lease(client, "s-end-super", cwd="/src/q")
    await snapshot(client, "s-end-super")
    await end(client, "s-end-super", "finished")
    await lease(client, "s-end-super", ttl=1, cwd="/src/q")
    await asyncio.sleep(1.1)

    state = (await client.get("/session/s-end-super", headers=LAPTOP)).json()
    assert state["active_lease"] is None and state["ended"] is None


# --------------------------------------------------------- refusals and races


async def test_a_reason_outside_the_vocabulary_is_refused(client):
    """Free text here would be the same mistake as a free-text `state`: this is
    read as a word in a fleet view and branched on by a dashboard, so a sixth
    spelling of "finished" reaches a human as an unknown."""
    await lease(client, "s-end-vocab", cwd="/src/q")
    await snapshot(client, "s-end-vocab")
    r = await end(client, "s-end-vocab", "gave up")
    assert r.status_code == 422
    assert (await client.get("/session/s-end-vocab", headers=LAPTOP)).json()["ended"] is None


async def test_stalled_and_crashed_are_not_reasons(client):
    """Both are conclusions a reader draws from silence, never a report somebody
    makes about themselves — the same rule `LeaseIn.state` states for `stalled`.
    A crashed session reports nothing at all, and a lease with no reason on it IS
    that report."""
    await lease(client, "s-end-notaword", cwd="/src/q")
    for word in ("stalled", "crashed"):
        assert (await end(client, "s-end-notaword", word)).status_code == 422


async def test_ending_twice_is_a_fine_answer_and_says_which_call_did_it(client):
    """A hook and a human on the ✕ race here by design, and the second must not
    fail because the first won. `ended` is the answer to "did I do it"."""
    await lease(client, "s-end-twice", cwd="/src/q")
    await snapshot(client, "s-end-twice")
    first = (await end(client, "s-end-twice", "killed")).json()
    second = (await end(client, "s-end-twice", "finished")).json()

    assert first["ended"] is True
    assert second["ended"] is False and second["lease_was"] == "already ended"
    # And the first reason stands. The observer that actually saw the ending got
    # there first; a backstop arriving afterwards must not relabel it.
    assert (await client.get("/session/s-end-twice",
                             headers=LAPTOP)).json()["ended"]["reason"] == "killed"


async def test_two_enders_at_once_still_produce_exactly_one_ending(client):
    """A hook and a human on the ✕ can arrive together, and the answer to "did I
    end it" has to be true for exactly one of them. Read-then-write would tell
    both yes and record whichever reason committed last — the module's own rule
    from `renew_claim`, which is why the release is a conditional UPDATE."""
    await lease(client, "s-end-race", cwd="/src/q")
    await snapshot(client, "s-end-race")

    first, second = await asyncio.gather(end(client, "s-end-race", "killed"),
                                         end(client, "s-end-race", "superseded"))
    assert sorted([first.json()["ended"], second.json()["ended"]]) == [False, True]

    winner = next(r for r in (first, second) if r.json()["ended"])
    recorded = (await client.get("/session/s-end-race", headers=LAPTOP)).json()["ended"]
    assert recorded["reason"] == winner.json()["reason"]


async def test_another_machines_session_is_refused(client):
    """A session is ended by the box it runs on, or by whatever is closing it
    there. Ending one from across the fleet would be the board operating a
    machine, which is the line this whole change is careful not to cross."""
    await lease(client, "s-end-theirs", headers=SERVER, device="server")
    r = await end(client, "s-end-theirs", "killed", headers=LAPTOP)
    assert r.status_code == 403
    assert (await client.get("/active", params={"holder": "server"},
                             headers=LAPTOP)).json()["agents"]


async def test_a_session_nobody_ever_leased_says_so(client):
    """Not a 404. The caller is a hook or a button reporting that something is
    over, and "there was nothing there" is a report about the board's state, not
    an error in the request — the ending still happened."""
    body = (await end(client, "s-end-phantom", "finished")).json()
    assert body["ended"] is False and body["lease_was"] == "never leased"


async def test_ending_one_session_leaves_the_agents_other_sessions_alone(client):
    """An agent can hold several sessions at once, and ending one is not the end
    of it — the rule `_retire_if_idle` already states, checked from the outside."""
    await lease(client, "s-end-pair-a", cwd="/src/q")
    await lease(client, "s-end-pair-b", cwd="/src/q")

    await end(client, "s-end-pair-a", "finished")
    live = (await client.get("/active", params={"cwd": "/src/q"}, headers=LAPTOP)).json()
    sessions = {a["session"] for a in live["agents"]}
    assert "s-end-pair-b" in sessions and "s-end-pair-a" not in sessions


async def test_the_shortname_is_freed_once_the_last_session_ends(client):
    """Ending is what release and handoff already treat as "that agent is going".
    The name goes back to the pool so the live space recycles; the key stays a
    permanent alias, so nothing it authored is rewritten."""
    who = {**SERVER, "X-Agent-Key": "endkey1"}
    name = (await client.get("/whoami", headers=who)).json()["name"]
    await lease(client, "s-end-named", headers=who, device="server")

    await end(client, "s-end-named", "finished", headers=who)

    # Freed, and handed straight back to the same key — the probe is seeded by
    # the key, so an agent that goes away and comes back keeps its name.
    assert (await client.get("/whoami", headers=who)).json()["name"] == name
    fresh = {**SERVER, "X-Agent-Key": "endkey2"}
    assert (await client.get("/whoami", headers=fresh)).json()["name"] != name
