"""The claim tools' own refusals, before anything reaches the board.

The MCP layer is a separate package that cannot import the board's models, so
every rule it shares with `app/api/claims.py` is a rule stated twice — and the two
statements drifting is the shape of #172 itself. This suite pins the ones a tool
can only get wrong locally:

  * `ref_kind` AND `kind`/`key` in one call. `ClaimIn._one_way_or_the_other`
    refuses that pair outright; the tools used to prefer the ref and drop the rest
    in silence, which leaves the caller believing it claimed the half that was
    thrown away.
  * neither of them, which is a caller that has not said what it wants.

Skipped without the `server` extra: importing the tools imports the MCP SDK, and a
tail-only install deliberately has none (see `test_package_contract.py`).

Run: uv run --extra dev --extra server pytest mcp/tests/test_server_claim_tools.py
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="the MCP SDK is only in the `server` extra")

from mcp.server.fastmcp.exceptions import ToolError
from mcp_server import server as srv


@pytest.fixture(autouse=True)
def _no_board(monkeypatch):
    """Nothing here may reach a board or a git checkout.

    Both are stubbed to raise, so a refusal that stopped happening fails loudly
    instead of quietly turning into a network call — the test would otherwise pass
    on a machine with a board and skip the point on one without.
    """
    def boom(*a, **k):
        raise AssertionError("the tool asked the board about an ambiguous request")

    monkeypatch.setattr(srv, "_get_client", boom)
    monkeypatch.setattr(srv, "_derive_repo",
                        lambda path: (_ for _ in ()).throw(
                            AssertionError("the tool derived a repo it did not need")))


@pytest.mark.parametrize("extra", [{"kind": "issue"}, {"key": "acme/widget#1"},
                                   {"kind": "issue", "key": "acme/widget#1"}])
def test_claiming_by_ref_AND_by_composed_key_is_refused_not_reconciled(extra):
    """The endpoint's own rule: "a request carrying both is a caller with two ideas
    about what it is claiming, and guessing which one it meant is how a claim lands
    on the wrong resource." Preferring the ref silently made the tool the softer
    door onto the same defect."""
    with pytest.raises(ToolError) as e:
        srv.claim(None, ref_kind="issue", ref_value="172", **extra)
    assert "not both" in str(e.value)
    assert "issue" in str(e.value), "the refusal has to show what it was sent"


@pytest.mark.parametrize("extra", [{"kind": "issue"}, {"key": "acme/widget#1"}])
def test_asking_by_ref_AND_by_composed_key_is_refused_too(extra):
    """A lookup is where two spellings actually bite: the answer is about one of the
    resources described, and read as the answer about the other it says "nobody
    holds it" of a row that is right there."""
    with pytest.raises(ToolError) as e:
        srv.claims(None, ref_kind="issue", ref_value="163", **extra)
    assert "not both" in str(e.value)


def test_a_ref_kind_with_no_value_still_says_which_value_is_missing():
    """Unchanged, and asserted next to the new refusal so the two cannot be
    conflated: this one is an incomplete description, not an ambiguous one."""
    with pytest.raises(ToolError) as e:
        srv.claim(None, ref_kind="issue")
    assert "ref_value" in str(e.value)


def test_saying_nothing_at_all_names_the_preferred_way_in():
    with pytest.raises(ToolError) as e:
        srv.claim(None)
    assert "ref_kind" in str(e.value)


def test_a_composed_pair_on_its_own_is_still_accepted(monkeypatch):
    """The compatibility half stays. Refusing it outright would have been tidier
    and wrong: every skill on the fleet composes a pair today, and an endpoint that
    422s them all writes no claims at all — the state #172 was filed about.

    Asserted through the body handed to the client rather than a response, because
    what matters here is that the pair survives to the board that canonicalises it.
    """
    sent: dict = {}

    class _Client:
        @staticmethod
        def claim(body):
            sent.update(body)
            return body

    monkeypatch.setattr(srv, "_get_client", lambda ctx: _Client())
    out = srv.claim(None, kind="issue", key="acme/widget#163", session="s-1")
    assert (out["kind"], out["key"]) == ("issue", "acme/widget#163")
    assert sent["session"] == "s-1"


def test_holding_a_whole_plan_can_say_force(monkeypatch):
    """A plan claim is now refused when another agent holds an item inside it, and
    `force` is the escape for the case where the item's holder really is sharing
    the plan. Without it forwarded here, a refusal the HTTP endpoint offers a way
    past is a dead end through the tool — a client that cannot spell the escape is
    a client for whom the escape does not exist.

    `force` is sent only when asked for, so the ordinary call is unchanged and the
    board's "said out loud when used" record stays meaningful.
    """
    sent: list[tuple[str, dict]] = []

    class _Client:
        @staticmethod
        def plan_verb(verb, body):
            sent.append((verb, body))
            return {"ok": True}

    monkeypatch.setattr(srv, "_get_client", lambda ctx: _Client())

    srv.plan_hold(None, plan_id="p-1")
    assert sent[-1] == ("claim", {"plan_id": "p-1", "note": None}), (
        "an ordinary hold must not start asserting force")

    srv.plan_hold(None, plan_id="p-1", force=True, note="sharing with amber-otter")
    verb, body = sent[-1]
    assert verb == "claim"
    assert body["force"] is True
    assert body["note"] == "sharing with amber-otter"


