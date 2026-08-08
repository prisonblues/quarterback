"""v2.8: published commits + derived sync advisories.

The staleness reasoning is pure (unit-tested directly); /sync runs against the
real Postgres via the shared client fixture. Each endpoint test uses its own repo
name and its own device names, so neither the append-only post log nor another
test's worktree snapshot can leak into its view.
"""

from __future__ import annotations

from app.sync import advice, has_commit, missing_published, repo_key, same_commit, worktree_state

from .conftest import LAPTOP, ZEUS

REPO = "v28repo"

A = "aaaaaaa1111111111111111111111111111111111"
B = "bbbbbbb2222222222222222222222222222222222"
C = "ccccccc3333333333333333333333333333333333"
D = "ddddddd4444444444444444444444444444444444"


def _pub(sha: str, summary: str = "x", author: str = "zeus", branch: str = "main") -> dict:
    return {"sha": sha, "from": author, "branch": branch, "summary": summary, "id": 1, "ts": "t"}


def _wt(head: str, commits: list[str], **kw) -> dict:
    return {
        "device": "laptop",
        "path": "/w",
        "branch": "main",
        "head_sha": head,
        "commits": [{"sha": s, "subject": "s"} for s in commits],
        **kw,
    }


# ---- identity + presence (pure) ---------------------------------------------

def test_repo_key_matches_slug_against_basename():
    # report_git registers "owner/name"; the hook tags posts with the basename.
    assert repo_key("prisonblues/quarterback") == repo_key("quarterback") == "quarterback"
    assert repo_key(None) is None
    assert repo_key("prisonblues/other") != repo_key("quarterback")


def test_same_commit_allows_abbreviation_but_not_short_prefixes():
    assert same_commit(A, A[:7])
    assert same_commit(A[:7], A)
    assert not same_commit(A, B)
    assert not same_commit(A, A[:6])  # below git's practical abbreviation floor
    assert not same_commit(A, None)


def test_has_commit_checks_head_and_recent_slice():
    wt = _wt(B, [B, A])
    assert has_commit(wt["head_sha"], wt["commits"], B[:7])
    assert has_commit(wt["head_sha"], wt["commits"], A)
    assert not has_commit(wt["head_sha"], wt["commits"], C)


# ---- the published-line comparison (pure) -----------------------------------

def test_missing_published_counts_only_the_unheld_prefix():
    published = [_pub(C), _pub(B), _pub(A)]  # newest-first
    wt = _wt(A, [A])
    missing = missing_published(published, wt["head_sha"], wt["commits"])
    assert [m["sha"] for m in missing] == [C, B]


def test_older_publishes_outside_the_commit_window_are_not_missing():
    # The worktree holds B but its recent slice no longer reaches A. A is an
    # ancestor of B, so it must not be reported missing — this is the guard that
    # stops a long-lived checkout being flagged stale forever.
    published = [_pub(C), _pub(B), _pub(A)]
    wt = _wt(B, [B])
    assert [m["sha"] for m in missing_published(published, wt["head_sha"], wt["commits"])] == [C]


def test_up_to_date_worktree_is_not_stale():
    state = worktree_state(_wt(C, [C, B, A]), [_pub(C), _pub(B)])
    assert state["behind_published"] == 0
    assert state["stale"] is False
    assert advice(REPO, [state]) is None


def test_self_reported_upstream_lag_alone_marks_stale():
    # No publishes on the board at all, but the device fetched and knows it's behind.
    state = worktree_state(_wt(A, [A], behind=2, upstream="origin/main"), [])
    assert state["stale"] is True and state["behind_published"] == 0
    line = advice(REPO, [state])
    assert "2 commits behind origin/main" in line and "git pull" in line


def test_advice_names_the_commit_publisher_and_local_hazards():
    state = worktree_state(_wt(A, [A], dirty=True, ahead=1), [_pub(C, "fix resolver ordering")])
    line = advice(REPO, [state])
    assert "1 published commit you don't have" in line
    assert C[:7] in line and "fix resolver ordering" in line and "from zeus" in line
    assert "git pull" in line
    assert "dirty" in line                      # don't pull onto uncommitted work
    assert "1 commit not on the remote" in line  # the other half of drift


# ---- /sync over the real board ----------------------------------------------

async def _publish(client, repo: str, sha: str, summary: str, branch: str = "main", headers=ZEUS):
    return await client.post(
        "/post",
        json={
            "type": "published",
            "summary": summary,
            "refs": [
                {"kind": "repo", "value": repo},
                {"kind": "branch", "value": branch},
                {"kind": "commit", "value": sha},
            ],
        },
        headers=headers,
    )


