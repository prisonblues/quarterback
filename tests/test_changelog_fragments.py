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
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

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
# assembly
# ---------------------------------------------------------------------------

def test_one_fragment_becomes_the_entry_and_lends_it_its_title(repo):
    fragment(repo, "296.feat.md", BODY)
    assert cf.main(["assemble", "--repo", str(repo)]) == 0
    changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert changelog.startswith(
        "# Version history\n\nEntries are newest first.\n\n"
        "## vNEXT — a branch stops guessing\n\n"
        "What was broken before this: the thing.\n\n"
        "## v2.1 — dev context\n")


def test_several_fragments_become_sub_sections_of_one_entry(repo):
    fragment(repo, "296.feat.md", "# the renderer\n\nWhy the renderer.\n")
    fragment(repo, "298.fix.md", "# the seats\n\nWhy the seats.\n")
    assert cf.main(["assemble", "--repo", str(repo), "--title", "two things"]) == 0
    changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## vNEXT — two things\n\n### the renderer\n\nWhy the renderer.\n" in changelog
    assert "### the seats\n\nWhy the seats.\n" in changelog


def test_the_bullet_lands_where_the_renderer_puts_it(repo):
    """The README bullet and the CHANGELOG entry are written by one command, so they cannot
    disagree about whether this release exists — which is what
    `test_the_changelog_and_the_readme_are_unstamped_together` asserts after the fact."""
    fragment(repo, "296.feat.md", BODY)
    cf.main(["assemble", "--repo", str(repo)])
    readme = (repo / "README.md").read_text(encoding="utf-8")
    assert ("- **v2.1** — dev context.\n"
            "- **vNEXT** — a branch stops guessing.\n"
            "- **Not yet numbered** — a roadmap item.\n") in readme


def test_assembly_consumes_the_fragments(repo):
    fragment(repo, "296.feat.md", BODY)
    cf.main(["assemble", "--repo", str(repo)])
    assert list((repo / cf.FRAGMENT_DIR).iterdir()) == []


def test_keep_leaves_them_alone(repo):
    fragment(repo, "296.feat.md", BODY)
    cf.main(["assemble", "--repo", str(repo), "--keep"])
    assert [p.name for p in (repo / cf.FRAGMENT_DIR).iterdir()] == ["296.feat.md"]


def test_a_dry_run_writes_nothing(repo):
    fragment(repo, "296.feat.md", BODY)
    assert cf.main(["assemble", "--repo", str(repo), "--dry-run"]) == 0
    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == CHANGELOG
    assert (repo / "README.md").read_text(encoding="utf-8") == README
    assert [p.name for p in (repo / cf.FRAGMENT_DIR).iterdir()] == ["296.feat.md"]


def test_assembling_nothing_is_a_noop_and_not_a_refusal(repo):
    """`assemble` is meant to be safe to run unconditionally before landing, beside
    `release_stamp.py apply`, and most branches ship no release."""
    assert cf.main(["assemble", "--repo", str(repo)]) == 0
    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == CHANGELOG


def test_assembling_twice_is_refused_rather_than_writing_a_second_entry(repo, capsys):
    """Two `## vNEXT` headings is a state `release_stamp.py apply` resolves by stamping BOTH
    with the same number — one release documented twice, which is the collision the
    placeholder exists to prevent. Refused here, before the commit, rather than by
    `test_at_most_one_release_is_unstamped` after it.

    The MESSAGE is asserted, not just the exit code. Delete this refusal and the run still
    ends at 2 with one heading in the file, because the README renderer then refuses the
    second `vNEXT` bullet as a release covered twice — a correct outcome reached from the
    wrong file, with advice about the README for a defect in the CHANGELOG."""
    fragment(repo, "296.feat.md", BODY)
    cf.main(["assemble", "--repo", str(repo)])
    capsys.readouterr()
    fragment(repo, "298.fix.md", BODY)
    assert cf.main(["assemble", "--repo", str(repo)]) == 2
    assert "CHANGELOG.md already carries an unstamped" in capsys.readouterr().err
    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8").count("## vNEXT") == 1


def test_a_changelog_with_no_entries_is_refused(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text("# Version history\n\nNothing yet.\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(README, encoding="utf-8")
    (tmp_path / cf.FRAGMENT_DIR).mkdir()
    fragment(tmp_path, "296.feat.md", BODY)
    assert cf.main(["assemble", "--repo", str(tmp_path)]) == 2


# ---------------------------------------------------------------------------
# this checkout
# ---------------------------------------------------------------------------

def test_this_repos_own_fragments_parse():
    """`changelog.d/` here, because a fragment nobody can parse is a landing that fails at
    its last step — after the review, after CI, at the moment somebody is merging."""
    cf.load(REPO_ROOT)
