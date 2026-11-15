"""`heavy-slot` (#874): admission by memory, first come first served, released by the kernel.

The script runs for real against a fake /proc/meminfo and a private queue directory. The
`box` fixture sets every config key from the environment, which wins over both config
files, so a real /etc/heavy-slot.conf cannot change a result. A holder is a `hold`
process kept alive by an open stdin; closing the pipe ends it, and SIGKILL is a crash.

Run: pytest harness/tests/test_heavy_slot.py
"""

from __future__ import annotations

import ast
import fcntl
import importlib.machinery
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "heavy-slot"
_loader = importlib.machinery.SourceFileLoader("heavy_slot", str(SCRIPT))
_spec = importlib.util.spec_from_loader("heavy_slot", _loader)
hs = importlib.util.module_from_spec(_spec)
sys.modules["heavy_slot"] = hs
_loader.exec_module(hs)

G = 1 << 30


@pytest.fixture
def box(tmp_path):
    """A 12G budget on a box with 12G available and a 2G floor."""
    meminfo = tmp_path / "meminfo"

    def set_mem(total_g: float, avail_g: float) -> None:
        meminfo.write_text(f"MemTotal: {int(total_g * 1048576)} kB\n"
                           f"MemAvailable: {int(avail_g * 1048576)} kB\n")

    set_mem(16, 12)
    env = {k: v for k, v in os.environ.items() if not k.startswith("HEAVY_SLOT_")}
    pinned = {"dir": str(tmp_path / "q"), "budget": "12G", "reserve": "4G", "min_free": "2G"}
    env.update({f"HEAVY_SLOT_{k.upper()}": v for k, v in pinned.items()},
               HEAVY_SLOT_MEMINFO=str(meminfo), HOME=str(tmp_path))
    assert set(pinned) == set(hs.DEFAULTS), "pin every config key, or a host file leaks in"
    return env, set_mem, tmp_path


def run(env, *args, timeout=20):
    return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True,
                          text=True, timeout=timeout)


def spawn(env, *args, **kw):
    return subprocess.Popen([sys.executable, str(SCRIPT), *args], env=env, **kw)


def hold(env, *args):
    """Start a holder; returns (process, grant) once it has been granted."""
    p = spawn(env, "hold", *args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
              stderr=subprocess.PIPE, text=True)
    line = p.stdout.readline()
    assert line, p.stderr.read()
    return p, json.loads(line)


def release(p):
    p.stdin.close()
    p.wait(timeout=10)


def queued(tmp) -> list[str]:
    return [f for f in os.listdir(tmp / "q") if f.startswith("wait-")]


def reserved(env) -> int:
    return json.loads(run(env, "status", "--json").stdout)["reserved"]


def wait_for(predicate, what: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def test_a_lone_job_gets_its_max_in_whole_units_and_sees_the_grant(box):
    env, _, _ = box
    r = run(env, "run", "--min", "2G", "--max", "7G", "--unit", "2G", "--",
            "sh", "-c", "echo $HEAVY_SLOT_GRANT_BYTES $HEAVY_SLOT_UNITS")
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == [str(6 * G), "3"]


def test_the_grant_is_what_is_left_of_the_budget(box):
    env, _, _ = box
    first, _ = hold(env, "--min", "8G")
    second, grant = hold(env, "--min", "2G", "--max", "10G", "--unit", "1G")
    release(second)
    release(first)
    assert grant == {"grant_bytes": 4 * G, "units": 4}


def test_a_job_whose_min_does_not_fit_waits_says_so_once_and_runs_when_room_frees(box):
    env, _, tmp = box
    first, _ = hold(env, "--min", "10G")
    done = tmp / "done"
    waiter = spawn(env, "run", "--min", "4G", "--label", "suite", "--", "touch", str(done),
                   stderr=subprocess.PIPE, text=True)
    wait_for(lambda: len(queued(tmp)) == 1, "the run to queue")
    assert not done.exists()
    release(first)
    assert waiter.wait(timeout=15) == 0 and done.exists()
    said = waiter.stderr.read()
    assert said.count("waiting") == 1 and "suite waiting for 4.0G" in said


def test_a_small_job_does_not_overtake_a_big_one_already_waiting(box):
    env, _, tmp = box
    first, _ = hold(env, "--min", "8G")
    pipes = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL}
    big = spawn(env, "hold", "--min", "6G", "--label", "big", **pipes)
    wait_for(lambda: len(queued(tmp)) == 1, "big to queue")
    small = spawn(env, "hold", "--min", "2G", "--label", "small", **pipes)
    wait_for(lambda: len(queued(tmp)) == 2, "small to queue behind big")
    time.sleep(2 * hs.POLL_SECONDS + 0.5)  # two polls in which small could have gone
    held = json.loads(run(env, "status", "--json").stdout)["holders"]
    assert [h["label"] for h in held] == [first.args[-1]] or len(held) == 1, held
    release(first)
    big.stdout.readline(), small.stdout.readline()
    held = json.loads(run(env, "status", "--json").stdout)["holders"]
    granted_at = {h["label"]: os.stat(tmp / "q" / h["file"]).st_ctime_ns for h in held}
    release(big), release(small)
    assert granted_at["big"] < granted_at["small"], granted_at


