# Panel Review and Fix PR

@description Like /review-pr, but the findings come from the multi-reviewer PANEL (Claude + Codex + Antigravity + master judge + SonarCloud hard gate) instead of one sub-agent reviewer. Ensures a PR exists, runs ~/.claude/loops/panel.py (which comments the summary on the PR), then a sub-agent fixes every master-confirmed finding boil-the-ocean style and pushes — and the panel then RE-REVIEWS that fix commit, which is the round nobody used to run. Give it several PR numbers and each one is reviewed+fixed by its own sub-agent, in parallel. Panel members default to the repo's .harness-rules and can be named explicitly. Merging stays opt-in.
@arguments $ARGS: [pr ...] [repo] [--reviewers a,b] [--rounds N|--loop]  (defaults: the current branch's open PR in the cwd's repo, the repo's configured reviewers, and 2 rounds)

You are the **ORCHESTRATOR**. This is `/review-pr` with the panel as the
finding engine: the panel finds, an autonomous sub-agent fixes everything to the
exact same bar. The only difference from `/review-pr` is *who reviews* — so
reuse its fixer brief rather than re-inventing it.

**The cycle is panel → fix → panel, not panel → fix.** One round leaves the
fixer's own commit read by nobody: the panel saw the diff as it was BEFORE the
fix, and structural fixes beget new interactions that did not exist until the fix
was written — a mirror added in one file creating dual-keyed nodes that an early
`return` in another leaves half-stale is not a defect any earlier round could
have found. Round 2 is the default and is not optional; `--rounds N` / `--loop`
buys more.

## 1. Ensure a PR exists (the panel diffs via `gh pr diff`, so a PR is required)

- Parse `$ARGS` in two passes. **First consume every `--flag` together with its
  value** (`--rounds 3`, `--reviewers codex,antigravity`) and remove both from the
  string. **Then** read what is left: every integer is a PR number — `12`, `#12`,
  `12,14` and `12 14 19` all parse — and an optional remaining non-numeric word is
  the repo. Order matters, and doing it the other way round is wrong for every
  flag, not just one: `--rounds 3` reviews PR #3, and `--reviewers codex` eats
  `codex` as the repo name.
- **Rounds:** `--rounds N` caps the panel/fix cycles (default **2**); `--loop` is
  `--rounds 5`. `--rounds 1` restores the old fix-and-don't-look behaviour and
  should be used only when someone explicitly asks for it — say in the relay that
  the fix went unreviewed.
- **PR number(s) given** → use them, in the order given.
- **No PR number** → resolve the current branch's open PR
  (`gh pr view --json number,baseRefName,headRefName`).
- **No PR yet** → push the branch and open one
  (`git push -u <remote> HEAD` then `gh pr create --fill`), then use it. (This
  is the agreed behaviour — auto-create rather than stop.)
- **No repo enrolment is needed.** The panel is read-only and resolves the repo
  from the checkout you are in; a repo with no `.harness-rules` runs on safe
  defaults (Claude + Codex, SonarQube off). Never stop for an "unconfigured repo".

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
- Each sub-agent returns its own panel summary (findings, SonarCloud gate,
  skipped reviewers, coverage declared) **and** the §4 fixer summary table for
  its PR **and** its §5 round log: rounds run, what each found that the last had
  not, what stopped it, and whether that stop was earned.
- **One PR failing does not stop the others.** A sub-agent that cannot resolve
  its PR, or whose panel or verification fails, reports that and returns; the
  remaining agents run to completion regardless.

Then relay per §6: one table per PR, plus a one-line-per-PR roll-up (PR ·
findings · all fixed? · rounds and what stopped them · SonarCloud gate ·
pushed?) so the batch is readable at a glance. A PR whose cycle stopped
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
python3 ~/.claude/loops/panel.py --pr <pr> --post --round 1 --max-rounds <N> \
    --json-file /tmp/tmp.AbC123/r1.json          # ← the path mktemp -d printed
```

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

**Panel members** default to the repo's `.harness-rules`; pass no `--reviewers`
unless the user named who should review ("just codex", "codex and antigravity"), then
add `--reviewers <comma-list>` from `claude`, `codex`, `antigravity`, `sonarqube`. It
replaces the configured set rather than filtering it, so a named reviewer runs
even where the rules disable it. Fewer reviewers means thinner coverage feeding
the fixer — surface that in §6 rather than letting a one-vendor review read
like a full panel.

From its output collect:
- **To fix** — the master-confirmed findings (any reviewer count, P1–P4).
- **SonarCloud** — the hard-gate issues (these MUST end up resolved).
- **Skipped reviewers** — note them; a skipped Codex/Sonar means thinner
  coverage, surface it.
- **Coverage declared** — per reviewer, what it said it could not assess, plus
  any reviewer the panel reports as truncated. These are what separate "clean"
  from "I could not tell"; carry them to §7 rather than dropping them.
- **Rounds** — the `**Rounds:**` line (also `round_stop` in the JSON): whether
  the cycle should go again and whether stopping would be convergence.

The run also records itself on the quarterback board (which models ran, what each
raised, and how the judge ruled on it) so the fleet accumulates an answer to
"which reviewer earns its cost" — see the board's `/panel` page. This is
automatic and best-effort: **do not** post the panel result to the board by hand,
and never re-run the panel to produce a record. A board that is down or
unconfigured prints one line and changes nothing about the review.

Show the user this panel summary before launching the fixer.

> First run may need `op signin` once (SonarCloud token caches afterwards) and
> `codex login` for the Codex reviewer. Missing reviewers are reported as
> skipped, not fatal.

## 4. Launch the fixer sub-agent

Read `~/.claude/commands/review-pr.md` and lift its **SUB-AGENT BRIEF** verbatim
— that is the canonical boil-the-ocean fix/verify/commit discipline; keep it
single-sourced. Launch **one** `general-purpose` sub-agent with that brief,
with these overrides:

- **Replace step 2 "Deep review" (self-discovery) with the supplied panel
  findings.** They are already exhaustively reviewed and judged — the sub-agent
  does NOT re-derive them. Paste the full **To fix** list and the **SonarCloud**
  issues into the brief as the findings to resolve.
- The sub-agent may still surface *additional* obvious defects it trips over
  while fixing, and must fix those too — but its job is to resolve **every**
  panel-confirmed finding (P1–P4) plus every SonarCloud issue, to the
  "nothing left to improve" standard.
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

## 5. Re-review the fix commit — the round that used to be skipped

Once the fixer has **pushed**, run the panel again over the new commit:

```
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

Read `round_stop` from the JSON (`jq .round_stop`). It is mechanical and it is
the decision — do not substitute your own judgement, and do not ask a reviewer
whether another round is needed (that asks a model to predict findings it has not
made; one that just wrote five says yes, one that silently produced nothing says
no with complete confidence):

- **`stop: false`** — there is work outstanding: findings no earlier round raised,
  a P1/P2 still confirmed, or a finding an earlier round already raised that is
  *still* confirmed at any severity (the fixer was told and it is still there —
  and SonarCloud's hard-gate issues count here exactly like the judged ones). Fix
  them (§4 again, with only this round's findings in the brief), then run the
  panel again as round `r+1`. Repeat until `stop` is true or the cap is reached.
- **`stop: true`** — the cycle is done. Note `confident`: **false** means the
  stop was not convergence — a reviewer read a prefix of the diff, never ran,
  returned nothing parseable or declared a gap; the cap ran out; or the round had
  no baseline to compare against. The `veto` list says which. Report it as a stop,
  never as "clean".

Two things this must NOT do:
- **Never let a fix ride out unreviewed silently.** At the cap, if the last fix
  pass changed anything, say so in the relay: "the round-N fix commit was not
  itself re-reviewed".
- **Never re-run a round to get a nicer answer.** Each panel run is recorded on
  the board as an observation; re-rolling one corrupts the record it exists to be.

## 6. Relay the result

Show the sub-agent's summary table verbatim. Then state plainly: that the panel
summary was posted as a PR comment, the branch it pushed to, whether the
**SonarCloud hard gate** is now clear, whether all checks passed, anything flagged
**unverified**, and any reviewers the panel skipped. If the panel ran on a
hand-picked set rather than the repo's configured one, say which reviewers ran —
the fixer's bar is only as good as the review that fed it. If the sub-agent
stopped early, report exactly where and why.

Then the part that is new, and is the point of running more than one round:

- **Rounds:** how many panel/fix cycles ran, what each round found that the last
  had not, and **what stopped it** — a dry round or the round cap. One line per
  round.
- **Was the stop earned?** If `confident` is false, say so in those words and
  list the vetoes. A reader must never have to infer that a "no new findings"
  round had a reviewer reading half the diff.
- **Coverage:** per reviewer, anything it declared it could not assess, and any
  reviewer the panel truncated (with the budget that cut it). If reviewers
  disagreed — one clean, one "could not assess X" — say so, and give the master's
  `coverage_note` if it ruled on the split. That disagreement is more informative
  than either verdict on its own.
- **Flagged for re-review:** findings whose reporter said the FIX needs re-reading,
  and whether the following round did find something there.

## 7. Merging (only if the user asks)

`gh pr merge --merge --delete-branch` — preserve commits; never squash.
