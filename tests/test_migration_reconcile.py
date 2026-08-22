"""Tests for `scripts/migration_reconcile.py`.

The graph core is a pure function of migration file text, so most of this is fixture
graphs and no I/O at all. The `git`-backed tests below build a throwaway repo — they
exercise the git layer, the CLI verbs and the on-disk rewrite, and still need no
database.

The test this file exists for is `test_duplicate_number_is_not_a_clean_merge`: the
donor implementation reports that graph as a clean no-op, which is the failure the
whole tool is aimed at. Its sequel is
`test_two_files_at_one_ref_claiming_one_id_refuse_to_build_a_graph` — the same
collision one merge later, where the tool's own id-keyed graph folded the two
migrations into one node and reported the result clean.

The CLI verbs are covered through `mr.main(...)` rather than by calling `cmd_*`
directly wherever the exit code is the point, because the exit code is the whole
interface #96's pre-land gate consumes.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

# `scripts/` is a directory of standalone tools, not an importable package, so the
# module is loaded by path. It must be registered in sys.modules before it executes:
# @dataclass resolves annotations through sys.modules[cls.__module__].
_SPEC = importlib.util.spec_from_file_location(
    "migration_reconcile",
    Path(__file__).resolve().parent.parent / "scripts" / "migration_reconcile.py",
)
mr = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = mr
_SPEC.loader.exec_module(mr)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

MIGRATION = '''"""{doc}

