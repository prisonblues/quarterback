"""#508: what a panel confirmed NEXT DOOR — the memory the per-PR chain cannot hold.

`finding_recurrence` (#67) chains a finding to earlier rounds of **its own** pull
request. The defect this fleet actually ships is one file over:

* **2026-08-26, ~07:30.** A panel confirmed a P1 in ``app.auth.delegated()`` — the
  dev bypass consulted *before* the credential check, so a caller with no
  credential authenticated.
* **~08:30, one hour later.** The identical shape shipped in ``app.auth.human()``
  on a different PR, copied out of the same source function. Codex reviewed that
  diff and returned four other real defects, not this one.

Both were in ``app/auth.py``, an hour apart, on two PRs. The second round was a
round **1**, so the per-PR chain had nothing to recur against, and the only thing
that caught it was a peer who happened to read the PR with their own fix fresh in
mind. `GET /review/next-door` is that peer's memory as a query, and this file is
its contract.

Grouped by the four properties the endpoint has to keep, because each one is a way
of being wrong that reads as working:

1. **It carries the motivating case.** ``test_a_defect_confirmed_next_door_this_
   morning_reaches_the_next_pr`` is the hour of 2026-08-26, replayed.
2. **It never asserts anything about the caller's diff.** Only other PRs, only
   ``confirmed``, and never a finding somebody has since *refuted* — a known-false
   line in front of a reviewer is worse than an empty list, because it spends
   attention and teaches the wrong shape.
3. **Absent is not clean.** A 404 where the subject recorded no file list, and a
   ``hints_dropped`` beside every cap, on this repo's rule that a trimmed answer
   which does not announce the trim reads as the whole one.
4. **The NULL trap, tested by writing rather than by reading the predicate.**
   ``test_a_hint_nobody_recorded_an_outcome_for_is_the_ordinary_case`` is the
   regression that matters most: written as a bare ``outcome != 'refuted'`` the
   filter is NULL for every finding with no outcome row — which is nearly all of
   them — and the endpoint answers ``hints: []`` on a repo full of them. That is
   the same three-valued-logic trap
   ``ck_review_findings_recurs_of_revisited`` documents one table over, and it
   was caught there by a test that tried the write.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.api.reviews import NEXT_DOOR_DETAIL_CHARS, NEXT_DOOR_LIMIT
from app.db import engine

from .conftest import LAPTOP

AGENT = {**LAPTOP, "X-Agent-Instance": "cc508d"}

#: The file both halves of the motivating case were in.
AUTH = "app/auth.py"


@pytest.fixture
def repo(request) -> str:
    """A repo name unique to this test.

    The schema is rebuilt once per session, not per test, so runs recorded by one
    test are *other PRs of the same repo* for the next — which is precisely the
    population this endpoint selects. Sharing a repo would make every
    ``considered`` assertion below a statement about pytest's collection order.
    """
    return f"acme/{re.sub(r'[^a-zA-Z0-9_]', '-', request.node.name)}"


def finding(title: str, **over) -> dict:
    f = {"title": title, "severity": "P2", "file": AUTH, "line": 10,
         "reviewers": ["claude"]}
    return {**f, **over}


def files(*paths: str) -> list[dict]:
    return [{"path": p, "additions": 10, "deletions": 2} for p in paths]


def payload(repo: str, pr: int, **over) -> dict:
    body = {
        "repo": repo,
        "pr": pr,
        "judged": True,
        "judge_model": "opus",
        "reviewers_selected": ["claude"],
        "reviewers": {"claude": {"model": "opus", "ran": True}},
        "to_fix": [],
        "dismissed": [],
        "sonar_findings": [],
    }
    return {**body, **over}


async def record(client, repo: str, pr: int, **over) -> dict:
    r = await client.post("/review", json=payload(repo, pr, **over), headers=AGENT)
    assert r.status_code == 201, r.text
    return r.json()


async def next_door(client, repo: str, pr: int, **params) -> dict:
    r = await client.get("/review/next-door",
                         params={"repo": repo, "pr": pr, **params}, headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


async def age_run(run_id: int, days: float) -> None:
    """Move a recorded run back in time.

    The window is the endpoint's decay term and there is no other way to test it:
    ``ts`` is server-set on ingest, deliberately, so a caller cannot backdate a
    run into somebody else's window.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE review_runs SET ts = :ts WHERE id = :id"),
            {"ts": datetime.now(UTC) - timedelta(days=days), "id": run_id},
        )


