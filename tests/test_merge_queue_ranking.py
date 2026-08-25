"""#80's ranking half: an order for the merge queue that says what it is worth.

#227 shipped the line and left ``suggested_order`` permanently null; #101 shipped
``GET /review/collisions`` and said in its own docstring that *"ordering PRs by
it — landing the disjoint ones first — is #80's job and needs a policy about what
a collision costs that this endpoint has no business presuming"*. Both sides
existed and nothing joined them. This is the join, and the properties under test
are the ones that decide whether an order is safe to publish beside a live queue:

* **The proposal is not the queue.** ``active_order`` is untouched, ``you`` still
  answers FIFO, and nothing merges because something is ranked first. #227's own
  argument — *"agents may propose order; they must not silently rewrite the queue
  while also trying to land"* — is the whole reason a second field exists rather
  than a better sort on the first one.
* **The heaviest colliding PR lands first.** Not the intuitive "small ones
  first": the work of a re-integration falls on the LATE PR, so the sum
  ``Σ shared(i,j)·w(j)`` is minimised by putting the expensive branch in front
  and letting the cheap ones rebase onto it. Both silent breakages #80 records
  were big structural branches meeting a moved main.
* **A disjoint PR costs nothing from any position**, so it is placed where it
  waits least. That is #80's "land the disjoint ones first", and the reason it is
  free rather than merely nice.
* **An order derived from a partial measurement is never presented as a confident
  one.** One PR the board holds no file list for suppresses ``suggested_order``
  entirely; a prefix list leaves the order standing with ``trusted`` false and a
  caveat naming the rows. The ``plan_read``/``order_trust`` precedent: an order
  whose chosen and unchosen parts are indistinguishable gets trusted uniformly,
  and usually too much.
* **The hole is named, not inferred.** #94's skipped runs — merges, promotes and
  format-the-world commits — are the PRs the cost model says should land FIRST,
  so a ranking that quietly dropped them to the bottom would make its largest
  error on its most important rows. It refuses instead, and says #94.
* **Absent evidence is never good news.** The newest run answers for a PR and
  there is no fallback to an older file-bearing one: a stale list is a confident
  wrong answer where no list is a loud one.
"""

from __future__ import annotations

import re

import pytest

from app.collisions import COLLIDES, DISJOINT, PARTIAL, UNANSWERABLE
from app.ranking import Candidate, Overlap, rank, shared_resource_keys

from .conftest import LAPTOP

AGENT = {**LAPTOP, "X-Agent-Instance": "aa22bb"}

SHA = {n: f"{n:x}" * 40 for n in range(1, 10)}


@pytest.fixture
def repo(request) -> str:
    """A repo unique to this test.

    The schema is rebuilt once per session, so review runs recorded by one test
    are rivals for the next — and this file's subject is a whole queue and the
    partition of it, which sharing a repo would make a statement about pytest's
    collection order.
    """
    return f"acme/{re.sub(r'[^a-zA-Z0-9_]', '-', request.node.name)}"


# ============================================================ the cost model,
# at the unit, where the argument is visible without a database.


def cand(pr: int, position: int, *, total: int | None = 1, recorded: int | None = None,
         ready: bool = True, resources: frozenset[str] = frozenset(),
         run_head: str | None = "", queue_head: str | None = "") -> Candidate:
    """A candidate whose evidence is fully attested unless a case says otherwise.

    ``run_head``/``queue_head`` default to the SAME per-PR oid, so the ordinary
    case is a run taken at the commit the queue is on and a test has to opt in to
    staleness. The alternative — defaulting to unpinned — would have made every
    existing assertion about tiers pass for the wrong reason.
    """
    same = SHA[pr % 9 + 1]
    return Candidate(
        pr=pr, position=position, ready=ready,
        changed_files_total=total,
        files_recorded=(total or 0) if recorded is None else recorded,
        run_id=pr, run_ts="2026-08-24T00:00:00+00:00", resources=resources,
        queue_head=same if queue_head == "" else queue_head,
        run_head=same if run_head == "" else run_head,
        queue_base="main", run_base="main",
    )


