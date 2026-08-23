#!/usr/bin/env python3
"""Render README.md's release list in CHANGELOG.md's order, instead of keeping it by hand.

The README carries a bullet per release, oldest first; CHANGELOG.md carries an entry per
release, newest first. Which releases exist and what order they came in is ONE fact, and it
was written down twice — so the README drifted, silently, for three releases in a row:

    v2.61, v2.59, v2.60, v2.62, v2.63, v2.64, v2.65

`74a0453` is a human pushing `docs(readme): put v2.62 at the end of the release list` to
correct the same class by hand, which is the tell: an ordering convention nobody wrote down
and nothing checked, corrected by whoever happened to notice.

So the ORDER is rendered from the CHANGELOG and the drift is a test failure
(`harness/tests/test_release_numbers.py::test_the_readme_release_list_is_in_changelog_order`):

    readme_releases.py check      # exit 1 if README.md's list is not in CHANGELOG order
    readme_releases.py write      # reorder it in place

## What is generated, and what is emphatically not

The ORDER of the bullets, and nothing else. A bullet's PROSE stays hand-written and is moved
byte-for-byte, because it is not a copy of the CHANGELOG heading: `## v2.19 — what each
reviewer cost, not just what it found` is a bullet reading `per-reviewer token usage and
vendor-stated cost, so the leaderboard ranks reviewers on what they cost as well as what they
find`. Fifty-odd bullets are like that. Rendering the list *from* the CHANGELOG's titles would
delete the summaries and call it generation, so this tool never writes prose — it only ever
reorders whole bullets and refuses everything it cannot fix.

That is why a MISSING bullet is a refusal rather than an insertion. Nothing here can write the
sentence, and inventing one from the heading would put text in the README that nobody chose.

## Ranges, and the bullets that are not releases

The oldest entries are grouped — one bullet whose bold run joins two release names with an en
dash — so a bullet covers a SPAN. The span is resolved against the CHANGELOG rather than by
arithmetic: a release that does not exist cannot fall inside one, and the `v1` to `v2.1` bullet
covers exactly the three entries the CHANGELOG has between those two headings. A range whose
endpoints leave a gap is reported as the releases that have no bullet.

`- **Not yet numbered** — …` names no release and is rendered last, after every version
bullet. It is deliberately unnumbered — a roadmap bullet naming `v3` would collide with the
real `v3` the day the release job issues it — so it has no position in the CHANGELOG's order
and this tool gives it the one place that cannot go stale.

## Who calls `write`

`scripts/release.py run`, on `main`, and a person repairing an order that has drifted. Not a
branch: the release list is one of the two files `release.py guard` refuses a branch for
editing, because it was the expensive half of every landing conflict — a bullet appended at
the end of the block by two branches at once is the same insertion-at-one-offset that made
`CHANGELOG.md` conflict (#122).
"""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import itertools
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent

# `scripts/` is a directory of standalone tools rather than an importable package, so the
# release tool is loaded by path — and registered in sys.modules before it executes, because
# @dataclass resolves annotations through sys.modules[cls.__module__]. An already-loaded
# module is reused rather than re-executed: `release.py` loads this file back, and a second
# module object would mean two `ReleaseError` classes that do not catch each other.
if "release" in sys.modules:
    rs = sys.modules["release"]
else:
    _SPEC = importlib.util.spec_from_file_location("release", _SCRIPTS / "release.py")
    assert _SPEC and _SPEC.loader
    rs = importlib.util.module_from_spec(_SPEC)
    sys.modules[_SPEC.name] = rs
    _SPEC.loader.exec_module(rs)

#: The README heading the list lives under. The list is found by this heading rather than by
#: scanning for `- **v…**` bullets anywhere in the file, because the README has other bold-run
#: bullets (`- **Datastore: Postgres** — …`) and a global scan would sweep them into the
#: release list and then reorder them into it.
LIST_HEADING = "### Every release, oldest first"

#: `- **<label>** — `, the opening of a release bullet. The label is captured whole and
#: classified afterwards: a range, a single release, or something that is not a version at
#: all. Matching only versions here would make a mistyped bullet invisible rather than a
#: refusal, and invisible is how the ordering drifted in the first place.
_BULLET = re.compile(r"^-[ \t]+\*\*(?P<label>[^*\n]+)\*\*[ \t]+—[ \t]", re.MULTILINE)

#: A continuation line of a bullet: indented, non-blank. This repo wraps the list at 100
#: columns with a two-space hanging indent.
_CONTINUATION = re.compile(r"^[ \t]+\S")

