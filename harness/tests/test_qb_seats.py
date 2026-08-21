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

import contextlib
import fcntl
import os
import pty
import re
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
    #
    # The brief is recorded with `${VAR+set}`/`${VAR-unset}`, the same distinction
    # qb-seat itself draws: set-and-empty means "no brief, wait" and unset means
    # "use the built-in one", and a test that cannot tell them apart cannot pin the
    # bug that forwarded the wrong one. It has to be logged rather than read back
    # off tmux, because --add passes it with `split-window -e`, which sets the
    # environment of the PANE — and there is no tmux command that shows that.
    stub.write_text(
        "#!/bin/sh\n"
        'printf "seat=%s instance=%s cwd=%s brief=%s\\n" "$1"'
        ' "${QUARTERBACK_INSTANCE:-unset}" "$PWD" "${QB_SEAT_BRIEF+set:}${QB_SEAT_BRIEF-unset}"'
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
        # decides their result: `QB_SEAT_BRIEF=` exported fleet-wide — which is the
        # documented way to ask for waiting seats — is forwarded correctly by the
        # code under test and makes `test_an_empty_brief_reaches_a_seat_added_later`
        # fail on its FIRST assertion, the one that wants a plain screen to leave the
        # knob unset. Green on CI, red on the box that configured it, and the diff
        # under test innocent in both.
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
    }
    sessions = []

    def _run(*args, name="t", exe=None):
        """`exe` starts the script as something else — a command list, in place of
        the plain path to the script under test.

        Only the resize-hook tests need it, and they need it for a reason no other
        test has: what qb-seats writes into a hook depends on the PATH IT WAS
        STARTED AS, since a hook that calls back into this script has to decide
        which copy of itself it means. A test about that decision cannot be run
        through one fixed entry point.
        """
        sessions.append(name)
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
    _run.stub_dir = stub_dir       # the stub qb-seat, for tests that rebuild PATH
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
        yield client
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
    # The stub qb-seat still has to be findable, or require_qb_seat falls back to
    # the real one beside the script under test and starts a real agent.
    return f"{run.stub_dir}:{d}"


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


def test_the_scope_reaches_the_panes(screen):
    """The knob for the one case the per-project default cannot read: two screens
    on ONE repository, whose seats would otherwise both be numbered from 1 in the
    same namespace and collide exactly as two projects used to (#208)."""
    screen.env["QB_SEAT_SCOPE"] = "review"
    screen("-n", "1")
    assert "QB_SEAT_SCOPE=review" in screen.tmux("show-environment", "-t", "t").stdout


def test_an_empty_scope_reaches_the_panes(screen):
    """Set-and-empty is the request for the machine-wide seat numbering this had
    before #208, and it is the one value a `-n` forwarding predicate drops — which
    would hand the screen the per-project default it was told not to use.

    Line-wise, not as a substring, for QB_SEAT_BRIEF's reason: `"QB_SEAT_SCOPE=" in
    env` also matches `QB_SEAT_SCOPE=review`.
    """
    screen.env["QB_SEAT_SCOPE"] = ""
    screen("-n", "1")
    env = screen.tmux("show-environment", "-t", "t").stdout.splitlines()
    assert "QB_SEAT_SCOPE=" in env, f"set-and-empty must arrive as itself: {env}"
    assert "-QB_SEAT_SCOPE" not in env, "must not be marked for removal"


def test_a_plain_screen_forwards_no_scope(screen):
    """The default lives in qb-seat and is derived from the pane's own cwd, which
    the layout already sets to the repo. Writing it out here as well would be a
    second place for it to live and to drift from."""
    screen("-n", "1")
    assert "QB_SEAT_SCOPE" not in screen.tmux("show-environment", "-t", "t").stdout
    assert screen.tmux("show-options", "-v", "-t", "t:", "@qb_scope").returncode != 0