async def test_published_is_an_accepted_post_type(client):
    res = await _publish(client, f"{REPO}-type", A, "pushed the resolver fix")
    assert res.status_code == 200
    posted = (await client.get(f"/post/{res.json()['id']}", headers=LAPTOP)).json()
    assert posted["type"] == "published"
    assert {"kind": "commit", "value": A} in posted["refs"]


async def test_sync_flags_the_stale_worktree_and_leaves_the_current_one_alone(client):
    repo = f"{REPO}-stale"
    slug = f"prisonblues/{repo}"
    await _publish(client, repo, B, "base commit")
    await _publish(client, repo, C, "fix op-resolver ordering")

    # zeus published and holds C; laptop is still on B.
    await client.put(
        "/worktrees",
        json={
            "device": "sync-zeus",
            "worktrees": [
                {
                    "path": "/src/zeus",
                    "repo": slug,
                    "branch": "main",
                    "head": C,
                    "commits": [{"sha": C, "subject": "fix"}, {"sha": B, "subject": "base"}],
                    "upstream": "origin/main",
                    "remote_sha": C,
                    "ahead": 0,
                    "behind": 0,
                    "dirty": False,
                }
            ],
        },
        headers=ZEUS,
    )
    await client.put(
        "/worktrees",
        json={
            "device": "sync-laptop",
            "worktrees": [
                {
                    "path": "/src/laptop",
                    "repo": slug,
                    "branch": "main",
                    "head": B,
                    "commits": [{"sha": B, "subject": "base"}],
                    "upstream": "origin/main",
                    "dirty": True,
                }
            ],
        },
        headers=LAPTOP,
    )

    res = (await client.get("/sync", params={"repo": repo, "branch": "main"}, headers=LAPTOP)).json()
    assert res["stale"] is True
    by_device = {w["device"]: w for w in res["worktrees"]}
    assert by_device["sync-zeus"]["stale"] is False
    assert by_device["sync-laptop"]["behind_published"] == 1
    assert by_device["sync-laptop"]["missing"][0]["sha"] == C
    # Stale worktrees sort first so a caller reading only the head sees the action.
    assert res["worktrees"][0]["device"] == "sync-laptop"
    assert "git pull" in res["advice"] and "dirty" in res["advice"]

    # Scoped to the device that is current: nothing to do, no advice.
    mine = (
        await client.get(
            "/sync", params={"repo": repo, "branch": "main", "device": "sync-zeus"}, headers=ZEUS
        )
    ).json()
    assert mine["stale"] is False and mine["advice"] is None


async def test_sync_ignores_other_branches_and_other_repos(client):
    repo = f"{REPO}-scope"
    await _publish(client, repo, B, "on main")
    await _publish(client, repo, C, "on a side branch", branch="feat/x")
    await _publish(client, f"{repo}-neighbour", D, "different repo entirely")
    await client.put(
        "/worktrees",
        json={
            "device": "sync-branchy",
            "worktrees": [
                {"path": "/src/b", "repo": f"prisonblues/{repo}", "branch": "main", "head": B,
                 "commits": [{"sha": B, "subject": "base"}]},
            ],
        },
        headers=LAPTOP,
    )
    res = (
        await client.get(
            "/sync",
            params={"repo": repo, "branch": "main", "device": "sync-branchy"},
            headers=LAPTOP,
        )
    ).json()
    shas = [p["sha"] for p in res["published"]]
    assert shas == [B]        # the side branch and the neighbouring repo stay out
    assert res["stale"] is False


async def test_sync_matches_a_repo_slug_against_a_bare_repo_name(client):
    # The hook tags posts with the checkout basename; report_git registers the
    # origin slug. A publish must still reach the worktree it belongs to.
    repo = f"{REPO}-slug"
    await _publish(client, repo, C, "pushed from zeus")   # bare name on the post
    await client.put(
        "/worktrees",
        json={
            "device": "sync-slug",
            "worktrees": [
                {"path": "/src/s", "repo": f"prisonblues/{repo}", "branch": "main", "head": B,
                 "commits": [{"sha": B, "subject": "base"}]},   # slug on the worktree
            ],
        },
        headers=LAPTOP,
    )
    res = (
        await client.get(
            "/sync", params={"repo": repo, "device": "sync-slug"}, headers=LAPTOP
        )
    ).json()
    assert res["stale"] is True
    assert res["worktrees"][0]["missing"][0]["sha"] == C


async def test_unregistered_worktree_says_so_instead_of_reading_as_in_sync(client):
    repo = f"{REPO}-unknown"
    await _publish(client, repo, C, "pushed something")
    res = (
        await client.get(
            "/sync", params={"repo": repo, "device": "never-reported"}, headers=LAPTOP
        )
    ).json()
    assert res["registered"] is False
    assert res["stale"] is False          # we genuinely don't know — don't claim staleness
    assert "report_git" in res["advice"]  # but never answer with silence


