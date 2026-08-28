"""#274 — every decision owed to a human leaves by ONE door, and the door emits.

Two halves, and the second is the one that matters. The first is the door
itself: refusing a flag with no reason, naming an unrecognised class rather than
dropping it, not repeating itself, never costing its caller the run. The second
is that the four producers actually go through it.

That second half is written the way it is because of what #274 measured. The
escalation path was documented at length in three skill files and had never once
been exercised through its own API — ``deferred: 0`` across sixty-five rounds and
thirty days. On the day this landed, three more mechanisms in this repo were
found in the same state: ``qb-reconcile``'s systemd units, ``HUMAN_EDGE_SECRET``
at the edge, and #210's stash guard — each shipped, each never wired up. A test
that asserts a helper exists would pass in every one of those four cases. So the
producer tests here drive the real code path and assert a post came out of it.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import epic  # noqa: E402
import needs_human as nh  # noqa: E402
import panel  # noqa: E402
import panel_core  # noqa: E402
import panel_rounds  # noqa: E402
import preland  # noqa: E402

HEAD = "a" * 40


@pytest.fixture
def door(monkeypatch, tmp_path):
    """The door, open, with the HTTP replaced and nothing else.

    :func:`needs_human._board_json` is doubled — **the one function that touches
    the network** — so every test below runs the real `announce`: the refusals,
    the class normalisation, the dedupe, the body it builds and the blocker write
    are the code under test, not a stub of them.

    It used to double :func:`_post` instead, and that was a hole rather than a
    style: when #523 added a second write, it went straight past the double and
    the suite made real HTTP requests to whatever board this host resolves. Every
    test still passed, because the blocker write swallows failures by design — so
    the apparatus reported success for a call it was supposed to be preventing.
    Doubling the single network function is what makes that unrepresentable.

    Returns the list of posted `/post` bodies, which is the assertion every
    producer test makes; `.blockers` on it carries the `/blockers` bodies.
    """
    class Posted(list):
        """A list that also carries the blocker writes, so the fixture keeps its
        shape for the twenty-odd tests that already index it."""
        blockers: list[dict]

    posted = Posted()
    blockers: list[dict] = []

    def fake_board_json(path, body):
        if path == "/blockers":
            blockers.append(body)
            # The condition comes back, because the real board returns it — that
            # is what lets a producer see an old board silently dropping it
            # (#576). A double that swallowed it would make every test below
            # exercise the "this board has not been upgraded" path.
            return ({"blocker": {"id": f"b{len(blockers)}",
                                 "condition": body.get("condition", "")},
                     "raised": True}, "")
        posted.append(body)
        return {"id": 100 + len(posted)}, ""

    monkeypatch.setenv("QUARTERBACK_NEEDS_HUMAN", "on")
    monkeypatch.delenv("QUARTERBACK_NEEDS_HUMAN_TO", raising=False)
    monkeypatch.setattr(nh, "SEEN_PATH", tmp_path / "seen.json")
    monkeypatch.setattr(nh, "_board_json", fake_board_json)
    # Refuse the network outright for the duration. The double above is the
    # mechanism; this is the proof it is complete — a write added later that
    # reaches for urlopen directly fails loudly here instead of quietly posting
    # to a live board.
    def no_network(*a, **k):  # pragma: no cover - only runs if the double leaks
        raise AssertionError("a needs_human test reached the network")
    monkeypatch.setattr(nh.urllib.request, "urlopen", no_network)
    posted.blockers = blockers
    return posted


# ----------------------------------------------------------------- vocabulary


def test_the_two_arms_of_the_vocabulary_are_one_tuple():
    """#279 defines the classes once, and this module must not be a fifth
    spelling of them. It has two arms — the canonical file when a checkout is in
    reach, a pinned copy when it is not — and this asserts they agree WHICHEVER
    ran, so it means something in the loops sandbox (no `app/` copied in, so the
    fallback) and on a developer's box (a checkout, so the import) alike.

    The other half of the guarantee, that the pinned copy equals what
    `app/needs_human.py` actually says, is `tests/test_needs_human_drift.py` in
    the app suite — the only suite where both files are readable at once.
    """
    assert nh._FALLBACK_CLASSES == tuple(nh.NEEDS_HUMAN_CLASSES)
    assert nh.VOCABULARY_FROM


def test_no_app_in_reach_is_not_a_reason_to_have_no_vocabulary():
    """An installed harness is `share/quarterback-harness/loops` with nothing
    beside it, which is the ordinary case rather than the broken one."""
    assert nh._canonical(Path("/nonexistent/app/needs_human.py")) is None
    assert len(nh.NEEDS_HUMAN_CLASSES) == 7


def test_a_file_that_is_not_the_module_is_not_loaded_as_it(tmp_path):
    """A stray `needs_human.py` must not become the vocabulary by being in the path."""
    impostor = tmp_path / "needs_human.py"
    impostor.write_text("SOMETHING_ELSE = 1\n")
    assert nh._canonical(impostor) is None


def test_a_module_that_will_not_import_costs_the_fallback_not_the_run(tmp_path):
    """This runs at import time of a module every loop imports."""
    broken = tmp_path / "needs_human.py"
    broken.write_text("raise RuntimeError('half-written checkout')\n")
    assert nh._canonical(broken) is None


def test_case_and_space_are_normalised_and_nothing_else_is():
    assert nh.class_or_none(" UI ") == "ui"
    assert nh.class_or_none("Decision") == "decision"
    assert nh.class_or_none("authorisation") is None
    assert nh.class_or_none(5) is None


# ------------------------------------------------------------------- the door


def test_a_flag_with_no_reason_is_refused_and_says_so(door):
    """#279 makes the evidence CHECK a biconditional; the door keeps the rule.

    Worse on a queue than in a column: a bare flag in a database is a row nobody
    can act on, and a bare flag here is somebody's afternoon interrupted for a
    question that was never stated.
    """
    said = nh.announce(cls="decision", reason="   ", summary="something")
    assert "NOT announced" in said and "costs a reason" in said
    assert posted_nothing(door)


def test_a_reason_with_no_class_is_still_announced_under_other(door):
    """A class is normalisable and a reason is not, so they fail differently.

    Dropping the announcement for an unrecognised spelling would lose the whole
    escalation over a typo. Filing it silently under the wrong class would lose
    it in a count. So it goes to `other` and the spelling is NAMED — the same
    thing `needs_human_unknown` does at ingest.
    """
    said = nh.announce(cls="authorisation", reason="may I spend money", summary="s")
    assert "(other)" in said
    assert door[0]["summary"].startswith("needs a human (other):")
    assert "unrecognised class 'authorisation'" in door[0]["detail"]


def test_the_post_is_a_stuck_post_carrying_the_class_the_label_and_the_reason(door):
    nh.announce(cls="ui", reason="does the chip read right on a phone",
                summary="the blocked chip", repo="o/r",
                refs=[{"kind": "pr", "value": "9", "repo": "o/r"}])
    body = door[0]
    assert body["type"] == "stuck"
    assert body["summary"] == "[o/r] needs a human (ui): the blocked chip"
    assert "class:  ui" in body["detail"]
    assert "label:  needs-human/ui" in body["detail"]
    assert "reason: does the chip read right on a phone" in body["detail"]
    assert body["refs"] == [{"kind": "pr", "value": "9", "repo": "o/r"}]


def test_the_headline_a_reader_composes_is_the_one_the_post_carries(door):
    """#569 moved deduplication of a fleet-wide condition onto the board, and the board
    keeps no dedupe table: the record of an announcement IS the announcement. So a
    producer asking *has another machine already rung this bell* has to recognise its own
    headline coming back off `GET /board`, and it composes the string it looks for with
    the same function that wrote it.

    A prefix has to survive that composition intact, which is why `headline` neither
    trims nor truncates — `announce` does both, and `.strip()` on a prefix ending in a
    space would silently eat the space and match the wrong row's post."""
    nh.announce(cls="environment", reason="nothing can land", summary="landed: 3 ready",
                repo="o/r")

    whole = nh.headline(cls="environment", repo="o/r", summary="landed: 3 ready")
    prefix = nh.headline(cls="environment", repo="o/r", summary="landed: ")

    assert door[0]["summary"] == whole
    assert prefix.endswith("landed: ")
    assert door[0]["summary"].startswith(prefix)


