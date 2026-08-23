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
#:
#: `.github/workflows/tests.yml` and `harness/loops/preland.py` arrived with the landing-hazards
#: guards at the end of this file: the hazards section points a lander at a CI job by id and at
#: preland's refusal sentence, and a pointer is only worth writing while it resolves. Reading the
#: workflow is what makes a renamed job a failure here rather than a page quietly citing a check
#: that will never appear in anybody's PR. `harness/loops` is already a sandbox TREE (it has to be
#: importable for a sibling suite), so only the workflow needed a new install line.
READS = frozenset({"harness/commands", "harness/hm-module.nix",
                   "harness/loops/preland.py", ".github/workflows/tests.yml"})


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


def test_no_brief_tells_an_agent_to_stamp_a_release():
    """The most important assertion in this file, and the one #122 is a report about.

    Five agents stamped in one night and every one of them was following a document. A
    mechanism removed from the code and left in the brief has not been removed — the code
    refuses, the agent retries or works around it, and the refusal is what looks broken. So
    this pins the CODE BLOCKS of every brief the harness ships: prose may explain what used to
    happen and why it does not any more, and a fence may not carry the command.
    """
    forbidden = (
        r"release_stamp\.py",                       # the tool itself, gone since #122
        r"changelog_fragments\.py\"?\s+assemble",    # the branch-side entry builder
        r"release_tag\.py\"?\s+reserve",             # the push-time number reservation
        r"release\.py\"?\s+run",                     # the release job — `main` only, never a brief
    )
    for name in sorted(_shipped()):
        runnable = _fenced(command(name))
        for pattern in forbidden:
            assert not re.search(pattern, runnable), (
                f"{name}.md has a code block running `{pattern}`. A branch names no version and "
                "cuts no release; the number is applied on the base after the merge (#122). "
                "Explain it in prose if it needs explaining — do not hand an agent the command.")


def test_the_briefs_that_land_say_where_a_branch_writes_instead():
    """A refusal that names no alternative gets retried or worked around, and a brief that
    removes a step without naming the replacement is the same failure a document earlier. Both
    landing briefs have to carry the fragment path."""
    for name in ("fix-and-land", "fix-and-review", "panel-review-pr"):
        assert "changelog.d/<issue>.<kind>.md" in command(name), (
            f"{name}.md no longer tells an agent where a release note goes")


def test_fix_and_review_asks_whether_the_branch_carries_a_release_note():
    """The cheap question that survived: finding out here costs a commit, and finding out at
    the merge costs a cycle."""
    runnable = _fenced(command("fix-and-review"))
    assert runnable.strip(), "no code blocks were read out of the file — the fence pattern is wrong"
    # Matched by regex, not substring: the script is invoked by a `$WT_DIR`-relative path that
    # has to be quoted, because `--repo` chooses which repo the answer is computed against and
    # NOT where the script is loaded from.
    assert re.search(r'changelog_fragments\.py"?\s+required', runnable), (
        "the merge-prep step no longer asks whether this branch carries its release note")
    # 216-F06, kept: `--repo` chooses which repo the answer is computed AGAINST; it does not
    # change where the script is loaded FROM. A cwd-relative invocation pairs one checkout's
    # tool with another checkout's files.
    for ln in (l for l in runnable.splitlines() if "changelog_fragments.py" in l):
        assert "$WT_DIR/scripts/changelog_fragments.py" in ln, (
            f"changelog_fragments.py is loaded from a cwd-relative path: {ln.strip()!r}")


def test_the_panel_offer_asks_whether_the_branch_carries_a_release_note():
    """preland has no check for it and never returns RECONCILE for it, so this step asks or
    nobody does. What it must NOT do is anything about a number: the release is cut on the base
    after the merge, once per batch, and never from a review (#122)."""
    step = _panel_review_verdict_step()
    runnable = _fenced(step)
    assert runnable.strip(), "no code blocks were read out of step 7 — the fence pattern is wrong"
    assert re.search(r"changelog_fragments\.py\s+required", runnable), (
        "step 7 never asks whether the branch carries its release note, so the offer is made "
        "without the one fact #365 is about")


