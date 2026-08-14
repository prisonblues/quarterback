"""Dedup happens once, in the judge, and never at the cost of a reviewer's text.

The panel used to merge findings three times over: a positional key before the
judge, a judge that could see duplicates but had no verb for them, and a fixer
that did the real merging by hand downstream. Only the last one worked, and the
first one was lossy — it kept one finding's text and discarded the rest, so an
observation only one reviewer made survived precisely when the merge FAILED.

So the tests here are mostly about what must NOT be lost: every reporter's
verbatim account, its own severity and line, and any finding the judge did not
mention. Clustering survives only as a hint, and is tested as one.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402

F = panel.Finding


def find(reviewer="codex", sev="P2", file="a.py", line=10, title="t", detail="d"):
    return F(reviewer=reviewer, severity=sev, file=file, line=line,
             title=title, detail=detail)


def clusters_of(*findings):
    return panel.cluster_findings(list(findings))


# --------------------------------------------------------------- clustering (a hint)

def test_nearby_lines_in_one_file_cluster_regardless_of_where_the_tens_fall():
    """The old key was `line // 10`, a fixed grid: 39 and 41 (two apart) landed in
    different buckets purely because a multiple of ten sat between them."""
    a, b = find(line=39, reviewer="codex"), find(line=41, reviewer="claude")
    assert clusters_of(a, b) == [[a, b]]


def test_a_run_of_close_findings_stays_one_cluster():
    """The window is between neighbours, so 10, 19, 28 is one observation area —
    and 10 vs 28 alone is not."""
    a, b, c = find(line=10), find(line=19, reviewer="pi"), find(line=28, reviewer="claude")
    assert clusters_of(a, b, c) == [[a, b, c]]
    assert clusters_of(a, c) == [[a], [c]]


def test_same_name_in_different_directories_is_not_the_same_file():
    """The old key used `Path(f.file).name`, so two unrelated `test_x.py` merged."""
    a = find(file="api/tests/test_x.py", line=10)
    b = find(file="web/tests/test_x.py", line=10, reviewer="claude")
    assert clusters_of(a, b) == [[a], [b]]


def test_findings_without_a_line_cluster_per_file_and_not_with_lined_ones():
    a, b = find(line=None), find(line=None, reviewer="claude")
    lined = find(line=3, reviewer="pi")
    assert sorted(clusters_of(a, b, lined), key=len) == [[lined], [a, b]]


def test_the_same_defect_cited_at_distant_lines_is_left_for_the_judge():
    """The duplicate pair that actually recurs — one defect, two line numbers —
    is semantic, and no line arithmetic finds it. Clustering must not pretend to."""
    a = find(file="tests/test_x.py", line=100)
    b = find(file="tests/test_x.py", line=41, reviewer="claude")
    assert clusters_of(a, b) == [[b], [a]]


def test_clusters_are_ordered_worst_first():
    low = find(sev="P4", file="z.py")
    high = find(sev="P1", file="a.py")
    assert clusters_of(low, high) == [[high], [low]]


# ------------------------------------------------------------------- the judge merges

def judged(reply, findings, pr=1609):
    """Run adjudicate with `reply` as the judge's output."""
    return panel._parse_verdicts(reply, list(findings), pr)


def test_a_merge_keeps_every_reviewer_s_account_verbatim():
    """The point of the change: the merged statement is NEW, the originals ride
    along. Previously the runner-up contributed its name and nothing else."""
    a = find(reviewer="codex", sev="P2", line=213, title="double render",
             detail="with the extra valid point that the helper calls create_app twice")
    b = find(reviewer="pi", sev="P3", line=44, title="fixture rebuilds the app", detail="")
    [merged] = judged([{"id": "F1", "members": [0, 1], "real": True, "severity": "P2",
                        "file": "a.py", "line": 213, "synthesis": "the helper rebuilds "
                        "the app and calls create_app twice", "reason": "confirmed"}],
                      [a, b])
    assert merged.synthesis == "the helper rebuilds the app and calls create_app twice"
    accounts = merged.as_dict()["reported_by"]
    assert [r["reviewer"] for r in accounts] == ["codex", "pi"]
    assert "calls create_app twice" in accounts[0]["account"]
    assert accounts[0]["account"] == "double render — " + a.detail
    assert accounts[1]["account"] == "fixture rebuilds the app"


