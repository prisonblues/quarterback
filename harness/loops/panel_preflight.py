#!/usr/bin/env python3
"""The verdict BEFORE the round: is this diff worth reading, and read as WHAT.

Everything the panel knew about a diff, it knew too late. #75 made the report say
`N of M configured` and name the seats whose diff was cut — in `config_notes`,
*after* the round. The `~120,000`-byte argv cap is written down in plan.md as "a
property of the harness rather than of a run, so it will recur on every diff over
~120k": known, stated, and gating nothing. #41 makes *later* rounds cheaper and
has nothing to say about round 1. So four seats were dispatched at full effort
against PR #137 — **763,375 chars, 6.4x antigravity's cap, on a pure move** — and
the only thing that stopped it was a human asking "is this a crazy token count?"

The token cost is the second problem. The first is that **a truncated read which
produces findings is worse than no review**, because the next step of the cycle
briefs a fixer to resolve every one of them to a "nothing left to improve" bar.
Every relocated line in a move appears twice, once as a delete and once as an
add, so the overwhelming bulk of that 763KB is code nobody changed — and a
finding about it is a finding about code that was already in `main` and already
reviewed when it landed there. The review manufactures work.

Three verdicts, and the second is the interesting one:

``run``
    Nothing here objects. Byte-identical behaviour to every release before this
    one, which is deliberately the answer whenever no seat declares a cap at all
    (see :func:`seat_ceilings`).

``manifest``
    The diff does not fit, AND it is move-shaped: the added lines are a
    near-permutation of the deleted ones. A move is not reviewable as content by
    anybody, at any budget, so it is reviewed as a **manifest** instead — what
    moved where, what did NOT survive, what changed *besides* moving, and which
    definitions the change ADDS in more than one place (#62's trap: a clean merge
    that keeps both copies and silently uses the second). That is a different and
    much smaller prompt, and it is the one a reviewer could actually answer.

``refuse``
    The diff does not fit by some multiple of the smallest configured cap and
    there is no smaller honest question to ask instead — either because it is not
    move-shaped, or because a manifest of it was tried and did not help. Refuse,
    loudly, and say why. ``--force`` overrides.

**Only ``manifest`` is a new answer; ``refuse`` and ``run`` are both reachable for
a move.** A move-shaped diff over a cap gets the manifest at any multiple, but
only when there IS a manifest worth substituting — `manifest_moves` can be off,
and on a small move just over a small ceiling the manifest can come out no smaller
than the diff (its brief is a fixed kilobyte). When neither holds, the multiple
decides as it does for content: over it, refuse; under it, run as an ordinary
truncated round. What must never happen is that outcome being *silent*, so the
``run`` verdict then carries a reason saying the manifest was tried and why it did
not help — see :func:`preflight`.

Only the half of #62's trap a DIFF CAN SHOW is checked, and the manifest says so
rather than reading as clean: a definition the change adds in two new places is
visible; an original left behind in a file the diff never touches is not in the
diff at all, and no amount of parsing recovers it from `gh pr diff` alone.

**This is not a diff budget, and the distinction is the whole design.** `v2.16`
(#49) refused a default budget on evidence — truncating when nothing forces it
biases toward false positives — and that reasoning stands untouched here. A
budget answers *what to send*; this answers *whether to start*, and only ever
against a ceiling the repo or the kernel already declared. On a repo that sets no
`max_diff_chars` and enables no argv-bound seat there is no cap, so nothing here
ever fires and no number this file invented is ever applied to anyone's diff.

**Nor is it a silent skip.** A panel that quietly declines is #62's disease in a
new place — a merge gate trusting a proxy. So a refusal prints, carries
``reviewed: False`` and a ``skip_reason``, is recorded on the board like any other
run, and is posted to the PR under ``--post``: "no review" must never be readable
as "clean".

Split into its own module rather than added to `panel.py` for #129's reason. That
file's seats section alone was over the argv cap, which meant the one seat whose
prompt travels in argv could never be handed the file it was reviewing — the
irony this issue was filed about, and not one worth re-earning.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from panel_core import (ARGV_PROMPT_MAX_BYTES, CLI_BIN, seat_installed,
                        _diff_file_path)
# `_ci_line` renders #548's states, so it names them rather than spelling the three
# strings a second time — a state named in two places is a state the two places can
# come to disagree about, and this one is compared for equality in four modules. The
# import is one-way: panel_scope sits on panel_core and knows nothing of this module.
from panel_scope import LOCAL_FAIL, LOCAL_PASS, LOCAL_UNREAD

# ----------------------------------------------------------------------------- tunables

#: What fraction of the LARGER side of a diff must be relocated text before the
#: change is called a move. High on purpose, and the reason is a false-positive
#: mode this measure genuinely has: identical boilerplate — `    return None`,
#: `        pass`, a closing brace — matches itself across unrelated files, so a
#: large refactor of repetitive code scores above zero without being a move.
#: 0.9 is well clear of that, and the manifest still LISTS the residue rather
#: than asserting the move was clean, so a misread costs a reviewer a look at 10%
#: of the change rather than a silent "nothing to see".
DEFAULT_MOVE_SHAPE_RATIO = 0.9

#: How many times the smallest configured seat cap a diff may exceed before the
#: round is refused. 3 rather than 1, because "over the cap" is ordinary
#: truncation and has been reported as such since #75 — the case this exists for
#: is the one where truncation has stopped being a caveat and become the review.
#: PR #137 was 6.4x. **`0` disables the refusal and keeps the manifest** — and only
#: `0`: `null` and an absent key mean "use this default", the way they do for every
#: other setting in this harness, so writing `null` to opt out leaves the refusal
#: ON. This said "`None` or 0" and was wrong about the half an operator would reach
#: for; see :func:`_rule`, which is where the two spellings are told apart.
DEFAULT_REFUSE_OVER_CAP_MULTIPLE = 3

#: Rows in the manifest's "what moved where" table, and lines in each residue
#: listing. The manifest's size is a function of the CHANGE's shape, not of the
#: diff's length, and these are what keep that true: a move at the 0.9 threshold
#: can still leave 10% of a 763KB diff as residue, which is 76KB and back where
#: we started. Over the cap is stated and the count given, so an elided tail is a
#: number a reader can act on rather than an absence.
MANIFEST_TABLE_ROWS = 200
MANIFEST_RESIDUE_LINES = 120

#: What labels the manifest where a round would put ``--- DIFF ---``. It travels
#: as :attr:`panel_scope.ReviewScope.header`, and it says NOT A DIFF because the
#: material under it is the one thing in the panel that looks like a diff summary
#: and is not one: a seat that reads it as a diff will report on file names.
MOVE_MANIFEST_HEADER = "--- MOVE MANIFEST (NOT A DIFF) ---"

#: A line of residue is quoted at this width. Long enough to identify the line,
#: short enough that 120 of them are a listing rather than a diff.
MANIFEST_LINE_CHARS = 120

#: How many one-sided paths :meth:`DiffShape.as_dict` serialises before it elides
#: the rest and says how many it dropped. The manifest's own file table has been
#: capped since it was written (:data:`MANIFEST_TABLE_ROWS`) and these two lists
#: were not, which mattered more rather than less: the manifest is read once by one
#: seat, and `preflight.shape` rides in `--json`, in `--json-file` and in the
#: payload piped to `qb record-review` on EVERY run. A 700-file refactor wrote 700
#: paths into every board row for that PR. Lower than the manifest's cap on
#: purpose — this is a shape summary a consumer reads to tell a split from a
#: rewrite, not the listing a reviewer works from — and an elided tail is a number
#: rather than an absence, exactly as it is in the manifest.
PAYLOAD_FILE_ROWS = 40

#: One level of nested angle brackets, which is what a TS generic parameter list
#: is: `<T>`, `<T, U>`, `<T extends string>` and `<T extends Foo<Bar>>`. Written
#: once, named, and defined AHEAD of the `_DEF_PATTERNS` doc-comment rather than
#: between it and the tuple: a `#:` block documents the NEXT binding, so a constant
#: dropped in the middle would have quietly taken that block for its own. Written
#: with an explicit nesting alternation rather than `<.*?>`, because the lazy form
#: matches the `<` of a comparison and stops at the next `>`.
_TS_GENERICS = r"<[^<>]*(?:<[^<>]*>[^<>]*)*>"

#: What a definition looks like, for the duplicate check. Python and the JS/TS
#: spellings only, because those are what the fleet's repos are written in and a
#: pattern that matches nothing is worse than an absent section: it reads as
#: "checked, and clean". :func:`duplicate_definitions` says which spellings it
#: covered for exactly that reason — a Go or Rust move gets the rest of the
#: manifest and an explicit "not checked here" instead of a false all-clear.
#:
#: The JS/TS half started as `function name(` alone, which is the spelling modern
#: JS/TS uses least. `const f = () => {}` and `class Foo extends Bar {` were both
#: invisible while :data:`_DEF_LANGUAGES` told the reader the section had covered
#: "JavaScript/TypeScript" — the same false all-clear the tunable above exists to
#: avoid, inside a language the manifest named. So: brace-style classes, `export`
#: and `export default`, generators, arrow and function-expression bindings, and
#: the TS type-level declarations — and then, because the first pass at those left
#: gaps in the middle of spellings it had just claimed, GENERIC arrows
#: (`const f = <T>(x: T) => x`), function-TYPED bindings
#: (`const f: (x: T) => U = x => x`), `const enum` and `export declare class`.
#: Each of those was a name the section would have found nothing for while telling
#: the reader it had looked, which is the failure this whole tunable's comment is
#: about.
#:
#: Class and object METHODS (`name(args) {`) are deliberately NOT matched, and
#: that is a limit rather than an oversight. A bare `name(args) {` is spelled
#: identically to a call, an `if`, a `for` and a `catch`, so a pattern loose
#: enough to catch a method flags every one of those as a duplicated definition —
#: and a section that fires on `if (ok) {` appearing twice is worse than one that
#: misses a method, because the reader stops believing it. Neither is a definition
#: whose signature wraps onto a second line: the measurement is per LINE, since
#: that is what a diff's multiset intersection is made of. Both are named in the
#: manifest's disclaimer, where a reader can act on them.
_DEF_PATTERNS = (
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\("),
    # `class Foo:` / `class Foo(Base):` (Python) and `class Foo {` /
    # `class Foo extends Bar {` / `export default class Foo` /
    # `export declare class Foo` (JS/TS), in one pattern: the name is the signal
    # and what follows it differs only by dialect.
    re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:abstract\s+)?"
               r"class\s+([A-Za-z_$][\w$]*)\b"),
    re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?"
               r"function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("),
    # `const f = () => {}`, `const f = async (x) => x`, `let f = function () {}`,
    # `export const f: Handler = x => x`, `const f = <T>(x: T) => x`,
    # `const f: (x: T) => U = x => x`. The right-hand side is required to be a
    # function of some spelling — a bare `= (` would match `const n = (a + b);`
    # and file an arithmetic expression as a definition.
    #
    # Two of those forms were misses in the shipped pattern, both inside a spelling
    # `_DEF_SPELLINGS` told the reader had been covered: the parenthesised arrow
    # alternative required `(` immediately, so no GENERIC arrow matched, and the
    # type annotation was `[^=]+?`, which cannot cross the `=` of a `=>` and so
    # gave up on every function-typed variable. Hence `_TS_GENERICS` before the
    # parameter list, and an annotation body that admits `=>` and nothing else
    # containing `=` — the restriction is what keeps `const a = b, c = (x) => y`
    # from being filed under the name `a`.
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*"
               r"(?::(?:[^=]|=>)+?)?=\s*(?:async\s+)?"
               rf"(?:function\b|(?:{_TS_GENERICS}\s*)?\([^)]*\)\s*(?::[^=]*?)?=>"
               r"|[A-Za-z_$][\w$]*\s*=>)"),
    # `const enum Colour {` is admitted alongside `enum`/`interface`: it matches
    # neither the plain enum spelling (which allowed only `export`/`declare`
    # before the keyword) nor the binding pattern above (which requires an `=`),
    # so it was a silent miss in the middle of a covered spelling.
    re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?(?:const\s+)?(?:interface|enum)\s+"
               r"([A-Za-z_$][\w$]*)\b"),
    # `type X = …` and `type X<T> = …`. The `=`/`<` is required: without it this
    # matches a line that merely starts with the word "type".
    re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?type\s+([A-Za-z_$][\w$]*)\s*[<=]"),
)

#: The cheap pre-test that decides whether a line is worth running the six
#: patterns above against. Definition sites are a small fraction of any diff and
#: every pattern is anchored on one of these words, so one `search` rejects the
#: overwhelming majority of lines — which is what keeps :func:`_hunk_bodies`
#: cheap enough to run on every round now that it collects definition sites per
#: file (see there) rather than only over the distinct bodies.
_DEF_HINT = re.compile(r"\b(?:def|class|function|interface|enum|type|const|let|var)\b")

#: Languages :data:`_DEF_PATTERNS` actually covers, named in the manifest so a
#: reader of a Go move is told the section did not apply rather than left to read
#: an empty one as a clean bill.
_DEF_LANGUAGES = ("Python", "JavaScript/TypeScript")

#: The spellings, for the same reason one level finer. "JavaScript/TypeScript" is
#: a language and not a promise about syntax, and a reader who knows a method or a
#: wrapped signature was not looked at can go and look at it themselves.
_DEF_SPELLINGS = (
    "`def`", "`class`", "`function`",
    "`const`/`let`/`var` bound to an arrow (generic and function-typed forms "
    "included) or a function expression",
    "`interface`/`enum`/`const enum`/`type`",
)


# ----------------------------------------------------------------------------- shape

def _paths(paths: tuple[str, ...]) -> tuple[list[str], int]:
    """``(listed, elided)`` — a one-sided file list cut to
    :data:`PAYLOAD_FILE_ROWS`, with the count of what was left out.

    The count is emitted even when it is 0, because the alternative is a consumer
    unable to tell "these are all of them" from "these are the first forty". A
    truncated list that does not say it was truncated is the shape of claim this
    whole module exists to stop making.
    """
    return list(paths[:PAYLOAD_FILE_ROWS]), max(0, len(paths) - PAYLOAD_FILE_ROWS)


@dataclass(frozen=True)
class DiffShape:
    """What a diff IS, measured rather than modelled.

    The one question worth asking mechanically about a large diff is whether its
    added lines are a near-permutation of its deleted ones, because that is what
    a rename, a split and a file move all look like and it is the shape no
    content review can say anything useful about.

    Measured off the diff text the round already has in hand, deliberately NOT
    off `git diff -M -C`'s similarity index. Rename detection is a property of
    the git invocation, and the panel's diff comes from `gh pr diff` over the
    API: asking git would mean a checkout of the PR head the panel never has, and
    trusting the API to have done the detection means trusting something that is
    not promised. A multiset comparison needs neither and is the same answer.
    """

    #: Characters of diff, which is the quantity a configured `max_diff_chars` is
    #: expressed in.
    chars: int
    #: Non-blank added / removed lines. Blank ones are excluded from BOTH sides:
    #: a blank line matches every other blank line, so counting them inflates the
    #: move ratio in exact proportion to how airy the code is.
    added: int
    removed: int
    #: Lines appearing on both sides — the multiset intersection, so a line
    #: deleted twice and added three times contributes two. This is the
    #: relocated text.
    moved: int
    #: Files the diff touches, and the two one-sided subsets. A split shows up
    #: here as one file losing everything and several gaining it.
    files: int
    #: The SAME diff in UTF-8 bytes, because one of the two ceilings this module
    #: measures against is not expressed in characters. `max_diff_chars` is a
    #: character count; :data:`panel_core.ARGV_PROMPT_MAX_BYTES` is the kernel's
    #: `MAX_ARG_STRLEN`, in bytes. Compared against `chars` an argv ceiling is
    #: understated by exactly the diff's non-ASCII density — and this repo's own
    #: diffs are full of em-dashes, arrows and box characters in comments and
    #: reports, so the error is not theoretical and it runs in the direction that
    #: lets an over-cap diff through. :func:`preflight` picks the reading that
    #: matches the ceiling it found; both are serialised so the choice is checkable
    #: rather than taken on trust.
    #:
    #: Defaulted so a hand-built `DiffShape` in a test stays a two-line literal;
    #: :func:`diff_shape` always measures it.
    nbytes: int = 0
    #: KEYWORD-ONLY, and that is a guard rather than a style. `nbytes` was inserted
    #: ahead of these two, which changed what the sixth POSITIONAL argument means
    #: without changing the arity: an existing
    #: `DiffShape(chars, added, removed, moved, files, added_only, removed_only)`
    #: would bind a tuple of paths to an `int` field and serialise
    #: `"bytes": ["a.py"]` into every board record for that PR, with no TypeError
    #: anywhere to notice it. Nothing in this repo constructs it that way — the
    #: class is exported, though, and its docstring invites hand-built instances —
    #: so the seventh positional argument is made an error instead of a silent
    #: rebinding. Costs a keyword at the one real call site.
    files_added_only: tuple[str, ...] = field(default=(), kw_only=True)
    files_removed_only: tuple[str, ...] = field(default=(), kw_only=True)

    @property
    def move_ratio(self) -> float:
        """Relocated lines as a fraction of the larger side, 0.0 when nothing
        changed.

        Against the LARGER side rather than against the total, because that is
        the conservative reading. A change that relocates 100 lines and writes 50
        new ones scores 0.67 here and 0.8 by `2*moved/(added+removed)` — and the
        50 new lines are exactly what a content review is for, so the measure
        that is slower to call it a move is the right one.
        """
        widest = max(self.added, self.removed)
        return self.moved / widest if widest else 0.0

    def is_move(self, threshold: float = DEFAULT_MOVE_SHAPE_RATIO) -> bool:
        """Is this a move? False for an empty diff, which relocates nothing.

        `>=`, so a threshold of 1.0 means "a move with no residue at all" and is
        reachable rather than being a switch that can never be true.
        """
        return bool(self.moved) and self.move_ratio >= threshold

    def as_dict(self) -> dict:
        added_only, added_more = _paths(self.files_added_only)
        removed_only, removed_more = _paths(self.files_removed_only)
        return {"chars": self.chars, "bytes": self.nbytes,
                "added": self.added, "removed": self.removed,
                "moved": self.moved, "move_ratio": round(self.move_ratio, 4),
                "files": self.files,
                "files_added_only": added_only,
                "files_added_only_elided": added_more,
                "files_removed_only": removed_only,
                "files_removed_only_elided": removed_more}


@dataclass(frozen=True)
class DiffParts:
    """What one pass over a diff yields, so nothing has to make a second one.

    :func:`diff_shape` and :func:`move_manifest` both need the same three
    structures and used to build them separately: the verdict measured a shape,
    and then the manifest it had just decided to substitute re-parsed the whole
    diff to rebuild what the measurement was made from. On the 763 KB case that is
    a second full pass and a second pair of Counters over ~10,000 lines, for data
    that was in hand and thrown away. It is also two answers where there should be
    one — the manifest must describe the diff the verdict weighed, and two parses
    of one string are two chances to disagree about that.
    """

    #: Non-blank added / removed line bodies, as multisets — see
    #: :attr:`DiffShape.added` for why blanks are out.
    added: Counter
    removed: Counter
    #: ``path -> (added, removed)``, counting EVERY body including blanks, because
    #: a file's tally is about how much of it the change touched.
    per_file: dict[str, tuple[int, int]]
    #: ``definition name -> {file: times added}``, over the spellings in
    #: :data:`_DEF_PATTERNS`. Collected here rather than re-derived from `added`
    #: because `added` is keyed by body and has no idea which file a body came
    #: from — and "which files is this name now defined in" is the one thing a
    #: reader of the duplicate section can act on without the checkout.
    def_sites: dict[str, Counter]


def _hunk_bodies(diff: str) -> DiffParts:
    """Parse a diff once: the line multisets, each file's tally, and where each
    definition was added.

    Only lines INSIDE a hunk are counted, and "inside" means "after this file's
    first ``@@``". That is not fussiness: a file header's ``--- a/x`` and
    ``+++ b/x`` both start with the marker characters, and so can a line of
    content (a diff of a diff, a Markdown rule, a docstring underline). Anchoring
    on the hunk header is the only reading that cannot confuse the two, and it
    costs one boolean.

    Blank and whitespace-only bodies are dropped from both sides — see
    :attr:`DiffShape.added`.

    Files are keyed by :func:`panel_core._diff_file_path`, the parser the rest of
    the panel already uses, and only ever on a ``diff --git`` line — so its
    ``+++`` branch, which would defeat the hunk anchoring above, is never reached.
    Sharing it is not tidiness: `unread_files` and `_provenance` compare paths
    across three producers through `_same_file`, and a fourth spelling of "what a
    path is" would misattribute in silence rather than fail. The hand-rolled
    version this replaced also got `a/x b/x` wrong, which is every ordinary file
    in every diff.

    The definition scan runs on ADDED lines only, behind :data:`_DEF_HINT`: the
    duplicate question is about what the change puts in more than one place, and
    every one of the six patterns is anchored on a word that hint carries, so the
    overwhelming majority of lines cost one `search` and no more. That is what
    keeps "cheap enough to run on every round" true of a per-file index.
    """
    added: Counter[str] = Counter()
    removed: Counter[str] = Counter()
    per_file: dict[str, list[int]] = {}
    def_sites: dict[str, Counter] = {}
    cur = ""
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            # A new file's headers begin; nothing is countable again until its
            # first hunk.
            in_hunk = False
            cur = _diff_file_path(line) or line.strip()
            per_file.setdefault(cur, [0, 0])
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+"):
            body = line[1:]
            tally = per_file.setdefault(cur, [0, 0])
            tally[0] += 1
            if body.strip():
                added[body] += 1
                name = _def_name(body)
                if name:
                    def_sites.setdefault(name, Counter())[cur] += 1
        elif line.startswith("-"):
            body = line[1:]
            tally = per_file.setdefault(cur, [0, 0])
            tally[1] += 1
            if body.strip():
                removed[body] += 1
    return DiffParts(added=added, removed=removed,
                     per_file={f: (a, r) for f, (a, r) in per_file.items()},
                     def_sites=def_sites)


def _def_name(body: str) -> str:
    """The name this line defines, or ``""``. See :data:`_DEF_PATTERNS` for what
    counts as a definition and, just as importantly, what deliberately does not."""
    if not _DEF_HINT.search(body):
        return ""
    for pat in _DEF_PATTERNS:
        m = pat.match(body)
        if m:
            return m.group(1)
    return ""


def _shape_of(parts: DiffParts, diff: str) -> DiffShape:
    """The measurement, off an already-parsed diff. Split from
    :func:`diff_shape` so a caller that needs the parts as well — the manifest
    path — pays for one parse instead of two."""
    moved = sum((parts.added & parts.removed).values())
    # A file the change only adds to, or only takes from — the shape of a split's
    # source and its destinations. Files with no counted lines either way (a mode
    # change, a pure rename git recorded without content) are neither, and the
    # manifest gives them their own label rather than calling them both.
    added_only = tuple(f for f, (a, r) in parts.per_file.items() if a and not r)
    removed_only = tuple(f for f, (a, r) in parts.per_file.items() if r and not a)
    return DiffShape(chars=len(diff), added=sum(parts.added.values()),
                     removed=sum(parts.removed.values()), moved=moved,
                     files=len(parts.per_file), nbytes=len(diff.encode()),
                     files_added_only=added_only,
                     files_removed_only=removed_only)


def diff_shape(diff: str) -> DiffShape:
    """Measure a diff. Cheap enough to run on every round: one pass over the
    text and a Counter intersection, against the minutes a round costs."""
    return _shape_of(_hunk_bodies(diff), diff)


# ----------------------------------------------------------------------------- the cap

# `seat_installed` is imported from `panel_core` (#222), not defined here. Both
# halves of this module's own history are the argument for that: this file added one
# for its ceiling calculation, #222 added another beside `CLI_BIN` for `budgets` and
# `run_seat`, and two spellings of "is this seat here" is exactly the disagreement
# that let a seat be skipped as absent while its budget record said it had been
# handed 116,287 chars. One predicate, in the module that owns `CLI_BIN`.


@dataclass(frozen=True)
class Ceiling:
    """One ceiling a round is under, CARRYING THE UNIT IT IS EXPRESSED IN.

    The unit is not decoration and it is not derivable from the number. A repo's
    `max_diff_chars` is a count of CHARACTERS; :data:`ARGV_PROMPT_MAX_BYTES` is the
    kernel's `MAX_ARG_STRLEN`, a count of BYTES. For an all-ASCII diff the two
    readings of a size are the same integer and nothing about which one is meant
    can be observed; for this repo's own diffs — em-dashes, arrows and box
    characters in every comment and every report — they differ by a factor that
    runs entirely in the direction of letting an over-cap round through.

    This type exists because that fact was carried OUT of BAND and rebuilt by
    every reader. `smallest_cap`, which :func:`seat_ceilings` and
    :func:`tightest_ceiling` replace, returned a bare `(int, seat)` and its callers
    worked the unit back out — `seat == "antigravity" and cap == ARGV_PROMPT_MAX_BYTES`
    in :func:`preflight`, and nothing at all in `refusal_report` or in the three
    `panel.py` banners, which printed "chars" over a ratio computed in bytes.
    Worse, the SELECTION itself compared the two: `min()` over a dict holding a
    character budget and a byte ceiling picked whichever was the smaller integer,
    which is a comparison of two different quantities and had no defined answer.

    So a ceiling is a value with a unit, every measurement is taken through it
    (:meth:`of` and :meth:`of_text`), and every renderer asks it what to print
    (:attr:`noun`, :attr:`adjective`). A reader who wants to check a multiple by
    hand is given the two numbers it was computed from, in the unit it was
    computed in.
    """

    #: The number, in :attr:`unit`.
    limit: int
    #: ``"chars"`` or ``"bytes"``. Only those two: they are the only two units any
    #: ceiling in this harness is expressed in, and a third would need a reading on
    #: :class:`DiffShape` to measure it against.
    unit: str
    #: The seat this ceiling belongs to. Named in the refusal's reason, so a reader
    #: can go and look at that seat's configuration.
    seat: str

    @property
    def noun(self) -> str:
        """``chars``/``bytes`` — what a quantity of diff is called in this unit."""
        return self.unit

    @property
    def adjective(self) -> str:
        """``char``/``byte`` — for `120,000-byte ceiling`."""
        return "byte" if self.unit == "bytes" else "char"

    def of(self, shape: DiffShape) -> int:
        """This diff's size READ IN THIS CEILING'S UNIT."""
        return shape.nbytes if self.unit == "bytes" else shape.chars

    def of_text(self, text: str) -> int:
        """The same reading of a string that is not the diff — the manifest, whose
        fit against this ceiling decides whether it can be substituted."""
        return len(text.encode()) if self.unit == "bytes" else len(text)

    def over(self, shape: DiffShape) -> float:
        """How many times over this ceiling the diff is, in this ceiling's unit.

        Guarded against a `limit` of 0 — `max_diff_chars: 0` is a value a repo can
        write, and a ZeroDivisionError raised from the middle of a verdict would
        take down a round over a config value that deserves a note at worst.
        """
        # `inf`, not 0.0, when the limit is zero (217-R3-F02). Guarding the
        # ZeroDivisionError with 0.0 substitutes the one answer that is wrong in the
        # most consequential direction: a diff of any nonzero size against a ceiling
        # of zero is INFINITELY over, and 0.0 reads as comfortably under — in the
        # function that decides whether to refuse the round. `inf` compares correctly
        # against every threshold, sorts as the tightest ceiling, and formats as
        # `inf` rather than lying.
        #
        # A zero-size diff against a zero ceiling is 0/0, which is not over
        # anything: there is nothing to send and nothing to cut.
        if not self.limit:
            return float("inf") if self.of(shape) else 0.0
        return self.of(shape) / self.limit


def seat_ceilings(budgets: dict[str, int | None], installed=None) -> tuple[Ceiling, ...]:
    """Every ceiling the seats that can actually RUN here are under, each with its
    unit. Empty when there is none.

    A seat's ceiling is its configured `max_diff_chars`, in characters. `antigravity`
    is additionally under the kernel's, in bytes: that seat's prompt travels in
    argv, so :data:`ARGV_PROMPT_MAX_BYTES` applies to it whether or not the repo set
    a number. Compared against the constant rather than against
    :func:`panel_seats.fit_argv_budget`'s exact answer, which is a few hundred
    bytes lower once the template is counted — a verdict about whether a diff is
    3x or 6x over does not turn on that, and depending on the render closure
    would make this callable only after the prompt exists.

    **Antigravity with a configured cap declares TWO ceilings, and that is the
    correction rather than an elaboration.** This used to collapse them with
    `min(cap, ARGV_PROMPT_MAX_BYTES)` — one number, chosen by comparing a character
    budget against a byte limit. A repo setting `antigravity.max_diff_chars:
    100_000` therefore hid the 120,000-BYTE argv ceiling behind the smaller
    integer, and a 100,000-character diff at two bytes per character sailed past a
    verdict that had measured 100,000 against 100,000 — into an `execve` that
    cannot carry 200,000 bytes. Two ceilings, both real, both evaluated: whichever
    binds first on THIS diff is the one that decides, and :func:`tightest_ceiling`
    is where that is worked out.

    **A seat whose CLI this box does not carry declares no ceiling here.** It is a
    fact about the HOST, not about the round — the same distinction `coverage_veto`
    makes at length, and for the same reason. `budgets` holds every CONFIGURED and
    selected seat, not every runnable one, and `agy` is a workstation package: on a
    headless box this repo's own rules enable a seat that records "antigravity: CLI
    absent" and never runs. Counting its argv ceiling would refuse a round on
    behalf of a reviewer that was never going to read anything, on exactly the
    unattended hosts where nobody is watching to pass `--force` — while the seats
    that DID run were reading off stdin with no cap at all.

    `installed` is injected so this stays a pure function of its arguments: a
    verdict that consults PATH is a verdict whose tests pass or fail depending on
    which vendor CLIs the machine happens to carry. It defaults to `None` and is
    resolved to :func:`seat_installed` in the BODY, never as a default argument
    value — a default binds the function object at `def` time, so
    `monkeypatch.setattr(panel_preflight, "seat_installed", ...)` would have no
    effect and every end-to-end test here would silently go on reading the real
    PATH. Verified by running the suite with the vendor CLIs hidden: ten tests that
    passed locally failed, which is what a CI runner would have found.

    **The judge's own budget is deliberately not counted, and the exclusion is
    worth stating because the refusal payload records `diff_budgets` WITH a
    `judge` entry and so invites the opposite reading.** `budgets` is the seats,
    and the two things this verdict decides are both about seats: whether to
    dispatch them, and whether to hand them a manifest instead of a diff. A repo
    that sets a tight `judge_max_diff_chars` has said something about what
    adjudication is worth, not about whether a round can be read — and counting it
    here would let that knob refuse a round every reviewer could read whole, which
    is a power nobody granted it. The judge is not left unprotected by the
    omission: a manifest substitution replaces the round's MATERIAL, so
    `judge_material` composes the manifest too, and a judge cut by its own budget
    is reported as truncation exactly as a seat is.

    The EMPTY tuple when no runnable seat declares a cap, and that answer is
    load-bearing: it is what keeps this file from becoming the default diff budget
    #49 refused. A repo running claude and codex off stdin with no `max_diff_chars`
    has declared no ceiling, so there is no size for a refusal to be measured
    against and the round proceeds exactly as it always has.
    """
    here = installed or seat_installed
    out: list[Ceiling] = []
    for name, budget in budgets.items():
        if not here(name):
            continue
        # `> 0`, not merely `is not None` (217-R3-F02). `diff_budget` already
        # refuses a non-positive value and falls back with a note, so a zero cannot
        # reach here from config today — this is latent rather than live, and it is
        # filtered anyway because `seat_ceilings` is also called with hand-built
        # budgets (its own tests, and whatever calls it next). A ceiling of zero
        # admits no diff at all, which is not a ceiling; it is a seat that cannot be
        # sent anything, and `diff_budget` is the layer that says so.
        if budget is not None and budget > 0:
            out.append(Ceiling(budget, "chars", name))
        if name == "antigravity":
            out.append(Ceiling(ARGV_PROMPT_MAX_BYTES, "bytes", name))
    return tuple(out)


def tightest_ceiling(budgets: dict[str, int | None], shape: DiffShape,
                     installed=None) -> Ceiling | None:
    """Which of :func:`seat_ceilings`' ceilings binds FIRST on this diff, or None.

    **Tightest means the largest RATIO, not the smallest number, and the
    distinction is the whole point of this function.** The predecessor took
    `min()` across every seat's cap and returned the smallest integer — with a
    character budget and a byte limit in the same dict. That comparison has no
    answer: 100,000 characters and 120,000 bytes are not two sizes of the same
    thing, and which of them a diff crosses first depends entirely on how much of
    the diff is not ASCII. On this repo's own diffs a 100,000-character budget won
    the `min()`, the round was then measured in characters, and antigravity's
    120,000-byte ceiling — genuinely tighter at ~60,000 characters for a
    two-bytes-per-character diff — went unevaluated. The verdict was computed
    against the looser real ceiling and `over` understated it, which is the exact
    error class :attr:`DiffShape.nbytes` was added to remove.

    So each ceiling is measured against the diff IN ITS OWN UNIT and the ratios are
    compared. Ratios are dimensionless, so this is the one comparison that is
    defined across units, and it is the one the verdict actually needs: "how far
    past this ceiling is this diff" is the question both the refusal multiple and
    the manifest substitution are asked.

    Ties are broken by the smaller `limit` and then by seat name. That secondary
    key compares numbers across units, which the primary key exists to stop doing —
    it is allowed here and only here because a tie in the RATIO means both ceilings
    return the same verdict, the same multiple and the same fit, so this chooses
    which ceiling is NAMED and can never change what is decided. Determinism is why
    it is broken at all: the seat's name goes in the refusal's reason, and a reason
    that names a different seat on two runs of the same round is a reason nobody can
    check. The empty diff is the case that makes the tie ordinary rather than
    exotic — every ratio is 0.0 — and there the smaller number is also the answer
    the predecessor gave.
    """
    ranked = seat_ceilings(budgets, installed)
    if not ranked:
        return None
    return min(ranked, key=lambda c: (-c.over(shape), c.limit, c.seat))


#: The spellings :func:`_flag` accepts for a hand-written boolean. JSON's own two
#: are handled before these are reached; these are what somebody types when the
#: file is edited rather than generated.
_TRUE_WORDS = frozenset({"true", "yes", "on", "1"})
_FALSE_WORDS = frozenset({"false", "no", "off", "0"})


def _rule(panel: dict, key: str, fallback, notes: list[str], high: float | None = None,
          off: str = ""):
    """One numeric pre-flight setting, with the same manners
    :func:`panel_seats.diff_budget` gives a diff budget: unset is silent, and a
    value that cannot be the thing at all falls back and SAYS so.

    **Absent and null both mean "use the default", and neither means "off".** The
    only spelling of "off" either caller reads is the number `0`, and this used to
    be documented the other way round in two places — the `harness_rules.py`
    comment and :data:`DEFAULT_REFUSE_OVER_CAP_MULTIPLE`'s own docstring both said
    `null` disabled the refusal, while `loops/README.md` said `0` did. An operator
    following the wrong pair wrote `refuse_over_cap_multiple: null`, got the
    default 3 back, and had rounds refused with nothing in `config_notes` to
    explain it, because null is — correctly — the silent case. The docs are what
    moved: `null` cannot mean "off" while it also means "inherit", and every other
    setting in this harness reads it as "inherit".

    `false`, the other way an operator writes "off", is rejected as a non-number
    rather than reinterpreted. It is tempting to read it as 0 and it would be
    wrong: `move_shape_ratio: false` would then mean a threshold of 0, at which
    every diff with one relocated line is a move — the switch flipped to "off"
    turning the feature all the way on.

    **`off` is what the note tells that operator to write instead, and it is a
    per-key argument because the two keys have different answers.** It used to be
    the literal string "(write `0` to switch it off)", emitted for every key — so
    `move_shape_ratio: false` was answered with advice to write the one value the
    paragraph above says is the trap: an operator who complied got a threshold of
    0, at which every over-ceiling diff with a single relocated line is a move and
    is handed a manifest. `0` is the off switch for `refuse_over_cap_multiple` and
    for nothing else. `move_shape_ratio` has no off at all — it is a fraction, and
    the switch an operator reaching for one wants is `manifest_moves: false` — so
    its note says that instead. Omitted, the note simply names the default it fell
    back to, which is the right answer for a key where "off" is not a thing.

    `high`, where given, is the largest value the setting can mean. Only
    `move_shape_ratio` has one: it is relocated lines as a fraction of the larger
    side, so 1.0 is "a move with no residue at all" and there is nothing above it
    to express. `move_shape_ratio: 90` (meant as 90%) otherwise passes validation,
    makes :meth:`DiffShape.is_move` unsatisfiable, and turns every over-cap round
    into a refusal whose reason reads "… under the 90 move ratio" — a plausible
    sentence about a threshold that cannot be met.

    Non-finite values are rejected for the same reason with none of the plausible
    sentence: `nan` compares false against everything, so `move_shape_ratio: nan`
    silently means "nothing is ever a move" and `refuse_over_cap_multiple: inf`
    silently means "never refuse". Both are the feature switched off by a value
    that reads like a number, and the negative check below already establishes that
    a value which cannot be the thing at all is reported rather than honoured.
    """
    raw = panel.get(key)
    if raw is None or raw == "":
        return fallback
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        # Bools first: `isinstance(True, int)` is True, so without this the
        # `float()` below would quietly turn `false` into the threshold 0.
        extra = f" ({off})" if raw is False and off else ""
        notes.append(f"`{key}`={raw!r} is not a number — using {fallback}{extra}")
        return fallback
    try:
        # float() throughout, never `type(fallback)(raw)`. That coerced through the
        # DEFAULT's type, so `refuse_over_cap_multiple: 2.9` — whose default is the
        # int 3 — became 2 and refused a third earlier than the number written in
        # the file. A threshold is a real number; both of these are compared and
        # formatted (`:g`) and neither needs to be an int.
        n = float(raw)
    except (TypeError, ValueError):
        notes.append(f"`{key}`={raw!r} is not a number — using {fallback}")
        return fallback
    if n != n or n in (float("inf"), float("-inf")):
        notes.append(f"`{key}`={raw!r} is not a finite number — using {fallback}")
        return fallback
    if n < 0:
        notes.append(f"`{key}`={raw!r} cannot be negative — using {fallback}")
        return fallback
    if high is not None and n > high:
        notes.append(f"`{key}`={raw!r} cannot be above {high:g} — using {fallback}")
        return fallback
    return n


def _flag(panel: dict, key: str, fallback: bool, notes: list[str]) -> bool:
    """One boolean pre-flight setting, with :func:`_rule`'s manners.

    `manifest_moves` was read as `panel.get("manifest_moves", True)` — raw Python
    truthiness — while both of the numeric settings introduced beside it went
    through `_rule` on purpose, so that "a junk threshold in `.harness-rules` is
    reported the way every other bad config value is". The gap mattered in the one
    direction that cannot be noticed: `manifest_moves: "false"` is a non-empty
    string, so the feature an operator had just written "false" against stayed ON,
    and `thresholds["manifest_moves"]` then reported `bool(raw)` as though the
    value had been validated.

    The string spellings are accepted rather than rejected because this key is
    written by hand and `"false"` is what a hand writes. Anything else — a list, a
    word that is not a boolean, a number that is not 0 or 1 — falls back and says
    so, which is the half that makes the accepted spellings safe: the reader of a
    note knows the value did not apply, and silence means it did.

    **The bare numbers 0 and 1 are accepted too, and leaving them out was a split
    nothing could justify.** `_FALSE_WORDS` already contains the STRING `"0"`, so
    `manifest_moves: "0"` switched the manifest off while `manifest_moves: 0` — the
    natural spelling in a JSON `.harness-rules`, where an unquoted number needs no
    quoting decision — fell through to "is not true or false" and left it ON. And
    `0` is the documented, only spelling of off for `refuse_over_cap_multiple`
    sitting in the same block, so an operator writing
    `{"manifest_moves": 0, "refuse_over_cap_multiple": 0}` had every reason to
    expect both switched off and got one. Of the three consistent answers —
    accept both spellings, reject both, or the split that shipped — the split was
    the only one no reader could predict.

    `isinstance(raw, bool)` is tested first because `isinstance(True, int)` is
    True, and a float `0.0`/`1.0` is admitted with the ints: it is the same value
    written by a generator that had a float in hand, and `2` (or `0.5`) still falls
    back and says so.
    """
    raw = panel.get(key)
    if raw is None or raw == "":
        return fallback
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str) and raw.strip().lower() in _TRUE_WORDS | _FALSE_WORDS:
        return raw.strip().lower() in _TRUE_WORDS
    notes.append(f"`{key}`={raw!r} is not true or false — using {fallback}")
    return fallback


# ----------------------------------------------------------------------------- verdict

@dataclass(frozen=True)
class Preflight:
    """The decision, and everything a reader needs to check it.

    `reason` is prose and is the part that has to survive: a refusal whose reason
    is a code is a refusal nobody argues with, and the whole complaint behind
    this file is that an advisory a human has to notice is what failed.
    """

    verdict: str
    reason: str
    shape: DiffShape
    #: The ceiling that bound first on this diff, WITH ITS UNIT; None when no
    #: runnable seat declares one, in which case nothing here ever fires. Carried
    #: whole rather than as a bare number and a seat name, because every renderer
    #: below and in `panel.py` has to say what it measured and the unit is not
    #: recoverable from the number — see :class:`Ceiling`. `cap`, `cap_seat`,
    #: `cap_unit` and `measured` read off it, so the four cannot drift apart.
    ceiling: Ceiling | None = None
    #: How many times over the ceiling the diff is, in that ceiling's unit. 0.0
    #: with no ceiling — and `as_dict` emits it as null only in that case, never
    #: for a measured ratio that happens to round to 0.00.
    over: float = 0.0
    #: Did `--force` overrule a refusal or a manifest? Recorded rather than
    #: inferred from `verdict == "run"`, because "the tool chose to run" and "a
    #: caller overrode the tool" are the two things this repo's standing rule
    #: says must never look alike.
    forced: bool = False
    #: What the verdict WOULD have been without --force.
    would_have: str = ""
    #: The PRECONDITION that refused this round, when a precondition did (#271) —
    #: today only "the branch cannot merge". Empty on every size-driven verdict.
    #:
    #: It exists because a gate refusal and a size refusal are the same `refuse`
    #: verdict with the same `reason` field and want different prose everywhere
    #: they are rendered: `refusal_report`'s measurement table and its three
    #: remedies are all about a diff that is too big, and printed over a branch
    #: that simply cannot merge they tell an operator to split a PR whose size was
    #: never the problem. `reason` says WHAT; this says which QUESTION was asked,
    #: which is the part a renderer has to branch on.
    gate: str = ""
    thresholds: dict = field(default_factory=dict)
    #: The manifest, on a ``manifest`` verdict and nowhere else. Carried rather
    #: than rebuilt by the caller because the verdict is made by MEASURING it —
    #: a manifest is only worth substituting if it is smaller than the diff it
    #: replaces — and building it twice is two texts that have to agree about
    #: which one was weighed.
    manifest: str = ""

    @property
    def refused(self) -> bool:
        return self.verdict == "refuse"

    # The ceiling, spelled the four ways its readers need it. Properties rather
    # than fields, so there is exactly one place the unit and the number can come
    # from: the previous shape of this class stored `cap` and `cap_seat` as
    # separate fields with the unit nowhere at all, and every renderer that wanted
    # it either re-derived it (`seat == "antigravity" and cap == ARGV_…`) or, more
    # often, assumed characters and printed a byte ratio under a "chars" label.
    @property
    def cap(self) -> int | None:
        return self.ceiling.limit if self.ceiling else None

    @property
    def cap_seat(self) -> str:
        return self.ceiling.seat if self.ceiling else ""

    @property
    def cap_unit(self) -> str:
        """``chars``/``bytes`` — the unit `cap`, `over` and `measured` are all in.
        ``chars`` with no ceiling, because nothing was measured and a caller
        formatting the shape's own size has the character reading in hand."""
        return self.ceiling.noun if self.ceiling else "chars"

    @property
    def cap_unit_adj(self) -> str:
        """``char``/``byte``, for `a 120,000-byte ceiling`."""
        return self.ceiling.adjective if self.ceiling else "char"

    @property
    def measured(self) -> int:
        """The diff's size AS THE VERDICT READ IT — `shape.nbytes` under a byte
        ceiling, `shape.chars` otherwise. This is the numerator of `over`, so it is
        the number a reader divides by `cap` to check the multiple, and printing
        `shape.chars` in its place is what made a refusal notice say "100,000 chars
        … exceeded 2.2x" against a 120,000 ceiling."""
        return self.ceiling.of(self.shape) if self.ceiling else self.shape.chars

    def as_dict(self) -> dict:
        # `round(self.over, 2) or None` collapsed a measured 0.00 into the same
        # null that means "no ceiling was declared" — reachable on any small diff
        # against a large ceiling (200 chars against 120,000 rounds to 0.0). The
        # `preflight` block's own documentation makes that exact distinction
        # load-bearing one level up ("null means the run never reached the
        # verdict … which is a different statement"), and reusing null for
        # "measured, and small" inside it undercuts the same argument. The
        # CEILING is what decides whether there was a measurement, so the ceiling
        # is what is asked — `if self.cap` would have made a configured
        # `max_diff_chars: 0` indistinguishable from no ceiling at all, which is
        # the same collapse one value along.
        #
        # `cap_unit` rides beside `cap`, and it is not optional: `cap` and
        # `over_cap` are a number and a ratio whose unit is a property of which
        # seat won, and a consumer holding `shape.chars`, `shape.bytes` and a
        # ratio it cannot attribute to either cannot check the verdict at all.
        # That is the whole claim `shape` carrying both readings was added to
        # make good on.
        return {"verdict": self.verdict, "reason": self.reason or None,
                "cap": self.cap, "cap_seat": self.cap_seat or None,
                "cap_unit": self.cap_unit if self.ceiling else None,
                "over_cap": round(self.over, 2) if self.ceiling else None,
                "forced": self.forced, "would_have": self.would_have or None,
                "thresholds": self.thresholds, "shape": self.shape.as_dict()}


