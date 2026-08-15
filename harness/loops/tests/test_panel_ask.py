"""The cheap premise check — `panel.py --ask` (#79).

A fix's premise had no challenger but a whole round: twenty minutes, every seat
reading the entire diff, thirty findings back when what was wanted was one answer
to one question. PR #62 spent three rounds that way on a yes/no question about one
branch of `panel.py`, each round trusting a fresh proxy for "did a review happen?"
and each proxy killed by the next round.

So these tests pin the three things that make an ask worth trusting rather than
merely fast:

* **an unreadable reply is not `cannot tell`.** One is a seat saying its context
  did not settle the question; the other is a seat whose answer we do not have.
  Collapsing them is #68's panel-of-one arriving through a side door — a tally
  that reads "nobody objected" over seats that never spoke.
* **nothing picks between candidates.** A reply holding two different legal
  verdicts is unreadable, not an opportunity to guess which one the model meant.
* **a tally with no standing says so.** Too few seats, or only the seat that
  wrote the premise, is `unchallenged` — which is where the premise started, and
  is never reported as confirmation.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import harness_rules  # noqa: E402
import panel  # noqa: E402

ANSWER = '{"verdict": "fails", "reason": "the skip branch returns finish(failed)"}'


def _answers(**seats) -> dict[str, panel.SeatAnswer]:
    """A tally's input: seat name -> the verdict it gave, or a SeatAnswer for the
    seats that gave none."""
    return {n: v if isinstance(v, panel.SeatAnswer) else panel.SeatAnswer(v, "because")
            for n, v in seats.items()}


# ---- reading one seat's reply ----------------------------------------------

def test_a_verdict_and_its_reason_are_read():
    got = panel.parse_answer(ANSWER)
    assert got == panel.Answer("fails", "the skip branch returns finish(failed)")


@pytest.mark.parametrize("said,want", [
    ("holds", "holds"),
    ("FAILS", "fails"),
    ("cannot tell", "cannot tell"),
    ("cannot_tell", "cannot tell"),
    ("Cannot  Tell", "cannot tell"),
    ("can't tell", "cannot tell"),
])
def test_the_three_verdicts_are_read_however_they_are_spelled(said, want):
    """A closed list of the same three words, normalised for case and for the
    separators a model reaches for in JSON. Nothing fuzzier: an unrecognised
    word must stay unreadable rather than be guessed into the tally."""
    assert panel.parse_answer(json.dumps({"verdict": said})) == panel.Answer(want, "")


@pytest.mark.parametrize("said", ["yes", "no", "true", "P2", "probably holds", "",
                                  "holds|fails|cannot tell"])
def test_a_verdict_that_is_not_one_of_the_three_is_unreadable(said):
    assert panel.parse_answer(json.dumps({"verdict": said, "reason": "r"})) is None


def test_the_prompts_own_example_is_not_an_answer():
    """The whole echo defence this parser needs, and it is structural rather than
    inferred: the schema spells its verdict as the UNION of the three legal
    values, so quoting it back produces a verdict that is not one of them. The
    review path needs `_quoted` for this because its example is a fully populated
    finding that reads exactly like an answer."""
    schema = next(s for _, s in panel._spans(panel.ASK_PROMPT, "{", "}")
                  if '"verdict"' in s)
    assert panel.parse_answer(schema.replace("{{", "{").replace("}}", "}")) is None


def test_an_echo_beside_a_real_answer_resolves_to_the_answer():
    reply = ('Returning the shape you asked for: '
             '{"verdict": "holds|fails|cannot tell", "reason": "one line"}\n'
             'My answer: {"verdict": "cannot tell", "reason": "the range stops short"}')
    assert panel.parse_answer(reply) == panel.Answer("cannot tell", "the range stops short")


def test_two_different_verdicts_in_one_reply_are_unreadable():
    """Nothing here picks. Choosing by position or by count is how a `holds` gets
    recorded for a seat that also wrote `fails`, and a wrong verdict is worse
    than a missing one — it carries the panel's authority."""
    assert panel.parse_answer('{"verdict":"holds"} ... {"verdict":"fails"}') is None