def test_the_scope_is_recorded_on_the_screen_as_well_as_forwarded(screen):
    """Two readers, two mechanisms. The variable tells the SEAT what to call
    itself; the option tells whatever reads the screen from outside — the
    dashboard joins `zeus/seat-review-1` to a pane, and @qb_repo answers for the
    default scope only."""
    screen.env["QB_SEAT_SCOPE"] = "review"
    screen("-n", "1")
    assert screen.tmux("show-options", "-v", "-t", "t:", "@qb_scope").stdout.strip() \
        == "review"


def test_a_seat_added_later_carries_the_scope_it_was_added_with(screen):
    """--add scopes its -e list to the one pane it creates rather than rewriting
    the session under seats already working in it, and the option goes the same
    way for the same reason."""
    screen("-n", "1")
    screen.env["QB_SEAT_SCOPE"] = "review"
    screen("--add")
    panes = dict(line.split("\t", 1) for line in
                 screen.tmux("list-panes", "-t", "t:", "-F",
                             "#{@qb_seat}\t#{@qb_scope}").stdout.splitlines())
    assert panes.get("2") == "review", panes
    # And NOT on the seat that was already working, whose own scope is the default.
    assert panes.get("1") == "", panes


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


def test_every_session_target_is_an_id_and_not_a_name():
    """A `-t` may not address a session by NAME, whatever the name looks like.

    `.` and `:` are a target's own separators. tmux used to rewrite them out of a
    session name it would not take, so `-t "=$SESSION"` worked by accident; 3.7b
    keeps `my.screen` verbatim and the same target then parses as pane `screen` of
    session `my` — "can't find pane: screen" — against a screen that plainly
    exists. Every seat command failed, `list` showed nothing and `resume` could not
    reach it (#259).

    Asserted on the SOURCE rather than by driving tmux, because the bug is
    invisible on the tmux a developer has: 3.6a renames the dot away, so no test
    using a session name can exercise it there. This holds under either version,
    which is the point — the next `-t "=$SESSION"` someone adds fails here and now
    rather than on whichever box happens to carry the newer tmux.
    """
    src = (Path(__file__).resolve().parents[1] / "bin" / "qb-seats").read_text()
    offenders = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(src.splitlines(), 1)
        # `=$s`/`=$SESSION` and friends: an `=`-anchored target built from a shell
        # variable holding a name. `session_id`'s own list-sessions lookup is not
        # a `-t` and does not match.
        if re.search(r'-t\s+"=\$', line)
    ]
    assert not offenders, (
        "these address a tmux session by name, which breaks the moment the name "
        "carries a `.` or a `:` — use the session id (SESSION_ID, or the id read "
        "beside the name in screens()):\n  " + "\n  ".join(offenders))


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


# ---- the dash pane -----------------------------------------------------------
#
# The tape says what just happened, the dash says what is true now. Both belong on
# the screen, which is why the dash is a second pane rather than a value of
# QB_SEATS_BOARD. It shipped first as dev/seats-extras.sh — scaffolding that
# hardcoded two of one developer's worktrees — and these are the assertions that
# let it move into the script proper (#174).

DASH_STUB = "printf dash-stub; sleep 300"


def dash_stubs(tmp_path, *names):
    """A bin directory holding a stub for each name, each printing its own marker.

    Every other dash test pins QB_SEATS_DASH to a command, which says nothing about
    how an UNSET one resolves — the PATH probe, its preference order, and the
    placeholder when neither binary is there had no coverage at all. These stubs are
    what let that be asserted without consulting the machine the suite runs on.
    """
    d = tmp_path / "dashbin"
    d.mkdir(parents=True, exist_ok=True)
    for name in names:
        stub = d / name
        stub.write_text(f"#!/bin/sh\nprintf {name}-ran\nexec sleep 300\n")
        stub.chmod(0o755)
    return d


def test_a_dash_pane_is_built_by_default(screen, tmp_path):
    """Default-ON is a deliberate divergence from #174, which asked for a flag.
    A screen whose whole job is situational awareness should not need one.

    Unset, not empty: this is the resolution path, so QB_SEATS_DASH is removed from
    the environment rather than set to a stub.
    """
    del screen.env["QB_SEATS_DASH"]
    screen.env["PATH"] = f"{dash_stubs(tmp_path, 'qb-dash')}:{screen.env['PATH']}"
    screen("-n", "2")
    dash = pane_id(screen, "dash")
    assert dash, "no pane labelled 'dash'"
    assert "qb-dash-ran" in wait_for_pane(screen, dash, "qb-dash-ran")


