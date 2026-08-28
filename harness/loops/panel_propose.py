"""#507's constructive pass: on an escalation, ask each seat what it would DO.

Every seat returns FINDINGS — a defect, a severity, a location — and for an
ordinary round that is the right contract. A reviewer that proposed a patch for
every nit would be a second author, and the leaderboard measures whether a
reviewer is RIGHT, not whether it is helpful.

On a cycle that will not converge the fixer is doing something else entirely. It
is inferring the reviewer's INTENT from a criticism and then guessing at a change
that satisfies it, and that guess is what the next round reads. #489's numbers are
what the guessing costs: 128 of 201 new findings across seven PRs were created by
the fix pass immediately before them. Nothing anywhere asks a seat the obvious
question — *what would you do instead?*

**The machinery is `--ask`'s and the question is not.** `panel_ask` fans a PREMISE
out to the same seats and tallies `holds`/`fails`/`unresolved`/`unchallenged`; it
adjudicates a claim somebody already wrote. Here nobody has written one, because
the whole problem is that the fixer does not know what the claim should be. So the
fan-out, the sandbox, the retry and the attribution are shared, and the question,
the reply shape and — crucially — the ABSENCE of a tally are not. See
:func:`propose` for why there is no verdict struck over the answers.

**Three properties, and they are the design rather than a checklist:**

1. **A proposal is NOT a finding.** It never enters the leaderboard, the
   cross-round defect chain, the severity floors or `round_stop`. #79's
   answer-versus-panel distinction is the precedent, and here it is structural:
   this module produces no :class:`panel_rounds.Canonical`, is computed AFTER the
   verdict is final, and rides in the payload under a key the board's ingest
   (`extra="ignore"`) drops. A reviewer that proposes is not thereby right.
2. **Disagreement is the signal, not the noise.** Four seats proposing four
   incompatible changes is the most useful possible answer on a stuck cycle: it
   says the finding set has no small resolution, which is the thing nobody
   currently learns until round five. So nothing here reconciles them.
3. **It cannot make a review look cleaner than it is.** It is pure ADDITION to an
   escalation that has already been decided — the same property `fix_injection`
   and #505's rung each claim, and easier to hold here, because this runs after
   `stop`, `reason`, `veto` and `confident` are all settled and writes to none of
   them.
"""

from __future__ import annotations

from panel_core import *            # noqa: F401,F403
import panel_core                   # noqa: F401
from panel_seats import *           # noqa: F401,F403
import panel_seats                  # noqa: F401
from panel_rounds import *          # noqa: F401,F403
import panel_rounds                 # noqa: F401

# Named directly, and BELOW the star imports for `panel_rounds`' reason: a star
# import brings only what the exporting module lists, and a name that arrives by
# accident today is one a tidy-up upstream removes tomorrow with no error here
# until the branch that uses it runs.
from collections.abc import Iterable   # noqa: E402

# ------------------------------------------------------------------- when it fires

#: The `review_panel.escalate_on` rungs a constructive pass follows, in the order
#: `round_stop` applies them.
#:
#: **Every built rung, not the three the issue names, and the argument for each one
#: past those three is the argument for those three.** #507 lists `fix_injection`,
#: `premise_repeated` and #505's `new_findings_not_falling`; `premise_undecidable`
#: (#491) is the same kind of event — an `escalate_on` rung, ending the cycle, not
#: as convergence, with a human at the veto line — and it is the rung where the
#: fixer has most obviously been guessing, since every fix for an unobservable
#: property is an approximation of it. A rule that covered "some escalations" would
#: also be one a reader has to memorise the membership of.
#:
#: #554's `unrefereed_fix` joins on that rule and has a claim of its own on top of
#: it. It fires precisely when the last fix pass wrote nothing anything could check —
#: all test and prose, no production line — which is the strongest evidence this
#: harness has that the fixer never found the change the findings were asking for.
#: Asking each seat what its smallest satisfying change would be is the one question
#: that answers it, and it is asked of the reviewers rather than of the fixer, which
#: is #297's discipline: the actor whose judgement is in question is not the one
#: polled about it.
#:
#: **What is NOT here, and why.** The round CAP is not an escalation: it is a cost
#: bound, it ends healthy cycles and diverging ones in the same place, and a
#: fan-out on every capped round is a fan-out on most rounds this harness runs —
#: which is the "every round" this feature exists to avoid. A HELD ESCALATION
#: (#221, `escalated_outstanding`) is not here either, and for the opposite reason:
#: the fixer that escalated has already been required to write down "the patch you
#: did not write" (`review-pr.md` step 3a), so a proposal for it is in hand and
#: asking again would buy a second copy. The growth ceiling (#165's
#: `max_fix_growth`) is a stop about the SIZE of the change and not about the
#: findings, and a seat's smallest change is not an answer to it.
PROPOSE_ESCALATIONS = ("new_findings_not_falling", "unrefereed_fix",
                       "fix_injection", "premise_repeated", "premise_undecidable")


