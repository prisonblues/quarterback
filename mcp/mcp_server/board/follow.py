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

import contextlib
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

#: ``GET /board`` refuses a ``limit`` above this.
_SERVER_LIMIT = 1000

#: How much more than `-n` to ask for when the type filter cannot be pushed to
#: the server. Sized for the shape of a real board — roughly nine posts in ten
#: are heartbeats — so that twenty asks/findings are usually in the window.
_OVERFETCH = 20

#: Seconds between cursor writes. The cursor advances on every post; only the
#: file lags, and only by this much.
_CURSOR_INTERVAL = 5.0


def wants(post: dict, types: set[str] | None, to: str | None, presence: bool) -> bool:
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


def _newest_id(posts: list[dict]) -> int:
    return max((int(p.get("id") or 0) for p in posts), default=0)


def _head(client: QuarterbackClient) -> int:
    """The id of the newest post on the board, in one deliberately tiny request.

    ``include_presence`` because heartbeats are most of the board and carry its
    highest ids, and ``window_min=0`` because a board that has been quiet longer
    than the default window still has an end.
    """
    return _newest_id(client.board({"limit": 1, "window_min": 0, "include_presence": True}))


def _backlog_params(tail: int, types: set[str] | None, to: str | None, presence: bool) -> dict:
    params: dict = {"limit": tail, "window_min": 0}
    if types is not None and len(types) == 1:
        # An explicit ``type=`` is honoured verbatim by the board, heartbeats
        # included, so ``-t presence`` needs no ``include_presence`` beside it.
        params["type"] = next(iter(types))
    elif presence or (types is not None and "presence" in types):
        params["include_presence"] = True
    if types is not None and len(types) > 1:
        # ``/board`` takes one type at a time, so several are matched here
        # instead — and asking it for exactly `tail` posts fetched `tail` posts
        # of every type, of which typically one or two survived the filter.
        # `-n 20` has to mean twenty lines, so over-fetch and trim.
        params["limit"] = min(_SERVER_LIMIT, tail * _OVERFETCH)
    if to is not None:
        params["to"] = to
    return params


def _backlog(
    client: QuarterbackClient,
    tail: int,
    types: set[str] | None,
    to: str | None,
    presence: bool,
) -> tuple[list[dict], int | None]:
    """The last `tail` matching posts, and where the stream should pick up.

    Fetched through ``/board`` rather than replayed through ``/stream`` with a
    low cursor: only ``/board`` can bound the count, and `--follow -n 20` on a
    board with 3000 posts must not stream all 3000 to throw away 2980.

    The cursor is the half that is easy to get wrong. It has to be the newest id
    the *server* returned, never the newest id printed — and a narrowed request
    doesn't carry it at all, since the ids in between belong to the posts that
    were filtered out. So this says ``None`` when it cannot know, and the caller
    anchors properly rather than defaulting to the beginning of the board.
    """
    if tail <= 0:
        return [], None
    params = _backlog_params(tail, types, to, presence)
    posts = list(client.board(params))
    narrowed = "type" in params or "to" in params or not params.get("include_presence")
    matched = [p for p in posts if wants(p, types, to, presence)]
    return matched[-tail:], None if narrowed else _newest_id(posts)


