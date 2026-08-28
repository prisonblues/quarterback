# Review and Fix PR

@description Delegated boil-the-ocean PR review+fix. Resolves the target (current branch by default, or a PR number), then launches ONE autonomous sub-agent that reviews, fixes every finding, writes tests, runs the quality pipeline, amends, and pushes. Run it right after a fix — fresh-eyes review without a new conversation.
@arguments $ARGS: [pr-number]  (optional — defaults to the current branch's diff vs its base)

You are the **ORCHESTRATOR**. You do **not** review or fix in this
conversation. You resolve the target, launch a single autonomous sub-agent
that does the entire boil-the-ocean pass, and relay its summary back. This
keeps the heavy diff-reading and fresh-eyes review out of your context — so
you can fire this right after making a change and carry on.

## 1. Resolve the target

- **PR number given** (`$ARGS` has an integer) → review that PR. Detect the
  canonical remote (`upstream` if it exists, else `origin`); the sub-agent
  works on the PR's branch **in a plain throwaway `git worktree`** (see the
  brief) so your current checkout is never disturbed.
- **No argument** → review **the current branch's own work**: its diff vs the
  base branch. Determine the base = merge-base with the repo default branch
  (`gh repo view --json defaultBranchRef`) or the branch's upstream. The
  sub-agent fixes **in place** (you are already on the branch you want fixed —
  no worktree, no checkout thrash). Uncommitted changes in the working tree
  ARE part of the review target; tell the sub-agent to include them.
  - **Fail fast:** if the current branch IS the repo's default branch
    (`main`/`master`/the detected default), STOP here — do **not** launch the
    sub-agent. In-place mode commits to the current branch, and that must never
    be a shared/default branch. Tell the user to switch to a feature branch
    (or pass a PR number to use the isolated-worktree path instead).
  - Be explicit to the user: in-place mode **adds a commit to the branch you
    are on** — it is the only mode that changes your current branch's state (by
    design — it's finalising the work you just did).

Capture: repo (`gh repo view --json nameWithOwner`), remote, base branch, the
PR number (if any), the branch name, and the absolute repo path. Pass all of
these to the sub-agent.

## 2. Launch the fixer sub-agent

Stamp the stage so the statusline says what this session is doing:
```bash
qb-stage R1
```
One agent here both reviews and fixes, so unlike `/panel-review-pr` there is no
separate `R1F` to stamp — the honest answer for the whole run is `R1`. Splitting
it would mean guessing when the sub-agent stopped reading and started writing,
and a bar that says `R1F` while a reviewer is still reading is worse than one
that says less. `/drop-worktree` clears it.

Launch **one** `general-purpose` sub-agent (Agent tool). Its brief is the
complete boil-the-ocean discipline below. It runs autonomously to completion —
find, fix, verify, amend, push — and returns a summary. Do not babysit it; do
not pre-empt its work in this conversation.

> Pass the resolved target context (repo, remote, base, PR number, branch, repo
> path, and whether to fix-in-place or use a worktree) at the top of the brief.

---
### SUB-AGENT BRIEF — Review and fix this PR to the "nothing left to improve" bar

You are an autonomous reviewer-fixer. Execute every step sequentially. Within the
scope this pass is given, **fix everything you find** — never note a problem and
move on, never dismiss a finding as "just style" or "minor" or "can do later". The
standard is not "good enough" — it's "nothing left to improve".

**What that scope IS is a repo setting, not your judgement.** The orchestrator
tells you which values are in force (`review_panel.*` in `.harness-rules`; a panel
report prints them on its **Panel dials** line). Four of them define this pass:

- **`fix_severity_floor`** (default `P3`) — the severity at or above which a
  finding gets fixed. Below it a finding is reported and recorded and **not** fixed
  by this pass. A panel report puts those under their own heading, *Reported, not
  this round's work*, marked 🔽; do not lift them into your list.
- **`low_severity_fix_lines`** (default `40`) — the churned lines the WHOLE pass may
  spend on findings below `round_trigger_floor` (`P2` by default, so this is the P3
  band). A panel report marks those 💸. Step 3 has how to spend it; what matters here
  is that it is a budget for the round and not a cap per fix, because the failure it
  answers was 408 lines of individually reasonable small fixes on a 185-line PR.
- **`reviewer_scope`** (default `diff`) — whether the change under review is the
  target or the starting point. Under `diff`, findings are about the change and the
  seams where it meets what was already there.
- **`fixer_may_defer`** (default `true`) — whether "real, and not this change's
  job" is a thing you may say. See the next paragraph.

None of that lowers the bar for what IS in scope: those findings get fixed
properly, with a test, and "note it and move on" is still forbidden. What the
settings bound is the size of the list, never the quality of the work on it. The
measurement behind them: across seven PRs, 128 of 201 new findings — 63.7% — were
created by the fix pass immediately before them, against a ~7% industry baseline
for bad-fix injection (#165).

**Three things may leave a finding unfixed, and each is named.** Two are always
available: a genuine false positive you re-examined and confirmed correct, and an
**escalation** — the finding says the *approach* is wrong rather than the code, and
patching it at the line it names is what produces the next round's findings (step
3a). The third is available while `review_panel.fixer_may_defer` is on, which is
the default: a **deferral** — the defect is real, and it is not what this change is
for. Everything that is none of the three gets fixed. None of them is a fix you
skip quietly: each costs you a write-up in step 6 and nothing else on the list.

