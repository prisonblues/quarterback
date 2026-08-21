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

import _flake_sandbox

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# No module-level `COMMANDS`/`HM_MODULE` paths: they were a route straight past `_at` below,
# and an unused one is worse than none — the next read reaches for the constant that already
# exists rather than the accessor.

#: Every repo path this suite reads, declared for the sandbox that runs it.
#:
#: `harness/commands` is the DIRECTORY, not a list of files, because `_shipped` globs it — "which
#: briefs exist" is the question, so the directory itself is the read and a file list could not
#: express it. `_prose_sandbox` compares this set against what
#: `nix build .#checks.<system>.prose-consistency-tests` installs; a read nobody installed does
#: not FAIL there, it ERRORS on a missing file, which is #163's mechanism and how four suites
#: before this one sat red in a check no workflow runs (#246, #251, #257).
READS = frozenset({"harness/commands", "harness/hm-module.nix"})


def _at(rel: str) -> Path:
    """A repo-root path this suite has declared, or an error naming what to do about it.

    Every filesystem access here goes through this. That is what makes `READS` a declaration
    rather than a summary — without it the set is a comment, and `_prose_sandbox`'s stated
    invariant ("each member routes every read through an accessor that refuses anything absent
    from that declaration") would simply be false for this member, silently, while its guards
    went on reporting the suite as covered.

    The check is by containment rather than equality, because one of this suite's two reads is a
    DIRECTORY it globs: `harness/commands/epic.md` is a declared read by virtue of
    `harness/commands` being one. Containment is `_flake_sandbox.under`, shared with the guard
    that compares this suite against the sandbox — a second hand-written prefix check is how the
    two come to disagree about what "inside" means, and a raw `startswith` lets
    `harness/commands/../hm-module.nix` through a gate whose whole job is to refuse it.
    """
    assert any(rel == d or _flake_sandbox.under(rel, d) for d in READS), (
        f"{rel!r} is read here but is not covered by READS, so flake.nix's "
        f"prose-consistency-tests check does not know to install it — where this read would "
        f"error as a FileNotFoundError rather than be asserted. Add it to READS and install it "
        f"in that check.")
    return REPO_ROOT / rel


def _hm_module() -> Path:
    """`hm-module.nix`, through the gate."""
    return _at("harness/hm-module.nix")


def _brief(name: str) -> Path:
    """One command brief, through the gate."""
    return _at(f"harness/commands/{name}.md")

#: The `commands` option's default: a nix list of bare quoted strings. Read out of the source text
#: rather than by evaluating the module — the point is to be runnable in CI with no nix at all, and
#: the value is a literal list of literals, which cannot mean something else.
_DEFAULT_LIST = re.compile(r"commands = lib\.mkOption \{.*?default = \[(.*?)\];", re.DOTALL)


def _listed() -> set[str]:
    text = _hm_module().read_text(encoding="utf-8")
    match = _DEFAULT_LIST.search(text)
    assert match, "hm-module.nix no longer declares a `commands` default as a literal list"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def _shipped() -> set[str]:
    return {p.stem for p in _at("harness/commands").glob("*.md")}


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
    text = _brief(name).read_text(encoding="utf-8")
    assert re.search(r"^@description \S", text, re.MULTILINE), (
        f"harness/commands/{name}.md has no `@description` line")


# --------------------------------------------- what makes /fix-and-review that command


def command(name: str) -> str:
    return _brief(name).read_text(encoding="utf-8")


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
    # Matched by regex, not substring: the script is invoked by a `$WT_DIR`-relative path that has
    # to be quoted (`"$WT_DIR/scripts/release_stamp.py" preflight`), because `--repo` chooses which
    # repo the plan is built against and NOT where the script is loaded from. Asserting the bare
    # unquoted spelling pinned the invocation to a form that pairs one checkout's tool with another
    # checkout's files.
    assert re.search(r'release_stamp\.py"?\s+preflight', runnable), (
        "the merge-prep step no longer asks whether the release could be stamped")
    assert not re.search(r'release_stamp\.py"?\s+apply', runnable), (
        "prep must not stamp the number — that belongs to whoever merges. The paragraph explaining "
        "why may name `apply`; a code block may not.")


def test_fix_and_land_assembles_the_changelog_before_it_stamps():
    """A fragment names no version, so there is nothing for `apply` to rewrite until `assemble`
    has built the entry. In that order and no other: run the other way round, `apply` sees a
    branch with no placeholder and no release, returns 0, and the fragments land unassembled
    with the release silently unnumbered — a green landing that shipped no entry.

    Both invocations asserted in a code BLOCK, not in prose. A step nobody runs is exactly what
    this file's sibling assertions exist to catch, and a mechanism the landing loop does not
    invoke is a mechanism this repo has documented rather than adopted."""
    runnable = _fenced(command("fix-and-land"))
    assert runnable.strip(), "no code blocks were read out of the file — the fence pattern is wrong"
    assemble = runnable.find("changelog_fragments.py")
    stamp = runnable.find("release_stamp.py")
    assert assemble >= 0, (
        "fix-and-land no longer assembles changelog fragments, so a branch that wrote one lands "
        "with its release entry still sitting in changelog.d/")
    assert stamp >= 0, "the release-number step is gone"
    assert assemble < stamp, (
        "`release_stamp.py apply` runs before `changelog_fragments.py assemble`, so it stamps a "
        "tree whose release entry has not been written yet and reports success having done "
        "nothing")


