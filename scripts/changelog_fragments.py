#!/usr/bin/env python3
"""A branch writes `changelog.d/<issue>.<kind>.md`, and the release assembles them.

Every branch that shipped anything edited the SAME lines at the top of `CHANGELOG.md`, so
every pair of concurrent branches conflicted there — PR #268 hit it in the same session three
PRs were open. That conflict is not a disagreement about anything: both sides are right, both
entries belong, and git cannot know that two insertions at one offset are independent.

A fragment is one file per change, named after the issue, so no two branches ever touch the
same path and the conflict has nowhere to occur. It is the towncrier model:

    changelog.d/296.feat.md      # this branch's entry, naming no version
    changelog.d/298.fix.md       # a sibling branch's, in a file this one never opens

    changelog_fragments.py check      # are the fragments well formed?
    changelog_fragments.py assemble   # fold them into a `## vNEXT` entry and delete them
    changelog_fragments.py required   # did a branch that changes something WRITE one?

## Why `required` is here rather than left to the landing

Because nothing else could ask it. Every other guard in this repo verifies that what is
PRESENT is correct — `release_stamp.py check` asks whether a `## vNEXT` is unstamped,
`frozen` asks whether a shipped entry still says what it said — and to all of them a branch
that never wrote an entry looks exactly like one that wrote a correct one. #363 landed a new
module, sixty-seven tests and two public helpers with `changelog.d/` holding nothing but its
README, and every CI job was green; a landing agent noticed by hand (#365).

It runs on `pull_request` and is scoped so that a docs-only or test-only branch passes in
silence, because a check that fires on every PR and is usually wrong is switched off within a
week. `_exempt` is where that scoping lives and is the part to read.

## Why not towncrier itself

Judged rather than assumed, and the answer is this file, at a third of towncrier's config
surface. Towncrier renders an entry for a version it is TOLD; here the number is not known
until `release_stamp.py apply` resolves `vNEXT` against the ref being merged into, which is
after assembly. So towncrier would have to be driven with a placeholder version and its
`--draft`/`build` split reinterpreted, and this repo would own a second grammar for release
entries beside the one `release_stamp.py` already parses — two answers to "what is a release
entry", which is the defect this repo keeps writing changelog entries about. It also renders
one file, and half of what drifts here is the README's release list.

## What `vNEXT` means once fragments exist

Unchanged, and deliberately so: `## vNEXT — <title>` at the top of `CHANGELOG.md` is still
the unstamped release, still the only thing `release_stamp.py apply` rewrites, and still what
`harness/tests/test_release_numbers.py` asserts about. What changes is WHEN it appears. A
branch in flight writes a fragment and no CHANGELOG entry at all; `assemble` creates the
`vNEXT` entry at land time, out of every fragment present; `apply` then resolves it. Two
branches can both be in flight with no entry between them to conflict over.

A fragment that lands unassembled is not lost and is not an error — it is swept into the next
release's entry, which is what a release IS: everything since the last one. The state to
avoid is assembling twice, so `assemble` refuses when `CHANGELOG.md` already carries an
unstamped entry rather than writing a second one, which would trip
`test_at_most_one_release_is_unstamped` after the commit rather than before it.

Hand-writing the `## vNEXT` entry still works and is still what the CHANGELOG's own
convention paragraph describes. Fragments are the way to avoid the conflict, not a new
requirement — nothing here refuses a branch that edits `CHANGELOG.md` directly.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_SCRIPTS = Path(__file__).resolve().parent


# `scripts/` holds standalone tools rather than an importable package, so its siblings are
# loaded by path. Both are imported for the same reason: the shapes of a release entry and of
# the README's list are defined once, where they are already defined.
def _sibling(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


rs = _sibling("release_stamp")
rr = _sibling("readme_releases")

#: Where fragments live. One directory, so "what is in flight" is `ls`.
FRAGMENT_DIR = "changelog.d"

#: The kinds a fragment may declare, in the order they are assembled. Taken from this repo's
#: own commit prefixes rather than invented, so the kind in a filename is the word already in
#: the commit subject and nobody has to learn a second vocabulary.
KINDS = ("feat", "fix", "perf", "refactor", "docs", "test", "chore", "ci", "revert")

#: `296.feat.md`, or `+worktree-db.fix.md` for a change with no issue. The `+` form is
#: towncrier's and is kept because the alternative is a made-up issue number in a filename,
#: which reads as a real one forever after.
_NAME = re.compile(r"^(?P<issue>\d+|\+[A-Za-z0-9][A-Za-z0-9._-]*)\.(?P<kind>[a-z]+)\.md$")

#: Files in `changelog.d/` that are not fragments. The directory documents itself, and a
#: reader that swept its own README into a release entry would be a funny way to find out.
_NOT_FRAGMENTS = frozenset({"README.md"})

#: A fragment's title: a level-1 heading on the first non-blank line.
_TITLE = re.compile(r"^#[ \t]+(?P<title>\S.*?)[ \t]*$")

#: A heading a fragment may not contain. `#` opens a document and `##` opens a RELEASE — an
#: assembled fragment carrying one would split the release it was folded into, and the split
#: would look like a second release to `release_stamp.py`. `###` and below are how the
#: CHANGELOG already sections a long entry and are left alone.
_TOP_HEADING = re.compile(r"^#{1,2}[ \t]+\S", re.MULTILINE)

#: The placeholder, in a file whose whole argument is that it names no version. Matched on
#: MASKED text, so a fragment may still write `` `vNEXT` `` while discussing the convention —
#: which entries in this repo do, at length. That is the stamper's own distinction: a token
#: inside a code span is documentation of the placeholder, not a use of it.
_PLACEHOLDER_MENTION = re.compile(rf"(?<![0-9A-Za-z]){rs.PLACEHOLDER}(?![0-9A-Za-z])")

#: The README wraps its release list at 100 columns with a two-space hanging indent.
_WRAP, _INDENT = 100, "  "


class FragmentError(Exception):
    """A fragment, or a tree of them, that this tool will not assemble."""


@dataclass(frozen=True)
class Fragment:
    path: Path
    issue: str
    kind: str
    title: str | None
    body: str

    @property
    def order(self) -> tuple[int, int, str]:
        """Kind first, then issue number. Deterministic, so two machines assembling the same
        fragments produce the same entry and a re-run is a no-op rather than a reshuffle."""
        numeric = int(self.issue) if self.issue.isdigit() else 0
        return KINDS.index(self.kind), numeric, self.issue


def parse_fragment(path: Path, text: str) -> Fragment:
    """One fragment, refusing every shape that would produce a broken release entry."""
    m = _NAME.match(path.name)
    if not m:
        raise FragmentError(
            f"{FRAGMENT_DIR}/{path.name} is not named `<issue>.<kind>.md` — for example "
            f"`296.feat.md`, or `+short-slug.fix.md` when there is no issue. Kinds: "
            + ", ".join(KINDS))
    kind = m.group("kind")
    if kind not in KINDS:
        raise FragmentError(
            f"{FRAGMENT_DIR}/{path.name} declares kind `{kind}`, which is not one of: "
            + ", ".join(KINDS)
            + ". The kinds are this repo's commit prefixes; add one here if the vocabulary "
            "really has grown, rather than spelling it differently in one file")

    lines = text.split("\n")
    title, consumed = None, 0
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is not None and (t := _TITLE.match(lines[first])):
        title = t.group("title")
        consumed = sum(len(ln) + 1 for ln in lines[:first + 1])
        lines = lines[first + 1:]
    body = "\n".join(lines).strip("\n")

    # Fenced blocks and code spans blanked, so an EXAMPLE of a release heading — which is how
    # a changelog entry explains this convention — is not read as one. Both checks below run
    # on it, and both would otherwise refuse the entry that documents them.
    masked = rs.mask_code(text, f"{FRAGMENT_DIR}/{path.name}")

    if not body.strip():
        raise FragmentError(
            f"{FRAGMENT_DIR}/{path.name} has no body under its title. A fragment IS the "
            "changelog entry — say what was broken or missing before this change, because "
            "that is the part no diff recovers")
    if _TOP_HEADING.search(masked, consumed):
        raise FragmentError(
            f"{FRAGMENT_DIR}/{path.name} contains a `#` or `##` heading in its body. Folded "
            "into CHANGELOG.md a `##` opens a RELEASE, so the entry would split in two and "
            "`release_stamp.py` would see a second one. Use `###` and below, which is how "
            "the CHANGELOG already sections a long entry")
    if _PLACEHOLDER_MENTION.search(masked):
        raise FragmentError(
            f"{FRAGMENT_DIR}/{path.name} mentions `{rs.PLACEHOLDER}`. A fragment names no "
            "version at all — that is the whole reason two branches can write one each "
            "without racing for a number. `assemble` writes the placeholder")
    return Fragment(path=path, issue=m.group("issue"), kind=kind, title=title, body=body)


def load(repo: Path) -> list[Fragment]:
    """Every fragment in `changelog.d/`, in assembly order.

    Sorted by name before parsing so that a tree with several bad fragments refuses on the
    same one every run — a refusal that moves between runs reads as flakiness.
    """
    directory = repo / FRAGMENT_DIR
    if not directory.is_dir():
        return []
    found = sorted(p for p in directory.iterdir()
                   if p.is_file() and not p.name.startswith(".")
                   and p.name not in _NOT_FRAGMENTS)
    return sorted((parse_fragment(p, p.read_text(encoding="utf-8")) for p in found),
                  key=lambda f: f.order)


def release_title(fragments: list[Fragment], given: str | None) -> str:
    """The title of the assembled release: `--title`, or the lone fragment's own.

    A release with several fragments has no title anywhere to derive one from. Concatenating
    the fragment titles would produce a heading nobody wrote for a release nobody named, and
    the heading is the one line of a CHANGELOG entry a reader actually scans — so it is asked
    for rather than generated.
    """
    if given:
        return given.strip()
    if len(fragments) == 1 and fragments[0].title:
        return fragments[0].title
    if len(fragments) == 1:
        raise FragmentError(
            f"{FRAGMENT_DIR}/{fragments[0].path.name} has no `# <title>` first line and no "
            "--title was given, so this release has no heading. Add one or the other")
    raise FragmentError(
        f"{len(fragments)} fragments are being assembled into one release, so no fragment's "
        "own title is the release's. Pass --title \"<what this release does>\" — the heading "
        "is the line a reader scans, and nothing here can write it")


def entry(fragments: list[Fragment], title: str) -> str:
    """The `## vNEXT` entry, ready to sit at the top of CHANGELOG.md.

    One fragment becomes the entry body directly. Several become `###` subsections under it,
    each keeping its own title, because this file's entries already section that way and
    running three unrelated changes together as one wall of prose loses which is which.
    """
    parts = [f"## {rs.PLACEHOLDER} — {title}\n"]
    if len(fragments) == 1:
        parts.append(f"\n{fragments[0].body}\n")
    else:
        for f in fragments:
            heading = f.title or f"{f.kind} #{f.issue.lstrip('+')}"
            parts.append(f"\n### {heading}\n\n{f.body}\n")
    return "".join(parts)


def insert_entry(changelog: str, text: str, where: str = "CHANGELOG.md") -> str:
    """`changelog` with `text` above its first release entry.

    Refuses an unstamped entry already present rather than writing a second one: two `##
    vNEXT` headings is a state `release_stamp.py` cannot resolve — it would stamp both with
    the same number — and `test_at_most_one_release_is_unstamped` reports it after the commit
    rather than here, before it.
    """
    names = rs.entry_names(changelog, where)
    if rs.PLACEHOLDER in names:
        raise FragmentError(
            f"{where} already carries an unstamped `## {rs.PLACEHOLDER}` entry. Assembling "
            "would add a second, and a release cannot have two. Either stamp that one "
            f"(`scripts/release_stamp.py apply`) first, or fold these fragments into it by "
            "hand and delete them")
    masked = rs.mask_code(changelog, where)
    first = re.search(r"^##[ \t]", masked, flags=re.MULTILINE)
    if first is None:
        raise FragmentError(
            f"{where} has no `## ` release entry to insert above. This tool puts a new entry "
            "at the top of the list, and there is no list")
    at = first.start()
    return changelog[:at] + text + "\n" + changelog[at:]


def insert_bullet(readme: str, changelog: str, title: str) -> str:
    """`readme` with a `- **vNEXT** — <title>.` bullet, placed by the list renderer.

    Appended to the end of the block and then rendered, rather than positioned here: where a
    bullet goes is `readme_releases.render`'s question, it answers it from the CHANGELOG that
    was just written, and a second placement rule here could disagree with it.
    """
    lead = title if title.endswith((".", "!", "?", ":")) else title + "."
    bullet = textwrap.fill(f"- **{rs.PLACEHOLDER}** — {lead}", width=_WRAP,
                           subsequent_indent=_INDENT, break_long_words=False,
                           break_on_hyphens=False) + "\n"
    _, end = rr.find_list(readme)
    return rr.render(readme[:end] + bullet + readme[end:], changelog)


# ------------------------------------------------------ was an entry written at all

#: The trailer that waives the requirement, written on a commit of the branch that owes the
#: entry. `Release-Body-Edit:` is the model, both in shape — a line a reviewer reads on the
#: PR rather than a setting somewhere nobody looks — and in how it is READ: git's own trailer
#: parser over the branch's range, never a regex over the message. The refusal below ends
#: with a ready-to-paste `Changelog-Exempt:` line, so a commit body quoting the refusal it
#: has just been given is the most likely message this branch will ever produce, and a regex
#: would read that paste as consent. A trailer block is the last paragraph of a message; a
#: quoted refusal in the middle of one is not it (#348).
_EXEMPT_KEY = "Changelog-Exempt"

#: A waiver still wearing its angle brackets — the refusal's own `<one line saying why>`,
#: pasted verbatim. git's trailer parser is perfectly happy with it and it says nothing,
#: which is the one shape of waiver indistinguishable from nobody having thought about it.
_UNFILLED = re.compile(r"^<.*>$")

#: A test file recognised by NAME, for the ones that do not live in a `tests/` tree. Both
#: forms exist here — `tests/test_dials.py` and `harness/tests/create_worktree_nginx.test.sh`
#: are under one, and a `conftest.py` beside a suite need not be.
_TEST_FILE = re.compile(r"^(?:test_.*\.py|.*_test\.py|.*\.test\.sh|conftest\.py)$")


def _exempt(path: str) -> str | None:
    """Why a change confined to `path` owes the release notes nothing — None if it ships.

    This function IS the check; everything else in this section is plumbing. A scoping rule
    that is wrong often enough to annoy anybody gets the whole job switched off within a
    week, which is the argument the `stamped` job's comment in `.github/workflows/tests.yml`
    makes at length and it is right.

    It is written as an EXEMPT list rather than as a list of source directories, and that is
    the load-bearing choice. An allowlist — `app/`, `harness/`, `mcp/`, `scripts/` —
    reproduces one level up the exact defect this check exists for: the day somebody adds a
    top-level `worker/`, every PR confined to it passes forever and nothing notices, because
    an absent rule and a satisfied one are the same shape. Here a new directory is in scope
    from the moment it exists, and exempting one means saying so here, in a diff a reviewer
    reads.

    What is exempt:

    * `changelog.d/` — the mechanism itself. A branch that only writes a fragment cannot owe
      a fragment.
    * `CHANGELOG.md` — the assembled notes, which is what a release commit consists of.
    * any `README.md` — description of the code rather than the code, and the root one's
      release list is written by `assemble`, so requiring an entry for touching it would be
      circular.
    * anything under a `tests/` directory, plus test files by name — a test asserts about
      behaviour that already shipped, and the release notes have nothing to say about it.
      `test` is one of `KINDS` all the same: a test-only branch MAY write a fragment, it is
      simply never made to.

    What is deliberately NOT exempt, because in this repo it ships:

    * `.github/` — `ci` is one of `KINDS`, and every CI-only PR since fragments existed
      (#306, #355) wrote one.
    * markdown that is not a README — `harness/commands/*.md` are the skills an agent
      executes and `harness/claude/quarterback-workflow.md` is installed into every session
      on the fleet. A blanket `*.md` exemption would have waved through #38, #103, #105,
      #212, #216 and #364, each of which changed how the fleet behaves and nothing else.
    * `flake.nix`, `pyproject.toml`, `Dockerfile`, `.harness-rules.sample` — what is built,
      shipped and defaulted to.
    """
    parts = PurePosixPath(path).parts
    if not parts:
        return None
    if parts[0] == FRAGMENT_DIR:
        return "the fragment directory"
    if path == "CHANGELOG.md":
        return "the assembled release notes"
    if parts[-1] == "README.md":
        return "documentation"
    if "tests" in parts[:-1] or _TEST_FILE.match(parts[-1]):
        return "tests"
    return None


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=False)


def _git(repo: Path, *args: str) -> str:
    proc = _run(repo, *args)
    if proc.returncode != 0:
        raise FragmentError(
            f"git {' '.join(args)} failed: {proc.stderr.strip() or 'no output'}")
    return proc.stdout


def changed_between(repo: Path, base: str, ref: str) -> list[str]:
    """Every path `ref` changes relative to `base`, from the trees and nothing else.

    Deliberately not `release_stamp.changed_paths`, which folds in the working tree and
    untracked files because `apply` runs on a branch that is not finished being written. A
    gate judges a COMMIT — the one being pushed, or the merge commit a pull request would
    land — and whatever happens to be lying around the runner's checkout is not part of it.

    `--no-renames`, and it is not a tidiness flag. `diff.renames` is on by default, and with
    `--name-only` a detected rename prints the DESTINATION alone — so a shipping module
    relocated under `tests/` would arrive here as one exempt path, the departure of the module
    itself would be invisible, and the branch would pass in silence. Both halves of a move are
    changes and both are wanted.
    """
    out = _git(repo, "diff", "--name-only", "--no-renames", "-z", base, ref)
    return sorted({p for p in out.split("\0") if p})


def fragments_at(repo: Path, ref: str) -> dict[str, str]:
    """`changelog.d/<issue>.<kind>.md` -> blob id, at `ref`. Nothing else in the directory.

    Filtered through `_NAME` rather than taken as "every file under `changelog.d/`", so the
    directory's own README is not an entry — which is precisely the state #363 landed in and
    the one this whole check exists to name.
    """
    found: dict[str, str] = {}
    for record in _git(repo, "ls-tree", "-r", "-z", ref, "--", FRAGMENT_DIR).split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        fields = meta.split()
        if len(fields) < 3 or fields[1] != "blob":
            continue
        directory, _, name = path.rpartition("/")
        if directory == FRAGMENT_DIR and _NAME.match(name):
            found[path] = fields[2]
    return found


def _changelog_at(repo: Path, ref: str) -> str:
    """`CHANGELOG.md` at `ref`, or empty when there is none. A repo without one has no
    entries rather than an unreadable state, and `frozen` is what refuses a branch that
    deleted the file."""
    proc = _run(repo, "show", f"{ref}:CHANGELOG.md")
    return proc.stdout if proc.returncode == 0 else ""


def waivers(repo: Path, base: str, branch: str) -> list[str]:
    """Reasons a commit in `base..branch` gives for owing no entry, in order, deduplicated.

    Read from the RANGE, so the waiver expires with the merge it was written for — the same
    property, for the same reason, as `release_stamp._body_edit_exemptions`.

    Fails CLOSED. A range git will not walk yields no waivers, which is the same answer as a
    branch that declared none: the entry is still required and the refusal still names the
    trailer that would have excused it. The other direction — an unreadable range read as
    blanket consent — is a waiver granted by a tool failure.
    """
    proc = _run(repo, "log", f"--format=%(trailers:key={_EXEMPT_KEY},valueonly)",
                f"{base}..{branch}")
    if proc.returncode != 0:
        return []
    seen: list[str] = []
    for line in proc.stdout.split("\n"):
        value = line.strip()
        if value and not _UNFILLED.match(value) and value not in seen:
            seen.append(value)
    return seen


def cmd_required(args: argparse.Namespace) -> int:
    """Refuse a branch that changes something that ships and writes no changelog entry.

    The gap #365 names: every other guard here verifies that what is PRESENT is correct.
    `release_stamp.py check` asks whether a `vNEXT` is unstamped, `frozen` asks whether a
    shipped entry still says what it said — and to both of them a branch that never wrote an
    entry at all looks exactly like one that wrote a correct one. #363 landed a new module,
    sixty-seven tests and two public helpers with `changelog.d/` holding only its README, and
    every job was green.

    Two refs, like `frozen` and `collision`, and for the same reason: what is judged is a
    commit, which need not be checked out anywhere.

    The two questions are asked of two different bases, deliberately:

    * WHAT CHANGED is fork-relative, against the merge base. `git diff <onto> HEAD` describes
      the round trip and would report, in reverse, every path that landed on the base while
      this branch was open — so a branch that touched nothing but its own tests would be told
      it had changed `app/`.
    * WHETHER AN ENTRY IS CARRIED is asked against `--onto` itself. A branch that merges the
      base in inherits whatever fragments and release headings the base has, and against the
      fork point those read as this branch's own work. #363's own head is that case exactly:
      it merged main and picked up three releases' headings, so a fork-relative reading of
      the CHANGELOG would have passed the very branch this check exists for.
    """
    repo = Path(args.repo).resolve()
    onto_sha = rs.resolve(repo, args.onto)
    branch_sha = rs.resolve(repo, args.branch)
    try:
        base = rs.merge_base(repo, onto_sha, branch_sha)
    except rs.StampError as e:
        # Fail CLOSED, and this is the one place it is worth spelling out. Without a fork
        # point there is no set of changed paths, so the honest answer is "cannot tell" — and
        # the shape a CI job takes when it cannot tell must not be the shape it takes when
        # everything is fine. On a runner this means `fetch-depth: 0` has gone missing from
        # the checkout, which is how #348's job would have reported green forever.
        raise FragmentError(
            f"{args.onto} and {args.branch} have no common ancestor, so there is no fork "
            "point to read this branch's changes against and nothing here can say whether an "
            "entry was owed. In CI that is a checkout without `fetch-depth: 0`; locally it "
            f"is a base that has not been fetched. ({e})"
        ) from e

    changed = changed_between(repo, base, branch_sha)
    ships = [p for p in changed if _exempt(p) is None]

    at_branch, at_onto = fragments_at(repo, branch_sha), fragments_at(repo, onto_sha)
    carried = sorted(p for p, blob in at_branch.items() if at_onto.get(p) != blob)

    # A hand-written release entry, which the CHANGELOG's own convention paragraph still
    # allows and which is also what a branch looks like AFTER `assemble` has run on it.
    #
    # Considered only when this branch edited `CHANGELOG.md` ITSELF — read from the fork
    # point, so merging the base in is not an edit. Without that clause a branch that merely
    # merged main inherits main's release headings and passes on somebody else's entry, which
    # is exactly the shape of #363's own head: it merged three releases in and wrote nothing.
    #
    # A NUMBER the base does not carry is this branch's, by name: numbered entries are only
    # ever added, and one the base lacks came from here.
    #
    # The PLACEHOLDER cannot be judged by name, because a base may legitimately carry a `##
    # vNEXT` of its own — a stacked PR onto an epic integration branch, and main itself did on
    # 2026-08-21, which is what made #302 read as entry-less. So its TEXT is compared across
    # the two refs. By name alone, a branch that edited CHANGELOG.md for some unrelated reason
    # — a typo in the preamble, a `Release-Body-Edit` correction — would be credited with the
    # base's own in-flight entry and ship with no note of its own.
    headings: list[str] = []
    if "CHANGELOG.md" in changed:
        branch_text = _changelog_at(repo, branch_sha)
        onto_text = _changelog_at(repo, onto_sha)
        onto_where = f"{args.onto}:CHANGELOG.md"
        mine = rs.unstamped_entry(branch_text, "CHANGELOG.md")
        if mine is not None and mine != rs.unstamped_entry(onto_text, onto_where):
            headings.append(rs.PLACEHOLDER)
        onto_names = set(rs.entry_names(onto_text, onto_where))
        headings += [n for n in rs.entry_names(branch_text, "CHANGELOG.md")
                     if n != rs.PLACEHOLDER and n not in onto_names]

    payload: dict[str, object] = {
        "onto": args.onto, "onto_sha": onto_sha,
        "branch": args.branch, "branch_sha": branch_sha,
        "base": base, "changed_paths": len(changed), "ships": ships,
        "fragments": carried, "headings": headings, "waivers": [],
    }

    def report(ok: bool, note: str, waived: list[str] | None = None) -> int:
        payload["ok"] = ok
        payload["waivers"] = waived or []
        if args.json:
            payload["refusal" if not ok else "note"] = note
            print(json.dumps(payload, indent=2))
            return 0 if ok else 2
        if waived:
            # stderr, and worded as a waiver rather than folded into the `ok:` line. A change
            # that ships with no entry is still a change that ships with no entry, and the
            # one place that has to be unmissable is the job that let it through.
            print("waived: " + "; ".join(waived), file=sys.stderr)
        print(note if ok else f"STOP: {note}", file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 2

    if not ships:
        # Silent for a docs-only or test-only branch, which is the other half of the design.
        # A check that nags them is a check somebody deletes.
        return report(True, f"ok: nothing that ships changed ({len(changed)} path(s), all "
                            "documentation, tests or release bookkeeping)")
    if carried or headings:
        return report(True, f"ok: {len(ships)} path(s) that ship changed, and this branch "
                            "carries " + ", ".join([*carried, *headings]))
    waived = waivers(repo, base, branch_sha)
    if waived:
        return report(True, f"ok: {len(ships)} path(s) that ship changed and no entry was "
                            f"written; a {_EXEMPT_KEY} trailer on this branch says why",
                      waived=waived)

    listing = "\n".join(f"    {p}" for p in ships[:20])
    if len(ships) > 20:
        listing += f"\n    … and {len(ships) - 20} more"
    return report(False, (
        f"{args.branch} changes {len(ships)} path(s) that ship and carries no changelog "
        f"entry:\n{listing}\n"
        "Nothing else here can notice that. `release_stamp.py check` asks whether a `vNEXT` "
        "is unstamped and `frozen` guards the entries that exist, so an entry that was never "
        "written is the same shape to both as a correct one (#365).\n"
        "Write one file, named after the issue, that no other branch will ever open:\n"
        f"    {FRAGMENT_DIR}/<issue>.<kind>.md      kinds: " + ", ".join(KINDS) + "\n"
        f"    # <what was broken or missing before this change>\n"
        f"{FRAGMENT_DIR}/README.md has the shape. Name no version in it — `assemble` and "
        "`release_stamp.py apply` decide that at land time, against the ref you merge into.\n"
        "Genuinely nothing for a reader of the release notes (a comment, a rename, a revert "
        "of something unlanded)? Say so on a commit of this branch, where a reviewer sees "
        f"it:\n    {_EXEMPT_KEY}: <one line saying why>"))


def cmd_check(args: argparse.Namespace) -> int:
    fragments = load(Path(args.repo))
    if not fragments:
        print(f"no fragments in {FRAGMENT_DIR}/")
        return 0
    for f in fragments:
        print(f"{FRAGMENT_DIR}/{f.path.name}: {f.title or '(no title)'}")
    return 0


def cmd_assemble(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    fragments = load(repo)
    if not fragments:
        # Exit 0, not a refusal. `assemble` is meant to be safe to run unconditionally before
        # landing, next to `release_stamp.py apply`, and most branches ship no release.
        print(f"no fragments in {FRAGMENT_DIR}/, nothing to assemble")
        return 0

    changelog_path, readme_path = repo / "CHANGELOG.md", repo / "README.md"
    changelog = changelog_path.read_text(encoding="utf-8")
    readme = readme_path.read_text(encoding="utf-8")

    title = release_title(fragments, args.title)
    new_changelog = insert_entry(changelog, entry(fragments, title))
    new_readme = insert_bullet(readme, new_changelog, title)

    if args.dry_run:
        print(entry(fragments, title))
        return 0

    changelog_path.write_text(new_changelog, encoding="utf-8")
    readme_path.write_text(new_readme, encoding="utf-8")
    consumed = [f.path.name for f in fragments]
    if not args.keep:
        for f in fragments:
            f.path.unlink()
    print(f"assembled {len(fragments)} fragment(s) into `## {rs.PLACEHOLDER} — {title}`: "
          + ", ".join(consumed))
    print("next: scripts/release_stamp.py apply --onto origin/main")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    ck = sub.add_parser("check", help="parse every fragment; exit 2 on a bad one")
    ck.add_argument("--repo", default=".", help="repo dir (default: cwd)")
    ck.set_defaults(func=cmd_check)

    asm = sub.add_parser("assemble", help="fold the fragments into a vNEXT entry")
    asm.add_argument("--repo", default=".", help="repo dir (default: cwd)")
    asm.add_argument("--title", default=None, help="the release heading (required past one "
                                                   "fragment)")
    asm.add_argument("--keep", action="store_true", help="do not delete the fragments")
    asm.add_argument("--dry-run", action="store_true", help="print the entry, write nothing")
    asm.set_defaults(func=cmd_assemble)

    rq = sub.add_parser(
        "required",
        help="fail if a REF changes something that ships and writes no changelog entry",
    )
    rq.add_argument("--repo", default=".", help="repo dir (default: cwd)")
    rq.add_argument("--json", action="store_true", help="machine-readable verdict")
    rq.add_argument("--onto", default="origin/main", help="the ref you are merging into")
    rq.add_argument(
        "--branch",
        default="HEAD",
        help="the ref being judged (default: HEAD). A commit, not a worktree — the same "
        "reason as `release_stamp.py frozen`: a gate judges what is being merged.",
    )
    rq.set_defaults(func=cmd_required)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (FragmentError, rr.ListError, rs.StampError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
