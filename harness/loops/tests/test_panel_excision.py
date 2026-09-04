"""#627 — a sub-floor fix that CAUSED a finding is excised, not repaired.

Everything else in this loop that knows a fix pass made things worse either stops
(`escalate_on.fix_injection`) or prices a revert and hands it to a human
(`round_stop.revert`). Both refusals are right and both are about a **pass**: a pass is
mixed, so reverting one that cleared three P2s to remove five P3s puts the P2s back, and
nothing in the loop can tell which half is which without asking.

A single fix that answered a finding BELOW `round_trigger_floor` is not a mixed pass. It
answered one complaint that was, by definition, not blocking the close, so the whole cost
of removing it is one P3 or P4 going back on the board unfixed — which every budgeted
cycle produces on purpose and this repo's policy already calls reportable and
non-blocking. There is nothing to weigh, so there is no decision to take upstairs, and
this is the one backtrack the loop takes on its own.

What is pinned here is the four things that keep it inside that argument:

* the GRAIN. Attribution has to be per FIX and `_provenance` is per pass, so the
  identification is `git blame` of the finding's own line at the head — and a pass that
  left no seam to blame is reported as such rather than aimed at approximately;
* the CASCADE. A sub-floor fix a later blocking fix built on is not a clean excision, and
  every way of not being sure of that declines with a sentence: the commit answered more
  than one finding, a later commit rewrote its lines, it is a merge, the checkout could
  not be read;
* the FLOOR IT IS MEASURED AGAINST is the ANCHOR round's, not this one's. An operator who
  moves `round_trigger_floor` between rounds must not thereby make a fix that answered
  mandatory work look like a cheap one that can be thrown away;
* the FACT THAT IT MOVES NO VERDICT. The caused finding is still outstanding to every
  rule in `round_stop`, so a round that names an excision goes again exactly as it would
  have without one. #627's rule is "the cycle continues", and a rung that ended a cycle on
  it would be the expensive reading of a cheap fact.

The repository is real wherever the claim is about git, on `test_panel_restored`'s
reason: the claims here are "this commit still owns the lines it wrote" and "this is the
commit that wrote that line", and a double answering `git blame` with a canned string
asserts them rather than checking them.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import panel  # noqa: E402
import panel_rounds  # noqa: E402
import panel_scope  # noqa: E402
from test_panel_provenance import _compare, _panel_round  # noqa: E402
from test_panel_reconstruct import _new_repo  # noqa: E402

#: The repo root, for the two briefs. Four levels up: tests -> loops -> harness -> repo.
REPO_ROOT = Path(__file__).resolve().parents[3]
REVIEW_PR = REPO_ROOT / "harness/commands/review-pr.md"
PANEL_REVIEW_PR = REPO_ROOT / "harness/commands/panel-review-pr.md"

#: The anchor round's brief as `Baseline.fixed_findings` carries it: one sub-floor
#: finding the fixer was sent to, and one blocking finding beside it. The pair is the
#: point — every test that matters here is about telling them apart.
CHEAP = ("cheap00000000001", "P3", "app/sync.py", 3, "the docstring is stale")
BLOCKER = ("block00000000002", "P2", "app/sync.py", 9, "the retry never backs off")
BRIEF = [CHEAP, BLOCKER]

#: The floors that brief was banded under, as the anchor round's payload records them.
WAS = {"round_trigger_floor": "P2", "fix_severity_floor": "P4",
       "low_severity_fix_lines": 40}

#: The round-local ids the anchor round's report printed beside those two findings —
#: the only spelling a fixer is actually shown.
IDS = {"77-F01": CHEAP[0], "77-F02": BLOCKER[0]}

#: A finding this round raised on a line the fix pass wrote, as
#: `round_stop.revert.removes` carries it.
CAUSED = {"key": "new0000000000003", "severity": "P3", "file": "app/sync.py",
          "line": 4, "title": "the new docstring contradicts the code"}

SHA_A, SHA_B = "a" * 40, "b" * 40


def _commit(sha=SHA_A, subject="docs: unstale the docstring", body="Answers 77-F01.",
            merge=False):
    return {"sha": sha, "subject": subject,
            "message": f"{subject}\n\n{body}\n", "merge": merge}


def _read(commits=None, why=None):
    """What :func:`panel_scope.fix_commit_seams` hands over."""
    return {"commits": list(commits if commits is not None else [_commit()]),
            "why": why}


def _state(**over):
    """An excision the round could actually name: a readable range, a brief with a
    sub-floor finding in it, one seam, and a caused finding standing on that seam's
    own line."""
    args = dict(brief=BRIEF, dials=WAS, ids=IDS, commits=_read(), caused=[CAUSED],
                blame={"app/sync.py": {3: SHA_A, 4: SHA_A, 9: "c" * 40}}.get,
                added={SHA_A: {"app/sync.py": 2}}.get)
    args.update(over)
    return panel_rounds.excision_state(panel_scope.FIX_RANGE_OK, **args)


# ------------------------------------------------- which findings were sub-floor


def test_the_sub_floor_findings_are_the_ones_below_the_ANCHORS_trigger_floor():
    """The set this whole rule is scoped to. `fix_severity_floor` is deliberately not
    a second condition: a finding below it was never in the brief, so no fix answered
    it, and naming the floor twice would only give a later dial change a way to break
    one of the two spellings."""
    got, floor, why = panel_rounds.sub_floor_brief(BRIEF, WAS)
    assert why is None and floor == "P2"
    assert set(got) == {CHEAP[0]}
    assert got[CHEAP[0]] == {"key": CHEAP[0], "severity": "P3", "file": "app/sync.py",
                             "line": 3, "title": "the docstring is stale"}


def test_a_floor_moved_between_rounds_does_not_reclassify_the_last_pass():
    """The anchor round's policy and not this round's. At `round_trigger_floor: P1`
    the P2 in that brief WAS sub-floor work, and at `P3` neither of them was — the
    fixer was briefed under one of those and spent under it, and an operator who moved
    the dial afterwards must not thereby make a fix that answered mandatory work look
    like a cheap one that can be thrown away."""
    wide, at, _ = panel_rounds.sub_floor_brief(BRIEF,
                                               {**WAS, "round_trigger_floor": "P1"})
    assert set(wide) == {CHEAP[0], BLOCKER[0]} and at == "P1"
    narrow, _at, _ = panel_rounds.sub_floor_brief(BRIEF,
                                                  {**WAS, "round_trigger_floor": "P3"})
    assert narrow == {}
    # And the floor the classification used travels in the block, because the report
    # around it is written under THIS round's dials.
    assert _state(dials={**WAS, "round_trigger_floor": "P1"})["floor"] == "P1"


def test_an_unreadable_floor_DECLINES_rather_than_admitting_everything():
    """The asymmetry `budgeted_brief` documents, arriving on the one test that decides
    whether a fix may be removed without asking. An unreadable floor read as "no floor"
    would put EVERY finding in the pass's brief below it, which is the loosening
    direction — so the payload has to name it as a severity or nothing here happens."""
    for dials in ({}, {"round_trigger_floor": "later"}, {"round_trigger_floor": 2},
                  None):
        got, floor, why = panel_rounds.sub_floor_brief(BRIEF, dials)
        assert got == {} and floor == "" and why and "`round_trigger_floor`" in why
    assert _state(dials={})["count"] is None


def test_a_SONAR_hard_gate_issue_in_the_brief_is_never_sub_floor():
    """Found by a Codex second opinion, on the ANSWERING side of a filter this already
    had on the caused side. `Baseline.fixed_findings` is read out of both of the
    fixer's brief buckets, because for every other consumer they are one list — so a
    hard-gate issue that mapped to P3 sat below the trigger floor and its fix was
    excisable, which would hand a red quality gate back to the board as
    reported-and-not-fixed. It is exempt from both floors at every rule in
    `round_stop`, and this rule's argument is that the finding it hands back was never
    owed."""
    brief = [CHEAP, ("gate00000000005", "P3", "app/sync.py", 7, "null deref")]
    got, _floor, why = panel_rounds.sub_floor_brief(brief, WAS,
                                                    gate={"gate00000000005"})
    assert why is None and set(got) == {CHEAP[0]}
    # And it travels from the baseline rather than being guessed at from the record,
    # which carries a severity and no source.
    assert "gate00000000005" not in _state(brief=brief,
                                           gate={"gate00000000005"})["excise"][0][
        "answered"]["key"]


def test_the_baseline_records_which_of_the_brief_was_a_hard_gate_issue(tmp_path):
    """The other half of the same defect: nothing downstream of `fixed_findings` can
    tell the two buckets apart, so the bucket has to be recorded where it is still
    known."""
    path = tmp_path / "r2.json"
    path.write_text(json.dumps({
        "round": 2, "cycle": "abc123", "reviewed": True, "repo": "e2e",
        "github": "acme/e2e", "pr": 77, "head_sha": "b" * 40, "dismissed": [],
        "to_fix": [{"key": CHEAP[0], "severity": "P3", "file": "app/sync.py",
                    "synthesis": "the docstring is stale"}],
        "sonar_findings": [{"key": "gate00000000005", "severity": "P3",
                            "file": "app/sync.py", "synthesis": "null deref"}],
    }))
    got = panel.load_baseline(
        [str(path)], {"repo": "e2e", "github": "acme/e2e", "pr": 77, "round": 3})
    assert got.fixed_gate == {"gate00000000005"}
    assert {k for k, *_ in got.fixed_findings} == {CHEAP[0], "gate00000000005"}
    sub, _floor, _why = panel_rounds.sub_floor_brief(got.fixed_findings,
                                                     {"round_trigger_floor": "P2"},
                                                     got.fixed_gate)
    assert set(sub) == {CHEAP[0]}


def test_an_EXCISED_finding_is_not_the_next_rounds_BRIEF(tmp_path):
    """The payload's `to_fix` bucket carries every non-dismissed finding with flags, so
    an excised one is still a row in it — and `load_baseline` read every row as a
    complaint the fixer had been sent to. It was not: the report says "hand a fixer NONE
    of the findings below", so no fix answered it and it is evidence about no location.

    The worst of the four false conclusions that follows is the last one: readmitted to
    the brief, the excised finding is sub-floor again, and the REVERT commit — which
    names it, because that is what the orchestrator was told to record — reads as a
    seam. The next round then proposes excising the excision, putting back the very fix
    this one removed. Found by a Codex second opinion.

    `fixed_severities` keeps it, and that asymmetry is deliberate: that list answers
    "was ALL of the brief budgeted", where an extra entry can only DECLINE."""
    path = tmp_path / "r2.json"
    path.write_text(json.dumps({
        "round": 2, "cycle": "abc123", "reviewed": True, "repo": "e2e",
        "github": "acme/e2e", "pr": 77, "head_sha": "b" * 40, "dismissed": [],
        "sonar_findings": [],
        "to_fix": [
            {"key": CHEAP[0], "id": "77-F01", "severity": "P3", "file": CHEAP[2],
             "synthesis": CHEAP[4], "excised": False},
            {"key": CAUSED["key"], "id": "77-F09", "severity": "P3",
             "file": CAUSED["file"], "synthesis": CAUSED["title"], "excised": True},
        ],
    }))
    got = panel.load_baseline(
        [str(path)], {"repo": "e2e", "github": "acme/e2e", "pr": 77, "round": 3})
    assert {k for k, *_ in got.fixed_findings} == {CHEAP[0]}
    assert got.fixed_here == {CHEAP[2]: {CHEAP[0]}}
    assert got.fixed_ids == {"77-F01": CHEAP[0]}
    assert CAUSED["key"] not in got.fixed_gate
    # …and counted where counting can only decline the strict premise.
    assert got.fixed_severities == ["P3", "P3"]
    # The consequence: a revert commit naming the excised finding is not a seam, so the
    # next round cannot propose excising the excision.
    sub, _floor, why = panel_rounds.sub_floor_brief(got.fixed_findings, WAS,
                                                    got.fixed_gate)
    assert why is None and set(sub) == {CHEAP[0]}
    seams, refused = panel_rounds.excision_seams(
        [_commit(sha=SHA_B, subject="revert: the docstring fix",
                 body="Excised 77-F09, which the docstring fix caused.")],
        sub, got.fixed_findings, got.fixed_ids)
    assert seams == {} and refused == []


def test_a_severity_NOTHING_CAN_READ_is_not_sub_floor():
    """`Baseline` writes `"?"` for a brief entry whose severity nothing could parse,
    and `severity_at_least` reads an unparseable severity as P1 — which is at or above
    every trigger floor. So the unreadable case declines through the predicate the rest
    of the round already uses rather than through a branch written here to agree with
    it."""
    got, _floor, why = panel_rounds.sub_floor_brief(
        [("k", "?", "a.py", 1, "who knows")], WAS)
    assert why is None and got == {}


# ------------------------------------------------- which commit answered which finding


def test_a_commit_naming_the_findings_ROUND_LOCAL_ID_is_a_seam():
    """The spelling a fixer actually has. The **To fix** list prints `[77-F01]` beside
    every finding and prints no key at all, so a rule that accepted only keys would
    find no seam on every pass that did exactly what the brief told it."""
    seams, refused = panel_rounds.excision_seams([_commit()], *_sub(), ids=IDS)
    assert refused == []
    assert list(seams) == [SHA_A]
    assert seams[SHA_A]["answered"]["key"] == CHEAP[0]
    assert seams[SHA_A]["subject"] == "docs: unstale the docstring"


def test_a_commit_naming_the_findings_KEY_is_a_seam_too():
    """The other spelling, for a fixer that has the payload open."""
    seams, _ = panel_rounds.excision_seams(
        [_commit(body=f"Answers {CHEAP[0]}.")], *_sub(), ids=IDS)
    assert list(seams) == [SHA_A]


def test_an_id_that_is_a_PREFIX_of_another_does_not_match_it():
    """The fence, and it is not pedantry: `_finding_id` is `%02d`, so a round with a
    hundred findings prints `77-F100` — and a bare `in` test would read that message as
    naming `77-F10`. An excision aimed at the wrong hunk removes work nobody asked to
    remove, which is the one failure this rule cannot afford."""
    assert panel_rounds._names_finding("Answers 77-F100.", "77-F10") is False
    assert panel_rounds._names_finding("Answers 77-F10.", "77-F10") is True
    # And a key quoted inside a longer hex string — a commit sha, which a fix pass's
    # message very often carries — is not the key.
    assert panel_rounds._names_finding(f"see {CHEAP[0]}0000", CHEAP[0]) is False
    assert panel_rounds._names_finding(f"see [{CHEAP[0]}]", CHEAP[0]) is True


def test_a_commit_that_answered_TWO_findings_is_refused_and_says_which():
    """A mixed pass in miniature. Removing it removes a fix nothing attributed a
    finding to — and where the second finding is mandatory work, removing it undoes a
    blocking fix, which is exactly the revert `round_stop.revert` refuses to execute.
    Refused with a sentence rather than quietly not matched: "the rule declined" and
    "the rule did not apply" are different news for the human reading the round."""
    both = _commit(body="Answers 77-F01 and 77-F02.")
    seams, refused = panel_rounds.excision_seams([both], *_sub(), ids=IDS)
    assert seams == {}
    assert len(refused) == 1 and refused[0]["commit"] == SHA_A
    assert "names 2 of the round's findings" in refused[0]["why"]
    assert "mandatory work above the trigger floor" in refused[0]["why"]


def test_two_SUB_FLOOR_fixes_in_one_commit_are_refused_as_well():
    """Both cheap, and still refused. #627's argument prices the excision at ONE
    sub-floor finding returning; a commit carrying two takes an innocent fix out with
    the offending one, and nothing attributed a finding to the second."""
    brief = [CHEAP, ("cheap00000000004", "P4", "app/sync.py", 20, "a stale comment")]
    sub, _floor, _why = panel_rounds.sub_floor_brief(brief, WAS)
    seams, refused = panel_rounds.excision_seams(
        [_commit(body=f"Answers {CHEAP[0]} and cheap00000000004.")], sub, brief)
    assert seams == {}
    assert "more than one sub-floor fix landed in it" in refused[0]["why"]


def test_a_MERGE_is_never_a_seam():
    """`git revert` refuses a merge without `-m`, and a merge is how other people's
    commits get inside a fix range in the first place — `revert_state`'s argument for
    withholding a range command, asked one commit at a time."""
    seams, refused = panel_rounds.excision_seams(
        [_commit(merge=True)], *_sub(), ids=IDS)
    assert seams == {}
    assert "merge" in refused[0]["why"] and "`-m`" in refused[0]["why"]


def test_a_commit_that_named_NOTHING_is_neither_a_seam_nor_a_refusal():
    """A commit that answered mandatory work, and a commit whose message names no
    finding at all, are simply not what this rule is about. Reporting them as refusals
    would bury the one line that matters under every commit of every pass."""
    seams, refused = panel_rounds.excision_seams(
        [_commit(body="Answers 77-F02."), _commit(sha=SHA_B, body="tidy up")],
        *_sub(), ids=IDS)
    assert seams == {} and refused == []


def _sub():
    """`(sub_floor, brief)` for the shared brief — the two positional arguments
    `excision_seams` takes, kept in one place so a test cannot pair one round's
    findings with another round's floors."""
    sub, _floor, why = panel_rounds.sub_floor_brief(BRIEF, WAS)
    assert why is None
    return sub, BRIEF


