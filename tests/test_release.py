"""Tests for `scripts/release.py`.

Every test builds a throwaway git repo: the tool's whole subject is "what number is free at
the commit I am about to tag", and a question about a ref cannot be answered by a fixture
string. No database, and nothing here reads the real repo — a suite that asserted about this
checkout's own CHANGELOG would go red on the day somebody landed a release, which is the day
it is needed.

The tests this file exists for are the ones where the tool could be plausibly wrong and
silently so:

  * `test_the_highest_heading_wins_not_the_first` — the file is newest-first and a sibling
    test enforces that, but the tool handing out numbers must not be the one thing trusting
    the ordering it is about to disturb.
  * `test_a_release_cut_on_a_branch_is_refused` and its two siblings — the whole of #122 is
    that there is no place for a branch to write a number. A command that merely advises
    against it is a convention, and this repo has watched five agents follow a document off a
    cliff in one night.
  * `test_the_served_version_is_measured_from_the_previous_release` — measured from anything
    else and a release either ships a bump nobody wrote or silently fails to ship one, and
    the second has no diff anywhere to catch it.
  * the `guard` block — a refusal that does not name `changelog.d/<issue>.<kind>.md` gets
    retried or worked around, and both are worse than the original mistake.
"""

from __future__ import annotations

import errno
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

# `scripts/` is a directory of standalone tools, not an importable package, so the module is
# loaded by path — and registered in sys.modules before it executes, because @dataclass
# resolves annotations through sys.modules[cls.__module__], and because `release.py` loads
# its siblings back by the same name.
_SPEC = importlib.util.spec_from_file_location(
    "release",
    Path(__file__).resolve().parent.parent / "scripts" / "release.py",
)
rs = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = rs
_SPEC.loader.exec_module(rs)

#: The real terminal read, captured before the autouse `no_terminal` fixture can patch over
#: it. Almost every test wants the seam; the two that exercise the terminal ITSELF want the
#: function, and there is nowhere else to get it back from once the fixture has run.
ASK_THE_TERMINAL = rs.ask_the_terminal


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
    lines = ["# quarterback", "", "### Every release, oldest first", ""]
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


def fragment(repo: Path, name: str = "+thing.feat.md", title: str = "a release",
             body: str = "did a thing.") -> None:
    """What a branch writes, and the only thing it writes."""
    write(repo, f"changelog.d/{name}", f"# {title}\n\n{body}\n")


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
    # The release commit is written by `git commit`, which wants a name and an email; with the
    # global config emptied there is nothing for git to fall back on except the hostname, and
    # a runner whose hostname has no dot fails the commit outright.
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")


@pytest.fixture(autouse=True)
def no_terminal(monkeypatch):
    """Every test runs the way an unattended job does, whatever shell pytest was started from.

    Autouse and not opt-in, for a reason that is not tidiness: `--major` reads the CONTROLLING
    terminal, and a developer running this suite from their own shell HAS one. Without this
    the `--major` tests would print a prompt into that terminal and block on it — a suite that
    passes in CI and hangs on a laptop. Pinning the seam shut by default also means a test
    that wants a person has to say so, which is the property being tested.
    """
    def no_such_device(prompt, timeout=None):
        raise OSError("[Errno 6] No such device or address: '/dev/tty'")

    monkeypatch.setattr(rs, "ask_the_terminal", no_such_device)
    monkeypatch.delenv("HARNESS_UNATTENDED", raising=False)


@pytest.fixture
def terminal(no_terminal, monkeypatch):
    """A person at a keyboard. `.answer` is what they type; `.prompts` is what they were shown.

    Depends on `no_terminal` so it patches second and wins, rather than relying on the order
    pytest happens to instantiate two fixtures in.
    """
    class Terminal:
        def __init__(self) -> None:
            self.prompts: list[str] = []
            #: The number the `repo` fixture's major bump asks for: main is at v2.33.
            self.answer = "v3"

        def __call__(self, prompt, timeout=None):
            self.prompts.append(prompt)
            return self.answer

    person = Terminal()
    monkeypatch.setattr(rs, "ask_the_terminal", person)
    return person


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """`main` at v2.33, tagged, level with an `origin` it can push to, holding one fragment.

    A real bare remote rather than a stub: `run` refuses a checkout that is not level with
    `origin/<default>`, and it pushes the commit and the tag it makes. Both are the subject,
    so neither can be mocked out without testing the mock.

    Tags for every release, because the served-version inference measures from the PREVIOUS
    release's tag and refuses where there is none — a checkout with no tags is a real state
    and it has its own test.
    """
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))

    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "remote", "add", "origin", str(origin))
    write(root, "CHANGELOG.md", CHANGELOG_HEAD + entry("v2.33") + entry("v2.32") + entry("v2"))
    write(root, "README.md", readme(["v2", "v2.32", "v2.33"]))
    write(root, "pyproject.toml", PYPROJECT.format(version="2.33.0"))
    write(root, "app/main.py", MAIN_PY.format(version="2.33.0"))
    write(root, "harness/loops/README.md", "# loops\n\nA nested doc.\n")
    commit(root, "v2.33")
    for name in ("v2", "v2.32", "v2.33"):
        git(root, "tag", "-a", name, "-m", name)
    git(root, "push", "-q", "origin", "main")
    git(root, "push", "-q", "origin", "--tags")
    fragment(root)
    commit(root, "feat: a thing")
    git(root, "push", "-q", "origin", "main")
    return root


def run(repo: Path, *argv: str) -> int:
    return rs.main([*argv, "--repo", str(repo)])


def cut(repo: Path, *argv: str) -> int:
    """`run` with a title, which every release past one fragment needs anyway."""
    return run(repo, "run", "--title", "a release", *argv)


def branch(repo: Path, name: str = "work") -> None:
    git(repo, "checkout", "-q", "-b", name)


def land(repo: Path, message: str) -> None:
    """Commit on `main` and push it — what a merged pull request leaves behind.

    `run` refuses a checkout that is not level with its remote, so a fixture that only
    committed would be testing that refusal rather than whatever it meant to.
    """
    commit(repo, message)
    git(repo, "push", "-q", "origin", "main")


def advance_the_integration_branch(repo: Path, *versions: str, name: str = "main") -> str:
    """Land `versions` on a REMOTE-TRACKING copy of the integration branch, leaving the local
    one stale. That is the shape the whole "inherited, not claimed" question is about: `--onto
    main` while `origin/main` has moved on. `update-ref` rather than a second repo, because
    what matters is the ref NAME and where it points, and a clone would add nothing else."""
    here = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    git(repo, "checkout", "-q", "-b", "_advance", name)
    text = (repo / "CHANGELOG.md").read_text()
    added = "".join(entry(v) for v in versions)
    write(repo, "CHANGELOG.md", text.replace("## v2.33", added + "## v2.33", 1))
    commit(repo, f"{', '.join(versions)} landed on {name}")
    sha = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "checkout", "-q", here)
    git(repo, "branch", "-q", "-D", "_advance")
    git(repo, "update-ref", f"refs/remotes/origin/{name}", sha)
    return sha


def shallow_clone_of(repo: Path, into: Path, *, branch: str = "main") -> Path:
    """A REAL depth-1 clone, which is what `actions/checkout@v4` produces with no options.

    Built rather than simulated: the thing under test is what git reports across a graft, and
    a monkeypatched stand-in for that would only ever assert the stand-in. `file://` and not a
    plain path — git treats a local path as an alternate-objects clone and ignores `--depth`
    on it, so a "shallow" fixture built that way is a full one.
    """
    git(into.parent, "clone", "-q", "--depth", "1", "--branch", branch,
        f"file://{repo}", str(into))
    return into


def plan_json(repo: Path, *argv: str, capsys) -> dict:
    assert run(repo, *argv, "--json") == 0
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# the number
# ---------------------------------------------------------------------------


def test_fragments_become_one_numbered_release(repo):
    assert cut(repo, "--no-push") == 0
    changelog = (repo / "CHANGELOG.md").read_text()
    assert changelog.index("## v2.34 — a release") < changelog.index("## v2.33")
    assert "did a thing." in changelog


def test_the_release_is_one_entry_however_many_fragments(repo):
    """A release IS everything since the last one. Six merges in a night are one release, and
    the alternative — one per pull request — puts the title back on a branch."""
    fragment(repo, "301.fix.md", "the second thing", "fixed the second thing.")
    fragment(repo, "302.feat.md", "the third thing", "added the third thing.")
    land(repo, "two more")
    assert cut(repo, "--no-push") == 0

    changelog = (repo / "CHANGELOG.md").read_text()
    assert changelog.count("## v2.34") == 1
    assert changelog.count("## v") == 4  # v2.34, v2.33, v2.32, v2
    for heading in ("### a release", "### the second thing", "### the third thing"):
        assert heading in changelog


def test_the_highest_heading_wins_not_the_first(repo):
    """The file is newest first and a sibling test enforces it, but the tool handing out
    numbers must not be the one thing trusting the ordering it is about to disturb: reading
    position 0 re-issues a live number the moment an entry is inserted a line too low."""
    text = (repo / "CHANGELOG.md").read_text()
    at = text.index("## v2.33")
    write(repo, "CHANGELOG.md", text[:at] + entry("v2.30") + text[at:])
    write(repo, "README.md", readme(["v2", "v2.32", "v2.30", "v2.33"]))
    land(repo, "an entry out of order")

    assert cut(repo, "--no-push") == 0
    assert "## v2.34 — a release" in (repo / "CHANGELOG.md").read_text()


def test_a_major_only_release_still_yields_a_minor(repo):
    """`## v2` has no minor at all — this repo's first two releases are `v1` and `v2` — and a
    parser that required one would refuse the file rather than number on top of it."""
    write(repo, "CHANGELOG.md", CHANGELOG_HEAD + entry("v2"))
    write(repo, "README.md", readme(["v2"]))
    land(repo, "back to v2")
    # And the tags with them: headings say what LANDED, tags say what was ISSUED, and a tag
    # for a release the file no longer declares would fold into the same max and win.
    for name in ("v2.32", "v2.33"):
        git(repo, "tag", "-d", name)

    assert cut(repo, "--no-push") == 0
    assert "## v2.1 — a release" in (repo / "CHANGELOG.md").read_text()


