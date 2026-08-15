"""v2.14: rounds, and what a run could not see.

Two runs of one PR used to be two unrelated records — nothing said which was the
re-review of the other's fix, what this round found that the last had not, or
what stopped the loop. And a run reported only what was *found*: a reviewer given
a prefix of the diff, one that never ran, and one with nothing to say all wrote
the same zero into the record.

These tests pin what makes a review reviewable: the round metadata survives the
round trip, a declaration is attributed to the member that made it (not to
everyone who happened to raise the same finding), "never asked" stays distinct
from "nothing to declare", and a re-review flag is checked against what the
following round actually found rather than taken on trust.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.api.reviews import _derive_key

from .conftest import LAPTOP

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness" / "loops"))
import panel

REPO = "acme/v214repo"
AGENT = {**LAPTOP, "X-Agent-Instance": "d14d14"}


def payload(pr: int, **over) -> dict:
    body = {
        "repo": REPO,
        "pr": pr,
        "judged": True,
        "judge_model": "opus",
        "reviewers_selected": ["claude", "codex"],
        "reviewers": {
            "claude": {"model": "sonnet", "ran": True, "could_not_assess": []},
            "codex": {"model": "gpt-5.6", "ran": True, "truncated": True,
                      "max_diff_chars": 60_000,
                      "could_not_assess": ["the migration, which the diff omits"]},
        },
        "round": 1,
        "new_findings": 1,
        "round_stop": {"stop": False, "reason": "1 finding(s) no earlier round raised",
                       "confident": False, "veto": ["codex saw 60,000 of 118,402 diff chars"]},
        "coverage_note": "codex is right that the migration is unread",
        "to_fix": [{
            "severity": "P2", "file": "app/sync.py", "line": 40,
            "title": "half-stale node after the early return",
            "reviewers": ["claude", "codex"],
            "reason": "real",
            "needs_rereview": True,
            "rereview_by": ["codex"],
            "new_this_round": True,
        }],
        "dismissed": [],
        "sonar_findings": [],
    }
    return {**body, **over}


async def record(client, pr: int, **over) -> int:
    r = await client.post("/review", json=payload(pr, **over), headers=AGENT)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def detail(client, run_id: int) -> dict:
    r = await client.get(f"/review/{run_id}", headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


def card(run: dict, name: str) -> dict:
    return next(c for c in run["reviewers"] if c["name"] == name)


# ---- the round survives the round trip -------------------------------------

async def test_a_run_records_where_it_sat_in_the_cycle(client):
    run = await detail(client, await record(client, 6100))
    assert run["round"] == 1
    assert run["new_findings"] == 1
    assert run["stop_reason"] == "1 finding(s) no earlier round raised"
    assert run["stop_confident"] is False
    assert run["coverage_note"] == "codex is right that the migration is unread"


async def test_a_stop_that_was_not_convergence_is_recorded_as_such(client):
    """The whole point of the column: a counter reading zero while a reviewer read
    half the diff is a fact about the panel, not about the code."""
    run = await detail(client, await record(
        client, 6101, round=2, new_findings=0,
        round_stop={"stop": True, "reason": "dry — nothing raised that an earlier round had not",
                    "confident": False, "veto": ["codex saw 60,000 of 118,402 diff chars"]}))
    assert run["round"] == 2 and run["stop_reason"].startswith("dry")
    assert run["stop_confident"] is False


async def test_an_older_payload_is_a_first_round_that_declared_nothing(client):
    """Recorded exactly as before: round 1, and NULL rather than zero everywhere
    the panel was never asked — a pre-v2.14 run must not read as earned-clean."""
    body = {k: v for k, v in payload(6102).items()
            if k not in ("round", "new_findings", "round_stop", "coverage_note")}
    body["reviewers"] = {"claude": {"model": "sonnet", "ran": True}}
    body["to_fix"] = [{"severity": "P3", "file": "a.py", "title": "x",
                       "reviewers": ["claude"], "reason": "real"}]
    r = await client.post("/review", json=body, headers=AGENT)
    assert r.status_code == 201
    run = await detail(client, r.json()["id"])
    assert run["round"] == 1
    assert run["new_findings"] is None and run["stop_reason"] is None
    assert run["stop_confident"] is None and run["coverage_note"] is None
    assert card(run, "claude")["could_not_assess"] is None
    assert run["findings"][0]["needs_rereview"] is False
    assert run["findings"][0]["new_this_round"] is None


async def test_a_flat_stop_reason_is_accepted_without_the_nested_verdict(client):
    run = await detail(client, await record(
        client, 6103, round_stop=None, stop_reason="round cap (2) reached"))
    assert run["stop_reason"] == "round cap (2) reached"
    # Nothing claimed about confidence — the flat field cannot carry it.
    assert run["stop_confident"] is None


# ---- coverage declarations -------------------------------------------------

async def test_nothing_to_declare_is_not_the_same_as_never_asked(client):
    """A finding count reports "clean" and "I could not tell" as the same zero.
    This is the column that separates them, so its own empty states must not
    collapse either."""
    run = await detail(client, await record(client, 6110))
    assert card(run, "claude")["could_not_assess"] == []
    assert card(run, "codex")["could_not_assess"] == ["the migration, which the diff omits"]


async def test_truncation_is_visible_on_the_row_it_affected(client):
    run = await detail(client, await record(client, 6111))
    codex = card(run, "codex")
    assert codex["truncated"] is True and codex["max_diff_chars"] == 60_000
    assert card(run, "claude")["truncated"] is None


# ---- the re-review declaration, and who made it ----------------------------

async def test_a_flag_is_credited_to_the_member_that_made_it(client):
    """Not to everyone who raised the finding: the declaration's accuracy is per
    reviewer, and crediting the group makes the honest member and the quiet one
    indistinguishable on exactly the statistic that separates them."""
    run = await detail(client, await record(client, 6120))
    assert card(run, "codex")["rereview_flagged"] == 1
    assert card(run, "claude")["rereview_flagged"] == 0
    assert run["findings"][0]["needs_rereview"] is True


async def test_a_reporters_own_flag_wins_over_the_panels_attribution(client):
    """``reported_by`` is the finer grain — where it carries flags, they are the
    record, and the coarser ``rereview_by`` is not consulted."""
    run = await detail(client, await record(client, 6121, to_fix=[{
        "severity": "P1", "file": "app/db.py", "title": "session leak",
        "reason": "real",
        "rereview_by": ["claude"],
        "reported_by": [
            {"reviewer": "claude", "severity": "P1", "account": "leaks on the error path"},
            {"reviewer": "codex", "severity": "P2", "account": "same, plus the retry",
             "needs_rereview": True},
        ],
    }]))
    assert card(run, "codex")["rereview_flagged"] == 1
    assert card(run, "claude")["rereview_flagged"] == 0
    flags = {r["reviewer"]: r["needs_rereview"] for r in run["findings"][0]["reported_by"]}
    assert flags == {"claude": False, "codex": True}


async def test_a_reporters_silence_is_not_treated_as_missing_data(client):
    """A member that sent an account is authoritative about itself, including its
    `false`. Filling that in from the coarser list would manufacture a
    declaration it did not make — and honesty per reviewer is exactly the
    statistic that ruins."""
    run = await detail(client, await record(client, 6123, to_fix=[{
        "severity": "P2", "file": "a.py", "title": "structural", "reason": "real",
        "reviewers": ["claude", "codex"],
        "rereview_by": ["claude", "codex"],
        "reported_by": [
            {"reviewer": "claude", "account": "not structural at all",
             "needs_rereview": False},
        ],
    }]))
    assert card(run, "claude")["rereview_flagged"] == 0
    # codex sent no account, so the panel's attribution still speaks for it.
    assert card(run, "codex")["rereview_flagged"] == 1


async def test_an_unattributed_flag_credits_everyone_rather_than_vanishing(client):
    """A panel that merges before it can send per-reporter accounts still made the
    declaration. Over-crediting is visible and correctable; dropping it is not."""
    run = await detail(client, await record(client, 6122, to_fix=[{
        "severity": "P2", "file": "a.py", "title": "structural", "reason": "real",
        "reviewers": ["claude", "codex"], "needs_rereview": True,
    }]))
    assert card(run, "claude")["rereview_flagged"] == 1
    assert card(run, "codex")["rereview_flagged"] == 1


# ---- the accuracy check on the declaration ---------------------------------

async def _two_rounds(client, pr: int, second_file: str):
    """Round 1 flags app/sync.py for re-reading; round 2 raises something new in
    `second_file`."""
    await record(client, pr)
    await record(client, pr, round=2, new_findings=1,
                 round_stop={"stop": False, "reason": "1 finding(s) no earlier round raised",
                             "confident": False, "veto": []},
                 to_fix=[{"severity": "P2", "file": second_file,
                          "title": "dual-keyed node the mirror created",
                          "reviewers": ["claude"], "reason": "real",
                          "new_this_round": True}])
    r = await client.get(f"/review/findings?repo={REPO}&pr={pr}", headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


async def test_a_flag_the_next_round_vindicated_is_recorded_as_a_hit(client):
    """The declaration that would have predicted the round the workflow never
    ran: a structural fix in app/sync.py, and the round after it finds a defect
    there that did not exist until the fix was written."""
    h = await _two_rounds(client, 6130, "app/sync.py")
    first, second = h["runs"]
    assert first["rereview_flagged"] == 1 and first["rereview_hit"] is True
    assert second["round"] == 2 and second["rereview_flagged"] == 0
    # The last round's stop is the PR's stop.
    assert h["stopped"].startswith("1 finding") and h["stop_confident"] is False


async def test_a_flag_nothing_followed_up_on_is_recorded_as_a_miss(client):
    """A wrong declaration is data too — honesty per reviewer needs the misses as
    much as the hits, and the declarer cannot mark its own homework."""
    h = await _two_rounds(client, 6131, "app/other.py")
    assert h["runs"][0]["rereview_hit"] is False


async def test_a_finding_the_judge_threw_out_is_not_the_flag_being_borne_out(client):
    """`rereview_hit` is the accuracy check on a declaration the declarer cannot
    mark itself. Letting a false positive the judge dismissed count as the flagged
    fix having gone wrong is the one thing that makes the number uninformative."""
    await record(client, 6134)
    await record(client, 6134, round=2, new_findings=0, to_fix=[],
                 dismissed=[{"severity": "P3", "file": "app/sync.py",
                             "title": "the mirror is redundant", "reviewers": ["claude"],
                             "reason": "not a defect"}])
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6134", headers=AGENT)).json()
    assert h["runs"][0]["rereview_flagged"] == 1
    assert h["runs"][0]["rereview_hit"] is False


async def test_a_later_cycle_is_not_the_answer_to_an_earlier_rounds_flag(client):
    """A standalone `/panel` read, or a new cycle restarting at round 1, lands in
    the next slot by position. Crediting it as the re-review of the earlier round
    attributes one cycle's findings to another cycle's declaration, and this
    number is presented as an honesty measure."""
    await record(client, 6135)                      # round 1, flags app/sync.py
    await record(client, 6135, round=1, new_findings=1,
                 to_fix=[{"severity": "P2", "file": "app/sync.py",
                          "title": "a wholly separate review of the same file",
                          "reviewers": ["claude"], "reason": "real"}])
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6135", headers=AGENT)).json()
    assert [r["round"] for r in h["runs"]] == [1, 1]
    assert h["runs"][0]["rereview_flagged"] == 1
    # A round 1 is nobody's round 2 — unanswered, not vindicated.
    assert h["runs"][0]["rereview_hit"] is None


async def test_a_flag_with_no_round_after_it_is_unanswered_not_wrong(client):
    """None, not False: nobody looked. Scoring an unrun round as a miss would
    punish the reviewer for the workflow stopping."""
    await record(client, 6132)
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6132", headers=AGENT)).json()
    assert h["runs"][0]["rereview_flagged"] == 1
    assert h["runs"][0]["rereview_hit"] is None


async def test_a_chain_carries_the_declaration_it_was_given(client):
    h = await _two_rounds(client, 6133, "app/sync.py")
    flagged = [c for c in h["findings"] if c["needs_rereview"]]
    assert [c["file"] for c in flagged] == ["app/sync.py"]


async def test_a_flag_naming_only_unknown_members_credits_someone(client):
    """`rereview_by: ["gemini"]` on a finding credited to codex — a retired member,
    a typo, a reviewer merged out. The filtered attribution comes back empty, and
    the fallback used to be skipped because `rereview_by` was non-empty: the flag
    was stored with nobody credited and nothing tallied, which is exactly the
    silent drop the fallback exists to prevent."""
    run = await detail(client, await record(client, 6124, to_fix=[{
        "severity": "P2", "file": "a.py", "title": "structural", "reason": "real",
        "reviewers": ["codex"], "needs_rereview": True, "rereview_by": ["gemini"],
    }]))
    assert run["findings"][0]["needs_rereview"] is True
    assert card(run, "codex")["rereview_flagged"] == 1


# ---- best-effort ingest ----------------------------------------------------

async def test_a_garbled_round_costs_the_number_not_the_whole_record(client):
    """This module's rule is that a review must never fail because the board
    choked (see `_line_or_none`). A `round: 0` or `new_findings: -1` from a
    hand-rolled caller used to 422 the payload, losing the findings, the
    scorecards and the accounts along with the bad integer."""
    r = await client.post("/review", json=payload(6150, round=0, new_findings=-1),
                          headers=AGENT)
    assert r.status_code == 201, r.text
    run = await detail(client, r.json()["id"])
    assert run["round"] == 1               # rounds are numbered from 1
    assert run["new_findings"] is None     # "the panel did not say", not "none"
    assert len(run["findings"]) == 1       # ...and the review itself survived


# ---- the stats side --------------------------------------------------------

async def test_coverage_counters_reach_the_leaderboard(client):
    await record(client, 6140)
    s = (await client.get(f"/review/stats?repo={REPO}", headers=AGENT)).json()
    # Keyed by (reviewer, model) like the leaderboard itself groups: the same
    # vendor at two tiers is two competitors, and a run that recorded no model
    # for it is a third row rather than the same one.
    rows = {(m["reviewer"], m["model"]): m for m in s["by_model"]}
    codex = rows[("codex", "gpt-5.6")]
    assert codex["truncated_runs"] >= 1
    assert codex["declared_gaps_runs"] >= 1
    assert codex["rereview_flagged"] >= 1
    # claude declared [] — asked, nothing to say — which is not a declared gap.
    claude = rows[("claude", "sonnet")]
    assert claude["declared_gaps_runs"] == 0
    assert claude["truncated_runs"] == 0


# ---- the two halves agree on what a defect IS ------------------------------

def test_the_panel_and_the_board_derive_the_same_defect_key():
    """The panel now sends `key` so the local round-over-round diff and the
    board's cross-run chains are provably the same identity. They are two
    implementations of one recipe (a third lives in migration 0012's SQL), so
    drift between them is silent: the round diff would say "new" about a finding
    the chain says is old, and only one of the two is on screen."""
    for file, title in (("app/sync.py", "half-stale node after the early return"),
                        ("a.py", "Unicode dash — survives the strip!"),
                        (None, ""),
                        ("x.py", "   spaced   out   ")):
        assert panel.finding_key(file, title) == _derive_key(file, title)


# ---- the two halves, end to end --------------------------------------------

PANEL_CFG = {
    "github": "acme/e2e",
    "path": "/tmp/acme-e2e",
    "reviewers": {"claude": {"enabled": True, "model": "sonnet"},
                  "codex": {"enabled": True, "model": "gpt-5.6", "max_diff_chars": 40}},
    "review_panel": {},
}
DIFF = "diff --git a/app/sync.py b/app/sync.py\n@@ -1,1 +1,2 @@\n+mirror = {}\n" + "x" * 200


def _fake_sh(args, **kw):
    if args[:3] == ["gh", "pr", "view"]:
        return json.dumps({"title": "feat: mirror", "additions": 20, "deletions": 2,
                           "baseRefName": "main", "headRefName": "feat/x",
                           "headRefOid": "abc123"})
    return DIFF


def _panel_round(monkeypatch, tmp_path, round_no, title, baseline=()):
    """One panel run with every process it would spawn replaced — the reviewers,
    the judge, the CI check — so what is under test is the payload the panel
    builds, not the CLIs."""
    def fake_review(name, model, prompt, effort=""):
        if name == "codex":
            return panel.ReviewerRun(
                [panel.Finding("codex", "P2", "app/sync.py", 12, title,
                               "detail", needs_rereview=True)],
                None, 900, ["the migration, which the diff omits"])
        # claude answered in the old bare-array shape: it declared NOTHING, which
        # is None all the way to the column — not [], which would say it was asked
        # and had no gap.
        return panel.ReviewerRun([], None, 800, None)

    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: PANEL_CFG)
    monkeypatch.setattr(panel, "sh", _fake_sh)
    monkeypatch.setattr(panel, "review_llm", fake_review)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "judge", lambda *a, **k: (
        {0: {"id": 0, "real": True, "severity": "P2", "reason": "real"}},
        None, "codex is right that the migration is unread"))
    out = tmp_path / f"r{round_no}.json"
    assert panel.run("e2e", 77, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline), max_rounds=2) == 0
    return str(out), json.loads(out.read_text())


async def test_a_real_panel_payload_records_and_reads_back(client, monkeypatch, tmp_path):
    """The board takes `panel.py --json` as-is, so the two can only stay in step if
    something exercises the actual payload. A renamed field otherwise fails
    silently into a NULL column — the run records, nothing errors, and the column
    the whole release exists for is empty."""
    r1_path, r1 = _panel_round(monkeypatch, tmp_path, 1, "half-stale node")
    assert r1["round"] == 1 and r1["new_findings"] == 1
    assert r1["round_stop"]["stop"] is False
    # codex's budget (40 chars) cut a longer diff: measured, not declared.
    assert r1["reviewers"]["codex"]["truncated"] is True
    assert r1["reviewers"]["codex"]["could_not_assess"] == \
        ["the migration, which the diff omits"]
    assert r1["reviewers"]["claude"]["could_not_assess"] is None
    assert r1["to_fix"][0]["needs_rereview"] is True
    assert r1["to_fix"][0]["rereview_by"] == ["codex"]

    posted = await client.post("/review", json=r1, headers=AGENT)
    assert posted.status_code == 201, posted.text
    run = await detail(client, posted.json()["id"])
    assert run["round"] == 1 and run["new_findings"] == 1
    assert run["coverage_note"] == "codex is right that the migration is unread"
    assert card(run, "codex")["could_not_assess"] == ["the migration, which the diff omits"]
    assert card(run, "claude")["could_not_assess"] is None
    assert card(run, "codex")["rereview_flagged"] == 1
    assert run["findings"][0]["new_this_round"] is True

    # Round 2 raises the SAME finding again, against round 1 as its baseline.
    _, r2 = _panel_round(monkeypatch, tmp_path, 2, "half-stale node", baseline=[r1_path])
    assert r2["new_findings"] == 0
    assert r2["to_fix"][0]["new_this_round"] is False
    # ...but a P2 is still confirmed, so the cycle is not done — and at the cap
    # that is recorded as running out, not as convergence.
    assert r2["round_stop"]["stop"] is True and r2["round_stop"]["confident"] is False
    assert "round cap (2)" in r2["round_stop"]["reason"]

    assert (await client.post("/review", json=r2, headers=AGENT)).status_code == 201
    h = (await client.get("/review/findings?repo=acme/e2e&pr=77", headers=AGENT)).json()
    assert h["rounds"] == 2
    # One defect, two observations — not two defects.
    assert len(h["findings"]) == 1 and h["findings"][0]["runs_seen"] == 2
    assert h["stop_confident"] is False
