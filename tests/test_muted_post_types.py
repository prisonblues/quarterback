"""The `message` post type, and muting as a property of the briefing not the lookup.

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
* **Never muted from the reader's own briefing either.** `since=` is ONE board-wide
  cursor. A briefing that hid your mail would still advance that cursor past it, and the
  `to=@me&since=<cursor>` read meant to fetch it asks only for what is newer — so the
  message is not delayed, it is gone. A briefing therefore never hides a post the same
  agent's inbox would return.
* **Never muted from a session's own record.** `session=` is a lookup too; dropping
  `message` there loses that session's half of every exchange it had. Only `presence`
  stays muted.
* **Addressing still holds.** Un-muting a lookup must not turn it into a broadcast, and
  un-muting your own mail must not un-mute everybody else's.
* **The exchange is findable by a third agent.** That is the entire point of #155.
* **`presence` still behaves.** The mute moved from `!=` to a list; pin the old case.
* **`include_presence` still works.** It is the deprecated spelling of `include_muted`.
"""

from __future__ import annotations

import pytest

from .conftest import DESKTOP, LAPTOP, SERVER

#: A *named* agent on desktop — `machine/name`, not the bare machine. The bare form is
#: also the broadcast address, so it exercises none of the addressing that matters:
#: hierarchy (mail to `desktop` lands here too) and the permanent key alias both need
#: an agent that has a name of its own.
NAMED = {**DESKTOP, "X-Agent-Key": "muted-types-agent"}

#: Fields POST /post accepts. Whitelisted so a typo (`typ=`, `summry=`) fails the test
#: instead of being quietly dropped by the request model and asserted about anyway.
_POST_FIELDS = frozenset({"type", "summary", "detail", "detail_ref", "re", "to", "session"})

#: Likewise for GET /board: an unknown query parameter is ignored by FastAPI, so
#: `windowmin=0` would silently read with the default 30-minute window.
_BOARD_PARAMS = frozenset(
    {"since", "window_min", "type", "to", "session", "include_muted", "include_presence", "limit"}
)


