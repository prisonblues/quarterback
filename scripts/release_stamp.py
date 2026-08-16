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

The number is `max(release headings at --onto) + 1`. It is computed from the CHANGELOG at
a git ref and from nothing else — not from a live board, not from the local checkout's own
history. Two branches that stamp at the same second get the same number and the second one
to reach the merge conflicts on the CHANGELOG, re-stamps against the moved base and gets
the next one. That is the intended loop: the number is cheap to redo because nothing else
in the branch was ever written in terms of it.

**This is not an allocator and deliberately does not become one.** #46/#99's
`POST /release/claim` allocates a number in advance for callers that want to announce one;
this tool needs no board, no network and no claim, and the two do not have to agree —
because a stamped number is only ever "the next one free at the ref I merged into", which
is a question a git ref answers on its own.

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
    release_stamp.py preflight [--repo DIR] [--onto REF] [--json]
    release_stamp.py apply     [--repo DIR] [--onto REF] [--json] [--serve | --no-serve]
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

_V = r"v(\d+)(?:\.(\d+))?"

#: `## v2.33 — …` / `## vNEXT — …`. Anchored at line start on a level-2 heading, which is
#: what both CHANGELOG.md and README.md use for a release entry.
_HEADING = re.compile(rf"^##[ \t]+{_V}\b", re.MULTILINE)
_HEADING_PLACEHOLDER = re.compile(rf"^##[ \t]+{PLACEHOLDER}\b", re.MULTILINE)

#: Where a placeholder is legal, and therefore where it gets rewritten: a markdown heading
#: of any level, or the opening of a bold run. Group 1 is the token itself, so the rewrite
#: is a span replacement and the surrounding syntax is never reconstructed.
_SITE = re.compile(rf"(?:^\#{{1,6}}[ \t]+|\*\*)({PLACEHOLDER})\b", re.MULTILINE)

#: Any mention at all, for the "you wrote it somewhere I will not rewrite" check.
_MENTION = re.compile(rf"\b{PLACEHOLDER}\b")

#: `version = "2.33.0"` in pyproject.toml, keeping whatever trails it (there is a comment).
_PYPROJECT_VERSION = re.compile(r'(?m)^(version\s*=\s*")\d+\.\d+\.\d+(")')

#: `app = FastAPI(… version="2.33.0" …)`. Bounded to that call's own parentheses, tolerating
#: one level of nesting, and needing no DOTALL — the same shape (and the same reasoning)
#: as the fixture in harness/tests/test_release_numbers.py, which explains at length why a
#: lazy `.*?` version of this silently latches onto the next version literal in the file.
_SERVED_VERSION = re.compile(
    r'(?m)^app\s*=\s*FastAPI\((?:[^()]|\([^()]*\))*?version="(\d+\.\d+\.\d+)"'
)


class StampError(Exception):
    """A refusal with a sentence attached. Always exits 2, never 1."""


