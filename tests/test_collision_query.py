"""#101: reading the v2.23 datum back as a collision query — select, then classify.

`GET /review/collisions` was written twice before this and pulled both times. A
four-seat panel reviewed it twice and found the **same defect** in both rounds,
and round 2's instance was introduced by round 1's fix:

===== ============================================== =============================
round  the bug                                        the shape
===== ============================================== =============================
r1     a rival was answered for by its newest          filter on *has files*,
       **file-bearing** run (``88-F06``)               **then** pick newest
r2     a rival was answered for by its newest          filter on *state*,
       **OPEN-state** run (``88-F01``)                 **then** pick newest
===== ============================================== =============================

One premise behind both: that the rival population can be narrowed by filters
composed at query level, with the newest-run selection as just another filter in
that composition. Per #67 that got reported rather than patched a third time, and
this is the redesign it was reported for — **select first, classify second**.

So the tests are grouped by the two halves, and the first group needs no database
at all, which is itself the point: the classification is a pure ladder in
:mod:`app.collisions` precisely so that its exhaustiveness can be asserted
directly instead of inferred from an endpoint's output.

Three regressions carry the file, one per historical defect:

* ``test_a_rival_is_represented_by_its_newest_run_outright`` — r1.
* ``test_a_rival_merged_after_an_open_round_is_not_answered_by_the_open_one`` —
  r2, the one whose fix introduced nothing because there is no filter in front of
  the selection to introduce it into.
* ``test_a_rival_that_claims_files_it_never_stored_is_partial_not_absent`` —
  ``88-F07``, the same disease from the other side: a rival that passed the "has a
  file list" test, contributed no join rows, and appeared in **no** list at all,
  read by a caller as "answered, and disjoint".

All three were confirmed to fail against the pulled implementation before being
committed — see the PR body. A regression test that was never run against the
broken code is a passing assertion that the bug is gone (v2.58).
"""

from __future__ import annotations

import re

import pytest

from app.api.reviews import SHARED_FILES_CAP
from app.collisions import (
    CLASSES,
    COLLIDES,
    DISJOINT,
    EXCLUDED,
    PARTIAL,
    UNANSWERABLE,
    Rival,
    classify,
    files_complete,
)

from .conftest import LAPTOP

AGENT = {**LAPTOP, "X-Agent-Instance": "cc11dd"}


@pytest.fixture
def repo(request) -> str:
    """A repo name unique to this test.

    The suite rebuilds the schema once per session, not per test, so runs
    recorded by one test are rivals for the next — and this file's whole subject
    is a *population* and its exhaustive partition. Sharing a repo would make
    ``counts.considered`` and every exact-list assertion below a statement about
    the order pytest happened to collect in.
    """
    return f"acme/{re.sub(r'[^a-zA-Z0-9_]', '-', request.node.name)}"


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


def files(*paths: str) -> list[dict]:
    return [{"path": p, "additions": 10, "deletions": 2} for p in paths]