def preflight(diff: str, budgets: dict[str, int | None], panel: dict,
              notes: list[str], forced: bool = False,
              installed=None, gate: str = "", gate_overridable: bool = True) -> Preflight:
    """Rule on a round before it is dispatched.

    `gate` is a PRECONDITION that has already failed — a sentence saying why this
    round should not happen at all, decided by the caller before any of the sizes
    below are looked at (#271). Handed one, this refuses on it and says so, and
    `--force` overrides it through exactly the same machinery that overrides a
    size refusal — **unless the caller says otherwise**.

    `gate_overridable=False` is #55's spend ceiling and is the one refusal on this
    path that `--force` may not turn into a run. Everything else here is a
    judgement about what THIS host's seats can usefully read, which is exactly the
    kind of judgement an operator standing in front of it may overrule; a ceiling
    is fleet policy set by a person on the board, and a local flag that switched it
    off would make it advice again — which is the state #55 exists to end. The
    refusal is still recorded and posted through the identical machinery, so a
    reader cannot tell the two apart by how loudly they arrive, only by whether
    `--force` moved them.

    It arrives as a parameter rather than being asked here because
    the question is not about the diff: `panel.run` reads the PR's mergeability off
    metadata it has already fetched, and the ONLY reason to route the answer
    through this function is that everything downstream of a refusal — the payload,
    `skip_reason`, the per-seat `ran: false` rows, the board record, `--force` —
    already exists here and must not be built a second time.

    The order of the tests is the argument. Fitting the cap settles it — a diff
    every seat can read is a diff to read, whatever its shape, because reading a
    small move as content costs nothing and tells you strictly more than a
    manifest of it does. Over the cap, SHAPE decides: a move gets the manifest at
    any multiple, because no budget makes relocated text reviewable and there is
    a better question to ask instead. Only a diff that is over the cap by the
    multiple AND has no smaller honest question behind it is refused.

    **"A smaller honest question" means a manifest that exists and fits, and
    neither is automatic.** `manifest_moves` can be off, and a manifest can come
    out no smaller than the diff (its body scales with the change's shape, its
    brief and headers are a fixed kilobyte) or smaller than the diff and still over
    the ceiling — in which case substituting it would hand a seat a PREFIX of a
    manifest and report the round as `manifest` with `diff_truncated: true`, the
    confusing pair the substitution exists to avoid. Each of those is measured, not
    assumed, because "the manifest is always smaller" is exactly the kind of claim
    that is true of every case anyone tested.

    That leaves two ways to reach ``run`` over the cap: "under the multiple, not a
    move", which is today's behaviour and #75's truncation report and is unchanged
    on purpose; and "under the multiple, move-shaped, but no manifest to put in its
    place". The second one used to return an EMPTY reason and throw away the one
    fact a reader of `preflight.reason` most needs in exactly that case — that the
    manifest path was reached and did not help. It now carries it. Both still run,
    because the multiple declining to fire is the operator saying truncation is
    acceptable at this size, and a refusal invented above that would be this module
    deciding a budget question it has no business deciding.

    Sizes are measured in the ceiling's OWN unit, and the ceiling that binds is
    chosen by comparing those measurements rather than the ceilings themselves. A
    configured `max_diff_chars` is characters; the kernel's argv limit is bytes;
    they are the same number only for an all-ASCII diff. See :class:`Ceiling` and
    :func:`tightest_ceiling`.
    """
    parts = _hunk_bodies(diff)
    shape = _shape_of(parts, diff)
    ratio = _rule(panel, "move_shape_ratio", DEFAULT_MOVE_SHAPE_RATIO, notes,
                  high=1.0, off="`manifest_moves: false` is the switch; this key is "
                                "a threshold and has no off")
    multiple = _rule(panel, "refuse_over_cap_multiple",
                     DEFAULT_REFUSE_OVER_CAP_MULTIPLE, notes,
                     off="write `0` to switch it off")
    manifest_on = _flag(panel, "manifest_moves", True, notes)
    thresholds = {"move_shape_ratio": ratio, "refuse_over_cap_multiple": multiple,
                  "manifest_moves": manifest_on}
    # The ceiling that binds first on THIS diff, carrying the unit it is expressed
    # in. Nothing below asks what unit that is: `ceiling.of()` reads the shape,
    # `ceiling.of_text()` reads the manifest, and `ceiling.noun`/`.adjective` write
    # the words. This used to be a bare `(int, seat)` and a `by_bytes` flag
    # reconstructed here by equality against ARGV_PROMPT_MAX_BYTES — a derivation
    # only this function made, which is why every other renderer of the verdict
    # went on printing "chars" over a ratio that was sometimes bytes.
    # The whole set, not only the tightest: the manifest branch has to re-rank
    # against its OWN text, because the seat that binds for the diff need not be the
    # seat that binds for a manifest of different size and density (217-R3-F01).
    ceilings = seat_ceilings(budgets, installed)
    ceiling = tightest_ceiling(budgets, shape, installed)
    cap = ceiling.limit if ceiling else None
    seat = ceiling.seat if ceiling else ""
    size = ceiling.of(shape) if ceiling else shape.chars
    unit = ceiling.noun if ceiling else "chars"
    unit_adj = ceiling.adjective if ceiling else "char"
    over = ceiling.over(shape) if ceiling else 0.0

    def verdict(name: str, reason: str, manifest: str = "",
                forced_reason: str = "") -> Preflight:
        # --force is applied HERE rather than by the caller, so the payload
        # carries what the tool decided AND what the caller did about it. A flag
        # that erases the verdict it overrode leaves no evidence the tool was
        # ever asked.
        #
        # `forced_reason` is not decoration. --force does NOT re-run the verdict:
        # it turns a non-run verdict into `run`, and the caller then reviews the
        # full diff as content, because `pre.verdict == "manifest"` is what
        # triggers the substitution and it is no longer true. Carried through
        # verbatim, a forced manifest therefore recorded "Reviewed as a MANIFEST
        # instead — what moved where …" on a round that reviewed the diff as
        # content: `preflight.reason` asserting the opposite of what happened, in
        # the field a reader six weeks later has instead of the round. So every
        # non-run verdict states its diagnosis and its OUTCOME separately, and the
        # forced form keeps the diagnosis and replaces the outcome.
        if name != "run" and forced:
            return Preflight("run", f"--force: {forced_reason or reason}", shape,
                             ceiling, over, forced=True, would_have=name,
                             gate=gate, thresholds=thresholds)
        return Preflight(name, reason, shape, ceiling, over,
                         gate=gate, thresholds=thresholds, manifest=manifest)

    # BEFORE any size question, because a precondition is not a budget: a branch
    # that cannot merge is not reviewable at any ceiling, and refusing it for its
    # size instead would print remedies ("split the PR") about the wrong problem.
    # After the measurement above, though, and deliberately — `preflight.shape` is
    # what a reader has instead of the round, and a gate refusal that recorded
    # nothing about the diff it declined would be the silent target #241 is about.
    if gate and not gate_overridable:
        # Built without `verdict()`, which is where --force is applied. Spelled as
        # its own return rather than as a flag threaded through that closure
        # because there is exactly one caller and the alternative is a `forced`
        # parameter that sometimes means forced — see the docstring.
        return Preflight("refuse", gate, shape, ceiling, over,
                         gate=gate, thresholds=thresholds)
    if gate:
        return verdict("refuse", gate)
    if ceiling is None:
        return verdict("run", "")
    if size <= cap:
        return verdict("run", "")

    moved_pct = f"{shape.move_ratio * 100:.1f}%"
    shape_said = (f"{shape.moved:,} of {max(shape.added, shape.removed):,} changed "
                  f"lines ({moved_pct}) appear on both sides of the diff")
    over_said = (f"{size:,} {unit} of diff against {seat}'s "
                 f"{cap:,}-{unit_adj} ceiling ({over:.1f}x)")
    is_move = shape.is_move(ratio)

    # Why no manifest was substituted, when the diff was move-shaped and one still
    # was not. Empty when the diff is not move-shaped at all, which is the case the
    # refusal has its own sentence for.
    tried_manifest = ""
    if is_move and not manifest_on:
        # The refusal reason used to fall through to "it is not move-shaped … under
        # the N move ratio" here, because `tried_manifest` was only ever set inside
        # the `manifest_on` branch — a refusal contradicting the measurement it was
        # made from, on a repo that had merely switched the manifest off.
        tried_manifest = (f"; it IS move-shaped ({shape_said}), but `manifest_moves` "
                          "is off for this repo, so there is no manifest to offer "
                          "instead")
    elif is_move:
        # Built even under --force, and that is not waste. Which of the three
        # branches below a forced round was in is what decides its `would_have` and
        # which diagnosis it records — "the MANIFEST that would have been sent was
        # overruled" against "a manifest of it came to N and so would replace the
        # problem with a copy of it" — and there is no way to know that without
        # measuring the manifest. An early bail on `forced` would buy one pass over
        # the diff and pay for it by recording the wrong reason, which is the exact
        # trade `forced_reason` exists to refuse.
        text = move_manifest(diff, shape, parts)
        # Measured against EVERY ceiling, not just the one that was tightest for the
        # diff (217-R3-F01). `tightest_ceiling` ranks by ratio against the DIFF's own
        # char/byte density, and the manifest has neither the same density nor the
        # same absolute size — so the seat that binds for the diff need not be the
        # seat that binds for the manifest. With `{claude: 60,000 chars,
        # antigravity: 120,000 bytes}` the ranking can flip between the two texts,
        # and checking only the diff's winner would substitute a manifest that does
        # not fit the seat which is actually tightest for it: a seat reading a prefix
        # of a manifest, which is the confusion this branch exists to avoid.
        #
        # `worst` is the ceiling the MANIFEST is furthest over, by the same
        # ratio-across-units comparison; `ceiling` stays the diff's for every
        # sentence below, because those explain why the DIFF did not fit.
        worst = max(ceilings, key=lambda c: c.of_text(text) / c.limit)
        fitted = worst.of_text(text)
        cap, unit = worst.limit, worst.unit
        # `<= cap`, matching the `size <= cap` the diff itself is admitted by a few
        # lines up. This was `< min(cap, size)`, which rejected a manifest whose
        # length was EXACTLY the ceiling and then explained itself with the "still
        # over the ceiling" sentence below — a claim that is false at the boundary:
        # a manifest of exactly `cap` fits, is not truncated, and is precisely the
        # substitution this branch exists to make. Two comparisons of the same
        # quantity against the same ceiling have to agree about the boundary, and
        # this is the one that was out of step.
        if fitted <= cap and fitted < size:
            return verdict(
                "manifest",
                f"this change is move-shaped — {shape_said}, so they "
                f"were relocated rather than written. {over_said}, and no "
                "budget makes relocated text reviewable as content: a seat "
                "would spend the round re-reading code that is already in the base "
                "branch and report findings about it. Reviewed as a MANIFEST "
                "instead — what moved where, what did not survive, and what "
                "changed besides moving",
                text,
                forced_reason=(
                    f"this change is move-shaped — {shape_said}, so they were "
                    f"relocated rather than written. {over_said}, and no budget "
                    "makes relocated text reviewable as content. The MANIFEST that "
                    "would have been sent instead was overruled: the full diff was "
                    "reviewed as content, which is the re-reading just described"))
        if fitted >= size:
            # Not a saving, a second copy of the problem — and then a truncated
            # one, so the seat reads a prefix of a manifest rather than a prefix of
            # a diff. Reachable on any small move just over a small ceiling.
            tried_manifest = (f"; it IS move-shaped ({shape_said}), but a manifest of "
                              f"it came to {fitted:,} {unit} against the diff's "
                              f"{size:,} and so would replace the problem with a "
                              "copy of it")
        else:
            # Smaller than the diff and still over the ceiling. `len(text) <
            # shape.chars` alone let this through, and the round then reported a
            # clean `manifest` verdict beside `diff_truncated: true` — a seat
            # reading a PREFIX of a manifest, which is the one outcome the
            # substitution was built to prevent. Reachable with the shipped
            # constants: 200 table rows plus 240 quoted residue lines at up to 120
            # chars is ~35 KB of manifest against a low-thousands ceiling.
            tried_manifest = (f"; it IS move-shaped ({shape_said}), and a manifest of "
                              f"it came to {fitted:,} {unit} — smaller than the diff, "
                              f"but still over the {cap:,}-{unit_adj} ceiling, so a "
                              "seat would read a prefix of a manifest and the round "
                              "would report having read one whole")
    if multiple and over > multiple:
        why_not_a_move = tried_manifest or (
            f", and it is not move-shaped ({shape_said} — under the "
            f"{ratio:g} move ratio), so there is no smaller honest question to ask "
            "instead")
        return verdict(
            "refuse",
            f"{over_said}, past the {multiple:g}x refusal threshold"
            f"{why_not_a_move}. A truncated read that produces "
            "findings is worse than no review: the next step of the cycle "
            "briefs a fixer to resolve every one of them. Split the PR, "
            "raise the cap for a seat that can take it, or pass --force",
            forced_reason=(
                f"{over_said}, past the {multiple:g}x refusal threshold"
                f"{why_not_a_move}. The refusal was overruled: the diff was "
                "reviewed as content anyway, so most of each seat's budget went "
                "somewhere and it was not necessarily where the change is"))
    if tried_manifest:
        why_ran = (f"Under the {multiple:g}x refusal threshold" if multiple else
                   "With the refusal switched off (`refuse_over_cap_multiple: 0`)")
        return verdict("run", f"{over_said}{tried_manifest}. {why_ran}, the round runs "
                              "as an ordinary truncated content review — read the "
                              "truncation report beside its findings, and read them "
                              "knowing most of what each seat saw was relocated text")
    return verdict("run", "")


