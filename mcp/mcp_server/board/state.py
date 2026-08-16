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
    base = env.get("XDG_STATE_HOME") or os.path.join(env.get("HOME", "~"), ".local", "state")
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
    """Record the cursor, best-effort.

    Never raises: an unwritable state directory should cost the *next* run its
    resume point, not this one its tail.
    """
    path = cursor_path(base_url, env)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(f"{int(cursor)}\n")
        tmp.replace(path)
    except OSError:
        pass
