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
PR's paths, each with its own additions/deletions) and ``changed_files_total``,
GitHub's own count, kept separate so a list truncated by GitHub's 3,000-file cap
is detectable rather than reading as complete. ``GET /review/collisions`` is what
that buys: the other PRs whose most recent run touched any of the same files, and
which files those are. It is deliberately only the OVERLAP, not an ordering —
ranking PRs by it is #80's job and needs a policy this endpoint should not
presume. Every pre-v2.23 run has no file list at all, which is not the same fact
as a PR that changed no files, and the endpoint says which runs it could not
speak for rather than silently reading them as disjoint.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from sqlalchemy import func, select
from sqlalchemy import or_ as sa_or
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import identify, reader
from app.db import get_session
from app.identity import agent_row, compose, machine_of
from app.models.review import (
    ReviewFinding,
    ReviewFindingReport,
    ReviewReviewer,
    ReviewRun,
    ReviewRunFile,
)

router = APIRouter(tags=["review"])


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


class ChangedFileIn(BaseModel):
    """One path the PR touched, with that path's own share of ``changed_lines``.

    A bare string is accepted too, and is the shape a hand-rolled caller reaches
    for first: ``["a.py", "b.py"]`` records the paths with null churn rather than
    422-ing the whole payload away. Recording is best-effort here as everywhere
    else in this module — losing the findings over the shape of a file list would
    be the wrong trade by a wide margin.
    """

    model_config = ConfigDict(populate_by_name=True)

    path: str = Field(min_length=1)
    additions: int | None = Field(default=None, ge=0)
    deletions: int | None = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _bare_path(cls, v: object) -> object:
        return {"path": v} if isinstance(v, str) else v


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
    changed_lines: int | None = None
    #: The PR's touched paths — the PR's, never the round's. Under #41 a later
    #: round reviews only the increment; narrowing this to it would report two
    #: PRs as no longer colliding because one stopped re-reading a file it still
    #: changes. Absent for every pre-v2.23 run, which is "no list", not "no files".
    changed_files: list[ChangedFileIn] = Field(default_factory=list)
    #: GitHub's own count of the PR's changed files. NOT derived from
    #: ``len(changed_files)``: `gh` pages that list and GitHub caps it at 3,000,
    #: so the two disagreeing is the only signal a collision query under-reports.
    changed_files_total: int | None = Field(default=None, ge=0)
    diff_chars: int | None = None
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
            *(s.lower() for s in SEVERITIES))
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
        changed_lines=body.changed_lines,
        # Stored AS SENT rather than backfilled from len(changed_files). A caller
        # that sends the paths and not the count leaves this NULL, and NULL there
        # honestly means "nobody said how many there were" — filling it in from
        # the rows would manufacture agreement between the two numbers whose
        # DISAGREEMENT is the only evidence the list is short.
        changed_files_total=body.changed_files_total,
        diff_chars=body.diff_chars,
        diff_truncated=body.diff_truncated,
        judged=body.judged,
        judge_model=body.judge_model or None,
        judge_skip=body.judge_skip,
        coverage_note=body.coverage_note or None,
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

    # Deduped on the way in, keeping the first mention of each path. The table's
    # unique constraint would otherwise turn a sender that repeats a path into an
    # IntegrityError that costs the whole run its findings — and this module's
    # rule is that recording is best-effort. Order is the sender's, so a reader
    # of one run's files sees them as the panel listed them.
    seen: set[str] = set()
    for cf in body.changed_files:
        path = cf.path.strip()
        if not path or path in seen:
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
    return {"id": run.id, "recorded": True, "findings": len(findings), "accounts": accounts}


# ------------------------------------------------------------------ read paths

def _run_view(r: ReviewRun) -> dict:
    return {
        "id": r.id,
        "ts": r.ts.isoformat(),
        "author": r.author,
        "session": r.session,
        "repo": r.repo,
        "pr": r.pr,
        "pr_title": r.pr_title,
        "base": r.base_branch,
        "changed_lines": r.changed_lines,
        # The count only — the paths themselves are per-run children and would
        # turn every page of `GET /reviews` into a file dump. `GET /review/{id}`
        # carries the list; this is what a run LIST needs to know a list exists.
        "changed_files_total": r.changed_files_total,
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


def _card_view(c: ReviewReviewer) -> dict:
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
    }


