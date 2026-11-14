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
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _path_sandbox  # noqa: E402
from test_remove_worktree_branch_guard import TOOLS, add_worktree, repo  # noqa: E402,F401
from test_remove_worktree_nothing_here import (  # noqa: E402,F401
    NOTHING_HELD,
    complete,
    fake_docker,
    other_repo,
    run_with,
)

BIN = Path(__file__).resolve().parents[1] / "bin"
LIB = BIN / "worktree-worker"
SCRIPTS = ("create-worktree", "remove-worktree", "prune-worktrees")
BASH = shutil.which("bash") or "/bin/bash"
GIT = shutil.which("git")

#: A dependency list that names huey, and nothing in `.worktree.json` about it.
HUEY_DEPS = '[project]\nname = "proj"\ndependencies = ["huey>=2"]\n'


def resolve(tmp_path, config: dict, files: dict[str, str] | None = None) -> tuple[str, str]:
    """WORKER_TYPE and WORKER_PREFIX as the library resolves them, with `cfg` taken
    from remove-worktree itself rather than a stand-in for it."""
    root = tmp_path / "r"
    root.mkdir()
    for name, text in ({"pyproject.toml": HUEY_DEPS} if files is None else files).items():
        (root / name).write_text(text)
    cfg_src = (BIN / "remove-worktree").read_text()
    cfg_fn = cfg_src[cfg_src.index("cfg() {"):]
    cfg_fn = cfg_fn[:cfg_fn.index("\n}\n") + 3]
    probe = (f"CONFIG_JSON={shlex.quote(json.dumps(config))}\n{cfg_fn}"
             f'. "{LIB}"; resolve_worker "{root}" proj; '
             'printf "%s|%s" "$WORKER_TYPE" "$WORKER_PREFIX"')
    out = subprocess.run([BASH, "-c", probe], capture_output=True, text=True, check=True)
    worker_type, prefix = out.stdout.split("|")
    return worker_type, prefix


# ------------------------------------------------------------------ the rule

@pytest.mark.parametrize("files, want", [
    ({"pyproject.toml": HUEY_DEPS}, ("huey", "huey-consumer")),
    ({"requirements.txt": "rq==1.16\n"}, ("rq", "proj-rq-worker")),
    ({"requirements.txt": "celery[redis]\n"}, ("celery", "proj-celery-worker")),
    ({"docker-compose.yml": "services:\n  jobs:\n    command: celery -A app worker\n"},
     ("celery", "proj-celery-worker")),
    ({}, ("none", "")),
], ids=["huey-pyproject", "rq-requirements", "celery-requirements", "celery-compose", "none"])
def test_detection_reads_every_source_and_names_every_type(tmp_path, files, want):
    assert resolve(tmp_path, {}, files) == want


def test_config_beats_detection(tmp_path):
    assert resolve(tmp_path, {"worker": {"type": "none"}}) == ("none", "")


def test_a_configured_prefix_beats_the_default(tmp_path):
    got = resolve(tmp_path, {"worker": {"container_prefix": "jobs"}})
    assert got == ("huey", "jobs")


@pytest.mark.parametrize("name", SCRIPTS)
def test_every_script_takes_the_rule_from_the_library(name):
    """The defect was three copies disagreeing. A fourth copy, or one script that
    stops sourcing the library, brings it back — so no script may carry its own."""
    src = (BIN / name).read_text()
    assert '. "$WORKTREE_WORKER"' in src and 'resolve_worker "$MAIN_REPO" "$PROJECT"' in src
    for copy in ("detect_worker_type() {", "huey-consumer", "-rq-worker", "-celery-worker",
                 "default_worker_prefix() {", "worker_owned() {"):
        assert copy not in src, f"{name} carries its own copy of the worker rule: {copy}"


def test_the_library_sets_no_shell_options():
    """Sourced into remove-worktree's `set +e` and prune-worktrees' `set -u`; a
    `set` or `shopt` here would quietly impose one script's options on the others."""
    lines = [ln.strip() for ln in LIB.read_text().splitlines()]
    assert not [ln for ln in lines if ln.startswith(("set ", "shopt "))]


# ------------------------------------------------------- a partial install

def _alone(tmp_path, name) -> Path:
    """The script copied where the library is not beside it: a partial install."""
    lone = tmp_path / "lone"
    lone.mkdir()
    shutil.copy2(BIN / name, lone / name)
    return lone / name


def test_create_worktree_without_the_library_refuses(tmp_path):
    """Creating is the one that may refuse: a worktree provisioned without the rule
    could get a worker the teardowns then cannot name."""
    proc = subprocess.run([str(_alone(tmp_path, "create-worktree")), "--help"],
                          capture_output=True, text=True, cwd=tmp_path)
    assert proc.returncode != 0
    assert "worktree-worker is not beside this script" in proc.stderr


def run_alone(tmp_path, cwd, name="fix-issue-43"):
    return subprocess.run(
        [str(_alone(tmp_path, "remove-worktree")), name], cwd=cwd,
        capture_output=True, text=True,
        env=_path_sandbox.sandbox_env(tmp_path, tools=TOOLS))


