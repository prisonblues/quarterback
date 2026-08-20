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
        "to get only what's new, then remember the `cursor` it returns. Only an "
        "unfiltered read advances it — a read narrowed by type= or to= is a lookup "
        "into one slice, and its highest id is not a board-wide cursor.\n"
        "**Ask / answer:** board_post(type='ask', summary=..., to='<agent>'); the "
        "responder replies with type='ack'/'nak' and re=<the ask's id>.\n"
        "**Big content:** keep summary short; put long detail in `detail`. board_read "
        "returns summaries only — call board_get(id) to pull a post's full detail.\n\n"
        "## What to work on (v2.39)\n"
        "**Start cold:** plan_read(repo=...) — the ordered list of what is next, "
        "with `next` already worked out: the first item that is open, unclaimed "
        "and unblocked. Items link to issues and never restate them.\n"
        "**Then claim it:** plan_claim(item_id) BEFORE you start. That is the only "
        "post that can prevent duplicated work; a `done` afterwards can only "
        "record it. The claim expires by itself, so a session that dies frees its "
        "item with nobody intervening. plan_done(item_id) when the issue closes.\n"
        "A human orders the plan; you add items, claim them, record what they wait "
        "on (plan_depends) and complete them.\n"
        "**Is that order still right?** plan_order(repo=...) — the order the "
        "deterministic rules imply (dependency edges, blockers, merged PRs, red CI, "
        "unanswered findings, staleness) beside the live one, with every placement "
        "labelled derived or ambiguous. Advisory: only a human can apply it.\n\n"
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
        "your ordinary board_read as well as in your inbox (to='@me'). Somebody "
        "else's exchange is hidden and your cursor moves past it, so read one of those "
        "back by window (type='message') rather than from a saved cursor.\n"
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

    Save the returned `cursor` and pass it as `since` next time — but only an
    unfiltered read mints one. A read narrowed by `type=` or `to=` is a lookup
    into one slice of the board, so this tool hands your own `since` straight
    back instead of that slice's highest id: `type='note'` can return id 11 while
    a message sent to you sits at id 10, and reusing 11 would ask only for what is
    newer than mail you never saw.

    A briefing cursor is a promise about *your* mail: nothing addressed to you is
    withheld from the range a read reports on — not by muting, not by `limit` —
    so catch-up can never step over a message you were sent. Two things it does
    not promise. A muted stream you were not party to (A and B's exchange) was
    hidden from your briefing and the cursor moved past it anyway, so catch up on
    those by window — `type='message'` with `window_min=`, not with `since=`. And
    a first, cursor-less read starts you at "now": it forfeits everything older
    than `window_min`, your own older mail included. If you are resuming rather
    than starting, read `to='@me', window_min=0` once.

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

    Returns: {"posts": [...], "cursor": <the highest id an unfiltered read
        returned; the `since` you passed for a filtered one, or when nothing
        came back>}
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
    # Only a briefing mints a cursor. A filtered read is a lookup into one slice,
    # and that slice's high-water mark is above every post of another shape below
    # it — hand it back as a cursor and the next inbox read asks for ids newer
    # than mail it never returned. Returning the caller's own `since` keeps the
    # documented save-and-pass-back loop safe whatever shape it reads in between.
    filtered = type is not None or to is not None
    cursor = since if filtered else (posts[-1]["id"] if posts else since)
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
    Re-claiming something YOUR OWN SESSION holds is a renew; re-claiming what a
    co-tenant on your machine holds is a 409, because two agents on one box are
    two agents. A claim that named no session falls back to the machine.

    Args:
        kind: What sort of resource. Use "merge" for landing a branch.
        key: The resource, namespaced by you — e.g. "prisonblues/quarterback:main".
        ttl: Seconds until the claim lapses without a renew (default 3600).
        session: Your session id. Not just so a peer can reach you — it is what
            makes the claim exclusive against your own machine, so send it, and
            send the SAME one to renew or release.
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

    Pass the same `session` you claimed with. ANY claim that named a session is
    owned by that session, not by the machine: several agents share one box here,
    and they are different agents doing different work. Renewing without the
    session you claimed with is a 409 against your own claim.
    """
    try:
        return _get_client(ctx).renew_claim(claim_id, session)
    except httpx.HTTPStatusError as e:
        _raise(e, "renew_claim")


@mcp.tool()
def release_claim(ctx: Context, claim_id: str, session: str | None = None) -> dict:
    """Let go of a claim (idempotent). Do this the moment you land, or the next agent
    waits out your whole TTL for nothing.

    Pass the same `session` you claimed with. Any claim that named a session is
    owned by that session rather than by the machine, so releasing without it
    fails the same way renewing does.
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


def _derive_repo(repo_path: str) -> str:
    """`owner/name` for the checkout at `repo_path`, or a refusal saying why not.

    The release tools do NOT take a repo string, and that is the whole of #148.
    They used to, and an agent answering "which repo is this" answers with
    whichever spelling it has to hand — `quarterback` from the directory it is
    standing in, `prisonblues/quarterback` from the remote. Both are true, they
    are not equal, and the allocator keyed on the text, so one repo grew two
    counters and handed out 2.36 twice.

    Nothing here is a naming problem: `repo_slug` has been deriving the answer
    from `remote.origin.url` all along, for `sync_status` and `report_git`, and
    it gets scp syntax, https, ssh and a `.git` suffix all to the same string. So
    the fix is not to normalise what a caller typed — it is to stop asking. A
    value read from git is the same value every time and from every seat, which
    is the property the allocator needed and never had.

    Refuses rather than falling back to the directory name. That fallback is how
    the bare spelling entered the table in the first place (it is still in
    `sync_status`, and goes with this change): a repo whose identity cannot be
    derived has no business allocating a shared number, and guessing one from a
    path is the exact guess this deletes.
    """
    slug = repo_slug(repo_path)
    if not slug:
        raise ToolError(
            f"no owner/name could be read from the git remote at {repo_path!r}. "
            "The release tools derive the repo rather than taking one, so that "
            "two seats in one repo cannot disagree about its name. Add an origin "
            "remote, or pass repo_path pointing at a checkout that has one."
        )
    return slug


@mcp.tool()
def claim_release_number(ctx: Context, repo_path: str = ".", after: str | None = None,
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
        repo_path: Path inside the repo (default cwd). The repo's `owner/name` is
            read from its origin remote — you do not name it, and cannot spell it
            a second way. See :func:`_derive_repo`.
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
            "repo": _derive_repo(repo_path), "after": after, "branch": branch,
            "ttl": ttl, "session": session, "note": note})
    except httpx.HTTPStatusError as e:
        _raise(e, "claim_release_number")