**A deferral is not "not now" as a way out of work.** It costs you two lines — why
the defect is real, and what this change is for such that the defect sits outside
it — and it has to go somewhere: once you have relayed, the ORCHESTRATOR records
the finding `deferred` on the board, and opens a GitHub issue for it where the
repo's `review_panel.file_deferral_issues` calls for one (#482 — the row is the
durable record and the issue is a work item on a human's tracker, and for the P3/P4
tail those are not the same thing). Either way it lands somewhere with your two
lines attached, which is what makes a deferral a record rather than a shrug. You
open nothing and record nothing, exactly as for an escalation. #223 and #237 are what a good one looks like. A finding you are simply
tired of, or one whose fix you have not worked out, is not a deferral. With
`fixer_may_defer` off, the first two are the whole list and "not now" is not
available to you.

Those three are the whole list for **you**. The orchestrator records what became of
every finding afterwards, from the same vocabulary — `fixed | refuted | deferred |
superseded`. `fixed` is its reading of your work and `superseded` is bookkeeping for
a finding a later one replaced; neither is yours to assign. `refuted` is yours —
it is your false positive, it goes in step 6's table, and it is deliberately the
same word the board records. `deferred` is where an escalation lands, and while
`fixer_may_defer` is on it is **also yours to return**, for a deferral you made.
There is no fifth value to reach for: the vocabulary is a database constraint, not
a convention.

#### 0. Set up the workspace

- **Fix-in-place mode** (current branch): `git branch --show-current`. **Hard
  stop:** if it is `main`, `master`, or the repo's default branch
  (`gh repo view --json defaultBranchRef`), do NOT proceed — return immediately
  saying in-place review may not commit to a shared/default branch. Otherwise
  work directly in the repo. **Snapshot the pre-existing dirty state now:** run
  `git status --porcelain` and record which files were already modified/
  untracked before you touched anything — you'll need this at commit time so you
  don't sweep the user's unrelated work into your fix commit.
- **Worktree mode** (PR number): `git fetch <remote>`, then open a **plain**
  worktree on the PR branch — `git worktree add <tmpdir> <remote>/<branch>` (or
  check out the PR with `gh pr checkout` *inside* the worktree). Do NOT use
  `create-worktree`/`/wt` — this is a review, not an app run, so no Docker/DB/
  nginx. **Mandatory cleanup:** remove the worktree (`git worktree remove
  --force <tmpdir>`) in a `finally`-style step before you return, even on error.
- Record the branch name now — you'll re-verify it before committing.
- If the working tree is unexpectedly dirty in worktree mode, stop and report —
  do not stash or discard.

#### 1. Understand context

Build a mental model before reading line-by-line:
- What problem does this change solve? Read linked issues if any.
- Read each changed file **in full** (not just the hunks) for surrounding code.
- Read the tests that cover the changed code. Read the docs describing it.

#### 2. Deep review

Review the full diff (`git diff <remote>/<base>...HEAD`, or for current-branch
work the diff vs the resolved base, **including uncommitted changes**).

**Core:** correctness (logic bugs, off-by-ones, races, boundaries, null/None);
security (injection, auth bypass, secrets in code, path traversal, SSRF, unsafe
deserialization); error handling (swallowed errors, missing validation, silent
failures, unhelpful messages); concurrency (async pitfalls, missing awaits,
shared mutable state, transaction isolation); performance (N+1, unbounded loops,
missing indexes for new queries, needless allocations).

