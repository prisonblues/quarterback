"""A round the board never saw has to say so where a human will read it (#284).

`record_run` opened with `if not shutil.which("qb"): return`. No stderr line, no
`config_notes` entry, nothing in the payload, nothing in the PR comment — and
`qb` lives in the fleet's own repo (#28), so its absence is an ordinary property
of a host rather than an anomaly. A sweep of 100 PRs found 45 panelled on GitHub,
**30 with no board record at all** and **67 rounds the board never saw** (43
recorded against 110 actual). Every one of them evaporated through that line.

The fix is not "record more" — recording stays best-effort, and a down board must
never fail a review that already ran. The fix is that a failure to record is
**visible**, in the three artefacts a round leaves behind:

* the payload (`config_notes`), which is what a fixer is briefed from;
* the `--json-file` on disk, which is round r+1's baseline;
* the report, and therefore the `--post` PR comment.

So the tests here come in two halves. The first asks `record_run` itself the four
questions it can now answer — no qb, qb refused, qb ran and the board did not
answer, recorded. The second runs a whole round and asserts the answer reached
all three artefacts, because the ordering that makes that true (record BEFORE the
payload is written) is the part a later edit is most likely to undo.
"""

import json
import subprocess
import sys

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402  — `sh` is the `gh` seam
import panel_preflight  # noqa: E402  — owns its own `seat_installed` global
import panel_seats  # noqa: E402  — `record_run` lives here
from conftest import gh_stub  # noqa: E402

#: What `qb record-review` prints on its success branch: the recorded id, on
#: STDOUT. The whole "did the board answer" test rests on which stream carries
#: what, so the double reproduces it rather than paraphrasing it.
QB_OK = "recorded review run 41\n"

#: And its failure branch: exit **0** — deliberately, so a down board cannot fail
#: a review — with the reason on stderr and stdout left empty.
QB_DOWN = "qb: review not recorded (curl: (7) Failed to connect)\n"

#: The real function, captured at import — before conftest's `recorded_runs`
#: fixture has had a chance to replace it on either module.
_REAL_RECORD_RUN = panel_seats.record_run


@pytest.fixture(autouse=True)
def _the_real_record_run(monkeypatch, recorded_runs):
    """This module is the one that must NOT have `record_run` stubbed away.

    `conftest.recorded_runs` replaces it everywhere, autouse, so that no test on
    an enrolled workstation can pipe a run to the live board. This module's whole
    subject is that function, and its own guard is lower down and stronger: it
    stubs `panel_seats.shutil.which` and `panel_seats.subprocess.run`, so the
    real `record_run` executes and reaches a `qb` that does not exist.

    So the real one goes back, on both names. Depending on `recorded_runs`
    explicitly rather than relying on fixture ordering: it makes this a
    documented override of a named fixture instead of a coincidence of
    declaration order, which is what would silently stop holding if either moved.

    Every test here that reaches the panel goes through `_run`, which stubs `qb`
    first — a test that forgot would fail on the missing stub rather than reach a
    board, because `_qb` is also what makes the assertions possible.
    """
    monkeypatch.setattr(panel_seats, "record_run", _REAL_RECORD_RUN)
    monkeypatch.setattr(panel, "record_run", _REAL_RECORD_RUN)


def _qb(monkeypatch, *, present=True, returncode=0, stdout="", stderr="",
        raises=None):
    """Stub `qb record-review`. Returns the dict the call is recorded into."""
    monkeypatch.setattr(panel_seats.shutil, "which",
                        lambda name: "/usr/bin/qb" if present else None)
    seen: dict = {}

    def fake(args, **kw):
        seen["args"], seen["input"] = args, kw.get("input")
        if raises:
            raise raises
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    monkeypatch.setattr(panel_seats.subprocess, "run", fake)
    return seen


# ---------------------------------------------------------------- record_run itself

def test_a_recorded_run_adds_no_note_and_still_names_the_id(capsys, monkeypatch):
    """The silent-success half. A note on every run is noise on every run, and
    then it stops being read on the one run it matters."""
    seen = _qb(monkeypatch, stdout=QB_OK)
    assert panel_seats.record_run({"pr": 7}) == ""
    assert seen["args"] == ["qb", "record-review"]
    assert json.loads(seen["input"])["pr"] == 7
    assert "recorded review run 41" in capsys.readouterr().err


