"""A slash command that is not in `hm-module.nix` is a file nobody can run.

#169's failure in the cheapest place it happens: a mechanism that ships unwired. `package.nix`
copies `commands/` wholesale, so writing the markdown feels like shipping it — but what puts a
command in front of a user is `programs.quarterback-harness.commands`, a hand-maintained list in
`hm-module.nix` that links each named file into `~/.claude/commands/`. Miss the entry and the file
is in the store, documented in the README, referenced by its siblings, and absent from the only
directory Claude Code reads. Nothing anywhere says so: `/fix-and-review` simply is not a command.

It fails the other way too, and louder — a name in the list with no file is a missing `source` path
and breaks the home-manager build for everyone who enables the module, which is why the pairing is
asserted in both directions rather than only the one that bit.

**Deliberately not asserted: that every command appears in `loops.md`'s table.** It is the loops
overview, and `/wt`, `/drop-worktree` and `/tree-shake` are not loops; a rule that demanded them
would be wrong on arrival and switched off within a week. What IS asserted about a command's prose
is the pair of properties that distinguish `/fix-and-review` from the command it would otherwise be
— see the end of this file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMMANDS = REPO_ROOT / "harness" / "commands"
HM_MODULE = REPO_ROOT / "harness" / "hm-module.nix"

#: The `commands` option's default: a nix list of bare quoted strings. Read out of the source text
#: rather than by evaluating the module — the point is to be runnable in CI with no nix at all, and
#: the value is a literal list of literals, which cannot mean something else.
_DEFAULT_LIST = re.compile(r"commands = lib\.mkOption \{.*?default = \[(.*?)\];", re.DOTALL)


def _listed() -> set[str]:
    text = HM_MODULE.read_text(encoding="utf-8")
    match = _DEFAULT_LIST.search(text)
    assert match, "hm-module.nix no longer declares a `commands` default as a literal list"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def _shipped() -> set[str]:
    return {p.stem for p in COMMANDS.glob("*.md")}


@pytest.fixture(scope="module")
def listed() -> set[str]:
    return _listed()


@pytest.fixture(scope="module")
def shipped() -> set[str]:
    return _shipped()


def test_the_lists_are_not_empty(listed: set[str], shipped: set[str]):
    """A guard on the guard: both sides are parsed, and an empty set makes every assertion below
    pass by describing nothing."""
    assert len(shipped) > 5
    assert len(listed) > 5


@pytest.mark.parametrize("name", sorted(_shipped()))
def test_every_command_file_is_linked_into_claude_commands(name: str, listed: set[str]):
    """The direction that ships a dead command. `harness/commands/<name>.md` exists, reads well,
    and is not a slash command on any host until this list names it."""
    assert name in listed, (
        f"harness/commands/{name}.md is not in hm-module.nix's `commands` default, so it is never "
        f"linked into ~/.claude/commands and /{name} does not exist for anyone")


@pytest.mark.parametrize("name", sorted(_listed()))
def test_every_linked_command_has_a_file(name: str, shipped: set[str]):
    """The direction that breaks the build. A listed name with no file is a home-manager `source`
    pointing at nothing, for every consumer of the module."""
    assert name in shipped, (
        f"hm-module.nix lists {name!r} but harness/commands/{name}.md does not exist — this fails "
        "the home-manager build, not just this command")


@pytest.mark.parametrize("name", sorted(_shipped()))
def test_every_command_declares_a_description(name: str):
    """`@description` is what Claude Code shows in the command list, and a command nobody can tell
    apart from its siblings is most of the way to not being installed."""
    text = (COMMANDS / f"{name}.md").read_text(encoding="utf-8")
    assert re.search(r"^@description \S", text, re.MULTILINE), (
        f"harness/commands/{name}.md has no `@description` line")


# --------------------------------------------- what makes /fix-and-review that command


def command(name: str) -> str:
    return (COMMANDS / f"{name}.md").read_text(encoding="utf-8")


def test_fix_and_review_says_it_never_merges():
    """Its whole reason to exist beside `/fix-and-land` is the step it does not take. The drift to
    guard is not a deleted sentence but an added convenience — "merge if the gate is green" is one
    edit away, and it silently turns the command a user picked *because* it stops into the one they
    were avoiding."""
    text = command("fix-and-review")
    assert "never merges" in text
    assert "/fix-and-land" in text, "it must name the command that does merge, or the user has to guess"


def _fenced(text: str) -> str:
    """Only the fenced blocks — the commands an agent actually runs.

    Prose about a command is not an instruction to run it, and this file's whole argument turns on
    the difference: the paragraph explaining why the number is not stamped here has to name `apply`
    to explain anything at all."""
    # The leading `[ \t]*` is not cosmetic: every fence in `fix-and-review.md` sits inside a
    # numbered step and is therefore indented, so an anchored ``` matches nothing at all — and a
    # "no forbidden command in any code block" assertion over zero code blocks passes on every
    # possible file.
    return "\n".join(
        re.findall(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```", text, re.DOTALL | re.MULTILINE))


def test_fix_and_review_does_not_stamp_the_release_number():
    """`apply` resolves `vNEXT` against the base as it stands NOW, and this command hands the PR to
    a human who merges later — so a number stamped here can be taken in the meantime, leaving
    `apply` refusing with a hand-edit as the repair. `preflight` asks the same question and spends
    nothing."""
    runnable = _fenced(command("fix-and-review"))
    assert runnable.strip(), "no code blocks were read out of the file — the fence pattern is wrong"
    assert "release_stamp.py preflight" in runnable, (
        "the merge-prep step no longer asks whether the release could be stamped")
    assert "release_stamp.py apply" not in runnable, (
        "prep must not stamp the number — that belongs to whoever merges. The paragraph explaining "
        "why may name `apply`; a code block may not.")


def test_the_two_end_to_end_commands_point_at_each_other():
    """Whichever one a user reaches first has to be able to tell them it is the wrong one. A
    one-way reference means the mistake is only recoverable from the file they did not open."""
    assert "/fix-and-review" in command("fix-and-land")
    assert "/fix-and-land" in command("fix-and-review")
