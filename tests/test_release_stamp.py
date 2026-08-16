"""Tests for `scripts/release_stamp.py`.

Every test builds a throwaway git repo: the tool's whole subject is "what number is free at
the ref I am merging into", and a question about a ref cannot be answered by a fixture string.
No database, and nothing here reads the real repo — a suite that asserted about this checkout's
own CHANGELOG would go red on the day somebody landed a release, which is the day it is needed.

The tests this file exists for are the three where the tool could be plausibly wrong and
silently so:

  * `test_the_number_comes_from_the_base_ref_not_the_branch` — a branch forked at v2.28 and
    landing when main is at v2.33 must get v2.34, not v2.29. Reading its own file is the exact
    mistake the placeholder exists to make impossible, and it looks correct on every branch
    that happens to be up to date.
  * `test_the_highest_heading_wins_not_the_first` — the file is newest-first and a sibling test
    enforces that, but the tool handing out numbers must not be the one thing trusting the
    ordering it is about to disturb.
  * `test_a_base_branch_change_to_app_does_not_bump_this_branch` — the served-version inference
    diffed against `onto` instead of the merge base would report every path the BASE changed,
    in reverse, as this branch's own work.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

# `scripts/` is a directory of standalone tools, not an importable package, so the module is
# loaded by path — and registered in sys.modules before it executes, because @dataclass
# resolves annotations through sys.modules[cls.__module__].
_SPEC = importlib.util.spec_from_file_location(
    "release_stamp",
    Path(__file__).resolve().parent.parent / "scripts" / "release_stamp.py",
)
rs = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = rs
_SPEC.loader.exec_module(rs)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

CHANGELOG_HEAD = """# Version history

Entries are newest first.

"""

PYPROJECT = """[project]
name = "quarterback"
version = "{version}"  # served version lives in app/main.py
"""

MAIN_PY = '''from fastapi import FastAPI

app = FastAPI(title="quarterback", lifespan=make_lifespan(), version="{version}")
'''


def entry(version: str, body: str = "did a thing.", title: str = "a release") -> str:
    return f"## {version} — {title}\n\n{body}\n\n"


def readme(bullets: list[str], extra: str = "") -> str:
    lines = ["# quarterback", "", "## Releases", ""]
    lines += [f"- **{b}** — a release." for b in bullets]
    return "\n".join(lines) + "\n" + extra


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout


def commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def write(repo: Path, path: str, text: str) -> None:
    full = repo / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(text)


@pytest.fixture(autouse=True)
def hermetic_git(monkeypatch, tmp_path: Path):
    """No developer's global git config reaches these repos.

    The subject under test is git behaviour, which makes the usual "it works on my machine"
    hazard sharper than usual: `commit.gpgSign=true` fails every `commit()` in this file with
    a signing error, a global `core.excludesFile` perturbs the ignored-markdown test by
    ignoring paths the fixture never mentioned, and `core.autocrlf` rewrites the line endings
    the masking logic counts. Pointing both config levels at an empty file removes all of it,
    and `GIT_CONFIG_*` is inherited by the `git` subprocesses the tool itself runs.
    """
    empty = tmp_path / "gitconfig-none"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    return empty


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo whose `main` is at v2.33, with a `work` branch checked out off it.

    `main` is a real branch rather than a remote-tracking ref: `--onto` takes any ref, and a
    local branch keeps the fixture from needing a second repo to clone from. The tests that
    care about the base MOVING push it forward here and then diff against it, which is the
    same shape as fetching an advanced `origin/main`.
    """
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    write(root, "CHANGELOG.md", CHANGELOG_HEAD + entry("v2.33") + entry("v2.32") + entry("v2"))
    write(root, "README.md", readme(["v2.32", "v2.33"]))
    write(root, "pyproject.toml", PYPROJECT.format(version="2.33.0"))
    write(root, "app/main.py", MAIN_PY.format(version="2.33.0"))
    write(root, "harness/loops/README.md", "# loops\n\nA nested doc.\n")
    commit(root, "v2.33")
    git(root, "checkout", "-q", "-b", "work")
    return root


def place(repo: Path, *, changelog: bool = True, bullet: bool = True) -> None:
    """Write an unstamped release entry the way a branch is meant to."""
    if changelog:
        text = (repo / "CHANGELOG.md").read_text()
        at = text.index("## ")  # above every numbered heading, wherever the fixture put them
        write(repo, "CHANGELOG.md", text[:at] + entry("vNEXT") + text[at:])
    if bullet:
        text = (repo / "README.md").read_text()
        write(repo, "README.md", text + "- **vNEXT** — a release.\n")


def run(repo: Path, *argv: str) -> int:
    return rs.main([*argv, "--repo", str(repo)])


def plan_json(repo: Path, *argv: str, capsys) -> dict:
    assert run(repo, *argv, "--json") == 0
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# the number
# ---------------------------------------------------------------------------


def test_a_placeholder_becomes_the_next_number(repo):
    place(repo)
    assert run(repo, "apply", "--onto", "main") == 0
    assert "## v2.34 — a release" in (repo / "CHANGELOG.md").read_text()
    assert "- **v2.34** — a release." in (repo / "README.md").read_text()
    assert "vNEXT" not in (repo / "CHANGELOG.md").read_text()


def test_the_number_comes_from_the_base_ref_not_the_branch(repo):
    """The failure the placeholder exists to prevent, in its last hiding place.

    This branch forked when main was at v2.33 and its own CHANGELOG says so; main has since
    landed v2.34 and v2.35. Reading the branch's file gives v2.34 — already shipped, and the
    collision this whole mechanism is for. Only the base ref can answer."""
    place(repo)
    commit(repo, "work")
    git(repo, "checkout", "-q", "main")
    text = (repo / "CHANGELOG.md").read_text()
    write(repo, "CHANGELOG.md",
          text.replace("## v2.33", entry("v2.35") + entry("v2.34") + "## v2.33", 1))
    commit(repo, "two more releases")
    git(repo, "checkout", "-q", "work")

    assert run(repo, "apply", "--onto", "main") == 0
    assert "## v2.36 — a release" in (repo / "CHANGELOG.md").read_text()


