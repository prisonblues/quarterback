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
        "and unblocked. Items link to issues and never restate them. Check "
        "`next.caveat`: it is set when part of the order is just the order things "
        "were added, and then the rank is not a priority.\n"
        "**Then claim it:** plan_claim(item_id) BEFORE you start. That is the only "
        "post that can prevent duplicated work; a `done` afterwards can only "
        "record it. The claim expires by itself, so a session that dies frees its "
        "item with nobody intervening. plan_done(item_id) when the issue closes.\n"
        "A human orders the plan; you add items — with plan_add(after=/before=) "
        "when you are writing down a position you were GIVEN, since placing a new "
        "item reorders nothing — claim them, record what they wait on "
        "(plan_depends) and complete them.\n"
        "**A scope is not always a repo.** Most plan items belong to a "
        "GitHub repo, spelled `owner/name`. Some belong to a `project:<name>` "
        "scope — real work with no repo behind it, so no issues, no PRs, no CI. "
        "`plan_read` returns the declared ones in `scopes`; name one exactly as it "
        "is spelled there. You cannot create one: a PERSON declares a scope, "
        "because a scope invented from a typo is a second name for work that "
        "already has one. A misspelled repo is still refused as a repo.\n"
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


#: The reasons `end_session` will send. Stated here as well as on the board so a
#: caller reads the vocabulary in the tool that takes it — and short, because the
#: board refuses anything outside it with a 422 rather than storing a sixth
#: spelling of "finished".
END_REASONS = ("finished", "killed", "timed_out", "context_reset", "superseded")


@mcp.tool()
def end_session(ctx: Context, session: str, reason: str = "finished") -> dict:
    """End a session cleanly: hand back its claims and its lease, and say why.

    The verb the fleet did not have. Without it the only thing that ever freed a
    finished agent's work was the TTL, so a claim outlived the conversation that
    took it and the board could not tell a session that finished from one that
    merely stopped answering — an expired lease says "nobody renewed", which is
    the identical row whether the work landed, the pane was closed, or the agent
    is thinking hard.

    Call it when a session is over and the harness did not do it for you: a
    conversation you are abandoning, a peer's pane you have just closed, a run
    you are superseding. Under Claude Code the SessionEnd hook already calls this
    for the ordinary endings, so you rarely need to end YOURSELF.

    It records; it does not operate. Nothing is signalled and no pane is closed —
    whatever stopped the session is what calls this.

    Idempotent: ending an already-ended session is a fine answer, not an error.
    `ended` says whether this call was the one that released a live lease, and
    `lease_was` says what it found instead.

    Args:
        session: the session key — the Claude Code session id, the same string
            its lease and its claims were taken with.
        reason: why it stopped. One of: finished (it said so), killed (something
            closed it), timed_out (a bounded run hit its ceiling), context_reset
            (/clear or /new — the pane lives on, this conversation does not),
            superseded (another session took the work over). Anything else is
            refused: this is read as a word in a fleet view, and a sixth spelling
            of "finished" reaches a human as an unknown.
    """
    if reason not in END_REASONS:
        raise ToolError(
            f"reason={reason!r} is not one of: {', '.join(END_REASONS)}. Say which "
            "of those it was — the point of the field is that a reader can branch "
            "on it, and free text cannot be branched on.")
    try:
        return _get_client(ctx).end_session(session, reason)
    except httpx.HTTPStatusError as e:
        _raise(e, "end_session")


