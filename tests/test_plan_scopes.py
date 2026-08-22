"""#323: a plan scope is not always a GitHub repo.

Two rows sat on the live plan under scope `65lowther` — house renovation work,
deliberately planned, with no GitHub anything. The write that created them was
accepted; the read that would find them answered 422 with `REPO_SHAPE`. The plan
had reused the `repo` column and inherited its validator, so "a GitHub repo has
exactly one spelling" (#148, correct) had quietly become "every plan scope is a
GitHub repo" (never argued for anywhere).

The properties under test, in the order they matter:

  1. **#148 is not weakened.** `65lowther` is still not a repo, on the read path
     and the write path, and a caller who meant a repo and mistyped it still gets
     the refusal that issue is about. Every test below that says "refused" is
     guarding that, not the new feature.
  2. **A scope cannot be invented by an agent**, which is the sharp edge: if "not
     a repo" were inferred from "does not match REPO_RE", every typo would mint a
     scope. Two gates — the sigil and the registry — and each is tested for the
     door it closes rather than for the pair.
  3. **A declared scope works like any other**: readable by its own name, `next`
     answers within it, ranks are its own.

`tests/test_migration_0029.py` covers the other half — the live rows arriving
here without being hand-edited or dropped.
"""

from __future__ import annotations

import pytest

from .conftest import DESKTOP, LAPTOP

EDGE = {**LAPTOP, "Remote-User": "person", "X-Edge-Auth": "tok-edge"}

#: The scope from the issue, spelled as the migration leaves it.
HOUSE = "project:65lowther"


async def declare(client, name: str, note: str | None = None, headers=EDGE):
    return await client.post("/plan/scope", json={"name": name, "note": note},
                             headers=headers)


async def declared(client, name: str, note: str | None = None) -> dict:
    r = await declare(client, name, note)
    assert r.status_code == 201, r.text
    return r.json()


async def add(client, repo: str, title: str, headers=LAPTOP, **over):
    return await client.post("/plan/item", json={"repo": repo, "title": title, **over},
                             headers=headers)


# ---- #148 is untouched -------------------------------------------------------


@pytest.mark.parametrize("path,call", [
    ("read", lambda c, r: c.get("/plan", params={"repo": r}, headers=LAPTOP)),
    ("add", lambda c, r: c.post("/plan/item", json={"repo": r, "title": "t"},
                                headers=LAPTOP)),
    ("submit", lambda c, r: c.post("/plan/submit",
                                   json={"repo": r, "label": "l",
                                         "items": [{"title": "t"}]}, headers=LAPTOP)),
])
async def test_a_bare_name_is_still_refused_as_a_repo_everywhere(client, path, call):
    """The whole issue is about `65lowther`, and it must STILL not be a repo.

    What #323 adds is a second namespace, reached by a prefix nobody types by
    accident. It does not make the bare name mean anything — if it did, the
    allocator defect would be back: `quarterback` beside `prisonblues/quarterback`
    keying one issue two ways, which is #148 exactly."""
    r = await call(client, "65lowther")
    assert r.status_code == 422, r.text
    assert "owner/name" in r.text


async def test_a_mistyped_repo_becomes_nothing_at_all(client):
    """The sharp edge, stated as a test.

    If "this is not a repo" were inferred from "this does not match REPO_RE", a
    caller that dropped the owner would mint a brand-new scope and the plan would
    hold two lists nobody can see both halves of — `quarterback` beside
    `prisonblues/quarterback`, which is #148 exactly. It is refused instead, and
    afterwards nothing exists under that name.

    A typo INSIDE a well-formed `owner/name` is a different thing and deliberately
    not this: `prisonblues/quaterback` is a valid repo spelling, and refusing it
    would mean the board deciding which repositories exist. `REPO_RE` guards the
    shape; only the shape is guessable."""
    r = await add(client, "quarterback", "typo")
    assert r.status_code == 422, r.text
    listed = (await client.get("/plan/scopes", headers=LAPTOP)).json()["scopes"]
    assert [x for x in listed if "quarterback" in x["scope"]] == []
    plan = (await client.get("/plan", headers=LAPTOP)).json()
    assert [i for i in plan["items"] if i["title"] == "typo"] == []


async def test_the_refusal_still_says_owner_name_first_and_then_the_other_door(client):
    """#148's message is what a mistyped repo needs; the scope sentence is what a
    person with genuinely repo-less work needs. Both, in that order — the common
    case is the mistyped repo."""
    r = await client.get("/plan", params={"repo": "65lowther"}, headers=LAPTOP)
    body = r.text
    assert body.index("owner/name") < body.index("project:")
    assert "origin remote" in body


# ---- gate 1: the sigil -------------------------------------------------------


async def test_a_scope_with_no_sigil_cannot_reach_the_project_namespace(client):
    """A colon cannot occur in a GitHub owner or repository name, which is what
    makes the two namespaces disjoint rather than merely conventional. Nothing
    without it is ever read as a project scope, however declared it is."""
    await declared(client, "nosigil")
    r = await client.get("/plan", params={"repo": "nosigil"}, headers=LAPTOP)
    assert r.status_code == 422, "declaring `project:nosigil` must not make " \
                                 "`nosigil` a name the board answers to"


