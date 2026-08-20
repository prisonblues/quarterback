"""The claim key, derived from the resource — never composed by the caller.

``claims()`` returned ``[]`` fleet-wide for four months while thirteen agents
worked three shared checkouts (#172). One reason was that nothing automatic ever
wrote a claim; the other is this module's subject, and it is the one that made
the claims that *did* exist useless:

**Two agents describing one collision produced two keys.** The plan wrote
``kind='work', key='prisonblues/quarterback#163'``. An agent claiming the same
issue by hand wrote ``kind='issue'`` with the same string. Same repo, same issue,
same second — and ``plan_read`` reported ``"claim": null`` with ``claimed: 0``
while ``claims()`` showed the live row. The unique index cannot help: it is
UNIQUE on ``(kind, key)``, so two spellings are two resources by construction.

The fix is the same one #148 applied to the repo name, one level up: **stop
asking for a name.** A caller says *which resource* — an issue, a PR, a branch, a
plan — and the key is read off it here, in one place. Anything already composed
is folded onto the derived form by :func:`canonical` at the edge, so the two
paths cannot disagree even for a caller that has not been updated.

What is deliberately NOT normalised: a key this module does not recognise passes
through untouched. The namespace is open on purpose — a real claim on the board
reads ``prisonblues/lexray:serving-row:32022R2554``, which is a database row and
nothing here should pretend to understand it. Canonicalisation that guessed at an
open domain is the mistake #148's own fix was reverted for; this recognises a
closed set of shapes and leaves the rest alone.

## Worktree blindness is the point

Every key here names a **repo-global** resource: an issue, a PR, a branch, a plan.
There is one ``prisonblues/quarterback#172`` however many checkouts exist on
however many machines, so two agents in different worktrees *should* collide on
it. No path, machine or worktree ever enters a key in this module.

The inverted case — an uncommitted file, contended only by agents sharing a
directory — must never enter this namespace, because a path key would block an
agent that is entirely free. That is #185, and it keys on
``<machine>:<worktree>:<relpath>`` in a namespace of its own.
"""

from __future__ import annotations

import re
import uuid

#: Every claim on a unit of work lands under one kind. The kind used to carry the
#: distinction (``issue`` vs ``work`` vs ``task``) and that is exactly what split
#: the namespace: ``(kind, key)`` is the unique index, so a second kind for the
#: same resource is a second resource. The *shape of the key* carries the
#: distinction instead, where the index can see it.
WORK = "work"

#: A branch being landed. Not folded into :data:`WORK` — landing a branch and
#: doing the issue behind it are two resources, held at different times by
#: possibly different agents, and ``preland.check_merge_claim`` reads this kind by
#: name.
MERGE = "merge"

#: Kinds a caller may spell that mean "a unit of work". Folded onto :data:`WORK`.
#: ``plan`` is here as a *kind* alias only — a plan's key is ``plan:<uuid>``, and
#: the prefix rather than the kind is what keeps it apart from an issue.
WORK_KINDS = frozenset({WORK, "issue", "item", "task", "plan", "epic"})

#: Kinds a caller may spell that mean "a pull request". Kept apart from
#: :data:`WORK_KINDS` because a PR and an issue can share a number, so they
#: cannot share a key — see :data:`PR_SIGIL`.
PR_KINDS = frozenset({"pr", "pull", "review"})

#: Kinds a caller may spell that mean "landing this branch".
MERGE_KINDS = frozenset({MERGE, "branch", "land"})

#: What separates the repo from the number for a PR. ``#`` is the issue's, and it
#: was already in use by the plan, by the dashboards' ``issue_claims`` join and by
#: every claim agents have taken by hand — so the PR needed the new sigil, not the
#: issue. ``!`` cannot occur in a GitHub owner, repository or branch name, which
#: is what makes it safe to parse back out.
PR_SIGIL = "!"

#: ``owner/name``, and nothing else — #148's refusal, moved here because the key
#: is now derived in this module and the shape rule belongs beside it.
#:
#: The allocator handed out 2.36 twice because its key was caller-supplied text:
#: one agent called itself ``quarterback`` and another ``prisonblues/quarterback``,
#: so one repo grew two counters. The obvious repair is to canonicalise — accept
#: every spelling and map them onto one — and it is the wrong one. That input
#: domain is open (URLs, scp syntax, ``.git`` suffixes, bare names, paths), and an
#: open domain cannot be enumerated: three review rounds on that parser produced
#: three more holes, each the previous fix overshooting. See PR #152, closed.
#:
#: Closing the domain makes the whole class impossible. Callers do not spell the
#: repo at all now — the MCP tools read it from ``remote.origin.url`` — so this
#: is the boundary check for anything reaching an endpoint another way, and the
#: right answer to a spelling it does not recognise is 422, not a guess. A bare
#: name is refused precisely because it is the ambiguous one. A trailing ``.git``
#: is refused rather than stripped: GitHub does not allow a repository name to end
#: in ``.git`` either, so the only thing that spelling can be is a clone URL's
#: suffix — and stripping it would be the first step back onto the parser this
#: replaces.
REPO_RE = re.compile(
    r"\A[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z(?<!\.git)"
)

