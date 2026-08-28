"""What a reviewer declares about its own pass, and how rounds are decided.

A panel used to answer one question — what did you find — and a run that found
nothing was indistinguishable from a run that could not look: a reviewer handed
half the diff, one whose CLI never started, and one with genuinely nothing to say
all reported the same zero. And the fix that followed was reviewed by nobody,
because the panel read the diff as it was BEFORE the fix and then stopped.

So reviewers now report two things they can actually observe (what they could not
assess, and which fixes need re-reading), the panel measures the one they cannot
(truncation), and the loop itself turns on a mechanical count of findings no
earlier round raised. These tests pin all three, and in particular that the
declarations never drive the loop — they only stop a broken round being read as a
converged one.
"""

import json
import re
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402  — `sh` is defined here since #129
import panel_seats  # noqa: E402  — run_cli lives here since #129
import panel_scope  # noqa: E402  — CI_STATE_WORDS, the vocabulary #546 must cover
import panel_rounds  # noqa: E402  — patched by name where a test forces a collision
from conftest import gh_stub  # noqa: E402





def _reports(title, file="a.py", reviewer="codex", line=1, flagged=False):
    """One reviewer's report of a defect — what a Canonical carries in
    `reported_by`, and what its key is derived from."""
    return [panel.Finding(reviewer, "P2", file, line, title, "",
                          needs_rereview=flagged)]


def _canonical(file, title, severity="P2", reviewer="codex"):
    """A judged finding as the panel serialises it, for the round diff to read."""
    reports = _reports(title, file=file, reviewer=reviewer)
    return panel.Canonical(id="34-F01", severity=severity, file=file, line=1,
                           synthesis=title, verdict="confirmed",
                           reported_by=reports)


# ---- the reply envelope ----------------------------------------------------

def _echoed(prompt: str, **fields) -> str:
    """The schema block exactly as `prompt` ships it, as a model that quoted the
    request back would return it: rendered, with the two tokens that are not JSON
    (`<int|null>`, `true|false`) resolved and nothing else touched.

    Extracted here independently of `panel._schema`, which is the point — the
    parser's idea of "the example this prompt ships" has to keep matching the
    text the prompt actually sends, and a guard that asked panel.py what its own
    prompts say would agree with itself for ever.

    It reports its own failure rather than raising out of import: a drift guard
    that dies during collection names no assertion, which is precisely the
    reader it exists for."""
    rendered = prompt.format(**fields)
    block = next((s for _, s in panel._spans(rendered, "{", "}")
                  if any(f'"{k}"' in s for k in panel.ENVELOPE_KEYS)), None)
    assert block, "no schema block found in the prompt — has its shape changed?"
    assert "<" not in re.sub(r"<[^<>]+>", "", block), f"unpaired `<` in the schema: {block}"
    return re.sub(r"<[^<>]+>", "null", block).replace("true|false", "true")


REVIEW_ECHO = _echoed(panel.REVIEW_PROMPT, n=1, repo="acme/board", base="main",
                      ci="", diff="", code="")
JUDGE_ECHO = _echoed(panel.JUDGE_PROMPT, findings="", coverage="", ci="", diff="")


def test_the_schema_the_prompts_ship_is_recognised_as_a_quotation():
    """The drift guard, and the whole basis of the design: an echo is discarded
    because it is POSITIVELY identified as the example our prompts ship, so the
    example panel.py reads out of its own prompt text has to be the one the
    prompt sends. Edit either schema without this staying true and the parser
    starts treating a quotation as an answer — it fails right here instead.

    Only this direction can be checked cheaply, and only this direction is safe
    to get wrong: a quotation mistaken for an answer costs a retry, whereas the
    converse — an answer mistaken for a quotation — silently discards a review,
    which is what the old stand-in blacklist did."""
    for echo, key in ((REVIEW_ECHO, "findings"), (JUDGE_ECHO, "verdicts")):
        assert panel.SCHEMA_ECHOES[key] is not None, key
        assert panel._quoted(json.loads(echo), panel.SCHEMA_ECHOES[key]), echo
        # ...including its single example entry, which is what stops the example
        # riding into a real reply as a finding or a ruling nobody made.
        assert not panel._is_answer(json.loads(echo)[key][0], key), echo


def test_every_envelope_has_a_declaration_beside_it():
    """`_read` reads `DECLARATION_KEYS[kind]` for a kind taken from
    `ENVELOPE_KEYS`, and the two are maintained a hundred lines apart. A third
    envelope key added without its declaration would crash the panel mid-run."""
    assert set(panel.ENVELOPE_KEYS) == set(panel.DECLARATION_KEYS)
    assert set(panel.ENVELOPE_KEYS) == set(panel.SCHEMA_ECHOES)


def test_the_envelope_carries_findings_and_declarations():
    raw = json.dumps({
        "findings": [{"severity": "P2", "file": "a.py", "line": 4, "title": "leak",
                      "detail": "closes nothing"}],
        "could_not_assess": ["the migration, which is not in the diff"],
    })
    findings, gaps = panel.parse_reply("codex", raw)
    assert [f.title for f in findings] == ["leak"]
    assert gaps == ["the migration, which is not in the diff"]


def test_an_illustration_the_model_wrote_itself_is_not_resolved_either_way():
    """A model that invents its own example — *"e.g. `{"findings": [{"severity":
    "P2", "file": "a.py", "title": "example only"}]}`"* — has written something
    this parser cannot tell from an answer, because it is not the schema and its
    text is ordinary. Ranking by how much a candidate reports read the
    illustration as the fuller answer, and the result was the one artefact the
    module must never produce: a FABRICATED finding filed under the reviewer's
    name, with the reviewer's real declaration discarded beside it.

    So it is not resolved. The reply retries once and is then kept as the
    reviewer's own words — which loses nothing and invents nothing."""
    illustration = ('e.g. {"findings": [{"severity": "P2", "file": "a.py", '
                    '"title": "example only"}]}')
    answer = '{"findings": [], "could_not_assess": ["the migration"]}'
    for raw in (f"{illustration}\n\n{answer}", f"{answer}\n\nFor reference, {illustration}"):
        assert panel.parse_reply("codex", raw) is None, raw


def test_two_paraphrases_of_the_envelope_are_not_a_ranking_problem():
    """The mirror direction, and the pair that broke both earlier rules inside one
    release: an empty envelope beside a full one. First-wins lost the review to
    the example in front of it; last-wins lost it to the one behind. Quantity
    settled it in the right direction here and the wrong one above, which is what
    quantity being no evidence means.

    Neither of these is the schema THIS file sends (see below, where the real echo
    is dropped and the answer survives) — they are a model's own shorthand, and
    nothing in the reply says which is meant."""
    empty = '{"findings": [], "could_not_assess": []}'
    full = ('{"findings": [{"severity": "P1", "file": "a.py", "title": "boom"}], '
            '"could_not_assess": ["the migration"]}')
    for raw in (f"I will reply as {empty}.\n\nHere it is:\n{full}",
                f"{full}\n\nFor reference, the shape I used was {empty}."):
        assert panel.parse_reply("codex", raw) is None, raw


def test_the_schema_this_file_actually_sends_is_not_an_answer():
    """The premise, checked against the prompt instead of against an invented
    example. `REVIEW_PROMPT` ends `"could_not_assess": ["..."]` and ships one
    populated example finding, so an echo read literally is not empty at all: it
    declares a gap of "..." and reports a P3 in a file called "path", which beats
    a clean review from either side — putting `codex could not assess: ...` in the
    PR comment and costing the round its confidence for good — and ties with a
    real declaration, which discards the whole reply."""
    clean = '{"findings": [], "could_not_assess": []}'
    declared = '{"findings": [], "could_not_assess": ["the migration"]}'
    found = ('{"findings": [{"severity": "P1", "file": "a.py", "title": "boom"}], '
             '"could_not_assess": []}')
    for answer, titles, gaps in ((clean, [], []),
                                 (declared, [], ["the migration"]),
                                 (found, ["boom"], [])):
        for raw in (f"I will reply as {REVIEW_ECHO}\n\n{answer}",
                    f"{answer}\n\nThe shape I used was {REVIEW_ECHO}"):
            got = panel.parse_reply("codex", raw)
            assert got is not None, raw
            assert ([f.title for f in got[0]], got[1]) == (titles, gaps), raw


def test_the_schema_does_not_beat_an_answer_in_a_lower_tier():
    """Shape is asked before agreement, and the requested envelope beats the bare
    array a model reached for instead — so an echo of the envelope would win the
    whole reply while the review sits one tier below it. Discounting what the echo
    SAYS is not enough to stop that: it has to stop being a candidate."""
    raw = (f"The shape asked for is {REVIEW_ECHO}\n\n"
           '[{"severity": "P2", "file": "a.py", "title": "the real one"}]')
    findings, gaps = panel.parse_reply("codex", raw)
    assert [f.title for f in findings] == ["the real one"] and gaps is None


def test_a_reply_that_is_only_the_schema_is_unreadable_not_flawless():
    """With no answer beside it the echo is all there is, and taking it at face
    value files a P3 in a file called "path" titled "..." and declares a coverage
    gap of "...". Unreadable is the truthful answer: the caller asks again and then
    keeps the reply's own words, which is how the round stays suspect."""
    assert panel.parse_reply("codex", REVIEW_ECHO) is None


def test_a_terse_verdict_whose_only_free_text_is_its_own_id_is_still_a_ruling(monkeypatch):
    """The direction that matters, and the one nothing pinned. `JUDGE_PROMPT`
    tells the judge that an issue id is "a label YOU invent for an issue you are
    returning ('F01')" and illustrates `related` as `["F03"]`, so those strings
    are simultaneously the schema's stand-ins and the values a compliant judge
    writes. A rule that read them as quotation marks discarded this whole reply:
    no ruling, every finding through as `unjudged`, and the round vetoed as not
    adjudicated — on a verdict shaped exactly as asked."""
    _judge_returning(monkeypatch, '{"verdicts": [{"id": "F01", "members": [0], '
                                  '"real": true, "related": ["F03"]}]}')
    leak = panel.Finding("codex", "P2", "a.py", 1, "leak", "")
    out, skip, note = panel.adjudicate([[leak]], "diff", "", 34)
    assert skip is None and [c.verdict for c in out] == ["confirmed"]
    assert [c.reviewers for c in out] == [["codex"]]


def test_a_finding_against_a_file_called_path_is_still_a_finding():
    """The reviewers' side of the same collision: `"path"` is the schema's example
    file name AND a plausible real one, and `"..."` is what a model writes when it
    has nothing to add to a title. Neither is evidence of anything on its own."""
    raw = json.dumps({"findings": [{"severity": "P2", "file": "path", "line": 12,
                                    "title": "the package shadows the stdlib name",
                                    "detail": "..."}]})
    findings, _ = panel.parse_reply("codex", raw)
    assert [(f.file, f.title) for f in findings] == [
        ("path", "the package shadows the stdlib name")]


def test_a_clean_review_that_left_the_prompts_ellipsis_in_place_is_still_clean():
    """`{"findings": [], "could_not_assess": ["..."]}` is a sloppy but perfectly
    real "I found nothing" — not the schema, which ships a populated example
    finding beside that declaration. Discarding the whole candidate bought a retry
    and then an `unstructured` veto for a flawless review. The stand-in is
    stripped off the DECLARATION instead, which is the one field where a stand-in
    cannot also be an answer: the reviewer is left never heard on coverage rather
    than declaring a gap of "..."."""
    raw = '{"findings": [], "could_not_assess": ["..."]}'
    assert panel.parse_reply("codex", raw) == ([], None)


def test_the_judges_own_schema_is_not_an_answer_either(monkeypatch):
    """`JUDGE_PROMPT` ends `"coverage_note": "..."`, and the judge gets NO retry.
    An echo taken for its reply reports an adjudicated round — no "not adjudicated"
    veto, and a coverage ruling nobody made — on the one round where the coverage
    split most needed ruling on."""
    answer = '{"verdicts": [], "coverage_note": "the migration is unread"}'
    for raw in (f"Shape: {JUDGE_ECHO}\n{answer}", f"{answer}\nShape used: {JUDGE_ECHO}"):
        val = panel.extract_json_value(raw, "verdicts")
        assert val["coverage_note"] == "the migration is unread", raw
    _judge_returning(monkeypatch, JUDGE_ECHO)
    out, skip, note = panel.adjudicate([], "diff", "", 34, coverage={"codex": ["the migration"]})
    assert out == [] and note.note == ""
    assert skip and "unparseable" in skip


def test_the_judge_is_picked_the_same_way():
    """`verdicts` is an envelope key too, and the judge runs the same parser. An
    echo taken as its answer rules on nothing — which sends every finding through
    as `unjudged` while the round reports a judge that answered."""
    raw = ('{"verdicts": [{"id": "F01", "members": [0], "real": true}], '
           '"coverage_note": "the migration is unread"}\n'
           f'Shape used: {JUDGE_ECHO}')
    val = panel.extract_json_value(raw, "verdicts")
    assert [v["id"] for v in val["verdicts"]] == ["F01"]
    assert val["coverage_note"] == "the migration is unread"


def test_the_schemas_example_verdict_rules_on_nothing_even_beside_a_real_note(monkeypatch):
    """The example verdict does not have to arrive alone. A judge that quotes it
    and then writes a genuine `coverage_note` produces a candidate that is NOT the
    schema handed back whole, so it is read as the answer — and consumed
    verbatim it claims reports 0 and 3, synthesises "the merged statement of the
    issue" and marks them real on nobody's authority.

    So the entry is dropped where entries are dropped, on both paths. The round is
    then a judge that ruled on nothing, which is what it was."""
    example = json.loads(JUDGE_ECHO)["verdicts"][0] | {"members": [0, 3]}
    _judge_returning(monkeypatch, json.dumps(
        {"verdicts": [example], "coverage_note": "the migration is unread"}))
    leak = panel.Finding("codex", "P2", "a.py", 1, "leak", "")
    out, skip, note = panel.adjudicate([[leak]], "diff", "", 34)
    assert [c.verdict for c in out] == ["unjudged"] and skip is None
    assert [c.synthesis for c in out] == ["leak"]
    assert note.note == "the migration is unread"


def test_a_judge_reply_is_read_on_its_verdicts_not_on_a_findings_key():
    """`findings` comes first in `ENVELOPE_KEYS`, so a judge candidate carrying an
    incidental `findings` key was read as a review — on the wrong items and the
    wrong declaration, which can call two spellings of one ruling an ambiguity and
    take the whole round through `unjudged`. The caller says which envelope it
    asked for."""
    verdict = '{"id": "F01", "members": [0], "real": true}'
    note = '"coverage_note": "the migration is unread"'
    raw = (f'{{"verdicts": [{verdict}], {note}, "findings": []}}\n'
           f'{{"verdicts": [{verdict}], {note}}}')
    val = panel.extract_json_value(raw, "verdicts")
    assert [v["id"] for v in val["verdicts"]] == ["F01"]
    assert val["coverage_note"] == "the migration is unread"
    # ...and read as a reviewer's reply the same text says two different things.
    assert panel.extract_json_value(raw) is None


def test_two_different_answers_are_not_resolved_by_position():
    """Nothing in a reply carrying two equally-shaped, equally-full envelopes says
    which is the answer. Picking either is a bet on prose style, and one of the two
    bets loses a whole review — so the reply is reported as unstructured instead.
    That path retries once and then keeps the raw text as a finding, which
    preserves the uncertainty rather than resolving it wrongly."""
    raw = ('{"findings": [{"title": "the first answer"}]}\n'
           '{"findings": [{"title": "a different one"}]}')
    assert panel.parse_reply("codex", raw) is None


