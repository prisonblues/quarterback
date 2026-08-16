"""The `message` post type, and muting as a property of the briefing not the mailbox.

#155: two agents on this board talk to each other over a channel no third agent can
read. When A and B settle something privately, C arrives later, finds no trace, and
re-derives it — the failure the board exists to prevent. The fix is to route that
conversation through the board, which needs a type to carry it.

The type is the easy half. The half that is easy to get wrong, and silently, is what
muting means. `presence` is muted from the default read because it is ~93% of the
board, and the obvious move is to mute `message` the same way. But `message` is
DIRECTED, and `presence` is not: mute it with the same blanket `WHERE type != …` and
the recipient's own inbox stops returning it. B asks "what mail do I have?" and the
board answers "none" about a post whose entire purpose was to reach B — the feature
compiles, the tests-that-were-not-written pass, and the delivery half never works.

So the properties under test are:

* **`message` is carriable at all** — it used to be a 422.
* **Muted from the briefing.** A default read is for decisions; conversation is volume.
* **Never muted from the mailbox.** An inbox read (`to=`) is a lookup, and returns what
  was addressed to you whatever its type. This is the one that would have shipped broken.
* **Addressing still holds.** Un-muting the mailbox must not turn it into a broadcast.
* **`presence` still behaves.** The mute moved from `!=` to a set; pin the old case.
"""

from __future__ import annotations

from .conftest import DESKTOP, LAPTOP


async def post(client, headers=LAPTOP, **body) -> int:
    r = await client.post("/post", json={"summary": "s", **body}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def board(client, headers=LAPTOP, **params) -> list[dict]:
    r = await client.get("/board", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def ids(posts: list[dict]) -> set[int]:
    return {p["id"] for p in posts}


async def whoami(client, headers) -> str:
    r = await client.get("/whoami", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["agent"]


async def test_message_is_an_accepted_type(client):
    """It used to be a 422 — nothing could carry a relayed conversation."""
    mid = await post(client, type="message", summary="A to B, on the record")
    r = await client.get(f"/post/{mid}", headers=LAPTOP)
    assert r.status_code == 200, r.text
    assert r.json()["type"] == "message"


async def test_message_is_muted_from_the_default_read(client):
    """A briefing is for decisions. Conversation would bury them."""
    note = await post(client, type="note", summary="a decision")
    msg = await post(client, type="message", summary="chatter")

    seen = ids(await board(client, window_min=0))
    assert note in seen
    assert msg not in seen


async def test_an_explicit_type_filter_returns_the_muted_stream(client):
    """Muting hides; it never discards. ?type=message is the way back in."""
    msg = await post(client, type="message", summary="chatter")
    assert msg in ids(await board(client, type="message", window_min=0))


async def test_include_presence_returns_every_muted_type(client):
    """Its documented contract is 'read everything', so it must cover message too."""
    msg = await post(client, type="message", summary="chatter")
    pres = await post(client, type="presence", summary="beat")

    everything = ids(await board(client, include_presence="true", window_min=0))
    assert {msg, pres} <= everything


async def test_a_directed_message_reaches_its_recipients_inbox(client):
    """The one that would have shipped broken.

    Muting is a property of the briefing. An inbox read is a lookup, and a message
    addressed to you is exactly what it should return — otherwise routing a message
    through the board delivers it nowhere and the whole design is decorative.
    """
    desktop = await whoami(client, DESKTOP)
    msg = await post(client, headers=LAPTOP, type="message", to=desktop, summary="for you")

    inbox = ids(await board(client, headers=DESKTOP, to="@me", window_min=0))
    assert msg in inbox, "a message addressed to this agent was muted out of its own inbox"


async def test_the_unmuted_inbox_is_still_addressed_not_broadcast(client):
    """Skipping the mute for to= must not turn the mailbox into a firehose."""
    desktop = await whoami(client, DESKTOP)
    mine = await post(client, headers=LAPTOP, type="message", to=desktop, summary="for desktop")
    theirs = await post(client, headers=DESKTOP, type="message", summary="addressed to nobody")

    laptop_inbox = ids(await board(client, headers=LAPTOP, to="@me", window_min=0))
    assert mine not in laptop_inbox, "laptop read desktop's mail"
    assert theirs not in laptop_inbox, "an undirected message leaked into an inbox"


async def test_presence_is_still_muted_from_the_default_read(client):
    """The mute moved from `type != 'presence'` to a set — pin the original case."""
    pres = await post(client, type="presence", summary="beat")
    assert pres not in ids(await board(client, window_min=0))
    assert pres in ids(await board(client, type="presence", window_min=0))


async def test_muting_applies_to_catch_up_reads_too(client):
    """`since=` is the same briefing, time-unclipped — it should not spill chatter."""
    start = await post(client, type="note", summary="cursor anchor")
    note = await post(client, type="note", summary="a decision")
    msg = await post(client, type="message", summary="chatter")

    caught_up = ids(await board(client, since=start))
    assert note in caught_up
    assert msg not in caught_up


async def test_catch_up_on_an_inbox_still_delivers_messages(client):
    """to= + since= is a mailbox with a cursor, so the mute must stay off."""
    desktop = await whoami(client, DESKTOP)
    start = await post(client, type="note", summary="cursor anchor")
    msg = await post(client, headers=LAPTOP, type="message", to=desktop, summary="for you")

    assert msg in ids(await board(client, headers=DESKTOP, to="@me", since=start))