#: The message a refusal carries. Named once so every endpoint, query parameter
#: and migration says the same thing to whoever hits it.
REPO_SHAPE = (
    "repo must be `owner/name` (e.g. `prisonblues/quarterback`). A bare name, a "
    "URL or an scp remote is refused rather than guessed at: two spellings of one "
    "repo is how the allocator issued the same number twice. The MCP tools read "
    "this from your origin remote — you should not be typing it."
)

#: The repo half of a key, as a group, for pulling one back out. Deliberately
#: looser than :data:`REPO_RE` on the boundary characters it must not consume:
#: ``#``, ``!`` and ``:`` are the separators, and a repository name may contain
#: none of them.
_REPO_GROUP = r"(?P<repo>[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99})"

_ISSUE_KEY = re.compile(rf"\A{_REPO_GROUP}#(?P<number>\d{{1,12}})\Z")
_PR_KEY = re.compile(rf"\A{_REPO_GROUP}{re.escape(PR_SIGIL)}(?P<number>\d{{1,12}})\Z")
#: A branch may contain almost anything except whitespace and a few git-reserved
#: characters; the repo group above cannot contain ``:``, so the first colon after
#: it is unambiguously the separator.
_MERGE_KEY = re.compile(rf"\A{_REPO_GROUP}:(?P<branch>\S+)\Z")
_UUID_KEY = re.compile(r"\A(?P<prefix>plan|item):(?P<id>[0-9a-fA-F-]{32,36})\Z")

#: The ref kinds :func:`derive` understands, and what each one needs. Every kind
#: has to have a key shape that cannot collide with another kind's — which is why
#: this is a closed set and adding to it is a code change, not a caller's choice.
REF_KINDS = ("issue", "pr", "branch", "plan", "item")


class BadRef(ValueError):
    """A ref that cannot be turned into a key. Callers render this as a 422."""


def canonical_repo(value: str) -> str:
    """The repo key, lowercased, or :class:`BadRef`.

    **Case is folded, and that is the one normalisation here.** It looks like the
    open-domain parser #148 deleted, and it is not the same operation: ``lower()``
    is total and its domain is closed, so unlike an alias enumeration it cannot be
    incomplete — there is no next case to discover.

    It has to happen because GitHub treats owner and repository names
    case-insensitively while preserving what you typed, so ``Acme/Widget`` and
    ``acme/widget`` are one repository with two possible remotes — which is #148
    exactly, in a spelling the shape rule alone would let through. Refusing the
    capitalised form instead was the alternative and is worse: a repo genuinely
    named ``acme/MyProject`` would be unable to claim at all.
    """
    if not isinstance(value, str) or not REPO_RE.match(value.strip()):
        raise BadRef(REPO_SHAPE)
    return value.strip().lower()


def _number(value: object) -> int:
    """An issue or PR number, from whatever spelling arrived.

    ``"#60"`` and ``"60"`` and ``60`` are one issue. ``_norm_ref`` in the plan
    router has always known that about its own column; the claim path did not, so
    an item added as ``"#60"`` and a claim taken on ``"60"`` were two keys.
    """
    text = str(value).strip().lstrip("#").strip()
    if not text.isdigit():
        raise BadRef(f"{value!r} is not an issue or pull request number")
    number = int(text)
    if number < 1:
        raise BadRef("issue and pull request numbers start at 1")
    return number


def _uuid(value: object) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as e:
        raise BadRef(f"{value!r} is not an id") from e


def _branch(value: object) -> str:
    """A branch name, as git would accept it — no whitespace, and not empty.

    Case is NOT folded. Repository names are case-insensitive on GitHub and
    branch names are not: ``main`` and ``Main`` are two refs, and folding them
    would let an agent landing one hold the claim on the other.
    """
    if not isinstance(value, str):
        raise BadRef("a branch is a string")
    branch = value.strip()
    if not branch or any(c.isspace() for c in branch):
        raise BadRef("a branch name cannot be blank or contain whitespace")
    return branch