def test_an_ambiguous_reply_lands_in_the_degradation_path_that_already_exists(monkeypatch):
    """What "not resolved" costs, end to end: one retry, and then the reviewer's
    own words kept as a finding with the round marked as carrying an unstructured
    reply — which is what stops that round being read as a quiet PR. Nothing is
    dropped and nothing clean is manufactured.

    Runs on `claude` rather than `codex` because codex is the one seat whose
    stdout is not its reply: it writes the reply to `--output-last-message` and
    puts events on stdout. A fake `run_cli` that returns the reply as stdout
    therefore models every seat but that one, and against codex it exercises the
    missing-reply-file path instead of the ambiguity this test is about."""
    raw = ('{"findings": [{"title": "the first answer"}]}\n'
           '{"findings": [{"title": "a different one"}]}')
    calls = []

    def fake_run_cli(args, label, timeout=panel.CLI_TIMEOUT, attempts=3, stdin_text=None,
                     on_output=None, replied=None, cwd=None):
        calls.append(attempts)
        return raw, None

    monkeypatch.setattr(panel.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(panel_seats, "run_cli", fake_run_cli)
    got = panel.review_llm("claude", "opus", "p")
    assert got.unstructured is True and len(calls) == 2
    assert got.skip is None and "the first answer" in got.findings[0].detail


def test_one_answer_printed_twice_is_not_an_ambiguity():
    """Fencing the envelope and then repeating it is one answer, not two — and
    degrading it would cost a second CLI call and the round its confidence."""
    envelope = '{"findings": [{"title": "boom"}], "could_not_assess": ["the migration"]}'
    raw = f'```json\n{envelope}\n```\n\nAgain, in full:\n{envelope}'
    findings, gaps = panel.parse_reply("pi", raw)
    assert [f.title for f in findings] == ["boom"] and gaps == ["the migration"]


def test_a_review_that_genuinely_found_nothing_is_not_degraded():
    """Two empty candidates report the same absence, so there is no answer to lose
    by taking one — and calling it ambiguous would buy a retry and a coverage veto
    for a reply that was perfectly clear."""
    raw = ('I will reply as {"findings": [], "could_not_assess": []}.\n'
           '{"findings": [], "could_not_assess": []}')
    assert panel.parse_reply("claude", raw) == ([], [])


def test_two_spellings_of_one_declaration_are_one_answer():
    """`could_not_assess: "the migration"` and `["the migration"]` are the same
    declaration to `_str_list` — the parser says so itself — so a reply carrying
    both spellings has said one thing, not two. Comparing raw Python values called
    that an ambiguity and spent a CLI call and the round's confidence on a reply
    nobody could have misread; the comparison is over what the parser will read."""
    one = '{"findings": [{"title": "boom"}], "could_not_assess": "the migration"}'
    other = '{"findings": [{"title": "boom"}], "could_not_assess": ["the migration"]}'
    for raw in (f"{one}\n{other}", f"{other}\n{one}"):
        findings, gaps = panel.parse_reply("codex", raw)
        assert [f.title for f in findings] == ["boom"] and gaps == ["the migration"], raw


def test_two_spellings_of_one_review_are_one_review():
    """The same rule over the findings, which is where it earns its keep now that
    equality is the WHOLE mechanism. `"p1"` and `"P1"`, `"line": null` and no line
    at all, an omitted `detail`, a per-finding `needs_rereview` and the matching
    `fix_needs_rereview` index, and any key the parser ignores are one review — so
    a model that fences its envelope and then restates it slightly differently has
    said one thing, and calling that an ambiguity spends a CLI call and the round's
    confidence on a reply nobody could have misread."""
    one = ('{"findings": [{"severity": "p1", "file": "a.py", "line": null, '
           '"title": "boom", "needs_rereview": true}]}')
    other = ('{"findings": [{"severity": "P1", "file": "a.py", "title": "boom", '
             '"detail": ""}], "fix_needs_rereview": [0], "summary": "I read it all"}')
    for raw in (f"{one}\n{other}", f"{other}\n{one}"):
        findings, gaps = panel.parse_reply("codex", raw)
        assert [(f.severity, f.title, f.line, f.needs_rereview) for f in findings] == [
            ("P1", "boom", None, True)], raw
        assert gaps is None, raw


def test_declaring_clean_is_not_the_same_answer_as_never_mentioning_the_key():
    """`[]` is "asked, and had nothing to declare"; a bare `{"findings": []}` was
    never heard on the question at all. The board stores the first as `[]` and the
    second as null, and a comparison that collapsed them would call one of these
    the other's repeat and pick whichever came last — in the payload where that
    distinction is the whole point."""
    declared = '{"findings": [], "could_not_assess": []}'
    silent = '{"findings": []}'
    for raw in (f"{silent}\n{declared}", f"{declared}\n{silent}"):
        assert panel.parse_reply("codex", raw) is None, raw
    # ...and each on its own still says its own thing.
    assert panel.parse_reply("codex", declared) == ([], [])
    assert panel.parse_reply("codex", silent) == ([], None)


def test_a_declaration_the_parser_keeps_nothing_of_is_not_a_clean_one():
    """Nulls, empty strings and nested objects are still dropped rather than
    stringified — `could_not_assess: [{"area": "the migration"}]` used to become
    the Python repr `"{'area': 'the migration'}"`, which `/panel` printed verbatim
    as words a reviewer had written, and `app/api/reviews.py::_phrases` has always
    dropped them.

    But dropping them is not the same as the reviewer having had nothing to say,
    and this test used to assert it was. A reviewer that WROTE something the
    parser cannot read has declared a gap it cannot name, which is `None` ("said
    nothing"), not `[]` ("asked, and had nothing to declare"). `coverage_veto`
    iterates the declaration, so `[]` here retired the reviewer's veto and let a
    round be recorded confident on a seat that had said out loud it could not
    assess something. Only a genuinely EMPTY list is a clean declaration.

    Storing `[]` and SCORING the round on `[]` are different questions; the
    `_phrases` mirror argument settles the first and says nothing about this."""
    junk = '{"findings": [], "could_not_assess": ["", null, {"area": "x"}, []]}'
    assert panel.parse_reply("codex", junk) == ([], None)
    empty = '{"findings": [], "could_not_assess": []}'
    assert panel.parse_reply("codex", empty) == ([], [])


def test_the_lower_tiers_answer_the_same_question_the_same_way():
    """Two tiers went on resolving by position after the others stopped — the
    object that is prose ABOUT the schema, and the fallback that takes any JSON at
    all. Nothing observable turned on it, since they only ever win when nothing
    better exists; but one file cannot hold two answers to "which value is the
    reply", and a later reader could not tell which was meant. Both now ask what
    was said, which among candidates that say nothing is the last of them — the
    behaviour these tiers always had."""
    prose = ('{"findings": "an array of objects"}\n'
             '{"findings": "one object per defect, severity P1..P4"}')
    assert panel.extract_json_value(prose) == {
        "findings": "one object per defect, severity P1..P4"}
    assert panel.extract_json_value('{"a": 1}\n{"b": 2}') == {"b": 2}
    assert panel.extract_json_value("no json here at all") is None


def test_a_sentence_about_the_schema_is_not_an_envelope():
    """`{"findings": "an array of objects"}` carries the key and no answer. The
    real reply — a bare array here — must win over it."""
    raw = ('{"findings": "an array of objects, severity P1..P4"}\n'
           '[{"severity": "P2", "file": "a.py", "title": "the real one"}]')
    findings, _ = panel.parse_reply("codex", raw)
    assert [f.title for f in findings] == ["the real one"]


def test_a_list_of_phrases_does_not_outweigh_the_review_beside_it():
    """A declaration list holds PHRASES and a findings array holds objects, so a
    rule that counted raw items would let a two-phrase `could_not_assess` beat a
    one-finding answer — and a list of strings survives `_to_findings` as nothing
    at all, which is the clean "found nothing" this parser must never invent."""
    raw = ('{"findings": "an array of objects", "could_not_assess": ["a", "b"]}\n'
           '[{"severity": "P2", "file": "a.py", "title": "the real one"}]')
    findings, _ = panel.parse_reply("codex", raw)
    assert [f.title for f in findings] == ["the real one"]


def test_a_bare_array_declares_nothing_which_is_not_declaring_clean():
    """Four CLIs get the new contract at four different speeds, and a model that
    ignores the envelope has still done the review — but it was never heard on
    coverage, and None is how the board stores that. `[]` would say it was asked
    and had nothing to declare, which is the collapse this release exists to
    remove."""
    findings, gaps = panel.parse_reply("claude", '[{"severity":"P1","title":"boom"}]')
    assert [f.severity for f in findings] == ["P1"]
    assert gaps is None


def test_an_envelope_that_omits_the_key_declares_nothing_either():
    _, gaps = panel.parse_reply("codex", json.dumps({"findings": []}))
    assert gaps is None
    # ...but present-and-empty IS an answer: asked, and no gap to report.
    _, gaps = panel.parse_reply("codex", json.dumps({"findings": [], "could_not_assess": []}))
    assert gaps == []


def test_prose_around_the_envelope_does_not_hide_the_declarations():
    """The failure this guards: preferring `[` unconditionally finds the envelope's
    INNER findings array, parses fine, and silently drops everything alongside it —
    a bug with no symptom except declarations that are never there."""
    raw = ('Here is my review:\n```json\n'
           '{"findings": [{"title": "x", "file": "a.py"}], "could_not_assess": ["runtime"]}\n'
           '```\nHope that helps.')
    findings, gaps = panel.parse_reply("pi", raw)
    assert len(findings) == 1 and gaps == ["runtime"]


def test_an_earlier_brace_in_the_prose_does_not_cost_the_declarations():
    """The nastier half of the same bug: the first `{...}` span is prose and fails
    to parse, so a scan that picks by bracket position falls through to the next
    span by offset — which is the envelope's own INNER findings array. It parses
    cleanly, the findings survive, and the declarations vanish with no symptom."""
    raw = ('Note: severities are {P1..P4}, dashes are literal.\n'
           '{"findings": [{"title": "x", "file": "a.py"}], "could_not_assess": ["the schema"]}')
    findings, gaps = panel.parse_reply("codex", raw)
    assert [f.title for f in findings] == ["x"]
    assert gaps == ["the schema"]


def test_a_bare_array_behind_a_prose_object_is_still_found():
    """The mirror case: an old-shape reviewer whose reply opens with something
    brace-shaped must not be read as an envelope with no findings."""
    findings, gaps = panel.parse_reply("claude", 'Legend: {} means nothing.\n'
                                                 '[{"severity": "P2", "title": "boom"}]')
    assert [f.title for f in findings] == ["boom"] and gaps is None


def test_a_string_where_a_list_was_asked_for_is_taken_as_one_item():
    _, gaps = panel.parse_reply("pi", json.dumps(
        {"findings": [], "could_not_assess": "the schema"}))
    assert gaps == ["the schema"]


def test_unparseable_is_still_none_so_the_caller_can_retry():
    """Distinct from ([], []) — "found nothing" and "said nothing usable" are not
    the same event, and collapsing them is how a reviewer's work gets dropped."""
    assert panel.parse_reply("codex", "sorry, I can't do that") is None
    assert panel.parse_reply("codex", "") is None
    # An object that isn't the envelope has no findings to take.
    assert panel.parse_reply("codex", '{"verdict": "looks fine"}') is None
    assert panel.parse_reply("codex", "[]") == ([], None)


def test_findings_that_are_not_objects_are_unreadable_not_absent():
    """A model that answers with a list of SENTENCES has reviewed the diff and
    said so; every entry is dropped by `_to_findings`, and reporting the empty
    remainder records the one thing that must never be manufactured — a reviewer
    that read the diff and found it flawless. Unreadable is the truthful answer,
    and it keeps the sentences: the caller retries and then holds the raw reply as
    a finding."""
    assert panel.parse_reply("codex", '["the migration is unread", "x is leaky"]') is None
    assert panel.parse_reply("codex", json.dumps(
        {"findings": ["x is leaky"], "could_not_assess": ["the migration"]})) is None
    # ...and a findings array with one usable entry among the junk is still a
    # review, which is the distinction the existing index arithmetic rests on.
    findings, _ = panel.parse_reply("codex", json.dumps({"findings": [None, {"title": "x"}]}))
    assert [f.title for f in findings] == ["x"]


# ---- the re-review declaration ---------------------------------------------

def test_fix_needs_rereview_flags_by_index():
    """Indexes into the array just returned, so a reviewer needs no id scheme —
    and cannot flag a finding it did not report."""
    raw = json.dumps({"findings": [{"title": "one"}, {"title": "two"}],
                      "fix_needs_rereview": [1, 7, True]})
    findings, _ = panel.parse_reply("codex", raw)
    assert [f.needs_rereview for f in findings] == [False, True]


def test_an_index_counts_the_findings_the_model_sent_not_the_ones_we_kept():
    """A junk entry among the findings is dropped, and every index after it would
    otherwise point one finding too far — flagging the neighbour of the one the
    reviewer meant."""
    raw = json.dumps({"findings": [None, {"title": "one"}, {"title": "two"}],
                      "fix_needs_rereview": [2]})
    findings, _ = panel.parse_reply("codex", raw)
    assert [f.title for f in findings] == ["one", "two"]
    assert [f.needs_rereview for f in findings] == [False, True]


def test_a_per_finding_flag_means_the_same_thing():
    findings, _ = panel.parse_reply("codex", json.dumps(
        {"findings": [{"title": "one", "needs_rereview": True}]}))
    assert findings[0].needs_rereview is True


def test_a_string_no_is_not_a_yes():
    """This parser deliberately tolerates imperfect LLM JSON, and `bool("false")`
    is True — so a model spelling the answer out made a declaration it explicitly
    declined to make. That flag both vetoes a stop and feeds the per-member honesty
    count, where a manufactured yes cannot be spotted later."""
    findings, _ = panel.parse_reply("codex", json.dumps({"findings": [
        {"title": "a", "needs_rereview": "false"},
        {"title": "b", "needs_rereview": "no"},
        {"title": "c", "needs_rereview": "0"},
        {"title": "d", "needs_rereview": None},
        {"title": "e", "needs_rereview": {"why": "structural"}},
        {"title": "f", "needs_rereview": "true"},
        {"title": "g", "needs_rereview": "Yes"},
        {"title": "h", "needs_rereview": 1},
        {"title": "i", "needs_rereview": True},
    ]}))
    assert [f.title for f in findings if f.needs_rereview] == ["f", "g", "h", "i"]


def test_the_flag_survives_the_merge_from_any_reporter():
    """One reviewer seeing that the fix will be structural is the observation;
    the others not saying so is not a contradiction of it."""
    a = panel.Finding("claude", "P2", "a.py", 10, "same bug", needs_rereview=False)
    b = panel.Finding("codex", "P2", "a.py", 12, "same bug", needs_rereview=True)
    [c] = panel._parse_verdicts([{"id": "F1", "members": [0, 1], "real": True}], [a, b], 34)
    assert c.reviewers == ["claude", "codex"] and c.needs_rereview is True
    # ...but attribution is not flattened with it: honesty is per reviewer. It is
    # READ off each reporter's own report now rather than reconstructed onto a
    # representative, so there is no longer an order in which the merge could
    # credit the wrong member.
    assert c.rereview_by == ["codex"]
    flags = {r["reviewer"]: r["needs_rereview"] for r in c.as_dict()["reported_by"]}
    assert flags == {"claude": False, "codex": True}


# ---- what the merge treats as one defect -----------------------------------

def test_two_files_with_one_basename_are_two_defects():
    """Reviewers spell paths differently, so a merge is tempted by the basename —
    but the file is half of the defect key, so merging these would hand one file's
    defect the other's identity."""
    a = panel.Finding("claude", "P2", "app/api/reviews.py", 10, "unused import")
    b = panel.Finding("codex", "P2", "harness/loops/reviews.py", 12, "unused import")
    assert panel.cluster_findings([a, b]) == [[a], [b]]
    assert panel._defect_key(a.file, [a]) != panel._defect_key(b.file, [b])


def test_a_short_path_and_the_full_one_are_the_same_defect():
    """One reviewer quotes `reviews.py`, another `app/api/reviews.py`. That is one
    defect — the judge is what says so now, and the record it writes keeps both
    accounts. What survives the ruling is the spelling the judge chose, so the
    round diff is where the two must still be recognised as one file."""
    a = panel.Finding("claude", "P2", "reviews.py", 10, "unused import")
    b = panel.Finding("codex", "P1", "app/api/reviews.py", 12, "unused import")
    [c] = panel._parse_verdicts(
        [{"id": "F1", "members": [0, 1], "file": "app/api/reviews.py"}], [a, b], 34)
    assert c.file == "app/api/reviews.py" and c.reviewers == ["claude", "codex"]
    was = panel.Baseline(keys={c.key}, titles={"unused import": {c.file}})
    assert was.raised_before(_canonical("reviews.py", "unused import"))


def test_the_representative_does_not_depend_on_who_reported_it():
    """The identity is the first of the reporters' titles alphabetically. Taking
    whichever member happened to win a merge let the wording flip between rounds
    as members dropped out or re-rated, which reads on the PR as a fix that broke
    something."""
    same = [panel.Finding("codex", "P2", "a.py", 10, "zebra wording"),
            panel.Finding("claude", "P2", "a.py", 12, "alpha wording")]
    round1 = panel.Canonical(id="34-F01", severity="P2", file="a.py", line=10,
                             synthesis="the judge's words", verdict="confirmed",
                             reported_by=same)
    round2 = panel.Canonical(id="34-F01", severity="P2", file="a.py", line=12,
                             synthesis="the judge's other words", verdict="confirmed",
                             reported_by=list(reversed(same)))
    assert round1.key == round2.key
    assert panel.Baseline(keys={round1.key}).raised_before(round2)


# ---- defect identity -------------------------------------------------------

def test_the_key_ignores_the_line_and_normalises_the_title():
    """A line number moves when the fix above it lands. An identity that moves
    links nothing, so the same defect described the same way in two rounds is one
    key, whatever line each reviewer put it on."""
    said = "Unicode dash survives the strip!"
    again = "unicode  dash survives the strip"
    assert (panel._defect_key("app/x.py", _reports(said, file="app/x.py", line=4))
            == panel._defect_key("app/x.py", _reports(again, file="app/x.py", line=91)))
    assert (panel._defect_key("app/x.py", _reports("a", file="app/x.py"))
            != panel._defect_key("app/y.py", _reports("a", file="app/y.py")))


# ---- baselines -------------------------------------------------------------

def _serialised(file, title):
    """A finding as an earlier round's --json-file holds it: the key it sent, and
    the reporters' own titles the key was made from."""
    return {"file": file, "synthesis": title,
            "key": panel._defect_key(file, _reports(title, file=file)),
            "reported_by": [{"reviewer": "codex", "title": title}]}


def _payload(tmp_path, name, round_no, titles, dismissed=(), **over):
    p = tmp_path / name
    p.write_text(json.dumps({
        "round": round_no,
        "repo": "acme", "github": "acme/board", "pr": 34,
        "to_fix": [_serialised("a.py", t) for t in titles],
        "dismissed": [_serialised("a.py", t) for t in dismissed],
        **over,
    }))
    return str(p)


THIS_RUN = {"repo": "acme", "github": "acme/board", "pr": 34, "round": 2}


def test_a_baseline_counts_what_was_raised_not_what_was_confirmed(tmp_path):
    """A finding the judge dismissed in round 1 and a reviewer raises again in
    round 2 is not new information. Diffing against confirmed findings only is how
    a loop re-discovers its own rejects forever and never converges."""
    path = _payload(tmp_path, "r1.json", 1, ["real bug"], dismissed=["false alarm"])
    b = panel.load_baseline([path], THIS_RUN)
    assert b.problems == [] and b.rounds == {1}
    assert b.raised_before(_canonical("a.py", "false alarm"))
    assert b.raised_before(_canonical("a.py", "real bug"))


def test_an_escalation_is_inherited_by_every_later_round(tmp_path):
    """An escalation is answered by a human on their own clock, so nothing about
    a later round makes it stop being open. A cycle that had to re-pass the flag
    every round would go back to counting it as work the loop can do the first
    time a caller forgot — the jam returning one round later."""
    path = _payload(tmp_path, "r1.json", 1, ["real bug"],
                    escalated={"deadbeefdeadbeef": 1})
    b = panel.load_baseline([path], THIS_RUN)
    assert b.problems == [] and b.escalated == {"deadbeefdeadbeef": 1}


def test_the_round_an_escalation_was_first_declared_in_survives_a_merge(tmp_path):
    """Earliest wins, like the cycle id: a later round re-stating a key it
    inherited must not re-date the claim to now. The round is the only part of
    this an auditor can check the caller's word against."""
    first = _payload(tmp_path, "r1.json", 1, ["a"], escalated={"aabbccddeeff0011": 1})
    later = _payload(tmp_path, "r2.json", 2, ["b"], escalated={"aabbccddeeff0011": 2})
    assert panel.load_baseline([first, later], {**THIS_RUN, "round": 3}).escalated \
        == panel.load_baseline([later, first], {**THIS_RUN, "round": 3}).escalated \
        == {"aabbccddeeff0011": 1}


def test_a_bare_list_of_escalated_keys_is_read_as_that_round_s(tmp_path):
    """A payload written before the field carried a round, or one written by
    hand. Attributing it to the round that wrote it is the only answer available
    and is never later than the truth."""
    path = _payload(tmp_path, "r1.json", 1, ["a"], escalated=["00ff00ff00ff00ff"])
    assert panel.load_baseline([path], THIS_RUN).escalated == {"00ff00ff00ff00ff": 1}


LATER = {**THIS_RUN, "round": 4}
GOOD = "deadbeefdeadbeef"


@pytest.mark.parametrize("junk,register,complaint", [
    # The CONTAINER is the wrong shape: nothing can be read out of it, and the
    # register reverting to empty in silence is the #221 jam arriving with no
    # diagnostic at all.
    ("a string", {}, "neither an object nor a list"),
    (7, {}, "neither an object nor a list"),
    # `None` is the absent field, which is not a fault: every payload written
    # before #221 has one, and they are the common case, not the corrupt one.
    (None, {}, None),
    # A key nothing can ever match. It would sit in the register forever while
    # the caller read the cycle's silence as the escalation being honoured.
    ({"": 1}, {}, "not the shape of a finding key"),
    ({"not a key": 1}, {}, "not the shape of a finding key"),
    ({"NOTHEXNOTHEXNOTH": 1}, {}, "not the shape of a finding key"),
    # A real key, dated with something that is not a round of this cycle. The key
    # is kept — losing it is the failure that matters — and the date falls back to
    # the round of the payload carrying it, which is never later than the truth.
    ({GOOD: "not a round"}, {GOOD: 3}, "not a round of this cycle"),
    ({GOOD: 0}, {GOOD: 3}, "not a round of this cycle"),
    ({GOOD: -2}, {GOOD: 3}, "not a round of this cycle"),
    ({GOOD: 9}, {GOOD: 3}, "not a round of this cycle"),
    ({GOOD: 2.9}, {GOOD: 3}, "not a round of this cycle"),
    # `bool` is an `int` subclass, so an unguarded `int(when)` reads `True` as
    # "declared in round 1" — a plausible-looking round number invented out of a
    # payload that carries none.
    ({GOOD: True}, {GOOD: 3}, "not a round of this cycle"),
    # ...and the shapes that are fine, so the complaint above is not just noise
    # this asserts against everything.
    ({GOOD: 2}, {GOOD: 2}, None),
    ([GOOD], {GOOD: 3}, None),
])
def test_what_a_malformed_escalated_field_costs(tmp_path, junk, register, complaint):
    """Same rule as every other field this function reads: a bad payload costs a
    `problems` entry, never a review every reviewer CLI has been paid for — and
    never silence. Silence is the expensive one here: an unreadable register puts
    a finding only a human can close back into the work a fix round counts, which
    is precisely the jam the register exists to prevent.

    One expected result per input, because these fail in five different ways and a
    disjunction over all of them passes for implementations that are wrong."""
    path = _payload(tmp_path, "r.json", 3, ["a"], escalated=junk)
    b = panel.load_baseline([path], LATER)
    assert b.escalated == register
    if complaint is None:
        assert b.problems == []
    else:
        assert any(complaint in x for x in b.problems), b.problems


def test_a_malformed_escalated_key_is_not_echoed_raw_into_the_report(tmp_path):
    """`problems` becomes a veto line, and the veto list is posted to the PR. A
    key is 8-64 hex characters; anything else is named by a flattened, truncated
    excerpt so a corrupt payload cannot put markdown on a public comment."""
    evil = "[click](http://x)\n\n# heading " + "z" * 200
    path = _payload(tmp_path, "r.json", 3, ["a"], escalated={evil: 1})
    problems = " ".join(panel.load_baseline([path], LATER).problems)
    assert "](" not in problems and "\n" not in problems and "# heading" not in problems
    assert len(problems) < 300


@pytest.mark.parametrize("typed", ["DEADBEEFDEADBEEF", " deadbeefdeadbeef",
                                   "deadbeefdeadbeef\n", "  DeadBeefDeadBeef  "])
def test_a_key_a_HUMAN_retyped_is_normalised_rather_than_rejected(tmp_path, typed):
    """This is the one value the design says is read out of a fixer's PROSE report
    and retyped, so an upper-case key or one carrying a copy-paste newline is the
    caller naming exactly the right finding. Rejecting it produced a note blaming
    the caller for a correct value AND left the escalation uncounted — the jam, with
    a misleading diagnostic on top. Normalising cannot admit anything `_KEY_RE`
    would not: case and surrounding blanks are all it touches."""
    assert panel._is_key(typed) and panel._key_norm(typed) == GOOD
    path = _payload(tmp_path, "r.json", 1, ["a"], escalated={typed: 1})
    b = panel.load_baseline([path], LATER)
    assert b.problems == []
    assert b.escalated == {GOOD: 1}, "stored as a finding's own key is spelled"


def test_a_homoglyph_excerpt_is_flattened_like_any_other_junk():
    """`_key_gist`'s excerpt is published by `--post`, and its job is to let a human
    recognise WHICH value was rejected. `str.isalnum` is true for letters and digits
    in every script, so a Cyrillic look-alike came through verbatim and rendered as
    a plausible key — harmless as markdown, useless as an excerpt."""
    assert panel._key_gist("\u0430\u0435\u043e\u0440\u0441\u0443\u0445") == "?" * 7
    assert panel._key_gist("\uff11\uff12\uff13") == "???"
    assert panel._key_gist(GOOD) == GOOD, "a real key still reads as itself"


def test_a_baseline_reads_the_key_the_payload_carries(tmp_path):
    """The panel sends a key with every finding, and it is the same identity the
    board chains runs on. Re-deriving one here — from the judge's freshly-worded
    synthesis, the only title an older payload had — is how the local round diff
    and the board's chains came to disagree about which findings were new."""
    p = tmp_path / "r1.json"
    p.write_text(json.dumps({
        "round": 1, "repo": "acme", "github": "acme/board", "pr": 34,
        "to_fix": [{"file": "a.py", "key": "deadbeefdeadbeef",
                    "synthesis": "the judge reworded this one completely",
                    "reported_by": [{"reviewer": "codex", "title": "leaky handle"}]}],
    }))
    b = panel.load_baseline([str(p)], THIS_RUN)
    assert "deadbeefdeadbeef" in b.keys
    assert b.raised_before(_canonical("a.py", "leaky handle"))


def test_an_unreadable_baseline_is_reported_not_swallowed(tmp_path):
    """Its absence makes every finding look new — which reads as "the fix broke
    things", the exact opposite of the truth. Silence here is worse than an error."""
    b = panel.load_baseline([str(tmp_path / "nope.json")], THIS_RUN)
    assert b.keys == set() and b.problems and "unreadable" in b.problems[0]


def test_a_malformed_round_costs_the_verdict_its_confidence_not_the_review(tmp_path):
    """Every other defect in a baseline is downgraded to a problems entry. An
    unguarded int() made this one raise out of run() — after the diff was fetched
    and every reviewer CLI had been paid for."""
    path = _payload(tmp_path, "r1.json", "two", ["real bug"])
    b = panel.load_baseline([path], THIS_RUN)
    assert any("malformed round" in p for p in b.problems)
    # ...and its findings are still counted, which is the point of not raising.
    assert b.raised_before(_canonical("a.py", "real bug"))


def test_another_prs_baseline_is_reported_and_not_counted(tmp_path):
    """A stale or cross-wired payload is not a thinner baseline, it is a wrong
    one: its keys make this PR's real findings read as repeated and can stop the
    loop a round early."""
    path = _payload(tmp_path, "other.json", 1, ["someone else's bug"], pr=99)
    b = panel.load_baseline([path], THIS_RUN)
    assert b.keys == set() and b.rounds == set()
    assert any("another review's" in p and "pr=99" in p for p in b.problems)


def test_a_baseline_that_is_not_earlier_is_reported_and_not_counted(tmp_path):
    """Reported AND excluded, like the cross-PR case. A current or future payload
    that still loaded made genuinely new findings read as repeated, which ends the
    loop a round early on findings nobody has fixed."""
    path = _payload(tmp_path, "r2.json", 2, ["x"])
    b = panel.load_baseline([path], THIS_RUN)
    assert any("not earlier than" in p for p in b.problems)
    assert b.keys == set() and b.rounds == set()
    assert not b.raised_before(_canonical("a.py", "x"))


def test_a_baseline_that_does_not_say_whose_it_is_is_not_believed(tmp_path):
    """The identity check only rejected fields that were PRESENT and unequal, so a
    hand-edited or truncated payload omitting them suppressed this run's findings
    on the word of a file that never said which review it came from."""
    path = tmp_path / "anon.json"
    path.write_text(json.dumps({"round": 1, "to_fix": [_serialised("a.py", "x")]}))
    b = panel.load_baseline([str(path)], THIS_RUN)
    assert b.keys == set() and b.rounds == set()
    assert any("does not say which review" in p for p in b.problems)


def test_a_baseline_written_from_another_checkout_is_the_same_review(tmp_path):
    """`repo` is the local directory's name, not the review's. /panel-review-pr's
    parallel mode gives every PR a throwaway worktree, so round 1 writing
    "quarterback-feat-issue-24" and round 2 asking as "quarterback" is the normal
    case — and rejecting the baseline for it would blame the review for where it
    was run. `github` + `pr` are what name a review."""
    path = tmp_path / "elsewhere.json"
    path.write_text(json.dumps({
        "round": 1, "repo": "board-worktree-r1", "github": "acme/board", "pr": 34,
        "to_fix": [_serialised("a.py", "real bug")],
    }))
    b = panel.load_baseline([str(path)], THIS_RUN)
    assert b.problems == [] and b.rounds == {1}
    assert b.raised_before(_canonical("a.py", "real bug"))
    # ...but a baseline from a different PR is still a wrong baseline, not a
    # thinner one.
    other = tmp_path / "other-pr.json"
    other.write_text(json.dumps({
        "round": 1, "repo": "acme", "github": "acme/board", "pr": 99,
        "to_fix": [_serialised("a.py", "real bug")],
    }))
    assert panel.load_baseline([str(other)], THIS_RUN).keys == set()


def test_a_round_two_with_no_baseline_at_all_is_a_problem(tmp_path):
    """Without one, every finding reads as new, the report prints "N of N raised by
    no earlier round (0 known from 0 earlier rounds)", and the round would be free
    to record a CONFIDENT verdict on a comparison it never made."""
    b = panel.load_baseline([], THIS_RUN)
    assert any("no --baseline" in p for p in b.problems)
    # ...and a first round is not missing anything.
    assert panel.load_baseline([], {**THIS_RUN, "round": 1}).problems == []


def test_a_path_spelled_short_is_the_same_defect_as_the_full_one(tmp_path):
    """Reviewers spell paths differently — `reviews.py` and `app/api/reviews.py`
    are one file. A round where only the short-path reviewer raised the defect
    hashes to a different key AND used to fail the exact-file title check, so a
    persistent defect counted as new and bought a fix cycle nobody needed."""
    p = tmp_path / "r1.json"
    p.write_text(json.dumps({
        "round": 1, "repo": "acme", "github": "acme/board", "pr": 34,
        "to_fix": [_serialised("app/api/reviews.py", "stop is parsed and discarded")],
    }))
    b = panel.load_baseline([str(p)], THIS_RUN)
    assert b.raised_before(_canonical("reviews.py", "stop is parsed and discarded"))
    # Not a licence to merge two distinct files that merely end in the same name.
    assert not b.raised_before(
        _canonical("harness/loops/reviews.py", "stop is parsed and discarded"))


def test_a_cycle_is_inherited_from_the_earliest_baseline(tmp_path):
    """Every round of one cycle carries the same id, so the board can tell "the
    re-review of THIS declaration" from "whatever ran next on this PR". Taken from
    the earliest ROUND, not from whichever --baseline was listed first — a round 1
    that predates the field carries only its run_key, which is the same thing."""
    r1 = _payload(tmp_path, "r1.json", 1, ["a"], run_key="cyc-1")
    r2 = _payload(tmp_path, "r2.json", 2, ["b"], cycle="cyc-1", run_key="run-2")
    b = panel.load_baseline([r2, r1], {**THIS_RUN, "round": 3})
    assert b.cycle == "cyc-1" and b.rounds == {1, 2} and b.problems == []
    # No usable baseline, no inherited cycle — the run mints its own.
    assert panel.load_baseline([], {**THIS_RUN, "round": 1}).cycle is None


def test_a_baseline_from_another_cycle_is_reported_and_not_counted(tmp_path):
    """Two agents looping one PR at once produce two cycles, and their keys are
    unrelated. Merged into one history, a finding only the OTHER cycle raised reads
    as a repeat here — which can suppress the fix round it needed. The stored cycle
    id exists precisely to make that checkable, and this was the one place the
    stored fact went unchecked."""
    mine = _payload(tmp_path, "r1.json", 1, ["a"], cycle="cyc-A")
    theirs = _payload(tmp_path, "other.json", 2, ["b"], cycle="cyc-B")
    b = panel.load_baseline([mine, theirs], {**THIS_RUN, "round": 3})
    assert b.cycle == "cyc-A" and b.rounds == {1}
    assert any("cycle cyc-B" in p for p in b.problems)
    assert b.raised_before(_canonical("a.py", "a"))
    assert not b.raised_before(_canonical("a.py", "b"))


def test_two_baselines_for_one_round_say_so_rather_than_picking_by_hex(tmp_path):
    """`min((round, cycle))` fell through to comparing opaque hex ids when two
    payloads shared a round, so the winner was lexicographic — neither the earliest
    nor the first listed, and contradicting the rule written beside it. The tie is
    now taken in order, and the ambiguity is reported rather than resolved in
    silence."""
    first = _payload(tmp_path, "a.json", 1, ["a"], cycle="zzz")
    second = _payload(tmp_path, "b.json", 1, ["b"], cycle="zzz")
    b = panel.load_baseline([first, second], THIS_RUN)
    assert b.cycle == "zzz"
    assert any("two payloads for one round" in p for p in b.problems)


def test_a_baseline_is_not_rejected_for_an_identity_this_run_cannot_supply(tmp_path):
    """The check tested key PRESENCE, so `{"repo": None}` — what the documented
    `panel.py --pr N` invocation produced before --repo resolved a default —
    rejected EVERY baseline for "not saying which review it is from". Round 2 then
    loaded nothing, called every finding new and could never record a confident
    stop: the round diff silently no-opped for the workflow it documents."""
    path = _payload(tmp_path, "r1.json", 1, ["real bug"])
    b = panel.load_baseline([path], {**THIS_RUN, "repo": None})
    assert b.problems == [] and b.rounds == {1}
    assert b.raised_before(_canonical("a.py", "real bug"))
    # ...and a field the caller DOES know is still required to be present.
    anon = tmp_path / "anon.json"
    anon.write_text(json.dumps({"round": 1, "to_fix": [_serialised("a.py", "x")]}))
    assert any("does not say which review" in p
               for p in panel.load_baseline([str(anon)], THIS_RUN).problems)


def test_earlier_rounds_are_counted_not_guessed_from_the_highest_label(tmp_path):
    """Baselines for rounds 1 and 3 are two earlier rounds. Reporting max(round)
    as the count invents a round nobody ran, and it prints on the PR comment."""
    b = panel.load_baseline(
        [_payload(tmp_path, "r1.json", 1, ["a"]), _payload(tmp_path, "r3.json", 3, ["b"])],
        {**THIS_RUN, "round": 4})
    assert len(b.rounds) == 2


def test_a_reworded_finding_is_the_same_defect_as_far_as_the_round_diff_goes(tmp_path):
    """The key is file + normalised title, and the title is the reporters' own — so
    a reviewer that re-words its report between rounds would otherwise land a
    persistent defect in `new_findings` and report the fix as having broken
    something."""
    path = _payload(tmp_path, "r1.json", 1, ["unused import in the header"])
    b = panel.load_baseline([path], THIS_RUN)
    assert b.raised_before(_canonical("a.py", "Unused import in the header!"))
    assert b.raised_before(_canonical("a.py", "unused imports in the header"))
    # Not a licence to swallow a different defect in the same file.
    assert not b.raised_before(_canonical("a.py", "session is never closed"))
    # ...nor the same words about another file.
    assert not b.raised_before(_canonical("b.py", "unused import in the header"))


def test_a_plural_ending_in_es_still_matches_its_own_singular(tmp_path):
    """The commonest noun class in a review title ends in a plain `s` after an
    `e` — files, lines, nodes, values, handles. An `es` rule ahead of the `s` one
    took two characters off every one of them ("files" -> "fil") while the
    singular stemmed to itself, so singular and plural never matched and the
    reword fallback never fired for the words it exists for: a persistent defect
    reworded from "handle" to "handles" between rounds read as one no earlier
    round raised, which prints as the fix having broken something."""
    path = _payload(tmp_path, "r1.json", 1, ["the stale node handle"])
    b = panel.load_baseline([path], THIS_RUN)
    assert b.raised_before(_canonical("a.py", "the stale node handles"))
    assert panel._stem("files") == panel._stem("file")
    assert panel._stem("values") == panel._stem("value")
    # ...and two different words are still two different words.
    assert not b.raised_before(_canonical("a.py", "the stale node cache"))


def test_one_material_word_apart_is_two_defects_however_alike_the_titles_read(tmp_path):
    """Findings share long boilerplate and differ in one noun. A character ratio
    puts these at 0.93, and calling the second a repeat drops it from
    `new_findings` and out of the fixer's brief entirely — a lost defect, which is
    strictly worse than the wasted round a false "new" costs."""
    path = _payload(tmp_path, "r1.json", 1, ["the N+1 query in the user loop"])
    b = panel.load_baseline([path], THIS_RUN)
    assert b.raised_before(_canonical("a.py", "the N+1 query in the user loop"))
    assert not b.raised_before(_canonical("a.py", "the N+1 query in the order loop"))
    # ...and a genuinely reworded title is still absorbed, which is the whole
    # reason the fallback exists.
    assert b.raised_before(_canonical("a.py", "The N+1 queries in the user loop!"))


# ---- the stopping rule -----------------------------------------------------

def _confirmed(sev):
    return _canonical("a.py", "t", severity=sev)


def test_new_findings_earn_another_round():
    d = panel.round_stop(1, 3, ["k1"], [_confirmed("P3")], [])
    assert d["stop"] is False and "1 finding" in d["reason"]


def test_a_blocker_still_confirmed_earns_another_round_even_with_nothing_new():
    """A P2 raised again after the fix is a P2 that was not fixed. Severity does
    this regardless of what any reviewer declared."""
    d = panel.round_stop(2, 3, [], [_confirmed("P2")], [])
    assert d["stop"] is False and "P1/P2" in d["reason"]


def test_a_dry_round_of_polish_is_finished():
    d = panel.round_stop(2, 3, [], [_confirmed("P4")], [])
    assert d["stop"] is True and d["confident"] is True and "dry" in d["reason"]


def test_the_cap_stops_the_loop_but_is_not_recorded_as_convergence():
    """A verdict of "we ran out of rounds" and one of "there was nothing left"
    are different facts, and only one of them is a clean bill of health."""
    d = panel.round_stop(2, 2, ["k1", "k2"], [_confirmed("P1")], [])
    assert d["stop"] is True and d["confident"] is False
    assert "round cap (2)" in d["reason"] and "unreviewed" in d["reason"]


def test_a_declaration_vetoes_the_verdict_but_never_extends_the_loop():
    """A truncated reviewer is truncated again next round at the same budget, so
    treating that as a reason to go again is a loop with no exit. It is a reason
    to stop CALLING the PR clean."""
    d = panel.round_stop(2, 3, [], [], ["codex saw 60,000 of 118,402 diff chars"])
    assert d["stop"] is True and d["confident"] is False and d["veto"]


def test_a_finding_still_there_after_the_fix_earns_another_round():
    """A judge-confirmed P3 the last round already raised is a finding the fixer was
    told about and did not fix. Recording a veto and stopping anyway ended the cycle
    with a confirmed defect present — /panel-review-pr's bar is every confirmed
    finding, not every P1/P2."""
    c = _confirmed("P3")
    d = panel.round_stop(2, 3, [], [c], [], repeated={c.key})
    assert d["stop"] is False and "still outstanding" in d["reason"]
    # No veto, though: the veto list answers "why this round's QUIET is not
    # evidence of a quiet PR", and this round was not quiet — its repeat is
    # already the stated reason for going again.
    assert d["veto"] == []


def test_the_cap_is_what_ends_an_argument_about_a_repeated_p4():
    """Two reviewers can disagree about a P4 forever, so rule 3 needs a floor. The
    cap is it — and a cap reached with work outstanding is not convergence."""
    c = _confirmed("P4")
    d = panel.round_stop(2, 2, [], [c], [], repeated={c.key})
    assert d["stop"] is True and d["confident"] is False
    assert "round cap (2)" in d["reason"] and "unreviewed" in d["reason"]


def test_a_baseline_that_could_not_be_read_also_costs_the_verdict_its_confidence():
    d = panel.round_stop(2, 3, [], [], [], baseline_ok=False)
    assert d["stop"] is True and d["confident"] is False


# ---- the finding no round can close (#221) ---------------------------------
#
# A fixer may report that a finding says the APPROACH is wrong rather than the
# code and write no patch (`review-pr.md` step 3a), and `panel-review-pr.md` §5
# forbids ever handing it to another fixer. Both rules are right and together
# they jammed the loop: the finding is outstanding, nothing may fix it, so rules
# 1-3 said "go again" every round until the cap — the mechanism built to stop a
# cycle circling a premise guaranteed it ran to the cap instead.


def _c(sev, file="a.py", title="t"):
    """A judged finding whose `.key` the test then reads.

    The key is a read-only property derived from the file and the reporters'
    words (:func:`_defect_key`), so a test cannot hand one in — two findings get
    two keys by differing in what they are ABOUT, which is also the only way they
    differ in production."""
    return _canonical(file, title, severity=sev)


def test_an_escalated_finding_alone_does_not_earn_another_round():
    """The jam itself. Without the register this is `stop: False` at every round
    until the cap, on a finding no fix pass is allowed to touch."""
    c = _c("P2")
    assert panel.round_stop(2, 5, [], [c], [], repeated={c.key})["stop"] is False
    d = panel.round_stop(2, 5, [], [c], [], repeated={c.key}, escalated=[c.key])
    assert d["stop"] is True
    assert "escalated" in d["reason"] and "await a human" in d["reason"]


def test_a_stop_holding_an_escalation_is_never_convergence():
    """It is a stop and it is not clean: the PR carries a question only a human
    closes. Reported through the existing veto, so `confident` falls out by the
    rule that was already there and every consumer of it needs no new field."""
    c = _c("P2")
    d = panel.round_stop(2, 5, [], [c], [], repeated={c.key}, escalated=[c.key])
    assert d["confident"] is False
    assert any("no round can close them" in v for v in d["veto"])
    assert d["escalated_outstanding"] == [c.key]


def test_it_is_never_reported_as_dry():
    """A "dry" verdict is a claim that nothing was raised. Something was raised,
    and is unanswered — a reader reconciling "dry" against an open premise question is
    being told something untrue about why the loop stopped."""
    c = _c("P4")
    d = panel.round_stop(2, 5, [], [c], [], escalated=[c.key])
    assert "dry" not in d["reason"]


def test_real_work_beside_an_escalation_still_earns_another_round():
    """The mixed case, which is the one a "stop on any escalation" rule gets
    wrong: one escalation beside a live P2 must not end the cycle, or the fixes
    that WERE made in the same pass go unreviewed."""
    held, live = _c("P3"), _c("P2", file="b.py", title="a different defect")
    assert held.key != live.key
    d = panel.round_stop(2, 5, [], [held, live], [], escalated=[held.key])
    assert d["stop"] is False and "P1/P2" in d["reason"]


def test_the_cycle_stops_as_soon_as_only_escalations_remain():
    """...and the round after, when the P2 is fixed, it stops — on the work being
    done rather than on the counter running out. That is the whole point: #67
    calls the cap arbitrary, and this is the case where the loop can know."""
    held = _c("P3")
    d = panel.round_stop(3, 5, [], [held], [], repeated={held.key}, escalated=[held.key])
    assert d["stop"] is True and d["round"] == 3 and d["max_rounds"] == 5


def test_a_finding_escalated_and_raised_again_as_NEW_is_still_held():
    """A later round can re-derive the same defect under the same key and report
    it as new. Filtering only `outstanding` would let rule 1 fire on it and buy
    the round nobody could act on."""
    held = _c("P3")
    d = panel.round_stop(2, 5, [held.key], [held], [], escalated=[held.key])
    assert d["stop"] is True


def test_escalating_something_that_is_not_outstanding_costs_nothing():
    """A key for a finding this round did not raise is not an error — an
    escalation stays open across rounds whether or not a reviewer mentions it
    again — but it must not invent a reason to stop or to go on."""
    fresh = _c("P2")
    d = panel.round_stop(2, 5, [fresh.key], [fresh], [],
                         escalated=["a0b1c2d3e4f56789"])
    assert d["stop"] is False and "1 finding" in d["reason"]


def test_a_register_key_this_round_never_raised_does_not_veto_a_dry_round():
    """Found by the codex seat on this diff's own review. The register is
    inherited and only grows, so holding the whole of it against every later round
    meant one stale key — a premise a human has since ANSWERED, a withdrawn
    finding, a typo — made the cycle permanently unable to report convergence, on
    rounds that were genuinely dry. A veto that can never be cleared is the loud
    wrong signal a reader learns to skip past."""
    d = panel.round_stop(3, 5, [], [], [], escalated=["a0b1c2d3e4f56789"])
    assert d["stop"] is True and d["confident"] is True
    assert "dry" in d["reason"] and d["veto"] == []
    assert d["escalated_outstanding"] == []


def test_a_typo_does_not_quietly_end_the_cycle_either():
    """The same key, with real work outstanding: it must not subtract a finding
    nobody escalated, and `--escalated` naming nothing is reported in
    `config_notes` by the caller rather than acted on here."""
    live = _c("P2")
    d = panel.round_stop(3, 5, [], [live], [], repeated={live.key},
                         escalated=["a0b1c2d3e4f56789"])
    assert d["stop"] is False and "P1/P2" in d["reason"]


def test_an_escalated_P1_alone_stops_the_cycle_with_the_blocker_PRESENT():
    """Rule 2 says a P1/P2 outstanding earns a round "whatever anyone declared",
    and this is the case where a declaration overrides it — the largest behavioural
    consequence of #221, and the one a reader of rule 2 will get wrong. Asserted
    deliberately at P1, the severity the other tests here do not use: the loop
    stops with a judge-confirmed BLOCKER present, because no fix round may touch
    it, and it says so rather than reporting convergence."""
    p1 = _c("P1")
    assert panel.round_stop(2, 5, [], [p1], [])["stop"] is False
    d = panel.round_stop(2, 5, [], [p1], [], escalated=[p1.key])
    assert d["stop"] is True and d["confident"] is False
    assert "P1/P2" not in d["reason"] and "await a human" in d["reason"]
    assert d["escalated_outstanding"] == [p1.key]


@pytest.mark.parametrize("field", ["repeated", "escalated"])
def test_a_bare_STRING_of_keys_is_refused_rather_than_iterated(field):
    """A `str` is itself iterable, so `escalated=key` instead of `escalated=[key]`
    — the natural slip now that both take keys — made `held` a set of single
    characters, `blocking` empty against real keys, and the escalation silently
    ignored while the cycle ran to its cap: the #221 jam, from inside the fix for
    it. `repeated="<key>"` is the same slip the other way and reports a repeat
    count invented out of the string's distinct characters."""
    c = _c("P3")
    with pytest.raises(TypeError) as bad:
        panel.round_stop(2, 5, [], [c], [], **{field: c.key})
    assert "not one string" in str(bad.value)


def test_the_COUNT_this_used_to_take_is_named_rather_than_left_to_a_TypeError():
    """`repeated` was `int = 0` until #221. A caller outside that change still
    passing a count now fails — which is right, since a count cannot express the
    escalation subtraction — but `'int' object is not iterable` says nothing about
    what to pass instead."""
    c = _c("P3")
    with pytest.raises(TypeError) as bad:
        panel.round_stop(2, 5, [], [c], [], repeated=1)
    assert "not a count" in str(bad.value)


def test_a_DICT_of_keys_is_still_read_as_its_keys():
    """The production call site passes the register itself (`{key: round}`), so the
    guard above must reject only the shapes that iterate into something other than
    keys."""
    c = _c("P3")
    d = panel.round_stop(2, 5, [], [c], [], escalated={c.key: 1})
    assert d["stop"] is True and d["escalated_outstanding"] == [c.key]


def test_an_empty_declaration_changes_nothing():
    """`--escalated ''` and no flag at all are the same run. Guarded because the
    filter is a membership test, and an empty string in the set would quietly
    match a finding whose key failed to serialise."""
    c = _c("P2")
    plain = panel.round_stop(2, 5, [], [c], [], repeated={c.key})
    empty = panel.round_stop(2, 5, [], [c], [], repeated={c.key}, escalated=["", None])
    assert plain["stop"] == empty["stop"] is False
    assert empty["escalated_outstanding"] == []


# ---- what makes a quiet round suspect --------------------------------------

def test_the_veto_names_every_way_a_round_can_look_quiet_without_being_quiet():
    """`code_blind: False` on pi is load-bearing and spelled out rather than left
    to the default: a declaration is evidence about the round only from a seat
    that could have read the answer. The blind case — which is every seat today —
    is the test below."""
    meta = {
        "claude": {"ran": True, "truncated": True, "max_diff_chars": 60_000},
        "codex": {"ran": False, "skip": "codex (gpt): exited 1 (429 rate limited)"},
        "pi": {"ran": True, "code_blind": False,
               "could_not_assess": ["the amendment path"]},
        "antigravity": {"ran": True, "unstructured": True},
    }
    why = panel.coverage_veto(meta, judge_skip="judge: claude CLI absent",
                              flagged=2, diff_chars=118_402, ci_status="PASS")
    joined = " | ".join(why)
    assert "claude saw 60,000 of 118,402" in joined
    assert "codex did not run" in joined
    assert "pi could not assess: the amendment path" in joined
    assert "antigravity returned no structured reply" in joined
    assert "not adjudicated" in joined
    assert "2 finding(s) whose reporter said the FIX needs re-reading" in joined


def test_a_seat_this_box_does_not_carry_is_reported_without_vetoing():
    """A reviewer whose CLI is not installed is a fact about the HOST, not about
    the round: it is absent every round, so a veto on it makes `confident`
    permanently unreachable on the headless machines — which is exactly where
    the unattended loops run and where the signal has to mean something. A repo
    that lists a workstation-only vendor (this one lists two) would otherwise
    buy every unattended run a standing veto and teach the reader to discount
    all of them. The skip is still reported; it is just not evidence."""
    absent = {"antigravity": {"ran": False, "absent": True,
                              "skip": "antigravity (m): CLI absent"},
              "pi": {"ran": False, "absent": True, "skip": "pi (kimi): CLI absent"},
              "claude": {"ran": True}}
    assert panel.coverage_veto(absent, None, 0, 1_000, ci_status="PASS") == []
    assert panel.round_stop(1, 2, [], [], [])["confident"] is True
    # Every other way of not running still says something about this round.
    crashed = {"antigravity": {"ran": False, "skip": "antigravity (m): exited 1 (boom)"},
               "claude": {"ran": True}}
    assert panel.coverage_veto(crashed, None, 0, 1_000, ci_status="PASS") == [
        "antigravity did not run (antigravity (m): exited 1 (boom))"]


def test_a_box_carrying_none_of_the_reviewer_clis_cannot_stop_confidently():
    """The floor under the exemption above. Absent seats are exempted one at a
    time, so a host that carries NONE of them produces an empty veto list — and
    `confident` is `not veto`, so the round reports a confident stop on a diff
    nobody read. That is the strongest wrong signal the panel can emit, and it
    lands on exactly the unattended hosts the exemption was added for."""
    none_ran = {"antigravity": {"ran": False, "absent": True, "skip": "a: CLI absent"},
                "pi": {"ran": False, "absent": True, "skip": "p: CLI absent"}}
    veto = panel.coverage_veto(none_ran, None, 0, 1_000, ci_status="PASS")
    assert veto == ["no reviewer ran — nothing read this diff"]
    assert panel.round_stop(1, 2, [], [], veto)["confident"] is False


def test_absence_is_recorded_state_rather_than_a_message_tail():
    """The exemption reads `meta["absent"]`, not the end of the skip line.

    Both directions of the message-matching version are wrong. A genuine failure
    whose composed reason happens to end in those words would escape the veto;
    and the day the absent branch's message gains a suffix — a hint, an install
    pointer — the standing veto the exemption exists to remove comes back, with
    nothing failing to say so."""
    lookalike = {"codex": {"ran": False, "skip": "codex (gpt): exited 1 — no CLI absent"},
                 "claude": {"ran": True}}
    assert panel.coverage_veto(lookalike, None, 0, 1_000, ci_status="PASS") == [
        "codex did not run (codex (gpt): exited 1 — no CLI absent)"]
    decorated = {"codex": {"ran": False, "absent": True,
                           "skip": "codex (gpt): CLI absent — install it first"},
                 "claude": {"ran": True}}
    assert panel.coverage_veto(decorated, None, 0, 1_000, ci_status="PASS") == []


def test_a_reviewer_whose_cli_is_missing_records_that_as_state(monkeypatch):
    """Where the flag comes from — the one branch that may set it."""
    monkeypatch.setattr(panel.shutil, "which", lambda _: None)
    got = panel.review_llm("antigravity", "m", "p")
    assert got.absent is True and got.skip.endswith(panel.CLI_ABSENT)


def test_a_seat_that_cannot_read_the_code_declares_without_vetoing():
    """The measured cost of the sandbox, and why a constant must not vote.

    Every LLM seat reviews from the diff alone — an empty `member_sandbox` cwd and
    no file tools — so "I could not read a function this diff does not change" is
    true of every round it sits. `round_stop` computes `confident` as `not veto`,
    so leaving it in denied a confident stop to any PR that so much as REFERENCES
    a file it does not touch, which is most of them: the one signal deciding
    whether to spend another round carried no information.

    Measured on PR #160 round 1 — 19 veto lines, 16 of them declarations, and
    NINE of those asked about a file in this very repo (whether
    `mcp_server/__init__.py` imports the MCP SDK; `QuarterbackClient`'s default
    timeout; `worktree-holder`'s exit codes). The orchestrator answered all nine
    with grep in about four minutes. That is work the panel was outsourcing to
    whoever read its output, and only when somebody happened to.

    The declarations are still REPORTED — `run` renders them from `reviewer_meta`
    regardless of the veto, and they are worth reading. What they no longer do is
    spend the round's confidence."""
    blind = {"claude": {"ran": True, "code_blind": True,
                        "could_not_assess": ["whether load_repo_cfg validates the path",
                                             "the other two CI jobs' conventions"]},
             "codex": {"ran": True, "code_blind": True,
                       "could_not_assess": ["migrations/versions/"]}}
    assert panel.coverage_veto(blind, None, 0, 1_000, ci_status="PASS") == []
    assert panel.round_stop(1, 2, [], [], [])["confident"] is True
    # A seat that COULD have read the tree is making a claim about this round.
    sighted = {"claude": {"ran": True, "code_blind": False,
                          "could_not_assess": ["migrations/versions/"]}}
    assert panel.coverage_veto(sighted, None, 0, 1_000, ci_status="PASS") == [
        "claude could not assess: migrations/versions/"]


def test_blindness_is_recorded_state_rather_than_read_out_of_the_declaration():
    """The exemption reads `meta["code_blind"]`, never the text of the gap.

    Both directions are wrong, and they are the same two `absent` records. The
    entries are free-form prose a model wrote, so a regex over them would exempt a
    genuine round-specific gap whose wording happened to match ("could not read
    the fix") while still counting the structural one that did not ("no view of
    the caller"). And the day a vendor's phrasing drifts, a rule keyed on wording
    silently changes which rounds can stop confidently, with nothing failing to
    say so.

    So a blind seat is exempt even when its gap reads like a fact about the round,
    and a sighted seat's is counted even when it reads like a fact about the
    design. What decides is how the seat was RUN."""
    round_shaped = {"codex": {"ran": True, "code_blind": True,
                              "could_not_assess": ["the fix in this very diff"]},
                    "claude": {"ran": True, "code_blind": True}}
    assert panel.coverage_veto(round_shaped, None, 0, 1_000, ci_status="PASS") == []
    design_shaped = {"codex": {"ran": True, "code_blind": False,
                               "could_not_assess": ["anything outside the diff"]},
                     "claude": {"ran": True, "code_blind": False}}
    assert panel.coverage_veto(design_shaped, None, 0, 1_000, ci_status="PASS") == [
        "codex could not assess: anything outside the diff"]


def test_a_blind_panel_still_vetoes_every_other_way_of_coming_up_short():
    """The exemption is one line of this function and must not read as a pardon.

    A blind seat that crashed, was cut by a budget, or returned something
    unparseable has told you something about THIS round, and so has a judge that
    never ruled. If exempting the declarations quietly took these with it, the
    change would have replaced a signal that was never positive with one that is
    never negative — which is the same defect wearing the opposite sign."""
    meta = {
        "claude": {"ran": True, "code_blind": True, "truncated": True,
                   "max_diff_chars": 60_000, "could_not_assess": ["the caller"]},
        "codex": {"ran": False, "code_blind": True,
                  "skip": "codex (gpt): exited 1 (429 rate limited)"},
        "pi": {"ran": True, "code_blind": True, "unstructured": True},
    }
    why = panel.coverage_veto(meta, judge_skip="judge: claude CLI absent",
                              flagged=1, diff_chars=118_402, ci_status="PASS")
    joined = " | ".join(why)
    assert "claude saw 60,000 of 118,402" in joined
    assert "codex did not run" in joined
    assert "pi returned no structured reply" in joined
    assert "not adjudicated" in joined
    assert "1 finding(s) whose reporter said the FIX needs re-reading" in joined
    # ...and the one thing that IS structural stayed out.
    assert "could not assess" not in joined


def test_a_partial_meta_dict_still_vetoes_its_declarations():
    """Which way the default falls, pinned deliberately.

    `code_blind` absent from a meta dict means "nobody recorded how this seat was
    run", and the answer to that has to be the veto. Failing closed costs a round
    its confidence; failing open claims a diff was read whole on the strength of a
    key nobody set — and that is the direction every other exemption in this file
    is written to avoid."""
    unrecorded = {"claude": {"ran": True, "could_not_assess": ["the caller"]}}
    assert panel.coverage_veto(unrecorded, None, 0, 1_000, ci_status="PASS") == [
        "claude could not assess: the caller"]


def test_the_kernel_ceiling_is_reported_without_vetoing():
    """`agy`'s prompt travels in argv and the kernel caps one element, so on a
    large diff that seat structurally cannot be handed all of it — on PR #160,
    116,771 of 175,547 chars, 66.5%. Constant, like an absent CLI: it is true of
    every round on this box at this diff size, so it cannot separate a quiet round
    from a broken one.

    A BUDGET is a different fact. Someone typed it, it can be raised, and
    `diff_budget` honours it precisely so the consequence gets surfaced — so
    truncation by config still vetoes. `argv_capped` is what tells the two
    apart."""
    kernel = {"antigravity": {"ran": True, "truncated": True, "argv_capped": True,
                              "max_diff_chars": 116_771, "code_blind": True},
              "claude": {"ran": True, "code_blind": True}}
    assert panel.coverage_veto(kernel, None, 0, 175_547, ci_status="PASS") == []
    budget = {"antigravity": {"ran": True, "truncated": True, "argv_capped": False,
                              "max_diff_chars": 6_000, "code_blind": True},
              "claude": {"ran": True, "code_blind": True}}
    assert panel.coverage_veto(budget, None, 0, 175_547, ci_status="PASS") == [
        "antigravity saw 6,000 of 175,547 diff chars"]


def test_a_panel_whose_every_running_seat_was_argv_capped_cannot_stop_confidently():
    """The floor under the exemption above, and it is the same floor the absent
    seats needed. Exempting per seat means a panel whose ONLY running seat is the
    argv-bound one produces an empty veto list — and `confident` is `not veto`, so
    the round claims a confident stop on a diff nothing saw whole. Reachable by
    `--reviewers antigravity`, or by a repo that switched the others off, and it
    lands on the unattended loops where the claim is believed."""
    alone = {"antigravity": {"ran": True, "truncated": True, "argv_capped": True,
                             "max_diff_chars": 116_771, "code_blind": True}}
    veto = panel.coverage_veto(alone, None, 0, 175_547, ci_status="PASS")
    assert veto == ["every reviewer that ran was cut by the argv ceiling — "
                    "nothing read this diff whole"]
    assert panel.round_stop(1, 2, [], [], veto)["confident"] is False
    # One seat that saw the whole thing is enough to lift it — that seat's reading
    # is what the round rests on, and it is not truncated.
    beside = dict(alone, claude={"ran": True, "code_blind": True})
    assert panel.coverage_veto(beside, None, 0, 175_547, ci_status="PASS") == []


def test_a_panel_with_nothing_to_declare_vetoes_nothing():
    meta = {"claude": {"ran": True, "truncated": False, "could_not_assess": []}}
    assert panel.coverage_veto(meta, None, 0, 1_000, ci_status="PASS") == []


# ---- the master rules on coverage, findings or not -------------------------

def _judge_returning(monkeypatch, reply):
    seen = {}

    def fake_run_cli(args, label, timeout=panel.CLI_TIMEOUT, attempts=3, stdin_text=None,
                     on_output=None, replied=None, cwd=None):
        seen["prompt"] = stdin_text
        return reply, None

    monkeypatch.setattr(panel.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(panel_seats, "run_cli", fake_run_cli)
    return seen


def test_a_round_with_no_findings_still_gets_its_coverage_adjudicated(monkeypatch):
    """The round where "clean versus could-not-tell" matters MOST is the one with
    no findings: two members that could not read the migration and a third
    reporting clean is a split, and a finding count of zero says nothing about
    it. Returning early on an empty group list skipped exactly that."""
    seen = _judge_returning(monkeypatch, json.dumps(
        {"verdicts": [], "coverage_note": "the migration is unread; claude's clean is partial"}))
    findings, skip, note = panel.adjudicate(
        [], "diff", "opus", 34, coverage={"codex": ["the migration"], "claude": []})
    assert findings == [] and skip is None
    assert note.note.startswith("the migration is unread")
    # Numbered, one line per declaration: the number is what the judge's typed
    # coverage ruling points at, so a listing that joined a seat's gaps onto one
    # line would leave it nothing to point AT (#547).
    assert "- [0] codex could not assess: the migration" in seen["prompt"]


def test_an_ambiguous_judge_reply_is_not_a_ruling(monkeypatch):
    """The reviewer path pays for an unresolved reply with one retry and its raw
    text kept; the judge has no second attempt, so the same reply takes the WHOLE
    round through `unruled` — every finding unjudged and a veto that says the
    round was not adjudicated. That asymmetry is what makes a new way to discard
    the judge's reply worth pinning: the failure has to be loud, because the
    alternative is a round that reads as triaged."""
    _judge_returning(monkeypatch, (
        '{"verdicts": [{"id": "F01", "members": [0], "real": true, '
        '"synthesis": "the handle is never closed"}]}\n'
        '{"verdicts": [{"id": "F01", "members": [0], "real": false, '
        '"synthesis": "the handle is closed by the context manager"}]}'))
    leak = panel.Finding("codex", "P2", "a.py", 1, "leak", "")
    out, skip, note = panel.adjudicate([[leak]], "diff", "", 34)
    assert [c.verdict for c in out] == ["unjudged"] and note.note == ""
    assert skip and "unparseable" in skip
    assert "not adjudicated" in " | ".join(panel.coverage_veto({}, skip, 0, 1_000, ci_status="PASS"))


def test_a_coverage_only_reply_is_not_a_judge_that_failed_to_rule(monkeypatch):
    """With nothing to adjudicate, an envelope carrying only the note is a
    complete answer — reporting it as unparseable would veto the round."""
    _judge_returning(monkeypatch, json.dumps({"coverage_note": "nothing unread"}))
    _, skip, note = panel.adjudicate([], "diff", "", 34,
                                     coverage={"codex": ["the schema"]})
    assert skip is None and note.note == "nothing unread"


def test_a_coverage_only_round_whose_judge_said_nothing_is_not_adjudicated(monkeypatch):
    """The findings path calls an unparseable reply a judge that failed to rule;
    the coverage-only path returned skip_reason=None for ANY unusable reply — prose,
    a crash-truncated answer, an object carrying neither key. `coverage_veto` then
    added no "the round was not adjudicated" entry and, with no other veto,
    `round_stop` recorded `stop: true, confident: true`: a confident clean verdict
    on a judge that produced nothing, in the one round where the split most needed
    adjudicating."""
    for reply in ("I had a look and it all seems fine, honestly.",
                  json.dumps({"summary": "fine"}),
                  '{"verdicts": [{"id": 1'):
        _judge_returning(monkeypatch, reply)
        findings, skip, note = panel.adjudicate([], "diff", "", 34,
                                                coverage={"codex": ["the migration"]})
        assert findings == [] and skip and "unparseable" in skip, reply


def test_nothing_found_and_nothing_declared_needs_no_judge(monkeypatch):
    monkeypatch.setattr(panel_seats, "run_cli", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("the judge must not run with nothing to rule on")))
    assert panel.adjudicate([], "diff", "", 34, coverage={"claude": []}) == ([], None, panel.CoverageRuling())


# ---- what the PR comment promises ------------------------------------------

PANEL_CFG = {"github": "acme/board", "path": "/tmp/acme-board",
             "_rules_baseline": ".harness-rules.sample",
             "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
             "review_panel": {}}


def _fake_adjudicate(clusters, diff, model, pr, budget=None, coverage=None, cwd=None, ci="", **_kw):  # **_kw: code_tree/budget_usd since #113
    """Every reported finding confirmed, one canonical record each — the judge's
    ruling is not what these tests are about."""
    flat = [f for grp in clusters for f in grp]
    return ([panel.Canonical(id=panel._finding_id(pr, i + 1), severity=f.severity,
                             file=f.file, line=f.line, synthesis=f.title,
                             verdict="confirmed", detail=f.detail, reported_by=[f],
                             rationale="real")
             for i, f in enumerate(flat)], None, panel.CoverageRuling())


def _stub_panel(monkeypatch, findings=None, title="feat: x", cfg=PANEL_CFG):
    """Every process a run would spawn, replaced — so what is under test is what
    the panel itself builds, not the CLIs."""
    if findings is None:
        findings = [panel.Finding("claude", "P3", "a.py", 3, "unused import")]
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": title, "additions": 3, "deletions": 1, "headRefOid": "abc"},
        diff="diff --git a/a.py b/a.py\n+x\n"))
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun(list(findings), None, 10, []))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", _fake_adjudicate)


