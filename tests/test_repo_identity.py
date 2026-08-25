"""One repo has one spelling, because nobody spells it.

#148 was not a naming problem and the fix is not a name parser. The key was
caller-supplied text, and an agent asked "which repo is this" answers with
whichever spelling it has to hand: `quarterback` from the directory it stands in,
`prisonblues/quarterback` from the remote. Both true, not equal. One repo, two
counters, 2.36 issued twice.

The rejected repair was to accept every spelling and reconcile them — PR #152,
closed. That input domain is open (bare names, `.git` suffixes, URLs, scp
remotes) and three review rounds found three more holes in the enumeration, each
one the previous fix overshooting. An alias set that can be incomplete will be.

So the domain is closed instead, in two halves that have to stay in step:

* the MCP tools do not take a repo. They read `owner/name` from
  `remote.origin.url` — which `sync_status` and `report_git` were already doing,
  six lines away in the same server.
* the endpoints accept `owner/name` and refuse everything else with a 422, so a
  caller arriving another way gets an answer rather than a new namespace.

Both halves are pinned here. The second without the first is a rule agents keep
tripping over; the first without the second is a convention.

**Where this rule LIVES moved in #172, and the tests moved with it.** The
allocator that first needed it is deleted; the shape rule outlived it, because a
repo name is now half of every derived claim key (`app.claimkey`). So the same
adversarial spellings are asserted against the surfaces that key on a repo today:
claiming a resource by ref, asking what you hold in a repo, and adding a plan item.
That is not a weaker test — it is the same rule on strictly more paths, and the
one it used to guard no longer exists to be guarded.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.claimkey import BadRef, canonical, canonical_repo, derive

from .conftest import LAPTOP, PINNED_SETTINGS

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_SERVER = REPO_ROOT / "mcp" / "mcp_server" / "server.py"

#: The spellings that reached the board before this release, plus the ones the
#: closed parser was still failing on in its third round.
NOT_A_REPO = [
    "quarterback",                                  # the #148 collision itself
    "https://github.com/prisonblues/quarterback",
    "git@github.com:prisonblues/quarterback.git",
    "prisonblues/quarterback.git",
    "https:///etc/passwd",                          # #152 round 3, still live when closed
    "/etc/passwd",
    "../etc/passwd",
    "a/b/c",
]


@pytest.mark.parametrize("repo", NOT_A_REPO)
def test_only_owner_slash_name_is_a_repo(repo):
    with pytest.raises(BadRef):
        canonical_repo(repo)


@pytest.mark.parametrize("repo", NOT_A_REPO)
async def test_a_claim_by_ref_refuses_every_other_spelling(client, repo):
    """The write path. A `ref` the board cannot key is a 422, not a guess — the
    whole of #148's fix, now guarding the key rather than the version number."""
    r = await client.post("/claim", json={
        "ref": {"kind": "issue", "repo": repo, "value": "172"}, "note": "t"},
        headers=LAPTOP)
    assert r.status_code == 422, f"{repo!r} was accepted: {r.text}"


@pytest.mark.parametrize("repo", NOT_A_REPO)
async def test_the_held_check_refuses_them_too(client, repo):
    """The read path matters as much as the write path: asking about one spelling
    and being answered about a different repo is how a caller concludes it holds
    something it does not — and this is the read a pickup gate makes."""
    r = await client.get("/claim/held", params={"repo": repo}, headers=LAPTOP)
    assert r.status_code == 422, f"{repo!r} was accepted: {r.text}"


@pytest.mark.parametrize("repo", NOT_A_REPO)
async def test_the_plan_refuses_them_as_a_scope(client, repo):
    """`_norm_scope` used to lower-case and accept anything. That made `Acme/Repo`
    and `acme/repo` agree while leaving `quarterback` and `prisonblues/quarterback`
    disagreeing — and since #172 the scope is half of the item's claim key, so the
    two-spellings defect would key one issue two ways."""
    r = await client.post("/plan/item", json={
        "title": "t", "repo": repo, "ref_kind": "issue", "ref_value": "9001"},
        headers=LAPTOP)
    assert r.status_code == 422, f"{repo!r} was accepted: {r.text}"


async def test_the_canonical_spelling_still_works(client):
    r = await client.post("/claim", json={
        "ref": {"kind": "issue", "repo": "acme/widget", "value": "7"}, "note": "t"},
        headers=LAPTOP)
    assert r.status_code == 200, r.text
    assert r.json()["key"] == "acme/widget#7"


