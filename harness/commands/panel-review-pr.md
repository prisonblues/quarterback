# Panel Review and Fix PR

@description Like /review-pr, but the findings come from the multi-reviewer PANEL (Claude + Codex + Antigravity + master judge + SonarCloud hard gate) instead of one sub-agent reviewer. Ensures a PR exists, runs ~/.claude/loops/panel.py (which comments the summary on the PR), then a sub-agent fixes every master-confirmed finding boil-the-ocean style and pushes. Give it several PR numbers and each one is reviewed+fixed by its own sub-agent, in parallel. Panel members default to the repo's .harness-rules and can be named explicitly. Merging stays opt-in.
@arguments $ARGS: [pr ...] [repo] [--reviewers a,b]  (defaults: the current branch's open PR in the cwd's repo, and the repo's configured reviewers)

You are the **ORCHESTRATOR**. This is `/review-pr` with the panel as the
finding engine: the panel finds, an autonomous sub-agent fixes everything to the
exact same bar. The only difference from `/review-pr` is *who reviews* — so
reuse its fixer brief rather than re-inventing it.

## 1. Ensure a PR exists (the panel diffs via `gh pr diff`, so a PR is required)

- Parse `$ARGS`: **every** integer is a PR number — `12`, `#12`, `12,14`, and
  `12 14 19` all parse; an optional non-numeric word (not a `--flag`) = repo.
- **PR number(s) given** → use them, in the order given.
- **No PR number** → resolve the current branch's open PR
  (`gh pr view --json number,baseRefName,headRefName`).
- **No PR yet** → push the branch and open one
  (`git push -u <remote> HEAD` then `gh pr create --fill`), then use it. (This
  is the agreed behaviour — auto-create rather than stop.)
- **No repo enrolment is needed.** The panel is read-only and resolves the repo
  from the checkout you are in; a repo with no `.harness-rules` runs on safe
  defaults (Claude + Codex, SonarQube off). Never stop for an "unconfigured repo".

Capture: `nameWithOwner`, the PR number(s), each PR's base and head branch, and
the absolute repo path of the working checkout.

**Pick the mode:** one PR → carry on to §3 and run it in this conversation.
**Two or more → §2, and do not run the panel yourself for any of them.**

## 2. Two or more PRs — fan out, one sub-agent per PR

Each PR gets its **own** `general-purpose` sub-agent that runs the whole
per-PR pipeline itself — panel then fix — so the PRs proceed in parallel and
none of their diffs land in your context. **Launch them in a single message**
(multiple Agent calls in one block) or they will not run concurrently. Launch
at most **4 at a time**; queue the rest and launch the next batch as they
return — each panel already runs several reviewer CLIs concurrently, and a
dozen at once just makes every one of them slower.

Each sub-agent's brief is §3 + §4 of this file for its own PR, with these
parallel-mode overrides:

- **Write the brief out in full — a sub-agent cannot see this file.** Read
  `~/.claude/commands/review-pr.md` **once** and paste its **SUB-AGENT BRIEF**
  into every agent's brief, alongside §3's panel instructions and that PR's
  resolved context (repo, remote, abs repo path, PR number, base, head branch).
- **No nested fixer.** §4 says to launch a fixer sub-agent; in parallel mode the
  per-PR agent *is* that fixer — it runs the panel and then fixes, rather than
  spawning a second agent to repeat the same pipeline. (It may still split a
  large, cleanly separable finding list across fix agents exactly as the brief's
  step 3 describes — that is fan-out *within* its own fix, not a nested
  pipeline. With several PRs already in flight it should be stingier about it:
  count its siblings against the ~4-concurrent budget.)
- **Always worktree mode, never fix-in-place** — concurrent agents cannot share
  your checkout. Each opens its own throwaway worktree at the PR's head
  (`git worktree add <tmpdir> <remote>/<branch>`, detached — this works even
  when a sibling agent, or you, has that branch checked out elsewhere) and
  removes it in a `finally` step. It pushes with `HEAD:<branch>`.
- **Pass the repo explicitly:** `python3 ~/.claude/loops/panel.py --repo <abs
  repo path> --pr <n> --post` — a sub-agent's cwd is not guaranteed to be your
  checkout, and `--repo` defaulting to cwd would silently review the wrong repo.
- A `--reviewers` list from `$ARGS` applies to **every** PR in the run.
- Each sub-agent returns its own panel summary (findings, SonarCloud gate,
  skipped reviewers) **and** the §4 fixer summary table for its PR.
- **One PR failing does not stop the others.** A sub-agent that cannot resolve
  its PR, or whose panel or verification fails, reports that and returns; the
  remaining agents run to completion regardless.

Then relay per §5: one table per PR, plus a one-line-per-PR roll-up (PR ·
findings · all fixed? · SonarCloud gate · pushed?) so the batch is readable at
a glance. Report each failed or partial PR explicitly — never let a roll-up
imply a PR was handled when its agent stopped early.

## 3. Run the panel

```
python3 ~/.claude/loops/panel.py --pr <pr> --post
```
(`--post` comments the panel summary on the PR by default — that is the review
record the fixer then resolves. Drop `--post` only if the user explicitly asked
not to comment.)

**Panel members** default to the repo's `.harness-rules`; pass no `--reviewers`
unless the user named who should review ("just codex", "codex and antigravity"), then
add `--reviewers <comma-list>` from `claude`, `codex`, `antigravity`, `sonarqube`. It
replaces the configured set rather than filtering it, so a named reviewer runs
even where the rules disable it. Fewer reviewers means thinner coverage feeding
the fixer — surface that in §5 rather than letting a one-vendor review read
like a full panel.

From its output collect:
- **To fix** — the master-confirmed findings (any reviewer count, P1–P4).
- **SonarCloud** — the hard-gate issues (these MUST end up resolved).
- **Skipped reviewers** — note them; a skipped Codex/Sonar means thinner
  coverage, surface it.

The run also records itself on the quarterback board (which models ran, what each
raised, and how the judge ruled on it) so the fleet accumulates an answer to
"which reviewer earns its cost" — see the board's `/panel` page. This is
automatic and best-effort: **do not** post the panel result to the board by hand,
and never re-run the panel to produce a record. A board that is down or
unconfigured prints one line and changes nothing about the review.

Show the user this panel summary before launching the fixer.

> First run may need `op signin` once (SonarCloud token caches afterwards) and
> `codex login` for the Codex reviewer. Missing reviewers are reported as
> skipped, not fatal.

## 4. Launch the fixer sub-agent

Read `~/.claude/commands/review-pr.md` and lift its **SUB-AGENT BRIEF** verbatim
— that is the canonical boil-the-ocean fix/verify/commit discipline; keep it
single-sourced. Launch **one** `general-purpose` sub-agent with that brief,
with these overrides:

- **Replace step 2 "Deep review" (self-discovery) with the supplied panel
  findings.** They are already exhaustively reviewed and judged — the sub-agent
  does NOT re-derive them. Paste the full **To fix** list and the **SonarCloud**
  issues into the brief as the findings to resolve.
- The sub-agent may still surface *additional* obvious defects it trips over
  while fixing, and must fix those too — but its job is to resolve **every**
  panel-confirmed finding (P1–P4) plus every SonarCloud issue, to the
  "nothing left to improve" standard.
- **Parallel fixes apply here too.** A panel list is typically longer than a
  single reviewer's, so the brief's step 3 fan-out (split a 6+ finding list into
  disjoint-file groups across fix sub-agents) is often the right call — the
  triage is already done, the findings arrive pre-grouped by file. Same rules:
  one file per group, nuanced findings stay with the fixer, and it keeps verify/
  commit/push.
- **Workspace:** if the PR's head branch == the current checkout's branch, fix
  **in place**; otherwise use the **plain throwaway `git worktree`** described
  in the brief (no Docker/DB/nginx; remove it in a `finally`).
- Keep all of the brief's verify (incl. DB-backed tests), branch-checkpoint,
  separate-commit, and push steps. Commit message:
  `fix: resolve panel review findings for PR #<n>`.

## 5. Relay the result

Show the sub-agent's summary table verbatim. Then state plainly: that the panel
summary was posted as a PR comment, the branch it pushed to, whether the
**SonarCloud hard gate** is now clear, whether all checks passed, anything flagged
**unverified**, and any reviewers the panel skipped. If the panel ran on a
hand-picked set rather than the repo's configured one, say which reviewers ran —
the fixer's bar is only as good as the review that fed it. If the sub-agent
stopped early, report exactly where and why.

## 6. Merging (only if the user asks)

`gh pr merge --merge --delete-branch` — preserve commits; never squash.