#: The en dash joining a range's endpoints, as an escape rather than as itself: the separator
#: between a bullet's LABEL and its prose is an EM dash, the two are one line apart in every
#: bullet, and they are indistinguishable in a diff. Reading the wrong one as a range splitter
#: would make every bullet a range over its own prose.
_RANGE_DASH = "\N{EN DASH}"


class ListError(Exception):
    """A README release list this tool will not silently repair."""


@dataclass(frozen=True)
class Bullet:
    """One rendered bullet: the label in its bold run, and its whole text including newline."""

    label: str
    text: str

    @property
    def endpoints(self) -> tuple[str, str] | None:
        """The releases this bullet's label names, or None if it names none.

        `(first, last)` for a range and `(name, name)` for a single release. Endpoints
        rather than the span, because the span only exists relative to a CHANGELOG.
        """
        if _RANGE_DASH in self.label:
            first, _, last = self.label.partition(_RANGE_DASH)
            first, last = first.strip(), last.strip()
            return (first, last) if _is_entry(first) and _is_entry(last) else None
        return (self.label, self.label) if _is_entry(self.label) else None


def _is_entry(label: str) -> bool:
    """Whether `label` is spelled like a release entry — `v2.34` or `v3`."""
    return bool(re.fullmatch(r"v\d+(?:\.\d+)?", label))


def changelog_order(changelog: str, where: str = "CHANGELOG.md") -> list[str]:
    """Every release entry in the CHANGELOG, OLDEST first — the order the README wants.

    The CHANGELOG is newest first, and this reverses it rather than sorting numerically. The
    file's own order is the fact being rendered: `test_the_changelog_is_newest_first` already
    asserts it is sorted, and sorting again here would let this tool quietly render a
    correct-looking README over a CHANGELOG that had stopped being sorted — hiding the one
    failure the other test exists to report.
    """
    return list(reversed(rs.entry_names(changelog, where)))


def find_list(readme: str, where: str = "README.md") -> tuple[int, int]:
    """`(start, end)` offsets of the bullet block under `LIST_HEADING`, in the ORIGINAL text.

    Located on masked text — a fenced example of a release bullet is documentation of this
    convention, not a use of it — and returned as offsets into the unmasked original, so
    every rewrite is a span replacement and no prose is ever reconstructed.
    """
    masked = rs.mask_code(readme, where)
    at = masked.find(LIST_HEADING)
    if at < 0:
        raise ListError(
            f"{where} has no `{LIST_HEADING}` heading, so this tool cannot tell which of its "
            "bold-run bullet lists is the release list. Restore the heading, or teach "
            "`LIST_HEADING` the new one — do not let the list go unrendered")
    first = _BULLET.search(masked, at)
    if first is None:
        raise ListError(
            f"{where} has a `{LIST_HEADING}` heading with no `- **<release>** — ` bullet "
            "under it. An empty release list is not a state this tool will render over")
    start = first.start()
    end = start
    for line in masked[start:].split("\n"):
        if not (_BULLET.match(line) or _CONTINUATION.match(line)):
            break
        end += len(line) + 1
    _refuse_a_detached_bullet(masked, end, where)
    return start, end


def _refuse_a_detached_bullet(masked: str, end: int, where: str) -> None:
    """A release bullet left BELOW the block, which the block would silently not contain.

    The list ends at the first line that is neither a bullet nor a continuation, so a blank
    line in the middle of it cuts everything after it out of this tool's sight — and what
    comes back is "these releases have no bullet" naming bullets that are sitting in the
    file. Only a bullet whose label is a release is refused; the README has other bold-run
    bullets further down (`- **Datastore: Postgres** — …`) and they end the list correctly.
    """
    rest = masked[end:]
    blank = re.match(r"(?:[ \t]*\n)+", rest)
    if not blank:
        return
    stray = _BULLET.match(rest, blank.end())
    if stray and Bullet(label=stray.group("label"), text="").endpoints is not None:
        line = masked.count("\n", 0, end + blank.end()) + 1
        raise ListError(
            f"{where}'s release list has a blank line in it, with `{stray.group('label')}` "
            f"and possibly more bullets below it at line {line}. The list ends at the first "
            "line that is not a bullet, so this tool would report every release below the "
            "gap as unlisted while its bullet sits in the file. Close the gap")


def parse_bullets(block: str, where: str = "README.md") -> list[Bullet]:
    """Split a bullet block into whole bullets, each keeping its own text verbatim."""
    starts = [m.start() for m in _BULLET.finditer(block)]
    if not starts:
        raise ListError(f"{where}'s release list has no bullets in it")
    if starts[0] != 0:
        raise ListError(
            f"{where}'s release list starts with {block[:starts[0]]!r} before its first "
            "bullet, which this tool would silently drop when it reorders")
    out = []
    for lo, hi in itertools.pairwise([*starts, len(block)]):
        m = _BULLET.match(block, lo)
        assert m  # every start came from the same pattern
        out.append(Bullet(label=m.group("label"), text=block[lo:hi]))
    return out


