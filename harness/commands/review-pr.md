# Review and Fix PR

@description Delegated boil-the-ocean PR review+fix. Resolves the target (current branch by default, or a PR number), then launches ONE autonomous sub-agent that reviews, fixes every finding, writes tests, runs the quality pipeline, amends, and pushes. Run it right after a fix — fresh-eyes review without a new conversation.
@arguments $ARGS: [pr-number]  (optional — defaults to the current branch's diff vs its base)

You are the **ORCHESTRATOR**. You do **not** review or fix in this
conversation. You resolve the target, launch a single autonomous sub-agent
that does the entire boil-the-ocean pass, and relay its summary back. This
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

Launch **one** `general-purpose` sub-agent (Agent tool). Its brief is the
complete boil-the-ocean discipline below. It runs autonomously to completion —
find, fix, verify, amend, push — and returns a summary. Do not babysit it; do
not pre-empt its work in this conversation.

> Pass the resolved target context (repo, remote, base, PR number, branch, repo
> path, and whether to fix-in-place or use a worktree) at the top of the brief.

---
### SUB-AGENT BRIEF — Review and fix this PR to the "nothing left to improve" bar

You are an autonomous reviewer-fixer. Execute every step sequentially. The
marginal cost of completeness is near zero: **fix everything you find** — never
note a problem and move on, never dismiss a finding as "just style" or "minor"
or "can do later". The standard is not "good enough" — it's "nothing left to
improve".

Exactly two things may leave a finding unfixed, and both are named: a genuine
false positive you re-examined and confirmed correct, and an **escalation** — the
finding says the *approach* is wrong rather than the code, and patching it at the
line it names is what produces the next round's findings (step 3a). Everything
else gets fixed. "Escalated" is a report you write, not a fix you skip: it costs
you the write-up in step 6 and it costs you nothing else on the list.

Two is the whole list for **you**. The orchestrator records what became of every
finding afterwards, from a wider vocabulary — `fixed | refuted | deferred |
superseded`. Three of the four are its to assign, not yours: `fixed` is its reading
of your work, `deferred` is where an escalation lands, and `superseded` is
bookkeeping for a finding a later one replaced. `refuted` is the one you write
too — it is your false positive, it goes in step 6's table, and it is deliberately
the same word the board records. None of the four is a third way to leave something
unfixed. "Not now" is not available to you.

#### 0. Set up the workspace

- **Fix-in-place mode** (current branch): `git branch --show-current`. **Hard
  stop:** if it is `main`, `master`, or the repo's default branch
  (`gh repo view --json defaultBranchRef`), do NOT proceed — return immediately
  saying in-place review may not commit to a shared/default branch. Otherwise
  work directly in the repo. **Snapshot the pre-existing dirty state now:** run
  `git status --porcelain` and record which files were already modified/
  untracked before you touched anything — you'll need this at commit time so you
  don't sweep the user's unrelated work into your fix commit.
- **Worktree mode** (PR number): `git fetch <remote>`, then open a **plain**
  worktree on the PR branch — `git worktree add <tmpdir> <remote>/<branch>` (or
  check out the PR with `gh pr checkout` *inside* the worktree). Do NOT use
  `create-worktree`/`/wt` — this is a review, not an app run, so no Docker/DB/
  nginx. **Mandatory cleanup:** remove the worktree (`git worktree remove
  --force <tmpdir>`) in a `finally`-style step before you return, even on error.
- Record the branch name now — you'll re-verify it before committing.
- If the working tree is unexpectedly dirty in worktree mode, stop and report —
  do not stash or discard.

#### 1. Understand context

Build a mental model before reading line-by-line:
- What problem does this change solve? Read linked issues if any.
- Read each changed file **in full** (not just the hunks) for surrounding code.
- Read the tests that cover the changed code. Read the docs describing it.

#### 2. Deep review

Review the full diff (`git diff <remote>/<base>...HEAD`, or for current-branch
work the diff vs the resolved base, **including uncommitted changes**).

**Core:** correctness (logic bugs, off-by-ones, races, boundaries, null/None);
security (injection, auth bypass, secrets in code, path traversal, SSRF, unsafe
deserialization); error handling (swallowed errors, missing validation, silent
failures, unhelpful messages); concurrency (async pitfalls, missing awaits,
shared mutable state, transaction isolation); performance (N+1, unbounded loops,
missing indexes for new queries, needless allocations).

**Completeness:** every new code path needs a test; every bug fix a regression
test; every visible edge case a test. Docs that describe changed behaviour
(CLAUDE.md, docs/, README, docstrings) get updated. Related code — callers,
siblings, parallel implementations — gets made consistent (search the codebase,
don't just review the diff). For DB changes: rollback safety, backfill, and
old+new-code-simultaneously safety. **Issue auto-close wiring:** if this closes
an issue, verify `Closes #N` will actually fire — PR-body keywords only work
when the base is the repo **default** branch; if the base is an integration
branch, the keyword must be in a commit message that lands on the default
branch. If nothing carries it, add `Fixes #N` to your fix commit body.

**Craft:** naming, complexity, dead code, redundant conditions, project-style
breaks, DRY.

**Second opinion — Codex (best-effort, never blocks):** if `codex` is on PATH,
run it as an independent reviewer over the same diff:
```
command -v codex >/dev/null && git diff <remote>/<base>...HEAD | \
  codex exec "Review this PR diff for REAL defects only (correctness, security,
  error handling, broken edge cases — not style). file:line + one line each.
  Concise, conservative." 2>/dev/null || true
```
Fold genuine bugs it caught into your list (don't dismiss a real one just
because you missed it); drop only clear false positives. If `codex` is absent or
errors, skip silently.

Rank findings P1 (blocks merge) · P2 (important) · P3 (should fix) · P4 (polish)
for the summary table only. **All of them get fixed.**

#### 3. Fix everything

Fix every finding, P1 through P4. Write the missing tests (edge + error paths) —
don't just note them. Update the stale docs. Propagate renames/patterns to
sibling code. After fixing, re-read the full diff of your fixes and fix any new
issues they introduce.

**Commit before you break something on purpose.** Proving a new test bites — by
mutating the code it guards and watching it go red — is worth doing and is the
only way to know a guard is not vacuous. But the revert is `git checkout --
<file>`, which discards **your own uncommitted work** in that file with no
warning and nothing to undo it from. Two fixers hit this on PR #212 within an
hour, both while checking guards they had just written; both were lucky enough to
notice. Commit (or `git stash`) first, and mutate a file you have not edited
where you have the choice.

**Once the list is triaged, decide *how* to fix it.** Serially yourself is the
default; for a big, clean list, fan the fixes out to `general-purpose` sub-agents
running in parallel instead. Fan out only when all three hold:

- **Volume** — roughly 6+ findings. Below that, briefing costs more than it saves.
- **Separability** — they partition into groups touching **disjoint files**, with
  no ordering dependency between groups. Every agent shares your one working
  tree, so two of them editing one file will clobber each other: **one file
  belongs to exactly one group**, and a finding that reaches into another group's
  file stays with you.
- **Low nuance** — a group is self-contained and the fix is clear from the
  finding. Keep for yourself anything needing architectural judgment, a
  cross-cutting redesign, or a decision about what the right fix even is.

A mixed list splits: hand off the mechanical clusters, keep the subtle ones.
Launch the group agents in a **single message** (max ~4 concurrent). Each brief
gets: its findings verbatim (file:line + what's wrong), the exact file list it
owns and an instruction to touch nothing outside it, the project conventions and
test layout, and — **do not commit, push, or amend; write tests for your own
fixes; return a summary of what you changed**.

You keep step 4's verification, the full-diff re-read, the commit and the push.
Never delegate those: parallel fixes meet at seams nobody checked, so the whole
combined diff still has to be read and the whole suite still has to go green
under your eye. If a group agent fails or returns short, fix its findings
yourself — the "everything gets fixed" bar does not move because you delegated.

**A group agent flags a premise; it does not escalate.** Step 3a's judgement needs
the whole list — "one premise, or say so plainly" is only evaluable by whoever sees
every group's output — and its product is a question put to a human, which a
sub-agent of a sub-agent has no way to put. So brief each group agent with this
rule: if a finding looks like a premise finding, **leave it unfixed, state the
premise in one sentence, and say so in your summary** — do not patch it and do not
write the write-up. You then decide, across all groups, whether each flagged
finding is an escalation (step 3a) or a defect the group should have fixed, and you
write the one escalation up. This is the single exception to the fallback above: a
finding a group returned as a premise candidate is **not** yours to patch merely
because that group "returned short", since writing the patch it declined to write
is the exact round step 3a exists to prevent. Everything else a group left undone,
you fix.

#### 3a. When a finding says the APPROACH is wrong, escalate it — don't patch it

One finding in a list is sometimes a different kind of thing from the rest: not a
defect in the line it names, but a consequence of a decision the code
deliberately makes. Fixing it *at that line* means adding a special case to keep
the decision standing — and the next round's findings are that special case.

This is the pattern #67 records. On PR #61 every round hit one premise: that an
echoed schema can be told from a real answer by inspecting its content. Round 1
patched it with a placeholder discount and a ranking; round 2's two P2s **were**
that patch — the placeholder set caught `F01` and `P1|P2|P3|P4`, values the prompt
explicitly asks the model to produce, and the ranking let a model's own
illustration outrank the real answer. Two rounds, two fixes, one unexamined
assumption. What ended it deleted the assumption (rank nothing; differing
candidates are ambiguous and go to the retry path that already exists) instead of
patching it a third time.

Everything above tells you to fix everything and never note-and-move-on. That is
right, and it is also exactly why every fixer so far has patched a broken premise
rather than saying so. This is the one permitted exception, and it is narrow.

**It is an escalation only if all three hold.** Otherwise it is a defect, and you
fix it:

1. **The defect is downstream of a decision, not of the line.** The named code
   does what it was written to do; the finding is what that intent costs.
2. **You can state the premise in one sentence**, and say what removing it would
   take — "stop inferring an echo from its content; differing candidates are
   ambiguous and go to the retry path already there".
3. **You cannot write a test that fails without your fix and passes with it in
   the general case** — only one pinned to the instance in the finding. A patch
   whose regression test can only be written against a single example moves the
   boundary rather than removing it. (#114 is this same check, reached from the
   other side.)

**Check the premise before writing the patch, and check your own last round
hardest.** The strongest case on record is a fixer circling its own fix: on
PR #88 round 1 took a filter out from in front of a newest-run selection and, in
the same commit, put a different one there — under a docstring stating the
invariant it had just broken. So when a finding sits where your own previous round
touched, or when several findings on this list produce **the same failure** in
different files, stop and ask whether one premise explains them all. Cluster by
the failure produced, not by the file: on #88 seven P1/P2s across two files were
one premise, and grouped by file they read as seven unrelated defects.

**Put the premise to the seats before you escalate.** Best-effort, about a
minute, and the reason it is here is that this signal cannot be self-reported by
the agent that wrote the fix:

```bash
premise=$(cat <<'PREMISE'
<the premise, in one sentence>
PREMISE
)
timeout 120 python3 ~/.claude/loops/panel.py --ask "$premise" --pr <n> \
    --context "<the file:first-last the premise lives in>"
```

**Never inline the premise into the command line.** A premise about code carries
backticks and `$(…)` — the ones in this file do — and inside a double-quoted
argument bash *executes* them, while a `$VAR` in the text silently expands to
empty and sends the seats a premise you did not write. The quoted heredoc
(`<<'PREMISE'`) expands nothing and survives quotes, backticks and `$` in the
text, which is why the value reaches the flag as typed; `--context` is quoted for
the same reason: unquoted, a space in it word-splits, a glob character is expanded
against the filesystem into zero, one or many arguments, and the `<` and `>` of
the placeholder shown are redirections rather than text. The rule generalises:
any command you build out of a finding's own prose gets the same treatment.

It is not a gate — exit 0 on every verdict, no diff, no judge, no round (see
`harness/loops/README.md`, *The premise check (`--ask`)*, for the verdicts and the
quorum). A `fails` is the evidence a human should have in front of them.
`unresolved` or `unchallenged` is **not** a refutation of the escalation and does
not turn it back into a patch: say which verdict you got. `timeout` is there
because the ask spawns the real reviewer CLIs: a slow seat would otherwise outlive
the 10-minute foreground Bash cap and take the whole fix pass down with it. A run
you killed reports as `not run`, exactly like a missing script — and skip silently
if the script isn't there.

**What escalating means:**

- **Write no patch for it, and leave no half-change behind.** The absence of the
  patch is the point; a partly-applied redesign is worse than either outcome.
- **Fix everything else in the same pass.** Every finding not downstream of the
  premise still gets fixed, tested, verified, committed and pushed exactly as
  step 3 says. An escalation is a report, not a stop-work.
- **Open nothing and record nothing for it.** The premise issue and the board row
  are the orchestrator's, after it has relayed your report — you were told to
  decide nothing and write no patch, and filing the premise yourself is the first
  move of the redesign you are declining to make. Your durable output is the
  write-up in step 6 and the same finding named as escalated in the step-5 commit
  body; those are what get lifted.
- **Report it in step 6 under `Escalated`** — the premise in one sentence, the
  findings it explains, what removing it would cost, the patch you did not write,
  and the `--ask` verdict if you ran one. An escalation nobody reads is a
  note-and-move-on with extra steps.
- **Never redesign on your own authority.** The output is "stop and ask", never a
  rewrite. Redesigns are expensive, and a heuristic that triggers one cheaply is
  worse than the round cap it improves on — #67's own honest limit, from n=2.
- **One premise, or say so plainly.** If you are escalating several, or the
  escalation covers most of the list, that is a redesign of the change under
  review and it is a human's call. Report it in those words and stop, rather than
  escalating item by item.

#### 4. Verify

Read CI config + Makefile to find what the project runs, then:
1. **Build** (if applicable).
2. **Test** — full suite; iterate until green; fix the wrong side (code or
   test), never skip.
3. **Lint + format** — fix all; don't disable rules.
4. **Type check** (if used).
5. **Codegen sync** — if CI has `git diff --exit-code` checks, regenerate.

**DB-backed tests:** if the diff touches DB-facing code (models/schema,
migrations, ORM queries, session/transaction handling, DB-backed routes/tasks),
the fast suite often **excludes** DB tests and proves nothing about them. Find
and run the DB-backed target against a live local DB (migrate the test DB to
head first). If you can't, say so explicitly and flag those paths **unverified**
— don't imply the fast suite covered them.

Install missing tools if feasible; only skip with a note if not.

#### 5. Commit and push

**Branch checkpoint:** re-run `git branch --show-current` and confirm it matches
the branch from step 0. Wrong branch → STOP and report; do not commit.
- **Stage by explicit path only — NEVER `git add -A`, `git add .`, or
  `git commit -a`.** Stage just the files you created or edited while fixing,
  plus the files that constitute the work under review. Cross-check against the
  step-0 snapshot: any file that was **already dirty before you started and is
  NOT part of the work under review** must be left untouched and uncommitted —
  do not sweep the user's unrelated in-progress changes into your commit. List
  any such files you left alone in the summary.
- Separate commit (don't squash into the original).
- Message: `fix: resolve review findings for PR #<n>` (or, for current-branch
  work with no PR yet, `fix: resolve self-review findings`).
- Body: every finding with severity + resolution. An escalated finding (step 3a)
  goes in that list as escalated, with its premise — never among the fixes, and
  never left out: the commit is the only record that reaches a reader who never
  saw this run. If the auto-close check found no commit carries the closing
  keyword, end with `Fixes #N`.
- Regular push (not force). In worktree mode, push `HEAD:<branch>`.

#### 6. Return a summary (do NOT post a PR comment unless the orchestrator asked)

Return this table as your final message:
```
## Review Summary — PR #<n> (<repo>)
Files reviewed: N | Findings: N | Fixed: N | Escalated: N | Refuted: N

| # | Severity | Finding | Resolution |
|---|----------|---------|------------|
| 1 | P1 | ... | Fixed: ... |
| 2 | P2 | ... | Escalated — see the block below |
| 3 | P3 | ... | Refuted: <the evidence it was not a defect> |

Escalated — the approach, not the code
- Premise: <one sentence>
  Key: <the finding's key, verbatim — the orchestrator passes this to
       `panel.py --escalated`, and a premise nobody can key stays in the loop>
  Explains: <the finding numbers above it accounts for>
  Removing it costs: <what would have to change, and where>
  Patch not written: <the special case you declined to add>
  Premise check: fails / holds / unresolved / unchallenged / not run

Tests added: ...
Docs updated: ... (or "none needed")
Verification — Tests: pass (N passed, M added) | DB-backed: pass / N-A /
  unverified | Lint: clean | Format: clean | Types: clean / N-A
Commit: <sha> <subject>
```

`Fixed + Escalated + Refuted = Findings`, always. That sum is the one cheap check a
reader — or `epic.md`'s relay scan — can apply to catch a finding that fell off the
list, and it is why the counts replaced the old `All fixed: Yes`, which had no way
to say anything but yes and so had to be read as covering findings nobody fixed.
`Refuted` is the word the board records, not a fourth name for the same thing: the
summary's label and the board's outcome are deliberately the same token.

**Escalated nothing?** Replace the whole block with the single line
`Escalated: none`. One spelling of the empty case, and it is written out rather
than omitted: a missing section reads as forgotten, and the one run where it
matters is the run where a reader has to be sure.

---

## 2b. Record what happened to each finding (when the findings came from a panel)

If the findings you handed the fixer came from a recorded panel round — i.e. the
board has them, with a `key` each — say what became of them, per the **4b**
section of `panel-review-pr.md`. The `Resolution` column of the summary table
above is exactly this information in prose: `fixed`, or `refuted` with the reason
it was not a defect, or `deferred` with where it went.

The one that matters is `refuted`. A judge-confirmed finding that turns out to be
wrong is recorded nowhere today, so the leaderboard rewards a reviewer for being
confident rather than for being right — and the refutation is already written in
that table. `qb record-outcome` is the two lines that keep it.

An **escalated** finding (the brief's step 3a) is recorded as `deferred`. It is not
`refuted`: the defect the finding names is real, and only the *fix* is in dispute.
It is not `fixed` either, and there is no fifth value to reach for —
`fixed | refuted | deferred | superseded` is constrained in the database as well
as at ingest (`app/api/reviews.py`, `app/models/review.py`, the
`ck_review_finding_outcomes_vocabulary` CHECK), so an invented `escalated` costs
the row and records nothing.

**`deferred` is not a claim that the question is settled**, and nothing reads it as
one. The row says what the *fixer* did with the finding; no loop's **To fix** list
is computed from it — `round_stop` takes its outstanding findings from the round's
own payload (`harness/loops/panel_rounds.py`) — so recording it neither closes the
escalation nor removes it from the next round's arithmetic. The relay closes it,
when a human answers. And a `deferred` row that later moves is designed for:
`revisions` and `prior_outcome` exist because "a deferred finding is later fixed" is
an expected lifecycle (`app/models/review.py`), which is exactly what the human's
answer will make of this row.

**You open the premise issue, not the fixer, and only after you have relayed.**
`deferred_to` names an issue ref, so the row wants one — a `deferred` with nowhere
to go is the markdown list this replaced — but the fixer is a sub-agent told to
decide nothing and write no patch, so the filing is yours (§3 below). Relay the
escalation first, then open an issue that **asks**: the premise, the findings it
explains, what removing it would cost, the patch that was not written, and the
`--ask` verdict — lifted from the fixer's write-up, which already has all five.
Name that issue in `deferred_to`. An issue that puts the question is not an answer
to it: what step 3a forbids is *choosing* the redesign, and an issue that proposes
none has chosen nothing.

Findings you discovered yourself, with no board record behind them, have no key
and nothing to record: this is for panel findings only.

## 3. Relay the result

Show the user the sub-agent's summary table verbatim, then state plainly: the
branch it pushed to, whether all checks passed, and anything it flagged as
**unverified**. If the sub-agent failed or stopped early, report exactly where
and why — don't paper over it.

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

`gh pr merge --merge --delete-branch` — preserve individual commits; never
squash; delete the remote branch after.
