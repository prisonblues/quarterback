"""What the fleet page says, and — more to the point — what it refuses to say.

Rich, describing what he wants from a phone: *"see the plan, state of each agent,
drag them up and down if needed, and then the seats pick things up."* Three of
those four existed; the state-of-each-agent half had no page at all (#378).

The data was all already served, so the page is a rendering job and the risk is
entirely in the rendering. Two readings can be wrong in ways nothing downstream
recovers from:

* **A stale agent must not read as a working one, and a working one must not read
  as dead.** ``/active`` lists only leases inside their TTL; a lease is renewed
  once per *prompt* and runs thirty minutes against a claim's hour, so a single
  long autonomous turn drops a working agent out of ``/active`` (#252). A naive
  render therefore reports a busy agent as gone. ``qb-reconcile`` already refuses
  to guess between "finished" and "busy for twenty minutes" and says so in as
  many words; this page has to do the same thing rather than resolve it.
* **A finished session and a slow one are different rows.** ``qb-end`` (#277)
  records a closed reason set and ``GET /sessions`` carries an ``ended`` block
  that is null for a lease nobody ended. A view that collapsed those throws away
  the distinction that was built to draw them apart.

There is no JS test runner here, so the page half of this greps the file that
ships — the same crude-but-real guard ``test_plan_page.py`` uses on ``plan.html``
and ``test_reviewer_cost.py`` uses on ``reviews.html``. The endpoint half runs for
real against the app.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path

import pytest

from app.api.leases import END_REASONS

from .conftest import LAPTOP, PINNED_SETTINGS, SERVER

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGE = REPO_ROOT / "app/static/fleet.html"
RECONCILE = REPO_ROOT / "harness/bin/qb-reconcile"
QBDATA = REPO_ROOT / "harness/bin/qbdata.py"
BOARD_VIEWS = REPO_ROOT / "mcp/mcp_server/board/views.py"

#: A person, as the edge proves one: the identity header AND the secret only the
#: auth proxy knows. Both halves, every time — that is the whole boundary.
EDGE = {"Remote-User": "rich", "X-Edge-Auth": PINNED_SETTINGS["HUMAN_EDGE_SECRET"]}
#: The half of it any caller can send, and must never be believed on its own.
SPOOFED = {"Remote-User": "rich"}


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


# ---- the page is served, and only to somebody the board will talk to ---------


async def test_the_page_is_served_and_needs_the_same_authentication_as_the_board(client):
    """A read, authorised like every other read: an agent's token, or a browser
    the edge vouched for. Not looser — the fleet view names every session on the
    board, which is more than a stranger has any business enumerating."""
    r = await client.get("/fleet", headers=LAPTOP)
    assert r.status_code == 200, r.text
    assert "quarterback fleet" in r.text
    assert (await client.get("/fleet")).status_code == 401


def test_the_page_holds_no_credential(page):
    """The browser is authenticated at the edge; the page never holds a machine
    credential, and a token pasted into it would ship to every reader."""
    assert "Bearer" not in page
    assert "Authorization" not in page


# ---- mobile-first is the requirement, not a nice-to-have --------------------


def test_the_page_is_built_for_a_phone(page):
    """It is read one-handed on a phone or it is not read. A viewport meta, room
    for the notch, and text that iOS will not zoom the page to reach."""
    assert re.search(r'<meta name="viewport"[^>]*width=device-width', page), \
        "no viewport meta — the page renders at desktop width on a phone"
    assert "env(safe-area-inset-bottom)" in page and "env(safe-area-inset-left)" in page, \
        "a notch is on the side in landscape, not only along the bottom"
    assert "viewport-fit=cover" in page, "safe-area insets are all zero without it"


def test_every_touch_target_is_big_enough_to_hit(page):
    """44px is the floor below which a control is missed rather than pressed, and
    the one verb on this page ends somebody's session — a mis-tap is not free."""
    heights = [int(n) for n in re.findall(r"min-height:(\d+)px", page)]
    assert heights, "no min-height on any control — nothing is guaranteed tappable"
    assert min(heights) >= 44, f"a control is only {min(heights)}px tall"


