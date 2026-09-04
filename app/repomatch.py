"""Which repository is the caller asking about — asked once, answered everywhere.

Four reads on this board answer "which repository" the two-tier way — accepting a
bare name beside ``owner/name``, because a bare name is what the board's own posts
and leases carry. Two of them had written that rule out themselves (``GET
/worktrees``, ``GET /landing``); the other two had written nothing at all, and one
of those is the endpoint whose entire job is collision detection:

    active(repo="prisonblues/lexray")  ->  {"agents": [], "subagents": []}
    active(repo="lexray")              ->  three agents, all in that repo

``GET /active`` is documented as *"the collision index. Check this before you
start substantive work so two agents don't collide"* and *"an empty result means
the coast is clear"*, and it filtered with ``Lease.repo == repo`` (#714). So the
qualified spelling — the **only** one ``plan_read``, ``claim`` and every other
keyed surface accepts, and therefore the one an agent orienting the documented way
has just been taught — answered that nobody was there while three agents were.
That is a false clean, not a missing filter: the difference between the two is
that a caller acts on the first.

## Why the two spellings both exist

:data:`app.claimkey.REPO_RE` closed the repo-name domain for everything that keys
on a repo, and it was right to (#148): two spellings of one repo is how the
release allocator issued 2.36 twice. But ``Lease.repo`` is not a key. It is a
report — the lifecycle hook says which repo it is standing in, thirty times an
hour, and until #714 it said the **basename**, because that is what a checkout
knows without asking its origin remote. So the column holds ``lexray`` where
every keyed surface holds ``prisonblues/lexray``, and a caller cannot tell from
the outside which surface it is talking to.

Both halves of that are fixed, and neither half is sufficient alone:

* **The write** now reports ``owner/name`` (``qb-hook`` reads the origin remote,
  which it was already reading), and :func:`fold_repo` folds its case so one
  repository has one spelling in what the board stores and displays.
* **The read** matches by repository **name**, so a lease written by a hook that
  has not been upgraded — or by a checkout whose remote is not a GitHub one, where
  a basename is genuinely all there is — is still found. A collision index that
  went quiet for a fleet mid-rollout would be answering the false clean again, in
  a narrower window.

## Two tiers, one gate, and the gate is the point

The matching rule differs by column, because some of them are canonical and some
are not (see :func:`canonical_clause` against :func:`name_clause`). What must NOT
differ is the gate in front of both: a spelling that is neither ``owner/name`` nor
a bare repository name is a **422**, never an empty answer. ``GET /worktrees`` and
``GET /landing`` settled that (#326, #350) and stated the reason each time — *an
empty answer reads as "nothing gates anything here" when it means "I could not
tell what you asked about"* — and then ``/active`` accepted any string at all and
returned ``[]`` for it.

So :func:`asked_repo` is the gate, in one place, and every two-tier ``repo`` filter
on this board goes through it. A rule written out at each call site is a rule each
copy can drift from — and the copy that drifts furthest is the one nobody wrote,
which is how ``/active`` came to have no rule and no refusal. Stated once, the
``owner/name`` tier and the bare-name tier cannot come to disagree about which is
which.

The strict reads are deliberately not folded in here. ``/review/*`` and the plan
take ``owner/name`` and nothing else (``app.api.reviews._asked_repo``,
``app.claimkey.canonical_repo``), because those columns are keys and the bare name
is the ambiguity #148 closed. Widening them would undo that; this module is for
the reads over columns the board itself fills with either spelling.

## What is deliberately NOT here

Any attempt to turn a third spelling into a repo. A clone URL, an scp remote, a
path and a ``.git`` suffix are all refused rather than parsed, for the reason PR
#152 was closed: that input domain is open, three review rounds on the parser
produced three more holes, and an alias set that can be incomplete will be. The
two shapes this module knows are the two the board itself writes.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import ColumnElement, func

from app.claimkey import REPO_NAME_RE, REPO_SHAPE, BadRef, canonical_repo
from app.sync import repo_key

#: Everything after the last ``/``, as a POSIX pattern for Postgres's two-argument
#: ``substring``. Wrapped in ``rtrim``/``lower`` at the call site so the SQL says
#: exactly what :func:`app.sync.repo_key` says in Python — the two implementations
#: of one rule, and the only way they cannot drift is by being written from the
#: same sentence.
_BASENAME = "[^/]*$"


@dataclass(frozen=True)
class AskedRepo:
    """A caller's ``?repo=``, parsed once into the forms a column can be compared to.

    ``qualified`` is the folded ``owner/name`` when that is what arrived, and
    ``None`` when a bare name did — which is the whole of the two-tier rule, hoisted
    out of the comparison so no call site has to re-derive it. ``name`` is the
    repository half, folded, and is always present: it is what ``owner/name`` and a
    bare name have in common, and therefore the only thing a column that may hold
    either shape can be matched on.

    ``asked`` is what the caller actually typed, kept for the refusal payload so an
    agent that mistyped a repo sees its own string back rather than a normalisation
    of it.
    """

    asked: str
    qualified: str | None
    name: str


def asked_repo(repo: str) -> AskedRepo:
    """Parse a ``?repo=`` filter, or refuse it with a 422 — the shared gate.

    Two spellings are accepted and everything else is refused:

    * ``owner/name``, folded through :func:`app.claimkey.canonical_repo` — the
      board's own key spelling, and the one every keyed surface teaches.
    * a **bare** repository name, checked against :data:`REPO_NAME_RE`, which is the
      repository half of :data:`app.claimkey.REPO_RE` itself — so ``foo.git``,
      ``foo/``, ``/foo`` and anything with a space are refused here exactly as they
      are everywhere else.

    The whole string has to be a bare name, not merely something with a basename in
    it. :func:`app.sync.repo_key` is total and answers ``passwd`` for
    ``/etc/passwd`` and ``c`` for ``a/b/c``, so a check on its output alone would
    turn every path and clone URL into a match on whatever it ends with.

    Raises :class:`fastapi.HTTPException` rather than returning a sentinel, because
    the alternative each call site reached for on its own is an empty result — and
    an empty result is the defect. A refusal is an answer; ``[]`` is a wrong one.
    """
    try:
        qualified = canonical_repo(repo)
    except BadRef:
        qualified = None
    if qualified is not None:
        return AskedRepo(asked=repo, qualified=qualified, name=_name_of(qualified))
    asked = repo.strip()
    if not REPO_NAME_RE.match(asked):
        raise HTTPException(422, detail={"error": REPO_SHAPE, "repo": repo})
    return AskedRepo(asked=repo, qualified=None, name=_name_of(asked))


def _name_of(value: str) -> str:
    """The repository half of an already-accepted spelling, folded.

    :func:`app.sync.repo_key` is the rule and is total; the assertion is that it
    cannot answer ``None`` here, because :func:`asked_repo` has already established
    that ``value`` is a non-empty repo or repository name.
    """
    name = repo_key(value)
    assert name is not None, value  # asked_repo has already vetted the shape
    return name


def canonical_clause(column: ColumnElement[str | None], asked: AskedRepo) -> ColumnElement[bool]:
    """Narrow a column that holds ONLY ``owner/name`` — ``Worktree.repo``.

    Exactness is available on such a column and is therefore used: a qualified
    question gets a plain ``==`` against the stored key, so ``acme/widget`` and
    ``other/widget`` stay two repositories — and on ``worktrees`` that comparison is
    what keeps ``ix_worktrees_repo`` serving the query, which the model's own
    ``__table_args__`` comment says out loud. Only the bare-name tier falls back to
    the basename, and it has to, because a bare name is all a board post carries.

    The guarantee this leans on is a CHECK constraint plus
    :func:`app.claimkey.canonical_repo` on every write path — see
    ``app/api/worktrees.py``. On a column with no such guarantee the exact tier
    silently stops matching half the rows, which is what :func:`name_clause` is for.

    :func:`canonical_predicate` is the same rule for a read that cannot use a
    ``WHERE`` at all (``GET /landing``, whose scope is a graph closure).
    """
    if asked.qualified is not None:
        return column == asked.qualified
    return _stored_name(column) == asked.name


def canonical_predicate(asked: AskedRepo):
    """:func:`canonical_clause` as a Python predicate over a stored value.

    For a read whose scope is a graph closure rather than a row filter — see
    ``app.api.landing.read_graph``. One rule, two renderings, written here beside
    each other so the tiers cannot come to disagree about which is which.
    """
    if asked.qualified is not None:
        want = asked.qualified
        return lambda stored: stored == want
    return lambda stored: repo_key(stored) == asked.name


def name_clause(column: ColumnElement[str | None], asked: AskedRepo) -> ColumnElement[bool]:
    """Narrow a column that may hold EITHER spelling — ``Lease.repo``.

    Matched by repository name, both sides, because that is the only thing the two
    shapes in the column have in common. The exact tier is not merely unhelpful
    here, it is wrong: it is what answered "the coast is clear" while three agents
    were standing in the repo (#714).

    **This is wide in one specific way, and it is the right direction to be wide
    in.** Two GitHub repositories may share a repository name under different
    owners, and a question about ``acme/widget`` will report a peer live in
    ``other/widget``. For a collision index that is the cheap error: a false
    positive costs a conversation with an agent working something else, and a false
    negative costs two agents editing one tree. It is also *visible* — every row
    ``/active`` and ``/overlap`` return carries its own ``repo``, so a caller can
    see which spelling matched, which is exactly what an empty answer denied it.

    It is the rule ``/sync`` has applied to this same column since v2.8, so the two
    endpoints agree about what counts as the same repo rather than each holding half
    the fleet.
    """
    return _stored_name(column) == asked.name


def name_matches(stored: str | None, asked: AskedRepo) -> bool:
    """:func:`name_clause` as a Python predicate — for rows already in hand.

    ``/active`` attributes a sub-agent through its parent's lease, which is a second
    query rather than a column, so that half of the filter is applied here. Same
    rule, or the two halves of one answer would disagree.
    """
    return repo_key(stored) == asked.name


def _stored_name(column: ColumnElement[str | None]) -> ColumnElement[str | None]:
    """The repository half of a stored spelling, in SQL — :func:`app.sync.repo_key`.

    ``rtrim`` and ``lower`` are what make it the *same* rule rather than a similar
    one: ``repo_key`` strips trailing slashes and folds case, and a SQL expression
    that did neither would answer differently from the Python one for a value the
    column's write path does not forbid. Both are no-ops on a canonical column.
    """
    return func.lower(func.substring(func.rtrim(column, "/"), _BASENAME))


def fold_repo(value: str | None) -> str | None:
    """The one spelling a free-form ``repo`` column STORES — for a write path.

    A qualified ``owner/name`` is folded through
    :func:`app.claimkey.canonical_repo`, so ``PrisonBlues/Quarterback`` and
    ``prisonblues/quarterback`` are not two repositories in what the board stores,
    displays, and threads posts under. That is #326's rule — fold on the write —
    applied to the one reporting column it had never reached.

    **Anything else passes through untouched, and that is deliberate rather than
    lax.** This column's other legitimate shape is a bare repository name: it is
    what a checkout whose origin is not a GitHub remote genuinely has, and what
    every hook older than #714 sends. Refusing it would take the lease — and with
    it the agent's whole presence on the board — away from a heartbeat over a field
    that is optional in the first place; guessing at it would be the open-domain
    parser :data:`app.claimkey.REPO_SHAPE` exists to refuse. So the fold — plus the
    surrounding whitespace ``canonical_repo`` would have taken off the other branch,
    which is not a guess about anything — is the only normalisation, and
    :func:`name_clause` is what makes the un-folded half findable.
    """
    if value is None or not value.strip():
        return None
    try:
        return canonical_repo(value)
    except BadRef:
        return value.strip()
