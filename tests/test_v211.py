"""v2.11: per-reviewer accounts, defect identity, calibration.

A finding used to keep one reviewer's text and reduce the rest to names, so the
board could say "codex and pi both reported this" but not what either of them
said — and the same defect seen twice was two unrelated rows. These tests pin
the three properties that fixes: accounts survive verbatim, attribution is
stored rather than inferred, and observations of one defect link across runs
without being collapsed into one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.api.reviews import _derive_key
from app.db import engine

from .conftest import LAPTOP, SERVER

REPO = "acme/v211repo"
AGENT_A = {**LAPTOP, "X-Agent-Instance": "aa11bb"}
AGENT_B = {**SERVER, "X-Agent-Instance": "cc22dd"}


def finding(**over) -> dict:
    """A judged finding two reviewers described differently."""
    f = {
        "severity": "P2",
        "file": "apps/europa/text_utils.py",
        "line": 213,
        "title": "unicode dash survives the strip",
        "detail": "the judge's merged statement",
        "verdict": "confirmed",
        "reason": "real — the regex only covers ascii",
        "reported_by": [
            {"reviewer": "claude", "severity": "P2", "line": 213,
             "account": "strip() leaves the en dash, so the key never matches"},
            {"reviewer": "pi", "severity": "P1", "line": 209,
             "account": "callers downstream compare the raw string; this is a P1"},
        ],
    }
    return {**f, **over}


def payload(pr: int, **over) -> dict:
    body = {
        "repo": REPO,
        "pr": pr,
        "pr_title": f"feat: thing {pr}",
        "judged": True,
        "judge_model": "opus",
        "reviewers_selected": ["claude", "pi"],
        "reviewers": {
            "claude": {"model": "sonnet", "ran": True},
            "pi": {"model": "kimi-k3", "effort": "high", "ran": True},
        },
        "to_fix": [finding()],
        "dismissed": [],
        "sonar_findings": [],
    }
    return {**body, **over}


async def record(client, pr: int, headers=AGENT_A, **over) -> dict:
    r = await client.post("/review", json=payload(pr, **over), headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


async def detail(client, run_id: int, headers=AGENT_A) -> dict:
    r = await client.get(f"/review/{run_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---- accounts --------------------------------------------------------------

async def test_each_reviewers_account_is_kept_verbatim(client):
    """The merge is additive: the synthesis is new, the originals ride along."""
    run = await record(client, 8201)
    f = (await detail(client, run["id"]))["findings"][0]

    assert f["title"] == "unicode dash survives the strip"  # the judge's synthesis
    by = {r["reviewer"]: r for r in f["reported_by"]}
    assert by["claude"]["account"].startswith("strip() leaves the en dash")
    assert by["pi"]["account"].startswith("callers downstream compare")
    # Each reviewer's own severity and line, not the judge's — the difference is
    # the calibration signal, so it must not be reconciled away.
    assert by["pi"]["severity"] == "P1" and by["pi"]["line"] == 209
    assert f["severity"] == "P2" and f["line"] == 213


async def test_reported_by_implies_attribution_without_a_reviewers_list(client):
    """`reviewers` becomes derivable, so a caller sending only accounts is whole."""
    run = await record(client, 8202)
    d = await detail(client, run["id"])
    assert d["findings"][0]["reviewers"] == ["claude", "pi"]
    cards = {c["name"]: c for c in d["reviewers"]}
    assert cards["claude"]["confirmed"] == 1 and cards["pi"]["confirmed"] == 1


async def test_named_reviewer_without_an_account_still_gets_credit(client):
    """A member listed alongside `reported_by` contributed no text, not nothing."""
    run = await record(client, 8203, to_fix=[finding(reviewers=["codex", "claude"])])
    d = await detail(client, run["id"])
    f = d["findings"][0]
    assert f["reviewers"] == ["codex", "claude", "pi"]   # payload order, then reporters
    assert {r["reviewer"] for r in f["reported_by"]} == {"claude", "pi"}
    assert {c["name"] for c in d["reviewers"]} >= {"codex", "claude", "pi"}


async def test_legacy_names_only_payload_still_records(client):
    """Older panels must not start failing to record."""
    run = await record(client, 8204, to_fix=[
        {"severity": "P1", "file": "app/x.py", "line": 4, "title": "off-by-one",
         "reviewers": ["claude", "codex"], "reason": "confirmed"},
    ])
    f = (await detail(client, run["id"]))["findings"][0]
    assert f["reviewers"] == ["claude", "codex"]
    assert f["reported_by"] == []
    assert f["key"]  # still identified, so old runs join the same chains


async def test_two_accounts_from_one_reviewer_keep_the_first(client):
    """(finding, reviewer) is unique; ingest is best-effort, so it must not 500."""
    run = await record(client, 8205, to_fix=[finding(reported_by=[
        {"reviewer": "pi", "severity": "P1", "account": "first"},
        {"reviewer": "pi", "severity": "P3", "account": "second"},
    ])])
    f = (await detail(client, run["id"]))["findings"][0]
    assert [r["account"] for r in f["reported_by"]] == ["first"]
    assert f["reviewers"] == ["pi"]


async def test_panel_canonical_field_names_are_accepted(client):
    """`synthesis`/`rationale` in the judge's shape, `title`/`reason` in the old
    one — a renamed field would otherwise fail silently into a null column."""
    run = await record(client, 8206, to_fix=[{
        "severity": "P2", "file": "app/a.py", "line": 9,
        "synthesis": "the judge's merged statement of the bug",
        "rationale": "real — both accounts describe the same write",
        "reported_by": [{"reviewer": "codex", "severity": "P2", "detail": "verbatim"}],
    }])
    f = (await detail(client, run["id"]))["findings"][0]
    assert f["title"] == "the judge's merged statement of the bug"
    assert f["reason"].startswith("real —")
    assert f["reported_by"][0]["account"] == "verbatim"


async def test_an_unstorable_line_number_costs_the_line_not_the_run(client):
    """Recording is best-effort: a line too big for the column must not take the
    whole run's record down with it."""
    run = await record(client, 8207, to_fix=[finding(
        line=9_999_999_999,
        reported_by=[{"reviewer": "pi", "severity": "P2", "line": -9_999_999_999,
                      "account": "still worth keeping"}],
    )])
    f = (await detail(client, run["id"]))["findings"][0]
    assert f["line"] is None
    assert f["reported_by"][0]["line"] is None
    assert f["reported_by"][0]["account"] == "still worth keeping"


