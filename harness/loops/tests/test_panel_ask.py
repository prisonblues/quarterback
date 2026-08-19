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
import panel_ask  # noqa: E402  — the ask path moved here in #129
import panel_seats  # noqa: E402  — run_cli lives here since #129

ANSWER = '{"verdict": "fails", "reason": "the skip branch returns finish(failed)"}'


@pytest.fixture(autouse=True)
def no_ambient_asker(monkeypatch):
    """These tests almost certainly run INSIDE a coding agent, which exports the
    very environment `asking_seat` reads. Cleared for every test, so a run under
    Claude Code and a run in CI ask the same question — the tests that care about
    detection set the variable themselves."""
    for var in panel.ASKER_ENV:
        monkeypatch.delenv(var, raising=False)


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


def test_two_verdicts_in_ONE_object_are_unreadable():
    """`json.loads` keeps the last of duplicate keys, silently — so a reply
    stating both `holds` and `fails` in one object was recorded as `fails` by a
    detail of the JSON parser. It is the same conflict two objects already get
    refused for, and nothing here picks."""
    assert panel.parse_answer('{"verdict": "holds", "verdict": "fails"}') is None


def test_a_reply_carrying_a_findings_array_is_not_an_answer():
    """The prompt says it in those words — "a reply carrying a findings array is
    an answer to a question nobody asked" — and the parser used to accept it
    anyway as long as a legal verdict sat beside it."""
    assert panel.parse_answer(
        '{"verdict": "holds", "findings": [{"severity": "P2"}]}') is None


def test_a_reason_is_one_line_and_bounded():
    reply = json.dumps({"verdict": "holds", "reason": "line one\n  line two\t" + "x" * 900})
    got = panel.parse_answer(reply)
    assert "\n" not in got.reason and got.reason.startswith("line one line two ")
    assert len(got.reason) == panel.ASK_REASON_CHARS
    # Ellipsised, like `_ask_gist`'s cut. A hard slice with no marker leaves a
    # reader of the report or the payload unable to tell a cut reason from a
    # complete one.
    assert got.reason.endswith("…")


def test_a_reason_that_is_not_a_string_is_rendered_and_not_dropped():
    """A model answering `{"verdict": "fails", "reason": ["line 10 is wrong"]}`
    has given its justification. Reading only `str` left the seat voting with no
    stated reason at all, and said nothing about having dropped it."""
    got = panel.parse_answer('{"verdict": "fails", "reason": ["line 10 is wrong", "and 11"]}')
    assert got == panel.Answer("fails", "line 10 is wrong and 11")
    assert panel.parse_answer('{"verdict": "fails", "reason": {"why": "x"}}').reason


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
    """It needs `len(seats) >= 2 * threshold` — NOT `ask_threshold: 1`, which is
    what the docstring beside the code used to claim. A four-seat panel on the
    default `ask_threshold: 2` reaches it on a two-against-two split, so this is the ordinary
    configuration and not a curiosity of a lowered bar. Either way the tie is not
    broken by the order this function tests things in.

    And it says which unresolved it is. "Nobody reached the threshold" and "both
    answers did" are opposite states: the first is an unconvincing challenge, the
    second is a real disagreement between vendors and worth reading."""
    got = panel.ask_tally(_answers(claude="holds", codex="fails"), quorum=2,
                          threshold=1)
    assert got.verdict == "unresolved" and "both answers reached" in got.reason
    quiet = panel.ask_tally(_answers(claude="holds", codex="fails"), quorum=2,
                            threshold=2)
    assert quiet.verdict == "unresolved" and "no answer reached" in quiet.reason


def test_the_default_configuration_can_reach_the_threshold_both_ways():
    """The case the docstring said was unreachable: four seats and the shipped
    `ask_quorum: 2` / `ask_threshold: 2`, split down the middle. A wrong reason
    recorded beside right code is what a future reader acts on."""
    block = harness_rules.DEFAULTS["review_panel"]
    got = panel.ask_tally(_answers(claude="holds", codex="holds",
                                   antigravity="fails", pi="fails"),
                          quorum=block["ask_quorum"], threshold=block["ask_threshold"])
    assert got.verdict == "unresolved" and "both answers reached" in got.reason


# ---- the context ------------------------------------------------------------

@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.py").write_text("\n".join(f"line {i}" for i in range(1, 21)))
    (tmp_path / "odd:dir").mkdir()
    (tmp_path / "odd:dir" / "b.py").write_text("colon\n")
    (tmp_path / "root.py").write_text("at the top\n")
    return tmp_path


def _said(problems) -> str:
    """The problems as one string. They are :class:`panel.ContextProblem` records
    rather than sentences, so that a consumer can answer "was this verdict
    reached with all the context the asker intended?" without matching prose."""
    return " | ".join(p.problem for p in problems)


def test_a_whole_file_is_read(repo):
    problems = []
    got = panel.read_context(repo, ["sub/a.py"], problems)
    assert problems == [] and got[0].path == "sub/a.py"
    assert got[0].first is None and got[0].text.startswith("line 1\n")


def test_a_file_at_the_repo_root_is_read(repo):
    """The single-component path, where the walk down from the root descriptor
    has no directories to descend through and the leaf is opened straight off the
    root. Every other test here goes through `sub/`, so this branch of
    `_read_confined` had nothing exercising it."""
    problems = []
    got = panel.read_context(repo, ["root.py"], problems)
    assert problems == [] and got[0].path == "root.py" and got[0].text == "at the top\n"


def test_a_line_range_is_one_based_and_inclusive(repo):
    problems = []
    got = panel.read_context(repo, ["sub/a.py:3-5"], problems)
    assert problems == []
    assert got[0].text == "line 3\nline 4\nline 5\n"
    assert (got[0].first, got[0].last) == (3, 5)


