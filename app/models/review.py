from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.needs_human import NEEDS_HUMAN_CLASSES

#: #279's vocabulary, rendered for a CHECK. Built from the tuple rather than
#: spelled out, which is the whole point of that module having no imports: these
#: two constraints were written as literals and #578 found them still naming six
#: classes after the vocabulary had grown to seven — the exact drift
#: `app/models/blocker.py` avoids by composing its own list the same way.
_NH_CLASS_LIST = ", ".join(f"'{c}'" for c in NEEDS_HUMAN_CLASSES)


class ReviewRun(Base):
    """One reviewer-panel run over one PR.

    The panel (``~/.claude/loops/panel.py``) fans a PR diff out to several
    vendor CLIs, dedups what they report, and has a master judge rule each
    finding real or not. That produces exactly the comparison nobody was
    keeping: the same diff, reviewed by several models, with an adjudicated
    answer for who was right. Recording it turns "which model should review?"
    and "is the expensive tier worth it?" into queries instead of opinions.

    ``author`` is the board identity that ran the panel (``machine/instance``),
    so a run is attributable to the agent that ordered it, not just the machine.

    Not a board post: posts are an append-only wire read newest-last by agents,
    and aggregating months of them by model would mean unpicking JSON in the
    read path. This is the durable, queryable half; the board still gets a
    one-line note so peers see the review happened.
    """

    __tablename__ = "review_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    author: Mapped[str] = mapped_column(Text, nullable=False)
    session: Mapped[str | None] = mapped_column(Text)

    # What was reviewed.
    repo: Mapped[str] = mapped_column(Text, nullable=False)  # github "owner/name"
    pr: Mapped[int] = mapped_column(Integer, nullable=False)
    pr_title: Mapped[str | None] = mapped_column(Text)
    base_branch: Mapped[str | None] = mapped_column(Text)
    #: The COMMIT this round reviewed (v2.26). Nothing else on a run identifies
    #: one — ``base_branch`` holds a branch *name*, which moves — so without this
    #: a round can never be replayed against the repo after the fact, and the fix
    #: range between two rounds cannot be computed at all. #98 wants the base end
    #: of that same range; #80 wants this column to reason about what a merge
    #: actually moved. NULL for every run recorded before the board stored it.
    head_sha: Mapped[str | None] = mapped_column(Text)
    #: The PR's base commit (v2.29) — the merge base. ``gh pr diff`` is the
    #: three-dot diff, so a whole-PR round reads ``merge_base...head_sha`` and
    #: until now nothing named the left-hand side. It moves when the PR merges its
    #: base in or is rebased, which is the branch acting; base branch movement
    #: cannot touch it.
    #:
    #: **The PR's anchor, not necessarily the round's.** Under v2.28's increment
    #: scope the target is ``since_sha...head_sha`` and this is where the tier-2
    #: context is measured from instead, so read ``scope`` before treating it as
    #: the left-hand side of what a given round reviewed.
    merge_base: Mapped[str | None] = mapped_column(Text)
    #: The live tip of ``base_branch`` at review time (v2.29) — what the PR would
    #: be merged INTO, as opposed to what it was diffed from.
    #:
    #: **The two are separate columns because the obvious single field cannot do
    #: this job.** #98 proposed storing GitHub's ``baseRefOid`` and comparing it
    #: later against the PR's current ``baseRefOid``; that field is the merge
    #: base, and a merge base is a common ancestor, so commits landing on the base
    #: branch cannot move it. PR #87 held one value across ten commits of ``main``
    #: and ``git merge-base`` still agreed with it afterwards. A staleness check
    #: built on it can only ever answer "unmoved".
    #:
    #: So this is the end that moves, and the one a pre-land check (#96) compares
    #: against the base branch's tip at LAND time. NULL where the panel could not
    #: read it — it costs its own lookup, and the skip path deliberately does not
    #: pay for one — never zero, never the merge base standing in.
    base_sha: Mapped[str | None] = mapped_column(Text)
    #: What this round actually REVIEWED, and the commit it reviewed from (#647) —
    #: ``pr`` (the whole diff) or ``increment`` (the commits since
    #: :attr:`since_sha`, with the rest of the PR as context).
    #:
    #: Not inferable from :attr:`round`. The panel falls back to ``pr`` whenever the
    #: anchor is missing or the fetch failed, so "round 2" does not imply
    #: "increment" — and :attr:`diff_chars` is scope-dependent, which makes this the
    #: field that says whether two rounds' ``diff_chars`` are the same measurement
    #: or two different ones. A consumer that compares them without reading this
    #: first is comparing a whole PR against a commit range.
    #:
    #: Stored verbatim against no vocabulary: see ``reviews._word_or_none``. NULL is
    #: "the panel did not say", which is every run recorded before this column.
    #: ``since_sha`` is a commit id normalised by the same rule as
    #: :attr:`head_sha` and the two base ends, because under increment scope the
    #: round's target is ``since_sha...head_sha`` and a range with one end
    #: normalised and one raw compares badly.
    scope: Mapped[str | None] = mapped_column(Text)
    since_sha: Mapped[str | None] = mapped_column(Text)
    #: WHICH range the attribution behind :attr:`provenance_counts` read (#512,
    #: stored by #647): ``increment`` — the diff this round reviewed — ``compare``,
    #: the separate API fetch used under ``pr`` scope and wherever the increment
    #: fell back, or ``reconstructed``, #504's rebuild after a rewritten history.
    #:
    #: The three are not one measurement: the increment drops a base-branch merge's
    #: files and the compare range does not. So a consumer holding ``introduced``
    #: counts across a cycle's rounds without this is holding counts whose
    #: denominator changed underneath it — which is the trap #642's changelog
    #: names and the reason #637 cannot recalibrate a threshold without this column.
    #:
    #: NULL where the question does not arise (round 1 attributes nothing) and for
    #: every run recorded before the column. Stored verbatim, and deliberately not
    #: against a frozen set of the three: #512 published two and #504 added the
    #: third, so a set written on this board would have dropped ``reconstructed``
    #: on the release that introduced it.
    fix_range_source: Mapped[str | None] = mapped_column(Text)
    #: Paths NO reviewer that ran read in full — the round's own coverage hole,
    #: banked for the NEXT round's ``missed-unread`` bucket. A file only lands
    #: here if every seat was truncated out of it: one seat that read it means the
    #: ROUND saw it.
    #:
    #: NULL = the panel did not say (every pre-v2.26 run); [] = it said, and
    #: nothing was cut. The same distinction ``could_not_assess`` and ``stop_veto``
    #: are built on, and for the same reason — collapsing them reads a round
    #: nobody measured as a round that read everything.
    #:
    #: JSONB rather than a child table like :class:`ReviewRunFile`: that table
    #: exists to carry per-path churn and answer a by-path collision query, and
    #: this list has neither. It is read whole, per run, by whatever computes the
    #: next round's provenance.
    #:
    #: **Deferred**, and that is load-bearing rather than tidy. The list views
    #: publish only a count, and the first cut of that computed it in Python as
    #: ``len(r.unread_files)`` — which meant Postgres still shipped every path of
    #: every row to the app and only the JSON serialisation was saved. The read
    #: path's stated defence did not hold on the read path it was written for. The
    #: count is now ``jsonb_array_length`` in the query and this column is not
    #: fetched at all unless somebody asks for the paths.
    #:
    #: Consequence for callers: async SQLAlchemy cannot lazy-load, so reading
    #: ``run.unread_files`` off a run this session did not undefer raises
    #: ``MissingGreenlet`` rather than quietly issuing a second query.
    #: ``GET /review/{id}`` asks with ``undefer()``; nothing else should need to.
    unread_files: Mapped[list[Any] | None] = mapped_column(JSONB, deferred=True)
    #: Whether this round ASKED for its seats to read the code
    #: (``review_panel.reviewer_code_access``, #113). The per-seat answer is
    #: ``review_reviewers.code_blind``; this is the setting, and the two are
    #: different facts worth keeping apart — a round with the setting on and every
    #: seat blind is a configuration doing nothing, which is visible in the
    #: difference and invisible in either column alone. NULL = the panel didn't
    #: say, which is also every round before the setting existed.
    code_access: Mapped[bool | None] = mapped_column(Boolean)
    #: Vendor instruction files removed from the reviewers' checkout before any CLI
    #: started — ``CLAUDE.md``, ``AGENTS.md``, ``.claude/`` and the rest, at any
    #: depth. Stored because a PR that shipped one is worth being able to find
    #: later: it is the clearest signal available that a contribution tried to
    #: instruct the reviewer judging it, and a silent strip makes that PR
    #: indistinguishable from one that shipped nothing. ``[]`` = a tree was built
    #: and carried none; NULL = no tree was built (access off, or the fetch failed).
    convention_files_removed: Mapped[list[Any] | None] = mapped_column(JSONB)
    #: WHETHER THIS ROUND WAS PRIMED BY THE PR'S OWN WORDS (#550, under #621).
    #:
    #: :attr:`pr_claim` is what the round ASKED for — ``review_panel.pr_claim``, or
    #: ``panel.py --no-pr-claim`` for one run. :attr:`pr_claim_sent` is what the
    #: seats actually got: the block is charged against the tightest seat's diff
    #: budget and dropped whole where that budget cannot carry it, so a round can
    #: ask and still send nothing.
    #:
    #: **Two columns because the question they exist for cannot be answered by
    #: one.** #631 shipped the claim block always-on and left #550's own condition
    #: unmet: a body that says "this is safe because X" primes a reviewer to accept
    #: X, a primed seat reports FEWER findings, and fewer findings look like a clean
    #: PR — so whether the framing holds has to be measured across two arms of the
    #: same PRs rather than asserted. Until this pair existed the arm a round
    #: belonged to was a sentence in ``config_notes``, which no aggregation can
    #: partition on; ``pr_claim`` is the arm and ``pr_claim_sent`` is whether it was
    #: delivered. A round that asked and dropped is in NEITHER arm and has to be
    #: excluded, which a single boolean would silently score as a control.
    #:
    #: The same split, and the same argument, as :attr:`code_access` above it: the
    #: setting and what actually happened are different facts, and a configuration
    #: doing nothing is visible only in the difference.
    #:
    #: **It is also what #623's merge gate has to read before it can mean anything.**
    #: One of that gate's conditions is "no claim-miss outstanding at any severity",
    #: and a claim-miss is only a finding a seat could have raised on a round that
    #: was shown the claim. On an unprimed round the condition is satisfied by
    #: construction and says nothing — which is the shape of clean result this whole
    #: epic exists to stop producing. :attr:`pr_claim_sent` is what tells the gate
    #: which kind of silence it is looking at.
    #:
    #: NULL = the panel did not say, on both. That is every run recorded before
    #: these columns, and — by design rather than by accident — every skip and
    #: refusal path, which dispatches no seat and so primes none: ``false`` there
    #: would put a round that reviewed nothing into the unprimed arm of a comparison
    #: it was never in. No backfill: the block landed mid-population and attributing
    #: today's setting to rounds that ran before it is the exact mixing these
    #: columns exist to make visible.
    pr_claim: Mapped[bool | None] = mapped_column(Boolean)
    pr_claim_sent: Mapped[bool | None] = mapped_column(Boolean)
    #: The panel's own tally of :data:`app.api.reviews.PROVENANCE` buckets over
    #: the findings the cycle still has to clear, verbatim (v2.26).
    #:
    #: **Stored, not derived from** :attr:`ReviewFinding.provenance`. The panel
    #: counts over ``outstanding``; the finding rows also include the dismissed
    #: ones, so a derivation would quietly disagree with the round's own statement
    #: about itself. And ``{}`` — "the question does not arise", a round 1 or a
    #: run outside any cycle — is a fact no count over findings can express; it is
    #: not the same as all-zero, which says attribution ran and found nothing.
    #: NULL is the third state: nobody said.
    provenance_counts: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: #67's two tallies over the same population, on the same terms as
    #: ``provenance_counts`` directly above: stored rather than derived (the panel
    #: counts over the findings the cycle must clear, these rows also carry the
    #: dismissed ones), and ``{}`` means the question does not arise, which is not
    #: all-zero.
    #:
    #: Two objects rather than one, because the whole point of asking twice is
    #: that they can disagree: ``recurrence_counts`` is what the panel MEASURED
    #: and ``premise_counts`` is what the judge SAID. ``premise_counts`` carries a
    #: ``not-said`` bucket, which is the commonest value and would otherwise have
    #: to be inferred from a shortfall against a denominator stored elsewhere.
    recurrence_counts: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    premise_counts: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: The review dials this round ran under, as the panel APPLIED them (#643) —
    #: ``review_panel`` on the round payload, which is ``panel_seats.Dials.as_dict()``.
    #:
    #: **Opaque JSON, deliberately.** This board does not know what any dial means
    #: and must not learn (``app/api/dials.py`` argues that at length; a second
    #: place that knew what ``review_panel.max_rounds`` was is the drift #305
    #: exists to end). It is stored so a reader can hold a round's verdict against
    #: the policy it was computed under — which is the one check nothing could make
    #: before, because ``converged``'s below-floor conjunct is cut at
    #: ``cleared_floor`` and that floor lived nowhere on the row.
    #:
    #: Not a replacement for :attr:`converged`. A stored answer still beats a
    #: reconstruction, and the migration for that column says why a board-side
    #: derivation would be free to disagree with the panel about the same round.
    #: This is the working, beside the answer.
    #:
    #: NULL = the panel did not say. That is every run recorded before this column,
    #: every run whose payload predates the field, and — by design rather than by
    #: accident — every skip and refusal path, which resolve a policy but never
    #: apply one. ``{}`` is a caller that sent an empty object.
    review_panel: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: WHICH LAYER supplied each of those dials (#305, stored by #647) —
    #: ``defaults``, ``sample``, ``overlay`` or ``board`` — with the source and,
    #: for a board dial, the reason somebody gave, who set it, its scope and when
    #: it lapses. ``rules`` on the round payload, which is
    #: ``panel_core.rules_record(cfg)``.
    #:
    #: **The pair with :attr:`review_panel`, and #305 is why.** A dial VALUE with
    #: no provenance is a value a reader has to go and guess the source of, from
    #: three files and a resolution order: ``.harness-rules.sample`` stated both
    #: floors at P2 while five rounds put P4 findings in ``to_fix``, and nothing in
    #: any round's artefact could settle which was describing the run. #643 stored
    #: the values; this is the other half.
    #:
    #: It is also the ONLY field on this row that records
    #: ``escalate_on.fix_injection``. :attr:`review_panel` is
    #: ``panel_seats.Dials.as_dict()`` — twelve settings — and ``escalate_on`` is
    #: not among them; ``rules.dials`` covers every dotted path under
    #: ``review_panel.`` and ``reviewers.``, fifty-two on this repository. A
    #: recalibration of a threshold against a population that does not say what the
    #: threshold was during each round is guesswork with extra steps.
    #:
    #: **Opaque JSON**, on :attr:`review_panel`'s terms and then some: interpreting
    #: a layer would mean this board learning the resolution ORDER as well as the
    #: vocabulary, and a second implementation of "which file answered" is the
    #: drift #305 was filed over rather than a convenience.
    #:
    #: NULL = the panel did not say. Every run recorded before this column — and
    #: unlike :attr:`review_panel`, NOT the skip and refusal paths: those never
    #: apply a review policy but they certainly resolve one, and the panel sends
    #: this on every exit for exactly that reason.
    #: **Deferred**, on :attr:`unread_files`' argument and for a sharper version of
    #: it. This is the largest column on the table — ``reviews.MAX_RULES_CHARS`` is
    #: two orders above the bound on :attr:`review_panel` beside it — and no list
    #: view publishes it, so a ``GET /reviews?limit=500`` that fetched it would have
    #: Postgres ship five hundred configuration records to the app to serialise none
    #: of them. Async SQLAlchemy cannot lazy-load, so reading ``run.rules`` off a run
    #: this session did not undefer raises ``MissingGreenlet``; ``GET /review/{id}``
    #: asks with ``undefer()`` and nothing else should need to.
    rules: Mapped[dict[str, Any] | None] = mapped_column(JSONB, deferred=True)
    #: How much of the fix range the round declined to attribute because the cycle
    #: had already seen it (#559, stored by #647): ``count`` and ``files`` (a
    #: count, not a list of paths), the ``rounds`` it compared against, the
    #: ``unread`` rounds it could not read, and ``why`` where the comparison could
    #: not be made at all.
    #:
    #: The working behind :attr:`provenance_counts`, which is the answer. This is
    #: the filter that moves ``introduced``, so a threshold fitted across rounds
    #: where it ran and rounds where it did not is a threshold fitted to a
    #: denominator that changed underneath it.
    #:
    #: **Opaque JSON.** A board that read a key out of here would be a second
    #: implementation of #559's filter, free to disagree with the panel about the
    #: same round — which is what ``m6bc45ff1`` refuses for :attr:`converged`.
    #:
    #: NULL = the question did not arise. Outside a cycle, and in round 2, whose
    #: only prior round IS the anchor. Not the same as a round that looked for
    #: restored lines and found none, which sends a ``count`` of 0.
    provenance_restored: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: THE FIX PASS THIS ROUND READ (#624) — the artifact for the one actor in this
    #: loop nothing recorded.
    #:
    #: Reviewers have scorecards (:class:`ReviewReviewer`), findings have keys and
    #: terminal outcomes (:class:`ReviewFindingOutcome`), rounds have this row, and
    #: the machinery that produced a round has four columns of its own. The pass
    #: BETWEEN two rounds — which writes the code that produces the next round's
    #: findings — had nothing. On ``prisonblues/lexray#1780`` its four passes came
    #: out at +850/-314 across 11 files, +322/-49 across 9, +356/-41 across 12 (7 of
    #: them files no round had read) and +142/-31 across 7, and every one of those
    #: numbers had to be reconstructed from ``git`` by hand afterwards to file the
    #: issue.
    #:
    #: What is in it: the commit range and which of three readers supplied the diff,
    #: which round's To fix list briefed it, the production/test/prose churn split,
    #: the files it touched and which of them no earlier round had read, which of the
    #: brief's findings this round no longer raises, how many of this round's
    #: findings were attributed to it, and — segregated under ``declared`` and named
    #: as declarations — the ``narrowed``/``declined``/``escalated`` keys the pass
    #: reported. ``gaps`` is the record's own account of what it cannot say.
    #:
    #: **Opaque JSON, on :attr:`rules`' and :attr:`provenance_restored`' rule.** Every
    #: value in here was derived by the panel from the diff, the commits and the
    #: payload the pass was given; a board that re-derived one would be a second
    #: implementation free to disagree with the panel about the same pass. The one
    #: thing ingest does read is the ``counts`` sub-object, lifted verbatim into
    #: :attr:`fix_pass_counts` beside it so a run LIST can carry the numbers without
    #: the path lists — a lift, not an interpretation, and
    #: ``_fix_pass_counts_or_none`` says so.
    #:
    #: **DEFERRED**, for :attr:`rules`' reason: it carries the file list and the
    #: finding keys, so a ``GET /reviews?limit=500`` that fetched it would have
    #: Postgres ship five hundred of them to serialise none. ``GET /review/{id}``
    #: asks with ``undefer()``.
    #:
    #: **NOTHING RANKS, SCORES OR GATES ON IT, AND THAT IS A REQUIREMENT OF THE
    #: FEATURE RATHER THAN A GAP IN IT.** #624's title carries it in the parenthesis
    #: and its own second opinion supplies the argument: every obvious ratio over a
    #: fix pass is gameable in a direction worse than the disease — lines per finding
    #: cleared rewards compressed and superficial fixes, findings introduced per pass
    #: rewards weakening tests and avoiding the files most likely to be read, new
    #: files opened rewards refusing a cross-file repair that is genuinely required (a
    #: P1 left unfixed to protect a metric), and share still standing a round later is
    #: invalid under increment scope because the later round may never have re-read
    #: the repair. So the record has no actor key at all — it names the pass by its
    #: range and the round that briefed it, never the agent, model or session that
    #: performed it — no endpoint aggregates it, no index invites one, and
    #: ``GET /review/stats``, which is the leaderboard this table already feeds, does
    #: not read it. ``tests/test_review_fix_pass.py`` pins each of those.
    #:
    #: NULL = there was no pass to record, which is round 1, a run outside a cycle,
    #: and any round that reviewed nothing. Never ``{}``: a pass that could not be
    #: read gets a record with nulls in it, because "opened no file and churned no
    #: line" is the flattering direction on every claim this record makes.
    fix_pass: Mapped[dict[str, Any] | None] = mapped_column(JSONB, deferred=True)
    #: The integer summary of :attr:`fix_pass`, lifted out of the record's own
    #: ``counts`` block so that it can ride the run LIST (#624).
    #:
    #: Eleven keys at most and every value a count or NULL, which is why this one is
    #: not deferred and its parent is — the same cut :attr:`provenance_counts` and
    #: ``unread_files_count`` already make on this row, and #112's grouping-key /
    #: locator cut one field over. A population question about fix passes ("how big
    #: were the passes on rounds that then attributed nothing to them") is a question
    #: about thousands of rows, and detail-only would have meant one fetch per run to
    #: ask it — which is precisely what #624 wants possible, since the issue's own
    #: instruction is to calibrate against real cycles before anything is scored.
    #:
    #: **Counts, and no arithmetic over them.** There is deliberately no share, rate,
    #: ratio or score in here and no column that is one: the numerators and the
    #: denominators are both stored, and a consumer that wants a quotient has to
    #: write it down in its own code where somebody can argue with it. See
    #: :attr:`fix_pass` for why.
    #:
    #: NULL wherever :attr:`fix_pass` is NULL, and also where the record arrived
    #: without a readable ``counts`` block — a producer this board has not met. Not
    #: ``{}``: an empty tally would say a pass was measured and every answer was
    #: zero.
    fix_pass_counts: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: WHICH HARNESS PRODUCED THIS ROUND (#112) — four fields, because no one of
    #: them is true in every case and a field that is sometimes a lie is worse than
    #: three that are each honest about their scope.
    #:
    #: Everything above says what the round was CONFIGURED with. Nothing said what
    #: RAN it. So a leaderboard aggregated confirmed/dismissed rates over runs whose
    #: prompts, budget arithmetic and seat-loss behaviour differed, and the r1 -> r2
    #: comparison every stop argument rests on assumed both rounds were read by the
    #: same machinery — an assumption nothing on this row could check. On
    #: 2026-08-31 six merges changed ``round_stop``, ``converged``, the
    #: ``fix_injection`` accounting and ``restored_lines`` in one day, and the panel
    #: on one host was rebuilt underneath a running session.
    #:
    #: :attr:`harness_rev` is the commit of the checkout the panel ran from, and is
    #: the only AUTHORITATIVE field here: it names something a reader can go and
    #: ``git show``. It is NULL on every installed harness, which is most of them —
    #: the nix store is not a checkout — and the panel refuses to report a rev it
    #: cannot prove is the harness's own, so a scratchpad copy sitting inside some
    #: other repository records NULL rather than that repository's HEAD.
    #:
    #: :attr:`harness_dirty` is whether the digested directory carried changes that
    #: rev does not, untracked files included. It is what makes a rev honest rather
    #: than merely present: a developing panel is edited in place, and #112 was found
    #: from exactly such a copy. NULL is "no rev, or nobody could ask" — never a
    #: silent ``false``.
    #:
    #: :attr:`harness_digest` is a content hash of the loop modules, scheme-tagged
    #: (``loops-sha256-1:<hex>``). A **PROXY**, and ``qb-doctor``'s ``check_harness``
    #: says why in the same words: the truthful answer is the flake pin's rev and a
    #: running harness cannot reach it, so content stands in. It cannot name a
    #: version and it cannot say which of two is newer. It is the only field that is
    #: always present and never wrong about the one question that matters most —
    #: same code, or not — which is precisely the question an r1 -> r2 comparison
    #: needs. Compare digests only within a scheme; the tag is on the value so that
    #: a change to what is hashed cannot masquerade as a change to the harness.
    #:
    #: :attr:`harness_path` is where it all ran from. A **LOCATOR**, machine-scoped:
    #: for a nix install it doubles as an exact identity of the build, and for a
    #: scratchpad copy it is the only field that says the round did not come from
    #: the deployed harness at all. Detail-only — see ``reviews._run_view``.
    #:
    #: Stored verbatim, all four. This board does not parse a store path, does not
    #: recompute a digest and does not resolve a rev against any repository; it has
    #: no checkout of the harness to resolve one against, and a second implementation
    #: of "which harness is this" is the drift #305 exists to end.
    #:
    #: NULL = the panel did not say, on all four. That is every run recorded before
    #: these columns, and it stays a permanent answer for ``rev``/``dirty`` on every
    #: installed harness. No backfill: attributing today's harness to a round that
    #: ran under another is the exact error this column exists to make impossible.
    harness_rev: Mapped[str | None] = mapped_column(Text)
    harness_dirty: Mapped[bool | None] = mapped_column(Boolean)
    harness_digest: Mapped[str | None] = mapped_column(Text)
    harness_path: Mapped[str | None] = mapped_column(Text)
    changed_lines: Mapped[int | None] = mapped_column(Integer)
    #: GitHub's own count of the PR's changed files (v2.23), stored beside the
    #: rows in :class:`ReviewRunFile` rather than derived from them. When the two
    #: disagree the stored list is partial — GitHub caps a PR's file list at
    #: 3,000 — and a collision query over it under-reports. NULL for every run
    #: recorded before the panel sent it, which is NOT the same as zero files.
    changed_files_total: Mapped[int | None] = mapped_column(Integer)
    #: The PR's state as of THIS panel — `OPEN` / `MERGED` / `CLOSED`, verbatim
    #: from `gh pr view --json state`. Recorded because the board otherwise holds
    #: no PR state at all, and a collision query without it reports every PR
    #: panelled in the window as a live rival, merged ones included: on a repo
    #: landing several a week the answer is mostly PRs that no longer exist,
    #: which is how an advisory endpoint stops being read.
    #:
    #: **As of the last panel, never live.** The board is told about panels, not
    #: about merges, so a PR merged after its final round still reads OPEN here.
    #: That is the same currency as `changed_files` itself, and why every read
    #: path hands back the run's `ts` beside it. NULL for every pre-v2.23 run.
    pr_state: Mapped[str | None] = mapped_column(Text)
    #: A draft PR is open and not landing yet, which is a different thing to
    #: collide with. Separate from `pr_state` because GitHub's `state` does not
    #: encode it — a draft's state is `OPEN`.
    is_draft: Mapped[bool | None] = mapped_column(Boolean)
    #: Did a reviewer read anything on this run (#94)? Three states, and the
    #: third is the one that makes the column safe to add to a table with
    #: history in it:
    #:
    #: * ``True`` — a panel ran. Every seat's account is in
    #:   :class:`ReviewReviewer` and the findings are real observations.
    #: * ``False`` — this run reviewed nothing and says so. The title-pattern
    #:   skip (a merge, a promote, a format-the-world commit) and the pre-flight
    #:   refusal both exit here. The row exists for what it MEASURED — the PR's
    #:   changed-file list, its state, the head it moved to — not for a verdict
    #:   it never reached.
    #: * ``NULL`` — nobody said. Every row recorded before this column, and the
    #:   only honest value for them: the board holds refusals among those rows
    #:   and cannot tell which they are, so asserting ``True`` would make a new
    #:   column knowingly wrong about a known class of row.
    #:
    #: **The reading rule, because the two are not interchangeable.** A question
    #: whose wrong answer is a false all-clear asks ``reviewed IS TRUE`` — it
    #: must not be satisfied by a row nobody can vouch for. A count that exists
    #: to match what has already been published asks ``reviewed IS NOT FALSE``,
    #: which keeps every legacy row where it has always been and excludes only
    #: the runs that state outright that they reviewed nothing. Sites doing the
    #: second are marked; every other reader wants the first.
    #:
    #: Not derivable, which is why it is stored. "No scorecards and no findings"
    #: is also what a pre-v2.15 payload looks like when its reviewers were
    #: inferred from finding attribution, so a derivation would have to guess
    #: over exactly the history this column refuses to guess about.
    reviewed: Mapped[bool | None] = mapped_column(Boolean)
    #: Why nothing was reviewed, in the panel's own words — ``title matches skip
    #: pattern /^Merge /``, or a pre-flight refusal's reason. NULL wherever
    #: ``reviewed`` is not ``False``.
    #:
    #: A second column rather than a richer ``reviewed``, because the two are
    #: read by different people. ``reviewed`` is a predicate a query filters on;
    #: this is a sentence a human reads off a collision row to decide whether a
    #: 400-file merge is worth looking at by hand. The panel has sent it on both
    #: non-review exits since long before this release and the board discarded it
    #: — storing it invents nothing.
    skip_reason: Mapped[str | None] = mapped_column(Text)
    diff_chars: Mapped[int | None] = mapped_column(Integer)
    diff_truncated: Mapped[bool | None] = mapped_column(Boolean)

    # How it was adjudicated. An unjudged run keeps every finding (the panel
    # never suppresses), so its findings must NOT count towards precision —
    # hence the flag rather than inferring from the verdicts.
    judged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    judge_model: Mapped[str | None] = mapped_column(Text)
    judge_skip: Mapped[str | None] = mapped_column(Text)
    #: The judge's ruling on the coverage the reviewers declared — the split
    #: between "clean" and "could not tell", adjudicated rather than averaged.
    coverage_note: Mapped[str | None] = mapped_column(Text)

    # Where this run sat in the panel -> fix -> panel cycle (v2.15). A PR's round
    # COUNT is derivable by counting its runs; what is not derivable is what each
    # round found that the one before it had not, and what stopped the loop.
    #: 1 for a first review, 2+ for a re-review of the fix commit.
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    #: Which panel -> fix -> panel CYCLE this round belongs to. Every round of one
    #: cycle carries the same opaque id (the panel inherits it from its earliest
    #: baseline), so "the re-review of THIS round's declaration" is a join rather
    #: than the guess "whatever ran next on this PR" — two agents looping the same
    #: PR interleave, and a positional rule credits one cycle's round 2 to the
    #: other's round 1. NULL for every run recorded before the panel sent it.
    cycle: Mapped[str | None] = mapped_column(Text)
    #: Findings this round that no earlier round raised. NULL where the panel
    #: never said — "not reported" and "nothing new" are different facts, and
    #: storing the second for the first is how a pre-v2.15 run reads as converged.
    new_findings: Mapped[int | None] = mapped_column(Integer)
    #: Whether the cycle actually STOPPED here. A round that ends with findings
    #: outstanding sends ``stop: false`` and carries a reason for going again, so
    #: reading the reason as "this is where it stopped" labels a cycle that must
    #: continue as finished. NULL where the panel didn't say.
    stopped: Mapped[bool | None] = mapped_column(Boolean)
    #: What ended the loop, in the panel's words: dry / a P1-P2 still outstanding /
    #: the round cap. A cap reached with work outstanding is not convergence.
    #: Also carries the reason to go AGAIN when ``stopped`` is false — the two are
    #: told apart by that column, never by the prose.
    stop_reason: Mapped[str | None] = mapped_column(Text)
    #: Whether that stop was EARNED. False when a reviewer was truncated, absent,
    #: unparsed, or declared a gap — the cases where a counter reading zero says
    #: nothing about the code. This is the column that lets a human review the
    #: review without re-reading the transcript.
    stop_confident: Mapped[bool | None] = mapped_column(Boolean)
    #: WHY it was unearned, verbatim from the panel ("codex saw 60,000 of 118,402
    #: diff chars", "claude could not assess: the migration"). ``stop_confident``
    #: says a clean verdict was not evidence; without the reasons the reader has
    #: no way to judge how badly — which is the question this release exists to
    #: answer, and the one the operator is told to relay.
    stop_veto: Mapped[list[Any] | None] = mapped_column(JSONB)
    #: Whether this round was a **clean finish** — the one boolean the convergence
    #: epic is judged on (#626), and strictly stronger than ``stop_confident``.
    #:
    #: The panel computes it in ``round_stop`` FROM ``confident`` and publishes it
    #: on the round payload; this column is that answer stored verbatim. It is not
    #: re-derived here and must never be: the conjuncts it is built from are the
    #: round's own ``outstanding.fixable``, its below-floor set and its escalation
    #: register, and the floors those are cut at are repo dials the board holds as
    #: opaque JSON and does not interpret (``app.api.dials`` argues that at length).
    #: A board-side derivation would therefore be a second implementation of a
    #: policy the board cannot read, free to disagree with the panel about the
    #: same round — and a convergence metric that can disagree with the panel is
    #: worse than no metric, because the direction it drifts in is the flattering
    #: one.
    #:
    #: Three states, and the third is why this is nullable with no backfill:
    #:
    #: * ``True`` — a stop, not capped, no veto, and nothing left: nothing a fix
    #:   pass could take, nothing under the cleared floor, no escalation held.
    #: * ``False`` — the round stopped or went again and it was not that. A
    #:   below-floor policy stop is the case worth naming: it is ``stopped``,
    #:   ``stop_confident`` **and** ``converged: False``, because real findings
    #:   were left unfixed by policy (#165) and counting it would flatter the loop.
    #: * ``NULL`` — the panel did not say. Every round recorded before this column,
    #:   #631's own rounds included: they POSTed ``converged`` and ingest dropped
    #:   it, so nothing on those rows says which they were. They are excluded from
    #:   the rate ``GET /review/convergence`` publishes rather than counted as
    #:   failures.
    #:
    #: This is NOT what ``app.review_queue`` gates ``ready``/``land`` on. That
    #: gate is ``stopped`` + ``stop_confident`` + no outstanding findings, which
    #: is strictly looser, and it stays looser deliberately: a below-floor policy
    #: stop is a landable PR and an unconverged cycle at the same time. The two
    #: questions are "may this land" and "did the loop finish cleanly", and
    #: collapsing them would either block landings this repo's own policy allows
    #: or count them as clean finishes they are not.
    converged: Mapped[bool | None] = mapped_column(Boolean)

    # Hard gates that sit alongside the LLM panel.
    sonar_gate: Mapped[str | None] = mapped_column(Text)
    ci_status: Mapped[str | None] = mapped_column(Text)

    reviewers_selected: Mapped[list[str] | None] = mapped_column(JSONB)
    reviewers_override: Mapped[str | None] = mapped_column(Text)
    skipped: Mapped[list[str] | None] = mapped_column(JSONB)

    # Denormalised run totals, so the run list renders without touching findings.
    n_confirmed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    n_dismissed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    n_unjudged: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    n_sonar: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    #: Client-chosen idempotency key. A panel run that records, times out on the
    #: response and retries would otherwise double-count itself into the stats.
    run_key: Mapped[str | None] = mapped_column(Text, unique=True)

    __table_args__ = (
        Index("ix_review_runs_repo_pr", "repo", "pr"),
        Index("ix_review_runs_ts", "ts"),
        Index("ix_review_runs_author", "author"),
        # The API is not the only writer, and a round 0 or a negative count breaks
        # run ordering and the published statistics (see migration 0014).
        CheckConstraint('"round" >= 1', name="ck_review_runs_round_positive"),
        CheckConstraint("new_findings >= 0",
                        name="ck_review_runs_new_findings_non_negative"),
        # A run that reviewed nothing cannot also have earned a confident stop
        # (#94). `stop_confident` is what `preland --require-earned-stop` reads
        # and what the review queue calls convergence, so the one combination
        # that must be unrepresentable is "no reviewer read anything, and the
        # cycle stopped confidently" — a non-event certifying a PR as done.
        #
        # At the boundary rather than only at ingest, for the reason the round
        # and repo constraints above give: the API is not the only writer, and a
        # write path added later must not be able to reintroduce it quietly.
        # NULL on either side passes — `reviewed IS NULL` is every legacy row and
        # `stop_confident IS NULL` is "the panel didn't say".
        CheckConstraint("NOT (reviewed IS FALSE AND stop_confident IS TRUE)",
                        name="ck_review_runs_unreviewed_not_confident"),
        # #626: `converged` is computed by the panel FROM `confident`, which is
        # itself `stop and not capped and not veto and baseline_ok`. So a
        # converged round is a stopped round and an earned one, by construction
        # and not by coincidence — and the pair that must be unrepresentable here
        # is a round claiming a clean finish while its own stop says it never
        # stopped, or says the stop was not evidence.
        #
        # At the boundary rather than only at ingest, on the rule the three
        # constraints above give: the API is not the only writer. A hand-rolled
        # POST is coerced at ingest and told what was dropped
        # (`ReviewIn._converged_cannot_outrun_the_stop`), so this constraint is
        # unreachable from the endpoint — which is the point. It exists for the
        # write path added later.
        #
        # NULL on any side passes. `converged IS NULL` is every row recorded
        # before the column existed, and `stopped`/`stop_confident` NULL is a
        # panel that never spoke; a constraint that refused those would make the
        # migration unrunnable rather than make the rows honest.
        CheckConstraint(
            "NOT (converged IS TRUE AND "
            "(stopped IS NOT TRUE OR stop_confident IS NOT TRUE))",
            name="ck_review_runs_converged_implies_earned_stop",
        ),
        # One repository, one stored spelling — at the boundary, so that the API
        # is not the only thing that remembers (#326, migration 0033).
        #
        # GitHub folds owner and repository names and preserves what you typed,
        # so `PrisonBlues/Quarterback` and `prisonblues/quarterback` are one repo
        # this column held as two — and every read compares it with `==`. The
        # visible cost was `GET /review/collisions` answering `considered: 0`:
        # an all-clear made of nothing having matched, on the endpoint written to
        # make exactly that unrepresentable.
        #
        # `POST /review` folds through `canonical_repo` now, and the point of
        # ALSO saying it here is that a write path added later cannot quietly
        # reintroduce the second spelling: the read sites this unblocked (#326
        # deleted the `func.lower()` folds in `app.api.plan` and
        # `app.api.review_queue`) depend on the column, not on the endpoint.
        # Case and surrounding space only — NOT `owner/name` shape. The shape is
        # refused at ingest where a caller can be told why; rows written before
        # that check existed are legitimately here, and a constraint that
        # rejected them would make this migration unrunnable rather than make
        # them canonical.
        # `btrim` is given its character class because the one-argument form trims
        # spaces and nothing else, while `canonical_repo`'s `str.strip()` takes
        # every whitespace character — see migration 0033. Two rules disagreeing
        # about one column is what this constraint exists to prevent.
        #
        # Vertical tab is `\013` and NOT `\v`, for the reason
        # `ck_review_finding_reports_needs_human_evidence` gives below: Postgres
        # has no `\v` escape, so it reads as the literal letter — and `btrim`
        # would then strip a `v` off the ends of a repository name. `vercel/next`
        # trims to `ercel/next` and fails this very constraint.
        CheckConstraint(r"repo = lower(btrim(repo, E' \t\n\r\f\013'))",
                        name="ck_review_runs_repo_canonical"),
    )


class ReviewReviewer(Base):
    """One panel member's scorecard for one run — the row the stats aggregate.

    Counts are computed server-side from the findings rather than sent, so the
    scorecard cannot disagree with the findings it summarises.

    ``model``/``effort`` are recorded per run because they drift: a repo's
    ``.harness-rules`` gets repinned, a slug is retired, a run is hand-picked
    with ``--reviewers``. Grouping stats by (name, model, effort) is what makes
    "is the higher tier worth it" answerable at all — the same vendor at two
    tiers is two competitors, not one.
    """

    __tablename__ = "review_reviewers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)  # claude|codex|gemini|pi|sonarqube
    model: Mapped[str | None] = mapped_column(Text)
    effort: Mapped[str | None] = mapped_column(Text)

    #: False when the vendor was selected but never reviewed (CLI absent, model
    #: refused, auth expired). Kept as a row rather than dropped: a reviewer that
    #: keeps failing to run is a finding about the panel, invisible if unrecorded.
    ran: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    skip_reason: Mapped[str | None] = mapped_column(Text)

    max_diff_chars: Mapped[int | None] = mapped_column(Integer)
    #: The reviewer read a PREFIX of the diff, at ``max_diff_chars``. Measured by
    #: the panel, never asked for: the one thing a truncated reviewer cannot
    #: notice is its own truncation, and a member that saw half the diff must be
    #: distinguishable from one that saw all of it on every row it contributed to.
    truncated: Mapped[bool | None] = mapped_column(Boolean)
    #: What this member said it could NOT judge — a file the diff omits, a runtime
    #: behaviour, a schema it cannot see. An observation, not a forecast, and the
    #: only thing that separates "clean" from "I could not tell"; a finding count
    #: reports both as zero. NULL = no structured declaration was obtained — the
    #: member was never asked (every pre-v2.15 panel), its CLI answered in the old
    #: bare-array shape, or its reply did not parse at all (see ``unstructured``);
    #: [] = asked, and it had nothing to declare. The two states must not collapse
    #: — that is the whole point of the column — so this says it the same way
    #: ``app.api.reviews.ReviewerIn.could_not_assess`` does.
    could_not_assess: Mapped[list[Any] | None] = mapped_column(JSONB)
    #: This member's reply carried no JSON and was kept as one raw finding. Its
    #: findings are real work, but nothing it might have declared survived the
    #: parse — so it lands on NULL ``could_not_assess`` for a reason that has
    #: nothing to do with never being asked, and only this column tells the two
    #: apart. Without it an unparsed reviewer is invisible to the honesty stats,
    #: which is the same NULL/[] collapse one level up. NULL = the panel didn't say.
    unstructured: Mapped[bool | None] = mapped_column(Boolean)
    #: This box does not carry the reviewer's CLI (#113). A fact about the HOST
    #: rather than about the round — it is absent every round — which is why
    #: `coverage_veto` reports it without spending the round's confidence on it.
    #: Stored because the exemption is only defensible if the thing being exempted
    #: is visible: an unattended host that quietly reviews with two of four seats
    #: looks identical to a full panel in every other column here.
    #: NULL = the panel didn't say (every payload before this release).
    absent: Mapped[bool | None] = mapped_column(Boolean)
    #: This member reviewed from the diff alone — no reading of the code under
    #: review (#113). The single most important confound in this table: a seat that
    #: could open the caller and one that could not are not comparable on findings,
    #: precision, or `could_not_assess`, and until this column existed nothing
    #: recorded which was which. Also what makes the veto exemption auditable — a
    #: blind seat's declarations are reported and not counted, and that is only a
    #: defensible trade if you can later ask how often it applied.
    #: NULL = the panel didn't say.
    code_blind: Mapped[bool | None] = mapped_column(Boolean)
    #: This member's ``truncated`` was the KERNEL's doing, not a budget's (#113).
    #: `agy`'s prompt travels in argv and one element is capped, so on a large diff
    #: it structurally cannot be handed all of it. Separated from `truncated`
    #: because the two have opposite remedies: a budget can be raised and is
    #: evidence about the round, while this is a property of the box and is
    #: exempted from the veto. NULL = the panel didn't say.
    argv_capped: Mapped[bool | None] = mapped_column(Boolean)
    #: Findings this member flagged as needing the FIX re-read. With the next
    #: round's new findings this is the accuracy check on the declaration itself —
    #: the raw material for honesty per reviewer, which precision cannot show.
    rereview_flagged: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: Findings this member declared no fix round can settle — ``needs_human``
    #: (#279). The ``rereview_flagged`` treatment, and it exists for a sharper
    #: reason than symmetry: a flag is a way OUT of work, so #67's "do not
    #: escalate to end a cycle you find tedious" is only enforceable if the rate
    #: at which each seat reaches for it is on the row. A seat that flags `taste`
    #: on everything is then measurable, and so is one that never flags `ui` on a
    #: TUI change.
    #:
    #: Tallied over CONFIRMED findings, like every sibling counter here and for
    #: the same reason ``rereview_flagged`` is: a declaration attached to a
    #: finding the judge dismissed is not a claim worth scoring.
    human_flagged: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: Wall-clock for this reviewer's whole turn, every CLI attempt included.
    #: The cost axis that is comparable *across* vendors — unlike the token
    #: counts below, a second is a second whoever spent it.
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    #: What the turn cost in tokens, read back out of the vendor's own session
    #: after the fact (the panel pins a session id up front rather than switching
    #: the CLI to a JSON output mode, which would put the findings inside an
    #: envelope on the one path that currently works).
    #:
    #: Null means *not recorded*, never zero: a vendor may not state a figure, or
    #: the transcript read may have failed, and both must stay distinguishable
    #: from a reviewer that genuinely spent nothing.
    #:
    #: Only comparable **within** a vendor — different tokenizers, different cache
    #: semantics. A "say hi" to Claude billed almost entirely against 10k
    #: cache-*creation* tokens on a two-token prompt; ranking vendors by this
    #: would be noise. Grouped by (name, model, effort), though, it is the answer
    #: to "is the higher tier worth it".
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer)
    #: Thinking tokens where the vendor separates them; folded into ``output``
    #: where it doesn't, which is another reason not to race two vendors on these.
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer)
    #: Only where the **vendor states it**, never derived from a price table: a
    #: run priced at today's rates is silently wrong when queried in six weeks,
    #: and the point of this table is that it stays true later.
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))

    raised: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    confirmed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    dismissed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    unjudged: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: Confirmed findings no other panel member raised — the marginal value of
    #: keeping this reviewer on the panel, which a raw count can't show.
    solo: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: Findings (any verdict) at least one other member also reported. With
    #: ``raised`` this is the consensus rate: a member that agrees with everyone
    #: and a member that only ever reports alone are different propositions even
    #: at the same precision.
    shared: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    #: Severity calibration against the judge, over confirmed findings where the
    #: member sent its own severity (``reported_by[].severity``). "Stricter" =
    #: the member rated it more severe than the judge settled on. A reviewer that
    #: is right but always cries P1 costs triage time, which precision can't say.
    sev_stricter: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sev_agree: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sev_looser: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    p1: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    p2: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    p3: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    p4: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    #: #48's axis, at the grain #48 asked for it (v2.26): of the defects THIS
    #: member found, how many did the previous fix pass introduce and how many had
    #: been sitting there all along? Those are different competencies and a
    #: confirmed-finding count cannot see either.
    #:
    #: Tallied here at ingest from :attr:`ReviewFinding.provenance`, like every
    #: sibling counter, so a scorecard cannot contradict the findings it
    #: summarises — and so the leaderboard is a ``SUM`` rather than a three-table
    #: join re-deriving per-reviewer attribution on every page load.
    #:
    #: **Over CONFIRMED findings only**, the same population as ``p1``..``p4`` and
    #: ``solo``. A dismissed finding was not a defect, so attributing its cause to
    #: a fix pass would credit a reviewer for spotting something that was not
    #: there. This makes them deliberately narrower than the run's own
    #: :attr:`ReviewRun.provenance_counts`, which the panel computes over
    #: everything still outstanding.
    #:
    #: 0 and "not recorded" are one value here — the price of matching the
    #: siblings — so ``GET /review/stats`` publishes a ``provenance_runs``
    #: coverage marker beside the sums, read off the run's ``provenance_counts``.
    #: Read a bare zero without it and a window of pre-v2.26 runs looks like a
    #: panel that never caught a regression.
    prov_introduced: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0")
    prov_missed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0")
    #: The bucket that indicts the HARNESS rather than the panel: the earlier
    #: round was truncated out of that file, so nobody could have caught it.
    prov_missed_unread: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0")
    #: Asked, and unplaceable — an unreadable fix range, a finding with no file.
    #: A real answer, and NOT the same as the NULL on a finding nobody asked about.
    prov_unknown: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        UniqueConstraint("run_id", "name", name="uq_review_reviewer_run_name"),
        Index("ix_review_reviewers_name_model", "name", "model"),
        CheckConstraint("rereview_flagged >= 0",
                        name="ck_review_reviewers_rereview_flagged_non_negative"),
        CheckConstraint("human_flagged >= 0",
                        name="ck_review_reviewers_human_flagged_non_negative"),
    )


