"""Where the round's wall clock went, and whether the round can say so (#192).

`/panel-review-pr` is slow. The working hypothesis was the serial fixer, and the
issue's own point is that the hypothesis could not be checked: `duration_ms` said
how long each seat thought, and nothing said how long the round spent before
dispatching them, how long the judge took, or how long the fix phase between two
rounds ran. Roughly half a cycle's wall clock was unattributable.

**Timing code is trivially easy to test vacuously**, which is the reason this
module reads the way it does. A test asserting that `round_ms` is an int and
`>= 0` passes against a stopwatch that measures the wrong span, against one that
reports the same seat as slowest every time, and against one that reports a fix
phase of zero for a round that pushed nothing. So nothing here asserts that a
number exists. Every test asserts an **attribution**: that the seat which was
made slow is the seat reported as slow, that a judge made slow moves the judge's
phase and not the seat phase, that the phases add up to the whole round, and that
each of the three cases where the commit-time derivation is wrong is reported as
`null` with a reason rather than as a number.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402
import panel_preflight  # noqa: E402  — the verdict resolves its own host predicate
import panel_rounds  # noqa: E402  — `load_baseline` reads the finish back
import panel_timing  # noqa: E402
from conftest import DEFAULT_HEAD, gh_stub  # noqa: E402

#: A three-seat panel. Three and not two, because `gated_ms` is the slowest
#: seat's finish minus the SECOND slowest's, and with two seats that is
#: indistinguishable from "the slowest minus the fastest" — the wrong formula
#: passing every test.
CFG = {"github": "acme/board", "path": "",
       "_rules_baseline": ".harness-rules.sample",
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"},
                     "codex": {"enabled": True, "model": "gpt-5"},
                     "pi": {"enabled": True, "model": "pi-1"}},
       "review_panel": {}}

#: seat -> seconds it "thinks" for. Deliberately spread far enough apart that a
#: reported ordering cannot be an artefact of scheduling jitter on a loaded box.
SLEEPS = {"claude": 0.02, "codex": 0.40, "pi": 0.10}


def _stub(monkeypatch, *, sleeps=None, judge_sleep=0.0, cfg=None, head=DEFAULT_HEAD,
          repo_path=""):
    """Every process a round would spawn, replaced — with a seat's wall clock as
    the thing under the test's control.

    The seats sleep rather than returning instantly, which is the whole point:
    a panel whose seats all take zero time cannot distinguish a stopwatch that
    attributes correctly from one that does not."""
    resolved = {**(cfg or CFG), "path": repo_path}
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: resolved)
    monkeypatch.setattr(panel_core, "sh", gh_stub(meta={"headRefOid": head}, head=head))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    naps = SLEEPS if sleeps is None else sleeps

    def review(name, model, prompt, *a, **k):
        time.sleep(naps.get(name, 0.0))
        return panel.ReviewerRun([], None, int(naps.get(name, 0.0) * 1000), [])

    monkeypatch.setattr(panel, "review_llm", review)

    def judge(clusters, *a, **k):
        time.sleep(judge_sleep)
        return ([], None, "")

    monkeypatch.setattr(panel, "adjudicate", judge)


def _run(tmp_path, *, round_no=1, baseline=(), name="out"):
    out = tmp_path / f"{name}.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline), max_rounds=2,
                     scope="pr") == 0
    return json.loads(out.read_text())


def _baseline(tmp_path, *, finished_at=None, head_sha="b" * 40, round_no=1,
              name="r1.json"):
    """A previous round's payload, as `--baseline` receives it."""
    payload = {"github": "acme/board", "pr": 34, "round": round_no, "cycle": "c1",
               "head_sha": head_sha, "reviewed": True, "scope": "pr",
               "reviewers": {"claude": {"ran": True}},
               "to_fix": [], "dismissed": [], "sonar_findings": []}
    if finished_at is not None:
        payload["timing"] = {"finished_at": finished_at}
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return str(path)