def quiet_broken_pipe() -> None:
    """Make a closed reader a clean exit rather than a traceback.

    `qb board --follow | head -20` is ordinary use of this tool, and `head`
    closing the pipe is how it ends. Python flushes stdout again at interpreter
    exit, so catching the error is not enough on its own — the descriptor has to
    be pointed somewhere harmless or the process still prints a BrokenPipeError
    after the tail has already finished cleanly.
    """
    devnull = None
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass
    finally:
        # dup2 gave stdout its own descriptor for the same file, so this one has
        # done its job. Leaving it open leaks a descriptor into whatever the
        # process does next.
        if devnull is not None:
            with contextlib.suppress(OSError):
                os.close(devnull)


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
    clock: Callable[[], float] = time.monotonic,
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
            to=to, presence=presence, colour=colour, sleep=sleep, clock=clock,
            max_reconnects=max_reconnects, env=env,
        )
    except KeyboardInterrupt:
        # Ctrl-C is how a tail ends, wherever it lands: during the backlog fetch
        # and its emit loop as much as inside the stream loop. Guarding only the
        # loop meant an interrupt in the first second of the process printed a
        # traceback instead of stopping.
        return 0
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
    clock: Callable[[], float],
    max_reconnects: int | None,
    env: dict[str, str] | None,
) -> int:
    """The loop itself, with every argument already resolved.

    Split out only so :func:`follow` can wrap the whole of it in one
    broken-pipe guard rather than guarding each write.
    """

    def emit(post: dict) -> None:
        print(format_post(post, colour=colour), file=out, flush=True)

    # Post ids are 1-based, so a `since` of 0 carries no information about where
    # to resume — and reading it as "from the beginning" is how `--resume` with
    # nothing recorded turned into a replay of the whole board. Treat it as the
    # absence it is, and anchor instead.
    cursor: int | None = since if since is not None and since > 0 else None

    persisted = cursor or 0
    last_write = float("-inf")

    def persist(value: int, *, force: bool = False) -> None:
        """Record the cursor, at most every `_CURSOR_INTERVAL` seconds.

        A busy board delivers heartbeats faster than once a second and each one
        is a mkdir, a write and a rename, most of them for a post that is never
        printed. The cursor still advances on every post — only the file lags,
        and the tail flushes it on the way out so the lag costs nothing.
        """
        nonlocal persisted, last_write
        if value <= persisted:
            return
        now = clock()
        if not force and now - last_write < _CURSOR_INTERVAL:
            return
        write_cursor(base_url, value, env)
        persisted, last_write = value, now

    try:
        if cursor is None:
            backlog: list[dict] = []
            try:
                backlog, cursor = _backlog(client, tail, types, to, presence)
            except httpx.HTTPStatusError as e:
                # A 4xx is this process asking for the wrong thing, and retrying
                # cannot fix it. A 5xx is the same transient outage the stream
                # loop below reconnects through — treating it as fatal made a
                # board restarting during startup kill a tail that would have
                # recovered a second later.
                if _is_client_error(e):
                    return _fatal(e, err)
                print(f"qb board: backlog unavailable (HTTP {e.response.status_code})", file=err)
            except httpx.HTTPError as e:
                print(f"qb board: {e}", file=err, flush=True)
            for post in backlog:
                emit(post)
            if cursor is not None:
                # A tail that prints its backlog on a quiet board and is then
                # Ctrl-C'd before anything live lands is the common case, and it
                # used to record nothing at all — so the next `--resume` reprinted
                # the backlog it had just shown.
                persist(cursor, force=True)

        attempts, delay = 0, _BACKOFF_START
        while True:
            if cursor is None:
                try:
                    cursor = _head(client)
                except httpx.HTTPStatusError as e:
                    if _is_client_error(e):
                        return _fatal(e, err)
                    _no_anchor(err, f"HTTP {e.response.status_code}")
                except httpx.HTTPError as e:
                    _no_anchor(err, type(e).__name__)

            if cursor is not None:
                try:
                    for post in client.stream(since=cursor):
                        cursor = max(cursor, int(post.get("id") or 0))
                        persist(cursor)
                        # Reconnecting is now cheap again: a stream that
                        # delivered anything has proved the board is up, so the
                        # next drop should retry immediately rather than inherit
                        # an old backoff.
                        attempts, delay = 0, _BACKOFF_START
                        if wants(post, types, to, presence):
                            emit(post)
                except httpx.HTTPStatusError as e:
                    if _is_client_error(e):
                        return _fatal(e, err)
                    print(
                        f"qb board: stream returned {e.response.status_code}; reconnecting",
                        file=err,
                    )
                except httpx.HTTPError as e:
                    print(f"qb board: stream dropped ({type(e).__name__}); reconnecting", file=err)

            attempts += 1
            if max_reconnects is not None and attempts > max_reconnects:
                return 0
            sleep(delay)
            delay = min(delay * 2, _BACKOFF_MAX)
    finally:
        if cursor is not None:
            persist(cursor, force=True)


def _is_client_error(e: httpx.HTTPStatusError) -> bool:
    """Is this our mistake rather than the board's trouble?

    Anything 4xx — a bad filter, a wrong path, an unparseable argument — comes
    back identically however many times it is asked, so retrying it produces an
    endless loop where the user wanted a message they could act on.
    """
    return 400 <= e.response.status_code < 500


def _no_anchor(err, why: str) -> None:
    print(
        f"qb board: cannot read the end of the board ({why}); retrying rather than "
        "streaming from the beginning",
        file=err,
        flush=True,
    )


def _fatal(e: httpx.HTTPStatusError, err) -> int:
    """Report an unretryable status and stop. Every fatal path arrives here."""
    code = e.response.status_code
    if code in (401, 403):
        detail = (
            f"the board rejected this machine's token ({code}). "
            "Check QUARTERBACK_TOKEN / QUARTERBACK_TOKEN_CMD."
        )
    else:
        detail = (
            f"the board rejected this request ({code}). Check the arguments and the "
            "board URL — retrying cannot change the answer."
        )
    print(f"qb board: {detail}", file=err, flush=True)
    return 1
