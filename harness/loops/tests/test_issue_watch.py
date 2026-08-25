"""Tests for issue_watch.py — the watcher that mostly declines.

The load-bearing ones are at the top and they are all about what does NOT happen.
A watcher whose only tested path is "it found something actionable" would pass
just as well against one that acts on a stranger's issue on a machine nobody
opted in, so the defaults are asserted against the shape of the accident: a
tempting, well-specified, correctly-labelled issue by the repo owner, handed to a
judge that raises if it is ever called, on a repo carrying the config this
repository actually ships.
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import appetite  # noqa: E402
import issue_watch as iw  # noqa: E402
import needs_human  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLE = REPO_ROOT / ".harness-rules.sample"


def issue(number=1, *, title="Thing is broken", body="", author="prisonblues",
          labels=(), comments=()):
    return {"number": number, "title": title, "body": body,
            "author": {"login": author},
            "labels": [{"name": n} for n in labels],
            "comments": [{"author": {"login": w}, "body": t} for w, t in comments],
            "url": f"https://github.com/acme/r/issues/{number}"}


def open_cfg(**pickup):
    """A cfg whose pickup gate is wide open except for what the test narrows.

    Same shape as test_appetite's, and verbose for the same reason: which of the
    five sequential refusals a case has got past is the interesting part of it.
    """
    base = {"enabled": True, "only_labels": ["p0"],
            "allowed_authors": ["prisonblues"], "require_human_triage": False,
            "skip_when_unlabelled": False}
    base.update(pickup)
    return {"github": "acme/r", "issue_pickup": base}


def raises(*_a, **_k):
    raise AssertionError("this must not have been reached")


def doable(_work, _model):
    return True, "a clear defect with a repro", "sonnet", ""


#: The issue most likely to get itself implemented by accident: the owner wrote
#: it, it carries the qualifying label, it says how to reproduce the fault and
#: how to know it is fixed, and nothing about it is open to interpretation.
TEMPTING = issue(
    7, title="qb-stage writes the stage file with > and is refused under $HOME",
    labels=("p0", "bug"), author="prisonblues",
    body="## Steps to reproduce\n\nRun `qb-stage F0`.\n\n## Expected\n\n"
         "The marker is written.\n\n## Actual\n\nThe guard refuses it.\n\n"
         "## Acceptance\n\nqb-stage uses tee, and a test covers it.\n")


# ================================================================ IT STARTS
# ================================================================ NOTHING

@pytest.mark.parametrize("cfg", [
    pytest.param({"github": "acme/r"}, id="no-block-at-all"),
    pytest.param({"github": "acme/r", "issue_pickup": {}}, id="empty-block"),
    pytest.param({"github": "acme/r", "issue_pickup": {"enabled": False}},
                 id="explicitly-off"),
    pytest.param({"github": "acme/r",
                  "issue_pickup": {"enabled": False, "only_labels": ["p0"],
                                   "allowed_authors": ["prisonblues"]}},
                 id="configured-but-off"),
])
def test_a_repo_that_has_not_opted_in_consults_no_judge_and_names_no_action(cfg):
    """The shipped default, against the issue most likely to be acted on.

    `judge=raises` is the point: it is not that the watcher declined to act on
    the judge's answer, it is that no judge was consulted at all, so no model was
    shown the issue and no money was spent deciding something the repo had
    already refused.
    """
    a = iw.assess(cfg, TEMPTING, judge=raises)
    assert a.action == "none"
    assert "issue_pickup.enabled" in a.why
    assert a.doable is None


def test_the_shipped_sample_keeps_the_gate_shut():
    """Landing this must start nothing, and the file that decides that is tracked.

    Asserted against the repository's own `.harness-rules.sample` rather than
    against `DEFAULTS`, because the accident this guards is a later edit to the
    sample rather than to the default — the sample is what a reader copies.
    """
    assert SAMPLE.is_file(), f"{SAMPLE} is what decides this and it is not here"
    got = json.loads(SAMPLE.read_text())["issue_pickup"]
    assert got["enabled"] is False
    assert got["only_labels"] == []
    assert got["require_human_triage"] is True


def _subprocess_calls(tree: ast.AST) -> list[ast.Call]:
    """Every call in `tree` that could put a process on this box."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        if name.split(".")[0] in ("subprocess", "os") or name in ("Popen", "run"):
            out.append(node)
    return out


def test_the_watcher_can_run_nothing_but_gh_and_qb_start():
    """The strong form of "it starts nothing on its own": a property, not a default.

    This module may now reach a session (#63's follow-up), so the audit is
    narrower than it was rather than gone. Two programs are permitted and they
    are named here: `gh`, and `qb-start` via a resolver that TAKES NO ARGUMENT.
    Everything else — in particular a command built out of a string this module
    computed, which is the shape an issue body could eventually reach through —
    still fails.
    """
    tree = ast.parse(Path(iw.__file__).read_text())
    calls = _subprocess_calls(tree)
    assert calls, "no subprocess call found — the walk is looking in the wrong place"
    for call in calls:
        rendered = ast.unparse(call)
        assert rendered.startswith("subprocess.run("), \
            f"only subprocess.run is expected here, got {rendered[:80]}"
        first = call.args[0]
        assert isinstance(first, ast.List) and first.elts, \
            f"a command built dynamically cannot be audited: {rendered[:80]}"
        head = first.elts[0]
        if isinstance(head, ast.Constant):
            assert head.value == "gh", \
                f"the only constant command here is gh: {rendered[:80]}"
            continue
        assert ast.unparse(head) == "qb_start_path()", \
            (f"a command head that is neither 'gh' nor qb_start_path() cannot be "
             f"audited: {rendered[:80]}")


