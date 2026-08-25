"""#294: the landing graph — what gates what, across repos, and who is minding it.

The fleet lands PRs into shared `main` branches across several repositories and
they gate each other, and until this the structure had no representation
anywhere: it lived in prose in board posts and in markdown on unpushed branches.
`nix-fleet#40` waited on `quarterback#290`, `nix-fleet#23`, `#31` and `#32` — one
issue, four blockers, two repositories — and nothing queried any of it.

The properties under test are the ones that distinguish a fact store from a
workflow engine, plus the ones that make the store worth having:

* **An edge crosses repositories, and both ends see it.** The motivating case
  fails on any single-repo design, so a scoped read has to return an edge with
  only its FAR end in that repository.
* **Fan-out costs nothing extra.** One node, three outbound edges (#293 closed
  #177 and #259 and unblocked #188), read from the blocker end.
* **Distance from landable is a number.** `depth == 0` means go now; it is the
  fact that would have said #290 should land before #265 and #268.
* **A cycle is recorded, not refused.** A store that will not hold a real
  deadlock puts it back into prose, which is the failure being fixed.
* **Minding is not claiming.** Several agents may stand by for one PR while none
  is doing it, and the second one is told about the first — the thing that took a
  human pasting one agent's message into the other's session.
* **A watch dies with its holder, and says which.** "Finished waiting" and
  "vanished" stay two facts; *blocked and unattended* is the dangerous state and
  it must not render like the safe one.
* **The wire resolves edges, and only when it is unambiguous.** A `published`
  merge announcement naming `owner/name` closes the edge and records the post
  that said so; a bare repository name closes nothing, because
  `Merge pull request #40` could be either repo's #40.
* **It decides nothing.** No order, no recommendation, no `next`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.db import async_session
from app.landing import MERGE_RE, announced_merges, cycles, depths

from .conftest import DESKTOP, LAPTOP, SERVER


def node(repo: str, kind: str, value) -> dict:
    return {"repo": repo, "kind": kind, "value": str(value)}


async def gate(client, blocked: dict, blocker: dict, headers=LAPTOP, **over):
    return await client.post("/landing/gate",
                             json={"blocked": blocked, "blocker": blocker, **over},
                             headers=headers)


async def must_gate(client, blocked: dict, blocker: dict, headers=LAPTOP, **over) -> dict:
    r = await gate(client, blocked, blocker, headers=headers, **over)
    assert r.status_code == 201, r.text
    return r.json()


async def graph(client, repo: str, headers=LAPTOP, **params) -> dict:
    r = await client.get("/landing", params={"repo": repo, **params}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def find(g: dict, key: str) -> dict:
    for n in g["nodes"]:
        if n["key"] == key:
            return n
    raise AssertionError(f"{key} not in {[n['key'] for n in g['nodes']]}")


async def publish(client, summary: str, refs: list[dict], headers=LAPTOP) -> int:
    r = await client.post("/post", json={"type": "published", "summary": summary,
                                         "refs": refs}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


# --------------------------------------------------- an edge crosses repositories

async def test_an_edge_spans_two_repositories_and_both_of_them_can_see_it(client):
    """`nix-fleet#40` waits on `quarterback#290`. Asking about EITHER repository
    has to return the edge, or the agent picking up #290 never learns that
    another repo's step 0 is behind it — which is the blindness #294 opens
    with."""
    fleet, board = "acme/xrepo-fleet", "acme/xrepo-board"
    out = await must_gate(client, node(fleet, "issue", 40), node(board, "pr", 290),
                          note="step 0 cannot start until this lands")
    assert out["cross_repo"] is True

    downstream = find(await graph(client, fleet), f"{fleet}#40")
    assert [b["key"] for b in downstream["blocked_by"]] == [f"{board}!290"]
    assert downstream["blocked_by"][0]["repo"] == board

    upstream = find(await graph(client, board), f"{board}!290")
    assert [b["key"] for b in upstream["blocks"]] == [f"{fleet}#40"]
    assert upstream["landable"] is True and upstream["depth"] == 0


async def test_a_repo_may_be_named_by_its_bare_basename_like_every_other_read(client):
    """The lifecycle hook tags posts with a checkout's basename, so the one
    spelling a caller has to hand is often the bare one. Answering `[]` to it
    would read as "nothing gates anything here" when it means "I could not tell
    what you asked about"."""
    repo = "acme/barename-graph"
    await must_gate(client, node(repo, "pr", 2), node(repo, "pr", 1))
    assert (await graph(client, "barename-graph"))["counts"]["edges"] == 1


