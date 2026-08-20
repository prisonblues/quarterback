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