def test_a_range_is_a_substring_of_the_whole_file(repo):
    """`path` and `path:1-N` over the same N lines are the same text, to the
    character — so the payload's `chars` agrees with itself. Re-joining the
    lines instead dropped (or invented) the file's trailing newline, which is
    nothing to a seat and confusing to anyone diffing two payloads."""
    whole = panel.read_context(repo, ["sub/a.py"], [])[0]
    ranged = panel.read_context(repo, ["sub/a.py:1-20"], [])[0]
    assert ranged.text == whole.text


def test_a_bare_line_number_is_that_one_line(repo):
    got = panel.read_context(repo, ["sub/a.py:7"], [])
    assert got[0].text == "line 7\n" and (got[0].first, got[0].last) == (7, 7)


def test_a_colon_inside_a_path_is_still_a_path(repo):
    got = panel.read_context(repo, ["odd:dir/b.py"], [])
    assert got[0].text == "colon\n"


def test_a_file_whose_name_ends_in_a_line_range_is_still_that_file(repo):
    """`notes:12` is the file `notes:12` when that file is there. There is no
    escaping syntax — `./notes:12` does not help — so the filesystem breaks the
    tie, and the alternative was a file that could never be named at all."""
    (repo / "config:2024").write_text("the whole file\n")
    problems = []
    got = panel.read_context(repo, ["config:2024"], problems)
    assert problems == [] and got[0].path == "config:2024"
    assert got[0].first is None and got[0].text == "the whole file\n"


def test_a_range_wins_when_no_such_file_exists(repo):
    """The other half of the same tie-break: with no file called `sub/a.py:7`,
    the spec is line 7 of `sub/a.py` exactly as before."""
    got = panel.read_context(repo, ["sub/a.py:7"], [])
    assert got[0].path == "sub/a.py" and (got[0].first, got[0].last) == (7, 7)


def test_a_malformed_range_is_reported_as_a_range_and_not_as_a_path(repo):
    """`sub/a.py:abc` used to be reported as `sub/a.py:abc` not being a file —
    accurate, and pointing at the wrong half of what was typed."""
    problems = []
    assert panel.read_context(repo, ["sub/a.py:abc"], problems) == []
    assert "is not a line range" in _said(problems) and problems[0].spec == "sub/a.py:abc"


def test_a_range_of_absurdly_many_digits_is_refused_rather_than_raising(repo):
    """CPython refuses `int()` on a string of more than 4,300 digits, so an
    unbounded digit run in the pattern turned a nonsense spec into a traceback
    and no payload at all."""
    problems = []
    assert panel.read_context(repo, ["sub/a.py:" + "9" * 5000], problems) == []
    assert "is not a line range" in _said(problems)


def test_a_path_the_os_refuses_is_a_problem_and_not_a_traceback(repo):
    """A spec carrying a NUL makes `Path.resolve` raise ValueError, and a symlink
    loop makes it raise RuntimeError — neither is an OSError, and both arrive off
    a command line an agent composed."""
    problems = []
    assert panel.read_context(repo, ["sub/a\x00b.py"], problems) == []
    assert "could not be resolved" in _said(problems)


def test_a_path_outside_the_repo_is_refused(repo, tmp_path):
    """The path comes off a command line an agent composes, and every seat's
    reply is a place its contents could come back out."""
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("s3cret")
    problems = []
    assert panel.read_context(repo, [f"../{outside.name}"], problems) == []
    assert "outside" in _said(problems)


def test_an_absolute_path_is_refused(repo):
    problems = []
    assert panel.read_context(repo, ["/etc/hostname"], problems) == []
    assert "outside" in _said(problems)


def test_a_symlink_out_of_the_repo_is_refused(repo, tmp_path):
    """A link inside the repo is not a file inside the repo — the same reason
    `write_payload` opens O_NOFOLLOW."""
    outside = tmp_path.parent / "linked.txt"
    outside.write_text("s3cret")
    (repo / "sub" / "link.txt").symlink_to(outside)
    problems = []
    assert panel.read_context(repo, ["sub/link.txt"], problems) == []
    assert "outside" in _said(problems)


def test_the_repos_own_git_store_is_refused(repo):
    """Containment was the ONLY filter, and it is the wrong one on its own: the
    repo under review is where the credentials are. `.git/config` carries the
    access token on every https remote that was cloned with one, and an ask ships
    its context to four third-party CLIs."""
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text(
        "[remote \"origin\"]\n\turl = https://x-access-token:ghp_realtoken@github.com/me/r\n")
    problems = []
    assert panel.read_context(repo, [".git/config"], problems) == []
    assert "was refused" in _said(problems) and "`.git/`" in _said(problems)
    assert "access token" in _said(problems) and problems[0].spec == ".git/config"


@pytest.mark.parametrize("name", [".env", ".env.production", ".envrc", ".npmrc",
                                  ".netrc", ".pypirc", "id_ed25519", "deploy.pem",
                                  "server.key"])
def test_the_usual_secret_files_are_refused_and_say_why(repo, name):
    """A short denylist of names, not a secret scanner — it closes the routes an
    agent composing a `--context` actually types. A refusal is a stated problem
    like every other spec that did not become context, so a false positive costs
    one visible sentence."""
    (repo / name).write_text("SECRET_KEY=hunter2\n")
    problems = []
    assert panel.read_context(repo, [name], problems) == []
    assert "was refused" in _said(problems)


def test_a_file_that_merely_mentions_a_secret_is_still_read(repo):
    """The denylist is names and never content — claiming otherwise would be a
    scanner this is not, and refusing every file with `key` in it would make
    `--context` useless on the module that reads the config."""
    (repo / "sub" / "keys.py").write_text("API_KEY = os.environ['API_KEY']\n")
    problems = []
    got = panel.read_context(repo, ["sub/keys.py"], problems)
    assert problems == [] and got[0].path == "sub/keys.py"