# ----------------------------------------------------------------------------- manifest

def duplicate_definitions(def_sites: dict[str, Counter]) -> dict[str, Counter]:
    """Definitions the change ADDS in more than one place, name -> where.

    #62's trap, and the reason it is worth a section of its own: a merge that
    keeps both copies of a moved function is a clean merge, a green test run and
    a silent bug, because Python binds the second definition and the first is
    dead. A move is precisely the change that produces it.

    **Only the half of that trap a diff can show, and the manifest says so.** What
    is detectable here is a definition the change ADDS in two or more places. The
    canonical merge accident — the original left where it always was, in a file the
    merge never touched, and a copy arriving somewhere new — puts no `+` and no `-`
    line in the diff for the original at all: it is not in the diff, it is in the
    base branch, and no amount of parsing recovers it from `gh pr diff`. Finding it
    needs the PR head checked out, which is the one thing this module states
    everywhere that it does not have. So the claim was narrowed to what is checked
    rather than the check widened to a promise it cannot keep, and the unseeable
    half is named in the manifest's "WHAT IS NOT HERE" section beside the two other
    facts a checkout would be needed for. A section that reads "checked, and clean"
    over a case it structurally cannot see is worse than no section.

    The values are WHERE each copy landed — file to how many times — which is the
    one thing a reader can act on without the checkout. They used to be `[]` for
    every name, under a `dict[str, list[str]]` annotation promising locations that
    the only caller never read.

    A `Counter` per name rather than a list of rendered strings, so what comes back
    is data: `{"review_llm": {"a.py": 1, "b.py": 2}}`. A list of `"b.py x2"` would
    be the same misleading annotation one step along — values a reader would take
    for paths and that are not paths. The rendering belongs to
    :func:`move_manifest`, which is the only thing that has an opinion about how a
    duplicate reads.

    **And "more than once" is not the same as "wrong", which is why the manifest
    names the ordinary reasons beside the limits.** The test is
    `sum(files.values()) > 1` over a name, so a `@typing.overload` stub chain, an
    `if TYPE_CHECKING:`/`else:` pair and a platform-conditional `def` pair are each
    reported — all three define one name several times in ONE file, on purpose.
    Widening the check to tell them apart means reading the enclosing block, which
    is a parser rather than a per-line pattern and would have every one of the
    ambiguities `_DEF_PATTERNS` refuses to guess at. So they stay reported and the
    section says what they are: a reviewer who is told the shape spends a second on
    it, and one who is not learns to distrust the whole section on its first
    `@overload`.

    Takes :attr:`DiffParts.def_sites` rather than the diff or the added multiset:
    `added` is keyed by BODY and has no idea which file a body came from, which is
    exactly why the locations could not be filled in before.
    """
    return {name: files for name, files in sorted(def_sites.items())
            if sum(files.values()) > 1}


