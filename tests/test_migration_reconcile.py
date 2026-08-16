"""Tests for `scripts/migration_reconcile.py`.

The graph core is a pure function of migration file text, so almost everything here
is fixture graphs and no I/O at all. The two `git`-backed tests at the bottom build a
throwaway repo — they exercise the git layer and the on-disk rewrite, and still need
no database.

The test this file exists for is `test_duplicate_number_is_not_a_clean_merge`: the
donor implementation reports that graph as a clean no-op, which is the failure the
whole tool is aimed at.
"""

from __future__ import annotations

import importlib.util
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


def test_an_unnumbered_id_in_a_contested_chain_falls_back_to_the_merge_migration():
    """The chain has to be renumbered and one of its links carries no number to
    renumber, so the arithmetic has nothing to work from. Say so rather than invent
    a position for it."""
    onto = [*chain("0016", "0017"), rev("0018", "0017", slug="theirs", col="t")]
    branch = [
        *chain("0016", "0017"),
        rev("0018", "0017", slug="mine", col="m"),
        mr.parse_migration(text("beef01", "0018"), path="b.py"),
    ]
    plan = mr.reconcile(onto, branch, frozenset({"0016", "0017"}))

    assert plan.action == "merge" and plan.go and plan.exit_code == 3
    assert "beef01" in plan.reason


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


# ---------------------------------------------------------------------------
# the guards
# ---------------------------------------------------------------------------


def test_a_multi_head_integration_ref_stops_before_anything_else_is_judged():
    onto = [*chain("0016", "0017"), rev("0018", "0016", slug="other", col="o")]
    plan = mr.reconcile(onto, [*onto, rev("0019", "0017")], frozenset())

    assert plan.action == "stop"
    assert plan.guards == {"A_onto_single_head": False}


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
    out = mr._rewrite_assignment(text("0018", "0017"), "down_revision", "0018")
    assert 'down_revision: str | None = "0018"' in out
    assert mr.parse_migration(out).down == ("0018",)


def test_rewriting_a_value_preserves_a_trailing_comment():
    src = text("0018", "0017").replace(
        'down_revision: str | None = "0017"',
        'down_revision: str | None = "0017"  # cut from the v2.26 head',
    )
    out = mr._rewrite_assignment(src, "down_revision", "0018")
    assert 'down_revision: str | None = "0018"  # cut from the v2.26 head' in out


def test_rewriting_a_missing_assignment_raises_rather_than_writing_nothing():
    with pytest.raises(RuntimeError):
        mr._rewrite_assignment("revision = '0018'\n", "down_revision", "0019")


# ---------------------------------------------------------------------------
# the git layer
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "user.name=test",
        "commit",
        "-q",
        "-m",
        message,
    )


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


def test_preflight_reads_the_graph_out_of_git(collided_repo: Path):
    onto = mr.revs_at_ref(str(collided_repo), "main")
    branch = mr.revs_at_ref(str(collided_repo), "feature")
    ancestors = mr.ancestor_ids_of(str(collided_repo), "main", "feature")

    assert ancestors == frozenset({"0016", "0017"})
    plan = mr.reconcile(onto, branch, ancestors)
    assert plan.action == "renumber" and plan.collisions == ["0018"]


def test_apply_renumbers_the_working_tree_and_leaves_it_uncommitted(collided_repo: Path):
    args = Namespace(
        repo=str(collided_repo),
        onto="main",
        branch="HEAD",
        versions_path="migrations/versions",
    )
    code = mr.cmd_apply(args)

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


def test_apply_refuses_when_the_named_branch_is_not_checked_out(collided_repo: Path):
    _git(collided_repo, "checkout", "-q", "main")
    args = Namespace(
        repo=str(collided_repo),
        onto="main",
        branch="feature",
        versions_path="migrations/versions",
    )
    assert mr.cmd_apply(args) == 2


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


def test_the_real_repos_own_chain_is_single_headed():
    """A guard on this repo rather than a fixture: `main` must never carry two heads.
    Runs against the checkout the suite is in."""
    repo = str(Path(__file__).resolve().parent.parent)
    assert len(mr.heads(mr.revs_at_ref(repo, "HEAD"))) == 1
