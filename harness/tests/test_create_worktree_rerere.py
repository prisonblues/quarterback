"""`create-worktree` turning on rerere, pinned rather than asserted in prose.

The behaviour was documented in three places — CHANGELOG, `harness/README.md` and
an inline comment — and tested nowhere, which is the arrangement where the docs
and the code drift apart and only the docs get read. Round 1 of PR #87 filed it
from three seats independently.

What is pinned here is exactly what those three places promise: an unset repo
gains `rerere.enabled=true` locally; a repo that turned it off **stays** off; the
global config is never touched; a replayed resolution is left unstaged (which is
`rerere.autoUpdate`, and is now written rather than merely left absent); and a
failure to write config does not cost you a worktree.

The block is extracted from the real script rather than copied into the test, so
a refactor that moves or renames it fails here instead of silently leaving this
suite green about code nobody runs any more.

Run: pytest harness/tests
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "create-worktree"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

# Explicit sentinels rather than a pattern over the code. Round 1's version
# anchored on the probe's exact text, and round 2's fix rewrote that probe — which
# would have left this suite extracting nothing and passing about it. A marker is
# the one thing a refactor cannot silently change the meaning of.
_START = "# >>> rerere"
_END = "# <<< rerere"


def rerere_block() -> str:
    """The rerere stanza, lifted out of create-worktree as it actually ships."""
    src = SCRIPT.read_text()
    assert _START in src and _END in src, (
        f"the {_START} / {_END} markers are gone from create-worktree, so this "
        "suite is asserting nothing — fix the markers rather than deleting the test")
    # Drop the remainder of the marker's own line — it carries a comment, and
    # bash would try to run it.
    block = src.split(_START, 1)[1].split("\n", 1)[1].split(_END, 1)[0]
    assert "rerere.enabled" in block, "the markers no longer bracket the rerere block"
    return block


def run_block(repo: Path, home: Path) -> subprocess.CompletedProcess:
    """Run the stanza in `repo`, with colour variables stubbed and an isolated
    global config, so 'never --global' is a claim this can actually check."""
    script = ("set -euo pipefail\n"
              'GREEN=""; YELLOW=""; NC=""\n'
              f'cd "{repo}"\n') + rerere_block()
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env=isolated_env(home))


def isolated_env(home: Path) -> dict:
    """A real PATH — git is not at /usr/bin/git on every host this runs on — with
    the global and system configs pointed somewhere this test owns, so 'never
    --global' is checkable rather than merely asserted."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
            "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
            "GIT_CONFIG_SYSTEM": str(home / ".gitconfig-system")}


