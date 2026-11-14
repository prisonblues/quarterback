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
  where the outputs live. Naming a different head zeroes the count. The reply says
  whether the branch has since been published at another commit (``behind``).
* ``POST /gates/advance`` records one run on a head, clean or failed. A run on a
  head the gate is no longer at is refused with a 409, never counted.
* ``POST /gates/clear`` removes the gate once it has done its job.

And one the board makes by itself: :func:`reset_on_publish`. When a ``published``
post puts a commit on a gated branch for the first time, the gate moves to it with
its count zeroed, and the board says so in a post.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.claims import is_unique_violation
from app.auth import author, reader
from app.claimkey import BadRef, canonical_branch, canonical_repo
from app.db import get_session
from app.landing import LANDING_POST_TYPE, post_repos
from app.models.gate import DEFAULT_EXIT_BAR, MAX_NOTE, MAX_STAGE, Gate
from app.models.post import Post
from app.sync import ref_value, same_commit

log = logging.getLogger(__name__)

router = APIRouter(tags=["gates"])

#: A full commit id. The head is the whole mechanism (a run counts only against
#: the head it ran on), so an abbreviation that might later match two commits is
#: refused rather than stored.
_SHA_RE = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")

#: How many of a branch's newest ``published`` posts a ``POST /gates`` reads to
#: find the commit the branch was last published at.
_PUBLISH_SCAN = 50


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return (ts if ts.tzinfo else ts.replace(tzinfo=UTC)).isoformat()


def _full_sha(value: str | None) -> str | None:
    """``value`` as a full lower-case commit id, or None if it is not one."""
    sha = (value or "").strip().lower()
    return sha if _SHA_RE.match(sha) else None


def _norm_sha(value: str) -> str:
    sha = _full_sha(value)
    if sha is None:
        raise HTTPException(422, detail={
            "error": f"{value!r} is not a full commit id",
            "hint": "send `git rev-parse HEAD`: a run counts only against the exact "
                    "head it ran on, so an abbreviation is not enough"})
    return sha


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


def _branch_publishes(repo: str, branch: str):
    """``published`` posts naming ``branch``, newest first. The repo is matched by
    the caller through :func:`app.landing.post_repos`, because the two producers
    put the qualified name on different refs (CI on the commit, the hook on the
    repo ref)."""
    return (select(Post)
            .where(Post.type == LANDING_POST_TYPE,
                   Post.refs.contains([{"kind": "branch", "value": branch}]))
            .order_by(Post.id.desc()))


def _names(post: Post, repo: str) -> bool:
    return post_repos(post.refs) == {repo}


async def _latest_published(session: AsyncSession, repo: str, branch: str) -> str | None:
    """The full commit the branch was last published at, or None if the board
    has not seen one."""
    rows = (await session.execute(
        _branch_publishes(repo, branch).limit(_PUBLISH_SCAN))).scalars()
    for p in rows:
        sha = _full_sha(ref_value(p.refs, "commit"))
        if sha and _names(p, repo):
            return sha
    return None


async def _published_before(session: AsyncSession, post: Post, repo: str,
                            branch: str, commit: str) -> bool:
    """Has this commit been published on this branch before ``post``?

    The board cannot see the commit graph, so this is how it tells a branch that
    moved from an announcement of a commit it already had: CI re-announcing a push,
    or a checkout that is behind its upstream announcing the old tip. Only a commit
    the branch has never been published at is a new head.
    """
    rows = (await session.execute(
        _branch_publishes(repo, branch)
        .where(Post.id < post.id,
               Post.refs.contains([{"kind": "commit", "value": commit}])))).scalars()
    return any(_names(p, repo) for p in rows)