async def test_the_sigil_folds_case_like_a_repo_does(client):
    """One thing, one spelling — #148's rule, and the only part of it that
    generalises past GitHub. `Project:65Lowther` is the same scope."""
    await declared(client, "casefold")
    r = await client.get("/plan", params={"repo": "Project:CaseFold"}, headers=LAPTOP)
    assert r.status_code == 200, r.text
    assert r.json()["repo"] == "project:casefold"


# ---- gate 2: the registry ----------------------------------------------------


async def test_an_agent_cannot_invent_a_scope_by_typo_inside_the_sigil(client):
    """The second door, and the reason the sigil alone is not enough.

    `project:65lowthr` is well-formed. Only a person saying so distinguishes it
    from the scope that exists, so an undeclared one is refused — and the refusal
    names the declared ones, because an agent that mistyped a live scope should be
    able to fix itself from the answer."""
    await declared(client, "65lowther")
    r = await add(client, "project:65lowthr", "the ASHP")
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert HOUSE in detail["declared"] and "project:65lowthr" not in detail["declared"]
    assert "person" in detail["hint"].lower()


async def test_an_agent_cannot_declare_a_scope_itself(client):
    """Human-only, like reordering and for the same reason one level up: an agent
    able to mint a scope would eventually mint one by typo, and it would do it
    silently."""
    r = await declare(client, "roof", headers=LAPTOP)
    assert r.status_code == 403, r.text
    assert "human-only" in r.text


async def test_a_spoofed_remote_user_is_not_a_person_here_either(client):
    """`Remote-User` is client-settable; the edge secret is the boundary. A scope
    declaration is a decision, so it is behind the same door as the order."""
    r = await declare(client, "roof", headers={**LAPTOP, "Remote-User": "person"})
    assert r.status_code == 403, r.text
    assert "X-Edge-Auth" in r.text


async def test_declaring_a_scope_twice_is_the_same_scope_not_an_error(client):
    """A person on a phone tapping a button twice is not an error condition, and
    the call's whole content is "let this scope exist" — which after the first one
    it does."""
    first = await declared(client, "loft", "the loft conversion")
    again = await declare(client, "Project:LOFT")
    assert again.status_code == 201, again.text
    assert again.json()["created"] is False
    assert again.json()["scope"] == first["scope"] == "project:loft"
    listed = (await client.get("/plan/scopes", headers=LAPTOP)).json()
    assert [s["scope"] for s in listed["scopes"] if s["scope"] == "project:loft"] == \
        ["project:loft"]


async def test_a_repo_name_is_refused_by_the_declare_endpoint(client):
    """A repo needs no declaring — it is a scope already. Accepting one here would
    put `owner/name` in a table whose whole content is "scopes with no repo behind
    them", which is two answers to what a scope is."""
    r = await declare(client, "prisonblues/quarterback")
    assert r.status_code == 422, r.text
    assert "no declaring" in r.text


async def test_a_person_declares_the_scope_and_the_row_records_who(client):
    """The column means "somebody decided this", and only a person can."""
    made = await declared(client, "garden", "beds and the shed")
    assert made["added_by"] == "person"
    assert made["label"] == "garden" and made["scope"] == "project:garden"
    assert made["note"] == "beds and the shed"


# ---- a declared scope behaves like any other ---------------------------------


async def test_the_stranded_rows_are_readable_by_their_own_scope(client):
    """The acceptance test from the issue, in miniature: two items, no refs, in a
    scope that is not a repo, found by naming that scope."""
    await declared(client, "65lowther")
    for title in ("D-007 has measured answers now", "Move the ASHP"):
        r = await add(client, HOUSE, title)
        assert r.status_code == 200, r.text
    plan = (await client.get("/plan", params={"repo": HOUSE, "exact": "true"},
                             headers=LAPTOP)).json()
    assert [i["title"] for i in plan["items"]] == \
        ["D-007 has measured answers now", "Move the ASHP"]
    assert plan["counts"]["open"] == 2


async def test_next_answers_within_a_project_scope(client):
    """`next` is the one answer an agent reads cold. A scope whose items are
    readable but whose `next` is null would be the same stranding one layer in."""
    await declared(client, "nextin")
    await add(client, "project:nextin", "no hinged wardrobe fits")
    plan = (await client.get("/plan", params={"repo": "project:nextin"},
                             headers=LAPTOP)).json()
    assert plan["next"] is not None
    assert plan["next"]["title"] == "no hinged wardrobe fits"


async def test_a_project_scope_ranks_its_own_list(client):
    """Ranks are per scope. A project scope is a scope, so its list starts at 1
    whatever the repos beside it are doing."""
    await declared(client, "ranks")
    await client.post("/plan/item", json={"repo": "acme/sib", "title": "a"},
                      headers=LAPTOP)
    await client.post("/plan/item", json={"repo": "acme/sib", "title": "b"},
                      headers=LAPTOP)
    r = await add(client, "project:ranks", "first here")
    assert r.status_code == 200, r.text
    assert r.json()["rank"] == 1


