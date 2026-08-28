#!/usr/bin/env python3
"""Reviewer panel — multi-reviewer PR review with consensus synthesis.

Runs the enabled reviewers for a repo (per its .harness-rules) in parallel over a
PR's diff, then synthesises:
  - SonarQube  -> HARD gate (quality-gate pass/fail) + issues
  - Claude     -> SOFT findings (read-only; diff in, JSON out)
  - Codex      -> SOFT findings (different vendor)
  - Antigravity-> SOFT findings (third vendor; off unless a repo enables it)
No consensus gate: every finding is judged on its merits by a "master" reviewer.
A real defect flagged by only ONE reviewer (e.g. Codex) is still fixed — agreement
is a confidence signal, not a filter. Reviewers apply the full /review-pr bar
(correctness, security, tests, docs, related code, craft — P1–P4); only genuine
false positives are dropped, so style and polish findings are kept, not filtered.

The master also MERGES the duplicates, and that is the only place a merge happens.
Deduping upstream of it could only pick one reviewer's text and discard the rest,
so a better key made the loss worse: the observation only one reviewer made
survived precisely when the merge FAILED. The judge instead writes a synthesis and
every reviewer's own title and detail ride along beside it (`reported_by`), so
merging is additive, attribution is a field rather than an inference, and the fix
loop and the board consume one canonical record instead of re-deriving it.

Reviewers whose prerequisites are missing (codex CLI absent, SONAR* env unset)
are reported as SKIPPED, not failed — the panel still produces a report.

LLM replies are parsed leniently: a balanced-bracket scan (not a greedy regex)
pulls the JSON out of ``` fences or surrounding prose, as either the object
envelope reviewers now return or the bare findings array they used to; an
unparseable reply is retried once, then kept as a single markdown finding rather
than dropped — so malformed JSON degrades into one ungrouped finding, never a
crash or a silent loss. A reply holding SEVERAL JSON-shaped values is settled by
agreement, never by rank: the prompts' own examples are identified and dropped,
and what remains must say one thing or the reply is treated as unstructured.

That is deliberately the pessimistic rule. Choosing among candidates — by
position, by size, by which strings they carry — can silently swap a review for
an echo or file a finding no reviewer made, and both artefacts read exactly like
a clean round.

Reviewers also DECLARE their own coverage — what they could not assess, and which
of their findings need the fix re-read — and the panel measures what they cannot
observe (whether the diff they got was truncated). Those are observations, not
forecasts: asking a model "will another round be needed?" asks it to predict its
own future findings, and a reviewer that silently produced nothing would answer
"no" with complete confidence. Rounds are driven mechanically instead — --round
and --baseline say which findings no earlier round raised, and that plus severity
decides whether to go again; the declarations only stop a broken round being read
as a clean one. The cycle belongs to the CALLER (/panel-review-pr): a run given
none of --round/--baseline/--max-rounds is a single review and says nothing about
rounds, rather than promising a re-review nothing will run.

Default prints a report. Pass --post to also comment the summary on the PR, or
--json to emit the whole run as JSON on stdout instead (progress goes to stderr,
so the payload parses without a preamble to strip); --json-file writes that same
JSON *and* keeps the report.

Every run is recorded on the quarterback board (`qb record-review`, best-effort —
a down board never fails a review) so which model finds the real issues, and
whether a pricier tier earns its keep, accumulate into an answer instead of an
impression. --no-record opts out; a machine with no board configured no-ops.

Which reviewers run comes from the repo's .harness-rules; --reviewers overrides
that for one run, which is how you get a single-vendor read (--reviewers codex)
without editing config to get it.

--ask challenges ONE premise instead of reviewing a PR: the same seats, no diff,
no clustering and no judge — each answers holds / fails / cannot tell in one line
and the vote is the output. It exists because a round is the only thing that
currently reviews a fix, and a round is twenty minutes and thirty findings when
what was wanted was one answer to one question. PR #62 spent three of them
answering "did a review actually happen?", each round trusting a fresh proxy for
it (the exit code, then the push, then the payload artefact), every one of them a
yes/no question about one branch of this file. It is deliberately NOT a gate: a
point of order a fixer runs before committing, exiting 0 on every verdict, since
a required minute is a minute that gets skipped.

Usage:
    python3 ~/.claude/loops/panel.py --pr 734
    python3 ~/.claude/loops/panel.py --pr 734 --post
    python3 ~/.claude/loops/panel.py --pr 734 --json
    python3 ~/.claude/loops/panel.py --pr 734 --reviewers codex
    python3 ~/.claude/loops/panel.py --pr 734 --reviewers claude,codex,antigravity,grok
    python3 ~/.claude/loops/panel.py --pr 734 --post --json-file "$rundir/panel.json"
    python3 ~/.claude/loops/panel.py --pr 734 --post --round 2 --max-rounds 2 \
        --baseline "$rundir/r1.json" --json-file "$rundir/r2.json"
    python3 ~/.claude/loops/panel.py \
        --ask "panel.py exits non-zero when it skips a PR on a title pattern" \
        --context harness/loops/panel.py:3500-3560

(`$rundir` being a `mktemp -d` of the caller's: a fixed /tmp path is a symlink
away from writing the payload somewhere else, and world-readable meanwhile.)
"""

from __future__ import annotations

# The imports, tunables, prompts and helpers this file used to carry live in
# panel_core (#129). Star-imported so every name this module already used stays
# a plain global here — which is also what keeps `monkeypatch.setattr(panel, …)`
# working for anything CALLED from this file. A helper called from inside
# panel_core resolves there instead, so tests driving one of those patch
# `panel_core` directly; the module docstring there says so.
from panel_core import *          # noqa: F401,F403
import panel_core                 # noqa: F401
from panel_core import (          # noqa: F401  — explicit, so a reader can grep
    sh, load_repo_cfg)            #   the two with the widest blast radius

# The seats moved to panel_seats (#129) — that section alone was over
# antigravity's argv cap, so the one seat whose prompt travels in argv could
# never read the file reviewing it. Imported back so `run()` below still calls
# `review_llm(...)` as a plain global, which is what keeps the suites' 
# `setattr(panel, "review_llm", …)` doubles working.
from panel_seats import *        # noqa: F401,F403
import panel_seats               # noqa: F401
from panel_scope import *        # noqa: F401,F403  — re-exported for callers
import panel_scope               # noqa: F401

# Synthesis and rounds moved to panel_rounds (#129). Imported back so run()
# below still calls them as plain globals, which is what keeps the suites'
# `setattr(panel, "adjudicate", …)` doubles working.
from panel_rounds import *       # noqa: F401,F403
import panel_rounds              # noqa: F401

# The constructive pass (#507) — on an escalation, one question to the seats that
# still have outstanding findings: what is the smallest change that resolves them?
# Deliberately NOT star-imported, for `panel_caps`' reason: it is the one thing in
# this file that spends a fan-out on a round the verdict has already ended, so
# `panel_propose.` on every call site is what makes "where does the extra spend
# happen" answerable with one grep.
import panel_propose              # noqa: F401

# The pre-flight verdict (#138) — whether a round is worth running at all, and
# whether it should read the diff or a manifest of it. Its own module for the
# reason the four above are: this file is what #129 split because one of its
# sections was over the cap of the seat that has to read it.
from panel_preflight import *     # noqa: F401,F403
import panel_preflight            # noqa: F401

# The caps (#55) — the board's round ceiling and the spend ceiling, and what an
# unverifiable ceiling means. Deliberately NOT star-imported, for `panel_timing`'s
# reason with more force: a cap is policy, `panel_caps.` on every call site is what
# makes "where is the ceiling checked" answerable with one grep, and a bare
# `check(...)` in this file would read like one of the dozen local helpers.
import panel_caps                 # noqa: F401

# Where the round's wall clock went (#192). Its own module, and deliberately NOT
# star-imported: nothing here calls it as a bare global, so an explicit import
# keeps `panel_timing.` on every call site and makes the instrumentation greppable
# as one thing — this file is edited by several changes at once and a timing call
# that reads like a panel helper is the kind of line a merge loses.
import panel_timing               # noqa: F401

# The mergeability sentence, from the merge gate that already owns it (#271). One
# import rather than a second copy: `preland.check_pr_state` refuses a CONFLICTING
# branch at merge time, this refuses a round on the same branch hours earlier, and
# the two saying it differently is how the three checks in #96 came to disagree.
from preland import board_get, mergeability   # noqa: E402
# #274's one door, and #279's escalation list read back through it.
from needs_human import announce, digest as nh_digest   # noqa: E402

#: What `GET /review/findings` calls the escalation list (#279). Named because
#: two things read it: the fetch below, and the note it writes when a board is
#: too old to publish it.
NEEDS_HUMAN_KEYS = "needs_human_keys"


def board_escalations(gh_repo: str, pr_number: int) -> tuple[list[str], str]:
    """`(the keys waiting on a human, why there are none)` for this PR.

    The read half of #274. `--escalated` has always taken keys the CALLER typed,
    which means the escalation list only ever existed in whatever prose the fixer
    wrote — the defect #274 measured as `deferred: 0` across sixty-five rounds.
    #279 publishes the same list as a field, so a fix cycle can subtract it
    without anybody transcribing a hex string.

    An error is REPORTED and never returns an empty list quietly. Absent must not
    read as clean here for the reason it must not in `preland`: "no escalations"
    and "we could not find out" have different remedies, and only one of them is
    a round that may count this work as clearable.
    """
    body, err = board_get("review/findings", {"repo": gh_repo, "pr": pr_number})
    if err:
        return [], (f"--escalated-from-board: {err} — this round could not read "
                    "which findings are waiting on a human, so it may count one of "
                    "them as work a fix round can clear")
    if not isinstance(body, dict):
        return [], ("--escalated-from-board: the board answered /review/findings "
                    f"with a {type(body).__name__}, not an object")
    keys = body.get(NEEDS_HUMAN_KEYS)
    if keys is None:
        # A CAPABILITY answer, not a failure — and still not "none". Reading its
        # silence as an empty escalation list is the same mistake as reading no
        # CI as green.
        #
        # What it does NOT say is WHY, and the message must not pretend
        # otherwise. At least two causes produce the identical absence: a board
        # older than #279, which has no such field at all, and a PR with no
        # recorded review run, where the field is simply not rendered. Both are
        # real — measured on the live board, where a PR with rounds returns
        # `needs_human_keys: []` and a PR with none omits it — and nothing in
        # this response tells them apart. Asserting one of them as fact here
        # would be exactly the inference-from-absence this branch exists to
        # refuse, one level up: the first draft of this line said "it predates
        # the field" and was wrong about a board running current code.
        #
        # A `/version` endpoint (#199, open) would separate them properly, at
        # which point this can name the cause instead of listing them.
        return [], (f"--escalated-from-board: the board published no "
                    f"`{NEEDS_HUMAN_KEYS}` for this PR. Either this board predates "
                    "the field, or no review run carrying it has been recorded on "
                    "this PR — nothing in the answer says which, so escalations "
                    "must be named with --escalated by hand")
    if not isinstance(keys, list):
        return [], (f"--escalated-from-board: `{NEEDS_HUMAN_KEYS}` came back as a "
                    f"{type(keys).__name__}, not a list")
    return [str(k) for k in keys], ""


def announce_escalations(payload: dict, cfg: dict) -> list[str]:
    """Tell the board about every finding this round says a human has to settle.

    The write half of #274 for the panel door. It runs on the round that FORMED
    the judgement rather than on the fix pass that inherits it, because the fix
    pass is exactly where the escalation used to evaporate: `--escalated` takes
    keys a fixer had to transcribe out of its own prose, and thirty days of
    rounds recorded not one.

    Confirmed findings only. A dismissed finding's escalation is a claim the
    judge already refused, and putting it on a person's queue would make "the
    panel disagreed with itself" indistinguishable from "you have to decide
    this". One post per CLASS, for the reason `preland.announce_hold` gives.
    """
    flagged = [f for f in (payload.get("to_fix") or [])
               if isinstance(f, dict) and f.get("needs_human")]
    if not flagged:
        return []
    repo = payload.get("github") or ""
    pr = payload.get("pr")
    head = str(payload.get("head_sha") or "")
    by_class: dict[str, list[dict]] = {}
    for f in flagged:
        by_class.setdefault(str(f.get("needs_human_class") or ""), []).append(f)
    said = []
    for cls, group in by_class.items():
        head_finding = group[0]
        note = announce(
            cls=cls, reason=str(head_finding.get("needs_human_reason") or ""),
            summary=(f"PR #{pr} — {len(group)} confirmed finding(s) no reviewer can "
                     f"settle from the diff"),
            repo=repo, cfg=cfg,
            # The finding keys are IN the dedupe key, not just the commit: a
            # later round on the same head that raises a NEW `decision` finding
            # is a new question, and a key naming only the class would swallow
            # it behind the first one for twelve hours (Codex).
            key=(f"panel:{repo}:{pr}:{cls}:{head}:"
                 + nh_digest(*sorted(str(f.get("key") or "") for f in group))),
            detail="\n".join(
                [f"Panel round on {payload.get('branch') or '?'} at {head[:12]}.", ""]
                + [f"- {str(f.get('key') or '')[:12]} {f.get('synthesis') or ''}\n"
                   f"    {f.get('needs_human_reason') or ''}" for f in group]
                + ["", "Pass these to the next round with --escalated-from-board so a "
                   "fix cycle stops counting them as work it can clear."]),
            refs=[{"kind": "pr", "value": str(pr), "repo": repo}])
        if note:
            said.append(note)
    return said


# ----------------------------------------------------------------------------- run

def _changed_files(meta: dict) -> tuple[list[dict], int | None, int]:
    """The PR's touched paths, with each one's own share of ``changed_lines``.

    Returns ``(files, total, dropped)``. ``total`` is GitHub's own count of the
    PR's changed files, carried separately and NOT derived from ``len(files)``,
    because the two are allowed to disagree: `gh` pages the `files` connection
    and GitHub caps a PR's file list at 3,000. A consumer comparing them learns
    the list is partial; one told only ``len(files)`` reads a truncated list as a
    complete one, which is this repo's standing disease — a shortfall presenting
    as a clean result.

    **``total`` is None when GitHub did not state it, never ``len(files)``.** The
    first version of this fell back to the list's own length and called that
    "falling back to what we can prove" — but agreeing by construction is not
    proof, and when BOTH fields were missing it returned ``([], 0)``, turning an
    unknown file list into a *known empty* PR. That is the same absent-vs-zero
    collapse this release exists to prevent, committed by the code enforcing it.
    None travels to a NULL column that already means "nobody said".

    ``dropped`` counts entries discarded for having no usable path, so the caller
    can tell "GitHub paged us short" from "we discarded a malformed row" — two
    different facts with different fixes, and the partial-list warning is only
    about the first.

    Paths, not hunk ranges. Paths answer "will these two PRs collide", which is
    what #80 orders by; ranges would answer "and exactly where", which nothing
    asks yet. The per-file additions/deletions ride along because the same `gh`
    call already returns them, and they turn ``changed_lines`` from a bare total
    into something attributable to a file — as themselves, so a file GitHub
    stated nothing about stays distinguishable from a pure-deletion file.

    A rename is recorded under its DESTINATION path only: `gh pr view --json
    files` offers `path`, `additions`, `deletions` and `changeType` and no
    previous filename (verified — `previousFilename` is not an available field),
    so another PR still touching the old path is a collision this cannot see.
    The REST `pulls/{n}/files` endpoint does carry `previous_filename`; wiring it
    up is a second call and is deliberately left to whoever needs rename-grain.
    """
    files, dropped = [], 0
    for f in meta.get("files") or []:
        # Shape-checked, not assumed. A bare string, a number or a null in the
        # array raised AttributeError inside `run()` — after the PR read and
        # before any review — killing a run that had not started yet. The board
        # end of this same field coerces exactly these shapes into a droppable
        # row (`ChangedFileIn._coerce`), and the two ends of one field should not
        # disagree about what they tolerate.
        if not isinstance(f, dict) or not isinstance(f.get("path"), str):
            dropped += 1
            continue
        path = f["path"].strip()
        if not path:
            dropped += 1
            continue
        files.append({
            "path": path,
            # `.get` without `or 0`: 0 and "not stated" are different facts, and
            # the board's columns are nullable precisely to keep them apart.
            "additions": f.get("additions"),
            "deletions": f.get("deletions"),
        })
    files.sort(key=lambda f: f["path"])
    total = meta.get("changedFiles")
    if total is not None:
        try:
            total = int(total)
        except (TypeError, ValueError):
            # A shape `gh` has never returned. Degrade like everything else in
            # this neighbourhood rather than killing a review that has not run
            # yet with an uncaught TypeError from inside `run()`.
            total = None
    return files, total, dropped


def _payload_defaults() -> dict:
    """Every key a run payload carries, valued as "this run never got that far".

    One shape on every non-error exit, because the skip-pattern path emits a
    payload too: a consumer reading `payload['judged']` or `payload['run_key']`
    should not have to know which exit produced it. It used to be a hand-written
    literal of nine keys against this one's two dozen, so the skipped PR — the
    case that payload exists FOR — was the one that raised KeyError."""
    return {
        # Where this round's wall clock went (#192). None means the run never got
        # far enough to say — the same distinction every other key here draws, and
        # it matters more than most: a fix phase is measured from the PREVIOUS
        # round's `timing.finished_at`, so a payload that carries a zero rather
        # than a null hands the next round a left-hand end that never happened.
        "timing": None,
        "changed_lines": 0,
        # The PR's file list, not this round's. Under #41 a later round reviews
        # only the increment, so the round's files narrow while the PR's
        # collision surface does not — and collision is what this is for.
        "changed_files": [],
        # None, not 0. This structure exists to describe a run that never got
        # that far, so the one value it must not assert is "this PR changed zero
        # files" — the release's whole distinction is NULL ("nobody said") versus
        # 0 ("counted, and it was none").
        "changed_files_total": None,
        # As of this PR's last panel, not live. Same currency as the file list,
        # and `ts` is what a reader judges staleness by.
        "pr_state": None,
        "is_draft": None,
        "reviewed": False,
        "skip_reason": None,
        # The pre-flight verdict (#138) and the shape it was read off. None means
        # this run never reached the verdict — the title-pattern skip returns
        # above it — which is a different statement from a `run` verdict, and the
        # difference matters to anything asking "was this PR ever weighed?"
        "preflight": None,
        # The commit this round reviewed. NOTHING else in the payload identifies
        # one — `base` holds a branch NAME — and two later readers need it: the
        # next round diffs against it to get the fix commit (an increment is
        # defined by the head its baseline read), and provenance uses it as the
        # far end of the range that says whether that fix pass INTRODUCED a
        # finding or MISSED it. Present on the skip path too — a skipped round
        # still moved the head, and a round 3 whose only baseline is a skipped
        # round 2 must still be able to find its anchor.
        "head_sha": None,
        # The other end of the range, and the two are NOT interchangeable (#98).
        #
        # `merge_base` is the PR's base commit: `gh pr diff` is the three-dot
        # diff, so a whole-PR round reads `merge_base...head` and nothing else in
        # the payload named that commit. It moves only when the PR merges its
        # base in or is rebased.
        #
        # It is the PR's anchor, NOT necessarily this round's target anchor.
        # Under #41's increment scope the target is `since_sha...head_sha` and
        # `merge_base` is where the tier-2 context is measured from instead. Read
        # `scope` before treating this as the left-hand side of what was
        # reviewed — the same warning `diff_chars` carries one field over.
        #
        # `base_sha` is the live tip of the base branch at review time — what the
        # PR would be merged INTO. It is the end that moves on its own, and the
        # only one a staleness check can be built on. Recording just the merge
        # base would produce a check that reports "unmoved" however far the base
        # ran away; see :func:`_base_tip_now` for the measurement behind that.
        #
        # Null on both means the panel did not say. Neither is ever derived from
        # the other.
        "merge_base": None,
        "base_sha": None,
        # What this round could not read in full, for the NEXT round's
        # `missed-unread` bucket. See :func:`_diff_files_cut`. Empty on a payload
        # whose `reviewed` is false means "no coverage at all", not "read
        # everything" — a skipped round never fetched a diff to name files from,
        # and the reader tells the two apart by `reviewed` (see `Baseline`).
        "unread_files": [],
        # Per-round tally of the buckets below, so a consumer gets the shape of a
        # round without walking every finding. Empty where the question does not
        # arise — outside a cycle, or in a cycle's round 1, which has no earlier
        # round to attribute against. All-zero is a different statement: a round
        # that could have attributed and had nothing to attribute.
        "provenance_counts": {},
        "fix_range_source": None,
        "fix_range_rebuilt": None,
        # #490's cross-round rows. Empty on every path that reviewed nothing, and
        # that costs a later round nothing: the block is rebuilt from the raw
        # per-round fields of every baseline, so a skipped round leaves a row that
        # says "not reviewed" rather than a hole.
        "cycle_trend": [],
        # #67's tally, on the same terms and empty where the same question does
        # not arise. Two objects rather than one: `recurrence_counts` is what the
        # panel MEASURED and `premise_counts` is what the judge SAID, and the whole
        # value of asking twice is that they can disagree.
        "recurrence_counts": {},
        "premise_counts": {},
        # #492's guard-to-guarded reading, and null where this round never measured
        # one — a skipped round, or a diff that adds nothing. Not `{}`: the three
        # counts and the ratio are one reading, and an empty mapping would let a
        # consumer index it and get zeros for a change nobody measured, which is the
        # absent-vs-zero collapse the keys above are shaped to prevent.
        "guard_ratio": None,
        # Where a run sits in the panel -> fix -> panel cycle. Defaulted here too,
        # so the skipped PR answers `payload['round_stop']` with "no cycle ran"
        # rather than with a KeyError.
        "round": 1,
        "cycle": None,
        # What this round actually REVIEWED: "pr" (the whole diff) or "increment"
        # (the commits since `since_sha`, with the rest of the PR as context).
        # Recorded rather than inferred from the round number, because scope
        # falls back to "pr" whenever the anchor is missing or the fetch failed —
        # so "round 2" does not imply "increment", and a consumer comparing
        # `diff_chars` across rounds is comparing two different measurements
        # unless it reads this first.
        "scope": "pr",
        "since_sha": None,
        # Chars of PR context prepared ALONGSIDE the target under increment scope.
        # Separate from `diff_chars` (which is the target) because losing context
        # and losing the thing under review are not the same event: context is
        # the part a reviewer can lose and still know it lost it.
        "context_chars": 0,
        "prior_rounds": 0,
        "prior_findings": 0,
        "new_findings": 0,
        "new_finding_keys": [],
        "round_stop": None,
        "stop_reason": None,
        # #507's constructive pass, ALWAYS present and never null — an absent key
        # and "we did not ask" are different claims, and a consumer forced to tell
        # them apart would be reading a payload's age rather than a round's state.
        # The block's own `asked`/`reason` carry which it was, on the skip exits as
        # on the real one, which is why the defaults come from `panel_propose`
        # rather than being spelled a second time here.
        "proposals": panel_propose._propose_defaults(),
        "coverage_note": None,
        "diff_truncated": False,
        "diff_chars": 0,
        # The PR's own size, which a skipped round never measured (#298). 0 rather
        # than null for the same reason `diff_chars` beside it is 0: `reviewed` is
        # what tells a reader this round measured nothing, and the growth ceiling
        # already refuses a baseline whose `reviewed` is false.
        "pr_chars": 0,
        "diff_budgets": {},
        "config_notes": [],
        # Present on every payload, empty by default: a consumer that reads it —
        # the next round, through --baseline — must not have to tell "no
        # escalations" from "a payload that predates the field".
        "escalated": {},
        # #547's two, on the same terms and for the same reason. `acknowledged` is
        # the register the next round inherits; `unresolved_claims` is the ledger a
        # human reads to decide what to acknowledge, and it is the artefact half of
        # the answer — an unverifiable claim that vanished without landing here would
        # be exactly the silence this issue exists to stop producing.
        "acknowledged": {},
        "unresolved_claims": [],
        "sonar_gate": "skipped",
        "ci_status": "unknown",
        "ci_failing": [],
        # Null and not `{}` for `code_access`'s reason: a round that never reached
        # the CI read did not decline to run a local suite, it never asked. #548.
        "local_suite": None,
        "judged": False,
        "judge_model": None,
        "judge_skip": None,
        "reviewers_ran": [],
        "reviewers": {},
        # Nulls rather than `{"setting": true, "seats": []}`, for the reason the
        # file-count key above gives: a run that never got as far as asking cannot
        # claim the setting was on and bought nothing. `seats: []` on a skipped
        # round would read as "code access was available and no seat used it",
        # which is a finding about the panel rather than about a round that never
        # ran one.
        "code_access": {"setting": None, "seats": None,
                        "convention_files_removed": None},
        # #165's dials as this round applied them. Null on the paths that reviewed
        # nothing, for the reason `code_access` is: a round that never dispatched a
        # seat, never briefed a fixer and never computed a stop did not apply a
        # review policy, and recording the resolved values there would read as
        # "these governed a round" about a round that did not happen. A bad VALUE
        # in the rules file is still reported on those paths — it lands in
        # `config_notes`, which the skip payloads carry.
        "review_panel": None,
        # WHICH LAYER supplied each of them (#305) — and unlike `review_panel`
        # above, present on every payload including the ones that reviewed nothing.
        # A round that refused still resolved a policy, and "what rules did this
        # repo have when it refused" is exactly the question a refusal raises. Null
        # only as the shape a caller building a payload by hand would leave.
        "rules": None,
        "reviewers_selected": [],
        "reviewers_override": None,
        "to_fix": [],
        "sonar_findings": [],
        "dismissed": [],
        "skipped": [],
    }


def _rounds_phrase(rounds: list[int]) -> str:
    """A list of round numbers as a noun phrase: ``round 1``, or ``rounds 1, 2``.

    These land in the veto list, which the operator is told to read as the reason
    a quiet round is not convergence — so it is one of the more closely-read lines
    the tool emits, and ``round 1, 2`` reads as a typo in it."""
    return f"round{'s' if len(rounds) > 1 else ''} {', '.join(str(r) for r in rounds)}"


#: The trend block's columns, in order, with the header each one prints under.
#: A header row rather than a unit repeated in every cell ("14 findings", "9
#: introduced"): this block is competing for space in a report that is already
#: dense, and the round cap means most cycles show two data rows — under which a
#: word repeated per row costs more width than the whole header does.
#:
#: **There is deliberately no density column here, and adding one needs an
#: argument this comment does not have** (#490). While reading the cycle this block
#: exists for, a reporter computed findings-per-10k-chars by hand and got 9.46 ->
#: 7.97 -> 4.82: a number that falls every round, reads as steady improvement, and
#: was describing a cycle that was diverging. It falls because the denominator is
#: growing, which is the failure, not evidence against it. Any per-size figure
#: added here must therefore sit beside BOTH the absolute count and the growth
#: ratio, so that a reader cannot take it on its own — and the safest version, the
#: one this is, adds none at all. `test_panel_trend.py` pins the absence.
TREND_COLUMNS = ("round", "findings", "P1/P2", "introduced", "whole PR")


def _trend_cells(row: RoundTrend) -> list[str]:
    """One trend row as its printable cells, before they are padded to a width.

    Every unknown prints as a mark rather than as a number, and the two marks are
    different on purpose. ``—`` is "the question does not arise" — round 1 has no
    earlier fix pass to have introduced anything — and ``?`` is "it was asked and
    could not be answered", which is a round whose fix range was unreadable, or a
    payload too old to record the size. Collapsed into one mark, a cycle whose
    attribution was silently broken for two rounds reads exactly like a cycle whose
    rounds had nothing to attribute.
    """
    if not row.reviewed:
        # A skipped or refused round measured nothing, and every cell after the
        # round number would be a fabrication. Said in words rather than as four
        # `?`s, because "this round did not happen" is a different fact from "this
        # round's numbers did not survive" and the reader needs it at a glance.
        # Two words that fit under the `findings` header, so one skipped round in a
        # cycle cannot widen the column every other row is read down.
        return [f"r{row.round}", "not run", "", "", ""]
    n, severe = row.findings, row.p1p2
    if row.introduced is None:
        # `—` only where the question genuinely does not arise. Round 1 is the whole
        # of that case: provenance is computed against the round BEFORE, so every
        # later round was asked, and a None there is a measurement that failed.
        got = "—" if row.round == 1 else "?"
    elif n:
        # The percentage is of THIS round's findings, which is the denominator that
        # cannot run away: the count and the share move together, so a round that
        # is mostly self-inflicted says so however few findings it has.
        got = f"{row.introduced} ({round(row.introduced * 100 / n)}%)"
    else:
        # `n` is 0 (no findings, so no share to take — `0 (0%)` would be a division
        # this block never performs) or None (the buckets could not be counted, so
        # there is no denominator to take a share against). The count still prints:
        # it is the number that survived.
        got = f"{row.introduced}"
    size = f"{row.pr_chars:,}" if row.pr_chars is not None else "?"
    # `?`, never `None`. A reviewed round whose finding buckets did not parse has no
    # count, and an f-string over the missing value would print the word `None` into
    # a numeric column — which reads as a value rather than as a gap.
    return [f"r{row.round}", "?" if n is None else f"{n}",
            "?" if severe is None else f"{severe}", got, size]


def _trend_growth(row: RoundTrend, first_chars: int | None) -> str:
    """The growth column: this round's whole-PR size over the size the cycle's
    FIRST reviewed round found it at.

    The denominator is `Baseline.first_reviewed`'s and nothing else, so this ratio
    and `max_fix_growth`'s veto line are the same measurement (#165, #298) — a
    report carrying two ratios computed from two denominators is worse than one
    carrying none, because a reader has no way to tell which one the ceiling is
    about to fire on. Where that denominator is missing the ceiling does not run
    either, and this column says `?` rather than picking a substitute.
    """
    if not row.reviewed:
        # Blank, not `?`: a round that reviewed nothing has no size to be a multiple
        # of anything, and `?` would say the measurement was attempted and lost. Its
        # own `findings` cell already says `not run` in words.
        return ""
    if first_chars is None or row.pr_chars is None:
        return "?"
    return f"{row.pr_chars / first_chars:.2f}x"


def _trend_record(row: RoundTrend, first_chars: int | None) -> dict:
    """One trend row as the PAYLOAD carries it (#490).

    The ratio is stored as well as the two sizes it came from, because the
    denominator is not this row's — it is the cycle's first reviewed round's — and a
    consumer that recomputed it from the rows would have to re-derive
    `Baseline.first_reviewed`'s rule to get the same number. The report and the
    payload therefore quote one arithmetic, which is the property the block is worth
    having at all.

    Rounded to 3 places to match `round_stop`'s `fix_growth.ratio`, which is the
    same quantity for this round's row and would otherwise differ from it in the
    tail digits of a float.
    """
    return {"round": row.round, "reviewed": row.reviewed,
            "findings": row.findings, "p1p2": row.p1p2,
            # #505's series. Carried in the payload but NOT given a printed column:
            # a column needs the argument the comment on `TREND_COLUMNS` demands and
            # this change does not make it, while a consumer plotting the cycle should
            # not have to re-read every round's file to get the number the stop rule
            # used. `round_stop.new_findings_not_falling.counts` carries the same
            # series for the round it decided.
            "new_findings": row.new_findings,
            "introduced": row.introduced, "pr_chars": row.pr_chars,
            "growth": (round(row.pr_chars / first_chars, 3)
                       if first_chars and row.reviewed and row.pr_chars is not None
                       else None)}


