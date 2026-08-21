"""The prose-consistency check installs exactly what its suites read, where they read it.

The enumeration in `flake.nix` is the thing that goes stale, so nothing relies on somebody
remembering it. This fails in the ordinary `pytest harness/tests` a developer runs before a
push — and in CI's `harness suites` job, which discovers every `harness/**/tests` — rather than
erroring in a nix build that no workflow runs, which is how all five instances of #163's
mechanism were found only by hand.

`_prose_sandbox`'s docstring has the reasoning for why the comparison lives outside the member
suites, and for the one thing a declaration-based contract cannot catch.
"""

from __future__ import annotations

import pytest

import _flake_sandbox as flake
import _prose_sandbox as contract


@pytest.fixture(scope="module")
def flake_text() -> str:
    """`flake.nix`, or a skip.

    Skipped rather than failed when it is absent: these suites are themselves collected from a
    sandbox, and one that cannot see the expression cannot judge it. The check installs
    `flake.nix` so that this does not skip there — and the check requires that it does not,
    since a skip would mean this guard had quietly stopped running in the build it protects."""
    path = contract.REPO_ROOT / "flake.nix"
    if not path.is_file():
        pytest.skip("no flake.nix beside this checkout, so there is no check to compare against")
    return path.read_text(encoding="utf-8")


def test_the_check_supplies_every_path_its_suites_read(flake_text):
    """The direction that matters. A declared read with no install line errors in the sandbox as
    a FileNotFoundError, which is not a failure anybody reads — it is an ERROR line in a build
    log, and that is how this went unnoticed five times."""
    sources = set(contract.installed(flake_text))
    missing = {member: sorted(p for p in reads if not flake.supplied_by(p, sources))
               for member, reads in contract.declared_reads().items()}
    missing = {m: paths for m, paths in missing.items() if paths}
    assert not missing, (
        f"these suites read paths that flake.nix's {contract.CHECK_NAME} check does not install "
        "into its sandbox, so they will error there as FileNotFoundError rather than be "
        "asserted: "
        + "; ".join(f"{m}: {', '.join(paths)}" for m, paths in sorted(missing.items()))
        + ". Add an `install -Dm644 ${./<path>}` for each")


def test_every_install_lands_where_the_suites_will_look(flake_text):
    """Supplying a file is not the same as supplying it at the path a suite reads.

    `install ${./app/api/reviews.py} repo/app/api/review.py` puts *a* file in the sandbox and
    the suite reading `app/api/reviews.py` still errors on a missing one — the guard above
    reporting green while the build fails, which is the exact failure this whole check exists
    to close. Comparing source paths alone cannot see it."""
    wrong = flake.misdirected(contract.installed(flake_text))
    assert not wrong, (
        f"flake.nix's {contract.CHECK_NAME} check installs files to destinations that do not "
        f"mirror their repo paths under `{flake.SANDBOX_PREFIX}`, so the suites will not find "
        "them where they look: " + ", ".join(wrong))


def test_the_check_installs_no_file_its_suites_do_not_read(flake_text):
    """The other direction, and not symmetry for its own sake. A file the check carries and
    nothing reads is an install line that outlived the read it was added for — so the next
    person to add a read finds a line already there, for a file nothing looks at, and trusts it.

    Note what this does NOT catch, per `_prose_sandbox`'s docstring: a read whose last call site
    was deleted while its DECLARATION stayed. The declaration still matches the install, and
    both directions stay green. `test_every_declared_read_exists` is the mitigation, and it is
    a narrower one."""
    every_read = frozenset().union(*contract.declared_reads().values())
    unread = sorted(
        set(contract.installed(flake_text))
        - every_read
        - contract.INSTALLED_BUT_NOT_READ
        - frozenset(contract.TREES))
    unread = [p for p in unread if not p.startswith(contract.SUITE_DIR)]
    assert not unread, (
        f"flake.nix's {contract.CHECK_NAME} check installs files none of its suites read: "
        + ", ".join(unread) + ". Either a read was removed and the install line outlived it, or "
        "the path belongs in INSTALLED_BUT_NOT_READ or TREES, with a reason")