async def test_a_repo_that_is_neither_a_slug_nor_a_bare_name_is_refused(client):
    r = await client.get("/landing", params={"repo": "https://github.com/a/b.git"},
                         headers=LAPTOP)
    assert r.status_code == 422, r.text


# ------------------------------------------------------------ fan-out and fan-in

async def test_one_landing_frees_three_downstream_nodes_read_from_the_blocker(client):
    """PR #293 closed #177 and #259 and unblocked #188: one node, three outbound
    edges. Fan-out is the same rows as fan-in, read from the other end."""
    repo = "acme/fanout"
    for waiting in (177, 259, 188):
        await must_gate(client, node(repo, "issue", waiting), node(repo, "pr", 293))
    blocker = find(await graph(client, repo), f"{repo}!293")
    assert sorted(b["value"] for b in blocker["blocks"]) == ["177", "188", "259"]
    assert blocker["landable"] is True


async def test_four_blockers_in_two_repositories_are_all_reported(client):
    """The motivating shape, whole: one issue, four blockers, two repos."""
    fleet, board = "acme/four-fleet", "acme/four-board"
    await must_gate(client, node(fleet, "issue", 40), node(board, "pr", 290))
    for pr in (23, 31, 32):
        await must_gate(client, node(fleet, "issue", 40), node(fleet, "pr", pr))
    waiting = find(await graph(client, fleet), f"{fleet}#40")
    assert len(waiting["blocked_by"]) == 4
    assert waiting["landable"] is False and waiting["depth"] == 1


# ------------------------------------------------------------------------ depth

async def test_depth_counts_the_landings_between_a_node_and_being_landable(client):
    """`0` is "go now" — the number a just-in-time trigger reads, and the one
    nothing on this board could previously answer."""
    repo = "acme/depth"
    await must_gate(client, node(repo, "pr", 3), node(repo, "pr", 2))
    await must_gate(client, node(repo, "pr", 2), node(repo, "pr", 1))
    g = await graph(client, repo)
    assert find(g, f"{repo}!1")["depth"] == 0
    assert find(g, f"{repo}!2")["depth"] == 1
    assert find(g, f"{repo}!3")["depth"] == 2
    assert g["counts"]["landable"] == 1


async def test_depth_takes_the_LONGEST_chain_because_that_is_what_you_wait_for(client):
    """A node with one ready blocker and one two-deep blocker is two deep. The
    shortest path is not what anybody waits for."""
    repo = "acme/longest"
    await must_gate(client, node(repo, "pr", 9), node(repo, "pr", 1))
    await must_gate(client, node(repo, "pr", 9), node(repo, "pr", 8))
    await must_gate(client, node(repo, "pr", 8), node(repo, "pr", 7))
    assert find(await graph(client, repo), f"{repo}!9")["depth"] == 2


# ------------------------------------------------------------------ cycles

async def test_a_cycle_is_recorded_and_named_rather_than_refused(client):
    """Two PRs that each genuinely need the other first is a real deadlock a
    human has to break. `plan_depends` refuses a cycle because it owns both ends
    of its edges; this does not, and a store that will not hold the fact leaves
    it exactly where #294 found it — in prose in a board post."""
    repo = "acme/cyclic"
    await must_gate(client, node(repo, "pr", 1), node(repo, "pr", 2))
    r = await gate(client, node(repo, "pr", 2), node(repo, "pr", 1))
    assert r.status_code == 201, r.text

    g = await graph(client, repo)
    assert g["cycles"] == [[f"{repo}!1", f"{repo}!2"]]
    assert find(g, f"{repo}!1")["in_cycle"] is True
    # No honest distance to publish, so none is published.
    assert find(g, f"{repo}!1")["depth"] is None
    assert g["counts"]["in_cycle"] == 2


async def test_a_node_behind_a_cycle_has_no_depth_either(client):
    repo = "acme/behind-cycle"
    await must_gate(client, node(repo, "pr", 1), node(repo, "pr", 2))
    await must_gate(client, node(repo, "pr", 2), node(repo, "pr", 1))
    await must_gate(client, node(repo, "pr", 3), node(repo, "pr", 1))
    assert find(await graph(client, repo), f"{repo}!3")["depth"] is None