def escalations_fired(stop: dict | None) -> list[str]:
    """Which `escalate_on` rungs ended this cycle, read off `round_stop`'s verdict.

    DERIVED, never stored: `round_stop` already publishes each rung's own answer,
    and a second list beside them is a second thing that can disagree with the
    first — the objection this file's neighbours raise against every duplicated
    count. It lives here rather than in `panel_rounds` for a reason of the day
    rather than of the design: #506 is rewriting `round_stop`'s body in the same
    hour, and the one edit this feature would have made there is worth nothing
    against a blind merge.

    `fired` and not `over` for the two measured rungs. `over` is a property of the
    MEASUREMENT and is true of plenty of rounds those rules deliberately do not
    touch — a below-floor policy stop, a round holding an escalation, a round going
    again under rule 2 for a P1. `fired` is the property of the VERDICT: this rung
    is why the cycle stopped. Asking the seats for a way out of a cycle that
    converged is the same misreport `round_stop` keeps those two keys apart to
    prevent.

    The premise rungs are read with their ARMING, exactly as `round_stop` reads
    them. `premises.repeated` is populated only where the limit was reached and
    always forces the stop, so it needs no flag; `premises.undecidable` is listed
    whether or not the repo armed the brake — the payload records what the cycle
    DECLARED — so `undecidable_brake` has to be checked here rather than inferred
    from the list being non-empty, or a repo that switched #491 off would be billed
    for a fan-out over a policy it declined.
    """
    if not isinstance(stop, dict):
        return []
    fired = []
    if (stop.get("new_findings_not_falling") or {}).get("fired"):
        fired.append("new_findings_not_falling")
    # #554, read on `fired` for the two measured rungs' reason: `over` is a property
    # of the MEASUREMENT and is true of rounds this rule deliberately does not touch.
    # Its `armed` flag needs no separate check here — unlike `premise_undecidable`'s,
    # it is already a conjunct of `over`, so a repo that switched the rung off can
    # never reach `fired` and is never billed for the fan-out.
    if (stop.get("unrefereed_fix") or {}).get("fired"):
        fired.append("unrefereed_fix")
    if (stop.get("fix_injection") or {}).get("fired"):
        fired.append("fix_injection")
    premises = stop.get("premises") or {}
    if premises.get("repeated"):
        fired.append("premise_repeated")
    if premises.get("undecidable") and premises.get("undecidable_brake"):
        fired.append("premise_undecidable")
    return [name for name in PROPOSE_ESCALATIONS if name in fired]


# ----------------------------------------------------------------------- the question

#: What a seat may answer. Three, on :data:`panel_core.ASK_VERDICTS`' rule that the
#: outcomes a consumer treats differently must not be flattened into one another.
#:
#: `no small change` is the one this feature is FOR as much as `change` is. Property
#: 2 says four incompatible proposals are the most useful answer on a stuck cycle;
#: one seat saying outright that its findings have no joint small resolution is the
#: same information arriving cheaper, and a schema that could only express a
#: proposal would have collected a small change from a seat that does not believe
#: in one.
PROPOSE_VERDICTS = ("change", "no small change", "cannot tell")

#: The spellings a model reaches for. Same shape and same reason as
#: `panel_core._ASK_ALIASES`: a seat that answered the question must not be
#: recorded as not having answered it because of an apostrophe.
_PROPOSE_ALIASES = {
    "no small change": "no small change", "no_small_change": "no small change",
    "none": "no small change", "no change": "no small change",
    "no small fix": "no small change", "not a small change": "no small change",
    "cannot tell": "cannot tell", "cant tell": "cannot tell",
    "can not tell": "cannot tell", "unknown": "cannot tell",
    "unclear": "cannot tell", "cannot say": "cannot tell",
    "change": "change", "a change": "change", "smallest change": "change",
}

#: One proposal's own text. Longer than `ASK_REASON_CHARS` (400) because a premise
#: verdict's reason says what DECIDED it while this says what to DO, and "delete the
#: branch and let the caller pass the floor" is a sentence with a shape; short
#: enough that it is still the one line the prompt asks for and not a patch.
PROPOSE_CHARS = 700

