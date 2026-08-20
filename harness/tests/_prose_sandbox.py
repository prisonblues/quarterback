"""The contract between the prose-consistency suites and the sandbox that runs them.

These suites — the ones under `harness/**/tests` whose subject is this repo's own text and the
code it describes, rather than the worktree scripts — read files outside their own directory.
`nix build .#checks.<system>.prose-consistency-tests` runs them against a sandbox holding only
what that check installs, so a read nobody installed does not FAIL there: it ERRORS on a missing
file, in a build no workflow runs. That is #163's mechanism, and by the time anyone counted there
were four instances of it across three checks (#163, #246, #251, #257).

Each member declares its own reads, next to the reads themselves, and refuses anything absent
from that declaration — so an undeclared read cannot happen. This module is the other half: it
collects those declarations and is where the comparison against `flake.nix` lives.

It lives here rather than in one of the suites because the comparison runs BOTH ways, and the
converse direction cannot belong to a member. A check serving two suites installs files that
either one alone does not read, so a member asking "does this check install anything I do not
read?" would report its neighbour's files as unread. Only something that knows the whole
category can ask that, which is this.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: The check that runs these suites. Written out rather than discovered: `nix flake check` on a
#: flake whose check has been renamed says nothing at all about them, so the name being wrong has
#: to be an error here rather than an empty comparison that reports everything as fine.
CHECK_NAME = "prose-consistency-tests"

#: The suites this check exists to run, by module name. Adding a suite to the check without
#: adding it here would leave its reads uncompared — so `test_every_member_is_collected_by_the
#: _check` holds this list against the test files the check installs, in both directions.
MEMBERS = (
    "test_fixer_escalation",
    "test_regression_test_redgreen",
    "test_commands_wired",
)

#: Installed as whole trees rather than file by file, with the reason each is not enumerable.
#: A tree supplies anything beneath it, so it weakens the staleness guard for its own contents —
#: which is the trade, stated here rather than left implicit.
TREES = {
    # panel_core imports harness_rules, which imports further modules in the package. Enumerating
    # a Python package's files is a list that goes stale on every refactor, and the failure would
    # be an ImportError naming a module rather than a path.
    "harness/loops": "a package that has to be importable, not a set of files that are read",
    # Between them the members read six of the briefs, and which six is a judgement that moves as
    # loops are added. The whole directory is prose these suites exist to read, so enumerating it
    # bought a staleness guard over the one tree where staleness is the normal case.
    # test_commands_wired.py globs this directory to ask which briefs exist, so the directory
    # IS the read — a file list could not express the question, let alone go stale gracefully.
    "harness/commands": "a directory whose entire contents are what these suites read",
}

#: Installed deliberately without being read: the guard needs `flake.nix` to compare against.
#: Named rather than subtracted inline, so a second entry has to be argued for in a diff.
INSTALLED_BUT_NOT_READ = frozenset({"flake.nix"})

#: Where the suites themselves live. Anything installed from here is code the check RUNS rather
#: than a file a suite reads, so the "installs nothing unread" direction cannot apply to it —
#: and no member reads anything under it, which is what makes the exemption safe to state as a
#: directory rather than a list.
SUITE_DIR = "harness/tests/"

#: An `install`/`cp` of a repo path into the sandbox. Anchored at line start and on the command,
#: so a `${./x}` in a comment or passed as an argument is not read as supplying a file —
#: interpolating a store path is not the same as the sandbox having it.
INSTALL_RE = re.compile(
    r'^\s*(?:install|cp)(?:\s+-\S+)*\s+\$\{\s*\./([^}\s]+?)\s*\}', re.MULTILINE)


def declared_reads() -> dict[str, frozenset[str]]:
    """Each member's own declaration of what it reads, keyed by module name.

    Imported rather than restated here: the declaration belongs beside the reads it describes,
    where somebody adding one will see it. A member that stops exposing `READS` is an error, not
    a member that silently contributes nothing to the comparison."""
    out: dict[str, frozenset[str]] = {}
    for name in MEMBERS:
        module = importlib.import_module(name)
        reads = getattr(module, "READS", None)
        assert reads is not None, (
            f"{name} is a member of the {CHECK_NAME} check but does not declare READS, so its "
            f"reads are compared against nothing. Give it a READS frozenset and an accessor that "
            f"refuses paths absent from it.")
        out[name] = frozenset(reads)
    return out


def check_region(flake_text: str) -> str:
    """The `prose-consistency-tests` block of `flake.nix`, and only it.

    Anchored on the attribute definition at line start, because `flake.nix` discusses this check
    in the prose above it: a first-occurrence search for the bare name would slice from a comment
    and compare these suites against whatever block followed — indistinguishable, from the
    outside, from a correct comparison. Exactly one definition is required, so a rename is an
    error here rather than a silently empty region that reports either every read as missing or
    nothing wrong at all."""
    starts = [m.start() for m in
              re.finditer(rf"^\s*{re.escape(CHECK_NAME)} = ", flake_text, re.MULTILINE)]
    assert len(starts) == 1, (
        f"expected exactly one line defining {CHECK_NAME} in flake.nix, found {len(starts)}")
    end = flake_text.find("\n        '';", starts[0])
    assert end != -1, (
        f"the {CHECK_NAME} block is not terminated by a closing ''; at its own indentation")
    return flake_text[starts[0]:end]


def installs(flake_text: str) -> frozenset[str]:
    """The repo paths the check puts in its sandbox."""
    return frozenset(INSTALL_RE.findall(check_region(flake_text)))


def supplied_by(path: str, installed: frozenset[str]) -> bool:
    """Whether `path` is in the sandbox, directly or under an installed tree.

    Component-wise, not by string prefix: `harness/loops` is a string prefix of
    `harness/loops_old/x.py` and a directory prefix of nothing but `harness/loops/…`."""
    if path in installed:
        return True
    return any(path.startswith(tree + "/") for tree in installed)
