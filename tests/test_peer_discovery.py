"""v2.7: topic-based self-discovery, self-quiet, and directed-ask seeding.

The overlap scorer is pure (unit-tested directly); the /active and /overlap
endpoints run against the real Postgres via the shared client fixture. Leases
are scoped to a unique repo name so rows from other tests can't leak in.
"""

from __future__ import annotations

from app.overlap import overlap_score, tokenize

from .conftest import DESKTOP, LAPTOP, SERVER

REPO = "v27repo"


def _lease_body(
    session: str, title: str, recap: str = "", repo: str = REPO, cwd: str | None = None
) -> dict:
    body = {"session": session, "device": "d", "repo": repo, "title": title, "recap": recap}
    if cwd is not None:
        body["cwd"] = cwd
    return body


def _own_cwd(active: dict, session: str) -> str | None:
    """The caller's own cwd as the board holds it, off ``/active``.

    A cwd test that compares a peer's path against the literal it posted is
    comparing a constant with itself; comparing against what the board says the
    *caller* is standing in is the relationship the endpoint is for."""
    return next(a for a in active["agents"] if a["session"] == session)["cwd"]


# ---- overlap scorer (pure) --------------------------------------------------

def test_tokenize_drops_stopwords_and_noise():
    toks = tokenize("Investigating the merge-test flakiness on CI")
    assert "merge" in toks and "flakiness" in toks
    assert "the" not in toks and "on" not in toks  # stopwords
    assert "ci" not in toks  # 2-char noise
    assert "investigating" not in toks  # domain filler


def test_overlap_identical_and_disjoint():
    assert overlap_score("merge test flakiness", "merge test flakiness") == 1.0
    assert overlap_score("merge test flakiness", "css button colors") == 0.0
    assert overlap_score("anything", None) == 0.0


def test_overlap_coefficient_not_diluted_by_length():
    # A terse title fully contained in a verbose recap scores 1.0 (overlap
    # coefficient), not a Jaccard-diluted fraction.
    assert overlap_score(
        "doltgres flakiness",
        "the doltgres host resolution flakiness keeps failing the merge suite",
    ) == 1.0


# ---- /active: repo scope + self-quiet ---------------------------------------

async def test_active_repo_filter_and_peers_only(client):
    await client.post("/lease", json=_lease_body("s-laptop", "merge tests"), headers=LAPTOP)
    await client.post("/lease", json=_lease_body("s-server", "merge tests"), headers=SERVER)

    both = (await client.get("/active", params={"repo": REPO}, headers=LAPTOP)).json()
    sessions = {a["session"] for a in both["agents"]}
    assert {"s-laptop", "s-server"} <= sessions
    assert all(a["repo"] == REPO for a in both["agents"])

    # mine tags ownership; peers_only drops my own lease entirely.
    tagged = (
        await client.get(
            "/active", params={"repo": REPO, "mine": "s-laptop"}, headers=LAPTOP
        )
    ).json()
    own = {a["session"]: a["own"] for a in tagged["agents"]}
    assert own["s-laptop"] is True and own["s-server"] is False

    peers = (
        await client.get(
            "/active",
            params={"repo": REPO, "mine": "s-laptop", "peers_only": "true"},
            headers=LAPTOP,
        )
    ).json()
    assert "s-laptop" not in {a["session"] for a in peers["agents"]}
    assert "s-server" in {a["session"] for a in peers["agents"]}


async def test_active_self_quiet_excludes_own_subagents(client):
    # A session with its own fan-out must not read that fan-out as a peer.
    await client.post("/lease", json=_lease_body("s-parent", "big audit"), headers=LAPTOP)
    await client.post(
        "/subagent",
        json={"parent_session": "s-parent", "agent_id": "sa1", "label": "Explore: x", "cwd": "/w"},
        headers=LAPTOP,
    )
    view = (await client.get("/active", params={"mine": "s-parent"}, headers=LAPTOP)).json()
    own_subs = [s for s in view["subagents"] if s["parent_session"] == "s-parent"]
    assert own_subs and all(s["own"] is True for s in own_subs)

    peers = (
        await client.get(
            "/active", params={"mine": "s-parent", "peers_only": "true"}, headers=LAPTOP
        )
    ).json()
    assert all(s["parent_session"] != "s-parent" for s in peers["subagents"])


