"""One repo, one namespace: canonicalising the repo half of a claim key.

The allocator in :mod:`app.api.claims` exists so that two branches cannot mint
the same release number, and its atomicity is a unique index over ``(kind,
key)``. **A unique index is only unique within a spelling.** The key is built
from a repo string the caller supplies as free text, and this fleet supplies two
of them for one repo, each locally correct:

* ``qb-hook`` derives identity from the origin remote and takes the *basename*
  (``quarterback``), because a checkout cloned to a different local name is still
  the same repo to everyone else;
* ``gh`` and every review payload use GitHub's ``nameWithOwner``
  (``prisonblues/quarterback``), which is what ``POST /review`` documents.

So the board kept two independent sequences over one repo and neither could see
the other. On 2026-08-16 it handed **2.36 to two agents 28 minutes apart**, both
with ``claimed: true``, which is worse than the announcement it replaced: an
announcement at least leaves the caller uncertain. That is #148/#150, and it is
the allocator's own failure mode arriving through the one input nobody checked.

**Normalise, do not advise.** "Callers should pass ``nameWithOwner``" is a prose
control, and the callers live in another repo (``qb`` is in nix-fleet) that is
not released with this one. A namespace a typo can fork is not a namespace.

**Canonical form is ``owner/name``, lowercased.** ``nameWithOwner`` rather than
the basename because the basename is genuinely ambiguous —
``prisonblues/quarterback`` and ``someone-else/quarterback`` collapse onto one
key, which is a *worse* bug hiding in the same place. Lowercased because GitHub
resolves owners and names case-insensitively, so ``PrisonBlues/Quarterback`` is
a third fork of the namespace waiting to happen.

**The host is deliberately discarded, and this board is GitHub-shaped.**
``gitlab.com/prisonblues/quarterback``, ``git@bitbucket.org:prisonblues/quarterback``
and ``https://github.com/prisonblues/quarterback`` all reduce to
``prisonblues/quarterback``. That is right here — every caller on this fleet
points at one forge, and keeping the host would restore the two-spellings bug
under a different name, since ``gh`` never sends one. It is worth saying out loud
because "the same owner/name on a different forge" is a real way to get two repos
onto one key, and it is the same class of bug as the one being fixed: if this
board ever coordinates across forges, the host has to come back into the key,
not be inferred at the edges.

Everything here is pure string work with no imports from the rest of the app, so
the data migration that rewrites the historical rows can use exactly the code the
endpoints use rather than a second implementation that agrees with it today.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

#: GitHub's grammar for an OWNER (a user or an organisation). Alphanumeric and
#: hyphen only — an owner cannot contain ``.`` or ``_``, which is not a detail:
#: it is the fact that lets a dotted leading segment be read as a hostname
#: (``github.com/owner/name``) with no ambiguity at all.
_OWNER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9-]{0,38}\Z")

#: GitHub's grammar for a repo NAME, which is looser than an owner's: ``.`` and
#: ``_`` are both legal (``acme/my_repo``, ``acme/repo.io``). Strict on the first
#: character, because that is the character separating a repo name from ``.`` and
#: ``..`` — the loose version turned ``../etc/passwd`` into the repo
#: ``etc/passwd``, a path traversal laundered into an identity.
_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")

#: Schemes a git remote actually uses. An allow-list rather than "anything with
#: ``://``", because the parser discards the authority: with every scheme
#: accepted, ``file:///etc/passwd`` canonicalised to the repo ``etc/passwd`` and
#: any remote host could alias any other's ``owner/name``.
_GIT_SCHEMES = frozenset({"http", "https", "ssh", "git", "git+ssh"})

_SCHEME_RE = re.compile(r"\A([A-Za-z][A-Za-z0-9+.-]*)://")

#: A scheme-less host in front of the path (``github.com/owner/name``). Matched
#: rather than "has a dot in it", because ``..`` has a dot in it. A bare IPv4
#: literal is included for self-hosted remotes. A single-label host
#: (``git-server/owner/repo``) is deliberately NOT matched and cannot be: without
#: a scheme it is indistinguishable from the owner in ``owner/name``, and
#: guessing would turn every two-segment repo into a one-segment basename. Spell
#: such a remote with its scheme (``ssh://git-server/owner/repo``), where the
#: authority is delimited and no guess is needed.
_HOST_RE = re.compile(
    r"\A(?:[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
    r"|\d{1,3}(?:\.\d{1,3}){3})\Z")

#: Where a claim key stops being a repo and starts being the resource:
#: ``<repo>:<branch>`` for a merge claim, ``<repo>:<version>`` for a release,
#: ``<repo>#<issue>`` for the work claims agents take by hand, and ``@`` because
#: a caller that writes ``acme/thing@v1.2`` means the same repo as one that
#: writes ``acme/thing#142`` — a separator the splitter cannot see is a silent
#: opt-out of the whole fix. The ``@`` of ``git@github.com:owner/name`` is inside
#: the authority and is never reached; see :func:`_split_authority`.
_KEY_SEPARATORS = ":#@"


def _strip_git_suffix(path: str) -> str:
    """``…/name.git`` -> ``…/name``, whatever case the suffix was written in.

    Case-insensitively, because the fold happens later: checking ``endswith(".git")``
    against the raw input left ``prisonblues/quarterback.GIT`` carrying a literal
    ``.git`` into the canonical form, and ``repo_basename('Quarterback.GIT')``
    returning ``quarterback.git``, which matches no repo this board knows.
    """
    return path[:-4] if path[-4:].lower() == ".git" else path


def _split_authority(given: str) -> tuple[str | None, int] | None:
    """``(scheme, index where the path begins)`` for a remote spelling, else None.

    **The one place URL and scp syntax is parsed**, because there used to be two
    and they disagreed: :func:`_remote_path` tested ``_SCHEME_RE`` while
    :func:`split_repo_head` tested for ``"://" in key``, so a key like
    ``https://github.com/acme/repo:2.36`` was opaque to the splitter — never
    canonicalised, never matched by a prefix scan, and therefore a release number
    the allocator could hand out a second time.

    ``scheme`` is None for scp syntax (``git@github.com:owner/name``), which has
    no scheme but does have an authority. It is returned rather than swallowed so
    the caller can apply its own policy: :func:`_remote_path` refuses a scheme
    that is not a git transport, while :func:`split_repo_head` only needs to know
    where the authority ends so it does not cut a key at the host's colon.
    """
    m = _SCHEME_RE.match(given)
    if m:
        slash = given.find("/", m.end())
        return m.group(1).lower(), (len(given) if slash < 0 else slash + 1)
    # scp-like: git@github.com:owner/name.git. The ``@`` must come before the
    # colon AND both must be in the first path segment, or ``acme/thing@v1.2``
    # (no colon at all) gets swallowed whole and opts out of canonicalisation.
    head = given.partition("/")[0]
    at, colon = head.find("@"), head.find(":")
    if 0 <= at < colon:
        return None, colon + 1
    return None


def _remote_path(given: str) -> str | None:
    """A git remote URL reduced to its ``owner/name`` path, or None if it is not one.

    Callers derive repo identity from ``git remote get-url``, so a URL reaching
    this board is a spelling of the same repo rather than a mistake — and
    refusing it would push the caller back to the basename, which is the
    ambiguous form this module exists to get away from.

    Two things are refused outright rather than reduced. A scheme that is not a
    git transport, because the authority is thrown away and ``file:///etc/passwd``
    is not a repo called ``etc/passwd``. And a scheme-less path beginning with
    ``/``, for the same reason: ``/etc/passwd`` has exactly the shape of
    ``owner/name`` and is a filesystem path, so an absolute path must not be able
    to launder itself into an identity. A real remote always carries a scheme or
    scp syntax, so nothing legitimate is lost.
    """
    s = given.strip()
    split = _split_authority(s)
    if split is not None:
        scheme, start = split
        if scheme is not None and scheme not in _GIT_SCHEMES:
            return None
        return _strip_git_suffix(s[start:].strip("/"))
    if s.startswith("/"):
        return None
    return _strip_git_suffix(s.rstrip("/"))


def _segments(given: str) -> list[str] | None:
    """``given`` as repo path segments, host dropped, or None if it is not one.

    Each position is checked against its own grammar, because they differ: an
    owner cannot contain ``.`` or ``_`` and a repo name can. Applying the looser
    one to both is what let ``github.com/quarterback`` canonicalise with
    ``github.com`` as its owner.
    """
    path = _remote_path(given)
    if path is None:
        return None
    parts = [p for p in path.split("/") if p]
    # A scheme-less host (``github.com/owner/name``, ``github.com/name``): owners
    # cannot contain a dot on GitHub, so a hostname in front of the path is a
    # host and never an owner — at two segments as much as at three. Only
    # stripping it at three left ``github.com/quarterback`` reading as a repo
    # owned by ``github.com``.
    if len(parts) >= 2 and _HOST_RE.match(parts[0]):
        parts = parts[1:]
    if len(parts) == 1:
        return parts if _NAME_RE.match(parts[0]) else None
    if len(parts) == 2:
        return parts if _OWNER_RE.match(parts[0]) and _NAME_RE.match(parts[1]) else None
    return None


def canonical_repo(given: str | None) -> str | None:
    """``owner/name``, lowercased — or None when ``given`` is not that.

    None covers three different inputs on purpose, because the caller's response
    to each is the same: it cannot key anything on this string. A bare basename
    (``quarterback``) needs :func:`repo_basename` and a lookup; a three-segment
    path is not GitHub's grammar; anything else is not a repo at all.
    """
    if not isinstance(given, str):
        return None
    parts = _segments(given)
    if parts is None or len(parts) != 2:
        return None
    return f"{parts[0].lower()}/{parts[1].lower()}"


def repo_basename(given: str | None) -> str | None:
    """The bare ``name`` half of a one-segment spelling, lowercased, or None.

    This is the ``qb-hook`` spelling. It is not a namespace — it is a lookup key
    for one, which is the whole distinction #148 turns on.
    """
    if not isinstance(given, str):
        return None
    parts = _segments(given)
    return parts[0].lower() if parts is not None and len(parts) == 1 else None


def name_half(repo: str) -> str:
    """``prisonblues/quarterback`` -> ``quarterback``. The basename a repo answers to."""
    return repo.rpartition("/")[2]


def lookup_name(given: str) -> str | None:
    """The basename any repo spelling answers to — canonical, bare or URL — else None.

    One question asked in one place: "if this string names a repo, what is that
    repo called?" Both ``acme/thing`` and ``thing`` answer ``thing``, which is
    what a comparison between a caller's spelling and a stored key's spelling
    actually needs when neither can be expanded to an owner.
    """
    canon = canonical_repo(given)
    return name_half(canon) if canon is not None else repo_basename(given)


def split_repo_head(key: str) -> tuple[str, str]:
    """A claim key split into its repo head and the rest, separator kept.

    ``prisonblues/quarterback#142`` -> ``("prisonblues/quarterback", "#142")``;
    ``portainer-stack-189`` -> ``("portainer-stack-189", "")``.

    **The authority is parsed, not stepped around.** The first ``:`` in
    ``git@github.com:owner/name`` belongs to the host, and cutting there would
    make ``git`` the repo head. The previous guard answered that by returning any
    key containing ``://`` or ``@`` whole and untouched — which meant
    ``https://github.com/acme/repo:2.36`` was never canonicalised by the
    migration and never matched by a prefix scan, so 2.36 could be issued again.
    :func:`_split_authority` says where the authority ends; the separator is
    looked for after it.
    """
    split = _split_authority(key)
    start = 0 if split is None else split[1]
    cuts = [key.index(c, start) for c in _KEY_SEPARATORS if c in key[start:]]
    if not cuts:
        return key, ""
    at = min(cuts)
    return key[:at], key[at:]


def version_tail(key: str) -> str:
    """The part of a release key after the last ``:`` — its version, unparsed.

    Sliced off the end rather than by removing a known repo prefix, because a row
    can predate normalisation and be keyed on a spelling the caller did not send.
    A canonical repo never contains ``:``, so the last one is always the version's.
    """
    return key.rpartition(":")[2]


def like_escape(value: str) -> str:
    """``value`` as a LIKE literal, with the wildcards escaped.

    ``_`` and ``%`` are LIKE wildcards and both occur in real repo names, so
    ``acme/my_repo`` matched ``acme/myXrepo`` — one repo's allocation floor
    raised by another's (v2.33's F19). Escaped with ``\\``, which callers must
    pass as ``escape="\\\\"``.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def like_prefix(value: str) -> str:
    """A LIKE pattern matching keys under ``value:``."""
    return f"{like_escape(value)}:%"


