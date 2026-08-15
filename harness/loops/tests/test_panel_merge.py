"""Merging happens once, in the judge, and never at the cost of a reviewer's text.

The panel used to merge findings three times over: a positional key before the
judge, a judge that could see duplicates but had no verb for them, and a fixer
that did the real merging by hand downstream. Only the last one worked, and the
first one was lossy — it kept one finding's text and discarded the rest, so an
observation only one reviewer made survived precisely when the merge FAILED.

So the tests here are mostly about what must NOT be lost: every reporter's own
title, detail, severity and line, any finding the judge did not mention, and any
ruling too malformed to read. Clustering survives only as a hint, and is tested
as one.
"""

import json
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


# ------------------------------------------------------- what a reviewer's reply becomes

def test_a_severity_the_panel_does_not_count_is_normalised_where_it_arrives():
    """A reviewer answering "BLOCKER" sorted before "P1" lexically, so it won the
    representative pick, headed the fix list, and counted in no severity bucket
    on the board. Normalised on the way in, so no comparison downstream has to
    defend itself — including the judge's own fallback to the reviewer's call."""
    parsed, _ = panel.parse_reply("codex", json.dumps([
        {"severity": "BLOCKER", "file": "a.py", "title": "t"},
        {"severity": " p1 ", "file": "a.py", "title": "u"},
        {"file": "a.py", "title": "v"},
    ]))
    assert [f.severity for f in parsed] == ["P3", "P1", "P3"]


def test_an_unreadable_severity_cannot_win_the_representative_pick():
    a = panel.parse_reply("codex", '[{"severity":"BLOCKER","file":"a.py","title":"a"}]')[0][0]
    b = panel.parse_reply("pi", '[{"severity":"P2","file":"a.py","title":"b"}]')[0][0]
    [c] = judged([{"id": "F1", "members": [0, 1]}], [a, b])
    assert (c.severity, c.synthesis) == ("P2", "b")


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


def test_an_account_carries_the_reviewer_s_title_and_detail_as_fields():
    """The joined `account` is a rendering — an em dash is punctuation reviewers
    use, so nobody can split it back apart. The structured pair travels beside
    it, or "kept verbatim" is only true of a string we made up."""
    f = find(reviewer="codex", title="detail — dropped", detail="and — again")
    [c] = judged([{"id": "F1", "members": [0]}], [f])
    [account] = c.as_dict()["reported_by"]
    assert account["title"] == "detail — dropped"
    assert account["detail"] == "and — again"
    assert account["account"] == "detail — dropped — and — again"


def test_two_accounts_from_one_reviewer_are_joined_rather_than_lost_at_ingest():
    """The board stores accounts under a (finding, reviewer) uniqueness
    constraint and keeps the FIRST, so a judge merging two findings from one
    reviewer — the panel's own motivating example, one defect cited at two lines
    — would drop the second silently. Joined here, where nothing is lost."""
    a = find(reviewer="codex", sev="P3", line=100, title="rebuilds the app", detail="at 100")
    b = find(reviewer="codex", sev="P1", line=41, title="fixture is per-test", detail="at 41")
    [c] = judged([{"id": "F1", "members": [0, 1], "real": True}], [a, b])
    [account] = c.as_dict()["reported_by"]        # one entry, not two
    assert account["reviewer"] == "codex"
    assert account["severity"] == "P1"            # the worst it called
    assert account["line"] == 100                 # the first it cited
    for said in ("rebuilds the app", "at 100", "fixture is per-test", "at 41"):
        assert said in account["account"]


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


def test_ids_are_the_pr_number_and_the_finding_s_position_in_the_output():
    out = judged([{"id": "F1", "members": [0]}, {"id": "F2", "members": [1]}],
                 [find(), find(reviewer="pi")], pr=42)
    assert [c.id for c in out] == ["42-F01", "42-F02"]


