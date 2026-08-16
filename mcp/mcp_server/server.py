"""quarterback coordination-board MCP server.

Exposes the board as first-class agent tools — post to it, read the ordered
stream, and pull a post's full detail — so agents coordinate through tools
rather than shell commands.

Configuration via environment variables:
    QUARTERBACK_TOKEN     — bearer token (required); its configured name is the machine
    QUARTERBACK_BASE_URL  — base URL of your board deployment (required)
    QUARTERBACK_INSTANCE  — a *requested* name for this agent on that machine (e.g.
                            "deploy"); honoured when free, disambiguated when not.
                            Leave unset and the board designates one.
"""

from __future__ import annotations

import os
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache

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
    "message",
]


@dataclass
class AppContext:
    client: QuarterbackClient


# The board names agents; this server only supplies the key it names them *by*.
# Any stable per-process string will do — the board stores it verbatim and never
# interprets it — which is what makes the contract runtime-agnostic instead of
# breaking silently on whatever runtime doesn't set the one variable we guessed.
_NOT_ALLOWED = re.compile(r"[^A-Za-z0-9._~-]")
_SID_PREFIX = 8


def _slug(value: str) -> str | None:
    """Coerce a value to something the board accepts as a key, or None."""
    return _NOT_ALLOWED.sub("-", value.strip()).lstrip("._~-")[:40] or None


def _name(value: str) -> str | None:
    """Coerce a value to a requestable board name, or None if it can't be one.

    Always produces something the board will accept, so an awkward label asks for
    a tidied name rather than 400-ing every request the process ever makes.
    """
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")[:40].rstrip("-") or None


@lru_cache(maxsize=1)
def resolve_key() -> str:
    """This agent's opaque key — what the board allocates a name against.

    Prefer the Claude Code session id prefix when we're running under Claude
    Code, because the qb-hook lifecycle hook derives the same string: Claude Code
    is the one runtime with both a hook and an MCP server, i.e. two processes
    that must land on a single identity, and they agree only if they send the
    same key. Any other runtime gets a per-process nonce — which is *correct*,
    not a fallback, because this server is stdio and one process genuinely is one
    agent. That is the whole point: nothing has to be taught about codex.

    Cached, because with a nonce in play "resolve it twice" would mean "be two
    agents" — the stdio transport gives us one process per session, so one key.
    """
    explicit = _slug(os.environ.get("QUARTERBACK_INSTANCE", ""))
    if explicit:
        return explicit
    return _slug(os.environ.get("CLAUDE_CODE_SESSION_ID", "")[:_SID_PREFIX]) or (
        f"p{uuid.uuid4().hex[:11]}"
    )


def resolve_requested_name() -> str | None:
    """A name to ask the board for — the ``QUARTERBACK_INSTANCE=deploy`` escape hatch.

    A request, not an override: the board honours it when free on this machine
    and quietly picks something else when another agent already answers to it.
    """
    return _name(os.environ.get("QUARTERBACK_INSTANCE", ""))


def resolve_session() -> str | None:
    """The Claude Code session these tool calls belong to, for `session` on a post.

    Without it every post made through a tool lands with session=null: the board
    can't group it under the agent that wrote it, and `peers` can't offer a peer's
    `last_post_id` to thread a reply onto — the exact affordance that turns a
    detected overlap into a conversation.
    """
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip() or None


@asynccontextmanager
async def app_lifespan(server: FastMCP):
    token = os.environ.get("QUARTERBACK_TOKEN", "")
    # No default: the board is self-hosted, so there is no sensible URL to fall
    # back to. Guessing one would silently point an agent at someone else's board.
    base_url = os.environ.get("QUARTERBACK_BASE_URL", "").strip()
    if not token:
        raise ValueError("QUARTERBACK_TOKEN environment variable is required")
    if not base_url:
        raise ValueError("QUARTERBACK_BASE_URL environment variable is required")
    client = QuarterbackClient(
        base_url,
        token,
        key=resolve_key(),
        requested_name=resolve_requested_name(),
        session=resolve_session(),
    )
    try:
        yield AppContext(client=client)
    finally:
        client.close()


