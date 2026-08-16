"""v2.38: one repo, one namespace — the allocator stops keying on caller spelling.

The claim table's atomicity is a partial unique index over `(kind, key)`, and a
unique index is only unique within a spelling. The key is built from a repo
string the caller supplies as free text, and this fleet supplies two of them for
one repo, each locally correct: `qb-hook` takes the origin remote's **basename**
(`quarterback`) because a checkout cloned under another local name is still the
same repo, while `gh` and every review payload use GitHub's **nameWithOwner**
(`prisonblues/quarterback`) because that is what `POST /review` documents.

So the board kept two independent release sequences over one repo and, on
2026-08-16, **handed 2.36 to two agents 28 minutes apart with `claimed: true` on
both** — the tenth release collision, and the first the allocator itself
produced. That is #148/#150, and it is worse than the announcement v2.31
replaced: an announcement leaves the caller uncertain, and this one did not.

The properties under test:

* **One floor per repo, whatever the caller called it.** The regression test is
  the collision itself, spelled two ways.
* **A basename is a lookup, not a namespace.** Expanded when exactly one owner
  answers to it; refused when none or several do, because coining a namespace is
  the failure being fixed and picking an owner is a guess.
* **The read side agrees with the write side.** The bug was found by reading
  `/releases` back under one spelling and not seeing a number known to be held.
* **A generic key is the caller's own vocabulary.** `kind='deploy',
  key='portainer-stack-189'` names no repo and must come back untouched — a
  normaliser that mangles it is a worse bug than the one it fixes.
* **History is rewritten, and the collision inside it is preserved.** Two live
  rows converging on one key is not a migration artefact; it is the bug's output.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.db import engine
from app.repokey import (
    LeaseRow,
    canonical_repo,
    like_escape,
    name_half,
    plan_rewrites,
    repo_basename,
    resolve_against,
    split_repo_head,
    version_tail,
)

from .conftest import DESKTOP, LAPTOP


async def alloc(client, repo: str, headers=LAPTOP, **over):
    return await client.post("/release/claim", json={"repo": repo, **over},
                             headers=headers)


async def took(client, repo: str, headers=LAPTOP, **over) -> dict:
    r = await alloc(client, repo, headers=headers, **over)
    assert r.status_code == 200, r.text
    return r.json()


async def claim(client, kind: str, key: str, headers=LAPTOP, **over):
    return await client.post("/claim", json={"kind": kind, "key": key, **over},
                             headers=headers)


async def seed_review_run(repo: str) -> None:
    """A review run, which is where `nameWithOwner` is documented to live."""
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO review_runs (author, repo, pr) VALUES (:a, :r, 1)"),
            {"a": "zeus/fern-hazel", "r": repo})


async def seed_legacy_release(key: str) -> None:
    """A release row keyed the pre-v2.38 way, which no endpoint can write now."""
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO resource_leases "
                 "(id, kind, key, holder, ttl_seconds, acquired_at, expires_at) "
                 "VALUES (gen_random_uuid(), 'release', :k, 'zeus', 3600, :t, :e)"),
            {"k": key, "t": now, "e": now + timedelta(hours=1)})


# ------------------------------------------------------ reading a repo string


@pytest.mark.parametrize("given", [
    "prisonblues/quarterback",
    "PrisonBlues/Quarterback",
    "prisonblues/quarterback.git",
    "prisonblues/quarterback/",
    "/prisonblues/quarterback",
    "https://github.com/prisonblues/quarterback",
    "https://github.com/prisonblues/quarterback.git",
    "git@github.com:prisonblues/quarterback.git",
    "ssh://git@github.com/prisonblues/quarterback",
    "github.com/prisonblues/quarterback",
])
def test_every_spelling_of_one_repo_lands_on_one_key(given):
    """Callers derive repo identity from `git remote get-url`, so a URL reaching
    the board is a spelling and not a mistake. Case is folded because GitHub
    resolves owners and names case-insensitively, and a third fork of the
    namespace is exactly what this release exists to prevent."""
    assert canonical_repo(given) == "prisonblues/quarterback"


@pytest.mark.parametrize("given", [
    "quarterback",          # the basename: a lookup key, not a namespace
    "group/sub/repo",       # not GitHub's grammar; guessing which two is a guess
    "../etc/passwd",        # a segment that cannot start a repo name
    "",
    "/",
])
def test_a_string_that_is_not_owner_slash_name_is_not_canonicalised(given):
    assert canonical_repo(given) is None


def test_the_basename_is_read_back_as_a_lookup_key():
    assert repo_basename("quarterback") == "quarterback"
    assert repo_basename("Quarterback.git") == "quarterback"
    assert repo_basename("prisonblues/quarterback") is None
    assert name_half("prisonblues/quarterback") == "quarterback"


@pytest.mark.parametrize("key,head,rest", [
    ("prisonblues/quarterback#142", "prisonblues/quarterback", "#142"),
    ("prisonblues/quarterback:main", "prisonblues/quarterback", ":main"),
    ("quarterback:2.36", "quarterback", ":2.36"),
    ("portainer-stack-189", "portainer-stack-189", ""),
    # A host's colon is not the key's separator, and a key that looks like a
    # remote must not be sliced at it — `git` is not a repo head.
    ("git@github.com:acme/thing", "git@github.com:acme/thing", ""),
    ("https://github.com/acme/thing", "https://github.com/acme/thing", ""),
])
def test_a_key_splits_at_the_resource_and_not_at_a_host(key, head, rest):
    assert split_repo_head(key) == (head, rest)


def test_the_version_is_read_off_the_end_not_off_a_known_prefix():
    """A legacy row is not keyed on the spelling the caller asked with, so
    removing a `repo:` prefix mis-slices it — and a mis-sliced key parses as
    nothing and drops out of the floor, which is how a number gets re-issued."""
    assert version_tail("prisonblues/quarterback:2.36") == "2.36"
    assert version_tail("quarterback:2.36") == "2.36"


def test_like_wildcards_in_a_repo_name_are_escaped():
    """`_` and `%` are LIKE wildcards and both occur in real repo names, so
    `acme/my_repo` matched `acme/myXrepo` — one repo's floor raised by another's."""
    assert like_escape("acme/my_repo") == "acme/my\\_repo"
    assert like_escape("acme/100%") == "acme/100\\%"


def test_a_basename_expands_only_when_exactly_one_owner_answers_to_it():
    known = {"acme/thing", "other/thing", "acme/lonely"}
    assert resolve_against("acme/thing", known) == ("acme/thing", [])
    assert resolve_against("lonely", known) == ("acme/lonely", ["acme/lonely"])
    # Two owners: a refusal that names the choices, never a pick.
    repo, candidates = resolve_against("thing", known)
    assert repo is None and candidates == ["acme/thing", "other/thing"]
    assert resolve_against("unknown", known) == (None, [])


# ------------------------------------------------------------ the collision


async def test_two_spellings_of_one_repo_share_one_floor(client):
    """**The bug, reproduced.** Before this release the second call was handed
    2.1 again — a fresh number by its own reckoning, and one already spoken for.
    Both calls reported `claimed: true`, which is why nobody noticed for 28
    minutes."""
    await seed_review_run("acme/nsone")
    first = await took(client, "acme/nsone", session="s-full")
    assert first["version"] == "0.1"

    second = await took(client, "nsone", headers=DESKTOP, session="s-base")
    assert second["version"] == "0.2", "the basename must not open a second floor"
    assert second["repo"] == "acme/nsone"
    assert second["repo_as_given"] == "nsone"
    assert second["key"] == "acme/nsone:0.2"


async def test_a_url_and_a_mixed_case_spelling_are_the_same_namespace(client):
    await seed_review_run("acme/nstwo")
    await took(client, "https://github.com/acme/nstwo.git", session="s-url")
    third = await took(client, "Acme/NsTwo", headers=DESKTOP, session="s-case")
    assert third["version"] == "0.2"
    assert third["repo"] == "acme/nstwo"


async def test_the_read_side_agrees_with_the_write_side(client):
    """#148 was found this way round: a caller allocated a number, read the list
    back under the other spelling and did not see one it knew was held. A read
    that can still disagree leaves the detection half as broken as the allocation
    half was."""
    await seed_review_run("acme/nsthree")
    await took(client, "acme/nsthree", session="s-1")

    r = await client.get("/releases", params={"repo": "nsthree"}, headers=LAPTOP)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["repo"] == "acme/nsthree"
    assert body["repo_as_given"] == "nsthree"
    assert body["highest_known"] == "0.1"
    assert [x["version"] for x in body["releases"]] == ["0.1"]


async def test_a_number_stranded_under_the_old_spelling_still_raises_the_floor(client):
    """Migration 0020 rewrites what it can resolve, and a basename it could not
    expand stays where it is. A number that has fallen out of the floor is a
    number this board hands out twice, so the read side also looks in the bucket
    the write side can no longer reach."""
    await seed_review_run("acme/nsfour")
    await seed_legacy_release("nsfour:2.9")

    got = await took(client, "acme/nsfour", session="s-legacy")
    assert got["version"] == "2.10", "the legacy row is part of this repo's history"


# ------------------------------------------------------ refusing, not guessing


async def test_a_basename_no_owner_answers_to_is_refused_with_the_form_that_works(client):
    """Silently coining a third namespace is the failure being fixed, and it
    fails with `claimed: true` on it. #148 asks for a refusal naming the expected
    form instead."""
    r = await alloc(client, "nobodyknowsthisrepo")
    assert r.status_code == 400, r.text
    d = r.json()["detail"]
    assert d["repo"] == "nobodyknowsthisrepo"
    assert "owner/name" in d["expected"]
    assert d["candidates"] == []


async def test_an_ambiguous_basename_names_the_candidates_rather_than_picking(client):
    """`prisonblues/quarterback` and `someone-else/quarterback` collapsing onto
    one key is a worse bug hiding in the same place, so the basename is only ever
    a lookup — and a lookup with two answers is a question for the caller."""
    await seed_review_run("owner-a/nsdup")
    await seed_review_run("owner-b/nsdup")

    r = await alloc(client, "nsdup")
    assert r.status_code == 400, r.text
    d = r.json()["detail"]
    assert d["candidates"] == ["owner-a/nsdup", "owner-b/nsdup"]
    assert "say which" in d["hint"]


# --------------------------------------------------- the other kinds of claim


async def test_one_issue_claimed_under_two_spellings_is_one_claim(client):
    """`kind='work'` and `kind='merge'` are keyed the same way, so a claim on an
    un-normalised repo name was not exclusive at all: two agents claiming #142
    under different spellings both succeeded, and each was told it held it."""
    await seed_review_run("acme/nswork")
    mine = await claim(client, "work", "acme/nswork#142", note="mine")
    assert mine.status_code == 200, mine.text

    theirs = await claim(client, "work", "nswork#142", headers=DESKTOP)
    assert theirs.status_code == 409, theirs.text
    assert theirs.json()["detail"]["key"] == "acme/nswork#142"


async def test_your_own_claim_is_found_under_either_spelling(client):
    await seed_review_run("acme/nsrenew")
    first = (await claim(client, "work", "acme/nsrenew#7", note="first")).json()
    again = await claim(client, "work", "nsrenew#7", note="second")
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["renewed"] is True
    assert body["claim_id"] == first["claim_id"], "the same row, not a second claim"
    assert body["key"] == "acme/nsrenew#7"
    assert body["key_as_given"] == "nsrenew#7", "told, not silently rewritten"


async def test_a_key_that_names_no_repo_is_left_exactly_as_sent(client):
    """The generic key is the caller's own vocabulary by design. Refusing or
    rewriting one the board cannot identify would break every claim that is not
    about a repo — and that asymmetry with `/release/claim` is deliberate, since
    `repo` there is a typed field documented as a repo."""
    r = await claim(client, "deploy", "portainer-stack-189")
    assert r.status_code == 200, r.text
    assert r.json()["key"] == "portainer-stack-189"
    assert "key_as_given" not in r.json()


async def test_a_lookup_by_the_spelling_you_claimed_with_finds_the_row(client):
    await seed_review_run("acme/nslookup")
    await claim(client, "merge", "acme/nslookup:main", note="landing")
    r = await client.get("/claims", params={"key": "nslookup:main"}, headers=LAPTOP)
    assert [c["key"] for c in r.json()["claims"]] == ["acme/nslookup:main"]


async def test_a_renumber_off_a_legacy_key_is_not_refused_as_another_repos(client):
    """The claim being given up may predate v2.38 and be keyed on the other
    spelling of this very repo. Refusing that as "another repo's claim" would
    strand exactly the rows #148 is about, at the moment their holder is trying
    to get off a collision."""
    await seed_review_run("acme/nsreclaim")
    held = await took(client, "acme/nsreclaim", session="s-r", after="2.40")
    assert held["version"] == "2.41"

    r = await client.post("/release/reclaim", headers=LAPTOP, json={
        "repo": "nsreclaim", "claim_id": held["claim_id"],
        "session": "s-r", "after": "2.41"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["gave_up"] == "2.41"
    assert body["version"] == "2.42"
    assert body["repo"] == "acme/nsreclaim"


# ------------------------------------------------------- rewriting the history


def _row(key, *, kind="release", at=1, held=True):
    return LeaseRow(id=f"id-{key}-{at}", kind=kind, key=key,
                    acquired_at=datetime(2026, 8, 16, at, tzinfo=UTC), held=held)


def test_the_migration_rewrites_what_it_can_and_leaves_what_it_cannot():
    known = {"prisonblues/quarterback"}
    rows = [
        _row("quarterback:2.32", at=1),
        _row("PrisonBlues/Quarterback:2.33", at=2),
        _row("prisonblues/quarterback:2.35", at=3),
        _row("mysteryrepo:1.4", at=4),
        _row("portainer-stack-189", kind="deploy", at=5),
    ]
    plans = {p.old_key: p for p in plan_rewrites(rows, known)}
    assert plans["quarterback:2.32"].new_key == "prisonblues/quarterback:2.32"
    assert plans["PrisonBlues/Quarterback:2.33"].new_key == "prisonblues/quarterback:2.33"
    # Untouched, and for two different reasons: one is already canonical, one
    # names a repo nobody can identify, and one is not about a repo at all.
    assert "prisonblues/quarterback:2.35" not in plans
    assert "mysteryrepo:1.4" not in plans
    assert "portainer-stack-189" not in plans
    assert not any(p.release for p in plans.values())


def test_two_live_rows_converging_on_one_number_keep_the_earlier_claim():
    """2.36 was held twice on the day this was written, which the partial unique
    index cannot represent. So the later-acquired row is released as part of the
    rewrite — it keeps its canonical key, because history has to record that the
    number went out twice or the floor forgets it, and it stops being live."""
    rows = [
        _row("prisonblues/quarterback:2.36", at=1),   # hazel-jasper, first
        _row("quarterback:2.36", at=2),               # nimbus-sorrel, 28 min later
    ]
    plans = {p.old_key: p for p in plan_rewrites(rows, {"prisonblues/quarterback"})}
    loser = plans["quarterback:2.36"]
    assert loser.new_key == "prisonblues/quarterback:2.36"
    assert loser.release is True
    # Named by WHEN the winner was taken, not by its key: both spellings are the
    # same string by this point, so quoting it twice would explain nothing.
    assert "already held since 2026-08-16T01:00:00+00:00" in loser.reason
    assert "quarterback:2.36" in loser.reason
    assert "prisonblues/quarterback:2.36" not in plans, "the earlier claim is untouched"


def test_a_released_row_never_blocks_a_rewrite():
    """The unique index is partial over unreleased rows, so history can carry the
    same key twice — and it must, or the number drops out of the floor."""
    rows = [
        _row("prisonblues/quarterback:2.36", at=1, held=False),
        _row("quarterback:2.36", at=2),
    ]
    plans = plan_rewrites(rows, {"prisonblues/quarterback"})
    assert [(p.new_key, p.release) for p in plans] == [
        ("prisonblues/quarterback:2.36", False)]


def test_every_release_is_planned_before_any_rewrite():
    """The order is part of the contract, and getting it wrong aborts the whole
    migration on exactly the rows it exists for. Here the EARLIER claim is the
    one that has to move: applying its rewrite while the later duplicate still
    holds the canonical key hits the unique index, because the plan has decided
    the loser should let go but nothing has told the database yet."""
    rows = [
        _row("quarterback:2.36", at=1),                # earlier: keeps the seat, moves
        _row("prisonblues/quarterback:2.36", at=2),    # later: already canonical, loses
    ]
    plans = plan_rewrites(rows, {"prisonblues/quarterback"})
    assert [p.release for p in plans] == [True, False], "releases first, always"
    assert plans[0].old_key == "prisonblues/quarterback:2.36"
    assert plans[1].new_key == "prisonblues/quarterback:2.36"


def test_an_unswept_row_still_occupies_its_key():
    """`held` is `released_at IS NULL` — the unique index's own predicate — and
    NOT "is this claim live". The index cannot test `expires_at`, so an
    expired-but-unswept row is still in it. Planning against liveness would
    rewrite a second row onto the same key and abort. The migration sweeps first
    so the two coincide; this asserts the planner does not assume that for it."""
    rows = [
        _row("prisonblues/quarterback:2.36", at=1),   # unswept, expired or not
        _row("quarterback:2.36", at=2),
    ]
    plans = plan_rewrites(rows, {"prisonblues/quarterback"})
    assert [(p.old_key, p.release) for p in plans] == [("quarterback:2.36", True)]


def test_a_collision_between_two_kinds_is_not_a_collision():
    """The index is on `(kind, key)`. A merge claim and a work claim that happen
    to normalise to the same key are two different resources."""
    rows = [
        _row("acme/thing:main", kind="merge", at=1),
        _row("thing:main", kind="work", at=2),
    ]
    plans = plan_rewrites(rows, {"acme/thing"})
    assert [(p.new_key, p.release) for p in plans] == [("acme/thing:main", False)]