def test_the_highest_heading_wins_not_the_first(repo):
    """A base ref whose newest entry was inserted a line too low still hands out a free
    number. Reading position 0 would re-issue a number that has already shipped — the tool
    that allocates must not be the one that trusts the ordering it is about to disturb."""
    git(repo, "checkout", "-q", "main")
    # Appended, so the highest number is the LAST heading in a file that is meant to be
    # newest-first. Nothing else is moved: the mechanism under test is that the tool takes
    # max() rather than position 0, and rearranging the rest would obscure which it did.
    write(repo, "CHANGELOG.md", (repo / "CHANGELOG.md").read_text() + entry("v2.40"))
    commit(repo, "an entry in the wrong place")
    git(repo, "checkout", "-q", "work")
    git(repo, "merge", "-q", "main")
    place(repo)

    assert run(repo, "apply", "--onto", "main") == 0
    assert "## v2.41 — a release" in (repo / "CHANGELOG.md").read_text()


def test_a_major_only_release_still_yields_a_minor(repo):
    """`## v2` parses as (2, 0), so the next one is v2.1 and not a crash. The repo's two
    oldest entries are spelled that way and the next major will be."""
    write(repo, "CHANGELOG.md", CHANGELOG_HEAD + entry("v2"))
    write(repo, "README.md", readme(["v2"]))
    commit(repo, "only v2")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "work")
    git(repo, "checkout", "-q", "work")
    place(repo)

    assert run(repo, "apply", "--onto", "main") == 0
    assert "## v2.1 — a release" in (repo / "CHANGELOG.md").read_text()


def test_nothing_to_stamp_is_a_noop_not_a_failure(repo, capsys):
    assert run(repo, "apply", "--onto", "main") == 0
    assert "noop" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# where a placeholder counts
# ---------------------------------------------------------------------------


def test_every_tracked_markdown_file_is_stamped(repo):
    """Including the nested one. `harness/loops/README.md` was the third file in every
    conflict and the one most often missed, because it is nobody's release checklist."""
    place(repo)
    write(repo, "harness/loops/README.md", "# loops\n\n**vNEXT** — the loops half.\n")
    assert run(repo, "apply", "--onto", "main") == 0
    assert "**v2.34** — the loops half." in (repo / "harness/loops/README.md").read_text()


def test_a_placeholder_in_a_code_span_is_documentation(repo):
    """This repo's README explains the convention, so it writes the token a dozen times.
    Stamping those would rewrite the instructions into a description of one release."""
    place(repo)
    write(repo, "docs.md", "# how\n\nWrite `## vNEXT — <title>` and `- **vNEXT** — …`.\n")
    assert run(repo, "apply", "--onto", "main") == 0
    assert "`## vNEXT — <title>`" in (repo / "docs.md").read_text()


def test_a_placeholder_in_a_fenced_block_is_documentation(repo):
    place(repo)
    write(repo, "docs.md", "# how\n\n```md\n## vNEXT — <title>\n```\n")
    assert run(repo, "apply", "--onto", "main") == 0
    assert "## vNEXT — <title>" in (repo / "docs.md").read_text()


def test_a_placeholder_nothing_would_rewrite_is_a_stop(repo, capsys):
    """Bare in running prose is neither a heading nor a bold run, so `apply` would walk past
    it and the literal string would ship. Refusing is the only honest answer: the alternative
    is a release document containing the word vNEXT and a tool that reported success."""
    place(repo)
    write(repo, "docs.md", "# how\n\nThis release is vNEXT and ships tomorrow.\n")
    commit(repo, "a doc")
    assert run(repo, "apply", "--onto", "main") == 2
    assert "will not be rewritten" in capsys.readouterr().err


def test_untracked_markdown_is_not_stamped_but_is_reported(repo, capsys):
    """`plan.md` is untracked on purpose in this repo, is where the agents argue about
    releases in prose, and ships with nothing — so stamping it would be writing into a
    scratchpad and calling it part of the release. But a genuinely new doc sits untracked for
    the minutes between being written and being added, so silence is how the literal string
    reaches a reader. Skipped and named, not skipped and quiet."""
    place(repo)
    (repo / "notes.md").write_text("# notes\n\n## vNEXT — a scratch entry\n")
    assert run(repo, "apply", "--onto", "main") == 0
    assert "## vNEXT" in (repo / "notes.md").read_text()
    assert "notes.md:3" in capsys.readouterr().err


def test_an_ignored_markdown_file_is_not_even_reported(repo, capsys):
    """The warning above has to stay worth reading. A path git is ignoring was decided
    against deliberately, so mentioning it on every run is how the warning gets skimmed."""
    place(repo)
    write(repo, ".gitignore", "ignored.md\n")
    (repo / "ignored.md").write_text("## vNEXT — not part of anything\n")
    assert run(repo, "apply", "--onto", "main") == 0
    assert "ignored.md" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


def test_two_unstamped_entries_are_a_stop(repo, capsys):
    place(repo)
    place(repo, bullet=False)
    assert run(repo, "preflight", "--onto", "main") == 2
    assert "2 `## vNEXT` headings" in capsys.readouterr().err


def test_an_entry_below_a_released_one_is_a_stop(repo, capsys):
    text = (repo / "CHANGELOG.md").read_text()
    write(repo, "CHANGELOG.md", text.replace("## v2.32", entry("vNEXT") + "## v2.32", 1))
    place(repo, changelog=False)
    assert run(repo, "preflight", "--onto", "main") == 2
    assert "below `v2.33`" in capsys.readouterr().err


def test_a_bullet_without_an_entry_is_a_stop(repo, capsys):
    place(repo, changelog=False)
    assert run(repo, "preflight", "--onto", "main") == 2
    assert "no `## vNEXT` heading" in capsys.readouterr().err