def test_low_memavailable_holds_a_job_back_even_inside_the_budget(box):
    env, set_mem, _ = box
    set_mem(16, 3)  # the budget is untouched, but only 3 - 2 = 1G is really free
    r = run(env, "run", "--min", "2G", "--timeout", "0.05", "--", "true")
    assert r.returncode == hs.EXIT_TIMEOUT and "MemAvailable 3.0G" in r.stderr


def test_a_killed_holder_stops_counting(box):
    env, _, _ = box
    first, _ = hold(env, "--min", "10G")
    first.send_signal(signal.SIGKILL)
    first.wait(timeout=10)
    assert reserved(env) == 0


def test_a_released_hold_frees_its_memory_and_removes_its_file(box):
    env, _, tmp = box
    first, _ = hold(env, "--min", "10G")
    release(first)
    assert reserved(env) == 0 and not [f for f in os.listdir(tmp / "q") if f[0] != "."]


def test_run_becomes_its_command_which_holds_the_reservation_until_it_exits(box):
    env, _, tmp = box
    pidfile = tmp / "pid"
    job = spawn(env, "run", "--min", "4G", "--", "sh", "-c", f"echo $$ > {pidfile}; exec sleep 30")
    wait_for(lambda: pidfile.exists() and pidfile.read_text().strip(), "the command to start")
    assert int(pidfile.read_text()) == job.pid  # no wrapper: the run's pid IS the command
    assert reserved(env) == 4 * G
    job.kill()
    job.wait(timeout=10)
    assert reserved(env) == 0


def test_ctrl_c_reaches_the_command(box):
    env, _, tmp = box
    ready = tmp / "ready"
    job = spawn(env, "run", "--min", "1G", "--", "sh", "-c",
                f"trap 'exit 9' INT; touch {ready}; while :; do sleep 0.1; done")
    wait_for(ready.exists, "the command to start")
    job.send_signal(signal.SIGINT)
    assert job.wait(timeout=10) == 9


@pytest.mark.parametrize(("cmd", "status"), [
    (["sh", "-c", "kill -TERM $$"], -signal.SIGTERM),
    (["sh", "-c", "exit 7"], 7),
    (["/nonexistent/tool"], 127),
])
def test_run_exits_as_its_command_does(box, cmd, status):
    env, _, _ = box
    assert run(env, "run", "--min", "1G", "--", *cmd).returncode == status


def test_a_min_bigger_than_the_whole_budget_is_refused_at_once(box):
    env, _, _ = box
    r = run(env, "run", "--min", "13G", "--", "true", timeout=5)
    assert r.returncode == hs.EXIT_NEVER and "more than this host can ever grant (12.0G)" in r.stderr


def test_a_fixed_budget_above_the_box_is_capped_by_memtotal_less_the_floor(box):
    env, _, _ = box
    env["HEAVY_SLOT_BUDGET"] = "40G"  # MemTotal 16G - min_free 2G: 14G is the most ever free
    r = run(env, "run", "--min", "15G", "--timeout", "0.05", "--", "true", timeout=5)
    assert r.returncode == hs.EXIT_NEVER and "(14.0G)" in r.stderr


def test_a_timed_out_waiter_leaves_no_queue_entry_behind(box):
    env, _, tmp = box
    first, _ = hold(env, "--min", "10G")
    r = run(env, "run", "--min", "4G", "--timeout", "0.05", "--", "true")
    release(first)
    assert r.returncode == hs.EXIT_TIMEOUT and queued(tmp) == []


