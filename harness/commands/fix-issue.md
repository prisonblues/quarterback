# Fix GitHub Issue

@description Plan, implement, test, and PR for a GitHub issue in a worktree with its own database copy.
@arguments $ARGS: <issue-number> [--base <branch>]

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

**There is no DB mode to decide, and that is deliberate** (#340). This step used
to ask you to classify the change — schema churn gets an isolated database copy,
read-only work shares the main one and skips the copy — and step 3 offered
`--shared-db` to act on the answer.

The classification asked the wrong question. What decides whether the shared
database is safe is not whether *your change* writes to it; it is whether
*anything you run* truncates it. Step 7 runs the full suite on every invocation,
without exception, and this suite's teardown truncates. So the answer was already
"unsafe" before you had finished reading the issue, and a correct classification
led to a worktree the suite's own guard then refused to run in — which is how
that guard came to stop two runs in one day.

So: the worktree always gets its own database copy, this skill passes no DB flag,
and there is nothing here for you to weigh. (`--shared-db` still exists on
`create-worktree`, where it is meaningful for a caller that genuinely never runs
a suite. This is not one of them.)

## 3. Get an isolated worktree — provision, reuse or inherit — then verify it

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
  as below), then go to **Isolation check** at the end of this step. Don't
  provision a database — the epic setup did that — but do check it: "it was
  provisioned" is the assumption the check exists to test, not a reason to skip.

- **Does the worktree already exist? Reuse is allowed; re-verifying is not
  optional.** `create-worktree` **refuses** an existing directory
  (`Worktree directory already exists: …`) rather than reusing it, so this is a
  decision you make after it has declined to do anything: work in the directory
  that is there, or `remove-worktree {prefix}issue-$ISSUE_NUMBER` and create it
  fresh. Salvaging a branch abandoned weeks ago is often the right call — the
  prior work is sometimes exactly what the issue needs — and nothing here
  forbids it.

  What it forbids is inheriting that worktree's configuration unexamined.
  **Nothing provisioned it this time, so nothing gave it its own database**, and
  a worktree created before per-worktree databases existed still names the
  **main** one. That is the second route into #340 and the one nobody chose:
  `feat/issue-85` reached the shared database with no `--shared-db` anywhere,
  because a reused worktree carried a `.env` older than the isolation that was
  supposed to protect it.

  Resolve `WT_DIR` for the existing worktree with the same `git worktree list`
  command below, then go to **Isolation check**.

- **Otherwise create the worktree** with `create-worktree` (it makes the dir,
  symlinks `.venv`/`.claude`/`CLAUDE.md`, copies configured data, provisions the
  DB, and wires Docker/nginx if the repo uses them):
  ```bash
  create-worktree --from <base> {prefix}issue-$ISSUE_NUMBER
  ```
  No DB flag: an isolated copy is `create-worktree`'s default and step 2 says why
  this skill never asks for anything else. Then resolve the new worktree
  directory:
  ```bash
  WT_DIR=$(git worktree list --porcelain \
    | awk -v b="refs/heads/{prefix}issue-$ISSUE_NUMBER" \
        '/^worktree /{p=substr($0,10)} $0=="branch "b{print p; exit}')
  ```

**If the claim reports a previous holder, look before you implement.** `create-worktree` takes
the issue's claim, and the board answers a fresh take with `previously` when somebody claimed this
same issue and then stopped renewing — printed on stderr as `previously: …`, with the worktree and
host their claim recorded and whether that tree or branch is still on this box. Go and read those
commits: the work you are about to write may already exist, half-finished, on a branch nobody
pushed. It is advice and not a refusal — "abandoned for a reason, carry on" is a legitimate
conclusion — but reach it after reading them, and say in the PR body which it was.

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

### Isolation check — every route ends here, and none of them may skip it

Ask the **resolved `.env`** which database this worktree names. Not
`create-worktree`'s output, not the flag you passed, not which route you took:
the file the application will actually read.

```bash
check-db-isolation "$WT_DIR"
```

Exit 0 and you may proceed to step 4. **Non-zero: STOP.** The refusal names the
database, the variable holding it and the checkout that owns it. Do not run the
suite, a migration, or anything else that touches the database — this worktree
would rebuild another checkout's data, and the suite's teardown truncates. Fix
the offending `.env` variable, or `remove-worktree` and provision again, then
re-run the check. If you cannot resolve it, say so and stop; do not carry on with
DB work flagged as "probably fine".

**Why it is a command and not a paragraph.** The check this replaces read
`create-worktree`'s output for its residual-`.env` warning, so it only ran when
`create-worktree` ran — and a **reused** worktree, the one route where nothing
provisioned a database and the `.env` is therefore least trustworthy, skipped the
check entirely. That is how `feat/issue-85` came to point at the shared database
with every decision above it made correctly. A check conditional on the safe path
having been taken is not a check.

`check-db-isolation` ships with this harness, so it is on `PATH` wherever this
brief is. If it is not, do not read that as permission: run
`harness/bin/check-db-isolation` from the harness checkout, or compare
`$WT_DIR/.env`'s database name against the main checkout's by hand and stop if
they match.

It is the same comparison the test suite's own guard makes at pytest start
(`tests/dbtarget.py`, and `harness/templates/dbtarget.py` for other repos —
`check-db-isolation` imports that module rather than re-implementing it). That
guard is the backstop and it holds; this one runs *before* the work, where the
answer still costs a re-provision rather than an afternoon.

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

### Red/green — prove each regression test would have caught the bug

A test written alongside its fix has never run against the broken
code. Nothing so far has shown it would catch anything, and a test
that would not is worse than no test: it is a passing assertion
that the bug is gone, and it keeps passing when the bug comes back.