def test_two_candidates_that_agree_are_one_answer():
    """Differently worded reasons for the same verdict are not a disagreement —
    the last wording is the one the model settled on."""
    reply = ('{"verdict":"fails","reason":"first pass"} '
             'on reflection: {"verdict":"fails","reason":"second pass"}')
    assert panel.parse_answer(reply) == panel.Answer("fails", "second pass")


@pytest.mark.parametrize("reply", [None, "", "   ", "no json here at all",
                                   '{"verdict": null}', '{"verdict": 3}',
                                   '["fails"]', '{"findings": [], "could_not_assess": []}'])
def test_a_reply_carrying_no_verdict_is_unreadable(reply):
    """Including a REVIEW: a seat that answered the wrong question has not
    answered this one, and its findings go nowhere."""
    assert panel.parse_answer(reply) is None


def test_a_reason_is_one_line_and_bounded():
    reply = json.dumps({"verdict": "holds", "reason": "line one\n  line two\t" + "x" * 900})
    got = panel.parse_answer(reply)
    assert "\n" not in got.reason and got.reason.startswith("line one line two ")
    assert len(got.reason) == panel.ASK_REASON_CHARS


def test_a_missing_reason_is_empty_and_not_a_failure_to_answer():
    """The verdict is the answer; the reason is what it is worth. A seat that
    gave one and not the other has still voted."""
    assert panel.parse_answer('{"verdict": "holds"}') == panel.Answer("holds", "")


# ---- the tally --------------------------------------------------------------

def test_the_threshold_decides_and_the_counts_are_reported():
    got = panel.ask_tally(_answers(claude="fails", codex="fails", pi="cannot tell"),
                          quorum=3, threshold=2)
    assert got.verdict == "fails" and got.answered == 3
    assert got.counts == {"holds": 0, "fails": 2, "cannot tell": 1}
    assert "2 of 3" in got.reason and "quorum 3, threshold 2" in got.reason


def test_cannot_tell_counts_for_quorum_and_never_for_the_threshold():
    """It is a seat that LOOKED, so it is in the quorum; it agreed with nothing,
    so it can never carry a verdict. A panel of three that could not read the
    code must not come back as agreement."""
    got = panel.ask_tally(_answers(claude="cannot tell", codex="cannot tell",
                                   pi="cannot tell"), quorum=2, threshold=2)
    assert got.verdict == "unresolved" and got.answered == 3
    assert got.counts["cannot tell"] == 3


def test_too_few_answers_is_unchallenged_rather_than_the_answer():
    got = panel.ask_tally(_answers(claude="fails"), quorum=2, threshold=1)
    assert got.verdict == "unchallenged" and "quorum is 2" in got.reason


def test_the_asker_alone_is_not_a_challenge():
    """An agent putting its own premise to itself has confirmed nothing, and
    reporting that as `holds` is worse than reporting nothing — it carries a
    panel's authority. Same rule as #78's `self_approval` and #40's refusal to
    let a reviewer act on its own finding unattended."""
    got = panel.ask_tally(_answers(claude="holds"), quorum=1, threshold=1,
                          asker="claude")
    assert got.verdict == "unchallenged" and "the asker" in got.reason


def test_the_asker_voting_beside_another_seat_is_a_challenge():
    got = panel.ask_tally(_answers(claude="holds", codex="holds"), quorum=2,
                          threshold=2, asker="claude")
    assert got.verdict == "holds"


def test_an_answer_carried_only_by_the_asker_is_unchallenged():
    """The layer that matters. The outer check catches the asker being the only
    SEAT; under `ask_threshold: 1` an asker could reach the threshold on its own
    vote while every other seat said `cannot tell` — quorum met, two seats
    answered, and a verdict resting entirely on the agent that wrote the premise.
    What has to be true is that the ANSWER is not the asker's alone."""
    got = panel.ask_tally(_answers(claude="holds", codex="cannot tell"),
                          quorum=2, threshold=1, asker="claude")
    assert got.verdict == "unchallenged" and "is the asker" in got.reason