class ReviewFinding(Base):
    """One merged finding from a run, with the judge's verdict on it.

    Stored per run (not per PR): the same defect found again after a fix loop is
    a new observation, and collapsing the two would erase the fix. ``key`` is
    what links those observations without collapsing them — see below.

    ``title``/``detail`` are the judge's synthesis of the group. What each
    reviewer actually said lives in :class:`ReviewFindingReport`, one row per
    reporter, so a merge is additive rather than a survivor of a coin toss.
    """

    __tablename__ = "review_findings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False
    )
    #: confirmed | dismissed | unjudged | sonar (the hard gate's own issues)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str | None] = mapped_column(Text)  # P1..P4, post-judge
    file: Mapped[str | None] = mapped_column(Text)
    line: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)  # the judge's rationale

    #: Identity of the *defect*, so two observations of it can be joined without
    #: being merged: "was this actually fixed?" and "how many rounds did this PR
    #: take?" are then queries. Scoped by the run's (repo, pr) at read time, so
    #: the same key in another repo is a different chain. Client-supplied when
    #: the panel has a stable id of its own; otherwise derived from file + a
    #: normalised title, deliberately *without* the line — a line number moves
    #: when the fix above it lands, and an identity that moves links nothing.
    finding_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: Other findings in the same run that share a cause and should be fixed
    #: together, as their ``finding_key``s. A decision spread over four files is
    #: not one finding, but it is one fix — no positional rule can say that.
    related: Mapped[list[Any] | None] = mapped_column(JSONB)

    reviewers: Mapped[list[Any] | None] = mapped_column(JSONB)  # ["codex", "gemini"]
    #: Denormalised len(reviewers) so consensus/solo queries don't unnest JSONB.
    n_reviewers: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    #: A reporter declared that fixing this takes a structural change whose RESULT
    #: should be re-read. Stored on the finding as well as per reporter
    #: (:class:`ReviewFindingReport`) because that is the grain the next round is
    #: checked against: did the round that followed find something here?
    needs_rereview: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    #: No fix round can settle this: it needs a person (#279). NOT
    #: ``could_not_assess`` one table over, and the difference is load-bearing —
    #: that field means "I lacked context", a gap a tool or a wider scope closes,
    #: and this means "no context would close this". Collapsing them would put a
    #: grep-able question and a design decision in one bucket.
    #:
    #: Stored on the finding as well as per reporter
    #: (:class:`ReviewFindingReport`) for the reason ``needs_rereview`` is: the
    #: finding is the grain a fix round is briefed against — a flagged finding is
    #: never re-briefed to a fixer and never counts against convergence — while
    #: the per-reporter row is the grain the DECLARATION is judged at. A group
    #: flag credited to everyone who happened to raise the finding makes the
    #: member that called it and the member that didn't indistinguishable.
    needs_human: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    #: WHICH judgement is owed — one of :data:`app.needs_human.NEEDS_HUMAN_CLASSES`.
    #: The class is the point rather than decoration: a bare "needs a human" says
    #: stop without saying who or what for, and the class is what routes it. A
    #: ``ui`` flag means somebody has to look at a terminal; an ``auth`` flag
    #: means somebody has to try the credential path on a real box.
    needs_human_class: Mapped[str | None] = mapped_column(Text)
    #: Why, in a line. Required whenever the flag is set — at the database, not
    #: only in the API — because a bare flag is a confident assertion with
    #: nothing behind it, and this one ENDS a cycle. #67: do not escalate to end
    #: a cycle you find tedious.
    needs_human_reason: Mapped[str | None] = mapped_column(Text)
    #: This observation was not raised by any earlier round of the same PR — the
    #: dry-round counter, per finding. NULL where the panel didn't say.
    new_this_round: Mapped[bool | None] = mapped_column(Boolean)
    #: Did the previous round's FIX introduce this defect, or did that round MISS
    #: it (v2.26)? One of :data:`app.api.reviews.PROVENANCE`. The two were one
    #: number (``new_this_round``) and they want opposite remedies: self-inflicted
    #: findings say make fix passes smaller, missed ones say the earlier round
    #: under-read and coverage is worth paying for.
    #:
    #: **The field this whole release exists for.** It is per finding, so unlike
    #: the run-level columns it cannot be reconstructed later from anything else
    #: the board keeps — every round that ran while it was dropped is gone.
    #:
    #: NULL where the question does not arise: outside a cycle, in a round 1, for
    #: a defect an earlier round already raised, or for any run recorded before
    #: this column existed. ``"unknown"`` is the opposite state — the question was
    #: asked and the answer could not be placed — and the two must never collapse.
    #:
    #: A SIGNAL, not a verdict. ``introduced`` requires exact membership in the
    #: fix's added lines, so a defect introduced by a DELETION and an ordinary
    #: reviewer line-drift both land in ``missed``: read the ``introduced`` count
    #: as a floor. #41 (review the increment) is what makes it exact.
    provenance: Mapped[str | None] = mapped_column(Text)
    #: Is this finding standing where the last fix pass was WORKING, on a
    #: complaint that pass was sent to answer (#67)? One of
    #: :data:`app.api.reviews.RECURRENCE`.
    #:
    #: The question after ``provenance``, and a different one. That column asks
    #: whether the fix wrote the line this sits on — an accusation about
    #: authorship, so it demands exact membership and reads low. This asks whether
    #: the fixes are making PROGRESS on the defect, so it takes a neighbourhood:
    #: the drift ``provenance`` must refuse (a reviewer naming the top of the
    #: enclosing function) is the drift this has to absorb.
    #:
    #: ``revisited`` is the conjunction of three things — the previous round raised
    #: a finding in this file, the fixer wrote lines in it, and this finding is
    #: near them. ``fix-site`` is two of the three: the fixer was here, nobody had
    #: complained here.
    #:
    #: **A position, not a verdict, and the names say so on the strength of a
    #: measurement.** Replayed over 36 rounds of this board's own history,
    #: ``revisited`` fires on roughly four new findings in five and does NOT
    #: separate the cycles #67 identifies as circling from the rest — because under
    #: #41's increment scope a later round is reading the fix commit, so a finding
    #: at the fix's site is the ordinary case. Read this column as context and as a
    #: denominator; ``premise_verdict`` below is the half that can see whether one
    #: premise is being patched twice.
    #:
    #: NULL where the question does not arise (round 1, outside a cycle, a defect
    #: an earlier round already raised, a run recorded before the column existed).
    #: ``"unknown"`` is the opposite state — asked, unplaceable — and the two must
    #: never collapse.
    #:
    #: **Nothing stops on it.** #67 asks for the instrument before the gate: its
    #: whole evidence base is a handful of pull requests, so this is measured,
    #: reported and read by no stop rule.
    recurrence: Mapped[str | None] = mapped_column(Text)
    #: WHICH earlier finding this one stands on, as that finding's own
    #: ``finding_key`` — set only under ``revisited`` (the CHECK enforces it).
    #:
    #: A pointer, not a copy, and it is what makes the bucket auditable. A signal
    #: nobody has calibrated is worth exactly as much as the ability to go back and
    #: check its labels against the record they were computed from, and "which
    #: premise did it think was being circled" is that check.
    recurs_of: Mapped[str | None] = mapped_column(Text)
    #: What the JUDGE said when asked #67's sharper question directly: does this
    #: finding invalidate the premise of the fix that preceded it, or is it a
    #: separate defect? One of :data:`app.api.reviews.PREMISE_VERDICTS`.
    #:
    #: Asked of the judge because the mechanical column above cannot answer it.
    #: ``recurrence`` can see that a fixer was working where this finding stands;
    #: it cannot see whether the finding says that fixer's ASSUMPTION was wrong,
    #: which is the distinction the whole issue turns on. The judge is the only
    #: party in the round already holding both the earlier round's complaints and
    #: the commit that answered them, so it costs one extra key on a verdict it is
    #: already writing rather than a second model call.
    #:
    #: Stored BESIDE the mechanical bucket and never folded into it. Two
    #: witnesses, one mechanical and one adjudicated; the rounds where they
    #: disagree are the rows worth reading, and a blended number would hide them.
    #:
    #: NOT the fixer's declared premise (#84's register), which is the other thing
    #: called a premise in this system and is a self-report. #67's record of PR #88
    #: is why both exist: the agent that wrote round 1's fix wrote round 2's
    #: regression of the same shape in the same commit as a docstring stating the
    #: invariant it broke.
    premise_verdict: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_review_findings_run", "run_id"),
        Index("ix_review_findings_verdict", "verdict"),
        Index("ix_review_findings_key", "finding_key"),
        # What is waiting on a human, by class — the read `GET /review/needs-human`
        # exists to answer. Partial, because a flagged finding is meant to be a
        # small minority of the table and an index over every row would be paid
        # for on every ingest to serve a query that only ever wants the few.
        Index("ix_review_findings_needs_human", "needs_human_class",
              postgresql_where=text("needs_human")),
        CheckConstraint(
            f"needs_human_class IS NULL OR needs_human_class IN ({_NH_CLASS_LIST})",
            name="ck_review_findings_needs_human_class",
        ),
        # The evidence rule, both ways round, at the boundary rather than only in
        # the API — for a backfill, an admin script, or the next write path.
        #
        # Forward: a flag with no class and no reason is the bare confident
        # assertion this whole feature exists to measure, arriving one level up.
        # Backward: a class or a reason with no flag is evidence for a judgement
        # nobody made, which would sit in the table looking exactly like one that
        # was later withdrawn.
        #
        # `btrim` with an explicit character set, and vertical tab spelled `\013`
        # rather than `\v` — Postgres' escape strings do not define `\v`, so
        # `E'\v'` is the LETTER v and the set would quietly refuse a reason of
        # "v" as empty. Both traps are documented at length on
        # `ck_review_finding_outcomes_refuted_note`; this is the same rule.
        CheckConstraint(
            r"(needs_human AND needs_human_class IS NOT NULL "
            r"AND needs_human_reason IS NOT NULL "
            r"AND btrim(needs_human_reason, E' \t\n\r\f\013') <> '') "
            r"OR (NOT needs_human AND needs_human_class IS NULL "
            r"AND needs_human_reason IS NULL)",
            name="ck_review_findings_needs_human_evidence",
        ),
        # #67's two vocabularies, at the boundary and not only in the API — the
        # rule `ck_review_findings_needs_human_class` follows above. `provenance`
        # carries no such CHECK and predates the convention; both of these are
        # COUNTED, and a value outside the vocabulary would leave the numerator
        # while still counting as coverage.
        CheckConstraint(
            "recurrence IS NULL OR recurrence IN "
            "('revisited', 'fix-site', 'elsewhere', 'unknown')",
            name="ck_review_findings_recurrence",
        ),
        CheckConstraint(
            "premise_verdict IS NULL OR premise_verdict IN "
            "('invalidates', 'separate', 'unclear')",
            name="ck_review_findings_premise_verdict",
        ),
        # One-directional on purpose. A `recurs_of` under any other bucket names a
        # circle the measurement did not find — evidence for a judgement nobody
        # made. The other way round is left open: a `revisited` naming no earlier
        # key is incomplete, not false, and a producer too old to send the pointer
        # should still be able to send the bucket.
        # `IS NOT DISTINCT FROM` and never `=`: a CHECK passes on NULL as well as
        # on true, and `recurrence = 'revisited'` is NULL for every row whose
        # `recurrence` is NULL — which is the very row this refuses. Spelled with
        # `=` it accepted a pointer attached to no measurement at all.
        CheckConstraint(
            "recurs_of IS NULL OR recurrence IS NOT DISTINCT FROM 'revisited'",
            name="ck_review_findings_recurs_of_revisited",
        ),
    )


