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

from .conftest import LAPTOP

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