def test_an_entry_without_a_bullet_is_not(repo):
    """The asymmetry is deliberate. A CHANGELOG entry with no README bullet is a branch
    mid-write and this tool stamps what is there; the missing bullet is caught by
    `harness/tests/test_release_numbers.py`, which asserts about the repo rather than about
    the operation. Refusing here would make the stamper a linter for a file it is not
    editing, and stop a release over a line it could not add anyway."""
    place(repo, bullet=False)
    assert run(repo, "apply", "--onto", "main") == 0
    assert "## v2.34 — a release" in (repo / "CHANGELOG.md").read_text()


def test_an_unstamped_base_ref_is_a_stop(repo, capsys):
    """main landed without being stamped. Numbering on top of it hands this branch the
    number the unstamped entry is going to want, so the two collide one merge later —
    the failure this tool exists to remove, reintroduced by the tool itself."""
    git(repo, "checkout", "-q", "main")
    place(repo)
    commit(repo, "unstamped on main")
    git(repo, "checkout", "-q", "work")
    place(repo)
    assert run(repo, "preflight", "--onto", "main") == 2
    assert "itself carries an unstamped" in capsys.readouterr().err


def test_a_number_already_in_the_branch_is_a_stop(repo, capsys):
    """The branch wrote v2.34 by hand before the placeholder existed, and the next free
    number at the base is also v2.34. Stamping it would put the number in twice."""
    place(repo)
    text = (repo / "CHANGELOG.md").read_text()
    write(repo, "CHANGELOG.md", text.replace("## v2.33", entry("v2.34") + "## v2.33", 1))
    assert run(repo, "preflight", "--onto", "main") == 2
    assert "already has an entry for v2.34" in capsys.readouterr().err


def test_a_hand_written_future_number_is_a_stop_even_when_it_is_not_the_next_one(repo, capsys):
    """`## v2.40` on a branch whose base is at v2.33 is not a collision today, and is one the
    week v2.40 comes round. More to the point it is a branch naming its own release, which is
    the practice this whole file exists to end — so the refusal is on any number ABOVE the
    base's newest, not only on the one that happens to equal what is about to be handed out.
    Stamped as it stands, v2.34 would land above or below a v2.40 nobody has shipped."""
    place(repo)
    text = (repo / "CHANGELOG.md").read_text()
    write(repo, "CHANGELOG.md", text.replace("## v2.33", entry("v2.40") + "## v2.33", 1))
    assert run(repo, "preflight", "--onto", "main") == 2
    err = capsys.readouterr().err
    assert "already has an entry for v2.40" in err and "does not exist at main" in err


def test_a_missing_ref_is_a_stop_not_a_traceback(repo, capsys):
    place(repo)
    assert run(repo, "preflight", "--onto", "origin/nope") == 2
    assert "does not exist here" in capsys.readouterr().err


def test_a_base_changelog_with_no_headings_is_a_stop_not_a_traceback(repo, capsys):
    """Exit 2 with a sentence, never Python's uncaught-exception 1 — a caller reading the
    documented 0/2 scheme takes 1 as "unknown", which is the one answer this tool promises
    not to give. `max(())` on an empty heading list was the way in."""
    git(repo, "checkout", "-q", "main")
    write(repo, "CHANGELOG.md", "# Version history\n\nNothing here yet.\n")
    commit(repo, "an empty changelog")
    git(repo, "checkout", "-q", "work")
    place(repo)
    assert run(repo, "preflight", "--onto", "main") == 2
    assert "no `## vX.Y` headings at the base ref" in capsys.readouterr().err


def test_a_symlinked_markdown_file_is_not_written_through(repo, capsys, tmp_path):
    """Git stores a symlink as its target path, so `write_text` through one lands wherever
    it points — outside the repo, if that is where it points. A release stamp is not
    something to apply to a file this repo does not own; skipped, and said out loud."""
    outside = tmp_path / "outside.md"
    outside.write_text("## vNEXT — somebody else's document\n")
    (repo / "linked.md").symlink_to(outside)
    place(repo)
    commit(repo, "a tracked symlink")

    assert run(repo, "apply", "--onto", "main") == 0
    assert outside.read_text() == "## vNEXT — somebody else's document\n"
    assert "linked.md" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# the served version
# ---------------------------------------------------------------------------


def test_a_board_change_bumps_the_served_version(repo):
    place(repo)
    write(repo, "app/routes.py", "# a board change\n")
    assert run(repo, "apply", "--onto", "main") == 0
    assert 'version = "2.34.0"' in (repo / "pyproject.toml").read_text()
    assert 'version="2.34.0"' in (repo / "app/main.py").read_text()


def test_a_migration_bumps_the_served_version(repo):
    place(repo)
    write(repo, "migrations/versions/0020_thing.py", "revision = '0020'\n")
    assert run(repo, "apply", "--onto", "main") == 0
    assert 'version = "2.34.0"' in (repo / "pyproject.toml").read_text()


def test_a_harness_only_release_leaves_the_served_version_alone(repo):
    """Most releases here are harness-side and correctly leave it where it was — v2.16,
    v2.17, v2.18, v2.20, v2.21 and v2.32 all did. A check that bumped anyway would be wrong
    every second release and switched off within a week."""
    place(repo)
    write(repo, "harness/loops/panel.py", "# a harness change\n")
    assert run(repo, "apply", "--onto", "main") == 0
    assert 'version = "2.33.0"' in (repo / "pyproject.toml").read_text()
    assert 'version="2.33.0"' in (repo / "app/main.py").read_text()


def test_a_base_branch_change_to_app_does_not_bump_this_branch(repo):
    """Diffed against `onto` rather than the merge base, `git diff` describes the round trip:
    every path the BASE changed since the fork comes back, in reverse, as this branch's work.
    A docs-only branch would then be told it changed `app/` because somebody else's release
    did, and would ship a served-version bump nobody wrote."""
    place(repo)
    commit(repo, "work")
    git(repo, "checkout", "-q", "main")
    write(repo, "app/routes.py", "# someone else's board change\n")
    commit(repo, "a board release on main")
    git(repo, "checkout", "-q", "work")

    assert run(repo, "apply", "--onto", "main") == 0
    assert 'version = "2.33.0"' in (repo / "pyproject.toml").read_text()