async def post(client, headers=LAPTOP, **body) -> int:
    unknown = set(body) - _POST_FIELDS
    assert not unknown, f"not a POST /post field: {sorted(unknown)}"
    r = await client.post("/post", json={"summary": "s", **body}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def board(client, headers=LAPTOP, **params) -> list[dict]:
    unknown = set(params) - _BOARD_PARAMS
    assert not unknown, f"not a GET /board parameter: {sorted(unknown)}"
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


async def test_include_muted_returns_every_muted_type(client):
    """Its documented contract is 'read everything', so it must cover message too."""
    msg = await post(client, type="message", summary="chatter")
    pres = await post(client, type="presence", summary="beat")

    assert {msg, pres} <= ids(await board(client, include_muted=True, window_min=0))


async def test_include_presence_still_works_as_the_deprecated_alias(client):
    """The flag was named before there was a second muted type.

    `include_muted` is the honest name now, but the old one is in the MCP tool, in the
    human board's own fetch, and in whatever else has been reading this board — so it
    keeps meaning exactly what it did, which is 'everything'.
    """
    msg = await post(client, type="message", summary="chatter")
    pres = await post(client, type="presence", summary="beat")

    assert {msg, pres} <= ids(await board(client, include_presence=True, window_min=0))


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


@pytest.mark.parametrize(
    "flags",
    [
        {},
        {"include_muted": False},
        {"include_muted": True},
        {"include_presence": False},
        {"include_presence": True},
    ],
    ids=["unset", "muted-false", "muted-true", "presence-false", "presence-true"],
)
async def test_an_inbox_read_ignores_the_include_flags(client, flags):
    """The include_* flags have no effect on to= — documented thrice, tested nowhere.

    The code satisfies it only incidentally, by the order of an if/elif — so a refactor
    could break a written contract with the whole suite still green.
    """
    desktop = await whoami(client, DESKTOP)
    msg = await post(client, headers=LAPTOP, type="message", to=desktop, summary="for you")

    inbox = ids(await board(client, headers=DESKTOP, to="@me", window_min=0, **flags))
    assert msg in inbox, f"an inbox read was muted by {flags}"


async def test_a_briefing_never_hides_the_readers_own_mail(client):
    """Delivery has to survive the *default* read, not just the inbox one.

    Nothing pushes a message at an agent — the transport half is blocked on #157 — so
    the briefing is where mail is actually seen. Everybody else's conversation stays
    muted; yours does not.
    """
    desktop = await whoami(client, DESKTOP)
    mine = await post(client, headers=LAPTOP, type="message", to=desktop, summary="for you")
    theirs = await post(client, headers=LAPTOP, type="message", summary="not for you")

    briefing = ids(await board(client, headers=DESKTOP, window_min=0))
    assert mine in briefing, "an agent's own mail was muted out of its own briefing"
    assert theirs not in briefing, "the carve-out un-muted somebody else's conversation"


async def test_a_single_cursor_cannot_lose_a_directed_message(client):
    """The data-loss shape, in the one cursor the docs tell you to keep.

    `since` is a single board-wide post id. Muting a message addressed to B out of B's
    briefing does not just delay it: the briefing still returns the *later* visible post,
    B saves that as its cursor, and `to=@me&since=<cursor>` then asks only for posts newer
    than the mail it was meant to deliver. The message is unreachable by the documented
    pattern, permanently.
    """
    desktop = await whoami(client, DESKTOP)
    msg = await post(client, headers=LAPTOP, type="message", to=desktop, summary="for you")
    note = await post(client, headers=LAPTOP, type="note", summary="a decision")

    briefing = ids(await board(client, headers=DESKTOP, window_min=0))
    assert note in briefing
    cursor = max(briefing)  # what board_read returns, and what the docstring says to save

    inbox = ids(await board(client, headers=DESKTOP, to="@me", since=cursor))
    assert msg in briefing | inbox, (
        "the briefing advanced the cursor past a message addressed to this agent, "
        "so the inbox read that cursor feeds can never reach it again"
    )


async def test_the_unmuted_inbox_is_still_addressed_not_broadcast(client):
    """Skipping the mute for to= must not turn the mailbox into a firehose."""
    desktop = await whoami(client, DESKTOP)
    mine = await post(client, headers=LAPTOP, type="message", to=desktop, summary="for desktop")
    theirs = await post(client, headers=DESKTOP, type="message", summary="addressed to nobody")

    laptop_inbox = ids(await board(client, headers=LAPTOP, to="@me", window_min=0))
    assert mine not in laptop_inbox, "laptop read desktop's mail"
    assert theirs not in laptop_inbox, "an undirected message leaked into an inbox"


async def test_a_message_to_a_whole_machine_reaches_each_agent_on_it(client):
    """Addressing is hierarchical, and the mute bypass keys on `to`, not on the form of it.

    A post to the bare machine is in every co-tenant's inbox, and that path goes through
    `inbox_clause` rather than an equality on the recipient — the likeliest place for the
    bypass to hold for `@me` and quietly fail for everything else.
    """
    agent = await whoami(client, NAMED)
    machine, _, name = agent.partition("/")
    assert name, "this test needs a named agent, not a bare machine"

    msg = await post(client, headers=LAPTOP, type="message", to=machine, summary="all of you")

    assert msg in ids(await board(client, headers=NAMED, to="@me", window_min=0))
    assert msg in ids(await board(client, headers=NAMED, window_min=0)), (
        "machine-addressed mail was muted out of the briefing of an agent on that machine"
    )


async def test_a_message_to_an_explicit_identity_is_readable_by_that_name(client):
    """`to=` takes any identity, not just @me — the form a third agent looks mail up with."""
    agent = await whoami(client, NAMED)
    msg = await post(client, headers=LAPTOP, type="message", to=agent, summary="by name")

    assert msg in ids(await board(client, headers=SERVER, to=agent, window_min=0))


async def test_type_message_and_an_explicit_recipient_compose(client):
    """The two ways back into muted traffic, used together: one type, one mailbox."""
    agent = await whoami(client, NAMED)
    mine = await post(client, headers=LAPTOP, type="message", to=agent, summary="for the agent")
    elsewhere = await post(client, headers=LAPTOP, type="message", to="server", summary="not")

    seen = ids(await board(client, headers=SERVER, type="message", to=agent, window_min=0))
    assert mine in seen
    assert elsewhere not in seen, "a type filter overrode the addressing"


async def test_a_third_agent_can_read_an_exchange_it_was_not_part_of(client):
    """#155 in one test: C, who was party to none of it, finds both sides and the thread.

    Every other inbox test reads as one of the two participants. This is the read the
    feature exists for — the one that fails today with a private channel, and the reason
    a `message` is on the board at all rather than muted into oblivion.
    """
    laptop, desktop = await whoami(client, LAPTOP), await whoami(client, DESKTOP)
    opener = await post(client, headers=LAPTOP, type="message", to=desktop, summary="did it land?")
    reply = await post(
        client, headers=DESKTOP, type="message", to=laptop, re=opener, summary="yes, on main"
    )

    by_type = {
        p["id"]: p for p in await board(client, headers=SERVER, type="message", window_min=0)
    }
    assert {opener, reply} <= set(by_type), "a third agent could not read the exchange"
    assert by_type[reply]["re"] == opener, "threading was lost, so the halves can't be paired"
    assert (by_type[opener]["from"], by_type[opener]["to"]) == (laptop, desktop)
    assert (by_type[reply]["from"], by_type[reply]["to"]) == (desktop, laptop)

    by_recipient = ids(await board(client, headers=SERVER, to=desktop, window_min=0))
    assert opener in by_recipient, "?to=<recipient> is the other way in, and it must work too"


async def test_a_session_read_keeps_that_sessions_messages(client):
    """`session=` replays one session's record, so it is a lookup, not a briefing.

    Muting `message` there drops that session's half of every exchange it had — the same
    silent loss as the inbox case, one indirection out. Heartbeats it still drops: a
    session's own presence beats are exactly the volume the mute exists for.
    """
    sid = "sess-muted-types"
    desktop = await whoami(client, DESKTOP)
    note = await post(client, type="note", summary="decided", session=sid)
    msg = await post(client, type="message", to=desktop, summary="the other half", session=sid)
    beat = await post(client, type="presence", summary="beat", session=sid)

    seen = ids(await board(client, session=sid, window_min=0))
    assert {note, msg} <= seen, "a session's own conversation was dropped from its record"
    assert beat not in seen, "a session replay does not want its own heartbeats"
    assert beat in ids(await board(client, session=sid, type="presence", window_min=0))


async def test_presence_is_still_muted_from_the_default_read(client):
    """The mute moved from `type != 'presence'` to a list — pin the original case."""
    pres = await post(client, type="presence", summary="beat")
    assert pres not in ids(await board(client, window_min=0))
    assert pres in ids(await board(client, type="presence", window_min=0))


async def test_muting_applies_to_catch_up_reads_too(client):
    """`since=` is the same briefing, time-unclipped — it should not spill chatter."""
    start = await post(client, type="note", summary="cursor anchor")
    note = await post(client, type="note", summary="a decision")
    msg = await post(client, headers=DESKTOP, type="message", summary="chatter")

    caught_up = ids(await board(client, since=start))
    assert note in caught_up
    assert msg not in caught_up


async def test_catch_up_on_an_inbox_still_delivers_messages(client):
    """to= + since= is a mailbox with a cursor, so the mute must stay off."""
    desktop = await whoami(client, DESKTOP)
    start = await post(client, type="note", summary="cursor anchor")
    msg = await post(client, headers=LAPTOP, type="message", to=desktop, summary="for you")

    assert msg in ids(await board(client, headers=DESKTOP, to="@me", since=start))
