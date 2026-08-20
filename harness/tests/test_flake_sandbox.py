"""The shared reader that tells a suite what its own nix sandbox contains.

`_flake_sandbox` is single-sourced across the suites with this problem, which means a break in
it breaks all of them at once — and in the direction that matters, silently: a reader that
finds nothing reports either every copy as missing or every read as supplied, depending which
way it is asked. So it is exercised on written-out snippets rather than only against the real
`flake.nix`, where a shape it cannot parse looks the same as a shape that is not there.
"""

from __future__ import annotations

import pytest

import _flake_sandbox as flake

#: A check region with everything the copy reader has to get right: a commented-out copy, a
#: `${./x}` in prose, one in an argument that is not a copy at all, both copy commands, a
#: directory copy, and the spacing a Nix formatter leaves behind.
REGION = """        example-tests = pkgs.runCommand "x" { } ''
          # cp ${./commented-out.md} repo/commented-out.md
          # mentions ${./prose.md} in passing
          install -Dm644 ${./CHANGELOG.md} repo/CHANGELOG.md
          cp ${ ./app/main.py }  repo/app/main.py
          cp -r ${./harness/loops} repo/harness/loops
          pytest -q --ignore=${./not-a-copy.py} tests
          touch $out
        '';
"""


def test_only_real_copy_lines_count_as_copies():
    """The fail-safe direction depends on this. A `${./x}` in a comment or an argument counted
    as a copy is a file the sandbox does not have and the comparison saying it does — the
    sandbox erroring on a missing file with the guard green, which is #163."""
    assert flake.copies(REGION) == {
        "CHANGELOG.md": "repo/CHANGELOG.md",
        "app/main.py": "repo/app/main.py",
        "harness/loops": "repo/harness/loops",
    }


def test_the_region_reader_takes_the_whole_check_and_stops_at_its_end():
    """A check's name occurs in prose and `'';` ends every indented string in flake.nix, so
    neither end can be found by taking the first occurrence of a substring — a slice that ran
    on would credit this check with a neighbour's copies."""
    text = ("        loops-tests = pkgs.runCommand \"a\" { } ''\n"
            "          cp ${./decoy.md} repo/decoy.md\n"
            "        '';\n"
            + REGION
            + "        mcp-tests = pkgs.runCommand \"b\" { } ''\n"
              "          cp ${./later.md} repo/later.md\n"
              "        '';\n")
    assert set(flake.copies(flake.check_region(text, "example-tests"))) == {
        "CHANGELOG.md", "app/main.py", "harness/loops"}


def test_the_region_reader_says_so_when_the_check_is_not_there():
    """A renamed check, which is the whole reason the name is passed in rather than guessed."""
    with pytest.raises(AssertionError, match="0 lines defining"):
        flake.check_region("        loops-tests = pkgs.runCommand \"a\" { } ''\n        '';\n",
                           "example-tests")


def test_the_region_reader_refuses_an_ambiguous_check_name():
    """Two definitions and there is no telling which sandbox feeds the suite."""
    with pytest.raises(AssertionError, match="2 lines defining"):
        flake.check_region(REGION + REGION, "example-tests")


def test_the_region_reader_says_what_it_saw_when_the_check_is_unterminated():
    """The third failure mode, and the one that was going untested — a refactor that dropped or
    inverted this assertion would return a slice running to end-of-file, crediting the check
    with every copy line below it. The message blames a restructured check rather than "the
    parser", because that is the far likelier cause and a message that blames the wrong thing
    sends whoever hits it to the wrong file."""
    with pytest.raises(AssertionError, match="no line closing an indented string"):
        flake.check_region("        example-tests = pkgs.runCommand \"x\" { } ''\n"
                           "          cp ${./CHANGELOG.md} repo/CHANGELOG.md\n",
                           "example-tests")


def test_the_reader_survives_the_spacing_a_formatter_leaves():
    """`^\\s*` would match from a preceding blank line and `= ` with exactly one space each side
    would break on a reindent — both are false failures on a repo where nothing is wrong, which
    is the outcome that gets a guard deleted rather than fixed."""
    assert flake.check_region("\n\n\t example-tests   =  pkgs.runCommand \"x\" { } ''\n"
                             "          cp ${./a.md} repo/a.md\n"
                             "        '';\n", "example-tests")


def test_a_misdirected_copy_is_reported():
    """Supplying a file is not supplying it where the suite reads it: a typo'd destination
    passes a source-only comparison and errors in the build."""
    pairs = {"app/api/reviews.py": "repo/app/api/review.py", "a.md": "repo/a.md"}
    assert flake.misdirected(pairs) == ["app/api/reviews.py -> repo/app/api/review.py"]
    assert flake.misdirected({"a.md": "repo/a.md"}) == []


def test_a_directory_supplies_what_is_under_it_and_only_that():
    """Component-wise, not by string prefix. `harness/loops` is a string prefix of
    `harness/loops_old/x.py`, and a `startswith` would call a file the sandbox does not hold
    supplied — the guard reporting satisfied on a read that errors."""
    sources = {"harness/loops", "flake.nix"}
    assert flake.supplied_by("harness/loops/panel_core.py", sources)
    assert flake.supplied_by("flake.nix", sources)
    assert not flake.supplied_by("harness/loops_old/panel.py", sources)
    assert not flake.supplied_by("harness/commands/review-pr.md", sources)
    assert not flake.supplied_by("harness/loopsfoo", sources)
