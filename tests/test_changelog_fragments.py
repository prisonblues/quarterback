"""Tests for `scripts/changelog_fragments.py`.

Every assembly test builds a throwaway repo of two files, for the reason
`tests/test_release_stamp.py` gives: a suite asserting about this checkout's own CHANGELOG
goes red on the day somebody lands a release. The one exception is
`test_this_repos_own_fragments_parse`, which reads `changelog.d/` — a fragment nobody can
parse is a landing that fails at the last step, and the directory is empty most of the time.

The tests that matter are the ones where this could be wrong and silently so:

  * `test_assembling_twice_is_refused_rather_than_writing_a_second_entry` — two `## vNEXT`
    headings is a state `release_stamp.py apply` resolves by stamping BOTH with the same
    number, which is the collision this whole convention exists to prevent.
  * `test_a_fragment_naming_a_version_is_refused` — a fragment that names one has opted its
    branch back into the race, and it would read as correct right up to the merge.
  * `test_the_bullet_lands_where_the_renderer_puts_it` — the README bullet and the CHANGELOG
    entry are written by one command precisely so they cannot disagree.

The `required` half, below the assembly tests, is the opposite question — not "is this
fragment well formed" but "should there have been one at all" (#365). Those build real git
repos, because the subject is two refs, a fork point and a trailer read by git's own parser.
The ones that matter there:

  * `test_merging_the_base_in_does_not_count_as_writing_an_entry` — #363's actual head, which
    inherited three releases' headings and wrote nothing; a fork-relative read of the CHANGELOG
    passes the very branch the check exists for.
  * `test_a_commit_quoting_the_refusal_does_not_waive_it` — the refusal ends with a pasteable
    trailer, so the likeliest message this branch will ever produce contains that line.
  * `test_no_fork_point_is_refused_rather_than_passed` — a depth-1 checkout has none, and a
    gate that answers "nothing changed" there is green while verifying nothing.
  * `test_the_rule_agrees_with_real_pull_requests` — the scoping rule against the paths real
    PRs really changed, because a rule tested only on invented file lists is a rule tested
    against its author's idea of the repo.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# `scripts/` is a directory of standalone tools, not an importable package.
_SPEC = importlib.util.spec_from_file_location(
    "changelog_fragments", REPO_ROOT / "scripts" / "changelog_fragments.py")
cf = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = cf
_SPEC.loader.exec_module(cf)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

CHANGELOG = """# Version history

Entries are newest first.

## v2.1 — dev context

The body of v2.1.

## v2 — presence

The body of v2.
"""

README = """# Board

### Every release, oldest first

Ending with what is next:

- **v2** — presence.
- **v2.1** — dev context.
- **Not yet numbered** — a roadmap item.

**[CHANGELOG.md](CHANGELOG.md)** has each release in full.
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    (tmp_path / "README.md").write_text(README, encoding="utf-8")
    (tmp_path / cf.FRAGMENT_DIR).mkdir()
    return tmp_path


def fragment(repo: Path, name: str, text: str) -> Path:
    path = repo / cf.FRAGMENT_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


BODY = "# a branch stops guessing\n\nWhat was broken before this: the thing.\n"


# ---------------------------------------------------------------------------
# what a fragment is
# ---------------------------------------------------------------------------

def test_a_fragment_is_its_title_and_its_body(repo):
    fragment(repo, "296.feat.md", BODY)
    [f] = cf.load(repo)
    assert (f.issue, f.kind, f.title) == ("296", "feat", "a branch stops guessing")
    assert f.body == "What was broken before this: the thing."


def test_a_change_with_no_issue_uses_the_plus_form(repo):
    fragment(repo, "+worktree-db.fix.md", BODY)
    [f] = cf.load(repo)
    assert f.issue == "+worktree-db"


def test_the_directorys_own_readme_is_not_a_fragment(repo):
    """It documents the convention. Swept into a release entry, it would be a funny way to
    find out — and the entry would carry a `#` heading, which is separately refused."""
    fragment(repo, "README.md", "# changelog.d\n\nHow to write one of these.\n")
    fragment(repo, "296.feat.md", BODY)
    assert [f.path.name for f in cf.load(repo)] == ["296.feat.md"]


def test_a_dotfile_is_not_a_fragment(repo):
    fragment(repo, ".gitkeep", "")
    fragment(repo, "296.feat.md", BODY)
    assert [f.path.name for f in cf.load(repo)] == ["296.feat.md"]


