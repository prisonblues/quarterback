"""What the gate tools send before anything reaches the board (#852).

The board decides what a gate means; the MCP layer only has to derive the repo
the way every other tool here does (#148) and leave an omitted field out of the
body, because the board reads a present key as "set it".

Skipped without the `server` extra, for the reason `test_server_claim_tools.py`
gives.

Run: uv run --extra dev --extra server pytest mcp/tests/test_server_gate_tools.py
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="the MCP SDK is only in the `server` extra")

from mcp_server import server as srv

HEAD = "a" * 40


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def gates(self, params: dict) -> dict:
        self.calls.append(("read", params))
        return {"gates": []}

    def gate_write(self, path: str, body: dict) -> dict:
        self.calls.append((path or "set", body))
        return {"gate": {}}


@pytest.fixture()
def sent(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(srv, "_get_client", lambda ctx: rec)
    monkeypatch.setattr(srv, "_derive_repo", lambda path: "acme/here")
    return rec


def test_setting_a_gate_derives_the_repo_and_sends_only_what_was_given(sent):
    """The body carries exactly the fields the caller named, so it says what the
    caller meant to change and nothing more."""
    srv.gate_set(None, branch="integration/v22", head=HEAD, outputs="zeus:~/sim-runs")
    assert sent.calls == [("set", {"repo": "acme/here", "branch": "integration/v22",
                                   "head": HEAD, "outputs": "zeus:~/sim-runs"})]


def test_every_gate_set_field_reaches_the_body(sent):
    srv.gate_set(None, branch="b", head=HEAD, exit_bar=3, stage="db",
                 outputs="zeus:~/o", reset_rule="a sim/ change")
    assert sent.calls[0][1] == {"repo": "acme/here", "branch": "b", "head": HEAD,
                                "exit_bar": 3, "stage": "db", "outputs": "zeus:~/o",
                                "reset_rule": "a sim/ change"}


def test_reading_one_branch_names_it(sent):
    srv.gate(None, branch="integration/v22")
    assert sent.calls == [("read", {"repo": "acme/here", "branch": "integration/v22"})]


def test_the_advance_result_is_an_enum_in_the_tool_schema():
    """The board takes only `clean` or `failed`; the schema says so, so a typo is
    refused by the caller's harness rather than as a 422."""
    tool, = (t for t in srv.mcp._tool_manager.list_tools() if t.name == "gate_advance")
    assert tool.parameters["properties"]["result"]["enum"] == ["clean", "failed"]


def test_advancing_sends_the_result_and_the_head_it_ran_on(sent):
    srv.gate_advance(None, branch="integration/v22", head=HEAD, result="clean",
                     stage="sim run 1")
    assert sent.calls == [("/advance", {"repo": "acme/here", "branch": "integration/v22",
                                        "head": HEAD, "result": "clean",
                                        "stage": "sim run 1"})]


def test_reading_without_a_branch_asks_for_the_whole_repo(sent):
    srv.gate(None)
    assert sent.calls == [("read", {"repo": "acme/here"})]


def test_clearing_names_the_branch(sent):
    srv.gate_clear(None, branch="integration/v22")
    assert sent.calls == [("/clear", {"repo": "acme/here", "branch": "integration/v22"})]


def test_all_four_tools_are_registered():
    names = {t.name for t in srv.mcp._tool_manager.list_tools()}
    assert {"gate", "gate_set", "gate_advance", "gate_clear"} <= names


def test_the_instructions_point_a_gate_runner_at_the_tools():
    assert "gate_advance(" in srv.mcp.instructions
