"""v2.12: the board designates agent names; the client's key is a permanent alias.

v2.9 had each client derive its own instance from a Claude Code environment
variable. Any other runtime set nothing, derived nothing, and collapsed to the
bare machine name — which is also the *broadcast* address, so such an agent was
indistinguishable from its co-tenants, unaddressable, and receiving all of their
mail. Adding one more variable to the `or` chain would have fixed one runtime and
left the next one broken the same way, silently.

So the client now sends only an opaque key and the board allocates the name.
These tests cover the four things that decide whether that helps or hurts:
allocation never collides (it can't — the server sees who is live), naming
happens on first contact so nothing is authored under a key, both forms address
the same agent, and exactly one of them appears in history.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import update

from app.db import async_session
from app.identity import (
    NAME_SPACE,
    NAME_TTL,
    SELF,
    WORDS,
    address_clause,
    addressed_to,
    allocate_name,
    name_at,
    name_probe,
    valid_name,
)
from app.models.agent_name import AgentName
from app.models.post import Post

from .conftest import DESKTOP, LAPTOP, SERVER

REPO = "v211repo"

CODEX = {**DESKTOP, "X-Agent-Key": "nonce-no-session-id"}   # a nonce: no session id anywhere
CLAUDE = {**DESKTOP, "X-Agent-Key": "key-session-prefix"}    # a session-id prefix
NAMED = {**LAPTOP, "X-Agent-Key": "deploybox", "X-Agent-Name": "deploy"}


async def whoami(client, headers) -> dict:
    return (await client.get("/whoami", headers=headers)).json()


# ---- the name space (pure) --------------------------------------------------

def test_word_list_is_distinct_and_sized_for_the_stated_space():
    assert len(WORDS) == 100 and len(set(WORDS)) == len(WORDS)
    assert NAME_SPACE == 9900  # 100 x 99 ordered distinct pairs


def test_every_index_yields_a_distinct_two_word_name():
    names = {name_at(i) for i in range(NAME_SPACE)}
    assert len(names) == NAME_SPACE
    for name in ("amber-otter", name_at(0), name_at(NAME_SPACE - 1)):
        first, _, second = name.partition("-")
        assert first != second and valid_name(name)


def test_probe_covers_the_whole_space_so_allocation_cannot_wedge():
    """Linear probing from a hashed start visits every name — which is why
    'pick one that is free' has no failure mode short of all 9,900 being live."""
    assert len(set(name_probe("some-key"))) == NAME_SPACE


def test_allocation_avoids_live_names_where_hashing_would_collide():
    taken = {name_at(i) for i in range(50)}
    assert allocate_name("k", taken) not in taken
    # Two different keys that hash to the same start still get different names,
    # because the second one sees the first as taken.
    first = allocate_name("alpha", set())
    assert allocate_name("beta", {first}) != first


def test_a_requested_name_is_honoured_when_free_and_disambiguated_when_not():
    assert allocate_name("k", set(), requested="deploy") == "deploy"
    fallback = allocate_name("k", {"deploy"}, requested="deploy")
    assert fallback != "deploy" and valid_name(fallback)


def test_addressing_matches_every_spelling_of_one_agent():
    assert addressed_to("zeus/amber-otter", "zeus/ed49425c", ("zeus/amber-otter",))
    assert addressed_to("zeus/ed49425c", "zeus/amber-otter", ("zeus/ed49425c",))
    assert addressed_to("zeus", "zeus/amber-otter", ("zeus/ed49425c",))
    assert not addressed_to("zeus/flint-raven", "zeus/amber-otter", ("zeus/ed49425c",))
    # The SQL form has to agree with the Python one, or the inbox read and the
    # inbox test disagree about the same post.
    clause = str(address_clause(Post.recipient, "zeus/amber-otter", ("zeus/ed49425c",)))
    assert "IN" in clause and "LIKE" in clause


# ---- allocation on first contact --------------------------------------------

async def test_a_runtime_that_exposes_nothing_still_gets_a_real_identity(client):
    """The bug: a non-Claude-Code runtime used to collapse to the bare machine
    name. A nonce is all it needs now, because the board does the naming."""
    me = await whoami(client, CODEX)
    assert me["machine"] == "desktop"
    assert me["name"] and me["name"] != "desktop"
    assert me["agent"] == f"desktop/{me['name']}"
    assert me["key"] == "nonce-no-session-id" and me["alias"] == "desktop/nonce-no-session-id"
    assert "-" in me["name"]  # a two-word designation, not the key echoed back


async def test_two_agents_on_one_machine_never_share_a_name(client):
    a, b = await whoami(client, CODEX), await whoami(client, CLAUDE)
    assert a["name"] != b["name"] and a["machine"] == b["machine"]


async def test_the_name_is_stable_across_calls(client):
    first = await whoami(client, CLAUDE)
    for _ in range(3):
        assert (await whoami(client, CLAUDE))["agent"] == first["agent"]


async def test_nothing_is_ever_authored_under_a_key(client):
    """Allocation is lazy but happens *before* the write, so there is no window
    of history written under the hex and no rename event to reconcile."""
    fresh = {**SERVER, "X-Agent-Key": "firstpost"}
    post_id = (await client.post(
        "/post", json={"summary": "my very first post"}, headers=fresh
    )).json()["id"]
    author = (await client.get(f"/post/{post_id}", headers=SERVER)).json()["from"]
    assert author == (await whoami(client, fresh))["agent"]
    assert author != "server/firstpost" and author != "server"


async def test_a_requested_name_survives_allocation(client):
    """`QUARTERBACK_INSTANCE=deploy` still works — as a request, not an override."""
    assert (await whoami(client, NAMED))["name"] == "deploy"


#: `qb-env`'s `qb_requested_name`, which is what actually fills `X-Agent-Name` on the
#: fleet. Imported the way `test_dials.py` imports `harness_rules`: by path, because
#: the harness is not a package and the skew this guards is between the two halves.
QB_ENV = Path(__file__).resolve().parent.parent / "harness" / "bin" / "qb-env"

#: Labels an operator might plausibly put in `QUARTERBACK_INSTANCE`, chosen for the
#: gap this test exists to close: `Deploy_1` and `seat.lexray~9` are perfectly good
#: *keys* — `KEY_RE` allows upper case, `.`, `_` and `~` — and none of them is a legal
#: *name*. Sending one raw is a 400, and both clients swallow a 400 in silence.
INSTANCE_LABELS = [
    "seat-3", "deploy-two", "Deploy_1", "seat.lexray~9", "UPPER",
    "-lead-and-trail-", "sea t 3", "café-3", "x" * 45, "9", "___", "",
]


def _requested_name(label: str) -> str:
    """What the shell clients would ask for, run for real rather than reimplemented."""
    env = {**os.environ, "QUARTERBACK_INSTANCE": label}
    got = subprocess.run(
        ["bash", "-c", 'set -uo pipefail; . "$1"; qb_requested_name', "_", str(QB_ENV)],
        capture_output=True, text=True, env=env, timeout=30, check=True,
    )
    return got.stdout


async def test_every_label_the_clients_may_send_is_a_name_this_board_takes(client):
    """The escape hatch's two halves are in two languages (#156).

    `qb-env` decides what `QUARTERBACK_INSTANCE=Deploy_1` is *asked* for and this
    module decides what a name may *be*, and until #156 nothing joined them up
    because no client sent the header at all. The clients cannot see a refusal —
    `qb-hook` is fail-open by contract and `qb`'s recorder exits 0 on a dead board —
    so a rule tightened on this side would silently unname every labelled agent on
    the fleet. That failure has to land here, in the suite that owns the rule.
    """
    for i, label in enumerate(INSTANCE_LABELS):
        asked = _requested_name(label)
        if not asked:
            continue  # nothing usable in the label: the client sends no header
        assert valid_name(asked), f"{label!r} -> {asked!r} is not a name"
        headers = {**DESKTOP, "X-Agent-Key": f"hatch{i}", "X-Agent-Name": asked}
        r = await client.get("/whoami", headers=headers)
        assert r.status_code == 200, (label, asked, r.text)
        assert r.json()["name"] == asked


async def test_a_malformed_requested_name_is_rejected_not_ignored(client):
    bad = {**SERVER, "X-Agent-Key": "k1", "X-Agent-Name": "Not A Name"}
    r = await client.get("/whoami", headers=bad)
    assert r.status_code == 400 and "X-Agent-Name" in r.json()["detail"]


async def test_the_legacy_instance_header_is_accepted_as_a_key(client):
    """Fleet clients ship from another repo, so the old spelling has to keep
    identifying the same agent — and identify it the *same* as the new one."""
    legacy = {**SERVER, "X-Agent-Instance": "sharedkey"}
    modern = {**SERVER, "X-Agent-Key": "sharedkey"}
    assert (await whoami(client, legacy))["agent"] == (await whoami(client, modern))["agent"]


async def test_a_blank_new_header_does_not_mask_the_legacy_one(client):
    """A proxy that injects an empty X-Agent-Key must not silently un-identify a
    client that named itself the old way — that is the collapse, reintroduced."""
    both = {**SERVER, "X-Agent-Key": "   ", "X-Agent-Instance": "notblank"}
    assert (await whoami(client, both))["key"] == "notblank"


async def test_concurrent_first_contact_allocates_exactly_one_name(client):
    """Two processes of one agent starting together must not become two agents."""
    headers = {**SERVER, "X-Agent-Key": "raceykey"}
    results = await asyncio.gather(*(whoami(client, headers) for _ in range(6)))
    assert len({r["agent"] for r in results}) == 1


async def test_distinct_keys_starting_together_get_distinct_names(client):
    batch = [{**DESKTOP, "X-Agent-Key": f"burst{i}"} for i in range(6)]
    names = {r["name"] for r in await asyncio.gather(*(whoami(client, h) for h in batch))}
    assert len(names) == len(batch)


# ---- both forms address; one form is recorded -------------------------------

async def test_a_post_addressed_by_key_is_recorded_under_the_name(client):
    target = await whoami(client, CLAUDE)
    post_id = (await client.post(
        "/post", json={"summary": "by key", "to": target["alias"]}, headers=NAMED
    )).json()["id"]
    # Canonicalised on write: history shows one spelling per agent, never two.
    assert (await client.get(f"/post/{post_id}", headers=SERVER)).json()["to"] == target["agent"]


async def test_both_forms_reach_the_same_inbox(client):
    target = await whoami(client, CLAUDE)
    by_name = (await client.post(
        "/post", json={"summary": "by name", "to": target["agent"]}, headers=NAMED
    )).json()["id"]
    by_key = (await client.post(
        "/post", json={"summary": "by key", "to": target["alias"]}, headers=NAMED
    )).json()["id"]

    for spelling in (target["agent"], target["alias"]):
        inbox = (await client.get("/board", params={"to": spelling}, headers=DESKTOP)).json()
        assert {by_name, by_key} <= {p["id"] for p in inbox}


async def test_an_agent_reads_its_own_inbox_without_knowing_its_name(client):
    """`to=@me` is the point: the board owns the name, so the caller can't spell
    it — the hook used to compose one locally and assume it was right."""
    target = await whoami(client, CLAUDE)
    mine = (await client.post(
        "/post", json={"summary": "direct", "to": target["agent"]}, headers=NAMED
    )).json()["id"]
    broadcast = (await client.post(
        "/post", json={"summary": "to the box", "to": "desktop"}, headers=NAMED
    )).json()["id"]
    theirs = (await client.post(
        "/post", json={"summary": "not for you", "to": "laptop"}, headers=NAMED
    )).json()["id"]

    inbox = {p["id"] for p in (
        await client.get("/board", params={"to": SELF}, headers=CLAUDE)
    ).json()}
    assert {mine, broadcast} <= inbox and theirs not in inbox


async def test_to_me_needs_a_bearer_token(client):
    r = await client.get("/board", params={"to": SELF}, headers={"Remote-User": "someone"})
    assert r.status_code == 400


async def test_active_holder_filter_accepts_either_spelling(client):
    me = await whoami(client, CLAUDE)
    await client.post(
        "/lease", json={"session": "v211-lease", "device": "desktop", "repo": REPO}, headers=CLAUDE
    )
    for spelling in (me["agent"], me["alias"]):
        found = (await client.get(
            "/active", params={"repo": REPO, "holder": spelling}, headers=LAPTOP
        )).json()["agents"]
        assert "v211-lease" in {a["session"] for a in found}


# ---- retirement: the live space recycles, the past does not change ----------

async def test_releasing_a_lease_frees_the_name_without_rewriting_history(client):
    agent = {**SERVER, "X-Agent-Key": "retiree"}
    me = await whoami(client, agent)
    post_id = (await client.post(
        "/post", json={"summary": "before I go"}, headers=agent
    )).json()["id"]
    lease_id = (await client.post(
        "/lease", json={"session": "v211-retire", "device": "server"}, headers=agent
    )).json()["lease_id"]
    await client.post("/lease/release", json={"lease_id": lease_id}, headers=agent)

    # The post keeps the name it was authored under...
    assert (await client.get(f"/post/{post_id}", headers=SERVER)).json()["from"] == me["agent"]
    # ...and the key still resolves to it, which is why the alias exists.
    inbox_probe = (await client.get(
        "/board", params={"to": me["alias"]}, headers=SERVER
    )).json()
    assert isinstance(inbox_probe, list)

    # A returning agent is handed its old name back (nobody took it meanwhile).
    assert (await whoami(client, agent))["agent"] == me["agent"]


async def test_an_agent_keeps_its_name_while_it_still_holds_another_lease(client):
    """One agent, several sessions: ending one is not the end of it. Retiring on
    the first release would rename it mid-life and split its work in two."""
    busy = {**SERVER, "X-Agent-Key": "twohats"}
    me = await whoami(client, busy)
    ids = [
        (await client.post(
            "/lease", json={"session": f"v211-hat{n}", "device": "server", "ttl": 600},
            headers=busy,
        )).json()["lease_id"]
        for n in (1, 2)
    ]

    await client.post("/lease/release", json={"lease_id": ids[0]}, headers=busy)
    contender = {**SERVER, "X-Agent-Key": "hatthief", "X-Agent-Name": me["name"]}
    assert (await whoami(client, contender))["name"] != me["name"]
    assert (await whoami(client, busy))["agent"] == me["agent"]

    # Once the last one goes, the name is free as usual.
    await client.post("/lease/release", json={"lease_id": ids[1]}, headers=busy)
    heir = {**SERVER, "X-Agent-Key": "hatheir", "X-Agent-Name": me["name"]}
    assert (await whoami(client, heir))["name"] == me["name"]


async def test_a_freed_name_can_be_taken_by_another_agent(client):
    first = {**DESKTOP, "X-Agent-Key": "recycle1"}
    me = await whoami(client, first)
    lease_id = (await client.post(
        "/lease", json={"session": "v211-recycle", "device": "desktop"}, headers=first
    )).json()["lease_id"]
    await client.post("/lease/release", json={"lease_id": lease_id}, headers=first)

    # A second agent may now request the freed name and get it.
    second = {**DESKTOP, "X-Agent-Key": "recycle2", "X-Agent-Name": me["name"]}
    assert (await whoami(client, second))["agent"] == me["agent"]

    # The original comes back to a *different* name rather than a stolen one —
    # this is why the key alias has to be permanent and the name does not.
    back = await whoami(client, first)
    assert back["name"] != me["name"] and back["alias"] == me["alias"]


async def test_replaying_an_old_release_does_not_retire_the_current_holder(client):
    """Names recycle, so a lease's stored holder can name a *different* agent by
    the time a duplicate release arrives. Only the transition may retire."""
    gone = {**SERVER, "X-Agent-Key": "replay1"}
    original = await whoami(client, gone)
    lease_id = (await client.post(
        "/lease", json={"session": "v211-replay", "device": "server"}, headers=gone
    )).json()["lease_id"]
    await client.post("/lease/release", json={"lease_id": lease_id}, headers=gone)

    successor = {**SERVER, "X-Agent-Key": "replay2", "X-Agent-Name": original["name"]}
    assert (await whoami(client, successor))["name"] == original["name"]

    # The duplicate release is still idempotent-OK, and must not unname the heir.
    again = await client.post("/lease/release", json={"lease_id": lease_id}, headers=gone)
    assert again.status_code == 200
    assert (await whoami(client, successor))["name"] == original["name"]


async def test_a_name_is_never_allocated_over_another_agents_key(client):
    """Both spellings live in one namespace, so a name that shadowed someone's
    key would make that agent unreachable by the form meant to be permanent."""
    holder = {**DESKTOP, "X-Agent-Key": "shadowme"}
    await whoami(client, holder)
    thief = {**DESKTOP, "X-Agent-Key": "thiefkey", "X-Agent-Name": "shadowme"}
    assert (await whoami(client, thief))["name"] != "shadowme"


async def test_a_leased_agent_is_exempt_from_the_staleness_sweep(client):
    """The sweep is a leak backstop, not a scheduler: an agent the board can see
    is alive keeps its name however long its stint has run."""
    longrunner = {**LAPTOP, "X-Agent-Key": "longrun"}
    me = await whoami(client, longrunner)
    await client.post(
        "/lease", json={"session": "v211-longrun", "device": "laptop", "ttl": 600},
        headers=longrunner,
    )
    async with async_session() as db:
        await db.execute(
            update(AgentName)
            .where(AgentName.machine == "laptop", AgentName.key == "longrun")
            .values(allocated_at=datetime.now(UTC) - NAME_TTL * 2)
        )
        await db.commit()

    # Someone else allocating triggers the sweep — which must skip the leaseholder.
    await whoami(client, {**LAPTOP, "X-Agent-Key": "sweeper1"})
    contender = {**LAPTOP, "X-Agent-Key": "contend1", "X-Agent-Name": me["name"]}
    assert (await whoami(client, contender))["name"] != me["name"]
    assert (await whoami(client, longrunner))["agent"] == me["agent"]


async def test_a_successor_does_not_inherit_its_predecessors_mail(client):
    """History keeps the name its author held at the time, so a recycled name
    points at two agents across time. The inbox only reaches back as far as the
    current holder has held it — the key alias is what stays permanent."""
    first = {**DESKTOP, "X-Agent-Key": "mailbox1"}
    original = await whoami(client, first)
    for_original = (await client.post(
        "/post", json={"summary": "for the first holder", "to": original["agent"]}, headers=NAMED
    )).json()["id"]
    lease_id = (await client.post(
        "/lease", json={"session": "v211-mailbox", "device": "desktop"}, headers=first
    )).json()["lease_id"]
    await client.post("/lease/release", json={"lease_id": lease_id}, headers=first)

    heir = {**DESKTOP, "X-Agent-Key": "mailbox2", "X-Agent-Name": original["name"]}
    assert (await whoami(client, heir))["name"] == original["name"]
    for_heir = (await client.post(
        "/post", json={"summary": "for the heir", "to": original["agent"]}, headers=NAMED
    )).json()["id"]

    heir_inbox = {p["id"] for p in (
        await client.get("/board", params={"to": SELF, "window_min": 0}, headers=heir)
    ).json()}
    assert for_heir in heir_inbox and for_original not in heir_inbox

    # The original still reaches its own mail by the permanent form.
    by_alias = {p["id"] for p in (
        await client.get(
            "/board", params={"to": original["alias"], "window_min": 0}, headers=DESKTOP
        )
    ).json()}
    assert for_original in by_alias


async def test_mail_to_a_retired_agents_alias_never_reaches_its_successor(client):
    """The alias is only permanent if it keeps meaning one agent. A post sent to
    a retired agent's key must not be rewritten to the name a successor now
    holds, and must not turn up in that successor's inbox."""
    first = {**DESKTOP, "X-Agent-Key": "willed1"}
    original = await whoami(client, first)
    lease_id = (await client.post(
        "/lease", json={"session": "v211-willed", "device": "desktop"}, headers=first
    )).json()["lease_id"]
    await client.post("/lease/release", json={"lease_id": lease_id}, headers=first)

    heir = {**DESKTOP, "X-Agent-Key": "willed2", "X-Agent-Name": original["name"]}
    assert (await whoami(client, heir))["name"] == original["name"]

    late = (await client.post(
        "/post", json={"summary": "for the one who left", "to": original["alias"]}, headers=NAMED
    )).json()["id"]
    # Recorded under the key, because the name no longer identifies that agent.
    assert (await client.get(f"/post/{late}", headers=DESKTOP)).json()["to"] == original["alias"]

    heir_inbox = {p["id"] for p in (
        await client.get("/board", params={"to": SELF, "window_min": 0}, headers=heir)
    ).json()}
    assert late not in heir_inbox


