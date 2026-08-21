"""The contract between the prose-consistency suites and the sandbox that runs them.

These suites — the ones whose subject is this repo's own text and the code it describes,
rather than the worktree scripts — read files outside their own directory.
`nix build .#checks.<system>.prose-consistency-tests` runs them against a sandbox holding only
what that check installs, so a read nobody installed does not FAIL there: it ERRORS on a
missing file, in a build no workflow runs. That is #163's mechanism, and by the time anyone
counted there were five instances of it across four checks (#163, #246, #251, #257, and
`test_commands_wired.py`, which had been erroring at COLLECTION since it landed).

Each member declares its own reads, next to the reads themselves, and routes every read
through an accessor that refuses anything absent from that declaration — so an undeclared read
cannot happen. This module is the other half: it collects those declarations and is where the
comparison against `flake.nix` lives.

It lives here rather than in one of the suites because the comparison runs BOTH ways, and the
converse direction cannot belong to a member. A check serving several suites installs files
that any one of them alone does not read, so a member asking "does this check install anything
I do not read?" would report its neighbours' files as unread. Only something that knows the
whole category can ask that, which is this.

`_flake_sandbox` holds the half that is about reading `flake.nix` — finding the check's block,
parsing its copy lines, checking they land where a suite will look. That is identical for every
suite with this problem and used to be written out twice; its docstring has the reasoning.

**What this cannot do.** A declaration says what a suite is ALLOWED to read, so it catches an
undeclared read and a stale install. It cannot notice that the last call site reading a
declared path was deleted — the declaration is still there, and the install answering it still
matches. `test_release_numbers.py` can, because it discovers its reads from its own syntax
tree; these suites cannot use that, because their reads go through an accessor and `doc(name)`
inside a loop over `FIX_LOOPS` is invisible to a parser. The mitigation is
`test_every_declared_read_exists`, which at least catches a declaration pointing at a file
nobody has, and the residual gap is a stale declaration whose file still exists. Stated because
an overclaimed guard is worse than a narrow one.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import _flake_sandbox as flake

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: The check that runs these suites. Written out rather than discovered: `nix flake check` on a
#: flake whose check has been renamed says nothing at all about them, so the name being wrong
#: has to be an error here rather than an empty comparison that reports everything as fine.
CHECK_NAME = "prose-consistency-tests"

#: The suites this check exists to run, by module name. A suite the check installs but that is
#: not listed here would have its reads compared against nothing, and a member listed here that
#: the check does not run would have its declaration checked against a sandbox it never sees —
#: `test_every_member_is_run_by_the_check_and_every_suite_it_runs_is_a_member` holds the two
#: against each other in both directions.
MEMBERS = (
    "test_fixer_escalation",
    "test_regression_test_redgreen",
    "test_commands_wired",
)

# MEMBERS is not the whole category, and the gap is structural rather than an oversight.
#
# `harness/loops/tests/test_panel_dials.py` reads two briefs at the repo root and has exactly
# this problem: it computes its root as `parents[3]`, while `loops-tests` copies the package in
# and runs from inside it, so that root resolves above the build directory entirely. It cannot
# join this check — it exercises `harness/loops` as the code under test, so it has to run in
# `loops-tests` where that package is the subject rather than a dependency.
#
# It is NOT guarded here and, on this branch, not guarded anywhere: PR #249 fixes it and adds
# the comparison on the `loops-tests` side, and that PR is not merged. Said plainly because the
# first draft of this paragraph claimed the guard already existed, which was true only in that
# other branch — a comment asserting a state that does not exist in the tree it ships in, which
# is the exact failure this whole check is about.
#
# `_flake_sandbox` is shared with `release-metadata-tests`, not with anything on the loops side.
# When #249 lands, that guard becomes its third importer.
#
# So a suite outside `harness/tests` is invisible to everything here: nothing in this module can
# enumerate the category, only the part of it that shares this sandbox. What closes that is a
# workflow running the flake at all (#179), not another assertion in here.

#: Installed as whole directories rather than file by file, with the reason each is not
#: enumerable. A directory supplies anything beneath it, so it weakens the staleness guard for
#: its own contents — which is the trade, stated here rather than left implicit.
#:
#: These are held to two assertions of their own (`test_every_tree_is_installed_as_a_directory`)
#: because everything else here only ever SUBTRACTS this mapping: without them a stale entry
#: survives forever, and adding a plain file's path to it would be a working way to silence the
#: converse guard.
TREES = {
    # panel_core imports harness_rules, which imports further modules in the package. A file
    # list for a Python package goes stale on every refactor, and the failure would be an
    # ImportError naming a module rather than a path — which no path-based guard here can see.
    "harness/loops": "a package that has to be importable, not a set of files that are read",
    # test_commands_wired.py globs this directory to ask which briefs exist, so the directory
    # IS the read: a file list could not express the question, and no count of the briefs the
    # other members name belongs in a comment — the union is in their own READS.
    "harness/commands": "a directory whose contents are the question, not a fixed set of files",
}

#: Installed deliberately without being read by a MEMBER. Named rather than subtracted inline,
#: so a second entry has to be argued for in a diff.
INSTALLED_BUT_NOT_READ = frozenset({
    # Read by `test_prose_sandbox`'s own fixture, which is not a member — it compares the
    # check against its members rather than reading the repo's prose. So the file IS read in
    # the sandbox, and dropping this install line would silence the comparison rather than
    # leave it unaffected.
    "flake.nix",
})

#: Where the suites and this contract live. Anything installed from here is code the check RUNS
#: rather than a file a suite reads, so the "installs nothing unread" direction cannot apply to
#: it — and no member reads anything under it, which is what makes the exemption safe to state
#: as a directory rather than a list.
SUITE_DIR = "harness/tests/"

#: The check that would otherwise collect these suites, and the reason this module has to know
#: about a check it is not part of.
#:
#: `worktree-tests` copies `harness/tests` in wholesale, so it picks up every file in this
#: directory — including these suites, whose reads it cannot satisfy. Each one is removed from
#: it by an explicit `rm`. Nothing tied that `rm` list to `MEMBERS`, which made the cost of
#: joining this category one step longer than it looked: declare READS, add to MEMBERS, add an
#: install — and if you stop there, the new suite is still collected by `worktree-tests`, still
#: reads files that sandbox does not hold, and still ERRORS there, with every guard in this
#: module reporting green. That is #163's mechanism arriving through the fix for it.
#:
#: So `test_every_member_is_removed_from_the_check_that_would_collect_it` holds the two lists
#: against each other. `test_commands_wired.py` is the evidence that this was not hypothetical:
#: it had been erroring in `worktree-tests` since it landed, and what fixed it was somebody
#: noticing and hand-writing the `rm`.
COLLECTING_CHECK = "worktree-tests"

#: The checks that adopt a suite removed from `COLLECTING_CHECK`. A suite deleted from that
#: sandbox has to run SOMEWHERE, and this category is not the only home: `test_release_numbers.py`
#: was the first suite with this problem and has its own check (#182, #163).
#:
#: Listed so the converse guard can ask the question that matters — is this removal accounted
#: for by a check that runs the file — rather than the narrower one it asked first, which
#: reported `release-metadata-tests`' suite as homeless.
ADOPTING_CHECKS = (CHECK_NAME, "release-metadata-tests")

#: One `rm` of a file under `SUITE_DIR` in a check's script.
REMOVAL_RE = __import__("re").compile(
    rf"^[ \t]*rm[ \t]+(?P<path>{SUITE_DIR}\S+)[ \t]*$", __import__("re").MULTILINE)


def removed_from_collecting_check(flake_text: str) -> frozenset[str]:
    """The suite files `COLLECTING_CHECK` explicitly deletes before running."""
    region = flake.check_region(flake_text, COLLECTING_CHECK)
    return frozenset(m.group("path") for m in REMOVAL_RE.finditer(region))


def adopted_suites(flake_text: str) -> frozenset[str]:
    """Every file under `SUITE_DIR` that some adopting check installs."""
    out: set[str] = set()
    for check in ADOPTING_CHECKS:
        out |= {p for p in flake.copies(flake.check_region(flake_text, check))
                if p.startswith(SUITE_DIR)}
    return frozenset(out)


def declared_reads() -> dict[str, frozenset[str]]:
    """Each member's own declaration of what it reads, keyed by module name.

    Imported rather than restated here: the declaration belongs beside the reads it describes,
    where somebody adding one will see it.

    A member that does not declare, or declares nothing, is an error rather than a member that
    silently contributes nothing — an empty `READS` would otherwise pass every comparison below
    while the suite read whatever it liked.
    """
    out: dict[str, frozenset[str]] = {}
    for name in MEMBERS:
        module = importlib.import_module(name)
        reads = getattr(module, "READS", None)
        assert reads is not None, (
            f"{name} is a member of the {CHECK_NAME} check but does not declare READS, so its "
            f"reads are compared against nothing. Give it a READS frozenset and an accessor "
            f"that refuses paths absent from it.")
        assert reads, (
            f"{name} declares READS but it is empty, which would let every comparison below "
            f"pass while the suite reads whatever it likes. If it genuinely reads nothing "
            f"outside its own directory, it does not belong in MEMBERS.")
        out[name] = frozenset(reads)
    return out


#: Each member's read gate: the callable every read in it goes through, and a path outside its
#: declaration that the gate must refuse. Named here rather than left to a per-member test,
#: because what stood in for this was three near-identical hand-written tests that a new member
#: had to remember to copy — the "list somebody has to remember to update" this whole design
#: exists to remove, reintroduced one level up.
#:
#: Without it, `declared_reads` only proves a member has a READS attribute. A member could
#: declare one, pass every guard here, and read `REPO_ROOT / "anything"` directly — which makes
#: the declaration exactly the unenforced summary this module's docstring says it is not.
GATES = {
    "test_fixer_escalation": ("doc", "docs/DEPLOY.md"),
    "test_regression_test_redgreen": ("brief", "loops.md"),
    "test_commands_wired": ("_at", "harness/package.nix"),
}


def gate_of(member: str):
    """The named member's read gate, as a callable, with the path it must refuse."""
    module = importlib.import_module(member)
    name, forbidden = GATES[member]
    gate = getattr(module, name, None)
    assert callable(gate), (
        f"{member} is expected to route its reads through `{name}`, which is missing or not "
        f"callable. Either it was renamed — update GATES — or the member stopped gating its "
        f"reads, in which case READS is a comment and the comparisons here prove nothing.")
    return gate, forbidden


def installed(flake_text: str) -> dict[str, str]:
    """Source path -> destination, for everything this check puts in its sandbox."""
    return flake.copies(flake.check_region(flake_text, CHECK_NAME))
