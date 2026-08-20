"""v2.31: an atomic claim on a named resource — landing, and release numbers.

#99 and #46 wanted the same primitive, and the reason it has to be a TABLE and
not a convention is that this repo falsified the convention nine times in two
days. Two agents announced the same version one second apart, both correct from
what they could see. A number announced on the board at 10:17 was taken at 11:18
by an agent picking its number from `main` plus the open PRs' CHANGELOGs — a
check that structurally cannot see a claim which is not in a file. The renumber
off that collision landed on a number claimed seven minutes earlier.

So the properties under test are the ones that distinguish an allocation from an
announcement:

* **Two claimants, one winner, decided by the database.** Not by looking first —
  every collision above happened in the gap between looking and writing.
* **The refusal names the holder and what they are doing.** A refusal that says
  only "held" leaves the loser nothing to do but spin.
* **A lapsed claim frees the key, passively.** No reaper; a crashed lander must
  not wedge everybody's landing.
* **Advisory, and it says so.** The board cannot gate github.com.

**The release allocator that shipped beside this is gone (#172).** Its tests went
with it, and the two premises here that were never about numbers were rewritten
against `POST /claim` rather than deleted with the endpoint that happened to
exercise them: a renew really extends the TTL, and a blank session is not an
identity. Both were round-1 findings about the primitive, and losing them with the
allocator would have been the deletion taking coverage it did not own.
"""

from __future__ import annotations

from .conftest import DESKTOP, LAPTOP

#: The repo half of every merge key here. `owner/name`, because that is the only
#: shape `app.claimkey` recognises — a key it cannot parse is left alone, and a
#: test written against an unparseable one would pass while asserting nothing.
REPO = "acme/allocrepo"


async def claim(client, kind: str, key: str, headers=LAPTOP, **over):
    return await client.post("/claim", json={"kind": kind, "key": key, **over},
                             headers=headers)


async def take(client, kind: str, key: str, headers=LAPTOP, **over) -> dict:
    r = await claim(client, kind, key, headers=headers, **over)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------ one winner, decided by the DB

async def test_a_second_claimant_is_refused_and_told_who_holds_it(client):
    """The refusal IS the coordination. An agent told only "held" can do nothing
    but retry; one told who has it and why can go and talk to them."""
    first = await take(client, "merge", f"{REPO}:main", session="s-1",
                       note="landing #128")
    assert first["claimed"] is True and first["renewed"] is False

    r = await claim(client, "merge", f"{REPO}:main", headers=DESKTOP)
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert d["held_by"] == first["holder"]
    assert d["note"] == "landing #128"
    assert d["session"] == "s-1"
    assert "expires" in d and "acquired" in d


async def test_the_refusal_says_it_is_advisory(client):
    """It cannot stop a merge — a human in the GitHub UI or an unenrolled agent
    lands regardless — and the one moment a reader is guaranteed to be paying
    attention is when they have just been refused. #99 asks for this in the
    implementation and not only in the issue, because the next reader will assume
    mutual exclusion means mutual exclusion."""
    await take(client, "merge", f"{REPO}:advisory")
    r = await claim(client, "merge", f"{REPO}:advisory", headers=DESKTOP)
    assert "advisory" in r.json()["detail"]["advisory"]


async def test_reclaiming_your_own_machines_claim_is_a_renew(client):
    """A claim belongs to the box, exactly as a session lease does. An agent that
    restarts mid-land must be able to pick its own claim back up rather than be
    locked out by its former self."""
    first = await take(client, "merge", f"{REPO}:renew", note="first")
    again = await take(client, "merge", f"{REPO}:renew", note="second")
    assert again["renewed"] is True
    assert again["claim_id"] == first["claim_id"], "the same row, not a new one"
    assert again["note"] == "second"


async def test_releasing_frees_it_for_the_next_agent(client):
    held = await take(client, "merge", f"{REPO}:handover")
    r = await client.post("/claim/release", json={"claim_id": held["claim_id"]},
                          headers=LAPTOP)
    assert r.status_code == 200 and r.json()["released"] is True

    nxt = await take(client, "merge", f"{REPO}:handover", headers=DESKTOP)
    assert nxt["claimed"] is True and nxt["renewed"] is False


async def test_releasing_is_idempotent_and_never_deletes_the_row(client):
    """The row is the history an allocator reads. A release that deleted it would
    make a handed-out number look never-issued."""
    held = await take(client, "merge", "acme/idemrepo:9.1")
    for _ in range(2):
        r = await client.post("/claim/release", json={"claim_id": held["claim_id"]},
                              headers=LAPTOP)
        assert r.status_code == 200
    r = await client.get("/claims", params={"kind": "merge", "key": "acme/idemrepo:9.1",
                                            "include_released": True}, headers=LAPTOP)
    assert len(r.json()["claims"]) == 1


