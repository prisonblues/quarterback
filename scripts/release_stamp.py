#!/usr/bin/env python3
"""Stamp a release number when the branch LANDS, instead of picking one when it is written.

A release number is a shared namespace with no lock on it. Every branch that wanted one
picked the next free number at the moment it wrote its CHANGELOG entry, which is the one
moment nobody can know the answer: the branch that lands first takes it, and everybody
else forked before that happened. Ten collisions in two days, and the tenth arrived an
HOUR after the board's allocator shipped and worked — two agents simply did not call it.
That is the argument for this file rather than for more allocator: a lock that has to be
remembered is a lock that will be forgotten, whereas a placeholder cannot be got wrong.

So a branch writes a placeholder:

    ## vNEXT — what this release does

and never names a number. This tool resolves it at land time, when the answer is knowable
because `origin/main` is sitting right there:

    release_stamp.py preflight            # what would happen, read-only
    release_stamp.py apply                # rewrite the worktree (never commits)
    release_stamp.py check                # is anything unstamped? (the post-merge guard)

The number is `max(release headings at --onto) + 1`, or `(major + 1).0` under `--major`,
which is the one part a tool cannot infer. It is computed from the CHANGELOG at a git ref
and from nothing else — not from a live board, not from the local checkout's own history.

## When two branches stamp the same number

They can, and the recovery is deliberately manual and deliberately one edit long. Once
`apply` has run, the placeholder is GONE — the branch says `## v2.34`, and re-running
`apply` has nothing left to rewrite. There is no automatic re-stamp and this file does not
pretend otherwise; what it does instead is make the collision impossible to miss:

  * both branches carry `## v2.34`, the merge conflicts on the CHANGELOG, you keep both
    sides, and `preflight`/`apply`/`check` all refuse on the duplicate heading;
  * or you have not merged yet, and `preflight`/`apply` refuse because the number your
    branch carries already exists at `--onto` under a different title.

Either way the repair is: put YOUR entry back to `## vNEXT` (and its README bullet back to
`- **vNEXT** — …`), then run `apply` again. Two tokens, because nothing else in the branch
was ever written in terms of the number — that is what "cheap to redo" actually buys, and
it is worth more than an unstamp command that would have to guess which of two identical
headings belongs to you.

**This is not an allocator and deliberately does not become one.** #46/#99's
`POST /release/claim` records that a caller INTENDS to take a number; it is an
announcement, not a reservation, and this tool neither reads it nor honours it. A branch
holding a claim for v2.34 gets no protection here: the next `apply` on any branch stamps
v2.34 too, because a stamped number is only ever "the next one free at the ref I merged
into", which is a question a git ref answers on its own. Announce a claim if it helps a
human coordinate; do not rely on it to keep a number free.

## What counts as a placeholder

Only tracked **markdown**, and only where a release is NAMED:

  * a heading — `## vNEXT — …`
  * a bold run — `- **vNEXT** — …`, `**vNEXT.**`, `**vNEXT — …**`

Occurrences inside a fenced block or an inline code span are documentation OF this
mechanism (this file's own README section writes ``vNEXT`` a dozen times) and are left
alone. An occurrence anywhere ELSE — bare in running prose, where the stamper would not
rewrite it — is a STOP rather than a shrug: it means a release entry was written in a
shape this tool does not recognise, and shipping it would put the literal string `vNEXT`
into a released document.

Untracked markdown is never stamped and never a STOP: `plan.md` is untracked in this repo
on purpose, is where the agents working it argue about releases in prose, and ships with
nothing. It is *reported* though, because a genuinely new doc is untracked for the minutes
between being written and being `git add`ed, and skipped-and-quiet is exactly how a literal
`vNEXT` reaches a reader. Ignored paths are not even reported — a warning nobody can act on
is a warning that gets skimmed.

## The served version

`pyproject.toml` and `app/main.py` carry the version `GET /openapi.json` reports, and most
releases here are harness-side and correctly leave it alone (v2.16, v2.17, v2.18, v2.20,
v2.21 and v2.32 all did). So the bump is INFERRED — from whether the branch changed `app/`
or `migrations/` — and always reported rather than done quietly. `--serve` / `--no-serve`
override the inference for the release the inference gets wrong.

## Exit codes

0 = go (stamped, or nothing to stamp) · 2 = STOP, a human decides.
Deliberately the same scheme as `scripts/migration_reconcile.py`, and for the same reason:
a caller consuming 0/2 reads Python's uncaught-exception 1 as "unknown", so every refusal
here is an explicit 2 with a sentence, never a traceback.

Usage (the file has a shebang and the executable bit; `python scripts/…` works too):
    release_stamp.py preflight [--repo DIR] [--onto REF] [--json] [--major]
    release_stamp.py apply     [--repo DIR] [--onto REF] [--json] [--major]
                               [--serve | --no-serve]
    release_stamp.py check     [--repo DIR] [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: The token a branch writes instead of a number. One string, spelled once, because the
#: whole point of this file is that the placeholder is unmistakable.
PLACEHOLDER = "vNEXT"

#: Paths whose change means the running board changed, and therefore that the served
#: version has to move with the release. `harness/`, `scripts/`, docs and tests do not.
BOARD_PATHS = ("app/", "migrations/")

#: A release as the CHANGELOG spells it: two components, with the minor optional because
#: this repo's first two releases are `## v1` and `## v2` and its next major will be `v3`.
Release = tuple[int, int]

#: `(?![\w.])` rather than `\b`, because `\b` fires between a digit and a dot: `## v2.33.1`
#: would parse as release (2, 33) and `## vNEXT.1` as a placeholder heading, and the rewrite
#: would then leave the trailing `.1` behind and produce `v2.34.1` — a version this repo does
#: not have a meaning for. A three-component heading is not a release entry this tool knows
#: how to number, so it must not match at all; the refusal that follows says so in a sentence.
_END = r"(?![\w.])"
_V = rf"v(\d+)(?:\.(\d+))?{_END}"

#: `## v2.33 — …` / `## vNEXT — …`. Anchored at line start on a level-2 heading, which is
#: what both CHANGELOG.md and README.md use for a release entry.
_HEADING = re.compile(rf"^##[ \t]+{_V}", re.MULTILINE)
_HEADING_PLACEHOLDER = re.compile(rf"^##[ \t]+{PLACEHOLDER}{_END}", re.MULTILINE)

#: Where a placeholder is legal, and therefore where it gets rewritten: a markdown heading
#: of any level, or the opening of a bold run. Group 1 is the token itself, so the rewrite
#: is a span replacement and the surrounding syntax is never reconstructed.
#:
#: The bold openers are LEFT-FLANKING, which is CommonMark's own rule and here the thing that
#: stops `**emphasis**vNEXT` being read as a placeholder site: the `**` before the token is
#: the CLOSE of an unrelated bold run, the token renders as plain running prose, and treating
#: it as rewritable would silently stamp a number into a sentence — the exact inverse of what
#: the loose-mention check exists to catch. `__` and `***` are here because they are valid
#: markdown for the same thing and an author who writes one has written a real placeholder,
#: not a mistake; classifying them as loose would refuse a correct branch.

#: What may follow the token at a site. Not `\b`, which fails on `__vNEXT__` because `_` is a
#: word character — the underscore spelling would be classified loose and refuse a correct
#: branch. Letters and digits are still rejected (`vNEXTish` is a different word), and so is a
#: dot followed by a digit: `**vNEXT.1**` is a three-component version this tool has no
#: meaning for, while `**vNEXT.**` is a sentence ending and is documented as legal.
_SITE_END = r"(?![0-9A-Za-z]|\.\d)"

_SITE = re.compile(
    rf"(?:^\#{{1,6}}[ \t]+|(?<![\w*])\*\*\*?|(?<![\w_])__)({PLACEHOLDER}){_SITE_END}",
    re.MULTILINE,
)

#: Any mention at all, for the "you wrote it somewhere I will not rewrite" check. Bounded the
#: same way as a site rather than by `\b`, so the two agree about where a token starts and
#: ends — a mention `_SITE` can see but `_MENTION` cannot is one that neither stamps nor stops.
_MENTION = re.compile(rf"(?<![0-9A-Za-z]){PLACEHOLDER}(?![0-9A-Za-z])")

#: `version = "2.33.0"`, keeping whatever trails it (there is a comment). Scoped by the
#: caller to pyproject.toml's `[project]` table — see `project_table`, which explains why a
#: file-wide search is the wrong thing.
_PYPROJECT_VERSION = re.compile(r'(?m)^(version[ \t]*=[ \t]*")\d+\.\d+\.\d+(")')

#: The header line of any TOML table, used to bound `[project]`.
_TOML_TABLE = re.compile(r"(?m)^[ \t]*\[")
_PROJECT_TABLE = re.compile(r"(?m)^[ \t]*\[project\][ \t]*(?:#.*)?$")

#: One argument of a call, with quoted strings and one level of parens treated as ATOMS.
#: That is what keeps a comma — or the literal text `version="1.0.0"` — inside a `title=` or
#: `description=` string from reading as a real keyword argument. A `[^()]`-style scan cannot
#: tell the two apart and would bump a version buried in a docstring while reporting success.
_ARG = r'(?:"[^"\n]*"|\'[^\'\n]*\'|\([^()]*\)|[^()"\'\n])'

#: `app = FastAPI(… version="2.33.0" …)`. Bounded to that call's own parentheses, tolerating
#: one level of nesting, and needing no DOTALL — the same shape (and the same reasoning)
#: as the fixture in harness/tests/test_release_numbers.py, which explains at length why a
#: lazy `.*?` version of this silently latches onto the next version literal in the file.
#: `version` must sit at the start of the call or immediately after a comma, so it is the
#: keyword argument and not a substring of an earlier one.
_SERVED_VERSION = re.compile(
    rf'(?m)^app[ \t]*=[ \t]*FastAPI\((?:{_ARG}*?,\s*)?version[ \t]*=[ \t]*"(\d+\.\d+\.\d+)"'
)


class StampError(Exception):
    """A refusal with a sentence attached. Always exits 2, never 1."""


@dataclass
class Site:
    """One placeholder occurrence the stamper will rewrite."""

    path: str
    line: int
    text: str  # the line it sits on, stripped, for the report


@dataclass
class Plan:
    """What `apply` would do. `preflight` prints this and changes nothing."""

    version: Release | None = None
    sites: list[Site] = field(default_factory=list)
    loose: list[Site] = field(default_factory=list)
    untracked: list[Site] = field(default_factory=list)
    symlinked: list[str] = field(default_factory=list)
    serves: bool = False
    serves_reason: str = ""
    served_from: str = ""
    served_to: str = ""
    onto_newest: Release | None = None
    major: bool = False

    @property
    def stamping(self) -> bool:
        return self.version is not None

    def as_json(self) -> dict:
        return {
            "stamping": self.stamping,
            "version": fmt(self.version) if self.version else None,
            "major": self.major,
            "onto_newest": fmt(self.onto_newest) if self.onto_newest else None,
            "sites": [{"path": s.path, "line": s.line, "text": s.text} for s in self.sites],
            "loose": [{"path": s.path, "line": s.line, "text": s.text} for s in self.loose],
            "untracked": [
                {"path": s.path, "line": s.line, "text": s.text} for s in self.untracked
            ],
            "symlinked": self.symlinked,
            "serves": self.serves,
            "serves_reason": self.serves_reason,
            "served_from": self.served_from or None,
            "served_to": self.served_to or None,
        }


# ------------------------------------------------------------------------- file i/o
#
# Every read and every write goes through these two. The exit-code contract at the top of
# this file promises that a refusal is always an explicit 2 with a sentence and never a
# traceback, and a bare `Path.read_text()` breaks that promise on inputs this repo actually
# has: a `.md` that is not UTF-8, a file mode 000 in a CI image, a full disk. `encoding` is
# pinned for the same reason — the docs here are full of em-dashes and arrows, and a
# container whose LANG resolves to POSIX would otherwise fail to decode its own CHANGELOG.


def _read(path: Path, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise StampError(
            f"{what} is not valid UTF-8 ({e.reason} at byte {e.start}). This tool reads and "
            "rewrites text; it will not guess an encoding for a file it is about to change"
        ) from e
    except OSError as e:
        raise StampError(f"cannot read {what}: {e.strerror or e}") from e


def _write(path: Path, text: str, what: str) -> None:
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        raise StampError(f"cannot write {what}: {e.strerror or e}") from e


# ---------------------------------------------------------------------------- git


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise StampError(f"git {' '.join(args)} failed: {proc.stderr.strip() or 'no output'}")
    return proc.stdout


def _git_ok(repo: Path, *args: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    ).returncode == 0


#: Case-insensitive, because `git ls-files -- '*.md'` is not: on Linux a `README.MD` or a
#: `NOTES.Md` is simply not in the scan, which is the same silent gap the untracked and
#: symlink warnings exist to close, for the cost of one pathspec magic word.
_MARKDOWN = ":(icase)*.md"


def tracked_markdown(repo: Path) -> list[str]:
    """Every tracked `.md` path, sorted. The stamp scope is markdown and only markdown:
    a placeholder has no meaning in code, and restricting the scan this way is also what
    keeps this tool's own docstring — which says `vNEXT` twenty times — out of its way."""
    out = _git(repo, "ls-files", "-z", "--", _MARKDOWN)
    return sorted({p for p in out.split("\0") if p})


