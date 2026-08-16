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
4. **Pre-land gate (mechanical).** Work in the PR branch's checkout, clean tree, `git fetch origin`.
   Each guardrail is **capability-detected** — run it only if that script exists in the repo; a repo
   without it skips that guardrail silently. These fix what CI can only *detect*.

   **4a. Migration graph → exactly one head** (if `scripts/migration_reconcile.py` exists):
   ```bash
   uv run python scripts/migration_reconcile.py preflight --onto origin/$BASE --branch HEAD
   ```
   Act on the reported `action` — **never override the tool's choice**, it picks relink vs merge on
   guards you are not re-deciding:
   - **NOOP** — nothing to reconcile.
   - **RELINK** — rewrite the base migration's `down_revision` onto `$BASE`'s head. `apply` writes
     the file and does **not** commit, so commit that one line yourself:
     ```bash
     uv run python scripts/migration_reconcile.py apply --onto origin/$BASE --branch HEAD
     git add migrations/versions/<base>.py
     git commit -m "fix(migrations): rebase <base> onto $BASE head <HEAD_REV>"
     ```
   - **MERGE** — relink is unsafe (multiple bases, a forked branch head, or a base that is itself a
     merge node). Bring `$BASE` **into the branch**. The direction is the opposite of a guided
     integration merge: there you merge branch→base locally, but here the PR does that, so the two
     heads have to meet on the branch.
     ```bash
     git merge origin/$BASE          # resolve conflicts — see the cache-version rule in 4b
     uv run flask db merge heads -m "merge <branch> and $BASE heads"
     # rename to the repo's convention if it has one, e.g. mNNNa_merge_<desc>_heads.py
     git add -A && git commit
     ```
     HOLD if a conflict is not mechanically obvious. Resolving product code by guess is exactly the
     judgement this loop must not make on its own.
   - **STOP** — `$BASE` itself has more than one head, independent of this branch. **HOLD.**
     Reconciling the integration branch is not this issue's job.

   Then **re-verify**, and HOLD if it is not exactly one head:
   ```bash
   uv run python scripts/migration_reconcile.py preflight --onto origin/$BASE --branch HEAD
   ```

   **4b. Service-worker cache-bust monotonicity** (if `scripts/check_sw_version.py` exists):
   ```bash
   uv run python scripts/check_sw_version.py --base origin/$BASE
   ```
   `SERVICE_WORKER_VERSION` is one hand-maintained global counter that every branch edits, so
   parallel branches collide on merge and a careless resolution lands a value **≤ what is already
   deployed** — which silently breaks cache invalidation rather than failing. On REGRESSION or STALE
   BUMP let the tool fix it (`--fix` rewrites to `max(base, head) + 1` and re-stages), then commit.
   A broken multiline value (`SERVICE_WORKER_VERSION = (`) → **HOLD**; that needs a human to restore
   a single-line literal. The same rule governs a merge conflict on that line in 4a:
   `max(both) + 1`, keeping the branch's descriptive comment — **never take the branch's number
   blindly**.

   **4c. Release number** (if `scripts/release_stamp.py` exists):
   ```bash
   python3 scripts/release_stamp.py apply --onto origin/$BASE
   ```
   A branch that ships a release writes `vNEXT` and names no number, so this is where the number
   is decided — against `$BASE` **as it stands now**, which is the only moment the answer is
   knowable. It is a noop on a branch that ships no release, so run it unconditionally rather than
   guessing whether this one does. Exit 0 = stamped or nothing to stamp; **exit 2 = HOLD** and read
   the message: every refusal is a release entry in a shape the tool will not guess about (two
   unstamped entries, an entry below a released one, a placeholder somewhere nothing rewrites), and
   guessing is what this whole mechanism exists to stop. `apply` writes and does not commit, so
   commit what it produced. Re-run it if the branch takes a later merge from `$BASE` — the number
   is computed from the base and a moved base can move it.

   **4d. Push whatever 4a/4b/4c produced to the PR branch.** Those commits are mechanical (a
   `down_revision` line, a version counter, a release number the tool chose) and need no
   re-review; a MERGE-path merge commit brings in `$BASE` changes already reviewed on their own PRs.
5. **Confidence gate — MERGE only if ALL hold:**
   - `gh pr checks <pr>` is **green** (never merge on red/pending CI) — **re-checked after the
     step-4 push**, since that push restarts CI and any earlier green is stale,
   - the pre-land gate came out clean (single head re-verified, cache-version guard passing),
   - the panel's **To fix** is empty (no master-confirmed defects) and SonarCloud is not failing,
   - the change is low-risk and you are **genuinely confident** it is correct and complete.

   If all hold → `gh pr merge <pr> --squash --delete-branch`.
   If not → **STOP**, post a concise PR comment explaining what's unresolved, and leave it for a human.
6. **Report** the outcome: implemented / reviewed / pre-land actions taken / merged-or-held, and the
   confidence reasoning.

Rules:
- **Be honest about confidence.** When unsure, do NOT merge — holding for a human is the correct,
  safe outcome, not a failure.
- **Never** merge with red/pending CI, unresolved P1/P2 findings, or a failing SonarCloud gate.
- Higher-risk changes (auth, migrations, data, security-sensitive paths) should bias strongly toward
  holding even if gates pass.
- **`gh pr merge` is a server-side merge, so a repo's `pre-push` hook never fires on this path.**
  Whatever invariant that hook backstops is unprotected here — CI plus step 4 are what replace it.
  That is why 4a is not optional where the script exists.
- **Landing on `$BASE` may deploy.** For lexray, `test` is a semi-production environment; the
  absence of a sign-off step is the whole point of this skill, and the price is that step 4 gets run
  in full rather than assumed.
- **Check the squash commit body carries the issue's closing keyword** (`Fixes #N`) before merging.
  A repo that closes issues by reading the commits landing on its integration branch — lexray does —
  gets nothing from a PR-body keyword, and GitHub's default squash message depends on a repo
  setting. Pass `--body` explicitly if the default would drop it.