# ------------------------------------------------------- the seat that held the round

def test_the_slow_seat_is_the_one_reported_as_slow(monkeypatch, tmp_path):
    """The attribution, not the existence of a number.

    `codex` is made twenty times slower than `claude` and four times slower than
    `pi`, so there is exactly one right answer to "what was this round waiting
    for" and the payload has to give it. Before this change the payload had no
    answer at all: the collection loop read the futures in submission order, so
    by the time the slow seat's result arrived every other seat had been
    collected and nothing recorded that they had been finished the whole time."""
    _stub(monkeypatch)
    payload = _run(tmp_path)
    t = payload["timing"]
    assert t["slowest_seat"] == "codex"
    finishes = t["seat_finish_ms"]
    assert set(finishes) == {"claude", "codex", "pi"}
    # The ORDER the seats finished in, which is what the report claims and what a
    # per-seat timeout would be set from.
    assert finishes["claude"] < finishes["pi"] < finishes["codex"]


def test_the_round_says_how_long_it_waited_on_that_one_seat(monkeypatch, tmp_path):
    """`gated_ms` is the span in which every seat but one had finished and the
    round was still held — the number #192's "parallel but gated on its slowest
    seat" claim is about, and the one thing nothing measured.

    It is the slowest finish minus the SECOND slowest, so with these sleeps it is
    codex (400ms) minus pi (100ms). A generous window either side, because this
    is a real thread pool on a real box; what is asserted tightly is that it is
    neither codex's whole duration (which would be the wrong formula) nor the gap
    to the FASTEST seat (the other wrong formula)."""
    _stub(monkeypatch)
    t = _run(tmp_path)["timing"]
    gated, finishes = t["gated_ms"], t["seat_finish_ms"]
    assert gated == finishes["codex"] - finishes["pi"]
    # Not the slowest seat's whole duration: two seats were still running for the
    # first stretch of it, and that stretch is not attributable to either.
    assert gated < finishes["codex"]
    # Not the gap to the fastest seat either — `pi` finished after `claude`, so
    # the two formulas give visibly different numbers here.
    assert gated < finishes["codex"] - finishes["claude"]
    assert 0.0 < t["gated_pct"] <= 100.0


def test_a_one_seat_panel_is_not_reported_as_maximally_gated():
    """The guard that stops a single-vendor round reading as the worst case.

    One seat running alone is not a seat holding up the others; there are none.
    Reporting its whole duration as `gated_ms` would send a reader to parallelise
    a panel of one — and it is exactly what the natural spelling ("the slowest
    seat's time is the wait") does."""
    assert panel_timing.seat_attribution({"codex": 600_000}, 700_000)["gated_ms"] == 0
    assert panel_timing.seat_attribution({}, 1000)["slowest_seat"] is None


def test_seat_idle_time_counts_every_finished_seat_not_just_the_fastest():
    """The complementary number: seat-time spent finished with findings
    undelivered. Summed over every seat but the last, so a panel where three
    seats wait on one reports three times the waste of a panel where one does."""
    one = panel_timing.seat_attribution({"a": 100, "b": 1000}, 1000)
    three = panel_timing.seat_attribution({"a": 100, "b": 100, "c": 100, "d": 1000}, 1000)
    assert one["seat_idle_ms"] == 900
    assert three["seat_idle_ms"] == 2700


# --------------------------------------------------------------- the phases partition

def test_the_phases_account_for_the_whole_round(monkeypatch, tmp_path):
    """"The judge took four minutes" has to be distinguishable from "four minutes
    went somewhere near the judge". Each mark closes at the previous one, so the
    four phases sum to the round exactly — anything else is a remainder nobody
    can name, which is the state #192 is about."""
    _stub(monkeypatch, judge_sleep=0.05)
    t = _run(tmp_path)["timing"]
    assert set(t["phases"]) == {"setup", "seats", "judge", "wrapup"}
    assert sum(t["phases"].values()) == t["round_ms"]
    assert t["measured_to"] == "payload"


