"""v2.10: reviewer-panel stats — recording runs and aggregating them by model.

The panel reviews one diff with several models and has a judge rule each finding
real or not, which is a controlled comparison that used to evaporate when the
process exited. These tests pin the parts that make the accumulated numbers
trustworthy: server-derived scorecards (so a scorecard can't contradict its
findings), precision counted only over adjudicated findings, solo attribution,
and idempotent ingest.
"""

from __future__ import annotations

import pytest

from .conftest import LAPTOP, SERVER

REPO = "acme/v210repo"
AGENT_A = {**LAPTOP, "X-Agent-Instance": "a1b2c3"}
AGENT_B = {**SERVER, "X-Agent-Instance": "d4e5f6"}


def payload(pr: int, **over) -> dict:
    """A judged two-vendor run: one consensus hit, one solo hit, one dismissal."""
    body = {
        "repo": REPO,
        "pr": pr,
        "pr_title": f"feat: thing {pr}",
        "base": "main",
        "changed_lines": 120,
        "diff_chars": 8000,
        "diff_truncated": False,
        "judged": True,
        "judge_model": "opus",
        "ci_status": "PASS",
        "sonar_gate": "OK",
        "reviewers_selected": ["claude", "codex"],
        "reviewers": {
            "claude": {"model": "sonnet", "ran": True},
            "codex": {"model": "gpt-5.6-luna", "effort": "max", "ran": True},
        },
        "to_fix": [
            {"severity": "P1", "file": "app/x.py", "line": 4, "title": "off-by-one",
             "reviewers": ["claude", "codex"], "reason": "confirmed in diff"},
            {"severity": "P3", "file": "app/y.py", "title": "unused import",
             "reviewers": ["codex"], "reason": "confirmed"},
        ],
        "dismissed": [
            {"severity": "P2", "file": "app/z.py", "title": "not a bug",
             "reviewers": ["claude"], "reason": "misreads the guard above"},
        ],
        "sonar_findings": [
            {"severity": "P2", "file": "app/x.py", "title": "cognitive complexity",
             "reviewers": ["sonarqube"]},
        ],
    }
    return {**body, **over}


