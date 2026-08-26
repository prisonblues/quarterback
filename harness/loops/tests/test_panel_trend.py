"""The cross-round trend block (#490): every round of a cycle beside the others.

Every round's report states that round's own figures and nothing else, so a reader
has to hold three reports in their head and do the arithmetic to see which way the
cycle is going. Read one at a time, a diverging cycle looks flat — 8 -> 14 -> 15
findings reads as converging right up until you notice the PR tripled underneath
it, on an underlying change of 113 lines. That happened on a live cycle: three
rounds ran before anyone computed the trend, and the answer took about ninety
seconds of arithmetic on numbers that had been in the payload since round 2.

Two properties get pinned harder than the rendering does, because they are the two
ways this block can stop being worth having:

**It is REPORTING and it must stay reporting.** No dial, no gate, nothing that can
end a cycle — #489 proposes the gate and is deliberately a separate decision. The
test at the bottom reads the stop rule's own source and insists the trend does not
appear in it, because a feature drifting into a stop condition is exactly the kind
of change that looks harmless in a diff.

**There is no density metric, and there is not allowed to be one.** While reading
the cycle this block exists for, the reporter computed findings-per-10k-chars by
hand and got 9.46 -> 7.97 -> 4.82: a number that falls every round, reads as steady
improvement, and was describing a cycle that was diverging. It falls because the
denominator is growing, which is the failure and not evidence against it. So
`test_the_block_emits_NO_density_figure` asserts the rendered rows exactly, and
`test_the_reporters_own_misleading_figure_is_not_in_the_block` computes the
misleading number from the very fixture the issue was filed from and insists it is
nowhere in the report. If a future change adds a per-size figure it must sit beside
the absolute count AND the growth ratio, and it will have to argue with these two
tests to do it.
"""

import inspect
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_caps  # noqa: E402  — the ceilings, which must not learn about this
import panel_core  # noqa: E402  — `sh` lives here since #129
import panel_rounds  # noqa: E402
import panel_preflight as pf  # noqa: E402
from conftest import gh_stub  # noqa: E402


# --------------------------------------------------------------------------
# Reading one earlier round out of its payload
# --------------------------------------------------------------------------

def _payload(**over) -> dict:
    """A baseline payload with the fields `_trend_row` reads, and no others — so a
    test that starts depending on a field it should not read goes red."""
    return {"round": 2, "cycle": "cyc", "reviewed": True, "pr_chars": 1000,
            "scope": "increment", "to_fix": [], "sonar_findings": [],
            "dismissed": [], "provenance_counts": {}, **over}


def test_a_rounds_findings_are_its_two_OUTSTANDING_buckets():
    """`to_fix` + `sonar_findings`, which is exactly the population `outstanding`
    counts on a live round. Sonar's hard-gate issues MUST end up resolved, so a
    cycle carrying one has work to do and a row that hid it would show the cycle
    quieter than it is."""
    row = panel_rounds._trend_row(2, _payload(
        to_fix=[{"severity": "P3"}, {"severity": "P1"}],
        sonar_findings=[{"severity": "P2"}]))
    assert (row.findings, row.p1p2) == (3, 2)


def test_the_DISMISSED_bucket_is_not_counted():
    """The master ruled those not real and no fixer will ever touch them. Counted,
    every row would be inflated by the judge's own work — and a cycle whose judge
    got stricter between rounds would read as one that was finding more."""
    row = panel_rounds._trend_row(2, _payload(
        to_fix=[{"severity": "P2"}],
        dismissed=[{"severity": "P1"}, {"severity": "P1"}]))
    assert (row.findings, row.p1p2) == (1, 1)


def test_an_UNREADABLE_severity_counts_as_severe():
    """`severity_at_least`'s standing asymmetry, and the right direction here too:
    a row that under-states severity is a row that argues for another round, and a
    hand-edited or foreign payload must not be able to quiet the block."""
    row = panel_rounds._trend_row(2, _payload(
        to_fix=[{"severity": "BLOCKER"}, {}, {"severity": "P4"}]))
    assert (row.findings, row.p1p2) == (3, 2)