def test_a_file_that_is_not_text_is_a_problem_and_not_a_wall_of_replacements(repo):
    """`errors="replace"` guaranteed the read SUCCEEDED, so `--context
    assets/logo.png` reached every seat's prompt as U+FFFD and the asker was
    never told."""
    (repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\xfd binary")
    problems = []
    assert panel.read_context(repo, ["logo.png"], problems) == []
    assert "not UTF-8 text" in _said(problems)


def test_a_text_file_carrying_nuls_is_a_problem_too(repo):
    (repo / "mixed.bin").write_bytes(b"looks like text\x00but is not")
    problems = []
    assert panel.read_context(repo, ["mixed.bin"], problems) == []
    assert "NUL bytes" in _said(problems)


def test_a_file_past_the_read_ceiling_is_refused_rather_than_read(repo, monkeypatch):
    """Bounds what is materialised in memory, which is a different cost from the
    char budget's: that one bounds what the seats are sent and paid for."""
    monkeypatch.setattr(panel_ask, "ASK_CONTEXT_FILE_MAX_BYTES", 64)
    (repo / "huge.py").write_text("x" * 500)
    problems = []
    assert panel.read_context(repo, ["huge.py"], problems) == []
    assert "larger than an ask will read" in _said(problems)


def test_the_same_spec_twice_is_read_once(repo):
    """Two identical sections in every seat's prompt is tokens spent on nothing,
    in the one feature whose whole argument is that it is cheap."""
    problems = []
    got = panel.read_context(repo, ["sub/a.py", "sub/a.py"], problems)
    assert len(got) == 1 and problems == []


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
    assert got[0].text == "line 1\nline 2\n"
    # The root descriptor, then every component below it. The root is opened
    # no-follow too — it is the step this used to skip, and a repo root replaced
    # between `resolve()` and here redirected the whole walk out of the tree that
    # had just been checked.
    assert opens[0][0] == repo and opens[0][1:] == (True, False)
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
    assert problems == [] and got[0].path == "sub/a.py" and got[0].text == "line 1\n"


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
    assert "changed after it was checked" in _said(problems)


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
    assert says in _said(problems) and problems[0].spec == spec


def test_a_missing_path_says_where_paths_are_anchored(repo):
    """The plausible mistake is an agent running this from `harness/loops/` and
    typing `panel.py`. "not a file in /repo" is accurate and unhelpful about
    why."""
    problems = []
    assert panel.read_context(repo, ["nope.py"], problems) == []
    assert "relative to the repo root" in _said(problems)


def test_a_range_past_the_end_is_clamped_and_said(repo):
    """Usually a stale line number, and a seat answering from five lines where
    the asker meant sixty is exactly what this feature exists to make cheap to
    notice."""
    problems = []
    got = panel.read_context(repo, ["sub/a.py:18-99"], problems)
    assert got[0].last == 20 and got[0].text.endswith("line 20")
    assert "has 20 lines" in _said(problems) and "18-20" in _said(problems)


# ---- the context budget ------------------------------------------------------

def test_the_context_budget_clamps_and_says_which_spec_it_cut(repo):
    """`--context` had no ceiling at all, and an ask's entire claim on anyone's
    attention is that it is the cheap check — one spec naming a generated file
    shipped a multi-megabyte prompt to every vendor on the panel."""
    problems = []
    got = panel.read_context(repo, ["sub/a.py"], problems, budget=30)
    assert len(got[0].text) == 30
    assert "the seats got 30 of" in _said(problems)
    assert "ask_max_context_chars" in _said(problems) and problems[0].spec == "sub/a.py"


def test_the_budget_is_a_total_across_every_spec(repo):
    """Per-spec would be no ceiling at all: `--context` is repeatable."""
    problems = []
    got = panel.read_context(repo, ["sub/a.py", "odd:dir/b.py"], problems, budget=30)
    assert [len(c.text) for c in got] == [30]
    assert "was spent by the specs before it" in _said(problems)
    assert problems[-1].spec == "odd:dir/b.py"


def test_a_clamped_range_reports_the_lines_the_seats_actually_saw(repo):
    """`_budgeted` used to replace only the text, so `sub/a.py:1-20` cut to 20
    chars still serialised `{"first": 1, "last": 20}` and rendered as
    `sub/a.py:1-20`. The payload then held two records disagreeing about what was
    read, and the wide one is the one an audit answers from."""
    problems = []
    got = panel.read_context(repo, ["sub/a.py:1-20"], problems, budget=20)
    assert len(got[0].text) == 20
    # 20 chars of "line 1\nline 2\n…" is lines 1-3, the third of them partly.
    assert (got[0].first, got[0].last) == (1, 3)
    assert got[0].text.count("\n") == 2
    assert "the seats got 20 of" in _said(problems)


def test_a_clamped_whole_file_keeps_no_range_at_all(repo):
    """Nothing to correct: a spec with no range asked for none, and inventing one
    here would claim a precision the clamp does not have."""
    got = panel.read_context(repo, ["sub/a.py"], [], budget=20)
    assert (got[0].first, got[0].last) == (None, None) and len(got[0].text) == 20


def test_an_unbudgeted_read_is_the_whole_file(repo):
    got = panel.read_context(repo, ["sub/a.py"], [], budget=None)
    assert got[0].text == (repo / "sub" / "a.py").read_text()


def test_no_context_says_so_in_the_prompt():
    """A model handed a bare assertion and nothing to check it against will
    answer from what it remembers. Saying it was given nothing is what makes
    `cannot tell` an available answer rather than a gap to invent across."""
    block = panel._context_block([])
    assert "None was given" in block and "cannot tell" in block


def test_a_clamped_block_never_cuts_a_delimiter_in_half(repo):
    """The argv clamp slices the file CONTENT section by section, never the
    assembled block: slicing the finished string is how a seat gets a prompt
    whose last section has half a `--- CONTEXT: … ---` header on it."""
    read = panel.read_context(repo, ["sub/a.py", "odd:dir/b.py"], [])
    block = panel._context_block(read, budget=10)
    assert "--- CONTEXT: sub/a.py ---\nline 1\nlin\n" in block
    # The second section had nothing left to carry, so it is not announced at all.
    assert "odd:dir/b.py" not in block
    for line in block.splitlines():
        assert not line.startswith("--- CONTEXT") or line.endswith("---")


@pytest.mark.parametrize("budget", [0, -1])
def test_a_budget_that_leaves_nothing_still_says_there_is_nothing(repo, budget):
    """It returned "" — no header, no sentence — so the prompt ended straight
    after `--- PREMISE ---`. The seat was neither given material nor told there
    was none, which is the one condition that invites an answer from memory."""
    read = panel.read_context(repo, ["sub/a.py"], [])
    assert panel._context_block(read, budget) == panel._context_block([])
    assert "None was given" in panel._context_block(read, budget)


# ---- one seat's turn --------------------------------------------------------

def _seat(monkeypatch, *replies, err=None):
    """Stub the CLI so `run_seat` returns each reply in turn."""
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    seen = list(replies)
    monkeypatch.setattr(panel_seats, "run_cli",
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
    # In `gist`, and NOT in `reason`: a quote of what the seat said is not the
    # seat stating a reason, and one key carrying both is how a rambling preamble
    # gets rendered as a justification by anything reading `reason` alone.
    assert "probably fine" in got.gist and got.reason == ""


def test_an_unreadable_reply_buys_exactly_one_retry(monkeypatch):
    """The common flake is a prose preamble the model omits on a second pass —
    and the retry's reply is the one that counts."""
    calls = []
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")

    def fake(*a, **k):
        calls.append(1)
        return ("prose" if len(calls) == 1 else ANSWER), None

    monkeypatch.setattr(panel_seats, "run_cli", fake)
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
    prompt.

    `err=None` deliberately: with an error string the seat returns on the CLI's
    own failure path and this branch — the one that reads an EMPTY reply from a
    process that exited cleanly — is never reached. codex gets here by its
    reply-file route, where stdout is non-empty and the reply file is not."""
    _seat(monkeypatch, "   ")
    got = panel.ask_llm("claude", "opus", "p")
    assert got.unreadable is False and got.verdict is None
    assert "produced no output" in got.skip


def test_a_seat_whose_cli_failed_is_a_skip_carrying_the_reason(monkeypatch):
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


def test_a_review_neither_attempt_could_read_is_kept_as_an_unstructured_finding(monkeypatch):
    """The review path's half of the shared seat, and the reason the two questions
    can share one: an ask has nothing to keep from an unreadable reply, while a
    round keeps the raw text as one finding for the judge — half a review is still
    worth reading. Moving `run_seat` out from under `review_llm` had to preserve
    that exactly, and nothing was pinning it."""
    _seat(monkeypatch, "Here are my thoughts, in prose:", "still prose, no JSON")
    got = panel.review_llm("claude", "opus", "p")
    assert got.unstructured is True and got.skip is None
    assert len(got.findings) == 1 and got.findings[0].reviewer == "claude"
    # The RETRY's text, matching run_cli, which returns the last attempt's stdout.
    assert "still prose" in got.findings[0].detail
    # Nothing it might have declared survived the parse, so a quiet round holding
    # one of these is not evidence of a quiet PR.
    assert got.could_not_assess is None


def test_a_review_that_produced_nothing_is_a_skip_and_not_a_blank_finding(monkeypatch):
    """"said nothing" and "said something we could not read" are different
    accounts on this path too — and a blank finding flagged `unstructured` is a
    dead reviewer wearing a live one's clothes, which is the whole of #68."""
    _seat(monkeypatch, "   ")
    got = panel.review_llm("claude", "opus", "p")
    assert got.findings == [] and got.unstructured is False
    assert "produced no output" in got.skip


def test_run_seat_without_a_parser_never_retries(monkeypatch):
    calls = []
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(panel_seats, "run_cli",
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
        {"review_panel": {"ask_quorum": 3, "ask_threshold": 2,
                          "ask_max_context_chars": 10_000}}) == {}


def test_a_rule_falls_back_to_the_declared_default_and_not_to_a_copy_of_it():
    """One source of truth for each number. Passing the fallback in meant every
    call site spelled it a second time, so a default changed where it is declared
    and documented would go on being ignored where it is applied."""
    notes = []
    for key, want in harness_rules.DEFAULTS["review_panel"].items():
        if key.startswith("ask_"):
            assert panel._ask_rule({}, key, notes) == want
    assert notes == []


@pytest.mark.parametrize("value,says", [
    ("lots", "is not a number"),
    (0, "tally of nobody"),
    (-1, "tally of nobody"),
])
def test_a_rule_that_cannot_be_one_falls_back_and_says_so(value, says):
    notes = []
    assert panel._ask_rule({"ask_quorum": value}, "ask_quorum", notes) == 2
    assert says in notes[0]


def test_a_rule_that_is_somebody_s_decision_is_honoured():
    notes = []
    assert panel._ask_rule({"ask_quorum": "3"}, "ask_quorum", notes) == 3
    assert notes == []


# ---- recording it on the board ----------------------------------------------

def _qb(monkeypatch, returncode=0, stdout="", stderr="", raises=None):
    """Stub `qb record-ask`, which every end-to-end fixture here monkeypatches
    away — so nothing exercised the subprocess call, the exit codes, or the
    branch the CHANGELOG and both READMEs promise ("says so once" on a host whose
    `qb` predates the ask)."""
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/qb")
    seen = {}

    def fake(args, **kw):
        seen["args"], seen["input"] = args, kw.get("input")
        if raises:
            raise raises
        return type("P", (), {"returncode": returncode, "stdout": stdout,
                              "stderr": stderr})()

    monkeypatch.setattr(panel.subprocess, "run", fake)
    return seen


def test_the_ask_is_piped_to_qb_record_ask_on_stdin(monkeypatch, capsys):
    seen = _qb(monkeypatch, stdout="recorded")
    panel.record_ask({"kind": "ask", "premise": "p"})
    assert seen["args"] == ["qb", "record-ask"]
    assert json.loads(seen["input"])["premise"] == "p"
    assert "recorded" in capsys.readouterr().err


def test_a_qb_without_the_subcommand_says_so_and_hedges(monkeypatch, capsys):
    """Exit 2 is a HINT, not a diagnosis: `qb` also exits 2 on a payload it
    cannot read and on argument validation, so a confident sentence about a
    program in another repo is how a real error stays invisible."""
    _qb(monkeypatch, returncode=panel.QB_NO_SUBCOMMAND, stderr="usage: qb ...")
    panel.record_ask({"kind": "ask"})
    err = capsys.readouterr().err
    assert "not recorded" in err and "most likely" in err
    assert "usage: qb" in err and "complete either way" in err


def test_any_other_failure_to_record_is_reported_rather_than_silent(monkeypatch, capsys):
    """A recorder that fails with no output was indistinguishable from one that
    worked."""
    _qb(monkeypatch, returncode=1, stderr="board refused the row")
    panel.record_ask({"kind": "ask"})
    err = capsys.readouterr().err
    assert "exited 1" in err and "board refused the row" in err


def test_a_host_without_qb_says_so_and_the_ask_is_untouched(monkeypatch, capsys):
    monkeypatch.setattr(panel.shutil, "which", lambda _: None)
    panel.record_ask({"kind": "ask"})
    assert "no `qb` on this host" in capsys.readouterr().err


def test_recording_never_raises(monkeypatch, capsys):
    _qb(monkeypatch, raises=OSError("nope"))
    panel.record_ask({"kind": "ask"})
    assert "not recorded (OSError)" in capsys.readouterr().err


# ---- end to end -------------------------------------------------------------

@pytest.fixture()
def cfg(monkeypatch, repo):
    """The repo's resolved config, pointed at the fixture tree — so these tests
    exercise `ask()` and not `git remote get-url`."""
    conf = copy.deepcopy(harness_rules.DEFAULTS)
    # `_rules_baseline` because `ask()` refuses a repo whose rules nobody wrote
    # (#238-user): the field is `resolve_repo`'s statement of WHICH file supplied the
    # baseline, and a double of that function owes its consumers the same statement.
    conf |= {"name": "demo", "github": "me/demo", "path": str(repo),
             "_rules_baseline": harness_rules.SAMPLE_FILENAME}
    monkeypatch.setattr(panel_ask, "load_repo_cfg", lambda name: conf)
    return conf


@pytest.fixture()
def asked(monkeypatch, cfg):
    """`ask()` over a repo of stub seats, returning (exit code, payload)."""
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)

    def run(verdicts, out, **kw):
        monkeypatch.setattr(panel_ask, "ask_llm", lambda name, model, prompt, effort="":
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
                                   "first": 1, "last": 3, "chars": 21}]
    assert payload["context_problems"] == []
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
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm", lambda *a, **k: panel.SeatAnswer("holds", "r"))
    out = tmp_path / "ask.json"
    panel.ask(cfg["path"], "p", [], reviewers="claude,sonarqube", json_file=str(out))
    payload = json.loads(out.read_text())
    assert list(payload["answers"]) == ["claude"]
    assert any("sonarqube cannot be asked" in n for n in payload["config_notes"])


