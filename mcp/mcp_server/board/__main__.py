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
import shlex
import sys
import time
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

#: ``GET /board`` declares ``limit: int = Query(ge=1, le=1000)``, so anything
#: above this is a 422 rather than a longer backlog — and the response is
#: materialised into a list here, so an unbounded ``-n`` is our allocation too.
_MAX_LINES = 1000

#: What a session id is allowed to look like. The value comes back from the
#: board and is interpolated into a filename, so it is matched against a shape
#: rather than merely escaped: uuid4 is what Claude Code writes, and nothing
#: with a separator, a dot or unbounded length gets near a path.
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

#: The options only the tail acts on, dest → the spelling to name back at the
#: user. The full-screen client is a different surface, driven by its own keys,
#: so there is nothing to forward these to — and a documented flag that does
#: nothing and says nothing reads as a bug in the filter, not in the command.
_FOLLOW_ONLY = {
    "since": "--since",
    "resume": "--resume",
    "lines": "-n/--lines",
    "types": "-t/--type",
    "to": "--to",
    "presence": "--presence",
    "colour": "--color/--no-color",
}


class RecipientUnresolved(Exception):
    """``--to @me`` could not be turned into the identity the board knows."""


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
        help=(
            f"--follow only: print up to N posts of backlog before following "
            f"(default 20; 0 for none; capped at {_MAX_LINES}, the server's own limit). "
            f"Fewer, if the filters match fewer than N in what the board will serve"
        ),
    )
    p.add_argument(
        "--since",
        type=int,
        metavar="ID",
        help="--follow only: start after this post id instead of a backlog",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help=(
            "--follow only: start from the cursor this client last recorded for this board "
            "(a backlog, if it has never run against it)"
        ),
    )
    p.add_argument(
        "-t",
        "--type",
        action="append",
        dest="types",
        metavar="TYPE",
        help="--follow only: only this post type (repeatable)",
    )
    p.add_argument(
        "--to",
        metavar="WHO",
        help=(
            "--follow only: only posts addressed to this agent or machine; "
            "@me for this client's own inbox"
        ),
    )
    p.add_argument(
        "--presence",
        action="store_true",
        help="--follow only: include presence heartbeats, hidden by default as ~93%% of the board",
    )
    colour = p.add_mutually_exclusive_group()
    colour.add_argument(
        "--color",
        "--colour",
        dest="colour",
        action="store_true",
        default=None,
        help="--follow only: colour the output even when stdout is not a tty",
    )
    colour.add_argument(
        "--no-color",
        "--no-colour",
        dest="colour",
        action="store_false",
        help="--follow only: never colour the output",
    )
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


def resolve_recipient(client: QuarterbackClient, to: str | None) -> str | None:
    """Turn ``--to @me`` into the identity the board knows this client by.

    ``@me`` is a server-side spelling: ``/board?to=@me`` resolves it from the
    bearer, but ``/stream`` takes no recipient filter, so the live half of the
    tail filters locally and has nothing to compare `@me` against. Resolving it
    once here keeps the backlog and the live stream meaning the same thing.

    Raises rather than degrading to ``None`` when ``/whoami`` is unreachable:
    ``None`` is "no recipient filter", so the failure would answer a request for
    one inbox with the entire board — the wrong answer, silently, to the one
    question where the user is watching for something addressed to them.
    """
    if to != "@me":
        return to
    try:
        return client.whoami().get("agent") or to
    except httpx.HTTPError as e:
        raise RecipientUnresolved(str(e)) from e


def _no_token_source_remedy(cfg) -> list[str]:
    """What to do when nothing on this host can produce a token — written once.

    Two callers, a dozen lines apart in :func:`_report_health`: the named-failure
    branch whose failing source was *not* a token command, and the plain "nothing was
    configured at all" ending. They said almost the same thing in almost the same
    words, which is the shape that drifts — one gets reworded, the other does not, and
    the same host gets different advice depending on which arm it lands in. The act
    being recommended is identical, so the text is built here and each caller prefixes
    only the sentence that genuinely differs.
    """
    return [
        "        Set QUARTERBACK_TOKEN or QUARTERBACK_TOKEN_CMD in",
        f"        {cfg.config_path}, or export QUARTERBACK_TOKEN for this",
        "        invocation.",
    ]