def test_an_answer_the_asker_merely_joins_still_stands():
    """The rule is against a verdict resting on the asker, not against the asker
    having a vote — its own reading is worth exactly as much as anyone's when
    somebody else reached the same one."""
    got = panel.ask_tally(_answers(claude="fails", codex="fails", pi="cannot tell"),
                          quorum=2, threshold=1, asker="claude")
    assert got.verdict == "fails"


def test_a_seat_that_did_not_answer_is_in_no_count():
    """Skipped and unreadable seats are not abstentions in the tally's sense —
    they are absences, and the quorum is what notices them."""
    got = panel.ask_tally({"claude": panel.SeatAnswer("fails", "r"),
                           "codex": panel.SeatAnswer(skip="codex: CLI absent", absent=True),
                           "pi": panel.SeatAnswer(unreadable=True)},
                          quorum=2, threshold=1)
    assert got.verdict == "unchallenged" and got.answered == 1


def test_no_seat_answered_at_all():
    got = panel.ask_tally({}, quorum=2, threshold=2)
    assert got.verdict == "unchallenged" and "no seat answered" in got.reason


def test_a_split_that_reaches_the_threshold_both_ways_is_unresolved():
    """Only reachable with `ask_threshold: 1`, which is a repo asking for a lower
    bar — not for a tie to be broken by the order this function tests things in.

    And it says which unresolved it is. "Nobody reached the threshold" and "both
    answers did" are opposite states: the first is an unconvincing challenge, the
    second is a real disagreement between vendors and worth reading."""
    got = panel.ask_tally(_answers(claude="holds", codex="fails"), quorum=2,
                          threshold=1)
    assert got.verdict == "unresolved" and "both answers reached" in got.reason
    quiet = panel.ask_tally(_answers(claude="holds", codex="fails"), quorum=2,
                            threshold=2)
    assert quiet.verdict == "unresolved" and "no answer reached" in quiet.reason


# ---- the context ------------------------------------------------------------

@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.py").write_text("\n".join(f"line {i}" for i in range(1, 21)))
    (tmp_path / "odd:dir").mkdir()
    (tmp_path / "odd:dir" / "b.py").write_text("colon\n")
    return tmp_path


def test_a_whole_file_is_read(repo):
    problems = []
    got = panel.read_context(repo, ["sub/a.py"], problems)
    assert problems == [] and got[0].path == "sub/a.py"
    assert got[0].first is None and got[0].text.startswith("line 1\n")


def test_a_line_range_is_one_based_and_inclusive(repo):
    problems = []
    got = panel.read_context(repo, ["sub/a.py:3-5"], problems)
    assert problems == []
    assert got[0].text == "line 3\nline 4\nline 5"
    assert (got[0].first, got[0].last) == (3, 5)


def test_a_bare_line_number_is_that_one_line(repo):
    got = panel.read_context(repo, ["sub/a.py:7"], [])
    assert got[0].text == "line 7" and (got[0].first, got[0].last) == (7, 7)


def test_a_colon_inside_a_path_is_still_a_path(repo):
    got = panel.read_context(repo, ["odd:dir/b.py"], [])
    assert got[0].text == "colon\n"


def test_a_path_outside_the_repo_is_refused(repo, tmp_path):
    """The path comes off a command line an agent composes, and every seat's
    reply is a place its contents could come back out."""
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("s3cret")
    problems = []
    assert panel.read_context(repo, [f"../{outside.name}"], problems) == []
    assert "outside" in problems[0]


def test_an_absolute_path_is_refused(repo):
    problems = []
    assert panel.read_context(repo, ["/etc/hostname"], problems) == []
    assert "outside" in problems[0]


def test_a_symlink_out_of_the_repo_is_refused(repo, tmp_path):
    """A link inside the repo is not a file inside the repo — the same reason
    `write_payload` opens O_NOFOLLOW."""
    outside = tmp_path.parent / "linked.txt"
    outside.write_text("s3cret")
    (repo / "sub" / "link.txt").symlink_to(outside)
    problems = []
    assert panel.read_context(repo, ["sub/link.txt"], problems) == []
    assert "outside" in problems[0]


