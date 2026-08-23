#!/usr/bin/env python3
"""Cut a release on `main`, after the merge — the one place a version number is ever written.

Nobody stamps anywhere else. A branch writes `changelog.d/<issue>.<kind>.md` and names no
version at all; this file is what turns a pile of fragments into a numbered release, and it
runs on the integration branch against the commit that actually exists.

    release.py preview                # what the next release would be, read-only, anywhere
    release.py run --title "…"        # assemble, number, write, commit, tag — `main` only
    release.py guard --onto REF       # refuse a branch that edits a generated file
    release.py frozen --onto REF      # refuse a branch that rewrites a SHIPPED entry

## Why the branch-side path is gone rather than documented against

Every branch that wanted a number used to pick one at the moment it wrote its CHANGELOG
entry, which is the one moment nobody can know the answer. Ten collisions in two days, and
the tenth arrived an hour after an allocator shipped and worked — two agents simply did not
call it. So the number moved to land time and a placeholder took its place, and that half
worked: no branch picked a number again.

What did not work was leaving `apply` runnable on a branch. Every stamped branch rewrote the
same two files — `CHANGELOG.md` and the README's release list — so N stamped branches in
flight was N-choose-2 conflicts BY CONSTRUCTION, over nothing: both entries were right and
both belonged, and git cannot know that two insertions at one offset are independent. On the
night this file was rewritten, six pull requests were open; the three that had stamped were
all `CONFLICTING`, the three that had not were all `MERGEABLE`, and PR #398 landed both ways
— unmergeable stamped, zero conflicts once the stamp was reverted. Same branch, same work,
same base.

The affordance was the bug (#122). A rule against using a command every brief in the repo
tells you to run is a convention, and this repo has settled that argument elsewhere: "if
agents can both apply a label and act on it, the gate is decorative" (#85). So `preflight`,
`apply`, `check` and `collision` are deleted, not deprecated, along with the push-time tag
reservation they needed (#296) and the `no unstamped release on main` guard that existed only
because a branch could forget to stamp. A stale brief now gets `No such command`, which is
the loudest thing a removal can say.

## What `run` does, in order

1. Refuses unless the checkout is on the default branch, clean, and level with its remote.
   `main` is the only ref where the number is knowable and where there is no race to lose.
2. Parses every fragment in `changelog.d/` (`scripts/changelog_fragments.py` owns the format).
3. Numbers the release: one past the highest `## vX[.Y]` heading in `CHANGELOG.md`, folding
   in any number a release TAG already holds. Both inputs are read at HEAD, which on `main`
   after the merge is the commit being released.
4. Writes the entry at the top of `CHANGELOG.md`, adds the README bullet and re-renders that
   list into CHANGELOG order, bumps the served version if the release touched `app/` or
   `migrations/`, and deletes the fragments it consumed.
5. Commits `chore(release): vX.Y — <title>`, tags the commit it just made, and pushes both
   in ONE atomic push, so a release never reaches `main` without its tag.

Anything that fails between step 4 and the commit puts back every file this run had written
and every fragment it had consumed, from text it is still holding. What it does not undo is a
tag or a push that has already succeeded — a tag is never moved or deleted here, for the same
reason `release_tag.py` never moves one.

The tag names a commit on `main` by construction, so #406 cannot recur: there is no separate
branch-side stamp commit for a squash merge to discard and no tag left pointing at a commit
that is not an ancestor of anything.

## `--major`

Whether v3 or v2.34 follows v2.33 is a statement about what the release MEANS — the one input
here no ref can answer — so it is an explicit flag and never an inference (#386). The flag is
not authority for what the flag does: `run --major` asks for the number at the CONTROLLING
TERMINAL and refuses where there is none, or where `HARNESS_UNATTENDED=1`, or where the answer
is not the number. It refuses rather than warns, because a warning in a log nobody reads turns
an absent decision into a benign one — which is how this repo went from v2.99 to v3 instead of
v2.100.

The gate survives the move to the release job intact, and the move is what makes it cheap:
one caller, once a batch, instead of one per branch. For the unattended path there is
`--major-confirm vN`, which the release workflow's `workflow_dispatch` form collects from a
person and which must match the number that would be issued — the same "type the number"
discipline as the terminal prompt, in the one place a person is already deciding to cut a
release. A fragment field and a PR label were both considered and rejected: either would put
the judgement back on a branch, which is the affordance this change exists to remove.

`preview --major` is NOT gated — asking what the flag would do decides nothing, and its
`would issue v3 (--major, NOT v2.100)` line is the sentence that makes the slip visible.

## `guard` — the consolidated files are OUTPUT

`CHANGELOG.md`'s release entries and the README's release list are regenerated here and stay
in git, so `git log CHANGELOG.md` keeps working and a reader with no network keeps the
history. A branch that edits either is refused, and the refusal NAMES
`changelog.d/<issue>.<kind>.md` — a worker told only "no" retries or works around it, and both
are worse than the original mistake.

The CHANGELOG's PREAMBLE is outside the guard on purpose: it documents the convention the file
follows, so a branch changing the convention has to be able to change it. That is the same
line `frozen` draws, and drawing it differently in two places would mean one edit refused by
one guard and cleared by the other.

The guard runs in `harness/githooks/pre-push` AND in CI on `pull_request`, because neither
place is sufficient alone: a local hook cannot see `gh pr merge`, a CI job cannot stop a bad
push, and a hook is per-checkout and best-effort — this is exactly the class of mechanism that
ships uninstalled (#169). "Exists but is refused" is enforceable in both; "does not exist" is
enforceable in neither, since nothing stops a branch creating a file.

## The served version

`pyproject.toml` and `app/main.py` carry the version `GET /openapi.json` reports, and most
releases here are harness-side and correctly leave it alone (v2.16, v2.17, v2.18, v2.20,
v2.21 and v2.32 all did). So the bump is INFERRED — from whether the release changed `app/`
or `migrations/` since the previous release — and always reported rather than done quietly.
`--serve` / `--no-serve` override the inference for the release the inference gets wrong.

## A released entry is immutable, and `frozen` is what says so

Once `vN` is issued its entry is finished. It records what was broken or missing before that
release, which is the one part of a release not recoverable from the diff — so a branch
rewriting it is destroying the only copy, and doing so behind headings that all still read
correctly. That happened on `feat/issue-232`: a CHANGELOG conflict was resolved by moving the
branch's own 133-line entry under `## v2.59`, on top of that release's notes. Every check in
the repo read the CHANGELOG as a LIST OF HEADINGS and all the headings were present, unique
and correctly ordered (#325).

`frozen` compares the TEXT. For every `## vX[.Y]` entry that exists at both refs, the whole
slab — heading line and body, byte for byte — must be identical. Gone is a refusal too.

`guard` now refuses any branch edit to `CHANGELOG.md` at all, which subsumes `frozen` on a
pull request. `frozen` is kept anyway and pointed at the one writer that remains: `run`
asserts it before it commits, so the release job cannot corrupt the history it is appending
to, and the `pull_request` job stays as the second opinion the guard would need if it were
ever misconfigured. A guard deleted because another guard covers it is how a repo ends up
with neither.

**What it compares against, and why that ref.** The MERGE BASE of `--branch` and `--onto`.
That is the only ref that separates what this branch DID from what it merely inherited:
comparing against `--onto` itself would report every entry that landed while the branch was
open as one the branch deleted. It needs no stored state — no digest file checked into the
repo to be maintained per release and rewritten by the very merge resolution this exists to
catch. The true text lives in git, where the bad merge cannot reach it.

**What it therefore does NOT catch, stated because a guard whose blind spot is undocumented
is how the original defect survived:**

  * a corruption that has already LANDED on main. The merge base moves with it and the wrong
    text becomes the text every later branch is judged against. The window is one pull
    request: it is red for as long as that PR is open, and green forever after it merges.
  * a commit pushed straight to main, once it is pushed. On main the merge base with
    `origin/main` is HEAD, so there is nothing to compare. Before the push there is:
    `harness/githooks/pre-push` runs this against `refs/remotes/origin/main`, which is behind,
    so a direct commit IS refused at the keyboard — but only where the hook is installed, and
    `--no-verify` and the GitHub web editor both go around it.
  * anything outside a numbered entry. The file's preamble documents the convention and is
    legitimately edited; so is the entry `run` is appending, which has no earlier text.
  * a release number declared twice at either ref. There is no way to say which of two
    `## v2.34` entries answers to which, so both are skipped.

**The override is a commit trailer**, `Release-Body-Edit: v2.59`, on any commit the branch
adds. Fixing a typo in a shipped entry is legitimate and rare, and this makes it a deliberate,
reviewer-visible line rather than a `--no-verify`. It is scoped to the range `base..branch`,
so it expires the moment the edit lands: nothing carries a permanent exemption forward. A real
trailer, in the trailer block git parses — the refusal above ends with a pasteable copy of the
line, and a commit body quoting the refusal is not consent to it.

## Exit codes

0 = go · 2 = STOP, a human decides.
Deliberately the same scheme as `scripts/migration_reconcile.py`, and for the same reason:
a caller consuming 0/2 reads Python's uncaught-exception 1 as "unknown", so every refusal
here is an explicit 2 with a sentence, never a traceback.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import re
import select
import shlex
import signal
import subprocess
import sys
import time
import tomllib
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

#: Paths whose change means the running board changed, and therefore that the served
#: version has to move with the release. `harness/`, `scripts/`, docs and tests do not.
BOARD_PATHS = ("app/", "migrations/")

#: A release as the CHANGELOG spells it: two components, with the minor optional because
#: this repo's first two releases are `## v1` and `## v2` and its next major will be `v3`.
Release = tuple[int, int]

#: `(?![\w.])` rather than `\b`, because `\b` fires between a digit and a dot: `## v2.33.1`
#: would parse as release (2, 33), and the number handed out would then be v2.34 for a file
#: that already has one. A three-component heading is not a release entry this tool knows how
#: to number, so it must not match at all; the refusal that follows says so in a sentence.
_END = r"(?![\w.])"
_V = rf"v(\d+)(?:\.(\d+))?{_END}"

#: `## v2.33 — …`. Anchored at line start on a level-2 heading, which is what both
#: CHANGELOG.md and README.md use for a release entry.
_HEADING = re.compile(rf"^##[ \t]+{_V}", re.MULTILINE)

#: Where one release entry ENDS: the next top- or second-level heading, whatever it says.
#: `## v2.58` and a stray `## Notes` both close the entry above them, and `###` and below do
#: not — sub-headings are how a long entry is structured and `changelog.d` requires them to be
#: `###` or deeper for exactly this reason. Deliberately not `_HEADING`: an entry that ended
#: only at the next NUMBERED heading would swallow anything written above it, so the newest
#: released entry's body would change every time a release was appended.
_SECTION = re.compile(r"^\#{1,2}[ \t]", re.MULTILINE)

#: Where the part of CHANGELOG.md that no branch writes BEGINS: the first second-level
#: heading, whatever it says. Deliberately not `_HEADING` — see `_entries_from`.
_ANY_SECTION = re.compile(r"^\#\#[ \t]", re.MULTILINE)

#: `Release-Body-Edit: v2.59` — the one sanctioned way to edit a shipped entry, written as a
#: git TRAILER on a commit of the branch making the edit. It lives in a commit message rather
#: than in a file on purpose: a file would accumulate a permanent exemption per typo ever
#: fixed, and would itself be editable by the same careless merge resolution this check exists
#: to catch.
#:
#: A trailer, and never merely a line that looks like one — which is why git's own parser
#: answers this rather than a regex over the message. The refusal below ENDS with a
#: ready-to-paste `Release-Body-Edit: v2.59`, so a commit body quoting the refusal it just
#: got is not a hypothetical: it is the most likely message this branch will ever produce,
#: and reading it as consent would waive the entry on the strength of a paste.
_BODY_EDIT_KEY = "Release-Body-Edit"

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


class ReleaseError(Exception):
    """A refusal with a sentence attached. Always exits 2, never 1."""


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
        raise ReleaseError(
            f"{what} is not valid UTF-8 ({e.reason} at byte {e.start}). This tool reads and "
            "rewrites text; it will not guess an encoding for a file it is about to change"
        ) from e
    except OSError as e:
        raise ReleaseError(f"cannot read {what}: {e.strerror or e}") from e


def _write(path: Path, text: str, what: str) -> None:
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        raise ReleaseError(f"cannot write {what}: {e.strerror or e}") from e


# ---------------------------------------------------------------------------- git


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed: {proc.stderr.strip() or 'no output'}")
    return proc.stdout


def _git_ok(repo: Path, *args: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    ).returncode == 0


def _git_maybe(repo: Path, *args: str) -> str:
    """`_git` for a question whose answer may legitimately be "nothing".

    `git config --get` and `git symbolic-ref` both exit non-zero for an unset key rather than
    printing an empty line, and `_git`'s refusal is right for a command that was supposed to
    work. Here the empty answer IS an answer, and turning it into a STOP would refuse a fresh
    clone that has simply never run `git remote set-head`.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


