"""`qb-watch-job` (#851): not done, could not ask, and done are three answers, not one.

The end-to-end tests run the real script against a fake `ssh` that runs the remote
command in a local `sh`, so the probe's own exit codes are exercised as written, and
fails with ssh's 255 while a "down" file exists. `classify` is pure and pinned on its own.

Run: pytest harness/tests/test_qb_watch_job.py
"""

from __future__ import annotations

import http.server
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from _path_sandbox import sandbox_env

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "qb-watch-job"
_loader = importlib.machinery.SourceFileLoader("qb_watch_job", str(SCRIPT))
_spec = importlib.util.spec_from_loader("qb_watch_job", _loader)
w = importlib.util.module_from_spec(_spec)
sys.modules["qb_watch_job"] = w
_loader.exec_module(w)

BASH = shutil.which("bash")
TOOLS = ("sh", "tail", "dirname")

FAKE_SSH = f"""#!{BASH}
# Records its argv, fails like ssh while $FAKE_SSH_DOWN exists, else runs the command here.
printf '%s\\n' "$*" >> "$FAKE_SSH_LOG"
[[ -e "$FAKE_SSH_BANNER" ]] && echo "Welcome to zeus: last run rc=1"
if [[ -e "$FAKE_SSH_DOWN" ]]; then
    echo "ssh: connect to host zeus port 22: Connection refused" >&2
    exit 255
fi
while [[ $# -gt 0 && "$1" != "--" ]]; do shift; done
shift 2
exec sh -c "$1"
"""


@pytest.fixture
def env(tmp_path):
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    ssh = fakes / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(0o755)
    return sandbox_env(tmp_path, fakes, tools=TOOLS,
                       FAKE_SSH_LOG=str(tmp_path / "ssh.log"),
                       FAKE_SSH_DOWN=str(tmp_path / "ssh-down"),
                       FAKE_SSH_BANNER=str(tmp_path / "ssh-banner"))


def start(env, *args):
    return subprocess.Popen([sys.executable, str(SCRIPT), "--interval", "1", *args],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)


def wait_for(proc, text, limit=30):
    """Read the watcher's lines until one contains `text`; the lines so far."""
    seen, end = [], time.monotonic() + limit
    while time.monotonic() < end:
        line = proc.stdout.readline()
        if not line:
            break
        seen.append(line)
        if text in line:
            return "".join(seen)
    proc.kill()
    raise AssertionError(f"never printed {text!r}: {''.join(seen)!r}")


def run(env, *args, timeout=30):
    proc = start(env, *args)
    out, err = proc.communicate(timeout=timeout)
    return proc.returncode, out, err


def test_marker_that_appears_mid_watch_ends_it_done(env, tmp_path):
    marker = tmp_path / "gate.done"
    proc = start(env, "--host", "zeus", "--file", str(marker))
    wait_for(proc, "marker absent, not done yet")
    marker.write_text("stage 4 finished rc=0\n")
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == w.EXIT_OK
    assert "done: stage 4 finished rc=0" in out


def test_marker_matching_the_fail_regex_is_a_failure(env, tmp_path):
    marker = tmp_path / "gate.done"
    marker.write_text("sim run 1 ok\nsim run 2 rc=3\n")
    code, out, _ = run(env, "--host", "zeus", "--file", str(marker),
                       "--fail-regex", r"rc=[1-9]")
    assert code == w.EXIT_FAILED
    assert "FAILED: sim run 2 rc=3" in out


def test_ssh_failing_every_time_is_could_not_ask_not_absent(env, tmp_path):
    (tmp_path / "ssh-down").touch()
    code, out, _ = run(env, "--host", "zeus", "--file", str(tmp_path / "gate.done"),
                       "--max-failures", "3")
    assert code == w.EXIT_UNREACHABLE
    assert "could not ask (ssh failed: ssh: connect to host zeus" in out
    assert "could not ask 3 times in a row, giving up" in out
    assert "not done yet" not in out


def test_missing_marker_keeps_waiting_and_says_so_once(env, tmp_path):
    code, out, _ = run(env, "--host", "zeus", "--file", str(tmp_path / "gate.done"),
                       "--timeout", "0.06")
    assert code == w.EXIT_TIMEOUT
    assert out.count("marker absent, not done yet") == 1
    assert "could not ask" not in out


def test_until_regex_waits_past_a_marker_that_is_not_finished(env, tmp_path):
    marker = tmp_path / "gate.log"
    marker.write_text("stage fast: running\n")
    proc = start(env, "--host", "zeus", "--file", str(marker), "--until-regex", "DONE")
    wait_for(proc, "marker present, not finished yet")
    marker.write_text("stage fast: running\nDONE all stages\n")
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == w.EXIT_OK
    assert "done: DONE all stages" in out


