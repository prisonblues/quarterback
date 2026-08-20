"""The reconcile pass, tested against the disagreements it was written for.

`qb-reconcile` exists because nothing checked the plan against reality: on
2026-08-20 ranks 2 and 4 pointed at PRs merged ninety minutes earlier and
`plan_read` returned rank 2 as `next`. Every fixture below is that day's data,
copied verbatim from the live board and `gh` — including the deployed board's
v2.48 `/review/findings` payload, which carries no `cycles` field at all.

The two properties worth pinning, in order:

  1. **A disagreement is reported.** Merged PR under an open item, a claim whose
     session is gone, an open PR nothing accounts for.
  2. **An unmade check never reads as a clean one.** This is the whole of #244 and
     half of #255. Every condition has a third answer, `unknowns` is never folded
     into `findings`, and the exit code tells the two apart.

Run: pytest harness/tests/test_qb_reconcile.py
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))


def _load():
    """`qb-reconcile` has no `.py`, so it is loaded the way test_qb_dash.py loads
    `qb-dash-tui.py` — by path, under a name Python can import.

    Registered in `sys.modules` BEFORE it is executed, which that file does not
    have to do and this one does: `@dataclass` resolves its own module through
    `sys.modules[cls.__module__]` to check for `KW_ONLY`, so a module still being
    executed is looked up, found absent, and the decorator dies on None.
    """
    loader = importlib.machinery.SourceFileLoader("qb_reconcile", str(BIN / "qb-reconcile"))
    spec = importlib.util.spec_from_loader("qb_reconcile", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["qb_reconcile"] = module
    loader.exec_module(module)
    return module


qr = _load()


REPO = "prisonblues/quarterback"


def item(**over) -> dict:
    """A plan item shaped as `GET /plan` returns one."""
    base = {
        "item_id": "f77e1b74-9c3e-4f81-ac0d-f5ca21e3dacd",
        "repo": REPO,
        "title": "Rebase and land PR #182 — release-metadata sandbox (#163)",
        "ref": {"kind": "pr", "value": "182"},
        "rank": 2,
        "state": "open",
        "note": None,
        "claim": None,
        "idle_days": 0.0,
        "stale": False,
    }
    base.update(over)
    return base


# ---- reading an item's own shape --------------------------------------------


def test_a_ref_is_read_as_its_kind_and_its_number():
    assert qr.item_ref(item()) == ("pr", "182")
    assert qr.item_ref(item(ref={"kind": "issue", "value": "255"})) == ("issue", "255")


def test_an_item_with_no_ref_has_nothing_to_resolve():
    """Most plan items are a line of plan and nothing else. Reading a missing ref
    as an empty one would send `gh pr view ''` at every one of them."""
    for ref in (None, {}, {"kind": "pr", "value": None}, {"kind": None, "value": "5"},
                {"kind": "pr", "value": ""}):
        assert qr.item_ref(item(ref=ref)) == (None, None)


def test_a_hashed_ref_is_the_same_ref():
    """`_norm_ref` on the board strips the `#`; a client that did not would ask
    GitHub about issue `#255` and be told nothing exists."""
    assert qr.item_ref(item(ref={"kind": "issue", "value": " #255 "})) == ("issue", "255")


def test_an_item_is_labelled_by_the_handle_a_reader_would_type():
    assert qr.item_label(item()) == "rank 2 pr#182"
    assert qr.item_label(item(ref=None, rank=9)) == "rank 9"


# ---- which PR a note is talking about ---------------------------------------


def test_a_pr_is_found_in_text_only_when_it_says_PR():
    """`PR #216` is a pull request. A bare `#216` is not: on this board most
    numbers are issues, and resolving one as a PR checks the wrong thing and then
    reports the answer with a straight face."""
    assert qr.pr_in_text("Merge PR #216 — the panel found 22") == "216"
    assert qr.pr_in_text("PRs #190 and #191") == "190"
    assert qr.pr_in_text("pr#182") == "182"
    assert qr.pr_in_text("Closes #216, refs #163") is None
    assert qr.pr_in_text(None, "", "no numbers here") is None


def test_an_issue_backed_item_can_still_be_about_a_pull_request():
    """"Merge PR #216" was a real plan row whose ref was the issue. Its note's
    readiness claim is about 216, and 216 is what /review/findings can answer."""
    assert qr.item_pr(item(ref={"kind": "issue", "value": "213"},
                           title="Merge PR #216 — the panel found 22")) == "216"


def test_an_items_own_pr_ref_wins_over_anything_in_its_prose():
    assert qr.item_pr(item(note="follows PR #999")) == "182"


# ---- condition 1 and 2: the item outlived its work --------------------------


def test_a_merged_pr_under_an_open_item_is_a_done_candidate():
    """The defect this file exists for: #182 and #211, merged, still `open`, and
    rank 2 returned as `next`."""
    assert qr.ref_verdict("pr", {"state": "MERGED"})[0] == "done_candidate"


def test_a_pr_closed_without_merging_is_a_dropped_candidate():
    assert qr.ref_verdict("pr", {"state": "CLOSED"})[0] == "dropped_candidate"


def test_an_open_pr_agrees_with_an_open_item():
    assert qr.ref_verdict("pr", {"state": "OPEN"})[0] is None


def test_an_issue_closed_as_completed_is_done_and_as_not_planned_is_dropped():
    """`dropped` is not `done` — the plan's own model says collapsing them makes
    its history unreadable a fortnight later, so the reason decides which."""
    assert qr.ref_verdict("issue", {"state": "CLOSED",
                                    "state_reason": "COMPLETED"})[0] == "done_candidate"
    assert qr.ref_verdict("issue", {"state": "CLOSED",
                                    "state_reason": "NOT_PLANNED"})[0] == "dropped_candidate"


def test_a_closed_issue_with_no_stated_reason_is_still_reported():
    """GitHub leaves `stateReason` null on older issues. The item has outlived its
    work either way — that is the ONE thing an open row claims — so withholding
    the finding over which KIND of closed it was would suppress the fact that is
    certain to protect a question that is the human's to answer anyway."""
    condition, reads, readable = qr.ref_verdict("issue", {"state": "CLOSED",
                                                          "state_reason": None})
    assert condition == "done_candidate"
    assert "no reason" in reads
    assert readable is True


