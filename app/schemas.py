from __future__ import annotations

from datetime import datetime

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
    "presence",
    "stuck",
}


class PostIn(BaseModel):
    """Request body for POST /post. Author is derived from the bearer token, not the body."""

    type: str = "note"
    summary: str = Field(min_length=1)
    detail: str | None = None
    detail_ref: str | None = None
    re: int | None = None
    to: str | None = None

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
        "ts": p.ts.isoformat() if isinstance(p.ts, datetime) else p.ts,
        "from": p.author,
        "type": p.type,
        "summary": p.summary,
        "re": p.re,
        "to": p.recipient,
        "detail_ref": p.detail_ref,
        "has_detail": p.detail is not None,
    }


def full_tier(p: Post) -> dict:
    """summary_tier plus the inline detail — returned by GET /post/{id}."""
    return {**summary_tier(p), "detail": p.detail}