def test_the_panel_merge_sequence_pushes_nothing_between_the_gate_and_the_merge():
    """What the release step's removal bought, and it has to be said rather than left to be
    noticed. The stamp commit used to be pushed after the last verification of the cycle, which
    moved the head past the round §5 had read — so the gate had just verified a commit that was
    no longer the head, and a re-run HOLDed about a commit two tools wrote. Nothing is pushed
    there now, so the READY describes the commit that actually lands."""
    merging = _section(_panel_review_verdict_step(), "Merging, once the user has said yes")
    runnable = _fenced(merging)
    assert "gh pr merge" in runnable, "the merge sequence lost its merge"
    assert "git commit" not in runnable and "git push" not in runnable, (
        "the merge sequence pushes a commit between the gate and the merge again, so the READY "
        "above it describes a commit that is no longer the head (#122)")


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


# ------------------------------------- the last step of a review: the verdict, and who may offer
#
# #100. `/panel-review-pr` spends several rounds computing whether a PR is in a landable state and
# used to end with "merge if the user asks" — everything the rounds established, discarded at the
# last step. The offer is now gated on a verdict; these pin the gate, and pin that the offer stays
# where it was earned.


def _section(text: str, heading: str) -> str:
    """One `## `/`### ` section of a brief, up to the next heading of that level or above.

    Section-scoped rather than file-wide because the words this file asserts on ("offer",
    "READY") appear all over `panel-review-pr.md`, and a substring search across 700 lines would
    go green on a sentence in §5 while §7 said the opposite."""
    match = re.search(rf"^(#+) {re.escape(heading)}", text, re.MULTILINE)
    assert match, f"no section headed {heading!r} — it was renamed, or this assertion is stale"
    depth = len(match.group(1))
    rest = text[match.end():]
    end = re.search(rf"^#{{1,{depth}}} ", rest, re.MULTILINE)
    return rest[:end.start()] if end else rest


def _panel_review_verdict_step() -> str:
    """§7, whatever it is called — matched on what it does, not on a title that may be reworded."""
    heads = re.findall(r"^## (7\..*)$", command("panel-review-pr"), re.MULTILINE)
    assert len(heads) == 1, f"panel-review-pr.md has {len(heads)} step 7 headings, expected one"
    return _section(command("panel-review-pr"), heads[0])


def test_the_review_ends_by_running_the_gate_rather_than_on_request():
    """The inversion #100 asked for. A gate the user has to know to ask for is a gate that did not
    reach them: preland HELD PR #299 twice on findings nobody had been told about, both times only
    because somebody thought to run it by hand."""
    step = _panel_review_verdict_step()
    assert "preland.py" in step, "the last step no longer runs the pre-land gate at all"
    assert re.search(r"--json", step), (
        "the gate is run without `--json`, so its verdict reaches the step as a report to "
        "paraphrase rather than a payload to act on")
    assert not re.search(r"^## 7\. Merging \(only if the user asks\)", command("panel-review-pr"),
                         re.MULTILINE), (
        "step 7 has gone back to running only on request, which is the whole of #100")


@pytest.mark.parametrize("verdict", ("READY", "RECONCILE", "HOLD"))
def test_the_last_step_rules_on_every_verdict_the_gate_can_return(verdict: str):
    """Three exits, three answers. A verdict with no branch written for it is a model improvising
    one, which is what the gate exists to stop."""
    step = _panel_review_verdict_step()
    assert re.search(rf"^### {verdict}\b", step, re.MULTILINE), (
        f"step 7 has no `### {verdict}` branch, so what the skill does with that verdict is "
        f"whatever the model decides on the day")


def test_the_offer_to_land_is_gated_on_ready():
    """READY offers; HOLD refuses to, in those words. The drift to guard is not a deleted
    sentence but a softened one — "shall I merge anyway" after a HOLD is one edit away, and it
    hands the user the merge they think they are getting rather than the one on screen."""
    step = _panel_review_verdict_step()
    ready = _section(step, "READY — offer, and say what the offer rests on")
    hold = _section(step, "HOLD — do not offer")
    assert "offer" in ready.lower(), "the READY branch no longer offers to land"
    assert re.search(r"do not offer|no offer", hold, re.IGNORECASE), (
        "the HOLD branch does not forbid the offer, so the verdict gates nothing")
    assert re.search(r"verbatim", hold), (
        "a HOLD that does not relay preland's `reasons` verbatim is a HOLD reported as a mood")