def test_a_state_or_kind_this_pass_cannot_read_is_not_reported_as_agreement():
    """`ref_kind` is free text in the database on purpose, and a state nobody
    recognises is not evidence that an item is fine. The third element says which
    of the two `condition is None` means — a flag rather than a prose prefix, so
    rewording a message cannot silently turn an unknown into a clean bill."""
    for kind, state in (("pr", {"state": "DRAFTED"}), ("issue", {"state": ""}),
                        ("epic", {"state": "OPEN"})):
        condition, _reads, readable = qr.ref_verdict(kind, state)
        assert condition is None
        assert readable is False, (kind, state)


def test_a_state_this_pass_understands_is_marked_readable():
    """The other side of the flag: `open` and `merged` are answers, not failures."""
    for kind, state in (("pr", {"state": "OPEN"}), ("pr", {"state": "MERGED"}),
                        ("issue", {"state": "CLOSED", "state_reason": "COMPLETED"})):
        assert qr.ref_verdict(kind, state)[2] is True, (kind, state)


# ---- condition 3: the claim does not describe the present -------------------
#
# The live board's three claims on the afternoon this was written, which happen to
# be one of each case.

HELD = {"holder": "daedalus/citrus-timber", "session": "dc539d16-d910-496f-a320-ed0b6838d9fb"}
ORPHANED = {"holder": "daedalus/quill-marble", "session": "24e8ee23-0ce6-4c8f-a265-925f1fd3ba2b"}
SESSIONLESS = {"holder": "daedalus/tallow-hazel", "session": None}

LIVE_SESSIONS = {"dc539d16-d910-496f-a320-ed0b6838d9fb",
                 "1e636395-4f12-4868-860b-012386395eb7",
                 "b7443f92-287a-4f21-930c-a92a4681b5f4"}
LIVE_HOLDERS = {"daedalus/citrus-timber", "daedalus/quill-marble", "daedalus/tallow-hazel"}


def test_a_claim_whose_session_is_live_is_held():
    assert qr.claim_verdict(HELD, LIVE_SESSIONS, LIVE_HOLDERS)[0] is False


def test_a_live_holder_whose_session_is_gone_is_still_a_stale_claim():
    """The case passive expiry cannot reach, and the reason this checks the session
    first. A `/new` resets the conversation; the seat identity and its claims are
    pinned to the pane, and the lifecycle hook renews the lease on every prompt
    whatever the new conversation is about. So the claim looks maximally fresh
    *because* the agent is busy — with something else — and it cannot lapse while
    the pane lives. Verified against the live board: holder live, session not."""
    stale, why = qr.claim_verdict(ORPHANED, LIVE_SESSIONS, LIVE_HOLDERS)
    assert stale is True
    assert "reset conversation" in why
    assert "daedalus/quill-marble is live" in why


def test_a_claim_whose_holder_and_session_are_both_gone_is_stale():
    stale, why = qr.claim_verdict(ORPHANED, set(), set())
    assert stale is True
    assert "neither" in why


def test_a_holder_with_no_live_lease_is_stale():
    stale, why = qr.claim_verdict({"holder": "daedalus/gone", "session": None},
                                  LIVE_SESSIONS, LIVE_HOLDERS)
    assert stale is True
    assert "no live lease" in why


def test_a_claim_naming_no_session_is_not_asserted_to_be_healthy():
    """A hand-taken CLI claim carries no session, so it can only be checked by
    holder name — and names are recycled when an agent finishes, so a live name is
    not proof the holder is the one that claimed it. `claim_verdict` returns "not
    stale" and the pass raises it as an unknown; the pair is what keeps this from
    reading as a clean bill. `test_a_sessionless_claim_is_reported_as_unmade`
    pins the other half."""
    stale, why = qr.claim_verdict(SESSIONLESS, LIVE_SESSIONS, LIVE_HOLDERS)
    assert stale is False
    assert "names no session" in why


# ---- condition 4: the note is fiction ---------------------------------------


@pytest.mark.parametrize("note, phrase", [
    ("free: MERGEABLE/CLEAN", "MERGEABLE"),
    ("panel says landable", "landable"),
    ("ready to land once rebased", "ready to land"),
    ("no open findings left", "no open findings"),
    ("all findings addressed", "all findings addressed"),
    ("reviewed clean at r3", "reviewed clean"),
    ("panel is green", "panel is green"),
    ("nothing left to fix", "nothing left to fix"),
])
def test_a_note_that_asserts_readiness_is_recognised(note, phrase):
    """The issue's own example is the first row: rank 1 said `free:
    MERGEABLE/CLEAN` while the board said 22 findings and `stopped: false`."""
    assert qr.note_asserts_ready(note) == phrase