def test_a_round_that_reviewed_NOTHING_counts_nothing():
    """A skipped or refused round has no finding count, and `0 findings` would put
    the strongest convergence signal the block can show against a round that never
    ran. Its size goes too: `first_reviewed` refuses an unreviewed round as a
    denominator (#298), so a growth ratio computed from one measures nothing."""
    row = panel_rounds._trend_row(2, _payload(reviewed=False, pr_chars=9999,
                                              to_fix=[{"severity": "P1"}]))
    assert row.reviewed is False
    assert (row.findings, row.p1p2, row.pr_chars) == (None, None, None)


def test_the_size_is_the_WHOLE_PR_and_never_the_review_target():
    """`pr_chars`, not `diff_chars` (#298). Under `increment` scope the target is
    one fix commit, so a size column read off it would cliff at round 2 and show
    the change shrinking exactly while it grows — which is the reading this whole
    block exists to prevent, arriving through its own size column."""
    row = panel_rounds._trend_row(2, _payload(pr_chars=4000, diff_chars=120))
    assert row.pr_chars == 4000


def test_a_pr_scoped_payload_too_old_for_pr_chars_still_has_a_size():
    """`_whole_pr_chars`' fallback, inherited rather than re-implemented: round 1
    of a cycle is `pr`-scoped by construction, so its `diff_chars` IS the whole PR
    and a cycle spanning the release that added `pr_chars` keeps its denominator."""
    p = _payload(scope="pr", diff_chars=777)
    del p["pr_chars"]
    assert panel_rounds._trend_row(1, p).pr_chars == 777


def test_an_increment_payload_too_old_for_pr_chars_has_NO_size():
    """The other half of the same rule. That payload's `diff_chars` is a fix commit
    and cannot stand in for the PR, so the cell says `?` rather than inventing a
    number that makes the PR look to have shrunk."""
    p = _payload(scope="increment", diff_chars=120)
    del p["pr_chars"]
    assert panel_rounds._trend_row(2, p).pr_chars is None


# --------------------------------------------------------------------------
# `attributed` — the one predicate two blocks share
# --------------------------------------------------------------------------

@pytest.mark.parametrize("counts,expect", [
    ({}, False),
    ({"introduced": 0, "missed": 0, "missed-unread": 0, "unknown": 3}, False),
    # ALL-ZERO is a measurement, not a failure: the round attributed and had
    # nothing to attribute, which is what a round of repeats looks like. `?` there
    # would say the fix range was unreadable when it was read fine.
    ({"introduced": 0, "missed": 0, "missed-unread": 0, "unknown": 0}, True),
    # Partly placed. `unknown` is not the ONLY positive bucket, so attribution ran;
    # the two it could not place are the reason `introduced` is read as a floor.
    ({"introduced": 0, "missed": 1, "missed-unread": 0, "unknown": 2}, True),
    ({"introduced": 0, "missed": 2, "missed-unread": 0, "unknown": 0}, True),
    ({"introduced": 1, "missed": 0, "missed-unread": 0, "unknown": 0}, True),
    ({"introduced": 0, "missed": 0, "missed-unread": 1, "unknown": 0}, True),
    # Not a mapping at all, and a mapping whose only content is a key this
    # function does not recognise: both are payloads nothing can be read from.
    (None, False),
    ([("introduced", 4)], False),
    ({"invented": 9}, False),
])
def test_whether_a_round_could_attribute_at_all(counts, expect):
    """An all-`unknown` tally means the fix range was unreadable — no commit
    recorded, a rewritten branch, an API refusal — so every bucket that says
    something about the fix pass is 0 by failure rather than by measurement.
    Rendered as a number that is "**0 introduced**": a bolded claim about the fix
    pass, and a false one. One predicate, so the trend block and the report's
    `of those:` line cannot answer it two ways in the same report."""
    assert panel_rounds.attributed(counts) is expect