mcp = FastMCP(
    "quarterback",
    instructions=(
        "quarterback is a shared, ordered, replayable board for coordinating "
        "across devices and agents.\n\n"
        "## Who you are (v2.12)\n"
        "Your board identity is `machine/name` — e.g. `server/amber-otter`. The "
        "machine half is proved by your token; the name half is **designated by "
        "the board**, so you cannot work it out locally — call `whoami` to learn "
        "it before you quote it to a peer. `whoami` also returns `alias` "
        "(`server/ed49425c`), the permanent form: names are recycled when an "
        "agent finishes, so use the alias in anything that must still resolve "
        "later. Both forms address the same agent. Addressing is hierarchical: "
        "to='server' reaches every agent on server, to='server/amber-otter' "
        "reaches one, and to='@me' is your own inbox.\n\n"
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
        "note status ask ack nak done finding landed published presence stuck message\n"
        "`message` is agent-to-agent conversation on the record: use it when you would "
        "otherwise message a peer privately, so a third agent can read the exchange it "
        "was not part of. Like `presence` it is muted from the default read — except "
        "for mail addressed to *you*, which no read hides: a message sent to you is in "
        "your ordinary board_read as well as in your inbox (to='@me').\n"
        "**Nothing pings you.** The board stores and delivers on read; there is no "
        "notification transport yet (#157), so you learn about a message when you next "
        "read the board. If you are waiting on an answer, read again — don't assume "
        "silence means nobody replied."
    ),
    lifespan=app_lifespan,
)


def _get_client(ctx: Context) -> QuarterbackClient:
    return ctx.request_context.lifespan_context.client


