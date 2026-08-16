# Loops — Fix and Land an issue (autonomous, confidence-gated merge)

@description Implement an issue, review it, run the mechanical pre-land guardrails, and MERGE it if confident enough — otherwise stop for a human.
@arguments $ARGS: <issue-number> [repo]   (repo defaults to the cwd's repo)

End-to-end autonomous flow for ONE issue. Unlike the epic driver (which always stops at the
human-merge gate), this **will merge** when the gates pass and you are genuinely confident. Treat it
deliberately — it lands code in a real repo.

This is the hybrid path: the guardrails of a guided integration merge (lexray's `/merge-to-test`)
**without** its human sign-off. It does not ask; it either satisfies the gate mechanically or holds.

1. **Parse** `$ARGS`: first integer is the issue number; optional repo (default: the cwd's repo).
   Run `python3 ~/.claude/loops/harness_rules.py --json` to see that repo's resolved `github`,
   `executor_pr_base` and `headless_permission_mode`. Loop commands run from anywhere. Let `$BASE` =
   `executor_pr_base` (`test` for lexray, `main`/`master` elsewhere).
2. **Implement:** run `/fix-issue <issue>` (it plans, implements, tests, pushes a branch, opens a PR
   against the right base). Capture the PR number and the branch checkout it worked in.
3. **Review:** run the panel — `python3 ~/.claude/loops/panel.py --pr <pr>`, in the **background**
   (a slow reviewer outlives the 10-minute foreground Bash cap, which would kill the panel) — and run
   `/review-pr <pr>` to address findings. Repeat review→fix until the panel's **To fix** list is
   empty and CI is green.
4. **Pre-land gate (mechanical).** Work in the PR branch's checkout and ask for the verdict.
   Do not re-derive it:
   ```bash
   python3 ~/.claude/loops/preland.py --pr <pr> --json
   ```
   **If that path does not exist, run it out of a checkout** — `python3 harness/loops/preland.py
   --pr <pr> --repo . --json` — and do NOT read the missing file as permission to skip step 4.
   `~/.claude/loops` is a nix store symlink, so it is exactly as current as the last
   home-manager rebuild and nothing announces the gap; a box whose flake pin predates this script
   simply does not have it yet. "The gate would not run, so I merged" is the failure this step was
   written to remove, arriving through the step itself.

   It exits **0 = READY**, **3 = RECONCILE**, **2 = HOLD**, and the payload says why: `reasons`
   (what is unresolved and who has to resolve it), `actions` (the exact commands a RECONCILE needs
   and the files they touch), `warnings`, and `checks` — per guardrail, whether it ran, was skipped
   for want of the script it needs, or was turned off.

   **The verdict is the decision.** Act on it; never substitute your own reading of the same facts
   for it. That substitution is exactly what this replaced: on 2026-08-16 a PR was merged on
   `mergeable` + CI-green over its own panel round, which had 8 P1s outstanding, by an agent who had
   written up that precise confusion an hour earlier and had itself recorded the PR as blocked.

   - **HOLD** → stop. Post `reasons` as a PR comment and leave it for a human. Do **not** clear a
     HOLD by re-running with that check turned off; `--skip` and `.harness-rules` exist for repos
     that genuinely lack the guardrail, not for a verdict you dislike.
   - **RECONCILE** → run every command in `actions`, in order, verbatim. Commit what they produce
     (they deliberately do not commit for you), push, and **run preland again**. Those commits are
     mechanical — a `down_revision` line, a version counter, a generated merge migration — and need
     no re-review. **Never override the reconciler's choice of action**: relink vs merge turns on
     guards you are not re-deciding. If a `git merge` in `actions` conflicts anywhere that is not
     mechanically obvious, that is a HOLD — resolving product code by guess is the judgement this
     loop must not make on its own.
   - **READY** → step 5.

   Re-running after the push is not optional. The push restarts CI, so the `ci` check's earlier
   green is a statement about a commit that is no longer the head — and preland is what re-reads it,
   along with everything else the push may have staled.

5. **Confidence gate — MERGE only if BOTH hold:**
   - preland's **last** run, after the final push, came out **READY**, and
   - the change is low-risk and you are **genuinely confident** it is correct and complete.

   The first is mechanical and preland owns it whole: the PR is open and not conflicting, CI is
   green *now*, the panel's newest round read *this* head and stopped with nothing confirmed and no
   failing Sonar gate, the migration graph lands on one head, and nobody else holds the merge claim
   on the branch. Do not re-check those by hand and do not weigh them against each other. A READY
   you talk yourself past and a HOLD you talk yourself through are the same failure in two
   directions.

   The second is yours, and it is stated separately because it is not mechanical and never will be:
   preland can tell you nothing objects. It cannot tell you the change is a good idea.

   If both hold → `gh pr merge <pr> --squash --delete-branch`.
   If not → **STOP**, post a concise PR comment quoting preland's `reasons`, and leave it for a human.

6. **Report** the outcome: implemented / reviewed / pre-land verdict and any actions taken /
   merged-or-held, and the confidence reasoning. Quote the verdict; do not paraphrase it.

Rules:
- **Be honest about confidence.** When unsure, do NOT merge — holding for a human is the correct,
  safe outcome, not a failure.
- **Never** merge with red/pending CI, unresolved P1/P2 findings, or a failing SonarCloud gate.
  Every one of those is a preland HOLD, so this rule now survives as the *reason* the gate exists
  rather than as a second checklist to run by hand — and a second checklist is how the two drifted
  apart in the first place.
- Higher-risk changes (auth, migrations, data, security-sensitive paths) should bias strongly toward
  holding even if gates pass.
- **`gh pr merge` is a server-side merge, so a repo's `pre-push` hook never fires on this path.**
  Whatever invariant that hook backstops is unprotected here — CI plus step 4 are what replace it.
  That is why step 4 is not optional, and why it is a script rather than a paragraph: a paragraph
  cannot be re-run after the push that staled it, and cannot be asked afterwards whether it ran.
- **preland is advisory and says so.** It is a script this loop chooses to run; it cannot stop a
  human merging in the UI, or a loop that skips the step. What would actually block a merge is a
  required status check on a protected branch, which does not exist for this repo yet.
- **Landing on `$BASE` may deploy.** For lexray, `test` is a semi-production environment; the
  absence of a sign-off step is the whole point of this skill, and the price is that step 4 gets run
  in full rather than assumed.
- **Check the squash commit body carries the issue's closing keyword** (`Fixes #N`) before merging.
  A repo that closes issues by reading the commits landing on its integration branch — lexray does —
  gets nothing from a PR-body keyword, and GitHub's default squash message depends on a repo
  setting. Pass `--body` explicitly if the default would drop it.