def test_the_heaviest_colliding_pr_is_proposed_first():
    """The exchange argument, and the least intuitive thing this module does.

    A re-integration is work done on the LATE PR — merge the moved base into it,
    re-run its CI, re-run its panel round — so a colliding pair costs `w(late)`,
    and swapping an adjacent pair changes the total by `shared·(w(i) - w(j))`.
    The shared count cancels; the heavier one goes first, for every pair at once.

    Concretely: a 400-file branch and a 3-file branch sharing three paths. Land
    the small one first and the 400-file branch is re-merged against a base that
    moved under it, which is exactly the shape that cost #80 44 test failures
    when `panel.py` came back stale from a split.
    """
    small, big = cand(1, 1, total=3), cand(2, 2, total=400)
    out = rank([small, big], [Overlap(a=1, b=2, shared=3, sample=("panel.py",))])
    assert out.order == (2, 1)
    assert out.differs is True
    assert [r.tier for r in out.rows] == [COLLIDES, COLLIDES]
    assert out.rows[0].moved == -1 and out.rows[1].moved == 1


def test_a_disjoint_pr_goes_first_because_its_position_is_free():
    """It contributes nothing to the sum from anywhere, so cost does not
    constrain it — and every land it waits through is exposure to the base moving
    for reasons unrelated to it (#294's #290, MERGEABLE in the morning and
    CONFLICTING by lunchtime). Free on cost, better on rot: it goes first."""
    out = rank(
        [cand(1, 1, total=90), cand(2, 2, total=90), cand(3, 3, total=1)],
        [Overlap(a=1, b=2, shared=4, sample=("a.py",))],
    )
    assert out.order == (3, 1, 2)
    assert out.rows[0].tier == DISJOINT
    assert out.trusted is True and out.covers_all is True


def test_disjoint_prs_keep_arrival_order_among_themselves():
    """Nothing distinguishes them, so the tiebreak is FIFO — the one component of
    the key that cannot thrash. A ranking that shuffled equals would make the
    proposal change under a poller for no reason at all."""
    out = rank([cand(7, 1), cand(3, 2), cand(5, 3)])
    assert out.order == (7, 3, 5)
    assert out.differs is False


def test_an_unmeasured_pr_is_ranked_at_no_position_and_suppresses_the_order():
    """The #94 hole, and the reason it is a refusal rather than a footnote.

    A PR with no changed-file list is `unanswerable` in `app.collisions`' own
    vocabulary, and the panel's title-skip path produces exactly that for merges,
    promotes and format-the-world commits — which under this cost model are the
    PRs that should land FIRST. Ranking it last would be the largest error the
    model can make, made silently on its most important rows.
    """
    out = rank([cand(1, 1, total=5), cand(2, 2, total=None, recorded=0)])
    assert out.unranked == (2,)
    assert out.order == (1,)
    assert out.covers_all is False
    assert out.trusted is False
    # And #1 is NOT disjoint: it shares nothing with the PR the board can see,
    # and there is one it cannot.
    assert [r.tier for r in out.rows] == [PARTIAL, UNANSWERABLE]
    assert out.rows[1].rank is None
    assert "#94" in out.rows[1].reason


def test_an_unmeasured_pr_cannot_make_anybody_else_collide():
    """It is unanswerable in BOTH directions. An edge to a PR whose list nobody
    holds would let a row nobody can read change another row's tier, which is the
    same invention as giving it a position."""
    out = rank(
        [cand(1, 1, total=5), cand(2, 2, total=None, recorded=0)],
        [Overlap(a=1, b=2, shared=9, sample=("x.py",))],
    )
    assert out.rows[0].tier != COLLIDES
    assert out.rows[0].shared_total == 0
    assert out.rows[0].collides_with == ()


def test_a_prefix_list_leaves_the_order_standing_and_untrusted():
    """Different from unmeasurable, and the difference is the direction of the
    error. A prefix means the shared count is a FLOOR — more collisions than were
    found, never fewer — so the order is still the best evidence available. What
    it may not be is attested, and `disjoint` stops being a safety claim."""
    out = rank([cand(1, 1, total=2500, recorded=2), cand(2, 2, total=3)])
    assert out.covers_all is True
    assert out.trusted is False
    assert out.rows[0].tier == PARTIAL or out.rows[1].tier == PARTIAL
    partial = next(r for r in out.rows if r.tier == PARTIAL)
    assert partial.files_complete is False
    assert "prefix" in partial.reason


