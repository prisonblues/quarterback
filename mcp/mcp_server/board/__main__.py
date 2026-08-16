"""Entry point for ``qb board`` — the tail and the full-screen client.

``qb`` is the fleet's CLI verb and lives (for now) in nix-fleet; issue #28 is
what settles whether it and ``harness/bin`` end up in the same repo. So the
implementation lands here, on the board's side, and a leading literal ``board``
argument is accepted — which is all a ``board) exec qb-board "$@"`` arm in ``qb``
needs to work, in either repo, without either forking the other.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import httpx

from ..client import QuarterbackClient
from .config import NoBoardConfigured, resolve
from .follow import follow
from .render import want_colour
from .state import read_cursor

_PROG = "qb board"

#: app.identity's NAME_RE. A requested name the board would refuse is a 400 on
#: the first request, and there is a better answer than that — see _client.
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=_PROG,
        description="Read the quarterback board from a terminal, and act on this machine.",
    )
    p.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="tail the board to stdout as plain lines instead of opening the full-screen client",
    )
    p.add_argument(
        "-n",
        "--lines",
        type=int,
        default=20,
        metavar="N",
        help="posts of backlog to print before following (default 20; 0 for none)",
    )
    p.add_argument(
        "--since", type=int, metavar="ID", help="start after this post id instead of a backlog"
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="start from the cursor this client last recorded for this board",
    )
    p.add_argument(
        "-t",
        "--type",
        action="append",
        dest="types",
        metavar="TYPE",
        help="only this post type (repeatable)",
    )
    p.add_argument(
        "--to",
        metavar="WHO",
        help="only posts addressed to this agent or machine; @me for this client's own inbox",
    )
    p.add_argument(
        "--presence",
        action="store_true",
        help="include presence heartbeats, hidden by default as ~93%% of the board",
    )
    colour = p.add_mutually_exclusive_group()
    colour.add_argument("--color", "--colour", dest="colour", action="store_true", default=None)
    colour.add_argument("--no-color", "--no-colour", dest="colour", action="store_false")
    p.add_argument(
        "-C",
        "--repo",
        default=".",
        metavar="PATH",
        help="checkout the status line reports staleness for (default: cwd)",
    )
    return p


def _strip_verb(argv: list[str]) -> list[str]:
    return argv[1:] if argv and argv[0] == "board" else argv


def _client(cfg, env: dict[str, str] | None = None) -> QuarterbackClient:
    """The board client this process talks through.

    **No agent key by default, and that is the interesting decision.** Send one
    and the board designates a fresh two-word name for it, so every launch of a
    terminal client would burn a name that never finishes and is never recycled —
    and, worse, ``?to=@me`` would then mean the inbox of an identity that came
    into existence a second ago. Without a key the caller is the bare machine
    name, which is also the broadcast address: `@me` becomes "everything anyone
    has asked of this machine", which is the right question for a human surface
    that is not an agent.

    ``QUARTERBACK_INSTANCE`` is the escape hatch, as it is everywhere else in the
    fleet. Set it and this client has a stable identity of its own — stable
    across invocations, because the key is the label rather than anything
    per-process, so the same box comes back as the same agent rather than as a
    new one each time.
    """
    env = os.environ if env is None else env
    instance = (env.get("QUARTERBACK_INSTANCE") or "").strip()
    # A label the board would reject as a *name* is still a perfectly good key,
    # so it is sent as one rather than 400ing the first request of the session.
    # The board then designates a name for it, which is the documented fallback.
    requested = instance if _NAME_RE.match(instance) else None
    return QuarterbackClient(
        base_url=cfg.base_url,
        api_token=cfg.token or "",
        key=instance or None,
        requested_name=requested,
    )


def resolve_recipient(client: QuarterbackClient, to: str | None, err) -> str | None:
    """Turn ``--to @me`` into the identity the board knows this client by.

    ``@me`` is a server-side spelling: ``/board?to=@me`` resolves it from the
    bearer, but ``/stream`` takes no recipient filter, so the live half of the
    tail filters locally and has nothing to compare `@me` against. Resolving it
    once here keeps the backlog and the live stream meaning the same thing.
    """
    if to != "@me":
        return to
    try:
        return client.whoami().get("agent") or to
    except httpx.HTTPError as e:
        print(f"{_PROG}: could not resolve @me ({e}); showing everything", file=err)
        return None


def _report_health(client: QuarterbackClient, cfg, err) -> int:
    """The tokenless answer: up or down, rather than a stack trace.

    ``/health`` is the one endpoint the server guards with no dependency, which
    is exactly so a host that has not been given a credential can still tell
    whether the board is there.
    """
    try:
        client.health()
    except httpx.HTTPError as e:
        print(f"{_PROG}: {cfg.base_url} is DOWN ({e})", file=err)
        return 1
    print(
        f"{_PROG}: {cfg.base_url} is up, but this machine has no token, so the board\n"
        f"        itself cannot be read. Set QUARTERBACK_TOKEN or QUARTERBACK_TOKEN_CMD\n"
        f"        in {cfg.config_path}.",
        file=err,
    )
    return 1


def _project_dir(cwd: str) -> str:
    """``~/.claude/projects`` directory name for a working directory.

    Claude Code replaces ``/`` and ``.`` with ``-``; ``qb resume`` does the same
    substitution in sed. Getting it wrong writes the transcript somewhere
    ``claude --resume`` will never look, and it fails silently.
    """
    return re.sub(r"[/.]", "-", cwd)


def do_resume(client: QuarterbackClient, session: str, err=sys.stderr) -> int:
    """Pull a session's transcript here and hand the terminal to ``claude --resume``.

    Refuses a session another device still holds a live lease on. That is the
    board's own rule for handoff — two machines resuming one session both write
    transcripts, and the second push silently overwrites the first.
    """
    try:
        state = client.session_state(session)
    except httpx.HTTPError as e:
        print(f"{_PROG}: could not read session {session} ({e})", file=err)
        return 1

    lease = state.get("active_lease") or {}
    if lease:
        holder = lease.get("holder") or lease.get("device") or "another device"
        print(
            f"{_PROG}: {session[:8]} is held by {holder} right now — refusing.\n"
            f"        Resuming a live session forks its transcript; wait for the lease to lapse.",
            file=err,
        )
        return 1

    blob, cwd = state.get("latest_blob"), state.get("cwd")
    if not blob:
        print(f"{_PROG}: session has no transcript yet to resume", file=err)
        return 1
    if not cwd:
        print(
            f"{_PROG}: no recorded cwd; cd to the repo and run: claude --resume {session}", file=err
        )
        return 1

    target = Path.home() / ".claude" / "projects" / _project_dir(cwd) / f"{session}.jsonl"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(client.get_blob(blob))
    except (OSError, httpx.HTTPError) as e:
        print(f"{_PROG}: failed to write transcript ({e})", file=err)
        return 1
    print(f"pulled transcript -> {target}", file=err)

    if not Path(cwd).is_dir():
        print(
            f"{_PROG}: {cwd} is not on this machine — clone it, then: claude --resume {session}",
            file=err,
        )
        return 1
    try:
        os.chdir(cwd)
        os.execvp("claude", ["claude", "--resume", session])
    except OSError as e:
        print(f"{_PROG}: could not exec claude ({e}); run: claude --resume {session}", file=err)
        return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(_strip_verb(list(sys.argv[1:] if argv is None else argv)))
    try:
        cfg = resolve()
    except NoBoardConfigured as e:
        print(f"{_PROG}: {e}", file=sys.stderr)
        return 1

    client = _client(cfg)
    since = args.since
    if since is None and args.resume:
        since = read_cursor(cfg.base_url)

    if args.follow:
        if not cfg.authenticated:
            return _report_health(client, cfg, sys.stderr)
        return follow(
            client,
            cfg.base_url,
            since=since,
            tail=max(0, args.lines),
            types=args.types,
            to=resolve_recipient(client, args.to, sys.stderr),
            presence=args.presence,
            colour=want_colour() if args.colour is None else args.colour,
        )

    try:
        from .tui import BoardApp, ResumeRequest
    except ImportError:
        print(
            f"{_PROG}: the full-screen client needs Textual, which is not installed.\n"
            f"        Install it (pip install 'quarterback-mcp[tui]'), or use\n"
            f"        `{_PROG} --follow`, which needs nothing extra.",
            file=sys.stderr,
        )
        return 1

    app = BoardApp(client, cfg, repo_path=args.repo)
    result = app.run()
    if isinstance(result, ResumeRequest):
        return do_resume(client, result.session)
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry
    sys.exit(main())
