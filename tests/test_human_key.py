"""A person's own key — the second way to satisfy `human()`, and the one that does
not go through Authelia (#477).

`human()` had exactly one method: the edge injects `HUMAN_EDGE_SECRET` as
`X-Edge-Auth` beside `Remote-User`, and a request without it is not a person no
matter what it calls itself. That argument is sound and none of it is weakened
here — a header alone is still not proof, because `Remote-User` is forgeable by
anything that can reach the port.

What is added is a **method**, not a loosening: a static `name:secret` the caller
has to HOLD, presented to the agent vhost. It exists because the alternative for a
terminal was a browser session, and a session expires on a wall clock — so a
dashboard depending on one goes dead whenever it lapses and stays dead until
somebody re-mints it by hand. A key rotates when somebody decides to rotate it.

**The residual is who holds it**, and that has an honest answer rather than a
reassuring one (#479): the key sits on a workstation, readable by the processes
running there, so an agent that goes looking can find it and author as a person.
Accepted deliberately, bounded by being per person and revocable in one line, and
strictly narrower than the SSO session it replaced.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.db import async_session
from app.models.dial import DialSetting

from .conftest import LAPTOP, PINNED_SETTINGS

REPO = "prisonblues/quarterback"
KEY_HEADER = "X-Human-Key"


@pytest.fixture(autouse=True)
async def _empty_board(client):
    """No dial survives a test here, in either direction.

    The suite rebuilds the schema once per session, so a dial set by one test is
    still in force in the next — and, unless this also cleans up on the way OUT,
    in the next FILE. That is not hypothetical: the last test below sets a FLEET
    dial, a fleet dial is returned by every repo-scoped read, and
    `test_repo_identity`'s `test_one_dial_cannot_hold_two_live_values…` asserts on
    exactly such a read. It failed with one extra row the moment this file was
    added, and passed alone, which is the signature of a leak rather than a bug.
    """
    async with async_session() as s:
        await s.execute(delete(DialSetting))
        await s.commit()
    yield
    async with async_session() as s:
        await s.execute(delete(DialSetting))
        await s.commit()


@pytest.fixture
def human_key(monkeypatch):
    """One person's key, for the tests that need the door to open.

    Deliberately NOT in `PINNED_SETTINGS`: the default state of this suite is a
    board with no human keys at all, so "unconfigured refuses everything" is what
    a test gets for free rather than something it has to arrange.
    """
    key = "test-human-key-not-a-real-secret"
    monkeypatch.setattr(settings, "human_tokens", f"rich:{key}")
    return key


def keyed(key: str) -> dict:
    """A request that carries a person's key and an ordinary machine bearer.

    Both, because that is what the dashboard sends: the key answers "which
    person", the bearer answers "from where".
    """
    return {**LAPTOP, KEY_HEADER: key}


async def test_the_key_authors_as_the_person_it_names(client, human_key):
    """`rich:<secret>` writes as `human/rich` — the same identity the edge would
    have produced, because it is the same person by a different door."""
    r = await client.post("/dials", json={
        "dial": "tempo", "value": "eager", "reason": "set from the dashboard",
        "repo": REPO}, headers=keyed(human_key))
    assert r.status_code == 200, r.text
    assert r.json()["dial"]["set_by"] == "human/rich"


async def test_a_bearer_alone_is_still_refused(client):
    """The gate has not moved. An agent's token is what it always was here, and
    the refusal names the ways in rather than only saying no."""
    r = await client.post("/dials", json={
        "dial": "tempo", "value": "eager", "reason": "an agent trying it on"},
        headers=LAPTOP)
    assert r.status_code == 403
    assert KEY_HEADER in r.json()["detail"]


async def test_a_key_that_matches_nobody_says_which_thing_to_check(client, human_key):
    """A wrong key and a browser problem want different things done about them, so
    the refusal must not be the one about browsers."""
    r = await client.post("/dials", json={
        "dial": "tempo", "value": "eager", "reason": "wrong key"},
        headers=keyed("not-the-key"))
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert "HUMAN_TOKENS" in detail and "does not match any configured person" in detail


async def test_an_empty_key_header_does_not_authenticate_anybody(client, human_key):
    """The rule `_edge_asserted` keeps, kept here too: an unconfigured or blank
    credential fails closed. Compared with `compare_digest` against non-empty keys
    only, so `X-Human-Key: ''` can never match a person whose key is unset."""
    r = await client.post("/dials", json={
        "dial": "tempo", "value": "eager", "reason": "blank"},
        headers=keyed(""))
    assert r.status_code == 403


async def test_a_board_with_no_human_keys_refuses_every_key(client):
    """Unconfigured is closed, not open — the same failure mode as an unset
    `HUMAN_EDGE_SECRET`. This deployment has none, so nothing is a person."""
    r = await client.post("/dials", json={
        "dial": "tempo", "value": "eager", "reason": "no keys configured"},
        headers=keyed("anything-at-all"))
    assert r.status_code == 403


async def test_the_key_reaches_every_human_endpoint_and_not_more(client, human_key):
    """It authorises a PERSON, so it opens what a person opens — that is the whole
    design, and `/dials/clear` is the second endpoint the dashboard needs.

    What it does NOT do is become a TIER that outranks `human()`. It is a second
    method on one gate, so it opens exactly what a person opens and nothing a
    person cannot — see the test below for the one endpoint that reading changed
    the answer for in #591, and why that was a gap rather than a widening.
    """
    await client.post("/dials", json={
        "dial": "tempo", "value": "eager", "reason": "to be cleared",
        "repo": REPO}, headers=keyed(human_key))
    r = await client.post("/dials/clear", json={"dial": "tempo", "repo": REPO},
                          headers=keyed(human_key))
    assert r.status_code == 200, r.text
    assert [d["dial"] for d in r.json()["cleared"]] == ["tempo"]


async def test_the_key_reaches_the_delegated_endpoints_too_as_a_person(client,
                                                                        human_key):
    """#591. `delegated()` had only ever honoured the EDGE-proved person, so the
    two gates disagreed about who a person is: Rich with a browser could reorder
    the plan and Rich at a terminal, holding the very key `human()` accepts, could
    not. Nothing wanted that — the browser was the only door anyone had walked
    through when `delegated()` was written.

    It is a GAP being closed, not the credential widening: the key still authorises
    a person, and what it reaches is what that person already reached from a
    browser. The proof that it is not a relabelling is the rank source — a person
    writes `ordered`, and an agent holding the machine secret still writes
    `derived`.
    """
    seeded = []
    for title in ("keyed-a", "keyed-b"):
        r = await client.post("/plan/item", json={"repo": REPO, "title": title},
                              headers=LAPTOP)
        assert r.status_code in (200, 201), r.text
        seeded.append(r.json()["item_id"])
    a, b = seeded
    try:
        r = await client.post("/plan/reorder",
                              json={"repo": REPO, "order": [b, a]},
                              headers=keyed(human_key))
        assert r.status_code == 200, r.text
        assert r.json()["by"] == "human/rich", r.json()["by"]

        got = await client.get("/plan", params={"repo": REPO}, headers=LAPTOP)
        rows = {i["item_id"]: i for i in got.json()["items"]}
        assert rows[b]["rank_source"] == "ordered", rows[b]
    finally:
        for item_id in seeded:
            await client.post("/plan/item/done", json={"item_id": item_id},
                              headers=LAPTOP)


async def test_a_stale_key_does_not_suppress_the_dev_bypass(client, human_key,
                                                            monkeypatch):
    """`delegated()` must not become non-monotonic: adding a header that proves
    nothing must never turn a request that would have succeeded into a refusal.

    An earlier draft of #591 raised on a wrong `X-Human-Key` before consulting
    either the bearer or the bypass, so on a dev board the SAME request succeeded
    without the header and failed with it. Caught by an adversarial review.
    """
    monkeypatch.setattr(settings, "browser_dev_human", True)
    monkeypatch.setattr(settings, "browser_dev_user", "devuser")
    r = await client.post("/dials", json={
        "dial": "tempo", "value": "eager", "reason": "local dev", "repo": REPO},
        headers={**LAPTOP, KEY_HEADER: "nope-not-a-real-key"})
    assert r.status_code == 200, r.text
    assert r.json()["dial"]["set_via"] == "dev"


async def test_a_stale_key_beside_a_bearer_names_the_credential_actually_missing(
        client, human_key):
    """An agent holding a good bearer and no elevated secret was being told to check
    `HUMAN_TOKENS` — the one credential that was not its problem. The refusal now
    names `X-Agent-Elevated` as the thing this call wants from a machine, and still
    mentions that the key it sent matched nobody, because both are true."""
    r = await client.post("/plan/reorder", json={"repo": REPO, "order": []},
                          headers={**LAPTOP, KEY_HEADER: "nope-not-a-real-key"})
    assert r.status_code == 403, r.text
    assert "X-Agent-Elevated" in r.json()["detail"]


async def test_a_stale_key_alone_still_says_to_check_human_tokens(client, human_key):
    """When the key IS the only credential offered, it is the answer, and the
    specific message is the useful one. The fix for the two tests above must not
    cost this."""
    r = await client.post("/dials", json={
        "dial": "tempo", "value": "eager", "reason": "x", "repo": REPO},
        headers={KEY_HEADER: "nope-not-a-real-key"})
    assert r.status_code == 403, r.text
    assert "HUMAN_TOKENS" in r.json()["detail"]


async def test_the_edge_still_comes_first_and_is_unchanged(client, human_key):
    """A real browser write is never adjudicated against a key. The edge path is
    tried first and returns the person it vouched for, exactly as before."""
    edge = {"Remote-User": "rich",
            "X-Edge-Auth": PINNED_SETTINGS["HUMAN_EDGE_SECRET"]}
    r = await client.post("/dials", json={
        "dial": "review_panel.max_rounds", "value": 2, "reason": "from a browser"},
        headers=edge)
    assert r.status_code == 200, r.text
    assert r.json()["dial"]["set_by"] == "human/rich"


# ---- and HOW they proved it, recorded beside who they were ---------------------


async def test_a_keyed_write_records_the_method_beside_the_identity(client, human_key):
    """Same author, different event. `human/rich` either way — a person is one
    author however they arrived — but the key sits on a workstation where anything
    running as that user can read it (#479), and a row that recorded only `set_by`
    could not tell that write from a browser's afterwards."""
    r = await client.post("/dials", json={
        "dial": "tempo", "value": "eager", "reason": "from the dashboard",
        "repo": REPO}, headers=keyed(human_key))
    assert r.json()["dial"]["set_by"] == "human/rich"
    assert r.json()["dial"]["set_via"] == "key"


async def test_an_edge_write_records_the_edge(client, human_key):
    """The method the key did NOT use, so the column distinguishes rather than
    merely being populated."""
    edge = {"Remote-User": "rich",
            "X-Edge-Auth": PINNED_SETTINGS["HUMAN_EDGE_SECRET"]}
    r = await client.post("/dials", json={
        "dial": "tempo", "value": "held", "reason": "from a browser", "repo": REPO},
        headers=edge)
    assert r.json()["dial"]["set_via"] == "edge"


async def test_the_method_survives_to_the_read(client, human_key):
    """It is on `GET /dials`, not only in the write's own answer — the reader
    deciding how much weight to put on a dial is looking at the list, not at the
    response somebody else got."""
    await client.post("/dials", json={
        "dial": "tempo", "value": "eager", "reason": "keyed", "repo": REPO},
        headers=keyed(human_key))
    live = (await client.get("/dials", params={"repo": REPO},
                             headers=LAPTOP)).json()["dials"]
    assert [(d["dial"], d["set_via"]) for d in live] == [("tempo", "key")]


async def test_replacing_a_dial_records_how_the_replacement_was_made(client, human_key):
    """The row that is cleared keeps a `cleared_via` as well, because `cleared_by`
    exists so the history of a dial's moves survives — and half a record of a move
    is an odd place to stop."""
    await client.post("/dials", json={
        "dial": "tempo", "value": "eager", "reason": "first", "repo": REPO},
        headers=keyed(human_key))
    r = await client.post("/dials", json={
        "dial": "tempo", "value": "held", "reason": "second", "repo": REPO},
        headers=keyed(human_key))
    assert r.status_code == 200, r.text
    assert [d["value"] for d in r.json()["replaced"]] == ["eager"]

    async with async_session() as s:
        rows = (await s.execute(
            select(DialSetting).where(DialSetting.cleared_at.is_not(None)))).scalars().all()
    assert [row.cleared_via for row in rows] == ["key"]


async def test_a_row_older_than_the_column_says_nothing_rather_than_guessing(client):
    """`null` is "not recorded", never "some other method". A back-filled guess
    would put the one value a reader must be able to distrust into the column they
    consult to decide whether to trust the row."""
    async with async_session() as s:
        s.add(DialSetting(repo=REPO, dial="tempo", value={"value": "eager"},
                          reason="written before the column existed",
                          set_by="human/rich"))
        await s.commit()
    live = (await client.get("/dials", params={"repo": REPO},
                             headers=LAPTOP)).json()["dials"]
    assert [(d["dial"], d["set_via"]) for d in live] == [("tempo", None)]


async def test_a_real_key_is_not_recorded_as_the_dev_bypass(client, human_key, monkeypatch):
    """The trap `delegated()` was bitten by, on #480, and this function's old order
    walked straight into it.

    `_dev_person` answers for ANY caller when `BROWSER_DEV_HUMAN` is on — that is
    what a bypass is. Consulted before the key it shadows it on exactly the boards
    where the key is developed: a wrong key authenticates, and a RIGHT one is
    recorded as `dev`, which is a falsehood in the column added to prevent
    falsehoods. So the credential is tried first and the bypass is the last resort,
    which is `author()`'s order and `_dev_person`'s own stated rule.
    """
    monkeypatch.setattr(settings, "browser_dev_human", True)
    monkeypatch.setattr(settings, "browser_dev_user", "devuser")
    r = await client.post("/dials", json={
        "dial": "tempo", "value": "eager", "reason": "keyed on a dev board",
        "repo": REPO}, headers=keyed(human_key))
    assert r.status_code == 200, r.text
    assert r.json()["dial"]["set_via"] == "key", "the bypass shadowed a real key"
    assert r.json()["dial"]["set_by"] == "human/rich"


async def test_the_bypass_still_works_when_there_is_no_credential(client, monkeypatch):
    """Last resort, not removed. A local board with `BROWSER_DEV_HUMAN` on is still
    writable with no key at all — that is what the flag is for — and it records
    `dev`, so the row says which of the three doors was used."""
    monkeypatch.setattr(settings, "browser_dev_human", True)
    monkeypatch.setattr(settings, "browser_dev_user", "devuser")
    r = await client.post("/dials", json={
        "dial": "tempo", "value": "eager", "reason": "local dev", "repo": REPO},
        headers=LAPTOP)
    assert r.status_code == 200, r.text
    assert r.json()["dial"]["set_via"] == "dev"