def test_the_same_defect_gets_a_different_id_on_a_rerun_that_orders_it_differently():
    """The id is positional, so it is not the defect's identity — a rerun where
    one finding is dismissed, merged or simply reported first renumbers it. The
    `key` is what survives that (see below); naming the id "stable" invited a
    consumer to chain runs on a number that moves."""
    a, b = find(reviewer="codex", title="alpha"), find(reviewer="pi", title="beta")
    first = judged([{"id": "F1", "members": [0]}, {"id": "F2", "members": [1]}], [a, b])
    rerun = judged([{"id": "F1", "members": [0]}, {"id": "F2", "members": [1]}], [b, a])
    beta = next(c for c in first if c.synthesis == "beta")
    beta_again = next(c for c in rerun if c.synthesis == "beta")
    assert beta.id != beta_again.id
    assert beta.key == beta_again.key


def test_the_defect_key_is_the_reviewer_s_words_not_the_judge_s_rewording():
    """The board derives a key from file + title when the caller sends none, and
    the title it would use is the judge's freshly-worded synthesis — so a
    re-review of an unfixed defect started a new chain every time. Keyed on the
    reviewers' own titles, the two runs join."""
    f = find(reviewer="codex", file="a.py", title="detail is dropped on serialise")
    [run1] = judged([{"id": "F1", "members": [0], "synthesis": "the serialiser omits detail"}], [f])
    [run2] = judged([{"id": "F1", "members": [0], "synthesis": "detail never reaches the board"}], [f])
    assert run1.synthesis != run2.synthesis
    assert run1.key == run2.key == run1.as_dict()["key"]


def test_the_defect_key_matches_the_board_s_own_derivation():
    """`app/api/reviews.py::_derive_key` (and migration 0012's SQL) must agree
    with this, or a run that sends a key joins no chain with one that didn't."""
    import hashlib
    import re as _re
    title = "detail is dropped on serialise"
    norm = _re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    expected = hashlib.md5(f"a.py|{norm}".encode(),
                           usedforsecurity=False).hexdigest()[:16]
    assert panel._defect_key("a.py", [find(file="a.py", title=title)]) == expected


def test_the_defect_key_does_not_move_with_report_order_or_the_representative():
    """It is the first of the reporters' titles alphabetically, so which reviewer
    the judge picked as representative — and which arrived first — cannot change
    the chain a finding lands in."""
    a = find(reviewer="codex", sev="P1", file="a.py", title="zeta")
    b = find(reviewer="pi", sev="P3", file="a.py", title="alpha")
    assert panel._defect_key("a.py", [a, b]) == panel._defect_key("a.py", [b, a])


def test_related_is_resolved_to_this_run_s_ids_and_dangling_refs_are_dropped():
    """Four findings across four files can be one design decision — not the same
    issue, so not merged, but linked so the fixer decides once."""
    out = judged([{"id": "A", "members": [0], "related": ["B", "ZZ", "A"]},
                  {"id": "B", "members": [1], "related": ["A"]}],
                 [find(), find(reviewer="pi")], pr=7)
    assert out[0].related == ["7-F02"]      # B resolved; ZZ unknown; self dropped
    assert out[1].related == ["7-F01"]


def test_a_reused_judge_id_leaves_its_links_unresolved_rather_than_arbitrary(capsys):
    """The map was a dict comprehension, so a repeated id silently last-wins and
    every link to it points at whichever record happened to be built last — a
    fixer sent to unrelated code. Ambiguous is not a link."""
    out = judged([{"id": "A", "members": [0]},
                  {"id": "A", "members": [1]},
                  {"id": "B", "members": [2], "related": ["A"]}],
                 [find(), find(reviewer="pi"), find(reviewer="claude")], pr=7)
    assert out[2].related == []
    assert "reused issue id(s) A" in capsys.readouterr().err