@mcp.tool()
def claim(ctx: Context, ref_kind: str | None = None, ref_value: str | None = None,
          repo_path: str = ".", kind: str | None = None, key: str | None = None,
          ttl: int = 3600, session: str | None = None, note: str | None = None) -> dict:
    """Claim what you are about to work on, BEFORE you start. This is the post that prevents duplicated work.

    Say WHICH resource and the board works out the key: `ref_kind='issue'`,
    `ref_value='172'`, and the repo is read from the checkout at `repo_path`. Do
    not compose a key by hand — that is #172. Two agents describing one collision
    produced two different keys ("issue/<repo>#163" and "work/<repo>#163"), the
    plan and the claims table then reported different answers about the same issue
    in the same second, and nobody could tell who held what.

    ADVISORY, not a lock. It cannot stop a merge: a human in the GitHub UI or an
    agent not on this board lands regardless. What it removes is collisions
    between agents that ask, which is the failure that actually happens here.

    Fails with a conflict naming the current holder, their session and what they
    said they were doing — so a refusal is somebody to talk to, not a wall.
    Re-claiming something YOUR OWN SESSION holds is a renew; re-claiming what a
    co-tenant on your machine holds is a 409, because two agents on one box are
    two agents. A claim that named no session falls back to the machine.

    Args:
        ref_kind: what sort of resource — "issue", "pr", "branch", "plan" or
            "item". This is the preferred way in.
        ref_value: the issue number, PR number, branch name or board id.
        repo_path: the checkout whose origin remote names the repo. Used for
            issue / pr / branch refs; ignored for plan and item ids, which are
            already globally unique.
        kind: the OLD way — a kind you compose yourself. Still accepted and
            canonicalised onto the derived key, and the answer says so. Never
            together with `ref_kind`: a request describing two resources is
            refused rather than guessed at, which is what `POST /claim` does too.
        key: the composed key, with `kind`.
        ttl: Seconds until the claim lapses without a renew (default 3600).
        session: Your session id. Not just so a peer can reach you — it is what
            makes the claim exclusive against your own machine, so send it, and
            send the SAME one to renew or release.
        note: One line on what you are doing with it. Send this — it is what the
            next agent is shown instead of a bare refusal.

    Returns the claim incl. claim_id; remember it to renew or release.
    """
    body: dict = {"ttl": ttl, "session": session, "note": note}
    if ref_kind and (kind or key):
        # The same refusal `ClaimIn` makes, made here so the tool cannot be the
        # softer door onto it. Preferring the ref silently was worse than either
        # answer: a request describing two resources is a caller with two ideas
        # about what it is claiming, and the one that gets dropped is the one it
        # will believe it holds — #172 with the parties swapped again.
        raise ToolError(
            "say it once: ref_kind + ref_value (the board derives the key), or "
            f"kind + key if you already have a composed one — not both. You sent "
            f"ref_kind={ref_kind!r} and kind/key={(kind, key)!r}; guessing which "
            "one you meant is how a claim lands on the wrong resource.")
    if ref_kind:
        if not ref_value:
            raise ToolError("ref_kind needs ref_value: which issue, PR, branch or id?")
        ref: dict = {"kind": ref_kind, "value": str(ref_value)}
        # A plan or an item id is globally unique already, so sending a repo
        # alongside would give one row two keys depending on the caller's cwd.
        if ref_kind.lower() not in ("plan", "item"):
            ref["repo"] = _derive_repo(repo_path)
        body["ref"] = ref
    elif kind and key:
        body["kind"], body["key"] = kind, key
    else:
        raise ToolError(
            "say what you are claiming: ref_kind + ref_value (preferred — the board "
            "derives the key from your checkout), or kind + key if you already have "
            "a composed one.")
    try:
        return _get_client(ctx).claim(body)
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
def claims(ctx: Context, ref_kind: str | None = None, ref_value: str | None = None,
           repo_path: str = ".", kind: str | None = None, key: str | None = None,
           holder: str | None = None) -> dict:
    """What is claimed right now, by whom, and why. Read before you queue behind something.

    To ask about ONE resource, describe it — `ref_kind='issue'`, `ref_value='163'`
    — and the key comes off your checkout, the same way `claim` derives the one it
    writes. Composing the key here instead is how a lookup misses a claim that is
    right there: `kind='issue'` and `kind='work'` were two namespaces for one
    issue, and a caller that guessed the wrong half was told nobody held it.

    Args:
        ref_kind: "issue", "pr", "branch", "plan" or "item" — the preferred way.
        ref_value: the number, branch name or board id.
        repo_path: the checkout whose origin remote names the repo.
        kind: a composed kind, if you already have one. Canonicalised with `key`.
            Not together with `ref_kind` — the answer could only be about one of
            them, and the refusal is the same one `claim` makes.
        key: the composed key.
        holder: only this agent's claims.
    """
    params: dict = {"holder": holder}
    if ref_kind and (kind or key):
        # Refused rather than resolved, exactly as `claim` refuses it. A lookup is
        # where the two-spellings defect actually bites: the answer comes back
        # empty or full for ONE of the resources described, the caller reads it as
        # the answer about the other, and "nobody holds it" about a row that is
        # right there is how #172 looked from outside.
        raise ToolError(
            "ask about one resource: ref_kind + ref_value (the board derives the "
            f"key), or kind/key — not both. You sent ref_kind={ref_kind!r} and "
            f"kind/key={(kind, key)!r}, and the answer would be about only one "
            "of them.")
    if ref_kind:
        if not ref_value:
            raise ToolError("ref_kind needs ref_value: which issue, PR, branch or id?")
        # The ref goes to the BOARD, which derives the key. Deriving it here would
        # be a second implementation of the rule in a package that cannot import
        # the first — which is #172's defect with the parties swapped.
        params["ref_kind"], params["ref_value"] = ref_kind, str(ref_value)
        if ref_kind.lower() not in ("plan", "item"):
            params["repo"] = _derive_repo(repo_path)
    else:
        params["kind"], params["key"] = kind, key
    try:
        return _get_client(ctx).claims(params)
    except httpx.HTTPStatusError as e:
        _raise(e, "claims")


