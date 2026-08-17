"""`/sync` stops answering "in sync" when what it means is "I didn't look" (#125).

The board's staleness verdict is a comparison against the ``published`` line, so
a repo with nothing on that line gets ``stale: false`` — which reads as "you're
current" and means "we have nothing to compare against". Those are different
answers and callers need them apart.
"""

from __future__ import annotations

from .conftest import LAPTOP, SERVER

REPO = "syncrepo"

B = "bbbbbbb2222222222222222222222222222222222"
C = "ccccccc3333333333333333333333333333333333"


async def _publish(client, repo: str, sha: str, summary: str, branch: str = "main"):
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
        headers=SERVER,
    )


async def test_a_repo_with_no_published_line_is_not_comparable(client):
    # nix-fleet's shape: no CI, so nothing ever announces, so the board holds no
    # published line for it at all. The verdict must not read as a clean bill.
    res = (
        await client.get(
            "/sync",
            params={"repo": f"{REPO}-silent", "branch": "main", "have": f"{C},{B}"},
            headers=LAPTOP,
        )
    ).json()
    assert res["comparable"] is False
    assert res["published"] == []
    assert res["stale"] is False  # we genuinely don't know — don't claim staleness


async def test_one_publish_is_enough_to_be_comparable(client):
    repo = f"{REPO}-comparable"
    await _publish(client, repo, C, "shipped")
    res = (
        await client.get(
            "/sync", params={"repo": repo, "branch": "main", "have": f"{C},{B}"}, headers=LAPTOP
        )
    ).json()
    assert res["comparable"] is True
    assert res["stale"] is False and res["advice"] is None  # current, and told so by silence


async def test_no_published_line_and_no_upstream_breaks_silence(client):
    # Both sources absent: nothing published, and the caller sends no `behind`
    # because it has no upstream to count against. Silence here would be the one
    # answer we can't support.
    res = (
        await client.get(
            "/sync",
            params={"repo": f"{REPO}-blind", "branch": "main", "have": f"{C},{B}"},
            headers=LAPTOP,
        )
    ).json()
    assert res["comparable"] is False
    assert res["advice"] is not None
    assert "unknown" in res["advice"]


async def test_a_local_upstream_count_keeps_the_advisory_quiet(client):
    # Deliberately narrow: with `behind` present we still hold the weak local
    # signal, so we stay quiet rather than nag a CI-less repo every session.
    res = (
        await client.get(
            "/sync",
            params={
                "repo": f"{REPO}-quiet",
                "branch": "main",
                "have": f"{C},{B}",
                "behind": "0",
            },
            headers=LAPTOP,
        )
    ).json()
    assert res["comparable"] is False  # still can't compare...
    assert res["advice"] is None  # ...but the local ref is a signal, so no nag


async def test_the_weak_local_signal_still_reports_staleness(client):
    # `behind` can only under-report (a stale @{u} ref counts too few commits,
    # never too many), so a non-zero count is a true positive even unfetched.
    res = (
        await client.get(
            "/sync",
            params={
                "repo": f"{REPO}-behind",
                "branch": "main",
                "have": f"{C},{B}",
                "behind": "3",
            },
            headers=LAPTOP,
        )
    ).json()
    assert res["stale"] is True
    assert "3 commits behind" in res["advice"]


async def test_comparable_is_judged_on_the_branch_line_asked_about(client):
    # A publish onto main says nothing about whether a feature line is comparable.
    repo = f"{REPO}-branchline"
    await _publish(client, repo, C, "shipped on main", branch="main")
    feat = (
        await client.get(
            "/sync", params={"repo": repo, "branch": "feat/x", "have": B}, headers=LAPTOP
        )
    ).json()
    assert feat["comparable"] is False
    main = (
        await client.get(
            "/sync", params={"repo": repo, "branch": "main", "have": B}, headers=LAPTOP
        )
    ).json()
    assert main["comparable"] is True


async def test_an_unregistered_scoped_worktree_keeps_its_own_answer(client):
    # The pre-existing fallback must win: "not registered" is more actionable
    # than "not comparable", and both can be true at once.
    repo = f"{REPO}-scoped"
    res = (
        await client.get("/sync", params={"repo": repo, "device": "never-reported"}, headers=LAPTOP)
    ).json()
    assert res["comparable"] is False
    assert "report_git" in res["advice"]