def test_a_judge_id_of_1_and_of_quoted_1_is_one_identifier(capsys):
    """`str()` coercion makes them the same id — which is what the judge means
    when it writes both — so the clash is caught as the duplicate it looks like
    instead of resolving a link to whichever came last."""
    out = judged([{"id": 1, "members": [0]},
                  {"id": "1", "members": [1]},
                  {"id": "B", "members": [2], "related": [1]}],
                 [find(), find(reviewer="pi"), find(reviewer="claude")], pr=7)
    assert out[2].related == []
    assert "reused issue id(s) 1" in capsys.readouterr().err


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
    assert (c.synthesis, c.detail) == ("b", "d")   # the worst report's own words


# ----------------------------------------------------------- nothing is ever suppressed

def test_a_finding_the_judge_never_mentioned_survives_unjudged():
    a, b = find(reviewer="codex"), find(reviewer="pi", line=400)
    out = judged([{"id": "F1", "members": [0], "real": True, "reason": "yes"}], [a, b])
    assert [c.verdict for c in out] == ["confirmed", "unjudged"]
    assert out[1].rationale == "unjudged"
    assert out[1].reported_by == [b]


def test_an_unmerged_finding_keeps_its_title_out_of_its_detail():
    """The synthesis is stored as the board's `title` and keyed off, so joining
    title and detail into it put a whole body — up to RAW_DETAIL_CHARS of it for
    an unparsed reply — in a title column and in the defect key."""
    body = "a mile of prose. " * 400
    raw = panel._raw_finding("codex", f"the CLI printed prose\n{body}")
    [c] = judged([], [raw])
    assert c.verdict == "unjudged"
    assert c.synthesis == raw.title and len(c.synthesis) <= 80
    assert body[:50] in c.detail
    assert c.as_dict()["detail"] == raw.detail


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


def test_a_malformed_real_flag_never_dismisses_a_finding():
    """`v.get("real", True)` was read for truthiness, so `0`, `""` and `[]` —
    the shapes a malformed reply takes — silently dismissed real findings. That
    is suppression from malformed output, which this module promises never to
    do; an unreadable ruling is no ruling."""
    for junk in (0, "", [], "no", 1):
        [c] = judged([{"id": "F1", "members": [0], "real": junk}], [find()])
        assert c.verdict == "unjudged", junk
    [dismissed] = judged([{"id": "F1", "members": [0], "real": False}], [find()])
    assert dismissed.verdict == "dismissed"
    [absent] = judged([{"id": "F1", "members": [0]}], [find()])
    assert absent.verdict == "confirmed"


def test_report_ids_quoted_as_strings_still_merge():
    """`"members": ["0", "1"]` has said exactly what it meant — dropping those
    would silently un-merge the finding."""
    [c] = judged([{"id": "F1", "members": ["0", "1"]}],
                 [find(reviewer="codex"), find(reviewer="pi")])
    assert c.reviewers == ["codex", "pi"]


def test_report_ids_that_json_parsed_as_floats_still_merge():
    """`2.0` in the judge's JSON is a float in Python, and dropping it would
    un-merge the finding for a difference the model never made."""
    [c] = judged([{"id": "F1", "members": [0.0, 1.0]}],
                 [find(reviewer="codex"), find(reviewer="pi")])
    assert c.reviewers == ["codex", "pi"]


def test_a_non_integral_float_is_not_a_report_id():
    out = judged([{"id": "F1", "members": [0.5]}, {"id": "F2", "members": [0]}], [find()])
    assert len(out) == 1 and out[0].reported_by[0].reviewer == "codex"


def test_a_verdict_whose_members_name_issue_labels_is_reported_not_silently_dropped(capsys):
    """The judge's prompt shows `"id": "F01"` beside `"members"`, which invites
    `members: ["F01"]` — every member is then dropped, the merge is lost, and the
    run reads exactly like one where the judge found no duplicates."""
    out = judged([{"id": "F01", "members": ["F01", "F02"], "real": True}],
                 [find(reviewer="codex"), find(reviewer="pi")])
    assert [c.verdict for c in out] == ["unjudged", "unjudged"]
    assert "F01" in capsys.readouterr().err


def test_one_verdict_listing_a_report_twice_credits_it_once():
    [c] = judged([{"id": "F1", "members": [0, 0]}], [find(reviewer="codex")])
    assert len(c.reported_by) == 1