def test_json_mode_puts_the_payload_on_stdout_and_nothing_else(monkeypatch, cfg, capsys):
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm", lambda *a, **k: panel.SeatAnswer("holds", "r"))
    panel.ask(cfg["path"], "p", [], reviewers="claude", json_out=True)
    assert json.loads(capsys.readouterr().out)["verdict"] == "unchallenged"


def test_an_unwritable_payload_fails_the_run(monkeypatch, cfg):
    """Same rule as a round's: the artefact IS the record, and a caller told it
    would get one must not be handed a 0 instead."""
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm", lambda *a, **k: panel.SeatAnswer("holds", "r"))
    code = panel.ask(cfg["path"], "p", [], reviewers="claude",
                     json_file=cfg["path"] + "/no/such/dir/x.json")
    assert code == panel.UNWRITTEN_PAYLOAD_EXIT


def test_a_run_whose_payload_could_not_be_written_is_not_recorded(monkeypatch, cfg):
    """The run is about to exit non-zero. A board row for it would be two records
    disagreeing about whether this ask happened."""
    recorded = []
    monkeypatch.setattr(panel_ask, "record_ask", recorded.append)
    monkeypatch.setattr(panel_ask, "ask_llm", lambda *a, **k: panel.SeatAnswer("holds", "r"))
    code = panel.ask(cfg["path"], "p", [], reviewers="claude",
                     json_file=cfg["path"] + "/no/such/dir/x.json")
    assert code == panel.UNWRITTEN_PAYLOAD_EXIT and recorded == []


