"""The landing graph's reasoning: depth, cycles, and what the wire already said.

Pure and deterministic — no model, no I/O, like :mod:`app.sync` and
:mod:`app.overlap` — so the awkward parts (a cycle, a merge announced twice, a
node nothing gates) are testable without a database and cheap to run on every
read.

Two questions live here, and neither of them is a decision.

**How far is this node from being landable?** ``depths`` walks the live edges and
answers per node: ``0`` means nothing gates it, ``3`` means the longest chain
below it is three landings deep. That is the fact a JIT review policy needs and
does not have — *"#188 is one landing away, #40 is four"* — and it is the fact
that would have said #290 should go first. This module computes it; nothing here
acts on it.

**Has the thing this edge waits for already landed?** The board receives that
event redundantly already: a ``published`` post reading ``Merge pull request
#265 from prisonblues/fix/issue-261``, announced by CI and again by whichever
agent pulled it, while every waiting agent separately burns a 60-second timer
against the GitHub API for the same fact. ``announced_merges`` reads it off the
posts the board already holds.

## The one rule that keeps that safe

**A merge announcement counts only when the post names its repository as
``owner/name``.** The lifecycle hook tags posts with the checkout's *basename*
(``quarterback``), and a bare name is exactly the ambiguity
:data:`app.claimkey.REPO_RE` refuses — ``nix-fleet#40`` and ``quarterback#40``
are different nodes and there is no honest way to tell which one a bare
``Merge pull request #40`` meant. Under-resolving is a stale edge somebody
clears by hand; over-resolving silently tells an agent that its blocker has
landed when it has not, and it would do so most often across exactly the
repository boundary this primitive exists to span. So the qualified spelling is
required and the rest is ignored. CI's own ``published`` post carries a
``commit`` ref with ``repo: prisonblues/quarterback`` on it, which is why this
resolves at all.
"""

from __future__ import annotations

import re
from typing import Any

from app.claimkey import PR_SIGIL, REPO_RE
from app.sync import repo_key

#: What a merge commit's subject looks like when GitHub writes it, and what the
#: ``published`` posts on this board therefore say. Anchored at the start: a post
#: *discussing* a merge ("reverted the Merge pull request #265") is prose, and
#: prose is the input that must never resolve an edge — the same reading that
#: failed on #372, where a body opening "**This does not close #371**" was parsed
#: as a closing keyword.
MERGE_RE = re.compile(r"\AMerge pull request #(\d{1,12})\b")

#: The post type a merge arrives on. Named once so this module and the router
#: cannot come to disagree about which stream is being read.
LANDING_POST_TYPE = "published"

#: How deep a chain this walk will follow before it stops calling the answer a
#: number. Not a safety valve for cycles — those are detected exactly — but for a
#: graph so deep that a "distance to landable" is no longer a useful thing to
#: publish. Thirty is far past the 28-PR backlog #80 models.
MAX_DEPTH = 30


def post_repos(refs: list[dict[str, Any]] | None) -> set[str]:
    """Every repository a post names as ``owner/name``, folded.

    Deliberately narrow: a ref whose ``repo`` (or, for a ``repo`` ref, whose
    ``value``) is not the qualified spelling contributes nothing. See the module
    docstring for why a bare name is dropped rather than guessed at.
    """
    found: set[str] = set()
    for ref in refs or []:
        if not isinstance(ref, dict):
            continue
        for candidate in (ref.get("repo"),
                          ref.get("value") if ref.get("kind") == "repo" else None):
            if isinstance(candidate, str) and REPO_RE.match(candidate.strip()):
                found.add(candidate.strip().lower())
    return found


def merge_announced(post: dict[str, Any]) -> tuple[str, int] | None:
    """``(repo, pr number)`` this post announces as merged, or None.

    **One qualified repository, or nothing.** A post that names two — an agent's
    announcement carrying a commit ref in one repository and an issue ref in
    another — cannot say which of them `Merge pull request #40` belongs to, and
    synthesising the number against both would resolve an edge in a repository
    the merge never touched. That is the precise failure the qualified-spelling
    rule exists to prevent, arriving through a second door, so ambiguity is
    dropped here exactly as a bare name is.
    """
    if post.get("type") != LANDING_POST_TYPE:
        return None
    match = MERGE_RE.match(str(post.get("summary") or "").strip())
    if match is None:
        return None
    repos = post_repos(post.get("refs"))
    if len(repos) != 1:
        return None
    return next(iter(repos)), int(match.group(1))