#: A finding at or above the default `round_trigger_floor`/`fix_severity_floor` (#165).
#: `_stub_panel`'s own default is a P3, which is BELOW both floors — correctly, since
#: most tests here are about how a finding is rendered and recorded and a P3 exercises
#: the below-floor path — so any test whose subject is the loop GOING AGAIN has to
#: raise something that buys a round, or it is asserting the floors rather than the
#: rule it names.
BLOCKING = [panel.Finding("claude", "P2", "a.py", 3, "unvalidated input")]


def _report(monkeypatch, capsys, tmp_path, round_no, baseline=(), max_rounds=None,
            findings=None, title="feat: x", cfg=PANEL_CFG):
    """One whole panel run, so what is under test is the report it writes on the
    PR and the payload it writes beside it."""
    _stub_panel(monkeypatch, findings, title, cfg)
    out = tmp_path / f"r{round_no}.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline),
                     max_rounds=max_rounds) == 0
    return capsys.readouterr().out, str(out)


def test_a_review_only_run_does_not_promise_a_round_nobody_will_run(monkeypatch, capsys,
                                                                    tmp_path):
    """`--max-rounds` is the CALLER's cap and only /panel-review-pr drives the
    loop. A plain `/panel` read used to comment "round 1 of at most 2 — go
    again", telling the reader a re-review was coming when nothing would run it —
    with a "no earlier round raised" count that is vacuously every finding."""
    report, out = _report(monkeypatch, capsys, tmp_path, 1)
    assert "**Rounds:**" not in report and "go again" not in report
    assert "· round 1" not in report
    assert "unused import" in report
    # ...and the PAYLOAD says the same thing, which is the copy the board keeps.
    # It used to carry `round_stop` regardless, so `record_review` stored a plain
    # `/panel` read as a cycle mid-flight ("not stopped: 1 finding(s) no earlier
    # round raised") that nothing would ever advance.
    payload = json.loads(Path(out).read_text())
    assert payload["round_stop"] is None and payload["stop_reason"] is None
    # None, not 0: "the panel did not say" is the column's own state, and the
    # count would be the vacuous "every finding, against no earlier round".
    assert payload["new_findings"] is None and payload["new_finding_keys"] == []