def test_reconcile_does_the_mechanical_work_and_re_runs_the_gate_before_offering():
    """The RECONCILE path is the one that must not shortcut into an offer. It may take the
    mechanical commits — that is what makes it worth having — but the verdict it offers on has to
    be the one computed AFTER them, because the push that carried them restarted CI."""
    reconcile = _section(_panel_review_verdict_step(),
                         "RECONCILE — do the mechanical work, re-verify, then offer")
    assert "actions" in reconcile, "the RECONCILE branch no longer reads preland's `actions`"
    assert re.search(r"run the gate again|re-?run", reconcile, re.IGNORECASE), (
        "RECONCILE does the work and then offers on the stale verdict that asked for it")
    # And the re-verification has to be one that can actually come back READY. preland's `review`
    # check compares the round's `head_sha` against the PR's head, so the push that carried the
    # mechanical commits has just invalidated the round — a gate re-run alone HOLDs forever, and
    # a branch of the skill that can never reach its own offer is worse than not having it.
    assert "head_sha" in reconcile and re.search(r"--round|another round|re-anchor", reconcile), (
        "RECONCILE pushes a commit and then re-runs the gate with nothing re-reading the new "
        "head, so preland HOLDs on `head_sha` and this path can never reach the offer it "
        "promises")


def test_an_unearned_stop_blocks_the_offer_even_when_the_gate_would_otherwise_pass():
    """The third #100 input, and the one that is easy to leave as prose. `confident: false` means
    a reviewer read a prefix, or never ran, or the cap ran out — already computed, already
    reported to a human, and until now not wired to anything. Both halves are asserted: the flag
    that makes preland's own verdict strict, and the cross-check against the round payload this
    step already holds, because `stop_confident` is nullable and a board that never took the round
    leaves it null rather than false."""
    step = _panel_review_verdict_step()
    assert "--require-earned-stop" in _fenced(step), (
        "step 7 runs preland without `--require-earned-stop`, so an unearned stop is a warning "
        "nobody on this path reads and the offer goes out on a round that nobody finished")
    assert re.search(r"round_stop\.confident|`confident`", step), (
        "step 7 never looks at the round's own `confident`, so a round the board recorded with a "
        "null `stop_confident` reaches the offer as though the stop had been earned")


def test_the_merge_is_claimed_and_re_verified_before_it_is_taken():
    """#99: two agents accepting the same offer at the same moment. The claim is advisory and
    that is the point — it is the only thing between them, and it has to be taken BEFORE the
    merge rather than recorded after it. Asserted over the runnable blocks: a paragraph about
    claiming is not a claim."""
    runnable = _fenced(_panel_review_verdict_step())
    claim = runnable.find("qb-claim")
    merge = runnable.find("gh pr merge")
    assert merge >= 0, "step 7 never actually merges in a runnable block"
    assert claim >= 0, (
        "the merge is taken without a `qb-claim` on the branch, so two agents accepting the offer "
        "at once both merge — which is what happened on the day the claim was written")
    assert claim < merge, "the claim is taken after the merge, which serialises nothing"
    assert re.search(r"--merge\b", runnable) and not re.search(r"--squash", runnable), (
        "step 7 must preserve the commits: the fix commits and the rounds that reviewed them are "
        "the record of the cycle, and a squash throws away the correspondence")