def test_an_uncommitted_board_change_still_counts(repo):
    """`apply` runs on a finished branch before the last commit — the entry it is stamping is
    usually still unstaged. An inference reading only committed work would miss the release
    it is being run for."""
    place(repo)
    write(repo, "app/routes.py", "# not committed yet\n")
    assert run(repo, "apply", "--onto", "main") == 0
    assert 'version = "2.34.0"' in (repo / "pyproject.toml").read_text()


def test_no_serve_overrides_the_inference(repo):
    place(repo)
    write(repo, "app/routes.py", "# a board change that ships no behaviour\n")
    assert run(repo, "apply", "--onto", "main", "--no-serve") == 0
    assert 'version = "2.33.0"' in (repo / "pyproject.toml").read_text()


def test_serve_overrides_the_inference(repo):
    place(repo)
    assert run(repo, "apply", "--onto", "main", "--serve") == 0
    assert 'version="2.34.0"' in (repo / "app/main.py").read_text()


def test_a_version_that_left_the_fastapi_call_is_a_stop(repo, capsys):
    """The regex is coupled to an inline literal and says so. Failing loudly is the honest
    outcome; the alternative is latching onto the next version-shaped string in the file and
    bumping something that is not what the app serves."""
    place(repo)
    write(repo, "app/main.py", "app = FastAPI(title='quarterback', version=VERSION)\n")
    write(repo, "app/routes.py", "# a board change\n")
    assert run(repo, "apply", "--onto", "main") == 2
    assert "no `app = FastAPI(" in capsys.readouterr().err


def test_an_unbumpable_version_stops_before_anything_is_written(repo):
    """A half-applied release is worse than either outcome, and is the state hardest to
    notice: the markdown reads as a finished release and the served version disagrees with
    it. So both version sites are validated before the first byte is written."""
    place(repo)
    write(repo, "app/main.py", "app = FastAPI(title='quarterback', version=VERSION)\n")
    write(repo, "app/routes.py", "# a board change\n")
    assert run(repo, "apply", "--onto", "main") == 2
    assert "## vNEXT — a release" in (repo / "CHANGELOG.md").read_text()
    assert "- **vNEXT** — a release." in (repo / "README.md").read_text()


def test_a_version_in_another_toml_table_is_not_the_package_version(repo):
    """`[tool.something].version` is somebody else's field. A file-wide search for a
    `version = "X.Y.Z"` line finds whichever table happens to have one — and the day
    `[project]` stops having one, that search does not report the absence, it reports the
    other table and bumps it, successfully and wrongly."""
    place(repo)
    write(repo, "pyproject.toml",
          PYPROJECT.format(version="2.33.0") + '\n[tool.other]\nversion = "9.9.9"\n')
    write(repo, "app/routes.py", "# a board change\n")
    assert run(repo, "apply", "--onto", "main") == 0
    text = (repo / "pyproject.toml").read_text()
    assert 'version = "2.34.0"' in text
    assert 'version = "9.9.9"' in text  # untouched


def test_two_version_lines_in_project_stop_before_anything_is_written(repo, capsys):
    place(repo)
    write(repo, "pyproject.toml",
          PYPROJECT.format(version="2.33.0") + 'version = "9.9.9"\n')
    write(repo, "app/routes.py", "# a board change\n")
    assert run(repo, "apply", "--onto", "main") == 2
    assert "expected exactly 1" in capsys.readouterr().err
    assert "## vNEXT — a release" in (repo / "CHANGELOG.md").read_text()


def test_a_pyproject_with_no_project_table_is_a_stop(repo, capsys):
    """Said as "no `[project]` table" rather than as "0 version lines". The tool's whole
    ergonomic argument is that a refusal carries the sentence that repairs it, and "0 lines,
    expected exactly 1" sends a reader looking for a line that was never the problem."""
    place(repo)
    write(repo, "pyproject.toml", '[tool.ruff]\nversion = "9.9.9"\n')
    write(repo, "app/routes.py", "# a board change\n")
    assert run(repo, "apply", "--onto", "main") == 2
    assert "no `[project]` table" in capsys.readouterr().err


def test_a_missing_pyproject_says_so_rather_than_counting_its_lines(repo, capsys):
    """"pyproject.toml has 0 version lines" about a file that does not exist is a sentence
    that sends the reader to look at a file they will not find."""
    place(repo)
    (repo / "pyproject.toml").unlink()
    write(repo, "app/routes.py", "# a board change\n")
    assert run(repo, "apply", "--onto", "main") == 2
    assert "pyproject.toml does not exist" in capsys.readouterr().err


def test_a_missing_main_py_says_so_rather_than_blaming_the_regex(repo, capsys):
    place(repo)
    (repo / "app" / "main.py").unlink()
    write(repo, "app/routes.py", "# a board change\n")
    assert run(repo, "apply", "--onto", "main") == 2
    assert "app/main.py does not exist" in capsys.readouterr().err


def test_a_version_inside_another_argument_is_not_the_served_version(repo, capsys):
    """`description="… version=\\"1.0.0\\" …"` is prose about a version, not the keyword
    argument. Matching it would rewrite a docstring, leave the real served version where it
    was, and report success — the one outcome worse than refusing, because nothing looks
    wrong afterwards. Quoted strings are atoms here, so the scan cannot reach inside one."""
    place(repo)
    write(repo, "app/main.py",
          'from fastapi import FastAPI\n\n'
          'app = FastAPI(description="written when version=\'1.0.0\' shipped", '
          'version="2.33.0")\n')
    write(repo, "app/routes.py", "# a board change\n")
    assert run(repo, "apply", "--onto", "main") == 0
    text = (repo / "app" / "main.py").read_text()
    assert 'version="2.34.0")' in text
    assert "version='1.0.0'" in text  # the prose is untouched