def _quote(body: str) -> str:
    return _ellipsis(body.rstrip(), MANIFEST_LINE_CHARS)


def _ellipsis(text: str, width: int) -> str:
    """`text` cut to `width`, and SAYING it was cut.

    One helper rather than a slice at each site, because an unmarked cut is a
    quotation a reader believes: a 60-character PR title and one chopped
    mid-word render identically, and the second one goes on the PR under a
    refusal notice somebody has to act on. Every other cut in this harness marks
    itself (`fit_comment`, `_cut_note`), and this is the same rule.
    """
    return text if len(text) <= width else text[:width] + " …"


def _listing(bodies: Counter, cap: int) -> tuple[list[str], int]:
    """``(lines, elided)`` — up to `cap` DISTINCT quoted lines, longest first,
    each with an ``xN`` where the change made it more than once.

    Longest first because the residue of a move is where the real change is, and
    a one-token line is the least likely of them to be it. Sorted by length then
    text, so the same diff produces the same manifest twice — a listing that
    reorders between two runs of the same round is a diff nobody can compare.

    Distinct, because this used to expand `Counter.elements()`: one boilerplate
    residue line — a repeated `raise NotImplementedError(...)`, a repeated log
    call — added five hundred times was quoted up to `cap` times, and since the
    sort is longest-first a single long repeated line crowded out every unique
    line behind it. The elision count then reported "… and N more, not listed"
    for exactly the lines a reviewer needed to see. The repetition is information
    and is kept as a multiplier; what it must not be is the whole budget.
    """
    ordered = sorted(bodies, key=lambda b: (-len(b.strip()), b))
    shown = [_quote(b) + ("" if bodies[b] == 1 else f"   (x{bodies[b]:,})")
             for b in ordered[:cap]]
    return shown, max(0, len(ordered) - cap)


