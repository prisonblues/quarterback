"""What `plan_reorder` and `plan_item_update` decide before anything is sent.

These two are the only tools in this server that write through
`app.auth.human` (#479), and the whole of what they can get wrong locally is the
body: a scope silently widened, an optional field sent as null and clobbering a
note, or a setup failure arriving as something a caller would retry.

Skipped without the `server` extra, for the reason `test_server_claim_tools.py`
gives.

Run: uv run --extra dev --extra server pytest mcp/tests/test_server_human_write_tools.py
"""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("mcp", reason="the MCP SDK is only in the `server` extra")

from mcp.server.fastmcp.exceptions import ToolError
from mcp_server import server as srv


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
    """No cookie is not a transient fault and not an answer about the request —
    it must not read as something to retry."""
    wire(monkeypatch, RuntimeError("QUARTERBACK_EDGE_COOKIE is not set"))
    with pytest.raises(ToolError) as e:
        srv.plan_reorder(None, order=["a"])
    assert "plan_reorder" in str(e.value)
    assert "QUARTERBACK_EDGE_COOKIE" in str(e.value)


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


def test_both_tools_are_registered(monkeypatch):
    """They are the reason the server now reads two more environment variables;
    a tool that silently failed to register would leave that unexplained."""
    names = {t.name for t in srv.mcp._tool_manager.list_tools()}
    assert {"plan_reorder", "plan_item_update"} <= names