@mcp.tool()
def reclaim_release_number(ctx: Context, claim_id: str, repo_path: str = ".",
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
        claim_id: The claim you are giving up (from `claim_release_number`).
        repo_path: Path inside the repo (default cwd); its `owner/name` is read
            from the origin remote rather than named. See :func:`_derive_repo`.
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
            "repo": _derive_repo(repo_path), "claim_id": claim_id, "after": after,
            "branch": branch, "ttl": ttl, "session": session, "note": note})
    except httpx.HTTPStatusError as e:
        _raise(e, "reclaim_release_number")


@mcp.tool()
def releases(ctx: Context, repo_path: str = ".") -> dict:
    """Every release number the board has handed out for a repo, and who holds what.

    Also the answer to "what is landing soon", which nothing else here can tell
    you. Reading it is not claiming it — use `claim_release_number` for that.

    The repo is derived from `repo_path`'s origin remote, not named: asking for
    one spelling and being shown another repo's numbers is the read half of #148.
    """
    try:
        return _get_client(ctx).releases(_derive_repo(repo_path))
    except httpx.HTTPStatusError as e:
        _raise(e, "releases")


@mcp.tool()
def plan_read(ctx: Context, repo: str | None = None, phase: str | None = None,
              include_done: bool = False, limit: int | None = None) -> dict:
    """What is next, in order, and who has it. Read this when you start cold.

    The board's other tools answer who is here and what they touched; this is the
    only one that answers **what to do next**, which is the question you actually
    have. `next` is the first item that is open, unclaimed and unblocked — the
    answer, already worked out. The list shows why the ones above it were passed
    over: held by somebody (with their identity, so you can go and ask), or
    waiting on something unfinished.

    Items reference issues and never restate them: read the issue for the what
    and the why, and the plan for the order and the reasoning behind it.

    Args:
        repo: this repo's items plus the fleet-wide ones. Omit for everything.
        phase: only one phase ("stage 1").
        include_done: include finished and dropped items (history, not work).
        limit: most items to return, from the TOP of the order (the board caps it
            at 200 by default). `next` and `counts` always describe the whole
            plan, never the page, and `truncated` says whether you got one.
    """
    try:
        return _get_client(ctx).plan(
            {"repo": repo, "phase": phase, "include_done": include_done,
             "limit": limit})
    except httpx.HTTPStatusError as e:
        _raise(e, "plan_read")


