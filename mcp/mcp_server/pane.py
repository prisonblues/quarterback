"""The derivations that exist TWICE, and the bash half is `harness/bin/qb-env`.

Claude Code is the one runtime with both a lifecycle hook and an MCP server —
two processes that have to land on one board identity — and one of them is bash.
So two rules are spelled in two languages, and PR #765 is what happens when they
drift: it shipped two slug implementations that disagreed about trailing
whitespace and about byte-vs-character classes, so the hook wrote one filename,
the server read another, the mechanism was a silent no-op, and the suite was
green. Everything with a twin lives in this file so that it has exactly one
counterpart file, and `harness/tests/test_pane_parity.py` runs both halves over
one table and requires the same answer of each.

## The pane: one Claude Code CLI process, however many conversations (#146)

`/clear` does not restart the CLI. It mints a new conversation id inside the same
process, and Claude Code injects the CURRENT conversation's id into every process
it spawns from then on. So `qb-hook` — a fresh process per event — is always
right about which conversation it is in, and this server, spawned once at
startup and never again, is the one thing that is not: it kept answering with the
conversation that happened to be live when it was launched. Measured 2026-09-06:
`whoami` in a session returned key `4c8f6a8a`, a conversation whose transcript
stopped 22 hours earlier, while the live conversation was `e267ef87` — and five
board claims taken that day carried the dead id.

What the two halves need in common is a name for the CLI PROCESS: something both
can derive, that a clear does not move. `CLAUDE_CODE_MESSAGING_SOCKET` is it —
`/run/user/<uid>/cc-socks/<cli pid>.sock`, created when the CLI starts and still
the same value across a conversation switch a day later. So the frozen
environment this server holds is WRONG for the session id and RIGHT for the
socket, and that asymmetry is the whole fix: the hook writes the current
conversation to a file named after the pane, and this reads it per call.

Two guards, both load-bearing, both explained in `harness/bin/qb-env` beside the
bash half of each. Ownership: the pane is ours only if the pid the socket names
is alive AND an ancestor of this process, so a stray process carrying a dead
CLI's socket — or a nested agent carrying its parent's — declines rather than
speaking for somebody else's pane. Recency: the socket's mtime is part of the
key, so a `qb-pane-<pid>` file left behind by a previous CLI at a recycled pid
simply has a different name and is never read.

Every rule here is therefore written to be spellable identically in bash: string
comparison rather than integer, `[0-9]` rather than `\\d` (which matches
Arabic-Indic digits), `fullmatch` rather than a `$` anchor (which in Python also
matches before a trailing newline), and plain concatenation rather than
`os.path.join` (which collapses a `//` that `"${XDG_RUNTIME_DIR}/…"` keeps). A
new rule needs a new row in the parity table.

Stdlib only, and deliberately: `harness/tests/test_pane_parity.py` loads this
file by path so the parity check runs without the MCP venv.
"""

from __future__ import annotations

import os
import re

#: `<digits>.sock`, and nothing else. Three deliberate choices, each of which is
#: a way the two languages come apart:
#:
#: * `fullmatch`, not a `$` anchor — `$` also matches BEFORE a trailing newline,
#:   which an environment variable can carry and bash's `case "$base" in *.sock)`
#:   cannot accept;
#: * `[0-9]`, not `\d`, which in Python also matches Arabic-Indic digits;
#: * the owner comes out of the CAPTURE GROUP rather than from slicing the
#:   basename, so this one expression is the whole rule and there is no second
#:   place for the two halves to disagree about where the pid ends.
_SOCK_NAME = re.compile(r"([0-9]+)\.sock")

#: How far up the process tree to look for the socket's owner. A bound rather
#: than a `while True`: /proc is a live filesystem and a cycle in it, however it
#: arose, must not hang a tool call.
_MAX_HOPS = 64


#: The board's key charset, as BYTES. `_key_slug` in `harness/bin/qb-hook`
#: spells the same rule with
#: `tr -c 'A-Za-z0-9._~-' '-'`, and `tr` works on bytes — so a character-wise
#: substitution here would turn one non-ASCII character into one `-` where the
#: hook turns it into two or three, and the two halves of an agent would send
#: different keys and land on different board identities.
_ALLOWED_KEY_BYTES = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-")


def key_slug(value: str) -> str | None:
    """Coerce a value to something the board accepts as a key, or None.

    No `.strip()` first, deliberately: the hook's pipeline has no equivalent, so
    a trailing space is a `-` there and used to be nothing here.
    """
    raw = value.encode("utf-8", "surrogateescape")
    swapped = bytes(b if b in _ALLOWED_KEY_BYTES else 0x2D for b in raw)
    return swapped.decode("ascii").lstrip("._~-")[:40] or None


def _ppid(pid: str) -> str | None:
    """`pid`'s parent, read from /proc, or None if that cannot be established."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            stat = fh.read()
    except OSError:
        return None
    # `comm` is parenthesised and may itself contain spaces and parens, so the
    # fields are unambiguous only after the LAST ") ". Same rule in qb-env.
    cut = stat.rfind(") ")
    if cut < 0:
        return None
    fields = stat[cut + 2:].split()
    return fields[1] if len(fields) >= 2 else None


def pane_key() -> str | None:
    """``<cli pid>-<socket mtime>``, or None when this process owns no pane."""
    sock = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET", "")
    if not sock:
        return None
    named = _SOCK_NAME.fullmatch(sock.rsplit("/", 1)[-1])
    if not named:
        return None
    owner = named.group(1)
    try:
        mtime = int(os.stat(sock).st_mtime)
    except OSError:
        return None
    # `int()` truncates toward zero where `stat -c %Y` floors, so the two differ
    # on a pre-epoch mtime and agree on every other. Refused rather than
    # reconciled: a socket dated before 1970 is not one this CLI just created.
    if mtime < 0:
        return None
    if not os.path.isdir(f"/proc/{owner}"):
        return None
    # Compared as STRINGS, never as numbers: `0123` and `123` are different panes
    # here and in qb-env, rather than the same one on whichever side happens to
    # parse an integer.
    pid: str | None = str(os.getpid())
    for _ in range(_MAX_HOPS):
        if not pid or pid == "0":
            return None
        if pid == owner:
            return f"{owner}-{mtime}"
        pid = _ppid(pid)
    return None


def pane_file() -> str | None:
    """The file holding this pane's current conversation, or None."""
    key = pane_key()
    if not key:
        return None
    # Concatenated, NOT `os.path.join`: bash's `"${XDG_RUNTIME_DIR:-/tmp}/…"`
    # keeps a double slash where `join` collapses it, and the two halves have to
    # name the same string as well as the same file.
    return f'{os.environ.get("XDG_RUNTIME_DIR") or "/tmp"}/qb-pane-{key}'


def pane_session() -> str | None:
    """The conversation `qb-hook` last saw in this pane, or None.

    None covers every "we cannot know": no pane, no hook writing the file, a
    board-less host, a runtime that is not Claude Code at all. The caller falls
    back to its own environment, which is what it did before this existed — so
    the worst this mechanism can do when it is wrong is nothing.
    """
    path = pane_file()
    if not path:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(4096).strip() or None
    except OSError:
        return None