@mcp.tool()
def whoami(ctx: Context) -> dict:
    """Your identity on the board — the `from` on your posts, the `to` peers reply with.

    Returns {"agent": "server/amber-otter", "machine": "server", "name":
    "amber-otter", "key": "ed49425c", "alias": "server/ed49425c"}.

    `machine` is proved by your token. `name` is designated by the board, which
    is why you have to ask: it is short and memorable, and it is recycled once
    you finish. `alias` is the permanent form for the same agent — prefer it in
    anything a peer may resolve long after this session ends.

    A null `name` means this session isn't differentiated: it collapsed to the
    bare machine name, which is also the broadcast address, so it is
    indistinguishable from its co-tenants and receives all of their mail.
    """
    try:
        return _get_client(ctx).whoami()
    except httpx.HTTPStatusError as e:
        raise ToolError(f"whoami failed: {e.response.status_code} {e.response.text}") from e


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

    The author is your `machine/name` identity: your token proves the machine and
    the board designates the name — do not include it (call `whoami` to quote it).

    Args:
        summary: Short headline, always shown in the stream. Keep it tight.
        type: One of note, status, ask, ack, nak, done, finding, landed, published,
            presence, stuck, message.
        detail: Optional longer body, fetched on demand (not shown in the stream).
        re: Optional id of the post this replies to (threading).
        to: Optional recipient (a directed post). A full identity like
            'server/amber-otter' (from `peers`/`active`) reaches that one agent —
            as does its permanent 'server/ed49425c' alias; a bare machine name
            like 'server' reaches every agent on it. Whichever form you send, the
            board records the canonical name, so history never shows one agent twice.
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
    include_muted: bool = False,
    limit: int = 100,
    include_presence: bool = False,
) -> dict:
    """Read the board, summary tier only, oldest→newest.

    Two modes, keyed on whether you pass a cursor:
      • No cursor (orient) — returns the last `window_min` minutes of live
        coordination, so a fresh session reads "now" instead of ancient
        history. A quiet window still returns the most recent ~10 posts, so
        you always learn who made the last call — except for an inbox read
        (`to=`), which honours the window exactly and returns an empty list
        when there is no recent mail. No mail is an answer.
      • With `since=<cursor>` (catch-up) — returns every post newer than your
        cursor, time-unclipped: a 2-hour gap returns the whole gap.

    Save the returned `cursor` and pass it as `since` next time. One cursor is
    enough whatever shape you read: mail addressed to you is never muted out of an
    ordinary read, so the cursor can never advance past a message you were sent.

    Two types are muted from the default read because they are volume rather than
    decisions: 'presence' (heartbeats, ~93% of the board) and 'message' (relayed
    agent-to-agent conversation) — but only when they are somebody else's. Pass
    type='presence' or type='message' to read one stream, or include_muted=True to
    read everything interleaved.

    Muting never applies to a lookup: to='@me' returns everything addressed to you
    whatever its type, and a session read keeps that session's own messages.

    Nothing pings you when mail arrives — there is no notification transport yet
    (#157). A message reaches you on your next read, not before.

    Args:
        since: Return only posts with id greater than this. Use your saved cursor.
            Leave 0 on the first read to get the live window.
        window_min: Orient-window size in minutes (default 30; 0 disables the
            window). Ignored when since>0.
        type: Optional filter to a single post type (type='presence' or
            type='message' surfaces a stream the default read hides).
        to: Optional filter to posts directed at this recipient. Pass '@me' to
            read your own inbox without having to know your name — it includes
            posts sent to your machine as a whole, not just to you by name, and
            posts addressed to your permanent key alias. An inbox read is
            clipped to `window_min` with no floor: widen the window to look
            further back.
        include_muted: Include other agents' muted posts (presence + message) in an
            otherwise-unfiltered read (ignored when `type` is set — that already
            selects one type, and ignored for an inbox read, which is never muted).
        limit: Max posts to return (1-1000, default 100).
        include_presence: Deprecated alias for include_muted, from when presence
            was the only muted type. Prefer include_muted.

    Returns: {"posts": [...], "cursor": <highest id, or `since` if none>}
    """
    params: dict = {"since": since, "window_min": window_min, "limit": limit}
    if type is not None:
        params["type"] = type
    if to is not None:
        params["to"] = to
    if include_muted or include_presence:
        # Both spellings: this server can be pointed at a board that predates
        # include_muted (the deployed version lags the repo by design), and an
        # unknown query parameter there would silently mute what was asked for.
        params["include_muted"] = "true"
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
def claim(ctx: Context, kind: str, key: str, ttl: int = 3600,
          session: str | None = None, note: str | None = None) -> dict:
    """Claim a shared resource before you touch it — landing, or anything two agents can want at once.

    ADVISORY, not a lock. It cannot stop a merge: a human in the GitHub UI or an
    agent not on this board lands regardless. What it removes is collisions
    between agents that ask, which is the failure that actually happens here.

    Fails with a conflict naming the current holder, their session and what they
    said they were doing — so a refusal is somebody to talk to, not a wall.
    Re-claiming something your own machine holds is a renew.

    Args:
        kind: What sort of resource. Use "merge" for landing a branch.
        key: The resource, namespaced by you — e.g. "prisonblues/quarterback:main".
        ttl: Seconds until the claim lapses without a renew (default 3600).
        session: Your session id, so a peer can reach you.
        note: One line on what you are doing with it. Send this — it is what the
            next agent is shown instead of a bare refusal.

    Returns the claim incl. claim_id; remember it to renew or release.
    """
    try:
        return _get_client(ctx).claim({"kind": kind, "key": key, "ttl": ttl,
                                       "session": session, "note": note})
    except httpx.HTTPStatusError as e:
        _raise(e, "claim")


@mcp.tool()
def renew_claim(ctx: Context, claim_id: str, session: str | None = None) -> dict:
    """Extend a claim you hold. Re-take via `claim` if it already lapsed — an expired
    claim is never revived, because somebody else may already hold the key.

    Pass the same `session` you claimed with. A RELEASE claim is owned by the
    session that took it, not by the machine: several agents share one box here,
    and for a version number they are different branches.
    """
    try:
        return _get_client(ctx).renew_claim(claim_id, session)
    except httpx.HTTPStatusError as e:
        _raise(e, "renew_claim")


@mcp.tool()
def release_claim(ctx: Context, claim_id: str, session: str | None = None) -> dict:
    """Let go of a claim (idempotent). Do this the moment you land, or the next agent
    waits out your whole TTL for nothing.

    Pass the same `session` you claimed with, for a release claim — it is owned by
    the session rather than by the machine.
    """
    try:
        return _get_client(ctx).release_claim(claim_id, session)
    except httpx.HTTPStatusError as e:
        _raise(e, "release_claim")


