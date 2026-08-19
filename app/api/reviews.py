"""Reviewer-panel stats (v2.10, per-reviewer accounts in v2.11).

``~/.claude/loops/panel.py`` reviews one PR diff with several vendor models at
once and has a master judge rule each deduped finding real or not. That is a
controlled comparison — same diff, same judge, different models — and it was
being discarded every run. ``POST /review`` records it; ``GET /review/stats``
aggregates it into the two answers worth having:

* **which reviewer finds the most real issues** — confirmed counts and, more
  usefully, *solo* counts: findings nobody else on the panel raised.
* **is the higher tier worth it** — precision (confirmed vs dismissed) per
  (reviewer, model, effort), so the same vendor at two tiers competes with itself.

Precision is only counted over **judged** runs. When the judge is skipped the
panel keeps every finding unadjudicated, and scoring those as correct would
flatter whichever reviewer was noisiest that day.

The ingest payload is ``panel.py --json`` as-is plus a small envelope, so the
panel needs no bespoke serialiser and the two can't drift apart.

**v2.11 — what each reviewer said, and which defect it was.** A finding used to
be one title, one detail and a list of reviewer *names*, because the panel
merged before the judge and kept one member's text; "codex and pi both reported
this" was recorded but not what either of them said. ``reported_by`` now carries
each reporter's verbatim account with its own severity and line
(``review_finding_reports``), so merging is additive and severity calibration
against the judge is answerable. Each finding also carries a ``key`` — the
identity of the *defect*, not of the observation — so the same bug seen in run 3
and again in run 7 stays two rows that can be joined (``GET /review/findings``),
which is what makes "was it actually fixed?" a query. The older payload shape
(``reviewers: ["codex", "pi"]``, no key) still records exactly as before.

**v2.15 — rounds, and what a run could not see.** Two runs of a PR were two
unrelated records: nothing said which was the re-review of the other's fix, what
this round found that the last had not, or what stopped the loop. And a run said
only what was *found* — a reviewer handed a prefix of the diff, one that never
ran, and one that had nothing to say all recorded the same zero.

A run now carries its ``round`` and ``cycle``, ``new_findings``, whether it
``stopped`` and with what ``stop_reason``, ``stop_confident`` and — when the stop
was unearned — the ``stop_veto`` reasons; a member carries ``could_not_assess``
(its own declaration) and ``unstructured`` (its reply did not parse) alongside the
panel-measured ``truncated``; a finding carries ``needs_rereview``, per reporter.
Together those make the review reviewable: whether a clean verdict
was *earned* is on the row rather than in a transcript, and a reviewer that says
"I could not assess X" and turns out to be right becomes distinguishable from one
that silently reported clean — see ``GET /review/findings``, which checks each
re-review flag against what the following round of the same cycle actually found,
at file grain — for the run, and for each member that made a declaration
(``rereview_by_reviewer``), which is what makes it a per-REVIEWER measure rather
than one boolean shared between them. Payloads without any of it record exactly as
before, as round 1 with nothing declared: ``could_not_assess`` is NULL for a member that said
nothing and ``[]`` only for one that was asked and had no gap, and the two never
collapse. ``needs_rereview`` is per reporter where the caller sends
``reported_by`` — which ``panel.py`` now does, the merge having moved into the
judge, so a panel run attributes the declaration to the member that made it
rather than to everyone who happened to raise the same finding. The coarser
``rereview_by`` remains for a caller that has only that.

**v2.19 — what each member COST, beside what it found.** A scorecard said what a
seat found and (since v2.13) how long it took, but never what it spent, so the
leaderboard could rank a reviewer top on confirmed findings while it was quietly
the most expensive seat on the panel. A member now carries ``input_tokens``,
``output_tokens``, ``cached_input_tokens``, ``reasoning_tokens`` and ``cost_usd``,
all independently optional and all null-means-*not recorded*: the panel reads
usage back out of a pinned session after the run, so a vendor that states no
figure or a transcript that could not be read loses a number and nothing else.
``GET /review/stats`` sums them per (reviewer, model, effort) and says how much of
the window reported (``token_runs``/``cost_runs``), because a sum over a partly
instrumented window is a real number about part of it. Both counts are out of
``runs`` and not ``ran``: a member that burned tokens and then failed still spent
them, so the sums include it and the coverage counts have to cover the same rows.
Compare them only WITHIN a vendor — different tokenizers, different cache
semantics; duration stays the cross-vendor axis. ``cost_usd`` is stored only
where the vendor states it, never derived from a price table.

**v2.23 — which FILES the PR touched, not just how many lines.** A run recorded
``changed_lines: 2032`` and no paths, so the board could not answer the question
integration cost actually turns on: *which other PRs does landing this one
disturb?* The only paths it held were the ones findings happened to name — a
proxy for the diff, and not the diff. A run now carries ``changed_files`` (the
PR's paths, each with its own additions/deletions), ``changed_files_total`` —
GitHub's own count, kept separate so a list truncated by GitHub's 3,000-file cap
is detectable rather than reading as complete — and the PR's ``pr_state`` /
``is_draft`` as of that panel, so a rival merged last week is distinguishable
from a live one.

This release lands the **datum only**. Reading it back as a collision query is
deliberately not here: two full panel rounds put the same defect in that endpoint
twice — a filter composed in front of the newest-run selection, resurrecting a
stale run under a confident answer — and the second instance was introduced by
the fix for the first. That is a design that wants its own rounds rather than a
third patch, and it has them (#101). What ships is the record: durable, queryable,
and with every "I do not know" kept apart from every "I know it is none".

**v2.26 — did the last fix cause this, or did the last round miss it?** v2.24
taught the panel to answer that and gave the answer nowhere to go. Four fields
went onto ``panel.py --json`` — ``head_sha``, ``unread_files``,
``provenance_counts`` and a per-finding ``provenance`` — and this model is
declared ``populate_by_name=True`` with no ``extra=``, so pydantic v2's default
``extra="ignore"`` applied: ``qb record-review`` POSTed all four, ingest dropped
all four, and nothing anywhere reported it (#93). The measurement's stated
destination was the ``/panel`` leaderboard, so the half that was not built was
the half the whole thing was for.

All four now land. The per-finding one matters most: it is the only one that
could never be reconstructed afterwards from anything the board keeps, so every
round that ran while it was being dropped is simply gone. ``GET /review/stats``
grows the axis #48 was filed for — per (reviewer, model, effort), how many of the
defects that member found were *introduced* by the previous fix pass against how
many had been sitting there all along, which are different competencies wanting
opposite remedies — plus ``by_provenance`` for the same split across the window,
where two seats agreeing on one finding counts once.

Two rules the ingest side holds to, both of them the reason this release exists:

* **Null is *not recorded*, never "no provenance".** A pre-v2.26 run has none
  because nothing stored it; a round 1 has none because the question does not
  arise; ``"unknown"`` is a real bucket for a finding that WAS asked about and
  could not be placed. Three states, kept apart end to end — which is why
  ``provenance_counts`` is stored as the panel sent it, ``{}`` included, rather
  than derived from the findings.
* **A dropped field says so.** An unrecognised bucket normalises to null (the
  ``pr_state`` rule) and is named back in the response as ``provenance_unknown``,
  because shipping a quieter version of #93 as the fix for #93 would be a poor
  joke. It is the machine-readable half of the drift check #65 asks for. Every
  other drop on this path is reported the same way — ``head_sha_dropped`` for a
  commit id that could not be one, ``unread_files_dropped`` for paths over the cap
  or unreadable, ``provenance_counts_unusable`` for a known bucket carrying a
  count that cannot be believed, ``unreadable_fields`` for a value that was not
  the shape its field takes at all — and each of them is also logged, because a
  response nobody stores is not a record. ``qb record-review`` prints the run id
  and nothing else.

Where the four fields are READ, and why they are not all in the same places:
``head_sha`` and ``provenance_counts`` ride every view — one string and at most
four integers. The unread PATHS are on ``GET /review/{id}`` only, exactly where
``changed_files`` lives; the list views carry ``unread_files_count`` instead,
which still separates "measured, nothing cut" (0) from "never measured" (null)
without letting one page of runs serialise a few million path strings.

**v2.29 — the other end of the range, and why it is two fields.** v2.26 recorded
which commit a round read and left what it was judged AGAINST as a branch name.
#98 proposed closing that with GitHub's ``baseRefOid``, compared later against
the PR's current ``baseRefOid``. That comparison cannot fire: ``baseRefOid`` is
the **merge base**, recomputed when the head branch is pushed and never when the
base branch advances, because a common ancestor is not moved by commits added to
one side of it. PR #87 held ``88643c14`` across ten commits of ``main``, REST
``.base.sha`` agreed, and ``git merge-base`` against the moved ``main`` still
answered ``88643c14``. A check written that way reports "the review still
stands" exactly when the base has run away underneath it.

So ``merge_base`` and ``base_sha`` are separate columns and mean different
things: the first is the PR's own base commit (``gh pr diff`` is the three-dot
diff, so a whole-PR round reads ``merge_base...head_sha``), the second is the
base branch's tip at review time — the end that moves on its own.

``merge_base`` is the PR's anchor and not always the ROUND's: under v2.28's
increment scope a later round's target is ``since_sha...head_sha``, and
``merge_base`` is where that round's tier-2 context is measured from. A consumer
assembling "what did this round read" reads ``scope`` first, exactly as one
comparing ``diff_chars`` across rounds already has to. Neither is
derived from the other, and a run that could not read one stores null there
rather than the other one standing in.

This release stamps and publishes them and deliberately draws no conclusion from
them. Whether a moved base makes a review stale is #96's verdict, and #98 states
the asymmetry it has to keep: proving staleness is cheap, proving freshness is
not, so a base that moved without touching the PR's files is "no overlap
detected" and never "the review is current".

**v2.37 — what happened to the finding, which the judge cannot know.** A
finding's life ended at the judge. ``verdict`` is ``confirmed | dismissed |
unjudged | sonar``, set once, at review time, by a master model with no more
access to the answer than the reviewer it is ruling on — and ``GET
/review/stats`` then ranked reviewers on it. The whole feedback loop closed
before anybody had tried to act on the finding, so the leaderboard was fed
confidence and called it correctness.

Measured, not supposed: on PR #64 **three of six judge-confirmed P2s were plainly
wrong** — ``install -m 0755 bin/*`` globs, ``CLAUDE_CODE_SESSION_ID`` is exported
by every session in this repo, and ``sed -n '4,34p'`` already ends on the last
help line, so the suggested "fix" would have printed the COLORS section into
``--help``. All three were conditionals from a reviewer that had declared it
could not assess the condition, in a round that was a panel of one (#68). They
are still in the board as confirmed. The same day produced the opposite case —
#32 r2's "``output_tokens_details.thinking_tokens`` is not a shape Claude's usage
object has", refuted by a transcript carrying it in all 801 assistant usage
blocks — and that refutation is recorded nowhere.

``POST /review/outcomes`` records the terminal state whoever ACTED on the finding
puts on it: ``fixed | refuted | deferred | superseded``. ``refuted`` is what pays
for the release and is the cheapest to capture, because the refutation is already
being written in the PR comment and the fix commit's message, in prose that
nothing can count. ``deferred`` gives the parked backlogs a state instead of a
markdown list; ``superseded`` is what a later round marks a finding as when it
re-derives it.

Three properties hold the thing up:

* **Per DEFECT, in its own table.** One row per (repo, pr, ``finding_key``),
  joined to every round that raised it. A column on ``review_findings`` would fan
  one refutation across however many rounds happened to raise the defect, and
  round count correlates with exactly the long fix loops this measures. It also
  keeps a round's record immutable: what a round said is a fact about that round.
* **The judge's verdict and the outcome never merge.** They are allowed to
  disagree — a ``confirmed`` finding with a ``refuted`` outcome is the case the
  issue was filed for — so ``GET /review/findings`` shows ``status`` (what the
  reviews support) beside ``outcome`` (what somebody found out), and neither is
  folded into the other.
* **The self-grading guard is published, not pretended, and ``attested_by`` is a
  CLAIM.** #77 says an agent must not mark its own findings ``refuted``
  unattended, and this API cannot tell a fixer from a reviewer: the reviewer is a
  model name, the caller is a board identity. ``set_by`` comes from the token and
  is proof; ``attested_by`` is free text from the same request that carried the
  refutation, so it records that the caller says a human agreed — the board
  cannot authenticate a person. Every publication says so: the response splits
  ``unattested_refutations`` out, the stats carry ``outcome_attested`` beside the
  raw counts, and ``/panel`` renders the claim with its claimant. An unattended
  refutation on the record beats one in a PR comment nothing counts; what neither
  must be is counted silently.
* **Every edit to a recorded outcome is visible.** A different outcome bumps
  ``revisions`` and keeps ``prior_outcome``; a repeat FILLS an empty field and
  never silently rewrites a stored one — overwriting the note that is the evidence
  for a refutation is itself a revision, and comes back in ``amended`` naming the
  fields. An explicitly-null field clears, which is how a mistaken attestation is
  retracted without flipping the outcome twice to do it.

``GET /review/stats`` grows ``precision_after`` per (reviewer, model, effort) —
``fixed / (fixed + refuted)``, the same ratio as ``precision`` but scored against
the code — plus ``outcome``, ``outcome_attested``, ``outcomes_recorded``,
``outcomes_scored`` (the population the ratio is actually over, which is NOT
``outcomes_recorded``) and ``confirmed_defects``, with ``by_outcome`` and
``by_outcome_attested`` for the window. The read paths carry the outcome's own
``set_by``/``session`` pair beside it, so a reader can reach whoever recorded a
refutation they disagree with. **The gap between
``precision`` and ``precision_after`` is the number the panel exists to produce
and could not.** These are the only counts on that page measured per defect
rather than per observation, which is why both denominators are published.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import and_ as sa_and
from sqlalchemy import case, func, select, tuple_
from sqlalchemy import or_ as sa_or
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.auth import identify, reader
from app.db import get_session
from app.identity import agent_row, compose, machine_of
from app.models.review import (
    ReviewFinding,
    ReviewFindingOutcome,
    ReviewFindingReport,
    ReviewReviewer,
    ReviewRun,
    ReviewRunFile,
)

router = APIRouter(tags=["review"])

#: A child of the logger ``app.main`` configures, so a drop recorded here reaches
#: the same handler as the rest of the service. This is the DURABLE half of "a
#: dropped field says so": the POST response names the drift to whoever made the
#: request, and ``qb record-review`` prints only the run id, so without a log line
#: the evidence was gone the moment the response was discarded — and #65's drift
#: check, the thing this signal exists to feed, would have had nothing to read.
_log = logging.getLogger("app.review")


async def _authored_as(session: AsyncSession, author: str) -> str:
    """The spelling a run was recorded under, for an ``author=`` filter.

    Runs store the agent's name, so a filter written from a `whoami` alias (the
    key form) has to be translated or it silently matches nothing. Unlike
    addressing, this resolves to the name even for a retired agent: the question
    is "what did it author under", and the answer does not change when it goes.

    Which means a name that has since been recycled attributes both holders'
    runs to one filter. That is a property of storing names in history, not of
    the translation — a filter written with the name directly does the same —
    and telling them apart would need a tenure log this doesn't keep.
    """
    row = await agent_row(session, author)
    return compose(machine_of(author), row.name) if row is not None else author


SEVERITIES = ("P1", "P2", "P3", "P4")

_INT32 = 2_147_483_647


def _line_or_none(v: int | None) -> int | None:
    """A line number the column cannot hold is no line number.

    Recording is best-effort for the panel — a review must never fail because
    the board choked — so a garbled line is dropped rather than costing the run
    its whole record, which is what both a 422 here and the driver's error on
    an out-of-range INTEGER would do.
    """
    return v if v is None or -_INT32 - 1 <= v <= _INT32 else None


def _count_or_none(v: object) -> int | None:
    """A non-negative count the column can hold, or no count at all.

    Same rule as :func:`_line_or_none`, for the same reason: a hand-rolled caller
    that sends ``new_findings: -1`` must not lose its findings, its scorecards and
    its accounts to a 422 over one bad integer. "The panel did not say" is a state
    this column already has, so a value that cannot be believed becomes it.

    A fractional number is one of those. ``int(1.9)`` is 1, and silently changing
    a caller's meaning is a different failure from the documented one — this
    helper's contract is a believable integer COUNT, and 1.9 is not one, so it
    becomes None rather than 1.
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, float) and not v.is_integer():
        return None
    try:
        n = int(v)  # a count spelled as a string is still a count
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= _INT32 else None


#: Numeric(12, 6) — the largest cost the column can hold. A figure beyond it is
#: not a panel run's cost, and rounding it in would poison every sum it joins.
_MAX_COST = Decimal("999999.999999")


def _cost_or_none(v: object) -> Decimal | None:
    """A stated cost, if it is a number the column can hold.

    ``NaN``/``Infinity`` arrive from a vendor that emitted a JSON non-number and
    are refused here rather than at the driver, where they would take the whole
    record down with them.

    Takes ``object`` and coerces, because it runs ``mode="before"``: the value
    arrives exactly as the caller spelled it, so ``"free"`` has to become "no
    cost recorded" here rather than a 422 that loses the whole run. A bool is
    refused for the same reason :func:`_count_or_none` refuses one — ``True`` is
    not a price.
    """
    if v is None or isinstance(v, bool):
        return None
    try:
        cost = v if isinstance(v, Decimal) else Decimal(str(v))
    except (ArithmeticError, ValueError, TypeError):
        return None
    if not cost.is_finite() or cost < 0 or cost > _MAX_COST:
        return None
    return cost


def _phrases(v: object) -> list[str]:
    """A list of phrases, however the caller spelled it — mirroring
    ``panel.py::_str_list``, which tolerates the same shapes on the way IN from a
    model. Anything unusable becomes [] — including a non-string ITEM, which is
    dropped rather than stringified. ``could_not_assess: [{"area": "the
    migration"}]`` used to store the Python repr ``"{'area': 'the migration'}"``,
    which ``/panel`` then printed verbatim as words a reviewer had written.

    Same rule as :func:`_count_or_none`, one type over. The declaration fields
    were the only strictly-typed ones in an ingest path that documents best-effort
    coercion, so ``could_not_assess: "the migration"`` — the exact shape the panel
    normalises away before sending — 422'd a hand-rolled caller's whole run:
    findings, scorecards and accounts lost to one badly-spelled list.
    """
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [s for s in (x.strip() for x in v if isinstance(x, str)) if s]


#: The buckets ``panel.py::_provenance`` sorts a new finding into, spelled
#: exactly as it spells them — this is a shared vocabulary and not a board
#: invention, so the two sides must not paraphrase each other (#65's class).
#: ``unknown`` is a real answer and not a failure: it is what an unreadable fix
#: range or an unplaceable finding honestly leaves, and it is emphatically NOT
#: what a finding nobody attributed carries. That one is NULL.
PROVENANCE = ("introduced", "missed", "missed-unread", "unknown")

#: How much of an unrecognised bucket name is echoed back to the sender. Long
#: enough to name the drift, short enough that a caller cannot use the response
#: as a mirror for arbitrary text. A name cut to this length is echoed with a
#: trailing ``…`` so a reader is never told a truncated name is the whole one.
MAX_BUCKET_ECHO = 64

#: C0 and C1 control characters, which no bucket name, path or commit id contains
#: and which an echoed value must never carry into a log line. Newline and
#: carriage return forge log entries; the C1 range covers the single-byte ANSI
#: escapes as well as ESC itself. See :func:`_echo`.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

#: How many distinct unrecognised names one response will list. ``MAX_BUCKET_ECHO``
#: bounds each name and nothing bounded the COUNT, so a tally with ten thousand
#: junk keys — or a payload whose findings each spell a different one — was sorted
#: and serialised in full. Drift is a handful of names in practice; past this the
#: response says how many there were instead of naming them all.
MAX_UNKNOWN_BUCKETS = 25

#: Bucket -> the :class:`ReviewReviewer` counter it feeds.
#:
#: Written out rather than derived by string transform. The derivation
#: (``"prov_" + b.replace("-", "_")``) read as the safer choice — a bucket could
#: not be added to :data:`PROVENANCE` and silently miss the leaderboard — but it
#: bought that by ASSUMING the column exists, and migration 0017 says in as many
#: words that this vocabulary grows when #41 makes attribution exact. Adding a
#: word here would then have raised ``AttributeError`` at request time, on
#: ``GET /review/stats`` and on the whole ``POST /review`` ingest path, because
#: :func:`_scorecards` splats the tally straight into the ORM object.
#:
#: The check below is what actually buys the safety, and it fires at import
#: rather than under a request, with the missing column named.
PROVENANCE_COUNTER = {
    "introduced": "prov_introduced",
    "missed": "prov_missed",
    "missed-unread": "prov_missed_unread",
    "unknown": "prov_unknown",
}

if set(PROVENANCE_COUNTER) != set(PROVENANCE):  # pragma: no cover - import guard
    raise RuntimeError(
        "PROVENANCE_COUNTER does not cover PROVENANCE: "
        f"{sorted(set(PROVENANCE) ^ set(PROVENANCE_COUNTER))}"
    )
_no_column = [c for c in PROVENANCE_COUNTER.values() if not hasattr(ReviewReviewer, c)]
if _no_column:  # pragma: no cover - import guard
    raise RuntimeError(
        f"PROVENANCE_COUNTER names columns review_reviewers does not have: {_no_column}"
        " — add them in a migration and on the model before adding the bucket."
    )
del _no_column


#: What happened to a defect after the judge ruled on it (v2.37). Set by whoever
#: ACTED on the finding — the fixer, or a human — and never by the judge, which
#: has already had its say and had no more access to the answer than the reviewer
#: it was ruling on.
#:
#: ``refuted`` is the one that pays for the feature: it is the only value here
#: that contradicts the judge, and it is the cheapest to capture, because the
#: refutation is already being written in the PR comment and the fix commit.
#: ``deferred`` gives the parked backlogs (#66, #69, #72, #74 and the ones after
#: them) a state instead of a markdown list. ``superseded`` is what a later round
#: marks a finding as when it re-derives it.
OUTCOMES = ("fixed", "refuted", "deferred", "superseded")

#: The two outcomes that are a judgement about whether the finding was RIGHT, and
#: therefore the only two in the precision-after-the-fact ratio. ``deferred`` and
#: ``superseded`` are decisions about what to do next and say nothing about
#: correctness — counting either as a success would make "we did not get to it"
#: read as "it was real", which is the direction that flatters.
OUTCOMES_SCORED = ("fixed", "refuted")

#: The longest note this endpoint stores. Long enough for the refutation itself —
#: which is the point, since a bare ``refuted`` flag is exactly the confident
#: assertion with nothing behind it that this feature exists to measure — and
#: bounded because an authenticated sender is not a bounded one.
MAX_NOTE_CHARS = 4000

#: Bounds on the single-line fields beside it: a board identity, an issue ref, a
#: defect key. Generous for all three and far short of a text dump.
MAX_REF_CHARS = 200