def titles(body: dict) -> list[str]:
    return [h["title"] for h in body["hints"]]


# ---- 1. the case it was filed for ------------------------------------------


async def test_a_defect_confirmed_next_door_this_morning_reaches_the_next_pr(
        client, repo):
    """The hour of 2026-08-26, replayed.

    PR #1 confirms the ordering defect in ``app/auth.py``. PR #2 opens an hour
    later touching the same file and its round 1 has nothing of its own to
    recur against — and *that* is the round this endpoint has to answer for.
    """
    await record(client, repo, 1, changed_files=files(AUTH),
                 to_fix=[finding("dev bypass consulted before the credential check",
                                 key="delegated-order", severity="P1")])
    await record(client, repo, 2, changed_files=files(AUTH))

    body = await next_door(client, repo, 2)
    assert titles(body) == ["dev bypass consulted before the credential check"]
    hint = body["hints"][0]
    assert hint["pr"] == 1 and hint["severity"] == "P1" and hint["file"] == AUTH
    # The evidence to check it without taking it on trust.
    assert hint["finding_key"] == "delegated-order"
    assert hint["run_id"] and hint["ts"] and hint["age_hours"] >= 0


async def test_the_contract_rides_on_the_wire_not_only_in_the_docstring(client, repo):
    """"A hint, not a finding" is the one property a consumer must implement and
    cannot infer from the payload, so it is stated in the payload. The same goes
    for the population: an empty `hints` means "nothing among the rounds I have
    seen", and nothing in the numbers can say so."""
    await record(client, repo, 1, changed_files=files(AUTH))
    body = await next_door(client, repo, 1)
    assert "a hint, not a finding" in body["contract"]
    assert "other PRs this board has panelled" in body["scope"]


# ---- 2. it never asserts anything about the caller's diff -------------------


async def test_the_subject_prs_own_findings_are_never_carried_back_to_it(client, repo):
    """This PR's own history is `GET /review/findings`. Folding the two would make
    a round's own last complaint indistinguishable from a peer's — and would let a
    reviewer be handed its own unfixed finding as though somebody else had found
    it, which is the recurrence chain eating its own tail."""
    await record(client, repo, 7, changed_files=files(AUTH),
                 to_fix=[finding("mine, from my own last round", key="own")])
    body = await next_door(client, repo, 7)
    assert body["hints"] == [] and body["considered"] == 0


async def test_a_dismissed_finding_is_not_something_to_carry_next_door(client, repo):
    """The judge already ruled against it. Carrying it would republish a rejected
    claim to a fresh audience that cannot see it was rejected."""
    await record(client, repo, 1, changed_files=files(AUTH),
                 to_fix=[finding("real", key="real")],
                 dismissed=[finding("the judge threw this out", key="tossed")])
    await record(client, repo, 2, changed_files=files(AUTH))
    assert titles(await next_door(client, repo, 2)) == ["real"]


async def test_a_finding_a_fixer_refuted_is_never_offered_as_confirmed(client, repo):
    """The sharper case, and the reason the outcome join exists at all.

    The judge confirmed it and a fixer then proved it wrong (#77). The row is a
    known-false statement, and "this was confirmed next door" is exactly the wrong
    sentence to attach to one: it spends a reviewer's attention and teaches the
    shape of a defect that was never there.
    """
    await record(client, repo, 1, changed_files=files(AUTH),
                 to_fix=[finding("stands up", key="good"),
                         finding("turned out to be wrong", key="bad")])
    r = await client.post(
        "/review/outcomes",
        json={"repo": repo, "pr": 1,
              "outcomes": [{"key": "bad", "outcome": "refuted",
                            "note": "the condition it assumed is never reached"}]},
        headers=AGENT)
    assert r.status_code in (200, 201), r.text

    await record(client, repo, 2, changed_files=files(AUTH))
    assert titles(await next_door(client, repo, 2)) == ["stands up"]


