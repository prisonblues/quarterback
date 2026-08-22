"""A person is an author, and the browser board can write (issue #108).

Five open issues end their acceptance criteria at *a human decides* — #85, #86,
#78, #84, #63 — and each stops at a mechanism that did not exist. The board could
address a machine, one agent, and every agent on a box. It could not address me,
and I could not answer it: ``board.html`` shipped two GET calls and nothing else,
so an ``ask`` directed at a person was a post nobody was watching.

The properties under test are the ones that make a person a first-class author
rather than a special case bolted onto the agent path:

* **A person has an identity of their own**, ``human/<user>``, and it is a
  reserved namespace — no bearer token can authenticate into it, so an agent
  cannot author a post that reads as a person's however it dresses the request.
* **The boundary that already guarded the plan now guards the board**, unchanged:
  a ``Remote-User`` is not a person unless the edge vouched for it with the
  shared secret, and with no secret configured nobody is.
* **The read bypass is not a write path.** ``BROWSER_DEV_USER`` authenticates
  every browser read as a fixed name; #56's constraint is that such a bypass must
  never author anything, and reading is not deciding.
* **The dev *human* bypass never outranks a token.** It is ambient, so on a local
  board a rule that let it win would author every agent's post as a person's.
* **A person has an inbox**, addressed and answered the same way an agent's is —
  including the honest empty one, which is what a mailbox exists to be able to say.
* **A person posts no presence.** A browser tab left open all night is not
  somebody at a desk, and the board's liveness data is what makes a claim mean
  anything.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import settings
from app.schemas import POST_TYPES

from .conftest import LAPTOP, PINNED_SETTINGS, SERVER

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_PAGE = REPO_ROOT / "app/static/board.html"

#: A person, as the edge proves one: the identity header AND the secret only the
#: auth proxy knows. Both halves, every time — that is the whole boundary.
EDGE = {"Remote-User": "rich", "X-Edge-Auth": PINNED_SETTINGS["HUMAN_EDGE_SECRET"]}
#: The half of it any caller can send, and must never be believed on its own.
SPOOFED = {"Remote-User": "rich"}


def edge_as(user: str) -> dict[str, str]:
    return {"Remote-User": user, "X-Edge-Auth": PINNED_SETTINGS["HUMAN_EDGE_SECRET"]}


async def post(client, headers, **body):
    return await client.post("/post", json={"summary": "s", **body}, headers=headers)


async def get_post(client, post_id: int) -> dict:
    r = await client.get(f"/post/{post_id}", headers=LAPTOP)
    assert r.status_code == 200, r.text
    return r.json()


async def inbox(client, headers, **params) -> list[dict]:
    r = await client.get("/board", params={"to": "@me", "window_min": 0, **params},
                         headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------ the identity a person authors under


async def test_a_person_authors_under_the_human_namespace(client):
    """The load-bearing decision: ``from`` is ``human/rich``, not ``rich``.

    Every other author on this board is ``<machine>/<name>`` with the machine
    proved by a token. Recording a person as a bare name would make theirs the
    one identity that could not be told from a machine of the same name — and
    the point of giving a person an identity at all is that their decisions are
    attributable.
    """
    r = await post(client, EDGE, summary="I'll take the deploy myself")
    assert r.status_code == 200, r.text
    assert (await get_post(client, r.json()["id"]))["from"] == "human/rich"


async def test_the_edge_identity_is_case_folded_into_one_person(client):
    """Two spellings of one person would be two authors and two inboxes.

    Forward-auth proxies are not consistent about case, and the failure is silent:
    an ask addressed to ``human/rich`` would simply never appear in ``Rich``'s
    inbox, with nothing anywhere saying why.
    """
    r = await post(client, edge_as("Rich"), summary="same person, shouted")
    assert (await get_post(client, r.json()["id"]))["from"] == "human/rich"


@pytest.mark.parametrize("bad", ["rich/zeus", "a b", "-rich", "ri%ch", "x" * 65])
async def test_a_remote_user_that_cannot_be_an_identity_is_refused(client, bad):
    """A separator is the one that matters: identities split on the first ``/``.

    A ``Remote-User`` carrying one would mint ``human/rich/zeus`` and produce an
    identity that reads as a person addressing a machine. Refused rather than
    sanitised — quietly rewriting somebody's user name hands them an identity
    they never chose and cannot predict.
    """
    r = await post(client, edge_as(bad))
    assert r.status_code == 400
    assert "cannot be a board identity" in r.json()["detail"]


async def test_the_human_namespace_cannot_be_claimed_by_a_token(client, monkeypatch):
    """A machine called ``human`` in API_TOKENS would let every agent on that box
    post as a person. Refused where a token becomes a machine name, rather than
    trusted to stay unused — the reservation is what makes ``human/…`` proof."""
    monkeypatch.setattr(settings, "api_tokens",
                        PINNED_SETTINGS["API_TOKENS"] + ",human:tok-human")
    r = await post(client, {"Authorization": "Bearer tok-human"})
    assert r.status_code == 503
    assert "reserved namespace" in r.json()["detail"]
    # ...and it is refused on every path a token identifies a caller on, not just
    # this one: /whoami would otherwise hand the caller `human/<name>` to quote.
    assert (await client.get("/whoami",
                             headers={"Authorization": "Bearer tok-human"})).status_code == 503


async def test_whoami_answers_for_a_person_and_says_which_kind(client):
    """The browser board asks this to decide whether to show the composer at all,
    so "which kind of caller is this" has to be a field rather than an inference
    from the shape of a string."""
    me = (await client.get("/whoami", headers=EDGE)).json()
    assert me["agent"] == "human/rich"
    assert me["kind"] == "human"
    assert me["machine"] == "human" and me["name"] == "rich"
    # No key and no alias: a person's name is designated by whoever runs the
    # edge, not allocated by the board, and it never recycles — which is what
    # one identity per person (rather than per browser session) buys.
    assert me["key"] is None and me["alias"] is None

    agent = (await client.get("/whoami", headers={**LAPTOP, "X-Agent-Key": "k-agentkind"})).json()
    assert agent["kind"] == "agent" and agent["machine"] == "laptop"


# ------------------------------------------------------------- the negative paths


async def test_a_forged_remote_user_cannot_author_a_post(client):
    """The boundary the whole split rests on. `reader` accepts a bare
    ``Remote-User`` because a spoofed read buys a caller a board every enrolled
    agent can already read; authoring is a different question, and the answer
    names the missing secret so the operator knows what to configure."""
    before = len((await client.get("/board", params={"window_min": 0, "limit": 1000},
                                   headers=LAPTOP)).json())
    r = await post(client, SPOOFED, summary="not me")
    assert r.status_code == 403
    assert "not asserted by the edge" in r.json()["detail"]
    after = (await client.get("/board", params={"window_min": 0, "limit": 1000},
                              headers=LAPTOP)).json()
    assert len(after) == before, "a refused write must write nothing"


async def test_a_near_miss_edge_secret_is_a_miss(client):
    r = await post(client, {**SPOOFED, "X-Edge-Auth": "not-the-secret"})
    assert r.status_code == 403


async def test_an_agent_token_cannot_author_as_a_person(client):
    """The traffic shape that makes this reachable: a forward-auth bypass rule
    that skips API paths for bearer traffic is normal, and it is exactly what
    agents send. The post is not refused — an agent may post — but it is authored
    as the agent, and nothing about the request moved it into the person's
    namespace."""
    r = await post(client, {**LAPTOP, **SPOOFED, "X-Agent-Key": "k-forger"},
                   summary="posting as myself, whatever the header says")
    assert r.status_code == 200, r.text
    author = (await get_post(client, r.json()["id"]))["from"]
    assert author.startswith("laptop/") and not author.startswith("human/")


async def test_an_edge_proved_person_outranks_a_bearer_token(client):
    """Deliberate, and the same precedence :func:`app.auth.human` already used.

    The reference deployment never sends both — the browser vhost has no token
    and the agent vhost strips ``X-Edge-Auth`` — so this decides only the
    misconfigured case, and it decides it towards the credential the app can
    verify for itself rather than the one it takes on trust from the proxy.
    """
    r = await post(client, {**LAPTOP, **EDGE}, summary="from the browser, on a box with a token")
    assert (await get_post(client, r.json()["id"]))["from"] == "human/rich"


async def test_with_no_edge_secret_configured_nobody_is_a_person(client, monkeypatch):
    """Fail closed. An unconfigured board is one nobody can post to as a person,
    never one every agent on the network can."""
    monkeypatch.setattr(settings, "human_edge_secret", "")
    assert (await post(client, EDGE)).status_code == 403
    assert (await client.get("/whoami", headers=EDGE)).status_code == 403


async def test_a_person_does_not_need_the_agent_tokens_to_be_configured(client, monkeypatch):
    """The two credentials are unrelated, and coupling them makes the board
    refuse a properly proved person over a setting about agents.

    `API_TOKENS` decides who is an *agent*. A person is proved at the edge by a
    shared secret the app checks itself, so an empty token map has nothing to say
    about them — it must not take away their post, their `/whoami` or their
    inbox. The 503 that names the missing config is still there for the caller it
    is actually about.
    """
    monkeypatch.setattr(settings, "api_tokens", "")
    monkeypatch.setattr(settings, "api_tokens_file", "")
    r = await post(client, EDGE, summary="an edge with no agents behind it yet")
    assert r.status_code == 200, r.text
    assert (await client.get("/whoami", headers=EDGE)).json()["agent"] == "human/rich"
    assert (await client.get("/board", params={"to": "@me", "window_min": 0},
                             headers=EDGE)).status_code == 200

    # ...and the caller the 503 IS about still gets it.
    assert (await post(client, {})).status_code == 503


async def test_no_credentials_at_all_is_still_a_401(client):
    r = await post(client, {})
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate") == "Bearer"


async def test_the_read_bypass_does_not_become_a_write_path(client, monkeypatch):
    """`BROWSER_DEV_USER` authenticates every browser *read* as a fixed name.
    #56 wrote the constraint down for the settings page and this issue lands
    first, so the answer is owed here: it authors nothing."""
    monkeypatch.setattr(settings, "browser_dev_user", "devuser")
    assert (await client.get("/board")).status_code == 200, "still a reader"
    assert (await post(client, {})).status_code == 401, "a read bypass is not an author"


async def test_the_dev_human_bypass_authors_a_person_on_a_local_board(client, monkeypatch):
    """`BROWSER_DEV_HUMAN` is off by default and documented as local-only. It
    already grants the plan's human-only writes; a local board that could reorder
    the plan and not answer an ask would be its own bug."""
    monkeypatch.setattr(settings, "browser_dev_human", True)
    r = await post(client, {}, summary="local board, no edge in front of it")
    assert r.status_code == 200, r.text
    assert (await get_post(client, r.json()["id"]))["from"] == "human/dev"


async def test_the_dev_human_bypass_never_outranks_a_bearer_token(client, monkeypatch):
    """The one ordering rule that differs from the edge's, and the reason it
    does: this bypass is ambient. Letting it win would relabel every agent's post
    on a dev board as a person's — losing the distinction precisely where it is
    cheapest to test."""
    monkeypatch.setattr(settings, "browser_dev_human", True)
    r = await post(client, {**SERVER, "X-Agent-Key": "k-devbox"}, summary="an agent on a dev box")
    author = (await get_post(client, r.json()["id"]))["from"]
    assert author.startswith("server/") and not author.startswith("human/")


# ------------------------------------------------------ answering, and the inbox


async def test_a_person_reads_and_answers_an_ask_addressed_to_them(client):
    """End to end, and the reason the issue exists: an ``ask`` put to a person is
    now a question that can be answered from a browser rather than a post nobody
    was watching."""
    asked = await post(client, {**LAPTOP, "X-Agent-Key": "k-asker"}, type="ask",
                       to="human/rich", summary="P2 floor for the fortnight — yes or no?")
    ask_id = asked.json()["id"]
    asker = (await get_post(client, ask_id))["from"]

    mine = await inbox(client, EDGE)
    assert ask_id in [p["id"] for p in mine]

    answer = await post(client, EDGE, type="ack", re=ask_id, to=asker, summary="yes, P2")
    assert answer.status_code == 200, answer.text
    written = await get_post(client, answer.json()["id"])
    assert (written["from"], written["type"], written["re"]) == ("human/rich", "ack", ask_id)

    # ...and it lands in the asker's inbox, canonicalised to the agent's name.
    theirs = await inbox(client, {**LAPTOP, "X-Agent-Key": "k-asker"})
    assert answer.json()["id"] in [p["id"] for p in theirs]
    assert written["to"] == asker


async def test_an_inbox_is_zero_until_something_is_addressed_to_it(client):
    """Two claims, one flow, because the second is only worth anything given the
    first: a person's mailbox says **zero honestly** on a busy board, and then
    fills the two ways an identity can be addressed.

    Honest zero is not free — an *orient* read floors at the board's last few
    posts so a fresh session always learns who made the last call, and applying
    that to a mailbox is issue #17: every reader handed the same long-dead asks
    to answer. `to='human'` then reaches every person and `to='human/stranger'`
    exactly one, which took no new concept — hierarchical addressing is the
    argument for making a person an identity rather than a flag.
    """
    stranger = edge_as("stranger")
    assert await inbox(client, stranger) == []
    busy = (await client.get("/board", params={"window_min": 0, "limit": 100},
                             headers=LAPTOP)).json()
    assert busy, "an empty board would make the zero above trivially true"

    direct = (await post(client, {**LAPTOP, "X-Agent-Key": "k-direct"}, type="ask",
                         to="human/stranger", summary="you specifically")).json()["id"]
    broadcast = (await post(client, {**LAPTOP, "X-Agent-Key": "k-broadcast"}, type="ask",
                            to="human", summary="anyone at a desk?")).json()["id"]
    assert {direct, broadcast} <= {p["id"] for p in await inbox(client, stranger)}

    # ...and neither reaches an agent, which shares no part of that address.
    theirs = {p["id"] for p in await inbox(client, {**SERVER, "X-Agent-Key": "k-notaperson"})}
    assert not ({direct, broadcast} & theirs)


async def test_to_me_needs_an_identity_and_says_which_ones_count(client):
    """A bare `Remote-User` may read the board and still has no inbox: `@me`
    resolves to a *named* identity, so believing an unvouched-for header here
    would be a way to read any person's mail by claiming to be them."""
    r = await client.get("/board", params={"to": "@me"}, headers=SPOOFED)
    assert r.status_code == 400
    assert "Remote-User" in r.json()["detail"]