# ---- /overlap: genuine peers ranked by subject ------------------------------

async def test_overlap_finds_same_problem_peer_and_threads_last_post(client):
    await client.post(
        "/lease",
        json=_lease_body("o-laptop", "merge test repeatability"),
        headers=LAPTOP,
    )
    await client.post(
        "/lease",
        json=_lease_body("o-server", "flaky merge tests after PR merge", "the merge suite is flaky"),
        headers=SERVER,
    )
    # A same-repo agent on an unrelated topic must NOT surface.
    await client.post(
        "/lease", json=_lease_body("o-desktop", "css button palette"), headers=DESKTOP
    )
    # server's latest post — the overlap result should thread onto it.
    pid = (
        await client.post(
            "/post",
            json={"type": "finding", "summary": "merge suite non-deterministic", "session": "o-server"},
            headers=SERVER,
        )
    ).json()["id"]

    res = (
        await client.get(
            "/overlap",
            params={"mine": "o-laptop", "repo": REPO, "subject": "merge test repeatability"},
            headers=LAPTOP,
        )
    ).json()
    peers = {p["session"]: p for p in res["peers"]}
    assert "o-server" in peers  # same problem, different angle
    assert "o-laptop" not in peers  # never myself
    assert "o-desktop" not in peers  # same repo, unrelated subject → filtered
    assert peers["o-server"]["score"] > 0
    assert peers["o-server"]["last_post_id"] == pid
    assert peers["o-server"]["holder"] == "server"  # the `to` address for a directed ask


async def test_overlap_reports_each_peer_cwd(client):
    # Same repo, two different checkouts, and the advice differs: a peer in its
    # own worktree shares a branch name, a peer in YOUR tree shares your index.
    # The caller can only tell them apart if the path comes back.
    tree = "/src/shared"
    await client.post(
        "/lease",
        json=_lease_body("c-laptop", "annex geometry", repo="cwdrepo", cwd=tree),
        headers=LAPTOP,
    )
    await client.post(
        "/lease",
        json=_lease_body("c-server", "annex geometry clashes", repo="cwdrepo", cwd=tree),
        headers=SERVER,
    )
    await client.post(
        "/lease",
        json=_lease_body(
            "c-desktop", "annex geometry review", repo="cwdrepo", cwd="/src/shared-wt-2"
        ),
        headers=DESKTOP,
    )

    # My own cwd read back off the board rather than reused from the literal
    # above: the relationship these assertions describe is "the same path as
    # mine" and "a different one", so they have to be written against my path.
    # Against the literal, swapping the two expected values leaves this green.
    live = (await client.get("/active", params={"repo": "cwdrepo"}, headers=LAPTOP)).json()
    mine = _own_cwd(live, "c-laptop")
    assert mine == tree

    res = (
        await client.get(
            "/overlap",
            params={"mine": "c-laptop", "repo": "cwdrepo", "subject": "annex geometry"},
            headers=LAPTOP,
        )
    ).json()
    peers = {p["session"]: p for p in res["peers"]}
    assert peers["c-server"]["cwd"] == mine  # my tree: uncommitted files shared
    assert peers["c-desktop"]["cwd"] != mine  # its own worktree: free to work
    assert peers["c-desktop"]["cwd"] == "/src/shared-wt-2"


