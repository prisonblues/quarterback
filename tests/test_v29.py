"""v2.9: identity differentiation — machine/instance instead of just the machine.

Every agent on a box shares that box's token, so they all authored posts as
"server" and none could be addressed individually. The token still proves the
machine; an ``X-Agent-Instance`` header names the agent on it. These tests cover
the composition, the hierarchical addressing that falls out of it, and the fact
that authorisation deliberately did *not* get finer (co-tenants share a token,
so a permission boundary between them would be theatre).
"""

from __future__ import annotations

from app.identity import addressed_to, compose, machine_of, same_machine, split, valid_instance

from .conftest import LAPTOP, SERVER

REPO = "v29repo"

# Two agents on one machine — the case that used to collapse to one identity.
SERVER_A = {**SERVER, "X-Agent-Instance": "f5ca7491"}
SERVER_B = {**SERVER, "X-Agent-Instance": "938fca68"}
LAPTOP_A = {**LAPTOP, "X-Agent-Instance": "deploy"}


# ---- identity algebra (pure) ------------------------------------------------

def test_compose_and_split_round_trip():
    assert compose("server", "f5ca7491") == "server/f5ca7491"
    assert compose("server", None) == "server"
    assert split("server/f5ca7491") == ("server", "f5ca7491")
    assert split("server") == ("server", None)
    assert machine_of("server/f5ca7491") == "server"


def test_same_machine_is_the_authorisation_grain():
    assert same_machine("server/f5ca7491", "server/938fca68")
    assert same_machine("server/f5ca7491", "server")  # pre-2.9 holder vs. its agent
    assert not same_machine("server/f5ca7491", "laptop/f5ca7491")


def test_addressing_is_hierarchical_both_ways():
    # An agent's inbox: itself, plus anything sent to its whole machine.
    assert addressed_to("server/f5ca7491", "server/f5ca7491")
    assert addressed_to("server", "server/f5ca7491")
    # A machine's inbox: anything sent to any of its agents.
    assert addressed_to("server/f5ca7491", "server")
    # Never across machines, and never a mere prefix collision.
    assert not addressed_to("server/938fca68", "server/f5ca7491")
    assert not addressed_to("zeusling", "server")
    assert not addressed_to(None, "server")


def test_valid_instance_rejects_separators_and_empties():
    assert valid_instance("f5ca7491") and valid_instance("deploy-2")
    assert not valid_instance("has/slash")
    assert not valid_instance("has space")
    assert not valid_instance("")
    assert not valid_instance("-leading")
    assert not valid_instance("x" * 41)


# ---- the header becomes the author ------------------------------------------

async def test_whoami_reflects_the_resolved_identity(client):
    r = (await client.get("/whoami", headers=SERVER_A)).json()
    assert r == {"agent": "server/f5ca7491", "machine": "server", "instance": "f5ca7491"}

    bare = (await client.get("/whoami", headers=SERVER)).json()
    assert bare == {"agent": "server", "machine": "server", "instance": None}


async def test_co_tenant_agents_author_distinctly(client):
    a = (await client.post("/post", json={"summary": "from A"}, headers=SERVER_A)).json()["id"]
    b = (await client.post("/post", json={"summary": "from B"}, headers=SERVER_B)).json()["id"]

    assert (await client.get(f"/post/{a}", headers=SERVER)).json()["from"] == "server/f5ca7491"
    assert (await client.get(f"/post/{b}", headers=SERVER)).json()["from"] == "server/938fca68"


async def test_bad_instance_header_is_rejected_not_ignored(client):
    bad = {**SERVER, "X-Agent-Instance": "a/b"}
    r = await client.post("/post", json={"summary": "x"}, headers=bad)
    assert r.status_code == 400
    assert "X-Agent-Instance" in r.json()["detail"]


async def test_instance_cannot_forge_another_machine(client):
    # The instance is scoped under the authenticated machine, never replacing it.
    r = (await client.get("/whoami", headers={**SERVER, "X-Agent-Instance": "laptop"})).json()
    assert r["agent"] == "server/laptop" and r["machine"] == "server"


# ---- directed posts reach the right inbox -----------------------------------

async def test_directed_post_reaches_the_named_agent_and_its_machine(client):
    to_one = (await client.post(
        "/post", json={"summary": "for A only", "to": "server/f5ca7491"}, headers=LAPTOP_A
    )).json()["id"]
    to_box = (await client.post(
        "/post", json={"summary": "for all server", "to": "server"}, headers=LAPTOP_A
    )).json()["id"]

    def ids(posts):
        return {p["id"] for p in posts}

    inbox_a = ids((await client.get("/board", params={"to": "server/f5ca7491"}, headers=SERVER)).json())
    inbox_b = ids((await client.get("/board", params={"to": "server/938fca68"}, headers=SERVER)).json())
    inbox_box = ids((await client.get("/board", params={"to": "server"}, headers=SERVER)).json())

    assert {to_one, to_box} <= inbox_a          # named agent sees both
    assert to_box in inbox_b and to_one not in inbox_b   # co-tenant sees only the broadcast
    assert {to_one, to_box} <= inbox_box        # the machine sees its agents' mail


# ---- leases: identity gets finer, authorisation does not --------------------

async def test_lease_holder_carries_the_instance(client):
    body = {"session": "v29-lease", "device": "server", "repo": REPO, "title": "identity work"}
    claim = (await client.post("/lease", json=body, headers=SERVER_A)).json()
    assert claim["holder"] == "server/f5ca7491"

    # /active hands a peer the exact address to reply to.
    agents = (await client.get("/active", params={"repo": REPO}, headers=LAPTOP)).json()["agents"]
    assert [a["holder"] for a in agents if a["session"] == "v29-lease"] == ["server/f5ca7491"]


async def test_holder_filter_matches_every_instance_on_a_machine(client):
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
    """A lease claimed before the agent had an instance adopts one on renew —
    that's the migration path for sessions already live when 2.9 lands."""
    body = {"session": "v29-upgrade", "device": "server"}
    assert (await client.post("/lease", json=body, headers=SERVER)).json()["holder"] == "server"
    renewed = (await client.post("/lease", json=body, headers=SERVER_A)).json()
    assert renewed["renewed"] is True and renewed["holder"] == "server/f5ca7491"


async def test_another_device_still_conflicts(client):
    body = {"session": "v29-conflict", "device": "server"}
    await client.post("/lease", json=body, headers=SERVER_A)
    clash = await client.post(
        "/lease", json={"session": "v29-conflict", "device": "laptop"}, headers=LAPTOP_A
    )
    assert clash.status_code == 409
    assert clash.json()["detail"]["held_by"] == "server/f5ca7491"