Prose mentioning revision **{rev}**, which a renumber does not rewrite.
"""

from collections.abc import Sequence

revision: str = "{rev}"
down_revision: str | None = {down}
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("{col}", sa.Text(), nullable=True))
'''


def text(rev: str, down: str | None, col: str = "x", doc: str = "a migration") -> str:
    return MIGRATION.format(rev=rev, down="None" if down is None else f'"{down}"', col=col, doc=doc)


def rev(num: str, down: str | None, slug: str = "thing", col: str = "x") -> mr.Rev:
    return mr.parse_migration(text(num, down, col), path=f"migrations/versions/{num}_{slug}.py")


def chain(*nums: str) -> list[mr.Rev]:
    """A linear chain 0001 <- 0002 <- ... from the ids given."""
    out, prev = [], None
    for n in nums:
        out.append(rev(n, prev))
        prev = n
    return out


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def test_parses_the_annotated_form_this_repo_actually_uses():
    r = mr.parse_migration(text("0017", "0016"), path="migrations/versions/0017_p.py")
    assert (r.id, r.down, r.depends, r.number) == ("0017", ("0016",), (), 17)


def test_root_migration_has_no_parents():
    assert mr.parse_migration(text("0001", None)).down == ()


def test_a_non_migration_file_is_rejected_rather_than_guessed_at():
    with pytest.raises(ValueError):
        mr.parse_migration("import os\n")


def test_a_quoted_string_inside_a_comment_is_not_a_phantom_parent():
    src = text("0018", "0017").replace(
        'down_revision: str | None = "0017"',
        'down_revision: str | None = "0017"  # was "0016" before the rebase',
    )
    assert mr.parse_migration(src).down == ("0017",)


def test_a_tuple_down_revision_parses_as_a_merge_node():
    src = text("0019", "0017").replace(
        'down_revision: str | None = "0017"', 'down_revision = ("0017", "0018")'
    )
    r = mr.parse_migration(src)
    assert r.down == ("0017", "0018") and r.is_merge


def test_an_unnumbered_revision_id_has_no_chain_position():
    assert mr.parse_migration(text("a1b2c3", None)).number is None


def test_depends_on_does_not_close_a_head():
    # Alembic keeps a revision a head even when another revision depends_on it.
    # Folding that edge in would under-count heads and pass a two-head graph.
    src = text("0018", "0016").replace(
        "depends_on: str | Sequence[str] | None = None", 'depends_on = ("0017",)'
    )
    revs = [rev("0016", "0015"), rev("0017", "0016"), mr.parse_migration(src, path="p.py")]
    assert mr.heads(revs) == ["0017", "0018"]


# ---------------------------------------------------------------------------
# the case the donor implementation cannot see
# ---------------------------------------------------------------------------


def test_duplicate_number_is_not_a_clean_merge():
    """Two branches both minted 0018. A graph-only reconciler sees one id, present at
    both refs, with identical parents — nothing rewritten, nothing new — and reports
    a clean no-op while the landed tree carries two different migrations claiming one
    revision id. It must renumber instead."""
    onto = [*chain("0016", "0017"), rev("0018", "0017", slug="run_files", col="files")]
    branch = [*chain("0016", "0017"), rev("0018", "0017", slug="base_sha", col="base_sha")]
    ancestors = frozenset({"0016", "0017"})

    plan = mr.reconcile(onto, branch, ancestors)

    assert plan.action == "renumber"
    assert plan.collisions == ["0018"]
    assert plan.go and plan.exit_code == 0
    (rn,) = plan.renames
    assert (rn.old_id, rn.new_id) == ("0018", "0019")
    assert rn.new_down == ("0018",)
    assert rn.old_path == "migrations/versions/0018_base_sha.py"
    assert rn.new_path == "migrations/versions/0019_base_sha.py"


def test_the_renumbered_graph_has_exactly_one_head():
    onto = [*chain("0016", "0017"), rev("0018", "0017", slug="run_files", col="files")]
    branch = [*chain("0016", "0017"), rev("0018", "0017", slug="base_sha", col="base_sha")]
    plan = mr.reconcile(onto, branch, frozenset({"0016", "0017"}))

    ok, h = mr.verify_single_head(mr.simulate_merged(onto, branch, plan))

    assert (ok, h) == (True, ["0019"])


def test_renumbering_leaves_the_integration_refs_own_migration_alone():
    """Both copies share the old id, so a rename keyed on id alone would rewrite the
    already-merged one too and silently drop it out of the chain."""
    onto = [*chain("0016", "0017"), rev("0018", "0017", slug="run_files", col="files")]
    branch = [*chain("0016", "0017"), rev("0018", "0017", slug="base_sha", col="base_sha")]
    plan = mr.reconcile(onto, branch, frozenset({"0016", "0017"}))

    merged = mr.simulate_merged(onto, branch, plan)

    assert sorted(r.id for r in merged) == ["0016", "0017", "0018", "0019"]
    kept = next(r for r in merged if r.id == "0018")
    assert kept.path == "migrations/versions/0018_run_files.py"


def test_a_whole_contested_chain_is_renumbered_in_order():
    onto = [*chain("0016", "0017"), rev("0018", "0017", slug="theirs", col="theirs")]
    branch = [
        *chain("0016", "0017"),
        rev("0018", "0017", slug="mine_a", col="a"),
        rev("0019", "0018", slug="mine_b", col="b"),
    ]
    plan = mr.reconcile(onto, branch, frozenset({"0016", "0017"}))

    assert plan.action == "renumber"
    assert [(r.old_id, r.new_id, r.new_down) for r in plan.renames] == [
        ("0018", "0019", ("0018",)),
        ("0019", "0020", ("0019",)),
    ]
    assert mr.verify_single_head(mr.simulate_merged(onto, branch, plan)) == (True, ["0020"])


def test_a_number_at_or_below_the_head_is_renumbered_before_it_can_collide():
    """No id is contested yet — the branch's 0018 is simply behind main's 0020. It is
    one merge away from being the collision above, and the resolution is the same."""
    onto = chain("0016", "0017", "0020")
    branch = [*chain("0016", "0017"), rev("0018", "0017", slug="mine", col="mine")]
    plan = mr.reconcile(onto, branch, frozenset({"0016", "0017"}))

    assert plan.action == "renumber"
    assert plan.collisions == []
    assert [(r.old_id, r.new_id, r.new_down) for r in plan.renames] == [("0018", "0021", ("0020",))]


def test_renumbering_skips_a_number_some_other_migration_already_holds():
    # A chain whose head is not its highest number: 0018 is an ancestor of 0017, so
    # the next free position above the head 0017 is 0019, not 0018.
    onto = chain("0016", "0018", "0017")
    branch = [*chain("0016", "0018"), rev("0017", "0018", slug="mine", col="mine")]
    plan = mr.reconcile(onto, branch, frozenset({"0016", "0018"}))

    assert plan.collisions == ["0017"]
    assert [(r.old_id, r.new_id) for r in plan.renames] == [("0017", "0019")]
    assert mr._allocate({17, 18, 19, 20}, 16, 2) == [21, 22]


def test_a_hash_named_revision_still_relinks_normally():
    """Not every revision id has to be a chain number — one that is not simply never
    collides, and the ordinary relink applies."""
    onto = [*chain("0016", "0017"), rev("0018", "0017", slug="theirs", col="t")]
    branch = [*chain("0016", "0017"), mr.parse_migration(text("beef01", "0017"), path="b.py")]
    plan = mr.reconcile(onto, branch, frozenset({"0016", "0017"}))

    assert plan.action == "relink" and plan.new_down == ("0018",)


def test_an_unnumbered_id_in_a_contested_chain_stops_rather_than_deferring_to_merge():
    """The chain has to be renumbered and one of its links carries no number to
    renumber, so the arithmetic has nothing to work from. `alembic merge heads` adds a
    merge revision and renumbers nothing, so it cannot resolve the duplicate `0018` —
    handing the caller a GO on that would leave the duplicate to land."""
    onto = [*chain("0016", "0017"), rev("0018", "0017", slug="theirs", col="t")]
    branch = [
        *chain("0016", "0017"),
        rev("0018", "0017", slug="mine", col="m"),
        mr.parse_migration(text("beef01", "0018"), path="b.py"),
    ]
    plan = mr.reconcile(onto, branch, frozenset({"0016", "0017"}))

    assert plan.action == "stop" and not plan.go and plan.exit_code == 2
    assert "beef01" in plan.reason and "0018" in plan.reason


def test_an_unnumbered_id_with_no_collision_still_falls_back_to_the_merge_migration():
    """Nothing is contested — the branch is merely behind — so a merge revision does
    resolve it, and the fallback is a real answer rather than a rubber stamp."""
    onto = chain("0016", "0017", "0020")
    branch = [
        *chain("0016", "0017"),
        rev("0018", "0017", slug="mine", col="m"),
        mr.parse_migration(text("beef01", "0018"), path="b.py"),
    ]
    plan = mr.reconcile(onto, branch, frozenset({"0016", "0017"}))

    assert plan.action == "merge" and plan.go and plan.exit_code == 3
    assert plan.collisions == []


# ---------------------------------------------------------------------------
# collision vs rewrite
# ---------------------------------------------------------------------------


def test_editing_an_already_merged_migration_stops_rather_than_renumbering():
    """Same id, differing content, and the id is in shared history — one migration
    that the branch edited. Renumbering would fork it into two."""
    onto = chain("0016", "0017")
    branch = [rev("0016", "0015"), rev("0017", "0016", col="changed")]
    plan = mr.reconcile(onto, branch, frozenset({"0016", "0017"}))

    assert plan.action == "stop" and not plan.go and plan.exit_code == 2
    assert plan.guards["C_no_shared_rewrite"] is False


def test_without_a_merge_base_a_shared_path_is_assumed_rewritten():
    onto = chain("0016", "0017")
    branch = [rev("0016", "0015"), rev("0017", "0016", col="changed")]
    assert mr.reconcile(onto, branch, None).action == "stop"


def test_without_a_merge_base_distinct_paths_are_still_a_collision():
    """Two files cannot be one migration however little else is known."""
    onto = [*chain("0016"), rev("0017", "0016", slug="theirs", col="t")]
    branch = [*chain("0016"), rev("0017", "0016", slug="mine", col="m")]
    plan = mr.reconcile(onto, branch, None)

    assert plan.action == "renumber" and plan.collisions == ["0017"]


def test_identical_content_at_both_refs_is_shared_history_not_a_collision():
    onto = chain("0016", "0017")
    assert mr.classify_shared(onto, list(onto), frozenset({"0016", "0017"})) == ([], [])


def test_equal_content_at_two_paths_is_still_two_files_and_still_a_collision():
    """Both branches minted `0018` and happened to write the same bytes. Equal content
    reads as "one migration seen twice" and the merge is called clean — but they are
    two files at two paths, git conflicts on neither, and the landed tree carries two
    migrations claiming one id."""
    same = dict(down="0017", col="same")
    onto = [*chain("0016", "0017"), rev("0018", slug="theirs", **same)]
    branch = [*chain("0016", "0017"), rev("0018", slug="mine", **same)]
    assert onto[-1].digest == branch[-1].digest

    plan = mr.reconcile(onto, branch, frozenset({"0016", "0017"}))

    assert plan.action == "renumber" and plan.collisions == ["0018"]
    assert mr.duplicate_ids(mr.simulate_merged(onto, branch, plan)) == []


def test_the_same_file_added_at_the_same_path_by_both_refs_merges_to_one():
    """The mirror case, which must NOT be renumbered: git merges two identical
    additions of one path into a single file, so there is nothing contested."""
    same = dict(down="0017", col="same", slug="shared")
    onto = [*chain("0016", "0017"), rev("0018", **same)]
    branch = [*chain("0016", "0017"), rev("0018", **same)]

    plan = mr.reconcile(onto, branch, frozenset({"0016", "0017"}))

    assert plan.action == "noop" and plan.collisions == []


# ---------------------------------------------------------------------------
# the guards
# ---------------------------------------------------------------------------


def test_a_multi_head_integration_ref_stops_before_anything_else_is_judged():
    onto = [*chain("0016", "0017"), rev("0018", "0016", slug="other", col="o")]
    plan = mr.reconcile(onto, [*onto, rev("0019", "0017")], frozenset())

    assert plan.action == "stop"
    # Every key, every time. A consumer reading one guard must not get a KeyError
    # depending on which guard happened to fire.
    assert plan.guards == {
        "A_onto_single_head": False,
        "B_single_chain": None,
        "C_no_shared_rewrite": None,
    }


def test_a_second_root_is_ambiguous_and_stops():
    onto = chain("0016", "0017")
    branch = [*onto, rev("0018", None, slug="orphan", col="o")]
    plan = mr.reconcile(onto, branch, frozenset({"0016", "0017"}))

    assert plan.action == "stop" and "new root" in plan.reason


def test_two_independent_bases_fall_back_to_a_merge_migration():
    onto = chain("0016", "0017")
    branch = [*onto, rev("0018", "0017", slug="a", col="a"), rev("0019", "0016", slug="b", col="b")]
    plan = mr.reconcile(onto, branch, frozenset({"0016", "0017"}))

    assert plan.action == "merge" and plan.go and plan.exit_code == 3
    assert plan.guards["B_single_chain"] is False


def test_an_unknown_action_fails_safe_to_stop():
    assert mr.Plan("typo", "", go=True).exit_code == 2


# ---------------------------------------------------------------------------
# the quiet cases
# ---------------------------------------------------------------------------


def test_a_branch_with_no_migrations_is_a_noop():
    onto = chain("0016", "0017")
    plan = mr.reconcile(onto, list(onto), frozenset({"0016", "0017"}))

    assert plan.action == "noop" and plan.go


def test_a_branch_cut_from_the_current_head_is_a_noop():
    onto = chain("0016", "0017")
    plan = mr.reconcile(onto, [*onto, rev("0018", "0017")], frozenset({"0016", "0017"}))

    assert plan.action == "noop" and plan.go


def test_a_branch_left_behind_by_two_merges_is_relinked():
    onto = chain("0016", "0017", "0018")
    branch = [*chain("0016"), rev("0019", "0016", slug="mine", col="m")]
    plan = mr.reconcile(onto, branch, frozenset({"0016"}))

    assert plan.action == "relink"
    assert (plan.base, plan.old_down, plan.new_down) == ("0019", ("0016",), ("0018",))
    assert mr.verify_single_head(mr.simulate_merged(onto, branch, plan)) == (True, ["0019"])


# ---------------------------------------------------------------------------
# the on-disk rewrite
# ---------------------------------------------------------------------------


def test_rewriting_a_value_preserves_the_type_annotation():
    out = mr._rewrite_assignment(text("0018", "0017"), "down_revision", '"0018"')
    assert 'down_revision: str | None = "0018"' in out
    assert mr.parse_migration(out).down == ("0018",)


def test_rewriting_a_value_preserves_a_trailing_comment():
    src = text("0018", "0017").replace(
        'down_revision: str | None = "0017"',
        'down_revision: str | None = "0017"  # cut from the v2.26 head',
    )
    out = mr._rewrite_assignment(src, "down_revision", '"0018"')
    assert 'down_revision: str | None = "0018"  # cut from the v2.26 head' in out


def test_rewriting_a_missing_assignment_raises_rather_than_writing_nothing():
    with pytest.raises(RuntimeError):
        mr._rewrite_assignment("revision = '0018'\n", "down_revision", '"0019"')


def test_a_multiline_tuple_value_is_replaced_whole():
    src = text("0019", "0017").replace(
        'down_revision: str | None = "0017"',
        'down_revision = (\n    "0017",\n    "0018",\n)',
    )
    out = mr._rewrite_assignment(src, "down_revision", mr._render_refs(("0020",)))
    assert mr.parse_migration(out).down == ("0020",)


def test_a_value_containing_a_non_ascii_line_is_spliced_at_the_right_place():
    """`ast` counts column offsets in UTF-8 bytes. Splicing a `str` with them lands in
    the wrong place on any line holding a non-ASCII character, and a migration
    docstring with an em dash in it is this repo's house style."""
    src = text("0018", "0017", doc="a migration — with an em dash")
    out = mr._rewrite_assignment(src, "down_revision", '"0019"')
    assert 'down_revision: str | None = "0019"' in out
    assert "— with an em dash" in out