def test_the_two_end_to_end_commands_point_at_each_other():
    """Whichever one a user reaches first has to be able to tell them it is the wrong one. A
    one-way reference means the mistake is only recoverable from the file they did not open."""
    assert "/fix-and-review" in command("fix-and-land")
    assert "/fix-and-land" in command("fix-and-review")


def test_fix_and_review_resyncs_the_worktree_before_preland():
    """216-F01, the P1. The review sub-agent fixes in a throwaway `git worktree` and pushes
    `HEAD:<branch>` (`panel-review-pr.md` §4), so `$WT_DIR` is left at the commit `/fix-issue` made
    and the branch has moved without it. `preland.py`'s `checkout` check compares
    `git -C $WT_DIR rev-parse HEAD` against the PR's `headRefOid` and fails on mismatch; HOLD
    dominates and the step forbids `--skip`. Without a re-sync, every PR whose review actually
    produced a fix dead-ends at a HOLD describing a stale local checkout — the happy path broken
    for the case the command exists to serve.

    Asserted as an ORDERING over the runnable blocks, not as the presence of a sentence: a
    fast-forward written after preland has already returned its verdict fixes nothing."""
    runnable = _fenced(command("fix-and-review"))
    ff = re.search(r'merge --ff-only', runnable)
    preland = re.search(r'preland\.py', runnable)
    assert ff, "merge prep never fast-forwards $WT_DIR onto the pushed head"
    assert preland, "merge prep never runs preland — the fence pattern or the step is gone"
    assert ff.start() < preland.start(), (
        "the re-sync must come BEFORE preland, or preland still reads the stale checkout it was "
        "the whole point of refreshing")


def test_fix_and_review_asks_harness_rules_about_the_repo_it_was_given():
    """216-F12. `harness_rules.py --repo` is "path or name (default: cwd)", so a bare invocation
    reads THIS checkout's `executor_pr_base` and applies it to a PR in another repo — which is the
    thing the sentence directly above it forbids. The flag is what makes the sentence true."""
    runnable = _fenced(command("fix-and-review"))
    rules = [ln for ln in runnable.splitlines() if "harness_rules.py" in ln]
    assert rules, "step 1 no longer resolves the repo's own answers at all"
    assert any("--repo" in ln for ln in rules), (
        "harness_rules.py is invoked without --repo, so it answers about the cwd while the command "
        "claims to answer about the named repo")


def test_fix_and_review_loads_release_stamp_from_the_worktree_it_asks_about():
    """216-F06. `--repo` chooses which repo the plan is built AGAINST; it does not change where the
    script is loaded FROM. A cwd-relative `python3 scripts/release_stamp.py` therefore pairs one
    checkout's tool with another checkout's files — the exact failure the `preland.py` paragraph
    above it spends five lines refusing."""
    runnable = _fenced(command("fix-and-review"))
    stamp = [ln for ln in runnable.splitlines() if "release_stamp.py" in ln]
    assert stamp, "the release-number question is no longer asked"
    for ln in stamp:
        assert "$WT_DIR/scripts/release_stamp.py" in ln, (
            f"release_stamp.py is loaded from a cwd-relative path: {ln.strip()!r}")


def test_fix_and_reviews_escalation_citation_resolves():
    """216-F14 was a PHANTOM — and this test is the reason it is worth keeping a test here at all.

    The panel reviewed this PR against a base 114 commits behind main and reported that
    `review-pr.md` had no step 3a and that nothing invoked `panel.py --ask`. Both were true of
    THAT base and false of main: `review-pr.md` now carries `#### 3a. When a finding says the
    APPROACH is wrong, escalate it` and invokes `--ask` directly. That is #241 — a round scoped to
    a stale base reporting confidently about code that had already moved.

    So the citation stands, and what needs guarding is the thing the phantom finding was right
    to care about: that it keeps resolving. A cross-file reference is only as good as the target."""
    assert "step 3a" in command("fix-and-review"), (
        "the escalation route lost its citation — a reader cannot find the mechanism")
    assert re.search(r"^#### 3a\.", command("review-pr"), re.MULTILINE), (
        "review-pr.md no longer has a step 3a, so fix-and-review.md now cites nothing — either "
        "restore it there or stop citing it here")


def test_the_reads_are_declared_rather_than_summarised():
    """`READS` is only worth comparing against flake.nix if it is what this suite may actually
    read, and `_at`'s refusal is what makes that true. Asserted rather than trusted: it is the
    mechanism `_prose_sandbox`'s guards rest on, and one `assert` away from being decoration."""
    with pytest.raises(AssertionError, match="not covered by READS"):
        _at("harness/package.nix")
    with pytest.raises(AssertionError, match="install it"):
        _at("docs/DEPLOY.md")


def test_a_path_under_a_declared_directory_is_allowed():
    """The containment rule, which is not incidental: one of this suite's two reads is a
    directory it globs, so every brief beneath it is a declared read and an equality check would
    refuse the suite's own ordinary work."""
    assert _at("harness/commands/epic.md").name == "epic.md"
    assert _at("harness/commands").is_dir()
    # But only genuinely beneath it — component-wise, not by string prefix.
    with pytest.raises(AssertionError, match="not covered by READS"):
        _at("harness/commands-old/epic.md")