async def test_another_machine_cannot_release_your_claim(client):
    held = await take(client, "merge", f"{REPO}:notyours")
    r = await client.post("/claim/release", json={"claim_id": held["claim_id"]},
                          headers=DESKTOP)
    assert r.status_code == 403


# --------------------------------------------------- passive expiry, no reaper

async def test_a_lapsed_claim_frees_the_key_without_a_reaper(client):
    """A crashed lander must not wedge everybody else's landing, and nothing runs
    in the background to notice. The sweep happens because somebody asked for
    this exact key — so a quiet key costs nothing at all."""
    await take(client, "merge", f"{REPO}:lapse", ttl=1)
    import asyncio
    await asyncio.sleep(1.1)

    got = await take(client, "merge", f"{REPO}:lapse", headers=DESKTOP)
    assert got["claimed"] is True and got["renewed"] is False

    r = await client.get("/claims", params={"kind": "merge", "key": f"{REPO}:lapse",
                                            "include_released": True}, headers=LAPTOP)
    swept = [c for c in r.json()["claims"] if c["released"]]
    assert len(swept) == 1
    assert swept[0]["lapsed"] is True, "the holder vanished; it did not let go"


async def test_letting_go_is_not_the_same_fact_as_lapsing(client):
    """Two different events, and for a release number the difference is 'shipped'
    against 'abandoned'. One column, and the allocator would have to guess."""
    held = await take(client, "merge", f"{REPO}:letgo")
    await client.post("/claim/release", json={"claim_id": held["claim_id"]},
                      headers=LAPTOP)
    r = await client.get("/claims", params={"kind": "merge", "key": f"{REPO}:letgo",
                                            "include_released": True}, headers=LAPTOP)
    assert r.json()["claims"][0]["lapsed"] is False


async def test_an_expired_claim_cannot_be_renewed_back_to_life(client):
    """Somebody else may already hold the key. Silently extending would hand one
    resource to two holders — the whole failure being fixed, reintroduced by the
    convenience path."""
    held = await take(client, "merge", f"{REPO}:expired", ttl=1)
    import asyncio
    await asyncio.sleep(1.1)
    r = await client.post("/claim/renew", json={"claim_id": held["claim_id"]},
                          headers=LAPTOP)
    assert r.status_code == 409
    assert "re-take" in r.json()["detail"]["error"]


# ---------------------------------------------- the race itself, run concurrently

async def test_two_machines_racing_for_one_key_produce_exactly_one_winner(client):
    """The property everything else rests on, exercised as an actual race rather
    than as two sequential calls.

    Every collision this table exists to prevent happened in the gap between
    looking and writing, so a test that looks and then writes cannot see the bug
    it is guarding. These two requests are in flight together, each with its own
    database session, and the partial unique index is the only thing deciding
    between them."""
    import asyncio
    key = f"{REPO}:racy"
    laptop, desktop = await asyncio.gather(
        claim(client, "merge", key, headers=LAPTOP, note="A"),
        claim(client, "merge", key, headers=DESKTOP, note="B"),
    )
    codes = sorted([laptop.status_code, desktop.status_code])
    assert codes == [200, 409], f"expected one winner and one refusal, got {codes}"

    won = laptop if laptop.status_code == 200 else desktop
    lost = desktop if laptop.status_code == 200 else laptop
    assert lost.json()["detail"]["held_by"] == won.json()["holder"]

    r = await client.get("/claims", params={"kind": "merge", "key": key}, headers=LAPTOP)
    assert len(r.json()["claims"]) == 1, "one key, one outstanding claim"


# ------------------------------------------------- the renumber, as one step

# ---------------------------------------------- round 1's premises, pinned

# --------------------------------------------------------------------------
# #142 — the co-tenant rule is about EXCLUSIVITY, not about releases
#
# v2.31's round 1 established that two agents on one box are two agents, and
# v2.33 applied it to `kind == "release"` alone. Every other kind kept the
# machine-only authorisation the argument had just removed — so on this fleet,
# where every agent authenticates as one machine, a second agent claiming a key
# another already held got `renewed: true` instead of a 409. A collision with a
# green light on it, which is worse than no claim because it reads as
# authoritative: measured 2026-08-16, three agents claimed overlapping work
# inside 56 seconds and a human resolved it by reading timestamps.
#
# These pin the general rule on a NON-release kind, because that is the half the
# release tests cannot cover.
# --------------------------------------------------------------------------

