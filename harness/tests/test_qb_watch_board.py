"""`qb-watch-board` (#825): which posts wake an agent, and how the watch ends.

Run: pytest harness/tests/test_qb_watch_board.py
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "qb-watch-board"
_loader = importlib.machinery.SourceFileLoader("qb_watch_board", str(SCRIPT))
_spec = importlib.util.spec_from_loader("qb_watch_board", _loader)
w = importlib.util.module_from_spec(_spec)
sys.modules["qb_watch_board"] = w
_loader.exec_module(w)

ME = {"zeus/amber-otter", "zeus/f5ca7491"}


def post(id, type="ack", to="zeus/amber-otter", frm="laptop/peer", re=None):
    return {"id": id, "type": type, "to": to, "from": frm, "re": re, "summary": f"post {id}"}


def test_a_reply_addressed_by_name_wakes_whatever_its_type():
    for kind in ("ack", "nak", "message", "note"):
        assert w.wanted(post(1, kind), ME, None)


def test_a_broadcast_to_the_machine_wakes_only_for_an_ask():
    assert w.wanted(post(1, "ask", to="zeus"), ME, None)
    assert not w.wanted(post(2, "finding", to="zeus"), ME, None)


def test_the_alias_counts_as_addressed_by_name():
    assert w.wanted(post(1, "nak", to="zeus/f5ca7491"), ME, None)


def test_my_own_posts_never_wake_me():
    assert not w.wanted(post(1, "ack", frm="zeus/amber-otter"), ME, None)
    assert not w.wanted(post(2, "ask", to="zeus", frm="zeus/f5ca7491"), ME, None)


def test_a_thread_watch_ignores_replies_to_other_posts():
    assert w.wanted(post(5, re=7), ME, {7})
    assert not w.wanted(post(6, re=8), ME, {7})


def test_the_line_names_the_sender_and_how_to_reply():
    text = w.line(post(9, "nak", frm="laptop/peer"))
    assert "#9 nak from laptop/peer" in text
    assert 'to="laptop/peer", re=9' in text


def test_a_long_summary_is_clipped():
    p = post(1) | {"summary": "x" * 500}
    assert len(w.line(p)) < 400


def test_instance_key_follows_the_label_then_the_session(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "f5ca7491-aaaa")
    monkeypatch.delenv("QUARTERBACK_INSTANCE", raising=False)
    assert w.instance_key() == "f5ca7491"
    monkeypatch.setenv("QUARTERBACK_INSTANCE", "seat 3")
    assert w.instance_key() == "seat-3"
    monkeypatch.setenv("QUARTERBACK_INSTANCE", " ")
    assert w.instance_key() == "f5ca7491"


QB_HOOK = Path(__file__).resolve().parents[1] / "bin" / "qb-hook"
BASH = shutil.which("bash") or "/bin/bash"


@pytest.mark.parametrize("label", ["seat-1", "seat 3", "..x", "aé-b", "a" * 60, "~~"])
def test_instance_slug_is_qb_hooks_rule(label):
    """qb-hook's `tr` replaces every non-ASCII BYTE, so "é" is two dashes there. A
    per-character slug made one, and named a different agent from the hooks (#857)."""
    rule = next(ln for ln in QB_HOOK.read_text().splitlines() if ln.startswith("_key_slug()"))
    got = subprocess.run([BASH, "-c", f'{rule}\n_key_slug "$1"', "_", label],
                         capture_output=True, text=True, check=True,
                         env={**os.environ, "LC_ALL": "C.UTF-8"})
    assert w.key_slug(label) == got.stdout.rstrip("\n")


class _Resp(io.BytesIO):
    headers = {"X-Board-Head": "7"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_every_board_request_carries_qbdatas_tls_context(monkeypatch):
    """A bare urlopen fails every HTTPS read on an interpreter with no CA bundle of
    its own, and the watch then reports an unreadable board that is up (#857)."""
    sentinel = object()
    seen = []

    def fake_urlopen(req, timeout=None, context=None):
        seen.append(context)
        return _Resp(json.dumps({"agent": "zeus/a"} if req.full_url.endswith("/whoami")
                                else []).encode())

    monkeypatch.setattr(w, "_ssl_context", lambda: sentinel, raising=False)
    monkeypatch.setattr(w.urllib.request, "urlopen", fake_urlopen)
    board = w.Board("https://qb.example", "tok", "t")
    board.read({})
    w.identities(board)
    assert seen == [sentinel, sentinel]


class FakeBoard:
    instance = "t"

    def __init__(self, pages):
        self.pages = list(pages)

    def read(self, params):
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page, max([p["id"] for p in page], default=params.get("since", 0))


def args(**kw):
    return argparse.Namespace(**{"re": None, "interval": 0, "timeout": 0, "once": True} | kw)


@pytest.fixture
def run(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(w, "identities", lambda board: ME)
    monkeypatch.setattr(w.time, "sleep", lambda s: None)

    def go(pages, **kw):
        return w.watch(FakeBoard(pages), args(**kw))

    return go


def test_first_pass_starts_at_now_and_reports_nothing_older(run, capsys):
    assert run([[post(40)], []]) == 0
    assert "📨" not in capsys.readouterr().out


def test_a_new_reply_is_printed_once_and_the_cursor_moves_past_it(run, capsys, tmp_path):
    run([[post(40)], [post(41, "nak")]])
    assert "#41 nak" in capsys.readouterr().out
    assert (tmp_path / "qb-board-watch-t").read_text() == "41"


def test_a_thread_watch_ends_at_the_first_reply(run, capsys):
    pages = [[], [post(41, re=7)]]
    assert run(pages, re=7, once=False) == 0
    assert "#41" in capsys.readouterr().out


def test_eight_failed_reads_in_a_row_end_the_watch(run, capsys):
    boom = urllib.error.URLError("down")
    assert run([[post(1)]] + [boom] * 8, once=False) == 1
    assert "giving up" in capsys.readouterr().out


def test_one_failed_read_is_a_wait_not_an_end(run):
    boom = urllib.error.URLError("down")
    assert run([[post(1)], boom, [post(2, re=7)]], re=7, once=False) == 0


# --- a thread watch must not lose the reply it was started for ---


class LiveBoard:
    """A board that honours `since`, so a cursor seeded too high loses posts."""

    instance = "t"

    def __init__(self, posts):
        self.posts = posts

    def read(self, params):
        head = max(p["id"] for p in self.posts)
        since = params.get("since", 0)
        page = [p for p in self.posts if p["id"] > since]
        return (page[: params["limit"]] if "limit" in params else page), head


def test_a_reply_posted_before_the_watch_started_is_still_reported(run, capsys):
    board = LiveBoard([post(41, re=7)])
    assert w.watch(board, args(re=7, once=False, timeout=-1)) == 0
    assert "#41" in capsys.readouterr().out


def test_a_thread_watch_leaves_the_standing_cursor_alone(run, tmp_path):
    (tmp_path / "qb-board-watch-t").write_text("30")
    run([[post(41, re=7)]], re=7, once=False)
    assert (tmp_path / "qb-board-watch-t").read_text() == "30"


def test_a_reply_that_cites_a_later_post_in_the_thread_counts():
    seen = {7, 12}
    assert w.wanted(post(15, re=12), ME, seen)


def test_a_directed_reply_with_no_re_still_wakes_a_thread_watch():
    assert w.wanted(post(15, "ack", re=None), ME, {7})


def test_a_reply_to_an_unrelated_post_does_not_wake_a_thread_watch():
    assert not w.wanted(post(15, re=99), ME, {7})


# --- a watch that ends says so ---


def test_a_timeout_says_the_watch_has_ended(run, capsys):
    assert run([[post(1)], []], once=False, timeout=-1) == 0
    assert "watch ended" in capsys.readouterr().out


def test_the_watch_names_who_it_is_listening_as(run, capsys):
    run([[post(1)], []])
    assert "zeus/amber-otter" in capsys.readouterr().out


def test_no_board_client_in_harness_bin_opens_a_url_without_the_tls_context():
    """The class, not the one instance: qb-watch-board was the bin/ client that
    skipped it. Each `urlopen(` call must name `context=` before it closes."""
    bin_dir = SCRIPT.parent
    bare = []
    for path in sorted(p for p in bin_dir.iterdir() if p.is_file()):
        text = path.read_text(errors="replace")
        if not text.startswith("#!") and path.suffix != ".py":
            continue
        for at in range(len(text)):
            if not text.startswith("urlopen(", at):
                continue
            depth, end = 0, at + len("urlopen")
            while end < len(text):
                depth += {"(": 1, ")": -1}.get(text[end], 0)
                end += 1
                if depth == 0:
                    break
            if "context=" not in text[at:end]:
                bare.append(f"{path.name}:{text.count(chr(10), 0, at) + 1}")
    assert not bare, f"urlopen without context=: {bare}"