#: The vocabulary is stated twice — here, and as a SQL CHECK on the table, which
#: cannot import this tuple. (Three times counting migration 0020, and that one
#: is a frozen snapshot on purpose: a migration that imported a live constant
#: would replay differently after the constant moved, which is the one thing a
#: migration may not do.) So the two that CAN be compared are, at import, with the
#: mismatch named — the same guard :data:`PROVENANCE_COUNTER` gets, and for the
#: same reason: adding a fifth outcome here and not there fails at the database
#: on the first insert, which is a long way from where the edit was made.
_VOCAB_CONSTRAINT = "ck_review_finding_outcomes_vocabulary"
_declared = {
    c for con in ReviewFindingOutcome.__table__.constraints
    if getattr(con, "name", None) == _VOCAB_CONSTRAINT
    # Deliberately wider than today's vocabulary: a future `not_applicable` or
    # `wont-fix` added correctly to BOTH sides would make a narrower pattern match
    # nothing, and a guard that matches nothing reports a mismatch against the
    # empty set — failing at import on a correct change, which is how a guard gets
    # deleted rather than fixed.
    for c in re.findall(r"'([a-z][a-z0-9_-]*)'", str(con.sqltext))
}
if _declared != set(OUTCOMES):  # pragma: no cover - import guard
    raise RuntimeError(
        f"OUTCOMES and the {_VOCAB_CONSTRAINT} CHECK disagree: "
        f"{sorted(_declared ^ set(OUTCOMES))} — a value in one and not the other is "
        "either rejected at ingest or refused by the database on insert."
    )
del _declared

#: How many outcomes one request may carry. A fix pass clears a round's findings
#: in one call — a round is tens of findings, not thousands — and this is the same
#: "one request should not insert a million rows in one transaction" bound
#: ``MAX_CHANGED_FILES`` sets on the ingest path. Entries past it are NAMED rather
#: than 422'ing the batch, so a caller that batched a long loop keeps the first
#: 500 rows.
MAX_OUTCOMES = 500

#: ...and a hard ceiling above it, where the request is refused outright.
#: "Name the overflow" is right for a caller that sent slightly too many and
#: wrong for one that sent 100,000: pydantic materialises the whole array either
#: way, and the naming itself then costs a rejection dict per entry and one
#: enormous log line. Ten times the cap is comfortably past any real fix pass and
#: far short of a body that hurts.
MAX_OUTCOMES_ACCEPTED = MAX_OUTCOMES * 10

#: How many of one item's validation errors the rejection reason quotes before it
#: says how many more there were.
_MAX_ITEM_ERRORS = 4

#: How many rejections one log line carries. The response names every one — that
#: is the contract — but the LOG is a shared resource, and a 5,000-item batch of
#: junk would otherwise write a megabyte of JSON into it.
MAX_REJECTIONS_LOGGED = 50

#: Postgres' SQLSTATE for a unique-constraint violation. The ONE integrity error
#: on the outcome path that means "somebody else got there first" — every other
#: one (a CHECK, a NOT NULL) is deterministic, and retrying it builds the same
#: invalid row again while reporting a bug in this service as contention.
_PG_UNIQUE_VIOLATION = "23505"


def _bucket_or_none(v: object) -> str | None:
    """One of :data:`PROVENANCE`, or nothing.

    The :meth:`ReviewIn._state` rule, one field over and for the same reason: a
    value a consumer *filters on* must never be stored verbatim when it is not
    one of the values that consumer knows. ``!= "introduced"`` would silently
    reclassify a typo, and it does so in the direction that hides the signal.

    What is different here is that ``None`` is not the end of it. A dropped
    bucket is exactly the panel↔board drift #93 was filed about, so
    :func:`record_review` reports every unrecognised name back in its response
    rather than swallowing it — see ``provenance_unknown`` there.
    """
    if not isinstance(v, str):
        return None
    s = v.strip().lower()
    return s if s in PROVENANCE else None


def _echo(v: object) -> str:
    """What the sender spelled, bounded, for a drop signal to name it back.

    Stripped — the same normalisation :func:`_bucket_or_none` applies before
    testing membership, so ``"  regressed  "`` is rejected and reported under one
    spelling rather than two. Both echo paths (a finding's ``provenance`` and a
    tally's keys) go through here for exactly that reason: they merge into one
    set in :func:`record_review`, and two spellings of one drift defeat the dedup
    the set is there for.

    A non-string is echoed as its ``str()``. ``provenance: 5`` and
    ``provenance: ["missed"]`` used to vanish leaving nothing at all, which reads
    as a finding nobody asked about rather than as a producer sending the wrong
    shape — and a type-confused sender is the more likely drift of the two.

    Marked with ``…`` when it was cut, so a truncated name is never handed to a
    reader as a whole one. Two names agreeing in their first
    :data:`MAX_BUCKET_ECHO` characters still report once, marked; that is a
    bounded echo doing its job, not a lost signal.

    **Control characters are replaced, not merely trimmed**, and that is the
    security half rather than tidiness. This value is caller-supplied and it
    reaches the service log, so a bare ``.strip()`` — which only touches the ends
    — let an embedded newline forge a whole extra log line: a sender posting
    ``provenance: "x\\nreview ingest dropped fields: run=1 repo=…"`` writes its own
    entry into the log #65's drift check is meant to read, which is a signal
    corrupting the exact record it exists to leave. ANSI escapes are the same
    class one step down, against whoever reads the log in a terminal. Replaced
    with ``␦`` rather than deleted: something unprintable arrived, and a reader
    of a drift signal should see that it did.
    """
    s = v.strip() if isinstance(v, str) else str(v)
    s = _CONTROL_RE.sub("␦", s)
    return s[:MAX_BUCKET_ECHO] + "…" if len(s) > MAX_BUCKET_ECHO else s


#: A commit id: sha-1's 40 hex characters or sha-256's 64, and nothing between.
#:
#: Abbreviations are refused deliberately. ``[0-9a-f]{7,64}`` also matched every
#: 7+ digit DECIMAL string, so a caller that sent a PR number, a timestamp or a
#: run id had it stored as a commit — looking like data at exactly the moment the
#: column's purpose (resolving it against the repo) fails. Requiring an ``a-f``
#: character instead would reject the roughly one in twenty-seven legitimate
#: 7-character abbreviations that happen to be all digits, which is a worse cure
#: than the disease. The full lengths cost nothing real:
#: ``panel.py`` sends ``meta["headRefOid"]`` and ``_head_sha_now`` sends the same,
#: both full oids, and an abbreviation could not be resolved without the repo
#: that minted it anyway.
_SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _sha_or_none(v: object) -> str | None:
    """A commit id, or no commit id at all.

    Same trade as :func:`_line_or_none`: recording is best-effort, so a garbled
    head is dropped rather than costing the run its findings. Not stored verbatim,
    because this column's whole purpose is to be *resolved* later — against the
    repo, against the next round's baseline, against #98's base end — and a value
    that cannot be a commit id would fail that lookup while looking like data.
    NULL already means "the panel did not say", which is the honest reading.

    The drop is reported: :meth:`ReviewIn._count_files_sent` keeps what arrived
    and :func:`record_review` names it back as ``head_sha_dropped``. A run that
    was sent ``"HEAD"`` must not be indistinguishable from one sent nothing —
    that silence is #93 in miniature.
    """
    if not isinstance(v, str):
        return None
    s = v.strip().lower()
    return s if _SHA_RE.match(s) else None


def _same_file(a: str, b: str) -> bool:
    """Do two path spellings name the same file? Equal, or one a path-suffix of
    the other (``reviews.py`` vs ``app/api/reviews.py``) — but never two distinct
    paths that merely end in the same basename.

    The same rule as ``panel.py::_same_file``, and it has to be: reviewers spell
    paths differently and the judge takes whichever spelling it likes per round,
    so scoring a re-review declaration on exact equality marked an honest flag on
    ``reviews.py`` a miss against the next round's ``app/api/reviews.py``. That
    error only ever runs one way — against the reviewer — which is the direction a
    published honesty measure must not be biased in."""
    if a == b:
        return True
    return a.endswith("/" + b) or b.endswith("/" + a)


# ----------------------------------------------------------------- ingest models

class ReportIn(BaseModel):
    """One reviewer's own account of a finding, before the judge merged it.

    ``severity``/``line`` are that reviewer's, not the judge's: the difference is
    the calibration signal, so they are stored rather than reconciled.
    """

    model_config = ConfigDict(populate_by_name=True)

    reviewer: str
    severity: str | None = None
    line: int | None = None
    #: Verbatim. ``detail`` is accepted as an alias because that is what the
    #: panel calls the same text on an unmerged finding.
    account: str = Field(default="", validation_alias=AliasChoices("account", "detail"))
    #: This reviewer declared the FIX for this finding needs re-reading. Omitting
    #: the key is not a declaration either way — only an explicit ``false`` says
    #: "no", and only that overrides the finding-level ``rereview_by``.
    needs_rereview: bool = False

    @field_validator("reviewer")
    @classmethod
    def _trim(cls, v: str) -> str:
        return v.strip()

    @field_validator("line")
    @classmethod
    def _line(cls, v: int | None) -> int | None:
        return _line_or_none(v)


class FindingIn(BaseModel):
    """One merged finding, exactly as ``panel.py --json`` serialises it.

    The aliases follow the same rule as :class:`ReviewIn`'s — take the panel's
    words rather than making it translate. The judge's merged statement is
    ``synthesis`` in its canonical shape and ``title`` in the older one; both
    land here, because a renamed field would otherwise fail silently into a null
    column rather than erroring.
    """

    model_config = ConfigDict(populate_by_name=True)

    severity: str = "P3"
    file: str | None = None
    line: int | None = None
    title: str = Field(default="", validation_alias=AliasChoices("title", "synthesis"))
    detail: str = ""
    reviewers: list[str] = Field(default_factory=list)
    reason: str = Field(default="", validation_alias=AliasChoices("reason", "rationale"))

    #: Per-reviewer accounts. Supersedes ``reviewers`` (the names are implied by
    #: it) but does not replace it: a panel may list a member that contributed no
    #: text, and every older panel sends names only.
    #:
    #: ``panel.py`` sends this: merging happens in its judge, which writes a new
    #: synthesis and keeps every member's own severity, line, title and detail
    #: beside it. So a panel run arrives at the finest attribution, and the
    #: calibration counters — which need a reporter's own severity — are fed.
    #: An older payload with names only still records; ``rereview_by`` below is
    #: the coarser grain that remains for it.
    reported_by: list[ReportIn] = Field(default_factory=list)

    #: The panel's id for this finding *within this run* (e.g. ``"1609-F03"``).
    #: Used only to resolve ``related`` into finding keys — never as the defect's
    #: identity, because the numbering restarts every run.
    id: str | None = None
    #: A stable identity for the defect, if the caller has one. Wins over the
    #: derived key, which is a best-effort fallback (see :func:`_derive_key`).
    key: str | None = None
    #: ``id``s of other findings in this payload that share a cause.
    related: list[str] = Field(default_factory=list)

    #: A reporter declared that fixing this takes a structural change whose result
    #: should be re-read. ``rereview_by`` names which members said so, for a caller
    #: that merges before it can send per-reporter accounts; where ``reported_by``
    #: carries its own flags those win, being the finer grain. A reporter row that
    #: OMITS ``needs_rereview`` has declared nothing, so this still speaks for it.
    needs_rereview: bool = False
    rereview_by: list[str] = Field(default_factory=list)
    #: No earlier round of this PR raised this. The panel computes it against the
    #: baseline it was given; None means it was not asked to.
    new_this_round: bool | None = None
    #: Did the last fix pass INTRODUCE this, or did the last round MISS it? One of
    #: :data:`PROVENANCE`. None means the question does not arise — outside a
    #: cycle, in a round 1, or for a defect an earlier round already raised — and
    #: is a different statement from the ``"unknown"`` bucket, which says it was
    #: asked and could not be placed. This rides beside ``new_this_round`` because
    #: it is the same kind of fact: about this run's comparison against a
    #: baseline, not about the defect.
    provenance: str | None = None
    #: What the caller actually spelled, when that was not a bucket this board
    #: knows. Set by the validator, never by the sender — it exists so
    #: :func:`record_review` can report the drift instead of dropping it in
    #: silence, which is the failure this whole field arrived to fix.
    #:
    #: ``None`` means the key was absent — nobody said. ``""`` means a blank or
    #: all-whitespace string arrived, which IS a statement, just not a usable one:
    #: the response filter therefore tests ``is not None`` rather than
    #: truthiness, or a producer sending empty buckets would look like one asking
    #: nothing.
    provenance_sent: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _keep_provenance_sent(cls, v: object) -> object:
        """Set unconditionally, so a caller cannot supply it: it is evidence about
        what arrived, and evidence the sender can write is not evidence.

        Every mapping is rewritten, so a caller spelling ``provenance_sent``
        itself has it overwritten rather than merged. The only other input
        pydantic will accept here is an instance of this model — the class sets no
        ``from_attributes``, so an arbitrary attribute-carrying object raises
        rather than passing through — and an instance carries a value this same
        validator derived. The claim above is therefore unconditional over every
        construction path: ``model_validate`` of a mapping, ``FindingIn(**kw)``
        (pydantic routes keyword construction through this validator too), and
        revalidation of an instance.
        """
        if not isinstance(v, Mapping):
            return v
        # `_echo`, not a bare strip: a non-string `provenance` (`5`,
        # `["missed"]`) used to leave nothing here at all, so a type-confused
        # producer read as a finding nobody asked about. See :func:`_echo`.
        raw = v.get("provenance")
        return {**v, "provenance_sent": None if raw is None else _echo(raw)}

    @field_validator("provenance", mode="before")
    @classmethod
    def _provenance(cls, v: object) -> str | None:
        return _bucket_or_none(v)

    @field_validator("line")
    @classmethod
    def _line(cls, v: int | None) -> int | None:
        return _line_or_none(v)


class ReviewerIn(BaseModel):
    """A panel member as configured for this run — its brain, what it declared
    about its own coverage, and what it cost.

    The cost fields are all optional and independently so: the panel reads usage
    back out of a pinned session after the run, and a vendor that states no
    figure, or a transcript that could not be read, simply sends nothing. Every
    one of them stays null rather than defaulting to 0 — "not recorded" and
    "spent nothing" are different claims and only one of them is ever true.
    """

    model: str | None = None
    effort: str | None = None
    ran: bool = True
    skip: str | None = None
    max_diff_chars: int | None = None
    truncated: bool | None = None
    duration_ms: int | None = None
    #: Areas it could not judge. None = no structured declaration was obtained —
    #: not asked (every panel before v2.15), answered in the old bare-array shape,
    #: or its reply did not parse (``unstructured``); [] = asked, and it had
    #: nothing to declare. The two must not collapse, or a reviewer that was never
    #: given the chance to say reads as one that had nothing to say.
    could_not_assess: list[str] | None = None
    #: Its reply carried no JSON and was kept as one raw finding. That is why some
    #: members land on ``could_not_assess: None`` having very much been asked, and
    #: it is a coverage failure in its own right — the panel already vetoes a stop
    #: over it, so the board must be able to see it too. None = the panel didn't say.
    unstructured: bool | None = None
    #: This box does not carry the reviewer's CLI. The panel has sent this since
    #: v2.32 and ingest dropped it, because this model declares
    #: ``populate_by_name=True`` with no ``extra=`` and pydantic's default is
    #: ``extra="ignore"`` — the exact silent-drop this file's v2.26 note is about
    #: (#93). Landed now with the two below it.
    absent: bool | None = None
    #: This member reviewed from the diff alone — it could not read the code under
    #: review (#113). The most important confound in the reviewer table: a seat that
    #: could open the caller and one that could not are not comparable on findings
    #: or on ``could_not_assess``, and a leaderboard that ranks them together is
    #: measuring two different jobs.
    code_blind: bool | None = None
    #: This member's ``truncated`` was the kernel's doing rather than a budget's
    #: (#113) — its prompt travels in argv and one element is capped. Kept apart
    #: from ``truncated`` because the two have opposite remedies.
    argv_capped: bool | None = None

    #: EVERY prompt-side token, cache hits included. Vendors disagree about this
    #: — Claude's own `input_tokens` is the uncached remainder and pi reports
    #: cache reads beside input rather than inside it — so the panel normalises
    #: before sending, and these two fields are what the board then means by it.
    input_tokens: int | None = None
    output_tokens: int | None = None
    #: The cached slice OF ``input_tokens``, never a sibling to be added to it.
    cached_input_tokens: int | None = None
    #: Thinking tokens, which every vendor counts INSIDE ``output_tokens``. Kept
    #: separately for visibility and never added on top, or the seats that think
    #: would be charged for it twice.
    reasoning_tokens: int | None = None
    #: Only when the **vendor states it**. Never a price-table derivation — see
    #: the column's docstring.
    cost_usd: Decimal | None = None

    @field_validator("could_not_assess", mode="before")
    @classmethod
    def _gaps(cls, v: object) -> list[str] | None:
        """Coerced, not rejected (see :func:`_phrases`). A shape that is not a
        declaration at all lands on None — "no structured declaration was
        obtained", which is what this field's None already means — rather than on
        [], which would say the member was asked and had no gap."""
        if v is None or not isinstance(v, (str, list)):
            return None
        return _phrases(v)

    # `mode="before"`, like every other tolerant validator here, and for the
    # reason the helpers exist: without it pydantic coerces against
    # `int | None` / `Decimal | None` FIRST, so a malformed telemetry number
    # 422s before the helper written to absorb it is ever called. Every
    # tolerance `_count_or_none` documents — a bool, a non-integral float, a
    # count spelled as a string — was unreachable. The out-of-range case
    # survived only because Python ints are unbounded, which is exactly why the
    # range test passed and the type gap stayed hidden. One unreadable number
    # must not cost a caller its findings, scorecards and accounts.
    @field_validator("duration_ms", "input_tokens", "output_tokens",
                     "cached_input_tokens", "reasoning_tokens", mode="before")
    @classmethod
    def _count(cls, v: object) -> int | None:
        return _count_or_none(v)

    @field_validator("cost_usd", mode="before")
    @classmethod
    def _cost(cls, v: object) -> Decimal | None:
        return _cost_or_none(v)


class StopIn(BaseModel):
    """The panel's mechanical verdict on whether the loop should go again."""

    #: None when the caller nested a ``round_stop`` but did not say — the same
    #: rule the flat ``stop_reason`` path follows, and the same one ``confident``
    #: follows here. Defaulting to True recorded a running cycle as finished on
    #: the strength of a payload that only carried a reason and a veto list.
    stop: bool | None = None
    reason: str = ""
    #: Whether stopping was convergence. False when the round was capped, or a
    #: reviewer was truncated / absent / unparsed / declaring a gap — the cases
    #: where "no new findings" is a fact about the panel, not about the code.
    confident: bool = False
    veto: list[str] = Field(default_factory=list)

    @field_validator("veto", mode="before")
    @classmethod
    def _veto(cls, v: object) -> list[str]:
        """Coerced, not rejected — ``veto: "capped"`` from a hand-rolled caller
        must not cost it the whole run (see :func:`_phrases`)."""
        return _phrases(v)


#: A path longer than this is not a path. Bounded because ``ReviewRunFile.path``
#: is ``Text`` and the sender is trusted only in the sense of being authenticated.
MAX_PATH_CHARS = 4096
#: GitHub caps a PR's file list at 3,000; this leaves room above it and still
#: bounds one request to something a single transaction can hold. A list longer
#: than this is truncated with a note, never silently — see :class:`ReviewIn`.
MAX_CHANGED_FILES = 5000
#: The states GitHub reports for a PR. Anything else is recorded as NULL rather
#: than verbatim — see :meth:`ReviewIn._state`.
PR_STATES = frozenset({"OPEN", "MERGED", "CLOSED"})


def _unread_paths(v: object) -> tuple[list[str] | None, int]:
    """``unread_files`` as paths, with the count of entries that were not paths.

    One function for both answers because they must agree: the validator stores
    the list and :meth:`ReviewIn._count_files_sent` reports the loss, and two
    implementations of "was this entry usable" would eventually disagree about a
    payload nobody looked at twice.

    The three results, and the reason each is a different state:

    * ``(None, n)`` — not a declaration. A shape that is not a list or a string
      at all, and also a **non-empty input from which no path could be read**:
      ``[{"path": "a.py"}, 7, None]`` is a garbled declaration, and returning
      ``[]`` for it would store the round's positive statement "coverage was
      measured and nothing was cut" on the strength of a value that says the
      opposite. That collapse is the exact one this release exists to prevent,
      and it was in the release's own ingest path.
    * ``([], 0)`` — the caller sent ``[]``: measured, nothing cut.
    * ``([...], n)`` — paths, stripped, deduped in first-mention order.

    The count is what ``unread_files_dropped.unusable`` reports, and it is kept in
    step with ``unread_files_sent`` so the arithmetic closes: what was stored, plus
    what was unusable, plus what the cap cut, is what arrived.

    An entry counts as unusable when something was there and no path could be read
    from it: a non-string, a blank, or a path over :data:`MAX_PATH_CHARS`. The
    first two are exactly what ``changed_files`` counts under
    ``changed_files_dropped.unusable``, deliberately — two path lists one function
    apart must not disagree about what counts as a loss.

    An over-long path is DROPPED rather than truncated, which is where this parts
    company with ``changed_files``' own path bound. This list is matched against
    the next round's diff by exact path, so a truncated entry is not a shortened
    path, it is a different file: the ``missed-unread`` bucket it feeds would
    silently never match, and the response would have said nothing was lost. "A
    path longer than this is not a path", as :data:`MAX_PATH_CHARS` already says.

    A REPEATED entry is folded and not counted. Nothing went missing, so firing a
    drop signal over it teaches its reader to ignore the signal — the same silence
    ``changed_files`` keeps over its own dedup.

    The cap is applied to the RAW list, before coercion, exactly as
    ``changed_files`` applies its own — and that ordering is load-bearing rather
    than incidental. ``record_review`` reports the truncation as ``sent - cap``,
    so capping after the dedup would make that arithmetic lie: 5,100 entries
    holding 200 duplicates fit under the cap with nothing lost, and the response
    would still have announced 100 missing paths.
    """
    if v is None or not isinstance(v, (str, list)):
        return None, 0
    # A bare string is one path: `panel.py::_str_list` tolerates that shape on the
    # way in and this module mirrors it. A blank one is no declaration rather than
    # a lost path — there is no entry there to have lost — which keeps the
    # accounting closed: what was stored, plus `unusable`, plus what the cap cut,
    # is what `unread_files_sent` says arrived.
    if isinstance(v, str) and not v.strip():
        return None, 0
    raw = [v] if isinstance(v, str) else v[:MAX_CHANGED_FILES]
    paths: list[str] = []
    unusable = 0
    for item in raw:
        if not isinstance(item, str):
            unusable += 1
            continue
        p = item.strip()
        if not p or len(p) > MAX_PATH_CHARS:
            unusable += 1
            continue
        paths.append(p)
    if raw and not paths:
        return None, unusable
    return list(dict.fromkeys(paths)), unusable


