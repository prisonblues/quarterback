"""Where the client remembers its place in the stream.

Reopening should resume rather than reload — re-reading an hour of posts you
already read is the thing that makes a board client not worth opening. The cursor
is one integer, so it lives in a file rather than anything cleverer.

Keyed by board URL, because a machine can belong to more than one island: the
fleet has a deliberately disjoint board on the work host, and a single shared
cursor would have one board's post ids silently suppressing the other's.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def state_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    # `Path` does not expand a tilde, so a literal "~" fallback for a missing HOME
    # made the cursor land in a directory called `~` under whatever the process's
    # cwd happened to be — a different one per invocation, so resume never worked
    # and the debris was easy to mistake for a stray file. Ask the OS instead:
    # `expanduser` falls back to the password database when HOME is unset.
    home = env.get("HOME") or os.path.expanduser("~")
    base = env.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state")
    return Path(base) / "quarterback"


def cursor_path(base_url: str, env: dict[str, str] | None = None) -> Path:
    key = hashlib.sha256(base_url.rstrip("/").encode()).hexdigest()[:12]
    return state_dir(env) / f"board-cursor-{key}"


def read_cursor(base_url: str, env: dict[str, str] | None = None) -> int:
    """The last post id this client acknowledged, or 0 if it has never run here."""
    try:
        return max(0, int(cursor_path(base_url, env).read_text().strip()))
    except (OSError, ValueError):
        return 0


def write_cursor(base_url: str, cursor: int, env: dict[str, str] | None = None) -> None:
    """Record the cursor, best-effort, and only ever forwards.

    Never raises: an unwritable state directory should cost the *next* run its
    resume point, not this one its tail.

    A tail and a TUI on the same board write this file concurrently, so both
    halves matter. The file means "seen up to here", which is monotonic even when
    a caller's own cursor deliberately rewinds to backfill — a rewind that
    reached disk would make the *other* client replay what it had already shown.
    And the scratch file is per-process, because one fixed ``.tmp`` name meant two
    writers could interleave their partial writes into a single file and rename
    the mixture into place.
    """
    path = cursor_path(base_url, env)
    cursor = int(cursor)
    if cursor <= read_cursor(base_url, env):
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(f"{cursor}\n")
        tmp.replace(path)
    except OSError:
        pass