def test_a_symlinked_served_version_file_is_a_stop(repo, capsys, tmp_path):
    """The markdown scan has always refused to write through a symlink; these two files were
    read and written with no check at all, which made `--serve` the one path by which this
    tool could write a release stamp into a file the repository does not own."""
    outside = tmp_path / "elsewhere.toml"
    outside.write_text(PYPROJECT.format(version="2.33.0"))
    (repo / "pyproject.toml").unlink()
    (repo / "pyproject.toml").symlink_to(outside)
    place(repo)
    write(repo, "app/routes.py", "# a board change\n")
    assert run(repo, "apply", "--onto", "main") == 2
    assert "pyproject.toml is a symlink" in capsys.readouterr().err
    assert 'version = "2.33.0"' in outside.read_text()


# ---------------------------------------------------------------------------
# the guard
# ---------------------------------------------------------------------------


def test_check_is_clean_on_a_released_tree(repo, capsys):
    assert run(repo, "check") == 0
    assert "clean" in capsys.readouterr().out


def test_check_fails_on_an_unstamped_tree(repo, capsys):
    """What CI runs on main after a merge. Exit 2 with the paths, because "a release landed
    without being stamped" is recoverable in one command and unnoticeable without this."""
    place(repo)
    assert run(repo, "check") == 2
    err = capsys.readouterr().err
    assert "CHANGELOG.md:" in err and "README.md:" in err


def test_check_needs_no_base_ref(repo, capsys):
    """Deliberately: the guard runs on an integration branch that may have no upstream
    configured, and a guard that errored on a missing ref would report the same exit code
    as the defect it looks for.

    So the condition has to be arranged rather than assumed. The fixture repo has no remote,
    but it does have branches and a HEAD that resolves; this detaches HEAD, deletes every
    other branch and points `origin/main` at nothing, which is the state a fresh CI checkout
    of a merge commit is actually in. `preflight` against that ref is the contrast: it needs
    a base, and says so with the same exit code the guard reserves for a real defect, which
    is exactly why `check` must not need one."""
    git(repo, "checkout", "-q", "--detach")
    git(repo, "branch", "-q", "-D", "main")
    git(repo, "branch", "-q", "-D", "work")
    assert run(repo, "check") == 0
    assert "clean" in capsys.readouterr().out

    place(repo)
    assert run(repo, "preflight", "--onto", "origin/main") == 2
    assert "does not exist here" in capsys.readouterr().err
    assert run(repo, "check") == 2


def test_preflight_changes_nothing(repo):
    place(repo)
    before = (repo / "CHANGELOG.md").read_text()
    assert run(repo, "preflight", "--onto", "main") == 0
    assert (repo / "CHANGELOG.md").read_text() == before


def test_json_reports_the_plan(repo, capsys):
    place(repo)
    plan = plan_json(repo, "preflight", "--onto", "main", capsys=capsys)
    assert plan["version"] == "v2.34"
    assert plan["onto_newest"] == "v2.33"
    assert plan["serves"] is False
    assert {s["path"] for s in plan["sites"]} == {"CHANGELOG.md", "README.md"}


def test_json_check_reports_the_offending_lines(repo, capsys):
    place(repo)
    assert run(repo, "check", "--json") == 2
    out = json.loads(capsys.readouterr().out)
    assert out["clean"] is False
    assert {s["path"] for s in out["sites"]} == {"CHANGELOG.md", "README.md"}


# ---------------------------------------------------------------------------
# the masking primitive, directly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("plain vNEXT", True),
    ("`vNEXT`", False),
    ("``a ` b vNEXT``", False),
    ("```\nvNEXT\n```", False),
    ("~~~\nvNEXT\n~~~", False),
    ("```\ncode\n```\nvNEXT", True),
    # A span may wrap a line: prose here is hard-wrapped at 100 columns and a long code
    # span reaches a boundary eventually.
    ("`a long\nspan vNEXT`", False),
    # It may not cross a blank line. An unbalanced backtick with no such bound pairs with
    # the next one anywhere in the file and blanks a real placeholder site on the way — and
    # `check` masks identically, so nothing downstream would notice.
    ("a stray ` tick\n\n## vNEXT — a release\n\nand ` another", True),
])
def test_mask_code_hides_only_code(text, expected):
    """Length is preserved by construction — every rewrite is applied to the ORIGINAL text by
    offset, so a mask that changed length would corrupt the file it was protecting."""
    masked = rs.mask_code(text)
    assert len(masked) == len(text)
    assert ("vNEXT" in masked) is expected


def test_a_longer_fence_is_not_closed_by_a_shorter_one():
    """A four-backtick fence exists to wrap a three-backtick one — which is how you document
    a markdown convention that itself contains fenced examples, and is what this repo's own
    README does. Closing the outer block on the inner block's first line leaves everything
    after it unmasked, so a `## vNEXT` still inside the documentation reads as a live
    placeholder and the stamper rewrites the instructions into a description of one release."""
    text = "````md\n```\n## vNEXT — an example\n```\n````\n\nreal prose\n"
    assert "vNEXT" not in rs.mask_code(text)


def test_an_unterminated_fence_is_a_stop_not_a_silent_blanking():
    """The largest silent failure available in this file, and the reason it is loud.

    `scan`, `stamp_text` and `check` all mask through this one function, so honouring
    CommonMark's "runs to the end of the document" would make a real `## vNEXT` below a stray
    ``` invisible to all three at once — the stamper walks past it, the guard agrees, and the
    literal string ships with nothing having failed. The inline-span rule a few lines below
    is bounded to a paragraph for exactly this reason; the fence case has the same shape and
    a far larger blast radius, so it gets an explicit refusal rather than a bound."""
    with pytest.raises(rs.StampError) as e:
        rs.mask_code("# doc\n\n```md\n## vNEXT — an example\n\nand on it goes\n", "docs.md")
    assert "never closes it" in str(e.value) and "line 3" in str(e.value)


def test_an_unterminated_fence_in_a_scanned_file_is_a_stop_not_a_traceback(repo, capsys):
    place(repo)
    write(repo, "docs.md", "# how\n\n```md\n## vNEXT — <title>\n")
    commit(repo, "a doc with a fence left open")
    assert run(repo, "apply", "--onto", "main") == 2
    assert "never closes it" in capsys.readouterr().err
    assert "## vNEXT — a release" in (repo / "CHANGELOG.md").read_text()