def test_a_host_without_qb_returns_a_note_rather_than_returning_silently(
        capsys, monkeypatch):
    """#284's actual defect, in one assertion: the bare `return`."""
    _qb(monkeypatch, present=False)
    note = panel_seats.record_run({"pr": 7})
    assert "NOT recorded on the board" in note
    assert "no `qb` on this host" in note
    assert "#28" in note                      # where the binary actually lives
    assert note in capsys.readouterr().err     # and on stderr too, as before


def test_a_board_that_did_not_answer_is_not_read_as_recorded(capsys, monkeypatch):
    """The quieter half of the same bug. `qb record-review` exits 0 whether or
    not the POST landed and distinguishes the two on its STREAMS, so an exit code
    alone cannot tell them apart — which is why this round was lost too."""
    _qb(monkeypatch, returncode=0, stdout="", stderr=QB_DOWN)
    note = panel_seats.record_run({"pr": 7})
    assert "NOT recorded on the board" in note
    assert "the board did not answer" in note
    # What `qb` said is quoted, so a wrong guess about a program in another repo
    # corrects itself in front of the reader.
    assert "Failed to connect" in note
    assert "NOT recorded" in capsys.readouterr().err


def test_a_qb_that_refuses_is_reported_with_its_exit_code(monkeypatch):
    """No board URL, no token, no such subcommand — all of them exit non-zero."""
    _qb(monkeypatch, returncode=1, stderr="qb: no token\n")
    note = panel_seats.record_run({"pr": 7})
    assert "exited 1" in note and "no token" in note


def test_recording_never_raises_and_still_says_so(monkeypatch):
    """Best-effort is the rule; silent is not. Both halves in one test."""
    _qb(monkeypatch, raises=subprocess.TimeoutExpired("qb", 20))
    note = panel_seats.record_run({"pr": 7})
    assert "TimeoutExpired" in note and "NOT recorded" in note


def test_the_note_names_the_recovery_because_the_payload_survives(monkeypatch):
    """#284 asks whether a lost round is recoverable. It is, with no new
    machinery: `--json-file` writes exactly the bytes piped to `qb`, so the note
    names the command rather than a queue that would have to be kept."""
    _qb(monkeypatch, present=False)
    assert "qb record-review <" in panel_seats.record_run({"pr": 7})


def test_a_shouting_board_cannot_push_a_page_of_markup_into_a_pr_comment(
        monkeypatch):
    """The quote goes into `config_notes`, which `--post` publishes. An HTML
    error page on stderr must not arrive there whole."""
    _qb(monkeypatch, returncode=1, stderr="<html>" + "x" * 5000)
    note = panel_seats.record_run({"pr": 7})
    assert len(note) < 600 and "xxx" in note


# ---------------------------------------------------------------- a whole round

META = {"title": "fix: a thing", "additions": 30, "deletions": 12,
        "baseRefName": "main", "headRefName": "h", "headRefOid": "abc",
        "files": [{"path": "a.py", "additions": 30, "deletions": 12}],
        "changedFiles": 1, "state": "OPEN", "isDraft": False}

CFG = {"github": "acme/board", "path": "/tmp/acme-board", "name": "board",
       "_rules_baseline": ".harness-rules.sample",
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
       "review_panel": {}}


def _round(monkeypatch, cfg=None, *, qb_present=False, stdout="", stderr=""):
    """A whole `run()` with a real `record_run` over a stubbed `qb`.

    Deliberately NOT monkeypatching `record_run` away, unlike every other
    end-to-end fixture in this suite — that is precisely how the defect stayed
    invisible for 67 rounds.
    """
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: {**CFG, **(cfg or {})})
    monkeypatch.setattr(panel_core, "sh", gh_stub(meta=META, diff="diff"))
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 5))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], None, panel.CoverageRuling()))
    _qb(monkeypatch, present=qb_present, stdout=stdout, stderr=stderr)
    # `_qb` pins `shutil.which` to answer for `qb` alone, and `seat_installed` is
    # a PATH read: pin it too, or which seats this round budgets depends on which
    # vendor CLIs the machine running the suite happens to carry (see conftest).
    monkeypatch.setattr(panel, "seat_installed", lambda name: True)
    monkeypatch.setattr(panel_preflight, "seat_installed", lambda name: True)


def _notes(payload) -> list[str]:
    return [n for n in payload["config_notes"] if "NOT recorded" in n]


def test_a_reviewed_round_the_board_never_saw_says_so_in_config_notes(
        monkeypatch, capsys):
    """`config_notes` is the channel that already exists for exactly this, and
    it is what `--post` puts in the public PR comment."""
    _round(monkeypatch, qb_present=False)
    assert panel.run("board", 284, post=False, json_out=True) == 0
    [note] = _notes(json.loads(capsys.readouterr().out))
    assert "no `qb` on this host" in note