def cycle_trend_lines(rows: list[RoundTrend],
                      first_reviewed: tuple[int, int, str] | None) -> list[str]:
    """#490's block: every round of this cycle, side by side.

    Every round's report states that round's own figures and nothing else, so the
    reader has to hold three reports in their head to see which way the cycle is
    going — and read one at a time, a diverging cycle looks flat. 8 -> 14 -> 15
    findings reads as converging; against a PR that tripled on an underlying change
    of 113 lines it is the opposite reading, and it was available from data every
    round already had. On the cycle that produced this, three rounds ran before
    anyone did the arithmetic.

    **Reporting only.** Nothing here is consulted by `round_stop`, by any ceiling in
    `panel_caps`, or by the fixer's brief; it cannot end a cycle and it cannot buy
    one another round. That is deliberate and separable from #489, which proposes
    the gate: chaining a cheap, uncontroversial reporting improvement to a policy
    argument is how the cheap half waits on the expensive one.

    Empty for anything under two rows, which is where the block has nothing to say —
    a single round beside itself is the report the reader already has.
    """
    if len(rows) < 2:
        return []
    first_round, first_chars = (first_reviewed[0], first_reviewed[1]) if first_reviewed \
        else (rows[0].round, None)
    # The growth column names its own denominator, so a reader can see WHICH round
    # the multiples are against without counting rows — a cycle whose round-1 payload
    # was never passed measures from the earliest baseline there is, and a header
    # reading `vs r1` would silently name a round that is not in the comparison.
    #
    # It does NOT read `vs r2` for a cycle whose round 1 was skipped:
    # `Baseline.first_reviewed` takes `ordered[0]` and nothing later, so an earliest
    # round that reviewed nothing leaves no denominator at all. That is #298's
    # deliberate refusal to invent one — `max_fix_growth` does not run there either —
    # and this column reports it rather than working around it.
    #
    # Where there is no denominator every cell is `?`, and a header still claiming
    # `vs r1` would name a comparison that is not being made.
    header = [*TREND_COLUMNS, f"vs r{first_round}" if first_chars else "growth"]
    body = [[*_trend_cells(r), _trend_growth(r, first_chars)] for r in rows]
    # Right-aligned, header included in the width, so the numbers line up under
    # their own labels — the whole value of the block is that a column can be read
    # down, and a ragged column cannot.
    width = [max(len(r[i]) for r in (header, *body)) for i in range(len(header))]
    # The leading blank line is load-bearing markdown, not spacing. Everything
    # directly above this block is a `  - ` bullet, and a paragraph starting at
    # column 0 straight after one is a LAZY CONTINUATION of that list item in GFM —
    # which would indent the fence under the bullet and render the table inside it.
    out = ["",
           "**Cycle so far** — every round of this cycle beside the others, because "
           "a round's own figures cannot show which way the cycle is going:", "",
           "```"]
    out += ["  ".join(c.rjust(w) for c, w in zip(r, width)).rstrip()
            for r in (header, *body)]
    out += ["```", ""]
    # The one sentence of guidance, and it is about the pair rather than about any
    # single column: findings that hold steady while the PR grows are the reading
    # this block exists to make visible, and a reader who takes the finding count
    # alone gets the same wrong answer the block was built to prevent.
    out.append("Read the counts and the size TOGETHER — a finding count that holds "
               "while the PR grows is not convergence, and `introduced` is the share "
               "of each round's findings that the fix pass before it wrote. "
               "`—` is a question that does not arise; `?` is one that could not be "
               "answered.")
    out.append("")
    return out


def _veto_gist(text: str, limit: int = 80) -> str:
    """The identifying head of a veto that is also a config note — enough to say
    WHICH note without repeating its full text on the PR comment. The problem
    strings put their consequence after an em-dash, so the head is the fact."""
    head = text.split(" — ", 1)[0].strip()
    return head if len(head) <= limit else head[:limit - 1] + "…"




def fit_comment(report: str, limit: int = COMMENT_CHARS) -> str:
    """The report, cut to fit a GitHub comment (65,536 chars, hard).

    The per-reviewer accounts are the part that grows without bound — one block
    per reporter per merged finding — so they go first and the verdicts survive;
    a report still over the limit is cut with a marker. The terminal copy is
    never trimmed, and neither are `--json` or the board record: this is about
    what `--post` can physically send, and a review that succeeded must not be
    lost to a comment one account too long.

    The round verdict is the one block a cut is taken AROUND rather than through:
    it sits at the foot of the report, it is what the caller of a cycle acts on,
    and a truncation from the end would drop precisely it.

    Reserved, not exempt. The verdict block carries one veto line per reviewer per
    declared gap, from free text a model wrote, so it is unbounded in principle —
    and reserving all of it clamped the SLICE rather than the RESULT, returning
    `cut + tail` over the limit and losing the whole comment to a hard API
    rejection. When the block alone will not fit it is cut from its own end, which
    keeps the mechanical verdict (its first line) and drops the vetoes."""
    if len(report) <= limit:
        return report
    note = ("\n\n_Per-reviewer accounts omitted — the full report exceeds GitHub's "
            "comment limit. They are intact in `--json` and on the board._")
    trimmed = "\n".join(ln for ln in report.splitlines() if not ln.startswith("  - _"))
    if len(trimmed) + len(note) <= limit:
        return trimmed + note
    cut = ("\n\n_…report truncated at GitHub's comment limit. The full run is in "
           "`--json` and on the board._")
    if limit <= len(cut):
        # No room for even the marker: the caller asked for a length no honest
        # report fits in, so give it the report's own first characters.
        return trimmed[:max(0, limit)]
    head, sep, tail = trimmed.partition("\n\n" + ROUNDS_HEADING)
    tail = sep + tail if sep else ""
    room = limit - len(cut)
    if len(tail) > room:
        tail = tail[:room - 1] + "…"
    return head[:room - len(tail)] + cut + tail


POST_TIMEOUT_S = 120


def post_summary(gh_repo: str, pr_number: int, report: str) -> bool:
    """Comment `report` on the PR. True when GitHub took it.

    Bounded, and NOT check=True. This is the last step of a run that has already
    succeeded and already printed its report: a hung network call here would
    block after every expensive thing is done, and raising would throw away a
    completed review over a failed comment. The comment is how the fix loop finds
    the findings, so a failure has to be LOUD — but it degrades the run, it
    doesn't void it.

    A function rather than the inline block it was, because #138 gave it a second
    caller with a stronger claim on it than the first. A REFUSED round posts too,
    and posting is most of what makes the refusal loud: the terminal copy is read
    by whoever is watching, and under the epic (#52) nobody is. A refusal that
    exists only in a payload is the silent skip the refusal was built to replace.
    """
    try:
        proc = subprocess.run(["gh", "pr", "comment", str(pr_number), "--repo",
                               gh_repo, "--body", fit_comment(report)],
                              capture_output=True,
                              text=True, stdin=subprocess.DEVNULL,
                              timeout=POST_TIMEOUT_S)
        if proc.returncode == 0:
            print(f"\n(posted panel summary to {gh_repo}#{pr_number})")
            return True
        why = stderr_gist(proc.stderr or "") or f"exited {proc.returncode}"
    except (subprocess.TimeoutExpired, OSError) as e:
        why = (f"timed out after {POST_TIMEOUT_S}s"
               if isinstance(e, subprocess.TimeoutExpired) else e.__class__.__name__)
    print(f"\n! panel summary NOT posted to {gh_repo}#{pr_number} ({why})"
          f" — the report above is the only copy", file=sys.stderr)
    return False