def test_a_seat_that_raises_does_not_take_the_ask_down(monkeypatch, cfg, tmp_path, capsys):
    """`run_seat` does filesystem work — a sandbox, temp dirs, an `os.open` — and
    ENOSPC or a permission error on any of it raises outside the err-string path.
    Re-raised out of the futures it discarded every other seat's finished answer,
    the tally, the payload and the --json-file, and handed the caller a traceback
    where the documented exit-0 report should be."""
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)

    def flaky(name, *a, **k):
        if name == "codex":
            raise OSError("No space left on device")
        return panel.SeatAnswer("fails", "the branch returns 0")

    monkeypatch.setattr(panel_ask, "ask_llm", flaky)
    out = tmp_path / "ask.json"
    code = panel.ask(cfg["path"], "p", [], reviewers="claude,codex",
                     json_file=str(out), asker="")
    payload = json.loads(out.read_text())
    assert code == 0 and payload["answers"]["claude"]["verdict"] == "fails"
    assert "OSError" in payload["answers"]["codex"]["skip"]
    assert payload["answered"] == 1
    assert "did not answer" in capsys.readouterr().out


def test_context_problems_are_not_config_problems(asked, monkeypatch, cfg, tmp_path, capsys):
    """A reader told that a missing file is a "config" problem goes looking for a
    key that does not exist, and #77's reader of `config_notes` could not tell
    "the repo is misconfigured" from "the asker's context never got read"."""
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm", lambda *a, **k: panel.SeatAnswer("holds", "r"))
    out = tmp_path / "ask.json"
    panel.ask(cfg["path"], "p", ["sub/nope.py", "sub/a.py:40"], reviewers="claude,codex",
              json_file=str(out), asker="")
    payload = json.loads(out.read_text())
    assert [p["spec"] for p in payload["context_problems"]] == ["sub/nope.py", "sub/a.py:40"]
    assert not any("nope.py" in n for n in payload["config_notes"])
    assert "⚠️ context:" in capsys.readouterr().out


