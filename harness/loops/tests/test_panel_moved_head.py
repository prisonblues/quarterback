"""A head that moves mid-round, and the range that then belongs to no round (#106).

`run()` reads the PR head from the metadata, hands it to `ReviewScope.decide` — which
under increment scope fetches `anchor...head` and makes THAT the review target — and
then re-reads the head after the diff fetch. When a push landed inside that window the
later commit is what gets recorded, deliberately: it is where the next round's fix range
has to start, or the fixer is blamed for commits it did not write.

Both halves are right on their own and they do not agree. The round reviewed up to the
EARLIER head; the next round's increment starts at the LATER one; everything between is
past the first and before the second, and is the review target of no round in the cycle.
The cycle could then stop with nothing new and nothing outstanding and stamp
`converged: true` over commits nobody read — this repo's stated disease, a failure that
records as a success.

What these tests pin is the cheap half of #106's three options: the gap costs the round
its confident stop, so it is visible in the payload instead of being inferable from a
prose note in `config_notes` that says the head moved and not what that cost. It does
not close the hole — the field still names a commit the round did not review — and the
day that `converged` feeds an automatic merge gate, the gate would be approving code no
round read and a veto will not be enough.

The negative cases are the point as much as the positive one. A veto that fires when the
head did not move, or on a whole-PR round whose next round re-reads everything anyway, is
a veto that fires on rounds with no gap in them — and `confident` is `not veto`, so a
signal that is never positive is one readers learn to skip.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402  — the `gh` seam
import panel_scope  # noqa: E402  — `fetch_increment` / `compare_facts`
from conftest import gh_stub  # noqa: E402

#: The three commits this file is about: what round 1 reviewed, what round 2 opened
#: with, and what a push put on the branch while round 2 was running.
ANCHOR = "a11ce0000000000000000000000000000000000d"
HEAD = "b0b0b00000000000000000000000000000000000"
MOVED = "c0ffee0000000000000000000000000000000000"

#: The PR's own diff. Two files, so the increment below is genuinely SMALLER than
#: the whole of it — a range that is not is rejected outright as a base-branch
#: merge and the round falls back to whole-PR scope, which is a different test.
#: The increment has to touch a file the PR touches for the same reason: anything
#: else is filtered out of the target as main's work rather than the fixer's.
PR = ("diff --git a/a.py b/a.py\n"
      "index 1111111..2222222 100644\n"
      "--- a/a.py\n"
      "+++ b/a.py\n"
      "@@ -1,0 +1,1 @@\n"
      "+first\n"
      "diff --git a/b.py b/b.py\n"
      "index 3333333..4444444 100644\n"
      "--- a/b.py\n"
      "+++ b/b.py\n"
      "@@ -1,0 +1,1 @@\n"
      "+second\n")

INCREMENT = ("diff --git a/a.py b/a.py\n"
             "index 2222222..5555555 100644\n"
             "--- a/a.py\n"
             "+++ b/a.py\n"
             "@@ -1,0 +1,1 @@\n"
             "+fixed\n")

#: What the near tier (base...anchor) comes back with — the rest of the PR, as
#: context behind the increment.
PRIOR = PR

FACTS = {"status": "ahead", "files": 1, "commits": 1, "total_commits": 1, "merges": 0}

#: The same range as the compare endpoint renders it for `_fix_range_diff`, which
#: is provenance's reader and wants per-file patches rather than a count. Answered
#: because an unreadable range is a `config_notes` line of its own, and these tests
#: read that list as one the head move alone writes to.
FIX_COMPARE = {"status": "ahead",
               "files": [{"filename": "a.py",
                          "patch": "@@ -1,1 +1,2 @@\n+the fix commit"}]}

CFG = {
    "github": "acme/board",
    # A path that cannot exist: #504 runs local git out of `path` on a round whose
    # fix range was rewritten, and a name somebody might plausibly have created
    # would make these rounds behave differently on one box than another.
    "path": "/nonexistent/acme-board",
    "_rules_baseline": ".harness-rules.sample",
    "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
    "review_panel": {},
}


def _baseline(tmp_path):
    """Round 1's payload — the thing that makes round 2 an increment at all. It
    names the head round 1 reviewed, which is round 2's anchor."""
    body = {"repo": "board", "github": "acme/board", "pr": 34, "round": 1,
            "cycle": "cyc", "head_sha": ANCHOR, "reviewers_ran": ["claude"],
            "scope": "pr", "to_fix": [], "dismissed": [], "sonar_findings": []}
    path = tmp_path / "r1.json"
    path.write_text(json.dumps(body))
    return str(path)