def test_every_declared_read_exists():
    """A declaration pointing at a file nobody has.

    This is what is left of catching a stale declaration once the syntax-tree route is off the
    table: it cannot tell that the last reader of a path was deleted, but it can tell that the
    path itself is gone — which is the case a rename produces, and the case where the install
    line and the declaration would otherwise agree with each other about a file that is not
    there."""
    for member, reads in contract.declared_reads().items():
        for rel in sorted(reads):
            assert (contract.REPO_ROOT / rel).exists(), (
                f"{member} declares a read of {rel!r}, which does not exist in this checkout. "
                f"It was renamed or removed: update READS and the {contract.CHECK_NAME} check "
                f"together.")


def test_every_tree_is_installed_as_a_directory(flake_text):
    """`TREES` is otherwise only ever SUBTRACTED — from the converse guard, so that a directory
    install is not reported as unread. Nothing above asserts a key is installed at all, or that
    it is a directory.

    Both matter. A stale entry survives forever once the install line goes, and the failure is
    the one this check exists to prevent: swap `cp -r ${./harness/loops}` for the two loops
    files that happen to be in READS and every guard here stays green while the sandbox dies at
    `import panel_core`, because package importability is exactly what a TREES entry claims and
    nothing else checks. And a plain file's path added to TREES would be a working way to
    silence the converse guard rather than answer it."""
    sources = set(contract.installed(flake_text))
    for tree in sorted(contract.TREES):
        assert tree in sources, (
            f"{tree!r} is declared in TREES — a dependency the suites need present but do not "
            f"read file by file — but the {contract.CHECK_NAME} check does not install it. "
            f"Either install it, or drop the TREES entry: as it stands the entry only "
            f"suppresses the converse guard.")
        assert (contract.REPO_ROOT / tree).is_dir(), (
            f"{tree!r} is declared in TREES but is not a directory in this checkout. TREES is "
            f"for whole directories, whose contents a path-based guard cannot enumerate; a "
            f"plain file belongs in a member's READS or in INSTALLED_BUT_NOT_READ.")


def test_every_member_is_run_by_the_check_and_every_suite_it_runs_is_a_member(flake_text):
    """A suite the check runs but that is not in `MEMBERS` has its reads compared against
    nothing — a member added to the sandbox and silently exempted from the guard. The converse
    is a member listed there that the check does not run, whose declaration is then checked
    against a sandbox it never sees."""
    installed_suites = {p.rsplit("/", 1)[-1].removesuffix(".py")
                        for p in contract.installed(flake_text)
                        if p.startswith(contract.SUITE_DIR) and p.endswith(".py")
                        and p.rsplit("/", 1)[-1].startswith("test_")}
    # This module and the shared reader's own suite are installed too and are not members:
    # they read flake.nix, not the repo's prose.
    listed = set(contract.MEMBERS) | {"test_prose_sandbox", "test_flake_sandbox"}
    assert installed_suites == listed, (
        "the suites this check installs and the members it compares have drifted apart. "
        f"installed: {sorted(installed_suites)}; expected: {sorted(listed)}")


def test_a_member_that_declares_nothing_is_an_error_not_an_exemption(monkeypatch):
    """Both ways a member can contribute no declaration while still being counted as covered:
    exposing no `READS` at all, and exposing an empty one. Either would pass every comparison
    above while the suite read whatever it liked.

    `monkeypatch` rather than a hand-written try/finally: this module's other tests read
    `MEMBERS` through `declared_reads`, so a restore that does not run leaks into them."""
    monkeypatch.setattr(contract, "MEMBERS", (*contract.MEMBERS, "_prose_sandbox"))
    with pytest.raises(AssertionError, match="does not declare READS"):
        contract.declared_reads()

    monkeypatch.setattr(contract, "MEMBERS", ("test_commands_wired",))
    monkeypatch.setattr("test_commands_wired.READS", frozenset())
    with pytest.raises(AssertionError, match="empty"):
        contract.declared_reads()


def test_the_removal_rule_covers_suites_and_exempts_helpers():
    """Pins the narrowing #230 made, in both directions, because the exemption is the kind
    that gets widened by the next person who finds the rule inconvenient.

    A suite is collected wherever it lands, so it must be removed from the collecting check.
    A helper is not collected at all, so requiring its removal forbids two sandboxes sharing
    one parser — which is what `_flake_sandbox` was factored out to allow."""
    assert contract.collectible("harness/tests/test_commands_wired.py")
    assert not contract.collectible("harness/tests/_flake_sandbox.py")
    assert not contract.collectible("harness/tests/_prose_sandbox.py")
    # Not a matter of the leading underscore: anything outside the suite directory is not this
    # rule's business either, and a `conftest.py` is collected by pytest but is not a suite
    # whose reads this contract describes.
    assert not contract.collectible("harness/commands/review-pr.md")
    assert not contract.collectible("harness/tests/conftest.py")