def test_the_qb_start_resolver_cannot_be_pointed_at_anything_else():
    """`qb_start_path` takes no argument, and that is what the audit above rests on.

    A resolver with a parameter is one a caller could aim: the audit would still
    see `qb_start_path(...)` at the call site and pass, while the program that
    actually ran came from wherever the argument did. On a public tracker the
    honest worst case for "wherever" is a stranger's issue body. So the
    no-parameter shape is asserted rather than left to review, along with the
    only program name it may mention.
    """
    tree = ast.parse(Path(iw.__file__).read_text())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "qb_start_path"), None)
    assert fn is not None, "qb_start_path is what the audit names — it must exist"
    a = fn.args
    assert not (a.args or a.posonlyargs or a.kwonlyargs or a.vararg or a.kwarg), \
        "qb_start_path must take no argument: a parameter is a way to aim it"
    body = fn.body[1:] if ast.get_docstring(fn) else fn.body
    names = {n.value for stmt in body for n in ast.walk(stmt)
             if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert names <= {"bin", ""}, \
        (f"qb_start_path may name no program but QB_START: stray literals {names}")
    assert iw.QB_START == "qb-start"

    # …and the literal check alone is not enough, which a codex review pointed
    # out with a working counter-example: `return os.environ[QB_START]` adds no
    # string constant and would have passed everything above while letting the
    # environment name the program. So the callable surface is bounded too.
    called = {ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert called <= {"which", "str", "Path", "Path(__file__).resolve"}, \
        f"qb_start_path may not reach for anything else: {called}"
    reads = {ast.unparse(n) for n in ast.walk(fn) if isinstance(n, ast.Subscript)}
    assert not reads, \
        f"a subscript here is a lookup this test cannot bound: {reads}"


def test_a_survey_says_it_started_nothing(monkeypatch):
    """And says so where a person reads it, not only in a docstring."""
    monkeypatch.setattr(iw, "gh_json", lambda *a, **k: [TEMPTING])
    monkeypatch.setattr(iw, "triage", raises)
    got = iw.survey({"github": "acme/r"}, limit=5)
    out = iw.render(got, "acme/r")
    assert "0 actionable" in out
    assert "Nothing was started" in out


def test_nothing_reaches_the_board_on_a_repo_that_has_not_opted_in(monkeypatch,
                                                                  capsys):
    """#337's bar and #360's: hand the run a board that raises if it is opened.

    The gate refusing is not the assertion. The assertion is that the escalation
    door was never so much as knocked on, so the claim "landing this posts
    nothing" cannot be true only because the board happened to be unreachable.
    """
    monkeypatch.setenv("QUARTERBACK_NEEDS_HUMAN", "on")
    monkeypatch.setattr(needs_human, "_post", raises)
    monkeypatch.setattr(iw, "_load", lambda spec: {"github": "acme/r"})
    monkeypatch.setattr(iw, "describe", lambda cfg: "acme/r")
    monkeypatch.setattr(iw, "gh_json", lambda *a, **k: [TEMPTING])
    monkeypatch.setattr(iw, "triage", raises)
    monkeypatch.setattr(iw.subprocess, "run", raises)
    assert iw.main(["--announce", "--comment"]) == 0
    assert "none" in capsys.readouterr().out


def test_a_strangers_issue_is_never_shown_to_a_model(monkeypatch):
    """This repo is public and anyone may open an issue; under a watcher that
    text becomes the instructions for an agent with a full shell. The allowlist
    is checked before the judge, so the text of an issue nobody vouched for does
    not reach a model at all."""
    cfg = open_cfg()
    a = iw.assess(cfg, issue(9, author="drive-by", labels=("p0",),
                             body=TEMPTING["body"]), judge=raises)
    assert a.action == "none"
    assert "issue_pickup.allowed_authors" in a.why
    assert "drive-by" in a.why


def test_the_judge_runs_only_once_everything_else_has_said_yes():
    """The ordering is the cost control as well as the security property: an
    issue the gate refused, or a signal held, costs nothing to refuse."""
    seen = []

    def judge(work, model):
        seen.append(work.num)
        return doable(work, model)

    cfg = open_cfg()
    iw.assess(cfg, issue(1, author="drive-by", labels=("p0",)), judge=judge)
    iw.assess(cfg, issue(2, labels=("p0",), body="## Open questions\n\n- one?"),
              judge=judge)
    iw.assess(cfg, issue(3, labels=("p0",), body=TEMPTING["body"]), judge=judge)
    assert seen == [3]


# ================================================================ THE SIGNALS
# ================================================================ #63 NAMES

def signals(body="", *, cfg=None, title="A thing", labels=(), comments=(),
            states=None, number=1):
    got = iw.decision_signals(cfg or open_cfg(),
                              issue(number, title=title, body=body, labels=labels,
                                    comments=comments),
                              states=states)
    return {s.name: s.detail for s in got}


@pytest.mark.parametrize("heading", [
    "## Open questions", "### Open Question", "**Open questions**",
    "# Unresolved questions", "- Outstanding questions",
])
def test_an_open_questions_section_is_a_decision_owed(heading):
    """#63's own evidence: #51 asks six, #55 asks what spend is measured in."""
    assert "open_questions" in signals(f"Some prose.\n\n{heading}\n\n- Which one?")


def test_open_questions_in_a_sentence_is_not_a_section():
    """A heading is somebody structuring the issue around what is undecided.
    'this has no open questions' is the opposite claim and must not read as it."""
    assert "open_questions" not in signals("There are no open questions here.")


def test_options_with_no_recommendation_are_a_decision_owed():
    """#43's shape: three options and none recommended conclusively."""
    body = "## Option A\n\nDo it here.\n\n## Option B\n\nDo it there.\n"
    got = signals(body)
    assert "unrecommended_options" in got
    assert "a, b" in got["unrecommended_options"]


def test_options_settled_by_a_later_comment_stop_being_a_decision_owed():
    """Deciding is what comments are for — #63's own decision arrived as one
    beginning '**Decided:**', and an answered question must stop reading as one
    nobody has answered."""
    body = "## Option A\n\nHere.\n\n## Option B\n\nThere.\n"
    assert "unrecommended_options" in signals(body)
    assert "unrecommended_options" not in signals(
        body, comments=[("prisonblues", "**Decided:** Option B, per the board.")])


def test_one_option_is_not_a_choice():
    assert "unrecommended_options" not in signals("## Option A\n\nThe only one.")


def test_the_last_word_in_the_thread_being_a_question_is_a_decision_owed():
    assert "unanswered_question" in signals(
        "It is broken.", comments=[("prisonblues", "Should this live in app or in "
                                                   "the harness?")])


def test_a_question_somebody_has_since_answered_is_not_unanswered():
    """Otherwise every issue that ever discussed anything is held forever."""
    got = signals("Where should it live?",
                  comments=[("prisonblues", "Where should it live?"),
                            ("prisonblues", "In the harness. Go ahead.")])
    assert "unanswered_question" not in got


def test_a_stranger_cannot_answer_the_question_for_you():
    """Anyone can comment on an approved author's issue, so a reply from anyone
    at all would let a passer-by clear a hold by saying anything underneath it.
    Raising a hold costs a report line; clearing one spends money and writes
    code, so the two do not have the same admission."""
    got = signals("Where should it live?",
                  comments=[("drive-by", "In the harness, obviously.")])
    assert "unanswered_question" in got


def test_the_watchers_own_comment_is_not_part_of_the_conversation():
    """Otherwise the refusal answers the question it was posted to explain — it
    is the newest thing on the thread and it is signed by an allowlisted account
    — and the next run finds the issue actionable. Self-approval, one surface
    along from the hole `require_human_triage` closes.

    Asserted on `utterances` and not only on the signal, because the signal
    survives by accident: the comment QUOTES the question it is about, so it
    happens to re-raise it. An issue held for a dependency rather than a question
    has nothing quoted, and there the accident does not save it.
    """
    a = iw.assess(open_cfg(), issue(1, labels=("p0",), body="depends on #5"),
                  judge=raises)
    posted = iw.comment_body(a)
    said = iw.utterances(issue(1, body="Which store should this use?",
                               comments=[("prisonblues", posted)]))
    assert len(said) == 1, "the watcher's own comment is back in the thread"
    assert "unanswered_question" in signals(
        "Which store should this use?", comments=[("prisonblues", posted)])


def test_a_stranger_cannot_decide_the_options_for_you():
    """"**Decided:** option B" is a sentence anyone can write on a public repo,
    and without this it takes the brake off."""
    body = "## Option A\n\nHere.\n\n## Option B\n\nThere.\n"
    assert "unrecommended_options" in signals(
        body, comments=[("drive-by", "**Decided:** Option B.")])
    assert "unrecommended_options" not in signals(
        body, comments=[("prisonblues", "**Decided:** Option B.")])


def test_a_title_that_is_a_question_is_a_decision_owed():
    assert "decision_shape" in signals(title="Should qb-start be per machine?")


def test_a_choice_has_to_be_inside_a_question():
    """'which of the' turns up in ordinary description. A signal that fires on
    most of the backlog tells a reader nothing about the six issues that are
    genuinely waiting on them."""
    prose = "The reconciler decides which of the two branches wins. It is wrong."
    assert "decision_shape" not in signals(prose)
    assert "decision_shape" in signals("So: which of these three should it be?")


@pytest.mark.parametrize("phrase", [
    "depends on #5", "blocked by #5", "once #5 lands", "after #5",
    "requires #5", "builds on #5",
])
def test_an_open_dependency_is_a_decision_owed(phrase):
    """`epic.DEP_RE` reads all six spellings already, and #63 asks for exactly
    those — a parallel copy here is two parsers that agree today."""
    got = signals(f"This {phrase} and cannot start before it.",
                  states={5: "OPEN"})
    assert "open_dependency" in got and "#5" in got["open_dependency"]


def test_a_closed_dependency_is_not_a_dependency():
    assert "open_dependency" not in signals("depends on #5", states={5: "CLOSED"})


def test_a_dependency_whose_state_could_not_be_read_still_holds_the_issue():
    """Absence is not "closed". A dependency we failed to fetch is not one we
    know has landed, and the difference decides whether an issue is held."""
    assert "open_dependency" in signals("depends on #5", states={})


def test_an_issue_does_not_depend_on_itself():
    assert "open_dependency" not in signals("blocked by #4 landing", number=4,
                                            states={})


@pytest.mark.parametrize("cls", ["decision", "taste", "ui", "environment",
                                 "auth", "other"])
def test_a_needs_human_label_is_a_decision_owed(cls):
    """Via `appetite.refusal_verdict`, not a second reading of `skip_labels`:
    the skip list is repo policy and a second reader of it is a second policy."""
    assert "needs_human_label" in signals(labels=(f"needs-human/{cls}",))


def test_the_label_decides_which_kind_of_human_is_waiting():
    a = iw.assess(open_cfg(), issue(1, labels=("needs-human/ui", "p0")),
                  judge=raises)
    assert a.human_class == "ui"


def test_an_unlabelled_issue_is_not_reported_as_a_decision_owed():
    """`skip_when_unlabelled` answers "may I select this out of an untriaged
    backlog", which is the GATE's question and `pickup_verdict` has asked it.
    Asking it again here would report every unlabelled issue as one a human owes
    a decision on, and bury the six that do."""
    assert signals("Just a plain defect report.") == {}


def test_a_code_block_is_not_prose():
    """A traceback holds question marks and a config sample holds the word
    "option". Counting those is how a defect with a good repro — the one issue
    class this module exists to let through — reads as an open decision."""
    body = ("Here is the repro:\n\n```\n$ qb-start\nOption A: which one?\n"
            "depends on #5\n```\n\nThat is the whole fault.\n")
    assert signals(body, states={}) == {}


# ================================================================ THE LADDER

def act(cfg=None, *, body="", labels=("p0",), author="prisonblues", judge=doable,
        comments=()):
    a = iw.assess(cfg or open_cfg(),
                  issue(1, body=body, labels=labels, author=author,
                        comments=comments),
                  judge=judge)
    return a.action, a.why


def test_a_decision_owed_beats_a_doable_ruling():
    """#63's whole argument: #51 was perfectly implementable and must not have
    been implemented. `doable` means "can an agent do this", not "has a human
    settled what to do", and the second question is the one that decides."""
    action, why = act(body="## Open questions\n\n- Which store?\n")
    assert action == "none" and "open_questions" in why


def test_an_untriaged_issue_is_not_confirmed_doable():
    """`doable=None` means no judgement was possible, which is not the same as a
    ruling that it is fine — `epic.py` treats it as not-confirmed and so does
    this."""
    action, why = act(body="## Acceptance\n\nIt works.",
                      judge=lambda w, m: (None, "judge timed out", "", "environment"))
    assert action == "none" and "timed out" in why


def test_a_not_agent_doable_ruling_stops_it():
    action, why = act(body="## Acceptance\n\nIt works.",
                      judge=lambda w, m: (False, "needs a licence", "", "decision"))
    assert action == "none" and "needs a licence" in why


def test_a_closed_issue_earns_no_action_however_open_the_gate():
    """A backlog sweep only sees open issues, but `--issue 63` names one and
    `gh issue view` answers about a closed one just as readily."""
    shut = dict(TEMPTING, state="CLOSED")
    a = iw.assess(open_cfg(), shut, judge=raises)
    assert a.action == "none" and "closed" in a.why


def test_a_specified_defect_that_everything_admits_earns_fix_issue():
    action, _ = act(body=TEMPTING["body"])
    assert action == "/fix-issue"


def test_investigate_is_the_default_rung_when_nothing_says_how_it_is_fixed():
    """`/investigate` produces understanding and writes no code, so it is the
    safer answer whenever the choice is close. Escalating wants evidence that
    somebody wrote down what "fixed" means."""
    action, why = act(body="The dashboard is slow sometimes. Not sure why.")
    assert action == "/investigate" and "understanding first" in why


def test_a_recommended_action_is_named_and_not_run(monkeypatch):
    """Even the actionable rung is a line of a report. The watcher names what
    could be run; running it is `qb-start`, and it is off."""
    monkeypatch.setattr(iw, "gh_json", lambda *a, **k: [issue(1, labels=("p0",),
                                                             body=TEMPTING["body"])])
    monkeypatch.setattr(iw, "triage", doable)
    monkeypatch.setattr(iw.subprocess, "run", raises)
    got = iw.survey(open_cfg(), limit=5)
    assert [a.action for a in got] == ["/fix-issue"]
    assert "1 actionable" in iw.render(got, "acme/r")


# ================================================================ SAYING SO

def test_a_gate_refusal_is_not_announced_as_a_question(monkeypatch, capsys):
    """A gate refusal is the repo's standing answer, not a question about this
    issue. Announcing one per issue would post the whole backlog to the board the
    first time a watcher ran, and would say "a human decision is owed" about
    issues where none is."""
    monkeypatch.setenv("QUARTERBACK_NEEDS_HUMAN", "on")
    monkeypatch.setattr(needs_human, "_post", raises)
    monkeypatch.setattr(iw, "_load", lambda spec: {"github": "acme/r"})
    monkeypatch.setattr(iw, "describe", lambda cfg: "acme/r")
    monkeypatch.setattr(iw, "gh_json", lambda *a, **k: [
        issue(1, labels=("p0",), body="Plain defect, nothing owed.")])
    monkeypatch.setattr(iw, "triage", raises)
    monkeypatch.setattr(iw.subprocess, "run", raises)
    assert iw.main(["--announce"]) == 0


def test_a_held_issue_is_announced_with_the_signals_in_its_key(monkeypatch, tmp_path):
    """`SEEN_PATH` goes to tmp_path, and not for tidiness: it defaults to this
    HOST's cache, so a second run of this test would be suppressed by the first
    one's own dedupe record and would fail having proved the mechanism works."""
    posted = []
    monkeypatch.setenv("QUARTERBACK_NEEDS_HUMAN", "on")
    monkeypatch.setattr(needs_human, "SEEN_PATH", tmp_path / "seen.json")
    monkeypatch.setattr(needs_human, "_post", lambda body: posted.append(body) or (7, ""))
    a = iw.assess(open_cfg(), issue(3, body="## Open questions\n\n- Which?\n",
                                    labels=("p0",)), judge=raises)
    said = iw.announce_held(a, open_cfg(), "acme/r")
    assert "post 7" in said
    assert "open_questions" in posted[0]["detail"]
    assert posted[0]["type"] == "stuck"


def test_the_watcher_never_comments_on_a_strangers_issue():
    """A stranger who could make the watcher comment could make it quote them
    under the repo owner's account, a thousand times."""
    a = iw.assess(open_cfg(), issue(1, author="drive-by"), judge=raises)
    ok, why = iw.may_write_on(open_cfg(), a)
    assert ok is False and "issue_pickup.allowed_authors" in why


def test_commenting_does_not_need_the_pickup_gate_open():
    """`enabled` answers "may a loop CHOOSE its own work", and saying what an
    issue is waiting on chooses nothing. The useful first cut of #63 is a
    watcher that reports on a repo whose pickup gate is, and stays, shut."""
    cfg = {"github": "acme/r", "issue_pickup": {"allowed_authors": ["prisonblues"]}}
    a = iw.assess(cfg, issue(1), judge=raises)
    assert a.gate.allowed is False
    assert iw.may_write_on(cfg, a)[0] is True


def test_an_unattended_run_reports_but_does_not_write(monkeypatch):
    """#40's standing decision, read off the setting a repo has already answered
    it with rather than a second switch that would come to disagree."""
    monkeypatch.setenv("HARNESS_UNATTENDED", "1")
    assert iw.may_write({"github": "acme/r"})[0] is False
    assert iw.may_write({"github": "acme/r",
                         "issue_filing": {"unattended": True}})[0] is True
    monkeypatch.delenv("HARNESS_UNATTENDED")
    assert iw.may_write({"github": "acme/r"})[0] is True


def test_a_comment_is_not_repeated_but_a_new_question_is():
    """A watcher that never repeats itself also never mentions the NEW question,
    and one that ignores its own comments posts the same paragraph every hour."""
    a = iw.assess(open_cfg(), issue(1, body="## Open questions\n\n- Which?\n",
                                    labels=("p0",)), judge=raises)
    body = iw.comment_body(a)
    assert iw.already_said(a, body) is False
    a.comments = [{"author": {"login": "bot"}, "body": body}]
    assert iw.already_said(a, body) is True

    b = iw.assess(open_cfg(), issue(1, body="depends on #5\n", labels=("p0",)),
                  judge=raises)
    b.comments = list(a.comments)
    assert iw.already_said(b, iw.comment_body(b)) is False


def test_a_comment_names_the_decision_that_is_missing():
    """Silence is indistinguishable from a broken watcher — #63's diagnosis of
    half the tracker."""
    a = iw.assess(open_cfg(), issue(1, body="## Open questions\n\n- Which?\n",
                                    labels=("p0",)), judge=raises)
    body = iw.comment_body(a)
    assert body.startswith(iw.COMMENT_MARKER)
    assert "open_questions" in body and "Not started" in body


# ================================================================ THE CLI

def test_a_single_issue_verdict_exits_three_when_it_is_held(monkeypatch, capsys):
    """`appetite.py`'s convention, so a shell caller can gate on it without
    parsing."""
    monkeypatch.setattr(iw, "_load", lambda spec: {"github": "acme/r"})
    monkeypatch.setattr(iw, "describe", lambda cfg: "acme/r")
    monkeypatch.setattr(iw, "gh_json", lambda *a, **k: TEMPTING)
    monkeypatch.setattr(iw, "triage", raises)
    monkeypatch.setattr(iw.subprocess, "run", raises)
    assert iw.main(["--issue", "7"]) == 3
    assert "#7" in capsys.readouterr().out


def test_a_backlog_with_nothing_actionable_is_the_healthy_state(monkeypatch):
    """A survey is not a verdict. Exiting non-zero for the ordinary case would
    read as the watcher being broken, in a timer nobody is watching."""
    monkeypatch.setattr(iw, "_load", lambda spec: {"github": "acme/r"})
    monkeypatch.setattr(iw, "describe", lambda cfg: "acme/r")
    monkeypatch.setattr(iw, "gh_json", lambda *a, **k: [TEMPTING])
    monkeypatch.setattr(iw, "triage", raises)
    monkeypatch.setattr(iw.subprocess, "run", raises)
    assert iw.main([]) == 0


def test_no_triage_does_not_manufacture_a_verdict(monkeypatch):
    """`None` and not `False`: nobody ruled this undoable, we declined to ask."""
    monkeypatch.setattr(iw, "gh_json", lambda *a, **k: [
        issue(1, labels=("p0",), body=TEMPTING["body"])])
    monkeypatch.setattr(iw, "triage", raises)
    monkeypatch.setattr(iw.subprocess, "run", raises)
    got = iw.survey(open_cfg(), use_judge=False)
    assert got[0].doable is None and got[0].action == "none"
    assert "--no-triage" in got[0].why


def test_the_json_report_carries_the_gate_and_the_signals(monkeypatch, capsys):
    monkeypatch.setattr(iw, "_load", lambda spec: {"github": "acme/r"})
    monkeypatch.setattr(iw, "gh_json", lambda *a, **k: [
        issue(2, body="## Open questions\n\n- Which?\n")])
    monkeypatch.setattr(iw, "triage", raises)
    monkeypatch.setattr(iw.subprocess, "run", raises)
    assert iw.main(["--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    one = got["issues"][0]
    assert one["gate"]["setting"] == "issue_pickup.enabled"
    assert [s["signal"] for s in one["signals"]] == ["open_questions"]
    assert one["action"] == "none" and "comments" not in one


def test_the_event_log_is_read_only_where_it_can_change_the_answer(monkeypatch):
    """One paginated API call per candidate. A watcher sweeping a backlog with
    the gate off must not make one per issue for an answer it cannot use."""
    monkeypatch.setattr(iw, "gh_json", lambda *a, **k: [TEMPTING])
    monkeypatch.setattr(iw, "label_events", raises)
    monkeypatch.setattr(iw, "triage", raises)
    monkeypatch.setattr(iw.subprocess, "run", raises)
    assert iw.survey({"github": "acme/r"})[0].action == "none"


def test_a_dependency_state_is_read_once_however_many_issues_name_it(monkeypatch):
    """Six issues waiting on the same one is six calls otherwise, and they
    cannot disagree."""
    calls = []

    def fake(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '{"state": "OPEN"}', "")

    monkeypatch.setattr(iw, "gh_json", lambda *a, **k: [
        issue(1, body="depends on #5"), issue(2, body="blocked by #5")])
    monkeypatch.setattr(iw, "triage", raises)
    monkeypatch.setattr(iw.subprocess, "run", fake)
    got = iw.survey({"github": "acme/r"})
    assert len(calls) == 1
    assert all("open_dependency" in [s.name for s in a.signals] for a in got)


# ============================================== what appetite grew for this

def test_events_are_needed_only_when_both_settings_ask_for_them():
    assert appetite.events_needed({}) is False
    assert appetite.events_needed(
        {"issue_pickup": {"enabled": True, "require_human_triage": False}}) is False
    assert appetite.events_needed(
        {"issue_pickup": {"enabled": True, "require_human_triage": True}}) is True


def test_the_author_verdict_names_the_setting_that_refused():
    v = appetite.author_verdict({"issue_pickup": {"allowed_authors": ["rich"]}},
                                "drive-by")
    assert v.allowed is False and v.setting == "issue_pickup.allowed_authors"
    assert appetite.author_verdict(
        {"issue_pickup": {"allowed_authors": ["rich"]}}, "RICH").allowed is True


def test_an_empty_allowlist_still_means_nobody_through_the_new_door():
    """The helper is a second entry point to one allowlist, not a second
    allowlist — so it must fail closed in exactly the same place."""
    assert appetite.author_verdict({}, "prisonblues").allowed is False


# ================================================================ AND THE
# ================================================================ ACTING HALF

#: Two issues that WILL come back actionable, so the tests below are about the
#: start pass rather than about the gate refusing before it.
def _actionable(n, title="Thing is broken"):
    return issue(n, title=title, labels=("p0",), author="prisonblues",
                 body="## Steps to reproduce\n\nRun it.\n\n## Acceptance\n\n"
                      "It stops doing that, and a test covers it.\n")


@pytest.fixture
def spawns(monkeypatch):
    """Records what reached `qb-start`, and answers with whatever exits are queued.

    Patches `subprocess.run` inside the module rather than `start_one`, so the
    argv the real code builds is what gets asserted — a double at the `start_one`
    seam would let the `--via`/command/number wiring rot untested.
    """
    calls, exits = [], []

    class Done:
        def __init__(self, code):
            self.returncode = code

    def fake(argv, **_kw):
        # ONE queue for both the `--policy` probe and the spawn requests, in
        # call order — so a test that queues exits has to account for the probe,
        # which is the arithmetic `--attempt-max` gets wrong if nobody does.
        # (This used to branch on `--policy` into two identical arms, implying a
        # distinction that was never there.)
        calls.append(list(argv))
        return Done(exits.pop(0) if exits else 0)

    monkeypatch.setattr(iw.subprocess, "run", fake)
    monkeypatch.setattr(iw, "qb_start_path", lambda: "/usr/bin/qb-start")
    return type("S", (), {"calls": calls, "exits": exits})()


def _starts(spawns):
    """Only the spawn calls — `--policy` is a question, not a start."""
    return [c for c in spawns.calls if "--policy" not in c]


def test_a_survey_starts_nothing_unless_it_is_asked_to(monkeypatch):
    """`--start` is off, and the proof is that the door is never knocked on.

    `spawning_enabled` raises: a run that consulted the machine's spawn policy at
    all — even to be told no — would be one line away from acting on a default,
    and #63's whole argument is that acting is the thing that needs asking for.
    """
    monkeypatch.setattr(iw, "_load", lambda spec: open_cfg())
    monkeypatch.setattr(iw, "describe", lambda cfg: "acme/r")
    monkeypatch.setattr(iw, "gh_json", lambda *a, **k: [_actionable(7)])
    monkeypatch.setattr(iw, "triage", doable)
    monkeypatch.setattr(iw, "spawning_enabled", raises)
    monkeypatch.setattr(iw, "start_one", raises)
    assert iw.main([]) == 0


def test_a_machine_that_never_opted_in_is_asked_once_not_once_per_issue(spawns,
                                                                       monkeypatch):
    """The commonest outcome by far, and it must cost one line.

    `qb-start --policy` answers no, so no spawn is attempted at all — and the
    reason lands on every actionable issue rather than on the first one.
    """
    monkeypatch.setattr(iw, "_load", lambda spec: open_cfg())
    monkeypatch.setattr(iw, "describe", lambda cfg: "acme/r")
    monkeypatch.setattr(iw, "gh_json",
                        lambda *a, **k: [_actionable(7), _actionable(8)])
    monkeypatch.setattr(iw, "triage", doable)
    spawns.exits.append(3)                     # --policy: NOT_ENABLED
    assert iw.main(["--start"]) == 0
    assert len(spawns.calls) == 1, "the policy question must be asked once"
    assert _starts(spawns) == [], "nothing may be started on an opted-out machine"


def test_what_reaches_qb_start_is_the_action_the_number_and_the_watch_trigger(
        spawns, monkeypatch):
    """The wiring itself: provenance is `watch`, and the brief is a named command.

    `--via watch` is the point. A session that appears on a box nobody is
    watching raises exactly one question, and this is the argv that has to be
    able to answer it.
    """
    monkeypatch.setattr(iw, "_load", lambda spec: dict(open_cfg(), path="/w/r"))
    monkeypatch.setattr(iw, "describe", lambda cfg: "acme/r")
    monkeypatch.setattr(iw, "gh_json", lambda *a, **k: [_actionable(7)])
    monkeypatch.setattr(iw, "triage", doable)
    assert iw.main(["--start"]) == 0
    (argv,) = _starts(spawns)
    assert argv[0] == "/usr/bin/qb-start"
    assert argv[1:3] == ["--via", "watch"]
    assert "/fix-issue" in argv and "7" in argv
    assert argv[argv.index("--repo-path") + 1] == "/w/r"


def test_a_dry_run_starts_nothing_and_says_that_is_why(spawns, monkeypatch,
                                                       capsys):
    monkeypatch.setattr(iw, "_load", lambda spec: open_cfg())
    monkeypatch.setattr(iw, "describe", lambda cfg: "acme/r")
    monkeypatch.setattr(iw, "gh_json", lambda *a, **k: [_actionable(7)])
    monkeypatch.setattr(iw, "triage", doable)
    assert iw.main(["--start", "--dry-run"]) == 0
    (argv,) = _starts(spawns)
    assert "--dry-run" in argv
    assert "would start" in capsys.readouterr().out


def test_one_run_starts_at_most_start_max_and_says_so_on_the_rest(spawns,
                                                                  monkeypatch):
    """The ceiling this module owns, which is not the one qb-start owns.

    The issues past the cap are recorded as `not attempted` rather than left
    blank: "the watcher declined this" and "the watcher ran out of room" are
    different answers to "why did nothing happen".
    """
    monkeypatch.setattr(iw, "_load", lambda spec: open_cfg())
    monkeypatch.setattr(iw, "describe", lambda cfg: "acme/r")
    monkeypatch.setattr(iw, "gh_json",
                        lambda *a, **k: [_actionable(n) for n in (7, 8, 9)])
    monkeypatch.setattr(iw, "triage", doable)
    got = iw.survey(open_cfg(), limit=9)
    iw.run_starts(got, open_cfg(), limit=1)
    assert [a.started for a in got][0] == "started"
    assert all("--start-max" in a.started for a in got[1:])
    assert len(_starts(spawns)) == 1


def test_a_refusal_about_the_box_stops_the_sweep(spawns, monkeypatch):
    """At-cap is a fact about the machine, so asking again per issue is noise.

    Thirty identical refusals is how a watcher becomes the thing people mute —
    and each one would have posted to the board and attempted a claim.
    """
    monkeypatch.setattr(iw, "gh_json",
                        lambda *a, **k: [_actionable(n) for n in (7, 8, 9)])
    monkeypatch.setattr(iw, "triage", doable)
    got = iw.survey(open_cfg(), limit=9)
    spawns.exits.extend([0, 5])                # --policy ok, then AT_CAP
    iw.run_starts(got, open_cfg(), limit=5)
    assert len(_starts(spawns)) == 1, "a per-box refusal is asked once"
    assert "spawn cap" in got[0].started
    assert all("not attempted" in a.started for a in got[1:])


def test_a_refusal_about_one_issue_does_not_stop_the_sweep(spawns, monkeypatch):
    """Somebody holding #7 says nothing about #8, so the next one is still tried."""
    monkeypatch.setattr(iw, "gh_json",
                        lambda *a, **k: [_actionable(7), _actionable(8)])
    monkeypatch.setattr(iw, "triage", doable)
    got = iw.survey(open_cfg(), limit=9)
    spawns.exits.extend([0, 8, 0])             # --policy ok, HELD, then started
    iw.run_starts(got, open_cfg(), limit=5)
    assert len(_starts(spawns)) == 2
    assert "already holds" in got[0].started
    assert got[1].started == "started"


def test_a_held_issue_is_never_handed_to_qb_start(spawns, monkeypatch):
    """The signals are the brake, and --start does not reach past them.

    The tempting issue with an open question on it: agent-doable, owner-authored,
    correctly labelled, and a human decision still owed.
    """
    body = TEMPTING["body"] + "\n## Open questions\n\n- Which of the three?\n"
    monkeypatch.setattr(iw, "gh_json",
                        lambda *a, **k: [issue(7, labels=("p0",), body=body)])
    monkeypatch.setattr(iw, "triage", raises)
    got = iw.survey(open_cfg(), limit=9)
    iw.run_starts(got, open_cfg(), limit=5)
    assert got[0].held and got[0].action == "none"
    assert spawns.calls == [], "a held issue must not even raise the policy question"


def test_a_strangers_issue_is_never_handed_to_qb_start(spawns, monkeypatch):
    """The allowlist bounds the acting half too, and it is checked before the judge."""
    stranger = dict(_actionable(7), author={"login": "a-stranger"})
    monkeypatch.setattr(iw, "gh_json", lambda *a, **k: [stranger])
    monkeypatch.setattr(iw, "triage", raises)
    got = iw.survey(open_cfg(), limit=9)
    iw.run_starts(got, open_cfg(), limit=5)
    assert got[0].action == "none"
    assert spawns.calls == []


def test_what_became_of_the_action_is_in_the_json(spawns, monkeypatch, capsys):
    """#63 asks for the verdict to be recorded, and JSON is what a consumer reads."""
    monkeypatch.setattr(iw, "_load", lambda spec: open_cfg())
    monkeypatch.setattr(iw, "describe", lambda cfg: "acme/r")
    monkeypatch.setattr(iw, "gh_json", lambda *a, **k: [_actionable(7)])
    monkeypatch.setattr(iw, "triage", doable)
    assert iw.main(["--start", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)["issues"][0]
    assert got["action"] == "/fix-issue" and got["started"] == "started"


def test_a_survey_that_did_not_start_records_absent_not_refused(monkeypatch):
    """Blank means "never asked", and that must not read as a refusal."""
    monkeypatch.setattr(iw, "gh_json", lambda *a, **k: [_actionable(7)])
    monkeypatch.setattr(iw, "triage", doable)
    got = iw.survey(open_cfg(), limit=9)
    assert got[0].started == ""
    assert got[0].as_dict()["started"] == ""


def test_an_unattended_run_surveys_but_does_not_spawn(spawns, monkeypatch):
    """"Start it with a human watching" — the plan's instruction, encoded.

    `HARNESS_UNATTENDED=1` with a repo that has not said loops may write here
    unwatched: the survey still happens and still reports, and the machine's
    spawn policy is never even consulted.
    """
    monkeypatch.setenv("HARNESS_UNATTENDED", "1")
    monkeypatch.setattr(iw, "gh_json", lambda *a, **k: [_actionable(7)])
    monkeypatch.setattr(iw, "triage", doable)
    got = iw.survey(open_cfg(), limit=9)
    assert got[0].action == "/fix-issue", "the survey half is unaffected"
    iw.run_starts(got, open_cfg(), limit=5)
    assert spawns.calls == [], "an unattended run must not consult the spawn policy"
    assert "unattended" in got[0].started


def test_an_unattended_run_may_spawn_where_the_repo_allowed_it(spawns, monkeypatch):
    """The same switch the repo already answered for writing, not a second one."""
    monkeypatch.setenv("HARNESS_UNATTENDED", "1")
    cfg = dict(open_cfg(), issue_filing={"unattended": True})
    monkeypatch.setattr(iw, "gh_json", lambda *a, **k: [_actionable(7)])
    monkeypatch.setattr(iw, "triage", doable)
    got = iw.survey(cfg, limit=9)
    iw.run_starts(got, cfg, limit=5)
    assert got[0].started == "started"


# ============================================================ THE TWO BUDGETS
# a codex review found --start-max bounded the wrong thing (see 63.feat.md)

def _many(n, action="/fix-issue"):
    return [iw.Assessment(number=i, title=f"t{i}", author="prisonblues",
                          action=action, why="w") for i in range(n)]


def test_a_backlog_of_held_issues_cannot_burn_the_board(spawns, monkeypatch):
    """The bug `--start-max`'s own docstring claimed to prevent, and did not.

    A refusal about one issue does not stop the sweep and starts no session, so
    it spends none of `--start-max` — which meant thirty issues held by peers
    made thirty `qb-start` calls and thirty board posts while `--start-max 1`
    looked like it was holding. `--attempt-max` is the counter a refusal spends.
    """
    monkeypatch.setattr(iw, "may_write", lambda cfg: (True, ""))
    spawns.exits.extend([0] + [8] * 30)         # policy ok, then all HELD
    got = _many(30)
    iw.run_starts(got, {"path": "."}, limit=1, attempts_max=5)
    assert len(_starts(spawns)) == 5, "the attempt budget is what bounds this"
    assert "--attempt-max" in got[-1].started


def test_a_command_the_policy_refuses_is_asked_once_not_once_per_issue(spawns,
                                                                       monkeypatch):
    """Exit 4 is a fact about (this box, this command), never about the issue.

    So unlike a held issue there is no chance at all that the next one answers
    differently, and re-asking is the same runaway in miniature.
    """
    monkeypatch.setattr(iw, "may_write", lambda cfg: (True, ""))
    spawns.exits.extend([0] + [4] * 30)
    got = _many(30)
    iw.run_starts(got, {"path": "."}, limit=5, attempts_max=20)
    assert len(_starts(spawns)) == 1
    assert "/fix-issue" in got[-1].started and "not attempted" in got[-1].started


def test_a_refused_command_does_not_hold_back_a_different_one(spawns,
                                                              monkeypatch):
    """Remembering is per COMMAND. A policy allowing /investigate and not
    /fix-issue must still start the /investigate one."""
    monkeypatch.setattr(iw, "may_write", lambda cfg: (True, ""))
    spawns.exits.extend([0, 4, 0])
    got = _many(1) + _many(1, action="/investigate")
    iw.run_starts(got, {"path": "."}, limit=5, attempts_max=20)
    assert "not allow" in got[0].started
    assert got[1].started == iw.STARTED


@pytest.mark.parametrize("limit,attempts,says", [
    pytest.param(0, 5, "--start-max of 0", id="start-max-zero"),
    pytest.param(1, 0, "--attempt-max of 0", id="attempt-max-zero"),
])
def test_a_zero_ceiling_is_a_freeze_that_asks_nothing_at_all(spawns, monkeypatch,
                                                             limit, attempts, says):
    """Zero means what it says, INCLUDING not knocking on the machine's door.

    Asserted on `spawns.calls`, not on `_starts()`. The distinction is the whole
    test: `_starts` filters out the `qb-start --policy` probe, so an earlier
    version of this passed while the probe still ran — the test was named "asks
    nothing" and was checking something weaker. A codex re-review caught exactly
    that, and the fix was to return before probing rather than to rename the test.
    """
    monkeypatch.setattr(iw, "may_write", lambda cfg: (True, ""))
    got = _many(3)
    iw.run_starts(got, {"path": "."}, limit=limit, attempts_max=attempts)
    assert spawns.calls == [], "a frozen run must not even ask --policy"
    assert all(says in a.started for a in got)


def test_the_attempt_budget_is_spawn_requests_not_qb_start_calls(spawns,
                                                                 monkeypatch):
    """`--attempt-max N` permits N spawn requests plus the one policy probe.

    Pinned because the docstring used to say "how many times one run may call
    qb-start at all", which was false by exactly one. The probe is a question —
    it posts nothing, claims nothing and reads a local file — so it is
    deliberately outside the budget; what is not acceptable is the prose and the
    behaviour disagreeing, so the arithmetic is asserted here.
    """
    monkeypatch.setattr(iw, "may_write", lambda cfg: (True, ""))
    spawns.exits.extend([0] + [8] * 30)
    iw.run_starts(_many(30), {"path": "."}, limit=9, attempts_max=5)
    assert len(_starts(spawns)) == 5, "five spawn requests"
    assert len(spawns.calls) == 6, "…plus exactly one policy probe"
    assert len([c for c in spawns.calls if "--policy" in c]) == 1


@pytest.mark.parametrize("bad", ["-1", "-30", "two", ""])
def test_a_negative_or_malformed_ceiling_is_a_cli_error(bad):
    """It used to be accepted, and it failed closed — safe, and still wrong.

    A ceiling that silently reinterprets `-1` (which an operator writes meaning
    "no limit") as a freeze is one nobody can tell from a working one.
    """
    with pytest.raises(SystemExit):
        iw.main(["--start", "--start-max", bad])
    with pytest.raises(SystemExit):
        iw.main(["--start", "--attempt-max", bad])