@dataclass
class Site:
    """One placeholder occurrence the stamper will rewrite."""

    path: str
    line: int
    text: str  # the whole line, for the report


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

    @property
    def stamping(self) -> bool:
        return self.version is not None

    def as_json(self) -> dict:
        return {
            "stamping": self.stamping,
            "version": fmt(self.version) if self.version else None,
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


def tracked_markdown(repo: Path) -> list[str]:
    """Every tracked `.md` path, sorted. The stamp scope is markdown and only markdown:
    a placeholder has no meaning in code, and restricting the scan this way is also what
    keeps this tool's own docstring — which says `vNEXT` twenty times — out of its way."""
    out = _git(repo, "ls-files", "-z", "--", "*.md")
    return sorted({p for p in out.split("\0") if p})


def untracked_markdown(repo: Path) -> list[str]:
    """Markdown git is not tracking but is not ignoring either.

    Never stamped — `plan.md` is untracked on purpose, is where the agents working this repo
    argue about releases in prose, and is not part of any release. But it is also where a
    genuinely new doc sits for the minutes between being written and being `git add`ed, so a
    placeholder here is reported rather than passed over in silence: skipped-and-mentioned is
    recoverable, and skipped-and-quiet is how the literal string `vNEXT` reaches a reader.
    """
    out = _git(repo, "ls-files", "-z", "--others", "--exclude-standard", "--", "*.md")
    return sorted({p for p in out.split("\0") if p})


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
    """
    base = _git(repo, "merge-base", onto, "HEAD").strip()
    diff = _git(repo, "diff", "--name-only", base).split("\n")
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard").split("\n")
    return sorted({p for p in [*diff, *untracked] if p})


# ------------------------------------------------------------------- release numbers


def release(major: str, minor: str | None) -> Release:
    return int(major), int(minor or 0)


def fmt(r: Release) -> str:
    """Spelled the way the files spell it: `v3`, not `v3.0`."""
    return f"v{r[0]}" if r[1] == 0 else f"v{r[0]}.{r[1]}"


def releases_in(text: str) -> list[Release]:
    """Every `## vX[.Y]` heading, in file order (this repo's CHANGELOG is newest first)."""
    return [release(m.group(1), m.group(2)) for m in _HEADING.finditer(text)]


def next_release(text: str) -> Release:
    """One past the highest heading in `text`.

    Highest, not first. The file is newest-first and a test enforces that, but a tool that
    hands out numbers must not be the one thing that trusts the ordering it is about to
    disturb: reading position 0 would re-issue a live number the moment an entry was
    inserted a line too low, which is precisely the mistake this whole mechanism exists to
    stop being possible.
    """
    found = releases_in(text)
    if not found:
        raise StampError("no `## vX.Y` headings at the base ref — CHANGELOG.md is not the "
                         "file this tool thinks it is, or the ref is wrong")
    major, minor = max(found)
    return major, minor + 1


# ------------------------------------------------------------------ markdown scanning


def mask_code(text: str) -> str:
    """Blank out fenced blocks and inline code spans, preserving length and newlines.

    Returned text is only ever used to LOCATE matches — every rewrite is applied to the
    original by offset — so the substitution character just has to be one no pattern here
    can match. Length preservation is what makes that safe.
    """
    out = list(text)
    pos, fence = 0, None
    for line in text.split("\n"):
        stripped = line.lstrip()
        marker = next((f for f in ("```", "~~~") if stripped.startswith(f)), None)
        inside = fence is not None
        if fence is None:
            fence = marker
        elif marker == fence:
            fence = None
        if inside or marker:  # the fence lines themselves are blanked too
            for i in range(pos, pos + len(line)):
                out[i] = "\0"
        pos += len(line) + 1

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
        if not full.exists():  # deleted in the worktree but still in the index
            continue
        if full.is_symlink():
            if skipped is not None:
                skipped.append(path)
            continue
        text = full.read_text()
        if PLACEHOLDER not in text:
            continue
        masked = mask_code(text)
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


def stamp_text(text: str, version: str) -> tuple[str, int]:
    """Rewrite every placeholder site in one file's text. Returns (new text, count)."""
    masked = mask_code(text)
    spans = [m.span(1) for m in _SITE.finditer(masked)]
    for start, end in reversed(spans):  # right to left, so earlier offsets stay valid
        text = text[:start] + version + text[end:]
    return text, len(spans)


# ------------------------------------------------------------------------- the plan


def build_plan(repo: Path, onto: str, serve: bool | None) -> Plan:
    plan = Plan()
    plan.sites, plan.loose = scan(repo, plan.symlinked)
    untracked_sites, untracked_loose = scan_paths(repo, untracked_markdown(repo))
    plan.untracked = untracked_sites + untracked_loose

    changelog = repo / "CHANGELOG.md"
    if not changelog.exists():
        raise StampError("no CHANGELOG.md in this repo")
    branch_text = changelog.read_text()
    branch_masked = mask_code(branch_text)

    headings = list(_HEADING_PLACEHOLDER.finditer(branch_masked))
    if len(headings) > 1:
        lines = ", ".join(str(_line_of(branch_text, m.start())[0]) for m in headings)
        raise StampError(
            f"CHANGELOG.md has {len(headings)} `## {PLACEHOLDER}` headings (lines {lines}). "
            "A branch ships one release; two placeholders cannot both become one number, "
            "and guessing which is which is exactly the judgement this tool refuses to make"
        )

    if plan.loose:
        where = "; ".join(f"{s.path}:{s.line}" for s in plan.loose[:4])
        raise StampError(
            f"{PLACEHOLDER} appears where it will not be rewritten ({where}). A placeholder "
            "is only stamped in a heading or a bold run — put it in one, or in backticks if "
            "you meant to write ABOUT the placeholder rather than to claim a release"
        )

    if not plan.sites:
        return plan  # nothing to stamp; a noop, not a failure

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
            f"the `## {PLACEHOLDER}` heading is below `{fmt(release(first.group(1), first.group(2)))}` "
            "in CHANGELOG.md. The file is newest first, so an unreleased entry belongs at "
            "the top — stamped where it is, it would break the ordering the whole file is read by"
        )

    onto_text = _show(repo, onto, "CHANGELOG.md")
    if _HEADING_PLACEHOLDER.search(mask_code(onto_text)):
        raise StampError(
            f"{onto} itself carries an unstamped `## {PLACEHOLDER}` — the previous release "
            "landed without being stamped. Fix that first; numbering on top of it would "
            "hand this branch a number the unstamped one is going to want"
        )
    # `next_release` first, because it is the one that refuses an unparseable file with a
    # sentence. Taking `max()` of the same list first would reach `max(())` on a CHANGELOG
    # with no headings and exit 1 on a ValueError — which a caller reading the documented
    # 0/2 scheme reads as "unknown", i.e. the one outcome this tool promises never to give.
    plan.version = next_release(onto_text)
    plan.onto_newest = max(releases_in(onto_text))

    if plan.version in releases_in(branch_text):
        raise StampError(
            f"{fmt(plan.version)} is the next number at {onto}, and this branch's CHANGELOG "
            "already has an entry for it. Rebase onto the ref you are actually merging into"
        )

    changed = changed_paths(repo, onto)
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
        main_py = repo / "app" / "main.py"
        m = _SERVED_VERSION.search(main_py.read_text()) if main_py.exists() else None
        if not m:
            raise StampError(
                'app/main.py has no `app = FastAPI(… version="X.Y.Z" …)` to bump — the '
                "version stopped being an inline literal in that call, so this tool cannot "
                "move it and must not pretend it did"
            )
        pyproject = repo / "pyproject.toml"
        found = len(_PYPROJECT_VERSION.findall(pyproject.read_text())) if pyproject.exists() else 0
        if found != 1:
            raise StampError(
                f'pyproject.toml has {found} `version = "X.Y.Z"` lines, expected exactly 1 — '
                "refusing to guess which one the package version is"
            )
        plan.served_from = m.group(1)
        plan.served_to = f"{plan.version[0]}.{plan.version[1]}.0"
    return plan


def _show(repo: Path, ref: str, path: str) -> str:
    if not _git_ok(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"):
        raise StampError(
            f"ref {ref!r} does not exist here. Fetch it first — the number this tool hands "
            "out is only correct relative to the ref you are merging into"
        )
    return _git(repo, "show", f"{ref}:{path}")


# ---------------------------------------------------------------------- the commands


def cmd_preflight(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    plan = build_plan(repo, args.onto, None)
    if args.json:
        print(json.dumps(plan.as_json(), indent=2))
        return 0
    _report(plan, args.onto, applied=False)
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    plan = build_plan(repo, args.onto, args.serve)
    if not plan.stamping:
        if args.json:
            print(json.dumps(plan.as_json(), indent=2))
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
        new_text, count = stamp_text(full.read_text(), version)
        if count:
            edits.append((path, full, new_text))

    if plan.serves:
        pyproject = repo / "pyproject.toml"
        text, n = _PYPROJECT_VERSION.subn(rf"\g<1>{plan.served_to}\g<2>", pyproject.read_text())
        if n != 1:  # build_plan already refused this; re-checked rather than assumed
            raise StampError("pyproject.toml's version line changed between plan and apply")
        edits.append(("pyproject.toml", pyproject, text))

        main_py = repo / "app" / "main.py"
        text = main_py.read_text()
        m = _SERVED_VERSION.search(text)
        if not m:  # build_plan already refused this; re-checked rather than asserted
            raise StampError("app/main.py's version literal vanished between plan and apply")
        edits.append(("app/main.py", main_py,
                      text[: m.start(1)] + plan.served_to + text[m.end(1) :]))

    written: list[str] = []
    for path, full, new_text in edits:
        full.write_text(new_text)
        written.append(path)

    if args.json:
        print(json.dumps({**plan.as_json(), "written": written}, indent=2))
    else:
        _report(plan, args.onto, applied=True)
        print("\nwritten: " + ", ".join(written))
        print("Nothing was committed — review the diff, then commit it with the release.")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """The guard. Run on the integration branch after a merge: did anything land unstamped?

    Separate from `preflight` because it asks a different question and needs no base ref.
    An unstamped placeholder is FINE on a feature branch — that is the whole convention —
    and is a defect the moment it is on main, so the check that fires on it cannot be one
    every branch runs.
    """
    repo = Path(args.repo).resolve()
    sites, loose = scan(repo)
    bad = sites + loose
    if args.json:
        print(json.dumps({
            "clean": not bad,
            "sites": [{"path": s.path, "line": s.line, "text": s.text} for s in sites],
            "loose": [{"path": s.path, "line": s.line, "text": s.text} for s in loose],
        }, indent=2))
        return 0 if not bad else 2
    if not bad:
        print(f"clean: no unstamped `{PLACEHOLDER}` in tracked markdown.")
        return 0
    print(f"STOP: {len(bad)} unstamped `{PLACEHOLDER}` placeholder(s):", file=sys.stderr)
    for s in bad:
        print(f"  {s.path}:{s.line}  {s.text}", file=sys.stderr)
    print("\nA release landed without being stamped. Run `release_stamp.py apply` and push "
          "the result — until then this ref documents a version that does not exist.",
          file=sys.stderr)
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
        return
    verb = "stamped" if applied else "would stamp"
    print(f"{verb} {fmt(plan.version)}  (newest at {onto}: {fmt(plan.onto_newest)})")
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

    pf = sub.add_parser("preflight", help="report what would be stamped (read-only)")
    common(pf)
    pf.add_argument("--onto", default="origin/main", help="the ref you are merging into")
    pf.set_defaults(func=cmd_preflight)

    ap = sub.add_parser("apply", help="stamp the worktree (never commits)")
    common(ap)
    ap.add_argument("--onto", default="origin/main", help="the ref you are merging into")
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