class ReviewFindingReport(Base):
    """What one reviewer actually said about one finding — verbatim.

    The panel used to merge before the judge and keep a single representative's
    text, so two models describing the same bug from different angles left one
    description on the floor. This is where the other accounts live: merging
    becomes additive, the synthesis is new and the originals ride along, and a
    human can audit whether the merge was right.

    It also turns attribution into a stored fact rather than an inference.
    ``severity``/``line`` are *this reviewer's own*, which differ from the
    judge's and are the raw material for calibration stats; "confirmed findings
    where pi was the sole reporter" becomes a join rather than a JSONB unnest.

    Fed by callers that send ``reported_by``, ``panel.py`` among them: its merge
    lives in the judge, which writes a new synthesis and keeps every member's own
    report beside it. An older payload that sends reviewer NAMES only leaves this
    table empty for its run — the finding's own ``rereview_by`` then carries what
    attribution there is, and the calibration counters stay at zero.
    """

    __tablename__ = "review_finding_reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    finding_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("review_findings.id", ondelete="CASCADE"), nullable=False
    )
    reviewer: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str | None] = mapped_column(Text)  # what this reviewer called it
    line: Mapped[int | None] = mapped_column(Integer)  # where this reviewer put it
    account: Mapped[str | None] = mapped_column(Text)  # verbatim, never rewritten
    #: THIS reviewer said the fix for this finding needs re-reading. Per reporter,
    #: not per finding, because the declaration's accuracy is per reviewer: a
    #: group flag credited to everyone who happened to raise the finding makes the
    #: member that called it and the member that didn't indistinguishable.
    needs_rereview: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    #: THIS reviewer said no fix round can settle this finding (#279). Per
    #: reporter, not per finding, for the reason ``needs_rereview`` above is: the
    #: declaration's accuracy is per reviewer, and this one is a way out of work
    #: — so who reached for it, and how often, is the measurement. A group flag
    #: makes the member that called it and the member that didn't identical.
    needs_human: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    #: This reviewer's own class and reason, which may differ from the finding's
    #: synthesis — two members can agree a human is needed and disagree about
    #: what for, and that disagreement is data rather than a merge conflict.
    needs_human_class: Mapped[str | None] = mapped_column(Text)
    needs_human_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("finding_id", "reviewer", name="uq_review_report_finding_reviewer"),
        Index("ix_review_finding_reports_finding", "finding_id"),
        Index("ix_review_finding_reports_reviewer", "reviewer"),
        # The same two rules the finding carries, on the row that attributes the
        # declaration. Not redundant with them: this table is written from a
        # different branch of the ingest (a reporter's own flag, which may be set
        # where the finding's is not yet resolved), and `/review/stats` scores
        # THESE rows — so a bare flag arriving here lands directly in a published
        # per-reviewer figure.
        CheckConstraint(
            f"needs_human_class IS NULL OR needs_human_class IN ({_NH_CLASS_LIST})",
            name="ck_review_finding_reports_needs_human_class",
        ),
        CheckConstraint(
            r"(needs_human AND needs_human_class IS NOT NULL "
            r"AND needs_human_reason IS NOT NULL "
            r"AND btrim(needs_human_reason, E' \t\n\r\f\013') <> '') "
            r"OR (NOT needs_human AND needs_human_class IS NULL "
            r"AND needs_human_reason IS NULL)",
            name="ck_review_finding_reports_needs_human_evidence",
        ),
    )