def test_ssh_that_recovers_says_so_and_keeps_watching(env, tmp_path):
    down = tmp_path / "ssh-down"
    down.touch()
    marker = tmp_path / "gate.done"
    proc = start(env, "--host", "zeus", "--file", str(marker), "--max-failures", "20")
    wait_for(proc, "could not ask (ssh failed")
    down.unlink()
    wait_for(proc, "asking works again after")
    marker.write_text("ok\n")
    proc.communicate(timeout=30)
    assert proc.returncode == w.EXIT_OK


def test_tilde_in_a_remote_marker_is_that_hosts_home(env, tmp_path):
    home = Path(env["HOME"])
    (home / "sim-runs").mkdir(parents=True)
    (home / "sim-runs" / "run 1.done").write_text("ok\n")
    code, out, _ = run(env, "--host", "zeus", "--file", "~/sim-runs/run 1.done")
    assert code == w.EXIT_OK, out
    log = (tmp_path / "ssh.log").read_text()
    assert "BatchMode=yes" in log
    assert "-- zeus" in log


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_unreadable_marker_is_could_not_ask(env, tmp_path):
    marker = tmp_path / "gate.done"
    marker.write_text("ok\n")
    marker.chmod(0)
    code, out, _ = run(env, "--host", "zeus", "--file", str(marker), "--max-failures", "2")
    assert code == w.EXIT_UNREACHABLE
    assert "could not be read" in out


@pytest.mark.parametrize("remote", [True, False])
def test_pid_that_exits_ends_the_watch(env, remote):
    job = subprocess.Popen(["sleep", "30"])
    try:
        host = ["--host", "zeus"] if remote else []
        proc = start(env, *host, "--pid", str(job.pid))
        wait_for(proc, "process running, not done yet")
    finally:
        job.kill()
        job.wait()
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == w.EXIT_OK
    assert "process ended" in out


def test_local_marker_needs_no_ssh(env, tmp_path):
    marker = tmp_path / "local.done"
    marker.write_text("finished\n")
    code, out, _ = run(env, "--file", str(marker))
    assert code == w.EXIT_OK
    assert not (tmp_path / "ssh.log").exists()


@pytest.mark.parametrize("args", [
    ["--host", "-oProxyCommand=x", "--file", "m"],
    ["--pid", "12", "--fail-regex", "x"],
    ["--file", "m", "--until-regex", "("],
    ["--file", "m", "--max-failures", "0"],
    ["--file", "", "--host", "zeus"],
    ["--pid", "12", "--until-regex", ""],
    ["--pid", str(2**40)],
    ["--file", "m", "--interval", "nan"],
    ["--file", "m", "--interval", "0"],
    ["--file", "m", "--timeout", "inf"],
    [],
])
def test_bad_arguments_are_usage_errors(env, args):
    proc = subprocess.run([sys.executable, str(SCRIPT), *args], env=env,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == w.EXIT_USAGE


def board_server(got):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            got.append((self.path, json.loads(body), self.headers.get("X-Agent-Instance")))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def board_env(env, server):
    return {**env, "QUARTERBACK_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            "QUARTERBACK_TOKEN": "t", "QUARTERBACK_AGENT": "zeus",
            "QUARTERBACK_INSTANCE": "seat-1"}


def test_post_puts_a_failure_on_the_board_as_stuck(env, tmp_path):
    got = []
    server = board_server(got)
    try:
        marker = tmp_path / "gate.done"
        marker.write_text("rc=2\n")
        code, out, _ = run(board_env(env, server), "--host", "zeus", "--file", str(marker),
                           "--fail-regex", "rc=[1-9]", "--post")
    finally:
        server.shutdown()
    assert code == w.EXIT_FAILED
    assert len(got) == 1
    path, body, instance = got[0]
    assert path == "/post" and body["type"] == "stuck" and instance == "seat-1"
    assert "FAILED: rc=2" in body["summary"]


def test_classify_reads_failure_before_finished():
    fail, until = w.re.compile("rc=[1-9]"), w.re.compile("DONE")
    probe = w.Probe("present", "DONE\nrc=1\n")
    assert w.classify(probe, until, fail)[0] == "failed"
    assert w.classify(w.Probe("present", "DONE rc=0"), until, fail)[0] == "ok"
    assert w.classify(w.Probe("present", "running"), until, fail)[0] == "pending"
    assert w.classify(w.Probe("absent"), until, fail)[0] == "absent"


def test_remote_path_quotes_everything_but_the_home_prefix():
    assert w.remote_path("~/a b;rm x") == "\"$HOME\"/'a b;rm x'"
    assert w.remote_path("/tmp/$(id)") == "'/tmp/$(id)'"


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches any directory")
@pytest.mark.parametrize("remote", [True, False])
def test_marker_in_a_directory_that_cannot_be_searched_is_could_not_ask(env, tmp_path,
                                                                         remote):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "gate.done").write_text("ok\n")
    runs.chmod(0o600)
    try:
        host = ["--host", "zeus"] if remote else []
        code, out, _ = run(env, *host, "--file", str(runs / "gate.done"),
                           "--max-failures", "2")
    finally:
        runs.chmod(0o700)
    assert code == w.EXIT_UNREACHABLE
    assert "not done yet" not in out