# ---------------------------------------------------------------------------
# masking is not optional on the number either
# ---------------------------------------------------------------------------


def test_a_fenced_heading_at_the_base_does_not_inflate_the_number(repo):
    """The base CHANGELOG documents the convention with a fenced example, which this repo's
    own now does. Parsed as a real heading it is the highest in the file, and the branch is
    handed a number a thousand releases away — from a code block, silently."""
    git(repo, "checkout", "-q", "main")
    write(repo, "CHANGELOG.md", (repo / "CHANGELOG.md").read_text()
          + "\nWrite one like this:\n\n```md\n## v999 — an example heading\n```\n")
    commit(repo, "document the convention")
    git(repo, "checkout", "-q", "work")
    git(repo, "merge", "-q", "main")
    place(repo)
    assert run(repo, "apply", "--onto", "main") == 0
    assert "## v2.34 — a release" in (repo / "CHANGELOG.md").read_text()


def test_a_fenced_heading_on_the_branch_is_not_a_duplicate_number(repo):
    """The mirror image: a fenced `## v2.34` in the branch's own CHANGELOG is documentation,
    and reading it as a real entry would refuse a correct branch with "already has an entry
    for it" about a line inside a code block."""
    place(repo)
    text = (repo / "CHANGELOG.md").read_text()
    write(repo, "CHANGELOG.md", text + "\n```md\n## v2.34 — an example heading\n```\n")
    assert run(repo, "apply", "--onto", "main") == 0
    assert "## v2.34 — a release" in (repo / "CHANGELOG.md").read_text()
    assert "## v2.34 — an example heading" in (repo / "CHANGELOG.md").read_text()


# ---------------------------------------------------------------------------
# what a placeholder site is, exactly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line,stamped", [
    ("- **vNEXT** — a release.", True),
    ("- __vNEXT__ — a release.", True),
    ("- ***vNEXT*** — a release.", True),
    ("#### vNEXT — a sub-heading.", True),
])
def test_every_bold_spelling_of_a_placeholder_is_a_site(repo, line, stamped):
    """`__vNEXT__` and `***vNEXT***` are valid markdown for the same thing an author who
    writes `**vNEXT**` means. Classifying them as loose mentions refuses a correct branch
    over a punctuation preference, in a tool whose refusals are meant to be unambiguous."""
    place(repo, bullet=False)
    write(repo, "docs.md", f"# doc\n\n{line}\n")
    commit(repo, "a doc")
    assert run(repo, "apply", "--onto", "main") == 0
    assert "v2.34" in (repo / "docs.md").read_text()
    assert "vNEXT" not in (repo / "docs.md").read_text()


def test_a_closing_bold_run_before_the_token_is_a_loose_mention(repo, capsys):
    """In `**shipped**vNEXT` the `**` immediately before the token CLOSES an unrelated bold
    run, so the token renders as plain running prose. Reading it as a rewritable site is the
    exact inverse of what the loose check is for: a placeholder nothing would render as a
    placeholder gets silently stamped instead of being caught."""
    place(repo)
    write(repo, "docs.md", "# doc\n\nThe release **shipped**vNEXT and nobody noticed.\n")
    commit(repo, "a doc")
    assert run(repo, "apply", "--onto", "main") == 2
    assert "will not be rewritten" in capsys.readouterr().err


def test_a_three_component_release_heading_is_not_a_release(repo, capsys):
    """`\\b` fires between a digit and a dot, so `## v2.33.1` parsed as release (2, 33) — a
    number the base does not actually declare, quietly standing in for one it does. A heading
    this tool cannot number has to not match at all, and the refusal that follows says the
    base ref is not the file this tool thinks it is, which is exactly the situation."""
    git(repo, "checkout", "-q", "main")
    write(repo, "CHANGELOG.md", CHANGELOG_HEAD + "## v2.33.1 — a patch\n\ndid a thing.\n")
    commit(repo, "a three-component heading")
    git(repo, "checkout", "-q", "work")
    place(repo)
    assert run(repo, "preflight", "--onto", "main") == 2
    assert "no `## vX.Y` headings at the base ref" in capsys.readouterr().err


def test_a_three_component_placeholder_heading_is_not_a_placeholder(repo, capsys):
    """`## vNEXT.1` matched `_HEADING_PLACEHOLDER` for the same reason, and the rewrite —
    being a span replacement over the token alone — left the trailing `.1` behind and
    produced `## v2.34.1`, a version this repo has no meaning for and no test asserts about.
    Now it is neither a heading nor a rewritable site, so it lands where it belongs: a
    placeholder written in a shape the stamper will not rewrite, which is a refusal rather
    than a shrug, with the line number of the thing to fix."""
    write(repo, "CHANGELOG.md",
          CHANGELOG_HEAD + "## vNEXT.1 — a patch\n\ndid a thing.\n\n" + entry("v2.33"))
    place(repo, changelog=False)
    assert run(repo, "preflight", "--onto", "main") == 2
    err = capsys.readouterr().err
    assert "will not be rewritten" in err and "CHANGELOG.md:5" in err


def test_uppercase_markdown_extensions_are_scanned_too(repo, capsys):
    """`git ls-files -- '*.md'` is case-sensitive on Linux, so a `README.MD` was simply not
    in the scan — the same silent gap the untracked and symlink warnings exist to close, for
    the cost of one pathspec magic word."""
    place(repo)
    write(repo, "NOTES.MD", "# notes\n\n**vNEXT** — the notes half.\n")
    commit(repo, "an uppercase-extension doc")
    assert run(repo, "apply", "--onto", "main") == 0
    assert "**v2.34** — the notes half." in (repo / "NOTES.MD").read_text()


# ---------------------------------------------------------------------------
# two branches, one number
# ---------------------------------------------------------------------------