#: The most findings one seat is shown. A seat with forty outstanding findings is a
#: seat whose smallest change is "no small change", and asking it over all forty
#: builds a prompt out of a listing nobody will read to the end. The cut is SAID in
#: the payload and in the report, never silent — a proposal made over half a seat's
#: findings is a different claim from one made over all of them, and a reader has to
#: be able to tell.
PROPOSE_MAX_FINDINGS = 20

#: One finding's share of the listing. `panel_core.LISTING_ACCOUNT_CHARS` is 1,200
#: for the JUDGE, which is reading to rule on each finding; this reader wrote them
#: and needs only to recognise which one is which.
PROPOSE_FINDING_CHARS = 400

# NO BESPOKE TIMEOUT, and it is a decision rather than an omission. This pass runs
# AFTER the round's verdict is final, so a seat that hangs here withholds a report
# that is otherwise complete — which is a real argument for giving it a shorter
# leash than `CLI_TIMEOUT`. It is not a good enough one to thread a second timeout
# through `run_seat`: that function's own budget logic is layered (a lowered
# retry bounded by `FALLBACK_MAX_ELAPSED_S`, and `antigravity_args`' `--print-timeout`
# derived from the same number), so a fourth caller with a private figure is a
# fourth place the seat runner's timing can disagree with itself, on the path where
# every other seat call in this harness agrees. One shape for every seat call is
# worth more here than a number nobody has calibrated.

PROPOSE_PROMPT = """You are being asked what you would DO, not what is wrong. This is NOT a code
review: do not look for defects, do not report anything new, and do not re-argue the findings
below. A finding you make here goes nowhere.

A fix-and-review cycle on this pull request has STOPPED without converging{why}. The findings
below are YOUR OWN, from that cycle, and they are still outstanding. Every round of it, a fixer
had to infer what you wanted from what you objected to and guess at a change that would satisfy
you. This is the one place anyone asks you directly.

THE QUESTION: given these findings of yours, what is the SMALLEST change that resolves them?

Smallest means smallest — not the best change, not the change you would make if the code were
yours. The least you would accept as resolving what you raised.

{no_tools}

Return ONLY a JSON object (no prose):
  {{"verdict": "change|no small change|cannot tell",
    "proposal": "one line",
    "where": "path, or path:line, or \\"\\" if it is not one place",
    "resolves": ["F1", "F2"]}}

- "change" — a small change resolves them. `proposal` IS that change, concrete enough for someone
  else to write it, and `resolves` names the findings below that it clears.
- "no small change" — there is none: they need a different design, or they have no joint
  resolution. `proposal` says in one line what it would take instead. This is a real answer and
  frequently the right one — the cycle stopped for a reason.
- "cannot tell" — you cannot say from what you have. `proposal` says what you would need to see.

`proposal` is ONE line: the change, not an essay. There is no severity, no file list and no
findings array — a reply carrying a findings array is an answer to a question nobody asked, and
is not read as an answer to this one.

Nothing you say here changes your findings. They stand exactly as you made them, this answer
is not scored, and proposing does not make you right. It goes to the person deciding what to do
about this pull request, who currently has a list of complaints and no proposal.

--- YOUR OUTSTANDING FINDINGS ---
{findings}"""


class Proposal(NamedTuple):
    """One seat's answer, read out of its reply."""

    verdict: str
    proposal: str
    where: str
    #: The finding LABELS the seat says its change clears — `F1`, `F2`, as listed
    #: to it. Resolved to real finding keys by :func:`propose`, which is the only
    #: place that knows what was listed and in what order.
    resolves: list[str]


def _propose_verdict(val: object) -> str | None:
    """`val` as one of :data:`PROPOSE_VERDICTS`, or None when it is not one.

    None includes the schema's own `"change|no small change|cannot tell"` handed
    straight back, which is `panel_core._ask_verdict`'s whole echo defence and
    works here for the same reason: the illustration is spelled as the union of the
    legal values and so is not one of them."""
    if not isinstance(val, str):
        return None
    said = " ".join(val.strip().lower().replace("_", " ").split())
    if said in PROPOSE_VERDICTS:
        return said
    return _PROPOSE_ALIASES.get(said)


