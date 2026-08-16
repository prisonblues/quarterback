"""The origin watch: a merge nobody pushed still reaches the board (#127).

The gap these cover: `gh pr merge` and the green button create the merge commit
server-side, so no machine runs a command the publish hook can see, and the
board's staleness advisory goes quiet about the most common way `main` moves.

GitHub is faked with `httpx.MockTransport` — the poller's two reads are ordinary
GETs, so a transport substitution tests the real request path (headers, status
handling, rate-limit parsing) without the network. The seam is the client, which
`poll_cycle` takes as an argument for exactly this reason.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app import github, origin
from app.db import async_session

from .conftest import SERVER

REPO = "prisonblues/watched"
OLD = "1111111111111111111111111111111111111111"
NEW = "2222222222222222222222222222222222222222"


def _sha(seed: str) -> str:
    """A distinct 40-char SHA per test.

    The suite builds the schema once and shares the database across tests, so a
    commit one test announces is on the board for the next one — and the whole
    job of `already_announced` is to not announce it twice. Tests that assert an
    announcement therefore need a commit no other test has published.
    """
    return (seed * 40)[:40]


def _github(heads: dict[str, str], *, default_branch: str = "main",
            remaining: str = "4999", calls: list[str] | None = None) -> httpx.AsyncClient:
    """A fake github.com serving `heads` as repo -> head sha."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        headers = {"X-RateLimit-Remaining": remaining}
        path = request.url.path
        if path.endswith(f"/commits/{default_branch}"):
            repo = path[len("/repos/"):-len(f"/commits/{default_branch}")]
            sha = heads.get(repo)
            if sha is None:
                return httpx.Response(404, json={"message": "Not Found"}, headers=headers)
            return httpx.Response(
                200,
                json={"sha": sha, "commit": {"message": "Merge pull request #7 from x/y\n\nbody"}},
                headers=headers,
            )
        if path.startswith("/repos/"):
            repo = path[len("/repos/"):]
            if repo not in heads:
                return httpx.Response(404, json={"message": "Not Found"}, headers=headers)
            return httpx.Response(200, json={"default_branch": default_branch}, headers=headers)
        return httpx.Response(404, json={"message": "Not Found"}, headers=headers)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _clean_module_state():
    """The branch cache, backoff and rate counter are process-global by design."""

    def reset():
        origin._default_branches.clear()
        origin._cooldown.clear()
        origin._failures.clear()
        github._remaining = None

    reset()
    yield
    reset()


async def _register(client, repo: str, *, head: str, device: str = "watch-dev",
                    branch: str = "main", path: str = "/src/watched"):
    return await client.put(
        "/worktrees",
        json={
            "device": device,
            "worktrees": [
                {
                    "path": path,
                    "repo": repo,
                    "branch": branch,
                    "head": head,
                    "commits": [{"sha": head, "subject": "held"}],
                    "upstream": f"origin/{branch}",
                    "remote_sha": head,
                    "ahead": 0,
                    "behind": 0,
                    "dirty": False,
                }
            ],
        },
        headers=SERVER,
    )


# ---- what gets polled --------------------------------------------------------


async def test_only_registered_github_slugs_are_polled(client):
    # report_git stamps owner/name for a GitHub remote and NULL otherwise, and a
    # bare name cannot be addressed on the API. Polling it would 404 every cycle.
    await _register(client, REPO, head=OLD, device="watch-slug")
    await _register(client, "bare-name", head=OLD, device="watch-bare", path="/src/bare")
    async with async_session() as db:
        assert REPO in await origin.watched_repos(db)
        assert "bare-name" not in await origin.watched_repos(db)


# ---- the announcement --------------------------------------------------------


async def _published_post(client, sha: str) -> dict:
    posts = (await client.get("/board?type=published&limit=200", headers=SERVER)).json()
    return next(p for p in posts if any(r.get("value") == sha for r in (p["refs"] or [])))


