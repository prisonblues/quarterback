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

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from app.db import engine
from app.repokey import (
    LeaseRow,
    canonical_key_of,
    canonical_repo,
    identified_repo,
    known_repos_from,
    like_escape,
    lookup_name,
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


async def seed_legacy_release(key: str, *, holder="zeus", session=None) -> str:
    """A release row keyed the pre-v2.38 way, which no endpoint can write now.

    Returns its claim id, so a test can renumber off it — the one thing the
    holder of a stranded number actually needs to do.
    """
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        row = await conn.execute(
            text("INSERT INTO resource_leases "
                 "(id, kind, key, holder, session, ttl_seconds, acquired_at, expires_at) "
                 "VALUES (gen_random_uuid(), 'release', :k, :h, :s, 3600, :t, :e) "
                 "RETURNING id"),
            {"k": key, "h": holder, "s": session, "t": now, "e": now + timedelta(hours=1)})
        return str(row.scalar_one())


# ------------------------------------------------------ reading a repo string


@pytest.mark.parametrize("given", [
    "prisonblues/quarterback",
    "PrisonBlues/Quarterback",
    "prisonblues/quarterback.git",
    "prisonblues/quarterback.GIT",
    "prisonblues/Quarterback.Git",
    "prisonblues/quarterback/",
    "https://github.com/prisonblues/quarterback",
    "https://github.com/prisonblues/quarterback.git",
    "git@github.com:prisonblues/quarterback.git",
    "ssh://git@github.com/prisonblues/quarterback",
    "git+ssh://git@github.com/prisonblues/quarterback.git",
    "github.com/prisonblues/quarterback",
    "192.168.1.10/prisonblues/quarterback",
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
    "my_owner/repo",        # a GitHub owner cannot contain `_` — a repo name can
    "my.owner/repo",        # nor `.`, which is what makes a dotted head a host
    "github.com/quarterback",   # a host and a BASENAME, not an owner and a name
    "/prisonblues/quarterback",  # an absolute path is a path
    "/etc/passwd",
    "file:///etc/passwd",   # every scheme accepted was every scheme laundered
    "svn://host/acme/repo",
    "",
    "/",
])
def test_a_string_that_is_not_owner_slash_name_is_not_canonicalised(given):
    assert canonical_repo(given) is None


def test_a_path_cannot_launder_itself_into_an_identity():
    """`canonical_repo('/etc/passwd')` and `canonical_repo('file:///etc/passwd')`
    both used to answer `etc/passwd`: the parser stripped leading slashes and
    accepted every URI scheme while throwing the authority away. A real remote
    always carries a git scheme or scp syntax, so refusing both costs nothing."""
    assert canonical_repo("/etc/passwd") is None
    assert canonical_repo("file:///etc/passwd") is None
    assert repo_basename("/passwd") is None
    # ...and the host is still discarded for the schemes that ARE git's, which is
    # deliberate and stated in the module docstring: this board is single-forge.
    assert canonical_repo("git@bitbucket.org:acme/thing") == "acme/thing"
    assert canonical_repo("https://gitlab.com/acme/thing") == "acme/thing"


def test_a_two_segment_host_is_a_host_and_not_an_owner():
    """`github.com/quarterback` canonicalised with `github.com` as its owner,
    because the host strip only ran at three segments or more. GitHub owners
    cannot contain a dot, so a dotted leading segment is never an owner — at two
    segments exactly as much as at three."""
    assert canonical_repo("github.com/quarterback") is None
    assert repo_basename("github.com/quarterback") == "quarterback"
    assert repo_basename("192.168.1.10/quarterback") == "quarterback"
    # A repo NAME may carry the dot an owner may not.
    assert canonical_repo("acme/repo.io") == "acme/repo.io"
    assert canonical_repo("acme/my_repo") == "acme/my_repo"


def test_the_basename_is_read_back_as_a_lookup_key():
    assert repo_basename("quarterback") == "quarterback"
    assert repo_basename("Quarterback.git") == "quarterback"
    assert repo_basename("Quarterback.GIT") == "quarterback", "the fold comes first"
    assert repo_basename("prisonblues/quarterback") is None
    assert name_half("prisonblues/quarterback") == "quarterback"
    assert lookup_name("prisonblues/quarterback") == "quarterback"
    assert lookup_name("Quarterback") == "quarterback"
    assert lookup_name("portainer-stack-189") == "portainer-stack-189"
    assert lookup_name("/etc/passwd") is None


@pytest.mark.parametrize("key,head,rest", [
    ("prisonblues/quarterback#142", "prisonblues/quarterback", "#142"),
    ("prisonblues/quarterback:main", "prisonblues/quarterback", ":main"),
    ("quarterback:2.36", "quarterback", ":2.36"),
    ("portainer-stack-189", "portainer-stack-189", ""),
    # A host's colon is not the key's separator, and a key that looks like a
    # remote must not be sliced at it — `git` is not a repo head.
    ("git@github.com:acme/thing", "git@github.com:acme/thing", ""),
    ("https://github.com/acme/thing", "https://github.com/acme/thing", ""),
    # ...but the separator AFTER the authority is still the separator. Returning
    # any key containing `://` or `@` whole left these opaque to the migration
    # and to every prefix scan, so the number in them could be issued again.
    ("https://github.com/acme/thing:2.36", "https://github.com/acme/thing", ":2.36"),
    ("git@github.com:acme/thing#142", "git@github.com:acme/thing", "#142"),
    ("ssh://git@github.com/acme/thing:main", "ssh://git@github.com/acme/thing", ":main"),
    # An `@` with no colon in front of it is a resource separator, not an
    # authority: any convention using it used to opt out of the fix silently.
    ("acme/thing@v1.2", "acme/thing", "@v1.2"),
])
def test_a_key_splits_at_the_resource_and_not_at_a_host(key, head, rest):
    assert split_repo_head(key) == (head, rest)


def test_a_url_key_is_the_same_release_as_the_plain_one():
    """The whole reason `split_repo_head` has to parse the authority: a legacy
    `https://github.com/acme/repo:2.36` that nothing can slice is a number no
    scan can see, and a number no scan can see is a number handed out twice."""
    assert canonical_repo(split_repo_head("https://github.com/acme/repo:2.36")[0]) \
        == "acme/repo"
    assert version_tail("https://github.com/acme/repo:2.36") == "2.36"
    assert canonical_key_of("https://github.com/acme/repo:2.36", {"acme/repo"}) \
        == "acme/repo:2.36"


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
    # Candidates are for refusals only, so a success is `(repo, [])` however it
    # was reached — the two elements never both carry information.
    assert resolve_against("acme/thing", known) == ("acme/thing", [])
    assert resolve_against("lonely", known) == ("acme/lonely", [])
    # Two owners: a refusal that names the choices, never a pick.
    repo, candidates = resolve_against("thing", known)
    assert repo is None and candidates == ["acme/thing", "other/thing"]
    assert resolve_against("unknown", known) == (None, [])


def test_a_repo_the_board_has_never_seen_is_not_identified():
    """`resolve_against` accepts any `owner/name`, which is right where the caller
    declared the field to be a repo. `identified_repo` does not, which is right
    where the string is the caller's own vocabulary: without the second rule any
    two-segment key was "a repo", so `Prod/Blue:resource` was quietly rewritten
    to `prod/blue:resource` and two distinct resources became one claim."""
    known = {"acme/thing"}
    assert identified_repo("acme/thing", known) == "acme/thing"
    assert identified_repo("Acme/Thing", known) == "acme/thing"
    assert identified_repo("thing", known) == "acme/thing"
    assert identified_repo("prod/blue", known) is None
    assert canonical_key_of("Prod/Blue:resource", known) == "Prod/Blue:resource"
    assert canonical_key_of("Thing:main", known) == "acme/thing:main"


def test_the_expansion_table_is_built_only_from_what_it_is_given():
    """The table used to be every `resource_leases` key regardless of kind, so a
    legal `kind='deploy', key='attacker/thing#1'` minted the repo identity
    `attacker/thing` — and a later release request for the bare basename `thing`
    was routed to it, or refused as ambiguous, which is a denial of service on
    somebody else's basename. Both remaining sources are written by machinery
    rather than by asking."""
    assert known_repos_from(["acme/thing", "acme/other:2.1", "acme/x#7"]) == {
        "acme/thing", "acme/other", "acme/x"}
    # Rows that name no repo, and non-strings, are simply not repos.
    assert known_repos_from(["portainer-stack-189", None, "/etc/passwd"]) == set()
    # A full remote URL in `review_runs.repo` still resolves — the old pattern
    # `%/name` matched neither that nor a trailing slash.
    assert known_repos_from(["https://github.com/acme/thing.git", "acme/thing/"]) == {
        "acme/thing"}


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


async def test_a_legacy_row_in_the_remotes_own_case_still_raises_the_floor(client):
    """`qb-hook` takes the origin remote's basename verbatim and repo names are
    commonly mixed case, so the rows 0020 leaves alone are exactly the ones most
    likely to be capitalised. A case-SENSITIVE legacy clause could not see
    `NsCase:2.9` at all: the number fell out of the floor and the allocator
    re-issued it — the failure this release exists to prevent, in the one code
    path written specifically to prevent it."""
    await seed_review_run("acme/nscase")
    await seed_legacy_release("NsCase:2.9")

    got = await took(client, "acme/nscase", session="s-case")
    assert got["version"] == "2.10"


async def test_a_legacy_row_raises_the_floor_and_belongs_to_nobody(client):
    """The bare-basename bucket is unattributable by construction: `nsown:2.9`
    could be either owner's. Reading it into the floor is conservative and right;
    reading it into an OWNERSHIP answer hands one owner the other's live claim as
    its own idempotent allocation, and lists a neighbour's numbers as this
    repo's. So `highest_known` may exceed every version listed, and that gap is
    the honest answer rather than a bug."""
    await seed_review_run("acme/nsown")
    await seed_legacy_release("nsown:2.9", holder="laptop", session="s-own")

    r = await client.get("/releases", params={"repo": "acme/nsown"}, headers=LAPTOP)
    body = r.json()
    assert body["releases"] == [], "an unattributable row is not this repo's"
    assert body["highest_known"] == "2.9", "...but it may still be taken"

    got = await took(client, "acme/nsown", session="s-own")
    assert got["version"] == "2.10", "not handed back as this session's own claim"
    assert got["renewed"] is False


async def test_one_repos_underscore_does_not_raise_anothers_floor(client):
    """`_` is a LIKE wildcard and legal in a repo name, so `acme/ns_wild` matched
    `acme/nsxwild` — one repo's allocation floor raised by another's (v2.33's
    F19). Asserted end to end here, not only over the escaper."""
    await seed_review_run("acme/ns_wild")
    await seed_review_run("acme/nsxwild")
    for _ in range(3):
        await took(client, "acme/nsxwild")

    got = await took(client, "acme/ns_wild")
    assert got["version"] == "0.1"


# ---------------------------------------- what a generic claim must not be able to do


async def test_a_generic_claim_cannot_mint_a_repo_identity(client):
    """The expansion table used to be every claim key regardless of kind, so a
    perfectly legal `kind='deploy', key='attacker/nspoison#1'` taught the board
    that `attacker/nspoison` is a repo — and the next release request for the
    bare basename was routed into it, or refused as ambiguous, which is a denial
    of service on somebody else's basename."""
    minted = await claim(client, "deploy", "attacker/nspoison#1")
    assert minted.status_code == 200, minted.text

    r = await alloc(client, "nspoison", headers=DESKTOP)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["candidates"] == [], "nothing was minted to choose from"


async def test_a_two_segment_key_the_board_cannot_identify_is_left_alone(client):
    """A generic key is the caller's own vocabulary, and "any two segments is a
    repo" broke that promise invisibly: `Prod/Blue:resource` came back as
    `prod/blue:resource`, so two genuinely distinct resources differing only in
    case became one claim."""
    r = await claim(client, "deploy", "Prod/Blue:resource")
    assert r.status_code == 200, r.text
    assert r.json()["key"] == "Prod/Blue:resource"
    assert "key_as_given" not in r.json()


async def test_a_bare_row_and_its_canonical_twin_cannot_both_be_live(client):
    """**This release's own bug, arriving through the door it deliberately left
    open.** A key whose head the board cannot yet identify is stored as sent —
    correctly. But the board's knowledge grows, and once a review run makes that
    basename resolvable the same request canonicalises. Checking only the
    canonical key at that point never sees the bare row still sitting there, so
    both spellings go live and two agents are each told they hold #9."""
    mine = await claim(client, "work", "nstwin#9", note="mine")
    assert mine.status_code == 200, mine.text
    assert mine.json()["key"] == "nstwin#9", "stored as sent while unidentifiable"

    await seed_review_run("acme/nstwin")

    theirs = await claim(client, "work", "acme/nstwin#9", headers=DESKTOP)
    assert theirs.status_code == 409, theirs.text
    d = theirs.json()["detail"]
    assert d["key"] == "nstwin#9", "the row that is actually held"
    assert d["key_as_given"] == "acme/nstwin#9", \
        "the error is the response a caller reads; it has to connect the two"

    again = await claim(client, "work", "acme/nstwin#9", note="second")
    assert again.status_code == 200, again.text
    assert again.json()["claim_id"] == mine.json()["claim_id"], "one row, not two"


async def test_a_lookup_finds_a_generic_key_and_treats_a_wildcard_literally(client):
    await claim(client, "deploy", "portainer-stack-190", note="hyphens")
    await claim(client, "deploy", "portainer_stack_190", note="underscores")

    r = await client.get("/claims", params={"key": "portainer_stack_190"},
                         headers=LAPTOP)
    assert [c["key"] for c in r.json()["claims"]] == ["portainer_stack_190"]


async def test_a_lookup_by_either_spelling_finds_a_row_stored_under_the_other(client):
    """The mirror of `_first_held`: a row taken before the board could identify
    the repo is still keyed on the bare name, and a reader told the resource is
    free while somebody is visibly holding it is the whole failure again."""
    await claim(client, "merge", "nsboth:main", note="landing")
    await seed_review_run("acme/nsboth")

    r = await client.get("/claims", params={"key": "acme/nsboth:main"}, headers=LAPTOP)
    assert [c["key"] for c in r.json()["claims"]] == ["nsboth:main"]


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
    assert body["gave_up_key"] == "acme/nsreclaim:2.41"
    assert body["version"] == "2.42"
    assert body["repo"] == "acme/nsreclaim"


async def test_the_holder_of_a_stranded_number_can_still_renumber(client):
    """**A regression this release introduced into the endpoint that exists to
    get a caller off a collision.** 0020 leaves a basename it cannot expand
    exactly where it is, and the only spelling that names such a claim is the
    stranded one — which resolving `repo` up front rejected with a 400, while any
    resolvable spelling 409'd because the old head resolves to nothing. The
    pre-v2.38 test was `old.key.startswith(f"{repo}:")`, which accepted this
    exact call."""
    claim_id = await seed_legacy_release("nsstrand:1.4", holder="laptop",
                                         session="s-strand")
    r = await client.post("/release/reclaim", headers=LAPTOP, json={
        "repo": "nsstrand", "claim_id": claim_id, "session": "s-strand"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["gave_up"] == "1.4"
    assert body["gave_up_key"] == "nsstrand:1.4"
    assert body["version"] == "1.5"
    assert body["key"] == "nsstrand:1.5", "the legacy bucket, deliberately"


async def test_a_stranded_number_can_be_renumbered_under_a_named_owner(client):
    """The other half: the caller CAN name the owner the board could not infer,
    and then the new number is taken in the canonical namespace. The floor still
    reads the legacy bucket, so it cannot land back on the number being left."""
    claim_id = await seed_legacy_release("nsadopt:1.4", holder="laptop",
                                         session="s-adopt")
    r = await client.post("/release/reclaim", headers=LAPTOP, json={
        "repo": "acme/nsadopt", "claim_id": claim_id, "session": "s-adopt"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["repo"] == "acme/nsadopt"
    assert body["version"] == "1.5"
    assert body["key"] == "acme/nsadopt:1.5"


async def test_the_old_claim_settles_a_basename_the_caller_cannot_expand(client):
    """The mirror case: the caller sends a basename two owners answer to, and the
    claim it is holding already says which. Refusing that with "say which" when
    the caller has already proved which is a question with a known answer."""
    await seed_review_run("owner-a/nspin")
    await seed_review_run("owner-b/nspin")
    held = await took(client, "owner-a/nspin", session="s-pin")

    r = await client.post("/release/reclaim", headers=LAPTOP, json={
        "repo": "nspin", "claim_id": held["claim_id"], "session": "s-pin"})
    assert r.status_code == 200, r.text
    assert r.json()["repo"] == "owner-a/nspin"
    assert r.json()["repo_as_given"] == "nspin"


async def test_a_renumber_naming_a_genuinely_different_repo_is_still_refused(client):
    """The ownership check is what the fallback must not cost. Two repos with
    different names never match, whichever of them the board can expand."""
    await seed_review_run("acme/nsmine")
    await seed_review_run("acme/nsother")
    held = await took(client, "acme/nsmine", session="s-mine")

    r = await client.post("/release/reclaim", headers=LAPTOP, json={
        "repo": "acme/nsother", "claim_id": held["claim_id"], "session": "s-mine"})
    assert r.status_code == 409, r.text
    assert "another repo" in r.json()["detail"]["error"]

    strand = await seed_legacy_release("nselse:1.4", holder="laptop", session="s-else")
    r = await client.post("/release/reclaim", headers=LAPTOP, json={
        "repo": "acme/nsmine", "claim_id": strand, "session": "s-else"})
    assert r.status_code == 409, r.text
    assert "another repo" in r.json()["detail"]["error"]


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


# --------------------------------- the migration, against a real database (0020)


def _load_0020():
    """Import revision 0020 by path — `migrations/versions` is not a package."""
    path = (Path(__file__).resolve().parents[1] / "migrations" / "versions"
            / "0020_canonical_repo_keys.py")
    spec = importlib.util.spec_from_file_location("_rev0020", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply_0020(sync_conn):
    """Run `upgrade()` the way alembic does: `op.get_bind()` needs a real context."""
    ctx = MigrationContext.configure(sync_conn)
    with Operations.context(ctx):
        _load_0020().upgrade()


_MIG_BASE = datetime(2026, 8, 16, tzinfo=UTC)


def _mig_row(key, *, kind="release", at, note=None, expired=False, released=False):
    return {
        "id": uuid.uuid4(), "kind": kind, "key": key, "holder": "zeus/one",
        "session": None, "note": note, "ttl": 3600,
        "acquired": _MIG_BASE + timedelta(hours=at),
        "expires": _MIG_BASE - timedelta(hours=1) if expired
        else datetime.now(UTC) + timedelta(hours=8),
        "released": _MIG_BASE + timedelta(hours=at) if released else None,
    }


#: One row per property the revision's docstring asserts and no test could see.
_MIG_ROWS = {
    "plain": _mig_row("migre:2.32", at=1),
    "cased": _mig_row("Acme/MigRe:2.33", at=2, note="thirty three"),
    "already": _mig_row("acme/migre:2.35", at=3),
    "gone": _mig_row("migre:2.31", at=0, released=True),
    "stranded": _mig_row("migmystery:1.4", at=1),
    # Expired, unswept, and on a key NOTHING is being rewritten onto. Its
    # `released_at` must survive: an unscoped sweep stamped a fresh one over the
    # only record of when the claim actually died.
    "bystander": _mig_row("portainer-migstack", kind="deploy", at=1, expired=True),
    # The collision the revision exists for: two live rows, one number.
    "winner": _mig_row("acme/migcol:2.36", at=1),
    "loser": _mig_row("migcol:2.36", at=2, note="nimbus"),
    # The order case. The EARLIER row has to move onto a key the LATER one is
    # still holding, so applying rewrites before releases hits the unique index
    # and Postgres aborts the migration — the abort no unit test can observe.
    "mover": _mig_row("migord:2.40", at=1),
    "displaced": _mig_row("acme/migord:2.40", at=2),
    # An expired-but-unswept row squatting on a seat a rewrite lands on. The
    # index cannot test `expires_at`, so this must be swept or the rewrite fails.
    "squatter": _mig_row("acme/migexp:2.50", at=1, expired=True),
    "arriving": _mig_row("migexp:2.50", at=2),
}


async def test_the_migration_upgrade_runs_against_a_real_database(_schema):
    """**Every other migration test here plans over hand-built rows, so what the
    revision actually does to a database was asserted only in prose.** Whether
    the pre-sweep frees the seats the planner assumes are free; whether one
    `SET key = ..., released_at = ...` really does escape the partial unique
    index — the crux of the whole design; what `coalesce(note || ' — ', '')`
    writes for a NULL note and for a set one; whether `_known_repos` returns what
    the planner expects. `test_every_release_is_planned_before_any_rewrite` says
    getting the order wrong "aborts the whole migration", and this is the only
    test that can watch Postgres do it.

    Runs inside a transaction that is always rolled back, so the rest of the
    suite's rows are read (and rewritten, and put back) rather than damaged.
    """
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            for repo in ("acme/migre", "acme/migcol", "acme/migord", "acme/migexp"):
                await conn.execute(
                    text("INSERT INTO review_runs (author, repo, pr) "
                         "VALUES ('zeus/fern-hazel', :r, 1)"), {"r": repo})
            await conn.execute(
                text("INSERT INTO resource_leases "
                     "(id, kind, key, holder, session, note, ttl_seconds, "
                     " acquired_at, expires_at, released_at) "
                     "VALUES (:id, :kind, :key, :holder, :session, :note, :ttl, "
                     "        :acquired, :expires, :released)"),
                list(_MIG_ROWS.values()))

            await conn.run_sync(_apply_0020)

            got = {
                name: (await conn.execute(
                    text("SELECT key, note, released_at, lapsed FROM resource_leases "
                         "WHERE id = :id"), {"id": row["id"]})).one()
                for name, row in _MIG_ROWS.items()
            }
        finally:
            await trans.rollback()

    # Rewritten, released rows included: history has to carry the number or the
    # floor forgets it was handed out.
    assert got["plain"].key == "acme/migre:2.32"
    assert got["cased"].key == "acme/migre:2.33"
    assert got["already"].key == "acme/migre:2.35"
    assert got["gone"].key == "acme/migre:2.31"
    assert got["gone"].released_at is not None, "a released row stays released"

    # Left exactly as it is, because guessing at an owner is the third namespace.
    assert got["stranded"].key == "migmystery:1.4"

    # The scoped sweep: an expired row nothing is landing on keeps its own state.
    assert got["bystander"].key == "portainer-migstack"
    assert got["bystander"].released_at is None
    assert got["bystander"].lapsed is False

    # The collision. One canonical key, two rows, exactly one still held — which
    # is only representable because the loser leaves the index's predicate in the
    # same statement that moves its key.
    assert got["winner"].key == "acme/migcol:2.36"
    assert got["winner"].released_at is None
    assert got["loser"].key == "acme/migcol:2.36"
    assert got["loser"].released_at is not None
    assert got["loser"].lapsed is False, "the board took it; the holder did not vanish"
    assert got["loser"].note.startswith("nimbus — released by 0020 (#148)"), \
        "an existing note is kept and the reason appended"

    # ...and with no note to append to, no dangling separator.
    assert got["displaced"].released_at is not None
    assert got["displaced"].note.startswith("released by 0020 (#148)")
    assert got["mover"].key == "acme/migord:2.40"
    assert got["mover"].released_at is None, "the earlier claim keeps the seat"

    # The squatter was swept so the rewrite could land on its key.
    assert got["squatter"].released_at is not None
    assert got["squatter"].lapsed is True
    assert got["arriving"].key == "acme/migexp:2.50"
    assert got["arriving"].released_at is None