def resolve_against(given: str, known: set[str] | frozenset[str]) -> tuple[str | None, list[str]]:
    """``(canonical repo, candidates)`` for ``given``, expanding a basename if it can.

    ``candidates`` is populated only on a REFUSAL, so the two elements never both
    carry information: a success returns ``(repo, [])`` whether it came from a
    canonical spelling or from an unambiguous basename, and a refusal returns
    ``(None, what the basename matched)`` so the caller can name the choices
    instead of asking the caller to guess what the board knows. Zero candidates
    and several are both refusals — an unknown basename must not coin a
    namespace, and an ambiguous one must not pick an owner.
    """
    canon = canonical_repo(given)
    if canon is not None:
        return canon, []
    base = repo_basename(given)
    if base is None:
        return None, []
    matches = sorted(r for r in known if name_half(r) == base)
    return (matches[0], []) if len(matches) == 1 else (None, matches)


def identified_repo(given: str, known: set[str] | frozenset[str]) -> str | None:
    """The canonical repo ``given`` names, but only if this board has SEEN it.

    Stricter than :func:`resolve_against`, and the difference is the whole reason
    a generic claim key can be trusted to survive this module. ``resolve_against``
    accepts any string shaped like ``owner/name``, which is right where the caller
    has declared the field to be a repo (``ReleaseClaimIn.repo``) — a brand-new
    repo has to be nameable in full the first time or nothing can ever be claimed
    for it. It is wrong where the string is the caller's own vocabulary: any
    two-segment key would be "a repo", so ``Prod/Blue:resource`` was silently
    rewritten to ``prod/blue:resource``.

    The cost is stated rather than hidden: the very first generic claim naming a
    repo this board has never seen keeps the caller's case. Two agents racing
    that first claim under different case would take two claims. That is a
    narrower window than the one it closes — it lasts until the repo's first
    release claim or review run, both of which put it in ``known`` for good — and
    it never mangles a key that is not about a repo at all.
    """
    repo, _ = resolve_against(given, known)
    return repo if repo in known else None


