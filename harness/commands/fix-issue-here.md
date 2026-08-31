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

**Red/green each regression test before you commit.** A test written alongside its
fix has never run against the broken code, so nothing has shown it would catch
anything — and a test that would not keeps passing when the bug returns. Capture the
fix as a patch, remove it, watch the tests go red, put it back:

```bash
git add -N <every file your fix changed OR ADDED>
git diff HEAD -- <those files> > .redgreen.patch
test -s .redgreen.patch || { echo "STOP: captured nothing"; exit 1; }
git checkout HEAD -- <the files that existed before>; rm <the files it ADDED>
pytest <the new tests>            # MUST fail, on the assertion
git apply .redgreen.patch && rm .redgreen.patch
pytest <the new tests>            # green again
```

**Not `git stash`.** Every worktree of a repo shares one `refs/stash`, so a stash
pushed here is poppable from every sibling worktree — the PR that added this
instruction lost its working tree that way, to a concurrent agent in another worktree.
A patch file shares nothing, and `git stash` in a harness worktree is now REFUSED by a
hook (`qb-stash push` is the per-worktree replacement; it takes no pathspec, so this step
still wants a patch). `test -s` is the guard and it must **halt** — `|| { echo …;
exit 1; }`, never a bare `|| echo …`, which warns and carries on: an empty capture (wrong
paths, or a fix already committed) leaves the red run executing with the fix in place,
coming out **green**, reading exactly like the step passing. `git add -N` is what puts a file the
fix ADDED into the patch — without it `git diff` ignores untracked files and the red
run imports the new half; those come back out with `rm`, not `git checkout HEAD --`.
Your new *test* file is not in the list and stays put, which is the point: remove it
too and the red run collects nothing, which is not a red test.
If the fix is already committed: `git checkout <remote>/<base> -- <the files your fix
changed>`, test, then `git checkout HEAD -- <the same files>`.

Read *how* it failed. An import error, a missing fixture or a `TypeError` proves
nothing — the failure has to be the assertion that names the defect. A removed fix
routinely takes a symbol the new test imports with it, so this is the usual outcome
rather than an unlikely one.

**Exempt only where there is nothing to fail against:** a test for a path the fix
*created* — a new function, flag or file. Report those as `red/green: N-A (new code
path)`. A prompt string, a config default or a doc that already existed is **not**
exempt — that text is the artefact and a test can assert on it — and neither is a fix
to code that already existed: a test that will not go red is testing something other
than the bug.

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

**Before you fix a finding, write one line naming who consumes the code the fix
would change** — the callers, and where that code reaches a response or a stored
artefact, the entitlement tier it is served to. Every finding gets one, before its
patch, in your summary beside the finding; `unknown` is allowed where a search could
not settle it, and then you fix that finding where it was raised and no further.
`/review-pr`'s step 3 has the full version and the instance behind it (#616). The
short reason: dropping a false positive is permitted and nothing is asked in support
of a fix, so refuting a wrong finding costs more than complying with it and the pass
complies — and unnecessary churn reads as diligence, because a fix for a non-defect
looks exactly like a fix for a defect.

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