def test_a_review_only_run_that_found_nothing_records_no_convergence(monkeypatch,
                                                                     capsys, tmp_path):
    """The other half of it: with no findings the payload said `stopped: true,
    stop_confident: true, "dry — no findings to fix"` — a confident-convergence
    record on the board for a PR that had no cycle at all."""
    _, out = _report(monkeypatch, capsys, tmp_path, 1, findings=[])
    payload = json.loads(Path(out).read_text())
    assert payload["round_stop"] is None and payload["stop_reason"] is None


def test_a_first_round_inside_a_cycle_still_reports_where_the_loop_stands(monkeypatch,
                                                                          capsys, tmp_path):
    """Naming the cap is what says this run belongs to a cycle — /panel-review-pr
    passes it on round 1, and that round's `go again` is a promise something will
    keep."""
    report, _ = _report(monkeypatch, capsys, tmp_path, 1, max_rounds=2,
                        findings=BLOCKING)
    assert "**Rounds:** round 1 of at most 2 — **go again**" in report


def test_a_re_review_says_which_round_it_is_and_where_the_loop_stands(monkeypatch, capsys,
                                                                      tmp_path):
    _, r1 = _report(monkeypatch, capsys, tmp_path, 1, max_rounds=2, findings=BLOCKING)
    report, out = _report(monkeypatch, capsys, tmp_path, 2, baseline=[r1], max_rounds=2,
                          findings=BLOCKING)
    assert "**Rounds:** round 2 of at most 2" in report
    # The same finding again is not fresh damage, and the ↻/🆕 marker stays off.
    assert "🆕" not in report
    assert "1 finding(s) an earlier round already raised" in report
    # ...and the payload says the same thing the report does.
    payload = json.loads(Path(out).read_text())
    assert payload["new_findings"] == 0
    assert payload["to_fix"][0]["new_this_round"] is False