async def test_a_hint_nobody_recorded_an_outcome_for_is_the_ordinary_case(
        client, repo):
    """**The NULL trap, and the regression this file exists for.**

    Written as a bare ``outcome <> 'refuted'`` the filter evaluates to NULL for
    every finding with no outcome row — which is nearly every finding there is —
    and SQL drops those rows. The endpoint then answers ``hints: []`` on a repo
    full of perfectly good hints, and reads exactly like a quiet week.

    Asserted by writing a finding with no outcome and demanding it back, rather
    than by reading the predicate: the same trap on
    ``ck_review_findings_recurs_of_revisited`` was caught by a test that tried the
    write and missed by everyone who read the SQL.
    """
    await record(client, repo, 1, changed_files=files(AUTH),
                 to_fix=[finding("nobody has said what became of this", key="mute")])
    await record(client, repo, 2, changed_files=files(AUTH))

    body = await next_door(client, repo, 2)
    assert titles(body) == ["nobody has said what became of this"]
    assert body["hints"][0]["outcome"] is None


async def test_an_outcome_that_is_not_refuted_rides_along_on_the_row(client, repo):
    """`fixed` is the strongest form this hint takes — somebody confirmed the
    defect AND acted on it — so it is carried rather than filtered."""
    await record(client, repo, 1, changed_files=files(AUTH),
                 to_fix=[finding("confirmed and dealt with", key="done")])
    r = await client.post(
        "/review/outcomes",
        json={"repo": repo, "pr": 1, "outcomes": [{"key": "done", "outcome": "fixed"}]},
        headers=AGENT)
    assert r.status_code in (200, 201), r.text
    await record(client, repo, 2, changed_files=files(AUTH))

    body = await next_door(client, repo, 2)
    assert body["hints"][0]["outcome"] == "fixed"


async def test_a_finding_in_a_file_this_pr_does_not_touch_is_not_next_door(
        client, repo):
    """The join is on paths and is exact. "Somewhere in this repo, recently" is
    not a hint, it is a newsletter."""
    await record(client, repo, 1, changed_files=files("app/other.py"),
                 to_fix=[finding("far away", key="far", file="app/other.py")])
    await record(client, repo, 2, changed_files=files(AUTH))
    assert (await next_door(client, repo, 2))["hints"] == []


async def test_a_confirmed_finding_outside_the_window_has_decayed_out(client, repo):
    """Decay by time, which is half of what keeps this a hint rather than a
    backlog. A defect shape from six weeks ago in a file that has since been
    rewritten is noise wearing the same clothes."""
    stale = await record(client, repo, 1, changed_files=files(AUTH),
                         to_fix=[finding("six weeks ago", key="old")])
    await age_run(stale["id"], days=42)
    await record(client, repo, 2, changed_files=files(AUTH))

    assert (await next_door(client, repo, 2))["hints"] == []
    # ...and it is still there for a caller that asks for the wider window, which
    # is what makes this decay rather than deletion.
    assert titles(await next_door(client, repo, 2, days=90)) == ["six weeks ago"]


# ---- 3. absent is not clean ------------------------------------------------


async def test_a_pr_whose_runs_recorded_no_file_list_is_a_404_not_an_empty_answer(
        client, repo):
    """Unanswerable, never "nothing nearby". A PR the board cannot place in any
    file has no next door, and reporting that as an empty hint list would be a
    shortfall presenting as a clean result — the failure this codebase keeps
    finding in itself."""
    await record(client, repo, 3)          # no changed_files at all
    r = await client.get("/review/next-door", params={"repo": repo, "pr": 3},
                         headers=AGENT)
    assert r.status_code == 404
    assert "nothing to look for next door" in r.json()["detail"]


async def test_a_pr_that_genuinely_changed_nothing_is_answered_and_empty(client, repo):
    """A known empty list is knowledge. It has no next door because it has no
    files, which is a different statement from "we could not tell"."""
    await record(client, repo, 4, changed_files=[], changed_files_total=0)
    body = await next_door(client, repo, 4)
    assert body["hints"] == [] and body["files_recorded"] == 0
    assert body["files_complete"] is True


