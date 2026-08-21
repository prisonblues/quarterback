# Loops — Fix and Land an issue (autonomous, confidence-gated merge)

@description Implement an issue, review it, run the mechanical pre-land guardrails, and MERGE it if confident enough — otherwise stop for a human.
@arguments $ARGS: <issue-number> [repo]   (repo defaults to the cwd's repo)

End-to-end autonomous flow for ONE issue. Unlike the epic driver (which always stops at the
human-merge gate), this **will merge** when the gates pass and you are genuinely confident. Treat it
deliberately — it lands code in a real repo.

This is the hybrid path: the guardrails of a guided integration merge (lexray's `/merge-to-test`)
**without** its human sign-off. It does not ask; it either satisfies the gate mechanically or holds.

**If the merge should stay yours, run `/fix-and-review` instead** — same implement-and-review, an
independent agent for the review, every mechanical prep step run, and it stops at merge-ready. The
two differ in one step, and picking between them is the whole decision; there is no flag here that
turns the merge off.

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

   **An escalated finding stops this loop; it does not go round again.** `/review-pr`'s brief lets a
   fixer report that a finding says the *approach* is wrong rather than the code (`/review-pr`'s
   step 3a) instead of patching it. Such a finding never leaves the **To fix** list, so "repeat
   until empty" would either spin or — worse — hand it to a fresh fixer who writes the patch the
   last one declined to write, which is #67's whole observation about where a fix round's findings
   come from. This loop has no human in it to ask, so: post the escalation as a PR comment (the
   premise, what it explains, what removing it would cost, the `--ask` verdict if there was one), do
   **not** merge, and stop for a human. A redesign is never this loop's call — that is the one
   judgement it exists not to make on its own, and it sits beside the confidence gate in step 5
   rather than inside it, because preland cannot see it either: the escalation reaches preland only
   as an unresolved confirmed finding, and a HOLD saying "1 finding unresolved" is not the same
   sentence as "the approach is in question".
4. **Join the line, then ask for the verdict.** Work in the PR branch's checkout.

   **4a — take a place in the merge queue, before any integration push.**

   ```
   merge_queue_enqueue(pr=<pr>, base="$BASE", head="<headRefOid>",
                       verdict="queued", note="fix-and-land: issue #<issue>")
   ```

   `head` is `gh pr view <pr> --json headRefOid`. The MCP tool derives the repo from the
   checkout's origin remote; with no quarterback MCP in this session, `POST /merge-queue/enqueue`
   on the board takes the same body plus `repo`.

   The queue is keyed on `<repo>` + `$BASE` — the branch being landed ONTO — and it exists
   because `kind=merge` is one slot that says *somebody is landing now* and cannot say who is
   next. Without it every review-clean PR behaves as though it were: merge the base, push, wait
   for CI, re-run the gate, discover somebody else landed, repeat. That is #80's quadratic
   integration cost, and each loser's integration push invalidates the winner's green checks on
   the way past. Enqueueing **before** step 4b is the whole point: the expensive half is the
   integration, so the stop has to come in front of it rather than in front of the merge.

   It is idempotent and re-registering never costs your place (`entered_at` is written once), so
   call it again whenever the head moves. `verdict="queued"` is the honest thing to say here —
   preland has not run yet, and the board takes your word for a verdict pinned to a commit rather
   than measuring one.

   **4b — the gate.** Ask for the verdict; do not re-derive it:
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

   - **HOLD whose only unresolved check is your place in the line** — `checks.queue.status` is
     `failed` and every other check reads `passed` or `skipped-*`. You are in the queue and not at
     the head. **Stand down**: report `checks.queue.reasons` — they name your position and the
     agent holding the place ahead — and stop. (`checks.queue.status` of `error` is a different
     thing: the board could not be read, so take the branch below. And if the reason says you are
     not in the line at all, 4a did not land — run it again rather than proceeding past a check
     that cannot see you.) Do **not** rebase, push or restart CI: you would
     spend a run to learn what the board already told you, and invalidate the head's checks doing
     it. Do **not** post a PR comment; the position changes on its own, and a comment per attempt
     is noise on a PR whose only problem is its turn.

     **Do not leave the queue here.** This is the one stop that keeps your entry — it is a lease,
     it is renewed by re-enqueueing, and it expires by itself if nobody comes back. Leaving would
     re-join at the back, which starves the PR every time it is overtaken.
   - **HOLD for anything else** → stop, and **leave the line on the way out**:

     ```
     merge_queue_leave(pr=<pr>, base="$BASE", entry_id="<the id 4a returned>",
                       reason="held: <the first reason, in a few words>")
     ```

     Then post `reasons` as a PR comment and leave it for a human. Leaving is not optional: an
     entry for a PR that cannot land sits in the line holding everybody behind it up until its
     TTL runs out, which is why `enqueue` refuses a `hold` verdict on the way in. Do **not** clear
     a HOLD by re-running with that check turned off; `--skip` and `.harness-rules.sample` exist for
     repos that genuinely lack the guardrail, not for a verdict you dislike.
   - **RECONCILE** → you are at the head, because HOLD dominates and the queue check would have
     held otherwise — and the head is the one entry entitled to push. Run every command in
     `actions`, in order, verbatim. Commit what they produce (they deliberately do not commit for
     you), push, **re-enqueue at the new head** and **run preland again**:

     ```
     merge_queue_enqueue(pr=<pr>, base="$BASE", head="<the new headRefOid>",
                         verdict="reconcile")
     ```

     The push moved the head, so the entry is pinned to a commit the PR is no longer on and its
     readiness is void — telling the board which commit you are on is what stops the line
     advertising a green light about code nobody checked. Those commits are mechanical — a
     `down_revision` line, a version counter, a generated merge migration — and need no re-review.
     **Never override the reconciler's choice of action**: relink vs merge turns on guards you are
     not re-deciding. If a `git merge` in `actions` conflicts anywhere that is not mechanically
     obvious, that is a HOLD — resolving product code by guess is the judgement this loop must not
     make on its own, and it is a HOLD that leaves the line.
   - **READY** → step 5.

   **Once READY, before you push: the release entry, then its number.** A branch that ships a
   release writes a `changelog.d/<issue>.<kind>.md` fragment and names no version at all, so the
   entry is BUILT here — and then numbered against `$BASE` **as it stands now**, which is the only
   moment the answer is knowable.

   ```bash
   rc=0
   python3 scripts/changelog_fragments.py assemble || rc=$?
   [[ $rc -eq 0 ]] || { echo "HOLD: read the error above"; exit "$rc"; }
   python3 scripts/release_stamp.py apply --onto origin/$BASE || rc=$?
   [[ $rc -eq 2 ]] && echo "HOLD: read the STOP above"
   [[ $rc -eq 0 ]] || exit "$rc"
   ```

   `assemble` folds every fragment present into one `## vNEXT — <title>` entry and adds the
   matching README bullet, then deletes what it consumed. It is a noop with no fragments, so it
   runs unconditionally like the stamp below it. Past ONE fragment it refuses without `--title`,
   and that refusal is a HOLD rather than something to work around: the release heading is the
   line a reader scans, several fragments have no shared title anywhere to derive one from, and
   picking one is a judgement about what the release MEANS — the same class as `--major`, which
   this loop also does not make on its own.

   The `|| rc=$?` is not decoration. Exit 2 is a refusal carrying the sentence that repairs it, and
   under a `set -e` wrapper a bare invocation terminates the surrounding script before anything
   reads the message — so the one output that makes a HOLD actionable is the one output that gets
   lost. Capture the status once, because `$?` is gone the moment anything else runs, and exit with
   it rather than a flat 1: same 0/2 scheme as `migration_reconcile.py`, and for the same reason —
   a caller consuming it reads Python's uncaught-exception 1 as "unknown" rather than as "stop".

   It is a noop on a branch that ships no release, so run it unconditionally rather than guessing
   whether this one does. Every refusal names its own repair, so read the message rather than
   matching it against a list of causes. Most are a release entry in a shape the tool will not
   guess about — two unstamped entries, an entry below a released one, a placeholder somewhere
   nothing rewrites, a number written by hand that `$BASE` has not reached, or one already taken
   there — but it also refuses on things that are not the entry at all: an unclosed code fence or
   non-UTF-8 in the markdown it scans, a missing or symlinked `pyproject.toml` or `app/main.py`, an
   `--onto` it cannot resolve or that carries no CHANGELOG.md.

   **If the branch was already stamped and `$BASE` has since taken that number**, `apply` refuses
   rather than re-stamping — the placeholder is gone, so there is nothing left for it to rewrite.
   The message names the repair and it is two tokens: put this branch's entry back to
   `## vNEXT — …` and its README bullet back to `- **vNEXT** — …`, then run this step again.
   Nothing else on the branch was ever written in terms of the number, which is what makes that an
   edit rather than a rewrite.

   `apply` writes and does not commit, so commit what it produced and push it. That commit is
   mechanical — a release number the tool chose — and needs no re-review.

   Re-running after the push is not optional, and neither is re-enqueueing at the commit it
   produced. The push restarts CI, so the `ci` check's earlier green is a statement about a commit
   that is no longer the head — and preland is what re-reads it, along with everything else the
   push may have staled. The queue entry is staled by exactly the same push, and the board cannot
   see it happen:

   ```
   merge_queue_enqueue(pr=<pr>, base="$BASE", head="<the stamp commit's oid>", verdict="queued")
   ```

5. **Confidence gate — MERGE only if BOTH hold:**
   - preland's **last** run, after the final push, came out **READY**, and
   - the change is low-risk and you are **genuinely confident** it is correct and complete.

   The first is mechanical and preland owns it whole: the PR is open and not conflicting, CI is
   green *now*, the panel's newest round read *this* head and stopped with nothing confirmed and no
   failing Sonar gate, the migration graph lands on one head, this PR is at the head of the line
   for `$BASE`, and nobody else holds the merge claim on that base. Do not re-check those by hand
   and do not weigh them against each other. A READY
   you talk yourself past and a HOLD you talk yourself through are the same failure in two
   directions.

   The second is yours, and it is stated separately because it is not mechanical and never will be:
   preland can tell you nothing objects. It cannot tell you the change is a good idea.

   If not → **STOP**, leave the line (`merge_queue_leave(..., reason="held: …")`), post a concise
   PR comment quoting preland's `reasons`, and leave it for a human.

   If both hold, **say so on the line, claim the base, re-verify, merge, then stand down** — in
   that order:

   ```
   merge_queue_enqueue(pr=<pr>, base="$BASE", head="<headRefOid>", verdict="ready")
   ```

   ```bash
   claim_id=$(qb-claim branch "$BASE" --note "landing PR #<pr>" --json)  # 0 yours / 1 held / 2 unknown
   python3 ~/.claude/loops/preland.py --pr <pr> --json --claim-holder "<the holder it printed>"
   gh pr merge <pr> --squash --delete-branch
   ```

   ```
   merge_queue_leave(pr=<pr>, base="$BASE", entry_id="<the id 4a returned>", reason="merged")
   release_claim(claim_id="<$claim_id>")
   ```

   - **`verdict="ready"` is the one assertion that lets a queue head merge**, and it is pinned to
     this commit: the board clears it the moment the head moves, which is the thing an agent's own
     memory of "preland said READY" structurally cannot do. Say it here rather than at 4a, because
     at 4a it was not true yet — and everyone behind you reads it to know the line is about to
     move rather than merely occupied.
   - **Being at the head of the queue is not the claim.** The queue orders; `kind=merge` is the
     one slot held across the merge itself, and the board's own answer says as much: *"take
     `kind=merge` on this base before you merge"*. Two agents at the head of two different bases,
     or a human merging in the UI, are both still possible — the claim is the only thing between
     you and somebody else's simultaneous merge, and it has to be taken BEFORE the merge rather
     than recorded after it.
   - **`$BASE`, not the PR's branch.** The claim keys on the branch being landed ONTO (#318),
     which is what `preland`'s `merge_claim` check reads and what the queue reports beside its
     line. Claim the head branch and the two name one land two ways.
   - **Exit 1** means another agent is landing onto `$BASE` right now: stop, say who holds it,
     and stay in the queue — your turn has not gone anywhere. **Exit 2 is "cannot tell"** — a
     board outage, a rotated token, no `qb-claim` on this box — and an autonomous loop resolves
     that the way it resolves every other uncertainty: do not merge. This loop has no human in it
     to ask whether landing unserialised is acceptable.
   - **Re-run the gate after claiming**, because time passed: CI can have gone red and the head
     can have moved. `--claim-holder` takes the `holder` field out of `qb-claim --json` so your
     own claim is not read as somebody else's. Anything but READY here ends the sequence — report
     the new verdict, and **release the claim on the way out** (`release_claim(claim_id="$claim_id")`)
     before you leave the queue and stop.
   - **Once you have taken the claim, every exit releases it — the merge and the stop alike.**
     `qb-claim` prints the claim id on stdout and everything else on stderr, which is what makes
     `claim_id=$(…)` above the whole capture. A claim left behind by a loop that stopped is worse
     than a queue entry left behind: it is `preland`'s `merge_claim` check answering "somebody is
     landing onto `$BASE`" to **every other agent in the fleet**, for the rest of its TTL, about a
     land that is not happening. Nobody merges onto that base in the meantime. Pass the same
     `session` you claimed with if the release is refused — `qb-claim` defaults it to
     `$CLAUDE_CODE_SESSION_ID`, and a claim that named a session is owned by that session.
   - **Leaving the queue is the last step and it is not optional.** The line advancing is the
     moment every PR behind this one may start spending CI, and until the entry goes they are all
     correctly waiting for a land that already happened. It expires on its own, but a lease
     nobody released is a queue that jams for the length of its TTL.

6. **Report** the outcome: implemented / reviewed / your place in the line / pre-land verdict and
   any actions taken / merged-or-held, and the confidence reasoning. Quote the verdict; do not
   paraphrase it. A stand-down says its position and what it is waiting on; a proceed says it
   checked and found the line clear. Neither is allowed to be silent about the queue — a stop
   whose reason nobody can read is indistinguishable from a loop that gave up.

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
- **Your queue entry is a lease, and every exit from this loop releases it.** Merged, held,
  abandoned, handed to a human — the entry goes, with a reason, except on the one stop that is
  *about* the queue, where keeping your place is the point. (Step 3's escalation stops the loop
  before step 4a, so there is nothing to release there.) The TTL (30 minutes by default) is the
  backstop for the exit nobody coded: a session that dies frees its place with nobody intervening.
  An entry nobody releases is a queue that jams, which is worse than no queue at all.
- **The queue is ordering, not a second lock.** Being at the head is permission to go and ask for
  the `kind=merge` claim; it is not the claim, it does not hold anything, and it does not outrank
  a holder who never enqueued at all. A human merging in the UI is entitled to, and the queue
  reports them rather than overriding them.
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
