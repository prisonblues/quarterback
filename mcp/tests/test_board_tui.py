"""The full-screen client, driven through Textual's own pilot.

Assertions are on app state rather than on pixels: what the client does with a
selection — which post it replies to, what it refuses, what it never fetches — is
the behaviour worth pinning, and it survives a restyle.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import httpx
import pytest

pytest.importorskip("textual", reason="the full-screen client is an optional extra")

from mcp_server.board import local as local_mod
from mcp_server.board.config import BoardConfig
from mcp_server.board.tui import BoardApp, ResumeRequest, _ref
from rich.text import Text
from textual.coordinate import Coordinate


class TuiClient:
    """Every endpoint the four panes touch, answering instantly."""

    def __init__(self, **overrides) -> None:
        self.posted: list[dict] = []
        self.fetched_details: list[int] = []
        self.board_queries: list[dict] = []
        self.worktree_queries: list[dict] = []
        self.stats_calls = 0
        self.data = {
            "whoami": {"agent": "zeus/fern-nectar", "machine": "zeus"},
            "active": {"agents": [], "subagents": []},
            "sessions": [],
            "review_stats": {"by_model": [], "runs": 0, "prs": 0, "repos": 0, "window": {}},
            "board": [],
            "worktrees": [],
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
        self.stats_calls += 1
        return self.data["review_stats"]

    def board(self, params):
        self.board_queries.append(dict(params))
        # `ack`/`nak` overrides let a test answer the two reply reads separately
        # from the mailbox read, which is how the client actually queries.
        kind = params.get("type")
        return self.data.get(kind, self.data["board"]) if kind else self.data["board"]

    def get_worktrees(self, params):
        self.worktree_queries.append(dict(params))
        registry = self.data["worktrees"]
        return registry(params) if callable(registry) else registry

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


def make_app(tmp_path, client=None, repo_path=None, **kw):
    return BoardApp(
        client or TuiClient(), cfg(tmp_path, **kw), repo_path=repo_path or str(tmp_path)
    )


def post(pid, ptype="status", **kw):
    base = {"id": pid, "ts": "2026-08-16T20:35:12+00:00", "from": "zeus/heron-sandy",
            "type": ptype, "summary": f"post {pid}", "refs": []}
    return {**base, **kw}


def published(pid, repo="a/b"):
    return post(pid, "published", refs=[{"kind": "repo", "value": repo}])


def landed(pid, sha="abc1234def56", repo="a/b"):
    return post(pid, "landed",
                refs=[{"kind": "commit", "value": sha}, {"kind": "repo", "value": repo}])


def record_notes(monkeypatch) -> list[str]:
    """Every alert the app raised, in order.

    The status line only keeps the last one, and the 20-second pane refresh
    overwrites it — so a test that wants to know what the app *said* about a
    background worker has to collect them as they happen.
    """
    notes: list[str] = []
    original = BoardApp.note

    def spy(self, text: str) -> None:
        notes.append(text)
        original(self, text)

    monkeypatch.setattr(BoardApp, "note", spy)
    return notes


async def until(pilot, predicate, timeout: float = 5.0) -> bool:
    """Pump the UI until ``predicate`` holds — thread workers report back late."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await pilot.pause()
        await asyncio.sleep(0.01)
    return predicate()


def plain(markup: str) -> str:
    """What the terminal actually shows for a markup-bearing string."""
    return Text.from_markup(markup).plain


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


async def test_a_failed_detail_fetch_says_so_rather_than_leaving_the_prompt_up(
    tmp_path, monkeypatch
):
    """Silence read as "enter did nothing" while the pane still offered the fetch."""
    notes = record_notes(monkeypatch)

    class Broken(TuiClient):
        def get_post(self, post_id):
            raise httpx.ConnectError("no route to board")

    app = make_app(tmp_path, Broken())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.add_post(post(9, has_detail=True))
        await pilot.pause()
        app.query_one("#board").focus()
        await pilot.press("enter")
        assert await until(pilot, lambda: any("could not load #9" in n for n in notes))