@pytest.mark.parametrize("value,expect", [(0, 0), (7, 7), (-1, None), (True, None),
                                          (1.0, None), ("3", None), (None, None)])
def test_a_count_a_payload_can_be_believed_about(value, expect):
    """`_positive_int`'s sibling, and separate because the two admit different
    numbers on purpose: a SIZE of 0 cannot be a denominator, while a COUNT of 0 is
    the most interesting reading in this block — nothing was introduced."""
    assert panel_rounds._nonneg_int(value) is expect


def test_a_round_of_REPEATS_introduced_zero_rather_than_unknown():
    """The all-zero tally arriving in a row. Every finding this round was raised
    before, so provenance never had one to place — the fix range was read fine and
    the honest answer is `0`. Read as unattributable it would print `?`, which says
    the measurement failed, on the quietest and most reassuring round of a cycle."""
    row = panel_rounds._trend_row(2, _payload(
        to_fix=[{"severity": "P2"}, {"severity": "P3"}],
        provenance_counts={"introduced": 0, "missed": 0,
                           "missed-unread": 0, "unknown": 0}))
    assert row.introduced == 0
    rows = [panel.RoundTrend(1, True, 2, 1, None, 1000), row]
    assert "0 (0%)" in _table(panel.cycle_trend_lines(rows, (1, 1000, "pr")))[2]


def test_a_round_that_reviewed_NOTHING_introduced_nothing_either():
    """A skipped in-cycle round records an all-zero tally by construction — it is
    the shape that says "could have attributed, had nothing to". It reviewed no
    code, so `0 introduced` off it is the same fabrication as `0 findings`, and the
    row is blank rather than reassuring."""
    row = panel_rounds._trend_row(2, _payload(
        reviewed=False, provenance_counts={"introduced": 0, "missed": 0,
                                           "missed-unread": 0, "unknown": 0}))
    assert row.introduced is None


def test_an_unbelievable_introduced_count_reads_as_UNANSWERED():
    """A tally that places something but whose `introduced` is not a count leaves
    the cell at `?`. Coerced to 0 it would say the fix pass introduced nothing,
    which is the flattering direction and is not what the payload said."""
    row = panel_rounds._trend_row(2, _payload(
        provenance_counts={"introduced": "lots", "missed": 2}))
    assert row.introduced is None


# --------------------------------------------------------------------------
# The block as it renders
# --------------------------------------------------------------------------

FIRST = (1, 113_402, "pr")


def diverging() -> list:
    """The cycle from the field report #490 was filed from: 8 -> 14 -> 15 findings
    against a PR that tripled. Read a row at a time it converges; read down the
    `whole PR` column it does not.

    A function and not a module constant so that the module still IMPORTS against a
    tree without this feature in it — which is what makes the report assertions
    below fail on the assertion, where a collection-time `AttributeError` would
    have demonstrated nothing about whether they catch anything.
    """
    return [panel.RoundTrend(1, True, 8, 2, None, 113_402),
            panel.RoundTrend(2, True, 14, 5, 9, 236_187),
            panel.RoundTrend(3, True, 15, 4, 13, 340_341)]


def _table(lines: list[str]) -> list[str]:
    """The rows inside the block's fence, header included."""
    fence = [i for i, ln in enumerate(lines) if ln == "```"]
    assert len(fence) == 2, lines
    return lines[fence[0] + 1:fence[1]]


def test_one_round_is_not_a_trend():
    """A single round beside itself is the report the reader already has, and an
    empty block in an already-dense report costs space for nothing."""
    assert panel.cycle_trend_lines(diverging()[:1], FIRST) == []
    assert panel.cycle_trend_lines([], None) == []