async def test_a_node_cannot_gate_itself(client):
    """The obvious client bug is passing one ref twice, and the result would be a
    node permanently blocked with nothing to wait for."""
    repo = "acme/selfedge"
    r = await gate(client, node(repo, "pr", 5), node(repo, "pr", 5))
    assert r.status_code == 422
    assert "cannot gate itself" in r.json()["detail"]["error"]


# ------------------------------------------------------- asserting is idempotent

async def test_asserting_a_live_edge_again_is_one_fact_not_two(client):
    repo = "acme/idem"
    first = await must_gate(client, node(repo, "pr", 2), node(repo, "pr", 1),
                            note="first reason")
    r = await gate(client, node(repo, "pr", 2), node(repo, "pr", 1),
                   headers=DESKTOP, note="a better reason")
    assert r.status_code == 201, r.text
    again = r.json()
    assert again["created"] is False
    assert again["edge_id"] == first["edge_id"]
    assert again["note"] == "a better reason"
    # And the original asserter is still the asserter: a note is not authorship.
    assert again["asserted_by"] == first["asserted_by"]
    assert (await graph(client, repo))["counts"]["edges"] == 1


async def test_a_same_repo_issue_to_issue_edge_is_kept_but_told_where_it_belongs(client):
    """#229 is right that the board should not be a second store of a fact GitHub
    owns — and refusing the edge would only push it back into prose. So it is
    stored, and the answer names GitHub's native graph every time."""
    repo = "acme/gh-owned"
    out = await must_gate(client, node(repo, "issue", 2), node(repo, "issue", 1))
    assert out["advice"] is not None and "#229" in out["advice"]

    cross = await must_gate(client, node(repo, "issue", 4),
                            node("acme/gh-other", "issue", 3))
    assert cross["advice"] is None

    pr_ended = await must_gate(client, node(repo, "issue", 6), node(repo, "pr", 5))
    assert pr_ended["advice"] is None


# ------------------------------------------------------------------ clearing

async def test_clearing_a_blocker_frees_everything_it_was_gating_at_once(client):
    """One landing frees every downstream node together. Making the caller
    enumerate them is how one gets missed."""
    repo = "acme/clear-all"
    for waiting in (10, 11, 12):
        await must_gate(client, node(repo, "issue", waiting), node(repo, "pr", 9))
    r = await client.post("/landing/clear",
                          json={"blocker": node(repo, "pr", 9), "resolution": "landed"},
                          headers=LAPTOP)
    assert r.status_code == 200, r.text
    assert r.json()["cleared"] == 3
    assert (await graph(client, repo))["counts"]["edges"] == 0


async def test_clearing_one_pair_leaves_the_others_alone(client):
    repo = "acme/clear-one"
    for waiting in (10, 11):
        await must_gate(client, node(repo, "issue", waiting), node(repo, "pr", 9))
    r = await client.post("/landing/clear",
                          json={"blocker": node(repo, "pr", 9),
                                "blocked": node(repo, "issue", 10),
                                "resolution": "dropped"},
                          headers=LAPTOP)
    assert r.json()["cleared"] == 1
    assert (await graph(client, repo))["counts"]["edges"] == 1


async def test_landed_and_dropped_are_different_facts_and_nothing_else_is_a_resolution(client):
    """"The work happened" and "somebody decided the edge was wrong" call for
    opposite reactions from whoever reads the history."""
    repo = "acme/resolution-vocab"
    await must_gate(client, node(repo, "pr", 2), node(repo, "pr", 1))
    r = await client.post("/landing/clear",
                          json={"blocker": node(repo, "pr", 1), "resolution": "gone"},
                          headers=LAPTOP)
    assert r.status_code == 422
    assert r.json()["detail"]["resolutions"] == ["landed", "dropped"]


async def test_clearing_an_edge_that_is_not_there_is_not_an_error(client):
    repo = "acme/clear-none"
    r = await client.post("/landing/clear",
                          json={"blocker": node(repo, "pr", 1), "resolution": "landed"},
                          headers=LAPTOP)
    assert r.status_code == 200 and r.json()["cleared"] == 0


# ---------------------------------------------------------------- minders