#: A release TAG, spelled exactly the way `fmt` spells a release and anchored at both ends.
#: Public because `scripts/release_tag.py` reads it: "which tags are release tags" has to
#: have one answer, and a second copy of this pattern agrees with it until the day it does
#: not — at which point a tag exists for a number no document explains.
#: `v2.96-rc1`, `v2.96.1` and `salvage/issue-85` are somebody else's refs: this file has no
#: opinion about tags it did not issue, and a repo is allowed to have others.
TAG_NAME = re.compile(r"^v(\d+)(?:\.(\d+))?$")


def tag_releases(repo: Path) -> dict[Release, str]:
    """Release numbers held by a git tag, as {release: the commit sha it names}.

    The counter this file hands numbers out of has always been read from ONE place — the
    CHANGELOG headings at `--onto` — and that reading is the defect its own header names:
    two landers seconds apart read the same file and get the same answer, because a file
    read is not a lock. A tag is. Creating `refs/tags/v2.96` on a remote succeeds for
    exactly one caller (`scripts/release_tag.py reserve`), so a number a tag holds is one
    somebody has already been given, whether or not their branch has landed yet.

    Read from refs and from nothing else, which keeps this file's promise that every answer
    is computed from git objects rather than from a service. The corollary is that it is
    only as fresh as the checkout: a reservation lives on a commit that is not reachable
    from `main`, and `git fetch <remote>` follows tags only into history it fetched, so
    `git fetch origin --tags` is what makes a sibling's reservation visible here. When it
    has not been run this simply falls back to what it always did, and the atomic create in
    `release_tag.py reserve` is what still refuses the second lander.

    Peeled with `%(*objectname)` so an ANNOTATED tag reports the commit rather than the tag
    object: `backfill` writes annotated tags, and comparing a tag object's sha with a commit
    sha reports every one of them as pointing somewhere unrelated.
    """
    out = _git(repo, "for-each-ref",
               "--format=%(refname:strip=2) %(objectname) %(*objectname)", "refs/tags/")
    found: dict[Release, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        m = TAG_NAME.match(parts[0])
        if m:
            found[release(m.group(1), m.group(2))] = parts[2] if len(parts) > 2 else parts[1]
    return found


#: Case-insensitive, because `git ls-files -- '*.md'` is not: on Linux a `README.MD` or a
#: `NOTES.Md` is simply not in the scan, which is the same silent gap the untracked and
#: symlink warnings exist to close, for the cost of one pathspec magic word.
_MARKDOWN = ":(icase)*.md"


def resolve(repo: Path, ref: str) -> str:
    """`ref` as a commit SHA, resolved ONCE and passed around from then on.

    Every question this tool asks of the base — what the CHANGELOG says there, what this
    branch changed relative to it, whether it carries an unstamped placeholder — has to be
    asked of the same commit. Re-resolving the NAME each time means a concurrent push during
    a long `apply` can have the release number computed against one base and the
    served-version inference computed against another, with nothing anywhere noticing.
    """
    if not _git_ok(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"):
        raise ReleaseError(
            f"ref {ref!r} does not exist here. Fetch it first — the number this tool hands "
            "out is only correct relative to the ref you are merging into"
        )
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def merge_base(repo: Path, onto: str, branch: str = "HEAD") -> str:
    """The commit this branch forked from, as a SHA.

    `branch` defaults to HEAD because every command that stamps is reasoning about the
    worktree it is standing in. `collision` passes a ref instead: a pre-push hook judges a
    COMMIT, which need not be checked out anywhere, and the fork point of a commit nobody has
    out is exactly as well defined.

    Run through a raw `subprocess` rather than `_git`, because `git merge-base` exits 1 when
    there is no common ancestor and `_git` turns any non-zero exit into "git merge-base failed:
    <stderr>" — which is empty in that case. The sentence below is the one a reader can act
    on, and routing through `_git` made it unreachable.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "merge-base", onto, branch],
        capture_output=True, text=True, check=False,
    )
    out = proc.stdout.split()
    if proc.returncode != 0 or not out:
        raise ReleaseError(
            f"{onto} and {branch} have no common ancestor, so there is no base to compute "
            "this branch's changes against. Fetch the ref you are actually merging into"
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


def _line_of(text: str, offset: int) -> tuple[int, str]:
    """`(1-based line number, the line stripped)` for a match offset."""
    line_no = text.count("\n", 0, offset) + 1
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    return line_no, text[start : end if end != -1 else len(text)].strip()


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


def release_entries(text: str, where: str = "") -> dict[Release, str]:
    """Every released entry as a whole SLAB: its heading line and everything under it.

    Verbatim out of the original text — offsets are located in the masked copy and sliced out
    of the real one, which `mask_code` guarantees is safe by preserving length. Verbatim is
    the point: this is the only thing in this file that reads what an entry SAYS rather than
    what number it carries, and a normalising read (stripping, re-wrapping, comparing line
    sets) would pass the exact corruption it exists to catch, which was a body MOVED intact
    from one heading to another.

    The heading line is part of the slab. A released entry's title is as shipped as its prose
    and `## v2.59 — a row key the dashboard can actually tell apart` is not a line a branch
    has any business rewriting either. Note that this is the opposite call from `_collision`,
    which deliberately does NOT compare heading text — but it is answering a different
    question there ("is this a number this branch CLAIMED"), where a retitled old entry is a
    false positive with a nonsensical repair. Here a retitled old entry is precisely the
    finding, and the trailer is how a deliberate one says so.

    A number declared twice is omitted rather than guessed at: there is no way to say which
    of two `## v2.34` entries corresponds to which, and a duplicate heading is already
    `_collision`'s refusal with a repair attached. Passing it through as "unchanged" would be
    a lie; refusing on it here would report the same defect twice in different words.
    """
    masked = mask_code(text, where)
    ends = [m.start() for m in _SECTION.finditer(masked)]
    starts: dict[Release, int] = {}
    seen: Counter[Release] = Counter()
    for m in _HEADING.finditer(masked):
        rel = release(m.group(1), m.group(2))
        seen[rel] += 1
        starts.setdefault(rel, m.start())
    return {
        rel: text[off:next((e for e in ends if e > off), len(text))]
        for rel, off in starts.items()
        if seen[rel] == 1
    }


def entry_names(text: str, where: str = "") -> list[str]:
    """Every release ENTRY heading in file order, spelled the way the file spells it.

    `releases_in` in string form, for `scripts/readme_releases.py` — which renders the
    README's release list from the CHANGELOG's order and wants labels, not tuples. Kept here
    rather than there so the answer to "what is a release heading" stays in one place.
    """
    masked = mask_code(text, where)
    return [fmt(release(m.group(1), m.group(2))) for m in _HEADING.finditer(masked)]


def duplicates_in(text: str, where: str = "") -> list[Release]:
    """Release numbers this file declares more than once, sorted.

    Only `run` writes a heading now and it reads the file first, so this is a corruption
    check rather than a race: a "keep both sides" resolution of a CHANGELOG conflict is a
    perfectly clean merge that ships one number describing two different releases, and every
    heading in it is present, unique-looking and correctly ordered.
    """
    found = releases_in(text, where)
    return sorted({r for r in found if found.count(r) > 1})


def next_release(text: str, major_bump: bool = False, where: str = "",
                 also: Iterable[Release] = ()) -> Release:
    """One past the highest heading in `text`, or in `also`, whichever is higher.

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

    `also` is every number a TAG already holds (`tag_releases`). Headings say what has
    LANDED; tags say what has been ISSUED, and between a sibling's `reserve` and its merge
    those two disagree by exactly the number this file was written because two branches kept
    taking. It folds into the same `max` rather than being checked separately, because "the
    next free number" has one answer and a second code path computing it is how the two
    drift apart. Empty `also` — no tags, or a checkout that has not fetched them — leaves
    the behaviour precisely as it was.

    An unparseable CHANGELOG is still the refusal even when tags are present. A repo whose
    release headings this tool cannot read is not one it should be handing numbers out for,
    and tags alone would let it do that silently.
    """
    found = releases_in(text, where)
    if not found:
        raise ReleaseError("no `## vX.Y` headings at the base ref — CHANGELOG.md is not the "
                         "file this tool thinks it is, or the ref is wrong")
    major, minor = max([*found, *also])
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
        raise ReleaseError(
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


# ------------------------------------------------------------------------- the plan


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
            raise ReleaseError(
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
        raise ReleaseError(
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
    except ReleaseError:
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
        raise ReleaseError(
            f"pyproject.toml's `[project]` table resolves to version {declared!r}, but "
            f"{detail}. This tool rewrites that line by byte offset and will not guess which "
            "text is the package version — look for a multi-line string, a line that only "
            "looks like a table header, or a version that is not three components"
        )
    return found


#: How far back the repair walk looks for a tree with no placeholder in it, counting the ref
#: it starts from. Bounded because of the case it is actually for: an unstamped entry that
#: landed a long time ago and was never repaired leaves a RUN of consecutive ancestors all
#: carrying it, and each one costs a `git grep` to rule out. A history that predates the
#: placeholder scheme is the cheap case, not the expensive one — the first commit the walk
#: looks at has no placeholder and it stops there.
_REPAIR_WALK_MAX = 50


#: One finding: (release, what happened, the first line that differs or None).
Drift = tuple[Release, str, str | None]


def _first_difference(before: str, after: str) -> str | None:
    """The first line the two slabs disagree on, as the reader will recognise it.

    A refusal that only names `v2.59` sends somebody to read two 130-line entries side by
    side. One line is enough to tell "the whole body was replaced" from "a word was
    rewrapped", which are the two shapes this catches and they want different repairs.
    """
    old, new = before.split("\n"), after.split("\n")
    for i in range(max(len(old), len(new))):
        was = old[i] if i < len(old) else "<end of entry>"
        now = new[i] if i < len(new) else "<end of entry>"
        if was != now:
            return f"line {i + 1}: was {was.strip()!r}, now {now.strip()!r}"
    return None


def _body_edit_exemptions(repo: Path, base: str, branch: str) -> set[Release]:
    """Releases a commit in `base..branch` says out loud it meant to edit.

    Read from the RANGE and never from the whole history, so the exemption expires with the
    merge it was written for: once the edit is on main, the base moves past the commit
    carrying the trailer and the entry is immutable again for everybody after. That is what a
    file of stored exemptions could not do, and the reason there is no such file.

    Never fatal, and it fails CLOSED. A range git will not walk — an odd ref pair, a repo
    mid-rebase, a git too old for `%(trailers:…)` — yields no exemptions, which is the same
    answer as a branch that declared none: the entry stays frozen and the refusal still names
    the trailer that would have applied. The other direction, treating an unreadable range as
    blanket consent, is a waiver granted by a tool failure.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "log",
         f"--format=%(trailers:key={_BODY_EDIT_KEY},valueonly)", f"{base}..{branch}"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return set()
    found: set[Release] = set()
    for token in re.split(r"[,\s]+", proc.stdout):
        named = re.fullmatch(rf"{_V}", token.strip())
        if named:
            found.add(release(named.group(1), named.group(2)))
    return found


def _frozen(before: dict[Release, str], after: dict[Release, str],
            unalignable: set[Release]) -> list[Drift]:
    """Every released entry that is not what it was, newest first.

    Two shapes, and the second is the worse one: an entry whose text CHANGED, and an entry
    that is GONE. Both arrived by the same door on `feat/issue-232` — a merge resolution that
    relocated the branch's own 133-line entry under `## v2.59`, replacing that release's
    notes — and every guard in this repo passed, because they all read the file as a list of
    headings and the heading was still there and still in order (#325).

    Deleting a released heading outright is not caught by anything either: `duplicates_in`
    counts repeats, `next_release` takes a maximum, and neither notices that v2.59 has
    stopped existing. It costs one branch of an `if` to catch here, so it is caught here.

    Only entries present at BOTH ends are compared. A number the branch adds is the release
    it is shipping and has no prior text to be identical to; one it removes is reported as
    gone. Nothing outside a `## vX[.Y]` entry is looked at at all — the file's preamble is
    living documentation of the convention and is edited on purpose.

    `unalignable` is the numbers declared twice at either ref, and it has to be subtracted
    rather than left to `release_entries` dropping them: dropped from the AFTER map alone,
    a "keep both sides" resolution reads as the entry having VANISHED, which is a confident
    refusal naming the wrong defect. The caller reports them as uncompared instead.
    """
    out: list[Drift] = []
    for rel in sorted(before, reverse=True):
        if rel in unalignable:
            continue
        if rel not in after:
            out.append((rel, "gone", None))
        elif after[rel] != before[rel]:
            out.append((rel, "changed", _first_difference(before[rel], after[rel])))
    return out


def _show(repo: Path, ref: str, path: str, named: str | None = None) -> str:
    """`ref:path`, with a sentence rather than a git error when the file is missing.

    The REF's existence is not re-checked: the only caller passes a SHA `resolve()` has
    already verified with the identical `rev-parse --verify`, so a second one was a
    subprocess per run guarding a branch nothing could reach. `named` stays, because the
    message below has to name the ref the operator typed rather than the SHA it became.
    """
    label = named or ref
    if not _git_ok(repo, "cat-file", "-e", f"{ref}:{path}"):
        raise ReleaseError(
            f"{label} has no {path}. The release number is read from that file at that ref "
            "and from nothing else, so there is no number to hand out — check `--onto`"
        )
    return _git(repo, "show", f"{ref}:{path}")


# ---------------------------------------------------------------------- the commands


def cmd_frozen(args: argparse.Namespace) -> int:
    """Refuse when a branch has rewritten or deleted a release that had already shipped.

    Asked of two refs, like `collision`, and for the same reason: the thing being judged is a
    COMMIT, and a pre-push hook or a CI job need not have it checked out. Also like
    `collision`, it is FORK-RELATIVE — the comparison is against the merge base, not against
    `--onto` itself, so a branch that is merely behind is judged against what it forked from
    and never against entries that landed while it was open. A branch that does not touch the
    released part of the file passes by construction, which is what lets this one run on
    every pull request without ever crying wolf.

    An unstamped `## vNEXT` is invisible here — it carries no number, so it is in neither
    map. So is the entry this branch is shipping. There is nothing to switch off.
    """
    repo = Path(args.repo).resolve()
    onto_sha = resolve(repo, args.onto)
    branch_sha = resolve(repo, args.branch)
    payload: dict[str, object] = {
        "onto": args.onto,
        "onto_sha": onto_sha,
        "branch": args.branch,
        "branch_sha": branch_sha,
    }

    # The same "cannot tell" as `_releases_at_fork`, reported rather than guessed at. Without
    # a fork point there is no text to be identical TO: comparing against `--onto` itself
    # would report every entry that landed while this branch was open as one the branch
    # deleted, which is a gate that refuses correct work and is off within the week.
    try:
        base = merge_base(repo, onto_sha, branch_sha)
    except ReleaseError:
        base = ""
    # `base == branch_sha` is a fork point that IS the branch: everything it carries is
    # already contained in `--onto`, so every entry is identical to itself and the check has
    # judged nothing. Reported as limited rather than as a clean bill, because the two look
    # identical from outside and one of them is a gate covering nothing. It is the normal
    # state of a run on `main` after the merge landed — which is the blind spot `pre-push`
    # covers, by asking before the push while `origin/main` is still behind — and it is also
    # what a depth-1 CI checkout produces, where `origin/main` and HEAD are the same grafted
    # commit and there is no history to find a real fork point in.
    if not base or base == branch_sha \
            or not _git_ok(repo, "cat-file", "-e", f"{base}:CHANGELOG.md"):
        payload |= {"ok": True, "fork_point": "unreadable", "base": base or None,
                    "compared": 0, "changed": [], "exempt": [], "skipped": []}
        why = (
            f"{args.branch} is already contained in {args.onto}, so its fork point is itself"
            if base == branch_sha else
            f"no merge base with {args.onto} (or no CHANGELOG.md there)"
        )
        if args.json:
            print(json.dumps(payload, indent=2))
            return 0
        print(
            f"limited: {why}, so there is no shipped text for {args.branch}'s released "
            "entries to be compared with. Nothing was checked.",
            file=sys.stderr,
        )
        print(f"ok: 0 released entries compared with {args.onto}")
        return 0

    before_text = _git(repo, "show", f"{base}:CHANGELOG.md")
    # The whole file gone is the largest version of the defect this exists for, and reporting
    # it as eighty-five entries each individually "gone" buries the one fact that matters. The
    # other direction — a base with no CHANGELOG and a branch that adds one — is the repo
    # growing its first release notes, and is handled by the `limited:` path above.
    if not _git_ok(repo, "cat-file", "-e", f"{branch_sha}:CHANGELOG.md"):
        shipped = len(release_entries(before_text, f"{base}:CHANGELOG.md"))
        raise ReleaseError(
            f"{args.branch} has no CHANGELOG.md at all, and {base[:12]} — the commit it "
            f"forked from — has {shipped} released entries in one. Every release note this "
            "repo has ever written is in that file and nowhere else. If the file genuinely "
            f"moved, this check has to move with it (`git show {base[:12]}:CHANGELOG.md`)"
        )
    after_text = _show(repo, branch_sha, "CHANGELOG.md", args.branch)
    before = release_entries(before_text, f"{base}:CHANGELOG.md")
    after = release_entries(after_text, "CHANGELOG.md")
    unalignable = {*duplicates_in(before_text, f"{base}:CHANGELOG.md"),
                   *duplicates_in(after_text, "CHANGELOG.md")}
    drifted = _frozen(before, after, unalignable)
    exempt = _body_edit_exemptions(repo, base, branch_sha) if drifted else set()
    refused = [d for d in drifted if d[0] not in exempt]
    # The trailers that actually EXCUSED something. A branch may name a release it then left
    # alone — a fix that turned out not to be needed — and reporting that as a waiver would
    # tell a reader a shipped entry had been edited when none had.
    waived = sorted({d[0] for d in drifted if d[0] in exempt}, reverse=True)

    payload |= {
        "fork_point": "read",
        "base": base,
        "compared": len(before) - len(unalignable & set(before)),
        "changed": [{"release": fmt(r), "what": what, "where": where}
                    for r, what, where in refused],
        "exempt": [fmt(r) for r in waived],
        "skipped": sorted((fmt(r) for r in unalignable), reverse=True),
    }

    if refused:
        named = ", ".join(fmt(r) for r, _, _ in refused)
        detail = "\n".join(
            f"  {fmt(r)} is {what}" + (f" ({where})" if where else "")
            for r, what, where in refused
        )
        payload["ok"] = False
        message = (
            f"{named} already shipped, and {args.branch} does not carry the same text for "
            f"{'them' if len(refused) > 1 else 'it'} as {base[:12]}, the commit it forked "
            f"from:\n{detail}\n"
            "A released entry is immutable — it says what was broken before that release, "
            "which is the part no diff recovers. The way this happens is a CHANGELOG "
            "conflict resolved by moving a body under the wrong heading, which leaves every "
            "heading present and correctly ordered and every other check green (#325).\n"
            f"Read what shipped:  git show {base[:12]}:CHANGELOG.md\n"
            "Deliberate (a typo in a shipped entry)? Say so on a commit of this branch, "
            f"where a reviewer sees it:  Release-Body-Edit: {fmt(refused[0][0])}"
        )
        if args.json:
            payload["refusal"] = message
            print(json.dumps(payload, indent=2))
        else:
            print(f"STOP: {message}", file=sys.stderr)
        return 2

    payload["ok"] = True
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    if unalignable:
        # Not a refusal and not silence. `collision` owns this state and names a repair for
        # it; what this has to do is say which entries it therefore did not read, or the
        # `ok:` line below claims cover it does not have.
        print(
            "uncompared: " + ", ".join(sorted((fmt(r) for r in unalignable), reverse=True))
            + " is declared twice, so there is no saying which entry answers to which. "
            "`collision` refuses that state and names the repair.",
            file=sys.stderr,
        )
    if waived:
        # stderr and worded as a waiver rather than folded into the ok: line. An entry edited
        # under an exemption is still an edited release, and the one place that has to be
        # unmissable is the push or the job that let it through.
        print(
            "waived: " + ", ".join(fmt(r) for r in waived)
            + " changed and a Release-Body-Edit trailer on this branch says that was meant.",
            file=sys.stderr,
        )
    print(f"ok: {payload['compared']} released entries unchanged since {base[:12]}")
    return 0


# ------------------------------------------------------------- the one human decision
#
# Everything else this file does is a reading. The number is `max(headings at --onto) + 1`,
# the served-version move is inferred from the paths the branch touched, and both are
# recomputed the same way by anybody who runs the tool. `--major` is the single input that is
# not a reading of anything: whether v3 or v2.100 follows v2.99 is a statement about what the
# release MEANS, and this file's docstring has said so since it was written.
#
# It said so and then took the flag from whoever typed it. On 2026-08-23 a prompt asked for
# "v2.99, v3.00, v3.01" — `major.minor` here is two integers and not a decimal, so v2.99 is
# followed by v2.100 and the sequence in that sentence does not exist. The lander flagged that
# it had a choice, cited the instruction, took `--major`, and reported it plainly. It behaved
# well. Nothing between a typo and six releases plus their pushed tags asked whether a person
# had meant it, and #325/#341 are why the answer is not being rewritten now.
#
# So the flag is no longer sufficient authority for what the flag does. This is the shape #85
# settled for labels ("the label that authorises work has to come from someone who is not the
# worker") and #55/v2.96 settled for the panel's ceilings ("a repo cannot be allowed to raise
# its own ceiling"): a dial that changes how thoroughly one repo is reviewed is human-gated,
# and the major version of the software was a flag. That was the wrong way round.
#
# WHY A TERMINAL AND NOT THE BOARD. #386 lists a board authorisation behind `app.auth.human`
# as the strongest option and it is — but `human()` needs HUMAN_EDGE_SECRET, whose deploy is
# unconfirmed, and the deployed board answers 403 to it today (selfhost 160). A gate whose
# only path is a call nobody can currently satisfy is not a strict gate, it is an outage: the
# next genuine major would be hand-written outside this mechanism, which is the exact failure
# the flag exists to prevent. The terminal is the gate that works today and refuses today, it
# is the one "a live person is at the keyboard" test this repo already has (`qb-bump --apply`,
# #267), and a board authorisation can be added beside it as a second path the day the secret
# is confirmed without any of this changing.
#
# AND WHY IT REFUSES RATHER THAN WARNS. A warning printed into a log nothing reads renders an
# absent decision as a benign one, which is the whole class of failure this is about.
#
# WHAT IT DOES NOT STOP, said out loud the way `appetite.py` says its own: a local process can
# allocate a pty for itself and answer its own prompt (`printf 'v3\n' | script -qec ...`). Every
# gate that lives in the same process as its caller has that hole, `qb-bump`'s included, and
# closing it needs an authority outside the machine — which is what option 1 of #386 is for, the
# day the edge secret is confirmed. What this gate is sized for is the failure that actually
# happened: a version sequence mistyped in a prompt, a lander that read it as an instruction,
# and no point anywhere between the two at which anybody was asked. Going around it takes a
# deliberate act that no report survives; the thing it replaces took none at all.

#: Opened directly, and that is the gate rather than a detail of it. `sys.stdin.isatty()` asks
#: what this process's stdin happens to be plugged into, so a heredoc, a pipe or `yes |`
#: answers it. `/dev/tty` is the process's CONTROLLING terminal: a session that has none — an
#: agent harness, a CI runner, cron, `nohup` — cannot open it at all, whatever its stdin is.
#: Measured rather than assumed: under the harness that took v3 it fails with ENXIO before any
#: prompt is printed.
TERMINAL = "/dev/tty"

#: Seconds to wait for the answer before refusing. A controlling terminal is NOT proof of a
#: person: this repo runs its loops in tmux panes (`harness/loops/run-loop.sh`, `qb-seats`),
#: and a pane has a tty whether or not anybody is looking at it. Without this, the failure
#: there is not a wrong release but a `readline()` that never returns — a loop wedged forever
#: on a prompt nobody can see. A timeout turns that into the same refusal as no terminal at
#: all, which is the direction this gate is allowed to fail in.
CONFIRM_TIMEOUT = 120.0

#: The harness's own word for "nothing is watching this run" (`harness_rules.unattended()`,
#: exported by `run-loop.sh`). Read here rather than imported: `scripts/` is a directory of
#: standalone tools with no harness on the path, and the string is the contract. Checked
#: BEFORE the terminal, because that is the case where a tty exists and means nothing.
UNATTENDED_ENV = "HARNESS_UNATTENDED"


def ask_the_terminal(prompt: str, timeout: float = CONFIRM_TIMEOUT) -> str:
    """Put `prompt` on the controlling terminal and read one line back from it.

    Raises OSError on every way this can fail to reach a person — no controlling terminal,
    a run that is not the terminal's foreground job, nobody answering inside `timeout`, a
    terminal that closed. Module-level and named without a leading underscore because it is
    the seam the tests drive: a suite has no terminal either, and a gate nobody could
    exercise would be a gate nobody could show working.
    """
    # SIGTTIN ignored for the duration, and this is the guarantee rather than a belt to the
    # braces below. A read from a terminal by a process that is not in its foreground group
    # is answered by the kernel with SIGTTIN, whose default action STOPS the process — a hang
    # no timeout can rescue, because the stop lands inside the read. Ignoring it turns that
    # into EIO, which is an OSError, which is a refusal. The `tcgetpgrp` check below stays,
    # because it fires BEFORE the prompt is printed and says something a person can act on;
    # what it cannot do on its own is close the window between the check and the read, which
    # shell job control can move through.
    try:
        previous = signal.signal(signal.SIGTTIN, signal.SIG_IGN)
    except (ValueError, OSError):
        # Not the main thread, or a platform without the signal. Nothing to restore.
        previous = None
    fd = os.open(TERMINAL, os.O_RDWR)
    try:
        # A terminal this run is not the foreground job of is one nobody is sitting in front
        # of, whatever else is true of it: `release.py run --major &` is the way in.
        # Only ENOTTY is read as "there is no job control here, so there is no background to
        # be in" — every other errno is a descriptor this tool does not understand, and is
        # left to propagate into the refusal rather than waved through as permission.
        try:
            foreground = os.tcgetpgrp(fd) == os.getpgrp()
        except OSError as e:
            if e.errno != errno.ENOTTY:
                raise
            foreground = True
        if not foreground:
            raise OSError("this run is not the terminal's foreground job, so nobody is at it")

        # A loop, because `os.write` is allowed to write less than it was given — and the
        # part left behind would be the tail, which is where the number to type is.
        written, raw = 0, prompt.encode("utf-8")
        while written < len(raw):
            written += os.write(fd, raw[written:])

        # Read until a newline or the deadline, rather than once. A terminal is USUALLY in
        # canonical mode and hands over exactly one line, but the application that owns it
        # may have left it in raw mode, where `select` goes ready on the first keystroke. One
        # read there returns "v" and refuses a person who typed the right answer.
        deadline = time.monotonic() + timeout
        answer = b""
        while b"\n" not in answer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                os.write(fd, b"\n  no answer - refusing.\n")
                raise OSError(f"nothing answered the terminal within {timeout:.0f}s")
            chunk = os.read(fd, 4096)
            if not chunk:
                # EOF on the terminal itself — a ^D at the prompt. Not an answer, and
                # specifically not the empty string, which `confirm_major` would otherwise
                # reject with a message about having typed the wrong number.
                raise OSError("the terminal closed without answering")
            answer += chunk
    finally:
        os.close(fd)
        if previous is not None:
            signal.signal(signal.SIGTTIN, previous)
    return answer.decode("utf-8", "replace").strip()


def confirm_major(version: Release, instead_of: Release, onto_newest: Release,
                  typed: str | None = None) -> None:
    """Refuse `--major` unless a person types the number out loud.

    Typing the number rather than `y` is deliberate. `y` answers "did you mean to pass the
    flag", and the flag was never the mistake — the mistake was a version sequence that read
    correctly in a prompt. What has to be taken in is `v3, NOT v2.100`, and the only way to be
    sure that sentence was read is to make the answer repeat the half of it being chosen.

    Two places a person can answer from, and no third. `typed` is `--major-confirm`, which the
    release workflow's `workflow_dispatch` form collects — the same "type the number"
    discipline, in the one place a person is already deciding to cut a release; a wrong or
    absent answer there refuses exactly as a wrong answer at the terminal does. Otherwise the
    CONTROLLING TERMINAL is asked.

    `HARNESS_UNATTENDED=1` refuses BEFORE either, and that ordering is the whole point rather
    than an accident of layout. The environment variable is a declaration that nobody is
    watching, and a typed answer produced by a run that has declared that is a number an agent
    worked out, not a judgement a person made. Read after the confirmation it would be
    unreachable — `--major --major-confirm v4` would carry an unattended loop straight past
    the gate, which is #386 with an extra flag on it.

    A fragment field and a PR label were both considered and rejected. Either would put the
    judgement back on a branch — a worker deciding what the release MEANS while writing one
    change of several — which is the affordance #122 exists to remove.
    """
    def refuse(why: str) -> ReleaseError:
        return ReleaseError(
            f"--major would issue {fmt(version)} instead of {fmt(instead_of)}, and no person "
            f"confirmed it ({why}). Whether {fmt(version)} or {fmt(instead_of)} follows "
            f"{fmt(onto_newest)} is a statement about what the release MEANS — the one input "
            "to this tool no ref can answer, so it is a person's to make and not an "
            "unattended run's to assume (#386). Nothing was written. Either cut this as the "
            f"minor by dropping --major, or run `release.py run --major` at your own keyboard "
            f"and type {fmt(version)} when it asks — or, from the release workflow, put "
            f"{fmt(version)} in its `major` field"
        )

    if os.environ.get(UNATTENDED_ENV) == "1":
        raise refuse(f"{UNATTENDED_ENV}=1")
    if typed is not None:
        if typed.strip() not in {fmt(version), fmt(version).lstrip("v")}:
            raise refuse(f"--major-confirm asked for {fmt(version)} and read {typed!r}")
        print(f"confirmed by --major-confirm: {fmt(version)}, not {fmt(instead_of)}",
              file=sys.stderr)
        return
    prompt = (
        f"\n  {fmt(version)}, NOT {fmt(instead_of)}.\n"
        f"  The newest release at the base is {fmt(onto_newest)}, and `major.minor` here is "
        "two integers\n"
        f"  rather than a decimal — so the next MINOR after {fmt(onto_newest)} is "
        f"{fmt(instead_of)}, not {fmt(version)}.\n"
        "  A major says this release MEANS something different, and that is yours to say, "
        "not the\n"
        "  lander's (#386).\n"
        f"\n  Type {fmt(version)} to confirm, anything else to abort: "
    )
    try:
        answer = ask_the_terminal(prompt)
    except OSError as e:
        raise refuse(str(e)) from e
    # An exact match against the two spellings the prompt names, and not `lstrip("vV")` —
    # that strips a prefix of any length, so `vvvV3` would confirm v3. Surrounding whitespace
    # is forgiven because a person typing at a prompt is not a parser.
    if answer not in {fmt(version), fmt(version).lstrip("v")}:
        raise refuse(f"it asked for {fmt(version)} at the terminal and read {answer!r}")
    print(f"confirmed at the terminal: {fmt(version)}, not {fmt(instead_of)}", file=sys.stderr)


# ---------------------------------------------------------------- writing, or not at all


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
    it", which was false in the one scenario this helper exists to handle.

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
    except ReleaseError as e:
        failed = []
        for full, text in reversed(originals):
            try:
                if full.read_bytes() == text.encode("utf-8"):
                    continue
                full.write_text(text, encoding="utf-8")
            except OSError:
                failed.append(str(full))
        if failed:
            raise ReleaseError(
                f"{e} — and rolling back left {', '.join(failed)} rewritten. The release is "
                f"half written; `git checkout --` those paths"
            ) from e
        raise ReleaseError(f"{e} — nothing was written; the worktree is as you left it") from e
    return written


# ------------------------------------------------------------------- the release job

#: The files this tool generates. A branch that edits one is refused by `guard`, and the
#: refusal names the fragment path instead — decision (a) on #122: the consolidated history
#: stays in git so `git log CHANGELOG.md` keeps working offline, and the guard rather than
#: the file's absence is what removes the affordance, because nothing stops a branch creating
#: a file that does not exist.
GENERATED = ("CHANGELOG.md", "README.md § Releases")

#: What a refused branch is told to write instead. Spelled once: a refusal that says only
#: "no" gets retried or worked around, and both are worse than the original mistake.
FRAGMENT_PATH = "changelog.d/<issue>.<kind>.md"


def _siblings() -> tuple:
    """`changelog_fragments` and `readme_releases`, loaded by path, only when needed.

    Lazy because they import THIS module — the shapes of a release entry and of the README's
    list are defined here and neither should redefine them — and a module-level import in
    both directions is a cycle. `_sibling` hands back an already-loaded module rather than
    re-executing it, so the `ReleaseError` they raise is the one this file catches.
    """
    import importlib.util

    def _sibling(name: str):
        if name in sys.modules:
            return sys.modules[name]
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent
                                                      / f"{name}.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    sys.modules.setdefault("release", sys.modules[__name__])
    return _sibling("changelog_fragments"), _sibling("readme_releases")


@dataclass
class Cut:
    """What `run` would do. `preview` prints this and changes nothing."""

    version: Release | None = None
    title: str = ""
    fragments: list[str] = field(default_factory=list)
    previous: Release | None = None
    #: The highest number a release TAG holds. Reported beside `previous` rather than folded
    #: into it, because the two say different things — what the CHANGELOG declares, and what
    #: has been issued to anybody — and the day they disagree is a release that landed
    #: without its tag.
    tagged_newest: Release | None = None
    major: bool = False
    #: The number the OTHER bump would have issued, set only on a `--major` cut: v2.100 where
    #: this one says v3. Carried rather than recomputed at print time, because the whole of
    #: #386 is that a major is invisible in a prompt and obvious in a sentence naming what it
    #: is not.
    instead_of: Release | None = None
    serves: bool = False
    serves_reason: str = ""
    served_from: str = ""
    served_to: str = ""

    @property
    def cutting(self) -> bool:
        return self.version is not None

    def as_json(self) -> dict:
        return {
            "cutting": self.cutting,
            "version": fmt(self.version) if self.version else None,
            "title": self.title or None,
            "fragments": self.fragments,
            "previous": fmt(self.previous) if self.previous else None,
            "tagged_newest": fmt(self.tagged_newest) if self.tagged_newest else None,
            "major": self.major,
            "instead_of": fmt(self.instead_of) if self.instead_of else None,
            "serves": self.serves,
            "serves_reason": self.serves_reason,
            "served_from": self.served_from or None,
            "served_to": self.served_to or None,
        }


def default_branch(repo: Path) -> str:
    """The branch releases are cut on. Read from the remote's own HEAD, never assumed.

    `qb.baseBranch` overrides it, which is the same knob `harness/githooks/pre-push` reads,
    so a repo that renamed its integration branch says so once. The literal fallback is
    `main` and it is a fallback rather than a default: a checkout with no `origin/HEAD` and
    no config is one where this tool cannot prove which branch is which, and refusing there
    would refuse a fresh clone that has simply never run `git remote set-head`.
    """
    configured = _git_maybe(repo, "config", "--get", "qb.baseBranch")
    if configured:
        return configured
    head = _git_maybe(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    return head.removeprefix("origin/") if head else "main"


def _refuse_off_main(repo: Path) -> str:
    """Refuse unless this checkout is the integration branch, clean, and level with it.

    This is the whole of "no place to do it". `apply` used to run on a branch and every brief
    in the repo told a worker to run it, so every worker did, correctly, as instructed — and
    produced three conflicting pull requests and an orphaned tag in one night (#122). A rule
    against using a runnable command is a convention; a command that refuses is a mechanism.

    Three conditions, and each is a different way for the number to be wrong:

      * not on the integration branch — the number would be read from a CHANGELOG that has
        not seen every merge, and written onto a commit that is going to be rewritten;
      * a dirty tree — the release commit would carry somebody's work in progress;
      * behind (or ahead of) the remote — the number is `max(headings) + 1` and a stale
        checkout reads a stale max, which is exactly the collision this file exists to end.
    """
    branch = _git_maybe(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    want = default_branch(repo)
    if branch != want:
        where = f"`{branch}`" if branch else "a detached HEAD"
        raise ReleaseError(
            f"a release is cut on `{want}`, and this checkout is on {where}. Nothing stamps "
            f"on a branch: write `{FRAGMENT_PATH}` and let the release job number it after "
            "the merge, against the commit that actually exists. That is not a rule about "
            "this command — it is why this command refuses (#122)"
        )
    dirty = _git(repo, "status", "--porcelain", "--untracked-files=no").strip()
    if dirty:
        raise ReleaseError(
            f"`{want}` has uncommitted changes, so the release commit would carry them:\n"
            + "\n".join("    " + ln for ln in dirty.split("\n")[:10])
            + "\nCommit or stash them first — a release commit contains the release and "
            "nothing else."
        )
    remote_ref = f"origin/{want}"
    if not _git_ok(repo, "rev-parse", "--verify", "--quiet", f"{remote_ref}^{{commit}}"):
        raise ReleaseError(
            f"there is no `{remote_ref}` in this checkout, so there is no way to tell "
            f"whether `{want}` has every merge that has landed. `git fetch origin` first — "
            "the release number is one past the highest heading, and a stale checkout reads "
            "a stale highest"
        )
    head_sha, remote_sha = resolve(repo, "HEAD"), resolve(repo, remote_ref)
    if head_sha != remote_sha:
        behind = _git_ok(repo, "merge-base", "--is-ancestor", head_sha, remote_sha)
        raise ReleaseError(
            f"`{want}` is {'behind' if behind else 'not level with'} `{remote_ref}` "
            f"({head_sha[:9]} vs {remote_sha[:9]}). Run `git fetch origin && git pull "
            f"--ff-only` and cut the release from the commit everybody else has — the number "
            "is read from the CHANGELOG at HEAD, so a checkout missing a merge issues a "
            "number that merge is going to want"
        )
    return want


def _served_plan(repo: Path, cut: Cut, serve: bool | None, since: str,
                 since_label: str = "") -> None:
    """Decide and validate the served-version bump, before a byte is written.

    Validated here rather than at the point of writing: a repo whose `app/main.py` had moved
    its literal would otherwise get its CHANGELOG written and then a STOP — a half-cut
    release, which is worse than either outcome on its own and the state hardest to notice.
    """
    changed = changed_paths(repo, since)
    board = [p for p in changed if p.startswith(BOARD_PATHS)]
    where = since_label or since[:9]
    if serve is None:
        cut.serves = bool(board)
        cut.serves_reason = (
            f"inferred: {len(board)} board path(s) changed since {where}, first {board[0]}"
            if board
            else f"inferred: no {' or '.join(BOARD_PATHS)} path changed since {where}"
        )
    else:
        cut.serves = serve
        cut.serves_reason = (f"forced by --{'serve' if serve else 'no-serve'} "
                             f"(inference said {'yes' if board else 'no'})")
    if not cut.serves:
        return

    main_py, pyproject = _served_files(repo)
    if not main_py.exists():
        raise ReleaseError(
            "app/main.py does not exist, so there is no served version to bump. Pass "
            "--no-serve if this repo does not serve one")
    m = _SERVED_VERSION.search(_read(main_py, "app/main.py"))
    if not m:
        raise ReleaseError(
            'app/main.py has no `app = FastAPI(… version="X.Y.Z" …)` to bump — the version '
            "stopped being an inline literal in that call, so this tool cannot move it and "
            "must not pretend it did")
    if not pyproject.exists():
        raise ReleaseError(
            "pyproject.toml does not exist, so the package version cannot be moved with the "
            "served one. Pass --no-serve if this repo does not have one")
    pyproject_text = _read(pyproject, "pyproject.toml")
    found = pyproject_versions(pyproject_text)
    if len(found) != 1:
        table = "" if project_table(pyproject_text) else " (it has no `[project]` table)"
        raise ReleaseError(
            f'pyproject.toml has {len(found)} `version = "X.Y.Z"` lines in `[project]`'
            f"{table}, expected exactly 1 — refusing to guess which one the package version "
            "is")
    cut.served_from = m.group(1)
    cut.served_to = f"{cut.version[0]}.{cut.version[1]}.0"  # type: ignore[index]


def plan_cut(repo: Path, *, major: bool = False, title: str | None = None,
             serve: bool | None = None) -> Cut:
    """What the next release would be. Reads the worktree and git; writes nothing."""
    cf, _ = _siblings()
    cut = Cut(major=major)
    fragments = cf.load(repo)
    cut.fragments = [f.path.name for f in fragments]
    if not fragments:
        return cut

    if _linked(repo, "CHANGELOG.md"):
        raise ReleaseError(
            "CHANGELOG.md is a symlink, or sits under one. The release number is read from "
            "that file and written back into it, and a link points wherever it points — "
            "which may not be this repository. Replace it with a real file")
    changelog = repo / "CHANGELOG.md"
    if not changelog.exists():
        raise ReleaseError("no CHANGELOG.md in this repo")
    text = _read(changelog, "CHANGELOG.md")

    duplicated = duplicates_in(text, "CHANGELOG.md")
    if duplicated:
        raise ReleaseError(
            "CHANGELOG.md declares " + ", ".join(fmt(r) for r in duplicated) + " more than "
            "once, so it does not say what the highest release is. Two entries under one "
            "number is a merge resolution that kept both sides; fix that before numbering "
            "anything on top of it")

    tags = tag_releases(repo)
    cut.tagged_newest = max(tags) if tags else None
    cut.previous = max(releases_in(text, "CHANGELOG.md"), default=None)
    cut.version = next_release(text, major, "CHANGELOG.md", also=tags)
    cut.instead_of = next_release(text, False, "CHANGELOG.md", also=tags) if major else None
    cut.title = cf.release_title(fragments, title)

    # The previous release's tag is what "changed since the last release" is measured
    # against. Its absence is a refusal rather than a silent `False`: a served version that
    # quietly fails to move ships a board whose `GET /openapi.json` reports the release
    # before it, with no diff anywhere to catch it.
    if serve is None:
        if cut.previous is None or cut.previous not in tags:
            raise ReleaseError(
                f"the previous release ({fmt(cut.previous) if cut.previous else 'none'}) has "
                "no tag in this checkout, so there is no ref to measure `did app/ or "
                "migrations/ change` against. `git fetch origin --tags`, or run "
                "`scripts/release_tag.py backfill --ref HEAD`, or decide it yourself with "
                "--serve / --no-serve")
        _served_plan(repo, cut, serve, tags[cut.previous], fmt(cut.previous))
    else:
        _served_plan(repo, cut, serve, tags.get(cut.previous, "HEAD"),
                     fmt(cut.previous) if cut.previous in tags else "HEAD")
    return cut


def _print_cut(cut: Cut, *, applied: bool) -> None:
    if not cut.cutting:
        print(f"no fragments in changelog.d/ — nothing to release. A branch writes "
              f"`{FRAGMENT_PATH}`; this is what folds them into a numbered release.")
        return
    verb = "issued" if applied else "would issue"
    line = f"{verb} {fmt(cut.version)} — {cut.title}"  # type: ignore[arg-type]
    if cut.major:
        line += f"  (--major, NOT {fmt(cut.instead_of)})"  # type: ignore[arg-type]
    print(line)
    print(f"  previous release: {fmt(cut.previous) if cut.previous else '(none)'}"
          + (f", newest tag {fmt(cut.tagged_newest)}" if cut.tagged_newest else ""))
    print(f"  fragments ({len(cut.fragments)}): " + ", ".join(cut.fragments))
    if cut.serves:
        print(f"  served version {cut.served_from} -> {cut.served_to} — {cut.serves_reason}")
    else:
        print(f"  served version unchanged — {cut.serves_reason}")


def cmd_preview(args: argparse.Namespace) -> int:
    """What `run` would do, from anywhere, changing nothing.

    Deliberately NOT gated on the branch, the tree or `--major`: asking what a release would
    be decides nothing, and the answer is what a person needs in front of them before they
    say yes. Its `--major` line naming the minor it is NOT is the sentence that makes the
    slip visible (#386).
    """
    cut = plan_cut(Path(args.repo).resolve(), major=args.major, title=args.title,
                   serve=args.serve)
    if args.json:
        print(json.dumps(cut.as_json(), indent=2))
    else:
        _print_cut(cut, applied=False)
    return 0


def _self_check_frozen(before: str, after: str) -> None:
    """Every entry that existed before this cut is byte-identical after it.

    `guard` refuses a branch that touches CHANGELOG.md at all, which leaves exactly one
    writer — this one — and an unwatched sole writer is how #325 happened in the first place.
    So the release job is held to the same rule it enforces: it may APPEND an entry and it
    may not alter one.
    """
    old, new = release_entries(before, "CHANGELOG.md"), release_entries(after, "CHANGELOG.md")
    changed = sorted(r for r, slab in old.items() if new.get(r) != slab)
    if changed:
        raise ReleaseError(
            "cutting this release would rewrite the text of " +
            ", ".join(fmt(r) for r in changed) + ", which has already shipped. A release "
            "entry records what was broken before it — the one part no diff recovers — so "
            "this appends and never edits. Nothing was written")


def cmd_run(args: argparse.Namespace) -> int:
    """Assemble, number, write, commit and tag. The only writer of a version number."""
    repo = Path(args.repo).resolve()
    branch = _refuse_off_main(repo)
    cf, rr = _siblings()

    cut = plan_cut(repo, major=args.major, title=args.title, serve=args.serve)
    if not cut.cutting:
        if args.json:
            print(json.dumps({**cut.as_json(), "written": [], "committed": None}, indent=2))
        else:
            _print_cut(cut, applied=False)
        return 0

    # Asked last: everything that could still refuse this release has refused by now, so a
    # person who confirms is not then told it was uncuttable all along. And asked here rather
    # than in `plan_cut`, so `preview --major` stays answerable anywhere — a gate that fires
    # where there is no decision is a gate people learn to get past.
    if cut.major:
        confirm_major(cut.version, cut.instead_of, cut.previous,  # type: ignore[arg-type]
                      typed=args.major_confirm)

    version = fmt(cut.version)  # type: ignore[arg-type]
    fragments = cf.load(repo)
    changelog_path, readme_path = repo / "CHANGELOG.md", repo / "README.md"
    changelog = _read(changelog_path, "CHANGELOG.md")
    readme = _read(readme_path, "README.md")

    new_changelog = cf.insert_entry(changelog, cf.entry(fragments, cut.title, version))
    _self_check_frozen(changelog, new_changelog)
    new_readme = cf.insert_bullet(readme, new_changelog, cut.title, version)

    edits: list[tuple[str, Path, str]] = [
        ("CHANGELOG.md", changelog_path, new_changelog),
        ("README.md", readme_path, new_readme),
    ]
    if cut.serves:
        main_py, pyproject = _served_files(repo)
        original = _read(pyproject, "pyproject.toml")
        found = pyproject_versions(original)
        if len(found) != 1:  # plan_cut already refused this; re-checked rather than assumed
            raise ReleaseError("pyproject.toml's version line changed between plan and run")
        offset = project_table(original)[0]  # type: ignore[index]
        m = found[0]
        at = offset + m.start()
        edits.append(("pyproject.toml", pyproject,
                      original[:at] + m.group(1) + cut.served_to + m.group("q")
                      + original[offset + m.end():]))
        text = _read(main_py, "app/main.py")
        served = _SERVED_VERSION.search(text)
        if not served:  # plan_cut already refused this; re-checked rather than asserted
            raise ReleaseError("app/main.py's version literal vanished between plan and run")
        edits.append(("app/main.py", main_py,
                      text[: served.start(1)] + cut.served_to + text[served.end(1):]))

    # The rollback runs to the COMMIT, not to `_write_all`'s last line. That helper puts back
    # the files it wrote and stops there, so a failure at the fragment unlink or at `git
    # commit` left the release written and its fragments consumed — a finished-looking release
    # nobody can find, and the one state harder to notice than either half alone (Codex).
    #
    # Restored from held text rather than from git. `git checkout --` would do it, and would
    # be a bigger hammer than this needs: these are files this function wrote seconds ago and
    # it still has both sides of every one.
    originals = [(f.path, f.path.read_text(encoding="utf-8")) for f in fragments]

    def undo() -> None:
        for path, text in originals:
            with contextlib.suppress(OSError):
                path.write_text(text, encoding="utf-8")
        for name, full, _ in edits:
            with contextlib.suppress(OSError, ReleaseError):
                _write(full, _read_before[name], name)

    _read_before = {name: _read(full, name) for name, full, _ in edits}
    written = _write_all(edits)
    consumed = [f"changelog.d/{name}" for name in cut.fragments]
    try:
        for f in fragments:
            f.path.unlink()
    except OSError as e:
        undo()
        raise ReleaseError(
            f"could not consume {e.filename}: {e.strerror}. Nothing was committed, and the "
            "files this run had already written have been put back") from e

    if args.no_commit:
        if args.json:
            print(json.dumps({**cut.as_json(), "written": written, "committed": None},
                             indent=2))
        else:
            _print_cut(cut, applied=True)
            print("\nwritten: " + ", ".join(written + consumed))
            print("--no-commit: nothing was committed or tagged.")
        return 0

    message = f"chore(release): {version} — {cut.title}"
    try:
        _git(repo, "add", "--", *[path for path, _, _ in edits], "changelog.d")
        # `--no-verify`: this commit is entirely this tool's own output, already checked by
        # everything above it, and the commit hooks in this fleet are for hand-written work.
        _git(repo, "commit", "--no-verify", "-m", message)
    except ReleaseError:
        _git_ok(repo, "reset", "--quiet", "--", ".")
        undo()
        raise

    sha = resolve(repo, "HEAD")
    _git(repo, "tag", "-a", version, "-m", message, sha)

    pushed = False
    if args.push:
        # ONE push, `--atomic`, so the commit and its tag arrive together or not at all.
        # Pushed separately, a tag push that failed left the release on `main` untagged — and
        # the `every release on main has a tag` job that would normally repair that does NOT
        # run here, because a push made with `GITHUB_TOKEN` triggers no workflows (Codex).
        _git(repo, "push", "--atomic", "origin",
             f"{sha}:refs/heads/{branch}", f"refs/tags/{version}")
        pushed = True

    if args.json:
        print(json.dumps({**cut.as_json(), "written": written, "committed": sha,
                          "tag": version, "pushed": pushed}, indent=2))
    else:
        _print_cut(cut, applied=True)
        print(f"\ncommitted {sha[:9]} `{message}`, tagged {version}"
              + (f", pushed to origin/{branch}" if pushed else " (not pushed: --no-push)"))
    return 0


# ------------------------------------------------- the consolidated files are OUTPUT


def _entries_from(text: str, where: str = "CHANGELOG.md") -> str:
    """Everything from the first release heading down — the part no branch writes.

    The preamble above it is deliberately outside: it documents the convention the file
    follows, it is edited when the convention changes (this release edits it), and a guard
    that froze it would refuse the one branch that had to fix it. That is the same line
    `frozen` draws — "anything outside a numbered entry" is its stated blind spot — and
    drawing it differently in two places would mean a branch refused by one and cleared by
    the other over the same edit.

    Inserting a release still lands INSIDE this region: the new entry sits above what used
    to be the first heading, so the region at the branch begins with text the base's does
    not have.

    ANY second-level heading opens it, not just a parseable `## vX.Y`. Anchoring on a release
    heading left a hole with a version number in it: `## v9.9.9` is three components, which
    `_HEADING` deliberately does not match, so a branch could prepend one and have the whole
    thing read as preamble. Codex found it. The preamble in this repo's CHANGELOG is a `#`
    heading and prose, so "the first `##`" is the same boundary for a correct file and a
    closed door for that one.
    """
    masked = mask_code(text, where)
    first = _ANY_SECTION.search(masked)
    return text[first.start():] if first else ""


def _releases_block(repo: Path, ref: str) -> str | None:
    """The README's release-list block at `ref`, or None if that ref has no README."""
    _, rr = _siblings()
    try:
        readme = _show(repo, ref, "README.md")
    except ReleaseError:
        return None
    try:
        start, end = rr.find_list(readme)
    except rr.ListError:
        # A README with no recognisable list is not this guard's finding to report: it is
        # `readme_releases.py check`'s, which says so in a sentence naming the heading. Here
        # it would mean refusing every branch in the repo over a file none of them touched.
        return None
    return readme[start:end]


def cmd_guard(args: argparse.Namespace) -> int:
    """Refuse a branch that edits a file the release job generates.

    Fork-relative, like `frozen` and for the same reason: a branch that is merely BEHIND has
    inherited whatever `main` did to these files and has touched nothing. Comparing against
    `--onto` itself would refuse every branch open while a release was cut, which is a gate
    switched off inside a week.

    The refusal names `changelog.d/<issue>.<kind>.md`. That is not politeness — a worker that
    is refused and not told where to write instead retries or works around it, and both are
    worse than the original mistake.
    """
    repo = Path(args.repo).resolve()
    onto_sha, branch_sha = resolve(repo, args.onto), resolve(repo, args.branch)
    payload: dict[str, object] = {"onto": args.onto, "onto_sha": onto_sha,
                                  "branch": args.branch, "branch_sha": branch_sha}
    try:
        base = merge_base(repo, onto_sha, branch_sha)
    except ReleaseError:
        base = ""
    if not base or base == branch_sha:
        why = (f"{args.branch} is already contained in {args.onto}, so its fork point is "
               "itself" if base == branch_sha else f"no merge base with {args.onto}")
        payload |= {"ok": True, "fork_point": "unreadable", "edited": []}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"limited: {why}, so there is nothing for {args.branch}'s generated files "
                  "to be compared with. Nothing was checked.", file=sys.stderr)
        return 0

    edited: list[str] = []
    changed = set(_git(repo, "diff", "--name-only", f"{base}..{branch_sha}").split("\n"))
    if "CHANGELOG.md" in changed:
        # The RELEASE ENTRIES, not the whole file — see `_entries_from`. A branch deleting
        # the file outright reads as an empty region, which differs from the base's and is
        # refused; that is the largest version of this defect and it must not be the one edit
        # that passes.
        try:
            before = _entries_from(_show(repo, base, "CHANGELOG.md"))
        except ReleaseError:
            before = ""
        after = ("" if not _git_ok(repo, "cat-file", "-e", f"{branch_sha}:CHANGELOG.md")
                 else _entries_from(_show(repo, branch_sha, "CHANGELOG.md")))
        if before != after:
            edited.append("CHANGELOG.md")
    if "README.md" in changed:
        before, after = _releases_block(repo, base), _releases_block(repo, branch_sha)
        # Only the release LIST. The README is 900 lines of prose a branch is meant to edit,
        # and refusing the whole file would make the guard a tax on documentation — which is
        # how a guard stops being installed.
        #
        # `before is not None and after is None` is the branch having BROKEN the list — the
        # heading removed, or every bullet gone — and it is a finding rather than a pass.
        # Reading it as "cannot tell" made deleting the block the one edit that got through,
        # which is the largest version of the defect wearing the smallest diff (Codex).
        # Neither side parsing is a repo that does not keep a release list, and stays silent.
        if before is not None and (after is None or before != after):
            edited.append("README.md § Releases")

    payload |= {"ok": not edited, "fork_point": base, "edited": edited}
    if args.json:
        print(json.dumps(payload, indent=2))
        return 2 if edited else 0
    if not edited:
        print(f"no generated release file edited since {args.onto}")
        return 0
    raise ReleaseError(
        f"{args.branch} edits " + " and ".join(edited) + ", which the release job "
        f"generates on `{default_branch(repo)}` and no branch writes.\n\n"
        f"    Write `{FRAGMENT_PATH}` instead — one file, named after your issue, that no\n"
        "    other branch will ever open. changelog.d/README.md has the format, and it is\n"
        "    the whole contract: one fragment, no version number, nothing else.\n\n"
        "    Two branches editing the top of CHANGELOG.md conflict every time, over nothing:\n"
        "    both entries are right and both belong, and git cannot know that two insertions\n"
        "    at one offset are independent (#122). The number is applied after the merge,\n"
        "    against the commit that actually exists.\n\n"
        "    Already committed the edit? `git checkout " + base[:9] + " -- "
        + " ".join(e.split(" ")[0] for e in edited) + "` and write the fragment."
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--repo", default=".", help="repo dir (default: cwd)")
        sp.add_argument("--json", action="store_true", help="machine-readable answer")

    def cutting(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--title", default=None,
                        help="the release heading (required past one fragment)")
        # Explicit and never inferred. Whether v2.34 or v3 follows v2.33 is a statement about
        # what the release MEANS, and no ref can answer it — but the flag has to exist, or
        # the next major is hand-written outside this mechanism, on the one release that most
        # needs not to be.
        sp.add_argument("--major", action="store_true",
                        help="issue the next MAJOR (v2.33 -> v3), not the next minor")
        serve = sp.add_mutually_exclusive_group()
        serve.add_argument("--serve", dest="serve", action="store_true", default=None,
                           help="bump the served version even if no board path changed")
        serve.add_argument("--no-serve", dest="serve", action="store_false",
                           help="leave the served version alone though a board path changed")

    def judging(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--onto", default="origin/main", help="the ref being merged into")
        sp.add_argument(
            "--branch", default="HEAD",
            help="the ref being judged (default: HEAD). A commit, not a worktree — this is "
            "what lets a pre-push hook judge what is being PUSHED rather than what happens "
            "to be checked out.",
        )

    pv = sub.add_parser("preview", help="what the next release would be (read-only, anywhere)")
    common(pv)
    cutting(pv)
    pv.set_defaults(func=cmd_preview)

    rn = sub.add_parser("run", help="cut the release: assemble, number, commit, tag (main only)")
    common(rn)
    cutting(rn)
    rn.add_argument("--major-confirm", default=None, metavar="vN",
                    help="confirm --major without a terminal by typing the number it issues "
                         "— what the release workflow's dispatch form collects")
    rn.add_argument("--no-commit", action="store_true",
                    help="write the files and stop; commit and tag nothing")
    rn.add_argument("--no-push", dest="push", action="store_false", default=True,
                    help="commit and tag locally; push neither")
    rn.set_defaults(func=cmd_run)

    gd = sub.add_parser(
        "guard",
        help="fail if a REF edits a file the release job generates (fork-relative)",
    )
    common(gd)
    judging(gd)
    gd.set_defaults(func=cmd_guard)

    fr = sub.add_parser(
        "frozen",
        help="fail if a REF rewrote or deleted a release that had already shipped",
    )
    common(fr)
    judging(fr)
    fr.set_defaults(func=cmd_frozen)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except ReleaseError as e:
        # Exit 2, not Python's uncaught-exception 1: a gate consuming the documented 0/2
        # scheme reads 1 as "unknown" rather than as "stop".
        print(f"STOP: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
