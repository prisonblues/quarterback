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
covers: two files at one ref declaring the same revision id; the integration ref is
itself multi-head; the branch *rewrites* or *deletes* a migration that already exists
in shared history; a migration naming a parent that exists at neither ref; the branch
adds a second independent root (`down_revision = None`); a contested id whose chain
cannot be renumbered (`alembic merge heads` does not resolve a duplicate id, so it is
never offered as the answer to one); or a post-resolution graph that would still be
multi-head or still carry a duplicate. Anything this tool will not guess about exits 2
with a message, never as an uncaught traceback — a gate consuming the 0/2/3 scheme
reads Python's exit 1 as "unknown".

`preflight --json` reports the plan verbatim, and `guards` always carries all three
keys (`null` for "not reached") so a consumer can read one without a KeyError.

Assumes a single Alembic version directory (`--versions-path` for a custom
`script_location`). Multiple `version_locations` are not supported — heads would be
under-counted across the split dirs.

Usage (the file has a shebang and the executable bit; `python scripts/… ` works too):
    migration_reconcile.py preflight [--repo DIR] [--versions-path DIR] \
                                     [--onto REF] [--branch REF] [--json]
    migration_reconcile.py apply     [--repo DIR] [--versions-path DIR] \
                                     [--onto REF] [--branch REF] [--json]
    migration_reconcile.py heads     [--repo DIR] [--versions-path DIR] [--ref REF]
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


class ReconcileError(RuntimeError):
    """Anything this tool refuses to guess about, reported at the CLI as exit 2."""


class GitError(ReconcileError):
    """A git command failed. Carries the exit status and stderr, which
    `CalledProcessError`'s own string throws away — and the first failure anyone hits
    is `--onto origin/main` not existing in a shallow or freshly cloned checkout."""

    def __init__(self, argv: list[str], returncode: int, stderr: str) -> None:
        self.argv, self.returncode, self.stderr = argv, returncode, stderr
        super().__init__(f"git {' '.join(argv)} exited {returncode}: {stderr.strip()}")


class MigrationParseError(ReconcileError):
    """A file that IS a migration but whose metadata cannot be read literally."""


class DuplicateRevisionError(ReconcileError):
    """Two files at one ref declare the same revision id."""


class ApplyError(ReconcileError):
    """A failure during the on-disk rewrite."""


# ---------------------------------------------------------------------------
# Pure graph core — no git, no DB, no filesystem. Fully unit-testable.
# ---------------------------------------------------------------------------

#: The three module-level assignments Alembic reads out of a migration.
_META = ("revision", "down_revision", "depends_on")

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