def known_repos_from(strings: Iterable[str | None]) -> set[str]:
    """The expansion table for a basename, from strings a generic claim cannot forge.

    Feed this ``review_runs.repo`` and ``kind='release'`` claim keys, and nothing
    else. It used to be fed every key in ``resource_leases`` regardless of kind,
    which made the table writable by anybody: a legal ``kind='deploy',
    key='attacker/thing#1'`` minted the repo identity ``attacker/thing``, and a
    later release request for the bare basename ``thing`` was then routed to it —
    or refused as ambiguous, which is a denial of service on somebody else's
    basename. Review runs are written by the review pipeline and ``release`` keys
    only by the allocator (``POST /claim`` refuses ``kind='release'``; see
    ``RESERVED_KINDS``), so neither can be minted by asking.
    """
    found = (canonical_repo(split_repo_head(s)[0]) for s in strings if isinstance(s, str))
    return {r for r in found if r is not None}


def canonical_key_of(key: str, known: set[str] | frozenset[str]) -> str:
    """``key`` with its repo head canonicalised, or ``key`` unchanged.

    Identified rather than merely parsed — see :func:`identified_repo`. Shared by
    the endpoint that writes new keys and the migration that rewrites old ones,
    so the two cannot drift into disagreeing about what a key means.
    """
    head, rest = split_repo_head(key)
    repo = identified_repo(head, known)
    return f"{repo}{rest}" if repo is not None else key


