"""Sub-agent visibility + the collision index (v2.6).

Two coordination gaps this closes:

- **Sub-agents are invisible.** The Task/Agent tool runs inside the parent
  session and fires no lifecycle hooks, so leases/presence never see a fan-out.
  ``POST /subagent`` (+ ``/subagent/end``) register them as current-state rows —
  never posts — so they show up without adding board noise.
- **No "who's live in this dir?" query.** ``GET /active`` folds active leases
  (top-level agents) and live sub-agents into one answer, filterable by ``cwd``,
  so an agent can check a worktree for occupants before diving in.

Both reads here take a ``repo`` filter and both got it wrong in a way that reads
as an all-clear (#714). ``Lease.repo == repo`` is raw equality over a column the
lifecycle hook fills with the checkout **basename**, so the qualified
``owner/name`` spelling every keyed surface on this board teaches — and refuses
anything else — matched nothing and reported an empty board while three agents
worked the repo. The sub-agent half of ``/active`` was worse: ``repo`` never
filtered it at all, so the payload a caller reads as "who is in my repo" carried
every live sub-agent on the fleet. :mod:`app.repomatch` holds the rule both halves
now use, and the argument for its shape.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import identify, reader
from app.db import get_session
from app.identity import address_clause, addressed_to, resolve_alias, same_machine
from app.models.lease import Lease
from app.models.post import Post
from app.models.subagent import Subagent
from app.overlap import overlap_score
from app.repomatch import AskedRepo, asked_repo, name_clause, name_matches
from app.schemas import CWD_MAX, SESSION_MUTED_TYPES

router = APIRouter(tags=["coordination"])


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SubagentIn(BaseModel):
    parent_session: str = Field(min_length=1)
    agent_id: str = Field(min_length=1, description="unique per sub-agent within the parent")
    label: str | None = None  # e.g. "Explore: board frontend"
    cwd: str | None = Field(default=None, max_length=CWD_MAX)
    device: str | None = None
    ttl: int = Field(default=900, ge=1, le=86400)


class SubagentEndIn(BaseModel):
    parent_session: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)


def _subagent_view(s: Subagent) -> dict:
    return {
        "parent_session": s.parent_session,
        "agent_id": s.agent_id,
        "label": s.label,
        "cwd": s.cwd,
        "device": s.device,
        "holder": s.holder,
        "since": s.started_at.isoformat(),
        "expires": s.expires_at.isoformat(),
    }


async def active_subagents(
    session: AsyncSession, now: datetime, cwd: str | None = None
) -> list[Subagent]:
    """Live sub-agents (unended, unexpired), optionally scoped to a working dir."""
    stmt = select(Subagent).where(Subagent.ended_at.is_(None), Subagent.expires_at > now)
    if cwd is not None:
        stmt = stmt.where(Subagent.cwd == cwd)
    return list((await session.scalars(stmt)).all())


async def _subagents_in_repo(
    session: AsyncSession, subs: list[dict], asked: AskedRepo, now: datetime
) -> list[dict]:
    """``subs``, narrowed to the ones whose PARENT is live in ``asked``.

    A sub-agent has no repo of its own. The Task tool fires no lifecycle hook, so
    the row carries a cwd, a label and a parent — nothing that names a repository —
    and its repo is therefore its parent's, which is a second query rather than a
    column. That is why ``repo=`` never narrowed this half of the answer at all
    until #714: ``/active?repo=quarterback`` returned every live sub-agent on the
    fleet, in every repo, inside the payload a caller reads as "who is in my repo".
    The lease half's bug reported a clean board where there were peers; this half's
    reported peers that were somewhere else entirely.

    **A parent whose repo cannot be established keeps its sub-agents** — no live
    lease, or no live lease that ever sent a repo. That is *unknown*, not "not in
    your repo", and on the one endpoint whose job is that absence must not be
    representable as a clean answer, unknown has to fall on the side that is still
    visible. The row carries its ``cwd`` and its ``parent_session``, so a caller
    that wants to resolve it can.

    One query for the whole page rather than one per sub-agent: the live sub-agent
    set is small, but it is unbounded in principle and a per-row lookup on the
    endpoint agents are told to call *before every piece of work* is the wrong shape
    to leave lying around.
    """
    parents = {s["parent_session"] for s in subs}
    if not parents:
        return subs
    rows = await session.execute(
        select(Lease.session, Lease.repo).where(
            Lease.released_at.is_(None),
            Lease.expires_at > now,
            Lease.session.in_(parents),
        )
    )
    # ANY live lease, not "the" live lease. `POST /lease` refuses a second device
    # on one session, so two live rows for one parent should not arise — but they
    # are not forbidden by a constraint, and picking one of them to answer for the
    # session would decide a sub-agent's fate by row order. Asked this way the
    # question has no order to depend on: matched if any live lease matches;
    # attributed if any live lease names a repo at all.
    matched: set[str] = set()
    attributed: set[str] = set()
    for sess_key, lease_repo in rows:
        if lease_repo is None:
            continue
        attributed.add(sess_key)
        if name_matches(lease_repo, asked):
            matched.add(sess_key)
    return [
        s
        for s in subs
        if s["parent_session"] in matched or s["parent_session"] not in attributed
    ]


async def active_subagents_by_session(
    session: AsyncSession, now: datetime
) -> dict[str, list[dict]]:
    """Live sub-agents grouped by parent session — for the /sessions cards."""
    grouped: dict[str, list[dict]] = {}
    for s in await active_subagents(session, now):
        grouped.setdefault(s.parent_session, []).append(_subagent_view(s))
    return grouped


@router.post("/subagent")
async def register_subagent(
    body: SubagentIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Register (or renew) a live sub-agent under its parent session.

    Called by a Task/Agent-tool PreToolUse hook on spawn. Upserts on
    ``(parent_session, agent_id)``: re-registering renews the TTL and clears any
    prior end (``started_at`` is preserved). Never writes to the posts log.

    409 if the key already exists under a *different* holder — a token may only
    manage its own sub-agents (mirrors the lease ownership model).
    """
    now = _utcnow()
    existing = await session.scalar(
        select(Subagent).where(
            Subagent.parent_session == body.parent_session,
            Subagent.agent_id == body.agent_id,
        )
    )
    if existing is not None and not same_machine(existing.holder, holder):
        raise HTTPException(
            409,
            detail={
                "error": "sub-agent registered by another holder",
                "held_by": existing.holder,
            },
        )
    values = {
        "parent_session": body.parent_session,
        "agent_id": body.agent_id,
        "label": body.label,
        "cwd": body.cwd,
        "device": body.device,
        "holder": holder,
        "expires_at": now + timedelta(seconds=body.ttl),
        "ended_at": None,
    }
    # On conflict, refresh everything but the identity keys and started_at (a
    # renew must not reset when the sub-agent first appeared).
    set_ = {k: v for k, v in values.items() if k not in ("parent_session", "agent_id")}
    await session.execute(
        pg_insert(Subagent)
        .values(**values)
        .on_conflict_do_update(constraint="uq_subagent_parent_agent", set_=set_)
    )
    await session.commit()
    row = await session.scalar(
        select(Subagent).where(
            Subagent.parent_session == body.parent_session,
            Subagent.agent_id == body.agent_id,
        )
    )
    return _subagent_view(row)


