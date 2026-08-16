#!/usr/bin/env python3
"""Deterministic Alembic migration-graph reconciler for quarterback.

`migrations/versions/` is a hand-numbered linear chain — `0017_review_provenance.py`
declares `revision = "0017"` and `down_revision = "0016"`. Several agents work this
repo at once, so the first two branches to need a schema change both write the next
number. That costs two things at once, and only the first is what lexray's reconciler
(the donor for this file) was built for:

  * **Two Alembic heads** — `alembic upgrade head` refuses to run, and the deployed
    database is left unable to advance.
  * **A duplicate revision id.** lexray's revisions are hash-named, so two branches
    can never pick the same one. Here the id *is* the number, so both branches write
    `revision = "0018"`. Git conflicts on neither (the filenames differ), and a
    graph-only reconciler reports the merge CLEAN: id `0018` is present at both refs
    with the same `down_revision`, so nothing looks rewritten, and the branch's real
    work is excluded from `branch_new` as "already present". The wrong answer is the
    reassuring one, which is why this case is detected before anything else.

So quarterback's resolution is **renumber-and-relink**, not lexray's relink: rename
the file, rewrite `revision`, and rewrite `down_revision` onto the integration head.

Everything is computed from the migration files **at a git ref, never from a live
database**. The deployed database is at whatever revision the last Portainer deploy
left it and no local one need agree; a resolution that is only valid from a
particular starting revision is not a resolution.

The tool chooses the action. A caller that overrides the choice silently is the bug
this exists to prevent — see #97.

Exit codes (preflight/apply): 0 = go (noop, relink or renumber), 3 = go via the merge
fallback (needs `alembic merge heads`), 2 = STOP, a human must reconcile first. STOP
covers: the integration ref is itself multi-head; the branch *rewrites* a migration
that already exists in shared history; the branch adds a second independent root
(`down_revision = None`); or the post-resolution graph would still be multi-head.

Assumes a single Alembic version directory (`--versions-path` for a custom
`script_location`). Multiple `version_locations` are not supported — heads would be
under-counted across the split dirs.

Usage:
    migration_reconcile.py preflight [--repo DIR] [--onto REF] [--branch REF] [--json]
    migration_reconcile.py apply     [--repo DIR] [--onto REF] [--branch REF]
    migration_reconcile.py heads     [--repo DIR] [--ref REF]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Pure graph core — no git, no DB, no filesystem. Fully unit-testable.
# ---------------------------------------------------------------------------

# Match `revision = '...'` and the modern annotated form `revision: str = '...'`
# (Alembic >=1.9 templates, which is what this repo's migrations use).
_REV_RE = re.compile(r"^revision\s*(?::[^=\n]+)?=\s*(['\"])(?P<id>[^'\"]+)\1", re.MULTILINE)

#: A revision id that encodes its own position in the chain. quarterback's whole
#: numbering convention, and the thing that makes a collision possible at all.
_NUM_ID_RE = re.compile(r"^(\d{4,})$")

#: `0017_review_provenance.py` -> ("0017", "review_provenance")
_FILENAME_RE = re.compile(r"^(?P<num>\d{4,})_(?P<slug>.+)\.py$")


@dataclass(frozen=True)
class Rev:
    """One migration node."""

    id: str
    down: tuple[str, ...] = ()  # parents; () == root (down_revision = None)
    depends: tuple[str, ...] = ()  # extra Alembic depends_on edges
    path: str | None = None  # repo-relative file path (for patching)
    #: Content hash. The graph cannot tell two *different* migrations that share an
    #: id from one migration seen at two refs; the bytes can.
    digest: str | None = None

    @property
    def is_merge(self) -> bool:
        return len(self.down) > 1

    @property
    def number(self) -> int | None:
        """The id read as a chain position, or None if it is not numbered."""
        m = _NUM_ID_RE.match(self.id)
        return int(m.group(1)) if m else None


def digest_of(text: str) -> str:
    """Content identity for a migration file. Short — this is an equality check
    between two blobs, never a security boundary."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _scan_rhs(text: str, name: str) -> tuple[str, int, int] | None:
    """Scan the assignment `name = ...` (optionally annotated `name: T = ...`).

    Returns ``(rhs, rhs_start, rhs_end)`` where ``rhs`` is the right-hand side with
    any trailing/inline ``#`` comment removed, spanning a multiline tuple/list until
    brackets balance; ``rhs_start`` is the offset of the first RHS character (just
    past ``name[: T] =``) and ``rhs_end`` is the offset just past the last captured
    RHS character (before any trailing comment/newline). Replacing
    ``text[rhs_start:rhs_end]`` swaps the value while preserving any type annotation
    and trailing comment. ``None`` if the assignment is absent.

    Comment handling is quote-aware: a ``#`` inside a string literal is data, a ``#``
    outside one starts a comment that is skipped to end-of-line. Without this, a
    quoted string inside a trailing comment (e.g. ``# was '0016'``) would be
    fabricated into a phantom revision reference.
    """
    m = re.search(rf"^{name}\s*(?::[^=\n]+)?=\s*", text, re.MULTILINE)
    if not m:
        return None
    depth, i, out = 0, m.end(), []
    last = m.end()  # offset just past the last non-comment, non-trailing-ws char
    quote: str | None = None
    while i < len(text):
        c = text[i]
        if quote is not None:  # inside a string literal — copy verbatim
            out.append(c)
            last = i + 1
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            out.append(c)
            last = i + 1
        elif c == "#":  # comment outside a string — skip to end of line
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        elif c in "([":
            depth += 1
            out.append(c)
            last = i + 1
        elif c in ")]":
            depth -= 1
            out.append(c)
            last = i + 1
        elif c == "\n" and depth <= 0:
            break
        elif c.isspace():
            out.append(c)  # interior/leading ws kept; stripped by the caller
        else:
            out.append(c)
            last = i + 1
        i += 1
    return "".join(out).strip(), m.end(), last


