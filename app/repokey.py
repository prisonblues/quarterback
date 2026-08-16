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

Everything here is pure string work with no imports from the rest of the app, so
the data migration that rewrites the historical rows can use exactly the code the
endpoints use rather than a second implementation that agrees with it today.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

#: GitHub's grammar for one path segment. Owners are alphanumeric-and-hyphen;
#: repo names additionally allow ``.`` and ``_``. Checked rather than assumed:
#: a segment that is not a plausible repo component means the string is not a
#: repo, and guessing at one is how a third namespace gets coined. Deliberately
#: permissive after the first character and strict on it, because that is the
#: character that separates a repo name from ``.`` and ``..``.
_SEGMENT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")

_SCHEME_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9+.-]*://")

#: A scheme-less host in front of the path (``github.com/owner/name``). Matched
#: rather than "has a dot in it", because ``..`` has a dot in it: the loose test
#: turned ``../etc/passwd`` into the repo ``etc/passwd``, which is a path
#: traversal laundered into an identity.
_HOST_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\Z")

#: Where a claim key stops being a repo and starts being the resource:
#: ``<repo>:<branch>`` for a merge claim, ``<repo>:<version>`` for a release,
#: ``<repo>#<issue>`` for the work claims agents take by hand.
_KEY_SEPARATORS = ":#"


def _strip_git_suffix(path: str) -> str:
    return path[:-4] if path.endswith(".git") else path


def _remote_path(given: str) -> str:
    """A git remote URL reduced to its ``owner/name`` path, or the input unchanged.

    Callers derive repo identity from ``git remote get-url``, so a URL reaching
    this board is a spelling of the same repo rather than a mistake — and
    refusing it would push the caller back to the basename, which is the
    ambiguous form this module exists to get away from.
    """
    s = given.strip()
    if _SCHEME_RE.match(s):
        # scheme://[user@]host[:port]/owner/name
        s = s.split("://", 1)[1].partition("/")[2]
    elif "@" in s.split("/", 1)[0] and ":" in s.split("/", 1)[0]:
        # scp-like: git@github.com:owner/name.git
        s = s.partition(":")[2]
    return _strip_git_suffix(s.strip("/"))


def _segments(given: str) -> list[str] | None:
    """``given`` as repo path segments, host dropped, or None if it is not one."""
    parts = [p for p in _remote_path(given).split("/") if p]
    # A scheme-less host (``github.com/owner/name``): owners cannot contain a
    # dot on GitHub, so a hostname in front of two more segments is a host and
    # never an owner.
    if len(parts) >= 3 and _HOST_RE.match(parts[0]):
        parts = parts[1:]
    return parts if all(_SEGMENT_RE.match(p) for p in parts) else None


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


def split_repo_head(key: str) -> tuple[str, str]:
    """A claim key split into its repo head and the rest, separator kept.

    ``prisonblues/quarterback#142`` -> ``("prisonblues/quarterback", "#142")``;
    ``portainer-stack-189`` -> ``("portainer-stack-189", "")``.

    A head carrying ``@`` or a URL scheme is returned whole and untouched: the
    first ``:`` in ``git@github.com:owner/name`` belongs to the host, and a
    generic key is the caller's own vocabulary — normalising one by guessing at
    its shape would corrupt a key this module has no business reading.
    """
    if "://" in key or "@" in key.partition(":")[0]:
        return key, ""
    cuts = [key.index(c) for c in _KEY_SEPARATORS if c in key]
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

    The second element is only interesting when the first is None: it is what the
    basename *did* match, so a refusal can name the choices instead of asking the
    caller to guess what the board knows. Zero candidates and several are both
    refusals — an unknown basename must not coin a namespace, and an ambiguous
    one must not pick an owner.
    """
    canon = canonical_repo(given)
    if canon is not None:
        return canon, []
    base = repo_basename(given)
    if base is None:
        return None, []
    matches = sorted(r for r in known if name_half(r) == base)
    return (matches[0] if len(matches) == 1 else None), matches


# ------------------------------------------------- the historical rows (0020)


@dataclass(frozen=True)
class LeaseRow:
    """One ``resource_leases`` row, as much of it as a rewrite needs."""

    id: object
    kind: str
    key: str
    acquired_at: datetime
    #: ``released_at IS NULL`` — **the unique index's predicate, exactly**, and
    #: not "is this claim live". The index cannot test ``expires_at`` (a partial
    #: predicate must be immutable), so an expired-but-unswept row still occupies
    #: its key and a rewrite onto that key still fails. Planning against liveness
    #: instead would abort the migration on precisely the rows it exists for. The
    #: caller sweeps first, so the two coincide by the time it gets here.
    held: bool


@dataclass(frozen=True)
class Rewrite:
    """What migration 0020 should do to one row."""

    id: object
    old_key: str
    new_key: str
    #: Release this row as part of the rewrite. Set only when two LIVE rows
    #: converge on one key — which is not a migration artefact but the bug's own
    #: output, two agents each certain they held the same number.
    release: bool
    reason: str


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

    Rows whose basename cannot be expanded are left exactly as they are. They
    cannot grow: the endpoints refuse that spelling now, so nothing new lands
    beside them, and guessing at an owner would be the third namespace this whole
    change exists to prevent.

    **The returned order is part of the contract: every release comes first.**
    Apply a rewrite before the duplicate it converges on has been released and
    the unique index refuses it mid-migration — the loser is still holding the
    key at that instant, even though this plan has already decided it should not
    be. Applying releases first frees each seat before anything moves into it.
    """
    plans: list[Rewrite] = []
    held_keys: dict[tuple[str, str], datetime] = {}
    for row in sorted(rows, key=lambda r: (r.acquired_at, str(r.id))):
        head, rest = split_repo_head(row.key)
        canon, _ = resolve_against(head, known)
        new_key = f"{canon}{rest}" if canon is not None else row.key
        if not row.held:
            if new_key != row.key:
                plans.append(Rewrite(row.id, row.key, new_key, False, "canonicalised"))
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
                f"other spelling of this repo"))
            continue
        held_keys[seat] = row.acquired_at
        if new_key != row.key:
            plans.append(Rewrite(row.id, row.key, new_key, False, "canonicalised"))
    return sorted(plans, key=lambda p: not p.release)