# ------------------------------------------------- what the round proposes to excise


def test_the_excision_names_the_commit_the_finding_and_the_command():
    """The happy path, and every column of it. One fix answered one sub-floor finding,
    this round raised a finding on a line that fix wrote, so: revert that commit, the
    P3 goes back on the board unfixed, and the P3 it caused goes away with it."""
    got = _state()
    assert got["kind"] == "ok" and got["why"] is None
    assert (got["sub_floor"], got["seams"], got["count"]) == (1, 1, 1)
    assert got["declined"] == []
    (entry,) = got["excise"]
    assert entry["commit"] == SHA_A
    assert entry["command"] == f"git revert --no-commit {SHA_A}"
    assert entry["answered"]["key"] == CHEAP[0]
    assert [f["key"] for f in entry["caused"]] == [CAUSED["key"]]


def test_the_command_carries_the_FULL_sha():
    """`revert_state`'s rule, and for its reason: a display span is read and a command
    is executed, so an abbreviation ambiguous in this repository resolves to nothing or
    to something else."""
    assert SHA_A in _state()["excise"][0]["command"]
    assert _state()["excise"][0]["command"].split()[-1] == SHA_A


def test_a_finding_the_pass_wrote_but_no_SUB_FLOOR_fix_did_is_left_alone():
    """The bound on the whole rule. A finding standing on a line the pass wrote for a
    P1 is a finding for a fixer, and this must not touch it — `_provenance` cannot tell
    the two apart, which is the entire reason the identification is per-commit."""
    got = _state(blame={"app/sync.py": {3: SHA_A, 4: SHA_B}}.get)
    assert got["count"] == 0 and got["excise"] == [] and got["declined"] == []
    # The seam is still reported, because the pass DID leave one — it just is not what
    # this round's finding is standing on.
    assert got["seams"] == 1


