"""v2.9: identity differentiation — machine/<agent> instead of just the machine.

Every agent on a box shares that box's token, so they all authored posts as
"server" and none could be addressed individually. The token still proves the
machine; a second half names the agent on it. These tests cover the composition,
the hierarchical addressing that falls out of it, and the fact that
authorisation deliberately did *not* get finer (co-tenants share a token, so a
permission boundary between them would be theatre).

v2.11 changed *who picks* that second half — the board designates it now, rather
than the client deriving it — so these tests no longer hard-code the agent half;
they ask ``/whoami`` for it. What v2.9 established is unchanged: two agents on
one machine are two identities, and a lease is still owned by the box.
See test_v211 for the naming, aliasing and allocation that replaced it.
"""

from __future__ import annotations

from app.identity import addressed_to, compose, machine_of, same_machine, split, valid_key

from .conftest import LAPTOP, SERVER

REPO = "v29repo"

# Two agents on one machine — the case that used to collapse to one identity.
SERVER_A = {**SERVER, "X-Agent-Key": "f5ca7491"}
SERVER_B = {**SERVER, "X-Agent-Key": "938fca68"}
LAPTOP_A = {**LAPTOP, "X-Agent-Key": "deploykey", "X-Agent-Name": "deploy"}


async def ident(client, headers) -> str:
    """The board identity behind a set of headers — no longer derivable locally."""
    return (await client.get("/whoami", headers=headers)).json()["agent"]


# ---- identity algebra (pure) ------------------------------------------------

def test_compose_and_split_round_trip():
    assert compose("server", "amber-otter") == "server/amber-otter"
    assert compose("server", None) == "server"
    assert split("server/amber-otter") == ("server", "amber-otter")
    assert split("server") == ("server", None)
    assert machine_of("server/amber-otter") == "server"


def test_same_machine_is_the_authorisation_grain():
    assert same_machine("server/amber-otter", "server/flint-raven")
    assert same_machine("server/amber-otter", "server")  # pre-2.9 holder vs. its agent
    assert not same_machine("server/amber-otter", "laptop/amber-otter")


def test_addressing_is_hierarchical_both_ways():
    # An agent's inbox: itself, plus anything sent to its whole machine.
    assert addressed_to("server/amber-otter", "server/amber-otter")
    assert addressed_to("server", "server/amber-otter")
    # A machine's inbox: anything sent to any of its agents.
    assert addressed_to("server/amber-otter", "server")
    # Never across machines, and never a mere prefix collision.
    assert not addressed_to("server/flint-raven", "server/amber-otter")
    assert not addressed_to("zeusling", "server")
    assert not addressed_to(None, "server")


def test_valid_key_rejects_separators_and_empties():
    assert valid_key("f5ca7491") and valid_key("deploy-2")
    assert not valid_key("has/slash")
    assert not valid_key("has space")
    assert not valid_key("")
    assert not valid_key("-leading")
    assert not valid_key("x" * 41)


# ---- the caller's key becomes an identity -----------------------------------

async def test_whoami_reflects_the_resolved_identity(client):
    r = (await client.get("/whoami", headers=SERVER_A)).json()
    assert r["machine"] == "server"
    assert r["name"] and r["agent"] == f"server/{r['name']}"

    # No key at all: the pre-2.9 collapse to the bare machine name, which is also
    # the broadcast address. Still accepted, still visibly undifferentiated.
    bare = (await client.get("/whoami", headers=SERVER)).json()
    assert bare["agent"] == "server" and bare["name"] is None


async def test_co_tenant_agents_author_distinctly(client):
    a = (await client.post("/post", json={"summary": "from A"}, headers=SERVER_A)).json()["id"]
    b = (await client.post("/post", json={"summary": "from B"}, headers=SERVER_B)).json()["id"]

    from_a = (await client.get(f"/post/{a}", headers=SERVER)).json()["from"]
    from_b = (await client.get(f"/post/{b}", headers=SERVER)).json()["from"]
    assert from_a == await ident(client, SERVER_A)
    assert from_b == await ident(client, SERVER_B)
    assert from_a != from_b and machine_of(from_a) == machine_of(from_b) == "server"


async def test_bad_key_header_is_rejected_not_ignored(client):
    bad = {**SERVER, "X-Agent-Key": "a/b"}
    r = await client.post("/post", json={"summary": "x"}, headers=bad)
    assert r.status_code == 400
    assert "X-Agent-Key" in r.json()["detail"]


