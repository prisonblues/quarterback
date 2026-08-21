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
import re
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

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

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (FragmentError, rr.ListError, rs.StampError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