def test_a_LATER_commit_that_built_on_the_fix_declines_the_excision():
    """#627's one exclusion, and the reason it is a count rather than a judgement.
    Reverting a sub-floor fix whose lines a blocking fix has since built on takes part
    of that blocking fix with it — the mixed revert this rule is careful not to be — so
    the round reports it and the caused finding stays in the cycle."""
    got = _state(blame={"app/sync.py": {3: SHA_A, 4: SHA_B}}.get,
                 caused=[{**CAUSED, "line": 3}])
    assert got["count"] == 0 and got["excise"] == []
    (refused,) = got["declined"]
    assert refused["commit"] == SHA_A
    assert "1 of the 2 line(s) this fix added are still its own" in refused["why"]
    assert "take part of another fix with it" in refused["why"]
    # And the finding it caused travels with the refusal, because that is what the
    # round has to say next: this one IS a fixer's work after all.
    assert [f["key"] for f in refused["caused"]] == [CAUSED["key"]]


def test_blame_crediting_MORE_lines_than_the_diff_counted_declines_too():
    """The other side of the same test. A rename shows up here as a commit blame
    credits with a whole file its own numstat did not count, and equality is the only
    reading that means "the commit's lines and the head's are the same set"."""
    got = _state(added={SHA_A: {"app/sync.py": 1}}.get)
    assert got["count"] == 0
    assert "2 of the 1 line(s)" in got["declined"][0]["why"]


def test_a_file_that_could_not_be_BLAMED_says_so_instead_of_going_quiet():
    """#500's posture, one instrument on. A checkout that cannot answer leaves the
    round exactly as it found it and reports the gap — never half-attributed, and never
    an excision aimed at a commit nothing checked."""
    got = _state(blame=lambda file: None)
    assert got["count"] == 0
    (refused,) = got["declined"]
    assert refused["commit"] is None
    assert "could not be blamed in this checkout" in refused["why"]


def test_a_commit_whose_lines_could_not_be_COUNTED_says_so():
    """The cascade test's denominator. Without it nothing can say the fix is still
    whole, and "we did not check" must never render as "we checked and it is clean"."""
    got = _state(added=lambda sha: None)
    assert got["count"] == 0
    assert "could not be counted" in got["declined"][0]["why"]
    assert got["declined"][0]["commit"] == SHA_A


def test_a_file_of_the_commit_that_could_not_be_blamed_declines_the_excision():
    """The finding's own line blames fine and a second file the fix touched does not.
    The cascade test is over every line the commit wrote, so a file missing from it is
    a file nothing checked for a later commit having built on."""
    got = _state(added={SHA_A: {"app/sync.py": 2, "docs/sync.md": 3}}.get)
    assert got["count"] == 0
    assert "`docs/sync.md` could not be blamed" in got["declined"][0]["why"]


