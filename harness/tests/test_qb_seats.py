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
        **{k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")},
        # $TMUX BEATS $TMUX_TMPDIR, and that is the whole bug. Inside a tmux pane
        # $TMUX names the server the client is attached to, so every tmux call
        # below reaches THAT server no matter where TMUX_TMPDIR points — the
        # suite creates its sessions on it and then tears it down. Measured: a
        # seat ran `pytest harness/tests` inside the screen it was sitting in and
        # killed the server hosting it, with TMUX_TMPDIR correctly set the whole
        # time. Unset both, so a run from inside tmux is a run outside it.
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
            assert "TMUX" not in env, "a test that inherits $TMUX drives the caller's server"
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


def test_no_yolo_reaches_the_panes(screen):
    """The flag is the env knob with the value supplied by the layout, so there
    is one mechanism to test and nothing that can drift between the two."""
    screen("--no-yolo", "-n", "1")
    assert "QB_SEAT_YOLO=0" in screen.tmux("show-environment", "-t", "t").stdout


def test_a_plain_screen_leaves_the_knob_unset(screen):
    """Not "sets it to on" — an unset variable is how qb-seat is told the question
    was not answered, and answering it here would be a second place for the
    default to live and to drift from."""
    screen("-n", "1")
    assert "QB_SEAT_YOLO" not in screen.tmux("show-environment", "-t", "t").stdout


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


def test_the_default_is_three_seats(screen):
    """Replaces the two-seat default.

    Two was chosen because integration cost grows quadratically in open PRs. That
    is still true and is still the reason for a ceiling — it is just not a reason
    for the ceiling to be below what one human can follow, which is three.
    """
    screen()
    assert sorted(n for _, n in panes(screen) if n) == ["1", "2", "3"]


def test_a_bare_number_is_the_seat_count(screen):
    """`qb-b 4` is the shape this is reached for, not `qb-b -n 4`."""
    screen("4")
    assert sorted(n for _, n in panes(screen) if n) == ["1", "2", "3", "4"]


def test_more_than_ten_seats_is_refused_with_the_reason(screen):
    r = screen("11")
    assert r.returncode != 0
    assert "ceiling is 10" in r.stderr, r.stderr


def seat_grid(run, name="t"):
    """{pane_top: [seat numbers, left to right]} — the screen as a human sees it."""
    out = run.tmux("list-panes", "-t", f"{name}:seats", "-F",
                   "#{pane_top}\t#{pane_left}\t#{@qb_seat}").stdout
    rows: dict[int, list] = {}
    for line in out.splitlines():
        top, left, seat = line.split("\t")
        if seat:
            rows.setdefault(int(top), []).append((int(left), seat))
    return {top: [s for _, s in sorted(cells)] for top, cells in rows.items()}


def test_ten_seats_are_five_across_and_two_down(screen):
    """`tiled` would pick its own arrangement — 2 across and 3 down for six, from
    the window's aspect ratio. That is the wrong axis: a seat needs WIDTH, for
    prose and diffs, and only enough height for the last few turns. So the rows
    are built rather than chosen, and this is the assertion that says so."""
    screen("10")
    grid = seat_grid(screen)
    assert len(grid) == 2, f"expected two rows of seats, got {grid}"
    top, bottom = (grid[k] for k in sorted(grid))
    assert len(top) == 5 and len(bottom) == 5, grid


def test_seat_numbers_read_left_to_right(screen):
    """A split lands to the RIGHT of its target, so splitting the first pane every
    time builds 1,5,4,3,2 across the row. Seat numbers are how a human addresses
    one of these."""
    screen("5")
    grid = seat_grid(screen)
    assert list(grid.values())[0] == ["1", "2", "3", "4", "5"], grid


def test_an_odd_count_puts_the_extra_seat_on_the_top_row(screen):
    screen("7")
    grid = seat_grid(screen)
    top, bottom = (grid[k] for k in sorted(grid))
    assert (len(top), len(bottom)) == (4, 3), grid
    assert top + bottom == [str(n) for n in range(1, 8)], grid


def test_the_board_still_spans_the_full_width_under_a_grid(screen):
    """`tiled` on a window that already holds the board folds the board into the
    grid. The rows are built before the board is split for that reason."""
    screen("6")
    out = screen.tmux("list-panes", "-t", "t:seats", "-F",
                      "#{@qb_seat}\t#{pane_width}").stdout
    rows = [line.split("\t") for line in out.splitlines() if line]
    seats = [int(w) for seat, w in rows if seat]
    board = [int(w) for seat, w in rows if not seat]
    assert max(seats) < board[0], "the board pane stopped being full width"


# ---- the seat bar ------------------------------------------------------------
#
# The bar is a second status line of clickable cells, and the reason it is there
# rather than on the pane borders is worth repeating where the tests are:
# `#[range=...]` is honoured in status-format and nowhere else, a click on a
# border arrives with #{mouse_x} empty, and a click on the TOP border row — where
# this screen draws seat 1's title — is not delivered at all.
#
# What cannot be tested here is the click itself: synthesising one needs a pty
# client and SGR mouse bytes. So these test the two halves either side of it —
# that the bar offers the right ranges, and that qb-seat-click does the right
# thing when handed one — and the join between them is the one line of
# `bind-key` asserted below.

def expand(run, fmt, name="t"):
    """A tmux format, expanded against the screen."""
    return run.tmux("display-message", "-p", "-t", f"{name}:seats", fmt).stdout.rstrip("\n")


def bar(run, name="t"):
    return expand(run, "#{E:status-format[1]}", name)


def click_env(run, name="t"):
    """The environment a `run-shell` gets: $TMUX naming this server."""
    tm = run.tmux("display-message", "-p", "-t", f"{name}:seats",
                  "#{socket_path},#{pid},0").stdout.strip()
    return {**run.env, "TMUX": tm}


def click(run, *args, name="t"):
    return subprocess.run([str(BIN / "qb-seat-click"), *args], env=click_env(run, name),
                          capture_output=True, text=True, timeout=60)


def test_the_bar_offers_a_cell_per_seat_and_none_for_the_board(screen):
    """One clickable cell per seat, generated by tmux from the panes.

    Generated, not printed: `#{P:...}` loops over the panes on every redraw, so
    a seat added or closed changes the bar with nothing regenerating it. The
    board pane has no @qb_seat and must not get an ✕ — closing it would take the
    board off a screen whose whole point is having one.
    """
    screen("-n", "3")
    line = bar(screen)
    for n in (1, 2, 3):
        assert f"range=user|seat{n}" in line, f"seat {n} is not clickable: {line}"
        assert f"range=user|kill{n}" in line, f"seat {n} has no ✕: {line}"
    assert "range=user|add" in line, f"nothing adds a seat: {line}"
    assert "range=user|seat4" not in line, "the board pane was given a seat cell"
    assert "range=user|kill4" not in line, "the board pane was given an ✕"


def test_the_bar_is_a_second_status_line_and_says_which_one_it_is(screen):
    """@qb_bar is what stops a server-wide key binding acting on other sessions.

    The binding is in the root key table, so it fires in every session on the
    box. It reads @qb_bar to decide whether the click was on OUR bar, and falls
    through to tmux's own `switch-client -t =` when it was not.
    """
    screen("-n", "2")
    assert screen.tmux("show-options", "-v", "-t", "=t:", "status").stdout.strip() == "2"
    assert screen.tmux("show-options", "-v", "-t", "=t:", "@qb_bar").stdout.strip() == "1"
    assert screen.tmux("show-options", "-v", "-t", "=t:", "mouse").stdout.strip() == "on"

    bound = screen.tmux("list-keys", "-T", "root", "MouseDown1Status").stdout
    assert "@qb_bar" in bound, "the binding does not check whose bar was clicked"
    assert "switch-client -t =" in bound, (
        "the binding has no fall-through — a click on the status line of an "
        "unrelated session would now do nothing")
    assert "\n" not in bound.strip(), (
        "the bound command spans lines; a newline ends a tmux command, so the "
        "nested if-shell would lose its arguments and the click would be silent")


def test_the_bar_can_be_turned_off(screen):
    """It costs the status line, the mouse, and a server-wide key binding.

    All three are reasonable to refuse, and refusing has to leave the screen
    itself working — the bar is an affordance, not the product.
    """
    screen.env["QB_SEATS_BAR"] = "0"
    try:
        screen("-n", "2", name="nobar")
    finally:
        del screen.env["QB_SEATS_BAR"]
    got = panes(screen, "nobar")
    assert sorted(n for _, n in got if n) == ["1", "2"], "the screen still builds"
    assert screen.tmux("show-options", "-v", "-t", "=nobar:", "@qb_bar").stdout.strip() == ""


def test_the_screen_records_what_the_plus_needs(screen):
    """A run-shell inherits the tmux SERVER's PATH, not the user's.

    The server usually predates the screen, so the ＋ cannot rely on finding
    qb-seats on PATH, and the seat panes' cwd is only the repo until somebody
    cds. Both are recorded on the session at build time instead.
    """
    screen("-n", "2")
    assert screen.tmux("show-options", "-v", "-t", "=t:", "@qb_repo").stdout.strip() \
        == str(screen.repo)
    recorded = screen.tmux("show-options", "-v", "-t", "=t:", "@qb_seats_bin").stdout.strip()
    assert recorded.endswith("qb-seats") and Path(recorded).exists(), recorded


def test_the_cross_closes_that_seat_and_reflows_the_row(screen):
    """What the ✕ dispatches, minus the mouse.

    The reflow is half the behaviour: a closed pane leaves a gap in a row whose
    widths mean something, and `-E` spreads the survivors into it without
    touching the board pane below.
    """
    screen("-n", "3")
    wait_for_log(screen.log, 3)
    assert click(screen, "kill2", "t").returncode == 0
    left = sorted(n for _, n in panes(screen) if n)
    assert left == ["1", "3"], f"closing seat 2 left {left}"

    widths = [int(w) for w in screen.tmux(
        "list-panes", "-t", "t:seats", "-F", "#{@qb_seat}\t#{pane_width}").stdout
        .splitlines() if w.split("\t")[0]
        for w in [w.split("\t")[1]]]
    assert max(widths) - min(widths) <= 1, f"the row was not reflowed: {widths}"


def test_the_plus_adds_a_seat_and_does_not_reuse_a_closed_number(screen):
    """A recycled seat number is a board bug, not a cosmetic one.

    The board keys an agent by its identity, so a new agent wearing the number
    of one that just exited reads as the old one coming back.
    """
    screen("-n", "2")
    wait_for_log(screen.log, 2)
    assert click(screen, "kill1", "t").returncode == 0
    assert click(screen, "add", "t").returncode == 0
    got = sorted(int(n) for _, n in panes(screen) if n)
    assert got == [2, 3], f"expected the new seat to be 3, got {got}"


def test_the_seat_name_jumps_to_that_pane(screen):
    screen("-n", "3")
    wait_for_log(screen.log, 3)
    assert click(screen, "seat3", "t").returncode == 0
    active = screen.tmux("display-message", "-p", "-t", "t:seats", "#{@qb_seat}")
    assert active.stdout.strip() == "3"


def test_the_dispatcher_reads_what_the_click_stashed(screen):
    """The argument form is for tests; the click has to work without one.

    `confirm-before` runs its command after the mouse event is over, and
    #{mouse_status_range} expands to nothing by then — so the binding stashes
    the range and the session, and this reads them back. The session half is not
    belt-and-braces: run-shell from a MOUSE binding gets no $TMUX_PANE at all.
    """
    screen("-n", "3")
    wait_for_log(screen.log, 3)
    screen.tmux("set-option", "-s", "@qb_click", "kill3")
    screen.tmux("set-option", "-s", "@qb_click_session", "t")
    assert click(screen).returncode == 0
    assert sorted(n for _, n in panes(screen) if n) == ["1", "2"]


def test_a_range_that_means_nothing_here_changes_nothing(screen):
    """The binding is server-wide, so this will be handed strings it does not own.

    `kill` with no number is the one that mattered. The board pane carries no
    @qb_seat, so `list-panes -F '#{pane_id} #{@qb_seat}'` gives it a one-field
    line and awk's $2 is "" — which matched the empty seat number a bare `kill`
    extracts. It closed the board pane, on a screen whose point is having one.
    """
    screen("-n", "2")
    before = panes(screen)
    for junk in ("", "window3", "kill", "seat", "kill0x", "killboard", "seat-1"):
        done = click(screen, junk, "t")
        assert done.returncode in (0, 1), f"{junk!r} → {done.returncode} {done.stderr}"
    assert panes(screen) == before, "an unknown range moved the furniture"


def test_an_empty_brief_reaches_the_panes(screen):
    """The one value QB_SEAT_BRIEF exists to express, and a `-n` forwarding test
    silently dropped it: nothing arrived in the pane, qb-seat saw unset, and the
    seat started on the full built-in brief — a screen asked for waiting seats
    that went and claimed work instead, reporting nothing wrong.
    """
    screen.env["QB_SEAT_BRIEF"] = ""
    screen("-n", "1")
    env = screen.tmux("show-environment", "-t", "t").stdout
    assert "QB_SEAT_BRIEF=" in env, "set-and-empty must be forwarded, not dropped"
    assert "-QB_SEAT_BRIEF" not in env, "must not be marked for removal"
