"""``qb board`` — the full-screen client.

Four views over endpoints that already exist, and a status line carrying the two
ambient facts a working agent needs without asking: is this checkout stale, and
is anyone waiting on an answer from me.

The views are the easy half. What justifies the client existing at all is the
last three actions — pull, cherry-pick, resume — because those need a process on
*this* machine and so are the one thing the browser board can never grow. See
``local.py`` for the refusals they inherit.
"""

from __future__ import annotations

from typing import ClassVar

import httpx
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import ContentSwitcher, DataTable, Footer, Input, Static

from ..client import QuarterbackClient
from ..gitctx import recent_shas, repo_slug
from . import local
from .config import BoardConfig
from .render import type_colour
from .state import read_cursor, write_cursor
from .views import (
    fleet_rows,
    panel_rows,
    panel_window,
    session_rows,
    staleness,
    unanswered_asks,
)

#: How many posts the Board pane keeps. The stream is unbounded and a client left
#: open for a day would otherwise grow without limit.
MAX_ROWS = 2000

#: Seconds between refreshes of the panes that have no live stream behind them.
POLL_SECONDS = 20.0

#: The orient read the Board pane opens with — `GET /board`'s own defaults, so
#: this client sees what an arriving agent sees, floor and all.
ORIENT_MINUTES = 30
ORIENT_LIMIT = 100

#: How far back "asks for you" looks. Wider than the orient window, because an
#: unanswered ask from earlier this shift is still unanswered — and deliberately
#: not a day, because an inbox that reaches back to yesterday is how every fresh
#: session ends up rediscovering the same handful of long-dead asks (issue #17).
INBOX_MINUTES = 240


class ResumeRequest:
    """What the app returns when the user picks a session to resume.

    Carried out *after* the app exits rather than from inside it: resuming means
    `claude --resume` taking over this terminal, and a full-screen app is the one
    thing that cannot hand its terminal to a child and get it back.
    """

    def __init__(self, session: str) -> None:
        self.session = session