def test_a_slow_judge_moves_the_judge_phase_and_not_the_seat_phase(monkeypatch,
                                                                   tmp_path):
    """Attribution across the phase boundary. The judge runs after the executor
    has joined, so a stopwatch that closed the seat phase in the wrong place
    would charge the judge's time to the seats — and the seats are what everyone
    already suspected, so the error would confirm the hypothesis it was meant to
    test."""
    _stub(monkeypatch, sleeps={"claude": 0.0, "codex": 0.0, "pi": 0.0},
          judge_sleep=0.30)
    t = _run(tmp_path, name="slowjudge")["timing"]
    assert t["phases"]["judge"] >= 280
    # The seats did nothing at all, so their phase must be a rounding error next
    # to the judge's — not the judge's own time wearing the seats' label.
    assert t["phases"]["seats"] < t["phases"]["judge"]


def test_a_slow_seat_moves_the_seat_phase_and_not_the_judge_phase(monkeypatch,
                                                                  tmp_path):
    """The same boundary from the other side, because one test alone passes
    against a stopwatch that has the two phases swapped."""
    _stub(monkeypatch, judge_sleep=0.0)
    t = _run(tmp_path, name="slowseat")["timing"]
    assert t["phases"]["seats"] >= 380
    assert t["phases"]["judge"] < t["phases"]["seats"]


# ----------------------------------------------------------------- the fix phase

def test_the_fix_phase_is_measured_from_the_previous_rounds_recorded_finish(
        monkeypatch, tmp_path):
    """The half of a cycle's wall clock that had no number anywhere.

    Round 1 finished 90 seconds ago; round 2 starts now; the fix phase between
    them is 90 seconds, and the round says so and says where the earlier end came
    from."""
    _stub(monkeypatch)
    base = _baseline(tmp_path, finished_at=time.time() - 90)
    fix = _run(tmp_path, round_no=2, baseline=[base], name="r2")["timing"]["fix"]
    assert fix["source"] == "payload"
    assert fix["from_round"] == 1
    assert 88_000 <= fix["ms"] <= 100_000


def test_round_one_reports_no_fix_phase_rather_than_a_zero(monkeypatch, tmp_path):
    """No fix phase ran into round 1. That is a different statement from one that
    could not be measured, so there is no note and nothing to explain — and a `0`
    here would be a claim that the fixer was instantaneous."""
    _stub(monkeypatch)
    fix = _run(tmp_path)["timing"]["fix"]
    assert fix == {"ms": None, "source": None, "from_round": None, "note": None}


def test_the_recorded_finish_survives_the_round_trip_through_load_baseline(
        monkeypatch, tmp_path):
    """The chain end to end: what round 1 WRITES is what round 2's baseline reader
    hands to the fix-phase measurement. A payload field and a reader that disagree
    about its name or its type leave every fix phase silently derived instead."""
    _stub(monkeypatch)
    out = tmp_path / "chain1.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=1, max_rounds=2, scope="pr") == 0
    written = json.loads(out.read_text())["timing"]["finished_at"]
    read = panel_rounds.load_baseline([str(out)],
                                      {"github": "acme/board", "pr": 34, "round": 2})
    assert read.finished_at == pytest.approx(written)
    assert read.finished_round == 1


