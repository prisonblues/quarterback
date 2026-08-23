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

What is deliberately **refused** rather than passed through: a kind this board
used to key and no longer does (:data:`RETIRED_KINDS`). Passing an unrecognised
kind through is right for a namespace nobody here owns, and wrong for one that
was deleted on purpose — ``kind='release'`` went on being writable after its
allocator, its endpoints and its tools were removed, which left the one path
still able to create the stale rows the deletion existed to stop.

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

#: A branch being landed ONTO. Not folded into :data:`WORK` — landing and doing
#: the issue behind it are two resources, held at different times by possibly
#: different agents, and ``preland.check_merge_claim`` reads this kind by name.
#:
#: **Which branch a lander names is the BASE, and #318 settled it.** ``derive``
#: keys any branch and cannot tell a head from a base, so the answer lives here
#: and in ``preland.check_merge_claim`` rather than in the validation. Two agents
#: landing two different PRs into ``main`` are the collision worth serialising;
#: keyed on their heads they hold two different keys, never see each other, and
#: both merge — which is the incident ``check_merge_claim``'s docstring cites.
#: :func:`app.api.merge_queue.merge_key` derives the queue's key the same way
#: from the same base, so the claim and the line cannot name one land two ways.
MERGE = "merge"

#: Kinds a caller may spell that mean "a unit of work". Folded onto :data:`WORK`.
#: ``plan`` is here as a *kind* alias only — a plan's key is ``plan:<uuid>``, and
#: the prefix rather than the kind is what keeps it apart from an issue.
WORK_KINDS = frozenset({WORK, "issue", "item", "task", "plan", "epic"})

#: Kinds a caller may spell that mean "a pull request". Kept apart from
#: :data:`WORK_KINDS` because a PR and an issue can share a number, so they
#: cannot share a key — see :data:`PR_SIGIL`.
PR_KINDS = frozenset({"pr", "pull", "review"})

#: Kinds a caller may spell that mean "landing onto this branch".
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
#: The owner half and the repository half, named so the second can be matched on
#: its own without a second copy of it. ``GET /worktrees?repo=`` needs that: it is
#: the one read on this board that also accepts a **bare** name — the board's own
#: posts carry no other spelling — and "bare name" has to mean the same thing there
#: as the half of a repo it is, or the widening becomes its own little parser.
_OWNER = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}"
_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"

REPO_RE = re.compile(rf"\A{_OWNER}/{_NAME}\Z(?<!\.git)")

#: The repository half alone. NOT a repo — a bare name is refused everywhere a
#: repo is asked for, for the reason above. This exists so the one place that
#: takes a bare name can check it against the same rule rather than a fresh one.
REPO_NAME_RE = re.compile(rf"\A{_NAME}\Z(?<!\.git)")

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

#: Kinds this board used to key and deliberately no longer does, with what to do
#: instead. **Not the same thing as a kind it does not recognise.** The open
#: namespace above passes through untouched because the board has no business
#: guessing at ``prisonblues/lexray:serving-row:32022R2554``; a retired kind is
#: one the board *did* own, whose writer was deleted on purpose, and whose rows
#: are the thing the deletion was for.
#:
#: ``release`` is the one. #172 deleted the allocator because the number was
#: already being taken at land from the ref being merged into — nine releases in a
#: day, no collisions — while the allocator's own rows went stale for every open
#: PR. (#122 moved that further still: the number is now applied on ``main`` after
#: the merge by ``scripts/release.py``, and no branch names one at all.) The
#: argument is that *a stale record is worse than none: it is a second answer to
#: a question that has one.* A path that still writes new ones concedes the
#: argument. The endpoints went; ``POST /claim {kind: 'release'}`` did not, and
#: canonicalising an unrecognised kind by passing it through was what carried it.
RETIRED_KINDS: dict[str, str] = {
    "release": (
        "`release` is not a kind this board keys any more (#172). The allocator "
        "was deleted because the number is taken from the CHANGELOG at the ref, "
        "and the allocator's records went stale for every PR that stayed open — a "
        "stale record is a second answer to a question that has one. Since #122 "
        "the number is applied on `main` after the merge, by scripts/release.py, "
        "and no branch names one: there is no race left to claim against."
    ),
}


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


#: The characters ``git check-ref-format`` refuses outright in a ref name: ASCII
#: control characters, space, and ``~ ^ : ? * [ \`` and DEL. Enumerated rather
#: than guessed at, and safe to enumerate because — unlike the repo-spelling
#: domain #148 deleted a parser for — this set is *closed by git*: a published
#: rule about a handful of characters, not an open space of remote spellings.
_BRANCH_BAD_CHARS = frozenset(" ~^:?*[\\\x7f") | {chr(c) for c in range(0x20)}