async def test_a_person_addressing_themselves_is_canonicalised_like_an_agent(client):
    """`to=@me` is resolved by the board on write, so history stores one spelling
    whichever form the sender used."""
    r = await post(client, EDGE, to="@me", summary="note to self")
    assert (await get_post(client, r.json()["id"]))["to"] == "human/rich"


async def test_a_person_posts_no_presence(client):
    """Decision 3 of the issue, enforced rather than left as a convention. A tab
    left open all night is not somebody at a desk, and presence is the stream an
    unattended loop reads to decide whether to escalate now or bank the question
    — so a person's heartbeat would be a lie in the one place it costs."""
    r = await post(client, EDGE, type="presence", summary="still here, honest")
    assert r.status_code == 422
    assert "posts no presence" in r.json()["detail"]
    # An agent's presence is untouched: this is about who is claiming to be live.
    assert (await post(client, {**SERVER, "X-Agent-Key": "k-alive"},
                       type="presence", summary="alive")).status_code == 200


async def test_a_persons_own_mail_survives_the_briefing_mute(client):
    """The carve-out in `_mute_clause` is keyed on "the reader's identity", and a
    person now has one — so a muted `message` addressed to them is not dropped
    from the briefing they orient on, exactly as it is not for an agent."""
    mid = (await post(client, {**LAPTOP, "X-Agent-Key": "k-msg"}, type="message",
                      to="human/rich", summary="quiet word")).json()["id"]
    briefing = (await client.get("/board", params={"window_min": 60, "limit": 1000},
                                 headers=EDGE)).json()
    assert mid in [p["id"] for p in briefing]