def _capture_rhs(text: str, name: str) -> str | None:
    """Right-hand side of `name = ...` (comment-stripped). None if absent."""
    scanned = _scan_rhs(text, name)
    return scanned[0] if scanned is not None else None


def _parse_refs(rhs: str | None) -> tuple[str, ...]:
    """Parse a revision-reference RHS: None -> (); 'x' -> ('x',);
    ('x','y') / ["x","y"] -> ('x','y')."""
    if rhs is None or rhs.lstrip().startswith("None"):
        return ()
    return tuple(re.findall(r"['\"]([^'\"]+)['\"]", rhs))


def parse_migration(text: str, path: str | None = None) -> Rev:
    """Parse a migration file's text into a Rev. Raises ValueError if it has no
    `revision = '...'` (i.e. it is not an Alembic migration)."""
    m = _REV_RE.search(text)
    if not m:
        raise ValueError("no `revision = '...'` found — not a migration")
    return Rev(
        id=m.group("id"),
        down=_parse_refs(_capture_rhs(text, "down_revision")),
        depends=_parse_refs(_capture_rhs(text, "depends_on")),
        path=path,
        digest=digest_of(text),
    )


def heads(revs: list[Rev]) -> list[str]:
    """Revision ids never referenced as a ``down_revision`` parent *by another rev in
    the set*. This matches Alembic exactly: head-ness is closed only by versioned
    ``down_revision`` edges. A ``depends_on`` edge orders application but does **not**
    close a head (Alembic's ``RevisionMap`` keeps a revision a head even when another
    revision ``depends_on`` it), so it is deliberately excluded here — folding it in
    would under-count heads and let a genuine two-head graph slip past the guard.

    References pointing outside the set (a base linking into pre-existing history) do
    not disqualify their target — it is not in the set anyway."""
    ids = {r.id for r in revs}
    referenced: set[str] = set()
    for r in revs:
        referenced.update(d for d in r.down if d in ids)
    return sorted(ids - referenced)


@dataclass(frozen=True)
class Rename:
    """One migration's renumbering. `new_path` keeps the old slug — the number is the
    only thing a collision makes wrong."""

    old_id: str
    new_id: str
    old_path: str | None
    new_path: str | None
    old_down: tuple[str, ...]
    new_down: tuple[str, ...]