def test_an_unreachable_board_reaches_config_notes_too(monkeypatch, capsys):
    """The path with a `qb` on PATH — the one an exit-code check would pass."""
    _round(monkeypatch, qb_present=True, stdout="", stderr=QB_DOWN)
    assert panel.run("board", 284, post=False, json_out=True) == 0
    [note] = _notes(json.loads(capsys.readouterr().out))
    assert "the board did not answer" in note


def test_a_round_the_board_took_carries_no_such_note(monkeypatch, capsys):
    _round(monkeypatch, qb_present=True, stdout=QB_OK)
    assert panel.run("board", 284, post=False, json_out=True) == 0
    assert _notes(json.loads(capsys.readouterr().out)) == []


def test_the_note_is_in_the_json_file_on_disk_not_only_in_the_report(
        monkeypatch, tmp_path, capsys):
    """The ordering assertion. `record_run` has to run BEFORE `write_payload`,
    or the file that is round r+1's baseline — and the artefact the recovery
    command is pointed at — is the one copy that does not say it was never
    recorded."""
    _round(monkeypatch, qb_present=False)
    out = tmp_path / "r1.json"
    assert panel.run("board", 284, post=False, json_out=True,
                     json_file=str(out)) == 0
    capsys.readouterr()
    [note] = _notes(json.loads(out.read_text()))
    assert "qb record-review <" in note


def test_the_report_a_human_reads_carries_the_note(monkeypatch, capsys):
    """Not `--json`: the printed report, which is what `--post` comments with."""
    _round(monkeypatch, qb_present=False)
    assert panel.run("board", 284, post=False) == 0
    assert "⚠️ config: this round was NOT recorded on the board" in capsys.readouterr().out


def test_a_refused_round_the_board_never_saw_says_so_on_its_notice(
        monkeypatch, tmp_path, capsys):
    """The refusal exit records on purpose (a refusal is an observation the board
    exists to accumulate) and prints its own notice, which `refusal_report`
    builds and which takes no notes list. So it needed its own wiring, and it is
    the exit most likely to be read by somebody asking why nothing happened."""
    big = "\n".join(f"+line {i}" for i in range(400))
    monkeypatch.setattr(panel, "load_repo_cfg",
                        lambda name: {**CFG, "review_panel": {"max_diff_chars": 50}})
    monkeypatch.setattr(panel_core, "sh", gh_stub(meta=META, diff=big))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    _qb(monkeypatch, present=False)
    monkeypatch.setattr(panel, "seat_installed", lambda name: True)
    monkeypatch.setattr(panel_preflight, "seat_installed", lambda name: True)
    out = tmp_path / "refused.json"
    assert panel.run("board", 284, post=False, json_file=str(out)) == 0
    printed = capsys.readouterr().out
    assert "REFUSED" in printed
    # On the NOTICE, which is what `--post` comments with — not only in the file.
    assert "⚠️ config: this round was NOT recorded on the board" in printed
    got = json.loads(out.read_text())
    assert got["reviewed"] is False
    [note] = _notes(got)
    assert "no `qb` on this host" in note


def test_record_false_records_nothing_and_claims_nothing(monkeypatch, capsys):
    """`--no-record` is a caller saying "do not tell the board", not a round that
    tried and failed. It must not acquire the warning."""
    _round(monkeypatch, qb_present=False)
    assert panel.run("board", 284, post=False, json_out=True, record=False) == 0
    assert _notes(json.loads(capsys.readouterr().out)) == []


def test_record_false_does_not_announce_an_escalation_either(monkeypatch, capsys):
    """An escalation post is a board write like any other (#274), so a run told
    to stay off the board stays off it — a preview that silently interrupted a
    person would be the same surprise as a round the board never heard about,
    in the other direction."""
    called: list[dict] = []
    monkeypatch.setattr(panel, "announce_escalations",
                        lambda payload, cfg: called.append(payload) or [])
    _round(monkeypatch, qb_present=True)
    assert panel.run("board", 284, post=False, json_out=True, record=False) == 0
    capsys.readouterr()
    assert called == [], "--no-record still wrote to the board"

    _round(monkeypatch, qb_present=True)
    assert panel.run("board", 284, post=False, json_out=True, record=True) == 0
    capsys.readouterr()
    assert len(called) == 1, "a recorded round no longer announces its escalations"
