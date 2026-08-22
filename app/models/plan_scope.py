from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.scope import PROJECT_SIGIL


class PlanScope(Base):
    """A scope a **person** has declared, for work with no forge behind it (#323).

    Every plan item belongs to a scope. Most scopes are GitHub repos and need no
    row here: ``owner/name`` is checkable against
    :data:`app.claimkey.REPO_RE`, so a typo fails a rule rather than inventing
    anything. This table exists for the other kind — ``project:65lowther`` — where
    there is no rule a typo can fail.

    **Why a registry at all, when the sigil is already explicit.** The sigil stops
    a mistyped *repo* becoming a scope, which is #148's ambiguity returning by
    another door and the thing #323 calls the sharp edge. It does not stop a
    mistyped *scope*: ``project:65lowthr`` is well-formed, and inferring the scope
    from the spelling alone would let one agent's slip create a second name for
    work that already has one — the board then holding two lists that no read
    reconciles and no reader can see the halves of. So a project scope exists
    because a row says it does.

    **And a person writes that row, not an agent.** ``app.auth.human`` guards the
    plan's ORDER because the plan is the fleet's shared intent and agents must not
    rewrite it. What the scopes *are* is the same decision one level up: an agent
    that could mint one could split the plan into lists nobody asked for, and it
    would do so silently, which is worse than a refusal. Agents put work into
    scopes; a person says which scopes there are.

    **No state column, deliberately.** ``plans`` and ``plan_items`` carry
    ``open``/``done``/``dropped`` because a plan is worked and finished. A scope is
    not worked; it is a name. Retiring one would mean deciding what happens to the
    rows still in it, and nothing has asked for that — an unused scope costs one
    row and shows an empty list, which is exactly what it is.
    """

    __tablename__ = "plan_scopes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The canonical scope string, sigil included — ``project:65lowther``. Stored
    #: with the prefix rather than as a bare name so it is byte-identical to
    #: ``plan_items.repo`` and ``plans.repo``: the two are compared constantly, and
    #: a registry keyed on a *different* spelling of the same scope would be the
    #: two-spellings defect this whole issue is downstream of, rebuilt inside the
    #: fix for it.
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    #: What this scope is, in the declaring person's words — the sentence that
    #: makes ``project:65lowther`` mean something to the next reader. A repo scope
    #: has a GitHub page to answer that question; this has only what was typed here.
    note: Mapped[str | None] = mapped_column(Text)
    #: The person who declared it. An identity, like every other ``*_by`` column
    #: here, and here it is the record of who made a decision only a person may.
    added_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # The sigil, as a database fact. Without it this table could hold
        # `65lowther`, and then a scope would have two spellings depending on which
        # table you read it out of.
        CheckConstraint(f"name LIKE '{PROJECT_SIGIL}%'", name="ck_plan_scopes_sigil"),
        CheckConstraint("name = lower(name)", name="ck_plan_scopes_lower"),
        CheckConstraint(f"length(name) > {len(PROJECT_SIGIL)}", name="ck_plan_scopes_name"),
    )