def _residue_count(bodies: Counter) -> str:
    """``N line(s)`` — and ``, D distinct`` when the two differ.

    The section headers count OCCURRENCES and everything under them counts
    DISTINCT lines: :func:`_listing` iterates distinct bodies, carries the
    repetition as an `xN` multiplier, and elides against the distinct total. So a
    residue of 500 copies of one line plus 5 unique ones rendered as "505 line(s)"
    followed by six quoted entries and no "and N more" note — two numbers in
    different units in adjacent lines, with nothing saying which was which. The
    multiplier makes it reconstructable and that is not the same as stating it.

    Both numbers only when they differ, because on the ordinary residue — every
    line unique — "12 line(s), 12 distinct" is a second number that says nothing
    and one more thing between the reader and the listing.
    """
    total, distinct = sum(bodies.values()), len(bodies)
    return (f"{total:,} line(s)" if total == distinct else
            f"{total:,} line(s), {distinct:,} distinct")


def move_manifest(diff: str, shape: DiffShape | None = None,
                  parts: DiffParts | None = None) -> str:
    """The prompt body for a move-shaped change: the evidence that bears on it.

    For a move, the mechanical facts are not a supplement to a content review —
    they are the only evidence there is. "Did this lose anything" is answerable
    from the diff alone and a seat reading 16% of it cannot answer it at all.

    What is here is what the DIFF can prove. PR #137 was also judged on identical
    test counts before and after and an AST closure check showing no module
    reaches backward into another; neither is here, and the reason is not that
    they do not matter. Both need the PR head checked out, and the panel reviews a
    PR it has never checked out — it has `gh pr diff` and nothing else. Claiming
    them from a diff would be inventing the two facts a reader would most want to
    rely on, so the brief asks the reviewer to say they are missing instead.

    `shape` and `parts` are accepted so the verdict that decided to substitute this
    manifest can hand over the parse it already made — see :class:`DiffParts`. Both
    stay optional because a caller holding only a diff (every test of this function,
    and anyone rendering a manifest by hand) should not have to know about either.
    """
    parts = _hunk_bodies(diff) if parts is None else parts
    shape = shape or _shape_of(parts, diff)
    added, removed, per_file = parts.added, parts.removed, parts.per_file
    survived = added & removed
    lost = removed - survived        # deleted, and not re-added anywhere
    gained = added - survived        # added, and not deleted anywhere
    dupes = duplicate_definitions(parts.def_sites)

    out: list[str] = [
        f"This change is move-shaped: {shape.moved:,} of "
        f"{max(shape.added, shape.removed):,} changed lines "
        f"({shape.move_ratio * 100:.1f}%) appear on BOTH sides of the diff — "
        f"deleted in one place and added in another, character for character. "
        f"The diff is {shape.chars:,} chars across {shape.files:,} file(s); most of "
        "it is text nobody wrote. You are NOT being given the diff, because "
        "re-reading relocated code produces findings about the base branch.",
        "",
        "WHAT MOVED WHERE",
    ]
    # Biggest movers first: the source of a split and its destinations are the
    # rows that say what the change IS, and they are the rows with the counts.
    rows = sorted(per_file.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))
    for path, (a, r) in rows[:MANIFEST_TABLE_ROWS]:
        # Four labels, not three. `a == r == 0` is the mode change, the binary and
        # the rename git recorded without content — the case `diff_shape` reasons
        # about explicitly as being NEITHER one-sided nor the other — and it used
        # to fall through to "both", rendering `path: +0 / -0  [both]` and
        # asserting a file had gained and lost text when it did neither.
        side = ("gained only" if a and not r else
                "lost only" if r and not a else
                "no counted lines" if not a and not r else "both")
        out.append(f"  {path or '(no path in header)'}: +{a:,} / -{r:,}  [{side}]")
    if len(rows) > MANIFEST_TABLE_ROWS:
        out.append(f"  … and {len(rows) - MANIFEST_TABLE_ROWS:,} more file(s), "
                   "not listed")

    out += ["", f"WHAT DID NOT SURVIVE — deleted and not re-added anywhere "
                f"({_residue_count(lost)})"]
    if lost:
        shown, elided = _listing(lost, MANIFEST_RESIDUE_LINES)
        out += [f"  - {b}" for b in shown]
        if elided:
            out.append(f"  … and {elided:,} more, not listed")
    else:
        out.append("  (nothing — every deleted line reappears somewhere)")

    out += ["", f"WHAT CHANGED BESIDES MOVING — added and not deleted anywhere "
                f"({_residue_count(gained)})"]
    if gained:
        shown, elided = _listing(gained, MANIFEST_RESIDUE_LINES)
        out += [f"  + {b}" for b in shown]
        if elided:
            out.append(f"  … and {elided:,} more, not listed")
    else:
        out.append("  (nothing — this is a pure move)")

    out += ["", "DEFINITIONS THIS CHANGE ADDS IN MORE THAN ONE PLACE — half of the "
                "duplicate-copy trap"]
    if dupes:
        out += [f"  ! {name} — added in " + ", ".join(
                    f"{f or '(no path in header)'}{'' if n == 1 else f' x{n}'}"
                    for f, n in sorted(where.items()))
                for name, where in dupes.items()]
        out.append("  A move that keeps both copies of a definition is a clean merge, "
                   "a green test run and a silent bug: the later binding wins and the "
                   "earlier one is dead. Each name above is added by this change in "
                   "more than one place.")
    else:
        out.append("  (none found.)")
    # The coverage disclaimer is UNCONDITIONAL, and it used to print only in the
    # empty branch. That inverted this module's own rule — "a pattern that matches
    # nothing is worse than an absent section: it reads as 'checked, and clean'" —
    # one step along: a section that found ONE duplicate read as having found THE
    # duplicates, with nothing beside it about what the scan cannot see. A TS move
    # that duplicates a `function` and a class METHOD listed the function, and the
    # reviewer filed the section as exhaustively answered. The limits are the same
    # limits whether the scan fired or not, so they are stated the same way — as
    # the unseeable-original note in WHAT IS NOT HERE already is.
    out.append(f"  Scanned over {' and '.join(_DEF_LANGUAGES)} — "
               f"{', '.join(_DEF_SPELLINGS)}. Class and object METHODS, any "
               "definition whose signature wraps onto a second line, and every "
               "other language are NOT covered, so this section can be empty or "
               "short and still be wrong — say so.")
    # The false positives, named beside the false negatives for the same reason.
    # The check is "this name is added more than once", which several legitimate
    # idioms are by design, and a section whose heading tells a reviewer "each name
    # above is a finding unless there is a reason it is not" has to say which
    # reasons are ordinary. Otherwise the first `@overload` chain it flags teaches
    # the reader to stop believing the section, which costs more than the miss.
    out.append("  Nor is every hit a fault: an `@typing.overload` stub chain, an "
               "`if TYPE_CHECKING:`/`else:` pair and platform-conditional "
               "definitions all define one name more than once ON PURPOSE, in one "
               "file. Those are the expected shape, not the trap.")

    out += ["", "WHAT IS NOT HERE",
            "  Test counts before and after, and whether any module now reaches "
            "backward into another, are the other two facts that bear on a move. "
            "Both need the PR checked out and the panel has only the diff, so "
            "neither has been measured. Do not assume either is fine.",
            "  Nor is the OTHER half of the duplicate-copy trap, and it is the more "
            "common half: an original definition left exactly where it was, in a file "
            "this change never touches, while a copy arrives somewhere new. That "
            "original appears in the diff as neither an added nor a deleted line — it "
            "is not in the diff at all — so the section above cannot see it and does "
            "not claim to. If a name in WHAT MOVED WHERE or WHAT CHANGED BESIDES "
            "MOVING looks like it may still exist at its old address, say so in "
            "`could_not_assess`: checking it needs the branch, and nobody here has it."]
    return "\n".join(out) + "\n"