def test_a_headline_prefix_does_not_match_another_row_or_another_repo(door):
    """The prefix carries the repo, the class and the row name, and matching on less
    than all three is how one repository's stalled queue speaks for another's."""
    posted = nh.headline(cls="environment", repo="o/r", summary="landed: 3 ready")

    assert not posted.startswith(nh.headline(cls="environment", repo="o/r",
                                             summary="queue: "))
    assert not posted.startswith(nh.headline(cls="environment", repo="o/other",
                                             summary="landed: "))


def test_switching_it_off_is_silence_and_not_a_refusal(door, monkeypatch):
    """Off is a decision, so it must not print a diagnostic on every run."""
    for off in ("", "0", "off", "no", "FALSE"):
        monkeypatch.setenv("QUARTERBACK_NEEDS_HUMAN", off)
        assert nh.announce(cls="ui", reason="r", summary="s") == ""
    assert posted_nothing(door)


def test_a_box_on_no_board_says_nothing(monkeypatch, tmp_path):
    """The ordinary case for a machine that is not enrolled — and it is silent."""
    monkeypatch.setenv("QUARTERBACK_NEEDS_HUMAN", "on")
    monkeypatch.setattr(nh, "SEEN_PATH", tmp_path / "seen.json")
    monkeypatch.setattr(nh, "board_config", lambda: ("", "", "no board configured"))
    said = nh.announce(cls="ui", reason="r", summary="s")
    assert "NOT announced" in said and "no board configured" in said


def test_a_board_that_will_not_answer_is_reported_never_raised(monkeypatch, tmp_path):
    """An escalation that cannot be announced is still an escalation."""
    monkeypatch.setenv("QUARTERBACK_NEEDS_HUMAN", "on")
    monkeypatch.setattr(nh, "SEEN_PATH", tmp_path / "seen.json")
    monkeypatch.setattr(nh, "board_config", lambda: ("http://b", "t", ""))

    def boom(_req, timeout=0, context=None):
        raise OSError("connection reset")

    monkeypatch.setattr(nh.urllib.request, "urlopen", boom)
    said = nh.announce(cls="ui", reason="r", summary="s")
    assert "NOT announced (ui)" in said and "unreachable" in said


def test_the_same_question_is_asked_once_a_day_not_once_a_tick(door):
    """A pre-land gate runs on every attempt; an epic runs on a timer."""
    for _ in range(4):
        nh.announce(cls="decision", reason="r", summary="s", key="repo:7:decision")
    assert len(door) == 1


def test_a_different_question_is_a_different_post(door):
    nh.announce(cls="decision", reason="r", summary="s", key="repo:7:decision")
    nh.announce(cls="ui", reason="r", summary="s", key="repo:7:ui")
    assert len(door) == 2


def test_the_window_expires_so_a_question_owed_for_days_is_asked_again(door,
                                                                      monkeypatch):
    nh.announce(cls="decision", reason="r", summary="s", key="k")
    later = time.time() + nh.REPEAT_AFTER + 1
    monkeypatch.setattr(nh.time, "time", lambda: later)
    nh.announce(cls="decision", reason="r", summary="s", key="k")
    assert len(door) == 2


