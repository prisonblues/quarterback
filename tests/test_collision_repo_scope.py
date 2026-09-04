"""The collision index answering about the right repository (#714).

``GET /active`` is *the* pre-flight call: every CLAUDE.md on the fleet tells an
agent to make it before substantive work, and the MCP tool's own docstring says
*"an empty result means the coast is clear"*. Observed on lexray, with three
agents live in ``/home/rich/source/lexray``::

    active(repo="prisonblues/lexray")  ->  {"agents": [], "subagents": []}
    active(repo="lexray")              ->  pine-mist, pebble-frost, meadow-coral

The qualified spelling is the **only** one ``plan_read``, ``claim`` and every
other keyed surface accepts — ``app/claimkey.py`` 422s a bare name and the message
teaches ``owner/name`` — so an agent orienting the documented way asks the
qualified question and is told nobody is there. That is a false clean, and the
difference between it and a missing filter is that a caller acts on it.

Three defects, all reachable from that one observation, and each has tests below:

1. **The lease filter was raw equality** over a column the lifecycle hook filled
   with the checkout *basename*. Neither the shape axis (bare vs qualified) nor
   the case axis (#326's, fixed on ``/review/collisions`` and never here).
2. **``repo=`` never filtered the sub-agents at all.** ``/active`` folds leases and
   live sub-agents into one payload; only the leases were narrowed, so the half a
   caller reads as "who is in my repo" carried every sub-agent on the fleet.
3. **A spelling the column can never hold was answered rather than refused.**
   ``GET /worktrees`` and ``GET /landing`` both settled that a clone URL gets a
   422 — *an empty answer reads as "nothing gates anything here" when it means "I
   could not tell what you asked about"* — and the one endpoint whose entire job
   is collision detection accepted any string and returned ``[]``.

The write path is here too, because the read-side fold is only half of it: the
hook reports ``owner/name`` now (``harness/tests/test_qb_hook_repo_identity.py``)
and ``POST /lease`` folds its case, so the column converges instead of being
folded over forever. What it does NOT do is refuse the un-qualified half — see
:func:`app.repomatch.fold_repo` — and there is a test pinning that, because taking
a lease away from a heartbeat over an optional field would remove the agent from
the board entirely.

Repo names here are unique per test: this suite shares one database across the
whole run and other modules' leases and sub-agents are live in it, which is also
why the sub-agent assertions are about the presence of named ids rather than the
size of the list.

Nothing here imports :mod:`app.repomatch`, deliberately: these are HTTP tests, and
a module-level import of the module the fix ADDED would turn every one of them
into a collection error when they are run against the pre-fix tree — which is a
red run that demonstrates nothing. The rule's own unit tests live in
``tests/test_repomatch.py``, where that import belongs.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.db import engine

from .conftest import LAPTOP, SERVER

# --------------------------------------------------------------------------- rig


async def _lease(client, session, headers=LAPTOP, **kw):
    body = {"session": session, "device": "d", **kw}
    got = await client.post("/lease", json=body, headers=headers)
    assert got.status_code == 200, got.text
    return got.json()


async def _subagent(client, parent, agent_id, headers=LAPTOP, **kw):
    got = await client.post(
        "/subagent",
        json={"parent_session": parent, "agent_id": agent_id, **kw},
        headers=headers,
    )
    assert got.status_code == 200, got.text
    return got.json()


async def _active(client, **params):
    got = await client.get("/active", params=params, headers=SERVER)
    assert got.status_code == 200, got.text
    return got.json()


def _sessions(active: dict) -> set[str]:
    return {a["session"] for a in active["agents"]}


def _agent_ids(active: dict) -> set[str]:
    return {s["agent_id"] for s in active["subagents"]}


# ------------------------------------------------- the lease half: both spellings


async def test_the_qualified_spelling_finds_a_lease_that_reported_a_bare_name(client):
    """#714's own observation. The hook reported `lexray`; the agent had just been
    taught `prisonblues/lexray` by the tool that refuses anything else."""
    await _lease(client, "s-714-bare", repo="scopeone")

    qualified = await _active(client, repo="acme/scopeone")
    assert "s-714-bare" in _sessions(qualified), (
        "the spelling every keyed surface on this board teaches still answers "
        "that the coast is clear"
    )


async def test_the_bare_name_still_finds_it(client):
    """The spelling that worked before must keep working: a hook mid-rollout, and
    every checkout whose origin is not a GitHub remote, has only this one."""
    await _lease(client, "s-714-bare-2", repo="scopetwo")
    assert "s-714-bare-2" in _sessions(await _active(client, repo="scopetwo"))


async def test_a_lease_that_reported_owner_slash_name_is_found_by_the_bare_name(client):
    """The other direction, and the one the fix creates: once the hook reports
    `owner/name`, a caller holding only the bare name — the board TUI off a post's
    `repo` ref, an older skill — must still find the agent."""
    await _lease(client, "s-714-qualified", repo="acme/scopethree")
    assert "s-714-qualified" in _sessions(await _active(client, repo="scopethree"))


async def test_capitals_are_not_a_third_answer(client):
    """The case axis is #326's, fixed on `/review/collisions` and never here:
    `Lease.repo == repo` is case-sensitive, so `active(repo="Lexray")` was a third
    way to be told an empty board. GitHub treats these as one repository."""
    await _lease(client, "s-714-caps", repo="Acme/ScopeFour")
    for asked in ("acme/scopefour", "Acme/ScopeFour", "ACME/SCOPEFOUR", "scopefour",
                  "ScopeFour"):
        assert "s-714-caps" in _sessions(await _active(client, repo=asked)), \
            f"{asked!r} saw a different board"


async def test_a_different_owners_repo_of_the_same_name_is_not_excluded(client):
    """The one way the lease match is deliberately WIDE, pinned so a later change
    cannot narrow it by accident and reintroduce the false clean.

    The column holds both shapes, so the only thing a query can compare is the
    repository name — and two owners may share one. For a collision index that is
    the cheap error: a false positive costs a conversation with an agent working
    something else, a false negative costs two agents editing one tree. It is also
    *visible*, which an empty answer never was — the row carries its own `repo`,
    so the caller can see exactly what matched and decide."""
    await _lease(client, "s-714-owner-a", repo="alpha/scopefive")
    await _lease(client, "s-714-owner-b", repo="beta/scopefive", headers=SERVER)

    seen = await _active(client, repo="alpha/scopefive")
    rows = {a["session"]: a["repo"] for a in seen["agents"]}
    assert "s-714-owner-a" in rows
    assert rows.get("s-714-owner-b") == "beta/scopefive", (
        "the wide match must stay disclosed: the row says which repo it is in"
    )


async def test_peers_answers_the_qualified_spelling_too(client):
    """`/overlap` carried the identical filter, and it is the call the fleet's
    CLAUDE.md tells an agent to make when it starts or pivots. "No peers" is the
    answer that ends the conversation the endpoint exists to start."""
    await _lease(client, "s-714-peer", repo="scopesix", title="merge test flakiness")
    got = await client.get(
        "/overlap",
        params={"mine": "s-714-asker", "repo": "acme/scopesix"},
        headers=SERVER,
    )
    assert got.status_code == 200, got.text
    assert "s-714-peer" in {p["session"] for p in got.json()["peers"]}


# -------------------------------------------- the sub-agent half: whose repo is it


async def test_a_repo_scoped_active_does_not_report_subagents_from_another_repo(client):
    """`repo=` narrowed the leases and not the sub-agents, so the payload a caller
    reads as "who is in my repo" listed the fleet's whole fan-out. The lease bug
    reported a clean board where there were peers; this one reported peers that
    were somewhere else entirely."""
    await _lease(client, "s-714-parent-here", repo="acme/scopeseven")
    await _lease(client, "s-714-parent-away", repo="acme/scopeeight", headers=SERVER)
    await _subagent(client, "s-714-parent-here", "sa-here", cwd="/w/seven")
    await _subagent(client, "s-714-parent-away", "sa-away", headers=SERVER, cwd="/w/eight")

    seen = _agent_ids(await _active(client, repo="acme/scopeseven"))
    assert "sa-here" in seen
    assert "sa-away" not in seen, "a sub-agent in another repo counted as company"


async def test_a_subagent_is_attributed_by_either_spelling_of_its_parents_repo(client):
    """Its parent's lease is where a sub-agent's repo comes from — the Task tool
    fires no lifecycle hook, so the row itself names no repository — which means
    this half inherits the same two spellings and has to fold them the same way."""
    await _lease(client, "s-714-parent-bare", repo="scopenine")
    await _subagent(client, "s-714-parent-bare", "sa-bare", cwd="/w/nine")

    assert "sa-bare" in _agent_ids(await _active(client, repo="acme/scopenine"))
    assert "sa-bare" in _agent_ids(await _active(client, repo="scopenine"))


async def test_a_subagent_whose_parent_repo_is_unknown_is_kept(client):
    """Unknown is not "not in your repo", and on this endpoint absence must not be
    representable as a clean answer. A parent that holds no live lease — or holds
    one that never sent a repo — keeps its sub-agents in every repo-scoped answer;
    the row carries its `cwd` and `parent_session` so a caller can resolve it."""
    await _subagent(client, "s-714-orphan", "sa-orphan", cwd="/w/orphan")
    await _lease(client, "s-714-repoless")
    await _subagent(client, "s-714-repoless", "sa-repoless", cwd="/w/repoless")

    seen = _agent_ids(await _active(client, repo="acme/scopeten"))
    assert {"sa-orphan", "sa-repoless"} <= seen, (
        "a sub-agent nobody can place was dropped from a collision check"
    )


async def _second_live_lease(session: str, repo: str) -> None:
    """A second LIVE lease on one session, written past the endpoint that refuses it.

    `POST /lease` 409s a different device and renews the same one, so it will not
    produce this state — and no constraint forbids it either, which is the point.
    Two live rows for one session are reachable by a race, by a repair, or by a
    future write path, and a filter that picked one of them to answer for the
    session would decide a sub-agent's visibility by row order.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO leases (id, session, device, holder, repo, ttl_seconds, "
                "expires_at) VALUES (:id, :s, 'd2', 'server/second', :repo, 1800, "
                "now() + interval '30 minutes')"
            ),
            {"id": uuid.uuid4(), "s": session, "repo": repo},
        )