class BoardApp(App):
    """The board, the fleet, the sessions and the panel — over ssh, on any host."""

    CSS = """
    Screen { layers: base; }
    #status { height: 1; background: $panel; color: $text-muted; padding: 0 1; }
    #alerts { height: 1; padding: 0 1; }
    #tabs { height: 1; background: $panel; padding: 0 1; }
    DataTable { height: 1fr; }
    #detail { height: 8; border-top: solid $primary; padding: 0 1; overflow-y: auto; }
    #prompt { display: none; height: 3; }
    #prompt.open { display: block; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("1", "view('board')", "Board"),
        Binding("2", "view('fleet')", "Fleet"),
        Binding("3", "view('sessions')", "Sessions"),
        Binding("4", "view('panel')", "Panel"),
        Binding("a", "reply('ack')", "ack"),
        Binding("n", "reply('nak')", "nak"),
        Binding("s", "claim", "status"),
        Binding("p", "pull", "pull"),
        Binding("c", "pick", "cherry-pick"),
        Binding("P", "toggle_presence", "presence"),
        Binding("r", "refresh", "refresh"),
        Binding("q", "quit", "quit"),
        # Escape has to reach the app while the Input holds focus, or an opened
        # prompt is a bar you can only leave by submitting something.
        Binding("escape", "cancel_prompt", "cancel", priority=True),
    ]

    def __init__(self, client: QuarterbackClient, cfg: BoardConfig, repo_path: str = ".") -> None:
        super().__init__()
        self.client = client
        self.cfg = cfg
        self.repo_path = repo_path
        self.me: str | None = None
        self.device: str = cfg.agent
        self.repo: str | None = repo_slug(repo_path)
        self.show_presence = False
        self.posts: dict[int, dict] = {}
        self.inbox: list[dict] = []
        self.replies: list[dict] = []
        self.cursor = read_cursor(cfg.base_url)
        self._pending: str | None = None  # which action the prompt is collecting for
        # Mirrors of the three one-line panels, so state can be read without
        # reaching into a widget that may not exist yet (or any more).
        self.status_text = ""
        self.alert_text = ""
        self.detail_text = ""
        self._panel_window = ""

    # -- layout ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(self._tab_line("board"), id="tabs")
        with ContentSwitcher(initial="board", id="switcher"):
            yield DataTable(id="board", cursor_type="row", zebra_stripes=True)
            yield DataTable(id="fleet", cursor_type="row", zebra_stripes=True)
            yield DataTable(id="sessions", cursor_type="row", zebra_stripes=True)
            yield DataTable(id="panel", cursor_type="row", zebra_stripes=True)
        yield Static("", id="detail")
        with Vertical(id="prompt"):
            yield Input(placeholder="", id="promptinput")
        with Horizontal():
            yield Static("", id="status")
        yield Static("", id="alerts")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#board", DataTable).add_columns("time", "id", "from", "type", "summary")
        self.query_one("#fleet", DataTable).add_columns(
            "kind", "holder", "device", "repo", "branch", "ttl", "age", "title"
        )
        self.query_one("#sessions", DataTable).add_columns(
            "", "title", "holder", "device", "age", "size", "session"
        )
        self.query_one("#panel", DataTable).add_columns(
            "reviewer", "model", "effort", "runs", "confirmed", "precision",
            "conf/run", "tokens", "cost",
        )
        self.set_status("connecting…")
        if not self.cfg.authenticated:
            # A host with no token still has to be able to answer "is the board
            # up". /health is the one endpoint with no auth dependency, which is
            # precisely so this case reports rather than fails to start.
            self.check_health()
            # ...and keep checking. Without the interval this pane would report
            # "DOWN" for the rest of the session on one unlucky first request.
            self.set_interval(POLL_SECONDS, self.check_health)
            self.note("no token on this host — read-only health check only")
            return
        self.identify()
        # Orient first, then follow. The tail alone would open on an empty pane
        # (a saved cursor means "you have seen everything up to here", so there
        # is nothing to replay) or, on a first run, on the whole board. `/board`
        # with no cursor is the orient read the browser and every agent already
        # use, floor included — so a quiet board still shows who made the last
        # call rather than showing nothing.
        self.bootstrap()
        self.refresh_panes()
        self.set_interval(POLL_SECONDS, self.refresh_panes)

    # -- small helpers --------------------------------------------------

    def _tab_line(self, current: str) -> str:
        names = [("1", "board"), ("2", "fleet"), ("3", "sessions"), ("4", "panel")]
        return "  ".join(
            f"[reverse] {k} {n} [/reverse]" if n == current else f" {k} {n} "
            for k, n in names
        )

    def _write(self, selector: str, text: str) -> None:
        """Update a one-line panel, tolerating its absence.

        Background workers post their results back through these, and a worker
        that finishes while the app is tearing down would otherwise take the
        shutdown down with it — the widget is gone, and its message is late by
        definition. Nothing here is worth an exception.
        """
        panel = self.query(selector)
        if panel:
            panel.first(Static).update(text)

    def set_status(self, text: str) -> None:
        self.status_text = text
        self._write("#status", text)

    def note(self, text: str) -> None:
        self.alert_text = text
        self._write("#alerts", text)

    def detail(self, text: str) -> None:
        self.detail_text = text
        self._write("#detail", text)

    def current_view(self) -> str:
        return self.query_one("#switcher", ContentSwitcher).current or "board"

    def selected_post(self) -> dict | None:
        table = self.query_one("#board", DataTable)
        if self.current_view() != "board" or not table.row_count:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            # Broad on purpose: the cursor can point at a row that the stream's
            # MAX_ROWS trim removed a moment ago, and Textual spells that
            # several ways across versions. "Nothing is selected" is the right
            # answer to all of them, and none is worth crashing a keypress over.
            return None
        return self.posts.get(int(key.value)) if key.value is not None else None

    def selected_session(self) -> str | None:
        table = self.query_one("#sessions", DataTable)
        if self.current_view() != "sessions" or not table.row_count:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        return str(key.value) if key.value is not None else None

    # -- background work ------------------------------------------------

    @work(thread=True)
    def identify(self) -> None:
        try:
            who = self.client.whoami()
        except httpx.HTTPError as e:
            self.call_from_thread(self.note, f"whoami failed: {e}")
            return
        self.call_from_thread(self._identified, who)

    def _identified(self, who: dict) -> None:
        self.me = who.get("agent")
        self.device = who.get("machine") or self.cfg.agent
        self.set_status(f"{self.me} · {self.cfg.base_url}")

    @work(thread=True)
    def check_health(self) -> None:
        try:
            self.client.health()
        except httpx.HTTPError as e:
            self.call_from_thread(self.set_status, f"{self.cfg.base_url} · DOWN ({e})")
            return
        self.call_from_thread(self.set_status, f"{self.cfg.base_url} · up (no token here)")

    @work(thread=True, group="bootstrap")
    def bootstrap(self) -> None:
        try:
            recent = self.client.board({"limit": ORIENT_LIMIT, "window_min": ORIENT_MINUTES})
        except httpx.HTTPError as e:
            self.call_from_thread(self.note, f"could not read the board: {e}")
            recent = []
        self.call_from_thread(self._oriented, recent)

    def _oriented(self, recent: list[dict]) -> None:
        for post in recent:
            self.add_post(post)
        # Resume where this client stopped — but never from the very beginning.
        # A first run here has cursor 0, and streaming from 0 would replay every
        # post the board has ever held to render the last few hundred of them.
        oldest = min((int(p["id"]) for p in recent if p.get("id")), default=0)
        if not self.cursor:
            self.cursor = max(oldest - 1, 0)
        self.tail()

    @work(thread=True, exclusive=True, group="tail")
    def tail(self) -> None:
        """Follow the stream forever, reconnecting from the cursor on a drop."""
        while True:
            try:
                for post in self.client.stream(since=self.cursor):
                    self.call_from_thread(self.add_post, post)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403):
                    self.call_from_thread(self.note, "board rejected this machine's token")
                    return
                self.call_from_thread(self.note, f"stream: HTTP {e.response.status_code}")
            except httpx.HTTPError as e:
                self.call_from_thread(self.note, f"stream dropped ({type(e).__name__}); retrying")
            if not self._sleep(3.0):
                return

    def _sleep(self, seconds: float) -> bool:
        """Sleep unless the app is shutting down. Returns False when it is."""
        import time

        for _ in range(int(seconds * 10)):
            if not self.is_running:
                return False
            time.sleep(0.1)
        return self.is_running

    @work(thread=True, group="panes")
    def refresh_panes(self) -> None:
        try:
            active = self.client.active({})
            sessions = self.client.sessions(limit=60)
            stats = self.client.review_stats({"days": 30})
            inbox = self.client.board(
                {"to": "@me", "window_min": INBOX_MINUTES, "limit": 100}
            )
            # The replies, over the SAME window as the inbox. Reading them off
            # the streamed posts instead would compare a 4-hour mailbox against
            # a 30-minute view of the board, so an ask answered three hours ago
            # would still be counted — the alert would only ever grow.
            replies = [
                p
                for kind in ("ack", "nak")
                for p in self.client.board(
                    {"type": kind, "window_min": INBOX_MINUTES, "limit": 200}
                )
            ]
        except httpx.HTTPError as e:
            self.call_from_thread(self.note, f"refresh failed: {e}")
            return
        sync = None
        if self.repo:
            try:
                sync = self.client.sync(
                    {
                        "repo": self.repo,
                        "have": ",".join(recent_shas(self.repo_path, 15)) or None,
                        "device": self.device,
                    }
                )
            except httpx.HTTPError:
                sync = None
        self.call_from_thread(
            self._panes_loaded, active, sessions, stats, inbox, replies, sync
        )

    def _panes_loaded(self, active, sessions, stats, inbox, replies, sync) -> None:
        self.inbox = inbox
        self.replies = replies
        self._fill_fleet(active)
        self._fill_sessions(sessions)
        self._fill_panel(stats)
        self._update_alerts(sync)

    # -- pane fills -----------------------------------------------------

    def add_post(self, post: dict) -> None:
        pid = int(post.get("id") or 0)
        if pid <= 0 or pid in self.posts:
            return
        self.posts[pid] = post
        self.cursor = max(self.cursor, pid)
        write_cursor(self.cfg.base_url, self.cursor)
        if post.get("type") == "presence" and not self.show_presence:
            return
        table = self.query_one("#board", DataTable)
        self._add_board_row(table, pid, post)
        while table.row_count > MAX_ROWS:
            table.remove_row(next(iter(table.rows)))
        # The dict backs the table's rows (detail, refs, the reply target), so it
        # is trimmed with them. Trimming only the table would leave a client left
        # open for a day holding every post the board produced in it.
        while len(self.posts) > MAX_ROWS:
            del self.posts[min(self.posts)]
        if table.cursor_row >= table.row_count - 2:
            table.move_cursor(row=table.row_count - 1)

    def _add_board_row(self, table: DataTable, pid: int, post: dict) -> None:
        colour = type_colour(post.get("type", "note"))
        table.add_row(
            (post.get("ts") or "")[11:19],
            str(pid),
            str(post.get("from") or "?"),
            f"[{colour}]{post.get('type', 'note')}[/]",
            self._summary_cell(post),
            key=str(pid),
        )

    def _summary_cell(self, post: dict) -> str:
        text = " ".join(str(post.get("summary") or "").split())
        if post.get("to"):
            text = f"{text}  [dim]→{post['to']}[/dim]"
        if post.get("has_detail"):
            text = f"{text}  [dim]+detail[/dim]"
        return text

    def _fill_fleet(self, active: dict) -> None:
        """Who is live where — with an agent's fan-out visibly its fan-out.

        ``/active`` also takes ``mine=``/``peers_only=``, which tag or drop the
        caller's *own* sub-agents. Neither is passed, and that is not an
        oversight: this client is a person at a terminal, not a session, so it
        has no `mine` to send and every row is somebody else's by construction.
        The ``kind`` column does the job those flags do for an agent — a
        session's five Explore workers read as five sub-agents of the holder
        above them rather than as five peers.
        """
        table = self.query_one("#fleet", DataTable)
        table.clear()
        for row in fleet_rows(active):
            table.add_row(
                row["kind"], row["holder"], row["device"], row["repo"], row["branch"],
                row["ttl"], row["since"], row["title"][:60],
            )

    def _fill_sessions(self, sessions: list[dict]) -> None:
        table = self.query_one("#sessions", DataTable)
        table.clear()
        for row in session_rows(sessions):
            flag = "●" if row["live"] else ("↻" if row["resumable"] else "○")
            table.add_row(
                flag, row["title"][:40], row["holder"], row["device"],
                row["age"], row["size"], row["session"][:8],
                key=row["session"],
            )

    def _fill_panel(self, stats: dict) -> None:
        table = self.query_one("#panel", DataTable)
        table.clear()
        for row in panel_rows(stats):
            table.add_row(
                row["reviewer"], row["model"], row["effort"], str(row["runs"]),
                str(row["confirmed"]), row["precision"], row["per_run"],
                row["tokens"], row["cost"],
            )
        # Kept rather than written straight to the detail panel: the panel view
        # may not be the one on screen when the poll lands, and switching to it
        # later should not show an empty line until the next one.
        self._panel_window = panel_window(stats)
        if self.current_view() == "panel":
            self.detail(self._panel_window)

    def _update_alerts(self, sync: dict | None) -> None:
        parts = []
        pending = unanswered_asks(self.inbox, [*self.replies, *self.posts.values()], self.me)
        if pending:
            newest = pending[-1]
            parts.append(
                f"[b]📨 {len(pending)} ask(s) for you[/b] — "
                f"#{newest.get('id')} {newest.get('from')}: "
                f"{' '.join(str(newest.get('summary') or '').split())[:70]}"
            )
        if sync is not None:
            stale, advice = staleness(sync)
            parts.append(f"[b]⬇️ {advice}[/b]" if stale else f"[dim]{advice}[/dim]")
        elif self.repo is None:
            parts.append("[dim]not in a git checkout — no staleness to report[/dim]")
        self.note("   ".join(parts) if parts else "[dim]nothing waiting on you[/dim]")

    # -- actions --------------------------------------------------------

    def action_view(self, name: str) -> None:
        self.query_one("#switcher", ContentSwitcher).current = name
        self.query_one("#tabs", Static).update(self._tab_line(name))
        self.detail(self._panel_window if name == "panel" else "")

    def action_toggle_presence(self) -> None:
        """Show or hide heartbeats, and redraw what is already here.

        The toggle used to only affect posts arriving after it, and said so —
        but `r` refreshes the other three panes and not this one, so the advice
        it gave was wrong. Every post is already in `self.posts`, so the redraw
        costs a redraw and no request.
        """
        self.show_presence = not self.show_presence
        self._redraw_board()
        state = "shown" if self.show_presence else "hidden"
        self.note(f"presence heartbeats {state}")

    def _redraw_board(self) -> None:
        table = self.query_one("#board", DataTable)
        table.clear()
        for pid in sorted(self.posts):
            post = self.posts[pid]
            if post.get("type") == "presence" and not self.show_presence:
                continue
            self._add_board_row(table, pid, post)
        if table.row_count:
            table.move_cursor(row=table.row_count - 1)

    def action_refresh(self) -> None:
        self.refresh_panes()
        self.note("refreshing…")

    @on(DataTable.RowHighlighted, "#board")
    def _board_row(self, event: DataTable.RowHighlighted) -> None:
        """Moving the cursor shows the summary tier — which the client already has."""
        post = self.posts.get(int(event.row_key.value)) if event.row_key.value else None
        if post is not None:
            self.detail(self._summary_detail(post))

    @on(DataTable.RowSelected, "#board")
    def _board_open(self, event: DataTable.RowSelected) -> None:
        """Enter opens the detail tier — and only then is ``/post/{id}`` fetched.

        Deliberately bound to selection rather than to the cursor moving. The
        Board pane auto-follows the stream, so a highlight-triggered fetch would
        hit /post/{id} for every arriving post that happened to carry detail —
        which is precisely "fetching detail for rows nobody opened", the mistake
        the browser board already declined to make.
        """
        post = self.posts.get(int(event.row_key.value)) if event.row_key.value else None
        if post is None:
            return
        self.detail(self._summary_detail(post))
        if post.get("has_detail"):
            self.load_detail(int(post["id"]))
        elif post.get("detail_ref"):
            self.detail(f"{self._summary_detail(post)}\n\ndetail stored at {post['detail_ref']}")

    def _summary_detail(self, post: dict) -> str:
        header = (
            f"#{post['id']} [b]{post.get('from')}[/b] {post.get('type')} "
            f"{post.get('ts', '')[:19]}"
        )
        if post.get("has_detail"):
            header += "  [dim](enter to load detail)[/dim]"
        return f"{header}\n{post.get('summary', '')}"

    @work(thread=True, group="detail")
    def load_detail(self, post_id: int) -> None:
        try:
            full = self.client.get_post(post_id)
        except httpx.HTTPError:
            return
        self.call_from_thread(self._detail_loaded, full)

    def _detail_loaded(self, full: dict) -> None:
        selected = self.selected_post()
        if selected is None or int(selected.get("id", 0)) != int(full.get("id", 0)):
            return  # the cursor moved on while we were fetching
        header = f"#{full['id']} [b]{full.get('from')}[/b] {full.get('type')}"
        self.detail(f"{header}\n{full.get('summary', '')}\n\n{full.get('detail') or ''}")

    def action_reply(self, kind: str) -> None:
        post = self.selected_post()
        if post is None:
            self.note("select a post on the Board view first")
            return
        self._open_prompt(kind, f"{kind} to {post.get('from')} re #{post.get('id')}: ")

    def action_claim(self) -> None:
        self._open_prompt("status", "status — what are you picking up: ")

    def _open_prompt(self, kind: str, placeholder: str) -> None:
        self._pending = kind
        prompt = self.query_one("#prompt", Vertical)
        prompt.add_class("open")
        field = self.query_one("#promptinput", Input)
        field.placeholder = placeholder
        field.value = ""
        field.focus()

    def _close_prompt(self) -> None:
        self._pending = None
        self.query_one("#prompt", Vertical).remove_class("open")
        self.query_one("#promptinput", Input).value = ""
        self.query_one(f"#{self.current_view()}", DataTable).focus()

    @on(Input.Blurred, "#promptinput")
    def _prompt_blurred(self, _event: Input.Blurred) -> None:
        """Losing focus abandons the draft rather than leaving a dead bar open."""
        if self._pending is not None:
            self._close_prompt()

    def action_cancel_prompt(self) -> None:
        if self._pending is not None:
            self._close_prompt()
            self.note("cancelled")

    @on(Input.Submitted, "#promptinput")
    def _prompt_submitted(self, event: Input.Submitted) -> None:
        kind, summary = self._pending, event.value.strip()
        self._close_prompt()
        if not kind or not summary:
            return
        body: dict = {"type": kind, "summary": summary}
        if kind in ("ack", "nak"):
            post = self.selected_post()
            if post is None:
                self.note("the post being replied to is no longer selected")
                return
            # Both halves of the addressing, prefilled: a reply that names the
            # thread but not the reader lands in nobody's inbox.
            body["re"] = post.get("id")
            body["to"] = post.get("from")
        self.send(body)

    @work(thread=True, group="post")
    def send(self, body: dict) -> None:
        try:
            result = self.client.post(body)
        except httpx.HTTPError as e:
            self.call_from_thread(self.note, f"post failed: {e}")
            return
        self.call_from_thread(self.note, f"posted {body['type']} as #{result.get('id')}")

    def action_pull(self) -> None:
        post = self.selected_post()
        if post is None or post.get("type") != "published":
            self.note("`p` acts on a `published` post — select one on the Board view")
            return
        if not _ref(post, "repo"):
            self.note("that `published` post names no repo, so there is no checkout to pull")
            return
        self.run_local("pull", post)

    def action_pick(self) -> None:
        post = self.selected_post()
        if post is None or post.get("type") != "landed":
            self.note("`c` acts on a `landed` post — select one on the Board view")
            return
        sha = _ref(post, "commit")
        if not sha:
            self.note("that `landed` post names no commit, so there is nothing to pick")
            return
        # Checked here rather than at the board: `has_commit` is rejected under
        # seven characters, and a 422 would report a hand-written post's short
        # ref as "could not ask the board where to act".
        if len(sha) < 7:
            self.note(f"{sha} is too short to locate — a commit ref needs 7+ characters")
            return
        if not _ref(post, "repo"):
            self.note("that `landed` post names no repo, so there is nowhere to pick it into")
            return
        self.run_local("pick", post)

    @work(thread=True, group="local")
    def run_local(self, action: str, post: dict) -> None:
        repo, sha = _ref(post, "repo"), _ref(post, "commit")
        try:
            params = {"device": self.device}
            if action == "pick":
                params["has_commit"] = sha
            else:
                params["repo"] = repo
                branch = _ref(post, "branch")
                if branch:
                    params["branch"] = branch
            registered = self.client.get_worktrees(params)
        except httpx.HTTPError as e:
            self.call_from_thread(self.note, f"could not ask the board where to act: {e}")
            return

        if action == "pick":
            # find_commit's job: the SHA already exists somewhere on this machine,
            # so pick the checkout that does NOT have it and can therefore take it.
            holders = {w["path"] for w in local.local_worktrees(registered, self.device)}
            try:
                targets = local.local_worktrees(
                    self.client.get_worktrees({"device": self.device, "repo": repo}), self.device
                )
            except httpx.HTTPError as e:
                self.call_from_thread(self.note, f"could not list this machine's worktrees: {e}")
                return
            candidates = [w for w in targets if w["path"] not in holders]
            if not candidates:
                where = ", ".join(sorted(holders)) or "nowhere on this machine"
                self.call_from_thread(self.note, f"{sha[:12]} is already in {where}")
                return
        else:
            candidates = local.local_worktrees(registered, self.device)

        if not candidates:
            self.call_from_thread(
                self.note,
                f"no registered checkout of {repo} on {self.device} — run report_git there first",
            )
            return
        path = candidates[0]["path"]
        outcome = local.pull(path) if action == "pull" else local.cherry_pick(path, sha)
        prefix = "✓" if outcome.ok else "✗"
        self.call_from_thread(self.note, f"{prefix} {path}: {outcome.message}")

    @on(DataTable.RowSelected, "#sessions")
    def _resume(self, event: DataTable.RowSelected) -> None:
        """Choosing a session from a list is a list operation — hence here.

        The app exits carrying the request; ``__main__`` does the pulling and the
        exec, because handing this terminal to `claude --resume` is exactly what a
        full-screen app cannot do and get back.
        """
        session = str(event.row_key.value) if event.row_key.value else None
        if session:
            self.exit(ResumeRequest(session))


def _ref(post: dict, kind: str) -> str | None:
    for ref in post.get("refs") or []:
        if ref.get("kind") == kind and ref.get("value"):
            return str(ref["value"])
    return None