def digest_of(data: bytes | str) -> str:
    """Content identity for a migration file: the full SHA-256 of its bytes.

    Untruncated, and over bytes rather than decoded text, because this is the exact
    equality that separates "collision, renumber it" from "shared history, ignore it".
    A digest taken over text decoded with ``errors="replace"`` makes two blobs that
    differ only in undecodable bytes compare equal.
    """
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def _module_assignments(text: str) -> dict[str, ast.Assign | ast.AnnAssign]:
    """Module-level ``name = ...`` / ``name: T = ...`` statements, last one winning.

    Read with ``ast`` rather than matched with a line-anchored regex. That is what
    keeps a ``revision = "0016"`` quoted inside a module docstring or a comment block
    — exactly the prose these migrations carry — from becoming the file's identity and
    then being rewritten by ``apply``. It also retires a hand-rolled string scanner
    that had to be right about backslash escapes, triple quotes and ``#`` inside a
    literal, and was not.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        raise MigrationParseError(f"not valid Python: {e}") from e
    out: dict[str, ast.Assign | ast.AnnAssign] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            targets: list[ast.expr] = [node.target] if node.value is not None else []
        elif isinstance(node, ast.Assign):
            targets = list(node.targets)
        else:
            continue
        for t in targets:
            if isinstance(t, ast.Name):
                out[t.id] = node
    return out


def _byte_span(text: str, node: ast.expr) -> tuple[int, int]:
    """``(start, end)`` offsets of an expression into ``text.encode("utf-8")``.

    Byte offsets, not character offsets: ``col_offset`` is counted in UTF-8 bytes, so
    slicing a ``str`` with it lands in the wrong place on any line holding a non-ASCII
    character — and a migration docstring with an em dash is not unusual here.
    """
    starts, off = [], 0
    for line in text.encode("utf-8").split(b"\n"):
        starts.append(off)
        off += len(line) + 1
    return (
        starts[node.lineno - 1] + node.col_offset,
        starts[node.end_lineno - 1] + node.end_col_offset,
    )


def _literal_refs(node: ast.Assign | ast.AnnAssign, name: str, path: str | None) -> tuple[str, ...]:
    """Read a revision-reference assignment as a literal: ``None`` -> ``()``,
    ``"x"`` -> ``("x",)``, ``("x", "y")`` / ``["x", "y"]`` -> ``("x", "y")``.

    A value that is not a literal — a module constant (``down_revision = PREVIOUS``),
    a call, an f-string — raises rather than being mined for quoted substrings. The
    ``()`` that a lenient parser returns for an unreadable value is indistinguishable
    from an explicit ``down_revision = None``, and the plan built on it reports a
    migration that IS attached as a second independent root, then reparents it.
    """
    where = f"{path}: " if path else ""
    try:
        value = ast.literal_eval(node.value)
    except (ValueError, SyntaxError, TypeError) as e:
        raise MigrationParseError(
            f"{where}`{name}` is not a literal, so its value cannot be read from the "
            "file text; write it as None, a string, or a tuple of strings"
        ) from e
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (tuple, list)) and all(isinstance(v, str) for v in value):
        return tuple(value)
    raise MigrationParseError(
        f"{where}`{name}` is {value!r}, which is not None, a string, or a tuple of strings"
    )


def _assignment_lines(text: str) -> set[int]:
    """1-based line numbers spanned by the metadata assignments this tool rewrites.

    Used to tell a stale prose reference from the assignment the renumber is about to
    fix. Keyed off the parsed span rather than a per-line regex: a value written
    across several lines puts the old id on a line carrying no keyword at all, which a
    per-line test reports as stale prose on an assignment that does get rewritten.
    """
    assigns = _module_assignments(text)
    out: set[int] = set()
    for name in _META:
        node = assigns.get(name)
        if node is not None:
            out.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return out


def parse_migration(text: str, path: str | None = None, raw: bytes | None = None) -> Rev:
    """Parse a migration file's text into a Rev.

    Two failure modes, deliberately different exceptions, because only one of them is
    safe to skip:

    * ``ValueError`` — the file carries no migration metadata at all. That is a helper
      module that happens to live in the versions directory, and skipping it is right.
    * ``MigrationParseError`` — the file plainly IS a migration and its metadata cannot
      be read literally. Skipping that one drops a real node out of the graph, and a
      graph missing a node under-counts its heads: a genuinely two-head merge then
      reports one head and gets a GO.
    """
    assigns = _module_assignments(text)
    where = path or "<text>"
    rev_node = assigns.get("revision")
    if rev_node is None:
        if any(name in assigns for name in _META[1:]):
            raise MigrationParseError(
                f"{where}: carries migration metadata but has no module-level "
                "`revision = '...'` — a computed or conditionally assigned id is not "
                "something this tool can reason about"
            )
        raise ValueError("no `revision = '...'` found — not a migration")
    ids = _literal_refs(rev_node, "revision", path)
    if len(ids) != 1:
        raise MigrationParseError(f"{where}: `revision` must be a single string literal")
    down_node = assigns.get("down_revision")
    if down_node is None:
        raise MigrationParseError(
            f"{where}: no `down_revision` assignment. An absent one is not the same "
            "claim as `down_revision = None`; write the root out explicitly."
        )
    depends_node = assigns.get("depends_on")
    return Rev(
        id=ids[0],
        down=_literal_refs(down_node, "down_revision", path),
        depends=(() if depends_node is None else _literal_refs(depends_node, "depends_on", path)),
        path=path,
        digest=digest_of(raw if raw is not None else text),
    )


def duplicate_ids(revs: list[Rev]) -> list[str]:
    """Ids declared by more than one revision in the set.

    Every id-keyed structure below this line — ``heads()``, ``_by_id``, the rename map
    — collapses a duplicate into a single node, so a set carrying one is not a graph
    this tool can reason about at all. It is also not a hypothetical: two branches both
    writing ``0018`` and both landing is precisely what this tool exists for, and once
    both are on one ref the collapsed graph looks single-headed and clean while
    ``alembic upgrade head`` refuses to load it.
    """
    seen: set[str] = set()
    dupes: set[str] = set()
    for r in revs:
        (dupes if r.id in seen else seen).add(r.id)
    return sorted(dupes)


def heads(revs: list[Rev]) -> list[str]:
    """Revision ids never referenced as a ``down_revision`` parent *by another rev in
    the set*. This matches Alembic exactly: head-ness is closed only by versioned
    ``down_revision`` edges. A ``depends_on`` edge orders application but does **not**
    close a head (Alembic's ``RevisionMap`` keeps a revision a head even when another
    revision ``depends_on`` it), so it is deliberately excluded here — folding it in
    would under-count heads and let a genuine two-head graph slip past the guard.

    References pointing outside the set (a base linking into pre-existing history) do
    not disqualify their target — it is not in the set anyway.

    Assumes ids are unique within the set, which is not free: `revs_at_ref` refuses to
    build a list carrying a duplicate and `reconcile` guards on `duplicate_ids` before
    anything else, because two nodes sharing an id collapse into one here and the
    answer comes back reassuring."""
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
    old_depends: tuple[str, ...] = ()
    new_depends: tuple[str, ...] = ()
    #: Which of two same-id revisions this rename is for. See `_is_renamed`.
    old_digest: str | None = None


#: Every guard key, always present in `Plan.guards`. A consumer reading
#: `guards["B_single_chain"]` must not get a KeyError depending on which guard fired.
_GUARD_KEYS = ("A_onto_single_head", "B_single_chain", "C_no_shared_rewrite")


def _guards(**evaluated: bool | None) -> dict[str, bool | None]:
    """All guard keys, `None` for the ones this outcome never reached."""
    out: dict[str, bool | None] = dict.fromkeys(_GUARD_KEYS)
    out.update(evaluated)
    return out


@dataclass
class Plan:
    action: str  # noop | relink | renumber | merge | stop
    reason: str
    go: bool  # is landing OK once the action is applied?
    onto_head: str | None = None
    #: The commit shas both refs were pinned to, so a consumer can tell what was read.
    onto_sha: str | None = None
    branch_sha: str | None = None
    base: str | None = None  # branch rev whose down_revision gets rewritten
    base_path: str | None = None
    branch_head: str | None = None
    old_down: tuple[str, ...] = ()
    new_down: tuple[str, ...] = ()
    renames: list[Rename] = field(default_factory=list)
    collisions: list[str] = field(default_factory=list)
    #: Ids declared twice *within one ref* — a graph Alembic will not load at all.
    duplicate_ids: list[str] = field(default_factory=list)
    guards: dict[str, bool | None] = field(default_factory=_guards)
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

    Identical bytes are not on their own enough to call an id shared. Two branches that
    both minted `0018` and happened to write the same file are still two files at two
    paths, and the merge keeps both — git conflicts on neither — so that is a collision
    like any other. Identical bytes at an identical path IS one file: git merges the
    two additions into one, and there is nothing to renumber.
    """
    onto = _by_id(onto_revs)
    collisions: list[str] = []
    rewrites: list[str] = []
    for r in branch_revs:
        other = onto.get(r.id)
        if other is None:
            continue
        same_content = other.digest == r.digest
        if ancestor_ids is None:
            # No merge base to consult: distinct paths cannot be one migration, while a
            # shared path is assumed to be a rewrite — the conservative half.
            if other.path != r.path:
                collisions.append(r.id)
            elif not same_content:
                rewrites.append(r.id)
        elif r.id in ancestor_ids:
            if not same_content:
                rewrites.append(r.id)
        elif not same_content or other.path != r.path:
            # Both refs added this id after the fork, and the merge will keep both.
            collisions.append(r.id)
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