def test_the_reason_buttons_do_not_zoom_the_viewport_on_focus(page):
    """Safari zooms the page when a control under 16px takes focus, and the sheet
    jumps out from under the thumb mid-choice — on the sheet that ends a
    session."""
    sheet = re.search(r"\.reasons button \{[^}]*\}", page)
    assert sheet, "could not find the reason buttons' rule"
    size = re.search(r"font-size:(\d+)px", sheet.group(0))
    assert size and int(size.group(1)) >= 16, \
        "a reason button under 16px zooms the viewport when it is focused"


# ---- the ambiguity is shown, not resolved -----------------------------------


def test_the_page_words_the_lease_ambiguity_the_way_qb_reconcile_already_does():
    """One ambiguity, one wording.

    ``qb-reconcile``'s ``_LEASE_ASYMMETRY`` is the sentence that already works,
    and it exists because reporting an absent lease as a dead claim *"is a finding
    accusing a working agent of holding a dead claim, re-posted every fifteen
    minutes"*. Two readers of one ambiguity wording it two ways teaches an
    operator to believe whichever they read first, and there is nothing outside
    either of them that says which to pick.
    """
    src = RECONCILE.read_text(encoding="utf-8")
    block = re.search(r"_LEASE_ASYMMETRY = \((.*?)\)\n", src, re.S)
    assert block, "could not find _LEASE_ASYMMETRY in harness/bin/qb-reconcile"
    harness = "".join(re.findall(r'"([^"]*)"', block.group(1)))
    sentence = harness.split(";")[0].strip()
    assert sentence, "the harness sentence parsed empty — this guard needs repointing"

    page = " ".join(PAGE.read_text(encoding="utf-8").split())
    # The page assembles it from string parts across lines, so compare on the
    # rendered text rather than the source layout.
    joined = re.sub(r'"\s*\+\s*"', "", page)
    assert sentence in joined, \
        f"the fleet page must use qb-reconcile's own wording: {sentence!r}"


def test_an_absent_lease_is_never_reported_as_a_dead_agent(page):
    """The false-clean this repo has settled five separate times today.

    ``/active`` answers "who renewed a lease inside its TTL", and nothing else.
    Read as "who is alive" it reports a busy agent as gone — so the page's own
    vocabulary must not contain a verdict that asserts death, and the two shapes
    of silence must be named as silence.
    """
    verdicts = re.search(r"const rank = \{([^}]*)\}", page)
    assert verdicts, "could not find the verdict vocabulary"
    words = set(re.findall(r"(\w+):", verdicts.group(1)))
    assert words == {"live", "unclear", "ended", "unreported"}, \
        f"the page's verdicts changed: {words}"
    for dead in ("dead", "gone", "crashed", "stalled:"):
        assert f'v:"{dead}"' not in page, f"{dead!r} is a conclusion, not a reading"


def test_a_long_turn_is_never_called_stalled(page):
    """#252's other half. ``stalled`` is what the dashboard concludes from an old
    ``working``, and on a phone the row is all the reader has — so this page names
    both readings instead of picking one."""
    assert "not calling it stalled" in page, \
        "the page must say out loud that it is not concluding a stall"
    # And the threshold it remarks on has to be the one every other reader uses.
    src = QBDATA.read_text(encoding="utf-8")
    theirs = re.search(r"^STALL_AFTER = (\d+)", src, re.M)
    assert theirs, "could not find STALL_AFTER in harness/bin/qbdata.py"
    mine = re.search(r"^const STALL_AFTER = (\d+);", page, re.M)
    assert mine, "the page must carry the same threshold"
    assert mine.group(1) == theirs.group(1), \
        "the dashboard, the pane footer and this page must agree when a state is old"


def test_a_row_the_board_cannot_settle_is_never_hidden_behind_a_toggle(page):
    """A settled row can wait behind `show settled`; an unsettled one cannot.

    Hiding the rows nobody has accounted for is the same false-clean by omission —
    the page would look tidy precisely when the fleet is not.
    """
    assert re.search(r"if\(!d\.settled \|\| settled\) shown\.push", page), \
        "live and unclear rows must render whatever the toggle says"
    assert re.search(r"settled:\s*false", page) and re.search(r"settled:\s*true", page), \
        "the verdicts must say which of them a person has actually accounted for"