def test_each_reporter_keeps_its_own_severity_and_line():
    """Calibration against the judge is only measurable if the reviewer's own
    call is stored rather than reconciled away."""
    a = find(reviewer="codex", sev="P1", line=213)
    b = find(reviewer="pi", sev="P3", line=44)
    [merged] = judged([{"id": "F1", "members": [0, 1], "real": True, "severity": "P2"}], [a, b])
    accounts = merged.as_dict()["reported_by"]
    assert [(r["severity"], r["line"]) for r in accounts] == [("P1", 213), ("P3", 44)]
    assert merged.severity == "P2"


def test_attribution_is_a_field_not_an_inference():
    a, b = find(reviewer="codex"), find(reviewer="claude")
    [merged] = judged([{"id": "F1", "members": [0, 1], "real": True}], [a, b])
    assert merged.reviewers == ["codex", "claude"]
    assert merged.as_dict()["reviewers"] == ["codex", "claude"]


def test_ids_are_run_local_and_stable_per_pr():
    out = judged([{"id": "F1", "members": [0]}, {"id": "F2", "members": [1]}],
                 [find(), find(reviewer="pi")], pr=42)
    assert [c.id for c in out] == ["42-F01", "42-F02"]


def test_related_is_resolved_to_this_run_s_ids_and_dangling_refs_are_dropped():
    """Four findings across four files can be one design decision — not the same
    issue, so not merged, but linked so the fixer decides once."""
    out = judged([{"id": "A", "members": [0], "related": ["B", "ZZ", "A"]},
                  {"id": "B", "members": [1], "related": ["A"]}],
                 [find(), find(reviewer="pi")], pr=7)
    assert out[0].related == ["7-F02"]      # B resolved; ZZ unknown; self dropped
    assert out[1].related == ["7-F01"]


def test_a_dismissed_finding_keeps_its_accounts_too():
    """A dismissal is a judgement about the code, not permission to lose the
    evidence — the board scores reviewers on it."""
    [c] = judged([{"id": "F1", "members": [0], "real": False, "reason": "guarded above"}],
                 [find(reviewer="pi", title="null deref")])
    assert c.verdict == "dismissed" and c.rationale == "guarded above"
    assert c.as_dict()["reported_by"][0]["reviewer"] == "pi"


def test_the_judge_may_spell_its_reason_rationale():
    [c] = judged([{"id": "F1", "members": [0], "rationale": "real bug"}], [find()])
    assert c.rationale == "real bug"


def test_severity_and_location_fall_back_to_the_worst_report():
    """An incomplete verdict costs the judge's refinement, never the finding."""
    a = find(reviewer="codex", sev="P3", file="a.py", line=9, title="a")
    b = find(reviewer="pi", sev="P1", file="b.py", line=4, title="b")
    [c] = judged([{"id": "F1", "members": [0, 1]}], [a, b])
    assert (c.severity, c.file, c.line) == ("P1", "b.py", 4)
    assert c.synthesis == "b — d"            # the worst report's own words


# ----------------------------------------------------------- nothing is ever suppressed

def test_a_finding_the_judge_never_mentioned_survives_unjudged():
    a, b = find(reviewer="codex"), find(reviewer="pi", line=400)
    out = judged([{"id": "F1", "members": [0], "real": True, "reason": "yes"}], [a, b])
    assert [c.verdict for c in out] == ["confirmed", "unjudged"]
    assert out[1].rationale == "unjudged"
    assert out[1].reported_by == [b]