def test_rendering_a_reference_tuple_matches_what_a_migration_spells():
    assert mr._render_refs(()) == "None"
    assert mr._render_refs(("0018",)) == '"0018"'
    assert mr._render_refs(("0018", "0019")) == '("0018", "0019")'


# ---------------------------------------------------------------------------
# the git layer
# ---------------------------------------------------------------------------


#: An identity for every command, not just `commit`. A CI runner has no global
#: user.email, so anything that writes a commit object fails with exit 128 there while
#: passing on a workstation that happens to have one — `merge` included, which is how
#: this file first went red on CI and green locally.
_IDENT = ("-c", "user.email=t@example.invalid", "-c", "user.name=test")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *_IDENT, *args], capture_output=True, text=True, check=True
    ).stdout


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _write(repo: Path, name: str, rev_id: str, down: str | None, col: str) -> None:
    p = repo / "migrations" / "versions" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text(rev_id, down, col))


@pytest.fixture
def collided_repo(tmp_path: Path) -> Path:
    """A repo where `main` and a feature branch each landed their own `0018`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "0016_a.py", "0016", None, "a")
    _write(repo, "0017_b.py", "0017", "0016", "b")
    _commit(repo, "chain to 0017")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "0018_base_sha.py", "0018", "0017", "base_sha")
    _commit(repo, "feature: 0018")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "0018_run_files.py", "0018", "0017", "run_files")
    _commit(repo, "main: 0018")
    _git(repo, "checkout", "-q", "feature")
    return repo


@pytest.fixture
def behind_repo(tmp_path: Path) -> Path:
    """A repo where `main` moved on to `0018` while the feature branch, cut at `0017`,
    added `0019`. Nothing is contested — the branch's base just points at the wrong
    parent — so the resolution is a relink rather than a renumber."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "0016_a.py", "0016", None, "a")
    _write(repo, "0017_b.py", "0017", "0016", "b")
    _commit(repo, "chain to 0017")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "0019_mine.py", "0019", "0017", "mine")
    _commit(repo, "feature: 0019")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "0018_theirs.py", "0018", "0017", "theirs")
    _commit(repo, "main: 0018")
    _git(repo, "checkout", "-q", "feature")
    return repo


def test_preflight_reads_the_graph_out_of_git(collided_repo: Path):
    onto = mr.revs_at_ref(str(collided_repo), "main")
    branch = mr.revs_at_ref(str(collided_repo), "feature")
    ancestors = mr.ancestor_ids_of(str(collided_repo), "main", "feature")

    assert ancestors == frozenset({"0016", "0017"})
    plan = mr.reconcile(onto, branch, ancestors)
    assert plan.action == "renumber" and plan.collisions == ["0018"]


def _args(repo: Path, **over) -> Namespace:
    """The parsed-argument shape every verb consumes, defaulted the way the CLI
    defaults it. Built here rather than by hand per test so a new flag cannot leave
    half the suite calling a verb with an argument namespace the CLI never produces."""
    return Namespace(
        **{
            "repo": str(repo),
            "onto": "main",
            "branch": "HEAD",
            "ref": "HEAD",
            "versions_path": "migrations/versions",
            "json": False,
            **over,
        }
    )


def test_apply_renumbers_the_working_tree_and_leaves_it_uncommitted(collided_repo: Path):
    code = mr.cmd_apply(_args(collided_repo))

    assert code == 0
    versions = collided_repo / "migrations" / "versions"
    assert not (versions / "0018_base_sha.py").exists()
    moved = mr.parse_migration((versions / "0019_base_sha.py").read_text())
    assert (moved.id, moved.down) == ("0019", ("0018",))
    # never commits
    assert _git(collided_repo, "status", "--porcelain").strip()
    # and the resolved tree is single-headed once merged
    _commit(collided_repo, "renumber")
    _git(collided_repo, "merge", "-q", "--no-edit", "main")
    assert mr.heads(mr.revs_at_ref(str(collided_repo), "HEAD")) == ["0019"]


def test_apply_leaves_the_index_alone_so_both_resolutions_look_the_same(collided_repo: Path):
    """`git mv` would stage the rename carrying the OLD blob, so `git status` showed a
    staged rename plus an unstaged modification of the same path and the printed
    `git add` was a required repair rather than the last step. A relink stages nothing,
    and a renumber must not either."""
    assert mr.cmd_apply(_args(collided_repo)) == 0

    staged = _git(collided_repo, "diff", "--cached", "--name-only").strip()
    assert staged == ""
    status = _git(collided_repo, "status", "--porcelain").splitlines()
    assert sorted(status) == [
        " D migrations/versions/0018_base_sha.py",
        "?? migrations/versions/0019_base_sha.py",
    ]


def test_apply_refuses_when_the_named_branch_is_not_checked_out(collided_repo: Path):
    _git(collided_repo, "checkout", "-q", "main")
    assert mr.cmd_apply(_args(collided_repo, branch="feature")) == 2


def test_apply_refuses_a_dirty_versions_directory(collided_repo: Path):
    """Equal shas do not mean equal content. The plan is computed from git blobs at
    `--branch` and the rewrite edits the working tree, so an uncommitted edit means
    `_rewrite_assignment` operates on text the reconciler never read."""
    (collided_repo / "migrations" / "versions" / "0018_base_sha.py").write_text(
        text("0018", "0017", col="edited_after_the_commit")
    )

    assert mr.cmd_apply(_args(collided_repo)) == 2
    # and it wrote nothing
    assert (collided_repo / "migrations" / "versions" / "0018_base_sha.py").exists()