class ChangedFileIn(BaseModel):
    """One path the PR touched, with that path's own share of ``changed_lines``.

    **Every field is coerced, never rejected.** This module's rule is that
    recording is best-effort — a run's findings, scorecards and accounts must not
    be lost over the shape of its file list — and the first version of this model
    only honoured that rule for one shape. A bare string was coerced, and then
    ``additions: -1``, ``path: ""``, ``path: null`` and a numeric entry each 422'd
    the entire payload, findings included. The leniency was applied to shape and
    not to values, which is the same thing as not being lenient.

    So: a bare string becomes a path; a negative or non-numeric churn number
    becomes None ("nobody said") rather than a rejection; anything with no usable
    path at all is dropped by :func:`record_review` rather than refused here.
    ``ReviewIn.changed_files`` accepts ``null`` for the same reason.
    """

    model_config = ConfigDict(populate_by_name=True)

    #: May be empty after coercion — `record_review` drops those rows. Validating
    #: it here would cost the whole run, which is exactly the trade this class
    #: exists not to make.
    path: str = ""
    additions: int | None = None
    deletions: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, v: object) -> object:
        """``"a.py"`` → ``{"path": "a.py"}``, and anything unusable → droppable."""
        if isinstance(v, str):
            return {"path": v}
        if not isinstance(v, dict):
            # A number, a list, a null in the array. Not a file, and not worth a
            # 422 either — it becomes a pathless row and is dropped downstream.
            return {"path": ""}
        return {**v, "path": v.get("path") if isinstance(v.get("path"), str) else ""}

    @field_validator("path", mode="after")
    @classmethod
    def _bound_path(cls, v: str) -> str:
        return v[:MAX_PATH_CHARS]

    @field_validator("additions", "deletions", mode="before")
    @classmethod
    def _churn(cls, v: object) -> int | None:
        """A count, or None. Negative and non-numeric both mean "nobody said" —
        the same NULL the column carries for a caller that sent nothing, because
        a number that cannot be true is not more informative than no number."""
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        except OverflowError:
            # JSON `1e309` parses to `inf`, which clears the isinstance gate and
            # then raises here — escaping the validator and 500-ing the request,
            # in the one model whose whole rule is that a malformed file list must
            # never cost a run its findings.
            return None
        # A non-integral float is "nobody said" rather than a silent truncation to
        # 3 — GitHub only ever sends integers, so a fractional churn count is a
        # value this cannot represent, and every other unrepresentable value here
        # becomes None rather than quietly changing.
        if isinstance(v, float) and n != v:
            return None
        return n if n >= 0 else None


class CodeAccessIn(BaseModel):
    """Whether this round's seats could read the code under review (#113).

    A nested object rather than three flat fields because the panel sends it as
    one, and because the three answer one question at different grains: what was
    ASKED for (`setting`), who actually got it (`seats`), and what had to be taken
    out of the tree first (`convention_files_removed`).

    ``seats`` is accepted and deliberately NOT stored: it is exactly the set of
    reviewers whose ``code_blind`` is False, so a column would be a second copy of
    a fact already on those rows — free to disagree with them, and the reviewer
    rows are the ones a stats query joins. Accepting it keeps the payload
    round-trippable without inventing a second source of truth."""

    model_config = ConfigDict(populate_by_name=True)

    #: What the repo (or `--no-code-access`) asked for. None = the panel didn't say.
    setting: bool | None = None
    #: Who actually got it. Recorded on the reviewer rows, not here — see above.
    seats: list[str] | None = None
    #: Vendor instruction files removed before any CLI started. ``[]`` = a tree was
    #: built and carried none; None = no tree was built.
    convention_files_removed: list[str] | None = None


class ReviewIn(BaseModel):
    """The panel's ``--json`` payload, accepted verbatim.

    The aliases exist so the panel needs no bespoke serialiser for the board: it
    calls the repo's GitHub slug ``github`` (``repo`` there is the *local*
    checkout name) and the PR's subject ``title``. Taking its words rather than
    making it translate is what keeps the two from drifting — a renamed field
    would otherwise fail silently into a null column.
    """

    model_config = ConfigDict(populate_by_name=True)

    repo: str = Field(min_length=1, validation_alias=AliasChoices("github", "repo"),
                      description="github nameWithOwner")
    pr: int = Field(ge=1)
    pr_title: str | None = Field(default=None,
                                 validation_alias=AliasChoices("pr_title", "title"))
    base: str | None = None
    #: The commit this round reviewed. ``base`` above is a branch NAME and moves,
    #: so before this the board held nothing that identified a commit at all and
    #: no round could ever be replayed against the repo. Coerced to a plausible
    #: commit id or dropped — see :func:`_sha_or_none`.
    head_sha: str | None = None
    #: The base end of that range, as two fields rather than one, because the
    #: field #98 named for the job reports the merge base and a merge base cannot
    #: move when the base branch does. ``merge_base`` is the PR's base commit —
    #: the round's own anchor under increment scope is ``since_sha``, so read
    #: ``scope`` before assuming the two are the same thing. ``base_sha`` is the
    #: base branch's tip at review time, and
    #: is the only one of the two a staleness check can rest on. Both coerced or
    #: dropped by :func:`_sha_or_none`, and neither ever derived from the other.
    merge_base: str | None = None
    base_sha: str | None = None
    #: Paths no reviewer that ran read in full. NULL (the field absent) is "the
    #: panel did not say"; ``[]`` is "it said, and nothing was cut"; a non-empty
    #: value nothing usable could be read from is NULL too, and says so in the
    #: response. Bounded and coerced like every other list here, never a 422 —
    #: see :func:`_unread_paths`.
    unread_files: list[str] | None = None
    #: The panel's own tally of :data:`PROVENANCE` buckets for this round, stored
    #: verbatim. Absent = nobody said; ``{}`` = the question does not arise (a
    #: round 1, or a run outside any cycle); all-zero = attribution ran and had
    #: nothing to attribute. Three states, and the release exists because
    #: collapsing states like these is how a measurement stops meaning anything.
    #:
    #: A tally that arrived non-empty and lost every pair to coercion lands on
    #: NULL, not ``{}`` — see :meth:`_prov_counts`.
    provenance_counts: dict[str, int] | None = None
    #: Set by the validators, never by the caller: what was trimmed, what could
    #: not be read, and which bucket names this board did not recognise. All of it
    #: reported in the response, none of it storable by the sender.
    unread_files_sent: int = 0
    #: Entries of ``unread_files`` that were there and were not paths — a
    #: non-string, or one over :data:`MAX_PATH_CHARS`. Distinct from the over-cap
    #: count: one says "we did not look", this says "we looked and it was not a
    #: path". Mirrors ``changed_files_dropped.unusable``.
    unread_files_unusable: int = 0
    #: The raw ``head_sha``, when one arrived and :func:`_sha_or_none` refused it.
    #: A run sent ``"HEAD"`` and a run sent nothing both store NULL, and only this
    #: tells the sender which of the two it just recorded.
    head_sha_dropped: str | None = None
    #: The same for the base end. Reported separately rather than folded into one
    #: "a commit id was dropped" flag: a producer that sends a good head and a
    #: garbled base has one bug, and a reader told only that *something* was
    #: refused has to guess which field to go and look at.
    merge_base_dropped: str | None = None
    base_sha_dropped: str | None = None
    #: Tally keys that are not a bucket this board knows.
    provenance_counts_unknown: list[str] = Field(default_factory=list)
    #: Tally keys that ARE a known bucket and whose count could not be believed —
    #: ``{"introduced": -1}``, ``{"missed": "two"}``. A second drop path, and it
    #: was the silent one: the release's rule is that a dropped field says so, and
    #: it was being applied to unknown NAMES only.
    provenance_counts_unusable: list[str] = Field(default_factory=list)
    #: Fields whose VALUE was not a shape this board can read at all —
    #: ``unread_files: 42``, ``provenance_counts: ["introduced"]``. The coarsest
    #: drop of the lot and the last one still silent: the per-entry signals above
    #: can only speak about a value they could iterate, so a wrong-typed field
    #: produced no entries, no keys and no word. Named, not counted: there is
    #: exactly one value and the fact worth reporting is which field it was.
    unreadable_fields: list[str] = Field(default_factory=list)
    changed_lines: int | None = None
    #: The PR's touched paths — the PR's, never the round's. Under #41 a later
    #: round reviews only the increment; narrowing this to it would report two
    #: PRs as no longer colliding because one stopped re-reading a file it still
    #: changes. Absent for every pre-v2.23 run, which is "no list", not "no files".
    #: ``null`` is accepted and means the empty list — a hand-rolled caller with
    #: no files to send reaches for it, and every neighbouring field on this model
    #: is ``| None``. Truncated at :data:`MAX_CHANGED_FILES` with a note rather
    #: than refused, and never silently: an authenticated sender is not a bounded
    #: one, and one request should not be able to insert a million rows in a
    #: single transaction.
    changed_files: list[ChangedFileIn] = Field(default_factory=list)
    #: GitHub's own count of the PR's changed files. NOT derived from
    #: ``len(changed_files)``: `gh` pages that list and GitHub caps it at 3,000,
    #: so the two disagreeing is the only signal a collision query under-reports.
    #: Coerced rather than validated, like everything else here.
    changed_files_total: int | None = None
    #: The PR's state as of this panel — `OPEN` / `MERGED` / `CLOSED`. Without it
    #: a collision query cannot tell a live rival from one merged last week.
    pr_state: str | None = None
    is_draft: bool | None = None
    #: How many entries the sender actually put in ``changed_files`` before the
    #: cap trimmed it. Set by the validator, never by the caller — it exists so
    #: the handler can report what it dropped instead of trimming in silence.
    changed_files_sent: int = 0
    diff_chars: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _count_files_sent(cls, v: object) -> object:
        """Record what the sender sent, before the bounds below trim it.

        Two path lists, one commit id and one bucket tally, all examined here for
        one reason: a cap or a filter that trims an answer has to be able to say
        so, and the field validators cannot reach the response.

        Every one of them is set unconditionally rather than only when the
        matching field is present. They are the record of what ARRIVED, so a
        caller that spells one of them itself must not be able to write its own
        account of how much of its payload was kept.

        **This runs after the whole body is parsed, which is where the storage
        caps bite and not where the cost does.** ``MAX_CHANGED_FILES`` bounds what
        is STORED; a sender can still make FastAPI parse an arbitrarily large list
        before either bound is reached, and ``changed_files`` has had that
        property since v2.23. Bounding it belongs in front of the app — a request
        body limit in the proxy — because by the time any pydantic validator runs
        the allocation has already happened. Noted rather than papered over: the
        cap here is a bound on what one request can INSERT, and it is not a
        defence against a large body.
        """
        if not isinstance(v, Mapping):
            return v
        files, unread = v.get("changed_files"), v.get("unread_files")
        counts = v.get("provenance_counts")
        _, unusable = _unread_paths(unread)
        head = v.get("head_sha")
        merge_base, base_sha = v.get("merge_base"), v.get("base_sha")
        return {**v,
                "changed_files_sent": len(files) if isinstance(files, list) else 0,
                # A bare string is one path — a shape `_unread_paths` explicitly
                # supports, and one this counted as zero sent, so a caller that
                # sent a single path read as a caller that sent nothing.
                "unread_files_sent": (len(unread) if isinstance(unread, list)
                                      else 1 if isinstance(unread, str) and unread.strip()
                                      else 0),
                "unread_files_unusable": unusable,
                "head_sha_dropped": (_echo(head) if head is not None
                                     and _sha_or_none(head) is None else None),
                # The base end, each named on its own. A run sent a garbled
                # `base_sha` records NULL there, which is the same value a run
                # that sent nothing records — the distinction #93 was filed over,
                # now on the field a pre-land verdict will read.
                "merge_base_dropped": (_echo(merge_base) if merge_base is not None
                                       and _sha_or_none(merge_base) is None else None),
                "base_sha_dropped": (_echo(base_sha) if base_sha is not None
                                     and _sha_or_none(base_sha) is None else None),
                # Keys this board has no column for. Named rather than dropped: a
                # bucket the panel has started sending and the board silently
                # ignores is #93 happening again, one release later.
                #
                # Echoed through `_echo`, the same normalisation
                # `FindingIn._keep_provenance_sent` uses, because `record_review`
                # merges the two into one set: `_bucket_or_none` strips before
                # testing membership, so an unstripped echo reported
                # `"  regressed  "` and `"regressed"` as two separate drifts.
                "provenance_counts_unknown": sorted(
                    {_echo(k) for k in counts if _bucket_or_none(k) is None}
                ) if isinstance(counts, Mapping) else [],
                # ...and the OTHER drop path, which said nothing at all: a key
                # this board does know, carrying a count it cannot believe.
                # `{"introduced": -1, "missed": "two", "unknown": 5}` stored one
                # pair of three and reported no loss.
                "provenance_counts_unusable": sorted(
                    {_echo(k) for k, n in counts.items()
                     if _bucket_or_none(k) is not None and _count_or_none(n) is None}
                ) if isinstance(counts, Mapping) else [],
                # ...and the coarsest drop of all: a value that is not the SHAPE
                # the field takes. The two signals above can only speak about a
                # value they could iterate, so `unread_files: 42` and
                # `provenance_counts: ["introduced"]` went to NULL with nothing
                # said — a wrong-typed producer reading exactly like one that sent
                # nothing, which is #93's own failure mode.
                "unreadable_fields": sorted(
                    name for name, val, ok in (
                        ("unread_files", unread, isinstance(unread, (str, list))),
                        ("provenance_counts", counts, isinstance(counts, Mapping)),
                    ) if val is not None and not ok)}

    @field_validator("head_sha", "merge_base", "base_sha", mode="before")
    @classmethod
    def _commit_id(cls, v: object) -> str | None:
        """Every commit id on this model, coerced by one rule.

        Listed on one validator rather than three copies: the head end and the
        base end have to agree about what a commit id IS, or a range assembled
        from them at read time compares a normalised value against a raw one."""
        return _sha_or_none(v)

    @field_validator("unread_files", mode="before")
    @classmethod
    def _unread(cls, v: object) -> list[str] | None:
        """Coerced to paths, deduped, bounded — or None. See :func:`_unread_paths`
        for the rule and for why an unreadable declaration is NULL rather than the
        empty list. What it could not read is counted in ``unread_files_unusable``
        and named back by :func:`record_review`."""
        return _unread_paths(v)[0]

    @field_validator("provenance_counts", mode="before")
    @classmethod
    def _prov_counts(cls, v: object) -> dict[str, int] | None:
        """The known buckets with believable counts, or None.

        Unknown keys are dropped — a published tally must not carry a key no
        consumer can interpret — and named in the response by
        :meth:`_count_files_sent` so the drop is visible. A count that cannot be
        believed drops with its key rather than becoming 0: this whole feature is
        built on zero being a claim. That drop is reported too, under
        ``provenance_counts_unusable``.

        ``{}`` survives as ``{}`` **when the caller sent** ``{}``. It is the
        panel's way of saying the question does not arise, and turning it into
        None here would make a round 1 indistinguishable from a run recorded
        before any of this existed.

        A tally that arrived non-empty and lost every pair lands on None, NOT on
        ``{}``. Returning the emptied dict manufactured the round-1 statement out
        of a payload that had attempted attribution and had every answer refused
        — the same collapse in the other direction, and worse, because it also
        cost the run its ``provenance_runs`` coverage marker (``{}`` is
        deliberately excluded from that filter). NULL is where an unreadable value
        belongs: nobody said anything this board could read. The names are in the
        response either way.

        Two keys that normalise to one bucket (``Introduced`` beside
        ``introduced``) leave the last one, which is what a dict comprehension
        over the same coercion would do anywhere else in this module. Nothing
        real sends that shape and inventing a merge rule for it would be picking
        a number nobody stated.
        """
        if not isinstance(v, Mapping):
            return None
        out = {}
        for k, raw in v.items():
            bucket = _bucket_or_none(k)
            n = _count_or_none(raw)
            if bucket is not None and n is not None:
                out[bucket] = n
        return out if out or not v else None

    @field_validator("changed_files", mode="before")
    @classmethod
    def _files(cls, v: object) -> object:
        """``null`` → ``[]``, a non-list → ``[]``, and bounded. Never a 422: the
        findings in the same payload are worth more than the file list is.

        What is trimmed here is counted in ``changed_files_sent`` and reported in
        the response — a truncation the sender cannot see is one it will read as
        a complete list."""
        if not isinstance(v, list):
            return []
        return v[:MAX_CHANGED_FILES]

    @field_validator("changed_files_total", mode="before")
    @classmethod
    def _files_total(cls, v: object) -> int | None:
        return _count_or_none(v)

    @field_validator("pr_state", mode="before")
    @classmethod
    def _state(cls, v: object) -> str | None:
        """One of :data:`PR_STATES`, upper-cased, or None.

        Anything outside the set becomes None rather than being stored verbatim.
        A typo, a variant spelling or a future GitHub state stored as-is is worse
        than useless to a consumer filtering on it: `!= "OPEN"` silently reclassifies
        the PR, and it does so in the direction that hides work. None is honest —
        "nobody stated a state I understand" — and is the value every consumer
        already has to handle, because every pre-v2.23 run carries it.
        """
        if not isinstance(v, str):
            return None
        s = v.strip().upper()
        return s if s in PR_STATES else None
    diff_truncated: bool | None = None

    judged: bool = False
    judge_model: str | None = None
    judge_skip: str | None = None
    coverage_note: str | None = None
    sonar_gate: str | None = None
    ci_status: str | None = None

    #: Where this run sat in the panel -> fix -> panel cycle. Absent = round 1,
    #: which is what every pre-v2.15 run was. Coerced rather than validated: this
    #: module's rule is that recording is best-effort (see :func:`_line_or_none`),
    #: and rejecting the payload would lose the findings, the scorecards and the
    #: accounts along with the bad integer.
    round: int = 1
    #: Which panel -> fix -> panel cycle this round belongs to. Opaque and minted
    #: by the panel; every round of one cycle sends the same value. Absent for
    #: every pre-v2.15 run, and for a standalone review that is nobody's round 2.
    cycle: str | None = None
    new_findings: int | None = None
    #: The stopping rule's own account of itself. ``stop_reason`` is accepted flat
    #: as well, because the panel prints it both ways and a caller reproducing the
    #: payload by hand should not have to nest one string.
    round_stop: StopIn | None = None
    stop_reason: str | None = None

    reviewers_selected: list[str] = Field(default_factory=list)
    reviewers_override: str | None = None
    skipped: list[str] = Field(default_factory=list)
    #: Per-member config keyed by vendor name. Optional: an older panel sends
    #: none and the members are inferred from finding attribution, with no model
    #: recorded — a run that can still be counted, just not tiered.
    reviewers: dict[str, ReviewerIn] = Field(default_factory=dict)
    #: Whether the seats could read the code (#113). Optional: a panel that predates
    #: the setting sends none, and every field inside it is independently nullable.
    code_access: CodeAccessIn | None = None

    to_fix: list[FindingIn] = Field(default_factory=list)
    dismissed: list[FindingIn] = Field(default_factory=list)
    sonar_findings: list[FindingIn] = Field(default_factory=list)

    session: str | None = None
    run_key: str | None = Field(
        default=None,
        description="idempotency key; re-POSTing the same key returns the first run",
    )

    @field_validator("round", mode="before")
    @classmethod
    def _round(cls, v: object) -> int:
        """Rounds are numbered from 1, and an unbelievable one is round 1 — the
        same answer the migration gives every row that predates the column."""
        n = _count_or_none(v)
        return n if n and n >= 1 else 1

    @field_validator("new_findings", mode="before")
    @classmethod
    def _new_findings(cls, v: object) -> int | None:
        return _count_or_none(v)


def _verdict(f: FindingIn, judged: bool) -> str:
    """Where a to_fix finding sits: adjudicated real, or merely never judged.

    The panel marks the latter with ``reason='unjudged'`` and keeps it (it never
    suppresses on a missing verdict), so the two arrive in the same list.
    """
    return "confirmed" if judged and f.reason != "unjudged" else "unjudged"


_NOT_WORD = re.compile(r"[^a-z0-9]+")


def _derive_key(file: str | None, title: str) -> str:
    """A defect identity for a caller that has none of its own.

    File plus a normalised title, and deliberately **not** the line: the line
    moves when the fix above it lands, and an identity that moves links nothing.
    Best-effort by nature — a judge that rewords its synthesis between runs
    breaks the chain, which is why an explicit ``key`` always wins.

    Duplicated as SQL in migration 0012 to backfill pre-v2.11 rows; the two must
    stay identical or old runs join no chain.
    """
    norm = _NOT_WORD.sub(" ", title.lower()).strip()
    return hashlib.md5(f"{file or ''}|{norm}".encode(), usedforsecurity=False).hexdigest()[:16]


@dataclass(slots=True)
class Prepared:
    """A finding with its ingest-time derivations settled once, up front."""

    f: FindingIn
    verdict: str
    #: What gets stored, which is also what the key is derived from — a title
    #: defaulted at storage time but keyed before it would put an untitled
    #: finding in a different chain from the backfilled ones.
    title: str
    #: Every member credited, in payload order: ``reviewers`` then any reporter
    #: only ``reported_by`` names.
    reviewers: list[str]
    reports: list[ReportIn]
    key: str
    related: list[str] = field(default_factory=list)
    #: Members that declared this finding's fix worth re-reading, finest grain
    #: first: each reporter's own flag, then the panel's ``rereview_by``, then —
    #: when the finding is flagged with no attribution at all — every credited
    #: member that sent no flag of its own, which over-credits but never silently
    #: drops the declaration.
    #:
    #: It CAN end up empty on a finding whose ``needs_rereview`` is true: when
    #: every credited member sent an EXPLICIT ``needs_rereview`` and every one of
    #: them said false. A reporter is authoritative about its own no, so there is
    #: nobody left to credit, and filling it in would manufacture a declaration
    #: nobody made. The
    #: finding still stores the flag (the caller said so at finding level), so
    #: ``GET /review/findings`` shows it while no member's ``rereview_flagged``
    #: counts it — a caller contradicting itself, recorded rather than resolved.
    rereview_by: list[str] = field(default_factory=list)


