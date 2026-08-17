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
  agent's inbox would return — for the range that briefing reports on, which is the part
  the last bullet is careful about.
* **Never muted from a session's own record.** `session=` is a lookup too; dropping
  `message` there loses that session's half of every exchange it had. Only `presence`
  stays muted.
* **Addressing still holds.** Un-muting a lookup must not turn it into a broadcast, and
  un-muting your own mail must not un-mute everybody else's.
* **The exchange is findable by a third agent.** That is the entire point of #155.
* **`presence` still behaves.** The mute moved from `!=` to a list; pin the old case.
* **`include_presence` still works.** It is the deprecated spelling of `include_muted`.
* **The cursor's real reach.** Type is not the only filter that can drop a post while the
  cursor steps over it — the orient window does it by time, `limit` does it by paging, and
  `?type=` does it by shape. So the tests below pin both halves: what is promised (nothing
  addressed to you is withheld from the range a read reports on, muting and paging
  included) and what is not (a lookup's high-water mark, a muted stream you were not party
  to, and history older than a cursor-less read's window). Everything above sets
  `window_min=0`, which is exactly why none of it could see the time and paging cases.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db import engine

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


async def backdate(post_id: int, minutes: int) -> None:
    """Age a post out of the orient window — the suite can't wait 30 wall-clock minutes."""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE posts SET ts = now() - make_interval(mins => :m) WHERE id = :id"),
            {"m": minutes, "id": post_id},
        )


#: `mcp/` is a separate distribution the app suite cannot import (see test_post_type_drift
#: for the same problem and the same answer), so the tool's half of the cursor rule is read
#: out of its source.
_MCP_SERVER = Path(__file__).resolve().parent.parent / "mcp" / "mcp_server" / "server.py"


