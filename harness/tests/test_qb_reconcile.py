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
from datetime import timedelta
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


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Two pieces of process state this file must not share or leak.

    `client_once` caches the board client (so the reads and the `--post` write are
    one secret-fetch rather than two), and `--post`'s change detection is a digest
    file under `$XDG_STATE_HOME`. Left alone, a test would inherit the previous
    test's client and the previous RUN's digest — which would make
    `test_the_pass_does_not_post_unless_asked` pass once and then silently stop
    posting — and would write into the developer's real state directory.
    """
    monkeypatch.setattr(qr, "_CLIENT", None)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


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


def test_every_pr_an_item_names_is_collected_not_just_the_first():
    """RED/GREEN: `tracked_prs` was built from `pr_in_text`, which is first-wins, so
    an item titled "Land PR #190 and PR #191" left #191 reported as work the plan
    does not account for — on a board post, every tick, forever. First-wins stays
    right for `item_pr` ("which PR is this note claiming about") and is wrong for
    "which PRs does this item account for"."""
    assert qr.prs_in_text("Land PR #190 and PR #191") == ["190", "191"]
    assert qr.prs_in_text("PR #190 landed", "then PR #191, then PR #190") == ["190", "191"]
    # Still `PR #n` and nothing looser: on this board most bare numbers are issues,
    # and resolving one as a PR would check the wrong thing.
    assert qr.prs_in_text("PRs #190 and #191") == ["190"]
    assert qr.prs_in_text(None, "", "Closes #216") == []
    assert qr.pr_in_text("Land PR #190 and PR #191") == "190"


def test_an_item_naming_two_prs_tracks_both_of_them():
    found, _, _ = qr.untracked_prs(
        [pr(190), pr(191)],
        [item(ref=None, title="Land PR #190 and PR #191")])
    assert found == []


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
    assert qr.claim_verdict(HELD, LIVE_SESSIONS, LIVE_HOLDERS)[0] == qr.CLAIM_HELD


def test_a_live_holder_whose_session_is_gone_is_still_a_stale_claim():
    """The case passive expiry cannot reach, and the reason this checks the session
    first. A `/new` resets the conversation; the seat identity and its claims are
    pinned to the pane, and the lifecycle hook renews the lease on every prompt
    whatever the new conversation is about. So the claim looks maximally fresh
    *because* the agent is busy — with something else — and it cannot lapse while
    the pane lives. Verified against the live board: holder live, session not."""
    verdict, why = qr.claim_verdict(ORPHANED, LIVE_SESSIONS, LIVE_HOLDERS)
    assert verdict == qr.CLAIM_STALE
    assert "reset conversation" in why
    assert "daedalus/quill-marble is live" in why


def test_a_claim_nothing_in_active_names_and_that_names_no_expiry_is_an_unknown():
    """RED/GREEN: this returned CLAIM_STALE with the words "the claim is past its own
    expiry" over a claim that never said when it expires.

    `_claim_live` is three-valued on purpose — True, False, and `None` for "the board
    did not say" — and its only caller tested it with a bare `if`, so Python's
    truthiness put `None` and `False` down the same branch. The wording is what makes
    it a defect rather than a lenience: a comparison the pass could not make was
    reported in a sentence asserting it had been made and come out unfavourably,
    which is the absence-vs-inability collapse this whole file exists to report on,
    inverted into a finding whose justification is invented. `ORPHANED` carries no
    `expires`, so there is nothing to be past."""
    verdict, why = qr.claim_verdict(ORPHANED, set(), set())
    assert verdict == qr.CLAIM_UNKNOWN
    assert "no readable `expires`" in why
    assert "past its own expiry" not in why


def test_a_holder_with_no_live_lease_and_an_expired_claim_is_stale():
    verdict, why = qr.claim_verdict(
        {"holder": "daedalus/gone", "session": None,
         "expires": (qr._utcnow() - timedelta(minutes=1)).isoformat()},
        LIVE_SESSIONS, LIVE_HOLDERS)
    assert verdict == qr.CLAIM_STALE
    assert "past its own expiry" in why


def test_a_live_claim_whose_agent_has_simply_gone_quiet_is_an_unknown():
    """RED/GREEN: this returned `stale` before the third verdict existed, and the
    finding it produced accused a working agent of holding a dead claim — re-posted
    every fifteen minutes.

    The two TTLs are not the same length. `/active` lists only leases with
    `expires_at > now`, a lease is renewed per PROMPT and runs 1800s on this board
    (300s by API default), and a plan claim runs 3600s. So an agent in a single long
    autonomous turn — the normal shape of the loops this harness drives — drops out
    of `/active` for up to half an hour with its claim perfectly live, and nothing
    in the payload tells "quiet" from "gone". The claim's own `expires` is the fact
    that CAN be read, and while it holds the board's own passive expiry is what
    settles the question."""
    live = {"holder": "daedalus/long-turn", "session": "a" * 36,
            "expires": (qr._utcnow() + timedelta(minutes=20)).isoformat()}
    verdict, why = qr.claim_verdict(live, set(), set())
    assert verdict == qr.CLAIM_UNKNOWN
    assert "nothing in /active" in why
    assert "30 minutes against a claim's hour" in why


def test_a_naive_or_unreadable_expiry_does_not_take_the_tick_down():
    """The board sends `expires` with an offset; a client that ever sees one without
    must not compare a naive datetime with an aware one and die inside a cron tick.
    An unparseable one is no evidence the claim is live, so it falls through."""
    live = {"holder": "daedalus/long-turn", "session": "a" * 36,
            "expires": (qr._utcnow() + timedelta(minutes=20)).replace(
                tzinfo=None).isoformat()}
    assert qr.claim_verdict(live, set(), set())[0] == qr.CLAIM_UNKNOWN
    # RED/GREEN: an expiry that will not parse used to come back STALE, i.e. as a
    # finding, saying the claim was past a date the pass had just failed to read.
    verdict, why = qr.claim_verdict(
        {"holder": "x", "session": "y", "expires": "not a date"}, set(), set())
    assert verdict == qr.CLAIM_UNKNOWN
    assert "no readable `expires`" in why


def test_a_claim_naming_no_session_is_not_asserted_to_be_healthy():
    """A hand-taken CLI claim carries no session, so it can only be checked by
    holder name — and names are recycled when an agent finishes, so a live name is
    not proof the holder is the one that claimed it. `claim_verdict` returns "not
    stale" and the pass raises it as an unknown; the pair is what keeps this from
    reading as a clean bill. `test_a_sessionless_claim_is_reported_as_unmade`
    pins the other half."""
    verdict, why = qr.claim_verdict(SESSIONLESS, LIVE_SESSIONS, LIVE_HOLDERS)
    assert verdict == qr.CLAIM_UNKNOWN
    assert "names no session" in why
    assert "recycled" in why


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
    # RED/GREEN: `clean` and `landable` shipped in the same tuple as the fixed
    # `\bmergeable\b` with no boundary of their own, so these three matched at an
    # offset with `before == "un"` and no negator fired — the note saying the
    # reverse of the vocabulary, reported as contradicting a board that agreed.
    "unclean after the rebase",
    "still unlandable",
    "the tree is unclean",
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


@pytest.mark.parametrize("note", [
    # RED/GREEN: confirmed red before `_PREFIX_NEGATOR`. The `\b` fix that closed
    # "unmergeable" does not close this, and the reason is that it is the SAME
    # boundary doing the work: a hyphen is a non-word character, so in
    # "non-mergeable" the leading `\b` of `\bmergeable\b` is satisfied by the hyphen
    # and the phrase matches at offset 4. `_NEGATORS` holds no `non`/`un`, so
    # `_negated("non-")` was False and the note read as a readiness claim — on the
    # exact three words the boundary fix was written for.
    "non-mergeable until #182 lands",
    "un-clean after the rebase",
    "still non-landable",
    "the tree is un-clean",
])
def test_a_hyphenated_negating_prefix_is_not_a_readiness_claim(note):
    """A hyphen is a word boundary, which is why `\b` alone cannot see this one."""
    assert qr.note_asserts_ready(note) is None


@pytest.mark.parametrize("note,phrase", [
    # The other side of the same guard, and why it is anchored to the end of the
    # preceding text rather than dropped into `_NEGATORS`: that set is searched
    # across the phrase's whole clause, so a bare `non` in it would silence every
    # one of these. "non-blocking", "non-trivial" and "non-fatal" are ordinary words
    # in these notes and none of them reverses a claim made six words later.
    ("non-blocking review comments only; mergeable now", "mergeable"),
    ("a non-trivial rebase, and the panel is clean", "panel is clean"),
    ("non-fatal warnings remain but it is ready to land", "ready to land"),
])
def test_a_hyphenated_word_that_negates_nothing_does_not_silence_the_claim(note, phrase):
    """A false NEGATIVE is the worse failure here: a disagreement this pass does not
    report is one nobody else is looking for."""
    assert qr.note_asserts_ready(note) == phrase


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
    "cleaned up the error handling",       # RED/GREEN: matched before the trailing \b
    "reviewed cleanup pass over the harness",
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


def test_a_denial_resting_on_findings_nobody_ruled_on_says_so():
    """`GET /review/findings` sets `status: "dismissed"` only when EVERY verdict in
    the chain is `dismissed`, so a chain whose latest observation is `unjudged` (the
    master judge crashed) or `sonar` (the hard gate's own issues, which no judge
    rules on) comes back `open`. Counting those as "still raised" is defensible;
    reporting the denial without saying how many of them nobody confirmed is not —
    the endpoint's own scoring excludes them from both sides of its measurement for
    exactly this reason."""
    unruled = chain()
    unruled["observations"] = [{"verdict": "unjudged"}]
    sonar = chain()
    sonar["observations"] = [{"verdict": "sonar"}]
    judged = chain()
    judged["observations"] = [{"verdict": "confirmed"}]
    denial, unknown, evidence = qr.findings_denial(
        history(findings=[unruled, sonar, judged]))
    assert unknown is None
    assert "3 finding(s) still raised" in denial
    assert "2 of them never adjudicated" in denial
    assert evidence["open_findings_unadjudicated"] == 2


def test_a_fully_adjudicated_denial_says_nothing_about_adjudication():
    judged = chain()
    judged["observations"] = [{"verdict": "confirmed"}]
    denial, _, evidence = qr.findings_denial(history(findings=[judged]))
    assert "never adjudicated" not in denial
    assert evidence["open_findings_unadjudicated"] == 0


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


def test_a_pr_a_note_merely_mentions_is_not_thereby_accounted_for():
    """RED/GREEN: confirmed red before `untracked_prs` stopped reading notes.

    Notes reference a PR in passing constantly — "follows PR #999", "rebase after PR
    #182 lands", "blocked until PR #247 lands" — and none of those says the item is
    the work on that PR. Reading them as ownership marked the PR tracked forever, so
    condition 5 went permanently silent about work nothing on the plan is doing: a
    false negative on the one condition whose entire job is finding unaccounted-for
    work. An item's ref and title say what it IS; its note is prose about it."""
    found, _, _ = qr.untracked_prs(
        [pr(247)],
        [item(ref={"kind": "issue", "value": "209"}, title="Ship the seat model",
              note="blocked until PR #247 lands")])
    assert [p["number"] for p in found] == [247]


