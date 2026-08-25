"""The landing graph — what gates what, across repositories, and who is minding it (#294)

The fleet lands pull requests into shared `main` branches across several repositories
and they gate each other: one PR unblocks three, one issue waits on four with two of
them in another repository, and a branch that was mergeable at breakfast is conflicting
by lunch because two unrelated things landed in front of it. That structure had no
representation anywhere. It lived in prose in board posts and in markdown on unpushed
branches, and nothing queried it — so no agent picking up `quarterback#290` could learn
that `nix-fleet#40`"s step 0 was sitting behind it.

Two tables, because there are two facts.

`landing_edges` is the dependency: *this node cannot land until that one has*. Both ends
are **claim keys** (`prisonblues/nix-fleet#40`, `prisonblues/quarterback!290`), derived
by `app.claimkey` and never composed by a caller, which is why an edge crosses a
repository boundary without any column having to be about that. It is also the very key
`resource_leases` uses, so "who is doing this node" joins to "what gates it" with no
translation. `blocked_repo` / `blocker_repo` are redundant against the keys on purpose:
a scoped read is then a column comparison rather than a LIKE against a key whose
separator (`#` or `!`) varies by kind.

`landing_watches` is the other half — *somebody is standing by for this*. It is
deliberately not a `resource_leases` row: that table is exclusive by construction, and
several agents may legitimately wait on one pull request while none of them is doing it.
Hence a unique index on `(node_key, holder)` rather than on the node alone.

## Live rows, history below them, and no reaper

Both tables use the partial-unique pattern `resource_leases` established: UNIQUE over
the *unresolved* / *unreleased* rows only, so asserting one fact twice is a renew at the
database rather than a second copy of it, while everything that has ended accumulates
underneath and stays readable. Expiry is passive — swept on the next read by
`app.api.landing._sweep` — because a background reaper would be a second implementation
of "notice that a thing has ended" in a codebase that has one.

`landing_edges.resolution` and `landing_watches.lapsed` exist so the ending is never
merely absent. `landed` and `dropped` are different facts (the work happened, versus
somebody decided the constraint was mistaken), and so are "the watcher finished waiting"
and "the watcher vanished". Collapsing either pair would make the graph"s own history
unreadable a fortnight later, and the second one is the whole point of recording minders:
*blocked and unattended* is the dangerous state, and today it renders identically to the
safe one.

## What is NOT here

No deadline column. "These three land before the flake bump, or they get re-cut against
a path this repo no longer owns" is already an edge — the bump is a node and the three
are its blockers — so a date field would be a weaker second spelling of a constraint the
graph holds exactly.

No order, rank or priority. Turning this graph into a merge order is #80"s half of the
problem and consumes these tables rather than living in them.

No mirror of GitHub. An issue-to-issue dependency inside one repository belongs in
GitHub"s native graph (#229); what these hold is what that graph cannot express —
edges across repositories, edges ending on a pull request, and minders.

Revision ID: mef441e81
Revises: m4355ba48
Create Date: 2026-08-24

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "mef441e81"
down_revision: str | None = "m4355ba48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "landing_edges",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("blocked_key", sa.Text(), nullable=False),
        sa.Column("blocked_repo", sa.Text(), nullable=False),
        sa.Column("blocker_key", sa.Text(), nullable=False),
        sa.Column("blocker_repo", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("asserted_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Text(), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        # A self-edge is a client bug, not a graph: a node permanently blocked
        # with nothing to wait for, poisoning every depth walk that touches it.
        sa.CheckConstraint("blocked_key <> blocker_key", name="ck_landing_edges_no_self"),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution IN ('landed', 'dropped')",
            name="ck_landing_edges_resolution"),
        # Resolved and why travel together or not at all — a row cleared with no
        # reason is exactly the "it is gone and I cannot tell you what happened"
        # state the vocabulary exists to prevent.
        sa.CheckConstraint("(resolved_at IS NULL) = (resolution IS NULL)",
                           name="ck_landing_edges_resolved_pair"),
        # #350's rule, applied at the write: one repository has one stored
        # spelling, held here so nothing but `canonical_repo` can populate it.
        sa.CheckConstraint(r"blocked_repo = lower(btrim(blocked_repo, E' \t\n\r\f\013'))",
                           name="ck_landing_edges_blocked_repo_canonical"),
        sa.CheckConstraint(r"blocker_repo = lower(btrim(blocker_repo, E' \t\n\r\f\013'))",
                           name="ck_landing_edges_blocker_repo_canonical"),
        sa.PrimaryKeyConstraint("id"),
    )
    # The atomicity, and the whole of "assert it twice, store it once": UNIQUE
    # over LIVE rows only, so resolved history accumulates below it.
    op.create_index("ix_landing_edges_live", "landing_edges",
                    ["blocked_key", "blocker_key"], unique=True,
                    postgresql_where=sa.text("resolved_at IS NULL"))
    # The two ends of the same rows. `blocker` is the fan-out read — "what does
    # landing this unblock?" — which is the direction nothing could answer.
    op.create_index("ix_landing_edges_blocker", "landing_edges", ["blocker_key"])
    op.create_index("ix_landing_edges_blocked", "landing_edges", ["blocked_key"])
    op.create_index("ix_landing_edges_repos", "landing_edges",
                    ["blocked_repo", "blocker_repo"])

    op.create_table(
        "landing_watches",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("node_key", sa.Text(), nullable=False),
        sa.Column("node_repo", sa.Text(), nullable=False),
        sa.Column("holder", sa.Text(), nullable=False),
        # The session whose presence keeps this alive. NULL means the watch has
        # only its TTL — a scripted watcher with no lease — and it is reported
        # as such rather than quietly treated as live forever.
        sa.Column("session", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("ttl_seconds", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("renewed_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lapsed", sa.Boolean(), server_default=sa.text("false"),
                  nullable=False),
        sa.CheckConstraint("ttl_seconds > 0", name="ck_landing_watches_ttl_positive"),
        sa.CheckConstraint(r"node_repo = lower(btrim(node_repo, E' \t\n\r\f\013'))",
                           name="ck_landing_watches_repo_canonical"),
        sa.PrimaryKeyConstraint("id"),
    )
    # On (node, holder) and NOT on the node: minding is not claiming. Several
    # agents may legitimately stand by for one pull request while none of them
    # is doing it, so this bounds one live watch per agent per node and no more.
    op.create_index("ix_landing_watches_live", "landing_watches",
                    ["node_key", "holder"], unique=True,
                    postgresql_where=sa.text("released_at IS NULL"))
    op.create_index("ix_landing_watches_node", "landing_watches", ["node_key"])
    # The presence sweep reads this: which watches belong to sessions that have
    # stopped being present.
    op.create_index("ix_landing_watches_session", "landing_watches", ["session"])
    op.create_index("ix_landing_watches_repo", "landing_watches", ["node_repo"])


def downgrade() -> None:
    op.drop_index("ix_landing_watches_repo", table_name="landing_watches")
    op.drop_index("ix_landing_watches_session", table_name="landing_watches")
    op.drop_index("ix_landing_watches_node", table_name="landing_watches")
    op.drop_index("ix_landing_watches_live", table_name="landing_watches",
                  postgresql_where=sa.text("released_at IS NULL"))
    op.drop_table("landing_watches")
    op.drop_index("ix_landing_edges_repos", table_name="landing_edges")
    op.drop_index("ix_landing_edges_blocked", table_name="landing_edges")
    op.drop_index("ix_landing_edges_blocker", table_name="landing_edges")
    op.drop_index("ix_landing_edges_live", table_name="landing_edges",
                  postgresql_where=sa.text("resolved_at IS NULL"))
    op.drop_table("landing_edges")