def test_the_read_walks_down_from_the_repo_and_follows_no_link(repo, monkeypatch):
    """Resolving a path and then opening it by that path are two traversals of
    one string, and between them any component can become a symlink out of the
    repo — the check passes and the read leaves. Each component is opened
    O_NOFOLLOW relative to the descriptor of the one above it, so the file read
    is the file checked or the open fails."""
    opens = []
    real = panel.os.open

    def watched(path, flags, *a, **kw):
        opens.append((path, bool(flags & panel.os.O_NOFOLLOW), "dir_fd" in kw))
        return real(path, flags, *a, **kw)

    monkeypatch.setattr(panel.os, "open", watched)
    got = panel.read_context(repo, ["sub/a.py:1-2"], [])
    assert got[0].text == "line 1\nline 2"
    # The root descriptor, then every component below it — no-follow and relative.
    assert opens[0][0] == repo and opens[0][1:] == (False, False)
    assert [o[0] for o in opens[1:]] == ["sub", "a.py"]
    assert all(nofollow and relative for _, nofollow, relative in opens[1:])


def test_the_walk_narrows_nothing_a_caller_can_reach_by_typing(repo):
    """A link inside the repo pointing inside the repo still reads its target:
    `resolve()` followed it before the containment test, so the walk sees only
    real directories and the path it records is the real one. The walk refusing a
    component means it changed AFTER it was checked — the race, and nothing else."""
    (repo / "alias").symlink_to(repo / "sub")
    problems = []
    got = panel.read_context(repo, ["alias/a.py:1"], problems)
    assert problems == [] and got[0].path == "sub/a.py" and got[0].text == "line 1"


def test_a_component_that_turns_into_a_link_after_the_check_is_refused(repo, monkeypatch):
    """The race itself, forced: the walk opens a component that was a directory
    when it was resolved and is a symlink by the time it is opened."""
    (repo / "elsewhere").mkdir()
    (repo / "elsewhere" / "a.py").write_text("someone else's file")
    real = panel.os.open
    swapped = []

    def watched(path, flags, *a, **kw):
        if path == "sub" and not swapped:
            swapped.append(True)
            (repo / "sub").rename(repo / "gone")
            (repo / "sub").symlink_to(repo / "elsewhere")
        return real(path, flags, *a, **kw)

    monkeypatch.setattr(panel.os, "open", watched)
    problems = []
    assert panel.read_context(repo, ["sub/a.py"], problems) == []
    assert "changed after it was checked" in problems[0]


@pytest.mark.parametrize("spec,says", [
    ("sub/nope.py", "not a file"),
    ("sub", "not a file"),
    ("sub/a.py:0-3", "numbered from 1"),
    ("sub/a.py:40", "has 20 lines"),
    ("sub/a.py:5-2", "ends before it starts"),
    (":3-5", "names no file"),
])
def test_an_unreadable_spec_is_a_problem_and_never_a_silent_omission(repo, spec, says):
    """A seat given less context than the asker believes it has answers
    `cannot tell` about a question the asker thinks it supplied the answer to —
    and the asker reads that as the code being unclear."""
    problems = []
    assert panel.read_context(repo, [spec], problems) == []
    assert says in problems[0]


def test_a_range_past_the_end_is_clamped_and_said(repo):
    """Usually a stale line number, and a seat answering from five lines where
    the asker meant sixty is exactly what this feature exists to make cheap to
    notice."""
    problems = []
    got = panel.read_context(repo, ["sub/a.py:18-99"], problems)
    assert got[0].last == 20 and got[0].text.endswith("line 20")
    assert "has 20 lines" in problems[0] and "18-20" in problems[0]


def test_no_context_says_so_in_the_prompt():
    """A model handed a bare assertion and nothing to check it against will
    answer from what it remembers. Saying it was given nothing is what makes
    `cannot tell` an available answer rather than a gap to invent across."""
    block = panel._context_block([])
    assert "None was given" in block and "cannot tell" in block


# ---- one seat's turn --------------------------------------------------------

def _seat(monkeypatch, *replies, err=None):
    """Stub the CLI so `run_seat` returns each reply in turn."""
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    seen = list(replies)
    monkeypatch.setattr(panel, "run_cli",
                        lambda *a, **k: (seen.pop(0) if seen else replies[-1], err))
    return seen