def test_a_round_two_with_no_baseline_says_so_on_the_pr(monkeypatch, capsys, tmp_path):
    """The operator is told to list the vetoes. A round that never had a baseline
    cannot claim convergence, and "not convergence" with nothing listed leaves the
    reader to guess which of the reasons applied."""
    report, out = _report(monkeypatch, capsys, tmp_path, 2, max_rounds=3)
    assert "no --baseline" in report
    payload = json.loads(Path(out).read_text())
    assert payload["round_stop"]["confident"] is False
    assert any("no --baseline" in v for v in payload["round_stop"]["veto"])


def test_a_cycles_rounds_all_carry_the_same_id(monkeypatch, capsys, tmp_path):
    """Round 1 mints it, every later round inherits it from its earliest baseline.
    Without it "the round that answered this declaration" is the guess "whatever
    ran next on this PR", which credits one cycle's round 2 to another's round 1
    the moment two agents loop the same PR at once."""
    _, r1 = _report(monkeypatch, capsys, tmp_path, 1, max_rounds=3)
    r1_payload = json.loads(Path(r1).read_text())
    _, r2 = _report(monkeypatch, capsys, tmp_path, 2, baseline=[r1], max_rounds=3)
    assert json.loads(Path(r2).read_text())["cycle"] == r1_payload["cycle"]
    # A round 1 of a DIFFERENT cycle over the same PR is not the same cycle.
    _, other = _report(monkeypatch, capsys, tmp_path, 1, max_rounds=3)
    assert json.loads(Path(other).read_text())["cycle"] != r1_payload["cycle"]


def test_the_round_verdict_survives_a_report_cut_to_fit_a_comment():
    """`fit_comment` trims from the end and the verdict is the last block, so the
    one line the caller of a cycle acts on was the first thing to go — leaving a
    comment that lists findings and never says whether to go again."""
    verdict = "\n\n**Rounds:** round 2 of at most 2 — **stop**: dry, unreviewed"
    report = ("## Reviewer panel — PR #1\n\n### To fix\n"
              + "\n".join(f"- **P2** `a.py:{i}` — issue {i}" for i in range(5_000))
              + verdict)
    fitted = panel.fit_comment(report)
    assert len(fitted) <= panel.COMMENT_CHARS
    assert fitted.endswith(verdict) and "truncated" in fitted


def test_a_verdict_block_too_long_to_reserve_is_cut_rather_than_overflowing():
    """The tail was reserved whole and only the head was sliced, so `max(0, …)`
    clamped the SLICE and not the RESULT: a run with many vetoes returned a comment
    LONGER than the limit, and `--post` loses the whole thing to a hard API
    rejection. The verdict line survives; the veto list is what gets cut."""
    verdict = ("\n\n**Rounds:** round 2 of at most 2 — **stop**: dry, unreviewed\n"
               + "\n".join(f"  - ⚠️ codex could not assess area {i}" for i in range(4_000)))
    report = ("## Reviewer panel — PR #1\n\n### To fix\n"
              + "\n".join(f"- **P2** `a.py:{i}` — issue {i}" for i in range(5_000))
              + verdict)
    assert len(verdict) > panel.COMMENT_CHARS       # the tail alone does not fit
    fitted = panel.fit_comment(report)
    assert len(fitted) <= panel.COMMENT_CHARS
    assert "**Rounds:** round 2 of at most 2 — **stop**" in fitted


def test_a_baseline_problem_is_not_printed_twice_on_the_pr(monkeypatch, capsys, tmp_path):
    """It is deliberately both a config note and a veto — the payload needs both,
    since `config_notes` never reaches the board and the veto list is its only
    copy — but a reader of the comment saw the same sentence in two places and read
    it as two problems. The second appearance points at the first."""
    report, out = _report(monkeypatch, capsys, tmp_path, 2, max_rounds=3)
    assert report.count("nothing to compare against") == 1
    assert "the config note above" in report
    # ...and the payload still carries it in both roles, in full.
    payload = json.loads(Path(out).read_text())
    assert any("no --baseline" in n for n in payload["config_notes"])
    assert any("nothing to compare against" in v for v in payload["round_stop"]["veto"])


# ---- the hard gate is part of the loop -------------------------------------

SONAR_CFG = {**PANEL_CFG,
             "reviewers": {**PANEL_CFG["reviewers"], "sonarqube": {"enabled": True}}}


def _sonar_round(monkeypatch, tmp_path, round_no, baseline=(), max_rounds=3):
    """A round whose ONLY outstanding item is a SonarCloud hard-gate issue."""
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: SONAR_CFG)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "feat: x", "additions": 3, "deletions": 1, "headRefOid": "abc"},
        diff="diff --git a/a.py b/a.py\n+x\n"))
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 10, []))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "review_sonarqube", lambda *a, **k: (
        "ERROR", [panel.Finding("sonarqube", "P2", "a.py", 9, "cognitive complexity 21")],
        [], None))
    out = tmp_path / f"s{round_no}.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline), max_rounds=max_rounds) == 0
    return str(out), json.loads(out.read_text())


def test_a_new_hard_gate_issue_is_not_a_dry_round(monkeypatch, tmp_path):
    """The gate's issues MUST end up resolved, and they were left out of the round
    diff entirely — so a round whose only outstanding item was one of them recorded
    itself as dry and the caller stopped without running another fixer."""
    _, r1 = _sonar_round(monkeypatch, tmp_path, 1)
    assert r1["new_findings"] == 1
    assert r1["round_stop"]["stop"] is False


def test_a_hard_gate_issue_still_open_after_the_fix_earns_another_round(monkeypatch,
                                                                        tmp_path):
    path, _ = _sonar_round(monkeypatch, tmp_path, 1)
    _, r2 = _sonar_round(monkeypatch, tmp_path, 2, baseline=[path])
    assert r2["new_findings"] == 0            # not new — but not resolved either
    assert r2["round_stop"]["stop"] is False
    # "outstanding", not "confirmed": a hard-gate issue is never adjudicated, and
    # the reason string ends up on the PR comment.
    assert "still outstanding" in r2["round_stop"]["reason"]


# ---- the baseline the next round needs -------------------------------------

def test_a_json_file_that_could_not_be_written_fails_the_run(monkeypatch, capsys,
                                                             tmp_path):
    """That file IS the next round's baseline. Warning and exiting 0 let the caller
    advance the cycle onto a baseline that does not exist, where every repeated
    finding reads as new."""
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: PANEL_CFG)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "feat: x", "additions": 3, "deletions": 1, "headRefOid": "abc"},
        diff="diff --git a/a.py b/a.py\n+x\n"))
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 10, []))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    unwritable = tmp_path / "no-such-dir" / "r1.json"
    assert panel.run("board", 34, post=False, json_file=str(unwritable),
                     record=False, round_no=1, max_rounds=2) == panel.UNWRITTEN_PAYLOAD_EXIT
    err = capsys.readouterr().err
    # ...and the review it already paid for is still reported, not thrown away.
    assert "could not write" in err and "re-run the CYCLE" in err


SKIP_CFG = {**PANEL_CFG, "review_panel": {"skip_title_patterns": [r"^chore\("]}}


def test_a_skipped_pr_still_writes_the_baseline_the_caller_was_promised(monkeypatch,
                                                                       capsys, tmp_path):
    """`--json-file` was honoured on every exit but this one, which returned 0 with
    no file. The caller is told "if the panel could not write that file the round
    did not happen", and then feeds the file to the next round as `--baseline`: a
    skipped PR left it no signal and no baseline at all."""
    _stub_panel(monkeypatch, title="chore(deps): bump x", cfg=SKIP_CFG)
    out = tmp_path / "skip.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False) == 0
    payload = json.loads(out.read_text())
    assert payload["reviewed"] is False and "skip pattern" in payload["skip_reason"]
    assert payload["round_stop"] is None


def test_a_skipped_round_says_which_round_it_was_and_whose_cycle(monkeypatch, capsys,
                                                                 tmp_path):
    """The skip payload was built from the defaults alone, so a skipped round 2
    serialised itself as round 1 with a fresh id. Fed forward as the next round's
    `--baseline` — which is what the caller is told to do with every round's file —
    it collided with the real round 1 over the round number and renamed the cycle
    out from under every later round."""
    r1 = _payload(tmp_path, "r1.json", 1, ["unused import"], cycle="cyc-1")
    _stub_panel(monkeypatch, title="chore(deps): bump x", cfg=SKIP_CFG)
    out = tmp_path / "skip.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=2, baseline=[r1]) == 0
    payload = json.loads(out.read_text())
    assert payload["round"] == 2 and payload["cycle"] == "cyc-1"
    assert payload["prior_rounds"] == 1 and payload["prior_findings"] == 1
    # ...and a round 3 reading both files sees two rounds of one cycle, not an
    # ambiguity and not a conflict.
    b = panel.load_baseline([r1, str(out)],
                            {"github": "acme/board", "pr": 34, "round": 3})
    assert b.problems == [] and b.rounds == {1, 2} and b.cycle == "cyc-1"