def test_fragments_assemble_in_a_deterministic_order(repo):
    """Kind first, then issue number, so two machines assembling the same tree produce the
    same entry and a re-run is a no-op rather than a reshuffle."""
    for name in ("300.fix.md", "12.fix.md", "299.feat.md", "5.docs.md"):
        fragment(repo, name, BODY)
    assert [f.path.name for f in cf.load(repo)] == [
        "299.feat.md", "12.fix.md", "300.fix.md", "5.docs.md"]


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["296.md", "feat.md", "296-feat.md", "296.feat.txt",
                                  "notes.md", "296.feat.md.bak"])
def test_a_filename_that_is_not_issue_dot_kind_is_refused(repo, name):
    fragment(repo, name, BODY)
    with pytest.raises(cf.FragmentError) as e:
        cf.load(repo)
    assert "<issue>.<kind>.md" in str(e.value)


def test_an_unknown_kind_names_the_ones_that_exist(repo):
    fragment(repo, "296.improvement.md", BODY)
    with pytest.raises(cf.FragmentError) as e:
        cf.load(repo)
    assert "improvement" in str(e.value) and "feat" in str(e.value)


def test_a_fragment_with_no_body_is_refused(repo):
    fragment(repo, "296.feat.md", "# a title and nothing else\n")
    with pytest.raises(cf.FragmentError) as e:
        cf.load(repo)
    assert "no body" in str(e.value)


@pytest.mark.parametrize("heading", ["# a second document", "## v2.67 — a release"])
def test_a_top_level_heading_in_the_body_is_refused(repo, heading):
    """Folded into CHANGELOG.md a `##` opens a RELEASE, so the entry would split in two and
    `release_stamp.py` would number the second half as its own."""
    fragment(repo, "296.feat.md", f"# the title\n\nProse.\n\n{heading}\n\nMore prose.\n")
    with pytest.raises(cf.FragmentError) as e:
        cf.load(repo)
    assert "opens a RELEASE" in str(e.value)


def test_a_sub_heading_in_the_body_is_allowed(repo):
    """`###` and below is how the CHANGELOG already sections a long entry."""
    fragment(repo, "296.feat.md", "# the title\n\nProse.\n\n### a section\n\nMore prose.\n")
    assert cf.load(repo)[0].body.endswith("More prose.")


def test_a_fragment_naming_a_version_is_refused(repo):
    """A fragment names no version at all. That is the whole reason two branches can each
    write one without racing for a number, and a fragment carrying `vNEXT` would be a
    placeholder site in a file `release_stamp.py apply` also rewrites."""
    fragment(repo, "296.feat.md", "# the title\n\nShips in **vNEXT**.\n")
    with pytest.raises(cf.FragmentError) as e:
        cf.load(repo)
    assert "names no version" in str(e.value)


def test_the_placeholder_inside_a_code_span_is_documentation_and_is_allowed(repo):
    """Entries in this repo discuss the placeholder at length. The stamper draws the same
    line — a token inside a code span is not a site it rewrites — so a fragment explaining
    the convention must not be refused for explaining it."""
    fragment(repo, "296.feat.md",
             "# the title\n\nThe entry `release_stamp.py` stamps is `## vNEXT — <title>`.\n")
    assert cf.load(repo)[0].title == "the title"


def test_a_release_heading_inside_a_fence_is_an_example_and_is_allowed(repo):
    fragment(repo, "296.feat.md",
             "# the title\n\nBefore this, a branch wrote:\n\n```md\n## v2.67 — a title\n```\n")
    assert cf.load(repo)[0].title == "the title"


def test_several_fragments_with_no_title_given_refuse_to_name_the_release(repo):
    """The heading is the line a reader scans, and concatenating three fragment titles would
    produce one nobody wrote for a release nobody named."""
    fragment(repo, "296.feat.md", BODY)
    fragment(repo, "298.fix.md", BODY)
    with pytest.raises(cf.FragmentError) as e:
        cf.release_title(cf.load(repo), None)
    assert "--title" in str(e.value)


def test_one_untitled_fragment_with_no_title_given_is_refused(repo):
    fragment(repo, "296.feat.md", "Prose with no title line.\n")
    with pytest.raises(cf.FragmentError) as e:
        cf.release_title(cf.load(repo), None)
    assert "no heading" in str(e.value)