def test_a_pr_the_title_names_is_still_accounted_for():
    """The other half: a title naming a PR is the item saying what it is landing,
    and dropping the note must not cost that."""
    found, _, _ = qr.untracked_prs(
        [pr(190), pr(191)],
        [item(ref=None, title="Land PR #190 and PR #191", note="follows PR #999")])
    assert found == []


def test_an_issue_backed_item_whose_pr_closes_it_still_accounts_for_that_pr():
    """Why dropping the note costs nothing real: a PR genuinely owned by an
    issue-backed item is reached through the issue leg, which is what it is for."""
    found, _, _ = qr.untracked_prs(
        [pr(268, closes=[213])],
        [item(ref={"kind": "issue", "value": "213"}, title="the reconcile pass",
              note="PR #268 is up")])
    assert found == []


def test_an_open_pr_no_item_names_is_untracked():
    found, unknowns, skipped = qr.untracked_prs([pr(247)], [item()])
    assert [p["number"] for p in found] == [247]
    assert unknowns == [] and skipped == []


def test_a_pr_an_item_refs_directly_is_tracked():
    found, _, _ = qr.untracked_prs([pr(182)], [item()])
    assert found == []


def test_a_pr_is_tracked_through_the_issue_it_closes():
    """"Rebase and land PR #182 (#163)" and an item refing issue #163 are one
    workstream. Matching on the PR number alone would report the PR as untracked
    while the plan was tracking it by its issue."""
    found, _, _ = qr.untracked_prs([pr(182, closes=[163])],
                                [item(ref={"kind": "issue", "value": "163"})])
    assert found == []