def test_bare_panel_reports_land_readiness_and_never_offers_to_merge():
    """`/panel` is review-only by design and gets pointed at other people's PRs, in repos the
    caller may not own. Reporting `gates: READY / blocked by X` is useful; an offer to merge one
    of those is a footgun whether or not it is accepted. It also has to name the command that HAS
    earned the offer, or the reader is left to guess where the step went."""
    text = command("panel")
    assert re.search(r"preland\.py", text), (
        "panel.md no longer says how to report land-readiness, which is the half of #100 that is "
        "in scope for it")
    assert re.search(r"never (?:\w+ ){0,4}offers? to (?:merge|land)|never offer", text,
                     re.IGNORECASE), (
        "panel.md does not forbid the offer to merge — and 'it never merges' is not the same "
        "prohibition, because proposing the merge is the footgun in a repo the caller does not own")
    assert "/panel-review-pr" in text, (
        "panel.md refuses the offer without naming the command that may make it")


# ------------------------------------- #227: the queue that has to be binding


def test_fix_and_land_joins_the_queue_before_it_spends_a_ci_run():
    """#317 built the queue and stopped at the contract, saying so: *"nothing yet forces the
    stop."* A queue nothing enqueues into is #169's defect exactly — a mechanism that ships
    unwired — and one enqueued into after the integration is a queue that orders nothing, because
    the expensive half is the integration and each loser's push invalidates the winner's green
    checks on the way past. So the ordering is the assertion, over the runnable blocks."""
    runnable = _fenced(command("fix-and-land"))
    enqueue = runnable.find("merge_queue_enqueue")
    gate = runnable.find("preland.py")
    assert enqueue >= 0, (
        "fix-and-land never enqueues, so its PRs are invisible to every other agent's queue check "
        "and the line it is meant to stand in has nobody in it")
    assert gate >= 0, "the pre-land gate is gone from the runnable blocks"
    assert enqueue < gate, (
        "the queue is joined after the gate has already run, so a PR third in line has done the "
        "expensive work before anything could tell it not to")


def test_fix_and_land_releases_its_place_on_every_exit_that_is_not_about_the_queue():
    """An entry is a lease and a lease nobody releases is a queue that jams — worse than no queue,
    because everybody behind it waits for a land that already happened. Both exits are asserted:
    the merge, and the hold."""
    text = command("fix-and-land")
    assert "merge_queue_leave" in _fenced(text), (
        "nothing in fix-and-land ever leaves the queue, so a merged or held PR holds its place "
        "until the TTL expires")
    runnable = _fenced(text)
    assert runnable.find("merge_queue_leave") < runnable.rfind("merge_queue_leave"), (
        "the queue is left in only one place — the merge and the hold are different exits and "
        "both have to release the lease")
    assert re.search(r'reason="merged"', runnable), "the merge exit does not say why it left"
    assert re.search(r'reason="held', runnable), "the hold exit does not say why it left"


def test_the_one_stop_that_keeps_its_place_is_the_one_about_position():
    """A loop that read "not your turn" as "leave the queue" would go to the back of the line every
    time it was overtaken, which starves the PR — strictly worse than the racing the queue
    replaced. So the queue stand-down is the exception, and it has to be written as one."""
    text = command("fix-and-land")
    assert re.search(r"[Dd]o not leave the queue here", text.replace("*", "")), (
        "the position stand-down does not say to keep the entry, so the obvious reading of "
        "'stop and clean up' sends the PR to the back of its own line")
    assert re.search(r"re-join at the back", text), "nothing says what leaving would cost"


def test_a_lone_pr_meets_no_queue_at_all():
    """The other half of the contract, and the thing that keeps this from being a gate people turn
    off. preland owns the behaviour; what the brief must not do is add a wait of its own on top of
    a verdict that already came back READY."""
    text = command("fix-and-land")
    assert re.search(r"empty|nobody is queued|nothing else queued|no new friction|"
                     r"idempotent", text, re.IGNORECASE), (
        "fix-and-land says nothing about the case where nobody else is in the line, so a reader "
        "has no way to tell that the queue is meant to be free for a lone PR")