async def test_a_server_side_merge_is_announced_as_published(client):
    repo, new = "prisonblues/announce", _sha("a")
    await _register(client, repo, head=OLD, device="watch-announce", path="/src/announce")
    async with _github({repo: new}) as gh, async_session() as db:
        assert await origin.poll_cycle(db, gh) == [new]

    post = await _published_post(client, new)
    assert post["type"] == "published"
    assert post["summary"] == "Merge pull request #7 from x/y"   # subject line, not the body
    refs = {(r["kind"], r["value"]) for r in post["refs"]}
    assert ("commit", new) in refs
    assert ("repo", repo) in refs
    assert ("branch", "main") in refs        # without this it would apply to every branch line


async def test_the_merge_is_not_attributed_to_whoever_noticed_it(client):
    # #127's one explicit constraint: a poller is a noticer, not the author. A
    # merge showing up as published *by the agent that spotted it* is worse than
    # one showing up unattributed.
    repo, new = "prisonblues/author", _sha("b")
    await _register(client, repo, head=OLD, device="watch-author", path="/src/author")
    async with _github({repo: new}) as gh, async_session() as db:
        await origin.poll_cycle(db, gh)

    post = await _published_post(client, new)
    assert post["from"] == "github"
    assert "/" not in post["from"]           # not a machine/agent identity
    assert post["session"] is None


# ---- not saying it twice -----------------------------------------------------


async def test_a_commit_ci_already_published_is_not_announced_again(client):
    # CI announces most merges first. Counting the same commit twice reports a
    # checkout as two commits behind when it is one.
    repo, new = "prisonblues/dupe", _sha("e")
    await _register(client, repo, head=OLD, device="watch-dupe", path="/src/dupe")
    await client.post(
        "/post",
        json={
            "type": "published",
            "summary": "Merge pull request #7 from x/y",
            "refs": [
                {"kind": "repo", "value": repo},
                {"kind": "commit", "value": new},
                {"kind": "branch", "value": "main"},
            ],
        },
        headers=SERVER,
    )
    async with _github({repo: new}) as gh, async_session() as db:
        assert await origin.poll_cycle(db, gh) == []


async def test_an_abbreviated_prior_announcement_still_suppresses(client):
    # Announcements abbreviate differently — the hook posts a full SHA, a
    # hand-written post may not. Matching must be prefix-wise, as /sync's is.
    repo, new = "prisonblues/abbrev", _sha("f")
    await _register(client, repo, head=OLD, device="watch-abbrev", path="/src/abbrev")
    await client.post(
        "/post",
        json={
            "type": "published",
            "summary": "landed it by hand",
            "refs": [
                {"kind": "repo", "value": "abbrev"},         # basename, as the hook tags them
                {"kind": "commit", "value": new[:8]},
            ],
        },
        headers=SERVER,
    )
    async with _github({repo: new}) as gh, async_session() as db:
        assert await origin.poll_cycle(db, gh) == []


async def test_cis_own_announce_payload_suppresses_the_poll(client):
    # The composition test for #143 + #145. The two above assert the dedup against
    # payloads this file made up; this one asserts it against the bytes
    # `announce-board.yml` actually sends, which are neither shape exactly: the
    # commit ref carries the full slug while the *repo* ref carries only the
    # basename (`$r|split("/")|last`), and that repo ref is the one
    # `already_announced` reads. If the two mechanisms ever drift apart, they
    # drift here, and the symptom is a checkout told it is two commits behind
    # when it is one.
    repo, new = "prisonblues/composed", _sha("z")
    await _register(client, repo, head=OLD, device="watch-composed", path="/src/composed")
    await client.post(
        "/post",
        json={
            "type": "published",
            "summary": "Merge pull request #143 from prisonblues/fix/issue-125-127",
            "refs": [
                {"kind": "commit", "value": new, "repo": repo},
                {"kind": "repo", "value": repo.split("/")[-1]},
                {"kind": "branch", "value": "main"},
            ],
        },
        headers=SERVER,
    )
    async with _github({repo: new}) as gh, async_session() as db:
        assert await origin.poll_cycle(db, gh) == []


