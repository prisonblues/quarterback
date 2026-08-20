"""The prose-consistency check installs exactly what its suites read.

The enumeration in `flake.nix` is the thing that goes stale, so nothing relies on somebody
remembering it. This fails in the ordinary `pytest harness/tests` a developer runs before a push
— and in CI's `harness suites` job, which discovers every `harness/**/tests` — rather than
erroring in a nix build that no workflow runs, which is how the four instances of #163 were each
found only by hand.

`_prose_sandbox`'s docstring has the reasoning for why the comparison lives outside the member
suites; the short version is that its converse direction cannot be asked from inside one member.
"""

from __future__ import annotations

import pytest

import _prose_sandbox as contract


@pytest.fixture(scope="module")
def flake_text() -> str:
    """`flake.nix`, or a skip.

    Skipped rather than failed when it is absent: these suites are themselves collected from a
    sandbox, and one that cannot see the expression cannot judge it. The check installs
    `flake.nix` so that this does not skip there — and requires that it does not, since a skip
    would mean this guard had quietly stopped running in the build it protects."""
    flake = contract.REPO_ROOT / "flake.nix"
    if not flake.is_file():
        pytest.skip("no flake.nix beside this checkout, so there is no check to compare against")
    return flake.read_text(encoding="utf-8")


def test_the_check_supplies_every_path_its_suites_read(flake_text):
    """The direction that matters. A declared read with no install line errors in the sandbox as
    a FileNotFoundError, which is not a failure anybody reads — it is an ERROR line in a build
    log, and that is exactly how this went unnoticed four times."""
    installed = contract.installs(flake_text)
    missing = {
        member: sorted(p for p in reads if not contract.supplied_by(p, installed))
        for member, reads in contract.declared_reads().items()
    }
    missing = {m: paths for m, paths in missing.items() if paths}
    assert not missing, (
        f"these suites read paths that flake.nix's {contract.CHECK_NAME} check does not install "
        "into its sandbox, so they will error there as FileNotFoundError rather than be "
        "asserted: "
        + "; ".join(f"{m}: {', '.join(paths)}" for m, paths in sorted(missing.items()))
        + ". Add an `install -Dm644 ${./<path>}` for each")


def test_the_check_installs_no_file_its_suites_do_not_read(flake_text):
    """The other direction, and not symmetry for its own sake. A file the check carries and
    nothing reads is a read that was deleted or renamed while the sandbox went on supplying the
    old path — so the next person to add a read finds an install line already there, for a file
    nothing looks at, and trusts it.

    Trees are exempt and declared as such: one supplies anything beneath it, so it cannot be held
    to this. That is the trade `_prose_sandbox.TREES` records."""
    every_read = frozenset().union(*contract.declared_reads().values())
    unread = sorted(
        contract.installs(flake_text)
        - every_read
        - contract.INSTALLED_BUT_NOT_READ
        - frozenset(contract.TREES))
    unread = [p for p in unread if not p.startswith(contract.SUITE_DIR)]
    assert not unread, (
        f"flake.nix's {contract.CHECK_NAME} check installs files none of its suites read: "
        + ", ".join(unread) + ". Either a read was removed and the install line outlived it, or "
        "the path belongs in INSTALLED_BUT_NOT_READ or TREES, with a reason")


def test_every_member_is_run_by_the_check_and_every_suite_it_runs_is_a_member(flake_text):
    """`MEMBERS` is what `declared_reads` iterates, so a suite the check runs but that is not
    listed there has its reads compared against nothing — a member added to the sandbox and
    silently exempted from the guard. The converse is a member listed here that the check does
    not actually run, whose declaration is then checked against a sandbox it never sees."""
    installed_suites = {p.rsplit("/", 1)[-1].removesuffix(".py")
                        for p in contract.installs(flake_text)
                        if p.startswith(contract.SUITE_DIR) and p.endswith(".py")
                        and p.rsplit("/", 1)[-1].startswith("test_")}
    # This module is installed too and is not a member: it reads flake.nix, not the repo's prose.
    listed = set(contract.MEMBERS) | {"test_prose_sandbox"}
    assert installed_suites == listed, (
        "the suites this check installs and the members it compares have drifted apart. "
        f"installed: {sorted(installed_suites)}; listed in MEMBERS: {sorted(listed)}")


def test_a_member_that_declares_nothing_is_an_error_not_an_exemption():
    """A member contributing no declaration would pass every comparison above while reading
    whatever it liked — the guard reporting green about a suite it is not looking at."""
    with pytest.raises(AssertionError, match="does not declare READS"):
        original = contract.MEMBERS
        try:
            contract.MEMBERS = (*original, "_prose_sandbox")  # a module with no READS
            contract.declared_reads()
        finally:
            contract.MEMBERS = original


def test_the_region_reader_stops_at_the_end_of_its_own_check():
    """`'';` is two characters a shell script may legitimately contain — this check's own script
    prints Nix-quoted advice — and a slice running past the block's end would credit it with a
    neighbouring check's installs, reporting a path as supplied that this sandbox never receives."""
    text = (f"        {contract.CHECK_NAME} = pkgs.runCommand \"a\" {{ }} ''\n"
            "          install -Dm644 ${./app/api/reviews.py} repo/app/api/reviews.py\n"
            "        '';\n"
            "        worktree-tests = pkgs.runCommand \"b\" { } ''\n"
            "          cp -r ${./harness/bin} harness/bin\n"
            "        '';\n")
    assert contract.installs(text) == {"app/api/reviews.py"}


def test_the_region_reader_refuses_an_absent_or_doubled_check():
    """A renamed check, which is the whole reason `CHECK_NAME` is written out. `nix flake check`
    on a flake whose check was renamed says nothing at all about these suites, and a region reader
    that quietly returned "" would report either every read as uninstalled or — after the
    subtractions above — nothing wrong at all."""
    with pytest.raises(AssertionError, match="found 0"):
        contract.check_region("        mcp-tests = pkgs.runCommand \"a\" { } ''\n        '';\n")
    with pytest.raises(AssertionError, match="found 2"):
        contract.check_region(
            f"        {contract.CHECK_NAME} = pkgs.runCommand \"a\" {{ }} ''\n        '';\n"
            f"        {contract.CHECK_NAME} = pkgs.runCommand \"b\" {{ }} ''\n        '';\n")


def test_only_an_install_counts_as_supplying_a_path():
    """`${./x}` is Nix putting a path in the store, which is not the same as the sandbox having
    that file where a suite reads it. A commented-out line and a script named as an argument both
    interpolate a store path, and neither is an install."""
    block = ("          # install -Dm644 ${./app/models/review.py} repo/app/models/review.py\n"
             "          bash ${./scripts/release_stamp.py} check\n"
             "          install -Dm644 ${./harness/README.md} repo/harness/README.md\n"
             "          cp -r ${./harness/loops} repo/harness/loops\n")
    assert set(contract.INSTALL_RE.findall(block)) == {"harness/README.md", "harness/loops"}


def test_a_tree_supplies_what_is_under_it_and_only_that():
    """Component-wise, not by string prefix. `harness/loops` is a string prefix of
    `harness/loops_old/x.py`, and a `startswith` would call a file supplied that the sandbox
    does not hold — the guard reporting satisfied on a read that errors."""
    installed = frozenset({"harness/loops", "flake.nix"})
    assert contract.supplied_by("harness/loops/panel_core.py", installed)
    assert contract.supplied_by("flake.nix", installed)
    assert not contract.supplied_by("harness/loops_old/panel.py", installed)
    assert not contract.supplied_by("harness/commands/review-pr.md", installed)
