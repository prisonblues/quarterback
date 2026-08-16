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


def entry(version: str, body: str = "did a thing.") -> str:
    return f"## {version} — a release\n\n{body}\n\n"


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
    write(repo, "CHANGELOG.md", text.replace("## v2.33", entry("v2.35") + entry("v2.34") + "## v2.33", 1))
    commit(repo, "two more releases")
    git(repo, "checkout", "-q", "work")

    assert run(repo, "apply", "--onto", "main") == 0
    assert "## v2.36 — a release" in (repo / "CHANGELOG.md").read_text()


def test_the_highest_heading_wins_not_the_first(repo):
    """A base ref whose newest entry was inserted a line too low still hands out a free
    number. Reading position 0 would re-issue a number that has already shipped — the tool
    that allocates must not be the one that trusts the ordering it is about to disturb."""
    git(repo, "checkout", "-q", "main")
    text = (repo / "CHANGELOG.md").read_text()
    write(repo, "CHANGELOG.md", text.replace("## v2.33", "## v2.33", 1) + entry("v2.40"))
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
    assert "already has an entry for it" in capsys.readouterr().err


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


def test_two_version_lines_in_pyproject_stop_before_anything_is_written(repo, capsys):
    place(repo)
    write(repo, "pyproject.toml",
          PYPROJECT.format(version="2.33.0") + '\n[tool.other]\nversion = "9.9.9"\n')
    write(repo, "app/routes.py", "# a board change\n")
    assert run(repo, "apply", "--onto", "main") == 2
    assert "expected exactly 1" in capsys.readouterr().err
    assert "## vNEXT — a release" in (repo / "CHANGELOG.md").read_text()


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


def test_check_needs_no_base_ref(repo):
    """Deliberately: the guard runs on an integration branch that may have no upstream
    configured, and a guard that errored on a missing ref would report the same exit code
    as the defect it looks for."""
    assert run(repo, "check") == 0


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