def test_the_force_escape_is_documented_where_an_agent_reads_it():
    """The docstring IS the tool's interface — an agent has nothing else to read —
    so a flag that exists and is not described is a flag nobody uses. It has to say
    what the refusal was, not merely that a flag exists."""
    doc = srv.plan_hold.__doc__ or ""
    assert "force" in doc
    assert "holder" in doc, (
        "the refusal names the item's holder and their session; the docstring has "
        "to say that a refusal is somebody to talk to")


# -- end_session (#277) -------------------------------------------------
#
# The tool checks the vocabulary before the call for the same reason the claim
# tools check their arguments: the board's refusal is a 422 the caller reads as
# "the board is unhappy", where a local one can say what the five words are. And
# the vocabulary being closed is the point of the field — it is branched on by a
# dashboard, so a sixth spelling of "finished" reaches a human as an unknown.


@pytest.mark.parametrize("reason", ["gave up", "stalled", "crashed", "", "FINISHED"])
def test_a_reason_outside_the_vocabulary_never_reaches_the_board(reason):
    """`stalled` and `crashed` are in here deliberately: both are conclusions a
    reader draws from silence, never reports an agent makes about itself."""
    with pytest.raises(ToolError) as e:
        srv.end_session(None, session="sid-1", reason=reason)
    assert "finished" in str(e.value) and "context_reset" in str(e.value)


@pytest.mark.parametrize("reason", list(srv.END_REASONS))
def test_every_reason_the_tool_advertises_is_one_it_will_send(reason, monkeypatch):
    """The list in the docstring and the list it enforces are one list. Two would
    disagree the day somebody adds a sixth to whichever they read first."""
    sent = {}

    class _Client:
        def end_session(self, session, why):
            sent.update(session=session, reason=why)
            return {"ended": True}

    monkeypatch.setattr(srv, "_get_client", lambda ctx: _Client())
    assert srv.end_session(None, session="sid-1", reason=reason)["ended"] is True
    assert sent == {"session": "sid-1", "reason": reason}


# -- lapsed_claims (#568) -----------------------------------------------
#
# The read that answers "who picked this up and vanished". It shares
# `_resource_params` with `claims`, which is the point: two tools asking about one
# row must not differ in how they name it, or "nobody holds that" and "nothing was
# abandoned there" become answers about two different keys.


@pytest.mark.parametrize("extra", [{"kind": "work"}, {"key": "acme/widget#1"}])
def test_asking_about_lapsed_work_two_ways_at_once_is_refused_too(extra):
    """Inherited from the shared builder, and pinned here anyway: a tool that
    stopped sharing it would fail this rather than silently answering about the
    half it preferred."""
    with pytest.raises(ToolError) as e:
        srv.lapsed_claims(None, ref_kind="issue", ref_value="196", **extra)
    assert "not both" in str(e.value)


def test_lapsed_claims_sends_the_ref_for_the_board_to_derive(monkeypatch):
    """The key is derived server-side, as everywhere else — deriving it in this
    package would be a second implementation of the rule in a package that cannot
    import the first (#172)."""
    sent: dict = {}

    class _Client:
        @staticmethod
        def lapsed_claims(params):
            sent.update(params)
            return {"claims": []}

    monkeypatch.setattr(srv, "_get_client", lambda ctx: _Client())
    monkeypatch.setattr(srv, "_derive_repo", lambda path: "acme/widget")
    assert srv.lapsed_claims(None, ref_kind="issue", ref_value="196")["claims"] == []
    assert sent["ref_kind"] == "issue" and sent["ref_value"] == "196"
    assert sent["repo"] == "acme/widget"
    assert "key" not in sent, "a composed key here would be the second spelling"


def test_the_tool_tells_an_agent_lapsed_is_not_released_and_it_never_refuses():
    """The docstring IS the interface — an agent has nothing else to read. Two
    things it has to carry: which population this is (a released claim means the
    work landed, and redirecting there is noise), and that a hit is advice rather
    than a reason to stop."""
    doc = srv.lapsed_claims.__doc__ or ""
    assert "Released means" in doc and "lapsed" in doc.lower()
    assert "advice" in doc.lower()
    assert "RECORDED" in doc, "the board cannot see that disk and has to say so"
