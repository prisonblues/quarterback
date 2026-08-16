"""``qb board --follow`` — the board tailed to stdout, journalctl-style.

The cheap half of issue #110, and deliberately the half that ships first. It is
plain lines on a pipe: no full-screen app to get in the way of a session, no
terminal it can't run in, and `| grep finding` works because the output is text.
That is most of the reach problem solved for very little — a headless host with
ssh and this binary can see the board.

Reconnection is the only complexity, and it earns its place: the point of a tail
is being left running, and an SSE connection dropped by a proxy overnight must
resume from the cursor rather than either dying or replaying the day.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Iterable

import httpx

from ..client import QuarterbackClient
from .render import format_post
from .state import write_cursor
from .views import addressed_to

#: Reconnect backoff, seconds. Capped low enough that a board coming back is
#: noticed within half a minute.
_BACKOFF_START = 1.0
_BACKOFF_MAX = 30.0


def wants(post: dict, types: Iterable[str] | None, to: str | None, presence: bool) -> bool:
    """Should this post be printed?

    Presence heartbeats are excluded unless asked for — ~93% of the board, and
    the same call ``GET /board`` already makes. Naming presence in ``types`` is
    itself asking for it, so ``--type presence`` needs no second flag.
    """
    ptype = post.get("type", "note")
    if types is not None:
        if ptype not in types:
            return False
    elif ptype == "presence" and not presence:
        return False
    return to is None or addressed_to(post.get("to"), to)


def _backlog(
    client: QuarterbackClient,
    tail: int,
    types: Iterable[str] | None,
    to: str | None,
    presence: bool,
) -> list[dict]:
    """The last `tail` posts, so a fresh tail opens with context rather than silence.

    Fetched through ``/board`` rather than replayed through ``/stream`` with a
    low cursor: only ``/board`` can bound the count, and `--follow -n 20` on a
    board with 3000 posts must not stream all 3000 to throw away 2980.
    """
    if tail <= 0:
        return []
    params: dict = {"limit": tail, "window_min": 0}
    if types is not None and len(list(types)) == 1:
        params["type"] = next(iter(types))
    elif presence or (types is not None and "presence" in types):
        params["include_presence"] = True
    if to is not None:
        params["to"] = to
    return [p for p in client.board(params) if wants(p, types, to, presence)]


def quiet_broken_pipe() -> None:
    """Make a closed reader a clean exit rather than a traceback.

    `qb board --follow | head -20` is ordinary use of this tool, and `head`
    closing the pipe is how it ends. Python flushes stdout again at interpreter
    exit, so catching the error is not enough on its own — the descriptor has to
    be pointed somewhere harmless or the process still prints a BrokenPipeError
    after the tail has already finished cleanly.
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass


def follow(
    client: QuarterbackClient,
    base_url: str,
    *,
    since: int | None = None,
    tail: int = 20,
    types: Iterable[str] | None = None,
    to: str | None = None,
    presence: bool = False,
    colour: bool = True,
    out=None,
    err=None,
    sleep: Callable[[float], None] = time.sleep,
    max_reconnects: int | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """Print the board to `out` forever. Returns a process exit code.

    `max_reconnects` bounds the retry loop (None = forever) so the suite can
    drive the same code path a long-running tail takes without being one.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    types = set(types) if types is not None else None

    try:
        return _tail(
            client, base_url, out=out, err=err, since=since, tail=tail, types=types,
            to=to, presence=presence, colour=colour, sleep=sleep,
            max_reconnects=max_reconnects, env=env,
        )
    except BrokenPipeError:
        # Only when we are the ones writing to the process's stdout. A caller
        # that passed its own stream owns it, and pointing the real descriptor at
        # /dev/null on its behalf would be a surprising thing to do to it — under
        # pytest, for one, that descriptor is the capture buffer.
        if out is sys.stdout:
            quiet_broken_pipe()
        return 0


def _tail(
    client: QuarterbackClient,
    base_url: str,
    *,
    out,
    err,
    since: int | None,
    tail: int,
    types: set[str] | None,
    to: str | None,
    presence: bool,
    colour: bool,
    sleep: Callable[[float], None],
    max_reconnects: int | None,
    env: dict[str, str] | None,
) -> int:
    """The loop itself, with every argument already resolved.

    Split out only so :func:`follow` can wrap the whole of it in one
    broken-pipe guard rather than guarding each write.
    """

    def emit(post: dict) -> None:
        print(format_post(post, colour=colour), file=out, flush=True)

    cursor = since if since is not None else 0
    if since is None:
        try:
            backlog = _backlog(client, tail, types, to, presence)
        except httpx.HTTPStatusError as e:
            # Only a rejected token is fatal here. A 5xx on the backlog fetch is
            # the same transient outage the stream loop below reconnects
            # through, and treating it as fatal made a board restarting during
            # startup kill a tail that would have recovered a second later.
            if e.response.status_code in (401, 403):
                return _fatal(e, err)
            print(f"qb board: backlog unavailable (HTTP {e.response.status_code})", file=err)
            backlog = []
        except httpx.HTTPError as e:
            print(f"qb board: {e}", file=err, flush=True)
            backlog = []
        for post in backlog:
            emit(post)
            cursor = max(cursor, int(post.get("id") or 0))

    attempts, delay = 0, _BACKOFF_START
    while True:
        try:
            for post in client.stream(since=cursor):
                pid = int(post.get("id") or 0)
                cursor = max(cursor, pid)
                write_cursor(base_url, cursor, env)
                # Reconnecting is now cheap again: a stream that delivered
                # anything has proved the board is up, so the next drop should
                # retry immediately rather than inherit an old backoff.
                attempts, delay = 0, _BACKOFF_START
                if wants(post, types, to, presence):
                    emit(post)
        except KeyboardInterrupt:
            return 0
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                return _fatal(e, err)
            print(f"qb board: stream returned {e.response.status_code}; reconnecting", file=err)
        except httpx.HTTPError as e:
            print(f"qb board: stream dropped ({type(e).__name__}); reconnecting", file=err)

        attempts += 1
        if max_reconnects is not None and attempts > max_reconnects:
            return 0
        try:
            sleep(delay)
        except KeyboardInterrupt:
            return 0
        delay = min(delay * 2, _BACKOFF_MAX)


def _fatal(e: httpx.HTTPStatusError, err) -> int:
    code = e.response.status_code
    if code in (401, 403):
        print(
            "qb board: the board rejected this machine's token "
            f"({code}). Check QUARTERBACK_TOKEN / QUARTERBACK_TOKEN_CMD.",
            file=err,
            flush=True,
        )
    else:
        print(f"qb board: board returned {code}", file=err, flush=True)
    return 1