def _round(monkeypatch, tmp_path, *, moves_to=None, scope="auto"):
    """One dry round 2 with every subprocess replaced.

    Dry on purpose: no findings, no truncation, a settled green CI. Everything
    that could otherwise cost the round its confidence is held open, so the only
    thing left to explain a veto is the head.
    """
    fake_sh = gh_stub(meta={"headRefOid": HEAD}, head=HEAD, head_moves_to=moves_to,
                      compare=json.dumps(FIX_COMPARE), compare_diff=INCREMENT, diff=PR)
    monkeypatch.setattr(panel_core, "sh", fake_sh)
    # Scope's own two compare calls are pinned rather than answered through the
    # stub: what is under test is what the round does with a head that moved, and
    # a range fetch degrading for its own reasons would take the increment away
    # and with it the branch being tested.
    monkeypatch.setattr(panel_scope, "fetch_increment",
                        lambda repo, a, b: (PRIOR, "") if a == "main" else (INCREMENT, ""))
    monkeypatch.setattr(panel_scope, "compare_facts", lambda *a: dict(FACTS))
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: CFG)
    monkeypatch.setattr(panel, "review_llm",
                        lambda name, model, prompt, effort="", **_kw:
                        panel.ReviewerRun([], None, 10, []))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate",
                        lambda *a, **k: ([], None, panel.CoverageRuling()))
    out = tmp_path / "r2.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=2, baseline=[_baseline(tmp_path)], max_rounds=3,
                     scope=scope) == 0
    return json.loads(out.read_text())


def _moved_veto(payload):
    return [v for v in payload["round_stop"]["veto"] if "the head moved from" in v]


def test_a_dry_scoped_round_whose_head_HELD_stops_confident(monkeypatch, tmp_path):
    """The control, and the half a veto is easiest to get wrong. Nothing moved,
    nothing was truncated, CI is settled and green: this round has a complete read
    of everything between the anchor and the head it recorded, and it is entitled
    to say so. A veto here would fire on every scoped round of every cycle, and
    `confident` is `not veto` — a flag that is never positive says nothing about
    the round that carries it and trains its reader to skip the list where the real
    coverage gaps are."""
    got = _round(monkeypatch, tmp_path)
    assert got["scope"] == "increment"
    assert got["head_sha"] == HEAD
    assert _moved_veto(got) == []
    assert got["round_stop"]["confident"] is True
    assert got["round_stop"]["converged"] is True


def test_a_head_that_MOVED_costs_a_scoped_round_its_confident_stop(monkeypatch, tmp_path):
    """The defect. Everything about this round is identical to the one above except
    that a push landed while it ran, and that push put commits on the branch which
    this round's target stops short of and the next round's anchor starts after.

    Before the fix the round recorded `head_sha: c0ffee…`, said in `config_notes`
    that the head had moved, and stopped `confident: true` / `converged: true` — a
    cycle reporting convergence over code no round of it ever read."""
    got = _round(monkeypatch, tmp_path, moves_to=MOVED)
    assert got["scope"] == "increment"
    assert got["head_sha"] == MOVED, "provenance still records the later commit"
    assert got["round_stop"]["confident"] is False
    assert got["round_stop"]["converged"] is False


def test_the_veto_names_BOTH_commits_and_says_what_was_not_reviewed(monkeypatch, tmp_path):
    """An operator is told to read the veto list, so the line has to carry the
    consequence and not just the fact. The `config_notes` entry already says the
    head moved and that the later commit was recorded; what it never said is that a
    range had just become the review target of no round, which is the thing acted
    on."""
    got = _round(monkeypatch, tmp_path, moves_to=MOVED)
    moved = _moved_veto(got)
    assert len(moved) == 1, "one line for one move"
    line = moved[0]
    assert HEAD[:8] in line and MOVED[:8] in line
    assert "the review target of no round" in line


def test_a_MOVED_head_on_a_WHOLE_PR_round_is_left_to_the_next_round(monkeypatch, tmp_path):
    """The deliberate limit of the cheap fix, pinned so it is a decision rather than
    an omission. Under whole-PR scope the next round re-reads the PR, so the range
    closes on its own — and this round's own material came off `gh pr diff` at a
    moment nothing can date, so the later commit may well be in it. Vetoing there
    would fire on rounds that did read the code, which is the direction that makes
    the signal worthless."""
    got = _round(monkeypatch, tmp_path, moves_to=MOVED, scope="pr")
    assert got["scope"] == "pr"
    assert got["head_sha"] == MOVED
    assert _moved_veto(got) == []
    assert got["round_stop"]["confident"] is True