async def test_hidden_heartbeats_cannot_outgrow_the_cache_or_orphan_a_row(
    tmp_path, monkeypatch
):
    """Presence used to skip the trim, so posts grew until it deleted a drawn row's data."""
    monkeypatch.setattr("mcp_server.board.tui.MAX_ROWS", 3)
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.add_post(post(1, "status"))
        for pid in range(2, 12):
            app.add_post(post(pid, "presence"))
        app.add_post(post(12, "status"))
        table = app.query_one("#board")
        assert len(app.posts) == 3
        # The table and the dict cannot disagree: every drawn row still has the
        # post behind it, so selection and the presence toggle still work.
        assert all(int(str(key.value)) in app.posts for key in table.rows)
        assert app.selected_post() is not None


async def test_a_post_whose_detail_was_offloaded_still_advertises_it(tmp_path):
    """Enter reveals where the body went, so the row has to say there is one."""
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.add_post(post(1, "finding", detail_ref="blob:sha256:abc"))
        await pilot.pause()
        assert "+detail" in app.query_one("#board").get_cell_at(Coordinate(0, 4))
        app.query_one("#board").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert "blob:sha256:abc" in app.detail_text


async def test_a_summary_full_of_markup_is_shown_as_written(tmp_path):
    """Board content is other people's text; a stray `[` must not restyle the pane."""
    hostile = "[bold red]not a style[/] [x"
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.add_post(post(1, "note", summary=hostile))
        await pilot.pause()
        cell = app.query_one("#board").get_cell_at(Coordinate(0, 4))
        assert plain(cell) == hostile
        assert hostile in plain(app.detail_text)