def _ci_line(status: str, failing: tuple[str, ...] = (), skip: str = "") -> str:
    """One line of CI reading for a human, over :func:`panel_scope.review_ci`'s six
    states, #548's three local ones, plus "not read at all".

    Deliberately not :func:`panel_scope.ci_brief`, which covers the same states:
    that text is written AT a reviewer model ("do not spend a finding or a
    `could_not_assess` entry on them") and is a paragraph, and both are wrong under
    a refusal notice whose whole point is that no model was involved. What the two
    must agree about is the discipline, not the words — `PENDING`, `none` and
    `unknown` each say "this is not a pass" in their own sentence, because a
    refusal notice that lets a missing gate read as a green one is the same failure
    as a refusal that reads as a clean review. `blocked` is the sixth and the one
    #324 had no word for: a run that exists, will not execute without a person, and
    so reports nothing — which is not the same sentence as `none` and must not
    borrow its wording.
    """
    if not status:
        return "NOT read for this refusal. Its result is unknown, not a pass."
    if status == "PASS":
        return ("PASSED on this commit — every test the project has thought to write "
                "is green. That is not evidence the code is correct, and it is not a "
                "review.")
    if status == "FAIL":
        return (f"FAILED. Non-passing checks: {', '.join(failing) or 'names unavailable'}. "
                "Something the project already tests is broken by this PR, and no "
                "reviewer had to read the diff to know it.")
    if status == "PENDING":
        return "STILL RUNNING, so its result is not known. This is not a pass."
    if status == "blocked":
        return ("EXISTS BUT WILL NOT RUN — the run for this commit is waiting on a "
                "human to approve it, so it has executed nothing. This is not a pass, "
                "and no amount of waiting changes it.")
    if status == "none":
        return ("NO RUN EXISTS for this commit, so there is no suite result either "
                "way. This is not a pass.")
    # #548's three. A refusal never runs a local suite — nothing is dispatched, so
    # there is nobody to raise the floor under, and spending fifteen minutes of test
    # run on a round that reviews nothing would contradict "a refusal must cost
    # nothing". They are rendered anyway, because the alternative is the catch-all
    # below telling a human that a suite which ran and passed "could NOT be read",
    # and a renderer that lies about the one state it never sees today is a renderer
    # that lies the first day something calls it with one.
    if status == LOCAL_PASS:
        return ("PASSED, but LOCALLY — no GitHub run exists for this commit, so the "
                "repo's own suite was run on this box instead. Weaker evidence than a "
                "green CI run and not a substitute for one: different machine, and "
                "nothing says this is the commit that will merge.")
    if status == LOCAL_FAIL:
        return (f"FAILED LOCALLY — no GitHub run exists for this commit, so the repo's "
                f"own suite was run on this box and it went red: "
                f"{', '.join(failing) or 'names unavailable'}. This is not a pass.")
    if status == LOCAL_UNREAD:
        return ("was ATTEMPTED LOCALLY and produced no result"
                + (f" ({skip})" if skip else "")
                + " — no GitHub run exists for this commit either. This is not a pass.")
    return ("could NOT be read" + (f" ({skip})" if skip else "")
            + ". Its result is unknown. This is not a pass.")