def test_junk_in_the_judge_reply_is_skipped_not_fatal():
    out = judged(["nonsense", 7, None, {"id": "F1", "members": [0]}], [find()])
    assert len(out) == 1


def test_an_absent_judge_keeps_every_finding_with_its_account(monkeypatch):
    monkeypatch.setattr(panel.shutil, "which", lambda _: None)
    a, b = find(reviewer="codex", line=39), find(reviewer="claude", line=41)
    out, skip, _ = panel.adjudicate(panel.cluster_findings([a, b]), "diff", "", 1609)
    assert "claude CLI absent" in skip
    assert [c.verdict for c in out] == ["unjudged", "unjudged"]
    assert [c.reported_by[0].reviewer for c in out] == ["codex", "claude"]
    assert [c.id for c in out] == ["1609-F01", "1609-F02"]


def test_an_unparseable_judge_reply_keeps_every_finding(monkeypatch):
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(panel, "run_cli", lambda *a, **k: ("I have thoughts.", None))
    out, skip, _ = panel.adjudicate(clusters_of(find()), "diff", "", 1)
    assert "unparseable" in skip and [c.verdict for c in out] == ["unjudged"]


def test_a_dead_judge_reports_why_and_suppresses_nothing(monkeypatch):
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(panel, "run_cli", lambda *a, **k: (None, "judge: timed out after 600s"))
    out, skip, _ = panel.adjudicate(clusters_of(find(), find(reviewer="pi")), "diff", "", 1)
    assert skip == "judge: timed out after 600s" and len(out) == 2


def test_no_findings_is_not_a_judge_failure(monkeypatch):
    monkeypatch.setattr(panel.shutil, "which", lambda _: None)
    assert panel.adjudicate([], "diff", "", 1) == ([], None, "")


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


def test_one_enormous_account_is_cut_rather_than_blowing_the_argv_limit():
    """The whole prompt is ONE argv entry and Linux caps that at 128 KiB, so an
    unparsed reply kept as a finding (RAW_DETAIL_CHARS each) used to be able to
    fail the review outright with E2BIG."""
    huge = find(reviewer="codex", detail="x" * panel.RAW_DETAIL_CHARS)
    listing, flat = panel._judge_listing(clusters_of(huge))
    assert flat == [huge]
    assert len(listing) < panel.LISTING_ACCOUNT_CHARS + 200
    assert "account truncated" in listing


def test_reports_beyond_the_listing_budget_are_omitted_and_said_to_be_kept():
    """`flat` still holds every finding, numbered as the judge sees it, so an
    omitted report is simply never claimed and survives as unjudged."""
    many = [find(reviewer="codex", line=i * 100, title=f"t{i}", detail="y" * 500)
            for i in range(20)]
    listing, flat = panel._judge_listing(panel.cluster_findings(many), budget=2_000)
    assert len(flat) == 20
    assert "further report(s) omitted" in listing
    assert "[19]" not in listing
    assert len(listing) < 2_600


def test_a_cluster_of_empty_groups_is_not_a_judge_failure(monkeypatch):
    """The listing used to be built before the empty check — harmless, but it
    meant the "nothing to judge" answer came from a prompt nobody would send."""
    monkeypatch.setattr(panel.shutil, "which", lambda _: None)
    assert panel.adjudicate([[]], "diff", "", 1) == ([], None, "")


# ------------------------------------------------------------------- the payload shape

