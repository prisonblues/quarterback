# Fix GitHub Issue

@description Plan, implement, test, and PR for a GitHub issue in an isolated worktree (auto-decides whether it needs a DB copy).
@arguments $ARGS: <issue-number> [--base <branch>] [--shared-db | --isolated-db]

Parse `$ARGS`: the first integer is the **issue number** (`$ISSUE_NUMBER`
below). An optional `--base <branch>` names the branch this issue's work
**forks from** and that its **PR targets** — used by the epic driver to point a
sub-issue at the epic integration branch instead of the default branch. If
`--base` is absent, use the remote's default branch (the original behaviour).

Read GitHub Issue #$ISSUE_NUMBER using `gh issue view $ISSUE_NUMBER`
thoroughly. Understand the full context: problem description,
acceptance criteria, linked PRs, and any discussion.

Detect the canonical remote: if `upstream` exists, use it;
otherwise use `origin`. Resolve `owner/name` from
`git remote get-url <remote>` and use `--repo <owner/name>` on
all `gh` commands. Run `git fetch <remote>`.

Execute every step sequentially. Do not stop or ask for
confirmation.

## Philosophy

The marginal cost of completeness is near zero. Do the whole
thing. Write the tests. Update the docs. Fix the related code.
Never leave a dangling thread when tying it off takes five more
minutes. The standard is "nothing left to improve".

## 0. Pre-flight

Run `git branch --show-current` and `git status --short`.
If the working tree is dirty, stop and tell the user — do not
stash or discard changes.

## 1. Research

Understand the problem space before writing code:
- Read every file that will be touched, in full — not just the
  function you'll change, but the whole file for context.
- Read existing tests for the affected code.
- Read docs that describe the affected behaviour.
- Search for related code: callers, siblings, parallel
  implementations that may need the same fix.
- If the issue involves unfamiliar APIs, libraries, or error
  messages, search for context using available tools (WebSearch,
  Exa if configured, Context7 for library docs).

## 2. Plan

Write an implementation plan. List:
- Files to modify, with specific functions/lines
- New files to create (if any)
- Tests to write (specific scenarios and edge cases)
- Docs to update
- Related code that needs to change for consistency
- Risks and how you'll mitigate them

**Decide the DB mode now** (drives worktree provisioning in step 3). From the
issue and your plan, classify the change:
- **Touches the DB model** — schema/migrations, ORM model classes, anything that
  ALTERs tables or writes migrations → needs an **isolated DB copy** so your
  schema churn never touches the main database.
- **Writes data but not schema** → also prefer an **isolated copy** (a single
  agent, but seeded/mutated data still pollutes the shared DB).
- **Read-only / no DB** → **shared DB** is fine and faster (no copy).

Record the decision as `DB_MODE = isolated | shared`. An explicit `--shared-db`
or `--isolated-db` arg overrides this classification. When genuinely unsure
between the two, choose **isolated** — it's the safe default.

## 3. Provision an isolated worktree (or reuse one you're already in)

This skill works in a **dedicated worktree** so your main checkout never moves
and its DB is never touched. Pick the branch prefix from the change type:
`fix/` for bugs, `feat/` for features, `refactor/` for refactors, `docs/` for
docs. The branch is `{prefix}issue-$ISSUE_NUMBER`.

- **Base branch:** if `--base <branch>` was passed, that is the fork point (and
  the PR target); otherwise use the remote's default branch. Fetch it first
  (`git fetch <remote> <base>`).

- **Already isolated? (epic driver case) — do NOT re-provision.** The epic
  driver runs this *inside* a worktree it already created, checked out on
  `{prefix}issue-$ISSUE_NUMBER` forked from `--base`. Detect this: if
  `git branch --show-current` already matches
  `{fix,feat,refactor,docs}/issue-$ISSUE_NUMBER`, you are already in the right
  place — **keep it, do not run `create-worktree`** (that would nest a worktree
  and throw away the correct fork point). Set `WT_DIR` to the current worktree
  (`WT_DIR=$(git rev-parse --show-toplevel)`), write the session marker for it
  (same `printf … | tee "$HOME/.cache/claude-code/session-cwd/$CLAUDE_CODE_SESSION_ID"`
  as below), then skip to step 4. The DB was provisioned by the epic setup;
  don't touch it.