@pytest.mark.parametrize("note", [
    # The three real plan notes that a bare `green` in the vocabulary matched, and
    # every one of them a false positive. RED/GREEN: confirmed to fail — each of
    # these returned a match against the first version of `_READY_PHRASES`, which
    # ended `r"panel (?:is )?(?:clean|done|green)", r"green",`.
    "this PR is what makes it green",
    "Rebase after #182 lands and it should go green on its own; if it does not, "
    "the remaining red is a real finding, not a merge problem.",
    "It is finished work and all checks were green at its last push — it is queue "
    "rot, not unfinished.",
])
def test_a_note_about_CI_is_not_a_claim_about_the_review_record(note):
    """CI is not the review record. A note about checks and a denial from
    /review/findings are claims about different things, so pairing them
    manufactures a contradiction out of two statements that never disagreed."""
    assert qr.note_asserts_ready(note) is None


@pytest.mark.parametrize("note", [
    # RED/GREEN: every one of these returned a match before the negation guard and
    # the `\b` on `mergeable` landed — a note saying the reverse of the vocabulary,
    # reported as contradicting a board that agreed with it. Found by Codex.
    "unmergeable until #182 lands",
    "not ready to land — the panel found 22",
    "no longer clean after the rebase",
    "never mergeable while CONFLICTING",
    "this is not landable yet",
    "cannot be ready to merge before its dependency",
    "doesn't have no open findings — three are still up",
])
def test_a_note_saying_the_reverse_is_not_a_readiness_claim(note):
    """The costliest false positive available, because it is wrong about the one
    condition that cannot be checked mechanically: the note and the board agree,
    and the pass reports them as contradicting each other."""
    assert qr.note_asserts_ready(note) is None


def test_a_negated_clause_does_not_hide_a_real_claim_later_in_the_note():
    """A negated match ends that match, not the search. Stopping at the first hit
    of any kind would miss the claim in the second clause — which is a false
    NEGATIVE, and the pass exists to find these."""
    assert qr.note_asserts_ready(
        "not ready to land last week, but the panel is clean now") == "panel is clean"


@pytest.mark.parametrize("note", ["anything but clean", "nothing but findings"])
def test_but_can_be_part_of_the_negation_rather_than_a_break_in_front_of_one(note):
    """The exception to `but` ending a negator's reach. Without it, making `but` a
    clause break to fix the false negative above reintroduced a false positive."""
    assert qr.note_asserts_ready(note) is None


def test_a_negator_does_not_reach_across_a_sentence():
    """`not` binds to the clause it governs. A previous sentence's negation must
    not silence the next sentence's claim."""
    assert qr.note_asserts_ready(
        "The rebase was not trivial. It is mergeable now.") == "mergeable"


@pytest.mark.parametrize("note", [
    "clean up the error handling",
    "cleanup pass over the harness",
    "clean-up of dead code",
    "cleaning the fixtures first",
])
def test_tidying_the_code_is_not_a_claim_that_the_code_is_clean(note):
    assert qr.note_asserts_ready(note) is None


def test_a_note_that_claims_nothing_is_not_forced_into_a_verdict():
    for note in (None, "", "Before #188, not after. CONFLICTING, so it needs a rebase."):
        assert qr.note_asserts_ready(note) is None


# ---- condition 4's other half: what the board says --------------------------
#
# `V248` is the deployed board's real payload shape on 2026-08-20 for PR #216:
# v2.48 predates the `cycles` field entirely, which is why its absence is a case
# rather than a hypothetical.

def history(**over) -> dict:
    base = {"repo": REPO, "pr": 216, "rounds": 1, "stopped": False,
            "stop_reason": "22 finding(s) no earlier round raised",
            "cycles": 1, "truncated": False, "findings": []}
    base.update(over)
    return base


def chain(status="open", severity="P1", outcome=None) -> dict:
    return {"key": "24290d45", "status": status, "severity": severity,
            "outcome": {"outcome": outcome} if outcome else None}


def test_findings_still_raised_deny_a_note_that_says_clean():
    denial, unknown, evidence = qr.findings_denial(history(findings=[chain(), chain()]))
    assert unknown is None
    assert "2 finding(s) still raised" in denial
    assert evidence["open_findings"] == 2


def test_a_finding_somebody_went_and_settled_does_not_deny_the_note():
    """`fixed` and `refuted` are what somebody found out by ACTING on a finding.
    A note saying "clean" is defensible when every open chain has been refuted by
    the person who looked, so those are excluded — and counted, so the report says
    how many."""
    denial, unknown, evidence = qr.findings_denial(
        history(stopped=True, findings=[chain(outcome="refuted"), chain(outcome="fixed")]))
    assert denial is None and unknown is None
    assert evidence["open_findings"] == 0
    assert evidence["open_findings_cleared_by_outcome"] == 2


def test_deferred_and_superseded_leave_the_defect_standing():
    for outcome in ("deferred", "superseded"):
        denial, _, _ = qr.findings_denial(history(findings=[chain(outcome=outcome)]))
        assert denial is not None, outcome


def test_a_finding_that_is_gone_or_dismissed_denies_nothing():
    for status in ("gone", "dismissed"):
        denial, unknown, _ = qr.findings_denial(
            history(stopped=True, findings=[chain(status=status)]))
        assert denial is None and unknown is None, status


def test_an_unstopped_cycle_denies_a_note_that_says_ready():
    denial, unknown, _ = qr.findings_denial(history(stopped=False))
    assert unknown is None
    assert "has not stopped" in denial
    assert "22 finding(s)" in denial


