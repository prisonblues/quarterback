"""Reading a flake check's sandbox out of `flake.nix`.

Several suites under `harness/` live two directories below the repo root and read files AT
that root, while `nix build .#checks.<system>.<name>` runs them in a sandbox holding only what
that check copies in. A read nobody copied in does not FAIL there — it ERRORS on a missing
file, in a build log nobody reads, which is how #163 sat unnoticed for a day and how #246,
#251 and #257 each sat after it.

So each of those suites compares what it reads against what its check supplies. The half of
that comparison which is about `flake.nix` — find my check's block, read its copy lines, check
they land where the suite will look — is identical for all of them, and it lived twice: once
in `test_release_numbers.py` (#182) and once in `_prose_sandbox.py` (#257). Two hand-rolled
readers of one file agree only until somebody edits one of them, and the failure would be
silent in the direction that matters: a reader that finds nothing reports every copy as
missing, or every read as supplied, depending on which way it is asked.

It is here rather than in either suite because the two run in DIFFERENT sandboxes, so neither
can import the other. This module is installed into both.

What is NOT shared is how a suite knows its own reads. `test_release_numbers.py` discovers
them from its own syntax tree because it builds paths inline; the prose-consistency suites
declare them and refuse anything undeclared, because their reads go through one accessor and
`doc(name)` inside a loop is invisible to a parser. Those are different problems with
different right answers, and collapsing them would make one of the two worse.
"""

from __future__ import annotations

import re

#: One copy line in a check's script: `cp ${./a/b} repo/a/b`, or the `install -D` form that
#: brings its own parent directory. Anchored at line start AND on the command, because the
#: region is bash inside a Nix indented string where `${./x}` also occurs in comments, in a
#: commented-out copy line and in `--ignore` arguments — counting one of those as a copy is how
#: this guard passes while the sandbox errors on the file it exists to catch. `\s*` inside the
#: braces is for the Nix formatters that write `${ ./x }`, which would otherwise be reported as
#: a missing copy on a repo where nothing is wrong. The destination is captured too: see
#: `misdirected`.
COPY_RE = re.compile(
    r"^[ \t]*(?:cp|install)\b[^\n]*?\$\{\s*\./(?P<src>[^}\s]+)\s*\}[ \t]+(?P<dest>\S+)[ \t]*$",
    re.MULTILINE)

#: The prefix every copy lands under: these sandboxes build a `repo/` tree and the suites'
#: repo root resolves to it. A destination that does not follow the rule puts the file
#: somewhere the suite will not look, which comparing source paths alone cannot see.
SANDBOX_PREFIX = "repo/"


def check_region(text: str, check: str) -> str:
    """The named check's own text, sliced out of `flake.nix`.

    Both ends are anchored at line start on shapes Nix actually writes, rather than found with
    a bare substring search. A check's name appears in comments, in prose and in a
    `checks.${system}` assembly entry; `'';` ends every indented string in the file. The first
    occurrence of either is not necessarily this check's, and a wrong slice silently compares a
    suite's reads against some other derivation's copies.

    Exactly one definition is required. A renamed check is then an error here rather than an
    empty region, which would report either every read as uncopied or — after the usual
    subtractions — nothing wrong at all.
    """
    opens = list(re.finditer(rf"^[ \t]*{re.escape(check)}\s*=", text, flags=re.MULTILINE))
    assert len(opens) == 1, (
        f"flake.nix has {len(opens)} lines defining `{check} =`, and this comparison needs "
        f"exactly one to know which sandbox feeds this suite. If the check was renamed, rename "
        f"it where the suite names it too — that name is the only thing tying the suite to the "
        f"sandbox that feeds it")
    start = opens[0].start()
    end = re.compile(r"^[ \t]*'';[ \t]*$", re.MULTILINE).search(text, start)
    assert end, (
        f"no line closing an indented string (`'';`) appears after the `{check}` definition at "
        f"offset {start} of flake.nix, so this comparison cannot tell where the check ends. The "
        f"check's script was restructured, or the file is truncated")
    return text[start:end.start()]


def copies(region: str) -> dict[str, str]:
    """Source path -> destination, for every copy line in a check's script."""
    return {m.group("src"): m.group("dest") for m in COPY_RE.finditer(region)}


def misdirected(pairs: dict[str, str], prefix: str = SANDBOX_PREFIX) -> list[str]:
    """The copies whose destination does not mirror their repo path under `prefix`.

    Comparing source paths alone cannot see this: `install ${./app/api/reviews.py}
    repo/app/api/review.py` supplies *a* file, and the suite reading `app/api/reviews.py`
    errors on a missing one — the guard reporting green while the sandbox fails, which is the
    whole failure this comparison exists to close.
    """
    return sorted(f"{src} -> {dest}" for src, dest in pairs.items()
                  if dest != prefix + src)


def supplied_by(path: str, sources: set[str] | frozenset[str]) -> bool:
    """Whether `path` is in the sandbox, directly or under a copied directory.

    Component-wise, not by string prefix: `harness/loops` is a string prefix of
    `harness/loops_old/x.py` and a directory prefix of nothing but `harness/loops/…`, and a
    `startswith` would report a file the sandbox does not hold as supplied.
    """
    return path in sources or any(path.startswith(src + "/") for src in sources)