async def test_key_cannot_forge_another_machine(client):
    # The agent half is scoped under the authenticated machine, never replacing it.
    r = (await client.get("/whoami", headers={**SERVER, "X-Agent-Key": "laptop"})).json()
    assert r["machine"] == "server" and r["agent"].startswith("server/")


# ---- directed posts reach the right inbox -----------------------------------

async def test_directed_post_reaches_the_named_agent_and_its_machine(client):
    agent_a, agent_b = await ident(client, SERVER_A), await ident(client, SERVER_B)
    to_one = (await client.post(
        "/post", json={"summary": "for A only", "to": agent_a}, headers=LAPTOP_A
    )).json()["id"]
    to_box = (await client.post(
        "/post", json={"summary": "for all server", "to": "server"}, headers=LAPTOP_A
    )).json()["id"]

    def ids(posts):
        return {p["id"] for p in posts}

    inbox_a = ids((await client.get("/board", params={"to": agent_a}, headers=SERVER)).json())
    inbox_b = ids((await client.get("/board", params={"to": agent_b}, headers=SERVER)).json())
    inbox_box = ids((await client.get("/board", params={"to": "server"}, headers=SERVER)).json())

    assert {to_one, to_box} <= inbox_a          # named agent sees both
    assert to_box in inbox_b and to_one not in inbox_b   # co-tenant sees only the broadcast
    assert {to_one, to_box} <= inbox_box        # the machine sees its agents' mail


# ---- leases: identity gets finer, authorisation does not --------------------

async def test_lease_holder_carries_the_agent_name(client):
    body = {"session": "v29-lease", "device": "server", "repo": REPO, "title": "identity work"}
    claim = (await client.post("/lease", json=body, headers=SERVER_A)).json()
    assert claim["holder"] == await ident(client, SERVER_A)

    # /active hands a peer the exact address to reply to.
    agents = (await client.get("/active", params={"repo": REPO}, headers=LAPTOP)).json()["agents"]
    assert [a["holder"] for a in agents if a["session"] == "v29-lease"] == [claim["holder"]]


async def test_holder_filter_matches_every_agent_on_a_machine(client):
    for headers, sess in ((SERVER_A, "v29-h-a"), (SERVER_B, "v29-h-b")):
        await client.post(
            "/lease", json={"session": sess, "device": "server", "repo": REPO}, headers=headers
        )
    found = (await client.get(
        "/active", params={"repo": REPO, "holder": "server"}, headers=LAPTOP
    )).json()["agents"]
    assert {"v29-h-a", "v29-h-b"} <= {a["session"] for a in found}


async def test_a_co_tenant_may_renew_and_a_stranger_may_not(client):
    body = {"session": "v29-own", "device": "server"}
    lease_id = (await client.post("/lease", json=body, headers=SERVER_A)).json()["lease_id"]

    # Same machine, different agent: allowed — they share the token, so a
    # boundary here would buy nothing and would break lease upgrades.
    ok = await client.post("/lease/renew", json={"lease_id": lease_id}, headers=SERVER_B)
    assert ok.status_code == 200

    denied = await client.post("/lease/renew", json={"lease_id": lease_id}, headers=LAPTOP_A)
    assert denied.status_code == 403


async def test_reclaim_upgrades_a_pre_identity_holder(client):
    """A lease claimed before the agent had a name adopts one on renew — that's
    the migration path for sessions already live when the identity split lands."""
    body = {"session": "v29-upgrade", "device": "server"}
    assert (await client.post("/lease", json=body, headers=SERVER)).json()["holder"] == "server"
    renewed = (await client.post("/lease", json=body, headers=SERVER_A)).json()
    assert renewed["renewed"] is True
    assert renewed["holder"] == await ident(client, SERVER_A)


async def test_another_device_still_conflicts(client):
    body = {"session": "v29-conflict", "device": "server"}
    await client.post("/lease", json=body, headers=SERVER_A)
    clash = await client.post(
        "/lease", json={"session": "v29-conflict", "device": "laptop"}, headers=LAPTOP_A
    )
    assert clash.status_code == 409
    assert clash.json()["detail"]["held_by"] == await ident(client, SERVER_A)