@pytest.mark.parametrize("name", ("fix-and-land", "panel-review-pr"))
def test_the_landing_claim_is_taken_on_the_base_branch(name: str):
    """#318. `kind=merge` keys on a branch and nothing in the ref name says which one, so the
    answer lives in the callers. Under the head-branch reading two agents landing two DIFFERENT
    PRs into `main` hold `<repo>:feat/a` and `<repo>:feat/b`, never see each other, and both
    merge — which is the incident `check_merge_claim`'s docstring cites. It is also the key
    `preland.py` reads and the key the merge queue reports beside its line, so a caller claiming
    the head names one land two ways."""
    runnable = _fenced(command(name))
    claim = re.search(r"qb-claim branch (\S+)", runnable)
    assert claim, f"{name} no longer claims a branch before merging"
    assert re.search(r"BASE|base", claim.group(1)), (
        f"{name} claims {claim.group(1)} — the PR's own head branch — so it serialises two agents "
        "landing the SAME pr and not the collision that actually happens, which is two agents "
        "landing different PRs onto one base")


def test_fix_and_land_claims_the_base_before_it_merges():
    """The queue orders; the claim is the one slot held across the merge itself. Being at the head
    is permission to go and ask for the claim, and an autonomous loop that merged on the strength
    of its queue position alone would have made the queue a second lock — the thing #317 says in
    its first paragraph it must not become."""
    runnable = _fenced(command("fix-and-land"))
    claim = runnable.find("qb-claim")
    merge = runnable.find("gh pr merge")
    assert merge >= 0, "fix-and-land never merges in a runnable block"
    assert claim >= 0, (
        "fix-and-land merges without taking `kind=merge`, so two agents each at the head of the "
        "queue for their own base — or one agent and one human in the UI — both merge")
    assert claim < merge, "the claim is taken after the merge, which serialises nothing"
    assert "--claim-holder" in runnable, (
        "the re-verification after claiming does not pass `--claim-holder`, so the loop's own "
        "claim is read as somebody else's and it holds its own merge")


def test_the_landing_claim_is_released_on_every_exit_after_it_is_taken():
    """Codex, round 1. The claim is a worse thing to leak than a queue place: it is preland's
    `merge_claim` check answering "somebody is landing onto $BASE" to every other agent in the
    fleet, for the rest of its TTL, about a land that is not happening — so nobody merges onto that
    base in the meantime. The post-claim re-verification can come back anything but READY, and that
    exit has to release it too."""
    text = command("fix-and-land")
    runnable = _fenced(text)
    assert "release_claim" in runnable, (
        "fix-and-land takes `kind=merge` and never releases it, so a loop that stopped between the "
        "claim and the merge blocks every other agent's landing until the TTL expires")
    assert re.search(r"claim_id=\$\(", runnable), (
        "nothing captures the claim id `qb-claim` prints on stdout, so there is no id to release "
        "with — the release step above cannot actually be run")
    assert re.search(r"every exit releases it", text), (
        "the release reads as a step in the happy path only; the exit that matters is the one where "
        "the re-verification did not come back READY")


def test_the_head_asserts_its_readiness_on_the_line_before_it_merges():
    """`ready` is the only verdict that lets a queue head merge, and it is pinned to a commit — the
    board drops it the moment the head moves, which is what an agent's own memory of "preland said
    READY" structurally cannot do. Asserted as an ordering: said at 4a it would be a lie, and said
    after the merge it would be a green light on a PR that has already gone."""
    runnable = _fenced(command("fix-and-land"))
    ready = runnable.find('verdict="ready"')
    merge = runnable.find("gh pr merge")
    assert ready >= 0, (
        "the head never tells the line it is ready, so `GET /merge-queue` reports the PR about to "
        "land as not-ready and preland's own advice to re-enqueue at this head goes nowhere")
    assert ready < merge, "readiness is asserted after the merge, which is a green light on a PR that has gone"


@pytest.mark.parametrize("name", ("fix-and-land", "panel-review-pr"))
def test_the_landing_claim_is_taken_on_a_bounded_ttl(name: str):
    """Keying the claim on the base (#318) widened what a leaked one costs: it blocks every merge
    onto that base rather than one branch's, and `preland`'s `merge_claim` check is what makes that
    a hard stop for everybody. The TTL is the only backstop for a session that ends between the
    claim and the release, and the board's default is an hour."""
    runnable = _fenced(command(name))
    claim = re.search(r"qb-claim branch \S+([^\n]*)", runnable)
    assert claim, f"{name} no longer claims a branch before merging"
    assert "--ttl" in claim.group(1), (
        f"{name} takes the landing claim on the board's default hour, so a session that dies "
        "between the claim and the merge blocks every land onto that base for an hour")