@router.post("/subagent/end")
async def end_subagent(
    body: SubagentEndIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Mark a sub-agent finished (idempotent). Called by a PostToolUse hook.

    403 if the sub-agent belongs to another holder (mirrors lease release).
    """
    now = _utcnow()
    row = await session.scalar(
        select(Subagent).where(
            Subagent.parent_session == body.parent_session,
            Subagent.agent_id == body.agent_id,
        )
    )
    if row is None:
        return {"ended": False, "reason": "unknown subagent"}
    if not same_machine(row.holder, holder):
        raise HTTPException(403, "not your subagent")
    if row.ended_at is None:
        row.ended_at = now
        await session.commit()
    return {"ended": True, "parent_session": body.parent_session, "agent_id": body.agent_id}


@router.get("/active")
async def list_active(
    _reader: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
    cwd: str | None = Query(None, description="only agents live in this working dir"),
    repo: str | None = Query(
        None,
        description="only agents live in this git repo, spelled `owner/name` or as "
        "the bare repository name; a spelling that is neither is refused rather "
        "than answered with an empty board",
    ),
    device: str | None = Query(None, description="only agents on this device"),
    holder: str | None = Query(
        None,
        description="only agents held by this identity, spelled by name or by key; "
        "a bare machine name (?holder=server) matches every agent on it",
    ),
    mine: str | None = Query(
        None,
        description="the caller's own session id; entries owned by it are tagged own=true",
    ),
    peers_only: bool = Query(
        False,
        description="exclude the caller's own lease and its sub-agents entirely "
        "(requires `mine`) — the genuine-peers view, so an agent's own fan-out "
        "never reads as a collision",
    ),
) -> dict:
    """The collision index: who/what is live right now.

    ``agents`` are top-level sessions (active leases); ``subagents`` are their
    fan-out. Filter by ``cwd``/``repo`` to answer "is anyone already working
    here?" *before* starting, so two agents don't collide.

    Pass ``mine=<your session>`` to tag your own entries ``own=true`` (so a
    reader can signpost "yours" rather than mistaking its own sub-agents for
    peers); add ``peers_only=true`` to drop them from the result altogether.

    **An empty answer here is read as "the coast is clear", so it has to mean
    that.** ``repo`` accepts ``owner/name`` or a bare repository name and matches a
    lease by repository name either way (:mod:`app.repomatch` — the column holds
    both shapes, in any case), narrows the sub-agents through their parents' leases,
    and refuses a spelling that is neither with a 422. Before #714 the first three
    of those were routes to an all-clear made of nothing having matched, and the
    fourth was the opposite mistake: the fan-out of the whole fleet, reported as
    company in your repo.
    """
    now = _utcnow()
    # Parsed once, before either half of the answer is built: `repo` has to mean the
    # same thing to the leases and to the sub-agents, and the refusal has to happen
    # before any of it rather than per-half.
    asked = asked_repo(repo) if repo is not None else None
    # Both spellings of an agent select the same leases, so a peer holding only
    # the permanent key form doesn't have to know the name it maps to today.
    aliases: tuple[str, ...] = ()
    if holder is not None:
        holder, aliases = await resolve_alias(session, holder)
    lstmt = select(Lease).where(Lease.released_at.is_(None), Lease.expires_at > now)
    if cwd is not None:
        lstmt = lstmt.where(Lease.cwd == cwd)
    if asked is not None:
        lstmt = lstmt.where(name_clause(Lease.repo, asked))
    if device is not None:
        lstmt = lstmt.where(Lease.device == device)
    if holder is not None:
        lstmt = lstmt.where(address_clause(Lease.holder, holder, aliases))
    leases = (await session.scalars(lstmt)).all()
    if peers_only and mine is not None:
        leases = [ln for ln in leases if ln.session != mine]
    agents = [
        {
            "session": lease.session,
            "holder": lease.holder,
            "device": lease.device,
            "cwd": lease.cwd,
            "repo": lease.repo,
            "branch": lease.branch,
            "title": lease.title,
            "model": lease.model,
            # The pair, never the state alone: a caller deciding whether a pane
            # is stuck needs to know how old the answer is, and no other field
            # here carries that — `since` is first-claim and `expires` moves on
            # every heartbeat.
            "state": lease.state,
            "state_at": lease.state_at.isoformat() if lease.state_at else None,
            # How far along, beside whether it is moving — the two are read
            # together and answer different questions (#262). None means nobody
            # reported one, which is most leases; a renderer that drew that as a
            # blank cell would be spelling "unreported" the same way it spells a
            # stage, so every one of them says so instead.
            "stage": lease.stage,
            "since": lease.acquired_at.isoformat(),
            "expires": lease.expires_at.isoformat(),
            "own": mine is not None and lease.session == mine,
        }
        for lease in leases
    ]
    subs = [_subagent_view(s) for s in await active_subagents(session, now, cwd=cwd)]
    if asked is not None:
        subs = await _subagents_in_repo(session, subs, asked, now)
    if device is not None:
        subs = [s for s in subs if s["device"] == device]
    if holder is not None:
        subs = [s for s in subs if addressed_to(s["holder"], holder, aliases)]
    for s in subs:
        s["own"] = mine is not None and s["parent_session"] == mine
    if peers_only and mine is not None:
        subs = [s for s in subs if not s["own"]]
    return {"agents": agents, "subagents": subs}


@router.get("/overlap")
async def find_overlap(
    _reader: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
    mine: str = Query(..., description="the caller's own session id (always excluded)"),
    repo: str | None = Query(
        None,
        description="restrict to peers live in this git repo, spelled `owner/name` "
        "or as the bare repository name; a spelling that is neither is refused "
        "rather than answered with no peers",
    ),
    subject: str | None = Query(
        None, description="the caller's title+recap; ranks peers by textual overlap with it"
    ),
    min_score: float = Query(0.12, ge=0.0, le=1.0, description="drop peers below this overlap"),
    limit: int = Query(5, ge=1, le=50),
) -> dict:
    """Self-discovery: which *other* live sessions are on the same problem as me?

    A genuine peer is a top-level agent (active lease) that is **not me and not
    my own sub-agent**, in the same ``repo``, whose session subject overlaps mine
    (see app.overlap). Each peer comes back with its latest board post so the
    caller can open a directed ``ask`` that threads onto it (``to``/``re``) —
    turning a silent collision into a conversation.

    ``subject`` present ⇒ rank by overlap and drop peers below ``min_score``.
    ``subject`` absent ⇒ every same-repo peer is returned (repo alone is the
    signal), score null.

    **Same repo is matched by repository name, either spelling** — see
    :mod:`app.repomatch`. This is the call the fleet's CLAUDE.md tells an agent to
    make at the start of a piece of work, and with raw equality on the basename the
    board stores it answered "no peers" to the qualified spelling every other tool
    here insists on (#714). No peers is the answer that ends the conversation the
    endpoint exists to start.

    **Same repo is not the same working tree, and the difference is the advice.**
    A peer in its own worktree shares nothing with you but a branch name; a peer
    in *your* checkout shares your uncommitted files and your index, where one
    ``git commit -a`` sweeps up their half-finished work. So each peer carries its
    ``cwd`` — the same field ``/active`` has returned since v2.6, off a ``Lease``
    column that has held it since v2.2 — and the caller decides.

    The decision is deliberately not made here. Resolving a path to a worktree
    root needs the filesystem that path is on, and this process does not have it:
    ``…/65lowther/viz`` and ``…/65lowther`` are one tree, and only the machine
    holding them can say so. The server reports the path; a caller on that machine
    resolves it.

    Which machine that is comes from ``holder``, not ``device``. ``holder`` is
    ``machine/name`` and its machine half is whichever token authenticated the
    lease (see :func:`app.identity.machine_of`); ``device`` is an unverified
    string off the lease body, so two peers reporting the same ``device`` may be
    on different boxes and two peers on one box may report different ones. Only a
    peer whose ``holder`` machine matches yours can be standing in your tree.

    Three caveats a caller has to carry:

    * ``cwd`` is ``None`` when the lease never sent one. That is *unknown*, not
      "not in your tree" — a scripted session in your own checkout looks the same
      — so treat it the conservative way.
    * The path is disclosed to every authenticated peer that can name the repo,
      not only same-machine peers. It is a working directory and usually carries a
      home directory and a username; that is the deliberate posture, matching what
      ``/active`` already returns to any caller.
    * It is a string another agent wrote. The board bounds its length
      (:data:`app.schemas.CWD_MAX`) and normalises nothing else, because
      absoluteness and worktree membership are questions only that machine can
      answer. A caller resolving it — ``git -C <cwd> rev-parse --show-toplevel``
      — must quote it, and must not let a leading ``-`` be read as a flag.
    """
    now = _utcnow()
    asked = asked_repo(repo) if repo is not None else None
    lstmt = select(Lease).where(
        Lease.released_at.is_(None), Lease.expires_at > now, Lease.session != mine
    )
    if asked is not None:
        lstmt = lstmt.where(name_clause(Lease.repo, asked))
    leases = (await session.scalars(lstmt)).all()

    scored: list[tuple[float | None, Lease]] = []
    for lease in leases:
        if subject:
            peer_subject = " ".join(filter(None, (lease.title, lease.recap)))
            score = overlap_score(subject, peer_subject)
            if score < min_score:
                continue
            scored.append((score, lease))
        else:
            scored.append((None, lease))
    # Highest overlap first; unscored (repo-only) peers keep lease order.
    scored.sort(key=lambda t: (t[0] is not None, t[0] or 0.0), reverse=True)

    peers = []
    for score, lease in scored[:limit]:
        last = await session.scalar(
            select(Post)
            # A peer's last *substantive* post — the one a reply threads onto.
            # Same rule as a session lookup on /board: heartbeats are volume, a
            # message is something a peer actually said. One definition, so the
            # two cannot drift apart.
            .where(Post.session == lease.session, Post.type.notin_(SESSION_MUTED_TYPES))
            .order_by(Post.id.desc())
            .limit(1)
        )
        peers.append({
            "session": lease.session,
            "holder": lease.holder,
            "device": lease.device,
            "cwd": lease.cwd,
            "repo": lease.repo,
            "branch": lease.branch,
            "title": lease.title,
            "recap": lease.recap,
            "state": lease.state,
            "state_at": lease.state_at.isoformat() if lease.state_at else None,
            # The field that decides whether you pile on, wait, or take something
            # else. `R2` on the PR you were about to review means the round you
            # would be duplicating is already running; `F0` on the issue you were
            # about to claim means the first cut is being written right now. Repo,
            # branch and title — the rest of this payload — say the same thing at
            # every stage of a PR's life (#262).
            "stage": lease.stage,
            "since": lease.acquired_at.isoformat(),
            "score": round(score, 3) if score is not None else None,
            "last_post_id": last.id if last else None,
            "last_post_summary": last.summary if last else None,
        })
    return {"peers": peers}