def test_what_the_excision_DESTROYS_is_named_even_though_it_is_not_priced():
    """#558, which is open: a revert proposal prices what comes back and never what
    goes away, and on lexray#1697 two "P3 findings return" entries were the sole
    coverage of the mechanism the PR existed to build. #627's rule is not conditioned
    on a pricing that does not exist — but a reader must not be able to take `answered`
    for the whole cost, so the lines and the guard share are on the record."""
    got = _state(added={SHA_A: {"app/sync.py": 2, "tests/test_sync.py": 9,
                                "docs/sync.md": 4}}.get,
                 blame={"app/sync.py": {3: SHA_A, 4: SHA_A},
                        "tests/test_sync.py": {n: SHA_A for n in range(1, 10)},
                        "docs/sync.md": {n: SHA_A for n in range(1, 5)}}.get)
    destroys = got["excise"][0]["destroys"]
    assert destroys["files"] == ["app/sync.py", "docs/sync.md", "tests/test_sync.py"]
    assert destroys["lines"] == 15
    # Test and documentation paths, which is where a sub-floor fix usually is: the
    # measured pass on lexray#1697 answered four of its five budgeted findings by
    # writing more test.
    assert destroys["guard_lines"] == 13


# ------------------------------------------------- the three ways there is no verdict


def test_a_REBASED_round_has_no_excision_to_name_and_says_so_in_500s_words():
    """The range that would name the offending fix is the range a rewrite removes.
    `kind` carries `_fix_range_diff`'s own verdict rather than a second vocabulary for
    "we cannot see this", and `count` is NULL — nobody looked."""
    got = panel_rounds.excision_state(panel_scope.FIX_RANGE_REWRITTEN,
                                      why="the branch was rewritten between rounds")
    assert got["kind"] == "rewritten" and got["count"] is None
    assert got["why"] == "the branch was rewritten between rounds"
    assert (got["floor"], got["sub_floor"], got["seams"]) == (None, None, None)


def test_round_one_has_no_fix_pass_and_that_is_a_NULL_and_not_a_zero():
    """`injection_state`'s rule. A `0` here would say a round looked for a sub-floor
    fix to excise and found none, and #627 asks for this count precisely because a repo
    where it fires often is being told something about its sub-floor budget — so the
    round that could not count must not report the flattering answer."""
    got = panel_rounds.excision_state(panel_rounds.REVERT_NOT_ASKED)
    assert got["count"] is None and "no fix pass between two rounds" in got["why"]


def test_a_brief_with_no_sub_floor_finding_in_it_is_a_MEASURED_zero():
    """The other side of the same distinction. Here the question was asked and
    answered: the pass had no cheap fix in it, so this rule has nothing to apply to."""
    got = _state(dials={**WAS, "round_trigger_floor": "P3"})
    assert got["count"] == 0 and got["seams"] == 0 and got["sub_floor"] == 0
    assert "no finding below its `P3` trigger floor" in got["why"]


def test_a_pass_that_left_NO_SEAM_is_reported_rather_than_guessed_at():
    """The fixer brief asks for each budgeted fix to land as its own commit naming the
    finding it answers. A pass that smeared them together cannot be taken apart, and
    the honest answer is to say so — an excision aimed at the wrong hunk removes work
    nobody asked to remove."""
    got = _state(commits=_read([_commit(body="fix everything")]))
    assert got["count"] == 0 and got["seams"] == 0 and got["sub_floor"] == 1
    assert "names one of the 1 sub-floor finding(s)" in got["why"]
    assert "cannot be removed without taking the others with it" in got["why"]


def test_a_checkout_that_could_not_list_the_pass_relays_the_readers_sentence():
    """One sentence, written where the failure was, carried up. Two vocabularies for
    "this checkout could not answer" is how a report and a payload come to disagree
    about what a round knew."""
    got = _state(commits=_read([], why="this checkout does not carry them"))
    assert got["count"] is None and got["why"] == "this checkout does not carry them"


# ------------------------------------------------- what it costs and what it moves


def test_each_reader_is_asked_at_most_once_per_file_and_per_commit():
    """The readers are `git blame` and `git show`, and the same file is wanted twice —
    once for the finding standing on it, once for the cascade test over the commit that
    wrote it. Caching it is what makes "at most once" a property of the code rather
    than of which branch happened to run."""
    blamed, counted = [], []

    def blame(file):
        blamed.append(file)
        return {3: SHA_A, 4: SHA_A}.get and {3: SHA_A, 4: SHA_A}

    def added(sha):
        counted.append(sha)
        return {"app/sync.py": 2}

    got = _state(blame=blame, added=added)
    assert got["count"] == 1
    assert blamed == ["app/sync.py"] and counted == [SHA_A]


def test_TWO_seams_in_one_pass_are_both_excised():
    """Found by a Codex second opinion on this change's first cut, and it is the shape
    a `low_severity_fix_lines` budget produces every time it pays for more than one
    fix. The line total was being written to the same name as the reader that supplies
    it, so the second seam asked an integer for its line counts and declined with "the
    lines could not be counted" over a checkout that had answered perfectly well."""
    brief = [CHEAP, ("cheap00000000004", "P4", "app/far.py", 8, "a stale comment")]
    ids = {"77-F01": CHEAP[0], "77-F04": "cheap00000000004"}
    got = panel_rounds.excision_state(
        panel_scope.FIX_RANGE_OK, brief=brief, dials=WAS, ids=ids,
        commits=_read([_commit(), _commit(sha=SHA_B, subject="docs: the comment",
                                          body="Answers 77-F04.")]),
        caused=[CAUSED, {**CAUSED, "key": "new0000000000005", "file": "app/far.py",
                         "line": 8, "title": "the comment now lies"}],
        blame={"app/sync.py": {3: SHA_A, 4: SHA_A},
               "app/far.py": {8: SHA_B}}.get,
        added={SHA_A: {"app/sync.py": 2}, SHA_B: {"app/far.py": 1}}.get)
    assert got["declined"] == []
    assert got["count"] == 2
    assert sorted(e["commit"] for e in got["excise"]) == sorted([SHA_A, SHA_B])


def test_the_seam_is_looked_for_in_the_range_the_ROUND_attributed_over():
    """Also Codex. `--since` overrides the baseline's commit for every other reader in
    the round — `_fix_range_diff` is called with `anchor`, so `introduced` is a claim
    about that span — and a seam looked for in `prior.head_sha..head_sha` instead can
    sit outside the range the attribution ran over, or leave a later commit inside it
    outside the window the cascade test checks. `restored_lines` carries the same
    correction and states it at length.

    Asserted on the call site, because the argument IS the call site: `panel.run` binds
    `anchor` before scope is decided and there is no seam in between for a double to
    stand in."""
    src = Path(panel.__file__).read_text()
    call = src.split("excision = excision_state(", 1)[1].split("\n    #", 1)[0]
    # The comments have to be able to name the commit this does NOT read, so the
    # assertion is over the code lines alone.
    code = [line.split("#", 1)[0] for line in call.split("\n")]
    assert any('fix_commit_seams(cfg.get("path") or "", anchor, head_sha)' in line
               for line in code)
    assert not any("prior.head_sha" in line for line in code)


#: Every name this repo can reach a subprocess or an arbitrary import through. Wider
#: than the four `test_nothing_here_RUNS_the_excision` used to check, because a Codex
#: second opinion pointed out that the narrow list passed over `from subprocess import
#: call`, `os.popen`, `getattr(os, "system")` and
#: `importlib.import_module("subprocess").run` — an assertion that only rejects the
#: spelling nobody was going to use is #516's defect class, a test that cannot fail.
#:
#: `compile` is deliberately absent: `re.compile` is four calls in this module and the
#: builtin is not the door this is about.
_SHELL_DOOR_NAMES = frozenset({
    "sh", "run", "Popen", "call", "check_call", "check_output", "communicate",
    "system", "popen", "getoutput", "getstatusoutput", "spawnl", "spawnv", "spawnvp",
    "execl", "execv", "execvp", "fork", "posix_spawn",
    "import_module", "__import__", "eval", "exec", "load_module",
})
#: The modules an import of which is itself the door, whatever is then called on them.
_SHELL_DOOR_MODULES = frozenset({"subprocess", "pty", "commands", "importlib"})


