---
description: "Delegated PR review+fix. Resolves the target (current branch by default, or a PR number), then launches ONE autonomous sub-agent that reviews, fixes every finding, writes tests, runs the quality pipeline, amends, and pushes. Run it right after a fix — fresh-eyes review without a new conversation."
argument-hint: "[pr-number]  (optional — defaults to the current branch's diff vs its base)"
---

# Review and Fix PR

You are the **ORCHESTRATOR**. You do **not** review or fix in this
conversation. You resolve the target, launch a single autonomous sub-agent
that does the entire review-and-fix pass, and relay its summary back. This
keeps the heavy diff-reading and fresh-eyes review out of your context — so
you can fire this right after making a change and carry on.

## 1. Resolve the target

- **PR number given** (`$ARGS` has an integer) → review that PR. Detect the
  canonical remote (`upstream` if it exists, else `origin`); the sub-agent
  works on the PR's branch **in a plain throwaway `git worktree`** (see the
  brief) so your current checkout is never disturbed.
- **No argument** → review **the current branch's own work**: its diff vs the
  base branch. Determine the base = merge-base with the repo default branch
  (`gh repo view --json defaultBranchRef`) or the branch's upstream. The
  sub-agent fixes **in place** (you are already on the branch you want fixed —
  no worktree, no checkout thrash). Uncommitted changes in the working tree
  ARE part of the review target; tell the sub-agent to include them.
  - **Fail fast:** if the current branch IS the repo's default branch
    (`main`/`master`/the detected default), STOP here — do **not** launch the
    sub-agent. In-place mode commits to the current branch, and that must never
    be a shared/default branch. Tell the user to switch to a feature branch
    (or pass a PR number to use the isolated-worktree path instead).
  - Be explicit to the user: in-place mode **adds a commit to the branch you
    are on** — it is the only mode that changes your current branch's state (by
    design — it's finalising the work you just did).

Capture: repo (`gh repo view --json nameWithOwner`), remote, base branch, the
PR number (if any), the branch name, and the absolute repo path. Pass all of
these to the sub-agent.

## 1b. Claim what you are about to review

