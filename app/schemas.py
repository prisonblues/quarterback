from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models.post import Post

# Post types, inherited from cena and extended for quarterback (issue #127).
POST_TYPES = {
    "note",
    "status",
    "ask",
    "ack",
    "nak",
    "done",
    "finding",
    "landed",
    "published",
    "presence",
    "stuck",
    "message",
}

#: Types kept out of the default board read, because they are high-volume traffic
#: rather than decisions an arriving agent orients on. Muting is a property of the
#: *briefing*, never of the mailbox — see :func:`app.api.posts.read_board`, where a
#: read narrowed by ``to=`` ignores this set entirely so directed mail always reaches
#: its recipient.
#:
#: ``presence`` is the heartbeat stream (~93% of the board). ``message`` is
#: agent-to-agent conversation relayed through the board (issue #155): it belongs on
#: the record so a third agent *can* read an exchange it was not part of, but putting
#: it in every orient read would drown the posts the board exists to surface.
MUTED_TYPES = frozenset({"presence", "message"})

REF_KINDS = ("issue", "pr", "branch", "worktree", "commit", "repo")


class Ref(BaseModel):
    """A link from a post to a piece of dev context.

    ``value`` is the display token (e.g. "45", "feat-x", "abc123f"); ``repo`` is
    "owner/name" for issue/pr/commit; ``url`` is an explicit link (else the
    browser derives a GitHub URL from kind + repo + value).
    """

    kind: Literal["issue", "pr", "branch", "worktree", "commit", "repo"]
    value: str = Field(min_length=1)
    repo: str | None = None
    url: str | None = None


class PostIn(BaseModel):
    """Request body for POST /post. Author is derived from the bearer token, not the body."""

    type: str = "note"
    summary: str = Field(min_length=1)
    detail: str | None = None
    detail_ref: str | None = None
    re: int | None = None
    to: str | None = None
    session: str | None = None      # the CC session this event belongs to
    refs: list[Ref] | None = None

    @model_validator(mode="after")
    def _check(self) -> PostIn:
        if self.type not in POST_TYPES:
            raise ValueError(f"unknown type {self.type!r}; allowed: {sorted(POST_TYPES)}")
        if self.detail is not None and self.detail_ref is not None:
            raise ValueError("provide at most one of detail / detail_ref")
        return self


def summary_tier(p: Post) -> dict:
    """The lightweight view carried in /board and /stream — no detail blob."""
    return {
        "id": p.id,
        "session": p.session,
        "ts": p.ts.isoformat() if isinstance(p.ts, datetime) else p.ts,
        "from": p.author,
        "type": p.type,
        "summary": p.summary,
        "re": p.re,
        "to": p.recipient,
        "detail_ref": p.detail_ref,
        "has_detail": p.detail is not None,
        "refs": p.refs or [],
    }


def full_tier(p: Post) -> dict:
    """summary_tier plus the inline detail — returned by GET /post/{id}."""
    return {**summary_tier(p), "detail": p.detail}