async def test_the_refusal_says_what_shape_is_wanted(client):
    """A 422 that only says "invalid" leaves the caller guessing, and guessing at
    a repo name is the bug. It also has to say the caller should not be typing
    this at all, or the next agent carefully supplies a better-spelled string."""
    r = await client.post("/claim", json={
        "ref": {"kind": "issue", "repo": "quarterback", "value": "1"}}, headers=LAPTOP)
    body = r.text
    assert "owner/name" in body
    assert "origin remote" in body


def test_case_is_folded_rather_than_treated_as_a_second_repo():
    """GitHub is case-insensitive and case-preserving, so `Acme/Widget` and
    `acme/widget` are one repository that can be cloned with either remote — #148
    again, in a spelling the shape rule alone lets through. Refusing the
    capitalised form would strand a repo genuinely named `acme/MyProject`, so it
    folds. This is the only normalisation here and it is safe for the reason the
    parser was not: `lower()` is total, so it has no next case to miss."""
    assert derive("issue", repo="Acme/CaseFold", value="12") == \
        derive("issue", repo="acme/casefold", value="12")


async def test_two_spellings_of_one_repo_can_no_longer_both_claim(client):
    """#148, as a test. Before this, these two calls were two namespaces — the
    second caller being told it had the resource, and being wrong. Now only one of
    them is a repo at all."""
    first = await client.post("/claim", json={
        "ref": {"kind": "issue", "repo": "prisonblues/quarterback", "value": "148"}},
        headers=LAPTOP)
    assert first.status_code == 200, first.text
    second = await client.post("/claim", json={
        "ref": {"kind": "issue", "repo": "quarterback", "value": "148"}}, headers=LAPTOP)
    assert second.status_code == 422, "the bare spelling opened a second namespace"


def test_an_unparseable_key_is_left_alone_rather_than_reshaped():
    """The counterweight, and the reason #152 was closed: canonicalisation that
    guesses at an open domain is the mistake. A real claim on this board reads
    `prisonblues/lexray:serving-row:32022R2554` — a database row, not a branch —
    and nothing here understands it well enough to rewrite it."""
    assert canonical("work", "prisonblues/lexray:serving-row:32022R2554") == \
        ("work", "prisonblues/lexray:serving-row:32022R2554")


# ----------------------------------------- the same rule, on the review tables

#: One `POST /review` body, minus the repo — the panel's smallest legal run.
def _run(repo: str, pr: int, **over) -> dict:
    return {"repo": repo, "pr": pr, "judged": True, "judge_model": "opus",
            "reviewers_selected": ["claude"],
            "reviewers": {"claude": {"model": "opus", "ran": True}},
            "to_fix": [], "dismissed": [], "sonar_findings": [], **over}


async def test_a_run_is_stored_under_one_spelling_however_the_panel_sent_it(client):
    """#326's root cause. `review_runs.repo` was stored exactly as the panel sent
    it and every read compared it with `==`, so two checkouts with two remotes put
    one repository on the board twice — and each spelling's queries answered about
    half of it. The fold happens on the write, so there is only ever one."""
    r = await client.post("/review", json=_run("Acme/CaseFold", 9101), headers=LAPTOP)
    assert r.status_code == 201, r.text

    for asked in ("acme/casefold", "Acme/CaseFold", "ACME/CASEFOLD"):
        listed = await client.get("/reviews", params={"repo": asked}, headers=LAPTOP)
        assert listed.status_code == 200, listed.text
        prs = [row["pr"] for row in listed.json()]
        assert 9101 in prs, f"{asked!r} could not see the run it recorded"
        assert [row["repo"] for row in listed.json() if row["pr"] == 9101] == \
            ["acme/casefold"]

        # And the aggregate, which is where a second spelling shows up as a
        # second repository: `/review/stats` counts `DISTINCT repo`, which no
        # fold in a WHERE clause could ever have corrected. Its `window` echoes
        # the spelling it filtered on, so the two cannot disagree.
        stats = await client.get("/review/stats",
                                 params={"repo": asked, "judged_only": "false"},
                                 headers=LAPTOP)
        assert stats.status_code == 200, stats.text
        assert stats.json()["window"]["repo"] == "acme/casefold"
        assert stats.json()["repos"] == 1, f"{asked!r} saw more than one repository"


