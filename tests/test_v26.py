"""v2.6 coordination: the /active collision index + sub-agent visibility."""

from __future__ import annotations

from .conftest import DESKTOP, LAPTOP, ZEUS


async def _lease(client, session, device, headers, **kw):
    return await client.post(
        "/lease", json={"session": session, "device": device, **kw}, headers=headers
    )


async def test_active_lists_leases_scoped_by_cwd(client):
    await _lease(client, "s-active-1", "zeus", ZEUS, cwd="/src/lexray")
    await _lease(client, "s-active-2", "laptop", LAPTOP, cwd="/src/quarterback")

    data = (await client.get("/active", params={"cwd": "/src/lexray"}, headers=DESKTOP)).json()
    sessions = {a["session"] for a in data["agents"]}
    assert "s-active-1" in sessions  # lives in the queried dir
    assert "s-active-2" not in sessions  # different worktree, filtered out


async def test_subagent_register_shows_active_then_ends(client):
    ps = "s-parent-1"
    await _lease(client, ps, "zeus", ZEUS, cwd="/src/quarterback")

    reg = await client.post(
        "/subagent",
        json={
            "parent_session": ps,
            "agent_id": "a1",
            "label": "Explore: board",
            "cwd": "/src/quarterback",
        },
        headers=ZEUS,
    )
    assert reg.status_code == 200
    assert reg.json()["label"] == "Explore: board"

    active = (await client.get("/active", params={"cwd": "/src/quarterback"}, headers=ZEUS)).json()
    assert any(s["agent_id"] == "a1" for s in active["subagents"])

    # It surfaces on the parent's session card, too.
    card = next(
        c for c in (await client.get("/sessions", headers=ZEUS)).json() if c["session"] == ps
    )
    assert any(s["agent_id"] == "a1" for s in card["subagents"])

    # Re-registering the same agent_id is an idempotent renew, not a duplicate.
    assert (
        await client.post("/subagent", json={"parent_session": ps, "agent_id": "a1"}, headers=ZEUS)
    ).status_code == 200

    # Ending removes it from the live view.
    end = await client.post(
        "/subagent/end", json={"parent_session": ps, "agent_id": "a1"}, headers=ZEUS
    )
    assert end.json()["ended"] is True
    after = (await client.get("/active", params={"cwd": "/src/quarterback"}, headers=ZEUS)).json()
    assert not any(s["agent_id"] == "a1" for s in after["subagents"])


async def test_active_requires_auth(client):
    assert (await client.get("/active")).status_code == 401


async def test_subagent_end_unknown_is_noop(client):
    r = await client.post(
        "/subagent/end", json={"parent_session": "nope", "agent_id": "x"}, headers=ZEUS
    )
    assert r.json()["ended"] is False


async def test_active_filters_by_device_and_holder(client):
    # Two leases + their sub-agents on the same cwd but different devices/holders.
    cwd = "/src/collide"
    await _lease(client, "s-dev-zeus", "zeus", ZEUS, cwd=cwd)
    await _lease(client, "s-dev-laptop", "laptop", LAPTOP, cwd=cwd)
    await client.post(
        "/subagent",
        json={"parent_session": "s-dev-zeus", "agent_id": "sz", "cwd": cwd, "device": "zeus"},
        headers=ZEUS,
    )
    await client.post(
        "/subagent",
        json={"parent_session": "s-dev-laptop", "agent_id": "sl", "cwd": cwd, "device": "laptop"},
        headers=LAPTOP,
    )

    # (rows from sibling tests persist — assert membership, not exact sets, and
    # that every returned row honours the filter.)
    by_device = (await client.get("/active", params={"device": "zeus"}, headers=DESKTOP)).json()
    assert "s-dev-zeus" in {a["session"] for a in by_device["agents"]}
    assert "s-dev-laptop" not in {a["session"] for a in by_device["agents"]}
    assert all(a["device"] == "zeus" for a in by_device["agents"])
    assert "sz" in {s["agent_id"] for s in by_device["subagents"]}
    assert "sl" not in {s["agent_id"] for s in by_device["subagents"]}
    assert all(s["device"] == "zeus" for s in by_device["subagents"])

    by_holder = (await client.get("/active", params={"holder": "laptop"}, headers=DESKTOP)).json()
    assert "s-dev-laptop" in {a["session"] for a in by_holder["agents"]}
    assert all(a["holder"] == "laptop" for a in by_holder["agents"])
    assert "sl" in {s["agent_id"] for s in by_holder["subagents"]}
    assert all(s["holder"] == "laptop" for s in by_holder["subagents"])


async def test_subagent_register_conflict_across_holders(client):
    ps = "s-hijack"
    await _lease(client, ps, "zeus", ZEUS, cwd="/src/x")
    assert (
        await client.post("/subagent", json={"parent_session": ps, "agent_id": "h1"}, headers=ZEUS)
    ).status_code == 200
    # A different token may not renew/relabel someone else's sub-agent.
    other = await client.post(
        "/subagent", json={"parent_session": ps, "agent_id": "h1"}, headers=LAPTOP
    )
    assert other.status_code == 409


async def test_subagent_end_by_other_holder_forbidden(client):
    ps = "s-endauth"
    await _lease(client, ps, "zeus", ZEUS, cwd="/src/y")
    await client.post("/subagent", json={"parent_session": ps, "agent_id": "e1"}, headers=ZEUS)
    forbidden = await client.post(
        "/subagent/end", json={"parent_session": ps, "agent_id": "e1"}, headers=LAPTOP
    )
    assert forbidden.status_code == 403
    # Still live — the foreign end did nothing.
    active = (await client.get("/active", headers=ZEUS)).json()
    assert any(s["agent_id"] == "e1" for s in active["subagents"])


async def test_subagent_requires_auth(client):
    assert (
        await client.post("/subagent", json={"parent_session": "p", "agent_id": "a"})
    ).status_code == 401
    assert (
        await client.post("/subagent/end", json={"parent_session": "p", "agent_id": "a"})
    ).status_code == 401


async def test_subagent_reregister_after_end_revives_and_keeps_started_at(client):
    ps = "s-revive"
    await _lease(client, ps, "zeus", ZEUS, cwd="/src/z")
    first = (
        await client.post("/subagent", json={"parent_session": ps, "agent_id": "r1"}, headers=ZEUS)
    ).json()
    await client.post("/subagent/end", json={"parent_session": ps, "agent_id": "r1"}, headers=ZEUS)

    revived = (
        await client.post("/subagent", json={"parent_session": ps, "agent_id": "r1"}, headers=ZEUS)
    ).json()
    assert revived["since"] == first["since"]  # started_at preserved across the renew
    assert revived["expires"] >= first["expires"]  # TTL refreshed
    active = (await client.get("/active", headers=ZEUS)).json()
    assert any(s["agent_id"] == "r1" for s in active["subagents"])  # live again


async def test_subagent_never_hits_the_posts_log(client):
    """Sub-agent churn is current-state only — it must not create board posts."""
    before = (await client.get("/board", params={"include_presence": "true"}, headers=ZEUS)).json()
    start = before[-1]["id"] if before else 0
    ps = "s-parent-noise"
    await _lease(client, ps, "zeus", ZEUS, cwd="/tmp/x")
    await client.post("/subagent", json={"parent_session": ps, "agent_id": "n1"}, headers=ZEUS)
    await client.post("/subagent/end", json={"parent_session": ps, "agent_id": "n1"}, headers=ZEUS)
    after = (
        await client.get(
            "/board", params={"since": start, "include_presence": "true"}, headers=ZEUS
        )
    ).json()
    assert after == []  # no posts generated