def test_a_number_a_tag_already_holds_is_not_handed_out_again(repo):
    """Headings say what has LANDED; tags say what has been ISSUED. A release cut in another
    checkout and not yet pushed is a tag and no heading, and folding the two into one `max` is
    what stops the next cut re-issuing it."""
    git(repo, "tag", "-a", "v2.34", "-m", "cut elsewhere", "HEAD")
    assert cut(repo, "--no-push") == 0
    assert "## v2.35 — a release" in (repo / "CHANGELOG.md").read_text()


def test_nothing_to_release_is_a_noop_not_a_failure(repo, capsys):
    """Most of the time there is nothing in `changelog.d/` and asking is free."""
    (repo / "changelog.d/+thing.feat.md").unlink()
    land(repo, "consume the fragment")

    assert cut(repo) == 0
    out = capsys.readouterr().out
    assert "nothing to release" in out
    assert "changelog.d/<issue>.<kind>.md" in out


def test_a_changelog_declaring_a_number_twice_is_a_stop(repo, capsys):
    """The state a "keep both sides" merge resolution leaves behind: every heading present,
    unique-looking and correctly ordered, and one number describing two releases. Numbering on
    top of it would hand out a number the file cannot tell you is free."""
    text = (repo / "CHANGELOG.md").read_text()
    write(repo, "CHANGELOG.md", text.replace("## v2.32", "## v2.33", 1))
    land(repo, "a bad resolution")

    assert cut(repo) == 2
    assert "declares v2.33 more than once" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# preview, which decides nothing
# ---------------------------------------------------------------------------


def test_preview_changes_nothing(repo, capsys):
    before = {p: p.read_text() for p in repo.rglob("*.md")}
    assert run(repo, "preview", "--title", "a release") == 0
    assert {p: p.read_text() for p in repo.rglob("*.md")} == before
    assert "would issue v2.34 — a release" in capsys.readouterr().out


def test_preview_answers_from_a_branch_and_from_a_dirty_tree(repo, capsys):
    """It is a question, not an act. Gating it on the branch would mean the one command a
    worker can safely run is the one it cannot run where it is standing."""
    branch(repo)
    write(repo, "notes.md", "uncommitted\n")
    assert run(repo, "preview", "--title", "a release") == 0
    assert "would issue v2.34" in capsys.readouterr().out


def test_preview_reports_the_cut_as_json(repo, capsys):
    plan = plan_json(repo, "preview", "--title", "a release", capsys=capsys)
    assert plan["cutting"] is True
    assert plan["version"] == "v2.34"
    assert plan["previous"] == "v2.33"
    assert plan["fragments"] == ["+thing.feat.md"]


# ---------------------------------------------------------------------------
# there is no place to do it — the whole of #122
#
# `apply` used to run on a branch, every brief in the repo told a worker to run it, and every
# worker did, correctly, as instructed. Three of six open pull requests were CONFLICTING that
# night and the three that had written no release entry all merged clean. A rule against a
# runnable command is a convention; a command that refuses is a mechanism (#85).
# ---------------------------------------------------------------------------


def test_a_release_cut_on_a_branch_is_refused(repo, capsys):
    branch(repo)
    before = (repo / "CHANGELOG.md").read_text()

    assert cut(repo) == 2

    assert (repo / "CHANGELOG.md").read_text() == before
    err = capsys.readouterr().err
    assert "a release is cut on `main`, and this checkout is on `work`" in err
    # The refusal has to name the way FORWARD, or the next agent works around it.
    assert "changelog.d/<issue>.<kind>.md" in err


def test_a_detached_head_is_refused_by_name_rather_than_by_a_blank(repo, capsys):
    git(repo, "checkout", "-q", "--detach")
    assert cut(repo) == 2
    assert "this checkout is on a detached HEAD" in capsys.readouterr().err


def test_a_dirty_tree_is_refused_because_the_commit_would_carry_it(repo, capsys):
    write(repo, "app/routes.py", "# work in progress\n")
    git(repo, "add", "-A")
    assert cut(repo) == 2
    err = capsys.readouterr().err
    assert "has uncommitted changes" in err and "app/routes.py" in err


