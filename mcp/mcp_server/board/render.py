"""Formatting shared by the line tail and the full-screen client.

One module so the two halves cannot drift: a post reads the same whether it
arrives in ``qb board --follow`` on a headless box or in the Board pane. The
colours are the browser board's ``TYPE_COLOR`` map, copied deliberately — three
surfaces onto one stream should agree about what a `published` post looks like.
"""

from __future__ import annotations

import os
import sys

#: app/static/board.html's TYPE_COLOR, verbatim. `published` is `landed`'s
#: louder sibling for the same reason there: it's on the remote, go pull it.
TYPE_COLOR = {
    "note": "#5b6472",
    "status": "#6ea8fe",
    "ask": "#c084fc",
    "ack": "#35c48a",
    "nak": "#e5484d",
    "done": "#35c48a",
    "finding": "#f0b429",
    "landed": "#22b8cf",
    "published": "#67e8f9",
    "presence": "#8b93a3",
    "stuck": "#e5484d",
}
_DEFAULT_COLOR = "#5b6472"

_MUTED = "#8b93a3"
_ACCENT = "#6ea8fe"


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def paint(text: str, hex_colour: str, *, colour: bool = True) -> str:
    if not colour or not text:
        return text
    r, g, b = _rgb(hex_colour)
    return f"\x1b[38;2;{r};{g};{b}m{text}\x1b[0m"


def type_colour(post_type: str) -> str:
    return TYPE_COLOR.get(post_type, _DEFAULT_COLOR)


def want_colour(stream=None, env: dict[str, str] | None = None) -> bool:
    """Colour when we're writing to a terminal and NO_COLOR isn't set.

    Piping is the point of the tail — `qb board --follow | grep finding` has to
    match on the word, not on the word wrapped in escapes — so a non-tty is the
    case that must come out plain without anyone remembering a flag.
    """
    env = os.environ if env is None else env
    if env.get("NO_COLOR"):
        return False
    stream = sys.stdout if stream is None else stream
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def short_time(ts: str | None) -> str:
    """``HH:MM:SS`` out of an ISO timestamp, without parsing it into a datetime.

    The board's timestamps are UTC ISO-8601 and the browser slices them the same
    way (`(p.ts||"").slice(11,19)`); converting to local time here would make two
    views of one post disagree about when it happened.
    """
    return (ts or "")[11:19] or "--:--:--"


def format_refs(refs: list[dict] | None) -> str:
    """``[issue #110, pr #139]`` — the dev context a post links itself to."""
    if not refs:
        return ""
    parts = []
    for ref in refs:
        kind, value = ref.get("kind"), ref.get("value")
        if not kind or not value:
            continue
        if kind in ("issue", "pr"):
            parts.append(f"{kind} #{value}")
        elif kind == "commit":
            parts.append(f"commit {str(value)[:12]}")
        else:
            parts.append(f"{kind} {value}")
    return f"[{', '.join(parts)}]" if parts else ""


def format_post(post: dict, *, colour: bool = True) -> str:
    """One post as one line: time, id, author, type, summary, addressing, refs.

    Deliberately single-line and column-aligned rather than wrapped. This is the
    output a headless host greps and pipes, and a summary that spilled onto a
    second line would break every `grep -c` over it.
    """
    tc = type_colour(post.get("type", "note"))
    ts = paint(short_time(post.get("ts")), _MUTED, colour=colour)
    pid = paint(f"#{post.get('id', '?'):<6}", _MUTED, colour=colour)
    author = paint(f"{post.get('from') or '?'!s:<20.20}", tc, colour=colour)
    ptype = paint(f"{post.get('type') or 'note'!s:<9.9}", tc, colour=colour)

    tail = []
    if post.get("to"):
        tail.append(paint(f"→{post['to']}", _ACCENT, colour=colour))
    if post.get("re"):
        tail.append(paint(f"re:{post['re']}", _MUTED, colour=colour))
    if post.get("has_detail") or post.get("detail_ref"):
        tail.append(paint("+detail", _MUTED, colour=colour))
    refs = format_refs(post.get("refs"))
    if refs:
        tail.append(paint(refs, _ACCENT, colour=colour))

    summary = " ".join(str(post.get("summary") or "").split())
    line = f"{ts} {pid} {author} {ptype} {summary}"
    return f"{line}  {'  '.join(tail)}" if tail else line