def test_every_member_is_removed_from_the_check_that_would_collect_it(flake_text):
    """Joining this category costs four steps, not three, and nothing used to check the fourth.

    `worktree-tests` copies `harness/tests` in wholesale, so a new member is collected there too
    — in a sandbox that holds none of what it reads. Every guard above would report green while
    the suite errored in that build, which is #163's mechanism arriving through the fix for it.

    `test_commands_wired.py` is why this is not hypothetical: it sat erroring in `worktree-tests`
    from the day it landed until somebody happened to run the flake and hand-write the `rm`."""
    removed = contract.removed_from_collecting_check(flake_text)
    # Only what pytest would COLLECT there. A helper module is not collected, so sharing one
    # between the two sandboxes is inert — and since #230 it is also necessary, because
    # `worktree-tests` has a member that declares its reads through `_flake_sandbox` too.
    # See `contract.collectible`.
    installed_here = {p for p in contract.installed(flake_text) if contract.collectible(p)}
    still_collected = sorted(installed_here - removed)
    assert not still_collected, (
        f"these files are installed into {contract.CHECK_NAME} but are not removed from "
        f"{contract.COLLECTING_CHECK}, which copies {contract.SUITE_DIR} in wholesale — so they "
        f"run there too, in a sandbox that does not hold what they read, and error rather than "
        f"fail: " + ", ".join(still_collected) + f". Add an `rm <path>` to {contract.COLLECTING_CHECK}")


def test_the_collecting_check_removes_nothing_that_is_not_ours(flake_text):
    """The converse, and the sharper question: a removal is fine if SOME check adopts the file,
    and wrong only if none does. Asked the narrow way first — is it in this check — it reported
    `release-metadata-tests`' own suite as homeless, which is how the list of adopting checks
    came to be written down.

    What it catches is a suite renamed on one side of the pair, or dropped from an adopting
    check while its `rm` stayed: the file then runs in no sandbox at all, and every guard that
    only looks at installs is satisfied because there is nothing left to look at."""
    orphaned = sorted(contract.removed_from_collecting_check(flake_text)
                      - contract.adopted_suites(flake_text))
    assert not orphaned, (
        f"{contract.COLLECTING_CHECK} removes files that no adopting check installs "
        f"({', '.join(contract.ADOPTING_CHECKS)}), so they now run in NO check at all: "
        + ", ".join(orphaned) + ". Either install them in the check that should run them, or "
        "drop the `rm` so worktree-tests keeps collecting them")


@pytest.mark.parametrize("member", contract.MEMBERS)
def test_every_member_actually_enforces_its_declaration(member):
    """`declared_reads` proves a member HAS a READS set. This proves the set is enforced.

    Without it a member could declare its reads, pass every comparison in this module, and go on
    reading `REPO_ROOT / "anything"` directly — leaving the declaration the unenforced summary
    the docstring promises it is not, and every guard here reporting the suite as covered.

    Parametrised over `MEMBERS` rather than written once per member: three near-identical
    hand-copied tests were what stood in for this, and a fourth member joining had to remember
    to write the fourth — which is the class of thing this design exists to stop relying on."""
    gate, forbidden = contract.gate_of(member)
    with pytest.raises(AssertionError):
        gate(forbidden)


@pytest.mark.parametrize("member", contract.MEMBERS)
def test_every_member_gates_the_reads_it_does_declare(member):
    """The other half: a gate that refuses everything is as useless as one that refuses nothing,
    and would pass the test above. Each member's own declared reads must get through it."""
    gate, _ = contract.gate_of(member)
    reads = contract.declared_reads()[member]
    # The gates take what their own suite naturally passes — a repo-relative path, a bare brief
    # filename — so this asks each one for something it must accept rather than a uniform shape.
    accepted = {"test_fixer_escalation": lambda: gate(sorted(reads)[0]),
                "test_regression_test_redgreen": lambda: gate("review-pr.md"),
                "test_commands_wired": lambda: gate("harness/hm-module.nix")}[member]
    assert accepted() is not None
