"""The shared `refs/stash` guard, pinned against real git rather than described.

`refs/stash` lives in the COMMON git dir, so every worktree of a repo shares one
stash stack: `git stash push` in `foo-fix-issue-114` is listed by `git stash
list` in `foo-fix-issue-113`, and `stash@{0}` there resolves to whatever the last
pusher meant. This harness runs many concurrent worktrees off one `.git` by
design, so that is the normal configuration here, and it has already taken two
working trees (#210) — the second time with the recovery note parked in the same
shared stash that ate it.

`test_a_stash_pushed_in_one_worktree_is_poppable_from_a_sibling` builds that
hazard out of real git with the guard switched off, so this suite states the bug
before it states the fix. Everything after it is the fix.

WHAT THE GUARD CAN AND CANNOT DO, measured on git 2.54.0 rather than assumed:
`git stash pop` removes its entry with a REFLOG delete, which raises no ref
transaction at all while another entry remains underneath — so no hook can refuse
a pop. The guard therefore keeps the shared stack EMPTY (it refuses writes TO
`refs/stash`) rather than policing reads from it, and `test_the_pop_side_is_not
_interceptable` records that limit so nobody re-derives it.

Run: pytest harness/tests/test_stash_guard.py
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
HOOKS = Path(__file__).resolve().parents[1] / "githooks"
QB_HOOKS = BIN / "qb-hooks"
CREATE_WORKTREE = BIN / "create-worktree"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_START = "# >>> stash-guard"
_END = "# <<< stash-guard"


def env(home: Path, **extra) -> dict:
    """A real PATH — git is not at /usr/bin/git on every host — with the global
    and system configs pointed at a dir the test owns, so the delegate this
    resolves is the test's and not the machine's gitleaks hook."""
    e = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
         "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
         "GIT_CONFIG_SYSTEM": str(home / ".gitconfig-system")}
    e.update(extra)
    return e


def git(repo: Path, *args, home: Path, check=True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, env=env(home, **kw), check=check)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    (h / ".gitconfig").write_text("")
    (h / ".gitconfig-system").write_text("")
    return h


@pytest.fixture
def repo(tmp_path, home):
    """A main checkout with one linked worktree — the configuration this harness
    creates on purpose, and the one the shared stash is unsafe in."""
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True,
                   env=env(home))
    git(main, "config", "user.email", "t@example.com", home=home)
    git(main, "config", "user.name", "T", home=home)
    (main / "f.txt").write_text("v1\n")
    git(main, "add", "f.txt", home=home)
    git(main, "commit", "-qm", "init", home=home)
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", str(wt), "-b", "side", home=home)
    return main, wt


def install(main: Path, home: Path) -> subprocess.CompletedProcess:
    return subprocess.run([str(QB_HOOKS), "install", "--repo", str(main)],
                          capture_output=True, text=True, env=env(home), check=True)


# ---------------------------------------------------------------- the hazard

def test_a_stash_pushed_in_one_worktree_is_poppable_from_a_sibling(repo, home):
    """The defect, built from real git with no guard installed. If this ever stops
    failing to isolate the two worktrees, git changed and the rest of this suite
    is guarding a bug that no longer exists."""
    main, wt = repo
    (wt / "f.txt").write_text("SIDE-AGENT-WORK\n")
    git(wt, "stash", "push", "-q", "-m", "side agent wip", home=home)

    # The sibling can see it, and `stash@{0}` there IS the other worktree's entry.
    assert "side agent wip" in git(main, "stash", "list", home=home).stdout
    git(main, "stash", "pop", home=home)
    assert (main / "f.txt").read_text() == "SIDE-AGENT-WORK\n", (
        "the sibling did not get the other worktree's work — has git gained a "
        "per-worktree stash?")
    assert git(main, "stash", "list", home=home).stdout.strip() == "", (
        "and the owning worktree's entry is gone, with nothing warning either side")


# ---------------------------------------------------------------- the guard

def test_a_linked_worktree_cannot_push_to_the_shared_stash(repo, home):
    main, wt = repo
    install(main, home)
    (wt / "f.txt").write_text("SIDE-AGENT-WORK\n")
    r = git(wt, "stash", "push", "-m", "side agent wip", home=home, check=False)
    assert r.returncode != 0
    assert "REFUSED" in r.stderr
    assert git(main, "stash", "list", home=home).stdout.strip() == ""


def test_the_working_tree_survives_a_refusal(repo, home):
    """A guard that aborted mid-stash and ate the change would be worse than the
    bug it prevents, so this is the property to pin rather than the message."""
    main, wt = repo
    install(main, home)
    (wt / "f.txt").write_text("SIDE-AGENT-WORK\n")
    git(wt, "stash", "push", "-m", "wip", home=home, check=False)
    assert (wt / "f.txt").read_text() == "SIDE-AGENT-WORK\n"


def test_the_main_checkout_is_guarded_too_while_worktrees_exist(repo, home):
    """The near-miss that prompted this was an orchestrator running `git stash
    push -u` in MAIN while sub-agents ran in siblings. Guarding only the linked
    worktrees would have let that one straight through."""
    main, _wt = repo
    install(main, home)
    (main / "f.txt").write_text("ORCHESTRATOR-WIP\n")
    r = git(main, "stash", "push", "-m", "clearing the tree", home=home, check=False)
    assert r.returncode != 0 and "REFUSED" in r.stderr
    assert (main / "f.txt").read_text() == "ORCHESTRATOR-WIP\n"


def test_a_repo_with_no_linked_worktrees_still_stashes_normally(tmp_path, home):
    """The hazard is the shared stack, and a single-checkout repo does not have
    one. A guard that bit there would be the kind people disable wholesale."""
    solo = tmp_path / "solo"
    solo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(solo)], check=True, env=env(home))
    git(solo, "config", "user.email", "t@example.com", home=home)
    git(solo, "config", "user.name", "T", home=home)
    (solo / "f.txt").write_text("v1\n")
    git(solo, "add", "f.txt", home=home)
    git(solo, "commit", "-qm", "init", home=home)
    install(solo, home)

    (solo / "f.txt").write_text("v2\n")
    git(solo, "stash", "push", "-q", "-m", "ordinary", home=home)
    assert "ordinary" in git(solo, "stash", "list", home=home).stdout
    git(solo, "stash", "pop", home=home)
    assert (solo / "f.txt").read_text() == "v2\n"


def test_the_escape_hatch_lets_a_deliberate_stash_through(repo, home):
    main, wt = repo
    install(main, home)
    (wt / "f.txt").write_text("deliberate\n")
    git(wt, "stash", "push", "-q", "-m", "on purpose", home=home,
        QB_ALLOW_SHARED_STASH="1")
    assert "on purpose" in git(wt, "stash", "list", home=home).stdout


def test_a_pre_existing_entry_can_still_be_cleared(repo, home):
    """Deletions of refs/stash are deliberately allowed: entries pushed before the
    guard was installed must remain droppable, or installing it strands them."""
    main, wt = repo
    (main / "f.txt").write_text("older than the guard\n")
    git(main, "stash", "push", "-q", "-m", "legacy", home=home)
    install(main, home)
    git(main, "stash", "drop", home=home)
    assert git(main, "stash", "list", home=home).stdout.strip() == ""


def test_ordinary_ref_transactions_are_untouched(repo, home):
    """The hook fires on every ref update in the repo — commits, branches,
    fetches. One that refused more than refs/stash would wedge the whole repo."""
    main, wt = repo
    install(main, home)
    (wt / "g.txt").write_text("new\n")
    git(wt, "add", "g.txt", home=home)
    git(wt, "commit", "-qm", "a commit under the guard", home=home)
    git(wt, "branch", "another", home=home)
    git(wt, "tag", "v0", home=home)
    assert "another" in git(wt, "branch", "--list", home=home).stdout


def test_the_pop_side_is_not_interceptable(repo, home):
    """Recorded so it is not re-derived, and so the design is not mistaken for
    something stronger than it is: `git stash pop` drops its entry through the
    REFLOG while another entry remains, which raises no ref transaction, so no
    hook sees it. Keeping the stack empty is the protection; refusing pops is not
    available."""
    main, wt = repo
    (main / "f.txt").write_text("A\n")
    git(main, "stash", "push", "-q", "-m", "A", home=home)
    (main / "f.txt").write_text("B\n")
    git(main, "stash", "push", "-q", "-m", "B", home=home)
    install(main, home)

    r = git(wt, "stash", "pop", home=home, check=False)
    assert r.returncode == 0, "if this now fails, git learned to raise a transaction"
    assert (wt / "f.txt").read_text() == "B\n"


# ------------------------------------------------- not breaking other hooks

def _managed_hooks_dir(home: Path) -> Path:
    d = home / "managed-hooks"
    d.mkdir()
    marker = d / "pre-commit"
    marker.write_text('#!/bin/sh\n'
                      'touch "$(git rev-parse --show-toplevel)/managed-ran"\n')
    marker.chmod(0o755)
    return d


def test_install_re_exports_the_managed_hooks(repo, home):
    """`core.hooksPath` REPLACES the hooks dir rather than stacking. On this fleet
    the global value is a nix store path whose only entry is a gitleaks
    `pre-commit`, so an install that did not re-export it would switch secret
    scanning off for the repo as a side effect of a stash-safety feature."""
    main, _wt = repo
    managed = _managed_hooks_dir(home)
    git(main, "config", "--global", "core.hooksPath", str(managed), home=home)
    install(main, home)

    (main / "h.txt").write_text("x\n")
    git(main, "add", "h.txt", home=home)
    git(main, "commit", "-qm", "with hooks", home=home)
    assert (main / "managed-ran").exists(), (
        "the managed pre-commit stopped running once core.hooksPath was repointed")


def test_the_delegate_is_resolved_at_run_time_not_recorded(repo, home):
    """The managed dir is a nix store path that changes on every rebuild, so an
    install-time snapshot of it rots into a broken or garbage-collected path."""
    main, _wt = repo
    git(main, "config", "--global", "core.hooksPath", str(_managed_hooks_dir(home)),
        home=home)
    install(main, home)

    # A home-manager rebuild: same hook name, brand new store path.
    later = home / "managed-hooks-v2"
    later.mkdir()
    hook = later / "pre-commit"
    hook.write_text('#!/bin/sh\n'
                    'touch "$(git rev-parse --show-toplevel)/v2-ran"\n')
    hook.chmod(0o755)
    git(main, "config", "--global", "core.hooksPath", str(later), home=home)

    (main / "h.txt").write_text("x\n")
    git(main, "add", "h.txt", home=home)
    git(main, "commit", "-qm", "after the rebuild", home=home)
    assert (main / "v2-ran").exists()


def test_re_installing_does_not_make_the_guard_delegate_to_itself(repo, home):
    main, _wt = repo
    managed = _managed_hooks_dir(home)
    git(main, "config", "--global", "core.hooksPath", str(managed), home=home)
    install(main, home)
    install(main, home)
    recorded = git(main, "config", "--get", "qb.hooksDelegate", home=home).stdout.strip()
    assert recorded == str(managed)


# ----------------------------------------------------------- the wiring in

def _block(script: Path, start: str, end: str) -> str:
    src = script.read_text()
    assert start in src and end in src, (
        f"the {start} / {end} markers are gone from {script.name}, so this suite is "
        "asserting nothing — fix the markers rather than deleting the test")
    return src.split(start, 1)[1].split("\n", 1)[1].split(end, 1)[0]


def test_create_worktree_installs_the_guard(repo, home):
    """Extracted from the real script rather than copied, so a refactor that moves
    or renames the stanza fails here instead of leaving this green about code
    nobody runs any more."""
    main, _wt = repo
    block = _block(CREATE_WORKTREE, _START, _END)
    assert "qb-hooks" in block
    script = ("set -uo pipefail\n"
              'YELLOW=""; NC=""\n'
              f'WORKTREE_SCRIPT_DIR="{BIN}"\n'
              f'MAIN_REPO="{main}"\n') + block
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env=env(home))
    assert r.returncode == 0, r.stderr
    hooks_path = git(main, "config", "--local", "--get", "core.hooksPath", home=home)
    assert hooks_path.stdout.strip().endswith("/qb-hooks")

    (main / "f.txt").write_text("after create-worktree\n")
    assert "REFUSED" in git(main, "stash", "push", "-m", "x", home=home,
                            check=False).stderr


def test_remove_worktree_rescues_qb_stash_entries(repo, home):
    """`refs/worktree/*` is what makes a qb-stash per-worktree, and also what makes
    `git worktree remove` take it with the worktree. The entries are invisible to
    `git status`, so the dirty-file backup does not see them."""
    main, wt = repo
    (wt / "f.txt").write_text("work worth keeping\n")
    subprocess.run([str(BIN / "qb-stash"), "push", "-m", "parked"], cwd=wt,
                   capture_output=True, text=True, env=env(home), check=True)

    block = _block(BIN / "remove-worktree", "# >>> qb-stash-rescue", "# <<< qb-stash-rescue")
    script = ("set -uo pipefail\n"
              'YELLOW=""; NC=""\n'
              'SAFE_NAME="side"\n'
              f'cd "{wt}"\n') + block
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env(home))
    assert r.returncode == 0, r.stderr
    assert "Rescued 1" in r.stdout

    git(main, "worktree", "remove", "--force", str(wt), home=home)
    rescued = git(main, "for-each-ref", "--format=%(refname)",
                  "refs/qb-stash-rescued/side/", home=home).stdout.strip()
    assert rescued, "the parked work died with the worktree"
    show = git(main, "stash", "show", "-p", rescued, home=home)
    assert "work worth keeping" in show.stdout


def test_the_hook_sources_ship_with_an_installed_harness():
    """`qb-hooks` copies its scripts out of `harness/githooks`, which is data
    rather than a bin entry point. If package.nix stops shipping the directory,
    an installed harness creates worktrees with no stash guard at all — and does
    it quietly, because create-worktree's call is best-effort."""
    nix = (Path(__file__).resolve().parents[1] / "package.nix").read_text()
    assert "githooks" in nix, "package.nix no longer installs harness/githooks"
    for name in ("reference-transaction", "pre-push", "qb-hook-forward"):
        assert (HOOKS / name).exists()
        assert os.access(HOOKS / name, os.X_OK), f"{name} is not executable"


