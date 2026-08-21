# Loops — Reviewer Panel

@description Run the multi-reviewer panel (Claude + Codex + Antigravity + master judge; SonarCloud hard gate) on a PR and post the summary as a PR comment by default. Give it several PR numbers and each is panelled by its own sub-agent, in parallel. Panel members default to the repo's .harness-rules; name them explicitly to run a subset or a single vendor.
@arguments $ARGS: <pr ...> [repo] [--no-post] [--reviewers a,b]   (repo defaults to the cwd's repo)

Run the reviewer panel over a pull request. Each reviewer (and the master judge)
applies the **same exhaustive bar as `/review-pr`** — full Core / Completeness /
Craft review (correctness, security, error handling, concurrency, performance,
test coverage, docs, related code, naming, complexity, style, DRY), ranked P1–P4,
with the "nothing left to improve" standard. The master keeps every genuine
finding (style and polish included) and drops only true false positives — the
panel just adds independent reviewers + hard CI/Sonar gates on top of that bar.

1. Parse `$ARGS`: **every** integer is a **PR number** — `12`, `#12`, `12,14` and `12 14 19` all
   parse; an optional non-numeric word (not a `--flag`) is the **repo** (default: the cwd's repo).
   **Default is to post** the summary as a PR comment; pass `--post` only
   when the user said `--no-post` (then omit it and skip the comment for a read-only run).
2. **Panel members.** Default to the repo's `.harness-rules` — pass no `--reviewers` at all. Pass it
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
   the step-5 summary for its PR, so no PR's diff or reviewer output lands in your context. A
   `--reviewers` list applies to every PR in the run. **One PR failing does not stop the others:**
   an unreadable PR or a dead reviewer CLI is that agent's report to make, and the rest run to
   completion.
5. Show the user the output: **To fix** (master-confirmed, any reviewer count), **Dismissed by
   master**, **SonarCloud issues**, any skipped reviewers, and the **Coverage declared** block —
   what each reviewer said it could not assess, and any reviewer the panel truncated. A clean
   panel whose reviewers each read half the diff is not a clean PR, and the finding list alone
   cannot tell you which one you got. Confirm whether the summary was
   posted to the PR. If the panel ran on a hand-picked set, say so — "reviewed by codex alone" is a
   materially weaker claim than "reviewed by the panel", and the reader of the PR comment can't tell
   the difference from the findings. For a multi-PR run, give that block per PR plus a
   one-line-per-PR roll-up (PR · to-fix count · SonarCloud gate · posted?), and name any PR whose
   agent stopped early rather than letting the roll-up imply it was panelled.

Notes:
- Posts the summary as a PR comment by default. This is review-only — it never edits code or
  merges; pass `--no-post` for a silent read-only run. It reviews the PR **as it is now**: use
  `/panel-review-pr` when the fix that follows should itself be reviewed, which is what its
  rounds are for.
- First run needs `op signin` once (the SonarCloud token then caches), `codex login` for the Codex
  reviewer, `agy` auth for the Antigravity one and `grok login` for the Grok one; missing reviewers
  are reported as skipped, not fatal.
- `antigravity`, `pi` and `grok` are off unless a repo's `.harness-rules` enables them — each is a
  workstation-only CLI on a personal account, so none reaches the work box. `--reviewers` still runs
  them on demand anywhere the CLI exists.
- The master judge is always `claude`, whoever the reviewers are. `--reviewers antigravity` on a machine
  without the claude CLI therefore returns findings **unjudged** rather than adjudicated — the report
  says so, and every finding is kept.