# ---------------------------------------------------- what the browser is handed


def test_the_browser_board_carries_no_bearer_token():
    """An acceptance criterion of the issue, and the whole reason the browser is
    authenticated at the edge: the page never holds a machine credential. There
    is no JS test runner here, so this greps the file that ships — the same
    crude-but-real guard `test_plan_page.py` uses on the plan page."""
    page = BOARD_PAGE.read_text(encoding="utf-8")
    assert "Bearer" not in page
    assert "Authorization" not in page
    for token in settings.token_map.values():
        assert token not in page


def test_the_compose_types_are_types_the_server_accepts():
    """The page offers a fixed set of post types. Every one has to be a type
    `POST /post` will take from a person, or the button is a 422 with a friendly
    label on it — and `presence` in particular is refused by the server, so it
    must not be offered."""
    page = BOARD_PAGE.read_text(encoding="utf-8")
    match = re.search(r"const HUMAN_TYPES = \[([^\]]*)\]", page)
    assert match, "the page's compose type list moved — this guard needs repointing"
    offered = set(re.findall(r'"([a-z]+)"', match.group(1)))
    assert offered, "no types parsed out of HUMAN_TYPES"
    assert offered <= POST_TYPES, f"the page offers types the server refuses: {offered - POST_TYPES}"
    assert "presence" not in offered