def _report_view(r: ReviewFindingReport) -> dict:
    return {
        "reviewer": r.reviewer,
        "severity": r.severity,
        "line": r.line,
        "account": r.account,
        "needs_rereview": r.needs_rereview,
    }


def _finding_view(f: ReviewFinding, reports: list[ReviewFindingReport]) -> dict:
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
    stmt = select(ReviewRun)
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

    runs = list((await session.scalars(stmt)).all())
    if not runs:
        return []
    cards = list(
        (await session.scalars(
            select(ReviewReviewer).where(ReviewReviewer.run_id.in_([r.id for r in runs]))
        )).all()
    )
    by_run: dict[int, list[dict]] = {}
    for c in cards:
        by_run.setdefault(c.run_id, []).append(_card_view(c))
    return [{**_run_view(r), "reviewers": sorted(by_run.get(r.id, []), key=lambda c: c["name"])}
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
    """
    # One over the window, so "there is older history" is a fact rather than the
    # guess "we returned exactly as many as we asked for".
    fetched = list(
        (await session.scalars(
            select(ReviewRun)
            .where(ReviewRun.repo == repo, ReviewRun.pr == pr)
            .order_by(ReviewRun.ts.desc(), ReviewRun.id.desc())
            .limit(limit + 1)
        )).all()
    )
    if not fetched:
        return {"repo": repo, "pr": pr, "rounds": 0, "stopped": None,
                "stop_reason": None, "stop_confident": None, "stop_veto": [],
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

    return {
        "repo": repo,
        "pr": pr,
        "rounds": len(runs),
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
        "stopped": runs[-1].stopped,
        "stop_reason": runs[-1].stop_reason,
        "stop_confident": runs[-1].stop_confident,
        # WHY the stop was unearned, in the panel's words. "not convergence" with
        # no reasons attached is the question this feature exists to answer left
        # unanswered.
        "stop_veto": runs[-1].stop_veto or [],
        # More runs exist than the window traced, so `first_run` and a `gone`
        # status describe the window, not the PR's whole history.
        "truncated": truncated,
        "runs": [
            {"id": r.id, "ts": r.ts.isoformat(), "author": r.author, "judged": r.judged,
             "confirmed": r.n_confirmed, "dismissed": r.n_dismissed,
             "unjudged": r.n_unjudged, "sonar": r.n_sonar,
             "round": r.round, "cycle": r.cycle, "new_findings": r.new_findings,
             "stopped": r.stopped, "stop_reason": r.stop_reason,
             "stop_confident": r.stop_confident, "stop_veto": r.stop_veto or [],
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


@router.get("/review/collisions")
async def pr_collisions(
    _reader: str = Depends(reader),
    repo: str = Query(..., description="github nameWithOwner"),
    pr: int = Query(..., ge=1),
    since: str | None = Query(None, description="ISO timestamp"),
    days: int | None = Query(30, ge=1, le=3650,
                             description="how far back a rival PR's newest run may be"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Which other PRs of this repo touch the files this one does.

    The overlap, and only the overlap. Ordering PRs by it — landing the disjoint
    ones first — is #80's job and needs a policy about what a collision COSTS
    that this endpoint has no business presuming; what was missing was the datum,
    not the ranking.

    A PR is represented by its most recent run **that recorded a file list**, not
    simply its most recent run, and that run's ``id``/``ts`` come back with it so
    a caller can see how stale the answer is. A PR still open whose last panel was
    Tuesday collides on Tuesday's files; the board is not told about pushes.

    ``unknown`` is the half that matters more than it looks. Every run recorded
    before v2.23 has no file list, and so does any PR that has never been
    panelled — those PRs are not disjoint from this one, they are *unanswered*.
    Returning them silently absent would make an empty ``collides`` read as "safe
    to land", which is exactly the shortfall-as-clean-result failure this codebase
    keeps finding in itself.
    """
    # The subject's files: its newest run that has any. Falling back through
    # earlier runs is deliberate — a round that skipped on a title pattern still
    # records a list, but a pre-v2.23 round does not, and the PR's file set is
    # better known late than not at all.
    has_files = select(ReviewRunFile.run_id)
    subject_id = await session.scalar(
        select(func.max(ReviewRun.id)).where(
            ReviewRun.repo == repo, ReviewRun.pr == pr, ReviewRun.id.in_(has_files)
        )
    )
    if subject_id is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no run of {repo}#{pr} recorded a changed-file list — nothing to compare",
        )
    subject = await session.get(ReviewRun, subject_id)
    paths = list((await session.scalars(
        select(ReviewRunFile.path).where(ReviewRunFile.run_id == subject_id)
    )).all())

    cutoff = _since_clause(since, days)
    others = select(ReviewRun.pr).where(ReviewRun.repo == repo, ReviewRun.pr != pr)
    if cutoff is not None:
        others = others.where(ReviewRun.ts >= cutoff)

    # One row per rival PR: its newest file-bearing run. `pr` is grouped on, so
    # two runs of the same PR can never both answer for it.
    latest = (
        select(ReviewRun.pr.label("pr"), func.max(ReviewRun.id).label("run_id"))
        .where(ReviewRun.repo == repo, ReviewRun.pr != pr, ReviewRun.id.in_(has_files))
    )
    if cutoff is not None:
        latest = latest.where(ReviewRun.ts >= cutoff)
    latest = latest.group_by(ReviewRun.pr).subquery()

    shared = (
        await session.execute(
            select(latest.c.pr, latest.c.run_id, ReviewRun.ts, ReviewRun.pr_title,
                   ReviewRunFile.path)
            .join(ReviewRunFile, ReviewRunFile.run_id == latest.c.run_id)
            .join(ReviewRun, ReviewRun.id == latest.c.run_id)
            .where(ReviewRunFile.path.in_(paths))
            .order_by(latest.c.pr, ReviewRunFile.path)
        )
    ).all() if paths else []

    hits: dict[int, dict] = {}
    for other_pr, run_id, ts, title, path in shared:
        row = hits.setdefault(other_pr, {
            "pr": other_pr, "pr_title": title, "run_id": run_id,
            "ts": ts.isoformat(), "files": [],
        })
        row["files"].append(path)

    answered = set((await session.scalars(select(latest.c.pr))).all())
    unknown = sorted(set((await session.scalars(others.distinct())).all()) - answered)

    return {
        "repo": repo,
        "pr": pr,
        "run_id": subject_id,
        "ts": subject.ts.isoformat() if subject else None,
        "files": sorted(paths),
        # Read against len(files): GitHub caps a PR's file list at 3,000, and a
        # subject whose own list was truncated under-reports its own collisions.
        "changed_files_total": subject.changed_files_total if subject else None,
        # Most shared files first — a description of the overlap, not a
        # recommendation about it.
        "collides": sorted(hits.values(), key=lambda h: (-len(h["files"]), h["pr"])),
        #: PRs of this repo with a run in the window and NO recorded file list.
        #: Not disjoint — unanswered. Every pre-v2.23 run lands here.
        "unknown": unknown,
    }


@router.get("/review/{run_id}")
async def get_review(
    run_id: int,
    _reader: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """One run in full — scorecards plus every finding and its verdict."""
    run = await session.get(ReviewRun, run_id)
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
    files = list(
        (await session.scalars(
            select(ReviewRunFile)
            .where(ReviewRunFile.run_id == run_id)
            .order_by(ReviewRunFile.path)
        )).all()
    )
    return {
        **_run_view(run),
        # Read `changed_files_total` against `len(changed_files)` before building
        # anything on this list: they are allowed to disagree, and when they do
        # the list is a PREFIX of what the PR touches.
        "changed_files": [
            {"path": f.path, "additions": f.additions, "deletions": f.deletions}
            for f in files
        ],
        "reviewers": [_card_view(c) for c in cards],
        "findings": [_finding_view(f, reports.get(f.id, [])) for f in findings],
    }