def test_a_round_that_lost_its_baseline_records_no_cycle_rather_than_a_new_one(
        monkeypatch, capsys, tmp_path):
    """`followed_by` requires the cycles to match, so minting a fresh id for a
    round 2 whose `--baseline` was mistyped makes round 1 and round 2 of one PR two
    unrelated cycles forever — every re-review declaration round 1 made answers
    `null` permanently, and nothing on the board says why. Null records
    "unattributable", which is the truth and is recoverable."""
    _, out = _report(monkeypatch, capsys, tmp_path, 2,
                     baseline=[str(tmp_path / "nope.json")])
    payload = json.loads(Path(out).read_text())
    assert payload["cycle"] is None
    assert any("unreadable" in n for n in payload["config_notes"])


def test_a_review_only_run_records_no_cycle_either(monkeypatch, capsys, tmp_path):
    """`ReviewIn.cycle` documents "absent ... for a standalone review that is
    nobody's round 2", and the producer minted one on every path anyway."""
    _, out = _report(monkeypatch, capsys, tmp_path, 1)
    assert json.loads(Path(out).read_text())["cycle"] is None


def test_a_json_file_that_is_a_symlink_is_not_followed(tmp_path):
    """`Path.write_text` follows symlinks, so a pre-planted
    `/tmp/panel-34-r1.json` -> `~/.ssh/authorized_keys` is a write under the
    caller's own identity. The shipped defence was a paragraph telling an LLM
    orchestrator to `mktemp -d`; this is the one every caller gets."""
    target = tmp_path / "authorized_keys"
    target.write_text("original")
    link = tmp_path / "r1.json"
    link.symlink_to(target)
    assert panel.write_payload(str(link), {"round": 1})
    assert target.read_text() == "original"


def test_an_integral_float_is_the_same_declaration_on_both_paths():
    """`app/api/reviews.py::_count_or_none` accepts `1.0` as a count; `_flag` fell
    through to False for it. One model output, two answers about whether a
    declaration was made."""
    assert panel._flag(1.0) is True
    assert panel._flag(0.0) is False
    assert panel._flag(1.5) is False


def test_a_skipped_pr_whose_baseline_could_not_be_written_fails_too(monkeypatch,
                                                                    capsys, tmp_path):
    """Same contract as a reviewed run: exit non-zero, so the orchestrator does not
    advance the cycle onto a baseline that does not exist."""
    _stub_panel(monkeypatch, title="chore(deps): bump x", cfg=SKIP_CFG)
    unwritable = tmp_path / "no-such-dir" / "skip.json"
    assert panel.run("board", 34, post=False, json_file=str(unwritable),
                     record=False) == panel.UNWRITTEN_PAYLOAD_EXIT
    assert "could not write" in capsys.readouterr().err


def test_the_repo_a_payload_names_is_the_one_it_resolved(monkeypatch, capsys, tmp_path):
    """`--repo` has no argparse default, so the documented `panel.py --pr N` wrote
    `"repo": null` — a payload nobody can attribute, which round 2 then discarded
    as "does not say which review it is from". The whole round diff no-opped for
    the invocation the workflow prescribes."""
    _, out = _report(monkeypatch, capsys, tmp_path, 1,
                     cfg={**PANEL_CFG, "name": "board"}, max_rounds=2)
    assert json.loads(Path(out).read_text())["repo"] == "board"


# ---- the CLI's own arguments -----------------------------------------------

def test_a_round_past_the_cap_is_rejected_rather_than_recorded(monkeypatch):
    """`--round 5 --max-rounds 2` records an impossible position and hits the cap
    branch on the spot, writing "round cap (2) reached" into a round 5.

    The guard lives in `run()` since #165 rather than in `main`, because the cap it
    has to be checked against can now come from `review_panel.max_rounds` and only
    `run()` has read the rules file. Still before anything is fetched, and it names
    which of the three answers supplied the cap — which is what these two assert."""
    monkeypatch.setattr(sys, "argv",
                        ["panel.py", "--pr", "1", "--round", "5", "--max-rounds", "2"])
    with pytest.raises(SystemExit, match="past the cap of 2, from --max-rounds"):
        panel.main()


def _resolved_cap() -> int:
    """The cap `run()` will actually apply with no `--max-rounds`.

    Derived, not written down. These two tests used to hard-code 2 on the
    coincidence that this repo's `review_panel.max_rounds` matched the shipped
    default — the old docstring said so in as many words. When the repo moved its
    own dial to 1 (P1/P2-only policy, 2026-08-20) the coincidence broke and a test
    named for the DEFAULT failed on a change to this repo's POLICY, which is not
    what it is for. The guard is what is under test; the number is configuration.
    """
    import harness_rules
    import panel_seats
    cfg = harness_rules.resolve_repo(str(Path(__file__).resolve().parents[3]))
    return panel_seats.resolve_dials(cfg.get("review_panel") or {}, None, []).max_rounds


def test_a_round_past_the_DEFAULT_cap_is_rejected_too(monkeypatch):
    """The guard used to fire only when --max-rounds was spelled out, and the cap
    `run()` applies is the resolved one when it is not. So `--round <cap+1>` alone
    passed validation and took the cap branch on the spot, writing "round cap (N)
    reached — …, unreviewed" into a round past the cap and printing "round N+1 of at
    most N" — the exact corrupted metadata the guard exists to prevent."""
    cap = _resolved_cap()
    monkeypatch.setattr(sys, "argv",
                        ["panel.py", "--pr", "1", "--round", str(cap + 1)])
    with pytest.raises(SystemExit, match=f"past the cap of {cap}"):
        panel.main()


def test_the_round_the_default_cap_allows_is_still_accepted(monkeypatch):
    """The last round the cap allows, with no --max-rounds, is the ordinary
    re-review and the tighter guard must not refuse it. It gets past validation and
    dies on the repo instead, which is as far as this test can go without a
    checkout.

    The round is the resolved cap rather than a literal 2, for the reason
    `_resolved_cap` gives. At a cap of 1 this asserts that round 1 — every ordinary
    single-round review — is still accepted, which is the property that actually
    matters once a repo turns the second round off."""
    cap = _resolved_cap()
    monkeypatch.setattr(sys, "argv", ["panel.py", "--pr", "1", "--round", str(cap)])
    monkeypatch.setattr(panel, "run", lambda *a, **k: 0)
    assert panel.main() == 0


def test_a_malformed_rereview_flag_costs_its_flags_not_the_run():
    """`"fix_needs_rereview": 1` used to raise TypeError out of `_findings_of`.

    Not a parse failure the caller could degrade from: `_read` calls
    `_findings_of` while reading CANDIDATES, so the crash escaped the
    retry-then-keep-it-unstructured path and could take the whole run down. A
    flag list the parser cannot read costs its flags; the review survives."""
    body = ('{"findings": [{"severity": "P1", "file": "a.py", "title": "t", '
            '"detail": "d"}], "fix_needs_rereview": %s}')
    for junk in ("1", "true", '"0"', '{"0": true}', "null"):
        findings, _ = panel.parse_reply("codex", body % junk)
        assert len(findings) == 1 and findings[0].needs_rereview is False
    findings, _ = panel.parse_reply("codex", body % "[0]")
    assert findings[0].needs_rereview is True


def test_two_coverage_only_replies_that_disagree_are_not_one_answer():
    """`adjudicate` accepts a coverage-only reply as a judge that ruled on
    nothing rather than one that failed to rule. `_read` used to skip the
    declaration whenever no envelope list sat beside it, so two such candidates
    carrying CONFLICTING notes both read as `_Read((), None)` — one answer, and
    `_agreed` returned the last of them. Position deciding which text survives is
    what this release removes; it cannot come back through the one reply shape
    that carries no items."""
    a, b = {"coverage_note": "could not read the migration"}, {"coverage_note": "saw everything"}
    read_a, read_b = panel._read(a, "verdicts"), panel._read(b, "verdicts")
    assert read_a != read_b
    assert panel._agreed([(a, read_a), (b, read_b)]) is panel._AMBIGUOUS
    # Two spellings of ONE answer are still one answer.
    assert panel._agreed([(a, read_a), (a, panel._read(a, "verdicts"))]) == a


def test_a_prose_bracket_does_not_rival_a_real_findings_array():
    """Tier 2 admitted any non-empty top-level array, so `[42]` written in prose
    parsed, escaped containment, was no schema echo, and became a RIVAL to the
    real answer — `_AMBIGUOUS`, a retry, the reply kept unstructured, and
    `coverage_veto` filing "returned no structured reply". All of it on exactly
    the older reviewers the bare-array tier exists to serve. An array the reader
    keeps nothing of is positively identifiable as not-an-answer, the same
    standard `_quoted` applies to the echo — no ranking needed."""
    real = '[{"severity": "P2", "file": "a.py", "title": "t", "detail": "d"}]'
    for noise in ("the severity on line [42]", "see [1]", "restating ids [0, 3]"):
        assert panel.extract_json_value(f"{noise}\n{real}", "findings") == json.loads(real)
    # Two arrays that BOTH say something still disagree, and still say so.
    other = '[{"severity": "P1", "file": "b.py", "title": "u", "detail": "e"}]'
    assert panel.extract_json_value(f"{real}\n{other}", "findings") is None


def test_echo_detection_wildcards_the_prompt_s_tokens_and_nothing_else():
    """`_quoted` used to accept ANY scalar wherever the schema held one, which was
    right only because every scalar in both schemas comes from `<int|null>` or
    `true|false`. Read as written it said a real `real: false` quotes the
    example's `true` and a real `line: 42` quotes its `null` — so the first
    literal scalar added to either prompt would have turned echo detection into a
    rule that discards rulings. The wildcards are now the tokens themselves."""
    schema = panel.SCHEMA_ECHOES["verdicts"]
    verbatim = json.loads(json.dumps(schema, default=lambda _: None))
    assert panel._quoted(verbatim, schema)
    # A token position takes any scalar; `members` takes any NON-EMPTY scalar list.
    assert panel.SCHEMA_ITEMS["verdicts"]["real"] is panel._TOKEN
    assert panel.SCHEMA_ITEMS["verdicts"]["line"] is panel._TOKEN
    named_nobody = json.loads(json.dumps(schema, default=lambda _: None))
    named_nobody["verdicts"][0]["members"] = []
    assert not panel._quoted(named_nobody, schema), "a verdict naming nobody is not a quotation"
    # A literal scalar means itself rather than "any scalar".
    assert panel._quoted(1, 1) and not panel._quoted(2, 1)


def test_the_judge_gets_the_same_one_shot_reparse_the_reviewers_get(monkeypatch):
    """`review_llm` answers an unresolvable reply with a second CLI call before
    degrading; `adjudicate` went straight to `unruled`. The asymmetry was the
    expensive half: a reviewer that cannot be read costs one seat, a judge that
    cannot be read takes EVERY finding through `unjudged` and vetoes the round.

    Agreement strictly enlarges the set of replies that resolve to None — an
    envelope plus a restatement, an envelope plus a self-authored illustration —
    so a failure that was rare under ranking now fires on ordinary model prose.
    One more turn keeps the pessimistic rule without paying the whole round for
    it."""
    ambiguous = ('{"verdicts": [{"id": "F01", "members": [0], "real": true, '
                 '"synthesis": "the handle is never closed"}]}\n'
                 '{"verdicts": [{"id": "F01", "members": [0], "real": false, '
                 '"synthesis": "the handle is closed by the context manager"}]}')
    settled = ('{"verdicts": [{"id": "F01", "members": [0], "real": true, '
               '"synthesis": "the handle is never closed"}]}')
    calls = []

    def fake_run_cli(args, label, timeout=panel.CLI_TIMEOUT, attempts=3, stdin_text=None,
                     on_output=None, replied=None, cwd=None):
        calls.append(attempts)
        return (ambiguous if len(calls) == 1 else settled), None

    monkeypatch.setattr(panel.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(panel_seats, "run_cli", fake_run_cli)
    leak = panel.Finding("codex", "P2", "a.py", 1, "leak", "")
    out, skip, _ = panel.adjudicate([[leak]], "diff", "", 34)
    assert len(calls) == 2 and calls[1] == 1, "one extra attempt, not another three"
    assert skip is None and [c.verdict for c in out] == ["confirmed"]


# ---- the whole round, end to end ------------------------------------------

def _round_declaring(monkeypatch, capsys, tmp_path, gaps, blind, round_no,
                     baseline=()):
    """One whole panel run whose seats declare `gaps` and were (or were not) able
    to read the code. `review_llm` is replaced AFTER `_stub_panel`, which installs
    its own — patching before it is silently overwritten, and the test then passes
    on the default seat rather than the one it described."""
    _stub_panel(monkeypatch, findings=[])
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 10, list(gaps),
                                                          code_blind=blind))
    out = tmp_path / f"decl{round_no}-{blind}.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline), max_rounds=3) == 0
    return capsys.readouterr().out, json.loads(Path(out).read_text())


def test_a_blind_seats_declarations_reach_the_payload_without_costing_the_stop(
        monkeypatch, capsys, tmp_path):
    """The change, exercised through a real `run` rather than through
    `coverage_veto` alone — because the exemption is only worth anything if the
    flag actually arrives, and there are four hops between the sandbox and the
    veto (`run_seat` -> `SeatTurn` -> `review_llm` -> `ReviewerRun` ->
    `reviewer_meta`). A unit test of the last hop passes just as happily when the
    first one drops the value.

    Three assertions, and they are the whole contract: the round stops CONFIDENTLY
    with gaps declared, the gaps are still in the report where a reader can act on
    them, and the payload records `code_blind` so a later round — or the round
    where #113's setting is turned on — can be told apart from this one."""
    declared = ["whether load_repo_cfg validates the path",
                "worktree-holder's exit codes"]
    # A round 1 to be the baseline: without one, round 2 vetoes on having nothing
    # to compare against, which would mask exactly what this measures.
    _, first = _round_declaring(monkeypatch, capsys, tmp_path, [], True, 1)
    r1 = str(tmp_path / "decl1-True.json")
    report, payload = _round_declaring(monkeypatch, capsys, tmp_path, declared,
                                       True, 2, baseline=[r1])
    stop = payload["round_stop"]

    # 1. it stopped, and it was allowed to mean it.
    assert stop["stop"] is True
    assert stop["confident"] is True, stop["veto"]
    assert not [v for v in stop["veto"] if "could not assess" in v]

    # 2. the declarations are still on the PR — reported, not counted.
    for gap in declared:
        assert gap in report
    assert "did not cost the round its confidence" in report

    # 3. and the state that decided it is in the payload, per seat.
    ran = [m for m in payload["reviewers"].values() if m.get("ran")]
    assert ran and all(m["code_blind"] is True for m in ran)


def test_a_seat_that_can_read_the_code_puts_its_gaps_back_in_the_veto(
        monkeypatch, capsys, tmp_path):
    """The forward-compatibility half, and the reason this is state rather than a
    deletion. #113's second half makes code access a per-repo setting defaulting
    ON; a seat that gets the PR's tree and still cannot answer something is making
    a claim about THIS round, and that claim has to cost the round its confidence
    again. Nothing else in this suite would notice if turning the setting on left
    the exemption in place — the veto list would simply stay short, which reads
    exactly like success."""
    _, _first = _round_declaring(monkeypatch, capsys, tmp_path, [], False, 1)
    r1 = str(tmp_path / "decl1-False.json")
    report, payload = _round_declaring(monkeypatch, capsys, tmp_path,
                                       ["migrations/versions/"], False, 2,
                                       baseline=[r1])
    stop = payload["round_stop"]
    assert stop["confident"] is False
    assert any("could not assess: migrations/versions/" in v for v in stop["veto"])
    # The reader-facing note belongs to the blind case and must not appear here.
    assert "did not cost the round its confidence" not in report


def test_sonarqube_cannot_switch_off_the_argv_floor():
    """The floor counts LLM seats only, and the second model that read this diff was
    right that counting everything was too permissive.

    `sonarqube` shares `reviewer_meta` and carries no `truncated` key, so an `all()`
    over every entry was False the moment sonar ran — switching the floor off. A
    round could then stop confidently with `--reviewers antigravity` and sonar
    enabled and no LLM having read the diff whole. Sonar is the hard gate alongside
    the panel, not a reviewer reading the change, so it cannot stand in for one.

    Note the floor above this one asks a different question — "did ANYTHING run?" —
    and counts sonar deliberately. That is why they are separate tests as well as
    separate branches."""
    capped = {"antigravity": {"ran": True, "truncated": True, "argv_capped": True,
                              "max_diff_chars": 116_771, "code_blind": True},
              "sonarqube": {"ran": True, "skip": None}}
    veto = panel.coverage_veto(capped, None, 0, 175_547, ci_status="PASS")
    assert any("nothing read this diff whole" in v for v in veto), (
        "a running sonarqube suppressed the floor")
    assert panel.round_stop(1, 2, [], [], veto)["confident"] is False

    # An LLM seat that saw the whole diff still lifts it — that seat's reading is
    # what the round rests on.
    with_reader = dict(capped, claude={"ran": True, "code_blind": True})
    assert panel.coverage_veto(with_reader, None, 0, 175_547, ci_status="PASS") == []


# ------------------------------------------- #546: nothing executed vs a green suite


def _one_seat_ran() -> dict:
    """A round with nothing wrong with its READING: one seat, installed, ran, whole
    diff, structured reply, no declared gaps. Every veto above `ci_status` is
    silent on it, so anything the assertions below see came from the CI state."""
    return {"claude": {"ran": True, "code_blind": False, "could_not_assess": []}}


def test_a_round_with_no_ci_run_cannot_stop_confidently():
    """#546. `ci_status` did not reach this function at all, so a round whose full
    suite passed on the exact commit and one where NO RUN EXISTS produced the same
    empty veto list and the same `confident: True`.

    That was latent only because four seats each declared "I cannot run the tests"
    and each declaration vetoed. #547 removes those declarations, and the whole
    point of landing this first is that it must not be what was holding the line."""
    green = panel.coverage_veto(_one_seat_ran(), None, 0, 1_000, ci_status="PASS")
    assert green == []
    assert panel.round_stop(1, 2, [], [], green)["confident"] is True

    nothing = panel.coverage_veto(_one_seat_ran(), None, 0, 1_000, ci_status="none")
    assert nothing == ["no CI run exists for this commit — nothing mechanical "
                       "executed this code"]
    assert panel.round_stop(1, 2, [], [], nothing)["confident"] is False


def test_a_red_suite_is_evidence_and_does_not_cost_the_round_its_confidence():
    """The line this issue turns on, and the one it would be easiest to get wrong:
    a FAILED run is *evidence*, and `ci_brief` already tells every seat to "treat
    that as a fact you may reason from, not as a finding to re-report". A round
    that read a real failure is not a round that read nothing — and `preland.check_ci`
    refuses the merge on FAIL regardless, so nothing is bought by vetoing here."""
    assert panel.coverage_veto(_one_seat_ran(), None, 0, 1_000, ci_status="FAIL") == []


