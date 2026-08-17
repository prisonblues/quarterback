"""Tests for qb-seats, the layout.

`qb-seat`, the per-seat wrapper each pane runs, ships and is tested separately;
the fixture here stubs it, so nothing below depends on the real one.

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
import tempfile
import time
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
QB_SEATS = BIN / "qb-seats"

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

    # A HOME of its own, so the developer's ~/.config/tmux/tmux.conf cannot
    # decide whether these pass. It is not hypothetical: a config setting
    # `pane-base-index 1` turned eight of these red at once, because the script
    # targeted `seats.0` and pane numbering no longer started at zero. The bug
    # was real and is fixed — but which config the machine happens to carry is
    # not something a test suite should depend on, so the conf is now an input.
    home = tmp_path / "home"
    (home / ".config" / "tmux").mkdir(parents=True)
    tmux_conf = home / ".config" / "tmux" / "tmux.conf"

    # A SHORT socket directory, not tmp_path. A unix socket path is capped at
    # ~104 characters and pytest's tmp_path spends most of that on the test name,
    # so tmux cannot bind there and silently uses the default socket instead —
    # /tmp/tmux-$UID. That is not a test smell, it is a hazard: this fixture used
    # to end with `tmux kill-server`, which then killed the DEVELOPER'S OWN tmux.
    # It did exactly that on this machine, taking a live seat screen with it.
    socket_dir = tempfile.mkdtemp(prefix="qbt-", dir="/tmp")
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
        # -L would be cleaner than an env var, but the script builds its own tmux
        # command lines; TMUX_TMPDIR isolates the server without touching them.
        "TMUX_TMPDIR": socket_dir,
        # A stray value in the launching environment is exactly the leak the
        # script has to defend against, so the fixture always supplies one.
        "QUARTERBACK_INSTANCE": "leaked-host-wide",
    }
    sessions = []

    def _run(*args, name="t"):
        sessions.append(name)
        done = subprocess.run(
            [str(QB_SEATS), "-C", str(repo), "-s", name, *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        # Isolation is asserted, not assumed. If tmux ever ignores TMUX_TMPDIR
        # again, these tests would quietly start driving the developer's own
        # server — and pass while doing it. Fail here instead, before the
        # teardown touches anything.
        if done.returncode == 0:
            assert (Path(socket_dir) / f"tmux-{os.getuid()}").exists(), (
                f"tmux did not use TMUX_TMPDIR={socket_dir}; it is on the default "
                "socket and this suite is now driving somebody else's server"
            )
        return done

    def _tmux(*args):
        return subprocess.run(
            ["tmux", *args], env=env, capture_output=True, text=True, timeout=30
        )

    _run.tmux = _tmux
    _run.tmux_conf = tmux_conf     # write to this BEFORE the first call: tmux
                                   # reads its config when the server starts
    _run.repo = repo
    _run.log = tmp_path / "seats.log"
    _run.env = env
    yield _run

    # Only the sessions this fixture made, addressed exactly (`=name`), and NEVER
    # `tmux kill-server`: a server that loses its last session exits on its own,
    # so the blunt instrument bought nothing and could reach a server that is not
    # ours. Whatever else is running on this machine is none of a test's business.
    for name in sessions:
        subprocess.run(["tmux", "kill-session", "-t", f"={name}"], env=env,
                       capture_output=True)
    shutil.rmtree(socket_dir, ignore_errors=True)


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


def test_a_pane_base_index_of_one_still_builds_the_screen(screen):
    """Pane numbering is the user's to set, and `pane-base-index 1` is common.

    Every `-t session:window.0` target fails outright under it ("can't find
    pane: 0"), which is why the script addresses panes by ID instead.
    """
    screen.tmux_conf.write_text("set -g base-index 1\nsetw -g pane-base-index 1\n")
    screen("-n", "2")
    got = panes(screen)
    assert sorted(n for _, n in got if n) == ["1", "2"]
    assert [n for _, n in got].count(None) == 1, "the board pane too"


def test_the_default_is_two_seats(screen):
    """Integration cost is quadratic in open PRs; the ceiling is not the monitor."""
    screen()
    assert sorted(n for _, n in panes(screen) if n) == ["1", "2"]


def test_no_inherited_instance_reaches_a_seat(screen):
    """The failure this guards against looks like nothing at all from the screen.

    Naming a seat is qb-seat's job and is tested with it; the LAYOUT's job is to
    guarantee that whatever was in the launching environment does not arrive
    here. So the stub seeing no value at all is the pass condition.
    """
    screen("-n", "2")
    lines = wait_for_log(screen.log, 2)
    assert len(lines) == 2
    assert all("instance=unset" in line for line in lines), lines
    assert "leaked-host-wide" not in "\n".join(lines)


def test_qb_seat_knobs_reach_the_panes(screen):
    """A pane's environment comes from the tmux SERVER, usually one that was
    already running for something else. Without explicit forwarding,
    `QB_SEAT_AGENT=... qb-seats` sets a variable no seat ever sees — and starts
    the REAL agent while reporting nothing wrong. That is how this was found.
    """
    screen.env["QB_SEAT_AGENT"] = "some-stand-in"
    screen("-n", "1")
    env = screen.tmux("show-environment", "-t", "t").stdout
    assert "QB_SEAT_AGENT=some-stand-in" in env


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


def test_qb_b_is_a_spelling_of_qb_seats(tmp_path):
    """The short name must reach the real script, including through the flat
    symlinks home-manager installs — which is the layout `readlink -f` breaks on.
    """
    r = subprocess.run([str(BIN / "qb-b"), "--help"], capture_output=True, text=True,
                       timeout=30)
    assert r.returncode == 0, r.stderr
    assert "--staged" in r.stdout and "--kill" in r.stdout

    # …and via a home-manager-shaped symlink: one flat link per file, so the
    # link's own directory holds nothing else.
    flat = tmp_path / "hm_qb-b"
    flat.symlink_to(BIN / "qb-b")
    r = subprocess.run([str(flat), "--help"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "--staged" in r.stdout