class GateIn(BaseModel):
    repo: str
    branch: str
    head: str
    #: Omitted and null both mean "leave it as it is" on an update; there is no
    #: way to unset a field here, and none is needed while every one is optional.
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
    the gate said before this write.

    ``branch_published`` is the commit the board last saw the branch published at,
    and ``behind`` is true when that is not ``head``: the gate is being set on a
    commit the branch has already moved past, and a publish after this one is the
    only thing that would move it.
    """
    repo, branch = _scope(body.repo, body.branch)
    head = _norm_sha(body.head)
    now = _utcnow()
    g = await _locked(session, repo, branch)
    previous = _view(g) if g is not None else None
    # Read before anything is added: a query autoflushes, and a racing insert's
    # unique violation must surface at the commit below, where it becomes a 409.
    published = await _latest_published(session, repo, branch)
    if g is None:
        g = Gate(repo=repo, branch=branch, head_sha=head, clean_runs=0,
                 exit_bar=body.exit_bar or DEFAULT_EXIT_BAR, created_at=now)
        session.add(g)
    else:
        if not same_commit(g.head_sha, head):
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
    return {"gate": _view(g), "previous": previous, "branch_published": published,
            "behind": published is not None and not same_commit(published, head)}


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


def _answer(post: Post, repo: str, branch: str, commit: str | None,
            summary: str, detail: str) -> Post:
    """The board's ``status`` reply to the publish that moved, or failed to move,
    a gate. Its branch ref carries the repo, because a branch name means nothing
    without one (the convention :mod:`app.api.leases` writes down)."""
    refs = [{"kind": "branch", "value": branch, "repo": repo},
            {"kind": "repo", "value": repo}]
    if commit:
        refs.insert(0, {"kind": "commit", "value": commit, "repo": repo})
    return Post(author=post.author, session=post.session, type="status",
                summary=f"[{repo}] {summary}", detail=detail, re=post.id, refs=refs)


async def reset_on_publish(session: AsyncSession, post: Post) -> None:
    """Move a gate whose branch a ``published`` post has put a new commit on.

    Called by :func:`app.api.posts.create_post` after the post is added and before
    it commits. It reads only the post's structured refs, and acts only on a post
    that names **one** qualified ``owner/name`` repository (the rule
    :func:`app.landing.post_repos` gives for merge announcements) and a branch
    with a gate on it. Then:

    * a full commit the branch has not been published at before moves the gate
      there, zeroes the count, and answers the post saying so;
    * the gate's own head, or a commit already published on the branch, changes
      nothing (see :func:`_published_before`);
    * no usable commit (absent or abbreviated) leaves the gate where it is and
      answers the post saying it did not move, so a reader is not left believing
      the gate followed the branch.

    ``landed`` posts are ignored: they say a commit exists, not that the branch's
    runners can fetch it.

    Nothing raised here can cost the publish, which every checkout's sync status
    reads: the database work runs in a SAVEPOINT, and the whole body under one
    ``except Exception`` that logs. Only the flush that gives the post its id is
    outside it, and a post that cannot be flushed has failed on its own account.
    """
    if post.type != LANDING_POST_TYPE:
        return
    await session.flush()
    try:
        await _reset(session, post)
    except Exception:
        log.exception("gate reset on post %s failed; the post stands", post.id)


async def _reset(session: AsyncSession, post: Post) -> None:
    repos = post_repos(post.refs)
    raw_branch = ref_value(post.refs, "branch")
    if len(repos) != 1 or not raw_branch:
        return
    try:
        repo, branch = canonical_repo(next(iter(repos))), canonical_branch(raw_branch)
    except BadRef:
        return
    commit = _full_sha(ref_value(post.refs, "commit"))
    async with session.begin_nested():
        g = await _locked(session, repo, branch)
        if g is None:
            return
        if commit is None:
            session.add(_answer(
                post, repo, branch, None,
                f"gate on {branch} NOT moved: post #{post.id} named no full commit",
                f"Post #{post.id} announced {branch} without a full commit id, so the "
                f"board cannot tell where the branch is. The gate stays at "
                f"{g.head_sha} with {g.clean_runs}/{g.exit_bar} clean. If the branch "
                "moved, set the new head with gate_set."))
            return
        if same_commit(g.head_sha, commit) or await _published_before(
                session, post, repo, branch, commit):
            return
        was, banked = g.head_sha, g.clean_runs
        now = _utcnow()
        _restart(g, commit, now)
        g.updated_by, g.updated_at = post.author, now
        session.add(_answer(
            post, repo, branch, commit,
            f"gate on {branch} restarts at {commit[:12]} "
            f"(was {was[:12]}, {banked}/{g.exit_bar} clean)",
            f"Post #{post.id} published {commit} on {branch}. The gate was at {was} "
            f"with {banked} of {g.exit_bar} clean runs; those were on the old head and "
            "no longer count. Runs recorded against the old head are refused from now "
            "on."))
