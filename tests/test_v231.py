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
* **A lapsed claim does NOT free the NUMBER.** The branch may have shipped it.
  This is the one place where the merge kind and the release kind differ, and
  getting it wrong manufactures the collision the table exists to prevent.
* **Advisory, and it says so.** The board cannot gate github.com.
"""

from __future__ import annotations

from .conftest import DESKTOP, LAPTOP

REPO = "acme/allocrepo"


async def claim(client, kind: str, key: str, headers=LAPTOP, **over):
    return await client.post("/claim", json={"kind": kind, "key": key, **over},
                             headers=headers)


async def take(client, kind: str, key: str, headers=LAPTOP, **over) -> dict:
    r = await claim(client, kind, key, headers=headers, **over)
    assert r.status_code == 200, r.text
    return r.json()


async def alloc(client, headers=LAPTOP, repo: str = REPO, **over) -> dict:
    r = await client.post("/release/claim", json={"repo": repo, **over},
                          headers=headers)
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
    held = await take(client, "release", "acme/idemrepo:9.1")
    for _ in range(2):
        r = await client.post("/claim/release", json={"claim_id": held["claim_id"]},
                              headers=LAPTOP)
        assert r.status_code == 200
    r = await client.get("/claims", params={"kind": "release", "key": "acme/idemrepo:9.1",
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
    assert "re-take" in r.json()["detail"]


# ------------------------------------------------------------- the allocator

async def test_two_agents_asking_get_different_numbers(client):
    """#46 in one test. Announcing left both branches correct and both wrong;
    asking cannot, because the second caller's number comes from the board that
    just handed out the first."""
    a = await alloc(client, repo="acme/tworepo", after="2.28")
    b = await alloc(client, repo="acme/tworepo", headers=DESKTOP, after="2.28")
    assert a["version"] == "2.29"
    assert b["version"] == "2.30", "the board saw its own allocation; the repo scan could not"


async def test_the_caller_and_the_board_cover_each_others_blind_spots(client):
    """Neither input is sufficient. The board cannot read a CHANGELOG, so it
    knows nothing of releases that merged before it existed; the caller's repo
    scan cannot see a claim that is not yet in any file, which is exactly how
    v2.28 was taken an hour after it was announced."""
    first = await alloc(client, repo="acme/blindspot", after="2.28")
    assert first["version"] == "2.29"

    # A caller whose checkout is stale still cannot be given a used number.
    stale = await alloc(client, repo="acme/blindspot", headers=DESKTOP, after="2.20")
    assert stale["version"] == "2.30"

    # ...and a caller that can see further than the board wins the other way.
    ahead = await alloc(client, repo="acme/blindspot", after="3.4")
    assert ahead["version"] == "3.5"


async def test_a_lapsed_release_number_is_never_reissued(client):
    """The one place the release kind differs from the merge kind, and getting it
    wrong manufactures the collision this table exists to prevent: a branch whose
    claim lapsed may well have SHIPPED that number."""
    got = await alloc(client, repo="acme/lapsedrel", after="5.1", ttl=1)
    assert got["version"] == "5.2"
    import asyncio
    await asyncio.sleep(1.1)

    nxt = await alloc(client, repo="acme/lapsedrel", headers=DESKTOP, after="5.1")
    assert nxt["version"] == "5.3", "5.2 was handed out once and is gone forever"


async def test_a_number_held_by_someone_else_is_skipped_not_refused(client):
    """Losing the arithmetic race means somebody just took the number this caller
    was about to. The right answer is the next one, not an error."""
    await alloc(client, repo="acme/skiprepo", after="1.1")          # takes 1.2
    second = await alloc(client, repo="acme/skiprepo", headers=DESKTOP, after="1.1")
    assert second["version"] == "1.3"


async def test_an_unreadable_after_is_reported_rather_than_treated_as_zero(client):
    """A hint this board cannot parse must not become (0, 0) — that would
    allocate v0.1 over the top of a live series. It falls back to board history
    and SAYS it did, because a caller that mistyped its own version wants to know
    before it writes the number into eight files."""
    await alloc(client, repo="acme/badafter", after="2.40")
    got = await alloc(client, repo="acme/badafter", after="HEAD")
    assert got["after_unreadable"] is True
    assert got["version"] == "2.42", "board history carried it, not a zero floor"

    clean = await alloc(client, repo="acme/badafter", after="2.42")
    assert clean["after_unreadable"] is False


async def test_no_after_at_all_is_fine(client):
    """A caller with no checkout — a skill, a hook, the board's own UI — can still
    ask. It just gets an allocation resting on board history alone."""
    got = await alloc(client, repo="acme/noafter")
    assert got["version"] == "0.1"
    assert got["after_unreadable"] is False


async def test_a_three_component_version_is_accepted_at_the_changelog_grain(client):
    """`pyproject.toml` says 2.31.0 and the CHANGELOG says v2.31. A caller
    reading either must not get a different answer."""
    got = await alloc(client, repo="acme/threecomp", after="2.31.0")
    assert got["version"] == "2.32"


async def test_the_releases_view_answers_what_is_landing_soon(client):
    """A question the board could not previously answer at all. #46 names it as a
    side benefit; it is arguably the more useful half day to day."""
    a = await alloc(client, repo="acme/viewrepo", after="7.0", branch="feat/x")
    await client.post("/claim/release", json={"claim_id": a["claim_id"]}, headers=LAPTOP)
    await alloc(client, repo="acme/viewrepo", headers=DESKTOP, note="the live one")

    r = await client.get("/releases", params={"repo": "acme/viewrepo"}, headers=LAPTOP)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["highest_known"] == "7.2"
    rows = {row["version"]: row for row in body["releases"]}
    assert rows["7.1"]["held"] is False and rows["7.1"]["released"] is not None
    assert rows["7.2"]["held"] is True and rows["7.2"]["note"] == "the live one"
    assert rows["7.1"]["note"] == "held for feat/x", "the branch is recorded unasked"


async def test_one_repos_numbers_do_not_leak_into_anothers(client):
    """The key is `<repo>:<version>` and the allocator reads by prefix, so a repo
    whose name is a prefix of another's must not inherit its series."""
    await alloc(client, repo="acme/proj", after="4.0")
    other = await alloc(client, repo="acme/proj-extra")
    assert other["version"] == "0.1"


