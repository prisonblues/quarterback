"""What the delegated tools decide before anything is sent.

`plan_reorder`, `plan_item_update` and — since #591 — `dial_set` and `dial_clear`
are the tools in this server that write through `app.auth.delegated` (#478) — NOT
`human`, which is the distinction the whole feature turns on — and the whole of
what they can get wrong locally is the body: a scope silently widened, an optional
field sent as null and clobbering a note, or a setup failure arriving as something
a caller would retry.

`dials` is the odd one out and is here for contrast: it READS, so it goes with the
ordinary bearer and must not demand the delegated credential. A read that refused
without one would make "what is in force?" — the question every agent has to be
able to answer — depend on a secret most hosts do not hold.

Skipped without the `server` extra, for the reason `test_server_claim_tools.py`
gives.

Run: uv run --extra dev --extra server pytest mcp/tests/test_server_delegated_tools.py
"""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("mcp", reason="the MCP SDK is only in the `server` extra")

from mcp.server.fastmcp.exceptions import ToolError
from mcp_server import server as srv
from mcp_server.client import QuarterbackClient

#: The client's OWN refusal text, not a string invented here. A fixture that
#: supplies the message it then asserts on proves only that `str(e)` works; taking
#: it from the source means this fails if the client stops naming the credential.
CLIENT_NO_CREDENTIAL = QuarterbackClient.NO_CREDENTIAL


