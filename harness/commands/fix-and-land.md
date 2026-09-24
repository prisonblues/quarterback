---
description: "Implement an issue, review it, run the mechanical pre-land guardrails, and MERGE it if confident enough — otherwise stop for a human."
argument-hint: "<issue-number> [repo]   (repo defaults to the cwd's repo)"
---

# Loops — Fix and Land an issue (autonomous, confidence-gated merge)

End-to-end autonomous flow for ONE issue. Unlike the epic driver (which always stops at the
human-merge gate), this **will merge** when the gates pass and you are genuinely confident. Treat it
deliberately — it lands code in a real repo.

This is the hybrid path: the guardrails of a guided integration merge (lexray's `/merge-to-test`)
**without** its human sign-off. It does not ask; it either satisfies the gate mechanically or holds.

The steps below are the decisions. **[The hazards](#the-hazards) at the end of this file point at
the things that go wrong while you carry them out** — written from the symptom, because a landing
that has gone wrong announces itself as an error message and not as a cause. Read them when
something reads as a fault: several of the worst ones read as a fault in your own PR and are not.

**If the merge should stay yours, run `/fix-and-review` instead** — same implement-and-review, an
independent agent for the review, every mechanical prep step run, and it stops at merge-ready. The
two differ in one step, and picking between them is the whole decision; there is no flag here that
turns the merge off.

1. **Parse** `$ARGS`: first integer is the issue number; optional repo (default: the cwd's repo).
   Run `python3 ~/.claude/loops/harness_rules.py --repo <repo> --json` (omit `--repo` only when
   no repo argument was given) to see that repo's resolved `github`,
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
   for it.

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
     and it expires by itself if nobody comes back. Leaving would re-join at the back, which
     starves the PR every time it is overtaken.

     **While you wait, poll the queue — that is what holds your place** (#405):

     ```
     merge_queue(pr=<pr>, base="$BASE")
     ```

     Asking where you are renews your own entry, and it is the only thing you have to do. Until
     this landed, the only act that renewed an entry was a push — the one act this very step
     forbids — so the agents that obeyed the queue were the ones it retired, and a PR that had
     waited politely for half an hour came back to the line at the BACK, behind PRs that had never
     integrated. Check `renewal.renewed` in the answer: `false` with a reason naming another
     holder means this session is not the one the entry is filed under, and a fresh
     `merge_queue_enqueue` (idempotent, and `entered_at` never moves) puts that right.

     If you are stopping rather than waiting, that is fine and the entry lapses on its own — which
     is correct, because nobody is working it. Re-enqueue when you come back.
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

   **Once READY, there is no release step.** A branch that ships something carries exactly one
   release note, `changelog.d/<issue>.<kind>.md`, naming no version. The version number is applied
   on `$BASE` after the merge by `scripts/release.py run`, never by this loop. Do not edit
   `CHANGELOG.md` or the README's release list: every branch touching those files conflicts with
   every other, and `pre-push` and the `generated release files are output` CI job both refuse it.
   If the fragment is missing, preland is already HOLD; write the fragment, commit, push, and re-run
   step 4.

   So step 4 ends at READY. Nothing is pushed here that was not already pushed, nothing is
   re-enqueued, and the head preland read is still the head.

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
   claim_id=$(qb-claim branch "$BASE" --ttl 1800 --note "landing PR #<pr>" --json)  # 0/1/2
   python3 ~/.claude/loops/preland.py --pr <pr> --json --claim-holder "<the holder it printed>"
   gh pr merge <pr> --squash --delete-branch
   ```

   ```
   merge_queue_leave(pr=<pr>, base="$BASE", entry_id="<the id 4a returned>", reason="merged")
   release_claim(claim_id="<$claim_id>")
   ```

   ```bash
   qb-release issue <n>          # the issue this PR closes — see the note below
   ```

   - **`verdict="ready"` is the one assertion that lets a queue head merge**, and it is pinned to
     this commit: the board clears it the moment the head moves, which is the thing an agent's own
     memory of "preland said READY" structurally cannot do. Say it here rather than at 4a, because
     at 4a it was not true yet — and everyone behind you reads it to know the line is about to
     move rather than merely occupied.
   - **`release_claim` gives back the MERGE claim; `qb-release` gives back the WORK claim** (#337).
     They are two claims on two resources: `kind=merge` on the base, taken above and held across
     the merge, and the `kind=work` claim on the issue that `create-worktree` took at checkout —
     machine-held, no session, 8h TTL, and untouched by anything on the landing path. On
     2026-08-22 four issues were still claimed hours after their PRs had merged. Forgetting it
     breaks nothing (the worktree teardown releases it too, and the TTL is under both) but it
     holds a slot: under `in_flight.max` the count is highest immediately after the fleet has been
     most productive. Exit 0 also means "nothing to release", so it is safe to run twice.
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
   - **`--ttl 1800`, not the board's hour.** Keying the claim on the base (#318) widened what a
     leaked one costs: it now blocks every merge onto `$BASE`, not one branch's. The TTL is the
     only backstop for a session that dies between the claim and the release, so it is set to the
     same window a queue entry gets — a land that takes longer than half an hour has gone wrong,
     and an hour of nobody landing is a jam bought for no margin anyone needs.
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

6. **Tear the worktree down — only if you merged.** A hold leaves everything standing; the branch
   is still being worked. On a merge, this loop is the last thing that will ever run in that
   worktree, and nothing else reaps it. `create-worktree` starts a stack per worktree — for lexray
   an app container plus a huey consumer with four workers, 1–2 GB a pair — and every workflow that
   says "`/drop-worktree` when the PR merges" is addressing a session that has usually ended by the
   time the merge happens. So the instruction lands on nobody: 45 containers holding 16.7 GB
   accumulated on zeus this way and OOM-killed the compositor on 2026-08-31.

   From a fresh shell (it is already at the main checkout, which is what `git worktree remove`
   needs), using the create-name — the worktree dir's suffix after `<project>-`:

   ```bash
   remove-worktree "$CREATE_NAME"
   ```

   That drops the containers, the nginx block, the isolated DB, the port entry and the directory,
   and hands back the board claim on the issue the create-name names. Best-effort: if it refuses,
   say so in the report and carry on — a failed teardown must not turn a successful land into a
   failure.

   - **It refuses a branch carrying commits its PR never took**, which is the guard working, not an
     error: something was committed after the PR and deleting the branch would be its last stop.
     Push them and re-run, or `--keep-branch` to drop only the worktree. Do **not** reach for
     `--force` on your own initiative.
   - **`--delete-branch` on the merge does not do this.** It deletes the remote branch; the
     worktree, its containers and its DB are all still here. See [The hazards](#the-hazards).

7. **Report** the outcome: implemented / reviewed / your place in the line / pre-land verdict and
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
- **Ask GitHub which issues the merge will close; never grep the body for keywords.** GitHub's
  closing-keyword parser does not understand negation. PR #372 opened with "**This does not close
  #371** — see the bottom", the parser matched the literal `close #371`, and merging as written
  would have closed the issue the PR existed to keep open — while a keyword grep returned that one
  hit and it read, to a human, as a disclaimer. The authoritative list is the one the merge acts on:

  ```bash
  gh api graphql -f query='{repository(owner:"OWNER",name:"NAME"){
    pullRequest(number:N){closingIssuesReferences(first:50){totalCount nodes{number state}}}}}'
  ```

  Run it before merging. If it lists an issue the PR is meant to leave open, reword the body until
  it does not (`#N stays open` parses as nothing) and re-run the query until `nodes` is empty — then
  merge. If it lists an issue the PR really does close, make sure a commit says `Fixes #N` too, for
  the reason the bullet above gives. The `closing-refs` CI job asks the same question against the
  branch's own reference lines and refuses the contradiction, but it passes — with a `::warning::`,
  not a refusal — when the body picks up an issue no commit names at all, and it cannot see one where
  the commit and the body agree with each other and only the prose disagrees, which is PR #363's
  case. Both are why this is still a step here (#374).

## The hazards

When a landing step fails with a message that reads like a fault in your PR, read
`~/.claude/loops/docs/landing-hazards.md` (in a checkout: `harness/loops/docs/landing-hazards.md`)
before acting. It is indexed by symptom: which CI guards already catch a trap (read what they say);
`--delete-branch` failing from a worktree after the merge landed; impossible test failures from a
concurrent pytest on the same worktree database (run one pytest at a time per worktree); `git stash`
and a ref checkout both refused (use a patch file and `git apply -R`); and `qb-doctor` reporting an
unchanged served version after a harness-only release.