async def test_two_agents_may_mind_one_node_and_the_second_is_told_about_the_first(client):
    """The failure this fixes: one agent set a watch on #293 in its own session,
    and eight minutes later another claimed the same work, unable to see it. What
    closed the loop was a human pasting one message into the other's session."""
    repo = "acme/minders"
    n = node(repo, "pr", 293)
    first = await client.post("/landing/mind", json={"node": n, "note": "landing #188"},
                              headers=LAPTOP)
    assert first.status_code == 201, first.text
    assert first.json()["also_minding"] == []

    second = await client.post("/landing/mind", json={"node": n, "note": "watching too"},
                               headers=SERVER)
    assert second.status_code == 201, second.text
    peers = second.json()["also_minding"]
    assert len(peers) == 1 and peers[0]["note"] == "landing #188"

    node_view = find(await graph(client, repo), f"{repo}!293")
    assert node_view["minded"] is True and len(node_view["minders"]) == 2


async def test_minding_is_not_claiming_and_the_graph_shows_both_separately(client):
    """Claiming work you cannot start blocks it for everybody while nothing
    happens, so "minded and unclaimed" is the correct state while you wait — and
    a reader has to be able to tell the two apart."""
    repo = "acme/mind-vs-claim"
    await client.post("/landing/mind", json={"node": node(repo, "pr", 7)}, headers=LAPTOP)
    view = find(await graph(client, repo), f"{repo}!7")
    assert view["minded"] is True and view["claim"] is None

    r = await client.post("/claim", json={"ref": {"kind": "pr", "repo": repo, "value": "7"},
                                          "note": "landing it"}, headers=SERVER)
    assert r.status_code == 200, r.text
    view = find(await graph(client, repo), f"{repo}!7")
    assert view["claim"]["note"] == "landing it"
    assert view["minded"] is True


async def test_minding_the_same_node_again_renews_rather_than_duplicating(client):
    repo = "acme/mind-renew"
    n = node(repo, "pr", 3)
    first = await client.post("/landing/mind", json={"node": n}, headers=LAPTOP)
    again = await client.post("/landing/mind", json={"node": n, "note": "still here"},
                              headers=LAPTOP)
    assert again.json()["renewed"] is True
    assert again.json()["watch_id"] == first.json()["watch_id"]
    assert len(find(await graph(client, repo), f"{repo}!3")["minders"]) == 1


async def test_letting_go_is_recorded_as_letting_go_and_is_idempotent(client):
    repo = "acme/unmind"
    n = node(repo, "pr", 4)
    await client.post("/landing/mind", json={"node": n}, headers=LAPTOP)
    first = await client.post("/landing/unmind", json={"node": n}, headers=LAPTOP)
    assert first.status_code == 200 and first.json()["released"] is True
    second = await client.post("/landing/unmind", json={"node": n}, headers=LAPTOP)
    assert second.status_code == 200 and second.json()["released"] is False

    async with async_session() as s:
        from sqlalchemy import select

        from app.models.landing import LandingWatch
        watch = await s.scalar(select(LandingWatch).where(
            LandingWatch.node_key == f"{repo}!4"))
        # It did not vanish; it stood down. The distinction is the whole point.
        assert watch.lapsed is False and watch.released_at is not None


async def test_another_machine_may_not_stand_down_your_watch(client):
    repo = "acme/unmind-other"
    n = node(repo, "pr", 5)
    await client.post("/landing/mind", json={"node": n}, headers=LAPTOP)
    r = await client.post("/landing/unmind", json={"node": n, "holder": "laptop"},
                          headers=SERVER)
    assert r.status_code == 403, r.text
    assert "not your watch" in r.json()["detail"]["error"]


async def test_a_watch_whose_holder_stopped_being_present_lapses_on_the_next_read(client):
    """Renewal on presence, which is the whole expiry design: a three-day wait is
    legitimate so a short TTL is wrong, and a session that dies at 2am must stop
    looking like somebody standing by. When the session's lease goes, so does the
    watch — passively, on the next read, with no reaper."""
    repo = "acme/presence"
    sess = "sess-presence-294"
    lease = await client.post("/lease", json={"session": sess, "device": "laptop",
                                              "ttl": 300}, headers=LAPTOP)
    assert lease.status_code == 200, lease.text
    await client.post("/landing/mind", json={"node": node(repo, "pr", 8),
                                             "session": sess}, headers=LAPTOP)
    assert find(await graph(client, repo), f"{repo}!8")["minded"] is True

    r = await client.post("/lease/release", json={"lease_id": lease.json()["lease_id"]},
                          headers=LAPTOP)
    assert r.status_code == 200, r.text

    g = await graph(client, repo)
    assert g["swept"]["watches"] == 1
    # The node has not gone; it is simply unattended now, which is the fact.
    assert g["counts"]["watches"] == 0

    async with async_session() as s:
        from sqlalchemy import select

        from app.models.landing import LandingWatch
        watch = await s.scalar(select(LandingWatch).where(
            LandingWatch.node_key == f"{repo}!8"))
        assert watch.lapsed is True