async def test_any_live_lease_of_the_parent_attributes_its_subagents(client):
    """Raised by an independent review of this change: the first cut kept the first
    non-null repo it saw per parent, so with two live leases in different repos a
    sub-agent's visibility depended on the order the rows came back in — and one of
    the two repos would be told the fan-out was somewhere else.

    Asked as "does ANY live lease match" there is no order to depend on, which is
    why both directions are asserted here: no order-dependent implementation can
    satisfy them both.

    Two DIFFERENT repository names, not two owners of one name — the lease match is
    by name (`app.repomatch.name_clause`), so `alpha/x` and `beta/x` would agree
    whichever row answered and the test would pass against the defect."""
    parent = "s-714-two-leases"
    await _lease(client, parent, repo="alpha/scopefifteen")
    await _second_live_lease(parent, "beta/scopesixteen")
    await _subagent(client, parent, "sa-two-leases", cwd="/w/fifteen")

    assert "sa-two-leases" in _agent_ids(await _active(client, repo="alpha/scopefifteen"))
    assert "sa-two-leases" in _agent_ids(await _active(client, repo="beta/scopesixteen"))
    assert "sa-two-leases" not in _agent_ids(
        await _active(client, repo="acme/scopeseventeen")
    ), "a parent the board CAN place was kept in a repo neither of its leases names"