def _shell_doors(source: str, inside: set[str] | None = None) -> set[str]:
    """Every way ``source`` could reach a subprocess, as a set of sentences.

    Over the parsed tree and not over the text, because the excision functions have to
    be able to SAY the words: their docstrings explain which module holds the subprocess
    and why, and a substring test over prose would either fail on the explanation or be
    weakened until it proved nothing.

    ``inside`` narrows the CALL scan to functions with those names; the IMPORT scan is
    always module-wide, because a door imported anywhere is reachable from anywhere —
    which is the hole a scan restricted to four functions leaves open.

    Returned rather than asserted, so that the checker can be pointed at a source that
    DOES shell out and shown to find it. That is the whole difference between this and
    the assertion it replaces: this one is proved able to fail.
    """
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _SHELL_DOOR_MODULES:
                    found.add(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _SHELL_DOOR_MODULES:
                found.add(f"from {node.module} import ...")
            for alias in node.names:
                if root == "os" and alias.name in _SHELL_DOOR_NAMES:
                    found.add(f"from os import {alias.name}")
    for node in ast.walk(tree):
        if inside is not None and not (isinstance(node, ast.FunctionDef)
                                       and node.name in inside):
            continue
        where = getattr(node, "name", "<module>")
        for call in (c for c in ast.walk(node) if isinstance(c, ast.Call)):
            named = (call.func.attr if isinstance(call.func, ast.Attribute)
                     else getattr(call.func, "id", ""))
            if named in _SHELL_DOOR_NAMES:
                found.add(f"{where} calls {named}()")
            # A door reached by NAME rather than by attribute — `getattr(os, "system")`
            # is the spelling a static scan for `os.system` walks straight past, and
            # the self-test below caught this checker walking past it too on its first
            # cut. Only a literal is decidable; a computed name is not, and a module
            # that built one would be doing something this repo does nowhere.
            if named == "getattr":
                for arg in call.args[1:2]:
                    if (isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                            and arg.value in _SHELL_DOOR_NAMES):
                        found.add(f"{where} calls getattr(..., {arg.value!r})")
    return found


def test_the_shell_door_checker_CAN_FAIL():
    """#516: an assertion that cannot fail is worse than no assertion, because it reads
    as coverage. The guard below is only worth its line if it finds a door when there is
    one, so it is pointed at seven sources that each have exactly one — including the
    four spellings the previous version of that guard passed over."""
    violations = [
        "import subprocess\nsubprocess.run(['git'])\n",
        "from subprocess import call\ndef f():\n    call(['git'])\n",
        "import os\ndef f():\n    os.popen('git')\n",
        "from os import system\ndef f():\n    system('git')\n",
        "import os\ndef f():\n    getattr(os, 'system')('git')\n",
        "import importlib\ndef f():\n    importlib.import_module('subprocess')\n",
        "def f():\n    exec('import subprocess')\n",
    ]
    for source in violations:
        assert _shell_doors(source), source
    # …and it is not simply true of everything: a module that only calls what it was
    # handed is clean, which is the shape `excision_state` is in.
    assert _shell_doors("def f(reader, of):\n    return reader(of)\n") == set()


def test_nothing_here_RUNS_the_excision():
    """The command is a string in a payload and there is no code path from it to a
    subprocess. `panel_rounds` shells out to nothing at all — the readers are passed
    IN, which is what lets the decision live here and the git live in `panel_scope`."""
    source = Path(panel_rounds.__file__).read_text()
    wanted = {"sub_floor_brief", "excision_seams", "excision_state", "_read_once"}
    found = {node.name for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.FunctionDef) and node.name in wanted}
    assert found == wanted, found
    # MODULE-WIDE and not just these four, which is the correction: a helper one of
    # them calls could shell out without any of their own bodies naming a door.
    # `panel_core.sh` is the package's only door to a subprocess and this module never
    # reaches it.
    assert _shell_doors(source) == set()


def test_the_excision_runs_nothing_with_every_shell_DOOR_NAILED_SHUT():
    """The behavioural half, because a static guard proves a shape and not a run. Every
    door out of this process is replaced with something that raises, and the happy path
    is walked end to end: if any part of `excision_state` reached a subprocess — itself
    or through a helper the AST scan spelled differently — this would raise instead of
    returning the excision."""
    import os
    import subprocess

    def boom(*_a, **_k):
        raise AssertionError("excision_state reached a subprocess")

    doors = [(subprocess, "run"), (subprocess, "Popen"), (subprocess, "call"),
             (subprocess, "check_call"), (subprocess, "check_output"),
             (os, "system"), (os, "popen"), (os, "execv"), (os, "fork")]
    was = [(mod, name, getattr(mod, name)) for mod, name in doors
           if hasattr(mod, name)]
    try:
        for mod, name, _old in was:
            setattr(mod, name, boom)
        got = _state()
    finally:
        for mod, name, old in was:
            setattr(mod, name, old)
    assert got["count"] == 1
    assert got["excise"][0]["command"] == f"git revert --no-commit {SHA_A}"


def test_an_excision_moves_no_verdict_in_round_stop():
    """The load-bearing constraint, asserted where it can be. #627's rule is that the
    cycle CONTINUES — not an escalation, not a stop — so the round that names an
    excision has to end exactly where it would have ended without one. Excising the fix
    removes the cause; it is the NEXT round that gets to observe that it did, and a
    round which dropped the finding from its own arithmetic would be recording a repair
    it had not seen."""
    finding = panel.Canonical(id="77-F03", severity="P2", file="app/sync.py", line=4,
                              synthesis="the new docstring contradicts the code",
                              verdict="confirmed",
                              reported_by=[panel.Finding("claude", "P2", "app/sync.py",
                                                         4, "boom", "")])
    args = (3, 6, [finding.key], [finding], [])
    without = panel_rounds.round_stop(*args)
    with_one = panel_rounds.round_stop(*args, excision=_state())
    assert with_one["excision"]["count"] == 1
    for key in ("stop", "confident", "converged", "reason", "veto", "outstanding"):
        assert with_one[key] == without[key], key


def test_every_entry_in_declined_is_ONE_shape():
    """`injection_state`'s rule applied inside a list. `declined` collects four
    different refusals — a mixed commit, a merge, a cascade, a checkout that could not
    answer — and a consumer walking it must not have to test for a key's presence to
    know which it is reading."""
    both = _state(commits=_read([_commit(body="Answers 77-F01 and 77-F02.")]))
    cascaded = _state(blame={"app/sync.py": {3: SHA_A, 4: SHA_B}}.get,
                      caused=[{**CAUSED, "line": 3}])
    unblamed = _state(blame=lambda file: None)
    uncounted = _state(added=lambda sha: None)
    entries = (both["declined"] + cascaded["declined"] + unblamed["declined"]
               + uncounted["declined"])
    assert len(entries) == 4
    for entry in entries:
        assert set(entry) == {"commit", "subject", "answered", "caused", "why"}
        assert isinstance(entry["caused"], list) and entry["why"]


def test_every_round_carries_the_key_whether_or_not_anything_was_excised():
    """`revert`'s rule: an absent key and "there was nothing to excise" are different
    claims, and a consumer forced to tell them apart would be reading the payload's age
    rather than the cycle's state."""
    got = panel_rounds.round_stop(1, 2, [], [], [])["excision"]
    assert set(got) == {"kind", "why", "floor", "sub_floor", "seams", "count",
                        "excise", "declined"}
    assert got["kind"] == "not-asked" and got["count"] is None


