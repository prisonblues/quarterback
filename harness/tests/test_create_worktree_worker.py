"""`create-worktree` provisions the worker but starts it only when asked (#853).

During lexray v22 one worktree's huey worker burned ~4 cores on zeus running
periodic jobs nobody had asked for (lexray#4458). Most worktrees are
code-and-test only, so the worker is written into docker-compose.yml under a
`worker` compose profile — out of every bare `docker compose up`, the script's or
anyone's later — and started only when `--with-worker` or `.worktree.json`'s
`worker.autostart` asks for it. The summary prints the one command that starts it.

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


REAL_DOCKER = shutil.which("docker")


def compose_up(calls: list[str]) -> list[str]:
    ups = [c for c in calls if c.startswith("compose") and c.endswith("up -d")]
    assert ups, f"docker compose up was never called: {calls}"
    return ups


def compose_file(tmp_path: Path) -> str:
    return (tmp_path / "myproj-feat-w" / "docker-compose.yml").read_text()


def worker_block(tmp_path: Path) -> str:
    return compose_file(tmp_path).split(f"  {WORKER}:", 1)[1].split("\nnetworks:", 1)[0]


def test_default_starts_the_app_and_not_the_worker(tmp_path):
    proc, calls = run_create(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert compose_up(calls) == ["compose up -d"]
    # A bare `up` leaves the worker out only because of its profile.
    assert 'profiles: ["worker"]' in worker_block(tmp_path)


def test_default_prints_the_command_that_starts_the_worker(tmp_path):
    proc, _ = run_create(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"docker compose up -d {WORKER}" in proc.stdout
    assert "not started" in proc.stdout


def test_worker_is_still_written_into_the_compose_file(tmp_path):
    proc, _ = run_create(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"container_name: {WORKER}" in compose_file(tmp_path)


def test_with_worker_asks_for_the_worker_profile(tmp_path):
    proc, calls = run_create(tmp_path, "--with-worker")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert compose_up(calls) == ["compose --profile worker up -d"]
    assert "not started" not in proc.stdout


def test_config_autostart_asks_for_the_worker_profile(tmp_path):
    proc, calls = run_create(tmp_path, worker={"type": "huey", "autostart": True})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert compose_up(calls) == ["compose --profile worker up -d"]


def test_no_worker_beats_config_autostart(tmp_path):
    proc, calls = run_create(tmp_path, "--no-worker", worker={"type": "huey", "autostart": True})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert compose_up(calls) == ["compose up -d"]
    assert "not started" in proc.stdout


def test_no_worker_type_is_unchanged(tmp_path):
    proc, calls = run_create(tmp_path, worker={"type": "none"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert compose_up(calls) == ["compose up -d"]
    assert "profiles:" not in compose_file(tmp_path)
    assert "not started" not in proc.stdout


def test_with_worker_and_no_worker_type_says_there_is_none(tmp_path):
    proc, calls = run_create(tmp_path, "--with-worker", worker={"type": "none"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "no worker to start" in proc.stdout
    assert compose_up(calls) == ["compose up -d"]


def test_with_worker_and_frontend_only_names_the_conflict(tmp_path):
    proc, calls = run_create(tmp_path, "--with-worker", "--frontend-only")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "--with-worker ignored: --frontend-only" in proc.stdout
    assert compose_up(calls) == ["compose up -d"]


def test_dry_run_says_the_worker_will_not_start(tmp_path):
    proc, calls = run_create(tmp_path, "--dry-run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "app only; the worker is not started" in proc.stdout
    assert not [c for c in calls if c.startswith("compose")]


def test_dry_run_with_worker_does_not_say_so(tmp_path):
    proc, _ = run_create(tmp_path, "--dry-run", "--with-worker")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Start Docker containers" in proc.stdout
    assert "the worker is not started" not in proc.stdout


@pytest.mark.parametrize("value", ["yes", "true", "false", 1, None], ids=repr)
def test_autostart_that_is_not_a_boolean_is_refused(tmp_path, value):
    """The JSON type decides: the STRING "true" is not the boolean, and `null` is
    absent, which is the default rather than an error."""
    proc, calls = run_create(tmp_path, worker={"type": "huey", "autostart": value})
    if value is None:
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert compose_up(calls) == ["compose up -d"]
        return
    assert proc.returncode != 0
    assert "worker.autostart must be true or false" in proc.stderr
    # Refused before anything was built, not merely before docker was reached.
    assert not (tmp_path / "myproj-feat-w").exists()
    assert not calls


@pytest.mark.skipif(REAL_DOCKER is None, reason="docker compose is needed to parse the file")
def test_real_compose_leaves_the_worker_out_of_a_bare_up(tmp_path):
    """The stub only records argv. This asks the real `docker compose` (no daemon
    needed for `config`) which services a bare `up` would start, and which the
    worker profile adds — so the generated file, not just the call, is pinned."""
    proc, _ = run_create(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    wt = tmp_path / "myproj-feat-w"

    def services(*profile: str) -> set[str]:
        out = subprocess.run(
            [REAL_DOCKER, "compose", *profile, "config", "--services"],
            cwd=wt,
            capture_output=True,
            text=True,
            timeout=60,
            env={**sandbox_env(tmp_path, inherit_path=True), "COMPOSE_PROFILES": ""},
        )
        if out.returncode != 0 and "is not a docker command" in out.stderr:
            pytest.skip("docker has no compose plugin here")
        assert out.returncode == 0, out.stderr
        return set(out.stdout.split())

    assert services() == {APP}
    assert services("--profile", "worker") == {APP, WORKER}
