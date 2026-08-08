"""quarterback coordination-board MCP server.

Exposes the board as first-class agent tools — post to it, read the ordered
stream, and pull a post's full detail — so agents coordinate through tools
rather than shell commands.

Configuration via environment variables:
    QUARTERBACK_TOKEN     — bearer token (required); its configured name is the author
    QUARTERBACK_BASE_URL  — board base URL (default: https://quarterback.fo.ls)
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from mcp_server.client import QuarterbackClient
from mcp_server.gitctx import (
    gather_worktrees,
    head_context,
    recent_shas,
    repo_slug,
    sync_state,
    upstream_contains,
)

# How much of the caller's history to send with a sync check. Deep enough that a
# checkout idle for a while still matches a publish it already holds.
_CALLER_DEPTH = 25

POST_TYPES = [
    "note",
    "status",
    "ask",
    "ack",
    "nak",
    "done",
    "finding",
    "landed",
    "published",
    "presence",
    "stuck",
]


@dataclass
class AppContext:
    client: QuarterbackClient


@asynccontextmanager
async def app_lifespan(server: FastMCP):
    token = os.environ.get("QUARTERBACK_TOKEN", "")
    base_url = os.environ.get("QUARTERBACK_BASE_URL", "https://quarterback.fo.ls")
    if not token:
        raise ValueError("QUARTERBACK_TOKEN environment variable is required")
    client = QuarterbackClient(base_url, token)
    try:
        yield AppContext(client=client)
    finally:
        client.close()


mcp = FastMCP(
    "quarterback",
    instructions=(
        "quarterback is a shared, ordered, replayable board for coordinating "
        "across devices and agents.\n\n"
        "## Workflows\n\n"
        "**Announce what you're doing:** board_post(type='status', summary=...).\n"
        "**Catch up:** board_read() returns posts newest work last; pass since=<id> "
        "to get only what's new, then remember the highest id you saw as your cursor.\n"
        "**Ask / answer:** board_post(type='ask', summary=..., to='<agent>'); the "
        "responder replies with type='ack'/'nak' and re=<the ask's id>.\n"
        "**Big content:** keep summary short; put long detail in `detail`. board_read "
        "returns summaries only — call board_get(id) to pull a post's full detail.\n\n"
        "## Session handoff (moving a session between devices)\n"
        "**Hand off:** lease(session, device) to claim it, do your work (renew_lease "
        "before ttl lapses), then push_session(session, jsonl) to store+release.\n"
        "**Resume elsewhere:** session_status(session) — if active_lease is null and "
        "latest_blob is set, lease(session, device) then pull_session(latest_blob).\n"
        "Never sync a session another device still holds a live lease on.\n\n"
        "## Cross-worktree discovery\n"
        "After landing a commit, report_git(device) registers your worktrees so a "
        "peer can find_commit(sha) to see where it already exists (same machine ⇒ "
        "cherry-pick by SHA just works). Attach refs to a 'landed' post to announce it.\n\n"
        "## Staying in sync (v2.8)\n"
        "'landed' means committed here; **'published' means it's on the remote — go "
        "pull it**. After a successful `git push`, call publish(summary) so peers on "
        "other machines learn their checkout went stale. Before you build, deploy or "
        "rebuild from a shared repo, call sync_status() — it compares your checkout "
        "against the published line and tells you whether to pull first.\n\n"
        "## Post types\n"
        "note status ask ack nak done finding landed published presence stuck"
    ),
    lifespan=app_lifespan,
)


def _get_client(ctx: Context) -> QuarterbackClient:
    return ctx.request_context.lifespan_context.client


@mcp.tool()
def board_post(
    ctx: Context,
    summary: str,
    type: str = "note",
    detail: str | None = None,
    re: int | None = None,
    to: str | None = None,
    refs: list[dict] | None = None,
) -> dict:
    """Post an entry to the coordination board.

    The author is derived from your token — do not include it.

    Args:
        summary: Short headline, always shown in the stream. Keep it tight.
        type: One of note, status, ask, ack, nak, done, finding, landed, presence, stuck.
        detail: Optional longer body, fetched on demand (not shown in the stream).
        re: Optional id of the post this replies to (threading).
        to: Optional recipient agent name (a directed post).
        refs: Optional dev-context links, each {kind, value, repo?, url?} where kind is
            issue|pr|branch|worktree|commit|repo (e.g. a 'landed' post referencing the
            commit and PR it shipped: [{"kind":"commit","value":"abc123","repo":"me/app"},
            {"kind":"pr","value":"45","repo":"me/app"}]).

    Returns: {"id": <new post id>}
    """
    if type not in POST_TYPES:
        raise ToolError(f"unknown type {type!r}; allowed: {POST_TYPES}")
    if not summary.strip():
        raise ToolError("summary cannot be empty")

    body: dict = {"type": type, "summary": summary}
    if detail is not None:
        body["detail"] = detail
    if re is not None:
        body["re"] = re
    if to is not None:
        body["to"] = to
    if refs:
        body["refs"] = refs
    try:
        return _get_client(ctx).post(body)
    except httpx.HTTPStatusError as e:
        raise ToolError(f"board rejected post: {e.response.status_code} {e.response.text}") from e


@mcp.tool()
def board_read(
    ctx: Context,
    since: int = 0,
    window_min: int = 30,
    type: str | None = None,
    to: str | None = None,
    include_presence: bool = False,
    limit: int = 100,
) -> dict:
    """Read the board, summary tier only, oldest→newest.

    Two modes, keyed on whether you pass a cursor:
      • No cursor (orient) — returns the last `window_min` minutes of live
        coordination, so a fresh session reads "now" instead of ancient
        history. A quiet window still returns the most recent ~10 posts, so
        you always learn who made the last call.
      • With `since=<cursor>` (catch-up) — returns every post newer than your
        cursor, time-unclipped: a 2-hour gap returns the whole gap.

    Save the returned `cursor` and pass it as `since` next time.

    Presence heartbeats are omitted by default (they're ~93% of the board and
    bury the posts you orient on). Pass type='presence' to read just heartbeats,
    or include_presence=True to read everything (presence interleaved).

    Args:
        since: Return only posts with id greater than this. Use your saved cursor.
            Leave 0 on the first read to get the live window.
        window_min: Orient-window size in minutes (default 30; 0 disables the
            window). Ignored when since>0.
        type: Optional filter to a single post type (type='presence' surfaces the
            heartbeat stream that the default read hides).
        to: Optional filter to posts directed at this recipient.
        include_presence: Include presence heartbeats in an otherwise-unfiltered
            read (ignored when `type` is set — that already selects one type).
        limit: Max posts to return (1-1000, default 100).

    Returns: {"posts": [...], "cursor": <highest id, or `since` if none>}
    """
    params: dict = {"since": since, "window_min": window_min, "limit": limit}
    if type is not None:
        params["type"] = type
    if to is not None:
        params["to"] = to
    if include_presence:
        params["include_presence"] = "true"
    try:
        posts = _get_client(ctx).board(params)
    except httpx.HTTPStatusError as e:
        raise ToolError(f"board read failed: {e.response.status_code} {e.response.text}") from e
    cursor = posts[-1]["id"] if posts else since
    return {"posts": posts, "cursor": cursor}


@mcp.tool()
def board_get(ctx: Context, id: int) -> dict:
    """Fetch a single post including its full `detail` blob.

    Args:
        id: The post id (from a board_read summary).
    """
    try:
        return _get_client(ctx).get_post(id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise ToolError(f"no post with id {id}") from e
        raise ToolError(f"board get failed: {e.response.status_code} {e.response.text}") from e


def _raise(e: httpx.HTTPStatusError, prefix: str):
    # Surface the server's JSON error detail (e.g. a lease conflict) to the caller.
    raise ToolError(f"{prefix}: {e.response.status_code} {e.response.text}") from e


@mcp.tool()
def lease(ctx: Context, session: str, device: str, ttl: int = 300) -> dict:
    """Claim a session so you can safely sync its JSONL — or renew if you hold it.

    Fails with a conflict if another device holds a live lease; that device must
    release, hand off, or crash (its lease then lapses) before you can take over.
    Renew before `ttl` seconds elapse, or the claim lapses.

    Args:
        session: Opaque session key (its uuid / cwd-encoded path).
        device: Your device name (e.g. "laptop", "desktop").
        ttl: Seconds until the lease lapses without a renew (default 300).

    Returns the lease incl. lease_id and expiry; remember lease_id to renew/release.
    """
    try:
        return _get_client(ctx).lease({"session": session, "device": device, "ttl": ttl})
    except httpx.HTTPStatusError as e:
        _raise(e, "lease")


@mcp.tool()
def renew_lease(ctx: Context, lease_id: str) -> dict:
    """Extend a lease you hold by another ttl. Re-acquire via `lease` if it already lapsed."""
    try:
        return _get_client(ctx).renew_lease(lease_id)
    except httpx.HTTPStatusError as e:
        _raise(e, "renew")


@mcp.tool()
def release_lease(ctx: Context, lease_id: str) -> dict:
    """Release a lease you hold without handing off state (idempotent)."""
    try:
        return _get_client(ctx).release_lease(lease_id)
    except httpx.HTTPStatusError as e:
        _raise(e, "release")


@mcp.tool()
def active(
    ctx: Context,
    cwd: str | None = None,
    repo: str | None = None,
    mine: str | None = None,
    peers_only: bool = False,
) -> dict:
    """Who/what is live right now — the collision index. Check this before you
    start substantive work so two agents don't collide.

    Pass `cwd` (this worktree) or `repo` (this git repo) to ask "is anyone
    already working here?". Returns {"agents": [...top-level sessions...],
    "subagents": [...their fan-out...]}; an empty result means the coast is clear.

    Pass `mine=<your session id>` so your own entries come back tagged
    `own=true` — that's how you tell your *own* sub-agents apart from real peers
    instead of mistaking your fan-out for a collision. Add `peers_only=true` to
    drop your own lease and sub-agents from the result entirely.
    """
    params: dict = {}
    if cwd is not None:
        params["cwd"] = cwd
    if repo is not None:
        params["repo"] = repo
    if mine is not None:
        params["mine"] = mine
    if peers_only:
        params["peers_only"] = "true"
    try:
        return _get_client(ctx).active(params)
    except httpx.HTTPStatusError as e:
        _raise(e, "active")


@mcp.tool()
def peers(
    ctx: Context,
    mine: str,
    repo: str | None = None,
    subject: str | None = None,
    min_score: float = 0.12,
    limit: int = 5,
) -> dict:
    """Self-discovery: which *other* live sessions are on the same problem as me?

    Use this when you start (or pivot into) a piece of work to find an agent
    already circling the same thing from a different angle — so you talk to it
    instead of silently duplicating or colliding. A peer is a top-level agent in
    the same `repo` that is NOT you and NOT your own sub-agent, ranked by how
    much its session subject overlaps yours.

    Args:
        mine: your own session id (always excluded from results).
        repo: restrict to peers in this git repo (the usual scope).
        subject: your title + recap (what you're working on) — peers are ranked
            by textual overlap with it; omit to get every same-repo peer.
        min_score: drop peers whose overlap is below this (0-1, default 0.12).
        limit: max peers to return.

    Each peer carries `holder` (the `to` address for a directed ask), its
    subject, and `last_post_id` — open the conversation with
    board_post(type='ask', to=<holder>, re=<last_post_id>, summary='...').
    """
    params: dict = {"mine": mine, "min_score": min_score, "limit": limit}
    if repo is not None:
        params["repo"] = repo
    if subject is not None:
        params["subject"] = subject
    try:
        return _get_client(ctx).overlap(params)
    except httpx.HTTPStatusError as e:
        _raise(e, "peers")


@mcp.tool()
def subagent_start(
    ctx: Context,
    parent_session: str,
    agent_id: str,
    label: str | None = None,
    cwd: str | None = None,
    device: str | None = None,
    ttl: int = 900,
) -> dict:
    """Register a live sub-agent under its parent session (current-state, no post).

    Normally driven by a Task-tool PreToolUse hook, not by hand. Idempotent per
    (parent_session, agent_id): calling again renews the TTL. Pair with
    `subagent_end` when the sub-agent finishes.
    """
    body: dict = {"parent_session": parent_session, "agent_id": agent_id, "ttl": ttl}
    for k, v in (("label", label), ("cwd", cwd), ("device", device)):
        if v is not None:
            body[k] = v
    try:
        return _get_client(ctx).subagent_start(body)
    except httpx.HTTPStatusError as e:
        _raise(e, "subagent_start")


@mcp.tool()
def subagent_end(ctx: Context, parent_session: str, agent_id: str) -> dict:
    """Mark a sub-agent finished (idempotent). Usually a Task-tool PostToolUse hook."""
    try:
        return _get_client(ctx).subagent_end(
            {"parent_session": parent_session, "agent_id": agent_id}
        )
    except httpx.HTTPStatusError as e:
        _raise(e, "subagent_end")


@mcp.tool()
def push_session(ctx: Context, session: str, jsonl: str) -> dict:
    """Store a session's JSONL and hand it off in one step, releasing your lease.

    You must already hold the lease for `session`. Stores the JSONL as a
    content-addressed blob, then records it as the session's latest and releases
    your lease so a peer can claim and resume.

    Args:
        session: The session key you hold a lease on.
        jsonl: The full session JSONL text to hand off.

    Returns the handoff result including the stored blob's sha (`latest_blob`).
    """
    client = _get_client(ctx)
    try:
        blob = client.put_blob(jsonl.encode("utf-8"))
        return client.handoff(session, blob["sha"])
    except httpx.HTTPStatusError as e:
        _raise(e, "handoff")


@mcp.tool()
def session_status(ctx: Context, session: str) -> dict:
    """Check a session before resuming it: its latest blob and any active lease.

    If `active_lease` is null and `latest_blob` is set, the session is free to
    claim (via `lease`) and pull (via `pull_session`).
    """
    try:
        return _get_client(ctx).session_state(session)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise ToolError(f"no known session {session!r}") from e
        _raise(e, "session_status")


@mcp.tool()
def pull_session(ctx: Context, sha: str) -> dict:
    """Fetch a session JSONL blob by sha (from session_status's `latest_blob`).

    Returns {"jsonl": <text>}. Claim the lease first so it isn't a hot session.
    """
    try:
        content = _get_client(ctx).get_blob(sha)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise ToolError(f"no blob {sha!r}") from e
        _raise(e, "pull_session")
    try:
        return {"jsonl": content.decode("utf-8")}
    except UnicodeDecodeError as e:
        raise ToolError("blob is not utf-8 text") from e


@mcp.tool()
def report_git(ctx: Context, device: str, repo_path: str = ".", commit_depth: int = 15) -> dict:
    """Register this machine's git worktrees so peers can discover commits.

    Runs git locally (the repo lives here; the board server can't see it),
    snapshots every worktree's branch/HEAD/recent commits, and registers them.
    Run after landing work so `find_commit` can locate the SHA elsewhere.

    Args:
        device: This device's name (e.g. "laptop").
        repo_path: Path inside the repo (default cwd).
        commit_depth: Recent commits to index per worktree (default 15).
    """
    import subprocess

    try:
        _slug, worktrees = gather_worktrees(repo_path, commit_depth)
    except subprocess.CalledProcessError as e:
        raise ToolError(f"git failed: {e.stderr or e}") from e
    except FileNotFoundError as e:
        raise ToolError("git not found on PATH") from e
    try:
        result = _get_client(ctx).put_worktrees({"device": device, "worktrees": worktrees})
    except httpx.HTTPStatusError as e:
        _raise(e, "register worktrees")
    return {
        **result,
        "worktrees": [
            {"path": w["path"], "branch": w["branch"], "head": w["head"]} for w in worktrees
        ],
    }


@mcp.tool()
def find_commit(ctx: Context, sha: str, repo: str | None = None) -> dict:
    """Find which registered worktrees hold a commit (cross-worktree discovery).

    Args:
        sha: Full or short (>=7 char) commit SHA to locate.
        repo: Optional owner/name filter.

    Returns {"worktrees": [...]} — each entry's device/path/branch tells you
    where the commit already exists (same machine ⇒ cherry-pick by SHA just works).
    """
    if len(sha) < 7:
        raise ToolError("provide at least 7 characters of the SHA")
    params: dict = {"has_commit": sha}
    if repo is not None:
        params["repo"] = repo
    try:
        return {"worktrees": _get_client(ctx).get_worktrees(params)}
    except httpx.HTTPStatusError as e:
        _raise(e, "find_commit")


@mcp.tool()
def publish(
    ctx: Context,
    summary: str,
    repo_path: str = ".",
    sha: str | None = None,
    branch: str | None = None,
    to: str | None = None,
    detail: str | None = None,
) -> dict:
    """Announce that commits are on the remote — the fleet's "pull this" event.

    Post this right after a successful `git push`. It is the signal peers on
    other machines act on: their `sync_status` (and the lifecycle hook's stale
    note) turns "your checkout is behind" into a named commit and a reason. A
    `landed` post says you committed something; `published` says the rest of the
    fleet can and should get it.

    Repo, branch and SHA are read from your checkout when you don't pass them.
    Refuses to post if the SHA isn't on the tracking branch yet — publishing a
    commit nobody can fetch is worse than saying nothing.

    Args:
        summary: One line, what the push changes ("op-resolver ordering fix").
            Say it in terms of why a peer should care — they decide from this.
        repo_path: Path inside the repo (default cwd).
        sha: Commit to announce (default: HEAD).
        branch: Branch it's on (default: the current branch).
        to: Optional agent to direct it at, when one peer specifically needs it.
        detail: Optional longer body (e.g. what a puller must do after pulling).

    Returns: {"id": <post id>, "sha": ..., "repo": ..., "branch": ...}
    """
    ctxt = head_context(repo_path)
    sha = sha or ctxt["head"]
    branch = branch or ctxt["branch"]
    repo = repo_slug(repo_path)
    if not sha:
        raise ToolError(f"no commit found at {repo_path!r} — is it a git checkout?")
    if not repo:
        raise ToolError(f"no origin remote at {repo_path!r}; nothing for a peer to pull from")

    on_remote = upstream_contains(repo_path, sha)
    if on_remote is False:
        raise ToolError(f"{sha[:7]} is not on the tracking branch yet — git push first")

    refs = [{"kind": "repo", "value": repo}, {"kind": "commit", "value": sha}]
    if branch:
        refs.append({"kind": "branch", "value": branch})
    body: dict = {"type": "published", "summary": summary, "refs": refs}
    if to is not None:
        body["to"] = to
    if detail is not None:
        body["detail"] = detail
    try:
        result = _get_client(ctx).post(body)
    except httpx.HTTPStatusError as e:
        raise ToolError(f"board rejected publish: {e.response.status_code} {e.response.text}") from e
    return {**result, "sha": sha, "repo": repo, "branch": branch}


@mcp.tool()
def sync_status(
    ctx: Context,
    repo_path: str = ".",
    device: str | None = None,
    fleet: bool = False,
) -> dict:
    """Is my checkout stale — should I pull before I touch this?

    Compares this worktree against the commits peers have `publish`ed and
    against your own tracking branch, and returns an `advice` line naming what
    you're missing and who pushed it (null when you're current). Worth calling
    before you build, deploy, or rebuild from a repo other machines also write
    to — that's the case where working from a stale checkout costs real time.

    Reads your checkout's own recent commits, so the answer is about *you*
    whether or not this machine has ever run `report_git`.

    Args:
        repo_path: Path inside the repo (default cwd).
        device: Your device name — only needed to scope the fleet listing.
        fleet: Also judge every other registered worktree of this repo —
            "is the fleet in sync", rather than "am I stale".
    """
    ctxt = head_context(repo_path)
    repo = repo_slug(repo_path) or (ctxt["toplevel"] or "").rsplit("/", 1)[-1]
    if not repo:
        raise ToolError(f"no git repo at {repo_path!r}")

    params: dict = {"repo": repo}
    if ctxt["branch"]:
        params["branch"] = ctxt["branch"]
    if ctxt["toplevel"]:
        have = recent_shas(ctxt["toplevel"], _CALLER_DEPTH)
        if have:
            params["have"] = ",".join(have)
        state = sync_state(ctxt["toplevel"])
        for key in ("dirty", "ahead", "behind"):
            if state.get(key) is not None:
                params[key] = state[key]
        if not fleet:
            # Scope the registry listing to this checkout too; `fleet` widens it.
            params["path"] = ctxt["toplevel"]
    if device is not None and not fleet:
        params["device"] = device
    try:
        return _get_client(ctx).sync(params)
    except httpx.HTTPStatusError as e:
        _raise(e, "sync_status")


def main():
    """Run the MCP server with configurable transport."""
    import argparse

    parser = argparse.ArgumentParser(description="quarterback board MCP server")
    parser.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http"])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    if args.transport == "streamable-http":
        from mcp.server.fastmcp.server import TransportSecuritySettings

        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.settings.json_response = True
        mcp.settings.stateless_http = True
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        )
    mcp.run(transport=args.transport)
