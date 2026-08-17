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


def test_an_explicitly_null_id_renders_as_a_placeholder():
    """`.get('id', '?')` never fires on `{"id": None}` — `f"#{None:<6}"` raises."""
    out = format_post({**POST, "id": None}, colour=False)
    assert "#?" in out


def test_escapes_in_a_post_do_not_reach_the_terminal():
    """Board content is written by anyone holding a token, and a tail runs unattended.

    An OSC in a summary retitles the reader's terminal and a CSI can clear it, so
    the sequences are stripped rather than passed through with the text.
    """
    hostile = {
        **POST,
        "from": "zeus/\x1b]0;pwned\x07agent",
        "summary": "looks fine\x1b[2J\x1b[38;2;255;0;0mred",
        "to": "zeus/\x07bell",
        "re": "33\x1b41",
        "ts": "2026-08-16T\x1b[1m20:35\x1b",
        "refs": [{"kind": "branch\x1b[0m", "value": "feat/\x1bx"}],
    }
    out = format_post(hostile, colour=False)
    assert "\x1b" not in out and "\x07" not in out
    assert "]0;pwned" in out  # the ESC is gone; the inert remains are still greppable
    assert "\n" not in out


def test_stripping_escapes_does_not_disturb_our_own_colour():
    """The scrub runs on the untrusted text, before paint wraps it."""
    out = format_post({**POST, "summary": "\x1b[2Jclear"}, colour=True)
    r, g, b = (0x35, 0xC4, 0x8A)  # TYPE_COLOR["ack"]
    assert f"\x1b[38;2;{r};{g};{b}m" in out
    assert "\x1b[2J" not in out


def test_scrubbing_happens_before_padding_so_the_columns_stay_put():
    """Escapes occupy no columns, so scrubbing after padding lets a post shift the row."""
    hostile = format_post({**POST, "from": "\x1b[31mzeus/a", "summary": "MARK"}, colour=False)
    plain = format_post({**POST, "from": "zeus/a", "summary": "MARK"}, colour=False)
    assert hostile.index("MARK") == plain.index("MARK")


def test_refs_carrying_escapes_are_stripped_too():
    assert format_refs([{"kind": "issue", "value": "1\x1b[31m10"}]) == "[issue #1[31m10]"


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