async def test_a_feature_worktree_is_not_stale_for_lacking_a_main_publish(client):
    repo = f"{REPO}-multibranch"
    await _publish(client, repo, C, "shipped on main", branch="main")
    await client.put(
        "/worktrees",
        json={
            "device": "sync-multi",
            "worktrees": [
                {"path": "/src/main", "repo": repo, "branch": "main", "head": B,
                 "commits": [{"sha": B, "subject": "base"}]},
                {"path": "/src/feat", "repo": repo, "branch": "feat/x", "head": D,
                 "commits": [{"sha": D, "subject": "wip"}]},
            ],
        },
        headers=LAPTOP,
    )
    # Fleet view (no branch filter): each worktree judged against its own line.
    res = (await client.get("/sync", params={"repo": repo}, headers=LAPTOP)).json()
    by_path = {w["path"]: w for w in res["worktrees"]}
    assert by_path["/src/main"]["behind_published"] == 1
    assert by_path["/src/feat"]["stale"] is False


async def test_caller_gets_a_verdict_without_ever_registering(client):
    # The hook path: no report_git, just "here's what I have". Same answer.
    repo = f"{REPO}-caller"
    await _publish(client, repo, B, "base")
    await _publish(client, repo, C, "fix the resolver")
    res = (
        await client.get(
            "/sync",
            params={"repo": repo, "branch": "main", "have": f"{B},{A}", "dirty": "true"},
            headers=LAPTOP,
        )
    ).json()
    assert res["registered"] is False       # nothing registered, and it doesn't matter
    assert res["stale"] is True
    assert res["caller"]["behind_published"] == 1
    assert res["caller"]["missing"][0]["sha"] == C
    assert res["caller"]["device"] == "laptop"   # identity comes from the token
    assert "git pull" in res["advice"] and "dirty" in res["advice"]

    # Caller holding the newest publish: current, and told so by silence.
    current = (
        await client.get(
            "/sync", params={"repo": repo, "branch": "main", "have": f"{C},{B}"}, headers=LAPTOP
        )
    ).json()
    assert current["stale"] is False and current["advice"] is None


async def test_caller_verdict_wins_over_other_stale_worktrees(client):
    # A peer's stale checkout must not make *my* "am I stale?" answer yes.
    repo = f"{REPO}-callerwins"
    await _publish(client, repo, C, "shipped")
    await client.put(
        "/worktrees",
        json={
            "device": "sync-laggard",
            "worktrees": [{"path": "/src/old", "repo": repo, "branch": "main", "head": A,
                           "commits": [{"sha": A, "subject": "ancient"}]}],
        },
        headers=ZEUS,
    )
    res = (
        await client.get(
            "/sync", params={"repo": repo, "branch": "main", "have": C}, headers=LAPTOP
        )
    ).json()
    assert res["stale"] is False and res["advice"] is None
    assert res["worktrees"][0]["stale"] is True  # the peer is still reported, just not as mine


async def test_the_same_commit_announced_twice_counts_once(client):
    # A local push fires the lifecycle hook; CI announces the same commit again
    # on merge. Counting both would say "2 commits behind" when it's one.
    repo = f"{REPO}-dup"
    await _publish(client, repo, C, "fix the resolver", headers=ZEUS)
    await _publish(client, repo, C[:7], "fix the resolver", headers=LAPTOP)  # abbreviated
    res = (
        await client.get(
            "/sync", params={"repo": repo, "branch": "main", "have": B}, headers=LAPTOP
        )
    ).json()
    shas = [p["sha"] for p in res["published"]]
    assert len(shas) == 1 and C.startswith(shas[0])  # one entry, naming that commit
    assert res["caller"]["behind_published"] == 1
    assert "1 published commit" in res["advice"]

    # And a caller holding it is current, whichever form it was announced in.
    held = (
        await client.get(
            "/sync", params={"repo": repo, "branch": "main", "have": C}, headers=LAPTOP
        )
    ).json()
    assert held["stale"] is False


async def test_worktree_sync_state_round_trips(client):
    await client.put(
        "/worktrees",
        json={
            "device": "sync-rt",
            "worktrees": [
                {"path": "/src/rt", "repo": f"prisonblues/{REPO}-rt", "branch": "main", "head": A,
                 "upstream": "origin/main", "remote_sha": C, "ahead": 2, "behind": 3,
                 "dirty": True},
            ],
        },
        headers=LAPTOP,
    )
    got = (await client.get("/worktrees", params={"device": "sync-rt"}, headers=LAPTOP)).json()
    assert got[0]["upstream"] == "origin/main"
    assert got[0]["remote_sha"] == C
    assert (got[0]["ahead"], got[0]["behind"], got[0]["dirty"]) == (2, 3, True)