def untracked_markdown(repo: Path) -> list[str]:
    """Markdown git is not tracking but is not ignoring either.

    Never stamped — `plan.md` is untracked on purpose, is where the agents working this repo
    argue about releases in prose, and is not part of any release. But it is also where a
    genuinely new doc sits for the minutes between being written and being `git add`ed, so a
    placeholder here is reported rather than passed over in silence: skipped-and-mentioned is
    recoverable, and skipped-and-quiet is how the literal string `vNEXT` reaches a reader.
    """
    out = _git(repo, "ls-files", "-z", "--others", "--exclude-standard", "--", _MARKDOWN)
    return sorted({p for p in out.split("\0") if p})


def resolve(repo: Path, ref: str) -> str:
    """`ref` as a commit SHA, resolved ONCE and passed around from then on.

    Every question this tool asks of the base — what the CHANGELOG says there, what this
    branch changed relative to it, whether it carries an unstamped placeholder — has to be
    asked of the same commit. Re-resolving the NAME each time means a concurrent push during
    a long `apply` can have the release number computed against one base and the
    served-version inference computed against another, with nothing anywhere noticing.
    """
    if not _git_ok(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"):
        raise StampError(
            f"ref {ref!r} does not exist here. Fetch it first — the number this tool hands "
            "out is only correct relative to the ref you are merging into"
        )
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def changed_paths(repo: Path, onto: str) -> list[str]:
    """Everything this branch changes relative to `onto`, INCLUDING the working tree.

    Committed and uncommitted both, because `apply` is meant to be run on a finished
    branch before the last commit — the CHANGELOG entry it is stamping is quite often
    still unstaged when it runs. Untracked files count too: a release that adds a new
    migration adds it as an untracked file right up until someone runs `git add`.

    Diffed against the MERGE BASE, not against `onto` itself. `git diff onto` describes
    the round trip — it also reports, in reverse, every path `onto` changed since the fork
    — so a branch that touches no board code at all would be told it changed `app/` merely
    because somebody else's release did. That is the inference deciding to bump the served
    version, so getting it backwards ships a version bump nobody wrote.

    `merge-base` without `--all` prints exactly one SHA even for a criss-cross history with
    several best common ancestors — git picks one rather than listing them — so there is no
    multi-line case to handle. `split()[0]` anyway: a one-token guard is cheaper than the
    next reader re-deriving that, and the failure it forecloses (a two-line string handed to
    `git diff`) surfaces as `fatal: ambiguous argument` rather than as anything readable.
    """
    out = _git(repo, "merge-base", onto, "HEAD").split()
    if not out:
        raise StampError(
            f"{onto} and HEAD have no common ancestor, so there is no base to compute this "
            "branch's changes against. Fetch the ref you are actually merging into"
        )
    base = out[0]
    diff = _git(repo, "diff", "--name-only", base).split("\n")
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard").split("\n")
    return sorted({p for p in [*diff, *untracked] if p})


# ------------------------------------------------------------------- release numbers


def release(major: str, minor: str | None) -> Release:
    return int(major), int(minor or 0)


def fmt(r: Release) -> str:
    """Spelled the way the files spell it: `v3`, not `v3.0`."""
    return f"v{r[0]}" if r[1] == 0 else f"v{r[0]}.{r[1]}"


def release_headings(text: str, where: str = "") -> list[tuple[Release, str]]:
    """Every `## vX[.Y]` heading as (release, the heading line stripped), in file order.

    Masked first, and this is the point where forgetting to would matter most: a `## v999`
    inside a fenced example — this repo's own README and CHANGELOG now carry several, since
    they document the convention — would otherwise be the highest heading in the file and
    hand out `v1000`. The placeholder checks in `build_plan` have always masked; the number
    parsing did not, and the asymmetry had no reason behind it.
    """
    masked = mask_code(text, where)
    return [(release(m.group(1), m.group(2)), _line_of(text, m.start())[1])
            for m in _HEADING.finditer(masked)]


def releases_in(text: str, where: str = "") -> list[Release]:
    """Every `## vX[.Y]` heading, in file order (this repo's CHANGELOG is newest first)."""
    return [r for r, _ in release_headings(text, where)]


def duplicates_in(text: str, where: str = "") -> list[Release]:
    """Release numbers this file declares more than once, sorted.

    This is the state the placeholder convention exists to make impossible, and — once both
    branches have stamped — the only state nothing else can see. `check` looks for the
    literal `vNEXT`, and by the time two `## v2.34` headings sit side by side there is no
    placeholder left to find: a "keep both sides" resolution of the CHANGELOG conflict is a
    perfectly clean merge that ships one number describing two different releases.
    """
    found = releases_in(text, where)
    return sorted({r for r in found if found.count(r) > 1})


def next_release(text: str, major_bump: bool = False, where: str = "") -> Release:
    """One past the highest heading in `text`.

    Highest, not first. The file is newest-first and a test enforces that, but a tool that
    hands out numbers must not be the one thing that trusts the ordering it is about to
    disturb: reading position 0 would re-issue a live number the moment an entry was
    inserted a line too low, which is precisely the mistake this whole mechanism exists to
    stop being possible.

    `major_bump` is the one thing no ref can answer. Whether v2.34 or v3 comes after v2.33
    is a judgement about what the release MEANS, so it is an explicit flag and never an
    inference — but it has to exist, because this repo's own README lists `v3` as what is
    next and a tool that could not produce it would be quietly opted out of at that moment,
    by hand, which is the whole failure mode the placeholder is here to remove.
    """
    found = releases_in(text, where)
    if not found:
        raise StampError("no `## vX.Y` headings at the base ref — CHANGELOG.md is not the "
                         "file this tool thinks it is, or the ref is wrong")
    major, minor = max(found)
    return (major + 1, 0) if major_bump else (major, minor + 1)


# ------------------------------------------------------------------ markdown scanning


#: An opening or closing code fence: three or more backticks or tildes. Captured whole rather
#: than normalised to three characters, because the length is load-bearing: a four-backtick
#: fence exists precisely to wrap a three-backtick one, and a closer that matched on the first
#: three characters would end the outer block at the inner block's first line, leaving
#: everything after it unmasked and a real `## vNEXT` invisible to every check here at once.
#:
#: Indentation is not bounded to CommonMark's three spaces: this repo's command docs put
#: fenced blocks inside numbered list items, where they are legitimately indented further,
#: and reading one of those as prose is a worse error than reading a four-space indented
#: block's stray backticks as a fence.
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*(.*)$")


def mask_code(text: str, where: str = "") -> str:
    """Blank out fenced blocks and inline code spans, preserving length and newlines.

    Returned text is only ever used to LOCATE matches — every rewrite is applied to the
    original by offset — so the substitution character just has to be one no pattern here
    can match. Length preservation is what makes that safe.

    An unterminated fence is a refusal rather than a best effort. CommonMark says such a
    block runs to the end of the document, and honouring that here would blank every
    remaining line of the file: `scan`, `stamp_text` and `check` all mask through this one
    function, so a real `## vNEXT` below a stray ``` would be invisible to all three at
    once and the literal string would ship with nobody's check having failed. That is the
    largest silent blast radius in this file, and the only honest end for it is loud.
    """
    out = list(text)
    pos, fence, opened_at = 0, None, 0
    for line_no, line in enumerate(text.split("\n"), start=1):
        m = _FENCE.match(line)
        marker, info = (m.group(1), m.group(2)) if m else (None, "")
        inside = fence is not None
        if fence is None:
            if marker:
                fence, opened_at = marker, line_no
        # A closer is the same character, at least as long, and carries no info string —
        # CommonMark's rule, and what makes a longer outer fence able to contain a shorter one.
        elif marker and marker[0] == fence[0] and len(marker) >= len(fence) and not info:
            fence = None
        if inside or marker:  # the fence lines themselves are blanked too
            for i in range(pos, pos + len(line)):
                out[i] = "\0"
        pos += len(line) + 1

    if fence is not None:
        raise StampError(
            f"{where or 'this markdown'} opens a `{fence}` code fence at line {opened_at} "
            "and never closes it, so everything below it reads as code — including any "
            "release heading. Close the fence: this tool will not decide which half of a "
            "file is documentation"
        )

    masked = "".join(out)

    # Inline spans, one or more backticks. Applied after the fence pass so a stray backtick
    # inside a fenced block cannot pair with one outside it.
    #
    # A span may wrap a line — this repo wraps prose at 100 columns and a long `code span`
    # lands on a boundary sooner or later — but it may NOT cross a blank line, which is
    # CommonMark's own rule and, here, the thing that bounds the damage. An unbalanced
    # backtick with no such bound pairs with the next one anywhere in the file and blanks
    # everything between, and what it blanks is a placeholder site: the stamper would then
    # walk past a real `## vNEXT`, and `check` would agree with it, since both mask the same
    # way. That is the one failure mode here with no loud end, so it is bounded to a
    # paragraph rather than left to the file.
    def blank(m: re.Match[str]) -> str:
        return "\0" * len(m.group(0))

    return re.sub(r"(`+)(?:(?!\1)(?:[^\n]|\n(?!\s*\n)))*?\1", blank, masked)


def _line_of(text: str, offset: int) -> tuple[int, str]:
    line_no = text.count("\n", 0, offset) + 1
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    return line_no, text[start : end if end != -1 else len(text)].strip()


def scan_paths(repo: Path, paths: list[str], skipped: list[str] | None = None
               ) -> tuple[list[Site], list[Site]]:
    """(rewritable sites, loose mentions) across the given markdown files.

    Symlinks are not followed. Git tracks a symlink as its target path, so `write_text`
    through one lands wherever it points — outside the repo, if that is where it points —
    and a release stamp is not a thing to apply to a file this repo does not own. Skipped
    paths are appended to `skipped` rather than dropped: the caller reports them, because
    the failure that matters here is the quiet one.
    """
    sites: list[Site] = []
    loose: list[Site] = []
    for path in paths:
        full = repo / path
        # Symlink BEFORE exists(): `Path.exists()` follows the link and answers False for a
        # broken one, so testing it first drops a broken tracked symlink with no record at
        # all — the same quiet skip this accounting exists to prevent, one edge case along.
        if full.is_symlink():
            if skipped is not None:
                skipped.append(path)
            continue
        if not full.exists():  # deleted in the worktree but still in the index
            continue
        text = _read(full, path)
        if PLACEHOLDER not in text:
            continue
        masked = mask_code(text, path)
        taken = set()
        for m in _SITE.finditer(masked):
            line_no, line = _line_of(text, m.start(1))
            sites.append(Site(path, line_no, line))
            taken.add(m.start(1))
        for m in _MENTION.finditer(masked):
            if m.start() not in taken:
                line_no, line = _line_of(text, m.start())
                loose.append(Site(path, line_no, line))
    return sites, loose


def scan(repo: Path, skipped: list[str] | None = None) -> tuple[list[Site], list[Site]]:
    return scan_paths(repo, tracked_markdown(repo), skipped)


def stamp_text(text: str, version: str, where: str = "") -> tuple[str, int]:
    """Rewrite every placeholder site in one file's text. Returns (new text, count)."""
    masked = mask_code(text, where)
    spans = [m.span(1) for m in _SITE.finditer(masked)]
    for start, end in reversed(spans):  # right to left, so earlier offsets stay valid
        text = text[:start] + version + text[end:]
    return text, len(spans)


# ------------------------------------------------------------------------- the plan


def _served_files(repo: Path) -> tuple[Path, Path]:
    """The two files that carry the version `GET /openapi.json` reports.

    Refused if either is a symlink, for exactly the reason markdown symlinks are refused:
    git stores a symlink as its target path, so writing through one lands wherever it
    points. The markdown scan has always guarded this; these two were read and written with
    `read_text`/`write_text` and no check at all, which made `--serve` the one way to write
    outside the repo through a path the repo does not own.
    """
    main_py, pyproject = repo / "app" / "main.py", repo / "pyproject.toml"
    for path, name in ((main_py, "app/main.py"), (pyproject, "pyproject.toml")):
        if path.is_symlink():
            raise StampError(
                f"{name} is a symlink. The served version is written in place, and writing "
                "through a link puts a release stamp wherever it points — which may not be "
                "this repository. Replace it with a real file, or pass --no-serve"
            )
    return main_py, pyproject


def project_table(text: str) -> tuple[int, int] | None:
    """The half-open span of pyproject.toml's `[project]` table, or None if it has none.

    A file-wide search for `version = "X.Y.Z"` finds whatever table happens to have one.
    The package version is `[project].version` specifically, and the day `[project]` stops
    having one — reworked to a dynamic version, say — a file-wide search does not report
    that, it reports `[tool.something]`'s version instead and bumps it, successfully.
    """
    m = _PROJECT_TABLE.search(text)
    if not m:
        return None
    nxt = _TOML_TABLE.search(text, m.end())
    return m.end(), nxt.start() if nxt else len(text)


def pyproject_versions(text: str) -> list[re.Match[str]]:
    """Every `version = "X.Y.Z"` line inside `[project]`, and nowhere else."""
    span = project_table(text)
    if span is None:
        return []
    start, end = span
    return list(_PYPROJECT_VERSION.finditer(text[start:end]))


def placeholder_at_ref(repo: Path, ref: str) -> list[str]:
    """Tracked markdown at `ref` that still carries an unstamped placeholder.

    Every markdown file, not just CHANGELOG.md. The base carrying a stray `## vNEXT` in
    `harness/loops/README.md` is the same defect as carrying one in the CHANGELOG — the
    previous release landed half-stamped — and numbering on top of it hands this branch a
    number the unstamped entry is going to want. One `git grep` over the ref rather than a
    `git show` per file, then the real masking on the candidates, so a fenced example of
    the convention is not mistaken for a live placeholder.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "grep", "-I", "--name-only", "-e", PLACEHOLDER,
         ref, "--", _MARKDOWN],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode == 1:  # git grep exits 1 on no match, which is the common answer
        return []
    if proc.returncode != 0:
        raise StampError(
            f"git grep at {ref} failed: {proc.stderr.strip() or 'no output'}"
        )
    found = []
    for line in proc.stdout.split("\n"):
        if not line:
            continue
        path = line.split(":", 1)[1] if ":" in line else line
        text = _git(repo, "show", f"{ref}:{path}")
        if _SITE.search(mask_code(text, f"{ref}:{path}")):
            found.append(path)
    return sorted(found)


def _collision(branch_text: str, onto: str, onto_text: str) -> None:
    """Refuse when this branch's release number is one somebody else has already used.

    This is the failure the whole file exists to remove, arriving by the one door the
    placeholder cannot hold shut: both branches stamped before either landed. It is checked
    BEFORE the "nothing to stamp" early return, because by the time it is true there is no
    placeholder left — the branch says `## v2.34`, `apply` has nothing to rewrite, and
    without this it would print `noop:` and exit 0 on the exact state it was written to catch.

    Two shapes, because the collision surfaces differently depending on whether the branch
    has taken the merge yet:

      * duplicate headings in the branch's own CHANGELOG — the "keep both sides" resolution
        of the conflict, which is the right resolution for the prose and the wrong one for
        the number;
      * the same number at the branch and at `onto` under a different title — the branch has
        not merged yet, and the number it stamped has since been handed to someone else.

    Both repair the same way, and the message says how: put THIS branch's entry back to the
    placeholder and run `apply` again.
    """
    repair = (
        f"Put THIS branch's entry back to `## {PLACEHOLDER} — …` (and its README bullet back "
        f"to `- **{PLACEHOLDER}** — …`), then run `apply` again to take the next free number. "
        "Nothing else on the branch was written in terms of the number, which is what makes "
        "that a two-token edit rather than a rewrite."
    )

    dupes = duplicates_in(branch_text, "CHANGELOG.md")
    if dupes:
        named = ", ".join(fmt(r) for r in dupes)
        raise StampError(
            f"CHANGELOG.md declares {named} more than once. Two branches stamped the same "
            f"number and the merge kept both sides — which is correct for the prose and "
            f"wrong for the heading, since one number cannot describe two releases. {repair}"
        )

    onto_titles = dict(release_headings(onto_text, f"{onto}:CHANGELOG.md"))
    for rel, line in release_headings(branch_text, "CHANGELOG.md"):
        theirs = onto_titles.get(rel)
        if theirs is not None and theirs != line:
            raise StampError(
                f"this branch's CHANGELOG says `{line}` and {onto} says `{theirs}` — the same "
                f"release number for two different releases. Whoever landed first took "
                f"{fmt(rel)}. {repair}"
            )


def build_plan(repo: Path, onto: str, serve: bool | None, major: bool = False) -> Plan:
    plan = Plan()
    plan.sites, plan.loose = scan(repo, plan.symlinked)
    untracked_sites, untracked_loose = scan_paths(repo, untracked_markdown(repo))
    plan.untracked = untracked_sites + untracked_loose

    changelog = repo / "CHANGELOG.md"
    if not changelog.exists():
        raise StampError("no CHANGELOG.md in this repo")
    branch_text = _read(changelog, "CHANGELOG.md")
    branch_masked = mask_code(branch_text, "CHANGELOG.md")

    headings = list(_HEADING_PLACEHOLDER.finditer(branch_masked))
    if len(headings) > 1:
        lines = ", ".join(str(_line_of(branch_text, m.start())[0]) for m in headings)
        raise StampError(
            f"CHANGELOG.md has {len(headings)} `## {PLACEHOLDER}` headings (lines {lines}). "
            "A branch ships one release; two placeholders cannot both become one number, "
            "and guessing which is which is exactly the judgement this tool refuses to make"
        )

    # The ref is resolved to a SHA once, here, and every later question is asked of the SHA.
    # `git merge-base origin/main HEAD` and `git show origin/main:CHANGELOG.md` re-resolve
    # the NAME, so a push landing mid-run would have the number computed against one base
    # and the served-version inference against another, silently.
    onto_sha = resolve(repo, onto)
    onto_text = _show(repo, onto_sha, "CHANGELOG.md", onto)
    _collision(branch_text, onto, onto_text)

    if not plan.sites:
        # A noop, not a failure — and reached before the loose-mention refusal on purpose.
        # A stray `vNEXT` in running prose in some unrelated tracked doc is a defect in that
        # doc, and `check` fails on it the moment it reaches main; making it stop every
        # branch in the repo, including the ones shipping no release at all, is how a gate
        # that is right in principle gets switched off in practice. `_warn_skipped` still
        # names it, so it is skipped-and-mentioned rather than skipped-and-quiet.
        return plan

    if plan.loose:
        where = "; ".join(f"{s.path}:{s.line}" for s in plan.loose[:4])
        raise StampError(
            f"{PLACEHOLDER} appears where it will not be rewritten ({where}). A placeholder "
            "is only stamped in a heading or a bold run — put it in one, or in backticks if "
            "you meant to write ABOUT the placeholder rather than to claim a release"
        )

    if not headings:
        where = ", ".join(f"{s.path}:{s.line}" for s in plan.sites[:4])
        raise StampError(
            f"{PLACEHOLDER} is used ({where}) but CHANGELOG.md has no `## {PLACEHOLDER}` "
            "heading. Half a release entry: the number would be stamped into a README "
            "bullet pointing at a CHANGELOG section that does not exist"
        )

    first = _HEADING.search(branch_masked)
    if first and first.start() < headings[0].start():
        raise StampError(
            f"the `## {PLACEHOLDER}` heading is below "
            f"`{fmt(release(first.group(1), first.group(2)))}` in CHANGELOG.md. The file is "
            "newest first, so an unreleased entry belongs at the top — stamped where it is, "
            "it would break the ordering the whole file is read by"
        )

    unstamped_at_base = placeholder_at_ref(repo, onto_sha)
    if unstamped_at_base:
        where = ", ".join(unstamped_at_base)
        raise StampError(
            f"{onto} itself carries an unstamped `{PLACEHOLDER}` ({where}) — the previous "
            "release landed without being stamped. Fix that first; numbering on top of it "
            "would hand this branch a number the unstamped one is going to want. To repair "
            f"{onto} itself, run `apply --onto` against a ref that predates the unstamped "
            "entry — the commit it merged into, e.g. HEAD^ on the merge that brought it in"
        )
    # `next_release` first, because it is the one that refuses an unparseable file with a
    # sentence. Taking `max()` of the same list first would reach `max(())` on a CHANGELOG
    # with no headings and exit 1 on a ValueError — which a caller reading the documented
    # 0/2 scheme reads as "unknown", i.e. the one outcome this tool promises never to give.
    plan.version = next_release(onto_text, major, f"{onto}:CHANGELOG.md")
    plan.onto_newest = max(releases_in(onto_text, f"{onto}:CHANGELOG.md"))
    plan.major = major

    # Any number ABOVE the base's newest is one this branch picked for itself, whether it
    # happens to equal the number about to be handed out or not. `## v2.40` on a branch whose
    # base is at v2.33 is not a collision today and is one the week v2.40 comes round; more
    # to the point it is a branch naming its own release, which is the practice this file
    # exists to end. Only checked when there is something to stamp: a branch that has ALREADY
    # been stamped legitimately carries a number above the base, and saying so would make
    # `apply` refuse its own output.
    ahead = sorted({r for r in releases_in(branch_text, "CHANGELOG.md") if r > plan.onto_newest})
    if ahead:
        named = ", ".join(fmt(r) for r in ahead)
        raise StampError(
            f"this branch's CHANGELOG already has an entry for {named}, which does not exist "
            f"at {onto} (newest there is {fmt(plan.onto_newest)}). A branch does not pick its "
            f"own number — write `## {PLACEHOLDER}` and let this tool resolve it against the "
            "ref you are actually merging into"
        )

    changed = changed_paths(repo, onto_sha)
    board = [p for p in changed if p.startswith(BOARD_PATHS)]
    if serve is None:
        plan.serves = bool(board)
        plan.serves_reason = (
            f"inferred: {len(board)} board path(s) changed vs {onto}, first {board[0]}"
            if board
            else f"inferred: no {' or '.join(BOARD_PATHS)} path changed vs {onto}"
        )
    else:
        plan.serves = serve
        plan.serves_reason = (
            f"forced by --{'serve' if serve else 'no-serve'} "
            f"(inference said {'yes' if board else 'no'})"
        )

    if plan.serves:
        # Both version sites are validated HERE, before `apply` writes a single byte.
        # Checked at the point of writing instead, a repo whose `app/main.py` had moved its
        # literal would get its markdown stamped and then a STOP — a half-applied release,
        # which is worse than either outcome on its own and is the state hardest to notice.
        main_py, pyproject = _served_files(repo)
        if not main_py.exists():
            raise StampError(
                "app/main.py does not exist, so there is no served version to bump. Pass "
                "--no-serve if this repo does not serve one"
            )
        m = _SERVED_VERSION.search(_read(main_py, "app/main.py"))
        if not m:
            raise StampError(
                'app/main.py has no `app = FastAPI(… version="X.Y.Z" …)` to bump — the '
                "version stopped being an inline literal in that call, so this tool cannot "
                "move it and must not pretend it did"
            )
        if not pyproject.exists():
            raise StampError(
                "pyproject.toml does not exist, so the package version cannot be moved with "
                "the served one. Pass --no-serve if this repo does not have one"
            )
        pyproject_text = _read(pyproject, "pyproject.toml")
        found = pyproject_versions(pyproject_text)
        if len(found) != 1:
            table = "" if project_table(pyproject_text) else " (it has no `[project]` table)"
            raise StampError(
                f'pyproject.toml has {len(found)} `version = "X.Y.Z"` lines in `[project]`'
                f"{table}, expected exactly 1 — refusing to guess which one the package "
                "version is"
            )
        plan.served_from = m.group(1)
        plan.served_to = f"{plan.version[0]}.{plan.version[1]}.0"
    return plan


def _show(repo: Path, ref: str, path: str, named: str | None = None) -> str:
    """`ref:path`, with a sentence rather than a git error when either is missing."""
    label = named or ref
    if not _git_ok(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"):
        raise StampError(
            f"ref {label!r} does not exist here. Fetch it first — the number this tool hands "
            "out is only correct relative to the ref you are merging into"
        )
    if not _git_ok(repo, "cat-file", "-e", f"{ref}:{path}"):
        raise StampError(
            f"{label} has no {path}. The release number is read from that file at that ref "
            "and from nothing else, so there is no number to hand out — check `--onto`"
        )
    return _git(repo, "show", f"{ref}:{path}")


# ---------------------------------------------------------------------- the commands


def cmd_preflight(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    plan = build_plan(repo, args.onto, None, args.major)
    if args.json:
        print(json.dumps(plan.as_json(), indent=2))
        return 0
    _report(plan, args.onto, applied=False)
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    plan = build_plan(repo, args.onto, args.serve, args.major)
    if not plan.stamping:
        if args.json:
            # `written` even here, and empty. A consumer that has to branch on whether the
            # key exists before it can read it is a schema with two shapes, and the caller
            # discovers the second one in production.
            print(json.dumps({**plan.as_json(), "written": []}, indent=2))
        else:
            _report(plan, args.onto, applied=False)
        return 0

    version = fmt(plan.version)  # type: ignore[arg-type]

    # Every rewrite is computed BEFORE any of them is written. A release stamped into three
    # of five files is worse than one stamped into none: it reads as finished, the served
    # version disagrees with the entry above it, and the repair is a hand-edit rather than
    # a re-run. This does not survive the machine losing power mid-loop, and is not trying
    # to — what it removes is the failure that actually happens, which is the tool itself
    # refusing on file four after rewriting files one to three.
    edits: list[tuple[str, Path, str]] = []
    for path in sorted({s.path for s in plan.sites}):
        full = repo / path
        new_text, count = stamp_text(_read(full, path), version, path)
        if count:
            edits.append((path, full, new_text))

    if plan.serves:
        main_py, pyproject = _served_files(repo)
        original = _read(pyproject, "pyproject.toml")
        found = pyproject_versions(original)
        if len(found) != 1:  # build_plan already refused this; re-checked rather than assumed
            raise StampError("pyproject.toml's version line changed between plan and apply")
        offset = project_table(original)[0]  # type: ignore[index]
        m = found[0]
        at = offset + m.start()
        edits.append(("pyproject.toml", pyproject,
                      original[:at] + m.group(1) + plan.served_to + m.group(2)
                      + original[offset + m.end():]))

        text = _read(main_py, "app/main.py")
        served = _SERVED_VERSION.search(text)
        if not served:  # build_plan already refused this; re-checked rather than asserted
            raise StampError("app/main.py's version literal vanished between plan and apply")
        edits.append(("app/main.py", main_py,
                      text[: served.start(1)] + plan.served_to + text[served.end(1) :]))

    written = _write_all(edits)

    if args.json:
        print(json.dumps({**plan.as_json(), "written": written}, indent=2))
    else:
        _report(plan, args.onto, applied=True)
        print("\nwritten: " + ", ".join(written))
        print("Nothing was committed — review the diff, then commit it with the release.")
    return 0


def _write_all(edits: list[tuple[str, Path, str]]) -> list[str]:
    """Write every planned edit, or leave the tree as it was found.

    Precomputing the edits removes the failure where the TOOL refuses on file four, and it
    is the one that actually happens. It does nothing about the other one: a permission
    error or a full disk on file four leaves files one to three rewritten, which is the same
    half-applied release by a different door and arrives as a traceback rather than as the
    explicit 2 this file's contract promises. So the originals are held and put back.

    Best-effort, and said plainly rather than claimed as a transaction: if restoring a file
    also fails, the message names every path that was already written, because at that point
    the only useful thing this tool can do is tell you exactly what to look at.
    """
    originals: list[tuple[Path, str]] = []
    written: list[str] = []
    try:
        for path, full, new_text in edits:
            originals.append((full, _read(full, path)))
            _write(full, new_text, path)
            written.append(path)
    except StampError as e:
        failed = []
        for full, text in reversed(originals[: len(written)]):
            try:
                full.write_text(text, encoding="utf-8")
            except OSError:
                failed.append(str(full))
        if failed:
            raise StampError(
                f"{e} — and rolling back left {', '.join(failed)} rewritten. The release is "
                f"half stamped; `git checkout --` those paths"
            ) from e
        raise StampError(f"{e} — nothing was written; the worktree is as you left it") from e
    return written


def cmd_check(args: argparse.Namespace) -> int:
    """The guard. Run on the integration branch after a merge: did anything land unstamped?

    Separate from `preflight` because it asks a different question and needs no base ref.
    An unstamped placeholder is FINE on a feature branch — that is the whole convention —
    and is a defect the moment it is on main, so the check that fires on it cannot be one
    every branch runs.

    Two defects, not one. The placeholder is the obvious one. The other is a release number
    that appears twice, which is what a stamped collision looks like after the merge that
    kept both sides: no placeholder anywhere, nothing for a `vNEXT` scan to find, and one
    number describing two releases. That is the exact state this whole mechanism exists to
    prevent, so the guard that runs on main has to be able to see it.
    """
    repo = Path(args.repo).resolve()
    symlinked: list[str] = []
    sites, loose = scan(repo, symlinked)
    untracked_sites, untracked_loose = scan_paths(repo, untracked_markdown(repo))
    untracked = untracked_sites + untracked_loose
    bad = sites + loose

    changelog = repo / "CHANGELOG.md"
    dupes = duplicates_in(_read(changelog, "CHANGELOG.md"), "CHANGELOG.md") \
        if changelog.exists() and not changelog.is_symlink() else []

    clean = not bad and not dupes
    if args.json:
        print(json.dumps({
            "clean": clean,
            "sites": [{"path": s.path, "line": s.line, "text": s.text} for s in sites],
            "loose": [{"path": s.path, "line": s.line, "text": s.text} for s in loose],
            "duplicates": [fmt(r) for r in dupes],
            # Present whether or not anything was skipped, and present for the same reason
            # `preflight --json` carries them: a CI consumer of this command has no other
            # field to inspect, and a file dropped without a key to report it in is
            # skipped-and-quiet, which is how the literal string reaches a reader.
            "untracked": [{"path": s.path, "line": s.line, "text": s.text} for s in untracked],
            "symlinked": sorted(symlinked),
        }, indent=2))
        return 0 if clean else 2

    # Warned in text mode too, and by the same helper `preflight` and `apply` use. `check`
    # threading no accumulator was the whole bug: a tracked markdown SYMLINK was dropped
    # with no record at all, and the guard whose one job is catching the literal string
    # printed "clean" over a file it had not read.
    _warn_skipped(Plan(untracked=untracked, symlinked=sorted(symlinked)))

    if clean:
        print(f"clean: no unstamped `{PLACEHOLDER}` and no repeated release number in "
              "tracked markdown.")
        return 0
    if bad:
        print(f"STOP: {len(bad)} unstamped `{PLACEHOLDER}` placeholder(s):", file=sys.stderr)
        for s in bad:
            print(f"  {s.path}:{s.line}  {s.text}", file=sys.stderr)
        print("\nA release landed without being stamped. Re-run `release_stamp.py apply "
              "--onto <a ref that predates this entry>` — on main after a merge that is the "
              "commit the release merged into, e.g. `HEAD^`, NOT the default `origin/main`, "
              "which is the ref carrying the unstamped entry — then push the result. Until "
              "then this ref documents a version that does not exist.", file=sys.stderr)
    if dupes:
        print("STOP: release number(s) declared twice in CHANGELOG.md: "
              + ", ".join(fmt(r) for r in dupes), file=sys.stderr)
        print("\nTwo branches were stamped the same number and the merge kept both sides. "
              "One of those entries has to be renumbered: put it back to "
              f"`## {PLACEHOLDER} — …` (and its README bullet with it) and run "
              "`release_stamp.py apply`.", file=sys.stderr)
    return 2


def _warn_skipped(plan: Plan) -> None:
    if plan.untracked:
        print(f"warning: {PLACEHOLDER} in untracked markdown, which is never stamped:",
              file=sys.stderr)
        for s in plan.untracked:
            print(f"  {s.path}:{s.line}  {s.text}", file=sys.stderr)
        print("  `git add` it if it ships with the release; ignore this if it is a "
              "scratchpad.", file=sys.stderr)
    if plan.symlinked:
        print("warning: symlinked markdown, not read and not stamped: "
              + ", ".join(plan.symlinked), file=sys.stderr)


def _report(plan: Plan, onto: str, *, applied: bool) -> None:
    _warn_skipped(plan)
    if not plan.stamping:
        print(f"noop: no `{PLACEHOLDER}` placeholder in this worktree — nothing to stamp.")
        # A loose mention does not stop a branch that ships no release (that would make one
        # stray word in one tracked doc refuse every branch in the repo), but it is still a
        # defect and `check` will fail on it the moment it lands, so it is said here.
        for s in plan.loose:
            print(f"warning: {PLACEHOLDER} in prose, where nothing would rewrite it: "
                  f"{s.path}:{s.line}  {s.text}", file=sys.stderr)
        return
    verb = "stamped" if applied else "would stamp"
    how = " (--major)" if plan.major else ""
    print(f"{verb} {fmt(plan.version)}{how}  (newest at {onto}: {fmt(plan.onto_newest)})")
    for s in plan.sites:
        print(f"  {s.path}:{s.line}  {s.text}")
    if plan.serves:
        print(f"\nserved version {plan.served_from} -> {plan.served_to}")
    else:
        print("\nserved version unchanged")
    print(f"  {plan.serves_reason}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--repo", default=".", help="repo dir (default: cwd)")
        sp.add_argument("--json", action="store_true", help="machine-readable plan")

    def against_a_ref(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--onto", default="origin/main", help="the ref you are merging into")
        # Explicit and never inferred. Whether v2.34 or v3 follows v2.33 is a statement about
        # what the release MEANS, and no ref can answer it — but the flag has to exist, or
        # the next major is hand-written outside this mechanism and the placeholder
        # convention is quietly opted out of on the one release that most needs it.
        sp.add_argument("--major", action="store_true",
                        help="stamp the next MAJOR (v2.33 -> v3), not the next minor")

    pf = sub.add_parser("preflight", help="report what would be stamped (read-only)")
    common(pf)
    against_a_ref(pf)
    pf.set_defaults(func=cmd_preflight)

    ap = sub.add_parser("apply", help="stamp the worktree (never commits)")
    common(ap)
    against_a_ref(ap)
    serve = ap.add_mutually_exclusive_group()
    serve.add_argument("--serve", dest="serve", action="store_true", default=None,
                       help="bump the served version even if no board path changed")
    serve.add_argument("--no-serve", dest="serve", action="store_false",
                       help="leave the served version alone even though a board path changed")
    ap.set_defaults(func=cmd_apply)

    ck = sub.add_parser("check", help="fail if an unstamped placeholder is present")
    common(ck)
    ck.set_defaults(func=cmd_check)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except StampError as e:
        # Exit 2, not Python's uncaught-exception 1: a gate consuming the documented 0/2
        # scheme reads 1 as "unknown" rather than as "stop".
        print(f"STOP: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
