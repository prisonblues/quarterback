"""The two derivations that exist twice, pinned against each other (#146).

Claude Code is the one runtime with both a lifecycle hook and an MCP server, so
two processes have to land on one board identity — and one of them is bash. Two
rules are therefore spelled in two languages:

* the **key** the board allocates a name against (the `INSTANCE=` pipeline in
  `harness/bin/qb-hook`, `key_slug` in `mcp/mcp_server/pane.py`);
* the **pane** — a name for the CLI process that a `/clear` does not move — and
  the file under it that carries the current conversation from the half that can
  see it to the half that cannot (`qb_pane_key`/`qb_pane_file`, `pane_key`/
  `pane_file`).

**This file is the reason the mechanism is not inert.** PR #765 shipped the pane
derivation twice and the two disagreed: one stripped whitespace before
substituting and the other after, one treated an empty result as "no pane" and
the other as a pane named nothing, and one worked on bytes where the other worked
on characters. The consequence was not a crash. The hook wrote
`qb-conv-seat-lexray-1-`, the server read `qb-conv-seat-lexray-1`, found nothing,
fell back — and every test was green over a mechanism that did nothing at all.
So the table below is deliberately unkind, and every case in it was chosen
because it is a way the two languages differ rather than a way a session does:
`\\d` in Python matches Arabic-Indic digits and `[0-9]` in bash does not, `$` in
Python matches before a trailing newline and bash's `case` glob does not, and
`tr` counts bytes where `re.sub` counts characters, and `st_mtime` is a float
whose spacing at 1.7e9 is coarse enough to round a socket into the next second.

BOTH HALVES RUN AS SUBPROCESSES OF THIS TEST, which is what lets the ancestry
cases be real: the pytest process is a genuine live ancestor of each, so
"the socket's owner is a live ancestor" can be arranged with `os.getpid()`
rather than with a fake `/proc` that would prove only that two fakes agree.

Run: pytest harness/tests
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_qb_hook_end import Hooked

ROOT = Path(__file__).resolve().parents[2]
QB_ENV = ROOT / "harness" / "bin" / "qb-env"
PANE_PY = ROOT / "mcp" / "mcp_server" / "pane.py"

BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    BASH is None or not Path("/proc/self/stat").exists(),
    reason="one half is bash and both read /proc",
)


def test_both_halves_are_here_to_be_compared():
    """Asserted, never skipped. A parity check that cannot reach one of the two
    implementations compares nothing and reports green, which is the failure this
    whole file exists to stop being possible — so a sandbox that runs this suite
    without `mcp/mcp_server/pane.py` fails on this line rather than on nothing
    (#163). `flake.nix`'s `worktree-tests` installs it for exactly this reason."""
    assert QB_ENV.is_file(), f"{QB_ENV} is not here"
    assert PANE_PY.is_file(), (
        f"{PANE_PY} is not here, so the Python half of the pane derivation cannot be "
        "compared against the bash half. If a sandbox runs this suite, it has to "
        "install that file — see flake.nix's worktree-tests.")

#: Loads `pane.py` BY PATH, never as a package. `mcp/` is its own project with
#: its own virtualenv, and this suite runs from the repo root against the root
#: one — so an import would make the parity check conditional on which venv
#: happened to be active. `pane.py` is stdlib-only precisely so it need not be.
_PY = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("pane", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
out = getattr(mod, sys.argv[2])(*sys.argv[3:])
sys.stdout.write(out or "")
"""


def _run(argv: list[str], env: dict, hops: int) -> str:
    """`argv`, optionally under `hops` extra shells.

    The trailing `; true` is not decoration: bash EXECS the last command of a
    `-c` string instead of forking it, so without something after it the wrapper
    would collapse into the process it was meant to sit above and the extra hop
    this asks for would not exist.
    """
    for _ in range(hops):
        argv = [BASH, "-c", f"{shlex.join(argv)}; true"]
    got = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)
    assert got.returncode == 0, got.stderr
    return got.stdout.rstrip("\n")


def _bash(fn: str, env: dict, *args: str, hops: int = 0) -> str:
    argv = " ".join(shlex.quote(a) for a in args)
    return _run([BASH, "-c", f". {QB_ENV}; {fn} {argv}"], env, hops)


def _python(fn: str, env: dict, *args: str, hops: int = 0) -> str:
    return _run([sys.executable, "-c", _PY, str(PANE_PY), fn, *args], env, hops)


def _env(**over: str) -> dict:
    """A clean environment: nothing about a pane inherited from the developer.

    This suite runs inside a Claude Code session as often as not, and that
    session has a real `CLAUDE_CODE_MESSAGING_SOCKET` naming a real live
    ancestor — so a case that means to say "no socket" has to say it, or it
    quietly tests the developer's own pane.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE_", "QUARTERBACK_", "QB_"))}
    env.pop("XDG_RUNTIME_DIR", None)
    env.update(over)
    return env


# --------------------------------------------------------------- the key slug

#: Values chosen for where the two LANGUAGES differ, not for where a session
#: does. A real key is a UUID prefix or an operator's label; these are the ways
#: `tr`+`sed`+`cut` and a Python regex come apart when handed something else.
KEY_SLUG_TABLE = [
    "4c8f6a8a",                     # the ordinary case: a session-id prefix
    "e267ef87",
    "",                             # nothing in, nothing out
    "seat-quarterback-4",           # an operator's label
    "Rich_Laptop",                  # a good key and a bad name (#156)
    "deploy",
    "abc ",                         # TRAILING space: `tr` makes it `-`, and a
                                    # `.strip()` on one side only made it vanish
    " abc",                         # LEADING space, then stripped as punctuation
    "  ",                           # nothing but whitespace
    "___",                          # sanitises away to nothing on both sides
    "...",
    "---",
    "a/b",                          # a separator that is not in the charset
    "a\nb",                         # a newline inside the value
    "a\tb",
    "é",                            # TWO bytes: one `-` character-wise, two by
                                    # bytes, and the halves would send different
                                    # keys if they did not agree which
    "naïve-agent",
    "日本",                          # three bytes each
    "١٢٣",                          # Arabic-Indic digits: `\\d` matches these
    "x" * 50,                       # past the board's 40
    "é" * 30,                       # past 40 only if you count bytes
    "-" + "a" * 45,
    "~._-abc",                      # every character the left-trim eats
]


def hook_key(hook, label: str) -> str:
    """The key `qb-hook` actually PUTS ON THE WIRE for `QUARTERBACK_INSTANCE=label`.

    Through the header rather than through a shell function, and deliberately.
    The rule lives inline in `qb-hook` because a half-migrated install (#204) can
    pair this hook with a `qb-env` that predates any function it were moved to,
    and a shim would be a third spelling of the very thing this file exists to
    stop there being two of. Asserting on what the board receives needs no
    function to call and is the stronger claim anyway: it cannot pass over a
    branch that never runs.
    """
    hook.fire("SessionEnd", env=hook.env(QUARTERBACK_INSTANCE=label), reason="other")
    calls = hook.to("/session/end")
    assert calls, hook.sent()
    # The stub curl logs its arguments whitespace-joined, and a key can hold no
    # whitespace by the time it is one — `tr` has turned every space into a `-`
    # — so the value is the token after the header name, and its absence means
    # the hook sent no key at all.
    tokens = calls[0].split()
    if "X-Agent-Instance:" not in tokens:
        return ""
    at = tokens.index("X-Agent-Instance:")
    return tokens[at + 1] if at + 1 < len(tokens) else ""


#: The session id `Hooked.fire` sends unless a test says otherwise, and so the
#: value the ladder's second rung slugs.
DEFAULT_SID = "sid-1"


@pytest.mark.parametrize("value", KEY_SLUG_TABLE, ids=repr)
def test_the_two_key_slugs_agree(tmp_path, value):
    """Both rungs of the ladder, because a label can fail to be a key.

    `QUARTERBACK_INSTANCE`, then the conversation, then (on the Python side only,
    which is the half that can run somewhere other than Claude Code) a nonce. A
    label that sanitises away to nothing has to fall through on BOTH sides or the
    hook posts as the bare machine while the server posts as the session — one
    agent with two identities, which is this issue by another road.
    """
    if not value:
        pytest.skip("an UNSET label is not a label; the fallback is its own test")
    hooked = Hooked(tmp_path)
    expected = _python("key_slug", _env(), value)
    if not expected:
        expected = _python("key_slug", _env(), DEFAULT_SID[:8])
    assert hook_key(hooked, value) == expected


def test_the_key_slug_table_is_not_all_empty():
    """The guard #765 needed. A table over which both halves return nothing is
    two implementations agreeing that they do not work, and it passes."""
    env = _env()
    answers = [_python("key_slug", env, v) for v in KEY_SLUG_TABLE]
    assert sum(1 for a in answers if a) >= len(KEY_SLUG_TABLE) // 2


# ------------------------------------------------------------------- the pane


@pytest.fixture
def socket_dir(tmp_path):
    d = tmp_path / "cc-socks"
    d.mkdir()
    return d


@pytest.fixture
def stranger():
    """A live process that is NOT an ancestor of anything this test starts."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


@pytest.fixture
def dead_pid():
    """A pid that named a process and does not any more."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    # Give the kernel a moment to reap it out of /proc before anyone looks.
    for _ in range(200):
        if not Path(f"/proc/{proc.pid}").exists():
            break
        time.sleep(0.01)
    return proc.pid


def _agree(env: dict, hops: int = 0) -> str:
    """Both halves' answer for `pane_key`, asserted identical, returned once."""
    bash_key = _bash("qb_pane_key", env, hops=hops)
    py_key = _python("pane_key", env, hops=hops)
    assert bash_key == py_key, (
        f"the two pane derivations disagree: bash={bash_key!r} python={py_key!r}. "
        "That is #765's failure exactly — the hook writes one filename and the "
        "server reads another, so the mechanism no-ops and the suite stays green.")
    return bash_key


def test_a_socket_named_for_a_live_ancestor_is_this_pane(socket_dir):
    """The case the whole mechanism rests on. `os.getpid()` here is pytest, and
    pytest is a real live ancestor of both subprocesses below."""
    owner = os.getpid()
    sock = socket_dir / f"{owner}.sock"
    sock.write_bytes(b"")
    os.utime(sock, (1_700_000_000, 1_700_000_000))
    key = _agree(_env(CLAUDE_CODE_MESSAGING_SOCKET=str(sock)))
    assert key == f"{owner}-1700000000"


def test_the_owner_may_be_further_up_than_the_parent(socket_dir):
    """One hop is the easy case and not the only one. A hook and an MCP server
    are direct children of the CLI, but a Bash tool call is several processes
    down and reaches the same pane — measured on this host, a shell three hops
    below `claude` derived `352423-…` on both sides. The walk has to keep going,
    and it has to stop in the same place on both sides."""
    owner = os.getpid()
    sock = socket_dir / f"{owner}.sock"
    sock.write_bytes(b"")
    os.utime(sock, (1_700_000_000, 1_700_000_000))
    env = _env(CLAUDE_CODE_MESSAGING_SOCKET=str(sock))
    for hops in (1, 3):
        assert _agree(env, hops=hops) == f"{owner}-1700000000", hops


def test_the_sockets_mtime_is_part_of_the_key(socket_dir):
    """A pid is recycled and `$XDG_RUNTIME_DIR` is never swept, so `qb-pane-<pid>`
    could be a file a previous CLI at the same pid left behind. Putting the
    socket's mtime in the NAME means a stale file is simply never found — no
    comparison to get wrong, and no second rule to spell twice."""
    owner = os.getpid()
    sock = socket_dir / f"{owner}.sock"
    sock.write_bytes(b"")
    os.utime(sock, (1_600_000_000, 1_600_000_000))
    assert _agree(_env(CLAUDE_CODE_MESSAGING_SOCKET=str(sock))) == f"{owner}-1600000000"
    os.utime(sock, (1_600_000_099, 1_600_000_099))
    assert _agree(_env(CLAUDE_CODE_MESSAGING_SOCKET=str(sock))) == f"{owner}-1600000099"


@pytest.mark.parametrize("ns", [
    0,
    1,
    500_000_000,
    999_000_000,
    999_999_000,
    # The rows that matter, and the reason `int(st.st_mtime)` is not good enough.
    # `st_mtime` is a float; float64 spacing at 1.7e9 is about 238ns, so an mtime
    # this close to the next second rounds UP and Python names a file one second
    # later than bash does. Measured: ns=…999999999 gives `stat -c %Y`
    # 1700000000 and `int(st_mtime)` 1700000001.
    999_999_900,
    999_999_999,
])
def test_the_sub_second_part_of_the_mtime_is_discarded_identically(socket_dir, ns):
    """The seconds have to match exactly or the two halves name different files.

    A `.999` fixture sat inside the safe band and proved nothing about the band
    that is not safe — which is the shape of a parity table that enumerates a
    class and then samples only the easy part of it. The odds of the bad rows in
    life are about one in eight million, which is exactly the kind of number that
    turns up once and is never reproduced.
    """
    owner = os.getpid()
    sock = socket_dir / f"{owner}.sock"
    sock.write_bytes(b"")
    os.utime(sock, ns=(1_700_000_000_000_000_000 + ns,) * 2)
    assert _agree(_env(CLAUDE_CODE_MESSAGING_SOCKET=str(sock))) == f"{owner}-1700000000"


def test_a_live_process_that_is_not_an_ancestor_owns_no_pane(socket_dir, stranger):
    """The guard that stops one agent speaking for another's pane. A process can
    hold a socket variable it did not earn — measured on this fleet, a shell a
    dead CLI left behind still carries `3524155.sock` — and if that named pane
    were adopted, a SessionStart would end a session it has nothing to do with
    and hand back claims a live agent is still working."""
    sock = socket_dir / f"{stranger}.sock"
    sock.write_bytes(b"")
    assert _agree(_env(CLAUDE_CODE_MESSAGING_SOCKET=str(sock))) == ""


def test_a_socket_whose_owner_is_dead_owns_no_pane(socket_dir, dead_pid):
    sock = socket_dir / f"{dead_pid}.sock"
    sock.write_bytes(b"")
    assert _agree(_env(CLAUDE_CODE_MESSAGING_SOCKET=str(sock))) == ""


def test_no_socket_at_all_owns_no_pane(socket_dir):
    assert _agree(_env()) == ""
    assert _agree(_env(CLAUDE_CODE_MESSAGING_SOCKET="")) == ""


def test_a_socket_the_variable_names_but_that_is_not_there_owns_no_pane(socket_dir):
    """No file, no mtime, no key. Our own CLI's socket exists for as long as it
    is running, so the absence is somebody else's socket or an older boot's."""
    sock = socket_dir / f"{os.getpid()}.sock"
    assert not sock.exists()
    assert _agree(_env(CLAUDE_CODE_MESSAGING_SOCKET=str(sock))) == ""


@pytest.mark.parametrize("basename", [
    "notapid.sock",             # nothing numeric in it at all
    "sock",
    ".sock",                    # the suffix and nothing else
    "claude-1234.sock",         # a pid with a prefix
    "1234",                     # a pid with no suffix
    "1234.socket",              # the wrong suffix
    "1234.sock.bak",
    "١٢٣.sock",                 # Arabic-Indic digits: `\\d` matches these and
                                # `[0-9]` does not. /proc backs the rule up here
                                # — no process is ever named `١٢٣` — so this case
                                # pins the intent rather than catching a live
                                # divergence; the trailing-newline case below is
                                # the one where the rule alone decides.
    "12 34.sock",
    " 1234.sock",
    "1234 .sock",
])
def test_a_basename_that_is_not_a_pid_owns_no_pane(socket_dir, basename):
    sock = socket_dir / basename
    sock.write_bytes(b"")
    assert _agree(_env(CLAUDE_CODE_MESSAGING_SOCKET=str(sock))) == ""


def test_a_trailing_newline_in_the_basename_owns_no_pane(socket_dir):
    """Python's `$` matches before a trailing newline and bash's `case` glob does
    not, so `re.match(r"([0-9]+)\\.sock$")` would call this a pane, take the pid
    out of the group, find it alive and in our ancestry, and name a pane the hook
    would never write to. `fullmatch` is what makes the two agree.

    The newline is IN THE FILENAME, not merely on the end of the variable — a
    variable with a stray newline names a file that is not there, so the stat
    refuses it and the rule under test never runs. A filename may contain a
    newline on Linux, so this is the case that reaches the rule."""
    owner = os.getpid()
    sock = socket_dir / f"{owner}.sock\n"
    sock.write_bytes(b"")
    assert _agree(_env(CLAUDE_CODE_MESSAGING_SOCKET=str(sock))) == ""


def test_a_variable_naming_a_file_that_is_not_there_owns_no_pane(socket_dir):
    """The same string with the newline on the variable rather than in the name.
    Both halves decline, and the reason is the missing file rather than the
    basename rule — kept separate from the case above so that neither is quietly
    passing for the other one's reason."""
    sock = socket_dir / f"{os.getpid()}.sock"
    sock.write_bytes(b"")
    assert _agree(_env(CLAUDE_CODE_MESSAGING_SOCKET=f"{sock}\n")) == ""


def test_a_leading_zero_makes_a_different_pane(socket_dir):
    """Compared as strings on both sides, so `0123` is not `123`. Parsed as an
    integer on one side only, they would be — and one half would find a pane
    where the other found none."""
    sock = socket_dir / f"0{os.getpid()}.sock"
    sock.write_bytes(b"")
    assert _agree(_env(CLAUDE_CODE_MESSAGING_SOCKET=str(sock))) == ""


# ------------------------------------------------------------- the pane's file


def _agree_file(env: dict) -> str:
    bash_path = _bash("qb_pane_file", env)
    py_path = _python("pane_file", env)
    assert bash_path == py_path, (
        f"the two halves name different files: bash={bash_path!r} "
        f"python={py_path!r} — which is a hook writing where nothing reads.")
    return bash_path


def test_the_two_halves_name_the_same_file(socket_dir, tmp_path):
    owner = os.getpid()
    sock = socket_dir / f"{owner}.sock"
    sock.write_bytes(b"")
    os.utime(sock, (1_700_000_000, 1_700_000_000))
    run = tmp_path / "run"
    run.mkdir()
    got = _agree_file(_env(CLAUDE_CODE_MESSAGING_SOCKET=str(sock),
                           XDG_RUNTIME_DIR=str(run)))
    assert got == f"{run}/qb-pane-{owner}-1700000000"


def test_a_runtime_dir_with_a_trailing_slash_names_the_same_string(socket_dir, tmp_path):
    """`os.path.join` collapses the `//` that `"${XDG_RUNTIME_DIR}/…"` keeps, and
    the two halves have to agree on the STRING as well as on the file: one of
    them writes a name and the other looks that name up."""
    owner = os.getpid()
    sock = socket_dir / f"{owner}.sock"
    sock.write_bytes(b"")
    os.utime(sock, (1_700_000_000, 1_700_000_000))
    run = tmp_path / "run"
    run.mkdir()
    got = _agree_file(_env(CLAUDE_CODE_MESSAGING_SOCKET=str(sock),
                           XDG_RUNTIME_DIR=f"{run}/"))
    assert got == f"{run}//qb-pane-{owner}-1700000000"


def test_with_no_runtime_dir_both_halves_fall_back_to_tmp(socket_dir):
    owner = os.getpid()
    sock = socket_dir / f"{owner}.sock"
    sock.write_bytes(b"")
    os.utime(sock, (1_700_000_000, 1_700_000_000))
    for env in (_env(CLAUDE_CODE_MESSAGING_SOCKET=str(sock)),
                _env(CLAUDE_CODE_MESSAGING_SOCKET=str(sock), XDG_RUNTIME_DIR="")):
        assert _agree_file(env) == f"/tmp/qb-pane-{owner}-1700000000"


def test_no_pane_means_no_file_on_either_side(socket_dir):
    assert _agree_file(_env()) == ""