def test_an_account_claimed_twice_stays_with_the_first_issue():
    """Two issues sharing one account would double-count it in every per-reviewer
    statistic on the board."""
    a, b = find(reviewer="codex"), find(reviewer="pi")
    out = judged([{"id": "F1", "members": [0, 1]}, {"id": "F2", "members": [1]}], [a, b])
    assert len(out) == 1 and out[0].reported_by == [a, b]


def test_a_verdict_naming_no_valid_report_is_dropped():
    """It attributes to nobody: crediting a reviewer that said nothing is worse
    than losing a record the judge invented."""
    out = judged([{"id": "F1", "members": [99], "real": True},
                  {"id": "F2", "members": [], "real": True},
                  {"id": "F3", "members": [0], "real": True}], [find()])
    assert len(out) == 1 and out[0].reported_by[0].reviewer == "codex"


def test_a_severity_outside_p1_p4_falls_back_to_the_reviewer_s_own():
    """An unreadable severity would reach the board's leaderboard as a bucket
    nothing counts; the reviewer's own call is a real answer."""
    [c] = judged([{"id": "F1", "members": [0], "severity": "critical"}],
                 [find(sev="P2")])
    assert c.severity == "P2"


def test_report_ids_quoted_as_strings_still_merge():
    """`"members": ["0", "1"]` has said exactly what it meant — dropping those
    would silently un-merge the finding."""
    [c] = judged([{"id": "F1", "members": ["0", "1"]}],
                 [find(reviewer="codex"), find(reviewer="pi")])
    assert c.reviewers == ["codex", "pi"]


def test_one_verdict_listing_a_report_twice_credits_it_once():
    [c] = judged([{"id": "F1", "members": [0, 0]}], [find(reviewer="codex")])
    assert len(c.reported_by) == 1


def test_junk_in_the_judge_reply_is_skipped_not_fatal():
    out = judged(["nonsense", 7, None, {"id": "F1", "members": [0]}], [find()])
    assert len(out) == 1


def test_an_absent_judge_keeps_every_finding_with_its_account(monkeypatch):
    monkeypatch.setattr(panel.shutil, "which", lambda _: None)
    a, b = find(reviewer="codex", line=39), find(reviewer="claude", line=41)
    out, skip = panel.adjudicate(panel.cluster_findings([a, b]), "diff", "", 1609)
    assert "claude CLI absent" in skip
    assert [c.verdict for c in out] == ["unjudged", "unjudged"]
    assert [c.reported_by[0].reviewer for c in out] == ["codex", "claude"]
    assert [c.id for c in out] == ["1609-F01", "1609-F02"]


def test_an_unparseable_judge_reply_keeps_every_finding(monkeypatch):
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(panel, "run_cli", lambda *a, **k: ("I have thoughts.", None))
    out, skip = panel.adjudicate(clusters_of(find()), "diff", "", 1)
    assert "unparseable" in skip and [c.verdict for c in out] == ["unjudged"]


def test_a_dead_judge_reports_why_and_suppresses_nothing(monkeypatch):
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(panel, "run_cli", lambda *a, **k: (None, "judge: timed out after 600s"))
    out, skip = panel.adjudicate(clusters_of(find(), find(reviewer="pi")), "diff", "", 1)
    assert skip == "judge: timed out after 600s" and len(out) == 2


def test_no_findings_is_not_a_judge_failure(monkeypatch):
    monkeypatch.setattr(panel.shutil, "which", lambda _: None)
    assert panel.adjudicate([], "diff", "", 1) == ([], None)


# --------------------------------------------------------------- what the judge is shown

def test_the_judge_sees_one_line_per_reviewer_and_the_clustering_as_a_hint():
    """It cannot merge what it was handed already merged — the old listing gave
    it one line per positional bucket, which is why it spotted duplicates it had
    no way to act on."""
    a = find(reviewer="codex", line=39, title="A", detail="detail A")
    b = find(reviewer="claude", line=41, title="B", detail="")
    listing, flat = panel._judge_listing(panel.cluster_findings([a, b]))
    assert flat == [a, b]
    assert "[0] P2 a.py:39 (reported by codex) — A — detail A" in listing
    assert "[1] P2 a.py:41 (reported by claude) — B" in listing
    assert "[0], [1]" in listing and "hint" in listing


