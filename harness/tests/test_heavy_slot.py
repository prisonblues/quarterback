"""`heavy-slot` (#874): admission by memory, first come first served, released by the kernel.

The script runs for real against a fake /proc/meminfo and a private queue directory. The
`box` fixture sets every config key from the environment, which wins over both config
files, so a real /etc/heavy-slot.conf cannot change a result. A holder is a `hold`
process kept alive by an open stdin; closing the pipe ends it, and SIGKILL is a crash.

Run: pytest harness/tests/test_heavy_slot.py
"""

from __future__ import annotations

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
    order = tmp / "order"
    big = spawn(env, "run", "--min", "6G", "--", "sh", "-c", f"echo big >> {order}; sleep 2",
                stderr=subprocess.DEVNULL)
    wait_for(lambda: len(queued(tmp)) == 1, "big to queue")
    small = spawn(env, "run", "--min", "2G", "--", "sh", "-c", f"echo small >> {order}",
                  stderr=subprocess.DEVNULL)
    wait_for(lambda: len(queued(tmp)) == 2, "small to queue behind big")
    time.sleep(2 * hs.POLL_SECONDS + 0.5)  # two polls in which small could have gone
    assert not order.exists(), "2G was free, but small must wait behind big"
    release(first)
    assert big.wait(timeout=15) == 0 and small.wait(timeout=15) == 0
    assert order.read_text().split() == ["big", "small"]


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
    env, _, _ = box
    job = spawn(env, "run", "--min", "4G", "--", "sleep", "30")
    wait_for(lambda: reserved(env) == 4 * G, "the run to be granted")
    job.kill()  # no wrapper is left to outlive or proxy it: the pid IS the command
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
    assert r.returncode == hs.EXIT_NEVER and "more than the whole budget of 12.0G" in r.stderr


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
    release(first)
    assert "holds    10.0G" in out and "lexray zeus-1" in out
    assert "waits     4.0G" in out and "panel?#861" in out


def test_an_entry_that_is_not_a_json_object_counts_for_nothing(box):
    env, _, tmp = box
    run(env, "status")  # creates the queue directory
    fd = os.open(tmp / "q" / "res-junk", os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.write(fd, b"\xff[1, 2]")
    try:
        assert reserved(env) == 0
    finally:
        os.close(fd)


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
    first, _ = hold(env, "--min", "9G")
    r = run(env, "run", "--min", "3G", "--unit", "2G", "--timeout", "0.05", "--", "true")
    release(first)
    assert r.returncode == hs.EXIT_TIMEOUT  # 3G fits in the 3 left, its 4G of units does not


def test_config_files_are_read_in_order_and_the_environment_wins(tmp_path, monkeypatch):
    etc, user = tmp_path / "etc.conf", tmp_path / "user.conf"
    etc.write_text("reserve = 10G\nbudget = 40G  # whole box\nbogus = 1\n")
    user.write_text("reserve=8G\n")
    monkeypatch.setattr(hs, "CONFIG_FILES", (str(etc), str(user)))
    cfg = hs.load_config({"HEAVY_SLOT_MIN_FREE": "3G"})
    assert (cfg["budget"], cfg["reserve"], cfg["min_free"]) == ("40G", "8G", "3G")
    assert "bogus" not in cfg


def test_the_budget_defaults_to_memtotal_minus_the_reserve():
    assert hs.budget_of({"budget": "", "reserve": "12G"}, 61 * G) == 49 * G
    assert hs.budget_of({"budget": "30G", "reserve": "12G"}, 61 * G) == 30 * G