def _report_health(client: QuarterbackClient, cfg, err) -> int:
    """The tokenless answer: up or down, rather than a stack trace.

    ``/health`` is the one endpoint the server guards with no dependency, which
    is exactly so a host that has not been given a credential can still tell
    whether the board is there.

    Two different tokenless states, because the remedies are not the same one.
    Nothing configured is the operator's to fix in their config. A credential
    source that ran and yielded nothing is not: this said "this machine has no
    token" on a host whose token file was present and valid, and the cheapest way
    to act on that message was to edit a `# Generated by ... do not edit` file to
    work around a client bug (#201).
    """
    try:
        client.health()
    except httpx.HTTPError as e:
        print(f"{_PROG}: {cfg.base_url} is DOWN ({e})", file=err)
        return 1
    if cfg.token_problem:
        # The agent name is in here on purpose. `$QUARTERBACK_AGENT` is how a site
        # picks its own line out of a shared token file, so when a command comes back
        # empty the identity it ran under is the first thing worth seeing — and the
        # bug this replaces was that name being empty at exactly this moment.
        #
        # `config.resolve` populates `token_problem` for every consumer, but this is
        # so far the only reader of it: the MCP server and the two harness-side
        # clients still print "no token" for a command that ran. Tracked in #235.
        #
        # And it ends with what to DO. A named failure the operator cannot act on is
        # a better diagnosis and the same dead end.
        #
        # Which remedy is the right one is `token_cmd_configured`'s question, not a
        # question about the wording of `token_problem`: the source that failed can be
        # the legacy token file on a host that configures no command at all, and telling
        # *that* operator "adding or editing a token command is not the remedy" would be
        # this bug over again — a confident instruction that is false on the box reading
        # it. Which is also why the two arms below say different things about the same
        # variable: adding or editing a token *command* is what is not the remedy here,
        # while exporting `QUARTERBACK_TOKEN` is a one-shot bypass of the command and is
        # available on every host, working or not.
        lines = [
            f"{_PROG}: {cfg.base_url} is up, but no token could be resolved, so the",
            "        board itself cannot be read.",
            f"        {cfg.token_problem}, running as agent {cfg.agent!r}.",
        ]
        if cfg.token_cmd_configured:
            # `shlex.quote` because an agent name is environment-overridable, and a
            # line offered for copy-pasting has to survive whatever is in it.
            #
            # The order of the three recipe lines is load-bearing, and it is the reverse
            # of the one that reads naturally. `config.resolve` puts the resolved name
            # into the environment FIRST — before it sources the file, before the command
            # runs — and deliberately does not honour a `QUARTERBACK_AGENT=` line in the
            # file at all; that divergence from `qb-env` is argued in config.py's
            # docstring. Sourcing after the export would hand exactly that ignored line
            # the chance to overwrite the name, so the paste would run the command under
            # an identity the client never used. A reproduction that reproduces something
            # else is worse than none here, because naming the identity the command ran
            # under IS this message (#201).
            #
            # And `wc -c` rather than a bare eval: two lines above, this promises not to
            # repeat the command's output because it can be the token. Telling the
            # operator to print it themselves puts the credential in scrollback — and in
            # shell history, if typed — to answer a question that a byte count already
            # answers, since the failure being diagnosed is "no output".
            cfg_file, agent = shlex.quote(str(cfg.config_path)), shlex.quote(cfg.agent)
            lines += [
                "        The command is the environment's QUARTERBACK_TOKEN_CMD, or the",
                f"        one in {cfg.config_path} — one of the two is",
                "        already set, so adding or editing a token command is not the",
                "        remedy. To see what it says, run it yourself under that name;",
                "        its own output is deliberately not repeated here, because it",
                "        can be the token:",
                f"            . {cfg_file}   # skip if yours is in the environment",
                f"            export QUARTERBACK_AGENT={agent}   # after the source, not before",
                '            eval "$QUARTERBACK_TOKEN_CMD" | wc -c   # bytes, not the token',
                "        The export goes AFTER the source on purpose: this client ignores",
                "        a QUARTERBACK_AGENT= line in the file, so sourcing last would run",
                "        the command as a name the client never used. And 0 bytes is the",
                "        failure above, reproduced, with nothing secret in your",
                "        scrollback — drop the pipe once you need the value itself.",
                "        Or export QUARTERBACK_TOKEN in the environment — a one-shot override",
                "        that bypasses the command rather than fixing it, and needs no edit",
                "        to a generated config.",
            ]
        else:
            lines += [
                "        Nothing configured a token command, so that was the last-resort",
                "        fallback failing rather than your own configuration.",
                *_no_token_source_remedy(cfg),
            ]
        print("\n".join(lines), file=err)
        return 1
    print(
        "\n".join(
            [
                f"{_PROG}: {cfg.base_url} is up, but this machine has no token, so the",
                "        board itself cannot be read.",
                *_no_token_source_remedy(cfg),
            ]
        ),
        file=err,
    )
    return 1


