"""#550: which arm of the priming comparison a round was in, on the row with the round.

PR #631 gave the reviewer prompt the pull request's own title and body, framed as the
author's assertion rather than as fact, so that "this change asserts a measured result
and ships nothing that produces it" became a finding a seat could actually report.

It shipped with #550's own condition unmet, and #550 said so at the time. Handing a
reviewer a body that says "this is safe because X" **primes** it to accept X; a primed
seat reports FEWER findings; and fewer findings look like a clean PR. So whether the
framing holds is not something to assert — it is something to measure, by running the
same PRs with and without the block and comparing the counts.

The panel knew whether it had sent the block and said so in `config_notes`, which is a
list of English sentences. Nothing on `review_runs` carried the fact, so a population
of rounds could not be partitioned by whether it was primed: both arms could be run and
the comparison would still be unaggregatable. That is the same shape of defect
`mc57fb9ba` fixed for the dials and `mdef4716b` for the harness — a fact the round knew
about itself, kept only in prose, and therefore lost to every reader that is not a
person.

**Two columns, and the split is the judgement.** `pr_claim` is the ARM (what was asked
for) and `pr_claim_sent` is whether the arm was delivered — the block is charged
against the tightest seat's diff budget and dropped whole where that budget cannot
carry it. A round that asked and dropped is in NEITHER arm; told only what the seats
got, an aggregation would score it as a control and read a budget effect as evidence
about the framing, which is the exact error the measurement exists to avoid making.
"""

from __future__ import annotations

# The module, not the names off it, on `test_review_harness_identity.py`'s rule: the
# bounds arrive with the feature, so a `from ... import` of one turns the red half of
# every other test in this file into a collection error.
from app.api import reviews  # noqa: F401

from .conftest import LAPTOP

REPO = "acme/primed550"
AGENT = {**LAPTOP, "X-Agent-Instance": "d550d1"}


def payload(pr: int, **over) -> dict:
    body = {
        "repo": REPO,
        "pr": pr,
        "pr_title": f"feat: thing {pr}",
        "base": "main",
        "reviewed": True,
        "judged": True,
        "judge_model": "sonnet",
        "reviewers_selected": ["claude"],
        "reviewers": {"claude": {"model": "sonnet", "ran": True}},
        "pr_claim": {"setting": True, "sent": True},
    }
    return {**body, **over}


async def record(client, pr: int, **over) -> dict:
    r = await client.post("/review", json=payload(pr, **over), headers=AGENT)
    assert r.status_code == 201, r.text
    return r.json()


async def detail(client, run_id: int) -> dict:
    r = await client.get(f"/review/{run_id}", headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


async def runs(client) -> list[dict]:
    r = await client.get("/reviews", params={"repo": REPO}, headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


async def test_the_arm_survives_the_round_trip(client):
    """The whole point: whether a round was primed is readable off the round
    afterwards, rather than inferred from a sentence somebody wrote in the notes."""
    run = await detail(client, (await record(client, 1))["id"])
    assert run["pr_claim"] is True
    assert run["pr_claim_sent"] is True


async def test_the_control_arm_is_stored_as_the_control_arm(client):
    """`--no-pr-claim`, or `review_panel.pr_claim: false`. Recorded as an ASSERTION
    that the round was unprimed, which is what makes it a control rather than a round
    nobody can vouch for."""
    run = await detail(client, (await record(
        client, 2, pr_claim={"setting": False, "sent": False}))["id"])
    assert run["pr_claim"] is False
    assert run["pr_claim_sent"] is False


async def test_a_round_that_asked_and_dropped_is_in_neither_arm(client):
    """Why two columns rather than one. The panel drops the block whole where the
    tightest seat's budget cannot carry the author's own words, so the round asked and
    the seats saw nothing. Told only the second half a reader would count it as a
    control and attribute a budget effect to the framing."""
    run = await detail(client, (await record(
        client, 3, pr_claim={"setting": True, "sent": False}))["id"])
    assert run["pr_claim"] is True
    assert run["pr_claim_sent"] is False


async def test_the_arms_partition_a_POPULATION_from_the_run_list(client):
    """The comparison this issue exists for, as a test rather than as an argument.

    Asserted off the LIST view on purpose, on `harness_digest`'s rule: a measurement
    reads a population and groups it, and a check that fetched each round one at a
    time would prove the value is stored without proving it can be grouped on. That is
    the difference between this landing and #550 staying blocked."""
    await record(client, 4, pr_claim={"setting": True, "sent": True})
    await record(client, 5, pr_claim={"setting": False, "sent": False})
    listed = {r["pr"]: r for r in await runs(client) if r["pr"] in (4, 5)}
    assert len(listed) == 2
    primed = [r for r in listed.values() if r["pr_claim"] and r["pr_claim_sent"]]
    control = [r for r in listed.values() if r["pr_claim"] is False]
    assert len(primed) == 1 and len(control) == 1


async def test_a_producer_that_says_nothing_records_nothing(client):
    """NULL is "the panel did not say" — every round recorded before these columns,
    and every skip and refusal path, which dispatches no seat and so primes none.

    `False` there would put a round that reviewed nothing into the unprimed arm of a
    comparison it was never in, which is the reading rule `code_access` and `reviewed`
    both state and the reason neither collapses its absent case."""
    run = await detail(client, (await record(client, 6, pr_claim=None))["id"])
    assert run["pr_claim"] is None
    assert run["pr_claim_sent"] is None


async def test_half_a_block_is_still_recorded_half(client):
    """Each field is independently nullable, so a producer that carries one and not
    the other is stored as it spoke rather than having the gap filled in. A guessed
    `sent` would be this board asserting something about a round it was not told."""
    run = await detail(client, (await record(
        client, 7, pr_claim={"setting": True}))["id"])
    assert run["pr_claim"] is True
    assert run["pr_claim_sent"] is None