async def record(client, pr: int, headers=AGENT_A, **over) -> dict:
    r = await client.post("/review", json=payload(pr, **over), headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


# ---- ingest ----------------------------------------------------------------

async def test_scorecards_are_derived_from_the_findings(client):
    """Counts come from the attributions, never from what the client asserts."""
    run = await record(client, 8101)
    detail = (await client.get(f"/review/{run['id']}", headers=AGENT_A)).json()
    cards = {c["name"]: c for c in detail["reviewers"]}

    # claude: the consensus P1 (confirmed) + the dismissal. No solo credit — the
    # only finding it got right was also found by codex.
    assert cards["claude"]["raised"] == 2
    assert cards["claude"]["confirmed"] == 1
    assert cards["claude"]["dismissed"] == 1
    assert cards["claude"]["solo"] == 0
    assert cards["claude"]["p1"] == 1

    # codex: both confirmed, one of which nobody else saw.
    assert cards["codex"]["confirmed"] == 2
    assert cards["codex"]["solo"] == 1
    assert cards["codex"]["dismissed"] == 0
    assert cards["codex"]["model"] == "gpt-5.6-luna"
    assert cards["codex"]["effort"] == "max"


async def test_sonar_hard_gate_is_not_scored_as_a_panel_member(client):
    """Gate issues never faced the judge, so they can't earn a precision."""
    run = await record(client, 8102)
    detail = (await client.get(f"/review/{run['id']}", headers=AGENT_A)).json()
    assert detail["sonar"] == 1
    assert "sonarqube" not in {c["name"] for c in detail["reviewers"]}
    assert any(f["verdict"] == "sonar" for f in detail["findings"])


async def test_unjudged_run_keeps_findings_out_of_precision(client):
    """The panel never suppresses on a missing verdict, so an unjudged finding
    is 'kept', not 'confirmed' — counting it as correct would flatter whichever
    reviewer was noisiest that day."""
    await record(
        client, 8103,
        judged=False,
        judge_skip="judge CLI absent",
        to_fix=[{"severity": "P2", "file": "a.py", "title": "maybe",
                 "reviewers": ["claude"], "reason": "unjudged"}],
        dismissed=[],
        sonar_findings=[],
    )
    stats = (await client.get(
        f"/review/stats?repo={REPO}&judged_only=false", headers=AGENT_A)).json()
    claude = next(m for m in stats["by_model"] if m["reviewer"] == "claude")
    assert claude["unjudged"] >= 1
    # The unjudged finding lands in neither side of the ratio.
    assert claude["confirmed"] + claude["dismissed"] == claude["raised"] - claude["unjudged"]


async def test_run_key_makes_recording_idempotent(client):
    """A panel that records, times out on the response and retries must not
    double-count itself into the stats."""
    first = await record(client, 8104, run_key="v210-retry")
    again = await client.post("/review", json=payload(8104, run_key="v210-retry"),
                              headers=AGENT_A)
    assert again.status_code == 201
    assert again.json() == {"id": first["id"], "recorded": False,
                            "reason": "duplicate run_key"}


async def test_missing_reviewers_block_infers_membership_from_skipped(client):
    """An older panel sends no per-member config. A quiet reviewer must not be
    filed as broken — only one named in `skipped` did not run."""
    run = await record(
        client, 8105,
        reviewers={},
        reviewers_selected=["claude", "codex", "gemini"],
        skipped=["gemini: CLI absent"],
    )
    cards = {c["name"]: c for c in
             (await client.get(f"/review/{run['id']}", headers=AGENT_A)).json()["reviewers"]}
    assert cards["claude"]["ran"] is True
    assert cards["gemini"]["ran"] is False
    assert cards["gemini"]["skip_reason"] == "gemini: CLI absent"
    assert cards["claude"]["model"] is None  # not recorded, not invented


# ---- aggregation -----------------------------------------------------------

async def test_stats_group_by_model_and_effort(client):
    """The same vendor at two tiers is two competitors — that IS the question."""
    await record(client, 8110, reviewers={
        "codex": {"model": "gpt-5.6-luna", "effort": "low", "ran": True},
        "claude": {"model": "sonnet", "ran": True},
    })
    stats = (await client.get(f"/review/stats?repo={REPO}", headers=AGENT_A)).json()
    codex_tiers = {(m["model"], m["effort"]) for m in stats["by_model"]
                   if m["reviewer"] == "codex"}
    assert ("gpt-5.6-luna", "max") in codex_tiers
    assert ("gpt-5.6-luna", "low") in codex_tiers


async def test_precision_is_none_not_zero_when_nothing_was_ruled_on(client):
    """'The judge never ruled on anything it raised' is a different statement
    from 'everything it raised was wrong'."""
    await record(
        client, 8111,
        reviewers={"gemini": {"model": "gemini-3.7-flash", "ran": True}},
        reviewers_selected=["gemini"],
        to_fix=[], dismissed=[], sonar_findings=[],
    )
    stats = (await client.get(f"/review/stats?repo={REPO}", headers=AGENT_A)).json()
    gemini = next(m for m in stats["by_model"] if m["reviewer"] == "gemini")
    assert gemini["precision"] is None


async def test_judged_only_is_the_default_window(client):
    """Precision over unjudged runs is not a ratio, so the default excludes them."""
    await record(client, 8120, judged=False, judge_skip="skipped")
    judged = (await client.get(f"/review/stats?repo={REPO}", headers=AGENT_A)).json()
    everything = (await client.get(
        f"/review/stats?repo={REPO}&judged_only=false", headers=AGENT_A)).json()
    assert judged["window"]["judged_only"] is True
    assert everything["runs"] > judged["runs"]


async def test_by_agent_attributes_runs_to_the_agent_that_ordered_them(client):
    """`author` is machine/instance, so two agents on one box stay distinct."""
    await record(client, 8130, headers=AGENT_A)
    await record(client, 8131, headers=AGENT_B)
    stats = (await client.get(f"/review/stats?repo={REPO}", headers=AGENT_A)).json()
    authors = {a["author"] for a in stats["by_agent"]}
    assert "laptop/a1b2c3" in authors
    assert "server/d4e5f6" in authors


async def test_reviews_list_filters_and_carries_scorecards(client):
    await record(client, 8140)
    rows = (await client.get(f"/reviews?repo={REPO}&pr=8140", headers=AGENT_A)).json()
    assert len(rows) == 1
    assert rows[0]["pr"] == 8140
    assert {c["name"] for c in rows[0]["reviewers"]} == {"claude", "codex"}


# ---- access ----------------------------------------------------------------

async def test_recording_requires_a_bearer_token(client):
    assert (await client.post("/review", json=payload(8150))).status_code == 401


async def test_reading_stats_requires_auth(client):
    assert (await client.get("/review/stats")).status_code == 401


async def test_unknown_run_is_404(client):
    assert (await client.get("/review/99999999", headers=AGENT_A)).status_code == 404


@pytest.mark.parametrize("path", ["/", "/panel"])
async def test_board_pages_render(client, path):
    r = await client.get(path, headers=AGENT_A)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ---- the panel's own payload -----------------------------------------------

# Verbatim shape of `panel.py --json` (v2.10): its repo slug is `github`, its
# local checkout name is `repo`, and the PR subject is `title`. Pinned as a
# fixture because the ingest's whole design is "take the panel's words" — if the
# two drift, the failure is a silently null column, not an error.
PANEL_JSON = {
    "repo": "lexray",
    "github": "simmons-simmons/lexray",
    "pr": 1625,
    "title": "feat: sleep detection",
    "base": "test",
    "changed_lines": 430,
    "diff_truncated": True,
    "diff_chars": 91000,
    "diff_budgets": {"claude": 60000, "codex": 60000, "pi": 200000, "judge": 60000},
    "config_notes": [],
    "sonar_gate": "no-pr-analysis",
    "ci_status": "PASS",
    "ci_failing": [],
    "judged": True,
    "judge_model": "opus",
    "judge_skip": None,
    "reviewers_ran": ["claude (sonnet)", "codex (gpt-5.6-luna, max)", "pi (kimi-k3, high)"],
    "reviewers": {
        "claude": {"model": "sonnet", "effort": None, "ran": True, "skip": None,
                   "max_diff_chars": 60000, "truncated": True},
        "codex": {"model": "gpt-5.6-luna", "effort": "max", "ran": True, "skip": None,
                  "max_diff_chars": 60000, "truncated": True},
        "gemini": {"model": "gemini-3.7-flash", "effort": None, "ran": False,
                   "skip": "gemini (gemini-3.7-flash): CLI absent",
                   "max_diff_chars": 60000, "truncated": True},
        "pi": {"model": "openrouter/moonshotai/kimi-k3", "effort": "high", "ran": True,
               "skip": None, "max_diff_chars": 200000, "truncated": False},
        "sonarqube": {"ran": True, "skip": None},
    },
    "reviewers_selected": ["claude", "codex", "gemini", "pi", "sonarqube"],
    "reviewers_override": None,
    "to_fix": [
        {"severity": "P1", "file": "app/sleep.py", "line": 88,
         "title": "timezone dropped on resume", "detail": "…",
         "reviewers": ["codex", "pi"], "reason": "real — the naive datetime wins"},
        {"severity": "P3", "file": "app/api.py", "line": None,
         "title": "docstring contradicts the guard", "detail": "",
         "reviewers": ["claude"], "reason": "real"},
    ],
    "sonar_findings": [
        {"severity": "P2", "file": "app/sleep.py", "line": 12,
         "title": "cognitive complexity 21 > 15", "detail": "sonarqube",
         "reviewers": ["sonarqube"], "reason": "sonarqube"},
    ],
    "dismissed": [
        {"severity": "P2", "file": "app/sleep.py", "line": 90, "title": "race on cache",
         "detail": "", "reviewers": ["pi"], "reason": "the lock above covers it"},
    ],
    "skipped": ["gemini (gemini-3.7-flash): CLI absent"],
    "run_key": "panel-fixture-1",
}


async def test_panel_json_payload_ingests_verbatim(client):
    """No translation layer between the panel and the board."""
    r = await client.post("/review", json=PANEL_JSON, headers=AGENT_A)
    assert r.status_code == 201, r.text
    run = (await client.get(f"/review/{r.json()['id']}", headers=AGENT_A)).json()

    assert run["repo"] == "simmons-simmons/lexray"   # `github`, not the local name
    assert run["pr_title"] == "feat: sleep detection"  # `title`
    assert run["judge_model"] == "opus"
    assert run["diff_truncated"] is True

    cards = {c["name"]: c for c in run["reviewers"]}
    assert cards["gemini"]["ran"] is False
    assert cards["gemini"]["confirmed"] == 0
    assert cards["pi"]["model"] == "openrouter/moonshotai/kimi-k3"
    assert cards["pi"]["truncated"] is False       # the 1M-context member saw it whole
    assert cards["pi"]["confirmed"] == 1 and cards["pi"]["dismissed"] == 1
    assert cards["codex"]["confirmed"] == 1 and cards["codex"]["solo"] == 0  # shared P1
    assert cards["claude"]["solo"] == 1

    # The sonar hard-gate issue is recorded but never scored.
    assert run["sonar"] == 1
    assert cards["sonarqube"]["raised"] == 0