async def test_a_second_cycle_over_an_unchanged_head_says_nothing(client):
    repo, new = "prisonblues/quiet", _sha("c")
    await _register(client, repo, head=OLD, device="watch-quiet", path="/src/quiet")
    async with _github({repo: new}) as gh, async_session() as db:
        assert await origin.poll_cycle(db, gh) == [new]
        assert await origin.poll_cycle(db, gh) == []


async def test_the_default_branch_is_learned_once_not_every_cycle(client):
    repo, new = "prisonblues/cache", _sha("d")
    await _register(client, repo, head=OLD, device="watch-cache", path="/src/cache")
    calls: list[str] = []
    async with _github({repo: new}, calls=calls) as gh, async_session() as db:
        await origin.poll_cycle(db, gh)
        await origin.poll_cycle(db, gh)
    assert calls.count(f"/repos/{repo}") == 1
    assert calls.count(f"/repos/{repo}/commits/main") == 2


# ---- the point of the whole thing --------------------------------------------


async def test_the_advisory_now_fires_for_a_merge_nobody_pushed(client):
    # End to end, and the reason #127 exists: a worktree sitting on the old head
    # is told to pull, from an event no machine generated.
    repo, new = "prisonblues/advisory", _sha("7")
    await _register(client, repo, head=OLD, device="watch-advice", path="/src/advisory")

    before = (await client.get(f"/sync?repo={repo}&device=watch-advice", headers=SERVER)).json()
    assert before["advice"] is None

    async with _github({repo: new}) as gh, async_session() as db:
        await origin.poll_cycle(db, gh)

    after = (await client.get(f"/sync?repo={repo}&device=watch-advice", headers=SERVER)).json()
    assert after["advice"] is not None
    assert new[:7] in after["advice"]
    assert "from github" in after["advice"]
    assert "git pull" in after["advice"]


# ---- staying up ---------------------------------------------------------------


async def test_a_repo_github_will_not_answer_for_does_not_stop_the_others(client):
    # A repo renamed, made private, or briefly 500ing must cost itself its turn
    # and nothing else. Registered under one device so both are in one cycle.
    await client.put(
        "/worktrees",
        json={
            "device": "watch-mixed",
            "worktrees": [
                {"path": "/src/gone", "repo": "prisonblues/gone", "branch": "main",
                 "head": OLD, "commits": [], "upstream": "origin/main",
                 "remote_sha": OLD, "ahead": 0, "behind": 0, "dirty": False},
                {"path": "/src/live", "repo": "prisonblues/live", "branch": "main",
                 "head": OLD, "commits": [], "upstream": "origin/main",
                 "remote_sha": OLD, "ahead": 0, "behind": 0, "dirty": False},
            ],
        },
        headers=SERVER,
    )
    live = _sha("9")
    async with _github({"prisonblues/live": live}) as gh, async_session() as db:
        assert await origin.poll_cycle(db, gh) == [live]


async def test_a_dead_repo_stops_costing_a_call_every_single_cycle(client):
    # Found by smoke-testing against the real API, not by the mocks: one cycle
    # spent 17 calls of an anonymous budget of 60 on repos that no longer exist,
    # and nothing stopped it doing that again minutes later. A registration for
    # a repo that has been renamed, deleted or made private is normal; paying for
    # it for ever is not.
    await _register(client, "prisonblues/dead", head=OLD, device="watch-dead", path="/src/dead")
    calls: list[str] = []
    asked: list[int] = []
    async with _github({}, calls=calls) as gh, async_session() as db:
        for _ in range(5):
            await origin.poll_cycle(db, gh)
            asked.append(calls.count("/repos/prisonblues/dead"))

    # Asks on cycle 1, sits out 2, retries on 3, then sits out 4 and 5 — the
    # backoff doubling each time it is disappointed. The property that matters
    # is that the cost stops being one call per cycle.
    assert asked == [1, 1, 2, 2, 2]