def test_a_board_too_old_to_report_cycles_cannot_have_its_stop_summary_attributed():
    """The deployed board on the day this was written. `stopped: false` is right
    there in the payload and reading it anyway would be the collapse the board's
    own docstring spends four paragraphs arguing against — the summary only speaks
    for a PR when it can be attributed to one cycle, and `cycles` is how you know."""
    v248 = history()
    del v248["cycles"]
    denial, unknown, evidence = qr.findings_denial(v248)
    assert denial is None
    assert "no `cycles`" in unknown
    assert evidence["cycles"] is None


def test_a_summary_spanning_two_cycles_does_not_speak_for_the_pr():
    """#44: reporting the newest loop's confident stop as this PR's ending is
    exactly what the board refuses to do, so a client must not do it either."""
    denial, unknown, _ = qr.findings_denial(history(cycles=2))
    assert denial is None
    assert "does not speak for this PR" in unknown


def test_a_truncated_window_does_not_speak_for_the_pr_either():
    denial, unknown, _ = qr.findings_denial(history(cycles=1, truncated=True))
    assert denial is None
    assert "truncated=True" in unknown


def test_a_null_stopped_is_not_read_as_false():
    """Three-state, and it must be read with `is None`: null is "no attributable
    cycle said", which is a different answer from "a round ran and said go
    again". Read for truthiness it calls a finished cycle unfinished."""
    denial, unknown, _ = qr.findings_denial(history(stopped=None))
    assert denial is None
    assert "no attributable cycle said" in unknown


def test_a_stopped_cycle_with_nothing_open_agrees_with_the_note():
    denial, unknown, _ = qr.findings_denial(history(stopped=True))
    assert denial is None and unknown is None


def test_open_findings_are_reported_even_when_the_window_is_truncated():
    """`status: "open"` means "raised in the most recent round", which is
    window-independent. A truncated history weakens the stop summary and not
    this, so the denial still stands."""
    denial, unknown, _ = qr.findings_denial(history(truncated=True, findings=[chain()]))
    assert unknown is None
    assert "1 finding(s) still raised" in denial


# ---- condition 5: work the plan does not account for ------------------------


def pr(number, closes=(), **over) -> dict:
    """`closes=None` is the case that matters: the field GitHub did not return,
    which is not the same as a PR that closes nothing."""
    base = {"number": number, "title": f"PR {number}", "repo": REPO, "isDraft": False,
            "closingIssuesReferences": None if closes is None
            else [{"number": n} for n in closes]}
    base.update(over)
    return base


def test_an_open_pr_no_item_names_is_untracked():
    found, unknowns = qr.untracked_prs([pr(247)], [item()])
    assert [p["number"] for p in found] == [247]
    assert unknowns == []


def test_a_pr_an_item_refs_directly_is_tracked():
    found, _ = qr.untracked_prs([pr(182)], [item()])
    assert found == []


def test_a_pr_is_tracked_through_the_issue_it_closes():
    """"Rebase and land PR #182 (#163)" and an item refing issue #163 are one
    workstream. Matching on the PR number alone would report the PR as untracked
    while the plan was tracking it by its issue."""
    found, _ = qr.untracked_prs([pr(182, closes=[163])],
                                [item(ref={"kind": "issue", "value": "163"})])
    assert found == []


def test_a_pr_named_in_an_items_prose_is_tracked():
    found, _ = qr.untracked_prs([pr(216)],
                                [item(ref=None, title="Merge PR #216 — the panel found 22")])
    assert found == []


def test_a_pr_whose_linked_issues_gh_did_not_return_is_an_unknown_not_a_finding():
    """The leg that decides "tracked through its issue" was unavailable, so this
    PR cannot be called untracked. Reporting it as a finding would be asserting
    the plan has a hole on the strength of a check that did not run."""
    found, unknowns = qr.untracked_prs([pr(247, closes=None)], [item()])
    assert found == []
    assert len(unknowns) == 1
    assert unknowns[0].condition == "untracked_pr"
    assert "closingIssuesReferences" in unknowns[0].reason


def test_a_pr_closing_an_issue_no_item_tracks_is_still_untracked():
    found, _ = qr.untracked_prs([pr(249, closes=[246])], [item()])
    assert [p["number"] for p in found] == [249]


OTHER = "prisonblues/lexray"


def test_two_repos_both_have_a_182():
    """RED/GREEN: confirmed to fail before tracked work was keyed by (repo,
    number) — this repo's plan item for PR #182 accounted for the OTHER repo's
    #182, and a real untracked PR vanished behind a coincidence of numbering.
    Found by Codex; the same rule `issue_key` in qbdata.py states for claims."""
    found, _ = qr.untracked_prs([pr(182, repo=OTHER)], [item()])
    assert [(p["repo"], p["number"]) for p in found] == [(OTHER, 182)]


def test_an_issue_link_is_matched_within_its_own_repo_too():
    """The second leg has the same collision: our #163 must not account for
    another repo's PR that closes ITS #163."""
    ours = item(ref={"kind": "issue", "value": "163"})
    found, _ = qr.untracked_prs([pr(900, repo=OTHER, closes=[163])], [ours])
    assert [p["number"] for p in found] == [900]
    found, _ = qr.untracked_prs([pr(900, closes=[163])], [ours])
    assert found == []


def test_a_fleet_scoped_item_tracks_no_repos_pull_requests():
    """A fleet item names no repo, so it cannot be said to track a PR in one.
    Attributing it to every repo is how the collision above gets back in."""
    found, _ = qr.untracked_prs([pr(182)], [item(repo=None)])
    assert [p["number"] for p in found] == [182]