async def test_a_watch_whose_session_this_board_never_saw_is_not_swept(client):
    """"Gone" is a different fact from "never here". A scripted watcher, or an
    agent whose lifecycle hook is not wired up, has no lease to lose — and
    deleting its watch on the first read would be a refusal its holder could not
    see. That one falls back to its TTL."""
    repo = "acme/no-lease"
    await client.post("/landing/mind", json={"node": node(repo, "pr", 9),
                                             "session": "sess-never-leased"},
                      headers=LAPTOP)
    assert find(await graph(client, repo), f"{repo}!9")["minded"] is True


async def test_a_watch_past_its_own_ttl_lapses_even_with_no_session(client):
    """And with the watch goes the node: a node is in the graph because there is
    a fact about it, so a node whose only fact has ended has nothing to say."""
    repo = "acme/ttl"
    await client.post("/landing/mind", json={"node": node(repo, "pr", 6), "ttl": 60},
                      headers=LAPTOP)
    async with async_session() as s:
        from sqlalchemy import select

        from app.models.landing import LandingWatch
        watch = await s.scalar(select(LandingWatch).where(
            LandingWatch.node_key == f"{repo}!6"))
        watch.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()

    g = await graph(client, repo)
    assert g["swept"]["watches"] == 1 and g["nodes"] == []
    async with async_session() as s:
        from sqlalchemy import select

        from app.models.landing import LandingWatch
        watch = await s.scalar(select(LandingWatch).where(
            LandingWatch.node_key == f"{repo}!6"))
        assert watch.lapsed is True


async def test_blocked_and_unminded_is_counted_because_it_is_the_dangerous_state(client):
    """`plan_read` can already say an item is blocked. It cannot say whether
    anybody is waiting to unblock it, and the unattended one renders identically
    to the safe one everywhere else."""
    repo = "acme/unminded"
    await must_gate(client, node(repo, "pr", 2), node(repo, "pr", 1))
    await must_gate(client, node(repo, "pr", 4), node(repo, "pr", 3))
    await client.post("/landing/mind", json={"node": node(repo, "pr", 2)}, headers=LAPTOP)
    counts = (await graph(client, repo))["counts"]
    assert counts["blocked"] == 2 and counts["blocked_unminded"] == 1


# ------------------------------------------------- resolving from the wire

async def test_a_published_merge_announcement_closes_the_edges_it_names(client):
    """The board already receives the event every watcher polls for, twice over —
    once from CI and again from whichever agent pulled it — while every waiting
    agent separately burns a 60-second timer against the GitHub API for it."""
    repo = "acme/wire"
    await must_gate(client, node(repo, "issue", 188), node(repo, "pr", 293))
    post_id = await publish(client, "Merge pull request #293 from acme/feat/thing",
                            [{"kind": "commit", "repo": repo, "value": "abc1234"}])

    g = await graph(client, repo)
    assert g["swept"]["edges"] == 1
    assert g["counts"]["edges"] == 0

    async with async_session() as s:
        from sqlalchemy import select

        from app.models.landing import LandingEdge
        edge = await s.scalar(select(LandingEdge).where(
            LandingEdge.blocker_key == f"{repo}!293"))
        assert edge.resolution == "landed"
        # The specific evidence, named — so a resolution anybody disputes is
        # traceable to the post that caused it.
        assert edge.resolved_by == f"board:post/{post_id}"


async def test_a_merge_announced_under_a_BARE_repo_name_resolves_nothing(client):
    """`Merge pull request #40` tagged only `quarterback` is indistinguishable
    from the same number in `nix-fleet`. Under-resolving leaves a stale edge
    somebody clears in one call; over-resolving tells an agent its blocker landed
    when it did not, across exactly the repository boundary this exists to span."""
    repo = "acme/bare-wire"
    await must_gate(client, node(repo, "issue", 41), node(repo, "pr", 40))
    await publish(client, "Merge pull request #40 from acme/feat/x",
                  [{"kind": "repo", "value": "bare-wire"}])
    g = await graph(client, repo)
    assert g["swept"]["edges"] == 0 and g["counts"]["edges"] == 1