#: The sequence rules from the same page, as (test, why) pairs.
_BRANCH_BAD_SEQUENCES = (
    ("..", "`..` is git's range operator"),
    ("@{", "`@{` is git's reflog syntax"),
    ("//", "an empty path component"),
)


def _branch(value: object) -> str:
    """A branch name, as git would accept it — :manpage:`git-check-ref-format`.

    Case is NOT folded. Repository names are case-insensitive on GitHub and
    branch names are not: ``main`` and ``Main`` are two refs, and folding them
    would let an agent landing one hold the claim on the other.

    **The shape rule is git's, and it has to be more than "no whitespace."** This
    used to reject whitespace alone while its own docstring claimed to reject
    git-reserved characters, so ``prisonblues/quarterback:feat~1`` and
    ``…:refs/heads:x`` were claimable merge keys naming branches that cannot
    exist. A coordination key for a nonexistent ref is the #172 defect in its
    purest form: a claim two agents can never contend over, sitting in the table
    that exists to make contention visible. And the ``:`` case is worse than
    merely useless — ``_MERGE_KEY`` splits on the first colon after the repo, so
    a branch spelled with one round-trips to a *different* branch name.

    This is a rejection, never a repair: a value that is not a branch name is
    refused rather than mangled into one, for the reason :data:`REPO_RE` gives.
    """
    if not isinstance(value, str):
        raise BadRef("a branch is a string")
    branch = value.strip()
    if not branch:
        raise BadRef("a branch name cannot be blank")
    bad = sorted(c for c in set(branch) if c in _BRANCH_BAD_CHARS)
    if bad:
        raise BadRef(
            f"{branch!r} is not a branch name git would accept: it contains "
            f"{', '.join(repr(c) for c in bad)}, which `git check-ref-format` "
            f"refuses. A claim on a ref that cannot exist is a key nobody can "
            f"contend over.")
    for sequence, why in _BRANCH_BAD_SEQUENCES:
        if sequence in branch:
            raise BadRef(f"{branch!r} is not a branch name git would accept: {why}")
    if branch == "@" or branch.startswith("/") or branch.endswith(("/", ".", ".lock")):
        raise BadRef(f"{branch!r} is not a branch name git would accept")
    for part in branch.split("/"):
        if part.startswith(".") or part.endswith(".lock"):
            raise BadRef(
                f"{branch!r} is not a branch name git would accept: no component "
                f"may start with `.` or end with `.lock`")
    return branch


def derive(ref_kind: str, *, repo: str | None = None, value: object = None) -> tuple[str, str]:
    """``(kind, key)`` for a resource, read off the resource. The only maker of keys.

    >>> derive("issue", repo="PrisonBlues/Quarterback", value="#172")
    ('work', 'prisonblues/quarterback#172')
    >>> derive("pr", repo="prisonblues/quarterback", value=207)
    ('work', 'prisonblues/quarterback!207')
    >>> derive("branch", repo="prisonblues/quarterback", value="feat/issue-172")
    ('merge', 'prisonblues/quarterback:feat/issue-172')
    >>> derive("branch", repo="prisonblues/quarterback", value="main")
    ('merge', 'prisonblues/quarterback:main')

    An issue keeps the shape agents already take by hand and the dashboards
    already parse (``owner/name#n``) — deriving it is about who computes it, not
    about renaming it. Everything that reads a key today keeps working; what
    changes is that there is now exactly one way to produce one.

    A branch is keyed whatever branch it is, and both examples above are valid
    keys. **Which one a LANDER claims is the base** — see :data:`MERGE` and #318.
    That is a caller's decision, not a validation one: nothing in a ref name says
    whether it is somebody's head or somebody's trunk, so a check here would be a
    guess dressed as a rule.
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


def _fold(kind: str, key: str) -> tuple[str, str]:
    """:func:`canonical` without the retired-kind refusal. **Never raises.**

    Split out because the read paths need a total function. ``repo_of`` is called
    once per row of ``GET /claim/held``, over rows that are *already stored* — so
    it has to have an answer for every one of them, including a legacy ``release``
    row and including a key whose shape matched but whose parts do not validate.
    """
    kind = (kind or "").strip()
    key = (key or "").strip()
    lowered = kind.lower()

    try:
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
    except BadRef:
        # **Matching a shape is not being one.** The regexes above are
        # deliberately looser than the validators :func:`derive` then applies:
        # ``_REPO_GROUP`` admits ``acme/foo.git`` where ``REPO_RE`` refuses the
        # ``.git`` suffix, ``_ISSUE_KEY`` admits ``#0`` where ``_number`` starts
        # at 1, and ``_UUID_KEY`` admits 32-36 characters of hex-and-dashes that
        # are not a uuid. Rows of exactly that shape are already on the board —
        # ``_norm_scope`` used only to lower-case a repo, so a plan item scoped
        # ``acme/foo.git`` stored the key ``acme/foo.git#12``.
        #
        # So the fallthrough is the module's own rule, not a new one: a key this
        # board cannot key is left exactly as it arrived. The alternative was a
        # ValueError out of a read, which turned one legacy row into a 500 for
        # the whole of ``GET /claim/held`` and for ``GET /claims?kind=work&key=…``.
        return kind, key
    # Not a shape this board keys. Left exactly as it arrived — see the module
    # docstring on why an open domain is not canonicalised.
    return kind, key


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

    Raises :class:`BadRef` for a kind in :data:`RETIRED_KINDS`. This is the one
    place a *write* can be refused for its kind, and it is here rather than in
    front of ``POST /claim`` for the reason ``ClaimRequest`` canonicalises here:
    a guard in front of one caller is a guard the fourth caller does not have,
    which is exactly what the deleted ``RESERVED_KINDS`` was.
    """
    lowered = (kind or "").strip().lower()
    if lowered in RETIRED_KINDS:
        raise BadRef(RETIRED_KINDS[lowered])
    return _fold(kind, key)


