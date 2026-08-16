"""The tail's output is a text interface — these pin the shape people grep."""

from __future__ import annotations

import io

from mcp_server.board.render import (
    TYPE_COLOR,
    format_post,
    format_refs,
    short_time,
    type_colour,
    want_colour,
)

POST = {
    "id": 3344,
    "ts": "2026-08-16T20:35:12.151350+00:00",
    "from": "zeus/heron-sandy",
    "type": "ack",
    "summary": "#110 prereqs: client blocked on nothing",
    "re": 3341,
    "to": "zeus/fern-nectar",
    "has_detail": True,
    "refs": [{"kind": "issue", "value": "110", "repo": "prisonblues/quarterback"}],
}


def test_a_post_is_exactly_one_line():
    """A summary that spilled onto a second line would break every `grep -c` over it."""
    multiline = {**POST, "summary": "first line\nsecond line\n\tthird"}
    out = format_post(multiline, colour=False)
    assert "\n" not in out
    assert "first line second line third" in out


def test_line_carries_time_id_author_type_and_addressing():
    out = format_post(POST, colour=False)
    assert out.startswith("20:35:12 #3344")
    assert "zeus/heron-sandy" in out
    assert "ack" in out
    assert "→zeus/fern-nectar" in out
    assert "re:3341" in out
    assert "+detail" in out
    assert "[issue #110]" in out


def test_colour_off_emits_no_escapes():
    assert "\x1b" not in format_post(POST, colour=False)


def test_colour_on_uses_the_browser_boards_palette():
    """Three surfaces onto one stream should agree what a `published` post looks like."""
    out = format_post({**POST, "type": "published"}, colour=True)
    r, g, b = (0x67, 0xE8, 0xF9)  # TYPE_COLOR["published"]
    assert TYPE_COLOR["published"] == "#67e8f9"
    assert f"\x1b[38;2;{r};{g};{b}m" in out


def test_unknown_type_falls_back_rather_than_raising():
    assert type_colour("something-new") == "#5b6472"
    assert format_post({**POST, "type": "something-new"}, colour=True)


def test_missing_fields_degrade_to_placeholders():
    out = format_post({"id": 1}, colour=False)
    assert "#1" in out and "?" in out and "--:--:--" in out


def test_short_time_slices_utc_without_reinterpreting_it():
    # Sliced, not parsed: converting to local time here would make the tail and
    # the browser disagree about when one post happened.
    assert short_time("2026-08-16T20:35:12.151350+00:00") == "20:35:12"
    assert short_time(None) == "--:--:--"


def test_refs_render_per_kind():
    refs = [
        {"kind": "issue", "value": "110"},
        {"kind": "pr", "value": "139"},
        {"kind": "commit", "value": "abc1234def5678901234"},
        {"kind": "branch", "value": "feat/x"},
        {"kind": "bogus"},
    ]
    assert format_refs(refs) == "[issue #110, pr #139, commit abc1234def56, branch feat/x]"
    assert format_refs([]) == ""
    assert format_refs(None) == ""


def test_colour_is_off_when_piped_and_when_no_color_is_set():
    tty = io.StringIO()
    tty.isatty = lambda: True  # type: ignore[method-assign]
    assert want_colour(tty, {}) is True
    assert want_colour(tty, {"NO_COLOR": "1"}) is False
    assert want_colour(io.StringIO(), {}) is False