@mcp.tool()
def claim_held(ctx: Context, repo_path: str = ".", holder: str | None = None,
               session: str | None = None) -> dict:
    """Am I holding anything in this repo right now — yes or no.

    The check to make before you start substantive work, and the one a pickup gate
    makes for you. `held` is the answer; `claims` is what you are holding and
    `unattributed` is anything whose key does not name a repo (a plan id, or a
    free-text namespace).

    This exists because presence is the wrong instrument for the question. `active`
    answers "who is around", double-counts an agent that has two identities, and
    lists leases inside the display window that already expired — so it
    systematically overstates how crowded a tree is. A claim is exactly one row per
    held resource, keyed on the resource rather than the holder, and it cannot be
    double-counted by an identity mix-up.

    Args:
        repo_path: the checkout whose origin remote names the repo to ask about.
        holder: whose claims. Defaults to yours, which is almost always what you
            want — naming yourself is how a co-tenant's claim comes back as your
            own.
        session: narrow to one session's claims.
    """
    try:
        return _get_client(ctx).claim_held(
            {"repo": _derive_repo(repo_path), "holder": holder, "session": session})
    except httpx.HTTPStatusError as e:
        _raise(e, "claim_held")


def _derive_repo(repo_path: str) -> str:
    """`owner/name` for the checkout at `repo_path`, or a refusal saying why not.

    No tool here takes a repo string, and that is the whole of #148. They used to,
    and an agent answering "which repo is this" answers with whichever spelling it
    has to hand — `quarterback` from the directory it is standing in,
    `prisonblues/quarterback` from the remote. Both are true, they are not equal,
    and the key was the text, so one repo grew two namespaces.

    #172 made this load-bearing rather than local: a repo name is half of every
    derived claim key now, so the same rule that stopped the allocator issuing
    2.36 twice is what stops two agents claiming one issue under two keys.

    Nothing here is a naming problem: `repo_slug` has been deriving the answer
    from `remote.origin.url` all along, for `sync_status` and `report_git`, and
    it gets scp syntax, https, ssh and a `.git` suffix all to the same string. So
    the fix is not to normalise what a caller typed — it is to stop asking. A
    value read from git is the same value every time and from every seat, which
    is the property the allocator needed and never had.

    Refuses rather than falling back to the directory name. That fallback is how
    the bare spelling entered the table in the first place: a repo whose identity
    cannot be derived has no business keying a shared claim, and guessing one from
    a path is the exact guess this deletes.
    """
    slug = repo_slug(repo_path)
    if not slug:
        raise ToolError(
            f"no owner/name could be read from the git remote at {repo_path!r}. "
            "These tools derive the repo rather than taking one, so that two "
            "seats in one repo cannot disagree about its name. Add an origin "
            "remote, or pass repo_path pointing at a checkout that has one."
        )
    return slug