def test_queue_files_are_readable_by_other_users_whatever_the_umask(box):
    env, _, tmp = box
    old = os.umask(0o077)
    try:
        first, _ = hold(env, "--min", "2G")
    finally:
        os.umask(old)
    modes = {f: os.stat(tmp / "q" / f).st_mode & 0o777 for f in os.listdir(tmp / "q")}
    release(first)
    assert modes and set(modes.values()) == {0o644}, modes


def test_status_lists_holders_and_waiters(box):
    env, _, tmp = box
    first, _ = hold(env, "--min", "10G", "--label", "lexray zeus-1")
    waiter = spawn(env, "hold", "--min", "4G", "--label", "panel\n#861", stdin=subprocess.PIPE,
                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    wait_for(lambda: len(queued(tmp)) == 1, "the waiter to queue")
    out = run(env, "status").stdout
    waiter.kill()
    waiter.wait(timeout=10)
    release(first)
    assert "holds    10.0G" in out and "lexray zeus-1" in out
    assert "waits     4.0G" in out and "panel?#861" in out


@pytest.mark.parametrize("args", [
    ["run", "--min", "lots", "--", "true"],
    ["run", "--min", "4G", "--max", "2G", "--", "true"],
    ["run", "--min", "1G", "--"],
    ["run", "--min", "1G", "--timeout", "nan", "--", "true"],
    ["run", "--min", "1e999G", "--", "true"],
])
def test_bad_requests_are_usage_errors(box, args):
    env, _, _ = box
    assert run(env, *args, timeout=5).returncode == hs.EXIT_USAGE


def test_an_unreadable_meminfo_is_a_usage_error_not_a_traceback(box):
    env, _, tmp = box
    env["HEAVY_SLOT_MEMINFO"] = str(tmp / "missing")
    r = run(env, "run", "--min", "1G", "--", "true")
    assert r.returncode == hs.EXIT_USAGE and "cannot read MemTotal" in r.stderr


def test_a_min_is_rounded_up_to_whole_units(box):
    env, _, _ = box
    # 11G fits the 12G budget, but 11G in 5G units is 15G, which never can: refused at
    # once. Unrounded, it would wait (the grant floors to 10G) until the timeout instead.
    r = run(env, "run", "--min", "11G", "--unit", "5G", "--timeout", "0.05", "--", "true")
    assert r.returncode == hs.EXIT_NEVER


def test_config_files_are_read_in_order_and_the_environment_wins(tmp_path, monkeypatch):
    etc, user = tmp_path / "etc.conf", tmp_path / "user.conf"
    etc.write_text("reserve = 10G\nbudget = 40G  # whole box\nmin_free = 1G\nbogus = 1\n")
    user.write_text("reserve=8G\nmin_free=5G\n")
    monkeypatch.setattr(hs, "CONFIG_FILES", (str(etc), str(user)))
    cfg = hs.load_config({"HEAVY_SLOT_MIN_FREE": "3G"})
    assert (cfg["budget"], cfg["reserve"], cfg["min_free"]) == ("40G", "8G", "3G")
    assert "bogus" not in cfg


def test_the_budget_defaults_to_memtotal_minus_the_reserve():
    assert hs.budget_of({"budget": "", "reserve": "12G"}, 61 * G) == 49 * G
    assert hs.budget_of({"budget": "30G", "reserve": "12G"}, 61 * G) == 30 * G


def _signal_masks(cmd, env, ignore_int):
    """SigIgn/SigCgt of a process `cmd` starts, launched with SIGINT ignored or not."""
    def setup():
        signal.signal(signal.SIGINT, signal.SIG_IGN if ignore_int else signal.SIG_DFL)
    out = subprocess.run(cmd, env=env, capture_output=True, text=True, preexec_fn=setup,
                         timeout=20).stdout
    return [line for line in out.splitlines() if line.startswith(("SigIgn", "SigCgt"))]


@pytest.mark.parametrize("ignore_int", [False, True])
def test_the_command_gets_the_signal_dispositions_a_plain_launch_would(box, ignore_int):
    """`run` is as transparent as subprocess: what the caller ignored stays ignored, and the
    two signals CPython ignores at start-up go back to default."""
    env, _, _ = box
    probe = ["grep", "^Sig\\(Ign\\|Cgt\\)", "/proc/self/status"]
    via_run = _signal_masks([sys.executable, str(SCRIPT), "run", "--min", "1G", "--", *probe],
                            env, ignore_int)
    assert via_run and via_run == _signal_masks(probe, env, ignore_int)


def _closed_std():
    for fd in (0, 1, 2):
        os.close(fd)


def test_a_caller_with_fds_0_to_2_closed_hands_the_command_its_lock_above_them(box):
    env, _, tmp = box
    out = tmp / "fds"
    subprocess.run([sys.executable, str(SCRIPT), "run", "--min", "1G", "--", "sh", "-c",
                    f"for f in 0 1 2 3; do echo $f $(readlink /proc/$$/fd/$f); done > {out}"],
                   env=env, preexec_fn=_closed_std, timeout=20)
    fds = dict(line.split(" ", 1) for line in out.read_text().splitlines())
    assert not any("res-" in fds.get(f, "") for f in "012") and "res-" in fds["3"], fds


@pytest.mark.parametrize(("args", "status"), [
    (["run", "--min", "13G", "--", "true"], 3),  # its "can never fit" message has nowhere to go
    (["run", "--min", "1G", "--", "/nonexistent/tool"], 127),
    (["hold", "--min", "1G"], 2),  # a grant nobody can read is not held
])
def test_closed_standard_fds_cost_the_message_never_the_exit_status(box, args, status):
    env, _, _ = box
    r = subprocess.run([sys.executable, str(SCRIPT), *args], env=env, preexec_fn=_closed_std,
                       timeout=20)
    assert r.returncode == status


def _plant(tmp, name, raw):
    fd = os.open(tmp / "q" / name, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.write(fd, raw)
    return fd


@pytest.mark.parametrize("raw", [
    b"[" * 4096,
    b"\xff\xfe",
    b'{"bytes": "lots"}',
    b'{"bytes": 1, "min": true, "max": 1, "pid": 1, "uid": 0, "label": "", "since": ""}',
    b'{"bytes": 1000000000000000000000000000000}',  # an int no formatter should meet
    b'{"min": 4, "max": 2, "pid": 1, "uid": 0, "label": "", "since": ""}',
])
def test_a_hostile_record_counts_for_nothing_holds_no_place_and_crashes_no_reader(box, raw):
    env, _, tmp = box
    run(env, "status")
    fds = [_plant(tmp, "res-hostile", raw), _plant(tmp, f"wait-{0:020d}-1", raw)]
    try:
        status = run(env, "status", "--json")
        assert status.returncode == 0 and json.loads(status.stdout)["reserved"] == 0
        assert json.loads(status.stdout)["waiters"] == []
        assert run(env, "status").returncode == 0
        # the planted wait- file sorts first; a job queued behind it would never go
        assert run(env, "run", "--min", "1G", "--timeout", "0.1", "--", "true").returncode == 0
    finally:
        for fd in fds:
            os.close(fd)


def test_a_planted_label_reaches_status_as_printable_text(box):
    env, _, tmp = box
    run(env, "status")
    fd = _plant(tmp, "res-planted", json.dumps({
        "bytes": 1, "min": 1, "max": 1, "pid": 1, "uid": 0, "since": "",
        "label": "\x1b]0;owned\x07\x1b[2J"}).encode())
    try:
        out = run(env, "status").stdout
        assert "\x1b" not in out and "?]0;owned??[2J" in out
    finally:
        os.close(fd)


def test_a_command_that_cannot_start_leaves_no_reservation_behind(box):
    env, _, tmp = box
    assert run(env, "run", "--min", "1G", "--", "/nonexistent/tool").returncode == 127
    assert not [f for f in os.listdir(tmp / "q") if f.startswith("res-")]


def test_a_fifo_planted_in_the_queue_cannot_block_a_reader(box):
    env, _, tmp = box
    run(env, "status")
    os.mkfifo(tmp / "q" / "res-planted")
    assert run(env, "status", "--json", timeout=5).returncode == 0


def test_a_long_label_still_leaves_a_record_a_reader_can_parse(box):
    env, _, _ = box
    first, _ = hold(env, "--min", "2G", "--label", "x" * 10000)
    try:
        (holder,) = json.loads(run(env, "status", "--json").stdout)["holders"]
        assert holder["bytes"] == 2 * G and len(holder["label"]) == hs.LABEL_MAX
    finally:
        release(first)



def test_every_message_goes_through_the_closed_fd_safe_writer():
    tree = ast.parse(SCRIPT.read_text())
    prints = [n.lineno for n in ast.walk(tree)
              if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "print"]
    assert prints == [], f"print() at lines {prints}: use _say, which survives a closed fd"