def test_the_block_emits_NO_density_figure():
    """**The issue's one hard constraint, asserted as an exact rendering.**

    Five columns and no sixth. Findings-per-10k-chars fell 9.46 -> 7.97 -> 4.82
    across exactly these rounds, reads as steady improvement, and describes a cycle
    that was diverging — it falls because the denominator is growing, which is the
    problem rather than evidence against it. Any per-size figure added here has to
    sit beside BOTH the absolute count and the growth ratio; the version that adds
    none needs no such argument, and this is what holds it to that.

    Asserted whole rather than by searching for words a future column might not
    use: a test looking for "density" is passed by a column called "per 10k"."""
    assert _table(panel.cycle_trend_lines(diverging(), FIRST)) == [
        "round  findings  P1/P2  introduced  whole PR  vs r1",
        "   r1         8      2           —   113,402  1.00x",
        "   r2        14      5     9 (64%)   236,187  2.08x",
        "   r3        15      4    13 (87%)   340,341  3.00x",
    ]


def test_the_reporters_own_misleading_figure_is_not_in_the_block():
    """The same constraint from the other end, on the actual numbers that were
    computed by hand on the live cycle. If any of 9.46 / 7.97 / 4.82 ever appears
    in this block, the block has started telling the reader the opposite of what it
    was built to tell them."""
    text = "\n".join(panel.cycle_trend_lines(diverging(), FIRST))
    for row in diverging():
        assert f"{row.findings / row.pr_chars * 10_000:.2f}" not in text


def test_the_growth_column_names_the_round_it_measures_from():
    """A cycle whose round 1 was skipped measures from round 2, and a header still
    reading `vs r1` would silently name a comparison nobody is making."""
    rows = [panel.RoundTrend(2, True, 4, 1, None, 1000),
            panel.RoundTrend(3, True, 6, 1, 2, 2500)]
    table = _table(panel.cycle_trend_lines(rows, (2, 1000, "pr")))
    assert table[0].endswith("vs r2")
    assert table[-1].endswith("2.50x")


def test_no_denominator_means_no_ratio_and_no_claim_of_one():
    """`Baseline.first_reviewed` is the only denominator this column will take, so
    that this ratio and `max_fix_growth`'s veto line are the same measurement. Where
    it is missing the ceiling does not run either, and the column says so rather
    than picking a substitute — a report carrying two ratios from two denominators
    is worse than one carrying none."""
    table = _table(panel.cycle_trend_lines(diverging(), None))
    assert table[0].endswith("growth")
    assert all(row.endswith("?") for row in table[1:])


def test_a_question_that_does_not_ARISE_reads_differently_from_one_that_FAILED():
    """Round 1 has no earlier fix pass, so `introduced` is `—`. A later round was
    asked and could not answer — an unreadable fix range — and gets `?`. Collapsed
    into one mark, a cycle whose attribution was silently broken for two rounds
    reads exactly like a cycle whose rounds had nothing to attribute."""
    rows = [panel.RoundTrend(1, True, 3, 1, None, 1000),
            panel.RoundTrend(2, True, 5, 1, None, 1200)]
    r1, r2 = _table(panel.cycle_trend_lines(rows, (1, 1000, "pr")))[1:]
    assert "—" in r1 and "?" in r2


def test_a_round_that_never_RAN_says_so_in_words():
    """Not four `?`s: "this round did not happen" is a different fact from "this
    round's numbers did not survive". It fits under the `findings` header on
    purpose, so one skipped round cannot widen the column the others are read down."""
    rows = [diverging()[0], panel.RoundTrend(2, False), diverging()[2]]
    table = _table(panel.cycle_trend_lines(rows, FIRST))
    assert table[2] == "   r2   not run"
    assert table[1].endswith("1.00x") and table[3].endswith("3.00x")


def test_a_round_with_NO_findings_takes_no_share():
    """`0 (0%)` would be a division this block never performs. The count is printed
    and the percentage is not."""
    rows = [panel.RoundTrend(1, True, 2, 0, None, 1000),
            panel.RoundTrend(2, True, 0, 0, 0, 1100)]
    assert _table(panel.cycle_trend_lines(rows, (1, 1000, "pr")))[2].split() \
        == ["r2", "0", "0", "0", "1,100", "1.10x"]


# --------------------------------------------------------------------------
# A whole round, end to end
# --------------------------------------------------------------------------