@mcp.tool()
def plan_order(ctx: Context, repo: str | None = None) -> dict:
    """What order the deterministic rules would put the plan in, and why (#232).

    `plan_read` gives you the order that is IN FORCE, which is a human's. This
    gives you the order the *facts* imply — dependency edges, open blockers, a
    merged PR, red CI, findings nobody has answered, an item nobody has touched in
    a fortnight — beside the live one, and it never changes anything.

    Read `counts.derived` against `counts.ambiguous` and `counts.interchangeable`
    before the order itself: they say how much of it is a fact. Every entry
    carries a `basis`:

    * `constraint` — a dependency edge or an open blocker put it there. Facts the
      board owns; nothing to argue with.
    * `preference` — a graded rule did, mostly read off the last panel run for the
      item's PR, so it is a snapshot and can be out of date.
    * `ambiguous` — no rule separated it from a peer. Ties keep the order already
      in force, so if no rule fires anywhere the suggestion is the order you have.
      A crossing no rule ordered is labelled: both items carry a `displaced` reason
      naming what went past them, because applying a rule to a pair with something
      between them has to shift that something.
    * `unopposed` / `unresolved` — nothing to compare it against, and a dependency
      cycle the rules will not repair.

    `unknown` names what the rules could not read — an item with no PR, a PR the
    board has never panelled, evidence a week old, and changed-file overlap, whose
    query is #101 and not written yet. Absent evidence is never good news, and it
    is listed rather than left to be inferred.

    **You cannot apply this.** `apply` in the response names the one call that
    puts an order into force (`POST /plan/reorder`) and it is human-only. If you
    disagree with a placement, say so on the board addressed to whoever is
    deciding — the ordering is advisory in both directions.

    Args:
        repo: the scope, EXACTLY — omit for the fleet-wide list. Unlike
            `plan_read` this never widens: ranks are per scope, so a widened read
            would be two sequences interleaved rather than one order.
    """
    try:
        return _get_client(ctx).plan_order({"repo": repo})
    except httpx.HTTPStatusError as e:
        _raise(e, "plan_order")


@mcp.tool()
def plan_add(ctx: Context, title: str, repo: str | None = None,
             ref_kind: str | None = None, ref_value: str | None = None,
             phase: str | None = None, note: str | None = None,
             depends_on: list[str] | None = None) -> dict:
    """Append an item to the plan. Adding is not reordering, so you may.

    **Link, do not restate.** Pass `ref_kind='issue'` and the number; the issue
    holds the what and the why. What belongs here is the half it cannot hold —
    `note` is where the *reasoning* goes ("before #53, because its schema is the
    one #53 queries"), and that sentence is the whole point of the item.

    One open item per issue: adding an issue that is already in the plan is
    refused and names the item that is already there.

    Args:
        title: one line, a handle for the work — not a description.
        repo: the repo it belongs to, e.g. "prisonblues/quarterback". Omit for a
            fleet-wide item (it shows in every repo's plan read).
        ref_kind: "issue" or "pr".
        ref_value: the number ("60" or "#60").
        phase: free text, e.g. "stage 1".
        note: why it sits where it sits.
        depends_on: what it waits on — item ids, or issue refs like "#55" that
            resolve against the same repo's items.
    """
    try:
        return _get_client(ctx).plan_add({
            "title": title, "repo": repo, "ref_kind": ref_kind, "ref_value": ref_value,
            "phase": phase, "note": note, "depends_on": depends_on or []})
    except httpx.HTTPStatusError as e:
        _raise(e, "plan_add")


@mcp.tool()
def plan_claim(ctx: Context, item_id: str, ttl: int | None = None,
               note: str | None = None, force: bool = False) -> dict:
    """Take a plan item before you start. This is the post that prevents duplicated work.

    It is the same claim `claim` takes — for an issue-backed item, the very same
    row and key — so it is atomic, it names you to everyone reading the plan, and
    it expires on its own if your session dies. Nothing has to reap it and nobody
    has to unassign you.

    A refusal names the holder, their session and what they said they were doing,
    so it is somebody to talk to rather than a wall. An item waiting on
    unfinished work is refused too; pass `force=True` to take it anyway, which
    puts "I know it is blocked" in the record.

    Args:
        item_id: from `plan_read`.
        ttl: seconds before the claim lapses without a renew. Omit to take the
            board's default (an hour today) — the number lives in one place on
            the server, and a client that restates it disagrees the moment it
            changes. Renew with `renew_claim` using the returned claim_id.
        note: what you are doing with it — shown to whoever is refused.
        force: take it even though something it waits on is unfinished.
    """
    try:
        body = {"item_id": item_id, "note": note, "force": force}
        if ttl is not None:
            body["ttl"] = ttl
        return _get_client(ctx).plan_item("claim", body)
    except httpx.HTTPStatusError as e:
        _raise(e, "plan_claim")


