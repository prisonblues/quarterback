"""One repo has one spelling, because nobody spells it.

#148 was not a naming problem and the fix is not a name parser. The allocator's
key was caller-supplied text, and an agent asked "which repo is this" answers
with whichever spelling it has to hand: `quarterback` from the directory it
stands in, `prisonblues/quarterback` from the remote. Both true, not equal. One
repo, two counters, 2.36 issued twice.

The rejected repair was to accept every spelling and reconcile them — PR #152,
closed. That input domain is open (bare names, `.git` suffixes, URLs, scp
remotes) and three review rounds found three more holes in the enumeration, each
one the previous fix overshooting. An alias set that can be incomplete will be.

So the domain is closed instead, in two halves that have to stay in step:

* the MCP release tools do not take a repo. They read `owner/name` from
  `remote.origin.url` — which `sync_status` and `report_git` were already doing,
  six lines away in the same server.
* the endpoints accept `owner/name` and refuse everything else with a 422, so a
  caller arriving another way gets an answer rather than a new namespace.

Both halves are pinned here. The second without the first is a rule agents keep
tripping over; the first without the second is a convention.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

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


async def claim_release(client, repo: str, **over):
    return await client.post("/release/claim",
                             json={"repo": repo, "note": "t", **over}, headers=LAPTOP)


@pytest.mark.parametrize("repo", NOT_A_REPO)
async def test_only_owner_slash_name_can_allocate(client, repo):
    r = await claim_release(client, repo)
    assert r.status_code == 422, f"{repo!r} was accepted: {r.text}"


async def test_the_canonical_spelling_still_works(client):
    r = await claim_release(client, "acme/widget")
    assert r.status_code == 200, r.text
    assert r.json()["version"]


async def test_the_refusal_says_what_shape_is_wanted(client):
    """A 422 that only says "invalid" leaves the caller guessing, and guessing at
    a repo name is the bug. It also has to say the caller should not be typing
    this at all, or the next agent carefully supplies a better-spelled string."""
    r = await claim_release(client, "quarterback")
    body = r.text
    assert "owner/name" in body
    assert "origin remote" in body


async def test_reading_releases_refuses_the_same_shapes(client):
    """The read path matters as much as the write path: asking for one spelling
    and being shown a different repo's numbers is how a caller concludes a number
    is free."""
    assert (await client.get("/releases", params={"repo": "quarterback"},
                             headers=LAPTOP)).status_code == 422
    assert (await client.get("/releases", params={"repo": "acme/widget"},
                             headers=LAPTOP)).status_code == 200


async def test_reclaim_refuses_it_too(client):
    """The renumber path is where both real collisions actually happened."""
    r = await client.post("/release/reclaim",
                          json={"repo": "quarterback",
                                "claim_id": "00000000-0000-0000-0000-000000000000"},
                          headers=LAPTOP)
    assert r.status_code == 422


async def test_case_is_folded_rather_than_treated_as_a_second_repo(client):
    """GitHub is case-insensitive and case-preserving, so `Acme/Widget` and
    `acme/widget` are one repository that can be cloned with either remote — #148
    again, in a spelling the shape rule alone lets through. Refusing the
    capitalised form would strand a repo genuinely named `acme/MyProject`, so it
    folds. This is the only normalisation here and it is safe for the reason the
    parser was not: `lower()` is total, so it has no next case to miss."""
    first = await claim_release(client, "acme/casefold")
    assert first.status_code == 200, first.text
    second = await claim_release(client, "Acme/CaseFold")
    assert second.status_code == 200, second.text
    assert first.json()["key"].startswith("acme/casefold:")
    assert second.json()["key"].startswith("acme/casefold:")
    assert second.json()["version"] != first.json()["version"], (
        "the capitalised spelling allocated from its own floor — two namespaces"
    )


async def test_two_spellings_of_one_repo_can_no_longer_both_allocate(client):
    """#148, as a test. Before this, these two calls were two namespaces and each
    handed out its own 2.36 — the second caller being told it had the number, and
    being wrong. Now only one of them is a repo at all."""
    first = await claim_release(client, "prisonblues/quarterback")
    assert first.status_code == 200, first.text
    second = await claim_release(client, "quarterback")
    assert second.status_code == 422, "the bare spelling opened a second namespace"


# ------------------------------------------------- the half that lives in mcp/

@pytest.fixture(scope="module")
def mcp_source() -> ast.Module:
    return ast.parse(MCP_SERVER.read_text(encoding="utf-8"))


def _args(mod: ast.Module, name: str) -> list[str]:
    fn = next(n for n in ast.walk(mod)
              if isinstance(n, ast.FunctionDef) and n.name == name)
    return [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]


@pytest.mark.parametrize("tool", ["claim_release_number", "reclaim_release_number", "releases"])
def test_no_release_tool_asks_for_a_repo(mcp_source, tool):
    """The endpoint rule alone would just move the guessing one layer out: an
    agent that must supply `owner/name` still has to decide what this repo is
    called, and it has two true answers. The tools take a PATH and read the
    remote, so the question is never put to a model."""
    args = _args(mcp_source, tool)
    assert "repo" not in args, (
        f"{tool} takes a repo string again. That parameter is #148: two agents "
        "answer it with the two spellings they each have, and the allocator "
        "believes both. Take repo_path and call repo_slug()."
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