# ---- scorecards ------------------------------------------------------------

async def test_severity_calibration_counts_against_the_judge(client):
    """A reviewer that is right but always cries P1 costs triage time, which
    precision alone cannot say."""
    run = await record(client, 8210)
    cards = {c["name"]: c for c in (await detail(client, run["id"]))["reviewers"]}
    assert cards["claude"]["sev_agree"] == 1     # P2 == the judge's P2
    assert cards["claude"]["sev_stricter"] == 0
    assert cards["pi"]["sev_stricter"] == 1      # called P1 what the judge called P2
    assert cards["pi"]["sev_agree"] == 0

    stats = (await client.get(
        f"/review/stats?repo={REPO}&judged_only=true", headers=AGENT_A)).json()
    pi = next(m for m in stats["by_model"] if m["reviewer"] == "pi")
    assert pi["sev_stricter"] >= 1
    assert pi["severity_calibration"] == round(
        pi["sev_agree"] / (pi["sev_agree"] + pi["sev_stricter"] + pi["sev_looser"]), 3)


async def test_calibration_stays_none_without_reviewer_severities(client):
    """Pre-v2.11 runs carry no per-reviewer severity; that must read as unknown,
    not as perfect disagreement."""
    await record(client, 8211, reviewers_selected=["gemini"],
                 reviewers={"gemini": {"model": "gemini-3.7-flash", "ran": True}},
                 to_fix=[{"severity": "P1", "file": "app/q.py", "title": "leak",
                          "reviewers": ["gemini"], "reason": "confirmed"}])
    stats = (await client.get(f"/review/stats?repo={REPO}", headers=AGENT_A)).json()
    gemini = next(m for m in stats["by_model"] if m["reviewer"] == "gemini")
    assert gemini["confirmed"] >= 1
    assert gemini["severity_calibration"] is None