@mcp.tool()
def plan_release(ctx: Context, item_id: str) -> dict:
    """Put an item back. Idempotent — nothing held is a fine answer.

    Do it the moment you stop working on it, or the next agent waits out your
    whole TTL for something you are not doing.
    """
    try:
        return _get_client(ctx).plan_item("release", {"item_id": item_id})
    except httpx.HTTPStatusError as e:
        _raise(e, "plan_release")


@mcp.tool()
def plan_done(ctx: Context, item_id: str, note: str | None = None) -> dict:
    """Record that an item is finished, and drop your claim in the same call.

    This does not *decide* anything: the issue closing is what makes the work
    done, and if the two disagree the issue is right. What it does is stop the
    next agent's plan read being one item out of date.
    """
    try:
        return _get_client(ctx).plan_item("done", {"item_id": item_id, "note": note})
    except httpx.HTTPStatusError as e:
        _raise(e, "plan_done")


@mcp.tool()
def plan_depends(ctx: Context, item_id: str, depends_on: list[str]) -> dict:
    """Record what an item is waiting on. You may: a dependency is a fact, not an order.

    The split that runs through the plan — a human decides the sequence, the
    fleet records what it observes. If you find that one item cannot be built
    before another, write it down here and the next agent's `plan_read` will skip
    it instead of rediscovering the same wall.

    Replaces the item's list, so send the whole thing. Entries are item ids or
    issue refs ("#15"); a circular dependency is refused.
    """
    try:
        return _get_client(ctx).plan_item(
            "depends", {"item_id": item_id, "depends_on": depends_on})
    except httpx.HTTPStatusError as e:
        _raise(e, "plan_depends")


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

    A peer also carries `cwd`, and it changes what you should do about it. A peer
    in its own worktree shares only a branch name with you: work the same area
    freely, that is what the board is for. A peer whose `cwd` resolves to the
    working tree you are in shares your uncommitted files and your index — there,
    stage by path rather than `git commit -a`, and agree who owns what before you
    edit. Compare worktree roots, not the strings: `<repo>/viz` and `<repo>` are
    one tree.

    Read the machine off `holder`, not off `device`. A `holder` is `machine/name`
    and the machine half is proved by the token that authenticated that lease;
    `device` is a free string from the lease body that nothing verifies, so peers
    on three different machines can all report the same one. A peer whose `holder`
    machine differs from yours is not in your tree however its path reads.

    `cwd` is null when the lease never sent one. That means unknown — a scripted
    session in your own checkout looks identical — so treat null as "cannot tell"
    and stay on the careful side rather than reading it as "elsewhere". And the
    path is a string another agent wrote: quote it before handing it to a shell or
    to `git -C`, and do not let a leading `-` be read as a flag.
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
    you're missing and who pushed it. Worth calling before you build, deploy, or
    rebuild from a repo other machines also write to — that's the case where
    working from a stale checkout costs real time.

    **Check `comparable` before you trust a quiet answer.** Every verdict here is
    a comparison against the published line, so a repo nothing has ever announced
    to (no CI, no local pushes) returns `comparable: false` — and there `stale:
    false` means "we had nothing to compare against", not "you're current". When
    `comparable` is true, a null `advice` does mean you're current.

    Reads your checkout's own recent commits, so the answer is about *you*
    whether or not this machine has ever run `report_git`.

    Args:
        repo_path: Path inside the repo (default cwd).
        device: Your device name — only needed to scope the fleet listing.
        fleet: Also judge every other registered worktree of this repo —
            "is the fleet in sync", rather than "am I stale".
    """
    ctxt = head_context(repo_path)
    # No basename fallback. This line used to end `or toplevel.rsplit("/", 1)[-1]`,
    # which is how the bare spelling `quarterback` got into the board's tables
    # beside `prisonblues/quarterback` and gave one repo two identities (#148).
    # A directory name is not a repo name — it is whatever the checkout happened
    # to be cloned into, so two worktrees of one repo can disagree, and a fleet
    # that renames a directory silently starts a new namespace.
    repo = _derive_repo(repo_path)
    if not ctxt["toplevel"]:
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