def test_a_found_collision_outranks_a_prefix_list():
    """`app.collisions`' ladder, and the same reasoning: a rival whose list is a
    prefix AND which shares a known path is a definite collision. Filing it under
    "might share something" hides a fact behind a doubt, and a ranking is exactly
    the caller that reads only the collision tier."""
    out = rank(
        [cand(1, 1, total=2500, recorded=2), cand(2, 2, total=3)],
        [Overlap(a=1, b=2, shared=1, sample=("panel.py",))],
    )
    assert {r.pr: r.tier for r in out.rows} == {1: COLLIDES, 2: COLLIDES}


def test_two_migrations_collide_without_sharing_a_path():
    """The case path intersection is blind to, and the answer to "what does a
    collision cost" being asked at the wrong granularity. Two branches each
    adding a DIFFERENT alembic revision share no file and collide absolutely:
    this repo keeps a single head, and its own pre-push hook refuses the
    multi-headed base that results."""
    migrations = frozenset({"migrations/"})
    out = rank([cand(1, 1, total=9, resources=migrations),
                cand(2, 2, total=2, resources=migrations)])
    assert {r.tier for r in out.rows} == {COLLIDES}
    assert out.order == (1, 2)
    edge = out.rows[0].collides_with[0]
    assert edge["shared"] == 0 and edge["shared_resources"] == ["migrations/"]


def test_shared_resource_keys_is_the_one_rule_both_sides_use():
    """Exported so the query that fetches the paths and the ranking that reasons
    about them cannot drift about what a `migrations/` path is."""
    assert shared_resource_keys(["migrations/0042_x.py"]) == frozenset({"migrations/"})
    assert shared_resource_keys(["app/migrations/x.py", "changelog.d/80.feat.md"]) == frozenset()


def test_one_pair_sent_twice_is_counted_once():
    """A caller that sends (a, b) and (b, a) must not double that pair's weight.
    Normalised on the way in rather than trusted, because the failure is silent:
    a pair weighed twice reorders a queue and nothing says why."""
    out = rank(
        [cand(1, 1, total=10), cand(2, 2, total=10)],
        [Overlap(a=1, b=2, shared=4), Overlap(a=2, b=1, shared=4)],
    )
    assert out.rows[0].shared_total == 4


def test_every_queued_pr_appears_exactly_once():
    """Total, like `app.collisions.classify`. A PR the queue holds and the
    proposal never mentions is the class of bug that endpoint was rewritten twice
    to make unreachable, and a proposal is read the same way — by a caller that
    walks one list."""
    queue = [cand(1, 1, total=4), cand(2, 2, total=None, recorded=0),
             cand(3, 3, total=2500, recorded=1), cand(4, 4, total=2)]
    out = rank(queue, [Overlap(a=1, b=4, shared=2, sample=("a.py",))])
    assert sorted([*out.order, *out.unranked]) == [1, 2, 3, 4]
    assert len(out.rows) == 4
    assert sum(out.counts.values()) == 4


def test_a_sample_longer_than_its_count_is_refused():
    """The count is what the sort weighs by and the sample is what a person
    reads; a caller that filled one and not the other produces a colliding pair
    weighed as sharing less than it does. Loud, here, rather than ranked."""
    with pytest.raises(ValueError, match="below"):
        Overlap(a=1, b=2, shared=1, sample=("a.py", "b.py"))


