# Fix GitHub Issue — in place (no worktree, no branch)

@description Plan, implement, and test a GitHub issue RIGHT HERE — current checkout, current branch, no worktree. The deliberate "just do it inline" path; relies on you (and quarterback) to control the chaos.
@arguments $ARGS: <issue-number>

The in-place sibling of `/fix-issue`. Same implementation discipline, but it
does **not** create a worktree, does **not** create a branch, and does **not**
open a PR by default. It commits to whatever branch you are on now. Use it for
small, low-risk fixes where a worktree + PR is overkill and you want to stay in
this checkout.

> **When NOT to use this:** anything that alters the DB model, anything you'll
> want reviewed via `/review-pr` on the default branch, or anything you'd run
> five of at once. Those want `/fix-issue` (isolated worktree + PR).

Parse `$ARGS`: the first integer is the issue number (`$ISSUE_NUMBER`).

Detect the canonical remote (`upstream` if it exists, else `origin`), resolve
`owner/name`, use `--repo <owner/name>` on all `gh` commands, and
`git fetch <remote>`.

Read GitHub Issue #$ISSUE_NUMBER with `gh issue view $ISSUE_NUMBER` thoroughly —
problem, acceptance criteria, linked PRs, discussion.

## 0. Pre-flight — branch guard

Run `git branch --show-current` and `git status --short`.

- **On the default branch?** If the current branch is the repo's default
  (`main`/`master`/the detected `gh repo view --json defaultBranchRef`), STOP and
  ask the user (AskUserQuestion) how to proceed:
  - **Proceed on the default branch** — commit straight to it (true in-place
    "chaos" mode; no review-pr afterward, since it refuses on the default
    branch), or
  - **Make a branch instead** — in which case you should really be running
    `/fix-issue` (worktree + PR); offer to hand off.
  Do not silently commit to the default branch.
- **Dirty tree:** the working tree may already have unrelated changes. Note them
  now (`git status --porcelain`) — at commit time you will stage **only** the
  files that are part of this fix, never `git add -A`, so you don't sweep the
  user's in-progress work into your commit.

## 1. Research

Understand the problem before writing code:
- Read every file you'll touch, in full.
- Read existing tests for the affected code, and the docs that describe it.
- Search for related code: callers, siblings, parallel implementations.
- For unfamiliar APIs/errors, search for context with available tools.

## 2. Plan + DB safety check

Write a short plan: files/functions to change, tests to add, docs to update,
related code to keep consistent, risks.

**DB guard.** Because this runs against your **current** database (there is no
isolated copy), classify the change:
- If it **alters the DB model** — schema, migrations, ORM model classes, any
  ALTER/DROP or new migration — **stop and confirm with the user** before
  proceeding. Running that here mutates the shared/working database. Recommend
  `/fix-issue` (isolated DB copy) instead; only continue in place if the user
  explicitly accepts the mutation.
- Read-only or non-schema changes are fine to run in place.

## 3. Implement

Follow the project's CLAUDE.md standards. Fix the root cause, not the symptom.
If related code has the same bug, fix it too. Propagate renames/patterns
everywhere they apply.

## 4. Write tests

Every fix needs tests — not optional.
- **Bug fix?** A regression test that fails without your fix and passes with it.
- **Feature?** Happy path, error paths, edge/boundary cases.
- **Changed behaviour?** Update existing tests and cover any new paths.

Match the existing test style. Name tests by scenario + expected outcome.

## 5. Update documentation

If the change affects behaviour described in CLAUDE.md, docs/, README, or
docstrings, update them now — part of the fix, not a follow-up.

## 6. Build, test, lint

Read CI config (`.github/workflows/`) and the Makefile for what the project
runs; those override fallbacks. Then:
1. **Build** — compile/bundle.
2. **Test** — full suite; iterate until green; fix the wrong side (code or
   test), never skip.
3. **Lint + format** — fix all; don't disable rules.
4. **Type check** — if the project uses it.
5. **Codegen sync** — if CI has `git diff --exit-code` checks, regenerate.

**DB-backed tests:** if the change touches DB-facing code and the fast suite
excludes DB tests, run the DB-backed target too (against your current local DB —
remember there's no isolated copy here). If you can't, say so and flag those
paths **unverified**.

## 7. Self-review

Read your full diff as if blocking someone else's merge: correctness, boundaries,
error handling, security, test-coverage gaps, naming/dead code, doc staleness,
related code that should also change.

**Second opinion — Codex (best-effort, never blocks).** If `codex` is on PATH:
```bash
command -v codex >/dev/null && git diff | \
  codex exec "Review this diff for REAL defects only (correctness, security,
  error handling, broken edge cases — not style). file:line + one line each.
  Concise, conservative." 2>/dev/null || true
```
Fold genuine bugs in; drop only clear false positives. Skip silently if absent.

Fix everything you find, then re-run the quality pipeline until clean.

## 8. Commit (in place)

**Branch checkpoint:** re-run `git branch --show-current` and confirm it's the
same branch you pre-flighted on (and that you cleared the default-branch guard).
Wrong/unexpected branch → STOP and tell the user.

- **Stage by explicit path only — NEVER `git add -A`, `git add .`, or
  `git commit -a`.** Stage just the files that constitute this fix. Leave any
  pre-existing unrelated dirty files untouched, and list them in your summary.
- Conventional commit message with a body explaining what and why.
- The body **MUST** end with `Fixes #$ISSUE_NUMBER` — with no PR to carry the
  keyword, the commit is what closes the issue when it lands on the default
  branch.

## 9. Push (optional — ask)

Do not push automatically. Ask the user whether to push:
- If yes, `git push` the current branch.
- If this is a shared/default branch, double-check they want to push directly to
  it before doing so.

## 10. Coordinate + report

- If the quarterback board tools are available, post a `landed` with the commit
  SHA and a one-line summary (and `report_git` so peers can find the commit) —
  that's the coordination substitute for the missing PR.
- Report to the user: branch committed to, the SHA, test/lint/DB status (flag
  anything **unverified**), any pre-existing files you deliberately left
  uncommitted, and whether you pushed.
