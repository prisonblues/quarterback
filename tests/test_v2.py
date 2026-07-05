"""v2 tests: content-addressed blobs, TTL leases, and the session-handoff flow."""

from __future__ import annotations

import asyncio
import hashlib

from .conftest import DESKTOP, LAPTOP, ZEUS


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- blobs ---------------------------------------------------------------


async def test_blob_round_trip_and_idempotent(client):
    content = b'{"line":1}\n{"line":2}\n'
    sha = _sha(content)

    r = await client.put(f"/blob/{sha}", content=content, headers=LAPTOP)
    assert r.status_code == 200
    assert r.json() == {"sha": sha, "size": len(content), "created": True}

    again = await client.put(f"/blob/{sha}", content=content, headers=LAPTOP)
    assert again.json()["created"] is False  # de-duplicated

    got = await client.get(f"/blob/{sha}", headers=ZEUS)
    assert got.status_code == 200
    assert got.content == content


async def test_blob_sha_mismatch_rejected(client):
    content = b"hello"
    wrong = _sha(b"something else")
    r = await client.put(f"/blob/{wrong}", content=content, headers=LAPTOP)
    assert r.status_code == 400


async def test_blob_missing_is_404(client):
    r = await client.get(f"/blob/{_sha(b'never stored')}", headers=LAPTOP)
    assert r.status_code == 404


# --- leases --------------------------------------------------------------


async def test_lease_claim_conflict_and_renew(client):
    sess = "sess-claim"
    a = await client.post("/lease", json={"session": sess, "device": "lap"}, headers=LAPTOP)
    assert a.status_code == 200
    assert a.json()["renewed"] is False
    lease_id = a.json()["lease_id"]

    # A different device cannot claim a live lease.
    conflict = await client.post("/lease", json={"session": sess, "device": "zeu"}, headers=ZEUS)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["held_by"] == "laptop"

    # The holder re-claiming renews rather than conflicts.
    renew = await client.post("/lease", json={"session": sess, "device": "lap"}, headers=LAPTOP)
    assert renew.status_code == 200
    assert renew.json()["renewed"] is True
    assert renew.json()["lease_id"] == lease_id


async def test_lease_renew_guards(client):
    sess = "sess-renew"
    lease_id = (
        await client.post("/lease", json={"session": sess, "device": "lap"}, headers=LAPTOP)
    ).json()["lease_id"]

    # Unknown lease -> 404.
    assert (
        await client.post(
            "/lease/renew",
            json={"lease_id": "00000000-0000-0000-0000-000000000000"},
            headers=LAPTOP,
        )
    ).status_code == 404
    # Someone else's lease -> 403.
    assert (
        await client.post("/lease/renew", json={"lease_id": lease_id}, headers=ZEUS)
    ).status_code == 403
    # Owner can renew.
    assert (
        await client.post("/lease/renew", json={"lease_id": lease_id}, headers=LAPTOP)
    ).status_code == 200


async def test_lease_expires_then_peer_can_claim(client):
    sess = "sess-expire"
    await client.post("/lease", json={"session": sess, "device": "lap", "ttl": 1}, headers=LAPTOP)

    # Immediately, a peer is locked out.
    assert (
        await client.post("/lease", json={"session": sess, "device": "zeu"}, headers=ZEUS)
    ).status_code == 409

    await asyncio.sleep(1.2)  # lease lapses (simulates a crashed holder)

    # Now the peer can take over.
    took = await client.post("/lease", json={"session": sess, "device": "zeu"}, headers=ZEUS)
    assert took.status_code == 200
    assert took.json()["holder"] == "zeus"


# --- handoff flow --------------------------------------------------------


async def test_full_handoff_flow(client):
    sess = "sess-handoff"
    jsonl = b'{"t":"user"}\n{"t":"assistant"}\n'
    sha = _sha(jsonl)

    # Laptop is working the session and pushes its JSONL.
    lease = (
        await client.post("/lease", json={"session": sess, "device": "lap"}, headers=LAPTOP)
    ).json()
    await client.put(f"/blob/{sha}", content=jsonl, headers=LAPTOP)

    # Handoff records the blob and releases the lease.
    ho = await client.post("/handoff", json={"session": sess, "blob": sha}, headers=LAPTOP)
    assert ho.status_code == 200
    assert ho.json()["latest_blob"] == sha
    assert ho.json()["released_lease"] == lease["lease_id"]

    # Desktop discovers the session: latest blob present, no active lease.
    state = await client.get(f"/session/{sess}", headers=DESKTOP)
    assert state.status_code == 200
    body = state.json()
    assert body["latest_blob"] == sha
    assert body["active_lease"] is None
    assert body["device"] == "lap"

    # Desktop claims and pulls the JSONL to resume.
    claim = await client.post("/lease", json={"session": sess, "device": "desk"}, headers=DESKTOP)
    assert claim.status_code == 200
    pulled = await client.get(f"/blob/{body['latest_blob']}", headers=DESKTOP)
    assert pulled.content == jsonl


async def test_handoff_requires_held_lease_and_known_blob(client):
    sess = "sess-guard"
    jsonl = b"data"
    sha = _sha(jsonl)

    # No lease held -> 409.
    assert (
        await client.post("/handoff", json={"session": sess, "blob": sha}, headers=LAPTOP)
    ).status_code == 409

    # Hold the lease but reference a blob that was never PUT -> 400.
    await client.post("/lease", json={"session": sess, "device": "lap"}, headers=LAPTOP)
    assert (
        await client.post("/handoff", json={"session": sess, "blob": sha}, headers=LAPTOP)
    ).status_code == 400


async def test_unknown_session_is_404(client):
    assert (await client.get("/session/never-seen", headers=LAPTOP)).status_code == 404