@dataclass
class Plan:
    action: str  # noop | relink | renumber | merge | stop
    reason: str
    go: bool  # is landing OK once the action is applied?
    onto_head: str | None = None
    base: str | None = None  # branch rev whose down_revision gets rewritten
    base_path: str | None = None
    branch_head: str | None = None
    old_down: tuple[str, ...] = ()
    new_down: tuple[str, ...] = ()
    renames: list[Rename] = field(default_factory=list)
    collisions: list[str] = field(default_factory=list)
    guards: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        # Unknown/typo actions fail safe to STOP (2) rather than raising a KeyError at
        # report time — a no-go is always the conservative default.
        return {"noop": 0, "relink": 0, "renumber": 0, "merge": 3, "stop": 2}.get(self.action, 2)


def _by_id(revs: list[Rev]) -> dict[str, Rev]:
    return {r.id: r for r in revs}


def classify_shared(
    onto_revs: list[Rev],
    branch_revs: list[Rev],
    ancestor_ids: frozenset[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Split ids present at BOTH refs whose content differs into
    ``(collisions, rewrites)``.

    Same id + identical bytes is one migration seen twice — shared history, and the
    overwhelmingly common case. When the bytes differ there are exactly two
    possibilities and they want opposite treatment:

    * **collision** — two branches independently minted the same number. The branch's
      migration is real, new work wearing someone else's id. Renumber it.
    * **rewrite** — the branch edited a migration that was already merged. Renumbering
      would fork one migration into two. Stop.

    ``ancestor_ids`` (the revisions present at the merge base) is what separates them:
    an id already in shared history and now differing has been rewritten; an id in
    neither ref's past was minted twice. Pass ``None`` when the merge base is unknown
    and the split falls back to file paths — distinct paths cannot be one migration,
    while a shared path is assumed to be a rewrite, which is the conservative half.
    """
    onto = _by_id(onto_revs)
    collisions: list[str] = []
    rewrites: list[str] = []
    for r in branch_revs:
        other = onto.get(r.id)
        if other is None or other.digest == r.digest:
            continue
        if ancestor_ids is not None:
            (rewrites if r.id in ancestor_ids else collisions).append(r.id)
        else:
            (rewrites if other.path == r.path else collisions).append(r.id)
    return sorted(collisions), sorted(rewrites)


def _chain_order(revs: list[Rev], new_ids: set[str], base_id: str) -> list[Rev]:
    """Order a single-chain set of new revs from base to head."""
    children: dict[str, Rev] = {}
    for r in revs:
        for d in r.down:
            if d in new_ids:
                children[d] = r
    by_id = _by_id(revs)
    ordered, cur = [], by_id[base_id]
    seen: set[str] = set()
    while cur is not None and cur.id not in seen:
        ordered.append(cur)
        seen.add(cur.id)
        cur = children.get(cur.id)
    return ordered


def _allocate(taken: set[int], after: int, count: int) -> list[int]:
    """The next `count` free chain positions strictly above `after`.

    Free rather than merely sequential: a repo whose numbering has a gap, or whose
    integration head is not its highest number, must not have a "next" number handed
    to it that some other migration already occupies.
    """
    out, n = [], after
    while len(out) < count:
        n += 1
        if n not in taken:
            out.append(n)
    return out


def _renamed(rev: Rev, new_num: int, new_down: tuple[str, ...], width: int) -> Rename:
    new_id = str(new_num).zfill(width)
    new_path = None
    if rev.path:
        p = Path(rev.path)
        m = _FILENAME_RE.match(p.name)
        # A file whose name does not carry the number keeps its name: the id is what
        # Alembic reads, and inventing a filename convention here would be worse than
        # leaving one file spelled unusually.
        new_path = str(p.with_name(f"{new_id}_{m.group('slug')}.py")) if m else rev.path
    return Rename(rev.id, new_id, rev.path, new_path, rev.down, new_down)


def reconcile(
    onto_revs: list[Rev],
    branch_revs: list[Rev],
    ancestor_ids: frozenset[str] | None = None,
) -> Plan:
    """Decide how to land `branch_revs` onto `onto_revs` with a single head.

    onto_revs   — migrations present at the integration ref (e.g. origin/main)
    branch_revs — migrations present at the feature ref (e.g. HEAD)
    ancestor_ids — revision ids at the merge base; see `classify_shared`
    """
    onto_ids = {r.id for r in onto_revs}
    onto_heads = heads(onto_revs)
    collisions, rewrites = classify_shared(onto_revs, branch_revs, ancestor_ids)

    # Guard A — the integration ref must itself have a single head. Checked first
    # because every later decision is stated relative to that one head.
    if len(onto_heads) != 1:
        return Plan(
            "stop",
            f"integration ref has {len(onto_heads)} heads {onto_heads}; "
            "reconcile it on its own branch first",
            go=False,
            collisions=collisions,
            guards={"A_onto_single_head": False},
        )
    onto_head = onto_heads[0]

    # Guard C — the branch must not *rewrite* a migration that is already shared. A
    # reparented shared migration is excluded from `branch_new` (its id is in
    # onto_ids), so the merged-graph simulation would keep the integration ref's
    # original parentage and falsely report a single head while the landed tree has
    # two. Rewriting shared history is dangerous regardless of head count, so STOP.
    if rewrites:
        return Plan(
            "stop",
            f"branch rewrites already-shared migration(s) {rewrites}; reconcile the "
            "history conflict manually (a reparented shared migration is not a clean "
            "head merge)",
            go=False,
            onto_head=onto_head,
            collisions=collisions,
            guards={"A_onto_single_head": True, "C_no_shared_rewrite": False},
        )

    # A collided id is present at the integration ref, so the usual "not already
    # there" test would exclude the branch's own new migration and call the merge
    # clean. Fold the collisions back in — they are the work, not the duplicate.
    collided = set(collisions)
    branch_new = [r for r in branch_revs if r.id not in onto_ids or r.id in collided]
    if not branch_new:
        return Plan(
            "noop",
            "branch added no migrations; merge is graph-clean",
            go=True,
            onto_head=onto_head,
            guards={"A_onto_single_head": True, "C_no_shared_rewrite": True},
        )

    new_ids = {r.id for r in branch_new}
    # base(s): branch migrations linking into pre-existing history (their down set has
    # no member inside branch_new). A collided rev's parent is an integration-ref
    # revision, so it bases like any other.
    bases = [r for r in branch_new if not (set(r.down) & new_ids)]

    # A brand-new root among the new migrations (`down_revision = None`) is a *second
    # base* in the graph — a fresh, independent lineage that does not attach to the
    # integration history at all. That is ambiguous (almost always an authoring
    # mistake), not a clean merge: flag it for a human rather than mislabelling it
    # "base is a merge node" and reporting a confident GO.
    new_roots = sorted(r.id for r in bases if not r.down)
    if new_roots:
        return Plan(
            "stop",
            f"branch introduces new root migration(s) {new_roots} (down_revision = "
            "None) — a second, independent base is ambiguous; reattach them to the "
            "migration chain first",
            go=False,
            onto_head=onto_head,
            collisions=collisions,
            guards={"A_onto_single_head": True, "C_no_shared_rewrite": True},
        )

    referenced_in_new: set[str] = set()
    for r in branch_new:
        referenced_in_new.update(d for d in r.down if d in new_ids)
    branch_heads = [r.id for r in branch_new if r.id not in referenced_in_new]

    # Guard C is structural below this point: `rewrites` already STOPped on any
    # shared-history rewrite, so a relink or renumber here never rewrites shared
    # history. Report it satisfied.
    guards = {
        "A_onto_single_head": True,
        "B_single_chain": len(bases) == 1 and len(branch_heads) == 1,
        "C_no_shared_rewrite": True,
    }
    single_base = bases[0] if len(bases) == 1 else None
    # A base that is itself a merge node (multiple external parents) cannot be cleanly
    # relinked onto one head — force the merge fallback.
    base_relinkable = single_base is not None and len(single_base.down) == 1

    if not (guards["B_single_chain"] and base_relinkable):
        why = []
        if len(bases) != 1:
            why.append(f"{len(bases)} bases")
        if len(branch_heads) != 1:
            why.append(f"{len(branch_heads)} branch heads")
        if single_base is not None and not base_relinkable:
            why.append("base is a merge node")
        return Plan(
            "merge",
            "relink unsafe (" + ", ".join(why) + "); use `alembic merge heads`",
            go=True,
            onto_head=onto_head,
            branch_head=branch_heads[0] if len(branch_heads) == 1 else None,
            collisions=collisions,
            guards=guards,
        )

    chain = _chain_order(branch_new, new_ids, single_base.id)
    onto_by_id = _by_id(onto_revs)
    head_number = onto_by_id[onto_head].number

    # Renumber when an id is contested, and also when the branch's numbers merely sit
    # at or below the integration head — a number that no longer states its own
    # position is the collision one merge away, and the fix is identical.
    stale_numbers = [
        r.id
        for r in chain
        if r.number is not None and head_number is not None and r.number <= head_number
    ]
    if collisions or stale_numbers:
        unnumbered = [r.id for r in chain if r.number is None]
        if unnumbered or head_number is None:
            return Plan(
                "merge",
                f"revision id(s) {unnumbered or [onto_head]} are not chain numbers, so "
                "renumbering cannot be derived; use `alembic merge heads`",
                go=True,
                onto_head=onto_head,
                branch_head=branch_heads[0],
                collisions=collisions,
                guards=guards,
            )
        width = max(len(onto_head), max(len(r.id) for r in chain))
        taken = {r.number for r in onto_revs if r.number is not None}
        numbers = _allocate(taken, head_number, len(chain))
        renames, prev = [], onto_head
        for rev, num in zip(chain, numbers, strict=True):
            rename = _renamed(rev, num, (prev,), width)
            renames.append(rename)
            prev = rename.new_id
        why = (
            f"revision id(s) {collisions} already exist at the integration ref with "
            "different content — two branches minted the same number"
            if collisions
            else f"revision id(s) {stale_numbers} are at or below the integration head "
            f"{onto_head}, so the chain no longer states its own order"
        )
        return Plan(
            "renumber",
            f"{why}; renumber {len(renames)} migration(s) onto {onto_head}",
            go=True,
            onto_head=onto_head,
            base=single_base.id,
            base_path=single_base.path,
            branch_head=branch_heads[0],
            old_down=single_base.down,
            new_down=(onto_head,),
            renames=renames,
            collisions=collisions,
            guards=guards,
        )

    if single_base.down == (onto_head,):
        # branch cut from the current integration head; the integration ref added
        # nothing new.
        return Plan(
            "noop",
            "branch base already links to the current integration head",
            go=True,
            onto_head=onto_head,
            base=single_base.id,
            base_path=single_base.path,
            branch_head=branch_heads[0],
            guards=guards,
        )
    return Plan(
        "relink",
        f"relink base {single_base.id}: {single_base.down[0]} -> {onto_head}",
        go=True,
        onto_head=onto_head,
        base=single_base.id,
        base_path=single_base.path,
        branch_head=branch_heads[0],
        old_down=single_base.down,
        new_down=(onto_head,),
        guards=guards,
    )


def apply_plan(revs: list[Rev], plan: Plan) -> list[Rev]:
    """Return a new rev list with the plan applied in-memory (for simulation and
    verification). Actions with nothing to rewrite return the list unchanged."""
    if plan.action == "relink":
        return [
            Rev(r.id, plan.new_down, r.depends, r.path, r.digest) if r.id == plan.base else r
            for r in revs
        ]
    if plan.action == "renumber":
        by_old = {rn.old_id: rn for rn in plan.renames}
        out = []
        for r in revs:
            rn = by_old.get(r.id)
            # Only the branch's copy is renamed. The integration ref's own migration
            # keeps the contested id, which is the whole point of resolving it this
            # way — so match on content, not on id alone.
            if rn is None or r.path != rn.old_path or r.down != rn.old_down:
                out.append(r)
            else:
                out.append(Rev(rn.new_id, rn.new_down, r.depends, rn.new_path, r.digest))
        return out
    return list(revs)


def verify_single_head(revs: list[Rev]) -> tuple[bool, list[str]]:
    h = heads(revs)
    return len(h) == 1, h


def simulate_merged(onto_revs: list[Rev], branch_revs: list[Rev], plan: Plan) -> list[Rev]:
    """The graph that will exist on the integration ref after the plan lands: every
    integration rev, plus the branch's new revs with the plan applied."""
    onto_ids = {r.id for r in onto_revs}
    collided = set(plan.collisions)
    branch_new = [r for r in branch_revs if r.id not in onto_ids or r.id in collided]
    return list(onto_revs) + apply_plan(branch_new, plan)


# ---------------------------------------------------------------------------
# --- git layer --- thin I/O that feeds file text to the pure core.
# ---------------------------------------------------------------------------

#: Default Alembic version location. This tool assumes a *single* migrations
#: directory; a project with a custom `script_location` or extra `version_locations`
#: must pass the right path via `--versions-path`, otherwise migrations outside it are
#: invisible and heads would be under-counted.
_VERSIONS = "migrations/versions"


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=True
    ).stdout


def _cat_file_batch(repo: str, specs: list[str]) -> dict[str, str]:
    """Read many git blobs in a single `git cat-file --batch` pass.

    `specs` are `<ref>:<path>` object names. Returns {spec: text} for the specs that
    exist (missing ones are omitted). One subprocess for the whole set.
    """
    if not specs:
        return {}
    proc = subprocess.run(
        ["git", "-C", repo, "cat-file", "--batch"],
        input=("\n".join(specs) + "\n").encode(),
        capture_output=True,
        check=True,
    )
    out, i, result = proc.stdout, 0, {}
    # Output is ordered exactly as the input specs: for each, a header line
    # `<oid> <type> <size>\n<size bytes>\n`, or `<spec> missing\n`.
    for spec in specs:
        nl = out.index(b"\n", i)
        header = out[i:nl].decode()
        i = nl + 1
        if header.endswith(" missing"):
            continue
        size = int(header.split()[2])
        result[spec] = out[i : i + size].decode("utf-8", "replace")
        i += size + 1  # skip the blob and its trailing LF
    return result


def revs_at_ref(repo: str, ref: str, pathspec: str = _VERSIONS) -> list[Rev]:
    """Parse every migration file present at `ref` under `pathspec`.

    `--end-of-options` stops a ref beginning with `-` being parsed as a git flag.
    """
    listing = _git(repo, "ls-tree", "-r", "--name-only", "--end-of-options", ref, "--", pathspec)
    files = [f for f in listing.splitlines() if f.endswith(".py") and not f.endswith("__init__.py")]
    blobs = _cat_file_batch(repo, [f"{ref}:{f}" for f in files])
    revs = []
    for f in files:
        text = blobs.get(f"{ref}:{f}")
        if text is None:
            continue
        try:
            revs.append(parse_migration(text, path=f))
        except ValueError:
            continue
    return revs


def ancestor_ids_of(repo: str, onto: str, branch: str, pathspec: str = _VERSIONS):
    """Revision ids present at the merge base of the two refs, or None if the refs
    share no history (in which case `classify_shared` falls back to file paths)."""
    try:
        base = _git(repo, "merge-base", "--end-of-options", onto, branch).strip()
    except subprocess.CalledProcessError:
        return None
    return frozenset(r.id for r in revs_at_ref(repo, base, pathspec)) if base else None


def stale_references(repo: str, ref: str, plan: Plan, pathspec: str = _VERSIONS) -> list[str]:
    """Places outside the graph that still name a renumbered migration.

    A migration's own docstring quotes its number in prose ("revision **0017**"), the
    CHANGELOG cites it, and neither is a `revision =` assignment — so renumbering is
    mechanically correct and textually stale at the same time. These are reported and
    never rewritten: the tool edits assignments it can parse, and prose it cannot.
    """
    out = []
    for rn in plan.renames:
        for needle in filter(None, {rn.old_id, Path(rn.old_path).name if rn.old_path else None}):
            try:
                hits = _git(repo, "grep", "-n", "--fixed-strings", needle, ref, "--", ".")
            except subprocess.CalledProcessError:
                continue  # git grep exits 1 on no match
            for line in hits.splitlines():
                # `<ref>:<path>:<lineno>:<text>` — skip the assignments the renumber
                # itself rewrites, which are not stale by the time anyone reads this.
                _, _, rest = line.partition(":")
                path, _, tail = rest.partition(":")
                if path.startswith(pathspec) and re.match(r"^\d+:\s*(down_)?revision\b", tail):
                    continue
                out.append(f"{path}:{tail.split(':', 1)[0]} still names {needle}")
    return sorted(set(out))


def _rewrite_assignment(text: str, name: str, value: str) -> str:
    """Replace the *value* span of `name = ...`, preserving any type annotation and
    trailing comment. A multiline tuple value collapses to the single new reference."""
    scanned = _scan_rhs(text, name)
    if scanned is None:
        raise RuntimeError(f"no {name} assignment to rewrite")
    _rhs, start, end = scanned
    return f'{text[:start]}"{value}"{text[end:]}'


def _do_apply_relink_on_disk(repo: str, plan: Plan) -> list[str]:
    """Rewrite the base migration's down_revision in the working tree."""
    path = Path(repo) / plan.base_path
    path.write_text(_rewrite_assignment(path.read_text(), "down_revision", plan.new_down[0]))
    return [plan.base_path]


def _do_apply_renumber_on_disk(repo: str, plan: Plan) -> list[str]:
    """Renumber the branch's chain in the working tree: rewrite each migration's
    `revision`/`down_revision`, then `git mv` it onto its new filename.

    Applied head-first so that no intermediate state has two files claiming one path:
    every new number is strictly above the integration head and therefore above every
    old number in the chain, so a later rename can never target an earlier one's
    still-unmoved source.
    """
    touched = []
    for rn in reversed(plan.renames):
        old = Path(repo) / rn.old_path
        text = _rewrite_assignment(old.read_text(), "revision", rn.new_id)
        text = _rewrite_assignment(text, "down_revision", rn.new_down[0])
        old.write_text(text)
        if rn.new_path and rn.new_path != rn.old_path:
            _git(repo, "mv", "--", rn.old_path, rn.new_path)
        touched.append(rn.new_path or rn.old_path)
    return list(reversed(touched))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_plan(plan: Plan, merged_ok: bool | None, merged_heads: list[str] | None) -> None:
    print(f"action : {plan.action.upper()}")
    print(f"reason : {plan.reason}")
    print(f"guards : {plan.guards}")
    if plan.onto_head:
        print(f"integration head : {plan.onto_head}")
    if plan.collisions:
        print(f"duplicate ids    : {plan.collisions}")
    if plan.action == "relink":
        print(f"relink base      : {plan.base} ({plan.base_path})")
        print(f"  down_revision  : {plan.old_down} -> {plan.new_down}")
        print(f"branch head      : {plan.branch_head}")
    if plan.action == "renumber":
        for rn in plan.renames:
            print(f"renumber {rn.old_id} -> {rn.new_id}   down {rn.old_down} -> {rn.new_down}")
            if rn.new_path != rn.old_path:
                print(f"  git mv {rn.old_path} {rn.new_path}")
    if plan.action == "merge":
        print("  run: uv run alembic merge heads -m 'merge <branch> and main heads'")
    if merged_ok is not None:
        print(f"merged single head: {merged_ok} {merged_heads}")
    for w in plan.warnings:
        print(f"WARNING: {w}")
    print(f"go/no-go: {'GO' if plan.go else 'NO-GO'}  (exit {plan.exit_code})")


def _rev_parse(repo: str, ref: str) -> str:
    """Resolve `ref` to a commit sha (`--end-of-options` blocks a `-`-ref)."""
    return _git(repo, "rev-parse", "--end-of-options", ref).strip()


def _plan_for(args) -> tuple[Plan, list[Rev], list[Rev]]:
    onto_revs = revs_at_ref(args.repo, args.onto, args.versions_path)
    branch_revs = revs_at_ref(args.repo, args.branch, args.versions_path)
    ancestors = ancestor_ids_of(args.repo, args.onto, args.branch, args.versions_path)
    plan = reconcile(onto_revs, branch_revs, ancestors)
    if plan.renames:
        plan.warnings.extend(stale_references(args.repo, args.branch, plan, args.versions_path))
    return plan, onto_revs, branch_revs


def cmd_preflight(args) -> int:
    plan, onto_revs, branch_revs = _plan_for(args)
    merged_ok = merged_heads = None
    if plan.action in ("relink", "renumber", "noop"):
        ok, h = verify_single_head(simulate_merged(onto_revs, branch_revs, plan))
        merged_ok, merged_heads = ok, h
        if not ok:
            plan.action, plan.go = "stop", False
            plan.reason = f"post-resolution graph has heads {h}; do not land"
    if args.json:
        out = asdict(plan)
        out["merged_single_head"] = merged_ok
        out["merged_heads"] = merged_heads
        out["exit_code"] = plan.exit_code
        print(json.dumps(out, indent=2))
    else:
        _print_plan(plan, merged_ok, merged_heads)
    return plan.exit_code


def cmd_apply(args) -> int:
    plan, onto_revs, branch_revs = _plan_for(args)
    if plan.action in ("relink", "renumber"):
        # The post-resolution head check is not preflight decoration: `apply` must not
        # write a resolution that preflight would have refused to bless.
        ok, h = verify_single_head(simulate_merged(onto_revs, branch_revs, plan))
        if not ok:
            print(f"STOP: post-resolution graph has heads {h}; do not land", file=sys.stderr)
            return 2
        # `apply` edits the *working tree*, which reflects the checked-out HEAD. If
        # --branch names a different commit, the plan was computed from content that
        # is not on disk — editing would rewrite the wrong file. Refuse.
        head_sha = _rev_parse(args.repo, "HEAD")
        branch_sha = _rev_parse(args.repo, args.branch)
        if branch_sha != head_sha:
            print(
                f"REFUSE: apply edits the working tree (HEAD {head_sha[:12]}), but "
                f"--branch {args.branch} is {branch_sha[:12]}. Check out that branch "
                "first, then re-run apply.",
                file=sys.stderr,
            )
            return 2

    if plan.action == "relink":
        edited = _do_apply_relink_on_disk(args.repo, plan)
        print(f"relinked {edited[0]}: down_revision -> '{plan.new_down[0]}'")
    elif plan.action == "renumber":
        edited = _do_apply_renumber_on_disk(args.repo, plan)
        for rn in plan.renames:
            print(f"renumbered {rn.old_id} -> {rn.new_id}  ({rn.new_path})")
        for w in plan.warnings:
            print(f"WARNING: {w}")
        print("review the warnings above — prose naming the old number is NOT rewritten.")
    elif plan.action == "merge":
        print("merge migration required — this needs alembic, not a file edit:")
        print("  uv run alembic merge heads -m 'merge <branch> and main heads'")
        return plan.exit_code
    elif plan.action == "noop":
        print("nothing to do: " + plan.reason)
        return plan.exit_code
    else:  # stop
        print("STOP: " + plan.reason, file=sys.stderr)
        return plan.exit_code

    if plan.action in ("relink", "renumber"):
        print(f"review + commit: git add -- {' '.join(edited)} && git commit")
    return plan.exit_code


def cmd_heads(args) -> int:
    h = heads(revs_at_ref(args.repo, args.ref, args.versions_path))
    print("\n".join(h) if h else "(no migrations)")
    print(f"# {len(h)} head(s)", file=sys.stderr)
    # 0 heads == a migration-free ref (nothing to break `upgrade head`), so it is
    # fine — only a genuine *multi*-head graph fails the single-head guard.
    return 0 if len(h) <= 1 else 2


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--repo", default=".", help="repo dir (default: cwd)")
        sp.add_argument(
            "--versions-path",
            default=_VERSIONS,
            help=f"migrations version dir, repo-relative (default: {_VERSIONS}). Set "
            "this for a custom script_location; multiple version_locations are not "
            "supported.",
        )

    pf = sub.add_parser("preflight", help="analyse + report resolution (read-only)")
    common(pf)
    pf.add_argument("--onto", default="origin/main", help="integration ref")
    pf.add_argument("--branch", default="HEAD", help="feature ref")
    pf.add_argument("--json", action="store_true")
    pf.set_defaults(func=cmd_preflight)

    ap = sub.add_parser("apply", help="apply the resolution to the worktree (never commits)")
    common(ap)
    ap.add_argument("--onto", default="origin/main")
    ap.add_argument("--branch", default="HEAD")
    ap.set_defaults(func=cmd_apply)

    hd = sub.add_parser("heads", help="print heads at a ref")
    common(hd)
    hd.add_argument("--ref", default="HEAD")
    hd.set_defaults(func=cmd_heads)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