# --------------------------------------------------- the landing hazards (#367)
#
# `fix-and-land.md` covered the decisions and said nothing about what goes wrong while you
# carry them out, so nine landings on 2026-08-22 were each briefed by hand with the same seven
# warnings and the knowledge went with the session. The section that fixes that is prose, and
# prose about tooling rots in a way that is invisible from inside the prose: a job renamed in
# `tests.yml` leaves the page pointing at a guard that does not exist, and pointing at nothing
# reads exactly like pointing at something. These assertions are the anchors, in the same
# spirit as `test_fixer_escalation.py`'s — they check that what the page points AT is still
# there, and deliberately not that any of it is good advice.

#: The heading the section lives under, and therefore the token every pointer at it uses.
#: One string because three files spell it: the in-file link at the top of `fix-and-land.md`,
#: and the two review commands that point here rather than carrying a copy.
HAZARDS_HEADING = "The hazards"

#: The pointer shape the guarded list uses — ``the `frozen` job, *"no shipped release entry
#: was rewritten"*`` — parsed rather than listed here, so a guard added to that list in the
#: same shape is checked without anybody remembering to extend this file. Whitespace is
#: collapsed before matching because a quoted job name wraps across lines in the markdown.
_GUARD_POINTER = re.compile(r"the `(?P<job>[a-z][a-z-]*)` job, \*\"(?P<name>[^\"]+)\"\*")

#: One job's `name:` in the workflow, keyed by its id. `^  <id>:` is a top-level job; the
#: `name:` that follows before the next such line is that job's display name, which is the
#: string a reader sees in the PR's checks list and therefore the string worth quoting.
_JOB = re.compile(r"^  (?P<id>[a-z][a-z0-9-]*):\n(?P<body>(?:(?!^  \S).*\n)*)", re.MULTILINE)
_JOB_NAME = re.compile(r"^    name: (?P<name>.+)$", re.MULTILINE)


def _squashed(text: str) -> str:
    """Whitespace collapsed, so a sentence that wraps in markdown still matches one that wraps
    in Python. Quote characters go with it: `preland.py` writes its refusals as adjacent string
    literals, so the sentence a reader sees has `" "` seams in the source that no reader has."""
    return re.sub(r"[\"\s]+", " ", text)


def _hazards() -> str:
    """`fix-and-land.md` from the hazards heading to the end of the file."""
    text = command("fix-and-land")
    marker = f"\n## {HAZARDS_HEADING}\n"
    at = text.find(marker)
    assert at >= 0, (
        f"fix-and-land.md has no `## {HAZARDS_HEADING}` section. Every assertion below would "
        "then be about an empty string, and three files point at that heading by name (#367)")
    return text[at:]


def _workflow_jobs() -> dict[str, str]:
    text = _at(".github/workflows/tests.yml").read_text(encoding="utf-8")
    jobs = {m["id"]: (_JOB_NAME.search(m["body"]) or {"name": ""})["name"]
            for m in _JOB.finditer(text)}
    assert len(jobs) > 3, "no jobs were parsed out of tests.yml — the shape has changed"
    return jobs


#: #367's own grep, split back into the four things it was looking for. As one alternation it is
#: satisfied by `#260` alone — Codex's finding, and a fair one: a section that dropped three of
#: the four traps would still match. Each branch is therefore asserted separately, and the names
#: are the failure message.
_MEASURED = {
    "the --delete-branch cleanup failing from a worktree": r"delete-branch[\s\S]*worktree",
    "#260 itself, by number": r"#260",
    "the host-specific artefact, in the words the grep looked for": r"host artefact",
    "two concurrent pytest runs": r"concurrent[\s\S]{0,80}pytest|pytest[\s\S]{0,80}concurrent",
}