async def test_a_release_claim_is_visible_as_an_ordinary_claim(client):
    """One table, two kinds — so `GET /claims` sees both and nobody has to learn
    a second vocabulary to find out what is held."""
    await alloc(client, repo="acme/onetable", after="1.0", session="s-9")
    r = await client.get("/claims", params={"kind": "release"}, headers=LAPTOP)
    keys = [c["key"] for c in r.json()["claims"]]
    assert "acme/onetable:1.1" in keys


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


async def test_racing_allocators_never_hand_out_the_same_number(client):
    """#46's failure mode, reproduced as the race it actually was: two agents
    that both read `main`, both computed the next free number and were both
    correct. Four concurrent callers, four distinct numbers, no coordination
    between them beyond the table."""
    import asyncio
    repo = "acme/racerepo"
    results = await asyncio.gather(*[
        client.post("/release/claim", json={"repo": repo, "after": "3.0"},
                    headers=h)
        for h in (LAPTOP, DESKTOP, LAPTOP, DESKTOP)
    ])
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    versions = [r.json()["version"] for r in results]
    assert len(set(versions)) == len(versions), f"a number was issued twice: {versions}"


async def test_two_agents_on_ONE_machine_still_get_different_numbers(client):
    """The bug the concurrent test above found, pinned as its own case.

    `POST /claim` treats a same-machine re-claim as a renew, because a merge
    claim belongs to the box and an agent recovering from a restart must be able
    to pick its own back up. Borrowing that rule here issued one number twice:
    this fleet runs several agents per machine, all authenticating as that
    machine, and for a release number they are two BRANCHES rather than one agent
    twice. That population is the whole reason the allocator exists."""
    a = await alloc(client, repo="acme/onebox", after="6.0")
    b = await alloc(client, repo="acme/onebox", after="6.0")   # same machine
    assert a["version"] == "6.1" and b["version"] == "6.2"


async def test_a_retrying_caller_gets_its_OWN_number_back(client):
    """...and the idempotency that rule was providing is keyed on the session
    instead, so a caller whose request timed out does not spend a second number
    while its co-tenant on the same box still cannot take its one."""
    first = await alloc(client, repo="acme/retry", after="8.0", session="s-retry")
    again = await alloc(client, repo="acme/retry", after="8.0", session="s-retry")
    assert again["version"] == first["version"] == "8.1"
    assert again["renewed"] is True
    assert again["claim_id"] == first["claim_id"]

    other = await alloc(client, repo="acme/retry", after="8.0", session="s-other")
    assert other["version"] == "8.2"
