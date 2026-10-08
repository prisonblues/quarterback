"""A post that waits on an answer tells its author to watch for it (#825).

Skipped without the `server` extra, for the reason `test_server_claim_tools.py` gives.

Run: uv run --extra dev --extra server pytest mcp/tests/test_server_board_post_hint.py
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="the MCP SDK is only in the `server` extra")

from mcp_server import server as srv


class Poster:
    def post(self, body: dict) -> dict:
        return {"id": 42}


@pytest.fixture()
def posted(monkeypatch):
    monkeypatch.setattr(srv, "_get_client", lambda ctx: Poster())


@pytest.mark.parametrize("kind", ["ask", "stuck"])
def test_a_post_that_awaits_a_reply_names_the_watcher_and_its_own_id(posted, kind):
    got = srv.board_post(None, summary="which way?", type=kind)
    assert got["id"] == 42
    assert "qb-watch-board --re 42" in got["next"]


@pytest.mark.parametrize("kind", ["status", "done", "ack"])
def test_other_posts_return_just_the_id(posted, kind):
    assert srv.board_post(None, summary="x", type=kind) == {"id": 42}


def test_the_instructions_tell_an_agent_to_act_on_a_post_it_is_pointed_at():
    assert "do not just ack" in srv.mcp.instructions