# ---- the pass end to end ----------------------------------------------------


def full_report(**over):
    """The live board's state on 2026-08-20, reduced to the four rows that matter."""
    items = over.pop("items", [
        item(rank=2, ref={"kind": "pr", "value": "182"}),
        item(rank=3, ref={"kind": "pr", "value": "188"}, item_id="b" * 8),
        item(rank=13, ref={"kind": "issue", "value": "255"}, item_id="c" * 8,
             title="#255 — reconcile the plan against reality", claim=dict(ORPHANED)),
    ])
    kwargs = {
        "ref_states": {(REPO, "pr", "182"): {"state": "MERGED"},
                       (REPO, "pr", "188"): {"state": "OPEN"},
                       (REPO, "issue", "255"): {"state": "OPEN"}},
        "live_sessions": LIVE_SESSIONS,
        "live_holders": LIVE_HOLDERS,
        "histories": {},
        "open_prs": [pr(247), pr(188)],
        "repos": [REPO],
    }
    kwargs.update(over)
    return qr.reconcile(items, **kwargs)


def test_the_pass_reports_the_days_actual_defects():
    report = full_report()
    by_condition = {}
    for f in report.findings:
        by_condition.setdefault(f.condition, []).append(f)

    assert [f.ref for f in by_condition["done_candidate"]] == ["pr#182"]
    assert [f.rank for f in by_condition["stale_claim"]] == [13]
    assert [f.ref for f in by_condition["untracked_pr"]] == ["pr#247"]
    assert report.items_checked == 3
    assert report.exit_code == 0


def test_a_finding_carries_the_facts_it_was_drawn_from():
    """A report that states the conclusion without the pair leaves a reader to
    re-run the query to find out whether to believe it — and `idle_days: 0.0,
    stale: false` beside `github_state: MERGED` is the whole point of #255."""
    done = next(f for f in full_report().findings if f.condition == "done_candidate")
    assert done.evidence["item_state"] == "open"
    assert done.evidence["github_state"] == "MERGED"
    assert done.evidence["idle_days"] == 0.0
    assert done.evidence["stale"] is False


def test_a_ref_that_could_not_be_resolved_is_an_unknown_and_raises_the_exit_code():
    """Both spellings of a failed lookup: the key absent (never attempted) and the
    key present with None (attempted, failed). Neither is agreement."""
    for states in ({}, {(REPO, "pr", "182"): None}):
        report = full_report(items=[item()], ref_states=states, open_prs=[])
        assert report.findings == []
        assert len(report.unknowns) == 1
        assert report.unknowns[0].condition == "done_candidate"
        assert "could not be resolved" in report.unknowns[0].reason
        assert report.exit_code == 1


def test_a_fleet_scoped_item_says_it_has_no_repo_to_resolve_against():
    """`repo` is NULL for a fleet-wide item — the plan spans repos, as does the
    fleet — and such an item can still carry a ref. There is no repository to ask,
    which is a different sentence from "the lookup failed", and reporting it as a
    failure would send a reader hunting for an outage that never happened."""
    report = full_report(items=[item(repo=None)], open_prs=[])
    assert report.findings == []
    assert "names no repo" in report.unknowns[0].reason


def test_an_unreadable_state_becomes_an_unknown_rather_than_silence():
    report = full_report(items=[item()],
                         ref_states={(REPO, "pr", "182"): {"state": "DRAFTED"}},
                         open_prs=[])
    assert report.findings == []
    assert "an unrecognised state (DRAFTED)" in report.unknowns[0].reason
    assert report.exit_code == 1


def test_a_sessionless_claim_is_reported_as_unmade():
    """Asserted per condition, not over the whole report: this item's ref is the
    merged #182, so `done_candidate` fires too and is meant to."""
    report = full_report(items=[item(claim=dict(SESSIONLESS))], open_prs=[])
    assert [f for f in report.findings if f.condition == "stale_claim"] == []
    assert any(u.condition == "stale_claim" and "recycled" in u.reason
               for u in report.unknowns)


def test_a_readiness_claim_the_board_could_not_be_asked_about_is_an_unknown():
    report = full_report(items=[item(note="free: MERGEABLE/CLEAN")],
                         histories={}, open_prs=[])
    assert [f for f in report.findings if f.condition == "note_contradicted"] == []
    unknown = next(u for u in report.unknowns if u.condition == "note_contradicted")
    assert "could not be read" in unknown.reason
    assert "MERGEABLE" in unknown.reason


def test_a_note_the_board_denies_is_a_finding():
    report = full_report(
        items=[item(rank=1, ref={"kind": "pr", "value": "216"},
                    title="Merge PR #216", note="free: MERGEABLE/CLEAN")],
        ref_states={(REPO, "pr", "216"): {"state": "OPEN"}},
        histories={(REPO, "216"): history(findings=[chain() for _ in range(22)])},
        open_prs=[])
    found = next(f for f in report.findings if f.condition == "note_contradicted")
    assert "MERGEABLE" in found.summary
    assert "22 finding(s) still raised" in found.summary
    assert found.evidence["stop_reason"] == "22 finding(s) no earlier round raised"


def test_the_board_is_only_asked_about_a_pr_whose_note_makes_a_claim():
    """`run` fetches one history per item that asserts readiness. An item making
    no claim has nothing for the board to contradict, so nothing is fetched and
    nothing is reported — not even an unknown."""
    report = full_report(items=[item(note="CONFLICTING, needs a rebase")], open_prs=[])
    assert [u.condition for u in report.unknowns] == []