# ------------------------------------------------- the local readers, against real git


def _pass(tmp_path):
    """A repo whose history is one round's fix pass: the commit round 2 reviewed, then
    a sub-floor fix landed as its own commit naming the finding it answers."""
    r = _new_repo(tmp_path)
    (r.path / "app").mkdir()
    anchor = r.commit("app/sync.py", "import os\n\nTAIL = 1\n", "the change under review")
    fix = r.commit("app/sync.py", 'import os\n\n"""mirror the paths."""\nTAIL = 1\n',
                   "docs: unstale the docstring\n\nAnswers 77-F01.")
    return r, anchor, fix


def test_the_reader_carries_the_whole_MESSAGE_and_not_just_the_subject(tmp_path):
    """The finding is named in the commit BODY, which a compare `--jq` does not carry —
    `fix_pass_commits` takes `.commit.message | split("\\n")[0]` and that is the subject
    line. This is why the seam reader is local git rather than a fourth field on that
    call."""
    r, anchor, fix = _pass(tmp_path)
    got = panel_scope.fix_commit_seams(str(r.path), anchor, fix)
    assert got["why"] is None and len(got["commits"]) == 1
    assert got["commits"][0]["sha"] == fix
    assert got["commits"][0]["subject"] == "docs: unstale the docstring"
    assert "Answers 77-F01." in got["commits"][0]["message"]
    assert got["commits"][0]["merge"] is False


def test_the_reader_says_which_commits_are_MERGES_one_at_a_time(tmp_path):
    """`fix_pass_commits` counts merges for a whole RANGE, because there the question
    is whether the range may be handed to `git revert`. Here it is asked of one commit,
    so a merge elsewhere in the pass must not disqualify a seam that is not one."""
    r, anchor, fix = _pass(tmp_path)
    r.git("checkout", "-q", "-b", "side", anchor)
    r.commit("app/other.py", "x = 1\n", "side work")
    r.git("checkout", "-q", "main")
    r.git("-c", "core.editor=true", "merge", "--no-ff", "-m", "merge side", "side")
    got = panel_scope.fix_commit_seams(str(r.path), anchor, r.at("HEAD"))
    assert [c["merge"] for c in got["commits"]].count(True) == 1
    assert [c["sha"] for c in got["commits"] if not c["merge"]].count(fix) == 1


def test_blame_names_the_commit_that_WROTE_each_line(tmp_path):
    """The per-fix attribution, and it is a real `git blame` because the claim is about
    what a file's history is. Line 3 is the docstring the fix wrote; the lines around it
    belong to the change under review."""
    r, anchor, fix = _pass(tmp_path)
    got = panel_scope.blame_owners(str(r.path), fix, "app/sync.py")
    assert got == {1: anchor, 2: anchor, 3: fix, 4: anchor}


def test_a_line_a_LATER_commit_rewrote_belongs_to_that_commit(tmp_path):
    """The cascade, seen through the same instrument. A later commit that reworked the
    fix's own line owns it at the head — which both moves the finding off the seam and
    fails the surviving-lines test, so the two answers cannot disagree."""
    r, anchor, fix = _pass(tmp_path)
    later = r.commit("app/sync.py",
                     'import os\n\n"""mirror the paths, once."""\nTAIL = 1\n',
                     "fix: the P2, on top of the docstring")
    owners = panel_scope.blame_owners(str(r.path), later, "app/sync.py")
    assert owners[3] == later
    # And the seam's own line count no longer survives, which is what declines it.
    wrote = panel_scope.commit_insertions(str(r.path), fix)
    assert wrote == {"app/sync.py": 1}
    assert sum(1 for owner in owners.values() if owner == fix) == 0


def test_the_insertion_count_is_the_commits_own(tmp_path):
    r, _anchor, fix = _pass(tmp_path)
    assert panel_scope.commit_insertions(str(r.path), fix) == {"app/sync.py": 1}


def test_a_checkout_that_does_not_carry_the_range_declines_with_a_sentence(tmp_path):
    """One `None` for every failure, one sentence for the reader: no git, no checkout,
    a commit this clone does not have. A seam set computed from a range only partly
    read is a set whose later commits were never checked for having built on the
    seam."""
    r, _anchor, _fix = _pass(tmp_path)
    got = panel_scope.fix_commit_seams(str(r.path), "a" * 40, "b" * 40)
    assert got["commits"] == [] and "could not list the commits" in got["why"]
    assert panel_scope.fix_commit_seams("/nonexistent/acme", "a" * 40,
                                        "b" * 40)["commits"] == []
    assert panel_scope.blame_owners(str(r.path), "a" * 40, "app/sync.py") is None
    assert panel_scope.commit_insertions(str(r.path), "not-a-sha") is None


def test_nothing_landed_between_the_rounds_is_its_own_answer(tmp_path):
    r, _anchor, fix = _pass(tmp_path)
    got = panel_scope.fix_commit_seams(str(r.path), fix, fix)
    assert got["commits"] == [] and "nothing landed between the rounds" in got["why"]


def test_a_commit_body_carrying_the_RECORD_SEPARATOR_cannot_forge_a_commit(tmp_path):
    """The seam reader parses one delimited `git log` stream, and a commit MESSAGE is
    arbitrary text a fixer pastes a report line into. A body carrying the record
    separator splits its own entry in two and the tail is parsed as a commit of its
    own — so a body ending `\x1e<the blocking fix's sha>\x1f<anything>\x1fAnswers
    77-F01` produces a fully-formed record naming a commit whose real message never
    named that finding. `excision_seams` keys seams by sha, so the excision is then
    aimed at THAT commit: `git revert --no-commit <the P2's fix>`, which is the one
    outcome this whole rule exists to prevent.

    Real git, because the claim is about what `git log --format` writes. Found by a
    Codex second opinion; the constant's own comment already claimed a message carrying
    a separator was treated as unreadable, and it was not."""
    r = _new_repo(tmp_path)
    (r.path / "app").mkdir()
    anchor = r.commit("app/sync.py", "import os\n\nTAIL = 1\n", "under review")
    blocking = r.commit("app/sync.py", "import os\n\nRETRY = 2\nTAIL = 1\n",
                        "fix: back the retry off\n\nAnswers 77-F02.")
    r.commit("app/sync.py", 'import os\n\nRETRY = 2\n"""paths."""\nTAIL = 1\n',
             f"docs: unstale the docstring\n\nnothing to see"
             f"\x1e{blocking}\x1fdeadbeef\x1fAnswers 77-F01.")
    got = panel_scope.fix_commit_seams(str(r.path), anchor, r.at("HEAD"))
    assert got["commits"] == []
    assert "carries the record separator" in got["why"]
    # And the consequence, asserted rather than inferred: nothing downstream can be
    # handed a seam that points at the blocking fix.
    seams, refused = panel_rounds.excision_seams(got["commits"], {CHEAP[0]: {}},
                                                 BRIEF, IDS)
    assert seams == {} and refused == []


def test_a_pass_past_the_commit_CEILING_declines_rather_than_reading_part_of_it(
        tmp_path, monkeypatch):
    """`_patch_ids`' rule. Reading the first N commits of a longer range would leave
    the commit that built on a seam outside the window this checked, which is precisely
    the cascade the rule exists to refuse."""
    r, anchor, _fix = _pass(tmp_path)
    for i in range(3):
        r.commit(f"app/f{i}.py", "x\n", f"more {i}")
    monkeypatch.setattr(panel_scope, "EXCISION_MAX_COMMITS", 2)
    got = panel_scope.fix_commit_seams(str(r.path), anchor, r.at("HEAD"))
    assert got["commits"] == []
    assert "past the 2 this reads" in got["why"]


