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

The number is `max(release headings at --onto) + 1`. It is computed from the CHANGELOG at a
git ref and from nothing else — not from a live board, not from the local checkout's own
history.

`--major` is the one part no ref can answer. Whether v3 or v2.34 follows v2.33 is a statement
about what the release MEANS, so it is an explicit flag and never an inference: `apply
--major` stamps `(major + 1).0` — v2.33 becomes v3, and the served version becomes 3.0.0. The
flag has to exist, or the next major gets hand-written outside this mechanism and the
placeholder convention is quietly opted out of on the one release that most needs it.

## When two branches stamp the same number

They can, and the recovery is deliberately manual and deliberately one edit long. Once
`apply` has run, the placeholder is GONE — the branch says `## v2.34`, and re-running
`apply` has nothing left to rewrite. There is no automatic re-stamp and this file does not
pretend otherwise; what it does instead is make the collision impossible to miss:

  * both branches carry `## v2.34`, the merge conflicts on the CHANGELOG, you keep both
    sides, and `preflight`/`apply`/`check` all refuse on the duplicate heading;
  * or you have not merged yet, and `preflight`/`apply` refuse because a number this branch
    ADDED — one its fork point did not have — already exists at `--onto`. Asked that way
    round, editing a released entry's title is not a collision and a shared boilerplate
    title is not a free pass; `check` cannot ask it at all, because it deliberately takes no
    base ref, so `check` sees the duplicate-heading shape and only that one.

Either way the repair is: put YOUR entry back to `## vNEXT` (and its README bullet back to
`- **vNEXT** — …`), then run `apply` again. Two tokens, because nothing else in the branch
was ever written in terms of the number — that is what "cheap to redo" actually buys, and
it is worth more than an unstamp command that would have to guess which of two identical
headings belongs to you.

**This is not an allocator, and since #172 there is no other one.** #46/#99's
`POST /release/claim` recorded that a caller INTENDED to take a number: an announcement,
not a reservation, which this tool never read and never honoured. A branch holding a
claim for v2.34 got no protection here — the next `apply` on any branch stamped v2.34
too, because a stamped number is only ever "the next one free at the ref I merged into",
which is a question a git ref answers on its own.

So the allocator is deleted: `POST /release/claim`, `POST /release/reclaim`,
`GET /releases`, their MCP tools and the `kind='release'` claim underneath them are all
gone, and this file is the whole mechanism. A namespace nobody claims in does not need
an allocator, and the rows it did have — one going stale for every PR still open — were
a second answer to a question that has one. There is nothing to announce and nothing to
opt into: write the placeholder, and `apply` at land.

## What counts as a placeholder

Only tracked **markdown**, and only where a release is NAMED:

  * a heading of any level — `## vNEXT — …`, `#### vNEXT — …`
  * a bold run, in all three of markdown's spellings for it — `**vNEXT**`, `__vNEXT__`,
    `***vNEXT***` — with anything or nothing after the token: `**vNEXT.**` and
    `- **vNEXT — …**` are both legal. An author who writes the underscore spelling has
    written a real placeholder, not a mistake, and refusing it would stop a correct branch
    over a punctuation preference.

`*vNEXT*` (single asterisk) is EMPHASIS, not a bold run, and is a refusal rather than a site:
it does not read as a release entry, and one character is too small a difference to guess
across. `**vNEXT.1**` is a refusal too — three-component versions are not something this tool
knows how to number, so it will not stamp half of one and leave the `.1` behind.

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
import shlex
import subprocess
import sys
import tomllib
from collections import Counter
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

#: `- **v2.33** — …` in the README's release list. The bold run has to CLOSE immediately after
#: the number, which is what keeps the roadmap-style `- **v3 (next)** —` and range entries like
#: `- **v1 to v2.1** —` out: they are not a release the tool stamped, and reading them as one
#: would report a duplicate against an entry that has not shipped. `check` scans these because
#: the bullets are stamped independently of the headings — a merge that kept both sides of the
#: README list and only one side of the CHANGELOG is a duplicate nothing else could see.
_README_BULLET = re.compile(rf"^-[ \t]+\*\*{_V}\*\*", re.MULTILINE)

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

#: Any mention at all, for the "you wrote it somewhere I will not rewrite" check.
#:
#: NOT bounded identically to `_SITE`, and the difference is load-bearing rather than an
#: oversight: `_SITE_END` also rejects `.\d`, this one does not. That asymmetry is the whole
#: reason `**vNEXT.1**` is a LOOSE MENTION — a placeholder written in a shape nothing will
#: rewrite, which is a refusal with a line number — instead of a token neither pattern can
#: see, which would ship the literal string. Making the two patterns agree would delete that
#: refusal silently, so do not "fix" this into symmetry.
_MENTION = re.compile(rf"(?<![0-9A-Za-z]){PLACEHOLDER}(?![0-9A-Za-z])")

#: `version = "2.33.0"` or `version = '2.33.0'`, keeping whatever trails it (there is a
#: comment). TOML gives basic and literal strings equal standing, and a file using the
#: single-quoted spelling was invisible here — reported as "0 version lines" about a file
#: whose version is present and correct. The closing quote is a backreference, so the two
#: spellings cannot be mixed. Scoped by the caller to pyproject.toml's `[project]` table —
#: see `project_table`, which explains why a file-wide search is the wrong thing.
_PYPROJECT_VERSION = re.compile(
    r"(?m)^(version[ \t]*=[ \t]*(?P<q>[\"']))(?P<v>\d+\.\d+\.\d+)(?P=q)"
)

#: The header line of any TOML table, used to bound `[project]`. A bare `^[ \t]*\[` also
#: matches a continuation line of a wrapped array whose element starts with `[`, which would
#: end the `[project]` span early and report "0 version lines" about a correct file — so the
#: whole header shape is required: brackets that close, on a line with nothing after them but
#: an optional comment. `pyproject_versions` cross-checks the answer against `tomllib`
#: anyway, because a regex over raw text cannot see that a `[project]`-looking line is really
#: the inside of a multi-line string.
_TOML_KEY = r"(?:[A-Za-z0-9_-]+|\"[^\"\n]*\"|'[^'\n]*')"
_TOML_TABLE = re.compile(
    rf"(?m)^[ \t]*\[\[?[ \t]*{_TOML_KEY}(?:[ \t]*\.[ \t]*{_TOML_KEY})*[ \t]*\]\]?"
    r"[ \t]*(?:#.*)?$"
)
_PROJECT_TABLE = re.compile(r"(?m)^[ \t]*\[project\][ \t]*(?:#.*)?$")

#: One Python string literal, every spelling, as a single ATOM. Escapes are honoured: a
#: `description="… version=\"8.8.8\" …"` whose atom ended at the escaped quote would let the
#: text inside the literal read as a real keyword argument, and `--serve` would rewrite prose
#: while leaving the served version where it was — and report success.
_STR = (
    r'"""(?:\\[\s\S]|(?!""")[^\\])*"""'
    r"|'''(?:\\[\s\S]|(?!''')[^\\])*'''"
    r'|"(?:\\[\s\S]|[^"\\\n])*"'
    r"|'(?:\\[\s\S]|[^'\\\n])*'"
)