def parse_proposal(raw: str | None) -> Proposal | None:
    """Read a seat's reply, or None when it cannot be read.

    None means UNREADABLE and never "the seat had no proposal", on
    :func:`panel_core.parse_answer`'s rule and for its reason: a seat whose reply
    could not be parsed must not be recorded as one that looked and could not tell.
    `cannot tell` is a seat's own answer; an unreadable reply is a seat's answer we
    do not have, and only the second is worth quoting back at whoever tunes this
    prompt.

    Candidates are settled by AGREEMENT and never by rank, and two DIFFERENT legal
    verdicts in one reply are not an answer at all — the same refusal, through the
    same `_one_verdict` hook, that stops `{"verdict":"holds","verdict":"fails"}`
    being recorded as whichever one `json.loads` happened to keep."""
    if not raw:
        return None
    seen: list[Proposal] = []
    for _, text in _spans(raw, "{", "}"):
        try:
            val = json.loads(text, object_pairs_hook=_one_verdict)
        except ValueError:
            continue
        if not isinstance(val, dict):
            continue
        # A reply that brought a findings array is a review, not an answer to this.
        if any(isinstance(val.get(k), list) for k in _REVIEW_SHAPED):
            continue
        verdict = _propose_verdict(val.get("verdict"))
        if verdict is None:
            continue
        proposal = _cut(_ask_reason(val.get("proposal")), PROPOSE_CHARS)
        # A `change` with no change in it is not an answer (found by a codex second
        # opinion on this PR). The whole content of that verdict IS the proposal:
        # without it the reply says a small change exists and does not say what it
        # is, which is the criticism-without-a-proposal this feature exists to
        # remove, now wearing the feature's own label. `no small change` is the same
        # claim inverted — its `proposal` is what it WOULD take instead — so both
        # need text, and an empty one is treated as UNREADABLE rather than recorded,
        # which is what buys `run_seat`'s one retry.
        #
        # `cannot tell` does not, on `--ask`'s precedent that a bare verdict is still
        # a verdict: "I cannot say from what I have" is complete on its own, and
        # refusing it would push a seat that genuinely cannot tell into inventing a
        # sentence for the retry.
        if verdict != "cannot tell" and not proposal.strip():
            continue
        seen.append(Proposal(
            verdict, proposal,
            _cut(_ask_reason(val.get("where")), 200),
            _str_list(val.get("resolves"))))
    if not seen:
        return None
    if len({p.verdict for p in seen} ) > 1:
        return None
    # The last of the agreeing candidates: a model that restates its answer at the
    # end of a reply has restated it, and that wording is the one it settled on.
    return seen[-1]


@dataclass
class SeatProposal:
    """What one seat did with the question. :class:`panel_core.SeatAnswer`'s three
    outcomes, kept apart for its reason — it answered, it never ran, or it ran and
    its reply could not be read."""

    verdict: str | None = None
    proposal: str = ""
    where: str = ""
    #: The finding keys this seat says its change clears — real keys, mapped back
    #: from the labels it was shown. A label naming nothing it was shown is dropped
    #: into `unmatched` rather than silently discarded.
    resolves: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    skip: str | None = None
    unreadable: bool = False
    #: The head of a reply that carried no verdict — WHAT the seat said, never why.
    gist: str = ""
    duration_ms: int = 0
    usage: dict | None = None
    absent: bool = False
    model_unavailable: str = ""
    effort_unsupported: str = ""


def propose_llm(cmd_name: str, model: str, prompt: str, effort: str = "") -> SeatProposal:
    """Put the question to one seat and read its proposal back.

    The same seat, the same sandbox, the same budget and the same one retry as a
    review or an ask — see :func:`panel_seats.run_seat`. What differs is only what an
    unreadable reply means, which is :func:`panel_ask.ask_llm`'s answer: a proposal
    is the entire content of the reply, so one that carries none is a seat that did
    not answer, recorded as such and shown in the report rather than folded into
    `cannot tell`.

    **No `code_tree` and therefore no `budget_usd`**, which a codex second opinion on
    this PR asked about and which is the same answer `--ask` gives. This seat gets no
    checkout: the question is about findings the seat already wrote, and there is
    nothing here to read the repository for. `reviewer_code_budget_usd` is
    documented as the CODE-READING seat's per-invocation cap and `claude_args` only
    emits `--max-budget-usd` under `reads_code` — "a diff-only seat makes one call
    with a bounded prompt, so a cap there adds a way to LOSE the seat and buys
    nothing". Passing one here would add exactly that failure mode to a pass that
    cannot afford it: a seat lost to a cap is a proposal missing from an escalation.

    **What this DOES spend is not visible to #55's ceilings, and that is a stated
    limit rather than an oversight** (also codex's). Per-seat usage is recorded — it
    is in the block, in `--json` and in `--json-file` — but the board's review ingest
    is `extra="ignore"`, so these tokens reach no column that `panel_caps` counts,
    exactly as `--ask`'s do today (`record_ask` is the seam and the board half is
    #77's to define). The alternative was worse and was rejected: folding proposal
    tokens into the round's own reviewer rows would charge them to the REVIEW, which
    inflates every cost-per-finding on the leaderboard for a seat that answered a
    question about findings it had already made. The honest posture is one fan-out,
    on an escalating cycle only, measured locally and named here."""
    turn = run_seat(cmd_name, model, prompt, effort, parse=parse_proposal)
    # The label this seat EARNED — a fallback (#215) means something other than the
    # pin answered, and saying otherwise is a false record whichever report it is.
    label = seat_label(cmd_name, model, effort, turn)
    fell_back = {"model_unavailable": turn.model_unavailable,
                 "effort_unsupported": turn.effort_unsupported}
    if turn.skip:
        return SeatProposal(skip=turn.skip, duration_ms=turn.duration_ms,
                            usage=turn.usage, absent=turn.absent, **fell_back)
    # Narrowed rather than trusted, on `ask_llm`'s reasoning: `parse_proposal` is
    # the only parser this call passes, so anything else is a bug — and a bug that
    # surfaces as an unreadable reply is one this function already reports, where an
    # AttributeError would take the whole pass down with it.
    if isinstance(turn.parsed, Proposal):
        return SeatProposal(turn.parsed.verdict, turn.parsed.proposal,
                            turn.parsed.where, list(turn.parsed.resolves),
                            duration_ms=turn.duration_ms, usage=turn.usage,
                            **fell_back)
    if not (turn.reply or "").strip():
        return SeatProposal(skip=f"{label}: produced no output",
                            duration_ms=turn.duration_ms, usage=turn.usage, **fell_back)
    return SeatProposal(unreadable=True, gist=_ask_gist(turn.reply or ""),
                        duration_ms=turn.duration_ms, usage=turn.usage, **fell_back)