# ------------------------------------------------- a whole round, end to end


#: The compare patch round 3 reads: the docstring line the fix pass added, at line 3 of
#: the new side. That is what makes the finding on it `introduced`.
FIX_PATCH = '@@ -2,0 +3,1 @@\n+"""mirror the paths."""'


def _anchor_payload(tmp_path, head_sha, to_fix):
    """Round 2's payload, hand-written so the brief, its ids and the floors it was
    banded under are exactly what the test means them to be. `_panel_round` builds
    every finding at P2, so the sub-floor band is stated on the dials instead."""
    path = tmp_path / "r2.json"
    path.write_text(json.dumps({
        "round": 2, "cycle": "abc123", "reviewed": True, "repo": "e2e",
        "github": "acme/e2e", "pr": 77, "head_sha": head_sha,
        "to_fix": to_fix, "dismissed": [], "sonar_findings": [],
        "review_panel": {"round_trigger_floor": "P1", "fix_severity_floor": "P4",
                         "low_severity_fix_lines": 40},
    }))
    return str(path)


def _e2e(monkeypatch, tmp_path, repo, anchor, head, findings, capsys):
    """Round 3 of a cycle whose round 2 sent its fixer to one sub-floor finding, over a
    real repo whose fix pass landed that fix as its own commit."""
    cfg = {"github": "acme/e2e", "path": str(repo.path),
           "_rules_baseline": ".harness-rules.sample",
           "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
           # P1, so the P2 every finding in this harness carries is BELOW the trigger
           # floor and therefore in the band this rule is about.
           "review_panel": {"round_trigger_floor": "P1"}}
    baseline = _anchor_payload(tmp_path, anchor, [
        {"id": "77-F01", "key": "cheap00000000001", "severity": "P2",
         "file": "app/sync.py", "line": 3, "synthesis": "the docstring is stale",
         "reported_by": [{"title": "the docstring is stale"}]}])
    _, payload = _panel_round(monkeypatch, tmp_path, 3, findings, head=head,
                              baseline=[baseline], cfg=cfg, max_rounds=6,
                              compare=_compare(files=(("app/sync.py", FIX_PATCH),)))
    # The report is printed rather than carried in the payload, so it is read off
    # stdout — the same place `/panel-review-pr` reads it from.
    return payload, capsys.readouterr().out


def test_a_round_names_the_excision_and_takes_the_finding_out_of_the_fix_list(
        monkeypatch, tmp_path, capsys):
    """#627 end to end. Round 2 sent its fixer to one sub-floor finding, the fixer
    landed the fix as its own commit naming it, and round 3 found a defect on the line
    that fix wrote. So the round names the commit, the command and the finding that
    comes back — and the finding it caused is NOT in the fixer's list, because there is
    no longer anything for a fixer to be briefed about."""
    repo, anchor, fix = _pass(tmp_path)
    payload, report = _e2e(
        monkeypatch, tmp_path, repo, anchor, fix,
        [("app/sync.py", 3, "the new docstring contradicts the code")], capsys)

    got = payload["round_stop"]["excision"]
    assert got["kind"] == "ok" and got["count"] == 1
    assert (got["sub_floor"], got["seams"]) == (1, 1)
    (entry,) = got["excise"]
    assert entry["commit"] == fix
    assert entry["command"] == f"git revert --no-commit {fix}"
    assert entry["answered"]["title"] == "the docstring is stale"
    assert [f["title"] for f in entry["caused"]] == [
        "the new docstring contradicts the code"]

    # The finding is flagged in the payload and out of the fixer's list in the report.
    (row,) = payload["to_fix"]
    assert row["excised"] is True and row["provenance"] == "introduced"
    assert "### Excised, not fixed (1)" in report
    assert "### To fix (0)" in report
    assert f"`{fix[:8]}`" in report and "returns to the board" in report
    assert any("EXCISED rather than repaired (#627)" in n
               for n in payload["config_notes"])
    # And #558's gap travels with it wherever it is said.
    assert any("not priced here (#558)" in n for n in payload["config_notes"])


def test_a_round_whose_seam_a_later_fix_built_on_hands_the_finding_to_a_fixer(
        monkeypatch, tmp_path, capsys):
    """The declined half, end to end. A later commit in the same pass rewrote the
    docstring line, so excising the sub-floor fix is no longer the removal of one fix —
    the round says so, and the finding stays in the **To fix** list exactly as it would
    have if this rule did not exist."""
    repo, anchor, _fix = _pass(tmp_path)
    later = repo.commit("app/sync.py",
                        'import os\n\n"""mirror the paths, once."""\nTAIL = 1\n',
                        "fix: the P2, on top of the docstring")
    payload, report = _e2e(
        monkeypatch, tmp_path, repo, anchor, later,
        [("app/sync.py", 3, "the new docstring contradicts the code")], capsys)

    got = payload["round_stop"]["excision"]
    assert got["count"] == 0 and got["excise"] == []
    # The later commit owns the line, so the finding is not on the seam at all — which
    # is the same conclusion the surviving-lines test reaches, one instrument earlier.
    assert got["seams"] == 1
    (row,) = payload["to_fix"]
    assert row["excised"] is False
    assert "### To fix (1)" in report
    assert "Excised, not fixed" not in report


def test_a_repo_with_no_local_checkout_says_so_and_excises_nothing(
        monkeypatch, tmp_path):
    """The same round with the clone taken away. Nothing gates on this — the round
    still attributes and still hands the finding to a fixer — but a reader has to be
    able to see that the cheap correction was unavailable rather than unnecessary."""
    repo, anchor, fix = _pass(tmp_path)
    cfg = {"github": "acme/e2e", "path": "/nonexistent/acme-e2e",
           "_rules_baseline": ".harness-rules.sample",
           "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
           "review_panel": {"round_trigger_floor": "P1"}}
    baseline = _anchor_payload(tmp_path, anchor, [
        {"id": "77-F01", "key": "cheap00000000001", "severity": "P2",
         "file": "app/sync.py", "line": 3, "synthesis": "the docstring is stale",
         "reported_by": [{"title": "the docstring is stale"}]}])
    _, payload = _panel_round(
        monkeypatch, tmp_path, 3,
        [("app/sync.py", 3, "the new docstring contradicts the code")], head=fix,
        baseline=[baseline], cfg=cfg, max_rounds=6,
        compare=_compare(files=(("app/sync.py", FIX_PATCH),)))

    got = payload["round_stop"]["excision"]
    assert got["count"] is None and "could not list the commits" in got["why"]
    assert payload["to_fix"][0]["excised"] is False


def test_a_SONAR_hard_gate_issue_is_never_excised(monkeypatch, tmp_path):
    """A hard-gate issue is exempt from both severity floors at every rule in
    `round_stop`, because it is an external gate's verdict rather than a judged
    opinion — and this rule's whole argument is that nothing above the floor was owed.
    That argument cannot be made about a finding no floor applies to, so an excision
    never takes one out of a fixer's list."""
    got = _state(caused=[{**CAUSED, "key": "gate000000000009"}])
    assert got["count"] == 1          # the shape works on a panel finding
    # …and `panel.py` hands it only the panel's own findings. Asserted on the source,
    # because the filter is a one-line set difference at the call site and the
    # behaviour it produces is the absence of a row.
    src = Path(panel.__file__).read_text()
    call = src.split("excision = excision_state(", 1)[1].split("\n    #", 1)[0]
    assert 'if f["key"] not in {c.key for c in sonar}' in call