def test_a_stamped_branch_whose_number_was_taken_is_a_stop_not_a_noop(repo, capsys):
    """The failure this whole file exists to remove, arriving by the one door the placeholder
    cannot hold shut: both branches stamped before either landed.

    Re-running `apply` cannot fix it and must not pretend to — the placeholder is gone, there
    is nothing left to rewrite, and the early "nothing to stamp" return would print `noop:`
    and exit 0 on the exact state the tool was written to catch. So the collision is checked
    BEFORE that return, and the message says what to do: put this branch's entry back to the
    placeholder and run `apply` again."""
    place(repo)
    assert run(repo, "apply", "--onto", "main") == 0
    commit(repo, "work, stamped v2.34")

    git(repo, "checkout", "-q", "main")
    text = (repo / "CHANGELOG.md").read_text()
    write(repo, "CHANGELOG.md",
          text.replace("## v2.33", entry("v2.34", title="somebody else's release") + "## v2.33", 1))
    commit(repo, "somebody else landed v2.34 first")
    git(repo, "checkout", "-q", "work")

    assert run(repo, "apply", "--onto", "main") == 2
    err = capsys.readouterr().err
    assert "the same release number for two different releases" in err
    assert "Put THIS branch's entry back" in err


def test_the_repair_the_message_names_actually_works(repo, capsys):
    """The loop the docs promise, end to end. A collision refusal that told you to do
    something that did not then work would be worse than no message at all, and the absence
    of this test is plausibly why the docs described a loop nobody had run."""
    place(repo)
    assert run(repo, "apply", "--onto", "main") == 0
    commit(repo, "work, stamped v2.34")
    git(repo, "checkout", "-q", "main")
    text = (repo / "CHANGELOG.md").read_text()
    write(repo, "CHANGELOG.md",
          text.replace("## v2.33", entry("v2.34", title="somebody else's release") + "## v2.33", 1))
    commit(repo, "somebody else landed v2.34 first")
    git(repo, "checkout", "-q", "work")
    assert run(repo, "apply", "--onto", "main") == 2
    capsys.readouterr()

    # The repair, exactly as the message describes it: two tokens, because nothing else on
    # the branch was ever written in terms of the number.
    for path in ("CHANGELOG.md", "README.md"):
        write(repo, path, (repo / path).read_text().replace("v2.34", "vNEXT", 1))

    assert run(repo, "apply", "--onto", "main") == 0
    assert "## v2.35 — a release" in (repo / "CHANGELOG.md").read_text()
    assert "- **v2.35** — a release." in (repo / "README.md").read_text()


def test_a_number_declared_twice_is_a_stop(repo, capsys):
    """What a "keep both sides" resolution of the CHANGELOG conflict leaves behind: no
    placeholder anywhere, a perfectly clean merge, and one number describing two releases.
    Keeping both sides is the right answer for the prose and the wrong one for the heading."""
    text = (repo / "CHANGELOG.md").read_text()
    write(repo, "CHANGELOG.md",
          text.replace("## v2.33", entry("v2.33", "and somebody else's.") + "## v2.33", 1))
    assert run(repo, "preflight", "--onto", "main") == 2
    assert "declares v2.33 more than once" in capsys.readouterr().err


def test_check_fails_on_a_number_declared_twice(repo, capsys):
    """The guard on main has to be able to see this. `check` only ever looked for the literal
    `vNEXT`, and by the time two branches have both been stamped there is no placeholder left
    to find — so the one state this mechanism exists to prevent was the one state nothing
    anywhere observed."""
    text = (repo / "CHANGELOG.md").read_text()
    write(repo, "CHANGELOG.md",
          text.replace("## v2.33", entry("v2.33", "and somebody else's.") + "## v2.33", 1))
    assert run(repo, "check") == 2
    err = capsys.readouterr().err
    assert "declared twice" in err and "v2.33" in err

    assert run(repo, "check", "--json") == 2
    assert json.loads(capsys.readouterr().out)["duplicates"] == ["v2.33"]


# ---------------------------------------------------------------------------
# the major bump
# ---------------------------------------------------------------------------


def test_major_stamps_the_next_major_not_the_next_minor(repo):
    """The one thing no ref can answer. Whether v2.34 or v3 follows v2.33 is a judgement
    about what the release MEANS — but the flag has to exist, because this repo's README
    lists v3 as what is next, and a tool that could not produce it would be opted out of by
    hand on the one release that most needs the convention."""
    place(repo)
    assert run(repo, "apply", "--onto", "main", "--major") == 0
    assert "## v3 — a release" in (repo / "CHANGELOG.md").read_text()
    assert "- **v3** — a release." in (repo / "README.md").read_text()


def test_a_major_bump_moves_the_served_version_to_the_new_major(repo):
    place(repo)
    write(repo, "app/routes.py", "# a board change\n")
    assert run(repo, "apply", "--onto", "main", "--major") == 0
    assert 'version = "3.0.0"' in (repo / "pyproject.toml").read_text()


def test_the_plan_says_which_kind_of_bump_it_is(repo, capsys):
    place(repo)
    plan = plan_json(repo, "preflight", "--onto", "main", "--major", capsys=capsys)
    assert plan["version"] == "v3" and plan["major"] is True
    plan = plan_json(repo, "preflight", "--onto", "main", capsys=capsys)
    assert plan["version"] == "v2.34" and plan["major"] is False


# ---------------------------------------------------------------------------
# the refusals that must not be tracebacks
# ---------------------------------------------------------------------------


def test_a_base_ref_without_a_changelog_is_a_stop_with_a_sentence(repo, capsys):
    """A generic "git show ... failed" is the shape of message that sends a reader to look at
    git rather than at their `--onto`."""
    git(repo, "checkout", "-q", "main")
    git(repo, "rm", "-q", "CHANGELOG.md")
    commit(repo, "no changelog here")
    git(repo, "checkout", "-q", "work")
    place(repo)
    assert run(repo, "preflight", "--onto", "main") == 2
    assert "has no CHANGELOG.md" in capsys.readouterr().err