def run(repo_name: str | None, pr_number: int, post: bool, json_out: bool = False,
        reviewers: str | None = None, json_file: str = "", record: bool = True,
        round_no: int = 1, baseline: list[str] | None = None,
        max_rounds: int | None = None, scope: str = "auto",
        since: str = "", force: bool = False,
        no_code_access: bool = False,
        escalated: list[str] | None = None,
        escalated_from_board: bool = False,
        acknowledge: list[str] | None = None,
        premise_file: str = "") -> int:
    # A cycle is something the CALLER drives, and only /panel-review-pr does:
    # naming a cap (or a round, or a baseline) is what says this run is part of
    # one. A review-only /panel run left to the default is a single pass, and
    # must not report itself as "round 1 of at most 2 — go again", promising a
    # re-review nothing will run.
    # `--escalated` is deliberately NOT one of them, and `main` refuses the flag
    # unless one of the three is given. It names work a LATER round must not count,
    # and it is read out of a fix pass that by construction followed a review
    # round — so `--escalated` with no round, cap or baseline is a caller error,
    # and the loud refusal at the edge is the whole of the answer. Treating the
    # flag as evidence of a cycle (the shape this had for one round) produced
    # exactly what the comment above forbids: "round 1 of at most 2 — go again",
    # promising a re-review nothing will run.
    in_cycle = max_rounds is not None or round_no > 1 or bool(baseline)
    # Idempotency key for the board record, minted once per process so a retry of
    # the POST cannot double-count the run into the stats. A fresh panel run is a
    # genuinely new observation and gets a new key — re-reviewing a PR after a fix
    # loop is data, not a duplicate.
    run_key = uuid.uuid4().hex
    # The round's stopwatch, started HERE rather than beside the seats (#192).
    # Half of a cycle's wall clock was unattributable, and the reason a
    # measurement that starts where the interesting code starts cannot close that
    # gap is that it can only ever report the part somebody already suspected.
    # Everything from the config read down is inside a phase.
    clock = panel_timing.RoundClock()
    cfg = load_repo_cfg(repo_name)
    # The name RESOLVED from the checkout, never the argument. `--repo` is
    # optional — `panel.py --pr N` in a repo is the documented single-PR form —
    # and the unresolved None went straight into the payload as `"repo": null`.
    # A payload that does not say which review it is from cannot be a baseline:
    # round 2 discarded round 1 as unattributable, called every finding new, and
    # could never record a confident stop. The whole round diff no-opped for
    # anyone who did not pass a flag they were never told to pass.
    repo_name = cfg.get("name") or repo_name
    gh_repo = cfg["github"]
    rev = cfg["reviewers"]
    panel = cfg["review_panel"]
    # Every config diagnostic this run will report. Initialised HERE, not beside the
    # file-list warnings below it, because #165's dials are resolved before the PR is
    # fetched: a rules file with a bad `fix_severity_floor` has to say so whether or
    # not the PR read succeeds, and the round cap the verdict is computed against
    # comes out of the same resolution. Everything downstream still just appends.
    # A round that ran under a board-set dial SAYS SO, in the list `--post` puts in
    # a public PR comment — #52's "never silent" applied to the layer that can move
    # a floor without a pull request. First, before the dial resolution below it,
    # because it is about where the dials came FROM and reads oddly after a
    # complaint about one of their values.
    notes: list[str] = board_dial_notes(cfg)
    # The eight `review_panel` settings that trade thoroughness against convergence,
    # resolved once (`panel_seats.resolve_dials`) so the prompt, the report, the stop
    # rule and the payload cannot disagree about which policy this round ran under.
    # #55's round ceiling, resolved before the dials because it is an INPUT to
    # them. `None` unless the board stated `review_panel.max_rounds` itself, and
    # `None` is exactly today's behaviour — which is what lets this land on a fleet
    # that has set no dial and change nothing at all.
    round_cap_ceiling, _ceiling_said = panel_caps.round_ceiling(cfg)
    dials = resolve_dials(panel, max_rounds, notes, round_cap_ceiling)
    cap = dials.max_rounds
    # #84's futility brake, from the round's side. Resolved beside the dials and for
    # the same reason: a rules file with a bad `escalate_on` has to say so whether or
    # not the PR read succeeds, and the brake is part of the policy the round's stop
    # is computed under.
    #
    # The register is READ here and never written — `panel.py --premise` is the only
    # writer, because the count is of fix passes PROPOSED and a round proposes none.
    # Two writers would be two answers to "how many times was this premise declared",
    # which is the one question the brake exists to answer.
    premise_limit = premise_repeat_limit(panel, notes)
    premise_undecidable = premise_undecidable_brake(panel, notes)
    # #489's brake, read here rather than at the stop rule so that a malformed value
    # hard-exits at the same moment `premise_repeated`'s does — before a seat is
    # dispatched, rather than after a whole panel has been paid for.
    injection_limit = fix_injection_limit(panel, notes)
    # #505's volume rung, read here for the same reason and at the same moment: a
    # malformed value has to hard-exit before a seat is dispatched rather than after a
    # whole panel has been paid for, and the round's stop is computed under one policy
    # that was resolved in one place.
    not_falling = not_falling_limit(panel, notes)
    # #554's rung, read here for its three siblings' reason and at their moment: a
    # malformed value has to hard-exit before a seat is dispatched rather than after a
    # whole panel has been paid for, and the round's stop is computed under one policy
    # resolved in one place.
    unrefereed_armed = unrefereed_fix_brake(panel, notes)
    # #507's constructive pass. Read at the same moment as the three brakes above, and
    # for their reason: a malformed value hard-exits before a seat is dispatched
    # rather than after a whole panel has been paid for. What it governs is not a
    # brake — it cannot stop or extend a cycle, and `panel_propose` runs after the
    # verdict is final — it governs whether an escalation ARRIVES with a proposal on
    # it or with a list of complaints and nothing else.
    propose_armed = panel_flag(panel, "propose_on_escalation", True, notes)
    # #548's local suite, read at the same moment as the brakes above and for their
    # reason: a malformed value has to hard-exit before a seat is dispatched rather
    # than after a whole panel has been paid for. It is read here and USED much
    # further down, beside the CI settle — the one place that knows whether GitHub
    # had anything to say — because running the suite is only ever the answer to
    # that question. Nothing is executed by this read.
    local_cmds = local_suite_commands(panel)
    local_timeout = local_suite_timeout(panel)
    premises, premise_problems = load_premises(premise_file, gh_repo, pr_number)
    notes.extend(premise_problems)

    # A repo that configured no review does not get one. Before `gh pr view`, and
    # before the --reviewers check below it, because this refusal is about the repo
    # and not about the run: there is nothing to spend an API call finding out.
    #
    # Shaped exactly like the title-pattern skip forty lines down — a loud line, a
    # payload with `reviewed: false` and a `skip_reason`, exit 0, and NOT recorded on
    # the board, because no review happened. That shape is load-bearing on the epic's
    # merge gate, which reads `reviewed`/`skip_reason` off the payload precisely
    # because a zero exit, a push and the existence of a payload have each been
    # mistaken for a review in turn (see `epic.sub_pr_merge` in the sample).
    # `enabled: false` — the repo's off switch, honoured here for the first time.
    # `lander.py` has read it since it existed and the review paths never did, so a
    # repo that had switched itself off still got panelled. It rides on
    # `review_refusal`'s path rather than beside it because it is the same KIND of
    # answer: per-repo, terminal, decided before an API call is spent, and not a
    # review that happened. #55's fourth acceptance criterion is served by the dial
    # behind it (`POST /dials {"dial": "enabled", "value": false, "repo": …}`),
    # which takes effect on the next resolution rather than the next restart —
    # `resolve_repo` reads the board on every run.
    refusal = panel_caps.enabled_refusal(cfg) or review_refusal(cfg)
    if refusal:
        # Its own stream selection rather than `chatter`, which is assigned below the
        # PR fetch this refusal exists to skip.
        print(f"[{repo_name}#{pr_number}] {refusal} — refusing to review. No panel ran.",
              file=sys.stderr if json_out else sys.stdout)
        unconfigured_payload = {
            **_payload_defaults(),
            "repo": repo_name, "github": gh_repo, "pr": pr_number,
            # Nothing was fetched, so nothing about the PR is known: `title` and
            # `base` are null here where the title-pattern skip has them, and that
            # is the honest difference between "we read the PR and declined it" and
            # "we declined before reading it".
            "title": None, "base": None,
            "round": round_no,
            "skip_reason": refusal,
            "config_notes": [refusal],
            "rules": rules_record(cfg),
            # No baseline was read on this path, so there is no earlier end and no
            # fix phase — but the payload still says when it started and stopped,
            # because `_payload_defaults`' rule is that a consumer reading a key
            # should not have to know which exit produced the payload.
            "timing": panel_timing.timing_block(clock,
                                                panel_timing.fix_phase(clock.started_at),
                                                measured_to="unconfigured"),
            "run_key": run_key,
        }
        # No `load_baseline` and no cycle bookkeeping, unlike the title skip. That
        # one is per-PR and the cycle around it goes on; this is per-REPO and
        # terminal — every round of every cycle here refuses identically until a
        # rules file is committed, so there is no next round for a baseline to
        # anchor and nothing an inherited escalation register could be carried by.
        failed = write_payload(json_file, unconfigured_payload)
        if json_out:
            print(json.dumps(unconfigured_payload, indent=2))
        return finish(failed)

    # `--round 4` against a cap of 2 is a caller error, and it used to be checked in
    # `main` against the CLI flag and the built-in constant alone — so a repo setting
    # `review_panel.max_rounds: 3` had `--round 3` refused on the strength of a cap it
    # had raised. Checked HERE instead, against the cap this run will actually apply,
    # and still before anything is fetched. Without it the round runs and hits the cap
    # branch on the spot, writing "round cap (2) reached — …, unreviewed" into a round
    # 3 whose caller believed it had asked for more.
    if round_no > cap:
        # The BOARD's ceiling is named first when it is what bound, because the two
        # remedies differ and only one of them is available to the person reading
        # this: a repo cap is raised by editing a file, and a fleet ceiling is not
        # raisable from here at all (#55).
        blame = (f"`review_panel.max_rounds` ({round_cap_ceiling}) set on the board"
                 if round_cap_ceiling is not None and cap == round_cap_ceiling
                 else "--max-rounds" if max_rounds is not None
                 else f"`review_panel.max_rounds` ({panel.get('max_rounds')})"
                 if panel.get("max_rounds") not in (None, "") else
                 f"the default cap of {DEFAULT_MAX_ROUNDS}")
        remedy = ("this ceiling is fleet policy and cannot be raised from inside the "
                  "repo being reviewed — clear or move the dial on the board"
                  if round_cap_ceiling is not None and cap == round_cap_ceiling
                  else "raise the cap")
        sys.exit(f"panel: --round {round_no} is past the cap of {cap}, from {blame}: "
                 f"{remedy}, or pass the round this run actually is")

    # #55's spend ceiling, RESOLVED here and CHECKED further down. The two halves
    # are split because they cost different things and want different moments: this
    # one is pure validation over the resolved rules, so a typo'd ceiling dies
    # beside the other bad-dial refusals and before an API call is spent on a run
    # that cannot proceed; the board read that checks it against what has actually
    # been spent waits until this PR is known to be one the panel would review at
    # all. Dormant unless somebody has set a number.
    budget = panel_caps.resolve_budget(panel, notes)

    # Resolved before anything is fetched, so a typo'd --reviewers fails on the
    # spot rather than after a PR read and a diff download.
    selected, override_note = select_reviewers(rev, reviewers)

    try:
        meta = json.loads(panel_core.sh(["gh", "pr", "view", str(pr_number), "--repo", gh_repo,
                              "--json", "title,additions,deletions,baseRefName,"
                                        "baseRefOid,headRefName,headRefOid,files,"
                                        "changedFiles,state,isDraft,mergeable"]))
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or "").strip().splitlines()
        # `gh pr view --json` rejects the WHOLE command on a field it does not
        # know ("Unknown JSON field: …", exit 1) rather than omitting it — which
        # is why no absent-field fallback lives in `_changed_files` any more: on
        # a `gh` too old for `files`/`changedFiles`/`state` the run dies here, and
        # the branch that claimed to handle it could never have run. Say so, since
        # "cannot read PR" reads like a network or permissions problem.
        sys.exit(f"panel: cannot read PR #{pr_number} in {gh_repo}"
                 + (f" — {tail[-1][:160]}" if tail else "")
                 + ("\n  (a `gh` predating --json files/changedFiles/state fails the whole "
                    "call on an unknown field; panel needs gh >= 2.40)"
                    if tail and "Unknown JSON field" in tail[-1] else ""))
    title, base = meta["title"], meta["baseRefName"]
    # The commit under review. Already fetched for the Sonar staleness check; it
    # is carried into the payload now because the next round needs it to tell a
    # defect its own fix pass created from one this round simply missed.
    head_sha = meta["headRefOid"]
    # The base end of the same range (#98), as GITHUB STORES IT — which is not the
    # merge base and must not be recorded as one (#241). It is what `gh pr diff`
    # builds its three-dot diff from, so it is the honest answer to "what was this
    # round's target measured against"; the true fork point is computed below, past
    # the skip branch, and the two are reconciled there. `.get`, not `[...]`: every
    # other key here is required because the run cannot proceed without it, and a
    # base commit is not that.
    stored_base = meta.get("baseRefOid") or None
    # What the SKIP path records. The skip branch returns before the merge-base
    # computation below, on purpose: that path exists to cost nothing, never
    # fetches a diff and never reaches the board, so it keeps the value that is
    # free off the metadata already in hand rather than buying an API call for a
    # round that reviewed nothing.
    merge_base = stored_base
    # Must the branch be able to MERGE for a round to be worth running (#271)? The
    # DIAL is read here, before anything is fetched, because `panel_flag` exits on
    # a value that is neither true nor false and a rules file this harness cannot
    # obey has to fail at the door. The question itself is asked past the skip
    # branch below, where it may cost an API call.
    require_mergeable = panel_flag(panel, "require_mergeable", True, notes)
    changed = meta["additions"] + meta["deletions"]
    # Same call that already produced `changed`, three fields wider — so the board
    # gets the paths behind the number, and the PR's state, without a second
    # round-trip, and gets them on the skip path too, where no diff is ever
    # fetched. The state is as of THIS panel: the board is told about panels, not
    # about merges, which is what the payload's timestamp is for.
    changed_files, changed_files_total, dropped_files = _changed_files(meta)
    pr_state, is_draft = meta.get("state"), meta.get("isDraft")

    # These are built BEFORE the skip branch, because the skip branch returns. They
    # used to sit with the diff budgets forty lines below, so a skipped PR carrying
    # two paths and a total of 3,000 said nothing at all — and the skip path is the
    # one this release argues is most likely to be merged unattended, which makes it
    # the worst possible place for the warning to go missing.
    # `dropped` is excluded on purpose: a discarded malformed row is not GitHub
    # paging us short, and one note covering both would send a reader looking for
    # a truncation that never happened.
    listed = len(changed_files) + dropped_files
    if changed_files_total is not None and listed < changed_files_total:
        notes.append(f"the PR's file list came back partial — {listed:,} of "
                     f"{changed_files_total:,} changed files; collision queries "
                     "against this run will under-report")
    if dropped_files:
        notes.append(f"{dropped_files:,} file entr{'y' if dropped_files == 1 else 'ies'} "
                     "had no usable path and were dropped")
    # Checked at the door, and before the skip branch returns. `--escalated` is
    # the one input here read out of a fixer's PROSE report rather than produced
    # by a machine, and its value is both written into every later round's
    # baseline and interpolated into a `config_notes` line that `--post` puts in a
    # public PR comment. Rejected rather than passed on, reported rather than
    # dropped: the note names a flattened, truncated excerpt, which says which
    # value was wrong without putting a caller's arbitrary markdown on the PR.
    #
    # Deduplicated, because `panel-review-pr.md` documents re-passing a key you
    # inherited as harmless and it has to actually be: this loop and the skip
    # branch's below both iterate the values, so `--escalated K --escalated K`
    # wrote the same note twice — into the payload, and with `--post` into a public
    # PR comment. On the value as WRITTEN (a `str()` of it), not on the value
    # itself: one duplicate note per spelling the caller used, and nothing here
    # assumes the caller passed something hashable.
    declared: list[str] = []
    reported: set[str] = set()
    named = list(escalated or [])
    if escalated_from_board:
        # Unioned with the hand-named keys rather than replacing them: the board
        # knows what an earlier round recorded, the caller knows what the fix
        # pass just decided, and neither is a superset of the other. The fetch's
        # own failure is a note and never an exception — a board that will not
        # answer must not cost a review that can still run.
        from_board, why = board_escalations(gh_repo, pr_number)
        if why:
            notes.append(why)
        else:
            fresh = [k for k in from_board if str(k) not in {str(x) for x in named}]
            notes.append(
                f"--escalated-from-board: the board reports {len(from_board)} "
                f"finding(s) waiting on a human on this PR"
                + (f", {len(fresh)} of them not named on the command line" if fresh
                   else " and every one was already named"))
            named += fresh
    for raw in named:
        if str(raw) in reported:
            continue
        reported.add(str(raw))
        if _is_key(raw):
            # The NORMALISED key: a value transcribed out of prose arrives
            # upper-cased or newline-padded often enough, and the register has to
            # hold the spelling a finding's own key equals.
            key = _key_norm(raw)
            if key not in declared:
                declared.append(key)
        else:
            # EVERY value that is not a key, the EMPTY one included. The
            # `elif str(raw or "")` this replaces let `--escalated ""` take neither
            # branch — no key recorded and no note, which is the one outcome this
            # flag's design rules out — and the empty value is the likeliest of all
            # to arrive: `--escalated "$KEY"` with an unset shell variable, or an
            # orchestrator interpolating a `Key:` line the fixer's report never
            # carried. `_key_gist` renders it `(empty)`, which is what that
            # fallback was written for.
            notes.append(f"--escalated `{_key_gist(raw)}` is not the shape of a finding "
                         "key (8-64 hex characters) — it was ignored, so the finding it "
                         "meant still counts as work a fix round can clear")

    # `--acknowledge` (#547), checked at the same door and by the same rules, because
    # it arrives the same way: a key a person read off a PR comment and typed back.
    # Deduplicated on the spelling passed, refused loudly when it is not the shape
    # this loop mints, and the refusal says what the silence would otherwise cost —
    # an ignored acknowledgement is an obligation that goes on vetoing while the
    # caller believes it discharged, which is the permanent HOLD wearing a fix.
    accepted: list[str] = []
    seen_ack: set[str] = set()
    for raw in acknowledge or []:
        if str(raw) in seen_ack:
            continue
        seen_ack.add(str(raw))
        if is_claim_key(raw):
            key = str(raw).strip().lower()
            if key not in accepted:
                accepted.append(key)
        else:
            notes.append(
                f"--acknowledge `{_key_gist(raw)}` is not the shape of an obligation "
                f"key ({CLAIM_KEY_PREFIX} and 12 hex characters, as the report prints "
                "it) — it was ignored, so the claim it meant still costs the round its "
                "confidence")

    # Progress goes to stderr in --json mode, so stdout is the payload and only
    # the payload: it is a machine-readable artifact, and a consumer that has to
    # strip a two-line preamble before parsing is one preamble away from breaking.
    chatter = sys.stderr if json_out else sys.stdout

    # Title-pattern skip (merges/promotes/format-the-world — not worth LLM review)
    for pat in panel.get("skip_title_patterns", []):
        if re.search(pat, title, re.I):
            print(f"[{repo_name}#{pr_number}] '{title[:50]}' matches skip pattern "
                  f"/{pat}/ — not worth panel review. Skipping.", file=chatter)
            # A consumer gets a payload on every non-error exit, or "reviewed
            # and found nothing" and "never reviewed at all" arrive as the
            # same empty stdout — and the second one silently reads as a
            # clean PR. Same SHAPE as a reviewed run, too, so reading any
            # other key of it is not a KeyError.
            #
            # RECORDED on the board since #94, and the payload's own
            # `reviewed: false` is what makes that not a lie. It used to
            # return here, on the correct reasoning that no review happened
            # and a non-event recorded as an event is its own disease — but
            # the board then held no file list for merges, promotes and
            # format-the-world commits, which are precisely the changes that
            # touch the most files and collide with the most work, and
            # `GET /review/collisions` saw a skipped PR as neither subject
            # nor rival. The fix is not to pretend a review happened; it is
            # to record what this round MEASURED and let the row say, in the
            # column the board now stores, that nothing was reviewed.
            #
            # It says WHICH round it is and which cycle that round belongs to,
            # because the caller is told to feed every round's --json-file
            # forward as the next round's --baseline. Left on the defaults it
            # serialised a skipped round 2 as round 1 with a fresh id, which then
            # collided with the real round 1 over the round number and renamed
            # the cycle out from under every later round.
            skip_prior = load_baseline(baseline or [],
                                       {"repo": repo_name, "github": gh_repo,
                                        "pr": pr_number, "round": round_no})
            # A key this round was handed that the register does not already hold
            # is LOST here, and said so. The alternative — recording it — dates
            # the declaration to a round that reviewed nothing and writes it in
            # unchecked, since the typo check needs findings this round does not
            # have. Silence is the one option ruled out: the caller would believe
            # the finding was excluded while every later round counted it.
            for key in sorted(k for k in declared if k not in skip_prior.escalated):
                notes.append(f"--escalated {key} was passed to a round that reviewed "
                             "nothing, so it was NOT recorded — pass it again on the "
                             "next round that runs")
            # The same answer for the same reason: a round that reviewed nothing
            # raised no obligations, so there is nothing here for an acknowledgement
            # to attach to and dating it to this round would write it in unchecked.
            for key in sorted(k for k in accepted if k not in skip_prior.acknowledged):
                notes.append(f"--acknowledge {key} was passed to a round that reviewed "
                             "nothing, so it was NOT recorded — pass it again on the "
                             "next round that runs")
            skipped_payload = {
                **_payload_defaults(),
                "repo": repo_name, "github": gh_repo, "pr": pr_number,
                "title": title, "base": base,
                # A skipped round still moved the head, and both of the next
                # round's readers have to start somewhere: its increment anchors
                # here, and its fix range runs from here. Left null, a skip
                # anywhere in a cycle loses the anchor entirely (round 3 silently
                # re-reads the whole PR) and blinds provenance for the round
                # after it.
                "head_sha": head_sha,
                # Free off the metadata this path already fetched. `base_sha` is
                # NOT here and is left at its null default: reading the base
                # branch's tip is a second API call, and this path is the one
                # that exists to cost nothing and is never recorded on the board.
                "merge_base": merge_base,
                # Zeroed rather than left `{}` when there ARE earlier rounds:
                # `{}` is the shape for a round where the question does not arise,
                # and a skipped round 3 of a cycle is not that — it attributed
                # nothing because it reviewed nothing, which a consumer must be
                # able to tell from "not a cycle run".
                "fix_range_source": None,
                "fix_range_rebuilt": None,
                "provenance_counts": ({b: 0 for b in PROVENANCE}
                                      if skip_prior.rounds else {}),
                # #67's two tallies follow the same rule, for the same reason.
                "recurrence_counts": ({b: 0 for b in RECURRENCE}
                                      if skip_prior.rounds else {}),
                "premise_counts": ({b: 0 for b in (*PREMISE_VERDICTS, "not-said")}
                                   if skip_prior.rounds else {}),
                # A skipped PR still collides with everything it touches, and it
                # is the case most likely to be re-merged unattended. The paths
                # are already in hand here — the diff never is.
                "changed_lines": changed,
                "changed_files": changed_files,
                "changed_files_total": changed_files_total,
                "pr_state": pr_state,
                "is_draft": is_draft,
                # The file-list warnings, built above this branch for exactly this
                # reason, plus any baseline problem — a baseline this run could not
                # read is a fact about the cycle, not about the review it skipped,
                # so it travels rather than being dropped on the floor.
                "config_notes": notes + skip_prior.problems,
                "rules": rules_record(cfg),
                # A skipped round carries the cycle's open escalations forward
                # and adds nothing to them. It is the baseline the NEXT round
                # inherits, and a register that emptied whenever a title matched
                # /^Merge / would lose the question on the quietest round of the
                # cycle — but it reviewed nothing, so it cannot date a declaration
                # to a round that read no code, and the typo check below the skip
                # branch (which needs this round's findings) never ran on this
                # path. A key passed here is reported instead, above.
                "escalated": dict(skip_prior.escalated),
                # Carried forward and added to by nothing, exactly as `escalated` is
                # and for the identical reason: the claims are still unverifiable and
                # the person who accepted them has not un-accepted them because a
                # title matched /^Merge /.
                "acknowledged": dict(skip_prior.acknowledged),
                "round": round_no,
                "cycle": skip_prior.cycle,
                "prior_rounds": len(skip_prior.rounds),
                "prior_findings": len(skip_prior.keys),
                "skip_reason": f"title matches skip pattern /{pat}/",
                # A finish, for the same reason `head_sha` is here (#192): this
                # payload is the next round's `--baseline`, and its fix phase runs
                # from where THIS round stopped. Left null, that round measures
                # from whichever earlier round last recorded a finish and reports a
                # span containing a whole skipped round as a fix phase.
                "timing": panel_timing.timing_block(
                    clock,
                    panel_timing.fix_phase(clock.started_at,
                                           prior_finished_at=skip_prior.finished_at,
                                           prior_round=(skip_prior.finished_round
                                                        or skip_prior.head_round),
                                           prior_head_sha=skip_prior.head_sha,
                                           head_sha=head_sha,
                                           repo_path=cfg.get("path") or ""),
                    measured_to="skip"),
                "run_key": run_key,
            }
            # Recorded BEFORE the file is written and before the payload is
            # printed, exactly as the reviewed and refusal paths do it and for
            # the same reason (#284): a board that would not take the run has to
            # say so in the artefact on disk and in `--json`, not in a stderr line
            # inside a subprocess nobody reads. Under `record`, because
            # `--no-record` is a caller saying this run does not go on the board.
            #
            # Appended to the PAYLOAD's list and not to `notes`, which is the one
            # difference from the reviewed path worth stating. There `notes` IS
            # `payload["config_notes"]` — the same object — so appending to either
            # reaches both. Here `config_notes` was built as `notes +
            # skip_prior.problems`, a new list, so a note appended to `notes`
            # after the payload exists lands nowhere the payload can see. The
            # refusal path does it this way for the same reason.
            #
            # What is sent carries `reviewed: false` and `skip_reason`, so the
            # board stores a run that states it reviewed nothing. It has no
            # `reviewers_selected` and no findings, so it contributes no
            # scorecard and no finding row — every per-reviewer statistic is
            # untouched by construction rather than by a filter.
            if record:
                missed = record_run(skipped_payload)
                if missed:
                    skipped_payload["config_notes"].append(missed)
            # --json-file is honoured here too, and its failure fails the run the
            # same way. The caller is told "if the panel could not write that file
            # the round did not happen", and it then feeds the file to the next
            # round as `--baseline`: a skipped PR that exited 0 leaving no file
            # gave that caller no signal at all.
            failed = write_payload(json_file, skipped_payload)
            if json_out:
                print(json.dumps(skipped_payload, indent=2))
            return finish(failed)
    # #55's spend ceiling, checked at last — past the title-pattern skip and long
    # before a seat is dispatched, which is the issue's requirement that enforcement
    # happen before the spend rather than after it.
    #
    # AFTER the title skip and not before it, and that is a correctness ordering
    # rather than one fewer request. A title-skipped PR spends nothing, so a ceiling
    # has nothing to say about it — and an unattended run whose board is unreachable
    # REFUSES on an unverifiable ceiling, which checked earlier would have turned a
    # release-merge that costs zero into a refusal. Cheap for the same reason: with
    # every ceiling unset this makes no board call at all.
    #
    # The verdict is carried to the pre-flight gate rather than acted on here, for
    # the reason the mergeability precondition is: everything a refusal needs — the
    # payload, `skip_reason`, the per-seat `ran: false` rows, the board record and
    # the PR comment — already exists down there, and a budget stop that did not
    # travel it would be the "looks like a clean review" failure #55 names.
    caps = panel_caps.check(cfg, panel, pr_number, notes, budget=budget)

    print(f"\n[{repo_name}#{pr_number}] {title[:60]}", file=chatter)
    print(f"  base={base}  changed={changed} lines\n", file=chatter)

    # ---- CAN THIS BRANCH MERGE AT ALL (#271)? The cheapest refusal in the system,
    # and until now the LAST one made: `preland.check_pr_state` refuses a
    # CONFLICTING branch at the merge gate, which is after a full multi-vendor
    # round and a judge have been spent on a diff that must be rebased before it
    # can land. Measured on PR #270 — 28 files, 5,572 lines, a branch four commits
    # behind its base — while the issue was being written.
    #
    # Past the title-skip branch, which returns above: a round skipped for its
    # title reviewed nothing for a reason that has nothing to do with merging, and
    # a note claiming a precondition refusal would be a second answer to "why is
    # this payload empty" — on the one path that must also stay free of API calls.
    #
    # **Asked TWICE when the first answer is UNKNOWN, and that is what makes this
    # gate work at all.** GitHub computes mergeability lazily: the first query
    # schedules the merge test and answers UNKNOWN while it runs. Measured on this
    # repo, three consecutive reads of an open PR gave UNKNOWN, CONFLICTING,
    # CONFLICTING — so a gate that asks once refuses only the PRs somebody happened
    # to have looked at recently, which is a gate that appears to work and mostly
    # does not. The re-read is one cheap call, and only on the cold answer.
    #
    # The answer then travels two ways. `gate` reaches the pre-flight verdict and
    # refuses the round through the machinery that already exists for that
    # (`skip_reason`, `preflight.verdict`, the per-seat `ran: false` rows,
    # `--force`); the NOTE is what a round that ran against a non-mergeable head
    # anyway says in its payload, rather than leaving a reader to infer it.
    # `config_notes: []` under a wrong target is the whole of #241's complaint, and
    # this is its sibling defect.
    mergeable, mergeable_said = mergeability(meta)
    if mergeable == "UNKNOWN":
        again = _mergeable_now(gh_repo, pr_number)
        if again:
            mergeable, mergeable_said = mergeability({"mergeable": again})
    gate = mergeable_said if mergeable == "CONFLICTING" and require_mergeable else ""
    merge_gate = gate
    if mergeable == "CONFLICTING" and not merge_gate:
        notes.append(f"{mergeable_said}. Reviewed anyway because "
                     "`review_panel.require_mergeable` is off for this repo: the "
                     "merged state these findings reason about does not exist yet, "
                     "and the rebase will change the diff they are about")
    elif mergeable == "CONFLICTING" and force:
        # `gate` is still set, and still recorded — `--force` turns the verdict
        # into `run` and leaves `preflight.would_have: refuse` behind it, which is
        # this repo's standing rule that "the tool chose to run" and "a caller
        # overrode the tool" must never look alike. This is the half a human reads.
        notes.append(f"{mergeable_said}. Reviewed anyway on --force: the merged "
                     "state these findings reason about does not exist yet, and "
                     "the rebase will change the diff they are about")
    elif mergeable == "CONFLICTING":
        notes.append(f"{mergeable_said}. This round is REFUSED before any seat is "
                     "dispatched — rebase and re-run, or set "
                     "`review_panel.require_mergeable: false`")
    elif mergeable_said:
        # Still not computed after two reads, or a `gh` too old to know the field.
        # The merge gate warns rather than refusing on it and so does this: a
        # refusal on "we could not tell" would stop a round on GitHub's own
        # scheduling. The fact is recorded either way — an unread precondition is
        # not a satisfied one — and it says the question was put twice, so a reader
        # does not take it for the cold first answer it usually is.
        notes.append(f"{mergeable_said}. It was asked for twice")

    # The spend ceiling outranks the mergeability precondition, and not only
    # because it was decided first. A CONFLICTING branch is a reason this round
    # would be WASTED; a reached ceiling is a reason this round may not HAPPEN —
    # and the second is the one `--force` must not turn into a run, so which of the
    # two occupies `gate` decides whether the flag works. Naming both would be a
    # refusal whose remedy list contains one thing the reader cannot do.
    #
    # Applied AFTER the mergeability notes rather than instead of them: a round
    # stopped for spend on a branch that also cannot merge should still say so, in
    # the payload, where the next round reads it.
    gate_overridable = not caps.stop
    if caps.stop:
        gate = caps.refusal
        if force:
            notes.append(
                "--force did NOT override the spend ceiling. It overrides this "
                "host's judgement about what its own seats can read; the ceiling "
                "is a number a person set on the board for the fleet, and a local "
                "flag that switched it off would make it advice again")

    # ---- WHERE THIS BRANCH ACTUALLY FORKED (#241). Past the skip branch, which
    # returns above and must stay free of API calls, and before the diff so that
    # everything downstream — the payload, the mid-round re-read, the next round's
    # anchor — agrees about which commit it means.
    #
    # `baseRefOid` is not this. GitHub maintains it for its own purposes and it has
    # been measured wrong in both directions on this repo: OLDER than the fork
    # point on PR #187, where a commit shared with another PR landed on `main` and
    # nothing recomputed the stored base, so `gh pr diff` returned already-landed
    # code and a full round confirmed 15 findings about it; and NEWER on PR #270,
    # where the stored base was the tip of `main` and named a commit the branch had
    # never contained. Recording the stored value as `merge_base` is what let both
    # rounds report a range nobody had reviewed.
    #
    # The diff is still `gh pr diff`, and that is deliberate rather than
    # unfinished: silently re-deriving the target from a locally-computed range
    # would swap one unannounced scope for another, and the load-bearing part of
    # #241 is that a mis-scoped round must not be SILENT. So the fork point is
    # recorded, the stored base is compared against it, and any disagreement is
    # said out loud in `config_notes` — where a reader can discount findings that
    # fall outside the true range.
    forked_at = _merge_base_now(gh_repo, base, head_sha)
    if forked_at is None and stored_base:
        notes.append(
            "the merge base could not be computed for this round, so `merge_base` "
            f"records GitHub's stored base for the PR ({stored_base[:8]}) instead "
            "— that field is not a merge base, so treat the recorded range as "
            "approximate")
    elif forked_at is None:
        notes.append(
            "the merge base could not be computed and GitHub stores no base commit "
            "for this PR, so this round records neither end of its base — a later "
            "staleness check has nothing to anchor against")
    else:
        merge_base = forked_at
        if stored_base and stored_base != forked_at:
            notes.append(
                f"the target may be MIS-SCOPED: this round's diff came from `gh pr "
                f"diff`, which GitHub builds against its stored base for the PR "
                f"({stored_base[:8]}), and that is not where the branch forked from "
                f"({forked_at[:8]} — `{base}...{head_sha[:8]}`). Findings may fall "
                f"outside the range this PR actually contributes; check any finding "
                f"against `git diff {forked_at[:8]}...{head_sha[:8]}` before acting "
                "on it. `merge_base` below records the fork point, not the stored "
                "base")

    # The head is read BEFORE the diff, and the order is load-bearing. The two are
    # separate requests, so a push that lands between them makes them disagree —
    # and this way round the recorded head is the OLDER of the two, so the next
    # round's increment starts at or before the last commit this round read. It
    # re-reads a little; it cannot skip anything. Read after the diff, the same
    # race would record a head ahead of what was reviewed, and the next round's
    # increment would begin after code no round had seen.
    try:
        diff = panel_core.sh(["gh", "pr", "diff", str(pr_number), "--repo", gh_repo])
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or "").strip().splitlines()
        sys.exit(f"panel: cannot fetch diff for PR #{pr_number} in {gh_repo}"
                 + (f" — {tail[-1][:160]}" if tail else ""))
    changed_lines = _diff_added_lines(diff)

    # ---- what this round REVIEWS. Round 1 reads the PR; a later round reads what
    # the fixer wrote since the last round read it, with the PR behind it as
    # context. Decided here, before budgets, because scope is what the budgets are
    # then spent on.
    prior = load_baseline(baseline or [],
                          {"repo": repo_name, "github": gh_repo, "pr": pr_number,
                           "round": round_no})
    want_scope = resolve_round_scope(scope, panel, notes)
    # `--since` wins over the baseline, and it is checked here rather than trusted:
    # the anchor is interpolated into a REST path, and a value carrying `..` or a
    # query string addresses a different endpoint — a fetch error where one of the
    # explained fallbacks belongs.
    if since and not _is_ref(since):
        notes.append(f"--since {since!r} is not a commit or a ref — it was ignored")
        since = ""
    anchor = since or prior.head_sha or ""
    # #278's dial, resolved here beside the scope it qualifies rather than inside
    # `decide`: a malformed value is a hard exit through `_refuse_value`, and the one
    # place a rules file may take a run down is where the run reads the rules file.
    review, scope_notes = ReviewScope.decide(
        want_scope, round_no, diff, (anchor, head_sha), gh_repo, base,
        None if since else prior.head_round, distant_merge_lines(panel, notes))
    notes.extend(scope_notes)

    # Diff budgets: panel-wide value, then each model's own override. Every
    # reviewer used to get the same 60k prefix regardless of its context window.
    # `notes` is already populated — the file-list warnings are built above the
    # skip branch so a skipped run carries them too. Deliberately NOT re-initialised
    # here: this line used to be `notes: list[str] = []`, and keeping it through the
    # merge with #82 would have silently discarded every file-list warning built
    # between there and here.
    #
    # Time-of-check/time-of-use: `headRefOid` was read from the PR metadata BEFORE
    # the diff was fetched, so a push landing in between leaves the payload naming
    # one commit while the reviewers read another — and the next round then
    # attributes its findings to a range that never produced the diff anyone
    # reviewed. Re-read straight after the diff fetch above, which NARROWS that
    # window but does not close it: the push could have landed either side of the
    # fetch, and nothing here can tell which. So the note reports the move rather
    # than claiming which commit produced the diff. The later commit is recorded
    # because it is the one the next round's fix range has to start from — and
    # "could not tell" (a None) leaves the earlier answer standing.
    moved_to = _head_sha_now(gh_repo, pr_number)
    if moved_to and moved_to != head_sha:
        notes.append(f"the PR head moved from {head_sha[:8]} to {moved_to[:8]} while this "
                     "round was running — the diff was fetched somewhere in that window and "
                     "which of the two produced it cannot be told from here; the later commit "
                     "is recorded, and provenance against this round is that much less certain")
        head_sha = moved_to
        # `merge_base` was computed against the head this round started with, and a
        # fork point is a fact about a PAIR of commits — move one end and the answer
        # can move with it. Leaving it would pair a re-stamped right end with a left
        # end computed for the commit it replaced, and the pair being replayable is
        # this release's whole claim. Worse, the common reason a head moves here is
        # a merge of the base branch INTO the PR (~1.8 integration merges per PR
        # landed on this repo, #80), which is exactly the case that moves it:
        # the stored range would then start before an integration merge its right
        # end contains, and it is a range no round ever reviewed.
        #
        # One extra call, on a path that fires rarely, and only when something has
        # already gone irregular. If it fails, the pair is not silently mismatched
        # — the note says which end is stale, because "unknown" and "stale" want
        # different treatment from whatever reads this later.
        moved_meta = _merge_base_now(gh_repo, base, head_sha)
        if moved_meta and moved_meta != merge_base:
            notes.append(f"the merge base moved with it, from {merge_base[:8] if merge_base else '?'} "
                         f"to {moved_meta[:8]} — both ends are re-read, so the recorded range is "
                         "the one that exists now rather than a pair straddling the push")
            merge_base = moved_meta
        elif moved_meta is None:
            # DROP it rather than keep the earlier head's answer (128-F12). Keeping
            # it stores a merge_base/head_sha pair that no programmatic consumer can
            # tell from a good one — the prose note below is not readable by #96,
            # which is the consumer this release exists to serve, and a
            # plausible-but-wrong range is worse than an absent one: the first is
            # acted on, the second is noticed. The board already treats a commit id
            # it will not store as null-plus-an-echo rather than as a best guess
            # (`merge_base_dropped`, app/api/reviews.py), and this is the same
            # judgement one layer up.
            #
            # Null is expressible today and costs no schema change, which is why it
            # is this rather than a new `merge_base_stale` field: adding a payload
            # field means adding it board-side too or it is silently dropped on
            # ingest (#93, #65), and that is a migration this round cannot carry.
            if merge_base is None:
                # Nothing was ever recorded, so "the one computed for the EARLIER
                # head" would name a commit that never existed (128-F11).
                notes.append("the merge base could not be read before or after the head moved, "
                             "so this round records neither end of its base — a later staleness "
                             "check has nothing to anchor against")
            else:
                notes.append(f"the merge base could not be re-read after the head moved, so the "
                             f"base end computed for the earlier head ({merge_base[:8]}) is "
                             "DROPPED rather than paired with the new head — the range is "
                             "recorded as unknown, not as one that was never reviewed")
            merge_base = None
    # The base end (#98), read here rather than above the skip branch: this is one
    # more API round trip and the skip path exists to be cheap, never fetches a
    # diff, and never reaches the board — so a base tip recorded there would have
    # no consumer to be worth the call. A skipped payload keeps `merge_base`,
    # which is free off the metadata already fetched, and leaves this null.
    #
    # No note when the two differ. `base_sha != merge_base` is the ordinary state
    # of every PR whose base gained a commit after it forked, so a warning there
    # would fire on almost every run and be trained away — the same reasoning
    # `unread_files` records for not warning about its own dedup. What the base's
    # movement MEANS is a verdict, and the verdict belongs to #96.
    base_sha = _base_tip_now(gh_repo, base)
    if base_sha is None:
        notes.append(f"the tip of base branch '{base}' could not be read, so this round "
                     "records what its diff was built from and not what the PR would be "
                     "merged into — a later staleness check has one end of the range only")
    panel_budget = diff_budget(panel, "max_diff_chars", DEFAULT_DIFF_BUDGET, notes)
    # Only for the reviewers actually running: a budget warning about a model
    # this run never asked for is noise, and a "truncated for antigravity" footnote
    # under a claude-only panel is a lie.
    #
    # "Actually running" means SELECTED *and* INSTALLED (#222): a seat whose CLI is
    # not on this box cannot be handed a diff, so a budget for it is the root of
    # four statements about a reviewer that never read a byte — the last of which,
    # a `truncated: True` record, `load_baseline` banked as a coverage gap the next
    # round inherited. `seat_installed`'s docstring in panel_core carries the whole
    # reasoning; it is filtered HERE rather than at each consumer so that a fifth
    # consumer added later inherits the fix instead of needing its own.
    #
    # Read ONCE per round rather than per consumer. `run_seat` asks the same
    # predicate again when the seat is dispatched, and two independently-timed PATH
    # reads can disagree; a snapshot is what makes the consumers below — the
    # budget, the argv clamp, the prompt, the payload — describe one host.
    #
    # This set is now the round's ONE answer to "which seats are here": a seat it
    # excludes is never dispatched, so `run_seat`'s own PATH read cannot contradict
    # it (see the dispatch loop). `adjudicate` still asks independently for
    # `claude`, which is a separate seat with a separate record, and its own gate
    # refuses it the same way.
    installed = {name for name in LLM_REVIEWERS if seat_installed(name)}
    budgets = {name: diff_budget(rev.get(name, {}), "max_diff_chars", panel_budget, notes)
               for name in LLM_REVIEWERS if name in selected and name in installed}
    # The judge is a seat on this box too: `adjudicate` runs it through the
    # `claude` CLI and refuses when that is absent, asking this same predicate. So
    # it gets no budget and no `config_notes` line there either — a "the judge saw
    # 60,000 of 177,872 chars" note about an adjudication that never happened is
    # the same lie as the reviewer footnote above.
    #
    # It is also NOT in `budgets`, and so is not weighed by the pre-flight verdict
    # below (#138). That is a boundary rather than an oversight, and `seat_ceilings`
    # states the argument: the verdict decides whether to dispatch the SEATS and
    # what to hand them, while `judge_max_diff_chars` says what adjudication is
    # worth. Counted there, that knob could refuse a round every reviewer could
    # read whole.
    judge_budget = (diff_budget(panel, "judge_max_diff_chars", panel_budget, notes)
                    if "claude" in installed else None)

    # ---- the pre-flight verdict (#138): is this round worth running, and read as
    # WHAT. Here and not earlier because it is measured against the seats' own
    # caps, which is what keeps it from being the default diff budget #49 refused;
    # here and not later because a refusal must cost nothing, and everything below
    # this — the CI read, the four seats, the judge — costs.
    #
    # Measured on `review.target`, NOT on `diff`, and under "pr" scope those are
    # the same string so round 1 is unaffected either way. Under increment scope
    # they are not: the target is the fix commit and `diff` is the whole PR, so
    # measuring the PR would refuse — or hand a manifest to — a round whose actual
    # material is a 3 KB increment, because of a size that round was never going to
    # send. Both questions this asks are about the thing being reviewed: "would a
    # seat read a useless fraction of it" and "is IT a move". A round 2 fix commit
    # is neither large nor move-shaped just because the PR it lands in is.
    #
    # It also means `preflight.shape.chars` is scope-dependent exactly as
    # `diff_chars` is — read `scope` beside it.
    #
    # **The CONTEXT tiers are deliberately not weighed with it, and this is where to
    # say why.** Under increment scope a seat's prompt also carries `review.near` and
    # `review.far`, so a round with a small increment and large context tiers is
    # handed more than the target measured here. Neither question this asks is about
    # that total. "Is IT a move" is plainly about the target. "Would a seat read a
    # useless fraction of it" is too, because the target is the tier that is never
    # cut while anything else is present (`ReviewScope.material`'s priority order):
    # losing context under a tight budget is the DESIGN of increment scope, is
    # labelled in the prompt, is reported in `config_notes`, and already vetoes a
    # confident stop through the `short_context` sentence below. Refusing a round
    # because the optional tiers behind its target are large would refuse the case
    # scoping exists to make cheap. The one place the total genuinely binds — argv,
    # which cannot carry an oversized prompt at all — is clamped separately against
    # `sendable` a few dozen lines down, and says so per seat.
    #
    # `notes` is passed so a junk threshold in `.harness-rules` is reported the way
    # every other bad config value is. The VERDICT itself is deliberately not a
    # config note: it rides in `payload["preflight"]`, which reaches the board,
    # where `config_notes` does not — and it gets its own warning above the
    # findings, which is a better place for it than a "⚠️ config:" line. Written
    # into both, one report carried the same three sentences twice.
    #
    # What this round set out to review, captured BEFORE the manifest can replace
    # the material: a manifest travels as a whole-target ("pr") scope by
    # construction — there are no tiers to compose — so substituting it flips
    # `review.scope`, and the inherited coverage vetoes are gated on that flag. A
    # move-shaped round 2 would therefore have skipped them and been free to stop
    # `confident: True` over gaps earlier rounds left, because its material stopped
    # looking scoped. The round's SCOPE and the shape of its material are two
    # different facts, and only the second one changed. Captured here rather than
    # after the refusal branch so that branch can record it too.
    target_scope = review.scope
    # `gate` is the mergeability precondition decided at the top of this function
    # (#271), and it is handed to the verdict rather than acted on where it was
    # computed so that a precondition refusal and a size refusal are ONE path: the
    # payload, `skip_reason`, the per-seat `ran: false` rows, the board record and
    # `--force` all already exist below, and a second refusal branch beside them is
    # how the checks in #96 came to disagree with each other.
    # `installed` is HANDED to the verdict rather than left to be re-derived, and
    # it is the round's own snapshot from a few dozen lines up. `seat_ceilings`
    # resolves the predicate in its body when it is given none, so the verdict was
    # taking a SECOND, independently-timed PATH reading — the exact thing the
    # snapshot's own comment above says it exists to prevent ("two independently
    # timed PATH reads can disagree; a snapshot is what makes the consumers below
    # describe one host"). The verdict was the one consumer still outside it.
    #
    # `.__contains__` and not the set, because the parameter is a PREDICATE that
    # `seat_ceilings` calls per seat. Being a bound method it is also always truthy,
    # which matters for the `installed or seat_installed` fallback there: an empty
    # set is falsy and would hand a host carrying no seat at all straight back to
    # the PATH read. That case cannot arise today — the predicate is only ever asked
    # about names in `budgets`, which is a subset of this set, so an empty snapshot
    # means an empty `budgets` and nothing to ask about — which is why there is no
    # test for it. It is spelled the safe way because the cost is a dunder.
    pre = preflight(review.target, budgets, panel, notes, forced=force, gate=gate,
                    gate_overridable=gate_overridable,
                    installed=installed.__contains__)
    if pre.refused:
        # The CI gate, read on a round that dispatches nobody. It is one API call,
        # is not defeated by diff size, and costs no seat's budget — and a refusal
        # that lost it told `/panel-review-pr` to stop the cycle with nothing said
        # about a red build. "A refusal must cost nothing" is about the seats and
        # the judge, which is where the minutes and the tokens are. Sonar is NOT
        # read: it is a selected panel MEMBER with a `ran: false` row below, and
        # dispatching a member while telling the board none ran is the exact
        # inconsistency this path is built to avoid. `refusal_report` states that
        # gate was not evaluated so its absence cannot read as a pass.
        ci_status, ci_failing, ci_skip = review_ci(gh_repo, pr_number)
        # `review_ci` returns its skip reason ALREADY LABELLED — `ci: TimeoutExpired`
        # — because the ordinary path puts that string straight into
        # `result.skipped`, which is parsed board-side as "<reviewer>: <reason>".
        # Neither consumer on this path parses it that way, and both were adding a
        # second label to the first: `config_notes` renders "⚠️ config: ci:
        # TimeoutExpired", filing a CI outage as a config key called `ci`, and
        # `_ci_line` renders "could NOT be read (ci: timed out)". So the bare reason
        # is what travels here and each renderer says what it is for itself.
        ci_why = (ci_skip or "").removeprefix("ci: ")
        if ci_skip:
            # `config_notes` and not `skipped`, unlike the ordinary path's
            # `result.skipped`: there is no `PanelResult` here, and a `ci:` entry
            # in `skipped` would be filed as a reviewer named "ci" that failed to
            # run, in the table that answers which reviewer finds the real issues.
            notes.append(f"CI could not be read — {ci_why}")
        report = refusal_report(repo_name, pr_number, title, base, pre,
                                ci_status, tuple(ci_failing), ci_why)
        # One short sentence per seat: the per-seat answer to "why is this row
        # empty". The whole reason is in `skip_reason` and `preflight.reason`.
        #
        # `pre.measured`/`pre.cap_unit` rather than `pre.shape.chars` and the word
        # "chars": the ceiling that refused this round may be antigravity's argv
        # limit, which is in bytes, and a per-seat skip reason that states a
        # character count against a byte ceiling disagrees with `skip_reason` in the
        # same payload. See `panel_preflight.Ceiling`.
        #
        # A GATE refusal names no ceiling: nothing was measured against one, and
        # `pre.cap` can legitimately be None there, so the size formatting would
        # raise from inside the payload build. The seat's row says which question
        # refused it, which is what "why is this row empty" wants.
        refused_by = ("not dispatched — the panel refused this round before any "
                      "seat, on a precondition the diff's size has nothing to do "
                      "with" if pre.gate else
                      f"not dispatched — the panel refused this round "
                      f"({pre.measured:,} {pre.cap_unit} against {pre.cap:,})")
        # The refusal's one phase, closed HERE rather than above the CI read. A
        # refusal is the cheap path by design, and this is what checks that claim —
        # so it has to contain the gh call the refusal still makes and the report
        # it still builds. Closed before either, `round_ms` would sum to a phase
        # that excluded the only two things this path does. It closes before the
        # payload is assembled, since the payload carries the timing; the print and
        # the board record now happen after that (#284), and are the cheap end.
        clock.mark("setup")
        refuse_payload = {
            **_payload_defaults(),
            "rules": rules_record(cfg),
            "repo": repo_name, "github": gh_repo, "pr": pr_number,
            "title": title, "base": base,
            # Same three ends the skip path records, and for the same reasons: a
            # refused round still moved the head, and round r+1 has to be able to
            # anchor its increment and its fix range somewhere.
            "head_sha": head_sha, "merge_base": merge_base, "base_sha": base_sha,
            # What the round was GOING to review, recorded for the same reason
            # `preflight.shape` records what it measured: a refusal under
            # `--scope increment --since <sha>` otherwise publishes the field
            # defaults, so nothing distinguishes it from a refused whole-PR round.
            # `load_baseline` reads `payload.get("scope") or "pr"`, which is
            # harmless here only because `reviewers_ran == []` routes a refused
            # round to `unread_rounds` before scope matters — a coupling nothing
            # states and nothing enforces.
            "scope": target_scope,
            "since_sha": review.since or None,
            # The one HARD gate a refusal can still report. `sonar_gate` stays at
            # its default: Sonar is a member and no member ran, which the notice
            # says out loud rather than leaving the default to be read as a pass.
            "ci_status": ci_status,
            "ci_failing": ci_failing,
            "changed_lines": changed,
            "changed_files": changed_files,
            "changed_files_total": changed_files_total,
            "pr_state": pr_state, "is_draft": is_draft,
            "config_notes": notes + prior.problems,
            "round": round_no,
            "cycle": prior.cycle,
            "prior_rounds": len(prior.rounds),
            "prior_findings": len(prior.keys),
            "fix_range_source": None,
            "fix_range_rebuilt": None,
            "provenance_counts": ({b: 0 for b in PROVENANCE} if prior.rounds else {}),
            "recurrence_counts": ({b: 0 for b in RECURRENCE} if prior.rounds else {}),
            "premise_counts": ({b: 0 for b in (*PREMISE_VERDICTS, "not-said")}
                               if prior.rounds else {}),
            "reviewers_selected": sorted(selected),
            "reviewers_override": override_note,
            # NOT optional, and the reason is this whole feature's own failure mode
            # arriving in the board's statistics. `_scorecards` builds a row for
            # every name in `reviewers_selected`, and with no `reviewers` block "a
            # member is assumed to have run unless it appears in `skipped`" —
            # deliberately, so that a quiet reviewer is not filed as broken. A
            # refusal sending `reviewers_selected` and nothing else would therefore
            # record every configured seat as having run and found nothing: a
            # refusal read as a clean review, per reviewer, in the very table that
            # answers "which reviewer finds the real issues". The title-pattern
            # skip dodges this by never being recorded; this path is recorded on
            # purpose, so it has to tell the truth per seat.
            #
            # Both shapes, because there are two consumers reading two keys:
            # `reviewers` is the structured one, `skipped` is parsed as
            # "<name>: <reason>".
            "reviewers": {n: {"ran": False, "skip": f"{n}: {refused_by}"}
                          for n in sorted(selected)},
            "skipped": [f"{n}: {refused_by}" for n in sorted(selected)],
            # The budgets the verdict was measured against, so the refusal can be
            # checked rather than taken on trust — the reason names one seat's cap
            # and this is every seat's.
            "diff_budgets": {**budgets, "judge": judge_budget},
            # `diff_chars` stays 0: nothing was reviewed, and the PR's own size is
            # in `preflight.shape.chars` where it is a measurement rather than a
            # claim about coverage.
            "skip_reason": pre.reason,
            "preflight": pre.as_dict(),
            # Timed like any other exit (#192). A refusal is the cheap path by
            # design, and "cheap" is a claim this is the only thing that checks —
            # but the load-bearing half is `finished_at`: this payload is fed to
            # round r+1 as a `--baseline`, and without a finish here that round
            # measures its fix phase from whichever earlier round last recorded
            # one, silently spanning this one as well.
            "timing": panel_timing.timing_block(
                clock,
                panel_timing.fix_phase(clock.started_at,
                                       prior_finished_at=prior.finished_at,
                                       prior_round=prior.finished_round or prior.head_round,
                                       prior_head_sha=prior.head_sha,
                                       head_sha=head_sha,
                                       repo_path=cfg.get("path") or ""),
                measured_to="refusal"),
            "run_key": run_key,
        }
        if caps.stop:
            # #55's second acceptance criterion, and v2.15 is what serves it: a cap
            # stop and a convergence are already distinguishable on the board by
            # `stop_confident`, so this only has to SAY it. Set on the caps refusal
            # alone and not on the size or mergeability ones — those are "this
            # round could not usefully read the diff", which leaves the cycle open
            # and is not a stop; a ceiling ends the cycle whether or not the caller
            # believed it was driving one.
            #
            # `confident: False` is what stops it reading as convergence downstream
            # — `preland --require-earned-stop` HOLDs on it and the review queue
            # files it `unconverged` — which is exactly right: a PR that stopped
            # because the money ran out has not been reviewed to a conclusion.
            refuse_payload["round_stop"] = {
                "stop": True, "confident": False, "reason": pre.reason,
                "veto": [caps.refusal],
            }
            refuse_payload["stop_reason"] = pre.reason
        # RECORDED, unlike the title-pattern skip, and that is the difference
        # between the two paths rather than an inconsistency. A title skip says
        # "this PR was never worth a panel"; a refusal says "a panel was wanted
        # and this diff defeated it", which is exactly the observation the board
        # exists to accumulate — and the issue's own requirement, so that "no
        # review" can never be read later as "clean".
        #
        # Ahead of the write and ahead of the print, because a refusal the board
        # never saw has to say so in all three artefacts and not just in the one
        # nobody keeps (#284). `refusal_report` takes no notes list — it is not
        # the panel's report — so the line is appended to the notice in the same
        # `⚠️ config:` shape the reviewed report renders `config_notes` in.
        if record:
            missed = record_run(refuse_payload)
            if missed:
                refuse_payload["config_notes"].append(missed)
                report += f"\n  - ⚠️ config: {missed}"
        print(report, file=chatter)
        failed = write_payload(json_file, refuse_payload)
        if json_out:
            print(json.dumps(refuse_payload, indent=2))
        elif post:
            post_summary(gh_repo, pr_number, report)
        return finish(failed)
    if pre.verdict == "manifest":
        # The manifest REPLACES the material rather than adding a review mode:
        # everything downstream works on `review`, so the budgets, the truncation
        # measurement, the judge and the board record all keep working and all
        # measure the manifest — which is the thing that was actually sent. See
        # `ReviewScope.header`.
        #
        # `since`/`since_round` ride across. They are what `since_sha` is recorded
        # from and what the report quotes as the anchor; dropped, a scoped round
        # that read a manifest would publish `since_sha: null` and lose the record
        # of which commit its target was measured from.
        review = ReviewScope(scope="pr", diff=pre.manifest, round_no=round_no,
                             since=review.since, since_round=review.since_round,
                             header=MOVE_MANIFEST_HEADER)

    # Read BEFORE the seats are dispatched, because its result now travels in
    # their prompt (#91). It used to run concurrently with them and be collected
    # afterwards, which is why the panel could compute CI on every run and still
    # tell no reviewer about it. One `gh pr checks` against a round that takes
    # minutes is a couple of seconds of wall-clock for a fact that refutes a whole
    # class of finding, so the overlap is not worth keeping.
    #
    # And a PENDING build gets a bounded chance to finish BEFORE the seats are
    # dispatched, which is the whole of #501. A round takes 20-40 minutes here and
    # a build about four and a half, so the old behaviour reliably told reviewers
    # "CI is still running" about a build that finished during the round — and a
    # reviewer that is told so declares "could not assess: CI result is unknown",
    # which `coverage_veto` counts and `round_stop` turns into `confident: false`.
    # Measured fleet-wide over five days: 19 rounds, 9 of them PENDING, and
    # `stop_confident` true on NONE of them.
    #
    # The cause is removed rather than the symptom filtered, because the symptom
    # cannot be filtered honestly: the veto is a reviewer's free-form prose, and
    # `coverage_veto`'s standing rule is that exemptions come off recorded state
    # and never off the wording of a declaration. See `review_ci_settled`.
    ci_status, ci_failing, ci_skip, ci_waited = review_ci_settled(
        gh_repo, pr_number, read=review_ci)
    if ci_skip:
        result.skipped.append(ci_skip)
    if ci_waited:
        # Said out loud: a round that sat for four minutes should account for the
        # time rather than look slow for no reason, and a wait that ran out is the
        # case where the veto below is still correct.
        settled = "settled" if ci_status != "PENDING" else "did not settle"
        notes.append(
            f"waited {ci_waited:.0f}s for CI before dispatching the seats — it "
            f"{settled} ({ci_status}). A reviewer told the build is still running "
            f"declares it as a gap it cannot assess, and that costs the round its "
            f"confident stop (#501)")
    # #548. `none` is the state the wait above cannot help: there is nothing to wait
    # for, so the channel #501 built is simply empty and every seat is told "no run
    # exists" about a repo that may have a perfectly good suite in it. If this repo
    # has declared one, run it once — here, before the seats, so that one execution
    # is shared by all of them — and let the answer travel down the same channel in
    # states of its own that can never read as CI.
    #
    # NOT on the refusal path above, which reads CI and dispatches nobody: there is
    # no seat to raise a floor under, and spending a suite's wall clock on a round
    # that reviews nothing contradicts "a refusal must cost nothing".
    local_record = None
    if local_cmds and ci_status in LOCAL_SUITE_WHEN:
        instead_of = ci_status
        (local_status, local_failing, local_why, local_output,
         local_secs) = review_local_suite(
            local_cmds, str(cfg.get("path") or ""), head_sha, timeout=local_timeout)
        # Recorded whatever happened, including the case where nothing ran: "this
        # repo declares a suite and the checkout was in no state to run it" is the
        # fact a reader of a `none` round now needs, and it is invisible in
        # `ci_status`, which is unchanged in exactly that case.
        local_record = {"commands": list(local_cmds), "status": local_status or None,
                        "failed": list(local_failing), "seconds": local_secs,
                        "why": local_why or None, "output": local_output or None,
                        "instead_of": instead_of, "timeout": local_timeout}
        if local_status:
            ci_status, ci_failing = local_status, local_failing
            # The seats get the harness's own sentence for a run that told us
            # nothing, and NEVER the gist of a failing command's output: `ci_brief`
            # renders `skip` into four reviewer prompts and the judge's, and that
            # text is produced by code from the PR under review.
            ci_skip = local_why if local_status == LOCAL_UNREAD else None
            notes.append(
                f"GitHub CI reported `{instead_of}` for this commit, so the repo's own "
                f"suite was run here instead (`{'`, `'.join(local_cmds)}`): "
                f"{local_status} in {local_secs:.0f}s"
                + (f" — {local_why}" if local_why else "")
                + ". A local run is weaker evidence than a green CI run and buys no "
                  "merge: the gate still reads GitHub (#548)")
            # The command's own OUTPUT goes here and nowhere else in this block.
            # `config_notes` is published as a public PR comment by `--post`, and a
            # failing test prints whatever it was holding — which on this fleet has
            # included a `DATABASE_URL` with a password in it. The operator running
            # the round sees it, the payload records it, and neither the PR nor a
            # reviewer's prompt is where it lands.
            if local_output:
                print(f"! local suite: {local_output}", file=chatter)
        else:
            # The setting is live and did nothing, which must not be silent — a
            # repo that configured a suite and never sees it run would otherwise
            # have to read this source to find out why.
            notes.append(
                f"`review_panel.local_suite` is set (`{'`, `'.join(local_cmds)}`) and "
                f"was NOT run: {local_why}. CI still reports `{instead_of}` (#548)")

    ci_text = ci_brief(ci_status, ci_failing, ci_skip)

    # A manifest round asks a different question, so it sends a different brief —
    # and only the brief differs. Both templates take the same `.format` keys and
    # end with the same reply contract (`_FINDINGS_ENVELOPE`), which is what lets
    # one closure serve both and what keeps `SCHEMA_ECHOES` able to recognise
    # either prompt's own example rather than filing it as a finding. `{code}`
    # lives in that shared tail too (v2.51), so a slot added to one template can
    # never be missing from the other.
    # `reviewer_brief` fills the scope slot from `review_panel.reviewer_scope` (#165):
    # `diff` asks for defects in the change and routes anything outside it to an
    # observation, `repo` is the pre-#165 wording verbatim. The manifest brief takes
    # no scope — it is already the narrowest question this panel asks, and its whole
    # instruction is "do not review the moved code".
    # TWO briefs, not one, and the difference is only `repo` scope: that paragraph
    # says "search the codebase, don't just review the diff", which a seat with no
    # tools cannot do and — since #458 put NO_TOOLS_BRIEF in the same prompt — is
    # told twice, in opposite directions. RELATED_CODE_SLOT exists because a bullet
    # contradicting its own paragraph "is the contradiction a model resolves
    # whichever way it likes", and on antigravity resolving it the wrong way is
    # fatal rather than merely wasteful (#459).
    brief = (MOVE_MANIFEST_PROMPT if pre.verdict == "manifest"
             else reviewer_brief(dials.reviewer_scope))
    brief_blind = (MOVE_MANIFEST_PROMPT if pre.verdict == "manifest"
                   else reviewer_brief(dials.reviewer_scope, reads_code=False))

    def prompt_for(budget: int | None, reads_code: bool = False) -> str:
        # `reads_code` defaults False so the one-argument callers keep working —
        # `fit_argv_budget` takes this as a single-arg render, and antigravity is
        # never a code-reading seat, so that path is unaffected by construction.
        # The slot is EITHER brief, never empty: a seat that gets the tree is told
        # to use it, and a seat that does not is told there is nothing to look at
        # and what to do instead (#458). Empty was the hole — it left the seats
        # with no tool flag to work out their own situation, and the way they work
        # it out is by trying.
        #
        # Inside the render on purpose, so `fit_argv_budget` counts it: the ceiling
        # applies to the PROMPT, and antigravity is both the seat this text is for
        # and the one seat the kernel can veto. Adding it in `antigravity_args`
        # would put ~1,100 bytes past the clamp that just measured the prompt.
        return (brief if reads_code else brief_blind).format(
                            n=pr_number, repo=gh_repo, base=base,
                            ci=ci_text, diff=review.material(budget)[0],
                            code=CODE_ACCESS_BRIEF if reads_code else NO_TOOLS_BRIEF)

    # `agy` is the only reviewer whose prompt must travel in argv, so it is the
    # only one the kernel can veto. Clamp it to what execve will carry and say
    # so — the alternative, honouring the number and dying at exec, is how a
    # panel came to report "LLM reviewers ran: none" as a clean review.
    #
    # It is also the only seat an UNCAPPED budget can still cut, which is why the
    # clamp starts from the material's own length when there is no budget: "no
    # cap" means "as much as this machine can hand over", and on this one seat
    # that is a smaller number than on the others. The note says so in chars of
    # the material rather than in config terms, since there is no config value to
    # blame.
    #
    # `sendable` is that length — everything this round would hand a reviewer,
    # target and context together, which under increment scope is not the PR's
    # length. Starting from the PR's would tell antigravity it had been cut on a
    # round whose material fits whole.
    sendable = len(review.target) + len(review.near) + len(review.far)
    #: Seats whose budget the KERNEL cut, rather than a number somebody typed.
    #: Half of the coverage exemption; the other half is whether the cut actually
    #: cost the seat any of the target, which only `truncated_for` below can
    #: measure — see `argv_clamp` for why a comparison against the target's length
    #: is not the same question under increment scope.
    kernel_cut: set[str] = set()
    if "antigravity" in budgets:
        asked = budgets["antigravity"]
        want = sendable if asked is None else asked
        fitted, cut_by_kernel = argv_clamp(prompt_for, sendable, asked)
        if fitted < want:
            notes.append(
                f"antigravity gets {fitted:,} of {sendable:,} diff chars — its prompt "
                f"travels in argv and the kernel caps one element at "
                f"{ARGV_PROMPT_MAX_BYTES:,} bytes. It is the only reviewer with no way "
                "to read a prompt off stdin.")
            budgets["antigravity"] = fitted
        if cut_by_kernel:
            kernel_cut.add("antigravity")

    # Truncation is measured against the review TARGET, not against everything
    # sent. Under increment scope losing context is the design — that is what the
    # priority order in `ReviewScope.material` is for, and a reviewer short of
    # context is told so in the prompt and can declare it. Losing the target is
    # the thing that must never pass silently, because a reviewer handed a prefix
    # of the thing it is reviewing cannot see what it was not given. Counting a
    # trimmed context tier here would make `truncated` fire on almost every
    # increment round and stop meaning anything on the round where it matters.
    #
    # Measured by COMPOSING each reviewer's material rather than by comparing its
    # budget against the target's length. The two are not the same number under
    # increment scope: the budget also has to pay for the brief and the section
    # headers, so a budget a little over the target's size still cuts it, and a
    # comparison against the raw budget would report that as untruncated.
    composed = {n: review.material(b) for n, b in budgets.items()}
    sent = {n: composed[n][1:] for n in composed}
    truncated_for = {n: b for n, b in budgets.items()
                     if sent[n][0] < len(review.target)}
    truncated = bool(truncated_for)
    # The coverage exemption, assembled from both halves: truncated by MEASUREMENT
    # (composed above, so the brief and headers are accounted for) and cut by the
    # kernel rather than by a config budget. A subset of `truncated_for` by
    # construction, which is what keeps the report footnote and the veto agreeing
    # about the same seats.
    argv_capped = {n for n in truncated_for if n in kernel_cut}
    # A budget below the scoped frame's OWN size cannot be honoured. The brief and
    # the section headers are over a kilobyte and they are what makes the target
    # legible as the target; cutting them to fit would hand the reviewer an
    # unlabelled increment, which is worse than overshooting. So the prompt runs
    # over — and says so here, because "the budget buys the whole PROMPT" is the
    # contract everywhere else, and a silent overshoot in exactly the regime where
    # a small budget was set to protect a small context window is the one place
    # that contract has to be visible when it cannot be kept.
    over = [(n, len(composed[n][0]), b) for n, b in sorted(budgets.items())
            if b is not None and len(composed[n][0]) > b]
    if review.scope == "increment" and over:
        notes.append(
            "the scoped prompt does not fit the budget it was given for "
            + ", ".join(f"{n} ({got:,} chars against {b:,})" for n, got, b in over)
            + " — a scoped prompt's brief and section headers are over a kilobyte and "
              "cannot be cut, so a budget below them buys no diff at all and is still "
              "exceeded")
    # Context loss is still REPORTED, just not as truncation. Without this the
    # saving is invisible in one direction and so is its cost: nothing else in
    # the payload distinguishes "the whole PR fitted behind the increment" from
    # "the increment used the entire budget and the reviewer saw no context".
    short_context = sorted(n for n in budgets
                           if n not in truncated_for
                           and sent[n][1] < len(review.near) + len(review.far))
    if review.scope == "increment" and short_context:
        notes.append(
            f"{', '.join(short_context)} got the whole target and only part of the PR "
            f"context ({sendable:,} chars of material, budget cut it) — expect "
            "`could_not_assess` entries about code outside the fix commit")
    # Increment scope always shrinks the review TARGET; it does not always shrink
    # the bill. The near tier is the anchor-era version of every file the fix
    # touched, so a fix spread across the files that carry most of the PR leaves
    # little to leave out. That is the price of reading the seam properly and it
    # is worth paying — but it is a cost, and an uncapped run should not discover
    # it from an invoice.
    #
    # The condition MEASURES that ("the near tier is most of the context") rather
    # than restating the arithmetic. It used to be `sendable > len(diff)`, which
    # was true on every scoped round — near and far were then a partition of the
    # whole PR, so the material was always the PR plus the target — and it fired
    # on a one-file fix in a fifty-file PR with a reason that was plainly false of
    # it.
    if review.scope == "increment" and len(review.near) > len(review.far):
        notes.append(
            f"scoping this round cut the review target to {len(review.target):,} chars "
            f"from the PR's {len(diff):,}, but the fix touches the files that carry most "
            f"of it: {len(review.near):,} of {len(review.near) + len(review.far):,} "
            f"context chars are the near tier, and {sendable:,} chars go out in all. The "
            "reviewer's attention is narrower; the token bill is not")

    result = PanelResult()
    # Resolved ONCE, so the label in the report cannot drift from the model that
    # actually ran (the fallbacks live here, not in two places). Effort is a
    # knob codex, pi and antigravity share (spelled differently on each CLI, and
    # over a different scale — see EFFORTS); claude takes its own default reasoning.
    models = {n: rev.get(n, {}).get("model", SEAT_MODEL_DEFAULTS.get(n, ""))
              for n in LLM_REVIEWERS}
    efforts = {n: rev.get(n, {}).get("effort", "") for n in EFFORTS}
    # No precomputed `labels` map: the label is not a property of the CONFIG any
    # more. A seat that could not use its pins reviewed on something else (#215),
    # so the only place the label can be built is where the run that earned it is
    # in hand — `seat_label(…, got)`, below. The map that used to be here was left
    # behind by that change with nothing reading it, which is a copy of the report's
    # header free to drift out of agreement with the header.

    # May the seats read the PR's code (#113)? A per-repo setting, ON by default,
    # and the seats that can actually use it are `SEAT_READS_CODE` — three of the
    # four vendors cannot express "read but do not execute", which is recorded
    # there rather than here. `--no-code-access` turns it off for one run, the
    # same shape as `--reviewers`: a switch this file honours over the config.
    want_code = code_access_wanted(panel, no_code_access, notes)
    # Read even when code access is off, so a misconfigured value is reported on
    # the round that carries it rather than staying silent until someone turns
    # access on months later and wonders why the cap is not applying.
    budget_usd = code_budget(panel, notes)
    #: The seats that both were ASKED for and could use it. Computed before the
    #: fetch so a repo whose only enabled seats are code-blind ones pays no
    #: download at all — the tree would be built and then handed to nobody.
    code_seats = sorted(n for n in selected if n in SEAT_READS_CODE)
    code_tree: Path | None = None
    stripped: list[str] = []
    #: Where the round's single copy of the tree lives, or None when nothing needed
    #: one. Removed after the seats have finished copying out of it — see the
    #: cleanup below the executor, which is why this is a plain mkdtemp rather than
    #: a `with`: the tree has to outlive the dispatch and die before the report.
    code_dir: Path | None = None
    if want_code and code_seats:
        # One fetch and one strip for the whole round, copied per seat by
        # `seat_checkout`. Doing it per seat would download the same tarball up to
        # four times and give the strip four chances to differ.
        code_dir = Path(tempfile.mkdtemp(prefix="panel-tree-"))
        code_tree, problem = fetch_pr_tree(gh_repo, meta["headRefOid"], code_dir)
        if problem:
            # A note, not a failure: a round that reviews from the diff is the OFF
            # posture, which works, and every seat records itself as blind so the
            # coverage veto reads the round correctly without being told twice.
            notes.append(f"{problem} — the seats review from the diff alone this round")
            code_tree = None
        else:
            try:
                stripped = strip_convention_files(code_tree)
            except OSError as e:
                # The strip is not optional. A tree that keeps its instruction
                # files is the injection channel this whole design turns off to,
                # so a strip that cannot finish means no seat gets the tree.
                notes.append(f"a vendor instruction file in the PR's tree could not be "
                             f"removed ({e}) — no seat was given the code, because a "
                             "checkout that keeps them can instruct its own reviewer")
                code_tree = None
    elif want_code and not code_seats:
        notes.append("`reviewer_code_access` is on, but no seat on this panel can use "
                     "it — see SEAT_READS_CODE; only claude can be given read tools "
                     "without also being given a shell")
    if stripped:
        # Said out loud, per round. A silent strip makes a PR that shipped an
        # `AGENTS.md` indistinguishable from one that did not, on the single axis
        # where the difference is worth knowing.
        notes.append(f"removed {len(stripped)} vendor instruction file(s) from the "
                     f"reviewers' checkout: {', '.join(stripped[:8])}"
                     + (f" and {len(stripped) - 8} more" if len(stripped) > 8 else ""))

    # Everything above — the PR read, the diff fetch, the scope decision, the
    # pre-flight verdict, the CI read and the code-tree download — closes here as
    # one phase. It is the part of a round that costs no vendor call and was
    # therefore assumed to cost no time; `setup` is what says whether that is true
    # on this repo.
    clock.mark("setup")
    tasks = {}
    with ThreadPoolExecutor(max_workers=len(ALL_REVIEWERS) + 1) as ex:
        # Every selected LLM reviewer runs — no de-minimis gate. If we asked for
        # the panel, we want each vendor's eyes regardless of diff size.
        for name in LLM_REVIEWERS:
            if name in selected:
                # The prompt differs per seat now: a seat with the tree is TOLD so,
                # because the default frame is "here is a diff" and a reviewer
                # following it faithfully declares gaps it could have opened a file
                # to close. See CODE_ACCESS_BRIEF.
                reads = code_tree is not None and name in SEAT_READS_CODE
                # A seat this box cannot run gets NO prompt (#222). Every SELECTED
                # seat is still dispatched, because `run_seat` is the single
                # authority on absence and is not only a PATH check — it answers a
                # typo'd reasoning effort as the config error it is, before looking
                # for the binary. But `budgets` has no entry for an absent seat, and
                # `prompt_for(None, …)` means "uncapped", so rendering one would
                # compose the entire diff — per absent seat, per round — for
                # `run_seat` to discard a moment later.
                prompt = (prompt_for(budgets[name], reads) if name in budgets
                          else "")
                tasks[name] = ex.submit(review_llm, name, models[name], prompt,
                                        efforts.get(name, ""),
                                        code_tree=code_tree,
                                        budget_usd=budget_usd if reads else None)
        sonar_future = None
        sonar_filed = False
        if "sonarqube" in selected:
            sonar_future = ex.submit(
                review_sonarqube, rev.get("sonarqube", {}),
                {"number": pr_number, "base": base,
                 "head": meta["headRefName"], "head_sha": meta["headRefOid"]},
                changed_lines, cfg["path"])

        # Observe the seats landing before collecting them in submission order
        # (#192). Sonar is watched with the rest: it is dispatched into the same
        # executor and its finish is part of the same join, so leaving it out
        # would attribute a round Sonar held to whichever LLM seat was slowest.
        # `watch` never reads a result, so the loop below still raises, skips and
        # records exactly as it did — see its docstring.
        panel_timing.watch(clock,
                           {**tasks, **({"sonarqube": sonar_future} if sonar_future
                                        else {})},
                           echo=chatter)

        llm_findings: list[Finding] = []
        ran_llm: list[str] = []
        # The same seats as `ran_llm`, under their BARE names. `ran_llm` holds
        # display labels (`claude (opus)`) for the report, and everything keyed by
        # reviewer — `budgets`, `models`, `efforts` — is keyed by the bare name.
        # Looking one up with the other returns None for every seat, silently.
        ran_names: list[str] = []
        llm_skipped: list[str] = []
        # Which brain each member actually used. Findings carry the bare vendor
        # name for attribution, which is the right grain for a report and the
        # wrong one for a record: "codex found 9 issues" means nothing six weeks
        # later without the model and effort behind it, and those drift (a repo
        # repins, a slug retires, --reviewers hand-picks a set).
        reviewer_meta: dict[str, dict] = {}
        for name, fut in tasks.items():
            got = fut.result()
            reviewer_meta[name] = {
                "model": models[name] or None,
                "effort": efforts.get(name) or None,
                "ran": not got.skip,
                "skip": got.skip,
                # Reconciled against `got.absent`, not read straight off `budgets`
                # (225-R3-F05). `budgets` is decided before dispatch and `run_seat`
                # decides absence after it, so on the one round where those two
                # disagree the payload would otherwise carry a real budget beside
                # `absent: true` — the contradictory pairing #222 exists to remove,
                # written by the fix meant to have removed it. Whatever happened,
                # happened: a seat the run found absent had no budget and read no
                # prefix, and both fields say so.
                #
                # None for a seat this box cannot run (#222) — it had no budget,
                # rather than a budget it failed to spend. `truncated` below is
                # keyed off `truncated_for`, which is built from the same dict, so
                # what the pair now guarantees is that a null budget can NEVER sit
                # beside a `truncated: True` — the pairing that made a round look
                # cut when nothing that ran was.
                #
                # That is the whole of it, and deliberately less than it looks: an
                # INSTALLED seat with no `max_diff_chars` configured records the
                # same null beside the same false, so the pair does not tell an
                # absent seat from an uncapped one. `absent` below is the field
                # that carries that distinction, and it is the one `coverage_veto`
                # and `load_baseline` read for exactly this reason.
                "max_diff_chars": None if got.absent else budgets.get(name),
                # The mechanical half of "did this reviewer see the whole thing":
                # checked against the budget rather than asked for, because the
                # one thing a truncated reviewer cannot notice is the truncation.
                "truncated": not got.absent and name in truncated_for,
                # WHY it was truncated, where the answer is the kernel rather than
                # a number in a config file. Same shape of fact as `absent` and
                # treated the same way by coverage_veto.
                "argv_capped": name in argv_capped,
                "duration_ms": got.duration_ms,
                "could_not_assess": got.could_not_assess,
                "unstructured": got.unstructured,
                # A fact about the HOST rather than about the round — see
                # coverage_veto, which is the one consumer that treats it
                # differently from every other way of not running.
                "absent": got.absent,
                # Which pin this host could not serve, when the seat reviewed on
                # the CLI default instead (#215). In the payload as well as the
                # header because the board is where "is the expensive tier worth
                # it" gets answered from accumulated runs, and a run whose model
                # was substituted must not be averaged in as the pinned one.
                "model_unavailable": got.model_unavailable or None,
                "effort_unsupported": got.effort_unsupported or None,
                # A fact about the panel's DESIGN rather than about the round: an
                # empty sandbox and no file tools, so this seat's declarations
                # about code outside the diff are constants. coverage_veto is
                # again the consumer that cares.
                "code_blind": got.code_blind,
                # Spread, not nested: a member whose usage could not be read
                # contributes no keys at all, so the board stores nulls and
                # renders "not recorded" — rather than a zero it would average in
                # as though the reviewer had cost nothing.
                **(got.usage or {}),
            }
            if got.skip:
                result.skipped.append(got.skip)
                llm_skipped.append(got.skip)
            else:
                # The label the seat EARNED, not the one it was configured with. A
                # seat that fell back to the CLI default (#215) reviewed on a
                # different model than `.harness-rules` names, and printing the pin
                # here would put a model in the record that never ran — the exact
                # attributability the pins exist to protect, broken in the
                # direction that looks correct.
                ran_llm.append(seat_label(name, models[name],
                                          efforts.get(name, ""), got))
                ran_names.append(name)
                llm_findings.extend(got.findings)
        if sonar_future:
            gate, hard, soft, skip = sonar_future.result()
            result.sonar_gate = gate
            reviewer_meta["sonarqube"] = {"ran": gate != "skipped", "skip": skip}
            # The 4th value is a skip reason ONLY when the reviewer didn't run
            # (gate == "skipped"); otherwise it's a caveat about a run that DID
            # produce findings — a degraded base branch, files it couldn't read.
            # Branching on its mere presence would drop those findings on the
            # floor and report the reviewer as skipped, which is the silent
            # zero this whole path exists to avoid.
            if skip:
                result.skipped.append(skip)
            if gate != "skipped":
                # PR-scanned issues are the hard gate; base-branch fallback
                # issues are soft — judged on merits alongside the LLM reviewers.
                result.sonar_findings = hard
                llm_findings.extend(soft)
                # Recorded HERE, where it is a fact rather than an inference: the
                # consensus count below needs to know whether sonarqube put
                # anything into the population the judge clusters, and only this
                # branch can. See `filers`.
                sonar_filed = bool(soft)

    # The executor has joined, so every seat has finished copying out of the tree
    # and nothing reads it again. Removed HERE rather than at the end of `run`
    # because a PR's checkout is the largest thing this process holds and the
    # report, the judge and the board write-up all still have to run; and rather
    # than in a `with`, because the reviewers are dispatched inside another block
    # and nesting a third would re-indent the whole panel. `ignore_errors`, since a
    # tree that will not delete is a disk problem and not a reason to lose a review
    # that has already happened — the directory is under the system temp root and
    # the next reboot takes it.
    #: Whether a tree was actually built and handed out, recorded BEFORE the
    #: directory goes away. `code_tree` stays a valid Path object after the rmtree,
    #: pointing at nothing, so a later reader of it would be asking a question the
    #: variable can no longer answer honestly.
    code_tree_used = code_tree is not None
    # The seat phase: dispatch to join. Every seat's own finish offset was recorded
    # inside it, so `seats` minus the second-slowest of those is the span the round
    # spent on one process with every other seat finished — `gated_ms`, the number
    # #192's "parallel but gated on its slowest seat" claim was missing.
    clock.mark("seats")

    # Pre-cluster as a hint, then let the master MERGE the duplicates and rule on
    # each issue in one step (no consensus gate). Dedup cannot happen upstream of
    # the judge without discarding what the other reviewers said — see adjudicate.
    clusters = cluster_findings(llm_findings)
    coverage = {n: m.get("could_not_assess") or [] for n, m in reviewer_meta.items()}
    # The judge reads the same material the reviewers did, composed the same way.
    # Handing it the whole PR while the panel reviewed an increment would put the
    # adjudicator and the parties in front of different evidence — it would rule
    # "not in the diff" on a finding whose diff it was looking at a different
    # version of, and it would do so with the authority of the final call.
    # `budget=None`, because the material arrives already fitted: composing to the
    # judge's budget and THEN cutting to it again would trim the tail a second
    # time, through the "[cut: …]" marker that says how much is missing. Nothing
    # else is lost by not passing it — `adjudicate` only ever used `budget` to
    # slice the diff it was handed.
    #
    # What the judge was actually given is kept, not discarded: `judge_budget` can
    # cut the judge's copy at a different point from every reviewer's, and until
    # this was measured nothing in the round reported or vetoed on it. A judge
    # short of the material dismisses a finding whose evidence sat in the part it
    # did not get, and the round records that as convergence.
    judge_text, judge_target, judge_context = review.judge_material(judge_budget)
    judge_gaps: list[str] = []
    if judge_target < len(review.target):
        judge_gaps.append(
            f"the judge ruled on {judge_target:,} of the review target's "
            f"{len(review.target):,} chars (its budget is {judge_budget:,}) — a finding "
            "about the part it did not get could only be dismissed as unsupported")
    elif judge_context < len(review.near) + len(review.far):
        judge_gaps.append(
            f"the judge saw {judge_context:,} of the "
            f"{len(review.near) + len(review.far):,} chars of context the panel was "
            f"offered (its budget is {judge_budget:,}) — it ruled on findings about code "
            "it was shown less of than the reviewers were")
    notes.extend(judge_gaps)
    # The judge is a claude seat, so it takes code access on the same terms the
    # reviewer seats do — and it is the party best placed to use it, because the
    # wrong findings #113 was filed over were CONFIRMED, not merely raised.
    #
    # The tree has to still exist at this line, which is what decides where the
    # cleanup below it goes. Removing it when the reviewer executor joined — the
    # obvious place, and where it was first written — left the judge holding a path
    # to a deleted directory: `seat_checkout` would fail its copy, fall back to an
    # empty sandbox, and the judge would silently review blind with the setting on
    # and nothing reporting it. Degrading correctly is exactly what made it silent.
    findings, judge_skip, ruled = adjudicate(
        clusters, judge_text, panel.get("judge_model", ""), pr_number, None, coverage,
        ci=ci_text, code_tree=code_tree, budget_usd=budget_usd,
        # #67's question, asked of the judge because it is the only party in the
        # round already holding both the previous round's complaints and the
        # commit that answered them. Empty on a round 1 and on any cycle whose
        # baseline named no findings, which leaves the prompt exactly as it was.
        recurrence=recurrence_brief(prior.fixed_findings, prior.head_round))
    # The prose half, unchanged since it was added — it is printed and recorded and
    # decides nothing. `ruled` carries the typed half beside it (#547), and the two
    # travel together so a reader of either can find the other.
    coverage_note = ruled.note
    # Now nothing reads the tree again: the reviewers copied out of it inside the
    # executor and the judge has just finished with it. Removed here rather than at
    # the end of `run` because a PR's checkout is the largest thing this process
    # holds and the report and board write-up still have to run.
    if code_dir is not None:
        shutil.rmtree(code_dir, ignore_errors=True)
    # The judge's own phase, and it is charged with the clustering and the material
    # composition ahead of it because those exist only to feed it. Its size against
    # `seats` is the argument for or against every proposal to overlap the two:
    # the judge cannot start until the slowest seat lands, so it pays `gated_ms`
    # before it begins and that cost is invisible in this number.
    clock.mark("judge")
    judged = judge_skip is None and bool(findings)
    to_fix = sorted((c for c in findings if c.verdict != "dismissed"),
                    key=lambda c: c.severity)
    # Which of them a fix round is actually asked to clear (#165). Below the floor a
    # finding is still master-confirmed, still recorded and still in the payload — it
    # is reported under its own heading with its own mark, exactly as an escalated
    # finding is marked ⛔, so a brief built from this report cannot pick it up by
    # accident. At the pre-#165 floor (`P4`) `under_floor` is empty and every list
    # below renders as it always did.
    # A PREDICATE, and every one of the four readers below goes through it. Splitting
    # into two lists and then asking `c in under_floor` looked equivalent and is not:
    # `Canonical` compares by value, so two genuinely distinct findings with the same
    # severity, file, line and synthesis are equal to each other, and one of them
    # would be marked on the strength of the other's membership.
    # `dials.fix_floor`, not `dials.fix_severity_floor`: the two differ only at
    # `low_severity_fix_lines: 0`, where the band the fix floor admits below the
    # trigger floor can buy nothing and so is not this round's work at all (#297).
    # The property carries the argument; what matters here is that ONE floor answers
    # "may this be fixed" for the report, the payload and the mark.
    def below_floor(c: Canonical) -> bool:
        return not severity_at_least(c.severity, dials.fix_floor)

    # The other half of #297: findings the fix floor admits but the trigger floor does
    # not, which the round pays for out of a shared line budget rather than
    # unconditionally. They stay in the fixer's LIST — a genuinely cheap fix is worth
    # taking while the pass is open, which is the argument `fix_severity_floor` is set
    # a tier low for — and are marked so the budget travels with them into any brief
    # built by pasting that list.
    def budgeted(c: Canonical) -> bool:
        return dials.budgeted(c.severity)

    for_fix = [c for c in to_fix if not below_floor(c)]
    under_floor = [c for c in to_fix if below_floor(c)]
    dismissed = [c for c in findings if c.verdict == "dismissed"]
    # Sonar's hard-gate issues never reach the judge, so each is a canonical
    # record of its own single account — numbered after the judged ones, since
    # `related` is resolved against ids that must be unique across the payload.
    sonar = [Canonical(id=_finding_id(pr_number, len(findings) + i + 1),
                       severity=f.severity, file=f.file, line=f.line,
                       synthesis=f.title, verdict="sonar", reported_by=[f],
                       rationale=f.detail)          # the Sonar rule that fired
             for i, f in enumerate(result.sonar_findings)]

    # ---- this round against the ones before it. Mechanical: which findings are
    # ones no earlier round raised, and does that make the loop done?
    # `prior` was loaded before the budgets: which commit the last round reviewed
    # is what decides this round's SCOPE, and scope decides what the budgets are
    # spent on. Loading it twice would also double every `problems` entry into
    # `notes`.
    prior_keys, prior_rounds = prior.keys, len(prior.rounds)
    notes.extend(prior.problems)
    seen_before: dict[str, bool] = {}

    def is_new(c: Canonical) -> bool:
        """Did no earlier round raise this? One predicate for the round diff, the
        🆕 marker and the serialised `new_this_round`, so the payload cannot
        disagree with the report about which findings are fresh. Memoised: the
        reworded-title fallback is a sequence comparison against every title the
        baseline holds."""
        if c.key not in seen_before:
            seen_before[c.key] = prior.raised_before(c)
        return not seen_before[c.key]

    # Every finding the cycle has to clear, not just the judged ones: Sonar's
    # hard-gate issues MUST end up resolved (/panel-review-pr §3), so a round
    # whose only outstanding item is a new or still-open gate issue is not a dry
    # round. Leaving them out classified exactly that as convergence and ended the
    # cycle without another fixer.
    #
    # #165's floors do not reach them, and `round_stop` is where that is enforced —
    # it exempts every key whose `verdict` is `sonar` from BOTH floors at every rule,
    # because Sonar's own severities are routinely P3/P4 and filtering by them put
    # this exact bug back: a new P3 gate issue fell out of `triggering`, landed in
    # `quiet_new`, and the cycle stopped `confident` on a PR that cannot merge. A
    # third floor has to go through the same exemption.
    outstanding = to_fix + sonar
    new_keys = sorted({c.key for c in outstanding if is_new(c)})
    flagged = sum(1 for c in to_fix if c.needs_rereview)
    # A baseline that could not be read, could not be attributed, or was never
    # passed is a veto in its own right, not just a lost confidence flag: the
    # operator is told to LIST the vetoes, and "not convergence" with an empty
    # list leaves the one question this exists to answer unanswered.
    # An earlier round's truncation becomes PERMANENT under increment scope, and
    # that is the one cost this feature has that its own numbers cannot show. Under
    # whole-PR scope a region round 1 was cut off from is read again by round 2, so
    # the gap closes on its own. Under increment scope round 2 reads only the fix
    # commit and never returns to it: the cycle can now converge — no new findings,
    # nothing outstanding — over code that no round in it ever read. So a quiet
    # round here is not evidence about that region, and says so.
    inherited: list[str] = []
    # Context the budget cut is a coverage gap in its own right, and it has to
    # veto rather than merely be noted. A scoped round is still allowed to raise a
    # defect nobody raised before, wherever it sits — the brief says so in as many
    # words — so the context is not decoration, it is the only part of the PR this
    # round can find a pre-existing defect in. Cut it and that becomes
    # unreachable, and the round would report the resulting quiet as convergence.
    #
    # `review.scope`, and NOT `target_scope` like the three below it — the one
    # exception in this block, so it is worth saying why rather than leaving it to
    # look like the conversion that was missed. This veto is about `short_context`,
    # which measures what the seats were sent AGAINST the context tiers of the
    # material they were sent it from. A manifest substitution replaces that
    # material with a whole-target composition whose `near` and `far` are both ""
    # (they are `init=False` on `ReviewScope` and only filled under increment
    # scope), so `short_context` on a manifest round is `sent < 0` for every seat —
    # empty by construction, whichever flag guards it. Converting the guard would
    # change nothing and would imply this veto can fire on a manifest round, which
    # it cannot: the gap a manifest round leaves is not "part of the context did
    # not fit", it is "nobody read the moved code", and `manifest_veto` below is
    # the sentence for that.
    if review.scope == "increment" and short_context:
        inherited.append(
            f"{', '.join(short_context)} saw only part of the PR behind the increment — a "
            "defect earlier rounds misjudged, in the part that did not fit, could not have "
            "been raised this round")
    if target_scope == "increment" and prior.truncated_rounds:
        cut = sorted(prior.truncated_rounds)
        inherited.append(
            f"{_rounds_phrase(cut)} had a truncated reviewer and this round reviewed only "
            f"the increment since {review.since[:8]} — whatever "
            f"{'those rounds were' if len(cut) > 1 else 'that round was'} cut off from has "
            "now been read by no round of this cycle, and re-reviewing the fix commit does "
            "not reach it")
    # The anchor advances over a round that read nothing — a title-skipped round
    # records a head, and so does one whose every seat failed. This round's
    # increment therefore starts AFTER code the cycle has no read of, and the
    # payload cannot show that: `scope` and `since_sha` say what was reviewed, not
    # what was stepped over.
    if target_scope == "increment" and prior.unread_rounds:
        skipped = sorted(prior.unread_rounds)
        inherited.append(
            f"{_rounds_phrase(skipped)} recorded a head but no reviewer read it, and this "
            f"round's increment starts after it — that code has been read by no round of "
            "this cycle")
    # The same shape of gap from a different cause (#138), and it needs its own
    # sentence because "no reviewer read it" is false of a manifest round: every
    # seat ran, on a description of a move rather than on the move. This round's
    # increment starts after that code, so nothing in the cycle will read it now.
    if target_scope == "increment" and prior.manifest_rounds:
        described = sorted(prior.manifest_rounds)
        inherited.append(
            f"{_rounds_phrase(described)} read a MANIFEST of a move rather than its code, "
            f"and this round's increment starts after it — the relocated code has been "
            "read by no round of this cycle")
    # A manifest round's quiet is the least trustworthy quiet the panel produces,
    # and it is the one nothing else catches. Every other coverage veto keys off a
    # seat being short of what it was sent — and a manifest round's seats were sent
    # the whole manifest, so `truncated` is false for all of them and the round can
    # stop `confident: True` having had nobody read a line of the moved code.
    #
    # Mechanical, not asked for. The brief does tell each seat to declare the facts
    # a manifest cannot carry, and a seat that does produces a `could_not_assess`
    # veto through the ordinary path — but "was the moved code read" is something
    # the panel KNOWS, from the material it composed, and this file's standing rule
    # is that what can be measured is never left to a model to volunteer.
    #
    # Not alert fatigue, unlike the absent-CLI case this deliberately does not
    # imitate: it fires only on a move-shaped diff over a seat's ceiling, not on
    # every round of every repo that configured something.
    manifest_veto = ([f"this round read a MANIFEST of a move, not the code — "
                      f"{pre.shape.moved:,} relocated lines went unread by every seat"]
                     if pre.verdict == "manifest" else [])
    # `ci_status` here is `review_ci_settled`'s answer — the one taken before the
    # seats were dispatched and after #501's bounded wait, which is the same value
    # the payload records and the report prints. Not re-read: a second fetch at the
    # end of the round would judge the round's confidence on a build that finished
    # after the seats had already reviewed without it.
    #
    # `ci_declared_absent` is the repo's own written answer to the question the CI
    # veto asks, read from the SAME key `preland` reads and refuses `none` by
    # pointing at. Read straight rather than through `preland.disabled_checks`,
    # which validates the list and hard-exits on a name nothing recognises: that is
    # the right behaviour for a merge gate, whose whole job is the checks, and the
    # wrong one for a read-only review that would then refuse to run over a typo in
    # a section it does not otherwise touch. A malformed list simply does not
    # contain "ci", so the veto stands — the strict direction — and preland still
    # hard-exits on it at land time, where it matters.
    _off = (cfg.get("preland") or {}).get("disabled_checks")
    # `isinstance(..., list)` is not defensive noise. `"ci" in "ci"` is True, and so
    # is `"ci" in "cinema"` — a `disabled_checks` written as a bare string rather
    # than a list would hand out the exemption by substring, which is the fail-OPEN
    # direction on the one setting here that can buy a confident stop.
    ci_declared_absent = isinstance(_off, list) and "ci" in _off
    # Declared this round plus every key an earlier round of the cycle accepted.
    # `prior` wins a collision for the same reason it does on `escalated`: the
    # earliest round that recorded the act owns its date, and re-passing a key you
    # inherited must not re-date the acknowledgement to now.
    ack_held = dict(sorted({**{k: round_no for k in accepted},
                            **prior.acknowledged}.items()))
    obligations = reached_obligations(reviewer_meta, ruled)
    veto = (coverage_veto(reviewer_meta, judge_skip, flagged, len(review.target),
                          ci_status=ci_status,
                          ci_declared_absent=ci_declared_absent,
                          coverage=ruled, acknowledged=ack_held)
            + manifest_veto + judge_gaps + inherited + prior.problems)
    # An acknowledgement naming no obligation this round raised is almost always a
    # re-worded claim under a new key, which `_claim_norm` says plainly it cannot
    # absorb — so it is SAID rather than corrected. The alternative readings are a
    # typo and a claim genuinely settled since, and this cannot tell the three apart;
    # what it can do is stop the caller reading the cycle's silence as the
    # acknowledgement having landed.
    raised = {ob.key for ob in obligations}
    for key in sorted(k for k in accepted if k not in raised):
        notes.append(f"--acknowledge {key} names no unverifiable claim this round "
                     "raised — check the key against the report's Unverifiable claims "
                     "list, and expect a new one if the judge reworded the claim")
    # Declared this round, plus every key an earlier round declared. The earliest
    # round that said so owns the answer, so `prior` wins a collision — a caller
    # re-passing a key it inherited must not re-date the claim to now.
    #
    # Sorted, for the same reason `round_stop`'s `escalated_outstanding` is: this
    # dict is serialised straight into the payload, so the order of the
    # `--escalated` flags and of the baseline reads must not change the artifact's
    # bytes. A diff between two payloads has to mean something changed.
    held = dict(sorted({**{k: round_no for k in declared}, **prior.escalated}.items()))
    # A key naming no finding this cycle has ever seen is almost always a typo,
    # and a typo here is silent by construction: the loop would simply carry on
    # counting a finding the caller believes it excluded. Said out loud rather
    # than corrected, because the other reading — a key from a cycle whose payload
    # was lost — is legitimate and this cannot tell them apart.
    #
    # `prior.escalated` counts as seen, and that is not redundant with
    # `prior.keys`: the register is inherited TRANSITIVELY (every payload carries
    # the whole register forward, and the skip path copies it) while the finding
    # RECORD is not. So passing only the latest baseline — which the docs allow —
    # inherits a key whose finding no bucket carries any more, and a premise
    # re-worded under a new key (`panel-review-pr.md` §5 says that happens very
    # often) drops out of every later round's buckets too. Without this the note
    # then fired every round for the rest of the cycle on a key that was never
    # mistyped, which is exactly the false positive that teaches a reader to skip
    # it. A key an earlier payload's register carries is by construction a key an
    # earlier round knew.
    seen = ({c.key for c in (*to_fix, *sonar, *dismissed)}
            | prior.keys | set(prior.escalated))
    for key in sorted(k for k in held if k not in seen):
        notes.append(f"--escalated {key} names no finding this round raised and no "
                     "earlier round's payload carries — check the key, or the "
                     "baseline it should have come in on")
    # Not a typo and not work either: the master ruled this finding not real, so
    # there is nothing for a fix round to clear and nothing for a human to answer.
    # Said rather than left implicit, because the escalation of a dismissed finding
    # is otherwise invisible — the key passes the typo check above (`seen` includes
    # `dismissed`), it is not in `outstanding` so no stop rule ever consults it, and
    # neither the record nor the report marks the row (see the `escalated` field on
    # the `dismissed` bucket below).
    ruled_out = sorted(({c.key for c in dismissed} & set(held))
                       - {c.key for c in outstanding})
    for key in ruled_out:
        notes.append(f"--escalated {key} names a finding this round's master DISMISSED "
                     "as not real, so it changes nothing about THIS round's stop — but "
                     "the key is still recorded and inherited, and a later round that "
                     "rules the same defect real will hold it there. Withdraw it from "
                     "the next round's --escalated if that is not what you meant")
    # ---- MEASURED BEFORE THE STOP RULE, WHICH IS WHAT #489 CHANGED ABOUT IT.
    # This block used to sit below `round_stop`, and could, because nothing read it:
    # provenance was recorded, tallied and printed and stopped no run. It now feeds
    # `escalate_on.fix_injection`, so the tally has to exist before the verdict that
    # consumes it. Nothing else moved and nothing here reads `stop` — the whole block
    # is a function of `outstanding`, the baseline and the fix range, all of which
    # were already resolved above, and nothing between here and where it used to sit
    # reads or writes any name it binds.
    #
    # The one visible consequence is ORDER: the two notes this block can post — an
    # unreadable fix range, and the #67 tally that goes dark with it — now appear in
    # `config_notes` above the stop rule's own notes rather than below them. That is
    # the right way round for a reader (the measurement, then what was decided on it)
    # and it is said here rather than left to be noticed, because `config_notes` is an
    # artifact people diff.
    # ---- provenance: did the last fix pass INTRODUCE this, or did it MISS it? --
    #
    # What this round could not read, banked for the next one. A file is unread
    # only if EVERY reviewer that ran was cut on it: one seat that read it means
    # the ROUND saw it, and blaming coverage for a defect some reviewer could
    # plainly see would let the panel off the hook for its own miss.
    #
    # Keyed on the BARE names of the seats that ran, not on `ran_llm`, which
    # holds the report's display labels: `budgets.get("claude (opus)")` is None
    # for every seat, every cut set comes back empty, and `unread_files` was
    # therefore empty on every run this ever made — the `missed-unread` bucket
    # unreachable in production while 487 unit tests, which call the helpers
    # directly with correct inputs, stayed green over it.
    # Defensive rather than expected: `budgets` covers the seats that are both
    # selected and installed (#222), and `run_seat` refuses an absent seat before
    # it can run — so a seat in `ran_names` has an entry (possibly None, meaning
    # uncapped). It survives so a future change to how `budgets` is built cannot
    # quietly turn "no budget recorded" into "read nothing", and it is the note a
    # test double that replaces `review_llm` wholesale would trip, since such a
    # double never reaches that refusal.
    no_budget = [n for n in ran_names if n not in budgets]
    if no_budget:
        notes.append("no diff budget is recorded for " + ", ".join(sorted(no_budget))
                     + " — those seats are left out of the unread-file record rather than "
                       "silently emptying it")
    cut = [_diff_files_cut(diff, budgets[n]) for n in ran_names if n in budgets]
    if cut:
        unread_files = sorted(set.intersection(*cut))
    elif not ran_names:
        # NO SEAT RAN, so nothing was read: every file is unread. The empty set
        # says the opposite — that the round read all of it — and would hand the
        # next round a `missed` for every defect in a diff nobody ever saw.
        unread_files = sorted(_diff_files_cut(diff, 0))
    else:
        # Seats ran, none of them with a recorded budget. That is a lookup miss,
        # not zero coverage, and the guard has to be on `ran_names` rather than on
        # `cut`: keyed on `cut`, one config miss would record a round that read the
        # whole diff as having read none of it, and the next round would bucket
        # every new finding `missed-unread` — a blanket indictment of the harness
        # bought by a missing dict key. Empty is what the `no_budget` note above
        # already promises ("left out of the record rather than silently emptying
        # it"), and the note is what tells the operator coverage is unrecorded.
        unread_files = []
    # Attribution needs both ends of the fix range, and `_fix_range_diff` says why
    # when it has none: a baseline written before `head_sha` was recorded, a
    # head that never moved, a branch rewritten between rounds, an API refusal.
    # All of them degrade to "unknown" rather than to a wrong answer.
    #
    # `cycle_run` is `in_cycle or prior_rounds`, so `cycle_run and prior_rounds`
    # only ever meant `prior_rounds`: round 1 has no earlier round to attribute
    # against whether it is in a cycle or not.
    attributable = bool(prior_rounds)
    # ---- ONE anchor, and the round's own lines where they are safe to use (#512).
    #
    # Two defects, one of them nearly introduced by the first cut of this change.
    #
    # **The anchor.** Scope anchors on `since or prior.head_sha`; this used to anchor
    # on `prior.head_sha` outright. `--since` is documented and legitimate — "pass it
    # to review a specific range, or when the baseline predates that field" — and
    # passing it pointed the two at different spans with nothing reporting the
    # mismatch, so the provenance numbers described a range nobody looked at. Both
    # now read `anchor`, so they cannot drift.
    #
    # **The status guard, which is why the compare call STAYS.** It is tempting to
    # drop it and attribute straight off `review.increment` — the round reviewed
    # that diff, so it is the fix pass by construction. It is not, after a rewrite.
    # `fetch_increment` uses the three-dot form and says what that costs: "when the
    # branch was force-pushed or rebased between rounds the merge base moves back
    # and the increment WIDENS toward the whole PR. That is the safe failure: the
    # round re-reads more than it needed to." Safe for a review; catastrophic for an
    # attribution, because every line the PR ever added is then inside the "fix
    # range" and every finding on one reads `introduced`. `panel_scope` only falls
    # back at `len(increment) >= len(diff)`, so a rebase that widens the increment to
    # most of the PR passes every guard and becomes the target.
    #
    # `_fix_range_diff` is the only thing that sees `status`, refuses `diverged` and
    # `behind`, and drives #509's veto. So it keeps running, and what changes is
    # WHICH LINES are attributed once it has said the range is sound.
    #
    # **And then the increment's lines, because they are the better ones.** It is
    # `_diff_subset`'d to files also in the PR diff, which drops a base-branch
    # merge's own files — the over-count `_fix_range_diff`'s docstring names and
    # cannot fix, since main's commits legitimately sit inside its range.
    # `anchor`, not `review.since`: the two agree while the increment holds, and
    # `review.since` is EMPTY on every round whose scope fell back to `pr` — so
    # reading it there would silently revert to `prior.head_sha` and drop an explicit
    # `--since`, which is the mismatch this is here to close. `anchor` is bound
    # before scope is decided, carries `--since`'s own validation, and is what the
    # round would have reviewed from.
    if attributable:
        _range_diff, no_range_why, range_kind = _fix_range_diff(
            gh_repo, anchor, head_sha)
    else:
        _range_diff, no_range_why, range_kind = None, None, FIX_RANGE_OK
    # ---- #504: a REWRITTEN range is a wrong range, not a lost fix pass.
    #
    # #509 made a rebased round honest and #512 gave it one range to be honest
    # about; neither keeps the instruments ARMED, so a rebase between rounds still
    # ends with every finding `unknown` and `escalate_on.fix_injection` unable to
    # fire on the shape it is worth most on — the fixer working against a base that
    # moved. #500's own observation is what makes the repair possible: the old SHAs
    # still resolve, so the range is wrong and the history is not.
    #
    # Tried ONLY on `rewritten`, and only on an attributable round. Every other
    # verdict already has a sound range in hand — `ok` has this reader's, `blind`
    # usually has the round's own increment — and spending local git on those would
    # buy a second answer to a question already answered, which is the duplication
    # #512 has just finished removing.
    rebuilt = (reconstruct_fix_range(cfg.get("path") or "", gh_repo, base,
                                     anchor, head_sha)
               if attributable and range_kind == FIX_RANGE_REWRITTEN else None)
    # The increment is usable whenever the range is not REWRITTEN — including when
    # this reader came back blind. `blind` means "I could not get the range" (too
    # large to hold, an API refusal), which says nothing about the copy the round
    # already reviewed; discarding a sound increment there is a false blindness, and
    # it would fire on exactly the big base-branch merge this feature is for.
    # `rewritten` is the one that forbids it, because then no diff of that span is
    # the fix pass — the round's included.
    have_increment = review.scope == "increment" and bool(review.increment)
    # `reconstructed` is checked FIRST and it is the only source a rewritten round
    # may use. The increment is barred there for the reason above it and stays
    # barred — the reconstruction does not make the round's own diff trustworthy,
    # it supplies a different one — and `_range_diff` is None on that road anyway.
    if rebuilt and rebuilt["diff"]:
        fix_diff, fix_range_source = rebuilt["diff"], "reconstructed"
    elif attributable and range_kind != FIX_RANGE_REWRITTEN and have_increment:
        fix_diff, fix_range_source = review.increment, "increment"
    else:
        fix_diff = _range_diff
        fix_range_source = ("compare" if _range_diff else None) if attributable else None
    # ONE predicate for "is there a range", used by the added lines, by the note
    # and by the attribution itself. Two of them disagreed over an EMPTY compare:
    # truthiness called it no range, `fix_diff is not None` called it a readable
    # range with no added lines — and that reading labels every new finding
    # `missed`, confidently, with no note to say the range was empty.
    fix_added = _diff_added_lines(fix_diff) if fix_diff else {}
    if attributable and not fix_diff:
        notes.append(f"provenance unavailable: {no_range_why} — new findings are recorded "
                     "as `unknown`, not attributed")
    # SAID either way, because a reconstruction is a different measurement from the
    # one every other round makes and a reader comparing `introduced` across a cycle
    # has to be able to see where the denominator changed under them. The failure is
    # worth a line for the opposite reason: `no_range_why` above says the branch was
    # rewritten, which by itself now reads as "so it was rebuilt" — the note names
    # which of the repair's own refusals it hit, and most of them (no local checkout,
    # commits this box never held) are things an operator can act on.
    if rebuilt and rebuilt["diff"]:
        # No caveat clause, and that is the point of the rewrite this had under review
        # rather than an omission: a reconstruction that would have needed one now
        # DECLINES. Every commit the last round reviewed came through the rewrite
        # intact, the pass is the tail of the branch, and what is attributed is one
        # net diff of it — so `introduced` is a floor here exactly as it is on a
        # linear round, which is what `escalate_on.fix_injection`'s threshold is
        # calibrated against.
        notes.append(
            "the fix range was RECONSTRUCTED across the branch rewrite (#504): "
            f"{len(rebuilt['commits'])} commit(s) on the branch have no patch-"
            f"equivalent among the {rebuilt['prior']} the last round reviewed, so "
            "provenance, recurrence and `escalate_on.fix_injection` read those. "
            "Not repaired by it: `--scope increment` (scope was settled before the "
            "seats ran) and #506's revert proposal (it reads the compare range)")
    elif rebuilt:
        notes.append("the fix range could not be reconstructed across the rewrite "
                     f"(#504): {rebuilt['why']}")
    # ---- #500: an instrument that could not run is a COVERAGE GAP, and takes a veto.
    #
    # Three of this cycle's convergence instruments read the same fix range —
    # provenance (#48), recurrence (#67), and `--scope increment` — so losing it
    # loses all three at once, on the round that most wants them. The panel already
    # DETECTS that and says so; what it said it in was a `config_notes` line in a
    # payload, read afterwards by whoever thought to look, while the round went on to
    # report a `stop` whose `reason` never mentioned that its main convergence test
    # was off.
    #
    # **#497 is what makes this cost something.** `fix_injection` is computed from
    # provenance: with the range gone every new finding lands in `unknown`, `unknown`
    # sits in the denominator, and the rate is depressed toward zero — so the gate
    # cannot fire on precisely the cycle it exists for. The measured case is #500's:
    # a base branch 21 commits ahead, an ordinary and correct rebase between rounds 2
    # and 3, and a round 3 that could not have produced either of the numbers #497
    # was calibrated on.
    #
    # This is the same rule the panel already applies to a reviewer that could not
    # read the whole diff — that takes a veto line and costs `confident` — and an
    # unavailable provenance is a coverage failure of exactly that kind. It does not
    # stop the cycle and must not: the answer to a blind round is another round with
    # the instruments back, not a stop.
    #
    # **`no-fix` is excluded, and that is the whole reason `range_kind` is a value.**
    # A head that never moved, or a fix pass that netted to nothing, leaves the
    # instruments VACUOUS rather than blind — there is no fix pass they failed to
    # see. Vetoing there would fire on an honest empty round, and a veto that fires
    # on nearly every round teaches the reader to skip the veto list, which is where
    # the real coverage gaps are reported. That is #501's argument about the CI veto
    # and it applies here before the fact rather than after it.
    # #512 narrows this: a `blind` range whose increment answered is not a blind
    # round — the attribution happened, from the diff the seats actually read — so
    # vetoing there would be the alert fatigue this veto was written to avoid. A
    # REWRITTEN range vetoes unless it was REBUILT, which is the next paragraph:
    # nothing already in hand may attribute after a rewrite, and #504 does not put
    # anything in hand, it goes and fetches a different thing.
    # #504 narrows it once more, and on the same rule #512 used: what the veto is
    # about is whether the round ATTRIBUTED, never which reader answered. A rewritten
    # range whose pass was rebuilt from the object store has its instruments back —
    # vetoing there would be the alert fatigue this veto was written to avoid, and it
    # would fire on the one round that had just repaired itself. What is left of the
    # rewrite is a lean, and the note above carries it. Written as one condition over
    # `fix_range_source` rather than two over `range_kind`, because the two spellings
    # had already drifted apart once: `rewritten` could only ever reach here with no
    # source, so this is today's behaviour with the new road exempted and nothing else
    # moved.
    if attributable and fix_range_source is None and range_kind in (
            FIX_RANGE_REWRITTEN, FIX_RANGE_BLIND):
        veto = [*veto, (
            f"provenance, recurrence and increment scoping all read the fix range and "
            f"this round had none — {no_range_why}. Every new finding is recorded "
            "`unknown`, so `escalate_on.fix_injection` (#497) cannot fire on this "
            "round whatever the fix pass did: the rate is computed over a denominator "
            "the unattributable findings sit in. Nor can #506 NAME the offending pass "
            "— it reads this same range — so a fix pass that did generate this round's "
            "work would ship unproposed as well as unmeasured. This round's quiet is "
            "not evidence of a converging cycle (#500)")]

    def provenance_of(c: Canonical) -> str | None:
        """None where the question does not arise — outside a cycle, in round 1,
        or for a defect an earlier round already raised. A repeat's provenance is
        not "unknown", it is not asked: it was not introduced by the fix pass
        under attribution, because it predates it. Same discipline as
        `new_findings` being None rather than 0 for a review-only run — the
        board's column already means "the panel did not say"."""
        if not attributable or not is_new(c):
            return None
        return _provenance(c.file or "", c.line, fix_added, prior.unread_files,
                           bool(fix_diff), all_unread=prior.read_nothing)

    # Counted over `outstanding` — the findings the cycle actually has to clear —
    # so the tally matches `new_findings` rather than roping in the dismissed
    # ones, which no fixer will ever touch. ONE pass rather than one per bucket:
    # `provenance_of` walks `fix_added` through `_same_file` on every call.
    #
    # Kept as a LIST of (finding, bucket) rather than tallied straight into a
    # Counter, because #506 needs the findings themselves and not only how many
    # there were: its proposal has to say WHICH findings a revert would remove, and
    # re-deriving them would mean a second walk that could disagree with this one
    # about a finding's bucket.
    placed = [(c, provenance_of(c)) for c in outstanding]
    tally = Counter(b for _, b in placed)
    provenance_counts = {b: tally.get(b, 0) for b in PROVENANCE} if attributable else {}

    # Whether a CYCLE exists at all, and the one predicate that decides it — for
    # the report's Rounds block, for the payload and for the trend row below alike.
    # They used to disagree: the report suppressed the block for a review-only run
    # while the payload sent `round_stop` regardless, so `record_review` stored a
    # `/panel` read with findings as `stopped: false` (the board shows a cycle
    # mid-flight that nothing will advance) and one without as `stopped: true,
    # stop_confident: true` — a confident-convergence record for a PR that had no
    # cycle.
    #
    # Resolved HERE rather than after the stop rule because the trend row below now
    # carries `new_findings` and has to gate it on exactly this predicate: for a
    # review-only run `len(new_keys)` is every finding — the vacuous count "raised by
    # no earlier round" when there was no earlier round — and #505's rung reads that
    # column. A second spelling of the same test beside it is how the report and the
    # payload came to disagree the first time.
    cycle_run = bool(in_cycle or prior_rounds)

    # ---- #490: this round's own row of the cross-round trend block, and the earlier
    # rounds' rows beside it.
    #
    # REPORTING, and — since #505 — one column that is not. `round_stop` reads
    # `new_findings` off these rows and nothing else off them: no ceiling in
    # `panel_caps` consults the block, the fixer's brief does not, and no other cell
    # here can stop a cycle or buy one another round. The block shipped strictly
    # reporting-only on purpose (a cheap reporting improvement chained to a policy
    # argument waits on the policy argument), and the column that grew a consumer is
    # the one #505 argued for; the rest stay where they were.
    #
    # This is also why the rows are built BEFORE the stop rule rather than in the
    # report: the same per-round series a reader checks the verdict against is the
    # series the verdict was taken over, so the block and the stop cannot disagree
    # about how the count moved.
    #
    # This round's row is built HERE rather than in the report, from the same
    # variables the report's own counts come from (`outstanding`, `provenance_counts`,
    # `review.diff`), so the block's last line cannot disagree with the summary two
    # lines above it about how many findings this round has.
    #
    # No filter here for a baseline claiming THIS round or a later one, and that is a
    # deliberate non-guard rather than an oversight: `load_baseline` already refuses
    # such a payload outright — "is round N, which is not earlier than this run's
    # round N" — and both call sites hand it this run's `round_no`, so no such row can
    # reach `prior.trend` and its findings are not counted as an earlier round either.
    # A second filter would be unreachable code carrying a config note that can never
    # fire, which reads to the next person as a case somebody has actually seen.
    #
    # The denominator, once, for the block and for the payload alike — and it is
    # `max_fix_growth`'s own (`Baseline.first_reviewed`, #298), never "the first row
    # that happens to carry a size". A report that printed two growth ratios from two
    # denominators would be worse than one that printed none: a reader has no way to
    # tell which of them the ceiling is about to fire on.
    # ROUND 1 is the cycle's first reviewed round by construction — `load_baseline`
    # refuses every payload at round 1, so `first_reviewed` is None there whatever the
    # caller passed — and it is therefore its own denominator, at 1.0. Left at None it
    # recorded `growth: null` for round 1 and a real ratio for every round after, so a
    # board plotting the field across a cycle's payloads saw the series start at
    # nothing on the one round whose answer is not in doubt. Round 2 re-derives that
    # same row from the baseline and gets the same 1.0, so this is the two paths
    # agreeing rather than a second rule.
    #
    # NOT a fallback for "there are earlier rounds but no usable size" — round 2 with
    # an unreadable baseline, or one written before `pr_chars`. There the denominator
    # genuinely is unknown, `max_fix_growth` does not run either, and substituting
    # this round's own size would record 1.0 against a PR that has been growing for
    # three rounds.
    trend_first_chars = (prior.first_reviewed[1] if prior.first_reviewed
                         else len(review.diff) if round_no == 1 else None)
    trend_rows = [*prior.trend,
                  RoundTrend(round=round_no, reviewed=True,
                             findings=len(outstanding),
                             p1p2=sum(1 for c in outstanding
                                      if severity_at_least(c.severity, TREND_SEVERE)),
                             # `provenance_counts`, not the raw tally: `attributed`
                             # has to see the same object a LATER round will read back
                             # out of this payload, or this round's row and its own row
                             # one round later would answer differently.
                             # #505's column, gated on `cycle_run` exactly as the
                             # payload's own `new_findings` is and for its reason.
                             #
                             # AND ON `prior.problems`, which the other cells do not
                             # need (found by a codex second opinion). "New" here means
                             # "no EARLIER round raised it", and that is a claim about
                             # the baselines — so a baseline this run could not read,
                             # or refused as another review's, or could not tell from a
                             # duplicate of the same round, makes findings an earlier
                             # round DID raise count as new and inflates this number.
                             # Fed to #505's rung that is an inflated count compared
                             # against a sound predecessor, which is the direction that
                             # ends a cycle. `None` withholds it, the streak treats it
                             # as the absence of the comparison, and `counts` carries
                             # the null so a reader can see which round went dark.
                             #
                             # Only THIS round's cell needs it. The mirror case — a
                             # later round comparing against an earlier round's inflated
                             # count — makes `was` larger and `cur >= was` less likely,
                             # so it already fails toward going again.
                             new_findings=(len(new_keys)
                                           if cycle_run and not prior.problems
                                           else None),
                             introduced=(provenance_counts.get("introduced")
                                         if attributed(provenance_counts) else None),
                             # The whole PR, whatever this round reviewed (#298) —
                             # `review.diff`, which is what `pr_chars` records below
                             # and what `max_fix_growth` measures.
                             pr_chars=len(review.diff))]


    # ---- #506: the remedy for the rule above, priced but not taken.
    #
    # `escalate_on.fix_injection` ends the cycle and leaves the pass that caused it on
    # the branch. This assembles the two columns of the decision that follows — what
    # reverting that pass would REMOVE (the findings this round attributed to it) and
    # what it would COST (the complaints it was sent to answer that this round no
    # longer raises) — and names the commit range, which is `prior.head_sha ..
    # head_sha`: the same range provenance attributed against, so the proposal cannot
    # point at a different pass from the one the rate accused.
    #
    # `range_kind` rather than `bool(fix_diff)`, and that is #500's constraint arriving
    # here rather than a style choice: a rebased branch is `blind` and an empty fix
    # pass is `no-fix`, both come back with no diff, and only the first is a pass this
    # cannot see. `revert_state` carries the distinction through in
    # `_fix_range_diff`'s own words so nothing downstream has to re-derive it from a
    # sentence.
    #
    # Built on every attributable round, not only on the one that fires: the payload
    # records what the round KNEW, and a consumer that had to infer "there was nothing
    # to propose" from a missing key would be reading the payload's age.
    # ONE injection state, built here and passed to `round_stop` below rather than
    # computed twice: `over` is also the precondition for the extra API call under it,
    # and two `injection_state` calls could disagree about whether to make it.
    injecting = injection_state(provenance_counts, injection_limit)
    # #554's measurement over the SAME fix range `injecting` was attributed against —
    # `fix_diff`, whichever of the compare and the round's own increment supplied it —
    # so the two rungs cannot end up accusing different passes. `None` where there is
    # no range, which is `over: False` by construction: a round that could not see the
    # pass does not end a cycle on what the pass contained. Reading `fix_diff` rather
    # than the compare's own answer is what gets #504's reconstruction for free: a
    # rewritten round that rebuilt its pass out of the local object store is measured
    # here exactly as a linear one is, and a rewrite nothing could rebuild disarms
    # this rung and #489's together.
    #
    # Cheap enough to compute on every attributable round rather than only where the
    # brake is armed: it is one pass over a diff already in memory, and the payload
    # records what the round KNEW — a repo that switched the rung off still gets to
    # see that its fix pass wrote nothing checkable.
    refereeing = referee_state(referee_split(fix_diff) if fix_diff else None,
                               unrefereed_armed)
    revert_cleared, revert_open = fix_pass_outcome(prior.fixed_findings, outstanding)
    # The commits inside the range, and the ONE extra `gh api` call this whole feature
    # makes — paid only on a round whose rate crossed the threshold, which is the
    # terminal round of a diverging cycle and no other. `over` rather than `fired`
    # because `fired` is `round_stop`'s answer and is not known yet; it is a strict
    # superset, so the call is made on a few rounds that go on not to propose anything
    # and never on a round that could not.
    #
    # It is not folded into `_fix_range_diff`'s read, which every round makes, for
    # exactly that reason — and it is not derived from the range either, because the
    # range cannot say it (found by Codex): a base-branch merge leaves the compare
    # `ahead` with main's own commits inside it, so a wholesale revert of the range
    # would undo commits no fix pass wrote and `git revert` would refuse the merge
    # anyway. `revert_state` withholds the command on that, and on a shape it could
    # not read at all.
    revert_shape = (fix_pass_commits(gh_repo, prior.head_sha, head_sha)
                    if attributable and range_kind == FIX_RANGE_OK and injecting["over"]
                    else {})
    revert = revert_state(
        range_kind if attributable else REVERT_NOT_ASKED,
        why=no_range_why, base_sha=prior.head_sha if attributable else None,
        head_sha=head_sha if attributable else None, head_round=prior.head_round,
        # This round, against the anchor's own round, so the proposal can say how many
        # fix phases the range covers. `Baseline.head_sha` is the latest earlier round
        # that SUPPLIED a commit, not the latest that ran, so a round whose payload
        # recorded none leaves the range spanning two passes (found by Codex).
        round_no=round_no,
        # What this round REVIEWED, which is what decides how the cost column reads
        # (`fix_pass_outcome`): under the default `increment` scope most of what the
        # pass was sent to fix was not looked at again, so "no longer raised" is a
        # ceiling on the cost rather than a measurement of it.
        scope=review.scope,
        removes=[{"key": c.key, "severity": c.severity, "file": c.file,
                  "line": c.line, "title": c.synthesis}
                 for c, bucket in placed if bucket == "introduced"],
        costs=revert_cleared, still_open=revert_open, shape=revert_shape)

    # The repeat KEYS, not a count of them: `round_stop` subtracts the escalated
    # ones itself, so the rule lives in one place instead of depending on every
    # caller to filter first. It takes keys and nothing else — the count overload
    # it used to accept could not obey the escalation rule, and a caller passing
    # one put the #221 jam straight back with nothing said.
    stop = round_stop(round_no, cap, new_keys, outstanding, veto, not prior.problems,
                      repeated={c.key for c in outstanding if not is_new(c)},
                      escalated=held,
                      # #165. The trigger floor bounds which NEW findings buy a round;
                      # the fix floor bounds rules 2 and 3, because a finding no fix
                      # round was asked to clear is outstanding every round by
                      # construction and would otherwise run the cycle to the cap on
                      # its own. `round_stop`'s docstring has the whole argument.
                      trigger_floor=dials.round_trigger_floor,
                      # The floor the round was REQUIRED to clear, which is the fix
                      # floor until a budget is in force and the cut afterwards
                      # (#297) — an unpaid budgeted finding is outstanding for the
                      # same reason a below-floor one is, and rule 3 would otherwise
                      # run every budgeted cycle to the cap on it.
                      # `Dials.cleared_floor` has the argument.
                      fix_floor=dials.cleared_floor,
                      # #84's register, read-only here. The BRAKE runs before a fix
                      # pass (`panel.py --premise`); this is the round's half — it
                      # ends a cycle whose premise was declared twice and reached a
                      # round anyway, and it reports the fix passes that declared
                      # nothing and so could not have been braked at all.
                      premises=premise_state(premises, round_no, premise_limit,
                                             premise_undecidable),
                      # #489's injection gate. The measurement is `provenance_counts`
                      # above — `introduced` over every new outstanding finding — and
                      # `injection_state` turns it into the verdict `round_stop`
                      # applies. Empty on round 1 and on any round whose fix range
                      # could not be read, which is `over: False` by construction: a
                      # round that could not be attributed does not end a cycle.
                      injection=injecting,
                      # #506's remedy for that gate, assembled above. It decides
                      # nothing — it cannot stop the cycle and cannot keep it going —
                      # and its only effect is one more veto line on the round the
                      # gate ended, naming the commit range that is still on the
                      # branch and pricing what undoing it would cost.
                      revert=revert,
                      # #505's volume rung beside it. The series is the trend block's
                      # own `new_findings` column — every round's count of findings no
                      # earlier round raised, this one included — so the block a reader
                      # checks the verdict against IS the series the verdict was taken
                      # over. Nothing here comes from provenance, which is why a rebase
                      # between rounds (#500) cannot disarm this the way it disarms the
                      # gate above.
                      not_falling=not_falling_state(
                          [(t.round, t.new_findings) for t in trend_rows],
                          not_falling),
                      # #554's rung. The measurement is the fix range's own churn,
                      # classified into production/test/prose, and the rule is a
                      # predicate on it rather than a threshold: a pass with no
                      # production line in it wrote only artefacts nothing in the loop
                      # can check, so the round it would buy is a review of unrefereed
                      # work. Empty on round 1 and on any round whose fix range could
                      # not be read.
                      unrefereed=refereeing)
    # Said in `config_notes` as well as in `round_stop`, because these two are read
    # by different people at different moments: the payload's `round_stop` is what
    # the orchestrator's `jq` reads to decide whether to go again, and `config_notes`
    # is what a human reads off the PR comment afterwards. An unescalatable cycle has
    # to be legible in the second place too, or the gap is only ever found by
    # somebody already looking for it.
    #
    # Gated on `--premise-file`, so it fires for a cycle that WIRED the brake and
    # skipped a declaration and stays quiet for one that never wired it at all. Those
    # are different facts and only the first is actionable by the caller reading this
    # report; a note on every round of every unwired cycle is the "loud and wrong" a
    # reader learns to skip, and it would arrive on the same line as the ones that
    # mean something. The unwired case is still IN the payload
    # (`round_stop.premises.undeclared_rounds`), where an auditor asking "could this
    # cycle have been braked at all?" is looking.
    undeclared = stop["premises"]["undeclared_rounds"]
    if undeclared and premise_file and premise_limit is not None:
        notes.append(
            f"the fix pass after round(s) {', '.join(str(r) for r in undeclared)} "
            "declared no premise (`panel.py --premise`), so #84's futility brake could "
            "not be evaluated on it — those passes are UNESCALATABLE, which is a gap "
            "in this cycle's record rather than a clean one")
    for repeated in stop["premises"]["repeated"]:
        notes.append(
            f"premise {repeated['key']} was declared in rounds "
            f"{', '.join(str(r) for r in repeated['rounds'])} — "
            f"{repeated['text']!r}. A fix pass was written against it more than once, "
            "so the cycle ends here and a human answers the premise (#67, #84)")
    # #491's late half, and it is the same shape as the repeat above for the same
    # reason: `panel.py --premise` refuses the fix when it is PROPOSED, and a caller
    # that ignored exit 4 wrote it anyway. The register is then the record that says
    # so, and the round that follows is where it costs something.
    #
    # Only when the brake is ARMED. `premise_state` lists an undecidable declaration
    # either way — the payload reports what the cycle declared — but a repo that
    # switched the brake off asked for a fixer to be allowed to approximate, and
    # ending its cycle on the answer anyway would apply a policy it declined.
    for undecidable in (stop["premises"]["undecidable"] if premise_undecidable else []):
        notes.append(
            f"premise {undecidable['key']} was declared in rounds "
            f"{', '.join(str(r) for r in undecidable['rounds'])} — "
            f"{undecidable['text']!r} — and answered `decidable: no`. A fix pass was "
            "written against a property the runtime cannot observe, so every fix for "
            "it is an approximation: the cycle ends here and a human answers it "
            "(#491)")
    # #489, said in `config_notes` as well as in `round_stop` and for the reason the
    # premise notes above are: `jq .round_stop` is what an orchestrator reads to
    # decide whether to go again, and this is what a human reads off the PR comment
    # afterwards. The number is already printed a few lines further down ("N
    # introduced by the last fix pass"); what this adds is that it crossed a
    # threshold and ended the cycle, which the bare count cannot say.
    # `fired`, NOT `over`. `over` is a property of the measurement and is true of
    # rounds this rule deliberately does not touch — a below-floor policy stop, a
    # round holding an escalation, a round going again under rule 2 for a P1. Gating
    # on it would print "the cycle ends here" under a confident, converged verdict,
    # which is a `config_notes` line contradicting the `reason` beside it.
    if stop["fix_injection"]["fired"]:
        fi = stop["fix_injection"]
        notes.append(
            f"{fi['introduced']} of {fi['new']} new outstanding findings "
            f"({fi['rate']:.0%}) were introduced by the fix pass before this round, "
            f"over the {fi['limit']:.0%} `review_panel.escalate_on.fix_injection` "
            "threshold — the cycle ends here. `introduced` is a floor and not a "
            "measurement (#48), so the real share is at least that; what this needs "
            "is a human deciding whether the fix passes are working, not another one "
            "(#489)")
    # #506, and only on the round that actually put a proposal. `offered`, not
    # `fired`: the two come apart on a branch whose fix range could not be read, where
    # the cycle can end on the rate and the pass still cannot be named — and a note
    # saying "here is the range" with no range in it is worse than silence. The veto
    # list carries the full sentence, since that is where a reader is told why the
    # quiet does not count; this is the shorter one, in the place a human reads what
    # the round DECIDED.
    if stop["revert"]["offered"]:
        rv = stop["revert"]
        notes.append(
            f"the fix pass the rate accuses is `{rv['range']}` — everything that "
            f"landed after round {rv['round']} — and ending the cycle does not remove "
            f"it from the branch. Reverting it would drop {len(rv['removes'])} finding"
            f"(s) attributed to it and give back up to {len(rv['costs'])} it cleared: "
            "a proposal for a human to weigh, not something this loop does (#506)")
    # An escalated SonarCloud issue is still a RED GATE, and "the approach is wrong,
    # a human must answer the premise" does not turn it green. `round_stop` counts
    # it like any other escalation — correctly, since it is work no fix round may
    # do — so a cycle whose only remaining item is an escalated `python:S2259`
    # stops with "nothing left that a fix round can clear", which is true of the
    # ROUND and reads as a clean finish on a PR that cannot merge.
    #
    # Named here rather than fixed by keeping gate issues out of the register:
    # `outstanding` is `to_fix + sonar` and the stop rule already acted on the key,
    # so a record that showed the issue as ordinary work would contradict the rule
    # that stopped the cycle. `preland.py` HOLDs on `sonar_gate == "ERROR"`
    # independently, so nothing lands on this silently — but that is a different
    # script, and this panel's own verdict has to say it too: in `reason`, which a
    # loop reads, and in `veto`, which a human reads. `confident` is already false
    # (holding an escalation takes a veto line of its own), so this adds no verdict
    # it has not already earned.
    held_gate = sorted({c.key for c in sonar} & set(stop["escalated_outstanding"]))
    if stop["stop"] and held_gate and result.sonar_gate == "ERROR":
        stop["reason"] += " — and the SonarCloud quality gate is still FAILING"
        stop["veto"] = [*stop["veto"],
                        f"{len(held_gate)} escalated finding(s) are SonarCloud gate "
                        "issues and the gate reads ERROR — a premise answer does not "
                        "clear an external merge gate, so this PR cannot land until "
                        "the issue is resolved or excluded in SonarCloud"]
    # ---- the growth ceiling (#165). A fix pass that MULTIPLIES the diff has written
    # a second change, not a fix: on PR #236 the fix passes took a 359-insertion bug
    # fix to 2,313 while none of the 67 findings was in the fix, and the last of them
    # introduced an unbounded FIFO read. So a round whose PR is more than
    # `max_fix_growth` times the size the cycle's FIRST round found it at stops and
    # says the change wants splitting, rather than buying another panel over a bigger
    # change.
    #
    # **BOTH ENDS ARE THE WHOLE PR, WHATEVER THIS ROUND REVIEWED (#298).**
    # `round_scope` decides what the reviewers are asked to LOOK AT; this ceiling
    # asks how big the change has BECOME. They are different questions, and the
    # second must not silently change its meaning because the first was configured.
    # Measured on `review.target` — as it was until #298 — the default `increment`
    # scope put one round's fix commit over the cycle's whole-PR starting size, which
    # is a real quantity and not the one that runs away: PR #188 went 185 -> 593 ->
    # 721 churned lines, 3.90x under this 3.0x ceiling, while its round-2 increment
    # was 128 lines and never came near it. The backstop against the 63.7% bad-fix
    # injection this repo measures was pointed at the wrong number and never fired.
    #
    # Still no new plumbing on this end: `review.diff` is the PR as `gh pr diff`
    # returned it under either scope, and `Baseline.first_reviewed` reads the
    # earliest baseline's whole-PR size off the payloads round 2+ already receive via
    # `--baseline` (`pr_chars`, recorded below).
    #
    # `review.diff` under a manifest round is the MANIFEST, exactly as
    # `review.target` was: what that round put in front of the panel is a description
    # of a move, and the target's pre-substitution size lives in `preflight.shape`.
    # Named rather than left to be discovered, because it is the one case where this
    # ratio is not two diff sizes.
    #
    # The measurement of each end still travels with it and is still printed, because
    # two readings of a size exist in this payload and whatever reports a ratio has to
    # be able to say which one it computed. `review_scope` rides alongside so a reader
    # can see what the round reviewed without mistaking it for what was measured.
    #
    # **NOT dressed up as convergence.** It takes a veto line naming itself and
    # `confident` is forced false, the same discipline the round cap and a held
    # escalation get: the cycle is ending because something went wrong, and a reader
    # who cannot tell that from a clean finish has been told the opposite of the truth.
    # Set AFTER the SonarCloud sentence above so the reason reads outermost-first.
    #
    # **TWO NUMBERS, CROSSED-FIRST (#492).** The multiple above is the whole of what
    # this used to be, and a pure multiple hands its rope out in proportion to the
    # starting size: at 3.0x a 113-line PR may grow ~226 lines while a 2,000-line one
    # may grow 4,000 — the loosest allowance handed to the case most in need of a
    # ceiling. So `max_fix_growth_chars` states the same limit absolutely and the
    # cycle stops on whichever is crossed first. Both are ceilings, so the pair can
    # only ever TIGHTEN: nothing this arrangement lets through would have been caught
    # by the multiple alone. Either half is `null`-able on its own and both null is
    # the pre-#165 behaviour of no check at all, which is why the block below runs
    # whenever EITHER is set rather than only when the multiple is.
    #
    # `grown` is the difference of the same two sizes the ratio divides — deliberately,
    # because two halves of one ceiling read off two different measurements is #298's
    # defect one level up, and there is exactly one pair of numbers in this payload
    # that both halves may be computed from.
    growth = None
    limit, limit_chars = dials.max_fix_growth, dials.max_fix_growth_chars
    if (limit is not None or limit_chars is not None) and prior.first_reviewed:
        first_round, first_chars, first_scope = prior.first_reviewed
        pr_chars = len(review.diff)
        ratio = pr_chars / first_chars
        grown = pr_chars - first_chars
        over_ratio = limit is not None and ratio > limit
        over_chars = limit_chars is not None and grown > limit_chars
        over = over_ratio or over_chars
        growth = {"limit": limit, "limit_chars": limit_chars,
                  "ratio": round(ratio, 3), "grown": grown,
                  "over": over, "over_ratio": over_ratio, "over_chars": over_chars,
                  "chars": pr_chars, "scope": "pr",
                  "review_scope": review.scope,
                  "first_round": first_round, "first_chars": first_chars,
                  "first_scope": first_scope}
        if over:
            # Which half fired, in the words each one is about. A stop that named the
            # multiple when the absolute is what bound would send an operator to raise
            # a key that was never crossed — and where both fired, both are said,
            # because "3.4x AND +38,000 chars" is a different argument for splitting
            # than either alone.
            crossed, ceilings = [], []
            if over_ratio:
                crossed.append(f"{ratio:.1f}x the size round {first_round} reviewed it "
                               f"at, past the {limit:g}x `max_fix_growth` ceiling")
                ceilings.append(f"{limit:g}x `max_fix_growth`")
            if over_chars:
                crossed.append(f"{grown:,} chars bigger than round {first_round} "
                               f"reviewed it, past the {limit_chars:,}-char "
                               "`max_fix_growth_chars` ceiling")
                ceilings.append(f"{limit_chars:,}-char `max_fix_growth_chars`")
            stop["stop"] = True
            stop["reason"] = (
                f"this PR is {' and '.join(crossed)} — {stop['reason']}, and what this "
                "needs is splitting, not another round")
            # The CONCLUSION follows the half that fired, and it is not cosmetic:
            # `config_notes` never reaches the board (see the veto render below), so
            # this list is the record's only copy of why a cycle ended. "A fix pass
            # that multiplies the change" is false of an absolute-only stop — a
            # 2,000,000-char baseline growing by 30,001 chars has a ratio of 1.02 —
            # and a board record that says it is a wrong claim about a real PR in the
            # one field an auditor reads.
            said = ("a fix pass that multiplies the change has written a second change"
                    if over_ratio else
                    "a fix pass that adds this much on top of what the cycle started "
                    "from has written a second change, whatever the ratio says")
            # The migration pointer, and it rides on the VETO rather than staying a
            # config note for the same reason. `max_fix_growth` can only be None from
            # a WRITTEN null (an absent key inherits 3.0), so this branch is exactly
            # the repo that switched "the growth check" off before #492 and has now
            # been stopped by the half it never wrote. `resolve_dials` already says so
            # in `config_notes`; that copy does not reach the board, and this is the
            # moment it matters most.
            migrated = ("" if limit is not None or not over_chars else
                        " — and note `max_fix_growth: null` switches off the MULTIPLE "
                        "only: `max_fix_growth_chars` is the half that stopped this, "
                        "and nulling it too is the pre-#492 no-growth-check-at-all")
            stop["veto"] = [*stop["veto"],
                            f"the PR's {pr_chars:,} chars (whole PR) "
                            f"against round {first_round}'s {first_chars:,} "
                            f"({first_scope}) is {ratio:.1f}x and +{grown:,} chars, "
                            f"past the {' and '.join(ceilings)} "
                            f"ceiling{'s' if len(ceilings) > 1 else ''} — {said}, and "
                            f"this stop is that measurement, not convergence{migrated}"]
            # Explicit rather than inferred from the veto line: `confident` was already
            # computed inside `round_stop`, so appending to `veto` here changes nothing
            # about it — and a verdict this file forces to `stop` must never be able to
            # carry the flag that means "nothing left to find".
            stop["confident"] = False
    stop["fix_growth"] = growth

    # ---- recurrence (#67): is this finding standing where the last fix pass was
    # working, on a complaint that pass was sent to answer?
    #
    # The third question over the same two inputs. `new_this_round` asks whether
    # anyone raised this before; `provenance` asks whether the fix pass wrote the
    # line it sits on; this asks whether the fixes are making PROGRESS — a fix that
    # patches a wrong assumption produces the next round's findings, and a fix that
    # removes the assumption does not.
    #
    # NOTHING READS IT TO STOP A RUN, and that is still a decision rather than an
    # omission — but it is now a NARROWER decision than the one this comment used to
    # record, and #489 is why. What it said was that #67 asks for the instrument
    # before the gate in as many words — two pull requests in one day is an
    # observation, not a calibrated rule — so recurrence is absent from `round_stop`,
    # from `stop["veto"]` (which decides `confident`) and from every ceiling in
    # `panel_caps`, and that a few dozen cycles of it are what would justify wiring it
    # to anything. That reasoning had been applied to PROVENANCE too, since neither
    # tally reached the stop rule; the two are not in the same position and the gap
    # was never argued, only inherited.
    #
    # Provenance's cycles came in and it is wired: `escalate_on.fix_injection` reads
    # `provenance_counts` above and ends a cycle on it (`round_stop`'s docstring has
    # the numbers and the whole argument). RECURRENCE STAYS EXACTLY WHERE IT WAS, and
    # for a reason of its own rather than for want of cycles. The replay behind
    # `_recurrence` is the disqualifying one: over 36 rounds of this board's own
    # history the mechanical bucket fires on about four new findings in five, on the
    # cycles #67 calls circling and on the ones it does not alike — a number that
    # does not separate the two populations cannot gate on the difference between
    # them, however many more cycles of it accumulate. `_recurrence`'s own docstring
    # already made that correction once, when replaying it retired a bucket called
    # `circling` in favour of `revisited`, and a gate here would be the same claim
    # coming back through the stop rule. Provenance is the one of the three that can
    # carry a threshold, because `introduced` is a floor with a known direction; this
    # is a POSITION, and a position has no direction to err in.
    #
    # The judge's per-finding `premise_counts` is the third and is nearer to earning
    # one than this: it is a witness rather than a coincidence of line numbers, and
    # 6 of 14 and 4 of 15 on the cycle #489 was filed from is the beginning of a
    # series. It is not wired here either, and the condition is the one #67 states:
    # the cycles first.
    if prior.fixed_here and not fix_diff and attributable:
        # Said, because the silence is otherwise unreadable: the previous round DID
        # name work, so an all-`unknown` tally here is the fix range failing rather
        # than a cycle that is not circling. `provenance` posts its own note on the
        # same condition; this one names the other measurement that went with it.
        notes.append("recurrence unavailable for the same reason — no fix range, so "
                     "no finding can be placed against the last pass's own lines")

    def recurrence_of(c: Canonical) -> tuple[str, str | None]:
        """`(bucket, the earlier finding it stands on)`, or `(None, None)` where the
        question does not arise — the same three cases `provenance_of` declines on,
        and declined for the same reason. A defect an earlier round already raised
        is not circling: it is the SAME complaint, which `new_this_round` already
        says and which a fix pass demonstrably did not clear."""
        if not attributable or not is_new(c):
            return None, None
        return _recurrence(c.file or "", c.line, fix_added, prior.fixed_here,
                           bool(fix_diff))

    # Memoised for the reason `is_new` is: `recurrence_of` walks `fix_added` and
    # `fixed_here` through `_same_file` on every call, and the payload asks each
    # finding twice (bucket, then the key it stands on) on top of the tally.
    recurrence_seen: dict[str, tuple[str | None, str | None]] = {}

    def recurrence_at(c: Canonical) -> tuple[str | None, str | None]:
        if c.key not in recurrence_seen:
            recurrence_seen[c.key] = recurrence_of(c)
        return recurrence_seen[c.key]

    # Over `outstanding`, exactly as provenance is counted, so the two tallies share
    # a denominator and can be read against each other.
    r_tally = Counter(recurrence_at(c)[0] for c in outstanding)
    recurrence_counts = ({b: r_tally.get(b, 0) for b in RECURRENCE}
                         if attributable else {})
    # The judge's own answer to the sharper question, counted beside the mechanical
    # one. Its denominator is the same population and its blank is the commonest
    # value — the judge was not asked, or had nothing to say — so it is counted as
    # a bucket of its own rather than left to be inferred from a shortfall.
    p_tally = Counter(c.premise_verdict or "not-said" for c in outstanding)
    premise_counts = ({b: p_tally.get(b, 0) for b in (*PREMISE_VERDICTS, "not-said")}
                      if attributable else {})
    # ---- the constructive pass (#507). The verdict above is FINAL by this line —
    # the stop rule, the growth ceiling, the Sonar gate sentence and `confident` are
    # all settled — and that ordering is the whole of the third property this
    # feature has to hold: it may only ADD to an escalation, never make one look
    # cleaner than it is. Nothing below writes to `stop`, and `propose` is handed a
    # read of it rather than the power to change it.
    #
    # It costs a fan-out, so it fires only where the issue says it is worth one: on
    # an `escalate_on` rung, which is a PR whose cycle was already ending badly.
    # `panel_propose.escalations_fired` is the whole of that test and it is the only
    # thing standing between a healthy round and a second panel's worth of tokens —
    # which is why the round the seats are asked about is the round that STOPPED,
    # never the round that is going again.
    #
    # BEFORE `clock.mark("wrapup")`, deliberately. This is minutes of wall clock and
    # it has to land inside a measured phase; after the mark it would be time the
    # round spent that `timing` attributes to nothing, which is the "remainder nobody
    # can name" that comment exists to rule out.
    #
    # Every finding it shows a seat is one the seat itself raised and this round
    # still has outstanding — `held` marks the escalated ones so the brief can say
    # whose answer they are. Nothing here reaches `round_stop`, the leaderboard, the
    # recurrence chain or the severity floors: a proposal is not a finding.
    proposals = panel_propose.propose(
        stop if cycle_run else None, outstanding, selected, models, efforts,
        held=held, armed=propose_armed, cycle_run=cycle_run)
    # In `config_notes` as well as in the block, on the premise notes' rule: the
    # payload is what an orchestrator's `jq` reads and `config_notes` is what a human
    # reads off the PR comment, and a pass that was SKIPPED because the repo switched
    # it off has to be legible in the second place too. Only for the armed-off case —
    # a note on every healthy round saying no escalation fired is the "loud and wrong"
    # a reader learns to skip.
    if not propose_armed and cycle_run and panel_propose.escalations_fired(stop):
        notes.append(
            "this round escalated and `review_panel.propose_on_escalation` is off, so "
            "the seats were not asked what they would do instead — whoever answers "
            "this escalation gets the findings and no proposal (#507)")

    # A cycle's rounds share one id, inherited from the earliest baseline, so the
    # board can tell "the re-review of THIS declaration" from "whatever ran next
    # on this PR". Only a round 1 of an actual cycle MINTS one.
    #
    # A later round whose baseline was missing, unreadable, from another PR or
    # not earlier sends null rather than a fresh id: `followed_by` requires the
    # cycles to match, so a minted one would make round 1 and round 2 of the same
    # PR two unrelated cycles forever and void every re-review declaration round 1
    # made — a permanent hole in a published measure, bought by a mistyped path.
    # Null records "unattributable", which is the truth and is recoverable.
    # A review-only run sends null too, which is what `ReviewIn.cycle` has always
    # documented ("for a standalone review that is nobody's round 2") and what the
    # producer never emitted.
    cycle = prior.cycle or (run_key if cycle_run and round_no == 1 else None)

    def loc(x: Canonical | Finding) -> str:
        return f"{x.file}:{x.line}" if x.line else x.file

    # ---- the run, as data. Built on every path, not just --json: it is what
    # --json prints, what --json-file writes, and what gets recorded on the
    # board. One structure, so the fix loop and the stats can never be looking
    # at different accounts of the same review — and one finding record per
    # defect, carrying every reviewer's own report, so a consumer reads the merge
    # instead of re-deriving it from an over-counted list.
    # Everything between the judge returning and this line: provenance, the stop
    # rule, the growth ceiling, the escalation register. Closed here so the four
    # phases partition the round exactly rather than leaving a remainder nobody
    # can name — `measured_to` says what is outside them.
    clock.mark("wrapup")
    # The fix phase that ran INTO this round (#192). Its earlier end comes from the
    # previous round's own recorded finish where there is one, and is derived from
    # the two rounds' head commit times where there is not — `fix_phase` records
    # which, because the derivation is a lower bound and breaks in the two places
    # it would matter most. `cfg["path"]` is the checkout the derivation reads; on
    # a host that has no clone of this repo it simply reports that it could not.
    timing = panel_timing.timing_block(
        clock,
        panel_timing.fix_phase(clock.started_at,
                               prior_finished_at=prior.finished_at,
                               prior_round=prior.finished_round or prior.head_round,
                               prior_head_sha=prior.head_sha,
                               head_sha=head_sha,
                               repo_path=cfg.get("path") or ""))
    payload = {
        **_payload_defaults(),
        "repo": repo_name, "github": gh_repo, "pr": pr_number,
        "title": title, "base": base, "changed_lines": changed,
        # Where the wall clock went, and what the round spent waiting on one thing
        # (#192). Board ingest is `extra="ignore"`, so this key is dropped there
        # until a column exists for it — it travels in `--json`/`--json-file`,
        # which is what a cycle chains its rounds through, and `finished_at` is
        # read straight back out of it by the NEXT round's `load_baseline` as the
        # left-hand end of that round's fix phase.
        "timing": timing,
        # The commit reviewed: the NEXT round anchors its increment on it, and
        # provenance measures its fix range to it. One key, because two would be
        # one fact with two chances to disagree.
        "head_sha": head_sha,
        # Both ends of what this round was judged against (#98). `merge_base` is
        # the PR's base commit — read `scope` before calling it this round's own
        # anchor, which under increment scope is `since_sha`. `base_sha` is where
        # the base branch had got to while the round was being read.
        "merge_base": merge_base,
        "base_sha": base_sha,
        "unread_files": unread_files,
        "changed_files": changed_files, "changed_files_total": changed_files_total,
        "pr_state": pr_state, "is_draft": is_draft,
        # True here, and no longer the only value the BOARD sees. This comment
        # used to read "always True in a payload the board sees", which was
        # already untrue when it was written: the pre-flight refusal path sends
        # `reviewed: false` and IS recorded, so the board has been receiving
        # "nothing was reviewed" and discarding it for several releases. It
        # stores the field since #94, and the title-pattern skip records too, so
        # all three exits now say what they are on the board as well as in
        # `--json`.
        "reviewed": True,
        "diff_truncated": truncated,
        # Where this run sits in the panel -> fix -> panel cycle, and what the
        # mechanical stopping rule made of it.
        "round": round_no,
        "cycle": cycle,
        # What was reviewed, and against what. Sent even under "pr" scope, where
        # `since_sha` is null: a consumer must be able to tell a round that chose
        # whole-PR scope from one written before scope existed, and the second one
        # sends no key at all.
        # `target_scope`, not `review.scope`: a manifest round's material is a
        # whole-target "pr" composition of an increment, and what this field means
        # is what the round REVIEWED. Read `preflight.verdict` beside it to know
        # whether the round read that target or a description of it.
        "scope": target_scope,
        "since_sha": review.since or None,
        "prior_rounds": prior_rounds,
        "prior_findings": len(prior_keys),
        # Gated on there being a cycle, exactly as the report's Rounds block is.
        # For a review-only run `len(new_keys)` is every finding — the vacuous
        # count "raised by no earlier round" when there was no earlier round — and
        # `round_stop` is a verdict about a loop nobody is running. None rather
        # than 0, because the board's column already means "the panel did not
        # say", and a zero there is a claim.
        "new_findings": len(new_keys) if cycle_run else None,
        "new_finding_keys": new_keys if cycle_run else [],
        "round_stop": stop if cycle_run else None,
        "stop_reason": stop["reason"] if cycle_run else None,
        # #507. Beside `round_stop` and emphatically not inside it: what the seats
        # would DO is not part of the verdict, and a consumer reading the verdict
        # must not have to step over a proposal to reach it. The board's ingest is
        # `extra="ignore"`, so this key is dropped there until a column exists for
        # it — which is the first property enforced by the plumbing rather than by
        # anyone remembering it: a proposal cannot reach the leaderboard, the
        # recurrence chain or the finding table, because it is not a finding and
        # never travels as one.
        "proposals": proposals,
        "coverage_note": coverage_note or None,
        # #547's ledger. Every claim this round established that nothing in it could
        # check, whether or not somebody has accepted it — the acceptance is a
        # separate field, so a reader can tell "no unverifiable claims" from "all of
        # them signed off" without holding two payloads side by side.
        #
        # It is the record that makes the exemption safe to grant. A judge ruling a
        # declaration unresolvable does not delete it; it moves it here, under a key,
        # with what would settle it beside it. A claim that vanished without landing
        # on this list would be the model-authored bypass Part 2 of #547 exists to
        # prevent, and the test that pins it asks exactly that question.
        "unresolved_claims": [{"key": ob.key, "claim": ob.claim, "reason": ob.reason,
                               "acknowledged": ob.key in ack_held}
                              for ob in obligations],
        # key -> the round it was first acknowledged in, inherited by the next round
        # through --baseline exactly as `escalated` is.
        "acknowledged": ack_held,
        # The REVIEW TARGET's size — the whole PR under "pr" scope, the increment
        # under "increment". Its meaning is scope-dependent and always has been
        # in spirit ("how big was the thing we reviewed"); what is new is that
        # the answer can now be smaller than the PR, so `scope` must be read
        # beside it. A consumer plotting this across a cycle's rounds without
        # reading `scope` will see a cliff at round 2 and call it a shrinking PR.
        #
        # This pair is what the round PREPARED, which is what a reviewer with no
        # budget was given. It is deliberately not "what each reviewer read":
        # budgets are per reviewer, so there is no single true number for that —
        # `reviewers.<name>.max_diff_chars` and `.truncated` carry the per-seat
        # answer, and a seat that got the whole target and only part of the
        # context is named in `config_notes`.
        "diff_chars": len(review.target),
        # The WHOLE PR's size, whatever this round reviewed — the growth ceiling's
        # own measurement (#298) and the one number in this group that means the same
        # thing on every round of a cycle. `diff_chars` above is scope-dependent by
        # design, so a later round reading it as "how big is this PR now" is handed a
        # fix commit; plotted across a cycle, this is the line that does not cliff at
        # round 2. Equal to `diff_chars` under "pr" scope, and under a manifest round
        # both measure the manifest, which is what was sent.
        "pr_chars": len(review.diff),
        # Everything prepared ALONGSIDE the target: 0 under "pr" scope, where
        # there is no such thing. This plus `diff_chars` is what a round put in
        # front of an uncapped reviewer, and the pair is the measurement issue #41
        # exists to produce.
        "context_chars": len(review.near) + len(review.far),
        # What the round was weighed against before it ran, on EVERY reviewed run
        # and not only on a refused one. A `run` verdict recorded beside the
        # findings is what makes the refused ones countable: "the panel weighed
        # this and proceeded" and "the panel never weighed it" are otherwise the
        # same silence, which is the failure this whole feature is about. It also
        # carries the review target's pre-substitution size under a manifest round,
        # where `diff_chars` measures the manifest.
        "preflight": pre.as_dict(),
        # Every SELECTED seat still has a key here, as it always has — a seat this
        # box cannot run records `null` rather than vanishing (#222). The internal
        # `budgets` dict genuinely OMITS it, and has to: everything that iterates
        # that dict (`composed`, `truncated_for`, the argv clamp) reads a null as
        # "uncapped" and would compose the whole diff for a seat that never ran.
        # The payload has no such reader and one shape to keep — a board or
        # dashboard doing `payload["diff_budgets"][name]` for a configured seat
        # must not start raising KeyError on exactly the unattended hosts this fix
        # is for. `null` is the same answer `reviewers.<name>.max_diff_chars`
        # already gives for that seat, and `reviewers.<name>.absent` is what says
        # which kind of null it is.
        "diff_budgets": {**{n: None for n in LLM_REVIEWERS if n in selected},
                         **budgets, "judge": judge_budget},
        "config_notes": notes,
        "sonar_gate": result.sonar_gate,
        "ci_status": ci_status,
        "ci_failing": ci_failing,
        # #548's recorded state. `ci_status` already carries the verdict; this
        # carries what produced it — which commands, how long they took, and what
        # CI state they stood in for — so that a `local-pass` round can be told
        # apart from a `PASS` one by a reader six weeks later without the notes.
        # Null on a round that ran none, which is every round on a repo that has
        # not declared a suite.
        "local_suite": local_record,
        "judged": judged,
        "judge_model": panel.get("judge_model", "") or None,
        "judge_skip": judge_skip,
        "reviewers_ran": ran_llm,
        "reviewers": reviewer_meta,
        # Whether the seats could read the code, at the grain a later comparison
        # needs (#113). `setting` is what the repo (or --no-code-access) asked for;
        # `seats` is who actually got it, read back from what each seat RECORDED
        # rather than from the intent — a fetch or a copy that failed leaves the
        # setting on and the seat blind, and only the second is true of the round.
        #
        # Both, because they answer different questions. Comparing rounds across
        # the change needs the setting; reading one round's coverage needs the
        # seats. And a repo that turned it on while every seat it enables is
        # code-blind is a configuration doing nothing, which is visible in the
        # difference and invisible in either half alone.
        "code_access": {
            "setting": want_code,
            "seats": sorted(n for n, m in reviewer_meta.items()
                            if m.get("ran") and m.get("code_blind") is False),
            # What the strip took out of the reviewers' checkout. `[]` is "the PR
            # carried none", which is a different fact from the null a round that
            # never fetched a tree records.
            "convention_files_removed": stripped if code_tree_used else None,
        },
        "reviewers_selected": sorted(selected),
        "reviewers_override": override_note,
        # `new_this_round` is added HERE rather than on the record: it is a fact
        # about this run's comparison against a baseline, not a property of the
        # defect, and a Canonical that carried it would have to be told about a
        # baseline to know its own shape.
        # `provenance` rides beside `new_this_round` and for the same reason: both
        # are facts about this run's comparison against a baseline rather than
        # properties of the defect, and a Canonical that carried either would have
        # to be told about a baseline to know its own shape.
        # key -> the round it was first declared escalated in. The next round
        # inherits it through `--baseline`, so a cycle cannot lose an open premise
        # question by forgetting to re-pass a flag.
        "escalated": held,
        # On the finding, not only in the register beside it: this is what a
        # fixer's brief is built from, and §5's rule is that an escalated finding
        # is never handed to another fixer. On EVERY bucket, not just `to_fix`:
        # `outstanding` is `to_fix + sonar`, so a Sonar gate issue whose key is in
        # the register is already subtracted from the work `round_stop` counts,
        # and a record that showed it as ordinary work would contradict the stop
        # rule that acted on it. `dismissed` is not work at all, but it is keyed
        # the same way — same fields, in the same ORDER, so a consumer reading one
        # bucket's shape does not find a different one in the next and a payload
        # diff does not move three keys around.
        #
        # `escalated` is always FALSE on a dismissed finding, and that is not an
        # oversight: the master ruled it not real, so "escalated — awaiting a human"
        # says the opposite about the same row. Nothing else treats it as escalated
        # either — it is not in `outstanding`, so no stop rule consults it, and the
        # report renders ⛔ only in the two lists a fixer's brief can be built from.
        # A caller who does escalate a dismissed key gets a `config_notes` line
        # saying so, rather than a record that contradicts the report.
        # `below_fix_floor` rides beside `escalated` and for the same reason: it is
        # the other way a confirmed, outstanding finding is deliberately NOT this
        # round's work, and a programmatic consumer building a fixer's brief has to
        # be able to see it without re-deriving the floor. On every bucket, in the
        # same position, on the same rule the comment above states — except
        # `dismissed`, where it is always False for the reason `escalated` is: the
        # master ruled it not real, so "below the fix floor" would describe work that
        # does not exist. `review_panel` below records the floor it was computed
        # against.
        # `budgeted_fix` is the third of that family and the one #297 adds: not "NOT
        # this round's work" but "this round's work only while the line budget lasts",
        # which is a third answer and cannot be spelled by the other two. Always
        # False on a SONAR issue, where the other two flags are computed: neither
        # floor applies to a hard-gate issue at any rule, so a budget that could
        # decline to fix one would say the panel may leave a red quality gate red.
        "to_fix": [{**c.as_dict(), "new_this_round": is_new(c),
                    "provenance": provenance_of(c),
                    "recurrence": recurrence_at(c)[0],
                    "recurs_of": recurrence_at(c)[1],
                    "escalated": c.key in held,
                    "below_fix_floor": below_floor(c),
                    "budgeted_fix": budgeted(c)} for c in to_fix],
        "sonar_findings": [{**c.as_dict(), "new_this_round": is_new(c),
                            "provenance": provenance_of(c),
                            "recurrence": recurrence_at(c)[0],
                            "recurs_of": recurrence_at(c)[1],
                            "escalated": c.key in held,
                            "below_fix_floor": below_floor(c),
                            "budgeted_fix": False} for c in sonar],
        "dismissed": [{**c.as_dict(), "new_this_round": is_new(c),
                       "provenance": provenance_of(c),
                       "recurrence": recurrence_at(c)[0],
                       "recurs_of": recurrence_at(c)[1],
                       "escalated": False,
                       "below_fix_floor": False,
                       "budgeted_fix": False} for c in dismissed],
        # The eight #165/#297 dials AS APPLIED, not as written: a repo whose
        # `fix_severity_floor` was rejected reads the floor that actually ran here
        # and the reason it was rejected in `config_notes`. Every key present on
        # every reviewed round, so a consumer never has to tell "the default applied"
        # from "a payload written before the field".
        "review_panel": dials.as_dict(),
        # …and which layer supplied each of them (#305). `review_panel` says what
        # ran; this says which of the four layers said so, so a round that ran under
        # a moved floor names the mover instead of leaving a reader to infer it from
        # three files and a resolution order.
        "rules": rules_record(cfg),
        "provenance_counts": provenance_counts,
        # WHICH range answered (#512): `increment` — the diff this round actually
        # reviewed — or `compare`, the separate API fetch used under `pr` scope and
        # wherever the increment fell back. `null` where the question does not arise
        # (round 1). Published because the two are not the same measurement: the
        # increment drops a base-branch merge's files and the compare range does
        # not, so a reader comparing `introduced` across rounds has to be able to
        # see that the denominator's provenance changed under them.
        "fix_range_source": fix_range_source,
        # #504's working, published rather than left in a sentence. `null` on every
        # round that did not have to rebuild — which is nearly all of them — and
        # otherwise the commits it named, how many the last round had reviewed, how
        # many of those came through the rewrite intact, and `why` when it declined.
        # `prior`/`carried`/`unmatched` are the correspondence it found. On a round
        # that ATTRIBUTED they are always `n`/`n`/`0` — an inexact reconstruction
        # declines rather than leaning — so what they are worth is on the declines,
        # where they say how far off the two histories were and whether a rebase or a
        # force-push is what an operator should go and look at.
        #
        # WITHOUT the diff, which is the one key here that is not a fact about the
        # round but a copy of the fix pass — up to `FIX_RANGE_MAX_CHARS` of it. This
        # payload is written to a file, passed as the next round's `--baseline` and
        # recorded on the board, and none of those wants a megabyte of patch riding
        # along; nothing downstream reads it, because everything that needed it read
        # `fix_added` above. `why` still tells a decline from a success.
        "fix_range_rebuilt": ({k: v for k, v in rebuilt.items() if k != "diff"}
                              if rebuilt else None),
        # #490's block as data, so a board or an orchestrator reading the payload
        # gets the trend without re-deriving it from every earlier round's file.
        # One row per round INCLUDING this one, and rebuilt from the raw per-round
        # fields every round rather than chained from the last round's copy — a
        # cycle with a skipped round, or one that spans the release that added this,
        # still gets a complete block instead of a tail.
        # Gated on there being a CYCLE, exactly as the report's Rounds block and
        # `new_findings` are: a review-only run has no cycle, so a "cycle trend"
        # carrying its single row is a claim about a loop nobody is running.
        "cycle_trend": ([_trend_record(t, trend_first_chars) for t in trend_rows]
                        if cycle_run else []),
        "recurrence_counts": recurrence_counts,
        "premise_counts": premise_counts,
        # #492, and measured on EVERY round including the first — which is the
        # point of it. `max_fix_growth` needs two rounds before it has a ratio at
        # all; how much apparatus a change is carrying is answerable from round 1's
        # diffstat, and one cycle produced 406 lines of test for a 66-line config
        # change with nothing in the panel noticing. `review.diff` is the whole PR
        # under either round scope, the same end the growth ceiling reads.
        "guard_ratio": guard_ratio(review.diff),
        "skipped": result.skipped,
        "run_key": run_key,
    }

    # Recorded BEFORE the file is written, which is the ordering the fix needs
    # rather than a preference: `record_run` now answers whether the board took
    # the run, and `notes` IS `payload["config_notes"]` — so appending here puts
    # "this round was NOT recorded" into the payload on disk, into `--json`, and
    # into the report and PR comment below, instead of into a stderr line in a
    # subprocess nobody reads (#284). The board is sent the payload as it stood,
    # which is right both ways round: if it answered, it has the run and the note
    # is false; if it did not, there is nothing to have received the note.
    if record:
        missed = record_run(payload)
        if missed:
            notes.append(missed)

    # #274: the round that formed the judgement is the one that announces it.
    # After the record and before the render, so the board has the run this post
    # points at; a failure here is a note in the same list, never an exception —
    # a review that ran is not undone by a board that would not take a post.
    #
    # Under `record`, because `--no-record` is a caller saying this run does not
    # go on the board, and an escalation post is a board write like any other.
    # A preview or a dry read that silently interrupted somebody would be the
    # same surprise in the opposite direction from the one #274 is fixing
    # (Codex).
    if record:
        notes.extend(announce_escalations(payload, cfg))

    # So a caller can have BOTH the PR comment and the machine-readable run.
    # Without --json-file, --json suppresses the report and the only way to get
    # both was to review the PR twice — several CLI invocations, for a copy. A
    # requested file that could not be written FAILS the run (see `finish`).
    write_failed = write_payload(json_file, payload)

    # ---- machine-readable mode: the whole run as JSON, no report/post. Same
    # shape as the skip-pattern exit's payload, so a consumer can read any key of
    # either without checking which exit it came from.
    if json_out:
        print(json.dumps(payload, indent=2))
        return finish(write_failed)

    # How many LLM seats the run was CONFIGURED to fill, against how many filled.
    # Both halves are needed and neither is derivable from the other: "claude ran"
    # is the same sentence whether it was the only seat asked for or the only one
    # of four that answered, and those are a hand-picked single-vendor read and a
    # panel that lost three quarters of its eyes.
    seats_asked = [n for n in LLM_REVIEWERS if n in selected]
    seats_filled = len(ran_llm)
    # A seat whose CLI this box does not carry is NOT a degraded panel, and this
    # is the same distinction `coverage_veto` makes at length a few hundred lines
    # up: an absent CLI is a fact about the HOST, not about the round. It is
    # absent every run, so counting it as degradation prints the warning on every
    # unattended run of a repo that enables a workstation-only vendor — where
    # nothing was lost and nothing could be recovered. That is the alert fatigue
    # `test_a_full_panel_says_none_of_it` exists to prevent, and it would take the
    # real degraded case down with it. Read off recorded state, never off the skip
    # TEXT, for the reason `ReviewerRun.absent` was added.
    seats_absent = [n for n in seats_asked if reviewer_meta.get(n, {}).get("absent")]
    seats_lost = len(seats_asked) - seats_filled - len(seats_absent)
    # The consensus signal needs two seats to exist AT ALL. Below that, "no
    # finding earned ⋆consensus" and "there was nobody to agree with" render
    # identically, and a reader takes the first meaning — the pessimistic
    # reading of a review that never had the chance to be pessimistic.
    #
    # Counted over everything that can FILE a finding, not over the LLM seats:
    # sonarqube's base-branch issues are judged alongside them (`llm_findings`
    # takes `soft`), so a canonical finding's `reviewers` can legitimately read
    # ["claude", "sonarqube"]. Counting LLM seats alone let `conf()` stamp
    # ⋆consensus on that finding while the header two dozen lines below declared
    # consensus impossible — the report contradicting itself in the exact place
    # this was added to stop it being misread.
    #
    # `sonar_filed`, not the gate STATUS, and the distinction is the same one #62
    # spent three rounds on: a status is a side effect, not the thing itself. Only
    # the `no-pr-analysis` fallback yields soft findings that can share a canonical
    # record — the scanned paths return `hard`, which renders in its own section
    # and never reaches `conf()`. So keying on the gate over-counted at one end (a
    # scanned "OK" repo with one LLM seat suppressed both the banner and the
    # sole-reviewer note, on findings nobody could corroborate) and under-counted
    # at the other ("ERROR" can still return hard findings, so the report could
    # claim nobody reviewed while Sonar issues were displayed beneath it).
    filers = seats_filled + (1 if sonar_filed else 0)
    consensus_possible = filers > 1

    def conf(c: Canonical) -> str:
        revs = c.reviewers
        if len(revs) > 1:
            return f" _(via {', '.join(revs)} ⋆consensus)_"
        # Said per finding rather than once at the top, because this is the line a
        # reader is looking at when they decide how much a finding is worth.
        sole = " — sole reviewer, no second opinion" if not consensus_possible else ""
        return f" _(via {', '.join(revs)}{sole})_"

    def escalation(c: Canonical) -> str:
        """The ⛔ mark, in every list a fixer's brief can be built from.

        The finding is still outstanding and still shown — hiding it would lose
        the question — but it is not this round's work, and §5's rule is that it
        is never handed to another fixer. Both the judged findings and the Sonar
        gate issues get it, because `outstanding` is the two of them together and
        a mark on only one would say the stop rule counted something the report
        presents as ordinary work."""
        return (f" ⛔ _escalated in round {held[c.key]} — awaiting a human, "
                "not a fix pass_" if c.key in held else "")

    def accounts(c: Canonical) -> list[str]:
        """What each reviewer actually said, under a MERGED finding.

        The synthesis is the judge's statement of the issue; these are the
        reports it was made from, and they are shown because one reviewer
        routinely makes a point the others didn't. Truncated here (the whole
        report is a PR comment) but kept whole in `--json` and on the board."""
        if len(c.reported_by) < 2:
            return []
        out = []
        for f in c.reported_by:
            said = _account(f)
            cut = said[:ACCOUNT_CHARS] + ("…" if len(said) > ACCOUNT_CHARS else "")
            out.append(f"  - _{f.reviewer}_ ({f.severity} `{loc(f)}`): {cut}")
        return out

    # ---- report
    heading = f"## Reviewer panel — PR #{pr_number}"
    # One predicate for the heading and the summary beneath it: a baseline that
    # parsed but held no findings is still an earlier round, and used to produce a
    # "· round 1" heading with nothing under it.
    in_rounds = round_no > 1 or bool(prior_rounds)
    if in_rounds:
        heading += f" · round {round_no}"
    lines = [heading, ""]
    if in_rounds:
        # Counted over everything the cycle has to clear (Sonar's hard gate
        # included), so the numerator and the denominator are the same population.
        lines.append(f"**Round {round_no}** — re-reviewing after the fix. "
                     f"{len(new_keys)} of {len(outstanding)} finding(s) here were raised by "
                     f"no earlier round ({len(prior_keys)} known from {prior_rounds} earlier "
                     f"round{'s' if prior_rounds != 1 else ''}).")
        # The split those "new" findings hide. Printed rather than left to the
        # payload because the operator deciding whether to go again is the one
        # the distinction is FOR: findings the fix pass created argue for a
        # smaller next pass, findings the last round missed argue for more
        # coverage, and the two read identically as a count.
        pc = payload["provenance_counts"]
        # Only the buckets with something in them, and nothing at all when the
        # only populated bucket is `unknown`. Leading with "**0 introduced**,
        # **0 missed**" under a config note explaining that nothing could be
        # attributed reads as a bolded claim about the fix pass, and a false one.
        phrasing = {"introduced": "**{n} introduced** by the last fix pass",
                    "missed": "**{n} missed** by the last round",
                    "missed-unread": "{n} in files that round could not read",
                    "unknown": "{n} unattributable"}
        if any(pc.get(b) for b in PROVENANCE if b != "unknown"):
            detail = [t.format(n=pc[b]) for b, t in phrasing.items() if pc.get(b)]
            lines.append("  - of those: " + ", ".join(detail)
                         + ". A signal, not a verdict — a fix can break something at a "
                           "distance, so `missed` is evidence rather than proof.")
        # #67, and printed on the same rule and for the same reason: the operator
        # deciding whether to go again is the one the distinction is FOR.
        #
        # It says what it is and stops. No recommendation, no threshold crossed,
        # no stop. And it deliberately does not read as an accusation, because the
        # replay behind `_recurrence` says the mechanical half cannot support one:
        # on 36 rounds of this board's history it fires on about four new findings
        # in five, on the cycles #67 calls circling and on the ones it does not
        # alike. The number is a rate worth watching accumulate; the sentence that
        # would tell an operator to stop is not available yet, and printing one
        # would be inventing it.
        rc = payload["recurrence_counts"]
        revisited, at_site = rc.get("revisited") or 0, rc.get("fix-site") or 0
        if revisited or at_site:
            said = [f"**{revisited}** where the last round complained and the fixer "
                    "then worked" if revisited else "",
                    f"{at_site} where the fixer worked and nobody had complained"
                    if at_site else ""]
            lines.append("  - on the last fix pass's own lines: "
                         + ", ".join(s for s in said if s)
                         + ". A position, not a verdict (#67) — a round past the "
                           "first is reading the fix commit, so this is usually most "
                           "of them. Nothing stops on it.")
        # What the judge said when asked the sharper question directly, and the half
        # that can actually see a repeated premise: the mechanical count above knows
        # the fixer was working here and not whether this finding says its
        # assumption was wrong. Reported apart rather than folded in — two
        # witnesses, and a round where they disagree is the row worth reading.
        mc = payload["premise_counts"]
        if mc.get("invalidates") or mc.get("unclear"):
            said = [f"**{mc['invalidates']}** contradict the premise of the fix before "
                    "them" if mc.get("invalidates") else "",
                    f"{mc['unclear']} it could not tell" if mc.get("unclear") else ""]
            lines.append("  - the judge, asked per finding: "
                         + ", ".join(s for s in said if s)
                         + f", {mc.get('separate', 0)} separate defects. A fix that "
                           "patches a wrong assumption produces the next round's "
                           "findings; one that removes it does not.")
        # #490, and the last thing in this block on purpose: everything above is
        # THIS round, and this is the cycle. A reader who stops at the round summary
        # has the report they always had; a reader who does not gets the arithmetic
        # that took ninety seconds by hand on the cycle this came from.
        #
        # It cannot stop anything and nothing consults it — see `cycle_trend_lines`.
        lines.extend(cycle_trend_lines(trend_rows, prior.first_reviewed))
    # ---- guard-to-guarded (#492), and it is printed OUTSIDE the `in_rounds` block
    # above deliberately. Provenance and recurrence are questions only a later round
    # can ask; this one is answerable from round 1's diffstat, and round 1 is where an
    # operator can still act on it cheaply — the whole complaint behind the issue is
    # that the growth ceilings bind a round late.
    #
    # It says what it is and stops (#67, exactly as recurrence above does). Nothing
    # gates on it, no threshold is crossed, no stop. A ceiling here would be a number
    # invented today with its argument written afterwards, and this repo's rule is
    # that an instrument earns a gate over a few dozen cycles or not at all.
    gr = payload["guard_ratio"]
    if gr:
        # The split is printed beside the ratio because `guard` alone cannot say
        # whether a change grew its tests or grew its prose, and those argue for
        # different things.
        against = (f"{gr['source']} source — **{gr['ratio']:g}:1**"
                   if gr["ratio"] is not None
                   else "no source lines at all — no ratio to take")
        lines.append(f"**Guard-to-guarded:** {gr['test']} test + {gr['doc']} doc "
                     f"line(s) added against {against}. Reported, not a threshold — "
                     "nothing stops on this (#67).")
    # ---- refereed-ness (#554), and it is the one instrument on this report that DOES
    # gate — so it says so, rather than letting a reader carry the line above's
    # "nothing stops on this" across two paragraphs. What it gates on is a predicate
    # and not a threshold, which is the whole reason it may gate at all (#67); the
    # numbers are printed either way so that a reader can check the verdict rather
    # than take it, exactly as `fix_injection` publishes its rate beside its limit.
    #
    # Printed only where there was a fix pass to read. On round 1, and on a round
    # whose range was unreadable, `churn` is 0 and there is no measurement — a line
    # reading "0 test, 0 prose, 0 production" would say a pass wrote nothing when in
    # fact none was looked at.
    rf = (payload.get("round_stop") or {}).get("unrefereed_fix") or {}
    if rf.get("churn"):
        verdict = ("**no production code at all** — nothing in the loop can check it"
                   if not rf["production"] else
                   f"{rf['production']} production line(s), which red/green, the "
                   "suite and CI can each catch being wrong")
        armed = ("" if rf["armed"] else
                 " `escalate_on.unrefereed_fix` is off for this repo, so this is "
                 "recorded and gates nothing.")
        lines.append(f"**Refereed-ness of the last fix pass:** {rf['churn']} churned "
                     f"line(s) — {rf['test']} test, {rf['prose']} prose, "
                     f"{verdict}.{armed}")
    ci_txt = {"PASS": "✅ PASS", "FAIL": "❌ FAIL", "PENDING": "⏳ pending",
              "blocked": "🚧 gated — a run exists and will not execute without a human",
              "none": "🚫 no run exists for this commit",
              "unknown": "❓ could not be determined",
              LOCAL_PASS: "✅ passed LOCALLY — no GitHub run exists for this commit",
              LOCAL_FAIL: "❌ failed LOCALLY — no GitHub run exists for this commit",
              LOCAL_UNREAD: "❓ run locally and produced no result — no GitHub run "
                            "exists for this commit"}.get(ci_status, ci_status)
    # The heading names the SOURCE, because "hard gate" is a claim about what
    # `preland` will refuse and it is false of a local run: `check_ci` reads GitHub,
    # so nothing a suite does on this box is a gate at all. A local answer under a
    # heading promising one is precisely the reading `_ci_line` refuses (#548).
    from_local_suite = ci_status in LOCAL_STATES
    lines.append(f"**CI (`gh pr checks`, hard gate):** {ci_txt}" if not from_local_suite
                 else f"**Test suite (the repo's own, run locally — NOT a gate):** {ci_txt}")
    if ci_failing:
        lines.append(("  - failed: " if from_local_suite else "  - failing: ")
                     + ", ".join(ci_failing[:10])
                     + (f" (+{len(ci_failing) - 10} more)" if len(ci_failing) > 10 else ""))
    # Every state that is not PASS, since #324. "no checks reported" used to print
    # with no warning under it at all, which is how an absent result read as a
    # benign one — the three added here are the three that cannot fix themselves.
    #
    # And `local-pass` is warned about too, which is the one line in this block that
    # is not about a missing or failing suite: it is green, and it is still not the
    # thing the merge gate reads. A pass under no warning at all is how a reader
    # concludes the PR is mergeable from a run that has no bearing on whether it is.
    if from_local_suite:
        lines.append("  - ⚠️ this is the LOCAL suite and not CI — a different "
                     "machine, possibly different service versions, and nothing "
                     "says this is the commit that will merge. The merge gate reads "
                     "GitHub, which still reports nothing for this commit")
    elif ci_status != "PASS":
        lines.append(f"  - ⚠️ CI is not green ({ci_status}) — do not merge, even if "
                     "the review below is clean")
    gate_txt = {"OK": "✅ PASS", "ERROR": "❌ FAIL"}.get(result.sonar_gate, result.sonar_gate)
    if result.sonar_gate in ("OK", "ERROR"):
        lines.append(f"**SonarCloud quality gate (hard):** {gate_txt}")
    elif result.sonar_gate == "no-pr-analysis":
        lines.append("**SonarCloud:** PR not scanned — base-branch issues on the "
                     "lines this PR adds, surfaced as soft findings (judged). "
                     "No hard gate: publish a PR analysis to get one.")
    else:
        lines.append(f"**SonarCloud:** {gate_txt}")
    # The seat count rides with the reviewer list on EVERY run, not only degraded
    # ones. A round's finding count is not comparable across different panel
    # sizes, and the convergence table this repo keeps has been read as if it
    # were: #32 went 22 -> 43 between rounds and gained a reviewer in the same
    # step. The number that disambiguates it has to be in the artifact.
    lines.append(f"**LLM reviewers ran:** {', '.join(ran_llm) or 'none'}"
                 f" — {seats_filled} of {len(seats_asked)} configured")
    # The panel-level version of #19's per-reviewer fix. #19 stopped a reviewer
    # that produced nothing from reading as a reviewer that found nothing; this
    # stops a PANEL that lost half its seats from reading as a panel that agreed.
    # A run with empty seats is a materially weaker artifact than a full one and
    # was presented identically — on PR #64 that meant 23 findings from a single
    # reviewer, whose own master wrote that nine self-declared coverage gaps
    # "stand unchallenged and unread", laid out exactly like 23 from a full panel.
    # It is stated here, above the findings, rather than in a footer: under the
    # epic (#52) nobody is reading this in a terminal as it happens.
    if seats_lost > 0:
        lines.append(f"  - ⚠️ **panel degraded** — {seats_lost} of {len(seats_asked)} "
                     f"configured reviewer{'s' if len(seats_asked) != 1 else ''} did not "
                     "run. Read what follows as a weaker review, not a cleaner one: "
                     "an empty seat cannot report what it would have found.")
    # Above the findings for the same reason the line before it is: a reader who
    # takes a manifest round's findings for a content review reads "no correctness
    # findings" as "the moved code is correct". Nobody read the moved code.
    if pre.verdict == "manifest":
        lines.append(f"  - ⚠️ **reviewed as a MOVE MANIFEST, not as a diff** — "
                     f"{pre.shape.moved:,} of "
                     f"{max(pre.shape.added, pre.shape.removed):,} changed lines "
                     f"({pre.shape.move_ratio * 100:.1f}%) are relocated text, and the "
                     f"diff is {pre.measured:,} {pre.cap_unit} against "
                     f"{pre.cap_seat}'s "
                     f"{pre.cap:,}. The seats were asked what MOVED, what did not "
                     "survive, and what changed besides moving. **The moved code "
                     "itself was not read by anybody** — treat its correctness as "
                     "carried over from when it landed on the base branch, not as "
                     "reviewed here.")
    if pre.forced and pre.gate:
        # A forced PRECONDITION refusal, and the size sentence below would be a
        # non-sequitur about it — there is no ceiling in this verdict, `pre.cap` may
        # be None, and the reader's problem is not that the seats were spread thin
        # but that the code they read is going to change before it lands.
        lines.append("  - ⚠️ **`--force` overrode a pre-flight `refuse` verdict** — "
                     f"{pre.reason.removeprefix('--force: ')}. The panel refused "
                     "this round and was overruled: what follows is a review of a "
                     "branch whose merged form does not exist yet, so read every "
                     "finding as provisional on the rebase.")
    elif pre.forced:
        lines.append(f"  - ⚠️ **`--force` overrode a pre-flight `{pre.would_have}` "
                     f"verdict** — the panel judged this round "
                     f"{'not worth running' if pre.would_have == 'refuse' else 'unreadable as content'}"
                     " and was overruled. What follows is a content review of a "
                     f"{pre.measured:,}-{pre.cap_unit_adj} diff against a "
                     f"{pre.cap:,}-{pre.cap_unit_adj} "
                     "ceiling: most of each seat's budget went somewhere, and it was "
                     "not necessarily where the change is.")
    if seats_absent:
        # Quieter, and separate, for the reason above: this one is about the box,
        # is true every run on it, and is nobody's fault.
        lines.append(f"  - _{', '.join(seats_absent)} not installed on this host — "
                     "configured, but never a seat here_")
    if not consensus_possible and seats_asked:
        # Two different sentences, because the one-seat and no-seat cases are
        # different claims and the single wording asserted "one filed" on a run
        # where nobody had — the exact class of misreport this block exists to
        # prevent, in the block itself.
        if filers == 0:
            lines.append("  - ⚠️ **nothing below was reviewed by a panel member** — "
                         "every configured seat is empty, so there are no findings to "
                         "agree about and no ⋆consensus notation appears at all.")
        else:
            lines.append("  - ⚠️ **no ⋆consensus is possible this round** — it takes two "
                         "reviewers to agree, and one filed. Absence of ⋆consensus below "
                         "means nobody was there to agree, NOT that nobody agreed.")
    if override_note:
        # Said on the PR, not just in the terminal: a reader of the comment needs
        # to know this panel was hand-picked before reading "reviewed by one".
        lines.append(f"  - {override_note}")
    # A lost reviewer is stated where the reviewer list is, not only in a section
    # at the foot of the report: a pinned model the CLI cannot use costs you a
    # whole vendor, and a one-reviewer panel that reads like a two-reviewer panel
    # is the failure mode worth shouting about.
    for skip in llm_skipped:
        lines.append(f"  - ⚠️ **not reviewed** — {skip}")
    if not findings:
        judge_txt = ("ruled on coverage only — no findings to judge" if coverage_note
                     else "n/a — no findings to judge")
    elif judged:
        judge_txt = reviewer_label("claude", panel.get("judge_model", ""))
    else:
        judge_txt = f"⚠️ {judge_skip} — all findings KEPT unjudged (re-run to get a verdict)"
    lines.append(f"**Master judge:** {judge_txt}")
    # Where the round's wall clock went, on the PR comment and not only in the
    # payload (#192). It sits directly under the reviewer list and the judge
    # because it is a fact about exactly those two, and the operator deciding
    # whether to spend another round is the reader it is for — the payload is not
    # where they are looking. It reports the fix phase BEFORE this round, which is
    # the half of a cycle's cost that had no number anywhere.
    lines.append(panel_timing.timing_line(timing))
    # Which seats could read the code, stated on the PR comment (#113). It belongs
    # next to the reviewer list because it is a property OF that list, and it has to
    # be visible: a reader weighing "codex could not assess the caller" against
    # "claude read it and disagreed" is comparing two seats that were given
    # different evidence, and that is a bigger confound than an unpinned model. It
    # is also the line that explains why some declarations cost the round its
    # confidence and others did not.
    read_code = sorted(n for n, m in reviewer_meta.items()
                       if m.get("ran") and m.get("code_blind") is False)
    diff_only = sorted(n for n, m in reviewer_meta.items()
                       if m.get("ran") and m.get("code_blind"))
    if read_code:
        lines.append(f"**Code access:** {', '.join(read_code)} read the PR's tree at "
                     f"{(meta.get('headRefOid') or '')[:8]}"
                     + (f"; {', '.join(diff_only)} reviewed the diff alone"
                        if diff_only else "")
                     + " — a seat's own `could_not_assess` counts against the round "
                       "only where it could have opened the file")
    elif want_code and diff_only:
        # The configured-but-unusable case, said once rather than left to be
        # inferred from an absence. A repo that switched this on and sees nothing
        # about it in the report would reasonably conclude it is working.
        lines.append("**Code access:** on, but no seat on this panel can take it — "
                     f"{', '.join(diff_only)} reviewed the diff alone")
    # On EVERY round, at the defaults or not (#165). The orchestrator that briefs the
    # fixer builds that brief out of THIS report, so "which findings is the fixer being
    # asked to clear, and what buys another round" has to be readable from the
    # artifact rather than from whoever remembers the repo's config — and a reader
    # weighing a quiet round needs to know whether the quiet was measured or
    # configured. Beside the reviewer list and the code-access line because it is the
    # same kind of fact: the terms this round ran under.
    lines.append(f"**Panel dials** (`review_panel`): {dials.gist()}")
    for note in notes:
        lines.append(f"  - ⚠️ config: {note}")
    if truncated:
        # Named per reviewer, since the budgets can now differ: "truncated" alone
        # would hide that one model saw the whole diff and another saw a third of
        # it, which is exactly what you need to know when they disagree.
        # The kernel-capped seat is marked, because the footnote is now the only
        # place its truncation appears at all: it no longer files a veto line, and
        # a reader comparing "truncated for antigravity" against a confident stop
        # needs to see, right there, that the cap was the machine's and not a
        # budget somebody could raise.
        cut = ", ".join(f"{n} ({b:,}{', argv ceiling' if n in argv_capped else ''})"
                        for n, b in sorted(truncated_for.items()))
        # `target_scope`, not `review.scope`: a manifest round's material is a
        # whole-target "pr" composition of whatever the round targeted (#138).
        what = ("manifest" if pre.verdict == "manifest"
                else "increment" if target_scope == "increment" else "diff")
        lines.append(f"\n_{what} is {len(review.target):,} chars — truncated for {cut}_")

    lines.append(f"\n### To fix ({len(for_fix)}) — master-confirmed, any reviewer count"
                 + (f", {dials.fix_floor} and above"
                    if under_floor else ""))
    # #297's budget, stated where the list it bounds is, not only on the dials line.
    # A mark inside **To fix** rather than a section of its own, which is the opposite
    # of the choice the below-floor findings get below — and for the same reason read
    # the other way. These ARE the fixer's work; what is bounded is how much of them
    # gets done. An orchestrator that pastes the To fix list into a brief has to
    # sweep these up, so the budget has to come with them, which means the header.
    on_budget = [c for c in for_fix if budgeted(c)]
    if on_budget:
        lines.append(
            f"_💸 marks the {len(on_budget)} finding(s) below the "
            f"`{dials.round_trigger_floor}` cut. They share a "
            f"{dials.low_severity_fix_lines}-line budget for the WHOLE round: measure "
            "each fix's churned lines (`git diff --numstat`) rather than estimating "
            "them, spend cheapest first, and stop when the budget is spent. "
            # #554, and it belongs HERE rather than only on the dials line: this note
            # is what an orchestrator sweeps into the fixer's brief along with the
            # findings it bounds, so a weight stated only elsewhere is a weight the
            # fixer is never told about — raised by a Codex second opinion, which
            # found the dial resolved, reported, and applied by nobody. Said at `1`
            # too, on the dials line's own rule: a clause that vanishes at some
            # settings is one a reader cannot tell from a dial that was never applied.
            f"A line of test or prose costs "
            f"{dials.unrefereed_line_weight}x a line of production code, because a "
            "production fix has a referee in red/green and a test fix has none — "
            "nothing tests a test (#554). "
            "Count, do not estimate, "
            "and do not ask yourself whether a fix risks ballooning — the budget is "
            "the answer to that question and it has already been given. What the "
            "budget does not reach is reported and recorded exactly like a below-floor "
            "finding: not dropped, and not this round's work (#297). Everything "
            "unmarked is unconditional._")
    if for_fix:
        for c in for_fix:
            tail = f" — {c.rationale}" if c.rationale and c.rationale != "unjudged" else ""
            rel = f" _(same decision as {', '.join(c.related)})_" if c.related else ""
            # Said per finding, not only in the header: the rationale is blank
            # for these, so an unruled finding otherwise renders identically to
            # an adjudicated one under a header naming the judge.
            unruled = " _(unjudged — the master never ruled on this one)_" \
                if c.verdict == "unjudged" else ""
            # Only where there IS an earlier round to be new against: on a first
            # round every finding is new and the marker would be decoration.
            fresh = " 🆕" if prior_rounds and is_new(c) else ""
            again = (" ↻ _fix needs re-reading (" + ", ".join(c.rereview_by) + ")_"
                     if c.needs_rereview else "")
            paid = "💸 " if budgeted(c) else ""
            lines.append(f"- {paid}**{c.severity}**{fresh} `{loc(c)}` [{c.id}] — "
                         f"{c.synthesis}"
                         f"{conf(c)}{unruled}{tail}{rel}{again}{escalation(c)}")
            lines += accounts(c)
    else:
        lines.append("- none")

    # The other half of the fix floor (#165), and it has to be a section of its own
    # rather than a mark inside **To fix**: the two lists are read by different
    # readers for different purposes — one is a fixer's work list, the other is a
    # record for a human — and an orchestrator that pastes "the To fix list" into a
    # brief must not be able to sweep these up with it. Absent entirely at the
    # pre-#165 floor, where nothing is below it.
    if under_floor:
        # The APPLIED floor in both lines, which is the written one except at a
        # budget of 0 — where it is the cut, and saying `fix_severity_floor` would
        # name a floor these findings are above while listing them as not this
        # round's work. The `because` clause names whichever key actually decided it,
        # so an operator reading the report knows which one to edit.
        because = (f"`review_panel.fix_severity_floor` is "
                   f"`{dials.fix_severity_floor}`"
                   if dials.fix_floor == dials.fix_severity_floor else
                   f"`review_panel.low_severity_fix_lines` is 0, so the round's "
                   f"applied floor is the `{dials.round_trigger_floor}` cut rather "
                   f"than the `{dials.fix_severity_floor}` fix floor")
        lines.append(f"\n### Reported, not this round's work ({len(under_floor)}) — "
                     f"below the `{dials.fix_floor}` fix floor")
        # Where the record of these goes, said on the report rather than left to
        # whoever remembers the repo's config (#482). This list IS §4b's road 2, so
        # the orchestrator reading it is about to decide issue-or-row for exactly
        # these findings, and the answer is one line away rather than one file away.
        #
        # Computed over the severities actually IN the list rather than off the
        # floor: at a budget of 0 the applied floor rises to the trigger cut and this
        # tier can then hold two bands, so a gate between them files for some of it
        # and not the rest. Answering from the floor would state one of those as the
        # answer for all of them.
        filed = [c for c in under_floor if dials.files_issue(c.severity)]
        goes = ("each also gets a GitHub issue" if len(filed) == len(under_floor)
                else "the board row is the whole record — no GitHub issue, so the "
                     "`deferred` row carries a one-line `note` instead"
                if not filed else
                f"{len(filed)} of them also get a GitHub issue and the rest are a "
                f"board row with a one-line `note` and no issue")
        lines.append("_Master-confirmed, recorded, and deliberately NOT for the fixer: "
                     f"{because}. Do not build a fix brief from "
                     "this list — a fix pass that takes them on is the growth this "
                     "floor exists to stop (#165). They stay in the payload "
                     "(`below_fix_floor`) and on the board; recorded `deferred` in "
                     f"§4b, where `review_panel.file_deferral_issues` is "
                     f"`{dials.file_deferral_issues}`, so {goes}._")
        for c in under_floor:
            fresh = " 🆕" if prior_rounds and is_new(c) else ""
            lines.append(f"- 🔽 **{c.severity}**{fresh} `{loc(c)}` [{c.id}] — "
                         f"{c.synthesis}{conf(c)}{escalation(c)}")

    if sonar:
        lines.append(f"\n### SonarCloud issues ({len(sonar)}) — part of the gate")
        for c in sorted(sonar, key=lambda x: x.severity):
            # Same 🆕 rule as the judged findings: these count towards the round
            # diff too, because the gate has to end up clear either way.
            fresh = " 🆕" if prior_rounds and is_new(c) else ""
            lines.append(f"- {c.severity}{fresh} `{loc(c)}` — {c.synthesis}"
                         f"{escalation(c)}")

    if dismissed:
        lines.append(f"\n### Dismissed by master ({len(dismissed)})")
        for c in dismissed:
            lines.append(f"- ~~{c.severity} `{loc(c)}` — {c.synthesis}~~"
                         f"{conf(c)} — {c.rationale}")
            lines += accounts(c)

    if result.skipped:
        lines.append("\n### Skipped reviewers\n" +
                     "\n".join(f"- {s}" for s in result.skipped))

    # What the reviewers said about their OWN coverage, and what the judge made of
    # the split. This is the difference between "clean" and "I could not tell",
    # which no finding count can express — and it is on the PR comment, not just
    # the terminal, because the person deciding whether a clean verdict was earned
    # is reading the comment.
    declared = {n: m["could_not_assess"] for n, m in sorted(reviewer_meta.items())
                if m.get("could_not_assess")}
    if declared or coverage_note:
        lines.append("\n### Coverage declared by the reviewers")
        for name, gaps in declared.items():
            lines.append(f"- **{name}** could not assess: " + "; ".join(gaps))
        # Said once, under the declarations themselves, because the report has to
        # answer the question a reader asks HERE: five declared gaps and a
        # confident stop used to be a contradiction, and now it is the design.
        # Without this the change reads as the panel having quietly stopped caring
        # what its reviewers could not see.
        if declared and all(reviewer_meta.get(n, {}).get("code_blind")
                            for n in declared):
            lines.append(
                "- _these did not cost the round its confidence: every seat above "
                "reviews from the diff alone, so a gap outside the diff is a fact "
                "about the panel and not about this PR. Whoever reads this can "
                "close one with `grep`, and it is worth doing._")
        if coverage_note:
            lines.append(f"- _master:_ {coverage_note}")

    # #547. Under its own heading and not folded into the declarations above,
    # because it answers a different question and is addressed to a different
    # person. The list above is "what did the reviewers say they could not do";
    # this is "what does this PR assert that nothing here can check", which is the
    # best output of a round that raised it and is the one item on the report a
    # human is being asked to act on rather than read.
    if obligations:
        lines.append("\n### Unverifiable claims")
        lines.append(
            "_The master ruled that no seat in this review could have settled these "
            "with what it was given. They are not findings and nobody is asked to "
            "patch them — but until one is acknowledged it costs the round its "
            "confidence, which is what stops a capability limit being recorded as an "
            "assurance._")
        for ob in obligations:
            mark = (f"✅ acknowledged in round {ack_held[ob.key]}"
                    if ob.key in ack_held else "⏳ **unacknowledged**")
            lines.append(f"- `{ob.key}` — {ob.claim}"
                         + (f" _(what would settle it: {ob.reason})_" if ob.reason else "")
                         + f" — {mark}")
        open_now = [ob for ob in obligations if ob.key not in ack_held]
        if open_now:
            keys = " ".join(f"--acknowledge {ob.key}" for ob in open_now)
            # The remedy in full, in the artefact, because the whole of Part 2 is
            # that this question can be discharged and the old one could not. A
            # veto whose remedy lives in a brief the reader does not have open is a
            # veto they will resolve by dropping the gate.
            lines.append(
                f"\nRead each claim, decide whether the PR may land with it "
                f"unchecked, and if so pass it back to the next round: `{keys}`. "
                "Acknowledging is per claim on purpose — a blanket yes is the cheap "
                "gate, and it is the failure on the other side of the one this "
                "replaces."
                # Read off the dial rather than asserted, so the sentence cannot come
                # to disagree with the function the orchestrator's brief sends people
                # to. `files_issue` is the one answer to "does this get an issue", and
                # an unverifiable claim's exemption belongs there and not here.
                + (" Each also gets a GitHub issue whatever "
                   "`review_panel.file_deferral_issues` says, on the same footing as "
                   "an escalation: it carries a question past the end of this session "
                   "rather than filing a task."
                   if dials.files_issue("", unresolvable=True) else ""))

    verdict = "**stop**" if stop["stop"] else "**go again**"
    unearned = stop["stop"] and not stop["confident"]
    # Only where a loop actually exists. `--max-rounds` is the CALLER's cap and
    # only /panel-review-pr drives the loop; a review-only run (`/panel`, or
    # panel.py by hand) that printed "round 1 of at most 2 — go again" promised a
    # round nothing would run, and counted every finding of a first review as one
    # "no earlier round raised" — which is vacuously all of them.
    if cycle_run:
        lines.append(f"\n{ROUNDS_HEADING} round {round_no} of at most {cap} — {verdict}: "
                     + stop["reason"]
                     + (" — a stop, not convergence" if unearned else ""))
        # What this round READ, said next to what it concluded — and INSIDE the
        # Rounds block, because `fit_comment` trims around that block and this is
        # the sentence that makes the rest of the comment mean what it says.
        # Without it "round 2 found 4 findings" is unreadable: against the whole
        # PR that is a quiet round, against one fix commit it is a busy one, and
        # the two render identically. The char count is the TARGET's, matching
        # `diff_chars`; the context is named separately because it is the half the
        # budget is allowed to eat.
        if review.scope == "increment":
            lines.append(
                f"  _scope: the increment since `{review.since[:8]}` "
                f"({len(review.target):,} chars), plus "
                f"{len(review.near) + len(review.far):,} chars of the rest of the PR as "
                "context. Findings are about the fix commit, or about the seam it "
                "landed in — not a re-read of code earlier rounds cleared._")
        veto_head, bullet = "  _why this round's quiet is not evidence of a quiet PR:_", "  - ⚠️ "
    else:
        veto_head, bullet = ("\n**Coverage caveats** — why this review's quiet is not "
                             "evidence of a quiet PR:"), "- ⚠️ "
    if stop["veto"]:
        lines.append(veto_head)
        # A baseline problem is deliberately BOTH a config note (what went wrong)
        # and a veto (why the quiet does not count), and the payload carries it in
        # both roles on purpose — `config_notes` never reaches the board, so the
        # veto list is the record's only copy. On the PR comment, though, printing
        # the same sentence twice reads as two problems. The second appearance is
        # rendered as a pointer to the first.
        was_a_note = set(notes)
        for why in stop["veto"]:
            if why in was_a_note:
                lines.append(f"{bullet}{_veto_gist(why)} — _the config note above, "
                             "which is also why this round's quiet does not count_")
            else:
                lines.append(f"{bullet}{why}")

    # #507, and UNDER the veto lines rather than over them. A reader coming down
    # this comment meets what ended the cycle first and what the seats would do
    # about it second; a proposal above the veto reads as a plan, and a plan at the
    # top of an escalation is exactly the "cleaner than it is" this must not be able
    # to produce. Empty on every round that did not escalate, which is most of them.
    lines += panel_propose.propose_lines(proposals)

    report = "\n".join(lines)
    print(report)

    if post:
        post_summary(gh_repo, pr_number, report)
    else:
        print("\n(report only — pass --post to comment on the PR)")
    return finish(write_failed)