async def test_an_ended_subagent_does_not_come_back_through_the_repo_filter(client):
    """The repo scope is a narrowing and must not reach past the liveness rule the
    endpoint already applies."""
    await _lease(client, "s-714-parent-end", repo="acme/scopeeleven")
    await _subagent(client, "s-714-parent-end", "sa-ended", cwd="/w/eleven")
    ended = await client.post(
        "/subagent/end",
        json={"parent_session": "s-714-parent-end", "agent_id": "sa-ended"},
        headers=LAPTOP,
    )
    assert ended.status_code == 200, ended.text
    assert "sa-ended" not in _agent_ids(await _active(client, repo="acme/scopeeleven"))


# ------------------------------------------------------------------- the write path


async def test_a_lease_stores_one_spelling_of_a_qualified_repo(client):
    """#326's rule — fold on the write — on the one repo column it had never
    reached. `PrisonBlues/Quarterback` and `prisonblues/quarterback` are one
    repository, and the value here is what `/active` displays, what `/lease/stage`
    threads its posts under, and what the human board renders."""
    await _lease(client, "s-714-fold", repo="  PrisonBlues/ScopeTwelve  ")
    rows = {
        a["session"]: a["repo"]
        for a in (await _active(client, repo="prisonblues/scopetwelve"))["agents"]
    }
    assert rows.get("s-714-fold") == "prisonblues/scopetwelve", rows