def test_a_pr_named_in_an_items_prose_is_tracked():
    found, _, _ = qr.untracked_prs([pr(216)],
                                [item(ref=None, title="Merge PR #216 — the panel found 22")])
    assert found == []


def test_a_pr_whose_linked_issues_gh_did_not_return_is_an_unknown_not_a_finding():
    """The leg that decides "tracked through its issue" was unavailable, so this
    PR cannot be called untracked. Reporting it as a finding would be asserting
    the plan has a hole on the strength of a check that did not run."""
    found, unknowns, _ = qr.untracked_prs([pr(247, closes=None)], [item()])
    assert found == []
    assert len(unknowns) == 1
    assert unknowns[0].condition == "untracked_pr"
    assert "closingIssuesReferences" in unknowns[0].reason


def test_a_pr_closing_an_issue_no_item_tracks_is_still_untracked():
    found, _, _ = qr.untracked_prs([pr(249, closes=[246])], [item()])
    assert [p["number"] for p in found] == [249]


def test_a_dependabot_pr_is_not_untracked_work():
    """RED/GREEN: every dependabot PR was a standing `untracked_pr`. The harness
    ships a whole loop whose job is those PRs (`harness/loops/lander.py`) and they
    are deliberately never on the plan, so on any repo with dependabot enabled the
    report is dominated by work that is already owned — permanently, in a post made
    every fifteen minutes."""
    bot = pr(300, author={"login": "dependabot[bot]", "is_bot": True})
    found, _, skipped = qr.untracked_prs([bot], [item()])
    assert found == []
    assert [s["number"] for s in skipped] == [300]
    assert "dependabot[bot]" in skipped[0]["reason"]


def test_any_bot_author_is_skipped_even_without_the_is_bot_flag():
    """`gh` reports `author.is_bot`, which catches renovate and the rest without a
    list; the list is the fallback for a `gh` that does not say."""
    assert qr.is_bot_pr(pr(1, author={"login": "renovate[bot]", "is_bot": True}))
    assert qr.is_bot_pr(pr(1, author={"login": "dependabot[bot]"}))
    assert not qr.is_bot_pr(pr(1, author={"login": "prisonblues", "is_bot": False}))
    assert not qr.is_bot_pr(pr(1))          # `author` absent is not a bot


def test_a_draft_is_opt_in_rather_than_a_standing_finding():
    draft = pr(301, isDraft=True)
    found, _, skipped = qr.untracked_prs([draft], [item()])
    assert found == [] and [s["number"] for s in skipped] == [301]
    assert "--include-drafts" in skipped[0]["reason"]

    found, _, skipped = qr.untracked_prs([draft], [item()], include_drafts=True)
    assert [p["number"] for p in found] == [301] and skipped == []


def test_a_tracked_bot_pr_is_not_even_reported_as_skipped():
    """The tracking check comes first: an item that names the PR has accounted for
    it, and saying "not compared" about a PR the plan tracks would be wrong."""
    _, _, skipped = qr.untracked_prs(
        [pr(300, author={"login": "dependabot[bot]", "is_bot": True})],
        [item(ref={"kind": "pr", "value": "300"})])
    assert skipped == []


def test_what_was_not_compared_is_said_out_loud_in_the_report_and_the_payload():
    """No silent filters, the same rule the fetch cap follows: "no untracked PRs" and
    "no untracked PRs among the ones I compared" are different sentences."""
    report = full_report(
        items=[item(ref=None)],
        open_prs=[pr(300, author={"login": "dependabot[bot]", "is_bot": True}),
                  pr(301, isDraft=True)])
    assert report.findings == []
    assert len(report.prs_skipped) == 2
    assert report.as_dict()["prs_skipped"] == report.prs_skipped
    text = qr.render(report)
    assert "2 open PR(s) not compared" in text
    assert "dependabot[bot]" in text
    # And it is neither a finding nor an unknown: nothing went unchecked.
    assert report.unknowns == [] and report.exit_code == 0