def test_a_checkout_behind_its_remote_is_refused(repo, capsys, tmp_path):
    """The number is `max(headings) + 1`, so a checkout missing a merge reads a stale max and
    issues a number that merge is going to want. This is the collision, at its only remaining
    door."""
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(repo / ".." / "origin.git"), str(other))
    git(other, "config", "user.email", "t@example.com")
    git(other, "config", "user.name", "t")
    write(other, "CHANGELOG.md", CHANGELOG_HEAD + entry("v2.34") + entry("v2.33"))
    write(other, "README.md", readme(["v2.33", "v2.34"]))
    commit(other, "somebody else's release")
    git(other, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q", "origin")

    assert cut(repo) == 2
    err = capsys.readouterr().err
    assert "is behind `origin/main`" in err
    assert "git pull --ff-only" in err


def test_a_checkout_with_no_remote_ref_is_refused_rather_than_guessing(repo, capsys):
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    assert cut(repo) == 2
    assert "there is no `origin/main` in this checkout" in capsys.readouterr().err


def test_the_branch_a_release_is_cut_on_is_read_from_config_not_assumed(repo, capsys):
    """`qb.baseBranch` is the same knob the pre-push hook reads, so a repo that renamed its
    integration branch says so once."""
    git(repo, "config", "qb.baseBranch", "trunk")
    assert cut(repo) == 2
    assert "a release is cut on `trunk`" in capsys.readouterr().err


def test_there_is_no_apply_or_stamp_subcommand_left(repo, capsys):
    """A stale brief gets `invalid choice`, which is the loudest thing a removal can say."""
    for gone in ("apply", "preflight", "stamp", "check", "collision"):
        with pytest.raises(SystemExit):
            run(repo, gone)
        assert "invalid choice" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# what `run` writes, commits and tags
# ---------------------------------------------------------------------------


def test_the_readme_gets_a_bullet_in_changelog_order(repo):
    assert cut(repo, "--no-push") == 0
    bullets = re.findall(r"^- \*\*(v[\d.]+)\*\*", (repo / "README.md").read_text(), re.M)
    assert bullets == ["v2", "v2.32", "v2.33", "v2.34"]


def test_the_fragments_it_consumed_are_deleted(repo):
    assert cut(repo, "--no-push") == 0
    assert not (repo / "changelog.d/+thing.feat.md").exists()
    assert git(repo, "status", "--porcelain").strip() == ""


def test_the_commit_and_the_tag_name_the_same_release(repo):
    assert cut(repo, "--no-push") == 0
    assert git(repo, "log", "-1", "--format=%s").strip() == "chore(release): v2.34 — a release"
    tagged = git(repo, "rev-list", "-n1", "v2.34").strip()
    assert tagged == git(repo, "rev-parse", "HEAD").strip()


def test_the_tag_is_on_the_integration_branch_by_construction(repo):
    """#406, removed rather than detected. The tag used to be reserved at push time against a
    branch-side `chore(release)` commit, which a squash merge then discarded — leaving
    `refs/tags/v3.8` pointing at a commit that is not an ancestor of main. There is no
    branch-side commit any more, so there is nothing for a rewrite to lose."""
    assert cut(repo, "--no-push") == 0
    assert rs._git_ok(repo, "merge-base", "--is-ancestor", "v2.34", "main")


def test_the_release_reaches_the_remote_commit_and_tag_together(repo, capsys):
    assert cut(repo) == 0
    origin = repo.parent / "origin.git"
    assert "## v2.34" in git(origin, "show", "main:CHANGELOG.md")
    assert git(origin, "rev-list", "-n1", "v2.34").strip() == git(repo, "rev-parse", "HEAD").strip()
    assert "pushed to origin/main" in capsys.readouterr().out


def test_no_commit_writes_the_files_and_stops(repo, capsys):
    assert cut(repo, "--no-commit") == 0
    assert "## v2.34" in (repo / "CHANGELOG.md").read_text()
    assert git(repo, "log", "-1", "--format=%s").strip() == "feat: a thing"
    assert "nothing was committed or tagged" in capsys.readouterr().out


def test_a_commit_that_fails_leaves_neither_the_files_nor_the_fragments_consumed(
        repo, capsys, monkeypatch):
    """The window Codex found. `_write_all` puts back the files IT wrote and stops there, so a
    failure while consuming the fragments, or at `git commit`, left the release written and
    its fragments gone — a finished-looking release nobody can find, which is harder to notice
    than either half alone."""
    before = {p: p.read_text() for p in (repo / "CHANGELOG.md", repo / "README.md")}
    real = rs._git

    def fail_on_commit(repo_, *args):
        if args and args[0] == "commit":
            raise rs.ReleaseError("the commit hook said no")
        return real(repo_, *args)

    monkeypatch.setattr(rs, "_git", fail_on_commit)
    assert cut(repo) == 2

    assert "the commit hook said no" in capsys.readouterr().err
    assert (repo / "changelog.d/+thing.feat.md").exists(), "the fragment was consumed anyway"
    assert {p: p.read_text() for p in before} == before
    assert git(repo, "status", "--porcelain").strip() == "", "and the index was left staged"


def test_the_commit_and_its_tag_are_pushed_atomically(repo, monkeypatch):
    """Pushed separately, a tag push that failed left the release on `main` untagged — and the
    job that would normally repair that does not run, because a push made with `GITHUB_TOKEN`
    triggers no workflows (Codex). One push, all or nothing."""
    pushes: list[tuple[str, ...]] = []
    real = rs._git

    def record(repo_, *args):
        if args and args[0] == "push":
            pushes.append(args)
        return real(repo_, *args)

    monkeypatch.setattr(rs, "_git", record)
    assert cut(repo) == 0

    assert len(pushes) == 1, f"the release was pushed in {len(pushes)} goes: {pushes}"
    assert "--atomic" in pushes[0]
    assert any(a.endswith(":refs/heads/main") for a in pushes[0])
    assert "refs/tags/v2.34" in pushes[0]


def test_a_release_cannot_rewrite_an_entry_that_already_shipped(repo, capsys, monkeypatch):
    """`guard` refuses a branch that touches CHANGELOG.md at all, which leaves exactly one
    writer — and an unwatched sole writer is how #325 happened. So the release job is held to
    the rule it enforces: it may append an entry and it may not alter one."""
    def clobber(changelog, text, where="CHANGELOG.md"):
        return changelog.replace("did a thing.", "a different body.", 1)

    monkeypatch.setattr(sys.modules["changelog_fragments"], "insert_entry", clobber)
    assert cut(repo, "--no-push") == 2
    err = capsys.readouterr().err
    assert "would rewrite the text of v2.33" in err and "Nothing was written" in err


# ---------------------------------------------------------------------------
# `guard` — the consolidated files are OUTPUT
#
# Decision (a) on #122: `CHANGELOG.md` stays in git so `git log CHANGELOG.md` keeps working
# and a reader offline keeps the history, and the guard rather than the file's absence is what
# removes the affordance — nothing stops a branch creating a file that does not exist.
# ---------------------------------------------------------------------------


def test_guard_refuses_a_branch_that_edits_the_changelog(repo, capsys):
    branch(repo)
    text = (repo / "CHANGELOG.md").read_text()
    write(repo, "CHANGELOG.md", CHANGELOG_HEAD + entry("v2.34") + text[len(CHANGELOG_HEAD):])
    commit(repo, "a release entry on a branch")

    assert run(repo, "guard", "--onto", "main", "--branch", "HEAD") == 2

    err = capsys.readouterr().err
    assert "edits CHANGELOG.md" in err
    assert "changelog.d/<issue>.<kind>.md" in err
    assert "git checkout" in err  # and how to undo what is already committed


def test_guard_refuses_a_release_entry_written_above_the_ones_it_can_parse(repo, capsys):
    """Codex found the hole. `## v9.9.9` is three components, which the release-heading
    pattern deliberately does not match — so anchoring the guarded region on the first
    PARSEABLE heading let a branch prepend a release entry and have the whole thing read as
    preamble. The region starts at the first `##` of any kind, which is the same boundary for
    a correct file and a closed door for that one."""
    branch(repo)
    text = (repo / "CHANGELOG.md").read_text()
    at = text.index("## v2.33")
    write(repo, "CHANGELOG.md",
          text[:at] + "## v9.9.9 — what this branch shipped\n\nSomething.\n\n" + text[at:])
    commit(repo, "a release entry the parser does not recognise")

    assert run(repo, "guard", "--onto", "main", "--branch", "HEAD") == 2
    assert "edits CHANGELOG.md" in capsys.readouterr().err


def test_guard_refuses_a_branch_that_removed_the_readmes_release_list(repo, capsys):
    """The other half Codex found: a release list that stops PARSING on the branch used to
    read as "cannot tell" and pass, which made deleting the block the one edit that got
    through — the largest version of the defect wearing the smallest diff."""
    branch(repo)
    text = (repo / "README.md").read_text()
    write(repo, "README.md", text[:text.index("### Every release, oldest first")])
    commit(repo, "docs: drop the release list")

    assert run(repo, "guard", "--onto", "main", "--branch", "HEAD") == 2
    assert "README.md § Releases" in capsys.readouterr().err


def test_guard_refuses_a_branch_that_edits_the_readmes_release_list(repo, capsys):
    """The expensive half of every landing conflict. A bullet appended at the end of the block
    by two branches at once is the same insertion-at-one-offset that made CHANGELOG.md
    conflict."""
    branch(repo)
    text = (repo / "README.md").read_text()
    write(repo, "README.md", text + "- **v2.34** — a release.\n")
    commit(repo, "a release bullet on a branch")

    assert run(repo, "guard", "--onto", "main", "--branch", "HEAD") == 2
    assert "README.md § Releases" in capsys.readouterr().err


def test_guard_lets_a_branch_edit_the_rest_of_the_readme(repo, capsys):
    """The README is nine hundred lines of prose a branch is meant to edit. Refusing the whole
    file would make the guard a tax on documentation, which is how a guard stops being
    installed."""
    branch(repo)
    write(repo, "README.md", (repo / "README.md").read_text() + "\n## A new section\n\nProse.\n")
    commit(repo, "docs: say more")

    assert run(repo, "guard", "--onto", "main", "--branch", "HEAD") == 0
    assert "no generated release file edited" in capsys.readouterr().out


def test_guard_passes_a_branch_that_only_writes_a_fragment(repo, capsys):
    branch(repo)
    fragment(repo, "404.fix.md", "a fix", "fixed it.")
    commit(repo, "fix: a thing")

    assert run(repo, "guard", "--onto", "main", "--branch", "HEAD") == 0


def test_guard_passes_a_branch_that_is_merely_behind_a_release(repo, capsys):
    """Fork-relative, which is what lets this run on every push. A branch open while a release
    was cut has inherited the whole entry and has touched nothing; judged against `main`
    itself, every such branch would be refused and the gate switched off within a week."""
    branch(repo)
    write(repo, "app/routes.py", "# work\n")
    commit(repo, "feat: work")
    fork = git(repo, "rev-parse", "HEAD").strip()

    git(repo, "checkout", "-q", "main")
    assert cut(repo, "--no-push") == 0
    git(repo, "checkout", "-q", "work")

    assert run(repo, "guard", "--onto", "main", "--branch", fork) == 0


def test_guard_lets_a_branch_edit_the_changelogs_preamble(repo, capsys):
    """The file's own convention paragraph, which is edited when the convention changes —
    this release edits it. It is the same line `frozen` draws, and drawing it differently in
    two places would mean one edit refused by one guard and cleared by the other."""
    branch(repo)
    text = (repo / "CHANGELOG.md").read_text()
    write(repo, "CHANGELOG.md",
          text.replace("Entries are newest first.",
                       "Entries are newest first. A branch writes changelog.d/ instead."))
    commit(repo, "docs: say where a branch writes")

    assert run(repo, "guard", "--onto", "main", "--branch", "HEAD") == 0


def test_guard_refuses_a_branch_that_deleted_the_changelog_outright(repo, capsys):
    """The largest version of the defect, and it must not be the one edit that passes. An
    empty entries region differs from the base's like any other edit."""
    branch(repo)
    (repo / "CHANGELOG.md").unlink()
    commit(repo, "chore: drop the changelog")

    assert run(repo, "guard", "--onto", "main", "--branch", "HEAD") == 2
    assert "edits CHANGELOG.md" in capsys.readouterr().err


def test_guard_says_it_read_nothing_rather_than_reporting_a_clean_bill(repo, capsys):
    """A depth-1 CI checkout has no fork point at all, and a check that reports green while
    comparing nothing is the shape of a gate that verifies nothing."""
    assert run(repo, "guard", "--onto", "main", "--branch", "HEAD") == 0
    assert "limited:" in capsys.readouterr().err


def test_guard_reports_its_verdict_as_json(repo, capsys):
    branch(repo)
    write(repo, "CHANGELOG.md", CHANGELOG_HEAD + entry("v2.34"))
    commit(repo, "a release entry on a branch")

    assert run(repo, "guard", "--onto", "main", "--branch", "HEAD", "--json") == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["edited"] == ["CHANGELOG.md"]


def test_guard_says_which_ref_it_cannot_find(repo, capsys):
    assert run(repo, "guard", "--onto", "no/such/ref") == 2
    assert "no/such/ref" in capsys.readouterr().err
# ---------------------------------------------------------------------------
# the served version
# ---------------------------------------------------------------------------


def test_a_board_change_bumps_the_served_version(repo):
    write(repo, "app/routes.py", "# a board change\n")
    land(repo, "feat: a board change")
    assert cut(repo, "--no-push") == 0
    assert 'version = "2.34.0"' in (repo / "pyproject.toml").read_text()
    assert 'version="2.34.0"' in (repo / "app/main.py").read_text()


def test_a_migration_bumps_the_served_version(repo):
    write(repo, "migrations/versions/0020_thing.py", "revision = '0020'\n")
    land(repo, "feat: a migration")
    assert cut(repo, "--no-push") == 0
    assert 'version = "2.34.0"' in (repo / "pyproject.toml").read_text()


def test_a_harness_only_release_leaves_the_served_version_alone(repo):
    """Most releases here are harness-side and correctly leave it where it was — v2.16,
    v2.17, v2.18, v2.20, v2.21 and v2.32 all did. A check that bumped anyway would be wrong
    every second release and switched off within a week."""
    write(repo, "harness/loops/panel.py", "# a harness change\n")
    land(repo, "feat: a harness change")
    assert cut(repo, "--no-push") == 0
    assert 'version = "2.33.0"' in (repo / "pyproject.toml").read_text()
    assert 'version="2.33.0"' in (repo / "app/main.py").read_text()


def test_the_served_version_is_measured_from_the_previous_release(repo):
    """From the previous release's TAG, not from HEAD and not from a branch's fork point.

    Measured from anything else the release either ships a bump nobody wrote or silently
    fails to ship one — and the second has no diff anywhere to catch it: the board would
    report the release before it from `GET /openapi.json` and nothing would say so.
    """
    write(repo, "app/routes.py", "# a board change, three merges ago\n")
    land(repo, "feat: a board change")
    write(repo, "harness/loops/panel.py", "# and then two that ship nothing\n")
    land(repo, "chore: harness")
    write(repo, "docs/notes.md", "# prose\n")
    land(repo, "docs: notes")

    assert cut(repo, "--no-push") == 0
    assert 'version = "2.34.0"' in (repo / "pyproject.toml").read_text()


def test_a_previous_release_with_no_tag_is_a_stop_rather_than_a_quiet_no(repo, capsys):
    """An inference that cannot run is not an inference that said no. Without the previous
    release's tag there is no ref to measure against, and reporting `serves: false` would ship
    a board whose served version silently stayed where it was."""
    git(repo, "tag", "-d", "v2.33")
    assert cut(repo) == 2
    err = capsys.readouterr().err
    assert "has no tag in this checkout" in err
    assert "--serve / --no-serve" in err


def test_no_serve_overrides_the_inference(repo):
    write(repo, "app/routes.py", "# a board change that ships no behaviour\n")
    land(repo, "feat: a board change")
    assert cut(repo, "--no-push", "--no-serve") == 0
    assert 'version = "2.33.0"' in (repo / "pyproject.toml").read_text()


def test_serve_overrides_the_inference(repo):
    assert cut(repo, "--no-push", "--serve") == 0
    assert 'version="2.34.0"' in (repo / "app/main.py").read_text()


def test_a_version_that_left_the_fastapi_call_is_a_stop(repo, capsys):
    """The regex is coupled to an inline literal and says so. Failing loudly is the honest
    outcome; the alternative is latching onto the next version-shaped string in the file and
    bumping something that is not what the app serves."""
    write(repo, "app/main.py", "app = FastAPI(title='quarterback', version=VERSION)\n")
    write(repo, "app/routes.py", "# a board change\n")
    land(repo, "the change this release ships")
    assert cut(repo, "--no-push") == 2
    assert "no `app = FastAPI(" in capsys.readouterr().err


def test_an_unbumpable_version_stops_before_anything_is_written(repo):
    """A half-applied release is worse than either outcome, and is the state hardest to
    notice: the markdown reads as a finished release and the served version disagrees with
    it. So both version sites are validated before the first byte is written."""
    write(repo, "app/main.py", "app = FastAPI(title='quarterback', version=VERSION)\n")
    write(repo, "app/routes.py", "# a board change\n")
    land(repo, "the change this release ships")
    before = (repo / "CHANGELOG.md").read_text(), (repo / "README.md").read_text()
    assert cut(repo, "--no-push") == 2
    assert ((repo / "CHANGELOG.md").read_text(), (repo / "README.md").read_text()) == before
    assert (repo / "changelog.d/+thing.feat.md").exists()


def test_a_version_in_another_toml_table_is_not_the_package_version(repo):
    """`[tool.something].version` is somebody else's field. A file-wide search for a
    `version = "X.Y.Z"` line finds whichever table happens to have one — and the day
    `[project]` stops having one, that search does not report the absence, it reports the
    other table and bumps it, successfully and wrongly."""
    write(repo, "pyproject.toml",
          PYPROJECT.format(version="2.33.0") + '\n[tool.other]\nversion = "9.9.9"\n')
    write(repo, "app/routes.py", "# a board change\n")
    land(repo, "the change this release ships")
    assert cut(repo, "--no-push") == 0
    text = (repo / "pyproject.toml").read_text()
    assert 'version = "2.34.0"' in text
    assert 'version = "9.9.9"' in text  # untouched


def test_two_version_lines_in_project_stop_before_anything_is_written(repo, capsys):
    write(repo, "pyproject.toml",
          PYPROJECT.format(version="2.33.0") + 'version = "9.9.9"\n')
    write(repo, "app/routes.py", "# a board change\n")
    land(repo, "the change this release ships")
    assert cut(repo, "--no-push") == 2
    assert "expected exactly 1" in capsys.readouterr().err
    assert "## v2.34" not in (repo / "CHANGELOG.md").read_text()


def test_a_pyproject_with_no_project_table_is_a_stop(repo, capsys):
    """Said as "no `[project]` table" rather than as "0 version lines". The tool's whole
    ergonomic argument is that a refusal carries the sentence that repairs it, and "0 lines,
    expected exactly 1" sends a reader looking for a line that was never the problem."""
    write(repo, "pyproject.toml", '[tool.ruff]\nversion = "9.9.9"\n')
    write(repo, "app/routes.py", "# a board change\n")
    land(repo, "the change this release ships")
    assert cut(repo, "--no-push") == 2
    assert "no `[project]` table" in capsys.readouterr().err


def test_a_missing_pyproject_says_so_rather_than_counting_its_lines(repo, capsys):
    """"pyproject.toml has 0 version lines" about a file that does not exist is a sentence
    that sends the reader to look at a file they will not find."""
    (repo / "pyproject.toml").unlink()
    write(repo, "app/routes.py", "# a board change\n")
    land(repo, "the change this release ships")
    assert cut(repo, "--no-push") == 2
    assert "pyproject.toml does not exist" in capsys.readouterr().err


def test_a_missing_main_py_says_so_rather_than_blaming_the_regex(repo, capsys):
    (repo / "app" / "main.py").unlink()
    write(repo, "app/routes.py", "# a board change\n")
    land(repo, "the change this release ships")
    assert cut(repo, "--no-push") == 2
    assert "app/main.py does not exist" in capsys.readouterr().err


def test_a_version_inside_another_argument_is_not_the_served_version(repo, capsys):
    """`description="… version=\\"1.0.0\\" …"` is prose about a version, not the keyword
    argument. Matching it would rewrite a docstring, leave the real served version where it
    was, and report success — the one outcome worse than refusing, because nothing looks
    wrong afterwards. Quoted strings are atoms here, so the scan cannot reach inside one."""
    write(repo, "app/main.py",
          'from fastapi import FastAPI\n\n'
          'app = FastAPI(description="written when version=\'1.0.0\' shipped", '
          'version="2.33.0")\n')
    write(repo, "app/routes.py", "# a board change\n")
    land(repo, "the change this release ships")
    assert cut(repo, "--no-push") == 0
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
    write(repo, "app/routes.py", "# a board change\n")
    land(repo, "the change this release ships")
    assert cut(repo, "--no-push") == 2
    assert "pyproject.toml is a symlink" in capsys.readouterr().err
    assert 'version = "2.33.0"' in outside.read_text()


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
        raise rs.ReleaseError(f"cannot write {what}: No space left on device")

    monkeypatch.setattr(rs, "_write", truncate_then_fail)
    with pytest.raises(rs.ReleaseError) as e:
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
            raise rs.ReleaseError(f"cannot write {what}: Input/output error")
        real_write(path, text, what)

    monkeypatch.setattr(rs, "_write", fail_on_the_second)
    with pytest.raises(rs.ReleaseError) as e:
        rs._write_all([("first.md", first, "stamped\n"), ("second.md", second, "stamped\n")])

    message = str(e.value)
    assert "rolling back left" in message and "first.md" in message
    assert "half written" in message
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
    write(repo, "app/main.py", MAIN_PY_WRAPPED.format(version="2.33.0"))
    write(repo, "app/routes.py", "# a board change\n")
    land(repo, "the change this release ships")
    assert cut(repo, "--no-push") == 0
    assert 'version="2.34.0",' in (repo / "app" / "main.py").read_text()


def test_a_version_first_in_a_multi_line_call_is_bumped(repo):
    """`version` on the line straight after the paren, with no comma before it — the optional
    "everything up to a comma" group has to be skippable across a newline, not only against
    the paren itself."""
    write(repo, "app/main.py",
          'from fastapi import FastAPI\n\napp = FastAPI(\n    version="2.33.0",\n'
          '    title="quarterback",\n)\n')
    write(repo, "app/routes.py", "# a board change\n")
    land(repo, "the change this release ships")
    assert cut(repo, "--no-push") == 0
    assert 'version="2.34.0",' in (repo / "app" / "main.py").read_text()


def test_an_escaped_quote_in_a_title_does_not_desynchronise_the_scan(repo):
    r"""`title="ends\", version=\"8.8.8\" here"` is ONE string. A quoted-string atom with no
    escape handling ends at the escaped quote, so the atom boundary and the real string
    boundary come apart and everything after them is read as the wrong kind of thing — here
    the whole call stopped matching, and the tool refused a file whose version literal was
    plainly present. The same desynchronisation the other way round reads text inside the
    literal as a real keyword argument."""
    write(repo, "app/main.py",
          'from fastapi import FastAPI\n\n'
          'app = FastAPI(title="ends\\", version=\\"8.8.8\\" here", version="2.33.0")\n')
    write(repo, "app/routes.py", "# a board change\n")
    land(repo, "the change this release ships")
    assert cut(repo, "--no-push") == 0
    text = (repo / "app" / "main.py").read_text()
    assert 'version="2.34.0")' in text
    assert 'version=\\"8.8.8\\"' in text  # the prose inside the literal is untouched


def test_a_single_quoted_package_version_is_found(repo):
    """TOML gives basic and literal strings equal standing. A `version = '2.33.0'` was
    invisible to the line matcher, so the tool reported "0 version lines in [project]" about a
    file whose version is present, correct, and spelled the other legal way."""
    write(repo, "pyproject.toml",
          "[project]\nname = \"quarterback\"\nversion = '2.33.0'\n")
    write(repo, "app/routes.py", "# a board change\n")
    land(repo, "the change this release ships")
    assert cut(repo, "--no-push") == 0
    assert "version = '2.34.0'" in (repo / "pyproject.toml").read_text()


def test_a_bracketed_continuation_line_does_not_end_the_project_table(repo):
    """`^[ \\t]*\\[` matches a wrapped array element that happens to start with `[`, which cut
    the `[project]` span short — and the user got "0 version lines in [project]" about a file
    whose version sits two lines below the truncation."""
    write(repo, "pyproject.toml",
          '[project]\nname = "quarterback"\nmatrix = [\n  ["a", "b"]\n]\n'
          'version = "2.33.0"\n')
    write(repo, "app/routes.py", "# a board change\n")
    land(repo, "the change this release ships")
    assert cut(repo, "--no-push") == 0
    assert 'version = "2.34.0"' in (repo / "pyproject.toml").read_text()


def test_a_version_inside_a_multiline_toml_string_is_not_the_package_version(repo, capsys):
    """A regex over raw text cannot see that a `[project]`-looking line is inside a multi-line
    string, so the tool could rewrite prose in a `description` and report success. `tomllib`
    can see it, so the two answers are required to agree — and when they do not, this refuses
    rather than picking one."""
    write(repo, "pyproject.toml",
          '[project]\nname = "quarterback"\ndescription = """\n'
          '[project]\nversion = "9.9.9"\n"""\nversion = "2.33.0"\n')
    write(repo, "app/routes.py", "# a board change\n")
    land(repo, "the change this release ships")
    assert cut(repo, "--no-push") == 2
    assert "will not guess which text is the package version" in capsys.readouterr().err
    assert 'version = "9.9.9"' in (repo / "pyproject.toml").read_text()  # untouched


def test_a_pyproject_that_is_not_toml_is_a_stop_not_a_traceback(repo, capsys):
    write(repo, "pyproject.toml", '[project]\nname = "quarterback\nversion = "2.33.0"\n')
    write(repo, "app/routes.py", "# a board change\n")
    land(repo, "the change this release ships")
    assert cut(repo, "--no-push") == 2
    assert "not valid TOML" in capsys.readouterr().err


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
    with pytest.raises(rs.ReleaseError) as e:
        rs.mask_code("# doc\n\n```md\n## vNEXT — an example\n\nand on it goes\n", "docs.md")
    assert "never closes it" in str(e.value) and "line 3" in str(e.value)


def test_an_unterminated_fence_in_the_changelog_is_a_stop_not_a_traceback(repo, capsys):
    """Only the two generated files are read now, and this is the one that decides the number.
    A fence left open in it pairs with the next backtick anywhere below and blanks everything
    between — including, on a bad day, the highest release heading in the file."""
    write(repo, "CHANGELOG.md",
          CHANGELOG_HEAD + "```md\n## an example\n" + entry("v2.33") + entry("v2"))
    land(repo, "a changelog with a fence left open")
    assert cut(repo, "--no-push") == 2
    assert "never closes it" in capsys.readouterr().err
    assert "## v2.34" not in (repo / "CHANGELOG.md").read_text()


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
# who may declare a major (#386)
#
# The number is a reading of a ref and `--major` is not: it is a statement about what the
# release MEANS, which this file's subject has said since it was written and which nothing
# enforced. v2.99 -> v3 happened because "v2.99, v3.00, v3.01" in a prompt reads as a
# sequence, `major.minor` is two integers rather than a decimal, and the flag was available
# to whoever typed it. These are the tests that it now refuses instead of advising.
# ---------------------------------------------------------------------------


def test_a_major_is_refused_where_there_is_no_terminal_to_ask(repo, capsys):
    """The whole issue in one test. `--major` used to be a flag anything could pass."""
    before = (repo / "CHANGELOG.md").read_text()

    assert cut(repo, "--no-push", "--major") == 2

    assert (repo / "CHANGELOG.md").read_text() == before
    assert "## v3" not in before  # nothing was half-written on the way out
    err = capsys.readouterr().err
    assert "STOP: --major would issue v3 instead of v2.34" in err
    # The refusal has to name the way FORWARD, or the next agent works around it.
    assert "dropping --major" in err and "your own keyboard" in err


def test_a_person_who_types_the_number_gets_the_major(repo, terminal, capsys):
    assert cut(repo, "--no-push", "--major") == 0
    assert "## v3 — a release" in (repo / "CHANGELOG.md").read_text()
    assert "confirmed at the terminal: v3, not v2.34" in capsys.readouterr().err


def test_the_prompt_names_the_number_it_is_NOT(repo, terminal):
    """The line that would have caught this one. A major is invisible in a prompt — "v2.99,
    v3.00, v3.01" reads as a sequence — and unmissable in a sentence naming the alternative."""
    assert cut(repo, "--no-push", "--major") == 0

    asked = terminal.prompts[0]
    assert "v3, NOT v2.34" in asked
    assert "two integers" in asked and "decimal" in asked
    assert "Type v3 to confirm" in asked


def test_typing_anything_else_aborts_and_writes_nothing(repo, terminal, capsys):
    """`y` would answer "did you mean to pass the flag", and the flag was never the mistake."""
    terminal.answer = "y"
    before = (repo / "CHANGELOG.md").read_text()

    assert cut(repo, "--no-push", "--major") == 2

    assert (repo / "CHANGELOG.md").read_text() == before
    assert "it asked for v3 at the terminal and read 'y'" in capsys.readouterr().err


def test_the_answer_may_be_typed_without_the_v(repo, terminal):
    terminal.answer = "3"
    assert cut(repo, "--no-push", "--major") == 0
    assert "## v3 — a release" in (repo / "CHANGELOG.md").read_text()


@pytest.mark.parametrize("typed", ["vvv3", "vV3", "v3.0", "v30", "3.0", "V3", ""])
def test_only_the_two_spellings_the_prompt_names_confirm(repo, terminal, typed):
    """`answer.lstrip("vV")` would have taken `vvvV3`, because lstrip strips a prefix of any
    length rather than one character. The prompt says "type v3", so v3 and 3 are the answers."""
    terminal.answer = typed
    assert cut(repo, "--no-push", "--major") == 2
    assert "## v3" not in (repo / "CHANGELOG.md").read_text()


def test_an_unattended_run_is_refused_before_the_terminal_is_even_asked(
    repo, terminal, monkeypatch, capsys
):
    """A controlling terminal is not proof of a person: this repo runs its loops in tmux
    panes, and a pane has a tty whether or not anybody is watching it. `HARNESS_UNATTENDED`
    is the harness's own word for that, so it is read before anything is printed — a prompt
    into an unwatched pane is a wedged loop, which is worse than the refusal."""
    monkeypatch.setenv("HARNESS_UNATTENDED", "1")

    assert cut(repo, "--no-push", "--major") == 2

    assert terminal.prompts == []
    assert "HARNESS_UNATTENDED=1" in capsys.readouterr().err
    assert "## v3" not in (repo / "CHANGELOG.md").read_text()


def test_preview_answers_the_question_without_asking_it(repo, capsys):
    """`preview` has to work where `run` refuses. Asking what `--major` WOULD do decides
    nothing, and the answer is exactly what a person needs in front of them before saying
    yes — so the read-only path is not gated, and the line naming the minor it is NOT is the
    sentence that makes the slip visible."""
    assert run(repo, "preview", "--title", "a release", "--major") == 0

    out = capsys.readouterr().out
    assert "would issue v3 — a release  (--major, NOT v2.34)" in out
    assert "## v3" not in (repo / "CHANGELOG.md").read_text()


def test_the_plan_names_the_number_the_major_is_not(repo, capsys):
    plan = plan_json(repo, "preview", "--title", "a release", "--major", capsys=capsys)
    assert plan["version"] == "v3" and plan["instead_of"] == "v2.34"
    plan = plan_json(repo, "preview", "--title", "a release", capsys=capsys)
    assert plan["version"] == "v2.34" and plan["instead_of"] is None


def test_a_release_with_no_fragments_is_never_asked(repo, terminal):
    """`--major` where there is nothing to release decides nothing, and a gate that fires
    where there is no decision is a gate people learn to get past."""
    (repo / "changelog.d/+thing.feat.md").unlink()
    land(repo, "consume the fragment")
    assert cut(repo, "--no-push", "--major") == 0
    assert terminal.prompts == []


def test_major_confirm_is_the_unattended_door_and_it_wants_the_number(repo, capsys):
    """The release workflow's dispatch form collects this from a person. It is the same "type
    the number" discipline as the terminal prompt, in the one place somebody is already
    deciding to cut a release — and a fragment field or a PR label would both have put the
    judgement back on a branch, which is the affordance #122 removes."""
    assert cut(repo, "--no-push", "--major", "--major-confirm", "v2.34") == 2
    assert "--major-confirm asked for v3 and read 'v2.34'" in capsys.readouterr().err

    assert cut(repo, "--no-push", "--major", "--major-confirm", "v3") == 0
    assert "## v3 — a release" in (repo / "CHANGELOG.md").read_text()
    assert "confirmed by --major-confirm: v3, not v2.34" in capsys.readouterr().err


def test_major_confirm_needs_no_terminal(repo, capsys):
    """The dispatch form has no terminal to ask at, which is the whole reason the flag
    exists — the autouse fixture makes every test here look like that."""
    assert cut(repo, "--no-push", "--major", "--major-confirm", "v3") == 0
    assert "## v3 — a release" in (repo / "CHANGELOG.md").read_text()


def test_an_unattended_run_is_refused_even_carrying_a_typed_confirmation(
        repo, capsys, monkeypatch):
    """The ordering is the point, and Codex found it the other way round. A number typed by a
    run that has declared nobody is watching is a number an agent worked out, not a judgement
    a person made — so `--major --major-confirm v3` under `HARNESS_UNATTENDED=1` would have
    carried a loop straight past the gate, which is #386 with an extra flag on it."""
    monkeypatch.setenv("HARNESS_UNATTENDED", "1")
    before = (repo / "CHANGELOG.md").read_text()

    assert cut(repo, "--no-push", "--major", "--major-confirm", "v3") == 2

    assert (repo / "CHANGELOG.md").read_text() == before
    assert "HARNESS_UNATTENDED=1" in capsys.readouterr().err


def test_the_answer_comes_from_the_terminal_and_never_from_stdin(repo, monkeypatch):
    """A REAL pty, because the property under test is which file the answer comes from.

    Named for what it proves and not for more: an `openpty` slave is a terminal but not this
    process's CONTROLLING one, so this test is about `TERMINAL` being the file that is read
    and stdin being ignored — which is the difference from `sys.stdin.isatty()`, where a
    heredoc or `yes |` is an answer. That `/dev/tty` names the controlling terminal is the
    kernel's job and is not restated here.
    """
    controller, terminal_fd = os.openpty()
    try:
        monkeypatch.setattr(rs, "TERMINAL", os.ttyname(terminal_fd))
        monkeypatch.setattr(sys, "stdin", io.StringIO("v9\n"))  # what a pipe would supply
        os.write(controller, b"v3\n")
        assert ASK_THE_TERMINAL("pick a number: ") == "v3"
        assert b"pick a number:" in os.read(controller, 4096)
    finally:
        os.close(controller)
        os.close(terminal_fd)


def test_a_terminal_nobody_answers_refuses_rather_than_hanging(repo, monkeypatch):
    """The tmux-pane case again, from the other side. Without the timeout the failure there
    is not a wrong release but a `readline()` that never returns."""
    controller, terminal_fd = os.openpty()
    try:
        monkeypatch.setattr(rs, "TERMINAL", os.ttyname(terminal_fd))
        with pytest.raises(OSError, match="nothing answered the terminal"):
            ASK_THE_TERMINAL("pick a number: ", timeout=0.05)
    finally:
        os.close(controller)
        os.close(terminal_fd)


def test_a_terminal_that_answers_a_letter_at_a_time_is_still_read_whole(repo, monkeypatch):
    """A terminal is USUALLY in canonical mode and hands over one whole line, but the
    application that owns it may have left it in raw mode, where `select` goes ready on the
    first keystroke. One read there returns "v" and refuses a person who typed v3."""
    controller, terminal_fd = os.openpty()
    typed = threading.Thread(
        target=lambda: [time.sleep(0.05), os.write(controller, b"3\n")],
    )
    try:
        monkeypatch.setattr(rs, "TERMINAL", os.ttyname(terminal_fd))
        os.write(controller, b"v")
        typed.start()
        assert ASK_THE_TERMINAL("pick a number: ", timeout=5) == "v3"
    finally:
        typed.join()
        os.close(controller)
        os.close(terminal_fd)


def test_a_terminal_that_closes_at_the_prompt_is_not_an_empty_answer(repo, monkeypatch):
    """^D. Distinguished from a wrong answer on purpose: "you typed '' " would be a lie
    about what happened, and the repair for the two is not the same."""
    controller, terminal_fd = os.openpty()
    try:
        monkeypatch.setattr(rs, "TERMINAL", os.ttyname(terminal_fd))
        os.write(controller, b"\x04")  # ^D at the start of a line: read returns nothing
        with pytest.raises(OSError, match="closed without answering"):
            ASK_THE_TERMINAL("pick a number: ", timeout=5)
    finally:
        os.close(controller)
        os.close(terminal_fd)


def test_a_tcgetpgrp_failure_that_is_not_enotty_is_a_refusal(repo, monkeypatch):
    """ENOTTY means "there is no job control here, so there is no background to be in", and
    is the only errno read that way. Anything else is a descriptor this tool does not
    understand, and waving it through would be permission derived from an error."""
    controller, terminal_fd = os.openpty()

    def broken(fd):
        raise OSError(errno.EBADF, "Bad file descriptor")

    try:
        monkeypatch.setattr(rs, "TERMINAL", os.ttyname(terminal_fd))
        monkeypatch.setattr(rs.os, "tcgetpgrp", broken)
        with pytest.raises(OSError, match="Bad file descriptor"):
            ASK_THE_TERMINAL("pick a number: ", timeout=0.05)
    finally:
        os.close(controller)
        os.close(terminal_fd)


def test_sigttin_is_ignored_for_the_read_and_restored_after(repo, monkeypatch):
    """The `tcgetpgrp` check fires before the prompt and cannot close the window between
    itself and the read — shell job control can move through it, and the kernel's answer to
    a background read is SIGTTIN, which STOPS the process. Ignoring it makes that an EIO,
    which is an OSError, which is a refusal. The handler is put back either way."""
    before = signal.getsignal(signal.SIGTTIN)
    seen = []
    real = rs.os.read

    def note_the_handler(fd, n):
        seen.append(signal.getsignal(signal.SIGTTIN))
        return real(fd, n)

    controller, terminal_fd = os.openpty()
    try:
        monkeypatch.setattr(rs, "TERMINAL", os.ttyname(terminal_fd))
        monkeypatch.setattr(rs.os, "read", note_the_handler)
        os.write(controller, b"v3\n")
        assert ASK_THE_TERMINAL("pick a number: ", timeout=5) == "v3"
    finally:
        os.close(controller)
        os.close(terminal_fd)

    assert seen == [signal.SIG_IGN]
    assert signal.getsignal(signal.SIGTTIN) is before


def test_a_terminal_this_run_is_not_the_foreground_of_refuses(repo, monkeypatch):
    """`apply --major &` under a shell. A background read gets SIGTTIN, whose default action
    stops the process — a hang the timeout cannot rescue, because the stop lands before
    anything begins waiting. It is also the honest answer: a background job has nobody at it.
    """
    controller, terminal_fd = os.openpty()
    try:
        monkeypatch.setattr(rs, "TERMINAL", os.ttyname(terminal_fd))
        monkeypatch.setattr(rs.os, "tcgetpgrp", lambda fd: os.getpgrp() + 1)
        with pytest.raises(OSError, match="not the terminal's foreground job"):
            ASK_THE_TERMINAL("pick a number: ", timeout=0.05)
    finally:
        os.close(controller)
        os.close(terminal_fd)


def test_a_terminal_with_no_job_control_is_still_readable(repo, monkeypatch):
    """The pty the two tests above build has no session attached, so `tcgetpgrp` answers
    ENOTTY rather than a pgid. That is "there is no background to be in", not "you are in
    it" — reading ENOTTY as a refusal would make the gate refuse every terminal it could
    actually have used."""
    controller, terminal_fd = os.openpty()
    try:
        monkeypatch.setattr(rs, "TERMINAL", os.ttyname(terminal_fd))
        with pytest.raises(OSError, match="Inappropriate ioctl"):
            os.tcgetpgrp(terminal_fd)  # the state this test is about, asserted not assumed
        os.write(controller, b"v3\n")
        assert ASK_THE_TERMINAL("pick a number: ") == "v3"
    finally:
        os.close(controller)
        os.close(terminal_fd)


def test_no_controlling_terminal_is_an_oserror_and_not_a_traceback(repo, monkeypatch, capsys):
    """The path every agent harness takes, spelled as a missing device rather than mocked."""
    monkeypatch.setattr(rs, "TERMINAL", str(repo / "no-such-tty"))
    monkeypatch.setattr(rs, "ask_the_terminal", ASK_THE_TERMINAL)

    assert cut(repo, "--no-push", "--major") == 2
    assert "no-such-tty" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# `frozen` — a released entry's TEXT, not its number
# ---------------------------------------------------------------------------
#
# Everything above this line reads the CHANGELOG as a list of NUMBERS: is one missing, is one
# repeated, did this branch claim one somebody else took. On 2026-08-20 a merge resolution
# moved a branch's own 133-line entry under `## v2.59`, on top of that release's notes, and
# every one of those questions still answered correctly — the headings were all present,
# unique and in order. The branch was pushed and sat on an open PR for two days (#325).
#
# So these tests are about the bytes. `frozen` is fork-relative like `guard` and asked of two
# refs for the same reason, so the working tree is left somewhere else here too.

#: What a shipped release says. Deliberately several lines with a sub-heading in it: an entry
#: whose slab stopped at the first `###` would compare only the first paragraph, and the
#: corruption this catches replaces everything.
SHIPPED = """The plan has had an order since v2.39 and one writer for it: a human.

### The rules, and why they are labelled

`app/ordering.py` is a pure function: candidates in, an order out, no session, no clock.
"""

#: The branch's own entry — the text that ended up under the wrong heading.
MOVED = """`claims()` returned `[]`. Not filtered — empty, fleet-wide, for every caller.

### A key the dashboard can tell apart

Derive it, make the plan a row, block on pickup.
"""


def _corrupted_by_the_resolution(repo: Path) -> None:
    """Write the CHANGELOG exactly as `843c506` left it, at this fixture's scale.

    Two unnumbered headings with one body between them belonging to the second, and the
    branch's own entry relocated under the newest released heading, whose text it replaced.
    Every heading is present, unique among the numbered ones, and correctly ordered.
    """
    write(repo, "CHANGELOG.md",
          CHANGELOG_HEAD
          + "## an order the rules derive\n"
          + "## a claim nobody takes\n\n"
          + "A paragraph belonging to the second heading.\n\n"
          + entry("v2.33", body=MOVED, title="a row key the dashboard can tell apart")
          + entry("v2.32") + entry("v2"))


@pytest.fixture
def shipped(repo: Path) -> Path:
    """The fixture repo with real prose under its newest release, on `main` and on `work`."""
    write(repo, "CHANGELOG.md",
          CHANGELOG_HEAD
          + entry("v2.33", body=SHIPPED, title="a row key the dashboard can tell apart")
          + entry("v2.32") + entry("v2"))
    commit(repo, "v2.33 ships its notes")
    git(repo, "checkout", "-q", "-b", "work")
    return repo


def test_frozen_refuses_the_merge_resolution_that_replaced_a_shipped_release(shipped, capsys):
    """#325 itself, reconstructed: the body moved, the heading left standing.

    The second half of the assertion is the whole reason this command exists. Every check
    that reads the file as a LIST OF NUMBERS passes on the identical tree, because the
    numbers are all fine. The prose is what is gone.
    """
    _corrupted_by_the_resolution(shipped)
    commit(shipped, "chore: resolving merge conflicts with origin/main")
    git(shipped, "checkout", "-q", "main")

    assert run(shipped, "frozen", "--onto", "main", "--branch", "work") == 2
    err = capsys.readouterr().err
    assert "v2.33" in err
    assert "is changed" in err
    assert "Release-Body-Edit: v2.33" in err, "the override is named where it is needed"

    assert run(shipped, "guard", "--onto", "main", "--branch", "work") == 2, (
        "the guard that existed passes on this tree — that is the gap #325 is about")
    capsys.readouterr()


def test_frozen_refuses_a_released_entry_that_has_vanished(shipped, capsys):
    """The stronger form of the same defect, and nothing else sees it either: counting
    duplicates and taking a maximum both survive a release simply ceasing to exist."""
    text = (shipped / "CHANGELOG.md").read_text()
    write(shipped, "CHANGELOG.md", text.replace(entry("v2.32"), "", 1))
    commit(shipped, "a resolution that dropped an entry entirely")
    git(shipped, "checkout", "-q", "main")

    assert run(shipped, "frozen", "--onto", "main", "--branch", "work") == 2
    assert "v2.32 is gone" in capsys.readouterr().err


def test_frozen_refuses_a_retitled_released_heading(shipped, capsys):
    """The heading line is part of the slab. `collision` deliberately does NOT compare
    heading text — a retitled old entry is a false positive there, with a repair message that
    is nonsense for an entry that shipped a year ago. Here it is precisely the finding: a
    shipped title is as shipped as the prose under it, and the trailer is how a deliberate
    rewrite says so."""
    text = (shipped / "CHANGELOG.md").read_text()
    write(shipped, "CHANGELOG.md",
          text.replace("## v2.33 — a row key the dashboard can tell apart",
                       "## v2.33 — a row key the dashboard can actually tell apart", 1))
    commit(shipped, "rewrite a shipped title")
    git(shipped, "checkout", "-q", "main")

    assert run(shipped, "frozen", "--onto", "main", "--branch", "work") == 2
    assert "line 1:" in capsys.readouterr().err, "the heading line is the one that differs"


def test_frozen_names_the_first_line_that_differs(shipped, capsys):
    """A refusal that only says `v2.33` sends somebody to read two 130-line entries side by
    side. One line separates "the whole body was replaced" from "a word was rewrapped", and
    those want different repairs."""
    text = (shipped / "CHANGELOG.md").read_text()
    write(shipped, "CHANGELOG.md", text.replace("no session, no clock", "no session", 1))
    commit(shipped, "rewrap a shipped paragraph")
    git(shipped, "checkout", "-q", "main")

    assert run(shipped, "frozen", "--onto", "main", "--branch", "work") == 2
    err = capsys.readouterr().err
    assert "line 7: was" in err, "the line NUMBER, counted from the heading"
    assert "no session, no clock." in err and "no session." in err, (
        "and both texts, so a rewrap is distinguishable from a body swap without opening "
        "the file")


def test_frozen_passes_a_branch_that_is_merely_behind(shipped, capsys):
    """The fork-relative half, and the failure mode that would have this switched off inside
    a week: `main` has taken two releases since `work` forked, and `work` never opened the
    file. Compared against `main` itself, both of those would read as entries this branch
    deleted."""
    write(shipped, "docs.md", "# how\n\nA branch that ships no release.\n")
    commit(shipped, "docs only")
    advance_the_integration_branch(shipped, "v2.35", "v2.34")
    git(shipped, "checkout", "-q", "main")

    assert run(shipped, "frozen", "--onto", "origin/main", "--branch", "work") == 0
    assert "released entries unchanged" in capsys.readouterr().out


def test_frozen_passes_the_fragment_a_branch_writes_and_the_release_it_becomes(shipped):
    """Neither state is visible to this check: a fragment is a new file under `changelog.d/`
    and the entry a release appends has no earlier text to be identical to. So there is
    nothing here for a branch to trip over, which is what makes it safe on `pull_request`."""
    fragment(shipped, "512.feat.md", "a thing", "did a thing.")
    commit(shipped, "feat: a thing")
    assert run(shipped, "frozen", "--onto", "main", "--branch", "work") == 0

    git(shipped, "checkout", "-q", "main")
    git(shipped, "merge", "-q", "--ff-only", "work")
    git(shipped, "push", "-q", "origin", "main")
    assert cut(shipped, "--no-push") == 0
    assert run(shipped, "frozen", "--onto", "main~", "--branch", "main") == 0


def test_frozen_is_answered_from_the_refs_and_not_from_the_worktree(shipped):
    """The property both gates are built on: a push carrying a rewritten release is refused
    even from a checkout that does not have it, and CI judges the merge commit."""
    _corrupted_by_the_resolution(shipped)
    commit(shipped, "the resolution")
    git(shipped, "checkout", "-q", "main")
    assert MOVED not in (shipped / "CHANGELOG.md").read_text(), "the worktree must be clean"

    assert run(shipped, "frozen", "--onto", "main", "--branch", "work") == 2


def test_frozen_reads_a_whole_entry_across_its_sub_headings(shipped, capsys):
    """An entry ends at the next `##`, never at a `###`. Sub-headings are how a long entry is
    structured — `changelog.d` requires `###` or deeper for exactly this reason — and a slab
    that stopped at the first one would compare an opening paragraph and call the other
    hundred lines unchanged."""
    text = (shipped / "CHANGELOG.md").read_text()
    write(shipped, "CHANGELOG.md",
          text.replace("no session, no clock.", "no session, no clock, no board.", 1))
    commit(shipped, "edit text below a sub-heading of a shipped entry")
    git(shipped, "checkout", "-q", "main")

    assert run(shipped, "frozen", "--onto", "main", "--branch", "work") == 2
    assert "v2.33" in capsys.readouterr().err


def test_a_fenced_heading_inside_an_entry_does_not_end_it(repo, capsys):
    """This repo's own CHANGELOG quotes release headings inside fenced blocks, and one of
    them read as a section boundary would truncate the entry containing it — leaving
    everything below the fence uncompared, which is the half of the file a corrupted
    resolution lands in."""
    fenced = "An example:\n\n```\n## v9.9 — a heading in a fence\n```\n\nAnd then the point.\n"
    write(repo, "CHANGELOG.md",
          CHANGELOG_HEAD + entry("v2.33", body=fenced) + entry("v2.32") + entry("v2"))
    land(repo, "v2.33 documents the convention it follows")
    git(repo, "checkout", "-q", "-b", "work")
    write(repo, "CHANGELOG.md",
          (repo / "CHANGELOG.md").read_text().replace("And then the point.",
                                                      "And then something else.", 1))
    commit(repo, "edit below the fence, inside a shipped entry")
    git(repo, "checkout", "-q", "main")

    assert run(repo, "frozen", "--onto", "main", "--branch", "work") == 2
    assert "v2.33" in capsys.readouterr().err


def test_frozen_ignores_the_files_preamble(shipped):
    """Everything above the first `## vX.Y` is the convention documenting itself, and it is
    edited on purpose — this release edits it. A guard that froze it would be red on the
    branch that improved the instructions, and off by the end of the week."""
    text = (shipped / "CHANGELOG.md").read_text()
    write(shipped, "CHANGELOG.md",
          text.replace("Entries are newest first.",
                       "Entries are newest first, and released ones never change.", 1))
    commit(shipped, "sharpen the preamble")
    git(shipped, "checkout", "-q", "main")

    assert run(shipped, "frozen", "--onto", "main", "--branch", "work") == 0


def test_frozen_declines_to_align_a_number_declared_twice(shipped, capsys):
    """There is no saying which of two `## v2.33` entries answers to which. That state is
    already `collision`'s refusal, with a repair attached; reporting it a second time in
    different words helps nobody, and guessing would report the wrong entry as rewritten."""
    text = (shipped / "CHANGELOG.md").read_text()
    write(shipped, "CHANGELOG.md",
          text.replace("## v2.33", entry("v2.33", body=MOVED) + "## v2.33", 1))
    commit(shipped, "a keep-both-sides resolution")
    git(shipped, "checkout", "-q", "main")

    assert run(shipped, "frozen", "--onto", "main", "--branch", "work") == 0
    captured = capsys.readouterr()
    assert "uncompared: v2.33" in captured.err, (
        "and says which entry it did not read — an `ok:` line on its own would claim cover "
        "this run does not have")
    assert "2 released entries unchanged" in captured.out, "the other two still were"
    assert run(shipped, "guard", "--onto", "main", "--branch", "work") == 2, (
        "the check that owns this state still refuses it")


def test_a_release_body_edit_trailer_waives_the_release_it_names(shipped, capsys):
    """The sanctioned exception: a typo in a shipped entry. Declared on a commit, where a
    reviewer sees it, rather than pushed past with `--no-verify`."""
    text = (shipped / "CHANGELOG.md").read_text()
    write(shipped, "CHANGELOG.md", text.replace("candidates in", "candidates in,", 1))
    commit(shipped, "docs: a comma in v2.33's entry\n\nRelease-Body-Edit: v2.33")
    git(shipped, "checkout", "-q", "main")

    assert run(shipped, "frozen", "--onto", "main", "--branch", "work") == 0
    captured = capsys.readouterr()
    assert "waived: v2.33" in captured.err, "a waived edit is still an edited release"
    assert "unchanged" in captured.out


def test_a_commit_body_quoting_the_refusal_is_not_consent(shipped, capsys):
    """Codex found this one. The refusal ENDS with a ready-to-paste
    `Release-Body-Edit: v2.33`, so a commit message quoting the message it just got is the
    most likely one this branch will ever produce — and reading it as consent would waive the
    entry on the strength of a paste. Git's own trailer parser is what makes it a trailer
    rather than a line that looks like one: this is in the middle of a paragraph, and there
    is a real trailer after it that git parses instead.
    """
    text = (shipped / "CHANGELOG.md").read_text()
    write(shipped, "CHANGELOG.md", text.replace("candidates in", "candidates in,", 1))
    commit(shipped,
           "fix: the changelog\n\nThe hook said: Release-Body-Edit: v2.33 was the way to "
           "declare this, but\nI have not decided yet.\n\nRefs: #325\n")
    git(shipped, "checkout", "-q", "main")

    assert run(shipped, "frozen", "--onto", "main", "--branch", "work") == 2
    assert "v2.33" in capsys.readouterr().err


def test_a_trailer_naming_another_release_waives_nothing(shipped, capsys):
    """Per-entry, not a switch. A branch legitimately fixing v2.32 has said nothing about
    v2.33, and the resolution that ate v2.33 is exactly what would otherwise ride along."""
    text = (shipped / "CHANGELOG.md").read_text()
    write(shipped, "CHANGELOG.md", text.replace("candidates in", "candidates in,", 1))
    commit(shipped, "docs: a comma\n\nRelease-Body-Edit: v2.32")
    git(shipped, "checkout", "-q", "main")

    assert run(shipped, "frozen", "--onto", "main", "--branch", "work") == 2
    assert "v2.33" in capsys.readouterr().err


def test_a_trailer_that_landed_does_not_waive_the_next_branch(shipped, capsys):
    """The reason the exemption is a commit trailer and not a file. It is read from
    `base..branch`, so once the edit lands the trailer is behind the merge base and the entry
    is immutable again for everybody after — where a stored exemption would sit in the repo
    forever, waiving the one entry somebody once had a reason to touch."""
    text = (shipped / "CHANGELOG.md").read_text()
    write(shipped, "CHANGELOG.md", text.replace("candidates in", "candidates in,", 1))
    commit(shipped, "docs: a comma in v2.33's entry\n\nRelease-Body-Edit: v2.33")
    git(shipped, "checkout", "-q", "main")
    git(shipped, "merge", "-q", "--ff-only", "work")

    git(shipped, "checkout", "-q", "-b", "later", "main")
    write(shipped, "CHANGELOG.md",
          (shipped / "CHANGELOG.md").read_text().replace("no session, no clock", "nothing", 1))
    commit(shipped, "a later branch, saying nothing")
    git(shipped, "checkout", "-q", "main")

    assert run(shipped, "frozen", "--onto", "main", "--branch", "later") == 2
    assert "v2.33" in capsys.readouterr().err


def test_frozen_refuses_a_branch_that_deleted_the_changelog_outright(shipped, capsys):
    """The largest version of the defect, and the one shape a "does this file exist" guard
    turns into a silent pass. Reported as the whole file rather than as three entries each
    individually gone: the count is the fact, and one line of it is more legible than N.
    """
    (shipped / "CHANGELOG.md").unlink()
    commit(shipped, "a resolution that took the file with it")
    git(shipped, "checkout", "-q", "main")

    assert run(shipped, "frozen", "--onto", "main", "--branch", "work") == 2
    err = capsys.readouterr().err
    assert "has no CHANGELOG.md at all" in err
    assert "3 released entries" in err


def test_frozen_says_when_it_could_not_read_the_fork_point(repo, capsys):
    """No merge base is no shipped text to be identical to. Passing is the only honest answer
    — refusing every entry would stop a correct branch over a question this could not ask —
    so a gate consuming it has to be told it got the weaker one, in a word it can test for."""
    git(repo, "checkout", "-q", "--orphan", "detached-history")
    write(repo, "CHANGELOG.md", CHANGELOG_HEAD + entry("v2.33") + entry("v2"))
    land(repo, "a history with no common ancestor")

    assert run(repo, "frozen", "--onto", "main", "--branch", "detached-history") == 0
    captured = capsys.readouterr()
    assert "limited: no merge base" in captured.err
    assert "0 released entries compared" in captured.out


def test_frozen_says_so_when_the_branch_is_already_contained_in_the_base(shipped, capsys):
    """The documented blind spot, pinned so it cannot be mistaken for cover.

    Once the corruption has landed, `main`'s fork point with `origin/main` is `main` itself,
    every entry is identical to itself, and there is nothing left for this to find. The guard
    for the commit going straight to `main` is `pre-push`, which asks the same question
    BEFORE the push, while `origin/main` is still behind. What must not happen is this run
    reading like the eighty-five-entry one.
    """
    _corrupted_by_the_resolution(shipped)
    commit(shipped, "the resolution")
    git(shipped, "checkout", "-q", "main")
    git(shipped, "merge", "-q", "--ff-only", "work")

    assert run(shipped, "frozen", "--onto", "main", "--branch", "main") == 0
    captured = capsys.readouterr()
    assert "limited: main is already contained in main" in captured.err
    assert "0 released entries compared" in captured.out

    # And the same commit, asked before it lands, is the refusal. Same tool, same question,
    # the difference being only which ref the base still points at.
    assert run(shipped, "frozen", "--onto", "main~1", "--branch", "main") == 2
    assert "v2.33" in capsys.readouterr().err


def test_frozen_reports_its_verdict_as_json(shipped, capsys):
    _corrupted_by_the_resolution(shipped)
    commit(shipped, "the resolution")
    git(shipped, "checkout", "-q", "main")

    assert run(shipped, "frozen", "--onto", "main", "--branch", "work", "--json") == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["fork_point"] == "read"
    assert payload["compared"] == 3
    assert [c["release"] for c in payload["changed"]] == ["v2.33"]
    assert payload["changed"][0]["what"] == "changed"
    assert "line 3: was 'The plan has had an order" in payload["changed"][0]["where"], (
        "the heading survived the resolution, which is why nothing else saw this; the body "
        "under it is where the difference starts")
    assert payload["skipped"] == [] and payload["exempt"] == []
    assert payload["branch_sha"] == git(shipped, "rev-parse", "work").strip()
    assert "#325" in payload["refusal"]


def test_frozen_says_which_ref_it_cannot_find(repo, capsys):
    """A gate consuming 0/2 reads Python's uncaught-exception 1 as "unknown"."""
    assert run(repo, "frozen", "--onto", "main", "--branch", "no-such-ref") == 2
    assert "does not exist here" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# the CI job that runs `frozen`
# ---------------------------------------------------------------------------
#
# A tool nothing calls is the state #325 is a report about: "diff the bodies of neighbouring
# released entries" was a line in the lander's brief for five landings in a row, and one of
# them would eventually have been the landing where somebody skimmed it. So the workflow's
# shape is asserted here rather than assumed — specifically the checkout depth, because a
# depth-1 clone has no fork point, `frozen` correctly reports `limited:` and exits 0, and the
# job goes green forever while reading nothing at all.


def _frozen_job() -> dict:
    """The job that runs `frozen`, found by what it runs rather than by its name."""
    yaml = pytest.importorskip("yaml")
    workflow = Path(__file__).resolve().parent.parent / ".github/workflows/tests.yml"
    jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
    running = [
        job for job in jobs.values()
        if any("release.py frozen" in "\n".join(
            line for line in str(step.get("run", "")).splitlines()
            if not line.lstrip().startswith("#"))
            for step in job.get("steps", []))
    ]
    assert len(running) == 1, (
        f"{len(running)} jobs in tests.yml run `release.py frozen`; the guard against a "
        "rewritten release entry has to run exactly once and has to run at all")
    return running[0]


def test_the_frozen_job_checks_out_the_whole_history():
    """The one way this job can be wrong and look right.

    `actions/checkout@v4` fetches a single commit by default. There is then no merge base,
    nothing to compare a released entry with, and `frozen` reports `limited:` and passes —
    which is a required check reporting green on a corrupted CHANGELOG.
    """
    checkouts = [step for step in _frozen_job()["steps"]
                 if str(step.get("uses", "")).startswith("actions/checkout")]
    assert checkouts, "the frozen job does not check the repo out at all"
    assert all(str(step.get("with", {}).get("fetch-depth")) == "0" for step in checkouts), (
        "the frozen job checks out at the default depth of 1, so it has no fork point and "
        "`frozen` will report `limited:` and pass on every run")


def test_the_frozen_job_runs_on_pull_requests():
    """Where the failure it catches actually sits. The corrupted branch in #325 was on an
    open PR for two days; on `main` the merge base with `origin/main` is HEAD and there is
    nothing left to compare, which is why `pre-push` covers that side instead."""
    assert "pull_request" in str(_frozen_job().get("if", "")), (
        "the frozen job does not name pull_request in its `if`, so it either never runs on "
        "the event that matters or runs on push-to-main where it can only be a no-op")


def test_a_depth_one_clone_says_it_read_nothing_rather_than_reporting_a_clean_bill(
        tmp_path, capsys):
    """The behaviour the assertion above is protecting against, run rather than described.

    In a depth-1 clone `origin/main` and HEAD are the same grafted commit, so the fork point
    is the branch itself and every entry is trivially identical to itself. The danger is not
    that the answer is wrong — it is that a run which judged nothing is worded exactly like a
    run which judged eighty-five entries. So this one is `limited:`, in the same word every
    other unaskable question here reports with, and the CI job asks for the whole history.

    Built as a real shallow clone for the same reason `check`'s is: what is under test is
    what git reports across a graft, and a stand-in would only ever assert the stand-in.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.email", "t@example.com")
    git(origin, "config", "user.name", "t")
    write(origin, "CHANGELOG.md", CHANGELOG_HEAD + entry("v2.33", body=SHIPPED) + entry("v2"))
    commit(origin, "v2.33 ships its notes")
    write(origin, "CHANGELOG.md",
          CHANGELOG_HEAD + entry("v2.33", body=MOVED) + entry("v2"))
    commit(origin, "and then something replaced them")

    clone = shallow_clone_of(origin, tmp_path / "clone")
    assert git(clone, "rev-parse", "--is-shallow-repository").strip() == "true"

    assert run(clone, "frozen", "--onto", "origin/main", "--branch", "HEAD") == 0
    captured = capsys.readouterr()
    assert "limited: HEAD is already contained in origin/main" in captured.err
    assert "0 released entries compared" in captured.out, (
        "the count is what a reader checks; two entries were there and neither was judged")