def test_a_lone_finding_produces_no_duplicate_hint():
    listing, _ = panel._judge_listing(clusters_of(find()))
    assert "hint" not in listing


def test_the_hint_numbers_match_the_listing_it_annotates():
    """The hint names report ids, so an off-by-one here would point the judge at
    the wrong findings to merge."""
    near = [find(reviewer="codex", file="a.py", line=10),
            find(reviewer="pi", file="a.py", line=12)]
    far = find(reviewer="claude", file="b.py", line=99, sev="P1")
    listing, flat = panel._judge_listing(panel.cluster_findings([*near, far]))
    assert flat[0] is far                       # P1 cluster sorts first
    assert "[1], [2]" in listing


# ------------------------------------------------------------------- the payload shape

def run_panel(monkeypatch, judge_reply, findings, capsys, json_out=False):
    """Drive `run()` with every subprocess stubbed. Returns (report, payload)."""
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: {
        "github": "o/r", "path": "/tmp/r",
        "reviewers": {"codex": {"enabled": True}, "claude": {"enabled": True}},
        "review_panel": {},
    })
    meta = ('{"title":"t","additions":1,"deletions":0,"baseRefName":"main",'
            '"headRefName":"h","headRefOid":"abc"}')
    monkeypatch.setattr(panel, "sh", lambda args, **k: meta if "view" in args else "diff")
    per_reviewer = {"codex": findings[:1], "claude": findings[1:]}
    monkeypatch.setattr(panel, "review_llm",
                        lambda name, *a, **k: (per_reviewer.get(name, []), None, 5))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(panel, "run_cli", lambda *a, **k: (judge_reply, None))
    recorded = {}
    monkeypatch.setattr(panel, "record_run", lambda p: recorded.update(p))
    assert panel.run("r", 1609, post=False, json_out=json_out) == 0
    return capsys.readouterr().out, recorded


def test_json_mode_puts_nothing_but_the_payload_on_stdout(monkeypatch, capsys):
    """It is a machine-readable artifact: a consumer should not have to strip a
    progress preamble before parsing it."""
    reply = '[{"id":"F1","members":[0],"real":true,"reason":"real"}]'
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: {
        "github": "o/r", "path": "/tmp/r",
        "reviewers": {"codex": {"enabled": True}}, "review_panel": {},
    })
    meta = ('{"title":"t","additions":1,"deletions":0,"baseRefName":"main",'
            '"headRefName":"h","headRefOid":"abc"}')
    monkeypatch.setattr(panel, "sh", lambda args, **k: meta if "view" in args else "diff")
    monkeypatch.setattr(panel, "review_llm", lambda *a, **k: ([find()], None, 5))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(panel, "run_cli", lambda *a, **k: (reply, None))
    monkeypatch.setattr(panel, "record_run", lambda p: None)
    panel.run("r", 1609, post=False, json_out=True)
    captured = capsys.readouterr()
    import json as _json
    assert _json.loads(captured.out)["pr"] == 1609
    assert "[r#1609]" in captured.err          # the progress line still shows


def test_a_skipped_pr_still_answers_json_mode(monkeypatch, capsys):
    """Otherwise "reviewed and found nothing" and "never reviewed" are the same
    empty stdout, and the second reads as a clean PR."""
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: {
        "github": "o/r", "path": "/tmp/r", "reviewers": {},
        "review_panel": {"skip_title_patterns": ["^Merge "]},
    })
    meta = ('{"title":"Merge test into main","additions":1,"deletions":0,'
            '"baseRefName":"main","headRefName":"h","headRefOid":"abc"}')
    monkeypatch.setattr(panel, "sh", lambda args, **k: meta)
    assert panel.run("r", 1609, post=False, json_out=True) == 0
    captured = capsys.readouterr()
    import json as _json
    payload = _json.loads(captured.out)
    assert payload["reviewed"] is False and payload["to_fix"] == []
    assert "skip pattern" in payload["skip_reason"]
    assert "Skipping" in captured.err