def test_apply_refuses_when_an_untracked_file_squats_the_destination(collided_repo: Path):
    """A destination that already exists is checked before anything is written, so the
    refusal leaves the tree untouched rather than half-renumbered."""
    versions = collided_repo / "migrations" / "versions"
    (versions / "0019_base_sha.py").write_text("# squatting the destination\n")

    assert mr.cmd_apply(_args(collided_repo)) == 2
    assert (versions / "0018_base_sha.py").exists()
    assert (versions / "0019_base_sha.py").read_text() == "# squatting the destination\n"


def test_apply_reports_what_it_rewrote_as_json(collided_repo: Path, capsys):
    assert mr.cmd_apply(_args(collided_repo, json=True)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "renumber"
    assert payload["exit_code"] == 0
    assert payload["edited"] == [
        "migrations/versions/0018_base_sha.py",
        "migrations/versions/0019_base_sha.py",
    ]


def test_apply_relinks_on_disk_when_that_is_the_resolution(behind_repo: Path):
    """The relink path writes a file too, and only the renumber path was exercised."""
    assert mr.cmd_apply(_args(behind_repo)) == 0

    moved = mr.parse_migration(
        (behind_repo / "migrations" / "versions" / "0019_mine.py").read_text()
    )
    assert (moved.id, moved.down) == ("0019", ("0018",))
    assert _git(behind_repo, "diff", "--cached", "--name-only").strip() == ""


def test_stale_prose_references_are_reported_and_never_rewritten(collided_repo: Path):
    plan = mr.reconcile(
        mr.revs_at_ref(str(collided_repo), "main"),
        mr.revs_at_ref(str(collided_repo), "feature"),
        mr.ancestor_ids_of(str(collided_repo), "main", "feature"),
    )
    warnings = mr.stale_references(str(collided_repo), "feature", plan)

    # The migration's own docstring quotes its number in prose (line 3 of the
    # fixture); that is a report, not an edit — the tool rewrites assignments it can
    # parse and prose it cannot. The `revision = "0018"` line two below it is NOT
    # reported: the renumber rewrites that one, so it is not stale by the time
    # anybody reads this.
    assert warnings == ["migrations/versions/0018_base_sha.py:3 still names 0018"]


def test_heads_at_a_ref_come_out_of_git(collided_repo: Path):
    assert mr.heads(mr.revs_at_ref(str(collided_repo), "main")) == ["0018"]
    assert mr.heads(mr.revs_at_ref(str(collided_repo), "feature")) == ["0018"]


# ---------------------------------------------------------------------------
# the duplicate this tool's own graph could not see
# ---------------------------------------------------------------------------


def test_two_files_at_one_ref_claiming_one_id_refuse_to_build_a_graph(collided_repo: Path):
    """The collision, already landed. Both `0018`s are now on one ref, where every
    id-keyed structure folds them into a single node: heads come back as one, the
    merge looks clean, and `alembic upgrade head` then refuses to load the graph.
    `revs_at_ref` makes id-uniqueness structural rather than checking for it later."""
    _git(collided_repo, "merge", "-q", "--no-edit", "main")

    with pytest.raises(mr.DuplicateRevisionError) as e:
        mr.revs_at_ref(str(collided_repo), "HEAD")

    assert "0018" in str(e.value)
    assert "0018_base_sha.py" in str(e.value) and "0018_run_files.py" in str(e.value)


def test_heads_exits_nonzero_on_a_landed_duplicate(collided_repo: Path, capsys):
    """`cmd_heads` had the same blind spot and returned 0 on it."""
    _git(collided_repo, "merge", "-q", "--no-edit", "main")

    assert mr.main(["heads", "--repo", str(collided_repo)]) == 2
    assert "0018" in capsys.readouterr().err


def test_preflight_stops_on_a_landed_duplicate(collided_repo: Path, capsys):
    """Guard A would otherwise see one head at `--onto` and bless the merge."""
    _git(collided_repo, "checkout", "-q", "main")
    _git(collided_repo, "merge", "-q", "--no-edit", "feature")
    _git(collided_repo, "checkout", "-q", "feature")

    assert mr.main(["preflight", "--repo", str(collided_repo), "--onto", "main"]) == 2
    assert "0018" in capsys.readouterr().err


def test_the_pure_core_stops_on_a_duplicate_it_is_handed_directly():
    """`reconcile` is documented as usable on its own, so the guard cannot live only
    in the git layer."""
    onto = [
        *chain("0016", "0017"),
        rev("0018", "0017", slug="theirs", col="t"),
        rev("0018", "0017", slug="mine", col="m"),
    ]
    plan = mr.reconcile(onto, list(onto), frozenset({"0016", "0017"}))

    assert plan.action == "stop" and plan.exit_code == 2
    assert plan.duplicate_ids == ["0018"]


def test_duplicate_ids_finds_repeats_and_ignores_a_clean_set():
    assert mr.duplicate_ids(chain("0016", "0017")) == []
    assert mr.duplicate_ids([rev("0018", "0017"), rev("0018", "0017", slug="other")]) == ["0018"]


# ---------------------------------------------------------------------------
# graphs the structural guard used to wave through
# ---------------------------------------------------------------------------


def test_a_diamond_closed_by_a_merge_node_is_not_a_single_chain():
    """One base and one head, so the old guard passed — but `_chain_order` follows a
    single path through the diamond, so two of the four revisions would keep their old
    parents, never appear in the renames, and be left untouched on disk under a GO."""
    onto = chain("0016", "0017")
    merge_src = text("0021", "0019").replace(
        'down_revision: str | None = "0019"', 'down_revision = ("0019", "0020")'
    )
    branch = [
        *onto,
        rev("0018", "0017", slug="base", col="b"),
        rev("0019", "0018", slug="left", col="l"),
        rev("0020", "0018", slug="right", col="r"),
        mr.parse_migration(merge_src, path="migrations/versions/0021_join.py"),
    ]
    plan = mr.reconcile(onto, branch, frozenset({"0016", "0017"}))

    assert plan.guards["B_single_chain"] is False
    assert plan.action == "merge" and plan.renames == []


def test_a_cycle_hanging_off_the_base_is_not_a_single_chain():
    """One base and one head again — the cycle members reference only each other, so
    neither is a base and neither is a head. `_chain_order`'s `seen` set stops it
    hanging; it does not stop it returning a partial answer."""
    onto = chain("0016", "0017")
    branch = [
        *onto,
        rev("0018", "0017", slug="base", col="b"),
        rev("0019", "0020", slug="a", col="a"),
        rev("0020", "0019", slug="b", col="b2"),
    ]
    plan = mr.reconcile(onto, branch, frozenset({"0016", "0017"}))

    assert plan.guards["B_single_chain"] is False
    assert plan.renames == []


def test_a_partial_chain_never_renumbers_only_the_part_it_traversed():
    """The property that matters, stated directly: whenever a renumber is planned,
    every new revision is in it."""
    onto = [*chain("0016", "0017"), rev("0018", "0017", slug="theirs", col="t")]
    branch = [
        *chain("0016", "0017"),
        rev("0018", "0017", slug="mine_a", col="a"),
        rev("0019", "0018", slug="mine_b", col="b"),
        rev("0020", "0018", slug="mine_c", col="c"),
    ]
    plan = mr.reconcile(onto, branch, frozenset({"0016", "0017"}))

    assert plan.action == "stop"
    assert "0018" in plan.reason  # the collision is named, not quietly dropped


# ---------------------------------------------------------------------------
# edges that point nowhere, and history the branch removed
# ---------------------------------------------------------------------------


def test_a_parent_present_at_neither_ref_stops_instead_of_being_reparented():
    """`0099` is absent from both refs, so the revision passes the "links into
    pre-existing history" test and is treated as a base — and the relink then replaces
    that unknown parent with the integration head, discarding the edge."""
    onto = chain("0016", "0017")
    branch = [*onto, rev("0018", "0099", slug="orphan", col="o")]
    plan = mr.reconcile(onto, branch, frozenset({"0016", "0017"}))

    assert plan.action == "stop" and not plan.go
    assert "0099" in plan.reason


def test_an_unknown_depends_on_target_stops_too():
    src = text("0018", "0017").replace(
        "depends_on: str | Sequence[str] | None = None", 'depends_on = ("0099",)'
    )
    onto = chain("0016", "0017")
    branch = [*onto, mr.parse_migration(src, path="migrations/versions/0018_x.py")]
    plan = mr.reconcile(onto, branch, frozenset({"0016", "0017"}))

    assert plan.action == "stop" and "0099" in plan.reason


def test_deleting_a_shared_migration_is_not_a_noop():
    """Everything downstream is built from files that are *present*, so a branch that
    only deletes reports "branch added no migrations; merge is graph-clean" — while
    the real merge deletes the file too and leaves `0018` pointing at nothing."""
    onto = chain("0016", "0017", "0018")
    branch = chain("0016", "0017")
    plan = mr.reconcile(onto, branch, frozenset({"0016", "0017", "0018"}))

    assert plan.action == "stop" and not plan.go
    assert "0018" in plan.reason


def test_a_migration_the_integration_ref_added_is_not_read_as_a_deletion():
    """The branch never had `0018`, so its absence is main moving on, not a delete —
    which is the ordinary relink case and must not be caught by the guard above."""
    onto = chain("0016", "0017", "0018")
    branch = [*chain("0016", "0017"), rev("0019", "0017", slug="mine", col="m")]
    plan = mr.reconcile(onto, branch, frozenset({"0016", "0017"}))

    assert plan.action == "relink"


# ---------------------------------------------------------------------------
# metadata that cannot be read, rather than guessed at
# ---------------------------------------------------------------------------


def test_a_down_revision_that_is_a_constant_is_refused_not_read_as_a_root():
    """`()` from an unreadable value is indistinguishable from `down_revision = None`,
    and the advice that follows — "reattach it to the migration chain" — is wrong for
    a file that IS attached."""
    src = text("0018", "0017").replace(
        'down_revision: str | None = "0017"', "down_revision = PREVIOUS"
    )
    with pytest.raises(mr.MigrationParseError) as e:
        mr.parse_migration(src, path="migrations/versions/0018_x.py")
    assert "down_revision" in str(e.value)


def test_an_absent_down_revision_is_refused_rather_than_treated_as_a_root():
    src = text("0018", "0017").replace('down_revision: str | None = "0017"\n', "")
    with pytest.raises(mr.MigrationParseError):
        mr.parse_migration(src, path="migrations/versions/0018_x.py")


def test_a_revision_assigned_inside_an_if_is_refused_rather_than_dropped():
    """It parses as "not a migration" on the `revision` lookup, but the file plainly is
    one — dropping it would take a node out of the graph and under-count heads."""
    src = text("0018", "0017").replace(
        'revision: str = "0018"', 'if True:\n    revision: str = "0018"'
    )
    with pytest.raises(mr.MigrationParseError):
        mr.parse_migration(src, path="migrations/versions/0018_x.py")


def test_a_revision_quoted_in_the_module_docstring_is_not_the_files_identity():
    """The docstring quotes an assignment as prose, at column zero. A line-anchored
    regex takes it as the file's id — and then `apply` rewrites the docstring."""
    src = '"""Follows on from\n\nrevision = "0016"\n"""\n' + text("0018", "0017")
    assert mr.parse_migration(src).id == "0018"


def test_a_backslash_escape_in_a_value_does_not_derail_the_parser():
    src = text("0018", "0017").replace(
        'down_revision: str | None = "0017"', 'down_revision = "a\\"b"'
    )
    assert mr.parse_migration(src).down == ('a"b',)


def test_a_helper_module_with_no_migration_metadata_is_skipped_not_refused():
    with pytest.raises(ValueError):
        mr.parse_migration("import os\n\nHELPERS = {}\n")


def test_a_helper_module_in_the_versions_dir_is_reported_as_skipped(collided_repo: Path):
    (collided_repo / "migrations" / "versions" / "helpers.py").write_text("HELPERS = {}\n")
    _commit(collided_repo, "add a helper module")
    skipped: list[str] = []

    mr.revs_at_ref(str(collided_repo), "HEAD", skipped=skipped)

    assert skipped == ["migrations/versions/helpers.py"]


def test_an_unreadable_migration_in_the_versions_dir_stops_the_run(collided_repo: Path):
    (collided_repo / "migrations" / "versions" / "0020_bad.py").write_text(
        'revision = COMPUTED\ndown_revision = "0018"\n'
    )
    _commit(collided_repo, "add an unreadable migration")

    with pytest.raises(mr.MigrationParseError):
        mr.revs_at_ref(str(collided_repo), "HEAD")


# ---------------------------------------------------------------------------
# depends_on, which the renumber used to leave naming a dead id
# ---------------------------------------------------------------------------


def test_renumbering_rewrites_a_depends_on_inside_the_moved_chain():
    """`heads()` deliberately ignores `depends_on`, so a stale one still verifies as a
    single head and the plan reports GO — Alembic then fails at runtime instead."""
    src = text("0019", "0018").replace(
        "depends_on: str | Sequence[str] | None = None", 'depends_on = ("0018",)'
    )
    onto = [*chain("0016", "0017"), rev("0018", "0017", slug="theirs", col="t")]
    branch = [
        *chain("0016", "0017"),
        rev("0018", "0017", slug="mine_a", col="a"),
        mr.parse_migration(src, path="migrations/versions/0019_mine_b.py"),
    ]
    plan = mr.reconcile(onto, branch, frozenset({"0016", "0017"}))

    assert plan.action == "renumber"
    assert [(rn.old_id, rn.new_id, rn.new_depends) for rn in plan.renames] == [
        ("0018", "0019", ()),
        ("0019", "0020", ("0019",)),
    ]


def test_apply_writes_the_rewritten_depends_on_to_disk(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "0016_a.py", "0016", None, "a")
    _write(repo, "0017_b.py", "0017", "0016", "b")
    _commit(repo, "chain to 0017")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "0018_mine_a.py", "0018", "0017", "a")
    (repo / "migrations" / "versions" / "0019_mine_b.py").write_text(
        text("0019", "0018", col="b").replace(
            "depends_on: str | Sequence[str] | None = None", 'depends_on = ("0018",)'
        )
    )
    _commit(repo, "feature: 0018 and 0019")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "0018_theirs.py", "0018", "0017", "theirs")
    _commit(repo, "main: 0018")
    _git(repo, "checkout", "-q", "feature")

    assert mr.cmd_apply(_args(repo)) == 0

    moved = mr.parse_migration((repo / "migrations" / "versions" / "0020_mine_b.py").read_text())
    assert (moved.id, moved.down, moved.depends) == ("0020", ("0019",), ("0019",))