def test_a_baseline_whose_finish_is_not_an_instant_is_reported_not_believed(tmp_path):
    """`True` is an `int` subclass, so an unguarded read dates the previous round
    to 1970 and reports a fifty-year fix phase. A number that looks measured and
    is not is the exact failure this issue exists to remove, so the value is
    refused and the refusal is a `problems` entry."""
    # `float("inf")` is in the list because Python's `json` parses a bare
    # `Infinity`: it passes an unguarded `> 0`, dates the previous round to the end
    # of time, and turns every later fix phase into a negative infinity.
    for bad in (True, "yesterday", -5, 0, float("inf"), float("nan")):
        path = tmp_path / f"bad-{str(bad).replace('.', '_')}.json"
        # `allow_nan` is left on deliberately: `Infinity` is what a real payload
        # would carry, since the panel's own writer would emit it the same way.
        path.write_text(json.dumps({"github": "acme/board", "pr": 34, "round": 1,
                                    "timing": {"finished_at": bad}}))
        got = panel_rounds.load_baseline([str(path)],
                                         {"github": "acme/board", "pr": 34, "round": 2})
        assert got.finished_at is None, bad
        assert any("not a wall-clock instant" in p for p in got.problems), bad


def test_the_latest_round_that_recorded_a_finish_wins(tmp_path):
    """Same rule as `head_sha` beside it, and it has two halves that a test of
    either one alone does not pin.

    When both rounds name a finish the LATEST must win, or round 3 measures its
    fix phase from before round 2 ran and reports a span containing a whole round
    as a fix phase. When the newer round names none — a payload written by an
    older panel — the earlier answer must STAND rather than being cleared, which
    is the same failure arriving from the other direction."""
    r1 = _baseline(tmp_path, finished_at=1_700_000_000.0, round_no=1, name="a.json")
    r2 = _baseline(tmp_path, finished_at=1_700_000_600.0, round_no=2, name="b.json")
    both = panel_rounds.load_baseline([r1, r2],
                                      {"github": "acme/board", "pr": 34, "round": 3})
    assert (both.finished_at, both.finished_round) == (1_700_000_600.0, 2)
    silent = _baseline(tmp_path, round_no=2, name="c.json")
    kept = panel_rounds.load_baseline([r1, silent],
                                      {"github": "acme/board", "pr": 34, "round": 3})
    assert (kept.finished_at, kept.finished_round) == (1_700_000_000.0, 1)


def test_clock_skew_is_refused_rather_than_reported_as_a_duration():
    """A previous round that finished after this one started is not a fix phase of
    any length."""
    now = time.time()
    fix = panel_timing.fix_phase(now, prior_finished_at=now + 600, prior_round=1)
    assert fix["ms"] is None and fix["source"] is None
    assert "not from one timeline" in fix["note"]


def test_clock_skew_is_not_quietly_answered_by_the_other_source(repo):
    """…and it does not fall through to the commit derivation either.

    Both ends of the recorded pair say round 1 came AFTER round 2; the commits say
    the opposite. Publishing whichever source still produces a number presents a
    fix phase whose own evidence contradicts it, with nothing in the payload
    saying so — and the caller cannot tell it from a measured one."""
    path, first, second = repo
    now = time.time()
    fix = panel_timing.fix_phase(now, prior_finished_at=now + 600, prior_round=1,
                                 prior_head_sha=first, head_sha=second,
                                 repo_path=path)
    assert fix["ms"] is None and fix["source"] is None
    assert "not from one timeline" in fix["note"]


# ------------------------------------- the commit-time derivation, and where it breaks

@pytest.fixture
def repo(tmp_path):
    """A real two-commit checkout with commit times 300 seconds apart."""
    root = tmp_path / "clone"
    root.mkdir()

    def git(*args, when=None):
        # The real environment, with identity and dates pinned. A hand-built env
        # loses PATH, and `git` is then not on it — which fails as a fixture error
        # rather than as the assertion the test is about.
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
               "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        if when:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
        return subprocess.run(["git", "-C", str(root), *args], env=env,
                              capture_output=True, text=True, check=True).stdout.strip()

    git("init", "-q", "-b", "main")
    (root / "a.py").write_text("one\n")
    git("add", "-A")
    git("commit", "-qm", "one", when="2026-01-01T00:00:00+00:00")
    first = git("rev-parse", "HEAD")
    (root / "a.py").write_text("two\n")
    git("add", "-A")
    git("commit", "-qm", "two", when="2026-01-01T00:05:00+00:00")
    return str(root), first, git("rev-parse", "HEAD")