async def test_an_outcome_recorded_under_capitals_settles_the_same_defect(client):
    """The other half of the review pair, and the one with a unique constraint on
    it. `review_finding_outcomes` is UNIQUE on `(repo, pr, finding_key)`, so a
    second spelling is a second terminal answer to "what happened to this
    defect?" — and the batch used to be rejected wholesale instead, with "no
    finding with this key on this PR", which points the caller at its keys."""
    r = await client.post("/review", json=_run(
        "acme/outcomefold", 9102,
        to_fix=[{"key": "F-1", "title": "a defect", "file": "a.py",
                 "reviewer": "claude", "severity": "major"}]), headers=LAPTOP)
    assert r.status_code == 201, r.text

    recorded = await client.post("/review/outcomes", json={
        "repo": "Acme/OutcomeFold", "pr": 9102,
        "outcomes": [{"key": "F-1", "outcome": "fixed"}]}, headers=LAPTOP)
    assert recorded.status_code in (200, 201), recorded.text
    body = recorded.json()
    assert body["recorded"] == ["F-1"], body
    assert body["rejected"] == [], body
    assert body["repo"] == "acme/outcomefold"


@pytest.mark.parametrize("path,params", [
    ("/reviews", {}),
    ("/review/stats", {}),
    ("/review/findings", {"pr": 1}),
    ("/review/collisions", {"pr": 1}),
    ("/review/needs-human", {}),
    ("/review/spend", {}),
])
@pytest.mark.parametrize("repo", NOT_A_REPO)
async def test_every_review_read_refuses_a_spelling_that_is_not_a_repo(
        client, path, params, repo):
    """The read path matters as much as the write path, and on these endpoints it
    matters more: a spelling the board cannot key used to come back as an empty
    list or a 404 saying the PR was never panelled, which is a confident answer
    made of not having matched anything."""
    r = await client.get(path, params={"repo": repo, **params}, headers=LAPTOP)
    assert r.status_code == 422, f"{path} accepted {repo!r}: {r.text}"


@pytest.mark.parametrize("table", ["review_runs", "review_finding_outcomes",
                                   "dial_settings", "worktrees"])