async def test_reading_a_retired_alias_does_not_spill_the_successors_mail(client):
    """The name half of an inbox is clipped to the stint that held it — on both
    sides, or looking up an old agent would read its successor's post."""
    first = {**SERVER, "X-Agent-Key": "spill1"}
    original = await whoami(client, first)
    mine = (await client.post(
        "/post", json={"summary": "sent while I held it", "to": original["agent"]}, headers=NAMED
    )).json()["id"]
    lease_id = (await client.post(
        "/lease", json={"session": "v211-spill", "device": "server"}, headers=first
    )).json()["lease_id"]
    await client.post("/lease/release", json={"lease_id": lease_id}, headers=first)

    heir = {**SERVER, "X-Agent-Key": "spill2", "X-Agent-Name": original["name"]}
    heir_id = (await whoami(client, heir))["agent"]
    theirs = (await client.post(
        "/post", json={"summary": "sent to the heir", "to": heir_id}, headers=NAMED
    )).json()["id"]

    history = {p["id"] for p in (await client.get(
        "/board", params={"to": original["alias"], "window_min": 0}, headers=SERVER
    )).json()}
    assert mine in history and theirs not in history


async def test_a_name_never_retired_is_reclaimed_after_the_ttl(client):
    """The runtimes this change exists for have no lifecycle hooks, so they never
    release a lease and never retire a name. Without a backstop the live space
    would only ever fill; with one, an abandoned name comes back."""
    ghost = {**LAPTOP, "X-Agent-Key": "ghostkey"}
    stranded = await whoami(client, ghost)

    async with async_session() as db:  # backdate it past any plausible session
        await db.execute(
            update(AgentName)
            .where(AgentName.machine == "laptop", AgentName.key == "ghostkey")
            .values(allocated_at=datetime.now(UTC) - NAME_TTL * 2)
        )
        await db.commit()

    heir = {**LAPTOP, "X-Agent-Key": "heirkey", "X-Agent-Name": stranded["name"]}
    assert (await whoami(client, heir))["name"] == stranded["name"]

    # And the ghost, if it ever speaks again, is renamed rather than duplicated.
    revived = await whoami(client, ghost)
    assert revived["name"] != stranded["name"] and revived["alias"] == stranded["alias"]
