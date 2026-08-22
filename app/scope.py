"""What a plan item belongs to: a scope, which may or may not be bound to a forge.

**The premise this replaces was inherited, not decided** (#323). The plan reused
the ``repo`` column and inherited :func:`app.claimkey.canonical_repo` with it, so
every plan scope had to be a GitHub repo. Two rows on the live board were house
renovation work — real work, deliberately planned, with no GitHub anything — and
the board held them badly in three separate ways: ``plan_read(repo='65lowther')``
answered 422 while the rows sat there, ``qb-reconcile`` reported them as an
unanswerable question every fifteen minutes, and the page offered to "pick a repo"
for something that is not one.

Read #148's argument carefully and it does not license that premise. It argues
that **a GitHub repo must have exactly one spelling** — the allocator issued 2.36
twice because one agent said ``quarterback`` and another
``prisonblues/quarterback``. It does not argue that every plan scope is a GitHub
repo. A bare name is ambiguous *as a repo*; it is not ambiguous as "the 65lowther
project", which is not a repo and never was.

So :data:`app.claimkey.REPO_RE` and :func:`app.claimkey.canonical_repo` are
untouched here, in every respect. This module adds a **second, disjoint**
namespace beside them and nothing else.

## Which way round this reads

A scope is a name for a body of work. That is the general case, and it needs no
forge: this server makes no outbound call to one and never has (#327 — ``app/``
holds no ``httpx``, no ``urlopen``, no ``subprocess``; every GitHub reference in
it is a docstring or a shape rule). What a repo scope adds is a **forge binding**:
by spelling itself ``owner/name`` it says "and this work lives in that GitHub
repository", which is what lets ``qb-reconcile`` compare the plan against issues,
PRs and CI. So a ``project:`` scope is not a repo scope with something taken away.
It is the plain case, and ``owner/name`` is the one carrying the extra claim.

:func:`forge_repo` is that question asked in one place, and it is the only thing
anything downstream needs to know: given a scope, is there a forge to reconcile
it against, and which repository is it?

## Two gates, and each one closes a different door

The sharp edge, and the reason this is not simply "accept a bare name when it is
not a repo": *if "this has no forge binding" is inferred from "does not match
``REPO_RE``", every mistyped repo silently becomes a brand-new scope* — and the
exact ambiguity #148 closed comes straight back in through a different door. So
nothing is ever inferred. There are two gates:

1. **The sigil.** A scope with no forge binding is spelled ``project:<name>``, and
   the two namespaces cannot overlap because a GitHub repository name may not
   contain a colon. ``prisonblues/quaterback`` is a mistyped repo and gets
   ``REPO_SHAPE``, exactly as it does today; ``65lowther`` is a mistyped repo too,
   and gets the same. Neither can become a scope by accident, because becoming one
   takes a prefix nobody types by mistake.

2. **The registry.** The sigil alone would still let an agent invent
   ``project:65lowthr`` by typo — the same defect one level in. So a ``project:``
   scope must have been *declared*, as a row, by a **person** (``POST
   /plan/scope``, behind :func:`app.auth.human`). Reordering the plan is
   human-only because the plan is the fleet's shared intent and an agent must not
   rewrite it; *what the scopes even are* is the same kind of decision, one level
   up. Agents put work into a scope; a person says a scope exists.

   A repo scope needs no such row, and that asymmetry is not an oversight: its
   name is checkable against a rule (:data:`app.claimkey.REPO_RE`) that a typo
   fails. ``project:65lowthr`` passes every rule there is; only a person can say
   whether it is a thing.

That existence check needs the database and therefore is not here — see
``app.api.plan._norm_scope``. What lives in this module is the shape rule alone,
so the spelling has one definition that the API, the migration and the reconciler
all read from.
"""

from __future__ import annotations

import re

from app.claimkey import REPO_SHAPE, BadRef, canonical_repo

#: What makes a scope declare that it carries no forge binding. A colon, because
#: GitHub allows no colon in an owner or repository name — so the two namespaces
#: are disjoint by construction rather than by convention, and no spelling can be
#: read as belonging to both. The same reasoning ``PR_SIGIL`` uses for ``!``.
#:
#: ``project`` rather than ``non_code`` or ``local``: the word a person uses out
#: loud for the thing ("the 65lowther project"), and one that names what the scope
#: IS rather than what it lacks. Nothing here is a lesser case.
PROJECT_SIGIL = "project:"