@pytest.mark.parametrize("what", sorted(_MEASURED))
def test_the_hazards_answer_the_measurement_that_filed_them(what: str):
    """#367 was filed on a grep that returned nothing across every command brief on this fleet,
    and the fix is only real while that grep returns this file. Asserted as the issue's own
    pattern rather than as "the words are there", because the words are what a rewrite moves and
    the measurement is what the rewrite has to keep answering."""
    assert re.search(_MEASURED[what], _hazards(), re.IGNORECASE), (
        f"the hazards section no longer covers {what}, which #367's measurement looked for. That "
        "grep returned zero when the issue was filed; a rewrite that drops one trap puts part of "
        "it back to zero without any other check noticing")


def test_every_guard_the_hazards_point_at_is_still_a_job_in_the_workflow():
    """A trap with a mechanism gets one line naming the mechanism instead of a paragraph
    restating the trap — which is only worth doing while the name resolves. A renamed job leaves
    the page telling a lander to read a check that will never appear in its PR's list, and that
    failure is silent from both ends: CI is happy, and the sentence still parses."""
    jobs = _workflow_jobs()
    pointers = list(_GUARD_POINTER.finditer(_hazards()))
    assert len(pointers) >= 3, (
        "fewer than three guard pointers were parsed out of the hazards section, so this check "
        "is green about almost nothing — #325, #351 and #365 each landed one")
    for m in pointers:
        job, quoted = m["job"], m["name"]
        assert job in jobs, (
            f"the hazards section points at a `{job}` job, and .github/workflows/tests.yml has "
            f"no such job — it has {sorted(jobs)}. A lander told to read that check will never "
            "see it report")
        assert _squashed(quoted) in _squashed(jobs[job]), (
            f"the hazards section quotes the `{job}` job as {quoted!r}, but the workflow names "
            f"it {jobs[job]!r}. The quoted string is what a reader matches against the checks "
            "list on the PR, so a stale one sends them looking for a check that is right there")


def test_the_ci_state_the_hazards_name_is_still_the_state_preland_refuses_on():
    """#324's guard is a word — `blocked` — and the value of the one-line pointer is that a
    lander reading "no checks at all" knows there is a state for it and what preland says. Both
    halves have to survive: the vocabulary in `qbdata.CI_STATES`, and the refusal sentence the
    page quotes, which is the text the lander will actually be looking at."""
    source = _at("harness/loops/preland.py").read_text(encoding="utf-8")
    preland = _squashed(source)
    hazards = _hazards()
    assert "qbdata.CI_STATES" in hazards, (
        "the hazards section stopped naming the vocabulary #324 landed, so the pointer says a "
        "guard exists without saying where to read it")
    # Codex, round 1: naming the vocabulary is not the same as the state still being in it. The
    # page's whole claim is that `blocked` is the word for a run that will never report, so the
    # refusal table keyed on `qbdata.CI_STATES` has to still have that key — a state deleted
    # there leaves the page describing a distinction the gate can no longer draw.
    refusals = re.search(r"^CI_REFUSALS = \{$(?P<body>.*?)^\}$", source, re.MULTILINE | re.DOTALL)
    assert refusals, "preland.py no longer defines CI_REFUSALS as a top-level dict literal"
    assert '"blocked":' in refusals.group("body"), (
        "preland's CI_REFUSALS has no `blocked` key, so the state #324 landed is gone and the "
        "hazards section is describing a distinction nothing draws any more")
    quoted = re.search(r"\*\"(CI will not run without a human[^\"]+)\"\*", hazards)
    assert quoted, "the hazards section no longer quotes preland's refusal for a gated run"
    assert _squashed(quoted.group(1)) in preland, (
        "the hazards section quotes a refusal sentence preland.py does not contain. The point "
        "of quoting it is that a lander can match the words in front of it against this page")