def test_remove_worktree_without_the_library_still_tears_down(repo, tmp_path):
    """A partial install must still tear down (the worktree-lock rule, #385), and
    says it did not look for a worker by name."""
    wt = add_worktree(repo, "fix-issue-43", "fix/issue-43")
    proc = run_alone(tmp_path, repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not wt.exists()
    assert "no worker container was looked for by name" in proc.stdout


def test_remove_worktree_without_the_library_still_says_nothing_was_here(tmp_path,
                                                                        other_repo):
    """Not looking for a worker is a could-not-tell, not a leftover (#843's split),
    so a run from the wrong checkout still exits 2 rather than WITH WARNINGS."""
    proc = run_alone(tmp_path, other_repo)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "Nothing removed" in proc.stderr


# --------------------------------------------------------- the two teardowns

def test_remove_worktree_removes_a_detected_worker_once_the_directory_is_gone(repo,
                                                                            tmp_path):
    """The issue's single signal: no worktree directory, no worker config, and the
    detected worker's container still up."""
    (repo / "pyproject.toml").write_text(HUEY_DEPS)
    name = "huey-consumer-fix-issue-43"
    docker = fake_docker(tmp_path, [name], owners={name: str(repo.parent / "proj-fix-issue-43")})

    proc = run_with(repo, ["fix-issue-43"], tmp_path, {**NOTHING_HELD, "docker": docker})

    assert complete(proc), proc.stdout + proc.stderr
    assert f"Removed container {name}" in proc.stdout
    assert (tmp_path / "docker-state").read_text() == ""


def test_remove_worktree_uses_a_configured_prefix(repo, tmp_path):
    (repo / ".worktree.json").write_text(json.dumps({"worker": {"container_prefix": "jobs"}}))
    (repo / "pyproject.toml").write_text(HUEY_DEPS)
    name = "jobs-fix-issue-43"
    docker = fake_docker(tmp_path, [name], owners={name: str(repo.parent / "proj-fix-issue-43")})

    proc = run_with(repo, ["fix-issue-43"], tmp_path, {**NOTHING_HELD, "docker": docker})

    assert complete(proc), proc.stdout + proc.stderr
    assert f"Removed container {name}" in proc.stdout


def test_remove_worktree_leaves_another_repos_same_named_worker_alone(repo, tmp_path):
    """huey's prefix does not encode the project, so another repo's worktree on a
    branch of the same name has a worker of the same name. Detection made every
    repo that mentions huey a candidate; ownership is what keeps it to this one."""
    (repo / "pyproject.toml").write_text(HUEY_DEPS)
    name = "huey-consumer-fix-issue-43"
    docker = fake_docker(tmp_path, [name], owners={name: "/srv/other-repo-fix-issue-43"})

    proc = run_with(repo, ["fix-issue-43"], tmp_path, {**NOTHING_HELD, "docker": docker})

    assert "belongs to another checkout" in proc.stdout, proc.stdout + proc.stderr
    assert (tmp_path / "docker-state").read_text() == f"{name}\n"


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


def run_prune(tmp_path, owner: str, script: Path = BIN / "prune-worktrees"):
    """prune-worktrees in a repo whose dead worktree left `huey-consumer-fix-issue-gone`,
    whose compose working_dir `docker inspect` reports as `owner`."""
    main = _prune_repo(tmp_path)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    docker = ('[[ "$1" == inspect ]] && { echo "' + owner + '"; exit 0; }\n'
              'for a in "$@"; do [[ "$a" == -a ]] && '
              '{ echo huey-consumer-fix-issue-gone; exit 0; }; done; exit 0')
    for name, body in (("docker", docker), ("qb-claimed", "exit 2"), ("qb-release", "exit 0")):
        (stubs / name).write_text(f"#!{BASH}\n{body}\n")
        (stubs / name).chmod(0o755)
    proc = subprocess.run(
        [str(script)], cwd=main, capture_output=True, text=True,
        env=_path_sandbox.sandbox_env(tmp_path, stubs, inherit_path=True))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc


@pytest.mark.skipif(GIT is None, reason="git must be on PATH")
def test_prune_worktrees_pairs_a_detected_worker_with_its_dead_worktree(tmp_path):
    proc = run_prune(tmp_path, str(tmp_path / "proj-fix-issue-gone"))
    assert "Orphan containers (1):" in proc.stdout, proc.stdout + proc.stderr
    assert "huey-consumer-fix-issue-gone" in proc.stdout


@pytest.mark.skipif(GIT is None, reason="git must be on PATH")
def test_prune_worktrees_does_not_claim_another_repos_same_named_worker(tmp_path):
    proc = run_prune(tmp_path, "/srv/other-repo-fix-issue-gone")
    assert "Orphan containers: none" in proc.stdout, proc.stdout + proc.stderr


@pytest.mark.skipif(GIT is None, reason="git must be on PATH")
def test_prune_worktrees_without_the_library_reports_workers_as_not_checked(tmp_path):
    """The harness degrades, never blocks (test_worktree_holder pins prune running
    with no helper beside it). What it could not check, it says it did not."""
    lone = _alone(tmp_path, "prune-worktrees")
    proc = run_prune(tmp_path, str(tmp_path / "proj-fix-issue-gone"), script=lone)
    assert "Orphan worker containers: NOT CHECKED" in proc.stdout, proc.stdout
    assert "Note: orphan worker containers" in proc.stdout
    assert "huey-consumer-fix-issue-gone" not in proc.stdout