async def collisions(client, repo: str, pr: int, **params) -> dict:
    r = await client.get("/review/collisions",
                         params={"repo": repo, "pr": pr, **params}, headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


def rival(**over) -> Rival:
    """A rival with the least interesting values, overridden per case.

    ``shared_total`` follows ``shared`` unless a case sets it, so a test can say
    "shares these paths" without restating the count — and the two cases that
    care about the split (a trimmed sample, and a mismatched pair) say so.
    """
    base = {"pr": 1, "run_id": 1, "pr_state": "OPEN", "is_draft": False,
            "changed_files_total": 1, "files_recorded": 1, "shared": ()}
    merged = {**base, **over}
    merged.setdefault("shared_total", len(merged["shared"]))
    return Rival(**merged)


def test_the_verdict_comes_from_the_overlap_count_not_the_trimmed_sample():
    """The sample the wire carries is capped; the count never is. A rival sharing
    3,000 paths must not depend on the trim for its verdict, and a caller that
    fills one field and not the other is refused rather than classified — the
    failure it would otherwise produce is a colliding rival reported as sharing
    nothing, which is the exact shape of wrong answer this endpoint exists to
    make unreachable."""
    trimmed = rival(shared=("a.py",), shared_total=3000)
    assert classify(trimmed).cls == COLLIDES
    with pytest.raises(ValueError, match="shared_total"):
        Rival(pr=1, run_id=1, pr_state="OPEN", is_draft=False, changed_files_total=1,
              files_recorded=1, shared=("a.py", "b.py"), shared_total=1)


# ---- the ladder, with no database in sight ---------------------------------
#
# `classify` is pure and takes an ALREADY-SELECTED run, which is the structural
# half of the fix: a predicate written into it cannot change which run answers for
# a PR, because by the time it runs the run is chosen.


def test_the_ladder_returns_exactly_one_known_class_for_every_shape():
    """Exhaustiveness, asserted directly. The bug this endpoint was pulled for is
    a rival that belongs in no bucket and therefore appears in no list — read by a
    caller as "answered, and disjoint" — so "every shape lands somewhere, and
    somewhere known" is the property, not any particular verdict."""
    shapes = [
        rival(changed_files_total=t, files_recorded=n, shared=s, pr_state=st, is_draft=d)
        for t in (None, 0, 1, 2500)
        for n in (0, 1)
        for s in ((), ("shared.py",))
        for st in (None, "OPEN", "MERGED", "CLOSED")
        for d in (None, False, True)
    ]
    for r in shapes:
        for closed in (False, True):
            for drafts in (False, True):
                v = classify(r, include_closed=closed, exclude_drafts=drafts)
                assert v.cls in CLASSES, (r, closed, drafts)


def test_a_run_that_recorded_nothing_at_all_is_unanswerable_never_disjoint():
    """The half that matters most. An unanswered PR reported silently absent makes
    an empty `collides` read as "safe to land" — a shortfall presenting as a clean
    result, which is the failure this codebase keeps finding in itself."""
    assert classify(rival(changed_files_total=None, files_recorded=0)).cls == UNANSWERABLE


def test_a_known_empty_list_is_knowledge_and_therefore_disjoint():
    """``88-F04``. A PR that genuinely changed zero files is disjoint from
    everything. Keying "has a file list" on the presence of child rows made it
    indistinguishable from a run that recorded nothing — which is the v2.23
    release's own three-ways distinction broken by the code enforcing it."""
    assert classify(rival(changed_files_total=0, files_recorded=0)).cls == DISJOINT


def test_a_rival_claiming_files_it_never_stored_is_partial():
    """``88-F07``, at the unit. ``changed_files_total IS NOT NULL`` was the whole
    "is this answerable" test, so a rival claiming 2,500 files with none stored
    passed it, contributed no join rows, and was absent from *both* lists. It is
    :data:`PARTIAL` here by construction, because "answerable" and "actually
    contributed paths" are no longer asked separately."""
    assert classify(rival(changed_files_total=2500, files_recorded=0)).cls == PARTIAL


def test_a_stored_prefix_is_partial_rather_than_disjoint():
    assert classify(rival(changed_files_total=2500, files_recorded=2)).cls == PARTIAL


def test_a_list_nobody_counted_is_answerable_but_never_disjoint():
    """The genuinely ambiguous case, resolved the safe way and on purpose. Nothing
    says an uncounted list is a prefix; nothing says it is not, either, and
    `disjoint` is the one verdict a caller may act on as a safety claim. Granting
    it from a list nobody attested to would be an answer reading safer than the
    evidence, which is the whole failure mode this issue exists for."""
    assert classify(rival(changed_files_total=None, files_recorded=3)).cls == PARTIAL
    assert files_complete(None, 3) is False


def test_a_definite_shared_path_outranks_a_doubt_about_completeness():
    """`collides` before `partial` in the ladder, and the order is load-bearing. A
    rival whose list is a prefix AND which shares a known path is a *definite*
    collision; filing it under "might share something" hides a fact behind a
    doubt, and a caller reading only `collides` — which is what a ranking function
    does (#232) — would miss it entirely."""
    r = rival(changed_files_total=2500, files_recorded=2, shared=("panel.py",))
    assert classify(r).cls == COLLIDES
    assert files_complete(r.changed_files_total, r.files_recorded) is False


def test_a_state_nobody_recorded_is_not_a_closed_state():
    """The NULL case, which decides whether the state test is safe to make at all.
    Every pre-v2.23 run states no PR state; treating that as closed would silently
    narrow the population to PRs panelled since v2.23 — a filter quietly becoming
    a cutoff, on the side that loses rivals."""
    assert classify(rival(pr_state=None, shared=("x.py",))).cls == COLLIDES


def test_include_closed_reclassifies_rather_than_unfilters():
    """It changes which BUCKET a merged rival lands in and never which run answers
    for it — the distinction the two pulled versions did not have."""
    merged = rival(pr_state="MERGED", shared=("x.py",))
    assert classify(merged).cls == EXCLUDED
    assert classify(merged).because == "MERGED"
    assert classify(merged, include_closed=True).cls == COLLIDES


def test_a_draft_is_set_aside_only_when_asked_and_says_so():
    """A draft's `pr_state` is `OPEN`, so this cannot be inferred from the state —
    which is why `is_draft` is a column of its own and a flag of its own."""
    draft = rival(is_draft=True, shared=("x.py",))
    assert classify(draft).cls == COLLIDES
    assert classify(draft, exclude_drafts=True).cls == EXCLUDED
    assert classify(draft, exclude_drafts=True).because == "DRAFT"


def test_a_merged_rival_is_excluded_before_it_is_asked_about_files():
    """Order of the first rung: a merged rival whose run recorded nothing is
    `excluded`, not `unanswerable`. Nobody asked for it to be answered for, and
    reporting it as an open question would put every merged PR of the window into
    the list a caller is meant to act on."""
    assert classify(rival(pr_state="MERGED", changed_files_total=None,
                          files_recorded=0)).cls == EXCLUDED


@pytest.mark.parametrize(("total", "recorded", "expected"), [
    (None, 0, False),   # nobody counted, nothing stored
    (None, 5, False),   # nobody counted: cannot be shown complete
    (0, 0, True),       # counted zero, holds zero — a complete empty list
    (2, 2, True),
    (2500, 2, False),   # GitHub's 3,000-file cap, visible
    (2, 3, True),       # more rows than the count: a sender bug, not a prefix
])
def test_complete_means_somebody_counted_and_the_board_holds_that_many(total, recorded, expected):
    assert files_complete(total, recorded) is expected


# ---- the endpoint ----------------------------------------------------------


async def test_a_pr_finds_the_other_prs_that_touch_its_files(client, repo):
    await record(client, repo, 10110, changed_files=files("panel.py", "reviews.py"),
                 changed_files_total=2)
    await record(client, repo, 10111, changed_files=files("panel.py", "epic.py"),
                 changed_files_total=2)
    await record(client, repo, 10112, changed_files=files("lander.py"), changed_files_total=1)
    c = await collisions(client, repo, 10110)
    assert [h["pr"] for h in c["collides"]] == [10111]
    assert c["collides"][0]["files"] == ["panel.py"]
    assert c["collides"][0]["shared"] == 1
    # ...and the one that shares nothing is *stated* as disjoint, not merely absent.
    assert [h["pr"] for h in c["disjoint"]] == [10112]


async def test_more_shared_files_sorts_first(client, repo):
    """A description of the overlap, in the order a reader wants to see it. Not a
    recommendation about which to land — that is #80's and #232's, and it needs a
    policy about what a collision costs that this endpoint does not have."""
    await record(client, repo, 10120, changed_files=files("o/a.py", "o/b.py", "o/c.py"),
                 changed_files_total=3)
    await record(client, repo, 10121, changed_files=files("o/a.py"), changed_files_total=1)
    await record(client, repo, 10122, changed_files=files("o/a.py", "o/b.py"),
                 changed_files_total=2)
    c = await collisions(client, repo, 10120)
    assert [(h["pr"], h["files"]) for h in c["collides"]] == [
        (10122, ["o/a.py", "o/b.py"]), (10121, ["o/a.py"])]


async def test_a_rival_is_represented_by_its_newest_run_outright(client, repo):
    """**Round 1's defect (``88-F06``).** A PR's file set grows while it is open, so
    the newest round is the current answer — and a later round that recorded NO
    list makes the PR unanswerable rather than handing back the older round's
    paths. Preferring the newest *file-bearing* run answers a stale question in a
    confident voice: the PR has been panelled since, that round said nothing about
    files, and quoting the earlier paths hides exactly that."""
    await record(client, repo, 10130, changed_files=files("target.py"), changed_files_total=1)
    await record(client, repo, 10131, round=1, changed_files=files("nothing-shared.py"),
                 changed_files_total=1)
    await record(client, repo, 10131, round=2, changed_files=files("target.py", "more.py"),
                 changed_files_total=2)
    c = await collisions(client, repo, 10130)
    assert [(h["pr"], h["files"]) for h in c["collides"]] == [(10131, ["target.py"])]

    # A later round that recorded nothing moves the rival to `unanswerable` — it
    # does NOT leave round 2's answer standing.
    await record(client, repo, 10131, round=3)
    c = await collisions(client, repo, 10130)
    assert c["collides"] == []
    assert [u["pr"] for u in c["unanswerable"]] == [10131]


async def test_a_rival_merged_after_an_open_round_is_not_answered_by_the_open_one(client, repo):
    """**Round 2's defect (``88-F01``), introduced by round 1's fix.** A rival
    panelled while OPEN, merged, then re-panelled after the merge came back in
    `collides` with `pr_state: "OPEN"` from the stale run — while the docstring
    written in that same commit said "a PR is represented by its newest run
    outright". The state test had been composed in FRONT of the selection, so
    `DISTINCT ON` chose the newest run *that was open* rather than the newest run.

    It cannot recur by construction: the state is read in `classify`, after the
    run is chosen, and there is no predicate in front of the selection to put it
    into."""
    await record(client, repo, 10140, changed_files=files("hot.py"), changed_files_total=1,
                 pr_state="OPEN")
    await record(client, repo, 10141, round=1, changed_files=files("hot.py"),
                 changed_files_total=1, pr_state="OPEN")
    await record(client, repo, 10141, round=2, changed_files=files("hot.py"),
                 changed_files_total=1, pr_state="MERGED")

    c = await collisions(client, repo, 10140)
    assert c["collides"] == []
    [gone] = c["excluded"]
    assert gone["pr"] == 10141
    assert gone["pr_state"] == "MERGED"  # the newest run's state, not the older one's
    assert gone["excluded_because"] == "MERGED"

    # And with the merged ones asked for, it is the MERGED run that answers — the
    # older OPEN one is not reachable through this endpoint at all.
    both = await collisions(client, repo, 10140, include_closed=True)
    assert [h["pr"] for h in both["collides"]] == [10141]
    assert both["collides"][0]["pr_state"] == "MERGED"


async def test_a_rival_that_claims_files_it_never_stored_is_partial_not_absent(client, repo):
    """**``88-F07``** — the same disease from the other side. Round 1's fix made
    "has a file list" true when `changed_files_total IS NOT NULL`, so a rival
    claiming 2,500 files with none stored passed the predicate, contributed no
    join rows, and was silently absent from BOTH lists — read by a caller as
    "answered, and disjoint"."""
    await record(client, repo, 10150, changed_files=files("shared.py"), changed_files_total=1)
    await record(client, repo, 10151, changed_files=[], changed_files_total=2500)
    c = await collisions(client, repo, 10150)
    assert c["collides"] == [] and c["unanswerable"] == [] and c["disjoint"] == []
    [p] = c["partial"]
    assert p["pr"] == 10151
    assert p["changed_files_total"] == 2500 and p["files_recorded"] == 0
    assert p["files_complete"] is False


async def test_every_selected_rival_lands_in_exactly_one_class(client, repo):
    """The property the redesign buys, asserted over a population holding one of
    every shape. `considered` is counted from the selected rows and the five class
    counts are counted from the buckets, so the two agreeing is a real check on
    the ladder rather than a tautology — and no PR may appear twice."""
    await record(client, repo, 10160, changed_files=files("core.py"), changed_files_total=1)
    await record(client, repo, 10161, changed_files=files("core.py"), changed_files_total=1)
    await record(client, repo, 10162, changed_files=files("elsewhere.py"), changed_files_total=1)
    await record(client, repo, 10163)  # a pre-v2.23 run: no list at all
    await record(client, repo, 10164, changed_files=files("a.py"), changed_files_total=99)
    await record(client, repo, 10165, changed_files=files("core.py"), changed_files_total=1,
                 pr_state="MERGED")

    c = await collisions(client, repo, 10160)
    seen = [h["pr"] for cls in CLASSES for h in c[cls]]
    assert sorted(seen) == [10161, 10162, 10163, 10164, 10165]
    assert len(seen) == len(set(seen))  # exactly one bucket each
    assert c["counts"]["considered"] == 5
    assert sum(c["counts"][cls] for cls in CLASSES) == c["counts"]["considered"]
    assert {h["class"] for cls in CLASSES for h in c[cls]} <= set(CLASSES)
    # Each row states its own class, and it is the bucket it came back in.
    for cls in CLASSES:
        assert all(h["class"] == cls for h in c[cls])


async def test_the_subject_reports_the_run_it_answered_from(client, repo):
    """So a caller can see how stale the answer is. The board is told about
    panels, not about pushes — a PR's files are as current as its last round."""
    run = await record(client, repo, 10170, changed_files=files("x.py"), changed_files_total=1)
    c = await collisions(client, repo, 10170)
    assert c["run_id"] == run["id"]
    assert c["ts"] and c["files"] == ["x.py"]
    assert c["changed_files_total"] == 1 and c["files_recorded"] == 1
    assert c["files_complete"] is True


async def test_the_subject_falls_back_past_a_later_run_with_no_list_and_a_rival_does_not(client, repo):
    """The asymmetry is deliberate and needs a test saying so, not just a comment.

    A caller naming a PR is asking about THAT PR, so answering "404, its last
    round recorded nothing" when an earlier round recorded a perfectly good list
    serves nobody. For a rival the same fallback would silently substitute stale
    data into an answer nobody asked to be approximate — the subject's `run_id`
    and `ts` come back so its fallback is visible, and a rival's would not be."""
    first = await record(client, repo, 10180, changed_files=files("f/x.py"), changed_files_total=1)
    await record(client, repo, 10180, round=2)  # a later round that recorded nothing
    c = await collisions(client, repo, 10180)
    assert c["run_id"] == first["id"] and c["files"] == ["f/x.py"]

    # The same history on a RIVAL gets no fallback: it is unanswerable.
    await record(client, repo, 10181, changed_files=files("f/x.py"), changed_files_total=1)
    await record(client, repo, 10181, round=2)
    c = await collisions(client, repo, 10180)
    assert c["collides"] == []
    assert [u["pr"] for u in c["unanswerable"]] == [10181]


async def test_the_subject_fallback_takes_the_newest_list_not_the_best_one(client, repo):
    """It reaches back for a run that recorded *a* list, never for one that
    recorded a COMPLETE list. Preferring the complete one would be this endpoint's
    own disease inside its single sanctioned fallback — the newest list, with
    `files_complete: false` saying it is a prefix, is the honest answer."""
    await record(client, repo, 10185, round=1, changed_files=files("old/full.py"),
                 changed_files_total=1)
    newer = await record(client, repo, 10185, round=2, changed_files=files("new/prefix.py"),
                         changed_files_total=2500)
    c = await collisions(client, repo, 10185)
    assert c["run_id"] == newer["id"]
    assert c["files"] == ["new/prefix.py"] and c["files_complete"] is False


async def test_the_subject_says_when_its_own_list_is_a_prefix(client, repo):
    """A subject holding 1 of 2,500 files under-reports its OWN collisions, and no
    per-rival verdict can see that — it is on the other side of the join. So it is
    a field on the subject, and a caller must read it before trusting anything in
    `disjoint`."""
    await record(client, repo, 10190, changed_files=files("t/one.py"), changed_files_total=2500)
    await record(client, repo, 10191, changed_files=files("t/unrelated.py"), changed_files_total=1)
    c = await collisions(client, repo, 10190)
    assert c["files_complete"] is False
    assert c["changed_files_total"] == 2500 and c["files_recorded"] == 1
    # The rival is complete and shares nothing, so it is reported disjoint — which
    # is exactly as trustworthy as the subject's own list, and no more.
    assert [h["pr"] for h in c["disjoint"]] == [10191]


async def test_a_pr_that_never_recorded_a_file_list_is_a_404_not_an_empty_answer(client, repo):
    """"Nothing to compare" and "compared, found nothing" are different facts, and
    returning the second for the first is how a caller lands a colliding PR
    believing the board cleared it."""
    await record(client, repo, 10200)
    r = await client.get("/review/collisions", params={"repo": repo, "pr": 10200},
                         headers=AGENT)
    assert r.status_code == 404
    assert "changed-file list" in r.json()["detail"]


async def test_a_known_empty_subject_is_answered_rather_than_404(client, repo):
    """``88-F04``, the subject half: a PR that genuinely changed zero files is
    knowledge, and it is disjoint from everything."""
    await record(client, repo, 10205, changed_files=[], changed_files_total=0)
    c = await collisions(client, repo, 10205)
    assert c["files"] == [] and c["changed_files_total"] == 0
    assert c["files_complete"] is True and c["collides"] == []


async def test_a_known_empty_rival_is_disjoint_rather_than_unanswered(client, repo):
    """``88-F04``, the rival half."""
    await record(client, repo, 10210, changed_files=files("e/x.py"), changed_files_total=1)
    await record(client, repo, 10211, changed_files=[], changed_files_total=0)
    c = await collisions(client, repo, 10210)
    assert [h["pr"] for h in c["disjoint"]] == [10211]
    assert c["unanswerable"] == [] and c["collides"] == []


async def test_a_rivals_truncation_is_visible_so_its_overlap_reads_as_a_floor(client, repo):
    """``88-F08``. A rival holding 2 of 2,500 files can share paths it never
    reported, so a caller must be able to see that the shared list is a floor on
    the overlap and not the whole of it."""
    await record(client, repo, 10220, changed_files=files("p/one.py"), changed_files_total=1)
    await record(client, repo, 10221, changed_files=files("p/one.py", "p/two.py"),
                 changed_files_total=2500)
    [hit] = (await collisions(client, repo, 10220))["collides"]
    assert hit["pr"] == 10221
    assert hit["changed_files_total"] == 2500 and hit["files_recorded"] == 2
    assert hit["files_complete"] is False
    assert hit["files"] == ["p/one.py"] and hit["shared"] == 1


async def test_a_merged_rival_is_set_aside_and_says_why(client, repo):
    """``88-F02``'s original point: `review_runs` held no PR state at all, so every
    PR panelled inside the window read as a live rival — merged ones included. On
    a repo landing several a week that is most of the list, which is how an
    advisory endpoint stops being read."""
    await record(client, repo, 10230, changed_files=files("m/shared.py"),
                 changed_files_total=1, pr_state="OPEN")
    await record(client, repo, 10231, changed_files=files("m/shared.py"),
                 changed_files_total=1, pr_state="MERGED")
    c = await collisions(client, repo, 10230)
    assert c["collides"] == []
    assert [(h["pr"], h["excluded_because"]) for h in c["excluded"]] == [(10231, "MERGED")]


async def test_a_rival_with_no_state_recorded_is_kept_not_filtered_away(client, repo):
    """The NULL case, which decides whether the state test is safe to make at all.
    Dropping the runs that state no PR state would silently narrow the answer to
    PRs panelled since v2.23 — a filter quietly becoming a cutoff."""
    await record(client, repo, 10240, changed_files=files("s/shared.py"),
                 changed_files_total=1, pr_state="OPEN")
    await record(client, repo, 10241, changed_files=files("s/shared.py"), changed_files_total=1)
    hits = (await collisions(client, repo, 10240))["collides"]
    assert [h["pr"] for h in hits] == [10241]
    assert hits[0]["pr_state"] is None


async def test_a_draft_rival_collides_unless_the_caller_sets_drafts_aside(client, repo):
    """A draft is open and not landing yet, which is a different thing to collide
    with — but it is the caller's call, so it is a flag and not a default. Its
    `pr_state` is `OPEN`, so nothing about this is inferable from the state."""
    await record(client, repo, 10250, changed_files=files("d/shared.py"), changed_files_total=1,
                 pr_state="OPEN")
    await record(client, repo, 10251, changed_files=files("d/shared.py"), changed_files_total=1,
                 pr_state="OPEN", is_draft=True)
    assert [h["pr"] for h in (await collisions(client, repo, 10250))["collides"]] == [10251]
    c = await collisions(client, repo, 10250, exclude_drafts=True)
    assert c["collides"] == []
    assert [(h["pr"], h["excluded_because"]) for h in c["excluded"]] == [(10251, "DRAFT")]
    assert c["excluded"][0]["is_draft"] is True


async def test_an_unanswerable_rival_carries_enough_to_judge_why(client, repo):
    """``88-F09``. The unanswered list is argued to be the half that matters most,
    and it was a bare list of ints — a caller could not tell a month-old harness
    from an hour-old failure, nor render a title beside the number."""
    await record(client, repo, 10260, changed_files=files("u/x.py"), changed_files_total=1)
    await record(client, repo, 10261, pr_title="a PR nobody recorded files for")
    [u] = (await collisions(client, repo, 10260))["unanswerable"]
    assert u["pr"] == 10261 and u["pr_title"] == "a PR nobody recorded files for"
    assert u["run_id"] and u["ts"]
    assert u["files_recorded"] == 0 and u["changed_files_total"] is None


async def test_the_window_bounds_which_rival_prs_answer(client, repo):
    """A repo accumulates PRs forever, and a collision with one panelled months ago
    is not a fact about landing this one. The window bounds RIVALS only — it never
    bounds how far back the board may look to learn what the subject touches."""
    await record(client, repo, 10270, changed_files=files("hot.py"), changed_files_total=1)
    await record(client, repo, 10271, changed_files=files("hot.py"), changed_files_total=1)
    assert [h["pr"] for h in (await collisions(client, repo, 10270, days=30))["collides"]] == [10271]

    far = await collisions(client, repo, 10270, since="2099-01-01T00:00:00Z")
    assert far["counts"]["considered"] == 0
    assert all(far[cls] == [] for cls in CLASSES)
    # The subject is still answered for: its own run predates that cutoff.
    assert far["files"] == ["hot.py"]
    assert far["window"]["since"] == "2099-01-01T00:00:00Z"
    assert far["window"]["cutoff"].startswith("2099-01-01")


async def test_a_pr_never_collides_with_itself(client, repo):
    await record(client, repo, 10280, changed_files=files("solo.py"), changed_files_total=1)
    await record(client, repo, 10280, round=2, changed_files=files("solo.py"),
                 changed_files_total=1)
    c = await collisions(client, repo, 10280)
    assert c["counts"]["considered"] == 0
    assert all(10280 not in [h["pr"] for h in c[cls]] for cls in CLASSES)


async def test_every_class_is_capped_and_says_how_much_it_dropped(client, repo):
    """``88-F04``: `collides` had a cap and the unanswered list did not, and the
    unanswered list is by construction the LARGER of the two — on the day this
    ships, every PR the board has ever panelled. `days=3650` is a permitted
    argument and an automated lander issues this in a loop. So the cap is per
    class, on every class, and each says what it dropped."""
    await record(client, repo, 10290, changed_files=files("l/hot.py"), changed_files_total=1)
    for other in (10291, 10292, 10293):
        await record(client, repo, other, changed_files=files("l/hot.py"), changed_files_total=1)
    for other in (10294, 10295, 10296):
        await record(client, repo, other)  # unanswerable

    c = await collisions(client, repo, 10290, limit=2)
    assert len(c["collides"]) == 2 and c["collides_dropped"] == 1
    assert len(c["unanswerable"]) == 2 and c["unanswerable_dropped"] == 1
    # The counts are pre-cap: a cap trims the list it is on and is never visible
    # in the arithmetic.
    assert c["counts"]["collides"] == 3 and c["counts"]["unanswerable"] == 3
    assert c["counts"]["considered"] == 6


async def test_a_shared_path_list_is_capped_and_says_so(client, repo):
    """A rival may share thousands of paths, and this endpoint is issued in a loop
    over every PR the board has panelled — so the per-row list is bounded too. The
    COUNT is what a ranking function weighs by and it is never trimmed."""
    paths = [f"c/f{i:04d}.py" for i in range(SHARED_FILES_CAP + 5)]
    await record(client, repo, 10300, changed_files=files(*paths), changed_files_total=len(paths))
    await record(client, repo, 10301, changed_files=files(*paths), changed_files_total=len(paths))
    [hit] = (await collisions(client, repo, 10300))["collides"]
    assert hit["shared"] == len(paths)  # never trimmed
    assert len(hit["files"]) == SHARED_FILES_CAP
    assert hit["files_dropped"] == 5
    assert hit["files"] == paths[:SHARED_FILES_CAP]  # sorted, and the first of them
    # The subject's own list is capped by the same rule, and says so the same way.
    c = await collisions(client, repo, 10300)
    assert len(c["files"]) == SHARED_FILES_CAP and c["files_dropped"] == 5


async def test_the_response_says_which_population_it_speaks_for(client, repo):
    """``88-F01`` of round 1, resolved by narrowing the claim rather than by
    pretending. A PR this board never panelled cannot appear in any class here,
    and that limit is not discoverable from the numbers — so it is stated in the
    response, not only in the docs. It is permanent until #80 decides otherwise."""
    await record(client, repo, 10310, changed_files=files("sc/x.py"), changed_files_total=1)
    assert (await collisions(client, repo, 10310))["scope"] == \
        "PRs this board has panelled within the window"


async def test_the_endpoint_is_not_swallowed_by_the_run_id_route(client, repo):
    """`/review/{run_id}` takes an int and is declared in the same router, so a
    reordering that put it first would make this endpoint 422 on every request."""
    r = await client.get("/review/collisions", params={"repo": repo, "pr": 999999},
                         headers=AGENT)
    assert r.status_code == 404  # not 422: the collisions route matched
    assert "nothing to compare" in r.json()["detail"]


async def test_collisions_are_scoped_to_one_repo(client, repo):
    """Two repos may both have a `panel.py`, and they are not the same file."""
    await record(client, repo, 10320, changed_files=files("panel.py"), changed_files_total=1)
    r = await client.post(
        "/review",
        json=payload(f"{repo}-elsewhere", 10321,
                     changed_files=files("panel.py"), changed_files_total=1),
        headers=AGENT,
    )
    assert r.status_code == 201, r.text
    c = await collisions(client, repo, 10320)
    assert all(10321 not in [h["pr"] for h in c[cls]] for cls in CLASSES)


async def test_reading_a_collision_answer_needs_auth(client, repo):
    r = await client.get("/review/collisions", params={"repo": repo, "pr": 10320})
    assert r.status_code == 401