def test_a_rule_no_seat_count_can_satisfy_is_the_one_that_is_warned_about(
        monkeypatch, cfg, tmp_path):
    """Quorum is a MINIMUM, so `ask_threshold` above `ask_quorum` is satisfiable
    and the warning that used to fire on it named an invariant that is not one.
    What can never be met is a rule above the number of seats: the ask runs and
    is paid for, then reports `unchallenged` however emphatic the seats were."""
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm", lambda *a, **k: panel.SeatAnswer("holds", "r"))
    out = tmp_path / "ask.json"
    panel.ask(cfg["path"], "p", [], reviewers="claude", json_file=str(out), asker="")
    payload = json.loads(out.read_text())
    assert payload["verdict"] == "unchallenged"
    assert any("above the 1 seat on this ask" in n for n in payload["config_notes"])


def test_a_threshold_above_the_quorum_is_not_warned_about(monkeypatch, cfg, tmp_path):
    """Three agreeing seats reach `ask_threshold: 3` under `ask_quorum: 2`, so
    this configuration works and a warning about it trains readers to ignore
    warnings."""
    cfg["review_panel"] |= {"ask_quorum": 2, "ask_threshold": 3}
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm", lambda *a, **k: panel.SeatAnswer("holds", "r"))
    out = tmp_path / "ask.json"
    panel.ask(cfg["path"], "p", [], reviewers="claude,codex,pi",
              json_file=str(out), asker="")
    payload = json.loads(out.read_text())
    assert payload["verdict"] == "holds" and payload["config_notes"] == []


def test_sonarqube_enabled_for_reviews_does_not_warn_on_every_ask(monkeypatch, cfg, tmp_path):
    """The note's own stated purpose — `--reviewers claude,sonarqube` otherwise
    looks like a two-seat ask — only argues for firing when it was ASKED for.
    On the resolved set it was a permanent warning about a seat nobody tried."""
    cfg["reviewers"]["sonarqube"] = dict(cfg["reviewers"].get("sonarqube", {}), enabled=True)
    cfg["reviewers"]["claude"] = dict(cfg["reviewers"].get("claude", {}), enabled=True)
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm", lambda *a, **k: panel.SeatAnswer("holds", "r"))
    out = tmp_path / "ask.json"
    panel.ask(cfg["path"], "p", [], json_file=str(out), asker="")
    payload = json.loads(out.read_text())
    assert not any("sonarqube" in n for n in payload["config_notes"])


def test_an_agent_no_env_marker_names_is_told_the_guard_is_off(monkeypatch, cfg, tmp_path):
    """`ASKER_ENV` is Claude Code's environment and only Claude Code's, so a
    codex- or pi-driven agent got `asker=""` and the headline safety rule
    silently did not fire — with nothing in the report or the payload saying
    detection had found nothing."""
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm", lambda *a, **k: panel.SeatAnswer("holds", "r"))
    out = tmp_path / "ask.json"
    panel.ask(cfg["path"], "p", [], reviewers="claude,codex", json_file=str(out))
    payload = json.loads(out.read_text())
    assert payload["asker"] is None
    assert any("no asker was detected" in n for n in payload["config_notes"])


def test_turning_the_guard_off_by_hand_while_an_agent_is_running_is_said(
        monkeypatch, cfg, tmp_path):
    """`--asker ''` is the one hole in the rule and it stays open — a person at a
    terminal must be able to turn off a rule that does not apply to them. It is
    no longer usable QUIETLY by an agent."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm", lambda *a, **k: panel.SeatAnswer("holds", "r"))
    out = tmp_path / "ask.json"
    panel.ask(cfg["path"], "p", [], reviewers="claude,codex", json_file=str(out), asker="")
    payload = json.loads(out.read_text())
    assert payload["verdict"] == "holds"
    assert any("guard is off by request" in n for n in payload["config_notes"])


def test_a_caller_that_is_not_the_command_line_still_gets_the_guard(monkeypatch, cfg,
                                                                   tmp_path):
    """`ask`'s asker used to default to "" — no asker, guard off — so every
    caller but `main()` silently lost the one rule this feature is built on."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm", lambda *a, **k: panel.SeatAnswer("holds", "sure"))
    out = tmp_path / "ask.json"
    panel.ask(cfg["path"], "p", [], reviewers="claude", json_file=str(out))
    payload = json.loads(out.read_text())
    assert payload["asker"] == "claude" and payload["verdict"] == "unchallenged"


