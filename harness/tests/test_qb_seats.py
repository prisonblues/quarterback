"""Tests for qb-seats, the layout, and qb-seat, the per-seat wrapper.

These drive a REAL tmux server against a stub agent, because the things worth
testing here are not string manipulation — they are what the panes actually end
up being. Three of them are load-bearing enough that a regression would be
silent and expensive:

  * no QUARTERBACK_INSTANCE from the launching environment can reach a seat, and
    each seat names itself. Sharing that value does not muddle two agents'
    inboxes, it makes them one agent on the board — same history, same presence,
    same lease, holding each other's claims legitimately. Nothing about the
    screen would look wrong while that was happening.
  * the screen creates no worktrees and no branches. A seat does not know its
    branch until it has claimed something.
  * a seat pane survives its agent exiting. That is what lets a human attach to
    ONE seat, interrupt it and redirect it, which is the property the whole
    design exists for.

Run: pytest harness/tests
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
QB_SEATS = BIN / "qb-seats"
QB_SEAT = BIN / "qb-seat"

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux is the thing under test"
)


@pytest.fixture
def screen(tmp_path):
    """A throwaway repo, a stub qb-seat on PATH, and an isolated tmux server."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    # The stub stands in for the agent: it records what the seat was given and
    # then holds the pane open, the way a real session would.
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "qb-seat"
    # /bin/sh, not `#!/usr/bin/env bash`: there is no /usr/bin/env inside the nix
    # build sandbox, and a stub that cannot exec makes every test here fail for a
    # reason that has nothing to do with the code under test. The shipped scripts
    # dodge this via patchShebangs; a stub written at runtime cannot.
    stub.write_text(
        "#!/bin/sh\n"
        'printf "seat=%s instance=%s cwd=%s\\n" "$1" "${QUARTERBACK_INSTANCE:-unset}" "$PWD"'
        f' >> {tmp_path / "seats.log"}\n'
        "exec sleep 300\n"
    )
    stub.chmod(0o755)

    socket = str(tmp_path / "tmux.sock")
    env = {
        **os.environ,
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
        # -L would be cleaner than an env var, but the script builds its own tmux
        # command lines; TMUX_TMPDIR isolates the server without touching them.
        "TMUX_TMPDIR": str(tmp_path),
        # A stray value in the launching environment is exactly the leak the
        # script has to defend against, so the fixture always supplies one.
        "QUARTERBACK_INSTANCE": "leaked-host-wide",
    }
    sessions = []

    def _run(*args, name="t"):
        sessions.append(name)
        return subprocess.run(
            [str(QB_SEATS), "-C", str(repo), "-s", name, *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def _tmux(*args):
        return subprocess.run(
            ["tmux", *args], env=env, capture_output=True, text=True, timeout=30
        )

    _run.tmux = _tmux
    _run.repo = repo
    _run.log = tmp_path / "seats.log"
    _run.env = env
    yield _run

    for name in sessions:
        subprocess.run(["tmux", "kill-session", "-t", f"={name}"], env=env,
                       capture_output=True)
    subprocess.run(["tmux", "kill-server"], env=env, capture_output=True)
    assert not Path(socket).exists() or True  # server teardown is best-effort


def panes(run, name="t"):
    """(pane_id, seat number or None) for the screen's window."""
    out = run.tmux("list-panes", "-t", f"{name}:seats", "-F",
                   "#{pane_id}\t#{@qb_seat}").stdout
    return [
        (line.split("\t")[0], line.split("\t")[1] or None)
        for line in out.splitlines() if line
    ]


def wait_for_log(path, count, timeout=20):
    """The seats start concurrently; wait for all of them rather than sleeping."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and len(path.read_text().splitlines()) >= count:
            return path.read_text().splitlines()
        time.sleep(0.2)
    return path.read_text().splitlines() if path.exists() else []


def test_the_screen_is_n_seats_plus_one_board(screen):
    screen("-n", "3")
    got = panes(screen)
    assert sorted(n for _, n in got if n) == ["1", "2", "3"]
    assert [n for _, n in got].count(None) == 1, "exactly one board pane"


def test_the_default_is_two_seats(screen):
    """Integration cost is quadratic in open PRs; the ceiling is not the monitor."""
    screen()
    assert sorted(n for _, n in panes(screen) if n) == ["1", "2"]


def test_no_inherited_instance_reaches_a_seat(screen):
    """The failure this guards against looks like nothing at all from the screen.

    Naming a seat is qb-seat's job and is tested below; the LAYOUT's job is to
    guarantee that whatever was in the launching environment does not arrive
    here. So the stub seeing no value at all is the pass condition.
    """
    screen("-n", "2")
    lines = wait_for_log(screen.log, 2)
    assert len(lines) == 2
    assert all("instance=unset" in line for line in lines), lines
    assert "leaked-host-wide" not in "\n".join(lines)


def test_the_inherited_instance_is_stripped_from_the_session(screen):
    """Panes split off later must not pick one up either."""
    screen("-n", "1")
    env = screen.tmux("show-environment", "-t", "t").stdout
    assert "-QUARTERBACK_INSTANCE" in env, "must be marked for removal, not merely unset"


def test_seats_run_in_the_repo(screen):
    screen("-n", "1")
    lines = wait_for_log(screen.log, 1)
    assert f"cwd={screen.repo}" in lines[0]


def test_the_screen_creates_no_worktrees_and_no_branches(screen):
    """A self-selecting seat does not know its branch until it has claimed."""
    screen("-n", "2")
    wait_for_log(screen.log, 2)
    trees = subprocess.run(["git", "worktree", "list"], cwd=screen.repo,
                           capture_output=True, text=True).stdout
    assert len(trees.splitlines()) == 1, "the repo itself and nothing else"


def test_a_seat_pane_survives_its_agent_exiting(screen):
    """The redirect property: interrupting a seat must leave a shell, not a hole."""
    screen("-n", "2")
    wait_for_log(screen.log, 2)
    victim = next(pid for pid, n in panes(screen) if n == "1")
    screen.tmux("send-keys", "-t", victim, "C-c")

    deadline = time.time() + 15
    while time.time() < deadline:
        if len(panes(screen)) == 3 and _idle(screen, victim):
            break
        time.sleep(0.2)

    assert len(panes(screen)) == 3, "the pane must still be there"
    assert _idle(screen, victim), "and holding a shell"


def _idle(screen, pane):
    out = screen.tmux("display-message", "-p", "-t", pane,
                      "#{pane_current_command}").stdout.strip()
    return out not in ("sleep", "")


def test_interrupting_one_seat_leaves_the_others_working(screen):
    """This is the property a star-topology fan-out cannot offer at any price."""
    screen("-n", "2")
    wait_for_log(screen.log, 2)
    first, second = (next(pid for pid, n in panes(screen) if n == k) for k in ("1", "2"))
    screen.tmux("send-keys", "-t", first, "C-c")
    time.sleep(2)
    assert _idle(screen, second) is False, "seat 2 must still be running its agent"


def test_add_grows_a_running_screen_without_restarting_it(screen):
    """The tmux answer to zellij's override-layout: no teardown."""
    screen("-n", "2")
    wait_for_log(screen.log, 2)
    before = {pid for pid, n in panes(screen) if n}
    screen("--add")
    after = panes(screen)
    assert sorted(n for _, n in after if n) == ["1", "2", "3"]
    assert before <= {pid for pid, n in after if n}, "existing seats keep their panes"


def test_add_numbers_from_the_highest_seat_not_the_count(screen):
    """A closed pane must not hand its number to the next seat and collide."""
    screen("-n", "3")
    victim = next(pid for pid, n in panes(screen) if n == "2")
    screen.tmux("kill-pane", "-t", victim)
    screen("--add")
    assert sorted(n for _, n in panes(screen) if n) == ["1", "3", "4"]


def test_rerunning_reattaches_rather_than_rebuilding(screen):
    """The ssh path. Rebuilding would kill work the seats were mid-way through."""
    screen("-n", "2")
    before = {pid for pid, _ in panes(screen)}
    screen("-n", "2")
    assert {pid for pid, _ in panes(screen)} == before


def test_staged_seats_wait_rather_than_starting(screen):
    """start_suspended: eyeball the layout before N agents start claiming."""
    screen("-n", "2", "--staged")
    time.sleep(3)
    assert not screen.log.exists(), "no seat may have run its agent yet"


def test_kill_tears_the_screen_down(screen):
    screen("-n", "1")
    assert screen("--kill").returncode == 0
    assert screen.tmux("has-session", "-t", "=t").returncode != 0


# ---- qb-seat, on its own ----------------------------------------------------

def _seat(tmp_path, *args, **env):
    """Run qb-seat with a stub agent that prints its argv and environment."""
    stub_dir = tmp_path / "agentbin"
    stub_dir.mkdir(exist_ok=True)
    agent = stub_dir / "fake-agent"
    agent.write_text(
        "#!/bin/sh\n"
        'printf "instance=%s\\ncwd=%s\\nargc=%s\\n" '
        '"${QUARTERBACK_INSTANCE:-unset}" "$PWD" "$#"\n'
        'printf "%s\\n" "$@"\n'
    )
    agent.chmod(0o755)
    return subprocess.run(
        [str(QB_SEAT), *args],
        env={**os.environ, "PATH": f"{stub_dir}:{os.environ['PATH']}",
             "QB_SEAT_AGENT": "fake-agent", **env},
        capture_output=True, text=True, timeout=30, cwd=tmp_path,
    )


def test_qb_seat_names_the_seat(tmp_path):
    assert "instance=seat-4" in _seat(tmp_path, "4").stdout


def test_qb_seat_passes_one_brief_argument(tmp_path):
    """The brief is one argument. Word-split, it would arrive as gibberish."""
    assert "argc=1" in _seat(tmp_path, "1").stdout


def test_every_seat_gets_the_same_brief(tmp_path):
    """Different briefs per seat would be dispatch, which is the thing removed."""
    briefs = [
        _seat(tmp_path, str(n)).stdout.split("argc=1\n", 1)[1].replace(
            f"seat {n} ", "seat N "
        ).replace(f"seat-{n}", "seat-N")
        for n in (1, 2, 3)
    ]
    assert briefs[0] == briefs[1] == briefs[2]


def test_the_brief_can_be_replaced_wholesale(tmp_path):
    brief = tmp_path / "brief.txt"
    brief.write_text("do the other thing")
    out = _seat(tmp_path, "1", QB_SEAT_BRIEF=str(brief)).stdout
    assert "do the other thing" in out
    assert "coordination board" not in out


def test_qb_seat_rejects_a_missing_or_bad_seat_number(tmp_path):
    assert _seat(tmp_path).returncode == 2
    assert _seat(tmp_path, "0").returncode == 2
    assert _seat(tmp_path, "two").returncode == 2


def test_qb_seat_says_so_when_the_agent_is_absent(tmp_path):
    r = _seat(tmp_path, "1", QB_SEAT_AGENT="definitely-not-installed")
    assert r.returncode == 1
    assert "QB_SEAT_AGENT" in r.stderr
