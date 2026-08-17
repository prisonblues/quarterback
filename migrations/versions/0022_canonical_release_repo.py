"""One repo, one spelling — resolve the legacy release keys, once.

`kind='release'` keys are `<repo>:<version>`, and the repo half used to be
whatever the caller typed. Two agents typed the two true answers for one repo —
`quarterback` from the directory they stood in, `prisonblues/quarterback` from
the remote — so the allocator kept two counters and handed out 2.36 twice (#148,
#150).

The fix is not in this file. It is that callers no longer spell the repo at all:
the MCP release tools read `owner/name` from `remote.origin.url` (which
`sync_status` and `report_git` were already doing), and the endpoints refuse any
other shape with a 422. This migration only deals with the rows written before
that, and it is deliberately the *only* place resolution happens.

**Why resolve here rather than at read time.** The rejected design was to accept
every spelling forever and reconcile them on every read — enumerate the aliases a
repo can sit under and union them into the query. That is an open set (bare
names, `.git` suffixes, URLs, scp remotes, mixed case) and three review rounds on
PR #152 found three more holes in the enumeration, each one the previous fix
overshooting. An alias set that can be incomplete will be. Resolving once, on
write, makes it empty by construction.

**Why this can refuse.** A bare name is only resolvable if exactly one canonical
repo on this board shares its name half. Zero, or more than one, and there is no
answer — and inventing one is the guess this release exists to delete. So the
migration stops and names the rows. A one-time refusal that a human answers is
better than a permanent read-time guess nobody revisits: the deploy blocks, which
is loud, instead of the numbering quietly drifting, which is not.

On the board this was written against there are two such rows (`quarterback:2.36`
and `quarterback:2.32`) and one canonical repo with the name half `quarterback`,
so both resolve and nothing is asked of anyone.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-17
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels = None
depends_on = None

#: Spelled out rather than imported from ``app.api.claims``. A migration is a
#: snapshot of what the schema meant on the day it ran, and a rule it imports can
#: be edited afterwards — which would silently change what this revision did.
#:
#: It must match ``_REPO_RE`` there **as of this revision**, including the
#: ``.git`` refusal. A migration whose idea of a repo is looser than the app's
#: writes rows the app then refuses to read, which is the original bug wearing a
#: different hat.
_REPO_RE = re.compile(
    r"\A[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z(?<!\.git)"
)


def _split(key: str) -> tuple[str, str] | None:
    """``(repo, version)`` for a release key, or None if it has no version tail.

    Split on the LAST colon: an scp-style repo half contains one of its own, and
    cutting at the first would make `git@github.com:acme/thing` into the repo
    `git@github.com`. Those rows are exactly the ones this migration is here to
    catch, so mis-splitting them would hide them.
    """
    repo, sep, version = key.rpartition(":")
    return (repo, version) if sep else None


def plan_rewrites(rows: list[tuple[object, str, object]]) -> list[tuple[object, str]]:
    """``[(id, new_key)]`` for the legacy rows, or raise saying what a human must do.

    Pure, and separated from :func:`upgrade` so it can be tested against the row
    shapes that actually exist rather than only against an empty table. A
    migration whose only exercise is "it ran on a fresh database" has tested the
    branch that does nothing.

    `rows` is ``(id, key, released_at)``.
    """
    parsed = [(rid, split, rel) for rid, key, rel in rows if (split := _split(key))]
    # Lowercased, because the endpoint folds case: `Acme/Widget` and `acme/widget`
    # are one GitHub repository, so leaving an existing capitalised row alone would
    # hide it from every query the app makes after this and drop it out of the
    # allocation floor. Folding here is the same total operation, applied once.
    canonical = {repo.lower() for _, (repo, _v), _rel in parsed if _REPO_RE.match(repo)}
    stranded = [(rid, repo, version, rel)
                for rid, (repo, version), rel in parsed if not _REPO_RE.match(repo)]
    recase = [(rid, f"{repo.lower()}:{version}")
              for rid, (repo, version), _rel in parsed
              if _REPO_RE.match(repo) and repo != repo.lower()]
    if not stranded:
        return recase

    # A bare name resolves only if exactly one canonical repo owns that name half.
    by_name: dict[str, set[str]] = {}
    for repo in canonical:
        by_name.setdefault(repo.split("/", 1)[1], set()).add(repo)

    plans: list[tuple[object, str, object]] = []
    unresolved: list[str] = []
    for rid, repo, version, rel in stranded:
        owners = by_name.get(repo.lower(), set())
        if len(owners) == 1:
            plans.append((rid, f"{next(iter(owners))}:{version}", rel))
        elif owners:
            unresolved.append(f"  {repo}:{version} — {len(owners)} repos share the "
                              f"name {repo!r}: " + ", ".join(sorted(owners)))
        else:
            unresolved.append(f"  {repo}:{version} — no canonical repo on this "
                              f"board is named {repo!r}")

    if unresolved:
        raise RuntimeError(
            "release rows whose repo is not `owner/name` and cannot be resolved:\n"
            + "\n".join(unresolved)
            + "\n\nName the owner by hand and re-run, e.g.\n"
              "  UPDATE resource_leases SET key = 'owner/name:2.36'\n"
              "   WHERE kind = 'release' AND key = 'name:2.36';\n"
              "This is asked once. Nothing resolves a repo spelling at read time any "
              "more, so an unresolvable row would otherwise sit in a namespace no "
              "caller can reach and silently drop out of the allocation floor."
        )

    # The partial unique index covers UNRELEASED rows only, so a rewritten key may
    # legitimately join a released row of the same name (history accumulates). Two
    # LIVE rows on one key is the real conflict, and it is refused rather than
    # picked between: the allocator's whole invariant is that a number is issued
    # once, so a migration must not decide which of two holders shipped it.
    live = {f"{repo.lower()}:{version}" for _, (repo, version), rel in parsed
            if rel is None and _REPO_RE.match(repo)}
    clashes = sorted({k for _, k, rel in plans if rel is None and k in live})
    if clashes:
        raise RuntimeError(
            "resolving these rows would put two LIVE claims on one key: "
            + ", ".join(clashes)
            + "\nRelease whichever is stale (SET released_at = now()) and re-run."
        )

    return recase + [(rid, key) for rid, key, _rel in plans]


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT id, key, released_at FROM resource_leases WHERE kind = 'release'"
    )).fetchall()
    for rid, new_key in plan_rewrites([(r.id, r.key, r.released_at) for r in rows]):
        bind.execute(sa.text("UPDATE resource_leases SET key = :k WHERE id = :i"),
                     {"k": new_key, "i": rid})


def downgrade() -> None:
    """Nothing. The old keys were a spelling, not information.

    Reversing this would mean putting a repo back under a name the endpoints now
    refuse — a row no caller could reach, in a namespace that cannot be claimed
    in. The rows are still here and still hold their numbers; only their repo
    half changed, and it changed to the one they always meant.
    """