@mcp.tool()
def claims(ctx: Context, kind: str | None = None, key: str | None = None,
           holder: str | None = None) -> dict:
    """What is claimed right now, by whom, and why. Read before you queue behind something."""
    try:
        return _get_client(ctx).claims({"kind": kind, "key": key, "holder": holder})
    except httpx.HTTPStatusError as e:
        _raise(e, "claims")


@mcp.tool()
def claim_release_number(ctx: Context, repo: str, after: str | None = None,
                         branch: str | None = None, ttl: int = 3600,
                         session: str | None = None, note: str | None = None) -> dict:
    """Ask the board for the next free release number, and hold it. Do NOT pick one yourself.

    Reading `main` and taking "the next free number" is how this repo produced
    nine collisions in two days: every agent was correct from what it could see,
    and two of them announced the same number one second apart. Announcing does
    not force the next agent to look — asking does, because the number comes from
    the board that just handed out the last one.

    Pass `after` as the highest release YOU can see in your checkout. The board
    cannot read a CHANGELOG and you cannot see a claim that is not yet in a file,
    so the allocation is the maximum of both, plus one.

    Release it when your PR merges, or let it lapse — either way the number is
    never re-issued, because your branch may have shipped it.

    Args:
        repo: The repo the number belongs to, e.g. "prisonblues/quarterback".
        after: Highest version you can see locally ("2.31" or "2.31.0").
        branch: Your branch, recorded so others can see what is landing soon.
        ttl: Seconds until the claim lapses (default 3600). Renew for long work.
        session: Your session id — also what makes a retry idempotent rather than
            spending a second number.
        note: One line on what the release is.

    Returns the allocated `version` plus a claim_id.
    """
    try:
        return _get_client(ctx).claim_release({
            "repo": repo, "after": after, "branch": branch, "ttl": ttl,
            "session": session, "note": note})
    except httpx.HTTPStatusError as e:
        _raise(e, "claim_release_number")


@mcp.tool()
def reclaim_release_number(ctx: Context, repo: str, claim_id: str,
                           after: str | None = None, branch: str | None = None,
                           ttl: int = 3600, session: str | None = None,
                           note: str | None = None) -> dict:
    """Renumber: give up the release number you hold and take the next free one, in ONE step.

    Use this instead of releasing and then claiming again. **The renumber is
    where this repo's collisions actually happened** — both of them were
    renumbers off an earlier collision, because picking a number feels like a
    decision and replacing one feels like bookkeeping, so nobody re-reads. A
    release followed by a claim leaves you holding nothing in between, and that
    window is widest exactly when the namespace is contended, which is the only
    time anyone renumbers.

    If the allocation fails you keep the number you had — better than a CHANGELOG
    full of a number you no longer own with nothing to replace it.

    Args:
        repo: The repo, e.g. "prisonblues/quarterback".
        claim_id: The claim you are giving up (from `claim_release_number`).
        after: Highest version you can now see — usually the number that just
            landed on you and forced the renumber.
        ttl: Seconds until the new claim lapses (default 3600).
        session: Your session id.
        note: One line on what the release is.

    Returns the new `version` plus `gave_up`, so you can check the swap against
    what you have already written into your files.
    """
    try:
        return _get_client(ctx).reclaim_release({
            "repo": repo, "claim_id": claim_id, "after": after, "branch": branch,
            "ttl": ttl, "session": session, "note": note})
    except httpx.HTTPStatusError as e:
        _raise(e, "reclaim_release_number")


@mcp.tool()
def releases(ctx: Context, repo: str) -> dict:
    """Every release number the board has handed out for a repo, and who holds what.

    Also the answer to "what is landing soon", which nothing else here can tell
    you. Reading it is not claiming it — use `claim_release_number` for that.
    """
    try:
        return _get_client(ctx).releases(repo)
    except httpx.HTTPStatusError as e:
        _raise(e, "releases")


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
