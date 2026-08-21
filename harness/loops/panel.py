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
        "coverage_note": None,
        "diff_truncated": False,
        "diff_chars": 0,
        "diff_budgets": {},
        "config_notes": [],
        "sonar_gate": "skipped",
        "ci_status": "unknown",
        "ci_failing": [],
        "judged": False,
        "judge_model": None,
        "judge_skip": None,
        "reviewers_ran": [],
        "reviewers": {},
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


def run(repo_name: str | None, pr_number: int, post: bool, json_out: bool = False,
        reviewers: str | None = None, json_file: str = "", record: bool = True,
        round_no: int = 1, baseline: list[str] | None = None,
        max_rounds: int | None = None, scope: str = "auto",
        since: str = "") -> int:
    # A cycle is something the CALLER drives, and only /panel-review-pr does:
    # naming a cap (or a round, or a baseline) is what says this run is part of
    # one. A review-only /panel run left to the default is a single pass, and
    # must not report itself as "round 1 of at most 2 — go again", promising a
    # re-review nothing will run.
    in_cycle = max_rounds is not None or round_no > 1 or bool(baseline)
    cap = DEFAULT_MAX_ROUNDS if max_rounds is None else max_rounds
    # Idempotency key for the board record, minted once per process so a retry of
    # the POST cannot double-count the run into the stats. A fresh panel run is a
    # genuinely new observation and gets a new key — re-reviewing a PR after a fix
    # loop is data, not a duplicate.
    run_key = uuid.uuid4().hex
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
    # Resolved before anything is fetched, so a typo'd --reviewers fails on the
    # spot rather than after a PR read and a diff download.
    selected, override_note = select_reviewers(rev, reviewers)

    try:
        meta = json.loads(panel_core.sh(["gh", "pr", "view", str(pr_number), "--repo", gh_repo,
                              "--json", "title,additions,deletions,baseRefName,"
                                        "baseRefOid,headRefName,headRefOid,files,"
                                        "changedFiles,state,isDraft"]))
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
    # The base end of the same range (#98). `baseRefOid` is the MERGE BASE — the
    # commit `gh pr diff`'s three-dot diff is built from — and not the base
    # branch's tip, which is why the tip is fetched separately below rather than
    # read off this call. `.get`, not `[...]`: every other key here is required
    # because the run cannot proceed without it, and a base commit is not that.
    merge_base = meta.get("baseRefOid") or None
    changed = meta["additions"] + meta["deletions"]
    # Same call that already produced `changed`, three fields wider — so the board
    # gets the paths behind the number, and the PR's state, without a second
    # round-trip, and gets them on the skip path too, where no diff is ever
    # fetched. The state is as of THIS panel: the board is told about panels, not
    # about merges, which is what the payload's timestamp is for.
    changed_files, changed_files_total, dropped_files = _changed_files(meta)
    pr_state, is_draft = meta.get("state"), meta.get("isDraft")

    # Built BEFORE the skip branch, because the skip branch returns. It used to
    # sit with the diff budgets forty lines below, so a skipped PR carrying two
    # paths and a total of 3,000 said nothing at all — and the skip path is the
    # one this release argues is most likely to be merged unattended, which makes
    # it the worst possible place for the warning to go missing.
    notes: list[str] = []
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
            # other key of it is not a KeyError. Not recorded on the board:
            # no review happened.
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
                "provenance_counts": ({b: 0 for b in PROVENANCE}
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
                "round": round_no,
                "cycle": skip_prior.cycle,
                "prior_rounds": len(skip_prior.rounds),
                "prior_findings": len(skip_prior.keys),
                "skip_reason": f"title matches skip pattern /{pat}/",
                "run_key": run_key,
            }
            # --json-file is honoured here too, and its failure fails the run the
            # same way. The caller is told "if the panel could not write that file
            # the round did not happen", and it then feeds the file to the next
            # round as `--baseline`: a skipped PR that exited 0 leaving no file
            # gave that caller no signal at all.
            failed = write_payload(json_file, skipped_payload)
            if json_out:
                print(json.dumps(skipped_payload, indent=2))
            return finish(failed)
    print(f"\n[{repo_name}#{pr_number}] {title[:60]}", file=chatter)
    print(f"  base={base}  changed={changed} lines\n", file=chatter)

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
    review, scope_notes = ReviewScope.decide(
        want_scope, round_no, diff, (anchor, head_sha), gh_repo, base,
        None if since else prior.head_round)
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
        # `merge_base` came off the metadata read before the round started, and
        # GitHub recomputes `baseRefOid` on every push to the head branch. Leaving
        # it would pair a re-stamped right end with a left end computed for the
        # commit it replaced — and the pair being replayable is this release's
        # whole claim. Worse, the common reason a head moves here is a merge of
        # the base branch INTO the PR (~1.8 integration merges per PR landed on
        # this repo, #80), which is exactly the case that moves the merge base:
        # the stored range would then start before an integration merge its right
        # end contains, and it is a range no round ever reviewed.
        #
        # One extra call, on a path that fires rarely, and only when something has
        # already gone irregular. If it fails, the pair is not silently mismatched
        # — the note says which end is stale, because "unknown" and "stale" want
        # different treatment from whatever reads this later.
        moved_meta = _merge_base_now(gh_repo, pr_number)
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
    budgets = {name: diff_budget(rev.get(name, {}), "max_diff_chars", panel_budget, notes)
               for name in LLM_REVIEWERS if name in selected}
    judge_budget = diff_budget(panel, "judge_max_diff_chars", panel_budget, notes)

    # Read BEFORE the seats are dispatched, because its result now travels in
    # their prompt (#91). It used to run concurrently with them and be collected
    # afterwards, which is why the panel could compute CI on every run and still
    # tell no reviewer about it. One `gh pr checks` against a round that takes
    # minutes is a couple of seconds of wall-clock for a fact that refutes a whole
    # class of finding, so the overlap is not worth keeping.
    ci_status, ci_failing, ci_skip = review_ci(gh_repo, pr_number)
    if ci_skip:
        result.skipped.append(ci_skip)
    ci_text = ci_brief(ci_status, ci_failing, ci_skip)

    def prompt_for(budget: int | None) -> str:
        return REVIEW_PROMPT.format(n=pr_number, repo=gh_repo, base=base,
                                    ci=ci_text, diff=review.material(budget)[0])

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
    labels = {n: reviewer_label(n, models[n], efforts.get(n, "")) for n in LLM_REVIEWERS}

    tasks = {}
    with ThreadPoolExecutor(max_workers=len(ALL_REVIEWERS) + 1) as ex:
        # Every selected LLM reviewer runs — no de-minimis gate. If we asked for
        # the panel, we want each vendor's eyes regardless of diff size.
        for name in LLM_REVIEWERS:
            if name in selected:
                tasks[name] = ex.submit(review_llm, name, models[name],
                                        prompt_for(budgets[name]), efforts.get(name, ""))
        sonar_future = None
        sonar_filed = False
        if "sonarqube" in selected:
            sonar_future = ex.submit(
                review_sonarqube, rev.get("sonarqube", {}),
                {"number": pr_number, "base": base,
                 "head": meta["headRefName"], "head_sha": meta["headRefOid"]},
                changed_lines, cfg["path"])

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
                "max_diff_chars": budgets[name],
                # The mechanical half of "did this reviewer see the whole thing":
                # checked against the budget rather than asked for, because the
                # one thing a truncated reviewer cannot notice is the truncation.
                "truncated": name in truncated_for,
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
                ran_llm.append(labels[name])
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
    findings, judge_skip, coverage_note = adjudicate(
        clusters, judge_text, panel.get("judge_model", ""), pr_number, None, coverage,
        ci=ci_text)
    judged = judge_skip is None and bool(findings)
    to_fix = sorted((c for c in findings if c.verdict != "dismissed"),
                    key=lambda c: c.severity)
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
    if review.scope == "increment" and short_context:
        inherited.append(
            f"{', '.join(short_context)} saw only part of the PR behind the increment — a "
            "defect earlier rounds misjudged, in the part that did not fit, could not have "
            "been raised this round")
    if review.scope == "increment" and prior.truncated_rounds:
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
    if review.scope == "increment" and prior.unread_rounds:
        skipped = sorted(prior.unread_rounds)
        inherited.append(
            f"{_rounds_phrase(skipped)} recorded a head but no reviewer read it, and this "
            f"round's increment starts after it — that code has been read by no round of "
            "this cycle")
    veto = (coverage_veto(reviewer_meta, judge_skip, flagged, len(review.target))
            + judge_gaps + inherited + prior.problems)
    stop = round_stop(round_no, cap, new_keys, outstanding, veto, not prior.problems,
                      repeated=len({c.key for c in outstanding if not is_new(c)}))
    # Whether a CYCLE exists at all, and the one predicate that decides it — for
    # the report's Rounds block and for the payload alike. They used to disagree:
    # the report suppressed the block for a review-only run while the payload sent
    # `round_stop` regardless, so `record_review` stored a `/panel` read with
    # findings as `stopped: false` (the board shows a cycle mid-flight that nothing
    # will advance) and one without as `stopped: true, stop_confident: true` — a
    # confident-convergence record for a PR that had no cycle.
    cycle_run = bool(in_cycle or prior_rounds)

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
    # Defensive rather than expected: `budgets` is built over the same selected
    # seats `tasks` is, so a seat that ran normally has an entry (possibly None,
    # meaning uncapped). It survives so a future change to how `budgets` is built
    # cannot quietly turn "no budget recorded" into "read nothing".
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
    fix_diff, no_range_why = (_fix_range_diff(gh_repo, prior.head_sha, head_sha)
                              if attributable else (None, None))
    # ONE predicate for "is there a range", used by the added lines, by the note
    # and by the attribution itself. Two of them disagreed over an EMPTY compare:
    # truthiness called it no range, `fix_diff is not None` called it a readable
    # range with no added lines — and that reading labels every new finding
    # `missed`, confidently, with no note to say the range was empty.
    fix_added = _diff_added_lines(fix_diff) if fix_diff else {}
    if attributable and not fix_diff:
        notes.append(f"provenance unavailable: {no_range_why} — new findings are recorded "
                     "as `unknown`, not attributed")

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
    tally = Counter(provenance_of(c) for c in outstanding)
    provenance_counts = {b: tally.get(b, 0) for b in PROVENANCE} if attributable else {}
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
    payload = {
        **_payload_defaults(),
        "repo": repo_name, "github": gh_repo, "pr": pr_number,
        "title": title, "base": base, "changed_lines": changed,
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
        # Always True in a payload the BOARD sees — the skip path returns before
        # `record_run` because no review happened. It is here for `--json`
        # consumers, which get both shapes and need to tell them apart.
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
        "scope": review.scope,
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
        "coverage_note": coverage_note or None,
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
        # Everything prepared ALONGSIDE the target: 0 under "pr" scope, where
        # there is no such thing. This plus `diff_chars` is what a round put in
        # front of an uncapped reviewer, and the pair is the measurement issue #41
        # exists to produce.
        "context_chars": len(review.near) + len(review.far),
        "diff_budgets": {**budgets, "judge": judge_budget},
        "config_notes": notes,
        "sonar_gate": result.sonar_gate,
        "ci_status": ci_status,
        "ci_failing": ci_failing,
        "judged": judged,
        "judge_model": panel.get("judge_model", "") or None,
        "judge_skip": judge_skip,
        "reviewers_ran": ran_llm,
        "reviewers": reviewer_meta,
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
        "to_fix": [{**c.as_dict(), "new_this_round": is_new(c),
                    "provenance": provenance_of(c)} for c in to_fix],
        "sonar_findings": [{**c.as_dict(), "new_this_round": is_new(c),
                            "provenance": provenance_of(c)} for c in sonar],
        "dismissed": [{**c.as_dict(), "new_this_round": is_new(c),
                       "provenance": provenance_of(c)} for c in dismissed],
        "provenance_counts": provenance_counts,
        "skipped": result.skipped,
        "run_key": run_key,
    }

    # So a caller can have BOTH the PR comment and the machine-readable run.
    # Without --json-file, --json suppresses the report and the only way to get
    # both was to review the PR twice — several CLI invocations, for a copy. A
    # requested file that could not be written FAILS the run (see `finish`).
    write_failed = write_payload(json_file, payload)

    if record:
        record_run(payload)

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
    ci_txt = {"PASS": "✅ PASS", "FAIL": "❌ FAIL", "PENDING": "⏳ pending",
              "none": "no checks reported", "unknown": "unknown"}.get(ci_status, ci_status)
    lines.append(f"**CI (`gh pr checks`, hard gate):** {ci_txt}")
    if ci_failing:
        lines.append("  - failing: " + ", ".join(ci_failing[:10])
                     + (f" (+{len(ci_failing) - 10} more)" if len(ci_failing) > 10 else ""))
    if ci_status in ("FAIL", "PENDING"):
        lines.append(f"  - ⚠️ CI is {ci_txt.split()[-1]} — do not merge until green, "
                     "even if the review below is clean")
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
        what = "increment" if review.scope == "increment" else "diff"
        lines.append(f"\n_{what} is {len(review.target):,} chars — truncated for {cut}_")

    lines.append(f"\n### To fix ({len(to_fix)}) — master-confirmed, any reviewer count")
    if to_fix:
        for c in to_fix:
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
            lines.append(f"- **{c.severity}**{fresh} `{loc(c)}` [{c.id}] — {c.synthesis}"
                         f"{conf(c)}{unruled}{tail}{rel}{again}")
            lines += accounts(c)
    else:
        lines.append("- none")

    if sonar:
        lines.append(f"\n### SonarCloud issues ({len(sonar)}) — part of the gate")
        for c in sorted(sonar, key=lambda x: x.severity):
            # Same 🆕 rule as the judged findings: these count towards the round
            # diff too, because the gate has to end up clear either way.
            fresh = " 🆕" if prior_rounds and is_new(c) else ""
            lines.append(f"- {c.severity}{fresh} `{loc(c)}` — {c.synthesis}")

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

    report = "\n".join(lines)
    print(report)

    if post:
        # Bounded, and NOT check=True. This is the last step of a run that has
        # already succeeded and already printed its report above: a hung network
        # call here would block after every expensive thing is done, and raising
        # would throw away a completed review over a failed comment. The comment
        # is how the fix loop finds the findings, so a failure has to be LOUD —
        # but it degrades the run, it doesn't void it.
        try:
            proc = subprocess.run(["gh", "pr", "comment", str(pr_number), "--repo",
                                   gh_repo, "--body", fit_comment(report)],
                                  capture_output=True,
                                  text=True, stdin=subprocess.DEVNULL, timeout=120)
            if proc.returncode == 0:
                print(f"\n(posted panel summary to {gh_repo}#{pr_number})")
            else:
                why = stderr_gist(proc.stderr or "") or f"exited {proc.returncode}"
                print(f"\n! panel summary NOT posted to {gh_repo}#{pr_number} ({why})"
                      f" — the report above is the only copy", file=sys.stderr)
        except (subprocess.TimeoutExpired, OSError) as e:
            why = "timed out after 120s" if isinstance(e, subprocess.TimeoutExpired) \
                else e.__class__.__name__
            print(f"\n! panel summary NOT posted to {gh_repo}#{pr_number} ({why})"
                  f" — the report above is the only copy", file=sys.stderr)
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
    ap.add_argument("--json-file", metavar="PATH", default="", dest="json_file",
                    help="also write the JSON payload here, keeping the report "
                         "(and --post) — unlike --json, which replaces them")
    ap.add_argument("--no-record", action="store_false", dest="record",
                    help="don't record this run on the quarterback board")
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
                                   ("--max-rounds", args.max_rounds is not None)) if used]
        if wrong:
            raise SystemExit(f"--ask does not take {', '.join(wrong)}: an ask is one "
                             "question to the seats, not a round — there is no diff to "
                             "post about, no judge, and no cycle for a baseline to be "
                             "part of")
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
    # Checked against the EFFECTIVE cap, not only against an explicit one. The
    # default is the cap `run()` actually applies, so `--round 3` with no
    # --max-rounds used to pass this guard and then hit the cap branch on the
    # spot — writing "round cap (2) reached … unreviewed" into a round 3 and
    # printing "round 3 of at most 2". That is precisely the corrupted cycle
    # metadata this guard exists to prevent, leaking through the one spelling it
    # did not cover.
    cap = DEFAULT_MAX_ROUNDS if args.max_rounds is None else args.max_rounds
    if round_no > cap:
        default_note = "" if args.max_rounds is not None else \
            " (the default, since --max-rounds was not passed)"
        raise SystemExit(f"--round {round_no} is past --max-rounds "
                         f"{cap}{default_note}: raise the cap, or pass the round "
                         "this run actually is")
    return run(args.repo, args.pr, args.post, args.json_out, args.reviewers,
               args.json_file, args.record, round_no, args.baseline,
               args.max_rounds, args.scope, args.since)


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