def test_a_seat_that_answers_reports_its_verdict_and_its_time(monkeypatch):
    _seat(monkeypatch, ANSWER)
    got = panel.ask_llm("claude", "opus", "p")
    assert (got.verdict, got.skip) == ("fails", None)
    assert got.reason.startswith("the skip branch") and got.duration_ms >= 0


def test_an_unreadable_reply_is_not_cannot_tell(monkeypatch):
    """The single most important line in this file. `cannot tell` is a seat's
    answer and counts toward the quorum; an unreadable reply is a seat we have no
    answer from, and counting it as the first would let a panel that could not
    read the code report as one that looked and was unsure."""
    _seat(monkeypatch, "I think the premise is probably fine, honestly")
    got = panel.ask_llm("claude", "opus", "p")
    assert got.verdict is None and got.unreadable is True and got.skip is None
    assert "probably fine" in got.reason


def test_an_unreadable_reply_buys_exactly_one_retry(monkeypatch):
    """The common flake is a prose preamble the model omits on a second pass —
    and the retry's reply is the one that counts."""
    calls = []
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")

    def fake(*a, **k):
        calls.append(1)
        return ("prose" if len(calls) == 1 else ANSWER), None

    monkeypatch.setattr(panel, "run_cli", fake)
    got = panel.ask_llm("claude", "opus", "p")
    assert got.verdict == "fails" and len(calls) == 2


def test_a_seat_this_box_does_not_carry_is_absent_not_silent(monkeypatch):
    monkeypatch.setattr(panel.shutil, "which", lambda _: None)
    got = panel.ask_llm("codex", "gpt-5.6-luna", "p")
    assert got.verdict is None and got.absent is True
    assert panel.CLI_ABSENT in got.skip


def test_a_seat_that_produced_nothing_is_a_skip_not_an_unreadable_reply(monkeypatch):
    """"said nothing" and "said something we could not read" are different
    accounts, and only the second is worth quoting back at whoever tunes the
    prompt."""
    _seat(monkeypatch, "", err="codex: timed out after 1800s")
    got = panel.ask_llm("claude", "opus", "p")
    assert got.unreadable is False and "timed out" in got.skip


# ---- run_seat serves both questions -----------------------------------------

def test_one_seat_implementation_serves_the_review_and_the_ask(monkeypatch):
    """The sandbox, the pinned sessions, the retry and the usage read-back are
    identical for both, and identical is what they have to stay: a second copy is
    a second place a seat can silently stop running (#68)."""
    _seat(monkeypatch, '[{"severity":"P2","file":"a.py","line":1,"title":"t","detail":"d"}]')
    assert len(panel.review_llm("claude", "opus", "p").findings) == 1
    _seat(monkeypatch, ANSWER)
    assert panel.ask_llm("claude", "opus", "p").verdict == "fails"


def test_run_seat_without_a_parser_never_retries(monkeypatch):
    calls = []
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(panel, "run_cli",
                        lambda *a, **k: (calls.append(1), ("prose", None))[1])
    turn = panel.run_seat("claude", "opus", "p")
    assert turn.reply == "prose" and turn.parsed is None and len(calls) == 1


# ---- who is asking ----------------------------------------------------------