def _prepare(findings: list[tuple[FindingIn, str]]) -> list[Prepared]:
    """Settle attribution, defect key and ``related`` links for one payload.

    Attribution is unioned rather than chosen: ``reported_by`` is authoritative
    about *what* was said, but a panel may still list a member alongside it that
    contributed no text, and dropping that member would silently un-credit it.
    """
    prepared: list[Prepared] = []
    for f, verdict in findings:
        reports: list[ReportIn] = []
        seen: set[str] = set()
        for r in f.reported_by:
            # Two accounts from one reviewer would violate the table's
            # (finding, reviewer) uniqueness; the first is kept rather than the
            # request being rejected, since ingest is best-effort for the panel.
            if r.reviewer and r.reviewer not in seen:
                seen.add(r.reviewer)
                reports.append(r)

        reviewers = [n for n in dict.fromkeys(x.strip() for x in f.reviewers) if n]
        reviewers += [r.reviewer for r in reports if r.reviewer not in reviewers]

        title = f.title or "(untitled)"
        # Who declared the fix worth re-reading, finest grain first. A reporter
        # that sent an EXPLICIT `needs_rereview` is authoritative about itself —
        # including its `false`, which `rereview_by` may not overturn, since
        # reading a member's own no as "no data" would manufacture a declaration
        # it declined to make.
        #
        # A reporter that omitted the key said nothing at all, and a defaulted
        # False is not a declaration: `rereview_by` is the coarser grain that
        # remains for a caller sending accounts without per-report flags, and
        # dropping it for every reviewer that happened to send an account lost
        # the attribution outright — the finding stored `needs_rereview=True`
        # with nobody credited for it.
        named = {r.reviewer for r in reports if "needs_rereview" in r.model_fields_set}
        flagged = [r.reviewer for r in reports if r.needs_rereview]
        flagged += [n for n in f.rereview_by if n in reviewers and n not in named]
        # A finding flagged with nobody creditable: credit every member that is
        # not authoritative about its own silence — i.e. everyone credited on the
        # finding that sent no `needs_rereview` of its own, whether or not it sent
        # an account. Over-crediting is visible and correctable; dropping the
        # declaration is neither — and "nobody creditable" includes a
        # `rereview_by` naming only members this finding does not credit (a
        # renamed or retired reviewer, a typo, a member merged out), which is
        # exactly the case that used to leave `needs_rereview` stored with no
        # member credited and no `rereview_flagged` tallied anywhere.
        if not flagged and f.needs_rereview:
            flagged = [n for n in reviewers if n not in named]
        # Ordered dedup. `rereview_by: ["codex", "codex"]` — trivially produced by
        # a caller merging two reviewer lists — tallied `rereview_flagged` twice
        # for one declaration, and nothing behind it catches the repeat: unlike a
        # duplicate reporter, these names create no rows for
        # `uq_review_report_finding_reviewer` to reject. The field feeds a
        # published per-reviewer statistic, so over-counting is not benign.
        flagged = list(dict.fromkeys(flagged))
        prepared.append(Prepared(
            f=f,
            verdict=verdict,
            title=title,
            reviewers=reviewers,
            reports=reports,
            key=(f.key or "").strip() or _derive_key(f.file, title),
            rereview_by=flagged,
        ))

    # `related` arrives as the panel's run-local ids; stored as keys so the
    # links survive the run they were made in. A ref to something not in this
    # payload names nothing that can be linked, so it is dropped.
    by_id = {p.f.id: p.key for p in prepared if p.f.id}
    for p in prepared:
        p.related = sorted({by_id[r] for r in p.f.related if r in by_id} - {p.key})
    return prepared


def _calibration(own: str | None, judged: str | None) -> str | None:
    """Which way this reviewer's severity missed the judge's, if it can be told.

    ``P1 < P2`` lexically and P1 is the more severe, so a reviewer whose severity
    sorts *before* the judge's called it worse than it was.
    """
    a, b = (own or "").upper(), (judged or "").upper()
    if a not in SEVERITIES or b not in SEVERITIES:
        return None
    if a == b:
        return "sev_agree"
    return "sev_stricter" if a < b else "sev_looser"


def _scorecards(
    findings: list[Prepared],
    cfg: dict[str, ReviewerIn],
    selected: list[str],
    skipped: list[str],
) -> list[ReviewReviewer]:
    """Tally each panel member from the findings it is credited on.

    Derived here rather than sent, so a scorecard cannot contradict the findings
    it summarises. Members that ran but found nothing, and members that never
    ran at all, still get a row — a zero is data and a silent absence isn't.

    Without a ``reviewers`` block (an older panel) a member is assumed to have
    run unless it appears in ``skipped``, whose entries read ``"codex: CLI
    absent"``. Assuming the opposite would file every quiet reviewer as broken.
    """
    # `rereview_by` is unioned in defensively, not because it can add a name
    # today: :func:`_prepare` builds it from report reviewers (appended to
    # `reviewers`), from `f.rereview_by` filtered by membership, or from
    # `reviewers` itself, so it is always a subset. The tally below indexes
    # `tally[name]` for every name in it, and that lookup must stay total if the
    # attribution rules ever widen.
    credited = {r for p in findings for r in (*p.reviewers, *p.rereview_by)}
    skips = {s.split(":", 1)[0].strip(): s for s in skipped if ":" in s}
    names = sorted(set(cfg) | set(selected) | credited)

    # Tallied as plain counters first: a column ``default=0`` is applied at
    # flush, so incrementing a freshly-constructed ORM object would start from
    # None.
    zero = ("raised", "confirmed", "dismissed", "unjudged", "solo", "shared",
            "sev_stricter", "sev_agree", "sev_looser", "rereview_flagged",
            *(s.lower() for s in SEVERITIES), *PROVENANCE_COUNTER.values())
    tally: dict[str, dict[str, int]] = {n: dict.fromkeys(zero, 0) for n in names}
    for p in findings:
        own = {r.reviewer: r for r in p.reports}
        # Confirmed only, which is the population `pr_finding_history` scores the
        # same declaration over. Counting dismissed and unjudged findings here
        # published two different numbers under one name — the run detail table's
        # "flagged for re-review" column and `/review/stats.rereview_flagged`
        # against the history block printed directly beneath them.
        if p.verdict == "confirmed":
            for name in p.rereview_by:
                tally[name]["rereview_flagged"] += 1
        for name in p.reviewers:
            t = tally[name]
            t["raised"] += 1
            if len(p.reviewers) > 1:
                t["shared"] += 1
            if p.verdict == "confirmed":
                t["confirmed"] += 1
                if len(p.reviewers) == 1:
                    t["solo"] += 1
                sev = (p.f.severity or "").upper()
                if sev in SEVERITIES:
                    t[sev.lower()] += 1
                # #48's axis, per member. Confirmed only, like the severity
                # counters directly above and for the same reason: a dismissed
                # finding was not a defect, so asking whether a fix pass caused
                # it credits a reviewer for the provenance of something that was
                # never there. `provenance` is already normalised to a known
                # bucket or None by `FindingIn`, so an unrecognised value counts
                # nowhere rather than inventing a column.
                counter = PROVENANCE_COUNTER.get(p.f.provenance or "")
                if counter:
                    t[counter] += 1
                # Calibration only over confirmed findings: on a dismissal the
                # recorded severity is the panel's own, so comparing a reviewer
                # against it would be comparing it to itself.
                bucket = _calibration(own[name].severity, sev) if name in own else None
                if bucket:
                    t[bucket] += 1
            elif p.verdict in ("dismissed", "unjudged"):
                t[p.verdict] += 1

    cards = []
    for name in names:
        c = cfg.get(name)
        skip = c.skip if c else skips.get(name)
        cards.append(ReviewReviewer(
            name=name,
            model=(c.model or None) if c else None,
            effort=(c.effort or None) if c else None,
            ran=c.ran if c else skip is None,
            skip_reason=skip,
            max_diff_chars=c.max_diff_chars if c else None,
            truncated=c.truncated if c else None,
            duration_ms=c.duration_ms if c else None,
            could_not_assess=c.could_not_assess if c else None,
            unstructured=c.unstructured if c else None,
            absent=c.absent if c else None,
            code_blind=c.code_blind if c else None,
            argv_capped=c.argv_capped if c else None,
            input_tokens=c.input_tokens if c else None,
            output_tokens=c.output_tokens if c else None,
            cached_input_tokens=c.cached_input_tokens if c else None,
            reasoning_tokens=c.reasoning_tokens if c else None,
            cost_usd=c.cost_usd if c else None,
            **tally[name],
        ))
    return cards