async def test_consensus_counts_findings_someone_else_also_raised(client):
    """Agreeing with everyone and always reporting alone are different
    propositions at the same precision."""
    run = await record(client, 8212, to_fix=[
        finding(),                                    # claude + pi
        finding(title="solo catch", line=9, reported_by=[
            {"reviewer": "pi", "severity": "P3", "account": "only pi saw this"}]),
    ])
    cards = {c["name"]: c for c in (await detail(client, run["id"]))["reviewers"]}
    assert cards["pi"]["raised"] == 2 and cards["pi"]["shared"] == 1
    assert cards["pi"]["solo"] == 1
    assert cards["claude"]["raised"] == 1 and cards["claude"]["shared"] == 1

    stats = (await client.get(f"/review/stats?repo={REPO}", headers=AGENT_A)).json()
    pi = next(m for m in stats["by_model"] if m["reviewer"] == "pi")
    assert pi["consensus_rate"] == round(pi["shared"] / pi["raised"], 3)


# ---- defect identity -------------------------------------------------------

async def test_related_ids_are_stored_as_keys_not_run_local_ids(client):
    """One decision spread over four files is not one finding, but it is one
    fix — and the panel's ids restart every run, so they cannot carry the link."""
    run = await record(client, 8220, to_fix=[
        finding(id="F01", related=["F02", "F99"]),
        finding(id="F02", file="apps/luna/text_utils.py", title="same decision"),
    ])
    finds = {f["title"]: f for f in (await detail(client, run["id"]))["findings"]}
    a = finds["unicode dash survives the strip"]
    b = finds["same decision"]
    assert a["related"] == [b["key"]]   # resolved; the dangling F99 is dropped
    assert b["related"] == []


async def test_explicit_key_wins_over_the_derived_one(client):
    """A caller with a stable defect id of its own must be able to say so."""
    run = await record(client, 8221, to_fix=[finding(key="europa-dash-1")])
    assert (await detail(client, run["id"]))["findings"][0]["key"] == "europa-dash-1"


async def test_derived_key_ignores_the_line(client):
    """The line moves when the fix above it lands; an identity that moves links
    nothing."""
    assert _derive_key("a/b.py", "Off-by-one!") == _derive_key("a/b.py", "off by one")
    assert _derive_key("a/b.py", "x") != _derive_key("c/b.py", "x")

    one = await record(client, 8222, to_fix=[finding(line=213)])
    two = await record(client, 8222, to_fix=[finding(line=880)])
    keys = {(await detail(client, r["id"]))["findings"][0]["key"] for r in (one, two)}
    assert len(keys) == 1


async def test_an_untitled_finding_is_keyed_on_what_was_stored(client):
    """The stored title is defaulted, and the backfill keys the *stored* title —
    so keying the raw empty string would put new rows in a different chain from
    the identical old ones."""
    run = await record(client, 8223, to_fix=[
        {"file": "app/n.py", "reviewers": ["codex"], "reason": "confirmed"},
    ])
    f = (await detail(client, run["id"]))["findings"][0]
    assert f["title"] == "(untitled)"
    assert f["key"] == _derive_key("app/n.py", "(untitled)")


async def test_backfilled_keys_match_the_apps_recipe(client):
    """Migration 0012 keys pre-v2.11 rows in SQL. If the two recipes drift, old
    runs join no chain — and nothing else would notice."""
    sql = ("select substr(md5(coalesce(:f, '') || '|' || "
           "btrim(regexp_replace(lower(:t), '[^a-z0-9]+', ' ', 'g'))), 1, 16)")
    cases = [
        ("app/x.py", "Off-by-one in the retry loop"),
        (None, "  spaces   and\tTABS  "),
        # An en dash by escape, not literally: the point is that a non-ascii
        # char normalises the same way in both recipes, and RUF001 is right that
        # a literal one in source is otherwise indistinguishable from a hyphen.
        ("apps/europa/text_utils.py", "unicode \u2013 dash survives the strip"),
        ("a.py", ""),
    ]
    async with engine.connect() as conn:
        for f, t in cases:
            got = await conn.scalar(text(sql), {"f": f, "t": t})
            assert got == _derive_key(f, t), f"drift on {t!r}"