OTHER = "prisonblues/lexray"


def test_two_repos_both_have_a_182():
    """RED/GREEN: confirmed to fail before tracked work was keyed by (repo,
    number) — this repo's plan item for PR #182 accounted for the OTHER repo's
    #182, and a real untracked PR vanished behind a coincidence of numbering.
    Found by Codex; the same rule `issue_key` in qbdata.py states for claims."""
    found, _, _ = qr.untracked_prs([pr(182, repo=OTHER)], [item()])
    assert [(p["repo"], p["number"]) for p in found] == [(OTHER, 182)]


def test_an_issue_link_is_matched_within_its_own_repo_too():
    """The second leg has the same collision: our #163 must not account for
    another repo's PR that closes ITS #163."""
    ours = item(ref={"kind": "issue", "value": "163"})
    found, _, _ = qr.untracked_prs([pr(900, repo=OTHER, closes=[163])], [ours])
    assert [p["number"] for p in found] == [900]
    found, _, _ = qr.untracked_prs([pr(900, closes=[163])], [ours])
    assert found == []


def test_a_fleet_scoped_item_tracks_no_repos_pull_requests():
    """A fleet item names no repo, so it cannot be said to track a PR in one.
    Attributing it to every repo is how the collision above gets back in."""
    found, _, _ = qr.untracked_prs([pr(182)], [item(repo=None)])
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
        assert report.unknowns[0].condition == "ref_unresolved"
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
        # `client_once` caches, so a test wiring a SECOND board would otherwise keep
        # reading the first one's plan — silently, since every stub still answers.
        monkeypatch.setattr(qr, "_CLIENT", None)
        monkeypatch.setattr(qr, "board_client", lambda: (board, None))
        monkeypatch.setattr(qr, "fetch_ref_state",
                            lambda repo, kind, value: ((ref_states or {}).get((kind, value)),
                                                       None))
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
        return {"state": "OPEN"}, None

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
        "/review/findings?repo=prisonblues%2Fquarterback&pr=182"]
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
    assert plan_unknown.condition == "plan_incomplete"
    assert report.exit_code == 1


def test_a_truncation_that_cut_only_finished_rows_says_which(wired):
    """The plan is fetched with `include_done=true`, so what the cap cut is usually
    the tail of history rather than any open item — the board orders open work first
    and publishes `counts.open` for the whole scope. Claiming "open items past the
    cap were not reconciled" when every open item was read would be overstating this
    pass's own blindness, which is the same error in the other direction."""
    board = FakeBoard({"items": [item(rank=2), item(rank=1, state="done",
                                                    item_id="b" * 8)],
                       "truncated": True, "counts": {"open": 1}})
    report = wired(board, ref_states={("pr", "182"): {"state": "OPEN"}})()
    plan_unknown = next(u for u in report.unknowns if u.subject == "the plan")
    assert "every open item was reconciled" in plan_unknown.reason
    assert "never asked for its open PRs" in plan_unknown.reason

    # And when an open row WAS lost, it says the stronger thing.
    board = FakeBoard({"items": [item(rank=2)], "truncated": True,
                       "counts": {"open": 40}})
    report = wired(board, ref_states={("pr", "182"): {"state": "OPEN"}})()
    plan_unknown = next(u for u in report.unknowns if u.subject == "the plan")
    assert "open items past the cap were not reconciled" in plan_unknown.reason


def test_an_untruncated_plan_says_nothing_about_truncation(wired):
    board = FakeBoard({"items": [item(rank=2)], "truncated": False})
    report = wired(board, ref_states={("pr", "182"): {"state": "OPEN"}})()
    assert report.unknowns == []
    assert report.as_dict()["complete"] is True


def test_the_repo_scope_comes_from_the_whole_plan_not_only_its_open_rows(monkeypatch):
    """RED/GREEN: `repos` was derived from the items left after the open filter, so a
    repo whose plan rows are all done or dropped fell out of the scope entirely —
    `fetch_open_prs` was never called for it and its open PRs could not be reported
    as untracked, in silence, under a report headed "every repo the board's plan
    names". That is the most likely place for untracked work to be."""
    asked = []
    board = FakeBoard({"items": [
        item(rank=1, repo=REPO),
        item(rank=2, repo=OTHER, state="done", item_id="b" * 8),
    ]})

    def listing(repo):
        asked.append(repo)
        return [], None

    monkeypatch.setattr(qr, "board_client", lambda: (board, None))
    monkeypatch.setattr(qr, "fetch_ref_state", lambda r, k, v: ({"state": "OPEN"}, None))
    monkeypatch.setattr(qr, "fetch_open_prs", listing)
    report = qr.run()
    assert report.repos == sorted([REPO, OTHER])
    assert sorted(asked) == sorted([REPO, OTHER])
    assert report.items_checked == 1


def test_a_repo_on_the_plan_only_for_finished_rows_is_not_all_untracked(monkeypatch):
    """RED/GREEN: confirmed red before `tracking_items`. The repo scope was widened to
    every row the plan has so a repo whose work is all finished still gets its open
    PRs read — but what could ACCOUNT for a PR stayed the open rows only, so nothing
    in such a repo could account for anything and every one of its open PRs became an
    `untracked_pr` finding, on every tick, forever. Three done rows from last month
    and a dozen live PRs is a dozen standing findings: the drowning-in-noise the bot
    and draft filters exist to prevent, let back in through the widened scope. A plan
    row that named a PR still names it after it is marked done."""
    board = FakeBoard({"items": [
        item(rank=1, repo=REPO),
        item(rank=2, repo=OTHER, state="done", item_id="b" * 8,
             ref={"kind": "pr", "value": "300"}),
        item(rank=3, repo=OTHER, state="dropped", item_id="c" * 8,
             ref=None, title="Land PR #301"),
    ]})
    monkeypatch.setattr(qr, "board_client", lambda: (board, None))
    monkeypatch.setattr(qr, "fetch_ref_state", lambda r, k, v: ({"state": "OPEN"}, None))

    def listing(repo):
        return ([pr(300, repo=OTHER), pr(301, repo=OTHER)] if repo == OTHER else []), None

    monkeypatch.setattr(qr, "fetch_open_prs", listing)
    report = qr.run()
    assert [f.ref for f in report.findings if f.condition == "untracked_pr"] == []


