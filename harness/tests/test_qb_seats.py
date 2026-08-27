"""Tests for qb-seats, the layout.

A pane holds a shell and one line is typed into it — `QB_SEAT_INITIAL_CMD`, which the
fixture points at a stub, so nothing below starts a real agent.

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

import contextlib
import fcntl
import os
import pty
import re
import shlex
import shutil
import struct
import subprocess
import tempfile
import termios
import threading
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
    """A throwaway repo, a stub agent on PATH, and an isolated tmux server."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    # The stub stands in for the agent. It is what QB_SEAT_INITIAL_CMD names, so
    # it is exactly what a seat is told — it records what it was given and then
    # holds the pane open, the way a real session would.
    #
    # NAMED FOR WHAT IT IS, not `qb-seat`. It stood in for that script until #540,
    # which was a per-pane wrapper this suite deliberately did not test; there is
    # no wrapper now, so the stub stands directly in the agent's place.
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "seat-stub"
    # /bin/sh, not `#!/usr/bin/env bash`: there is no /usr/bin/env inside the nix
    # build sandbox, and a stub that cannot exec makes every test here fail for a
    # reason that has nothing to do with the code under test. The shipped scripts
    # dodge this via patchShebangs; a stub written at runtime cannot.
    #
    # `args` is logged because the initial command is a COMMAND LINE now and not a
    # program name: `--cmd 'seat-stub -- /get-involved'` has to arrive as a stub
    # with two arguments, and a test that only saw the program could not tell that
    # from the prompt having been dropped.
    stub.write_text(
        "#!/bin/sh\n"
        'printf "instance=%s cwd=%s args=%s\\n"'
        ' "${QUARTERBACK_INSTANCE:-unset}" "$PWD" "$*"'
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
        # EVERY QB_SEAT* KNOB IS STRIPPED, not merely overridden below. These tests
        # assert what a seat was told, so a value in the developer's own environment
        # decides their result: `QB_SEAT_INITIAL_CMD=` exported fleet-wide — which
        # is the documented way to ask for a screen of bare shells — makes every
        # test that asserts on what a seat was told fail, against code that handled
        # it exactly right. Green on CI, red on the box that configured it, and the
        # diff under test innocent in both. (It was QB_SEAT_BRIEF that proved this;
        # the prefix test catches the whole family either way.)
        #
        # Same rule the tmux.conf above is an input for, and the same rule that put
        # QB_SEATS_DASH and QB_SEATS_BOARD below: what the machine happens to carry
        # is not something a test may consult. A test that wants a knob sets it.
        **{k: v for k, v in os.environ.items()
           if k not in ("TMUX", "TMUX_PANE") and not k.startswith("QB_SEAT")},
        # $TMUX BEATS $TMUX_TMPDIR, and that is the whole bug. Inside a tmux pane
        # $TMUX names the server the client is attached to, so every tmux call
        # below reaches THAT server no matter where TMUX_TMPDIR points — the
        # suite creates its sessions on it and then tears it down. Measured: a
        # seat ran `pytest harness/tests` inside the screen it was sitting in and
        # killed the server hosting it, with TMUX_TMPDIR correctly set the whole
        # time. Unset both, so a run from inside tmux is a run outside it.
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        # THE SCRIPTS UNDER TEST GO ON PATH, ahead of any installed copy. Not
        # cosmetic: qb-seats records `beside_me qb-seats` on the session for the ＋
        # to shell out to, and beside_me asks PATH first — so with only the
        # developer's PATH here, the ＋ tests, the ✕ tests and the dash's resize
        # hook were all driving the INSTALLED qb-seats and qb-seat-click and not
        # these ones. They passed while proving nothing about the working tree, and
        # the same rule the tmux.conf is an input for applies: what the machine
        # happens to carry is not something a test may consult. The stub qb-seat
        # still comes first, since that one has to win over the real thing.
        "PATH": f"{stub_dir}:{BIN}:{os.environ['PATH']}",
        # -L would be cleaner than an env var, but the script builds its own tmux
        # command lines; TMUX_TMPDIR isolates the server without touching them.
        "TMUX_TMPDIR": socket_dir,
        # A stray value in the launching environment is exactly the leak the
        # script has to defend against, so the fixture always supplies one.
        "QUARTERBACK_INSTANCE": "leaked-host-wide",
        # AND NO REAL AGENT. The shipped default is `claude-yolo`, so every screen
        # these tests build would otherwise try to start one — or, more likely,
        # type a command the pane's shell cannot resolve and leave a `command not
        # found` where the assertions expect a seat. Same rule as the dash, the
        # tape and the pace note below: a test supplies what it is about to assert
        # on. The tests that are ABOUT the default unset this and check what
        # qb-seats types rather than running it.
        "QB_SEAT_INITIAL_CMD": "seat-stub",
        # NO DASH unless a test asks for one. Left alone, dash_cmd() resolves
        # whatever qb-dash happens to be installed on the machine running the
        # suite, so a third of these tests would depend on that — and would start
        # a real dashboard, against a real board, in every screen they build. Same
        # rule the tmux.conf above is an input for: what the developer's machine
        # carries is not something a test may consult. Tests that want a dash set
        # QB_SEATS_DASH themselves, to a stub; the two that are ABOUT the default
        # unset it again and put stubs named qb-dash/qb-dash-tui on PATH instead.
        "QB_SEATS_DASH": "",
        # AND NO REAL TAPE EITHER, for the same reason one line up: qb-board is on
        # the PATH above, so every screen these tests build would otherwise start
        # `qb-board --follow` against the real board — fifty of them in a run.
        # A test may not need the network to be up to pass.
        "QB_SEATS_BOARD": "printf tape-stub",
        # AND NO REAL PACE NOTE, for the third time and the same reason. qb-pace
        # is on the BIN above, so every screen these tests build would otherwise
        # read the developer's own subscription out of ~/.claude and call the
        # usage endpoint — fifty times a run, on figures that decide nothing here.
        # A test may not need the network to be up to pass. The tests that are
        # ABOUT the note turn it back on and put a stub qb-pace ahead of the real
        # one on PATH.
        "QB_SEATS_PACE": "off",
        # AND NO TOP-LINE LOOP, for the fourth time and the same reason as the
        # three above. `qb-seat-top` is started by every screen these tests build
        # — a hundred and twenty of them in a run — and each would sit in a sleep
        # loop for the life of its session replaying a decrypt animation. Worse
        # than the waste, it WRITES to the session while the assertions read it:
        # a test that captures the bar mid-reveal is a test that fails once a
        # fortnight for a reason nobody will find. 0 replays means one pass and
        # nothing left running, and the tests that are ABOUT the line turn it
        # back on.
        "QB_SEATS_TOP_EVERY": "0",
        "QB_SEATS_TOP_ANIMATE": "0",
    }

    def _run(*args, name="t", exe=None):
        """`exe` starts the script as something else — a command list, in place of
        the plain path to the script under test.

        Only the resize-hook tests need it, and they need it for a reason no other
        test has: what qb-seats writes into a hook depends on the PATH IT WAS
        STARTED AS, since a hook that calls back into this script has to decide
        which copy of itself it means. A test about that decision cannot be run
        through one fixed entry point.
        """
        done = subprocess.run(
            [*(exe or [str(QB_SEATS)]), "-C", str(repo), "-s", name, *args],
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
    _run.stub_dir = stub_dir       # the stub agent, for tests that rebuild PATH
    _run.log = tmp_path / "seats.log"
    _run.env = env
    yield _run

    # BY ID, AND ONLY ON OUR OWN SOCKET. Never `tmux kill-server`: a server that
    # loses its last session exits on its own, so the blunt instrument bought
    # nothing and could reach a server that is not ours. Whatever else is running
    # on this machine is none of a test's business — which is what the socket
    # check below is for, and it is the same check `_run` makes: if tmux ignored
    # TMUX_TMPDIR then this is somebody else's server and nothing gets killed.
    #
    # This used to address `=name`, using the name each call ASKED for, and leaked
    # a server per test whenever the two differed: tmux renames a name it will not
    # take (3.6a turns `my.screen` into `my_screen`) and tmux 3.7b keeps the dot,
    # which makes `-t "=my.screen"` parse as pane `screen` of session `my`. Either
    # way the kill missed, the session stayed up holding a `sleep 300`, and the
    # socket directory was removed out from under a server still running in it.
    # Listing the ids is both simpler and exact.
    if (Path(socket_dir) / f"tmux-{os.getuid()}").exists():
        live = subprocess.run(["tmux", "list-sessions", "-F", "#{session_id}"],
                              env=env, capture_output=True, text=True)
        for sid in live.stdout.split():
            subprocess.run(["tmux", "kill-session", "-t", sid], env=env,
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


def aux_panes(run, name="t"):
    """[(label, width, top)] for the panes that are NOT seats — one entry each.

    A list and not a dict, because the count is an assertion some tests make: two
    unlabelled panes both key on "" and the second would silently overwrite the
    first, so `len()` on the dict cannot support "the tape is the only auxiliary
    pane" — which is exactly the regression the no-dash test exists to catch.
    """
    out = run.tmux("list-panes", "-t", f"{name}:seats", "-F",
                   "#{@qb_seat}\t#{@qb_label}\t#{pane_width}\t#{pane_top}").stdout
    got = []
    for line in out.splitlines():
        if not line:
            continue
        seat, label, width, top = line.split("\t")
        if not seat:
            got.append((label, int(width), int(top)))
    return got


def labels(run, name="t"):
    """{@qb_label: (width, top)}, for the tests that look one pane up by name."""
    got = {}
    for label, width, top in aux_panes(run, name):
        assert label not in got, f"two auxiliary panes are both labelled {label!r}"
        got[label] = (width, top)
    return got


def pane_id(run, label, name="t"):
    """The pane id carrying @qb_label, or None."""
    out = run.tmux("list-panes", "-t", f"{name}:seats", "-F",
                   "#{pane_id}\t#{@qb_label}").stdout
    for line in out.splitlines():
        if line and line.split("\t")[1] == label:
            return line.split("\t")[0]
    return None


def border_label(run, pane, name="t"):
    """What pane-border-format actually RENDERS for a pane, stripped.

    Every other assertion here reads `#{@qb_label}` directly, which only proves the
    script SET an option. The border is what a user sees, and it reaches the label
    through a nested conditional in pane-border-format — so if that middle case
    were missing, every pane with no seat number would read 'board' and every
    label assertion in this file would still pass. This closes that gap by
    expanding the window's real format against the pane.
    """
    fmt = run.tmux("show-options", "-w", "-t", f"{name}:seats", "-v",
                   "pane-border-format").stdout.strip()
    assert fmt, "the window has no pane-border-format"
    return run.tmux("display-message", "-p", "-t", pane, fmt).stdout.strip(" *\n")


def wait_for_pane(run, pane, want, timeout=20):
    """Poll a pane's visible text for `want`.

    The pane's shell, the command typed into it and this process are all concurrent
    — the same shape wait_for_log exists for.
    """
    deadline = time.time() + timeout
    seen = ""
    while time.time() < deadline:
        seen = run.tmux("capture-pane", "-p", "-t", pane).stdout
        if want in seen:
            return seen
        time.sleep(0.2)
    return seen


def wait_for_dash_width(run, want, name="t", timeout=20):
    """Poll the dash's width. The resize hooks fire `run-shell -b`, i.e. in the
    background, so the refit lands some moments after the window resize does."""
    deadline = time.time() + timeout
    got = None
    while time.time() < deadline:
        got = dict((lbl, w) for lbl, w, _ in aux_panes(run, name)).get("dash")
        if got == want:
            return got
        time.sleep(0.2)
    return got


def wait_until(predicate, timeout=20):
    """Poll until a predicate holds. The actions a key fires go through
    `run-shell -b`, i.e. in the background, so they land some moments after the
    keystroke does — the same shape wait_for_dash_width exists for."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


@contextlib.contextmanager
def attached_client(run, cols, rows, name="t"):
    """A REAL tmux client attached at a size of the test's choosing.

    Nothing else in this file has one, and that is how a 78-column dash that came
    out at 32 on every attach got as far as review: a detached session is exactly
    the -x/-y it was built with, so every pane-width assertion made without a
    client is made about a window no user ever looks at. Attaching is what resizes
    the window and rescales every pane in the dash's row.

    tmux will only attach to a real terminal, hence the pty; the winsize is set on
    the slave BEFORE the client starts, so the very first thing it reports is the
    size under test. The master end has to be drained continuously or tmux blocks
    writing its first redraw into a full pty buffer and the window never resizes at
    all — hence the pump thread.

    It yields a `press`, which is what the qb key's end-to-end test needs and what
    the bar can never have: synthesising a CLICK means SGR mouse bytes and a
    status line whose geometry the test would have to compute, while a key is one
    byte written to the master. Nothing before #248 had a caller for it, which is
    why this used to yield the Popen — nothing read that either.
    """
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    stop = threading.Event()

    def pump():
        while not stop.is_set():
            try:
                if not os.read(master, 1 << 16):
                    return
            except BlockingIOError:
                time.sleep(0.05)
            except OSError:
                return

    os.set_blocking(master, False)
    client = subprocess.Popen(
        ["tmux", "attach-session", "-t", f"={name}"],
        env={**run.env, "TERM": "xterm-256color"},
        stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
    )
    os.close(slave)
    pumping = threading.Thread(target=pump, daemon=True)
    pumping.start()
    try:
        got = None
        deadline = time.time() + 20
        while time.time() < deadline:
            got = run.tmux("display-message", "-p", "-t", f"{name}:seats",
                           "#{window_width}").stdout.strip()
            if got == str(cols):
                break
            time.sleep(0.2)
        assert got == str(cols), f"the client never resized the window to {cols}: {got}"

        def press(*keys, gap=0.4):
            """Type keys into the client's terminal, one at a time.

            The gap is not a sleep-until-it-passes. A key table is a state machine
            and tmux reads its input in chunks: the two bytes of `C-q t` arriving
            in one read are one paste, not a chord, and the table has to have been
            switched into before the second byte is looked up in it.
            """
            for key in keys:
                os.write(master, key.encode() if isinstance(key, str) else key)
                time.sleep(gap)

        press.client = client
        yield press
    finally:
        stop.set()
        run.tmux("detach-client", "-s", f"={name}")
        client.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            client.wait(timeout=10)
        pumping.join(timeout=2)
        os.close(master)


def path_with_no_dash_on_it(tmp_path, run):
    """A PATH carrying what qb-seats needs to run and nothing named like a dash.

    Filtering the real PATH by directory cannot work here: a nix profile keeps tmux
    and qb-dash in the SAME bin directory, so dropping the directory that has a
    dash in it takes the multiplexer with it. The tools are therefore linked in by
    name — and the test asserts the run succeeded, so a tool missing from this list
    fails loudly instead of turning the test into a no-op.
    """
    d = tmp_path / "nodash"
    d.mkdir(exist_ok=True)
    for tool in ("env", "bash", "sh", "tmux", "git", "awk", "sort", "dirname",
                 "sleep", "cat"):
        found = shutil.which(tool)
        if found and not (d / tool).exists():
            (d / tool).symlink_to(found)
    # The stub agent still has to be findable: QB_SEAT_INITIAL_CMD names it by the
    # bare name, and a pane that cannot resolve it types a `command not found`
    # where the assertions expect a seat.
    return f"{run.stub_dir}:{d}"


def wait_for_log(path, count, timeout=20):
    """The seats start concurrently; wait for all of them rather than sleeping."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and len(path.read_text().splitlines()) >= count:
            return path.read_text().splitlines()
        time.sleep(0.2)
    return path.read_text().splitlines() if path.exists() else []


def typing_shell(run):
    """Give the screen's panes a shell that records what is typed into them.

    For the tests whose subject is WHAT qb-seats types rather than what the
    command then does. The shipped default is `claude-yolo`, so those tests cannot
    let the line run — and they cannot substitute a stub either, because the value
    they are asserting on is the default itself.

    A shell that reads its stdin is exactly the hazard `QB_SEATS` exists to warn
    rc files off (keys sent to a pane wait in the pty until something reads them),
    which is what makes it the right instrument here: it consumes the keystrokes
    the way a prompt would and writes down what it got.

    Returns the path it appends to. Call it BEFORE the screen is built — tmux
    reads its config when the server starts.
    """
    typed = run.repo.parent / "typed.log"
    sh = run.stub_dir / "recording-shell"
    sh.write_text(
        "#!/bin/sh\n"
        "while IFS= read -r line; do\n"
        f"  printf '%s\\n' \"$line\" >> {typed}\n"
        "done\n"
        "exec sleep 300\n"
    )
    sh.chmod(0o755)
    # Appended, because a test may have set something here already, and the fixture
    # documents this file as an input rather than as fixture-owned.
    with open(run.tmux_conf, "a") as conf:
        conf.write(f'set -g default-shell "{sh}"\n')
    return typed


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

    Nothing names a seat since #540 — with no value the hook falls back to the
    session id, which is unique per conversation. The LAYOUT's job is to guarantee
    that whatever was in the launching environment does not arrive here, because
    one value shared across panes makes them one agent on the board. So the stub
    seeing no value at all is the pass condition.
    """
    screen("-n", "2")
    lines = wait_for_log(screen.log, 2)
    assert len(lines) == 2
    assert all("instance=unset" in line for line in lines), lines
    assert "leaked-host-wide" not in "\n".join(lines)


def test_the_default_initial_command_is_the_one_a_seat_is_given(screen):
    """`claude-yolo` and not `claude --dangerously-skip-permissions`.

    The value is TYPED INTO A SHELL rather than exec'd, so naming the alias is
    what lets a consumer put their own wrapper in front of it — and on the box
    this was written for `claude-yolo` is an alias in ~/.bashrc that is not on
    PATH at all, which an exec'd default could not have run.

    Asserted on what the screen TYPES, with the pane's shell replaced by one that
    records its input: running the real default would start an agent.
    """
    del screen.env["QB_SEAT_INITIAL_CMD"]
    typed = typing_shell(screen)
    screen("-n", "1")
    assert wait_for_log(typed, 1) == ["claude-yolo"]


def test_an_empty_initial_command_leaves_a_bare_shell(screen):
    """Set-and-empty is a request, not a missing answer, and it is the one value a
    `${VAR:-}` test cannot express — it reads as unset and hands the pane the
    default instead. That exact bug had a name in the knob this replaced."""
    screen.env["QB_SEAT_INITIAL_CMD"] = ""
    typed = typing_shell(screen)
    screen("-n", "2")
    assert panes(screen), "the panes are still built"
    time.sleep(1.0)
    assert not typed.exists() or typed.read_text() == "", typed.read_text()


def test_the_initial_command_may_carry_a_prompt(screen):
    """`claude-yolo -- /get-involved` is a screen that comes up claiming work, and
    it is the whole of what an eager fleet needs from this script — so the line has
    to arrive as a command LINE and not as a program name."""
    screen.env["QB_SEAT_INITIAL_CMD"] = "seat-stub -- /get-involved"
    screen("-n", "1")
    lines = wait_for_log(screen.log, 1)
    assert "args=-- /get-involved" in lines[0], lines


def test_cmd_beats_the_environment(screen):
    """One invocation's answer, over an exported one. The precedence matters
    because the export is how a box says what its screens are made of."""
    screen.env["QB_SEAT_INITIAL_CMD"] = "seat-stub exported"
    screen("-n", "1", "--cmd", "seat-stub flagged")
    assert "args=flagged" in wait_for_log(screen.log, 1)[0]


def test_cmd_with_an_empty_string_is_a_screen_of_shells(screen):
    """`--cmd ''` has to survive the argument parser, which is why the guard there
    counts arguments rather than testing the value for emptiness."""
    typed = typing_shell(screen)
    screen("-n", "1", "--cmd", "")
    assert panes(screen)
    time.sleep(1.0)
    assert not typed.exists() or typed.read_text() == "", typed.read_text()


def test_cmd_without_a_value_is_refused_rather_than_read_as_empty(screen):
    """A caller bug, and it says so: silently reading a missing value as "bare
    shells" would build a screen that starts nothing and look deliberate."""
    done = screen("-n", "1", "--cmd")
    assert done.returncode != 0
    assert "--cmd needs a command line" in done.stderr, done.stderr


def test_no_yolo_asks_for_the_agent_that_stops_to_ask(screen):
    """The flag is sugar for --cmd, so there is one mechanism and nothing that can
    drift between the two."""
    del screen.env["QB_SEAT_INITIAL_CMD"]
    typed = typing_shell(screen)
    screen("--no-yolo", "-n", "1")
    assert wait_for_log(typed, 1) == ["claude"]


def test_yolo_overrides_an_exported_initial_command(screen):
    """The only thing --yolo is for: saying the default out loud over the top of
    an export that said something else."""
    screen.env["QB_SEAT_INITIAL_CMD"] = "seat-stub exported"
    typed = typing_shell(screen)
    screen("--yolo", "-n", "1")
    assert wait_for_log(typed, 1) == ["claude-yolo"]


def test_the_screen_records_what_it_was_built_with(screen):
    """--add and the bar's ＋ cannot read the environment the screen was BUILT in:
    the ＋ arrives through `run-shell`, whose environment is the tmux server's. So
    the answer is recorded on the session, where the screen's own tooling reads
    it."""
    screen("-n", "1", "--cmd", "seat-stub recorded")
    assert screen.tmux("show-options", "-v", "-t", "t:",
                       "@qb_initial_cmd").stdout.strip() == "seat-stub recorded"


def test_a_seat_added_later_is_the_seat_the_screen_is_made_of(screen):
    """--add with nothing said takes the screen's own answer, not the default.

    And not an exported one either — the fixture always exports one, which is the
    ambient case this ordering is about: an export in an rc file would otherwise
    have every ＋ quietly add a seat unlike the ones beside it, where the screen
    was built deliberately and in front of somebody.
    """
    screen("-n", "1", "--cmd", "seat-stub original")
    assert screen.env["QB_SEAT_INITIAL_CMD"] == "seat-stub", "the ambient export"
    screen("--add")
    lines = wait_for_log(screen.log, 2)
    assert all("args=original" in line for line in lines), lines


def test_a_screen_of_bare_shells_stays_that_way_when_a_seat_is_added(screen):
    """The reason the recording distinguishes unset from empty. tmux answers an
    option that was never set with exit 1 and one set to "" with exit 0, so a
    screen built with `--cmd ''` says so — and an `--add` that read the two the
    same way would start an agent on a screen deliberately built without one."""
    typed = typing_shell(screen)
    screen("-n", "1", "--cmd", "")
    screen("--add")
    assert len([n for _, n in panes(screen) if n]) == 2
    time.sleep(1.0)
    assert not typed.exists() or typed.read_text() == "", typed.read_text()


def test_an_added_seat_can_be_told_something_else(screen):
    """One pane, told once. It does not rewrite what the screen is made of, so the
    NEXT --add is back to the screen's own answer."""
    screen("-n", "1", "--cmd", "seat-stub original")
    screen("--add", "--cmd", "seat-stub added")
    lines = wait_for_log(screen.log, 2)
    assert any("args=added" in line for line in lines), lines
    assert screen.tmux("show-options", "-v", "-t", "t:",
                       "@qb_initial_cmd").stdout.strip() == "seat-stub original"


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


def test_add_fills_the_hole_a_closed_seat_left(screen):
    """Close seat 2 of 3, add one, get seat 2 back.

    It was max+1 until #540, so that this could never hand a new agent the number
    of one that had just exited — the number was half of the seat's board NAME, and
    the board's returning-key rule would have given the new pane the old agent's
    identity. The number is a pane option now and the board keys on the session id,
    so the collision cannot happen and the row can stay dense, which is what
    somebody opening and closing panes actually wants.
    """
    screen("-n", "3")
    victim = next(pid for pid, n in panes(screen) if n == "2")
    screen.tmux("kill-pane", "-t", victim)
    screen("--add")
    assert sorted(n for _, n in panes(screen) if n) == ["1", "2", "3"]


def test_add_still_appends_when_the_row_has_no_hole(screen):
    """The dense case is the ordinary one, and it is unchanged: the lowest free
    number on a screen nobody has closed a seat in IS the next one up."""
    screen("-n", "2")
    screen("--add")
    assert sorted(n for _, n in panes(screen) if n) == ["1", "2", "3"]


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

    # The WHOLE root table, filtered here — not `list-keys -T root
    # MouseDown1Status`. tmux 3.7b stopped answering the one-key query form for a
    # mouse key (empty output, exit 0) while still listing the binding in the
    # table, so the narrower form reported "no binding" for a binding that was
    # present and correct, and this test failed on 3.7b while passing on the 3.6a
    # a developer has (#259). The table form is answered by both and is no less
    # specific.
    table = screen.tmux("list-keys", "-T", "root").stdout
    # The KEY field, matched whole. A substring test also catches
    # `C-MouseDown1Status` — a different binding, and one this screen really does
    # install — so it would find two lines and pick the wrong one.
    lines = [ln for ln in table.splitlines()
             if re.search(r"-T\s+root\s+MouseDown1Status\s", ln)]
    assert len(lines) == 1, (
        f"expected exactly one MouseDown1Status binding in the root table, got "
        f"{len(lines)}")
    bound = lines[0]
    assert "@qb_bar" in bound, "the binding does not check whose bar was clicked"
    assert "switch-client -t =" in bound, (
        "the binding has no fall-through — a click on the status line of an "
        "unrelated session would now do nothing")
    assert "\n" not in bound.strip(), (
        "the bound command spans lines; a newline ends a tmux command, so the "
        "nested if-shell would lose its arguments and the click would be silent")


#: A `-t` target built from a shell variable that holds a session NAME, in either
#: of the two spellings this screen ever used: `-t "=$SESSION"`, and
#: `-t "$SESSION:window"`. The second survives a `.` by luck — tmux splits a target
#: on `:` first, so the dot stays inside the session part and an exact-name match
#: still wins — but not a `:`, which turns `has:colon:seats` into "can't find
#: window: colon:seats". One rule for both is easier to hold than "ids here, names
#: there because that spelling happens to be safe".
#:
#: The brace form is matched too (`${SESSION}`), because a pattern that a pair of
#: braces walks past is a tripwire with a documented way around it. `$SESSION_ID`
#: and `$sid` do not match: `\b` will not fire between `N` and `_`, nor inside
#: `sid`.
_TARGET_BY_NAME = re.compile(r'-t\s+"?=?\$\{?(?:SESSION|session|s)\}?\b')

#: An `=`-anchored target of any kind. `=` means "match this NAME exactly", so its
#: presence is proof the target is a name — whatever variable, or literal, follows
#: it. This is the half of the rule that does not need to know the variable names a
#: future author will pick.
_EXACT_NAME_TARGET = re.compile(r'-t\s+"=')


@pytest.mark.parametrize("script", ["qb-seats", "qb-seat-click"])
def test_every_session_target_is_an_id_and_not_a_name(script):
    """A `-t` may not address a session by NAME, whatever the name looks like.

    `.` and `:` are a target's own separators. tmux used to rewrite them out of a
    session name it would not take, so `-t "=$SESSION"` worked by accident; 3.7b
    keeps `my.screen` verbatim and the same target then parses as pane `screen` of
    session `my` — "can't find pane: screen" — against a screen that plainly
    exists. Every seat command failed, `list` showed nothing and `resume` could not
    reach it (#259).

    BOTH SCRIPTS, because the bar's buttons are in the other one. `qb-seat-click`
    carried six of these targets after the first pass converted `qb-seats`, and it
    is the worse place for them: it is reached through `run-shell -b`, which
    discards both streams, so the ✕, the ＋ and the seat cells would have gone on
    doing nothing at all with no error anywhere.

    Asserted on the SOURCE rather than by driving tmux, because the bug is
    invisible on the tmux a developer has: 3.6a renames the dot away, so no test
    using a session name can exercise it there. This holds under either version,
    which is the point — the next `-t "=$SESSION"` someone adds fails here and now
    rather than on whichever box happens to carry the newer tmux.
    """
    src = (Path(__file__).resolve().parents[1] / "bin" / script).read_text()
    offenders = [
        f"{script}:{n}: {line.strip()}"
        for n, line in enumerate(src.splitlines(), 1)
        # COMMENTS ARE NOT CODE. Both files describe the broken spelling in prose
        # — that is how the next reader learns why the ids are there — and a guard
        # that could not tell the two apart would make the explanation unwritable.
        if not line.lstrip().startswith("#")
        and (_TARGET_BY_NAME.search(line) or _EXACT_NAME_TARGET.search(line))
    ]
    assert not offenders, (
        "these address a tmux session by name, which breaks the moment the name "
        "carries a `.` or a `:` — use the session id (SESSION_ID/sid, or the id "
        "read beside the name in screens()):\n  " + "\n  ".join(offenders))


def test_the_name_target_pattern_catches_the_forms_that_actually_shipped():
    """The guard's own red: a pattern matching nothing passes everything."""
    for shipped in (
        'tmux has-session -t "=$SESSION" 2>/dev/null',
        'tmux list-panes -t "$SESSION:seats" -F \'#{pane_id}\'',
        'tmux list-panes -s -t "=$s" -F \'#{@qb_seat}\'',
        'tmux list-panes -s -t "=$session" -F \'#{pane_id} #{@qb_seat}\'',
        'tmux show-options -v -t "=$session:" @qb_repo',
        # The two ways past a pattern anchored on `"$NAME`: braces, and a
        # variable this guard has never heard of behind an `=`.
        'tmux kill-session -t "${SESSION}"',
        'tmux kill-session -t "=$screen_name"',
    ):
        assert (_TARGET_BY_NAME.search(shipped)
                or _EXACT_NAME_TARGET.search(shipped)), shipped
    for fixed in (
        'tmux list-panes -t "$SESSION_ID:seats" -F \'#{pane_id}\'',
        'tmux list-panes -s -t "$sid" -F \'#{@qb_seat}\'',
        'tmux kill-session -t "$SESSION_ID"',
        'tmux resize-pane -t "$p" -x "$want"',
        "  'switch-client -t ='",
    ):
        assert not _TARGET_BY_NAME.search(fixed), fixed
        assert not _EXACT_NAME_TARGET.search(fixed), fixed


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
    # The one under test, exactly. `endswith("qb-seats")` was satisfied by the copy
    # installed on the machine running the suite, which is how the ＋ and ✕ tests
    # came to be driving that one instead of this working tree.
    assert recorded == str(QB_SEATS), recorded


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


def stub_qb_end(screen, tmp_path, exit_code: int = 0) -> tuple[Path, dict]:
    """A `qb-end` on PATH that records its arguments. Returns (log, env).

    The ✕ has to tell the board the agent is going BEFORE it kills the pane, and
    that call is the only part of closing a seat that leaves the machine — so it
    is stubbed rather than pointed at a board, exactly as the agent itself is.
    """
    d = tmp_path / "endbin"
    d.mkdir(exist_ok=True)
    log = tmp_path / "qb-end.log"
    (d / "qb-end").write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        f"exit {exit_code}\n")
    (d / "qb-end").chmod(0o755)
    env = {**click_env(screen), "PATH": f"{d}:{screen.env['PATH']}"}
    return log, env


def click_with(env, *args):
    return subprocess.run([str(BIN / "qb-seat-click"), *args], env=env,
                          capture_output=True, text=True, timeout=60)


def test_the_cross_ends_the_agents_session_before_it_kills_the_pane(screen, tmp_path):
    """#277. A `kill-pane` SIGHUPs the agent, and Claude Code's SessionEnd hook is
    not documented to survive that — so the ✕ used to leave the board holding a
    live lease and every claim that session had taken, for the rest of their TTL.
    Nothing was wrong with the pane; it just was not there any more, and an
    expired lease cannot say that.

    The pane knows which session it holds because `qb-hook` stamps it at
    SessionStart. That stamp is what #266 records as missing — "nothing stamps a
    pane with its session id, so a plain local session is not locatable at all".
    """
    screen("-n", "2")
    wait_for_log(screen.log, 2)
    pane = next(p for p, n in panes(screen) if n == "1")
    screen.tmux("set-option", "-p", "-t", pane, "@qb_session", "sid-of-seat-1")

    log, env = stub_qb_end(screen, tmp_path)
    assert click_with(env, "kill1", "t").returncode == 0

    assert log.exists(), "the ✕ closed the pane without telling the board"
    said = log.read_text()
    assert "sid-of-seat-1" in said
    # `killed`, because that is what happened: something closed it from outside.
    # `finished` would say the agent was done, which nobody knows.
    assert "--reason killed" in said
    assert sorted(n for _, n in panes(screen) if n) == ["2"]


def test_a_pane_with_no_session_stamp_is_closed_all_the_same(screen, tmp_path):
    """A seat whose agent never reached the board — no hook, no token, an
    unconfigured host — has no stamp, and a ✕ that refused to close it would make
    the board a prerequisite for a button that closes a pane."""
    screen("-n", "2")
    wait_for_log(screen.log, 2)
    log, env = stub_qb_end(screen, tmp_path)

    assert click_with(env, "kill1", "t").returncode == 0
    assert not log.exists(), "qb-end was called for a pane holding no session"
    assert sorted(n for _, n in panes(screen) if n) == ["2"]


def test_a_board_that_refuses_or_is_down_does_not_keep_the_pane_open(screen, tmp_path):
    """Best effort, always. Of "a button that reports nothing" and "a button that
    does nothing", the silent one is right — the pane is what the human clicked
    to close."""
    screen("-n", "2")
    wait_for_log(screen.log, 2)
    pane = next(p for p, n in panes(screen) if n == "1")
    screen.tmux("set-option", "-p", "-t", pane, "@qb_session", "sid-unreachable")

    log, env = stub_qb_end(screen, tmp_path, exit_code=2)
    assert click_with(env, "kill1", "t").returncode == 0
    assert "sid-unreachable" in log.read_text()
    assert sorted(n for _, n in panes(screen) if n) == ["2"]


def test_the_plus_puts_a_seat_back_where_the_cross_left_a_hole(screen):
    """✕ then ＋ is the pair a human actually presses, and it should leave the row
    as it found it. A recycled number was a board bug while the number was part of
    a seat's identity; since #540 it is a label on a pane (see
    test_add_fills_the_hole_a_closed_seat_left)."""
    screen("-n", "2")
    wait_for_log(screen.log, 2)
    assert click(screen, "kill1", "t").returncode == 0
    assert click(screen, "add", "t").returncode == 0
    got = sorted(int(n) for _, n in panes(screen) if n)
    assert got == [1, 2], f"expected the hole at 1 to be filled, got {got}"


def test_the_seat_name_jumps_to_that_pane(screen):
    screen("-n", "3")
    wait_for_log(screen.log, 3)
    assert click(screen, "seat3", "t").returncode == 0
    active = screen.tmux("display-message", "-p", "-t", "t:seats", "#{@qb_seat}")
    assert active.stdout.strip() == "3"


def test_the_bar_works_on_a_screen_whose_name_tmux_keeps_verbatim(screen):
    """Every widget on the bar, against a session called `my.screen`.

    The three buttons are the reason `qb-seat-click` had to convert too: it looked
    its session up with `-t "=$session"`, and on tmux 3.7b — which keeps the dot
    rather than renaming it — that parses as pane `screen` of session `my` and
    comes back "can't find pane: screen". The ✕ then closed nothing, the ＋ added
    nothing and a seat cell jumped nowhere, all three in silence, because the bar
    reaches the script through `run-shell -b` and that discards both streams.

    Driven by the name tmux ACTUALLY gave the screen, not by `my.screen`: 3.6a
    renames the dot to an underscore and there is nothing to exercise there, so
    hardcoding either spelling would pin a tmux version rather than this script's
    promise. On 3.6a this is a second pass over the ordinary case; on 3.7b it is
    the regression.
    """
    screen("-n", "3", name="my.screen")
    wait_for_log(screen.log, 3)
    names = [n for _, n in listing(screen)]
    assert len(names) == 1, f"expected exactly one screen, got {names}"
    real = names[0]

    done = click(screen, "seat3", real, name=real)
    assert done.returncode == 0, done.stderr
    active = screen.tmux("display-message", "-p", "-t", f"{real}:seats", "#{@qb_seat}")
    assert active.stdout.strip() == "3", done.stderr

    done = click(screen, "kill2", real, name=real)
    assert done.returncode == 0, done.stderr
    assert sorted(n for _, n in panes(screen, real) if n) == ["1", "3"], done.stderr

    done = click(screen, "add", real, name=real)
    assert done.returncode == 0, done.stderr
    # 2, because the ✕ above left a hole there — see
    # test_add_fills_the_hole_a_closed_seat_left.
    assert sorted(int(n) for _, n in panes(screen, real) if n) == [1, 2, 3], done.stderr


def test_the_cross_works_on_a_screen_that_is_not_the_last_one_on_the_server(screen):
    """`session_id` used to leave the pipeline non-zero unless the screen it was
    asked about happened to be listed LAST.

    Its loop body was `[ "${line#* }" = "$1" ] && printf …`, so a final line that
    did not match made the `while` exit 1 — and under `pipefail` that is the
    status of the whole pipeline, and so of the command substitution around it.
    `sid=$(session_id "$session") || sid=""` then threw away the id it had just
    been handed, and every button on the bar reported "no screen named 'one' is
    up" about a screen tmux was listing on the line above. One screen on a server
    could never show it, which is why it shipped.
    """
    screen("-n", "2", name="one")
    screen("-n", "2", name="two")
    assert click(screen, "kill2", "one", name="one").returncode == 0
    assert sorted(n for _, n in panes(screen, "one") if n) == ["1"]
    assert sorted(n for _, n in panes(screen, "two") if n) == ["1", "2"], \
        "the click reached the wrong screen"


def test_a_click_naming_a_screen_that_is_gone_says_so(screen):
    """It used to present as "seat 1 has no pane", which names the wrong thing.

    @qb_click_session is a SERVER option and outlives the screen that set it, so a
    stale one is the ordinary way this arrives — and the answer a user needs is
    that the screen is gone, not that one of its seats is.
    """
    screen("-n", "2")
    done = click(screen, "seat1", "no-such-screen")
    assert done.returncode == 1
    # The script's own words, not tmux's. Before the id conversion this reached
    # tmux with `-t "=no-such-screen"` and reported whatever came back, which is
    # how a missing SCREEN came to be described as a missing seat.
    assert "no screen named 'no-such-screen' is up" in done.stderr, done.stderr


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


# ---- the top line -----------------------------------------------------------
#
# `status 2` makes room for the seat bar and tmux numbers the two lines 0 and 1;
# install_bar only ever wrote index 1. Writing ONE index of an array option at
# session level stops tmux inheriting the global array, so index 0 resolved to
# empty and every screen carried a full-width blank strip in whatever
# status-style is. Nothing was relying on it, which is what makes it a free line
# rather than a trade.

def top_format(run, name="t"):
    return run.tmux("show-options", "-v", "-t", f"={name}:", "status-format[0]").stdout.strip()


def test_the_top_line_is_not_blank(screen):
    """The regression, stated as the thing that was wrong: a screen's line 0 was
    empty, so it drew as a full-width bar of `status-style` and nothing else."""
    screen("-n", "2")
    fmt = top_format(screen)
    assert fmt, "line 0 is empty again — the screen has a blank strip across the top"
    assert "#{@qb_top}" in fmt, f"the line says nothing about this screen: {fmt}"


def test_the_top_line_says_which_screen_this_is(screen):
    """The seat bar says what the seats are doing; nothing said whose they were."""
    screen("-n", "2")
    said = screen.tmux("show-options", "-v", "-t", "=t:", "@qb_top").stdout.strip()
    assert said == f"quarterback: {screen.repo.name}", said


def test_the_top_line_takes_words_of_your_own(screen):
    """`${VAR+set}`, so an EMPTY QB_SEATS_TOP is a deliberate "no words of ours
    there" and an unset one means "pick for me" — the same two different answers
    QB_SEATS_DASH draws, and the same spelling."""
    screen.env["QB_SEATS_TOP"] = "the fleet, at rest"
    try:
        screen("-n", "2")
    finally:
        del screen.env["QB_SEATS_TOP"]
    assert screen.tmux("show-options", "-v", "-t", "=t:",
                       "@qb_top_text").stdout.strip() == "the fleet, at rest"

    screen.env["QB_SEATS_TOP"] = ""
    try:
        screen("-n", "2", name="bare")
    finally:
        del screen.env["QB_SEATS_TOP"]
    assert screen.tmux("show-options", "-v", "-t", "=bare:",
                       "@qb_top_text").stdout.strip() == ""


def test_the_top_line_runs_no_shell_on_every_redraw(screen):
    """A status line re-expands every `status-interval` — 15s by default, PER
    ATTACHED CLIENT — and a `#(shell command)` in one runs on that cadence. So
    the ceiling is not `#(qb-pace)`: qb-seat-top is awake on its own timer
    anyway, writes the answer into a session option, and the format reads it.
    """
    screen("-n", "2")
    fmt = top_format(screen)
    assert "#(" not in fmt, f"the top line shells out on every redraw: {fmt}"
    assert "#{@qb_pace}" in fmt, f"the ceiling is not on the line at all: {fmt}"


def test_the_top_line_paints_its_own_colours_too(screen):
    """The same rule the seat bar has, and for the same reason — this line sits
    on the identical `status-style` green."""
    screen("-n", "2")
    fmt = top_format(screen)
    naked = [fg for fg, bg in bar_pairs(fmt) if bg is None]
    assert not naked, f"these take whatever status-style is: {naked}"
    assert "#[fill=" in fmt, "the top line does not paint its own line"
    for fg, bg in bar_pairs(fmt):
        if bg is None or _rgb(fg) is None or _rgb(bg) is None:
            continue
        ratio = _contrast(fg, bg)
        assert ratio >= 4.5, f"{fg} on {bg} is {ratio:.2f}:1, and 4.5:1 is the floor"


def test_a_toggle_does_not_steal_the_cursor(screen):
    """`join-pane` leaves the joined pane ACTIVE — measured on 3.6a — and that is
    not what a toggle should do.

    Showing the tape again landed the cursor in the tape, so the next thing typed
    went to a board follower instead of the agent being worked with, and the next
    `C-q x` refused with "that pane is not a seat" — correctly, and confusingly.
    A toggle is about what is on the screen, never about where you are on it.
    """
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("-n", "3")
    seat2 = next(p for p, n in panes(screen) if n == "2")
    screen.tmux("select-pane", "-t", seat2)

    def where():
        return screen.tmux("display-message", "-p", "-t", "=t:",
                           "#{pane_id}").stdout.strip()

    for action in ("tape", "tape", "dash", "dash"):
        assert seat_key(screen, action, "t").returncode == 0
        assert where() == seat2, f"{action} moved the cursor to {where()} (was {seat2})"


def test_the_key_works_from_a_checkout_full_of_metacharacters(screen, tmp_path):
    """RED/GREEN. `sh_quote` alone left `$`, `"`, `\\` and `#` live for tmux's OWN
    parser, and the failure was the silent one: a checkout under `a$Bdir` bound
    every key to `/…/a/qb-seat-key`, because tmux expanded `$Bdir` to nothing.
    The screen builds, the bar draws, the table installs, and every key does
    nothing at all — `run-shell -b` discards both streams. A `"` in the path is
    the loud version: `syntax error`, and a half-built screen.

    Driven through a real keystroke, because that is the only thing that proves
    the whole chain: the bind-time parse, the confirm-time second parse, the
    format expansion, and the shell that finally runs it. A PLAIN action crosses
    one tmux parse and a CONFIRMED one crosses two, so both are pressed.
    """
    odd = tmp_path / "a$B\"c\\d#e f'g"
    odd.mkdir()
    for name in ("qb-seats", "qb-seat-click", "qb-seat-key", "qb-seat-top"):
        copy = odd / name
        copy.write_text((BIN / name).read_text())
        copy.chmod(0o755)
    screen.env["PATH"] = path_with_no_dash_on_it(tmp_path, screen)
    screen.env["QB_SEATS_DASH"] = DASH_STUB

    done = screen("-n", "2", exe=[str(odd / "qb-seats")])
    assert done.returncode == 0, f"the screen did not build: {done.stderr}"
    assert pane_id(screen, "tape"), "no tape to toggle"

    with attached_client(screen, 140, 40) as press:
        press("\x11", "t")                      # plain: one tmux parse
        assert wait_until(lambda: pane_id(screen, "tape") is None), \
            "C-q t did nothing — the bound path is not the real one"
        press("\x11", "t")
        assert wait_until(lambda: pane_id(screen, "tape") is not None)

        # The cursor has to be on a seat for `close` to have one to close, and
        # the toggle above deliberately left it where it was.
        seat1 = next(p for p, n in panes(screen) if n == "1")
        screen.tmux("select-pane", "-t", seat1)
        press("\x11", "x", "y")                 # confirmed: two tmux parses
        assert wait_until(lambda: sorted(n for _, n in panes(screen) if n) == ["2"]), \
            "C-q x y did nothing — the confirmed path lost a quoting layer"


def test_a_key_press_runs_the_copy_its_own_screen_was_built_with(screen, tmp_path):
    """`bind-key -T qb` is SERVER-WIDE and holds one path. The root key is gated
    per screen, but the table is not: the last screen built writes it, so a key
    pressed on an older screen arrives in the newer screen's qb-seat-key. That is
    only wrong during a rollout, and it is exactly then that it matters — the
    same "which copy of itself" question dash_hooks answers for the resize hook.

    So the screen records @qb_key_bin and the dispatcher hands over. The marker
    proves the hand-off happened rather than the wrong copy quietly coping.
    """
    marker = tmp_path / "handed-off"
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    for name in ("qb-seat-key", "qb-seat-click", "qb-seats", "qb-seat-top"):
        copy = theirs / name
        text = (BIN / name).read_text()
        if name == "qb-seat-key":
            text = text.replace("set -euo pipefail\n",
                                f"set -euo pipefail\n: > {marker}\n", 1)
        copy.write_text(text)
        copy.chmod(0o755)

    screen.env["PATH"] = path_with_no_dash_on_it(tmp_path, screen)
    assert screen("-n", "2", exe=[str(theirs / "qb-seats")]).returncode == 0
    recorded = screen.tmux("show-options", "-v", "-t", "=t:", "@qb_key_bin").stdout.strip()
    assert recorded == str(theirs / "qb-seat-key"), recorded

    # Now press it with the OTHER copy — which is what a second screen built from
    # a different checkout leaves in the shared table.
    marker.unlink(missing_ok=True)
    done = subprocess.run([str(BIN / "qb-seat-key"), "tape", "t"],
                          env=click_env(screen), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert marker.exists(), "the wrong copy handled the key instead of handing over"
    assert aux_panes(screen) == [], "the tape did not hide"


def test_a_pace_reading_that_could_not_be_refreshed_says_so(screen, tmp_path):
    """A verdict that could not be refreshed is not a current one. `pace()` used
    to return quietly on a missing binary, a timeout, an error or an empty
    answer, which left the last reading on screen looking live — so a `STOP` from
    twenty minutes ago read exactly like a `STOP` from now, on the one number the
    line exists to carry.

    The reading is KEPT, because a stale ceiling is still the best estimate
    anyone has; it is marked, and the format draws a marked one dimmer and
    prefixed `~`.
    """
    paceb = tmp_path / "paceb"
    paceb.mkdir()
    stub = paceb / "qb-pace"
    stub.write_text("#!/bin/sh\necho 'pace: STOP — 5h at 99% (critical); resets in 12m'\n")
    stub.chmod(0o755)
    screen.env["PATH"] = f"{paceb}:{screen.env['PATH']}"
    screen.env["QB_SEATS_PACE"] = "on"
    screen.env["QB_SEATS_TOP_EVERY"] = "0"
    try:
        screen("-n", "2")
    finally:
        screen.env["QB_SEATS_PACE"] = "off"

    def opt(name):
        return screen.tmux("show-options", "-v", "-t", "=t:", name).stdout.strip()

    assert wait_until(lambda: opt("@qb_pace_sev") == "critical"), opt("@qb_pace_sev")
    said = opt("@qb_pace")
    assert "STOP" in said, said

    # Now qb-pace stops answering, exactly as a timeout or an outage would.
    stub.write_text("#!/bin/sh\nexit 1\n")
    done = subprocess.run([str(BIN / "qb-seat-top"), "$0", "--once"],
                          env={**click_env(screen), "PATH": f"{paceb}:{screen.env['PATH']}"},
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert opt("@qb_pace_sev") == "stale", \
        f"a reading that could not be refreshed still reads as current: {opt('@qb_pace_sev')}"
    assert opt("@qb_pace") == said, "the last known reading was thrown away"


def test_a_screen_still_builds_when_qb_seat_top_is_missing(screen, tmp_path):
    """The partial-install case, and it used to take the whole build down.

    During a rollout PATH's harness and a checkout disagree about which scripts
    exist, so `beside_me` answers with a path that is not there — and a
    `run-shell -b` on a missing command does NOT fail quietly. Measured on 3.6a
    as `no current client` and `not in a mode` on stderr and a non-zero exit,
    which under `set -e` killed qb-seats with the session, the seats and the tape
    already created, on an error naming none of that. Same shape as the resize
    hook's version of this, and the same answer: ask first.

    What the screen loses is only that the line refreshes. The line itself is set
    before the probe, so it still says which screen this is.
    """
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    for name in ("qb-seats", "qb-seat-click", "qb-seat-key"):
        copy = lonely / name
        copy.write_text((BIN / name).read_text())
        copy.chmod(0o755)
    # BIN off PATH, or beside_me finds the ordinary qb-seat-top there.
    screen.env["PATH"] = path_with_no_dash_on_it(tmp_path, screen)

    done = screen("-n", "2", exe=[str(lonely / "qb-seats")])
    assert done.returncode == 0, f"the screen did not build: {done.stderr}"
    assert sorted(n for _, n in panes(screen) if n) == ["1", "2"]
    assert "qb-seat-top" in done.stderr, \
        f"nothing said why the line will not refresh: {done.stderr}"
    said = screen.tmux("show-options", "-v", "-t", "=t:", "@qb_top").stdout.strip()
    assert said == f"quarterback: {screen.repo.name}", \
        f"the line is not even drawn once: {said!r}"


def test_the_reveal_is_the_same_shape_as_the_answer(screen):
    """The effect itself, read rather than caught.

    A frame is on screen for 40ms, so a test that polls for one is a test that
    races it — and that is not theoretical: the first spelling of this passed
    locally, passed in the flake sandbox, and failed in the flake sandbox on the
    same commit, for no reason but load. `--frames` prints the reveal with no
    tmux and no clock anywhere near it, so everything the effect promises is
    asserted by reading:

      * every frame is exactly as wide as the answer, or the line jitters
        sideways all the way through the reveal;
      * spaces are never scrambled, which is what makes it read as decryption
        rather than as noise — the shape of the words is there from frame one;
      * characters settle left to right and never come unsettled;
      * the last frame is the text itself.
    """
    text = "quarterback: a-repo"
    done = subprocess.run([str(BIN / "qb-seat-top"), "--frames", text],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    frames = done.stdout.splitlines()
    assert len(frames) > len(text), f"only {len(frames)} frames for {len(text)} characters"

    for frame in frames:
        assert len(frame) == len(text), f"{frame!r} is {len(frame)} wide, not {len(text)}"
        for i, c in enumerate(text):
            if c == " ":
                assert frame[i] == " ", f"a space was scrambled in {frame!r}"

    # THE SETTLED PREFIX IS COMPUTED, NOT MEASURED, and that distinction is the
    # whole of why this test is not flaky. `commonprefix(frame, text)` counts a
    # SCRAMBLED character that happens to land on the right one, so the measured
    # prefix jumps around and a monotonicity assertion over it fails roughly one
    # run in ten — which is exactly how this test first shipped. The reveal's
    # contract is arithmetic: one character settles every second tick, so frame i
    # has (i + 1) // 2 of them and those must be right whatever the tail rolled.
    settle_every = 2                      # decrypt_text.js's settleEvery
    for i, frame in enumerate(frames[:-1]):
        revealed = (i + 1) // settle_every
        assert frame[:revealed] == text[:revealed], (
            f"frame {i} should have settled {revealed} characters: {frame!r}")
    assert frames[-1] == text, f"the reveal ended on {frames[-1]!r}"


def test_the_line_reveals_on_a_screen_somebody_is_looking_at(screen):
    """That it runs at all, which is the only part a live screen has to answer —
    and it is answered with a COUNT rather than by catching a frame, because a
    count is still true a minute later.

    A detached screen is deliberately not animated to: fifty `set-option`s played
    to an empty socket buys nothing, and a screen left over a weekend would play
    thousands. So this needs a real client, and the count must not move before
    one arrives.
    """
    screen.env["QB_SEATS_TOP_EVERY"] = "5"
    screen.env["QB_SEATS_TOP_ANIMATE"] = "1"
    try:
        screen("-n", "2")
    finally:
        screen.env["QB_SEATS_TOP_EVERY"] = "0"
        screen.env["QB_SEATS_TOP_ANIMATE"] = "0"

    def reveals():
        said = screen.tmux("show-options", "-v", "-t", "=t:",
                           "@qb_top_reveals").stdout.strip()
        return int(said) if said.isdigit() else 0

    assert reveals() == 0, "a detached screen was animated to"
    with attached_client(screen, 120, 30):
        assert wait_until(lambda: reveals() >= 1, timeout=40), \
            "the line never revealed on a screen with a client on it"
        assert screen.tmux("show-options", "-v", "-t", "=t:", "@qb_top").stdout.strip() \
            == f"quarterback: {screen.repo.name}", "the reveal did not settle on the text"


def test_a_screen_still_builds_when_qb_seat_top_is_missing(screen, tmp_path):
    """The partial-install case, and it used to take the whole build down.

    During a rollout PATH's harness and a checkout disagree about which scripts
    exist, so `beside_me` answers with a path that is not there — and a
    `run-shell -b` on a missing command does NOT fail quietly. Measured on 3.6a
    as `no current client` and `not in a mode` on stderr and a non-zero exit,
    which under `set -e` killed qb-seats with the session, the seats and the tape
    already created, on an error naming none of that. Same shape as the resize
    hook's version of this, and the same answer: ask first.

    What the screen loses is only that the line refreshes. The line itself is set
    before the probe, so it still says which screen this is.
    """
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    for name in ("qb-seats", "qb-seat-click", "qb-seat-key"):
        copy = lonely / name
        copy.write_text((BIN / name).read_text())
        copy.chmod(0o755)
    # BIN off PATH, or beside_me finds the ordinary qb-seat-top there.
    screen.env["PATH"] = path_with_no_dash_on_it(tmp_path, screen)

    done = screen("-n", "2", exe=[str(lonely / "qb-seats")])
    assert done.returncode == 0, f"the screen did not build: {done.stderr}"
    assert sorted(n for _, n in panes(screen) if n) == ["1", "2"]
    assert "qb-seat-top" in done.stderr, \
        f"nothing said why the line will not refresh: {done.stderr}"
    said = screen.tmux("show-options", "-v", "-t", "=t:", "@qb_top").stdout.strip()
    assert said == f"quarterback: {screen.repo.name}", \
        f"the line is not even drawn once: {said!r}"


def test_the_reveal_settles_on_exactly_the_static_text(screen):
    """The animation is an aesthetic and nothing may depend on it, so the thing
    worth pinning is that it is INVISIBLE in the outcome: the reveal ends on
    precisely the text a screen with QB_SEATS_TOP_ANIMATE=0 would have shown.

    It also has to actually animate, or this passes against a line that never
    moved — hence the intermediate frames. A detached screen is deliberately not
    animated to, so this needs a real client.
    """
    screen.env["QB_SEATS_TOP_EVERY"] = "5"
    screen.env["QB_SEATS_TOP_ANIMATE"] = "1"
    try:
        screen("-n", "2")
    finally:
        screen.env["QB_SEATS_TOP_EVERY"] = "0"
        screen.env["QB_SEATS_TOP_ANIMATE"] = "0"

    settled = f"quarterback: {screen.repo.name}"
    with attached_client(screen, 120, 30):
        seen = set()
        deadline = time.time() + 25
        while time.time() < deadline and len(seen - {settled, ""}) < 3:
            seen.add(screen.tmux("show-options", "-v", "-t", "=t:",
                                 "@qb_top").stdout.strip())
            time.sleep(0.03)
        frames = seen - {settled, ""}
        assert frames, f"the line never moved: {seen}"
        # Every frame is the same width as the answer, or the line would jitter
        # sideways all the way through the reveal.
        for frame in frames:
            assert len(frame) == len(settled), f"{frame!r} is not {len(settled)} wide"
        assert wait_until(
            lambda: screen.tmux("show-options", "-v", "-t", "=t:",
                                "@qb_top").stdout.strip() == settled), \
            "the reveal did not settle on the text it was decrypting to"


# ---- the qb key --------------------------------------------------------------
#
# The keyboard half of the bar (#248). Until it existed every seat-level action
# was a click: adding a seat from the keyboard meant dropping to a shell for
# `qb-seats --add`, and the tape and the dash could not be got out of the way at
# all without dragging borders.
#
# The same split as the bar's tests and for the same reason — a keystroke cannot
# be synthesised here any more than a click can, and a `display-menu` cannot be
# opened headless at all. So these test the two halves either side of the press:
# that the table and the menu offer the right keys, and that `qb-seat-key` does
# the right thing when handed an action. The join between them is the one
# `bind-key` line asserted below.
#
# WHAT IS WORTH THE TROUBLE OF ASSERTING is the geometry. A toggle that puts a
# pane back in the WRONG place still puts it back, so every wrong answer here
# looks like a working feature until somebody compares it with what they had —
# which is how `pane_top == 0` (the seat bar makes it 1) shipped in a draft,
# recording no widths at all and restoring none.

QB_TABLE = re.compile(r"^\s*bind-key\s+-T\s+qb\s+(\S+)\s+(.*)$")


def seat_key(run, *args, name="t"):
    return subprocess.run([str(BIN / "qb-seat-key"), *args], env=click_env(run, name),
                          capture_output=True, text=True, timeout=60)


def qb_table(run, name="t"):
    """{key: the command bound to it} in the `qb` key table."""
    got = {}
    for line in run.tmux("list-keys", "-T", "qb").stdout.splitlines():
        found = QB_TABLE.match(line)
        if found:
            got[found.group(1)] = found.group(2)
    return got


def qb_menu(run, name="t"):
    """[(key, label, command)] for the menu the `Any` binding opens.

    tmux re-quotes a stored command when it lists it, in a dialect shlex reads:
    the items come back as the flat `name key command …` argv display-menu was
    given, so the triples are recovered by position after `-y`'s value.
    """
    words = shlex.split(qb_table(run, name)["Any"])
    items = words[words.index("-y") + 2:]
    assert len(items) % 3 == 0, f"the menu is not whole triples: {items}"
    return [(items[i + 1], items[i], items[i + 2]) for i in range(0, len(items), 3)]


def action_of(command):
    """The qb-seat-key action a bound command runs, or None."""
    found = re.search(r"qb-seat-key'? (\w+)", command)
    return found.group(1) if found else None


def geometry(run, name="t"):
    """{pane_id: (left, top, width, height)} — the whole screen, exactly."""
    out = run.tmux("list-panes", "-t", f"{name}:seats", "-F",
                   "#{pane_id}\t#{pane_left}\t#{pane_top}\t#{pane_width}\t#{pane_height}"
                   ).stdout
    got = {}
    for line in out.splitlines():
        if line:
            pane, *rest = line.split("\t")
            got[pane] = tuple(int(v) for v in rest)
    return got


def test_the_qb_key_is_bound_and_gated_on_being_this_screens_key(screen):
    """A key table is SERVER-wide, exactly as MouseDown1Status is.

    So the binding cannot simply act: it compares @qb_key — set on this session
    and on nothing else — against the key it is bound to, and in the other branch
    does verbatim what tmux would have done, which for a key is to send it on to
    the pane. A session that is not a screen is therefore not quietly missing a
    keystroke, which is the failure a bare `bind-key -n` has.
    """
    screen("-n", "2")
    assert screen.tmux("show-options", "-v", "-t", "=t:", "@qb_key").stdout.strip() == "C-q"

    # The WHOLE root table, filtered here rather than queried key by key: tmux
    # 3.7b answers the one-key query form for some keys with empty output and
    # exit 0, which is what made the bar's equivalent assertion fail on 3.7b
    # while passing on 3.6a (#259).
    table = screen.tmux("list-keys", "-T", "root").stdout
    lines = [ln for ln in table.splitlines() if re.search(r"-T\s+root\s+C-q\s", ln)]
    assert len(lines) == 1, f"expected one root C-q binding, got {lines}"
    bound = lines[0]
    assert "#{==:#{@qb_key},C-q}" in bound, f"the binding is not gated on ITS key: {bound}"
    assert "switch-client -T qb" in bound, f"the binding opens no key table: {bound}"
    assert "send-keys C-q" in bound, f"nothing falls through elsewhere: {bound}"


def test_two_screens_with_different_keys_do_not_answer_for_each_other(screen):
    """Nothing UNBINDS the first screen's key when a second is built with another
    one, so a server ends up carrying both.

    Gated on merely *being* a screen, both conditions are then true on both
    screens — and C-q would open the key table on the screen whose user had asked
    for M-q precisely to get C-q back for their emacs. Each binding compares
    @qb_key against the key it is bound to instead, so it answers for its own
    screen and falls through everywhere else.
    """
    screen("-n", "2", name="cq")
    screen.env["QB_SEATS_KEY"] = "M-q"
    try:
        screen("-n", "2", name="mq")
    finally:
        del screen.env["QB_SEATS_KEY"]

    table = screen.tmux("list-keys", "-T", "root").stdout
    for key in ("C-q", "M-q"):
        lines = [ln for ln in table.splitlines() if re.search(rf"-T\s+root\s+{re.escape(key)}\s", ln)]
        assert len(lines) == 1, f"{key}: {lines}"
        assert f"#{{==:#{{@qb_key}},{key}}}" in lines[0], \
            f"{key} fires on any screen, not only on one whose key it is: {lines[0]}"

    assert screen.tmux("show-options", "-v", "-t", "=cq:", "@qb_key").stdout.strip() == "C-q"
    assert screen.tmux("show-options", "-v", "-t", "=mq:", "@qb_key").stdout.strip() == "M-q"


def test_a_session_that_is_not_a_screen_carries_nothing_for_the_gate_to_find(screen):
    """The other half of the gate, on the same server: the binding is there, and
    the option it reads is not — so the condition is false and the key goes to
    the pane. This is the assertion that a screen cannot take C-q away from the
    rest of somebody's tmux."""
    screen("-n", "2")
    screen.tmux("new-session", "-d", "-s", "plain")
    assert screen.tmux("show-options", "-v", "-t", "=plain:", "@qb_key").stdout.strip() == ""


def test_the_key_can_be_turned_off(screen):
    """It costs one keystroke inside every pane of the screen, and C-q in
    particular is XON under `stty ixon` and quoted-insert in readline and emacs.
    Refusing has to leave the screen itself working."""
    screen.env["QB_SEATS_KEY"] = ""
    try:
        screen("-n", "2", name="nokey")
    finally:
        del screen.env["QB_SEATS_KEY"]
    got = panes(screen, "nokey")
    assert sorted(n for _, n in got if n) == ["1", "2"], "the screen still builds"
    assert screen.tmux("show-options", "-v", "-t", "=nokey:", "@qb_key").stdout.strip() == ""
    assert qb_table(screen, "nokey") == {}, "a key table was installed anyway"


def test_the_key_can_be_a_different_one(screen):
    """`${VAR+set}`, so EMPTY means none and unset means pick for me — the same
    spelling as QB_SEATS_DASH, and the reason the two answers stay different."""
    screen.env["QB_SEATS_KEY"] = "M-q"
    try:
        screen("-n", "2")
    finally:
        del screen.env["QB_SEATS_KEY"]
    assert screen.tmux("show-options", "-v", "-t", "=t:", "@qb_key").stdout.strip() == "M-q"
    table = screen.tmux("list-keys", "-T", "root").stdout
    lines = [ln for ln in table.splitlines() if re.search(r"-T\s+root\s+M-q\s", ln)]
    assert len(lines) == 1, f"M-q was not bound: {lines}"
    assert "send-keys M-q" in lines[0], f"the fall-through sends the wrong key: {lines[0]}"


def test_a_key_that_would_end_the_tmux_command_is_refused_before_anything_is_built(screen):
    """The value is written into a `bind-key` command line, and `bind-key` is one
    of the last things the script does — so a key tmux will not take would refuse
    with the session, the seats, the dash and the tape already built. Same
    argument, and the same place, as QB_SEATS_DASH_SIZE's check."""
    # Two places the value lands and the format is the strict one: `#`, `}`, `,`
    # and `:` are the gate's own punctuation, and a key carrying one of them does
    # not fail loudly — `#{==:#{@qb_key},<key>}` still parses, as something else,
    # and the gate then answers a question nobody asked.
    for bad in ("a;b", "a b", "a'b", 'a"b', "a$b", "a#b", "a,b", "a:b", "a}b"):
        screen.env["QB_SEATS_KEY"] = bad
        try:
            done = screen("-n", "2", name="badkey")
        finally:
            del screen.env["QB_SEATS_KEY"]
        assert done.returncode == 1, f"{bad!r} was accepted: {done}"
        assert "QB_SEATS_KEY" in done.stderr, done.stderr
        assert screen.tmux("has-session", "-t", "=badkey").returncode != 0, \
            f"a screen was built for {bad!r}, which was then refused"


def test_every_menu_accelerator_is_a_key_in_the_table(screen):
    """The claim the menu makes is that it teaches the shortcut it replaces, and
    two lists of keys is how that becomes a lie. Both are generated from one
    table in the script; this is what keeps it that way."""
    screen("-n", "2")
    table = qb_table(screen)
    for key, label, command in qb_menu(screen):
        if not key:
            continue                      # a separator: an empty name, no key
        assert key in table, f"the menu offers {key!r} ({label!r}) and nothing binds it"
        # AS WORDS, SORTED, and both halves of that are tmux's doing rather than
        # slack. A command stored as a binding is re-quoted when it is listed and
        # its flags come back in tmux's own order (`-w 76 -h 16` prints as
        # `-h 16 -w 76`), while the same command sitting inside the menu is an
        # opaque argument that is only parsed when the item is chosen — so it
        # keeps the spelling it was given. What must match is which script runs
        # with which arguments, and that survives both.
        assert sorted(shlex.split(table[key])) == sorted(shlex.split(command)), (
            f"{key!r} does something else on the menu than in the table:\n"
            f"  table: {table[key]}\n  menu:  {command}")


def test_the_menu_carries_every_action_a_key_does(screen):
    """The other direction: a key bound in the table and absent from the menu is
    undiscoverable, which is what the menu exists to prevent. The nine seat
    digits are the deliberate exception — ten more rows would be a worse menu,
    and the title says what they do instead."""
    screen("-n", "2")
    table = {k: v for k, v in qb_table(screen).items()
             if k != "Any" and not k.isdigit()}
    on_menu = {key for key, _, _ in qb_menu(screen) if key}
    assert set(table) == on_menu, f"table {sorted(table)} vs menu {sorted(on_menu)}"
    title = qb_table(screen)["Any"]
    assert "1-9" in title, f"nothing tells a reader what the digits do: {title}"


def test_the_digits_jump_to_seats(screen):
    """Bound flat 1 to 9 rather than one per seat that exists. A screen grows and
    shrinks under `--add` and the ✕, and a table rebuilt on every change is a
    table that is stale between them; a digit naming a seat that is not there
    reports it the way the bar's cells do."""
    screen("-n", "2")
    table = qb_table(screen)
    for n in range(1, 10):
        assert str(n) in table, f"{n} is not bound"
        assert action_of(table[str(n)]) == f"seat{n}", table[str(n)]


def test_the_tape_toggle_puts_the_screen_back_exactly(screen):
    """Hidden with `break-pane -d` to a holding window, brought back with
    `join-pane`. The `-v -f -l` is verbatim what qb-seats' own split is, so the
    tape comes back as a strip off the bottom of the WHOLE window."""
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("-n", "3")
    before = geometry(screen)
    tape = pane_id(screen, "tape")
    assert tape, "no tape to toggle"

    assert seat_key(screen, "tape", "t").returncode == 0
    hidden = geometry(screen)
    assert tape not in hidden, "the tape is still in the window"
    assert len(hidden) == len(before) - 1
    state = screen.tmux("show-options", "-v", "-t", "=t:", "@qb_hidden_tape").stdout.split()
    assert state[0] == tape, state
    assert state[2:], "no widths were recorded, so nothing can be put back"

    assert seat_key(screen, "tape", "t").returncode == 0
    assert geometry(screen) == before, "the tape came back to a different screen"
    assert screen.tmux("show-options", "-v", "-t", "=t:",
                       "@qb_hidden_tape").stdout.strip() == ""


def test_the_dash_comes_back_above_the_tape_and_not_beside_it(screen):
    """The regression this toggle is most likely to have, and the reason showing
    the dash replays the build order rather than simply joining it.

    qb-seats splits the dash off the whole window FIRST and takes the tape's
    strip off the bottom afterwards, which is what leaves the dash above the tape
    rather than beside it. Join the dash back with the tape already in place and
    `-f` gives it the full height of the window instead: measured as a 78x44 dash
    down the side of a 121-column tape, on a screen whose dash had been 78x32
    over a full-width one. It looks almost right, which is the problem.
    """
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("-n", "3")
    before = geometry(screen)

    assert seat_key(screen, "dash", "t").returncode == 0
    assert pane_id(screen, "dash") not in geometry(screen)

    assert seat_key(screen, "dash", "t").returncode == 0
    got = labels(screen)
    dash_w, dash_top = got["dash"]
    tape_w, tape_top = got["tape"]
    assert dash_top < tape_top, "the dash came back beside the tape, not above it"
    assert tape_w > dash_w, "the tape stopped spanning the full width"
    assert geometry(screen) == before, "the dash came back to a different screen"


def test_expanding_the_dash_gives_it_a_window_and_puts_the_screen_back_exactly(screen):
    """`z` — out of the row into a window of its own, and back with the geometry.

    The same break-and-rejoin `d` uses, so it inherits the thing that was hard
    about that one: the widths are recorded BEFORE the break, because afterwards
    the dash's columns have already gone to a neighbour and recording then puts
    the screen back the way the break left it. Asserted on the whole screen and
    not on the dash alone — a rejoined dash at the right width with the seats
    beside it wrong is the failure that looks like success.
    """
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("-n", "3")
    before = geometry(screen)
    dash = pane_id(screen, "dash")
    assert dash, "this screen was supposed to have a dash"

    assert seat_key(screen, "expand", "t").returncode == 0
    assert pane_id(screen, "dash") is None, "the dash is still in the seats window"
    assert screen.tmux("show-options", "-t", "t:", "-qv",
                       "@qb_dash_expanded").stdout.strip() == dash

    assert seat_key(screen, "expand", "t").returncode == 0
    assert pane_id(screen, "dash") == dash, "a different pane came back"
    assert geometry(screen) == before, "the screen came back a different shape"
    for option in ("@qb_dash_expanded", "@qb_hidden_dash"):
        left = screen.tmux("show-options", "-t", "t:", "-qv", option).stdout.strip()
        assert left == "", f"{option} outlived the collapse: {left!r}"


def test_an_expanded_dash_is_visible_rather_than_parked(screen):
    """The whole difference between this and `d`, and it is one flag.

    `break-pane -d` leaves the pane in a window nobody is looking at, which is
    what hiding means; without it the client follows the pane to its new window,
    which is what expanding means. Same recording, same way back — so the only
    thing worth asserting is that the window is the current one and is named for
    reading rather than for parking.
    """
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("-n", "2")
    assert seat_key(screen, "expand", "t").returncode == 0

    name = screen.tmux("display-message", "-p", "-t", "t:",
                       "#{window_name}").stdout.strip()
    assert name == "qb-dash", f"the expanded dash landed in {name!r}"
    assert screen.tmux("display-message", "-p", "-t", "t:",
                       "#{window_panes}").stdout.strip() == "1", \
        "the expanded dash is sharing its window"


def test_expanding_keeps_the_process_that_was_in_the_pane(screen):
    """Which is the whole argument for `break-pane` over a popup running a second
    dashboard: the expanded pane is the SAME process, so it has everything it had
    already polled and there is no "waiting for gh" caption on the way in."""
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("-n", "2")
    dash = pane_id(screen, "dash")
    was = screen.tmux("display-message", "-p", "-t", dash, "#{pane_pid}").stdout.strip()
    assert was.isdigit(), f"the dash has no process at all: {was!r}"

    assert seat_key(screen, "expand", "t").returncode == 0
    assert seat_key(screen, "expand", "t").returncode == 0
    assert pane_id(screen, "dash") == dash, "the pane was replaced rather than moved"
    assert screen.tmux("display-message", "-p", "-t", dash,
                       "#{pane_pid}").stdout.strip() == was, \
        "the dash came back with a different process in it"


def test_expanding_a_hidden_dash_shows_it_rather_than_putting_it_in_the_row(screen):
    """The one crossing between the two toggles that had to be decided.

    `d` means "in the row or not" and `z` means "full screen or not", so a hidden
    dash asked to expand is asked for the thing it is one step from — not brought
    back to its 78-column column, which is `d`'s answer to a different question,
    and not refused, which answers nothing. It is also the cheap direction: the
    pane is already alone in a window, so this is a rename and a select and no
    geometry moves. A `break-pane` here would fail outright.
    """
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("-n", "3")
    before = geometry(screen)
    dash = pane_id(screen, "dash")

    assert seat_key(screen, "dash", "t").returncode == 0            # hide it
    assert seat_key(screen, "expand", "t").returncode == 0          # now show it big
    assert pane_id(screen, "dash") is None, "the dash went back into the row"
    assert screen.tmux("display-message", "-p", "-t", "t:",
                       "#{window_name}").stdout.strip() == "qb-dash"
    assert screen.tmux("show-options", "-t", "t:", "-qv",
                       "@qb_dash_expanded").stdout.strip() == dash

    # And the way back is still one route, with the geometry hide_pane recorded
    # before any of this — which is the thing a rename could quietly have lost.
    assert seat_key(screen, "expand", "t").returncode == 0
    assert geometry(screen) == before, "the round trip through both states lost the row"


def test_a_nudge_says_which_of_the_two_states_the_dash_is_in(screen):
    """"Hidden" about a dash filling the screen in front of you is the kind of
    wrong answer that makes somebody doubt the tool rather than the state."""
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("-n", "2")

    assert seat_key(screen, "expand", "t").returncode == 0
    nudged = seat_key(screen, "wider", "t")
    assert nudged.returncode != 0
    assert "expanded" in nudged.stderr, nudged.stderr
    assert "hidden" not in nudged.stderr, \
        f"an expanded dash was reported as hidden: {nudged.stderr}"

    assert seat_key(screen, "expand", "t").returncode == 0
    assert seat_key(screen, "dash", "t").returncode == 0
    hidden = seat_key(screen, "wider", "t")
    assert hidden.returncode != 0
    assert "hidden" in hidden.stderr, hidden.stderr


def test_the_dash_toggle_also_brings_back_an_expanded_dash(screen):
    """One route into the row, whichever route left it.

    Expanded and hidden are both "the dash is not in the seats window", and they
    come back the same way — so `d` on an expanded dash must put it back rather
    than refusing or, worse, breaking it out a second time.
    """
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("-n", "3")
    before = geometry(screen)

    assert seat_key(screen, "expand", "t").returncode == 0
    assert seat_key(screen, "dash", "t").returncode == 0
    assert geometry(screen) == before, "`d` brought an expanded dash back wrong"
    assert screen.tmux("show-options", "-t", "t:", "-qv",
                       "@qb_dash_expanded").stdout.strip() == "", \
        "the expanded marker outlived a collapse made with `d`"


def test_killing_the_expanded_dash_takes_both_markers_with_it(screen):
    """A dead pane must not leave a screen describing one.

    Two options describe the expanded dash — `@qb_hidden_dash` is where it is
    parked, `@qb_dash_expanded` is which of the two ways it got there — and the
    pane-is-gone branch used to clear only the first. What was left was a screen
    marked expanded with nothing recorded, so every later `z` took the expanded
    branch and reported "nothing recorded to put back": the wrong problem named,
    on a screen whose real one is that the dash process died. Neither key could
    put it right, because the marker is the only thing either of them reads.

    Closing the window the dash was expanded into is not a contrived way to reach
    that — it is `C-q z` followed by the reflex of closing a window you are done
    with, and the dash is the only pane in it.
    """
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("-n", "3")
    assert seat_key(screen, "expand", "t").returncode == 0
    dash = screen.tmux("show-options", "-t", "t:", "-qv",
                       "@qb_dash_expanded").stdout.strip()
    assert dash, "the dash did not record itself as expanded"

    screen.tmux("kill-pane", "-t", dash)
    # It refuses, and that is right — there is no dash to bring back. What it
    # must not do is refuse the same way for ever.
    assert seat_key(screen, "expand", "t").returncode != 0
    for option in ("@qb_dash_expanded", "@qb_hidden_dash"):
        left = screen.tmux("show-options", "-t", "t:", "-qv", option).stdout.strip()
        assert left == "", f"{option} outlived the pane it describes: {left!r}"

    # And the message now names the screen's actual state rather than the marker's.
    again = seat_key(screen, "expand", "t")
    assert "marked expanded" not in again.stderr, \
        f"still answering from a marker that was cleared: {again.stderr!r}"


def test_a_marker_left_without_a_record_clears_itself(screen):
    """The other order the same contradiction can arrive in, and its only exit.

    `expand_dash` asks `@qb_dash_expanded` first and hands off to `restore_dash`,
    which answers from `@qb_hidden_dash`. A marker with no record is a state that
    describes no pane, and leaving it set made this refusal the answer to every
    later `z` — so `restore_dash` returning 1 is taken as the proof the marker is
    wrong, and it does not survive being disproved.
    """
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("-n", "2")
    dash = pane_id(screen, "dash")
    screen.tmux("set-option", "-t", "t:", "@qb_dash_expanded", dash)

    first = seat_key(screen, "expand", "t")
    assert first.returncode != 0, "a marker with no record was acted on"
    assert screen.tmux("show-options", "-t", "t:", "-qv",
                       "@qb_dash_expanded").stdout.strip() == "", \
        "the disproved marker survived being disproved"
    # With it gone, `z` reads the screen as it is — a dash in the row, to expand.
    assert seat_key(screen, "expand", "t").returncode == 0
    assert pane_id(screen, "dash") is None, "the second z did not expand the dash"


def test_the_top_line_carries_a_clickable_expand(screen):
    """`#[range=…]` is honoured in status-format and nowhere else, which is the
    whole reason a control can live on a status line — and the top line is where
    this one belongs, since every cell on the seat bar names a seat and a control
    for the pane down the right would be the exception a reader has to learn."""
    screen("-n", "2")
    fmt = top_format(screen)
    assert "#[range=user|expand]" in fmt, f"no expand widget on the top line: {fmt}"
    assert "⛶" in fmt, "the range is there and has nothing in it"
    # `norange` and not `default`: the note in seat_bar applies here too — a
    # `#[default]` jumps back to status-style, which is the theme's green.
    after = fmt.split("#[range=user|expand]", 1)[1]
    assert "#[norange]" in after.split("#{@qb_top}", 1)[0], \
        "the range is never closed, so the whole line is one click target"


def test_the_expand_widget_reaches_qb_seat_key(screen):
    """The ⛶, `C-q z` and the dash's own `z` are three front ends onto ONE
    definition of expanding, and this is the join for the first of them.

    Driven through qb-seat-click by the range name the status line would send,
    which is the part that can be tested without synthesising a mouse event —
    the same split the seat bar's own widgets are tested along.
    """
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("-n", "2")
    before = geometry(screen)

    done = click(screen, "expand", "t")
    assert done.returncode == 0, done.stderr
    assert pane_id(screen, "dash") is None, "the click did not expand the dash"

    assert click(screen, "expand", "t").returncode == 0
    assert geometry(screen) == before, "the click did not put the screen back"


def test_a_click_on_the_top_line_reaches_the_dispatcher_too(screen):
    """The join the test above deliberately skips, and the one that was broken.

    `qb-seat-click expand` worked from the moment it was written; the ⛶ still did
    nothing, because the mouse binding gated on
    `#{==:#{mouse_status_line},#{@qb_bar}}` — true of the seat bar, line 1, and
    of nothing else. The widget drew, registered its range, and every click on it
    fell through to `switch-client -t =`. Both halves of the feature passed their
    own tests; what nobody owned was the line between them.

    A status-line click cannot be synthesised — tmux routes it to the client, not
    to a pane, so there is no `send-keys` that produces one — so this asserts the
    property that made the click unreachable rather than the click. The binding
    must decide on the SCREEN and not on the line: both status lines are ours,
    every range on either is one this script put there, and which widget was hit
    is the range's job to say.
    """
    screen("-n", "2")
    lines = [ln for ln in screen.tmux("list-keys", "-T", "root").stdout.splitlines()
             if re.search(r"-T\s+root\s+MouseDown1Status\s", ln)]
    assert len(lines) == 1, lines
    assert "mouse_status_line" not in lines[0], (
        "the binding gates on which status line was clicked, so a widget on the "
        "top line registers its range and is then dropped — which is exactly how "
        "the ⛶ shipped inert")
    # And the widget it could not reach is on the line that was being excluded.
    assert "#[range=user|expand]" in top_format(screen), \
        "the ⛶ is no longer on the top line, so this test is measuring nothing"


def test_a_hidden_pane_keeps_the_process_that_was_in_it(screen):
    """Which is the whole reason this is `break-pane` and not "kill it and split a
    new one": a tape that restarted would lose everything it had followed, and a
    dash would come back to a blank pane and a poll interval.

    THE PID, NOT THE TEXT ON SCREEN. This asserted that the stub's output was
    still in the pane, which is a proxy for the claim and a fragile one — the
    board stub `printf`s without a newline, so the shell's prompt lands on the
    same line and readline redraws it from column 0, wiping the output. That
    depends on the shell, the width and the timing: it passed 139 runs locally
    and in the flake sandbox, and failed once on CI. The pane's pid is the claim
    itself, and it cannot be redrawn away.
    """
    screen("-n", "2")

    def the_tape():
        return pane_id(screen, "tape") or [p for p, n in panes(screen) if not n][0]

    def pid_of(pane):
        return screen.tmux("display-message", "-p", "-t", pane,
                           "#{pane_pid}").stdout.strip()

    tape = the_tape()
    was = pid_of(tape)
    assert was.isdigit(), f"the tape has no process at all: {was!r}"

    assert seat_key(screen, "tape", "t").returncode == 0
    assert seat_key(screen, "tape", "t").returncode == 0
    assert the_tape() == tape, "the pane was replaced rather than moved"
    assert pid_of(tape) == was, "the pane came back with a different process in it"


def test_the_tape_toggle_works_on_a_screen_with_no_dash(screen):
    """qb-seats labels the tape `tape` only when there is a dash to tell it apart
    FROM — on a one-auxiliary-pane screen the border has said `board` since that
    script existed. So a lookup by label alone finds nothing on exactly the
    screens most likely to want the toggle, and the fallback is the pane that is
    neither a seat nor labelled anything."""
    screen("-n", "2")                     # the fixture builds no dash by default
    assert pane_id(screen, "tape") is None, "this screen was supposed to have no dash"
    before = geometry(screen)
    assert len(aux_panes(screen)) == 1

    assert seat_key(screen, "tape", "t").returncode == 0
    assert aux_panes(screen) == [], "the unlabelled tape was not found"
    assert seat_key(screen, "tape", "t").returncode == 0
    assert geometry(screen) == before


def test_two_screens_disagree_about_whether_their_tape_is_showing(screen):
    """Which is why the state is a SESSION option and not a server one. A server
    option would make the second screen to toggle answer for the first, and the
    two are different screens on purpose."""
    screen("-n", "2", name="one")
    screen("-n", "2", name="two")
    assert seat_key(screen, "tape", "one", name="one").returncode == 0
    assert aux_panes(screen, "one") == []
    assert len(aux_panes(screen, "two")) == 1, "hiding one screen's tape hid the other's"
    assert screen.tmux("show-options", "-v", "-t", "=two:",
                       "@qb_hidden_tape").stdout.strip() == ""


@pytest.mark.parametrize("action", ["dash", "expand", "tape"])
def test_a_pane_taken_out_of_the_row_stays_in_its_own_session(screen, action):
    """`break-pane` with no `-t` uses the CLIENT'S CURRENT session, not the source
    pane's — so on a server running two screens, taking a pane out of the one you
    are not looking at parks it in the other one's window list.

    Everything downstream then fails to find it: `pane_exists` and the whole
    restore path search `list-panes -s -t "$SID"`, which is scoped to the session,
    so the pane is at once alive, stranded, and reported as gone. There is no way
    back for it through this script.

    IT CANNOT BE SEEN WITH ONE SESSION ON THE SERVER, which is why it survived the
    hide/show tests entirely and only turned up when a screen was built beside a
    real one on a developer's box: the dash landed in `seats-quarterback:qb-dash`
    while its own screen reported it missing. Two screens, and the second is the
    current one — which is what makes the wrong answer available.
    """
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("-n", "2", name="one")
    screen("-n", "2", name="two")          # built last, so it is the current session

    assert seat_key(screen, action, "one", name="one").returncode == 0

    stranded = [line for line in screen.tmux(
        "list-panes", "-a", "-F", "#{pane_id} #{session_name}:#{window_name}"
    ).stdout.splitlines() if line.startswith(("%",)) and " two:" in line
        and not line.endswith(":seats")]
    assert stranded == [], f"`{action}` on screen one put a pane in screen two: {stranded}"

    # And the round trip still works, which is the thing the stranding broke: the
    # pane was findable enough to break out and not findable enough to bring back.
    assert seat_key(screen, action, "one", name="one").returncode == 0
    assert len(aux_panes(screen, "one")) == 2, \
        f"`{action}` could not put the pane back on its own screen"


def test_a_nudge_records_the_width_it_landed_at(screen):
    """@qb_dash_width is what the window-resized hook puts the dash back to on
    every attach and every terminal resize, so a nudge that did not write it
    would be undone by the next one — which is not what somebody pressing `>`
    four times is asking for. What LANDED is recorded rather than what was asked
    for: tmux clamps quietly as well as refusing loudly, and recording the
    request would have the hook asking for a width already turned down."""
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen.env["QB_SEATS_DASH_SIZE"] = "60"
    screen("-n", "2")
    dash = pane_id(screen, "dash")
    assert labels(screen)["dash"][0] == 60

    assert seat_key(screen, "wider", "t").returncode == 0
    got = labels(screen)["dash"][0]
    assert got > 60, f"`>` did not widen the dash: {got}"
    recorded = screen.tmux("show-options", "-p", "-t", dash, "-v",
                           "@qb_dash_width").stdout.strip()
    assert recorded == str(got), f"the dash is {got} wide and asks for {recorded}"

    assert seat_key(screen, "narrower", "t").returncode == 0
    assert labels(screen)["dash"][0] == 60, "`<` did not undo one `>`"


def test_a_nudge_refuses_while_the_dash_is_hidden(screen):
    """Resizing a pane that is parked in the holding window would succeed and
    change nothing anybody can see, and the recorded width would then be one
    chosen against a window of the wrong size."""
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("-n", "2")
    assert seat_key(screen, "dash", "t").returncode == 0
    done = seat_key(screen, "wider", "t")
    assert done.returncode == 1
    assert "hidden" in done.stderr, done.stderr


def test_close_acts_on_the_pane_the_key_was_pressed_in(screen):
    """The ✕ knows which seat it is because the click named one; a key knows only
    where it was pressed, so the seat number comes off the pane's own @qb_seat."""
    screen("-n", "3")
    wait_for_log(screen.log, 3)
    pane = next(p for p, n in panes(screen) if n == "2")
    assert seat_key(screen, "close", "t", pane).returncode == 0
    assert sorted(n for _, n in panes(screen) if n) == ["1", "3"]


def test_close_refuses_a_pane_that_is_not_a_seat(screen):
    """Press it in the tape or the dash and the honest answer is that there is no
    seat here. Closing the board pane is what a missing guard did to
    qb-seat-click, on a screen whose whole point is having one."""
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("-n", "2")
    before = panes(screen)
    for label in ("dash", "tape"):
        pane = pane_id(screen, label)
        done = seat_key(screen, "close", "t", pane)
        assert done.returncode == 1, f"closing the {label} was allowed"
        assert "not a seat" in done.stderr, done.stderr
    assert panes(screen) == before, "something moved anyway"


def test_close_ends_the_agents_session_before_the_pane_goes(screen, tmp_path):
    """The keyboard has to do what the ✕ does, and this is the assertion that it
    is the SAME code rather than a second copy of it.

    A `kill-pane` SIGHUPs the agent, and Claude Code's SessionEnd hook is not
    documented to survive that — so a close that skipped the qb-end call would
    leave the board holding a live lease and every claim that session had taken,
    for the rest of their TTL (#277). Nothing would look wrong while it happened,
    which is exactly why the key delegates to qb-seat-click rather than
    reimplementing the path.
    """
    screen("-n", "2")
    wait_for_log(screen.log, 2)
    pane = next(p for p, n in panes(screen) if n == "1")
    screen.tmux("set-option", "-p", "-t", pane, "@qb_session", "sid-of-seat-1")

    log, env = stub_qb_end(screen, tmp_path)
    done = subprocess.run([str(BIN / "qb-seat-key"), "close", "t", pane], env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr

    assert log.exists(), "the key closed the pane without telling the board"
    said = log.read_text()
    assert "sid-of-seat-1" in said
    assert "--reason killed" in said
    assert sorted(n for _, n in panes(screen) if n) == ["2"]


def test_the_bindings_hand_each_action_the_screen_and_the_pane(screen):
    """Where qb-seat-click reads a server option back, this passes arguments.

    The ✕ has to stash because `#{mouse_status_range}` is scoped to a mouse EVENT
    and expands to nothing by the time `confirm-before` runs its command. A
    session and a pane are CLIENT state, so a binding can expand them and pass
    them — which is also the better answer, because a server option is a race
    between two clients pressing the key on one server and an argument cannot be.

    The id and not the name: the value crosses tmux's expansion into a shell
    command line, where a session called `it's` would leave an unterminated quote.
    """
    screen("-n", "2")
    for key, cmd in qb_table(screen).items():
        if key == "Any" or "qb-seat-key" not in cmd:
            continue
        if action_of(cmd) == "guide":
            # The one that cannot be handed anything: `display-popup` does not
            # format-expand its command, so the guide asks tmux instead.
            assert "#{" not in cmd, f"the guide is a popup and cannot be told: {cmd}"
            continue
        assert "'#{session_id}'" in cmd, f"{key!r} is told no screen: {cmd}"
        assert "'#{pane_id}'" in cmd, f"{key!r} is told no pane: {cmd}"
        assert "#{session_name}" not in cmd, (
            f"{key!r} carries a NAME, which a session called `it's` breaks: {cmd}")


def test_an_action_takes_the_screens_id_as_well_as_its_name(screen):
    """The id is what the bindings pass; the name is what `list` prints and what
    `qb-seat-click` and `qb-seats -s` take. Both have to reach the same screen."""
    screen("-n", "3")
    wait_for_log(screen.log, 3)
    sid = screen.tmux("display-message", "-p", "-t", "=t:", "#{session_id}").stdout.strip()
    assert sid.startswith("$"), sid
    pane = next(p for p, n in panes(screen) if n == "2")
    assert seat_key(screen, "close", sid, pane).returncode == 0
    assert sorted(n for _, n in panes(screen) if n) == ["1", "3"]


def test_a_digit_jumps_from_the_key_and_the_menu_leads_back_to_it(screen):
    """The exact path a user took, end to end, because every part of it worked in
    isolation and the whole did not.

    Press the key, see nothing happen, press it again — that lands on `Any` and
    opens the menu, which has no digit accelerators, so `2` does nothing at all.
    `C-q 2` on its own was always fine. So: the digit works from the key, the
    double press still reaches the menu, and the menu's `j` hands you back to the
    table where a digit means a seat.
    """
    screen("-n", "3")
    wait_for_log(screen.log, 3)

    def active():
        return screen.tmux("display-message", "-p", "-t", "t:seats",
                           "#{@qb_seat}").stdout.strip()

    with attached_client(screen, 200, 50) as press:
        press("\x11", "2")                       # straight from the key
        assert wait_until(lambda: active() == "2"), f"C-q 2 did not jump: {active()}"

        press("\x11", "\x11")                    # the double press: the menu
        press("j")                               # ...which leads back to the table
        press("3")
        assert wait_until(lambda: active() == "3"), \
            f"the menu's seat row did not lead back to the digits: {active()}"


# ---- the bar's colours ------------------------------------------------------
# These pin a PROPERTY rather than a palette: every foreground the bar sets has a
# background set beside it, and every resulting pair is legible. Which colours
# they are is a taste that may move; that they are readable is not.

_STYLE = re.compile(r"#\[(fg|bg)=([A-Za-z0-9]+)\]")
_XTERM_BASIC = [
    (0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0), (0, 0, 128), (128, 0, 128),
    (0, 128, 128), (192, 192, 192), (128, 128, 128), (255, 0, 0), (0, 255, 0),
    (255, 255, 0), (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
]
_NAMED = {"black": 0, "red": 1, "green": 2, "yellow": 3, "blue": 4, "magenta": 5,
          "cyan": 6, "white": 7}


def _rgb(name):
    """An xterm-256 colour as RGB, or None for one this cannot resolve."""
    if name in _NAMED:
        name = f"colour{_NAMED[name]}"
    if not name.startswith("colour"):
        return None
    n = int(name[len("colour"):])
    if n < 16:
        return _XTERM_BASIC[n]
    if n < 232:
        n -= 16
        level = [0, 95, 135, 175, 215, 255]
        return (level[n // 36], level[(n // 6) % 6], level[n % 6])
    v = 8 + (n - 232) * 10
    return (v, v, v)


def _contrast(fg, bg):
    """WCAG contrast ratio between two xterm colours. 4.5:1 is the readable floor."""
    def lum(c):
        def channel(x):
            x /= 255
            return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
        r, g, b = c
        return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
    a, b = lum(_rgb(fg)), lum(_rgb(bg))
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def bar_pairs(fmt):
    """[(fg, bg)] for every foreground the format sets, in order.

    A `#[fg=…]` with no `#[bg=…]` beside it yields a bg of None — which is the
    bug this exists for, not an omission in the parser: it means the span takes
    whatever `status-style` happens to be.
    """
    styles = _STYLE.findall(fmt)
    pairs, i = [], 0
    while i < len(styles):
        kind, value = styles[i]
        if kind == "fg":
            nxt = styles[i + 1] if i + 1 < len(styles) else None
            if nxt and nxt[0] == "bg":
                pairs.append((value, nxt[1])); i += 2; continue
            pairs.append((value, None))
        i += 1
    return pairs


def test_the_bar_never_borrows_the_themes_background(screen):
    """Every span used to set a foreground only and inherit `status-style`, which
    on a stock tmux is `bg=green,fg=black`. Read off the wire with a real client
    attached, the terminal was being sent `ESC[38;5;108m ESC[42m` for the ＋ —
    green on green — and `ESC[38;5;167m ESC[42m` for the ✕.

    A foreground without a background is the whole defect, so that is what this
    looks for. It cannot be checked by reading colours off the rendered line,
    because what makes it wrong is a colour the bar never names.
    """
    screen("-n", "2")
    fmt = screen.tmux("show-options", "-v", "-t", "=t:", "status-format[1]").stdout
    naked = [fg for fg, bg in bar_pairs(fmt) if bg is None]
    assert not naked, (
        f"these set a foreground and take whatever status-style is: {naked}")
    # And the line itself, or the cells are dark islands in the theme's green:
    # tmux pre-fills the status line with status-style and draws the format over
    # it, so only `fill=` makes the bar a strip.
    assert "#[fill=" in fmt, f"the bar does not paint its own line: {fmt}"


def test_every_colour_pair_on_the_bar_is_legible(screen):
    """4.5:1 is the readable floor. What shipped was 1.39:1 to 2.78:1 throughout,
    because every pair had the theme's green on one side of it.

    The ratios are computed rather than the colour numbers asserted: which colours
    the bar uses is a taste that may move, and that they are readable is not.
    """
    screen("-n", "2")
    fmt = screen.tmux("show-options", "-v", "-t", "=t:", "status-format[1]").stdout
    checked = 0
    for fg, bg in bar_pairs(fmt):
        if bg is None or _rgb(fg) is None or _rgb(bg) is None:
            continue        # the naked-foreground case is the test above
        ratio = _contrast(fg, bg)
        assert ratio >= 4.5, f"{fg} on {bg} is {ratio:.2f}:1, and 4.5:1 is the floor"
        checked += 1
    assert checked >= 6, f"only {checked} pairs were checked; the parser has drifted"


def test_the_bar_says_the_key_table_is_waiting(screen):
    """Pressing the key switches the client into a key table and tmux says
    nothing about it — its own prefix is the same and its users know. Here nobody
    did: the first press looked like a dead key, the natural next move was to
    press it again, that lands on `Any` and opens the menu, and the menu has no
    digit accelerators — so `1`-`9` did nothing while the menu's title promised
    they jumped to a seat. One invisible state, reported as three bugs.

    `#{client_key_table}` is the whole fix, and the bar already redraws on every
    change, so the strip appears the instant the key is pressed.
    """
    screen("-n", "2")
    fmt = screen.tmux("show-options", "-v", "-t", "=t:", "status-format[1]").stdout
    assert "#{==:#{client_key_table},qb}" in fmt, f"the bar says nothing about the key: {fmt}"
    # NO COMMA IN THE HINT. `,` separates a conditional's arms, so one would end
    # the arm early and print the rest of the strip unconditionally — in every
    # session on the box, since status-format is read per client.
    arm = fmt.split("#{==:#{client_key_table},qb},", 1)[1]
    arm = arm[:arm.rindex(",}")]          # the conditional's own closing arm
    said = re.sub(r"#\[[^\]]*\]", "", arm)   # the literal text, styles removed
    assert "," not in said, f"a comma in the hint ends the conditional early: {said!r}"
    for key in ("a add", "x close", "1-9 seat", "t tape", "d dash", "? keys"):
        assert key in said, f"the hint does not teach {key!r}: {said}"


def test_the_hint_appears_while_the_key_waits_and_goes_when_it_is_used(screen):
    """The bar is a format, so this is the only way to see what it renders: with
    a client attached and the key actually pressed."""
    screen("-n", "2")
    with attached_client(screen, 200, 50) as press:
        assert "qb" not in bar(screen).split("＋ seat")[-1], "the hint is up before any key"
        press("\x11")                     # C-q, and nothing after it
        assert wait_until(lambda: "1-9 seat" in bar(screen)), \
            f"the bar does not say the key is waiting: {bar(screen)}"
        press("t")                        # spend the key
        assert wait_until(lambda: "1-9 seat" not in bar(screen)), \
            "the hint stayed up after the key was used"


def test_the_guide_fits_the_popup_it_is_opened_in(screen):
    """A line longer than the popup wraps, and a wrapped cheatsheet is worse than
    none. This shipped at 79 columns inside a 78-column popup — whose border
    takes two more — and the last paragraph folded. The width is read out of the
    binding rather than written here, so the text and the popup cannot drift.
    """
    screen("-n", "2")
    opens = qb_table(screen)["?"]
    words = shlex.split(opens)
    cols = int(words[words.index("-w") + 1])
    rows = int(words[words.index("-h") + 1])

    said = seat_key(screen, "guide", "t")
    assert said.returncode == 0, said.stderr
    lines = said.stdout.splitlines()
    # CHARACTERS, not bytes: the guide carries an em dash and an ellipsis, and
    # `len()` on the encoded form would call a fitting line too wide.
    widest = max(len(line) for line in lines)
    assert widest <= cols - 2, (
        f"the guide is {widest} columns wide and the popup is {cols} "
        f"({cols - 2} inside its border)")
    assert len(lines) <= rows - 2, (
        f"the guide is {len(lines)} lines and the popup holds {rows - 2}")


def test_the_menu_does_not_promise_what_a_menu_cannot_do(screen):
    """Its title said "1-9 jumps to a seat". A `display-menu` has no digit
    accelerators, so pressing one did nothing at all — which is how the digits
    came to be reported as broken when they worked perfectly from the key.

    The honest version is a row that hands you back to the table, which is the
    one place a digit means a seat.
    """
    screen("-n", "2")
    # The -T argument alone, not the whole binding: one of the menu ROWS says
    # "then 1-9" and is meant to.
    words = shlex.split(qb_table(screen)["Any"])
    title = words[words.index("-T") + 1]
    assert "1-9" not in title, f"the menu still promises the digits: {title!r}"
    back = [(k, label, cmd) for k, label, cmd in qb_menu(screen)
            if cmd == "switch-client -T qb"]
    assert len(back) == 1, f"nothing on the menu leads to the digits: {qb_menu(screen)}"
    key, label, _ = back[0]
    assert key not in ("", None) and "seat" in label, (key, label)


def test_a_real_keystroke_reaches_the_action(screen):
    """The one thing nothing else here can prove: that the binding FIRES.

    Every other test in this section drives `qb-seat-key` directly or reads the
    key table back, and all of them would pass with a root binding that never
    matched, a gate that was always false, or a `switch-client -T` naming a table
    that is not there. This is the join, and it is testable for the reason the
    bar's click is not: a click means SGR mouse bytes and a status line whose
    geometry the test would have to work out, while `C-q t` is two bytes written
    to a pty.
    """
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("-n", "2")
    assert pane_id(screen, "tape"), "no tape to hide"
    with attached_client(screen, 200, 50) as press:
        press("\x11", "t")                  # C-q, then t
        assert wait_until(lambda: pane_id(screen, "tape") is None), \
            "C-q t did not hide the tape"
        press("\x11", "t")
        assert wait_until(lambda: pane_id(screen, "tape") is not None), \
            "C-q t did not bring the tape back"


def test_a_key_this_does_not_know_changes_nothing(screen):
    """`Any` hands the unbound key over so the menu can teach it, and an action
    this script has never heard of must not fill the status line with a complaint
    about it."""
    screen("-n", "2")
    before = geometry(screen)
    for junk in ("floop", "seat", "seatx", "", "--help"):
        done = seat_key(screen, junk, "t")
        assert done.returncode in (0, 1, 2), f"{junk!r} → {done.returncode} {done.stderr}"
    assert geometry(screen) == before, "an unknown action moved the furniture"


def test_the_guide_names_the_key_this_screen_uses(screen):
    """A cheatsheet for somebody else's keyboard is worse than none, so it is read
    off @qb_key rather than written into the text. It is also the one action that
    needs no server — `display-popup` runs it in a pane, a human runs it in a
    shell to find out what the key does, and this reads it."""
    screen.env["QB_SEATS_KEY"] = "M-q"
    try:
        screen("-n", "2")
    finally:
        del screen.env["QB_SEATS_KEY"]
    said = seat_key(screen, "guide", "t")
    assert said.returncode == 0, said.stderr
    assert "M-q is the qb key" in said.stdout, said.stdout
    for key in ("a", "x", "t", "d", "?"):
        assert re.search(rf"^\s+{re.escape(key)}\s", said.stdout, re.M), \
            f"{key!r} is bound and the guide does not mention it:\n{said.stdout}"

    # No server, no session, and it still answers — with the default, which is
    # what a screen this user has not reconfigured will be using.
    bare = subprocess.run([str(BIN / "qb-seat-key"), "guide"],
                          env={k: v for k, v in screen.env.items() if k != "TMUX"},
                          capture_output=True, text=True, timeout=60)
    assert bare.returncode == 0, bare.stderr
    assert "C-q is the qb key" in bare.stdout


def test_killing_a_named_screen_needs_no_repo(screen):
    """`K` reaches `qb-seats --kill` through a `run-shell`, whose cwd is the tmux
    SERVER's — wherever that server was started, which need not be a repo at all.
    A kill that was told which screen must therefore not need one, the same way
    `list`, `resume` and `--dash-fit` do not: all four are about a screen that
    already exists. It used to refuse with "not in a git repo" against a screen it
    could see."""
    screen("-n", "2")
    outside = tempfile.mkdtemp(prefix="qb-notarepo-")
    try:
        done = subprocess.run([str(QB_SEATS), "--kill", "-s", "t"], cwd=outside,
                              env=screen.env, capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, f"{done.returncode}: {done.stderr}"
        assert screen.tmux("has-session", "-t", "=t").returncode != 0, "the screen is still up"
    finally:
        shutil.rmtree(outside, ignore_errors=True)


# ---- the dash pane -----------------------------------------------------------
#
# The tape says what just happened, the dash says what is true now. Both belong on
# the screen, which is why the dash is a second pane rather than a value of
# QB_SEATS_BOARD. It shipped first as dev/seats-extras.sh — scaffolding that
# hardcoded two of one developer's worktrees — and these are the assertions that
# let it move into the script proper (#174).

DASH_STUB = "printf dash-stub; sleep 300"


def dash_stubs(tmp_path, *names, can_tui=True, dir=None):
    """A bin directory holding a stub for each name, each printing its own marker.

    Every other dash test pins QB_SEATS_DASH to a command, which says nothing about
    how an UNSET one resolves — the PATH probe, its preference order, and the
    placeholder when neither binary is there had no coverage at all. These stubs are
    what let that be asserted without consulting the machine the suite runs on.

    `--can-tui` IS ANSWERED BEFORE THE MARKER, and the order is the point: since
    #426 `dash_cmd` probes with `qb-dash --can-tui`, and a stub that ignored its
    arguments would print its marker and then `sleep 300` — hanging the resolution
    rather than answering it, in a helper whose whole job is to answer without
    consulting the machine. `can_tui=False` is how a box with rich but no textual
    is simulated, which is the fallback path and cannot otherwise be reached here.

    THE MARKER CARRIES THE ARGUMENTS, because since the review of #426 the two
    renderers are one binary and a flag — `qb-dash --tui` rather than a separate
    `qb-dash-tui` — so the name alone no longer says which one was asked for.
    `dir=` puts the stubs somewhere other than the default, which is how a PATH
    carrying two half-installs is built.
    """
    d = tmp_path / (dir or "dashbin")
    d.mkdir(parents=True, exist_ok=True)
    for name in names:
        stub = d / name
        stub.write_text(
            "#!/bin/sh\n"
            f'[ "$1" = --can-tui ] && exit {0 if can_tui else 1}\n'
            f'printf "{name}-ran $*"\nexec sleep 300\n')
        stub.chmod(0o755)
    return d


def test_a_dash_pane_is_built_by_default(screen, tmp_path):
    """Default-ON is a deliberate divergence from #174, which asked for a flag.
    A screen whose whole job is situational awareness should not need one.

    Unset, not empty: this is the resolution path, so QB_SEATS_DASH is removed from
    the environment rather than set to a stub.

    BOTH renderers are stubbed and the PATH carries no real dash, which this test
    did not need until #426. It prepended one stub to the machine's own PATH, and
    that was safe only while `dash_cmd` consulted nothing else — the flip made it
    look for `qb-dash-tui` too, the installed one answered from further down the
    real PATH, and the pane ran the machine's dashboard against the live board.
    A resolution test that reaches the host it runs on is not testing resolution.
    """
    del screen.env["QB_SEATS_DASH"]
    bindir = dash_stubs(tmp_path, "qb-dash", "qb-dash-tui")
    screen.env["PATH"] = f"{bindir}:{path_with_no_dash_on_it(tmp_path, screen)}"
    screen("-n", "2")
    dash = pane_id(screen, "dash")
    assert dash, "no pane labelled 'dash'"
    # Which renderer is a different test's question; this one is only that the
    # pane exists and something ran in it.
    assert "-ran" in wait_for_pane(screen, dash, "-ran")


def test_the_default_dash_is_the_tui_when_textual_is_there(screen, tmp_path):
    """The clickable renderer is the default since #426.

    It was `qb-dash` under a comment saying the plain one was not better and should
    give up the slot "the moment #209 is fixed": the TUI keyed its seat rows by seat
    NAME, every screen numbers its seats from 1, and a second screen anywhere on the
    box turned this pane into a Textual DuplicateKey traceback. #209 and #208 closed
    on 2026-08-20 — seat rows key on the tmux pane id now — and the workaround then
    outlived its bug by four days because nothing pointed back at this decision.

    Both installed, so this pins the PREFERENCE ORDER rather than availability.

    Asserted on the FLAG rather than on the binary: the review of #426 collapsed
    the launch onto the same `qb-dash` the probe ran, so what says "the clickable
    one" is `--tui` and not a second name.
    """
    del screen.env["QB_SEATS_DASH"]
    bindir = dash_stubs(tmp_path, "qb-dash", "qb-dash-tui")
    screen.env["PATH"] = f"{bindir}:{screen.env['PATH']}"
    screen("-n", "1")
    seen = wait_for_pane(screen, pane_id(screen, "dash"), "qb-dash-ran")
    assert "qb-dash-ran --tui" in seen, seen


def test_without_textual_the_default_falls_back_to_the_plain_dash(screen, tmp_path):
    """A box with rich but no textual gets the plain renderer, not an error pane.

    This is the half of #426 that keeps the plain dash alive: a checkout whose
    mcp/.venv was built without the `tui` extra is not a broken install, and the
    probe is a DEPENDENCY check rather than the crash check it replaced. Simulated
    by a `qb-dash` stub that answers `--can-tui` with 1, which is exactly what the
    real launcher does when no interpreter on the box can import textual.
    """
    del screen.env["QB_SEATS_DASH"]
    bindir = dash_stubs(tmp_path, "qb-dash", "qb-dash-tui", can_tui=False)
    screen.env["PATH"] = f"{bindir}:{screen.env['PATH']}"
    screen("-n", "1")
    seen = wait_for_pane(screen, pane_id(screen, "dash"), "qb-dash-ran")
    assert "qb-dash-ran" in seen, seen
    assert "--tui" not in seen, "the TUI ran without textual to run it on"


def test_the_install_that_answers_the_probe_is_the_install_that_runs(screen, tmp_path):
    """One resolution, not two — the defect a review of #426 caught before it landed.

    `dash_cmd` asked `qb-dash --can-tui` and then launched `qb-dash-tui`, resolving
    the name a second time. That is not the same question twice: `qb-dash-tui` is a
    name rather than an implementation and execs the `qb-dash` BESIDE IT, ignoring
    PATH entirely. So a box carrying the two entry points in two directories — a
    checkout's bin ahead of the installed profile, which the comment above
    `dash_cmd` calls the normal case — probed one install and ran another, and the
    yes it acted on was about neither the interpreter nor the renderer that came up.

    Built here as the skew it actually is: a `qb-dash` that says yes in the first
    directory, a `qb-dash-tui` from some other install further down. Nothing from
    the second may run.
    """
    del screen.env["QB_SEATS_DASH"]
    first = dash_stubs(tmp_path, "qb-dash", dir="checkout-bin")
    second = dash_stubs(tmp_path, "qb-dash-tui", dir="installed-bin")
    screen.env["PATH"] = \
        f"{first}:{second}:{path_with_no_dash_on_it(tmp_path, screen)}"
    screen("-n", "1")
    seen = wait_for_pane(screen, pane_id(screen, "dash"), "qb-dash-ran")
    assert "qb-dash-ran --tui" in seen, seen
    assert "qb-dash-tui-ran" not in seen, \
        "the probe answered for one install and the launch ran another"


def test_a_qb_dash_too_old_to_know_the_probe_falls_back_instead_of_hanging(screen,
                                                                            tmp_path):
    """The upgrade window, which is the one case the probe could have made worse.

    `dash_cmd` runs `qb-dash --can-tui` SYNCHRONOUSLY while building the screen. A
    `qb-dash` predating #426 does not know the flag: it passes it through to the
    renderer, argparse rejects it as an unrecognized argument and it exits 2. That
    is the answer we want — anything nonzero means "not the TUI" — and the screen
    comes up on the plain renderer until the harness is rebuilt.

    Pinned because the failure it rules out is silent and expensive: an older
    `qb-dash` that treated the flag as no flag at all would START THE DASHBOARD
    here, and `dash_cmd` would block until somebody killed it — a seat screen that
    never finishes building, from a probe added to improve it. Exit 2 with a usage
    message on stderr is exactly what the shipped one does, so that is what the
    stub does.
    """
    del screen.env["QB_SEATS_DASH"]
    bindir = tmp_path / "oldbin"
    bindir.mkdir(parents=True, exist_ok=True)
    old = bindir / "qb-dash"
    old.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = --can-tui ]; then\n'
        '  echo "qb-dash: error: unrecognized arguments: --can-tui" >&2\n'
        "  exit 2\n"
        "fi\n"
        "printf qb-dash-ran\nexec sleep 300\n")
    old.chmod(0o755)
    tui = bindir / "qb-dash-tui"
    tui.write_text("#!/bin/sh\nprintf qb-dash-tui-ran\nexec sleep 300\n")
    tui.chmod(0o755)

    screen.env["PATH"] = f"{bindir}:{path_with_no_dash_on_it(tmp_path, screen)}"
    assert screen("-n", "1").returncode == 0
    seen = wait_for_pane(screen, pane_id(screen, "dash"), "qb-dash-ran")
    assert "qb-dash-ran" in seen, seen
    assert "qb-dash-tui-ran" not in seen, "an old qb-dash was read as a yes"


def test_the_tui_alone_on_path_is_still_not_promoted(screen, tmp_path):
    """`qb-dash` on PATH stays the gate, and that survives #426's flip.

    A partial install carrying only the TUI entry point falls to the placeholder
    rather than being promoted. The old reason was that the TUI crashed; the
    surviving reason is narrower and still good — a half-installed harness should
    say so rather than improvise, and the probe itself is `qb-dash --can-tui`, so
    without `qb-dash` there is nothing to ask.
    """
    del screen.env["QB_SEATS_DASH"]
    only_tui = dash_stubs(tmp_path / "tui-only", "qb-dash-tui")
    screen.env["PATH"] = f"{only_tui}:{path_with_no_dash_on_it(tmp_path, screen)}"
    assert screen("-n", "1", name="tuionly").returncode == 0
    seen = wait_for_pane(screen, pane_id(screen, "dash", "tuionly"),
                         "QB_SEATS_DASH=qb-dash-tui")
    assert "QB_SEATS_DASH=qb-dash-tui" in seen, seen
    assert "qb-dash-tui-ran" not in seen, "a partial install promoted the TUI"


def test_with_no_dash_installed_the_pane_explains_itself(screen, tmp_path):
    """Neither binary on PATH is a pane with a reason in it, not a missing pane.

    The message is typed into the pane's shell, so this is also the regression test
    for how it is quoted: `printf %%s\\n %q` emitted a `\\n` that the pane shell ate
    on its way past, printing `...yet.n` and running into the next prompt, and %q
    on a message containing a newline emits bash's `$'...'` form, which a POSIX
    /bin/sh pane prints verbatim. Both are visible in the pane and nowhere else.
    """
    del screen.env["QB_SEATS_DASH"]
    screen.env["PATH"] = path_with_no_dash_on_it(tmp_path, screen)
    assert screen("-n", "1").returncode == 0
    dash = pane_id(screen, "dash")
    assert dash, "no dash pane at all — the explanation has nowhere to go"
    seen = wait_for_pane(screen, dash, "nothing to show here yet")
    assert "dash pane: qb-dash is not on PATH" in seen, seen
    assert "$'" not in seen, "the message arrived in bash's ANSI-C quoting form"
    assert "yet.n" not in seen, "the trailing newline was eaten by the pane's shell"
    # One argument per line, so each line of the message is its own line in the pane.
    assert "Set QB_SEATS_DASH" in seen.split("nothing to show here yet.")[1]


def test_a_named_dash_command_is_actually_RUN_in_the_pane(screen):
    """Every geometry assertion below would pass on a pane the send-keys never
    reached: keys typed before the shell was ready, a value send-keys read as a key
    NAME rather than as text, an unresolvable command. The pane's own output is the
    only witness that the dash was started rather than merely placed."""
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("-n", "2")
    dash = pane_id(screen, "dash")
    assert dash, "no pane labelled 'dash'"
    assert "dash-stub" in wait_for_pane(screen, dash, "dash-stub")


def test_a_dash_command_that_collides_with_a_key_name_is_still_typed(screen):
    """`send-keys` looks its argument up as a key name before falling back to text,
    so a one-word command called `Enter`, `Space` or `Tab` used to be sent as that
    KEY — the pane got a keystroke and no command, silently. -l is the fix."""
    stub = Path(screen.env["PATH"].split(":")[0]) / "Space"
    stub.write_text("#!/bin/sh\nprintf space-stub-ran\nexec sleep 300\n")
    stub.chmod(0o755)
    screen.env["QB_SEATS_DASH"] = "Space"
    screen("-n", "1")
    dash = pane_id(screen, "dash")
    assert "space-stub-ran" in wait_for_pane(screen, dash, "space-stub-ran")


def test_no_dash_command_means_no_dash_pane(screen):
    """Set-and-empty is the off switch, matching QB_SEAT_BRIEF's spelling."""
    screen.env["QB_SEATS_DASH"] = ""
    screen("-n", "2")
    got = aux_panes(screen)
    assert [label for label, _, _ in got] == [""], "the tape should be the only one"


def test_the_tape_is_named_tape_only_when_a_dash_shares_the_screen(screen):
    """pane-border-format falls back to 'board' for an unlabelled pane, which is
    what the bottom pane has always read. Renaming it for everybody to solve a
    problem only the two-pane screen has would be a gratuitous break — so the
    label arrives with the dash and not before.

    The label is all this pins. Asserting the width and top as well made a change
    to either default fail here, with a message about the tape's NAME.
    """
    screen.env["QB_SEATS_DASH"] = ""
    screen("-n", "1", name="plain")
    assert list(labels(screen, "plain")) == [""], "the lone pane must stay unlabelled"

    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("-n", "1", name="withdash")
    assert set(labels(screen, "withdash")) == {"dash", "tape"}


def test_the_pane_border_renders_the_label_and_not_board(screen):
    """The whole argument for @qb_label over a name of its own is that the border
    reads one option. Everything else here reads @qb_label directly, which proves
    only that the script SET it: if pane-border-format's middle case were missing or
    broken, both auxiliary panes would render as 'board' and every other assertion
    in this file would stay green."""
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("-n", "2")
    assert border_label(screen, pane_id(screen, "dash")) == "dash"
    assert border_label(screen, pane_id(screen, "tape")) == "tape"
    seat = [p for p, n in panes(screen) if n == "1"][0]
    assert border_label(screen, seat) == "seat 1"


def test_the_dash_is_a_column_above_the_tape_not_beside_it(screen):
    """Order of construction, asserted as geometry: the dash is split before the
    tape so the tape's -f takes its strip from the bottom of the whole window.
    Reverse them and the tape stops being full width."""
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("-n", "2")
    got = labels(screen)
    dash_w, dash_top = got["dash"]
    tape_w, tape_top = got["tape"]
    assert dash_top < tape_top, "the dash must sit above the tape"
    assert tape_w > dash_w, "the tape stopped spanning the full width"


def test_the_dash_spans_both_rows_of_a_grid(screen):
    """It reports on the screen, not on the top row of it."""
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("7")
    out = screen.tmux("list-panes", "-t", "t:seats", "-F",
                      "#{@qb_seat}\t#{@qb_label}\t#{pane_height}").stdout
    rows = [line.split("\t") for line in out.splitlines() if line]
    seats = [int(h) for seat, _, h in rows if seat]
    dash = [int(h) for seat, label, h in rows if not seat and label == "dash"]
    assert dash[0] > max(seats), "the dash is inside one seat row rather than beside both"


def test_the_dash_width_is_configurable(screen):
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen.env["QB_SEATS_DASH_SIZE"] = "40"
    screen("-n", "2")
    assert labels(screen)["dash"][0] == 40


def test_a_dash_width_that_is_not_a_number_is_refused_before_anything_is_built(screen):
    """The value reaches `split-window -l` verbatim, inside a command substitution
    under `set -e`. A typo therefore used to kill the script with the session, the
    seats and the tape already created but no agent started — on a bare tmux error
    naming neither the variable nor the fix, and with a session name that then made
    the obvious rerun fail too."""
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen.env["QB_SEATS_DASH_SIZE"] = "78px"
    done = screen("-n", "2")
    assert done.returncode == 1, done
    assert "QB_SEATS_DASH_SIZE" in done.stderr, done.stderr
    assert screen.tmux("has-session", "-t", "=t").returncode != 0, \
        "a half-built screen was left behind"


def test_add_does_not_squeeze_the_dash(screen):
    """`select-layout -E` spreads the row the new seat lands in, and the dash is
    in it. Without a reassert, every --add narrows the dashboard a little more."""
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen.env["QB_SEATS_DASH_SIZE"] = "50"
    screen("-n", "2")
    assert labels(screen)["dash"][0] == 50
    screen("--add")
    assert labels(screen)["dash"][0] == 50, "the dash was resized by --add"


def test_add_takes_the_width_from_the_screen_and_not_from_its_own_environment(screen):
    """The requested width is per-screen state, read once when the screen is built
    and recorded on the pane. Resolving QB_SEATS_DASH_SIZE afresh on every
    invocation meant `QB_SEATS_DASH_SIZE=50 qb-seats` followed by a plain
    `qb-seats --add` — from any other shell, or from the ＋ on the seat bar, whose
    environment is the tmux server's — silently resized the dash back to the 78
    nobody asked for. The fixture used to hide this by exporting the variable for
    both calls."""
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen.env["QB_SEATS_DASH_SIZE"] = "50"
    screen("-n", "2")
    assert labels(screen)["dash"][0] == 50
    del screen.env["QB_SEATS_DASH_SIZE"]
    screen("--add")
    assert labels(screen)["dash"][0] == 50, "--add took the width from its environment"


def test_a_width_set_by_hand_survives_an_add(screen):
    """Dragging the border is the other way to set this, and a reflow is no reason
    to undo it: --add reasserts the width the dash HAD, not the one the screen was
    built asking for. Same reassert, better input."""
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen("-n", "2")
    dash = pane_id(screen, "dash")
    screen.tmux("resize-pane", "-t", dash, "-x", "70")
    assert labels(screen)["dash"][0] == 70
    screen("--add")
    assert labels(screen)["dash"][0] == 70, "--add overrode a width set by hand"


def test_closing_a_seat_does_not_squeeze_the_dash(screen):
    """qb-seat-click's reflow runs the same `select-layout -E` for the same reason,
    on the same row, so it needs the same reassert. Only --add had one: three ✕s in
    a row left a dashboard nobody chose the width of."""
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen.env["QB_SEATS_DASH_SIZE"] = "50"
    screen("-n", "3")
    wait_for_log(screen.log, 3)
    assert labels(screen)["dash"][0] == 50
    assert click(screen, "kill3", "t").returncode == 0
    assert sorted(n for _, n in panes(screen) if n) == ["1", "2"]
    assert labels(screen)["dash"][0] == 50, "closing a seat resized the dash"


def test_a_client_attaching_does_not_narrow_the_dash(screen):
    """THE ONE THAT MATTERED, and the one nothing here could catch.

    A detached session is exactly the 240 columns it was built with. Attaching a
    client resizes the WINDOW to the client and rescales every pane in the dash's
    row proportionally — measured on tmux 3.6a: 78 columns asked for, 32 found after
    a 100-column client attached. The reassert this feature shipped with ran at
    BUILD time, before the attach it was written to defend against, so it could not
    survive it: `qb-seats` then `tmux attach` from an ordinary terminal still landed
    on a narrowed dash. The fix is a window hook that runs afterwards. Every other
    test in this file asserts widths on a window no user ever looks at, which is
    precisely how that got as far as review.
    """
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen.env["QB_SEATS_DASH_SIZE"] = "60"
    screen("-n", "2")
    assert labels(screen)["dash"][0] == 60, "wrong before a client even attached"
    with attached_client(screen, 200, 50):
        # 60 of 200 columns is inside the third the dash may take, so it is asked
        # for in full. Unfixed, this comes out at 50 — 60 * 200/240, the rescale.
        assert wait_for_dash_width(screen, 60) == 60


def test_a_narrow_client_costs_the_dash_and_not_the_seats(screen):
    """Why the reassert is clamped rather than absolute. Holding the 78-column
    default on a 100-column window is worse than the bug it fixes: measured, it left
    the two seats 19 columns and ONE column, i.e. it spent the whole terminal on the
    dashboard and none of it on the agents. A third of the window is the ceiling, so
    what a narrow terminal costs is dash.
    """
    screen.env["QB_SEATS_DASH"] = DASH_STUB          # the 78-column default
    screen("-n", "2")
    with attached_client(screen, 100, 30):
        # A third of the window it is actually in, not the 78 it asked for — and not
        # the 78 an unclamped reassert would hold on to.
        assert wait_for_dash_width(screen, 100 // 3) == 100 // 3, "the dash was not clamped"
        rows = screen.tmux("list-panes", "-t", "t:seats", "-F",
                           "#{@qb_seat}\t#{pane_width}").stdout.splitlines()
        seats = [int(r.split("\t")[1]) for r in rows if r and r.split("\t")[0]]
        assert len(seats) == 2, rows
        assert min(seats) >= 25, f"a seat was starved to fit the dash: {seats}"


def resize_hook(run, name="t"):
    """The window-resized hook as tmux stored it, or "" if there is none."""
    out = run.tmux("show-hooks", "-w", "-t", f"{name}:seats").stdout
    return "".join(x for x in out.splitlines() if "window-resized" in x)


def hook_binary(run, name="t"):
    r"""The qb-seats the resize hook will actually run.

    TWO LAYERS COME OFF, and both are the point of the escaping this pins. The hook is
    stored as a tmux command string, so what show-hooks prints is still tmux-escaped:
    a qb-seats under a directory called `a$Bdir` reads `a\$Bdir` there and only
    becomes itself when tmux parses the string to fire the hook. Inside that, the path
    is the first shell-quoted word. No test here puts a quote in a DIRECTORY name, so
    taking the word this plainly is enough.
    """
    m = re.search(r"run-shell -b \"'([^']+)'", resize_hook(run, name))
    if not m:
        return ""
    # `\\+` — ANY RUN of backslashes, not one. How many layers `show-hooks` prints
    # is not portable: the same hook, set the same way on tmux 3.6a, read back with
    # one backslash before the `$` on this box and two on GitHub's runner, so a
    # single-layer peel passed here and failed there. Nothing about the escaping
    # under test differs between them — only how tmux renders it back — and the
    # question this helper exists to answer is "did the path arrive intact", which
    # is the same answer either way. No test puts a literal backslash in a
    # directory name, so collapsing runs cannot lose one that mattered.
    return re.sub(r"\\+(.)", r"\1", m.group(1).replace("##", "#"))


def stale_qb_seats(tmp_path):
    """A qb-seats that predates --dash-fit, in a directory of its own.

    It stands in for the installed copy on a box mid-rollout, and it fails the way
    the real one does: `qb-seats --dash-fit` on the v2.47 build falls through the
    argument parser to `usage; exit 2`.
    """
    d = tmp_path / "stalebin"
    d.mkdir(exist_ok=True)
    stale = d / "qb-seats"
    stale.write_text("#!/bin/sh\necho 'usage: qb-seats [N]' >&2\nexit 2\n")
    stale.chmod(0o755)
    return stale


def test_a_dash_that_cannot_be_split_off_is_not_fatal(screen):
    """The tolerated-failure branch, which shipped with no test at all.

    `split-window -l 240` in a 240-column window is refused — "no space for new
    pane", measured — and that split runs inside a command substitution under
    `set -e`, by which point the session, the seats and the tape all exist. Dying
    there leaves a half-built screen with no agent started and a session name that
    makes the obvious rerun fail too, so it warns instead.

    What this pins is that EVERYTHING conditional on the dash is skipped with it and
    not half-applied: a regression that left DASH_CMD set would send the dash's
    command at an empty pane target, label the tape 'tape' with nothing to tell it
    apart from, and install a resize hook for a pane that does not exist.
    """
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen.env["QB_SEATS_DASH_SIZE"] = "240"          # the whole window, so no room
    done = screen("-n", "2")
    assert done.returncode == 0, done
    assert "no dash" in done.stderr, done.stderr
    assert pane_id(screen, "dash") is None, "a dash pane was built after all"
    # The tape is the only auxiliary pane, and stays unlabelled — there is nothing
    # to tell it apart from.
    assert [label for label, _, _ in aux_panes(screen)] == [""], aux_panes(screen)
    assert resize_hook(screen) == "", "a hook was installed for a pane that is not there"
    # ...and the seats that were already built still started.
    assert len(wait_for_log(screen.log, 2)) == 2


def test_the_resize_hook_names_a_copy_that_understands_the_flag(screen, tmp_path):
    """The rollout case, which PATH-first resolution got silently wrong.

    beside_me asks PATH before the script's own directory, so that an installed copy
    beats a checkout — right for the ＋, whose path is recorded on the session and
    has to outlive a worktree. For a hook calling a flag this feature ADDS it was
    wrong: reproduced on the host this was written on, where running the working
    tree's qb-seats installed a hook pointing at the v2.47 build, which exits 2 into
    a `run-shell -b` that discards both streams. The hook fired on every resize and
    said nothing, so the dash bug it exists to fix was fixed on no box whose
    installed copy lagged the flag.

    The fixture's own PATH is exactly what hid this — it puts the scripts under test
    first — so this test has to put an older one ahead of them again.
    """
    stale = stale_qb_seats(tmp_path)
    screen.env["PATH"] = f"{stale.parent}:{screen.env['PATH']}"
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen.env["QB_SEATS_DASH_SIZE"] = "60"
    assert screen("-n", "2").returncode == 0
    chose = hook_binary(screen)
    assert chose, f"no resize hook at all: {resize_hook(screen)!r}"
    assert str(stale) != chose, "the hook calls a qb-seats with no --dash-fit"
    assert Path(chose).resolve() == QB_SEATS.resolve(), chose
    # The property, not the path: whatever it chose answers the flag it is called
    # with. `-s` names a session that does not exist, which is what a hook left
    # behind by a killed screen looks like and is not an error.
    probe = subprocess.run([chose, "--dash-fit", "-s", "no-such-screen"],
                           env=screen.env, capture_output=True, text=True, timeout=30)
    assert probe.returncode == 0, probe
    # And end to end, which is the whole point of the hook: the dash still puts
    # itself back when a client attaches, with the stale copy first on PATH.
    with attached_client(screen, 200, 50):
        assert wait_for_dash_width(screen, 60) == 60


def test_no_resize_hook_when_no_copy_understands_the_flag(screen, tmp_path):
    """A screen that does not re-fit is honest; a hook erroring invisibly on every
    resize is not. So when nothing it can reach answers --dash-fit, qb-seats installs
    NOTHING and says so on stderr, where a version skew can still be acted on.

    Both candidates have to fail to get here, and the second one is this very file —
    which understands the flag by construction. The reachable shape of "and yet it
    does not" is a path that cannot be executed: `bash qb-seats` on a checkout
    mounted noexec, a stale symlink farm, a copy whose mode was lost in transit. So
    the script is started as one of those, with the mid-rollout copy on PATH as the
    other candidate.
    """
    stale = stale_qb_seats(tmp_path)
    screen.env["PATH"] = f"{stale.parent}:{screen.env['PATH']}"
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen.env["QB_SEATS_DASH_SIZE"] = "60"
    unrunnable = tmp_path / "noexec"
    unrunnable.mkdir()
    copy = unrunnable / "qb-seats"
    copy.write_text(QB_SEATS.read_text())
    copy.chmod(0o644)
    done = screen("-n", "1", exe=["bash", str(copy)])
    assert done.returncode == 0, done
    assert "no resize hook" in done.stderr, done.stderr
    assert resize_hook(screen) == "", "an inert hook was installed anyway"
    # The screen is otherwise entirely built — this costs the re-fit and nothing else.
    assert pane_id(screen, "dash"), "the dash went missing with its hook"
    assert labels(screen)["dash"][0] == 60, "the width it was built asking for"


def test_the_resize_hook_survives_a_session_name_that_needs_quoting(screen):
    r"""The session name crosses two quoting layers on its way into the hook, and
    interpolating it raw broke both. Measured on tmux 3.6a: an apostrophe left the
    shell an unterminated quote, and a `"` escaped out of tmux's own double quotes
    and truncated the command mid-word. Both are a hook that does nothing, for ever,
    in silence — `run-shell -b` discards both streams — so this asserts the end that
    matters, the dash putting itself back, rather than the string tmux stored.

    WHICH CHARACTERS A SESSION NAME MAY HAVE IS TMUX'S BUSINESS, NOT OURS, and the
    two questions have to be kept apart. Measured on 3.6a and on 3.4 (what
    ubuntu-24.04, and therefore CI, ships), asking for a session called X and then
    looking it up as `=X`:

      * `'`, `"`, a space, `;`, `#`, `!`, `%`, `&`, `*` — kept verbatim by both. Safe
        to ask a test for, which is what this name is made of.
      * `.` and `:` — replaced with `_` by both, because they are a target's own
        separators.
      * `\` — escaped to `\\` by both; `$` — kept by 3.6a and escaped to `\\$` by 3.4,
        since session_check_name runs the name through strvis and the two versions
        do not agree on what needs it.

    The last two groups are names whose lookup no longer matches what was asked for.
    An earlier version of this test put a `$` in the name; it passed here and failed
    on CI for that reason alone, which is a fact about tmux and not about the quoting
    this test is for. `$` is covered by
    test_the_resize_hook_survives_a_dollar_in_the_path_to_itself instead, where the
    character is in something we own, and what tmux does with a name it will not take
    is covered by test_a_session_name_tmux_will_not_take_is_still_a_working_screen.
    """
    name = "it's a \"screen\""
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen.env["QB_SEATS_DASH_SIZE"] = "60"
    assert screen("-n", "2", name=name).returncode == 0
    assert labels(screen, name)["dash"][0] == 60
    with attached_client(screen, 200, 50, name=name):
        assert wait_for_dash_width(screen, 60, name=name) == 60


def test_the_resize_hook_survives_a_dollar_in_the_path_to_itself(screen, tmp_path):
    """`$` is the character tmux expands inside its OWN double quotes, and the hook is
    stored as a tmux command string. Unescaped, a `qb-seats` living under a directory
    called `a$Bdir` went into the hook as `'…/a/qb-seats'` — tmux substituted its
    idea of `$Bdir`, which is nothing — so the hook pointed at a path that does not
    exist and failed on every resize into a `run-shell -b` that discards both streams.

    The `$` is in the path to the script rather than in the session name on purpose:
    a name is the obvious place to put one and cannot be asserted portably, because
    tmux 3.4 escapes `$` in a session name and 3.6a does not (see the test above). A
    directory is ours, reaches the hook through exactly the same tmux double quotes,
    and behaves identically on both versions.
    """
    dollar = tmp_path / "a$Bdir"
    dollar.mkdir()
    # qb-seat-click too: with BIN off PATH, beside_me falls back to the script's own
    # directory for the seat bar as well, and a missing one there would fail the run
    # for an unrelated reason.
    for name in ("qb-seats", "qb-seat-click"):
        copy = dollar / name
        copy.write_text((BIN / name).read_text())
        copy.chmod(0o755)
    # BIN has to come OFF this PATH, or beside_me finds the ordinary copy there —
    # PATH first, deliberately — and the hook never sees the awkward directory.
    screen.env["PATH"] = path_with_no_dash_on_it(tmp_path, screen)
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen.env["QB_SEATS_DASH_SIZE"] = "60"
    assert screen("-n", "2", exe=[str(dollar / "qb-seats")]).returncode == 0
    assert hook_binary(screen) == str(dollar / "qb-seats"), resize_hook(screen)
    with attached_client(screen, 200, 50):
        assert wait_for_dash_width(screen, 60) == 60


def test_a_session_name_tmux_will_not_take_is_still_a_working_screen(screen):
    """tmux renames a session whose name it will not take verbatim, and every `-t` in
    this script addresses the session EXACTLY (`=$SESSION`) — so the rename turned the
    next command into a fatal "no such session", with the session, its window and one
    pane already created: a half-built screen with no seat numbers, no dash, no bar,
    no hook and no agent started, reported as a bare tmux error naming neither the
    cause nor the fix. Measured on 3.6a against `qb-seats -s my.screen`, which tmux
    calls `my_screen`.

    `.` and `:` are a target's own separators and every tmux version replaces them, so
    this was reachable on this host all along. It was found while diagnosing the
    tmux-3.4-only `$` escaping that CI caught — the same class, one step along — which
    is why it is tested here.

    The fix is to ask tmux what it called the session (`new-session -P -F`) rather
    than to reimplement its naming rules, which 3.4 and 3.6a do not even share.
    """
    asked = "my.screen"
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen.env["QB_SEATS_DASH_SIZE"] = "60"
    done = screen("-n", "2", name=asked)
    assert done.returncode == 0, done
    # ASKED OF TMUX, not predicted. 3.6a rewrites `.` to `_`; the flake's pinned
    # 3.7b takes the name verbatim, so there is nothing to warn about — and which
    # of them is on PATH is not something this suite should decide (#259). What
    # the script guarantees in BOTH is the property worth pinning: the name it
    # reports back is the name the session really has. Hardcoding `my_screen`
    # pinned 3.6a's naming rule instead, and went red under the flake while
    # passing on every developer's box.
    names = [n for _, n in listing(screen)]
    assert len(names) == 1, f"expected exactly one screen, got {names}"
    real = names[0]
    if real != asked:
        # Where tmux DID rename, the warning at build time is the only place the
        # usable name is ever said, so it still has to be said.
        assert real in done.stderr, (
            f"it must say what the screen is really called: {done.stderr}")
    # A whole screen, not the first pane of one.
    assert sorted(n for _, n in panes(screen, real) if n) == ["1", "2"]
    assert labels(screen, real)["dash"][0] == 60
    # And the hook names the session that EXISTS. Built from the name that was asked
    # for, it would look up a session that is not there and quietly do nothing.
    assert f"'{real}'" in resize_hook(screen, real), resize_hook(screen, real)
    # The warning promises this name works for --kill; hold it to that. It is also how
    # this test tidies up, since the fixture only knows the name it asked for.
    assert screen("--kill", name=real).returncode == 0


def test_dash_fit_tolerates_a_screen_that_has_moved_on(screen):
    """--dash-fit is reached from the resize hook through `run-shell -b`, so it runs
    a moment AFTER the resize that asked for it — by which time the dash pane, or
    the whole screen, can be gone. That is the ordinary way a screen ends and not an
    error, and every tmux read in dash_resize is tolerated for that reason: measured
    with the session killed, tmux exits 1 with an empty stdout, which took the script
    down through pipefail on `p=$(dash_pane)` and made the clamp's
    `$(( $(tmux display-message …) / DASH_SHARE ))` the arithmetic syntax error
    `$(( / 3 ))` — fatal under `set -e` however tolerant the code around it is.

    What this pins is every shape of that reachable without a race. The one that is
    NOT is the window-width read failing between the two commands around it, which
    is why it is guarded rather than tested.
    """
    screen.env["QB_SEATS_DASH"] = DASH_STUB
    screen.env["QB_SEATS_DASH_SIZE"] = "50"
    screen("-n", "2")
    screen.tmux("kill-pane", "-t", pane_id(screen, "dash"))
    assert screen("--dash-fit").returncode == 0, "the dash pane closing is not an error"
    # A window that is no longer called `seats` — renamed by hand, or by an agent in
    # a pane. `list-panes` then FAILS rather than printing nothing, and that is the
    # half that took the whole script down: the failure travels out of the pipeline
    # through `pipefail` into `p=$(dash_pane)`, and `set -e` does the rest.
    screen.tmux("rename-window", "-t", "t:seats", "elsewhere")
    assert screen("--dash-fit").returncode == 0, "a renamed window is not an error"
    # ...and a screen that is not there at all. The server stays up for session `t`,
    # so this is the hook's own shape and not a dead socket.
    assert screen("--dash-fit", name="no-such-screen").returncode == 0


# The two tests that were here pinned QB_SEAT_BRIEF's forwarding — that an empty
# value arrived as itself on the build path and on --add, because a `-n` predicate
# dropped it and the seat then started on the full built-in brief. There is no
# brief and nothing is forwarded through the session environment any more (#540);
# what replaced both is `test_an_empty_initial_command_leaves_a_bare_shell` and
# `test_a_screen_of_bare_shells_stays_that_way_when_a_seat_is_added`, which make
# the same distinction — set-and-empty is an answer — against the mechanism that
# carries it now.


# ---- list and resume --------------------------------------------------------
# The ssh link drops and the shell that comes back up is in the wrong directory,
# or on the wrong repo, or does not remember what the screen was called. `qb-seats`
# on its own only reattaches to the screen for the repo you are STANDING IN, which
# is the one thing a recovering shell cannot be relied on to be.


def qb(run, *args, cwd=None):
    """qb-seats with NO -C and NO -s, from a directory of the caller's choosing.

    The fixture's own runner always supplies both, which is exactly what `list`
    and `resume` must work without — so these tests go around it.
    """
    return subprocess.run(
        [str(QB_SEATS), *args], cwd=cwd, env=run.env,
        capture_output=True, text=True, timeout=60,
    )


def listing(run, cwd=None):
    """[(number, name)] as `list` printed it."""
    out = qb(run, "list", cwd=cwd)
    assert out.returncode == 0, out.stderr
    rows = []
    for line in out.stdout.splitlines():
        num, name = line.split()[0], line.split()[1]
        rows.append((num, name))
    return rows


def test_list_names_every_screen_that_is_up(screen):
    screen("-n", "2", name="t")
    screen("-n", "1", name="t2")
    rows = listing(screen)
    assert [n for _, n in rows] == ["t", "t2"], rows
    assert [i for i, _ in rows] == ["1", "2"], "the rows are numbered for `resume`"


def test_list_says_the_seats_and_the_repo(screen):
    """The two columns that tell two screens apart when the names do not."""
    screen("-n", "2")
    line = qb(screen, "list").stdout
    assert "2 seats" in line, line
    assert str(screen.repo) in line or f"~{str(screen.repo)}" in line, line


def test_list_finds_a_screen_whatever_it_is_called(screen):
    """A screen is identified by a pane carrying @qb_seat, never by its name.

    `-s` takes anything and the fleet's own screen is `qbseats`, not
    `seats-nix-fleet`; a listing keyed on a `seats-` prefix would simply not show
    it, which is worse than not having the command.
    """
    screen("-n", "1", name="nothing-like-the-default")
    assert [n for _, n in listing(screen)] == ["nothing-like-the-default"]


def test_list_ignores_a_tmux_session_that_is_not_a_screen(screen):
    """The user's editor session on the same server is none of this command's
    business, and offering it under a number that `resume` would attach to is the
    failure that matters."""
    screen("-n", "1")
    screen.tmux("new-session", "-d", "-s", "not-ours")
    try:
        assert [n for _, n in listing(screen)] == ["t"]
    finally:
        screen.tmux("kill-session", "-t", "=not-ours")


def test_list_with_nothing_up_prints_nothing_and_says_so(screen):
    """Empty stdout for a caller parsing it, a word on stderr for the human who
    would otherwise be staring at silence."""
    screen.tmux("start-server")
    out = qb(screen, "list")
    assert out.returncode == 0, out.stderr
    assert out.stdout == "", out.stdout
    assert "no screens" in out.stderr, out.stderr


def test_the_list_says_how_to_open_one(screen):
    """A list of names is only half an answer. `resume` is not guessable from
    `--add` and `--kill`, so the list has to carry the next command — and a WORKED
    one, since the difficulty it exists to solve is not knowing what your screens
    are called."""
    screen("-n", "1", name="t")
    screen("-n", "1", name="t2")
    out = qb(screen, "list")
    assert "resume 1" in out.stderr, out.stderr
    assert "resume t)" in out.stderr, "and by name, the spelling that survives"
    # STDOUT STAYS ROWS AND NOTHING ELSE, or every caller that pipes this breaks.
    # `listing()` in this file is one of them: it would read the hint as a screen.
    assert len(out.stdout.splitlines()) == 2, out.stdout


def test_one_screen_needs_no_number_and_the_hint_says_so(screen):
    """`resume` takes no argument when there is nothing to choose between, and a
    hint that printed `resume 1` anyway would teach the longer of the two."""
    screen("-n", "1")
    out = qb(screen, "list")
    assert "open it:" in out.stderr, out.stderr
    assert out.stderr.rstrip().endswith("resume"), out.stderr


def test_resume_takes_the_number_from_the_list(screen):
    screen("-n", "1", name="t")
    screen("-n", "1", name="t2")
    want = dict((i, n) for i, n in listing(screen))["2"]
    out = qb(screen, "resume", "2")
    assert out.returncode == 0, out.stderr
    # No tty here, so attach() reports rather than attaching — which is the
    # assertion: it named the screen the list numbered 2.
    assert want in out.stdout, out.stdout


def test_resume_takes_the_name(screen):
    screen("-n", "1", name="t")
    screen("-n", "1", name="t2")
    out = qb(screen, "resume", "t2")
    assert out.returncode == 0, out.stderr
    assert "t2" in out.stdout, out.stdout


def test_a_number_after_resume_is_not_the_seat_count(screen):
    """The parser trap this subcommand had to be written around: a bare number is
    the seat count everywhere else on this command line, so `resume 2` read by the
    ordinary rules is "resume, and by the way three seats" with the 2 swallowed —
    silently resuming the wrong screen, or the only one.
    """
    screen("-n", "1", name="t")
    screen("-n", "1", name="t2")
    before = {n for _, n in listing(screen)}
    assert qb(screen, "resume", "2").returncode == 0
    assert {n for _, n in listing(screen)} == before, "resume must build nothing"
    assert sorted(n for _, n in panes(screen, "t2") if n) == ["1"], "and add no seat"


def test_resume_needs_no_repo_and_no_C(screen, tmp_path):
    """The whole point. A screen already knows the directory it was built in, so
    recovery must not depend on the shell that came back up being anywhere near
    it — which is the one thing `qb-seats` on its own does depend on.
    """
    screen("-n", "1")
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    out = qb(screen, "resume", "t", cwd=elsewhere)
    assert out.returncode == 0, out.stderr
    assert "t" in out.stdout, out.stdout


def test_resume_with_no_argument_takes_the_only_screen(screen):
    """A list of one is not a choice to make."""
    screen("-n", "1")
    out = qb(screen, "resume")
    assert out.returncode == 0, out.stderr
    assert "t is up" in out.stdout, out.stdout


def test_resume_with_no_argument_will_not_guess_between_two(screen):
    screen("-n", "1", name="t")
    screen("-n", "1", name="t2")
    out = qb(screen, "resume")
    assert out.returncode == 1
    assert "say which" in out.stderr, out.stderr
    # ...and the list is right there, so the next command is one word away.
    assert "t2" in out.stderr, out.stderr


def test_resume_of_a_screen_that_is_not_there_shows_what_is(screen):
    screen("-n", "1")
    out = qb(screen, "resume", "no-such-screen")
    assert out.returncode == 1
    assert "no-such-screen" in out.stderr, out.stderr
    assert "  1   t " in out.stderr or "\n  1 " in out.stderr, out.stderr


def test_resume_with_nothing_up_says_how_to_start_one(screen):
    screen.tmux("start-server")
    out = qb(screen, "resume", "1")
    assert out.returncode == 1
    assert "no screens are up" in out.stderr, out.stderr


def test_a_name_beats_a_number(screen):
    """Both spellings are offered, so a screen that happens to be CALLED `1` while
    sitting at some other row has to resume when you type its name — the name is
    the half the user can see is unambiguous."""
    screen("-n", "1", name="t")
    screen("-n", "1", name="1")      # sorts first, so row 1 is the screen named `1`
    rows = dict((n, i) for i, n in listing(screen))
    assert rows["1"] == "1" and rows["t"] == "2", rows
    assert "1 is up" in qb(screen, "resume", "1").stdout


def test_list_and_resume_reach_a_screen_tmux_renamed(screen):
    """`-s my.screen` becomes `my_screen`, and every `-t` in this script addresses
    the session exactly — so the name you asked for is not a name that reattaches,
    and the warning at build time is the only place it was ever said. A list read
    from tmux is the durable answer: it can only print names that exist.
    """
    asked = "my.screen"
    assert screen("-n", "1", name=asked).returncode == 0
    real = asked
    try:
        # Whatever tmux called it — 3.6a `my_screen`, 3.7b `my.screen` (#259) —
        # the list can only print a name that EXISTS, and that is the name that
        # has to reattach. Asserting the 3.6a spelling asserted tmux's rules
        # rather than this script's promise.
        names = [n for _, n in listing(screen)]
        assert len(names) == 1, f"expected exactly one screen, got {names}"
        real = names[0]
        assert f"{real} is up" in qb(screen, "resume", real).stdout
        assert f"{real} is up" in qb(screen, "resume", "1").stdout
    finally:
        screen("--kill", name=real)


# ---- what a screen of N seats is about to spend (#275) ------------------------
#
# Bringing up N agents on one shared subscription is the largest single spending
# decision the fleet makes, and it is the moment nobody is looking at the dash
# the caps are drawn on. So the screen says it here, where the terminal survives
# — a seat pane execs its agent moments later and the agent paints over anything
# printed before it.


def _pace_stub(screen, body: str):
    """A stub `qb-pace` ahead of the real one, and the note turned back on.

    /bin/sh, like every other stub here: there is no /usr/bin/env in the nix build
    sandbox and a stub that cannot exec fails the test for a reason that has
    nothing to do with the code under test.
    """
    stub = screen.stub_dir / "qb-pace"
    stub.write_text("#!/bin/sh\n" + body)
    stub.chmod(0o755)
    screen.env["QB_SEATS_PACE"] = "on"
    # A LOG FILE, not stderr: the caller reads this command's stdout and discards
    # its stderr, so a stub that recorded its argv on the second stream would be
    # recording it into the same /dev/null the real one's failures go to.
    return screen.log.parent / "pace.log"


def test_a_new_screen_says_what_its_seats_are_about_to_spend(screen):
    """RED/GREEN. The prediction, not the percentage: what these N seats cost
    against what is left. It is asked for N — the seat count the screen is actually
    building — because "5h at 91%" is the thing the dash already showed."""
    log = _pace_stub(screen,
                     f'echo "asked $*" >> {screen.log.parent / "pace.log"}\n'
                     'echo "pace: SLOW — 5h at 74%; resets in 47m"\n'
                     'echo "estimate  3 seats x 1 round ~ 851,385 tokens"\n')
    result = screen("-n", "3")
    assert result.returncode == 0
    # The ESTIMATE line, not the whole log. The top line asks the same binary a
    # different question a moment later — `qb-pace` with no arguments, for the
    # verdict it puts on the right of the screen — and both reads hit qb-pace's
    # own three-minute cache. What this test is about is which N the note asked
    # for.
    asked = [ln for ln in log.read_text().splitlines() if "--estimate" in ln]
    assert asked == ["asked --estimate 3"], \
        f"the note was not asked about THIS screen: {log.read_text()!r}"
    assert "qb-seats: pace: SLOW — 5h at 74%; resets in 47m" in result.stderr
    assert "qb-seats: estimate  3 seats x 1 round ~ 851,385 tokens" in result.stderr


def test_the_screen_is_built_whatever_the_window_says(screen):
    """It warns and proceeds under the default mode. A human bringing up a screen
    has decided to spend; withholding the agents is `QB_SEATS_PACE=obey`, which is
    opt-in and tested above."""
    # Exiting non-zero as well, because that is the shape of the case worth
    # pinning: a verdict that says stop must still be RELAYED by the thing that
    # has decided not to stop.
    _pace_stub(screen, 'echo "pace: HOLD — 5h at 99%; resets in 12m"\nexit 3\n')
    result = screen("-n", "2")
    assert result.returncode == 0
    assert "qb-seats: pace: HOLD — 5h at 99%; resets in 12m" in result.stderr
    assert len([p for p in panes(screen) if p[1]]) == 2, "the screen was not built"


def test_obey_brings_the_seats_up_as_shells_rather_than_refusing_the_screen(screen):
    """What a spent window costs: the agents, not the panes.

    The refusal this replaces lived in the per-pane wrapper and refused to create
    the PANE at all (exit 4). A pane costs nothing, and refusing somebody a
    terminal because a subscription window is spent is a refusal they can only
    work around by not using this script — so `obey` withholds the thing that
    actually spends the window and leaves a shell you can type into (#540).
    """
    _pace_stub(screen, "echo 'pace: HOLD — 5h spent; resets in 12m'\nexit 3\n")
    screen.env["QB_SEATS_PACE"] = "obey"
    typed = typing_shell(screen)
    done = screen("-n", "2")
    assert done.returncode == 0, done.stderr
    assert len([p for p in panes(screen) if p[1]]) == 2, "the panes are still built"
    # Phrase-wise and not as one string: `warn` prints this refusal as the
    # multi-line block it is written as, so the sentence carries the wrap.
    assert "these seats come up as" in done.stderr, done.stderr
    assert "bare shells" in done.stderr, done.stderr
    assert "resets in 12m" in done.stderr, "and it says when it comes back"
    time.sleep(1.0)
    assert not typed.exists() or typed.read_text() == "", typed.read_text()


def test_warn_says_the_window_is_spent_and_starts_the_seats_anyway(screen):
    """The default, and the direction this deliberately points: a human standing in
    front of a screen, told the window is spent, is a human who can decide."""
    _pace_stub(screen, "echo 'pace: HOLD — 5h spent'\nexit 3\n")
    screen.env["QB_SEATS_PACE"] = "warn"
    screen("-n", "1")
    assert wait_for_log(screen.log, 1), "the seat started"


def test_a_screen_of_bare_shells_does_not_consult_the_pace_at_all(screen):
    """With no initial command there is no agent to withhold, so the gate has
    nothing to act on — and a suite that left the mode at its default would
    otherwise read the developer's own subscription to decide nothing."""
    log = _pace_stub(screen, f'echo "gate $*" >> {screen.log.parent / "pace.log"}\nexit 3\n')
    screen.env["QB_SEATS_PACE"] = "obey"
    screen("-n", "1", "--cmd", "")
    assert "--gate" not in (log.read_text() if log.exists() else "")


def test_obey_withholds_a_seat_added_to_a_spent_screen_too(screen):
    """--add is the other way a seat starts, and the window does not care which."""
    screen("-n", "1")
    wait_for_log(screen.log, 1)
    _pace_stub(screen, "echo 'pace: HOLD'\nexit 3\n")
    screen.env["QB_SEATS_PACE"] = "obey"
    done = screen("--add")
    assert done.returncode == 0, done.stderr
    assert len([p for p in panes(screen) if p[1]]) == 2, "the pane is still added"
    time.sleep(1.0)
    assert len(wait_for_log(screen.log, 2, timeout=1)) == 1, "and no second agent"


def test_a_pace_that_cannot_answer_does_not_withhold_anything(screen):
    """3 is `hold` and only 3. A timeout, a broken install or an `unknown` all mean
    the gate did not RUN rather than that it passed — and none of them is a reason
    to leave a screen somebody asked for standing empty."""
    _pace_stub(screen, "exit 127\n")
    screen.env["QB_SEATS_PACE"] = "obey"
    screen("-n", "1")
    assert wait_for_log(screen.log, 1), "the seat started"


def test_a_pace_mode_that_is_not_a_mode_is_named_and_read_as_warn(screen):
    """A typo in the one variable that decides whether a screen starts anything.
    Read as the strict answer it would be a screen that mysteriously starts
    nothing; read silently as warn, a `QB_SEATS_PACE=obay` never obeys."""
    _pace_stub(screen, "echo 'pace: HOLD'\nexit 3\n")
    screen.env["QB_SEATS_PACE"] = "obay"
    done = screen("-n", "1")
    assert "not off, warn or obey" in done.stderr, done.stderr
    # ONCE, for one typo. Three callers ask what the mode is — the estimate, the
    # gate, and the top line's flag — and a complaint repeated per caller is how a
    # message worth reading becomes one to skip. It is also why the answer is a
    # global rather than something a `$(…)` subshell computes and throws away.
    assert done.stderr.count("not off, warn or obey") == 1, done.stderr
    assert wait_for_log(screen.log, 1), "and it starts, because warn starts"


def test_a_broken_pace_command_costs_the_note_and_not_the_screen(screen):
    """This runs under `set -e` before a single pane exists. A note that could take
    the build down with it would be a budget warning that causes outages."""
    _pace_stub(screen, "exit 127\n")
    assert screen("-n", "1").returncode == 0
    assert len([p for p in panes(screen) if p[1]]) == 1