def canonical_kind(kind: str) -> str:
    """The kind a resource of this sort is STORED under — for a kind-only filter.

    ``canonical`` needs a key to work with, because the key's shape is what
    carries the issue/PR/plan distinction now. A caller narrowing by kind alone
    has no key to offer, and every spelling the old vocabulary trained agents to
    use (``issue``, ``task``, ``item``, ``epic``, ``pr``, ``branch``) is now
    *stored* as ``work`` or ``merge`` — so an un-folded kind filter answered
    ``{"claims": []}`` about resources that were held. That is #172's own defect
    reproduced in the read path: a lookup missing a row that is right there.

    >>> canonical_kind("issue"), canonical_kind("pr"), canonical_kind("branch")
    ('work', 'work', 'merge')

    Coarser than a key lookup on purpose, and it cannot be otherwise: ``pr`` and
    ``issue`` share one kind by design, so a kind-only filter can no longer tell
    them apart. Callers say so to whoever asked (``note_on_kind``) rather than
    quietly returning either too much or nothing at all.
    """
    lowered = (kind or "").strip().lower()
    if lowered in RETIRED_KINDS:
        raise BadRef(RETIRED_KINDS[lowered])
    if lowered in WORK_KINDS or lowered in PR_KINDS:
        return WORK
    if lowered in MERGE_KINDS:
        return MERGE
    return (kind or "").strip()


def board_object(kind: str, key: str) -> tuple[str, str] | None:
    """``("plan"|"item", id)`` for a board-object key, else None. Never raises.

    :func:`repo_of` cannot attribute these and is right not to — the key is a
    board id and says nothing about a repository. The *row* does, though, so a
    caller that can reach the table can finish the join; this is how it asks
    which row. See ``GET /claim/held``.
    """
    canon_kind, canon_key = _fold(kind, key)
    if canon_kind != WORK:
        return None
    m = _UUID_KEY.match(canon_key)
    return (m.group("prefix"), m.group("id")) if m else None


def repo_of(kind: str, key: str) -> str | None:
    """Which repository a claim is against, or None if the key does not say.

    The join the fleet did not have. ``GET /claim/held`` answers "does this agent
    hold anything in this repo" off this, and it has to be derived from the key
    rather than stored beside it for the same reason the key itself is: a
    ``repo`` column the caller fills in is a second spelling waiting to happen.

    A plan or item key returns None — those are board objects and may span repos
    (the open question at the end of #172). A caller asking "am I holding
    anything here" therefore gets a truthful "not via this key", and finishes the
    join against the row itself via :func:`board_object`.

    **Total, and it has to be**: it runs once per row over rows that already
    exist, so a legacy or malformed key is an answer of None rather than an
    exception out of a read.
    """
    canon_kind, canon_key = _fold(kind, key)
    patterns = ((_ISSUE_KEY, _PR_KEY) if canon_kind == WORK
                else (_MERGE_KEY,) if canon_kind == MERGE
                else ())
    for pattern in patterns:
        m = pattern.match(canon_key)
        if m:
            try:
                return canonical_repo(m.group("repo"))
            except BadRef:
                # The key names something in the repo position that is not a repo
                # this board keys (``acme/foo.git#12``). "The key does not say
                # where" is the truthful answer, and it is the one `unattributed`
                # exists for — better than attributing a claim to a repo no
                # caller can ever name in a query.
                return None
    return None