def derive(ref_kind: str, *, repo: str | None = None, value: object = None) -> tuple[str, str]:
    """``(kind, key)`` for a resource, read off the resource. The only maker of keys.

    >>> derive("issue", repo="PrisonBlues/Quarterback", value="#172")
    ('work', 'prisonblues/quarterback#172')
    >>> derive("pr", repo="prisonblues/quarterback", value=207)
    ('work', 'prisonblues/quarterback!207')
    >>> derive("branch", repo="prisonblues/quarterback", value="feat/issue-172")
    ('merge', 'prisonblues/quarterback:feat/issue-172')

    An issue keeps the shape agents already take by hand and the dashboards
    already parse (``owner/name#n``) — deriving it is about who computes it, not
    about renaming it. Everything that reads a key today keeps working; what
    changes is that there is now exactly one way to produce one.
    """
    kind = (ref_kind or "").strip().lower()
    if kind == "issue":
        return WORK, f"{canonical_repo(repo or '')}#{_number(value)}"
    if kind in PR_KINDS:
        return WORK, f"{canonical_repo(repo or '')}{PR_SIGIL}{_number(value)}"
    if kind in MERGE_KINDS:
        return MERGE, f"{canonical_repo(repo or '')}:{_branch(value)}"
    if kind in ("plan", "item"):
        # A plan and an item are board objects, not repo objects: their id is
        # already globally unique, so the repo adds nothing and would only give
        # the same row two keys depending on whether the caller sent one.
        return WORK, f"{kind}:{_uuid(value)}"
    raise BadRef(f"{ref_kind!r} is not a resource this board keys: {', '.join(REF_KINDS)}")


def canonical(kind: str, key: str) -> tuple[str, str]:
    """Fold a caller-COMPOSED pair onto the derived one. Unrecognised pairs pass through.

    This is the compatibility half, and the half that fixes the observed defect:
    an agent posting ``kind='issue', key='prisonblues/quarterback#163'`` writes
    the same row the plan reads, without knowing anything changed.

    >>> canonical("issue", "PrisonBlues/Quarterback#163")
    ('work', 'prisonblues/quarterback#163')
    >>> canonical("pr", "acme/widget#5")
    ('work', 'acme/widget!5')
    >>> canonical("work", "acme/widget:serving-row:32022R2554")
    ('work', 'acme/widget:serving-row:32022R2554')

    Note the third: ``kind='work'`` is not enough to make a key a merge key, or
    the real board claim on a database row would have been rewritten into a claim
    on a branch called ``serving-row:32022R2554``. Only :data:`MERGE_KINDS` fold
    onto :data:`MERGE`.
    """
    kind = (kind or "").strip()
    key = (key or "").strip()
    lowered = kind.lower()

    if lowered in PR_KINDS:
        # A PR spelled with the issue sigil is still a PR: the kind is the only
        # thing that can tell them apart in a composed key, so it decides.
        m = _ISSUE_KEY.match(key) or _PR_KEY.match(key)
        if m:
            return derive("pr", repo=m.group("repo"), value=m.group("number"))
    if lowered in WORK_KINDS:
        m = _PR_KEY.match(key)
        if m:
            return derive("pr", repo=m.group("repo"), value=m.group("number"))
        m = _ISSUE_KEY.match(key)
        if m:
            return derive("issue", repo=m.group("repo"), value=m.group("number"))
        m = _UUID_KEY.match(key)
        if m:
            return derive(m.group("prefix"), value=m.group("id"))
    if lowered in MERGE_KINDS:
        m = _MERGE_KEY.match(key)
        if m:
            return derive("branch", repo=m.group("repo"), value=m.group("branch"))
    # Not a shape this board keys. Left exactly as it arrived — see the module
    # docstring on why an open domain is not canonicalised.
    return kind, key


def repo_of(kind: str, key: str) -> str | None:
    """Which repository a claim is against, or None if the key does not say.

    The join the fleet did not have. ``GET /claim/held`` answers "does this agent
    hold anything in this repo" off this, and it has to be derived from the key
    rather than stored beside it for the same reason the key itself is: a
    ``repo`` column the caller fills in is a second spelling waiting to happen.

    A plan or item key returns None — those are board objects and may span repos
    (the open question at the end of #172). A caller asking "am I holding
    anything here" therefore gets a truthful "not via this key", and the plan
    router answers the plan-scoped question itself.
    """
    canon_kind, canon_key = canonical(kind, key)
    patterns = ((_ISSUE_KEY, _PR_KEY) if canon_kind == WORK
                else (_MERGE_KEY,) if canon_kind == MERGE
                else ())
    for pattern in patterns:
        m = pattern.match(canon_key)
        if m:
            return m.group("repo").lower()
    return None