# ------------------------------------------------------------------------ the fan-out

def seat_findings(outstanding: list, held: Iterable[str] = ()) -> dict[str, list]:
    """Each seat's own still-outstanding findings, by seat.

    Read off `Canonical.reported_by` through `.reviewers`, which is #79's rule that
    attribution is a FIELD and never an inference from a merge that threw the
    evidence away. A defect three seats raised is in all three lists: each of them
    made that finding, and each is being asked what it would do about its own.

    **ESCALATED findings are included, and marked.** They are outstanding, and the
    human at the veto line is precisely who needs a proposal on them — an escalated
    finding is one the fixer has already said the approach is wrong on, which is
    where "what would you do instead" has most to say. What it must NOT become is a
    licence for a fix pass to patch one, so the listing marks them and the brief
    says whose the answer is. `round_stop`'s subtraction is untouched: nothing here
    reaches it.

    **SONAR is not a seat.** `sonarqube` scans code against a rule set and has no
    reply to give — `panel_ask` says so about the identical case — so its gate
    issues appear in no seat's list. They also do not go unrepresented, because a
    gate issue keeps the PR unmergeable whatever anyone proposes.
    """
    marked = frozenset(k for k in held if k)
    by_seat: dict[str, list] = {}
    for c in outstanding:
        if getattr(c, "verdict", "") == "dismissed":
            # Belt and braces: `outstanding` is built as `to_fix + sonar` and holds
            # no dismissal, and a dismissed finding must never reach a seat as
            # something to propose against — the judge already ruled it not real.
            continue
        for name in c.reviewers:
            if name in LLM_REVIEWERS:
                by_seat.setdefault(name, []).append(c)
    for name in by_seat:
        # Severity first, then the escalated ones last within it: a seat reading its
        # own list meets the work a fix round could still do before the work no fix
        # round may.
        by_seat[name].sort(key=lambda c: (c.severity, c.key in marked, c.file,
                                          c.line or 0))
    return by_seat


def _own_report(c, seat: str):
    """The account `seat` itself wrote for this defect, or None.

    Arrival order, `Canonical.reviewers`' rule and for its reason: a seat may
    report one defect twice and the first is the one it made."""
    return next((f for f in c.reported_by if f.reviewer == seat), None)