def test_a_managed_reference_transaction_hook_still_runs(repo, home):
    """The guard occupies the one hook name it most needs to share. Chaining is
    gated on a marker symlink rather than a config lookup, because this fires on
    every ref update in the repo and two `git config` calls there made `git
    branch` several times slower."""
    main, _wt = repo
    managed = home / "managed-hooks"
    managed.mkdir()
    hook = managed / "reference-transaction"
    hook.write_text('#!/bin/sh\ntouch "$(git rev-parse --show-toplevel)/rt-ran"\n'
                    'cat >/dev/null\nexit 0\n')
    hook.chmod(0o755)
    git(main, "config", "--global", "core.hooksPath", str(managed), home=home)
    install(main, home)

    git(main, "branch", "chained", home=home)
    assert (main / "rt-ran").exists(), "the managed reference-transaction stopped running"


def test_no_marker_is_left_when_there_is_nothing_to_chain_to(repo, home):
    main, _wt = repo
    managed = _managed_hooks_dir(home)   # pre-commit only
    git(main, "config", "--global", "core.hooksPath", str(managed), home=home)
    install(main, home)
    common = Path(git(main, "rev-parse", "--path-format=absolute", "--git-common-dir",
                      home=home).stdout.strip())
    assert not (common / "qb-hooks" / "reference-transaction.delegate").exists()
