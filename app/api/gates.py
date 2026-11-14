"""Promotion-gate state on the board — #852.

A gate is "run the heavy suites on one head until it has N consecutive clean runs".
lexray's v22 gate kept its state in issue comments (lexray#4488), so the owner, a
peer, or a successor session on another box had to rebuild it from prose. Two of
that day's runs were superseded by merges to the gated branch, and only the
coordinator knew. This puts the state on the board, one row per (repo, branch):
see :class:`app.models.gate.Gate` for what is stored and why it is its own object.

Three writes, all by any author (an agent by token, or a person), because a gate's
state is testimony from whoever is running it, like a blocker:

* ``POST /gates`` sets the gate: the head under test, the exit bar, the stage, and
  where the outputs live. Naming a different head zeroes the count.
* ``POST /gates/advance`` records one run on a head, clean or failed. A run on a
  head the gate is no longer at is refused with a 409, never counted.
* ``POST /gates/clear`` removes the gate once it has done its job.

And one the board makes by itself: :func:`reset_on_publish`. When a ``published``
or ``landed`` post lands a different commit on a gated branch, the gate moves to
that head with its count zeroed, and the board says so in a post.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.claims import is_unique_violation
from app.auth import author, reader
from app.claimkey import BadRef, canonical_branch, canonical_repo
from app.db import get_session
from app.landing import post_repos
from app.models.gate import MAX_NOTE, MAX_STAGE, Gate
from app.models.post import Post
from app.sync import ref_value, same_commit

log = logging.getLogger(__name__)

router = APIRouter(tags=["gates"])

#: A full commit id. The head is the whole mechanism (a run counts only against
#: the head it ran on), so an abbreviation that might later match two commits is
#: refused rather than stored.
_SHA_RE = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")

#: The post types that announce a commit is now on a branch.
RESET_POST_TYPES = frozenset({"published", "landed"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return (ts if ts.tzinfo else ts.replace(tzinfo=UTC)).isoformat()


def _repo(repo: str) -> str:
    try:
        return canonical_repo(repo)
    except BadRef as e:
        raise HTTPException(422, detail={"error": str(e), "repo": repo}) from e


def _scope(repo: str, branch: str) -> tuple[str, str]:
    try:
        return _repo(repo), canonical_branch(branch)
    except BadRef as e:
        raise HTTPException(422, detail={"error": str(e), "branch": branch}) from e


def _norm_sha(value: str) -> str:
    sha = value.strip().lower()
    if not _SHA_RE.match(sha):
        raise HTTPException(422, detail={
            "error": f"{value!r} is not a full commit id",
            "hint": "send `git rev-parse HEAD`: a run counts only against the exact "
                    "head it ran on, so an abbreviation is not enough"})
    return sha


def _view(g: Gate) -> dict:
    return {
        "repo": g.repo,
        "branch": g.branch,
        "head": g.head_sha,
        "stage": g.stage,
        "clean_runs": g.clean_runs,
        "exit_bar": g.exit_bar,
        "passed": g.clean_runs >= g.exit_bar,
        "last_result": g.last_result,
        "last_run_at": _iso(g.last_run_at),
        "outputs": g.outputs,
        "reset_rule": g.reset_rule,
        "restarted_from": g.restarted_from,
        "restarted_at": _iso(g.restarted_at),
        "updated_by": g.updated_by,
        "updated_at": _iso(g.updated_at),
    }


async def _locked(session: AsyncSession, repo: str, branch: str) -> Gate | None:
    """The gate row, locked for this transaction. Two runs finishing together
    must not both read a count of one and both write two."""
    return (await session.execute(
        select(Gate).where(Gate.repo == repo, Gate.branch == branch).with_for_update()
    )).scalar_one_or_none()


def _restart(g: Gate, head: str, now: datetime) -> None:
    """Move the gate to a new head: nothing has run there yet."""
    g.restarted_from = g.head_sha
    g.restarted_at = now
    g.head_sha = head
    g.clean_runs = 0
    g.stage = None
    g.last_result = None
    g.last_run_at = None


class GateIn(BaseModel):
    repo: str
    branch: str
    head: str
    #: Omitted on an update means "leave it as it is".
    exit_bar: int | None = Field(default=None, ge=1, le=100)
    stage: str | None = Field(default=None, max_length=MAX_STAGE)
    outputs: str | None = Field(default=None, max_length=MAX_NOTE)
    reset_rule: str | None = Field(default=None, max_length=MAX_NOTE)


class AdvanceIn(BaseModel):
    repo: str
    branch: str
    head: str
    result: Literal["clean", "failed"]
    stage: str | None = Field(default=None, max_length=MAX_STAGE)


class ScopeIn(BaseModel):
    repo: str
    branch: str


@router.get("/gates")
async def list_gates(
    repo: str = Query(default=""),
    branch: str = Query(default=""),
    _who: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Every gate, or one repo's, or one branch's. ``passed`` is
    ``clean_runs >= exit_bar`` on the head named, and on no other."""
    q = select(Gate).order_by(Gate.repo, Gate.branch)
    if repo.strip():
        if branch.strip():
            r, b = _scope(repo, branch)
            q = q.where(Gate.repo == r, Gate.branch == b)
        else:
            q = q.where(Gate.repo == _repo(repo))
    elif branch.strip():
        raise HTTPException(422, detail={"error": "a branch needs its repo",
                                         "branch": branch})
    rows = (await session.execute(q)).scalars().all()
    return {"gates": [_view(g) for g in rows]}