def test_markdown_that_is_not_utf8_is_a_stop_not_a_traceback(repo, capsys):
    """The exit-code contract is explicit that a refusal is always a 2 with a sentence and
    never a traceback, and a bare `read_text()` breaks it on an input this scan will meet:
    tracked markdown that is not UTF-8 at all. Exit 1 is what a caller consuming 0/2 reads as
    "unknown", which is the one answer this tool promises not to give."""
    place(repo)
    (repo / "broken.md").write_bytes(b"# notes\n\n\xff\xfe not utf-8 at all\n")
    commit(repo, "a latin-1 doc")
    assert run(repo, "apply", "--onto", "main") == 2
    err = capsys.readouterr().err
    assert "broken.md is not valid UTF-8" in err
    assert "## vNEXT — a release" in (repo / "CHANGELOG.md").read_text()


def test_an_unwritable_file_leaves_the_tree_as_it_was(repo, capsys):
    """Precomputing the edits removes the failure where the TOOL refuses on file four. It
    does nothing about a permission error on file four, which is the same half-applied
    release by a different door — markdown stamped, served version not, and a traceback
    instead of the documented refusal. So the originals are held and put back."""
    place(repo)
    write(repo, "harness/loops/README.md", "# loops\n\n**vNEXT** — the loops half.\n")
    before = {p: (repo / p).read_text()
              for p in ("CHANGELOG.md", "README.md", "harness/loops/README.md")}
    (repo / "harness" / "loops" / "README.md").chmod(0o444)
    try:
        assert run(repo, "apply", "--onto", "main") == 2
    finally:
        (repo / "harness" / "loops" / "README.md").chmod(0o644)
    assert "nothing was written" in capsys.readouterr().err
    for path, text in before.items():
        assert (repo / path).read_text() == text


# ---------------------------------------------------------------------------
# the guard, and what it is allowed to be quiet about
# ---------------------------------------------------------------------------


def test_check_fails_on_a_loose_mention_too(repo, capsys):
    """Both halves of `bad = sites + loose`. A placeholder nothing would rewrite is exactly
    as much of a defect on main as one that would have been — more so, since `apply` cannot
    even repair it."""
    write(repo, "docs.md", "# how\n\nThis release is vNEXT and ships tomorrow.\n")
    commit(repo, "a doc")
    assert run(repo, "check") == 2
    assert "docs.md:3" in capsys.readouterr().err


def test_check_reports_a_symlinked_markdown_file_rather_than_passing_over_it(
        repo, capsys, tmp_path):
    """`check` threaded no accumulator, so a tracked markdown symlink was dropped with no
    record at all — and the guard whose one job is catching the literal string printed
    "clean" over a file it had not read. `preflight` and `apply` warned about the same file,
    so the two commands disagreed about the same repo state."""
    outside = tmp_path / "outside.md"
    outside.write_text("## vNEXT — somebody else's document\n")
    (repo / "linked.md").symlink_to(outside)
    commit(repo, "a tracked symlink")

    assert run(repo, "check") == 0  # nothing IN this repo is unstamped
    assert "linked.md" in capsys.readouterr().err  # but it is said out loud

    assert run(repo, "check", "--json") == 0
    assert json.loads(capsys.readouterr().out)["symlinked"] == ["linked.md"]


def test_check_json_carries_the_untracked_signal(repo, capsys):
    """A CI consumer of `check --json` had no field to inspect: the payload was
    `{clean, sites, loose}` and nothing else, while the human output of the sibling commands
    carried both skipped categories."""
    (repo / "notes.md").write_text("# notes\n\n## vNEXT — a scratch entry\n")
    assert run(repo, "check", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert [s["path"] for s in out["untracked"]] == ["notes.md"]
    assert out["clean"] is True  # untracked is reported, never a failure


def test_a_broken_symlink_is_reported_rather_than_dropped(repo, capsys):
    """`Path.exists()` follows the link and answers False for a broken one, so testing it
    first dropped a broken tracked symlink with no record at all — the same quiet skip this
    accounting exists to prevent, one edge case along."""
    (repo / "linked.md").symlink_to(repo / "nowhere.md")
    place(repo)
    commit(repo, "a broken tracked symlink")
    assert run(repo, "apply", "--onto", "main") == 0
    assert "linked.md" in capsys.readouterr().err


def test_a_loose_mention_does_not_stop_a_branch_that_ships_no_release(repo, capsys):
    """`harness/commands/fix-and-land.md` tells the operator to run this unconditionally
    because "it is a noop on a branch that ships no release" — which was false whenever any
    tracked doc anywhere carried a stray mention, since the loose refusal ran before the
    no-sites return and stopped every branch in the repo. It is still a defect, so it is
    warned about here and `check` still fails on it the moment it reaches main."""
    write(repo, "docs.md", "# how\n\nThis release is vNEXT and ships tomorrow.\n")
    commit(repo, "a doc, on a branch shipping no release")
    assert run(repo, "apply", "--onto", "main") == 0
    out = capsys.readouterr()
    assert "noop" in out.out
    assert "docs.md:3" in out.err
    assert run(repo, "check") == 2


def test_an_unstamped_placeholder_anywhere_at_the_base_is_a_stop(repo, capsys):
    """Not only in CHANGELOG.md. A base carrying a stray `**vNEXT**` in a nested doc is the
    same defect — the previous release landed half-stamped — and numbering on top of it hands
    this branch a number the unstamped entry is going to want."""
    git(repo, "checkout", "-q", "main")
    write(repo, "harness/loops/README.md", "# loops\n\n**vNEXT** — the loops half.\n")
    commit(repo, "half-stamped on main")
    git(repo, "checkout", "-q", "work")
    place(repo)
    assert run(repo, "preflight", "--onto", "main") == 2
    err = capsys.readouterr().err
    assert "itself carries an unstamped" in err and "harness/loops/README.md" in err


def test_a_noop_apply_still_reports_a_written_key(repo, capsys):
    """A schema with two shapes is one the caller discovers the second of in production."""
    assert run(repo, "apply", "--onto", "main", "--json") == 0
    assert json.loads(capsys.readouterr().out)["written"] == []