def test_the_default_dash_is_the_plain_one_never_the_tui(screen, tmp_path):
    """qb-dash-tui is the nicer renderer and cannot be the default: it keys its seat
    rows by seat name, every screen numbers its seats from 1, so the second screen
    anywhere on the box turns this pane into a Textual traceback (#209, underlying
    cause #208). A default has to work on the second screen as well as the first.

    Both installed, so this pins the preference order rather than availability —
    and the fallback that used to reach the TUI whenever qb-dash was missing is
    gone with it, since a partial install is no reason to hand somebody the
    renderer that crashes.
    """
    del screen.env["QB_SEATS_DASH"]
    bindir = dash_stubs(tmp_path, "qb-dash", "qb-dash-tui")
    screen.env["PATH"] = f"{bindir}:{screen.env['PATH']}"
    screen("-n", "1")
    seen = wait_for_pane(screen, pane_id(screen, "dash"), "qb-dash-ran")
    assert "qb-dash-ran" in seen
    assert "qb-dash-tui-ran" not in seen, "the TUI became the default"

    # ...and with only the TUI installed the default is still not the TUI: the pane
    # says what to set instead, which is the one thing that cannot crash.
    only_tui = dash_stubs(tmp_path / "tui-only", "qb-dash-tui")
    screen.env["PATH"] = f"{only_tui}:{path_with_no_dash_on_it(tmp_path, screen)}"
    assert screen("-n", "1", name="tuionly").returncode == 0
    seen = wait_for_pane(screen, pane_id(screen, "dash", "tuionly"),
                         "QB_SEATS_DASH=qb-dash-tui")
    assert "QB_SEATS_DASH=qb-dash-tui" in seen, seen
    assert "qb-dash-tui-ran" not in seen, "the TUI was started as the default"


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


def test_an_empty_brief_reaches_the_panes(screen):
    """The one value QB_SEAT_BRIEF exists to express, and a `-n` forwarding test
    silently dropped it: nothing arrived in the pane, qb-seat saw unset, and the
    seat started on the full built-in brief — a screen asked for waiting seats
    that went and claimed work instead, reporting nothing wrong.

    Asserted line-wise and not as a substring. `"QB_SEAT_BRIEF=" in env` also
    matches `QB_SEAT_BRIEF=go and claim something`, so a regression that forwarded
    the built-in default instead of the empty string — the precise failure above —
    would have kept it green.
    """
    screen.env["QB_SEAT_BRIEF"] = ""
    screen("-n", "1")
    env = screen.tmux("show-environment", "-t", "t").stdout.splitlines()
    assert "QB_SEAT_BRIEF=" in env, f"set-and-empty must arrive as itself: {env}"
    assert "-QB_SEAT_BRIEF" not in env, "must not be marked for removal"
    # And the seat that received it agrees, which is the half tmux cannot be asked.
    assert wait_for_log(screen.log, 1)[0].endswith("brief=set:")


def test_an_empty_brief_reaches_a_seat_added_later(screen):
    """--add builds its own -e list, and for a while that list was written out by
    hand rather than being the forwarder's: `QB_SEAT_BRIEF= qb-seats --add` onto a
    screen built without the variable forwarded nothing at all, qb-seat saw unset,
    and the added seat went off on the full built-in brief — the same silent failure
    the `up` path had, in the half of the code that had no test.
    """
    screen("-n", "1")
    assert wait_for_log(screen.log, 1)[0].endswith("brief=unset")
    screen.env["QB_SEAT_BRIEF"] = ""
    screen("--add")
    lines = wait_for_log(screen.log, 2)
    assert len(lines) == 2, lines
    assert lines[1].endswith("brief=set:"), lines


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