def test_an_unkeyed_announcement_is_never_suppressed(door):
    """`key=""` means "this happens once per run" and must not be deduplicated."""
    nh.announce(cls="decision", reason="r", summary="s")
    nh.announce(cls="decision", reason="r", summary="s")
    assert len(door) == 2


def test_an_unwritable_cache_costs_a_duplicate_post_not_the_announcement(
        door, monkeypatch, tmp_path):
    monkeypatch.setattr(nh, "SEEN_PATH", tmp_path / "no" / "such" / "dir" / "s.json")
    monkeypatch.setattr(nh.Path, "mkdir",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    nh.announce(cls="ui", reason="r", summary="s", key="k")
    nh.announce(cls="ui", reason="r", summary="s", key="k")
    assert len(door) == 2


def test_the_addressee_comes_from_the_environment_then_the_repo(door, monkeypatch):
    """Never invented: a name on a queue somebody never agreed to hold is worse
    than an undirected post, which still answers "what is the fleet stuck on"."""
    assert "to" not in door_post(door, nh.announce, cls="ui", reason="r", summary="s")
    cfg = {"needs_human": {"to": "zeus/flint-lumen"}}
    assert door_post(door, nh.announce, cls="ui", reason="r", summary="s",
                     cfg=cfg)["to"] == "zeus/flint-lumen"
    monkeypatch.setenv("QUARTERBACK_NEEDS_HUMAN_TO", "rich")
    assert door_post(door, nh.announce, cls="ui", reason="r", summary="s",
                     cfg=cfg)["to"] == "rich"


def posted_nothing(posted: list) -> bool:
    return posted == []


def door_post(posted: list, fn, **kw) -> dict:
    fn(**kw)
    return posted[-1]


# ------------------------------------------------- door 1: epic.py's ruling


def work(**over):
    body = dict(num=41, title="buy a licence for the thing", checked=False,
                issue_state="OPEN", pr_number=None, pr_state=None, stage="blocked",
                doable=False, reason="needs a commercial licence",
                human_class="decision")
    return epic.IssueWork(**{**body, **over})


EPIC_CFG = {"path": "/w", "github": "acme/r", "name": "r"}


def test_an_epic_ruling_reaches_the_board_and_not_just_stdout(door, capsys):
    """#274's first measurement: an unattended epic run's entire human-decision
    output lived in a systemd journal."""
    epic.work_issue(EPIC_CFG, work(), execute=True)
    assert len(door) == 1
    body = door[0]
    assert body["type"] == "stuck"
    assert "needs a human (decision)" in body["summary"]
    assert "#41" in body["summary"]
    assert "needs a commercial licence" in body["detail"]
    assert body["refs"] == [{"kind": "issue", "value": "41", "repo": "acme/r"}]
    assert "announced on the board" in capsys.readouterr().out


def test_a_dry_run_plans_and_does_not_interrupt_anybody(door):
    """A plan preview announcing what a run WOULD be stuck on is how a queue
    fills with questions nobody is blocked by."""
    epic.work_issue(EPIC_CFG, work(), execute=False)
    assert posted_nothing(door)


def test_an_untriaged_issue_is_an_environment_problem_not_a_decision(door):
    """The judge never answered — nobody has a decision to make. Every branch
    that produces one of these is about this box."""
    epic.work_issue(EPIC_CFG, work(doable=None, reason="untriaged (no judge available)",
                                   human_class=epic.UNTRIAGED_CLASS), execute=True)
    assert "needs a human (environment)" in door[0]["summary"]


def test_the_same_ruling_is_announced_once_however_often_the_epic_runs(door):
    for _ in range(3):
        epic.work_issue(EPIC_CFG, work(), execute=True)
    assert len(door) == 1


def test_the_judge_names_the_class_and_an_unknown_one_falls_back(monkeypatch):
    """`taste` and `environment` are different afternoons; a fixed class loses
    the distinction #279 built the vocabulary for."""
    def judge(verdict):
        monkeypatch.setattr(epic.shutil, "which", lambda _: "/usr/bin/claude")
        monkeypatch.setattr(epic.subprocess, "run",
                            lambda *a, **k: subprocess.CompletedProcess(
                                [], 0, json.dumps(verdict), ""))
        return epic.triage(work(), "opus")

    assert judge({"doable": False, "reason": "r", "needs_human": "TASTE"})[3] == "taste"
    assert judge({"doable": False, "reason": "r"})[3] == epic.RULING_CLASS
    assert judge({"doable": False, "reason": "r", "needs_human": "vibes"})[3] == \
        epic.RULING_CLASS
    # A doable issue is waiting on nobody, however the judge answered.
    assert judge({"doable": True, "reason": "r", "needs_human": "ui"})[3] == ""


def test_the_plan_carries_the_class_so_a_consumer_need_not_parse_english():
    entry = epic.plan_entry(EPIC_CFG, work(), 0)
    assert entry["needs_human"] == "decision"
    assert entry["action"] == "skip-blocked"


# --------------------------------------------- door 2: preland's HOLD reasons


def hold(*checks) -> dict:
    return {"verdict": preland.HOLD, "repo": "acme/r", "pr": 7, "branch": "feat/x",
            "base": "main", "head_sha": HEAD,
            "needs_human": [{"check": c.name, **h} for c in checks for h in c.human]}


def test_a_hold_only_a_person_can_clear_reaches_the_board(door):
    c = preland.Check("ci", "failed")
    c.hold_for_human("decision", "the PR has no checks at all")
    said = preland.announce_hold(hold(c), {})
    assert len(door) == 1 and len(said) == 1
    assert "needs a human (decision)" in door[0]["summary"]
    assert "PR #7 cannot land" in door[0]["summary"]
    assert door[0]["refs"] == [{"kind": "pr", "value": "7", "repo": "acme/r"}]


def test_a_hold_a_command_can_clear_says_nothing(door):
    """The whole point of `Check.human` existing: a gate that posted every HOLD
    would be a CI log with an addressee."""
    c = preland.Check("checkout", "failed",
                      reasons=["3 tracked file(s) modified or staged"])
    assert preland.announce_hold(hold(c), {}) == []
    assert posted_nothing(door)


def test_a_reconcile_is_mechanical_work_and_never_an_escalation(door):
    c = preland.Check("migrations", "reconcile")
    c.hold_for_human("decision", "the reconciler says STOP")
    out = {**hold(c), "verdict": preland.RECONCILE}
    assert preland.announce_hold(out, {}) == []
    assert posted_nothing(door)


def test_several_sentences_of_one_class_are_one_interruption(door):
    """PR #131 held on two independent counts. The question a person is being
    asked is what KIND of judgement they owe, and that is the class."""
    a = preland.Check("review", "failed")
    a.hold_for_human("environment", "board unreachable")
    b = preland.Check("queue", "failed")
    b.hold_for_human("environment", "queue unreadable")
    c = preland.Check("ci", "failed")
    c.hold_for_human("decision", "no checks at all")
    preland.announce_hold(hold(a, b, c), {})
    assert len(door) == 2
    assert {p["summary"].split("needs a human (")[1].split(")")[0]
            for p in door} == {"environment", "decision"}
    env = next(p for p in door if "(environment)" in p["summary"])
    assert "2 objections (queue, review)" in env["summary"]


def test_the_same_head_is_announced_once_and_a_new_one_again(door):
    c = preland.Check("ci", "failed")
    c.hold_for_human("decision", "no checks at all")
    preland.announce_hold(hold(c), {})
    preland.announce_hold(hold(c), {})
    assert len(door) == 1
    preland.announce_hold({**hold(c), "head_sha": "b" * 40}, {})
    assert len(door) == 2


def test_the_classification_rides_beside_the_reason_and_never_replaces_it():
    """`reasons` is what every existing consumer reads, verbatim — the skills
    relay it word for word, and a HOLD that stopped listing one of its reasons
    would be a HOLD reported as a mood."""
    c = preland.Check("ci", "failed")
    c.hold_for_human("decision", "no checks at all")
    assert c.reasons == ["no checks at all"]
    assert c.as_dict()["reasons"] == ["no checks at all"]
    assert c.as_dict()["needs_human"] == [{"class": "decision",
                                           "reason": "no checks at all"}]


def test_the_payload_splits_the_mechanical_hold_from_the_human_one():
    mech = preland.Check("checkout", "failed", reasons=["commit or stash them"])
    human = preland.Check("ci", "failed")
    human.hold_for_human("decision", "no checks at all")
    out = preland.payload({"github": "acme/r"}, {"number": 7, "headRefName": "feat/x",
                                                 "headRefOid": HEAD},
                          [mech, human], preland.BaseRef("main"))
    assert len(out["reasons"]) == 2
    assert out["needs_human"] == [{"check": "ci", "class": "decision",
                                   "reason": "no checks at all"}]


def test_every_class_preland_uses_is_in_the_vocabulary():
    """A misspelt class would leave every by-class count while still reading as a
    flag — the direction that hides the signal."""
    import re
    src = (Path(preland.__file__)).read_text(encoding="utf-8")
    used = set(re.findall(r'hold_for_human\(\s*"([a-z]+)"', src))
    assert used, "no hold_for_human call sites found — the classification was removed"
    assert used <= set(nh.NEEDS_HUMAN_CLASSES)


# ------------------------------------------------- door 3: the panel's seats


def test_a_seat_can_say_no_diff_settles_this_and_it_survives_the_parse():
    [f] = panel_core._to_findings("codex", [{
        "severity": "P2", "file": "a.py", "line": 3, "title": "which shape",
        "needs_human": True, "needs_human_class": " Decision ",
        "needs_human_reason": " whether this is one table or two "}])
    assert (f.needs_human, f.needs_human_class) == (True, "decision")
    assert f.needs_human_reason == "whether this is one table or two"


def test_a_bare_flag_is_refused_by_the_panel_before_the_board_has_to(door):
    """The panel never sends a shape the board would reject, so the refusal is
    not something an operator reads out of a `needs_human_refused` key later."""
    for bad in ({"needs_human": True},
                {"needs_human": True, "needs_human_class": "ui"},
                {"needs_human": True, "needs_human_reason": "why"},
                {"needs_human": True, "needs_human_class": "vibes",
                 "needs_human_reason": "why"},
                {"needs_human": True, "needs_human_class": "ui",
                 "needs_human_reason": "   "}):
        [f] = panel_core._to_findings("codex", [{"title": "t", **bad}])
        assert f.needs_human is False
        assert (f.needs_human_class, f.needs_human_reason) == ("", "")


def test_evidence_with_no_flag_behind_it_is_not_an_escalation():
    """An orphan class reads exactly like a declaration somebody withdrew."""
    [f] = panel_core._to_findings("codex", [{
        "title": "t", "needs_human_class": "ui", "needs_human_reason": "look at it"}])
    assert f.needs_human is False and f.needs_human_class == ""


def test_a_string_no_is_not_an_escalation():
    """`bool("false")` is True, and a manufactured escalation is the one error
    nobody can spot afterwards."""
    [f] = panel_core._to_findings("codex", [{
        "title": "t", "needs_human": "false", "needs_human_class": "ui",
        "needs_human_reason": "why"}])
    assert f.needs_human is False


def test_the_flag_survives_the_merge_and_is_scored_per_reporter():
    """A flag credited to everyone who raised the defect makes the member that
    saw the design question and the member that missed it one row."""
    saw = panel_core.Finding("codex", "P2", "a.py", 3, "t", needs_human=True,
                             needs_human_class="decision", needs_human_reason="which")
    missed = panel_core.Finding("pi", "P3", "a.py", 3, "t")
    c = panel_rounds.Canonical("1-F01", "P2", "a.py", 3, "t", "confirmed",
                               reported_by=[missed, saw])
    d = c.as_dict()
    assert d["needs_human"] is True
    assert d["needs_human_by"] == ["codex"]
    assert d["needs_human_class"] == "decision" and d["needs_human_reason"] == "which"
    by = {r["reviewer"]: r for r in d["reported_by"]}
    assert by["codex"]["needs_human"] is True and by["pi"]["needs_human"] is False
    assert by["pi"]["needs_human_class"] == ""


def test_two_reporters_disagreeing_about_the_class_are_never_blended():
    """Both are kept per reporter; the finding-level pair comes from ONE of them,
    whole, because a class from one account and a reason from another is a
    statement neither member made."""
    a = panel_core.Finding("codex", "P2", "a.py", 3, "t", needs_human=True,
                           needs_human_class="taste", needs_human_reason="the name")
    b = panel_core.Finding("pi", "P2", "a.py", 3, "t", needs_human=True,
                           needs_human_class="ui", needs_human_reason="the screen")
    d = panel_rounds.Canonical("1-F01", "P2", "a.py", 3, "t", "confirmed",
                               reported_by=[a, b]).as_dict()
    assert (d["needs_human_class"], d["needs_human_reason"]) == ("taste", "the name")
    assert d["needs_human_by"] == ["codex", "pi"]
    by = {r["reviewer"]: r for r in d["reported_by"]}
    assert by["pi"]["needs_human_class"] == "ui"
    assert by["pi"]["needs_human_reason"] == "the screen"


def test_the_seat_prompt_names_every_class_and_tells_them_from_could_not_assess():
    """A vocabulary the seats are not given is a vocabulary the seats cannot use
    — and the field it is nearest to means the opposite thing."""
    envelope = panel_core._FINDINGS_ENVELOPE
    for cls in nh.NEEDS_HUMAN_CLASSES:
        assert cls in envelope, f"the seats are never told about {cls!r}"
    assert '"needs_human"' in envelope
    assert "`could_not_assess`" in envelope
    assert "no context would close it" in envelope.lower()


def confirmed(**over):
    body = {"key": "ab" * 8, "synthesis": "which shape", "needs_human": True,
            "needs_human_class": "decision", "needs_human_reason": "one table or two"}
    return {**body, **over}


def test_a_round_that_flags_a_finding_announces_it(door):
    said = panel.announce_escalations(
        {"github": "acme/r", "pr": 7, "branch": "feat/x", "head_sha": HEAD,
         "to_fix": [confirmed()]}, {})
    assert len(door) == 1 and len(said) == 1
    assert "needs a human (decision)" in door[0]["summary"]
    assert "1 confirmed finding(s)" in door[0]["summary"]
    assert "one table or two" in door[0]["detail"]
    assert "--escalated-from-board" in door[0]["detail"]


def test_a_dismissed_escalation_is_a_claim_the_judge_already_refused(door):
    """`dismissed` never reaches a person's queue: "the panel disagreed with
    itself" and "you have to decide this" must not arrive as one thing."""
    payload = {"github": "acme/r", "pr": 7, "head_sha": HEAD, "to_fix": [],
               "dismissed": [confirmed()], "sonar_findings": [confirmed()]}
    assert panel.announce_escalations(payload, {}) == []
    assert posted_nothing(door)


def test_a_round_with_nothing_to_escalate_says_nothing(door):
    payload = {"github": "acme/r", "pr": 7, "head_sha": HEAD,
               "to_fix": [confirmed(needs_human=False)]}
    assert panel.announce_escalations(payload, {}) == []
    assert posted_nothing(door)


# ------------------------------- door 4: --escalated reads the board's own list


@pytest.fixture
def findings(monkeypatch):
    """`GET /review/findings`, doubled at panel.py's own seam."""
    answer: dict = {"body": {}, "err": ""}

    def board_get(path, params):
        assert path == "review/findings", path
        answer["params"] = params
        return answer["body"], answer["err"]

    monkeypatch.setattr(panel, "board_get", board_get)
    return answer


def test_the_escalation_list_comes_off_the_board(findings):
    """#279 publishes the list; before this nothing in harness/ read it, and a
    key a fixer had to transcribe out of its own prose is a key nobody
    transcribes — which is why thirty days of rounds recorded zero."""
    findings["body"] = {"needs_human_keys": ["ab" * 8, "cd" * 8]}
    keys, why = panel.board_escalations("acme/r", 7)
    assert why == "" and keys == ["ab" * 8, "cd" * 8]
    assert findings["params"] == {"repo": "acme/r", "pr": 7}


def test_a_missing_list_is_not_a_board_with_none(findings):
    """Absent must not read as clean: a missing field and an empty list have
    different remedies, and only one of them lets a round count the work."""
    findings["body"] = {"findings": []}
    keys, why = panel.board_escalations("acme/r", 7)
    assert keys == [] and "must be named with --escalated by hand" in why


def test_a_missing_list_does_not_pick_a_cause_for_its_own_absence(findings):
    """The message reports the observation and offers the causes; it does not
    choose one, because nothing in the response distinguishes them.

    Two produce the identical absence — a board older than #279, and a PR with
    no recorded review run — and both are real: on the live board a PR with
    rounds returns `needs_human_keys: []` while a PR with none omits the field.
    The first draft of this line asserted "it predates the field" and was wrong
    about a board running current code, which is the same
    inference-from-an-absence the branch exists to refuse. #199's version
    endpoint would let it name the cause instead of listing them.
    """
    findings["body"] = {"findings": []}
    _keys, why = panel.board_escalations("acme/r", 7)
    assert "Either" in why and " or " in why, why
    assert "nothing in the answer says which" in why
    assert "predates the field, so" not in why, (
        "the message is asserting one cause as fact again")


def test_an_empty_list_from_a_board_that_has_the_field_is_an_answer(findings):
    """`[]` and a missing key are the two halves of the distinction, and the
    empty one is a real answer: this PR has been reviewed and nothing on it is
    waiting on a human. It must not acquire the missing-field note."""
    findings["body"] = {"findings": [], "needs_human_keys": []}
    keys, why = panel.board_escalations("acme/r", 7)
    assert keys == [] and why == ""


def test_a_board_that_will_not_answer_is_reported_not_treated_as_empty(findings):
    findings["err"] = "board unreachable at http://b (URLError)"
    keys, why = panel.board_escalations("acme/r", 7)
    assert keys == [] and "board unreachable" in why
    assert "may count one of them as work a fix round can clear" in why


def test_a_list_that_is_not_a_list_is_reported(findings):
    findings["body"] = {"needs_human_keys": "ab" * 8}
    keys, why = panel.board_escalations("acme/r", 7)
    assert keys == [] and "came back as a str" in why


def test_the_flag_needs_a_cycle_exactly_as_escalated_does(monkeypatch):
    """A flag accepted and ignored is a caller believing it asked for something
    this run does not do."""
    monkeypatch.setattr(sys, "argv",
                        ["panel.py", "--pr", "7", "--escalated-from-board"])
    with pytest.raises(SystemExit) as e:
        panel.main()
    assert "--escalated needs a cycle" in str(e.value)


@pytest.mark.parametrize("mode", (["--ask", "a premise"],
                                  ["--premise", "p", "--premise-file", "/tmp/p.json"]))
def test_the_premise_doors_refuse_the_flag_rather_than_ignoring_it(monkeypatch, mode):
    monkeypatch.setattr(sys, "argv", ["panel.py", *mode, "--escalated-from-board"])
    with pytest.raises(SystemExit) as e:
        panel.main()
    assert "--escalated-from-board" in str(e.value)


# ------------------------------------------ what Codex found on the first cut


def test_a_post_that_never_landed_is_not_remembered_as_said(monkeypatch, tmp_path):
    """The dedupe used to be recorded BEFORE the post, so a board that refused
    one suppressed every retry of that question for twelve hours — and the
    escalation was simply lost, which is the failure this module exists to end.

    The two orderings fail in opposite directions and only one is survivable:
    recording afterwards costs a duplicate post if the process dies in between.
    """
    monkeypatch.setenv("QUARTERBACK_NEEDS_HUMAN", "on")
    monkeypatch.setattr(nh, "SEEN_PATH", tmp_path / "seen.json")
    landed: list[dict] = []
    outage = {"on": True}

    def flaky(body):
        if outage["on"]:
            return None, "the board was unreachable (URLError)"
        landed.append(body)
        return 7, ""

    monkeypatch.setattr(nh, "_post", flaky)
    assert "NOT announced" in nh.announce(cls="ui", reason="r", summary="s", key="k")
    outage["on"] = False
    assert "announced on the board" in nh.announce(cls="ui", reason="r",
                                                   summary="s", key="k")
    assert len(landed) == 1
    # …and now it IS remembered, so the retry does not become a second post.
    assert nh.announce(cls="ui", reason="r", summary="s", key="k") == ""
    assert len(landed) == 1


def test_a_new_finding_of_a_class_already_announced_is_still_announced(door):
    """A round-2 `decision` finding on the same commit is a NEW question. Keyed
    on class and head alone it was swallowed behind round 1's for twelve hours."""
    payload = {"github": "acme/r", "pr": 7, "head_sha": HEAD,
               "to_fix": [confirmed(key="ab" * 8)]}
    panel.announce_escalations(payload, {})
    panel.announce_escalations(payload, {})
    assert len(door) == 1, "the same finding announced twice"
    panel.announce_escalations({**payload, "to_fix": [confirmed(key="cd" * 8)]}, {})
    assert len(door) == 2, "a second finding of the same class was swallowed"


def test_a_new_objection_of_a_class_already_announced_is_still_announced(door):
    """Same defect one door over: a second `preland` run on one head can raise an
    objection the first could not — the board came back and the queue check now
    answers."""
    first = preland.Check("review", "failed")
    first.hold_for_human("environment", "board unreachable")
    preland.announce_hold(hold(first), {})
    preland.announce_hold(hold(first), {})
    assert len(door) == 1
    second = preland.Check("queue", "failed")
    second.hold_for_human("environment", "queue unreadable")
    preland.announce_hold(hold(first, second), {})
    assert len(door) == 2



def test_an_interpreter_that_trusts_nothing_still_reaches_the_board(monkeypatch):
    """A uv-installed standalone Python has no CA bundle, so `urllib` there
    fails every HTTPS request with CERTIFICATE_VERIFY_FAILED — and the failure
    arrives as "the board was unreachable", a sentence about a board that is up.

    `qbdata._ssl_context` was written after that bit the dashboard. It matters
    more here: an empty pane gets noticed, a question a person never learns they
    were asked does not. Found by running the measurement for this very issue out
    of the project venv, where every announcement failed against a live board.
    """
    seen: dict = {}

    def urlopen(req, timeout=0, context=None):
        seen["context"] = context
        raise OSError("not actually opening a socket in a test")

    monkeypatch.setattr(nh, "board_config", lambda: ("https://b", "t", ""))
    monkeypatch.setattr(nh.urllib.request, "urlopen", urlopen)
    nh._post({"type": "stuck", "summary": "s"})
    assert "context" in seen, "the post no longer passes an SSL context at all"
    # `None` is the right answer on an interpreter whose default store works;
    # what must never happen is the argument going missing, because then the
    # venv case is back and it fails as an unreachable board.
    expected = nh.ssl_context()
    assert (seen["context"] is None) == (expected is None)


def test_the_read_half_trusts_the_same_store_as_the_write_half(monkeypatch):
    """`--escalated-from-board` goes through `preland.board_request`, and on the
    venv interpreter that read failed while the post beside it succeeded — the
    round then reported "board unreachable" about a board it had just written to,
    and would have counted an escalation as work a fix round can clear."""
    seen: dict = {}

    def urlopen(req, timeout=0, context=None):
        seen["context"] = context
        raise OSError("not actually opening a socket in a test")

    monkeypatch.setattr(preland, "board_config", lambda: ("https://b", "t", ""))
    monkeypatch.setattr(preland.urllib.request, "urlopen", urlopen)
    preland.board_request("review/findings", {"repo": "o/r", "pr": 1})
    assert "context" in seen
    assert (seen["context"] is None) == (nh.ssl_context() is None)


def test_the_ci_states_only_a_person_can_clear_are_the_ones_that_announce(door):
    """#324 made `qbdata.CI_STATES` the one closed vocabulary for CI state, and
    added `blocked`: a run exists and will not execute until somebody presses
    the button. That is a new human door and it announces.

    `red` and `pending` do not, and that is the line the whole preland half of
    #274 is drawn on — a failing build is work an agent does, a pending one
    clears itself, and putting either on a person's queue turns the board into a
    CI log with an addressee.
    """
    assert set(preland.CI_HUMAN_CLASSES) < set(preland.CI_REFUSALS)
    assert set(preland.CI_HUMAN_CLASSES) == {"blocked", "none", "unknown"}
    assert set(preland.CI_HUMAN_CLASSES.values()) <= set(nh.NEEDS_HUMAN_CLASSES)
    # …and "the lookup failed" is not "somebody has to decide something".
    assert preland.CI_HUMAN_CLASSES["unknown"] == "environment"


def test_a_gated_ci_run_reaches_a_person_and_a_red_one_does_not(door, monkeypatch):
    """End to end through `check_ci`, because the mapping above is only worth
    anything if the check actually reads it."""
    def report(state, blocking=True):
        monkeypatch.setattr(preland, "ci_report",
                            lambda pr, repo: _Report(state, blocking))
        return preland.check_ci({"statusCheckRollup": []}, "o/r")

    gated = report("blocked")
    assert [h["class"] for h in gated.human] == ["decision"]
    assert gated.reasons and gated.reasons[0] == gated.human[0]["reason"]
    assert report("red").human == []
    assert report("pending").human == []
    assert report("green", blocking=False).reasons == []


class _Report:
    """The three fields `check_ci` reads off #324's report."""

    def __init__(self, state, blocking):
        self.state, self.blocking = state, blocking
        self.summary, self.reason, self.last_executed = state, "", None


# ------------------------- the row behind the door (#328, #523) --------------


def test_announcing_also_records_a_blocker(door):
    """#274 built the door every producer calls; #328 built the queue behind it.
    The join is here rather than in the six callers, because `announce`'s own
    docstring promised it: it is "the only place that knows the post type, the
    addressee and the wire format, so #328's `blockers` row can become the store
    by changing this function and nothing else"."""
    note = nh.announce(cls="decision", reason="no diff settles this",
                       summary="A or B?", repo="acme/one",
                       refs=[{"kind": "pr", "value": "7", "repo": "acme/one"}])
    assert len(door) == 1, "the post must still be made"
    (row,) = door.blockers
    assert row["subject_kind"] == "pr" and row["subject_value"] == "7"
    assert row["kind"] == "decision" and row["question"] == "A or B?"
    assert "recorded as a blocker" in note


def test_the_post_is_made_even_when_the_row_cannot_be(door, monkeypatch):
    """The doorbell rings first and independently. A board that accepts the post
    and refuses the row has still told somebody — and the note says which half
    failed, on the line an operator is already reading."""
    def half_broken(path, body):
        if path == "/blockers":
            return None, "the board answered HTTP 500"
        door.append(body)
        return {"id": 1}, ""
    monkeypatch.setattr(nh, "_board_json", half_broken)
    note = nh.announce(cls="taste", reason="r", summary="s", repo="acme/one",
                       refs=[{"kind": "issue", "value": "3", "repo": "acme/one"}])
    assert len(door) == 1
    assert "announced on the board" in note
    assert "not recorded as a blocker" in note and "HTTP 500" in note


def test_a_pr_ref_beats_an_issue_ref_as_the_subject(door):
    """A `stuck` carrying both is about the PR: it is the more specific object,
    and a blocker filed against the issue would sit on the wrong phase once #521
    splits fix from land."""
    nh.announce(cls="decision", reason="r", summary="s", repo="acme/one",
                refs=[{"kind": "issue", "value": "3", "repo": "acme/one"},
                      {"kind": "pr", "value": "9", "repo": "acme/one"}])
    (row,) = door.blockers
    assert (row["subject_kind"], row["subject_value"]) == ("pr", "9")


def test_an_escalation_naming_nothing_is_announced_and_not_stored(door):
    """Honest rather than lossy. A blocker's whole value is answering "what is
    waiting on me" with rows; one whose subject is "something, somewhere" answers
    it with noise. The post still carries it."""
    note = nh.announce(cls="environment", reason="r", summary="the box is wrong",
                       repo="acme/one")
    assert len(door) == 1, "it must still be announced"
    assert door.blockers == []
    assert "recorded as a blocker" not in note


def test_an_unrecognised_class_is_stored_as_other_not_dropped(door):
    """#279's rule at ingest, applied to the row: the class is normalised and the
    escalation survives — a blocker refused for a spelling would be a judgement
    lost to a typo."""
    nh.announce(cls="URGENT", reason="r", summary="s", repo="acme/one",
                refs=[{"kind": "pr", "value": "1", "repo": "acme/one"}])
    (row,) = door.blockers
    assert row["kind"] == "other"


def test_a_repeat_inside_the_window_makes_neither_a_post_nor_a_row(door):
    """The post's dedupe governs both, because a second row would be refused by
    the board anyway (the partial unique index) and calling it to find that out
    is a request spent to learn nothing."""
    for _ in range(2):
        nh.announce(cls="decision", reason="r", summary="s", repo="acme/one",
                    key="same-key",
                    refs=[{"kind": "pr", "value": "4", "repo": "acme/one"}])
    assert len(door) == 1 and len(door.blockers) == 1


def test_the_note_distinguishes_a_new_row_from_an_existing_one(door, monkeypatch):
    """Re-raising an identical open question is a no-op at the board, so calling
    this every run is safe — and the operator should be able to tell "I just
    raised this" from "this has been waiting"."""
    def already_open(path, body):
        if path == "/blockers":
            return {"blocker": {"id": "b1"}, "raised": False}, ""
        door.append(body)
        return {"id": 1}, ""
    monkeypatch.setattr(nh, "_board_json", already_open)
    note = nh.announce(cls="ui", reason="r", summary="s", repo="acme/one",
                       refs=[{"kind": "pr", "value": "2", "repo": "acme/one"}])
    assert "already an open blocker" in note


# ------------------------------------------------------- #576: one row per QUESTION


def test_the_machine_is_spelled_one_way(monkeypatch):
    """Short, lowercased, trimmed — because a `condition` outlives every cache a
    hostname used to be compared against. `qb-bump` truncated at the first dot and
    `qb-doctor` did not, which is one box under two names across the two halves of
    one escalation."""
    monkeypatch.setattr(nh.socket, "gethostname", lambda: "  ZEUS.fo.ls ")
    assert nh.machine_id() == "zeus"


def test_a_condition_drops_empty_parts_rather_than_leaving_a_dangling_join():
    """A host-scoped fault on a box whose name could not be read must degrade to
    the bare fault, not to `harness@` — which would be a third spelling of one
    question."""
    assert nh.condition_for("harness", "zeus") == "harness@zeus"
    assert nh.condition_for("harness", "") == "harness"
    assert nh.condition_for(" Unpushed ") == "unpushed"


def test_a_condition_is_bounded_the_way_the_board_bounds_it():
    """Overshooting `MAX_CONDITION` is a 422, and a 422 here refuses an
    escalation. The door trims instead."""
    assert len(nh.condition_for("x" * 400)) == nh.MAX_CONDITION


def test_the_condition_reaches_the_row(door):
    """#576: without it every `environment` escalation about one repo was one row,
    and the second and third questions were answered "already an open blocker"."""
    nh.announce(cls="environment", reason="r", summary="25 commits on no remote",
                repo="acme/one", condition="unpushed",
                refs=[{"kind": "repo", "value": "acme/one"}])
    (row,) = door.blockers
    assert row["condition"] == "unpushed"


def test_no_condition_is_the_empty_string_and_not_a_null(door):
    """NULLs are distinct in a unique index, so a null here would switch the
    deduplication off for every producer that passes nothing — which is most of
    them. The column is NOT NULL and this is what feeds it."""
    nh.announce(cls="decision", reason="r", summary="s", repo="acme/one",
                refs=[{"kind": "pr", "value": "1", "repo": "acme/one"}])
    (row,) = door.blockers
    assert row["condition"] == ""


def test_two_conditions_on_one_subject_are_two_rows(door):
    """The whole of #576, at this layer: same repo, same class, two faults. The
    door must send two distinguishable bodies and leave the board to enforce it."""
    for cond, said in (("landed", "4 PRs ready"), ("unpushed", "25 commits")):
        nh.announce(cls="environment", reason="r", summary=said, repo="acme/one",
                    condition=cond, refs=[{"kind": "repo", "value": "acme/one"}])
    assert [b["condition"] for b in door.blockers] == ["landed", "unpushed"]
    assert len({(b["subject_kind"], b["subject_value"], b["kind"])
                for b in door.blockers}) == 1, \
        "the subject and the class are identical — the condition is the only difference"


def test_a_board_that_drops_the_condition_says_so_on_the_line(door, monkeypatch):
    """A board predating #576 ignores an unknown field and stores the row under the
    old, coarser key. Silent degradation is the failure #576 is filed about, so the
    producer is told on the line it is already printing rather than left to find out
    by counting rows."""
    def old_board(path, body):
        if path == "/blockers":
            return {"blocker": {"id": "b1"}, "raised": True}, ""
        door.append(body)
        return {"id": 1}, ""
    monkeypatch.setattr(nh, "_board_json", old_board)
    note = nh.announce(cls="environment", reason="r", summary="s", repo="acme/one",
                       condition="harness@zeus",
                       refs=[{"kind": "repo", "value": "acme/one"}])
    assert "did not keep the condition" in note and "#576" in note