#: One argument of a call, with string literals and one level of parens treated as ATOMS.
#: That is what keeps a comma — or the literal text `version="1.0.0"` — inside a `title=` or
#: `description=` string from reading as a real keyword argument. A `[^()]`-style scan cannot
#: tell the two apart and would bump a version buried in a docstring while reporting success.
#:
#: Newlines are NOT excluded. Excluding them made the atom unable to cross a line, which broke
#: the canonical Black/Ruff-formatted call — `FastAPI(\n    title=…,\n    version="X.Y.Z",\n)`
#: — outright: the tool refused a file whose version literal was plainly there. Quoted strings
#: being atoms is what makes the scan safe; the line boundary was never doing that work.
_ARG = rf"(?:{_STR}|\((?:{_STR}|[^()])*\)|[^()\"'])"

#: `app = FastAPI(… version="2.33.0" …)`. Bounded to that call's own parentheses, tolerating
#: one level of nesting and any amount of line-wrapping — the same shape (and the same
#: reasoning) as the fixture in harness/tests/test_release_numbers.py, which explains at
#: length why a lazy `.*?` version of this silently latches onto the next version literal in
#: the file. `version` must sit at the start of the call or immediately after a comma, so it
#: is the keyword argument and not a substring of an earlier one; the `\s*` after the opening
#: paren is what lets "the start of the call" be the next LINE, which is where a formatter
#: puts it.
_SERVED_VERSION = re.compile(
    rf'(?m)^app[ \t]*=[ \t]*FastAPI\(\s*(?:{_ARG}*?,\s*)?version[ \t]*=[ \t]*"(\d+\.\d+\.\d+)"'
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
    unreadable: list[str] = field(default_factory=list)
    serves: bool = False
    serves_reason: str = ""
    served_from: str = ""
    served_to: str = ""
    onto_newest: Release | None = None
    major: bool = False
    #: Things a caller should know that are not this branch's to fix, and must not
    #: stop it. A broken base is the one that matters (#168): it is a refusal for a
    #: branch that needs a number and noise for one that does not, and refusing both
    #: is how one skipped stamp took out every branch in the repo at once.
    warnings: list[str] = field(default_factory=list)

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
            "unreadable": self.unreadable,
            "warnings": self.warnings,
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


def _linked(repo: Path, rel: str) -> bool:
    """True if `rel` — or any directory between it and the repo root — is a symlink.

    Checking only the leaf was the whole guard for a while, and it does not hold: a symlinked
    `app/` or `docs/` passes a leaf-only test and every read and write below it lands wherever
    the link points, which is the exact escape the leaf check exists to prevent. The repo root
    itself is `Path.resolve()`d by the commands, so the walk starts inside the repository.
    """
    cur = repo
    for part in Path(rel).parts:
        cur = cur / part
        if cur.is_symlink():
            return True
    return False


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


def merge_base(repo: Path, onto: str) -> str:
    """The commit this branch forked from, as a SHA.

    Run through a raw `subprocess` rather than `_git`, because `git merge-base` exits 1 when
    there is no common ancestor and `_git` turns any non-zero exit into "git merge-base failed:
    <stderr>" — which is empty in that case. The sentence below is the one a reader can act
    on, and routing through `_git` made it unreachable.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "merge-base", onto, "HEAD"],
        capture_output=True, text=True, check=False,
    )
    out = proc.stdout.split()
    if proc.returncode != 0 or not out:
        raise StampError(
            f"{onto} and HEAD have no common ancestor, so there is no base to compute this "
            "branch's changes against. Fetch the ref you are actually merging into"
        )
    return out[0]


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

    `merge_base` takes the first token for a reason worth knowing here: `merge-base` without
    `--all` prints exactly one SHA even for a criss-cross history with several best common
    ancestors — git picks one rather than listing them — so there is no multi-line case, and
    the guard is there because a two-line string handed to `git diff` surfaces as
    `fatal: ambiguous argument` rather than as anything readable.
    """
    base = merge_base(repo, onto)
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


def entry_names(text: str, where: str = "") -> list[str]:
    """Every release ENTRY heading in file order, spelled the way the file spells it.

    Numbered entries and the unstamped one in the same list, because a reader that wants the
    CHANGELOG's order — `scripts/readme_releases.py`, which renders the README's release list
    from it — wants the in-flight entry in its place at the top rather than as a separate
    question asked of a second scan. Merging two scans at the call site would put the answer
    to "which entry comes first" in the caller, and there are now two callers.

    Exported for that caller rather than used here: everything in this file numbers releases,
    and a placeholder has no number. Kept beside `releases_in` all the same, so the answer to
    "what is a release heading" stays in one place — the reason this repo keeps repeating.
    """
    masked = mask_code(text, where)
    found = [(m.start(), fmt(release(m.group(1), m.group(2)))) for m in _HEADING.finditer(masked)]
    found += [(m.start(), PLACEHOLDER) for m in _HEADING_PLACEHOLDER.finditer(masked)]
    return [name for _, name in sorted(found)]


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


def bullet_releases(text: str, where: str = "") -> list[Release]:
    """Every `- **vX[.Y]**` release bullet in the README's list, in file order."""
    masked = mask_code(text, where)
    return [release(m.group(1), m.group(2)) for m in _README_BULLET.finditer(masked)]


def duplicates_by_file(repo: Path) -> dict[str, list[Release]]:
    """Repeated release numbers, per file, across both places a release is declared.

    CHANGELOG.md headings AND README.md bullets, because the two are stamped independently
    and a "keep both sides" merge can leave the duplicate in either. Checking only the
    CHANGELOG meant two identical README bullets with one clean heading printed `clean: true`
    from the guard whose entire purpose is catching that merge — while the repo's own
    invariant suite treated both files as carrying release numbers.
    """
    out: dict[str, list[Release]] = {}
    for path, parse in (("CHANGELOG.md", releases_in), ("README.md", bullet_releases)):
        full = repo / path
        if _linked(repo, path) or not full.exists():
            continue
        found = parse(_read(full, path), path)
        dupes = sorted({r for r in found if found.count(r) > 1})
        if dupes:
            out[path] = dupes
    return out


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
#: block's stray backticks as a fence. The corollary, and it is a real limitation: an
#: INDENTED code block (four spaces, no fence) is not masked, so a `**vNEXT**` inside one is
#: a rewritable site. Masking those instead would unstamp every nested list bullet in this
#: repo's docs, which is the larger error — write fenced blocks, not indented ones.
#:
#: A fence may also carry a container prefix: `> ``` ` inside a blockquote, or `- ``` ` as the
#: first line of a list item. Both are ordinary markdown, and a masker that did not see them
#: read the block's contents as prose — so a fenced EXAMPLE of a release heading, quoted in a
#: review comment, would have been stamped as a live placeholder.
_FENCE = re.compile(
    r"^(?P<prefix>[ \t]*(?:>[ \t]*)*(?:[-*+][ \t]+|\d+[.)][ \t]+)?)"
    r"(?P<marker>`{3,}|~{3,})[ \t]*(?P<info>.*)$"
)


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
    pos, fence, quote, opened_at = 0, None, 0, 0
    for line_no, line in enumerate(text.split("\n"), start=1):
        m = _FENCE.match(line)
        marker = m.group("marker") if m else None
        info = m.group("info") if m else ""
        depth = m.group("prefix").count(">") if m else 0
        inside = fence is not None
        if fence is None:
            if marker:
                fence, quote, opened_at = marker, depth, line_no
        # A closer is the same character, at least as long, carries no info string, and sits
        # at the same blockquote depth — CommonMark's rules, and between them what makes a
        # longer outer fence able to contain a shorter one and a quoted example unable to
        # close the block that is quoting it.
        elif (marker and marker[0] == fence[0] and len(marker) >= len(fence)
              and not info and depth == quote):
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


def scan_paths(repo: Path, paths: list[str], skipped: list[str] | None = None,
               unreadable: list[str] | None = None) -> tuple[list[Site], list[Site]]:
    """(rewritable sites, loose mentions) across the given markdown files.

    Symlinks are not followed, and neither is a symlinked PARENT. Git tracks a symlink as its
    target path, so `write_text` through one lands wherever it points — outside the repo, if
    that is where it points — and a release stamp is not a thing to apply to a file this repo
    does not own. Skipped paths are appended to `skipped` rather than dropped: the caller
    reports them, because the failure that matters here is the quiet one.

    `unreadable`, when given, downgrades a per-file refusal (not UTF-8, unterminated fence) to
    a recorded skip. Tracked markdown passes None and keeps the refusal, because a tracked
    file in that state stops the release. UNtracked markdown passes a list, because the module
    contract says untracked markdown is never a STOP — a scratchpad with a stray byte in it
    must not be able to refuse every branch in the repo.
    """
    sites: list[Site] = []
    loose: list[Site] = []
    for path in paths:
        full = repo / path
        # Symlink BEFORE exists(): `Path.exists()` follows the link and answers False for a
        # broken one, so testing it first drops a broken tracked symlink with no record at
        # all — the same quiet skip this accounting exists to prevent, one edge case along.
        if _linked(repo, path):
            if skipped is not None:
                skipped.append(path)
            continue
        if not full.exists():  # deleted in the worktree but still in the index
            continue
        try:
            text = _read(full, path)
            if PLACEHOLDER not in text:
                continue
            masked = mask_code(text, path)
        except StampError as e:
            if unreadable is None:
                raise
            unreadable.append(f"{path}: {e}")
            continue
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


def scan_untracked(repo: Path, skipped: list[str], unreadable: list[str]) -> list[Site]:
    """Untracked markdown, reported and never stamped — and never a STOP, either.

    One helper because `build_plan` and `cmd_check` both do this and drifted apart: `check`
    threading no accumulator was how a symlinked untracked file vanished from both commands'
    output at once, which is the skipped-and-quiet outcome the whole accounting exists to end.
    """
    sites, loose = scan_paths(repo, untracked_markdown(repo), skipped, unreadable)
    return sites + loose


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

    Refused if either is a symlink, or sits under one, for exactly the reason markdown
    symlinks are refused: git stores a symlink as its target path, so writing through one
    lands wherever it points. The markdown scan has always guarded this; these two were read
    and written with `read_text`/`write_text` and no check at all, which made `--serve` the
    one way to write outside the repo through a path the repo does not own. A symlinked
    `app/` gets there just as well as a symlinked `app/main.py`, so the whole path is walked.
    """
    for name in ("app/main.py", "pyproject.toml"):
        if _linked(repo, name):
            raise StampError(
                f"{name} is a symlink, or sits under one. The served version is written in "
                "place, and writing through a link puts a release stamp wherever it points — "
                "which may not be this repository. Replace it with a real file, or pass "
                "--no-serve"
            )
    return repo / "app" / "main.py", repo / "pyproject.toml"


def project_table(text: str) -> tuple[int, int] | None:
    """The half-open span of pyproject.toml's `[project]` table, or None if it has none.

    A file-wide search for `version = "X.Y.Z"` finds whatever table happens to have one.
    The package version is `[project].version` specifically, and the day `[project]` stops
    having one — reworked to a dynamic version, say — a file-wide search does not report
    that, it reports `[tool.something]`'s version instead and bumps it, successfully.

    A span, not a value, because the rewrite is applied by byte offset to the original text —
    which is what keeps comments and formatting exactly as they were. `declared_version` is
    the one that answers what the version IS, and `pyproject_versions` makes the two agree.
    """
    m = _PROJECT_TABLE.search(text)
    if not m:
        return None
    nxt = _TOML_TABLE.search(text, m.end())
    return m.end(), nxt.start() if nxt else len(text)


def declared_version(text: str) -> str | None:
    """`[project].version` as a real TOML parser reads it, or None if there is not one.

    The line matching above is a regex over raw text and cannot see TOML's own structure: a
    multi-line string containing lines that look like `[project]` and `version = "9.9.9"` is
    indistinguishable from the real table, so the tool could rewrite prose inside a string
    literal and report success. Parsing settles it. The regex still does the LOCATING, because
    `tomllib` gives values and not offsets and a rewrite that reformatted the file would be a
    worse tool; this exists so the two answers can be required to match.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise StampError(
            f"pyproject.toml is not valid TOML ({e}). The served version is written into it, "
            "and this tool will not rewrite a file it cannot parse"
        ) from e
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    version = project.get("version")
    return version if isinstance(version, str) else None


def pyproject_versions(text: str) -> list[re.Match[str]]:
    """Every `version = "X.Y.Z"` line inside `[project]`, and nowhere else.

    Cross-checked against `declared_version`: if the one line matched is not the value TOML
    resolves `[project].version` to, then the line found is not the package version — the
    span was bounded by something that only looks like a table header, or the text is inside
    a multi-line string — and rewriting it would move the wrong number while reporting
    success. That is a refusal with a sentence, not a silent zero.
    """
    span = project_table(text)
    if span is None:
        return []
    start, end = span
    found = list(_PYPROJECT_VERSION.finditer(text[start:end]))
    try:
        declared = declared_version(text)
    except StampError:
        # Not parseable at all. When one line matched, the parse error IS the answer; when
        # the count is already wrong, "expected exactly 1" names the thing to look at — two
        # `version =` keys in one table is itself the parse error, said more usefully.
        if len(found) == 1:
            raise
        return found
    if declared is None:
        return found  # `[project]` with no version — the caller's message says exactly that
    if len(found) != 1 or found[0].group("v") != declared:
        detail = (f"the line matched inside it says {found[0].group('v')!r}"
                  if len(found) == 1 else f"{len(found)} lines there match")
        raise StampError(
            f"pyproject.toml's `[project]` table resolves to version {declared!r}, but "
            f"{detail}. This tool rewrites that line by byte offset and will not guess which "
            "text is the package version — look for a multi-line string, a line that only "
            "looks like a table header, or a version that is not three components"
        )
    return found


def placeholder_at_ref(repo: Path, ref: str) -> list[str]:
    """Tracked markdown at `ref` that still carries an unstamped placeholder.

    Every markdown file, not just CHANGELOG.md. The base carrying a stray `## vNEXT` in
    `harness/loops/README.md` is the same defect as carrying one in the CHANGELOG — the
    previous release landed half-stamped — and numbering on top of it hands this branch a
    number the unstamped entry is going to want. One `git grep` over the ref rather than a
    `git show` per file, then the real masking on the candidates, so a fenced example of
    the convention is not mistaken for a live placeholder.

    `-z`, like every other path-reading git call in this file. Newline-separated output splits
    a legal path containing a newline into two, and both halves then go to `git show` — which
    fails with a generic git error instead of the readable sentence this function exists to
    produce. The `ref:path` prefix survives `-z` (only the RECORD separator becomes NUL), so
    the split on the first colon still holds.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "grep", "-I", "-z", "--name-only", "-e", PLACEHOLDER,
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
    for line in proc.stdout.split("\0"):
        if not line:
            continue
        path = line.split(":", 1)[1] if ":" in line else line
        text = _git(repo, "show", f"{ref}:{path}")
        if _SITE.search(mask_code(text, f"{ref}:{path}")):
            found.append(path)
    return sorted(found)


#: How far back the repair walk looks for a tree with no placeholder in it, counting the ref
#: it starts from. Bounded because of the case it is actually for: an unstamped entry that
#: landed a long time ago and was never repaired leaves a RUN of consecutive ancestors all
#: carrying it, and each one costs a `git grep` to rule out. A history that predates the
#: placeholder scheme is the cheap case, not the expensive one — the first commit the walk
#: looks at has no placeholder and it stops there.
_REPAIR_WALK_MAX = 50


def _cmd(repo: Path, sub: str, *rest: str) -> str:
    """One invocation of this tool, spelled so it can be pasted into any shell.

    Absolute on BOTH halves — the interpreter's argument and `--repo` — because nothing here
    knows the cwd of the shell that will run it. `harness/commands/fix-and-review.md` runs
    this tool as `python3 "$WT_DIR/scripts/release_stamp.py" preflight --repo "$WT_DIR"`
    precisely so it operates on the worktree under review rather than on the caller's cwd,
    and a repair command that dropped both halves would run against whatever that shell
    happens to be sitting in: silently the wrong checkout, or a bare filesystem error where
    it is not a repository at all. A cwd-relative `scripts/release_stamp.py` is also on
    nobody's PATH, and a resolved ref is only worth resolving if the whole line can be pasted.

    `--repo` goes straight after the subcommand because that is where argparse accepts it:
    it is declared on each subparser, so `… --repo DIR apply` is a usage error rather than a
    repair. Both paths go through `shlex.quote`, since a checkout under a directory with a
    space in it is otherwise a command that parses as two arguments.
    """
    script = shlex.quote(str(Path(__file__).resolve()))
    return " ".join(["python3", script, sub, "--repo", shlex.quote(str(repo)), *rest])


def _releases_at(repo: Path, ref: str) -> set[Release] | None:
    """CHANGELOG.md's release numbers at `ref`, or None when they cannot be read there.

    None is "cannot tell" and never "none": no CHANGELOG.md at that commit, or one this tool
    will not parse. Every caller is composing advice or excusing a number rather than
    deciding a release, so a ref it cannot read is a question it declines rather than a stop.
    """
    if not _git_ok(repo, "cat-file", "-e", f"{ref}:CHANGELOG.md"):
        return None
    try:
        return set(releases_in(_git(repo, "show", f"{ref}:CHANGELOG.md"),
                               f"{ref}:CHANGELOG.md"))
    except StampError:
        return None


def _clean_ancestor(repo: Path, onto_sha: str) -> tuple[str | None, int]:
    """The newest first-parent commit at or before `onto_sha` with no placeholder in its tree.

    Returns `(sha, in_reach)` — the second being how many commits of first-parent history the
    walk could see at all, which is not decoration: with nothing found, "the walk ran out of
    BUDGET" and "the walk ran out of HISTORY" want opposite advice, and a bare `None` cannot
    tell them apart. Out of budget means an unstamped entry that landed more
    than `_REPAIR_WALK_MAX` commits ago and there is an older clean commit to point at. Out of
    history means there is not — a placeholder in the root commit, or, far more often, a
    shallow clone: `.github/workflows/tests.yml` runs `check` from a bare `actions/checkout@v4`,
    which is a depth-1 clone, where this `git log` returns exactly one grafted SHA no matter
    what the real history holds. Telling that caller the entry "has been there longer than
    fifty commits" is a statement about a history it does not have.

    A history exactly `_REPAIR_WALK_MAX` long with nothing clean in it reads as "out of
    budget", which is one `git log` cheaper than distinguishing it and is not wrong by much:
    "the entry has been there longer than fifty commits" is true of a fifty-commit history
    whose root already carries it.

    First parent, because that is the side a merge came FROM: on `main` the first parent is
    the previous `main`, so the walk crosses releases rather than descending into the branch
    that made one. `onto_sha` ITSELF is a candidate — `check` calls this with a HEAD whose
    placeholder is still uncommitted, and the ref to stamp against there is HEAD, not HEAD^.

    One `git log` for the candidates rather than a `rev-parse` per step. The walk is bounded
    at `_REPAIR_WALK_MAX`, and spending fifty subprocesses to enumerate what a single call
    already knows is fifty too many on a path whose only output is one line of advice.
    """
    log = _git(repo, "log", "--first-parent", "--format=%H", f"-n{_REPAIR_WALK_MAX}", onto_sha)
    shas = log.split()
    for sha in shas:
        if not placeholder_at_ref(repo, sha):
            return sha, len(shas)
    return None, len(shas)


def _is_shallow(repo: Path) -> bool:
    """Whether this checkout is a shallow clone, for advice that would otherwise be a lie.

    Best-effort, like everything else the advice path calls: a git too old to know the
    question is answered "no" rather than allowed to turn a message into an exit 2.
    """
    try:
        return _git(repo, "rev-parse", "--is-shallow-repository").strip() == "true"
    except StampError:
        return False


def _repair_advice(repo: Path, onto: str, onto_sha: str | None = None) -> str:
    """How to repair an `onto` carrying an unstamped placeholder, as a sentence to print.

    The message this replaces described how to find the ref — *"a ref that predates the
    unstamped entry — the commit it merged into, e.g. HEAD^ on the merge that brought it in"*
    — which is correct and is the opposite of every other invocation of this tool, where
    `--onto` is `origin/main` and `origin/main` is the thing that is broken. #168's reading is
    that somebody hitting this under time pressure reaches for the usual command, gets the
    same refusal, and concludes the tool is stuck. So the ref is resolved and the command
    printed ready to run.

    NEVER RAISES, and never returns a command-shaped string it could not resolve. Both
    matter, and for the same reason: this composes ADVICE, on a path that includes the one
    branch that must not be stopped — the one shipping no release, which is warned and
    carries on. A `git grep` against a shallow clone, an object missing from an old ancestor,
    a `git log` that fails for any reason at all: none of that is this branch's problem, so
    the walk degrades to the prose it replaces rather than turning a noop into an exit 2.
    And a `<placeholder>` inside a printed command is worse than prose, not better — pasted
    into a shell, `<a ref…>` redirects input from a file named `a` and the promised
    ready-to-run repair fails with a filesystem error.

    An empty walk is THREE outcomes wearing one sentence, and two of them were being told
    something false: out of BUDGET (an older clean commit exists, the bound just did not reach
    it), out of the history THIS CLONE HAS (a shallow checkout — which is what
    `actions/checkout@v4` hands the only automated caller of `check` — where the repair is a
    fetch and no local ref can be named), and out of history full stop (the root commit
    carries it, and there has never been a clean tree here to stamp against).
    """
    try:
        sha = onto_sha or resolve(repo, onto)
        ref, in_reach = _clean_ancestor(repo, sha)
        if ref is None and in_reach >= _REPAIR_WALK_MAX:
            return (
                f"No commit within {_REPAIR_WALK_MAX} first-parent commits of {onto} has a "
                f"tree without a `{PLACEHOLDER}` in it, so there is no ref to name here — the "
                f"unstamped entry has been there longer than that. Stamp it by hand, or point "
                f"`{_cmd(repo, 'apply', '--onto')}` at an older commit whose tree is clean."
            )
        if ref is None and _is_shallow(repo):
            # The one automated caller of `check` is this case. `.github/workflows/tests.yml`
            # checks out with a bare `actions/checkout@v4`, which is depth 1, so the walk sees
            # a single grafted commit and finds a placeholder in it — measured, not inferred.
            # "It has been there longer than fifty commits" is then a claim about a history
            # this clone does not have, and "point apply at an older commit" names refs it
            # cannot resolve. What it can act on is the fetch.
            return (
                f"Every commit this clone has of {onto}'s first-parent history — {in_reach} "
                f"of them — carries a `{PLACEHOLDER}`, and this is a shallow clone, so the "
                "commit that would repair it is almost certainly one that was never fetched. "
                "Deepen the checkout (`git fetch --unshallow`, or `fetch-depth: 0` on "
                "`actions/checkout`) and run this again; there is no ref to name until then."
            )
        if ref is None:
            return (
                f"{onto}'s first-parent history is {in_reach} commit(s) long and every one of "
                f"them carries a `{PLACEHOLDER}`, back to the root commit — so there is no ref "
                "to name here, and never was one. Stamp the entry by hand: this history has "
                "no commit with a clean tree to resolve a number against."
            )
        advice = f"To repair {onto}, run:\n    {_cmd(repo, 'apply', '--onto', ref)}"
        # What to expect from that command, when it is not simply going to work. A release
        # that landed AFTER the skipped stamp is above the clean ancestor's newest, so the
        # branch being repaired holds a number that ref has never issued — and with a
        # placeholder still present that is refused, differently worded, by the command
        # handed over to fix the first refusal. Said here rather than discovered there.
        #
        # And said WITHOUT the repair it used to suggest. "Put those entries back to
        # `## vNEXT` first" was reachable only because `onto` already carries one, so
        # following it leaves two placeholders in one CHANGELOG — which `build_plan` refuses
        # in a third differently-worded way ("two placeholders cannot both become one
        # number"). The advice written to stop somebody concluding the tool is stuck has to
        # end at an edit that works, and here that edit is a hand-stamp: the entry needs a
        # number nothing else has taken, and the newest at `onto` plus one is free by
        # construction.
        at_onto = _releases_at(repo, sha) or set()
        gained = sorted(at_onto - (_releases_at(repo, ref) or set()))
        if gained:
            named = ", ".join(fmt(r) for r in gained)
            free = (max(at_onto)[0], max(at_onto)[1] + 1)
            advice += (
                f"\n{onto} has gained {named} since that commit, so that run stops on those "
                f"instead: they are numbers {ref[:12]} never issued, which is the same "
                f"refusal one step along. Putting them back to `## {PLACEHOLDER}` is not the "
                f"way round it — {onto} already carries one, and two placeholders in one "
                "CHANGELOG cannot both become one number. Stamp the unstamped entry by hand "
                f"instead; {fmt(free)} is free at {onto}."
            )
        return advice
    except StampError:
        # Not resolvable, not walkable: the prose this replaces, which needs no git at all.
        return (
            f"To repair {onto}, run `{_cmd(repo, 'apply', '--onto')}` against a ref that "
            f"predates the unstamped entry — the commit it merged into, e.g. `HEAD^` on the "
            f"merge that brought it in, NOT the default `origin/main`, which is the ref "
            "carrying it."
        )


def _collision(repo: Path, branch_text: str, onto: str, onto_text: str) -> None:
    """Refuse when this branch's release number is one somebody else has already used.

    This is the failure the whole file exists to remove, arriving by the one door the
    placeholder cannot hold shut: both branches stamped before either landed. It is checked
    BEFORE the "nothing to stamp" early return, because by the time it is true there is no
    placeholder left — the branch says `## v2.34`, `apply` has nothing to rewrite, and
    without this it would print `noop:` and exit 0 on the exact state it was written to catch.

    Three shapes, because the collision surfaces differently depending on whether the branch
    has taken the merge yet, and because the base can already be in the state too:

      * duplicate headings in the branch's own CHANGELOG — the "keep both sides" resolution
        of the conflict, which is the right resolution for the prose and the wrong one for
        the number;
      * the same duplicate already present at `onto` — the branch inherited a bad merge and
        would compound it by stamping on top; the repair is on the base, not here;
      * a release number this BRANCH ADDED which also exists at `onto` — the branch has not
        merged yet, and the number it stamped has since been handed to someone else.

    "Added by this branch" is the load-bearing part of the third, and it is answered by the
    merge base rather than by comparing heading TEXT. Text equality gets it wrong in both
    directions: two genuinely different releases that happen to share a boilerplate title read
    as no collision at all, while fixing a typo in a released entry's title — rewrapping it,
    normalising an em dash — reads as one, with a repair message ("put your entry back to
    `## vNEXT`") that is nonsense for an entry that shipped a year ago. What the merge base
    answers is the actual question: is this number one this branch is claiming, or one it
    inherited and is merely editing?

    The repair message says how: put THIS branch's entry back to the placeholder and run
    `apply` again.
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

    at_base = duplicates_in(onto_text, f"{onto}:CHANGELOG.md")
    if at_base:
        named = ", ".join(fmt(r) for r in at_base)
        raise StampError(
            f"{onto} itself declares {named} more than once — a "
            '"keep both sides" merge landed there without being caught. Numbering on top of '
            f"that compounds it. Fix {onto} first: one of those entries has to go back to "
            f"`## {PLACEHOLDER} — …` and be re-stamped."
        )

    inherited = _releases_at_fork(repo, onto)
    if inherited is None:
        # No CHANGELOG.md at the merge base, so nothing here can tell which numbers this
        # branch claimed and which it inherited. Refusing on every shared number would stop
        # a correct branch; the duplicate checks above still hold, and `check` on main still
        # catches the state this one is for.
        return
    theirs = dict(release_headings(onto_text, f"{onto}:CHANGELOG.md"))
    for rel, line in release_headings(branch_text, "CHANGELOG.md"):
        if rel in inherited or rel not in theirs:
            continue
        raise StampError(
            f"this branch's CHANGELOG adds `{line}` and {onto} already has "
            f"`{theirs[rel]}` — the same release number for two different releases. Whoever "
            f"landed first took {fmt(rel)}. {repair}"
        )


def _releases_at_fork(repo: Path, onto: str) -> set[Release] | None:
    """Release numbers already in CHANGELOG.md at the commit this branch forked from.

    None when there is no merge base, or the merge base has no CHANGELOG.md at all — a repo
    that only just grew one, or histories with no common ancestor. That is "cannot tell"
    rather than "none", and the caller treats it as such: refusing on every shared number
    would stop a correct branch over a question this could not answer.
    """
    try:
        base = merge_base(repo, onto)
    except StampError:
        return None
    if not _git_ok(repo, "cat-file", "-e", f"{base}:CHANGELOG.md"):
        return None
    return set(releases_in(_git(repo, "show", f"{base}:CHANGELOG.md"), f"{base}:CHANGELOG.md"))


def build_plan(repo: Path, onto: str, serve: bool | None, major: bool = False) -> Plan:
    plan = Plan()
    plan.sites, plan.loose = scan(repo, plan.symlinked)
    plan.untracked = scan_untracked(repo, plan.symlinked, plan.unreadable)

    changelog = repo / "CHANGELOG.md"
    # Guarded like every other file this tool reads or writes. The markdown scan already
    # skipped a symlinked CHANGELOG.md and said so, but this read went straight through it —
    # so the release number could be computed from a file outside the repository that would
    # then never be written to. `check` guards its own read of this file for the same reason.
    if _linked(repo, "CHANGELOG.md"):
        raise StampError(
            "CHANGELOG.md is a symlink, or sits under one. The release number is read from "
            "that file and the stamp is written back into it, and a link points wherever it "
            "points — which may not be this repository. Replace it with a real file"
        )
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

    # A loose mention IN THE CHANGELOG is a release entry written in a shape nothing will
    # rewrite — `## vNEXT.1`, say — and it refuses whether or not there is anything else to
    # stamp, because there is no other file that could be carrying the real entry. A loose
    # mention anywhere else is a defect in that doc, not in this branch's release, and is
    # warned about instead: making one stray word in one tracked doc refuse every branch in
    # the repo is how a gate that is right in principle gets switched off in practice.
    def _refuse_loose(sites: list[Site]) -> None:
        where = "; ".join(f"{s.path}:{s.line}" for s in sites[:4])
        raise StampError(
            f"{PLACEHOLDER} appears where it will not be rewritten ({where}). A placeholder "
            "is only stamped in a heading or a bold run — put it in one, or in backticks if "
            "you meant to write ABOUT the placeholder rather than to claim a release"
        )

    loose_here = [s for s in plan.loose if s.path == "CHANGELOG.md"]
    nothing_to_do = not plan.sites and not loose_here and not headings

    def _noop() -> Plan:
        """Return with nothing to stamp. Reached from more than one place now: a base this
        tool cannot read a number at is not a stop for a branch that needs no number, and it
        is not a licence to skip the one refusal that still applies either."""
        if loose_here:
            _refuse_loose(loose_here)
        return plan

    # The ref is resolved to a SHA once, here, and every later question is asked of the SHA.
    # `git merge-base origin/main HEAD` and `git show origin/main:CHANGELOG.md` re-resolve
    # the NAME, so a push landing mid-run would have the number computed against one base
    # and the served-version inference against another, silently.
    #
    # A branch with nothing to stamp does not need the ref to EXIST, though. `fix-and-land`
    # documents running `apply` unconditionally because it is a noop on a branch that ships
    # no release, and it wires exit 2 straight to a HOLD — so a fresh clone, a fork off
    # `origin/develop`, or a CI checkout that only fetched the PR head must not turn every
    # such branch into a hold. The collision check still runs whenever the ref IS resolvable,
    # which is the case it exists for.
    try:
        onto_sha = resolve(repo, onto)
        onto_text = _show(repo, onto_sha, "CHANGELOG.md", onto)
    except StampError:
        if not nothing_to_do:
            raise
        return plan

    # Outside that guard on purpose: a collision is a real refusal and must not be swallowed
    # by the "nothing to stamp" case, since a branch that has ALREADY stamped is exactly the
    # state with no placeholder left and the one this check exists for.
    _collision(repo, branch_text, onto, onto_text)

    # THE NUMBERS COME FIRST, into LOCALS. Both checks below are about the base and about
    # numbers already written down — neither is a question about this branch's placeholder,
    # and both used to sit under the `not plan.sites` early return where a branch that had
    # hard-coded its number could not reach them (#167). They stay locals until the early
    # return has been passed, because `plan.version` is what `plan.stamping` reads: setting
    # it here would make every docs-only branch report `stamped vX` and break the noop that
    # `fix-and-land` runs unconditionally.
    #
    # `next_release` before `max()` for the reason it always was: it refuses an unparseable
    # file with a sentence, where `max(())` on a CHANGELOG with no headings exits 1 on a
    # ValueError — which a caller reading the documented 0/2 scheme reads as "unknown", the
    # one outcome this tool promises never to give.
    #
    # In a `try` BECAUSE they are hoisted. That refusal used to sit below the early return,
    # so a base whose CHANGELOG has no parseable headings — a repo that just adopted the
    # file, an `--onto` pointing at a stub, one whose only headings are inside fenced
    # examples — never reached a branch that ships no release. Hoisted and unguarded it exits
    # 2 for that branch instead, and `fix-and-land` wires exit 2 straight to a HOLD: the
    # blast radius #168 is about, reintroduced one line above the fix for it. A branch that
    # DOES need a number still gets the refusal, because there is no number to hand it.
    #
    # Both bumps come out of one pair of calls and `next_version` is chosen from them, rather
    # than a third parse asking a question these two have already answered between them: the
    # flag picks one of two answers, it does not produce a third.
    try:
        minor_next = next_release(onto_text, False, f"{onto}:CHANGELOG.md")
        major_next = next_release(onto_text, True, f"{onto}:CHANGELOG.md")
        onto_newest = max(releases_in(onto_text, f"{onto}:CHANGELOG.md"))
    except StampError as e:
        if plan.sites:
            raise
        plan.warnings.append(
            f"no release number could be read at {onto} ({e}), so nothing on this branch "
            "could be checked against one. Not this branch's problem, since it ships no "
            "release and needs no number"
        )
        return _noop()
    next_version = major_next if major else minor_next
    could_have_issued = {minor_next, major_next}

    # A number above the base's newest is one nobody at `onto` has issued, and whether that
    # is a refusal turns on WHETHER THIS BRANCH STILL HAS A PLACEHOLDER.
    #
    #   * With one, every such number is refused, the next one included. The branch has
    #     something to stamp AND has already written a number down: stamping would put a
    #     number in twice, which is the case the old check was written for.
    #   * Without one, the next number is the single legitimate reading. Re-running `apply`
    #     on a branch it already stamped has to stay a noop — `fix-and-land` runs it
    #     unconditionally — and once `apply` has run there is no placeholder left, so that
    #     branch is byte-identical to one that hard-coded the same number. Nothing in the
    #     tree tells those two apart, so the number is judged and not its author.
    #
    # The second bullet is the whole of #167, and it is what hoisting buys: `## v2.40` on a
    # branch whose base is at v2.33 is not a collision today and is one the week v2.40 comes
    # round. Under the old placement it was invisible, because a branch that names its own
    # release has no placeholder to trip the check — measured across an eight-PR queue in
    # #167, where all eight hard-coded a number, none carried a `vNEXT`, and the guard fired
    # for none of them.
    #
    # What it CANNOT catch, and the README and CHANGELOG say so rather than implying
    # otherwise: a hand-written `max+1`. That is byte-identical to `apply`'s own output, and
    # `max+1` read off the top of `main` is exactly what a person hard-coding by hand picks.
    # What is left is the number that skips ahead of the next free one, the number already
    # taken at the base (`_collision`, above), and more than one new number at once.
    #
    # At or BELOW the base's newest is not this check's business: the branch either inherited
    # the number (fine — it is editing a shipped entry) or somebody else has since taken it,
    # which is `_collision`'s third shape and is refused above.
    #
    # BOTH bumps are admissible, not just the one this invocation was asked for. `--major` is
    # a flag and never an inference, so a branch stamped `v3` re-meets this check the next
    # time `apply` runs WITHOUT it — and `fix-and-land` runs `apply` unconditionally and
    # without the flag. Allowing only the minor bump would refuse every major release branch
    # on the second run, which is the noop that caller depends on. ONE of the two, though,
    # never both at once: a blanket difference against the pair let a branch carrying `##
    # v2.34` AND `## v3` straight through — two releases on one branch, which the convention
    # says cannot happen and which nothing else here can see, since `_collision` looks for a
    # number that already exists at `onto` or twice in this file and neither is true of two
    # different numbers nobody has issued yet.
    #
    # WHERE THE NUMBER CAME FROM IS NOT ASKED, and three rounds of review are the reason.
    # Hoisting this check above the early return means it also runs for branches with nothing
    # to stamp — including one sitting on top of a ref FRESHER than a stale `--onto`, whose
    # CHANGELOG carries numbers that shipped elsewhere. Refusing those as "a branch does not
    # pick its own number" is nonsense about an entry the branch only inherited, so two
    # attempts were made to tell the two apart from the local repository: the second-and-later
    # parents of merge commits, and then any ref sharing `onto`'s branch name. Both were
    # simultaneously too wide and too narrow, and each hole was the same one:
    #
    #   * merge parents excused a number found in ANY merged snapshot, so a branch that
    #     hand-wrote `## v2.40` and was refused for it had the refusal laundered by a second
    #     branch merging it — and missed rebase and fast-forward, which carry no merge commit;
    #   * same-named refs excused a purely local `refs/heads/main`, never pushed and never
    #     reviewed, which is what `git checkout main && git commit && git checkout -b feat`
    #     leaves behind — and refused any checkout that holds the commits but not the ref
    #     (`clone --single-branch`, `pull <url> main`, a pruned remote).
    #
    # The premise both share is that a local repository can say where a number LANDED. It
    # cannot: a ref proves somebody wrote a number down, never that it was issued. So this
    # asks the one question it can answer — is this number above the newest at the ref I was
    # given — and the message names BOTH repairs rather than guessing which applies. A stale
    # base is a real thing to be told about, and "fetch and try again" costs a reader nothing
    # when it was not the problem.
    issued = releases_in(branch_text, "CHANGELOG.md")
    claimed = {r for r in issued if r > onto_newest}
    allowed = (claimed if not plan.sites and len(claimed) == 1 and claimed <= could_have_issued
               else set())
    ahead = sorted(claimed - allowed)
    if ahead:
        named = ", ".join(fmt(r) for r in ahead)
        # "Put it back to the placeholder" is only a repair when there is not already one
        # here. With a placeholder present, following it literally writes a SECOND, and
        # `build_plan` then refuses with "two placeholders cannot both become one number" —
        # a different message about a state the advice created, which is the advice loop
        # `_repair_advice` was rewritten to stop. That branch is told to delete instead.
        own = (f"delete it — this branch already carries a `## {PLACEHOLDER}`, and that is "
               "the entry that becomes a number" if plan.sites else
               f"put the entry back to `## {PLACEHOLDER} — …` (and its README bullet with "
               "it) and run `apply` again")
        raise StampError(
            f"this branch's CHANGELOG already has an entry for {named}, which does not exist at "
            f"{onto} (newest there is {fmt(onto_newest)}, so the next free number is "
            f"{fmt(next_version)}). Either this branch named its own number — {own} — or "
            f"{onto} is behind and the entry was inherited from a later one, in which case "
            "fetch and re-run against the updated ref. This tool cannot tell which from "
            "here: a ref proves somebody wrote a number down, never that it was issued."
        )

    # DOES THIS BRANCH SHIP A RELEASE? Only a branch with somewhere to stamp does, and the
    # numbers are deliberately not consulted — the third and last place this file tried to
    # read intent off a release number, and the third to get it wrong.
    #
    # It used to be `bool(plan.sites) or bool(claimed & could_have_issued)`, to keep a branch
    # already stamped `v3` and one already stamped `v2.34` on the same side of the #168
    # refusal. But `claimed` cannot tell an already-stamped number from an INHERITED one — a
    # docs-only branch that pulled a `main` which had since issued v2.34 has `claimed ==
    # {v2.34}`, which is also exactly `could_have_issued` against the stale base. So that
    # branch read as shipping a release and was refused over a broken base it does not touch:
    # #168's blast radius, arriving through the flag rather than through the check.
    #
    # Consistency between the two already-stamped branches was the wrong thing to want. The
    # refusal exists to stop `apply` handing out `max+1` while the base holds an entry that is
    # going to want a number — and a branch with no placeholder stamps NOTHING, so there is no
    # number for a broken base to make wrong. It is told, and it carries on. Having something
    # to stamp is the whole of the question.
    ships_release = bool(plan.sites)

    # An unstamped placeholder at the base is a real refusal for a branch that needs a number
    # and NOISE for one that does not. Refusing both is how one skipped stamp took out every
    # branch in the repo at once (#168): `fix-and-land` wires exit 2 straight to a HOLD, so a
    # branch shipping no release would be held over somebody else's mistake, in a file it
    # does not touch. It is told instead, and carries on.
    #
    # A branch that has already MERGED the broken base carries the placeholder in its own
    # worktree, so it is refused here through `plan.sites` — correctly, since `apply` would
    # otherwise stamp somebody else's entry. The relief is for branches that have not taken
    # that merge, and the docs say so rather than claiming the refusal is gone.
    try:
        unstamped_at_base = placeholder_at_ref(repo, onto_sha)
    except StampError as e:
        # Same reasoning one level along: reading the base must not be able to stop a branch
        # that needs nothing from it. A shallow clone, an object missing from the ref, a
        # `git grep` that fails for any reason — for a branch that ships a release this stays
        # a refusal, because the check is load-bearing for it; for one that does not, it is a
        # line on stderr and the branch carries on.
        if ships_release:
            raise
        plan.warnings.append(f"could not check {onto} for an unstamped `{PLACEHOLDER}`: {e}")
        unstamped_at_base = []
    if unstamped_at_base:
        where = ", ".join(unstamped_at_base)
        if ships_release:
            raise StampError(
                f"{onto} itself carries an unstamped `{PLACEHOLDER}` ({where}) — the previous "
                "release landed without being stamped. Numbering on top of it would hand this "
                "branch a number the unstamped one is going to want.\n"
                f"{_repair_advice(repo, onto, onto_sha)}"
            )
        plan.warnings.append(
            f"{onto} carries an unstamped `{PLACEHOLDER}` ({where}) — the previous release "
            "landed without being stamped. Not this branch's problem, since it ships no "
            f"release and needs no number. {_repair_advice(repo, onto, onto_sha)}"
        )

    if not plan.sites:
        return _noop()

    # Past the early return, so this branch really is stamping: `plan.version` is what
    # `plan.stamping` reads and what `_report` prints.
    plan.version = next_version
    plan.onto_newest = onto_newest
    plan.major = major

    if plan.loose:
        _refuse_loose(plan.loose)

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
    """`ref:path`, with a sentence rather than a git error when the file is missing.

    The REF's existence is not re-checked: the only caller passes a SHA `resolve()` has
    already verified with the identical `rev-parse --verify`, so a second one was a
    subprocess per run guarding a branch nothing could reach. `named` stays, because the
    message below has to name the ref the operator typed rather than the SHA it became.
    """
    label = named or ref
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
    # The plan is a snapshot, and the files are re-read here. A hook, an editor or a
    # concurrent agent can change one in between, and a count that no longer matches the plan
    # means the tool is about to write a release it did not plan — so it refuses rather than
    # quietly dropping the file, which produced a successful, partially-stamped release with
    # nothing anywhere reporting what had been skipped.
    planned = Counter(s.path for s in plan.sites)
    edits: list[tuple[str, Path, str]] = []
    for path in sorted(planned):
        full = repo / path
        new_text, count = stamp_text(_read(full, path), version, path)
        if count != planned[path]:
            raise StampError(
                f"{path} was planned with {planned[path]} `{PLACEHOLDER}` site(s) and now has "
                f"{count} — it changed between planning and writing. Nothing was written; "
                "re-run `apply` against the tree as it is now"
            )
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
                      original[:at] + m.group(1) + plan.served_to + m.group("q")
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

    EVERY snapshot is restored, including the one for the file whose own write raised.
    `write_text` opens in mode `w`, which truncates before it writes a byte, so a failure
    part way through — ENOSPC, EIO, a quota — leaves that file empty or half written while
    it is still absent from `written`. Restoring only `originals[:len(written)]` skipped
    exactly that file and then reported "nothing was written; the worktree is as you left
    it", which was false in the one scenario this helper exists to handle. The permission
    test passes either way, because a read-only file fails at open() before truncation — so
    the failure with real data loss in it was the untested one.

    A snapshot whose file still matches it is skipped rather than rewritten, which is what
    keeps the read-only case honest: nothing there was written, and re-writing it would fail
    and turn a clean refusal into a "rolling back left … rewritten" that names a file nobody
    touched.
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
        for full, text in reversed(originals):
            try:
                if full.read_bytes() == text.encode("utf-8"):
                    continue
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
    unreadable: list[str] = []
    sites, loose = scan(repo, symlinked)
    untracked = scan_untracked(repo, symlinked, unreadable)
    bad = sites + loose

    # A symlinked CHANGELOG.md used to make `dupes` unconditionally empty, so `clean` could
    # still be true and this command printed "no repeated release number" about a file it had
    # never opened — the same "clean over a file it did not read" shape the tracked-symlink
    # accounting was added to end, one level up. It is a refusal now: the guard cannot do its
    # job on a CHANGELOG that lives outside the repository, and saying so is the honest end.
    if _linked(repo, "CHANGELOG.md"):
        raise StampError(
            "CHANGELOG.md is a symlink, or sits under one, so the duplicate-number check has "
            "no file in this repository to read. Replace it with a real file — a guard that "
            "cannot read the CHANGELOG cannot report on it"
        )
    dupes = duplicates_by_file(repo)

    clean = not bad and not dupes
    if args.json:
        print(json.dumps({
            "clean": clean,
            "sites": [{"path": s.path, "line": s.line, "text": s.text} for s in sites],
            "loose": [{"path": s.path, "line": s.line, "text": s.text} for s in loose],
            "duplicates": sorted({fmt(r) for rels in dupes.values() for r in rels}),
            "duplicates_by_file": {p: [fmt(r) for r in rels] for p, rels in dupes.items()},
            # Present whether or not anything was skipped, and present for the same reason
            # `preflight --json` carries them: a CI consumer of this command has no other
            # field to inspect, and a file dropped without a key to report it in is
            # skipped-and-quiet, which is how the literal string reaches a reader.
            "untracked": [{"path": s.path, "line": s.line, "text": s.text} for s in untracked],
            "symlinked": sorted(symlinked),
            "unreadable": sorted(unreadable),
        }, indent=2))
        return 0 if clean else 2

    # Warned in text mode too, and by the same helper `preflight` and `apply` use. `check`
    # threading no accumulator was the whole bug: a tracked markdown SYMLINK was dropped
    # with no record at all, and the guard whose one job is catching the literal string
    # printed "clean" over a file it had not read.
    _warn_skipped(Plan(untracked=untracked, symlinked=sorted(symlinked),
                       unreadable=sorted(unreadable)))

    if clean:
        print(f"clean: no unstamped `{PLACEHOLDER}` and no repeated release number in "
              "CHANGELOG.md or the README release list.")
        return 0
    if bad:
        print(f"STOP: {len(bad)} unstamped `{PLACEHOLDER}` placeholder(s):", file=sys.stderr)
        for s in bad:
            print(f"  {s.path}:{s.line}  {s.text}", file=sys.stderr)
        # The same resolved command `preflight` and `apply` hand out, for the same reason and
        # on the harder case: this is the guard that fires ON main, so the ref to stamp
        # against is never the `origin/main` every other invocation passes. Describing how to
        # find one and resolving one are the same sentence said two ways, and leaving the
        # description here would have left it on the path that hits it most.
        print("\nA release landed without being stamped, so this ref documents a version that "
              f"does not exist. {_repair_advice(repo, 'HEAD')}\nThen push the result.",
              file=sys.stderr)
    if dupes:
        for path, rels in dupes.items():
            print(f"STOP: release number(s) declared twice in {path}: "
                  + ", ".join(fmt(r) for r in rels), file=sys.stderr)
        print("\nTwo branches were stamped the same number and the merge kept both sides. "
              "One of those entries has to be renumbered: put it back to "
              f"`## {PLACEHOLDER} — …` (and its README bullet with it) and run "
              "`release_stamp.py apply`.", file=sys.stderr)
    return 2


def _warn_skipped(plan: Plan) -> None:
    for line in plan.warnings:
        print(f"warning: {line}", file=sys.stderr)
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
    if plan.unreadable:
        # Untracked only. A tracked file in this state still refuses — but the module
        # contract says untracked markdown is never a STOP, and a scratchpad with a stray
        # byte or an unclosed fence in it must not be able to hold up every branch in the
        # repo. Named rather than dropped, for the usual reason.
        print("warning: untracked markdown this tool could not read, so it was not "
              "scanned:", file=sys.stderr)
        for why in plan.unreadable:
            print(f"  {why}", file=sys.stderr)


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