def test_each_unsettled_state_keeps_its_own_sentence():
    """Four states, four sentences, and the separation is the point (#546).

    Only two of them are claims about EXECUTION. `none` is nothing ran. `blocked`
    (#324) is a run that EXISTS and will not execute without a person — it must not
    borrow `none`'s wording, which is the conflation that made PR #282 look
    untouched for two days. The other two are weaker on purpose: `PENDING` is
    #501's residue after the bounded wait and may be a suite whose other checks
    went green, and `unknown` is a lookup that failed and says nothing either way.
    They veto for the same reason — no settled result to earn confidence on — and
    they must not be WORDED the same, because could-not-check is not
    nothing-to-report. #548 is the sibling issue about filling the channel;
    nothing here folds its case into this one."""
    said = {state: panel.coverage_veto(_one_seat_ran(), None, 0, 1_000,
                                       ci_status=state)
            for state in ("PENDING", "none", "blocked", "unknown")}
    assert all(len(v) == 1 for v in said.values())
    assert len({v[0] for v in said.values()}) == 4, "two states share a sentence"
    assert "gated on a human" in said["blocked"][0]
    assert "no CI run exists" in said["none"][0]
    assert "had not settled" in said["PENDING"][0]
    assert "could not be read" in said["unknown"][0]
    # Neither of the two that is NOT about execution may claim anything ran or
    # did not: that is the stronger fact, it is false of both, and asserting it
    # here is what stops the wording drifting back into it. `unknown` is allowed
    # to name execution — it is the thing it says it cannot determine — but only
    # as the open question it leaves, never as an answer.
    assert "execut" not in said["PENDING"][0]
    assert said["unknown"][0].endswith("is unknown")
    for weaker in ("PENDING", "unknown"):
        assert "nothing" not in said[weaker][0]


def test_a_repo_that_declared_it_has_no_ci_is_not_asked_again_every_round():
    """`coverage_veto`'s forbidden constant, caught by codex on this change.

    `preland.check_ci` refuses `none` by naming the remedy in its own refusal:
    say so with `"preland": {"disabled_checks": ["ci"]}`. A repo that HAS said so
    would otherwise carry this veto on every round it will ever run — an
    observation true of all of them, which distinguishes nothing, makes
    `--require-earned-stop` unsatisfiable and trains its reader to ignore the
    signal. That is the exact failure the absent-CLI exemption above was added for.

    Exactly `none` is exempted. The declaration explains an ABSENT run; a gated
    run, an unsettled suite and a failed lookup all contradict it rather than
    being covered by it, and a repo with no CI cannot produce any of them."""
    said = panel.coverage_veto(_one_seat_ran(), None, 0, 1_000,
                               ci_status="none", ci_declared_absent=True)
    assert said == []
    assert panel.round_stop(1, 2, [], [], said)["confident"] is True
    for still in ("PENDING", "blocked", "unknown"):
        assert panel.coverage_veto(_one_seat_ran(), None, 0, 1_000,
                                   ci_status=still,
                                   ci_declared_absent=True) != [], still


def test_an_unexplained_absence_of_ci_still_vetoes():
    """The other half of the exemption, and the whole of its value. "This repo has
    no CI" is a written statement somebody made; "no run exists for this commit" is
    what a repo with a workflow that failed to trigger looks like. They produce the
    identical `ci_status`, and only the declaration tells them apart — so the
    default is the strict one, and a caller that knows nothing about the repo's
    rules gets the veto rather than the exemption."""
    assert panel.coverage_veto(_one_seat_ran(), None, 0, 1_000,
                               ci_status="none") != []
    assert panel.coverage_veto(_one_seat_ran(), None, 0, 1_000, ci_status="none",
                               ci_declared_absent=False) != []


def test_an_unrecognised_ci_state_vetoes_rather_than_passing():
    """Fail closed, by construction. The set is written as the states that DO NOT
    veto, so a seventh `CI_STATE_WORDS` entry added next year costs the round its
    confidence until somebody argues it into `CI_EXECUTED` — rather than passing
    silently, which is exactly how `none` reached today.

    #548's two are the first states argued in, and they are spelled out here rather
    than derived so that the next one is also somebody's decision: a suite that RAN
    on this commit and reported is execution evidence, which is the only thing this
    veto asks about. `local-unknown` is not in it — a command that reported nothing
    established nothing."""
    assert sorted(panel.CI_SETTLED) == ["FAIL", "PASS", panel.LOCAL_FAIL,
                                        panel.LOCAL_PASS]
    covered = set(panel.CI_UNSETTLED) | set(panel.CI_SETTLED)
    # Every state `ci_status` can arrive as: the six the forge can report, plus the
    # three a local run produces. A state in neither mapping falls to the generic
    # fallback line, which vetoes — safe, and mute about which fact it is stating.
    assert covered == set(panel_scope.CI_STATE_WORDS.values()) | set(panel.LOCAL_STATES), \
        "a CI state exists that this function neither exempts nor has a sentence for"
    for odd in ("", "queued", "PASSED"):
        assert panel.coverage_veto(_one_seat_ran(), None, 0, 1_000,
                                   ci_status=odd) != []


def test_the_ci_veto_cannot_be_forgotten_by_a_caller():
    """`ci_status` is keyword-only with no default. The alternative — defaulting to
    a value — picks between two bad answers: `PASS` silently buys a confident stop
    for any path that forgets, and anything else silently vetoes rounds nobody
    meant to veto. A TypeError is neither, and `coverage_veto`'s standing
    discipline is that a path which forgets to set something must not fail open."""
    with pytest.raises(TypeError):
        panel.coverage_veto(_one_seat_ran(), None, 0, 1_000)


def _cycle_payload(monkeypatch, capsys, tmp_path, ci, cfg=PANEL_CFG):
    """One whole `run()` as a CYCLE round, returning the payload it wrote.

    A cycle round because `round_stop` is `None` on a review-only payload — a
    verdict about a loop nobody is running — so `max_rounds` is what makes the
    field this is about exist at all."""
    _stub_panel(monkeypatch, findings=[], cfg=cfg)
    monkeypatch.setattr(panel, "review_ci", lambda *a: (ci, [], None))
    out = tmp_path / f"{ci}.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=1, max_rounds=2) == 0
    capsys.readouterr()
    return json.loads(out.read_text())


def test_the_ci_veto_reaches_the_payload_a_consumer_actually_reads(
        monkeypatch, capsys, tmp_path):
    """The plumbing, not the rule — asserted end to end because the two can be
    right separately and still not meet.

    `--require-earned-stop`, `/fix-and-land`'s merge gate and the board's own
    column all read `round_stop.confident` out of the payload, not
    `coverage_veto`'s return value. A call site passing a stale `ci_status`, or
    dropping the veto between the function and the file, is invisible to every
    unit test above and is exactly the shape of defect this issue is about."""
    green = _cycle_payload(monkeypatch, capsys, tmp_path, "PASS")
    assert green["ci_status"] == "PASS"
    assert green["round_stop"]["confident"] is True
    assert green["round_stop"]["veto"] == []

    dark = _cycle_payload(monkeypatch, capsys, tmp_path, "none")
    assert dark["ci_status"] == "none"
    assert dark["round_stop"]["confident"] is False
    assert any("no CI run exists" in v for v in dark["round_stop"]["veto"])


def test_a_repos_written_no_ci_declaration_reaches_the_round_from_harness_rules(
        monkeypatch, capsys, tmp_path):
    """The exemption's own plumbing. The declaration lives in `.harness-rules`
    under the key `preland` refuses `none` by naming, and it has to travel from
    the resolved config through `run()` to the veto — otherwise a repo can write
    it, watch preland honour it at land time, and still never earn a stop."""
    declared = {**PANEL_CFG, "preland": {"disabled_checks": ["ci"]}}
    said = _cycle_payload(monkeypatch, capsys, tmp_path, "none", cfg=declared)
    assert said["round_stop"]["confident"] is True
    assert said["round_stop"]["veto"] == []
    # The declaration covers the absence it explains and nothing else: a gated
    # run contradicts "this repo has no CI" rather than being covered by it.
    gated = _cycle_payload(monkeypatch, capsys, tmp_path, "blocked", cfg=declared)
    assert gated["round_stop"]["confident"] is False


def test_a_malformed_disabled_checks_does_not_hand_out_the_exemption(
        monkeypatch, capsys, tmp_path):
    """`preland.disabled_checks` hard-exits on a list it cannot read, which is
    right for a merge gate and wrong for a read-only review: a typo in a section
    the panel does not otherwise touch must not stop a round running. So the
    panel reads the key straight, and every way of being unreadable fails in the
    strict direction — no "ci" in it, so the veto stands."""
    for junk in ("ci", {"ci": True}, None, ["c i"], 7):
        cfg = {**PANEL_CFG, "preland": {"disabled_checks": junk}}
        got = _cycle_payload(monkeypatch, capsys, tmp_path, "none", cfg=cfg)
        assert got["round_stop"]["confident"] is False, junk


# ------------------------------------------------------------------ #547's two cases
#
# A `could_not_assess` from a seat that did not open a file it could have opened, and
# one from a seat that would need a running Postgres and a browser, produced the
# identical artefact: a veto line, `confident: False`, a HOLD. The first impugns the
# round. The second says what kind of instrument a panel of models reading a diff IS,
# is true of every PR about runtime behaviour, and as a veto is exactly the standing
# constant `coverage_veto`'s own docstring rules out.


def _two_seats(claude=("the jsonb ceiling holds", "the html import"),
               codex=("the jsonb ceiling under load",)) -> dict:
    """Two running, sighted seats with declarations to rule on."""
    return {"claude": {"ran": True, "code_blind": False,
                       "could_not_assess": list(claude)},
            "codex": {"ran": True, "code_blind": False,
                      "could_not_assess": list(codex)}}


def _numbered(meta: dict) -> list[tuple[str, str]]:
    """The declaration list `adjudicate` hands the judge, in its order."""
    return [(n, g) for n, m in sorted(meta.items())
            for g in m.get("could_not_assess") or []]


def _ruled(meta: dict, *entries):
    """A judge reply's `coverage_rulings`, resolved against `meta`'s declarations —
    the same path `adjudicate` takes, so these tests exercise the parser and not a
    hand-built mapping."""
    return panel._coverage_ruling(panel_core._rulings(list(entries)), _numbered(meta))


#: One entry ruling declarations 0 and 2 — the same claim in two seats' words —
#: structurally unanswerable, and declaration 1 answerable and unanswered.
CEILING = {"declarations": [0, 2], "claim": "the jsonb ceiling holds at 8.16 MB",
           "resolvable_in_harness": False, "reason": "needs a running Postgres"}
IMPORT = {"declarations": [1], "claim": "the html import",
          "resolvable_in_harness": True, "reason": "read the test file's imports"}


def test_the_two_could_not_assess_cases_stop_producing_one_artefact():
    """The issue, in one assertion. Before, three declarations meant three veto
    lines and no way to tell which of them any round could ever answer. After, the
    one that was trivially checkable and was not still costs the round its
    confidence by name, and the two that no seat here could have settled are ONE
    named obligation with a key somebody can act on."""
    meta = _two_seats()
    before = panel.coverage_veto(meta, None, 0, 1_000, ci_status="PASS")
    assert before == ["claude could not assess: the jsonb ceiling holds",
                      "claude could not assess: the html import",
                      "codex could not assess: the jsonb ceiling under load"]

    after = panel.coverage_veto(meta, None, 0, 1_000, ci_status="PASS",
                                coverage=_ruled(meta, CEILING, IMPORT))
    assert after == [
        "claude could not assess: the html import",
        "an unverifiable claim is unacknowledged [uc-61a3c6451332]: the jsonb ceiling "
        "holds at 8.16 MB — needs a running Postgres"]


def test_a_ruling_on_its_own_never_removes_a_veto_line():
    """The property the whole design rests on, and the one to put to any change
    here: a MODEL cannot author a confident stop.

    The judge's ruling is a judgement about a judgement — not recorded state like
    `absent` or `code_blind` — so it buys no exemption by itself. All it can do is
    change what the veto SAYS. What ends the veto is a human passing the key back,
    and that is an argument on a command line."""
    meta = _two_seats()
    ruling = _ruled(meta, CEILING, IMPORT)
    assert ruling.unresolvable, "the judge did rule them unresolvable"
    veto = panel.coverage_veto(meta, None, 0, 1_000, ci_status="PASS", coverage=ruling)
    assert panel.round_stop(1, 2, [], [], veto)["confident"] is False


def test_acknowledging_the_claim_by_key_is_what_discharges_it():
    """And the other half: the act IS available, so the gate is satisfiable. That is
    the whole of #547 — a veto nobody can ever discharge is a veto that gets dropped,
    and then there is no gate at all."""
    meta = _two_seats(claude=("the jsonb ceiling holds",), codex=())
    ruling = _ruled(meta, {**CEILING, "declarations": [0]})
    key = "uc-61a3c6451332"
    veto = panel.coverage_veto(meta, None, 0, 1_000, ci_status="PASS",
                               coverage=ruling, acknowledged=[key])
    assert veto == []
    assert panel.round_stop(1, 2, [], [], veto)["confident"] is True


def test_an_unruled_declaration_vetoes_exactly_as_it_did_before():
    """The asymmetric default, stated as a test rather than as a paragraph. Silence
    never buys the exemption: a reply that ruled on one declaration and forgot the
    other leaves the other exactly where it was."""
    meta = _two_seats(claude=("the jsonb ceiling holds", "the html import"), codex=())
    ruling = _ruled(meta, {**CEILING, "declarations": [0]})
    assert panel.coverage_veto(meta, None, 0, 1_000, ci_status="PASS",
                               coverage=ruling)[0] == \
        "claude could not assess: the html import"


@pytest.mark.parametrize("value", ["false", "False", 0, None, "no", [], "unresolvable"])
def test_only_a_typed_false_buys_the_exemption(value):
    """A typed enum is not prose, and this is where that stops being a claim. The
    flag is read with `is False`, exactly as `_ruling` reads `real` and for the
    mirror-image reason: there an unreadable flag must not dismiss a finding, here it
    must not excuse a gap. A string spelling of the word is a spelling, not an act."""
    meta = _two_seats(claude=("the jsonb ceiling holds",), codex=())
    ruling = _ruled(meta, {**CEILING, "declarations": [0],
                           "resolvable_in_harness": value})
    assert not ruling.unresolvable
    assert panel.coverage_veto(meta, None, 0, 1_000, ci_status="PASS",
                               coverage=ruling) == \
        ["claude could not assess: the jsonb ceiling holds"]


def test_a_missing_flag_buys_nothing_either():
    """The commonest malformation, and the one a model reaches by writing prose
    where a key was asked for."""
    meta = _two_seats(claude=("the jsonb ceiling holds",), codex=())
    ruling = _ruled(meta, {"declarations": [0], "claim": "the ceiling"})
    assert not ruling.unresolvable


def test_an_obligation_with_no_name_is_no_obligation():
    """#547 asks for a NAMED obligation. An entry that rules a declaration
    unresolvable and says nothing about what the claim IS would delete a veto line
    and put nothing in its place, which is the model-authored bypass Part 2 exists to
    prevent — so the declaration stays where it was."""
    meta = _two_seats(claude=("the jsonb ceiling holds",), codex=())
    ruling = _ruled(meta, {"declarations": [0], "claim": "",
                           "resolvable_in_harness": False})
    assert not ruling.unresolvable
    assert panel.coverage_veto(meta, None, 0, 1_000, ci_status="PASS",
                               coverage=ruling) == \
        ["claude could not assess: the jsonb ceiling holds"]


def test_a_declaration_two_entries_both_claim_is_left_unruled():
    """`JUDGE_PROMPT` asks for exactly one entry per declaration number. Two claims
    on one number is a reply that did not answer the question, and resolving it by
    taking the first would let the ORDER of a model's array decide whether a gap
    vetoes — which is the failure `_agreed` was written to end."""
    meta = _two_seats(claude=("the jsonb ceiling holds",), codex=())
    ruling = _ruled(meta,
                    {**CEILING, "declarations": [0]},
                    {"declarations": [0], "claim": "the same gap, differently",
                     "resolvable_in_harness": False})
    assert not ruling.unresolvable


def test_an_entry_that_names_no_declaration_exempts_nothing():
    """It merges nothing, so it can exempt nothing — and keeping it would put a
    claim on the round's ledger that no reviewer ever raised."""
    meta = _two_seats(claude=("the jsonb ceiling holds",), codex=())
    assert not _ruled(meta, {**CEILING, "declarations": []}).unresolvable
    assert not _ruled(meta, {**CEILING, "declarations": [9]}).unresolvable


def test_the_ruling_is_read_off_numbers_and_never_off_the_wording():
    """The rule every exemption in `coverage_veto` keeps, and the reason its
    docstring gives for keeping it twice: a regex over free-form prose "would exempt
    a genuine round-specific gap whose wording happened to match while missing the
    structural one that did not".

    So an entry whose `claim` quotes a declaration verbatim, while its
    `declarations` list points somewhere else, exempts what it POINTED at and never
    what it quoted."""
    meta = _two_seats(claude=("the jsonb ceiling holds", "the html import"), codex=())
    ruling = _ruled(meta, {"declarations": [1], "claim": "the jsonb ceiling holds",
                           "resolvable_in_harness": False, "reason": "needs Postgres"})
    assert [gap for _n, gap in ruling.unresolvable] == ["the html import"]


def test_a_ruling_that_is_the_schema_quoted_back_rules_on_nothing(monkeypatch):
    """The same guard `_is_answer` puts on a verdict, and it is needed more here: a
    verdict echoed back files a fabricated finding, and a RULING echoed back removes
    a veto line."""
    # The schema as a MODEL resolves it: the two positions the prompt writes as
    # tokens (`<...>`, `true|false`) filled in, and every literal string kept.
    schema = panel_core.SCHEMA_RULING
    example = {**schema, "declarations": [0, 2], "resolvable_in_harness": False}
    assert all(v is not panel_core._TOKEN for v in example.values())
    assert panel_core._rulings([example]) == ()

    _judge_returning(monkeypatch, json.dumps(
        {"verdicts": [], "coverage_note": "x",
         panel_core.COVERAGE_RULINGS: [example]}))
    _f, skip, ruled = panel.adjudicate(
        [], "diff", "opus", 34, coverage={"claude": ["the migration"]})
    assert skip is None and not ruled.unresolvable


# ------------------------------------------------------- it can only get SHORTER


def test_a_blind_seats_declaration_cannot_become_an_obligation():
    """The property that makes this change safe to make at all: it can never add a
    veto line anywhere.

    A `code_blind` seat's declarations are reported and do not vote, because with the
    diff as its whole evidence "I could not read a function this diff does not
    change" is true of every round it sits. Letting one become an obligation would
    hand that seat a standing veto it does not have today — #546's codex pass caught
    the identical shape, a repo with no CI acquiring a veto every round — so
    obligations are built only from declarations that WOULD have vetoed."""
    meta = {"claude": {"ran": True, "code_blind": True,
                       "could_not_assess": ["the jsonb ceiling holds"]}}
    ruling = _ruled(meta, {**CEILING, "declarations": [0]})
    assert ruling.unresolvable, "the judge ruled on it — it is in the listing"
    assert panel.reached_obligations(meta, ruling) == ()
    assert panel.coverage_veto(meta, None, 0, 1_000, ci_status="PASS",
                               coverage=ruling) == []