def test_the_fix_phase_is_derived_from_commit_times_when_no_finish_was_recorded(repo):
    """#192's own proposal, for the cycles already on the board: a payload written
    before `timing` existed carries a `head_sha` and nothing else, and the two
    rounds' commit times bound the fix phase between them."""
    path, first, second = repo
    fix = panel_timing.fix_phase(time.time(), prior_round=1, prior_head_sha=first,
                                 head_sha=second, repo_path=path)
    assert fix["source"] == "commits"
    assert fix["ms"] == 300_000
    assert "LOWER bound" in fix["note"]


def test_a_round_that_pushed_nothing_is_not_a_fix_phase_of_zero(repo):
    """The first of the two places the derivation breaks exactly where it would
    matter most. Both rounds read the same head, so the commit-time span is zero —
    and a round that pushed nothing is not a round that fixed nothing. Null, with
    the reason."""
    path, _first, second = repo
    fix = panel_timing.fix_phase(time.time(), prior_round=1, prior_head_sha=second,
                                 head_sha=second, repo_path=path)
    assert fix["ms"] is None and fix["source"] is None
    assert "pushed nothing" in fix["note"]


def test_a_commit_the_checkout_cannot_reach_is_reported_not_guessed(repo):
    """The second: a rebase between rounds leaves the earlier round's head
    unreachable. The sha is well-formed, so nothing upstream refuses it — only
    reading it does."""
    path, _first, second = repo
    gone = "dead" + "b" * 36
    fix = panel_timing.fix_phase(time.time(), prior_round=1, prior_head_sha=gone,
                                 head_sha=second, repo_path=path)
    assert fix["ms"] is None and fix["source"] is None
    assert gone[:8] in fix["note"] and "rebase" in fix["note"]


def test_a_branch_that_moved_backwards_is_not_a_negative_fix_phase(repo):
    path, first, second = repo
    fix = panel_timing.fix_phase(time.time(), prior_round=1, prior_head_sha=second,
                                 head_sha=first, repo_path=path)
    assert fix["ms"] is None and "moved backwards" in fix["note"]


def test_a_sha_shaped_like_a_flag_never_reaches_git(repo, monkeypatch):
    """The value is interpolated into a subprocess argv. `load_baseline` already
    refuses a non-commit, and this is the second check at the point of use —
    `--upload-pack=...` in a hand-edited baseline must not become an argument.

    Asserted on whether `git` is INVOKED, not on the return value: git rejects an
    unknown flag by itself, so a test that only checked for `None` would pass with
    the guard deleted and the flag handed straight to the subprocess."""
    path, _first, _second = repo
    calls = []
    monkeypatch.setattr(panel_timing.subprocess, "run",
                        lambda *a, **k: calls.append(a) or pytest.fail("git ran"))
    assert panel_timing._commit_time(path, "--upload-pack=touch /tmp/x") is None
    assert panel_timing._commit_time(path, "not-a-sha") is None
    assert panel_timing._commit_time(path, "") is None
    # And with no checkout to read, there is nothing to ask either.
    assert panel_timing._commit_time("", "a" * 40) is None
    assert calls == []


def test_the_recorded_finish_is_preferred_over_the_derivation(repo):
    """Both sources available: the recorded one wins, because the derivation is a
    lower bound that starts at the first fix commit rather than when the round
    ended."""
    path, first, second = repo
    now = time.time()
    fix = panel_timing.fix_phase(now, prior_finished_at=now - 30, prior_round=1,
                                 prior_head_sha=first, head_sha=second,
                                 repo_path=path)
    assert fix["source"] == "payload" and 29_000 <= fix["ms"] <= 40_000


# --------------------------------------------------------- what the reader actually sees

