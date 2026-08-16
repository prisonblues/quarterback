"""The full-screen client, driven through Textual's own pilot.

Assertions are on app state rather than on pixels: what the client does with a
selection — which post it replies to, what it refuses, what it never fetches — is
the behaviour worth pinning, and it survives a restyle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("textual", reason="the full-screen client is an optional extra")

from mcp_server.board.config import BoardConfig
from mcp_server.board.tui import BoardApp, ResumeRequest, _ref


class TuiClient:
    """Every endpoint the four panes touch, answering instantly."""

    def __init__(self, **overrides) -> None:
        self.posted: list[dict] = []
        self.fetched_details: list[int] = []
        self.data = {
            "whoami": {"agent": "zeus/fern-nectar", "machine": "zeus"},
            "active": {"agents": [], "subagents": []},
            "sessions": [],
            "review_stats": {"by_model": [], "runs": 0, "prs": 0, "repos": 0, "window": {}},
            "board": [],
            "sync": {"stale": False, "advice": "quarterback: in sync"},
            **overrides,
        }

    def whoami(self):
        return self.data["whoami"]

    def health(self):
        return {"status": "ok"}

    def active(self, params):
        return self.data["active"]

    def sessions(self, limit=50):
        return self.data["sessions"]

    def review_stats(self, params):
        return self.data["review_stats"]

    def board(self, params):
        return self.data["board"]

    def sync(self, params):
        return self.data["sync"]

    def get_post(self, post_id):
        self.fetched_details.append(post_id)
        return {"id": post_id, "from": "zeus/a", "type": "note", "summary": "s", "detail": "body"}

    def post(self, body):
        self.posted.append(dict(body))
        return {"id": 4242}

    def stream(self, since=0, read_timeout=90.0):
        return iter(())


def cfg(tmp_path, token="tok"):
    return BoardConfig(
        base_url="https://board.example", token=token, agent="zeus",
        config_path=Path(tmp_path) / "config",
    )


def make_app(tmp_path, client=None, **kw):
    return BoardApp(client or TuiClient(), cfg(tmp_path, **kw), repo_path=str(tmp_path))


def post(pid, ptype="status", **kw):
    base = {"id": pid, "ts": "2026-08-16T20:35:12+00:00", "from": "zeus/heron-sandy",
            "type": ptype, "summary": f"post {pid}", "refs": []}
    return {**base, **kw}


# -- boot --------------------------------------------------------------


async def test_it_starts_and_has_the_four_views(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        for view in ("board", "fleet", "sessions", "panel"):
            assert app.query_one(f"#{view}")
        assert app.current_view() == "board"


async def test_number_keys_switch_views(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("2")
        assert app.current_view() == "fleet"
        await pilot.press("4")
        assert app.current_view() == "panel"


async def test_with_no_token_it_starts_and_reports_health_instead_of_erroring(tmp_path):
    """The acceptance criterion for a host that has never been given a credential."""
    app = make_app(tmp_path, token=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        status = app.status_text
        assert "up" in status and "no token" in status


# -- the board pane ----------------------------------------------------


async def test_presence_is_hidden_until_toggled(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#board")
        app.add_post(post(1, "presence"))
        app.add_post(post(2, "status"))
        assert table.row_count == 1  # ~93% of the board, hidden as GET /board decided
        app.show_presence = True
        app.add_post(post(3, "presence"))
        assert table.row_count == 2


async def test_the_presence_toggle_redraws_what_is_already_here(tmp_path):
    """It used to affect only later arrivals, and pointed at an `r` that redraws other panes."""
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#board")
        app.add_post(post(1, "presence"))
        app.add_post(post(2, "status"))
        assert table.row_count == 1
        await pilot.press("P")
        assert table.row_count == 2  # the heartbeat already received is now shown
        assert "shown" in app.alert_text
        await pilot.press("P")
        assert table.row_count == 1
        assert len(app.posts) == 2  # hiding a row does not discard the post


async def test_the_pane_and_the_post_cache_are_both_bounded(tmp_path, monkeypatch):
    """Trimming only the table would leave a day-old client holding every post."""
    monkeypatch.setattr("mcp_server.board.tui.MAX_ROWS", 5)
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        for pid in range(1, 20):
            app.add_post(post(pid))
        assert app.query_one("#board").row_count == 5
        assert len(app.posts) == 5
        assert min(app.posts) == 15  # the oldest went, not the newest


async def test_a_post_arriving_twice_is_only_rendered_once(tmp_path):
    """The stream replays its backlog on reconnect; that must not double the pane."""
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.add_post(post(7))
        app.add_post(post(7))
        assert app.query_one("#board").row_count == 1


async def test_arriving_posts_never_fetch_their_detail(tmp_path):
    """The pane auto-follows the stream; a fetch per arrival is the mistake to avoid."""
    client = TuiClient()
    app = make_app(tmp_path, client)
    async with app.run_test() as pilot:
        await pilot.pause()
        for pid in (1, 2, 3):
            app.add_post(post(pid, has_detail=True))
        await pilot.pause()
        assert client.fetched_details == []
        # ...and the summary tier, which the client already has, is shown anyway.
        assert "post 3" in app.detail_text


async def test_opening_a_post_fetches_exactly_that_ones_detail(tmp_path):
    client = TuiClient()
    app = make_app(tmp_path, client)
    async with app.run_test() as pilot:
        await pilot.pause()
        for pid in (1, 2, 3):
            app.add_post(post(pid, has_detail=True))
        await pilot.pause()
        app.query_one("#board").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert client.fetched_details == [3]
        assert "body" in app.detail_text


# -- replying ----------------------------------------------------------


async def test_ack_prefills_both_halves_of_the_addressing(tmp_path):
    """A reply that names the thread but not the reader lands in nobody's inbox."""
    client = TuiClient()
    app = make_app(tmp_path, client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.add_post(post(3341, "ask", to="zeus/fern-nectar"))
        await pilot.pause()
        await pilot.press("a")
        await pilot.press(*"on it")
        await pilot.press("enter")
        await pilot.pause()
        assert client.posted == [
            {"type": "ack", "summary": "on it", "re": 3341, "to": "zeus/heron-sandy"}
        ]


async def test_an_empty_reply_posts_nothing(tmp_path):
    client = TuiClient()
    app = make_app(tmp_path, client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.add_post(post(1, "ask"))
        await pilot.pause()
        await pilot.press("a")
        await pilot.press("enter")
        await pilot.pause()
        assert client.posted == []


async def test_a_status_claim_needs_no_selection(tmp_path):
    client = TuiClient()
    app = make_app(tmp_path, client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.press(*"taking 110")
        await pilot.press("enter")
        await pilot.pause()
        assert client.posted == [{"type": "status", "summary": "taking 110"}]


async def test_replying_with_nothing_selected_says_so(tmp_path):
    client = TuiClient()
    app = make_app(tmp_path, client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert "select a post" in app.alert_text
        assert client.posted == []


# -- the local actions -------------------------------------------------


async def test_pull_only_acts_on_a_published_post(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.add_post(post(1, "note"))
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        assert "`published` post" in app.alert_text


async def test_cherry_pick_needs_a_landed_post_that_names_a_commit(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.add_post(post(1, "landed", refs=[{"kind": "pr", "value": "9"}]))
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        assert "names no commit" in app.alert_text


async def test_a_short_commit_ref_is_refused_here_rather_than_by_the_board(tmp_path):
    """`has_commit` 422s under seven characters, and that reads as "board unreachable"."""
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.add_post(post(1, "landed", refs=[{"kind": "commit", "value": "abc12"},
                                             {"kind": "repo", "value": "a/b"}]))
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        assert "too short" in app.alert_text


async def test_pull_needs_a_repo_ref_to_know_which_checkout(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.add_post(post(1, "published", refs=[{"kind": "commit", "value": "abc1234def"}]))
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        assert "names no repo" in app.alert_text


async def test_escape_abandons_a_prompt(tmp_path):
    """Otherwise an opened prompt is a bar you can only leave by submitting."""
    client = TuiClient()
    app = make_app(tmp_path, client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.press(*"half a thought")
        await pilot.press("escape")
        await pilot.pause()
        assert client.posted == []
        assert app._pending is None


# -- resume ------------------------------------------------------------


async def test_choosing_a_session_exits_carrying_the_request(tmp_path):
    """Handing this terminal to `claude --resume` is what a TUI cannot do in-place."""
    client = TuiClient(sessions=[{"session": "abc-123", "title": "t", "live": False,
                                  "resumable": True, "updated_at": "2026-08-16T20:00:00+00:00"}])
    app = make_app(tmp_path, client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        app.query_one("#sessions").focus()
        await pilot.press("enter")
        await pilot.pause()
    assert isinstance(app.return_value, ResumeRequest)
    assert app.return_value.session == "abc-123"


# -- helpers -----------------------------------------------------------


def test_ref_lookup_picks_the_named_kind():
    p = {"refs": [{"kind": "repo", "value": "a/b"}, {"kind": "commit", "value": "abc123"}]}
    assert _ref(p, "commit") == "abc123"
    assert _ref(p, "branch") is None
    assert _ref({}, "commit") is None