def test_marker_whose_directory_does_not_exist_yet_is_not_done(env, tmp_path):
    code, out, _ = run(env, "--host", "zeus", "--file", str(tmp_path / "later" / "x.done"),
                       "--timeout", "0.03")
    assert code == w.EXIT_TIMEOUT
    assert "marker absent, not done yet" in out


@pytest.mark.parametrize("remote", [True, False])
def test_pid_that_is_not_running_at_the_start_is_not_a_finished_job(env, remote):
    job = subprocess.Popen(["true"])
    job.wait()
    host = ["--host", "zeus"] if remote else []
    code, out, _ = run(env, *host, "--pid", str(job.pid))
    assert code == w.EXIT_USAGE
    assert "process ended" not in out
    assert f"no pid {job.pid} is running there" in out


def test_login_shell_output_is_not_read_as_the_marker(env, tmp_path):
    (tmp_path / "ssh-banner").touch()
    marker = tmp_path / "gate.done"
    marker.write_text("all clean\n")
    code, out, _ = run(env, "--host", "zeus", "--file", str(marker),
                       "--fail-regex", "rc=[1-9]")
    assert code == w.EXIT_OK, out
    assert "done: all clean" in out


@pytest.mark.parametrize("remote", [True, False])
def test_only_the_last_64_kib_of_the_marker_is_read(env, tmp_path, remote):
    marker = tmp_path / "gate.log"
    marker.write_text("early rc=1\n" + "x" * 70000 + "\nfinished\n")
    host = ["--host", "zeus"] if remote else []
    code, out, _ = run(env, *host, "--file", str(marker), "--fail-regex", "rc=[1-9]")
    assert code == w.EXIT_OK, out
    assert "done: finished" in out


def test_timeout_is_posted_as_stuck_too(env, tmp_path):
    got = []
    server = board_server(got)
    try:
        code, _, _ = run(board_env(env, server), "--host", "zeus",
                         "--file", str(tmp_path / "gate.done"), "--timeout", "0.03", "--post")
    finally:
        server.shutdown()
    assert code == w.EXIT_TIMEOUT
    assert [body["type"] for _, body, _ in got] == ["stuck"]


def test_a_crash_in_the_watcher_is_its_own_exit_not_failed(monkeypatch):
    def boom(*_):
        raise OverflowError("probe exploded")

    monkeypatch.setattr(w, "probe_file", boom)
    assert w.main(["--file", "m", "--interval", "1"]) == w.EXIT_ERROR


def test_classify_refuses_an_error_rather_than_reading_it_as_done():
    with pytest.raises(ValueError):
        w.classify(w.Probe("error", error="ssh failed"), None, None)


def test_the_reported_match_is_the_most_recent_one():
    probe = w.Probe("present", "stage 1 rc=2\nretry\nstage 1 rc=3\n")
    assert w.classify(probe, None, w.re.compile("rc=[1-9]"))[1] == "FAILED: stage 1 rc=3"


def test_control_bytes_in_marker_content_never_reach_the_output():
    probe = w.Probe("present", "done \x1b]0;pwned\x07 \x1b[31mred\n")
    assert "\x1b" not in w.classify(probe, None, None)[1]
    assert "\x07" not in w.classify(probe, None, None)[1]


QB_HOOK = Path(__file__).resolve().parents[1] / "bin" / "qb-hook"


@pytest.mark.parametrize("label", ["seat-1", "seat 3", "..x", "aé-b", "a" * 60, "~~"])
def test_instance_slug_is_qb_hooks_rule(label):
    rule = next(ln for ln in QB_HOOK.read_text().splitlines() if ln.startswith("_key_slug()"))
    got = subprocess.run([BASH, "-c", f'{rule}\n_key_slug "$1"', "_", label],
                         capture_output=True, text=True, check=True,
                         env={**os.environ, "LC_ALL": "C.UTF-8"})
    assert w.key_slug(label) == got.stdout.rstrip("\n")  # as `$(…)` reads it