# ------------------------------------------------- the historical rows (0020)


@dataclass(frozen=True)
class LeaseRow:
    """One ``resource_leases`` row, as much of it as a rewrite needs."""

    id: uuid.UUID | str
    kind: str
    key: str
    acquired_at: datetime
    #: ``released_at IS NULL`` — **the unique index's predicate, exactly**, and
    #: not "is this claim live". The index cannot test ``expires_at`` (a partial
    #: predicate must be immutable), so an expired-but-unswept row still occupies
    #: its key and a rewrite onto that key still fails. Planning against liveness
    #: instead would abort the migration on precisely the rows it exists for. The
    #: caller sweeps the CONTENDED seats before it gets here — the only ones where
    #: the index can bite — so the two coincide for every row that matters.
    held: bool
    #: Who was holding it, carried only so the migration can name the agents whose
    #: number it took away. Not read by :func:`plan_rewrites`.
    holder: str | None = None
    session: str | None = None


@dataclass(frozen=True)
class Rewrite:
    """What migration 0020 should do to one row."""

    id: uuid.UUID | str
    old_key: str
    new_key: str
    #: Release this row as part of the rewrite. Set only when two LIVE rows
    #: converge on one key — which is not a migration artefact but the bug's own
    #: output, two agents each certain they held the same number.
    release: bool
    reason: str
    #: Copied off the losing row so the operator can go and tell that agent. A
    #: released loser is otherwise indistinguishable in ``GET /releases`` from any
    #: other released row, and the only signal it gets is a ``note`` suffix it
    #: sees only if it re-reads the row.
    holder: str | None = None
    session: str | None = None