def _diff(files: int) -> str:
    """A PR diff of `files` files, so a later round can be measurably bigger than
    the one before it — which is the whole quantity the growth column reports."""
    return "".join(
        f"diff --git a/app/f{i}.py b/app/f{i}.py\n"
        f"index 1111111..2222222 100644\n"
        f"--- a/app/f{i}.py\n"
        f"+++ b/app/f{i}.py\n"
        "@@ -1,1 +1,2 @@\n"
        f"+line = {i}\n" for i in range(files))


COMPARE = json.dumps({"status": "ahead", "files": [
    {"filename": "app/f0.py",
     "patch": "@@ -9,2 +9,4 @@\n context\n+introduced one\n+introduced two\n"}]})

CFG = {"github": "acme/e2e", "path": "/tmp/acme-e2e",
       "_rules_baseline": ".harness-rules.sample",
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
       # The pre-flight refusal and the manifest substitution have their own
       # suites; here they would replace the arithmetic under test.
       "review_panel": {"refuse_over_cap_multiple": 0, "manifest_moves": False}}


@pytest.fixture(autouse=True)
def every_seat_is_on_this_box(monkeypatch):
    """Pin the HOST out of every round in this file: `seat_ceilings` skips a seat
    whose CLI is not on PATH, so a test that leaves the real predicate in place is
    asserting on which vendor CLIs the machine running the suite happens to carry."""
    monkeypatch.setattr(pf, "seat_installed", lambda name: True)


def _round(monkeypatch, tmp_path, round_no, findings, head, files, baseline=(),
           max_rounds=4):
    """One panel run with every subprocess replaced. `findings` is a list of
    `(file, line, severity, title)`."""
    fake_sh = gh_stub(meta={"title": "feat: mirror", "additions": 20,
                            "deletions": 2, "headRefOid": head},
                      compare=COMPARE, diff=_diff(files))

    def fake_review(name, model, prompt, effort="", **_kw):
        return panel.ReviewerRun(
            [panel.Finding("claude", sev, f, ln, t, "detail")
             for f, ln, sev, t in findings], None, 800, None)

    def fake_adjudicate(clusters, diff, model, pr, budget=None, coverage=None,
                        ci="", **_kw):
        return ([panel.Canonical(id=panel._finding_id(pr, i + 1),
                                 severity=f.severity, file=f.file, line=f.line,
                                 synthesis=f.title, verdict="confirmed",
                                 detail="detail", reported_by=[f], rationale="real")
                 for i, grp in enumerate(clusters) for f in grp], None, "")

    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: CFG)
    monkeypatch.setattr(panel_core, "sh", fake_sh)
    monkeypatch.setattr(panel, "review_llm", fake_review)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", fake_adjudicate)
    out = tmp_path / f"r{round_no}.json"
    assert panel.run("e2e", 77, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline),
                     max_rounds=max_rounds) == 0
    return str(out), json.loads(out.read_text())


def test_a_review_only_run_has_no_cycle_and_no_trend(monkeypatch, tmp_path, capsys):
    """No round cap, no baseline, no `--round` past 1: nothing is driving a loop, so
    a "cycle trend" carrying this run's single row would be a claim about a cycle
    nobody is running. Gated on `cycle_run`, exactly as `new_findings` and
    `round_stop` beside it are, and for the same reason."""
    _, r = _round(monkeypatch, tmp_path, 1,
                  [("app/f0.py", 3, "P2", "a stale mirror")],
                  head="aaa111", files=2, max_rounds=None)
    assert r["cycle_trend"] == []
    assert "Cycle so far" not in capsys.readouterr().out


def test_round_ONE_shows_no_block_at_all(monkeypatch, tmp_path, capsys):
    """There is no cycle yet, so there is nothing to put beside anything. The block
    is gated exactly as the Rounds block above it is."""
    _round(monkeypatch, tmp_path, 1, [("app/f0.py", 3, "P2", "a stale mirror")],
           head="aaa111", files=2)
    assert "Cycle so far" not in capsys.readouterr().out