def test_disjoint_is_a_claim_about_the_queue_and_not_about_one_row():
    """The sharpest version of "do not be confident on partial data", because it
    is the one that survives every other guard: `suggested_order` is already null
    in all three cases below, and a row still labelled `disjoint` would be a false
    safety claim a reader could quote out of the payload on its own.

    A PR's own list being complete, pinned and consistent is not enough. The peer
    it is being compared against may touch, on the files it never reported,
    exactly what this PR touches — so one unattested row anywhere in the queue
    means no row is disjoint."""
    perfect = cand(1, 1, total=3)

    # (a) a peer nobody has any list for.
    assert rank([perfect, cand(2, 2, total=None, recorded=0)]).rows[0].tier == PARTIAL
    # (b) a peer whose list is a prefix.
    assert rank([perfect, cand(2, 2, total=2500, recorded=2)]).rows[0].tier == PARTIAL
    # (c) a peer answered for by a run taken at a commit it has since left.
    stale = cand(2, 2, total=3, run_head=SHA[7], queue_head=SHA[8])
    row = rank([perfect, stale]).rows[0]
    assert row.tier == PARTIAL
    assert "#2" in row.reason and "not attested" in row.reason

    # And with the whole queue attested, the claim is made and says so.
    clean = rank([perfect, cand(2, 2, total=3)])
    assert clean.rows[0].tier == DISJOINT
    assert "every one of which is attested too" in clean.rows[0].reason
    assert clean.trusted is True


def test_movement_is_measured_against_the_population_that_was_ranked():
    """With an unrankable PR in the line, arrival position over the whole queue
    and arrival position over the ranked subset differ — and reporting the first
    would attribute a displacement to the proposal that it did not make, sized by
    the number of PRs it could not see."""
    out = rank(
        [cand(1, 1, total=None, recorded=0), cand(2, 2, total=8), cand(3, 3, total=2)],
        [Overlap(a=2, b=3, shared=1, sample=("a.py",))],
    )
    assert out.order == (2, 3)
    # #2 and #3 arrived in that order among the ranked, and stayed in it.
    assert [r.moved for r in out.rows if r.rank is not None] == [0, 0]


def test_readiness_is_reported_and_never_ranked_on():
    """Measured first-hand and pinned to a commit — and still excluded from the
    sort. A verdict is invalidated by every push, so tiering on it would reshuffle
    the proposal each time the head does the one thing its slot is for. The
    queue's own model refuses that trade for `active_order`; a suggestion making
    the opposite one would be advising against the queue it advises on."""
    out = rank([cand(1, 1, total=2, ready=False), cand(2, 2, total=2, ready=True)])
    assert out.order == (1, 2)
    assert [r.ready for r in out.rows] == [False, True]


# ============================================================ end to end,
# against real review payloads and a real queue.


def files(*paths: str) -> list[dict]:
    return [{"path": p, "additions": 10, "deletions": 2} for p in paths]


def head_of(pr: int) -> str:
    """One full oid per PR, so a run and a queue entry can be pinned together."""
    return f"{pr:040x}"


async def record(client, repo: str, pr: int, paths: list[str] | None = None,
                 **over) -> dict:
    """One panel round, recorded the way `harness/loops/panel.py` records one.

    ``head_sha`` defaults to :func:`head_of` — the same oid :func:`join` enqueues
    the PR at — because a run that never said which commit it reviewed is not
    attested, and a suite whose every fixture was unattested could not test the
    attested path at all.
    """
    body = {
        "repo": repo, "pr": pr, "judged": True, "judge_model": "opus",
        "head_sha": head_of(pr), "base_branch": "main",
        "reviewers_selected": ["claude"],
        "reviewers": {"claude": {"model": "opus", "ran": True}},
        "to_fix": [], "dismissed": [], "sonar_findings": [],
    }
    if paths is not None:
        body["changed_files"] = files(*paths)
        body["changed_files_total"] = len(paths)
    r = await client.post("/review", json={**body, **over}, headers=AGENT)
    assert r.status_code == 201, r.text
    return r.json()


async def join(client, repo: str, pr: int, head: str | None = None, **over) -> dict:
    r = await client.post("/merge-queue/enqueue", headers=AGENT, json={
        "repo": repo, "base": "main", "pr": pr,
        "head": head or head_of(pr), **over})
    assert r.status_code == 200, r.text
    return r.json()


async def read(client, repo: str, **params) -> dict:
    r = await client.get("/merge-queue", headers=AGENT,
                         params={"repo": repo, "base": "main", **params})
    assert r.status_code == 200, r.text
    return r.json()