class FakeBoard:
    """A board that answers the three GETs `run` makes, and records them."""

    def __init__(self, plan, active=None, findings=None):
        self.plan, self.paths = plan, []
        self.active = active or {"agents": []}
        self.findings = findings or {}

    def get(self, path):
        self.paths.append(path)
        if path.startswith("/plan"):
            return self.plan
        if path.startswith("/active"):
            return self.active
        if path.startswith("/review/findings"):
            return self.findings
        raise AssertionError(f"unexpected path {path}")


@pytest.fixture
def wired(monkeypatch):
    """`run` with GitHub stubbed: it is the orchestration under test, not `gh`."""
    def wire(board, ref_states=None, open_prs=None):
        monkeypatch.setattr(qr, "board_client", lambda: (board, None))
        monkeypatch.setattr(qr, "fetch_ref_state",
                            lambda repo, kind, value: (ref_states or {}).get((kind, value)))
        monkeypatch.setattr(qr, "fetch_open_prs", lambda repo: (open_prs or [], None))
        return qr.run
    return wire


def test_a_done_item_is_never_reconciled(wired):
    """`run` filters to open items. A finished row pointing at a merged PR is the
    record working, and reporting it would bury every live disagreement under the
    plan's whole history."""
    board = FakeBoard({"items": [
        item(rank=1, state="done"),
        item(rank=2, state="dropped", ref={"kind": "pr", "value": "190"}),
    ]})
    report = wired(board, ref_states={("pr", "182"): {"state": "MERGED"},
                                      ("pr", "190"): {"state": "MERGED"}})()
    assert report.items_checked == 0
    assert report.findings == [] and report.unknowns == []


def test_run_resolves_each_distinct_ref_once(wired, monkeypatch):
    """Two open items can point at one PR only across scopes, but a repo read
    widens to the fleet-wide items — so the same ref can appear twice, and asking
    GitHub twice per tick is a network call bought for nothing."""
    asked = []
    board = FakeBoard({"items": [item(rank=2), item(rank=3, item_id="b" * 8)]})
    monkeypatch.setattr(qr, "board_client", lambda: (board, None))
    monkeypatch.setattr(qr, "fetch_open_prs", lambda repo: ([], None))

    def counting(repo, kind, value):
        asked.append((repo, kind, value))
        return {"state": "OPEN"}

    monkeypatch.setattr(qr, "fetch_ref_state", counting)
    qr.run()
    assert asked == [(REPO, "pr", "182")]


def test_run_only_asks_the_board_about_notes_that_claim_something(wired):
    """One `/review/findings` per readiness claim, and none at all otherwise. The
    orientation tax #135 measures is paid in calls nobody needed."""
    board = FakeBoard({"items": [item(rank=2, note="CONFLICTING, needs a rebase")]},
                      findings=history())
    wired(board, ref_states={("pr", "182"): {"state": "OPEN"}})()
    assert not [p for p in board.paths if p.startswith("/review/findings")]

    board = FakeBoard({"items": [item(rank=2, note="free: MERGEABLE/CLEAN")]},
                      findings=history(findings=[chain()]))
    report = wired(board, ref_states={("pr", "182"): {"state": "OPEN"}})()
    assert [p for p in board.paths if p.startswith("/review/findings")] == [
        f"/review/findings?repo={REPO}&pr=182"]
    assert [f.condition for f in report.findings] == ["note_contradicted"]


def test_a_truncated_plan_is_never_reported_as_a_whole_plan(wired):
    """RED/GREEN: confirmed to fail before `truncated` was read — the pass printed
    a report headed "every repo the board's plan names" over a list the board had
    already told it was cut short, which is precisely the error it exists to find.
    Found by Codex."""
    board = FakeBoard({"items": [item(rank=2)], "truncated": True})
    report = wired(board, ref_states={("pr", "182"): {"state": "OPEN"}})()
    assert report.findings == []
    plan_unknown = next(u for u in report.unknowns if u.subject == "the plan")
    assert f"truncated at {qr.PLAN_LIMIT}" in plan_unknown.reason
    assert report.exit_code == 1


def test_an_untruncated_plan_says_nothing_about_truncation(wired):
    board = FakeBoard({"items": [item(rank=2)], "truncated": False})
    report = wired(board, ref_states={("pr", "182"): {"state": "OPEN"}})()
    assert report.unknowns == []
    assert report.as_dict()["complete"] is True


def test_run_filters_to_one_repo_when_asked(wired):
    board = FakeBoard({"items": [
        item(rank=2, repo=REPO),
        item(rank=3, repo="prisonblues/lexray", item_id="b" * 8),
        item(rank=4, repo=None, item_id="c" * 8),          # fleet scope
    ]})
    report = wired(board, ref_states={("pr", "182"): {"state": "MERGED"}})(REPO)
    assert report.repos == [REPO]
    assert report.items_checked == 1
    assert [f.rank for f in report.findings] == [2]


def test_a_board_that_will_not_answer_is_not_a_clean_plan(wired, monkeypatch):
    """Exit 2, not an empty report. Each of the three GETs `run` makes is load-
    bearing: with no plan there is nothing to reconcile, and with no /active every
    claim would be reported stale — a page of findings manufactured by an outage."""
    class Dead:
        def __init__(self, dies_on):
            self.dies_on = dies_on

        def get(self, path):
            if path.startswith(self.dies_on):
                raise OSError("board down")
            return {"items": [], "agents": []}

    for path, expected in (("/plan", "plan could not be read"),
                           ("/active", "no claim could be checked")):
        monkeypatch.setattr(qr, "board_client", lambda p=path: (Dead(p), None))
        with pytest.raises(qr.Unavailable, match=expected):
            qr.run()

    def no_board():
        raise RuntimeError("no board configured")

    monkeypatch.setattr(qr, "board_client", no_board)
    with pytest.raises(qr.Unavailable, match="no board to reconcile against"):
        qr.run()