class ReviewFindingOutcome(Base):
    """What actually happened to a defect after the judge ruled on it (v2.37).

    A finding's life used to end at the judge. ``verdict`` is set once, at review
    time, by a judge with no more access to the answer than the reviewer that
    raised it — and then ``/review/stats`` ranked reviewers on it. On PR #64
    three of six judge-confirmed P2s were simply wrong (``install -m 0755 bin/*``
    does glob; ``CLAUDE_CODE_SESSION_ID`` is exported; line 34 *is* the last help
    line), all three conditionals from a reviewer that had declared it could not
    assess the condition. They sit in the board indistinguishable from the real
    ones, so the leaderboard rewards a confident wrong finding — confidence is
    what the judge can see and correctness is not.

    This is the terminal state, set by whoever ACTED on the finding rather than
    by whoever ruled on it. ``refuted`` is the one that pays for the feature, and
    it is also the cheapest to capture: the refutation is already being written,
    in the PR comment and the fix commit's message, in prose where nothing can
    count it.

    **Per DEFECT, not per observation** — one row per (repo, pr, finding_key),
    which is why this is its own table rather than a column on
    :class:`ReviewFinding`. A defect raised in rounds 2, 3 and 4 is three finding
    rows and one thing that happened to it; a column would fan one refutation out
    over however many rounds happened to raise it, and the number of rounds
    correlates with exactly the PRs this measure is about. It also keeps the
    finding rows immutable: what a round said is a fact about that round, and
    later knowledge is a different fact with a different author.

    ``set_by`` is the board identity that recorded it, taken from the token, and
    is proof. ``attested_by`` is **not**: it is free text from the same request
    that carried the outcome, so it records that the caller CLAIMS a named human
    signed off. The board cannot authenticate a person, and an agent that wants
    to write ``attested_by: "rich"`` can. #77 is explicit that an agent must not
    mark its own findings ``refuted`` unattended — a self-grading loop, #40's
    constraint for the same reason — and since this API cannot tell a fixer from a
    reviewer, the claim is stored beside its claimant and published as a claim:
    ``GET /review/stats`` splits the attested counts out and ``/panel`` renders
    "X claims signoff by Y", never a signature.
    """

    __tablename__ = "review_finding_outcomes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    #: Scoped the way ``finding_key`` is scoped — by (repo, pr) — because that is
    #: what makes a key identify a defect at all. Not a foreign key to a run: the
    #: outcome outlives any one round, and pinning it to the run that happened to
    #: raise the defect first would delete the outcome with that run.
    repo: Mapped[str] = mapped_column(Text, nullable=False)
    pr: Mapped[int] = mapped_column(Integer, nullable=False)
    finding_key: Mapped[str] = mapped_column(Text, nullable=False)

    #: One of :data:`app.api.reviews.OUTCOMES` — fixed | narrowed | refuted |
    #: deferred | superseded. Constrained in the database as well as at ingest:
    #: this table feeds a published precision figure, and an unknown value would
    #: silently leave the numerator while still counting as coverage.
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    #: Why — required by the API for ``refuted`` and for ``narrowed``, optional
    #: otherwise. A bare `refuted` flag is a confident assertion with nothing
    #: behind it, which is the failure this whole feature exists to measure,
    #: arriving one level up. A bare `narrowed` fails in the mirror image: the
    #: note is where the general form the fix did NOT take is written, so without
    #: it the row says only "fixed, sort of" and #615's whole distinction is gone.
    note: Mapped[str | None] = mapped_column(Text)
    #: Where a ``deferred`` finding went: an issue ref. #66, #69, #72, #74 and the
    #: backlogs after them park findings in a markdown list with no state at all,
    #: and this is the state.
    deferred_to: Mapped[str | None] = mapped_column(Text)
    #: The ``finding_key`` that replaced this one, for ``superseded``. Kept apart
    #: from ``deferred_to`` rather than sharing one "ref" column: two readings of
    #: one field is how a tool ends up guessing which it was looking at.
    superseded_by: Mapped[str | None] = mapped_column(Text)

    #: The board identity that recorded this — an agent or a human at a terminal.
    set_by: Mapped[str] = mapped_column(Text, nullable=False)
    session: Mapped[str | None] = mapped_column(Text)
    #: Who the recorder SAYS signed it off — a claim, not a signature; see the
    #: class docstring. NULL = unattended, which is a fact reported rather than a
    #: request refused: refusing it would leave the refutation exactly where it is
    #: today, in prose nothing counts.
    attested_by: Mapped[str | None] = mapped_column(Text)

    #: How many times this record has CHANGED since it was first written — an
    #: outcome replaced, or a stored field rewritten under an unchanged outcome.
    #: Both, deliberately: a terminal state that moves is legitimate (a deferred
    #: finding is later fixed) and a silent flip is not, and a refutation whose
    #: NOTE is quietly rewritten improves an after-the-fact precision figure by
    #: exactly the same route. ``prior_outcome`` covers only the first kind — the
    #: answer this row used to give — so a non-zero ``revisions`` with an empty
    #: ``prior_outcome`` is a record that was edited without the answer moving.
    revisions: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    prior_outcome: Mapped[str | None] = mapped_column(Text)

    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # One terminal outcome per defect. A second row for the same key would
        # make "what happened to this?" a question with two answers, and every
        # count over the table a double count.
        #
        # Its B-tree on (repo, pr, finding_key) is also the read path: the stats
        # join matches all three columns and the chain view looks up by (repo,
        # pr), which the leftmost prefix serves. No separate index — the same
        # argument `ReviewRunFile` records for its own unique constraint.
        UniqueConstraint("repo", "pr", "finding_key", name="uq_review_finding_outcome"),
        # The unique constraint above is only as good as the spelling it is on:
        # `Acme/X` and `acme/x` are one repository, so without this they are two
        # rows for one defect and "what happened to this?" has two answers again
        # — the thing the constraint was written to stop. Same rule and same
        # reasoning as `ck_review_runs_repo_canonical` (#326, migration 0033).
        CheckConstraint(r"repo = lower(btrim(repo, E' \t\n\r\f\013'))",
                        name="ck_review_finding_outcomes_repo_canonical"),
        # `narrowed` is #615's fifth member and it is a FIX, not a refusal: the
        # finding is real, the fix answers it at the point it was raised, and the
        # general form is not that pass's work. It is here rather than expressed
        # as a `fixed` with a note because "I fixed this" and "I fixed the
        # instance of this" are different facts, and the round-stop machinery and
        # the leaderboard read them apart.
        CheckConstraint(
            "outcome IN ('fixed', 'narrowed', 'refuted', 'deferred', 'superseded')",
            name="ck_review_finding_outcomes_vocabulary",
        ),
        # The evidence rule, at the boundary rather than only in the API. A bare
        # `refuted` is a confident contradiction of the judge with nothing behind
        # it, and it lands in a published precision figure — so an admin script,
        # a backfill or a future write path must not be able to insert one either.
        # NOT NULL rather than non-empty: the API already collapses whitespace to
        # NULL, so the two agree, and a CHECK that has to reason about trimming
        # would be a second opinion about what counts as a note.
        # The three "the value must actually say something" rules, at the
        # boundary rather than only in the API — for a backfill, an admin script,
        # or the next write path. Four things each of them gets right:
        #
        # * the NOT NULL is not redundant beside the trim test. **A CHECK passes
        #   when its expression evaluates to NULL**, so the trim alone would let a
        #   null straight through — the exact row the rule exists to refuse.
        # * `btrim` with an explicit character set, because single-argument
        #   `btrim` strips ORDINARY SPACES ONLY: a note of one tab satisfied it.
        # * vertical tab is spelled `\013` and NOT `\v`. Postgres' escape strings
        #   do not define `\v`, and an undefined escape drops the backslash and
        #   keeps the character — so `E'\v'` is the LETTER v (ascii 118, measured,
        #   not read off a doc page). The set would have trimmed v's off both ends
        #   and refused a note of "v" as empty: a rule about whitespace quietly
        #   deciding a letter of the alphabet does not count as evidence.
        # * they mirror the API's three required-field rules exactly, so a row
        #   this service would refuse cannot arrive by another door.
        CheckConstraint(
            r"outcome <> 'refuted' OR (note IS NOT NULL "
            r"AND btrim(note, E' \t\n\r\f\013') <> '')",
            name="ck_review_finding_outcomes_refuted_note",
        ),
        # The same rule again for `narrowed` (#615), as its own constraint rather
        # than by widening the one above: the two are required for different
        # reasons and a caller is owed the one that applies to it, and a CHECK
        # named `..._refuted_note` that also refuses a narrowed row is a name that
        # lies to whoever reads the error. What the note carries here is the
        # general form — what fixing the class would have taken — which is the
        # only thing distinguishing this row from a `fixed` one.
        CheckConstraint(
            r"outcome <> 'narrowed' OR (note IS NOT NULL "
            r"AND btrim(note, E' \t\n\r\f\013') <> '')",
            name="ck_review_finding_outcomes_narrowed_note",
        ),
        CheckConstraint(
            "outcome <> 'superseded' OR (superseded_by IS NOT NULL "
            r"AND btrim(superseded_by, E' \t\n\r\f\013') <> '')",
            name="ck_review_finding_outcomes_superseded_by",
        ),
        CheckConstraint("revisions >= 0", name="ck_review_finding_outcomes_revisions"),
    )