#: The name half. Deliberately NOT a general string: a scope name reaches a query
#: parameter, a ``select`` option, a board post and a claim key, and this keeps
#: every one of those free of anything needing escaping. Lower case only, folded
#: rather than refused — for the reason :func:`app.claimkey.canonical_repo` folds a
#: repo: one thing must have one spelling, which is the whole of #148 and the only
#: part of it that generalises past GitHub.
PROJECT_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,63}\Z")

#: The refusal a badly spelled ``project:`` scope carries.
PROJECT_SHAPE = (
    f"a project scope is spelled `{PROJECT_SIGIL}<name>` (e.g. "
    f"`{PROJECT_SIGIL}65lowther`), where the name starts with a letter or digit "
    "and carries only letters, digits, `.`, `-` and `_`, up to 64 characters."
)

#: The refusal anything else carries: #148's message, plus the one sentence that
#: says the other door exists. The repo half is unchanged and deliberately comes
#: first — a caller that meant a repo and mistyped it is the overwhelmingly common
#: case, and it is the case #148 was about.
SCOPE_SHAPE = (
    f"{REPO_SHAPE} If this work is not in a GitHub repo at all — house work, a "
    f"piece of admin, anything with no forge behind it — then it is a project "
    f"scope rather than a repo: {PROJECT_SHAPE} A person declares one with `POST "
    "/plan/scope`; an agent cannot, because a scope invented from a typo is a "
    "second name for work that already has one."
)


def is_project(value: str | None) -> bool:
    """Does this scope's spelling say it carries no forge binding?

    Purely syntactic, and that is what makes it usable from the reconciler, which
    sees scope strings over HTTP and has no database to consult. It answers "does
    this spelling claim to have no forge", not "is this scope declared" — the
    second is the registry's question and only the API can answer it.

    **Stripped before the test, because ``canonical_repo`` strips before its own.**
    Without it ``" project:65lowther"`` misses this branch, falls through to the
    repo rule and is refused as a badly spelled repo, while
    ``" prisonblues/quarterback"`` is accepted — one namespace tolerating leading
    space and the other not, decided by nothing.
    """
    return bool(value) and str(value).strip().lower().startswith(PROJECT_SIGIL)


def forge_repo(scope: str | None) -> str | None:
    """The GitHub repository this scope is bound to, or None if it is bound to none.

    **The one question anything downstream of a scope actually asks**, and the
    reason it is a function rather than an ``in`` test at each call site. A
    reconciler asking "why can I not ask ``gh`` about this?" has already assumed
    the answer should be yes; asking "is there a forge to reconcile this against?"
    gets a plain no for a project scope and a plain no for the fleet (which names
    nothing and never did), with no failure anywhere in the sentence.

    This server never *uses* the answer to call anything — see the module
    docstring — it publishes it, and the harness calls out.
    """
    if not scope or is_project(scope):
        return None
    try:
        return canonical_repo(scope)
    except BadRef:
        return None


def project_name(value: str) -> str:
    """The bare name out of ``project:65lowther``, canonicalised, or :class:`BadRef`.

    What a page prints and what a person says. The stored form keeps the sigil —
    see :func:`canonical_project`.
    """
    if not is_project(value):
        raise BadRef(PROJECT_SHAPE)
    name = str(value).strip()[len(PROJECT_SIGIL):].strip().lower()
    if not PROJECT_NAME_RE.match(name):
        raise BadRef(PROJECT_SHAPE)
    return name


def canonical_project(value: str) -> str:
    """``project:65lowther``, from that or from a bare ``65lowther``.

    A bare name is accepted **here and only here**, on the declaration path, where
    a person has already said which namespace they mean by calling that endpoint.
    Nowhere that takes a scope from an agent may use this function: there, a bare
    name is #148's ambiguity and has to keep meeting ``REPO_SHAPE``.
    """
    text = "" if value is None else str(value).strip()
    return PROJECT_SIGIL + project_name(
        text if is_project(text) else PROJECT_SIGIL + text)


def canonical_scope(value: str) -> str:
    """The one spelling of a scope: a repo, or a well-shaped project.

    Raises :class:`BadRef` carrying :data:`SCOPE_SHAPE` for anything else — which
    for a mistyped repo is still #148's refusal, with one sentence appended saying
    the other namespace exists. It does NOT check that a project scope has been
    declared; that is a database question, asked at the endpoint.
    """
    text = "" if value is None else str(value).strip()
    if is_project(text):
        return PROJECT_SIGIL + project_name(text)
    try:
        return canonical_repo(text)
    except BadRef:
        raise BadRef(SCOPE_SHAPE) from None
