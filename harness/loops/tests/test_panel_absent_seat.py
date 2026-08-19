"""A seat this box cannot run declares nothing about the round.

`coverage_veto` learned in v2.x that an absent CLI is a fact about the HOST, not
about the round: it is absent every round, so vetoing on it makes `confident`
permanently unreachable on exactly the unattended boxes where the signal has to
mean something. That exemption was applied to the veto **and to nothing else**.

`budgets` was still built from the CONFIGURED set, so a seat with no CLI on the
box acquired a diff budget, an argv clamp, a `config_notes` line saying it "gets
116,287 of 177,872 diff chars", and a `truncated: True` record — four statements
about a reviewer that was never going to read a byte. Measured on PR #217, on a
box without `agy`:

    antigravity:  ran=False  absent=True  truncated=True
    diff_truncated (run level): True
    truncated among seats that RAN: []      <- nothing

And it did not stop there. `load_baseline` read `any(m.get("truncated") …)` over
every member regardless of whether it ran, so that round was banked as truncated
and the NEXT round inherited it:

    round 1 had a truncated reviewer and this round reviewed only the increment
    since e1354dde — whatever that round was cut off from has now been read by no
    round of this cycle

False on that host, and a `confident` veto — so every multi-round cycle on such a
box was non-confident from round 2 onward, permanently. The exact failure the
absent-CLI exemption exists to prevent, arriving through the one consumer nobody
exempted.

These tests pin both halves, because they fail on different days: the writer
(`budgets`, so new payloads stop carrying the pairing) and the reader
(`load_baseline`, so payloads already written stop re-introducing it — baselines
outlive the release that wrote them and `--baseline` is fed them by design).

**The reader's exemption is `absent`, not `ran`, and both directions are pinned
here.** `ran` is `not skip`, false for every way of not running, so exempting on
it would also drop the truncation of a seat that was INSTALLED, read a real
prefix, and then crashed or timed out — a genuine tail nobody read, silently
un-banked, in the fail-open direction on a `confident` veto. `coverage_veto` in
the same file is the model: it exempts `absent` specifically and says in as many
words that every other way of not running still vetoes. Keying on `absent` also
keeps pre-`ran` payloads reading exactly as they always did, since they carry
neither field.

**Every test here pins the host.** `seat_installed` consults PATH, so a test that
leaves it alone asserts on which vendor CLIs the machine happens to carry — green
on a workstation, red on a CI runner that has none of them, and green for the
wrong reason on a box that has all four. That is the same trap the pre-flight
suite documents, and it is sharper here because this whole module is *about* the
predicate. It is also why this module takes no package-wide pin: `conftest`'s
`every_seat_installed` is requested by the modules that need a stated host,
never applied to everything, so the one module whose subject is absence cannot be
silently flipped to asserting presence.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402
import panel_rounds  # noqa: E402
import panel_seats  # noqa: E402
from conftest import gh_stub  # noqa: E402

#: A diff big enough to be cut by a small budget AND by the kernel's argv ceiling.
#:
#: Sized deliberately past :data:`panel.ARGV_PROMPT_MAX_BYTES` rather than "big
#: enough looking": the first version of this was ~14 KB, so the argv clamp never
#: fired, and the test asserting the clamp STILL works for an installed
#: `antigravity` passed vacuously — a fixture that would have reported the
#: regression it exists to catch as a pass.
BIG = "".join(
    f"diff --git a/f{i}.py b/f{i}.py\nindex 1..2 100644\n"
    f"--- a/f{i}.py\n+++ b/f{i}.py\n@@ -0,0 +1,100 @@\n"
    + "".join(f"+    value_{i}_{j} = compute({j}, retries=3, timeout=30)\n"
              for j in range(100))
    for i in range(40))

CFG = {"github": "acme/board", "path": "/tmp/acme-board", "name": "board",
       "review_panel": {}}


def test_the_fixture_really_is_over_the_kernels_argv_ceiling():
    """Pinned, because the clamp test below is meaningless otherwise and passes
    just as green when it is."""
    assert len(BIG.encode()) > panel.ARGV_PROMPT_MAX_BYTES


def _cfg(**seats):
    """Seats with per-seat budgets, and #138's pre-flight verdict switched off.

    The budgets here are truncation DEVICES — `_cfg(claude=200)` exists to make a
    seat read a prefix — and a budget that far under the diff is also what the
    pre-flight verdict refuses a round for, or replaces with a move manifest. Either
    would substitute the round this module is measuring, so both are disabled: the
    refusal by `refuse_over_cap_multiple: 0`, the manifest by `manifest_moves:
    false`. The verdict has its own suite; what matters here is absence.
    """
    return {**CFG,
            "review_panel": {**CFG.get("review_panel", {}),
                             "refuse_over_cap_multiple": 0,
                             "manifest_moves": False},
            "reviewers": {n: {"enabled": True, "model": "sonnet",
                              **({} if b is None else {"max_diff_chars": b})}
                          for n, b in seats.items()}}


def _host(monkeypatch, present):
    """Pin which seats this box carries, for every caller of the predicate.

    `panel.py`, `panel_seats.py` and `panel_rounds.py` each resolve
    `seat_installed` through their own module globals (all three star-import it),
    so patching one leaves the others reading the real PATH — and two consumers
    disagreeing about which seats exist is precisely the bug this module is about.

    `panel_core` is patched last and for a different reason: nothing exercised
    through `run()` resolves the name there, so this one is inert for the
    end-to-end tests. It exists for the predicate tests below, which call
    `panel_core.seat_installed` directly, and for a future caller that has not
    star-imported it.
    """
    def installed(name):
        return name in present
    monkeypatch.setattr(panel, "seat_installed", installed)
    monkeypatch.setattr(panel_seats, "seat_installed", installed)
    monkeypatch.setattr(panel_rounds, "seat_installed", installed)
    monkeypatch.setattr(panel_core, "seat_installed", installed)


def _round(monkeypatch, tmp_path, cfg, *, present, diff=BIG, baselines=(),
           judge_budget=None):
    seen = {"prompts": {}}
    if judge_budget is not None:
        cfg = {**cfg, "review_panel": {**cfg.get("review_panel", {}),
                                       "judge_max_diff_chars": judge_budget}}
    _host(monkeypatch, present)

    def fake_review(name, model, prompt, effort="", **kw):
        # `**kw` swallows whatever `review_llm` has grown since — `code_tree` and
        # `budget_usd` in v2.51, more later. What this module tests is which seats
        # get dispatched and what their record says, never the seat's own argument
        # list, so enumerating it would only break on somebody else's feature.
        seen["prompts"][name] = prompt
        # The real `run_seat` refuses an absent seat before it spends anything;
        # this double stands in for that, so the test exercises `run()`'s
        # bookkeeping rather than the CLI layer.
        if not panel.seat_installed(name):
            return panel.ReviewerRun([], f"{name}: {panel.CLI_ABSENT}", 1, None,
                                     absent=True)
        return panel.ReviewerRun([], None, 10, None)

    monkeypatch.setattr(panel, "load_repo_cfg", lambda n: cfg)
    monkeypatch.setattr(panel_core, "sh", gh_stub(diff=diff))
    monkeypatch.setattr(panel, "review_llm", fake_review)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], None, ""))
    out = tmp_path / "r.json"
    assert panel.run("e2e", 34, post=False, json_file=str(out), record=False,
                     baseline=list(baselines), max_rounds=2) == 0
    return json.loads(out.read_text()), seen


# ------------------------------------------------------------------ the predicate

def _never_run(*a, **kw):
    """A `run_cli` double for the paths that must not reach a CLI at all. Raising
    rather than returning: a seat that got this far has already been let past the
    absence check, and a quiet stub would let that regression read as a pass."""
    raise AssertionError("run_seat reached the CLI for a seat it should have refused")


def _record_run(seen):
    """A `run_cli` double that notes it was called and reports a failure, so the
    seat returns a skip of its own rather than spawning anything or being parsed."""
    def fake(*a, **kw):
        seen.append(a)
        return None, "stubbed by the test"
    return fake


def test_seat_installed_asks_about_the_COMMAND_not_the_seat_name(monkeypatch):
    """The reviewer is `antigravity`; the command is `agy`. Asking PATH for
    "antigravity" reports the one argv-bound seat absent on every box — the
    direction that silently switches its ceiling off everywhere."""
    asked = []
    monkeypatch.setattr(panel_core.shutil, "which",
                        lambda c: asked.append(c) or "/x/bin/agy")
    assert panel_core.seat_installed("antigravity") is True
    assert asked == ["agy"]
    assert panel_core.seat_installed("claude") is True
    assert asked[-1] == "claude"


def test_seat_installed_is_FALSE_and_a_bool_when_the_command_is_not_there(monkeypatch):
    """The other direction, and the type. `which` returns a PATH or None, and the
    `bool(...)` around it is load-bearing rather than decoration: every `is True` /
    `is False` assertion in this module — and `not m.get("absent")` in
    `load_baseline` — reads an identity, and a bare path string would satisfy the
    truthiness while failing the identity."""
    monkeypatch.setattr(panel_core.shutil, "which", lambda c: None)
    assert panel_core.seat_installed("antigravity") is False
    assert panel_core.seat_installed("claude") is False


def test_run_seat_and_budgets_ask_the_SAME_predicate(monkeypatch):
    """One question, one implementation, pinned by BEHAVIOUR. `run_seat` used its
    own inline `shutil.which(CLI_BIN.get(...))`; two copies are two chances to
    disagree, and the disagreement is silent — a seat skipped as absent while its
    budget record says it was handed 116,287 chars.

    This used to read `panel_seats.py` and grep it for `shutil.which(CLI_BIN`,
    which is a test of the SPELLING: an alias, a line break or a rename defeats it,
    a legitimate use elsewhere in the file false-fires it, and the `seat_installed(`
    half was satisfied by the comment sitting two lines above the real call. So it
    patches the shared predicate instead and asks `run_seat` what it does — the
    only thing that proves the two are one question.
    """
    # PATH says the binary IS there, the shared predicate says it is not. A
    # `run_seat` still carrying its own `shutil.which` would sail past the patch
    # and spawn the CLI; one asking the predicate refuses. Nothing may reach a
    # subprocess either way, so the runner is doubled into a hard failure.
    monkeypatch.setattr(panel_core.shutil, "which", lambda c: "/x/bin/" + c)
    monkeypatch.setattr(panel_seats, "seat_installed", lambda name: False)
    monkeypatch.setattr(panel_seats, "run_cli", _never_run)
    got = panel_seats.run_seat("claude", "sonnet", "prompt")
    assert got.absent is True
    assert got.skip.endswith(panel.CLI_ABSENT)

    # And the refusal really is the predicate's answer rather than a seat that
    # never runs: flip the same predicate and the seat gets as far as the CLI.
    reached = []
    monkeypatch.setattr(panel_seats, "seat_installed", lambda name: True)
    monkeypatch.setattr(panel_seats, "run_cli", _record_run(reached))
    got = panel_seats.run_seat("claude", "sonnet", "prompt")
    assert reached, "an installed seat was refused anyway"
    assert got.absent is False


# ------------------------------------------------------------------ the writer

def test_an_absent_seat_gets_no_budget(monkeypatch, tmp_path):
    """The root. Everything below is a consequence of this dict.

    A NULL rather than a missing key: the internal dict genuinely drops the seat,
    because everything that iterates it reads a null as "uncapped" and would
    compose the whole diff for a reviewer that never ran — but the PAYLOAD keeps
    every selected seat, so a board reading `diff_budgets[name]` for a configured
    seat does not start raising KeyError on the unattended hosts this is for."""
    got, _ = _round(monkeypatch, tmp_path, _cfg(claude=None, antigravity=None),
                    present={"claude"})
    assert got["diff_budgets"]["claude"] is None
    assert got["diff_budgets"]["antigravity"] is None
    assert set(got["diff_budgets"]) == {"claude", "antigravity", "judge"}


def test_an_absent_seat_is_not_handed_the_diff_it_will_never_read(monkeypatch,
                                                                 tmp_path):
    """It is still DISPATCHED — `run_seat` is the single authority on absence, and
    it is not only a PATH check: it answers a typo'd reasoning effort as the config
    error it is, before looking for the binary. A round that short-circuits dispatch
    skips that (225-R4-F03).

    What it is not handed is a prompt. No budget means `prompt_for(None)`, which
    means the WHOLE diff — rendered per absent seat, per round, for `run_seat` to
    throw away. The empty string also bounds the blast radius if the round's PATH
    read and `run_seat`'s ever disagree: a seat decided absent here can never carry
    an uncapped prompt into `agy`'s argv, which is the E2BIG that
    `ARGV_PROMPT_MAX_BYTES` exists to prevent."""
    got, seen = _round(monkeypatch, tmp_path, _cfg(claude=None, antigravity=None),
                       present={"claude"})
    assert seen["prompts"]["antigravity"] == ""
    assert len(seen["prompts"]["claude"]) > len(BIG)
    assert got["reviewers"]["antigravity"]["absent"] is True


def test_a_seat_that_VANISHED_since_the_budgets_were_built_records_no_budget(
        monkeypatch, tmp_path):
    """The race the record has to survive (225-R3-F05).

    `budgets` is decided before dispatch and `run_seat` decides absence after it, so
    a seat installed at the first moment and gone at the second gets a real budget
    written and then comes back `absent`. Read straight off `budgets`, the payload
    would carry a real `max_diff_chars` — and possibly `truncated: true` — beside
    `absent: true`: exactly the contradictory pairing #222 exists to remove, written
    by the fix meant to have removed it. The record is reconciled against what
    actually happened instead."""
    seen = {"prompts": {}}
    # Present when `budgets` is built...
    _host(monkeypatch, {"claude", "antigravity"})

    def vanished(name, model, prompt, effort="", **kw):
        seen["prompts"][name] = prompt
        # ...and gone by the time the seat runs.
        if name == "antigravity":
            return panel.ReviewerRun([], f"{name}: {panel.CLI_ABSENT}", 1, None,
                                     absent=True)
        return panel.ReviewerRun([], None, 10, None)

    monkeypatch.setattr(panel, "load_repo_cfg",
                        lambda n: _cfg(claude=None, antigravity=None))
    monkeypatch.setattr(panel_core, "sh", gh_stub(diff=BIG))
    monkeypatch.setattr(panel, "review_llm", vanished)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], None, ""))
    out = tmp_path / "r.json"
    assert panel.run("e2e", 34, post=False, json_file=str(out), record=False,
                     max_rounds=2) == 0
    meta = json.loads(out.read_text())["reviewers"]["antigravity"]
    assert meta["absent"] is True
    assert meta["max_diff_chars"] is None, "a budget beside `absent: true`"
    assert meta["truncated"] is False, "a truncation beside `absent: true`"

def test_the_JUDGE_gets_no_budget_on_a_box_that_cannot_run_it_either(monkeypatch,
                                                                    tmp_path):
    """The consumer sitting one line below the fix, and missed by the first pass.
    `adjudicate` runs the judge through the `claude` CLI and refuses when it is
    absent — asking this same predicate — so a judge budget there is the same
    statement about a reviewer that never read a byte, and "the judge saw 60,000
    of 177,872 chars" is the same footnote-that-is-a-lie.

    Not cosmetic either: a judge shortfall is appended to `veto` as well as to
    `config_notes`, and `confident` is `not veto` — so a box without `claude` and a
    configured `judge_max_diff_chars` bought every one of its rounds a standing
    veto about an adjudication that never happened. The same shape of failure as
    the reviewer half, one line down."""
    got, _ = _round(monkeypatch, tmp_path, _cfg(antigravity=None),
                    present={"antigravity"}, judge_budget=200)
    assert got["diff_budgets"]["judge"] is None
    assert not any("the judge" in n for n in got["config_notes"]), got["config_notes"]
    assert not any("the judge" in v for v in got["round_stop"]["veto"]), \
        got["round_stop"]["veto"]
    # The other direction: with `claude` on the box the judge budget is honoured
    # and the shortfall is reported, exactly as before.
    got, _ = _round(monkeypatch, tmp_path, _cfg(claude=None),
                    present={"claude"}, judge_budget=200)
    assert got["diff_budgets"]["judge"] == 200
    assert any("the judge" in n for n in got["config_notes"]), got["config_notes"]


def test_an_absent_seat_is_still_DISPATCHED_and_still_reports_itself_absent(
        monkeypatch, tmp_path):
    """Filtering the budget must not filter the seat. That record is what
    `coverage_veto` reads to exempt it, and what the report reads to say
    "configured, but never a seat here" — drop the dispatch and an absent seat
    becomes invisible instead of accounted for."""
    got, _ = _round(monkeypatch, tmp_path, _cfg(claude=None, antigravity=None),
                    present={"claude"})
    meta = got["reviewers"]["antigravity"]
    assert meta["ran"] is False
    assert meta["absent"] is True
    assert panel.CLI_ABSENT in meta["skip"]
    assert any("antigravity" in s for s in got["skipped"])


def test_an_absent_seat_records_a_NULL_budget_and_is_not_truncated(monkeypatch,
                                                                  tmp_path):
    """The pairing that caused all of this: `max_diff_chars: 116287` beside
    `truncated: True`, on a seat that never ran. The two fields must agree about
    whether the seat existed — null budget and not-truncated, or neither."""
    got, _ = _round(monkeypatch, tmp_path, _cfg(claude=None, antigravity=None),
                    present={"claude"})
    meta = got["reviewers"]["antigravity"]
    assert meta["max_diff_chars"] is None
    assert meta["truncated"] is False


def test_a_round_where_nothing_that_RAN_was_cut_is_not_truncated(monkeypatch,
                                                                tmp_path):
    """The headline symptom, end to end. `agy` absent, claude uncapped: no seat
    read a prefix of anything, so the round is not a truncated round."""
    got, _ = _round(monkeypatch, tmp_path, _cfg(claude=None, antigravity=None),
                    present={"claude"})
    assert got["reviewers_ran"] == ["claude (sonnet)"]
    assert got["diff_truncated"] is False
    assert [n for n, m in got["reviewers"].items() if m["ran"] and m["truncated"]] == []


def test_no_config_note_claims_an_absent_seat_GETS_a_share_of_the_diff(monkeypatch,
                                                                      tmp_path):
    """`antigravity gets 116,287 of 177,872 diff chars` on a box with no `agy`.
    It gets nothing; there is no `agy` to get it."""
    got, _ = _round(monkeypatch, tmp_path, _cfg(claude=None, antigravity=None),
                    present={"claude"})
    assert not any("antigravity" in n for n in got["config_notes"]), got["config_notes"]


def test_the_argv_clamp_still_fires_when_the_seat_IS_installed(monkeypatch,
                                                              tmp_path):
    """The other direction, and the one that must not regress: where `agy` really
    is on the box, its prompt really does travel in argv and the kernel really
    does cap it. Filtering absent seats must not disarm the clamp for present
    ones."""
    got, _ = _round(monkeypatch, tmp_path, _cfg(claude=None, antigravity=None),
                    present={"claude", "antigravity"})
    assert got["diff_budgets"]["antigravity"] is not None
    assert got["diff_budgets"]["antigravity"] <= panel.ARGV_PROMPT_MAX_BYTES
    assert any("antigravity" in n and "argv" in n for n in got["config_notes"])
    assert got["reviewers"]["antigravity"]["truncated"] is True


def test_a_seat_that_RAN_and_was_cut_is_still_recorded_truncated(monkeypatch,
                                                                tmp_path):
    """The signal this whole area exists to carry, unharmed: a real budget on a
    real seat that really read a prefix."""
    got, _ = _round(monkeypatch, tmp_path, _cfg(claude=200), present={"claude"})
    meta = got["reviewers"]["claude"]
    assert meta["ran"] is True and meta["truncated"] is True
    assert got["diff_truncated"] is True


def test_a_box_carrying_no_seat_at_all_still_produces_a_payload(monkeypatch,
                                                               tmp_path):
    """Every budget filtered away is the degenerate case, and it must not raise:
    no seat ran, so `coverage_veto` says exactly that, which is the existing and
    correct answer."""
    got, _ = _round(monkeypatch, tmp_path, _cfg(claude=None, antigravity=None),
                    present=set())
    assert got["diff_budgets"] == {"claude": None, "antigravity": None,
                                   "judge": None}
    assert got["reviewers_ran"] == []
    assert got["diff_truncated"] is False
    assert any("no reviewer ran" in v for v in got["round_stop"]["veto"])


def test_the_real_adjudicate_refuses_a_box_that_cannot_run_the_judge(monkeypatch):
    """The judge's own gate, exercised for real (225-R2-F03).

    Every end-to-end test here replaces `adjudicate` wholesale, so nothing reached
    the refusal inside it — and the budget half is only half the change. The two
    gates have to be ONE predicate: a judge skipped as absent while
    `diff_budgets.judge` records 60,000 chars is the same contradiction between two
    fields that #222 is about, one seat over.

    `run_cli` is doubled into a hard failure so a regression cannot quietly spawn a
    real `claude` instead of failing the assertion — and it is doubled on
    `panel_seats`, which is where `adjudicate` calls it from. Patching
    `panel_rounds.run_cli` looks right, does nothing, and lets the test spawn a real
    reviewer: that is how the first version of this test was written, and it took
    twelve seconds and a live CLI to notice.
    """
    monkeypatch.setattr(panel_rounds, "seat_installed", lambda name: False)
    def _judge_never_runs(*a, **kw):
        # Its own double rather than `_never_run`, whose message names `run_seat`
        # (225-R3-F08). The judge does not go through `run_seat`, so on failure that
        # message would send a reader to the wrong function.
        raise AssertionError("adjudicate reached the CLI on a box with no claude")

    monkeypatch.setattr(panel_seats, "run_cli", _judge_never_runs)
    findings, skip, note = panel_rounds.adjudicate(
        [[panel.Finding("claude", "P2", "a.py", 1, "t", "d")]],
        "diff text", "sonnet", 34)
    assert skip.startswith("judge: ") and panel.CLI_ABSENT in skip
    # Nothing is suppressed by the refusal: every finding survives, unruled.
    assert len(findings) == 1
    assert note == ""


def test_the_real_adjudicate_proceeds_when_the_judge_IS_there(monkeypatch):
    """The other direction, so the guard above cannot be satisfied by a predicate
    that is simply always False."""
    reached = {}
    monkeypatch.setattr(panel_rounds, "seat_installed", lambda name: True)

    # A dedicated sentinel, not a bare AssertionError (225-R3-F11): an
    # `AssertionError` raised here is indistinguishable from a genuine assertion
    # failing inside `adjudicate`, so a real regression could be caught by the
    # `except` and read as this test passing. `_Reached` can only come from below.
    class _Reached(Exception):
        pass

    def fake_run_cli(*a, **kw):
        reached["yes"] = True
        raise _Reached

    monkeypatch.setattr(panel_seats, "run_cli", fake_run_cli)
    with pytest.raises(_Reached):
        panel_rounds.adjudicate(
            [[panel.Finding("claude", "P2", "a.py", 1, "t", "d")]],
            "diff text", "sonnet", 34)
    assert reached.get("yes"), "the judge was refused on a box that has claude"


# ------------------------------------------------------------------ the reader

def _payload(tmp_path, name, reviewers, **kw):
    body = {"repo": "board", "github": "acme/board", "pr": 34, "round": 1,
            "cycle": "cyc", "head_sha": "a" * 40, "reviewers_ran": ["claude"],
            "scope": "pr", "to_fix": [], "dismissed": [], "sonar_findings": [],
            "reviewers": reviewers, **kw}
    p = tmp_path / name
    p.write_text(json.dumps(body))
    return str(p)


def _baseline(paths, round_no=2):
    """`load_baseline` as the round after the baselines would call it.

    `round_no` must be LATER than every payload passed, or the payload is refused
    as "not an earlier round" and silently contributes nothing — which reads
    exactly like a payload that contributed and had nothing to say. A two-round
    baseline therefore needs `round_no=3`.
    """
    return panel_rounds.load_baseline(list(paths), {"repo": "board",
                                                    "github": "acme/board",
                                                    "pr": 34, "round": round_no})


def test_an_absent_seats_truncation_does_not_bank_a_truncated_round(tmp_path):
    """The reader half, and the reason it is needed even after the writer is
    fixed: baselines outlive the release that wrote them. Every payload already on
    disk carries the old pairing, and `--baseline` is fed them by design — so a
    fix that only cleans the writer leaves every cycle in flight banking phantom
    gaps until it ends."""
    old = _payload(tmp_path, "r1.json",
                   {"claude": {"ran": True, "truncated": False},
                    "antigravity": {"ran": False, "absent": True, "truncated": True}})
    assert _baseline([old]).truncated_rounds == set()


def test_a_seat_that_RAN_and_was_truncated_still_banks_one(tmp_path):
    """The signal, unharmed. The exemption must not become "never truncated"."""
    real = _payload(tmp_path, "r1.json",
                    {"claude": {"ran": True, "truncated": True}})
    assert _baseline([real]).truncated_rounds == {1}


def test_a_seat_that_was_CUT_and_then_CRASHED_still_banks_a_truncated_round(tmp_path):
    """The over-correction this exemption must not make, and the reason it is
    keyed on `absent` rather than on `ran`.

    `ran` is `not skip` — false for EVERY way of not running. An INSTALLED seat
    with a small `max_diff_chars` is handed a genuine prefix and then times out,
    exits non-zero, or is refused for a bad effort pin: it is written
    `ran: False, absent: False, truncated: True`, because `truncated` is keyed off
    `truncated_for`, which is built from `budgets`, and an installed seat still has
    a budget. A tail of that round really was read by nobody. Dropping it is a
    fail-open on a `confident` veto — the phantom-inverse of the bug this fixes,
    and the direction this repo consistently refuses.

    `coverage_veto` is the model: it exempts `absent` specifically and states that
    every other way of not running still vetoes. So does this."""
    crashed = _payload(tmp_path, "r1.json",
                       {"claude": {"ran": False, "absent": False, "truncated": True,
                                   "skip": "claude (sonnet): timed out after 900s",
                                   "max_diff_chars": 200}})
    assert _baseline([crashed]).truncated_rounds == {1}


@pytest.mark.parametrize("member", [
    {"ran": True, "truncated": None},
    {"ran": False, "absent": True},
    {},
])
def test_a_payload_that_says_nothing_about_truncation_banks_nothing(tmp_path, member):
    """No recorded truncation is no gap to bank. `load_baseline`'s standing rule is
    that a payload it cannot read costs a `problems` entry, never a wrong
    inference, and inferring a cut from a member that never claimed one is the
    wrong inference in the expensive direction — it is a `confident` veto."""
    p = _payload(tmp_path, "r1.json", {"claude": member})
    assert _baseline([p]).truncated_rounds == set()


@pytest.mark.parametrize("member", [
    {"truncated": True},                       # pre-v2.15: no `ran` recorded
    {"ran": None, "truncated": True},          # recorded, unreadably
])
def test_an_OLD_payload_that_recorded_a_truncation_still_banks_it(tmp_path, member):
    """A payload written before the panel recorded `ran` per member says nothing
    about whether the seat ran — but it does say the seat was CUT, and that is the
    fact this reads. `not absent` is True for it, so the truncation banks, which is
    exactly what the pre-#222 reader did with it.

    The first version of this fix required `ran` and therefore silently dropped
    these, with no `problems` entry and no trace: an old baseline recording a real
    coverage gap would have made a later round look better covered than it was, and
    the operator would have had no way to see it happen. Keying on the absence the
    payload can actually be asked about costs nothing here and loses nothing."""
    p = _payload(tmp_path, "r1.json", {"claude": member})
    assert _baseline([p]).truncated_rounds == {1}


# ------------------------------- `truncated_any`: which rounds may CLOSE a gap

def test_an_ABSENT_seats_truncation_does_not_stop_a_round_closing_earlier_gaps(tmp_path):
    """`truncated_any` drives `reread`, which erases every earlier round's recorded
    gap. Exempting `absent` there is the other half of #222 (225-R2-F01): leaving it
    in let one legacy payload's phantom record block `reread` forever, keeping every
    earlier gap open and the cycle permanently non-confident — the exact veto this
    release removes, surviving in the one path the first fix had not reached."""
    cut = _payload(tmp_path, "r1.json", {"claude": {"ran": True, "truncated": True}},
                   round=1)
    phantom = _payload(tmp_path, "r2.json",
                       {"claude": {"ran": True, "truncated": False},
                        "antigravity": {"ran": False, "absent": True,
                                        "truncated": True}},
                       round=2)
    b = _baseline([cut, phantom], round_no=3)
    assert b.truncated_rounds == set(), "round 2 read everything and closed round 1's gap"


def test_an_ARGV_CAPPED_truncation_still_stops_a_round_closing_earlier_gaps(tmp_path):
    """The asymmetry, pinned — without this, adding `not argv_capped` to
    `truncated_any` to "match" `cut` would pass the suite unnoticed.

    A kernel-capped seat RAN and saw a prefix, so the round genuinely did not read
    its target whole and cannot be the one that clears an older gap. An absent seat
    read nothing and is no evidence either way. `cut` exempts both because neither
    gap will ever close on this box; `truncated_any` exempts only the one that is
    not evidence of a short read."""
    cut = _payload(tmp_path, "r1.json", {"claude": {"ran": True, "truncated": True}},
                   round=1)
    capped = _payload(tmp_path, "r2.json",
                      {"claude": {"ran": True, "truncated": False},
                       "antigravity": {"ran": True, "argv_capped": True,
                                       "truncated": True}},
                      round=2)
    b = _baseline([cut, capped], round_no=3)
    assert b.truncated_rounds == {1}, "a capped round must not close round 1's gap"


def test_a_round_where_NOBODY_ran_cannot_close_anyone_elses_gap(tmp_path):
    """The positive-evidence guard (225-R3-F01). Exempting `absent` from
    `truncated_any` makes an all-absent round come out False — identical to a round
    that read everything — so without a companion check a round in which nothing ran
    could erase a real gap banked by a round that did. The `reviewers_ran == []`
    branch catches this for every payload this release writes; a hand-edited or
    truncated one need not carry that key, and `reread` is the most destructive
    thing in this function."""
    cut = _payload(tmp_path, "r1.json", {"claude": {"ran": True, "truncated": True}},
                   round=1)
    nobody = tmp_path / "r2.json"
    body = json.loads(Path(_payload(tmp_path, "r2.json",
                                    {"claude": {"ran": False, "absent": True},
                                     "antigravity": {"ran": False, "absent": True}},
                                    round=2)).read_text())
    body.pop("reviewers_ran")          # the key a hand-edited payload may lack
    nobody.write_text(json.dumps(body))
    b = _baseline([cut, str(nobody)], round_no=3)
    assert b.truncated_rounds == {1}, "a round nobody ran closed a real gap"


def test_a_round_where_every_seat_CRASHED_cannot_close_anyone_elses_gap(tmp_path):
    """The case that tells `ran` from `not absent` (225-R4-F13).

    A seat that was present and then crashed, timed out, or returned nothing is
    `absent: False, ran: False` — it was THERE, and it read nothing. The weaker
    `not absent` test counts it as positive evidence and lets a round in which every
    seat failed erase a gap banked by a round whose seats worked. `ran` is the field
    that means somebody actually got through.

    The all-absent case passes under either spelling, which is why it cannot pin
    this and this test exists."""
    cut = _payload(tmp_path, "r1.json", {"claude": {"ran": True, "truncated": True}},
                   round=1)
    crashed = tmp_path / "r2.json"
    body = json.loads(Path(_payload(
        tmp_path, "r2.json",
        {"claude": {"ran": False, "absent": False, "skip": "claude: timed out"},
         "codex": {"ran": False, "absent": False, "skip": "codex: exited 1"}},
        round=2)).read_text())
    body.pop("reviewers_ran")   # the key a hand-edited payload may lack
    crashed.write_text(json.dumps(body))
    b = _baseline([cut, str(crashed)], round_no=3)
    assert b.truncated_rounds == {1}, "a round where every seat crashed closed a real gap"


def test_a_phantom_truncation_no_longer_vetoes_a_later_SCOPED_round(monkeypatch,
                                                                   tmp_path):
    """End to end, and this is the sentence that was false: a round 2 inheriting
    "whatever that round was cut off from has now been read by no round of this
    cycle" from a seat that never ran. It is a `confident` veto, so on a box
    configuring a seat it cannot carry, no cycle could report convergence."""
    old = _payload(tmp_path, "r1.json",
                   {"claude": {"ran": True, "truncated": False},
                    "antigravity": {"ran": False, "absent": True, "truncated": True}})
    got, _ = _round(monkeypatch, tmp_path, _cfg(claude=None, antigravity=None),
                    present={"claude"}, baselines=[old])
    assert not any("truncated reviewer" in v for v in got["round_stop"]["veto"]), \
        got["round_stop"]["veto"]