async def test_the_database_refuses_a_second_spelling_of_one_repo(client, table):
    """What actually closes the class, rather than fixing the endpoint twice.

    #232 folded one read site, this endpoint was the second instance, and the
    audit for #326 found twelve more — including a `COUNT(DISTINCT repo)` and a
    Python dict keyed on `(repo, pr, finding_key)`, neither of which a
    `func.lower()` in a WHERE clause can reach. A validator on today's write paths
    would leave tomorrow's to remember. The CHECK constraint means it cannot
    forget: an INSERT that bypasses the API entirely still fails.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from app.db import engine

    rows = {
        "review_runs": "INSERT INTO review_runs (author, repo, pr) "
                       "VALUES ('zeus/x', 'Acme/Sneaky', 1)",
        "review_finding_outcomes":
            "INSERT INTO review_finding_outcomes (repo, pr, finding_key, outcome, "
            "set_by) VALUES ('Acme/Sneaky', 1, 'F-1', 'fixed', 'zeus/x')",
        # #350's two, closed the same way and for a sharper reason: a second
        # spelling in `dial_settings` is a second LIVE ROW under a unique index,
        # which no read can undo, and one in `worktrees` is a repository two
        # endpoints then disagree about.
        "dial_settings":
            "INSERT INTO dial_settings (repo, dial, value, reason, set_by) VALUES "
            "('Acme/Sneaky', 'review_panel.max_rounds', '{\"value\": 1}', 'r', 'rich')",
        "worktrees": "INSERT INTO worktrees (device, path, repo) "
                     "VALUES ('zeus', '/sneaky', 'Acme/Sneaky')",
    }
    with pytest.raises(IntegrityError) as e:
        async with engine.begin() as conn:
            await conn.execute(text(rows[table]))
    assert "repo_canonical" in str(e.value)


# -------------------------------- the same rule, on the dial and worktree tables

#: `POST /dials` is human-gated (see `app.api.dials`), so these need the edge
#: secret the suite pins rather than a machine token.
HUMAN = {"Remote-User": "rich", "X-Edge-Auth": PINNED_SETTINGS["HUMAN_EDGE_SECRET"]}


async def _set_dial(client, repo, dial, value):
    return await client.post("/dials", json={
        "dial": dial, "value": value, "reason": "pinning the rule", "repo": repo},
        headers=HUMAN)


async def test_a_dial_set_with_capitals_is_in_force_for_the_canonical_spelling(client):
    """#350's sharpest half. `_norm_repo` checked the shape and never lower-cased —
    the one repo validator on this board that did one without the other — while
    `harness_rules.detect_github` reads the repo off the origin remote and keeps
    its capitals. So which value a review ran under depended on how the remote was
    spelled."""
    dial = "review_panel.fix_severity_floor"
    r = await _set_dial(client, "Acme/CaseDial", dial, "P2")
    assert r.status_code == 200, r.text
    assert r.json()["dial"]["repo"] == "acme/casedial"

    for asked in ("acme/casedial", "Acme/CaseDial", "ACME/CASEDIAL"):
        got = await client.get("/dials", params={"repo": asked}, headers=LAPTOP)
        assert got.status_code == 200, got.text
        mine = [d for d in got.json()["dials"] if d["dial"] == dial]
        assert [(d["repo"], d["value"]) for d in mine] == [("acme/casedial", "P2")], \
            f"{asked!r} could not see the dial it set"


async def test_one_dial_cannot_hold_two_live_values_under_two_spellings(client):
    """The consequence a read-side fold could never have reached.
    `ix_dial_settings_live` is UNIQUE over `COALESCE(repo,'')` and `dial` where
    `cleared_at IS NULL`, so two spellings were two rows in the index: two answers
    to a settings question that has one, and a resolver seeing whichever the
    planner returned. Setting it the second way now REPLACES the first, which is
    what the endpoint has always promised."""
    dial = "review_panel.max_rounds"
    assert (await _set_dial(client, "Acme/TwiceDial", dial, 1)).status_code == 200
    second = await _set_dial(client, "acme/twicedial", dial, 2)
    assert second.status_code == 200, second.text
    assert [d["value"] for d in second.json()["replaced"]] == [1], second.text

    live = (await client.get("/dials", params={"repo": "acme/twicedial"},
                             headers=LAPTOP)).json()["dials"]
    assert [(d["repo"], d["value"]) for d in live if d["dial"] == dial] == \
        [("acme/twicedial", 2)]


@pytest.mark.parametrize("repo", NOT_A_REPO)
async def test_every_dial_surface_refuses_a_spelling_that_is_not_a_repo(client, repo):
    """All three surfaces, because a dial that can be written under a spelling the
    reads refuse is a setting nobody can turn off again."""
    listed = await client.get("/dials", params={"repo": repo}, headers=LAPTOP)
    assert listed.status_code == 422, f"GET accepted {repo!r}: {listed.text}"
    written = await _set_dial(client, repo, "review_panel.max_rounds", 1)
    assert written.status_code == 422, f"POST accepted {repo!r}: {written.text}"
    cleared = await client.post("/dials/clear", json={
        "dial": "review_panel.max_rounds", "repo": repo}, headers=HUMAN)
    assert cleared.status_code == 422, f"clear accepted {repo!r}: {cleared.text}"


async def _register(client, device, repo, path="/src/wt"):
    return await client.put("/worktrees", json={
        "device": device,
        "worktrees": [{"path": path, "repo": repo, "branch": "main",
                       "head": "0" * 40, "commits": []}]}, headers=LAPTOP)


async def test_a_worktree_registered_with_capitals_is_found_by_either_spelling(client):
    """`worktrees.repo` is the origin slug and `GET /worktrees?repo=` compared it
    with `==`, so a device whose remote is spelled `PrisonBlues/Quarterback`
    registered a repository the board held apart from the same one registered in
    lower case — and each spelling's query answered about half the fleet."""
    assert (await _register(client, "wt-caps", "Acme/CaseTree",
                            "/src/caps")).status_code == 200
    for asked in ("acme/casetree", "Acme/CaseTree", "ACME/CASETREE"):
        got = await client.get("/worktrees", params={"repo": asked}, headers=LAPTOP)
        assert got.status_code == 200, got.text
        assert [(w["device"], w["repo"]) for w in got.json()] == \
            [("wt-caps", "acme/casetree")], f"{asked!r} saw a different fleet"


