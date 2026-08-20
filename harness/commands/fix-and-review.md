# Loops — Fix and Review an issue (implement, independent review, merge-ready, stop)

@description Implement an issue, have an INDEPENDENT agent panel-review it, do every piece of merge prep that is stable — and stop at the merge for a human. `/fix-and-land` without the landing.
@arguments $ARGS: <issue-number> [repo] [--rounds N|--loop] [--base <branch>]   (repo defaults to the cwd's repo)

End-to-end for ONE issue, up to but **not including** the merge. This is the command for anything
you intend to look at yourself: it spends the review properly, leaves the PR mechanically
merge-ready, and hands you the decision.

The three siblings, so this is picked deliberately:

| | implements | reviews | merges |
|---|---|---|---|
| `/fix-issue <n>` | yes | no — you run `/review-pr` yourself, later | no |
| **`/fix-and-review <n>`** | yes | **yes, by an agent that did not write it** | **no — stops at merge-ready** |
| `/fix-and-land <n>` | yes | yes | **yes**, on a mechanical gate + your confidence |

If you find yourself wanting this one to merge, that is not a missing flag — it is `/fix-and-land`.

1. **Parse** `$ARGS`: the first integer is the issue number; an optional non-numeric word is the
   repo (default: the cwd's repo). `--rounds N` / `--loop` pass straight through to
   `/panel-review-pr` (default: its default, 2). Consume every `--flag` **with its value** before
   reading the integers, or `--rounds 3` reviews issue 3 — `panel-review-pr.md` §1 has the parsing
   rule and the reason it is written that way, **including the one exception**: `--loop` takes no
   value and is consumed alone (`--loop 42` is `--loop` and issue #42, not a flag whose value is
   `42`). Applying the general rule to `--loop` eats the issue number and leaves you with none,
   which is the bug class the rule exists to prevent, one flag over. A `--flag` this command does
   not know is **not** silently swallowed: say which flag you did not recognise and stop, rather
   than running a narrower command than the caller asked for.

   Then resolve the repo's own answers — do not assume this repo's — and say which repo you asked
   about, because the default is the cwd and the cwd is the thing being overridden:
   ```bash
   python3 ~/.claude/loops/harness_rules.py --repo <repo> --json    # omit --repo only when no repo argument was given
   ```
   `harness_rules.py --repo` takes "path or name (default: cwd)", so a bare invocation reads *this*
   checkout's answers and uses them for a PR in another repo — exactly what the sentence above
   forbids. (`fix-and-land.md`:19 carries the identical defect; fix them together or they drift.)

   `$BASE` = `executor_pr_base` (`test` for lexray, `main`/`master` elsewhere), unless `--base`
   overrode it. `headless_permission_mode` is what the sub-agents run under.

   **Then check the repo argument is one you can actually honour.** `/fix-issue` has **no repo
   parameter** — its `@arguments` is `<issue-number> [--base <branch>] [--shared-db | --isolated-db]`
   and it operates on the canonical remote of whatever checkout it runs in. So a repo argument
   naming anything other than the cwd's repo cannot be carried into step 2: it would resolve
   *that* repo's rules and then implement the issue *here*, silently. If `<repo>` was given and
   does not match the cwd's repo (`gh repo view --json nameWithOwner`), **stop and say so** — the
   fix is to run the command from that repo's checkout, not to proceed.

2. **Implement:** run `/fix-issue <issue> --base $BASE`. It plans, implements, tests, opens a PR
   against the right base, and leaves its worktree in place.

   **Capture three things and carry them everywhere below:** the PR number, the **absolute worktree
   path** it worked in (`WT_DIR`), and the branch. A sub-agent's cwd is not your cwd and nothing
   downstream can re-derive them from the conversation.

   **Fail loud on nothing.** If `/fix-issue` produced no artifact — no PR, and no commit beyond
   `$BASE` — stop and say so. Do not proceed to review an empty diff; a clean panel over no change
   is the most convincing wrong answer this command can produce. (`epic.py --execute` asserts the
   same thing for the same reason.)

3. **Review — with an agent that did not write the code.** Launch **one** `general-purpose`
   sub-agent (Agent tool) whose entire job is to run `/panel-review-pr <pr> --rounds <N>` for this
   PR, to completion, and return its §6 relay.

   **Why not run it here.** This conversation now holds the author's model of the change: every
   reason the code is the way it is, and none of a reader's surprise. That is exactly the context a
   review needs *not* to have — and `/review-pr` says in its own description that it exists for
   "fresh-eyes review without a new conversation", which running it in the implementing
   conversation quietly spends. It is also #40's constraint one level up: an agent reviewing its own
   work is grading itself, and the board cannot tell a fixer from a reviewer, so it records the
   verdict either way.

   **Write the brief out in full — the sub-agent cannot see this file, or any command file you
   name.** Read `~/.claude/commands/panel-review-pr.md` once and paste its §§3–6 into the brief
   verbatim; do not hand over the *reference* "`panel-review-pr.md` §§3–6" and assume the sub-agent
   will resolve it. This is `panel-review-pr.md` §2's own rule for its own fan-out ("Write the brief
   out in full — a sub-agent cannot see this file"), and it applies here for the same reason plus
   one more: `~/.claude/commands` is a nix store symlink, so a sub-agent asked to resolve a slash
   command may be reading a different version of it than you are, or none at all. If the paste
   fails — the file is not where you looked — stop and say so rather than sending a reference.

   The pasted brief covers the whole cycle for this PR, including the re-review rounds, which are
   not yours to run afterwards. Add to it:
   - the resolved context: repo `nameWithOwner`, remote, **`WT_DIR`**, PR number, base and head
     branch, and the round cap;
   - `--repo <WT_DIR>` on every `panel.py` invocation, since its cwd is not guaranteed to be the
     checkout (§2's parallel-mode rule, for the same reason);
   - **the issue number and its text, and nothing about how you implemented it.** It needs to know
     what the change was meant to do; it does not need your defence of how.

   Wait for it. Do not review in parallel in this conversation — two fixers in one working tree
   clobber each other, and the second opinion you would get is the one whose independence you just
   paid for.

   **What comes back that you must not absorb quietly:**
   - **An escalation** (`review-pr.md` step 3a): the fixer reported that a finding says the
     *approach* is wrong rather than the code, and wrote no patch. That is a report about the
     approach **you** chose, which is precisely why it is not yours to answer: do not brief another
     fixer at it, do not redesign, carry it to step 5 as the headline. Another fix pass on an
     escalated premise is the round that manufactures the next round's findings (#67).
   - **A stop that was not convergence** — `confident: false`, with a veto list. Report it as a
     stop, never as clean.
   - **Coverage** — anything a reviewer declared it could not assess, and any reviewer that was
     skipped or truncated. A finding count reports "clean" and "I could not tell" as the same zero.

   The sub-agent's own `qb-stage R1`/`R1F` calls land on this session when it inherits the
   environment, which is the answer the bar should be giving while a review runs. Best-effort;
   nothing here depends on it.

4. **Merge prep — everything that is stable, and nothing that is not.**

   **First, re-sync `$WT_DIR` to the PR's head — step 3 almost certainly moved it and did not
   touch your copy.** Per `panel-review-pr.md` §4, the review sub-agent fixes in place only if the
   PR's head branch is already its own checkout's branch; otherwise it works in a throwaway
   `git worktree`, pushes `HEAD:<branch>`, and deletes it. That is the documented default whenever
   the fixer's cwd is not the PR branch — which is the sub-agent's situation every time. So after
   any round that produced a fix, the branch has moved on the remote and `$WT_DIR` is still at the
   commit `/fix-issue` left there:
   ```bash
   git -C "$WT_DIR" fetch origin && git -C "$WT_DIR" merge --ff-only @{u}
   ```
   This is not housekeeping — it is load-bearing for the very next command. `preland.py`'s
   `checkout` check compares `git -C $WT_DIR rev-parse HEAD` against the PR's `headRefOid` and
   fails on any mismatch; HOLD dominates the verdict and the HOLD bullet below forbids clearing it
   with `--skip`. Skip the re-sync and **every PR whose review actually produced a fix dead-ends at
   a HOLD that misreports your stale local checkout as a merge blocker** — the command's happy path
   broken for the case it exists to serve. If the fast-forward is refused, `$WT_DIR` has commits the
   remote does not: **stop and say so**, do not force anything.

   ```bash
   python3 ~/.claude/loops/preland.py --pr <pr> --repo "$WT_DIR" --json
   ```
   `--repo` explicitly, never "run it from the worktree": the shell cwd resets between tool calls,
   so a `cd` does not stick and a defaulted `--repo` reads whichever checkout you were launched in —
   which is the one whose branch is not under review. **If that path does not exist, run it out of
   the checkout** — `python3 "$WT_DIR/harness/loops/preland.py" --pr <pr> --repo "$WT_DIR" --json` —
   and do not read a missing file as permission to skip the step. `~/.claude/loops` is a nix store
   symlink, so it is exactly as current as the last home-manager rebuild and nothing announces the
   gap.

   **The verdict is the decision.** Act on it; never substitute your own reading of the same facts.
   - **RECONCILE (exit 3)** → run every command in `actions`, in order, verbatim. Commit what they
     produce (they deliberately do not commit), push, and **run preland again** — the push restarts
     CI, so the earlier green describes a commit that is no longer the head. Those commits are
     mechanical (a `down_revision`, a version counter, a generated merge migration) and need no
     re-review. Never override the reconciler's choice of action. A `git merge` in `actions` that
     conflicts anywhere not mechanically obvious is a **stop**, not a judgement call.
   - **HOLD (exit 2)** → stop here and report `reasons` as they are written. Do not clear a HOLD by
     re-running with the check turned off: `--skip` and `.harness-rules` are for repos that
     genuinely lack a guardrail, not for a verdict you dislike.
   - **READY (exit 0)** → preland is satisfied, but **prep is not done**. Do 4a and 4b below, in
     that order, and only then go to step 5. They are part of this step, not optional trailing
     prose: step 5 asks you to report what `preflight` said, and the closing-keyword check is the
     one piece of prep that is load-bearing on a non-default `$BASE`.

   **4a. Ask the release-number question without spending the answer.** `release_stamp.py apply`
   resolves `vNEXT` against `$BASE` **as it stands now**, and this command hands the PR to a human
   who merges later — by which time another branch may have taken that number, leaving `apply`
   refusing with nothing left to rewrite and a hand-edit as the repair (`fix-and-land.md` step 4
   documents it). A number is only knowable at the moment of the merge, so ask the cheaper question
   and leave the number alone:
   ```bash
   python3 "$WT_DIR/scripts/release_stamp.py" preflight --repo "$WT_DIR" --onto origin/$BASE
   ```
   **The script path is `$WT_DIR`-relative, not cwd-relative** — `--repo` only chooses which repo
   the plan is built *against*, not where the script is loaded *from*, so a bare
   `python3 scripts/release_stamp.py` pairs one checkout's tool with another checkout's files, which
   is the same defect the `preland.py` paragraph above spends five lines refusing.

   **This script is per-repo, so detect it rather than assuming it.** `scripts/release_stamp.py`
   does not exist in every repo this command runs against (lexray has no such file). If it is
   absent, **skip 4a with a stated reason** and say so in step 5 — this is `preland.py`'s own
   `_detected()` discipline, which skips a guardrail that is not on the branch instead of failing
   on it. A bare `python3: No such file or directory` at exit 2 is **not** "a refusal carrying the
   sentence that repairs it"; reading it as one reports a missing tool as a release-number problem.

   Where the script *is* present: report what it said. Exit 2 is a genuine refusal carrying the
   sentence that repairs it — quote that sentence rather than paraphrasing it, because it is the
   whole value of running this early. On a branch that ships no release it is a noop, so run it
   unconditionally once you know the file is there. Whoever merges runs `apply` then (a human, or
   `/fix-and-land`).

   **4b. Check the closing keyword.** If `$BASE` is not the repo's default branch, a `Closes #N` in
   the PR body fires on nothing: the keyword has to be in a commit message that lands on the default
   branch. `/fix-issue` puts `Fixes #N` in its commit body for exactly this reason — confirm it is
   there, and add a commit that carries it if it is not.

   **If you added that commit, push it and run preland again**, for the reason the RECONCILE bullet
   already states: a push restarts CI, so the green preland just reported describes a commit that is
   no longer the head, its `checkout` check is now at a stale sha, and its `review` check finds the
   newest recorded round read the previous head. Quoting the earlier READY to the human after
   pushing describes code the PR no longer contains. Flag it as what it is, too — a commit riding
   out unreviewed, which `panel-review-pr.md` §5 otherwise forbids; it is allowed here only because
   it is a message-only commit with no code in it, and if yours has code in it, it is not this
   commit and needs a round.

5. **Report, and hand over the merge.** One block, in this order, because it is read top-down by
   someone deciding whether to press the button:

   - **Escalations first, or "none".** The premise, what it explains, what removing it would cost,
     the patch that was not written, and the `--ask` verdict if one was run. This is a question
     addressed to the reader, and no round will close it.
   - **Rounds:** how many panel/fix cycles ran, what each found that the last had not, what stopped
     it, and **whether the stop was earned** (`confident: false` → say so in those words, with the
     vetoes).
   - **Coverage:** per reviewer, what it could not assess; any reviewer skipped or truncated; any
     disagreement between them, which is more informative than either verdict alone.
   - **preland:** the verdict, quoted, plus anything a RECONCILE did and the fact that preland was
     re-run after it.
   - **Release number:** what `preflight` said, and that `apply` is deliberately left to the merge —
     or that 4a was skipped because this repo ships no `scripts/release_stamp.py`, which is a fact
     about the repo and not a step that failed.
   - **The PR, the issue, and `WT_DIR`** — the worktree stays up so review findings can be
     addressed on the same branch and DB (`/drop-worktree` when the PR merges).
   - **One sentence: the merge is yours.** Say what it would be — `gh pr merge <pr> --merge` for a
     repo that preserves commits — without running it.

Rules:

- **This command never merges.** Not on a green gate, not on your own confidence, not when asked
  mid-run. Say that `/fix-and-land` is the command that does and let the user choose it
  deliberately — a merge is not a flag on a review.
- **Never review your own implementation in this conversation.** If the review sub-agent fails or
  returns short, say so and stop; re-running it as yourself converts an independent review into a
  self-assessment without anything saying that is what happened.
- **Never re-run a panel round to get a nicer answer.** Each round is recorded on the board as an
  observation; re-rolling one corrupts the record it exists to be.
- **Leave the worktree in place.** Findings get addressed on the branch and the DB that produced
  them. `/drop-worktree` when the PR lands, or `/tree-shake` to sweep later.
- **A stop is a result.** An issue that ends at HOLD with the reasons written down is this command
  working, not failing. The failure mode it exists to remove is a PR that looks reviewed.
