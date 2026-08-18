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
    (see :func:`smallest_cap`).

``manifest``
    The diff does not fit, AND it is move-shaped: the added lines are a
    near-permutation of the deleted ones. A move is not reviewable as content by
    anybody, at any budget, so it is reviewed as a **manifest** instead — what
    moved where, what did NOT survive, what changed *besides* moving, and which
    definitions now exist in more than one file (#62's trap: a clean merge that
    keeps both copies and silently uses the second). That is a different and much
    smaller prompt, and it is the one a reviewer could actually answer.

``refuse``
    The diff does not fit by some multiple of the smallest configured cap and is
    not move-shaped, so there is no smaller honest question to ask. Refuse,
    loudly, and say why. ``--force`` overrides.

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
import shutil
from collections import Counter
from dataclasses import dataclass, field

from panel_core import ARGV_PROMPT_MAX_BYTES, CLI_BIN, _diff_file_path

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
#: PR #137 was 6.4x. `None` or 0 disables the refusal and keeps the manifest.
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

#: What a definition looks like, for the duplicate check. Python and the
#: C-family/JS spellings only, because those are what the fleet's repos are
#: written in and a pattern that matches nothing is worse than an absent section:
#: it reads as "checked, and clean". :func:`duplicate_definitions` says which
#: languages it covered for exactly that reason — a Go or Rust move gets the rest
#: of the manifest and an explicit "not checked here" instead of a false all-clear.
_DEF_PATTERNS = (
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\("),
    re.compile(r"^\s*class\s+([A-Za-z_]\w*)\s*[(:]"),
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("),
)

#: Languages :data:`_DEF_PATTERNS` actually covers, named in the manifest so a
#: reader of a Go move is told the section did not apply rather than left to read
#: an empty one as a clean bill.
_DEF_LANGUAGES = ("Python", "JavaScript/TypeScript")


# ----------------------------------------------------------------------------- shape

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

    #: Characters of diff, which is the quantity every cap is expressed in.
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
    files_added_only: tuple[str, ...] = ()
    files_removed_only: tuple[str, ...] = ()

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
        return {"chars": self.chars, "added": self.added, "removed": self.removed,
                "moved": self.moved, "move_ratio": round(self.move_ratio, 4),
                "files": self.files,
                "files_added_only": list(self.files_added_only),
                "files_removed_only": list(self.files_removed_only)}


def _hunk_bodies(diff: str) -> tuple[Counter, Counter, dict[str, tuple[int, int]]]:
    """``(added, removed, per_file)`` — the line multisets and each file's tally.

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
    """
    added: Counter[str] = Counter()
    removed: Counter[str] = Counter()
    per_file: dict[str, list[int]] = {}
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
        elif line.startswith("-"):
            body = line[1:]
            tally = per_file.setdefault(cur, [0, 0])
            tally[1] += 1
            if body.strip():
                removed[body] += 1
    return added, removed, {f: (a, r) for f, (a, r) in per_file.items()}


def diff_shape(diff: str) -> DiffShape:
    """Measure a diff. Cheap enough to run on every round: two passes over the
    text and a Counter intersection, against the minutes a round costs."""
    added, removed, per_file = _hunk_bodies(diff)
    moved = sum((added & removed).values())
    # A file the change only adds to, or only takes from — the shape of a split's
    # source and its destinations. Files with no counted lines either way (a mode
    # change, a pure rename git recorded without content) are neither.
    added_only = tuple(f for f, (a, r) in per_file.items() if a and not r)
    removed_only = tuple(f for f, (a, r) in per_file.items() if r and not a)
    return DiffShape(chars=len(diff), added=sum(added.values()),
                     removed=sum(removed.values()), moved=moved,
                     files=len(per_file), files_added_only=added_only,
                     files_removed_only=removed_only)


# ----------------------------------------------------------------------------- the cap

def seat_installed(name: str) -> bool:
    """Is this seat's CLI on PATH — i.e. can it run on THIS box at all?

    The same test `run_cli` makes before dispatching a seat, through the same
    `CLI_BIN` mapping (the reviewer is `antigravity`, the command is `agy`), so the
    two cannot come to disagree about which seats exist here.
    """
    return bool(shutil.which(CLI_BIN.get(name, name)))


def smallest_cap(budgets: dict[str, int | None],
                 installed=None) -> tuple[int | None, str]:
    """``(chars, whose)`` — the tightest ceiling any seat that can actually RUN
    here is under.

    A seat's cap is its configured `max_diff_chars`, and for `antigravity` it is
    additionally the kernel's: that seat's prompt travels in argv, so
    :data:`ARGV_PROMPT_MAX_BYTES` applies to it whether or not the repo set a
    number. Compared against the constant rather than against
    :func:`panel_seats.fit_argv_budget`'s exact answer, which is a few hundred
    bytes lower once the template is counted — a verdict about whether a diff is
    3x or 6x over does not turn on that, and depending on the render closure
    would make this callable only after the prompt exists.

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

    ``(None, "")`` when no runnable seat declares a cap, and that answer is
    load-bearing: it is what keeps this file from becoming the default diff budget
    #49 refused. A repo running claude and codex off stdin with no `max_diff_chars`
    has declared no ceiling, so there is no size for a refusal to be measured
    against and the round proceeds exactly as it always has.
    """
    here = installed or seat_installed
    caps: dict[str, int] = {}
    for name, budget in budgets.items():
        if not here(name):
            continue
        cap = budget
        if name == "antigravity":
            cap = ARGV_PROMPT_MAX_BYTES if cap is None else min(cap, ARGV_PROMPT_MAX_BYTES)
        if cap is not None:
            caps[name] = cap
    if not caps:
        return None, ""
    whose = min(caps, key=lambda n: (caps[n], n))
    return caps[whose], whose


def _rule(panel: dict, key: str, fallback, notes: list[str]):
    """One numeric pre-flight setting, with the same manners
    :func:`panel_seats.diff_budget` gives a diff budget: unset is silent, and a
    value that cannot be the thing at all falls back and SAYS so.

    Absent and null mean "use the default"; 0 and None-after-a-value mean
    "switched off" only where the caller reads them that way — this returns the
    number and rules on nothing else.
    """
    raw = panel.get(key)
    if raw is None or raw == "":
        return fallback
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        notes.append(f"`{key}`={raw!r} is not a number — using {fallback}")
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
    if n < 0:
        notes.append(f"`{key}`={raw!r} cannot be negative — using {fallback}")
        return fallback
    return n


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
    #: The tightest seat ceiling and which seat is under it; None when no seat
    #: declares one, in which case nothing here ever fires.
    cap: int | None = None
    cap_seat: str = ""
    #: How many times over the cap the diff is, to one decimal. 0.0 with no cap.
    over: float = 0.0
    #: Did `--force` overrule a refusal or a manifest? Recorded rather than
    #: inferred from `verdict == "run"`, because "the tool chose to run" and "a
    #: caller overrode the tool" are the two things this repo's standing rule
    #: says must never look alike.
    forced: bool = False
    #: What the verdict WOULD have been without --force.
    would_have: str = ""
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

    def as_dict(self) -> dict:
        return {"verdict": self.verdict, "reason": self.reason or None,
                "cap": self.cap, "cap_seat": self.cap_seat or None,
                "over_cap": round(self.over, 2) or None,
                "forced": self.forced, "would_have": self.would_have or None,
                "thresholds": self.thresholds, "shape": self.shape.as_dict()}


def preflight(diff: str, budgets: dict[str, int | None], panel: dict,
              notes: list[str], forced: bool = False,
              installed=None) -> Preflight:
    """Rule on a round before it is dispatched.

    The order of the tests is the argument. Fitting the cap settles it — a diff
    every seat can read is a diff to read, whatever its shape, because reading a
    small move as content costs nothing and tells you strictly more than a
    manifest of it does. Over the cap, SHAPE decides: a move gets the manifest at
    any multiple, because no budget makes relocated text reviewable and there is
    a better question to ask instead. Only a diff that is over the cap by the
    multiple AND has no smaller honest question behind it is refused.

    That leaves "over the cap, under the multiple, not a move" as ``run``, which
    is today's behaviour and #75's truncation report — unchanged on purpose. This
    exists for the case where truncation has stopped being a caveat and become
    the review.
    """
    shape = diff_shape(diff)
    ratio = _rule(panel, "move_shape_ratio", DEFAULT_MOVE_SHAPE_RATIO, notes)
    multiple = _rule(panel, "refuse_over_cap_multiple",
                     DEFAULT_REFUSE_OVER_CAP_MULTIPLE, notes)
    manifest_on = panel.get("manifest_moves", True)
    thresholds = {"move_shape_ratio": ratio, "refuse_over_cap_multiple": multiple,
                  "manifest_moves": bool(manifest_on)}
    cap, seat = smallest_cap(budgets, installed)
    over = (shape.chars / cap) if cap else 0.0

    def verdict(name: str, reason: str, manifest: str = "") -> Preflight:
        # --force is applied HERE rather than by the caller, so the payload
        # carries what the tool decided AND what the caller did about it. A flag
        # that erases the verdict it overrode leaves no evidence the tool was
        # ever asked.
        if name != "run" and forced:
            return Preflight("run", f"--force: {reason}", shape, cap, seat, over,
                             forced=True, would_have=name, thresholds=thresholds)
        return Preflight(name, reason, shape, cap, seat, over,
                         thresholds=thresholds, manifest=manifest)

    if cap is None:
        return verdict("run", "")
    if shape.chars <= cap:
        return verdict("run", "")

    moved_pct = f"{shape.move_ratio * 100:.1f}%"
    shape_said = (f"{shape.moved:,} of {max(shape.added, shape.removed):,} changed "
                  f"lines ({moved_pct}) appear on both sides of the diff")
    over_said = (f"{shape.chars:,} chars of diff against {seat}'s "
                 f"{cap:,}-char ceiling ({over:.1f}x)")

    tried_manifest = ""
    if manifest_on and shape.is_move(ratio):
        text = move_manifest(diff, shape)
        # A manifest that is not SMALLER than the diff is not a saving, it is a
        # second copy of the problem. Its body scales with the change's shape but
        # its brief and section headers are a fixed kilobyte or so, so on a diff
        # only just over a small ceiling the substitution can hand a seat MORE
        # text than the diff did — and then have it truncated, so the seat reads a
        # prefix of a manifest instead of a prefix of a diff. Measured rather than
        # assumed, because "the manifest is always smaller" is exactly the kind of
        # claim that is true of every case anyone tested.
        if len(text) < shape.chars:
            return verdict("manifest",
                           f"this change is move-shaped — {shape_said}, so they "
                           f"were relocated rather than written. {over_said}, and no "
                           "budget makes relocated text reviewable as content: a seat "
                           "would "
                           "spend the round re-reading code that is already in the base "
                           "branch and report findings about it. Reviewed as a MANIFEST "
                           "instead — what moved where, what did not survive, and what "
                           "changed besides moving", text)
        tried_manifest = (f"; it IS move-shaped ({shape_said}), but a manifest of it "
                          f"came to {len(text):,} chars against the diff's "
                          f"{shape.chars:,} and so would replace the problem with a "
                          "copy of it")
    if multiple and over > multiple:
        why_not_a_move = tried_manifest or (
            f", and it is not move-shaped ({shape_said} — under the "
            f"{ratio:g} move ratio), so there is no smaller honest question to ask "
            "instead")
        return verdict("refuse",
                       f"{over_said}, past the {multiple:g}x refusal threshold"
                       f"{why_not_a_move}. A truncated read that produces "
                       "findings is worse than no review: the next step of the cycle "
                       "briefs a fixer to resolve every one of them. Split the PR, "
                       "raise the cap for a seat that can take it, or pass --force")
    return verdict("run", "")


# ----------------------------------------------------------------------------- manifest

def duplicate_definitions(added: Counter) -> dict[str, list[str]]:
    """Definitions the change ADDS in more than one place, keyed by name.

    #62's trap, and the reason it is worth a section of its own: a merge that
    keeps both copies of a moved function is a clean merge, a green test run and
    a silent bug, because Python binds the second definition and the first is
    dead. A move is precisely the change that produces it.

    Takes the added-line multiset rather than the diff, because a definition line
    added twice is the whole signal and a Counter already holds that. Covers the
    spellings in :data:`_DEF_LANGUAGES` and nothing else — see there for why the
    manifest says so out loud.
    """
    seen: dict[str, int] = Counter()
    for body, n in added.items():
        for pat in _DEF_PATTERNS:
            m = pat.match(body)
            if m:
                seen[m.group(1)] += n
                break
    return {name: [] for name, n in sorted(seen.items()) if n > 1}


def _quote(body: str) -> str:
    body = body.rstrip()
    if len(body) > MANIFEST_LINE_CHARS:
        body = body[:MANIFEST_LINE_CHARS] + " …"
    return body


def _listing(bodies: Counter, cap: int) -> tuple[list[str], int]:
    """``(lines, elided)`` — up to `cap` quoted lines, longest first.

    Longest first because the residue of a move is where the real change is, and
    a one-token line is the least likely of them to be it. Sorted by length then
    text, so the same diff produces the same manifest twice — a listing that
    reorders between two runs of the same round is a diff nobody can compare.
    """
    ordered = sorted(bodies.elements(), key=lambda b: (-len(b.strip()), b))
    return [_quote(b) for b in ordered[:cap]], max(0, len(ordered) - cap)


def move_manifest(diff: str, shape: DiffShape | None = None) -> str:
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
    """
    shape = shape or diff_shape(diff)
    added, removed, per_file = _hunk_bodies(diff)
    survived = added & removed
    lost = removed - survived        # deleted, and not re-added anywhere
    gained = added - survived        # added, and not deleted anywhere
    dupes = duplicate_definitions(added)

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
        side = ("gained only" if a and not r else
                "lost only" if r and not a else "both")
        out.append(f"  {path or '(no path in header)'}: +{a:,} / -{r:,}  [{side}]")
    if len(rows) > MANIFEST_TABLE_ROWS:
        out.append(f"  … and {len(rows) - MANIFEST_TABLE_ROWS:,} more file(s), "
                   "not listed")

    out += ["", f"WHAT DID NOT SURVIVE — deleted and not re-added anywhere "
                f"({sum(lost.values()):,} line(s))"]
    if lost:
        shown, elided = _listing(lost, MANIFEST_RESIDUE_LINES)
        out += [f"  - {b}" for b in shown]
        if elided:
            out.append(f"  … and {elided:,} more, not listed")
    else:
        out.append("  (nothing — every deleted line reappears somewhere)")

    out += ["", f"WHAT CHANGED BESIDES MOVING — added and not deleted anywhere "
                f"({sum(gained.values()):,} line(s))"]
    if gained:
        shown, elided = _listing(gained, MANIFEST_RESIDUE_LINES)
        out += [f"  + {b}" for b in shown]
        if elided:
            out.append(f"  … and {elided:,} more, not listed")
    else:
        out.append("  (nothing — this is a pure move)")

    out += ["", "DEFINITIONS ADDED IN MORE THAN ONE PLACE — the duplicate-copy trap"]
    if dupes:
        out += [f"  ! {name}" for name in dupes]
        out.append("  A move that keeps both copies of a definition is a clean merge, "
                   "a green test run and a silent bug: the later binding wins and the "
                   "earlier one is dead. Each name above is added more than once.")
    else:
        out.append(f"  (none found, over {' and '.join(_DEF_LANGUAGES)} definition "
                   "spellings only — a move in any other language is NOT covered by "
                   "this check and you should say so)")

    out += ["", "WHAT IS NOT HERE",
            "  Test counts before and after, and whether any module now reaches "
            "backward into another, are the other two facts that bear on a move. "
            "Both need the PR checked out and the panel has only the diff, so "
            "neither has been measured. Do not assume either is fine."]
    return "\n".join(out) + "\n"


def refusal_report(repo_name: str, pr_number: int, title: str,
                   base: str, pre: Preflight) -> str:
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
    ``:,`` below, so the same assertion covers it rather than leaving a
    ``TypeError`` to be raised from the middle of a report.)
    """
    assert pre.refused and pre.cap is not None, (
        f"refusal_report on a {pre.verdict!r} verdict (cap={pre.cap!r}) — only a "
        "refusal has a reason to state, and a reasonless refusal notice is worse "
        "than no notice at all")
    s = pre.shape
    widest = max(s.added, s.removed)
    lines = [
        "\n## 🛑 Panel REFUSED — no review happened",
        "",
        f"[{repo_name}#{pr_number}] {title[:60]}",
        f"  base={base}",
        "",
        "**This is not a clean review. Nothing was read and nothing was found, "
        "because no seat was dispatched.** Do not read the absence of findings "
        "below as the absence of defects.",
        "",
        f"**Why:** {pre.reason}.",
        "",
        "**The measurement:**",
        f"  - diff: {s.chars:,} chars, {s.files:,} file(s), "
        f"+{s.added:,} / -{s.removed:,} non-blank lines",
        f"  - relocated: {s.moved:,} of {widest:,} ({s.move_ratio * 100:.1f}%) — "
        f"the move threshold is {pre.thresholds.get('move_shape_ratio'):g}",
        f"  - tightest seat ceiling: {pre.cap:,} chars ({pre.cap_seat}), "
        f"exceeded {pre.over:.1f}x — the refusal threshold is "
        f"{pre.thresholds.get('refuse_over_cap_multiple'):g}x",
        "",
        "**What to do,** in the order they are worth doing:",
        "  1. Split the PR. A diff this far over every seat's ceiling is a diff "
        "no reviewer reads in one sitting either.",
        "  2. Raise `review_panel.max_diff_chars` (or the cap of the one seat "
        "holding the floor) if a model you run can genuinely take it.",
        "  3. `--force` to review it anyway, and read the result knowing most of "
        "each seat's budget went on text it could not usefully judge.",
    ]
    return "\n".join(lines)


__all__ = [
    "DEFAULT_MOVE_SHAPE_RATIO", "DEFAULT_REFUSE_OVER_CAP_MULTIPLE",
    "MANIFEST_TABLE_ROWS", "MANIFEST_RESIDUE_LINES", "MANIFEST_LINE_CHARS",
    "MOVE_MANIFEST_HEADER",
    "DiffShape", "Preflight", "diff_shape", "seat_installed", "smallest_cap",
    "preflight",
    "move_manifest", "duplicate_definitions", "refusal_report",
    "_hunk_bodies", "_rule", "_listing", "_quote",
]