async def test_the_cap_says_what_it_dropped(client, repo):
    """A cap that trims an answer announces itself, or the trimmed answer reads as
    the whole one."""
    await record(client, repo, 1, changed_files=files(AUTH),
                 to_fix=[finding(f"finding {i}", key=f"k{i}") for i in range(5)])
    await record(client, repo, 2, changed_files=files(AUTH))

    body = await next_door(client, repo, 2, limit=2)
    assert len(body["hints"]) == 2
    assert body["considered"] == 5 and body["hints_dropped"] == 3


async def test_the_subjects_own_prefix_file_list_is_visible_to_the_caller(
        client, repo):
    """GitHub caps a PR's file list at 3,000. A subject whose list is a prefix
    under-reports its OWN next door, and that shortfall is on this side of the
    join where no hint row can show it."""
    await record(client, repo, 5, changed_files=files(AUTH), changed_files_total=900)
    body = await next_door(client, repo, 5)
    assert body["files_recorded"] == 1 and body["changed_files_total"] == 900
    assert body["files_complete"] is False


# ---- 4. one thing to know is one row ---------------------------------------


async def test_a_rival_that_raised_one_defect_four_times_is_one_hint(client, repo):
    """Four copies would push the other rivals off the end of `limit`, which is
    the cap silently choosing to report one PR's persistence over another PR's
    existence."""
    for _ in range(4):
        await record(client, repo, 1, changed_files=files(AUTH),
                     to_fix=[finding("the same defect, round after round",
                                     key="persistent")])
    await record(client, repo, 2, changed_files=files(AUTH))

    body = await next_door(client, repo, 2)
    assert body["considered"] == 1 and len(body["hints"]) == 1


async def test_the_newest_observation_of_a_defect_is_the_one_carried(client, repo):
    """A defect re-raised at a higher severity should arrive at its newest
    reading, not its first: the hint is meant to be what the board knows now."""
    await record(client, repo, 1, changed_files=files(AUTH),
                 to_fix=[finding("escalating", key="esc", severity="P3")])
    await record(client, repo, 1, changed_files=files(AUTH),
                 to_fix=[finding("escalating", key="esc", severity="P1")])
    await record(client, repo, 2, changed_files=files(AUTH))

    assert (await next_door(client, repo, 2))["hints"][0]["severity"] == "P1"


async def test_two_different_prs_raising_one_shape_are_two_hints(client, repo):
    """Deduplication is per (rival PR, defect) and deliberately not per defect. Two
    PRs shipping the same shape is a stronger signal than one, not a repetition to
    collapse — it is the whole observation #508 was filed on."""
    for pr in (1, 2):
        await record(client, repo, pr, changed_files=files(AUTH),
                     to_fix=[finding("one shape, two PRs", key="shared")])
    await record(client, repo, 3, changed_files=files(AUTH))

    body = await next_door(client, repo, 3)
    assert body["considered"] == 2
    assert {h["pr"] for h in body["hints"]} == {1, 2}


async def test_a_long_detail_is_cut_and_says_that_it_was(client, repo):
    """A hint is a handful of lines in a prompt. A judge's synthesis can run to
    paragraphs, and a sentence ending mid-clause reads to a model as the
    sentence."""
    await record(client, repo, 1, changed_files=files(AUTH),
                 to_fix=[finding("wordy", key="w", detail="x" * 2000)])
    await record(client, repo, 2, changed_files=files(AUTH))

    detail = (await next_door(client, repo, 2))["hints"][0]["detail"]
    assert detail.endswith("…") and len(detail) == NEXT_DOOR_DETAIL_CHARS + 1


async def test_the_default_limit_is_the_documented_one(client, repo):
    """The cap is part of the contract — a caller that never passes `limit` is
    entitled to know what it got."""
    await record(client, repo, 1, changed_files=files(AUTH))
    body = await next_door(client, repo, 1)
    assert body["hints_dropped"] == 0
    assert len(body["hints"]) <= NEXT_DOOR_LIMIT