# ---- linking observations across runs --------------------------------------

async def test_the_same_defect_in_two_runs_is_two_linked_observations(client):
    """Collapsing them would erase whether the fix landed; not linking them
    leaves 'how many rounds did this PR take?' unanswerable."""
    await record(client, 8230)
    await record(client, 8230)
    h = (await client.get(f"/review/findings?repo={REPO}&pr=8230", headers=AGENT_A)).json()

    assert h["rounds"] == 2 and h["truncated"] is False
    chain = next(c for c in h["findings"] if c["title"] == "unicode dash survives the strip")
    assert chain["runs_seen"] == 2
    assert chain["status"] == "open"            # still raised in the newest run
    assert chain["first_run"] != chain["last_run"]
    # The accounts travel with each observation, so the merge stays auditable.
    assert {r["reviewer"] for r in chain["observations"][0]["reported_by"]} == {"claude", "pi"}


async def test_a_finding_absent_from_the_latest_run_reads_as_gone(client):
    await record(client, 8231)
    await record(client, 8231, to_fix=[finding(file="app/other.py", title="something else")])
    h = (await client.get(f"/review/findings?repo={REPO}&pr=8231", headers=AGENT_A)).json()
    by_title = {c["title"]: c for c in h["findings"]}
    assert by_title["unicode dash survives the strip"]["status"] == "gone"
    assert by_title["something else"]["status"] == "open"


async def test_a_finding_the_judge_always_rejected_reads_as_dismissed(client):
    await record(client, 8232, to_fix=[], dismissed=[finding()])
    await record(client, 8232, to_fix=[], dismissed=[finding()])
    h = (await client.get(f"/review/findings?repo={REPO}&pr=8232", headers=AGENT_A)).json()
    assert h["findings"][0]["status"] == "dismissed"
    assert h["findings"][0]["runs_seen"] == 2


async def test_chains_are_scoped_to_one_pr(client):
    """`key` identifies a defect within a PR: the same 'unused import' in two
    PRs is not one chain."""
    await record(client, 8240)
    await record(client, 8241)
    h = (await client.get(f"/review/findings?repo={REPO}&pr=8240", headers=AGENT_A)).json()
    assert h["rounds"] == 1
    assert all(o["run_id"] == h["runs"][0]["id"]
               for c in h["findings"] for o in c["observations"])


async def test_history_window_reports_when_it_truncated(client):
    """A `gone` status over a truncated window describes the window, so the
    window has to say it was one."""
    await record(client, 8250)
    await record(client, 8250)
    h = (await client.get(f"/review/findings?repo={REPO}&pr=8250&limit=1",
                          headers=AGENT_A)).json()
    assert h["rounds"] == 1 and h["truncated"] is True


async def test_history_of_an_unreviewed_pr_is_empty_not_404(client):
    h = (await client.get(f"/review/findings?repo={REPO}&pr=999999", headers=AGENT_A)).json()
    assert h == {"repo": REPO, "pr": 999999, "rounds": 0, "stopped": None,
                 "stop_confident": None, "truncated": False,
                 "runs": [], "findings": []}


async def test_history_needs_a_repo_and_pr(client):
    assert (await client.get("/review/findings", headers=AGENT_A)).status_code == 422


async def test_history_requires_auth(client):
    assert (await client.get(f"/review/findings?repo={REPO}&pr=8230")).status_code == 401


@pytest.mark.parametrize("headers", [AGENT_A, AGENT_B])
async def test_history_is_readable_by_any_authenticated_machine(client, headers):
    """The board is one shared workspace; a review is not private to its author."""
    await record(client, 8260, headers=AGENT_A)
    r = await client.get(f"/review/findings?repo={REPO}&pr=8260", headers=headers)
    assert r.status_code == 200 and r.json()["rounds"] >= 1