# ---- the two things the report must never do --------------------------------


def test_unknowns_are_never_folded_into_findings():
    """The one structural promise. A consumer that reads `findings`, sees an empty
    list and calls the plan reconciled is the bug #255 is about, so `complete`
    says so in the payload the orderer will read."""
    report = full_report(items=[item()], ref_states={}, open_prs=[])
    payload = report.as_dict()
    assert payload["findings"] == []
    assert len(payload["unknowns"]) == 1
    assert payload["complete"] is False


def test_a_clean_run_says_it_is_complete():
    report = full_report(items=[item(ref={"kind": "pr", "value": "188"})],
                         ref_states={(REPO, "pr", "188"): {"state": "OPEN"}},
                         open_prs=[])
    payload = report.as_dict()
    assert payload["findings"] == [] and payload["unknowns"] == []
    assert payload["complete"] is True
    assert report.exit_code == 0


def test_every_condition_appears_in_the_counts_even_at_zero():
    """A consumer reading `counts["dropped_candidate"]` must not get a KeyError on
    the run where nothing was dropped — the same reason `migration_reconcile`'s
    `_guards` always carries all three keys."""
    counts = full_report().as_dict()["counts"]
    assert set(counts) == set(qr.CONDITIONS)


def test_the_payload_is_json_serialisable():
    json.dumps(full_report().as_dict())


# ---- the rendered report ----------------------------------------------------


def test_the_text_report_separates_what_it_found_from_what_it_could_not_check():
    text = qr.render(full_report(items=[item(), item(rank=3, ref=None, claim=dict(SESSIONLESS))],
                                 ref_states={(REPO, "pr", "182"): None}, open_prs=[]))
    assert "COULD NOT CHECK (2)" in text
    assert "not the same as nothing to report" in text


def test_a_clean_report_does_not_claim_more_than_it_checked():
    """"Everything agrees" printed under three unmade checks is the sentence this
    whole pass exists to stop being printed."""
    partial = qr.render(full_report(items=[item()], ref_states={}, open_prs=[]))
    assert "What could not be checked is above." in partial

    clean = qr.render(full_report(items=[item(ref=None)], open_prs=[]))
    assert "agrees with GitHub and the board on everything checked." in clean
    assert "What could not be checked" not in clean


def test_every_condition_has_a_heading():
    """A finding whose condition has no heading renders under nothing at all."""
    assert set(qr._HEADINGS) == set(qr.CONDITIONS)


def test_the_board_post_summary_counts_both_halves():
    summary, detail = qr.post_summary(full_report(items=[item(), item(rank=3, ref=None,
                                                                     claim=dict(SESSIONLESS))],
                                                  open_prs=[]))
    assert "1 done candidate" in summary
    assert "could not be checked" in summary
    assert detail.startswith("qb-reconcile ")


def test_a_report_with_nothing_in_it_still_says_so():
    summary, _ = qr.post_summary(full_report(items=[item(ref=None)], open_prs=[]))
    assert "no disagreement" in summary


def test_the_post_links_the_repos_and_the_findings():
    """The board renders `refs` as links, which is what makes a posted report
    something a reader can act on rather than a paragraph to re-derive."""
    refs = qr.post_refs(full_report())
    assert {"kind": "repo", "value": "quarterback"} in refs
    assert {"kind": "pr", "value": "182", "repo": REPO} in refs
    assert {"kind": "issue", "value": "255", "repo": REPO} in refs
    assert {"kind": "pr", "value": "247", "repo": REPO} in refs


def test_the_refs_are_capped_but_the_repos_never_are():
    """A plan with forty disagreements must not turn one post into a ref dump —
    and must still be filed under the repo it is about, which is why the repos go
    on before the cap applies."""
    many = [item(rank=n, ref={"kind": "pr", "value": str(300 + n)}, item_id=f"{n:08d}")
            for n in range(40)]
    report = full_report(
        items=many,
        ref_states={(REPO, "pr", str(300 + n)): {"state": "MERGED"} for n in range(40)},
        open_prs=[])
    refs = qr.post_refs(report)
    assert len(report.findings) == 40
    assert refs[0] == {"kind": "repo", "value": "quarterback"}
    assert len(refs) == 1 + qr.MAX_POST_REFS


def test_a_finding_with_no_ref_contributes_no_link():
    """`untracked_pr` always has one; a `stale_claim` on a refless item does not,
    and a ref of `None` would be a link to nothing."""
    report = full_report(items=[item(ref=None, claim=dict(ORPHANED))], open_prs=[])
    assert [f.condition for f in report.findings] == ["stale_claim"]
    assert qr.post_refs(report) == [{"kind": "repo", "value": "quarterback"}]


# ---- the CLI ----------------------------------------------------------------


def test_a_board_that_cannot_be_reached_exits_2_rather_than_traceback():
    """Exit 2 is "could not run", which a cron log and a consuming gate can both
    read. An uncaught traceback exits 1 — the code that means "ran, some checks
    unavailable" — and would make an outage look like a partial pass."""
    def boom(_repo=None):
        raise qr.Unavailable("no board configured")

    saved, qr.run = qr.run, boom
    try:
        assert qr.main([]) == 2
    finally:
        qr.run = saved