def test_round_TWO_prints_both_rounds_and_the_growth_between_them(
        monkeypatch, tmp_path, capsys):
    """The block arriving in a real report. Round 2 finds more than round 1 against
    a PR that doubled — which reads as a worse cycle only if the two are put beside
    each other, and that is the whole feature."""
    r1_path, r1 = _round(monkeypatch, tmp_path, 1,
                         [("app/f0.py", 3, "P2", "a stale mirror")],
                         head="aaa111", files=2)
    capsys.readouterr()
    _, r2 = _round(monkeypatch, tmp_path, 2,
                   [("app/f0.py", 10, "P1", "the fix left a dangling handle"),
                    ("app/f0.py", 3, "P2", "a stale mirror"),
                    ("app/f1.py", 4, "P3", "an unrelated defect nobody saw")],
                   head="bbb222", files=5, baseline=[r1_path])
    table = _table([ln for ln in capsys.readouterr().out.splitlines()])
    assert table[0].startswith("round")
    assert table[1].split() == ["r1", "1", "1", "—", f"{r1['pr_chars']:,}", "1.00x"]
    # Two of the three are new; one of those sits on a line the fix pass wrote.
    assert table[2].split() == ["r2", "3", "2", "1", "(33%)",
                                f"{r2['pr_chars']:,}",
                                f"{r2['pr_chars'] / r1['pr_chars']:.2f}x"]


def test_the_blocks_ratio_is_the_SAME_ONE_the_growth_ceiling_measures(
        monkeypatch, tmp_path):
    """One denominator, one arithmetic. `max_fix_growth` stops a cycle on this
    ratio and prints it in a veto line; a block printing a second ratio from a
    second denominator would leave a reader unable to tell which of them the
    ceiling is about to fire on."""
    r1_path, _ = _round(monkeypatch, tmp_path, 1,
                        [("app/f0.py", 3, "P2", "a stale mirror")],
                        head="aaa111", files=2)
    _, r2 = _round(monkeypatch, tmp_path, 2,
                   [("app/f0.py", 10, "P1", "a dangling handle")],
                   head="bbb222", files=5, baseline=[r1_path])
    assert r2["cycle_trend"][-1]["growth"] == r2["round_stop"]["fix_growth"]["ratio"]


def test_round_ONE_is_its_own_denominator(monkeypatch, tmp_path):
    """Round 1 is the cycle's first reviewed round by construction, so its stored
    `growth` is 1.0 — the same number round 2 will compute for that row off the
    baseline. Left null, a board plotting `growth` across a cycle's payloads sees
    the series start at nothing on the one round whose answer is not in doubt."""
    _, r1 = _round(monkeypatch, tmp_path, 1,
                   [("app/f0.py", 3, "P2", "a stale mirror")],
                   head="aaa111", files=2)
    assert r1["cycle_trend"][0]["growth"] == 1.0


def test_a_round_TWO_with_no_usable_baseline_invents_no_denominator(
        monkeypatch, tmp_path):
    """The case round 1's rule must not be widened to cover. Nothing here knows how
    big the PR was when the cycle started — `max_fix_growth` does not run either —
    and reading this round's own size as the denominator would record 1.0 against a
    PR that may have been growing for three rounds."""
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"round": 1, "cycle": "cyc", "reviewed": True,
                               "repo": "e2e", "github": "acme/e2e", "pr": 77,
                               "to_fix": [], "dismissed": [], "sonar_findings": []}))
    _, r2 = _round(monkeypatch, tmp_path, 2,
                   [("app/f0.py", 10, "P1", "a dangling handle")],
                   head="bbb222", files=5, baseline=[str(old)])
    assert [t["growth"] for t in r2["cycle_trend"]] == [None, None]