async def test_the_queue_is_ranked_from_the_boards_own_file_lists(client, repo):
    """The join, end to end. Three PRs, the overlap taken from what the panel
    recorded, and an order the endpoint can explain per pair."""
    await record(client, repo, 501, ["panel.py", "epic.py", "harness_rules.py",
                                     "lander.py", "reviews.py"])
    await record(client, repo, 502, ["panel.py"])
    await record(client, repo, 503, ["docs/README.md"])
    for pr in (501, 502, 503):
        await join(client, repo, pr, verdict="ready")

    view = await read(client, repo)
    # The live queue is arrival order and the proposal did not touch it.
    assert view["active_order"] == [501, 502, 503]
    assert view["ordering"] == "fifo"
    # Disjoint first, then the heavier of the colliding pair.
    assert view["suggested_order"] == [503, 501, 502]

    s = view["suggestion"]
    assert s["covers_all"] is True and s["differs_from_active"] is True
    assert s["order_trust"]["trusted"] is True
    assert s["order_trust"]["caveat"] is None
    assert s["counts"] == {DISJOINT: 1, COLLIDES: 2, PARTIAL: 0, UNANSWERABLE: 0}

    rows = {row["pr"]: row for row in s["prs"]}
    assert rows[503]["tier"] == DISJOINT
    assert rows[501]["weight"] == 5 and rows[502]["weight"] == 1
    assert rows[501]["collides_with"] == [
        {"pr": 502, "shared": 1, "files": ["panel.py"], "files_dropped": 0,
         "shared_resources": []}]


async def test_the_suggestion_never_becomes_the_queue(client, repo):
    """The boundary #227 is emphatic about. A PR ranked first is not at the head,
    is told so, and gets the same refusal it would have got without a ranking
    existing — `may_integrate: false`, and the PR it is actually behind."""
    await record(client, repo, 601, ["a.py"] * 1 + [f"f{i}.py" for i in range(40)])
    await record(client, repo, 602, ["a.py"])
    await join(client, repo, 601, verdict="ready")
    await join(client, repo, 602, verdict="ready")
    # 602 arrived second; ranked first it is not, but 601 is much heavier, so the
    # proposal puts 601 in front — and 602 is second in BOTH orders. Take the one
    # the proposal actually moved.
    view = await read(client, repo, pr=602, head=head_of(602))
    assert view["active_order"] == [601, 602]
    assert view["you"]["is_head"] is False
    assert view["you"]["may_integrate"] is False
    assert view["you"]["waiting_on"]["pr"] == 601

    # And reading it twice changes nothing about the line.
    again = await read(client, repo)
    assert again["active_order"] == view["active_order"]
    assert again["suggested_order"] == view["suggested_order"]


async def test_one_unmeasurable_pr_suppresses_the_whole_order(client, repo):
    """#94, in the payload. The panel's title-skip path records no files, so a
    merge or a format-the-world commit reaches the queue with nothing on the
    board about it — and that is the PR the cost model says should land first.
    So there is no confident order at all, the reasoning still comes back, and
    the caveat names the PR and the issue rather than leaving a reader to infer
    a hole from a short list."""
    await record(client, repo, 701, ["panel.py", "epic.py"])
    await record(client, repo, 702, ["panel.py"])
    # 703 reaches the queue having been skipped: a run exists for the repo, but
    # none for this PR.
    for pr in (701, 702, 703):
        await join(client, repo, pr, verdict="ready")

    view = await read(client, repo)
    assert view["active_order"] == [701, 702, 703]
    assert view["suggested_order"] is None

    s = view["suggestion"]
    assert s["covers_all"] is False
    assert s["partial_order"] == [701, 702]
    assert s["unranked"] == [703]
    trust = s["order_trust"]
    assert trust["trusted"] is False
    assert trust["measured"] == 2 and trust["unmeasured"] == 1
    assert trust["blind_spots"] == [
        {"pr": 703, "fault": "no-file-list", "issue": 94,
         "why": "no run of this PR recorded a changed-file list",
         "fix": "run a panel round on it; the panel's title-skip path will not "
                "record one until #94"}]
    assert "#703" in trust["caveat"] and "#94" in trust["caveat"]
    # A blind PR is one shortfall, not two: it is `unmeasured`, and counting it
    # under `incomplete_lists` as well would stop the numbers reconciling against
    # `counts` for a caller checking the population itself.
    assert trust["incomplete_lists"] == 0
    assert trust["measured"] + trust["unmeasured"] == sum(s["counts"].values())