async def test_a_bare_name_is_stored_rather_than_refused(client):
    """The fold is not a gate, deliberately. A bare name is what a checkout with no
    GitHub remote genuinely has and what every hook older than #714 sends, and
    refusing it would take the whole lease — the agent's entire presence on the
    board — away from a heartbeat over a field that is optional to begin with."""
    got = await client.post(
        "/lease", json={"session": "s-714-open", "device": "d", "repo": "scopethirteen"},
        headers=LAPTOP,
    )
    assert got.status_code == 200, got.text
    rows = {
        a["session"]: a["repo"]
        for a in (await _active(client, repo="scopethirteen"))["agents"]
    }
    assert rows.get("s-714-open") == "scopethirteen", rows


async def test_a_blank_repo_becomes_no_repo_rather_than_a_repo_called_nothing(client):
    """An older client sending `""` means "I do not know", and a lease reporting a
    repository whose name is the empty string is a row no query can name."""
    await _lease(client, "s-714-blank", repo="   ")
    rows = {a["session"]: a["repo"] for a in (await _active(client))["agents"]}
    assert "s-714-blank" in rows, "the lease itself went missing"
    assert rows["s-714-blank"] is None, rows["s-714-blank"]


# ----------------------------------------------------- absence is not a clean answer


#: Spellings that are neither `owner/name` nor a bare repository name — the ones
#: `GET /worktrees` and `GET /landing` already refuse. Kept here rather than
#: imported so this file states its own vectors; `tests/test_repo_identity.py`
#: holds the fleet-wide list and now sweeps these two endpoints with it too.
NEITHER = [
    "https://github.com/prisonblues/quarterback",
    "git@github.com:prisonblues/quarterback.git",
    "prisonblues/quarterback.git",
    "/etc/passwd",
    "a/b/c",
    "quarterback.git",
    "quarterback/",
    "/quarterback",
    "quarter back",
]


@pytest.mark.parametrize("repo", NEITHER)
async def test_the_collision_index_refuses_a_spelling_it_could_never_match(client, repo):
    """The endpoint where absence must not be representable as a clean answer, and
    the one that accepted any string at all and answered `[]` for it."""
    got = await client.get("/active", params={"repo": repo}, headers=SERVER)
    assert got.status_code == 422, f"accepted {repo!r}: {got.text}"


@pytest.mark.parametrize("repo", NEITHER)
async def test_peer_discovery_refuses_them_too(client, repo):
    """`peers()` is the other half of the same orientation, with the same stake in
    the difference between "nobody is here" and "I did not understand you"."""
    got = await client.get(
        "/overlap", params={"mine": "s-714-asker", "repo": repo}, headers=SERVER
    )
    assert got.status_code == 422, f"accepted {repo!r}: {got.text}"


async def test_the_refusal_says_what_shape_is_wanted(client):
    """A 422 that did not name the accepted spellings would just be a quieter dead
    end. It is the board's own `REPO_SHAPE`, so every refusal reads the same."""
    got = await client.get("/active", params={"repo": "a/b/c"}, headers=SERVER)
    assert got.status_code == 422
    detail = got.json()["detail"]
    assert "owner/name" in detail["error"]
    assert detail["repo"] == "a/b/c"