**Completeness:** every new code path needs a test; every bug fix a regression
test; every visible edge case a test. Review the tests **as tests**, not only for
their absence: a test that would still pass with the bug put back is a passing
assertion that the defect is gone, and it will keep passing when the defect returns.
On PR #90 a deliberate, docstring'd regression test passed because its fixture
happened to list two baselines in the working order, and the defect it was written
for had to be found a round later in code that was already "covered". Docs that describe changed behaviour
(CLAUDE.md, docs/, README, docstrings) get updated. Related code — callers,
siblings, parallel implementations — is governed by **`reviewer_scope`**: under
`repo` it gets made consistent (search the codebase, don't just review the diff);
under `diff`, the default, you read it to judge the change and file work only where
this change BREAKS it or leaves it inconsistent with itself. Read the neighbours
either way — what the setting decides is where the answer lands, not how far you
look. For DB changes: rollback safety, backfill, and
old+new-code-simultaneously safety. **Issue auto-close wiring:** if this closes
an issue, verify `Closes #N` will actually fire — PR-body keywords only work
when the base is the repo **default** branch; if the base is an integration
branch, the keyword must be in a commit message that lands on the default
branch. If nothing carries it, add `Fixes #N` to your fix commit body.

**Craft:** naming, complexity, dead code, redundant conditions, project-style
breaks, DRY.

**Second opinion — Codex (best-effort, never blocks):** if `codex` is on PATH,
run it as an independent reviewer over the same diff:
```
command -v codex >/dev/null && git diff <remote>/<base>...HEAD | \
  codex exec "Review this PR diff for REAL defects only (correctness, security,
  error handling, broken edge cases — not style). file:line + one line each.
  Concise, conservative." 2>/dev/null || true
```
Fold genuine bugs it caught into your list (don't dismiss a real one just
because you missed it); drop only clear false positives. If `codex` is absent or
errors, skip silently.

Rank findings P1 (blocks merge) · P2 (important) · P3 (should fix) · P4 (polish).
The rank is not decoration and not just a column: **`review_panel.fix_severity_floor`
decides which of them this pass fixes, and `low_severity_fix_lines` decides how much
of the low tier it can afford.** At or above the floor they get fixed. Below
it they are reported in step 6 with `Deferred` against them and left alone — that
is the setting's judgement, already made, and re-making it by fixing them anyway is
the growth it exists to stop. At `P4` it is all of them, which is the pre-#165
behaviour.

#### 3. Fix everything

Fix every finding at or above `fix_severity_floor` (`P3` by default, so P1, P2 and
P3; `P4` means all of them). Write the missing tests (edge + error paths) — don't just
note them. Update the stale docs. Propagate renames/patterns to
sibling code. After fixing, re-read the full diff of your fixes and fix any new
issues they introduce.

**The low-severity band is on a budget, and you spend it by COUNTING.** Findings
below `round_trigger_floor` — the ones a panel report marks 💸 — share
`low_severity_fix_lines` churned lines for the whole round (40 by default). Findings
at or above the cut are not on the budget and none of this touches them.

Spend it like this, and do not improvise around it:

1. Do the unbudgeted findings first and commit them. The budget pays for the budgeted
   fixes alone, so they need a clean tree to be measured against.
2. **Measure before you spend.** You cannot know what a fix costs until you have made
   it, so find out rather than guessing: make each budgeted fix on its own, run `git
   diff --numstat` for it, write down insertions + deletions, and put it back
   (`qb-stash push` it, or `git restore` the file — and mind the warning just below
   about discarding your own uncommitted work). You now have a counted cost for each one.
3. **Spend cheapest first, and stop when it runs out.** Re-apply them in ascending
   order of that cost, subtracting each from the budget as you go. Stop at the first
   one that does not fit — in ascending order, nothing after it fits either. If the
   whole list fits, the whole list gets fixed and the budget never binds.
4. Everything the budget did not reach goes into step 6 exactly as a below-floor
   finding does: reported, recorded `deferred` against the issue you open for the
   batch, and **not** fixed. It is not dropped and it is not yours to sneak in.

**Count, never estimate, and never ask yourself whether a fix "risks ballooning".**
That question is a judgement, and it is the judgement the measurement indicts:
across seven PRs 63.7% of new findings were created by the fix pass immediately
before them, and on PR #268's round 2 it was 85%. The budget is the answer to that
question and it has already been given. Your job here is arithmetic — a numstat and
a running total — not a forecast about your own work.

**Commit before you break something on purpose.** Proving a new test bites — by
mutating the code it guards and watching it go red — is worth doing and is the
only way to know a guard is not vacuous. But the revert is `git checkout --
<file>`, which discards **your own uncommitted work** in that file with no
warning and nothing to undo it from. Two fixers hit this on PR #212 within an
hour, both while checking guards they had just written; both were lucky enough to
notice. Commit first, and mutate a file you have not edited where you have the
choice.

**Not `git stash` — `qb-stash push`.** Every worktree of a repo shares one
`refs/stash`, so a stash you push here is listed and poppable from every sibling
worktree, and `stash@{0}` there resolves to whatever the last pusher meant. Two
working trees have already gone that way. `create-worktree` now installs a hook
that refuses the shared stash outright, so a plain `git stash` in a harness
worktree stops with a `REFUSED:` message rather than racing; `qb-stash` is the
same push/pop/list/apply/drop stored per-worktree. It takes no pathspec and does
not save untracked files (`git stash create` supports neither), which is why the
red/green step below uses a patch file instead.

**Once the list is triaged, decide *how* to fix it.** Serially yourself is the
default; for a big, clean list, fan the fixes out to `general-purpose` sub-agents
running in parallel instead. Fan out only when all three hold:

- **Volume** — roughly 6+ findings. Below that, briefing costs more than it saves.
- **Separability** — they partition into groups touching **disjoint files**, with
  no ordering dependency between groups. Every agent shares your one working
  tree, so two of them editing one file will clobber each other: **one file
  belongs to exactly one group**, and a finding that reaches into another group's
  file stays with you.
- **Low nuance** — a group is self-contained and the fix is clear from the
  finding. Keep for yourself anything needing architectural judgment, a
  cross-cutting redesign, or a decision about what the right fix even is.

A mixed list splits: hand off the mechanical clusters, keep the subtle ones.
Launch the group agents in a **single message** (max ~4 concurrent). Each brief
gets: its findings verbatim (file:line + what's wrong), the exact file list it
owns and an instruction to touch nothing outside it, the project conventions and
test layout, and — **do not commit, push, or amend; write tests for your own
fixes; return a summary of what you changed**.

You keep step 4's verification, the full-diff re-read, the commit and the push.
Never delegate those: parallel fixes meet at seams nobody checked, so the whole
combined diff still has to be read and the whole suite still has to go green
under your eye. If a group agent fails or returns short, fix its findings
yourself — the "everything gets fixed" bar does not move because you delegated.

**A group agent flags a premise; it does not escalate.** Step 3a's judgement needs
the whole list — "one premise, or say so plainly" is only evaluable by whoever sees
every group's output — and its product is a question put to a human, which a
sub-agent of a sub-agent has no way to put. So brief each group agent with this
rule: if a finding looks like a premise finding, **leave it unfixed, state the
premise in one sentence, and say so in your summary** — do not patch it and do not
write the write-up. You then decide, across all groups, whether each flagged
finding is an escalation (step 3a) or a defect the group should have fixed, and you
write the one escalation up. This is the single exception to the fallback above: a
finding a group returned as a premise candidate is **not** yours to patch merely
because that group "returned short", since writing the patch it declined to write
is the exact round step 3a exists to prevent. Everything else a group left undone,
you fix.

#### 3a. When a finding says the APPROACH is wrong, escalate it — don't patch it

One finding in a list is sometimes a different kind of thing from the rest: not a
defect in the line it names, but a consequence of a decision the code
deliberately makes. Fixing it *at that line* means adding a special case to keep
the decision standing — and the next round's findings are that special case.

This is the pattern #67 records. On PR #61 every round hit one premise: that an
echoed schema can be told from a real answer by inspecting its content. Round 1
patched it with a placeholder discount and a ranking; round 2's two P2s **were**
that patch — the placeholder set caught `F01` and `P1|P2|P3|P4`, values the prompt
explicitly asks the model to produce, and the ranking let a model's own
illustration outrank the real answer. Two rounds, two fixes, one unexamined
assumption. What ended it deleted the assumption (rank nothing; differing
candidates are ambiguous and go to the retry path that already exists) instead of
patching it a third time.

Everything above tells you to fix everything and never note-and-move-on. That is
right, and it is also exactly why every fixer so far has patched a broken premise
rather than saying so. This is the one permitted exception, and it is narrow.

**It is an escalation if tests 1-3 all hold, or if test 4 fails.** Otherwise it is
a defect, and you fix it:

1. **The defect is downstream of a decision, not of the line.** The named code
   does what it was written to do; the finding is what that intent costs.
2. **You can state the premise in one sentence**, and say what removing it would
   take — "stop inferring an echo from its content; differing candidates are
   ambiguous and go to the retry path already there".
3. **You cannot write a test that fails without your fix and passes with it in
   the general case** — only one pinned to the instance in the finding. A patch
   whose regression test can only be written against a single example moves the
   boundary rather than removing it. (#114 is this same check, reached from the
   other side.)
4. **Is the property your fix asserts decidable in the runtime the assertion runs
   in?** If it is not, escalate — whatever tests 1-3 said.

**Test 4 stands alone, and the other three are why it has to.** Tests 1-3 are a
conjunction that describes one shape: *the code is right, your patch would be a
special case, and you cannot write a general test for it.* Test 4 describes a
different one, and a stronger one. Ask what your fix really needs to know, then ask
whether anything where the assertion runs can actually observe it. If the answer is
no, every possible fix is an **approximation** of that property, the next round's
findings are the gap between your approximation and the property, and the round
count is unbounded by construction — no fix can close it, because no fix can check
it.

Requiring test 4 to hold *alongside* the other three would guarantee it never fired.
A better approximation is generally testable, so test 3 passes precisely when test 4
is failing — which is how this pattern survived four fix passes on one cycle with all
three tests applied honestly and answered correctly every time.

**The tell is that you can describe what you are really checking for, and then
notice the runtime cannot see it.** "The panel actually reviewed the PR" is not
observable from inside the process that ran it; exit codes, payload files and head
SHAs are all proxies for it, each one checkable, none of them the property. If your
next sentence is *"well, a better signal would be…"*, you are choosing the next
proxy, and that is the loop this test exists to stop.

**Check the premise before writing the patch, and check your own last round
hardest.** The strongest case on record is a fixer circling its own fix: on
PR #88 round 1 took a filter out from in front of a newest-run selection and, in
the same commit, put a different one there — under a docstring stating the
invariant it had just broken. So when a finding sits where your own previous round
touched, or when several findings on this list produce **the same failure** in
different files, stop and ask whether one premise explains them all. Cluster by
the failure produced, not by the file: on #88 seven P1/P2s across two files were
one premise, and grouped by file they read as seven unrelated defects.

**Record the premise before you write the patch — that is where the brake is.**
If the brief gave you a **premise register** path, declare the premise there
*before* deciding whether to patch or escalate. It costs nothing — no seats, no
diff, no judge, no vendor call — and it is the only thing that can tell you the
same premise was already patched once:

```bash
premise=$(cat <<'PREMISE'
<the premise, in one sentence>
PREMISE
)
python3 ~/.claude/loops/panel.py --premise "$premise" --pr <n> --round <r> \
    --premise-file <the register path from the brief> \
    --premise-decidable yes|no \
    --premise-for <each finding key the premise explains>
```

`--premise-decidable` is **test 4, answered where it can brake something**. Pass
`no` when the runtime the assertion runs in cannot observe the property the fix
asserts, `yes` when it can. Omit it and the declaration is recorded as *not
answered*: nothing brakes on it, the report says so, and #491's whole mechanism is
off for that pass. It is one word and it is the only part of step 3a that a later
round can act on.

Read the exit code, not the prose. **0** means the fix may be written: carry on and
decide patch-or-escalate on the four tests above. **4** means it may not — **do not
write the patch** — and the report says which brake fired:

- `escalate_on.premise_repeated`: this premise has been patched before in this
  cycle. It is an escalation now whether or not it passes tests 1-3, because a
  premise a fix pass has already been written against once is #67's circling by
  definition, and the second patch is what produces the third round.
- `escalate_on.premise_undecidable`: you answered `no` to test 4. This fires on the
  **first** declaration, not the second, and that is deliberate — an unobservable
  property does not become observable on the next attempt, so waiting for a repeat
  buys a fix pass and a panel to confirm what your own answer already said.

A `no` **sticks to the premise**. Re-declaring it later with `yes`, or with the flag
omitted, does not clear it and does not get you past the brake — the answer is about
the property, not about one pass's opinion of it, and the one agent whose fix is being
refused is not the one who gets to lift the refusal. If you believe the `no` was
wrong, that is the escalation talking to a human, which is where it was going anyway. Report it under `Escalated` with the command's output,
including the `--escalated` keys it prints, and fix everything else in the pass
as usual.

The heredoc is not optional and the reason is the same one the `--ask` block
below gives: a premise about code carries backticks and `$(…)`, and inside a
double-quoted argument bash *executes* them. `--premise-for` takes finding
**keys** (8-64 hex characters, as sent with each finding), not IDs and not
titles — they are what the orchestrator hands the next round as `--escalated`,
and an ID there names no finding at all.

**State the premise, never the proxy.** The repeat brake compares declarations, not
code: "the panel exiting 0 means it reviewed" and "the payload existing means it
reviewed" are one premise wearing two proxies, they share almost no words, and
declared that way they count as two. Declare what the fix *assumes about the
world* — "a local check can prove a review happened" — and the second one is
caught.

**And when you cannot — answer test 4, which does not depend on your wording.**
Declaring at the right altitude is a discipline, and a discipline is not a
mechanism: replacing a proxy produces a genuinely different premise, honestly
declared, so the counter stays at 1 while the cycle circles. That is measured, not
theoretical — one cycle declared four premises, no two of them matched, and three
fix passes went by. `--premise-decidable no` is what brakes that cycle at the
first pass, because the answer is a fact about the runtime rather than a fact about
the sentence you chose.

If the brief gave you no register path, say so in your write-up rather than
inventing one: an undeclared fix pass is **unescalatable** — nothing can brake it
— and #84's rule is to report that rather than pretend the count covered it.

**Put the premise to the seats before you escalate.** Best-effort, about a
minute, and the reason it is here is that this signal cannot be self-reported by
the agent that wrote the fix:

```bash
premise=$(cat <<'PREMISE'
<the premise, in one sentence>
PREMISE
)
timeout 120 python3 ~/.claude/loops/panel.py --ask "$premise" --pr <n> \
    --context "<the file:first-last the premise lives in>"
```

**Never inline the premise into the command line.** A premise about code carries
backticks and `$(…)` — the ones in this file do — and inside a double-quoted
argument bash *executes* them, while a `$VAR` in the text silently expands to
empty and sends the seats a premise you did not write. The quoted heredoc
(`<<'PREMISE'`) expands nothing and survives quotes, backticks and `$` in the
text, which is why the value reaches the flag as typed; `--context` is quoted for
the same reason: unquoted, a space in it word-splits, a glob character is expanded
against the filesystem into zero, one or many arguments, and the `<` and `>` of
the placeholder shown are redirections rather than text. The rule generalises:
any command you build out of a finding's own prose gets the same treatment.

It is not a gate — exit 0 on every verdict, no diff, no judge, no round (see
`harness/loops/README.md`, *The premise check (`--ask`)*, for the verdicts and the
quorum). A `fails` is the evidence a human should have in front of them.
`unresolved` or `unchallenged` is **not** a refutation of the escalation and does
not turn it back into a patch: say which verdict you got. `timeout` is there
because the ask spawns the real reviewer CLIs: a slow seat would otherwise outlive
the 10-minute foreground Bash cap and take the whole fix pass down with it. A run
you killed reports as `not run`, exactly like a missing script — and skip silently
if the script isn't there.

**What escalating means:**

- **Write no patch for it, and leave no half-change behind.** The absence of the
  patch is the point; a partly-applied redesign is worse than either outcome.
- **Partition the rest, then fix one half of it.** An escalation is not a
  stop-work — but "fix everything else" is not what it means either, and reading
  it that way is what #555 is filed about. Split the remaining findings in two:

  - **downstream of the premise** — anything whose fix only makes sense if the
    premise holds, including tests that assert the behaviour it questions and
    docstrings that describe it. **Write no patch for these.** If the premise
    resolves the other way they describe something that no longer exists, and the
    pass that wrote them is spend against an open question.
  - **independent** — everything else. These get fixed, tested, verified,
    committed and pushed exactly as step 3 says.

  **Name the downstream half when you declare**, with one `--premise-for <key>`
  per finding. That flag is the partition, not bookkeeping: it is what keeps the
  next round from counting those findings as work a fix pass could have cleared,
  and `--premise` prints the two halves back to you before you patch anything.

  This is measured, not cautious. On lexray#1697 round 1 the fixer read this
  bullet in its previous wording, fixed five findings, and **four of them were
  about the behaviour of the very flag the escalation questioned**. The pass was
  reverted the next day and everything it wrote had nothing left to attach to.
  Exactly one finding was independent — and the line budget dropped that one, so
  on *that* round the partition left nothing to write and the right output was to
  escalate and stop.

  **Read that as the arithmetic of one round, not as the rule.** "Escalate and
  stop" is what the partition happened to come to when four of five findings were
  downstream; it is not what an escalation means. The rule is the two bullets
  above — the downstream half gets no patch, the independent half gets fixed —
  and a round with an independent half still writes it, tests it and pushes it.
  Stopping outright is correct only when the independent half is empty, and that
  is a thing you determine by partitioning, never by assuming.
- **Open nothing yourself, and do not file the board row by hand.** The premise
  ISSUE is still the orchestrator's, after it has relayed your report — you were
  told to decide nothing and write no patch, and filing the premise yourself is
  the first move of the redesign you are declining to make.

  The board ROW is nobody's to file any more: when the brake refuses your fix,
  `--premise` raises it for you (#555) and prints what it did on the `board` line
  of its report. Re-raising an identical open question is a no-op at the board, so
  a second declaration of the same premise re-uses the row rather than opening a
  second one — which is why you should not "help" by opening one as well. If that
  line says it was NOT recorded, say so in your write-up; it is a fact about the
  board, and the escalation stands either way.

  Your durable output is still the write-up in step 6 and the same finding named
  as escalated in the step-5 commit body; those are what get lifted.
- **Report it in step 6 under `Escalated`** — the premise in one sentence, the
  findings it explains, what removing it would cost, the patch you did not write,
  and the `--ask` verdict if you ran one. An escalation nobody reads is a
  note-and-move-on with extra steps.
- **Never redesign on your own authority.** The output is "stop and ask", never a
  rewrite. Redesigns are expensive, and a heuristic that triggers one cheaply is
  worse than the round cap it improves on — #67's own honest limit, from n=2.
- **One premise, or say so plainly.** If you are escalating several, or the
  escalation covers most of the list, that is a redesign of the change under
  review and it is a human's call. Report it in those words and stop, rather than
  escalating item by item.

#### 4. Verify

Read CI config + Makefile to find what the project runs, then:
1. **Build** (if applicable).
2. **Test** — full suite; iterate until green; fix the wrong side (code or
   test), never skip.
3. **Lint + format** — fix all; don't disable rules.
4. **Type check** (if used).
5. **Codegen sync** — if CI has `git diff --exit-code` checks, regenerate.

**Red/green every regression test you wrote.** A regression test written alongside
its fix has never once run against the broken code, so nothing so far has shown it
would have caught anything. Before you commit, make each one fail:

```
git add -N <every file your fix changed OR ADDED>
git diff HEAD -- <those same files> > .redgreen.patch
test -s .redgreen.patch || { echo "STOP: captured nothing"; exit 1; }
git checkout HEAD -- <the files that existed before>   # drop the edits
rm <the files your fix ADDED>                          # and the additions
pytest <the new tests>                                 # MUST fail, on the assertion
git apply .redgreen.patch && rm .redgreen.patch        # put the fix back
pytest <the new tests>                                 # green again
```

**Do NOT use `git stash` for this.** Every worktree of a repo shares one
`refs/stash` — it lives in the common git dir, not the per-worktree one — so a stash
you push is listed and poppable from every other worktree in the fleet, and
`stash@{0}` means something different depending on who pushed last. This is not
theoretical: the PR that added this instruction lost its own working tree to it, when
a concurrent agent in a sibling worktree popped the red/green stash into its own
checkout. A patch file is per-worktree by construction and shares nothing.

**`test -s` is the guard, and it has to HALT.** If the capture comes out empty —
mistyped paths, or a fix already committed — the "red" run executes with the fix still
in place, comes out **green**, and reads exactly like the step passing. So the guard is
`|| { echo …; exit 1; }` and not `|| echo …`: a bare `echo` prints a warning, exits 0,
and carries straight on into the run it was meant to prevent, which is the failure mode
wearing the costume of a check. Every version
of this check that trusted something other than "did we actually capture bytes" failed
on that state: a `git stash list | head -1` label match is answered yes by a leftover
stash from an earlier run.

**`git add -N` is what makes a file the fix ADDED show up in the patch.** Without
intent-to-add, `git diff` ignores untracked files, so a fix spanning an edit and a new
module is half-captured and the red run imports the new half. Those same added files
are removed with `rm` rather than `git checkout HEAD --`, which cannot restore a path
that is absent from HEAD. Your new *test* file is not in this list and stays where it
is — which is the point.

**If your fix is already committed**, there is nothing uncommitted to capture: get the
pre-fix state with `git checkout <remote>/<base> -- <the files your fix changed>`, run
the tests, then `git checkout HEAD -- <the same files>` to put your fix back.

Read *how* it failed. A test that errors on an import, a missing fixture or a
`TypeError` has not demonstrated anything — it has to fail on the assertion that
names the defect. Stash the **fix**, not the test: stashing both proves only that a
file you deleted no longer runs.

**Exempt only when there is genuinely nothing to fail against.** A regression test
for a path the fix *created* — a new function, a new flag, a new file — has no
pre-fix behaviour to run against. Name those in the summary as `red/green: N-A (new
code path)`.

**A prompt string, a config default or a doc that already existed is NOT exempt.**
That text is the artefact, a test can assert on it, and such a test fails against the
pre-fix text exactly like any other — this instruction arrived in a PR that changed a
prompt string and a set of markdown briefs, and nine of its eleven tests went red
against the previous text. Nor is a fix to code that already existed: if that test
will not go red, it is testing something other than the bug, and the test is the
thing to fix. The exemption exists so the legitimate case does not have to be lied
about; an exemption wide enough to cover the awkward cases is how the whole step
stops happening.

**DB-backed tests:** if the diff touches DB-facing code (models/schema,
migrations, ORM queries, session/transaction handling, DB-backed routes/tasks),
the fast suite often **excludes** DB tests and proves nothing about them. Find
and run the DB-backed target against a live local DB (migrate the test DB to
head first). If you can't, say so explicitly and flag those paths **unverified**
— don't imply the fast suite covered them.

Install missing tools if feasible; only skip with a note if not.

#### 5. Commit and push

**Branch checkpoint:** re-run `git branch --show-current` and confirm it matches
the branch from step 0. Wrong branch → STOP and report; do not commit.
- **Stage by explicit path only — NEVER `git add -A`, `git add .`, or
  `git commit -a`.** Stage just the files you created or edited while fixing,
  plus the files that constitute the work under review. Cross-check against the
  step-0 snapshot: any file that was **already dirty before you started and is
  NOT part of the work under review** must be left untouched and uncommitted —
  do not sweep the user's unrelated in-progress changes into your commit. List
  any such files you left alone in the summary.
- Separate commit (don't squash into the original).
- Message: `fix: resolve review findings for PR #<n>` (or, for current-branch
  work with no PR yet, `fix: resolve self-review findings`).
- Body: every finding with severity + resolution. An escalated finding (step 3a)
  goes in that list as escalated, with its premise — never among the fixes, and
  never left out: the commit is the only record that reaches a reader who never
  saw this run. If the auto-close check found no commit carries the closing
  keyword, end with `Fixes #N`.
- Regular push (not force). In worktree mode, push `HEAD:<branch>`.

#### 6. Return a summary (do NOT post a PR comment unless the orchestrator asked)

Return this table as your final message:
```
## Review Summary — PR #<n> (<repo>)
Files reviewed: N | Findings: N | Fixed: N | Deferred: N | Escalated: N | Refuted: N

| # | Severity | Finding | Resolution |
|---|----------|---------|------------|
| 1 | P1 | ... | Fixed: ... |
| 2 | P2 | ... | Escalated — see the block below |
| 3 | P3 | ... | Deferred — see the block below |
| 4 | P3 | ... | Refuted: <the evidence it was not a defect> |

Deferred — real, and not this change's job
- Finding: <the number above>
  ID: <the panel's finding ID for it, verbatim — e.g. `236-F01`, exactly as the
       report you were briefed from prints it in square brackets. The orchestrator
       maps ID to key from the round's JSON payload and records the outcome against
       the key; a deferral nobody can identify is a deferral nothing tracks. Say
       `none` for a finding you discovered yourself, which has no panel record. Do
       NOT try to supply the digest key itself — you were never given one, and the
       report leaves it out on purpose, because a literal key on a PR comment reads
       as an API key to every secret scanner>
  Why it is real: <one line — this is not a refutation>
  Why not here: <one line — what this change is for, and why the defect sits
       outside it>
  Goes to: the orchestrator records it — a board row always, an issue where
       `file_deferral_issues` calls for one. You open nothing

Escalated — the approach, not the code
- Premise: <one sentence>
  ID: <the panel's finding ID for it, verbatim — e.g. `236-F01`. The orchestrator
       maps it to the key and passes THAT to `panel.py --escalated`, and a premise
       nobody can identify stays in the loop. `none` if you found it yourself>
  Explains: <the finding numbers above it accounts for>
  Removing it costs: <what would have to change, and where>
  Patch not written: <the special case you declined to add>
  Premise check: fails / holds / unresolved / unchallenged / not run

Tests added: ...
Docs updated: ... (or "none needed")
Verification — Tests: pass (N passed, M added) | Red/green: N of M went red
  (rest N-A: new code path) | DB-backed: pass / N-A / unverified | Lint: clean |
  Format: clean | Types: clean / N-A
Commit: <sha> <subject>
```

`Fixed + Deferred + Escalated + Refuted = Findings`, always. That sum is the one
cheap check a reader — or `epic.md`'s relay scan — can apply to catch a finding that
fell off the list, and it is why the counts replaced the old `All fixed: Yes`, which
had no way to say anything but yes and so had to be read as covering findings nobody
fixed. `Deferred` is in the sum for exactly that reason: a permitted outcome missing
from the invariant is a finding that can leave the list without the arithmetic
noticing, which is the note-and-move-on this brief opens by forbidding, arriving
through the permission granted to replace it. `Refuted` and `Deferred` are the words
the board records, not other names for the same things: the summary's labels and the
board's outcomes are deliberately the same tokens.

**Escalated nothing?** Replace the whole block with the single line
`Escalated: none`. **Deferred nothing?** The same, `Deferred: none`. One spelling of
each empty case, and they are written out rather than omitted: a missing section
reads as forgotten, and the one run where it matters is the run where a reader has
to be sure. With `fixer_may_defer` off, `Deferred: none` is the only honest answer
and `Deferred: 0` is the count.

---

## 2b. Record what happened to each finding (when the findings came from a panel)

If the findings you handed the fixer came from a recorded panel round — i.e. the
board has them, with a `key` each — say what became of them, per the **4b**
section of `panel-review-pr.md`. The `Resolution` column of the summary table
above is exactly this information in prose: `fixed`, or `refuted` with the reason
it was not a defect, or `deferred` with where it went.

**The fixer reports finding IDs; you supply the keys.** The report it was briefed
from prints `[236-F01]` and never the 16-character key — deliberately, since a
literal key on a PR comment reads as an API key to every secret scanner
(`panel-review-pr.md` §4b) — so the fixer's `Deferred` and `Escalated` blocks name
IDs. Map each one to its key out of the round's JSON payload before you record
anything or pass `--escalated`; §4b has the `jq` one-liner that prints both.

**Three roads arrive at `deferred` and all three are the same row.** An escalation is
a deferral you infer (the fixer wrote no patch because the approach is in dispute); a
**fixer deferral** is one the fixer states outright, under
`review_panel.fixer_may_defer` — the defect is real and outside what this change is
for, and its two justifying lines are in the summary's `Deferred` block; and a finding
the panel reported below the fix floor is the third, described in the next paragraph.
Your job is the same on all three and it is the half the fixer is forbidden to do:
**record the finding `deferred`, and open an issue for it only if
`review_panel.file_deferral_issues` says so.** #223 and #237
are what that record looks like — a human applying exactly this judgement by hand,
at the round cap, which is the thing the setting exists to let a fixer reach on
round 1 instead.

**The row is the record; the issue is a work item, and they are not the same thing
(#482).** `review_panel.file_deferral_issues` is a severity gate — at or above it a
deferral gets a GitHub issue named in `deferred_to` as it always did; below it the
deferral is a board row with **no `deferred_to`** and **a one-line `note`** saying
what the defect is and why it was not fixed. `deferred_to` is nullable, the API takes
a `deferred` outcome without one, and `/panel` renders such a row with no target
rather than as broken. The note is what makes that row worth having: it is the thing
somebody reads later, and `GET /review/findings?repo=<owner/name>&pr=<n>` is where
they read it. A row with neither an issue nor a note is the markdown list this
replaced, wearing a database.

Measured on this repo on 2026-08-26, roughly twenty open issues were panel
deferred-finding exhaust and nothing else. The default is `P2`; `always` restores the
pre-#482 "an issue for every deferral" and `never` files none. **An escalation is
exempt at every setting** — its issue asks a question rather than filing a task, and
it is what carries that question past the end of the session. And if the board write
fails, file the issue whatever the gate says and say so in the relay: below the gate
the row is the only record, so losing both loses the finding.

A finding the panel reported **below the round's `fix_severity_floor`** is the one
that needs no judgement from anybody: the floor already decided. Those arrive marked 🔽 under *Reported, not this round's work*, they were
never in the fixer's brief, and they are recorded `deferred` — as board rows alone at
the default gate, which is precisely the tier it exists for. Where the gate does call
for an issue, one issue for the batch is fine and is usually right, since filing nine
issues for nine P3s is the overflow this floor exists to stop (#165).

The one that matters is `refuted`. A judge-confirmed finding that turns out to be
wrong is recorded nowhere today, so the leaderboard rewards a reviewer for being
confident rather than for being right — and the refutation is already written in
that table. `qb record-outcome` is the two lines that keep it.

An **escalated** finding (the brief's step 3a) is recorded as `deferred`. It is not
`refuted`: the defect the finding names is real, and only the *fix* is in dispute.
It is not `fixed` either, and there is no fifth value to reach for —
`fixed | refuted | deferred | superseded` is constrained in the database as well
as at ingest (`app/api/reviews.py`, `app/models/review.py`, the
`ck_review_finding_outcomes_vocabulary` CHECK), so an invented `escalated` costs
the row and records nothing.

**`deferred` is not a claim that the question is settled**, and nothing reads it as
one. The row says what the *fixer* did with the finding; no loop's **To fix** list
is computed from it — `round_stop` takes its outstanding findings from the round's
own payload (`harness/loops/panel_rounds.py`) — so recording it neither closes the
escalation nor removes it from the next round's arithmetic. The relay closes it,
when a human answers. And a `deferred` row that later moves is designed for:
`revisions` and `prior_outcome` exist because "a deferred finding is later fixed" is
an expected lifecycle (`app/models/review.py`), which is exactly what the human's
answer will make of this row.

**You open the premise issue, not the fixer, and only after you have relayed.** This
one is filed at every setting of `file_deferral_issues`, `never` included: an
escalation's issue is not a work item on somebody's backlog, it is the question being
put to a human, and a `deferred` row with nowhere to go and nothing carrying the
question is the markdown list this replaced. The fixer is a sub-agent told to
decide nothing and write no patch, so the filing is yours (§3 below). Relay the
escalation first, then open an issue that **asks**: the premise, the findings it
explains, what removing it would cost, the patch that was not written, and the
`--ask` verdict — lifted from the fixer's write-up, which already has all five.
Name that issue in `deferred_to`. An issue that puts the question is not an answer
to it: what step 3a forbids is *choosing* the redesign, and an issue that proposes
none has chosen nothing.

Findings you discovered yourself, with no board record behind them, have no key
and nothing to record: this is for panel findings only.

## 3. Relay the result

Show the user the sub-agent's summary table verbatim, then state plainly: the
branch it pushed to, whether all checks passed, and anything it flagged as
**unverified**. If the sub-agent failed or stopped early, report exactly where
and why — don't paper over it.

**A deferral is relayed, not silently absorbed.** If the fixer returned anything in
its `Deferred` block, or the panel reported anything below the fix floor, say so
plainly with the count and the one-line reason for each: those are defects this pass
knowingly did not fix, and a relay that omits them tells the user a PR is finished
when the record says otherwise. Then follow §2b in order — record the row, and open
an issue only where `file_deferral_issues` calls for one. Below the gate the relay is
where a human hears about it at all, so the count and the reasons are not optional
there; that is the half of the deal that keeps a board row from being a place things
go to be forgotten.

**An escalation is the headline, not a footnote.** If the sub-agent escalated
anything (the brief's step 3a), lead with it: the premise, what it explains,
what removing it would cost, and that no patch was written for it. That is a
question being put to the user, and until they answer it the review is not
finished — so do not answer it yourself by launching another fixer at the same
finding, which is precisely the round that produces the next round's findings.
For panel findings, §2b is the follow-through in order: relay, then open the
issue that asks the premise, then record the finding `deferred` with that
issue in `deferred_to`.

## 4. Merging (only if the user asks)

`gh pr merge --merge --delete-branch` — preserve individual commits; never
squash; delete the remote branch after.

**Read `fix-and-land.md`'s *The hazards* section before you act on anything that
looks like a failure here.** It is one copy, not a second one: `--delete-branch`
failing its cleanup from a worktree while the merge lands, a body whose "does not
close #N" closes #N, impossible-looking test failures that are a concurrent pytest,
and the two refusals that meet a lander trying to undo a change. Everything on that
page applies to this path too — this command merges from the same worktrees, on the
same box, against the same GitHub.

Then hand back the claim on the issue the PR closes: `qb-release issue <n>` (#337).
`create-worktree` took a `kind=work` claim on it at checkout — held by the machine,
no session, 8h TTL — and merging a PR does not touch it; on 2026-08-22 four issues
were still claimed hours after their PRs had merged. Exit 0 also means "already
handed back", so it is safe to run twice, and the worktree teardown does it again.