async def test_overlap_without_a_subject_also_reports_each_peer_cwd(client):
    # `subject` absent is a second branch of find_overlap — every same-repo peer
    # comes back, score null — and it is the branch an agent takes when the
    # question is "who else is standing in this checkout?" rather than "who is on
    # my problem?". That is the question cwd exists to answer, so the unscored
    # path has to carry it too, not just the ranked one.
    tree = "/src/nosubject"
    await client.post(
        "/lease", json=_lease_body("s-laptop", "any", repo="subjrepo", cwd=tree), headers=LAPTOP
    )
    await client.post(
        "/lease",
        json=_lease_body("s-server", "unrelated entirely", repo="subjrepo", cwd=tree),
        headers=SERVER,
    )
    await client.post(
        "/lease",
        json=_lease_body(
            "s-desktop", "also unrelated", repo="subjrepo", cwd="/src/nosubject-wt-2"
        ),
        headers=DESKTOP,
    )
    # A fourth lease on this machine that never reported a path at all.
    await client.post(
        "/lease", json=_lease_body("s-quiet", "scripted run", repo="subjrepo"), headers=LAPTOP
    )

    live = (await client.get("/active", params={"repo": "subjrepo"}, headers=LAPTOP)).json()
    mine = _own_cwd(live, "s-laptop")
    res = (
        await client.get(
            "/overlap", params={"mine": "s-laptop", "repo": "subjrepo"}, headers=LAPTOP
        )
    ).json()
    peers = {p["session"]: p for p in res["peers"]}
    # No subject was sent, so this is the unscored branch and not the ranked one —
    # the titles above deliberately share nothing, and all three still come back.
    assert set(peers) == {"s-server", "s-desktop", "s-quiet"}
    assert all(p["score"] is None for p in peers.values())
    assert peers["s-server"]["cwd"] == mine  # my tree
    assert peers["s-desktop"]["cwd"] != mine  # its own worktree
    assert peers["s-desktop"]["cwd"] == "/src/nosubject-wt-2"
    assert peers["s-quiet"]["cwd"] is None  # unknown, which is not "elsewhere"


async def test_overlap_peer_cwd_is_null_when_the_lease_never_sent_one(client):
    # A lease may carry no cwd at all (a scripted or non-git session). The field
    # must still be present and null rather than absent, so a caller can tell
    # "the board does not know where this peer is" from "the board does not
    # report this at all". Null is UNKNOWN either way — never "not in your tree".
    await client.post(
        "/lease", json=_lease_body("n-laptop", "cwdless probe", repo="nocwdrepo"), headers=LAPTOP
    )
    await client.post(
        "/lease", json=_lease_body("n-server", "cwdless probe", repo="nocwdrepo"), headers=SERVER
    )

    res = (
        await client.get(
            "/overlap",
            params={"mine": "n-laptop", "repo": "nocwdrepo", "subject": "cwdless probe"},
            headers=LAPTOP,
        )
    ).json()
    peer = next(p for p in res["peers"] if p["session"] == "n-server")
    assert "cwd" in peer
    assert peer["cwd"] is None


async def test_directed_ask_inbox_by_to_and_type(client):
    # The bidirectional close: an incumbent polls /board?to=<me>&type=ask for
    # questions a peer directed at it. Lock that filter combination.
    start = (await client.get("/board", headers=DESKTOP)).json()
    start_id = start[-1]["id"] if start else 0

    aid = (
        await client.post(
            "/post",
            json={"type": "ask", "summary": "re-run merge suite?", "to": "desktop"},
            headers=SERVER,
        )
    ).json()["id"]
    # A note to desktop and an ask to someone else must NOT show in desktop's ask inbox.
    await client.post("/post", json={"type": "note", "summary": "fyi", "to": "desktop"}, headers=SERVER)
    await client.post("/post", json={"type": "ask", "summary": "other", "to": "laptop"}, headers=SERVER)

    inbox = (
        await client.get(
            "/board",
            params={"to": "desktop", "type": "ask", "since": start_id},
            headers=DESKTOP,
        )
    ).json()
    assert [p["id"] for p in inbox] == [aid]
    assert inbox[0]["from"] == "server"
