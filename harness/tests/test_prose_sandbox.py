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