class ReviewRunFile(Base):
    """One path the reviewed PR touched, with that path's share of the churn.

    The board could already say a merge changed 2,032 lines and never which
    files, so it could not answer the only question integration cost turns on:
    *which other open PRs does this one disturb?* The paths findings happen to
    name are a proxy for the diff and not the diff — nine files for a run whose
    PR touched far more, and only the nine somebody complained about.

    Stored per RUN rather than per PR, like :class:`ReviewFinding`: a PR's file
    set grows while it is open, and a row that is overwritten cannot say what the
    round that ran on Tuesday was actually looking at. "The PR's files now" is
    then the newest run's rows, which is a query.

    It is the PR's file list, not the round's. Under a review-the-increment
    round the two diverge, and the collision surface is the PR's — narrowing this
    to the increment would say two PRs no longer collide because one of them
    stopped re-reading the file it still changes.

    Paths, not hunk ranges: paths answer "will these two collide", ranges answer
    "and where", and nothing asks the second yet. ``additions``/``deletions`` ride
    along because the same ``gh pr view`` call already returns them, and they make
    ``ReviewRun.changed_lines`` attributable to a file instead of a bare total.
    """

    __tablename__ = "review_run_files"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    additions: Mapped[int | None] = mapped_column(Integer)
    deletions: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        # One row per path per run. A payload that repeats a path is a bug in the
        # sender, and letting it through would double that file's weight in every
        # collision count built on this table.
        #
        # Its underlying B-tree on (run_id, path) also serves every run_id-only
        # lookup through its leftmost prefix, so there is deliberately NO separate
        # index on run_id: it would be storage and write cost, on the largest
        # table this feature creates, for a lookup already covered.
        UniqueConstraint("run_id", "path", name="uq_review_run_file_run_path"),
        # The collision index. (path, run_id), not (path): the query this table
        # exists for reads by PATH and wants run_id back, which the composite
        # answers from the index alone rather than a heap fetch per matching row.
        Index("ix_review_run_files_path", "path", "run_id"),
    )