def test_the_asker_is_detected_from_the_agent_running_it(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    assert panel.asking_seat(None) == "claude"


def test_an_explicit_asker_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    assert panel.asking_seat("CODEX") == "codex"


def test_an_explicitly_empty_asker_means_nobody(monkeypatch):
    """A person at a terminal, where there is no agent and so no self-challenge
    to guard against. It is the one hole in the rule, and it has to be typed."""
    monkeypatch.setenv("CLAUDECODE", "1")
    assert panel.asking_seat("") == ""


def test_no_agent_no_asker(monkeypatch):
    for var in panel.ASKER_ENV:
        monkeypatch.delenv(var, raising=False)
    assert panel.asking_seat(None) == ""


# ---- the tally rules as config ----------------------------------------------

def test_the_ask_rules_are_settable_and_default_to_two():
    panel_block = harness_rules.DEFAULTS["review_panel"]
    assert (panel_block["ask_quorum"], panel_block["ask_threshold"]) == (2, 2)
    assert harness_rules.unknown_keys(
        {"review_panel": {"ask_quorum": 3, "ask_threshold": 2}}) == {}


@pytest.mark.parametrize("value,says", [
    ("lots", "is not a number"),
    (0, "tally of nobody"),
    (-1, "tally of nobody"),
])
def test_a_rule_that_cannot_be_one_falls_back_and_says_so(value, says):
    notes = []
    assert panel._ask_rule({"ask_quorum": value}, "ask_quorum", 2, notes) == 2
    assert says in notes[0]


def test_a_rule_that_is_somebody_s_decision_is_honoured():
    notes = []
    assert panel._ask_rule({"ask_quorum": "3"}, "ask_quorum", 2, notes) == 3
    assert notes == []


# ---- end to end -------------------------------------------------------------

@pytest.fixture()
def cfg(monkeypatch, repo):
    """The repo's resolved config, pointed at the fixture tree — so these tests
    exercise `ask()` and not `git remote get-url`."""
    conf = copy.deepcopy(harness_rules.DEFAULTS)
    conf |= {"name": "demo", "github": "me/demo", "path": str(repo)}
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: conf)
    return conf


@pytest.fixture()
def asked(monkeypatch, cfg):
    """`ask()` over a repo of stub seats, returning (exit code, payload)."""
    monkeypatch.setattr(panel, "record_ask", lambda payload: None)

    def run(verdicts, out, **kw):
        monkeypatch.setattr(panel, "ask_llm", lambda name, model, prompt, effort="":
                            verdicts[name])
        code = panel.ask(cfg["path"], "the premise", ["sub/a.py:1-3"],
                         reviewers=",".join(verdicts), json_file=str(out), **kw)
        return code, json.loads(out.read_text())
    return run


def test_a_failing_premise_still_exits_zero(asked, tmp_path, capsys):
    """Not a gate. Making it one turns a one-minute question into a required
    wait, and a required wait gets skipped."""
    code, payload = asked(_answers(claude="fails", codex="fails"),
                          tmp_path / "ask.json")
    assert code == 0 and payload["verdict"] == "fails"
    assert "Not a gate" in capsys.readouterr().out


def test_the_payload_records_the_premise_the_context_and_every_seat(asked, tmp_path):
    """What makes a challenge auditable later: which premise, checked against
    what, by whom, and what each of them actually said."""
    code, payload = asked(_answers(claude="fails", codex="cannot tell"),
                          tmp_path / "ask.json", pr_number=62, asker="claude")
    assert code == 0 and payload["kind"] == "ask" and payload["pr"] == 62
    assert payload["premise"] == "the premise" and payload["asker"] == "claude"
    assert payload["context"] == [{"spec": "sub/a.py:1-3", "path": "sub/a.py",
                                   "first": 1, "last": 3, "chars": 20}]
    assert payload["counts"] == {"holds": 0, "fails": 1, "cannot tell": 1}
    assert payload["answers"]["codex"]["verdict"] == "cannot tell"
    assert (payload["quorum"], payload["threshold"], payload["answered"]) == (2, 2, 2)


def test_an_unchallenged_tally_is_never_shown_as_confirmation(asked, tmp_path, capsys):
    code, payload = asked({"claude": panel.SeatAnswer("holds", "sure")},
                          tmp_path / "ask.json", asker="claude")
    assert code == 0 and payload["verdict"] == "unchallenged"
    out = capsys.readouterr().out
    assert "UNCHALLENGED" in out and "nobody checked" in out


def test_an_unreadable_seat_is_shown_as_one(asked, tmp_path, capsys):
    code, payload = asked({"claude": panel.SeatAnswer("fails", "r"),
                           "codex": panel.SeatAnswer(unreadable=True, reason="waffle")},
                          tmp_path / "ask.json")
    assert code == 0
    out = capsys.readouterr().out
    assert "no verdict" in out and "NOT counted as `cannot tell`" in out
    assert payload["answers"]["codex"]["unreadable"] is True