async def test_a_co_tenant_cannot_take_a_work_claim_another_agent_holds(client):
    """The whole point. A second agent on the same box asking for a key that is
    already held must be REFUSED and told who has it — not quietly handed a
    renew, which is what made a stampede look like three successful claims."""
    key = "acme/repo#142"
    mine = await client.post("/claim", headers=LAPTOP, json={
        "kind": "work", "key": key, "ttl": 600, "session": "s-mine"})
    assert mine.status_code == 200, mine.text
    assert mine.json()["renewed"] is False

    theirs = await client.post("/claim", headers=LAPTOP, json={
        "kind": "work", "key": key, "ttl": 600, "session": "s-theirs"})
    assert theirs.status_code == 409, theirs.text
    detail = theirs.json()["detail"]
    # The loser of a race is the caller who most needs to know who won.
    assert detail["held_by"] == mine.json()["holder"]
    assert detail["key"] == key


async def test_the_same_session_still_renews_its_own_work_claim(client):
    """The other side of it, and the one a too-broad fix would break: an agent
    re-claiming its OWN key is a renew, not a conflict. That is how a long task
    keeps its claim alive past the TTL."""
    key = "acme/repo#renew"
    first = await client.post("/claim", headers=LAPTOP, json={
        "kind": "work", "key": key, "ttl": 600, "session": "s-mine"})
    again = await client.post("/claim", headers=LAPTOP, json={
        "kind": "work", "key": key, "ttl": 600, "session": "s-mine"})
    assert again.status_code == 200, again.text
    assert again.json()["renewed"] is True
    assert again.json()["claim_id"] == first.json()["claim_id"]


async def test_a_work_claim_that_named_no_session_still_falls_back_to_the_machine(client):
    """Kept deliberately from the release-only version, because it was right and
    was never release-specific: a claim with no session has nothing finer to
    check, and refusing outright would strand claims taken by callers that sent
    none."""
    key = "acme/repo#nosession"
    mine = await client.post("/claim", headers=LAPTOP, json={
        "kind": "work", "key": key, "ttl": 600})
    assert mine.status_code == 200, mine.text
    again = await client.post("/claim", headers=LAPTOP, json={
        "kind": "work", "key": key, "ttl": 600, "session": "s-anything"})
    assert again.status_code == 200, again.text
    assert again.json()["renewed"] is True


async def test_a_co_tenant_cannot_release_or_renew_another_agents_work_claim(client):
    """Refusing the TAKE is not enough on its own — release and renew are the
    paths by which a co-tenant could drop a claim out from under the agent doing
    the work, and they authorise through the same predicate."""
    key = "acme/repo#mutate"
    mine = await client.post("/claim", headers=LAPTOP, json={
        "kind": "work", "key": key, "ttl": 600, "session": "s-mine"})
    body = {"claim_id": mine.json()["claim_id"], "session": "s-theirs"}
    assert (await client.post("/claim/release", json=body, headers=LAPTOP)).status_code == 403
    assert (await client.post("/claim/renew", json=body, headers=LAPTOP)).status_code == 403


async def test_a_different_MACHINE_is_still_refused_before_any_session_check(client):
    """The machine remains necessary throughout. A session id is not a bearer
    token: presenting the right one from the wrong machine must not pass, or the
    session becomes a credential anybody who saw a board post can replay."""
    key = "acme/repo#machine"
    mine = await client.post("/claim", headers=LAPTOP, json={
        "kind": "work", "key": key, "ttl": 600, "session": "s-mine"})
    assert mine.status_code == 200
    r = await client.post("/claim", headers=DESKTOP, json={
        "kind": "work", "key": key, "ttl": 600, "session": "s-mine"})
    assert r.status_code == 409, r.text


# ------------------------------- round 1's premises about the PRIMITIVE, pinned

async def test_a_renewed_claim_really_has_its_ttl_extended(client):
    """Round 1's F05 + F21: `renewed: true` meant two different things. One path
    extended the TTL and wrote it; two others returned the row untouched and
    uncommitted, so a caller retrying was told it was renewed and had its claim
    lapse anyway. Asserted on `POST /claim` now that the allocator is gone — the
    finding was about `_renew_onto`, which every path still shares."""
    first = await take(client, "merge", f"{REPO}:ttl", session="s-t", ttl=60)
    again = await take(client, "merge", f"{REPO}:ttl", session="s-t", ttl=3600)
    assert again["renewed"] is True
    assert again["expires"] > first["expires"], "the TTL actually moved"


async def test_an_empty_session_is_not_a_session(client):
    """Round 1's F27. `session=""` was stored on the first claim and skipped by
    every lookup that tests truthiness, so the two disagreed about whether the
    claim had an owner at all. One normalisation at the edge (`clean_session`),
    and blank is absent everywhere."""
    got = await take(client, "merge", f"{REPO}:blanksess", session="")
    assert got["session"] is None
    # ...and it is absent for AUTHORISATION too, which is where it mattered: a
    # claim that recorded no session falls back to the machine rather than being
    # owned by the empty string, so the same box can still act on it.
    ok = await client.post("/claim/renew",
                           json={"claim_id": got["claim_id"], "session": "s-anything"},
                           headers=LAPTOP)
    assert ok.status_code == 200, ok.text