def test_the_pr_comment_names_the_seat_that_held_the_round(monkeypatch, tmp_path,
                                                           capsys):
    """The operator deciding whether to spend another round is the reader this
    measurement is for, and the payload is not where they are looking. So the
    slow seat has to be named in the report, not merely recorded."""
    _stub(monkeypatch)
    _run(tmp_path, name="report")
    out = capsys.readouterr().out
    assert "**Wall clock:**" in out
    assert "Slowest seat codex" in out
    assert "holding the round alone for" in out


def test_the_comment_says_the_fix_phase_was_not_measured_rather_than_omitting_it(
        monkeypatch, tmp_path, capsys):
    """A round 2 whose fix phase cannot be derived must say so on the PR. Silence
    there is indistinguishable from a fast fix phase, which is the reading this
    whole change exists to stop."""
    _stub(monkeypatch)
    base = _baseline(tmp_path, head_sha=DEFAULT_HEAD)  # head never moved
    _run(tmp_path, round_no=2, baseline=[base], name="r2msg")
    out = capsys.readouterr().out
    assert "Fix phase before this round: not measured" in out
    assert "pushed nothing" in out


def test_the_round_says_which_seat_it_is_still_waiting_for_while_it_waits(
        monkeypatch, tmp_path, capsys):
    """Live attribution, which is the half a payload cannot give: a round in
    progress printed nothing between dispatch and the report, so an operator
    watching a twenty-minute round could not tell a slow seat from a hung one."""
    _stub(monkeypatch)
    _run(tmp_path, name="live")
    out = capsys.readouterr().out
    assert "· claude finished in" in out
    # Named while the round is still on it — the fast seats report the slow one as
    # outstanding, and the last seat in reports that nothing is left.
    assert "still waiting on codex" in out
    assert "all seats in" in out


def test_watching_the_seats_does_not_reorder_what_the_round_records(monkeypatch,
                                                                    tmp_path):
    """The collection loop still reads the futures in SUBMISSION order, which is
    what keeps finding ids and the reported reviewer list deterministic across
    runs — two things a baseline chain depends on. Watching the seats land must
    not become watching them land and recording them in that order.

    Asserted by finishing the seats in two different orders and requiring the
    same record from both."""
    _stub(monkeypatch, sleeps={"claude": 0.30, "codex": 0.02, "pi": 0.10})
    slow_first = _run(tmp_path, name="orderA")
    _stub(monkeypatch, sleeps={"claude": 0.02, "codex": 0.30, "pi": 0.10})
    slow_last = _run(tmp_path, name="orderB")
    assert list(slow_first["reviewers"]) == list(slow_last["reviewers"])
    assert slow_first["reviewers_ran"] == slow_last["reviewers_ran"]
    # …while the attribution itself DID follow the seats, or the test above proves
    # nothing about this one.
    assert slow_first["timing"]["slowest_seat"] == "claude"
    assert slow_last["timing"]["slowest_seat"] == "codex"


def test_a_seat_that_raises_still_raises_where_it_always_did(monkeypatch, tmp_path):
    """`watch` observes and never reads a result, so a seat that blew up surfaces
    from the same `.result()` in the same collection loop. An instrumentation pass
    that quietly turned a crash into a skipped seat would be the worst possible
    trade for a stopwatch."""
    _stub(monkeypatch)

    def boom(name, *a, **k):
        if name == "codex":
            raise RuntimeError("codex exploded")
        return panel.ReviewerRun([], None, 1, [])

    monkeypatch.setattr(panel, "review_llm", boom)
    with pytest.raises(RuntimeError, match="codex exploded"):
        _run(tmp_path, name="boom")


# ------------------------------------------------------------------------ formatting

@pytest.mark.parametrize("ms,want", [(None, "not measured"), (0, "0.0s"), (45_000, "45s"),
                                     (824_000, "13m 44s"), (3_930_000, "1h 05m")])
def test_durations_render_at_the_grain_a_reader_acts_on(ms, want):
    assert panel_timing.hms(ms) == want


# ------------------------------------------------------------ the refusal path is timed