def test_sonarqube_is_not_a_correspondent(monkeypatch, cfg, tmp_path):
    """Selectable for a review and meaningless here — said out loud, because
    `--reviewers claude,sonarqube` otherwise looks like a two-seat ask."""
    monkeypatch.setattr(panel, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel, "ask_llm", lambda *a, **k: panel.SeatAnswer("holds", "r"))
    out = tmp_path / "ask.json"
    panel.ask(cfg["path"], "p", [], reviewers="claude,sonarqube", json_file=str(out))
    payload = json.loads(out.read_text())
    assert list(payload["answers"]) == ["claude"]
    assert any("sonarqube cannot be asked" in n for n in payload["config_notes"])


def test_json_mode_puts_the_payload_on_stdout_and_nothing_else(monkeypatch, cfg, capsys):
    monkeypatch.setattr(panel, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel, "ask_llm", lambda *a, **k: panel.SeatAnswer("holds", "r"))
    panel.ask(cfg["path"], "p", [], reviewers="claude", json_out=True)
    assert json.loads(capsys.readouterr().out)["verdict"] == "unchallenged"


def test_an_unwritable_payload_fails_the_run(monkeypatch, cfg):
    """Same rule as a round's: the artefact IS the record, and a caller told it
    would get one must not be handed a 0 instead."""
    monkeypatch.setattr(panel, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel, "ask_llm", lambda *a, **k: panel.SeatAnswer("holds", "r"))
    code = panel.ask(cfg["path"], "p", [], reviewers="claude",
                     json_file=cfg["path"] + "/no/such/dir/x.json")
    assert code == panel.UNWRITTEN_PAYLOAD_EXIT


# ---- the command line -------------------------------------------------------

def _main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["panel.py", *argv])
    with pytest.raises(SystemExit) as e:
        panel.main()
    return str(e.value)


def test_a_review_still_requires_a_pr(monkeypatch):
    assert "--pr is required" in _main(monkeypatch, "--repo", ".")


def test_an_ask_needs_no_pr(monkeypatch, cfg):
    monkeypatch.setattr(panel, "ask", lambda *a, **k: 0)
    monkeypatch.setattr(sys, "argv", ["panel.py", "--repo", cfg["path"], "--ask", "p"])
    assert panel.main() == 0


def test_an_empty_premise_is_refused(monkeypatch):
    assert "the premise is empty" in _main(monkeypatch, "--ask", "   ")


@pytest.mark.parametrize("flag", [["--post"], ["--round", "2"], ["--max-rounds", "2"],
                                  ["--baseline", "r1.json"],
                                  # The round the default happens to be. Compared
                                  # against that default it was accepted silently,
                                  # which is a caller believing it asked for
                                  # something this run does not do.
                                  ["--round", "1"]])
def test_an_ask_takes_none_of_a_round_s_flags(monkeypatch, flag):
    """There is no diff to post about, no judge, and no cycle for a baseline to
    be part of — accepting them quietly would promise a round nothing will run."""
    said = _main(monkeypatch, "--ask", "p", *flag)
    assert flag[0] in said and "not a round" in said


@pytest.mark.parametrize("flag", [["--context", "a.py"], ["--asker", "claude"],
                                  ["--asker", ""]])
def test_the_ask_only_flags_are_refused_on_a_review(monkeypatch, flag):
    assert "belongs to --ask" in _main(monkeypatch, "--pr", "1", *flag)


def test_an_unknown_asker_is_refused(monkeypatch):
    assert "unknown seat" in _main(monkeypatch, "--ask", "p", "--asker", "gemini")


def test_the_asker_reaches_the_tally(monkeypatch, cfg):
    """The environment says which seat is running this, and that has to survive
    the whole way to the rule it exists for."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    monkeypatch.setattr(panel, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel, "ask_llm", lambda *a, **k: panel.SeatAnswer("holds", "r"))
    seen = {}
    monkeypatch.setattr(panel, "ask_tally",
                        lambda answers, quorum, threshold, asker="": (
                            seen.update(asker=asker),
                            panel.AskTally("unchallenged", "r", dict.fromkeys(
                                panel.ASK_VERDICTS, 0), 0))[1])
    monkeypatch.setattr(sys, "argv", ["panel.py", "--repo", cfg["path"], "--ask", "p",
                                      "--reviewers", "claude", "--no-record"])
    assert panel.main() == 0 and seen["asker"] == "claude"