def _finding_listing(findings: list, held: frozenset[str],
                     seat: str) -> tuple[str, list[dict], int]:
    """One seat's findings as the prompt shows them, plus the map back and how many
    were cut.

    **A seat is shown what IT wrote, not the judge's merge of it** (found by a codex
    second opinion on this PR, and it is the difference between the question working
    and not). `Canonical.synthesis` is the judge's merged statement over every
    reporter of one defect, so a finding three seats raised carries one sentence
    none of them wrote — and the whole premise of this pass is "given these findings
    of YOURS". A seat asked to propose against a rewording it does not recognise is
    being asked about somebody else's finding. `reported_by` is where its own title
    and detail are, as fields rather than welded into the merge, which is exactly
    what #79 kept them apart for.

    The merged synthesis is shown BESIDE it where the two differ, labelled, because
    it is what the PR comment and the next round call this defect — a seat proposing
    against a wording the report does not use would be proposing about a finding
    nobody can find. It is dropped where they are the same, which is the ordinary
    unmerged case, rather than printing one sentence twice.

    The labels are ORDINALS (`F1`, `F2`) rather than finding keys, and that is the
    whole reason the map exists. A key is a 16-hex-digit digest: a model echoing one
    back is one transposed character away from naming a finding that does not exist,
    and there would be no way to tell that from a real answer. An ordinal it can
    copy. The map is in the payload beside the answers, so nothing has to be
    reconstructed by whoever reads them.
    """
    shown, cut = findings[:PROPOSE_MAX_FINDINGS], max(0, len(findings) - PROPOSE_MAX_FINDINGS)
    lines, mapped = [], []
    for n, c in enumerate(shown, 1):
        label = f"F{n}"
        mine = _own_report(c, seat)
        # The seat's own location and its own words. Falling back to the canonical
        # only where its account cannot be found at all, which `seat_findings`
        # makes impossible — kept because a listing that raised over a missing
        # account would take down a pass whose whole job is to be additional.
        title = (mine.title if mine else c.synthesis) or c.synthesis
        detail = (mine.detail if mine else "") or ""
        file = (mine.file if mine else c.file) or c.file
        line = mine.line if mine else c.line
        where = f"{file}:{line}" if line else file
        escalated = c.key in held
        body = [f"[{label}] {c.severity} {where} — {_cut(title, PROPOSE_FINDING_CHARS)}"]
        if detail.strip():
            body.append(f"       {_cut(detail.strip(), PROPOSE_FINDING_CHARS)}")
        merged = (c.synthesis or "").strip()
        if merged and merged != (title or "").strip():
            body.append("       (the panel merged this with other reports and calls it: "
                        f"{_cut(merged, PROPOSE_FINDING_CHARS)})")
        if escalated:
            body.append("       (ESCALATED — no fix round may touch this one. Answer it "
                        "for the human who has to, not for a fixer.)")
        lines.append("\n".join(body))
        mapped.append({"label": label, "key": c.key, "id": c.id, "severity": c.severity,
                       "file": file, "line": line, "title": title,
                       "synthesis": c.synthesis, "escalated": escalated})
    return "\n".join(lines), mapped, cut


def _propose_defaults() -> dict:
    """Every key the block carries, valued as "this pass never got that far".

    Spread into BOTH the not-asked and the asked payloads, on
    `panel_ask._ask_payload_defaults`' rule: the exit a consumer is least likely to
    have tested against is the one that ran nothing, and it was the one written
    short."""
    return {
        "asked": False,
        "reason": None,
        "escalations": [],
        "seats": {},
        "counts": {},
        "config_notes": [],
    }


