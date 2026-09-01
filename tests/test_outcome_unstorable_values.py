"""#646, the sibling write path: a NUL in one outcome took the whole batch down.

``POST /review/outcomes`` has the same defect and a different remedy, and the
difference is stated in ``_outcome_reason``: the panel must never fail a review
because the board was fussy, so ingest takes what it can read — but a fixer
recording an outcome has no such constraint and can simply be told. So a NUL is
**refused** here, per ITEM, and never marked.

The two values it would be worst to mark are exactly the ones this endpoint
carries. ``key`` is a defect identity matched with ``==``, so a marked key names
no finding at all; ``note`` is the evidence behind a refutation, and a board that
silently edits the evidence for a contradiction of its own judge has no business
publishing the precision figure that rests on it.

Before this, one bad byte in one ``note`` raised
``asyncpg.exceptions.CharacterNotInRepertoireError: invalid byte sequence for
encoding "UTF8": 0x00`` at the INSERT and the request 500ed — losing the eleven
good rows the endpoint's whole design promises to keep.
"""

from __future__ import annotations

from .conftest import LAPTOP

REPO = "acme/outcomes646"
AGENT = {**LAPTOP, "X-Agent-Instance": "u646o1"}

NUL = "a\x00b"


async def seed(client, pr: int) -> int:
    r = await client.post("/review", headers=AGENT, json={
        "repo": REPO, "pr": pr, "reviewed": True, "judged": True,
        "to_fix": [{"severity": "P2", "title": "one", "key": "k1"},
                   {"severity": "P2", "title": "two", "key": "k2"}]})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def stored_note(client, run_id: int, key: str) -> str | None:
    """The note on the outcome of one finding, off the run that raised it."""
    r = await client.get(f"/review/{run_id}", headers=AGENT)
    assert r.status_code == 200, r.text
    f = next(f for f in r.json()["findings"] if f["key"] == key)
    return None if f["outcome"] is None else f["outcome"]["note"]


async def send(client, pr: int, *items) -> dict:
    r = await client.post("/review/outcomes", headers=AGENT, json={
        "repo": REPO, "pr": pr, "outcomes": list(items)})
    # 422 when nothing at all was recorded — this endpoint's existing rule, and
    # two of the cases below deliberately send one bad item and nothing else.
    assert r.status_code in (200, 201, 422), r.text
    return r.json()


async def test_a_nul_in_a_note_costs_that_item_and_not_the_batch(client):
    """The guarantee this endpoint makes, held against the 500 that broke it.

    The good sibling records; the bad item is rejected by key with a reason that
    names the field. Nothing about this is silent, which is the same rule
    ``POST /review`` holds to by a different means.
    """
    await seed(client, 1)
    body = await send(client, 1,
                      {"key": "k1", "outcome": "fixed", "note": NUL},
                      {"key": "k2", "outcome": "fixed"})
    assert body["recorded"] == ["k2"]
    assert [r["key"] for r in body["rejected"]] == ["k1"]
    assert "NUL in note" in body["rejected"][0]["reason"]


async def test_the_rejection_names_every_field_that_held_one(client):
    """Named field by field, on ``_bounds``' rule: a caller told only that
    *something* held a NUL has to go and diff its own payload.

    The names come first in the message because each rendered item error is
    bounded at ``MAX_BUCKET_ECHO``, and it is the explanation that can be spared.
    """
    await seed(client, 2)
    body = await send(client, 2,
                      {"key": "k1", "outcome": "deferred", "deferred_to": NUL,
                       "note": NUL})
    reason = body["rejected"][0]["reason"]
    assert "note" in reason and "deferred_to" in reason


async def test_a_nul_in_a_key_is_refused_rather_than_marked(client):
    """A marked key would be a defect identity that matches nothing.

    Refusal is the honest answer: the caller is told, and no outcome row is
    written against an identity no finding has.
    """
    await seed(client, 3)
    body = await send(client, 3, {"key": NUL, "outcome": "fixed"})
    assert body["recorded"] == []
    assert "NUL in key" in body["rejected"][0]["reason"]


async def test_a_nul_in_the_batch_session_is_a_422_and_not_a_500(client):
    """The request's own field, so there is no item to reject it with.

    ``session`` is a contact address a peer is meant to reach the recorder on, and
    a marked one resolves to nothing while looking like the real thing — the same
    argument the over-length refusal beside it already makes. Refused as a 422,
    which is what the over-length case has always done.
    """
    await seed(client, 4)
    r = await client.post("/review/outcomes", headers=AGENT, json={
        "repo": REPO, "pr": 4, "session": NUL,
        "outcomes": [{"key": "k1", "outcome": "fixed"}]})
    assert r.status_code == 422, r.text
    assert "NUL" in r.text


async def test_a_non_finite_pr_number_is_refused_rather_than_500ing(client):
    """Nothing here stores a float, so this is about the REFUSAL.

    ``pr`` is a plain ``int``; a ``NaN`` fails validation, and FastAPI renders that
    422 by quoting the offending input back into JSON, which cannot represent one.
    The refusal itself then raised ``ValueError: Out of range float values are not
    JSON compliant: nan`` and the request 500ed having never reached the database.
    """
    r = await client.post(
        "/review/outcomes",
        content=b'{"repo": "acme/outcomes646", "pr": NaN, '
                b'"outcomes": [{"key": "k1", "outcome": "fixed"}]}',
        headers={**AGENT, "content-type": "application/json"})
    assert r.status_code == 422, r.text


async def test_an_ordinary_batch_still_records(client):
    """The negative half: the refusal must not fire on a payload nobody objected
    to, or every honest fix pass loses its outcomes."""
    await seed(client, 5)
    body = await send(client, 5, {"key": "k1", "outcome": "fixed", "note": "done"})
    assert body["recorded"] == ["k1"] and body["rejected"] == []


async def test_a_non_finite_note_is_rejected_and_never_read_as_a_clear(client):
    """The trap in fixing the ``pr`` 500 by walking the whole body.

    ``note: null`` is an explicit CLEAR here — ``model_fields_set`` is what tells a
    retraction from an absent key — so a pass that nulled ``note: NaN`` on its way
    past would turn a garbled value into a deliberate erasure of evidence already
    on the row. The request-level normalisation stops at the request's own keys;
    the item fails ``OutcomeIn`` and becomes that item's rejection, and the stored
    note is still there afterwards.
    """
    run_id = await seed(client, 6)
    first = await send(client, 6, {"key": "k1", "outcome": "fixed",
                                   "note": "the evidence"})
    assert first["recorded"] == ["k1"]
    r = await client.post(
        "/review/outcomes",
        content=b'{"repo": "acme/outcomes646", "pr": 6, "outcomes": '
                b'[{"key": "k1", "outcome": "fixed", "note": NaN}]}',
        headers={**AGENT, "content-type": "application/json"})
    assert r.status_code == 422, r.text
    assert "note" in r.json()["rejected"][0]["reason"]
    assert await stored_note(client, run_id, "k1") == "the evidence"