def test_the_page_cap_can_only_ever_drop_a_settled_row(page):
    """A few hundred sessions nobody ever ended is a page that does not open on a
    phone, so the tail is capped. A `live` or `unclear` row dropped by a page
    limit would be the same false-clean arriving through the back door, and there
    is no number of them that makes cutting one right."""
    assert re.search(r"const capped = shown\.slice\(0, unsettled\.length \+ SETTLED_CAP\)", page), \
        "the cap must sit on top of every unsettled row, never compete with them"


def test_a_claim_that_names_no_session_is_still_on_the_page(page):
    """It belongs to a machine, not to an agent, so it cannot be a row — and left
    off entirely it is work in flight invisible on the page whose whole job is
    naming work in flight."""
    assert "orphanClaims" in page, "claims naming no session must be kept"
    assert "no session" in page, "and the page must say what they are"


# ---- a finished session and a slow one are different rows (#277) ------------


def test_the_page_reads_the_ending_block_rather_than_inferring_one(page):
    """``ended`` is null for a lease nobody ended, and that null is the whole
    distinction. A page that decided "finished" from `live: false` would report a
    crashed session and a completed one identically."""
    assert "r.ended" in page, "the page must read the ending block"
    assert re.search(r'v:"ended".*?r\.ended\.reason', page, re.S), \
        "an ended row must name the reason somebody reported"


def test_the_reasons_the_sheet_offers_are_the_reasons_the_server_takes():
    """The vocabulary is closed on purpose. A sixth spelling offered here is a 422
    with a friendly label on it; a missing one is a reason a person cannot
    record."""
    page = PAGE.read_text(encoding="utf-8")
    block = re.search(r"const END_REASONS = \[(.*?)\n\];", page, re.S)
    assert block, "the page's reason list moved — this guard needs repointing"
    offered = [m for m in re.findall(r'\["(\w+)",', block.group(1))]
    assert set(offered) == set(END_REASONS), \
        f"the page and app.api.leases.EndReason disagree: {set(offered) ^ set(END_REASONS)}"


def test_the_page_offers_no_reason_that_is_a_conclusion(page):
    """``stalled`` and ``crashed`` are refused by the server for the reason
    ``LeaseIn.state`` refuses ``stalled``: both are things a reader concludes from
    silence, never a report anybody makes. A button for one would invite a person
    to record a guess as an observation."""
    block = re.search(r"const END_REASONS = \[(.*?)\n\];", page, re.S)
    offered = set(re.findall(r'\["(\w+)",', block.group(1)))
    assert not offered & {"stalled", "crashed"}


# ---- one verb, and deliberately only one ------------------------------------


def test_the_page_writes_to_exactly_one_endpoint(page):
    """One verb — end a session — because it is the one somebody needs from a
    phone when something has gone wrong. Everything else is a later argument."""
    posts = set(re.findall(r'fetch\("([^"]+)",\s*\{method:"POST"', page))
    assert posts == {"/session/end"}, f"the page posts to more than one place: {posts}"


def test_the_page_has_no_spawn_button(page):
    """``qb-start`` is off by default per machine (#360), a phone is the worst
    place to reason about whether a box has opted in, and #371 is where that
    argument belongs. The issue says so explicitly."""
    for verb in ("/session/start", "qb-start", "spawn", "/seat"):
        assert verb not in page, f"{verb!r} is not this page's argument"


# ---- the write path: a person may end a session, an unproved header may not --