def test_an_open_pr_no_row_of_any_state_names_is_still_untracked(monkeypatch):
    """The widened accounting must not become a mute button: a PR nothing on the
    plan names in any state is exactly what condition 5 is for."""
    board = FakeBoard({"items": [
        item(rank=2, repo=OTHER, state="done", item_id="b" * 8,
             ref={"kind": "pr", "value": "300"})]})
    monkeypatch.setattr(qr, "board_client", lambda: (board, None))
    monkeypatch.setattr(qr, "fetch_ref_state", lambda r, k, v: ({"state": "OPEN"}, None))
    monkeypatch.setattr(qr, "fetch_open_prs",
                        lambda repo: ([pr(300, repo=OTHER), pr(999, repo=OTHER)], None))
    report = qr.run()
    assert [f.ref for f in report.findings if f.condition == "untracked_pr"] == ["pr#999"]


def test_the_plan_is_asked_for_its_finished_rows_too(wired):
    """The repo scope above needs them, and `GET /plan` returns open rows only unless
    asked. The board orders open work ahead of history, so the cap can only ever cut
    into the tail of finished rows."""
    board = FakeBoard({"items": [item(rank=2)]})
    wired(board, ref_states={("pr", "182"): {"state": "OPEN"}})()
    plan_paths = [p for p in board.paths if p.startswith("/plan")]
    assert plan_paths == [f"/plan?include_done=true&limit={qr.PLAN_LIMIT}"]


def test_a_plan_payload_with_no_items_key_is_not_an_empty_plan(wired):
    """RED/GREEN: `plan.get("items") or []` read a body with no `items` at all — a
    proxy page, a 204 from a board mid-deploy, anything that is not the plan — as a
    plan with nothing on it, and printed "The plan agrees with GitHub and the board
    on everything checked" with exit 0. A clean bill from a check that never ran is
    the one thing this pass exists to stop."""
    for payload in ({}, {"truncated": False}, {"items": None}):
        board = FakeBoard(payload)
        if "items" in payload:
            report = wired(board)()          # `items: null` IS an answer: nothing.
            assert report.items_checked == 0
            continue
        with pytest.raises(qr.Unavailable, match="no `items` key"):
            wired(board)()


def test_report_scoped_unknowns_do_not_borrow_a_findings_condition():
    """`--json` is the deterministic input #232's orderer reads and `render` prints
    the condition verbatim, so a consumer grouping unknowns by condition was told the
    plan truncation was a `done_candidate` problem while the text underneath said it
    invalidated `untracked_pr`. `CONDITIONS` is the closed vocabulary for FINDINGS."""
    report = full_report(items=[item(repo=None)], open_prs=[])
    assert [u.condition for u in report.unknowns] == ["ref_unresolved"]
    assert "ref_unresolved" not in qr.CONDITIONS
    for condition in qr.UNKNOWN_CONDITIONS:
        assert condition not in qr.CONDITIONS


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
        monkeypatch.setattr(qr, "_CLIENT", None)
        monkeypatch.setattr(qr, "board_client", lambda p=path: (Dead(p), None))
        with pytest.raises(qr.Unavailable, match=expected):
            qr.run()

    def no_board():
        raise RuntimeError("no board configured")

    monkeypatch.setattr(qr, "_CLIENT", None)
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
    def boom(_repo=None, **_k):
        raise qr.Unavailable("no board configured")

    saved, qr.run = qr.run, boom
    try:
        assert qr.main([]) == 2
    finally:
        qr.run = saved


def test_the_exit_code_tells_a_partial_run_from_a_clean_one(capsys):
    for states, expected in (({(REPO, "pr", "182"): {"state": "MERGED"}}, 0), ({}, 1)):
        saved, qr.run = qr.run, lambda _repo=None, s=states, **_k: full_report(
            items=[item()], ref_states=s, open_prs=[])
        try:
            assert qr.main([]) == expected
        finally:
            qr.run = saved
    capsys.readouterr()


def test_quiet_prints_nothing_when_there_is_nothing_to_report(capsys):
    saved, qr.run = qr.run, lambda _repo=None, **_k: full_report(items=[item(ref=None)], open_prs=[])
    try:
        assert qr.main(["--quiet"]) == 0
        assert capsys.readouterr().out == ""
    finally:
        qr.run = saved


def test_quiet_still_prints_a_disagreement(capsys):
    saved, qr.run = qr.run, lambda _repo=None, **_k: full_report(items=[item()], open_prs=[])
    try:
        qr.main(["--quiet"])
        assert "pr#182 is merged" in capsys.readouterr().out
    finally:
        qr.run = saved