def plan_rewrites(rows: list[LeaseRow], known: set[str] | frozenset[str]) -> list[Rewrite]:
    """Rewrite every row whose key names a repo by a non-canonical spelling.

    **The collision is the point, not an edge case.** Two live rows can converge
    on one key here — ``quarterback:2.36`` and ``prisonblues/quarterback:2.36``
    were both held on the day this was written — and the partial unique index
    will refuse the second. So the later-acquired one is *released as part of the
    rewrite*: it keeps its canonical key (history has to record that the number
    was handed out twice, or the floor forgets it) and stops being live, which is
    first-claim-wins applied to a fact that was already true and merely
    unrepresentable.

    Rows whose head this board cannot identify as a repo are left exactly as they
    are. They cannot grow: the endpoints refuse that spelling now, so nothing new
    lands beside them, and guessing at an owner would be the third namespace this
    whole change exists to prevent.

    **The returned order is part of the contract: every release comes first.**
    Apply a rewrite before the duplicate it converges on has been released and
    the unique index refuses it mid-migration — the loser is still holding the
    key at that instant, even though this plan has already decided it should not
    be. Applying releases first frees each seat before anything moves into it.
    """
    plans: list[Rewrite] = []
    held_keys: dict[tuple[str, str], datetime] = {}
    for row in sorted(rows, key=lambda r: (r.acquired_at, str(r.id))):
        new_key = canonical_key_of(row.key, known)
        if not row.held:
            if new_key != row.key:
                plans.append(Rewrite(row.id, row.key, new_key, False, "canonicalised",
                                     row.holder, row.session))
            continue
        seat = (row.kind, new_key)
        if seat in held_keys:
            # The winner is named by WHEN it was taken rather than by its key.
            # Both spellings canonicalise to the same string by this point, so
            # quoting the other row's key would print the same text twice and
            # explain nothing — the acquisition time is the fact that decided it.
            plans.append(Rewrite(
                row.id, row.key, new_key, True,
                f"released by 0020 (#148): taken as {row.key!r}, which is the same "
                f"claim as {new_key!r} — already held since "
                f"{held_keys[seat].isoformat()} by an earlier claim under the "
                f"other spelling of this repo",
                row.holder, row.session))
            continue
        held_keys[seat] = row.acquired_at
        if new_key != row.key:
            plans.append(Rewrite(row.id, row.key, new_key, False, "canonicalised",
                                 row.holder, row.session))
    return sorted(plans, key=lambda p: not p.release)