async def test_the_worktree_and_sync_endpoints_agree_about_one_repo(client):
    """The disagreement #350 names. `/sync` folds this column through
    `app.sync.repo_key` (basename, lower-cased) while `/worktrees` compared it
    exactly — and the only caller of the `?repo=` filter in the tree, the board
    TUI's cherry-pick discovery, has the bare name off a POST's `repo` ref and
    nothing else. It was answered `[]`, which renders as "no registered checkout
    of quarterback on zeus": the false-clean this class is about."""
    assert (await _register(client, "wt-agree", "PrisonBlues/AgreeTree",
                            "/src/agree")).status_code == 200

    bare = await client.get("/worktrees", params={"repo": "AgreeTree"}, headers=LAPTOP)
    assert bare.status_code == 200, bare.text
    assert [w["path"] for w in bare.json()] == ["/src/agree"], \
        "the bare name the board's own posts carry still finds nothing"

    synced = await client.get("/sync", params={"repo": "agreetree"}, headers=LAPTOP)
    assert synced.status_code == 200, synced.text
    assert [w["path"] for w in synced.json()["worktrees"]] == ["/src/agree"]


#: Everything `REPO_RE` refuses that is not simply the ambiguous bare name. A bare
#: name IS accepted by `GET /worktrees?repo=` and by nothing else on the board —
#: see the endpoint's module docstring for why that widening is a read and not a
#: second namespace.
NOT_A_REPO_NOR_A_NAME = [r for r in NOT_A_REPO if "/" in r]


@pytest.mark.parametrize("repo", NOT_A_REPO_NOR_A_NAME)
async def test_a_worktree_read_refuses_a_spelling_that_is_neither(client, repo):
    """A clone URL or a path answered with `[]` reads as "nothing is registered"
    when it means "I could not tell what you asked about"."""
    r = await client.get("/worktrees", params={"repo": repo}, headers=LAPTOP)
    assert r.status_code == 422, f"accepted {repo!r}: {r.text}"


#: Spellings that have a bare name INSIDE them, and are not one. `repo_key` is
#: total and answers `passwd` for `/etc/passwd` and `c` for `a/b/c`, so a widening
#: that checked its output rather than the caller's whole string would turn every
#: path and clone URL into a match on whatever it ends with.
NOT_A_NAME_EITHER = ["/quarterback", "quarterback/", "quarterback.git",
                     "quarter back", ".quarterback", "a/b/c", "/etc/passwd"]


@pytest.mark.parametrize("repo", NOT_A_NAME_EITHER)
async def test_the_bare_name_widening_is_a_name_and_not_a_basename(client, repo):
    """The widening is `REPO_NAME_RE` — the repository half of `REPO_RE` itself —
    applied to the whole string, so it admits exactly the spelling a board post
    carries and nothing that merely ends with one."""
    r = await client.get("/worktrees", params={"repo": repo}, headers=LAPTOP)
    assert r.status_code == 422, f"accepted {repo!r}: {r.text}"


@pytest.mark.parametrize("repo", NOT_A_REPO_NOR_A_NAME + NOT_A_NAME_EITHER)
async def test_the_landing_graph_read_refuses_a_spelling_that_is_neither(client, repo):
    """`GET /landing` is the second read on this board that takes a bare name, and
    for the same reason `GET /worktrees` does — the spelling a board post carries.
    It widens exactly that far and no further: an empty graph in answer to a clone
    URL would read as "nothing gates anything here"."""
    r = await client.get("/landing", params={"repo": repo}, headers=LAPTOP)
    assert r.status_code == 422, f"accepted {repo!r}: {r.text}"


@pytest.mark.parametrize("repo", NOT_A_REPO)
async def test_a_landing_EDGE_cannot_be_WRITTEN_under_any_of_them(client, repo):
    """The write is strict where the read is not, and here the asymmetry matters
    more than usual: a node IS a claim key, so a second spelling of one repo is a
    second node that nothing will ever match against the first."""
    r = await client.post("/landing/gate", headers=LAPTOP, json={
        "blocked": {"kind": "pr", "value": "2", "repo": repo},
        "blocker": {"kind": "pr", "value": "1", "repo": "acme/keyed"}})
    assert r.status_code == 422, f"accepted {repo!r}: {r.text}"