def run_panel(monkeypatch, judge_reply, findings, capsys, json_out=False, sonar=()):
    """Drive `run()` with every subprocess stubbed. Returns (report, payload)."""
    reviewers = {"codex": {"enabled": True}, "claude": {"enabled": True}}
    if sonar:
        reviewers["sonarqube"] = {"enabled": True}
        monkeypatch.setattr(panel, "review_sonarqube",
                            lambda *a, **k: ("OK", list(sonar), [], None))
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: {
        "github": "o/r", "path": "/tmp/r",
        "reviewers": reviewers,
        "review_panel": {},
    })
    meta = ('{"title":"t","additions":1,"deletions":0,"baseRefName":"main",'
            '"headRefName":"h","headRefOid":"abc"}')
    monkeypatch.setattr(panel, "sh", lambda args, **k: meta if "view" in args else "diff")
    per_reviewer = {"codex": findings[:1], "claude": findings[1:]}
    monkeypatch.setattr(panel, "review_llm",
                        lambda name, *a, **k: panel.ReviewerRun(
                            per_reviewer.get(name, []), None, 5))
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
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([find()], None, 5))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(panel, "run_cli", lambda *a, **k: (reply, None))
    monkeypatch.setattr(panel, "record_run", lambda p: None)
    panel.run("r", 1609, post=False, json_out=True)
    captured = capsys.readouterr()
    assert json.loads(captured.out)["pr"] == 1609
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
    payload = json.loads(captured.out)
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
    printed = json.loads(out)
    assert printed == payload                      # what is printed IS what is recorded
    assert printed["to_fix"][0]["related"] == ["1609-F02"]
    assert printed["to_fix"][0]["verdict"] == "confirmed"
    assert printed["dismissed"][0]["verdict"] == "dismissed"
    # Both members are credited, one per record, wherever the panel ordered them.
    credited = {r["reviewer"] for rec in printed["to_fix"] + printed["dismissed"]
                for r in rec["reported_by"]}
    assert credited == {"codex", "claude"}


def test_a_skipped_pr_answers_with_the_same_payload_SHAPE_as_a_reviewed_one(
        monkeypatch, capsys):
    """The skip payload was a hand-written nine-key literal against a two-dozen
    key one, so a consumer reading `payload['judged']` or `['run_key']` raised
    KeyError on exactly the PR the payload exists for."""
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: {
        "github": "o/r", "path": "/tmp/r", "reviewers": {},
        "review_panel": {"skip_title_patterns": ["^Merge "]},
    })
    meta = ('{"title":"Merge test into main","additions":1,"deletions":0,'
            '"baseRefName":"main","headRefName":"h","headRefOid":"abc"}')
    monkeypatch.setattr(panel, "sh", lambda args, **k: meta)
    assert panel.run("r", 1609, post=False, json_out=True) == 0
    skipped = json.loads(capsys.readouterr().out)

    reply = '[{"id":"F1","members":[0],"real":true,"reason":"real"}]'
    out, _ = run_panel(monkeypatch, reply, [find()], capsys, json_out=True)
    reviewed = json.loads(out)

    assert set(skipped) == set(reviewed)
    assert skipped["judged"] is False and reviewed["judged"] is True
    assert skipped["run_key"] and skipped["skip_reason"]
    assert reviewed["skip_reason"] is None


def test_sonar_gate_issues_become_canonical_records_of_their_own(monkeypatch, capsys):
    """They never reach the judge, so this is the one record built outside
    `_parse_verdicts` — and its ids are offset past the judged findings because
    `related` is resolved against ids that must be unique across the payload."""
    hard = [F(reviewer="sonarqube", severity="P1", file="a.py", line=7,
              title="null deref", detail="python:S2259"),
            F(reviewer="sonarqube", severity="P3", file="b.py", line=None,
              title="unused import", detail="python:S1128")]
    reply = '[{"id":"F1","members":[0],"real":true,"reason":"real"}]'
    report, payload = run_panel(monkeypatch, reply, [find(reviewer="codex")],
                                capsys, sonar=hard)
    judged_ids = [c["id"] for c in payload["to_fix"] + payload["dismissed"]]
    sonar_recs = payload["sonar_findings"]
    assert judged_ids == ["1609-F01"]
    assert [c["id"] for c in sonar_recs] == ["1609-F02", "1609-F03"]
    assert len(set(judged_ids) | {c["id"] for c in sonar_recs}) == 3
    assert [c["verdict"] for c in sonar_recs] == ["sonar", "sonar"]
    assert sonar_recs[0]["synthesis"] == "null deref"      # the message, not the rule
    assert sonar_recs[0]["rationale"] == "python:S2259"    # the rule that fired
    assert sonar_recs[0]["reported_by"][0]["reviewer"] == "sonarqube"
    assert sonar_recs[0]["key"] != sonar_recs[1]["key"]
    assert "### SonarCloud issues (2)" in report