# ---------------------------------------------------------------------------
# the CLI surface, which is what #96's gate consumes
# ---------------------------------------------------------------------------


def test_preflight_json_carries_the_keys_a_gate_reads(collided_repo: Path, capsys):
    assert mr.main(["preflight", "--repo", str(collided_repo), "--onto", "main", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "renumber"
    assert payload["exit_code"] == 0
    assert payload["merged_single_head"] is True
    assert payload["merged_heads"] == ["0019"]
    assert payload["rejected_plan"] is None
    assert set(payload["guards"]) == {
        "A_onto_single_head",
        "B_single_chain",
        "C_no_shared_rewrite",
    }
    assert payload["onto_sha"] and payload["branch_sha"]


def test_a_post_resolution_stop_does_not_ship_the_renames_it_just_refused(
    collided_repo: Path, capsys
):
    """`{"action": "stop", "go": false, "renames": [...]}` reads as "stop, and here are
    the edits to make". The refused resolution moves under `rejected_plan`."""
    plan = mr.Plan("renumber", "", go=True, renames=[mr.Rename("0018", "0019", "a", "b", (), ())])

    rejected = mr._reject(plan, "post-resolution graph has heads ['0018', '0019']; do not land")

    assert (rejected.action, rejected.go) == ("stop", False)
    assert rejected.renames == [] and rejected.base is None and rejected.new_down == ()


def _plan_whose_rename_matches_nothing() -> mr.Plan:
    """A renumber plan naming a file that is not there.

    The post-resolution check is a backstop — every resolution `reconcile` returns
    today verifies clean, which is the point of it — so the honest way to exercise the
    backstop is to hand it a plan the planner would not produce. This is the shape a
    mis-matched rename leaves behind: the branch's copy keeps the contested id, and the
    merged graph carries `0018` twice.

    The rename names a file that exists at the branch ref — the warning scan reads it —
    but not the one carrying `0018`, so `_is_renamed` matches nothing.
    """
    return mr.Plan(
        "renumber",
        "a rename that matches no revision",
        go=True,
        onto_head="0018",
        collisions=["0018"],
        renames=[
            mr.Rename(
                "0018",
                "0019",
                "migrations/versions/0017_b.py",
                "migrations/versions/0019_b.py",
                ("0017",),
                ("0018",),
            )
        ],
    )


def test_a_resolution_that_would_leave_a_duplicate_is_caught_before_it_is_blessed(
    collided_repo: Path,
):
    onto = mr.revs_at_ref(str(collided_repo), "main")
    branch = mr.revs_at_ref(str(collided_repo), "feature")

    problem, _ok, _h = mr._post_resolution_problem(
        onto, branch, _plan_whose_rename_matches_nothing()
    )

    assert problem and "duplicate revision id(s) ['0018']" in problem


def test_preflight_flips_a_go_plan_to_stop_and_moves_it_under_rejected_plan(
    collided_repo: Path, capsys, monkeypatch
):
    """The flip was only ever exercised by calling `verify_single_head` directly, so
    nothing checked what the JSON a gate reads looks like afterwards."""
    monkeypatch.setattr(mr, "reconcile", lambda *a, **k: _plan_whose_rename_matches_nothing())

    code = mr.main(["preflight", "--repo", str(collided_repo), "--onto", "main", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert (payload["action"], payload["go"], payload["exit_code"]) == ("stop", False, 2)
    # the refused resolution is reported as refused, not as work to do
    assert payload["renames"] == []
    assert payload["rejected_plan"]["action"] == "renumber"
    assert payload["rejected_plan"]["renames"]


def test_apply_refuses_the_same_resolution_preflight_would_not_bless(
    collided_repo: Path, capsys, monkeypatch
):
    """`apply` re-runs the check rather than trusting that preflight was run."""
    monkeypatch.setattr(mr, "reconcile", lambda *a, **k: _plan_whose_rename_matches_nothing())

    assert mr.main(["apply", "--repo", str(collided_repo), "--onto", "main"]) == 2
    assert "duplicate revision id" in capsys.readouterr().err
    assert (collided_repo / "migrations" / "versions" / "0018_base_sha.py").exists()


def test_heads_exits_zero_on_a_migration_free_ref(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("no migrations here\n")
    _commit(repo, "empty")

    assert mr.main(["heads", "--repo", str(repo)]) == 0
    assert "(no migrations)" in capsys.readouterr().out


def test_heads_exits_zero_on_a_single_head_and_two_on_a_split(collided_repo: Path):
    assert mr.main(["heads", "--repo", str(collided_repo), "--ref", "main"]) == 0
    _git(collided_repo, "checkout", "-q", "-b", "split")
    _write(collided_repo, "0018_third.py", "0018b", "0017", "third")
    _commit(collided_repo, "a second head")
    assert mr.main(["heads", "--repo", str(collided_repo), "--ref", "HEAD"]) == 2


def test_an_integration_ref_with_no_migrations_is_not_a_stop():
    """`cmd_heads` calls a migration-free ref fine, so the planner must agree — landing
    the very first migration into a fresh repo was blocked with "integration ref has 0
    heads []; reconcile it on its own branch first", which is not advice."""
    branch = chain("0016", "0017")
    plan = mr.reconcile([], branch, frozenset())

    assert plan.action == "noop" and plan.go and plan.exit_code == 0


def test_several_independent_roots_are_still_ambiguous_against_an_empty_ref():
    branch = [rev("0016", None, slug="a"), rev("0017", None, slug="b")]
    plan = mr.reconcile([], branch, frozenset())

    assert plan.action == "stop"


def test_a_missing_ref_is_a_stop_with_advice_not_a_traceback(collided_repo: Path, capsys):
    """The likeliest first-run failure: `--onto origin/main` in a checkout that never
    fetched it. A gate reading the 0/2/3 scheme sees Python's exit 1 as "unknown"."""
    assert mr.main(["preflight", "--repo", str(collided_repo), "--onto", "origin/main"]) == 2

    err = capsys.readouterr().err
    assert "origin/main" in err and "Fetch it" in err


def test_a_missing_blob_is_reported_rather_than_dropped(collided_repo: Path):
    """`_cat_file_batch` used to omit missing specs silently, which takes a migration
    out of the graph and under-counts heads."""
    with pytest.raises(mr.ReconcileError):
        mr._cat_file_batch(str(collided_repo), ["HEAD:migrations/versions/does_not_exist.py"])


def test_git_failures_carry_the_stderr_git_printed(collided_repo: Path):
    with pytest.raises(mr.GitError) as e:
        mr._git(str(collided_repo), "rev-parse", "--verify", "no/such/ref")

    assert e.value.returncode != 0
    assert e.value.stderr.strip()


def test_a_failed_stale_reference_scan_says_so_rather_than_reporting_nothing(collided_repo: Path):
    """`git grep` exits 1 on no match and 128 on a bad ref. Treating them alike means
    a scan that never ran reports a clean bill, and these warnings are the only thing
    telling the caller that prose went stale."""
    plan = mr.reconcile(
        mr.revs_at_ref(str(collided_repo), "main"),
        mr.revs_at_ref(str(collided_repo), "feature"),
        mr.ancestor_ids_of(str(collided_repo), "main", "feature"),
    )

    warnings = mr.stale_references(str(collided_repo), "no-such-ref", plan)

    assert warnings and "scan failed" in warnings[0]


def test_a_larger_number_containing_the_old_one_is_not_reported_as_stale(collided_repo: Path):
    """A bare four-digit needle matched `20240018`, lockfile hashes and port numbers,
    and buried the real CHANGELOG and docstring hits."""
    (collided_repo / "notes.md").write_text("build 20240018 and issue 100189 are unrelated\n")
    _commit(collided_repo, "notes")
    plan = mr.reconcile(
        mr.revs_at_ref(str(collided_repo), "main"),
        mr.revs_at_ref(str(collided_repo), "feature"),
        mr.ancestor_ids_of(str(collided_repo), "main", "feature"),
    )

    warnings = mr.stale_references(str(collided_repo), "HEAD", plan)

    assert not any("notes.md" in w for w in warnings)


def test_a_multiline_assignment_is_not_reported_as_stale_prose(tmp_path: Path):
    """The old per-line filter needed `revision`/`down_revision` on the line carrying
    the hit, so a value split across lines was reported as stale prose on an assignment
    the renumber does rewrite."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "0016_a.py", "0016", None, "a")
    _write(repo, "0017_b.py", "0017", "0016", "b")
    _commit(repo, "chain to 0017")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "0018_mine_a.py", "0018", "0017", "a")
    # The old id lands on a line carrying no keyword at all — which is exactly what the
    # per-line filter needed in order to recognise it as an assignment.
    (repo / "migrations" / "versions" / "0019_mine_b.py").write_text(
        text("0019", "0018", col="b").replace(
            'down_revision: str | None = "0018"', 'down_revision = (\n    "0018",\n)'
        )
    )
    _commit(repo, "feature: 0018 and 0019")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "0018_theirs.py", "0018", "0017", "t")
    _commit(repo, "main: 0018")
    _git(repo, "checkout", "-q", "feature")

    plan = mr.reconcile(
        mr.revs_at_ref(str(repo), "main"),
        mr.revs_at_ref(str(repo), "feature"),
        mr.ancestor_ids_of(str(repo), "main", "feature"),
    )
    warnings = mr.stale_references(str(repo), "feature", plan)

    # Line 3 of each file is the docstring's prose reference, which IS stale and is
    # reported. Line 49, the `"0018"` inside the multiline tuple, is not.
    assert warnings == [
        "migrations/versions/0018_mine_a.py:3 still names 0018",
        "migrations/versions/0019_mine_b.py:3 still names 0019",
    ]


def test_the_repos_own_migration_chain_is_single_headed():
    """A guard on this repo rather than a fixture, read from the WORKTREE rather than
    from HEAD.

    Reading `HEAD` made it useless at the moment it was meant to help: an agent adding
    `0018_something.py` and running the suite before committing got a pass, because the
    new file is not at HEAD yet — the guard only went red once the commit existed. It
    also failed on any work-in-progress branch or detached HEAD while `main` was fine,
    which its own docstring said it was about.
    """
    repo = Path(__file__).resolve().parent.parent
    try:
        tracked = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z", "--", "migrations/versions"],
            capture_output=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("not a git checkout — nothing to guard")

    paths = [repo / p for p in tracked.decode().split("\0") if p.endswith(".py")]
    on_disk = {p for p in (repo / "migrations" / "versions").glob("*.py")}
    revs = []
    for path in sorted(set(paths) | on_disk):
        if path.name == "__init__.py" or not path.exists():
            continue
        raw = path.read_bytes()
        revs.append(mr.parse_migration(raw.decode("utf-8"), path=str(path), raw=raw))

    if not revs:
        pytest.skip("no migrations in this copy of the tree")
    assert mr.duplicate_ids(revs) == [], "two migrations in the worktree claim one id"
    assert len(mr.heads(revs)) == 1, "the worktree's migration chain has two heads"


# ---------------------------------------------------------------------------
# the CI job that runs `heads`
# ---------------------------------------------------------------------------
#
# `harness/githooks/pre-push` asks this same question and, in this fleet, can never be the
# one to answer it: the hook gates the migration half on `is_protected "$branch"`, so a
# feature-branch push skips it, and `gh pr merge` goes through the GitHub API and touches
# no local hook at all. Twelve PRs landed that way on 2026-08-22 and the check ran on none
# of them (#351). So `.github/workflows/tests.yml` carries a `migration-heads` job that
# asks it at the merge, and the tests below assert that job's shape and then EXECUTE its
# own script body against throwaway repositories.
#
# Running the body rather than reading it is the point. A workflow step is the one kind of
# code in this repo that nothing else ever calls, and the failure mode it has — passing
# because it read nothing — is silent by construction. `fetch-depth` is the specific
# worry, and `test_a_checkout_without_the_base_branch_is_refused` is that worry run rather
# than described.

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "tests.yml"
RECONCILER = Path(__file__).resolve().parent.parent / "scripts" / "migration_reconcile.py"

# The job's body is bash driving git and python3. All three are on ubuntu-latest, where
# the `app suite` job collects this file, so nothing here skips in CI.
_MISSING_TOOLS = tuple(t for t in ("bash", "git", "python3") if not shutil.which(t))
needs_a_shell = pytest.mark.skipif(
    bool(_MISSING_TOOLS),
    reason=f"the job's body is bash driving git and python3; missing: {', '.join(_MISSING_TOOLS)}",
)


def _uncommented(step: dict) -> str:
    """A step's `run` with its comment lines dropped — so a job is found by what it
    executes and not by what its comments happen to mention."""
    return "\n".join(
        line
        for line in str(step.get("run", "")).splitlines()
        if not line.lstrip().startswith("#")
    )


def _runs_the_reconciler(step: dict) -> bool:
    return "migration_reconcile.py" in _uncommented(step)


def _heads_job() -> dict:
    """The job that runs the reconciler, found by what it runs rather than by its name."""
    yaml = pytest.importorskip("yaml")
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    running = [
        job
        for job in jobs.values()
        if any(_runs_the_reconciler(step) for step in job.get("steps", []))
    ]
    assert len(running) == 1, (
        f"{len(running)} jobs in tests.yml run `migration_reconcile.py`; the guard against "
        "a merge that would leave two migration heads has to run exactly once and has to "
        "run at all — the pre-push hook cannot cover it, because this fleet merges through "
        "the GitHub API"
    )
    return running[0]


def _heads_script() -> str:
    return str(next(s for s in _heads_job()["steps"] if _runs_the_reconciler(s))["run"])


def test_the_heads_job_checks_out_the_whole_history():
    """The one way this job can be wrong and look right.

    `actions/checkout@v4` fetches a single commit by default, and the base branch is then
    not in the clone at all. #348's `frozen` job put it best: that is the shape of a job
    that reports green while verifying nothing.
    """
    checkouts = [
        step
        for step in _heads_job()["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout")
    ]
    assert checkouts, "the migration-heads job does not check the repo out at all"
    assert all(str(step.get("with", {}).get("fetch-depth")) == "0" for step in checkouts), (
        "the migration-heads job checks out at the default depth of 1, so the base branch "
        "is not in the clone and the merge cannot be judged against what it targets"
    )


def test_the_heads_job_runs_on_pull_requests():
    """Where this fleet actually merges. `pre-push` covers a direct push to a protected
    branch and nothing else — and nobody here pushes one."""
    assert "pull_request" in str(_heads_job().get("if", "")), (
        "the migration-heads job does not name pull_request in its `if`, so it does not "
        "run on the event this fleet lands PRs through, which was the whole of #351"
    )


def test_the_heads_job_records_what_it_cannot_catch():
    """#351 asked for the limitation to sit on the job, not only in the issue.

    A duplicate revision id minted by two branches that have both yet to land is invisible
    here — each branch is single-headed on its own — and the person who trips this check
    should learn that from the check rather than from a later incident.
    """
    raw = WORKFLOW.read_text(encoding="utf-8")
    block = raw[raw.index("\n  migration-heads:") :]
    assert "#338" in block and "#341" in block, (
        "the migration-heads job no longer names #338 (the blind spot it inherits) and "
        "#341 (what closes it), so it now reads as covering more than it does"
    )


def _job_repo(tmp_path: Path) -> Path:
    """A repo laid out the way the runner's workspace is: the reconciler at the path the
    job invokes, and git identity pinned by `_git`."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(RECONCILER, repo / "scripts" / "migration_reconcile.py")
    _git(repo, "init", "-q", "-b", "main")
    return repo


def _as_pull_request(repo: Path) -> None:
    """Leave the repo the way `actions/checkout@v4` leaves a pull_request run: the base at
    `refs/remotes/origin/main`, and HEAD detached on the merge commit GitHub builds for the
    PR — which is what would actually land, and so what the job has to judge."""
    _git(repo, "update-ref", "refs/remotes/origin/main", "main")
    _git(repo, "checkout", "-q", "--detach", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge pull request", "work")


def _run_job(repo: Path, base_ref: str = "main") -> subprocess.CompletedProcess:
    """The workflow step's own script, run verbatim."""
    return subprocess.run(
        ["bash", "-c", _heads_script()],
        cwd=repo,
        env={**os.environ, "BASE_REF": base_ref},
        capture_output=True,
        text=True,
    )


@pytest.fixture
def split_merge_repo(tmp_path: Path) -> Path:
    """The case the job exists for, and the reason a per-branch check cannot see it.

    `main` moves on to `0019` off `0017`; the branch adds `0018`, also off `0017`. Neither
    ref is two-headed on its own — `main` has one head and so does the branch, so every
    per-branch gate in the repo says GO — and the merge of the two has two.
    """
    repo = _job_repo(tmp_path)
    _write(repo, "0016_a.py", "0016", None, "a")
    _write(repo, "0017_b.py", "0017", "0016", "b")
    _commit(repo, "chain to 0017")
    _git(repo, "checkout", "-q", "-b", "work")
    _write(repo, "0018_mine.py", "0018", "0017", "mine")
    _commit(repo, "branch: 0018 on 0017")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "0019_theirs.py", "0019", "0017", "theirs")
    _commit(repo, "main: 0019 on 0017")
    _as_pull_request(repo)
    return repo


@needs_a_shell
def test_neither_side_of_the_split_is_two_headed_on_its_own(split_merge_repo: Path):
    """The premise of the test below, asserted rather than assumed: if either ref were
    already refused, the merge being refused would prove nothing about the merge."""
    repo = str(split_merge_repo)
    assert mr.main(["heads", "--repo", repo, "--ref", "origin/main"]) == 0
    assert mr.main(["heads", "--repo", repo, "--ref", "work"]) == 0


@needs_a_shell
def test_the_job_refuses_a_merge_that_would_leave_two_migration_heads(split_merge_repo: Path):
    """The acceptance criterion of #351, on a real two-head graph rather than a stand-in."""
    done = _run_job(split_merge_repo)

    assert done.returncode != 0, f"the job passed a two-headed merge:\n{done.stdout}"
    assert "0018" in done.stdout and "0019" in done.stdout, (
        f"the refusal does not carry the reconciler's own head list:\n{done.stdout}"
    )
    assert "::error::" in done.stdout, "the refusal leaves no annotation on the run"
    assert "this branch is where it comes from" in done.stdout, (
        "the base is single-headed, so the refusal has to say the branch introduced the "
        f"second head rather than leaving the reader to guess:\n{done.stdout}"
    )
    assert "#341" in done.stdout, "the refusal does not say what it cannot catch"


@needs_a_shell
def test_the_job_passes_a_merge_that_adds_one_migration_in_the_right_place(tmp_path: Path):
    repo = _job_repo(tmp_path)
    _write(repo, "0016_a.py", "0016", None, "a")
    _write(repo, "0017_b.py", "0017", "0016", "b")
    _commit(repo, "chain to 0017")
    _git(repo, "checkout", "-q", "-b", "work")
    _write(repo, "0018_mine.py", "0018", "0017", "mine")
    _commit(repo, "branch: 0018 on 0017")
    _as_pull_request(repo)

    done = _run_job(repo)

    assert done.returncode == 0, done.stdout + done.stderr
    assert "migration graph after this merge: 0018" in done.stdout


@needs_a_shell
def test_a_pull_request_touching_no_migration_is_a_no_op(tmp_path: Path):
    repo = _job_repo(tmp_path)
    _write(repo, "0016_a.py", "0016", None, "a")
    _write(repo, "0017_b.py", "0017", "0016", "b")
    _commit(repo, "chain to 0017")
    _git(repo, "checkout", "-q", "-b", "work")
    (repo / "README.md").write_text("nothing to do with migrations\n")
    _commit(repo, "branch: docs only")
    _as_pull_request(repo)

    done = _run_job(repo)

    assert done.returncode == 0, done.stdout + done.stderr
    assert "migration graph after this merge: 0017" in done.stdout


@needs_a_shell
def test_a_repo_with_no_migrations_at_all_passes_and_says_so(tmp_path: Path):
    """The job is wired into a workflow that also runs on repos mid-bootstrap, and the
    reconciler's answer for "there is nothing to break" is a sentence, not an exit 2."""
    repo = _job_repo(tmp_path)
    (repo / "README.md").write_text("no migrations here\n")
    _commit(repo, "empty")
    _git(repo, "checkout", "-q", "-b", "work")
    (repo / "README.md").write_text("still none\n")
    _commit(repo, "branch: still none")
    _as_pull_request(repo)

    done = _run_job(repo)

    assert done.returncode == 0, done.stdout + done.stderr
    assert "(no migrations)" in done.stdout


@needs_a_shell
def test_a_branch_that_resolves_a_two_headed_base_is_green(tmp_path: Path):
    """The gate is the merge commit, never the base — otherwise the one branch that fixes
    a two-headed `main` is the one branch that cannot be merged."""
    repo = _job_repo(tmp_path)
    _write(repo, "0016_a.py", "0016", None, "a")
    _write(repo, "0017_b.py", "0017", "0016", "b")
    _write(repo, "0018_mine.py", "0018", "0017", "mine")
    _write(repo, "0019_theirs.py", "0019", "0017", "theirs")
    _commit(repo, "main: two heads, 0018 and 0019 both on 0017")
    _git(repo, "checkout", "-q", "-b", "work")
    _write(repo, "0019_theirs.py", "0019", "0018", "theirs")
    _commit(repo, "branch: relink 0019 onto 0018")
    _as_pull_request(repo)

    done = _run_job(repo)

    assert done.returncode == 0, done.stdout + done.stderr
    assert "migration graph after this merge: 0019" in done.stdout
    assert "this PR resolves it" in done.stdout


@needs_a_shell
def test_a_checkout_without_the_base_branch_is_refused(split_merge_repo: Path):
    """`fetch-depth: 0`, run rather than described.

    At the default depth of 1 the base branch is not fetched, and a job that shrugged that
    off would be green on precisely the graph the fixture above is two-headed on. So the
    same repo, minus the base ref: it must go red, and it must name the reason.
    """
    _git(split_merge_repo, "update-ref", "-d", "refs/remotes/origin/main")

    done = _run_job(split_merge_repo)

    assert done.returncode != 0, f"a checkout with no base branch reported clean:\n{done.stdout}"
    assert "fetch-depth" in done.stdout, (
        f"the refusal does not name the checkout depth that caused it:\n{done.stdout}"
    )


@needs_a_shell
def test_a_duplicate_revision_id_reads_as_the_reconciler_refusing_rather_than_as_two_heads(
    tmp_path: Path,
):
    """Two answers arrive as the same exit 2, and the wrong one is the reassuring one.

    `heads` returns 2 by itself when it counted the heads and the count was not one. It
    also returns 2 when it declined to build a graph at all — a duplicate revision id, an
    unreadable migration, git failing underneath it — and there the head count is not a
    thing it has. Reporting the second as "two heads" would send a reader off to renumber a
    graph that may be fine, so the job splits them on the reconciler's own `STOP:`.

    The graph here is the one this repo actually reaches: `main` landed `0018` and the
    branch minted its own. Once one of the two has landed this IS caught — it is only the
    pair where BOTH are unlanded that no ref-against-ref check can see (#338).
    """
    repo = _job_repo(tmp_path)
    _write(repo, "0016_a.py", "0016", None, "a")
    _write(repo, "0017_b.py", "0017", "0016", "b")
    _commit(repo, "chain to 0017")
    _git(repo, "checkout", "-q", "-b", "work")
    _write(repo, "0018_mine.py", "0018", "0017", "mine")
    _commit(repo, "branch: 0018")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "0018_theirs.py", "0018", "0017", "theirs")
    _commit(repo, "main: 0018")
    _as_pull_request(repo)

    done = _run_job(repo)

    assert done.returncode != 0, f"the job passed a duplicate revision id:\n{done.stdout}"
    assert "duplicate revision id" in done.stdout, (
        f"the refusal does not carry the reconciler's own words:\n{done.stdout}"
    )
    assert "not exactly one head" not in done.stdout, (
        "the reconciler never counted the heads here, so the job must not report a head "
        f"count it was not given:\n{done.stdout}"
    )
