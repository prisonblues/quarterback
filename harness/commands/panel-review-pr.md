# Panel Review and Fix PR

@description Like /review-pr, but the findings come from the multi-reviewer PANEL (Claude + Codex + Antigravity + master judge, plus the SonarCloud hard gate where the repo enables that seat) instead of one sub-agent reviewer. Ensures a PR exists, runs ~/.claude/loops/panel.py (which comments the summary on the PR), then a sub-agent fixes every master-confirmed finding boil-the-ocean style and pushes — and the panel then RE-REVIEWS that fix commit, which is the round nobody used to run. Give it several PR numbers and each one is reviewed+fixed by its own sub-agent, in parallel. Panel members default to the repo's .harness-rules.sample and can be named explicitly. It ends by running the pre-land gate and offering to land only on READY; merging stays opt-in.
@arguments $ARGS: [pr ...] [repo] [--reviewers a,b] [--rounds N|--loop]  (defaults: the current branch's open PR in the cwd's repo, the repo's configured reviewers, and 6 rounds)

You are the **ORCHESTRATOR**. This is `/review-pr` with the panel as the
finding engine: the panel finds, an autonomous sub-agent fixes everything to the
exact same bar. The only difference from `/review-pr` is *who reviews* — so
reuse its fixer brief rather than re-inventing it.

**The cycle is panel → fix → panel, not panel → fix.** One round leaves the
fixer's own commit read by nobody: the panel saw the diff as it was BEFORE the
fix, and structural fixes beget new interactions that did not exist until the fix
was written — a mirror added in one file creating dual-keyed nodes that an early
`return` in another leaves half-stale is not a defect any earlier round could
have found. Round 2 is not optional; the default cap is **6** and `--rounds N` /
`--loop` moves it.

## 1. Ensure a PR exists (the panel diffs via `gh pr diff`, so a PR is required)

- Parse `$ARGS` in two passes. **First consume every `--flag` together with its
  value** (`--rounds 3`, `--reviewers codex,antigravity`) and remove both from the
  string — except **`--loop`, which takes no value and is consumed alone**
  (`--loop 12` is `--loop` and PR #12, not a flag whose value is `12`).
  **Then** read what is left: every integer is a PR number — `12`, `#12`,
  `12,14` and `12 14 19` all parse — and an optional remaining non-numeric word is
  the repo. Order matters, and doing it the other way round is wrong for every
  flag, not just one: `--rounds 3` reviews PR #3, and `--reviewers codex` eats
  `codex` as the repo name.
- **Rounds:** `--rounds N` caps the panel/fix cycles. The default is
  `review_panel.max_rounds`, which is **6**; `--loop` is `--rounds 10`. `--rounds 1`
  restores the old fix-and-don't-look behaviour and should be used only when someone
  explicitly asks for it — say in the relay that the fix went unreviewed.

  **6 is a backstop against running forever, not a convergence mechanism**, and the
  difference matters to how you read a stop. At 2 the cap ended most cycles, so "stopped
  at the cap" was the ordinary ending and `confident: false` was the ordinary verdict. At
  6 a cycle that is converging converges well before the cap, and what actually ends the
  ones that are not is **`escalate_on.fix_injection`** — more than half a round's new
  outstanding findings attributed to the fix pass before them, which is the loop being
  fed by its own output (§5). **A stop on that rung is a stop and never convergence.** It
  takes a veto line and `confident: false`, it means the last fix pass was generating the
  work rather than clearing it, and relaying it as "the panel finished" is a claim the
  round explicitly refused to make. Reaching the cap at 6 says the same thing more
  expensively.
- **PR number(s) given** → use them, in the order given.
- **No PR number** → resolve the current branch's open PR
  (`gh pr view --json number,baseRefName,headRefName`).
- **No PR yet** → push the branch and open one
  (`git push -u <remote> HEAD` then `gh pr create --fill`), then use it. (This
  is the agreed behaviour — auto-create rather than stop.)
- **The repo has to be enrolled, and enrolment is one file.** The panel is
  read-only and resolves the repo from the checkout you are in, but a repo with
  **no** rules file at all is REFUSED a review rather than reviewed on built-in
  defaults: those defaults are a two-seat panel on models nobody chose, judged by a
  judge nobody chose, and the findings then brief a fixer that edits the repo. The
  refusal prints, writes a payload with `reviewed: false` and a `skip_reason`, exits
  0, and records nothing on the board. Do not work around it — commit a
  `.harness-rules.sample` naming the seats, models and judge this repo wants (copy
  the quarterback repo's and cut it down), then re-run. Every OTHER unconfigured key
  still falls back to a safe default, so the file can be short.

Capture: `nameWithOwner`, the PR number(s), each PR's base and head branch, and
the absolute repo path of the working checkout.

**Pick the mode:** one PR → carry on to §3 and run it in this conversation.
**Two or more → §2, and do not run the panel yourself for any of them.**

## 2. Two or more PRs — fan out, one sub-agent per PR

Each PR gets its **own** `general-purpose` sub-agent that runs the whole
per-PR pipeline itself — panel, fix, re-review — so the PRs proceed in parallel and
none of their diffs land in your context. **Launch them in a single message**
(multiple Agent calls in one block) or they will not run concurrently. Launch
at most **4 at a time**; queue the rest and launch the next batch as they
return — each panel already runs several reviewer CLIs concurrently, and a
dozen at once just makes every one of them slower.

Each sub-agent's brief is §3 + §4 + §5 of this file for its own PR — including
the re-review rounds, which are not the orchestrator's job to run afterwards —
with these parallel-mode overrides:

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
- **The premise register is the per-PR agent's own, and it declares before its
  first edit — including the pass after round 1.** §5's declaration rule assumes an
  orchestrator that briefs a separate fixer, so it starts at round 2 and leaves the
  round-1 pass to the path in §4's brief. Collapse the two roles and there is no
  brief and no path: the whole point of the brake is that exit 4 means *do not write
  the patch*, and a premise declared once the pass is written and pushed is an
  annotation. So the agent makes its own `mktemp -d` register at the start, declares
  after reading each round's `round_stop` and **before** editing anything, and passes
  the same path to every round's `--premise-file`. `--premise` records the commit the
  tree was on, and a later round names in `config_notes` any premise stamped with a
  head that arrived after the round it answers (#560) — a fix pass already committed
  and pushed when its premise was stated. **It catches nothing else, deliberately:** a
  patch written into the working tree and not committed moves no `HEAD`, and no
  reading taken in the fixer's own environment separates that from an honest
  declaration (#622). Declaring before the first edit is the mechanism there, not the
  check.
- **Always worktree mode, never fix-in-place** — concurrent agents cannot share
  your checkout. Each opens its own throwaway worktree at the PR's head
  (`git worktree add <tmpdir> <remote>/<branch>`, detached — this works even
  when a sibling agent, or you, has that branch checked out elsewhere) and
  removes it in a `finally` step. It pushes with `HEAD:<branch>`.
- **Pass the repo explicitly:** `python3 ~/.claude/loops/panel.py --repo <abs
  repo path> --pr <n> --post …` — a sub-agent's cwd is not guaranteed to be your
  checkout, and `--repo` defaulting to cwd would silently review the wrong repo.
  It carries the same `--round`/`--baseline`/`--json-file` arguments; each agent
  makes its own `mktemp -d` directory for them (§3), so concurrent agents cannot
  read or clobber each other's baseline.
- A `--reviewers` list from `$ARGS` applies to **every** PR in the run.
- Each sub-agent returns its own panel summary (findings, the SonarCloud gate where
  that seat ran, skipped reviewers, coverage declared) **and** the §4 fixer summary table for
  its PR **and** its §5 round log: rounds run, what each found that the last had
  not, what stopped it, and whether that stop was earned.
- **One PR failing does not stop the others.** A sub-agent that cannot resolve
  its PR, or whose panel or verification fails, reports that and returns; the
  remaining agents run to completion regardless.
- **`--new-cycle` is not a sub-agent's to decide (§3).** A PR whose board record holds a
  terminal verdict is refused, and starting a fresh cycle over it is a judgement about
  whether the thing that ended the last one has been answered — which the agent holding
  one PR's context is not the reader for. Brief each sub-agent to **report the refusal
  and return**, not to pass the flag; you relay it and the user chooses. Pass it only on
  a re-launch, and only for the PRs it was chosen for.

Then relay per §6: one table per PR, plus a one-line-per-PR roll-up (PR ·
findings · all fixed? · rounds and what stopped them · SonarCloud gate, or `n/a` where
the seat did not run · pushed?) so the batch is readable at a glance. A PR whose cycle stopped
unearned is marked as such in the roll-up, not only in its own table. Report
each failed or partial PR explicitly — never let a roll-up imply a PR was
handled when its agent stopped early.

## 3. Run the panel (round 1)

Make one private directory for this PR's round payloads first, and keep using it
for every round:

```
mktemp -d                                 # 0700, and a name nobody can predict
```

**Read the path it printed and write it out literally from here on** — every Bash
call is a fresh shell, so a `rundir=$(mktemp -d)` set in one command is gone by
the next one and `"$rundir/r1.json"` expands to `/r1.json` in §5: the baseline
fails to load and the round-2 payload lands at the filesystem root. There is no
shell variable to carry; substitute the actual directory into each command:

```
qb-stage R1                                      # what the statusline shows
python3 ~/.claude/loops/panel.py --pr <pr> --post --round 1 --max-rounds <N> \
    --json-file /tmp/tmp.AbC123/r1.json          # ← the path mktemp -d printed
```

(`qb-stage` is best-effort and takes no time; stamp it before the panel starts,
not after, because a round is when the bar most needs to say what is happening —
a top-tier reviewer can think for 20+ minutes and an unstamped bar reads as an
idle session for all of it.)

(`--post` comments the panel summary on the PR by default — that is the review
record the fixer then resolves. Drop `--post` only if the user explicitly asked
not to comment. `--json-file` is what makes round 2 able to say which findings
are NEW rather than the same ones again; without it every reappearing finding
reads as fresh damage. `<N>` is the round cap from `$ARGS`, and passing it is
also what tells the panel this run is part of a cycle rather than a one-off
read.)

**If the panel exits non-zero because it could not write that file, the round did
not happen for cycle purposes.** The payload is round 2's baseline; without it the
next round calls every repeated finding new. Fix the path and start the cycle
again at round 1 rather than carrying on — never re-run this round on its own.
Every non-error exit writes it, including a PR skipped by title pattern (its
payload is marked `reviewed: false`), so exit 0 always means the file is there.

**A round the panel REFUSED did not happen either, and it exits 0.** Read
`preflight.verdict` from the payload before doing anything with the findings:

- `refuse` — no seat was dispatched (`reviewed: false`, with `skip_reason`). **Stop
  the cycle here.** Do NOT go to §4: a fix pass briefed with zero findings from a
  round nobody ran is a fixer told the PR is clean. Relay the panel's reason and
  the remedies it named — for an oversized diff: split the PR, raise the cap for a
  seat that can take it, or re-run with `--force`; for a branch that cannot merge
  (`require_mergeable`, #271): rebase and re-run, turn the dial off for this repo,
  or re-run with `--force` — and let the user choose. Never add `--force` on your
  own initiative; the refusal is the panel's decision about a diff it measured, and
  overriding it unasked is exactly the failure the check was built to stop.
  **Relay `ci_status` and `ci_failing` with it.** A refusal still reads the CI gate
  — that is size-independent and cost the round one API call, and it exists in the
  payload because a refusal that lost the build status left this step telling the
  user to stop with nothing said about a red suite. If `ci_status` is `FAIL`, say
  so and name the checks: the PR is broken by something the project already tests,
  and nobody had to read the diff to know it. `PASS`, `PENDING`, `none` and
  `unknown` are four different statements and none of them is "reviewed" — say
  which one it was, never that CI was fine.
- `manifest` — the change is move-shaped and the seats were asked what *moved*, not
  whether the code is correct. The cycle runs normally, but the findings are answers
  about the move and **the moved code was not read by anybody**. Say so in the relay
  (§6), and keep it out of "reviewed and clean": its correctness is carried over
  from when it landed on the base branch.

**One refusal is not about the diff at all, and its remedy is a different flag (#617).**
Before anything else, the panel reads the board for a **terminal verdict already recorded
on this PR**. If the last round it can find stopped the cycle, this run is refused — the
verdict is printed, `preflight.prior_cycle` carries the record, and it exits 0. Read
`prior_cycle` as well as `verdict`, because the remedies in the `refuse` bullet above are
every one of them wrong here: rebasing does not undo a stop, no size ceiling was
consulted, and **`--force` cannot move this gate** — deliberately, since `--force` says
*this diff is worth reading anyway*, which is not an answer to *an earlier round already
ended this cycle*. The refusal notice says all of that itself; relay it rather than
working around it.

**A refusal here is the board answering, not a broken tool**, and it is what
lexray#1780's rounds 3-5 would have hit: three standalone workflows that ran on after
round 2 stopped the cycle on `fix_injection`, each of them round 1 of its own cycle with
none of the convergence guards connected. **`--new-cycle`** is the opt-in that starts a
genuinely new cycle — it mints a fresh cycle rather than continuing one that was told to
stop, and puts a banner at the top of the report naming the round that stopped, its
reason, how long ago, whether that stop was convergence and whether the branch has moved
since.

**It is ROUND 1, and it takes neither `--round` (2 or more) nor `--baseline` — both are
REFUSED.** The flag opens the gate; it does not reset anything, and there is nothing for
it to reset that the caller does not control. A run passing the old cycle's round counter
and baseline alongside it is a continuation whatever the banner says, and every guard —
`fix_injection`, `premise_repeated`, `max_fix_growth`, the trend table — would go on being
measured against the cycle the banner had just said nothing was measured against.
`--max-rounds` is fine and is how a new cycle declares its cap; the **rounds after this
one continue the new cycle with `--round`/`--baseline` and WITHOUT the flag**, because a
live cycle's own rounds make the earlier stop non-terminal and the gate no longer fires.

Three legitimate runs meet this refusal, and all three take `--new-cycle`:

- **a genuinely new cycle** on a PR whose earlier cycle stopped — the intended case, and
  the flag belongs there **once the thing that ended the last cycle has been answered**.
  A terminal verdict names something a fix pass cannot clear; passing the flag without
  answering it just buys the same stop again, more expensively;
- **§5's verification pass on the final capped fix (#629)** — that pass follows a stop by
  construction, so it is refused without the flag. See §5;
- **a re-review after the branch has moved on** from the head the stop was recorded at.
  The banner says the branch moved, which is the fact that makes the re-read worth doing.

**Not** a fixed `/tmp/panel-<pr>-r<n>.json`. Two reasons, both real on a shared
host: the panel writes that path with `Path.write_text`, which follows symlinks,
so a pre-planted `/tmp/panel-34-r1.json → ~/.ssh/authorized_keys` is a write
under your own identity — and the payload (diff excerpts, every finding's text)
is world-readable while it sits there. Separately the name is scoped by PR
number alone, so two repos reviewing their own PR #34 overwrite each other's
baseline and round 2 silently diffs against the wrong PR.

**Run it in the background** (`run_in_background`), not as a foreground Bash
call: a reviewer on a top-tier model at high effort can think for 20+ minutes,
and the foreground Bash timeout caps at 10 — which kills the whole panel, not
just the slow seat. Poll the background task instead.

**Panel members** default to the repo's `.harness-rules.sample`; pass no `--reviewers`
unless the user named who should review ("just codex", "codex and antigravity"), then
add `--reviewers <comma-list>` from `claude`, `codex`, `antigravity`, `pi`, `grok`,
`sonarqube`. It
replaces the configured set rather than filtering it, so a named reviewer runs
even where the rules disable it. Fewer reviewers means thinner coverage feeding
the fixer — surface that in §6 rather than letting a one-vendor review read
like a full panel.

From its output collect:
- **Panel dials** — the line the report prints under that name, naming `review_panel`.
  It says which severity floor the fixer is being briefed to, what buys another round,
  whether the fixer may defer, the low-severity line budget, and the fix-growth ceiling
  — which since #492 has TWO halves, a multiple and an absolute char count, and stops
  the cycle on whichever is crossed first. Read it BEFORE §4: the brief you build
  depends on it, and it is the only place the round's policy is written down where
  you can see it (#165).
- **To fix** — the master-confirmed findings the fix round is asked to clear (at or
  above the round's `fix_severity_floor`, and — where the repo has written
  `threshold_by_severity` — corroborated by at least as many members as its band asks
  for; the heading says which of the two rules the list was filtered by). The ones marked 💸
  are below `round_trigger_floor` and share the round's `low_severity_fix_lines`
  budget rather than being unconditional (#297); the note under the heading states
  the number and the rule.
- **Reported, not this round's work** — the master-confirmed findings BELOW that
  floor, marked 🔽. Present only where the floor left something under it. These are
  recorded and relayed and **never pasted into the fixer's brief**; §4b says what
  becomes of them.
- **Reported, not this round's work — under the corroboration threshold** — a SECOND
  section under the same words, marked 👥, and it is not the same list (#78). These
  findings are above every floor; what they are short of is agreement, having been
  raised by fewer members than `review_panel.threshold_by_severity` asks for at their
  band. Present only where a repo has written that key, which no repo does by default.
  **Never pasted into the fixer's brief either**, and for a sharper reason than the
  floor's: the round has said the evidence for these is one seat, and a fix pass that
  takes them on is spending churn on the class of finding least likely to be real.
  Unlike a below-floor deferral they get **no GitHub issue** — a threshold is a bar on
  evidence rather than a judgement that the finding is not worth fixing, so the record
  is the board row and the report, and the next round can raise it again with a second
  seat behind it.
- **SonarCloud** — the hard-gate issues, **if the `sonarqube` seat ran**. Where it did,
  they MUST end up resolved: that is what "hard gate" means, and it is the one part of a
  round that is not a judgement. **Where it did not, there is no gate on this round and
  you must not write as though there were.** The seat is `enabled: false` in this repo's
  `.harness-rules.sample` and off in the harness defaults, and it is currently being
  switched off across the fleet while the convergence work is proven — so "no SonarCloud
  block" is the ordinary case right now, not an anomaly. Read `reviewers_ran` rather than
  inferring it from an empty list: an empty **SonarCloud issues** block and an absent one
  are different claims, and only the first says a gate looked and found nothing.

  Everything downstream of this bullet is conditional on the same fact. Where a later
  section says a Sonar issue must be resolved, or that `round_stop` counts one, or that
  the gate is clear, it is describing a round where the seat ran. **A fixer briefed on a
  hard gate that does not exist is being told to resolve an empty list**, which reads to
  it as a gate it has passed.
- **Skipped reviewers** — and the word covers two different facts, so say which. A seat
  the repo has **configured off** is a decision somebody took, not a gap: it is not
  thinner coverage than the review this repo asked for, and warning about it every round
  trains a reader to skip the line where the other kind appears. `sonarqube` is that case
  today, fleet-wide. A seat that was **configured on and did not run** — a missing CLI, a
  dead login, an auth failure, a crash — IS a coverage gap: the review is thinner than
  the one that was asked for and nobody chose that, so surface it in §6 with what it
  cost. Report the first as configuration when it is worth stating at all; report the
  second every time.
- **Coverage declared** — per reviewer, what it said it could not assess, plus
  any reviewer the panel reports as truncated. These are what separate "clean"
  from "I could not tell"; carry them to §6 rather than dropping them.
- **Guard-to-guarded** — the line printed under that name (`guard_ratio` in the
  JSON): test and doc lines ADDED against source lines added, over the whole PR. It is
  REPORTED and gates nothing (#67's instrument-before-gate rule, #492), so there is no
  threshold here for you to apply and none for you to invent — carry the number into
  §6 the way you carry the coverage declarations. What makes it worth reading is
  WHEN it arrives: it is available from round 1's diffstat, where `max_fix_growth`
  needs a second round before it has a ratio at all, and the cycle it was filed from
  produced 406 lines of test for a 66-line config change with nothing noticing.
- **Refereed-ness of the last fix pass** — the line printed under that name
  (`round_stop.unrefereed_fix` in the JSON): the CHURN of the pass that landed
  between the last round and this one, split into production / test / prose. Unlike
  Guard-to-guarded above it, this one GATES — see the section on #554 below — and it
  says so on the line. Absent on round 1 and on a round with no readable fix range,
  because there was then no pass to measure and a line of zeroes would claim one
  wrote nothing.
- **Budget spend of the last fix pass** — the line printed under that name
  (`round_stop.fix_budget` in the JSON, #622): what the pass that landed between the last
  round and this one COST, priced the way `low_severity_fix_lines` is spent — production
  at 1, test and prose at `unrefereed_line_weight`, over `git diff --numstat` churn — set
  against the budget that was in force. Every other bound on a fix pass is measured from
  outside it; this one used to be counted by the fix pass, out of the paragraph you relay
  in §4. It is now counted here as well, and the two are held to one number because both
  read the same resolved dials.

  **`within` is three-state and you must not flatten it.** `true` means the WHOLE pass —
  mandatory work included — priced under the budget, so the 💸 band did too whatever the
  fixer's own arithmetic said: that is a fact and it is the reading worth carrying into
  §6. `false` means only that the budget cannot be SHOWN to have been kept, because the
  budget bounds the 💸 band and a diff cannot say which lines paid for which finding — a
  round clearing two P1s can spend three hundred production lines the budget never
  applied to. **Do not treat `false` as a breach and do not brief the next fixer as
  though it were.** `null` means there was nothing to measure: round 1, an unreadable fix
  range, or no budget in force. The upper bound is REPORTED and gates nothing (#67).

  **`breach` is the side `within` does not have, and it is the one that gates.** It is
  `true` only where every entry in the LAST round's To fix list could be READ and every
  one of them sat in the 💸 band — no mandatory work in front of that pass, and nothing
  in the list this round cannot identify — so the priced total is not an upper bound on
  the budgeted spend, it IS the budgeted spend, and going past the limit is a fact.
  That ends the round, files a veto line and costs the round its confidence.
  `false` means the premise held and the pass stayed inside its budget, which is a
  stronger statement than `within: true`. `null` is the ordinary case and means no
  strict verdict was available; `brief` beside it says which of the reasons it was, in a
  sentence, and carries the prior round's finding count and how many of them were
  budgeted so you can check the claim rather than take it. One of those reasons is
  worth knowing about: since #551 the budget is `min(dial, its pro-rata share of the
  cycle's first round)`, so the number a round SPENDS against is not the dial — and a
  cycle whose two rounds applied different budgets reaches no strict verdict, because
  a breach priced against a bound the fixer was never given is an accusation about a
  policy nobody ran. **A `null` here is evidence
  of nothing** — do not read it as a pass and do not read it as a failure. There is
  still no flag to arm and no new dial: the limit is one the repo already wrote, the
  band is one its own floors carve out, and the premise is a proof rather than a
  threshold — the epic this came from is explicit that it adds no dial, and this adds
  none.
- **Fix surface** — `round_stop.fix_surface` in the JSON (#619): `files`, the files the
  last fix pass touched; `new_files` and `count`, the subset no earlier round had read;
  `prior_files`, what the rounds before it had. Like Guard-to-guarded it is REPORTED and
  **gates nothing** (#67) — there is no threshold to apply and none to invent, and unlike
  its siblings in that block it has no `fired` field, because there is no verdict to
  have. Carry the count into §6.

  What makes it worth reading is that it measures a different quantity from everything
  else downstream of a fix pass: `max_fix_growth`, `max_fix_growth_chars` and
  `fix_injection` all count lines or findings, and fifteen lines added to two nginx
  templates nobody had reviewed is small by all three. On lexray#1780 round 3's pass
  touched 12 files, 7 of them never reviewed, and both of the cycle's later P1s were in
  that new surface. The fixer brief carries the corresponding rule — a fix stays inside
  the files the change already touches, and going outside them is declared in the fix
  summary.

  **It is `null` where it could not be measured, and null is not zero.** Round 1 has no
  fix pass to read and a rewritten branch has no readable range; a `0` there would be the
  claim that a pass opened no new files, when what happened is that nobody looked. Relay
  the two differently.
- **Rounds** — the `**Rounds:**` line (also `round_stop` in the JSON): whether
  the cycle should go again and whether stopping would be convergence.

The run also records itself on the quarterback board (which models ran, what each
raised, and how the judge ruled on it) so the fleet accumulates an answer to
"which reviewer earns its cost" — see the board's `/panel` page. This is
automatic and best-effort: **do not** post the panel result to the board by hand,
and never re-run the panel to produce a record. A board that is down or
unconfigured prints one line and changes nothing about the review.

Show the user this panel summary before launching the fixer.

> First run may need `op signin` once where the `sonarqube` seat is enabled (its token
> caches afterwards), and `codex login` for the Codex reviewer. Missing reviewers are
> reported as skipped, not fatal.

## 4. Launch the fixer sub-agent

Stamp the stage first — `R<r>F`, the fix phase of round `<r>` (`R1F` after round
1, `R2F` after round 2):
```bash
qb-stage R1F
```
This is the half of the cycle nothing else can see. A review round and its fix
phase sit on the same branch, the same PR and the same commit range, so without
it the bar cannot tell "waiting on four reviewers" from "a sub-agent is
rewriting the code right now" — and those want very different things from you.

Read `~/.claude/commands/review-pr.md` and lift its **SUB-AGENT BRIEF** verbatim
— that is the canonical boil-the-ocean fix/verify/commit discipline; keep it
single-sourced. Launch **one** `general-purpose` sub-agent with that brief,
with these overrides:

- **Replace step 2 "Deep review" (self-discovery) with the supplied panel
  findings.** They are already exhaustively reviewed and judged — the sub-agent
  does NOT re-derive them. Paste the full **To fix** list, and the **SonarCloud**
  issues **where that seat ran**, into the brief as the findings to resolve.
- **Replacing step 2 does NOT remove step 3's consumer line (#616), and this is the
  path it was written for.** The brief owes one line per finding, before that finding's
  patch, naming who calls the code the fix would change and — where it reaches a response
  or a stored artefact — the entitlement tier it is served to. Nothing about a
  panel-supplied list makes that cheaper to skip; it makes it easier, because the finding
  arrives already verified by a seat and confirmed by the judge, and **that is exactly the
  provenance the measured failure had**. On lexray#1780 round 3 a P2 carrying both was
  wrong about what `html_preview` is for, the fix merged a paid glossary into the
  anonymous teaser, and round 4 found the entitlement leak. Say in the brief that every
  finding you are handing over gets its line whatever you and the judge already think of
  it, and require the **Consumers** column in the summary table it returns.
- Its job is to resolve **every panel-confirmed finding the round asked it to
  clear** — the **To fix** list, which is already filtered to the round's
  `fix_severity_floor` — plus every SonarCloud issue **on a round the `sonarqube` seat
  ran**, to the "nothing left to
  improve" standard. Paste the **To fix** list and those Sonar issues, and
  **not** the 🔽 *Reported, not this round's work* list: a fix pass that takes those
  on is the growth the floor exists to stop, and the floor has already made that
  judgement (#165). The 👥 list under the same words is out for the same reason and a
  different rule — one seat's evidence, and the round has said that is not enough to
  spend a pass on (#78).
- **On a round with no `sonarqube` seat, say nothing about a hard gate at all** — do not
  paste an empty SonarCloud block, and do not tell the fixer the gate is clear. An empty
  list under a heading reads as a gate that looked and found nothing, and the fixer has
  no way to tell that from a gate that never ran. With the seat off across the fleet
  today (§3), this is the ordinary case: the brief is the **To fix** list and that is all.
- **An additional defect the sub-agent trips over while fixing is subject to the same
  floor and the same scope as a panel finding.** At or above the round's
  `fix_severity_floor` *and* inside the change under review, it gets fixed — a P1 the
  panel missed and the fixer walks straight into is still a P1, and leaving it because
  no reviewer happened to name it is the worst outcome on offer. Below the floor, or
  outside the change under review, it is **reported in the summary and not fixed**,
  exactly like a below-floor panel finding. What this replaced granted a blanket
  permission to fix whatever the pass noticed, three lines from the instruction that
  establishes the floor — an open route back to fixing everything, and it bit hardest
  for precisely the P3/P4 items the floor exists to hold back.
- **Paste the 💸 marks with the findings that carry them, and the budget with the
  list.** The marks are how the fixer tells a budgeted finding from an unconditional
  one, and a **To fix** list pasted without them briefs the pre-#297 behaviour: every
  low-severity finding unconditional, which is the 408-line round-1 fix pass this
  budget exists to stop. Copy the note under the heading verbatim — it carries the
  line count and the spend rule, and since #551 that count is **this round's**, scaled
  to the size of the cycle's first round: on a small PR it is less than
  `low_severity_fix_lines`, so never substitute the dial's value for the number the note
  states.
- **Selecting findings and capping churn are INDEPENDENT controls, and naming
  findings NEVER lifts the budget (#492).** A human who says "just fix the
  concurrency ones" has narrowed *which* findings this pass may touch. They have said
  nothing whatever about *how much churn* one pass may add, which is the separate
  question `low_severity_fix_lines` answers — and that dial's own docstring is
  emphatic that the question is **mechanical, not discretionary**: the spend is
  COUNTED with `git diff --numstat` after each fix, never estimated, and the fixer is
  never asked "does this risk ballooning?", because that is a judgement by the actor
  whose judgement the 85% impugns. Reading a shorter list as the budget having been
  spent by decision is a natural mistake for an orchestrator that has just been handed
  one, and it has been made: on the cycle #492 was filed from the budget was lifted
  for round 2 *because* the human had named the findings, and the pass came out at 422
  lines and produced 13 new findings — the exact shape the budget exists to prevent,
  with the one brake still capable of firing being the one that was removed. So relay
  the budget with a narrowed list exactly as you would with the full one, and a pass
  that runs out of it reports the unpaid findings exactly as it reports below-floor
  ones (§4b's road 2).
- **Relay the dials into the brief.** The sub-agent cannot read `.harness-rules` for
  itself in worktree mode and must not guess: state `fix_severity_floor`,
  `low_severity_fix_lines`, `unrefereed_line_weight`, `reviewer_scope` and
  `fixer_may_defer` from the panel
  report's **Panel dials** line, in the brief, as the values in force. The brief's own opening asks for them
  by name, and a fixer left to guess reverts to "fix everything you find, anywhere",
  which is the behaviour these settings exist to bound.
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
  `fix: resolve panel review findings for PR #<n>` — add ` (round <r>)` from
  round 2 on, so the PR's history says which commit answered which review.
- **A later round gets its own fixer, briefed with that round's findings only.**
  Re-briefing it with round 1's list has it re-examine work already done and
  buries the new finding — which is the one the round existed to catch.
- **The brief's step 3a (escalate, don't patch) applies to panel findings too**,
  and this is where it earns its keep: a panel finding can be a premise finding
  rather than a defect — #132's P1 was one — and the fixer is the only reader
  positioned to notice, because it is the one being asked to write the special
  case. An escalation from this fixer is **not** a finding left outstanding for
  the next round to pick up; §5 says what happens to it.

## 4b. Record what actually happened to each finding

Once the fixer has pushed, say what became of every finding it was given. This is
the half the judge cannot know: it ruled at review time with no more access to
the answer than the reviewer it was ruling on, so without this the board scores a
confident wrong finding exactly like a real one. On PR #64 three of six
judge-confirmed P2s were plainly wrong — the `installPhase` it said enumerated
three scripts does `install -m 0755 bin/*` and globs — and they are still in the
board as confirmed.

**`qb record-outcome` ships in this repo** (`harness/bin/qb`, as of #230) and is on PATH
wherever the harness is — but a host still running an older `qb` from somewhere else has a
verb that exits 2 with a usage line. If that happens, record the outcomes after the rebuild
rather than dropping them, and say in the relay that they are outstanding — an outcome nobody
records is the gap this whole feature exists to close.

**Map the fixer's finding IDs to keys first — this is a step, not an aside.** The
fixer reports the ID it was given (`236-F01`, exactly as the report prints it in
square brackets) in its `Narrowed`, `Deferred` and `Escalated` blocks, because the ID is
the only
identifier it ever saw: the report carries no keys, on purpose (see below). You hold
the payload, so the ID → key mapping is yours, and every `key` in the JSON below and
every `--escalated` and `--narrowed` argument in §5 comes out of it. The one-liner prints
both columns:

```bash
# id, key, and what each finding actually was — the left column is what the fixer
# quotes back at you, the second is what the board, --escalated and --narrowed take
jq -r '.to_fix[] | "\(.id)\t\(.key)\t\(.severity)\t\(.synthesis)"' r<r>.json

cat <<'JSON' | qb record-outcome
{"repo": "<owner/name>", "pr": <pr>, "outcomes": [
  {"key": "<key of a finding the fixer resolved>", "outcome": "fixed"},
  {"key": "<key of one fixed where it was raised, general form not taken>",
   "outcome": "narrowed",
   "note": "gzip on the /api/ location block; server-level gzip is a separate change"},
  {"key": "<key of one that was not a defect>", "outcome": "refuted",
   "note": "installPhase does `install -m 0755 bin/*` — it globs, the script IS installed"},
  {"key": "<key of one left for later, at or above the issue gate>",
   "outcome": "deferred", "deferred_to": "prisonblues/quarterback#132"},
  {"key": "<key of one BELOW the gate — board row only, see below>",
   "outcome": "deferred",
   "note": "P4: the retry loop has no jitter; real, not this change's job"}
]}
JSON
```

A key is a 16-character hex digest, and it is the same identity the board chains
observations by — so an outcome recorded now attaches to every round that raised
the defect, including the ones still to come. (Substituted rather than shown
inline because a literal one reads as an API key to every secret scanner,
`gitleaks` on this repo's pre-commit hook included. That is also why the **report**
renders only finding IDs and why the fixer is asked for one rather than a key: the
report is posted as a PR comment, and rendering keys into it would trade a mapping
step you can do with `jq` for a secret-scanner hit on every panelled PR.)

One of five per finding:

- **`fixed`** — the fixer changed the code and the finding is answered.
- **`narrowed`** (#615) — the fixer changed the code and the finding is answered **at the
  point it was raised**, with the general form left unwritten. It **clears**, exactly as
  `fixed` does: `round_stop` counts it answered, not outstanding, and a cycle may
  converge with narrowed findings in it. **Requires a `note`, and the note is the general
  form** — what fixing the class would have taken — lifted from the fixer's `Narrowed`
  block, because that sentence is the entire reason the outcome exists. A bare `narrowed`
  is a `fixed` that has lost the only thing distinguishing it.

  **It gets a GitHub issue only where the general form is itself a claim-miss** — where
  the class-wide gap means the change does not do what it set out to do, which is a work
  item somebody has to pick up. Every other general form is an observation and the row is
  the whole record. This does **not** read `file_deferral_issues`: that dial gates
  **deferrals**, a narrowed finding is not one, and the question here is what the general
  form IS rather than what the instance was. (Both tests happen to be about shape rather
  than severity now — see the next section — but they are separate tests and only one of
  them is a dial.) Do not round a narrowed
  finding up to `fixed` because it looks like one — "I fixed this" and "I fixed the
  instance of this" are different facts, and the leaderboard and the round-stop rules
  read them differently.

  **Recording the row is half of it — the key also goes to the next round, as
  `--narrowed`.** The board row is the record; the flag is what makes the finding clear.
  Record it here and pass it there, in §5. Do one without the other and the fourth
  outcome reaches nothing: the fixer declares `narrowed`, you record it, and the next
  round still counts the finding as outstanding and goes again — which is the same
  pressure to write the class-wide fix, arriving through the cycle instead of through the
  brief.
- **`refuted`** — it was not a defect. **Requires a `note`, and the note is the
  point**: you are already writing the refutation into the PR comment and the fix
  commit, in prose nothing can count. A bare `refuted` is the same
  confident-assertion-with-nothing-behind-it the release exists to measure.
- **`deferred`** — real, not now. **Three roads
  arrive here and all three are the same row.** (1) The fixer said so itself, under
  `review_panel.fixer_may_defer` — the defect is real and outside what this change is
  for, with the two justifying lines in its summary's `Deferred` block. (2) The
  panel reported it BELOW the round's `fix_severity_floor`, so it was never in the
  fixer's brief at all — or it was marked 💸 and the round's `low_severity_fix_lines`
  budget ran out before it, which is the same row for the same reason (#297). (3) An
  **escalated** finding (the brief's step 3a): the defect is
  real and the fix is what is in dispute, so `refuted` would be a lie about the
  finding, `fixed` a lie about the code, and `narrowed` a lie about both — no patch was
  written at all, narrow or otherwise. There is no sixth outcome to
  invent — the vocabulary is a database constraint, not a convention. Recording it
  does **not** settle the question or take the finding off §5's outstanding list:
  that list is computed from the round's own payload, never from this table, and the
  escalation stays open until a human answers it. `deferred_to` names the premise
  issue, and that issue does not exist yet at this point in the run — so **the
  escalated row is the one you record last**: relay (§6), open the issue there,
  then come back and record this row naming it. You open it, never the fixer, and
  it is an issue that *asks* the question in the fixer's own five fields
  (premise, what it explains, what removing it costs, the patch not written, the
  `--ask` verdict) rather than one that picks an answer. When the human's answer
  lands, the row moves: `revisions` and `prior_outcome` exist because a `deferred`
  that later becomes `fixed` is the expected lifecycle, not an anomaly.
- **`superseded`** — a later finding replaced it; name that finding's key in
  `superseded_by`, which is **required** for the same reason a note is required
  for a refutation: without it the row records "replaced by something".

### Which deferrals get a GitHub issue — `review_panel.file_deferral_issues` (#482)

**Every deferral gets a board row. Only some of them get a GitHub issue**, and this
setting is which. The report's dial line says the answer for the round you are
recording, in words, so read it there rather than opening the rules file.

**It is no longer a severity gate. The shipped value is `shape` (#620), and the question
it asks is what the ticket would BE**, not how bad the finding was. A **category** or a
substantive **single named item** may become an issue; a **batch** gets board rows and
never one, whatever its severity mix. The two ends and the old bands are all still legal:
`always` is the pre-#482 behaviour (an issue for every deferral), `never` files none, and
any of `P1`..`P4` restores the severity cut this dial ran under from #482 until
2026-08-30 — at or above the band an issue, below it a row. The bands are the documented
way back, so a repo that wants the old behaviour says so in one word rather than working
around this one.

The two records were being conflated. The **board row** is the durable one — it
chains by finding key across rounds, it feeds `/panel`, and it is what stops the
leaderboard scoring a confident wrong finding like a real one. The **GitHub issue** is
a work item on somebody's tracker. For a P1 or P2 deferral those coincide; for the
P3/P4 tail they do not, and the tail is where the volume is. Measured on this repo on
2026-08-26, roughly twenty open issues were panel deferred-finding exhaust and nothing
else (#66 #69 #72 #74 #95 #104 #111 #119 #120 #126 #132 #133 #140 #223 #237 #285 #286
#288 #300), and #283 is a rescue *from* one of them — three live defects that had been
sitting inside a deferred-findings dump nobody read.

So, per finding — under `shape`, which is the default:

- **A category** — "the retry paths in this module have no jitter anywhere", "these four
  readers were never made exhaustive". It has a subject, and somebody picking it up knows
  when they are done. **Open the issue and name it in `deferred_to`.**
- **A substantive single named item** — one real defect, stated once, with its file and
  its consequence. **Open the issue and name it in `deferred_to`.**
- **A batch, or anything you cannot classify** — open nothing. Record the row with **no
  `deferred_to`** (the
  field is nullable, the API accepts a `deferred` outcome without one, and `/panel`
  renders such a row with no target rather than as broken) and **a one-line `note`
  saying what the defect is and why it was not fixed this round.**

  **Unclassified falls through to `batch`, and that direction is deliberate.** The gate
  tests whether a deferral IS a category or a single item, so no shape given, an empty
  one, or a word the panel does not know all land here and file nothing. Under the old
  severity bands the fall-through went the other way — an unreadable severity filed the
  issue, because a spare line on a tracker was the cheap error. Under `shape` that spare
  line is precisely the failure being fixed, so the cheap error is now the row.

  **And nothing upstream classifies for you, so read the gate's silence carefully.**
  No seat, no judge and no round payload emits a shape — the panel reports findings,
  not ticket shapes — and neither of the harness's two automatic deferral paths
  supplies one either: a below-floor remainder is gated on severity alone, an
  unverifiable claim on its own exemption. So under `shape` **every deferral the
  machine raises by itself lands in this bullet and files nothing.** That is the right
  answer for a remainder, which IS a batch, and it is **not** the harness having
  judged that no issue is warranted — it has not looked. The two roads above are
  reachable only where you classify a deferral yourself and open the issue by hand,
  which is what this section asks of you; if you do not make that call, nobody does.

  **The note is not optional here and it is the whole difference between a record and
  a dumping ground.** With an issue, the issue's title and body are what somebody
  reads later; with no issue, the note is. A row with neither is the markdown list this
  all replaced, wearing a database. It is also what makes the row *findable*: `GET
  /review/findings?repo=<owner/name>&pr=<n>` returns every chain on the PR with its
  outcome attached, which is how a fiddly finding gets found again — the read this
  write exists to serve.
- **An escalation is exempt at every setting, `never` included.** Its issue is not a
  work item, it is the question being put to a human, and it is what carries that
  question past the end of this session. Road 3 above is unchanged.
- **An unverifiable claim is exempt too, for the same reason** (#547) — see the next
  section, which is where the fourth road arrives.

**Why a batch may not, whatever its severity mix.** Nine sub-floor findings from one
round get nine board rows, each with the one-line note above, and no issue. Twenty P3s in
one issue is not a deferral, it is a **transfer**: the findings leave this cycle for
somewhere with no owner and no next action, and the note that made each row readable is
flattened into a list. Measured on this repo: twenty issues existed only because a
panel round found something and the cycle did not clear it (#66 #69 #72 #74 #95 #104
#111 #119 #120 #126 #132 #133 #140 #223 #237 #283 #285 #286 #288 #300), carrying 345
findings between them by their own titles, created over six days, and **not one has
ever been closed** — in either sense, worked or abandoned. #283 is a rescue *from* one
of them: three live defects sitting inside a deferred-findings dump nobody read. That is
the measurement the dial's default was changed on.

**This knowingly amends #42.** That rule said a capped round's findings must be handed to
somebody rather than to nobody, and it is still in force — what changes is who somebody
is. For a batch it is the **board**: a row per finding, each with its note, queryable by
PR at `GET /review/findings?repo=<owner/name>&pr=<n>`. An issue that nobody opens is not
a better answer than a row somebody can query; it is the same findings filed twice and
read never, which is what the measurement above is.

If a batch contains something that IS a category or a real single item — and it usually
does — lift that one out and file it on its own terms. What is forbidden is the
issue-shaped dump, not the issue.

**If `qb record-outcome` fails, file the issue whatever the gate says**, and say in
the relay that you did and why. For a batch the row is the *only* record, so a
board that refused the write and a gate that suppressed the issue between them lose
the finding outright — which is the one outcome this setting must never produce. The
tracker is the fallback, not the default, and this is the one case where an
issue-shaped dump beats losing the findings.

**Do not mark your own findings `refuted` unattended.** That is a self-grading
loop and #40's constraint applies for the same reason. The board cannot tell a
fixer from a reviewer, so it does not refuse — it records `set_by` from your
token, marks the row unattested, names it back in the response, and `/panel`
shows the split. When a human has confirmed the refutation, send `attested_by`;
when one has not, record it anyway (an unattested refutation on the board beats
one in a comment nothing reads) and say so in the relay.

**`attested_by` is a claim you are making, not a signature the board checked** —
it is free text in your own request, stored beside your identity and rendered as
"you claim signoff by X". Sending it for a human who did not actually confirm is
the one way to corrupt the number this whole feature exists to produce.

Re-reporting is safe and every edit is visible: a repeat FILLS an empty field,
rewriting a stored one counts as a revision and comes back in `amended`, an
explicit `null` clears a field (how you retract a mistaken attestation), and a
changed answer keeps what it changed from. The status code says which happened —
201 created, 200 updated, 422 when nothing was accepted — so `qb`'s exit status
is worth reading rather than assuming.

## 4c. The round's unverifiable claims — a fourth road, and the only one with a flag

The report's **Unverifiable claims** block lists what this PR asserts that nothing in
the review could check: the claim, a key (`uc-` and twelve hex), and what instrument
*would* settle it. They are not findings. Nobody is asked to patch one, there is no
severity, and the fixer never sees them — the judge ruled that no seat here could have
settled them with what it was given, which is a fact about the instrument and not
about this PR.

Each one still costs the round its confidence until somebody accepts it, and that is
deliberate: without it, a model could talk its own review into a confident stop by
declaring everything unanswerable. What it buys is that the question can now be
**discharged**, where before it could only be held.

So, per claim:

1. **Read it and decide** whether this PR may land with the claim unchecked. That is
   a judgement about the change, and it is yours (or a human's) — never the panel's.
2. **Open a GitHub issue for it, whatever `review_panel.file_deferral_issues` says.**
   Same footing as an escalation and for the identical reason: the issue *asks* the
   question rather than filing a task, and it is what carries it past the end of this
   session. Title it as the claim; body says what would settle it and links this PR.
3. **Do not try to `qb record-outcome` it.** That call keys on a FINDING key and an
   obligation is not a finding — it has no reporter, no severity and no chain — so
   there is nothing on the board for the row to attach to, and inventing one would
   need a schema change this deliberately does not take. Its durable record is the
   round's payload (`unresolved_claims`, with the key, the claim, what would settle it
   and whether it has been accepted) and the issue you opened in step 2. Say in the
   relay which issues those are.
4. **Then pass the key back** to the next round: `--acknowledge uc-0123456789ab`,
   repeatable, and the report prints the exact command. It is inherited by later
   rounds through `--baseline`, so you do it once per cycle and not once per round.

**Per claim, never in bulk, and there is no flag that accepts them all.** A blanket
yes is the cheap gate, and a gate that always passes is worse than one that always
holds because it looks like assurance. If a claim comes back next round under a new
key, the judge reworded it — the key is derived from the claim's text and absorbs
spelling but not rewording — and the run says so in `config_notes` rather than
silently ignoring the stale one. Where that same round also raises a claim no
acknowledgement names, it pairs the two by key and asks you outright whether they are
the same claim. It does not compare the two wordings and never re-acknowledges for you:
read both in the Unverifiable claims block, and if they are the same claim, pass the new
key on the next round's `--acknowledge`.

## 5. Re-review the fix commit — the round that used to be skipped

Once the fixer has **pushed**, run the panel again over the new commit:

```
qb-stage R<r>                                    # R2 in round 2, R3 in round 3 …
python3 ~/.claude/loops/panel.py --pr <pr> --post --round <r> --max-rounds <N> \
    --baseline /tmp/tmp.AbC123/r1.json [--baseline …each earlier round…] \
    --json-file /tmp/tmp.AbC123/r<r>.json
```

(The `/tmp/tmp.AbC123` above stands for the directory `mktemp -d` printed in §3 —
paste that literal path here, in every round. Shell variables do not survive
between commands. The panel checks each baseline's `repo`/`github`/`pr` against
the run it is doing — they must be present *and* match — and refuses to count a
payload from another review, or one whose round is not earlier than this one's,
so a mis-wired `--baseline` shows up as a reported problem rather than as
findings that look repeated. A round past the first with **no** `--baseline` at
all is reported the same way, and costs the round its confidence.)

Pass **every** earlier round's payload as a `--baseline`, or a finding raised in
round 1, missed in round 2 and raised again in round 3 counts as new.

Add `--acknowledge uc-xxxxxxxx` for each unverifiable claim you accepted in §4c —
once, on the first round after you accepted it; the register is inherited through
`--baseline` from there. Omit them and the round holds on a question you have already
answered.

### Do not rewrite the branch between rounds, and know the cost if you must (#500)

**A rebase or force-push between rounds disarms three of this cycle's convergence
instruments at once**, because provenance (#48), recurrence (#67) and `--scope
increment` all read the same thing: the range between the last round's `head_sha`
and this one's. `compare/a...b` is the three-dot form, so after a rewrite the old
head is no longer an ancestor and GitHub answers `diverged` — the range would span
commits no fix pass wrote, so the panel refuses it rather than blaming the fixer for
every line the PR ever added.

What that costs is concrete. Every new finding is recorded `unknown` instead of
`introduced` or `missed`, and **`escalate_on.fix_injection` (#497) cannot fire**:
the rate is `introduced` over every new outstanding finding, and the unattributable
ones sit in the denominator, so it is depressed toward zero however badly the fix
pass behaved. On the cycle #500 was filed from, that happened on round 3 of a
three-round cycle that ended on the cap — the exact shape the gate exists to stop.

The round says so where the verdict is read: a **veto line, and `confident` false**,
the same treatment a reviewer that could not read the whole diff gets.

**And then it tries to repair it (#504).** The range is wrong, not the history: the
fix pass's commits are still on the branch under new SHAs, and `git patch-id` names
them by what they CHANGED rather than by where they sit. So a rewritten round rebuilds
the pass out of the local object store, `payload.fix_range_source` reads
`reconstructed`, provenance, recurrence and `escalate_on.fix_injection` come back, and
the veto does not fire. Read `config_notes`: the round states what it rebuilt and what
it cost.

**It is exact or it refuses, and a refusal leaves the round exactly as blind as it
was** — the veto fires and nothing is attributed, with `fix_range_rebuilt.why`
naming which of these it hit:

- **No local checkout.** `patch-id` is git rather than the compare API, so a repo
  with no `path` in its rules cannot rebuild anything — and neither can a box that
  never held the pre-rebase head, since a rewrite only orphans commits where
  somebody still has them.
- **A commit the last round reviewed changed content in the rewrite** — a conflict
  resolved during the rebase, an amended tip. That commit is somewhere among the
  ones this would call the fix pass and nothing can say which, so attributing them
  would blame the fixer for work already reviewed.
- **The pass is not the TAIL of the branch** (a reorder, an `--autosquash` that
  landed a fixup low in the series). Then no single diff is the pass, and reading
  its commits' patches separately would attribute lines the pass added and then
  removed.
- **An ambiguous patch-id** — the branch carries more copies of a patch than the
  last round had, so which is the fixer's own cannot be told from which is the
  replayed one.
- **No correspondence at all** (a squash, a re-created branch), and **a branch reset
  BACKWARDS**, where the pass was removed rather than rewritten. The round says the
  second in those words, because a force-push that dropped work must not read as a
  quiet cycle.

Refusing rather than leaning is a deliberate trade, and worth knowing when you read a
round that did not rebuild: `escalate_on.fix_injection` is calibrated on `introduced`
being a FLOOR, so a reconstruction that over-counted would end cycles wrongly and no
`config_notes` line prevents that — nothing reads a note before firing a brake.
`--scope increment` is not repaired either way (scope is settled before the seats
run), and neither is #506's proposal below, which reads the compare range.

So:

- **Prefer merging the base branch into the PR** over rebasing it. That leaves the
  old head an ancestor (`status: ahead`), so the range still reads without a rebuild.
  It is not free — the base branch's own commits then fall inside the range and their
  lines are attributed to the fix pass, so `introduced` over-counts — but an
  over-counting instrument is worth more than a dark one, and it fails toward stopping
  the cycle rather than toward letting it run.
- **If you must rewrite, do it between CYCLES rather than between rounds** — after a
  stop, before the next `--round 1`. The rebuild is a repair, not a licence: it costs
  an accuracy you did not have to spend.
- **Rewrite in the checkout the panel reads.** A rebase done somewhere the panel will
  never see — another box, a worktree that is then thrown away — is the one that
  cannot be rebuilt, and it looks identical to the one that can until the round runs.
- **If you already have, and `fix_range_source` is not `reconstructed`, do not read
  that round's quiet as convergence.** The veto says as much. Re-running the round
  with `--scope pr` gets the review back but not the attribution; only a round whose
  fix pass can be reached — by range or by patch — can attribute.

One instrument this does *not* disarm, worth knowing so you do not over-correct:
#84's premise register is keyed on declared text rather than on commits, so it
survives a rewrite intact. `max_fix_growth`/`max_fix_growth_chars` also keep working,
but note they measure against `Baseline.first_reviewed` — a base-branch merge inflates
the PR against a denominator from before it, so a ceiling may fire on growth the fix
passes did not write.

**Round 2+ reviews the fix commit, not the whole PR again** (v2.28), and it gets
there off the baseline you just passed — `head_sha` in that payload is the anchor,
so this needs no new flag from you. Behind the fix commit the reviewers get the PR
**as it stood at that anchor**, which is what the earlier rounds actually read.
Three consequences worth knowing when you read the result:

- **`diff_chars` is the increment, not the PR.** It drops sharply at round 2 and
  that is the feature working, not the PR shrinking. `scope` in the payload says
  which it is; `context_chars` is the context prepared alongside it, and `pr_chars`
  is the whole PR's size on every round whatever its scope — that is the one to read
  for "how big has this change become", and the one `max_fix_growth` measures (#298).
- **The target shrinks; the bill mostly does not.** A round still sends its target
  plus its context, so do not read a small `diff_chars` as a cheap round. What the
  scoping buys is where the reviewer's attention goes, and — when a budget is set —
  which end of the material a cut lands on.
- **Check `config_notes` before believing the round was scoped.** Whenever the
  anchor is missing, nothing was pushed between the rounds, a fetch failed, GitHub
  returned a truncated comparison, or a base-branch merge made the range bigger
  than the PR itself, the panel falls back to reviewing the whole PR and says so
  there. `scope: "pr"` on a round 2 is that, and it means the round cost what it
  always used to. The same list carries the caveats on a round that WAS scoped: a
  rebase between the rounds, or a merge commit inside the range.

### When the cycle ends because NOTHING COULD CHECK the fix pass (#554)

`escalate_on.unrefereed_fix` ends the cycle when the pass that landed between the
last round and this one churned four or more lines and **not one of them was
production code** — all test and prose. You will see it as a veto line, `confident:
false`, and a `stop_reason` that names the dial rather than the cap.

**The argument, in one sentence:** a production fix has an external referee — red/
green either detects the bug or it does not, with the suite and CI behind it — and a
test fix has none, because nothing tests a test. A docstring fix has none either. So
a pass whose entire output is test and prose produced only artefacts that no
mechanism in this loop can check, and the round it would buy is a review of them.

Measured on lexray#1697 round 1, since reverted: a 93-line pass across three files
whose entire production share was a docstring and a comment introduced ten findings,
nine in the test files it wrote and the tenth in that docstring. Red/green ran and
went red 4 of 4 — it asks whether a new test detects the thing it was written for,
never whether that test also opens a socket or whether its assertion is sufficient.

**What it is NOT.** It is not a ratio and there is no proportion to tune: a five-line
production fix carrying a forty-line regression test is 89% unrefereed and is exactly
the work this panel wants, so the rule is the ABSENCE of a refereed component and
nothing else. It is also not a judgement about the fix pass's worth — the split is
read off the fix range's own diff, which the round already fetched for provenance, so
it costs no extra call and the fixer is never asked about it (#297's discipline).

(Not `git diff --numstat`: that reports per-file insertion and deletion TOTALS and
cannot see a comment, a blank or a docstring. Paths are free from numstat and lines are
not, which is the half that makes this measurement mean what it says.)

**What to do with it.** Read `round_stop.unrefereed_fix` for the counts, and relay
it in §6 as what it is: the last pass answered its findings by writing more test, so
the question for a human is whether the findings had a production answer that nobody
found. #507's constructive pass follows this rung like the others, so where it is
armed each seat has already been asked for the smallest change that satisfies its
findings — that is the material to put in front of them.

It shares one blindness with #489's rung below and it is worth knowing which: both
read the fix range, so a rewrite between rounds that #504 cannot rebuild disarms
both. `escalate_on.new_findings_not_falling` is the only rung computed from the
rounds' own counts.

### When the cycle ends because the FIX PASS was generating the work (#489, #506)

`escalate_on.fix_injection` ends the cycle when more than half a round's new
outstanding findings were attributed to the fix pass immediately before them: the
loop's rule 1 is being fed by the loop's own output, and a termination test fed by
its own output can only end on the cap. You will see it as a veto line, `confident:
false`, and a `stop_reason` that names the dial rather than the cap.

**Ending the cycle is half the answer, and the other half is your job.** The fix
pass that caused it is still on the branch — the PR ships carrying a change the panel
has just finished saying generated more of the round's work than the pull request
did, minus the round that would have found the rest of it. Stopping means the loop no
longer makes it worse; it does not make it better.

So the round now hands you the decision already priced, in `round_stop.revert`:

```
jq '.round_stop.revert' /tmp/tmp.AbC123/r<r>.json
```

- `range` / `commits` / `commit_count` — the offending pass's **commit range**,
  which is the same range provenance attributed against, and the commits inside it.
- `spans` — how many fix phases that range covers. Normally `1`. More than one means
  no intervening round recorded a commit to anchor on, so the range is wider than "the
  last fix pass" — the rate was computed over all of it too, but say so when you
  relay it.
- `command` — the `git revert --no-commit` invocation, with FULL SHAs. Nothing has run
  it. **It can be `null` even when the proposal was made**, and then `no_command` says
  why: a merge commit inside the range (a `git revert` of a range refuses a merge
  without `-m`, and a merge is how the base branch got in there — reverting wholesale
  would undo commits no fix pass wrote); a range GitHub's compare truncated, where a
  merge past its 250-commit ceiling would be invisible so the merge count is a floor;
  or commits that could not be listed at all.
  The range is still named in both cases; it is only the paste-and-run shortcut that is
  withheld. If you see `no_command`, **do not reconstruct the command** — go and read
  `git log --oneline <range>` and decide what actually wants undoing.
- `removes` — what undoing it would take off the board: the findings this round
  attributed to it, with severities.
- `costs` — what undoing it would hand back: the complaints that pass was **sent to
  answer** and this round no longer raises.
- `still_open` — the complaints it was sent to and did not clear. Those are
  outstanding either way, so reverting costs nothing there.

**Take it to the user; do not act on it.** Reverting a pass reverts the real fixes in
it, and a pass that cleared three P2s and introduced eight P3s is a net loss to undo
wholesale — nothing in the loop knows which is which without asking, which is exactly
why this is a proposal. Read the two columns knowing they are biased in opposite
directions on purpose: the cost is an **upper bound** (matched on finding keys alone,
and under `increment` scope it includes complaints this round did not re-read) and
the benefit is a **lower bound** (`introduced` is a documented floor). A revert those
numbers still argue for is one they cannot have talked you into.

**On a rebased branch there is no proposal, and the round says so rather than going
quiet.** `revert.kind` carries the fix range's own verdict — `ok`, `no-fix`, `blind`,
`rewritten`, `not-asked` — and the last two are the case the subsection above is
about: the range that would name the offending pass is the range a rewrite removes, so
the cycle can measure a change it cannot point at. `offered: false` with a `kind` of
`blind` or `rewritten` means "we cannot see this", not "there was nothing wrong". This
is the one thing #504 does **not** give back: a round can be attributing from a
rebuilt pass and still be unable to offer a revert, because the proposal reads the
compare range rather than the reconstruction.

Two things this deliberately does **not** do, so you are not waiting for them:
revert-and-re-run as an automatic mode, and re-running the fixer with a narrower
brief instead of reverting. Both are open on #506 and both are decisions a human
takes today. The next subsection is not an exception to that — it undoes **one fix**,
never a pass.

### When a SUB-FLOOR fix caused the finding, excise it rather than repairing it (#627)

**The rule.** When a round attributes a new finding to a fix that answered a finding
**below `round_trigger_floor`** — a P3 or P4, one of the 💸 items the budget paid for —
the response is to **revert that fix**. One fix, its own hunk, which is why the fixer
brief asks for each budgeted fix to be landed as its own hunk or commit. Then:

- the sub-floor finding it answered **returns to the board as reported-and-not-fixed**,
  exactly as an unpaid budget item does — a `deferred` row with its one-line note (§4b);
- the finding it caused **disappears with it** and is not handed to a fixer, because
  there is no longer anything for a fixer to be briefed about;
- **the cycle continues.** This is not an escalation, not a stop, and not a decision you
  take to a human. It is the cheap correction that lets the round carry on, and treating
  it as a stop is the expensive reading of a cheap fact.

**Why this is safe here and not in general.** Automatic backtracking over a whole fix
pass was considered and refused, and `round_stop.revert` above is that refusal: a pass is
**mixed**, and reverting one that cleared three P2s to remove five P3s puts the P2s back.
Nothing in the loop can tell which half is which without asking, which is why that
proposal is priced and handed to you rather than executed. **A single sub-floor fix is
not a mixed pass.** It answered one finding that was, by definition, not blocking the
close, so the entire cost of removing it is one P3 or P4 returning to a state this repo's
own policy already calls reportable and non-blocking. There is nothing to weigh, and
where there is nothing to weigh there is no decision to take upstairs.

**The one case where it does not apply: a sub-floor fix a later blocking fix has built
on.** Reverting it then is not a clean excision — it takes lines a P1 or P2 fix depends
on, and undoing a blocking fix is exactly the mixed revert this rule is careful not to
be. **Report it instead of forcing it**: name the sub-floor fix, the finding attributed
to it, and the blocking fix that now rests on it, and let the round proceed normally with
the caused finding handed to a fixer like any other. A forced excision that breaks a P1
fix has converted the cheapest correction in the loop into the most expensive one.

**The round works out which fix, and publishes it — you do not have to.** `_provenance`
attributes a finding to the fix *pass*; an excision needs the individual fix, and
`round_stop.excision` is that answer:

- **`count`** — how many excisions this round names. `null` is "nobody looked" (round 1,
  a rebased range, an anchor payload whose trigger floor cannot be read, a checkout that
  could not list the pass) and `why` says which; `0` is a measured none.
- **`excise[]`** — one per fix, each carrying the `commit`, its `subject`, the
  `command` (`git revert --no-commit <sha>` — **run it**), `answered` (the sub-floor
  finding that goes back on the board unfixed: record it `deferred` with its one-line
  note, §4b) and `caused` (the findings that go away with it: hand a fixer **none** of
  them). The report lists the same thing under **Excised, not fixed**, and every caused
  row in `to_fix` is flagged `excised: true`, so a list pasted out of the report cannot
  pick one up by accident.
- **`declined[]`** — a seam it refused, with a sentence: a later commit in the pass built
  on the fix, the commit answered more than one finding, it is a merge, or the checkout
  could not be read. Those caused findings are still in the cycle and are fixed like any
  other finding. **Relay the sentence** — this is the case #627 says to report rather
  than force.
- **`seams`** and **`sub_floor`** — how many commits in the pass named exactly one
  sub-floor finding, against how many sub-floor findings the pass was sent to. `seams: 0`
  with `sub_floor` above zero means the pass left nothing to excise; that is the fixer
  brief's instruction not being followed, and it is worth a sentence to the user because
  the cheap correction was unavailable on this round as a result.
- **`floor`** — the trigger floor that decided which findings were sub-floor. It is the
  **anchor** round's, not the round you are reading, so quote it from here rather than
  from `review_panel`: a floor moved between rounds would otherwise have you naming a cut
  the classification did not use.

**The excision costs the round no budget, and its churn is not hidden.** Do not charge
it to `low_severity_fix_lines` — that budget bounds what a round may spend fixing
sub-floor findings, and this is the removal of such a fix rather than one more of them.
Its churn is still churn: the revert commit lands in the next round's fix range and every
churn reading there counts it, which is #692's unit working as intended. What comes out of
the next round's **attribution** is only the lines the revert restored, because those sat
at an earlier round's head and #559 is what stops a correction reading as the disease.

**What it does NOT price is what the excision destroys (#558).** `destroys` names the
files, the lines and how many of them sit in test or documentation paths, and that is a
line count rather than a valuation. A sub-floor fix is very often the only test over the
path it was written for — on lexray#1697 two "P3 findings return" entries were the sole
coverage of the mechanism the PR existed to build — and `answered` says nothing about
that. The rule still applies: this is Rich's decision on #621 and it is not conditioned
on a pricing that does not exist yet. But when `destroys.guard_lines` is most of the
commit, say so to the user in the same breath as the excision.

### When the range between the rounds is an integration (#278)

An integration moves the head, and a moved head used to invalidate the round that
preceded it outright — so merging `origin/main` into a branch to clear a stale base
cost a whole panel cycle across every seat, whatever the merge contained. It no
longer does. **The order is not applied blindly: what decides it is how much of the
merge is genuinely new material to this PR.** The measurement is `git diff` between
the commit the round read and the merge result, restricted to the files this PR
touches, counted in changed lines, against `review_panel.distant_merge_lines`
(default **20**; `0` admits only an empty resolution, `null` restores the old flat
behaviour where any head move is a review of earlier code).

Whenever the range carries a merge commit, the round says which reading it took, in
`config_notes`. Read it — the two are different claims about coverage and you must
never have to infer which happened:

- **`round N follows an integration and takes the DISTANT reading`** — the merge
  touched nothing this PR touches and the resolution was trivial or absent, so
  **the earlier round STANDS**. Nothing is being claimed as reviewed that was not:
  the merged code is not this PR's change and is not what the findings are about.
  A round was not required on that merge's account, and `preland`'s `review` check
  says the same thing as a WARNING rather than a HOLD.
- **`round N follows an integration and takes the INVOLVED reading`** — a real
  resolution in code this PR also touches. That resolution is unreviewed work and
  it gets reviewed — **only that part**, which is what the increment already is
  when it is pointed at the range between the round and the merge. `preland` HOLDs
  until a round has read it.

A range with **no** merge commit in it is never distant, whatever its size: that is
a push, not an integration, and unreviewed work of this PR's own kind holds at any
size. So does a range that could not be measured at all.

### What may BLOCK, as against what may be touched (#623)

Before you read the verdict, know what a finding has to BE to hold the cycle open. Three
kinds, and the fixer brief's step 2 is where they are defined and where a fixer assigns
them:

- **claim-miss** — the change does not do what it set out to do;
- **regression** — something that worked no longer works, and it has been
  **demonstrated**: a failing command, a diff of outputs, an assertion that goes red;
- **observation** — everything else, including everything that would be equally true of
  this code before the change.

**An observation is recorded, relayed, and blocks nothing**, at every severity — a P1
somebody ranked an observation at is still an observation. **Reclassifying one as a
regression takes a demonstrated observable failure, not an argument that one is
possible**; "this could break if X" is an observation with a story attached, and it is
the story that fix passes spend their lines on.

This is a different question from the floors and it does not overrule them: **the floors
(`fix_severity_floor`, `low_severity_fix_lines`) decide what may be TOUCHED, this decides
what may BLOCK.** An observation above the floor still gets fixed if the round asks for
it and the budget reaches it. What it may not do is keep the cycle running, hold a
landing, or turn a dry round into an open one on its own. On lexray#1780 the P1 that cost
a whole extra round was a claim-miss the issue had stated before a line was written —
that is the class this distinction exists to keep visible, against a round's worth of
observations priced identically to it.

Read `round_stop` from the JSON (`jq .round_stop`). It is mechanical and it is
the decision — do not substitute your own judgement, and do not ask a reviewer
whether another round is needed (that asks a model to predict findings it has not
made; one that just wrote five says yes, one that silently produced nothing says
no with complete confidence):

- **`stop: false`** — there is work outstanding: findings no earlier round raised,
  a P1/P2 still outstanding, or a finding an earlier round already raised that is
  *still* outstanding at any severity (the fixer was told and it is still there —
  and on a round the `sonarqube` seat ran, its hard-gate issues count here exactly like
  the judged ones; on a round it did not, there are none to count). Fix
  them (§4 again, with only this round's findings in the brief), then run the
  panel again as round `r+1`. Repeat until `stop` is true or the cap is reached.
- **`stop: true`** — **no further PANEL runs.** That is the whole of what it says,
  and reading it as "the cycle is done, nothing is left" is the bug #42 records: a
  capped round's findings were found, judged, posted and handed to nobody. Note
  `confident`: **false** means the stop was not convergence — a reviewer read a
  prefix of the diff, never ran, returned nothing parseable or declared a gap; the
  cap ran out; or the round had no baseline to compare against. The `veto` list says
  which. Report it as a stop, never as "clean". **Then read
  `round_stop.outstanding`** — the answer to the other question — and do what it
  says.

### What is left when the cycle ends, and who gets it (#42)

`stop` answers *should another panel run*: a question about cost, the cap and
convergence. It is not the answer to *should these findings be fixed*, which by this
command's own bar is always yes — every confirmed finding, and every SonarCloud hard-gate
issue on a round that had a `sonarqube` seat to raise one. The cap is where the two come
apart, and it is the common case: the last
round's P1/P2s, the repeats whose fix did not land, the gate issues and everything
that round newly found are all outstanding at the moment the loop is told to end.

`jq .round_stop.outstanding` and dispatch on `handed_to`. Do not substitute your own
judgement here either — it is computed from which rule stopped the cycle.

- **`handed_to: "fixer"`** — run **one** final fix pass (§4, with `fixable` as the
  brief), then **one verification pass over its commit**, and then **stop**. Do not run
  a panel over it: the cycle has ended and there is no round left to read the result.

  **The verification pass is unconditional (#629).** This path ends with a commit
  nobody reviews — that is a structural hole in the middle of a review loop, and with
  the cap at 6 it is the commit most likely to be the last thing touched before a
  hands-off merge. So it always gets one cheap read, and *cheap enough to be
  unconditional* is the requirement: anything that costs a round will be skipped, and
  then the hole stays.

  ```bash
  qb-stage V                                       # a verification pass, not a round
  ```

  - **One seat, the fix commit's own diff, one question.** Launch a single
    `general-purpose` sub-agent over `git show <the fix commit>` and ask it exactly
    that: **does this commit do what it was briefed to do, and does it break anything it
    touches.** Give it the brief the fix pass was given — the `fixable` findings it was
    sent to clear — and the commit's diff, and nothing else. No worktree, no fixing, no
    push; it reads and reports.
  - **It is explicitly NOT another round, and must not be run as one.** Do not invoke
    `panel.py` here with `--round`/`--max-rounds`/`--baseline`. A panel over the final
    fix restarts the cycle the cap just ended and would find new work by construction —
    that is the cap being raised by the agent it was there to bound. Nothing about this
    pass is recorded as a round, and it does not feed `round_stop`.
  - **If you run it through `panel.py` at all — a single seat with `--reviewers <one>`
    is a legitimate way to do it — it will be REFUSED without `--new-cycle`.** This pass
    follows a terminal verdict by construction: the cycle has just stopped, that is the
    whole reason the pass exists, and #617's gate reads the board for exactly that at
    launch (§3). So pass `--new-cycle`, and **never `--force`** — it cannot move this
    gate, and reaching for it here means the refusal was read as an obstacle rather than
    as the board correctly saying the cycle ended. The banner it prints naming the stop
    is the right header for this pass's report: it says what ended the cycle, which is
    the context the seat's answer has to be read in. The sub-agent route above touches
    `panel.py` not at all and so meets no refusal — that is a difference in mechanism,
    not a way around the gate.
  - **Its findings are not a new round's work.** A **regression** it finds — a
    demonstrated observable failure, per the block above — **blocks the landing**: it
    goes to a human or to a targeted fix at that one defect, and §7 does not offer.
    Everything else is an **observation**: it goes to the board as a row and nothing
    else, and it does not reopen the cycle.

  **Say in the relay what the commit got.** The old sentence was "the round-N fix commit
  was not itself re-reviewed", and it was honest when nothing read it. Now say that it
  was **reviewed by a verification pass, not by a round** — which is a weaker claim than
  a round and a much stronger one than nothing, and the user is entitled to know which
  of the three they got. If the verification pass could not run at all, the old sentence
  is the true one and it is the one to use. Do not run "one more round to check it"
  instead: that is the cap being raised by the agent it was there to bound.

  **It is a proposal and not an order.** If the user has said they would rather ship
  with the findings unfixed and triage them separately, that is theirs to choose —
  do that instead, and record each finding `deferred` (§4b) so nothing is lost.
- **`handed_to: "human"`** — **do not run a fixer.** The cycle ended on a futility
  rung (#84, #489, #491, #505, #554) or on an escalation, and every one of those says
  in its own `reason` that a human answers this rather than another fix pass. Sending
  the remainder to one contradicts the verdict you are relaying. Relay `why`, list
  `fixable` and `escalated`, and record the findings `deferred` (§4b) with the issue
  that carries the question.
- **`handed_to: "nobody"`** — nothing is owed. Either the round was dry, or
  everything left is under the repo's `fix_severity_floor` and the repo's own policy
  is that those are reported and not fixed here (#165). Relay `below_floor` anyway if
  it is non-empty: a policy stop that says nothing about what it held back reads as a
  dry one.
- **`handed_to: null`** — the round is going again; this is the `stop: false` bullet
  above and there is no final pass to run.

`escalated` is never a fixer's, at any of these: no fix round may touch an escalated
finding (#221). It goes to §6's **Escalated** item and to the issue that asks the
question.

**Before a `stop: false` becomes another fix pass, declare the premise that pass
will rest on.** This is the futility brake (#84), and it is yours to run — not the
fixer's — because you are the only reader with both rounds in front of you, which
is the same reason the *Match it by premise, not by key* rule below is yours. Every
round from 2 on, before you go back to §4:

```
python3 ~/.claude/loops/panel.py --premise "<one sentence: what this fix pass assumes>" \
    --pr <pr> --round <r> --premise-file /tmp/tmp.AbC123/premises.json \
    --premise-decidable yes|no \
    --premise-for <each finding key the premise explains>
```

One register per PR, in the same `mktemp -d` directory as the payloads, and pass
that path to §4's brief so the fixer can declare against the same file. It costs
nothing — no seats, no diff, no judge, no vendor call — so it runs on every fix
pass rather than on the ones you suspect.

**Build the premise with a quoted heredoc**, exactly as `review-pr.md` step 3a
does and for its reason: a premise about code carries backticks and `$(…)`, and
inside a double-quoted argument bash executes them while a `$VAR` expands to
empty and declares a premise you did not write.

**`--premise-decidable` is the question the counter cannot ask (#491).** Answer `no`
when the runtime the fix's assertion runs in cannot observe the property the fix
asserts, `yes` when it can. Omitted is *not answered*, and nothing brakes on it.

You are the right reader for this one for the same reason you are the right reader
for the declaration itself: a fixer replacing one proxy with a better one is not
being careless, it is answering the finding in front of it, and only somebody
holding all the rounds can see that the proxies keep changing while the thing being
approximated does not. **When you find yourself writing a premise that restates the
last round's premise with a different signal in it, the answer to this flag is
`no`.**

**Read the exit code.** `0` records the declaration: brief the fix pass. `4` is a
brake, and the report names which:

- `escalate_on.premise_repeated` (default `2`) — a fix has already been written
  against this premise once in this cycle, and **the second one is not to be
  written**.
- `escalate_on.premise_undecidable` (default `true`) — you answered `no`. It fires
  on the **first** declaration: an unobservable property is not going to become
  observable next round, so the cycle cannot converge on it whatever the counter
  says.

**Partition the round; do not simply drop it (#555).** Either way the findings that
premise explains become escalations under the `--escalated` rule below — relay
them, open the premise issue, and stop the cycle after this pass. The command
prints the `--escalated` keys for the round you are recording against, under
`DOWNSTREAM OF THE PREMISE`, and what you do with them is the whole of this rule:

- **The downstream findings do not get a fix pass.** That is what the brake
  refused, and it is refused whoever writes it — you may not hand them to a fixer
  under a different description.
- **The independent findings still do.** Launch §4 with **the escalated keys
  withheld from the brief**, exactly as you would brief any fix pass, then stop
  the cycle when it returns. An escalation partitions this pass; it does not
  cancel it.
- **Unless nothing is left.** If every outstanding finding is downstream of the
  premise, there is no independent half and no pass to launch — relay, open the
  issue, stop. That is a real outcome and not a failure to try.

This used to read *"do not launch §4"* full stop, and that was the same defect
#555 was filed about, one level up. The rule is that work downstream of an open
question is speculative spend — it is not that everything alongside such a
question is. On lexray#1697 the fixer made the opposite error, spending a whole
pass on findings the premise had already voided; a blanket stop here makes the
mirror-image one, dropping the findings the premise says nothing about. The
partition is what both halves of #555 are for, and this is the reader that has
it: the command prints the two halves, and you are the one holding the brief.

**Why it is here and not at the end of a round.** The cap bounds cost; this bounds
futility — it stops when the rounds have stopped being about *different things*. On
PR #299 (2026-08-21) rounds 1, 2 and 3 each found the previous round's fix reopening
one hole, patched three ways — merge parents, then same-named refs, then a local
branch — and the premise underneath all three, *that a local repository can say
where a release number LANDED*, was named at round 3 by a human. 39 of the 53
findings after round 1 were introduced by the previous fix pass; round 2 was 17 of
17. Evaluated at the end of a round instead, the brake would have fired one fix
pass and one whole panel later — which is exactly the round the rule exists to save.

**Pass `--premise-file` to the ROUND as well**, on the same path. The round reads
the register (it never writes it) and the payload then says which premises repeated
and which fix passes declared none:

```
python3 ~/.claude/loops/panel.py --pr <pr> --post --round <r> --max-rounds <N> \
    --premise-file /tmp/tmp.AbC123/premises.json \
    --baseline /tmp/tmp.AbC123/r1.json [--baseline …] \
    --json-file /tmp/tmp.AbC123/r<r>.json
```

A premise declared twice that reaches a round anyway ends the cycle there: it takes
a veto line, `confident` is false, and `round_stop.reason` names the premise. That
is the late half of the same brake — worse than stopping before the fix, better than
the cap. A premise answered `decidable: no` that reaches a round does the same, on
the same terms (#491), and the payload carries both lists under
`round_stop.premises` with `undecidable_brake` saying whether this repo armed the
second one.

**An undeclared fix pass is unescalatable, and the report says so.** If a round's
`config_notes` says the fix pass after round N declared no premise, that is a gap in
the record and not a clean one: nothing could have braked that pass, and a cycle
nobody could brake reads exactly like a cycle that did not need braking. Say which
it was in the relay.

**The honest limit.** The brake counts DECLARATIONS, and comparing declarations is
all it does — it does not infer a premise from the findings, deliberately (#84: "the
cheap version is to have the fixer declare the premise and compare declarations …
treat an undeclared fix as unescalatable rather than pretending to infer"). Two
consequences you carry, not the loop: the same premise stated through two different
proxies — `rc == 0` one round, an artefact's existence the next — shares almost no
words and is counted as two premises, so **state the premise, never the proxy**; and
on #299 the fixers escalated zero times across five rounds, so a brake waiting for
someone to volunteer a declaration would not have fired either. Running it every
round is what makes the count real.

**An escalation ends the fix half of the cycle for that finding — tell the loop,
with `--escalated`.** Pass the key on the round you learn of it — the fixer gave you
a finding ID, so map it through §4b's `jq` first; this flag takes keys and nothing
else:

```
python3 ~/.claude/loops/panel.py --pr <pr> --post --round <r> --max-rounds <N> \
    --escalated <the key the fixer's escalated ID maps to> \
    --escalated-from-board \
    --narrowed <the key each of the fixer's NARROWED IDs maps to> \
    --declined <key>:<budget|premise|scope|refuted> \
    --baseline /tmp/tmp.AbC123/r1.json [--baseline …] \
    --json-file /tmp/tmp.AbC123/r<r>.json
```

**Pass `--escalated-from-board` on every round of a cycle.** It adds the keys the
BOARD already knows are waiting on a human — the findings a seat flagged
`needs_human` (#279), published as `needs_human_keys` — to whatever you named by
hand. The two lists are different and neither contains the other: the board knows
what an earlier round declared, you know what the fix pass just decided. It costs
one read and it closes the failure mode this whole section is written around,
which is a key that only ever existed in prose a fixer wrote and somebody had to
transcribe. Over thirty days and sixty-five rounds, `by_outcome.deferred` was 0 —
the escalation path was documented at length here and never once exercised.

Without it the cycle jams, and the jam is the mechanism defeating itself: the
finding is outstanding (correctly), no fixer may touch it (correctly), so
`round_stop` returned `stop: false` every round until the cap — the thing built to
stop a loop circling a premise guaranteed it ran to the cap. With it, the key is
subtracted from the work a fix round can clear: the cycle goes again exactly while
there is other work, and stops as soon as only escalations remain. The finding
stays in the report, marked ⛔. `round_stop`'s docstring
(`harness/loops/panel_rounds.py`) is where the rule and its limits are kept; what
follows is only what YOU have to do.

**Pass each key once — and pass a NEW key when the premise comes back under one.**
A key rides in the payload as `escalated: {key: round}` and every later round
inherits it through `--baseline`, so a cycle cannot lose the question by forgetting
a flag, and re-passing a key you inherited is harmless (the round it was FIRST
declared in survives, a re-declaration cannot re-date the claim, and a repeated
flag is deduplicated rather than noted twice). Pass it **with the round flags** —
`--escalated` without `--round`/`--max-rounds`/`--baseline` is refused, because it
names work a later round must not count and a single-pass review has no later
round. What inheritance
cannot do is follow a premise into a different key, and §5 below is the case where
that happens: a fresh panel over the same code very often words the same premise
differently, which mints a new `finding_key`. Nothing mechanical connects the two.
So when §5's re-read finds a premise you have already relayed wearing a new key,
add that key to `--escalated` as well — otherwise rule 1 fires on it as brand-new
work, it reaches a fixer with no ⛔ mark, and the cycle runs to the cap on a
finding no fixer may patch. Escalating by premise is yours; the loop only knows
keys.

**`--narrowed` is the same argument and the opposite meaning (#615).** It takes finding
keys, in the same shape (8-64 hex, repeatable, duplicates deduplicated), mapped the same
way — the fixer's `Narrowed` block gives IDs, §4b's `jq` gives the keys. **Everything
about the flag's SHAPE is `--escalated`'s; everything about its MEANING is `fixed`'s**,
and conflating those is the one misreading it can produce. An escalated finding is work
no fix pass may do and a human owes an answer on. A narrowed finding is real, was
**fixed** at the point it was raised, and only the general form was declined.

So it **CLEARS**, where an escalation merely stops counting. The key is subtracted before
the round's four rules, so the finding is not outstanding, rule 3 does not hold the cycle
open on it, and no veto line is owed for it — `round_stop.narrowed` publishes the keys
that were actually honoured. **Without the flag, the fourth outcome reaches nothing**: the
fixer declares `narrowed`, you record the row, and the round still counts the finding as
outstanding and goes again — which puts back the whole pressure to write the class-wide
fix, wearing the cycle instead of the brief. Three things about it that are NOT
`--escalated`'s:

- **It is NOT inherited through `--baseline`, so pass it on the round that follows the
  fix pass that declared it — and only that round.** An escalation is open until a person
  closes it, so forgetting it between rounds puts the work straight back; a narrowing is
  **discharged the moment it is honoured**, because the finding it names was fixed. There
  is nothing for a later round to carry, and a register of it would be a record of
  decisions already spent. Do not chase a narrowed key into later rounds the way you
  chase a premise.
- **A SonarCloud hard-gate issue cannot be narrowed**, at any severity. `round_stop`
  subtracts the exempt set before honouring anything you pass, so naming one is a no-op
  rather than an error. Narrowing is a judgement about how far to fix a judged finding; a
  red quality gate is not a judgement, and it keeps the PR unmergeable whatever anybody
  says about the general form.
- **A malformed key fails SAFE but quietly — read `config_notes`.** An ignored narrowing
  leaves the finding outstanding and the cycle goes again, which is the harmless
  direction; what it does not do is announce itself, so a caller who believes it declared
  a narrowing watches the round it meant to end run anyway with nothing on screen saying
  why. The note is there. An ID or a title in place of a key is the usual cause.

**`--declined` is the third register, and it is the one that stops the loop paying
twice (#665).** A fix pass that identifies a correction and does not make it is
already recording that in §4b — a `deferred` row under roads (1) or (2), or a
`refuted` one. Until now that record went nowhere the loop could read: on a real
cycle a pass declared two corrections it could not pay for under the growth
ceiling, and the round after it spent its own budget rediscovering one of them and
reported it as a fresh finding. Pass the same keys forward:

```
    --declined <key>:budget      # a ceiling the fix did not fit under
    --declined <key>:premise     # an assumption the pass could not decide
    --declined <key>:scope       # a repair that would open files this change never touched
    --declined <key>:refuted     # the pass disagreeing with the finding on the merits
```

Same argument shape as `--escalated` — keys from §4b's `jq`, repeatable,
deduplicated, refused outside a cycle — and it **is** inherited through
`--baseline`, so pass a key on the round after the pass that declined it and let
the register carry it from there. The reason word is not decoration: it is what
the next round's reader is briefed off, and "priced out" and "I think this finding
is wrong" call for opposite next moves. A word this loop does not recognise is
recorded as `unstated` and named in `config_notes` — the declaration survives, the
adjective does not.

**It loosens NOTHING, which is the point of it.** The finding stays outstanding,
stays counted at every one of `round_stop`'s four rules, stays a fix pass's work,
and still buys another round exactly as it did — this is not a second
`--escalated`. What it changes is what the round can CLAIM: a finding matching an
inherited declaration is marked 🧾 and reported `new_this_round: false`, because an
earlier round already raised it and calling it news overstates what this round
found; and a cycle that ENDS with declarations on the record takes a veto line and
cannot report `converged: true`. Landing with a known-unfixed defect is allowed;
landing with one while calling the cycle clean is not.

**The register does not retract, and a `refuted` declaration is an argument you
should have.** There is no un-decline: a correction genuinely made in a later pass
goes on costing the cycle its `converged` until the cycle ends, which is the
conservative direction and #617's `--new-cycle` is the clean start for a PR that
has actually had the work done. And a pass that declines everything buys itself
nothing — every declaration is a veto against the cycle it is in, so the flag
cannot be gamed in the direction the fixer would want.

**A cycle that ends holding declarations reaches the board.** The round that ends
it posts one `needs-human` `decision` naming the keys, their rounds and their
reasons, so the PR lands with its known-unfixed defects named rather than with a
`config_notes` line nobody reads. Its answers are a person's: raise the ceiling,
widen the scope, argue with the fixer, or accept the defect.

**A stop that is HOLDING an escalation is never convergence — and that is
narrower than "the cycle can never converge with a question open".** When the
round that stops still raises the escalated finding, the stop takes a veto line,
`confident` is false, and the reason says a human is owed an answer. When it does
not raise it — a round under `--scope increment` reviewing only the fix commit,
or a round whose fresh panel gave the premise a new key — that round is genuinely
dry and is reported `confident: true` with the question still open. `confident`
is a claim about the ROUND, never a claim that the PR has nothing outstanding.
What tracks the open premise across the cycle is your relay and its issue (§4b),
which is where the human is looking for it.

**When a human ANSWERS the premise, the cycle is over — start a fresh one.** The
register only grows: there is no un-escalate, and no way to drop one key without
throwing away the whole baseline. So an answered premise's key would go on
subtracting its finding from the work a fix round can clear, and go on rendering
⛔, for every round that inherits the baseline. Take the answer as the end of this
cycle, land the work it calls for, and open a new cycle over the result.

**The honest limit, and the reason this is a flag rather than a detector.** The
loop is taking your word for it, and you read that word out of a fixer's prose —
so the agent whose fix pass produced the finding is, one step removed, the agent
ending the cycle over it. That is the signal #67's own evidence says cannot be
self-reported. The key and its round are recorded so the claim is auditable after
the fact, and the cap still binds; nothing here detects a premise on its own
(#67's first piece, still unbuilt). Do not escalate to end a cycle you find
tedious — that is not a loophole, it is the one way to make this number lie.

**A key that names nothing, or is not a key at all, is reported.** A value that is
not 8-64 hex characters is rejected outright, and a well-formed key matching no
finding this cycle has ever seen lands in `config_notes` saying so. Both are said
out loud because the failure they would otherwise cause is invisible: the loop
carries on counting a finding you believe you excluded. A round the panel SKIPPED
(a merge-title match) records no new key at all — it reviewed nothing — and says
which key it dropped; pass it again on the next round that runs.

- **Never re-brief an escalated finding to a fixer.** Not this round, not a later
  one. It goes to the human with the write-up the fixer produced (§6). The ⛔ mark
  in the panel's own **To fix** list — and in its **SonarCloud issues** list where that
  seat ran — is there so a brief built from either cannot include it by accident.
- **Match it by premise, not by key.** The next round is a fresh panel over the
  same code, so it will very likely report the same premise defect again — with a
  **new** `finding_key`, at a different line, in different words, and nothing
  mechanical will connect the two (`superseded_by` records the opposite direction:
  a finding a *later* one replaced). You are the only reader who has both, because
  you ran both rounds. So before briefing a round's **To fix** list, read it
  against every escalation you have already relayed and pull out anything that is
  the same premise wearing a new key. It does not go in the brief, and it **does**
  go into the next round's `--escalated` under its new key — inheritance follows
  keys, not premises, so nothing else will hold it. It **does** get
  its own `deferred` row, naming the same premise issue in `deferred_to` — it is a
  real finding nobody fixed, and a key recorded nowhere is the gap §4b exists to
  close. Two rows do not double-count one premise: only `fixed` and `refuted` are
  in the precision ratio (`OUTCOMES_SCORED`, `app/api/reviews.py`), so `deferred`
  says what happened without scoring anyone. One premise is still one open
  question — it lives in the issue and in the relay, re-stated under `Escalated`
  as still open, naming the round that first raised it and the key this round gave
  it. If you genuinely cannot tell whether it is the same premise, say that in the
  relay and leave it out of the brief: a premise question asked twice costs a
  paragraph, and a premise question patched costs the round.
- **The rest of the cycle carries on.** Findings that WERE fixed still get their
  re-review round, and `round_stop` still decides that — you are not overruling it
  for them, only declining to send one finding back through a pass that has
  already been tried on it.
- **If the escalated premise is what most of the round hangs off, stop the cycle**
  and say so. Another round would review code whose shape is the open question,
  and #67's whole observation is that this is where the loop spends the most for
  the least.
- **`--ask` is evidence, not the decision.** A premise that survives a challenge
  may still be the wrong premise, and the seats were never asked whether the
  redesign is worth its cost. `holds` is not permission to go back and patch.

Two things this must NOT do:
- **Never let a fix ride out unreviewed silently.** If the last fix pass changed
  anything and no round read it, say so in the relay: "the round-N fix commit was
  not itself re-reviewed". This used to be written for the cap alone and assumed a
  fix pass had happened at round N — at the cap, none had, which is how #42's hole
  stayed invisible. It covers the round-N pass that no round followed, and it is the
  sentence to use whenever nothing at all read a fix. The final pass
  `outstanding.handed_to: "fixer"` asks for is the one case that now has an answer:
  it gets the verification pass above, so the honest line there is that the commit was
  **reviewed by a verification pass, not by a round** — say that, not this, and never
  the other way round.
- **Never re-run a round to get a nicer answer.** Each panel run is recorded on
  the board as an observation; re-rolling one corrupts the record it exists to be.
- **Never `--force` past a refusal to keep the cycle moving.** A refused round is
  the panel declining to manufacture work, and a forced one hands the fixer findings
  about code that is already in the base branch — the failure mode in full, with the
  check bypassed on the way. It is the user's call, and it is recorded either way.

## 6. Relay the result

Show the sub-agent's summary table verbatim. Then state plainly: that the panel
summary was posted as a PR comment, the branch it pushed to, whether the
**SonarCloud hard gate** is now clear **or that there was no such gate on this cycle**,
whether all checks passed, anything flagged
**unverified**, and any reviewers the panel skipped. If the panel ran on a
hand-picked set rather than the repo's configured one, say which reviewers ran —
the fixer's bar is only as good as the review that fed it. If the sub-agent
stopped early, report exactly where and why.

**"The gate is clear" and "there was no gate" are different sentences and only one of
them may be said.** With `sonarqube` off — this repo's setting, and the fleet's while the
convergence work is proven — the second is the true one, and reporting a clear gate off
an empty list is a claim about a check nobody ran. Say it once and without alarm: a seat
the repo configured off is a decision, not a coverage gap (§3). A seat that was
configured on and failed to run is the other thing, and that one belongs in the
**Coverage** item below with what it cost.

Then the part that is new, and is the point of running more than one round:

- **Rounds:** how many panel/fix cycles ran, what each round found that the last
  had not, and **what stopped it** — a dry round or the round cap. One line per
  round.
- **Whether this cycle stepped past an earlier one (#617):** if the run was refused
  because the board held a terminal verdict on this PR, say so and stop — that refusal is
  the answer, not an obstacle. If it went ahead on `--new-cycle`, lead with what the
  banner said: which round stopped the last cycle, its reason, how long ago, whether it
  was convergence, and whether the branch has moved since. **"The tool chose to run" and
  "a caller overrode the tool" must never read alike**, which is the same rule
  `preflight.would_have` keeps for `--force` — and here it matters more, because the
  thing being stepped past is a verdict about this very PR.
- **Was the stop earned?** If `confident` is false, say so in those words and
  list the vetoes. A reader must never have to infer that a "no new findings"
  round had a reviewer reading half the diff. This is not only a line in the
  relay: §7 blocks the offer to land on it.
- **Coverage:** per reviewer, anything it declared it could not assess, any
  reviewer the panel truncated (with the budget that cut it), and **any seat the repo
  configured ON that did not run** — a missing CLI, a dead login, a crash — with what its
  absence cost the round. A seat configured OFF does not belong here: it is
  configuration, it was reported once above, and repeating it every round as a coverage
  gap is how a reader learns to skim the line that matters. If reviewers
  disagreed — one clean, one "could not assess X" — say so, and give the master's
  `coverage_note` if it ruled on the split. That disagreement is more informative
  than either verdict on its own.
- **Flagged for re-review:** findings whose reporter said the FIX needs re-reading,
  and whether the following round did find something there.
- **Wall clock:** each round's `timing` block, one line per round: the round's
  total, how it split across `setup` / `seats` / `judge` / `wrapup`, which seat was
  slowest, and `gated_ms` — how long the round sat on that one seat with every
  other seat finished and its findings undelivered. Then the fix phase between the
  rounds, from `timing.fix`. Say `source` with it: `payload` is measured end to
  end, `commits` is a lower bound derived from the two rounds' head commit times,
  and a `null` with a note is not a fast fix phase, it is an unmeasured one.
  Report the numbers; do not act on them here. **This cycle is where the evidence
  for or against "the fixer is the slow part" is produced** (#192), and until
  several cycles have produced it the answer is a hunch — the panel's own hunch
  had the judge and the gating wait folded into a phase nobody had measured.
- **What the cycle left behind, and who got it (#42):** `round_stop.outstanding`,
  every time the cycle ended — **including when the answer is nothing**. Say
  `handed_to`, the counts in `fixable` / `escalated` / `below_floor`, and what you
  then did about it: a final fix pass ran and **what read its commit** — the
  verification pass (§5), with what it found, or nothing at all if it could not run —
  or the remainder went to a human, or nothing was owed. This is the one line that
  distinguishes "the panel converged" from "the panel ran out of rounds with eleven
  findings still open", and until #42 the relay could not tell a reader which had
  happened.
- **Narrowed (#615):** every finding the fixer answered at the point it was raised, with
  **the general form it did not write**, in the fixer's own line. Give the count and the
  lines; do not fold them into the fixed count. It is a fix, so the temptation to relay
  it as one is real, and the sentence that gets lost in the rounding is the only reason
  the outcome exists. Say `Narrowed: none` when there were none.
- **Consumers (#616):** the fixer's **Consumers** column arrives in the table you show
  verbatim, so do not restate it. Say the two things reading the column tells you and the
  table does not: any finding whose line came back `unknown`, and — for a finding the
  fixer **refuted** — that the line and the refutation are the same sentence, which is the
  column working. A finding fixed against a consumer set the line does not cover is the
  failure it was built for, and it is worth a sentence of its own.
- **Fix surface (#619):** `fix_surface` from the last round — how many files the fix
  passes touched that no earlier round had read, and which ones. Say it even when it is
  zero, and pair it with whatever the fixer declared in its summary's **Surface** line;
  the two should agree, and a disagreement is worth a sentence of its own. It gates
  nothing, so this is a report and not a verdict — but a P1 in a file that entered the PR
  through a fix pass is the shape both halves of this exist to make visible.
- **Budget spend (#622):** `fix_budget` from each round that measured one. Report
  `within: true` as what it is — the pass was shown from outside to fit inside the round's
  budget — and `within: false` as what it is not: the budget could not be shown to have
  been kept, which on a round that cleared a P1 says nothing about the 💸 band at all.
  Where the fixer's own summary counted a spend and this priced a different one, say both
  numbers; that disagreement is the entire reason the count moved out of the fixer.
  `breach: true` is the one reading here that is an accusation, and it is the only one:
  say what the pass priced, what the budget was, and that every finding in the prior
  round's **To fix** list was budgeted — that last clause is what makes the number
  binding, and a reader given the accusation without it has been asked to take it on
  trust. Note also what it does NOT cover: the rung needs the round to be going again,
  which a wholly-budgeted prior list normally prevents, so it enforces an exceptional
  lifecycle (a Sonar hard-gate issue keeping the cycle alive, or a cycle continued by
  hand) rather than the ordinary budgeted-fix path. Do not report a `null` here as
  evidence that a pass stayed inside its budget.
- **A sub-floor fix excised (#627):** `round_stop.excision` from each round that named
  one. Say which fix (the commit and its subject), which finding it answered — now back
  on the board as reported-and-not-fixed — and which findings went away with it, plus
  what `destroys` says came out with it and that its worth is unpriced (#558). Say it too
  when the excision was **declined**, with the sentence `declined[].why` gives: a
  blocking fix built on it, the commit answered more than one finding, or the checkout
  could not be read. That is the case where a caused finding is still in the cycle and a
  reader needs to know why. A `count: null` is not evidence that nothing was excisable —
  it is the round saying nobody could look.
- **A revert proposed (#506):** if `round_stop.revert.offered` is true, relay it as
  a decision the user has to take, not as a footnote — the commit range, what
  reverting it would remove, what it would cost, and that nothing has run it. If the
  cycle ended on `fix_injection` and `offered` is false, say why: `revert.kind`
  names it, and `blind` means a rewrite between rounds took away the range that would
  have identified the pass.
- **Escalated:** any finding a fixer reported as the approach being wrong rather
  than the code, with its premise, what it explains, what removing it would cost,
  and its `--ask` verdict if one was run. Say it even when the answer is none. This
  is the one item in the relay that is a question rather than a report: it is
  outstanding until a human answers it, and no round will close it. Having relayed
  it, open the issue that **asks** it — the fixer's five fields, no answer picked —
  and then record the finding `deferred` with that issue in `deferred_to` (§4b),
  which is the step §4b deferred to here.

## 7. The pre-land verdict, and the offer to land

**This step runs at the end of every cycle, not only when the user asks to
merge.** The rounds you just ran computed whether this PR is in a landable state;
stopping without saying so throws that away and leaves the user to know to ask.
So: run the gate, report its verdict whatever it is, and offer to land **only on
READY**.

This step used to be one line with nothing in front of it, and the PR that exposed
that (#131) was merged on `mergeable` + CI-green over its own panel round — 8 P1s
and 12 P2s outstanding at the moment it landed, on `main`, for three hours, two of
them auth-shaped.

```bash
python3 ~/.claude/loops/preland.py --pr <pr> --require-earned-stop --json
```

If that path does not exist, the box's `~/.claude/loops` predates the script — run
`python3 harness/loops/preland.py --pr <pr> --repo . --require-earned-stop --json`
from a checkout instead. A missing gate is not a passed one, and "the gate would
not run, so I offered" is this step's failure arriving through the step itself.

`--json` because you are going to read `verdict`, `reasons`, `actions`,
`warnings` and `checks` out of it rather than paraphrase a report. **The verdict
is the decision** — act on it, and never substitute your own reading of the same
facts for it. A READY you talk yourself past and a HOLD you talk yourself through
are the same failure in two directions.

`--require-earned-stop` is what wires §5's `confident` into the landing decision.
An unearned stop — a reviewer read a prefix of the diff, never ran, returned
nothing parseable, or the round cap ran out — is a HOLD input in its own right,
and without the flag preland reports it as a warning for a human to weigh, which
on this path is nobody. The flag is not the default because a headless box with
two permanently-absent seats could never reach a green verdict; here the round is
one **you just ran**, so an unearned stop is this cycle saying nobody read the
whole diff, and an offer to land resting on that is an offer resting on nothing.

**Cross-check it against the round payload you already have.** Read
`jq '.round_stop.confident' /tmp/tmp.AbC123/r<r>.json` from the last round of §5.
If that is `false`, do not offer — whatever the gate said. The gate reads the
round **as the board recorded it**, and a board that never took the round, or an
API too old to store the field, leaves `stop_confident` null rather than false;
null is not a stop that was earned, it is a question nobody answered. Two sources
disagreeing is itself worth a line in the report.

**And cross-check the verification pass, if this cycle ran one (§5).** A **regression**
it found — a demonstrated observable failure, not an argument that one is possible —
**blocks the landing**, whatever preland says: the gate reads rounds, and the
verification pass is deliberately not one, so it is invisible there and this is the only
place it can be applied. Report it and do not offer; the repair is a targeted fix at that
one defect, or a human. Anything else the pass found is an observation, goes to the board
as a row, and does not block.

### READY — offer, and say what the offer rests on

State, before the offer and in this order:

- **What was checked.** Name the checks from `checks` and their statuses,
  including any that read `skipped-flag` or `skipped-disabled` — a gate that
  passed with `review` turned off is a materially weaker claim than one that
  passed with it on, and the verdict alone cannot tell them apart.
- **Which round the gate ruled on**: `checks.review.detail` gives the round
  number, its cycle, and the head it read.
- **What moved since the review.** `checks.review.detail.head_sha` against the
  payload's top-level `head_sha`: equal means the round read exactly this commit
  and nothing has been pushed since. If they differ the gate has already HELD, so
  on a READY the honest sentence is that nothing moved — say it, because "the
  review is of this code" is the thing the reader most needs and cannot see.
- **Anything in `warnings`.** A READY with warnings is still a READY; a READY
  reported as though it had none is a different PR from the one on screen.

- **Whether the branch carries its release note.** preland has no check for this
  and never returns RECONCILE for it, so it is asked here or it is not asked:

  ```bash
  python3 scripts/changelog_fragments.py required --onto origin/<base> --branch HEAD
  ```

  A branch that ships something writes `changelog.d/<issue>.<kind>.md` and nothing
  else. There is no number to ask about and nothing here to stamp: the number is
  applied on the base after the merge, by `scripts/release.py run`, once per batch
  (#122). `scripts/release_stamp.py` no longer exists, and a document that tells
  you to run it is stale.

  The repair for an exit 2 is to write the fragment — never to write in
  `CHANGELOG.md`, which no longer counts for this check and is refused separately
  by `pre-push` and by CI.

Then **offer, and stop**. `Land it?` — and wait for an answer. A verdict is not
consent: the user asked for a review, and the merge is a second decision that is
theirs. Do not merge because READY looks like permission.

### RECONCILE — do the mechanical work, re-verify, then offer

`actions` holds the exact commands and the files each one touches. Run them **in
order, verbatim**, commit what they produce (they deliberately do not commit for
you), and push. Those commits are mechanical — a `down_revision` line, a version
counter, a generated merge migration — so they go to no fixer and need no brief,
which is what makes this path something the skill may take on its own rather than
hand back.

**Do only what is mechanical, and nothing that is not.** Never override the
reconciler's choice of action: relink versus merge turns on guards you are not
re-deciding. If an action needs a judgement — a `git merge` that conflicts
anywhere that is not mechanically obvious — that is where this path ends. Report
it as unresolved and do not offer. Resolving product code by guess is the
judgement this loop must not make on its own.

**Then re-anchor the review, and only then run the gate again.** That push moved
the head past the round §5 read, and preland gates on the round having read *this*
commit — so a gate run straight after it HOLDs on `head_sha`, correctly, and no
amount of re-running clears it. What clears it is a round at the new head: §5's
command again at `--round <r+1>`, with every earlier round still passed as
`--baseline`. Raise `--max-rounds` if the cap is already spent, and if you will
not, say so and offer nothing.

That round will normally be dry, and it is not there to find anything. It is
there because a mechanical commit is still a commit no round has read, and this
file's whole argument is that a fix nobody re-reviewed is a fix nobody reviewed.
It is affordable here for the same reason §5 is: this skill already runs rounds,
which is what makes the RECONCILE path available to it and not to a loop that
reviews once.

Then run the gate, with the same flags, and let it decide. Re-running it is not
optional either — the push restarted CI, so the earlier green is a statement about
a commit that is no longer the head. If it comes back READY, continue at the READY
branch above, and report this as what it is: a RECONCILE that was reconciled,
naming what you ran and the round that re-read it, rather than as a PR that was
READY all along.

### HOLD — do not offer

Report `reasons` **verbatim**, every one of them, and stop. No offer, no
"shall I merge anyway", not even if the user asked for a merge at the top of the
run: someone asking for a merge is asking for the merge they think they are
getting, and the reasons are what tells them which one this is. Then say what
would clear each one and who has to do it.

Do **not** clear a HOLD by re-running with the offending check turned off.
`--skip` and `.harness-rules` exist for repos that genuinely lack a guardrail, not
for a verdict you dislike.

### Merging, once the user has said yes

**`fix-and-land.md`'s *The hazards* section is what to read when a step here reads as
a failure** — one copy of it, deliberately, rather than a second that drifts. It covers
`--delete-branch` failing its cleanup from a worktree after the merge has already
landed, a PR body whose "does not close #N" closes #N, impossible test failures that
are a concurrent pytest rather than the PR, the two refusals that meet a lander trying
to undo a change, and which of those already have a guard. None of it is specific to
the autonomous loop: the worktree, the box and the GitHub are the same.

Claim the base, re-verify, merge. In that order:

```bash
qb-claim branch <base> --ttl 1800 --note "landing PR #<pr>" --json  # exit 1 = held
python3 ~/.claude/loops/preland.py --pr <pr> --require-earned-stop --json \
    --claim-holder "<the holder from that answer>"                 # must be READY
gh pr merge <pr> --merge --delete-branch
qb-release issue <n>                                               # the issue the PR closes
```

- **`--ttl 1800`, not the board's hour.** Keying on the base widened what a leaked claim costs —
  it blocks every merge onto that base rather than one branch's — and the TTL is the only backstop
  if this session ends between the claim and the merge. Half an hour is well past any land.
- **The branch claimed is `<base>`, not `<branch>`** — the branch being landed
  ONTO. #318: two agents landing two *different* PRs into `main` hold
  `<repo>:feat/a` and `<repo>:feat/b` under a head key, never see each other, and
  both merge, which is the incident the claim was written for. The base is what a
  simultaneous merge collides on, and it is what `preland.py`'s `merge_claim`
  check reads, so `--claim-holder` below only excludes your own claim if the two
  name the same key.
- **`qb-claim` exit 1** means another agent is already landing onto this base.
  Stop and say who holds it; two agents accepting the same offer at the same
  moment is what the claim exists to prevent, and it is the only thing between
  them. Exit 2 is "cannot tell" — a board outage, a rotated token — and whether to
  land without the serialisation is the user's call, so ask rather than deciding
  for them.
- **Re-run the gate after claiming**, because time passed between the offer and
  the yes: CI can have gone red and the head can have moved. `--claim-holder`
  takes the `holder` field out of `qb-claim --json`, so your own claim is not read
  as somebody else's. Anything but READY here ends the sequence: report the new
  verdict, and say that you hold the claim and did not merge, so nobody reads a
  live claim as a landing in progress. It carries a TTL and lapses on its own.
- **There is no release step here, and its absence is the fix.** This sequence
  used to assemble the fragments and stamp a number before the merge, and the
  commit that produced moved the head past the round §5 read — so the gate had
  just verified a commit that was no longer the head, and every other branch in
  flight now conflicted with this one on the same two files. Three of six open
  pull requests were `CONFLICTING` that way on 2026-08-23; PR #398 landed both
  ways and settled it (#122). Nothing is pushed between the gate and the merge
  now, so the READY above describes the commit that actually lands.
- **Cutting the release is a separate, later act.** When a batch is done, on the
  base branch: `scripts/release.py run --title "<what this release does>"`, or the
  **Cut a release** workflow. It assembles every fragment, derives the number,
  writes both files and tags the commit — once per batch, not once per PR. It
  refuses anywhere but the base branch, so there is no version of this you can do
  from here.
- **If the PR was in the merge queue, stand its entry down once the merge lands**:
  `merge_queue_leave(pr=<pr>, base="<base>", reason="merged")`. This command does not enqueue —
  `/fix-and-land` does — so there is usually nothing to leave, and the call is a no-op that says
  `left: false` rather than an error. But a PR this session merged on somebody else's behalf
  leaves every PR behind it in the line correctly waiting for a land that already happened, and
  any agent may retire any entry precisely so that whoever notices can fix it.
- **`--merge`, never `--squash`.** Preserve the commits: the fix commits and the
  rounds that reviewed them are the record of this cycle, and a squash throws away
  the correspondence between them.
- **Hand back the issue claim once the merge lands** (#337): `qb-release issue <n>`,
  where `<n>` is the issue the PR closes. `create-worktree` took a `kind=work` claim
  on it at checkout, held by the machine with an 8h TTL, and merging a PR does not
  touch it — on 2026-08-22 four issues were still claimed hours after their PRs had
  merged, one of them shipped as v2.78. Nothing breaks if you forget: the teardown
  releases it too, and the TTL is under both. What it costs is a slot — under
  `in_flight.max` the count is highest right after the fleet has been most
  productive, which is the wrong way round. Exit 0 also means "already handed back",
  so running it twice is free.

**The rounds you just ran are an input to that verdict, not a substitute for it.**
preland reads the round the panel *recorded on the board*, so a round that never
got there — no `qb` on this host, a board outage, `--no-record` — reads as never
reviewed and HOLDs. That is deliberate: a review nobody can point to afterwards
is not evidence the review happened.