def test_a_finding_the_master_never_ruled_on_says_so_in_the_report(monkeypatch, capsys):
    """It has no rationale, so under a header naming the judge it rendered
    identically to an adjudicated one — the payload has carried a per-finding
    verdict since this change, and the report is where a human reads it."""
    reply = '[{"id":"F1","members":[0],"real":true,"reason":"real"}]'
    report, payload = run_panel(monkeypatch, reply,
                                [find(reviewer="codex", title="one finding"),
                                 find(reviewer="claude", title="another finding")], capsys)

    def line_for(record):
        return next(ln for ln in report.splitlines()
                    if ln.startswith("- **") and record["id"] in ln)

    by_verdict = {c["verdict"]: c for c in payload["to_fix"]}
    assert set(by_verdict) == {"confirmed", "unjudged"}
    assert "unjudged" in line_for(by_verdict["unjudged"])
    assert "unjudged" not in line_for(by_verdict["confirmed"])


def test_a_report_too_big_for_a_github_comment_is_cut_to_fit():
    """`gh pr comment` fails outright over 65,536 chars, which would lose an
    otherwise successful review — and the per-reviewer accounts, one block per
    reporter per merged finding, are what grows without bound."""
    head = "## Reviewer panel — PR #1\n\n### To fix (2)\n"
    findings = "".join(f"- **P2** `a.py:{i}` [1-F0{i}] — issue {i}\n"
                       f"  - _codex_ (P2 `a.py:{i}`): {'x' * 40_000}\n" for i in (1, 2))
    report = head + findings
    fitted = panel.fit_comment(report)
    assert len(fitted) <= panel.COMMENT_CHARS
    assert "issue 1" in fitted and "issue 2" in fitted      # verdicts survive
    assert "accounts omitted" in fitted

    huge = head + "\n".join(f"- **P2** `a.py:{i}` — issue {i}" for i in range(5_000))
    cut = panel.fit_comment(huge)
    assert len(cut) <= panel.COMMENT_CHARS and "truncated" in cut

    small = head + "- **P2** `a.py:1` — issue 1\n  - _codex_ (P2 `a.py:1`): said\n"
    assert panel.fit_comment(small) == small               # untouched under the limit


def test_the_serialised_finding_is_the_canonical_record():
    """One shape for the fix loop, the board and the report — every consumer
    reads the merge instead of re-deriving it."""
    a = find(reviewer="codex", sev="P2", line=213, title="double render", detail="twice")
    b = find(reviewer="pi", sev="P3", line=44, title="rebuild", detail="")
    [c] = judged([{"id": "F1", "members": [0, 1], "real": True, "severity": "P1",
                   "file": "a.py", "line": 213, "synthesis": "merged", "reason": "why"}],
                 [a, b])
    assert c.as_dict() == {
        "id": "1609-F01",
        "key": panel._defect_key("a.py", [a, b]),
        "severity": "P1",
        "file": "a.py",
        "line": 213,
        "synthesis": "merged",
        "detail": "twice",
        "verdict": "confirmed",
        "reported_by": [
            {"reviewer": "codex", "severity": "P2", "line": 213,
             "title": "double render", "detail": "twice",
             "account": "double render — twice", "needs_rereview": False},
            {"reviewer": "pi", "severity": "P3", "line": 44,
             "title": "rebuild", "detail": "", "account": "rebuild",
             "needs_rereview": False},
        ],
        "reviewers": ["codex", "pi"],
        "needs_rereview": False,
        "rereview_by": [],
        "related": [],
        "rationale": "why",
    }