async def test_a_prefix_list_withholds_the_order_and_keeps_the_reasoning(client, repo):
    """GitHub caps a PR's file list at 3,000 and the panel records what it read,
    so `changed_files_total` disagreeing with the stored count is the only
    evidence a list is a prefix — and a prefix is a partial measurement. The
    convenience field is withheld, because it is the field a consumer reads
    without reading anything else; `partial_order` carries the same list with its
    `trusted` beside it, which is where a caveated answer belongs."""
    await record(client, repo, 801, ["one.py"], changed_files_total=2500)
    await record(client, repo, 802, ["two.py"])
    await join(client, repo, 801, verdict="ready")
    await join(client, repo, 802, verdict="ready")

    view = await read(client, repo)
    assert view["suggested_order"] is None
    s = view["suggestion"]
    # Every PR was rankable — this is a trust refusal, not a coverage one.
    assert s["covers_all"] is True
    assert s["partial_order"] == [801, 802]
    assert s["trusted"] is False
    trust = s["order_trust"]
    assert trust["unmeasured"] == 0 and trust["incomplete_lists"] == 1
    assert trust["blind_spots"] == [
        {"pr": 801, "fault": "prefix-list", "issue": None,
         "why": "the stored file list is a prefix of what the PR touches, so a "
                "shared-path count is a floor",
         "fix": "nothing to do here — GitHub caps a PR's file list at 3,000 and "
                "this PR is over it"}]
    assert "#801" in trust["caveat"]


async def test_a_run_taken_at_a_commit_the_branch_has_left_is_not_evidence(client, repo):
    """The defect a second-opinion review found, and the one that would have been
    invisible: a PR reviewed at commit A and pushed to B is answered for by A's
    file list, which is complete, attested and about a diff that is not the one
    landing. Two such PRs could be reported disjoint on the strength of two lists
    that were both true and both about somewhere else.

    The queue already holds the commit — its whole guarantee over an agent's
    memory is that a claim names the commit it is about — so the check is a
    comparison the endpoint simply was not making."""
    await record(client, repo, 901, ["a.py"])          # recorded at head_of(901)
    await record(client, repo, 902, ["b.py"])
    await join(client, repo, 901, verdict="ready")
    # #902 pushed since its round: the queue is on a commit the run never saw.
    await join(client, repo, 902, head="f" * 40, verdict="queued")

    view = await read(client, repo)
    assert view["suggested_order"] is None
    s = view["suggestion"]
    assert s["covers_all"] is True and s["trusted"] is False
    rows = {r["pr"]: r for r in s["prs"]}
    # The stale row keeps its list — the file list is a floor, not a fiction —
    # and loses only its right to stand behind a safety claim.
    assert rows[902]["files_complete"] is True
    assert rows[902]["evidence_pinned"] is False
    assert rows[902]["attested"] is False
    assert rows[902]["run_head"] == head_of(902) and rows[902]["queue_head"] == "f" * 40
    # And #901, whose own evidence is perfect, is not disjoint either.
    assert rows[901]["attested"] is True
    assert rows[901]["tier"] == PARTIAL
    assert [b["fault"] for b in s["order_trust"]["blind_spots"]] == ["evidence-not-at-head"]
    assert s["order_trust"]["stale_evidence"] == 1


