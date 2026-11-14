"""One worker rule for create-worktree, remove-worktree and prune-worktrees (#854).

create-worktree named a repo's worker container from `.worker.type`, falling back
to what the repo's dependencies say (`detect_worker_type`). The two teardown
scripts fell back to "none" and "" instead. So a repo whose huey worker was only
DETECTED got a `huey-consumer-<name>` container that neither teardown could name:
remove-worktree's name-based pass skipped it once the worktree directory was gone,
and prune-worktrees could not pair it with a dead worktree.

The rule now lives in `harness/bin/worktree-worker`, which all three source. Each
end-to-end test below configures NO worker, so the only way to the container's
name is detection.

Run: pytest harness/tests
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _path_sandbox  # noqa: E402
from test_remove_worktree_branch_guard import TOOLS, add_worktree, repo  # noqa: E402,F401
from test_remove_worktree_nothing_here import (  # noqa: E402
    NOTHING_HELD,
    complete,
    fake_docker,
    run_with,
)

BIN = Path(__file__).resolve().parents[1] / "bin"
LIB = BIN / "worktree-worker"
SCRIPTS = ("create-worktree", "remove-worktree", "prune-worktrees")
BASH = shutil.which("bash") or "/bin/bash"
GIT = shutil.which("git")

#: A dependency list that names huey, and nothing in `.worktree.json` about it.
HUEY_DEPS = '[project]\nname = "proj"\ndependencies = ["huey>=2"]\n'


def resolve(tmp_path, config: dict, deps: str | None = HUEY_DEPS) -> tuple[str, str]:
    """WORKER_TYPE and WORKER_PREFIX as the library resolves them for a repo."""
    root = tmp_path / "r"
    root.mkdir()
    if deps is not None:
        (root / "pyproject.toml").write_text(deps)
    probe = (
        f'cfg() {{ local v; v=$(jq -r "$1 // empty" <<<\'{json.dumps(config)}\'); '
        'echo "${v:-$2}"; }; '
        f'. "{LIB}"; resolve_worker "{root}" proj; printf "%s|%s" "$WORKER_TYPE" "$WORKER_PREFIX"'
    )
    out = subprocess.run([BASH, "-c", probe], capture_output=True, text=True, check=True)
    worker_type, prefix = out.stdout.split("|")
    return worker_type, prefix


# ------------------------------------------------------------------ the rule

def test_a_detected_huey_worker_gets_its_prefix(tmp_path):
    assert resolve(tmp_path, {}) == ("huey", "huey-consumer")


def test_config_beats_detection(tmp_path):
    assert resolve(tmp_path, {"worker": {"type": "none"}}) == ("none", "")


def test_a_configured_prefix_beats_the_default(tmp_path):
    got = resolve(tmp_path, {"worker": {"container_prefix": "jobs"}})
    assert got == ("huey", "jobs")


def test_no_worker_anywhere_is_none(tmp_path):
    assert resolve(tmp_path, {}, deps=None) == ("none", "")


@pytest.mark.parametrize("name", SCRIPTS)
def test_every_script_takes_the_rule_from_the_library(name):
    """The defect was three copies disagreeing. A fourth copy, or one script that
    stops sourcing the library, brings it back — so no script may carry its own."""
    src = (BIN / name).read_text()
    assert '. "$WORKTREE_WORKER"' in src and 'resolve_worker "$MAIN_REPO" "$PROJECT"' in src
    assert "detect_worker_type() {" not in src
    assert "huey-consumer" not in src


def _alone(tmp_path, name) -> Path:
    """The script copied where the library is not beside it: a partial install."""
    lone = tmp_path / "lone"
    lone.mkdir()
    shutil.copy2(BIN / name, lone / name)
    return lone / name


@pytest.mark.parametrize("name", ["create-worktree", "prune-worktrees"])
def test_a_script_without_the_library_beside_it_refuses(tmp_path, name):
    """Without it the first call is a bare "command not found" mid-run. These two
    refuse instead, the way prune-worktrees already refuses a config it cannot
    read rather than report categories it never checked."""
    proc = subprocess.run([str(_alone(tmp_path, name)), "--help"], capture_output=True,
                          text=True, cwd=tmp_path)
    assert proc.returncode != 0
    assert "worktree-worker is not beside this script" in proc.stderr


def test_remove_worktree_without_the_library_still_tears_down_and_says_so(repo,
                                                                         tmp_path):
    """A partial install must still tear down (the worktree-lock rule, #385). What
    it cannot do is name a worker container, and the summary says so."""
    wt = add_worktree(repo, "fix-issue-43", "fix/issue-43")
    proc = subprocess.run(
        [str(_alone(tmp_path, "remove-worktree")), "fix-issue-43"], cwd=repo,
        capture_output=True, text=True,
        env=_path_sandbox.sandbox_env(tmp_path, tools=TOOLS))
    assert not wt.exists(), proc.stdout + proc.stderr
    assert "WITH WARNINGS" in proc.stdout
    assert "worktree-worker is not beside this script" in proc.stdout


# --------------------------------------------------------- the two teardowns

def test_remove_worktree_removes_a_detected_worker_once_the_directory_is_gone(repo,
                                                                            tmp_path):
    """The issue's single signal: no worktree directory, no worker config, and the
    detected worker's container still up."""
    (repo / "pyproject.toml").write_text(HUEY_DEPS)
    docker = fake_docker(tmp_path, ["huey-consumer-fix-issue-43"])

    proc = run_with(repo, ["fix-issue-43"], tmp_path, {**NOTHING_HELD, "docker": docker})

    assert complete(proc), proc.stdout + proc.stderr
    assert "Removed container huey-consumer-fix-issue-43" in proc.stdout
    assert (tmp_path / "docker-state").read_text() == ""


def _prune_repo(tmp_path) -> Path:
    main = tmp_path / "proj"
    main.mkdir()
    subprocess.run([GIT, "init", "-q", "-b", "main", str(main)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "T")):
        subprocess.run([GIT, "-C", str(main), "config", k, v], check=True)
    (main / ".worktree.json").write_text(json.dumps({"project": "proj"}))
    (main / "pyproject.toml").write_text(HUEY_DEPS)
    # The evidence the sweep is gated on: a port entry for a worktree that is gone.
    (main / ".worktree-ports").write_text("5700:fix/issue-gone\n")
    subprocess.run([GIT, "-C", str(main), "add", "-A"], check=True)
    subprocess.run([GIT, "-C", str(main), "commit", "-qm", "init"], check=True)
    return main


@pytest.mark.skipif(GIT is None, reason="git must be on PATH")
def test_prune_worktrees_pairs_a_detected_worker_with_its_dead_worktree(tmp_path):
    main = _prune_repo(tmp_path)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in (
        ("docker", 'for a in "$@"; do [[ "$a" == -a ]] && '
                   '{ echo huey-consumer-fix-issue-gone; exit 0; }; done; exit 0'),
        ("qb-claimed", "exit 2"),
        ("qb-release", "exit 0"),
    ):
        (stubs / name).write_text(f"#!{BASH}\n{body}\n")
        (stubs / name).chmod(0o755)

    proc = subprocess.run(
        [str(BIN / "prune-worktrees")], cwd=main, capture_output=True, text=True,
        env=_path_sandbox.sandbox_env(tmp_path, stubs, inherit_path=True))

    assert "Orphan containers (1):" in proc.stdout, proc.stdout + proc.stderr
    assert "huey-consumer-fix-issue-gone" in proc.stdout


def test_the_library_sets_no_shell_options():
    """Sourced into remove-worktree's `set +e` and prune-worktrees' `set -u`; a
    `set` here would quietly impose one script's options on the others."""
    lines = [ln.strip() for ln in LIB.read_text().splitlines()]
    assert not [ln for ln in lines if ln.startswith("set ")]