def _renamed(
    rev: Rev,
    new_num: int,
    new_down: tuple[str, ...],
    width: int,
    new_depends: tuple[str, ...],
) -> Rename:
    new_id = str(new_num).zfill(width)
    new_path = None
    if rev.path:
        p = Path(rev.path)
        m = _FILENAME_RE.match(p.name)
        # A file whose name does not carry the number keeps its name: the id is what
        # Alembic reads, and inventing a filename convention here would be worse than
        # leaving one file spelled unusually.
        new_path = str(p.with_name(f"{new_id}_{m.group('slug')}.py")) if m else rev.path
    return Rename(
        rev.id,
        new_id,
        rev.path,
        new_path,
        rev.down,
        new_down,
        rev.depends,
        new_depends,
        rev.digest,
    )


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
    # Guard A0 — no id may be declared twice at a single ref. First, because every
    # structure below is keyed by id and would fold the two into one node: the graph
    # then looks single-headed and clean while `alembic upgrade head` refuses to load
    # it. This is the tool's own disease one level up — the collision it exists to
    # prevent, already landed — and no resolution below can address it.
    dupes = sorted(set(duplicate_ids(onto_revs)) | set(duplicate_ids(branch_revs)))
    if dupes:
        return Plan(
            "stop",
            f"revision id(s) {dupes} are declared by more than one migration at a "
            "single ref; Alembic cannot load that graph and no merge resolves it — "
            "renumber one of each pair first",
            go=False,
            duplicate_ids=dupes,
        )

    onto_ids = {r.id for r in onto_revs}
    branch_ids = {r.id for r in branch_revs}
    onto_heads = heads(onto_revs)
    collisions, rewrites = classify_shared(onto_revs, branch_revs, ancestor_ids)

    # Guard A — the integration ref must itself have a single head. Checked early
    # because every later decision is stated relative to that one head. A ref with no
    # migrations at all is NOT a failure: it has nothing to break `upgrade head`, and
    # `cmd_heads` already says so, so the two entry points must agree.
    if onto_revs and len(onto_heads) != 1:
        why = (
            f"has {len(onto_heads)} heads {onto_heads}"
            if onto_heads
            else "has migrations but no head at all, so its graph contains a cycle"
        )
        return Plan(
            "stop",
            f"integration ref {why}; reconcile it on its own branch first",
            go=False,
            collisions=collisions,
            guards=_guards(A_onto_single_head=False),
        )
    onto_head = onto_heads[0] if onto_heads else None

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
            guards=_guards(A_onto_single_head=True, C_no_shared_rewrite=False),
        )

    ok_so_far = _guards(A_onto_single_head=True, C_no_shared_rewrite=True)

    # A migration the branch DELETED that is still present at the integration ref and
    # was present at the merge base. Everything below is built from files that are
    # *present* — `branch_new` skips it and the simulation keeps every onto revision —
    # so a branch that only deletes reports a clean no-op, while the real git merge
    # deletes the file too and leaves whatever pointed at it dangling.
    if ancestor_ids is not None:
        deleted = sorted((ancestor_ids & onto_ids) - branch_ids)
        if deleted:
            return Plan(
                "stop",
                f"branch deletes migration(s) {deleted} that are in shared history; the "
                "merge would drop them and leave the chain dangling — restore them, or "
                "reconcile the deletion by hand",
                go=False,
                onto_head=onto_head,
                collisions=collisions,
                guards=ok_so_far,
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
            guards=ok_so_far,
        )

    new_ids = {r.id for r in branch_new}

    # An edge pointing at an id that exists at neither ref. Without this the target of
    # a dangling `down_revision` is simply absent from `new_ids`, which makes the
    # revision satisfy the base test — and the relink/renumber then replaces that
    # unknown parent with the integration head, silently discarding the dependency
    # instead of reporting that it cannot be resolved.
    known = branch_ids | onto_ids
    unknown = sorted(
        f"{r.id} -> {d}" for r in branch_new for d in (*r.down, *r.depends) if d not in known
    )
    if unknown:
        return Plan(
            "stop",
            f"branch migration(s) name a parent or dependency present at neither ref: "
            f"{unknown}; a missing target is not a base to reattach — restore the "
            "migration it names, or fix the reference",
            go=False,
            onto_head=onto_head,
            collisions=collisions,
            guards=ok_so_far,
        )

    # base(s): branch migrations linking into pre-existing history (their down set has
    # no member inside branch_new). A collided rev's parent is an integration-ref
    # revision, so it bases like any other.
    bases = [r for r in branch_new if not (set(r.down) & new_ids)]

    # A brand-new root among the new migrations (`down_revision = None`) is a *second
    # base* in the graph when the integration ref already has history — a fresh,
    # independent lineage that does not attach to it at all. That is ambiguous (almost
    # always an authoring mistake), not a clean merge. Against an integration ref with
    # NO migrations, one root is the legitimate first migration; several still are not.
    new_roots = sorted(r.id for r in bases if not r.down)
    if new_roots and (onto_head is not None or len(new_roots) > 1):
        why = (
            "a second, independent base is ambiguous"
            if onto_head is not None
            else "a migration graph has one base, and this branch declares several"
        )
        return Plan(
            "stop",
            f"branch introduces new root migration(s) {new_roots} (down_revision = "
            f"None) — {why}; reattach them to the migration chain first",
            go=False,
            onto_head=onto_head,
            collisions=collisions,
            guards=ok_so_far,
        )

    referenced_in_new: set[str] = set()
    for r in branch_new:
        referenced_in_new.update(d for d in r.down if d in new_ids)
    branch_heads = [r.id for r in branch_new if r.id not in referenced_in_new]

    single_base = bases[0] if len(bases) == 1 else None
    chain = _chain_order(branch_new, new_ids, single_base.id) if single_base else []

    # Guard B — the new revisions must be ONE linear chain, which is what `_chain_order`
    # and the renumber below both consume. One base and one head does NOT establish
    # that: a diamond closed by an internal merge node has both, and `_chain_order`
    # follows a single path through it, so the untraversed revisions keep their old
    # parents, never appear in `plan.renames`, and are left on disk while the tool
    # reports a confident GO. Covering the whole set is the property that matters, and
    # it is also what rules out a cycle hanging off the base.
    linear = (
        single_base is not None
        and len(branch_heads) == 1
        and len(chain) == len(branch_new)
        and all(len(r.down) <= 1 for r in branch_new)
    )
    guards = _guards(A_onto_single_head=True, B_single_chain=linear, C_no_shared_rewrite=True)

    if not linear:
        why = []
        if len(bases) != 1:
            why.append(f"{len(bases)} bases")
        if len(branch_heads) != 1:
            why.append(f"{len(branch_heads)} branch heads")
        if single_base is not None and len(single_base.down) > 1:
            why.append("base is a merge node")
        if single_base is not None and len(chain) != len(branch_new):
            why.append(f"{len(branch_new) - len(chain)} revision(s) off the base's chain")
        if single_base is not None and any(
            r is not single_base and len(r.down) > 1 for r in branch_new
        ):
            why.append("an internal merge node")
        if collisions:
            # `alembic merge heads` adds a merge revision. It does not renumber
            # anything, so it cannot make two migrations claiming one id into two ids —
            # the duplicate survives onto the integration ref. Offering it here would
            # be a GO on a graph that is still broken.
            return Plan(
                "stop",
                f"revision id(s) {collisions} are claimed by both refs with different "
                f"content, and the branch's migrations are not one linear chain "
                f"({', '.join(why)}) so they cannot be renumbered; `alembic merge "
                "heads` does not resolve a duplicate revision id — reconcile by hand",
                go=False,
                onto_head=onto_head,
                collisions=collisions,
                guards=guards,
            )
        return Plan(
            "merge",
            "relink unsafe (" + ", ".join(why) + "); use `alembic merge heads`",
            go=True,
            onto_head=onto_head,
            branch_head=branch_heads[0] if len(branch_heads) == 1 else None,
            collisions=collisions,
            guards=guards,
        )

    # Linear from here: one base, one head, every new revision on the chain, and no
    # revision with more than one parent.
    assert single_base is not None  # `linear` proved it; this narrows the type

    if onto_head is None:
        return Plan(
            "noop",
            "integration ref has no migrations; the branch's chain lands as written",
            go=True,
            base=single_base.id,
            base_path=single_base.path,
            branch_head=branch_heads[0],
            guards=guards,
        )

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
            blocker = unnumbered or [onto_head]
            if collisions:
                return Plan(
                    "stop",
                    f"revision id(s) {collisions} are claimed by both refs with "
                    f"different content, and {blocker} are not chain numbers so a "
                    "renumbering cannot be derived; `alembic merge heads` does not "
                    "resolve a duplicate revision id — renumber by hand",
                    go=False,
                    onto_head=onto_head,
                    collisions=collisions,
                    guards=guards,
                )
            return Plan(
                "merge",
                f"revision id(s) {blocker} are not chain numbers, so renumbering "
                "cannot be derived; use `alembic merge heads`",
                go=True,
                onto_head=onto_head,
                branch_head=branch_heads[0],
                guards=guards,
            )
        width = max(len(onto_head), max(len(r.id) for r in chain))
        taken = {r.number for r in onto_revs if r.number is not None}
        numbers = _allocate(taken, head_number, len(chain))
        id_map = {r.id: str(n).zfill(width) for r, n in zip(chain, numbers, strict=True)}
        renames, prev = [], onto_head
        for r, num in zip(chain, numbers, strict=True):
            # A `depends_on` naming another link of this same chain moves with it.
            # References from outside the chain are refused below rather than guessed.
            rename = _renamed(r, num, (prev,), width, tuple(id_map.get(d, d) for d in r.depends))
            renames.append(rename)
            prev = rename.new_id

        # An id being renumbered, referenced by a migration that is NOT being
        # renumbered, cannot be rewritten from here: outside the chain the same id also
        # names the integration ref's own copy, and picking one is how a renumber
        # silently reparents somebody else's migration. (A reference to the *collided*
        # id from outside is fine — that one keeps its id and still resolves.)
        renamed_ids = {rn.old_id for rn in renames}
        outsiders = sorted(
            f"{r.id} -> {d}"
            for r in branch_revs
            if r.id not in new_ids
            for d in (*r.down, *r.depends)
            if d in renamed_ids and d not in onto_ids
        )
        if outsiders:
            return Plan(
                "stop",
                f"migration(s) outside the renumbered chain reference ids it moves: "
                f"{outsiders}; rewriting those references is not something this tool "
                "can do unambiguously — reconcile by hand",
                go=False,
                onto_head=onto_head,
                collisions=collisions,
                guards=guards,
            )

        why_renumber = (
            f"revision id(s) {collisions} already exist at the integration ref with "
            "different content — two branches minted the same number"
            if collisions
            else f"revision id(s) {stale_numbers} are at or below the integration head "
            f"{onto_head}, so the chain no longer states its own order"
        )
        return Plan(
            "renumber",
            f"{why_renumber}; renumber {len(renames)} migration(s) onto {onto_head}",
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
        by_old: dict[str, list[Rename]] = {}
        for rn in plan.renames:
            by_old.setdefault(rn.old_id, []).append(rn)
        out = []
        for r in revs:
            rn = next((c for c in by_old.get(r.id, ()) if _is_renamed(r, c)), None)
            if rn is None:
                out.append(r)
            else:
                out.append(Rev(rn.new_id, rn.new_down, rn.new_depends, rn.new_path, r.digest))
        return out
    return list(revs)


def _is_renamed(r: Rev, rn: Rename) -> bool:
    """Is `r` the revision this rename is for?

    Only the branch's copy is renamed — the integration ref's own migration keeps the
    contested id, which is the whole point of resolving it this way — so the match
    cannot be on id alone.

    Identity is the file's path AND its bytes. Path alone is not enough: two branches
    can mint one id and write byte-identical files at different paths. Bytes alone are
    not enough either, for the same reason from the other side. `Rev.digest` is what
    the bytes half reads; both halves fall through when a Rev was built from a fixture
    with no file behind it, and parentage decides.
    """
    if r.path is not None and rn.old_path is not None and r.path != rn.old_path:
        return False
    if r.digest is not None and rn.old_digest is not None:
        return r.digest == rn.old_digest
    return r.down == rn.old_down


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
    """Run git, or raise `GitError` carrying stderr.

    `subprocess.run(check=True)` raises a `CalledProcessError` whose default string is
    only "Command [...] returned non-zero exit status N" — stderr is swallowed. The
    likeliest first-run failure is `--onto origin/main` not existing (a shallow CI
    clone, a differently named remote, never fetched), and a traceback out of
    `revs_at_ref` is a poor way to be told so.
    """
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitError(list(args), proc.returncode, proc.stderr)
    return proc.stdout


def _cat_file_batch(repo: str, specs: list[str]) -> dict[str, bytes]:
    """Read many git blobs in a single `git cat-file --batch` pass.

    `specs` are `<ref>:<path>` object names; returns {spec: raw bytes}. Raw, not
    decoded, because `digest_of` compares blobs for exact equality and a lossy decode
    makes two different files compare equal.

    A spec git reports `missing` raises rather than being dropped: every spec here
    came from a listing of that same ref, so a missing one means the read is wrong,
    and silently omitting it removes a node from the graph and under-counts heads.
    """
    if not specs:
        return {}
    if any("\n" in s for s in specs):
        raise ReconcileError(
            "a path containing a newline cannot be addressed through `git cat-file "
            f"--batch`: {[s for s in specs if chr(10) in s]}"
        )
    proc = subprocess.run(
        ["git", "-C", repo, "cat-file", "--batch"],
        input=("\n".join(specs) + "\n").encode(),
        capture_output=True,
    )
    if proc.returncode != 0:
        raise GitError(
            ["cat-file", "--batch"], proc.returncode, proc.stderr.decode("utf-8", "replace")
        )
    out, i, result = proc.stdout, 0, {}
    # Output is ordered exactly as the input specs: for each, a header line
    # `<oid> <type> <size>\n<size bytes>\n`, or `<spec> missing\n`.
    for spec in specs:
        nl = out.index(b"\n", i)
        header = out[i:nl].decode()
        i = nl + 1
        if header.endswith(" missing"):
            raise ReconcileError(f"git reports {spec} missing, but it was listed at that ref")
        size = int(header.split()[2])
        result[spec] = out[i : i + size]
        i += size + 1  # skip the blob and its trailing LF
    return result


def revs_at_ref(
    repo: str,
    ref: str,
    pathspec: str = _VERSIONS,
    skipped: list[str] | None = None,
) -> list[Rev]:
    """Parse every migration file present at `ref` under `pathspec`.

    Revision ids are unique in the returned list, structurally: two files declaring one
    id raise `DuplicateRevisionError` naming both paths. That is the property `heads()`
    and every id-keyed structure downstream silently assume, and the case is real —
    once two branches' `0018`s have both landed, the collapsed graph reads clean.

    A file with no migration metadata at all is a helper module and is skipped (its
    path appended to `skipped`, if given). A file that IS a migration but cannot be
    read literally raises: dropping it would take a node out of the graph, and a graph
    missing a node under-counts its heads.

    `--end-of-options` stops a ref beginning with `-` being parsed as a git flag, and
    `-z` stops `core.quotePath` C-quoting a non-ASCII filename into a spec that then
    resolves to nothing.
    """
    listing = _git(
        repo, "ls-tree", "-r", "--name-only", "-z", "--end-of-options", ref, "--", pathspec
    )
    files = [f for f in listing.split("\0") if f.endswith(".py") and Path(f).name != "__init__.py"]
    blobs = _cat_file_batch(repo, [f"{ref}:{f}" for f in files])
    revs = []
    for f in files:
        raw = blobs[f"{ref}:{f}"]
        try:
            revs.append(parse_migration(raw.decode("utf-8", "replace"), path=f, raw=raw))
        except ValueError:
            if skipped is not None:
                skipped.append(f)
    dupes = duplicate_ids(revs)
    if dupes:
        detail = "; ".join(
            f"{i}: {', '.join(sorted(r.path or '?' for r in revs if r.id == i))}" for i in dupes
        )
        raise DuplicateRevisionError(
            f"at {ref}, revision id(s) {dupes} are declared by more than one file "
            f"({detail}); Alembic cannot load that graph — renumber one of each pair"
        )
    return revs


def ancestor_ids_of(
    repo: str, onto: str, branch: str, pathspec: str = _VERSIONS
) -> frozenset[str] | None:
    """Revision ids present at the merge base of the two refs, or None if the refs
    share no history (in which case `classify_shared` falls back to file paths)."""
    try:
        base = _git(repo, "merge-base", "--end-of-options", onto, branch).strip()
    except GitError as e:
        if e.returncode == 1:  # no common ancestor; anything else is a real failure
            return None
        raise
    return frozenset(r.id for r in revs_at_ref(repo, base, pathspec)) if base else None


def _rewritten_assignment_lines(
    repo: str, ref: str, plan: Plan, pathspec: str
) -> set[tuple[str, str]]:
    """`(path, lineno)` pairs the renumber itself rewrites, so the stale-reference scan
    can skip them: they are not stale by the time anybody reads the warning.

    Keyed off each file's parsed assignment spans rather than a per-line regex. A value
    written across several lines (`down_revision = (\n    "0018",\n)`) puts the old id
    on a line carrying no keyword at all, which a per-line test reports as stale prose
    on an assignment that does get rewritten.
    """
    paths = [rn.old_path for rn in plan.renames if rn.old_path and rn.old_path.startswith(pathspec)]
    blobs = _cat_file_batch(repo, [f"{ref}:{p}" for p in paths])
    out: set[tuple[str, str]] = set()
    for path in paths:
        text = blobs[f"{ref}:{path}"].decode("utf-8", "replace")
        out.update((path, str(ln)) for ln in _assignment_lines(text))
    return out


def stale_references(repo: str, ref: str, plan: Plan, pathspec: str = _VERSIONS) -> list[str]:
    """Places outside the graph that still name a renumbered migration.

    A migration's own docstring quotes its number in prose ("revision **0017**"), the
    CHANGELOG cites it, and neither is a `revision =` assignment — so renumbering is
    mechanically correct and textually stale at the same time. These are reported and
    never rewritten: the tool edits assignments it can parse, and prose it cannot.

    One `git grep` for the whole rename set rather than two per rename, `-w` so `0018`
    does not match `20240018`, `-I` so a binary file cannot produce a "Binary file …
    matches" line dressed up as a warning, and restricted to `*.py`/`*.md` because
    those are where prose about a migration lives — a bare four-digit needle otherwise
    hits lockfile hashes, ports and fixtures, and buries the real warnings.
    """
    needles = sorted(
        {
            n
            for rn in plan.renames
            for n in (rn.old_id, Path(rn.old_path).name if rn.old_path else None)
            if n
        }
    )
    if not needles:
        return []
    args = ["grep", "-n", "-I", "-w", "--fixed-strings"]
    for n in needles:
        args += ["-e", n]
    args += ["--end-of-options", ref, "--", "*.py", "*.md"]
    try:
        hits = _git(repo, *args)
    except GitError as e:
        if e.returncode == 1:  # git grep exits 1 on no match
            return []
        # Anything else — a bad ref, an unreadable index — is not "no stale prose".
        # These warnings are the only thing telling the caller prose went stale, so a
        # scan that did not run must say so rather than report nothing.
        return [f"stale-reference scan failed (git grep exit {e.returncode}): {e.stderr.strip()}"]
    skip = _rewritten_assignment_lines(repo, ref, plan, pathspec)
    out = []
    for line in hits.splitlines():
        # `<ref>:<path>:<lineno>:<text>`
        _, _, rest = line.partition(":")
        path, _, tail = rest.partition(":")
        lineno, _, body = tail.partition(":")
        if (path, lineno) in skip:
            continue
        out.extend(f"{path}:{lineno} still names {n}" for n in needles if n in body)
    return sorted(set(out))


def _render_refs(refs: tuple[str, ...]) -> str:
    """A revision-reference tuple as the Python literal a migration file spells it
    with: `()` -> `None`, one -> `"x"`, several -> `("x", "y")`."""
    if not refs:
        return "None"
    if len(refs) == 1:
        return f'"{refs[0]}"'
    return "(" + ", ".join(f'"{r}"' for r in refs) + ")"


def _rewrite_assignment(text: str, name: str, literal: str) -> str:
    """Replace the *value* of `name = ...` with `literal` — already-rendered Python
    source, see `_render_refs` — preserving any type annotation and trailing comment.
    A multiline tuple value collapses to whatever the literal spells.

    Spliced in UTF-8 bytes, because that is the unit `ast` counts column offsets in.
    """
    assigns = _module_assignments(text)
    node = assigns.get(name)
    if node is None or node.value is None:
        raise ReconcileError(f"no {name} assignment to rewrite")
    start, end = _byte_span(text, node.value)
    raw = text.encode("utf-8")
    return (raw[:start] + literal.encode("utf-8") + raw[end:]).decode("utf-8")


def _do_apply_relink_on_disk(repo: str, plan: Plan) -> list[str]:
    """Rewrite the base migration's down_revision in the working tree."""
    if not plan.base_path:
        raise ApplyError(
            "plan has no base_path, so there is no file to relink — it was computed "
            "from revisions with nothing on disk behind them"
        )
    path = Path(repo) / plan.base_path
    text = path.read_text(encoding="utf-8")
    path.write_text(
        _rewrite_assignment(text, "down_revision", _render_refs(plan.new_down)),
        encoding="utf-8",
    )
    return [plan.base_path]


def _do_apply_renumber_on_disk(repo: str, plan: Plan) -> list[str]:
    """Renumber the branch's chain in the working tree, returning the paths to stage.

    Every file is read and rewritten in memory, and every destination checked free,
    before anything at all is written — so the reachable failures (an unreadable
    source, an assignment that cannot be rewritten, a destination already occupied by
    an untracked file or an unrelated same-slug migration) all happen with the tree
    untouched. A failure past that point is reported with the files already written
    and a recovery hint, because from here there is no way to put them back.

    Moves run tail-first. The invariant that makes that safe is NOT that every new
    number is above every old one — a branch chain can hold numbers above the
    integration head, e.g. head `0018` with a chain `0018` -> `0030` renumbered to
    `0019`, `0020` — but that `_allocate` hands out strictly increasing positions
    disjoint from the numbers already taken, so walking the chain from its head down
    never targets a source that has not moved yet.

    The move is a plain filesystem rename rather than `git mv`, which would stage it:
    `apply` writes the working tree and leaves the index alone, so relink and renumber
    leave the same shape of change behind and the printed `git add` is all that is left
    to do rather than a repair step.
    """
    root = Path(repo)
    prepared: list[tuple[Rename, Path, Path, str]] = []
    for rn in plan.renames:
        if not rn.old_path:
            raise ApplyError(f"rename {rn.old_id} -> {rn.new_id} has no file behind it")
        src = root / rn.old_path
        try:
            # Explicit utf-8 on both ends: a POSIX-locale container defaults to ASCII,
            # and one em dash in a docstring would otherwise fail mid-apply.
            text = src.read_text(encoding="utf-8")
        except OSError as e:
            raise ApplyError(f"cannot read {rn.old_path}: {e}") from e
        text = _rewrite_assignment(text, "revision", _render_refs((rn.new_id,)))
        text = _rewrite_assignment(text, "down_revision", _render_refs(rn.new_down))
        if rn.new_depends != rn.old_depends:
            text = _rewrite_assignment(text, "depends_on", _render_refs(rn.new_depends))
        prepared.append((rn, src, root / (rn.new_path or rn.old_path), text))

    sources = {src for _, src, _, _ in prepared}
    occupied = sorted(
        str(dst.relative_to(root))
        for _, src, dst, _ in prepared
        if dst != src and dst.exists() and dst not in sources
    )
    if occupied:
        raise ApplyError(
            f"destination path(s) {occupied} already exist and are not part of this "
            "renumber; move or delete them, then re-run apply"
        )

    written: list[str] = []
    try:
        for _rn, src, dst, text in reversed(prepared):
            src.write_text(text, encoding="utf-8")
            written.append(str(src.relative_to(root)))
            if dst != src:
                src.rename(dst)
                written[-1] = str(dst.relative_to(root))
    except OSError as e:
        hint = shlex.quote(str(Path(plan.renames[0].old_path or ".").parent))
        raise ApplyError(
            f"renumber failed after writing {written}: {e}. The working tree is now "
            f"part-renumbered; recover with `git checkout -- {hint}` and re-run."
        ) from e
    staged = {p for rn in plan.renames for p in (rn.old_path, rn.new_path) if p}
    return sorted(staged)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _format_guards(guards: dict[str, bool | None]) -> str:
    """`A=ok B=ok C=n/a`, rather than a raw Python dict repr in output that is
    otherwise formatted prose."""
    marks = {True: "ok", False: "FAIL", None: "n/a"}
    return " ".join(f"{k.split('_')[0]}={marks[v]}" for k, v in guards.items())


def _print_plan(plan: Plan, merged_ok: bool | None, merged_heads: list[str] | None) -> None:
    print(f"action : {plan.action.upper()}")
    print(f"reason : {plan.reason}")
    print(f"guards : {_format_guards(plan.guards)}")
    if plan.onto_head:
        print(f"integration head : {plan.onto_head}")
    if plan.collisions:
        print(f"contested ids    : {plan.collisions}")
    if plan.duplicate_ids:
        print(f"duplicate ids    : {plan.duplicate_ids}  (declared twice at one ref)")
    if plan.action == "relink":
        print(f"relink base      : {plan.base} ({plan.base_path})")
        print(f"  down_revision  : {plan.old_down} -> {plan.new_down}")
        print(f"branch head      : {plan.branch_head}")
    if plan.action == "renumber":
        for rn in plan.renames:
            print(f"renumber {rn.old_id} -> {rn.new_id}   down {rn.old_down} -> {rn.new_down}")
            if rn.new_path != rn.old_path:
                print(f"  rename {rn.old_path} -> {rn.new_path}")
    if plan.action == "merge":
        print("  run: uv run alembic merge heads -m 'merge <branch> and main heads'")
    if merged_ok is not None:
        print(f"merged single head: {merged_ok} {merged_heads}")
    for w in plan.warnings:
        print(f"WARNING: {w}")
    print(f"go/no-go: {'GO' if plan.go else 'NO-GO'}  (exit {plan.exit_code})")


def _rev_parse(repo: str, ref: str) -> str:
    """Resolve `ref` to a sha (`--end-of-options` blocks a `-`-ref).

    `--verify` is load-bearing, not decoration: without it `git rev-parse` echoes the
    `--end-of-options` marker back as its own output line, so the return value is two
    lines and unusable as a ref. It also makes an unresolvable ref exit non-zero rather
    than echoing the string it could not resolve.
    """
    return _git(repo, "rev-parse", "--verify", "--end-of-options", ref).strip()


def _resolve(repo: str, ref: str, flag: str) -> str:
    """A ref, resolved to a sha, with a message a caller can act on when it is not
    there — the default `--onto origin/main` is missing in any checkout that has not
    fetched it, and that must not surface as a traceback out of `revs_at_ref`."""
    try:
        return _rev_parse(repo, ref)
    except GitError as e:
        raise ReconcileError(
            f"{flag} ref {ref!r} does not resolve in {repo}: "
            f"{e.stderr.strip() or 'unknown revision'}. Fetch it, or pass a ref that exists."
        ) from e


def _plan_for(args: argparse.Namespace) -> tuple[Plan, list[Rev], list[Rev]]:
    """Both refs are resolved to commit shas ONCE and everything reads those.

    A symbolic ref resolved separately for `ls-tree`, `merge-base` and the
    stale-reference scan is three answers if a fetch lands between them, and the apply
    that follows would write a resolution computed against an integration head that
    has already moved.
    """
    onto_sha = _resolve(args.repo, args.onto, "--onto")
    branch_sha = _resolve(args.repo, args.branch, "--branch")
    skipped: list[str] = []
    onto_revs = revs_at_ref(args.repo, onto_sha, args.versions_path, skipped)
    branch_revs = revs_at_ref(args.repo, branch_sha, args.versions_path, skipped)
    ancestors = ancestor_ids_of(args.repo, onto_sha, branch_sha, args.versions_path)
    plan = reconcile(onto_revs, branch_revs, ancestors)
    plan.onto_sha, plan.branch_sha = onto_sha, branch_sha
    plan.warnings.extend(
        f"{f} is under {args.versions_path} and carries no migration metadata, so it "
        "is not part of the graph"
        for f in sorted(set(skipped))
    )
    if plan.renames:
        plan.warnings.extend(stale_references(args.repo, branch_sha, plan, args.versions_path))
    return plan, onto_revs, branch_revs


def _post_resolution_problem(
    onto_revs: list[Rev], branch_revs: list[Rev], plan: Plan
) -> tuple[str | None, bool, list[str]]:
    """`(problem, single_head, heads)` for the graph the plan would leave behind.

    A duplicate id is checked as well as the head count: a rename that failed to match
    its revision leaves both copies claiming one id, which `heads()` folds into a
    single node and would report as clean.
    """
    merged = simulate_merged(onto_revs, branch_revs, plan)
    ok, h = verify_single_head(merged)
    dupes = duplicate_ids(merged)
    if dupes:
        return f"post-resolution graph still carries duplicate revision id(s) {dupes}", ok, h
    if not ok:
        return f"post-resolution graph has heads {h}", ok, h
    return None, ok, h


def _reject(plan: Plan, reason: str) -> Plan:
    """A STOP that keeps the facts and drops the resolution it just refused.

    Overwriting `action`/`go` in place left `renames`, `base` and `new_down` describing
    the renumber that was rejected, so the JSON read `{"action": "stop", "go": false,
    "renames": [...]}` — a stop with a list of edits to go and make.
    """
    return Plan(
        "stop",
        reason,
        go=False,
        onto_head=plan.onto_head,
        onto_sha=plan.onto_sha,
        branch_sha=plan.branch_sha,
        collisions=list(plan.collisions),
        duplicate_ids=list(plan.duplicate_ids),
        guards=dict(plan.guards),
        warnings=list(plan.warnings),
    )


def cmd_preflight(args: argparse.Namespace) -> int:
    plan, onto_revs, branch_revs = _plan_for(args)
    merged_ok = merged_heads = None
    rejected = None
    if plan.action in ("relink", "renumber", "noop"):
        problem, merged_ok, merged_heads = _post_resolution_problem(onto_revs, branch_revs, plan)
        if problem:
            rejected = asdict(plan)
            plan = _reject(plan, f"{problem}; do not land")
    if args.json:
        out = asdict(plan)
        out["merged_single_head"] = merged_ok
        out["merged_heads"] = merged_heads
        out["exit_code"] = plan.exit_code
        out["rejected_plan"] = rejected
        print(json.dumps(out, indent=2))
    else:
        _print_plan(plan, merged_ok, merged_heads)
    return plan.exit_code


def _refuse_apply(args: argparse.Namespace, plan: Plan) -> str | None:
    """Why `apply` must not write, or None. Every check is about the gap between the
    git *blobs* the plan was computed from and the *working tree* it rewrites."""
    # `apply` edits the working tree, which reflects the checked-out HEAD. If --branch
    # names a different commit, the plan was computed from content that is not on disk.
    head_sha = _rev_parse(args.repo, "HEAD")
    if plan.branch_sha != head_sha:
        return (
            f"apply edits the working tree (HEAD {head_sha[:12]}), but --branch "
            f"{args.branch} is {(plan.branch_sha or '?')[:12]}. Check out that branch "
            "first, then re-run apply."
        )
    # Equal shas do not mean equal content. An uncommitted edit, a new unstaged
    # migration, a half-staged rename or an untracked file sitting on a destination
    # path all mean the rewrite operates on text the plan never saw — in the worst
    # case the on-disk `revision` is not the id the plan reasoned about.
    dirty = _git(args.repo, "status", "--porcelain", "--", args.versions_path).strip()
    if dirty:
        return (
            f"the plan is computed from the commit at --branch, but {args.versions_path} "
            f"has uncommitted changes, so apply would rewrite text it never read:\n{dirty}\n"
            "Commit or stash them, then re-run apply."
        )
    return None


def cmd_apply(args: argparse.Namespace) -> int:
    plan, onto_revs, branch_revs = _plan_for(args)
    edited: list[str] = []
    if plan.action in ("relink", "renumber"):
        # The post-resolution check is not preflight decoration: `apply` must not write
        # a resolution that preflight would have refused to bless.
        problem, _ok, _h = _post_resolution_problem(onto_revs, branch_revs, plan)
        if problem is None:
            problem = _refuse_apply(args, plan)
        if problem:
            print(f"STOP: {problem}", file=sys.stderr)
            return 2

    if plan.action == "relink":
        edited = _do_apply_relink_on_disk(args.repo, plan)
        messages = [f"relinked {edited[0]}: down_revision -> {_render_refs(plan.new_down)}"]
    elif plan.action == "renumber":
        edited = _do_apply_renumber_on_disk(args.repo, plan)
        messages = [
            f"renumbered {rn.old_id} -> {rn.new_id}  ({rn.new_path})" for rn in plan.renames
        ]
        messages += [f"WARNING: {w}" for w in plan.warnings]
        messages.append("review the warnings above — prose naming the old number is NOT rewritten.")
    elif plan.action == "merge":
        messages = [
            "merge migration required — this needs alembic, not a file edit:",
            "  uv run alembic merge heads -m 'merge <branch> and main heads'",
        ]
    elif plan.action == "noop":
        messages = ["nothing to do: " + plan.reason]
    else:  # stop
        messages = ["STOP: " + plan.reason]

    if edited:
        messages.append(f"review + commit: git add -- {shlex.join(edited)} && git commit")

    if args.json:
        out = asdict(plan)
        out["exit_code"] = plan.exit_code
        out["edited"] = edited
        print(json.dumps(out, indent=2))
    else:
        stream = sys.stderr if plan.action == "stop" else sys.stdout
        for m in messages:
            print(m, file=stream)
    return plan.exit_code


def cmd_heads(args: argparse.Namespace) -> int:
    revs = revs_at_ref(args.repo, args.ref, args.versions_path)
    h = heads(revs)
    if h:
        print("\n".join(h))
    elif not revs:
        print("(no migrations)")
    else:
        print("(no head — the graph at this ref has a cycle)")
    print(f"# {len(h)} head(s)", file=sys.stderr)
    # A ref with no migrations at all has nothing to break `upgrade head`, so it is
    # fine. A ref with migrations and no head has a cycle, which is not. A duplicate id
    # never reaches here: `revs_at_ref` refuses to build a graph carrying one.
    return 0 if len(h) == 1 or not revs else 2


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
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
    ap.add_argument(
        "--json",
        action="store_true",
        help="machine-readable record of the plan and the paths rewritten",
    )
    ap.set_defaults(func=cmd_apply)

    hd = sub.add_parser("heads", help="print heads at a ref")
    common(hd)
    hd.add_argument("--ref", default="HEAD")
    hd.set_defaults(func=cmd_heads)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except ReconcileError as e:
        # Exit 2, not Python's uncaught-exception 1: a gate consuming the documented
        # 0/2/3 scheme reads 1 as "unknown" rather than as "stop".
        print(f"STOP: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