@pytest.mark.parametrize("spelled", ["Claude", "CLAUDE", "claude "])
def test_however_a_caller_spells_the_asker_the_guard_still_fires(monkeypatch, cfg,
                                                                 tmp_path, spelled):
    """The SECOND hole found in this one guard. `main()` normalised `--asker` and
    `ask()` did not, so a skill or a loop passing `"Claude"` compared a
    lower-cased seat key against a string it could never equal — and a premise an
    agent put to itself came back `holds` with a panel's authority. Normalised at
    the single point an asker enters `ask()`, so no spelling can lose it."""
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm", lambda *a, **k: panel.SeatAnswer("holds", "sure"))
    out = tmp_path / "ask.json"
    panel.ask(cfg["path"], "p", [], reviewers="claude", json_file=str(out), asker=spelled)
    payload = json.loads(out.read_text())
    assert payload["asker"] == "claude" and payload["verdict"] == "unchallenged"


def test_an_asker_no_seat_answers_to_is_refused_and_said(monkeypatch, cfg, tmp_path):
    """`"claude-code"` is not a seat, so it can never match a vote — a guard that
    cannot fire. Carrying it silently is what made the hole invisible; it is
    recorded as no asker and the run says so."""
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm", lambda *a, **k: panel.SeatAnswer("holds", "sure"))
    out = tmp_path / "ask.json"
    panel.ask(cfg["path"], "p", [], reviewers="claude", json_file=str(out),
              asker="claude-code")
    payload = json.loads(out.read_text())
    assert payload["asker"] is None
    assert any("is not one of" in n and "guard is inactive" in n
               for n in payload["config_notes"])


def test_the_asker_that_is_not_a_seat_claims_no_vote(monkeypatch, cfg, capsys):
    """`--reviewers codex --asker claude` asserted "its own answer is one vote
    and cannot be the only one" of a seat with no vote at all on this ask."""
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm", lambda *a, **k: panel.SeatAnswer("holds", "r"))
    panel.ask(cfg["path"], "p", [], reviewers="codex", asker="claude")
    out = capsys.readouterr().out
    assert "**Asked by:** claude — not a seat on this ask" in out


def test_the_heading_separates_the_repo_from_the_pr(monkeypatch, cfg, capsys):
    """"Premise challenge — demo#62" reads as one token."""
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm", lambda *a, **k: panel.SeatAnswer("holds", "r"))
    panel.ask(cfg["path"], "p", [], reviewers="claude", pr_number=62, asker="")
    assert "## Premise challenge — demo, PR #62" in capsys.readouterr().out


def test_telemetry_can_never_overwrite_what_a_seat_answered(monkeypatch, cfg, tmp_path):
    """A usage key colliding with a primary field silently replaced the seat's
    actual answer, because usage was unpacked last."""
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm", lambda *a, **k: panel.SeatAnswer(
        "fails", "the branch returns 0",
        usage={"model": "some-telemetry-model", "verdict": "holds", "input_tokens": 12}))
    out = tmp_path / "ask.json"
    panel.ask(cfg["path"], "p", [], reviewers="claude", json_file=str(out), asker="")
    seat = json.loads(out.read_text())["answers"]["claude"]
    assert seat["verdict"] == "fails" and seat["model"] != "some-telemetry-model"
    assert seat["input_tokens"] == 12


def test_an_unreadable_reply_keeps_its_quote_out_of_the_reason(monkeypatch, cfg, tmp_path):
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm", lambda *a, **k: panel.SeatAnswer(
        unreadable=True, gist="I think it is probably fine"))
    out = tmp_path / "ask.json"
    panel.ask(cfg["path"], "p", [], reviewers="claude", json_file=str(out), asker="")
    seat = json.loads(out.read_text())["answers"]["claude"]
    assert seat["gist"] == "I think it is probably fine" and seat["reason"] == ""


def test_the_seat_whose_prompt_travels_in_argv_is_clamped_and_said(monkeypatch, cfg,
                                                                   tmp_path, repo):
    """`agy` has no stdin path, so its prompt goes in one argv element and the
    kernel caps that at MAX_ARG_STRLEN whatever is in it. Nothing exercised the
    clamp, which is how a seat could get an unbounded prompt and die at execve
    with an opaque error."""
    (repo / "big.py").write_text("z" * 4_000)
    monkeypatch.setattr(panel_seats, "ARGV_PROMPT_MAX_BYTES", 2_000)
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    prompts = {}

    def seat(name, model, prompt, effort=""):
        prompts[name] = prompt
        return panel.SeatAnswer("holds", "r")

    monkeypatch.setattr(panel_ask, "ask_llm", seat)
    out = tmp_path / "ask.json"
    panel.ask(cfg["path"], "p", ["big.py"], reviewers="antigravity,claude",
              json_file=str(out), asker="")
    payload = json.loads(out.read_text())
    assert len(prompts["antigravity"].encode()) <= 2_000
    assert len(prompts["claude"]) > len(prompts["antigravity"])
    assert any("antigravity gets" in n and "4,000 context chars" in n
               for n in payload["config_notes"])
    # Cut through the content, never through a delimiter.
    assert "--- CONTEXT: big.py ---" in prompts["antigravity"]


def test_a_prompt_that_cannot_fit_argv_at_all_skips_the_seat_rather_than_execve(
        monkeypatch, cfg, tmp_path):
    """The fitting only takes CONTEXT out, and the premise and the template have
    no budget — so a long premise leaves a prompt over the ceiling with nothing
    left to cut. `fit_argv_budget` returning 0 is not the same claim as "it
    fits", and the oversized argv used to go to execve and die there with an
    opaque error and no note at all."""
    monkeypatch.setattr(panel_ask, "ARGV_PROMPT_MAX_BYTES", 200)
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    ran = []

    def seat(name, model, prompt, effort=""):
        ran.append(name)
        return panel.SeatAnswer("holds", "r")

    monkeypatch.setattr(panel_ask, "ask_llm", seat)
    out = tmp_path / "ask.json"
    panel.ask(cfg["path"], "q" * 500, [], reviewers="antigravity,claude",
              json_file=str(out), asker="")
    # Not run at all: a CLI invocation on a prompt known not to survive exec.
    assert ran == ["claude"]
    seat_row = json.loads(out.read_text())["answers"]["antigravity"]
    assert seat_row["verdict"] is None and "argv ceiling" in seat_row["skip"]


