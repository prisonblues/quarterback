# Loops — Epic Driver

@description Decompose an epic and work each issue: master triages doability + routes a model per issue → choose a landing model → worktree → /fix-issue → CI → panel → /review-pr → STOP at human merge.
@arguments $ARGS: <epic-number> [repo] [--execute] [--max-issues N] [--landing auto|integration|multi] [--integration-branch NAME] [--sub-pr-merge auto|gate] [--base BRANCH] [--model fable|opus|sonnet]

Drive an epic's sub-issues through the per-issue pipeline.

1. Parse `$ARGS`: the first integer is the **epic issue number**; an optional word is the **repo**
   (default: the cwd's repo); pass any `--execute` / `--max-issues N` / `--landing` / `--integration-branch`
   / `--sub-pr-merge` / `--base` / `--model` flags through.
2. **Default is DRY-RUN.** Run (from anywhere):
   ```
   python3 ~/.claude/loops/epic.py --epic <n> --model <your-tier>   # --repo <path|name> for another repo
   ```
   where `<your-tier>` is the tier of the model **you** (the master) are running as — `fable`
   (Fable/Mythos), `opus`, or `sonnet`. If the user pinned `--model` in `$ARGS`, pass that
   through instead. Show the user the decomposition + landing plan + per-issue plan (stage,
   existing PR, action, **model**).

## LLM model routing — judge at the initiating tier, discretion downward

The triage judge runs at the tier the epic was initiated with (`--model`), and for each
sub-issue uses its discretion to pick an **equal-or-lesser** tier (`sonnet < opus < fable`;
haiku is deliberately not on the ladder) to implement it. The pick is passed to `/fix-issue`
and `/review-pr` via `--model`, shown as a column in the dry-run plan and a `model` field in
`--json`; invalid or over-ceiling picks clamp to the ceiling. Sanity-check the routing in the
dry-run — if a hard schema/engine issue got `sonnet`, override or re-run. Without `--model`,
routing is off and implementers run on the CLI's saved default (the old behaviour).

## Sub-issue discovery

`epic.py` finds sub-issues from **two sources, unioned**: GitHub's **native sub-issues**
(authoritative, ordered — the relationship shown under "Sub-issues" in the UI) and issue refs
**scraped from the epic body** (checklist `- [ ] #N`, numbered `1. #N`, bold `**#N**`, or bare
`#N` lines). Native order leads; body-only refs follow. If a decomposition looks wrong, the epic
either has no native sub-issues registered (link them in the UI) or uses an unusual body format.

## Landing model — the master's call

The big choice is how the work lands. `epic.py` emits **coupling signals** (`landing` block in
`--json`: `suggested`, `reasons`, `edges` (inter-issue deps), `phases`) and a *suggested* strategy.
**You (the master) make the final call** — `--landing auto` (the default) just hands you the
suggestion:

- **`integration` → one PR.** A durable epic branch `epic/<n>-<slug>` is cut off the base
  (`executor_pr_base`). Each sub-issue gets its own disposable worktree branched off the **epic
  branch**, and its PR (via `/fix-issue --base <epic-branch>`) targets the epic branch. Under
  `--execute`, **`epic.py` itself stacks the work**: it runs the sub-issues in **dependency order**
  (topo-sorted from the `edges` signal), and as each sub-PR goes green it **fast-forward-merges the
  sub-branch into the epic branch and pushes** (`--sub-pr-merge auto`, the default), so the next
  issue forks from a branch that already contains everything before it. This ff-only stacking keeps
  history — and alembic migration heads — linear by construction. With `--sub-pr-merge gate` each
  sub-PR is left for a human instead. At the end **you open ONE** `epic/<n>-<slug>` → base PR — the
  single human merge gate. Best for **coupled / sequential** epics. This is the proven #859 "dolt"
  pattern, now first-class in the tool rather than hand-driven by the master.
- **`multi` → a PR per sub-issue.** Today's behavior: one branch + PR per sub-issue into base, each
  reviewed and merged independently. Best for **genuinely independent** sub-issues.

**How to decide:** trust the suggestion when the signals are clear (deps present, or a flat list
with no phases → integration; independent phases → multi). **Interview the user only when it's
genuinely borderline** — otherwise state your choice + the one-line reason and proceed. The user
can always pin it with `--landing` (or `epic.landing` in the repo's `.harness-rules`), which skips the
judgment entirely.

**Base branch.** Sub-issues target `executor_pr_base` from the repo's `.harness-rules` by default
(falling back to its default branch).
To land an epic's work into a different branch for one run — a feature branch like `omnibus`, say —
pass **`--base <branch>`** rather than editing `.harness-rules` (which is committed and easy to forget to
revert). The base must already exist on the remote (push it first). In integration mode the epic
branch is cut off this base; in multi mode each sub-PR targets it directly. Merge to the real
trunk stays a human step regardless.

3. The **master triages each sub-issue for doability** — issues an agent can't actually do (e.g.
   "obtain a content licence") are flagged `blocked` and skipped, not implemented. A judge that
   could not rule at all reports `untriaged (…)` naming the cause, never a bare "untriaged": the
   judge never started (`could not start: …`), ran out of time (`timed out after 300s`), exited
   non-zero (`judge failed: …` — a non-zero exit means the RUN failed, so its stdout is not a
   verdict even when it parses), produced nothing (`no verdict: …` quoting the stderr that explains
   it — a denied tool permission, an unusable model pin), or answered unusably (`no verdict: no JSON
   in reply` / `bad verdict: malformed JSON`). Stderr is never blamed for a run that replied, since
   a judge's warm-up chatter is not why its answer was unusable. Untriaged issues are also skipped
   on `--execute`, like blocked ones, so that reason is the only account of why one was passed over.
4. If the user asked to `--execute`: this **creates branches/worktrees, runs `/fix-issue` +
   `/review-pr`, and opens PRs**. It is gated on `loops.issue_executor` in the repo's `.harness-rules` and uses
   `headless_permission_mode`. **Confirm with the user and check prerequisites first**, then run
   with `--execute [--max-issues N]`.
   - In **integration mode** with `--sub-pr-merge auto` (the default), `epic.py` runs issues in
     dependency order and **performs the sub-PR→epic-branch ff-merges itself** as each goes green,
     so the stack builds up automatically. You only open the final `epic→base` PR. With
     `--sub-pr-merge gate`, the merges are left to you. A non-ff merge (the epic branch diverged /
     a second migration head) is **surfaced and the run stops** rather than force-merged — relinearize
     and re-run.
   - **Merge to the real base is NEVER automatic** in either mode — it stays a human step.

### Hardening — fail-loud, resumable, preflighted

`epic.py --execute` is defensive by design; know these behaviours:

- **Fail loud.** After `/fix-issue`, the driver asserts a real artifact (a pushed PR, or at least a
  commit beyond base) exists. If an issue produced **nothing**, it is marked `failed` and the run
  **stops** (it never prints a merge/gate line for an issue with no artifact). Pass `--keep-going`
  to collect failures and press on instead. `epic.auto_finish: true` (config) lets it salvage
  staged-but-uncommitted work into a commit before deciding.
- **Idempotent resume.** A per-run state file (`~/.local/state/loops/epic-<repo>-<epic>.json`) records each
  issue's stage/branch/PR/merged status, and any sub-branch already an ancestor of the epic branch
  is treated as `done` (git ancestry). Re-running `--execute` resumes where it left off — safe to
  kill and restart.
- **Preflight.** Before executing it checks the workspace is **trusted** (an untrusted workspace
  makes `claude` silently drop `permissions.allow`, stalling git/gh) and that
  `headless_permission_mode` actually permits git/gh; it warns on low memory / leaked
  `feat-issue-*` containers. Blockers abort with a clear message.
- **An escalation is invisible to the driver.** `/review-pr` may report that a finding says the
  approach is wrong rather than the code, and write no patch for it (`/review-pr`'s step 3a). That
  report exists only in the agent's text: nothing here parses it, so an issue still counts as a real
  artifact and, in integration mode, its sub-PR still ff-merges onto the epic branch. So read each
  `/review-pr` relay for an `Escalated` block before opening the `epic→base` PR — the merge to the
  real base is the human step this is for. Making it mechanical is #67's first piece, which is not
  built. The detection lands after the ff-merge, so the epic branch is already carrying code whose
  approach is in question: **copy every non-empty `Escalated` block verbatim into the `epic→base`
  PR body**, under a heading that says so, naming the sub-issue and sub-PR it came from. The relays
  that said `Escalated: none` get one line listing them, not a block each: the empty case is written
  out per sub-PR so you can see it was answered, and a dozen of them here buries the one that is
  not. That is the only place the human doing the real merge will see it — the alternative is
  reading harness logs — and it is a question they answer, so do not open the epic PR as if the
  epic were finished, and never redesign a sub-PR's approach yourself to clear it.
- **Resource discipline.** Each issue's worktree **and its containers / isolated DB** are torn down
  in a `finally` (via `remove-worktree`), not just the directory. Set
  `epic.executor_worktree_args` in config (e.g. `["--frontend-only"]`) to run a lighter worktree
  for the executor.