Without a claim, an agent hours into a review looks on every fleet surface like one
that has just opened the repo, and the plan keeps offering the work (#713). This
command *fixes and pushes*, so a duplicate here is two agents pushing to one branch.

Take it **in this conversation, before §2 launches the sub-agent**. Not because the
sub-agent has a session of its own — it does not: a Task sub-agent inherits
`CLAUDE_CODE_SESSION_ID` and is distinguished only by `CLAUDE_CODE_CHILD_SESSION=1` —
but because a claim is the only thing that can *prevent* duplicated work, one taken
after the fixer has started reading can only record it, and this conversation
outlives the sub-agent, so the claim belongs to the conversation still here to hand
it back.

- **A PR number was given:**
  ```bash
  qb-claim pr <n> --no-plan-item --ttl 28800 --note "review-pr: reviewing and fixing PR #<n>"
  ```
  `--no-plan-item` (#722): a claim normally writes the repo's plan item at rank 1,
  because picking work up should put it on the board — but reviewing a PR is not
  picking it up, and that row would outlive the claim, offering the next agent a
  review that already happened.
- **No argument (the current branch), and the branch names an issue** — `fix/issue-N`
  and friends: `create-worktree` already claimed `N` for this tree, held by the
  machine. Adopt that claim instead of taking a second one; re-claiming from here is
  a renew by the same machine and stamps this session onto the existing row, which
  is what lets `/session/end` end it (#681):
  ```bash
  qb-claim issue <the number in the branch> --ttl 28800 --note "reviewing $(git branch --show-current)"
  ```
- **No argument and the branch names no issue** — there is nothing repo-global to
  key on. Say so in the relay and carry on; do not invent a resource to claim.

Whichever line ran: **exit 0** is expected; **exit 1** names a holder — say so to
the user before the sub-agent starts, because a second reviewer-fixer on one PR is
two agents pushing to one branch; **exit 2** is a board that could not be reached,
which is worth a line in the relay and stops nothing.

**Eight hours, and every exit after this releases it.** Nothing renews a work claim
while it is held, so a fuse shorter than the review would report the PR free with the
fixer still in it — worse than a claim that lingers, because it manufactures the
collision. `qb-release` ends it: at §3 on the happy path, and as part of stopping on
every other. A sub-agent that returns an error, a user who says stop, an escalation
you relay without fixing: release before you report.

Do not lean on the session-end backstop for those. `POST /session/end` fires when
the SESSION ends, not when a command stops, and the pane routinely lives for hours
after a command has given up — so an abort with no release holds the PR for the
whole fuse, which is precisely the window in which that fuse gets exercised.

## 2. Launch the fixer sub-agent

Stamp the stage so the statusline says what this session is doing:
```bash
qb-stage R1
```
One agent here both reviews and fixes, so unlike `/panel-review-pr` there is no
separate `R1F` to stamp — the honest answer for the whole run is `R1`. Splitting
it would mean guessing when the sub-agent stopped reading and started writing,
and a bar that says `R1F` while a reviewer is still reading is worse than one
that says less. `/drop-worktree` clears it.

Launch **one** `general-purpose` sub-agent (Agent tool). Its brief is
`~/.claude/loops/docs/review-pr-brief.md` (source: quarterback's
`harness/loops/docs/review-pr-brief.md`): put the resolved target context (repo,
remote, base, PR number, branch, repo path, and whether to fix in place or use a
worktree) at the top of the prompt, then either paste the brief verbatim or tell the
sub-agent to Read that file first and follow it. Do not summarise it. The sub-agent
runs autonomously to completion — find, fix, verify, commit, push — and returns the
summary table from the brief's step 6. Do not babysit it; do not pre-empt its work in
this conversation.

## 2b. Record what happened to each finding (when the findings came from a panel)

If the findings you handed the fixer came from a recorded panel round — i.e. the
board has them, with a `key` each — say what became of them, per the **4b**
section of `panel-review-pr.md`. The `Resolution` column of the fixer's summary
table is exactly this information in prose: `fixed`, or `narrowed` with the general
form it did not take, or `refuted` with the reason it was not a defect, or `deferred`
with where it went.

**The fixer reports finding IDs; you supply the keys.** The report it was briefed
from prints `[236-F01]` and never the 16-character key — deliberately, since a
literal key on a PR comment reads as an API key to every secret scanner
(`panel-review-pr.md` §4b) — so the fixer's `Deferred` and `Escalated` blocks name
IDs. Map each one to its key out of the round's JSON payload before you record
anything or pass `--escalated`; §4b has the `jq` one-liner that prints both.

Record outcomes exactly as `panel-review-pr.md` §4b says, including which deferrals
get an issue under `review_panel.file_deferral_issues` and the escalation order. The
parts that land on you here:

- **`refuted` is the one that matters most.** A judge-confirmed finding that turns out
  to be wrong is otherwise recorded nowhere, and the refutation is already written in
  the fixer's table.
- **An escalated finding (the brief's step 3a) is recorded `deferred`** — the defect is
  real and only the fix is in dispute — and it is the row you record last: relay the
  escalation, then open the premise issue (at every setting of
  `file_deferral_issues`, `never` included), then record `deferred` naming that issue
  in `deferred_to`. The fixer opens nothing. There is no sixth value: the vocabulary
  is constrained in the database (`ck_review_finding_outcomes_vocabulary`), so an
  invented `escalated` costs the row.
- **A fixer deferral and a finding the panel reported below the fix floor are
  `deferred` too**; a `narrowed` finding is recorded `narrowed`, with its general form
  as the row's `note`.

Findings you discovered yourself have no key and are not recorded.

## 3. Relay the result

**Hand back what §1b claimed, first.** The pass is over, so the record should stop
saying this agent is on it — a claim that outlives its work is #135, and it is what
makes the fleet's claim count highest right after it has been most productive.

```bash
qb-release pr <n>                 # or: qb-release issue <n>, on the current-branch route
```

Nothing to release is exit 0, so run it even on the paths where §1b took nothing —
the board was unreachable, or the branch named no issue. If §1b adopted the
checkout's claim on an *issue* and the work is not finished, leave it: that claim
covers the branch, not this pass, and §4 and the worktree teardown are where it goes
back.

Show the user the sub-agent's summary table verbatim, then state plainly: the
branch it pushed to, whether all checks passed, and anything it flagged as
**unverified**. If the sub-agent failed or stopped early, report exactly where
and why — don't paper over it.

**A deferral is relayed, not silently absorbed.** If the fixer returned anything in
its `Deferred` block, or the panel reported anything below the fix floor, say so
plainly with the count and the one-line reason for each: those are defects this pass
knowingly did not fix, and a relay that omits them tells the user a PR is finished
when the record says otherwise. Then follow §2b in order — record the row, and open
an issue only where `file_deferral_issues` calls for one. Where it files nothing —
which, under `shape`, is every batch — the relay is
where a human hears about it at all, so the count and the reasons are not optional
there; that is the half of the deal that keeps a board row from being a place things
go to be forgotten.

**A narrowed finding is relayed as what it is — a fix, with a general form nobody
wrote.** Give the count, and for each one the general form in the fixer's own line.
Relaying it as `Fixed` is the failure this outcome was created to stop: the finding
really was answered where it was raised, so the temptation to round it up to a fix is
strong, and the sentence the user needs is precisely the one that gets lost. Say the
same about anything in the summary's **Surface** line: a file the fix pass touched that
no round has read is a decision the user is entitled to hear about, not a detail of the
diff.

**An escalation is the headline, not a footnote.** If the sub-agent escalated
anything (the brief's step 3a), lead with it: the premise, what it explains,
what removing it would cost, and that no patch was written for it. That is a
question being put to the user, and until they answer it the review is not
finished — so do not answer it yourself by launching another fixer at the same
finding, which is precisely the round that produces the next round's findings.
For panel findings, §2b is the follow-through in order: relay, then open the
issue that asks the premise, then record the finding `deferred` with that
issue in `deferred_to`.

## 4. Merging (only if the user asks)

Follow `panel-review-pr.md` §7 *Merging, once the user has said yes* exactly: claim the
base branch, re-run preland with `--claim-holder`, then `gh pr merge <pr> --merge
--delete-branch` (never squash), then `qb-release issue <n>` for the issue the PR
closes. Read `fix-and-land.md`'s *The hazards* before acting on anything that looks
like a failure.