# ---------------------------------------------------------------------------
# assembly, which only the release job calls
#
# `assemble` used to be a subcommand and every brief in the repo told a worker to run it —
# which is how a branch came to carry a release entry, and then a number, and then a conflict
# with every sibling doing the same (#122). The functions stayed; the door out of them is now
# `release.py run`, on `main`, after the merge. So these call them directly.
# ---------------------------------------------------------------------------


def assemble(repo: Path, title: str | None = None, version: str = "v2.2") -> None:
    """What `release.py run` does with these four functions, in the order it does it."""
    fragments = cf.load(repo)
    heading = cf.release_title(fragments, title)
    changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    readme = (repo / "README.md").read_text(encoding="utf-8")
    changelog = cf.insert_entry(changelog, cf.entry(fragments, heading, version))
    (repo / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    (repo / "README.md").write_text(
        cf.insert_bullet(readme, changelog, heading, version), encoding="utf-8")
    for f in fragments:
        f.path.unlink()


def test_one_fragment_becomes_the_entry_and_lends_it_its_title(repo):
    fragment(repo, "296.feat.md", BODY)
    assemble(repo)
    changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert changelog.startswith(
        "# Version history\n\nEntries are newest first.\n\n"
        "## v2.2 — a branch stops guessing\n\n"
        "What was broken before this: the thing.\n\n"
        "## v2.1 — dev context\n")


def test_several_fragments_become_sub_sections_of_one_entry(repo):
    fragment(repo, "296.feat.md", "# the renderer\n\nWhy the renderer.\n")
    fragment(repo, "298.fix.md", "# the seats\n\nWhy the seats.\n")
    assemble(repo, "two things")
    changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## v2.2 — two things\n\n### the renderer\n\nWhy the renderer.\n" in changelog
    assert "### the seats\n\nWhy the seats.\n" in changelog


def test_the_bullet_lands_where_the_renderer_puts_it(repo):
    """The README bullet and the CHANGELOG entry are written in one pass, so they cannot
    disagree about whether this release exists."""
    fragment(repo, "296.feat.md", BODY)
    assemble(repo)
    readme = (repo / "README.md").read_text(encoding="utf-8")
    assert ("- **v2.1** — dev context.\n"
            "- **v2.2** — a branch stops guessing.\n"
            "- **Not yet numbered** — a roadmap item.\n") in readme


def test_assembly_consumes_the_fragments(repo):
    fragment(repo, "296.feat.md", BODY)
    assemble(repo)
    assert list((repo / cf.FRAGMENT_DIR).iterdir()) == []


def test_a_changelog_with_no_entries_is_refused(tmp_path):
    """There is no list to put a release at the top of, and inventing the first entry from a
    fragment would be this tool guessing what the file is."""
    (tmp_path / "CHANGELOG.md").write_text("# Version history\n\nNothing yet.\n",
                                           encoding="utf-8")
    (tmp_path / "README.md").write_text(README, encoding="utf-8")
    (tmp_path / cf.FRAGMENT_DIR).mkdir()
    fragment(tmp_path, "296.feat.md", BODY)
    with pytest.raises(cf.FragmentError) as e:
        assemble(tmp_path)
    assert "no `## ` release entry" in str(e.value)


def test_there_is_no_assemble_subcommand_left(repo, capsys):
    """A stale brief gets `invalid choice`, which is the loudest thing a removal can say."""
    with pytest.raises(SystemExit):
        cf.main(["assemble", "--repo", str(repo)])
    assert "invalid choice" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# this checkout
# ---------------------------------------------------------------------------

def test_this_repos_own_fragments_parse():
    """`changelog.d/` here, because a fragment nobody can parse is a landing that fails at
    its last step — after the review, after CI, at the moment somebody is merging."""
    cf.load(REPO_ROOT)


# ---------------------------------------------------------------------------
# `required` — did a branch that changes something write an entry at all
# ---------------------------------------------------------------------------
#
# The other direction from everything above it. Those tests ask whether a fragment that
# EXISTS is well formed; these ask whether one should have existed. PR #363 landed a new
# module, sixty-seven tests and two public helpers with `changelog.d/` holding nothing but
# its README, and app, harness, mcp, flake, `frozen` and `migration-heads` were all green —
# an absent entry and a correct one are the same shape to every check that reads the file
# (#365).
#
# These build real git repos rather than trees of files, because the subject is git
# behaviour: two refs, a fork point, and a trailer read by git's own parser.

BASE_CHANGELOG = """# Version history

Entries are newest first.

## v2.1 — dev context

The body of v2.1.

## v2 — presence

The body of v2.
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


def write(repo: Path, path: str, text: str) -> None:
    full = repo / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(text, encoding="utf-8")


def commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture(autouse=True)
def hermetic_git(monkeypatch, tmp_path: Path) -> None:
    """No developer's global git config reaches these repos.

    `commit.gpgSign=true` fails every `commit()` in this file with a signing error and
    `core.excludesFile` can hide a path the fixture just wrote. `GIT_CONFIG_*` is inherited
    by the `git` subprocesses the tool itself runs, so both ends see the same empty config.
    """
    empty = tmp_path / "gitconfig-none"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)


@pytest.fixture
def branched(tmp_path: Path) -> Path:
    """A repo at v2.1 on `main`, with `work` forked from it and checked out.

    `main` is a local branch rather than a remote-tracking ref, for `test_release_stamp.py`'s
    reason: `--onto` takes any ref, and a local one saves the fixture a second repo to clone
    from. `changelog.d/` holds its README and nothing else — #363's exact state.
    """
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    write(root, "CHANGELOG.md", BASE_CHANGELOG)
    write(root, "README.md", "# board\n\n- **v2.1** — dev context.\n")
    write(root, f"{cf.FRAGMENT_DIR}/README.md", "# how fragments work\n")
    write(root, "app/main.py", "VERSION = '2.1'\n")
    write(root, "harness/README.md", "# harness\n")
    write(root, "tests/test_main.py", "def test_it():\n    pass\n")
    commit(root, "v2.1")
    git(root, "checkout", "-q", "-b", "work")
    return root


def required(repo: Path, *args: str, onto: str = "main", branch: str = "work") -> int:
    return cf.main(["required", "--repo", str(repo), "--onto", onto, "--branch", branch, *args])


# --- the scoping rule, which is the feature -------------------------------------------

def test_a_branch_that_changes_source_and_writes_nothing_is_refused(branched, capsys):
    """#363 at its own scale: a module changes, `changelog.d/` holds only its README."""
    write(branched, "app/main.py", "VERSION = '2.2'\n")
    commit(branched, "feat(app): something")
    assert required(branched) == 2
    err = capsys.readouterr().err
    assert "app/main.py" in err
    assert f"{cf._EXEMPT_KEY}:" in err


def test_a_fragment_is_what_satisfies_it(branched, capsys):
    write(branched, "app/main.py", "VERSION = '2.2'\n")
    write(branched, f"{cf.FRAGMENT_DIR}/12.feat.md", "# a thing\n\nIt was missing.\n")
    commit(branched, "feat(app): something")
    assert required(branched) == 0
    assert f"{cf.FRAGMENT_DIR}/12.feat.md" in capsys.readouterr().out


def test_the_directorys_own_readme_is_not_an_entry(branched):
    """The precise shape of #363: `changelog.d/` is present, non-empty and holds nothing."""
    write(branched, "app/main.py", "VERSION = '2.2'\n")
    write(branched, f"{cf.FRAGMENT_DIR}/README.md", "# how fragments work, revised\n")
    commit(branched, "feat(app): something")
    assert required(branched) == 2


def test_a_docs_only_branch_passes_in_silence(branched, capsys):
    """A README edit at any depth. A check that nags these is a check somebody deletes."""
    write(branched, "README.md", "# board\n\nA better sentence.\n")
    write(branched, "harness/README.md", "# harness\n\nAlso better.\n")
    commit(branched, "docs: say it better")
    assert required(branched) == 0
    assert "nothing that ships" in capsys.readouterr().out


def test_a_test_only_branch_passes_in_silence(branched, capsys):
    """Both shapes: a file in a `tests/` tree, and one named like a test beside the code."""
    write(branched, "tests/test_main.py", "def test_it():\n    assert True\n")
    write(branched, "harness/bin/test_wiring.py", "def test_wired():\n    pass\n")
    commit(branched, "test: cover it")
    assert required(branched) == 0
    assert "nothing that ships" in capsys.readouterr().out


def test_a_workflow_change_is_not_exempt(branched):
    """`ci` is one of KINDS, and every CI-only PR since fragments existed wrote a fragment
    (#306, #355). A `.github/` exemption would have made this very job's own PR silent."""
    write(branched, ".github/workflows/tests.yml", "name: Tests\n")
    commit(branched, "ci: something")
    assert required(branched) == 2


def test_a_skill_brief_is_not_exempt(branched):
    """`harness/commands/*.md` is prose an agent EXECUTES, not documentation about code. A
    blanket `*.md` exemption would have waved through #38, #103, #105, #212, #216 and #364,
    each of which changed how the fleet behaves and touched nothing else."""
    write(branched, "harness/commands/fix-issue.md", "# Fix GitHub Issue\n\nNew step.\n")
    commit(branched, "docs(commands): a new step")
    assert required(branched) == 2


def test_relocating_shipping_source_under_tests_is_still_a_change(branched):
    """Rename detection is on by default and `--name-only` prints the DESTINATION alone, so a
    module relocated into `tests/` would arrive as a single exempt path and the departure of
    the shipping file would be invisible. Both halves of a move are changes."""
    (branched / "tests" / "main_moved.py").write_text(
        (branched / "app" / "main.py").read_text(encoding="utf-8"), encoding="utf-8")
    (branched / "app" / "main.py").unlink()
    commit(branched, "refactor: relocate it out of the way")
    assert required(branched) == 2


#: Pull requests that really happened, with the paths they really changed and the subset of
#: those paths this rule calls shipping. Read out of git rather than imagined, because a
#: scoping rule tested only against invented file lists is a rule tested against its author's
#: idea of the repo. The one judgement call is #345's
#: `harness/templates/test_migrations_self_contained.py`: it is a test file that
#: `create-worktree` installs into new worktrees, so it both ships and is a test, and it is
#: read as a test — which leaves #345, a `test(...)` PR, passing in silence as it should.
REAL_PRS: dict[str, tuple[list[str], list[str]]] = {
    "#363 the incident — a new module and 67 tests, no fragment": (
        [".harness-rules.sample", "harness/loops/README.md", "harness/loops/appetite.py",
         "harness/loops/issue_watch.py", "harness/loops/tests/test_issue_watch.py"],
        [".harness-rules.sample", "harness/loops/appetite.py", "harness/loops/issue_watch.py"],
    ),
    "#345 test(migrations) — a test-only branch": (
        ["README.md", "changelog.d/344.test.md", "harness/README.md",
         "harness/templates/test_migrations_self_contained.py",
         "tests/test_migration_drift.py", "tests/test_migrations_self_contained.py"],
        [],
    ),
    "#361 fix(doctor) — a one-line harness fix with a fragment": (
        ["changelog.d/358.fix.md", "harness/bin/qb-doctor", "harness/tests/test_qb_doctor.py"],
        ["harness/bin/qb-doctor"],
    ),
    "#355 ci(migrations) — a workflow job with a fragment": (
        [".github/workflows/tests.yml", "README.md", "changelog.d/351.ci.md",
         "tests/test_migration_reconcile.py"],
        [".github/workflows/tests.yml"],
    ),
    "#295 docs — one bullet out of the README": (["README.md"], []),
}


@pytest.mark.parametrize("pr", sorted(REAL_PRS), ids=lambda s: s.split()[0])
def test_the_rule_agrees_with_real_pull_requests(pr):
    changed, ships = REAL_PRS[pr]
    assert [p for p in changed if cf._exempt(p) is None] == ships


# --- a branch cannot pass on somebody else's entry --------------------------------------

def test_merging_the_base_in_does_not_count_as_writing_an_entry(branched):
    """#363's actual head. It merged main and inherited three releases' headings, so a
    fork-relative read of the CHANGELOG would have passed the branch this check exists for."""
    write(branched, "app/main.py", "VERSION = '2.2'\n")
    commit(branched, "feat(app): something")
    git(branched, "checkout", "-q", "main")
    write(branched, "CHANGELOG.md", BASE_CHANGELOG.replace(
        "## v2.1", "## v2.2 — somebody else's release\n\nTheir body.\n\n## v2.1", 1))
    commit(branched, "chore(release): v2.2")
    git(branched, "checkout", "-q", "work")
    git(branched, "merge", "-q", "--no-edit", "main")
    assert required(branched) == 2


def test_a_fragment_the_base_already_carries_is_not_this_branchs_entry(branched):
    """An unassembled fragment on the base is swept into the next release and is on every
    branch forked after it. Crediting it here would exempt every branch until it lands."""
    git(branched, "checkout", "-q", "main")
    write(branched, f"{cf.FRAGMENT_DIR}/9.fix.md", "# theirs\n\nSomebody else's.\n")
    commit(branched, "fix: a sibling's fragment")
    git(branched, "checkout", "-q", "work")
    git(branched, "merge", "-q", "--no-edit", "main")
    write(branched, "app/main.py", "VERSION = '2.2'\n")
    commit(branched, "feat(app): something")
    assert required(branched) == 2


def test_a_hand_written_release_entry_does_not_satisfy_it(branched, capsys):
    """It used to, and it must not now (#122). `release.py guard` refuses a branch that edits
    `CHANGELOG.md` at all, so an entry written there is not a second way to pass this check —
    it is a separate refusal with its own remedy. Crediting it here would let a branch satisfy
    the note requirement by doing the one thing the guard exists to stop, and the two gates
    would then disagree about the same commit."""
    write(branched, "app/main.py", "VERSION = '2.2'\n")
    write(branched, "CHANGELOG.md", BASE_CHANGELOG.replace(
        "## v2.1", "## v2.2 — a thing\n\nIt was missing.\n\n## v2.1", 1))
    commit(branched, "feat(app): something")
    assert required(branched) == 2
    assert f"{cf.FRAGMENT_DIR}/<issue>.<kind>.md" in capsys.readouterr().err


def test_an_unrelated_changelog_edit_is_no_substitute_for_a_fragment(branched):
    """A branch that touched `CHANGELOG.md` for some other reason — a typo in the preamble, a
    `Release-Body-Edit` correction — has still written no release note, and the file it edited
    is one `guard` will refuse it for separately."""
    write(branched, "app/main.py", "VERSION = '2.2'\n")
    write(branched, "CHANGELOG.md",
          BASE_CHANGELOG.replace("Entries are newest first.",
                                 "Entries are newest first (oldest last)."))
    commit(branched, "feat(app): something, and a preamble typo")
    assert required(branched) == 2


# --- the opt-out ------------------------------------------------------------------------

def test_a_trailer_waives_it_and_says_so_out_loud(branched, capsys):
    write(branched, "app/main.py", "VERSION = '2.2'  # a typo in a comment\n")
    git(branched, "add", "-A")
    git(branched, "commit", "-q", "-m",
        "chore(app): fix a comment typo\n\nNothing a reader of the notes would want.\n\n"
        f"{cf._EXEMPT_KEY}: a comment typo, no behaviour changed\n")
    assert required(branched) == 0
    out = capsys.readouterr()
    assert "waived: a comment typo, no behaviour changed" in out.err
    assert "ok:" in out.out


def test_a_commit_quoting_the_refusal_does_not_waive_it(branched):
    """#348's lesson, applied before it could be learned twice. The refusal ENDS with a
    ready-to-paste trailer, so a commit body quoting it is the likeliest message this branch
    will ever produce — and a regex over the message would read that paste as consent. A
    trailer block is a message's LAST paragraph; a quotation in the middle of one is not."""
    write(branched, "app/main.py", "VERSION = '2.2'\n")
    git(branched, "add", "-A")
    git(branched, "commit", "-q", "-m",
        "feat(app): something\n\nCI refused this and said:\n\n"
        "    Genuinely nothing for a reader of the release notes? Say so on a commit:\n"
        f"        {cf._EXEMPT_KEY}: <one line saying why>\n\n"
        "which is fair, so a fragment is coming in the next commit.\n")
    assert required(branched) == 2


def test_a_trailer_still_wearing_its_angle_brackets_does_not_waive_it(branched):
    """The other half of the same paste, in a real trailer position this time. git's parser
    is perfectly happy with `Changelog-Exempt: <one line saying why>`, and it says nothing —
    the one shape of waiver indistinguishable from nobody having thought about it."""
    write(branched, "app/main.py", "VERSION = '2.2'\n")
    git(branched, "add", "-A")
    git(branched, "commit", "-q", "-m",
        f"feat(app): something\n\n{cf._EXEMPT_KEY}: <one line saying why>\n")
    assert required(branched) == 2


def test_a_waiver_on_the_base_does_not_carry_over_to_the_next_branch(branched):
    """Read from the range, so it expires with the merge it was written for. A waiver that
    outlived its own branch would exempt everything forked after it, permanently."""
    git(branched, "checkout", "-q", "main")
    write(branched, "app/other.py", "OTHER = 1\n")
    git(branched, "add", "-A")
    git(branched, "commit", "-q", "-m",
        f"chore: a waived change\n\n{cf._EXEMPT_KEY}: renamed a local variable\n")
    git(branched, "checkout", "-q", "work")
    git(branched, "merge", "-q", "--no-edit", "main")
    write(branched, "app/main.py", "VERSION = '2.2'\n")
    commit(branched, "feat(app): something")
    assert required(branched) == 2


# --- it must not go quietly green -------------------------------------------------------

def test_no_fork_point_is_refused_rather_than_passed(branched, capsys):
    """The `fetch-depth: 0` failure, which is the one that matters: at depth 1 there is no
    fork point, and a check that answers "nothing changed" there reports green while
    verifying nothing. It says which, and it says so with a non-zero exit."""
    git(branched, "checkout", "-q", "--orphan", "elsewhere")
    git(branched, "rm", "-rq", "--cached", ".")
    write(branched, "app/main.py", "VERSION = '9'\n")
    commit(branched, "an unrelated root")
    assert required(branched, branch="elsewhere") == 2
    assert "fetch-depth: 0" in capsys.readouterr().err


def test_a_ref_that_is_not_here_is_refused(branched, capsys):
    assert required(branched, onto="origin/nope") == 2
    assert "does not exist here" in capsys.readouterr().err


def test_the_verdict_is_available_as_json(branched, capsys):
    write(branched, "app/main.py", "VERSION = '2.2'\n")
    commit(branched, "feat(app): something")
    assert required(branched, "--json") == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["ships"] == ["app/main.py"]
    assert payload["fragments"] == []
    assert cf._EXEMPT_KEY in payload["refusal"]


def test_an_empty_pull_request_changes_nothing_and_passes(branched):
    """`work` is `main`. Not a special case in the code and it should not become one — no
    path changed, so nothing is owed."""
    assert required(branched) == 0


# ---------------------------------------------------------------------------
# the CI job that runs it
# ---------------------------------------------------------------------------
#
# Found by what it RUNS rather than by its name, for `test_release_stamp.py`'s reason: a
# renamed job would otherwise skip these silently. The checkout depth is the one to keep —
# at depth 1 there is no fork point, and the whole job would be an unrunnable gate reporting
# as a passing one.

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"


def _changelog_job() -> dict:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    running = [job for job in jobs.values()
               if any("changelog_fragments.py required" in str(step.get("run", ""))
                      for step in job.get("steps", []))]
    assert len(running) == 1, (
        f"{len(running)} jobs in tests.yml run `changelog_fragments.py required`; the guard "
        "against a branch that writes no entry is one job, and these tests assert about it")
    return running[0]


def test_the_changelog_job_runs_on_pull_request():
    """Where it can be asked at all. After the merge the branch's fork point is itself, the
    diff is empty, and the answer would be a vacuous pass on every push to main."""
    assert "pull_request" in _changelog_job()["if"]


def test_the_changelog_job_checks_out_the_whole_history():
    """`fetch-depth: 0`, as on `frozen` and `migration-heads`. What changed is measured from
    the fork point and a depth-1 checkout has none."""
    checkouts = [s for s in _changelog_job()["steps"] if "actions/checkout" in str(s.get("uses"))]
    assert checkouts, "the job has no checkout step"
    assert all(s.get("with", {}).get("fetch-depth") == 0 for s in checkouts)


def test_the_changelog_job_also_parses_the_fragments():
    """Nothing in CI ran `check` before this job existed: a fragment with a `##` heading in
    its body was caught by whoever ran `assemble` at land time, which is the last moment
    anybody wants to find out."""
    steps = "\n".join(str(s.get("run", "")) for s in _changelog_job()["steps"])
    assert "changelog_fragments.py check" in steps


def test_the_base_branch_reaches_the_script_as_data():
    """A branch name is written by whoever opened the pull request, and `${{ }}` substitution
    happens before bash sees the line — so a base ref spliced into a `run:` body is that
    person's text executing on the runner. Through `env:` it is data."""
    for step in _changelog_job()["steps"]:
        assert "github.base_ref" not in str(step.get("run", ""))
    envs = "".join(str(s.get("env", "")) for s in _changelog_job()["steps"])
    assert "github.base_ref" in envs
