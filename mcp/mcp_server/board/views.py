"""Turning board payloads into rows — the part of the client that is not Textual.

Kept apart from ``tui.py`` on purpose: the decisions worth testing are here (what
counts as an unanswered ask, how a null cost renders, when a lease reads as
stale), and none of them needs a terminal to check.
"""

from __future__ import annotations

from datetime import UTC, datetime

#: What a figure the vendor never reported renders as. Deliberately a phrase and
#: not a dash: "not recorded" and "free" are different claims, and a reviewer with
#: no stated cost must not read as the cheap one on a page whose whole question is
#: whether the expensive tier is worth it.
NOT_RECORDED = "not recorded"

#: What a lease that never reported a workflow stage renders as in a column.
#:
#: The word is **unreported**, and that is not a private choice: it is what
#: ``/fleet`` says, in the same four-word vocabulary it uses for a session nobody
#: ended (``live | ended | unclear | unreported``). The browser page spells it out
#: because it has the width. This column is six characters wide, so it uses the
#: glyph the fleet's terminals already use for an unsaid value — ``repo``,
#: ``state`` and ``title`` all render one this way, and ``harness/bin/qbdata.py``
#: renders this same field this same way for ``qb-dash``.
#:
#: **It cannot be misread as a stage**, which is the whole job. A stage is 1-6
#: alphanumerics by construction — ``app.api.leases.STAGE_RE`` at the board's
#: edge, and ``qb-stage``'s own check before that — so a non-alphanumeric glyph is
#: outside the value space entirely. An *empty* cell is not: it is equally
#: consistent with a rendering bug, a truncated column and a stage nobody said,
#: and a column that fills in for some rows and is blank for others reads as
#: "those agents have no stage" (#261, and the note on #262 about local markers).
STAGE_UNREPORTED = "—"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        # TypeError as well as ValueError: a payload carrying an epoch int or a bool
        # where a timestamp belongs is malformed, not exceptional, and every caller
        # here already renders None as "?".
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def age(ts: str | None, now: datetime | None = None) -> str:
    """Coarse age of an ISO timestamp — ``12s`` / ``4m`` / ``2h13m`` / ``3d``."""
    when = _parse(ts)
    if when is None:
        return "?"
    now = now or datetime.now(UTC)
    delta = int((now - when).total_seconds())
    if delta < 0:
        return "0s"
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h{(delta % 3600) // 60:02d}m"
    return f"{delta // 86400}d"


def ttl(expires: str | None, now: datetime | None = None) -> str:
    """Lease freshness: how long this agent's presence is still good for.

    An expired lease is reported as expired rather than as a negative age. The
    board only returns live leases, so seeing one here means it lapsed between the
    fetch and the render — which is information, not an error.
    """
    when = _parse(expires)
    if when is None:
        return "?"
    now = now or datetime.now(UTC)
    left = int((when - now).total_seconds())
    if left <= 0:
        return "expired"
    if left < 60:
        return f"{left}s"
    if left < 3600:
        return f"{left // 60}m"
    return f"{left // 3600}h{(left % 3600) // 60:02d}m"


def fleet_rows(active: dict, now: datetime | None = None) -> list[dict]:
    """``/active`` as one flat list — top-level agents first, then their fan-out.

    Sub-agents are kept rather than filtered: "who is live where" includes the
    fan-out, and the ``kind`` column is what stops one agent's five workers
    reading as five peers.
    """
    rows = []
    for a in active.get("agents", []):
        rows.append(
            {
                "kind": "agent",
                "holder": a.get("holder") or "?",
                "device": a.get("device") or "?",
                "repo": a.get("repo") or "",
                "branch": a.get("branch") or "",
                # The one field here that changes as the work progresses: repo,
                # branch and title read identically from the first cut to the
                # third review round (#262).
                "stage": a.get("stage") or STAGE_UNREPORTED,
                "title": a.get("title") or "",
                "ttl": ttl(a.get("expires"), now),
                "since": age(a.get("since"), now),
                "own": bool(a.get("own")),
                "session": a.get("session") or "",
                "cwd": a.get("cwd") or "",
            }
        )
    for s in active.get("subagents", []):
        rows.append(
            {
                "kind": "subagent",
                "holder": s.get("holder") or "?",
                "device": s.get("device") or "?",
                # Sub-agents carry no repo/branch of their own — they inherit the
                # parent's checkout, and inventing one here would put a value in
                # the column that nothing on the board actually said.
                "repo": "",
                "branch": "",
                # No stage of its own either, and the parent's is not borrowed —
                # the same rule repo and branch follow above. The fan-out of an
                # `R1F` fix pass is arguably the clearest case for inheriting one,
                # and it is still an invention: nothing on the board said it.
                "stage": STAGE_UNREPORTED,
                "title": s.get("label") or "",
                "ttl": ttl(s.get("expires"), now),
                "since": age(s.get("since"), now),
                "own": bool(s.get("own")),
                "session": s.get("parent_session") or "",
                "cwd": s.get("cwd") or "",
            }
        )
    rows.sort(key=lambda r: (r["kind"] != "agent", r["device"], r["holder"]))
    return rows


def session_rows(sessions: list[dict], now: datetime | None = None) -> list[dict]:
    """``/sessions`` as rows: what can I resume, from which machine."""
    rows = []
    for s in sessions:
        size = s.get("size")
        rows.append(
            {
                "live": bool(s.get("live")),
                "resumable": bool(s.get("resumable")),
                "session": s.get("session") or "",
                "title": s.get("title") or (s.get("cwd") or "").rsplit("/", 1)[-1] or "—",
                "holder": s.get("holder") or "?",
                "device": s.get("device") or "?",
                "cwd": s.get("cwd") or "",
                "age": age(s.get("updated_at"), now),
                "size": f"{size // 1024}k" if isinstance(size, int) else "—",
                "recap": s.get("recap") or "",
            }
        )
    return rows


def _fmt_cost(value: float | None) -> str:
    return NOT_RECORDED if value is None else f"${value:.4f}"


def _fmt_int(value: int | None) -> str:
    return NOT_RECORDED if value is None else f"{value:,}"


def _fmt_ratio(value: float | None) -> str:
    return NOT_RECORDED if value is None else f"{value:.0%}"


def _fmt_per_run(value: float | None) -> str:
    return NOT_RECORDED if value is None else f"{value:.2f}"


def panel_rows(stats: dict) -> list[dict]:
    """``/review/stats`` by_model as rows: what reviews cost and what they found.

    Four columns can be genuinely absent and each is rendered as *not recorded*
    rather than as a zero — cost, tokens, precision, and confirmed-per-run. A
    vendor that does not state a price is not a free vendor, and a member the judge
    never ruled on is not a member that was always wrong.
    """
    rows = []
    for m in stats.get("by_model", []):
        rows.append(
            {
                "reviewer": m.get("reviewer") or "?",
                # The em-dash here means "none", not "unknown": a member with no
                # model or effort pinned genuinely has none, and that is the fact
                # rather than a gap in the payload — hence a mark, not NOT_RECORDED.
                "model": m.get("model") or "—",
                "effort": m.get("effort") or "—",
                "runs": m.get("runs", 0),
                "ran": m.get("ran", 0),
                "confirmed": m.get("confirmed", 0),
                "dismissed": m.get("dismissed", 0),
                "precision": _fmt_ratio(m.get("precision")),
                "per_run": _fmt_per_run(m.get("confirmed_per_run")),
                "tokens": _fmt_int(m.get("total_tokens")),
                "cost": _fmt_cost(m.get("cost_usd")),
                # Coverage markers: a sum over a half-instrumented window is not a
                # sum over the window, and saying so is the difference between a
                # number and a misleading one.
                "cost_runs": m.get("cost_runs", 0),
                "token_runs": m.get("token_runs", 0),
            }
        )
    return rows


def panel_window(stats: dict) -> str:
    """One line describing what the Panel numbers are actually over."""
    window = stats.get("window") or {}
    scope = window.get("repo") or "all repos"
    # No default of True: "judged runs only" is a claim about which runs the numbers
    # came from, and an older or partial stats payload that omits the field has not
    # made it. Saying so beats labelling an unknown window as the narrow one.
    judged_only = window.get("judged_only")
    if judged_only is None:
        judged = f"coverage {NOT_RECORDED}"
    else:
        judged = "judged runs only" if judged_only else "all runs"
    return (
        f"{stats.get('runs', 0)} run(s) over {stats.get('prs', 0)} PR(s) "
        f"in {stats.get('repos', 0)} repo(s) · {scope} · {judged}"
    )


def addressed_to(recipient: str | None, who: str) -> bool:
    """Does a post addressed to ``recipient`` reach ``who``?

    The board's rule, both directions: an agent's inbox includes what was sent to
    its whole machine, and a machine's inbox includes what was sent to its agents
    (``app.identity.inbox_clause``). Reproduced here because ``/stream`` carries
    no ``to`` filter — so without it the tail's server-fetched backlog and its
    client-filtered live half would disagree about what ``--to zeus`` means, and
    the same post would appear in one and not the other.
    """
    if not recipient:
        return False
    if recipient == who:
        return True
    # Split on the separator rather than testing a prefix: `zeus-two/a` starts
    # with neither `zeus` as a path segment nor anything else `zeus` should see.
    return recipient.split("/", 1)[0] == who or who.split("/", 1)[0] == recipient


def answers_for(author: str | None, me: str | None) -> bool:
    """Does a reply by ``author`` count as *my* reply?

    The board's addressing is hierarchical: ``to=zeus`` reaches every agent on
    zeus, and ``?to=@me`` for a bare machine returns its agents' mail as well as
    its own. So the answer side has to be hierarchical too. Without this, a
    terminal client identifying as the machine reads every ask any of its agents
    already answered as still outstanding — which was 20 of them on the first
    real run, and an alert nobody can clear is an alert nobody reads.
    """
    if me is None:
        # Identity unknown: every reply counts, because an alert that fires on
        # everything is worse than one that fires on nothing you can act on.
        return True
    if author is None:
        # An anonymous reply is somebody's, but there is no evidence it is mine, and
        # treating it as mine clears an ask that is still sitting in my inbox.
        return False
    if author == me:
        return True
    return "/" not in me and author.startswith(f"{me}/")


def unanswered_asks(inbox: list[dict], seen: list[dict], me: str | None) -> list[dict]:
    """Asks directed at me that I have not answered, newest last.

    "Answered" means an ``ack``/``nak`` carrying ``re`` of the ask, from someone
    the reply counts for (see :func:`answers_for`). It is computed over the posts
    this client has actually seen, so a reply sent before the window opened
    cannot be counted — the honest reading is "asks you have not answered *in
    view*", and the client says so rather than claiming a mailbox it cannot see
    all of.
    """
    # Compared as strings: `re` and `id` reach here from two payloads — a `/board`
    # page and a `/stream` frame — and JSON only guarantees they mean the same
    # number, not that both sides spelled it as one. `"123" != 123` would leave an
    # answered ask on the list for good.
    answered = {
        str(p.get("re"))
        for p in seen
        if p.get("type") in ("ack", "nak") and p.get("re") and answers_for(p.get("from"), me)
    }
    return [p for p in inbox if p.get("type") == "ask" and str(p.get("id")) not in answered]


def staleness(sync: dict) -> tuple[bool, str]:
    """The other ambient fact: is this checkout behind what peers have pushed?

    Returns (stale, one-line advice). ``/sync`` already composes the advice line
    the hooks print, so it is passed through rather than reworded — one wording
    of "you are behind" across the fleet.
    """
    return bool(sync.get("stale")), sync.get("advice") or "in sync"
