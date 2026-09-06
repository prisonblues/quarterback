"""A finding's identity by WHERE IT IS, for the questions that cross a round boundary.

`_defect_key` hashes the file plus the lexicographically-first reviewer-authored
title, so a finding's identity is a hash of one seat's own wording. Within a round
that is exactly right — two seats reporting the same defect in the same words are
the same defect, and the hash dedups them for nothing. Across a round boundary it
is wrong by construction: the seat re-words its own title next round, the hash
moves, and every question asked of the earlier round answers "no such finding".
#750 measured it at **five of five** — every `--assessed` answer on `lexray#1611`
matched nothing at all — and #748 is the same fault from the other side, a cycle
that can never carry an answer forward and so can never reach `confident: true`.

mergeCraft hit the same wall and wrote the cost down rather than papering over it
(*"paraphrases and minor rewordings produce new fingerprints; the tradeoff favors
stable dedup over semantic similarity"*), then stopped using the hash where the
answer has to survive a round: `evals/convergence.py` matches on **locality** —
the finding's line span against the round's diff hunks, within a few lines of
slack. That is what this module is.

Locality is robust to two things the hash is not. The MODEL moves: a reworded
restatement of last round's finding lands on the same lines and stops reading as
new. The CODE moves: a fix pass three lines above shifts a defect's line number
and it is still the same defect. Neither of those is a semantic judgement — this
module never reads a word a reviewer wrote, which is the whole point.

**Pure by rule.** Arguments in, value out: no I/O, no git, no clock, no module
state, no import of the panel's foundation. It is called from the middle of a
round, from a test, and from a replay over recorded rounds, and all three have to
get the same answer from the same bytes. It also means every function here can be
tested with a string literal, which is why the parser below reads a diff's text
rather than asking `git` anything.

The content hash STAYS. It is right for exact dedup inside one round and this
module does not replace it; the two identities answer different questions and are
meant to be held at once.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

#: Lines of drift a fix pass is allowed to introduce before a finding stops
#: being the same finding. Three, the figure mergeCraft settled on and the one
#: this repo's own increments justify: a fix that adds a guard clause or a log
#: line above a defect moves it by one or two, and a fix that moves it by thirty
#: has restructured the function the defect was in — at which point calling the
#: new finding a continuation of the old one is a guess, not a match.
#:
#: The slack is a tolerance on the PAIR and not on each end. One span is widened
#: by `slack` in both directions and the other is compared as written, so
#: `DEFAULT_LINE_SLACK = 3` means three lines of gap are tolerated, never six.
#: Widening both would double the reach silently, and on a dense file that is the
#: difference between matching the defect below and matching the next one along.
DEFAULT_LINE_SLACK: int = 3

#: The new-side hunk header. Only the `+` half is read: every line number a
#: finding carries is a line number in the file as it stands NOW, so the `-` half
#: names positions in a file nobody in this loop is looking at.
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

#: Where a finding's path may be spelled, in the order the spellings are
#: preferred. `file` is this repo's own wire field (``Canonical.as_dict``, the
#: board's finding row); `path` is what a reviewer writes when it answers in
#: mergeCraft's or an analyzer's vocabulary. Both are read because a matcher that
#: understood one of them would silently locate nothing from the other and report
#: that as "no match", which is the exact failure this module was written for.
_PATH_FIELDS = ("file", "path")


def normalize_path(value: object) -> str:
    """A path in this module's NORMAL FORM: forward slashes, no surrounding
    whitespace, no leading `./`, no `a/` or `b/` diff prefix.

    Public because the normal form is part of the contract. A caller holding a
    parsed scope and wanting the file-level question ("did this round's diff
    touch that file at all?") asks `normalize_path(p) in scope` — it must be able
    to spell the key the same way the parser did, and the alternative is every
    caller inventing its own strip and one of them getting it wrong in silence.

    Three spellings reach here for one file: a reviewer's `./harness/loops/panel.py`,
    a reviewer's bare `harness/loops/panel.py`, and a diff's `a/harness/loops/panel.py`
    or `b/…`. An absolute `/home/rich/source/quarterback/harness/loops/panel.py`
    reaches here too, from a seat that had a checkout and answered with the path it
    actually opened; it is left as-is apart from the slash normalisation, because
    the repo root is not knowable from inside a pure function and guessing at it by
    hunting for a familiar-looking segment would fold two different checkouts of
    two different repos onto one key.
    """
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    for prefix in ("a/", "b/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text


def _header_path(line: str) -> str | None:
    """The new-side path a `diff --git a/… b/…` header names, provisionally.

    Provisional because the `+++` line that follows is authoritative and
    overrides this — the header carries TWO paths with an unescaped space between
    them, so a path containing a space cannot be split out of it with certainty.
    Two of the three cases can be: the ordinary `a/P b/P` has equal halves and the
    split point is arithmetic rather than a guess, and a quoted pair has an
    unambiguous `" "` between them. A rename with a space in either name falls to
    the first ` b/` and is corrected a line later.

    The same rule `panel_core._diff_file_path` uses, on purpose. It is restated
    here rather than imported so this module stays free of the panel's foundation
    (see the module docstring) — and the two only ever have to agree about the
    header, since every consumer of either resolves to the `+++` path in the end.
    """
    rest = line[len("diff --git "):].strip()
    if len(rest) > 5 and (len(rest) - 5) % 2 == 0:
        half = (len(rest) - 5) // 2
        a_side, b_side = rest[:2 + half], rest[2 + half:]
        if a_side.startswith("a/") and b_side == " b/" + a_side[2:]:
            return a_side[2:]
    if rest.startswith('"') and rest.endswith('"') and '" "' in rest:
        return normalize_path(rest.rsplit('" "', 1)[1].rstrip('"'))
    _, sep, tail = rest.partition(" b/")
    return tail.strip() if sep else None


def parse_diff_scope(diff: str) -> dict[str, list[tuple[int, int]]]:
    """Map each changed path to the 1-based line ranges the diff touches, in the
    file as it stands AFTER the change. Paths are in :func:`normalize_path` form;
    ranges are in the order the diff states them, which is ascending.

    **Only the `@@` header is read, never the hunk body.** The header states the
    new-side start and length, so the range is arithmetic on two integers a
    walker would otherwise have to re-derive by counting `+` and ` ` lines. That
    walker is the version with bugs in it, and they are not hypothetical: a
    `\\ No newline at end of file` marker is neither, and a diff OF a diff — which
    this repo's own test fixtures are full of — carries body lines beginning with
    `+`, `-`, `@@` and `+++` that a counter must know not to believe. Every one of
    those shifts the numbering for the rest of the file, silently, and a finding
    matched against a scope whose line numbers are three off is matched against
    the wrong place while reporting a confident answer.

    `+++ b/P` is the authoritative path and overrides the `diff --git` header,
    which cannot be split with certainty (see :func:`_header_path`). It is
    honoured only when the line before it began with `--- `, because a body line
    whose content is `++ something` renders as `+++ something` and would otherwise
    re-key the rest of the file under whatever followed. That pairing also lets a
    plain `diff -u` with no `diff --git` header parse at all.

    `+++ /dev/null` is a deletion: the file has no new side, so nothing in it can
    be located and it contributes no ranges. A hunk with a new-side count of zero
    is a pure deletion WITHIN a file and does contribute one — collapsed to the
    single line the removed text sat above, because "the fixer deleted the lines a
    finding was about" is the case cross-round matching most needs to see.
    """
    scope: dict[str, list[tuple[int, int]]] = {}
    path: str | None = None
    prev_minus = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path, prev_minus = _header_path(line), False
            continue
        if prev_minus and line.startswith("+++ "):
            tok = line[4:].strip()
            path = None if tok == "/dev/null" else normalize_path(tok.strip('"'))
            prev_minus = False
            continue
        prev_minus = line.startswith("--- ")
        hunk = _HUNK_RE.match(line)
        if not hunk or path is None:
            continue
        start = max(int(hunk.group(1)), 1)
        count = int(hunk.group(2)) if hunk.group(2) is not None else 1
        scope.setdefault(path, []).append((start, max(start + count - 1, start)))
    return scope


def finding_line_bounds(finding: Mapping[str, Any]) -> tuple[int, int] | None:
    """The finding's `(start, end)` line range, or None when it names no line.

    **None is a real answer and the callers must handle it.** mergeCraft's version
    of this returns `(1, 1)` for a finding with no line, and that default is a
    quiet lie: it puts every unlocated finding on line 1 of its file, where they
    all match each other and all match any hunk near the top. Returning None
    instead forces the question up to :func:`same_finding`, which refuses (see
    there), and leaves the caller able to see WHY a finding matched nothing.

    Reads the three shapes a finding arrives in: `line_range: [start, end]`, an
    explicit `start_line`/`end_line` pair, and this repo's own single `line`. A
    single line is a one-line span and not a point, so a caller never has to know
    which shape it was handed. A range stated backwards is put back in order
    rather than refused — a reviewer that swapped two numbers still named the
    span, and refusing it would throw away a locatable finding over an ordering.
    """
    span = finding.get("line_range")
    if isinstance(span, (list, tuple)) and len(span) == 2:
        start, end = _as_line(span[0]), _as_line(span[1])
    else:
        start = _as_line(finding.get("start_line"))
        if start is None:
            start = _as_line(finding.get("line"))
        end = _as_line(finding.get("end_line"))
    if start is None and end is None:
        return None
    if start is None:
        start = end
    if end is None:
        end = start
    return (end, start) if end < start else (start, end)


def _as_line(value: object) -> int | None:
    """One line number, or None. A bool is refused before anything else: `True`
    is an `int` to Python and would silently become line 1, which is the same
    class of quiet default :func:`finding_line_bounds` exists to refuse."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 1 else None


def is_locatable(finding: Mapping[str, Any]) -> bool:
    """Can this finding be matched by locality at all — does it name a path AND a
    line? The predicate exists so a caller can SAY why a finding matched nothing
    ("the seat named no line") instead of reporting it as "the earlier round did
    not raise this", which is the wrong sentence and the one that costs a cycle."""
    return bool(_path_of(finding)) and finding_line_bounds(finding) is not None


def _path_of(finding: Mapping[str, Any]) -> str:
    for field in _PATH_FIELDS:
        got = normalize_path(finding.get(field))
        if got:
            return got
    return ""


def line_intersects_hunks(path: str, start: int | None, end: int | None,
                          scope: Mapping[str, list[tuple[int, int]]], *,
                          slack: int = DEFAULT_LINE_SLACK) -> bool:
    """Does `[start - slack, end + slack]` on `path` intersect any hunk in `scope`?

    `scope` is what :func:`parse_diff_scope` returned; `path` is normalised here,
    so a caller may pass whatever spelling it holds.

    A span with no bounds answers **False**, and that is a decision rather than an
    oversight. mergeCraft's equivalent answers True whenever the path appears in
    the diff at all, which reads "somewhere in this file" as "here" — on a file a
    fix pass rewrote, that attributes every file-level finding to the fixer. False
    is the honest answer to "is this place inside the diff" when no place was
    named, and the caller that genuinely wants the file-level question still has
    it in one legible line: `normalize_path(p) in scope`. Stated at the call site,
    where a reader can see it, rather than folded in here where nobody would.
    """
    ranges = scope.get(normalize_path(path))
    if not ranges or start is None or end is None:
        return False
    lo, hi = start - slack, end + slack
    return any(lo <= hunk_end and hi >= hunk_start for hunk_start, hunk_end in ranges)


def same_finding(a: Mapping[str, Any], b: Mapping[str, Any], *,
                 slack: int = DEFAULT_LINE_SLACK) -> bool:
    """Are these two findings, from different rounds, about the same place?

    Same file, and spans that overlap once one of them is widened by `slack`.
    Nothing either reviewer WROTE is read — not the title, not the detail, not the
    severity — because a matcher that consulted the wording would inherit the
    wording's instability, which is the defect this module exists to route around.

    **A finding that names no line matches nothing, including another finding that
    names no line.** The tempting fallback is path equality, and it is worse than
    no match at all: on `panel_rounds.py` — 8,549 lines, and the file most rounds
    raise something in — it would declare every unlocated finding in the file to be
    every other one, so a round's answer would carry forward onto a defect nobody
    asked about and `confident: true` would be reachable by accident. Refusing is
    visible rather than silent: :func:`is_locatable` tells a caller which findings
    were excluded and why, and the caller reports "the seat named no line" rather
    than "the earlier round did not raise this".
    """
    path_a, path_b = _path_of(a), _path_of(b)
    if not path_a or path_a != path_b:
        return False
    bounds_a, bounds_b = finding_line_bounds(a), finding_line_bounds(b)
    if bounds_a is None or bounds_b is None:
        return False
    start_a, end_a = bounds_a
    start_b, end_b = bounds_b
    return start_b <= end_a + slack and start_a - slack <= end_b


def _closeness(a: tuple[int, int], b: tuple[int, int]) -> int:
    """How well two spans coincide: the number of lines they share, falling
    negative as they separate. One monotone scale over both cases — overlaps and
    the near-misses `slack` let through — so "closest" is one comparison and never
    a branch that orders the two by different rules."""
    return min(a[1], b[1]) - max(a[0], b[0]) + 1


def match_across_rounds(prior: Iterable[Mapping[str, Any]],
                        current: Iterable[Mapping[str, Any]], *,
                        slack: int = DEFAULT_LINE_SLACK) -> dict[str, str]:
    """Map each prior finding's key to the current finding that is about the same
    place, where one exists. A prior finding matching nothing is ABSENT from the
    result — never present with an empty value, so `key in matched` is the whole
    question and a caller cannot read a miss as a match by forgetting to test the
    value.

    **One-to-one.** Two prior findings that both overlap one current finding
    cannot both claim it, or a single reworded finding would answer for two
    earlier ones and a cycle would count itself converged on one fix. The claim is
    resolved deterministically: closest overlap first (:func:`_closeness`), then
    lowest start line, then key order. The priors themselves are walked in sorted
    order rather than in the order the caller happened to hold them, so the same
    two rounds give the same map whatever built the lists — a matcher whose answer
    depended on dict or list ordering would be reproducible in a test and not in a
    replay, which is the worst of both.

    A finding with no key is skipped on either side. The key is the only handle
    the result has, and inventing one here would mint an identity no other layer
    could resolve.
    """
    todo = sorted((f for f in prior if _key_of(f)),
                  key=lambda f: (_path_of(f), finding_line_bounds(f) or (0, 0), _key_of(f)))
    unclaimed = sorted((f for f in current if _key_of(f)),
                       key=lambda f: (_path_of(f), finding_line_bounds(f) or (0, 0), _key_of(f)))
    matched: dict[str, str] = {}
    for old in todo:
        bounds = finding_line_bounds(old)
        if bounds is None:
            continue
        candidates = [f for f in unclaimed if same_finding(old, f, slack=slack)]
        if not candidates:
            continue
        best = min(candidates, key=lambda f: (-_closeness(bounds, finding_line_bounds(f)),  # type: ignore[arg-type]
                                              (finding_line_bounds(f) or (0, 0))[0], _key_of(f)))
        matched[_key_of(old)] = _key_of(best)
        unclaimed.remove(best)
    return matched


def _key_of(finding: Mapping[str, Any]) -> str:
    return str(finding.get("key") or "").strip()


# ------------------------------------------------------------ what a finding is ABOUT

#: The three populations a finding's rate is split across (#774, closing the
#: reporting half of #751). It is a classification of the PATH and nothing else —
#: not of severity, not of how load-bearing the file is — because the number it
#: feeds is `escalate_on.fix_injection`, and that rung ends cycles.
#:
#: The measurement behind the split: `lexray#1611` round 2 raised thirteen
#: findings, six of them about TEXT — a stale comment, a doc table, message
#: strings — and four of those said in their own words that the previous round had
#: made them stale. Pooled into one 69% injection rate they read as the loop
#: circling and the cycle stopped. A repo that states one fact in five prose files
#: produces five findings from one edit; that is the repo's shape, not the loop
#: failing to converge, and it must not be able to end a cycle on its own.
KIND_PRODUCTION, KIND_TEST, KIND_PROSE = "production", "test", "prose"

#: A path segment that makes everything beneath it test material. `fixtures` and
#: `testdata` are here with the obvious two because a fixture is routinely a `.md`
#: or a `.json`, and a rate about test churn should own it rather than leaking it
#: into prose.
TEST_DIR_SEGMENTS = frozenset({
    "test", "tests", "testing", "testdata", "fixtures", "__tests__", "__mocks__",
})

#: Test files by their own name, for the ones that live beside the code they
#: exercise. `.test.sh` is in this repo (`harness/tests/create_worktree_nginx.test.sh`);
#: the JS/TS spellings are here so the rule set does not have to be reopened the
#: first time a panel reviews a front-end PR.
TEST_BASENAMES = frozenset({"conftest.py"})
TEST_NAME_PREFIXES = ("test_", "test-")
TEST_NAME_SUFFIXES = ("_test.py", ".test.sh", ".test.ts", ".test.js", ".test.tsx",
                      ".spec.ts", ".spec.js", ".spec.tsx")

#: Prose by extension, by directory, and by the conventional extensionless names.
#: `changelog.d` is named explicitly: its fragments are `.md` today and the
#: directory is what makes them prose, not the suffix they happen to carry.
#: The basenames match the WHOLE name and never a stem, or `license.py` — a real
#: module name — would be scored as prose on the strength of the word in it.
PROSE_SUFFIXES = (".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc")
PROSE_DIR_SEGMENTS = frozenset({"doc", "docs", "changelog.d"})
PROSE_BASENAMES = frozenset({
    "CHANGELOG", "README", "LICENSE", "LICENCE", "NOTICE", "AUTHORS", "CONTRIBUTING",
})


def finding_kind(path: str) -> str:
    """`production` | `test` | `prose` — what a finding at this path is ABOUT.

    Test rules are tried FIRST, so a `.md` under a test tree is test material and
    not prose. That ordering is chosen for fixtures: a recorded diff, an expected
    report, a sample changelog fragment all live under `tests/` with prose
    extensions, and they are churn in the suite rather than statements of fact the
    repo makes about itself. It costs a `tests/README.md`, which lands in the test
    population; that is one file and the wrong answer is cheap, where a directory
    of fixtures scored as prose would move a rate.

    **The ambiguous case, decided: a `.md` under `harness/commands/` is `prose`.**
    Those files are this harness's executable contract — `panel.md` is not
    documentation of the loop, it IS the loop — and the argument for calling them
    production is real. It is declined because this is a classification of what a
    finding is about, not of how much a file matters. A defect in a command file is
    a defect in WORDING: an instruction that reads two ways, a step that contradicts
    another. The fix is an edit to text, and the same phrasing is restated across
    the seventeen files in that directory, so one such edit produces N findings —
    exactly the population #751 measured and exactly what must not be pooled with
    code defects. Nothing is hidden by the choice: #774 keeps the pooled rate
    alongside the split, so a command-file finding still counts, it just cannot
    end a cycle by itself.

    An unrecognised path — including the empty one, from a finding that named no
    file — is `production`. The catch-all is deliberately the STRICTEST population:
    a path this rule set cannot read must not be excused out of the code rate on
    the strength of not being understood.
    """
    normal = normalize_path(path)
    segments = [s for s in normal.split("/") if s]
    name = segments[-1] if segments else ""
    lower = name.lower()
    if any(s.lower() in TEST_DIR_SEGMENTS for s in segments[:-1]):
        return KIND_TEST
    if (name in TEST_BASENAMES or lower.startswith(TEST_NAME_PREFIXES)
            or lower.endswith(TEST_NAME_SUFFIXES)):
        return KIND_TEST
    if any(s.lower() in PROSE_DIR_SEGMENTS for s in segments[:-1]):
        return KIND_PROSE
    if lower.endswith(PROSE_SUFFIXES) or name.upper() in PROSE_BASENAMES:
        return KIND_PROSE
    return KIND_PRODUCTION
