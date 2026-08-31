# Loops — Reviewer Panel

@description Run the multi-reviewer panel (Claude + Codex + Antigravity + master judge; SonarCloud hard gate where the repo enables that seat) on a PR and post the summary as a PR comment by default. Give it several PR numbers and each is panelled by its own sub-agent, in parallel. Panel members default to the repo's .harness-rules.sample; name them explicitly to run a subset or a single vendor. A repo with no rules file at all is REFUSED rather than reviewed on built-in defaults.
@arguments $ARGS: <pr ...> [repo] [--no-post] [--reviewers a,b]   (repo defaults to the cwd's repo)

Run the reviewer panel over a pull request. Each reviewer (and the master judge)
applies the **same exhaustive bar as `/review-pr`** — full Core / Completeness /
Craft review (correctness, security, error handling, concurrency, performance,
test coverage and whether the tests present are load-bearing, docs, related code,
naming, complexity, style, DRY), ranked P1–P4,
with the "nothing left to improve" standard. The master keeps every genuine
finding (style and polish included) and drops only true false positives — the
panel just adds independent reviewers on top of that bar, plus the hard CI gate and —
where the repo enables the `sonarqube` seat — the SonarCloud one.

1. Parse `$ARGS`: **every** integer is a **PR number** — `12`, `#12`, `12,14` and `12 14 19` all
   parse; an optional non-numeric word (not a `--flag`) is the **repo** (default: the cwd's repo).
   **Default is to post** the summary as a PR comment; pass `--post` only
   when the user said `--no-post` (then omit it and skip the comment for a read-only run).
2. **Panel members.** Default to the repo's `.harness-rules.sample` (narrowed by the box's own
   untracked `.harness-rules`) — pass no `--reviewers` at all. Pass it
   only when the user named who should review, in any phrasing ("just codex", "codex and antigravity",
   "run the whole panel"): map that to a comma-separated list of `claude`, `codex`, `antigravity`,
   `pi`, `grok`, `sonarqube`. The flag REPLACES the configured set rather than filtering it, so naming a reviewer
   runs it even where the rules disable it — which is the point. Never guess a member the user
   didn't ask for, and never pass the flag to "be explicit" about the default.
3. Run (from anywhere — post-by-default, drop `--post` only on `--no-post`):
   ```
   qb-stage R1                                                # what the statusline shows
   python3 ~/.claude/loops/panel.py --pr <pr> --post          # add --repo <path|name> for another repo
   python3 ~/.claude/loops/panel.py --pr <pr> --post --reviewers codex          # single-vendor read
   python3 ~/.claude/loops/panel.py --pr <pr> --post --reviewers claude,codex,antigravity,grok
   ```
   **Run it in the background** (`run_in_background`), not as a foreground Bash call. A reviewer on
   a top-tier model at high effort can think for 20+ minutes, and the foreground Bash timeout caps
   at 10 — which kills the whole panel, not just the slow seat, and reads afterwards as a panel that
   never ran. Poll the background task instead.
4. **Two or more PRs → one sub-agent per PR, run in parallel.** Give each `general-purpose`
   sub-agent one PR and the same step-2/3 instructions, and **launch them in a single message** or
   they will not run concurrently. Cap at **4 at a time**, queueing the rest: each panel already runs
   its reviewer CLIs concurrently, so a dozen at once only makes every one of them slower. Each
   sub-agent must get `--repo <abs repo path>` explicitly — its cwd is not guaranteed to be your
   checkout, and `--repo` defaulting to cwd would silently panel the wrong repo — and returns just
   its step-6 summary for its PR, written under step 5's reporting rules, so no PR's diff or
   reviewer output lands in your context. A `--reviewers` list applies to every PR in the run.
   **One PR failing does not stop the others:** an unreadable PR or a dead reviewer CLI is that
   agent's report to make, and the rest run to completion.
5. **A refused or manifest round is not a clean round — report it as what it is.** Before
   dispatching a seat the panel rules on whether the round is worth running (see
   `loops/README.md`, "the pre-flight verdict"). Read `preflight.verdict` from the payload, or
   the warning the report prints above the findings:
   - `refuse` — **nobody reviewed anything.** The payload is `reviewed: false` with a
     `skip_reason`. Never summarise this as a clean PR or as "no findings": say the panel
     declined, quote its reason, and give the user the remedies it named (split the PR, raise
     the cap, or re-run with `--force`). Do not pass `--force` yourself unless the user asks
     for it — the refusal is the tool's decision, and overriding it silently is the bug the
     check exists to prevent. A refusal still reads CI, so report `ci_status`/`ci_failing`
     alongside it — a red build is the one hard fact a refused round still has, and it is why
     the extra API call is made.
   - `manifest` — the change is move-shaped and the seats were asked what *moved*, not whether
     the code is correct. **The moved code was not read by anybody.** Report the findings as
     answers about the move (what did not survive, what changed besides moving, duplicated
     definitions) and say explicitly that its correctness is carried over from when it landed
     on the base branch, not established here.
6. Show the user the output: **To fix** (master-confirmed, any reviewer count), **Dismissed by
   master**, **SonarCloud issues** where the `sonarqube` seat ran, any skipped reviewers, and the
   **Coverage declared** block —
   what each reviewer said it could not assess, and any reviewer the panel truncated. A clean
   panel whose reviewers each read half the diff is not a clean PR, and the finding list alone
   cannot tell you which one you got. Confirm whether the summary was
   posted to the PR. If the panel ran on a hand-picked set, say so — "reviewed by codex alone" is a
   materially weaker claim than "reviewed by the panel", and the reader of the PR comment can't tell
   the difference from the findings. For a multi-PR run, give that block per PR plus a
   one-line-per-PR roll-up (PR · to-fix count · SonarCloud gate, or `n/a` where the seat did not
   run · posted?), and name any PR whose
   agent stopped early rather than letting the roll-up imply it was panelled.

   **"The hard gate is clear" may only be said about a round that had one.** `sonarqube` is
   `enabled: false` in this repo's rules and off in the harness defaults, and it is being switched
   off across the fleet while the convergence work is proven — so most rounds have no SonarCloud
   block, and an empty one would be a claim that a gate looked and found nothing. Read
   `reviewers_ran`, and where the seat did not run say there was no gate rather than reporting a
   clear one.

   **Two kinds of "skipped", and only one is a coverage gap.** A seat the repo has **configured
   off** is a decision somebody took: state it once as configuration if it is worth stating at all,
   and do not warn about it every round — that is how a reader learns to skim the line where the
   other kind appears. A seat **configured on that did not run** — missing CLI, dead login, a
   crash — makes the review thinner than the one that was asked for and nobody chose that: report
   it every time, with what its absence cost.

7. **Land-readiness: report it, never offer it.** If the user wants to know whether the PR could
   merge, `python3 ~/.claude/loops/preland.py --pr <pr> --repo <path>` answers it, and reporting
   the verdict — `gates: READY`, or `blocked by: <its reasons>` — is useful and in scope. What is
   not in scope is an offer to merge, in any wording, however green the gate came back. **This
   command never offers to land and never merges.** It is pointed at other people's PRs, in repos
   the caller may not own and did not write, and an offer to merge one of those is a footgun
   whether or not it is accepted. `/panel-review-pr` §7 is the one that may offer, and it has
   earned that: it owns the branch, it wrote the fix commits, and it re-reviewed them. Say so and
   stop, rather than proposing the step this command does not take.

Notes:
- Posts the summary as a PR comment by default. This is review-only — it never edits code, never
  merges, and never offers to merge; pass `--no-post` for a silent read-only run. It reviews the
  PR **as it is now**: use `/panel-review-pr` when the fix that follows should itself be reviewed,
  which is what its rounds are for, and when the review should end with an offer to land.
- `sonarqube` is off in the harness defaults and `enabled: false` in this repo's
  `.harness-rules.sample`, and it is being switched off across the fleet while the convergence work
  is proven. That is a temporary deactivation of a seat that comes back, not the removal of the
  hard-gate concept: where a repo turns it on, its issues are a hard gate and MUST end up resolved.
  Where it is off there is no gate on the round, and step 6 is where that has to be said rather
  than implied.
- First run needs `op signin` once where that seat is on (the SonarCloud token then caches),
  `codex login` for the Codex reviewer, `agy` auth for the Antigravity one and `grok login` for the
  Grok one; missing reviewers are reported as skipped, not fatal.
- `antigravity`, `pi` and `grok` are off unless a repo's `.harness-rules` enables them — each is a
  workstation-only CLI on a personal account, so none reaches the work box. `--reviewers` still runs
  them on demand anywhere the CLI exists.
- `--force` overrides a pre-flight refusal (and the manifest substitution) and reviews the diff
  as content. Pass it only when the user asked for it. What it overrode is recorded in
  `preflight.would_have`, printed above the findings and posted to the PR — an override is a
  decision, not a way of avoiding one.
- The master judge is always `claude`, whoever the reviewers are. `--reviewers antigravity` on a machine
  without the claude CLI therefore returns findings **unjudged** rather than adjudicated — the report
  says so, and every finding is kept.