class Recorder:
    def __init__(self, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._raises = raises

    def plan_reorder(self, body: dict) -> dict:
        self.calls.append(("reorder", body))
        if self._raises:
            raise self._raises
        return {"reordered": len(body.get("order", []))}

    def plan_item_update(self, body: dict) -> dict:
        self.calls.append(("update", body))
        if self._raises:
            raise self._raises
        return {"ok": True}

    def dial_set(self, body: dict) -> dict:
        self.calls.append(("dial_set", body))
        if self._raises:
            raise self._raises
        return {"dial": {"set_by": "laptop/agent", "set_via": "agent"}}

    def dial_clear(self, body: dict) -> dict:
        self.calls.append(("dial_clear", body))
        if self._raises:
            raise self._raises
        return {"cleared": []}

    def dials(self, repo: str | None = None) -> dict:
        self.calls.append(("dials", {"repo": repo}))
        if self._raises:
            raise self._raises
        return {"dials": []}


def wire(monkeypatch, raises=None) -> Recorder:
    rec = Recorder(raises)
    monkeypatch.setattr(srv, "_get_client", lambda ctx: rec)
    return rec


def test_a_reorder_sends_the_scope_exactly_as_given(monkeypatch):
    """`plan_read` widens a repo scope to include fleet-wide items; this must not.
    An order is per scope, and reordering a wider list than the caller named would
    permute rows they never saw."""
    rec = wire(monkeypatch)
    srv.plan_reorder(None, order=["a", "b"], repo="acme/one")
    assert rec.calls == [("reorder", {"repo": "acme/one", "order": ["a", "b"]})]


def test_an_omitted_scope_stays_null_rather_than_becoming_a_guess(monkeypatch):
    """null is the fleet-wide list — a real scope, not a missing argument."""
    rec = wire(monkeypatch)
    srv.plan_reorder(None, order=["a"])
    assert rec.calls[0][1]["repo"] is None


def test_update_sends_only_the_fields_it_was_given(monkeypatch):
    """The failure this prevents: passing note=None through as JSON null and
    erasing the reasoning of an item somebody meant only to retitle."""
    rec = wire(monkeypatch)
    srv.plan_item_update(None, item_id="x", note="corrected")
    assert rec.calls == [("update", {"item_id": "x", "note": "corrected"})]


def test_update_can_still_send_an_empty_string_because_it_means_something(monkeypatch):
    """"" detaches an item from its plan — distinct from "leave it alone"."""
    rec = wire(monkeypatch)
    srv.plan_item_update(None, item_id="x", plan="")
    assert rec.calls[0][1] == {"item_id": "x", "plan": ""}


def test_a_setup_failure_becomes_a_tool_error_naming_the_tool(monkeypatch):
    """No credential is not a transient fault and not an answer about the request —
    it must not read as something to retry."""
    wire(monkeypatch, RuntimeError(CLIENT_NO_CREDENTIAL))
    with pytest.raises(ToolError) as e:
        srv.plan_reorder(None, order=["a"])
    assert "plan_reorder" in str(e.value)
    assert "QUARTERBACK_ELEVATED_TOKEN" in str(e.value)


def test_the_board_s_own_refusal_is_surfaced_with_its_detail(monkeypatch):
    """A 422 naming items that are not open in the scope is the useful half of
    the answer, and swallowing it leaves the caller guessing."""
    resp = httpx.Response(
        422, json={"error": "those items are not open in this scope"},
        request=httpx.Request("POST", "https://human.example/plan/reorder"))
    wire(monkeypatch, httpx.HTTPStatusError("nope", request=resp.request,
                                            response=resp))
    with pytest.raises(ToolError) as e:
        srv.plan_reorder(None, order=["gone"])
    assert "422" in str(e.value)
    assert "not open in this scope" in str(e.value)


def test_the_delegated_tools_are_registered(monkeypatch):
    """They are the reason the server now reads two more environment variables;
    a tool that silently failed to register would leave that unexplained."""
    names = {t.name for t in srv.mcp._tool_manager.list_tools()}
    assert {"plan_reorder", "plan_item_update",
            "dial_set", "dial_clear", "dials"} <= names


# ---- dials (#591) -------------------------------------------------------------


def test_setting_a_dial_omits_the_scope_rather_than_sending_null(monkeypatch):
    """An omitted repo means FLEET-WIDE, and the board reads absent and null the
    same way here — but `value` does not, so the body must not acquire keys the
    caller never gave. Sending `repo: None` alongside a real one is how a
    fleet/repo distinction gets blurred at the only layer that can still keep it."""
    rec = wire(monkeypatch)
    srv.dial_set(None, dial="review_panel.max_rounds", value=3, reason="asked to")
    (_, body), = rec.calls
    assert "repo" not in body
    assert "expires_at" not in body
    assert body == {"dial": "review_panel.max_rounds", "value": 3,
                    "reason": "asked to"}


def test_a_null_dial_value_is_sent_and_not_dropped(monkeypatch):
    """`null` is a real setting — the documented off switch for `max_fix_growth`
    and others — and is NOT the same as clearing the dial. A body-builder that
    treated it as "unset" would silently turn one instruction into the other."""
    rec = wire(monkeypatch)
    srv.dial_set(None, dial="review_panel.max_fix_growth", value=None,
                 reason="off, as asked")
    (_, body), = rec.calls
    assert "value" in body and body["value"] is None


def test_clearing_sends_the_scope_exactly_as_given(monkeypatch):
    """Not widened, for `plan_reorder`'s reason and with a sharper consequence:
    omitting the repo clears the FLEET dial and leaves a repo dial of the same
    name standing, which is a different outcome rather than a broader one."""
    rec = wire(monkeypatch)
    srv.dial_clear(None, dial="tempo", repo="acme/one")
    assert rec.calls == [("dial_clear", {"dial": "tempo", "repo": "acme/one"})]
    rec.calls.clear()
    srv.dial_clear(None, dial="tempo")
    assert rec.calls == [("dial_clear", {"dial": "tempo"})]


def test_a_dial_read_does_not_need_the_delegated_credential(monkeypatch):
    """The contrast that keeps the split honest. `dials` goes through the ordinary
    bearer, so a host with no elevated secret can still answer "what is in force?"
    — and this asserts it by wiring the client to raise the no-credential error and
    checking the READ is unaffected while the WRITE is not."""
    rec = wire(monkeypatch)
    assert srv.dials(None) == {"dials": []}
    assert rec.calls == [("dials", {"repo": None})]


def test_a_dial_write_without_a_credential_names_the_tool(monkeypatch):
    """Same shape as `plan_reorder`'s: a setup failure, not an answer, and not
    something to retry."""
    wire(monkeypatch, RuntimeError(CLIENT_NO_CREDENTIAL))
    with pytest.raises(ToolError) as e:
        srv.dial_set(None, dial="tempo", value="eager", reason="asked to")
    assert "dial_set" in str(e.value)
    assert "QUARTERBACK_ELEVATED_TOKEN" in str(e.value)

    with pytest.raises(ToolError) as e:
        srv.dial_clear(None, dial="tempo")
    assert "dial_clear" in str(e.value)


# ---- a value that arrives as text (#699) --------------------------------------
#
# The bug these are about: `value` was annotated `object`, which produces a schema
# entry with no `type`, and a parameter with no declared type reached the board
# serialised as TEXT. `POST /dials` stores opaque JSON by design, so `"8000000"`
# was accepted, `dials` reported it as in force, and `harness_rules.board_dials`
# refused it on every read — the repo ran on its own defaults for the whole life of
# the row, and the one person who never found out was the one who set it.


def test_a_number_that_arrived_as_text_reaches_the_board_as_a_number(monkeypatch):
    """`review_panel.budget.tokens_per_pr` is a `number` dial, and the harness
    refuses `'8000000'` by name — "must be a number, not '8000000'". The string is
    what a caller that ignores the schema sends, so the string is what this asserts
    on; sending an int here would test pydantic rather than the tool."""
    rec = wire(monkeypatch)
    srv.dial_set(None, dial="review_panel.budget.tokens_per_pr", value="8000000",
                 reason="asked to", repo="acme/one")
    (_, body), = rec.calls
    assert body["value"] == 8000000
    assert isinstance(body["value"], int)


def test_a_boolean_that_arrived_as_text_reaches_the_board_as_a_boolean(monkeypatch):
    """The case with teeth. `'false'` is a NON-EMPTY string, so a reader that has
    not been told the vocabulary takes it for ON — a seat somebody switched off,
    dispatched anyway on every round, with the board showing it as off."""
    rec = wire(monkeypatch)
    srv.dial_set(None, dial="reviewers.sonarqube.enabled", value="false",
                 reason="asked to", repo="acme/one")
    (_, body), = rec.calls
    assert body["value"] is False


def test_a_word_is_left_alone_because_it_is_the_value(monkeypatch):
    """`P2`, `eager`, `shape`, `sonnet` — the values that were never broken, and
    the ones a coercion rule could easily break. None of them parse as JSON, which
    is exactly why the rule is "JSON where it parses" and not a lookup."""
    rec = wire(monkeypatch)
    for value in ("P2", "eager", "shape", "sonnet", ""):
        rec.calls.clear()
        out = srv.dial_set(None, dial="review_panel.fix_severity_floor",
                           value=value, reason="asked to")
        (_, body), = rec.calls
        assert body["value"] == value
        assert "value_read_as" not in out


def test_a_value_that_arrived_typed_is_passed_through_untouched(monkeypatch):
    """Nothing here is a parser for values that were already right. A caller that
    honours the schema — the fix's first half — must land in exactly the code path
    it landed in before, or the belt has become the mechanism."""
    rec = wire(monkeypatch)
    for value in (3, 3.5, True, False, None, ["a", "b"], {"P3": 2}):
        rec.calls.clear()
        out = srv.dial_set(None, dial="review_panel.threshold_by_severity",
                           value=value, reason="asked to")
        (_, body), = rec.calls
        assert body["value"] == value
        assert type(body["value"]) is type(value)
        assert "value_read_as" not in out


def test_the_reply_says_what_it_read_and_only_when_it_read_something(monkeypatch):
    """A silent coercion is the same class of problem as a silent refusal: the
    caller asked for one thing, something else is in force, and nothing said so.
    The note names both halves so a caller that meant the string can see it didn't
    get one — and it is absent on the ordinary path, so its presence means
    something."""
    rec = wire(monkeypatch)
    out = srv.dial_set(None, dial="review_panel.max_rounds", value="4",
                       reason="asked to")
    assert "'4'" in out["value_read_as"]          # what the caller sent
    assert "read as 4" in out["value_read_as"]    # what went to the board
    assert "699" in out["value_read_as"]          # where the reason is written down
    # And the board's own answer is still there, beside it rather than replaced.
    assert out["dial"] == {"set_by": "laptop/agent", "set_via": "agent"}

    out = srv.dial_set(None, dial="review_panel.max_rounds", value=4,
                       reason="asked to")
    assert "value_read_as" not in out


def test_the_value_parameter_declares_its_types(monkeypatch):
    """The half of the fix that has to hold at the boundary, where no assertion in
    this suite can reach: `object` yields `{"title": "Value"}` — no `type` — and a
    parameter with no declared type is one a caller may hand over as text. This is
    the only thing that keeps it spelled out, since every other test here passes
    the value straight to the function and never sees the schema at all."""
    tool, = (t for t in srv.mcp._tool_manager.list_tools() if t.name == "dial_set")
    spec = tool.parameters["properties"]["value"]
    declared = {branch.get("type") for branch in spec.get("anyOf", [])}
    assert {"string", "integer", "number", "boolean", "array", "object",
            "null"} <= declared


def test_a_non_finite_number_is_left_as_the_text_it_was(monkeypatch):
    """`json.loads` accepts `NaN` and `Infinity` as an extension; JSON has neither,
    and `POST /dials` refuses them outright (`allow_nan=False`). Reading one would
    take a value that WAS storable — the string somebody typed — and make it a 422.
    At any depth, hence `[NaN]` beside the bare word."""
    rec = wire(monkeypatch)
    for value in ("NaN", "Infinity", "-Infinity", "[NaN]"):
        rec.calls.clear()
        out = srv.dial_set(None, dial="review_panel.max_rounds", value=value,
                           reason="asked to")
        (_, body), = rec.calls
        assert body["value"] == value
        assert "value_read_as" not in out