def propose(stop: dict | None, outstanding: list,
            selected: Iterable[str], models: dict, efforts: dict,
            held: Iterable[str] = (), armed: bool = True,
            cycle_run: bool = True) -> dict:
    """The constructive pass, as a payload block.

    One seat can never take it down, and it can never take the ROUND down: every
    seat call is caught, and what remains is dict and string work over data the
    round has already built. That bound is deliberate and it is the narrow one — a
    bug in this file should be loud in a suite, and only the expensive, flaky part
    is swallowed.

    Returns a block that ALWAYS says what happened — `asked` and a `reason` when it
    is false — because "we did not ask" and "we asked and nobody answered" are
    different claims and a consumer forced to tell them apart from an empty `seats`
    would be reading the payload's age rather than the round's state.

    **There is no tally, and that is the feature.** `panel_ask` strikes one because
    a premise has a truth value and the panel's job is to settle it. A proposal has
    none. Property 2 of #507 says four seats proposing four incompatible changes is
    the most useful possible answer on a stuck cycle — it says the finding set has
    no small resolution — so a verdict struck over them would destroy exactly the
    information the pass exists to collect, and destroy it by averaging. Nor is
    agreement COMPUTED: deciding whether two proposals are the same change is the
    similarity heuristic #84 rules out for premises, one level down, and a wrong
    "the seats agree" over an escalation is worse than no sentence at all. They are
    printed side by side, attributed, and the reader does the comparing.

    **No asker guard.** `panel_ask` refuses to let the agent that wrote a premise be
    the only voter, because a tally of one confirms nothing while carrying a panel's
    authority. Nothing is confirmed here: no verdict is struck, and every proposal
    is attributed to the seat that made it. There is no self-confirmation for a
    guard to prevent.

    `armed` is the repo's dial, resolved by the caller. `cycle_run` is false for a
    review-only run, which has no cycle to have escalated and so no escalation for
    this to attach to.
    """
    block = _propose_defaults()
    if not armed:
        block["reason"] = ("`review_panel.propose_on_escalation` is off — the seats "
                           "were not asked what they would do instead")
        return block
    if not cycle_run:
        block["reason"] = ("no cycle ran, so nothing escalated — a constructive pass "
                           "attaches to an escalation and there is none")
        return block
    fired = escalations_fired(stop)
    block["escalations"] = fired
    if not fired:
        # The ordinary case, and the one that has to be cheap: a healthy round buys
        # nothing from this and would pay a whole fan-out for it.
        block["reason"] = ("no `escalate_on` rung fired on this round — a constructive "
                           "pass runs on an escalation, not on every round")
        return block

    notes: list[str] = []
    seats_with = seat_findings(outstanding, held)
    marked = frozenset(k for k in held if k)
    seats = [n for n in LLM_REVIEWERS if n in set(selected) and seats_with.get(n)]
    if not seats:
        block["reason"] = ("this round escalated on "
                           f"{', '.join(fired)}, and no seat on it has an outstanding "
                           "finding to propose against")
        return block

    prompts, listings = {}, {}
    # Named in the brief, because "the cycle stopped" and "the cycle stopped
    # because the new-finding count stopped falling" send a reader — and a seat —
    # to different places. `fired` is non-empty by the guard above.
    why = " — `escalate_on." + "`, `escalate_on.".join(fired) + "` fired"
    for name in seats:
        listing, mapped, cut = _finding_listing(seats_with[name], marked, name)
        listings[name] = (mapped, cut)
        if cut:
            # Said, never silent: a proposal made over 20 of a seat's 31 findings is
            # a different claim from one made over all 31.
            notes.append(f"{name} was shown {len(mapped)} of {len(mapped) + cut} "
                         f"outstanding finding(s) — its proposal is about those, and "
                         f"{cut} more are outstanding for it")
        prompts[name] = PROPOSE_PROMPT.format(why=why, no_tools=NO_TOOLS_RULE,
                                              findings=listing)

    answers: dict[str, SeatProposal] = {}
    # `agy`'s prompt travels in argv and the kernel caps one element. Unlike an
    # ask there is nothing here to CUT — the listing is already bounded by
    # `PROPOSE_MAX_FINDINGS` and every line of it is one of this seat's own
    # findings — so a prompt over the ceiling is a stated skip rather than a
    # truncation that would ask the seat about a different set than the one the
    # payload records. A stated skip is the panel's idiom for a seat that could
    # not be run, and it keeps the absence in the report instead of in an opaque
    # execve failure.
    if "antigravity" in prompts:
        over = len(prompts["antigravity"].encode()) - ARGV_PROMPT_MAX_BYTES
        if over > 0:
            label = reviewer_label("antigravity", models.get("antigravity", ""),
                                   efforts.get("antigravity", ""))
            answers["antigravity"] = SeatProposal(skip=(
                f"{label}: its prompt is {over:,} bytes over the "
                f"{ARGV_PROMPT_MAX_BYTES:,}-byte argv ceiling — `agy` takes a prompt "
                "only as one argv element, and this seat's finding listing does not "
                "fit in one"))

    to_run = [n for n in seats if n not in answers]
    if to_run:
        with ThreadPoolExecutor(max_workers=len(to_run)) as ex:
            tasks = {n: ex.submit(propose_llm, n, models.get(n, ""), prompts[n],
                                  efforts.get(n, "")) for n in to_run}
            for n, fut in tasks.items():
                try:
                    answers[n] = fut.result()
                except Exception as e:  # noqa: BLE001 — one seat never takes the pass down
                    # `run_seat` does filesystem work, and ENOSPC or a permission
                    # error on any of it raises outside the err-string path. Re-raised
                    # here it would take a whole round's REPORT down with it, over a
                    # block that is additional information about a verdict already
                    # taken. That trade is not close.
                    answers[n] = SeatProposal(skip=f"{n}: raised {e.__class__.__name__} — {e}")

    block["asked"] = True
    block["reason"] = None
    for name, a in answers.items():
        mapped, cut = listings[name]
        by_label = {m["label"]: m["key"] for m in mapped}
        # Labels the seat named that were never shown to it are RECORDED, not
        # dropped. A model inventing an `F9` over a six-finding listing is a fact
        # about how well this prompt is being followed, and the only place it can
        # be seen is here.
        named = list(a.resolves)
        a.resolves = [by_label[label] for label in named if label in by_label]
        a.unmatched = [label for label in named if label not in by_label]
    # The counts DESCRIBE the fan-out; they do not adjudicate it. `no small change`
    # beside `change` is the split a reader most wants and cannot get from prose,
    # and it is still not a verdict: two seats saying `change` have not agreed on a
    # change, and this file never claims they have.
    counts = {v: 0 for v in PROPOSE_VERDICTS}
    for a in answers.values():
        if a.verdict:
            counts[a.verdict] += 1
    block["counts"] = counts
    block["config_notes"] = notes
    block["seats"] = {
        n: {**(a.usage or {}),
            "verdict": a.verdict, "proposal": a.proposal or "", "where": a.where or "",
            "resolves": a.resolves, "unmatched": a.unmatched,
            "findings": listings[n][0], "findings_cut": listings[n][1],
            "skip": a.skip, "unreadable": a.unreadable, "absent": a.absent,
            "gist": a.gist or None,
            "model": models.get(n) or None, "effort": efforts.get(n) or None,
            "model_unavailable": a.model_unavailable or None,
            "effort_unsupported": a.effort_unsupported or None,
            "duration_ms": a.duration_ms}
        for n, a in sorted(answers.items())}
    return block


