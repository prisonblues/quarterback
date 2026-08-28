"""What the blocker tools decide before anything reaches the board (#328).

The MCP layer cannot import the board's models, so what these can get wrong
locally is the shape of the request: which subject was named, whether the caller
gave exactly one, and whether a filter says what it meant.

Skipped without the `server` extra, for the reason `test_server_claim_tools.py`
gives.

Run: uv run --extra dev --extra server pytest mcp/tests/test_server_blocker_tools.py
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="the MCP SDK is only in the `server` extra")

from mcp.server.fastmcp.exceptions import ToolError
from mcp_server import server as srv


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def blocker_write(self, path: str, body: dict) -> dict:
        self.calls.append((path or "raise", body))
        return {"blocker": {"id": "b1"}, "raised": True}

    def blockers(self, params: dict) -> dict:
        self.calls.append(("read", params))
        return {"blockers": [], "open": 0}


@pytest.fixture()
def sent(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(srv, "_get_client", lambda ctx: rec)
    monkeypatch.setattr(srv, "_derive_repo", lambda path: "acme/here")
    return rec


def test_a_plan_item_is_the_subject_and_carries_no_derived_repo(sent):
    """An item id is globally unique and already knows its scope. Deriving a repo
    from the caller's checkout would scope the blocker to wherever the agent
    happened to be standing, which is not where the item is."""
    srv.plan_block(None, question="A or B?", item_id="i-1")
    _, body = sent.calls[0]
    assert body["subject_kind"] == "item" and body["subject_value"] == "i-1"
    assert body["repo"] is None


def test_an_issue_takes_the_checkout_s_repo(sent):
    """An issue number means nothing without one — #148's rule, and the same
    reason every other tool here derives rather than asking."""
    srv.plan_block(None, question="ship it?", issue=42)
    _, body = sent.calls[0]
    assert body == dict(body, subject_kind="issue", subject_value="42",
                        repo="acme/here")


def test_naming_two_subjects_is_refused_before_the_network(sent):
    """What is blocked has to be one thing, or the queue cannot say what answering
    it would release."""
    with pytest.raises(ToolError) as e:
        srv.plan_block(None, question="?", item_id="i-1", pr=7)
    assert not sent.calls
    assert "exactly one" in str(e.value)


def test_naming_none_is_refused_too(sent):
    with pytest.raises(ToolError):
        srv.plan_block(None, question="?")
    assert not sent.calls


def test_the_class_defaults_to_decision_and_is_passed_through(sent):
    """The board owns the vocabulary and refuses an unknown value with the list —
    this layer must not keep a second copy to validate against (#56)."""
    srv.plan_block(None, question="?", item_id="i-1")
    assert sent.calls[0][1]["kind"] == "decision"
    sent.calls.clear()
    srv.plan_block(None, question="?", item_id="i-1", kind="anything-at-all")
    assert sent.calls[0][1]["kind"] == "anything-at-all"


def test_unblocking_sends_the_resolution_and_nothing_else(sent):
    srv.plan_unblock(None, blocker_id="b1", resolution="go with A")
    path, body = sent.calls[0]
    assert path == "/resolve"
    assert body == {"blocker_id": "b1", "resolution": "go with A"}


def test_the_queue_asks_for_open_ones_by_default(sent):
    srv.blockers(None)
    _, params = sent.calls[0]
    assert params == {"open": "true"}


def test_include_resolved_flips_it_and_filters_are_omitted_when_unset(sent):
    """An omitted filter must not go out as null: the board reads a present key as
    "filter by this", so `owner=None` would ask for blockers owned by nobody."""
    srv.blockers(None, include_resolved=True, owner="human/rich")
    _, params = sent.calls[0]
    assert params == {"open": "false", "owner": "human/rich"}


def test_all_three_tools_are_registered():
    names = {t.name for t in srv.mcp._tool_manager.list_tools()}
    assert {"plan_block", "plan_unblock", "blockers"} <= names