def _project_dir(cwd: str) -> str:
    """``~/.claude/projects`` directory name for a working directory.

    Claude Code replaces ``/``, ``.`` **and** ``_`` with ``-`` — the underscore
    is the one that is easy to miss, and a real directory proves it:
    ``/tmp/panel-claude-3q6p345_/cwd`` is stored as
    ``-tmp-panel-claude-3q6p345--cwd``, with the doubled dash where the ``_``
    was. Getting it wrong writes the transcript somewhere ``claude --resume``
    will never look, and it fails silently: you get a fresh session with no
    history and no error.
    """
    return re.sub(r"[/._]", "-", cwd)


def _transcript_path(session: str, cwd: str) -> Path:
    """Where ``claude --resume <session>`` will look for this session's transcript.

    Both halves come off the wire, so neither is trusted. The session id is
    interpolated into a filename, and one carrying ``/``, ``..`` or an absolute
    path would put the write anywhere on the disk; the regex is the guard. The
    containment check behind it is deliberate belt-and-braces — a later, looser
    session format cannot quietly turn this back into an arbitrary write.
    """
    if not _SESSION_RE.match(session):
        raise ValueError(f"refusing session id {session!r}: not a bare id")
    projects = (Path.home() / ".claude" / "projects").resolve()
    target = projects / _project_dir(cwd) / f"{session}.jsonl"
    if projects not in target.resolve().parents:
        raise ValueError(f"refusing to write {target}: outside {projects}")
    return target


def _lease_holder(state: dict) -> str | None:
    """Who holds this session right now, or None if nobody does."""
    lease = state.get("active_lease") or {}
    if not lease:
        return None
    return lease.get("holder") or lease.get("device") or "another device"


def _held_by(client: QuarterbackClient, session: str, err) -> str | None:
    """Re-read the lease. Returns the holder if one is live *now*.

    A board that cannot be reached counts as "not held": this is an advisory
    re-read of something already read successfully once, and turning a dropped
    packet into a refusal would make resuming flakier than the fork it guards
    against — which the local backup in ``_pull`` already survives.
    """
    try:
        return _lease_holder(client.session_state(session))
    except httpx.HTTPError as e:
        print(f"{_PROG}: could not re-check the lease ({e}); continuing", file=err)
        return None


def _refuse_held(session: str, holder: str, err) -> int:
    print(
        f"{_PROG}: {session[:8]} is held by {holder} right now — refusing.\n"
        f"        Resuming a live session forks its transcript; wait for the lease to lapse.",
        file=err,
    )
    return 1