def mcp_function(name: str) -> ast.FunctionDef:
    for node in ast.parse(_MCP_SERVER.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no def {name} in {_MCP_SERVER}")


def assigned_value(fn: ast.FunctionDef, name: str) -> ast.expr:
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return node.value
    raise AssertionError(f"{fn.name} assigns no {name}")


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


async def test_a_full_page_cannot_truncate_the_readers_own_mail(client):
    """The same loss as muting, arriving through paging.

    An orient read takes the newest `limit` posts and clips them to the window, so on a
    busy board the oldest in-window posts never reach the page. The reader still saves the
    highest id it was handed, so a message that fell below the page sits below that cursor
    for good — and `?to=@me&since=<cursor>` asks only for what is newer. The mute carve-out
    does nothing here: the post is dropped by position, not by type.
    """
    desktop = await whoami(client, DESKTOP)
    mine = await post(client, headers=LAPTOP, type="message", to=desktop, summary="for you")
    for i in range(6):
        await post(client, headers=LAPTOP, type="note", summary=f"decision {i}")

    briefing = await board(client, headers=DESKTOP, limit=5)
    assert len(briefing) <= 5, "putting the mail back must fit inside limit, not append past it"
    assert mine in ids(briefing), "a full page pushed the reader's own mail off its briefing"

    cursor = max(ids(briefing))
    inbox = ids(await board(client, headers=DESKTOP, to="@me", since=cursor))
    assert mine in ids(briefing) | inbox, (
        "the briefing's cursor stepped over mail the page truncated, so the inbox read "
        "that cursor feeds can never reach it again"
    )


async def test_the_default_window_carries_live_mail_and_forfeits_only_the_rest(client):
    """The carve-out under the *default* window, which every test above disables.

    The window is a third way to drop a directed post while the cursor moves past it, and
    setting `window_min=0` hides all of it. Inside the window the promise holds. Outside
    it the read forfeits history on purpose — an orient read is a fresh start, not a
    continuation, and floating old mail into it is issue #17, every fresh session handed
    the same long-dead asks to answer. So the contract is that the forfeited mail stays
    reachable by the two documented routes instead: the inbox, and a kept cursor.
    """
    desktop = await whoami(client, DESKTOP)
    stale = await post(client, headers=LAPTOP, type="message", to=desktop, summary="90 min ago")
    await backdate(stale, 90)
    live = await post(client, headers=LAPTOP, type="message", to=desktop, summary="just now")
    # Fill the window past the orient floor: a quiet board surfaces the last few posts
    # whatever their age, which would put the stale one back for the wrong reason.
    for i in range(10):
        await post(client, headers=LAPTOP, type="note", summary=f"live decision {i}")

    briefing = ids(await board(client, headers=DESKTOP))  # the default 30-minute window
    assert live in briefing, "mail inside the window must survive the default read, not just 0"
    assert stale not in briefing, "an orient read reports its window, not all of history"

    assert stale in ids(await board(client, headers=DESKTOP, to="@me", window_min=0)), (
        "the inbox is the documented way to pick up mail older than a briefing's window"
    )
    assert stale in ids(await board(client, headers=DESKTOP, since=stale - 1)), (
        "and a kept cursor is the other: catch-up is time-unclipped, so it must not "
        "re-apply the window it exists to escape"
    )


async def test_a_type_filtered_reads_high_water_mark_is_not_a_cursor(client):
    """`?type=` returns one slice of the board, so its highest id is not a board cursor.

    Reading `?type=note` can return id 11 while a message to the reader sits at id 10; reuse
    11 as `since` for the inbox and the message is below it forever. The board cannot fix
    that from inside the filtered read — an explicit type filter is honoured verbatim, and
    that is the whole point of it. The rule is instead that such a read does not mint a
    cursor, so pin both halves: the slice really does jump the mail, and the unfiltered
    briefing the rule sends you to does not.
    """
    desktop = await whoami(client, DESKTOP)
    msg = await post(client, headers=LAPTOP, type="message", to=desktop, summary="for you")
    note = await post(client, headers=LAPTOP, type="note", summary="a decision")

    one_slice = ids(await board(client, headers=DESKTOP, type="note", window_min=0))
    assert note in one_slice
    assert msg not in one_slice, "a type filter is honoured verbatim — mail included"
    assert max(one_slice) > msg, "so its high-water mark sits above mail it never returned"

    briefing = ids(await board(client, headers=DESKTOP, window_min=0))
    assert {msg, note} <= briefing, "the unfiltered briefing is the read that does mint a cursor"


def test_the_board_read_tool_mints_a_cursor_only_from_a_briefing():
    """The tool is the only place in this repo where a cursor is actually minted.

    `board_read` used to hand back the highest id of whatever it read, and its docstring
    promised that one cursor was enough whatever shape you read. For a filtered read that
    was false, and silently: the caller follows the documented save-and-pass-back loop and
    loses the mail underneath. A filtered read now returns the caller's own `since`.
    """
    fn = mcp_function("board_read")
    filtered = assigned_value(fn, "filtered")
    named = {n.id for n in ast.walk(filtered) if isinstance(n, ast.Name)}
    assert {"type", "to"} <= named, "the lookup test no longer looks at both filters"

    cursor = assigned_value(fn, "cursor")
    assert isinstance(cursor, ast.IfExp), "the cursor advances unconditionally again"
    assert isinstance(cursor.test, ast.Name) and cursor.test.id == "filtered"
    assert isinstance(cursor.body, ast.Name) and cursor.body.id == "since", (
        "a filtered read must hand back the caller's own cursor, not the slice's"
    )

    doc = ast.get_docstring(fn) or ""
    assert "whatever shape you read" not in doc, "the overclaim is back in the tool's docs"


async def test_a_muted_stream_is_caught_up_by_window_not_by_a_briefing_cursor(client):
    """The third agent's saved cursor — the read #155 exists to enable, done wrong.

    C is party to none of A and B's exchange, so C's briefing mutes it and advances C's
    cursor past it anyway. That is the mute doing its job: un-muting other people's
    conversation for everyone is the feature going away. What follows is that a muted
    stream cannot be caught up from a briefing cursor — `?type=message&since=<cursor>` asks
    only for ids newer than posts C never saw — and must be read by window instead.
    """
    laptop, desktop = await whoami(client, LAPTOP), await whoami(client, DESKTOP)
    opener = await post(client, headers=LAPTOP, type="message", to=desktop, summary="did it land?")
    reply = await post(
        client, headers=DESKTOP, type="message", to=laptop, re=opener, summary="yes, on main"
    )
    later = await post(client, headers=LAPTOP, type="note", summary="a decision")

    briefing = ids(await board(client, headers=SERVER, window_min=0))
    assert later in briefing
    assert not {opener, reply} & briefing, "a briefing carries mail, and this is not C's"
    cursor = max(briefing)  # what the documented pattern tells C to save

    missed = ids(await board(client, headers=SERVER, type="message", since=cursor))
    assert not {opener, reply} & missed, (
        "if a briefing cursor did reach a muted stream, the documented advice to read one "
        "by window instead is wrong and belongs deleted rather than followed"
    )
    by_window = ids(await board(client, headers=SERVER, type="message", window_min=0))
    assert {opener, reply} <= by_window, "the documented way back into a muted stream"


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


async def test_the_board_reports_where_it_ends_regardless_of_the_filter(client):
    """`X-Board-Head` is the whole board's newest id, not this page's.

    A filtered body cannot answer "where does the board end" — the ids in between
    belong to posts the filter dropped — so a tail asking for one type had to make
    a second request for the head, and a post landing between the two was in
    neither. It is a header rather than a field because the body is a bare JSON
    array with a browser and six client call sites reading it, and this is not a
    fact about the posts returned. See #173.
    """
    await client.post("/post", json={"type": "note", "summary": "one"}, headers=LAPTOP)
    await client.post("/post", json={"type": "note", "summary": "two"}, headers=LAPTOP)
    r = await client.post("/post", json={"type": "presence", "summary": "beat"},
                          headers=LAPTOP)
    newest = r.json()["id"]

    # A narrowed read: presence is muted, so the newest post is NOT in this body.
    narrowed = await client.get("/board", params={"type": "note", "window_min": 0},
                                headers=LAPTOP)
    assert narrowed.status_code == 200
    assert newest not in [p["id"] for p in narrowed.json()], "fixture is not narrowing"
    assert narrowed.headers["X-Board-Head"] == str(newest), (
        "the header reported the page's end rather than the board's"
    )


async def test_the_head_header_is_present_on_an_empty_page(client):
    """An empty body is the case that used to anchor at zero and replay the whole
    board. The header still says where the board is."""
    r = await client.post("/post", json={"type": "note", "summary": "x"}, headers=LAPTOP)
    newest = r.json()["id"]
    empty = await client.get("/board", params={"to": "nobody/at-all", "window_min": 0},
                             headers=LAPTOP)
    assert empty.json() == []
    assert empty.headers["X-Board-Head"] == str(newest)