@mcp.tool()
def merge_queue(ctx: Context, base: str, repo_path: str = ".",
                pr: int | None = None, head: str | None = None) -> dict:
    """The line to land on a branch: who is next, who is waiting, and why (#227).

    Read this BEFORE you integrate, push or start a CI run on a PR you mean to
    land. `claims` tells you whether somebody is landing right now; it cannot tell
    you whether *you* are next, and a PR that is third in line pays a full CI run
    to find that out the expensive way — and invalidates the head's green checks
    with its own integration push on the way.

    Pass `pr` (and `head`, the oid `gh pr view --json headRefOid` gives you) and
    `you` answers directly: `may_integrate`, `may_merge`, your `position`, and a
    `reason` you can paste into a board post. `head` is how the board notices your
    PR has moved since it was last checked — a readiness that cannot expire is a
    permanent green light.

    Being at the head is NOT the merge claim. It is permission to go and ask for
    one: take `kind='merge'` through `claim` before you merge, and expect `claim`
    here to sometimes name a holder who never enqueued at all — a human merging in
    the UI is entitled to, and this queue is advisory like everything else here.

    Ordering is strict FIFO by arrival. `suggested_order` is always null: ordering
    proposals are the unfinished half of #227, deliberately absent because an
    agent rewriting the queue while trying to land makes the queue one more shared
    thing to fight over.

    Args:
        base: the branch being landed ONTO — `main`, `release/2.x`. One queue per
            `repo` + `base`, exactly as the merge claim is keyed.
        repo_path: the checkout whose origin remote names the repo.
        pr: your pull request number, to also get `you`.
        head: `pr`'s current head oid, so a stale entry reads as stale.
    """
    try:
        return _get_client(ctx).merge_queue(
            {"repo": _derive_repo(repo_path), "base": base, "pr": pr, "head": head})
    except httpx.HTTPStatusError as e:
        _raise(e, "merge_queue")


@mcp.tool()
def merge_queue_enqueue(ctx: Context, pr: int, base: str, head: str,
                        verdict: str = "queued", repo_path: str = ".",
                        note: str | None = None, ttl: int | None = None) -> dict:
    """Take a place in the line to land, or update the place you have (#227).

    Idempotent, and it never costs you your place: `entered_at` is written once,
    so calling this on every poll is the intended use. Call it when your PR is
    review-clean and not a draft, and re-call it whenever your head moves.

    `verdict` is what preland said about THIS head, and the board takes your word
    for it — what it adds is that your word is pinned to a commit and stops
    counting when the branch moves:

      * `ready` — preland READY at `head`. The only one that lets a queue head
        merge, and the one that goes away the moment you push.
      * `reconcile` — preland RECONCILE: your base is stale. Admissible, because
        landing in turn dissolves it. Integrate, re-run preland, re-enqueue.
      * `queued` — nothing is wrong except your turn. What to send when this
        endpoint has just told you that.

    preland HOLD is refused: a PR that cannot land would sit at the head holding
    everyone up until its lease expired. Fix the objection first.

    The response's `you` says what you may do right now. If it says you are queued
    behind somebody, STOP — do not rebase, do not push, do not restart CI. That is
    the entire point: you would spend a run to learn what the board already told
    you, and invalidate the head's checks doing it.

    Args:
        pr: your pull request number.
        base: the branch being landed onto.
        head: the PR's full head oid (`gh pr view <pr> --json headRefOid`).
        verdict: `ready`, `reconcile` or `queued`, about `head`.
        repo_path: the checkout whose origin remote names the repo.
        note: what you are landing. Everyone behind you reads it.
        ttl: seconds before your entry lapses and the line moves past you.
            Default 1800; re-enqueueing renews it.
    """
    body = {"repo": _derive_repo(repo_path), "base": base, "pr": pr, "head": head,
            "verdict": verdict, "note": note}
    if ttl is not None:
        body["ttl"] = ttl
    try:
        return _get_client(ctx).merge_queue_write(
            "enqueue", {k: v for k, v in body.items() if v is not None})
    except httpx.HTTPStatusError as e:
        _raise(e, "merge_queue_enqueue")


@mcp.tool()
def merge_queue_leave(ctx: Context, pr: int, base: str, reason: str,
                      repo_path: str = ".", entry_id: str | None = None) -> dict:
    """Stand down from the line, so everyone behind you can move (#227).

    Call it the moment your PR merges, closes or is superseded. The entry expires
    on its own if you vanish, but that is the crude fallback — until it does,
    every PR behind yours is correctly waiting for a land that already happened.

    **Any agent may retire any entry, and that is deliberate**: the one best
    placed to notice a dead head is whoever is sitting behind it. `reason` is
    required and `left_by` records you, so an entry stood down on somebody else's
    behalf is visible as exactly that afterwards.

    It does not touch the `kind='merge'` claim. Release that through
    `release_claim` — two resources, two lifecycles.

    Args:
        pr: the pull request leaving the queue.
        base: the branch it was queued to land on.
        reason: merged / closed / superseded / abandoned, in your own words.
        repo_path: the checkout whose origin remote names the repo.
        entry_id: the id your enqueue returned. Send it when you have one — a PR
            number names a pull request, not one of its stays in the line, so
            without it a leave arriving late can retire the place the PR took
            after re-joining.
    """
    body = {"repo": _derive_repo(repo_path), "base": base, "pr": pr,
            "reason": reason, "entry_id": entry_id}
    try:
        return _get_client(ctx).merge_queue_write(
            "leave", {k: v for k, v in body.items() if v is not None})
    except httpx.HTTPStatusError as e:
        _raise(e, "merge_queue_leave")


