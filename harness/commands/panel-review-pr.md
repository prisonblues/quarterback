# Panel Review and Fix PR

@description Like /review-pr, but the findings come from the multi-reviewer PANEL (Claude + Codex + Antigravity + master judge + SonarCloud hard gate) instead of one sub-agent reviewer. Ensures a PR exists, runs ~/.claude/loops/panel.py (which comments the summary on the PR), then a sub-agent fixes every master-confirmed finding boil-the-ocean style and pushes — and the panel then RE-REVIEWS that fix commit, which is the round nobody used to run. Give it several PR numbers and each one is reviewed+fixed by its own sub-agent, in parallel. Panel members default to the repo's .harness-rules.sample and can be named explicitly. It ends by running the pre-land gate and offering to land only on READY; merging stays opt-in.
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
  string — except **`--loop`, which takes no value and is consumed alone**
  (`--loop 12` is `--loop` and PR #12, not a flag whose value is `12`).
  **Then** read what is left: every integer is a PR number — `12`, `#12`,
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
add `--reviewers <comma-list>` from `claude`, `codex`, `antigravity`, `sonarqube`. It
replaces the configured set rather than filtering it, so a named reviewer runs
even where the rules disable it. Fewer reviewers means thinner coverage feeding
the fixer — surface that in §6 rather than letting a one-vendor review read
like a full panel.

From its output collect:
- **Panel dials** — the line the report prints under that name, naming `review_panel`.
  It says which severity floor the fixer is being briefed to, what buys another round,
  whether the fixer may defer, the low-severity line budget, and the fix-growth ceiling. Read it BEFORE §4: the brief you build
  depends on it, and it is the only place the round's policy is written down where
  you can see it (#165).
- **To fix** — the master-confirmed findings the fix round is asked to clear (any
  reviewer count, at or above the round's `fix_severity_floor`). The ones marked 💸
  are below `round_trigger_floor` and share the round's `low_severity_fix_lines`
  budget rather than being unconditional (#297); the note under the heading states
  the number and the rule.
- **Reported, not this round's work** — the master-confirmed findings BELOW that
  floor, marked 🔽. Present only where the floor left something under it. These are
  recorded and relayed and **never pasted into the fixer's brief**; §4b says what
  becomes of them.
- **SonarCloud** — the hard-gate issues (these MUST end up resolved).
- **Skipped reviewers** — note them; a skipped Codex/Sonar means thinner
  coverage, surface it.
- **Coverage declared** — per reviewer, what it said it could not assess, plus
  any reviewer the panel reports as truncated. These are what separate "clean"
  from "I could not tell"; carry them to §6 rather than dropping them.
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
  does NOT re-derive them. Paste the full **To fix** list and the **SonarCloud**
  issues into the brief as the findings to resolve.
- Its job is to resolve **every panel-confirmed finding the round asked it to
  clear** — the **To fix** list, which is already filtered to the round's
  `fix_severity_floor` — plus every SonarCloud issue, to the "nothing left to
  improve" standard. Paste the **To fix** list and the SonarCloud issues, and
  **not** the 🔽 *Reported, not this round's work* list: a fix pass that takes those
  on is the growth the floor exists to stop, and the floor has already made that
  judgement (#165).
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
  line count and the spend rule.
- **Relay the dials into the brief.** The sub-agent cannot read `.harness-rules` for
  itself in worktree mode and must not guess: state `fix_severity_floor`,
  `low_severity_fix_lines`, `reviewer_scope` and `fixer_may_defer` from the panel
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
square brackets) in its `Deferred` and `Escalated` blocks, because the ID is the only
identifier it ever saw: the report carries no keys, on purpose (see below). You hold
the payload, so the ID → key mapping is yours, and every `key` in the JSON below and
every `--escalated` argument in §5 comes out of it. The one-liner prints both columns:

```bash
# id, key, and what each finding actually was — the left column is what the fixer
# quotes back at you, the second is what the board and --escalated take
jq -r '.to_fix[] | "\(.id)\t\(.key)\t\(.severity)\t\(.synthesis)"' r<r>.json

cat <<'JSON' | qb record-outcome
{"repo": "<owner/name>", "pr": <pr>, "outcomes": [
  {"key": "<key of a finding the fixer resolved>", "outcome": "fixed"},
  {"key": "<key of one that was not a defect>", "outcome": "refuted",
   "note": "installPhase does `install -m 0755 bin/*` — it globs, the script IS installed"},
  {"key": "<key of one left for later>", "outcome": "deferred",
   "deferred_to": "prisonblues/quarterback#132"}
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

One of four per finding:

- **`fixed`** — the fixer changed the code and the finding is answered.
- **`refuted`** — it was not a defect. **Requires a `note`, and the note is the
  point**: you are already writing the refutation into the PR comment and the fix
  commit, in prose nothing can count. A bare `refuted` is the same
  confident-assertion-with-nothing-behind-it the release exists to measure.
- **`deferred`** — real, not now. Put where it went in `deferred_to`. **Three roads
  arrive here and all three are the same row.** (1) The fixer said so itself, under
  `review_panel.fixer_may_defer` — the defect is real and outside what this change is
  for, with the two justifying lines in its summary's `Deferred` block. (2) The
  panel reported it BELOW the round's `fix_severity_floor`, so it was never in the
  fixer's brief at all — or it was marked 💸 and the round's `low_severity_fix_lines`
  budget ran out before it, which is the same row for the same reason (#297); one
  issue for the batch is fine and usually right, since
  filing nine issues for nine P3s is the overflow the floor exists to stop. (3) An
  **escalated** finding (the brief's step 3a): the defect is
  real and the fix is what is in dispute, so `refuted` would be a lie about the
  finding and `fixed` a lie about the code, and there is no fifth outcome to
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

Read `round_stop` from the JSON (`jq .round_stop`). It is mechanical and it is
the decision — do not substitute your own judgement, and do not ask a reviewer
whether another round is needed (that asks a model to predict findings it has not
made; one that just wrote five says yes, one that silently produced nothing says
no with complete confidence):

- **`stop: false`** — there is work outstanding: findings no earlier round raised,
  a P1/P2 still outstanding, or a finding an earlier round already raised that is
  *still* outstanding at any severity (the fixer was told and it is still there —
  and SonarCloud's hard-gate issues count here exactly like the judged ones). Fix
  them (§4 again, with only this round's findings in the brief), then run the
  panel again as round `r+1`. Repeat until `stop` is true or the cap is reached.
- **`stop: true`** — the cycle is done. Note `confident`: **false** means the
  stop was not convergence — a reviewer read a prefix of the diff, never ran,
  returned nothing parseable or declared a gap; the cap ran out; or the round had
  no baseline to compare against. The `veto` list says which. Report it as a stop,
  never as "clean".

**Before a `stop: false` becomes another fix pass, declare the premise that pass
will rest on.** This is the futility brake (#84), and it is yours to run — not the
fixer's — because you are the only reader with both rounds in front of you, which
is the same reason the *Match it by premise, not by key* rule below is yours. Every
round from 2 on, before you go back to §4:

```
python3 ~/.claude/loops/panel.py --premise "<one sentence: what this fix pass assumes>" \
    --pr <pr> --round <r> --premise-file /tmp/tmp.AbC123/premises.json \
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

**Read the exit code.** `0` records the declaration: brief the fix pass. `4` is the
brake: `review_panel.escalate_on.premise_repeated` (default `2`) says a fix has
already been written against this premise once in this cycle, and **the second one
is not to be written**. Do not launch §4. The findings that premise explains become
escalations under the `--escalated` rule below — relay them, open the premise
issue, and stop the cycle. The command prints the `--escalated` keys for the round you are recording
against.

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
the cap.

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
    --baseline /tmp/tmp.AbC123/r1.json [--baseline …] \
    --json-file /tmp/tmp.AbC123/r<r>.json
```

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
  in the panel's own **To fix** and **SonarCloud issues** lists is there so a
  brief built from either cannot include it by accident.
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
- **Never let a fix ride out unreviewed silently.** At the cap, if the last fix
  pass changed anything, say so in the relay: "the round-N fix commit was not
  itself re-reviewed".
- **Never re-run a round to get a nicer answer.** Each panel run is recorded on
  the board as an observation; re-rolling one corrupts the record it exists to be.
- **Never `--force` past a refusal to keep the cycle moving.** A refused round is
  the panel declining to manufacture work, and a forced one hands the fixer findings
  about code that is already in the base branch — the failure mode in full, with the
  check bypassed on the way. It is the user's call, and it is recorded either way.

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
  round had a reviewer reading half the diff. This is not only a line in the
  relay: §7 blocks the offer to land on it.
- **Coverage:** per reviewer, anything it declared it could not assess, and any
  reviewer the panel truncated (with the budget that cut it). If reviewers
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

- **Whether the release entry is ready to be numbered.** preland has no check for
  this and never returns RECONCILE for it, so it is asked here or it is not asked:

  ```bash
  python3 scripts/release_stamp.py preflight
  ```

  `preflight` asks the question and spends nothing — it does not stamp. That is
  deliberate. `apply` resolves the placeholder against the base **as it stands
  now**, so a number taken here could be taken by another branch while the user
  is deciding, and the repair for that is a hand-edit. It is stamped in the merge
  sequence below, once there is a yes. Forgetting it entirely is issue #168: two
  of the last three releases landed unstamped and needed repair PRs (#289, #291),
  which is why it is in the sentence the offer is made with.

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

Claim the branch, re-verify, stamp the release, merge. In that order:

```bash
qb-claim branch <branch> --note "landing PR #<pr>" --json          # exit 1 = held
python3 ~/.claude/loops/preland.py --pr <pr> --require-earned-stop --json \
    --claim-holder "<the holder from that answer>"                 # must be READY
python3 scripts/changelog_fragments.py assemble
python3 scripts/release_stamp.py apply --onto origin/<base>
git diff --stat                                                    # read this before committing
git add -A CHANGELOG.md README.md changelog.d
git commit -m "chore(release): stamp vNEXT" && git push
gh pr merge <pr> --merge --delete-branch
```

- **`qb-claim` exit 1** means another agent is already landing this branch. Stop
  and say who holds it; two agents accepting the same offer at the same moment is
  what the claim exists to prevent, and it is the only thing between them. Exit 2
  is "cannot tell" — a board outage, a rotated token — and whether to land without
  the serialisation is the user's call, so ask rather than deciding for them.
- **Re-run the gate after claiming**, because time passed between the offer and
  the yes: CI can have gone red and the head can have moved. `--claim-holder`
  takes the `holder` field out of `qb-claim --json`, so your own claim is not read
  as somebody else's. Anything but READY here ends the sequence: report the new
  verdict, and say that you hold the claim and did not merge, so nobody reads a
  live claim as a landing in progress. It carries a TTL and lapses on its own.
- **The release entry, then its number, in that order and no other.** A fragment
  names no version, so there is nothing for `apply` to rewrite until `assemble`
  has built the entry; run it the other way round and `apply` sees a branch with
  no placeholder, returns 0, and the fragments land unassembled with the release
  silently unnumbered. Both are noops on a branch that ships no release, so run
  them rather than guessing whether this one does. Exit 2 from either is a
  refusal carrying the sentence that repairs it — read the message rather than
  matching it against a list of causes.
- **That commit is the last thing before the merge, and it is bounded.** Pushing
  it moves the head past the round §5 read, so a gate run after it would HOLD on
  `head_sha` — correctly, and about a commit two tools wrote rather than about the
  PR. The RECONCILE path answers that by re-anchoring with another round; this one
  cannot, because it is the terminal action and there is nothing after it to
  verify. So the verification is the run *above* it, and what makes that sound is
  a bound: read `git diff --stat` before committing and confirm that what moved is
  `CHANGELOG.md`, `README.md` and the fragments those two tools consumed, and
  nothing else — which is also why the commit stages those paths by name rather
  than with `-a`. If anything else moved, that is not a mechanical commit: stop,
  say so, and put it back through §4.
- **`--merge`, never `--squash`.** Preserve the commits: the fix commits and the
  rounds that reviewed them are the record of this cycle, and a squash throws away
  the correspondence between them.

**The rounds you just ran are an input to that verdict, not a substitute for it.**
preland reads the round the panel *recorded on the board*, so a round that never
got there — no `qb` on this host, a board outage, `--no-record` — reads as never
reviewed and HOLDs. That is deliberate: a review nobody can point to afterwards
is not evidence the review happened.