@router.post("/review", status_code=status.HTTP_201_CREATED)
async def record_review(
    body: ReviewIn,
    author: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Record one panel run: the run, a scorecard per member, every finding.

    Best-effort from the caller's side — the panel must never fail a review
    because the board was down — so this stays cheap and idempotent on
    ``run_key``.
    """
    if body.run_key:
        existing = await session.scalar(
            select(ReviewRun).where(ReviewRun.run_key == body.run_key)
        )
        if existing is not None:
            return {"id": existing.id, "recorded": False, "reason": "duplicate run_key"}

    findings = _prepare(
        [(f, _verdict(f, body.judged)) for f in body.to_fix]
        + [(f, "dismissed") for f in body.dismissed]
        + [(f, "sonar") for f in body.sonar_findings]
    )
    counts = {v: sum(1 for p in findings if p.verdict == v) for v in
              ("confirmed", "dismissed", "unjudged", "sonar")}

    run = ReviewRun(
        author=author,
        session=body.session,
        repo=body.repo,
        pr=body.pr,
        pr_title=body.pr_title,
        base_branch=body.base,
        # The commit reviewed, and what the round could not read of it. Both
        # stored AS SENT, NULL included: a run that says nothing about its
        # coverage is not a run that read everything.
        head_sha=body.head_sha,
        # The base end of the same range, both halves stored AS SENT. `base_sha`
        # is the base branch's tip and `merge_base` is what the diff was built
        # from; neither is backfilled from the other, because a merge base
        # standing in for a tip is exactly the substitution that makes a
        # staleness check unable to fire.
        merge_base=body.merge_base,
        base_sha=body.base_sha,
        unread_files=body.unread_files,
        # The panel's own tally, not a count over the rows below. The two have
        # different populations by design (see the column's docstring), and `{}`
        # carries a fact — "the question does not arise" — that no derivation
        # from findings could express.
        provenance_counts=body.provenance_counts,
        changed_lines=body.changed_lines,
        # Stored AS SENT rather than backfilled from len(changed_files). A caller
        # that sends the paths and not the count leaves this NULL, and NULL there
        # honestly means "nobody said how many there were" — filling it in from
        # the rows would manufacture agreement between the two numbers whose
        # DISAGREEMENT is the only evidence the list is short.
        changed_files_total=body.changed_files_total,
        pr_state=body.pr_state,
        is_draft=body.is_draft,
        diff_chars=body.diff_chars,
        diff_truncated=body.diff_truncated,
        judged=body.judged,
        judge_model=body.judge_model or None,
        judge_skip=body.judge_skip,
        coverage_note=body.coverage_note or None,
        # Flattened onto the run from the nested object, minus `seats` — that one
        # lives on the reviewer rows it describes (see CodeAccessIn).
        code_access=body.code_access.setting if body.code_access else None,
        convention_files_removed=(body.code_access.convention_files_removed
                                  if body.code_access else None),
        round=body.round,
        cycle=body.cycle or None,
        new_findings=body.new_findings,
        # The nested verdict wins over the flat string: it is the one that also
        # carries whether the stop was earned, and a payload sending both sends
        # the same reason twice. The flat form can only carry the reason, so a
        # caller using it says nothing about whether the cycle stopped — NULL,
        # not a guessed True.
        stopped=body.round_stop.stop if body.round_stop else None,
        stop_reason=(body.round_stop.reason if body.round_stop else body.stop_reason) or None,
        stop_confident=body.round_stop.confident if body.round_stop else None,
        # The reasons, not just the verdict. Accepting the list and dropping it
        # left the board able to say a stop was not convergence and unable to say
        # why — which is the half an operator is told to relay.
        #
        # Stored AS SENT, empty list included. `veto or None` collapsed "the panel
        # ran the stopping rule and found nothing to veto" onto "no panel ever
        # said" — the same NULL/[] collapse this release argues at length must not
        # happen to `could_not_assess`, one field over — and `_run_view` passes
        # it through unmasked, so a reader of the API sees the distinction too.
        stop_veto=body.round_stop.veto if body.round_stop else None,
        sonar_gate=body.sonar_gate,
        ci_status=body.ci_status,
        reviewers_selected=body.reviewers_selected or None,
        reviewers_override=body.reviewers_override,
        skipped=body.skipped or None,
        n_confirmed=counts["confirmed"],
        n_dismissed=counts["dismissed"],
        n_unjudged=counts["unjudged"],
        n_sonar=counts["sonar"],
        run_key=body.run_key,
    )
    session.add(run)
    await session.flush()  # need run.id for the children

    #: Entries this request sent that were not stored: over the cap, or with no
    #: usable path. Reported back in the response, because the alternative is the
    #: silence the first version shipped — a caller posting 6,000 paths got a run
    #: short by 1,000 with nothing anywhere saying so, and then read its own
    #: complete-looking list as evidence of no collision. A cap that trims an
    #: answer has to announce itself, which is this module's rule elsewhere and
    #: had no channel here.
    over_cap = max(0, body.changed_files_sent - len(body.changed_files))

    # Deduped on the way in, keeping the first mention of each path. The table's
    # unique constraint would otherwise turn a sender that repeats a path into an
    # IntegrityError that costs the whole run its findings — and this module's
    # rule is that recording is best-effort.
    #
    # Paths are STRIPPED before storing and comparing, which is a real
    # normalisation and not just tidying: it is what makes `" a.py"` and `"a.py"`
    # one row rather than an IntegrityError, and a padded path would otherwise
    # join to nothing in every collision query. Git does permit leading and
    # trailing whitespace in a path, so this can in principle fold two genuinely
    # distinct paths together; that trade is taken deliberately, because the
    # alternative is a silent collision miss on every ordinary padded path.
    #
    # No insertion order is preserved or promised: there is no ordinal column and
    # every reader sorts by path (`GET /review/{id}`).
    seen: set[str] = set()
    unusable = 0
    for cf in body.changed_files:
        path = cf.path.strip()
        if not path:
            unusable += 1
            continue
        if path in seen:
            continue
        seen.add(path)
        session.add(ReviewRunFile(
            run_id=run.id, path=path, additions=cf.additions, deletions=cf.deletions,
        ))

    # Sonar's hard-gate issues are the gate's own output, not a panel member's
    # judged findings — excluded from the scorecards so they can't inflate a
    # precision the judge never ruled on.
    scored = [p for p in findings if p.verdict != "sonar"]
    for card in _scorecards(scored, body.reviewers, body.reviewers_selected, body.skipped):
        card.run_id = run.id
        session.add(card)

    rows = [
        (
            ReviewFinding(
                run_id=run.id,
                verdict=p.verdict,
                severity=(p.f.severity or "").upper() or None,
                file=p.f.file,
                line=p.f.line,
                title=p.title,
                detail=p.f.detail or None,
                reason=p.f.reason or None,
                finding_key=p.key,
                related=p.related or None,
                reviewers=p.reviewers or None,
                n_reviewers=len(p.reviewers),
                needs_rereview=bool(p.rereview_by) or p.f.needs_rereview,
                new_this_round=p.f.new_this_round,
                # The irreplaceable one: per finding, so nothing the board keeps
                # could reconstruct it after the fact.
                provenance=p.f.provenance,
            ),
            p.reports,
        )
        for p in findings
    ]
    for finding, _ in rows:
        session.add(finding)
    if rows:
        await session.flush()  # need finding.id for the accounts hanging off it

    accounts = 0
    # Zipped with the prepared findings rather than looked up by key: two findings
    # in one payload can share a defect key (the same title raised and dismissed),
    # and a lookup would then hand one finding's declarations to the other.
    for p, (finding, reports) in zip(findings, rows, strict=True):
        for r in reports:
            accounts += 1
            session.add(
                ReviewFindingReport(
                    finding_id=finding.id,
                    reviewer=r.reviewer,
                    severity=(r.severity or "").upper() or None,
                    line=r.line,
                    account=r.account or None,
                    # A flag the panel attributed via `rereview_by` belongs on the
                    # reporter's row too — same declaration, arriving by the only
                    # channel a panel that merges before the judge still has.
                    needs_rereview=r.needs_rereview or r.reviewer in p.rereview_by,
                )
            )

    await session.commit()
    recorded = {"id": run.id, "recorded": True, "findings": len(findings),
                "accounts": accounts, "changed_files": len(seen)}
    #: Everything this request sent and this run did not store, keyed as the
    #: response keys it: merged into the response below AND logged, because the
    #: response is read once by whoever posted it and `qb record-review` prints
    #: only the run id.
    dropped: dict = {}
    # Only when something WAS dropped, so an ordinary response stays the shape
    # every existing caller already parses.
    if over_cap or unusable:
        dropped["changed_files_dropped"] = {"over_cap": over_cap, "unusable": unusable}
    # Two ways an unread path can be lost, reported as two numbers under one key
    # exactly as `changed_files_dropped` does. `over_cap` is entries beyond the
    # cap and only those — deliberately NOT the difference between what was sent
    # and what was stored, because a blank or repeated path is folded rather than
    # lost (the same silence `changed_files` keeps over its own dedup) and a
    # signal that fires on a payload nothing went missing from teaches its reader
    # to ignore it. `unusable` is an entry that was there and was not a path.
    unread_over_cap = max(0, body.unread_files_sent - MAX_CHANGED_FILES)
    if unread_over_cap or body.unread_files_unusable:
        dropped["unread_files_dropped"] = {
            "over_cap": unread_over_cap, "unusable": body.unread_files_unusable}
    # A commit id that was sent and could not be one. Without this, a run whose
    # producer sent `"HEAD"` is indistinguishable on read from a run whose
    # producer sent nothing — the drop this release was filed over, one field
    # across.
    if body.head_sha_dropped is not None:
        dropped["head_sha_dropped"] = body.head_sha_dropped
    # ...and the base end, each named rather than one flag for "a commit id was
    # refused". These two are what a pre-land verdict resolves against the repo,
    # so a producer sending a base it thinks was stored has to be told it was not.
    if body.merge_base_dropped is not None:
        dropped["merge_base_dropped"] = body.merge_base_dropped
    if body.base_sha_dropped is not None:
        dropped["base_sha_dropped"] = body.base_sha_dropped
    # Every provenance bucket this board did not recognise, from the findings and
    # from the run's own tally, named rather than swallowed.
    #
    # This issue IS the cost of an ingest that drops what it does not understand
    # without a word, so the repair must not ship its own quieter version of the
    # same thing.
    #
    # A finding whose `provenance_sent` is `""` counts: an empty or all-whitespace
    # bucket name is a producer sending a malformed value, not one asking nothing,
    # and the two must not read the same. Hence `is not None`.
    unknown = sorted({
        p.f.provenance_sent for p in findings
        if p.f.provenance is None and p.f.provenance_sent is not None
    } | set(body.provenance_counts_unknown))
    # Each NAME is bounded by `MAX_BUCKET_ECHO` and the COUNT was bounded by
    # nothing, so a tally of ten thousand junk keys was sorted and serialised in
    # full. Past the cap the response says how many there were rather than listing
    # them: drift is a handful of names, and a longer list is a sender fault the
    # count describes better than the names would. Both the list and the total are
    # over distinct ECHOES — two names agreeing in their first `MAX_BUCKET_ECHO`
    # characters are one marked entry and one unit of the count, which is the
    # bounded echo doing its job rather than the count being wrong.
    if unknown:
        dropped["provenance_unknown"] = unknown[:MAX_UNKNOWN_BUCKETS]
        if len(unknown) > MAX_UNKNOWN_BUCKETS:
            dropped["provenance_unknown_total"] = len(unknown)
    # Bounded for the same reason, and it is not only the pathological case: a
    # known bucket is echoed as the sender SPELLED it, and one word has as many
    # spellings as it has letters to re-case.
    if body.provenance_counts_unusable:
        dropped["provenance_counts_unusable"] = (
            body.provenance_counts_unusable[:MAX_UNKNOWN_BUCKETS])
        if len(body.provenance_counts_unusable) > MAX_UNKNOWN_BUCKETS:
            dropped["provenance_counts_unusable_total"] = len(
                body.provenance_counts_unusable)
    # A field whose value was not a shape this board reads at all. Last of the
    # silent drops, and the coarsest: nothing about the value could be reported
    # per entry, because nothing about it could be walked.
    if body.unreadable_fields:
        dropped["unreadable_fields"] = body.unreadable_fields

    # The durable half. The response names the drift to whoever made the request,
    # and `qb record-review` prints only the run id — so without this line the
    # evidence was gone as soon as the response was parsed, and #65's drift check,
    # the reader this signal exists for, would have had nothing left to read. One
    # line per run, and only when something was actually dropped: a line per
    # ordinary run is noise, and noise is how a real drop goes unread.
    #
    # Emitted as ONE json object rather than interpolated fields, because every
    # string in it is caller-supplied and this line is a record something else is
    # meant to read. `json.dumps` escapes the newline that would otherwise forge a
    # second entry — `_echo` already replaces control characters at the source,
    # but `repo` reaches the same line and travels no such path, so the escaping
    # has to be here as well as there. It also makes the line parseable, which is
    # what #65's check wants of it.
    if dropped:
        _log.warning("review ingest dropped fields: %s",
                     json.dumps({"run": run.id, "repo": body.repo, "pr": body.pr,
                                 "author": author, **dropped}, default=str))
    return {**recorded, **dropped}


# --------------------------------------------------------------- outcome ingest

def _trimmed_or_none(v: object) -> str | None:
    """A caller's single-line value, trimmed, or nothing at all.

    Empty and whitespace collapse to None deliberately: ``attested_by: "  "`` is
    not an attestation, and storing it would make an unattended refutation
    indistinguishable from a signed-off one in every query that tests the column
    for NULL.
    """
    if not isinstance(v, str):
        return None
    return v.strip() or None


#: The single-line fields a caller may set on an outcome, and the bound each
#: takes. Named once because three separate rules (bounds, "was it sent",
#: fill-versus-rewrite) all iterate the same set, and a field added to one list
#: and not the others is the silent half-wiring this endpoint keeps finding.
OUTCOME_FIELDS = {
    "note": MAX_NOTE_CHARS,
    "deferred_to": MAX_REF_CHARS,
    "superseded_by": MAX_REF_CHARS,
    "attested_by": MAX_REF_CHARS,
}


class OutcomeIn(BaseModel):
    """One defect's terminal outcome, as the fixer (or a human) reports it.

    ``extra="forbid"``, unlike every model on the ingest path: a misspelled
    ``attestedBy`` there costs a field on a review that still records, and here it
    silently downgrades a signed-off refutation to unattended in a published
    figure. This model can afford it because a rejection is per ITEM — see
    :func:`record_outcomes` — so a typo costs its own row and not the batch.

    Over-long values are refused rather than trimmed, for the same reason: a
    refutation cut at :data:`MAX_NOTE_CHARS` loses whichever sentence was last,
    which on a refutation is usually the conclusion, and the caller is told it
    recorded fine.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    #: The defect's ``finding_key`` — the identity of the defect, which is what
    #: makes an outcome joinable to every round that raised it. ``finding_key`` is
    #: accepted as an alias because that is what the read paths call it back.
    key: str = Field(min_length=1, max_length=MAX_REF_CHARS,
                     validation_alias=AliasChoices("key", "finding_key"))
    outcome: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    #: Why. Required for ``refuted`` — see :func:`record_outcomes`.
    note: str | None = None
    deferred_to: str | None = None
    superseded_by: str | None = None
    #: Who the CALLER SAYS signed this off. Recorded as a claim, never as proof:
    #: the board cannot authenticate a human, and this field is free text from the
    #: same request that carries the refutation. See :func:`record_outcomes`.
    attested_by: str | None = None

    @field_validator("key", "outcome")
    @classmethod
    def _trim(cls, v: str) -> str:
        # `min_length=1` is checked before this runs, so `"   "` passes it and
        # trims to empty — and an empty key was then rejected downstream with
        # "no finding with this key on this PR", which points the caller at its
        # keys when the fault was a blank one. Say what actually happened.
        s = v.strip()
        if not s:
            raise ValueError("blank")
        return s

    @field_validator("outcome")
    @classmethod
    def _fold(cls, v: str) -> str:
        return v.lower()

    @field_validator("note", "deferred_to", "superseded_by", "attested_by")
    @classmethod
    def _text(cls, v: str | None) -> str | None:
        # None survives as None and means CLEAR when the key was sent explicitly
        # — `model_fields_set` is what tells that apart from an absent key, so a
        # mistaken attestation can be retracted without inventing two revisions
        # by flipping the outcome and back.
        return _trimmed_or_none(v)

    @model_validator(mode="after")
    def _bounds(self) -> OutcomeIn:
        long = [f for f, cap in OUTCOME_FIELDS.items()
                if (getattr(self, f) or "") and len(getattr(self, f)) > cap]
        if long:
            caps = ", ".join(f"{f} over {OUTCOME_FIELDS[f]} characters" for f in long)
            raise ValueError(f"too long: {caps}")
        return self


class OutcomesIn(BaseModel):
    """A batch of outcomes for one PR.

    Batched because that is the shape the work has: a fix pass clears a round's
    findings together, and one call per finding turns "record what happened" into
    a loop the fixer can abandon half-way — which is how the outcomes end up
    partially recorded and the coverage marker lies about the rest.

    ``outcomes`` is a list of raw objects rather than of :class:`OutcomeIn`, and
    that is the whole point: FastAPI validates a typed list before the handler
    runs, so one item with a missing ``key`` would 422 the request and lose the
    eleven good rows this endpoint promises to keep. Each item is validated
    individually in :func:`record_outcomes` and a failure becomes that item's
    rejection.
    """

    model_config = ConfigDict(populate_by_name=True)

    repo: str = Field(min_length=1, max_length=MAX_REF_CHARS,
                      validation_alias=AliasChoices("github", "repo"),
                      description="github nameWithOwner")
    pr: int = Field(ge=1)
    #: The recorder's session, stored beside its identity exactly as a run stores
    #: it: ``set_by`` says who, and this is what lets a peer reach them about it.
    session: str | None = None
    #: ``list[Any]``, and every word of that is load-bearing. Untyped, because
    #: even ``list[dict]`` is validated by FastAPI before the handler runs — one
    #: entry that is a bare string then costs the whole request, which is
    #: precisely the guarantee this endpoint makes. Bounded at
    #: :data:`MAX_OUTCOMES_ACCEPTED` rather than at :data:`MAX_OUTCOMES`, so an
    #: over-cap batch has its overflow NAMED (a caller that batched a long fix
    #: loop keeps its first 500 rows) while a body ten times that is refused at
    #: parse time: naming 99,500 rejections costs a dict each and one enormous
    #: log line, which is a worse answer than one cheap 422.
    #: The item schema is attached by hand, because `list[Any]` erases it: a
    #: generated client would otherwise be told the request body is an array of
    #: anything, with none of `key`/`outcome`/`note` named. The type is what buys
    #: per-item rejection; the schema is what a caller reads.
    outcomes: list[Any] = Field(
        min_length=1, max_length=MAX_OUTCOMES_ACCEPTED,
        json_schema_extra=lambda f: f.update(items=OutcomeIn.model_json_schema()),
    )

    @field_validator("repo")
    @classmethod
    def _repo(cls, v: str) -> str:
        # Trimmed because it is matched with `==` against what `POST /review`
        # stored: a trailing space makes `known` empty and every item in the
        # batch is then rejected with "no finding with this key on this PR",
        # which points the caller at its keys, which were fine.
        s = v.strip()
        if not s:
            raise ValueError("repo is blank")
        return s

    @field_validator("session")
    @classmethod
    def _session(cls, v: str | None) -> str | None:
        # Refused, not sliced — the same rule `OutcomeIn._bounds` enforces for
        # every other bounded field, and for a sharper reason here: a truncated
        # session is a contact address that resolves to nothing, handed back as
        # if it were the real one.
        s = _trimmed_or_none(v)
        if s and len(s) > MAX_REF_CHARS:
            raise ValueError(f"session over {MAX_REF_CHARS} characters")
        return s


def _outcome_reason(item: OutcomeIn, known: set[str], stored: ReviewFindingOutcome | None,
                    seen: set[str]) -> str | None:
    """Why this item cannot be recorded, or None if it can.

    Rejections are itemised and named rather than the request being refused
    wholesale: a fix pass reporting twelve findings must not lose the eleven good
    ones to one typo. They are also never silent — this is the same rule the
    review ingest holds to, and it exists because a caller told nothing assumes it
    was told everything.

    Unlike ``POST /review``, an unusable value here is refused rather than coerced
    away. The panel must never fail a review because the board was fussy, so that
    path takes what it can read; a fixer recording an outcome has no such
    constraint and can simply be told.
    """
    if item.outcome not in OUTCOMES:
        return f"unknown outcome {_echo(item.outcome)!r}; one of {'|'.join(OUTCOMES)}"
    if item.key in seen:
        # `seen` holds every key this payload has already spoken about, accepted
        # or not: it used to hold only the accepted ones, so a key whose FIRST
        # entry was rejected had its later duplicates told "the first entry was
        # kept" when nothing had been kept for it at all.
        return ("this key already appears earlier in this payload; "
                "a key may be reported once per request")
    if item.key not in known:
        return "no finding with this key on this PR"
    # The refutation IS the evidence. Without it this records a bare contradiction
    # of the judge, which is the same confident-assertion-with-nothing-behind-it
    # that the confirmed findings on PR #64 were — and it would land in a
    # published precision figure. An existing note on an unchanged `refuted` row
    # counts: the evidence is already on the record, and re-reporting a refutation
    # should not require re-typing it.
    held = stored if stored is not None and stored.outcome == item.outcome else None

    def inherits(attr: str) -> bool:
        """Would the stored value survive this item? Not if it is being CLEARED —
        an explicit null retracts, and a rule that read only "was a value sent"
        would let ``{"outcome": "refuted", "note": null}`` pass on the strength of
        the note it is in the act of deleting."""
        if attr in item.model_fields_set and getattr(item, attr) is None:
            return False
        return held is not None and getattr(held, attr) is not None

    if item.outcome == "refuted" and not item.note and not inherits("note"):
        return "refuted needs a note: the refutation is the evidence for it"
    # ...and the same rule for `superseded`, which was asymmetric: the key of the
    # finding that replaced this one is the entire content of that outcome, and
    # it was optional, so `superseded` alone recorded "replaced by something".
    if item.outcome == "superseded" and not item.superseded_by and not inherits("superseded_by"):
        return "superseded needs superseded_by: the key of the finding that replaced it"
    if item.deferred_to and item.outcome != "deferred":
        return f"deferred_to is only meaningful on a deferred outcome, not {item.outcome}"
    if item.superseded_by:
        if item.outcome != "superseded":
            return ("superseded_by is only meaningful on a superseded outcome, "
                    f"not {item.outcome}")
        if item.superseded_by == item.key:
            return "superseded_by names this finding itself"
        if item.superseded_by not in known:
            return "superseded_by names no finding on this PR"
    return None


def _outcome_item(raw: object) -> tuple[OutcomeIn | None, str | None]:
    """One payload entry as a validated item, or the reason it is not one.

    Validated here rather than by FastAPI, which is the point of ``outcomes``
    being a list of raw objects: a typed list is validated whole, so one entry
    with a missing ``key`` or a misspelled field would 422 the request and lose
    every valid sibling — the opposite of what this endpoint promises.
    """
    if not isinstance(raw, dict):
        return None, f"not an object: {_echo(type(raw).__name__)}"
    try:
        return OutcomeIn.model_validate(raw), None
    except ValidationError as e:
        # One line, naming each field and what was wrong with it. Pydantic's own
        # rendering is multi-line with a docs URL per error, which is not what a
        # per-item `reason` string is for.
        parts = [f"{'.'.join(str(p) for p in err['loc']) or 'item'}: {err['msg']}"
                 for err in e.errors()]
        shown = "; ".join(_echo(p) for p in parts[:_MAX_ITEM_ERRORS])
        # ...and say how many were not shown. Dropping the rest silently is the
        # same "a caller told nothing assumes it was told everything" failure the
        # whole endpoint is organised against, one layer down.
        rest = len(parts) - _MAX_ITEM_ERRORS
        return None, f"{shown} (+{rest} more)" if rest > 0 else shown


@router.post(
    "/review/outcomes",
    status_code=status.HTTP_201_CREATED,
    # Declared, because the handler sets the code from what it did and a generated
    # client built off the schema would otherwise be told 201 is the only answer.
    # The 422 is doubly worth naming: FastAPI already uses it for request-shape
    # failures with a `detail` body, and this one is a well-formed request whose
    # every item was refused, carrying the endpoint's own `rejected` list. A
    # client tells them apart by which key is present, never by the code.
    responses={
        200: {"description": "existing outcomes updated, amended or confirmed unchanged"},
        201: {"description": "at least one outcome recorded for the first time"},
        409: {"description": "another writer recorded one of these while this was in flight"},
        422: {"description": "either the request shape was wrong (`detail`) or every item "
                             "in a well-formed request was refused (`rejected`)"},
    },
)
async def record_outcomes(
    body: OutcomesIn,
    response: Response,
    author: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """What actually happened to these findings — the half the judge cannot know.

    A finding's life ended at the judge: ``verdict`` is set once, at review time,
    by a model with no more access to the answer than the reviewer that raised
    the finding, and ``GET /review/stats`` then ranked reviewers on it. So a
    confident wrong finding scored exactly like a real one — three of six
    judge-confirmed P2s on PR #64 were plainly wrong and are still in the board
    as confirmed, and #32 r2's refuted finding is recorded nowhere at all.

    This is the terminal state, per DEFECT rather than per observation: one row
    for each (repo, pr, ``key``), joined to every round that raised it. Rounds are
    what a long fix loop produces, so attaching it to the observation would
    multiply one refutation by the number of rounds — largest exactly where the
    measurement matters most.

    **Who may set what, and what ``attested_by`` is NOT.** #77 is explicit that an
    agent must not mark its own findings ``refuted`` unattended: that is a
    self-grading loop, and #40's constraint applies for the same reason. This API
    cannot tell a fixer from a reviewer — the reviewer is a model name, the caller
    is a board identity — so it does not pretend to enforce it.

    ``set_by`` comes from the token and is proof. ``attested_by`` is **free text
    from the same request that carries the refutation**, so it is a CLAIM that a
    named human signed off, recorded beside who claimed it — the board cannot
    authenticate a person, and an agent that wants to write ``attested_by:
    "rich"`` can. Every place it is published says so: the response splits
    ``unattested_refutations`` out, ``GET /review/stats`` publishes the attested
    counts beside the raw ones, and ``/panel`` renders the claim with its claimant
    rather than as a signature. Read together they say "this agent says a human
    agreed", which is worth having on the record and is not the same thing as a
    human having agreed. An unattended refutation is still worth more here than in
    a PR comment nothing counts; what neither is worth is being counted silently.

    **Re-reporting.** An outcome may move — a deferred finding is later fixed — so
    a repeat updates rather than 409s, and every kind of change is visible:

    * a different ``outcome`` bumps ``revisions``, keeps ``prior_outcome``, and
      clears the fields the caller did not resend (the old note explained the old
      answer);
    * a repeat of the SAME outcome FILLS empty fields and never silently rewrites
      a stored one. Overwriting a stored value — replacing the note that IS the
      evidence for a refutation, or adding an attestation to somebody else's
      unattended one — is a real change, so it bumps ``revisions`` too and comes
      back in ``amended`` naming the fields. A terminal state that flips quietly
      is how an after-the-fact precision figure improves without anybody deciding
      to, and so is a note rewritten under an unchanged verdict;
    * an explicitly-null field CLEARS, which is how a mistaken attestation is
      retracted. Absent and null are different: absent is "nothing to add".
    """
    # Retried once, and the retry is not defensive tidiness: the commonest way two
    # writers race for one (repo, pr, key) is not two agents, it is ONE agent
    # whose request the board accepted and whose client timed out waiting for the
    # response — `qb` gives curl 15 seconds — so the retry arrives with the row
    # already inserted. Both attempts read the stored rows first, so the second
    # one takes the update path and the outcome is the same as if the two had
    # been sequential. Two attempts, never a loop: a third failure is not
    # contention, and a request that keeps retrying itself hides whatever it is.
    for attempt in (1, 2):
        try:
            result = await _apply_outcomes(session, body, author)
            break
        except IntegrityError as e:
            await session.rollback()
            # ONLY the unique constraint is contention. A CHECK violation or a
            # NOT NULL is deterministic: retrying builds the identical invalid
            # row, and reporting it as "another writer got there first; retry"
            # sends the caller round a loop over a bug in this service while
            # hiding it from the logs.
            # `sqlstate` is asyncpg's spelling and `pgcode` is psycopg's. Reading
            # only one fails CLOSED in the wrong direction under the other: every
            # genuine insert race would stop being retried and become a 500.
            code = getattr(e.orig, "sqlstate", None) or getattr(e.orig, "pgcode", None)
            if code != _PG_UNIQUE_VIOLATION:
                raise
            if attempt == 2:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "another writer recorded an outcome for one of these findings "
                    "while this request was in flight; retry",
                ) from None

    if result["rejected"]:
        # Named back, and logged, for the same reason the ingest path names its
        # drops: a caller that is told nothing assumes everything landed, and
        # these are the rows a coverage marker will otherwise report as never
        # recorded.
        # The RESPONSE names every rejection; the log takes a bounded prefix and
        # says how many there were. A shared log is not the place to render a
        # 5,000-item batch of junk in full.
        listed = result["rejected"][:MAX_REJECTIONS_LOGGED]
        _log.warning("review outcomes rejected: %s", json.dumps(
            {"repo": body.repo, "pr": body.pr, "author": author,
             "rejected_total": len(result["rejected"]), "rejected": listed}, default=str))

    # The status code has to agree with the body, because a shell pipeline built
    # around `qb` (a curl wrapper) checks the code and nothing else. 201 only when
    # something was created; 200 when the batch changed or confirmed existing
    # rows; 422 when nothing was accepted at all and something was refused —
    # "created" over a body of twelve rejections is the response lying.
    if result["recorded"]:
        response.status_code = status.HTTP_201_CREATED
    elif result["changed"] or result["amended"] or result["unchanged"]:
        response.status_code = status.HTTP_200_OK
    else:
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    return result


async def _apply_outcomes(session: AsyncSession, body: OutcomesIn, author: str) -> dict:
    """One attempt at recording a batch — see :func:`record_outcomes`.

    Separate so the retry re-reads: the stored rows are what decide insert from
    update, so replaying a failed attempt against the map fetched before it would
    take the same doomed path again.
    """
    known = set((await session.scalars(
        select(ReviewFinding.finding_key)
        .join(ReviewRun, ReviewRun.id == ReviewFinding.run_id)
        .where(ReviewRun.repo == body.repo, ReviewRun.pr == body.pr)
        .distinct()
    )).all())

    # Validate every entry BEFORE the row read, so `sent` names only keys that
    # could be written and one malformed entry costs its own row.
    items: list[tuple[int, OutcomeIn | None, str | None]] = []
    for i, raw in enumerate(body.outcomes):
        if i >= MAX_OUTCOMES:
            # Named rather than 422'd: a caller batching a long fix loop would
            # otherwise lose all 501 rows, when the first 500 were fine.
            items.append((i, None, f"over the {MAX_OUTCOMES}-outcome cap for one request"))
            continue
        item, why = _outcome_item(raw)
        items.append((i, item, why))
    sent = [it.key for _, it, why in items if it is not None and why is None]

    # `with_for_update`, and it is the difference between two writers and a lost
    # one. The insert race is caught by the unique constraint and retried; two
    # writers UPDATING one existing row raise nothing at all — both read, both
    # mutate in Python, and the second commit silently discards the first's note,
    # attestation or revision. Locking the rows this batch will touch makes the
    # second writer wait and then re-read, which is the only way its "fill an
    # empty field" decision is made against what is actually stored.
    stored = {o.finding_key: o for o in (await session.scalars(
        select(ReviewFindingOutcome).where(
            ReviewFindingOutcome.repo == body.repo,
            ReviewFindingOutcome.pr == body.pr,
            ReviewFindingOutcome.finding_key.in_(sent),
        # ORDER BY under FOR UPDATE: two overlapping batches take their row locks
        # in whatever order the planner returns them, and a plan change between
        # the two (index scan one side, bitmap heap the other) is enough to
        # reverse it and deadlock. One agreed order removes the possibility
        # rather than making it rare.
        ).order_by(ReviewFindingOutcome.finding_key).with_for_update()
    )).all()} if sent else {}

    now = datetime.now(UTC)
    recorded: list[str] = []
    changed: list[dict] = []
    amended: list[dict] = []
    unchanged: list[str] = []
    rejected: list[dict] = []
    unattested: list[str] = []
    seen: set[str] = set()

    for i, item, why in items:
        if item is None:
            # The key an unparseable item MEANT, under either spelling the model
            # accepts. Two reasons it is dug out of the raw object rather than
            # left as "item N": a caller using the `finding_key` alias had its
            # rejection reported anonymously, and — the real one — a key that
            # reserves nothing lets a LATER well-formed entry for the same key
            # slip past the once-per-request rule, on the strength of an earlier
            # entry that failed to parse.
            raw = body.outcomes[i]
            key = None
            if isinstance(raw, dict):
                key = raw.get("key") if raw.get("key") is not None else raw.get("finding_key")
            if isinstance(key, str) and key.strip():
                seen.add(key.strip())
            rejected.append({"key": _echo(key) if key is not None else f"item {i}",
                             "reason": why})
            continue
        row = stored.get(item.key)
        reason = _outcome_reason(item, known, row, seen)
        # Every key this payload has spoken about, accepted or refused — so a
        # later duplicate is never told "the first entry was kept" when the first
        # entry was itself rejected.
        seen.add(item.key)
        if reason is not None:
            rejected.append({"key": _echo(item.key), "reason": reason})
            continue
        if row is None:
            if item.outcome == "refuted" and not item.attested_by:
                unattested.append(item.key)
            session.add(ReviewFindingOutcome(
                repo=body.repo, pr=body.pr, finding_key=item.key,
                outcome=item.outcome, note=item.note,
                deferred_to=item.deferred_to, superseded_by=item.superseded_by,
                set_by=author, session=body.session, attested_by=item.attested_by,
            ))
            recorded.append(item.key)
            continue

        if row.outcome != item.outcome:
            row.revisions += 1
            row.prior_outcome = row.outcome
            row.outcome = item.outcome
            # The old answer's explanation does not survive the answer. Anything
            # the caller did not resend is cleared, so a note reading "not a
            # defect: install globs" cannot end up filed under `fixed`.
            for attr in OUTCOME_FIELDS:
                setattr(row, attr, getattr(item, attr))
            changed.append({"key": item.key, "from": row.prior_outcome, "to": row.outcome})
            # Whoever moved the answer owns it. On an unchanged repeat the row
            # keeps its original author (below), so `set_by` names the agent that
            # made the current statement rather than the last one to touch it.
            row.set_by, row.session = author, body.session
            row.updated_at = now
        else:
            # A repeat of the same answer FILLS an empty field and never silently
            # rewrites a stored one. Rewriting is a real change — replacing the
            # note that IS the evidence for a refutation, or adding an
            # attestation to somebody else's unattended one — so it is counted as
            # a revision and named back. An absent field is "nothing to add"; an
            # explicit null CLEARS, which is how a mistaken attestation is
            # retracted without flipping the outcome twice to do it.
            rewrote, filled = [], []
            for attr in OUTCOME_FIELDS:
                if attr not in item.model_fields_set:
                    continue
                value, was = getattr(item, attr), getattr(row, attr)
                if value == was:
                    continue
                (rewrote if was is not None else filled).append(attr)
                setattr(row, attr, value)
            if rewrote or filled:
                # Reported whichever it was, because BOTH move `set_by` and a
                # fill used to move it invisibly: an agent adding an attestation
                # to somebody else's unattended refutation reassigned the
                # authorship of the claim, landed in `unchanged`, and left no
                # trace anywhere in the response. `set_by` is the field #77's
                # self-grading guard is read against, so an edit to it that
                # nothing reports is the one edit that must not be silent.
                row.revisions += 1
                amended.append({"key": item.key, "outcome": row.outcome,
                                "filled": sorted(filled), "rewrote": sorted(rewrote)})
            else:
                unchanged.append(item.key)
            # `set_by` names the agent responsible for the row's CURRENT content,
            # which is whoever last changed any of it — a fill counts. An agent
            # that adds an attestation to somebody else's refutation is making
            # that claim, and leaving `set_by` on the original recorder filed the
            # claim under the wrong name, in the one field that exists to say who
            # is claiming. A genuine no-op changes nothing and therefore steals
            # no authorship — which is the case an idempotent retry hits, and the
            # reason this is not simply "the last toucher wins".
            #
            # `session` travels WITH it, always: they are one provenance pair, and
            # updating the session without the identity leaves a row whose contact
            # details belong to a different agent from its author.
            if rewrote or filled:
                row.set_by, row.session = author, body.session
                # ...and only then. A no-op repeat — the shape an idempotent
                # retry storm has — must not move the row's published timestamp
                # while `set_by` and `revisions` correctly report that nothing
                # happened, or "when was this last decided" answers "just now"
                # for a decision taken last week.
                row.updated_at = now
        # Read off the ROW, after it has been written, not off the payload: a
        # repeat that inherits a stored attestation is attested, and one that
        # explicitly clears it is not. Reading the payload alone reported the
        # first as unattested — the response contradicting the row it had just
        # written, in the direction that cries wolf.
        if row.outcome == "refuted" and not row.attested_by:
            unattested.append(item.key)

    await session.commit()

    return {
        "repo": body.repo,
        "pr": body.pr,
        "recorded": recorded,
        "changed": changed,
        # A repeat that REWROTE a stored field under an unchanged outcome. Its own
        # bucket rather than folded into `changed` (which is about the answer
        # moving) or `unchanged` (which it is not): the fields are named because
        # the one that matters is `note` on a refutation, and a rewritten
        # refutation is a rewritten piece of evidence.
        "amended": amended,
        "unchanged": unchanged,
        "rejected": rejected,
        # Refutations nobody CLAIMS a human signed off. Not an error and not a
        # refusal — an unattended refutation on the record beats a refutation in
        # prose — but the caller is told which of its rows the stats will report
        # as unattested, rather than finding out from the leaderboard.
        "unattested_refutations": unattested,
    }


# ------------------------------------------------------------------ read paths

#: How many paths a run's ``unread_files`` holds, computed in SQL so the column
#: itself never has to be fetched — see :attr:`ReviewRun.unread_files`, which is
#: deferred for exactly this.
#:
#: ``case`` rather than a bare ``jsonb_array_length``, and guarded the same way
#: ``declared_runs`` guards its comparison one endpoint down: that function raises
#: on a jsonb value that is not an array rather than returning NULL, and this API
#: is not the only thing that can write the column. A non-array reads as "nobody
#: said", which is the honest answer for a value this board cannot interpret, and
#: it cannot take a whole page of runs down with it.
_UNREAD_COUNT = case(
    (func.jsonb_typeof(ReviewRun.unread_files) == "array",
     func.jsonb_array_length(ReviewRun.unread_files)),
    else_=None,
)


def _run_view(r: ReviewRun, unread_count: int | None) -> dict:
    """One run as the list and detail views publish it.

    ``unread_count`` is passed in rather than read off ``r``: the column is
    deferred, so touching it here would either refetch every path the count
    exists to avoid or — under async SQLAlchemy, which cannot lazy-load — raise.
    Callers get it from :data:`_UNREAD_COUNT` in their own query.
    """
    return {
        "id": r.id,
        "ts": r.ts.isoformat(),
        "author": r.author,
        "session": r.session,
        "repo": r.repo,
        "pr": r.pr,
        "pr_title": r.pr_title,
        "base": r.base_branch,
        # The commit this round read, which `base` (a branch name) cannot say.
        "head_sha": r.head_sha,
        # ...and both ends of what it was judged against. Two scalars, so they
        # ride the list view where the path lists deliberately do not: the whole
        # point of them is to be compared against the repo's CURRENT base without
        # first fetching each run one at a time.
        "merge_base": r.merge_base,
        "base_sha": r.base_sha,
        # The COUNT, not the paths — the same trade `changed_files_total` makes
        # two lines down, and for the same reason. The cost `changed_files` was
        # kept out of this view for is response SIZE: `unread_files` is bounded
        # only by MAX_CHANGED_FILES entries of MAX_PATH_CHARS each, so
        # `GET /reviews?limit=500` could serialise millions of path strings.
        # "Bounded by what one round could not read" is a fact about a real panel
        # and not a bound this ingest enforces on an authenticated-but-unbounded
        # sender, which is precisely why the cap exists. `GET /review/{id}`
        # carries the list.
        #
        # And the count is computed in SQL (:data:`_UNREAD_COUNT`) against a
        # DEFERRED column, because the first version of this argument only saved
        # the serialisation: `len(r.unread_files)` still had Postgres ship every
        # path of every row to the app before Python counted them. A defence
        # against a page of file dumps that still transfers the file dump is a
        # comment, not a defence.
        #
        # Unmasked in the sense that matters, like `stop_veto` further down: NULL
        # here means the panel never said and 0 means it said nothing was cut, so
        # the three states survive into the list view rather than being folded
        # into one by the read path — which is the collapse the storage side is
        # built to prevent.
        "unread_files_count": unread_count,
        # Four integer keys at most, so this one rides everywhere: it is the
        # round's own statement about its attribution and the thing a reader needs
        # beside every per-finding bucket.
        "provenance_counts": r.provenance_counts,
        "changed_lines": r.changed_lines,
        # The count only — the paths themselves are per-run children and would
        # turn every page of `GET /reviews` into a file dump. `GET /review/{id}`
        # carries the list; this is what a run LIST needs to know a list exists.
        "changed_files_total": r.changed_files_total,
        # As of this run, not live — the run's own `ts` is the staleness signal.
        "pr_state": r.pr_state,
        "is_draft": r.is_draft,
        "diff_chars": r.diff_chars,
        "diff_truncated": r.diff_truncated,
        "judged": r.judged,
        "judge_model": r.judge_model,
        "judge_skip": r.judge_skip,
        "coverage_note": r.coverage_note,
        "round": r.round,
        "cycle": r.cycle,
        "new_findings": r.new_findings,
        "stopped": r.stopped,
        "stop_reason": r.stop_reason,
        "stop_confident": r.stop_confident,
        # Unmasked, like `could_not_assess` two fields down: ingest stores this
        # AS SENT so that "the stopping rule ran and vetoed nothing" ([]) and "no
        # panel ever said" (NULL) stay apart, and masking it on read handed every
        # consumer exactly the collapse the storage side argues against.
        "stop_veto": r.stop_veto,
        "sonar_gate": r.sonar_gate,
        "ci_status": r.ci_status,
        "reviewers_selected": r.reviewers_selected or [],
        "reviewers_override": r.reviewers_override,
        "skipped": r.skipped or [],
        "confirmed": r.n_confirmed,
        "dismissed": r.n_dismissed,
        "unjudged": r.n_unjudged,
        "sonar": r.n_sonar,
    }


def _prov_measured(c: ReviewReviewer, run: ReviewRun) -> bool:
    """Did this run attribute anything at all, for this member's scorecard?

    The same question ``review_stats.provenance_runs`` answers for a window, at
    the grain a single card is read on, and by the same rule, down to the judged
    guard: a JUDGED run sent a non-empty tally, OR this member's own counters are
    not all zero.

    The second half is not belt-and-braces — a payload that attributes its
    findings while sending no run tally has a real split and no tally to prove it.
    The judged guard is not decoration either: these counters are tallied over
    confirmed findings, so an unjudged run's card can only hold zeros, and its
    tally would otherwise present four of them as a measurement.

    A member that did NOT run still reads as measured on a run that attributed,
    and that is the same answer ``provenance_runs`` gives for the same row: the
    round asked the question, this seat contributed nothing to it, and its zero is
    as honest as the ``raised: 0`` beside it. "Measured" is a fact about the run;
    ``ran`` is the field that says whether this seat was in it.
    """
    return (run.judged and bool(run.provenance_counts)) or any(
        getattr(c, col) for col in PROVENANCE_COUNTER.values())


def _card_view(c: ReviewReviewer, run: ReviewRun) -> dict:
    return {
        "name": c.name,
        "model": c.model,
        "effort": c.effort,
        "ran": c.ran,
        "skip_reason": c.skip_reason,
        "max_diff_chars": c.max_diff_chars,
        "truncated": c.truncated,
        "duration_ms": c.duration_ms,
        "could_not_assess": c.could_not_assess,
        "unstructured": c.unstructured,
        # #113. Read back out as well as stored: a column nothing exposes is a
        # column nothing can be measured with, which is the same half-built shape
        # v2.26 records for the four fields it landed (#93) — stored is not shipped.
        "absent": c.absent,
        "code_blind": c.code_blind,
        "argv_capped": c.argv_capped,
        "rereview_flagged": c.rereview_flagged,
        "input_tokens": c.input_tokens,
        "output_tokens": c.output_tokens,
        "cached_input_tokens": c.cached_input_tokens,
        "reasoning_tokens": c.reasoning_tokens,
        # float, not the Decimal the column holds: JSON has no decimal type, and
        # a client that sees a quoted string here would have to know to parse it.
        "cost_usd": float(c.cost_usd) if c.cost_usd is not None else None,
        "raised": c.raised,
        "confirmed": c.confirmed,
        "dismissed": c.dismissed,
        "unjudged": c.unjudged,
        "solo": c.solo,
        "shared": c.shared,
        "sev_stricter": c.sev_stricter,
        "sev_agree": c.sev_agree,
        "sev_looser": c.sev_looser,
        "p1": c.p1, "p2": c.p2, "p3": c.p3, "p4": c.p4,
        # Keyed by the panel's own bucket names rather than the column names, so
        # one vocabulary crosses the whole contract — payload, storage, API,
        # page. Over this member's CONFIRMED findings only, which is a narrower
        # population than the run's `provenance_counts` beside it.
        #
        # **null when this run attributed nothing**, which is the same care
        # `review_stats` takes with `provenance_runs` and for the same reason: the
        # columns behind these four are NOT NULL, so a pre-v2.26 run — or any run
        # whose panel sent no provenance — would otherwise render as a scorecard
        # stating four honest zeros that mean nothing. A zero is a claim
        # everywhere else in this feature, so it must not be manufactured here.
        # The run's own `provenance_counts` is in the same payload; this says
        # which of the two readings applies to this card.
        "provenance": ({b: getattr(c, col) for b, col in PROVENANCE_COUNTER.items()}
                       if _prov_measured(c, run) else None),
    }


def _report_view(r: ReviewFindingReport) -> dict:
    return {
        "reviewer": r.reviewer,
        "severity": r.severity,
        "line": r.line,
        "account": r.account,
        "needs_rereview": r.needs_rereview,
    }


def _outcome_view(o: ReviewFindingOutcome) -> dict:
    """What happened to this defect afterwards — never merged into the verdict.

    ``verdict`` is the judge's ruling at review time and this is what somebody
    found out by acting on it, and the whole value of the pair is that they can
    disagree: a ``confirmed`` finding with a ``refuted`` outcome is precisely the
    case #77 was filed for. Folding one into the other on read would hand every
    consumer back the collapse this feature exists to undo.
    """
    return {
        "outcome": o.outcome,
        "note": o.note,
        "deferred_to": o.deferred_to,
        "superseded_by": o.superseded_by,
        "set_by": o.set_by,
        # The recorder's session, beside its identity — the same pairing a run
        # publishes, and what lets a reader reach whoever wrote this about a
        # refutation they disagree with. Stored since v2.37 and, until this was
        # noticed, never handed back by any read path.
        "session": o.session,
        # Who the recorder SAYS signed this off; absent = unattended. A claim
        # carried beside `set_by`, which is proof — the board cannot authenticate
        # a human. Published rather than hidden because #77's rule is that an
        # agent must not grade its own findings unattended, and this pair is what
        # a reader checks that against.
        "attested_by": o.attested_by,
        "ts": o.ts.isoformat(),
        "updated_at": o.updated_at.isoformat() if o.updated_at else None,
        # A terminal state that moved, and what it moved from. Silence here is a
        # first answer; a count is an answer that changed.
        "revisions": o.revisions,
        "prior_outcome": o.prior_outcome,
    }


async def _outcomes_for(
    session: AsyncSession, repo: str, pr: int, keys: list[str]
) -> dict[str, ReviewFindingOutcome]:
    """Terminal outcomes for these defects, by key, in one query rather than N."""
    if not keys:
        return {}
    rows = (await session.scalars(
        select(ReviewFindingOutcome).where(
            ReviewFindingOutcome.repo == repo,
            ReviewFindingOutcome.pr == pr,
            ReviewFindingOutcome.finding_key.in_(keys),
        )
    )).all()
    return {o.finding_key: o for o in rows}


def _finding_view(f: ReviewFinding, reports: list[ReviewFindingReport],
                  outcome: ReviewFindingOutcome | None = None) -> dict:
    return {
        "key": f.finding_key,
        "verdict": f.verdict,
        "severity": f.severity,
        "file": f.file,
        "line": f.line,
        "title": f.title,
        "detail": f.detail,
        "reason": f.reason,
        "reviewers": f.reviewers or [],
        "related": f.related or [],
        "needs_rereview": f.needs_rereview,
        "new_this_round": f.new_this_round,
        # Beside `new_this_round`, which it splits in two: that field says the
        # defect is new to this cycle, this one says whether the last fix pass
        # caused it. null = the question does not arise (round 1, outside a cycle,
        # or a repeat) and is NOT the `"unknown"` bucket.
        "provenance": f.provenance,
        # What happened to the DEFECT afterwards (v2.37), null where nobody has
        # said yet. It is deliberately the same object on every observation of one
        # defect: the outcome is a fact about the defect, not about the round that
        # happened to raise it, and a per-round outcome is the multiplication the
        # storage side refuses.
        "outcome": _outcome_view(outcome) if outcome is not None else None,
        "reported_by": [_report_view(r) for r in reports],
    }


async def _reports_by_finding(
    session: AsyncSession, finding_ids: list[int]
) -> dict[int, list[ReviewFindingReport]]:
    """Every account for these findings, in one query rather than N."""
    if not finding_ids:
        return {}
    rows = (await session.scalars(
        select(ReviewFindingReport)
        .where(ReviewFindingReport.finding_id.in_(finding_ids))
        .order_by(ReviewFindingReport.reviewer)
    )).all()
    out: dict[int, list[ReviewFindingReport]] = {}
    for r in rows:
        out.setdefault(r.finding_id, []).append(r)
    return out


def _since_clause(since: str | None, days: int | None):
    """``since`` as an ISO instant, or ``days`` as a lookback. Neither = all time."""
    if since:
        try:
            ts = datetime.fromisoformat(since)
        except ValueError as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"since={since!r} is not an ISO timestamp"
            ) from e
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts
    if days:
        return datetime.now(UTC) - timedelta(days=days)
    return None