def test_json_mode_emits_the_payload_and_nothing_else(capsys):
    saved, qr.run = qr.run, lambda _repo=None, **_k: full_report()
    try:
        qr.main(["--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["counts"]["done_candidate"] == 1
        assert payload["complete"] is True
    finally:
        qr.run = saved


def test_the_unchanged_post_notice_never_lands_in_the_json_payload(capsys):
    """`--json` is the deterministic input #232's orderer reads, so stdout under it
    has to stay parseable — a line of prose appended after the payload is not JSON
    any more, and the notice goes to stderr for exactly that reason."""
    posts = []
    saved_run, qr.run = qr.run, lambda _repo=None, **_k: full_report(
        items=[item()], open_prs=[])
    saved_client, qr.board_client = qr.board_client, lambda: (
        type("C", (), {"post": lambda _s, p, b: posts.append((p, b))})(), None)
    try:
        qr.main(["--json", "--post"])
        capsys.readouterr()
        qr.main(["--json", "--post"])
        out = capsys.readouterr()
        assert json.loads(out.out)["counts"]["done_candidate"] == 1
        assert "unchanged since the last post" in out.err
        assert len(posts) == 1
    finally:
        qr.run, qr.board_client = saved_run, saved_client


def test_the_pass_does_not_post_unless_asked(capsys):
    """Report-only by default, like the lander. The only write this file can make
    is opt-in, and a run that was not asked must not touch the board at all."""
    posts = []
    saved_run, qr.run = qr.run, lambda _repo=None, **_k: full_report()
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
    saved_run, qr.run = qr.run, lambda _repo=None, **_k: full_report()

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


def test_gh_missing_is_reported_as_could_not_run(monkeypatch):
    """Every GitHub-side check would be an unknown, which is not a report worth
    printing — so it is exit 2, not a page of "could not check".

    `monkeypatch`, not assignment: `qr.subprocess` IS the stdlib module object, so
    `qr.subprocess.run = ...` replaces `subprocess.run` for the whole interpreter
    — anything else that shells out inside that window gets the stub, and a
    `finally` that never runs because an assertion raised first leaves it that way.
    """
    def no_gh(*_a, **_k):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(qr.subprocess, "run", no_gh)
    with pytest.raises(qr.Unavailable, match="not on PATH"):
        qr._gh_json(["pr", "view", "1"])


def test_a_gh_call_that_fails_carries_the_reason_it_failed(monkeypatch):
    """A missing `gh` is fatal because nothing can be checked; one failing call is
    not, because the rest of the pass still means something. It returns None —
    which every caller turns into an unknown — *with GitHub's own words attached*.

    RED/GREEN: `_gh_json` captured stderr and threw it away, so "no such pull
    request" (a plan item pointing at a ref that does not exist, a real and
    permanent disagreement) and "API rate limit exceeded" (this tick's weather)
    produced the identical unknown, and the pass's own thesis is that the reason a
    check could not be made is part of the report."""
    class Failed:
        returncode, stdout, stderr = 1, "", "GraphQL: Could not resolve to a PullRequest"

    monkeypatch.setattr(qr.subprocess, "run", lambda *_a, **_k: Failed())
    got, why = qr._gh_json(["pr", "view", "9999"])
    assert got is None
    assert "Could not resolve to a PullRequest" in why
    state, why = qr.fetch_ref_state(REPO, "pr", "9999")
    assert state is None
    assert "Could not resolve to a PullRequest" in why


def test_a_ref_kind_gh_has_no_command_for_is_never_shelled_out(monkeypatch):
    """`ref_kind` is free text in the database. `gh epic view` is not a command,
    and building the call to find that out would be a subprocess per row."""
    called = []
    monkeypatch.setattr(qr.subprocess, "run", lambda *a, **k: called.append(a))
    assert qr.fetch_ref_state(REPO, "epic", "1")[0] is None
    assert called == []


@pytest.mark.parametrize("kind, repo, value, expected", [
    # A ref `gh` would read as a FLAG rather than as a value. `--repo=someone/else`
    # silently answers the question about a different repository, and the verdict
    # would then be reported as a checked fact — the one thing this pass promises
    # never to do. `ref_value` is 64 characters of free board text, normalised only
    # by stripping a leading `#`.
    ("pr", REPO, "--repo=someone/else", "is not a number"),
    ("pr", REPO, "--help", "is not a number"),
    ("issue", REPO, "182 --repo other/repo", "is not a number"),
    # And the repo half, which is 256 characters of free text on the board.
    ("pr", "--json=x", "182", "not a repository name"),
    ("pr", "owner/name&admin=1", "182", "not a repository name"),
])
def test_free_board_text_never_reaches_gh_as_a_flag(monkeypatch, kind, repo, value,
                                                    expected):
    called = []
    monkeypatch.setattr(qr.subprocess, "run", lambda *a, **k: called.append(a))
    state, why = qr.fetch_ref_state(repo, kind, value)
    assert state is None
    assert expected in why
    assert called == [], "no subprocess should have been built at all"


def test_a_repo_gh_cannot_be_asked_about_is_a_whole_repo_unchecked(monkeypatch):
    """The same guard on the other call, and it must not be silent: a repo whose PRs
    were never listed is a repo whose untracked work cannot be reported."""
    called = []
    monkeypatch.setattr(qr.subprocess, "run", lambda *a, **k: called.append(a))
    got, problem = qr.fetch_open_prs("--limit=1")
    assert got == [] and called == []
    assert "not compared against the plan at all" in problem.reason


def test_the_boards_own_query_string_is_encoded_rather_than_concatenated():
    """`&` and `=` in free board text would otherwise inject extra query parameters
    into the board's path — the same class as the `gh` flag above, arriving at the
    other of the two sources this pass compares."""
    asked = []

    class Recording:
        def get(self, path):
            asked.append(path)
            return {}

    qr.fetch_history(Recording(), "owner/name&pr=1", "182")
    assert asked == ["/review/findings?repo=owner%2Fname%26pr%3D1&pr=182"]


def test_an_open_pr_list_that_hit_the_fetch_limit_says_so():
    """A silent cap here reads as "the plan accounts for everything". The rows are
    still returned — a partial answer beats none — with the truncation reported
    beside them."""
    rows = [{"number": n} for n in range(qr.PR_FETCH_LIMIT)]
    saved, qr._gh_json = qr._gh_json, lambda _a: (rows, None)
    try:
        got, problem = qr.fetch_open_prs(REPO)
        assert len(got) == qr.PR_FETCH_LIMIT
        assert problem is not None
        assert "fetch limit" in problem.reason
    finally:
        qr._gh_json = saved


def test_a_pr_list_that_failed_is_reported_as_a_whole_repo_unchecked():
    saved, qr._gh_json = qr._gh_json, lambda _a: (None, "no such repository")
    try:
        got, problem = qr.fetch_open_prs(REPO)
        assert got == []
        assert "not compared against the plan at all" in problem.reason
    finally:
        qr._gh_json = saved


# ---- what `--post` will and will not say twice ------------------------------


def test_post_says_nothing_at_all_on_a_clean_complete_report(capsys):
    """The other side of `test_the_pass_does_not_post_unless_asked`, and the branch
    that is one `not` away from inverting: `--post` on a report with no findings and
    no unknowns is the difference between a quiet board and a `finding` post every
    fifteen minutes saying "no disagreement"."""
    posts = []
    saved_run, qr.run = qr.run, lambda _repo=None, **_k: full_report(
        items=[item(ref=None)], open_prs=[])
    saved_client, qr.board_client = qr.board_client, lambda: (
        type("C", (), {"post": lambda _s, p, b: posts.append((p, b))})(), None)
    try:
        report = qr.run()
        assert report.findings == [] and report.unknowns == []
        assert qr.main(["--post"]) == 0
        assert posts == []
    finally:
        qr.run, qr.board_client = saved_run, saved_client
    capsys.readouterr()


def test_an_unchanged_report_is_not_posted_a_second_time(capsys):
    """RED/GREEN: both ticks posted before the digest existed. `--post` on a
    15-minute timer with no change detection is ~96 identical `finding` posts a day,
    each carrying the whole rendered report in `detail` — and `finding` is not in
    the board's MUTED_TYPES, so every one of them lands in every agent's orient
    read, which is the volume problem that list exists to solve."""
    posts = []
    saved_run, qr.run = qr.run, lambda _repo=None, **_k: full_report(
        items=[item()], open_prs=[])
    saved_client, qr.board_client = qr.board_client, lambda: (
        type("C", (), {"post": lambda _s, p, b: posts.append((p, b))})(), None)
    try:
        qr.main(["--post"])
        assert len(posts) == 1
        qr.main(["--post"])
        assert len(posts) == 1, "the same report must not be posted twice"
        assert "unchanged since the last post" in capsys.readouterr().err
    finally:
        qr.run, qr.board_client = saved_run, saved_client


def test_an_unchanged_report_is_posted_again_once_it_has_aged_out(capsys):
    """RED/GREEN: confirmed red before `suppressed`/`REPOST_AFTER`. Change detection
    alone is "post once, then never again while the disagreement persists", which is
    not the same as "do not spam". `GET /board` orients over a 30-minute window by
    default, so 31 minutes after the single post the still-live disagreement is
    invisible to every subsequent cold orient — exactly the reader `--post` exists
    for, per the timer unit's own comment that a report reaching only
    ~/reconcile-logs is invisible to the agents whose plan it is about. The trade
    went from ~96 posts a day to eventual zero visibility; it should have been a
    re-post interval, which needs a timestamp beside the hash and not just the hash.
    """
    posts = []
    saved_run, qr.run = qr.run, lambda _repo=None, **_k: full_report(
        items=[item()], open_prs=[])
    saved_client, qr.board_client = qr.board_client, lambda: (
        type("C", (), {"post": lambda _s, p, b: posts.append((p, b))})(), None)
    try:
        qr.main(["--post"])
        assert len(posts) == 1
        # Age the recorded post past the interval, leaving the digest itself alone:
        # the report has not changed, only the time since anyone was told about it.
        digest, when = qr.read_digest()
        qr.write_digest(digest, now=when - qr.REPOST_AFTER - timedelta(minutes=1))
        qr.main(["--post"])
        assert len(posts) == 2, "a standing disagreement must not go permanently quiet"
    finally:
        qr.run, qr.board_client = saved_run, saved_client
    capsys.readouterr()


def test_the_repost_interval_needs_both_halves():
    """`suppressed` is unchanged AND recent, and each half alone is a different bug:
    without the first a changed report is swallowed, without the second a standing
    one is. An unreadable timestamp counts as long ago, because the failure that
    costs is a live disagreement nobody can see."""
    now = qr._utcnow()
    fresh = now - qr.REPOST_AFTER + timedelta(minutes=1)
    aged = now - qr.REPOST_AFTER - timedelta(minutes=1)
    assert qr.suppressed("beef", "beef", fresh, now) is True
    assert qr.suppressed("beef", "beef", aged, now) is False
    assert qr.suppressed("beef", "cafe", fresh, now) is False
    assert qr.suppressed("beef", "beef", None, now) is False


def test_a_state_file_written_before_the_timestamp_existed_posts_once(monkeypatch,
                                                                     tmp_path):
    """The upgrade path: a file holding only a hash has no time to compare, which
    reads as "old enough" rather than "posted just now"."""
    path = tmp_path / "last-post"
    path.write_text("beef\n", encoding="utf-8")
    monkeypatch.setattr(qr, "state_path", lambda: str(path))
    assert qr.read_digest() == ("beef", None)
    assert qr.suppressed("beef", "beef", None) is False


def test_a_report_that_changed_is_posted_again(capsys):
    """The guard must not be a mute button: a NEW disagreement is exactly what the
    timer is for."""
    posts = []
    reports = [full_report(items=[item()], open_prs=[]),
               full_report(items=[item(), item(rank=3, ref={"kind": "pr", "value": "190"},
                                               item_id="b" * 8)],
                           ref_states={(REPO, "pr", "182"): {"state": "MERGED"},
                                       (REPO, "pr", "190"): {"state": "MERGED"}},
                           open_prs=[])]
    saved_run, qr.run = qr.run, lambda _repo=None, **_k: reports.pop(0)
    saved_client, qr.board_client = qr.board_client, lambda: (
        type("C", (), {"post": lambda _s, p, b: posts.append((p, b))})(), None)
    try:
        qr.main(["--post"])
        qr.main(["--post"])
        assert len(posts) == 2
    finally:
        qr.run, qr.board_client = saved_run, saved_client
    capsys.readouterr()


def test_the_digest_ignores_the_facts_that_move_on_their_own(capsys):
    """`idle_days` and GitHub's `updatedAt` change between ticks on a disagreement
    that has not changed at all. Hashing `as_dict()` would make every report new and
    buy nothing, so the digest is over what the report SAYS."""
    a = full_report(items=[item(idle_days=0.0)], open_prs=[])
    b = full_report(items=[item(idle_days=3.7, stale=True)], open_prs=[])
    assert a.findings[0].evidence["idle_days"] != b.findings[0].evidence["idle_days"]
    assert qr.post_digest(a) == qr.post_digest(b)


def test_an_unreadable_digest_posts_rather_than_silencing_the_report(monkeypatch):
    """Fail open. Silencing a disagreement because a cache could not be read is the
    wrong way round, and this pass exists to stop exactly that trade."""
    monkeypatch.setattr(qr, "state_path", lambda: "/proc/self/nonexistent/dir/last-post")
    assert qr.read_digest() == (None, None)
    assert qr.suppressed("beef", None, None) is False
    assert qr.write_digest("beef") is not None      # and says why, rather than raising


def test_a_tick_whose_only_content_is_skipped_prs_still_prints_and_posts(capsys):
    """RED/GREEN: confirmed red before `has_content`. Both gates keyed off findings
    and unknowns only, and `prs_skipped` is neither — so a tick with thirty skipped
    dependabot PRs and nothing else printed nothing under `--quiet` and posted
    nothing under `--post`. The shipped systemd unit runs exactly `qb-reconcile
    --post --quiet`, so in the deployed configuration the whole feature was invisible
    whenever it was the only thing to say — against a README and a CHANGELOG that
    both promise "neither is dropped silently"."""
    posts = []
    report = full_report(items=[item(ref={"kind": "pr", "value": "188"})],
                         ref_states={(REPO, "pr", "188"): {"state": "OPEN"}},
                         open_prs=[pr(400, author={"login": "dependabot[bot]", "is_bot": True})])
    assert report.findings == [] and report.unknowns == []
    assert len(report.prs_skipped) == 1

    saved_run, qr.run = qr.run, lambda _repo=None, **_k: report
    saved_client, qr.board_client = qr.board_client, lambda: (
        type("C", (), {"post": lambda _s, p, b: posts.append((p, b))})(), None)
    try:
        assert qr.main(["--post", "--quiet"]) == 0
        out = capsys.readouterr().out
        assert "not compared" in out
        assert len(posts) == 1
    finally:
        qr.run, qr.board_client = saved_run, saved_client


def test_a_wholly_empty_tick_is_still_silent_under_quiet(capsys):
    """The other side of the same gate: widening it to `prs_skipped` must not make
    `--quiet` print on a tick with genuinely nothing to say."""
    posts = []
    report = full_report(items=[item(ref={"kind": "pr", "value": "188"})],
                         ref_states={(REPO, "pr", "188"): {"state": "OPEN"}},
                         open_prs=[])
    assert not (report.findings or report.unknowns or report.prs_skipped)

    saved_run, qr.run = qr.run, lambda _repo=None, **_k: report
    saved_client, qr.board_client = qr.board_client, lambda: (
        type("C", (), {"post": lambda _s, p, b: posts.append((p, b))})(), None)
    try:
        assert qr.main(["--post", "--quiet"]) == 0
        assert capsys.readouterr().out == ""
        assert posts == []
    finally:
        qr.run, qr.board_client = saved_run, saved_client


def test_the_board_client_is_built_once_for_the_reads_and_the_write(capsys):
    """`resolve_config` sources the site config in a subshell and can run
    `QUARTERBACK_TOKEN_CMD`, so a second call is a second secret-fetch subprocess
    per tick — and a token command that succeeded for the reads can fail for the
    write, producing "report stands, but the board post failed" against a board that
    answered a second ago."""
    built = []
    board = FakeBoard({"items": [item(rank=2)]})
    board.posts = []
    board.post = lambda path, body: board.posts.append((path, body))

    def once():
        built.append(1)
        return board, None

    saved_client, qr.board_client = qr.board_client, once
    saved_ref, qr.fetch_ref_state = qr.fetch_ref_state, lambda r, k, v: (
        {"state": "MERGED"}, None)
    saved_prs, qr.fetch_open_prs = qr.fetch_open_prs, lambda repo: ([], None)
    try:
        assert qr.main(["--post"]) == 0
        assert [p for p, _ in board.posts] == ["/post"]
        assert built == [1], "one client for three GETs and the POST"
    finally:
        qr.board_client, qr.fetch_ref_state, qr.fetch_open_prs = (
            saved_client, saved_ref, saved_prs)
    capsys.readouterr()