def test_a_seat_that_never_ran_cannot_raise_an_obligation_either():
    """Same rule, the other recorded state. Its declarations are not in the veto
    list today (the seat's own `did not run` line is), so they cannot arrive as an
    obligation and add a second."""
    meta = {"claude": {"ran": False, "skip": "timed out",
                       "could_not_assess": ["the jsonb ceiling holds"]},
            "codex": {"ran": True, "code_blind": False, "could_not_assess": []}}
    ruling = _ruled(meta, {**CEILING, "declarations": [0]})
    assert panel.reached_obligations(meta, ruling) == ()
    assert panel.coverage_veto(meta, None, 0, 1_000, ci_status="PASS",
                               coverage=ruling) == \
        ["claude did not run (timed out)"]


def test_adding_a_seat_that_restates_a_claim_costs_the_round_nothing():
    """#547's second ordering constraint, and the test of whether the divergence is
    actually fixed: **does adding a reviewer still make a confident stop strictly
    less reachable?**

    It did, by construction. Under `confident = not veto` each extra seat
    contributed its own copy of the same capability limit and every copy was a veto,
    so a fifth seat made a confident stop less reachable while adding findings rather
    than evidence. Now the judge merges the copies into one claim and the ledger
    carries one entry, so the fourth and fifth seats saying it too cost nothing."""
    one = {"claude": {"ran": True, "code_blind": False,
                      "could_not_assess": ["the jsonb ceiling holds"]}}
    four = {**one,
            "codex": {"ran": True, "code_blind": False,
                      "could_not_assess": ["the jsonb ceiling under load"]},
            "grok": {"ran": True, "code_blind": False,
                     "could_not_assess": ["whether the ceiling holds at all"]},
            "pi": {"ran": True, "code_blind": False,
                   "could_not_assess": ["the 8.16 MB figure"]}}
    merged = {**CEILING, "declarations": [0, 1, 2, 3]}
    assert len(panel.coverage_veto(one, None, 0, 1_000, ci_status="PASS",
                                   coverage=_ruled(one, {**CEILING, "declarations": [0]}))) == 1
    assert len(panel.coverage_veto(four, None, 0, 1_000, ci_status="PASS",
                                   coverage=_ruled(four, merged))) == 1
    # And the old rule, for the contrast the issue measured: four seats, four vetoes.
    assert len(panel.coverage_veto(four, None, 0, 1_000, ci_status="PASS")) == 4


# --------------------------------------------------------------- #546 stays intact


def test_a_round_where_nothing_executed_still_vetoes_though_every_claim_is_acknowledged():
    """The sequencing constraint, checked from the other side. #546 made `ci_status`
    reach this function precisely so that removing the seats' prose vetoes would not
    turn a round with no execution behind it into a confident one — the prose vetoes
    were holding that line by accident.

    So: every declaration ruled unresolvable, every obligation acknowledged, and the
    round still cannot stop confidently, because nothing ran."""
    meta = _two_seats(claude=("the jsonb ceiling holds",), codex=())
    ruling = _ruled(meta, {**CEILING, "declarations": [0]})
    veto = panel.coverage_veto(meta, None, 0, 1_000, ci_status="none",
                               coverage=ruling,
                               acknowledged=["uc-61a3c6451332"])
    assert veto == ["no CI run exists for this commit — nothing mechanical "
                    "executed this code"]
    assert panel.round_stop(1, 2, [], [], veto)["confident"] is False


def test_the_judge_cannot_exempt_itself():
    """A judge that did not rule takes its own veto line, and no ruling it did not
    make can remove it. Stated as a test because the exemption this issue adds is the
    first one in this function that a MODEL grants, and the model granting it is the
    same party the line is about."""
    meta = _two_seats(claude=("the jsonb ceiling holds",), codex=())
    veto = panel.coverage_veto(meta, "judge: claude CLI absent", 0, 1_000,
                               ci_status="PASS",
                               coverage=_ruled(meta, {**CEILING, "declarations": [0]}),
                               acknowledged=["uc-61a3c6451332"])
    assert veto == ["the round was not adjudicated (judge: claude CLI absent)"]


# ------------------------------------------------------- #547's ledger, end to end
#
# Part 1 without Part 2 is a model-authored bypass of the confidence gate with no
# ledger. These are Part 2: every unverifiable claim lands somewhere a human reads,
# whether or not anybody has accepted it, and the acceptance is its own recorded act.


def _ruling_judge(unresolvable=("the enactment drops to 8.16 MB",)):
    """An `adjudicate` stub that rules every declaration it is given unresolvable
    under one merged claim — the shape of the round #547 was filed off."""
    def fake(clusters, diff, model, pr, budget=None, coverage=None, ci="", **_kw):
        numbered = [(n, g) for n, items in sorted((coverage or {}).items())
                    for g in items]
        entries = [{"declarations": list(range(len(numbered))),
                    "claim": c, "resolvable_in_harness": False,
                    "reason": "needs the deployed system"} for c in unresolvable]
        return [], None, panel._coverage_ruling(panel_core._rulings(entries), numbered)
    return fake


def _claiming_round(monkeypatch, capsys, tmp_path, *, acknowledge=(), baseline=(),
                    round_no=1, gaps=("the enactment size",)):
    """One whole cycle round whose single seat declares a gap the judge rules
    structurally unanswerable. Returns (report, payload)."""
    _stub_panel(monkeypatch, findings=[])
    monkeypatch.setattr(panel, "review_llm", lambda *a, **k: panel.ReviewerRun(
        [], None, 10, list(gaps)))
    monkeypatch.setattr(panel, "adjudicate", _ruling_judge())
    out = tmp_path / f"r{round_no}.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=round_no, max_rounds=3, baseline=list(baseline),
                     acknowledge=list(acknowledge)) == 0
    return capsys.readouterr().out, json.loads(out.read_text())


#: The key `claim_key` mints for the claim the stub judge rules on. Written out
#: rather than derived from the function under test — a key the tests compute the
#: same way the code does would agree with any derivation, including a broken one,
#: and this is the string a human types back off a PR comment.
ENACTMENT = "uc-f1554b5ef264"


def test_every_unverifiable_claim_reaches_the_payloads_ledger(monkeypatch, capsys,
                                                              tmp_path):
    """"Nobody checked whether stored rows are now a mixed corpus" is the BEST
    output of the round that raised it. The problem was never that it was raised —
    it is that raising it discharged nothing and cost the round its verdict forever.

    So the claim is kept, under a key, with what would settle it beside it, and
    whether it has been accepted is a separate field: a reader has to be able to tell
    "this round raised none" from "all of them are signed off"."""
    _report, got = _claiming_round(monkeypatch, capsys, tmp_path)
    assert got["unresolved_claims"] == [
        {"key": ENACTMENT, "claim": "the enactment drops to 8.16 MB",
         "reason": "needs the deployed system", "acknowledged": False}]
    assert got["round_stop"]["confident"] is False
    assert got["round_stop"]["veto"] == [
        f"an unverifiable claim is unacknowledged [{ENACTMENT}]: the enactment "
        "drops to 8.16 MB — needs the deployed system"]


def test_an_acknowledged_claim_stays_on_the_ledger_and_stops_costing_the_round(
        monkeypatch, capsys, tmp_path):
    """Acknowledging is not deleting. The claim, its key and what would settle it
    all stay in the artefact; what changes is that the round may now stop
    confidently, which is the one-time human act replacing a permanent HOLD."""
    _report, got = _claiming_round(monkeypatch, capsys, tmp_path,
                                   acknowledge=[ENACTMENT])
    assert got["unresolved_claims"] == [
        {"key": ENACTMENT, "claim": "the enactment drops to 8.16 MB",
         "reason": "needs the deployed system", "acknowledged": True}]
    assert got["acknowledged"] == {ENACTMENT: 1}
    assert got["round_stop"]["veto"] == []
    assert got["round_stop"]["confident"] is True


def test_the_report_names_the_claim_its_key_and_the_command_that_discharges_it(
        monkeypatch, capsys, tmp_path):
    """The remedy has to be IN the artefact. A veto whose discharge lives in a brief
    the reader does not have open is a veto they will resolve by dropping the gate,
    which is the outcome this whole issue exists to avoid."""
    report, _got = _claiming_round(monkeypatch, capsys, tmp_path)
    assert "### Unverifiable claims" in report
    assert f"`{ENACTMENT}` — the enactment drops to 8.16 MB" in report
    assert f"--acknowledge {ENACTMENT}" in report
    assert "unacknowledged" in report
    # And the ledger's other half: an issue is opened whatever the deferral gate
    # says, on the same footing as an escalation.
    assert "whatever\n`review_panel.file_deferral_issues` says" in report \
        or "whatever `review_panel.file_deferral_issues` says" in report


def test_an_acknowledgement_is_inherited_by_the_next_round(monkeypatch, capsys,
                                                           tmp_path):
    """An unverifiable claim does not stop being unverifiable because a round ended.
    A cycle that forgot the acknowledgement between rounds would put the identical
    question to the same person every round — the permanent HOLD arriving one round
    later, wearing a discharge — so the register travels in the payload exactly as
    `escalated` does."""
    _r1, first = _claiming_round(monkeypatch, capsys, tmp_path,
                                 acknowledge=[ENACTMENT])
    p1 = tmp_path / "r1.json"
    assert first["acknowledged"] == {ENACTMENT: 1}
    _r2, second = _claiming_round(monkeypatch, capsys, tmp_path, round_no=2,
                                  baseline=[str(p1)])
    # Nothing was passed on the command line this time.
    assert second["acknowledged"] == {ENACTMENT: 1}
    assert second["round_stop"]["veto"] == []


def test_an_acknowledgement_naming_no_claim_this_round_raised_is_said_out_loud(
        monkeypatch, capsys, tmp_path):
    """`_claim_norm` absorbs spelling and not rewording, and says so. A judge that
    words the claim differently next round mints a different key, so the caller's
    acknowledgement matches nothing — and the one outcome ruled out is silence,
    because the caller would read it as the acknowledgement having landed."""
    _report, got = _claiming_round(monkeypatch, capsys, tmp_path,
                                   acknowledge=["uc-deadbeefcafe"])
    assert any("--acknowledge uc-deadbeefcafe names no unverifiable claim this "
               "round raised" in n for n in got["config_notes"]), got["config_notes"]


@pytest.mark.parametrize("junk", ["", "deadbeefdeadbeef", "uc-", "uc-nothexvalue1",
                                  "uc-1234abcd", "uc-1234abcdef012"])
def test_a_value_that_is_not_an_obligation_key_is_refused_and_says_what_that_costs(
        monkeypatch, capsys, tmp_path, junk):
    """The same door `--escalated` is checked at, and the refusal has to name the
    cost for the same reason: an ignored acknowledgement is an obligation that goes
    on vetoing while the caller believes it discharged.

    A finding key is refused too, and deliberately — the two flags sit two lines
    apart in the parser, and a key pasted into the wrong one must be told about
    rather than silently matching nothing for the rest of the cycle."""
    _report, got = _claiming_round(monkeypatch, capsys, tmp_path, acknowledge=[junk])
    assert any("is not the shape of an obligation key" in n
               for n in got["config_notes"]), got["config_notes"]
    assert got["round_stop"]["confident"] is False


def test_there_is_no_flag_that_accepts_every_claim_at_once(monkeypatch, capsys,
                                                           tmp_path):
    """The failure mode on the far side of this one. A gate that always passes is
    worse than one that always holds, because it looks like assurance — so
    acknowledging is per claim and the only way to accept two is to name two."""
    def two(clusters, diff, model, pr, budget=None, coverage=None, ci="", **_kw):
        numbered = [(n, g) for n, items in sorted((coverage or {}).items())
                    for g in items]
        entries = [{"declarations": [i], "claim": c,
                    "resolvable_in_harness": False, "reason": "needs the system"}
                   for i, c in enumerate(("the enactment drops to 8.16 MB",
                                          "no stored row is left mixed"))]
        return [], None, panel._coverage_ruling(panel_core._rulings(entries), numbered)

    _stub_panel(monkeypatch, findings=[])
    monkeypatch.setattr(panel, "review_llm", lambda *a, **k: panel.ReviewerRun(
        [], None, 10, ["the enactment size", "the stored corpus"]))
    monkeypatch.setattr(panel, "adjudicate", two)
    out = tmp_path / "two.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=1, max_rounds=3, acknowledge=[ENACTMENT]) == 0
    capsys.readouterr()
    got = json.loads(out.read_text())
    assert [c["acknowledged"] for c in got["unresolved_claims"]] == [True, False]
    assert len(got["round_stop"]["veto"]) == 1
    assert got["round_stop"]["confident"] is False


def test_the_judge_is_asked_for_the_ruling_in_the_shape_this_file_reads():
    """The prompt and the parser have to agree, and the prompt is the artefact — a
    schema this file reads out of `JUDGE_PROMPT` cannot drift from the text a model
    is sent, but the INSTRUCTION beside it can."""
    prompt = panel_core.JUDGE_PROMPT
    assert '"coverage_rulings"' in prompt
    assert '"resolvable_in_harness"' in prompt
    # The declarations are pointed at by NUMBER, and the listing says so where the
    # numbers are printed.
    assert "the bracketed declaration NUMBERS this claim merges" in prompt
    assert "the bracketed number is the declaration id" in prompt
    # One entry per claim, not per declaration — the property that makes a fifth
    # seat restating a claim cost the round nothing.
    assert "One entry per CLAIM, not per declaration" in prompt
    # And the tie-break is towards vetoing, said to the model in its own words.
    assert "When you cannot tell, answer `true`" in prompt
    # It is told what the `false` actually buys, so it is not writing one under the
    # impression that it settles anything.
    assert "somebody has to acknowledge by hand" in prompt


def test_an_obligation_key_is_not_a_finding_key_and_the_two_cannot_be_swapped():
    """They meet in the argument parser, two lines apart. A prefix nothing else uses
    is what stops `--acknowledge <finding key>` matching nothing in silence — and
    it keeps an 8-hex digest, which reads as an API key to every secret scanner, out
    of a report that gets posted as a PR comment."""
    key = panel.claim_key("the enactment drops to 8.16 MB")
    assert panel.is_claim_key(key) and not panel._is_key(key)
    assert not panel.is_claim_key("deadbeefdeadbeef")
    assert panel._is_key("deadbeefdeadbeef")


def test_one_claim_keeps_one_key_across_the_spellings_a_rewrite_changes():
    """Content-addressed so two rounds raising the same claim raise it under the same
    key, and one acknowledgement discharges it for the rest of the cycle. The limit
    is stated rather than papered over: it absorbs spelling, not rewording."""
    assert panel.claim_key("the enactment drops to 8.16 MB") == ENACTMENT
    same = ["the enactment drops to 8.16 MB",
            "The enactment drops to 8.16 MB.",
            "the  enactment   drops to 8.16 MB"]
    assert len({panel.claim_key(c) for c in same}) == 1
    assert panel.claim_key("the enactment is smaller now") != panel.claim_key(same[0])


# ------------------------------------------- the two ways a claim could have vanished
#
# Both found by the codex pass on this change, and both are the same failure: a
# declaration suppressed from the veto list whose claim never reached the ledger. That
# is precisely the disappearance Part 2 exists to make impossible, so both are refused
# rather than resolved — the declarations go on vetoing under the line they always had.


def test_a_seat_that_repeated_itself_leaves_both_declarations_unruled():
    """The mapping is keyed by `(reviewer, declaration)`, because `coverage_veto` walks
    seats and gap TEXT and has no numbers to look one up by. A seat that wrote the same
    gap twice therefore gives two declaration numbers one key, and two rulings on them
    would overwrite each other — suppressing both gaps from the veto while only the
    surviving obligation reached the payload. One claim, gone from both."""
    meta = {"claude": {"ran": True, "code_blind": False,
                       "could_not_assess": ["the ceiling", "the ceiling"]}}
    ruling = _ruled(meta,
                    {"declarations": [0], "claim": "the jsonb ceiling holds at 8.16 MB",
                     "resolvable_in_harness": False, "reason": "needs Postgres"},
                    {"declarations": [1], "claim": "no stored row is left mixed",
                     "resolvable_in_harness": False, "reason": "needs the corpus"})
    assert not ruling.unresolvable
    assert panel.coverage_veto(meta, None, 0, 1_000, ci_status="PASS",
                               coverage=ruling) == \
        ["claude could not assess: the ceiling", "claude could not assess: the ceiling"]


def test_two_claims_that_hash_alike_are_refused_rather_than_merged():
    """A truncated digest can collide, and the two consequences are not symmetrical:
    one claim would be absent from the ledger, and one `--acknowledge` would discharge
    both. Twelve hex characters make it negligible; refusing it makes it fail-closed,
    which is the direction every other branch in this resolver takes."""
    meta = _two_seats(claude=("the ceiling", "the corpus"), codex=())
    collide = [
        {"declarations": [0], "claim": "the jsonb ceiling holds at 8.16 MB",
         "resolvable_in_harness": False, "reason": "needs Postgres"},
        {"declarations": [1], "claim": "no stored row is left mixed",
         "resolvable_in_harness": False, "reason": "needs the corpus"},
    ]
    forced = panel_core._rulings(collide)
    # Force the collision rather than searching for one: the resolver's rule is what
    # is under test, not the hash's spread.
    with mock.patch.object(panel_rounds, "claim_key", lambda _c: "uc-000000000000"):
        ruling = panel_rounds._coverage_ruling(forced, _numbered(meta))
    # The first claim keeps the key it minted; the second is refused, and its
    # declaration goes back to the line it always produced. Two claims in, two veto
    # lines out — neither is suppressed by an obligation that does not name it.
    assert [ob.claim for ob in ruling.unresolvable.values()] == \
        ["the jsonb ceiling holds at 8.16 MB"]
    assert panel.coverage_veto(meta, None, 0, 1_000, ci_status="PASS",
                               coverage=ruling) == [
        "claude could not assess: the corpus",
        "an unverifiable claim is unacknowledged [uc-000000000000]: the jsonb "
        "ceiling holds at 8.16 MB — needs Postgres"]


def test_a_ruling_can_delete_lines_but_never_the_last_one():
    """The invariant stated precisely, because the loose version is wrong in a way
    that matters. Merging IS deletion — four seats stating one limit become one
    obligation where they were four vetoes — and that is the whole of the seat-count
    fix. What no ruling can do is leave the list empty where it was not empty before,
    which is the only thing `confident` reads."""
    meta = _two_seats()
    for entries in ([CEILING, IMPORT],
                    [{**CEILING, "declarations": [0, 1, 2],
                      "claim": "everything about this PR"}],
                    [{**CEILING, "declarations": [0]}]):
        ruling = _ruled(meta, *entries)
        after = panel.coverage_veto(meta, None, 0, 1_000, ci_status="PASS",
                                    coverage=ruling)
        before = panel.coverage_veto(meta, None, 0, 1_000, ci_status="PASS")
        assert 0 < len(after) <= len(before), entries
        assert panel.round_stop(1, 2, [], [], after)["confident"] is False