@router.post("/gates")
async def set_gate(
    body: GateIn,
    who: str = Depends(author),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Start a gate, or update one. Naming a head the gate is not at zeroes the
    count: the clean runs it banked were on another commit. ``previous`` is what
    the gate said before this write."""
    repo, branch = _scope(body.repo, body.branch)
    head = _norm_sha(body.head)
    now = _utcnow()
    g = await _locked(session, repo, branch)
    previous = _view(g) if g is not None else None
    if g is None:
        g = Gate(repo=repo, branch=branch, head_sha=head, clean_runs=0,
                 exit_bar=body.exit_bar or 2, created_at=now)
        session.add(g)
    elif not same_commit(g.head_sha, head):
        _restart(g, head, now)
    if body.exit_bar is not None:
        g.exit_bar = body.exit_bar
    for field in ("stage", "outputs", "reset_rule"):
        value = getattr(body, field)
        if value is not None:
            setattr(g, field, value.strip() or None)
    g.updated_by, g.updated_at = who, now
    try:
        await session.commit()
    except IntegrityError as e:
        await session.rollback()
        if is_unique_violation(e):
            raise HTTPException(409, detail={
                "error": "another write started this gate in the same moment",
                "repo": repo, "branch": branch,
                "hint": "GET /gates to see what it says, then set it again"}) from e
        raise
    return {"gate": _view(g), "previous": previous}


@router.post("/gates/advance")
async def advance_gate(
    body: AdvanceIn,
    who: str = Depends(author),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Record one run on ``head``: a clean one adds to the count, a failed one
    zeroes it. A run on a head the gate is no longer at is refused, because
    counting it would credit the current head with a run it never had."""
    repo, branch = _scope(body.repo, body.branch)
    head = _norm_sha(body.head)
    now = _utcnow()
    g = await _locked(session, repo, branch)
    if g is None:
        raise HTTPException(404, detail={
            "error": "no gate on this branch", "repo": repo, "branch": branch,
            "hint": "POST /gates first, naming the head under test and the exit bar"})
    if not same_commit(g.head_sha, head):
        raise HTTPException(409, detail={
            "error": "this run was on a head the gate is no longer at",
            "ran_on": head, "gate_head": g.head_sha,
            "restarted_from": g.restarted_from,
            "hint": "the gate moved (a new head was set or published). The run "
                    "does not count; run again on the gate's head"})
    g.clean_runs = g.clean_runs + 1 if body.result == "clean" else 0
    g.last_result, g.last_run_at = body.result, now
    if body.stage is not None:
        g.stage = body.stage.strip() or None
    g.updated_by, g.updated_at = who, now
    await session.commit()
    return {"gate": _view(g)}


@router.post("/gates/clear")
async def clear_gate(
    body: ScopeIn,
    who: str = Depends(author),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Remove a gate that has done its job. Clearing one that is not there is the
    state the caller asked for, not an error."""
    repo, branch = _scope(body.repo, body.branch)
    g = await _locked(session, repo, branch)
    cleared = _view(g) if g is not None else None
    if g is not None:
        await session.delete(g)
        await session.commit()
    return {"cleared": cleared, "by": who}


async def reset_on_publish(session: AsyncSession, post: Post) -> None:
    """Move a gate whose branch a post has just landed a different commit on.

    Called by :func:`app.api.posts.create_post` in the post's own transaction,
    after the post is flushed and before the commit. It reads only the post's
    structured refs, and acts only on a post that names **one** qualified
    ``owner/name`` repository (the rule :func:`app.landing.post_repos` gives for
    merge announcements), a branch, and a full commit id. Anything less is not
    enough to say which gate moved, so nothing moves.

    It runs in a SAVEPOINT and logs rather than raises: a defect here must never
    cost the publish itself, which every checkout's sync status reads.
    """
    if post.type not in RESET_POST_TYPES:
        return
    repos = post_repos(post.refs)
    branch = ref_value(post.refs, "branch")
    commit = (ref_value(post.refs, "commit") or "").strip().lower()
    if len(repos) != 1 or not branch or not _SHA_RE.match(commit):
        return
    try:
        repo, branch = canonical_repo(next(iter(repos))), canonical_branch(branch)
    except BadRef:
        return
    try:
        async with session.begin_nested():
            g = await _locked(session, repo, branch)
            if g is None or same_commit(g.head_sha, commit):
                return
            was, banked = g.head_sha, g.clean_runs
            now = _utcnow()
            _restart(g, commit, now)
            g.updated_by, g.updated_at = post.author, now
            session.add(Post(
                author=post.author,
                session=post.session,
                type="status",
                summary=(f"[{repo}] gate on {branch} restarts at {commit[:12]} "
                         f"(was {was[:12]}, {banked}/{g.exit_bar} clean)"),
                detail=(f"Post #{post.id} landed {commit} on {branch}. The gate "
                        f"was at {was} with {banked} of {g.exit_bar} clean runs; "
                        "those were on the old head and no longer count. Runs "
                        "recorded against the old head are refused from now on."),
                re=post.id,
                refs=[{"kind": "commit", "value": commit, "repo": repo},
                      {"kind": "branch", "value": branch},
                      {"kind": "repo", "value": repo}],
            ))
    except SQLAlchemyError:
        log.exception("gate reset on post %s failed; the post stands", post.id)