@pytest.mark.parametrize("repo", NOT_A_REPO)
async def test_a_worktree_cannot_be_REGISTERED_under_any_of_them(client, repo):
    """The write is strict where the read is not, and that asymmetry is the whole
    design: the column stays `owner/name` because only `canonical_repo` can write
    it, which is what lets the read compare it rather than fold it."""
    r = await _register(client, "wt-refused", repo, "/src/refused")
    assert r.status_code == 422, f"accepted {repo!r}: {r.text}"


async def test_a_checkout_with_no_github_remote_still_registers(client):
    """`repo_slug` returns None where the origin is not a GitHub-style remote, and
    that is a fact about the checkout rather than a bad spelling. It must not be
    caught by the refusal above — the row is what makes the commit findable by
    SHA, which is the index's main job."""
    r = await client.put("/worktrees", json={
        "device": "wt-remoteless",
        "worktrees": [{"path": "/src/local", "repo": None, "branch": "main",
                       "head": "0" * 40, "commits": []}]}, headers=LAPTOP)
    assert r.status_code == 200, r.text
    listed = await client.get("/worktrees", params={"device": "wt-remoteless"},
                              headers=LAPTOP)
    assert [w["repo"] for w in listed.json()] == [None]


async def test_a_refused_snapshot_does_not_destroy_the_one_it_would_replace(client):
    """`PUT /worktrees` is a full replace, so the refusal has to happen before the
    DELETE — a 422 that had already emptied the device's registry would make a bad
    spelling cost the fleet its cross-worktree discovery until the next report."""
    assert (await _register(client, "wt-keep", "acme/keeptree",
                            "/src/keep")).status_code == 200
    assert (await _register(client, "wt-keep", "quarterback",
                            "/src/keep")).status_code == 422
    still = await client.get("/worktrees", params={"device": "wt-keep"}, headers=LAPTOP)
    assert [w["path"] for w in still.json()] == ["/src/keep"]


# ------------------------------------------------- the half that lives in mcp/

@pytest.fixture(scope="module")
def mcp_source() -> ast.Module:
    return ast.parse(MCP_SERVER.read_text(encoding="utf-8"))


def _args(mod: ast.Module, name: str) -> list[str]:
    fn = next(n for n in ast.walk(mod)
              if isinstance(n, ast.FunctionDef) and n.name == name)
    return [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]


def _names(mod: ast.Module) -> set[str]:
    return {n.name for n in ast.walk(mod) if isinstance(n, ast.FunctionDef)}


@pytest.mark.parametrize("tool", ["claim_release_number", "reclaim_release_number", "releases"])
def test_the_release_tools_are_gone(mcp_source, tool):
    """#172 deleted the allocator. `release_stamp.py` takes `max+1` at land from
    the ref being merged into and shipped nine releases in a day with no
    collision, while the allocator's own rows went stale for every PR still open.
    A tool that still offers to allocate a number is a second answer to a question
    that has one — which is the defect the whole issue is about."""
    assert tool not in _names(mcp_source), (
        f"{tool} is back. The board no longer allocates versions: release_stamp.py "
        "reads max+1 off the ref at land, and a claimed-but-stale number is worse "
        "than none."
    )


@pytest.mark.parametrize("tool", ["claim", "claims", "claim_held"])
def test_no_claim_tool_asks_for_a_repo_STRING(mcp_source, tool):
    """The endpoint rule alone would just move the guessing one layer out: an
    agent that must supply `owner/name` still has to decide what this repo is
    called, and it has two true answers. The tools take a PATH and read the
    remote, so the question is never put to a model."""
    args = _args(mcp_source, tool)
    assert "repo" not in args, (
        f"{tool} takes a repo string again. That parameter is #148: two agents "
        "answer it with the two spellings they each have, and the board believes "
        "both. Take repo_path and call repo_slug()."
    )
    assert "repo_path" in args, f"{tool} should take repo_path"


def test_sync_status_does_not_fall_back_to_the_directory_name(mcp_source):
    """`repo_slug(p) or toplevel.rsplit("/", 1)[-1]` is how the bare spelling got
    into the table in the first place: one call site deriving the tight name and
    quietly degrading to the loose one. A directory name is not a repo name — two
    worktrees of one repo can disagree about it, and renaming a checkout would
    silently start a new namespace."""
    fn = next(n for n in ast.walk(mcp_source)
              if isinstance(n, ast.FunctionDef) and n.name == "sync_status")
    src = ast.dump(fn)
    assert "rsplit" not in src, "sync_status is guessing the repo from a path again"


