"""v2.29: the other end of the range — what a round was judged AGAINST

v2.26 (revision 0017) gave a run a `head_sha`, and its own docstring names what
was still missing: "#98 wants the other end of that range". This is that end,
and it turns out to be two columns rather than one, because the field the issue
named for the job cannot do it.

**`baseRefOid` is the merge base, not the base branch's tip.** #98 proposed
storing `baseRefOid` as `base_sha` and having a pre-land check compare it
against the PR's *current* `baseRefOid` — unmoved meaning the review still
stands. But a merge base is a common ancestor, and adding commits to one side of
a common ancestor does not move it: GitHub recomputes `baseRefOid` when the HEAD
branch is pushed, never when the base advances. Measured on this repo rather
than reasoned about: PR #87 held `baseRefOid = 88643c14` while `main` took ten
commits, REST `.base.sha` agreed, and `git merge-base` against the moved `main`
still answered `88643c14`. The proposed check would have answered "unmoved, the
review still stands" in precisely the case it exists to catch — a staleness
detector whose only possible output is *fresh*.

So both ends are stored, and they answer different questions:

* `review_runs.merge_base` — the commit the reviewed diff was built FROM.
  `gh pr diff` is the three-dot diff, so the seats read `merge_base...head` and
  nothing in the payload named that commit. Free off metadata `panel.py` already
  fetches. It moves only when the PR merges its base in or is rebased, which is
  the *branch* acting.
* `review_runs.base_sha` — the live tip of the base branch at review time: what
  the PR would be merged INTO. The end that moves on its own, and therefore the
  only one a staleness check can rest on. Costs its own lookup, which is why it
  is null on the skip path (that path never reaches the board anyway).

Their disagreement is not itself a defect. `base_sha != merge_base` is the
ordinary state of any PR whose base gained a commit after it forked, so neither
this schema nor the panel treats it as a warning. What the movement MEANS is a
verdict, and the verdict belongs to #96 — which is also where #98's asymmetry
has to be honoured: proving staleness is cheap and proving freshness is not, so
a base that moved without touching the PR's files is "no overlap detected" and
never "the review is current".

**Nullable, and null means NOT RECORDED** — the rule 0017 set and the reason it
set it. Every run before this revision has no base commit because nothing stored
one; a run whose base tip could not be read has none because `gh` would not say,
and `panel.py` puts that in `config_notes` rather than inventing a value. Text
rather than fixed-width for the same reason `head_sha` is: a sha arrives as
whatever `gh` printed, and `_sha_or_none` in the API normalises or refuses it, so
the column never truncates a value into looking like data.

**No index.** Both columns are read BY RUN — the pre-land check has a run in
hand and asks what it was judged against — and the collision query that does
scan across runs joins `review_run_files`, which carries its own by-path index.
An index here would be write cost on every ingest for a lookup nothing performs;
the same argument 0017 records for `review_findings.provenance`.

The revision number and the release number are unrelated counters: this is schema
revision **0018** and it ships in product version **v2.29**.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-16

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("merge_base", sa.Text(), nullable=True))
    op.add_column("review_runs", sa.Column("base_sha", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_runs", "base_sha")
    op.drop_column("review_runs", "merge_base")