def _span(endpoints: tuple[str, str], order: list[str], label: str) -> list[str]:
    """The releases a bullet covers, resolved against the CHANGELOG's own order."""
    first, last = endpoints
    for name in (first, last):
        if name not in order:
            raise ListError(
                f"README.md's release list has a bullet for {label}, and CHANGELOG.md has no "
                f"`## {name}` entry. Either the release was renamed in one file and not the "
                "other, or the bullet names a release that never shipped")
    lo, hi = order.index(first), order.index(last)
    if lo > hi:
        raise ListError(
            f"README.md's bullet `{label}` runs backwards against CHANGELOG.md, where {last} "
            f"comes before {first}. A range is written oldest-to-newest")
    return order[lo:hi + 1]


def coverage(bullets: list[Bullet], order: list[str]) -> dict[str, Bullet]:
    """Release -> the bullet covering it, refusing anything covered twice."""
    seen: dict[str, Bullet] = {}
    for b in bullets:
        ends = b.endpoints
        if ends is None:
            continue
        for name in _span(ends, order, b.label):
            if name in seen:
                raise ListError(
                    f"README.md's release list covers {name} twice: `{seen[name].label}` and "
                    f"`{b.label}`. That is what a merge keeping both sides of this list "
                    "leaves behind, and reordering it would hide the duplicate rather than "
                    "fix it")
            seen[name] = b
    return seen


def render(readme: str, changelog: str) -> str:
    """`readme` with its release list in CHANGELOG order. Prose is never touched."""
    order = changelog_order(changelog)
    start, end = find_list(readme)
    bullets = parse_bullets(readme[start:end])
    covered = coverage(bullets, order)

    missing = [name for name in order if name not in covered]
    if missing:
        raise ListError(
            "CHANGELOG.md has entries with no bullet in README.md's release list: "
            + ", ".join(missing)
            + ". Write one — this tool reorders bullets and never invents prose, because a "
            "bullet is a summary somebody chose and not a copy of the heading")

    # Version bullets in the CHANGELOG's order, then the ones that name no release. A bullet
    # covering a span sorts by its OLDEST release, which is where the whole span belongs;
    # keying on the newest would put the `v1` to `v2.1` bullet after `v2` and split the file's
    # own order.
    ordered = sorted(
        (b for b in bullets if b.endpoints is not None),
        key=lambda b: order.index(_span(b.endpoints, order, b.label)[0]),
    )
    # Unversioned bullets keep their relative order and go last. `- **Not yet numbered**` is
    # the only one today and is already last; a second one has no position in the CHANGELOG
    # to render from, so "after everything that has shipped" is the only answer that is not a
    # guess.
    ordered += [b for b in bullets if b.endpoints is None]
    return readme[:start] + "".join(b.text for b in ordered) + readme[end:]


def _diff(before: str, after: str) -> str:
    """Only the release list moves, so a unified diff of the whole file is the list's diff."""
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile="README.md", tofile="README.md (rendered)", n=1))


def _files(repo: Path) -> tuple[Path, Path]:
    return repo / "README.md", repo / "CHANGELOG.md"


def cmd_check(args: argparse.Namespace) -> int:
    readme, changelog = _files(Path(args.repo))
    before = readme.read_text(encoding="utf-8")
    after = render(before, changelog.read_text(encoding="utf-8"))
    if before == after:
        print("README.md's release list is in CHANGELOG.md's order")
        return 0
    print("README.md's release list is not in CHANGELOG.md's order. "
          "Run `scripts/readme_releases.py write`:", file=sys.stderr)
    print(_diff(before, after), file=sys.stderr)
    return 1


def cmd_write(args: argparse.Namespace) -> int:
    readme, changelog = _files(Path(args.repo))
    before = readme.read_text(encoding="utf-8")
    after = render(before, changelog.read_text(encoding="utf-8"))
    if before == after:
        print("README.md's release list is already in CHANGELOG.md's order")
        return 0
    readme.write_text(after, encoding="utf-8")
    print("README.md's release list reordered to match CHANGELOG.md")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn, help_ in (("check", cmd_check, "exit 1 if the list has drifted"),
                            ("write", cmd_write, "reorder the list in place")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--repo", default=".", help="repo dir (default: cwd)")
        sp.set_defaults(func=fn)
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (ListError, rs.ReleaseError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