def test_a_merged_finding_reaches_the_report_with_every_account(monkeypatch, capsys):
    """The report is the PR comment, so the accounts have to be visible there —
    that a second reviewer made a point of its own is the whole payoff."""
    a = find(reviewer="codex", sev="P2", line=213, title="double render",
             detail="the helper calls create_app twice")
    b = find(reviewer="claude", sev="P3", line=44, title="fixture rebuilds", detail="")
    reply = ('[{"id":"F1","members":[0,1],"real":true,"severity":"P2","file":"a.py",'
             '"line":213,"synthesis":"the helper rebuilds the app","reason":"real"}]')
    report, payload = run_panel(monkeypatch, reply, [a, b], capsys)
    assert "### To fix (1)" in report
    assert "[1609-F01] — the helper rebuilds the app _(via codex, claude ⋆consensus)_" in report
    assert "- _codex_ (P2 `a.py:213`): double render — the helper calls create_app twice" in report
    assert "- _claude_ (P3 `a.py:44`): fixture rebuilds" in report
    [rec] = payload["to_fix"]
    assert rec["synthesis"] == "the helper rebuilds the app"
    assert [r["account"] for r in rec["reported_by"]] == [
        "double render — the helper calls create_app twice", "fixture rebuilds"]
    assert payload["judged"] is True and payload["dismissed"] == []


def test_a_solo_finding_is_not_padded_with_its_own_account(monkeypatch, capsys):
    """Nothing was merged, so the synthesis already IS what the reviewer said."""
    reply = '[{"id":"F1","members":[0],"real":true,"reason":"real"}]'
    report, _ = run_panel(monkeypatch, reply, [find(reviewer="codex", title="only me")], capsys)
    assert "only me" in report and "- _codex_ (" not in report


def test_the_json_payload_is_the_canonical_list(monkeypatch, capsys):
    reply = ('[{"id":"F1","members":[0],"real":true,"reason":"real","related":["F2"]},'
             '{"id":"F2","members":[1],"real":false,"reason":"false positive"}]')
    out, payload = run_panel(monkeypatch, reply, [find(), find(reviewer="claude")],
                             capsys, json_out=True)
    import json as _json
    printed = _json.loads(out)
    assert printed == payload                      # what is printed IS what is recorded
    assert printed["to_fix"][0]["related"] == ["1609-F02"]
    assert printed["to_fix"][0]["verdict"] == "confirmed"
    assert printed["dismissed"][0]["verdict"] == "dismissed"
    # Both members are credited, one per record, wherever the panel ordered them.
    credited = {r["reviewer"] for rec in printed["to_fix"] + printed["dismissed"]
                for r in rec["reported_by"]}
    assert credited == {"codex", "claude"}


def test_the_serialised_finding_is_the_canonical_record():
    """One shape for the fix loop, the board and the report — the fixer consumes
    the merge instead of re-deriving it."""
    a = find(reviewer="codex", sev="P2", line=213, title="double render", detail="twice")
    b = find(reviewer="pi", sev="P3", line=44, title="rebuild", detail="")
    [c] = judged([{"id": "F1", "members": [0, 1], "real": True, "severity": "P1",
                   "file": "a.py", "line": 213, "synthesis": "merged", "reason": "why"}],
                 [a, b])
    assert c.as_dict() == {
        "id": "1609-F01",
        "severity": "P1",
        "file": "a.py",
        "line": 213,
        "synthesis": "merged",
        "verdict": "confirmed",
        "reported_by": [
            {"reviewer": "codex", "severity": "P2", "line": 213,
             "account": "double render — twice"},
            {"reviewer": "pi", "severity": "P3", "line": 44, "account": "rebuild"},
        ],
        "reviewers": ["codex", "pi"],
        "related": [],
        "rationale": "why",
    }