async def test_a_sender_that_contradicts_its_own_count_is_weighed_at_the_larger(client, repo):
    """`files_complete` tolerates `recorded > total` deliberately — for a
    completeness verdict that is a sender bug and not a prefix. A ranking cannot,
    because the count is the WEIGHT: a run claiming one changed file while storing
    forty paths would be sorted behind every branch it collides with, which is the
    end of a colliding pair that pays."""
    await record(client, repo, 1001, [f"f{i}.py" for i in range(40)],
                 changed_files_total=1)
    await record(client, repo, 1002, ["f0.py"])
    await join(client, repo, 1001, verdict="ready")
    await join(client, repo, 1002, verdict="ready")

    view = await read(client, repo)
    assert view["suggested_order"] is None
    s = view["suggestion"]
    rows = {r["pr"]: r for r in s["prs"]}
    assert rows[1001]["counts_agree"] is False
    assert rows[1001]["weight"] == 40, "the larger of two numbers that disagree"
    assert rows[1001]["attested"] is False
    # And it is still ranked ahead of the PR it collides with, which is what
    # taking the count at face value would have got wrong.
    assert s["partial_order"] == [1001, 1002]
    assert [b["fault"] for b in s["order_trust"]["blind_spots"]] == ["inconsistent-counts"]


async def test_the_newest_run_answers_and_nothing_older_does(client, repo):
    """#101 found the same defect twice — a predicate in front of the newest-run
    selection resurrecting a stale one and handing its answer back in a confident
    voice. The subject-side fallback that endpoint allows is refused here on
    purpose: every queued PR is one somebody named, so the same argument would
    apply to all of them, and a stale list is a confident wrong answer where no
    list is a loud one."""
    await record(client, repo, 901, ["shared.py"], round=1)
    # Round 2 recorded nothing — a skipped round, or a round before v2.23.
    await record(client, repo, 901, None, round=2)
    await record(client, repo, 902, ["shared.py"])
    await join(client, repo, 901, verdict="ready")
    await join(client, repo, 902, verdict="ready")

    view = await read(client, repo)
    assert view["suggested_order"] is None, "the newest run of #901 answered, and it said nothing"
    rows = {row["pr"]: row for row in view["suggestion"]["prs"]}
    assert rows[901]["tier"] == UNANSWERABLE
    # And #902 is not reported disjoint off the back of it.
    assert rows[902]["tier"] == PARTIAL or rows[902]["shared_total"] == 0


async def test_two_migrations_are_contended_end_to_end(client, repo):
    """Two branches, no shared path, one alembic head."""
    await record(client, repo, 1001, ["migrations/0100_a.py", "app/one.py"])
    await record(client, repo, 1002, ["migrations/0101_b.py"])
    await join(client, repo, 1001, verdict="ready")
    await join(client, repo, 1002, verdict="ready")

    view = await read(client, repo)
    rows = {row["pr"]: row for row in view["suggestion"]["prs"]}
    assert rows[1001]["tier"] == COLLIDES and rows[1002]["tier"] == COLLIDES
    assert rows[1001]["collides_with"][0]["shared_resources"] == ["migrations/"]
    assert rows[1001]["shared_total"] == 0, "they share a directory, not a path"


async def test_a_queue_of_one_is_not_ranked(client, repo):
    """One arrangement, and this endpoint is polled in a loop. `suggestion` being
    null says the ranking did not run, which is a different fact from a ranking
    that ran and refused — and `order_trust` is where the second one lives."""
    await record(client, repo, 1101, ["a.py"])
    await join(client, repo, 1101, verdict="ready")
    view = await read(client, repo)
    assert view["suggestion"] is None
    assert view["suggested_order"] is None


async def test_the_axes_it_does_not_weigh_are_named_in_the_payload(client, repo):
    """A consumer deciding how much of a landing decision to hand over needs the
    shape of what was left out. "File overlap says they are disjoint" is a very
    different claim from "file overlap says they are disjoint and nothing
    modelled whether one gates the other" — and the second one is #294."""
    await record(client, repo, 1201, ["a.py"])
    await record(client, repo, 1202, ["b.py"])
    await join(client, repo, 1201, verdict="ready")
    await join(client, repo, 1202, verdict="ready")

    axes = (await read(client, repo))["suggestion"]["axes_not_weighed"]
    assert 294 in [a["issue"] for a in axes]
    assert any("hunk" in a["axis"] for a in axes)
    assert any("preland" in a["axis"] for a in axes)