- **Otherwise create the worktree** with `create-worktree` (it makes the dir,
  symlinks `.venv`/`.claude`/`CLAUDE.md`, copies configured data, provisions the
  DB, and wires Docker/nginx if the repo uses them):
  ```bash
  create-worktree --from <base> [--shared-db] {prefix}issue-$ISSUE_NUMBER
  ```
  Pass `--shared-db` **only when `DB_MODE = shared`** (step 2). For
  `DB_MODE = isolated`, pass nothing — an isolated DB copy is create-worktree's
  default. Then resolve the new worktree directory:
  ```bash
  WT_DIR=$(git worktree list --porcelain \
    | awk -v b="refs/heads/{prefix}issue-$ISSUE_NUMBER" \
        '/^worktree /{p=substr($0,10)} $0=="branch "b{print p; exit}')
  ```

**Record the worktree for this session (drives the statusline).** The shell cwd
resets to the launch dir between tool calls, so a plain `cd` will NOT stick and
the statusline can't otherwise tell it's in the worktree. Write a per-session
marker so the statusline shows the worktree's branch + port, and `/drop-worktree`
knows which worktree this session owns:
```bash
mkdir -p "$HOME/.cache/claude-code/session-cwd"
printf '%s' "$WT_DIR" | tee "$HOME/.cache/claude-code/session-cwd/$CLAUDE_CODE_SESSION_ID" >/dev/null
```
**Write it with `tee`, not `>`.** A `>` redirect anywhere under `$HOME` is
refused by the `dcg` pre-tool guard (`core.filesystem:redirect-truncate-root-home`),
so the obvious `printf … > "$marker"` never runs and the bar spends the whole
session showing the main checkout. `tee` is not a redirect and is allowed.

**Work via explicit paths, not a persistent `cd`.** Because the cwd resets every
call, do NOT assume you're "in" the worktree. For the rest of this skill:
- git commands: `git -C "$WT_DIR" …`
- shell commands (tests, build, lint): `cd "$WT_DIR" && …` in the *same* command
- file edits: absolute paths under `$WT_DIR`

Verify the worktree branch: `git -C "$WT_DIR" branch --show-current` must be the
issue branch. Store the branch name and `WT_DIR` — you'll verify the branch
again before committing, and report `WT_DIR` at the end.

**Isolation check (isolated mode).** After `create-worktree` runs, scan its
output. If it prints a red warning that any `.env` entry still equals the main
DB name (its residual-var safety net), **STOP** — the worktree may still point at
the **main** database, and a migration would corrupt shared data. Do not run
migrations or DB-touching tests until it's resolved (fix the offending `.env` var
by hand, or report it). This is the backstop for the bug class where the app
reads a DB-name var (`DB_NAME`, `PGDATABASE`, …) that the provisioner didn't
rewrite — verify the isolated DB is actually in use before touching it.

**DB confirm-guard (fallback).** If you provisioned with `--shared-db` and then
discover during implementation that the change actually needs to ALTER the model
or run a migration, **stop** and confirm with the user before proceeding — that
would mutate the shared database. Offer to re-provision the worktree with an
isolated DB copy (`remove-worktree {prefix}issue-$ISSUE_NUMBER` then
`create-worktree --from <base> {prefix}issue-$ISSUE_NUMBER`) rather than running
schema changes against the shared DB.

## 4. Implement