# ------------------------------------------------- what I have already said (from=)


async def test_from_me_returns_my_own_record_and_nobody_elses(client):
    """The mirror of ``to=``, and the reason the inbox needed it.

    An answer is addressed to whoever asked, so **nothing in my own mailbox can
    tell me which asks I have closed** — the browser board counted every ask ever
    sent to me as still open, on every reload, which is a number that teaches you
    to ignore it. `?from=@me` is what makes "no answer of yours on record" a fact
    rather than a guess.
    """
    theirs = (await post(client, {**SERVER, "X-Agent-Key": "k-notme"},
                         summary="somebody else's note")).json()["id"]
    ask = (await post(client, {**LAPTOP, "X-Agent-Key": "k-askedme"}, type="ask",
                      to="human/rich", summary="worth doing?")).json()["id"]
    answer = (await post(client, EDGE, type="ack", re=ask,
                         to="laptop", summary="yes")).json()["id"]

    r = await client.get("/board", params={"from": "@me", "window_min": 0, "limit": 200},
                         headers=EDGE)
    assert r.status_code == 200, r.text
    mine = r.json()
    ids = {p["id"] for p in mine}
    assert answer in ids and theirs not in ids and ask not in ids
    assert all(p["from"] == "human/rich" for p in mine)
    # ...and it carries the `re` the page needs to mark that ask answered.
    assert ask in {p["re"] for p in mine}


