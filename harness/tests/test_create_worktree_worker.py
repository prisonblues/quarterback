"""`create-worktree` provisions the worker but starts it only when asked (#853).

During lexray v22 one worktree's huey worker burned ~4 cores on zeus running
periodic jobs nobody had asked for (lexray#4458). Most worktrees are
code-and-test only, so the worker is written into docker-compose.yml and left
stopped unless `--with-worker` or `.worktree.json`'s `worker.autostart` asks for
it, and the summary prints the one command that starts it later.

Each test runs the REAL script against a throwaway repo with a `docker` stub that
records every call, and reads back what `docker compose up` was asked to start.
The board tools are stubs too, and the environment comes from `sandbox_env`, so
nothing here reaches a daemon or the board (#528).

Run: pytest harness/tests
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from _path_sandbox import sandbox_env

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "create-worktree"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    BASH is None or shutil.which("git") is None or shutil.which("jq") is None,
    reason="bash, git and jq are needed to run create-worktree",
)

APP = "myproj-feat-w"
WORKER = "huey-consumer-feat-w"

DOCKER_STUB = """#!{bash}
printf '%s\\n' "$*" >> {log}
case "${{1:-}}" in
    ps)      echo {app} ;;
    images)  echo myproj ;;
    network) echo myproj_web_network ;;
    inspect) echo '{{}}' ;;
esac
exit 0
"""

BOARD_TOOLS = ("worktree-holder", "qb-admit", "qb-claim", "qb-release", "qb-mode", "qb-catchup")


def make_repo(tmp_path: Path, worker: dict | None) -> Path:
    repo = tmp_path / "myproj"
    repo.mkdir()
    (repo / "Dockerfile").write_text("FROM scratch\n")
    config = {
        "project": "myproj",
        "app_port": 5005,
        "base_port": 5680,
        "symlinks": [],
        "copies": [],
    }
    if worker is not None:
        config["worker"] = worker
    (repo / ".worktree.json").write_text(json.dumps(config))
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    return repo


def run_create(tmp_path: Path, *args: str, worker: dict | None = None):
    """Run create-worktree; return (process, the `docker` calls it made)."""
    repo = make_repo(tmp_path, {"type": "huey"} if worker is None else worker)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log = tmp_path / "docker.calls"
    (stubs / "docker").write_text(DOCKER_STUB.format(bash=BASH, log=log, app=APP))
    for tool in BOARD_TOOLS:
        (stubs / tool).write_text(f"#!{BASH}\nexit 0\n")
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    env = sandbox_env(tmp_path, stubs, inherit_path=True)
    proc = subprocess.run(
        [BASH, str(SCRIPT), "--no-workspace", "--no-fetch", "--no-claim", *args, "feat-w"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def compose_up(calls: list[str]) -> list[str]:
    ups = [c for c in calls if c.startswith("compose up")]
    assert ups, f"docker compose up was never called: {calls}"
    return ups


def test_default_starts_the_app_and_not_the_worker(tmp_path):
    proc, calls = run_create(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert compose_up(calls) == [f"compose up -d {APP}"]


def test_default_prints_the_command_that_starts_the_worker(tmp_path):
    proc, _ = run_create(tmp_path)
    assert f"docker compose up -d {WORKER}" in proc.stdout
    assert "not started" in proc.stdout


def test_worker_is_still_written_into_the_compose_file(tmp_path):
    proc, _ = run_create(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    compose = (tmp_path / "myproj-feat-w" / "docker-compose.yml").read_text()
    assert f"container_name: {WORKER}" in compose


def test_with_worker_starts_every_service(tmp_path):
    proc, calls = run_create(tmp_path, "--with-worker")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert compose_up(calls) == ["compose up -d"]
    assert "not started" not in proc.stdout


def test_config_autostart_starts_every_service(tmp_path):
    proc, calls = run_create(tmp_path, worker={"type": "huey", "autostart": True})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert compose_up(calls) == ["compose up -d"]


def test_no_worker_beats_config_autostart(tmp_path):
    proc, calls = run_create(tmp_path, "--no-worker", worker={"type": "huey", "autostart": True})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert compose_up(calls) == [f"compose up -d {APP}"]


def test_no_worker_type_starts_everything_as_before(tmp_path):
    proc, calls = run_create(tmp_path, worker={"type": "none"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert compose_up(calls) == ["compose up -d"]
    assert "not started" not in proc.stdout


def test_with_worker_and_no_worker_type_says_there_is_none(tmp_path):
    proc, calls = run_create(tmp_path, "--with-worker", worker={"type": "none"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "no worker to start" in proc.stdout
    assert compose_up(calls) == ["compose up -d"]


def test_autostart_that_is_not_a_boolean_is_refused(tmp_path):
    proc, calls = run_create(tmp_path, worker={"type": "huey", "autostart": "yes"})
    assert proc.returncode != 0
    assert "worker.autostart must be true or false" in proc.stderr
    assert not [c for c in calls if c.startswith("compose")]