def test_the_seat_that_could_not_be_run_is_shown_as_one(monkeypatch, cfg, capsys):
    """A skip is the panel's idiom for a seat that did not run, and the report
    shows it beside the seats that did rather than quietly dropping the column."""
    monkeypatch.setattr(panel_ask, "ARGV_PROMPT_MAX_BYTES", 200)
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm", lambda *a, **k: panel.SeatAnswer("holds", "r"))
    panel.ask(cfg["path"], "q" * 500, [], reviewers="antigravity", asker="")
    out = capsys.readouterr().out
    assert "did not answer" in out and "argv ceiling" in out
    # It could not be run, so nothing was challenged.
    assert "UNCHALLENGED" in out


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


@pytest.mark.parametrize("pr", ["0", "-5"])
def test_a_pr_number_is_validated_at_the_edge(monkeypatch, pr):
    """An ask fetches nothing for `--pr`, so nothing else ever looks at it —
    `"pr": -5` went into the payload as a link for the board to render."""
    assert "numbered from 1" in _main(monkeypatch, "--ask", "p", "--pr", pr)
    assert "numbered from 1" in _main(monkeypatch, "--pr", pr)


def test_the_asker_reaches_the_tally(monkeypatch, cfg):
    """The environment says which seat is running this, and that has to survive
    the whole way to the rule it exists for."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm", lambda *a, **k: panel.SeatAnswer("holds", "r"))
    seen = {}
    monkeypatch.setattr(panel_ask, "ask_tally",
                        lambda answers, quorum, threshold, asker="": (
                            seen.update(asker=asker),
                            panel.AskTally("unchallenged", "r", dict.fromkeys(
                                panel.ASK_VERDICTS, 0), 0))[1])
    monkeypatch.setattr(sys, "argv", ["panel.py", "--repo", cfg["path"], "--ask", "p",
                                      "--reviewers", "claude", "--no-record"])
    assert panel.main() == 0 and seen["asker"] == "claude"


# --------------------------------- a repo nobody configured is not asked either

def test_an_unconfigured_repo_is_REFUSED_rather_than_asked(monkeypatch, capsys,
                                                           tmp_path, repo):
    """The same gate as the review path, through the same predicate.

    An ask is cheaper than a round and it is not less configured: which seats answer,
    on which models, at which effort, and how many of them make a verdict
    (`ask_quorum`/`ask_threshold`) all come out of the rules file. A repo with none
    gets a tally struck by a panel nobody chose — and the whole standing of an ask is
    that it is evidence about a premise."""
    conf = copy.deepcopy(harness_rules.DEFAULTS)
    conf |= {"name": "stranger", "github": "me/demo", "path": str(repo),
             "_rules_baseline": "", "_rules_from": "none (defaults)"}
    monkeypatch.setattr(panel_ask, "load_repo_cfg", lambda name: conf)
    monkeypatch.setattr(panel_ask, "record_ask",
                        lambda payload: pytest.fail("a refused ask is not recorded"))
    monkeypatch.setattr(panel_ask, "ask_llm", lambda *a, **k: pytest.fail(
        "no seat may be called for a repo whose rules nobody wrote"))
    out = tmp_path / "ask.json"
    assert panel.ask(str(repo), "the premise", json_file=str(out)) == 0
    payload = json.loads(out.read_text())
    assert payload["reviewed"] is False and payload["answers"] == {}
    # Null rather than one of the four verdicts: `unchallenged` means the seats
    # answered and it did not resolve, and nothing here was asked.
    assert payload["verdict"] is None
    assert harness_rules.SAMPLE_FILENAME in payload["skip_reason"]
    assert "refusing to ask" in capsys.readouterr().out


def test_the_ask_payload_has_the_same_shape_on_both_exits(monkeypatch, cfg, repo,
                                                          tmp_path, capsys):
    """#238-F07. The review path spreads `_payload_defaults()` into its refusal so a
    consumer reading any key need not know which exit produced the payload; the ask
    path hand-built thirteen keys against the nineteen a real ask emits, so
    `context`, `context_problems`, `quorum`, `threshold`, `answered`, `counts` and
    `seats_override` raised KeyError on exactly the exit with least else to go on.
    And there was no test, so nothing caught it drifting further — which is the half
    of the finding that matters most."""
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm",
                        lambda *a, **k: panel.SeatAnswer("holds", "r"))
    ok_file = tmp_path / "ok.json"
    panel.ask(cfg["path"], "p", [], reviewers="claude", json_file=str(ok_file))
    answered = json.loads(ok_file.read_text())

    conf = copy.deepcopy(harness_rules.DEFAULTS)
    conf |= {"name": "stranger", "github": "me/demo", "path": str(repo),
             "_rules_baseline": "", "_rules_from": "none (defaults)"}
    monkeypatch.setattr(panel_ask, "load_repo_cfg", lambda name: conf)
    monkeypatch.setattr(panel_ask, "record_ask",
                        lambda payload: pytest.fail("a refused ask is not recorded"))
    refused_file = tmp_path / "refused.json"
    assert panel.ask(str(repo), "p", json_file=str(refused_file)) == 0
    refused = json.loads(refused_file.read_text())

    assert set(refused) == set(answered), (
        "one shape on both exits, or the refusal is the payload nobody tested: "
        f"only when answered={sorted(set(answered) - set(refused))}, "
        f"only when refused={sorted(set(refused) - set(answered))}"
    )
    # `reviewed` is the one key that MUST differ in value — it is what tells
    # "asked and unresolved" from "never asked".
    assert answered["reviewed"] is True and refused["reviewed"] is False
    assert refused["run_key"] and refused["verdict"] is None