def test_a_refused_round_times_the_work_it_still_does(monkeypatch, tmp_path):
    """A refusal is the cheap path by design (#138) — and this is the only thing
    that checks the claim, so its one phase has to contain what the path actually
    does: the CI read it still makes and the report it still builds.

    Asserted by making the CI read slow. A `setup` closed before it would report a
    round_ms that excluded the only expensive thing on the path, which is the exact
    shape of the gap #192 is about, in the code fixing it."""
    _stub(monkeypatch, cfg={**CFG, "reviewers": {"claude": {"enabled": True,
                                                            "model": "sonnet",
                                                            "max_diff_chars": 20}},
                              "review_panel": {"refuse_over_cap_multiple": 2}})

    def slow_ci(*a):
        time.sleep(0.30)
        return ("PASS", [], None)

    monkeypatch.setattr(panel, "review_ci", slow_ci)
    # A box carrying no vendor CLI at all — a CI runner. The round's OWN answer to
    # "which seats are here" is the conftest fixture's pin on `panel.seat_installed`,
    # and the pre-flight verdict must use that snapshot rather than taking a second
    # reading of its own. See the test below, which is what this line is here for.
    monkeypatch.setattr(panel_preflight, "seat_installed", lambda name: False)
    payload = _run(tmp_path, name="refused")
    assert payload["skip_reason"], "this round was meant to be refused"
    t = payload["timing"]
    assert t["measured_to"] == "refusal"
    assert sum(t["phases"].values()) == t["round_ms"]
    assert t["round_ms"] >= 280
    # And no seat ran, so there is nothing to report as having held the round.
    assert t["slowest_seat"] is None and t["gated_ms"] == 0


def test_the_preflight_verdict_reads_the_round_s_host_snapshot_not_path(monkeypatch,
                                                                        tmp_path):
    """The verdict must not take its own reading of which seats this box carries.

    `panel.run` resolves that once — "read ONCE per round rather than per consumer
    … two independently-timed PATH reads can disagree, and a snapshot is what makes
    the consumers below describe one host" — and then hands the snapshot to the
    budgets, the argv clamp, the prompt and the payload. `seat_ceilings` resolves
    the predicate in its own body when it is given none, so the verdict was the one
    consumer still outside that snapshot, reading PATH for itself.

    The effect is a review tool whose ANSWER depends on the machine: the same PR at
    the same size is refused on a workstation that carries `claude` and reviewed,
    truncated to a fifth of its diff, on a runner that does not — with nothing
    reporting the difference. It reached this suite as a test that passed locally
    and failed in CI, which is the mildest way it could possibly have shown up.

    Pinned FALSE, not True, because that is the direction that fails open: an empty
    host is exactly where a missing ceiling turns a refusal into a truncated
    review."""
    _stub(monkeypatch, cfg={**CFG, "reviewers": {"claude": {"enabled": True,
                                                            "model": "sonnet",
                                                            "max_diff_chars": 20}},
                              "review_panel": {"refuse_over_cap_multiple": 2}})
    monkeypatch.setattr(panel_preflight, "seat_installed", lambda name: False)
    refused = _run(tmp_path, name="bare-host")

    # The same round on a box that carries everything: the verdict is the same,
    # which is the whole claim. One direction alone passes against a verdict that
    # simply ignores the predicate.
    _stub(monkeypatch, cfg={**CFG, "reviewers": {"claude": {"enabled": True,
                                                            "model": "sonnet",
                                                            "max_diff_chars": 20}},
                              "review_panel": {"refuse_over_cap_multiple": 2}})
    monkeypatch.setattr(panel_preflight, "seat_installed", lambda name: True)
    stocked = _run(tmp_path, name="full-host")

    assert refused["skip_reason"] == stocked["skip_reason"]
    assert refused["preflight"]["verdict"] == stocked["preflight"]["verdict"] == "refuse"
    assert refused["reviewers_ran"] == stocked["reviewers_ran"] == []