async def test_from_is_hierarchical_and_clipped_to_a_name_s_tenure(client):
    """`?from=<machine>` is everything the box wrote, exactly as `?to=` is
    everything sent to it. The clipping is the same one that stops an inbox
    inheriting a predecessor's mail: an author filter must not attribute a
    recycled name's earlier posts to whoever holds it now."""
    one = (await post(client, {**SERVER, "X-Agent-Key": "k-h1"}, summary="agent one")).json()["id"]
    two = (await post(client, {**SERVER, "X-Agent-Key": "k-h2"}, summary="agent two")).json()["id"]
    whole_box = {p["id"] for p in (await client.get(
        "/board", params={"from": "server", "window_min": 0, "limit": 500},
        headers=LAPTOP)).json()}
    assert {one, two} <= whole_box

    just_one = {p["id"] for p in (await client.get(
        "/board", params={"from": "@me", "window_min": 0, "limit": 500},
        headers={**SERVER, "X-Agent-Key": "k-h1"})).json()}
    assert one in just_one and two not in just_one


async def test_an_author_filter_never_climbs_to_the_machine_root(client):
    """Delivery climbs and authorship does not, and conflating them is a wrong
    answer rather than a wide one.

    A post addressed *to* ``server`` is in every co-tenant's inbox — that is what
    a broadcast means. A post **written** by bare ``server`` (a keyless caller on
    that box) is that caller's, and attributing it to ``server/<name>`` would make
    ``?from=@me`` return a co-tenant's work — and the browser inbox that reads it
    to decide which asks are answered would mark answered an ask nobody answered.
    """
    keyless = (await post(client, SERVER, summary="posted by hand, no key")).json()["id"]
    named = {p["id"] for p in (await client.get(
        "/board", params={"from": "@me", "window_min": 0, "limit": 500},
        headers={**SERVER, "X-Agent-Key": "k-noclimb"})).json()}
    assert keyless not in named

    # ...while asking for the machine itself still returns it, and everything
    # its agents wrote. Downward is unchanged; only the climb is gone.
    whole_box = {p["id"] for p in (await client.get(
        "/board", params={"from": "server", "window_min": 0, "limit": 500},
        headers=LAPTOP)).json()}
    assert keyless in whole_box


async def test_from_me_needs_an_identity_like_to_me_does(client):
    r = await client.get("/board", params={"from": "@me"}, headers=SPOOFED)
    assert r.status_code == 400
    assert "?from=@me" in r.json()["detail"]


async def test_an_author_lookup_drops_only_the_heartbeats(client):
    """A `from=` read is one author's own record — a lookup, not a briefing — so
    it keeps the conversation and drops the volume, exactly as `session=` does.
    Dropping `message` would lose that agent's half of every exchange it had."""
    headers = {**SERVER, "X-Agent-Key": "k-authorlookup"}
    beat = (await post(client, headers, type="presence", summary="alive")).json()["id"]
    word = (await post(client, headers, type="message", to="laptop",
                       summary="a quiet word")).json()["id"]
    got = {p["id"] for p in (await client.get(
        "/board", params={"from": "@me", "window_min": 0, "limit": 500},
        headers=headers)).json()}
    assert word in got and beat not in got
    # ...and the heartbeats are still reachable by naming the type.
    typed = {p["id"] for p in (await client.get(
        "/board", params={"from": "@me", "type": "presence", "window_min": 0, "limit": 500},
        headers=headers)).json()}
    assert beat in typed


async def test_an_author_lookup_is_not_floored_to_the_boards_last_posts(client):
    """The orient floor exists so a fresh session always learns who made the last
    call. Applying it to a lookup is issue #17: the answer to "what have I said"
    on a quiet board would be ten posts by other people."""
    quiet = await client.get("/board", params={"from": "human/nobody-at-all", "window_min": 0},
                             headers=LAPTOP)
    assert quiet.json() == []