def _earlier(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Whichever post is older, tolerating a missing timestamp."""
    ats, bts = a.get("ts"), b.get("ts")
    if ats is None:
        return b
    if bts is None:
        return a
    return a if ats <= bts else b


def announced_merges(posts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """``{pr claim key: the earliest post that announced it}``.

    Earliest wins because the same merge arrives twice — CI announces it and then
    an agent that pulled it announces it again (board 4910 and 4920 are one
    merge) — and the fact being recorded is *when it landed*, not when the second
    witness got round to saying so.

    It is compared rather than taken from arrival order, because the query that
    feeds this is newest-first: "the first one I saw" is the *latest* duplicate,
    which is the opposite of what this promises.
    """
    seen: dict[str, dict[str, Any]] = {}
    for post in posts:
        found = merge_announced(post)
        if found is None:
            continue
        repo, number = found
        key = f"{repo}{PR_SIGIL}{number}"
        seen[key] = post if key not in seen else _earlier(seen[key], post)
    return seen


def landings_since(posts: list[dict[str, Any]], repo: str, since: Any) -> int:
    """How many merges have landed on ``repo`` since ``since`` — the rot datum.

    This is what an unsequenced graph costs, counted. #290 was ``MERGEABLE`` when
    it opened and ``CONFLICTING`` by lunchtime because two unrelated PRs landed
    while it sat; nothing told it that was happening. The number of landings a
    node has been overtaken by is that fact, and — unlike GitHub's ``mergeable``
    word — the board can answer it from posts it already holds, with no GitHub
    client and no second store of a fact GitHub owns (#229).

    **It counts merges, not announcements of merges.** Board 4910 and 4920 are
    one landing said twice, by CI and by the agent that pulled it, and a reader
    told two PRs had gone past it when one had would draw exactly the wrong
    conclusion about how stale its branch is. So the pull request NUMBER is the
    unit, and each one is counted once at the earliest moment anybody said it —
    which also stops a merge that landed before this node entered the graph from
    being counted because its second witness spoke afterwards.

    Repository matching is by basename here, and only here, because this is a
    **count** rather than a resolution: over-counting says "more has landed than
    you think, go and look", which is the safe direction, while the same laxity
    applied to :func:`announced_merges` would clear an edge that is still real.
    """
    want = repo_key(repo)
    first: dict[int, Any] = {}
    for post in posts:
        if post.get("type") != LANDING_POST_TYPE:
            continue
        match = MERGE_RE.match(str(post.get("summary") or "").strip())
        if match is None:
            continue
        names = {repo_key(r) for r in post_repos(post.get("refs"))}
        for ref in post.get("refs") or []:
            if isinstance(ref, dict) and ref.get("kind") == "repo":
                names.add(repo_key(str(ref.get("value") or "")))
        if want not in names:
            continue
        number, ts = int(match.group(1)), post.get("ts")
        if number not in first or (ts is not None and first[number] is not None
                                   and ts < first[number]):
            first[number] = ts
    return sum(1 for ts in first.values()
               if since is None or ts is None or ts > since)


def adjacency(edges: list[tuple[str, str]]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """``(blockers, blocks)`` — the same edges read from both ends.

    Fan-in and fan-out are not two relations; they are one relation and two
    indexes on it. PR #293 closing #177 and #259 and unblocking #188 is three
    rows in ``blocks``; ``nix-fleet#40``'s four blockers are four rows in
    ``blockers``. Nodes with no edge in either direction do not appear.
    """
    blockers: dict[str, set[str]] = {}
    blocks: dict[str, set[str]] = {}
    for blocked, blocker in edges:
        blockers.setdefault(blocked, set()).add(blocker)
        blockers.setdefault(blocker, set())
        blocks.setdefault(blocker, set()).add(blocked)
        blocks.setdefault(blocked, set())
    return blockers, blocks


def cycles(blockers: dict[str, set[str]]) -> list[list[str]]:
    """Every set of nodes that gate each other, each sorted, the list sorted.

    **A cycle is recorded, not refused.** ``plan_depends`` refuses one, and it is
    right to: it owns both ends of every edge it stores. This does not — an edge
    here describes two pull requests that genuinely each need the other to land
    first, which is a real deadlock a human has to break, and a store that
    refuses to hold the fact leaves it exactly where #294 found it, in prose in a
    board post. So the walk names the condition instead, ``depth`` is ``None``
    for the nodes in it, and whoever reads the graph decides what to do.

    Tarjan's strongly connected components, iteratively — a landing graph is
    small, but recursion depth is not a thing to bet a board read on.
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    found: list[list[str]] = []

    for root in sorted(blockers):
        if root in index:
            continue
        work: list[tuple[str, list[str]]] = [(root, sorted(blockers.get(root, ())))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, pending = work[-1]
            if pending:
                nxt = pending.pop()
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, sorted(blockers.get(nxt, ()))))
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                # A single node is a component too; it is only a cycle if it
                # gates itself, which the table's CHECK constraint forbids —
                # kept here so this function is honest read on its own.
                if len(component) > 1 or node in blockers.get(node, ()):
                    found.append(sorted(component))
    return sorted(found)


def depths(blockers: dict[str, set[str]]) -> dict[str, int | None]:
    """Landings between each node and being landable — ``None`` inside a cycle.

    ``0`` is the answer that matters most: nothing live gates this node, so it
    can go now. Everything above it is the longest chain beneath it, because the
    shortest one is not what you wait for — a node with two blockers, one ready
    and one four deep, is four deep.

    Capped at :data:`MAX_DEPTH`, which reports ``None`` for the same reason a
    cycle does: past that the number has stopped being a distance anybody can
    act on, and a made-up one would read as a real one.
    """
    known: dict[str, int | None] = {node: None for cycle in cycles(blockers)
                                    for node in cycle}

    for root in sorted(blockers):
        if root in known:
            continue
        # Post-order, iteratively. The `(node, True)` marker is pushed before the
        # node's blockers, so it is popped after every one of them has an answer
        # — which is only sound because the nodes in a cycle were answered above
        # and are skipped here, leaving an acyclic remainder.
        stack: list[tuple[str, bool]] = [(root, False)]
        while stack:
            node, settled = stack.pop()
            if node in known:
                continue
            if not settled:
                stack.append((node, True))
                stack.extend((b, False) for b in sorted(blockers.get(node, ()))
                             if b not in known)
                continue
            below = [known.get(b) for b in blockers.get(node, ())]
            if any(d is None for d in below):
                # Behind a cycle, or behind something already past the cap. The
                # distance is not knowable, and a made-up one would read as real.
                known[node] = None
                continue
            depth = 0 if not below else max(below) + 1  # type: ignore[type-var]
            known[node] = None if depth > MAX_DEPTH else depth
    return known