async def lease(client, session, headers=LAPTOP, device="laptop", **over):
    body = {"session": session, "device": device, "ttl": 300, **over}
    r = await client.post("/lease", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def test_a_person_at_the_edge_can_end_a_session(client):
    """The whole point of the page. Until #378 the endpoint depended on
    :func:`app.auth.identify`, which wants a bearer token no browser holds — so
    the one verb a person needs from a phone was the one they could not reach."""
    await lease(client, "s-fleet-person", headers=LAPTOP, cwd="/src/q")
    r = await client.post("/session/end",
                          json={"session": "s-fleet-person", "reason": "killed"},
                          headers=EDGE)
    assert r.status_code == 200, r.text
    assert r.json()["ended"] is True
    state = (await client.get("/session/s-fleet-person", headers=LAPTOP)).json()
    assert state["ended"]["reason"] == "killed"


async def test_a_person_ends_a_session_on_any_box(client):
    """A person is not another machine, so the machine check has no answer for
    them. It stops zeus ending a session on the laptop; it must not stop the
    person whose fleet both are."""
    await lease(client, "s-fleet-far", headers=SERVER, device="server", cwd="/src/q")
    r = await client.post("/session/end",
                          json={"session": "s-fleet-far", "reason": "killed"},
                          headers=EDGE)
    assert r.status_code == 200, r.text
    assert r.json()["ended"] is True


async def test_a_remote_user_the_edge_did_not_vouch_for_cannot_end_a_session(client):
    """``Remote-User`` is an ordinary request header. The boundary is the secret
    the proxy injects with it, and it is the same boundary that guards the plan's
    order — a header anyone can send cannot separate a person from an agent."""
    await lease(client, "s-fleet-spoof", headers=LAPTOP, cwd="/src/spoof")
    r = await client.post("/session/end",
                          json={"session": "s-fleet-spoof", "reason": "killed"},
                          headers=SPOOFED)
    assert r.status_code == 403, r.text
    assert "not asserted by the edge" in r.json()["detail"]
    # And nothing happened: the lease is still there to be renewed. A refusal
    # that had already released the claims would be the worst of both.
    live = (await client.get("/active", params={"cwd": "/src/spoof"}, headers=LAPTOP)).json()
    assert [a["session"] for a in live["agents"]] == ["s-fleet-spoof"]


async def test_an_agents_reach_is_unchanged(client):
    """The person path is an addition, not a widening. One machine still cannot
    end another's session — only the box a session runs on can see what closing it
    does."""
    await lease(client, "s-fleet-theirs", headers=SERVER, device="server", cwd="/src/q")
    r = await client.post("/session/end",
                          json={"session": "s-fleet-theirs", "reason": "killed"},
                          headers=LAPTOP)
    assert r.status_code == 403, r.text


async def test_a_persons_ending_hands_back_the_claims_that_session_took(client):
    """``may_mutate`` asks which machine the caller is, and a person shares one
    with nothing on the fleet — so the ordinary rule refuses every row and the
    verb would return 200 having done none of its job."""
    await lease(client, "s-fleet-claims", headers=LAPTOP, cwd="/src/q")
    r = await client.post("/claim", json={
        "kind": "merge", "key": "prisonblues/quarterback:fleet",
        "session": "s-fleet-claims", "note": "landing"}, headers=LAPTOP)
    assert r.status_code == 200, r.text

    body = (await client.post("/session/end",
                              json={"session": "s-fleet-claims", "reason": "killed"},
                              headers=EDGE)).json()
    assert [c["key"] for c in body["released_claims"]] == ["prisonblues/quarterback:fleet"]
    assert not body.get("refused_claims")
    held = (await client.get("/claims", params={"kind": "merge",
                                                "key": "prisonblues/quarterback:fleet"},
                             headers=LAPTOP)).json()["claims"]
    assert held == []


async def test_a_persons_ending_leaves_a_claim_that_names_no_session_alone(client):
    """``create-worktree`` takes one before the agent that will use the tree
    exists, so it belongs to the box. A person's authority here is the session,
    and a claim naming none is outside it — the SELECT is what guarantees that,
    and this is the test that says so out loud."""
    await lease(client, "s-fleet-machine", headers=LAPTOP, cwd="/src/q")
    await client.post("/claim", json={
        "kind": "merge", "key": "prisonblues/quarterback:boxwide",
        "note": "worktree claim, no session"}, headers=LAPTOP)

    r = await client.post("/session/end",
                          json={"session": "s-fleet-machine", "reason": "killed"},
                          headers=EDGE)
    assert r.status_code == 200, r.text
    assert r.json()["released_claims"] == []
    held = (await client.get("/claims", params={"kind": "merge",
                                                "key": "prisonblues/quarterback:boxwide"},
                             headers=LAPTOP)).json()["claims"]
    assert len(held) == 1


# ---- the verb has to work on the rows the page exists to surface -------------


async def test_a_lease_that_merely_lapsed_can_still_be_told_what_happened(client):
    """The whole point of the button on an `unclear` row.

    `/session/end` stamped the reason onto an *active* lease and did nothing at
    all otherwise, so the one case a person opens this page for — an agent that
    went quiet twenty minutes ago and never came back — was exactly the one the
    verb could not record. The row stayed "nobody ever said", permanently,
    because the only window in which anything could be said had already closed.
    """
    await lease(client, "s-fleet-lapse", headers=LAPTOP, ttl=1, cwd="/src/lapse")
    await asyncio.sleep(1.1)

    r = await client.post("/session/end",
                          json={"session": "s-fleet-lapse", "reason": "killed"},
                          headers=EDGE)
    assert r.status_code == 200, r.text
    assert r.json()["ended"] is True
    # And it says what it FOUND, which was not a live session to release.
    assert r.json()["lease_was"] == "lapsed"

    state = (await client.get("/session/s-fleet-lapse", headers=LAPTOP)).json()
    assert state["ended"]["reason"] == "killed"


async def test_the_recorded_ending_is_when_the_lease_stopped_not_when_somebody_said_so(client):
    """A lease that lapsed on Tuesday did not end on Thursday because that is
    when a person got round to saying so. `expires_at` is the last moment the
    board knows the session was alive, and it is the closest thing to an answer
    there is."""
    got = await lease(client, "s-fleet-when", headers=LAPTOP, ttl=1, cwd="/src/when")
    await asyncio.sleep(1.1)
    await client.post("/session/end",
                      json={"session": "s-fleet-when", "reason": "timed_out"},
                      headers=EDGE)
    state = (await client.get("/session/s-fleet-when", headers=LAPTOP)).json()
    assert state["ended"]["at"] == got["expires"]


async def test_stamping_a_lapsed_lease_still_belongs_to_its_own_machine(client):
    """Reaching this path used to be a no-op and so needed no authority. It
    WRITES now, and a record of who stopped what does."""
    await lease(client, "s-fleet-farlapse", headers=SERVER, device="server",
                ttl=1, cwd="/src/farlapse")
    await asyncio.sleep(1.1)
    r = await client.post("/session/end",
                          json={"session": "s-fleet-farlapse", "reason": "killed"},
                          headers=LAPTOP)
    assert r.status_code == 403, r.text
    # 404 rather than an `ended: null`, and it is the stronger assertion: this
    # session never pushed a transcript, so a recorded ending is the ONLY thing
    # that could make `GET /session/{key}` answer at all (#277). Nothing was
    # written.
    assert (await client.get("/session/s-fleet-farlapse", headers=LAPTOP)).status_code == 404


async def test_a_handoff_is_still_not_an_ending_to_stamp_over(client):
    """A device handing a session to another device has not finished anything,
    and the released lease it leaves behind is not an empty slot for a reason."""
    got = await lease(client, "s-fleet-handed", headers=LAPTOP, cwd="/src/handed")
    r = await client.post("/lease/release", json={"lease_id": got["lease_id"]},
                          headers=LAPTOP)
    assert r.status_code == 200, r.text
    body = (await client.post("/session/end",
                              json={"session": "s-fleet-handed", "reason": "killed"},
                              headers=EDGE)).json()
    assert body["ended"] is False and body["lease_was"] == "already released"
    # Same reasoning as above: with no transcript behind it, an answer here would
    # itself be the ending this test says was not recorded.
    assert (await client.get("/session/s-fleet-handed", headers=LAPTOP)).status_code == 404


async def test_an_ending_with_no_transcript_behind_it_is_still_a_row(client):
    """A session that never pushed a transcript has no `sessions` row, so it was
    visible exactly while it held a lease and vanished from the list the moment
    it ended — the one transition a fleet view most needs to show. #277 fixed the
    same hole in `GET /session/{key}` and left the list, which is what a page
    renders."""
    await lease(client, "s-fleet-notranscript", headers=LAPTOP, cwd="/src/nt")
    await client.post("/session/end",
                      json={"session": "s-fleet-notranscript", "reason": "finished"},
                      headers=LAPTOP)

    def rows(**params):
        return client.get("/sessions", params={"limit": 500, **params}, headers=LAPTOP)

    # Off by default: this list is paged, and folding an unbounded second
    # population into it would spend an existing reader's page on rows it never
    # asked for.
    plain = (await rows()).json()
    assert not any(r["session"] == "s-fleet-notranscript" for r in plain)

    widened = (await rows(include_ended="true")).json()
    row = next((r for r in widened if r["session"] == "s-fleet-notranscript"), None)
    assert row is not None, "an ended session with no transcript fell off /sessions"
    assert row["live"] is False and row["resumable"] is False
    assert row["ended"]["reason"] == "finished"


async def test_a_lapsed_session_with_no_transcript_is_not_invented_as_a_row(client):
    """Only an ending gets a row of its own here. A lease nobody renewed and
    nobody reported on says nothing, and manufacturing a row for it would be this
    page's own failure mode written into the endpoint."""
    await lease(client, "s-fleet-quietnt", headers=LAPTOP, ttl=1, cwd="/src/qnt")
    await asyncio.sleep(1.1)
    rows = (await client.get("/sessions",
                             params={"limit": 500, "include_ended": "true"},
                             headers=LAPTOP)).json()
    assert not any(r["session"] == "s-fleet-quietnt" for r in rows)


# ---- the clock a silence is measured against --------------------------------


async def test_the_sessions_list_carries_the_clock_a_silence_is_measured_against(client):
    """`updated_at` is the TRANSCRIPT's clock and moves on `/snapshot`; the lease
    moves on every prompt. Where they diverge the gap runs the wrong way — a
    session that pushed at ten, renewed until noon and then died is two minutes
    quiet at 12:02 and two hours quiet by the transcript — and a fleet view
    reading the wrong one calls a working agent long gone."""
    got = await lease(client, "s-fleet-clock", headers=LAPTOP, cwd="/src/clock")
    rows = (await client.get("/sessions", params={"limit": 500}, headers=LAPTOP)).json()
    row = next(r for r in rows if r["session"] == "s-fleet-clock")
    assert row["last_lease"]["expires"] == got["expires"]
    assert row["last_lease"]["holder"] == row["holder"]
    assert row["last_lease"]["released"] is None and row["last_lease"]["end_reason"] is None


def test_the_page_measures_silence_off_the_lease_not_the_transcript(page):
    """The other half of the same fact, on the side that renders it."""
    clock = re.search(r"function heardAt\(r\)\{(.*?)\n\}", page, re.S)
    assert clock, "could not find the page's silence clock"
    assert "last_lease" in clock.group(1), \
        "the page must measure a silence against the lease that fell silent"
    # And `updated_at` may only be the FALLBACK — a row built from a claim alone
    # has no lease to ask.
    assert clock.group(1).index("last_lease") < clock.group(1).index("updated_at")


async def test_a_resumed_session_is_ended_on_its_live_lease_not_its_lapsed_one(client):
    """A key can be leased again, and the stamp path must never reach past a live
    lease to the dead one behind it. `lease_was` is how a caller can tell which
    lease this call actually found."""
    await lease(client, "s-fleet-resume", headers=LAPTOP, ttl=1, cwd="/src/resume")
    await asyncio.sleep(1.1)
    await lease(client, "s-fleet-resume", headers=LAPTOP, cwd="/src/resume")  # resumed

    body = (await client.post("/session/end",
                              json={"session": "s-fleet-resume", "reason": "finished"},
                              headers=EDGE)).json()
    assert body["ended"] is True
    assert body["lease_was"] == "released", "it found a live lease, not a lapsed one"
    assert (await client.get("/active", params={"cwd": "/src/resume"},
                             headers=LAPTOP)).json()["agents"] == []


def test_the_page_asks_for_the_endings_the_list_hides_by_default(page):
    """`include_ended` is off in `GET /sessions` because it widens a paged list
    every other reader walks. This page is the reader it was added for, so it has
    to ask — a page that did not would drop a session the moment it ended."""
    assert "include_ended=true" in page, \
        "the fleet page must opt into endings with no transcript behind them"


async def test_a_session_with_a_transcript_is_never_re_rendered_as_one_without(client):
    """`include_ended` adds endings that have no `sessions` row behind them, and
    that is the condition it must filter on. Excluding the keys already on the
    page looks equivalent and is not: the page is one page, so a session whose
    record sits past the limit came back as transcript-less — `blob: null`,
    `resumable: false` — about a session that is perfectly resumable."""
    jsonl = b'{"turn":1}\n'
    sha = hashlib.sha256(jsonl).hexdigest()
    await client.put(f"/blob/{sha}", content=jsonl, headers=LAPTOP)
    await lease(client, "s-fleet-hastranscript", headers=LAPTOP, cwd="/src/hast")
    await client.post("/snapshot", json={"session": "s-fleet-hastranscript", "blob": sha},
                      headers=LAPTOP)
    await client.post("/session/end",
                      json={"session": "s-fleet-hastranscript", "reason": "finished"},
                      headers=LAPTOP)

    rows = (await client.get("/sessions", params={"limit": 500, "include_ended": "true"},
                             headers=LAPTOP)).json()
    row = next(r for r in rows if r["session"] == "s-fleet-hastranscript")
    assert row["blob"] == sha and row["resumable"] is True
    assert row["ended"]["reason"] == "finished"


def test_the_widening_query_filters_on_the_condition_it_names():
    """The invariant above cannot be produced on demand — it needs a record that
    has fallen off its own page, and a test cannot backdate one through the API.
    So the filter itself is pinned, because the filter is where the difference
    lives: "already on this page" and "has no transcript" are two different
    questions, and only the second is the one this branch is answering."""
    src = (REPO_ROOT / "app/api/leases.py").read_text(encoding="utf-8")
    assert "Lease.session.not_in(select(SessionRecord.session))" in src, \
        "the widening query must exclude every session that HAS a record, not "\
        "merely the ones this page happens to hold"


# ---- how far along, and the one word for "nobody said" (#262) ----------------


def test_the_page_shows_how_far_along_each_live_agent_is(page):
    """The question repo, branch and title cannot answer.

    Those three read identically writing the first cut and coming out of the
    third review round; `stage` is what `qb-stage` reports and nothing else can
    derive, so a page without it can say what an agent is on and not how far
    along it is.
    """
    assert "stageChip" in page, "the page must render a stage"
    assert re.search(r"l\.stage", page), "and it must read it off the lease"
    assert re.search(r"\$\{stageChip\(l\)\}", page), \
        "the chip belongs beside the verdict, where `state` is already read"


def test_a_live_lease_that_reported_no_stage_says_unreported(page):
    """The majority case, in the page's own word for silence.

    `unreported` is already what this page calls a session nobody ended, and one
    vocabulary for silence is the whole point — a blank chip, or no chip, would
    read as "this agent has no stage", which is the class of lie #261 named.
    """
    assert re.search(r'class="stage unreported"[^>]*>?[^<]*unreported', page, re.S), \
        "an unreported stage must say so in the page's own word"
    # And the word must be one of the four this page already uses for silence,
    # not a fifth invented beside them.
    words = set(re.findall(r'v:"(\w+)"', page))
    assert "unreported" in words


def test_the_chip_only_exists_where_a_lease_does(page):
    """A stage lives on the lease, so a row with no live lease has no field to be
    silent about. Printing `unreported` there would invent a third state out of a
    question that does not arise."""
    assert re.search(r"function stageChip\(l\)\{\s*\n?\s*if\(!l\) return \"\";", page), \
        "no lease, no chip"


def test_every_surface_spells_an_unreported_stage_the_same_way():
    """A terminal that quietly draws an empty cell where the browser says
    `unreported` is two vocabularies for one fact.

    The browser has the width for the word and the panels have six characters, so
    the terminals use the glyph they already use for every unsaid value. What is
    pinned here is that there is exactly ONE such glyph across the two client
    codebases, and that it cannot be mistaken for a stage: a stage is 1-6
    alphanumerics (`app.api.leases.STAGE_RE`), and this is not alphanumeric.
    """
    glyphs = {}
    for path in (QBDATA, BOARD_VIEWS):
        # Read, not imported: `mcp_server` is a second installable package with
        # its own venv and `qbdata.py` is a script beside a bin/, so neither is on
        # this suite's path — the same reason STALL_AFTER is grepped above.
        found = re.search(r'^STAGE_UNREPORTED = "(.+)"', path.read_text(encoding="utf-8"), re.M)
        assert found, f"could not find STAGE_UNREPORTED in {path}"
        glyphs[path.name] = found.group(1)

    assert len(set(glyphs.values())) == 1, \
        f"qb-dash and qb-board must render an unreported stage the same way: {glyphs}"
    glyph = next(iter(glyphs.values()))
    assert not glyph.isalnum(), \
        "the unreported glyph must sit outside the value space a stage can occupy"
    assert re.search(r"^STAGE_RE = ", (REPO_ROOT / "app/api/leases.py").read_text(), re.M), \
        "and that value space has to still be the thing STAGE_RE describes"