def _keep_local_copy(target: Path) -> Path:
    """Move an existing transcript aside instead of writing over it.

    Every other refusal in this file exists so a resume cannot destroy somebody's
    work; this is the one write that could. If the exit hook never pushed — a
    crash, a ``kill -9``, no network — the local jsonl is *ahead* of the board's
    blob, and overwriting it loses the only copy. The suffix goes after
    ``.jsonl`` rather than replacing it so Claude Code, which reads every
    ``*.jsonl`` in the directory, does not pick the backup up as a session.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = target.with_name(f"{target.name}.{stamp}.local")
    n = 1
    while backup.exists():
        backup = target.with_name(f"{target.name}.{stamp}-{n}.local")
        n += 1
    target.replace(backup)
    return backup


def _pull(client: QuarterbackClient, blob: str, target: Path, err) -> bool:
    """Fetch the blob and land it at `target`, keeping any differing local copy."""
    try:
        data = client.get_blob(blob)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and (target.stat().st_size != len(data) or target.read_bytes() != data):
            kept = _keep_local_copy(target)
            print(f"{_PROG}: local transcript differed from the board's; kept at {kept}", file=err)
        target.write_bytes(data)
    except (OSError, httpx.HTTPError) as e:
        print(f"{_PROG}: failed to write transcript ({e})", file=err)
        return False
    print(f"pulled transcript -> {target}", file=err)
    return True


def do_resume(client: QuarterbackClient, session: str, err=sys.stderr) -> int:
    """Pull a session's transcript here and hand the terminal to ``claude --resume``.

    Refuses a session another device still holds a live lease on. That is the
    board's own rule for handoff — two machines resuming one session both write
    transcripts, and the second push silently overwrites the first.

    **The check is advisory, not a lock**, and cannot be made into one here. It
    is re-read immediately before the write and again immediately before the
    exec, which narrows the window to about as small as it goes without faking
    exclusivity — but this process deliberately takes no lease. A lease claimed
    by a CLI that is about to ``execvp`` itself away is a lease nothing ever
    renews or releases, and it would wedge the very session it was opening. The
    ``claude`` starting a moment later claims it properly through its own
    lifecycle hooks; presence here lapses rather than blocks, as it does
    everywhere else on the board.
    """
    try:
        state = client.session_state(session)
    except httpx.HTTPError as e:
        print(f"{_PROG}: could not read session {session} ({e})", file=err)
        return 1

    holder = _lease_holder(state)
    if holder:
        return _refuse_held(session, holder, err)

    blob, cwd = state.get("latest_blob"), state.get("cwd")
    if not blob:
        print(f"{_PROG}: session has no transcript yet to resume", file=err)
        return 1
    if not cwd:
        print(
            f"{_PROG}: no recorded cwd; cd to the repo and run: claude --resume {session}", file=err
        )
        return 1
    # An absolute path is what gets chdir'd into and what the directory name is
    # derived from; a relative one would mean both against whatever cwd this
    # process happens to have been started in.
    if not Path(cwd).is_absolute():
        print(f"{_PROG}: recorded cwd {cwd!r} is not an absolute path — refusing", file=err)
        return 1

    try:
        target = _transcript_path(session, cwd)
    except ValueError as e:
        print(f"{_PROG}: {e}", file=err)
        return 1

    holder = _held_by(client, session, err)
    if holder:
        return _refuse_held(session, holder, err)
    if not _pull(client, blob, target, err):
        return 1

    if not Path(cwd).is_dir():
        print(
            f"{_PROG}: {cwd} is not on this machine — clone it, then: claude --resume {session}",
            file=err,
        )
        return 1

    holder = _held_by(client, session, err)
    if holder:
        return _refuse_held(session, holder, err)
    try:
        os.chdir(cwd)
        os.execvp("claude", ["claude", "--resume", session])
    except OSError as e:
        print(f"{_PROG}: could not exec claude ({e}); run: claude --resume {session}", file=err)
        return 1
    return 1  # unreachable: a successful execvp has already replaced this process


def _load_tui() -> tuple[type, type]:
    """The full-screen client, imported only when it is going to be used.

    A function rather than an inline import so the failure is something the
    suite can hand a specific ``ImportError`` to: telling "textual is absent"
    from "tui.py is broken" is the whole point of the handler around it.
    """
    from .tui import BoardApp, ResumeRequest

    return BoardApp, ResumeRequest


def _tail_lines(lines: int) -> int:
    """Backlog size, clamped to what the board will actually serve."""
    return min(max(0, lines), _MAX_LINES)


def _follow_only_flags(args: argparse.Namespace) -> list[str]:
    """Which of the tail's options this invocation set, for a run that has no tail.

    Compared against a defaults-only parse rather than against argparse's
    internals, so changing a default here cannot start warning about a flag
    nobody passed.
    """
    defaults = build_parser().parse_args([])
    return [
        flag
        for dest, flag in _FOLLOW_ONLY.items()
        if getattr(args, dest) != getattr(defaults, dest)
    ]


def _resume_since(args: argparse.Namespace, base_url: str, err) -> int | None:
    """The post id to start after: explicit ``--since``, the saved cursor, or none.

    A cursor of 0 is "this client has never run against this board", and passing
    it through as ``since`` would mean ``GET /stream?since=0`` — a replay of the
    board's entire history, which is the opposite of what the flag promises.
    """
    if args.since is not None or not args.resume:
        return args.since
    cursor = read_cursor(base_url)
    if cursor:
        return cursor
    print(
        f"{_PROG}: no cursor recorded for {base_url} yet; showing the backlog instead of\n"
        f"        replaying the whole board.",
        file=err,
    )
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(_strip_verb(list(sys.argv[1:] if argv is None else argv)))
    try:
        cfg = resolve()
    except NoBoardConfigured as e:
        print(f"{_PROG}: {e}", file=sys.stderr)
        return 1

    client = _client(cfg)

    if args.follow:
        if not cfg.authenticated:
            return _report_health(client, cfg, sys.stderr)
        try:
            to = resolve_recipient(client, args.to)
        except RecipientUnresolved as e:
            print(
                f"{_PROG}: could not resolve @me ({e}) — refusing to tail the whole board\n"
                f"        in place of one inbox. Name the recipient explicitly, or retry.",
                file=sys.stderr,
            )
            return 1
        return follow(
            client,
            cfg.base_url,
            since=_resume_since(args, cfg.base_url, sys.stderr),
            tail=_tail_lines(args.lines),
            types=args.types,
            to=to,
            presence=args.presence,
            colour=want_colour() if args.colour is None else args.colour,
        )

    ignored = _follow_only_flags(args)
    if ignored:
        print(
            f"{_PROG}: {', '.join(ignored)} apply to --follow only and were ignored.\n"
            f"        Add --follow, or drive the full-screen client with its own keys.",
            file=sys.stderr,
        )

    try:
        BoardApp, ResumeRequest = _load_tui()
    except ImportError as e:
        # ImportError.name is the module that was actually missing, so a broken
        # import *inside* tui.py surfaces as itself instead of sending the user
        # to install a package they already have.
        if (e.name or "").split(".")[0] != "textual":
            raise
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