# ------------------------------------------------- the two briefs


def test_the_fixers_brief_asks_for_a_seam_the_harness_can_actually_READ():
    """The instruction and the mechanism have to name the same thing. "Named in the
    commit body by the finding it answers" is what the brief said before this landed,
    and it does not say WHICH name — so a fixer quoting the title left a seam nothing
    could aim at."""
    flat = " ".join(REVIEW_PR.read_text(encoding="utf-8").split())
    assert "**Land each budgeted fix as its own commit, and name the finding in the " \
           "commit body.**" in flat
    # The id the report actually prints, and the key as the alternative.
    assert "the `[1609-F03]` form" in flat
    assert "The finding's 16-character key works too" in flat
    # One finding per commit, because a mixed commit is refused.
    assert "**One finding per commit, and no other finding's id in that body.**" in flat
    assert "read as a mixed one and is left alone" in flat
    # And where the answer lands, so a fixer knows the seam is read by something.
    assert "publishes what it found at `round_stop.excision`" in flat


def test_the_orchestrator_is_pointed_at_the_payload_rather_than_asked_to_derive_it():
    """The round now computes which fix answered which finding. A brief that still told
    the orchestrator to work it out from the commits would be asking for the guess the
    rule forbids — and would let the two answers disagree."""
    flat = " ".join(PANEL_REVIEW_PR.read_text(encoding="utf-8").split())
    assert "`round_stop.excision` is that answer" in flat
    assert "**run it**" in flat
    # Every field an orchestrator has to act on, named.
    for field in ("`count`", "`excise[]`", "`declined[]`", "`seams`", "`sub_floor`"):
        assert field in flat
    # The null is not evidence of anything, which is the one misreading that turns a
    # blind round into a clean one.
    assert "A `count: null` is not evidence that nothing was excisable" in flat
    # #558's gap, relayed rather than inherited.
    assert "What it does NOT price is what the excision destroys (#558)" in flat
    assert "the sole coverage of the mechanism the PR existed to build" in flat
    # #692's unit, answered rather than left implicit: the excision costs no budget and
    # its churn is counted by every reading in the next round, that budget included.
    assert "**The excision's churn is churn, and `low_severity_fix_lines` counts it.**" \
        in flat
    # And it does NOT tell the orchestrator to apply an exemption the harness does not
    # implement and the orchestrator cannot apply by hand (found by a Codex second
    # opinion): `fix_budget_state` prices the referee's split and the revert commit is
    # in it. What the brief asks for instead is that the cost be NAMED.
    assert "Do not charge it to `low_severity_fix_lines`" not in flat
    assert "report it as the cost of the correction" in flat


# ------------------------------------------------- and it does not trip #559


def test_the_churn_question_is_ANSWERED_in_the_docs_and_not_left_implicit():
    """#692 decided the unit is churned lines, cumulative across passes — and an
    excision touches lines, so "is the correction itself churn?" is a question the
    mechanism has to answer out loud rather than by omission. The answer is yes, all of
    it, counted by every churn reading in the next round; what #559 takes out is
    ATTRIBUTION, which is the other question. Pinned in the engine README because that
    is where a reader goes to find out what a payload key means."""
    readme = " ".join(
        (REPO_ROOT / "harness/loops/README.md").read_text(encoding="utf-8").split())
    assert "Its own restoration is CHURN, it is counted, and #559 is why that is not a " \
           "contradiction." in readme
    assert "**None of it is exempted.**" in readme
    # AND `low_severity_fix_lines` IS ONE OF THOSE READINGS. Three documents used to
    # claim an exemption from it — this README, the changelog fragment and the
    # orchestrator's brief, which told the orchestrator not to charge it — and nothing
    # anywhere implemented one: `fix_budget_state` prices `referee_state`'s split, the
    # revert commit is in that split, and its churn is priced like any other. A claim a
    # reader cannot act on is worse than silence, and this one contradicted the
    # paragraph above it. Found by a Codex second opinion.
    #
    # The changelog fragment carries the same correction and is NOT asserted here: the
    # flake's check sandbox stages `harness/` and not `changelog.d/`, so a test reading
    # it fails in the sandbox and passes everywhere else. `scripts/changelog_fragments.py`
    # is what reads that file in CI.
    assert "the excision IS charged to it" in readme
    assert "not charged to `low_severity_fix_lines`" not in readme
    assert "stated rather than closed" in readme
    brief = " ".join(PANEL_REVIEW_PR.read_text(encoding="utf-8").split())
    assert "`low_severity_fix_lines` counts it" in brief
    assert "Do not charge it to `low_severity_fix_lines`" not in brief
    # And the two questions are named apart, because conflating them is how a
    # correction comes to read as the disease.
    assert "*How much surface was disturbed*" in readme
    assert "*Who wrote this line*" in readme
    # #624's record is an assembly at the grain of the PASS; this is the one thing
    # derived at the grain of the FIX, so the join is named rather than duplicated.
    assert "Where this joins the fix-pass record (#624)." in readme


def test_the_lines_an_excision_puts_BACK_are_the_ones_559_already_filters(tmp_path):
    """The interaction the remedy has to survive. #559: restoring reviewed code counts
    as writing it, so a naive revert inflates `fix_injection` and the remedy for the
    gate reads to the gate as the disease.

    An excision puts back the content the sub-floor fix replaced, and that content sat
    at an earlier round's head BY CONSTRUCTION — the fix landed after it. So
    `restored_lines` is looking for exactly these lines, and the round trip it requires
    holds at both ends: the block was on the branch at round 1's head and is not there
    at the anchor the excision's range starts from.

    This is evidence rather than a regression test — nothing in `restored_lines`
    changed — and the honest limit is stated in the assertion below it:
    `RESTORED_RUN_MIN` is five, so an excision smaller than that is not distinguished
    from authorship and the exposure is one round's `introduced` count leaning high by
    a handful of lines."""
    block = ["def mirror(paths):", "    out = {}", "    for p in paths:",
             "        out[p] = read(p)", "    return out"]
    r = _new_repo(tmp_path)
    (r.path / "app").mkdir()
    # Round 1 read the block…
    one = r.commit("app/sync.py", "\n".join(["import os", "", *block, "", "T = 1"]) + "\n",
                   "the change under review")
    # …a sub-floor fix replaced it (the anchor of the next round's fix range)…
    two = r.commit("app/sync.py", "import os\n\nmirror = dict\n\nT = 1\n",
                   "refactor: shorten it\n\nAnswers 77-F01.")
    # …and the excision put it back.
    three = r.commit("app/sync.py",
                     "\n".join(["import os", "", *block, "", "T = 1"]) + "\n",
                     'Revert "refactor: shorten it"')
    added = {"app/sync.py": {n: line for n, line in enumerate(block, start=3)}}
    got = panel_scope.restored_lines(str(r.path), added, {1: one}, (2, two))
    assert got["lines"] == {"app/sync.py": {3, 4, 5, 6, 7}}
    assert got["count"] == 5 and got["rounds"] == [1] and got["why"] is None
    assert three  # the excision commit is what carried them; the filter is content

    # The floor, from below, and it is #559's own deliberate choice rather than a gap
    # this feature opened: a blank line and a `return None` are byte-identical to lines
    # in almost any file, so a per-line rule would exclude a large share of every fix
    # pass ever written and leave `fix_injection` unable to fire.
    assert panel_scope.RESTORED_RUN_MIN == 5
    short = {"app/sync.py": {n: line for n, line in enumerate(block[:4], start=3)}}
    assert panel_scope.restored_lines(str(r.path), short, {1: one},
                                      (2, two))["count"] == 0
