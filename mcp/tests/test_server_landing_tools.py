"""The landing tools' own decisions, before anything reaches the board (#294).

The MCP layer cannot import the board's models, so the rules it shares with
`app/api/landing.py` are rules stated twice and drift between the two statements
is the shape of #172 itself. What a landing tool can only get wrong locally is
**which repository each end of an edge is in**, and that is the whole reason this
primitive exists: the motivating case is `nix-fleet#40` waiting on
`quarterback#290`, so a tool that copied one end's repository onto the other's
would get the interesting case wrong by default and right only when somebody
remembered to override it.

Pinned here:

  * a repository given as `owner/name` is taken as given, and one given as
    anything else is read off that checkout's git remote — the #148 rule;
  * neither end is ever inferred from the other;
  * a node is an issue or a pull request, and nothing else.

Skipped without the `server` extra, for the reason `test_server_claim_tools.py`
gives.

Run: uv run --extra dev --extra server pytest mcp/tests/test_server_landing_tools.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the MCP SDK is only in the `server` extra")

from mcp.server.fastmcp.exceptions import ToolError
from mcp_server import server as srv


class Recorder:
    """Enough of QuarterbackClient to see what a tool decided to send."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def landing_write(self, path: str, body: dict) -> dict:
        self.calls.append((path, body))
        return {"ok": True}

    def landing(self, params: dict) -> dict:
        self.calls.append(("read", params))
        return {"nodes": []}


@pytest.fixture()
def sent(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(srv, "_get_client", lambda ctx: rec)
    # A path is a checkout, and this is the only thing that reads one.
    monkeypatch.setattr(srv, "_derive_repo", lambda path: f"acme/{path.strip('./') or 'here'}")
    return rec


def test_an_edge_defaults_both_ends_to_the_caller_s_own_checkout(sent):
    srv.landing_gate(None, blocked=188, blocker=293, repo_path="./board")
    _, body = sent.calls[0]
    assert body["blocked"] == {"kind": "pr", "value": "188", "repo": "acme/board"}
    assert body["blocker"] == {"kind": "pr", "value": "293", "repo": "acme/board"}


def test_naming_one_end_s_repo_never_moves_the_other_end(sent):
    """`nix-fleet#40` waits on `quarterback#290`. Overriding the blocker must not
    drag the blocked node's repository along with it, and vice versa."""
    srv.landing_gate(None, blocked=40, blocked_kind="issue", blocker=290,
                     repo_path="./fleet", blocker_repo="prisonblues/quarterback")
    _, body = sent.calls[0]
    assert body["blocked"]["repo"] == "acme/fleet"
    assert body["blocker"]["repo"] == "prisonblues/quarterback"


def test_a_repo_that_is_not_a_slug_is_read_off_the_checkout_at_that_path(sent):
    """The #148 rule, and the reason no tool here takes a repo name it did not
    derive: a value read from git is the same value from every seat."""
    srv.landing_gate(None, blocked=2, blocker=1, repo_path="./a", blocker_repo="../other")
    _, body = sent.calls[0]
    assert body["blocker"]["repo"] == "acme/other"


def test_a_slug_is_folded_rather_than_sent_as_typed(sent):
    srv.landing_gate(None, blocked=2, blocker=1, repo_path="./a",
                     blocker_repo="PrisonBlues/Quarterback")
    assert sent.calls[0][1]["blocker"]["repo"] == "prisonblues/quarterback"


def test_a_node_is_an_issue_or_a_pull_request_and_nothing_else(sent):
    with pytest.raises(ToolError) as e:
        srv.landing_gate(None, blocked=1, blocker=2, blocked_kind="branch")
    assert "issue" in str(e.value) and "pr" in str(e.value)


def test_clearing_by_blocker_alone_sends_no_blocked_node(sent):
    """One landing frees every downstream node at once, which is the shape the
    fact arrives in."""
    srv.landing_clear(None, blocker=293, repo_path="./board")
    path, body = sent.calls[0]
    assert path == "clear" and "blocked" not in body
    assert body["resolution"] == "landed"


def test_clearing_one_pair_names_both_ends(sent):
    srv.landing_clear(None, blocker=293, blocked=188, blocked_kind="issue",
                      resolution="dropped", repo_path="./board")
    _, body = sent.calls[0]
    assert body["blocked"]["kind"] == "issue" and body["resolution"] == "dropped"


def test_minding_sends_no_ttl_unless_asked_because_presence_is_the_expiry(sent):
    """A tool that restated the server's default would disagree with it the
    moment it changed — the rule `plan_claim` is already held to."""
    srv.landing_mind(None, node=293, repo_path="./board", note="landing #188")
    path, body = sent.calls[0]
    assert path == "mind" and "ttl" not in body
    assert body["note"] == "landing #188"


def test_the_fleet_wide_graph_is_asked_for_by_star_and_scopes_to_nothing(sent):
    """A cross-repo graph has to be readable across every repo at once, and the
    board spells "no scope" as an absent parameter."""
    srv.landing_graph(None, repo="*")
    _, params = sent.calls[0]
    assert params["repo"] is None


def test_the_graph_defaults_to_the_repo_you_are_standing_in(sent):
    srv.landing_graph(None, repo_path="./board")
    assert sent.calls[0][1]["repo"] == "acme/board"


def test_the_mind_tool_does_not_restate_the_servers_ttl():
    """The `plan_claim` contract, applied here: the number lives in one place on
    the server, and a client that hard-codes it disagrees the day it moves."""
    src = Path(srv.__file__).read_text(encoding="utf-8")
    tool = src[src.index("def landing_mind("):src.index("def landing_unmind(")]
    assert "ttl: int | None = None" in tool
    assert "604800" not in tool and "ttl: int = " not in tool


def test_the_tools_say_that_minding_is_not_claiming():
    """The one thing an agent can most easily get wrong about this primitive.
    Claiming work you cannot start blocks it for everybody while nothing
    happens, so the tool has to say so where the agent reads it."""
    assert "not claiming" in (srv.landing_mind.__doc__ or "").lower()