@router.get("/reviews")
async def list_reviews(
    _reader: str = Depends(reader),
    repo: str | None = Query(None),
    pr: int | None = Query(None),
    author: str | None = Query(None),
    since: str | None = Query(None, description="ISO timestamp"),
    days: int | None = Query(None, ge=1, le=3650),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Recorded panel runs, newest first."""
    # The run, plus its unread-path COUNT computed in the database. The column
    # itself is deferred and deliberately not fetched here — see `_run_view`.
    stmt = select(ReviewRun, _UNREAD_COUNT)
    if repo is not None:
        stmt = stmt.where(ReviewRun.repo == repo)
    if pr is not None:
        stmt = stmt.where(ReviewRun.pr == pr)
    if author is not None:
        author = await _authored_as(session, author)
        stmt = stmt.where(ReviewRun.author == author)
    cutoff = _since_clause(since, days)
    if cutoff is not None:
        stmt = stmt.where(ReviewRun.ts >= cutoff)
    stmt = stmt.order_by(ReviewRun.ts.desc(), ReviewRun.id.desc()).limit(limit)

    rows = list((await session.execute(stmt)).all())
    if not rows:
        return []
    runs = [r for r, _ in rows]
    unread_counts = {r.id: n for r, n in rows}
    cards = list(
        (await session.scalars(
            select(ReviewReviewer).where(ReviewReviewer.run_id.in_([r.id for r in runs]))
        )).all()
    )
    run_by_id = {r.id: r for r in runs}
    by_run: dict[int, list[dict]] = {}
    for c in cards:
        by_run.setdefault(c.run_id, []).append(_card_view(c, run_by_id[c.run_id]))
    return [{**_run_view(r, unread_counts[r.id]),
             "reviewers": sorted(by_run.get(r.id, []), key=lambda c: c["name"])}
            for r in runs]


@router.get("/review/stats")
async def review_stats(
    _reader: str = Depends(reader),
    repo: str | None = Query(None),
    author: str | None = Query(None),
    since: str | None = Query(None, description="ISO timestamp"),
    days: int | None = Query(None, ge=1, le=3650),
    judged_only: bool = Query(
        True, description="count only judge-adjudicated runs (required for precision)"
    ),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Per-model and per-agent aggregates over the recorded runs.

    ``by_model`` is grouped by (reviewer, model, effort) — the same vendor at a
    different tier is a different competitor, which is the whole question.

    Each ``by_model`` row also carries ``provenance`` (v2.26): of the defects that
    member found, how many the previous fix pass *introduced* against how many the
    previous round *missed*. #48's second axis, and the one a confirmed count
    cannot see — finding a regression somebody else just wrote and finding a
    defect that has been there for months are different competencies. Read it
    against ``provenance_runs``, which says how many of the group's runs could
    attribute at all: the counters are NOT NULL, so a window of older runs
    reports four honest zeros that mean nothing.

    ``by_provenance`` is the same split over the window's confirmed findings,
    counted once each rather than once per member that raised them, plus
    ``not_attributed`` for every finding the question never reached. That is the
    number to read at the cap: how much of what this loop found did it inflict on
    itself.

    "Counted once each" is **within a run**, not within a cycle: this counts
    OBSERVATIONS, like every other number on this page. A defect raised in rounds
    2, 3 and 4 of one cycle is three rows — once as ``introduced`` in round 2 and
    twice as ``not_attributed`` in the rounds after, where the question does not
    arise for a defect an earlier round already raised. So repeats inflate
    ``not_attributed`` and leave the four buckets alone, and the ratio the
    ``/panel`` tile computes (``introduced / traced``) is unaffected — but
    ``not_attributed`` is not a count of distinct defects and should not be read
    against one. ``GET /review/findings`` is where a chain collapses to a defect.

    ``precision_after`` (v2.37) is ``precision``'s honest twin: the same ratio
    scored against what happened to the finding rather than against the judge's
    opinion of it, over ``fixed`` and ``refuted`` only. The GAP between the two is
    the measurement #77 asks for — how often a confident, judge-confirmed finding
    survives contact with the code. ``outcome`` / ``outcome_attested`` /
    ``outcomes_recorded`` sit beside it, and ``by_outcome`` is the same split
    across the window.

    **These are the one set of counts here measured per DEFECT rather than per
    observation**, and ``confirmed_defects`` is published so the two denominators
    cannot be confused. An outcome is one fact about one defect; a defect raised
    in rounds 2, 3 and 4 of a cycle is three observations, so counting outcomes
    per observation would weight a single refutation by how many rounds the fix
    loop took — heaviest on exactly the PRs where a reviewer's reliability is the
    question. Nobody is obliged to record an outcome, so read the counts against
    ``outcomes_recorded``: zeros mean unrecorded far more often than they mean
    nothing happened.

    Every figure here comes from six separate statements against one connection,
    so under PostgreSQL's default READ COMMITTED each sees its own snapshot: a run
    recorded between them can appear in one aggregate and not another, leaving the
    sums, ``provenance_runs`` and the page's percentages fractionally out of step
    within a single response. That has been true of this endpoint since it had
    three statements; it is a stats page over a window measured in days, and
    paying for a repeatable-read transaction to make a sub-second boundary exact
    is a worse trade than saying so here.
    """
    filters = []
    if repo is not None:
        filters.append(ReviewRun.repo == repo)
    if author is not None:
        author = await _authored_as(session, author)
        filters.append(ReviewRun.author == author)
    cutoff = _since_clause(since, days)
    if cutoff is not None:
        filters.append(ReviewRun.ts >= cutoff)
    if judged_only:
        filters.append(ReviewRun.judged.is_(True))

    totals = (
        await session.execute(
            select(
                func.count(ReviewRun.id),
                func.count(func.distinct(func.concat(ReviewRun.repo, "#", ReviewRun.pr))),
                func.count(func.distinct(ReviewRun.repo)),
                func.min(ReviewRun.ts),
                func.max(ReviewRun.ts),
            ).where(*filters)
        )
    ).one()

    # Labelled rather than unpacked positionally: this grew to two dozen
    # aggregates, and a tuple that long is one inserted column away from
    # silently reporting `dismissed` under `unjudged`.
    # Every token column, because each is independently optional: a scorecard
    # carrying only a cached or reasoning figure has still been instrumented, and
    # counting it as unmeasured would make `token_runs` disagree with the sums
    # sitting next to it.
    tok = (ReviewReviewer.input_tokens, ReviewReviewer.output_tokens,
           ReviewReviewer.cached_input_tokens, ReviewReviewer.reasoning_tokens)
    model_rows = (
        await session.execute(
            select(
                ReviewReviewer.name.label("name"),
                ReviewReviewer.model.label("model"),
                ReviewReviewer.effort.label("effort"),
                func.count(ReviewReviewer.id).label("runs"),
                func.count(ReviewReviewer.id)
                    .filter(ReviewReviewer.ran.is_(False)).label("skipped"),
                func.sum(ReviewReviewer.raised).label("raised"),
                func.sum(ReviewReviewer.confirmed).label("confirmed"),
                func.sum(ReviewReviewer.dismissed).label("dismissed"),
                func.sum(ReviewReviewer.unjudged).label("unjudged"),
                func.sum(ReviewReviewer.solo).label("solo"),
                func.sum(ReviewReviewer.p1).label("p1"),
                func.sum(ReviewReviewer.p2).label("p2"),
                func.sum(ReviewReviewer.p3).label("p3"),
                func.sum(ReviewReviewer.p4).label("p4"),
                func.avg(ReviewReviewer.duration_ms)
                    .filter(ReviewReviewer.duration_ms.isnot(None)).label("avg_ms"),
                func.sum(ReviewReviewer.shared).label("shared"),
                func.sum(ReviewReviewer.sev_stricter).label("stricter"),
                func.sum(ReviewReviewer.sev_agree).label("agree"),
                func.sum(ReviewReviewer.sev_looser).label("looser"),
                func.sum(ReviewReviewer.duration_ms).label("total_ms"),
                # How often this member reviewed a PREFIX of the diff. A row that
                # says "12 confirmed" reads differently when half of those runs
                # only showed it half the change.
                func.count(ReviewReviewer.id)
                    .filter(ReviewReviewer.truncated.is_(True)).label("truncated_runs"),
                # Runs where it said what it could not judge. A member that was
                # never asked (pre-v2.15) must not read as one that declared
                # nothing. Deliberately NOT `jsonb_array_length(...) > 0`: this
                # column holds JSON `null` for "not asked" (SQLAlchemy's JSONB
                # rendering of a Python None), that function ERRORS on a scalar
                # rather than returning NULL, and SQL gives no evaluation-order
                # guarantee that a typeof guard beside it would run first. A
                # comparison against an empty array is total over every jsonb
                # value. It is built server-side rather than cast from "[]",
                # because a Python string bound to a JSONB parameter serialises
                # to the jsonb *string* `"[]"`, which no array ever equals — so
                # every row would count as a declared gap.
                func.count(ReviewReviewer.id).filter(
                    func.jsonb_typeof(ReviewReviewer.could_not_assess) == "array",
                    ReviewReviewer.could_not_assess != func.jsonb_build_array(),
                ).label("declared_runs"),
                # Runs where its reply did not parse. Those land on
                # could_not_assess NULL for a reason that is not "never asked",
                # and a member whose CLI keeps producing unreadable output is a
                # coverage failure that no other counter here can show.
                func.count(ReviewReviewer.id)
                    .filter(ReviewReviewer.unstructured.is_(True)).label("unstructured_runs"),
                func.sum(ReviewReviewer.rereview_flagged).label("rereview_flagged"),
                func.sum(ReviewReviewer.input_tokens).label("input_tokens"),
                func.sum(ReviewReviewer.output_tokens).label("output_tokens"),
                func.sum(ReviewReviewer.cached_input_tokens).label("cached_input_tokens"),
                func.sum(ReviewReviewer.reasoning_tokens).label("reasoning_tokens"),
                func.sum(ReviewReviewer.cost_usd).label("cost_usd"),
                # How many of these scorecards carried any token figure at all.
                # Without it a sum over a half-instrumented window reads as the
                # whole window's spend, and "tokens per run" comes out low by
                # however many runs said nothing.
                #
                # Counted over ALL rows in the group, `ran` or not, because that
                # is the population the sums beside it cover: a member that
                # burned tokens and then timed out spent them, so `review_llm`
                # reports usage on the skip path too. Read against `ran` this
                # would exceed it — a measured failure is in the numerator and
                # not the denominator — so the coverage marker suppressed itself
                # on exactly the groups that had one. Compare it to `runs`.
                func.count(ReviewReviewer.id)
                    .filter(sa_or(*(c.isnot(None) for c in tok))).label("token_runs"),
                func.count(ReviewReviewer.id)
                    .filter(ReviewReviewer.cost_usd.isnot(None)).label("cost_runs"),
                # #48's axis: of the defects this member found, how many did the
                # previous fix pass introduce and how many had been sitting there
                # all along? Different competencies, opposite remedies, and a
                # confirmed count cannot see either.
                *(func.sum(getattr(ReviewReviewer, col)).label(col)
                  for col in PROVENANCE_COUNTER.values()),
                # ...and how much of the group those sums actually cover, the same
                # job `token_runs` does one field up. A scorecard counter cannot
                # hold "not recorded" — it is NOT NULL like every sibling — so a
                # window of pre-v2.26 runs would otherwise read as a panel that
                # never once caught a regression. Coverage is a fact about the
                # RUN: it attributed if the panel sent a non-empty tally. `{}` is
                # excluded deliberately — a round 1 has no earlier round to
                # attribute against, so it is not a run that found nothing, it is
                # a run that was never asked.
                #
                # `jsonb_typeof` guards the comparison, and the empty object is
                # built server-side: a Python `{}` bound to a JSONB parameter is a
                # different thing from the jsonb `{}` this needs to compare
                # against, the same trap `declared_runs` documents above.
                #
                # OR'd with the scorecard's own counters, and that second half is
                # not belt-and-braces: without it the marker could read 0 beside
                # sums that do not, because it was measuring a DIFFERENT field
                # from the one it annotates. A caller that attributes its findings
                # and sends no run tally would have its real split hidden by a
                # marker saying nothing was measured. The invariant a reader needs
                # is that a non-zero sum always comes with non-zero coverage, and
                # only counting the counters gives it.
                #
                # The tally half is restricted to JUDGED runs, and that is not a
                # tightening for its own sake. The counters are tallied under the
                # `confirmed` branch of `_scorecards`, so an unjudged run can only
                # ever contribute zeros to the sums — while its non-empty tally
                # made it count as coverage. A reader following the documented
                # rule ("read the sums against `provenance_runs`") then saw a
                # covered window with zero `introduced` and concluded the member
                # catches no regressions, when nothing in that run was adjudicated
                # at all. `judged_only=true` hid it and is not the default. The
                # counter half needs no such guard: a counter can only be non-zero
                # on a judged run.
                func.count(ReviewReviewer.id).filter(sa_or(
                    sa_and(ReviewRun.judged.is_(True),
                           func.jsonb_typeof(ReviewRun.provenance_counts) == "object",
                           ReviewRun.provenance_counts != func.jsonb_build_object()),
                    *(getattr(ReviewReviewer, col) > 0
                      for col in PROVENANCE_COUNTER.values()),
                )).label("provenance_runs"),
                # The population `total_tokens` is actually a sum OVER, which is
                # not `token_runs`. `total_tokens` is input+output only, while
                # `token_runs` counts a row carrying ANY of the four columns — so
                # a run that reported only `cached_input_tokens` (legal, and
                # pinned as "instrumented" by its own test) inflated the
                # denominator while adding nothing to the numerator, and
                # `tokens_per_run` understated by up to half. A ratio has to be
                # over one population; this is the one the numerator comes from.
                func.count(ReviewReviewer.id).filter(sa_or(
                    ReviewReviewer.input_tokens.isnot(None),
                    ReviewReviewer.output_tokens.isnot(None))).label("billable_runs"),
            )
            .join(ReviewRun, ReviewRun.id == ReviewReviewer.run_id)
            .where(*filters)
            .group_by(ReviewReviewer.name, ReviewReviewer.model, ReviewReviewer.effort)
        )
    ).all()

    # --- precision after the fact (v2.37). What happened to the defects each
    # member found, once somebody acted on them: the judge's verdict is a
    # judgement made at review time by a model with no more access to the answer
    # than the reviewer, so `precision` above rewards a confident finding and
    # `precision_after` is the same ratio scored against the code.
    #
    # Per DEFECT and not per observation, which makes it the one aggregate on this
    # page with a different grain, deliberately: an outcome is one fact about one
    # defect, and a defect raised in rounds 2, 3 and 4 is three observations. A
    # per-observation count would weight one refutation by how many rounds the fix
    # loop took — largest exactly on the PRs a reviewer's reliability matters most
    # for. `confirmed_defects` is published beside it so the two denominators are
    # never mistaken for each other.
    #
    # Attribution is `ReviewFinding.reviewers`, the same population `_scorecards`
    # tallies `confirmed` over, rather than the per-reporter rows: a caller that
    # sent no `reported_by` has no rows there, and its members would silently
    # score no outcomes at all while still showing a confirmed count.
    # A row constructor, not a `concat` with a separator. `repo` and
    # `finding_key` are both free text — the panel supplies its own keys and this
    # release's own docs are full of refs containing `#` — so any separator can
    # occur inside a value, and two different (repo, pr, key) triples that
    # concatenate to one string collapse into a single counted defect. Always in
    # the undercounting direction, and undetectably. `COUNT(DISTINCT (a,b,c))`
    # compares the fields, so no spelling of a value can forge another triple.
    defect = tuple_(ReviewRun.repo, ReviewRun.pr, ReviewFinding.finding_key)
    outcome_join = sa_and(
        ReviewFindingOutcome.repo == ReviewRun.repo,
        ReviewFindingOutcome.pr == ReviewRun.pr,
        ReviewFindingOutcome.finding_key == ReviewFinding.finding_key,
    )
    # LEFT, so a defect nobody has ruled on lands in the NULL bucket and is
    # counted as coverage-missing rather than dropped: a ratio over only the
    # defects somebody bothered to record is the most flattering possible reading.
    outcome_rows = (
        await session.execute(
            select(
                ReviewReviewer.name.label("name"),
                ReviewReviewer.model.label("model"),
                ReviewReviewer.effort.label("effort"),
                ReviewFindingOutcome.outcome.label("outcome"),
                func.count(func.distinct(defect)).label("defects"),
                func.count(func.distinct(defect))
                    .filter(ReviewFindingOutcome.attested_by.isnot(None)).label("attested"),
            )
            .select_from(ReviewFinding)
            .join(ReviewRun, ReviewRun.id == ReviewFinding.run_id)
            .join(ReviewReviewer, sa_and(
                ReviewReviewer.run_id == ReviewFinding.run_id,
                # The member is credited on the finding. `@>` against an array
                # built server-side from the column: a Python list bound as a
                # JSONB parameter is the trap `declared_runs` documents, one
                # operator over.
                ReviewFinding.reviewers.op("@>")(
                    func.jsonb_build_array(ReviewReviewer.name)),
            ))
            .outerjoin(ReviewFindingOutcome, outcome_join)
            .where(*filters, ReviewFinding.verdict == "confirmed")
            .group_by(ReviewReviewer.name, ReviewReviewer.model, ReviewReviewer.effort,
                      ReviewFindingOutcome.outcome)
        )
    ).all()

    # One defect has exactly one outcome row, so a group's buckets partition its
    # distinct defects and summing them is not a double count — the property the
    # unique constraint on (repo, pr, finding_key) exists to give.
    outcomes_by_group: dict[tuple[str, str | None, str | None], dict] = {}
    for r in outcome_rows:
        g = outcomes_by_group.setdefault(
            (r.name, r.model, r.effort),
            {"counts": dict.fromkeys(OUTCOMES, 0), "attested": dict.fromkeys(OUTCOMES, 0),
             "recorded": 0, "defects": 0},
        )
        n, att = int(r.defects or 0), int(r.attested or 0)
        g["defects"] += n
        if r.outcome is None:
            continue
        # A value outside the vocabulary cannot come from this API — the column
        # has a CHECK — so it is surfaced under its own name rather than folded
        # into a bucket it is not, the rule `by_provenance` follows.
        g["counts"][r.outcome] = g["counts"].get(r.outcome, 0) + n
        g["attested"][r.outcome] = g["attested"].get(r.outcome, 0) + att
        g["recorded"] += n

    by_model = []
    for r in model_rows:
        confirmed, dismissed = int(r.confirmed or 0), int(r.dismissed or 0)
        raised = int(r.raised or 0)
        ruled = confirmed + dismissed
        runs, skipped = r.runs, r.skipped
        ran = runs - skipped
        shared = int(r.shared or 0)
        stricter, agree, looser = int(r.stricter or 0), int(r.agree or 0), int(r.looser or 0)
        rated = stricter + agree + looser
        avg_ms, total_ms = r.avg_ms, r.total_ms
        total_ms = int(total_ms) if total_ms is not None else None

        # Sums stay None when nothing in the group reported — "not instrumented"
        # must not render as a reviewer that spent zero tokens.
        toks = {k: (int(v) if v is not None else None) for k, v in (
            ("input_tokens", r.input_tokens),
            ("output_tokens", r.output_tokens),
            ("cached_input_tokens", r.cached_input_tokens),
            ("reasoning_tokens", r.reasoning_tokens),
        )}
        # Input + output only. Reasoning is inside `output` for some vendors and
        # beside it for others, and cached input is a slice of `input`, so adding
        # either would double-count precisely the seats being compared.
        billable = [t for t in (toks["input_tokens"], toks["output_tokens"]) if t is not None]
        total_tokens = sum(billable) if billable else None
        token_runs, cost_runs, billable_runs = r.token_runs, r.cost_runs, r.billable_runs
        cost = float(r.cost_usd) if r.cost_usd is not None else None

        # What became of the defects this member found. Absent from the map only
        # when it was credited on no confirmed finding in the window at all, which
        # is a real zero rather than missing coverage.
        og = outcomes_by_group.get((r.name, r.model, r.effort))
        outcome_counts = og["counts"] if og else dict.fromkeys(OUTCOMES, 0)
        outcome_attested = og["attested"] if og else dict.fromkeys(OUTCOMES, 0)
        outcomes_recorded, confirmed_defects = (og["recorded"], og["defects"]) if og else (0, 0)
        # Fixed against refuted, and nothing else: `deferred` and `superseded` are
        # decisions about what to do next, so counting either would let "we never
        # got to it" read as "it was real". None where nobody has scored one of
        # this member's findings yet — the same rule as `precision`, where "the
        # judge never ruled" must not render as "everything it raised was wrong".
        #
        # Published as `outcomes_scored`, because it is the population
        # `precision_after` is actually over and `outcomes_recorded` is NOT: a
        # member with 1 fixed and 11 deferred has 12 recorded outcomes and one
        # scored defect, so a client marking thin ratios on the recorded count
        # renders a confident 100% off a single finding — the exact over-reading
        # a thinness marker exists to prevent, in the flattering direction.
        scored = sum(outcome_counts.get(b, 0) for b in OUTCOMES_SCORED)

        by_model.append({
            "reviewer": r.name,
            "model": r.model,
            "effort": r.effort,
            "runs": runs,
            "ran": ran,
            "skipped_runs": skipped,
            "raised": raised,
            "confirmed": confirmed,
            "dismissed": dismissed,
            "unjudged": int(r.unjudged or 0),
            "solo": int(r.solo or 0),
            # Findings someone else raised too. Its complement is a superset of
            # `solo` — a lone reporter is either the only one who saw it or the
            # only one who was wrong, and precision is what separates those.
            "shared": shared,
            "consensus_rate": round(shared / raised, 3) if raised else None,
            # None, not 0.0 — "the judge never ruled on anything it raised" is a
            # different statement from "everything it raised was wrong".
            "precision": round(confirmed / ruled, 3) if ruled else None,
            "confirmed_per_run": round(confirmed / ran, 2) if ran else None,
            "p1": int(r.p1 or 0), "p2": int(r.p2 or 0),
            "p3": int(r.p3 or 0), "p4": int(r.p4 or 0),
            "sev_stricter": stricter,
            "sev_agree": agree,
            "sev_looser": looser,
            # Needs `reported_by` severities to be non-null, so it stays None for
            # every pre-v2.11 run rather than reading as perfect disagreement.
            "severity_calibration": round(agree / rated, 3) if rated else None,
            # The coverage side of a scorecard: how often this member reviewed
            # only part of the diff, how often it said so about something else,
            # and how many fixes it asked to have re-read. A reviewer that
            # reliably declares what it could not see is worth more than one that
            # silently reports clean, and nothing else here tells them apart.
            "truncated_runs": r.truncated_runs,
            "declared_gaps_runs": r.declared_runs,
            # Runs whose reply did not parse at all: its findings were kept as one
            # raw block, and anything it might have declared was lost. Reported
            # beside the declarations because it is the reason some of those are
            # null — not because the member had nothing to say.
            "unstructured_runs": r.unstructured_runs,
            "rereview_flagged": int(r.rereview_flagged or 0),
            "avg_duration_ms": round(float(avg_ms)) if avg_ms is not None else None,
            # The cost side of "is the expensive tier worth it": time spent per
            # finding that survived the judge, not per finding raised.
            # `is not None` on the numerator, positivity on the denominator
            # only. Guarding both on truthiness rendered a genuinely recorded
            # zero as null — and null means *not recorded* everywhere else in
            # this feature, never *spent nothing*. A free tier and a fully
            # cached run a vendor states at $0 are real measurements; the two
            # states the whole thing is built on collapsed in exactly the
            # derived fields the page puts in front of a reader.
            "ms_per_confirmed": (round(total_ms / confirmed)
                                 if total_ms is not None and confirmed else None),

            # --- tokens (v2.19). Only ever compare these BETWEEN ROWS SHARING A
            # `reviewer`: different vendors have different tokenizers and
            # different cache semantics, so a cross-vendor ranking on them is
            # noise dressed as a measurement. Within a vendor they are the
            # sharpest form of "is the expensive tier worth it".
            **toks,
            "total_tokens": total_tokens,
            # How much of this group is actually instrumented. A client that
            # renders the sums without it will present a partial window as a
            # complete one.
            "token_runs": token_runs,
            "cost_runs": cost_runs,
            # Named in the response, not just used, so a client can see which
            # population the average is over instead of assuming it matches
            # `token_runs`. They differ exactly when a run reported a cached or
            # reasoning figure and neither input nor output.
            "billable_runs": billable_runs,
            "tokens_per_run": (round(total_tokens / billable_runs)
                               if total_tokens is not None and billable_runs else None),
            "tokens_per_confirmed": (round(total_tokens / confirmed)
                                     if total_tokens is not None and confirmed else None),
            # Stated by the vendor or absent — never derived from a price table,
            # so a null here is "this vendor doesn't say", not "this was free".
            "cost_usd": cost,
            "cost_per_confirmed": (round(cost / confirmed, 4)
                                   if cost is not None and confirmed else None),

            # --- provenance (v2.26). Read these against `provenance_runs`, never
            # alone: the counters are NOT NULL, so a group whose runs all predate
            # the measurement reports four honest zeros that mean nothing at all.
            # Keyed by the panel's bucket names so one vocabulary spans the
            # payload, the storage, this response and the page.
            "provenance": {b: int(getattr(r, col) or 0)
                           for b, col in PROVENANCE_COUNTER.items()},
            "provenance_runs": r.provenance_runs,

            # --- what happened afterwards (v2.37). Read these against
            # `outcomes_recorded` / `confirmed_defects` for the same reason
            # provenance is read against `provenance_runs`: nobody has to record
            # an outcome, so a group with none reports four honest zeros.
            #
            # PER DEFECT — every other count in this row is per observation. A
            # defect this member raised in three rounds is three `confirmed` and
            # one outcome.
            "outcome": outcome_counts,
            # The subset a human signed off. #77's rule is that an agent must not
            # mark its own findings refuted unattended, and this API cannot tell a
            # fixer from a reviewer — so the split is published rather than the
            # guard pretended. A reader who wants only human-confirmed refutations
            # has them here; one who takes the raw number knows what it includes.
            "outcome_attested": outcome_attested,
            "outcomes_recorded": outcomes_recorded,
            # The population `precision_after` is over — fixed + refuted, never
            # the whole of `outcomes_recorded`. A ratio and the marker that
            # qualifies it have to be computed over one population; they were not.
            "outcomes_scored": scored,
            # The denominator `outcomes_recorded` is out of: distinct confirmed
            # defects this member raised in the window. Named because it is NOT
            # `confirmed` two lines up — that one counts observations.
            "confirmed_defects": confirmed_defects,
            # The number this release exists to publish: how often a confident,
            # judge-confirmed finding survived contact with the code. Sits beside
            # `precision`, never replacing it — the gap between the two is the
            # measurement.
            "precision_after": (round(outcome_counts["fixed"] / scored, 3)
                                if scored else None),
        })
    by_model.sort(key=lambda m: (-m["confirmed"], m["reviewer"]))

    agent_rows = (
        await session.execute(
            select(
                ReviewRun.author,
                func.count(ReviewRun.id),
                func.count(func.distinct(func.concat(ReviewRun.repo, "#", ReviewRun.pr))),
                func.sum(ReviewRun.n_confirmed),
                func.sum(ReviewRun.n_dismissed),
                func.max(ReviewRun.ts),
            )
            .where(*filters)
            .group_by(ReviewRun.author)
        )
    ).all()
    by_agent = [
        {
            "author": author_id,
            "runs": runs,
            "prs": prs,
            "confirmed": int(confirmed or 0),
            "dismissed": int(dismissed or 0),
            "last_run": last.isoformat(),
        }
        for author_id, runs, prs, confirmed, dismissed, last in agent_rows
    ]
    by_agent.sort(key=lambda a: -a["runs"])

    # The same axis at the window's grain rather than the reviewer's: how much of
    # what this loop found did it inflict on itself? That is the number an
    # operator reads at the cap, and it is the one `by_model` cannot give — a sum
    # across members double-counts every finding two seats agreed on.
    #
    # Confirmed only, the population every other quality figure here is over.
    prov_rows = (
        await session.execute(
            select(ReviewFinding.provenance, func.count(ReviewFinding.id))
            .join(ReviewRun, ReviewRun.id == ReviewFinding.run_id)
            .where(*filters, ReviewFinding.verdict == "confirmed")
            .group_by(ReviewFinding.provenance)
        )
    ).all()
    # Zeroed over the whole vocabulary first, so a bucket that happens to be empty
    # in this window renders as 0 rather than vanishing from the object and
    # leaving a client to guess whether it was zero or unsupported.
    by_provenance = dict.fromkeys(PROVENANCE, 0)
    # NULL findings, under a name that cannot be mistaken for the `unknown`
    # bucket. `unknown` was ASKED and could not be placed; this is every finding
    # the question never reached — a round 1, a run outside a cycle, a repeat of
    # something an earlier round raised, and every finding recorded before v2.26.
    # Reported rather than omitted so the four buckets are never read as the whole
    # window: they are usually a small part of it.
    by_provenance["not_attributed"] = 0
    for bucket, n in prov_rows:
        # A value outside the vocabulary can only come from a writer that is not
        # this API — ingest normalises to a known bucket or to NULL — so it is
        # surfaced verbatim rather than folded into a bucket it is not.
        key = "not_attributed" if bucket is None else str(bucket)
        by_provenance[key] = by_provenance.get(key, 0) + int(n or 0)

    # The same after-the-fact split at the window's grain: how much of what this
    # loop confirmed turned out to be real. Counted per DEFECT, once, rather than
    # once per member that raised it — a sum across `by_model` double-counts every
    # finding two seats agreed on, exactly as it does for provenance.
    outcome_window = (
        await session.execute(
            select(
                ReviewFindingOutcome.outcome,
                func.count(func.distinct(defect)),
                func.count(func.distinct(defect))
                    .filter(ReviewFindingOutcome.attested_by.isnot(None)),
            )
            .select_from(ReviewFinding)
            .join(ReviewRun, ReviewRun.id == ReviewFinding.run_id)
            .outerjoin(ReviewFindingOutcome, outcome_join)
            .where(*filters, ReviewFinding.verdict == "confirmed")
            .group_by(ReviewFindingOutcome.outcome)
        )
    ).all()
    by_outcome = dict.fromkeys(OUTCOMES, 0)
    by_outcome_attested = dict.fromkeys(OUTCOMES, 0)
    # Confirmed defects nobody has ruled on yet. Under its own name, and reported
    # rather than omitted, because the four buckets are a small part of the window
    # until the fix passes start recording — and a page that showed only them
    # would present today's handful as the whole picture.
    by_outcome["not_recorded"] = 0
    for bucket, n, attested in outcome_window:
        key = "not_recorded" if bucket is None else str(bucket)
        by_outcome[key] = by_outcome.get(key, 0) + int(n or 0)
        if bucket is not None:
            by_outcome_attested[key] = by_outcome_attested.get(key, 0) + int(attested or 0)

    runs_total, prs_total, repos_total, first_ts, last_ts = totals
    return {
        "window": {
            "since": cutoff.isoformat() if cutoff else None,
            "judged_only": judged_only,
            "repo": repo,
            "author": author,
            "first_run": first_ts.isoformat() if first_ts else None,
            "last_run": last_ts.isoformat() if last_ts else None,
        },
        "runs": runs_total,
        "prs": prs_total,
        "repos": repos_total,
        "by_model": by_model,
        "by_agent": by_agent,
        # Confirmed findings in this window by what caused them: `introduced` by
        # the previous fix pass, `missed` by the previous round, `missed-unread`
        # in a file that round was truncated out of, `unknown` where the fix range
        # could not be read — and `not_attributed` for every finding the question
        # never reached. Read `introduced` as a FLOOR: it needs exact membership
        # in the fix's added lines, so a defect introduced by a deletion and an
        # ordinary reviewer line-drift both land in `missed`.
        #
        # Per OBSERVATION, not per defect: a finding raised again in a later round
        # of the same cycle is another row, carrying NULL provenance, so repeats
        # land in `not_attributed`. See the docstring.
        "by_provenance": by_provenance,
        # What became of this window's confirmed findings once somebody acted on
        # them, per DEFECT — `fixed` and `refuted` are the judgement about whether
        # the finding was right, `deferred` and `superseded` are decisions about
        # what to do next, and `not_recorded` is every confirmed defect nobody has
        # ruled on. The gap between `fixed / (fixed + refuted)` here and the
        # `precision` figures above is what a reviewer's confidence is actually
        # worth.
        "by_outcome": by_outcome,
        # The subset a human signed off, so an unattended agent's refutations
        # cannot be read as adjudicated without the reader choosing to.
        "by_outcome_attested": by_outcome_attested,
    }


@router.get("/review/findings")
async def pr_finding_history(
    _reader: str = Depends(reader),
    repo: str = Query(..., min_length=1, description="github nameWithOwner"),
    pr: int = Query(..., ge=1),
    limit: int = Query(50, ge=1, le=200, description="trace this many of the PR's runs"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """One PR's findings as chains of observations — did the fix land?

    Observations are never collapsed: run 3 and run 7 seeing the same defect are
    two rows joined by ``key``, which is what makes "was this actually fixed?"
    and "how many rounds did this PR take?" answerable at all. Collapsing them
    into one current-state row would erase precisely that.

    ``status`` is what the record supports, not a claim about the code:

    * ``dismissed`` — the judge ruled against it every time it was raised.
    * ``gone`` — raised in an earlier run of this PR and not in the latest one.
      Usually the fix landed; it can also mean the reviewer that raised it did
      not run again, which the observation list shows.
    * ``open`` — still raised in the most recent run.

    ``outcome`` is the other thing entirely (v2.37): what somebody found out by
    acting on the finding — ``fixed`` / ``refuted`` / ``deferred`` /
    ``superseded`` — recorded by the fixer or a human through
    ``POST /review/outcomes``. It sits beside ``status`` and is never folded into
    it, because they answer different questions and are allowed to disagree: a
    chain reading ``gone`` (the reviewer that raised it did not run again) with an
    outcome of ``refuted`` is the exact case #77 was filed for. Null means nobody
    has said, which is neither.

    Scoped to one PR because ``key`` identifies a defect within a PR: the same
    "unused import" in two repos is not one chain.

    Each run also carries ``rereview_flagged``/``rereview_hit``, and the same pair
    per member in ``rereview_by_reviewer``. A hit says the round that followed *in
    the same cycle* (the next run of the same ``cycle`` whose round is this one's
    plus 1) raised a confirmed finding in a file this round flagged for re-reading.
    That is a file-grain coincidence, not a causal claim: an unrelated defect in a
    large flagged file counts, a regression that lands in another file does not,
    and it is worth reading over many rounds rather than as a verdict on one.

    Both halves are counted over **confirmed** findings only, and that is what
    makes the number mean anything: a declaration attached to a finding the judge
    dismissed is not a prediction worth scoring, and a finding nobody adjudicated
    is not the flagged fix being borne out. A flag on a finding that was never
    confirmed is therefore not scored at all rather than scored as wrong.

    Per-member attribution comes from ``review_finding_reports.needs_rereview`` —
    the declaration on the row of the member that made it. A caller that sends no
    ``reported_by`` has no such row, so its flags count in the run's total and in
    nobody's per-member entry; ``panel.py`` sends them, the merge having moved into
    its judge.

    ``stopped`` is the last round's own boolean — whether the cycle ended there —
    with ``stop_reason`` for the words and ``stop_veto`` for the reasons a stop was
    not convergence. It is not the reason string: that states a reason to go again
    just as often, so naming the ending after it called a running cycle finished.

    Those four describe **one** cycle, so this endpoint reports them only when
    every traced run belongs to the same one, and nulls all four otherwise (#44).
    ``cycles`` says how many groups the window held — and it is a BUCKET count,
    not a census of loops. Every run carrying no cycle at all (a standalone
    ``/panel`` read, or anything recorded before the cycle column existed) is one
    bucket together, because a run outside any cycle never ended the cycle running
    around it. So one real cycle plus one cycle-less run is ``cycles: 2`` with a
    null summary.

    That is the ordinary case rather than a rare one, and this docstring will not
    pretend otherwise: it is not only "two agents looped this PR". A cycle-less run
    landing BESIDE a real cycle's rounds — one standalone ``/panel``, or one round
    predating the cycle column sharing a window with a modern one — is enough, and
    the summary stays null for as long as that run is in the window. What it takes
    is the MIXTURE, not the cycle-less run on its own: a window that is entirely
    one cycle summarises, and so does one that is entirely cycle-less (which is the
    whole pre-cycle archive, and why that archive still reads as it always did).

    The four are therefore three-state, and must be read with ``is None`` rather
    than for truthiness: ``stopped: null`` is "no attributable cycle said", which
    is a different answer from ``false`` ("a round ran and said go again") — read
    for truthiness it calls a finished cycle unfinished. Same for ``stop_veto``,
    where ``[]`` is "the stopping rule ran and vetoed nothing" and null is "nobody
    attributable said" — the distinction ``GET /review/{id}`` already draws.
    ``cycles`` is the single field that answers "can I trust the summary?": one
    means yes.

    Narrowing ``limit`` can bring a summary back, but it is a trade, not a clean
    escape hatch, and both halves belong here. ``limit`` trims from the OLD end
    only, so the sole summary it can recover is the NEWEST bucket's — no value of
    ``limit`` reaches an older cycle's ending. And ``limit`` is the same window
    that decides ``first_run``, the ``gone`` status and new-vs-old detection, so
    narrowing it to recover a summary degrades the finding history in the same
    response and sets ``truncated``. The per-run rows in ``runs[]`` carry each
    round's own ``stopped``/``stop_reason``/``stop_confident``/``stop_veto``
    unaltered at any window size, and reading those is usually the better answer.

    The alternative — which this endpoint used to do — is to report ``runs[-1]``
    regardless and let the newest loop decide how an older one reads; the
    per-finding join beside it has refused that inference since cycles became a
    stored fact, and a summary that contradicts the rows underneath it is worse
    than an absent one.
    """
    # One over the window, so "there is older history" is a fact rather than the
    # guess "we returned exactly as many as we asked for".
    # Runs plus their unread-path counts, the count in SQL against a deferred
    # column — same reason as `list_reviews`: this endpoint traces up to `limit`
    # runs, and fetching every path of each to call `len()` on it is the transfer
    # the count exists to avoid.
    fetched_rows = list(
        (await session.execute(
            select(ReviewRun, _UNREAD_COUNT)
            .where(ReviewRun.repo == repo, ReviewRun.pr == pr)
            .order_by(ReviewRun.ts.desc(), ReviewRun.id.desc())
            .limit(limit + 1)
        )).all()
    )
    fetched = [r for r, _ in fetched_rows]
    unread_counts = {r.id: n for r, n in fetched_rows}
    if not fetched:
        # All four null, `stop_veto` included. An unreviewed PR is the clearest
        # case of "the stopping rule never ran", and [] is reserved for "it ran and
        # vetoed nothing" — the three-state contract the rest of this handler
        # keeps, which this branch used to be the one exception to. `cycles: 0`:
        # no runs, so no buckets.
        return {"repo": repo, "pr": pr, "rounds": 0, "cycles": 0, "stopped": None,
                "stop_reason": None, "stop_confident": None, "stop_veto": None,
                "truncated": False, "runs": [], "findings": []}

    truncated = len(fetched) > limit
    runs = list(reversed(fetched[:limit]))  # chronological: a chain reads left to right
    order = {r.id: i for i, r in enumerate(runs)}
    ts_by_run = {r.id: r.ts for r in runs}
    latest_id = runs[-1].id

    findings = list(
        (await session.scalars(
            select(ReviewFinding)
            .where(ReviewFinding.run_id.in_(list(order)))
            .order_by(ReviewFinding.id)
        )).all()
    )
    reports = await _reports_by_finding(session, [f.id for f in findings])
    outcomes = await _outcomes_for(session, repo, pr,
                                   sorted({f.finding_key for f in findings}))

    chains: dict[str, list[ReviewFinding]] = {}
    for f in sorted(findings, key=lambda f: order[f.run_id]):
        chains.setdefault(f.finding_key, []).append(f)

    # Was each round's re-review declaration any good? A reviewer that says "the
    # fix for this needs re-reading" is making a checkable claim, and this is the
    # check: the round that followed either did raise something new in that file
    # or it did not. Derived from the record rather than asked for — the declarer
    # cannot mark its own homework.
    #
    # What it establishes is FILE-GRAIN and no more: something the following round
    # raised in a file this round flagged. It does not establish that the later
    # finding was caused by the earlier fix — an unrelated defect in a big flagged
    # file scores as a hit, and a regression that surfaces in another file scores
    # as a miss. It is a signal over many rounds, not a verdict on one.
    #
    # "New" is the finding's own `new_this_round` wherever the panel said — it was
    # computed against the real baseline, which is the whole cycle rather than this
    # window. Only where it is null does this fall back to first appearance INSIDE
    # the window, and that fallback is what made a long-standing finding read as
    # fresh whenever the round that first raised it fell outside `limit`, falsely
    # vindicating the re-review flag that pointed at its file.
    # ONE population on both sides of the measurement: a finding the judge
    # confirmed. The two halves used to disagree twice over. The flagged side
    # counted every verdict, so a declaration attached to a finding the judge
    # DISMISSED inflated `rereview_flagged` and put its file where it could only
    # register as a miss — scoring a reviewer as having predicted wrongly when it
    # had made no scorable prediction. And the new side admitted `unjudged`, so a
    # round whose judge crashed vindicated every flag pointing at those files on
    # the strength of findings nobody ruled on. Sonar's hard-gate issues are out
    # for the same reason — nobody adjudicated them either.
    #
    # A flagged finding that was never confirmed is therefore not counted at all,
    # which is the honest answer: no claim was scorable, so none was scored.
    first_seen = {key: order[obs[0].run_id] for key, obs in chains.items()}
    flagged_files: dict[int, set[str]] = {}
    fresh_files: dict[int, set[str]] = {}
    flagged_counts: dict[int, int] = {}
    # ...and the same thing again per member that made the declaration. The
    # declaration rides on the reporter's own row (`review_finding_reports`), so
    # who said it survives to here — which is what "honesty per reviewer" needs
    # and a run-level boolean cannot give. Only a caller that sent `reported_by`
    # has that grain: a coarser payload's flags count in the run total and in no
    # member's row, since nothing on the record says which member made them.
    by_member_files: dict[int, dict[str, set[str]]] = {}
    by_member_counts: dict[int, dict[str, int]] = {}
    for f in findings:
        i = order[f.run_id]
        scorable = f.verdict == "confirmed"
        if f.needs_rereview and scorable:
            flagged_counts[i] = flagged_counts.get(i, 0) + 1
            if f.file:
                flagged_files.setdefault(i, set()).add(f.file)
            for r in reports.get(f.id, []):
                if not r.needs_rereview:
                    continue
                seen = by_member_counts.setdefault(i, {})
                seen[r.reviewer] = seen.get(r.reviewer, 0) + 1
                if f.file:
                    by_member_files.setdefault(i, {}).setdefault(
                        r.reviewer, set()).add(f.file)
        fresh = f.new_this_round if f.new_this_round is not None else (
            first_seen[f.finding_key] == i)
        if f.file and fresh and scorable:
            fresh_files.setdefault(i, set()).add(f.file)

    def followed_by(i: int) -> int | None:
        """The run that re-reviewed run ``i``'s fix, if the record says there was
        one: the next round of the SAME cycle.

        Cycle identity is required, not preferred. The old fallback took the
        adjacent run whenever the cycle was null and its round was this one's plus
        1, and that guess is wrong exactly when it matters — A-r1, B-r2 recorded
        by two agents looping one PR credits B's findings as the answer to A's
        declaration. This number is published as an honesty measure, so an unknown
        attribution yields ``rereview_hit: null`` rather than a guess. Nothing real
        is lost: the flag column arrived in the same release as the cycle id, so
        every run that can carry a declaration also carries a cycle.
        """
        this = runs[i]
        if not this.cycle:
            return None
        want = (this.round or 1) + 1
        return next((j for j in range(i + 1, len(runs))
                     if runs[j].cycle == this.cycle and runs[j].round == want), None)

    def rereview(i: int) -> dict:
        """Run ``i``'s declarations and how the round that followed bore them out —
        for the run as a whole, and for each member that made one."""
        j = followed_by(i)
        answered = fresh_files.get(j, set()) if j is not None else set()

        def hit(flagged: set[str]) -> bool | None:
            # None = nobody looked (no following round) or nothing was claimed,
            # which is a different answer from "nothing was there".
            if j is None or not flagged:
                return None
            return any(_same_file(a, b) for a in flagged for b in answered)

        return {
            "rereview_flagged": flagged_counts.get(i, 0),
            "rereview_hit": hit(flagged_files.get(i, set())),
            "rereview_by_reviewer": {
                name: {"flagged": n, "hit": hit(by_member_files.get(i, {}).get(name, set()))}
                for name, n in sorted(by_member_counts.get(i, {}).items())
            },
        }

    out = []
    for key, obs in chains.items():
        last = obs[-1]
        reviewers: list[str] = []
        related: list[str] = []
        for f in obs:
            reviewers += [r for r in (f.reviewers or []) if r not in reviewers]
            related += [r for r in (f.related or []) if r not in related]
        verdicts = {f.verdict for f in obs}
        out.append({
            "key": key,
            # The latest observation's words: the newest statement of the defect
            # is the one worth showing, and its line has survived any fix above it.
            "file": last.file,
            "line": last.line,
            "title": last.title,
            "severity": last.severity,
            "status": ("dismissed" if verdicts == {"dismissed"}
                       else "open" if last.run_id == latest_id else "gone"),
            # What somebody found out by ACTING on it — beside `status`, never
            # folded into it. `status` is what the record of the reviews supports
            # ("raised earlier, not raised in the latest run"); this is a claim
            # about the code, made by whoever did the work, and the two are
            # allowed to disagree loudly: a chain that reads `gone` because the
            # reviewer never ran again, with an outcome of `refuted`, is exactly
            # the case this feature exists to make visible. Null = nobody has said.
            "outcome": (_outcome_view(outcomes[key]) if key in outcomes else None),
            "runs_seen": len(obs),
            "first_run": obs[0].run_id,
            "last_run": last.run_id,
            "reviewers": reviewers,
            "related": related,
            "needs_rereview": any(f.needs_rereview for f in obs),
            "observations": [
                {
                    "run_id": f.run_id,
                    "ts": ts_by_run[f.run_id].isoformat(),
                    **_finding_view(f, reports.get(f.id, [])),
                }
                for f in obs
            ],
        })
    out.sort(key=lambda c: (c["severity"] or "P9", order[c["first_run"]], c["key"]))

    # Whether this PR's stop state is one thing to report. Runs bucket by cycle id,
    # and a null cycle is its own identity here rather than a wildcard: a run
    # outside any cycle — a review-only `/panel`, or anything recorded before
    # cycles were stored — cannot be said to have ended a cycle it was never part
    # of, so a window mixing one with a real cycle is exactly as unattributable as
    # one holding two. A window that is ALL nulls is one bucket and still
    # summarises, which keeps pre-cycle history reading as it always did.
    #
    # `summarisable`, not `one_cycle`: a window of only cycle-less runs passes this
    # and holds ZERO cycles, so the name would assert the opposite of the case it
    # was written for. What is being asked is whether there is one bucket to
    # attribute a summary to.
    #
    # The reach of this is wide and the docstring says so plainly: one standalone
    # `/panel` read is enough, so any PR ever read outside a loop reads as
    # unattributable at the default `limit` until that run leaves the window. The
    # obvious narrower rule — summarise when the newest cycle forms a contiguous
    # tail of the window — is not an improvement, it is the bug: A-r1 followed by
    # B-r1 has B as a contiguous tail, and reporting B's confident stop as this
    # PR's ending is exactly what #44 was filed about.
    buckets = {r.cycle for r in runs}
    summarisable = len(buckets) == 1
    last = runs[-1]

    return {
        "repo": repo,
        "pr": pr,
        "rounds": len(runs),
        # How many groups the traced runs fall into, which is what makes the four
        # fields below readable: one, and only one, summarises.
        #
        # It counts BUCKETS, not loops, and the difference is not pedantry. Every
        # run carrying no cycle is one bucket together, so one real cycle plus a
        # standalone `/panel` read reports 2 while only one loop ever ran here.
        # The page therefore says "separate groups of runs" rather than claiming a
        # cycle count, and nothing should read this number as "N agents looped this
        # PR". What it does say exactly — and all a caller needs — is whether the
        # window holds one attributable thing (1) or not (anything else).
        "cycles": len(buckets),
        # What ended the cycle, from the last round that ran — and whether that
        # was convergence or merely a stop. A PR whose panel gave up at the round
        # cap, or stopped while a reviewer was reading half the diff, must not
        # read like one that was reviewed until there was nothing left.
        #
        # `stopped` is the panel's own boolean, spelled the same way it is on
        # ``GET /review/{id}``. It used to be the reason STRING, which reads as a
        # reason to go again just as often ("N finding(s) no earlier round
        # raised") — so a cycle that explicitly must continue was labelled
        # finished by the field that names the ending.
        #
        # NULL when the window holds more than one bucket (#44). These four came
        # from `runs[-1]` whatever cycle it belonged to, so cycle B's last round
        # decided how cycle A read — complete, unfinished, or unconfident — in the
        # same response whose per-finding join refuses that exact inference:
        # `followed_by` requires matching cycle ids rather than guessing from
        # adjacency. A summary is a claim about one loop, and with two buckets in
        # the window there is no one loop to claim it about. `cycles` above says
        # so, which is the honest answer.
        #
        # Who a nullable bool/str breaks, audited rather than asserted — and the
        # audit covers all four, not just the list one, because `stopped` and
        # `stop_confident` are the fields most likely to be read for truthiness,
        # where null silently reads as False: "the cycle is still going" and "the
        # stop was not earned", both wrong.
        #
        # `app/static/reviews.html` is the ONLY consumer of this endpoint in the
        # repo — grep for `/review/findings` over *.py, *.html, *.js and harness/
        # — and it now tests `cycles` before anything else, so it never reaches a
        # truthiness test on a null. `harness/loops/preland.py` is NOT a consumer
        # of this response despite reading identically-named keys null-safely: its
        # `_judge_round` rules on per-round rows from `GET /reviews`, and those
        # four stay per-run facts that this change does not touch (the same is
        # true of the `runs[]` rows below). So the exposure is future callers, and
        # the docstring states the three-state contract for them.
        "stopped": last.stopped if summarisable else None,
        "stop_reason": last.stop_reason if summarisable else None,
        "stop_confident": last.stop_confident if summarisable else None,
        # WHY the stop was unearned, in the panel's words. "not convergence" with
        # no reasons attached is the question this feature exists to answer left
        # unanswered.
        #
        # NULL rather than [] when the buckets are mixed, the same distinction
        # `GET /review/{id}` already draws: [] is "the stopping rule ran and
        # vetoed nothing", null is "nobody attributable said". The zero-runs branch
        # at the top of this handler returns null for the same reason.
        #
        # The one consumer guards every read of it — `(h.stop_veto || []).length`
        # in reviews.html — and that is now PINNED rather than left as prose:
        # `test_the_page_never_reads_the_summary_stop_veto_unguarded` counts the
        # mentions in the file that ships, so a future `for (const v of
        # h.stop_veto)` fails a test instead of throwing in a browser.
        #
        # `last.stop_veto` RAW, never `or []`. The attributable case is exactly
        # where the three-state contract has to hold: a stored NULL on the one run
        # this summary speaks for means "that round recorded no veto answer", and
        # coercing it to [] reports the opposite — "the stopping rule ran and
        # vetoed nothing" — about the run whose evidence the whole summary rests
        # on. `GET /review/{id}` returns it raw for this reason; so does this.
        "stop_veto": last.stop_veto if summarisable else None,
        # More runs exist than the window traced, so `first_run` and a `gone`
        # status describe the window, not the PR's whole history.
        "truncated": truncated,
        "runs": [
            # `head_sha` rides along because this endpoint is where a defect is
            # traced to the fix that caused it, and a round is only replayable
            # against the repo if something says which commit it read.
            #
            # `provenance_counts` for the same reason one field over: this is
            # where the per-finding buckets are READ, and a reader interpreting
            # them without the round's own tally beside them had to issue a
            # request per run to get it. It is at most four integers.
            #
            # The unread PATHS are not here — up to `limit` runs of them would be
            # the file dump `_run_view` keeps out of `GET /reviews`. The count says
            # whether a `missed-unread` bucket had coverage behind it, and
            # `GET /review/{id}` has the list.
            {"id": r.id, "ts": r.ts.isoformat(), "author": r.author, "judged": r.judged,
             "head_sha": r.head_sha,
             # The base end beside it, for the same reason `head_sha` is here: a
             # round is only replayable, and its empty To-fix list only still
             # true, relative to a base — and this endpoint is where a finding is
             # traced to the fix that caused it. Two strings per run.
             "merge_base": r.merge_base,
             "base_sha": r.base_sha,
             "provenance_counts": r.provenance_counts,
             "unread_files_count": unread_counts[r.id],
             "confirmed": r.n_confirmed, "dismissed": r.n_dismissed,
             "unjudged": r.n_unjudged, "sonar": r.n_sonar,
             "round": r.round, "cycle": r.cycle, "new_findings": r.new_findings,
             "stopped": r.stopped, "stop_reason": r.stop_reason,
             # RAW, for the same reason as the summary above and with one more on
             # top: the docstring, the README and the CHANGELOG all promise these
             # four ride here UNALTERED at any window size, and point callers at
             # them as the better answer precisely because the summary can be
             # unattributable. An `or []` here made that promise false for a run
             # with no recorded veto, and made the same run read differently
             # through this endpoint than through `GET /review/{id}`.
             "stop_confident": r.stop_confident, "stop_veto": r.stop_veto,
             # Findings this round declared worth re-reading, and whether the
             # round that followed found anything where it pointed — file-grain,
             # over confirmed findings only. None = no round followed it in this
             # cycle, which is a different answer from "nothing there".
             # `rereview_by_reviewer` is the same question per member that made
             # the declaration, which is what makes the measure per-reviewer.
             **rereview(i)}
            for i, r in enumerate(runs)
        ],
        "findings": out,
    }


@router.get("/review/{run_id}")
async def get_review(
    run_id: int,
    _reader: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """One run in full — scorecards plus every finding and its verdict."""
    # `undefer`, explicitly: this is the ONE endpoint that publishes the unread
    # paths, and the column is deferred so the list views never pay for them.
    # Async SQLAlchemy cannot lazy-load, so without this the attribute access
    # below raises `MissingGreenlet` rather than quietly issuing a second query —
    # which is the failure mode worth having, since it cannot go unnoticed.
    run = await session.scalar(
        select(ReviewRun).where(ReviewRun.id == run_id)
        .options(undefer(ReviewRun.unread_files))
    )
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no review run {run_id}")
    cards = list(
        (await session.scalars(
            select(ReviewReviewer)
            .where(ReviewReviewer.run_id == run_id)
            .order_by(ReviewReviewer.name)
        )).all()
    )
    findings = list(
        (await session.scalars(
            select(ReviewFinding)
            .where(ReviewFinding.run_id == run_id)
            .order_by(ReviewFinding.severity, ReviewFinding.id)
        )).all()
    )
    reports = await _reports_by_finding(session, [f.id for f in findings])
    # What became of each defect this run raised. Keyed by (repo, pr, key) rather
    # than by run: the outcome outlives the round, and this run is one of the
    # rounds that saw it.
    outcomes = await _outcomes_for(session, run.repo, run.pr,
                                   [f.finding_key for f in findings])
    files = list(
        (await session.scalars(
            select(ReviewRunFile)
            .where(ReviewRunFile.run_id == run_id)
            .order_by(ReviewRunFile.path)
        )).all()
    )
    return {
        # The count is `len()` here and not `_UNREAD_COUNT`: this is the one
        # caller that has already paid to load the list, so counting it in SQL
        # would be a second trip for a number already in hand.
        **_run_view(run, None if run.unread_files is None else len(run.unread_files)),
        # The paths themselves, here and not in the run LIST — exactly where
        # `changed_files` lives and for the same reason: one run's worth of paths
        # is a fair payload, a page of them is a file dump. `_run_view` carries
        # `unread_files_count` so a list reader still knows a list exists and can
        # still tell "measured nothing" (0) from "never measured" (null).
        #
        # Unmasked: NULL means the panel never said and [] means it said nothing
        # was cut, and folding one into the other on read would hand every
        # consumer the collapse the storage side is built to prevent.
        "unread_files": run.unread_files,
        # #113: what this round ASKED for, kept apart from the per-seat answer in
        # `reviewers[].code_blind`. A round with the setting on and every seat
        # blind is a configuration doing nothing, and only the difference shows it.
        "code_access": run.code_access,
        # Unmasked for the reason `unread_files` above is: [] means a tree was
        # built and carried no instruction files, NULL means no tree was built at
        # all, and folding them together loses which PRs tried to instruct their
        # own reviewer.
        "convention_files_removed": run.convention_files_removed,
        # Read `changed_files_total` against `len(changed_files)` before building
        # anything on this list: they are allowed to disagree, and when they do
        # the list is a PREFIX of what the PR touches.
        "changed_files": [
            {"path": f.path, "additions": f.additions, "deletions": f.deletions}
            for f in files
        ],
        "reviewers": [_card_view(c, run) for c in cards],
        "findings": [_finding_view(f, reports.get(f.id, []), outcomes.get(f.finding_key))
                     for f in findings],
    }