# The --ask path moved to panel_ask (#129). Imported back so the CLI below
# still dispatches to `ask(...)` as a plain global.
from panel_ask import *          # noqa: F401,F403
import panel_ask                 # noqa: F401


def main() -> int:
    ap = argparse.ArgumentParser(description="Reviewer panel for a PR")
    ap.add_argument("--repo", help="repo path, or a name under ~/source (default: cwd)")
    ap.add_argument("--pr", type=int,
                    help="the PR to review. With --ask, the PR the premise is being "
                         "asked on behalf of — recorded as a link, never fetched")
    ap.add_argument("--ask", metavar="PREMISE",
                    help="challenge one premise instead of reviewing a PR: put this "
                         "yes/no question to the enabled seats, with no diff, no judge "
                         "and no cycle, and print the tally. NOT a gate — it exits 0 on "
                         "every verdict, including `fails`")
    ap.add_argument("--context", action="append", default=[], metavar="PATH[:A-B]",
                    help="a file (or line range) from the repo under review to hand the "
                         "seats with the premise, e.g. harness/loops/panel.py:3500-3560. "
                         "Paths are relative to the REPO ROOT, not to the cwd. Repeatable, "
                         "and capped in total by review_panel.ask_max_context_chars. "
                         "--ask only")
    ap.add_argument("--asker", metavar="SEAT", default=None,
                    help="which seat the agent running this challenge IS, so its own "
                         f"vote cannot be the only one ({', '.join(LLM_REVIEWERS)}). "
                         "Detected from CLAUDE CODE's environment only — an agent on any "
                         "other CLI must pass this itself or the guard does not fire. "
                         "Pass an empty string to say there is no asker. --ask only")
    ap.add_argument("--post", action="store_true", help="post summary as a PR comment")
    ap.add_argument("--json", action="store_true", dest="json_out",
                    help="emit the whole run as JSON on stdout; no report/post")
    ap.add_argument("--reviewers", metavar="LIST",
                    help="comma-separated panel members to run instead of the repo's "
                         f"configured set ({', '.join(ALL_REVIEWERS)}); e.g. "
                         "--reviewers codex for a single-vendor read. "
                         "Default: whatever .harness-rules enables")
    ap.add_argument("--no-code-access", action="store_true", dest="no_code_access",
                    help="review from the diff alone, even where .harness-rules sets "
                         "`review_panel.reviewer_code_access: true`. One run's override "
                         "of the repo's setting, the same shape as --reviewers; there is "
                         "deliberately no flag the other way, because turning code "
                         "access ON for a repo that switched it off is a decision about "
                         "trusting that repo's contributors and belongs in its config")
    ap.add_argument("--json-file", metavar="PATH", default="", dest="json_file",
                    help="also write the JSON payload here, keeping the report "
                         "(and --post) — unlike --json, which replaces them")
    ap.add_argument("--no-record", action="store_false", dest="record",
                    help="don't record this run on the quarterback board")
    ap.add_argument("--force", action="store_true",
                    help="review the diff as content even when the pre-flight check "
                         "refuses the round or rules the change move-shaped. The "
                         "verdict it overrode is recorded and printed — an override "
                         "is a decision, not a way of not making one")
    # Defaulted to None rather than 1, so "not passed" and "passed as 1" stay
    # distinguishable. They are the same round to `run()` — resolved to 1 a few
    # lines below — but not to the `--ask` guard: comparing against the default
    # accepted `--ask --round 1` silently, which is a caller believing it asked
    # for something this run does not do.
    ap.add_argument("--round", type=int, default=None, dest="round_no", metavar="N",
                    help="which panel/fix cycle this is (default 1). Round 2+ is the "
                         "re-review of the fix commit — the one nobody reads otherwise")
    ap.add_argument("--baseline", action="append", default=[], metavar="PATH",
                    help="a previous round's --json-file payload, so this run can say "
                         "which findings no earlier round raised. Repeatable")
    ap.add_argument("--escalated", action="append", default=[], metavar="KEY",
                    help="a finding key (as sent with the finding: 8-64 hex chars) "
                         "whose fixer reported that the APPROACH is wrong rather than "
                         "the code, and wrote no patch (review-pr.md step 3a). It "
                         "stays outstanding and stays in the report, but no longer "
                         "counts as work a fix round can clear — otherwise the cycle "
                         "runs to its cap on a finding only a human can close. "
                         "Repeatable; needs a cycle (--round/--max-rounds/--baseline) "
                         "to mean anything, and is inherited by later rounds through "
                         "--baseline")
    ap.add_argument("--acknowledge", action="append", default=[], metavar="KEY",
                    help=f"an obligation key ({CLAIM_KEY_PREFIX} and 12 hex characters, "
                         "as the report's Unverifiable claims list prints it) whose "
                         "claim a human has read and accepted going unchecked (#547). "
                         "The claim stays in the report and in the payload's ledger; "
                         "what it stops doing is costing the round its confidence, "
                         "which is what makes an unanswerable declaration a one-time "
                         "act instead of a permanent HOLD. Per claim on purpose — "
                         "there is no flag that accepts them all. Repeatable, and "
                         "inherited by later rounds through --baseline")
    ap.add_argument("--escalated-from-board", action="store_true",
                    dest="escalated_from_board",
                    help="take the escalation list from the board instead of naming "
                         "keys by hand: every finding on this PR the panel flagged as "
                         "needing a human (`needs_human_keys`, #279) is added to "
                         "--escalated. This is the half that makes the flag actually "
                         "get used — a key a fixer has to transcribe out of its own "
                         "prose is a key nobody transcribes, which is why thirty days "
                         "of rounds recorded zero escalations. Unions with --escalated "
                         "rather than replacing it, and needs a cycle for the same "
                         "reason --escalated does")
    ap.add_argument("--premise", metavar="TEXT",
                    help="#84's futility brake, run BEFORE a fix pass rather than "
                         "after it: declare in one sentence the premise the fix you "
                         "are about to write rests on. Records it in the cycle's "
                         "register (--premise-file) and exits non-zero when this is "
                         "the Nth time that premise has been declared, where N is "
                         "review_panel.escalate_on.premise_repeated — the fix is not "
                         "to be written, the finding is escalated instead. No seats, "
                         "no diff, no judge, no cost")
    ap.add_argument("--premise-file", metavar="PATH", default="", dest="premise_file",
                    help="the cycle's premise register: written by --premise, and read "
                         "by a round so the payload can say which premises repeated "
                         "and which fix passes declared none. One path per PR, beside "
                         "the --json-file payloads")
    ap.add_argument("--premise-decidable", choices=("yes", "no"), default=None,
                    dest="premise_decidable",
                    help="#491's question, and the one thing the occurrence counter "
                         "cannot ask: can the runtime this fix's assertion runs in "
                         "OBSERVE the property the fix asserts? `no` refuses the fix "
                         "on its FIRST declaration under "
                         "review_panel.escalate_on.premise_undecidable — every fix for "
                         "an unobservable property is an approximation, and the next "
                         "round finds the gap between the approximation and the "
                         "property. Omitted is 'not answered' and brakes nothing. "
                         "--premise only")
    ap.add_argument("--premise-for", action="append", default=[], metavar="KEY",
                    dest="premise_for",
                    help="a finding key this fix pass would have cleared. Repeatable. "
                         "When the brake fires these are the keys to pass to the next "
                         "round's --escalated, which is how a braked premise reaches "
                         "the stop rule. --premise only")
    ap.add_argument("--scope", choices=ROUND_SCOPES, default="auto",
                    help="what a round past the first REVIEWS. increment: the "
                         "commits since the last round's head, with the rest of the "
                         "PR as context — cheaper as the PR grows, and it is the fix "
                         "commit the cycle exists to read. pr: re-read the whole diff, "
                         "as every release before v2.28 did. auto (default): the repo's "
                         f"review_panel.round_scope, itself defaulting to "
                         f"{DEFAULT_ROUND_SCOPE}. Round 1 is always the whole PR")
    ap.add_argument("--since", default="", metavar="SHA",
                    help="the commit the PREVIOUS round reviewed, for --scope "
                         "increment. Normally unnecessary: it is read from the "
                         "--baseline payload's `head_sha`. Pass it to review a "
                         "specific range, or when the baseline predates that field")
    ap.add_argument("--max-rounds", type=int, default=None,
                    dest="max_rounds", metavar="N",
                    help=f"the CALLER's round cap ({DEFAULT_MAX_ROUNDS} when this run is "
                         "part of a cycle); used to tell a round that stopped because it "
                         "was done from one that stopped because it ran out. Passing it "
                         "is what says this run belongs to a panel -> fix -> panel cycle: "
                         "without it (and without --round/--baseline) the run is a single "
                         "review and reports no rounds. `/panel-review-pr` spells it "
                         "--rounds N and passes it here on every invocation")
    args = ap.parse_args()
    # Validated at the edge, on both paths. A review would have GitHub refuse it
    # eventually; an ask fetches nothing, so nothing else ever looks at this
    # number — `--ask p --pr -5` put `"pr": -5` in the payload as a link for the
    # board to render.
    if args.pr is not None and args.pr < 1:
        raise SystemExit("--pr: pull requests are numbered from 1")
    # The ask is settled before the round flags are validated, because it accepts
    # none of them: an ask that reached those checks would be answering a question
    # about a cycle it is not part of.
    if args.ask is not None:
        if not args.ask.strip():
            raise SystemExit("--ask: the premise is empty — say what is being challenged")
        wrong = [f for f, used in (("--post", args.post),
                                   ("--round", args.round_no is not None),
                                   ("--baseline", bool(args.baseline)),
                                   ("--force", args.force),
                                   ("--escalated", bool(args.escalated)),
                                   ("--escalated-from-board",
                                    args.escalated_from_board),
                                   ("--acknowledge", bool(args.acknowledge)),
                                   # Refused rather than ordered, because the two are
                                   # different questions about one premise and the
                                   # answer to "which ran?" must not be a reading of
                                   # this file's branch order. `--ask` puts the premise
                                   # to the SEATS (#79, and it costs a vendor call per
                                   # seat); `--premise` counts how many times a fix has
                                   # been written against it (#84, and it costs
                                   # nothing). Run them as two commands.
                                   ("--premise", args.premise is not None),
                                   ("--premise-file", bool(args.premise_file)),
                                   ("--premise-for", bool(args.premise_for)),
                                   ("--premise-decidable",
                                    args.premise_decidable is not None),
                                   ("--max-rounds", args.max_rounds is not None)) if used]
        if wrong:
            raise SystemExit(f"--ask does not take {', '.join(wrong)}: an ask is one "
                             "question to the seats, not a round — there is no diff to "
                             "post about, no judge, no pre-flight verdict to override, "
                             "and no cycle for a baseline to be part of")
        asker = asking_seat(args.asker)
        if asker and asker not in LLM_REVIEWERS:
            raise SystemExit(f"--asker: unknown seat {asker!r} — expected one of "
                             f"{', '.join(LLM_REVIEWERS)}, or '' for no asker")
        return ask(args.repo, args.ask.strip(), args.context, args.reviewers,
                   args.pr, args.json_out, args.json_file, args.record,
                   # The NORMALISED value when one was typed, None when none was.
                   # `ask` needs the difference: "nobody, and I mean it" is a
                   # person at a terminal, while "nothing was detected" is an
                   # agent whose CLI this file cannot recognise, and only the
                   # second is worth a note in the report.
                   asker if args.asker is not None else None)
    # Settled next, and on the same principle as the ask: a declaration is not a
    # round either. It reviews nothing, so it takes none of the round flags except
    # the two that say WHICH cycle and WHICH round's findings it is answering.
    if args.premise is not None:
        if not args.premise.strip():
            raise SystemExit("--premise: the premise is empty — say in one sentence "
                             "what the fix you are about to write assumes")
        if not args.premise_file:
            raise SystemExit(
                "--premise needs --premise-file: the brake counts OCCURRENCES across a "
                "cycle, and a declaration with nowhere to be counted is not a check. "
                "Use one path per PR, beside the --json-file payloads")
        wrong = [f for f, used in (("--post", args.post),
                                   ("--baseline", bool(args.baseline)),
                                   ("--force", args.force),
                                   ("--escalated", bool(args.escalated)),
                                   ("--escalated-from-board",
                                    args.escalated_from_board),
                                   ("--acknowledge", bool(args.acknowledge))) if used]
        if wrong:
            raise SystemExit(
                f"--premise does not take {', '.join(wrong)}: declaring a premise is a "
                "check made BEFORE a fix pass, not a round — there is no diff to post "
                "about, no pre-flight verdict to override, and nothing to compare a "
                "baseline against. --escalated is what the NEXT round is given when "
                "this check refuses the fix")
        # The round flags' own check runs below, on the review path this branch
        # returns before reaching. A round of 0 or less would date the declaration to
        # a round that cannot exist and make `undeclared_passes` count from it.
        if args.round_no is not None and args.round_no < 1:
            raise SystemExit("--round: rounds are numbered from 1")
        bad = [k for k in args.premise_for if not panel_rounds._is_key(k)]
        if bad:
            raise SystemExit(
                f"--premise-for takes finding KEYS (8-64 hex characters), not "
                f"{panel_rounds._key_gist(bad[0])!r} — these are the keys the next "
                "round's --escalated would need, so an ID or a title here would name "
                "no finding at all")
        return declare(args.repo, args.premise.strip(), args.premise_file,
                       1 if args.round_no is None else args.round_no,
                       args.premise_for, args.pr, args.json_out,
                       args.premise_decidable or "unknown")
    # `--premise-file` is NOT refused here: a round READS the register, so the
    # payload can say which premises repeated and which fix passes declared none.
    # `--premise-for` has no reading outside a declaration and is refused, on the
    # rule `--context` and `--asker` are refused by: a flag accepted and ignored is
    # a caller believing it asked for something this run does not do.
    if args.premise_for:
        raise SystemExit("--premise-for belongs to --premise — it names the findings a "
                         "refused fix pass would have cleared, and a review round has "
                         "no fix pass to refuse. The round's equivalent is --escalated")
    if args.premise_decidable:
        raise SystemExit("--premise-decidable belongs to --premise — it is an answer "
                         "about the fix a pass is ABOUT to write, and a review round "
                         "writes none. The round reads the answers already in the "
                         "register through --premise-file")
    if args.pr is None:
        raise SystemExit("--pr is required — or pass --ask to challenge one premise "
                         "instead of reviewing a PR")
    for flag, given in (("--context", bool(args.context)),
                        ("--asker", args.asker is not None)):
        if given:
            raise SystemExit(f"{flag} belongs to --ask — a PR review takes neither")
    # The sentinel has done its one job (telling `--ask --round 1` from `--ask`);
    # from here down a round that was not named is round 1, exactly as before.
    round_no = 1 if args.round_no is None else args.round_no
    if round_no < 1:
        raise SystemExit("--round: rounds are numbered from 1")
    if args.max_rounds is not None and args.max_rounds < 1:
        raise SystemExit("--max-rounds: at least one round has to run")
    # The round-against-the-cap check MOVED into `run()` (#165). It has to be made
    # against the EFFECTIVE cap — otherwise `--round 3` with no --max-rounds passes
    # here and then hits the cap branch on the spot, writing "round cap (2) reached …
    # unreviewed" into a round 3 and printing "round 3 of at most 2" — and the
    # effective cap is now `review_panel.max_rounds` where a repo sets one, which
    # nothing here has read: the rules file is resolved inside `run()`. Checked there,
    # still before any PR is fetched, and it names which of the three answers supplied
    # the cap. Resolving the repo twice to keep the check here would print every rules
    # diagnostic twice for it.
    # `--escalated` only means something ACROSS rounds: it names work a later round
    # must not count. A run that is not part of a cycle has no later round, so
    # accepting the flag there leaves two options and both are worse than refusing
    # it — drop the declaration silently, or invent a cycle to hold it and print
    # "round 1 of at most 2 — go again" for a re-review nobody will run. And an
    # escalation is by construction read out of a fix pass that followed a review
    # round, so arriving without one of these three is a caller error: it is much
    # more likely a forgotten --round than a considered single-pass declaration.
    #
    # The condition is `round_no > 1`, NOT "was --round passed", because that is
    # what `run()`'s `in_cycle` tests. Asking a different question here let
    # `--round 1 --escalated <key>` past both doors — the guard saw a --round and
    # allowed it, `in_cycle` saw round 1 with no cap or baseline and built a
    # single-pass run — so the flag was accepted outside a cycle, which is exactly
    # the case this refusal exists for. Two conditions for one predicate is how
    # that happened, so they are spelled the same way and `in_cycle`'s own terms
    # are the ones used.
    if (args.escalated or args.escalated_from_board) and not (
            round_no > 1 or args.max_rounds is not None or args.baseline):
        raise SystemExit("--escalated needs a cycle to mean anything: pass --round (2 or "
                         "more) and --max-rounds, plus the earlier rounds' --baseline. "
                         "It names work a LATER round must not count, and a single-pass "
                         "review — which `--round 1` on its own still is — has no later "
                         "round")
    return run(args.repo, args.pr, args.post, args.json_out, args.reviewers,
               args.json_file, args.record, round_no, args.baseline,
               args.max_rounds, args.scope, args.since, args.force,
               args.no_code_access, args.escalated, args.escalated_from_board,
               args.acknowledge, args.premise_file)


if __name__ == "__main__":
    raise SystemExit(main())


#: Everything this module offers, INCLUDING the underscore names — the suites
#: reach for several of them through `panel`, and a plain star import would drop
#: them silently. Generated from the module's own top level, so a helper added here
#: is exported without anyone remembering to list it.
__all__ = [
    "panel_core", "panel_seats", "_RANGE", "ASK_CONTEXT_FILE_MAX_BYTES",
    "ASK_SECRET_DIRS", "ASK_SECRET_FILES", "ASK_SECRET_SUFFIXES", "AskContext",
    "ContextProblem", "_readable_file", "_context_spec", "_read_confined",
    "_secret_context", "read_context", "_budgeted", "_context_chars",
    "NO_CONTEXT", "_context_block", "ASK_DEFAULTS", "_ask_rule",
    "ASKER_ENV", "asking_seat", "detected_asker", "ask",
    "main",
]