@mcp.tool()
def plan_read(ctx: Context, repo: str | None = None, plan: str | None = None,
              include_done: bool = False, limit: int | None = None) -> dict:
    """What is next, in order, and who has it. Read this when you start cold.

    The board's other tools answer who is here and what they touched; this is the
    only one that answers **what to do next**, which is the question you actually
    have. `next` is the first item that is open, unclaimed, unblocked and not
    inside a plan somebody else is holding — the answer, already worked out. The
    list shows why the ones above it were passed over: held by somebody (with
    their identity, so you can go and ask), waiting on something unfinished, or
    `covered_by` an agent that claimed the whole plan.

    Items reference issues and never restate them: read the issue for the what
    and the why, and the plan for the order and the reasoning behind it.

    **Check `next.caveat` before you act on `next`.** The answer is worked out
    from ranks, so it is exactly as good as the ranks are — and an item that was
    appended sits last because that was all `plan_add` could do, not because
    anybody decided it was least important. `order_trust` says how much of the
    sequence was actually chosen and from which rank it stops meaning anything;
    `caveat` is that fact carried to whoever reads only the headline. When it is
    set, read the notes rather than trusting the number.

    **A scope is not always a GitHub repo.** `repo` names a *scope*: usually
    `owner/name`, sometimes `project:<name>` — a scope with no repo behind it, for
    work that has no forge (house work, admin, anything with no code). The reply's
    `scopes` lists every project scope somebody has declared; that is where the
    exact spelling comes from, and reading it is how you find out such work exists
    at all.

    Args:
        repo: the scope — `owner/name` for a repo, or `project:<name>` for one of
            the scopes listed in `scopes`. You get that scope's items plus the
            fleet-wide ones. Omit for everything.
        plan: only one plan, by label ("stage 1") or by id.
        include_done: include finished and dropped items (history, not work).
        limit: most items to return, from the TOP of the order (the board caps it
            at 200 by default). `next` and `counts` always describe the whole
            plan, never the page, and `truncated` says whether you got one.
    """
    try:
        return _get_client(ctx).plan(
            {"repo": repo, "plan": plan, "include_done": include_done,
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
             plan: str | None = None, note: str | None = None,
             depends_on: list[str] | None = None, after: str | None = None,
             before: str | None = None, placed_for: str | None = None) -> dict:
    """Add an item to the plan, appending unless you say where it goes.

    **Placing is not reordering, so you may place.** Permuting items already in
    the plan is contested — two agents rewriting each other is how it stops being
    the shared intent it exists to be — and stays human-only. Saying where a NEW
    item enters alters the relative order of nothing already there: insert between
    ranks 2 and 3 and every existing pair keeps its existing relationship. There
    is no prior decision to overwrite, so there is nothing to thrash.

    **A position is for transcribing an order you were given, not asserting one
    you formed.** If a human tells you mid-session that #85 is near the top, that
    is theirs and you are writing it down — `after`/`before` plus `placed_for` is
    the channel for it, and before it existed the only way to record it was to
    write "TOP PRIORITY — Rich, 23:00" into a field meant for something else. If
    you merely think an item is important, append it and say why in `note`.

    **Link, do not restate.** Pass `ref_kind='issue'` and the number; the issue
    holds the what and the why. What belongs here is the half it cannot hold —
    `note` is where the *reasoning* goes ("before #53, because its schema is the
    one #53 queries"), and that sentence is the whole point of the item.

    One open item per issue: adding an issue that is already in the plan is
    refused and names the item that is already there.

    **You cannot invent a scope here, and that is deliberate.** `repo` takes a repo
    spelled `owner/name`, or a `project:<name>` scope a PERSON has already declared
    (they are listed in `plan_read`'s `scopes`). Anything else is refused —
    including a bare `quarterback` and including `project:` plus a name nobody has
    declared. If it were not, one mistyped scope would put your item in a second
    list that no read reconciles against the first and no reader can see both
    halves of. If a scope is genuinely missing, ask the person whose plan it is;
    the board refuses you rather than guessing, on purpose.

    Args:
        title: one line, a handle for the work — not a description.
        repo: the scope it belongs to — a repo like "prisonblues/quarterback", or a
            declared "project:<name>". Omit for a fleet-wide item (it shows in
            every scope's plan read).
        ref_kind: "issue" or "pr". Both name something on GitHub, so both need a
            repo scope: an item in a `project:` scope has no forge to point into
            and carries its title and note instead.
        ref_value: the number ("60" or "#60").
        plan: the plan it belongs to, by label ("stage 1") or id. A label the
            board does not know creates the plan — one row per label, folded for
            case, so "stage 1" and "Stage 1" cannot become two. Submitting a whole
            plan at once is `plan_submit`; this is for appending to one.
        note: why it sits where it sits.
        depends_on: what it waits on — item ids, or issue refs like "#55" that
            resolve against the same repo's items.
        after: put it immediately below this item — an item id or an issue ref
            ("#84") in the SAME scope (a repo's list and the fleet's are two
            sequences, and a position in one says nothing about the other).
        before: put it immediately above that item. One or the other, not both.
        placed_for: whose priority the placement transcribes — "Rich, 23:00". Only
            accepted with a position; on its own it would be a priority written
            into free text, which is the thing this replaced.
    """
    try:
        return _get_client(ctx).plan_add({
            "title": title, "repo": repo, "ref_kind": ref_kind, "ref_value": ref_value,
            "plan": plan, "note": note, "depends_on": depends_on or [],
            "after": after, "before": before, "placed_for": placed_for})
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
    so it is somebody to talk to rather than a wall. Two things are refused: an
    item waiting on unfinished work, and an item inside a plan somebody ELSE holds
    (`plan_hold` means "all of this is mine", and a claim blocks rather than being
    a note to read past). `force=True` takes it anyway in either case, which puts
    "I know" in the record.

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
def plan_exempt(ctx: Context, reason: str, item_id: str | None = None,
                repo: str | None = None, pr: str | None = None,
                withdraw: bool = False) -> dict:
    """ASK for a PR to be taken out of the review queue. You cannot grant it.

    Exempting a PR from review is a human write, on the same footing as
    reordering the plan (#335): the authorisation to skip a check cannot come
    from the party the check is on. Writing the `review: exempt` marker yourself
    — through `plan_add`, `plan_submit` or `plan_done` — is refused.

    This is where the request goes instead, and it is not a formality. It is
    recorded on the plan item, attributed to you, and announced on the board as a
    `stuck` post addressed to whoever is reading it, so a person can grant or
    decline it from a phone. **Until they do, nothing changes**: the PR stays in
    the queue and stays reviewable, because a request that suspended its own
    review would be the same self-approval by a longer route.

    Asking twice is one request. Say why in a sentence somebody can judge in a
    fortnight — "docs only" is a reason, "not needed" is not.

    Args:
        reason: why review can be skipped. Required, and not blank.
        item_id: the plan item, if you have its id.
        repo, pr: name it by the PR instead — the item must already exist, since
            a PR with no plan item is in the queue and cannot be exempted.
        withdraw: take back a request you made. It will not remove an exemption a
            person granted — if the PR should be reviewed anyway, review it.
    """
    try:
        return _get_client(ctx).plan_item("exempt", {
            "item_id": item_id, "repo": repo, "pr": pr,
            "reason": reason, "grant": not withdraw})
    except httpx.HTTPStatusError as e:
        _raise(e, "plan_exempt")


@mcp.tool()
def plans(ctx: Context, repo: str | None = None, include_closed: bool = False) -> dict:
    """Which plans exist, who is holding one, and how many open items each has.

    Read this BEFORE you start surveying a vague problem. Two agents working out a
    plan for the same thing in parallel is the one genuinely fuzzy race left on
    this board — everything downstream of a plan is exact item keys — and it is the
    race a plan-level claim exists to cover.

    Args:
        repo: this scope's plans plus the fleet-wide ones — `owner/name`, or a
            declared "project:<name>". Omit for everything.
        include_closed: include finished and dropped plans.
    """
    try:
        return _get_client(ctx).plans({"repo": repo, "include_closed": include_closed})
    except httpx.HTTPStatusError as e:
        _raise(e, "plans")


@mcp.tool()
def plan_submit(ctx: Context, label: str, items: list[dict], repo: str | None = None,
                note: str | None = None, claim: bool = True,
                ttl: int | None = None) -> dict:
    """Submit a whole plan — every item, in one transaction — and hold it.

    **Write the plan before you start the work.** Handed a vague problem, your
    first act is this call, not an edit. It gives the fleet something exact to
    coordinate on and it gives you the claim that stops a second agent surveying
    the same ground.

    One call, not a loop over `plan_add`, and that is the point: an eight-item plan
    added one item at a time lands incrementally, so an agent reading the plan
    between item three and item four sees a plan that is not the plan and can claim
    from it. Nothing is written unless all of it is.

    Args:
        label: what this plan is called — a handle you say out loud. One open plan
            per label per repo, folded for case.
        items: the ordered list. Each is a dict:
            `{"title": ..., "ref_kind": "issue", "ref_value": "172",
              "note": "why here", "depends_on": ["@1", "#55", "<item id>"]}`
            `"@1"` means the FIRST item of this submission — which is what lets a
            plan carry its own dependency graph without being written twice.
        repo: the scope it belongs to — `owner/name`, or a declared
            "project:<name>" (see `plan_read`'s `scopes`). Omit for a fleet-wide
            plan; its items can still name repos of their own. A `project:` scope
            takes no `ref_kind`/`ref_value` on its items: there is no forge behind
            it for an issue or a PR number to mean anything against.
        note: why this plan, in your words.
        claim: hold it on the way out (default true — you wrote it).
        ttl: seconds before that claim lapses. Omit for the board's default.
    """
    body: dict = {"label": label, "items": items, "repo": repo, "note": note,
                  "claim": claim}
    if ttl is not None:
        body["ttl"] = ttl
    try:
        return _get_client(ctx).plan_submit(body)
    except httpx.HTTPStatusError as e:
        _raise(e, "plan_submit")


@mcp.tool()
def plan_hold(ctx: Context, plan_id: str, ttl: int | None = None,
              note: str | None = None, force: bool = False) -> dict:
    """Take a WHOLE plan — "all of this is mine", including the planning pass itself.

    Not the same as `plan_claim`, which takes one item. Hold a plan when you are
    surveying (there are no items yet to be exact about) or when you intend to work
    the whole list; then claim its items as you reach them. Your own plan claim
    never blocks you from its items.

    Everyone else's `plan_read` shows the items as `covered_by` you, `next` skips
    them, and `plan_claim` on one of them is refused — so this is how you stop four
    agents converging on one problem without holding twenty item claims.

    Args:
        plan_id: the plan's board id.
        ttl: seconds to hold it.
        note: what the plan is for — it is what a refused agent is shown.
        force: an item somebody ELSE holds refuses the whole plan, because "all of
            this is mine" over a truthful item claim is two agents each correctly
            believing the work is theirs. `force` says it anyway and is recorded in
            the note, for the case where the item's holder really is sharing the
            plan with you. The refusal names the item, its holder and their session
            first — it is somebody to talk to, not a wall.
    """
    body: dict = {"plan_id": plan_id, "note": note}
    if ttl is not None:
        body["ttl"] = ttl
    if force:
        body["force"] = True
    try:
        return _get_client(ctx).plan_verb("claim", body)
    except httpx.HTTPStatusError as e:
        _raise(e, "plan_hold")


@mcp.tool()
def plan_unhold(ctx: Context, plan_id: str) -> dict:
    """Put a whole plan back. Idempotent — holding nothing is a fine answer.

    Do it the moment you stop, or every item in it looks covered to everybody else
    for the rest of your TTL.
    """
    try:
        return _get_client(ctx).plan_verb("release", {"plan_id": plan_id})
    except httpx.HTTPStatusError as e:
        _raise(e, "plan_unhold")


@mcp.tool()
def plan_finish(ctx: Context, plan_id: str, note: str | None = None,
                force: bool = False) -> dict:
    """Record that a whole plan is finished, and let your hold go with it.

    Refused while it still has open items, naming them — "finished" and "six items
    outstanding" cannot both be true, and the plan is what the next agent reads.
    `force=True` closes it over them, deliberately.
    """
    try:
        return _get_client(ctx).plan_verb(
            "done", {"plan_id": plan_id, "note": note, "force": force})
    except httpx.HTTPStatusError as e:
        _raise(e, "plan_finish")


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
        repo: Optional filter — `owner/name`, or the bare name a board post's
            `repo` ref carries, which is matched by basename (#350).

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
