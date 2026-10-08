"""`qb-watch-board` (#825): which posts wake an agent, and how the watch ends.

Run: pytest harness/tests/test_qb_watch_board.py
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
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
    assert w.wanted(post(5, re=7), ME, 7)
    assert not w.wanted(post(6, re=8), ME, 7)


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
    assert capsys.readouterr().out == ""


def test_a_new_reply_is_printed_once_and_the_cursor_moves_past_it(run, capsys, tmp_path):
    run([[post(40)], [post(41, "nak")]])
    assert "#41 nak" in capsys.readouterr().out
    assert (tmp_path / "qb-board-watch-t").read_text() == "41"


def test_a_thread_watch_ends_at_the_first_reply(run, capsys):
    pages = [[post(40)], [], [post(41, re=7)]]
    assert run(pages, re=7, once=False) == 0
    assert "#41" in capsys.readouterr().out


def test_eight_failed_reads_in_a_row_end_the_watch(run, capsys):
    boom = urllib.error.URLError("down")
    assert run([[post(1)]] + [boom] * 8, once=False) == 1
    assert "giving up" in capsys.readouterr().out


def test_one_failed_read_is_a_wait_not_an_end(run):
    boom = urllib.error.URLError("down")
    assert run([[post(1)], boom, [post(2, re=7)]], re=7, once=False) == 0