async def test_the_backoff_grows_and_is_capped(client):
    repo = "prisonblues/flaky"
    for expected in (1, 2, 4, 8):
        origin._note_failure(repo)
        assert origin._cooldown[repo] == expected
    origin._failures[repo] = 99
    origin._note_failure(repo)
    assert origin._cooldown[repo] == origin.MAX_COOLDOWN


async def test_a_repo_that_comes_back_is_polled_again(client):
    # Backoff, not a blacklist: a private window or an outage must recover.
    repo, new = "prisonblues/revived", _sha("6")
    await _register(client, repo, head=OLD, device="watch-revive", path="/src/revived")
    async with _github({}) as gh, async_session() as db:
        assert await origin.poll_cycle(db, gh) == []
    origin._cooldown.clear()                      # the cooldown elapses
    async with _github({repo: new}) as gh, async_session() as db:
        assert await origin.poll_cycle(db, gh) == [new]
    assert repo not in origin._cooldown           # success clears the backoff


async def test_a_registration_nobody_has_refreshed_in_a_month_is_not_polled(client):
    # Every watched repo costs a call per cycle out of a shared budget. A row
    # `report_git` has not rewritten in a month is a checkout that is gone.
    from sqlalchemy import update

    from app.models.worktree import Worktree

    repo = "prisonblues/abandoned"
    await _register(client, repo, head=OLD, device="watch-old", path="/src/abandoned")
    async with async_session() as db:
        assert repo in await origin.watched_repos(db)
        await db.execute(
            update(Worktree)
            .where(Worktree.device == "watch-old")
            .values(updated_at=datetime.now(UTC) - timedelta(days=45))
        )
        await db.commit()
        assert repo not in await origin.watched_repos(db)


async def test_an_untokened_404_names_the_credential_as_the_likely_cause(client, caplog):
    # 42 of this account's 46 repos are private, and anonymously a private repo
    # 404s exactly like a deleted one — so an untokened deploy watches nothing
    # that matters and the only trace is this line. A bare "-> 404" would send
    # whoever reads it looking for a renamed repo.
    repo = "prisonblues/private-ish"
    await _register(client, repo, head=OLD, device="watch-private", path="/src/priv")

    def not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    transport = httpx.MockTransport(not_found)
    with caplog.at_level("WARNING", logger="app.github"):
        async with httpx.AsyncClient(transport=transport) as gh, async_session() as db:
            assert await origin.poll_cycle(db, gh) == []
    assert "no token configured" in caplog.text
    assert "DEPLOY.md" in caplog.text


async def test_a_transport_error_is_swallowed_rather_than_killing_the_cycle(client):
    await _register(client, REPO, head=OLD, device="watch-broken")

    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    transport = httpx.MockTransport(explode)
    async with httpx.AsyncClient(transport=transport) as gh, async_session() as db:
        assert await origin.poll_cycle(db, gh) == []


async def test_the_cycle_sits_out_when_the_github_budget_is_nearly_gone(client):
    # Measured, not assumed: a 304 still costs a unit, so the anonymous ceiling
    # of 60/hour is real and shared with everything else on the egress IP.
    repo = "prisonblues/budget"
    await _register(client, repo, head=OLD, device="watch-budget", path="/src/budget")
    async with _github({repo: _sha("8")}, remaining="3") as gh, async_session() as db:
        await origin.poll_cycle(db, gh)          # spends one call, learns it is low
        assert github.budget_spent()
        assert await origin.poll_cycle(db, gh) == []


async def test_the_poller_is_off_under_the_suite():
    # Pinned to 0 in conftest: an unpinned default of 300 would have any test
    # that ran the lifespan calling github.com for real and writing posts.
    from app.config import settings

    assert settings.github_poll_seconds == 0