async def test_prose_about_a_merge_is_not_a_merge(client):
    """The reading that failed on #372, where a body opening "**This does not
    close #371**" was parsed as a closing keyword. Prose must never resolve an
    edge."""
    repo = "acme/prose-wire"
    await must_gate(client, node(repo, "issue", 2), node(repo, "pr", 1))
    await publish(client, "reverted the Merge pull request #1 from acme/x",
                  [{"kind": "commit", "repo": repo, "value": "deadbee"}])
    assert (await graph(client, repo))["counts"]["edges"] == 1


async def test_passed_by_counts_the_merges_that_have_gone_past_a_waiting_node(client):
    """#290 was MERGEABLE when it opened and CONFLICTING by lunchtime because two
    unrelated PRs landed while it sat, and nothing told it that was happening.
    This is that, counted — from posts the board already holds, with no GitHub
    client (#229) and no second store of a fact GitHub owns."""
    repo = "acme/passed-by"
    await must_gate(client, node(repo, "issue", 99), node(repo, "pr", 90))
    for other in (265, 268):
        await publish(client, f"Merge pull request #{other} from acme/unrelated",
                      [{"kind": "commit", "repo": repo, "value": f"sha{other}"}])
    waiting = find(await graph(client, repo), f"{repo}#99")
    assert waiting["passed_by"] == 2


async def test_a_merge_older_than_the_edge_does_not_count_as_having_passed_it(client):
    repo = "acme/passed-before"
    await publish(client, "Merge pull request #1 from acme/before",
                  [{"kind": "commit", "repo": repo, "value": "old1234"}])
    await must_gate(client, node(repo, "issue", 50), node(repo, "pr", 49))
    assert find(await graph(client, repo), f"{repo}#50")["passed_by"] == 0


# ------------------------------------------------------------- it decides nothing

async def test_the_graph_offers_no_order_and_no_recommendation(client):
    """Turning this and #94's file overlap into a merge order is #80's half of
    the problem. This exposes; that consumes."""
    repo = "acme/no-order"
    await must_gate(client, node(repo, "pr", 2), node(repo, "pr", 1))
    g = await graph(client, repo)
    assert "next" not in g and "suggested_order" not in g and "order" not in g


async def test_every_landing_verb_is_a_route_the_board_serves(client):
    from app.main import app as board
    paths = set(board.openapi()["paths"])
    assert {"/landing", "/landing/gate", "/landing/clear",
            "/landing/mind", "/landing/unmind"} <= paths


async def test_a_node_is_a_number_and_a_title_is_refused(client):
    r = await gate(client, node("acme/badnode", "pr", "the big one"),
                   node("acme/badnode", "pr", 1))
    assert r.status_code == 422
    assert r.json()["detail"]["field"] == "blocked"


# ----------------------------------------------------- the reasoning, on its own

def test_a_merge_subject_is_matched_only_at_the_start_of_the_summary():
    assert MERGE_RE.match("Merge pull request #265 from prisonblues/fix/issue-261")
    assert MERGE_RE.match("reverted the Merge pull request #265") is None
    assert MERGE_RE.match("Merge pull requests #265") is None


def test_one_merge_announced_twice_is_one_landing_at_the_earlier_time():
    """Board 4910 (ci) and 4920 (an agent that pulled it) are the same merge. The
    fact being recorded is when it landed, not when the second witness spoke."""
    refs = [{"kind": "commit", "repo": "prisonblues/quarterback", "value": "abc"}]
    posts = [
        {"id": 4920, "type": "published", "refs": refs,
         "summary": "Merge pull request #268 from prisonblues/feat/issue-255"},
        {"id": 4910, "type": "published", "refs": refs,
         "summary": "Merge pull request #268 from prisonblues/feat/issue-255"},
    ]
    # Posts arrive newest-first from the query, so the LATER id is seen first and
    # must not win.
    landed = announced_merges(sorted(posts, key=lambda p: -p["id"]))
    assert set(landed) == {"prisonblues/quarterback!268"}


def test_a_lone_node_with_no_edges_is_not_a_cycle():
    assert cycles({"a": set()}) == []


def test_depth_is_none_for_every_member_of_a_cycle_and_a_number_below_it():
    blockers = {"a": {"b"}, "b": {"a"}, "c": {"a"}, "d": set(), "e": {"d"}}
    d = depths(blockers)
    assert d["a"] is None and d["b"] is None and d["c"] is None
    assert d["d"] == 0 and d["e"] == 1