async def test_a_person_can_reorder_a_project_scope(client):
    """Rule 3 does not care what kind of scope it is: the order is the human's."""
    await declared(client, "reorderme")
    ids = [(await add(client, "project:reorderme", t)).json()["item_id"]
           for t in ("wardrobe", "ashp", "bathroom")]
    r = await client.post("/plan/reorder",
                          json={"repo": "project:reorderme",
                                "order": [ids[2], ids[0], ids[1]]},
                          headers=EDGE)
    assert r.status_code == 200, r.text
    assert [i["title"] for i in r.json()["items"]] == ["bathroom", "wardrobe", "ashp"]


async def test_an_item_in_a_project_scope_is_claimable(client):
    """Its claim key comes off its own id — there is no issue to key it by — and
    that path already existed for a ref-less item. Worth pinning because the plan
    is only a coordination surface if its rows can be taken."""
    await declared(client, "claimable")
    item_id = (await add(client, "project:claimable", "the ASHP")).json()["item_id"]
    r = await client.post("/plan/item/claim", json={"item_id": item_id, "note": "on it"},
                          headers=DESKTOP)
    assert r.status_code == 200, r.text
    assert r.json()["claimed"] is True
    plan = (await client.get("/plan", params={"repo": "project:claimable",
                                              "exact": "true"}, headers=LAPTOP)).json()
    assert plan["next"] is None, "a claimed item is not free work"


# ---- a project scope has no forge, so no forge refs ---------------------------


@pytest.mark.parametrize("kind", ["issue", "pr"])
async def test_a_forge_ref_is_refused_in_a_project_scope(client, kind):
    """`issue` and `pr` both name something on GitHub, and a project scope supplies
    nothing to resolve one against. Refused rather than stored: an item carrying
    one would render a link to a page that does not exist and be chased by
    qb-reconcile every quarter of an hour."""
    await declared(client, f"noref{kind}")
    r = await add(client, f"project:noref{kind}", "the ASHP",
                  ref_kind=kind, ref_value="7")
    assert r.status_code == 422, r.text
    assert "project scope" in r.json()["detail"]["error"]


async def test_a_forge_ref_is_refused_on_submit_too_naming_the_line(client):
    """A submission is all-or-nothing, so the caller has to be told WHICH line to
    change rather than bisecting its own plan."""
    await declared(client, "submitref")
    r = await client.post("/plan/submit", json={
        "repo": "project:submitref", "label": "the works",
        "items": [{"title": "wardrobe"},
                  {"title": "ashp", "ref_kind": "issue", "ref_value": "8"}]},
        headers=LAPTOP)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["item"] == 2
    plan = (await client.get("/plan", params={"repo": "project:submitref",
                                              "exact": "true"}, headers=LAPTOP)).json()
    assert plan["items"] == [], "a refused submission has to leave nothing"


async def test_a_repo_scope_still_takes_its_refs(client):
    """The guard is on the scope having no forge, not on refs being suspicious."""
    r = await add(client, "acme/withref", "#7", ref_kind="issue", ref_value="7")
    assert r.status_code == 200, r.text
    assert r.json()["ref"] == {"kind": "issue", "value": "7"}


# ---- discoverability ---------------------------------------------------------


async def test_the_cold_read_carries_the_declared_scopes(client):
    """An agent learns this API from the call it already makes. A scope nobody can
    find the name of is a scope nobody puts work into — and the exact spelling is
    the thing the anti-typo gate makes load-bearing."""
    await declared(client, "65lowther", "the house")
    plan = (await client.get("/plan", headers=LAPTOP)).json()
    assert {"scope": HOUSE, "label": "65lowther"}.items() <= \
        next(s for s in plan["scopes"] if s["scope"] == HOUSE).items()


async def test_the_scopes_list_holds_project_scopes_only(client):
    """A repo scope exists because a repository does. Registering them here would
    be a second store of something GitHub already holds."""
    await declared(client, "listedonly")
    await client.post("/plan/item", json={"repo": "acme/listed", "title": "t"},
                      headers=LAPTOP)
    listed = (await client.get("/plan/scopes", headers=LAPTOP)).json()
    assert all(s["scope"].startswith("project:") for s in listed["scopes"])
    assert listed["sigil"] == "project:"


async def test_reading_the_scope_list_needs_authentication(client):
    """It is an ordinary board read — an agent's token or the edge — and not open."""
    r = await client.get("/plan/scopes")
    assert r.status_code == 401, r.text


async def test_a_scope_is_stripped_before_it_is_classified(client):
    """`canonical_repo` strips before its own shape test, so a repo tolerates a
    leading space. Classifying the namespace before stripping made the project
    branch the one namespace that did not — an asymmetry decided by nothing, and
    it refused a well-spelled scope with the repo's message."""
    await declared(client, "stripped")
    r = await client.get("/plan", params={"repo": "  project:stripped  "},
                         headers=LAPTOP)
    assert r.status_code == 200, r.text
    assert r.json()["repo"] == "project:stripped"