def cfg(repo: Path, key: str) -> str | None:
    r = subprocess.run(["git", "-C", str(repo), "config", "--local", "--get", key],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    return d


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    (h / ".gitconfig").write_text("")
    (h / ".gitconfig-system").write_text("")
    return h


def test_an_unset_repo_gains_rerere_locally(repo, home):
    assert run_block(repo, home).returncode == 0
    assert cfg(repo, "rerere.enabled") == "true"


def test_the_global_config_is_never_written(repo, home):
    """`--global` would turn this on for every repo on the machine off the back of
    one worktree in one of them. The claim is unconditional in two READMEs."""
    before = (home / ".gitconfig").read_text()
    run_block(repo, home)
    assert (home / ".gitconfig").read_text() == before


def test_a_repo_that_turned_it_off_stays_off(repo, home):
    """The promise users rely on to opt out — and the only opt-out there is, since
    `create-worktree` has no --no-rerere flag."""
    subprocess.run(["git", "-C", str(repo), "config", "rerere.enabled", "false"],
                   check=True)
    assert run_block(repo, home).returncode == 0
    assert cfg(repo, "rerere.enabled") == "false"


def test_a_repo_that_already_turned_it_on_is_left_alone(repo, home):
    subprocess.run(["git", "-C", str(repo), "config", "rerere.enabled", "true"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "config", "rerere.autoUpdate", "true"],
                   check=True)
    assert run_block(repo, home).returncode == 0
    # Its autoUpdate choice survives: this block only writes the pair together,
    # for a repo that had decided neither.
    assert cfg(repo, "rerere.autoUpdate") == "true"


def test_a_replayed_resolution_is_left_unstaged(repo, home):
    """The documented guarantee — 'read it before committing it' — is exactly
    `rerere.autoUpdate=false`, and it is WRITTEN rather than left absent when
    nobody has expressed a preference. Absent is not off: git's default is off,
    but a user carrying `autoUpdate=true` in a global config gets the staging the
    docs promise cannot happen, and the first version of this block never looked."""
    assert run_block(repo, home).returncode == 0
    assert cfg(repo, "rerere.autoUpdate") == "false"


def test_an_explicit_global_autoUpdate_is_warned_about_not_overwritten(repo, home):
    """Where round 1 and round 2 disagreed, and the settled answer. Round 1 pinned
    `false` unconditionally; round 2 pointed out that a user who set `autoUpdate`
    deliberately — in any scope — has expressed a decision, and a worktree helper
    is not the thing that gets to overrule it.

    So the pin is only written when nobody has decided. When somebody has decided
    the *other* way, the unstaged guarantee genuinely does not hold here, and the
    block says so rather than letting this script's own docs be a lie about it."""
    (home / ".gitconfig").write_text("[rerere]\n\tautoUpdate = true\n")
    r = run_block(repo, home)
    assert r.returncode == 0
    assert cfg(repo, "rerere.autoUpdate") is None, "their choice was overwritten"
    assert "autoUpdate is true" in r.stderr, (
        "rerere was turned on into a config that stages replayed resolutions, and "
        "nothing said so")


def test_a_global_enabled_true_still_gets_the_autoUpdate_promise_checked(repo, home):
    """The arrangement the first version of this suite did not cover, and the
    realistic one: nobody sets `autoUpdate` globally without also setting
    `enabled`. The probe reads system+global+local while a bare `git config` write
    touches only local — so a user who likes rerere and turned it on in
    `~/.gitconfig` skipped this block entirely and never got the pin, while the
    docs promised them a replayed resolution would be left unstaged.

    The block does not fight them for `enabled` — their choice, any scope, stands.
    What it must not do is claim the unstaged guarantee while `autoUpdate=true` is
    in force, so it says so on stderr."""
    (home / ".gitconfig").write_text(
        "[rerere]\n\tenabled = true\n\tautoUpdate = true\n")
    r = run_block(repo, home)
    assert r.returncode == 0
    effective = subprocess.run(["git", "-C", str(repo), "config", "--get",
                                "--type=bool", "rerere.autoUpdate"],
                               capture_output=True, text=True, env=isolated_env(home))
    if effective.stdout.strip() == "true":
        assert "autoUpdate" in r.stderr, (
            "rerere is on and autoUpdate is true, so a replayed resolution WILL be "
            "staged — the docs promise the opposite, and saying nothing makes them a lie")


def test_an_explicit_autoUpdate_is_never_clobbered(repo, home):
    """`enabled` unset but `autoUpdate` explicitly set is a decision, and the block
    only ever writes the pin when nobody has expressed one."""
    subprocess.run(["git", "-C", str(repo), "config", "rerere.autoUpdate", "true"],
                   check=True, env=isolated_env(home))
    assert run_block(repo, home).returncode == 0
    assert cfg(repo, "rerere.enabled") == "true"     # we did enable it
    assert cfg(repo, "rerere.autoUpdate") == "true"  # and left their choice alone


def test_a_set_but_invalid_value_counts_as_undecided(repo, home):
    """`git config --get` exits 0 for any value at all, so without `--type=bool`
    the script reads 'banana' as a decision and git then fails every merge in
    every worktree with 'bad boolean config value'."""
    subprocess.run(["git", "-C", str(repo), "config", "rerere.enabled", "banana"],
                   check=True)
    assert run_block(repo, home).returncode == 0
    assert cfg(repo, "rerere.enabled") == "true"


def test_a_config_write_failure_does_not_cost_you_a_worktree(repo, home):
    """A convenience default must never be a hard failure: the script runs under
    `set -euo pipefail`, and parallel loops contend for this repo's config lock.
    The stanza immediately below this one in the real script makes the same
    choice out loud for the remote refresh."""
    lock = repo / ".git" / "config.lock"
    lock.write_text("")          # what a concurrent `git config` holds
    r = run_block(repo, home)
    assert r.returncode == 0, (
        "a held config.lock aborted the run — under set -e that kills worktree "
        f"creation just after the banner and before any worktree exists:\n{r.stderr}")
    assert "could not enable rerere" in r.stderr


def test_linked_worktrees_share_the_resolution_cache(repo, home):
    """'Resolve once, replay in the other nine' rests on `rr-cache` living in the
    common git dir. That is a git property rather than one this script sets, and
    it is the whole justification for the feature, so it is worth one assertion."""
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty",
                    "-m", "root"], check=True,
                   env={**isolated_env(home),
                        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
                        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com"})
    run_block(repo, home)
    linked = repo.parent / "linked"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "wt",
                    str(linked)], check=True)
    common = subprocess.run(["git", "-C", str(linked), "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert Path(common).resolve() == (repo / ".git").resolve()
    # And the setting itself reaches the linked worktree, which is what makes the
    # replay happen there at all.
    r = subprocess.run(["git", "-C", str(linked), "config", "--local", "--get",
                        "rerere.enabled"], capture_output=True, text=True,
                       env=isolated_env(home))
    assert r.stdout.strip() == "true", (
        "--local and the isolated env are both load-bearing here: without them a "
        "developer's own global rerere.enabled=true makes this pass against a block "
        "that wrote nothing at all")