def refusal_report(repo_name: str, pr_number: int, title: str,
                   base: str, pre: Preflight,
                   ci_status: str = "", ci_failing: tuple[str, ...] = (),
                   ci_skip: str = "") -> str:
    """The refusal, written to be read by whoever finds it — in a terminal, on the
    PR, or in a payload six weeks later.

    Shaped like the panel's own report (a heading, then the facts) so a reader
    scanning a PR's comments does not have to work out which of the two they are
    looking at, and headed with a sentence that cannot be mistaken for a clean
    result. "0 findings" and "nobody looked" render identically everywhere else in
    this harness, and every guard in it exists because that once cost somebody a
    merge.

    Refuses to render anything but a refusal. Handed a ``run`` verdict it would
    print "**Why:** ." over a measurement table and a list of remedies — a
    document that reads exactly like a refusal, names no reason, and would be
    posted to the PR. That is not hypothetical: it is what the first hand-run of
    this function produced. A caller holding the wrong verdict has a bug, and the
    bug has to surface here rather than on somebody's PR. (`cap` is never None on
    a refusal — none is reachable without a ceiling — but it is formatted with
    ``:,`` below, so the same check covers it rather than leaving a ``TypeError``
    to be raised from the middle of a report.)

    That check is a ``raise``, not an ``assert``. ``python -O`` and a non-empty
    ``PYTHONOPTIMIZE`` strip assertions, and a guard the docstring above insists is
    not hypothetical must not be one that a flag on the interpreter removes: with
    it gone the reasonless notice is back on somebody's PR, or the ``{pre.cap:,}``
    below raises ``TypeError`` from the middle of report construction. Nothing here
    is hot, so an explicit test costs nothing worth counting.

    **The two HARD gates are REPORTED here, and neither is read here.** This
    function calls nothing: `ci_status`/`ci_failing`/`ci_skip` arrive as arguments,
    and the `gh pr checks` that produced them is made by `panel.run`, in the
    refusal branch, before this is called. The distinction is worth the sentence
    because the docstring used to claim the read, and a reader auditing where a
    refusal's one extra API call happens was sent to the wrong function — the same
    class of wrong-place claim the rest of this module is careful about. `ci_skip`
    arrives as the BARE reason (`TimeoutExpired`), not the `ci: TimeoutExpired`
    form `review_ci` returns for `PanelResult.skipped`; :func:`_ci_line` supplies
    its own label.

    What is worth stating is WHY a refusal carries them at all. CI is
    size-independent, costs one `gh pr checks`, and is the one part of a round a
    763 KB diff cannot make useless — a refusal that also lost the build status
    left `/panel-review-pr` told to stop the cycle with nothing said about a red
    suite. Sonar is different and is not read at all: it is a selected panel
    MEMBER, with a `ran: false` row in the payload like every other seat, and
    dispatching it while telling the board no member ran would be the inconsistency
    this whole path exists to avoid. So the notice states that gate was not
    evaluated rather than quietly leaving its default to be read as a pass.

    **Every measurement below is printed in the unit the verdict was made in.**
    `pre.over` is a ratio of a size to a ceiling, and which of `shape.chars` and
    `shape.bytes` is its numerator depends on which seat's ceiling bound — see
    :class:`Ceiling`. Printing `shape.chars` under a byte-derived ratio produced a
    notice that refuted itself in three lines: "100,000 chars … tightest seat
    ceiling: 120,000 chars, exceeded 2.2x", where the reader who divides gets 0.83x
    and concludes the tool is broken. Both readings are printed whenever they
    differ, so the multiple can be checked by hand from the numbers on the page
    rather than from an encoding the reader would have to guess.
    """
    if not pre.refused or (pre.cap is None and not pre.gate):
        raise ValueError(
            f"refusal_report on a {pre.verdict!r} verdict (cap={pre.cap!r}) — only a "
            "refusal has a reason to state, and a reasonless refusal notice is worse "
            "than no notice at all")
    s = pre.shape
    widest = max(s.added, s.removed)
    lines = [
        "\n## 🛑 Panel REFUSED — no review happened",
        "",
        f"[{repo_name}#{pr_number}] {_ellipsis(title, 60)}",
        f"  base={base}",
        "",
        "**This is not a clean review. Nothing was read and nothing was found, "
        "because no seat was dispatched.** Do not read the absence of findings "
        "below as the absence of defects.",
        "",
        f"**Why:** {pre.reason}.",
        "",
        "**The gates, which do not depend on the diff's size:**",
        f"  - CI: {_ci_line(ci_status, ci_failing, ci_skip)}",
        "  - SonarCloud: NOT evaluated. It is a panel member, and no member was "
        "dispatched — read its absence here as unknown, never as a pass.",
        "",
        "**The measurement:**",
        # Both readings whenever they differ, because the ratio two lines down was
        # computed from exactly one of them and a reader with only the other cannot
        # check it. Identical on an all-ASCII diff, where a second copy of the same
        # number would be noise.
        f"  - diff: {s.chars:,} chars"
        + (f" / {s.nbytes:,} bytes" if s.nbytes != s.chars else "")
        + f", {s.files:,} file(s), +{s.added:,} / -{s.removed:,} non-blank lines",
    ]
    if pre.gate:
        # A precondition refusal, and every line below the diff's own size would be
        # about a budget question this round never reached. The size is still
        # printed above — it is what a reader is owed about the thing that was NOT
        # reviewed — but the ceiling, the move ratio and "split the PR" all describe
        # a problem this refusal is not about, and `pre.cap` may legitimately be
        # None here (a repo with no configured budget declares no ceiling at all,
        # and this refusal does not need one).
        lines += [
            "  - the size was not the problem, and no ceiling was consulted: this "
            "round was refused on a precondition, before any seat was dispatched.",
            "",
            "**What to do,** in the order they are worth doing:",
            "  1. Rebase the branch onto its base and push, then re-run the panel. "
            "A review of a branch that cannot merge is a review of a diff that is "
            "about to change, and at `review_panel.max_rounds: 1` nothing re-reads "
            "what the rebase does.",
            "  2. `review_panel.require_mergeable: false` if this repo genuinely "
            "wants conflicted branches reviewed — an architectural read where the "
            "conflict is incidental is a real case.",
            "  3. `--force` to review this one anyway. The findings will be about "
            "code whose merged form does not exist yet; read them as provisional.",
        ]
        return "\n".join(lines)
    lines += [
        f"  - relocated: {s.moved:,} of {widest:,} ({s.move_ratio * 100:.1f}%) — "
        f"the move threshold is {pre.thresholds.get('move_shape_ratio'):g}",
        f"  - tightest seat ceiling: {pre.cap:,} {pre.cap_unit} ({pre.cap_seat}), "
        f"exceeded {pre.over:.1f}x by this diff's {pre.measured:,} "
        f"{pre.cap_unit} — the refusal threshold is "
        f"{pre.thresholds.get('refuse_over_cap_multiple'):g}x"
        + (" (that seat's prompt travels in argv, so the kernel's ceiling is in "
           "BYTES rather than characters)" if pre.cap_unit == "bytes" else ""),
        "",
        "**What to do,** in the order they are worth doing:",
        "  1. Split the PR. A diff this far over every seat's ceiling is a diff "
        "no reviewer reads in one sitting either.",
        # Remedy 2 depends on WHOSE ceiling this was, and the byte case is the one
        # where the obvious advice is wrong. `ARGV_PROMPT_MAX_BYTES` is the kernel's
        # `MAX_ARG_STRLEN`: no key in `.harness-rules` raises it, and an operator
        # who follows a "raise `max_diff_chars`" line here gets the same refusal
        # back and no idea why. This case only became visible when the notice
        # started saying which unit it measured in — before that it read as an
        # ordinary configured ceiling, which is exactly the confusion.
        ("  2. Drop the `antigravity` seat for this PR, or split it. This ceiling "
         "is the kernel's argv limit rather than a configured budget, and no "
         "setting raises it — that seat is the only one whose prompt cannot travel "
         "on stdin." if pre.cap_unit == "bytes" else
         "  2. Raise `review_panel.max_diff_chars` (or the cap of the one seat "
         "holding the floor) if a model you run can genuinely take it."),
        "  3. `--force` to review it anyway, and read the result knowing most of "
        "each seat's budget went on text it could not usefully judge.",
    ]
    return "\n".join(lines)


# The PRIVATE helpers are deliberately absent, and their absence is the point.
# `panel.py` does `from panel_preflight import *` LAST of five sibling modules, so
# anything listed here wins a name collision against `panel_core`, `panel_seats`,
# `panel_scope` and `panel_rounds` — silently, with no error and no test able to
# notice, because the tests reach each module's helpers through the module object
# (`pf._rule`) rather than through `panel`. `_rule`, `_listing` and `_quote` are
# generic enough names that a sibling could grow one of its own any day. No
# collision exists today (checked across all four); this is the cheap half of not
# having to check again.
__all__ = [
    "DEFAULT_MOVE_SHAPE_RATIO", "DEFAULT_REFUSE_OVER_CAP_MULTIPLE",
    "MANIFEST_TABLE_ROWS", "MANIFEST_RESIDUE_LINES", "MANIFEST_LINE_CHARS",
    "MOVE_MANIFEST_HEADER", "PAYLOAD_FILE_ROWS",
    "DiffShape", "DiffParts", "Preflight", "diff_shape",
    "Ceiling", "seat_ceilings", "tightest_ceiling", "preflight",
    "move_manifest", "duplicate_definitions", "refusal_report",
]
