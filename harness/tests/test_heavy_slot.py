"""`heavy-slot` (#874), run for real against a fake meminfo and a private queue. The `box`
fixture pins every config key, so no host config file can change a result.
"""

from __future__ import annotations

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
    first, _ = hold(env, "--min", "8G", "--label", "first")
    pipes = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL}
    big = spawn(env, "hold", "--min", "6G", "--label", "big", **pipes)
    wait_for(lambda: len(queued(tmp)) == 1, "big to queue")
    small = spawn(env, "hold", "--min", "2G", "--label", "small", **pipes)
    wait_for(lambda: len(queued(tmp)) == 2, "small to queue behind big")
    time.sleep(2 * hs.POLL_SECONDS + 0.5)  # two polls in which small could have gone
    held = [h["label"] for h in json.loads(run(env, "status", "--json").stdout)["holders"]]
    assert held == ["first"], "4G was free and small wanted 2G, but it must wait behind big"
    release(first)
    big.stdout.readline(), small.stdout.readline()
    release(big), release(small)


def test_low_memavailable_holds_a_job_back_even_inside_the_budget(box):
    env, set_mem, _ = box
    set_mem(16, 3)  # the budget is untouched, but only 3 - 2 = 1G is really free
    r = run(env, "run", "--min", "2G", "--timeout", "0.05", "--", "true")
    assert r.returncode == hs.EXIT_TIMEOUT and "MemAvailable 3.0G" in r.stderr


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


@pytest.mark.parametrize(("cmd", "status"), [
    (["sh", "-c", "kill -TERM $$"], -signal.SIGTERM),
    (["sh", "-c", "exit 7"], 7),
    (["/nonexistent/tool"], 127),
])
def test_run_exits_as_its_command_does_and_leaves_no_reservation(box, cmd, status):
    env, _, tmp = box
    assert run(env, "run", "--min", "1G", "--", *cmd).returncode == status
    assert reserved(env) == 0  # a failed exec removes its file; the others end with CMD


@pytest.mark.parametrize(("budget", "want", "cap"), [
    ("12G", "13G", "12.0G"),
    ("40G", "15G", "14.0G"),  # above the box: MemTotal 16G - min_free 2G is the most ever free
])
def test_a_min_the_host_can_never_grant_is_refused_at_once(box, budget, want, cap):
    env, _, _ = box
    env["HEAVY_SLOT_BUDGET"] = budget
    r = run(env, "run", "--min", want, "--timeout", "0.05", "--", "true", timeout=5)
    assert r.returncode == hs.EXIT_NEVER and f"can ever grant ({cap})" in r.stderr


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


def test_a_caller_that_closed_stderr_still_gets_its_place_and_its_grant(box):
    """With fd 2 closed, sys.stderr is None, and the "waiting" message must not go to
    stdout, which is where the caller reads the grant."""
    env, _, tmp = box
    first, _ = hold(env, "--min", "10G")
    waiter = spawn(env, "hold", "--min", "4G", stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                   preexec_fn=lambda: os.close(2))
    wait_for(lambda: len(queued(tmp)) == 1, "the waiter to queue")
    time.sleep(hs.POLL_SECONDS + 0.5)  # past its "waiting" message
    release(first)
    assert json.loads(waiter.stdout.readline())["grant_bytes"] == 4 * G
    release(waiter)
