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
import os
import shutil
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

#: The shape a formatter produces the moment the call grows a third argument, and the one the
#: repo will have the day somebody runs `ruff format` over `app/main.py`. It is a fixture
#: rather than an inline string because "the tool works on single-line calls only" is not a
#: property anybody would choose, and nothing said so out loud until it stopped working.
MAIN_PY_WRAPPED = '''from fastapi import FastAPI

app = FastAPI(
    title="quarterback",
    lifespan=make_lifespan(),
    version="{version}",
)
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


#: chmod means nothing to root, and this stack's CI and dev containers both run as it. A test
#: whose subject is "the tool refuses when it cannot write" would otherwise report a rollback
#: bug when the real difference is the user id, which is the least legible failure available.
not_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores file mode bits, so an unwritable file is not unwritable",
)


@pytest.fixture(autouse=True)
def hermetic_git(monkeypatch, tmp_path: Path) -> None:
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


@not_root
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


# ---------------------------------------------------------------------------
# the rollback, at the two failures that lose data
# ---------------------------------------------------------------------------


def test_the_file_whose_own_write_failed_is_restored_too(tmp_path, monkeypatch):
    """`write_text` opens in mode `w`, which truncates before it writes a byte. A failure part
    way through — a full disk, an I/O error, a quota — therefore leaves THAT file empty while
    it is still absent from the written list, and a rollback that skipped it went on to report
    "nothing was written; the worktree is as you left it" over a file it had just emptied.

    The permission test above cannot catch this: a read-only file fails at open(), before
    truncation, so the one failure mode with real data loss in it was the untested one."""
    doomed = tmp_path / "doomed.md"
    doomed.write_text("the original\n")

    def truncate_then_fail(path: Path, text: str, what: str) -> None:
        path.write_text("")  # what mode 'w' does before the disk says no
        raise rs.StampError(f"cannot write {what}: No space left on device")

    monkeypatch.setattr(rs, "_write", truncate_then_fail)
    with pytest.raises(rs.StampError) as e:
        rs._write_all([("doomed.md", doomed, "## v2.34 — a release\n")])

    assert doomed.read_text() == "the original\n"
    assert "nothing was written" in str(e.value)


def test_a_rollback_that_cannot_restore_says_which_file_it_left(tmp_path, monkeypatch):
    """The other end of the same helper, and the branch nothing exercised. When putting a file
    back fails too, "nothing was written" is a lie and the only useful thing left is the list
    of paths to look at — so the message names them and says the release is half stamped."""
    kept = tmp_path / "sub"
    kept.mkdir()
    first, second = kept / "first.md", tmp_path / "second.md"
    first.write_text("first original\n")
    second.write_text("second original\n")
    real_write = rs._write

    def fail_on_the_second(path: Path, text: str, what: str) -> None:
        if path == second:
            shutil.rmtree(kept)  # the first file's directory goes away mid-run
            raise rs.StampError(f"cannot write {what}: Input/output error")
        real_write(path, text, what)

    monkeypatch.setattr(rs, "_write", fail_on_the_second)
    with pytest.raises(rs.StampError) as e:
        rs._write_all([("first.md", first, "stamped\n"), ("second.md", second, "stamped\n")])

    message = str(e.value)
    assert "rolling back left" in message and "first.md" in message
    assert "half stamped" in message
    assert "nothing was written" not in message


# ---------------------------------------------------------------------------
# the served version, when the call is not on one line
# ---------------------------------------------------------------------------


def test_a_multi_line_fastapi_call_is_still_bumped(repo):
    """The canonical formatter output — `FastAPI(\\n    title=…,\\n    version="X.Y.Z",\\n)` —
    and for a while the one shape the tool could not read. Excluding newlines from the
    argument atom made every wrapped call report "no version literal to bump" about a file
    that plainly had one, which blocked `--serve` on a repo whose only crime was running
    `ruff format`. Quoted strings being atoms is what makes the scan safe; the line boundary
    was never doing that work."""
    place(repo)
    write(repo, "app/main.py", MAIN_PY_WRAPPED.format(version="2.33.0"))
    write(repo, "app/routes.py", "# a board change\n")
    assert run(repo, "apply", "--onto", "main") == 0
    assert 'version="2.34.0",' in (repo / "app" / "main.py").read_text()


def test_a_version_first_in_a_multi_line_call_is_bumped(repo):
    """`version` on the line straight after the paren, with no comma before it — the optional
    "everything up to a comma" group has to be skippable across a newline, not only against
    the paren itself."""
    place(repo)
    write(repo, "app/main.py",
          'from fastapi import FastAPI\n\napp = FastAPI(\n    version="2.33.0",\n'
          '    title="quarterback",\n)\n')
    write(repo, "app/routes.py", "# a board change\n")
    assert run(repo, "apply", "--onto", "main") == 0
    assert 'version="2.34.0",' in (repo / "app" / "main.py").read_text()


def test_an_escaped_quote_in_a_title_does_not_desynchronise_the_scan(repo):
    r"""`title="ends\", version=\"8.8.8\" here"` is ONE string. A quoted-string atom with no
    escape handling ends at the escaped quote, so the atom boundary and the real string
    boundary come apart and everything after them is read as the wrong kind of thing — here
    the whole call stopped matching, and the tool refused a file whose version literal was
    plainly present. The same desynchronisation the other way round reads text inside the
    literal as a real keyword argument."""
    place(repo)
    write(repo, "app/main.py",
          'from fastapi import FastAPI\n\n'
          'app = FastAPI(title="ends\\", version=\\"8.8.8\\" here", version="2.33.0")\n')
    write(repo, "app/routes.py", "# a board change\n")
    assert run(repo, "apply", "--onto", "main") == 0
    text = (repo / "app" / "main.py").read_text()
    assert 'version="2.34.0")' in text
    assert 'version=\\"8.8.8\\"' in text  # the prose inside the literal is untouched


def test_a_single_quoted_package_version_is_found(repo):
    """TOML gives basic and literal strings equal standing. A `version = '2.33.0'` was
    invisible to the line matcher, so the tool reported "0 version lines in [project]" about a
    file whose version is present, correct, and spelled the other legal way."""
    place(repo)
    write(repo, "pyproject.toml",
          "[project]\nname = \"quarterback\"\nversion = '2.33.0'\n")
    write(repo, "app/routes.py", "# a board change\n")
    assert run(repo, "apply", "--onto", "main") == 0
    assert "version = '2.34.0'" in (repo / "pyproject.toml").read_text()


def test_a_bracketed_continuation_line_does_not_end_the_project_table(repo):
    """`^[ \\t]*\\[` matches a wrapped array element that happens to start with `[`, which cut
    the `[project]` span short — and the user got "0 version lines in [project]" about a file
    whose version sits two lines below the truncation."""
    place(repo)
    write(repo, "pyproject.toml",
          '[project]\nname = "quarterback"\nmatrix = [\n  ["a", "b"]\n]\n'
          'version = "2.33.0"\n')
    write(repo, "app/routes.py", "# a board change\n")
    assert run(repo, "apply", "--onto", "main") == 0
    assert 'version = "2.34.0"' in (repo / "pyproject.toml").read_text()


def test_a_version_inside_a_multiline_toml_string_is_not_the_package_version(repo, capsys):
    """A regex over raw text cannot see that a `[project]`-looking line is inside a multi-line
    string, so the tool could rewrite prose in a `description` and report success. `tomllib`
    can see it, so the two answers are required to agree — and when they do not, this refuses
    rather than picking one."""
    place(repo)
    write(repo, "pyproject.toml",
          '[project]\nname = "quarterback"\ndescription = """\n'
          '[project]\nversion = "9.9.9"\n"""\nversion = "2.33.0"\n')
    write(repo, "app/routes.py", "# a board change\n")
    assert run(repo, "apply", "--onto", "main") == 2
    assert "will not guess which text is the package version" in capsys.readouterr().err
    assert 'version = "9.9.9"' in (repo / "pyproject.toml").read_text()  # untouched


def test_a_pyproject_that_is_not_toml_is_a_stop_not_a_traceback(repo, capsys):
    place(repo)
    write(repo, "pyproject.toml", '[project]\nname = "quarterback\nversion = "2.33.0"\n')
    write(repo, "app/routes.py", "# a board change\n")
    assert run(repo, "apply", "--onto", "main") == 2
    assert "not valid TOML" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# the collision detector, and what it is NOT allowed to refuse
# ---------------------------------------------------------------------------


def test_editing_a_released_entrys_title_is_not_a_collision(repo):
    """The check compares against the FORK POINT, not against heading text, and this is why.
    Comparing text made every edit to a shipped entry — fixing a typo, rewrapping a long
    title, normalising an em dash — read as "two branches took v2.32", with a repair message
    telling the author to put an entry that shipped weeks ago back to `## vNEXT`. The question
    that matters is whether the branch CLAIMED the number or inherited it."""
    place(repo)
    text = (repo / "CHANGELOG.md").read_text()
    write(repo, "CHANGELOG.md", text.replace("## v2.32 — a release", "## v2.32 — a relase"))

    assert run(repo, "apply", "--onto", "main") == 0
    assert "## v2.34 — a release" in (repo / "CHANGELOG.md").read_text()


def test_a_docs_only_branch_may_edit_a_released_title(repo, capsys):
    """The same shape with nothing to stamp at all. `fix-and-land` runs `apply`
    unconditionally and wires exit 2 straight to a HOLD, so a branch that only fixes a
    CHANGELOG typo must reach the noop rather than the refusal."""
    text = (repo / "CHANGELOG.md").read_text()
    write(repo, "CHANGELOG.md", text.replace("## v2.32 — a release", "## v2.32 — a relase"))
    assert run(repo, "apply", "--onto", "main") == 0
    assert "noop" in capsys.readouterr().out


def test_two_releases_sharing_a_title_still_collide(repo, capsys):
    """The mirror of the test above, and why heading text cannot be the proxy in the other
    direction either: two branches that both wrote a boilerplate title got identical heading
    lines, `theirs != line` was false, and one number describing two releases passed straight
    through the check built to stop it."""
    place(repo)
    assert run(repo, "apply", "--onto", "main") == 0
    commit(repo, "work, stamped v2.34")

    git(repo, "checkout", "-q", "main")
    text = (repo / "CHANGELOG.md").read_text()
    write(repo, "CHANGELOG.md",
          text.replace("## v2.33", entry("v2.34", "somebody else's body.") + "## v2.33", 1))
    commit(repo, "somebody else landed v2.34 first, under the same title")
    git(repo, "checkout", "-q", "work")

    assert run(repo, "apply", "--onto", "main") == 2
    assert "the same release number for two different releases" in capsys.readouterr().err


def test_a_base_that_already_declares_a_number_twice_is_a_stop(repo, capsys):
    """A branch that has not taken the merge yet cannot see the duplicate in its own file, so
    scanning only `branch_text` left the base's invalid state undetected — and stamping on top
    of it compounds it. The repair belongs on the base, and the message says so rather than
    telling this branch to put its entry back."""
    git(repo, "checkout", "-q", "main")
    text = (repo / "CHANGELOG.md").read_text()
    write(repo, "CHANGELOG.md",
          text.replace("## v2.33", entry("v2.33", "and somebody else's.") + "## v2.33", 1))
    commit(repo, "a keep-both-sides merge nobody caught")
    git(repo, "checkout", "-q", "work")
    place(repo)

    assert run(repo, "preflight", "--onto", "main") == 2
    err = capsys.readouterr().err
    assert "main itself declares v2.33 more than once" in err


# ---------------------------------------------------------------------------
# what a branch with nothing to stamp is allowed to need
# ---------------------------------------------------------------------------


def test_a_branch_with_nothing_to_stamp_needs_no_resolvable_base(repo, capsys):
    """`fix-and-land` says to run `apply` unconditionally because it is a noop on a branch
    that ships no release, and wires exit 2 straight to a HOLD. Resolving the ref before the
    no-op return made that false on a fresh clone, a fork off another default branch, or a CI
    checkout that only fetched the PR head: every branch in the repo became a hold."""
    assert run(repo, "apply", "--onto", "origin/never-fetched") == 0
    assert "noop" in capsys.readouterr().out


def test_a_branch_with_something_to_stamp_still_needs_one(repo, capsys):
    """The other half of the same rule, and the reason it is scoped rather than dropped: a
    number handed out against a ref that does not exist is a guess."""
    place(repo)
    assert run(repo, "apply", "--onto", "origin/never-fetched") == 2
    assert "does not exist here" in capsys.readouterr().err


def test_a_placeholder_heading_nothing_can_rewrite_is_a_stop_on_its_own(repo, capsys):
    """`## vNEXT.1` matches neither the heading placeholder nor a rewritable site, but it does
    match a loose mention — so with no other site anywhere it landed in the no-op return and
    `apply` printed `noop:` over an unstampable release placeholder sitting in the CHANGELOG.

    A loose mention in CHANGELOG.md is a release entry written in a shape nothing will
    rewrite, and refuses on its own; one anywhere else is a defect in that doc and is warned
    about, because making one stray word refuse every branch in the repo is how a gate that is
    right in principle gets switched off in practice."""
    write(repo, "CHANGELOG.md",
          CHANGELOG_HEAD + "## vNEXT.1 — a patch\n\ndid a thing.\n\n" + entry("v2.33"))
    assert run(repo, "apply", "--onto", "main") == 2
    assert "will not be rewritten" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# paths this tool refuses to write through
# ---------------------------------------------------------------------------


def test_a_symlinked_parent_directory_is_not_written_through(repo, capsys, tmp_path):
    """The leaf check was the whole guard, and it does not hold: a symlinked `app/` passes a
    leaf-only test on `app/main.py` and the served-version write then lands wherever the link
    points — which is the exact escape the leaf check exists to prevent, one directory up."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "main.py").write_text(MAIN_PY.format(version="2.33.0"))
    shutil.rmtree(repo / "app")
    (repo / "app").symlink_to(outside)
    place(repo)

    assert run(repo, "apply", "--onto", "main", "--serve") == 2
    assert "app/main.py is a symlink, or sits under one" in capsys.readouterr().err
    assert 'version="2.33.0"' in (outside / "main.py").read_text()
    assert "## vNEXT — a release" in (repo / "CHANGELOG.md").read_text()


def test_a_symlinked_changelog_is_a_stop_rather_than_a_number_from_outside(repo, capsys,
                                                                          tmp_path):
    """The markdown scan skipped a symlinked CHANGELOG.md and said so, but the read that
    computes the release number went straight through it — so the number could come from a
    file outside the repository that would then never be written to."""
    outside = tmp_path / "outside.md"
    outside.write_text(CHANGELOG_HEAD + entry("v9.9"))
    (repo / "CHANGELOG.md").unlink()
    (repo / "CHANGELOG.md").symlink_to(outside)
    place(repo, changelog=False)
    assert run(repo, "preflight", "--onto", "main") == 2
    assert "CHANGELOG.md is a symlink" in capsys.readouterr().err


def test_check_refuses_rather_than_calling_a_symlinked_changelog_clean(repo, capsys,
                                                                      tmp_path):
    """`dupes` was unconditionally empty for a symlinked CHANGELOG, so `clean` could still be
    true and the guard printed "no repeated release number" about a file it never opened —
    the same "clean over a file it did not read" shape the symlink accounting exists to end."""
    outside = tmp_path / "outside.md"
    outside.write_text(CHANGELOG_HEAD + entry("v2.33") + entry("v2.33"))
    (repo / "CHANGELOG.md").unlink()
    (repo / "CHANGELOG.md").symlink_to(outside)
    assert run(repo, "check") == 2
    assert "CHANGELOG.md is a symlink" in capsys.readouterr().err


def test_an_untracked_symlink_is_reported_rather_than_dropped(repo, capsys, tmp_path):
    """Both scans of untracked markdown passed no accumulator, so an untracked symlink
    vanished from the text output and from the JSON `symlinked` field of both commands at
    once — which is the skipped-and-quiet outcome the whole accounting exists to end."""
    outside = tmp_path / "outside.md"
    outside.write_text("## vNEXT — somebody else's document\n")
    (repo / "scratch.md").symlink_to(outside)

    assert run(repo, "check", "--json") == 0
    assert json.loads(capsys.readouterr().out)["symlinked"] == ["scratch.md"]

    place(repo)
    assert run(repo, "preflight", "--onto", "main", "--json") == 0
    assert json.loads(capsys.readouterr().out)["symlinked"] == ["scratch.md"]


def test_untracked_markdown_that_cannot_be_read_is_not_a_stop(repo, capsys):
    """The module contract says untracked markdown is never a STOP, only reported — and the
    scan of it went through the same refusing reader as tracked markdown, so a scratchpad
    that was not UTF-8, or had a fence left open, aborted the whole release."""
    place(repo)
    (repo / "notes.md").write_bytes(b"# notes\n\n\xff\xfe vNEXT\n")
    (repo / "open-fence.md").write_text("# notes\n\n```md\n## vNEXT — a scratch entry\n")

    assert run(repo, "apply", "--onto", "main") == 0
    err = capsys.readouterr().err
    assert "notes.md" in err and "open-fence.md" in err
    assert "## v2.34 — a release" in (repo / "CHANGELOG.md").read_text()


def test_tracked_markdown_that_cannot_be_read_still_is(repo, capsys):
    """The asymmetry is the point: a tracked file in that state ships with the release."""
    place(repo)
    (repo / "broken.md").write_bytes(b"# notes\n\n\xff\xfe vNEXT\n")
    commit(repo, "a latin-1 tracked doc")
    assert run(repo, "apply", "--onto", "main") == 2
    assert "broken.md is not valid UTF-8" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# the guard, over both files that carry a number
# ---------------------------------------------------------------------------


def test_check_fails_on_a_readme_bullet_declared_twice(repo, capsys):
    """README bullets are stamped independently of the CHANGELOG headings, so a merge that
    kept both sides of the release LIST and one side of the CHANGELOG leaves a duplicate
    number nothing else can see. `check` computing duplicates from the CHANGELOG alone printed
    `clean: true` on exactly that state — while the repo's own invariant suite treated both
    files as carrying release numbers."""
    write(repo, "README.md", readme(["v2.32", "v2.33", "v2.33"]))
    assert run(repo, "check") == 2
    err = capsys.readouterr().err
    assert "README.md" in err and "v2.33" in err

    assert run(repo, "check", "--json") == 2
    out = json.loads(capsys.readouterr().out)
    assert out["duplicates"] == ["v2.33"]
    assert out["duplicates_by_file"] == {"README.md": ["v2.33"]}


def test_a_roadmap_bullet_is_not_a_released_number(repo):
    """`- **v3 (next)** — …` names what is coming, not what shipped. Reading it as a release
    would report a duplicate the day v3 is actually stamped, against an entry that is not one
    — so the bold run has to close immediately after the number."""
    write(repo, "README.md",
          readme(["v2.32", "v2.33"]) + "- **v3 (next)** — a roadmap entry.\n")
    assert run(repo, "check") == 0
    assert rs.bullet_releases("- **v3 (next)** — a roadmap entry.\n") == []
    assert rs.bullet_releases("- **v3** — a release.\n") == [(3, 0)]


# ---------------------------------------------------------------------------
# drift between planning and writing
# ---------------------------------------------------------------------------


def test_a_file_that_changed_under_the_plan_is_a_stop(repo, capsys, monkeypatch):
    """Every rewrite is computed before any of them is written, which means the files are read
    twice. A hook, an editor or a concurrent agent between the two reads used to have its
    file silently dropped from the edit list — producing a successful, partially stamped
    release with nothing anywhere reporting what had been skipped."""
    place(repo)
    real_read = rs._read
    reads: list[str] = []

    def drift(path: Path, what: str) -> str:
        # On the SECOND read of README.md — the one `cmd_apply` does after planning — the
        # bullet has gone, which is what a hook or a concurrent editor looks like from here.
        text = real_read(path, what)
        reads.append(what)
        if what == "README.md" and reads.count("README.md") == 2:
            path.write_text(text.replace("- **vNEXT** — a release.\n", ""))
            return path.read_text()
        return text

    monkeypatch.setattr(rs, "_read", drift)
    assert run(repo, "apply", "--onto", "main") == 2
    err = capsys.readouterr().err
    assert "changed between planning and writing" in err
    assert "## vNEXT — a release" in (repo / "CHANGELOG.md").read_text()


# ---------------------------------------------------------------------------
# masking, at the container prefixes
# ---------------------------------------------------------------------------


def test_a_fence_inside_a_blockquote_is_still_a_fence():
    """A quoted example — a review comment pasted into a doc, say — is documentation of the
    convention exactly as an unquoted one is. A masker that could not see the `> ` prefix read
    the block's contents as prose and would have stamped the example."""
    text = "# doc\n\n> ```md\n> ## vNEXT — an example\n> ```\n\nreal prose\n"
    assert "vNEXT" not in rs.mask_code(text)


def test_a_fence_opening_a_list_item_is_still_a_fence():
    text = "# doc\n\n- ```md\n  ## vNEXT — an example\n  ```\n\nreal prose\n"
    assert "vNEXT" not in rs.mask_code(text)


def test_a_quoted_fence_does_not_close_an_unquoted_one():
    """Closing on any same-character marker let a quoted ``` inside a block end it, leaving
    everything below unmasked — a real `## vNEXT` there is then invisible to the stamper and
    to `check` at once, since both mask through this one function."""
    text = "# doc\n\n```md\n> ```\n## vNEXT — an example\n```\n\nreal prose\n"
    assert "vNEXT" not in rs.mask_code(text)


# ---------------------------------------------------------------------------
# git edges that must not surface as a git error
# ---------------------------------------------------------------------------


def test_no_common_ancestor_says_so_rather_than_failing_as_git(repo, capsys, tmp_path):
    """`git merge-base` exits non-zero when there is no common ancestor, and routing it
    through the generic runner turned that into "git merge-base failed:" with an empty stderr
    — while the sentence written for this case sat downstream, unreachable."""
    place(repo)
    git(repo, "checkout", "-q", "--orphan", "unrelated")
    write(repo, "CHANGELOG.md", CHANGELOG_HEAD + entry("vNEXT") + entry("v2.33"))
    commit(repo, "an unrelated history")
    assert run(repo, "preflight", "--onto", "main") == 2
    assert "no common ancestor" in capsys.readouterr().err


def test_a_path_with_a_newline_in_it_does_not_break_the_base_scan(repo, capsys):
    """`git grep --name-only` without `-z` splits a legal path containing a newline into two,
    and both halves then go to `git show` — which fails with a generic git error rather than
    the readable sentence this scan exists to produce. Every other path-reading git call in
    the file already used `-z`."""
    git(repo, "checkout", "-q", "main")
    write(repo, "odd\nname.md", "# odd\n\n**vNEXT** — the half-stamped one.\n")
    commit(repo, "a path with a newline in it")
    git(repo, "checkout", "-q", "work")
    place(repo)
    assert run(repo, "preflight", "--onto", "main") == 2
    assert "itself carries an unstamped" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# a branch that hard-codes its number reaches the checks written to catch it (#167, #168)
#
# Both checks used to sit below `if not plan.sites: return plan`, and "no placeholder" was
# standing in for "ships no release". Those are different states: a branch that hard-coded
# its number ships a release AND has no placeholder, so it returned early and met neither.


def test_a_hand_written_number_is_refused_even_with_no_placeholder_to_stamp(repo, capsys):
    """#167's own worked example: `## v<base+7>`, no `vNEXT` anywhere, must be refused.

    This is the shape the check existed for and could not see. Measured across an eight-PR
    queue in the issue: all eight hard-coded a number, none carried a placeholder, and the
    guard fired for none of them — so it was inert exactly when it was needed.
    """
    text = (repo / "CHANGELOG.md").read_text()
    write(repo, "CHANGELOG.md", text.replace("## v2.33", entry("v2.40") + "## v2.33", 1))
    commit(repo, "a branch that named its own release")
    assert run(repo, "preflight", "--onto", "main") == 2
    err = capsys.readouterr().err
    assert "already has an entry for v2.40" in err
    assert "does not exist at main" in err
    assert "next free number is v2.34" in err, "say what it should have been, not only what is wrong"


def test_re_running_apply_on_a_branch_it_already_stamped_is_still_a_noop(repo, capsys):
    """The reason the refusal above is about the NUMBER and not about who typed it.

    After `apply` runs there is no placeholder left, so a branch it stamped is byte-identical
    to one that hard-coded the same number — nothing in the tree tells them apart. Refusing
    every number above the base would therefore make `apply` refuse its own output, and
    `fix-and-land` runs it unconditionally. The next number is the one legitimate reading.
    """
    place(repo)
    assert run(repo, "apply", "--onto", "main") == 0
    assert "stamped v2.34" in capsys.readouterr().out
    commit(repo, "work, stamped v2.34")

    assert run(repo, "apply", "--onto", "main") == 0
    assert "noop" in capsys.readouterr().out


def test_a_placeholder_beside_a_hand_written_number_is_still_refused(repo, capsys):
    """…and the leniency above is scoped to branches with nothing left to stamp.

    With a placeholder still present the branch has something to stamp AND has already
    written a number down, so stamping would put the number in twice. That holds for the
    next number as much as for any other, which is what keeps the pre-existing refusal.
    """
    place(repo)
    text = (repo / "CHANGELOG.md").read_text()
    write(repo, "CHANGELOG.md", text.replace("## v2.33", entry("v2.34") + "## v2.33", 1))
    assert run(repo, "preflight", "--onto", "main") == 2
    assert "already has an entry for v2.34" in capsys.readouterr().err


def test_a_hand_written_number_also_detects_a_broken_base(repo, capsys):
    """#167's "second effect, same cause": the base check sat below the same early return.

    A branch with no placeholder did not merely skip the ordering check — it also failed to
    notice that `main` carries an unstamped entry, which is the one thing that makes its
    number wrong.
    """
    git(repo, "checkout", "-q", "main")
    place(repo)
    commit(repo, "a release landed on main without being stamped")
    git(repo, "checkout", "-q", "work")

    text = (repo / "CHANGELOG.md").read_text()
    write(repo, "CHANGELOG.md", text.replace("## v2.33", entry("v2.34") + "## v2.33", 1))
    commit(repo, "a branch that named its own release")
    assert run(repo, "preflight", "--onto", "main") == 2
    assert "carries an unstamped" in capsys.readouterr().err


def test_a_broken_base_does_not_hold_a_branch_that_ships_no_release(repo, capsys):
    """#168's blast radius: one skipped stamp must not take out every branch at once.

    The refusal is right for a branch that needs a number and noise for one that does not,
    and `fix-and-land` wires exit 2 straight to a HOLD — so refusing both held every branch
    in the repo over somebody else's mistake, in a file it does not touch. It is told, and
    it carries on.
    """
    git(repo, "checkout", "-q", "main")
    place(repo)
    commit(repo, "a release landed on main without being stamped")
    git(repo, "checkout", "-q", "work")

    write(repo, "docs.md", "# how\n\nA branch that ships no release.\n")
    commit(repo, "docs only")
    assert run(repo, "apply", "--onto", "main") == 0
    out = capsys.readouterr()
    assert "noop" in out.out
    assert "carries an unstamped" in out.err, "silence would leave main broken with nobody told"
    assert "ships no release" in out.err


def test_the_broken_base_refusal_names_a_ref_instead_of_describing_one(repo, capsys):
    """#168: the repair is the opposite of every other invocation of this tool.

    Every normal use passes `--onto origin/main`, and here `origin/main` is the broken thing;
    the old message described how to find a ref that predates the unstamped entry. Someone
    hitting this under time pressure reaches for the usual command, gets the same refusal and
    concludes the tool is stuck — so the ref is resolved and the command printed ready to run.
    """
    git(repo, "checkout", "-q", "main")
    before = git(repo, "rev-parse", "HEAD").strip()
    place(repo)
    commit(repo, "a release landed on main without being stamped")
    git(repo, "checkout", "-q", "work")

    place(repo)
    assert run(repo, "apply", "--onto", "main") == 2
    err = capsys.readouterr().err
    assert "apply --onto" in err
    assert before[:12] in err, f"the resolved ref itself, not prose about finding it: {err}"


def test_the_repair_ref_is_the_last_commit_before_the_placeholder_arrived(repo, capsys):
    """It walks back to a base that is actually clean, not merely to the first parent.

    Two unstamped commits in a row is the realistic shape — a release lands unstamped, work
    carries on top of it — and `HEAD^` there is still broken, so a command built from it
    would fail with the same message it was handed out to fix.
    """
    git(repo, "checkout", "-q", "main")
    clean = git(repo, "rev-parse", "HEAD").strip()
    place(repo)
    commit(repo, "a release landed on main without being stamped")
    write(repo, "after.md", "# work carried on regardless\n")
    commit(repo, "another commit on top of the broken one")
    git(repo, "checkout", "-q", "work")

    place(repo)
    assert run(repo, "apply", "--onto", "main") == 2
    assert clean[:12] in capsys.readouterr().err


def test_a_branch_stamped_as_a_major_is_not_refused_by_a_plain_rerun(repo, capsys):
    """`--major` is a flag and never an inference, and `fix-and-land` runs `apply` without it.

    So a branch stamped `v3` meets the "did somebody pick their own number" check again on the
    next plain run, with `next_release` answering v2.34. Allowing only the minor bump would
    refuse every major release branch on its second run — turning the noop that caller depends
    on into a HOLD.
    """
    place(repo)
    assert run(repo, "apply", "--onto", "main", "--major") == 0
    assert "stamped v3" in capsys.readouterr().out
    commit(repo, "work, stamped v3")

    assert run(repo, "apply", "--onto", "main") == 0
    assert "noop" in capsys.readouterr().out
