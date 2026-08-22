# Loops — Fix and Land an issue (autonomous, confidence-gated merge)

@description Implement an issue, review it, run the mechanical pre-land guardrails, and MERGE it if confident enough — otherwise stop for a human.
@arguments $ARGS: <issue-number> [repo]   (repo defaults to the cwd's repo)

End-to-end autonomous flow for ONE issue. Unlike the epic driver (which always stops at the
human-merge gate), this **will merge** when the gates pass and you are genuinely confident. Treat it
deliberately — it lands code in a real repo.

This is the hybrid path: the guardrails of a guided integration merge (lexray's `/merge-to-test`)
**without** its human sign-off. It does not ask; it either satisfies the gate mechanically or holds.

The steps below are the decisions. **[The hazards](#the-hazards) at the end of this file are the
things that go wrong while you carry them out** — written from the symptom, because a landing that
has gone wrong announces itself as an error message and not as a cause. Read that section when
something reads as a fault: several of the worst ones read as a fault in your own PR and are not.

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

Everything above is about decisions — the queue, the gate, what you are and are not allowed to
judge. This section is the other half, and it is written from the *symptom* rather than the cause,
because the symptom is what you will be looking at when you need it. Every agent landing on
2026-08-22 needed these same facts and every one of them got them from a human writing them out
again — by the fifth the paragraphs were being pasted with the issue numbers swapped, which is
the point at which prose should have become a document (#367).

Two groups, and the split is the point. The first are properties of the tools — `gh`, `git`, dcg,
GitHub's own parser — and were true before this box existed. The second is one fact about **this
machine, today**, which will read as nonsense on another one, so it says how to tell rather than
asking to be believed. A list that mixes the two ages badly and then gets distrusted whole.

### Four of them already have a guard

Where a mechanism exists, this is one line saying so. Read what it says when it fires; do not
re-derive it, and do not go looking for the trap by hand.

- **A merge resolution that deletes a shipped release's notes** — the `frozen` job, *"no shipped
  release entry was rewritten"* (#325, v2.85). It compares the **text** of every `## vX[.Y]` entry
  present at both refs, byte for byte, because on `feat/issue-232` the branch's own entry was
  relocated *under* `## v2.59` on top of that release's notes and every heading-based check read
  the file as correct: the headings were all present, unique and correctly ordered.
- **A PR that ships something and writes no release note** — the `changelog` job, *"a change that
  ships carries a release note"* (#365, v2.95). It parses the fragments too, which nothing in CI
  did before it; a malformed fragment used to surface at `assemble` time, one merge queue away
  from the release entry coming out wrong.
- **A merge that would leave two migration heads** — the `migration-heads` job, *"one migration
  head after the merge"* (#351, v2.88). The `pre-push` hook asks the same question and never gets
  to answer it on the path this fleet lands by: the merge is an API call, and no push carries the
  commit that creates the second head.
- **CI that looks like it has not run** — `qbdata.CI_STATES` gave the state a word (#324, v2.78)
  and preland refuses on it: *"CI will not run without a human — the run for this head is gated
  and has executed nothing, so nothing is verifying this change"*. A run behind GitHub's
  workflow-approval gate is created, executes nothing, contributes no check runs, and leaves the
  PR's check list **empty** — which, before there was a word for it, was indistinguishable from a
  PR nobody had pushed to. PR #282 sat two days that way over a run that had gone red two commits
  earlier. `conclusion` alone cannot reach that distinction; `blocked` is the state that can.

All four reach you through preland's verdict anyway, which is why step 4b says the verdict is the
decision. Knowing them is diagnostic rather than procedural: it is what lets you read a HOLD as a
fact about the branch instead of a fault in the tooling.

### `--delete-branch` from a worktree: the merge landed, the branch survived, and the error reads like the merge failed

```
$ gh pr merge 216 --merge --delete-branch
failed to run git: fatal: 'main' is already used by worktree at '/home/rich/source/quarterback'
```

The merge had already landed. What did not happen is *either* half of the cleanup. To delete the
local branch `gh` first moves off it, onto the default branch — and a sibling worktree holds that
branch, so `git checkout main` exits 128 and `gh` abandons everything after it, including the
**remote** delete, which never needed a local checkout at all (#260).

The trigger is narrow and this loop lives inside it: cwd is a worktree not on the default branch,
and the default branch is checked out somewhere else. That is the shape of every landing here.

So do not read that message as a failed merge. Ask: `gh pr view <pr> --json state,mergeCommit`.
If it merged, finish the cleanup by hand — `git push origin --delete <branch>` — and say in your
report that the merge landed and the cleanup did not.

**What #260 does not establish is `gh`'s exit code**: the original observation was piped through
`tail`, so the status captured was `tail`'s. The abandoned remote delete proves `gh` gave up on
its cleanup; it does not prove the process exited non-zero. Treat the message as unreliable and
the branch as probably still there, and do not infer anything further than that — `lander.py`
merges under `check=True`, and whether that raises after a successful merge turns on the very
number nobody has captured.

### A PR body saying "this does not close #N" closes #N

GitHub's closing-keyword parser does not understand negation. PR #372 opened with

> **This does not close #371** — see the bottom.

and `closingIssuesReferences` listed #371. Merging as written would have closed the issue the PR
existed to keep open. A person reads that sentence as a disclaimer, which is exactly what makes it
harder to catch than PR #363's plain `Closes #63` in a body arguing that #63 stays open — the same
failure, the same day, and a keyword grep catches only the first of the two (#374).

**A grep is not the check.** Ask GitHub what it parsed:

```bash
gh api graphql -f query='
  query($o:String!,$r:String!,$n:Int!) {
    repository(owner:$o, name:$r) {
      pullRequest(number:$n) { closingIssuesReferences(first: 10) { nodes { number state } } }
    }
  }' -f o=prisonblues -f r=quarterback -F n=<pr>
```

That is the same list the merge will act on, and it is immune to phrasing, negation and prose.
Reword the body until `nodes` is empty — *"#371 stays open"* did it — and then **query again**
rather than assuming the edit worked.

This is not an edge case here. The repo has a real and correct habit of partial PRs — #277's
`stop` half, #371's button, #63's reporter — so a body that discusses an issue it must not close
is the normal shape, and prose about not-closing is the most natural thing to write in it. #374
proposes a `pull_request` check comparing the commit trailer against `closingIssuesReferences`;
if it has landed, that check is the answer and this section is background for the day it does not
run.

Note the converse, from step 9 of `/fix-issue`: a PR that *should* close its issue needs the
keyword in the **commit** body, because a PR-body keyword only fires when the PR merges into the
repository default branch.

### Impossible test failures that move between runs are a second pytest, not your branch

A landing agent reported **118 failures** against PR #349's merged result on 2026-08-22, re-ran
the suite on its own, and got a clean pass. It had a targeted suite running concurrently against
the same worktree database (#366).

The shape to recognise: a row the fixture just committed reported absent, foreign-key violations,
`ObjectDeletedError` — failures that are impossible rather than merely surprising, and that move
around between runs. The other run's provisioning path terminates every backend on the database
and drops it, and what the victim reports is a scattered set of **assertion** failures rather than
a connection error — which is precisely what makes it read as the PR's fault. (#366 attributes the
absent error to `pool_pre_ping` handing back a reconnected connection; treat the observed shape as
the fact and that as the explanation offered for it.) PR #30 gave every worktree its own database,
which removed the cross-worktree case and not this one.

Nothing guards it — #366 is open. So: **one pytest at a time per worktree.** Do not start a
targeted run while a full one is going, which is precisely what a lander does by reflex. And when
a suite comes back with failures that look impossible, check for a concurrent pytest *before* you
read them as the PR's, because the honest response — re-run it — makes them disappear and confirms
the wrong conclusion.

### Undoing a change: both obvious ways are refused, and each one recommends the other

`git stash push` stops here with

```
REFUSED: refs/stash is shared across every worktree of this repo.
```

That is a `reference-transaction` hook `create-worktree` installs, and it is right: `refs/stash`
lives in the **common** git dir, so a stash pushed in one worktree is listed and poppable from
every sibling and `stash@{0}` there means whatever the last pusher meant. Two working trees have
already gone that way (#210). `qb-stash push` is the per-worktree replacement — same verbs, stored
under `refs/worktree/`, invisible to siblings, and it dies with the worktree.

`git checkout HEAD -- <path>` is refused too, by dcg's `core.git:checkout-ref-discard`, whose
advice is *"Use 'git stash' first"* — the thing the paragraph above refuses. The two point at each
other, and on 2026-08-22 two agents worked around it independently without either knowing the
other had.

The way through is a patch file, which is what `/fix-issue`'s red/green step wants anyway, since
`qb-stash` takes no pathspec:

```bash
git add -N <the files your change touched or added>
git diff HEAD -- <those same files> > .redgreen.patch
test -s .redgreen.patch || { echo "STOP: captured nothing"; exit 1; }
git apply -R .redgreen.patch     # the change is gone
git apply .redgreen.patch        # and back
```

`git apply` in both directions is allowed; it is not a checkout and it takes a pathspec by
construction. Delete the patch afterwards: it is untracked, so preland reports it as a warning
rather than a reason, and what it actually costs you is a `git add -A` sweeping it into the commit.

**The redirect in that snippet is written as a relative path, and that is load-bearing.** dcg's
`core.filesystem:redirect-truncate-root-home` refuses a truncating redirect whose target is
spelled as an **absolute** path under `$HOME`; the same redirect written relative to a directory
you have already `cd`'d into is allowed. Both measured, 2026-08-22. So a heredoc addressed to the
full path of a file in your worktree does not run at all, and the refusal reads as your command
being wrong rather than as a policy — while the same command one `cd` earlier goes through. `>>`
is allowed at either spelling because it does not truncate, and `tee` is not a redirect; either is
what to reach for when the path has to be absolute.

**Both rules match on the command's TEXT, not on what it will do**, which is the part that costs
you time rather than a file. Writing this section tripped the redirect rule from a `>` inside a
Python string an editing script was carrying, and tripped the checkout rule from the literal
`git checkout HEAD -- <path>` inside a commit message being passed on a heredoc. Neither command
was going to do the thing. So when a refusal names a rule you are not breaking, look for the
pattern quoted in your argument — and put long prose in a file and pass the path, which is the
fix for both.

### "Served version unchanged" is the correct answer for a harness-only release

After a harness-side land, `qb-doctor` reports the board serving the number it served before, and
it reads as a failed deploy every time. It is not one.

`pyproject.toml` and `app/main.py` carry the version `GET /openapi.json` reports, and
`release_stamp.py` moves it only when the branch changed `app/` or `migrations/` — `BOARD_PATHS`
is exactly those two, and `harness/`, `scripts/`, docs and tests are deliberately outside it.
Most releases here are harness-side and correctly leave the served version alone. The release
**number** moves, in CHANGELOG.md and README.md; the served version does not, and `qb-doctor` then
says *"matching this checkout"* because the checkout did not move either.

The bump is inferred rather than declared, and `apply` always reports which way it went, so read
that line rather than guessing. `--serve` / `--no-serve` override the inference for the release it
gets wrong.

### One thing that is true of this box and not of the tools — checked 2026-08-22

`harness/tests/test_create_worktree_claim.py::test_a_missing_qb_claim_does_not_abort_the_run_under_set_e`
fails on this machine and passes in CI. It is a **host artefact**, not your branch. Do not chase it.

The test runs `create-worktree`'s claim stanza with no `qb-claim` available and asserts stderr
carries `qb-claim is not on PATH`. Here `qb-claim` **is** on PATH — the user profile puts it at
`/etc/profiles/per-user/rich/bin/qb-claim` — so the stanza gets further than the test expects and
fails on a different message, about the fixture repo having no resolvable remote. The CI runner has
no such profile, so the assertion holds there.

That shape recurs, and it is worth knowing as a class rather than as one test name: a failure that
is local-only in this repo is usually a harness CLI on `PATH` that the runner does not have. Check
it by stripping the profile out of `PATH` for one run, not by pushing to find out — a round trip
through CI to answer a question this box can answer in a second is the expensive way to read an
assertion.

And treat this subsection as perishable in a way the ones above it are not. It is a claim about a
machine's `PATH` on a date. The day `qb-claim` stops being installed here, or the test learns to
stub it, this paragraph is wrong — and nothing else on this page is affected by that, which is
the whole reason it is fenced off down here.