# ------------------------------------------------------------------------- the report

def propose_lines(block: dict) -> list[str]:
    """The section that goes in front of whoever the escalation goes to.

    Empty for a pass that did not run: the escalation's own veto lines are the
    output there, and a heading saying nothing was asked would be a paragraph about
    the absence of a paragraph. The `reason` is in the payload for anyone auditing
    why.

    It is printed UNDER the stop's veto lines rather than over them, which is the
    ordering property 3 requires in the report as well as in the code: a reader
    meets what ended the cycle, and only then what the seats would do about it. A
    proposal above the veto reads as a plan, and a plan at the top of an escalation
    is exactly the "cleaner than it is" this must not be able to produce."""
    if not block.get("asked"):
        return []
    seats = block.get("seats") or {}
    fired = ", ".join(f"`{name}`" for name in (block.get("escalations") or []))
    out = [f"\n### What the seats would do instead ({len(seats)} asked)",
           f"_This cycle escalated on {fired}. Each seat below was asked one question about "
           "its OWN outstanding findings: what is the smallest change that resolves them? "
           "**A proposal is not a finding** — it is scored by nothing, it changes no verdict "
           "on this PR, and a seat that proposes is not thereby right. Where they disagree, "
           "that IS the answer: it says the finding set has no small resolution._"]
    for name, a in sorted(seats.items()):
        n_shown = len(a.get("findings") or [])
        cut = a.get("findings_cut") or 0
        over = f"{n_shown} of {n_shown + cut} finding(s)" if cut else f"{n_shown} finding(s)"
        if a.get("verdict"):
            where = f" — `{a['where']}`" if a.get("where") else ""
            out.append(f"- **{name}** ({over}) — _{a['verdict']}_{where}: "
                       f"{a.get('proposal') or 'it did not say what it would need'}")
            if a.get("resolves"):
                out.append(f"    - resolves {len(a['resolves'])} of them: "
                           + ", ".join(f"`{k[:12]}`" for k in a["resolves"]))
            if a.get("unmatched"):
                out.append("    - ⚠️ it also named "
                           + ", ".join(f"`{x}`" for x in a["unmatched"])
                           + ", which is not a finding it was shown")
        elif a.get("unreadable"):
            out.append(f"- **{name}** ({over}) — ⚠️ no proposal: its reply could not be read "
                       "as one" + (f" (it said: {a['gist']})" if a.get("gist") else ""))
        else:
            out.append(f"- **{name}** ({over}) — ⚠️ did not answer — {a.get('skip')}")
    for note in block.get("config_notes") or []:
        out.append(f"  - ⚠️ {note}")
    return out


#: Everything this module offers, INCLUDING the underscore names — the suites reach
#: for several of them through `panel`, and a plain star import would drop them
#: silently.
__all__ = [
    "panel_core", "panel_seats", "panel_rounds",
    "PROPOSE_ESCALATIONS", "escalations_fired",
    "PROPOSE_VERDICTS", "_PROPOSE_ALIASES", "PROPOSE_CHARS",
    "PROPOSE_MAX_FINDINGS", "PROPOSE_FINDING_CHARS",
    "PROPOSE_PROMPT", "Proposal", "_propose_verdict", "parse_proposal",
    "SeatProposal", "propose_llm", "seat_findings", "_own_report",
    "_finding_listing",
    "_propose_defaults", "propose", "propose_lines",
]
