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
improve". The ONLY valid reason to skip a finding is a genuine false positive
you re-examined and confirmed correct.

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
- Body: every finding with severity + resolution. If the auto-close check found
  no commit carries the closing keyword, end with `Fixes #N`.
- Regular push (not force). In worktree mode, push `HEAD:<branch>`.

#### 6. Return a summary (do NOT post a PR comment unless the orchestrator asked)

Return this table as your final message:
```
## Review Summary — PR #<n> (<repo>)
Files reviewed: N | Findings: N | All fixed: Yes

| # | Severity | Finding | Resolution |
|---|----------|---------|------------|
| 1 | P1 | ... | Fixed: ... |

Tests added: ...
Docs updated: ... (or "none needed")
Verification — Tests: pass (N passed, M added) | DB-backed: pass / N-A /
  unverified | Lint: clean | Format: clean | Types: clean / N-A
Commit: <sha> <subject>
```
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

Findings you discovered yourself, with no board record behind them, have no key
and nothing to record: this is for panel findings only.

## 3. Relay the result

Show the user the sub-agent's summary table verbatim, then state plainly: the
branch it pushed to, whether all checks passed, and anything it flagged as
**unverified**. If the sub-agent failed or stopped early, report exactly where
and why — don't paper over it.

## 4. Merging (only if the user asks)

`gh pr merge --merge --delete-branch` — preserve individual commits; never
squash; delete the remote branch after.