async def test_a_hostile_agent_name_cannot_restyle_the_alert_line(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.me = "zeus"
        app._panes_loaded(
            {
                "active": {"agents": [], "subagents": []},
                "sessions": [],
                "inbox": [post(5, "ask", **{"from": "[blink]zeus/x", "summary": "[red]help"})],
                "replies": [],
                "truncated": False,
                "sync": {"stale": True, "advice": "behind by [2] commits"},
            }
        )
        shown = plain(app.alert_text)
        assert "[blink]zeus/x" in shown and "[red]help" in shown
        assert "behind by [2] commits" in shown


async def test_a_full_mailbox_page_makes_the_ask_count_a_floor(tmp_path, monkeypatch):
    """A count the client cannot back is how an alert stops being read."""
    monkeypatch.setattr("mcp_server.board.tui.INBOX_LIMIT", 3)
    monkeypatch.setattr("mcp_server.board.tui.REPLY_LIMIT", 3)
    asks = [post(pid, "ask", to="zeus") for pid in (1, 2, 3)]
    client = TuiClient(board=asks, ack=[], nak=[])
    app = make_app(tmp_path, client)
    async with app.run_test() as pilot:
        assert await until(pilot, lambda: "ask(s) for you" in app.alert_text)
        assert "≥3 ask(s) for you" in app.alert_text


async def test_the_mailbox_and_its_replies_are_read_over_the_same_window(tmp_path):
    """One `type` per `/board` call, so ack and nak are two reads, not one."""
    client = TuiClient()
    app = make_app(tmp_path, client)
    async with app.run_test() as pilot:
        assert await until(pilot, lambda: len(client.board_queries) >= 4)
        mail = next(q for q in client.board_queries if q.get("to") == "@me")
        replies = [q for q in client.board_queries if q.get("type") in ("ack", "nak")]
        assert {q["type"] for q in replies} == {"ack", "nak"}
        assert {q["window_min"] for q in replies} == {mail["window_min"]}
        assert mail["limit"] >= 500  # the server caps at 1000; 100 hid real replies


async def test_a_slow_refresh_cannot_overwrite_a_newer_one(tmp_path):
    """Two refreshes in flight finish in whatever order the board answers them."""
    gate, in_flight, lock = threading.Event(), threading.Event(), threading.Lock()
    calls = []

    class Slow(TuiClient):
        def active(self, params):
            with lock:
                calls.append(1)
                first = len(calls) == 1
            if first:
                in_flight.set()
                gate.wait(5)
                return {"agents": [{"holder": "stale", "device": "zeus"}], "subagents": []}
            return {"agents": [{"holder": "fresh", "device": "zeus"}], "subagents": []}

    app = make_app(tmp_path, Slow())
    async with app.run_test() as pilot:
        assert await until(pilot, in_flight.is_set)
        await pilot.press("r")  # the mount refresh is still stuck; this supersedes it
        table = app.query_one("#fleet")
        assert await until(pilot, lambda: table.row_count == 1)
        assert table.get_cell_at(Coordinate(0, 1)) == "fresh"
        gate.set()  # ...and now the older answer arrives, too late to be believed
        await until(pilot, lambda: False, timeout=0.5)
        assert table.get_cell_at(Coordinate(0, 1)) == "fresh"


async def test_the_cursor_reaches_disk_on_the_way_out_rather_than_on_every_post(
    tmp_path, monkeypatch
):
    """A write per heartbeat is disk IO on Textual's own thread; losing it is worse."""
    written: list[int] = []
    monkeypatch.setattr(
        "mcp_server.board.tui.write_cursor", lambda url, cursor: written.append(cursor)
    )
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        for pid in range(1, 30):
            app.add_post(post(pid))
        assert written == []
    assert written[-1] == 29


async def test_orienting_resumes_the_stream_from_before_the_page_not_after_it(tmp_path):
    """100 orient rows cannot stand in for the 400 posts that arrived since."""
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.cursor = 100
        app._oriented([post(pid) for pid in range(500, 520)])
        assert app._tail_cursor == 100  # not 519, which would skip 101..499
        assert app.cursor == 519  # ...and the pane's own position still advanced


async def test_a_first_run_orients_without_replaying_the_whole_board(tmp_path):
    """`/stream?since=0` replays every post the board has ever held."""
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.cursor = 0
        app._oriented([post(pid) for pid in range(500, 520)])
        assert app._tail_cursor == 499


async def test_the_stream_stops_on_a_4xx_instead_of_retrying_a_wrong_request(
    tmp_path, monkeypatch
):
    """A 422 from a schema mismatch is not something a retry can fix."""
    notes = record_notes(monkeypatch)
    slept: list[float] = []
    # Bounded, so a regression fails this test rather than spinning the suite in
    # the very retry loop it is here to rule out.
    monkeypatch.setattr(BoardApp, "_sleep", lambda self, s: slept.append(s) or len(slept) < 3)

    class Refusing(TuiClient):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def stream(self, since=0, read_timeout=90.0):
            self.attempts += 1
            response = httpx.Response(422, request=httpx.Request("GET", "https://b/stream"))
            raise httpx.HTTPStatusError("unprocessable", request=response.request,
                                        response=response)

    client = Refusing()
    app = make_app(tmp_path, client)
    async with app.run_test() as pilot:
        assert await until(pilot, lambda: any("422" in n for n in notes))
        await asyncio.sleep(0.2)
        assert client.attempts == 1
        assert slept == []


async def test_the_stream_retries_a_5xx(tmp_path, monkeypatch):
    """The other half of the rule: the board being unwell is not this client's mistake."""
    monkeypatch.setattr(BoardApp, "_sleep", lambda self, s: self.client.attempts < 2)

    class Unwell(TuiClient):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def stream(self, since=0, read_timeout=90.0):
            self.attempts += 1
            response = httpx.Response(503, request=httpx.Request("GET", "https://b/stream"))
            raise httpx.HTTPStatusError("down", request=response.request, response=response)

    client = Unwell()
    app = make_app(tmp_path, client)
    async with app.run_test() as pilot:
        assert await until(pilot, lambda: client.attempts >= 2)


async def test_a_reconnect_resumes_from_the_last_post_received(tmp_path, monkeypatch):
    """Otherwise every drop replays the same batch, forever."""
    monkeypatch.setattr(BoardApp, "_sleep", lambda self, s: len(self.client.since) < 2)

    class Dropping(TuiClient):
        def __init__(self):
            super().__init__()
            self.since: list[int] = []

        def stream(self, since=0, read_timeout=90.0):
            self.since.append(since)
            if len(self.since) == 1:
                yield post(41)
                yield post(42)
            raise httpx.ReadTimeout("connection went away")

    client = Dropping()
    app = make_app(tmp_path, client)
    async with app.run_test() as pilot:
        assert await until(pilot, lambda: len(client.since) >= 2)
        assert client.since[1] == 42


# -- the panel ---------------------------------------------------------


async def test_the_panel_says_how_much_of_the_window_reported_a_cost(tmp_path):
    """A sum over seven of twelve runs is not a sum over the window."""
    stats = {
        "by_model": [{"reviewer": "codex", "model": "gpt-5", "effort": "high", "runs": 12,
                      "confirmed": 3, "precision": 0.5, "confirmed_per_run": 0.25,
                      "cost_usd": 1.2345, "cost_runs": 7,
                      "total_tokens": 900, "token_runs": 12}],
        "runs": 12, "prs": 2, "repos": 1, "window": {},
    }
    app = make_app(tmp_path, TuiClient(review_stats=stats))
    async with app.run_test() as pilot:
        table = app.query_one("#panel")
        assert await until(pilot, lambda: table.row_count == 1)
        assert table.get_cell_at(Coordinate(0, 8)) == "$1.2345 (7/12 runs)"
        assert table.get_cell_at(Coordinate(0, 7)) == "900"  # fully covered, so no marker


async def test_the_panel_aggregate_is_asked_for_on_demand_not_on_every_poll(tmp_path):
    """A 30-day aggregate every 20 seconds is the expensive read on this screen."""
    client = TuiClient()
    app = make_app(tmp_path, client)
    async with app.run_test() as pilot:
        assert await until(pilot, lambda: client.stats_calls == 1)
        app.refresh_panes()
        await until(pilot, lambda: False, timeout=0.3)
        assert client.stats_calls == 1  # the pane poll no longer carries it
        await pilot.press("4")
        assert await until(pilot, lambda: client.stats_calls == 2)


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


async def test_a_reply_goes_to_the_post_it_was_opened_on(tmp_path):
    """The pane follows the stream, so the cursor moves under a reply being typed."""
    client = TuiClient()
    app = make_app(tmp_path, client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.add_post(post(3341, "ask", to="zeus/fern-nectar"))
        await pilot.pause()
        await pilot.press("a")
        # ...and a post lands mid-sentence, taking the cursor with it.
        app.add_post(post(3342, "status", **{"from": "zeus/interloper"}))
        await pilot.press(*"on it")
        await pilot.press("enter")
        await pilot.pause()
        assert client.posted == [
            {"type": "ack", "summary": "on it", "re": 3341, "to": "zeus/heron-sandy"}
        ]


async def test_ack_refuses_a_row_that_is_not_an_ask(tmp_path):
    """`a`/`n` thread onto an ask; an ack on a status is a post nothing reads."""
    client = TuiClient()
    app = make_app(tmp_path, client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.add_post(post(1, "finding"))
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert "answers an `ask`" in app.alert_text
        assert app._pending is None  # the prompt never opened
        assert client.posted == []


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


def worktree(path, device="zeus") -> dict:
    return {"device": device, "path": str(path), "repo": "a/b"}


async def test_pull_runs_in_the_registered_checkout_and_reports_the_outcome(
    tmp_path, monkeypatch
):
    """The whole point of a local client: `p` puts a git pull in a real directory."""
    notes = record_notes(monkeypatch)
    here = tmp_path / "checkout"
    here.mkdir()
    pulled: list[str] = []
    monkeypatch.setattr(
        local_mod, "pull",
        lambda path: pulled.append(path) or local_mod.Outcome(True, "Fast-forwarded to abc123"),
    )
    client = TuiClient(worktrees=[worktree(here)])
    app = make_app(tmp_path, client, repo_path=str(here))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.add_post(published(1))
        await pilot.pause()
        await pilot.press("p")
        assert await until(pilot, lambda: pulled == [str(here)])
        assert await until(pilot, lambda: any("Fast-forwarded" in n for n in notes))
        assert any(str(here) in n and n.startswith("✓") for n in notes)


async def test_a_cherry_pick_targets_the_checkout_that_does_not_have_the_sha(
    tmp_path, monkeypatch
):
    """find_commit's job: the SHA exists here already, so pick the tree that can take it."""
    holder, target = tmp_path / "holder", tmp_path / "target"
    holder.mkdir()
    target.mkdir()
    picked: list[tuple[str, str]] = []
    monkeypatch.setattr(
        local_mod, "cherry_pick",
        lambda path, sha: picked.append((path, sha)) or local_mod.Outcome(True, "picked"),
    )

    def registry(params):
        # The board answers "who has this commit" and "what is here" differently.
        if params.get("has_commit"):
            return [worktree(holder)]
        return [worktree(holder), worktree(target)]

    app = make_app(tmp_path, TuiClient(worktrees=registry), repo_path=str(target))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.add_post(landed(1))
        await pilot.pause()
        await pilot.press("c")
        assert await until(pilot, lambda: picked == [(str(target), "abc1234def56")])


async def test_a_pick_with_no_checkout_of_the_repo_here_says_exactly_that(
    tmp_path, monkeypatch
):
    """It used to report the SHA as "already in nowhere on this machine"."""
    notes = record_notes(monkeypatch)
    app = make_app(tmp_path, TuiClient(worktrees=[]))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.add_post(landed(1))
        await pilot.pause()
        await pilot.press("c")
        assert await until(pilot, lambda: any("no registered checkout of a/b" in n for n in notes))
        assert not any("nowhere on this machine" in n for n in notes)


async def test_a_pick_whose_only_checkout_already_has_the_sha_names_it(tmp_path, monkeypatch):
    notes = record_notes(monkeypatch)
    here = tmp_path / "checkout"
    here.mkdir()
    app = make_app(tmp_path, TuiClient(worktrees=[worktree(here)]))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.add_post(landed(1))
        await pilot.pause()
        await pilot.press("c")
        assert await until(pilot, lambda: any("is already in" in n for n in notes))
        assert any(str(here) in n for n in notes if "is already in" in n)


async def test_a_board_failure_while_locating_the_checkout_is_reported(tmp_path, monkeypatch):
    notes = record_notes(monkeypatch)

    class Broken(TuiClient):
        def get_worktrees(self, params):
            raise httpx.ConnectError("no route to board")

    app = make_app(tmp_path, Broken())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.add_post(published(1))
        await pilot.pause()
        await pilot.press("p")
        assert await until(
            pilot, lambda: any("could not ask the board where to act" in n for n in notes)
        )


async def test_a_pick_reports_a_board_failure_on_the_second_lookup_too(tmp_path, monkeypatch):
    """A pick asks twice — who holds the SHA, then what is here — and either can fail."""
    notes = record_notes(monkeypatch)
    here = tmp_path / "checkout"
    here.mkdir()

    def registry(params):
        if params.get("has_commit"):
            return [worktree(here)]
        raise httpx.ConnectError("no route to board")

    app = make_app(tmp_path, TuiClient(worktrees=registry))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.add_post(landed(1))
        await pilot.pause()
        await pilot.press("c")
        assert await until(
            pilot, lambda: any("could not list this machine's worktrees" in n for n in notes)
        )


async def test_the_checkout_is_chosen_stably_and_the_choice_is_declared(tmp_path, monkeypatch):
    """`--repo` picking the status line while report_git's order picks the directory."""
    notes = record_notes(monkeypatch)
    first, mine = tmp_path / "aaa", tmp_path / "zzz"
    first.mkdir()
    mine.mkdir()
    pulled: list[str] = []
    monkeypatch.setattr(
        local_mod, "pull",
        lambda path: pulled.append(path) or local_mod.Outcome(True, "up to date"),
    )
    client = TuiClient(worktrees=[worktree(first), worktree(mine)])
    app = make_app(tmp_path, client, repo_path=str(mine))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.add_post(published(1))
        await pilot.pause()
        await pilot.press("p")
        assert await until(pilot, lambda: pulled == [str(mine)])  # not candidates[0]
        assert any("chosen from 2 matching checkouts" in n for n in notes)


async def test_a_second_local_action_is_refused_while_one_is_running(tmp_path, monkeypatch):
    """Two gits in one checkout is what local.py's refusals exist to prevent."""
    notes = record_notes(monkeypatch)
    here = tmp_path / "checkout"
    here.mkdir()
    gate = threading.Event()
    started = threading.Event()

    def slow_pull(path):
        started.set()
        gate.wait(5)
        return local_mod.Outcome(True, "done")

    monkeypatch.setattr(local_mod, "pull", slow_pull)
    app = make_app(tmp_path, TuiClient(worktrees=[worktree(here)]), repo_path=str(here))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.add_post(published(1))
        await pilot.pause()
        await pilot.press("p")
        assert await until(pilot, started.is_set)
        await pilot.press("p")
        await pilot.pause()
        assert "already running" in app.alert_text
        gate.set()
        assert await until(pilot, lambda: any("done" in n for n in notes))
        # ...and the refusal lifts once it has reported.
        assert await until(pilot, lambda: app._local_busy is False)


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
