#!/usr/bin/env python3
"""A branch writes `changelog.d/<issue>.<kind>.md`, and the release job assembles them.

It writes nothing else. Not a version, not a placeholder, not a line of `CHANGELOG.md` — the
number is applied on `main` after the merge, by `scripts/release.py`, against the commit that
actually exists. A branch that edits a consolidated file is refused (`release.py guard`), and
the refusal names the path above.

Every branch that shipped anything used to edit the SAME lines at the top of `CHANGELOG.md`,
so every pair of concurrent branches conflicted there. That conflict is not a disagreement
about anything: both sides are right, both entries belong, and git cannot know that two
insertions at one offset are independent. A fragment is one file per change, named after the
issue, so no two branches ever touch the same path and the conflict has nowhere to occur. It
is the towncrier model:

    changelog.d/296.feat.md      # this branch's entry, naming no version
    changelog.d/298.fix.md       # a sibling branch's, in a file this one never opens

    changelog_fragments.py check      # are the fragments well formed?
    changelog_fragments.py required   # did a branch that changes something WRITE one?

Assembly is not a subcommand here any more. `assemble` was runnable on a branch, every brief
in the repo told a worker to run it, and running it is what put the release entry — and then
its number — on the branch, which is where the conflicts came from (#122). The functions it
used are still here, exported for the one caller that has any business folding fragments into
a release: `release.py run`, on `main`.

## Why `required` is here rather than left to the landing

Because nothing else could ask it. Every other guard in this repo verifies that what is
PRESENT is correct — `release.py frozen` asks whether a shipped entry still says what it
said, `guard` asks whether a branch touched a generated file — and to all of them a branch
that never wrote an entry looks exactly like one that wrote a correct one. #363 landed a new
module, sixty-seven tests and two public helpers with `changelog.d/` holding nothing but its
README, and every CI job was green; a landing agent noticed by hand (#365).

It runs on `pull_request` and is scoped so that a docs-only or test-only branch passes in
silence, because a check that fires on every PR and is usually wrong is switched off within a
week. `_exempt` is where that scoping lives and is the part to read.

## Why not towncrier itself

Judged rather than assumed, and the answer is this file, at a third of towncrier's config
surface. Towncrier renders an entry for a version it is TOLD; here the number is not known
until the release job reads `CHANGELOG.md` on `main`, which is after assembly. So towncrier
would have to be driven with a placeholder version and its `--draft`/`build` split
reinterpreted, and this repo would own a second grammar for release entries beside the one
`release.py` already parses — two answers to "what is a release entry", which is the defect
this repo keeps writing changelog entries about. It also renders one file, and half of what
drifts here is the README's release list.

## A fragment that lands unassembled is not lost

It is swept into the next release's entry, which is what a release IS: everything since the
last one. There is no state to keep in step and nothing to remember: `ls changelog.d/` is
what is in flight, and cutting a release empties it.
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
    # An already-loaded module is handed back rather than re-executed. `release.py` loads
    # THIS file lazily and this file loads it back; a second module object would mean two
    # `ReleaseError` classes, and the `except` in one would not catch the raise in the other.
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


rs = _sibling("release")
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

#: An ATX heading in a fragment's body — the one definition the refusals and the demotion
#: both read. Two of them disagreeing about which line is a heading is precisely where a
#: silent rewrite would hide, so there is one.
#:
#: Up to three leading spaces, because that is CommonMark's limit and a `  ## v9.9` renders
#: as a heading like any other. **Four or more is an indented code block**, so a `### ` there
#: is a shell comment or a quoted sample and is neither refused nor demoted — `mask_code`
#: says the same thing about indented blocks and for the same reason.
#:
#: `#hashtag` is not a heading: ATX wants a space (or end of line) after the hashes.
_HEADING = re.compile(r"^(?P<indent>[ ]{0,3})(?P<hashes>#{1,6})(?=[ \t]|$)", re.MULTILINE)

#: A setext underline: `Title` on one line, `===` or `---` under it. Refused rather than
#: demoted, because setext has only two levels and both of them are the levels a fragment
#: may not contain anyway — this closes a spelling of the `_HEADING` level-1-and-2 refusal,
#: it does not add a rule. `_is_setext` decides whether a match really is one, since `---`
#: is also a thematic break and a front-matter fence.
_RULE_LINE = re.compile(r"^(?P<indent>[ ]{0,3})(?P<rule>=+|-+)[ \t]*$", re.MULTILINE)

#: Lines that a `---` may follow WITHOUT being a setext underline: CommonMark only makes one
#: out of a paragraph, so a rule under a heading, a fence, a quote, a list marker, an
#: indented code block, an HTML block, a link reference definition, a table row or another
#: rule is a thematic break. Erring towards "not setext" here leaves such a line exactly as
#: today's parser leaves it, which is the conservative direction: a missed one is the status
#: quo, a false one refuses prose nobody could rewrite to please it. The four-space case is
#: the one that matters in practice — a fragment ending a sample with `    ls -l` and then
#: ruling a line off under it is ordinary, and refusing it would be this check's own bug.
_NOT_A_PARAGRAPH = re.compile(
    r"^(?:[ ]{4,}"
    r"|[ ]{0,3}(?:#{1,6}(?=[ \t]|$)|>|[-*+][ \t]|\d+[.)][ \t]|`{3,}|~{3,}"
    r"|<|\|"
    r"|\[[^\]]*\]:"
    r"|=+[ \t]*$|-+[ \t]*$))")

#: The retired placeholder. It meant "a release entry whose number is not decided yet" and
#: there are no undecided entries any more — a fragment IS the entry until the release job
#: numbers it. A fragment that writes `vNEXT` is a worker following a document that has not
#: caught up, so it is refused with the current contract rather than accepted and folded in.
#:
#: Matched on MASKED text, so a fragment may still write `` `vNEXT` `` while discussing the
#: history — which the entry for this very change does, at length. A token inside a code span
#: is documentation of a convention, not a use of it.
_RETIRED_PLACEHOLDER = "vNEXT"
_PLACEHOLDER_MENTION = re.compile(rf"(?<![0-9A-Za-z]){_RETIRED_PLACEHOLDER}(?![0-9A-Za-z])")

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
    #: `body` with its own headings pushed down one level, for the assembly that puts this
    #: fragment's title above them. Computed by the pass that REFUSED the bodies it cannot
    #: demote (`demote`), so the text that ships and the text that was checked are one text.
    demoted: str

    @property
    def order(self) -> tuple[int, int, str]:
        """Kind first, then issue number. Deterministic, so two machines assembling the same
        fragments produce the same entry and a re-run is a no-op rather than a reshuffle."""
        numeric = int(self.issue) if self.issue.isdigit() else 0
        return KINDS.index(self.kind), numeric, self.issue


def _is_setext(masked: str, m: re.Match[str]) -> bool:
    """Is this `===`/`---` line an underline for the line above it, or a thematic break?

    CommonMark makes a setext heading out of a **paragraph** line, so the answer is entirely
    about what precedes the rule. Everything doubtful is answered "thematic break", which
    leaves the line exactly where today's parser leaves it — see `_NOT_A_PARAGRAPH`.

    One gap, stated rather than papered over: a paragraph line made *only* of code spans
    (`` `qb-doctor` `` alone on its line) masks to nothing visible, so a `---` under it reads
    as a break here. That is what this parser already does with it, so the gap is the status
    quo and not a new hole.
    """
    start = masked.rfind("\n", 0, m.start())
    if start < 0:  # the rule is the first line: a front-matter fence or a break, not a heading
        return False
    prev = masked[masked.rfind("\n", 0, start) + 1:start]
    if not prev.strip(" \t\0"):  # blank, or wholly inside a fenced block
        return False
    return not _NOT_A_PARAGRAPH.match(prev)


def demote(body: str, where: str) -> str:
    """`body` with every heading in it pushed down one level, refusing what cannot be.

    A fragment's title becomes a `###` when several fragments are folded into one release
    entry, and the fragment's own sections are `###` too because `changelog.d/README.md`
    requires `###`-or-deeper. Left alone the two collide: v3.13 assembled seven fragments
    into twenty-one sibling `###` headings, in which a fragment's title and its own first
    section read as equals (#413). Demoting the body restores the nesting its author wrote.

    Located on MASKED text and applied to the original by offset. A `###` inside a fence or a
    code span is a quoted example — this repo's entries are mostly shell — and rewriting one
    would corrupt the sample with nothing to notice. Four-space-indented blocks are outside
    `_HEADING` for the same reason.

    Refuses rather than guesses at the two shapes it cannot represent: a `######`, which has
    no seventh level to go to, and a `#`/`##`, which would open a release rather than a
    section. Both name the file, because the fragment's author is the only one who can fix it
    and `check` runs on their branch.
    """
    masked = rs.mask_code(body, where)

    for m in _RULE_LINE.finditer(masked):
        if _is_setext(masked, m):
            kind = "`=`" if m.group("rule").startswith("=") else "`-`"
            raise FragmentError(
                f"{where} underlines a line with {kind}, which markdown reads as a setext "
                "heading — a `#` if `=`, a `##` if `-`. Folded into CHANGELOG.md that opens "
                "a RELEASE, and there is no setext spelling of `###` for the demotion to "
                "reach. Write the heading as `###` or deeper. If the rule was meant as a "
                "horizontal break, give it a blank line above — that is what makes it one — "
                "and note that a fragment has no YAML front matter: its first line is its "
                "`# title`")

    out, at = [], 0
    for m in _HEADING.finditer(masked):
        level = len(m.group("hashes"))
        if level <= 2:
            raise FragmentError(
                f"{where} contains a `#` or `##` heading in its body. Folded into "
                "CHANGELOG.md a `##` opens a RELEASE, so the entry would split in two and "
                "`release.py` would see a second one. Use `###` and below, which is how the "
                "CHANGELOG already sections a long entry")
        if level == 6:
            raise FragmentError(
                f"{where} contains a `######` heading, and markdown has no seventh level to "
                "demote it to. A fragment's own headings each drop one level when it is "
                "folded in under its title, so `#####` is the deepest a fragment may go. "
                "Flatten that section — or the one above it — to `#####` or shallower")
        out.append(body[at:m.start("hashes")])
        out.append("#" * (level + 1))
        at = m.end("hashes")
    out.append(body[at:])
    return "".join(out)


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
    title = None
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is not None and (t := _TITLE.match(lines[first])):
        title = t.group("title")
        lines = lines[first + 1:]
    body = "\n".join(lines).strip("\n")

    # Fenced blocks and code spans blanked, so an EXAMPLE of a release heading — which is how
    # a changelog entry explains this convention — is not read as one. The placeholder check
    # below runs on it, and would otherwise refuse the entry that documents it; `demote` masks
    # the body the same way, for the same reason and with more at stake — it REWRITES.
    masked = rs.mask_code(text, f"{FRAGMENT_DIR}/{path.name}")

    if not body.strip():
        raise FragmentError(
            f"{FRAGMENT_DIR}/{path.name} has no body under its title. A fragment IS the "
            "changelog entry — say what was broken or missing before this change, because "
            "that is the part no diff recovers")
    demoted = demote(body, f"{FRAGMENT_DIR}/{path.name}")
    if _PLACEHOLDER_MENTION.search(masked):
        raise FragmentError(
            f"{FRAGMENT_DIR}/{path.name} mentions `{_RETIRED_PLACEHOLDER}`, which is retired "
            "(#122). A fragment names no version at all — not a number, not a placeholder — "
            "and that is the whole reason two branches can write one each without racing for "
            "anything. The release job numbers the entry on `main` after the merge. If you "
            "are reading a document that told you to write it, that document is stale")
    return Fragment(path=path, issue=m.group("issue"), kind=kind, title=title, body=body,
                    demoted=demoted)


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


def entry(fragments: list[Fragment], title: str, version: str) -> str:
    """The `## vX.Y` entry, ready to sit at the top of CHANGELOG.md.

    One fragment becomes the entry body directly, verbatim: its own `###` sections already
    sit one level under the `##` release heading, which is the nesting its author wrote.

    Several become `###` subsections under it, each keeping its own title — this file's
    entries already section that way, and running three unrelated changes together as one
    wall of prose loses which is which. Here a fragment's body is **demoted** one level, so
    its `###` sections become `####` and sit under its title rather than beside it. Without
    that the two collide: v3.13 folded seven fragments into twenty-one sibling `###`
    headings with the fragment boundaries invisible (#413).
    """
    parts = [f"## {version} — {title}\n"]
    if len(fragments) == 1:
        parts.append(f"\n{fragments[0].body}\n")
    else:
        for f in fragments:
            heading = f.title or f"{f.kind} #{f.issue.lstrip('+')}"
            parts.append(f"\n### {heading}\n\n{f.demoted}\n")
    return "".join(parts)


def insert_entry(changelog: str, text: str, where: str = "CHANGELOG.md") -> str:
    """`changelog` with `text` above its first release entry.

    Called by `release.py run` and by nothing else. The file is newest first, so a new
    release goes above every existing one; the caller has already refused a CHANGELOG whose
    highest number it cannot read, and checks afterwards that no existing entry moved.
    """
    masked = rs.mask_code(changelog, where)
    first = re.search(r"^##[ \t]", masked, flags=re.MULTILINE)
    if first is None:
        raise FragmentError(
            f"{where} has no `## ` release entry to insert above. This tool puts a new entry "
            "at the top of the list, and there is no list")
    at = first.start()
    return changelog[:at] + text + "\n" + changelog[at:]


def insert_bullet(readme: str, changelog: str, title: str, version: str) -> str:
    """`readme` with a `- **vX.Y** — <title>.` bullet, placed by the list renderer.

    Appended to the end of the block and then rendered, rather than positioned here: where a
    bullet goes is `readme_releases.render`'s question, it answers it from the CHANGELOG that
    was just written, and a second placement rule here could disagree with it.

    The README's release list is generated in exactly this sense — ordered by the renderer,
    extended only by the release job, and never by a branch. The bullets themselves stay
    hand-written, because a bullet is a summary somebody chose rather than a copy of the
    CHANGELOG heading; what a branch no longer does is write one.
    """
    lead = title if title.endswith((".", "!", "?", ":")) else title + "."
    bullet = textwrap.fill(f"- **{version}** — {lead}", width=_WRAP,
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
      release list is written by the release job, so requiring an entry for touching it
      would be circular.
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

    Deliberately not `release.changed_paths`, which folds in the working tree and untracked
    files because the release job reads a checkout rather than a pushed commit. A
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
    property, for the same reason, as `release._body_edit_exemptions`.

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
    `release.py frozen` asks whether a shipped entry still says what it said, `guard` asks
    whether a branch touched a generated file — and to both of them a branch that never wrote
    an entry at all looks exactly like one that wrote a correct one. #363 landed a new module,
    sixty-seven tests and two public helpers with `changelog.d/` holding only its README, and
    every job was green.

    Two refs, like `release.py frozen` and `guard`, and for the same reason: what is judged
    is a commit, which need not be checked out anywhere.

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
    except rs.ReleaseError as e:
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

    # A hand-written release entry used to count here: the CHANGELOG's own convention
    # paragraph allowed one, and it was also what a branch looked like after `assemble` had
    # run on it. Neither is true any more. `release.py guard` refuses a branch that edits
    # `CHANGELOG.md` at all, so an entry written there is not a second way to satisfy this
    # check — it is a separate refusal with its own remedy, and crediting it here would let a
    # branch pass the note requirement by doing the one thing the guard exists to stop.
    #
    # A fragment is therefore the only answer, which is what #122 means by "strictly easier
    # to check and harder to get wrong": one question, one shape, one place to look.

    payload: dict[str, object] = {
        "onto": args.onto, "onto_sha": onto_sha,
        "branch": args.branch, "branch_sha": branch_sha,
        "base": base, "changed_paths": len(changed), "ships": ships,
        "fragments": carried, "waivers": [],
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
    if carried:
        return report(True, f"ok: {len(ships)} path(s) that ship changed, and this branch "
                            "carries " + ", ".join(carried))
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
        "Nothing else here can notice that. `release.py frozen` guards the entries that "
        "exist and `guard` refuses a branch that touches them, so an entry that was never "
        "written is the same shape to both as a correct one (#365).\n"
        "Write one file, named after the issue, that no other branch will ever open:\n"
        f"    {FRAGMENT_DIR}/<issue>.<kind>.md      kinds: " + ", ".join(KINDS) + "\n"
        f"    # <what was broken or missing before this change>\n"
        f"{FRAGMENT_DIR}/README.md has the shape. Name no version in it, and edit no other "
        "file for this: the release job numbers the entry on `main` after the merge.\n"
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    ck = sub.add_parser("check", help="parse every fragment; exit 2 on a bad one")
    ck.add_argument("--repo", default=".", help="repo dir (default: cwd)")
    ck.set_defaults(func=cmd_check)

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
        "reason as `release.py frozen`: a gate judges what is being merged.",
    )
    rq.set_defaults(func=cmd_required)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (FragmentError, rr.ListError, rs.ReleaseError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