def test_the_closing_keyword_check_is_the_graphql_query_and_never_a_grep():
    """#374's whole finding is that the check everyone reaches for does not work: GitHub's
    parser ignores negation, so `close #371` inside "this does not close #371" is a closing
    reference and a keyword grep reads the sentence as a disclaimer. The runnable block has to
    be the authoritative query, because what an agent copies is the fenced text."""
    hazards = _hazards()
    # Codex, round 1: the identifier appearing anywhere in any fence proves nothing — a passing
    # mention in an unrelated block would satisfy it. What the reader copies is one block, so the
    # block that names the field has to be the `gh api graphql` call that fetches it.
    blocks = re.findall(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```", hazards, re.DOTALL | re.MULTILINE)
    queries = [b for b in blocks if "closingIssuesReferences" in b]
    assert queries, (
        "the hazards section describes the closing-keyword trap without a runnable "
        "`closingIssuesReferences` query — leaving a reader to invent the check, which is how "
        "#374 happened twice in one day")
    assert any("gh api graphql" in b and "pullRequest" in b for b in queries), (
        "the block naming `closingIssuesReferences` is not a `gh api graphql` query against a "
        "pull request, so the thing an agent copies out of this section is not the check. The "
        "whole finding of #374 is that the obvious check — a grep — gives the wrong answer")
    assert "#374" in hazards, (
        "nothing points at #374, so a reader cannot tell whether a CI guard has since landed "
        "and made this section background rather than procedure")


def test_the_host_specific_trap_is_fenced_off_from_the_permanent_ones():
    """The half of #367 that is easy to get wrong. #260 and the dcg refusals are properties of
    tools and will be true on the next box; the failing claim test is a property of THIS box's
    PATH and will read as nonsense elsewhere. A page that mixes them ages badly and then gets
    distrusted whole — so the host-specific one lives under its own heading, below everything
    permanent, and carries the date it was checked."""
    hazards = _hazards()
    artefact = "test_a_missing_qb_claim_does_not_abort_the_run_under_set_e"
    heading = re.search(r"^### (?P<title>.*this box.*)$", hazards, re.MULTILINE | re.IGNORECASE)
    assert heading, (
        "the hazards section has no heading marking which trap is a property of this machine "
        "rather than of the tools, so a reader on another box cannot tell what to ignore")
    assert re.search(r"\d{4}-\d{2}-\d{2}", heading.group("title")), (
        "the host-specific heading carries no date. It is a claim about one machine's PATH, and "
        "the date is what tells a later reader how much to trust it")
    at = hazards.find(artefact)
    assert at > heading.start(), (
        f"{artefact} is named at or above the host-specific heading. It fails here and passes "
        "in CI because this box has qb-claim on PATH — filed among the permanent traps it reads "
        "as a defect in the repo")


@pytest.mark.parametrize("name", ("review-pr", "panel-review-pr"))
def test_the_review_commands_point_at_the_hazards_rather_than_carrying_a_copy(name: str):
    """Two copies of a document is the thing this repo has re-learned twice: they agree until
    somebody edits one, and then the stale one is indistinguishable from the current one. Both
    commands merge from the same worktrees on the same box, so they need the same warnings —
    by reference, and the reference has to name the heading it lands on."""
    text = command(name)
    assert HAZARDS_HEADING in text and "fix-and-land" in text, (
        f"{name}.md does not point at fix-and-land.md's `{HAZARDS_HEADING}` section, so an agent "
        "landing through this command gets none of it")
    assert "closingIssuesReferences" not in _fenced(text), (
        f"{name}.md carries its own copy of the closing-reference query. One copy, in "
        "fix-and-land.md — a second drifts, and a drifted copy of a check reads exactly like "
        "a current one")


def test_the_in_file_pointer_at_the_hazards_resolves_to_the_heading():
    """The steps are what a lander reads on the way in; the hazards are what it needs on the way
    out, and a page whose only route to them is scrolling to the bottom is a page they are found
    on the second landing. The link at the top is that route, and a markdown anchor that does not
    match its heading fails silently — it lands the reader at the top of the file."""
    text = command("fix-and-land")
    link = re.search(r"\[[^\]]+\]\(#(?P<anchor>[a-z0-9-]+)\)", text)
    assert link, "fix-and-land.md's steps no longer link to the hazards section at all"
    slug = re.sub(r"[^a-z0-9]+", "-", HAZARDS_HEADING.lower()).strip("-")
    assert link.group("anchor") == slug, (
        f"the pointer links to #{link.group('anchor')} and the heading slugs to #{slug}, so it "
        "silently lands the reader at the top of the file instead")
