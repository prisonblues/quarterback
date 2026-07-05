"""v2.1 tests: browser read-auth, post refs, and the worktree registry."""

from __future__ import annotations

import pytest

from app.config import settings

from .conftest import LAPTOP

AUTHELIA = {"Remote-User": "devuser"}


# --- reader auth (browser board) -----------------------------------------


async def test_board_view_served_and_gated(client):
    page = await client.get("/", headers=LAPTOP)
    assert page.status_code == 200
    assert "quarterback board" in page.text
    assert (await client.get("/")).status_code == 401  # no auth, no dev bypass


async def test_reader_accepts_authelia_header(client):
    r = await client.get("/board", headers=AUTHELIA)
    assert r.status_code == 200


async def test_reader_dev_bypass(client):
    assert (await client.get("/board")).status_code == 401
    settings.browser_dev_user = "devuser"
    try:
        assert (await client.get("/board")).status_code == 200
        assert (await client.get("/")).status_code == 200
    finally:
        settings.browser_dev_user = ""


async def test_writes_still_require_bearer_not_browser(client):
    # A browser-authenticated (Authelia) request must not be able to POST.
    r = await client.post("/post", json={"summary": "x"}, headers=AUTHELIA)
    assert r.status_code == 401


# --- post refs -----------------------------------------------------------


async def test_post_with_refs_round_trip(client):
    refs = [
        {"kind": "pr", "value": "45", "repo": "devuser/quarterback"},
        {"kind": "commit", "value": "abc123def", "repo": "devuser/quarterback"},
    ]
    pid = (
        await client.post(
            "/post",
            json={"type": "landed", "summary": "shipped the retry helper", "refs": refs},
            headers=LAPTOP,
        )
    ).json()["id"]

    row = next(p for p in (await client.get("/board", headers=LAPTOP)).json() if p["id"] == pid)
    assert row["refs"] == refs
    assert (await client.get(f"/post/{pid}", headers=LAPTOP)).json()["refs"] == refs


async def test_post_without_refs_defaults_to_empty_list(client):
    pid = (await client.post("/post", json={"summary": "plain"}, headers=LAPTOP)).json()["id"]
    row = next(p for p in (await client.get("/board", headers=LAPTOP)).json() if p["id"] == pid)
    assert row["refs"] == []


async def test_bad_ref_kind_rejected(client):
    r = await client.post(
        "/post",
        json={"summary": "x", "refs": [{"kind": "bogus", "value": "1"}]},
        headers=LAPTOP,
    )
    assert r.status_code == 422


# --- worktree registry ---------------------------------------------------


@pytest.fixture
def snapshot():
    return {
        "device": "laptop",
        "worktrees": [
            {
                "path": "/home/devuser/source/quarterback",
                "repo": "devuser/quarterback",
                "branch": "master",
                "head": "1111111aaaaaaaa",
                "commits": [{"sha": "1111111aaaaaaaa", "subject": "root"}],
            },
            {
                "path": "/home/devuser/source/quarterback-feat",
                "repo": "devuser/quarterback",
                "branch": "feat-x",
                "head": "2222222bbbbbbbb",
                "commits": [
                    {"sha": "2222222bbbbbbbb", "subject": "feature tip"},
                    {"sha": "3333333ccccccc", "subject": "earlier"},
                ],
            },
        ],
    }


async def test_worktrees_register_and_query(client, snapshot):
    r = await client.put("/worktrees", json=snapshot, headers=LAPTOP)
    assert r.json() == {"device": "laptop", "count": 2}

    rows = (
        await client.get("/worktrees", params={"repo": "devuser/quarterback"}, headers=LAPTOP)
    ).json()
    assert {w["branch"] for w in rows} == {"master", "feat-x"}

    branch = (await client.get("/worktrees", params={"branch": "feat-x"}, headers=LAPTOP)).json()
    assert len(branch) == 1 and branch[0]["path"].endswith("quarterback-feat")


async def test_worktrees_find_commit(client, snapshot):
    await client.put("/worktrees", json=snapshot, headers=LAPTOP)

    # Match by a commit deeper in the list (not the head), using a short prefix.
    hit = (await client.get("/worktrees", params={"has_commit": "3333333c"}, headers=LAPTOP)).json()
    assert len(hit) == 1 and hit[0]["branch"] == "feat-x"

    # A sha nobody has -> empty.
    miss = (
        await client.get("/worktrees", params={"has_commit": "deadbeefcafe"}, headers=LAPTOP)
    ).json()
    assert miss == []


async def test_worktrees_snapshot_replaces(client, snapshot):
    await client.put("/worktrees", json=snapshot, headers=LAPTOP)
    # Re-register with a single worktree — the stale one must disappear.
    await client.put(
        "/worktrees",
        json={"device": "laptop", "worktrees": [snapshot["worktrees"][0]]},
        headers=LAPTOP,
    )
    rows = (await client.get("/worktrees", params={"device": "laptop"}, headers=LAPTOP)).json()
    assert len(rows) == 1 and rows[0]["branch"] == "master"