**Say what stage you are in**, so the statusline can show it — the branch and PR
name *which* work, never how far along it is:
```bash
qb-stage F0
```
(`F0` = implementing the first cut. The review skills move it on to `R1`, `R1F`,
`R2` … as rounds happen; `/drop-worktree` clears it. Best-effort — if `qb-stage`
is not on PATH, carry on, it is a status field and nothing depends on it.)

Follow the project's CLAUDE.md standards.

Write the complete solution:
- Fix the root cause, not the symptom.
- If related code has the same problem, fix it too — don't leave
  known bugs for a follow-up.
- If a rename or pattern change should propagate, propagate it
  everywhere.

## 5. Write tests

Every fix needs tests. Not optional. Not "if time permits".

- **Bug fix?** Write a regression test that fails without your
  fix and passes with it.
- **New feature?** Test the happy path, error paths, edge cases,
  and boundary conditions.
- **Changed behaviour?** Update existing tests to match, and add
  new ones for any uncovered paths.

Look at the existing test style and follow it. Name tests
descriptively: what scenario, what expected outcome.

## 6. Update documentation

If the change affects behaviour described in CLAUDE.md, docs/,
README, or docstrings, update them now. This is part of the fix,
not a follow-up.

For non-trivial features: add or update the relevant doc section
with enough detail for someone unfamiliar with the code to
understand what was built and why.

## 7. Build, test, lint

### Discover project checks

Read CI config (`.github/workflows/`) and Makefile to find what
the project runs. These override fallbacks.

### Run quality pipeline

1. **Build** — compile or bundle.
2. **Test** — full suite. Iterate until green. If a test fails,
   fix the code or the test (whichever is wrong), don't skip it.
3. **Lint and format** — fix all issues. Don't disable rules.
4. **Type check** — if the project uses type checking, run it.
5. **Codegen sync** — if CI has `git diff --exit-code` checks,
   run the generator and verify.

If a tool is not installed, install it if possible. Only skip
with a note if installation isn't feasible.

### Database-backed tests (when the change touches the DB)

The default/fast test run often **excludes DB-backed tests** — e.g. pytest
marker exclusions (`-m "not database"`), or a CI job with no database service.
A green fast suite then says **nothing** about code that reads or writes the DB.

So if the change touches any DB-facing code — models/schema, migrations, ORM
queries (`Model.query`, `session.get`, `select(...)`), session/transaction
handling, or DB-backed routes/tasks — you **must** also run the project's
DB-backed suite, not just the fast one. Find the dedicated target (e.g.
`make test-db`, a `database`/`integration` marker, or a tox/CI env that
provisions a real database) and run it against a live local database. If you
cannot run it, say so explicitly and flag the DB paths as **unverified**.

## 8. Self-review

Review your full diff against the base branch. Read it as if
someone else wrote it and you're trying to block the merge.

Check for:
- Correctness bugs, off-by-ones, boundary conditions
- Missing error handling, silent failures
- Security issues (injection, auth, secrets)
- Test coverage gaps — every new code path should have a test
- Naming, complexity, dead code, style
- Documentation staleness
- Related code outside your diff that should also change

### Second opinion: Codex (best-effort, never blocks)

If the `codex` CLI is available, run it as an **independent** reviewer over your
diff — a different model catches what you miss:

```bash
command -v codex >/dev/null && \
  git diff <remote>/<base>...HEAD | \
  codex exec "Review this diff for REAL defects only (correctness, security,
  error handling, broken edge cases — not style). List each as file:line + a one-
  line description. Be concise and conservative." 2>/dev/null || true
```

Fold genuine bugs Codex flags into your fixes — don't dismiss one just because
you didn't spot it. Drop only clear false positives or pure nits. If `codex` is
absent, not logged in, or errors, skip it silently — it never blocks.

Rank findings P1-P4 for the summary. Fix all of them. The only
valid skip is a genuine false positive where re-examination
confirms the code is correct. "Not worth the churn" is not valid.
"Can do later" is not valid.