def test_the_payload_carries_the_block_as_data(monkeypatch, tmp_path):
    """So a board or an orchestrator gets the trend without re-reading every
    earlier round's file — the issue names the orchestrator as a reader of this
    alongside the human."""
    r1_path, r1 = _round(monkeypatch, tmp_path, 1,
                         [("app/f0.py", 3, "P2", "a stale mirror")],
                         head="aaa111", files=2)
    # Round 1 OF A CYCLE carries its own row, so the orchestrator has the first
    # row banked before the round that needs to compare against it exists.
    assert [t["round"] for t in r1["cycle_trend"]] == [1]
    _, r2 = _round(monkeypatch, tmp_path, 2,
                   [("app/f0.py", 10, "P1", "a dangling handle")],
                   head="bbb222", files=5, baseline=[r1_path])
    assert [t["round"] for t in r2["cycle_trend"]] == [1, 2]
    assert r2["cycle_trend"][0] == {"round": 1, "reviewed": True, "findings": 1,
                                    "p1p2": 1, "introduced": None,
                                    "pr_chars": r1["pr_chars"], "growth": 1.0}
    assert r2["cycle_trend"][1]["introduced"] == 1


def test_a_baseline_for_THIS_round_never_reaches_the_block(
        monkeypatch, tmp_path, capsys):
    """A stale payload from a re-run of the same round would put two rows labelled
    `r2` in the block, with different numbers, in the one place whose whole job is
    to be read down a column.

    Nothing in this feature filters for it, and this is the test that says why not:
    `load_baseline` refuses such a payload outright and says so in `config_notes`,
    so the row cannot arrive. Pinned rather than assumed, because the alternative —
    a second filter here — would be unreachable code carrying a note that can never
    fire, and the invariant it rests on lives in another module."""
    r1_path, _ = _round(monkeypatch, tmp_path, 1,
                        [("app/f0.py", 3, "P2", "a stale mirror")],
                        head="aaa111", files=2)
    r2_path, _ = _round(monkeypatch, tmp_path, 2,
                        [("app/f0.py", 10, "P1", "a dangling handle")],
                        head="bbb222", files=5, baseline=[r1_path])
    capsys.readouterr()
    _, again = _round(monkeypatch, tmp_path, 2,
                      [("app/f0.py", 10, "P1", "a dangling handle")],
                      head="bbb222", files=5, baseline=[r1_path, r2_path])
    # Round 2's row here is THIS run's own, built from `outstanding`; the stale
    # payload contributed nothing.
    assert [t["round"] for t in again["cycle_trend"]] == [1, 2]
    assert any("is round 2, which is not earlier than this run's round 2" in n
               for n in again["config_notes"])


# --------------------------------------------------------------------------
# It is reporting, and it has to stay reporting
# --------------------------------------------------------------------------

@pytest.mark.parametrize("fn", [panel_rounds.round_stop, panel_caps.check])
def test_nothing_that_can_STOP_a_cycle_reads_the_trend(fn):
    """#490 is reporting only: no dial, no gate, nothing that can end a cycle. It is
    worth having whether or not the injection-rate stop of #489 is ever wired to
    `round_stop`, and chaining a cheap reporting improvement to a policy argument is
    how the cheap half waits on the expensive one.

    Asserted against the source of the two functions that CAN end a cycle, because
    a feature drifting into a stop condition is precisely the change that looks
    harmless in a diff — nothing else in the tree would go red for it."""
    assert not re.search(r"\btrend\b", inspect.getsource(fn))


def test_the_trend_does_not_reach_the_fixers_brief(monkeypatch, tmp_path):
    """The other place a reporting number turns into an instruction. The block is
    for the operator (and the orchestrator) deciding whether to go again; a fixer
    handed a trend would be being asked to answer for the cycle rather than for the
    findings, which nothing here has the evidence to ask."""
    r1_path, _ = _round(monkeypatch, tmp_path, 1,
                        [("app/f0.py", 3, "P2", "a stale mirror")],
                        head="aaa111", files=2)
    _, r2 = _round(monkeypatch, tmp_path, 2,
                   [("app/f0.py", 10, "P1", "a dangling handle")],
                   head="bbb222", files=5, baseline=[r1_path])
    for finding in r2["to_fix"]:
        assert "cycle_trend" not in finding and "trend" not in finding