Before you commit, make each new regression test fail:

```bash
cd "$WT_DIR" && git add -N <every file your fix changed OR ADDED>
cd "$WT_DIR" && git diff HEAD -- <those same files> > .redgreen.patch
cd "$WT_DIR" && { test -s .redgreen.patch || { echo "STOP: captured nothing"; exit 1; }; }
cd "$WT_DIR" && git checkout HEAD -- <the files that existed before>
cd "$WT_DIR" && rm <the files your fix ADDED>
cd "$WT_DIR" && pytest <the new tests>    # MUST fail, on the assertion
cd "$WT_DIR" && git apply .redgreen.patch && rm .redgreen.patch
cd "$WT_DIR" && pytest <the new tests>    # green again
```

**Do NOT use `git stash` for this.** Every worktree of a repo shares
one `refs/stash` — it is in the common git dir, not the per-worktree
one — so a stash pushed here is listed and poppable from every other
worktree this skill has ever created, and `stash@{0}` means whatever
the last pusher meant. The PR that added this instruction lost its
own working tree exactly that way: a concurrent agent in a sibling
worktree popped the red/green stash into its own checkout. A patch
file shares nothing. `create-worktree` now installs a hook
that REFUSES the shared stash, so a plain `git stash` here stops with
a `REFUSED:` message; `qb-stash push` is the per-worktree
replacement. It takes no pathspec, which is why this step still wants
a patch file.

**`test -s` is the guard, and it has to HALT.** An empty capture —
mistyped paths, or a fix already committed — means the red run
executes with the fix still in place, comes out **green**, and reads
precisely like the step passing. Hence `|| { echo …; exit 1; }`
rather than `|| echo …`, which warns, exits 0, and proceeds into the
run the check existed to prevent.

**`git add -N` is what puts a file the fix ADDED into the patch.**
Without intent-to-add, `git diff` ignores untracked files, so a fix
spanning an edit and a new module is half-captured and the red run
imports the new half. Added files come back out with `rm`, not `git
checkout HEAD --`, which cannot restore a path absent from HEAD.
Your new *test* file is not in the list and stays put — the point.

**If the fix is already committed** there is nothing uncommitted to
capture: use `git checkout <remote>/<base> -- <the files your fix
changed>`, run the tests, then `git checkout HEAD -- <the same
files>`.

Read *how* it failed. An import error, a missing fixture or a
`TypeError` demonstrates nothing — the failure has to be the
assertion that names the defect. Stash the **fix**, not the test:
stash both and all you have proved is that a file you removed no
longer runs.

**Exempt only where there is genuinely nothing to fail against:** a
regression test for a path the fix *created* — a new function, flag
or file — has no pre-fix behaviour to run against. Report those as
`red/green: N-A (new code path)`.

**A prompt string, a config default or a doc that already existed is
NOT exempt.** That text is the artefact and a test can assert on it,
so such a test goes red against the pre-fix text like any other —
this very instruction arrived in a PR that changed a prompt string
and a set of markdown briefs, and nine of its eleven tests failed
against the previous text. Nor is a fix to code that already
existed: a test that will not go red is testing something other than
the bug, and the test is what needs fixing. The exemption exists so
the legitimate case need not be lied about; one wide enough to cover
the awkward cases is how the step stops happening at all.

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

**Before you fix a finding, write one line naming who consumes the
code the fix would change** — the callers, from a search you ran, and
where that code reaches a response or a stored artefact, the
entitlement tier it is served to. Every finding gets one, before its
patch, and it goes in your summary beside the finding.
`/review-pr`'s step 3 has the full version and the instance it was
written from (#616); the short reason is that the paragraph above
allows a false positive and asks nothing at all in support of a fix,
so refuting a wrong finding costs more than complying with it and
the pass complies. One line owed either way removes the difference,
and it is usually the refutation itself. `unknown` is allowed where
a search could not settle it, and a finding whose consumers you
could not establish is one you fix where it was raised and no
further.

One finding can be neither: it says the *approach* you chose is
wrong rather than the code, and fixing it where it points means
adding a special case to keep that approach standing (`/review-pr`'s
step 3a has the test and why it matters — #67). Step 3a tells a
fixer never to redesign on its own authority; here the opposite
applies, and **authorship is the whole difference**. A fixer is
changing somebody else's shipped decision on a PR under review, so
its output is a question. You are the author, nothing is merged and
no PR exists yet, so the decision is still yours to make and this is
the cheapest moment in the cycle to make it: change the approach,
and say in the PR body which finding made you.

If you judge the redesign too big for this issue, do not bury it in
a patch — write it up in the PR body with step 3a's five fields, so
a reviewer inherits exactly what a fixer's escalation would have
given them:

- the premise, in one sentence;
- the findings it explains;
- what removing it would cost, and where;
- the patch you did not write (the special case you declined to add);
- the `--ask` verdict, if you put the premise to the seats
  (`fails` / `holds` / `unresolved` / `unchallenged` / `not run`).

Step 3a's invocation with its `--pr` dropped — that flag only links
the ask to a PR for the board to render, and there is no PR yet; an
`--ask` carrying no `--pr` is accepted, and it is the only form
available here:

```bash
premise=$(cat <<'PREMISE'
<the premise, in one sentence>
PREMISE
)
timeout 120 python3 ~/.claude/loops/panel.py --ask "$premise" \
    --context "<the file:first-last the premise lives in>"
```

Keep the quoted heredoc — step 3a says why, and the short version is
that a premise about code carries backticks and `$(…)`, which bash
executes inside a double-quoted argument.

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