One finding can be neither: it says the *approach* you chose is
wrong rather than the code, and fixing it where it points means
adding a special case to keep that approach standing (`/review-pr`'s
step 3a has the test and why it matters — #67). Here you are the
author and no PR exists yet, which makes it the cheapest moment in
the whole cycle to act on: change the approach, and say in the PR
body which finding made you. If you judge the redesign too big for
this issue, say so in the PR body in one sentence with the premise
named — a stated tension a reviewer can rule on, never a patch that
buries it.

After fixing, re-run the quality pipeline. Iterate until clean.

## 9. Commit and push

**Branch checkpoint:** Run `git branch --show-current` and verify
you are still on the branch created in step 3. If the branch is
wrong, STOP and tell the user — do not commit to the wrong branch.

- Commit with a conventional commit message
- Include a body that explains what was done and why
- The body MUST end with `Fixes #$ISSUE_NUMBER` — a bare `(#N)`
  reference closes nothing. PR-body keywords only fire when the
  PR merges into the repository default branch; in repos where
  PRs target an integration branch (e.g. `test`), the commit
  message is what closes the issue when the release merge lands
  on the default branch.
- Push the branch

## 10. Create PR

- Concise title (under 70 chars)
- **Base:** if `--base <branch>` was passed, target it explicitly with
  `gh pr create --base <branch>`; otherwise the PR targets the default
  branch. (In the epic integration flow this points the sub-PR at the
  epic branch, so the driver can stack it.)
- Description:
  - Summary of the problem
  - What was implemented (map changes to issue requirements)
  - Tests added (list specific test cases)
  - Docs updated (list specific changes)
  - "Closes #$ISSUE_NUMBER" (or "Refs" if partial). Note: this
    only auto-closes if the PR's base is the default branch —
    the commit-body keyword from step 9 is what guarantees the
    close otherwise.
- If `upstream` remote, use `gh pr create --repo <owner/repo>`

**Record the PR for this session (drives the statusline).** The bar shows the
branch, which names the *issue*, not the PR — so in a fleet of worktrees there
is otherwise no way to read off which PR a session is on. Write the number the
moment the PR exists, the same way and for the same reason as the worktree
marker in step 3 (`tee`, never `>`):
```bash
mkdir -p "$HOME/.cache/claude-code/session-pr"
printf '%s' "$PR_NUMBER" | tee "$HOME/.cache/claude-code/session-pr/$CLAUDE_CODE_SESSION_ID" >/dev/null
```
Take `$PR_NUMBER` from `gh pr create`'s output URL, or `gh pr view --json number
--jq .number`. This is only the fast path: the statusline also falls back to a
cached `gh pr list --head <branch>` (5 min TTL, refreshed in the background), so
a PR opened by hand or picked up by a later session still appears within a few
minutes — the marker just makes it instant and survives `gh` being unavailable.

## 11. Comment on issue

Post a summary comment on the issue linking to the PR:
- What was implemented (1-3 bullets)
- Key design decisions
- Tests added
- Link to PR

## 12. Leave the worktree in place

Do **not** remove the worktree — leave it so review findings (`/review-pr`,
`/panel-review-pr`) can be addressed on the same branch and DB. Tell the user:
- the worktree directory (`WT_DIR`) and its branch,
- that their main checkout was never touched,
- that the statusline now reflects this worktree (branch + PR + app port) for
  the rest of the session, via the session markers,
- how to tear it down when the PR merges: **`/drop-worktree`** (destroys the
  worktree + trappings, keeps the branch, and clears the session marker), or
  `remove-worktree {prefix}issue-$ISSUE_NUMBER` directly (the marker then
  self-heals on the next statusline render), or sweep everything later with
  `/tree-shake`.

(If you were in the epic-driver's pre-existing worktree — step 3 "already
isolated" — you did not create it, so don't advise removing it; the driver owns
that lifecycle.)