def test_the_exit_code_tells_a_partial_run_from_a_clean_one(capsys):
    for states, expected in (({(REPO, "pr", "182"): {"state": "MERGED"}}, 0), ({}, 1)):
        saved, qr.run = qr.run, lambda _repo=None, s=states: full_report(
            items=[item()], ref_states=s, open_prs=[])
        try:
            assert qr.main([]) == expected
        finally:
            qr.run = saved
    capsys.readouterr()


def test_quiet_prints_nothing_when_there_is_nothing_to_report(capsys):
    saved, qr.run = qr.run, lambda _repo=None: full_report(items=[item(ref=None)], open_prs=[])
    try:
        assert qr.main(["--quiet"]) == 0
        assert capsys.readouterr().out == ""
    finally:
        qr.run = saved


def test_quiet_still_prints_a_disagreement(capsys):
    saved, qr.run = qr.run, lambda _repo=None: full_report(items=[item()], open_prs=[])
    try:
        qr.main(["--quiet"])
        assert "pr#182 is merged" in capsys.readouterr().out
    finally:
        qr.run = saved


def test_json_mode_emits_the_payload_and_nothing_else(capsys):
    saved, qr.run = qr.run, lambda _repo=None: full_report()
    try:
        qr.main(["--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["counts"]["done_candidate"] == 1
        assert payload["complete"] is True
    finally:
        qr.run = saved


def test_the_pass_does_not_post_unless_asked(capsys):
    """Report-only by default, like the lander. The only write this file can make
    is opt-in, and a run that was not asked must not touch the board at all."""
    posts = []
    saved_run, qr.run = qr.run, lambda _repo=None: full_report()
    saved_client, qr.board_client = qr.board_client, lambda: (
        type("C", (), {"post": lambda _s, p, b: posts.append((p, b))})(), None)
    try:
        qr.main([])
        assert posts == []
        qr.main(["--post"])
        assert [p for p, _ in posts] == ["/post"]
        assert posts[0][1]["type"] == "finding"
        assert "plan reconcile:" in posts[0][1]["summary"]
    finally:
        qr.run, qr.board_client = saved_run, saved_client
    capsys.readouterr()


def test_a_failed_post_does_not_lose_the_report(capsys):
    """Best-effort, like the panel's board recording: telemetry that can fail the
    thing it reports on is worse than none. The report already reached stdout."""
    saved_run, qr.run = qr.run, lambda _repo=None: full_report()

    def broken():
        raise RuntimeError("board down")

    saved_client, qr.board_client = qr.board_client, broken
    try:
        assert qr.main(["--post"]) == 0
        out = capsys.readouterr()
        assert "pr#182 is merged" in out.out
        assert "report stands" in out.err
    finally:
        qr.run, qr.board_client = saved_run, saved_client


def test_gh_missing_is_reported_as_could_not_run():
    """Every GitHub-side check would be an unknown, which is not a report worth
    printing — so it is exit 2, not a page of "could not check"."""
    def no_gh(*_a, **_k):
        raise FileNotFoundError("gh")

    saved, qr.subprocess.run = qr.subprocess.run, no_gh
    try:
        with pytest.raises(qr.Unavailable, match="not on PATH"):
            qr._gh_json(["pr", "view", "1"])
    finally:
        qr.subprocess.run = saved


def test_a_gh_call_that_fails_for_any_other_reason_is_an_unmade_check():
    """A missing `gh` is fatal because nothing can be checked; one failing call is
    not, because the rest of the pass still means something. It returns None,
    which every caller turns into an unknown."""
    class Failed:
        returncode, stdout, stderr = 1, "", "no such PR"

    saved, qr.subprocess.run = qr.subprocess.run, lambda *_a, **_k: Failed()
    try:
        assert qr._gh_json(["pr", "view", "9999"]) is None
        assert qr.fetch_ref_state(REPO, "pr", "9999") is None
    finally:
        qr.subprocess.run = saved


def test_a_ref_kind_gh_has_no_command_for_is_never_shelled_out():
    """`ref_kind` is free text in the database. `gh epic view` is not a command,
    and building the call to find that out would be a subprocess per row."""
    called = []
    saved, qr.subprocess.run = qr.subprocess.run, lambda *a, **k: called.append(a)
    try:
        assert qr.fetch_ref_state(REPO, "epic", "1") is None
        assert called == []
    finally:
        qr.subprocess.run = saved


def test_an_open_pr_list_that_hit_the_fetch_limit_says_so():
    """A silent cap here reads as "the plan accounts for everything". The rows are
    still returned — a partial answer beats none — with the truncation reported
    beside them."""
    rows = [{"number": n} for n in range(qr.PR_FETCH_LIMIT)]
    saved, qr._gh_json = qr._gh_json, lambda _a: rows
    try:
        got, problem = qr.fetch_open_prs(REPO)
        assert len(got) == qr.PR_FETCH_LIMIT
        assert problem is not None
        assert "fetch limit" in problem.reason
    finally:
        qr._gh_json = saved


def test_a_pr_list_that_failed_is_reported_as_a_whole_repo_unchecked():
    saved, qr._gh_json = qr._gh_json, lambda _a: None
    try:
        got, problem = qr.fetch_open_prs(REPO)
        assert got == []
        assert "not compared against the plan at all" in problem.reason
    finally:
        qr._gh_json = saved