# ------------------------------------------- the one-time resolution, on write

@pytest.fixture(scope="module")
def migration():
    """Revision 0022, imported by path — it is not on a package path."""
    import importlib.util
    path = REPO_ROOT / "migrations" / "versions" / "0022_canonical_release_repo.py"
    spec = importlib.util.spec_from_file_location("_m0022", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_it_resolves_the_rows_this_board_actually_has(migration):
    """The real shape on 2026-08-17: two bare rows written before the fix, beside
    nine canonical ones, and exactly one repo whose name half is `quarterback`.
    Both resolve, so nobody is asked anything."""
    rows = [
        (1, "quarterback:2.36", None),
        (2, "quarterback:2.32", None),
        (3, "prisonblues/quarterback:2.36", "2026-08-16T20:06:19"),  # released
        (4, "prisonblues/quarterback:2.43", None),
    ]
    assert sorted(migration.plan_rewrites(rows)) == [
        (1, "prisonblues/quarterback:2.36"),
        (2, "prisonblues/quarterback:2.32"),
    ]


def test_a_rewrite_may_join_a_RELEASED_row_on_the_same_key(migration):
    """Row 1 above becomes `prisonblues/quarterback:2.36`, which already exists —
    but released. The unique index covers unreleased rows only, because an
    allocator needs every number it ever handed out, so history stacking up on one
    key is correct rather than a conflict."""
    rows = [(1, "quarterback:2.36", None),
            (2, "prisonblues/quarterback:2.36", "released-at"),
            (3, "prisonblues/quarterback:2.40", None)]
    assert migration.plan_rewrites(rows) == [(1, "prisonblues/quarterback:2.36")]


def test_two_LIVE_claims_on_one_key_is_refused_not_merged(migration):
    """The case the migration must not decide. Both rows are unreleased, so both
    holders may have shipped that number; picking one is exactly the re-issue the
    allocator exists to prevent."""
    rows = [(1, "quarterback:2.36", None),
            (2, "prisonblues/quarterback:2.36", None)]
    with pytest.raises(RuntimeError, match="two LIVE claims"):
        migration.plan_rewrites(rows)


def test_an_ambiguous_name_stops_the_migration(migration):
    """Two owners, one repo name, no answer. A one-time refusal a human resolves
    beats a permanent read-time guess nobody revisits — the deploy blocks, which
    is loud, rather than the numbering drifting, which is not."""
    rows = [(1, "widget:1.0", None),
            (2, "acme/widget:1.0", None),
            (3, "other/widget:2.0", None)]
    with pytest.raises(RuntimeError, match="share the name"):
        migration.plan_rewrites(rows)


def test_an_unknown_name_stops_it_too(migration):
    rows = [(1, "orphan:1.0", None), (2, "acme/widget:1.0", None)]
    with pytest.raises(RuntimeError, match="no canonical repo"):
        migration.plan_rewrites(rows)


def test_a_clean_board_is_a_noop(migration):
    rows = [(1, "acme/widget:1.0", None), (2, "acme/widget:1.1", None)]
    assert migration.plan_rewrites(rows) == []


def test_the_version_is_split_off_the_LAST_colon(migration):
    """An scp-style key carries its own colon. Splitting at the first would read
    `git@github.com:acme/thing:1.0` as the repo `git@github.com` — which is a
    stranded row being quietly renamed instead of caught."""
    assert migration._split("git@github.com:acme/thing:1.0") == (
        "git@github.com:acme/thing", "1.0")
    assert migration._split("noversion") is None


def test_an_existing_capitalised_row_is_folded_too(migration):
    """The endpoint lowercases what it writes, so a row left capitalised would be
    invisible to every query made after this and would drop out of the allocation
    floor — its number silently free to reissue. The migration's rule has to match
    the endpoint's exactly, in both directions."""
    rows = [(1, "Acme/Widget:1.0", None), (2, "acme/widget:1.1", None)]
    assert migration.plan_rewrites(rows) == [(1, "acme/widget:1.0")]


def test_the_migration_refuses_a_dot_git_repo_like_the_endpoint_does(migration):
    """If the migration's idea of canonical were looser than the app's it would
    resolve rows into keys the app then refuses to read."""
    assert migration._REPO_RE.match("acme/widget")
    assert not migration._REPO_RE.match("acme/widget.git")
