# Agent coding loops — engine

Agent-driven PR loops: a dependabot lander, a multi-reviewer panel, an epic
driver, and an issue watcher that reads the tracker and mostly declines. Originally designed in selfhost `issues/open/117-feature-agent-coding-loops.md`,
which remains the reference for the *why*. **The gates are the product.**

> **Where this lives.** Source: `harness/loops/` in the quarterback repo, which is
> canonical — it was previously vendored into a personal NixOS config and two other
> repos, and those copies drifted. Deployed: the flake's home-manager module ships this
> directory to **`~/.claude/loops/`**, next to the `/loops`, `/panel`,
> `/panel-review-pr`, `/lander`, `/epic` and `/fix-and-land` skills in
> `~/.claude/commands/`. No repo checkout is needed to run any of it — see
> [../README.md](../README.md) for both install paths.
>
> `~/.claude/loops/` is a **read-only nix-store symlink**, so epic run state goes to
> `~/.local/state/loops/` (override with `LOOPS_STATE_DIR`). The scripts are invoked
> with plain `python3`, not `uv run` — there is no project to resolve from here, and
> they import only the stdlib (`certifi` is used when present, with a system-CA
> fallback at every call site).

## Configuration — `.harness-rules.sample` + `.harness-rules`, in the repo

Each repo describes itself with a rules file in its root, the same convention
`create-worktree` already uses for `.worktree.json`. **Every key is optional**;
anything omitted falls back to `DEFAULTS` in `harness_rules.py`, which is the safe
end of every switch — no auto-merge, no unattended loop, edit-only headless agents.
A repo with no rules file at all still works, with one exception: **a panel review
refuses to run**, see below.

It is **two** files, and which one you edit follows from what you are saying:

| File | Tracked? | Holds |
|---|---|---|
| `.harness-rules.sample` | **yes**, on the protected branch | policy — merge gates, bases, budgets, title patterns, the fleet's model pins |
| `.harness-rules` | **no** (gitignored) | what THIS BOX's providers will actually serve: `reviewers.<seat>.{enabled,model,effort}` and nothing else |

A model pin is one value for the whole fleet; what a provider will serve is a fact
about one machine, and while the file was tracked those two had to be the same
thing. On daedalus they are not: codex routes through an employer Azure gateway
deploying `gpt-5.5` and refuses the fleet's `gpt-5.6-luna` with a 404, then refuses
`max` effort separately. So the box says so in a file of its own, rather than in a
committed one that would move the pin for every other box too.

**Where the box says it, and why it is not in the repo.** The answer is read from two
places, and the repo's wins per key:

| | Path | Scope |
|---|---|---|
| box | `$QUARTERBACK_HARNESS_RULES`, else `$XDG_CONFIG_HOME/quarterback/harness-rules.json` (`~/.config/…`) | every repo and every worktree on this machine |
| repo | `<repo>/.harness-rules`, untracked, beside a `.sample` | this checkout only |

Prefer the box file. "What will this machine's providers serve" is true of the
machine, so keeping it per-checkout kept it N times and propagated it none —
`create-worktree` does not copy it — and a fresh worktree therefore resolved a seat to
a pin its provider does not deploy. What followed was not a crash but a conversation:
the agent holding that worktree rediscovered the machine's own configuration and
relayed the workaround to its peers in prose, which is the least reliable channel
available and the one the tool needing the answer cannot read. It also blocked the
remedy for a worse problem — the fix for agents clobbering each other in one checkout
is a worktree each, and adopting that multiplied the rediscovery by the number of
worktrees. One file per box is what makes worktree-per-agent safe to turn on (#240).

The two are gated differently on purpose. The repo file needs BOTH its conditions —
untracked, and a `.sample` supplied the baseline — because untracked alone does not
mean "overlay": a repo whose only config is an uncommitted `.harness-rules` would have
its whole policy demoted to a seat toggle. The box file needs neither, because it
cannot be the baseline nor be mistaken for it, and a legacy repo whose committed rules
name an unservable pin is exactly what the machine's own answer should still correct.
A path named in `$QUARTERBACK_HARNESS_RULES` that does not exist is a hard exit, not a
fallback: somebody pointed at a file, and quietly running the fleet pin instead is the
silent-policy failure this module exists to prevent.

**The overlay may set three keys, and it may only NARROW.** All three answer one
question — what will this machine serve — so `enabled` can take a seat OFF a box
whose CLI is missing, and cannot turn one back ON that the sample deliberately
disabled for cost, policy or merge quorum. Everything that decides what may be
MERGED (`auto_merge`, the `epic` and `preland` blocks, budgets, title patterns) stays
in the sample, because an untracked file is reviewed by nobody. Values are checked as
well as names: `"enabled": "false"` is a truthy string and is **dropped and reported**
rather than silently keeping a seat on, and an `effort` no CLI serves is refused
against the same set `run_seat` rules on. Every key it drops is named on stderr.

**Migrating a repo** is one commit, and it is optional — a repo with only a tracked
`.harness-rules` and no sample resolves exactly as it always did:

```bash
git mv .harness-rules .harness-rules.sample     # policy, tracked
```

Two things to know, because this path removes a file that used to be tracked:

- **A checkout with local edits to the old `.harness-rules` should copy it aside
  before pulling that commit.** That is a plausible state, since it was the only
  per-repo knob there was — and the rename deletes the tracked path out from under
  those edits. The copy is what becomes the per-box overlay, gitignored from then on.
- **Doing the two halves in two commits leaves both files tracked.** That is safe:
  the sample wins as the baseline and the resolver **says so by name** rather than
  silently dropping the other one's policy. Finish it with
  `git rm --cached .harness-rules`, which untracks the file while keeping it on disk,
  where it is now the overlay.

### The third layer: dials the board states and the repo only defaults (#305)

`review_panel.fix_severity_floor` decides which findings a fix pass may touch;
`round_trigger_floor` decides which ones buy another round. Between them they decide
what a review costs and what it is worth — and changing either was **a commit on a
pull request, reviewed by the panel those dials configure.**

That is the wrong shape for a policy knob, and here is what it cost: this repo's
`.harness-rules.sample` stated both floors at P2 while every round of the five run on
PR #299 put P4 findings in `to_fix` with `below_fix_floor` empty, which cannot happen
under a P2 fix floor. The file that stated the policy and the rounds that applied it
disagreed for five rounds, four agents and a landed release, because there was no way
to **ask** what the floor was.

So there is a third layer, applied last:

```
DEFAULTS  ->  .harness-rules.sample  ->  per-box overlay  ->  the board
   (a default)      (this repo's default)    (capability)      (the value IN FORCE)
```

**The repo states a DEFAULT; the board states the value IN FORCE; and the reported
answer names which layer produced it.** That last clause is what stops this becoming
a second place a dial is written down (#56's rule) — nothing on the board is
authoritative on its own, because every resolution reports the layer behind every
dial.

```bash
harness_rules.py --dial review_panel.fix_severity_floor
# review_panel.fix_severity_floor  "P3"  [board] repo — 'trying P3 for a fortnight'
#                                                  by rich, 2026-09-05T00:00:00+00:00

harness_rules.py --dials      # every dial, its value, and the layer that answered
harness_rules.py --json       # the same table, as `_dials`
```

Every reviewed round records it too, in the payload's `rules.dials`, and a round that
ran under a board dial says so in `config_notes` — which `--post` puts in the public
PR comment.

**Setting one takes no pull request:**

```bash
curl -X POST "$QUARTERBACK_BASE_URL/dials" -H "$edge_headers" -d '{
  "repo": "prisonblues/quarterback",
  "dial": "review_panel.fix_severity_floor",
  "value": "P3",
  "reason": "trying the P3 floor for a fortnight against the deferred backlog",
  "expires_at": "2026-09-05T00:00:00Z"}'
```

Four properties, each closing a failure the two file layers have:

- **It expires by itself.** `expires_at` is optional — omit it for a floor you mean
  to keep — and an expired dial is simply *absent*: a resolution whose dial lapsed and
  one that never had a dial are indistinguishable, so nothing has to be remembered and
  cleared.
- **Reads happen on BOTH paths**, unlike the overlay. The overlay is excluded
  unattended because it is a file in an untrusted working tree; the board is not in
  the working tree. Unattended is also the path a budget governor exists to govern
  (#276).
- **Writes are human-only.** Reads take any agent's bearer token; `POST /dials` takes
  the same edge-authenticated human gate the plan's order takes. Anything running while
  a branch under review is checked out — a test suite, a build step, a git hook — holds
  this box's machine token, so a machine-writable dial would be the two-ref rule's own
  hole reopened from the board side.
- **A repo with no dial behaves exactly as before.** An empty table and an unenrolled
  host are the same silent answer. A board that is *configured here and will not
  answer* is a different fact, and is reported rather than swallowed.

**Which dials, and the one boundary case.** The settable set is
`harness_rules.BOARD_DIALS`, and it is the dials whose value is a judgement about
**cost** rather than about capability: both floors, `low_severity_fix_lines`,
`unrefereed_line_weight`,
`max_fix_growth`, `max_fix_growth_chars`, `max_fix_guard_lines`,
`threshold_by_severity`,
`max_rounds`, `reviewer_scope`,
`next_door_days`,
`max_diff_chars`, `judge_max_diff_chars`, `judge_model`,
`distant_merge_lines`, `escalate_on.premise_repeated`,
`escalate_on.premise_undecidable`, `escalate_on.fix_injection`,
`escalate_on.new_findings_not_falling`, `escalate_on.unrefereed_fix`,
`escalate_on.guard_lines` and
`propose_on_escalation`.
Everything that decides what may be **merged** —
`auto_merge`, the `epic` and `preland` blocks, title patterns, the loop schedule —
stays in the sample, for the reason the overlay refuses the same set.

`reviewers.<seat>.enabled` is settable and is the one dial with a direction rule: the
board **may turn a seat off and may not turn one on.** It is both capability and
policy, and the board's claim is the policy half only — whether a seat is worth its
tokens. Nothing on the board knows which CLIs a machine carries, and `panel.py` counts
a seat that never ran as coverage it did not get, so a board that could enable a seat
could veto every round's confidence on a box that never had it. Every other dial may
move **either way**, because raising `fix_severity_floor` from P3 to P2 makes rounds
cheaper and coverage thinner while lowering it does the reverse: neither direction is
the safe one, which is exactly why #276's narrow-only throttle rule cannot govern a
floor.

**The board does not know what a dial IS.** It stores an opaque name and opaque JSON.
The harness ships the dial table and the server image carries no `harness/` directory,
so a copy there would be the second-source-of-truth failure arriving from the other
end. A dial the harness does not recognise, or holds at a value it may not take, is
**refused and named on stderr at every resolution** — never dropped quietly.

`QUARTERBACK_DIALS` set at all — the empty string included — is the offline switch:
the variable becomes the whole layer and the board is not consulted. That is what the
suites use, and what an operator uses when the board is down.

Two conventions the resolver enforces so a rules file can be read like prose:

- **A key starting with `_` is a comment**, at any depth (`"_": "why this seat is
  on"`, `"_effort": "…"`). JSON has none of its own, these files exist to be argued
  with, and comments are stripped before anything reads the config — so they can
  never be mistaken for a setting.
- **A name nothing recognises is warned about on stderr and dropped**, loudly and
  non-fatally, at every level: the top level, each deep block, and the fields
  inside a reviewer. `reviewers.antigravty` would otherwise be a silent
  one-vendor-short panel, `reviewers.pi.enabld` a seat left off, `auto_merg` an
  auto-merge policy quietly on its default, and `loops.issue_executer` a loop
  switched off by a typo (those defaults are OFF); dropped as well as warned about, so the word
  "ignored" is true and no consumer iterating the resolved config sees a phantom
  seat. A warning rather than a hard exit, because a file shared across boxes may
  name a setting only a newer harness knows — and once per name per process, not
  once per `resolve_repo`, since epic resolves per run and shells out to panel.py
  which resolves again. A seat that was RENAMED says so (`gemini` →
  `antigravity`), which is the one unknown name a fleet file is likely to carry.

This replaced a central `config.json` in the fleet config. That file was a registry
of personal repos that had to be enrolled by hand before anything would run; the
plumbing it carried (`path`, `github`, `default_branch`) was derivable from the
checkout anyway, and three documented fields (`ci_gate`, `worktree_isolation`,
`consensus_threshold`) were read by no code at all. Dropping it is what lets the
read-only commands run in **any** repo — and what lets this engine ship to the work
host, since it no longer names anything personal.

Inspect what a repo resolves to:

```bash
python3 ~/.claude/loops/harness_rules.py                 # cwd's repo, one line
python3 ~/.claude/loops/harness_rules.py --json          # the full merged config
python3 ~/.claude/loops/harness_rules.py --discover      # repos shipping either rules file
```

### Which ref the rules are read from

This is the one security-relevant choice in the design.

| Mode | Rules come from | Used by |
|---|---|---|
| interactive | the working tree: `.harness-rules.sample`, plus this box's overlay — `~/.config/quarterback/harness-rules.json` and/or the untracked `.harness-rules` | `/panel`, `/epic`, `/lander` — anything you typed |
| unattended | `git show origin/<default>:.harness-rules.sample` (falling back to `:.harness-rules` for an unmigrated repo), and **nothing** out of the working tree | the systemd timer (`HARNESS_UNATTENDED=1`) |

An in-tree rules file means repo content influences the harness. For the flows a
human triggers that is not a new door: `/epic`'s executor already runs with
`bypassPermissions` and a full shell, so it could run `gh pr merge` itself without
touching any config, and anyone able to commit to a default branch can already put
arbitrary code in the build.

The exception is the lander's red-CI fixer. That agent is deliberately **edit-only**
(`--permission-mode acceptEdits`, no bash — the loop owns commit+push and the merge
decision is Python, not an agent), and it operates on an **upstream-authored
dependabot branch**. Reading rules from that branch would let a poisoned PR rewrite
the policy governing its own review — reaching something the agent otherwise cannot.
Hence the split. A human at the keyboard *is* the authorization; unattended runs only
honour rules already merged to the default branch.

**And the working tree means the whole working tree, tracked or not.** The per-box
overlay is read on the interactive path only. The first version of this split read it
unattended too, arguing that an untracked file cannot have come from a PR because git
will not deliver a file it is not carrying — which is true of what git *checks out*
and says nothing about what code run from that checkout *writes*. A test suite, a
build or lint step, a git hook or a Makefile target, invoked while the branch under
review is checked out, can create `.harness-rules` in the repo root; it is gitignored,
so `git status` will not even show it; and `{"reviewers": {"<seat>": {"enabled":
false}}}` planted that way shrinks the panel reviewing that very PR, which panel.py
counts as coverage it did not get. The cost of closing it is named rather than argued
away: an unattended panel on a box whose fleet pin is unservable falls back at
*runtime* (two extra CLI round-trips, and `codex (CLI default)` in the cost history)
instead of being configured correctly. `describe()`'s provenance line says when an
overlay went unread for this reason.

**A repo with NO rules file at all is refused a panel review** (`panel.py --pr`, and
`--ask`), rather than reviewed on the built-in defaults. Absence means "use the
defaults" for `epic`, `lander` and `preland`, where every default is the safe end of
its switch and an unconfigured repo simply does less — but a review's defaults are a
two-seat panel on models nobody chose, adjudicated by a judge nobody chose, and its
output is not inert: the findings brief a fixer that edits the repo, and a confident
stop reads downstream as coverage. The refusal prints, writes a payload with
`reviewed: false` and a `skip_reason` naming the remedy, exits 0, and is **not**
recorded on the board — no review happened. The remedy is one committed
`.harness-rules.sample`.

### Schema

Detected from the checkout, **not settable** here: `path`, `github`, `default_branch`
(and `name`, which defaults to the directory name).

| Key | Meaning |
|---|---|
| `enabled` | Master switch — `false` means the loops skip this repo, **the panel included** as of #55 (it was honoured by `lander.py` alone before that, so a repo that had switched itself off still got reviewed). Board-settable and **narrow-only**: the board may turn a repo off and may not turn one back on over a file that said no. One `POST /dials` is #55's "turning the watcher off takes one setting", and it takes effect on the next resolution rather than the next restart. |
| `auto_merge` | `none` \| `dependabot_patch_minor` \| `all_green` — see below. |
| `headless_permission_mode` | `claude -p` mode for headless agents. `acceptEdits` = edit-only (safe). `/epic`'s executor runs `/fix-issue` + `/review-pr`, which need git+gh, so it needs `bypassPermissions`. |
| `executor_pr_base` | Base branch the issue→PR executor opens PRs against. Defaults to the default branch. **Only used when *creating* PRs** — see "Branch base". |
| `dependabot_author` | Author filter for the lander (`app/dependabot`). |
| `reviewers` | Which reviewers run — see below. |
| `review_panel.skip_title_patterns` | Regexes for PRs not worth LLM review (merge/promote/release/format-the-world). These drove a cost blow-up in #117 — one release-merge ≈ $750. |
| `review_panel.judge_model` | Claude model for the master judge — `sonnet`, deliberately **not** `reviewers.claude.model`; see below. An explicit `""` is passed through as empty and lets the CLI pick, which is NOT the same as omitting the key (that gets the default). |
| `review_panel.ask_quorum` / `ask_threshold` | `--ask`'s tally rules: how many seats must have **answered** for the vote to mean anything, and how many must have said the same thing for it to be that answer. Both **2** — one seat agreeing with the agent that wrote the premise is not a challenge. A rule above the number of seats on the ask is warned about: it can never be met. |
| `review_panel.ask_max_context_chars` | Total `--context` material one ask may hand its seats, across every spec. **60,000** (~15k tokens). Over budget is clamped and SAID, per spec — an ask's whole claim is that it is the cheap check, and unbounded context is the #117 cost shape on the path advertised as costing a minute. |
| `review_panel.reviewer_code_access` | May a seat READ the code under review? **true**. `false` is the old posture — every seat in an empty repo, the diff its only evidence — and is what a repo taking UNTRUSTED contributions selects. On does not mean every seat gets it: only a CLI that can express "read but do not execute" is handed the tree (today just `claude`), and which seats did is recorded per seat. `--no-code-access` turns it off for one run. See below. |
| `review_panel.require_mergeable` | Must the branch be able to MERGE before a round is worth running? **true**. GitHub reporting the branch `CONFLICTING` means the merged state the review is implicitly reasoning about does not exist, and the rebase that resolves it changes the diff every finding is about — so the round is refused before any seat is dispatched. It is nearly free: mergeability rides on the PR metadata the panel already fetches, and the same check used to run only at the merge gate (`preland.check_pr_state`), i.e. after a full multi-vendor round and a judge had been spent. `false` reviews conflicted branches and says so in `config_notes`; `--force` is the per-run override. GitHub computes mergeability lazily and answers `UNKNOWN` while it does, so the question is put a second time when the first answer is cold — measured here, three consecutive reads of an open PR gave `UNKNOWN`, `CONFLICTING`, `CONFLICTING`. An `UNKNOWN` that survives both reads is a note and never a refusal. |
| `review_panel.reviewer_code_budget_usd` | Dollars the code-reading seat may spend per invocation (`claude --max-budget-usd`). **`null`** — uncapped — for the reason `max_diff_chars` is: reaching the cap is a LOST seat (a skip, which vetoes), not a cheaper review. Measured for calibration: ~$4 for one seat on a 75,628-char diff against ~$0.70 diff-only. Applies only to a seat that got the tree; the cap is per invocation and a reparse retry can spend it twice. |
| `review_panel.fixer_may_defer` | May a fixer answer "real, and not this change's job"? **true**. Its two exits were a refuted false positive and an escalation about the *approach*, and the brief then said "'Not now' is not available to you" — so a correct third judgement had no legal way out and the only move left was the patch. Maps to the existing `deferred` outcome; the fixer owes a justification and the orchestrator owns the record — a board row always, and a GitHub issue where `file_deferral_issues` calls for one. `false` is the old two-exit behaviour. |
| `review_panel.file_deferral_issues` | Which deferrals get a GitHub ISSUE as well as the board row every deferral gets anyway (#482). **`shape`**, and since #620 it is not a floor. The two records were being treated as one and they are not: the row chains by finding key across rounds, feeds `/panel` and keeps a confident wrong finding from scoring like a real one, while the issue is a work item on somebody's tracker. **The gate asks what shape the TICKET would be, not how severe the finding is.** A **category** — one standing item for a recurring class, the form a human can work as a batch — gets an issue; a **single named item** with real substance behind it, one defect or one decision owed, gets an issue whatever severity it carries; a **batch**, a round's leftovers swept into one ticket, gets board rows and never an issue, whatever its severity mix, a P1 in the pile included. Twenty P3s in one issue is not a deferral, it is a transfer of the problem to a human, and the P1 inside it is better served by the row that chains across rounds and gets read than by being the fourth bullet of a dump. Severity cannot express that, because severity is a property of a finding and batchness is a property of the ticket — so a cut anywhere on P1..P4 files some batches and blocks some single items, which is backwards. The measurement, taken on this repo on 2026-08-26 and re-counted on 2026-08-30: twenty open issues were panel deferred-finding exhaust and nothing else (#66 #69 #72 #74 #95 #104 #111 #119 #120 #126 #132 #133 #140 #223 #237 #283 #285 #286 #288 #300), carrying 345 findings by their own titles, **every one of them a batch, and not one ever closed** — zero drain in fifteen days; #283 is a rescue *from* one of them. **An unclassifiable deferral is a batch**: the safe direction inverts here, because under a band an unreadable case files an issue nobody needed and costs a line on a tracker, while under `shape` that line IS the failure the gate was written for. Where an issue is opened the orchestrator names it in `deferred_to`; where it is not, the row carries **no `deferred_to`** (the column is nullable, the API accepts a targetless `deferred`, and `/panel` renders it rather than breaking) and **a one-line `note`** instead — which is what makes the row worth reading later, and `GET /review/findings?repo=&pr=` is the read that write exists for. Nothing is dropped: a finding kept off the tracker still gets its row, its note and its line in the summary. **The bands are the way back and still work**: `P2` restores the severity cut exactly as it ran from #482 until 2026-08-30, and any of `P1`..`P4` states it at another band. `always` is the pre-#482 behaviour and `never` files none — the right answer for a repo whose tracker is not where its work is queued. **An escalation is exempt at every setting**, `never` included: its issue asks a question rather than filing a task, and it is what carries that question past the end of the session. **An unverifiable claim is exempt on the same reasoning** (#547) and is the one road here with no board row at all — it is not a finding, so nothing on the board keys it, and its durable record is the round's payload plus the issue. If the board write fails the orchestrator files the issue whatever the gate says — without an issue the row is the only record, so losing both loses the finding. The report's dial line says the answer in words on every round. |
| `review_panel.fix_severity_floor` | Severity a fix round is asked to clear, at or above. **`P4`**, as of 2026-08-30, from `P3` (#621). Below it a finding is reported, marked 🔽 under its own heading, recorded (`below_fix_floor` in the payload) and **not fixed**. The measured cut is at P2 (67.3% of findings, zero P1s lost) and this deliberately sits below it: severity is model-authored, and the class a P2 floor misses is correctness expressed as craft — a missing regression test on a parser or an auth boundary, a missing timeout or cleanup, a migration rollback gap — and no floor can tell a mislabelled P2 from a genuine P3. Fixing one in a pass already open is one edit. **Admitting P4 adds no obligation, which is the whole of why it is safe**: this is not the blocking band and has not been since #297 — `round_trigger_floor` is, and it stays at P2, unbudgeted. This key says how far DOWN an already-open pass may reach, and `low_severity_fix_lines` is all it may spend down there, so P4 moves from outside every rule to inside the BUDGETED band where P3 already was. Which of the two a round actually takes is then decided cheapest-first by a count, which is the mechanical answer "limited budget" asks for and the one #297 refuses to hand back to the fixer's judgement. `P3` restores the 2026-08-22 setting and `P2` the measured cut — for `round_stop`'s rules 1 and 3; rule 2's bar is a hardcoded `("P1", "P2")` and only `P1` moves it. A Sonar hard-gate issue is exempt from both floors at every rule. |
| `review_panel.round_trigger_floor` | Severity a NEW finding needs to buy another round. **`P2`** — a tier above `fix_severity_floor`, because letting a P3 buy a round costs a whole panel plus another fix pass where fixing it in the open pass costs one edit. Below-floor new findings are still reported and recorded; `round_stop`'s reason names the floor and the count. `P4` is the pre-#165 behaviour. |
| `review_panel.low_severity_fix_lines` | Churned lines the WHOLE round may spend fixing findings below `round_trigger_floor` — at the shipped floors, the P3 band. **40**. Spent cheapest-first and COUNTED (`git diff --numstat` after each fix), never estimated; what it does not reach is reported and recorded exactly like a below-floor finding. The measurement: PR #188's feature was 185 churned lines and two fix passes made it 721 — **74% of the PR was review-response code** — with round 2's fix list 89% below P2, and on #268 85% of round-2 findings were created by the round-1 fix pass. A budget and not a per-fix cap, because #188's round 1 was 408 lines of individually reasonable small fixes that any per-fix cap waves through; and not a higher floor, because a genuinely cheap correctness-adjacent fix is still worth taking while the pass is open. `0` fixes none of the band (the applied floor becomes the cut, and the report says so); `null` is no budget at all, the pre-#297 unconditional behaviour. While a budget is in force, `round_stop`'s repeat rules are bounded at the cut rather than at the fix floor — an unpaid budgeted finding is outstanding by construction, exactly as a below-floor one is. |
| `review_panel.unrefereed_line_weight` | What ONE churned line of test or prose costs the budget above, against a line of production code at 1. **2**. The budget's unit becomes exposure rather than length. The measurement, on lexray#1697 round 1 (since reverted): a 93-line fix pass across three files that changed **no production logic at all** — the production file's entire share of it was a docstring and a comment — introduced ten findings, nine in the test files and the tenth in that docstring. The 40-line budget worked exactly as specified (31 of 40 lines, cheapest-first, one 17-line fix measured against 9 remaining and dropped) and it priced a 10-line comment correction as equal risk to an 11-line new assertion, because lines are all it could see; four of the five budgeted fixes were "write more test". **Why they are not equal risk:** a production fix has an external referee — red/green either detects the bug or it does not, with the suite and CI behind it — and a test fix has none, because nothing tests a test; a docstring fix has none either. **Not value-weighting**, which #297 refuses deliberately because it hands judgement back to the actor whose judgement the 63.7% indicts: being refereed is a property of the path and the line, read off the fix's own diff — the one the fixer already produces to measure the fix at all — and the fixer is asked for a multiplication rather than a forecast. **Not `git diff --numstat`**, which reports per-file insertion and deletion TOTALS and cannot see a comment, a blank or a docstring: #554's "classifying each PATH is free" is true of numstat, and the LINE half that makes this mean what it says needs the diff body. A line is UNREFEREED when it is in a test path, in a documentation path, or is a comment/docstring/blank line anywhere — `panel_seats.referee_split` is the classifier and `review-pr.md` step 3 is the arithmetic. **2 is the one number in #554 that is a judgement rather than a fact**, and it is small deliberately: an unrefereed line has no referee rather than a weaker one, so the budget must buy strictly fewer of them, while at 40 lines a weight of 2 still affords a 20-line regression test inside the band — and the band is only the P3 tier, since a P1/P2 fix and its test are not on the budget at all. `1` prices every line alike, the pre-#554 behaviour, and is how this is switched off; there is no `null` spelling for the same thing. |
| `review_panel.max_fix_growth` | Multiple of its size at the cycle's FIRST round that the PR may grow to before the cycle stops and says the change wants splitting. **3.0**; `null` switches the check off (the one key here where `null` is not "inherit"). Not dressed up as convergence — it takes a veto line and `confident` is false. **Both ends are whole-PR sizes whatever `round_scope` is** (#298): that dial decides what the reviewers are asked to *look at*, this one asks how big the change has *become*. Measured on the review target it compared one round's fix commit against the cycle's whole-PR starting size, and PR #188 reached 3.90x — 185 → 593 → 721 churned lines — without the guard firing. `pr_chars` in the payload is the number both ends are read from. |
| `review_panel.escalate_on.fix_injection` | Fraction of a round's **new outstanding findings** that may have been INTRODUCED by the previous fix pass before the cycle ends. **0.5**, read strictly — more than half a round's news being the fix pass's own damage means `round_stop`'s rule 1 is being fed by the loop's own output, and a termination test fed by its own output can only end on the cap. `null` switches it off (the second key here where `null` is not "inherit"). Not dressed up as convergence: a veto line naming the dial, `confident` false, and a `reason` saying a human answers it. **It can only turn a `go again` into a stop**, never the reverse — a dry round, a below-floor policy stop and a round holding an escalation all keep the reason they earned. **And only the round RULE 1 was buying**: a round going again under rule 2 (a P1/P2 or a Sonar gate issue outstanding) or rule 3 (a finding an earlier round already raised) is going again for work the fix pass FAILED to do, not work it generated, so a rate over four below-floor findings cannot cancel the repair round for an unrelated P1. That is where it parts from #84's brake, which fires at any of the four rules: a repeated premise is a fixer's own declaration, and this is a threshold on a statistic. The payload therefore carries **`over`** (the measurement crossed) and **`fired`** (this rule is why the cycle stopped) apart — reading the first as the second attaches "ended on divergence" to a confident, converged round. **One round, not two consecutive**: provenance is only attributable from round 2, so a two-round rule could not fire before round 3 and would have been off under the cap this shipped with. **This rung is load-bearing as of 2026-08-30 and the argument that used to stand here went with the old cap.** At `max_rounds: 2` the only round it could fire on was the round the cap was ending anyway, so what a default-on bought was a better `reason` and one more veto line; at **6** it is the brake that decides when a cycle stops, and a false positive now costs a real round rather than a stop reason. **And 0.5 is UNCALIBRATED for the population it now counts**: with observations no longer outstanding work (#623) the findings it divides are a smaller and more serious set than the P3/P4-heavy rounds every number below was measured on, so neither the threshold nor its false-positive rate transfers, and nobody has measured where a healthy cycle sits on it. The first cycles run under a cap of 6 are that measurement rather than a validation of it, and #626 is where the per-round marginal counts they feed belong — it has since shipped as `GET /review/convergence`, whose `marginal_by_round` is those counts and whose `overall` is the population the recalibration has to be fitted to. Read `new_findings` there against `new_findings_runs` and never against `rounds`: the column is nullable and a window holding older rounds sums over a fraction of itself. The measurement: 128 of 201 new findings across seven PRs, 39 of 53 after round 1 on PR #299 (17 of 17 in its round 2), 64% then 87% on the cycle #489 was filed from — every one of them far above 0.5, and every one ran to its cap. `introduced` is a documented FLOOR and not a measurement (#48), which is what makes a threshold on it err safe: a measured 0.64 is at least 0.64. **What #559 removed was the one way it was not a floor**: a fix pass that RESTORED already-reviewed lines — a revert of a revert, the correct answer to a stop this rung fired — had every restored line attributed to it, so the remedy for the gate read to the gate as the disease. Restored runs are now excluded and the round says how many; a round that could not make that comparison says that instead. The unattributable buckets sit in the denominator for the same reason — they depress the rate, so a round the harness could not place does not end a cycle. `panel_rounds.FIX_INJECTION_MIN_NEW` (**4**) is the minimum denominator and is a constant, not a second dial: a rate over two findings is not a rate. One honest mismatch, recorded rather than fixed: the denominator is every new outstanding finding while the rules are applied to the CLEARABLE ones, so a cycle holding an escalation computes its rate partly over a finding no fix round may touch — the two are answers to different questions (what this round PRODUCED, and what the next one could DO about it) and are allowed to differ. Recorded every round as `round_stop.fix_injection` whether it fired or not. **Ending the cycle is half the answer, and the other half is #506**: the fix pass that caused it is still on the branch when the round finishes, so the PR ships carrying the change the panel has just finished saying generated more of the round's work than the pull request did. A round that fires this rule therefore adds one more veto line naming the offending pass's COMMIT RANGE — `prior.head_sha..head_sha`, the same range provenance attributed against, with the commits it is made of and, **where the range is known to hold nothing but the fixer's own commits**, the `git revert` invocation spelled out — and pricing it: what undoing it would REMOVE (the findings attributed to it) against what it would COST (the complaints it was sent to answer that this round no longer raises). **A proposal and not an action**, because reverting a pass reverts the real fixes in it too and nothing here knows which those are without asking; the two columns are biased in opposite directions on purpose — the cost is an upper bound (matched on keys alone, and under `increment` scope it includes what the round did not re-read) and the benefit is a lower one (`introduced` is a floor) — so the case AGAINST reverting always gets the benefit of the doubt. On a REBASED branch the offending pass cannot be named at all: `round_stop.revert.kind` carries `_fix_range_diff`'s own verdict (`ok`/`no-fix`/`blind`/`rewritten`/`not-asked`) rather than a second vocabulary for #500's blindness, and the round says the pass is unnameable instead of guessing at a range. **#504 does not change that**, and the round's notes say so: a rewritten round can be attributing from a REBUILT pass — so the rate that fires this rule is real — and still have no proposal to make, because the proposal reads the compare range rather than the reconstruction. And a range carrying a base-branch MERGE is named without a command: `git revert` refuses a merge without `-m`, and a merge is how main's own commits get inside a range the compare still calls `ahead` — the lean `_fix_range_diff` documents for attribution, which for a proposal would mean offering to undo commits no fix pass wrote. `revert.no_command` says which it was — and the command is withheld on two more: a shape that could not be read, and a range GitHub's compare TRUNCATED at its 250-commit ceiling, where the merge count is a floor and a merge past the ceiling is invisible. "We did not check" must never render as "clean". One extra `gh api` call, made only on a round whose rate crossed the threshold. `revert.spans` counts how many fix phases the range covers — more than one where no intervening round recorded a commit to anchor on, which widens what a revert would undo without making it wrong, since the rate was computed over the same range. Recorded every round as `round_stop.revert`, with `offered` apart from `kind` for the reason `fired` is apart from `over`. |
| `review_panel.escalate_on.new_findings_not_falling` | How many **consecutive rounds whose new-finding count did not decrease** end the cycle. **1**, and on. The volume rung BESIDE the attribution one above, not a second stopping system: `fix_injection` asks *did the fix cause this?*, this asks *is the count still falling?*, and the two have different answers on the same cycle. Findings a reviewer reading deeper produced, a seat that woke up, a scope that widened or a vendor added mid-cycle are news no fix pass wrote — and `_provenance` under-counts the ones it did — so a diverging cycle can sit under 0.5 for its whole life and be stopped only by the cap, which fires in the same place whether the round found two findings or twenty. The rule is the one stated on #480 over a real cycle: 44 findings, then 15 new, then 18 new — stop and triage the remainder. **1 for `fix_injection`'s own "one round, not two consecutive" reason**: a count can only be compared against a predecessor, so round 1 is never a not-falling round and a 2 could not fire before round 3 — which under the cap of 2 this shipped with would have left the rung OFF for every repo that did not configure it. At 1 the earliest round it can fire on is round 2, and under `max_rounds: 6` that is four rounds short of the cap rather than the round the cycle was ending on anyway: like its sibling above, this rung now ends cycles, and what a default-on costs or buys is a real round rather than a better `reason`. **Computed from the ROUNDS' OWN COUNTS and never from provenance**, which is the argument for it existing rather than for tightening the threshold above: #500 (a rebase between rounds silently disarms provenance) disarms `fix_injection` outright and cannot touch this. Same two bounds as its sibling — it can only turn a `go again` into a stop, and only the round rule 1 was buying — which since #505 is enforced as what it says rather than as "rule 1 won the `reason`": rules 1-3 are an if/elif chain, so a round can be going again under all three at once, and either rung used to end such a round with an earlier round's P1 still unfixed. What disarms both now is a P1/P2 or gate issue an EARLIER round raised, or a repeat — this round's own new P1s do not, and must not, since they are the news being counted. The corrected bound is shared with `fix_injection`, which had the same gap — and the payload keeps **`over`** and **`fired`** apart for the same reason. A round is part of the streak only when its predecessor in the series is the round immediately BEFORE it (a gap — round 3 with only round 1's baseline readable — is a missing round, and missing data must never end a cycle; it would also make the `reason`'s "the round before" untrue), both its count and its predecessor's are known — a count is withheld, never guessed, for a round that reviewed nothing, a payload that cannot say which round it is, or a round whose BASELINE HISTORY was incomplete, since "new" means "no earlier round raised it" and a baseline this run could not read inflates that, its count did not decrease (`>=`, so a flat series counts — fifteen a round forever is not converging), and BOTH its count and its predecessor's are at least `panel_rounds.NOT_FALLING_MIN_NEW` (**4**, a constant not a second dial: 1 -> 2 is arithmetic, not divergence). Both ends, because "not falling" is a claim about a SERIES and a series needs two volumes to be one — a round that went from one finding to four has not stopped falling, it was never falling. That is not hypothetical: with the floor on one end, the case where an earlier round simply UNDER-READ (1 finding, then 4) was ended by this rung instead of by the cap. `null` switches it off. **What it does not do:** #505's second clause — triage the remainder into an issue — is only half-served. Since #42 the remainder is no longer handed to nobody: `round_stop.outstanding` carries it with `handed_to: "human"`, because this rung's own `reason` says a human triages what is left rather than another fix pass. Filing it is the caller's step (`panel-review-pr.md` §5), not this rung's. Recorded every round as `round_stop.new_findings_not_falling` whether it fired or not, with the whole series in `counts`. |
| `review_panel.next_door_days` | How many days back a defect **confirmed on another pull request** may be carried in front of this round's reviewers as context (#508). **7**, and on; `0` sends none and makes no board call. `finding_recurrence` (#67) chains a finding to earlier rounds of ITS OWN PR, and the defect this fleet actually ships is one file over: on 2026-08-26 a panel confirmed a P1 in `app.auth.delegated()` — the dev bypass consulted BEFORE the credential check, so a caller with no credential authenticated — and an hour later the identical shape shipped in `app.auth.human()` on another PR, copied out of the same source function. Codex reviewed that diff and returned four other real defects, not that one; the round that missed it was a round **1**, so the per-PR chain had nothing to recur against, and what caught it was a peer reading the PR with their own fix an hour old in their head. `GET /review/next-door` is that peer's memory as a query — this PR's file list joined to every finding another PR of the same repo made in those same files inside the window, deduplicated to each defect's NEWEST observation and then tested for `confirmed` — that order matters, because filtering first resurrects a stale confirmation over a later dismissal — and `panel_core.next_door_brief` renders at most `NEXT_DOOR_MAX` (**8**) of them into `NEXT_DOOR_SLOT`. **A hint and never a finding:** a seat must find the defect in the diff in front of it before reporting one, or the recurrence chain eats its own tail and a seat is rewarded for repeating what it was told. **Nothing enforces that, and it is not described as though something does** — the board refuses to publish anything shaped like a verdict and the paragraph above the list instructs the seat, but an instruction is not a mechanism: nothing checks that a returned finding cites a line in this diff, carries evidence independent of the hint, or differs from the text it was shown. The gap is deliberate on #67's instrument-before-the-gate rule — per-hint ids, an overlap check at judging time and a record of what each round was shown are what would close it, and they want a few dozen cycles of this shipped before anybody designs them. What IS mechanical is `panel_core._one_line`: a hint's title and detail are model output from OTHER PRs quoted into a prompt that instructs a model, so every rendered field is flattened to one line, stripped of control characters and capped, and `line`/`pr`/`severity` are type- and vocabulary-checked rather than interpolated. Rendered raw, a title carrying newlines escapes its bullet and can forge further `- P1 file:line — …` bullets indistinguishable from the real ones. That removes the structural attack and not the semantic one. **Confirmed only, and never `refuted`:** the first keeps out what a judge threw away, the second keeps out a finding a fixer later proved wrong (#77), which is a known-false statement and worse in front of a reviewer than an empty list. **A window rather than a count**, because what earns a hint its line is that it is recent — a quiet week should send nothing rather than reach back to fill a quota. NOT nullable: `0` already means off, so a `null` spelling would be one written value with two meanings. Off costs nothing and cannot fail, and a round with no hints sends a prompt byte-identical to its pre-#508 self. |
| `review_panel.escalate_on.unrefereed_fix` | Whether a fix pass that wrote **nothing anything can check** ends the cycle. **true**, and on. Fires when the pass that landed between the last round and this one churned at least `panel_seats.UNREFEREED_MIN_CHURN` (**4**) lines and **not one of them was production code** — all test and prose. The fifth futility rung, and the only one that asks about the SHAPE of the fix pass rather than about its consequences: the cap bounds cost, `premise_repeated` bounds one assumption patched twice, `fix_injection` asks whether the pass authored this round's findings and `new_findings_not_falling` whether the count is still falling. The measurement is `unrefereed_line_weight`'s, read for its other half: red/green ran on that pass and went red 4 of 4 and could not have caught any of the ten findings it introduced, because it asks whether a new test detects the thing it was written for — never whether that test also opens a socket, whether its assertion is sufficient, or whether it is as strong as the test beside it. **A FLAG and not a fraction, which is what makes it shippable as a gate on one cycle's evidence.** #67's rule is that an instrument earns a threshold over a few dozen cycles or not at all — the reason `guard_ratio` ships report-only — and there is no threshold here to earn: the rule is a predicate on a fact, and a predicate has nothing to calibrate. A fraction would need a number nobody has measured AND would be wrong on the commonest healthy shape there is, since a 5-line production fix carrying a 40-line regression test is 89% unrefereed and is exactly the work the panel wants. **Same two bounds as the two rungs above it**: it can only turn a `go again` into a stop, and only the round rule 1 was buying, computed off the same pre-brake state so a round over all three records all three. `fix_injection` owns the `reason` when both fire — a rate naming the pass as the AUTHOR of this round's findings is the more specific truth than a fact about its shape — and both veto lines are on the record. **What it buys over `fix_injection`:** that rung needs four new findings AND a rate over the threshold AND a readable range, so a pass that wrote only tests and drew three findings sails past it; this needs the range and four churned lines. It shares one blindness with it — both read the fix range, so a rewrite #504 cannot rebuild disarms both, leaving `new_findings_not_falling` as the only rung a rewrite cannot touch. **The honest case against, recorded rather than argued away:** a round whose only finding is "this branch has no test" gets a pass that is legitimately all test and this ends the cycle on it. The round it removes is a round that would have reviewed those tests, the rule can only stop a cycle so the worst case is one fewer round with a veto line saying why, and `false` switches it off in one line. #507's constructive pass follows it like the other rungs. Recorded every round as `round_stop.unrefereed_fix` whether it fired or not, with `armed` apart from the counts and `over` apart from `fired`. |
| `review_panel.max_fix_growth_chars` | The ABSOLUTE half of that same ceiling (#492): chars the PR may **grow** past the size the cycle's first round read it at, with the cycle stopping on whichever of the two is crossed **first**. **30,000**; `null` switches this half off and leaves the multiple, and `null` on both is no growth check at all. A pure multiple hands its rope out in proportion to the starting size — at 3.0x a 113-line PR may grow ~226 lines and a 2,000-line one may grow 4,000, so the loosest allowance goes to the case most in need of a ceiling. Chars rather than lines because the multiple beside it already divides `pr_chars` by `pr_chars`, and two halves of one ceiling read off two different measurements is #298's defect one level up; 30,000 is ~380-450 churned lines at PR #188's own measured 66 chars a line, which stops #188's +536 and #236's +1,954 with margin. Both are ceilings, so the pair can only ever **tighten** — nothing it lets through would have been caught by the multiple alone. A repo that wrote `max_fix_growth: null` before #492 gets a `config_notes` line saying the other half is still in force and naming the key that finishes the job. |
| `review_panel.max_fix_guard_lines` | The **guard** half of that ceiling, and the only one measured per **fix pass** rather than per PR (#618): lines of test and prose ONE pass may churn. **`null`** — and that is **unset**, not off. **The measurement**, on `lexray#1780`, five rounds: `guard_ratio` read 2.21 → 2.19 → 2.13 → 2.09 → 2.02 while source went 476 → 941 and test went 883 → 1,632. **It fell monotonically through the runaway it was watching**, because a proportion cannot tell "this change is well guarded" from "this change and its guards are both running away" — the runaway moves numerator and denominator together. A `max_guard_ratio` would have fired on none of those five rounds and would fire at round 1 on a heavily guarded PR that never churned: wrong in both directions, which is why `guard_ratio` stays report-only and there is no such key. The per-round **delta** sees the event — rounds 2-5 wrote 380, 205, 205 and 58 guard lines against 177, 116, 114 and 58 production lines. **It does not bank**: each round reads the churn of its own fix range and nothing earlier, so a quiet round cannot fund a loud one, which is the case a per-pass ceiling exists for. The count is `panel_seats.referee_split`'s `test + prose` over that range — the same bucket `unrefereed_line_weight` prices, so the budget and the ceiling count the same lines — and it is mechanical, never a forecast. **Why it is still `null`:** one cycle is not a calibration. Its quiet round wrote 58 guard lines and its loud one 380, and a threshold between them is a number chosen to fit one PR with its argument written afterwards, which is exactly what #67 refuses. So the count is taken and published every round (`round_stop.guard_churn`, and the `prod`/`test`/`prose` columns in the round table) and nothing fires until a repo writes a number it can defend. `0` is refused — every healthy pass carrying a regression test crosses it, and `null` already spells what an operator writing `0` meant. |
| `review_panel.escalate_on.guard_lines` | Whether crossing the ceiling above **ends the cycle**, or is only reported. **false**. Two keys rather than one, and the split is the whole design: #67's rule is that an instrument earns a gate over a few dozen cycles or not at all, so a repo that writes a ceiling gets it **measured and reported** and has to say so a **second time** before a round ends on it. `max_fix_growth` ends a cycle on #188 and #236; this has `lexray#1780` and nothing else, so the weaker action goes first and the stronger one is a flag away rather than a release away. A flag and not a number, on `escalate_on.unrefereed_fix`'s precedent — the threshold is `max_fix_guard_lines` and writing it twice is two places for it to disagree. Armed, it is `round_stop`'s **sixth** rung and takes the same two bounds as the four beside it: it can only turn a `go again` into a **stop**, and only the round rule 1 was buying, so it never cancels the repair round for a P1 an earlier round raised. That is deliberately narrower than `max_fix_growth`, which the caller applies unbounded. It joins `panel_propose.PROPOSE_ESCALATIONS` like every other built rung, and it is the one where the fan-out's question is nearly its own answer: the pass wrote more guard work than the findings asked for, and each seat is asked what the **smallest** change resolving its findings would be. Armed with the ceiling at `null` there is nothing to fire on, and the round says so in `config_notes` rather than leaving it to be discovered. `false`/`null` are one value. |
| `review_panel.threshold_by_severity` | How many **distinct members** must independently raise a finding at a given severity before it is this round's work (#78). `{"P3": 2}` reads "a P3 one seat raised is reported, not fixed"; a band the mapping does not name needs one seat. **`{}`** — off, and it is the shipped value. **The reviewer-side half of the convergence problem**: every other brake here acts on a finding already accepted as work (`low_severity_fix_lines` bounds the spend, `unrefereed_line_weight` prices where it lands, `max_fix_guard_lines` bounds one pass, #616 makes a fixer name the consumer first), and this one asks whether it should have become work at all. **The evidence**: of the eight findings #78 adjudicated by hand on 2026-08-20, the four refuted were each single-seat and every multi-seat finding was real. **Why it still ships off**: `32-F01` in that same table was solo and real, #64's round 1 was a panel of ONE (#68) where a threshold of 2 discards the round entire, and judge confirmation demonstrably does not filter false positives — on `lexray#1780` round 3 a P2 was seat-raised AND judge-confirmed and was still wrong, and complying with it introduced the entitlement leak round 4 found. Eight findings on two PRs is an observation, and #67's rule is that an instrument earns a gate over a few dozen cycles; the instrument is already published (`reviewers` on every finding, and the ⋆consensus notation since #62), so measure before writing a number. **IT IS THE ONE DIAL HERE THAT CAN HIDE A REAL DEFECT, so the bound is in code and not in the default**: `panel_seats.Dials.corroboration_applies` refuses a threshold on any severity at or above `round_trigger_floor`, and on `P1`/`P2` at any floor (`panel_core.BLOCKING_SEVERITIES`, read from where `round_stop` rule 2 reads it). A repo writing `{"P1": 2}` gets 1 applied and a `config_notes` line saying so. Two consequences: a solo genuine P1 reaches the fixer exactly as it always did, and anything a threshold **can** stand down is a finding `round_stop`'s rules 1, 2 and 3 already ignore — so it cannot hold a cycle open and `round_stop` needed no new parameter. **Reported, never suppressed**: the finding keeps its master verdict and its board row, is printed under its own heading with the count that stood it down, and carries `below_threshold` and `seats_required` in the payload. `1` at a band is legal and is the identity; `0` is refused. |
| `review_panel.reviewer_scope` | What a reviewer is asked to look for: **`diff`** (defects in the change and its seams; anything outside it is an observation) or `repo` (the pre-#165 wording — "search the codebase, don't just review the diff"). Not how hard anyone looks: every dimension stays in the prompt and a code-reading seat still reads the callers. |
| `review_panel.require_failing_test` | A finding must carry a reproducible failing test to block. **false, and read-only**: the reviewer-emitted test artefact it needs is not built (#92 — a reviewer emits a test and never runs one; #114 — it must be shown RED pre-fix). Setting it `true` is recorded, reported, and enforces nothing, and the round says so in `config_notes`. |
| `review_panel.max_rounds` | The round cap, as a repo setting. **6**, as of 2026-08-30, up from 2 (#621, taken per PR review #626). `panel.py --max-rounds` still wins over it, and neither wins over the BOARD: a value the board states is a ceiling nothing below it may exceed (#55). This wins over the built-in constant. **The cap is a backstop against running forever and not a convergence mechanism**, and 2 was being asked to be both: the metric this epic is judged on is the share of cycles ending in a confident dry round, and a cycle that ends on the cap has not produced one — it has produced a fix nobody read and a remainder handed to somebody. The evidence is lexray#1780: five rounds, to-fix counts 5 → 7 → 12 at a P2 floor, both remaining P1s caused by the fix pass before them, the PR growing from +1529/18 files to +2891/27, and 1,313 lines written by fix passes after round 1 of which 848 were test and doc. Nothing there was converging and a cap of 2 would not have converged it — it would have shipped round 1's fix unread. What ends a cycle from here is the `escalate_on` rungs above, `fix_injection` first. `2` restores the 2026-08-22 setting (round 2 is what caught a defect *created* by round 1's fix on #236) and `1` the 2026-08-20 one; any cap below 3 returns those three rungs to annotations on a round that was ending anyway. `harness_rules.DEFAULTS` carries the whole argument. |
| `review_panel.escalate_on` | #78's reserved matters — the decisions the process may not take on its own. Four are built, and the two above are the volume/attribution pair. `premise_repeated` (**2**) is the OCCURRENCE a declared premise is stopped on: the second time a fix is written against one premise, stop. `premise_undecidable` (**true**) stops on the FIRST declaration that answers `--premise-decidable no` — the property the fix asserts is not observable in the runtime the assertion runs in, so every fix for it is an approximation and no number of rounds converges (#491). The asymmetry is the point: one counts because a single declaration is not evidence, the other reads a fact about the runtime that a repeat cannot make truer. It exists because the counter is structurally blind to proxy-replacement — a fixer swapping one proxy for a better one declares a genuinely different premise each round, and one measured cycle declared four that no two of matched. `quorum_failed` and `judge_absent` are accepted, reported in `config_notes` and enforced by nothing. Merged one level deep, with a per-key fallback so writing one key does not switch the others off. **A refusal from either premise rung is now an escalation that LEAVES (#555):** it goes through #274's door as a `stuck` post and a `blockers` row of class `decision`, keyed on the premise rather than its wording — so two premises on one PR are two rows and a restatement re-raises the first — and the report names the partition the fixer is about to act on, listing the findings the premise explains under *write no patch for these* against the independent half that still gets fixed. A declaration that named no findings is told it has stated no partition at all. Only the refusal announces; the ordinary declaration that runs before every fix pass still touches no network. |
| `review_panel.propose_on_escalation` | On an escalation, ask each seat that still has outstanding findings on this PR one question: **given these findings of yours, what is the smallest change that resolves them?** **true**, and on. It is NOT a fifth `escalate_on` rung and is deliberately not filed inside that block: every key there answers *does this end the cycle?*, and this one ends nothing, extends nothing and moves no verdict — it decides what an escalation ARRIVES WITH. The hole it fills is #507's: every seat returns findings, which is the right contract for an ordinary round, but on a cycle that will not converge the fixer is inferring the reviewer's INTENT from a criticism and guessing at a change that satisfies it — and that guess is what the next round reads. 128 of 201 new findings across seven PRs were created by the fix immediately before them. **On escalation, not every round**: it fires where an `escalate_on` rung FIRED (`fix_injection`, `new_findings_not_falling`, `premise_repeated`, `premise_undecidable` — `fired`, never merely `over`), so it costs one fan-out on a PR whose cycle was already ending badly and nothing at all on a healthy round. The round CAP is not an escalation and does not trigger it: a cap is a cost bound that ends healthy cycles and diverging ones in the same place. `--ask` (#129) is the machinery and the wrong question — it adjudicates a premise somebody already wrote, and here nobody has, because the whole problem is that the fixer does not know what the claim should be — so `panel_propose` reuses the fan-out and reuses neither the question nor, deliberately, the **tally**: four seats proposing four incompatible changes is the most useful answer a stuck cycle can get, and a verdict struck over them would average away the one thing worth collecting. Nothing is reconciled and no agreement is computed. **A proposal is not a finding**: it enters no leaderboard, no cross-round defect chain and no severity floor, it reaches `round_stop` through nothing, and the board's `extra="ignore"` ingest drops the key — a reviewer that proposes is not thereby right (#79). **It cannot make a review look cleaner than it is**: it runs after `stop`, `reason`, `veto` and `confident` are final and writes to none of them, and its section is printed UNDER the veto lines rather than over them. An escalated finding is shown and MARKED as the human's to answer, never as work for a fixer. Recorded every round as `proposals`, with each seat's verdict (`change` / `no small change` / `cannot tell`), its one-line proposal, where, which of its findings it claims to resolve, and the exact listing it was shown. `false` switches it off — and a round that then escalates says so in `config_notes`, because a repo that declined this must not be indistinguishable from one where nothing fired. |
| `review_panel.budget` | #55's spend ceiling: `tokens_per_day`, `runs_per_day`, `tokens_per_pr`, `runs_per_pr`, `fleet_tokens_per_day`. **Every one `null` — no ceiling — which is what landing it did to every repo.** Checked against `GET /review/spend` *before any seat is dispatched*, and a repo at its ceiling is refused through the pre-flight's own machinery: printed, recorded with `reviewed: false` and a per-seat `ran: false`, posted to the PR, and carrying `stop_confident: false` so a budget stop cannot read as convergence. Tokens are input + output (`/review/stats`' `billable`); the run ceilings are what still binds where nothing was instrumented, and `runs_per_pr` is the one that binds a caller which renumbers its rounds. `0` is a real value and means nothing may be spent; `--force` overrides none of them. Board-settable, and the board's value beats this file's — a ceiling a repo can raise from inside itself is decorative. |
| `review_panel.budget_window_hours` | What "per day" means for the two rolling ceilings. **24**. A dial rather than a constant because the window and the ceiling are one decision: halving the window halves the ceiling. The per-PR ceilings have no window — they are about one pull request's whole cost, and a rolling one would let a PR reviewed daily for a fortnight stay under a ceiling it passed on day two. |
| `review_panel.local_suite` | The repo's **own test suite**, run once on this box before the seats are dispatched, when GitHub CI has nothing to say about this commit (#548). `null` — **off**, and off is what every repo gets until it writes this, because it is the one key in the file that names something to EXECUTE. A string is one command; a list is several, run in order (`make test`, plus a DB-backed target where the box has the service). Each is split with `shlex` and run **without a shell**, so the list is where "and then" is spelled. **Read from `origin/<default branch>` in both modes**, never from the working tree — unlike every other key here — so a change to it takes effect when it lands, not when it is pushed. **Not board-settable** either: a dial for a command line would run code on every box in the fleet with one POST. See below. |
| `review_panel.local_suite_timeout` | Wall clock for the **whole** declared run, in seconds. **900**, and at most 3600. Per-command would bound nothing — four commands would be four times the number in the docs. The bound fails in the honest direction: a run that does not finish is reported as not having finished and vetoes. |
| `loops` | `dependabot_lander` / `stacked_driver` / `issue_executor` — which loops may run. |
| `issue_pickup` | What a loop may pick up **of its own accord** — on/off, an author allowlist, a label allowlist, human triage, and the labels that refuse outright. Every default refuses; see below. |
| `issue_filing` | How much a loop may **file** — a per-run cap, a duplicate search, and whether an unattended run may file at all. See below. |
| `epic` | Epic-driver settings — see below. |
| `in_flight.max` / `in_flight.min` | How much work may be in flight in this repo at once (#337). **Both `null` — no bound at all**, which is what landing it did to every repo. The count is CLAIMS, fleet-wide (`GET /claims/in-flight`): live `work` claims naming an issue or a PR here, whoever holds them. `max` is enforced at the checkout by `qb-admit`, which `create-worktree` calls before it takes the claim, so a full window refuses where a refusal costs nothing — the same refusal `--require-claim` already makes when the issue is taken. `0` is a freeze, not a typo; a value that is neither null nor a non-negative integer is reported and NOT enforced (failing open beats a fleet throttled by a typo). `min` is the floor and **nothing reads it** — it is where #232's planner discretion will be configured, and that needs the changed-file overlap (#101/#287) and a planner that does not exist. |
| `preland.disabled_checks` | Checks `preland.py` must not run, by name. Empty by default — every guardrail it can detect, it runs. A name nothing answers to is a **hard error**, not a warning; see below. |

### The appetite gates — `issue_pickup` and `issue_filing` (#85, #86)

An autonomous loop needs limits in **both** directions, and neither existed. A
watcher that acts on whatever it finds "actionable" works the backlog in whatever
order it happens to enumerate it — not an order anybody chose. And a loop applying
a consistent standard with no ceiling produces a backlog nobody reads: nine issues
were filed here in one day, every one a response to something real, which is what
makes it a risk rather than a bug.

The policy lives here rather than in the issue watcher (#63) that first wanted it,
because that watcher is one consumer — see [Issue watcher](#issue-watcher-issue_watchpy--63),
which reads these blocks and adds none of its own. The PR watcher (#54), the epic driver and
whatever acquires an appetite next want the same brakes, and a brake implemented
per-consumer is three brakes that will disagree. `appetite.py` reads these blocks
and answers; nothing else should re-implement the question.

```json
"issue_pickup": {
  "enabled": false,
  "only_labels": [],
  "allowed_authors": [],
  "require_human_triage": true,
  "agent_actors": [],
  "skip_labels": ["needs-human/*"],
  "skip_when_unlabelled": true
},
"issue_filing": {
  "max_per_run": 1,
  "require_dedup_check": true,
  "unattended": false
}
```

**Every default refuses, and that is the design.** Acting is what needs
justifying; refusing does not. `enabled` is off, so no repo acquires an appetite by
upgrading. `only_labels` and `allowed_authors` are empty, and empty means **nothing
and nobody qualifies** rather than everything and anyone: turning the gate on is
one decision and saying what may come through it is another, and a repo that has
made only the first has not said "anything".

**`require_human_triage` is the load-bearing line.** An allowlist of labels
authorises nothing if the agent can apply the labels — #78's `judge_model` problem
one level out. So the check does not ask whether the label is *present*; it reads
the issue's label **events** and asks who put it there. A Bot-type actor never
counts, and `agent_actors` names any further logins that do not. The hole it leaves
is worth stating rather than discovering: an agent authenticating as its human's
own GitHub account is indistinguishable here from that human. The check is sound
against bots and against a named agent account, and is not a substitute for agents
having their own identity.

**`allowed_authors` is an allowlist, not a filter, and the distinction is the
whole mitigation.** `prisonblues/quarterback` is public, anyone can open an issue,
and under a watcher that text becomes the instructions for an agent with a full
shell. A filter is a list of the phrasings somebody already thought of; the attack
is the phrasing nobody thought of.

**`skip_labels` refuses what no diff can settle.** Some issues cannot be auto-fixed
well, and the failure is not that the agent writes bad code — it is that it writes
*plausible* code for a question nobody has answered yet. A UI issue reads as
actionable to any "is this actionable" test: there are files, there is an obvious
place to put a change, and a diff will appear. What will not appear is the design
decision the issue was waiting on, and a reviewed, tested, merged answer to the
wrong question is worse than nothing — it is now the thing to argue with.

The default list is **#279's closed vocabulary** (`needs-human/decision`, `taste`,
`ui`, `environment`, `auth`, `other`), matched as a glob so the `other` escape
hatch can grow the vocabulary a word without this list going stale by letting the
new class through. Entries are globs *or* plain names, so a repo listing `design`
loses nothing.

**A label a person applied, never a classifier.** An agent could be asked to judge
whether an issue needs human judgement, but that is self-referential and wrong in
the expensive direction: the issues most needing a human are the ones a model is
most confident it understands. #64 is the measured shape — three of six confirmed
findings were conditionals from a reviewer that had *declared it could not assess
the condition*, and it raised them anyway.

`skip_when_unlabelled` is the safe end and the default: an unlabelled issue has
not been triaged by *anyone*, so nothing has established which class it is, and
"no signal" must not read as "no objection".

**Two entry points, because there are two questions.** `pickup_verdict` is the
whole gate and answers *may a loop CHOOSE this issue, with no human having named
it*. `refusal_verdict` is the `skip_labels` half alone and answers *regardless of
who chose it, has a person marked this as one no diff can settle*. `epic.py
--execute 42` names an epic on the command line and the human typing it is the
authorisation, so the epic driver calls the second and not the first: a
`needs-human/ui` label a person applied does not stop meaning what it says because
the issue arrived inside an epic, but `skip_when_unlabelled` — a statement about
selecting out of an untriaged backlog — deliberately does not apply to it. Gated
sub-issues appear in the plan as `stage: blocked` / `action: skip-blocked` with a
reason naming the label and the setting, and their `doable` stays `null`: no
doability ruling was made, because we declined to ask for one, and claiming a
verdict never obtained is the fabrication the triage reason lines exist to
prevent. The gate runs **before** the judge, so a refused issue costs nothing.

**A refusal names the setting that refused it, and the CLI exits 3.** "No" and
"misconfigured" look identical from a caller's side, and an agent that cannot tell
them apart routes around the gate instead of fixing it.

**`max_per_run` persists its tally** under `$LOOPS_STATE_DIR`, keyed by `--run`:
the gate is a CLI invoked once per candidate, so a count held only in the process
it constrains is self-reported rather than enforced. Without a `--run` id only the
current process is counted, which is the weak form and a caller should know it is
choosing it.

**`require_dedup_check` searches the title's own words**, not the whole title. The
duplicate that matters is the one somebody phrased differently, and an exact-title
check would pass every single time and read as a working gate. An *unsearched*
check and an *empty* one are the same value in Python and are deliberately not the
same answer here. If `gh` search is unavailable the call raises rather than
returning "no duplicates" — an outage must not silently open the gate.

A malformed value is a **hard error**, like `preland.disabled_checks`. Unlike a
misspelled *key* — which leaves the setting at its safe default — a malformed
*value* would leave the gate reading as configured while doing nothing: an open
door that looks shut. `skip_labels`, `only_labels`, `allowed_authors` and
`agent_actors` must each be a list of strings, `max_per_run` a non-negative
integer or `null`, and **every switch here must be a real JSON boolean**. A bare
string where a list belongs is the subtle one, because a string is *iterable*: it
silently becomes a list of single characters, which for `agent_actors` fails
**open** — the named agent account stops being recognised as an agent, and the one
setting whose job is to stop self-approval stops doing it. A quoted `"false"` is a non-empty string and therefore *truthy*, so it
would turn the gate on; `harness_rules` already checks overlay *values* and not
just their names for exactly this reason, and every default in these two blocks
is closed, so the direction of that mistake is always "gate silently open".

Two more places where a failed read must not read as an allow. An **unreadable
filing tally** — truncated JSON, a hand-mangled count — refuses rather than
reporting zero, because zero hands a fresh budget to a run that may have spent
it; the tally is written via `os.replace` so a crash mid-write cannot produce
that state in the first place. And a **title with no searchable words** counts as
*unsearched* rather than *searched and clean*, or `require_dedup_check` could be
defeated by choosing a title of short words.

A `labeled` event that names **no actor** — a deleted account, or the API
omitting the block — does not count as human triage either. "Somebody applied this
and we cannot see who" is exactly the state the check exists to distinguish.

`human_triaged` reads only the **current** application of each qualifying label.
Scanning for any historical `labeled` event by a human would let a
removed-and-re-applied label inherit an authorisation given to a different
application of it: a person applies `p0`, someone takes it off, an agent puts it
back, and the agent is now working under the person's signature.

Two limits stated rather than papered over. `max_per_run` is enforced across
invocations only when a `--run` id is given, and an allow **says so** when it is
not — otherwise the weak form and the strong form print the same "within budget".
And the read-increment-write of the tally is not atomic across processes, so two
filers sharing a run id can both pass a cap of 1 before either records; it bounds
an enthusiastic loop rather than a concurrent one.

```bash
python3 appetite.py pickup 85 --repo quarterback          # may I work this?
python3 appetite.py file --title "..." --run $SESSION      # may I file this?
python3 appetite.py file --title "..." --run $SESSION --record   # I did
```

`auto_merge`:
- `none` — never auto-merge; always stop for a human.
- `dependabot_patch_minor` — auto-merge only dependabot patch/minor on green CI. **(default)**
- `all_green` — auto-merge any PR once gates pass. Not recommended for a repo with users.

`reviewers` — each block has `enabled`; LLM reviewers also take `model`.
- `claude.model` — set to a **different** model than the PR author for cheap diversity.
  Same-model self-review is the weak case (#117).
- `codex` — different-vendor LLM reviewer (the high-value diversity add).
  `model` and `effort` (`low|medium|high|xhigh|max|ultra`) both default to empty,
  meaning *the codex CLI's own defaults*. Pin them per repo to experiment, e.g.
  `"codex": {"enabled": true, "model": "gpt-5.6-luna", "effort": "high"}`.
  Unlike `opus` (a floating alias), codex slugs are versioned build names that
  get retired, and a slug the **installed** CLI is too old for is refused by the
  API — so check `codex --version` before pinning a new one. That failure is
  loud rather than silent: the report names the model each reviewer used and
  flags a lost reviewer next to the reviewer list, not in a footnote.
- `antigravity` / `pi` / `grok` — the third, fourth and fifth vendors, all **off by
  default**: each is a workstation-only package authenticated against a personal
  account, so a repo asks for them rather than every box inheriting a reviewer it
  cannot run. Enable per repo, or ad hoc with `--reviewers claude,codex,grok`.
- `grok` — xAI's CLI. **Pin `model`**: grok's own default is whatever `[models]
  default` says in the user's `~/.grok/config.toml`, which on this fleet is an
  OpenRouter route rather than the first-party model — an unpinned seat reviews on
  a different model, through a different account, than the report names. `grok
  models` lists what is servable. `effort` is `low|medium|high|xhigh`, validated by
  the CLI before the turn starts, so a typo costs a startup rather than a turn.
  Unlike `agy` its prompt travels in a **file**, so the argv ceiling below does not
  apply to it.
- `sonarqube` — deterministic static-analysis **hard gate**. `project_key`,
  `organization` and `host` are non-secret and belong here; the token does not.

`review_panel.judge_model` (default: **`sonnet`**) — the model that adjudicates what
the seats found. It is deliberately not `opus`, which is `reviewers.claude.model`: the
adjudicator should not be the same brain as a seat it rules on, which is the note above
about `claude.model` applied one level up. The evidence is one day's worth and small —
on 2026-08-15 four judge-confirmed findings turned out plainly wrong on inspection, and
all four were raised by claude and confirmed by an `opus` judge — so the mechanism is the
argument, not the sample.

**Why `sonnet` and not `fable`, which this started as.** The first version chose `fable`
on a tie-break — the judge's job did not get easier, and `clamp_model` states this repo's
preference as failing toward capability. Both review rounds then attacked that from two
directions. `fable` is **not universally available**: it wants a recent Claude Code, is
not on every plan, can be organization-disabled and may want credits, and the panel does
no availability preflight — so an installation without access gets an unadjudicated round
every single time (loudly: `judge_skip` feeds the coverage veto and the report says "all
findings KEPT unjudged", so it degrades visibly rather than silently — but it degrades).
And it is the **priciest model in the panel**, on a run that happens for every reviewed
PR, in a file whose `skip_title_patterns` exist because one release-merge came to about
$750.

A premise attacked twice is the one to delete rather than patch. The requirement is
**independence** — the adjudicator must not be the brain that raised the finding — and
`sonnet` meets it outright: it is not `reviewers.claude.model`, it runs wherever the CLI
runs, and it is *cheaper* than the `opus` judge it replaces rather than dearer. The
capability argument was never evidence either way: the four wrong findings above were
confirmed by an `opus` judge, so capability is not what was failing.

**Want the most capable adjudicator? Set `judge_model: fable`.** That is what the key is
for. What a default should not do is make that trade on every repo's behalf, silently.

This is only the default. **Pinning both keys to the same model still works and says
nothing** — the enforcement half (`judge_independent`: refuse to run when the judge's
model matches an enabled seat's) is [#78](https://github.com/prisonblues/quarterback/issues/78)
and is not implemented.

`review_panel.max_diff_chars` (default: **none — the whole diff**) — how much of
the diff each model is given. Override per reviewer with
`reviewers.<name>.max_diff_chars` and for the master with
`review_panel.judge_max_diff_chars`; both inherit the panel value when unset. Since
v2.28 that budget buys the review target first and the PR context with whatever is left
(see rounds, below), so on a scoped round it is no longer the target that a tight budget
cuts. On a scoped round the budget also covers the brief and the section headers around
the diff — a little over a kilobyte — because the ceiling that matters is the model's
context window, and the prompt is what lands in it. Under `pr` scope it means chars of
diff, exactly as it always has.

`review_panel.round_scope` (default: **`increment`**) — whether a round past the first
reads the fix commit or the whole PR again. `pr` restores the pre-v2.28 behaviour for a
repo whose PRs are small enough that re-reading them costs nothing. Anything else is a
typo, and lands in `config_notes` saying so rather than quietly turning scoping off.

There used to be a 60,000-char default, and it was a fossil: prompts travelled in
argv, where Linux caps one element at 128 KiB, so a budget was mandatory. Since
they moved to stdin the only ceiling is the model's own context, and 60k chars is
about 15k tokens — an order of magnitude under every reviewer the panel runs.
Truncating when nothing forces it is not a saving, because a truncated reviewer
cannot notice that it was truncated: it reports on a prefix with full confidence,
and the errors that produces are *false positives* ("this migration is
syntactically incomplete" — it was cut mid-file) that then cost a judge call and
a fixer's attention to disprove.

Set one only if a model you run cannot take the change. Any positive value is
honoured — there is no lower sanity bound, deliberately; what surfaces a
too-small budget is that the report names **which** reviewers were truncated and
at what budget, which is worth knowing when two reviewers disagree and only one
of them saw the whole change. Only a value that cannot be a budget at all — not a
number, or `<= 0` — falls back, with a ⚠️ config line in the report saying so.

One seat is capped whatever you configure: `agy` takes its prompt in argv, so the
kernel bounds it. The panel clamps that seat to what `execve` will carry and
reports it as ordinary truncation rather than dying at exec — which is what it
used to do, reporting "LLM reviewers ran: none" as a clean review.

`review_panel.refuse_over_cap_multiple` (default: **3**) — how many times over the
**tightest seat ceiling** a diff may be before the round is *refused* instead of
truncated; `0` switches the refusal off and keeps the manifest, and `0` is the only
spelling of off (`null` and an absent key mean "use the default", here as everywhere
else in this file). `review_panel.move_shape_ratio` (default: **0.9**) — what fraction
of the larger side of a diff must be relocated text before the change counts as a
*move*; a fraction, so values above `1.0` are rejected with a `config_notes` line
rather than silently making the threshold unsatisfiable.
`review_panel.manifest_moves` (default: **true**) — review a move-shaped over-ceiling
diff as a manifest rather than as content; `false`, `"false"`/`"off"`/`"no"` and the bare
`0` all switch it off, and `true`/`1` all leave it on. All three keys are validated: a
value that cannot be what the key means falls back to the default and *says so* in
`config_notes`. `false` on a *number* is rejected rather than read as 0 — and the note
then names the right way to say "off" **for that key**: `refuse_over_cap_multiple: 0`
switches the refusal off, while `move_shape_ratio` is a threshold with no off at all (a
ratio of `0` turns the feature all the way *on* — every diff with one relocated line
becomes a move), so its note points at `manifest_moves` instead. All three are the
pre-flight verdict, below.

### Thoroughness against convergence — #165's seven dials, and #297's eighth

The panel had one behaviour and no dials, and the measurement says that behaviour does
not converge. Across seven PRs panelled on 2026-08-16, the last round of each raised
201 findings no earlier round had, and **128 of them — 63.7% — were created by the fix
pass immediately before it**, against a ~7% industry baseline for bad-fix injection.
Every one of those panels terminated on the round cap, each saying so in its own
output: *"a stop, not convergence."* Nine of this repo's open issues are the panel's own
deferred-finding overflow.

The severity split of that queue is P1 4.1% / P2 28.6% / P3 36.1% / P4 31.3% — about
1.2 P1s per PR, roughly what a production generator-verifier loop reports. **The signal
is calibrated; the 67.3% tail beside it is not.** These eight settings bound the tail.
None of them makes the panel look less carefully, and none lowers the bar for what a fix
round takes on: in scope, everything still gets fixed properly, with a test, and "note
it and move on" stays forbidden.

The diagnosis they act on, in four parts — the fourth measured five days after the
first three landed:

1. **The panel computed a calibrated severity and the prompts threw it away.**
   `review-pr.md` ranked findings "for the summary table only. All of them get fixed."
   → `fix_severity_floor`, which is the rule #223 and #237 already apply by hand — but
   only *at the cap*, after three fix passes have grown the change.
2. **The fixer was forbidden to scope.** Two exits, and "'Not now' is not available to
   you" → `fixer_may_defer`, plus `reviewer_scope`, which stops the review round handing
   the fixer work outside the change in the first place.
3. **The loop's termination test was fed by its own output.** `round_stop`'s rule 1 went
   again on a new finding at any severity, and from round 2 the thing under review IS the
   previous round's fix → `round_trigger_floor`, with `max_fix_growth` as the backstop for
   the case where the fix pass has already written a second change.
4. **The fix pass became most of the PR.** Measured again on 2026-08-21, five days after
   the first three landed: PR #188's 185-line feature came out of two fix passes at 721
   churned lines — **74% of the PR was review-response code** — and round 2's fix list
   was 89% below P2. The floor above is a per-FINDING rule and cannot see that: #188's
   round 1 was 408 lines of individually reasonable small fixes, each of which the floor
   correctly admitted → `low_severity_fix_lines`, a combined line budget for the band
   between the two floors, spent cheapest-first and counted rather than estimated. The
   floor sat at P3 then — the argument below for the extra tier was unchanged, and a
   budget is what that argument was always missing. **It reaches to P4 as of 2026-08-30**
   (#621), which is the same argument taken one tier further: the budget is what makes an
   extra tier cost a bounded number of lines instead of an open-ended one, so the tier
   that ballooned #236 is safer inside it than outside every rule.

`max_rounds` and `require_failing_test` are the two that change nothing on their own:
the first surfaces an existing constant so a repo can move it, the second reserves the
name for the evidence contract #165 argues is the real termination condition, and reports
that it is not built rather than pretending to enforce it.

**Every one of the eight is validated, and a bad value is a hard exit** — the mechanism
and the sentence shape `harness_rules._check_block_shape` already uses, naming the key,
the value and the accepted set. The line is drawn between an unknown KEY and a malformed
VALUE of a known one, and it is the line `_check_block_shape` already draws: an
unrecognised name may be a setting only a newer harness knows about, so it stays
warn-and-drop (`warn_unknown_keys`) and a rules file shared across a fleet of boxes that
upgrade at different times does not become a version pin; a value this harness cannot
read is not version skew in any direction, it is a typo, and `fix_severity_floor: "p-4"`
meaning "fix everything" must not quietly become the default and stop the pass fixing
P3s. Unset — missing, `null`, `""` — is not a mistake and stays silent (and for
`max_fix_growth`, `max_fix_growth_chars` and `low_severity_fix_lines`, all of which distinguish an absent key
from a written one, an explicit `null` is the off switch).

**What the round applied is in the artifact.** The report carries a
**Panel dials** line on every round, at the defaults or not, and the
payload carries `review_panel` with all eight as applied, and each finding carries
`budgeted_fix` beside `below_fix_floor` so a consumer can tell "not this round's work"
from "this round's work while the budget lasts" without re-deriving either floor.
`below_threshold` is the third of that family (#78) and answers the question neither
floor can: the finding is above every cut and is still not this round's work, because
fewer members raised it than `threshold_by_severity` asks for at its band.
`seats_required` rides beside it, because the flag alone does not say what the bar was
and the bar moves per band. Both are `false`/`1` on a Sonar hard-gate issue, which no
threshold may stand down. The
orchestrator that briefs the fixer builds that brief out of the report, so the policy
has to be readable there rather than from whoever remembers the repo's config.

### The SonarQube token

Resolved in this order, first hit wins:

| # | Source | Where it comes from |
|---|---|---|
| 1 | `$SONARQUBE_TOKEN` (the var named by `token_env`) | a login-time export (zeus does this) |
| 2 | the repo's **`.env`** | **the work-machine source** — no 1Password/sops there |
| 3 | `~/.cache/loops/sonar-<key>.token` (`0600`) | write-through cache of the `op` read |
| 4 | `op read` of `token_op_ref` | needs `op signin` once, then the cache serves |

**Never commit a token value.** `.env` must be gitignored — if it is committed,
the panel prints a loud warning on stderr and tells you to rotate, but it will
still use the token rather than failing your run.

`.env` sits **below** the real environment variable, not above it. An exported
token is a deliberate override, and a stale `.env` shadowing it would surface as
an unexplained 401 from SonarCloud rather than an obvious misconfiguration.
Nothing is lost on a work machine, where no such export exists and resolution
falls straight through to `.env`. (Same default as python-dotenv's
`override=False`.)

`.env` is read from the **working tree** in every mode, unlike `.harness-rules`.
That's not an exception to the two-ref rule: `.env` is gitignored, so it exists
on no branch and a PR cannot introduce one.

`epic`:
- `landing` — `auto` suggests integration vs multi from coupling signals; `integration`
  = one epic branch → one PR; `multi` = a PR per sub-issue.
- `sub_pr_merge` — integration only. `auto` ff-merges each green sub-PR into the epic
  branch; `gate` holds each at a human merge. **Defaults to `gate`.** It is the switch
  that decides whether the review gate merges anything, and that gate has been wrong on
  its first attempt three rounds running — so it defaults closed, like every other
  setting that lets an agent act unattended. Turning it on is one line.
- `auto_finish` — on a `/fix-issue` that pushed nothing, commit+push salvaged work
  rather than failing the issue.
- `executor_worktree_args` — extra flags for `create-worktree` (e.g. `["--no-docker"]`).
- `min_free_mb` — preflight warns below this `MemAvailable`.
- `model_ceiling` — highest tier a sub-issue may be implemented at when `--model` is
  not passed (`sonnet` < `opus` < `fable`; default `opus` — the ordering and the
  off-switch are pinned against `epic.MODEL_TIERS` in
  `harness/loops/tests/test_epic_model_ceiling.py`, so this sentence cannot go stale
  quietly). The triage judge runs here and routes each issue to this tier or lower.
  Anything outside those three turns model routing off, as does an explicit `""` or
  `null`. It used to fall back to `review_panel.judge_model`, which is a different
  question and now deliberately has a different answer — see `review_panel.judge_model`
  earlier in this file.
  > **If you previously set `review_panel.judge_model` to control what an epic spends,
  > set `epic.model_ceiling` too.** That key was load-bearing for two things and only
  > one of them was documented. A repo that set `judge_model: sonnet` to keep unattended
  > implementation cheap kept its custom judge across this change and picked up the new
  > `opus` ceiling — an unannounced rise in what an unattended sub-issue may spend. Repos
  > on the default are unaffected, which is what the CHANGELOG's "epic behaviour is
  > unchanged" means and all it means.
- `migrations_dir` — where alembic revisions live, for the linear-heads guard.
  **Leave it alone in a repo without alembic.** The default doesn't exist there, so the
  guard returns `None` and no-ops. Setting it to `""` breaks that: `Path(repo)/""` *is*
  the repo root, so an empty value makes the guard think migrations exist.

## Branch base — the one subtle rule

Repos are not uniform: dependabot may target `main` while features land on `test`.

- **Acting on an existing PR** (lander, stacked driver, panel): read `baseRefName`
  from the PR. Never reset it.
- **Creating a new PR** (issue executor): use `executor_pr_base`.

## Loop A — dependabot lander (`lander.py`)

`python3 ~/.claude/loops/lander.py` (dry run) / `--execute` to act. Defaults to the
cwd's repo; `--repo` takes a path or a name under `~/source`.

- **Green patch/minor → squash-merge** (within `auto_merge` policy).
- **Security → escalate** — left for a human; never auto-merged.
- **Red CI → fix-in-worktree:** a plain `git worktree` on the *existing* dependabot
  branch (not `create-worktree` — the gate is CI, not a local app run), a headless
  edit-only `claude -p` fixes the bump breakage, and the loop pushes back so CI
  re-runs. Removal is in a `finally`. **Idempotent:** if the branch tip is no longer
  a dependabot commit, a fix was already attempted — left for a human.
- **The agent's account of itself is kept.** "Agent made no edits" is an observation
  with two opposite explanations — nothing needed fixing, or the agent was stopped
  from fixing anything — so the line now quotes its last word. A run that exited
  non-zero, or exited 0 having printed nothing with a cause on stderr, is reported
  as a *failure* and nothing is pushed; the sweep carries on to the other PRs
  rather than raising. A run that said nothing anywhere but left real edits has
  them pushed: the staged diff is the direct evidence, and CI is the gate.

## Reviewer panel (`panel.py`)

`python3 ~/.claude/loops/panel.py --pr <n>` (report) / `--post` (also comment on the
PR) / `--json` (the run as JSON on stdout — nothing else, progress goes to stderr) /
`--round <r> --max-rounds <N> --baseline <earlier round's --json-file>` (a re-review that
knows what the earlier rounds raised, and where it sits in the caller's cycle) /
`--scope pr|increment|auto` + `--since <sha>` (what a later round reads — see below;
`auto` is the default and needs no flag) /
`--ask "<premise>"` (one question to the seats instead of a review — see below) /
`--force` (review the diff as content even when the pre-flight verdict refuses the round
or rules the change move-shaped — see below).

Read-only, so it runs in **any** repo — an unconfigured one just uses the defaults.

- Runs the repo's **enabled** reviewers in parallel over the PR diff: SonarQube (HARD
  quality-gate pass/fail), Claude (SOFT, read-only), Codex (SOFT, different vendor).
- **Skip patterns:** PRs matching `skip_title_patterns` are skipped entirely — but that
  is still a payload, marked `reviewed: false`, on `--json` *and* in `--json-file` (an
  empty stdout, or a missing baseline, would read as a clean PR).
- **Pre-flight verdict** (see below): before any seat is dispatched, the panel rules on
  whether the round is worth running and whether the diff or a *manifest* of it is what a
  seat should read. There is still **no diff-size de-minimis** — nothing is skipped for
  being small, and nothing is refused against a ceiling the repo or the kernel did not
  already declare.
- **Master judgment, no consensus gate:** a master reviewer judges every finding on
  its merits. A real defect flagged by only ONE reviewer is still fixed — agreement
  shows as a `⋆consensus` confidence marker, never a filter. Only clear false
  positives are dismissed, with a recorded reason. If no judge is available, nothing
  is suppressed. Agreement takes two reviewers, so on a panel of one the marker is
  not merely absent but unavailable, and the report says which of those it means.
- **Every member runs in its own empty sandbox repo**, not in whatever directory the
  panel was launched from — a seat that inherits the caller's shell is a seat whose
  participation nothing configures and nothing can reproduce, and codex, which refuses
  to start outside a git repository, was lost exactly that way. The sandbox is `git
  init`ed so codex is satisfied, and *empty* rather than the checkout for two reasons:
  a headless CLI reads its project configuration (CLAUDE.md, `.claude/settings.json`,
  hooks that execute) from its cwd, and the checkout is on whatever branch it was left
  on — never the PR's code, which the panel reads as a diff and never checks out. A
  seat pointed there can quote a different branch as the code under review. The
  members need no working directory at all; they need a reproducible one.
- **The seats can read the PR's code, per repo, on by default** (#113). Each seat that
  can take it runs in a checkout of the PR **at its head**, fetched from GitHub's
  tarball endpoint — never from `cfg["path"]`, which is the main checkout on whatever
  branch it was last left on and is the failure #75 measured. Why it is on: the
  blindness was expensive and it did not merely lose findings, it manufactured wrong
  ones. On PR #160's round 1, nine of nineteen veto lines were seats declaring they
  could not read a file this repo answers — all nine closed with `grep` in four
  minutes. On #64 the proposed fix *was* the bug; on #90 a P1 inferred a missing
  `--json` field from its absence in the diff when it was already there; on #123 no
  seat could see `migrations/versions/`, the tool's entire subject.
  - **Only a seat that can express "read but do not execute" gets it**, which is
    `SEAT_READS_CODE` and today means **claude alone**. #92 answered "may reviewers
    execute?" with no. Verified by running each CLI: claude names a tool set
    (`--allowedTools Read Grep Glob`, no `Bash`) and enforces a working-directory
    boundary of its own; codex's `-c` knobs only REMOVE tools and its single read path
    is the shell, so granting reads grants execution and re-opens the tool-hunt that
    once spent 99% of a run; pi's `--no-tools` is all-or-nothing over read/bash/edit/
    write; antigravity has no tool mechanism at all. A seat that cannot read keeps its
    empty sandbox — standing in a checkout it cannot open buys the instruction-file
    channel for nothing.
  - **Vendor convention files are stripped before any CLI starts** — `CLAUDE.md`,
    `AGENTS.md`, `GEMINI.md`, `.claude/`, `.codex/` and the rest of
    `CONVENTION_FILES`/`CONVENTION_DIRS`, at every depth, because a nested one is
    read too. This is a **denylist and it will rot**; that is an accepted cost where
    the contributors are your own agents and exactly why `false` is right where they
    are strangers. Symlinks are unlinked, never followed: a `.claude ->` pointing out
    of the tree would otherwise send `rmtree` at the real one. What was removed is
    named in `config_notes` and in the payload, so a PR that shipped one is
    distinguishable from a PR that did not.
  - **Recorded per seat**, which is what makes the measurement possible:
    `reviewers.<name>.code_blind` and a `code_access` block holding the setting, the
    seats that actually got it, and the files stripped. A seat that can read the tree
    while another cannot is a bigger confound than an unpinned model.
  - **Every failure degrades to the OFF posture, loudly.** A fetch that 502s, a
    tarball that will not unpack or has an unexpected shape, a copy that runs out of
    disk: the seat is blind, recorded as blind, and the round says why in
    `config_notes`. A review that would have happened always happens.
  - **A transient fetch failure is retried, a settled one is not.** Measured: five
    hand-run fetches of one sha during development returned two 502s and a 503 —
    GitHub packs a repository on demand for this endpoint and it is markedly flakier
    than the JSON API the rest of the panel uses. That matters more than the rate
    suggests, because the degrade is silent in *effect*: one note among several, and
    an ordinary-looking report. A feature that quietly stops applying a third of the
    time is worse than one that is off, because the config still says it is on. A 404
    is not retried — it is a settled answer about that sha (a fork PR, most likely),
    and `run_cli` already draws that line for reviewer CLIs.
  - **A per-repo spend cap, defaulting to uncapped** —
    `review_panel.reviewer_code_budget_usd` passes `--max-budget-usd` to the seat that
    got the tree. Uncapped by default because a number invented here would silently
    degrade reviews on repos that never asked for one, and because reaching the cap is
    not a cheaper review: the seat is lost, records a skip, and the skip vetoes the
    round's confident stop. Reaching it needs a guard nothing about the flag suggests —
    `claude` exits 1, writes its message to **stdout**, and leaves **stderr empty**, so
    `run_cli`'s stderr-based reason and stderr-based retry decision would give a bare
    "exited 1" and then repeat the attempt three times, re-burning a cap already spent.
  - **What it costs, measured** (one seat, sonnet, PR #214's own 75,628-char diff,
    run twice with only this feature differing): wall clock **922s vs 372s** (2.5×),
    input tokens **7,879,643 vs 159,520** (49×, though 97% of the larger figure was
    cached so the billed multiple is well below that), output 71,674 vs 36,364.
    Against that: `could_not_assess` went **4 → 0**, and the blind run filed a FALSE
    finding the sighted one did not — it saw a diff line mentioning `argv_capped`,
    could not tell which function it belonged to, guessed `accounts()`, and concluded
    the name was undefined. That is #90's failure mode reproduced unprompted.
    The cost lands per seat per round, and `/panel-review-pr` fans out up to four
    concurrent panels, so this seat is also the critical path. `claude` documents
    `--max-budget-usd`, which works with `--print`; bounding the hunting with it is
    the obvious follow-up and is deliberately not in this change.
  - **The judge gets the tree too, on the same terms.** It is a `claude` seat, so it
    takes the same stripped checkout, the same `Read Grep Glob` pin and the same spend
    cap — and it is the party best placed to use them, because the wrong findings #113
    was filed over were **confirmed**, not merely raised. On #90 a reviewer inferred a
    missing `--json` field from its absence in the diff and a judge with the same
    blindness had no way to check; on #64 three of six confirmed P2s were conditionals
    from a reviewer that had *declared* it could not assess the condition. Dismissing
    false positives is the judge's stated job and it cannot do it from the same diff
    that produced them. One ordering trap, pinned by a test: the tree's cleanup must
    run **after** `adjudicate`, or the judge gets a path to a deleted directory,
    degrades to an empty sandbox, and reviews blind with the setting still reading
    true — the silent failure that the degrade path's own correctness makes possible.
  - **The board stores it, rather than dropping it at ingest.** `absent`,
    `code_blind` and `argv_capped` per reviewer, plus `code_access` and
    `convention_files_removed` per run (migration `0024`), read back out of
    `GET /review/{id}` as well as written. `absent` had been sent since v2.32 and
    silently discarded, because `ReviewerIn` inherits pydantic's `extra="ignore"` —
    the same drop v2.26 records for `head_sha` and `unread_files` (#93). `code_blind`
    is the one that matters most for anything ranking reviewers: a seat that could
    open the caller and one that could not are not comparable on findings or on
    `could_not_assess`, and a leaderboard averaging them measures two different jobs.
    All columns nullable with no backfill — NULL is "the panel did not say", which is
    the honest value for every round recorded before this, and inventing `false` would
    assert coverage those rounds may not have had.
- **A short panel says so.** The report states seats filled against seats configured
  on every run, and calls the panel degraded above the findings when they differ — a
  weaker review, not a cleaner one. A CLI the host does not carry is exempt (it is a
  fact about the box, true every run, and `coverage_veto` already treats it that way);
  it is noted quietly instead. ⋆consensus needs two members that filed, so when only
  one did the report says agreement was *impossible* rather than letting its absence
  read as disagreement.
- **Merging happens once, in the judge, and adds rather than replaces.** The judge
  sees one entry per *reviewer*, merges the entries that are the same defect, and
  writes a `synthesis`; each reviewer's own title, detail, severity and line ride
  along in `reported_by`. Dedup upstream of the judge could only keep one
  reviewer's text and discard the rest — so the point only one reviewer made
  survived exactly when the merge *failed*, and a better key made the loss worse.
  Findings are pre-clustered by file and adjacent lines purely as a **hint**; the
  duplicates that matter are semantic (one defect, two line numbers), which no line
  arithmetic finds. Separate defects sharing one cause are linked with `related`
  instead of merged, so one decision is fixed once.
- Reviewers whose prerequisites are missing are reported **SKIPPED**, not failed.
  A seat whose CLI this box does not carry is reported but does not veto a
  confident stop: it is absent every round, so it says nothing about the round —
  otherwise a repo listing a workstation-only vendor would buy every unattended
  run on a headless box a standing veto. Every other way of not running does veto.
- **An absent seat gets no diff budget either, and that is the same rule reaching
  the other four places it was missing.** The exemption above was applied to the
  veto and to nothing else, while `budgets` was still built from the *configured*
  set — so a seat with no CLI on the box acquired a budget, an argv clamp, a
  `config_notes` line saying it "gets 116,287 of 177,872 diff chars", and a
  `truncated: true` record. Four statements about a reviewer that was never going
  to read a byte, and the last one was not cosmetic: `diff_truncated` went true on
  rounds where nothing that *ran* was cut, `load_baseline` banked the round as
  truncated, and the next round inherited *"whatever that round was cut off from
  has now been read by no round of this cycle"* — a `confident` veto, so every
  multi-round cycle on such a box was non-confident from round 2 onward,
  permanently. `budgets` is now filtered by `seat_installed` (in `panel_core`,
  beside `CLI_BIN`, read once per round and shared with `run_seat` and with the
  judge's own `adjudicate`, so no two of them can come to disagree about which
  seats exist), which closes all four at once. The seat is still **dispatched**
  and still records itself absent — that record is what the exemption above reads
  — but it is no longer handed a rendered prompt either, since a seat with no
  budget would otherwise be given the whole diff to throw away. Its
  `max_diff_chars` is `null` and its `truncated` is `false`, which is the pairing
  that broke; that guarantees a null budget can never sit beside `truncated: true`
  and nothing stronger, because an *installed* seat with no configured budget
  records the same pair. `absent` is the field that tells them apart.
  In the payload the seat keeps its `diff_budgets` key with a `null` value rather
  than vanishing, so a consumer reading `diff_budgets[name]` for a configured seat
  does not begin raising `KeyError` on exactly the boxes this is for.
  `load_baseline` banks a round as truncated on `truncated and not argv_capped and
  not absent` — both exemptions, each keyed on its own recorded field — because
  baselines written before either release still carry the old pairings. Its sibling
  `truncated_any`, which decides whether a round may **close** every earlier round's
  gap, exempts `absent` and deliberately **not** `argv_capped`: a capped seat ran and
  saw a prefix, so the round did not read its target whole and cannot be the one that
  clears an older gap, while an absent seat read nothing and is no evidence either
  way. It also requires positive evidence that a seat actually RAN, so a round in
  which every seat was absent — or present and crashed — cannot erase a gap banked
  by a round whose seats worked. The two do
  not subsume each other: `argv_capped` covers only what the kernel bounded, so an
  absent `pi` or `codex` with a configured budget smaller than the target would
  still bank a phantom round under the argv exemption alone. And not
  `ran and truncated`: `ran` is false for *every* way of not running, so that would
  also drop the truncation of a seat that was installed, read a real prefix and then
  crashed — a genuine coverage gap, and the fail-open direction.
- **A constant never vetoes, and three of them used to** (#113). The rule
  generalises the absent-CLI exemption above: an observation that is true of every
  round cannot tell a quiet round from a broken one, and because `confident` is
  `not veto`, leaving it in makes a confident stop unreachable rather than rare.
  The three are an absent CLI (above), a seat that **cannot read the code**
  (`ReviewerRun.code_blind` — see the coverage bullet below), and the **argv
  ceiling** on antigravity: its prompt travels in argv, the kernel caps one element
  at 120,000 bytes, and on a large diff it structurally cannot be handed all of it
  (measured on PR #160: 116,771 of 175,547 chars, 66.5%). Each is exempted off
  recorded state, never off the wording of a message; each is still reported; and
  each has a floor so that exempting seats one at a time cannot empty the veto list
  on a round where nothing was read whole. Truncation by a **budget** still vetoes —
  someone typed that number and can raise it, so it is a fact about the round.
  The argv exemption is applied in **two** places, and the second is easy to miss: the
  baseline loader carries an earlier round's truncation forward (increment scope never
  returns to what round 1 was cut off from), and a kernel-capped seat was not going to
  be closed by a later round either — so carrying it reintroduces the constant one
  round later and leaves it standing for the rest of the cycle, with round 1's veto
  list looking fixed. `/panel-review-pr` drives several rounds, so exempting only
  `coverage_veto` would have undone the change exactly where it matters. A budget
  truncation still carries: raise the number and the next round really does read it.
- **No settled CI result is its own veto, and it is not the same as nothing read**
  (#546). Every bullet above asks whether a seat read the diff; a round can satisfy
  all of them and still have had no test run against the code. `ci_status` used to
  reach the report as a warning and `coverage_veto` not at all, so a round whose
  full suite passed on the exact commit and one where **no run exists** were the
  same round as far as `confident` was concerned. `PASS` and `FAIL` do not veto: a
  red suite is *evidence*, `ci_brief` already tells the seats to reason from it,
  and `preland.check_ci` refuses the merge on it anyway. The other states each veto
  in **their own sentence**, and the wording matters because only two of them are
  claims about execution — `none` (nothing ran) and `blocked` (#324: a run exists,
  is gated on a person and has executed nothing) — while `PENDING` is #501's
  residue after the bounded wait and may hold green checks, `unknown` is a
  lookup that failed and says nothing either way, and #548's `local-unknown` is a
  command that was started and did not report. Could-not-check is not
  nothing-to-report. #548's `local-pass` and `local-fail` are the states argued INTO
  the non-vetoing set since, on the ground this set is for — a suite that ran and
  reported is evidence, whatever it reported — and the section below has the
  argument, including the draft that got the second half of it wrong.
  **One exemption, off recorded state:** a repo that has written
  `"preland": {"disabled_checks": ["ci"]}` — the declaration `preland.check_ci`
  refuses `none` by naming — is not asked again every round, because there the
  veto would be the standing constant the bullet above rules out. Exactly `none`,
  since the declaration explains an absent run and not a gated, unsettled or
  unreadable one; an **unexplained** `none` still vetoes. The set is written as
  the states that do NOT veto, so a CI state added later vetoes until somebody
  argues it out, and `ci_status` is keyword-only with no default so a caller
  cannot forget it and quietly buy a confident stop.
- **A declaration no seat here could ever resolve becomes an obligation, not a
  permanent veto** (#547). `could_not_assess` was answering three questions with one
  boolean. *Diligence*: did the reviewers do the job they could have done?
  *Capability*: can a panel of models reading a diff answer this class of question at
  all? *Evidence*: has anything actually checked the claim (the bullet above, off
  `ci_status`)? A seat that did not open a file it could have opened and a seat that
  would need a running Postgres and a browser produced the identical artefact, so
  `--require-earned-stop` held forever on any PR about runtime behaviour — and an
  unsatisfiable gate is one that gets dropped, leaving no gate.
  The split is the **judge's**, which was already being asked to adjudicate exactly
  these declarations and was already doing it well in prose that decided nothing. It
  now answers in `coverage_rulings`, one entry per **claim** rather than per
  declaration, pointing at declarations by the **number** the panel minted — never at
  their wording, which is the rule every exemption above keeps.
  **A ruling on its own exempts nothing.** `absent` and `code_blind` are things the
  host and the sandbox did; a ruling is a model's opinion about a model's sentence,
  and an exemption resting on one alone would be a confidence gate the panel could
  open by writing about itself. So a `resolvable_in_harness: false` *converts* the
  declaration into a named obligation with a key (`uc-` + 8 hex), which goes on
  vetoing until a human passes that key to `--acknowledge` — recorded state of the
  plainest kind, inherited across a cycle's rounds through `--baseline` exactly as
  `--escalated` is. Only a literal JSON `false` counts; a missing key, a malformed
  entry, a claim with no name, a declaration two entries both claim, and a judge that
  did not run all leave the declaration vetoing as before.
  **It can only ever shorten the list.** An obligation stands in only for lines this
  would have emitted anyway, so a blind or absent seat's declarations cannot become
  one. That is also why adding a reviewer no longer costs a confident stop by
  construction: several seats stating one capability limit merge into one obligation,
  where before each copy was its own veto. Every claim reaches the payload's
  `unresolved_claims` ledger whether or not it has been acknowledged, and each gets a
  GitHub issue whatever `review_panel.file_deferral_issues` says — on an escalation's
  footing, because it carries a question past the end of the session rather than
  filing a task. There is no board row: `qb record-outcome` keys on a finding key and
  an obligation is not a finding, so the payload and the issue are the record, and
  giving it a row would need a schema change this deliberately does not take.
- **A reviewer that produces nothing is SKIPPED, never counted as an empty review.**
  A zero exit with empty stdout is a failure for panel members and the master alike,
  and the skip line quotes the CLI's own stderr, which usually names both the cause
  and the fix. A blank reply is retried unless that stderr names a settled cause (a
  refused request, an auto-denied tool permission), which no retry can change.
  Why it is worth the code: `run_cli`'s docstring in `panel.py`. The neighbouring
  case — output that is neither empty nor a findings array, e.g. an agent narrating
  a wait — is *not* a skip, because "no parseable array" would also throw away a
  reviewer that answered in prose because it had something to say. It is kept as one
  raw finding and flagged `unstructured`, which the coverage veto states as
  "returned no structured reply — its coverage is unknown".
- **Reviewers declare their own coverage.** Each returns `could_not_assess` (areas it
  could not judge — a file the diff omits, a runtime behaviour) and can mark a finding
  `needs_rereview` (fixing it takes a structural change whose result should be read
  again). Both are *observations*: reviewers are never asked to forecast whether
  another round is needed, because that asks a model to predict findings it has not
  made — and one that silently produced nothing would answer "no" with total
  confidence. Truncation is measured, not asked for, since a truncated reviewer is the
  one party that cannot notice it. A bare findings array (any older reviewer) still
  parses and simply declares nothing.
  **A blind seat's declarations are reported and do not veto** (#113). Every
  seat reviews from the diff alone — an empty sandbox, no file tools — so "I could not
  read a function this diff does not change" is true of every round it sits, and a
  constant is exactly what the veto must not contain. Measured on PR #160's round 1:
  19 veto lines, 16 of them declarations, and **nine of those asked about a file in
  this repo**, all nine answered with `grep` in about four minutes. Worse than the lost
  confidence, blindness manufactures wrong findings — PR #64's proposed fix *was* the
  bug, PR #90's round-2 P1 inferred a missing `--json` field from its absence in the
  diff when it was already there. The declarations stay on the PR comment, under a line
  saying they cost the round nothing and are worth a `grep`; that is the work this
  used to outsource to whoever read the output, and only when someone happened to.
  Recorded per seat as `code_blind`, so #113's second half — code access as a per-repo
  setting — flips it and the declarations start counting again, which is right: a seat
  that *could* have read the tree and still could not answer is describing the round.
- **Rounds are mechanical.** `--round`/`--baseline` make each run say which findings no
  earlier round raised; `round_stop` in the payload then says go-again (something new,
  a P1/P2 still outstanding, or a finding an earlier round raised that is still outstanding
  — SonarCloud's hard-gate issues included) or stop (dry / below a floor / a premise
  declared twice / the fix pass generating the round's work / round cap), and whether
  stopping was *convergence*. The declarations never extend the loop — a truncated
  reviewer is truncated again next round — and the ones a blind seat makes no longer
  cost the stop its confidence either; what is left only stops a broken round being
  reported as clean. A round past the first with no `--baseline` is itself a veto: it has nothing
  to compare against, so its "all new" count means nothing and its stop is unearned.
- **A round past the first reviews the INCREMENT, not the whole PR** (v2.28). The target is
  what changed since the head its baseline reviewed (`head_sha` in the payload; `--since`
  overrides it), and behind it comes the PR **as it stood at that head**, in two tiers: the
  files the increment touched as they were before it touched them, then the rest of the PR.
  A budget is spent in that order, so what a tight budget drops is context and never the
  thing under review — which is the point, since a loop that re-reads the whole PR every
  round inflates its own input until it truncates itself. `--scope pr` restores the old
  behaviour and `review_panel.round_scope` sets it per repo; round 1 is always the whole PR.
  The near tier is fetched as its own `base...anchor` comparison, not sliced out of the
  current PR diff: the fix commit is part of that diff, so slicing it would send the target
  twice and label the copy as code an earlier round had already dealt with.
  **Every fallback to whole-PR scope lands in `config_notes`** — no anchor, nothing pushed
  between the rounds, a failed fetch (of the increment or of the context behind it), a
  truncated compare response, or a base-branch merge that makes the range bigger than the PR
  (measured: PR #62's raw round-to-round range was 92,415 chars against a 45,370-char PR, so
  the range is first cut to the PR's own files and then rejected outright if it is still the
  larger). A round that claimed the increment and re-read the PR would be wrong about the
  only thing this measures, and invisible in the numbers. Two degraded-but-usable cases are
  reported rather than refused: a range that is not a fast-forward from the anchor (a
  rebase or force-push, where a reverted change is in neither tier) and one carrying a merge
  commit (main's changes to files the PR also touches cannot be filtered out of the target).
  What shrinks is the **target**, always. The material sent is target plus context, so the
  token bill is in the same range as a whole-PR round; a note with both numbers says so on
  any round where the near tier is most of the context.
  One caveat: scope makes an earlier round's truncation **permanent** — round 2 never returns
  to what round 1 was cut off from — so a scoped round that inherits one vetoes its own
  confident stop, until a later whole-PR round with nothing truncated closes the gap. A round
  that recorded a head but ran no reviewer at all (a title skip, or every seat failing)
  vetoes the round after it the same way: the anchor steps over code nobody read.
  **A head that moves mid-round vetoes the round it moved under** (#106), and this one is
  about the round's *own* coverage rather than an inherited gap. The scope is decided
  against the head the round opened with, so its target ends there; `head_sha` is then
  re-stamped to the later commit because that is where the next round's fix range starts —
  and the commits between the two are the review target of no round at all. The veto is the
  only thing that stops a cycle stamping `converged: true` over them. **It reports the hole
  and does not close it.** Closing it means recording the head a round *reviewed* apart from
  the head it *knows about*, which is one field and one payload key more than this is; until
  that exists, a consumer that turns `converged` into an automatic merge gate would be
  approving code no round read, and only the veto stands between the two.
- **`--json-file` is a requirement, not a courtesy.** It is the next round's baseline, so
  a write that fails exits non-zero after the report: carrying on would leave round `r+1`
  calling every repeated finding new. Every non-error exit writes it, the skip-pattern one
  included, so "the panel exited 0 and wrote no file" is not a state the caller has to
  interpret.
- **`--max-rounds N` is the CALLER's cap**, not a loop panel.py runs: it is the only
  input that tells a round which stopped because it was done from one which stopped
  because it ran out, and `/panel-review-pr` passes it on every invocation. Its flag is
  spelled `--rounds N` on the slash command and `--max-rounds N` here — same number, and
  `--round <r>` (singular) is a different thing entirely: which round THIS run is.
  A run given none of the three is a single review and says nothing about rounds — in the
  report *and* in the payload, whose `round_stop`, `stop_reason` and `new_findings` are
  null rather than a verdict about a loop nobody is running.
  A `--round` past `--max-rounds` is rejected rather than recorded.
- **The `/panel` and `/panel-review-pr` skills** run `panel.py --post` and work from
  the **PR comment** it leaves: `/panel` stops there (review-only), `/panel-review-pr`
  hands the confirmed findings to a fixer sub-agent that fixes, verifies and pushes —
  and then panels the fix commit, 2 rounds by default (`--rounds N`), so the fixer's
  own work is read by somebody. `panel.py` itself stays read-only either way: the
  fix/verify/commit lives in the skill, and so does the loop.

### When CI has nothing to say, the repo's own suite does (#548)

`ci_brief` gives every seat a real answer about execution and #546 prices the rounds
that have none. `none` is the state neither can help: there is nothing to **wait**
for, so the channel #501 built is empty — on a repo with no CI, and on a stacked PR
whose CI branch filter never fired. A repo that sets `review_panel.local_suite` has
its own suite run once, on this box, before the seats are dispatched, and the answer
travels down that same channel.

**Evidence, not a seat.** Sonar is a panel MEMBER because it produces findings; a
test run produces **evidence**, and evidence raises the floor under every seat at
once rather than adding a fifth voice with a coverage row that answers a different
question. It is also not "give the seats a shell": three vendor CLIs each holding a
live database is expensive, nondeterministic, and costs the seats the independence
that is the only reason to seat four of them. One execution, attributable to one
commit, shared by all of them — the shape `review_ci` already has.

**It fires only where GitHub has no result and none is coming** — `none` and
`unknown`. A real CI result about this exact commit is never displaced by a weaker
local one, and `PENDING` belongs to #501's bounded wait, which is in the business of
turning it into a real one. **`blocked` is excluded too**, and that one had to be
argued out: #324 named it because it is *actionable* — a run exists, a person must
click — and replacing it with `local-pass` would overwrite the field every downstream
consumer reads with a value that says nothing about the click, while handing the round
the confident stop that is the only thing still making anybody look.

**The command is read from the default branch, never from the working tree**, which
is the one place this diverges from how every other setting resolves. A round usually
runs from a worktree checked out at the PR's *own* head — that is where the fix loop
lives — so the working tree's `.harness-rules.sample` is the pull request's, and a
command read from it would be one the PR chose, executed by the thing reviewing it.
"Checking a branch out is consent to run it" is false: checkout writes files, it does
not execute them. So `harness_rules.default_branch_rules` gives this one key the
protection the unattended path already had, in both modes. An unreadable branch means
no run and a note saying so.

**It runs only in a checkout already sitting on the PR's head, with nothing
uncommitted or unignored in it** (`panel_scope._local_head_problem`). That is what
makes a `local-pass` a statement about a commit rather than about a directory:
`panel.py --repo x --pr N` reviews a PR without ever checking it out, and a suite run
against some other branch would be evidence about code nobody is reviewing.

*Ignored* files are tolerated and *unignored* untracked ones are not, which is the
line `git status --porcelain` already draws. A provisioned worktree carries a `.env`,
a virtualenv and scratch of its own, all gitignored and none of it listed — refusing
that would mean this never runs anywhere real. A file that is untracked and not
ignored is a different animal: a stray `conftest.py` is loaded by pytest before a
line of the suite runs, a shadowing executable is found on `PATH` first, and either
decides the result of a run recorded as evidence about a commit neither is in. That
the ignore list is itself part of the commit is what makes this a line the repo drew.

**The checkout is checked again after the run**, and a pass whose tree moved
underneath it becomes `local-unknown`. Three things falsify the first answer without
anyone acting in bad faith: another agent on the same box, a command earlier in the
list that rewrote a tracked file, and the plain race between the check and the first
`execve`.

**Three states of its own, which can never read as CI.** `local-pass`, `local-fail`
and `local-unknown` each open by saying no GitHub run exists for this commit, in
`ci_brief` for the seats, `_ci_line` for a human and the round's report. A local pass
refutes the same class of finding a green CI run does — "this new test never runs",
"this may not even import" — carries the same warning that it is not evidence the
code is correct, and carries one more: it is **weaker** than a green CI run, being a
different machine, possibly different service versions, and no guarantee this is the
commit that will merge.

**What it buys, and what it does not.** `local-pass` joins `CI_SETTLED`, so it earns
a round its confident stop — the whole point. It buys no merge: `preland.check_ci`
reads GitHub and has never heard of it, so a repo with no CI is refused there exactly
as before. `local-fail` **vetoes**, which is the one asymmetry with CI's `FAIL`, and
it is argued rather than inherited: `FAIL` costs the round nothing *because* the
merge gate refuses on red anyway — a division `CI_SETTLED` says is "only sound while
both gates are applied" — and for a local failure the second gate does not exist.
`local-fail` joins it, beside CI's `FAIL` and for the same reason: a suite that ran
and reported is evidence, whatever it reported. An earlier draft of this vetoed a
local failure, arguing that `FAIL`'s exemption is conditioned on `preland.check_ci`
refusing the merge on red — a division `CI_SETTLED` says is "only sound while both
gates are applied" — which `check_ci` cannot do for a run it never sees. A Codex
second opinion on PR #604 called that special pleading and was right twice over: it
answers a question this list does not ask, since whether a second gate consumes the
evidence is deployment policy while the list comes off recorded state; and it closed
nothing, because the only repo that reaches `local-fail` with the merge gate satisfied
has written `preland.disabled_checks: ["ci"]`, and that repo merges a red GitHub
`FAIL` too. `local-unknown` is the one local state that vetoes, in its own sentence: a
command was started and told us nothing, which is not the same claim as `none`'s
"nothing mechanical executed this code".

The **board** agrees without being told. `app.ordering` compares `ci_status` against
`PASS` and `FAIL` for equality and treats anything else as neither known-green nor
known-red, so a `local-pass` reaching the plan orders that item as conservatively as
a `none` would. Left alone on purpose: the board is ordering work for people, and "a
suite passed on somebody's laptop" is not the fact its green rule is about.

**A failing command's OUTPUT never reaches a reviewer prompt, and never reaches the
PR either** — only its name and its exit code do. Two different readers, two
different reasons. `ci_brief` is rendered into four reviewer prompts and the judge's,
and that output is text produced by code from the PR under review, which is the door
`member_sandbox` exists to close; `config_notes` is published as a **public comment**
by `--post`, and a failing test prints whatever it was holding — on this fleet that
has included a `DATABASE_URL` with a password in it. So `review_local_suite` returns
the harness's sentence and the command's output as two fields, and the output goes to
the operator's terminal and **nowhere else** — not the payload either, which is POSTed
to the board: moving a leak onto a remote service with its own retention is not
closing one. The commands, their timing
and the CI state they stood in for are recorded in the payload as `local_suite`, so a
`local-pass` round can be told from a `PASS` one six weeks later without the notes.

**The bound is a bound on this process, not a request.** The command's output is
spooled to a temporary file rather than a pipe, and only its tail is read back: a
suite that prints for fifteen minutes must not be measured in the panel's RSS, on a
box already holding four reviewer prompts and a diff. That file is also what makes
the timeout real — `subprocess.run` kills its child and then reads the pipes to EOF,
and a surviving grandchild holding the write end keeps that read open for as long as
it likes. The run gets its own session and the whole process group is killed on the
bound, so a suite that started a database or a `docker compose` does not outlive the
round. Three residuals, stated rather than implied: **disk is not bounded**, a
descendant that calls `setsid` or hands its work to a container runtime leaves the
group and survives, and the post-kill reap can add up to ten seconds past the budget.
Bounding those needs a cgroup or a systemd scope, which is a heavier feature than this
one.

**The honest limit, recorded before anyone leans on it.** This answers a *subset*. On
the PR that prompted the issue the open questions were a jsonb size ceiling, whether
a reader delivers a field to a browser, and whether stored rows are now a mixed
corpus — a test database holds no corpus, so a local `make test` would have answered
none of the three. It is the structural fix for a class, not a fix for that round.

### Caps — the round ceiling and the spend ceiling (#55)

`--max-rounds` has existed since the cycle did and has never bound anything. It is
*"the caller's cap"*, honoured by a human reading a markdown file — and unattended
there is no such reader. Meanwhile the long tail needs no loop at all: a busy repo
with a dozen open PRs, each pushed to several times a day, each panel running
several models over a large diff. This repo carries the scar — one release-merge
review came to about $750, which is why `skip_title_patterns` exists.

`panel_caps.py` is the enforcement, and it is deliberately **not** a second pacing
mechanism. `qb-pace` already reads the shared subscription's five-hour and weekly
windows and `qb-start` already gates a spawn on it; what was missing was a
*policy* a person sets and the spender obeys.

**Where the number lives.** On the board, as a dial — the layer described above,
whose writes take `app.auth.human` (a `Remote-User` plus the edge secret; a machine
token is refused 403). That is what makes the ceiling something the repo under
review cannot raise: the dial layer is applied last, the per-box overlay is not
read at all on the unattended path, and the rules baseline is read from
`origin/<default branch>` rather than from the branch being reviewed. A repo may
write a ceiling into its own `.harness-rules.sample` and it is honoured when the
board has stated none — that is a self-imposed bound, not a way to raise one.

**Two ceilings, two questions.**

*The round ceiling* is `review_panel.max_rounds` when the **board** is the layer
that stated it. Under it, `--max-rounds` and the repo's file say whatever they
like; above it they do not, and the clamp says which number bound. A `--round N`
past a board ceiling exits naming the remedy that exists — *clear or move the dial*
— rather than the one that does not.

*The spend ceiling* is `review_panel.budget`, checked against `GET /review/spend`
before any seat is dispatched. Five numbers, all `null` by default:

| dial | measured over |
|---|---|
| `tokens_per_day` / `runs_per_day` | this repo, rolling `budget_window_hours` |
| `tokens_per_pr` / `runs_per_pr` | this PR's whole life, no time bound |
| `fleet_tokens_per_day` | every repo on the board, same rolling window |

**What spend is measured in.** Tokens, input + output — `/review/stats`' own
`billable`, since cached input is a slice *of* input and reasoning sits inside
output for some vendors and beside it for others. #55 named per-reviewer token
capture (#15) as its blocker and settled meanwhile for a crude "reviews per day"
proxy; #15 landed, so the honest unit is available and is what the ceiling uses.

The run ceilings are not that proxy's leftovers. A seat nobody instrumented
(`antigravity`), a vendor that states nothing, a run recorded before v2.14 — each
spent real money and measured no tokens, so a token-only ceiling reads an
unmeasured spend as *no* spend. `/review/spend` reports `rows` against
`measured_rows` and a refusal that was computed over partial coverage says
*"measured over 6 of 20 reviewer runs — the real spend is higher"*. And
`runs_per_pr` is the only one of the five that binds a caller which **renumbers its
rounds**: `--round N` is an argument, so a driver that always says `--round 1`
never reaches the round ceiling at all, while a run is a row on the board whatever
it called itself.

**What a stopped run says.** It travels the pre-flight refusal's existing path,
which is the whole reason it was routed there: printed, recorded with
`reviewed: false`, a `skip_reason`, a per-seat `ran: false` row so the board cannot
file the refusal as a panel that found nothing, and posted to the PR under
`--post`. On top of that it carries `round_stop: {stop: true, confident: false}`,
because *"a budget stop that looks like a clean review is the exact failure v2.15
exists to prevent"* — `preland --require-earned-stop` HOLDs on it and the review
queue files it `unconverged`. A size refusal is deliberately **not** a stop: that
one means "this round could not usefully read the diff" and leaves the cycle open.

**`--force` does not override a ceiling.** It overrides this host's judgement about
what its own seats can read, which is exactly the kind of judgement an operator
standing in front of it may overrule. A fleet ceiling a local flag could switch off
would be advice again.

**When the board cannot be read**, the answer differs by whether anybody is
watching, and #59 asked for that decision to be made here rather than inherited:

* **attended** — proceed, and say in the report that the ceiling was unverified.
  `/panel` on a laptop with no board, no network and no `qb` reviews a PR and
  always has; the round ceiling still binds, because it needs no board read.
* **unattended** — refuse. `qb-start` already reasons this way about `qb-pace`
  (a spawn proceeds only on a definite go), and a governor that cannot read its
  input must not report clear (#244). An unattended run that treated an
  unreachable board as headroom would be a ceiling anybody could remove by
  unplugging a cable.

**With every ceiling unset, none of this happens** — including the board call.
`Budget.dormant` returns before `fetch_spend` is reached, which is asserted by a
test that makes the read raise rather than by a comment.

**It does not reserve.** The check is a read and the dispatch after it is a
separate act, so two panels starting in the same second both see the same
headroom. The overshoot is bounded at one round per concurrent run and is a
stated property, not an oversight. Closing it would mean the board holding a claim
on part of a budget for a run's duration — a reservation, whose own failure mode
is a leak that parks a repo until somebody notices, which is what `qb-pace`
refuses when it insists a `hold` is a wait and not a stop.

### The pre-flight verdict — whether a round is worth running

A panel was launched on PR #137 and killed five minutes in by a human asking *"is this a
crazy token count?"*. It was. The more important half is that **the output would have been
worth less than nothing**, and nothing in the harness knew that.

That PR's diff was **763,375 chars — 6.4× `agy`'s 120,000-byte argv ceiling — on a change
that was a pure move**: `panel.py` split into six modules with nothing retyped. Every
relocated line appears *twice*, once as a delete and once as an add, so the overwhelming
bulk of that 763 KB is code nobody changed. A seat spends its whole budget re-reading
relocated text, and whatever it reports is a finding about code that was already in the
base branch and already reviewed when it landed there.

**The token cost is the second problem. The first is that a truncated read which produces
findings is worse than no review,** because the next step of the cycle briefs a fixer to
resolve every one of them to a "nothing left to improve" bar.

Every piece needed to catch this already existed and none was wired to the decision: the
truncation report (above) fires *after* the round, the argv ceiling is documented as a
permanent property of the harness and gates nothing, and increment scope makes only *later*
rounds cheaper. So the panel now rules before it dispatches, and the ruling is its own
rather than the caller's.

**Three verdicts.**

| verdict | when | what happens |
|---|---|---|
| `run` | the diff fits every seat's ceiling; or no seat declares one; or it is over but under the refusal multiple | exactly what every release before this one did, truncation report included |
| `manifest` | over a ceiling **and** move-shaped **and** a manifest of it is smaller than *both* the diff and the ceiling | the seats are handed a *manifest* instead of the diff |
| `refuse` | over a ceiling by `refuse_over_cap_multiple` with no smaller honest question to ask — not move-shaped, or move-shaped with no manifest to substitute; **or** a precondition failed, which today means the branch cannot merge (`require_mergeable`) | nobody is dispatched; the refusal is printed, recorded, and posted under `--post` |

A `refuse` on a **precondition** is the same verdict reached without measuring anything: the
branch cannot merge, so no ceiling was consulted and none is quoted. It is decided before the seats, and `--force` overrides it through the
same machinery that overrides a size refusal — leaving `preflight.would_have: refuse` behind, so
"the tool chose to run" and "a caller overrode the tool" never look alike.

A move-shaped diff over a ceiling therefore has three outcomes, not one. It gets the manifest
whenever there IS one to substitute; when there is not — `manifest_moves` is off, or the
manifest came out no smaller than the diff, or smaller than the diff and still over the
ceiling — the refusal multiple decides as it does for content, and *below* the multiple the
round runs as an ordinary truncated content review. That last case is the one that must not be
silent, and it is not: the `run` verdict then carries a `reason` naming the manifest path, what
it measured, and why the substitution did not happen. An empty `reason` on a `run` means
"nothing objected", and only that.

**Every ceiling carries its unit, and the one that binds is chosen by ratio rather than by
number.** A configured `max_diff_chars` is characters; the kernel's argv limit is *bytes*, and
the two differ by the diff's non-ASCII density — this repo's own diffs are full of em-dashes
and arrows. Those are not two sizes of the same thing, so `min()` across them had no defined
answer: a repo setting `antigravity.max_diff_chars: 100_000` hid the 120,000-**byte** argv
ceiling behind the smaller integer, and at two bytes per character that seat's real ceiling is
~60,000 characters — tighter than the one that won. So `agy` with a configured cap declares
**two** ceilings, each is measured against the diff in its own unit, and the tightest *ratio*
decides. `preflight.shape` carries `chars` and `bytes` both and `preflight.cap_unit` names
which of them `cap` and `over_cap` are in, so the multiple can be checked by hand rather than
assumed — and every renderer of the verdict (the refusal notice, the manifest banner, the
`--force` banner, each seat's skip reason) states the unit it measured in.

**The judge's `judge_max_diff_chars` is deliberately not one of the ceilings.** This verdict
decides whether to dispatch the *seats* and what to hand them; that key says what adjudication
is worth, not whether a round is readable, and counting it would let it refuse a round every
reviewer could read whole. The judge is not left out in the cold: a manifest substitution
replaces the round's *material*, so the judge composes the manifest too, and a judge cut by its
own budget is reported as truncation exactly as a seat is. (`diff_budgets` in the payload does
include `judge` — that is the budget record, not the ceiling list.)

**A seat whose CLI this box does not carry declares no ceiling here.** That is the same
host-versus-round distinction the coverage veto makes for an absent seat, and it matters most
where it is least visible: `agy` is a workstation package, so on a headless box this repo's own
rules enable a seat that records "antigravity: CLI absent" and never runs. Counting its argv
ceiling would refuse rounds on behalf of a reviewer that was never going to read anything, on
exactly the unattended hosts where nobody is watching to pass `--force`, while the seats that
did run were reading off stdin with no cap at all. A box carrying no seat at all therefore has
no ceiling and refuses nothing — the round runs and `coverage_veto` says "no reviewer ran",
which is the existing and correct answer.

**Both questions are asked of the review target, not of the PR.** Under `pr` scope those are
the same string, so round 1 — always whole-PR, and the case #137 was — is unaffected either
way. Under increment scope they are not: the target is the fix commit and the PR is
everything, so weighing the PR would refuse (or hand a manifest to) a round whose actual
material is a 3 KB increment, because of a size that round was never going to send. A round 2
fix commit is neither large nor move-shaped just because the PR it lands in is.

**The shape is measured, not guessed.** A rename, a split or a relocation all look the same
mechanically: the added lines are a near-permutation of the deleted ones. The panel takes
the multiset intersection of the `+` and `-` bodies and divides by the larger side — no
`git diff -M`, because rename detection is a property of the git invocation and the panel's
diff comes from `gh pr diff` over the API, having never checked the PR out. Blank lines are
excluded from both sides (a blank matches every other blank), and only lines *inside a hunk*
are counted, because `--- a/x` and `+++ b/x` begin with the same characters a content line
can.

**A move is reviewed as a manifest, because for a move the mechanical evidence is the only
evidence that bears.** The manifest is:

- **what moved where** — each file's `+`/`-` tally, and whether it only gained or only lost
- **what did not survive** — lines deleted and not re-added anywhere. A dropped guard
  clause, `except` arm or decorator is invisible in a content review of the destination
- **what changed besides moving** — lines added and not deleted anywhere. This is the only
  genuinely new code in the change, and the one place a content review belongs
- **definitions the change adds in more than one place**, with the files each copy landed in —
  half of the duplicate-copy trap: a merge that keeps both copies of a moved function is a
  clean merge, a green test run and a silent bug, because the later binding wins and the dead
  one is what anybody reading the old file finds. Only *half*, and the manifest says which
  half. What is detectable from a diff is a definition **added** in two or more places. The
  more common accident — the original left exactly where it was, in a file the merge never
  touched, while a copy arrives somewhere new — puts no `+` and no `-` line in the diff for
  that original: it is not in the diff, it is in the base branch, and nothing recovers it from
  `gh pr diff`. Detecting it needs the PR checked out, which the panel never has, so the claim
  was narrowed to what is checked rather than the check widened to a promise it cannot keep.
  The spellings covered are Python `def`/`class` and the JS/TS `function`, brace-class,
  `const`/`let`/`var`-bound-arrow (generic and function-typed forms included), `const enum`
  and `interface`/`type`/`enum` forms. What is *not* covered is named too, and named
  **whether or not the section found anything** — class and object *methods*, wrapped
  signatures and every other language, so a section that found one duplicate cannot read as
  having found them all. So are the ordinary reasons a name is defined twice on purpose
  (`@typing.overload` chains, `if TYPE_CHECKING:`/`else:` pairs, platform-conditional
  definitions), because a reviewer who is not told about them stops believing the section on
  its first false positive
- **what is not here** — test counts before and after, whether a module now reaches backward
  into another, and the unseeable half of the duplicate trap above. All three need the branch
  checked out. They are *named as unmeasured* rather than claimed, and the brief tells the
  reviewer to declare them

Its size is a function of the change's **shape**, not of the diff's length: the 428 KB
worked example in the test suite produces a 1.3 KB manifest. It travels as the round's
review material, so the per-seat budgets, the truncation measurement, the judge and the
board record all keep working unchanged — and all measure the manifest, which is the thing
that was actually sent.

The material it replaces is a whole-target composition (there are no context tiers in a
manifest), so a *scoped* round whose increment is itself move-shaped keeps `scope:
"increment"` and its `since_sha` regardless: what the round **targeted** and the shape of the
material it composed are two different facts, and the inherited coverage vetoes are gated on
the first. Read `preflight.verdict` beside `scope` to know whether a round read its target or
a description of it.

> **A manifest round's findings answer a different question.** The report says so above the
> findings, because "no correctness findings" would otherwise read as "the moved code is
> correct". Nobody read the moved code.

It also **vetoes a confident stop**, mechanically. That is the least trustworthy quiet the
panel produces and the one nothing else catches: every other coverage veto keys off a seat
being short of what it was *sent*, and a manifest round's seats got the whole manifest, so
`truncated` is false for all of them. Without the veto a cycle could stop `confident: true`
having had nobody read a line of the moved code. The brief does ask each seat to declare the
facts a manifest cannot carry, and a seat that does adds its own `could_not_assess` veto — but
"was the moved code read" is something the panel *knows* from the material it composed, and
what can be measured is never left to a model to volunteer. A **forced** round needs no such
veto: its seats are cut by the ceiling that caused the refusal, so the ordinary truncation
veto already says so with the numbers.

**And the gap it leaves is inherited.** A manifest round is recorded on the baseline
(`Baseline.manifest_rounds`), for two reasons. It must not count as having **re-read the PR**:
it records `scope: "pr"` — the manifest travels as the round's material — with nothing
truncated, because the manifest fitted, so it satisfied every term of that test while having
read no code at all, and one such entry erases *every* earlier round's truncation and unread
record. And under increment scope the next round's anchor steps over the code it did not read,
so a later scoped round carries its own veto naming it. A round that genuinely re-reads the
whole PR clears it, on the same terms as the other two — the veto must not become permanent. A
payload written before `preflight` existed is not a manifest round: the field's absence is
evidence about the writer, not about the round.

**This is not a diff budget, and that distinction is the whole design.** v2.16 refused a
default budget on evidence — truncating when nothing forces it biases toward false
positives — and that reasoning is untouched. A budget answers *what to send*; this answers
*whether to start*, and only ever against a ceiling that already exists. **On a repo with
`max_diff_chars` unset and no argv-bound seat enabled there is no ceiling, so none of this
ever fires** and no number it invented reaches anyone's diff.

**Nor is it a silent skip.** A panel that quietly declines is a merge gate trusting a proxy.
A refusal:

- prints a notice headed **"Panel REFUSED — no review happened"**, stating in the first
  sentence that this is not a clean review, followed by the measurement and the remedies
- carries `reviewed: false`, a `skip_reason` and the full `preflight` block in `--json` and
  `--json-file`
- **is recorded on the board**, unlike the title-pattern skip — and that difference is
  deliberate. A title skip says *this PR was never worth a panel*; a refusal says *a panel
  was wanted and this diff defeated it*, which is the observation worth accumulating
- carries a `reviewers` block marking every selected seat `ran: false`, and the matching
  `skipped` entries. Not optional: the board builds a scorecard row for each name in
  `reviewers_selected` and, absent a `reviewers` block, assumes a member ran unless `skipped`
  names it — deliberately, so a quiet reviewer is not filed as broken. Sending the selection
  and nothing else would file a refusal as a clean review *per reviewer*, in the table that
  answers "which reviewer finds the real issues"
- **reads the CI gate anyway, and says so under the notice.** CI is size-independent, costs one
  `gh pr checks`, and consumes no seat's budget — it is the one part of a round a 763 KB diff
  cannot make useless, and a refusal that lost it left `/panel-review-pr` told to stop the cycle
  with nothing said about a red build. `ci_status`/`ci_failing` are recorded, and both
  `/panel-review-pr` and `/panel` are told to relay them with the refusal — the capability is
  worth nothing if the consumer it was added for is not asked to read it. **Sonar is not
  read**: it is a panel *member*, with a `ran: false` row like every other seat, and dispatching
  one while telling the board none ran would be the inconsistency this path exists to avoid — so
  the notice states that gate was **not evaluated**, rather than letting its default read as a
  pass
- **records what it was going to review** — `scope` and `since_sha`, the same way the ordinary
  payload does. A refusal under `--scope increment --since <sha>` otherwise publishes the field
  defaults, and nothing then distinguishes it from a refused whole-PR round
- is posted to the PR under `--post`. The terminal copy is read by whoever is watching, and
  under the epic driver nobody is

**`--force` overrides both the refusal and the manifest** and reviews the diff as content.
The verdict it overrode is *not* erased: `preflight.forced` and `preflight.would_have`
record it, a `config_notes` line says so, and the report carries a warning above the
findings. An override is a decision, and it must not look like the tool having decided to
run.

Two guards worth knowing, because both are cases that read plausibly when wrong:

- **A manifest is only substituted when it is smaller than the diff *and* under the ceiling.**
  Its body scales with the change's shape but its brief and section headers are a fixed
  ~1.3 KB, so on a small move over a small ceiling the substitution would hand a seat *more*
  text than the diff did and then have it truncated. Testing it against the diff alone was not
  enough: 200 table rows plus 240 quoted residue lines is ~35 KB of manifest, which is smaller
  than a 763 KB diff and still over a low-thousands `max_diff_chars` — a seat reading a
  *prefix of a manifest*, reported as a clean `manifest` verdict beside
  `diff_truncated: true`. Both are measured, not assumed; when the manifest does not fit,
  the round falls through to the refusal above the multiple and to an ordinary truncated
  content review below it, and either way the `reason` says the manifest was tried and why it
  did not help.
- **A tightly configured `max_diff_chars` can now trigger a refusal.** `max_diff_chars: 30`
  on a 1,559-char diff is 52× over. That is the intended reading — a seat handed 2% of a PR
  produces exactly the review this feature exists to prevent — but it is a behaviour change
  for a repo that set a small budget on purpose. `refuse_over_cap_multiple: 0` switches the
  refusal off and keeps the manifest.

### A finding no round can close (`--escalated`)

`--escalated <key>` (repeatable) tells a round that a finding's fixer reported the
**approach** wrong rather than the code and wrote no patch — `review-pr.md` step
3a. The key stops counting as work a fix round can clear.

It exists because the two rules around it are individually right and were jointly
a trap. An escalated finding is outstanding, and `panel-review-pr.md` §5 forbids
ever handing it to another fixer — so `round_stop` returned `stop: false` every
round until the cap, and the mechanism meant to stop a cycle circling a premise
guaranteed it ran to the cap instead (#221).

**The rule, its exact scope and its two caveats live in one place: `round_stop`'s
docstring in `panel_rounds.py`.** What a caller has to DO about it lives in
`panel-review-pr.md`. Neither is paraphrased here — this rationale was restated
five ways once, and two of the copies had already drifted into saying something
untrue by the time anyone read them together. What is worth recording here is only
what the flag touches outside that function:

- **The mixed case is why this is a filter and not "stop on any escalation".** One
  escalation beside a live P2 still goes again, for the P2; the cycle stops when
  the fixable work is gone rather than when the counter runs out. Stopping the
  whole cycle on any escalation would throw away the re-review of fixes made in
  the same pass — the round that, on PR #212, found 16 defects the previous fix
  introduced.
- **What it writes.** The payload carries `escalated: {key: round}` — the cycle's
  whole register, which later rounds read out of `--baseline` and which only ever
  grows — and `round_stop.escalated_outstanding`, the sorted subset of it that
  THIS round raised. The two are deliberately different questions: "what is this
  cycle holding" and "what stopped round 3". `escalated_outstanding` is the one
  that earned the veto line, so it is the one a consumer usually wants. Every
  finding in `to_fix`, `sonar_findings` and `dismissed` also carries
  `escalated: true|false`, and the report marks it ⛔ in the two lists a fixer's
  brief can be built from.
- **It needs a cycle to mean anything, and is refused without one.**
  `--escalated` names work a LATER round must not count, and it is read out of a
  fix pass that followed a review round — so the flag without `--round`,
  `--max-rounds` or `--baseline` exits non-zero saying so. The two alternatives
  were dropping the declaration silently and inventing a cycle to hold it, and the
  second one printed "round 1 of at most 2 — go again" for a re-review nobody
  would run.
- **A key naming nothing is reported** in `config_notes` rather than ignored, and
  a value that is not a finding key (8-64 hex characters) is rejected before it
  reaches the register or a PR comment — including the empty string, which is the
  likeliest one to arrive (`--escalated "$KEY"` with the variable unset). The
  alternative failure is silent: the loop simply carries on counting a finding the
  caller believes it excluded. Keys are lower-cased and stripped first, because
  this value is transcribed out of a fixer's prose by hand, and repeated flags are
  deduplicated, so re-passing an inherited key really is harmless. A key naming a
  finding this round's master DISMISSED is reported too: that is not work either
  way, so the declaration changes nothing. A round the panel SKIPPED adds no key —
  it reviewed nothing — but carries the inherited register forward and names the
  key it dropped.
- **An escalated SonarCloud gate issue does not make the gate green.** A premise
  answer is not a fix, and the gate is an external merge blocker, so a stop whose
  only remaining work is an escalated gate issue names the failing gate in both
  `reason` and `veto` instead of reporting "nothing left that a fix round can
  clear" on its own. (`preland.py` HOLDs on that gate independently; this is the
  panel saying it in its own verdict.)

### What the cycle leaves behind — `round_stop.outstanding` (#42)

`stop` answers **one** question: *should another panel run?* It is a question about
cost, the cap and convergence. It was being read as also answering *should these
findings be fixed?* — which it is not computed from, and which has a different answer:
by `/panel-review-pr`'s own bar that one is always yes, for every confirmed finding and
every SonarCloud hard-gate issue.

**The cap is where the two come apart, and the cap is the common case.** `round_stop`
flips `stop` to true with `"round cap (N) reached — …, unreviewed"`, and §5 launched a
fixer only on `stop: false` — so the final round's findings (P1/P2s still outstanding,
repeats whose fix did not land, gate issues, and everything that round newly found)
were found, judged, posted to the PR, recorded on the board, and **handed to nobody**.
Twelve deferred-findings issues on this repo are one instance each of that.

So the payload states both questions. `round_stop.outstanding` carries:

| field | what it is |
|---|---|
| `fixable` | The keys a fix pass is asked to clear — everything still outstanding at or above `fix_severity_floor`, gate issues included (they are exempt from both floors). Built from **one universe**: `outstanding` ∪ `new_keys` ∪ `repeated`, minus the escalated. A key reaching only one of those parameters would otherwise be counted for the STOP and dropped from the disposal, which is this bug one level down. |
| `below_floor` | Outstanding and under the fix floor. Handed to nobody **deliberately** — #165's policy stop, and the repo's own decision. Listed rather than dropped, because silence about them is what lets such a stop read as a dry one. |
| `escalated` | `escalated_outstanding` under a second name, off the same local so the two cannot disagree. Never a fixer's, at any stop (#221). |
| `handed_to` | `fixer`, `human`, `nobody` — or **null on a `go again`**, where no disposal is being made and §5's ordinary path takes the findings to the next fix pass. |
| `why` | The one sentence the relay repeats. |

**Measurement and verdict are kept apart**, on exactly the terms `fix_injection` keeps
`over` from `fired`: the three lists are a property of the ROUND and are computed on
every round including one that is going again; `handed_to` is the property of the
VERDICT. A caller gating a final, unreviewed fix pass on that field must not have it
answered by a round that is mid-cycle.

**A futility rung's leftovers go to a human, not to a fixer.** `premise_repeated`,
`premise_undecidable`, `fix_injection`, `new_findings_not_falling`, `unrefereed_fix`
and `guard_lines`
each end the cycle by saying *in their own `reason`* that a human answers this rather
than another fix pass, so handing their remainder to one would contradict a sentence
the same payload is carrying. A cap says only that the cycle has spent enough, which is
not a claim about what the next fix pass is worth — which is why the cap is the rule
this block was written for.

**The honest end state is the point of it.** A cycle that ends with clearable work left
ends with **either unfixed findings or an unreviewed fix**. There is no third option,
the choice belongs to the operator, and the workflow used to take the first in silence.
`handed_to: "fixer"` names the second as the default and `why` says in as many words
that the resulting commit ships unreviewed — **a proposal and not an action**, on
#506's terms: nothing here runs a fixer, and §5 tells the caller to say which it did.

### The premise check (`--ask`)

`python3 ~/.claude/loops/panel.py --ask "<premise>" [--context <path[:first-last]> …]
[--pr N] [--asker <seat>]` — one question to the enabled seats instead of a review.
**No diff, no clustering, no judge: the vote is the output.**

```
$ panel.py --ask "panel.py exits non-zero when it skips a PR on a title pattern" \
           --context harness/loops/panel.py:3500-3560
    claude  fails       — the skip branch calls finish(failed) and returns 0
    codex   fails       — write_payload then `return finish(...)`; exit 0 on that path
    pi      cannot tell — did not locate the skip branch in the given range

→ the premise FAILS — 2 of 3 say the premise FAILS (quorum 2, threshold 2)
```

- **It is not a gate.** Exit 0 on every verdict, `fails` included. A fixer runs it before
  committing; making it mandatory turns a one-minute question into a required wait, and a
  required wait gets skipped. It takes none of a round's flags (`--post`, `--round`,
  `--baseline`, `--max-rounds`) and says so rather than ignoring them — there is no diff to
  post about and no cycle for a baseline to be part of.
- **`cannot tell` is an answer, and an unreadable reply is not that answer.** A seat whose
  context did not settle the question counts toward the **quorum** and never toward the
  **threshold**; a seat whose reply carried no verdict counts toward neither and is shown
  as such, with the head of what it did say. Folding the second into the first is #68's
  panel-of-one through a side door: a tally reading "nobody objected" over seats that
  never spoke.
- **The asker cannot be the only seat.** `--asker` names the seat the agent running the
  challenge is (pass `--asker ''` for a human at a terminal). A tally whose only voter is
  the asker is `unchallenged` — where the premise started — never `holds`.
  **Detection is Claude Code's environment and only Claude Code's**: nothing codex, pi or
  `agy` exports says which seat is running a command, so an agent on one of those has to
  pass `--asker` itself. The run says so in its notes when nothing was detected, and says
  so again when `--asker ''` turned the guard off while an agent's environment was
  present — a guard believed to be on and quietly off is worse than one known to need a
  flag.
- **Nothing picks between candidate answers.** Two different legal verdicts in one reply is
  an unreadable reply, not a chance to guess which the model meant; an unreadable reply
  buys exactly one retry, the same as a review's. The schema's own example is refused by
  the same check that refuses a typo, because it spells the verdict as the union of the
  three legal values.
- **Verdicts:** `holds` / `fails` (threshold reached, one way only), `unresolved` (the seats
  looked and did not agree), `unchallenged` (too few answered, or only the asker did).
  `review_panel.ask_quorum` and `ask_threshold` govern it, both 2 — so "1 of 1 says it
  holds" reports as unchallenged rather than as agreement. Named for the ask because that
  is all they govern today; #78 generalises the same primitives to a round's verdict.
  A rule above the seat count is warned about, because it can never be met and the ask
  still runs (and pays for) every seat first. A threshold above the quorum is fine: quorum
  is a minimum, so three agreeing seats reach `ask_threshold: 3` under `ask_quorum: 2`.
- **`--context` is confined to the repo under review**, symlinks resolved before the
  containment test and the file then read by walking down from a descriptor on the repo
  root — the root's own open included — because resolving a path and re-opening it by that
  path are two traversals of one string, and a component that changes between them would
  pass the check and read elsewhere. Paths are relative to the **repo root**, not to the
  cwd, and the problem message says so. A spec that cannot be read is a stated problem
  rather than a silent omission — a seat given less context than the asker believes it has
  answers `cannot tell` about a question the asker thinks it supplied the answer to. That
  covers a file that is not UTF-8 text or carries NULs (a PNG used to arrive as a wall of
  U+FFFD), a malformed range (said as a range and not as a path), and a file past the
  4 MB read ceiling. A file whose own name ends in `:12` wins over reading line 12 of a
  file called something else; an exact repeat of a spec is read once. A range past the end,
  and anything over `ask_max_context_chars`, is clamped and said — and the range the report
  and the payload carry is the one the seats actually got, not the one that was typed. With
  no context at all (or none of it left after a clamp) the prompt says so, which is what
  keeps `cannot tell` available instead of inviting an answer from memory.
- **Containment was never the whole rule, so an ask refuses to read a secret.** Being
  inside the repo under review is exactly the case where `--context .git/config` hands a
  personal access token to four third-party CLIs, since every seat's reply is a place its
  prompt can come back out. `.git/` is refused outright, as are the files that are nothing
  but credentials by the names they always have — `.env` (and `.env.*`), `.envrc`,
  `.npmrc`, `.netrc`, `.pgpass`, `.pypirc`, `id_rsa`/`id_ed25519` and friends, and anything
  ending `.pem`, `.key`, `.p12` or `.pfx`. Each refusal is a stated `context_problem`
  naming why, like every other spec that did not become context. It is a denylist of names
  and not a secret scanner: it closes the routes an agent composing a command line actually
  types, and says nothing about a token pasted into a source file.
- **`--json` / `--json-file`** emit the ask's own payload: `kind: "ask"`, the premise, the
  `context` actually read (spec, path, line range, chars), the specs that did NOT become
  context as `context_problems` (`{spec, problem}` — machine-readable, so "was this verdict
  reached with all the context the asker intended?" is answerable without matching prose,
  and kept out of `config_notes`, which is about the repo's configuration), every seat's
  `verdict` and `reason` — plus `gist` for a reply that carried no verdict, which is what
  the seat SAID and never why — and the tally with its rules. Per-seat token usage is
  spread into the same object but written first, so a telemetry key can never overwrite the
  answer. Recording on the board goes through `qb record-ask`, best-effort and never
  fatal: `qb` ships in the fleet's own repo and the row it writes is #77's to define, so on
  a host whose `qb` predates it the ask says so once and is otherwise untouched. Every
  other recording failure is reported too, and a run whose `--json-file` could not be
  written is not recorded at all — it is about to exit non-zero, and a board row for it
  would be two records disagreeing about whether the ask happened.

### What the seats would do instead (#507)

Every seat returns **findings** — a defect, a severity, a location — and for an ordinary round
that is the right contract. A reviewer that proposed a patch for every nit would be a second
author, and the leaderboard measures whether a reviewer is *right*, not whether it is helpful.

On a cycle that will not converge the fixer is doing something else entirely: **inferring the
reviewer's intent from a criticism**, and then guessing at a change that satisfies it. That guess
is what the next round reads, and 128 of 201 new findings across seven PRs were created by the
fix pass immediately before them. Nothing anywhere asked a seat the obvious question.

So when an `escalate_on` rung **fires**, the round asks each seat that still has outstanding
findings on this PR one thing:

> given these findings of yours, what is the smallest change that resolves them?

```
### What the seats would do instead (3 asked)
_This cycle escalated on `new_findings_not_falling`. …_
- **claude** (4 finding(s)) — _change_ — `panel_rounds.py:3402`: hold the escalated keys out of
  `blockers` instead of filtering them at each rule
    - resolves 3 of them: `a1b2c3d4e5f6`, `1122334455aa`, `9f8e7d6c5b4a`
- **codex** (2 finding(s)) — _no small change_: the two rules disagree about what `outstanding`
  means; one of them has to move before either finding can be closed
- **pi** (1 finding(s)) — _cannot tell_: I would need the previous round's payload
```

- **On escalation, not every round.** `fix_injection`, `new_findings_not_falling`,
  `premise_repeated` and `premise_undecidable`, and on each one's `fired` rather than its `over`
  — the measurement crossing a threshold is true of plenty of rounds those rules deliberately do
  not touch. The round **cap** does not trigger it: a cap is a cost bound and ends healthy cycles
  in the same place as diverging ones, so firing on it would be the "every round" this avoids.
- **`--ask` is the machinery and the wrong question.** That path fans a *premise* out and tallies
  it; here nobody has written a premise, because the whole problem is that the fixer does not
  know what the claim should be. The fan-out, the sandbox, the retry and the attribution are
  shared; the question, the reply shape and the **tally** are not.
- **There is no tally, and that is the feature.** Four seats proposing four incompatible changes
  is the most useful possible answer on a stuck cycle — it says the finding set has no small
  resolution, which is the thing nobody currently learns until round five. Nothing here
  reconciles them and nothing computes agreement: deciding whether two proposals are the same
  change is the similarity heuristic #84 rules out for premises, one level down.
- **A seat is shown what IT wrote**, not the judge's merge of it. `Canonical.synthesis` is one
  sentence over every reporter of a defect, so a finding three seats raised carries a wording none
  of them wrote — and the premise is *given these findings of yours*. The merged wording is shown
  beside it where the two differ, because that is what the PR comment calls the defect.
- **A proposal is not a finding.** No leaderboard, no cross-round defect chain, no severity
  floor, and no path into `round_stop`. The prompt says so to the seat as well: a reviewer that
  believed this was scored would propose in order to score. A `change` verdict carrying no change
  is unreadable rather than recorded — the whole content of that verdict is the proposal.
- **Its spend is measured locally and is invisible to #55's ceilings**, exactly as `--ask`'s is:
  the board's review ingest is `extra="ignore"`, and folding proposal tokens into the round's own
  reviewer rows would charge them to the review and inflate the leaderboard's cost-per-finding for
  a seat that answered a question about findings it had already made.
- **It cannot make a review look cleaner than it is.** It runs after `stop`, `reason`, `veto` and
  `confident` are final and writes to none of them, and its section is printed **under** the veto
  lines — a plan at the top of an escalation is exactly the "cleaner" this must not produce.
- **An escalated finding is shown and marked** as the human's to answer. It is outstanding, and
  the human at the veto line is precisely who needs a proposal on it; what it must not become is
  a licence for a fix pass to patch one, so the brief says whose answer it is.
- **`propose_on_escalation: false`** switches it off, and a round that then escalates says so in
  `config_notes` rather than looking like a round where nothing fired.

### The `--json` payload

One record per **defect**, in `to_fix` / `dismissed` / `sonar_findings`, plus the run's
own fields (`judged`, `reviewers`, `diff_budgets`, `run_key`, …). A skipped PR emits the
same keys with empty values and `reviewed: false`, so nothing has to branch on which
exit produced it.

**v2.23 — what the PR touched, and what state it is in.** The run carries `changed_files`
(the PR's paths, each with its own `additions`/`deletions`) beside the `changed_lines`
total, plus `changed_files_total` (GitHub's own count), `pr_state` and `is_draft`.

**`changed_files_total` may be NULL, and that is not the same as `0`.** NULL means GitHub
did not state a count; `0` means it counted and the answer was none — the second is
knowledge and the board acts on it. The same rule holds per file: an `additions` of `null`
means "not stated", never "no lines". The one number never derived from another is this
one, because `len(changed_files) < changed_files_total` is the *only* evidence the list is
a prefix — `gh` pages it and GitHub caps it at 3,000. When they differ the run's
`config_notes` say so, on **every** exit including the skipped one.

It is the **PR's** file list, not the round's, read from `gh pr view` rather than from the
diff the reviewers are handed. That is what lets the skip path — which never fetches a diff
— still emit a complete one, and what keeps it correct under a round that reviews only the
increment: a collision surface that narrowed with the increment would report two PRs as no
longer colliding because one stopped *re-reading* a file it still changes.

> **The skip path records it too, since #94** — and the row says no review happened. It
> used to return before `record_run` on the correct reasoning that recording a non-event as
> an event is its own disease, which left the board with no file list for merges, promotes
> and format-the-world commits: the changes that touch the most files, collide with the most
> work and are merged unattended most often. `GET /review/collisions` saw a skipped PR as
> neither subject nor rival.
>
> The payload has always carried `reviewed: false` and a `skip_reason`; the board simply had
> nowhere to put them. It stores both now, so a skipped run is a row that states what it
> measured and denies having reviewed anything. It carries no `reviewers_selected` and no
> findings, so it contributes no scorecard and no finding row — every per-reviewer statistic
> is untouched by construction rather than by a filter.
>
> Read `reviewed` before counting runs. A query that means "a review happened" asks
> `reviewed IS NOT FALSE`, never `IS TRUE`: every run recorded before the column carries
> NULL, and `IS TRUE` would report the whole board as never reviewed.

### A round the board did not take says so (#284)

Recording goes through `qb record-review` and stays **best-effort** — a board that is down
must never fail a review that already ran. What it is not is silent. `record_run` used to
open with `if not shutil.which("qb"): return`, and since `qb` ships in the fleet's own repo
rather than this one, whether a round was recorded depended on a binary from elsewhere being
on the PATH of whichever box ran the panel. 67 rounds across 30 PRs went missing that way,
leaving the board holding 39% of this repo's review history with nothing anywhere saying so
— and every measurement taken from it (the `/panel` leaderboard, per-reviewer precision, the
dial calibration) computed off a three-day tail nobody knew was a tail.

Three ways to miss, one sentence each: **no `qb` on this host**; **`qb` refused** (no board
URL, no token, no such subcommand); **`qb` ran and the board did not answer** — that last one
is invisible to an exit code, because `qb record-review` exits 0 either way and distinguishes
them on its streams. What `qb` itself said is quoted, capped, so a wrong guess about a program
in another repo corrects itself in front of the reader.

The sentence lands in **`config_notes`** — which puts it in the payload a fixer is briefed
from, in the report, and in the `--post` PR comment — and in the `--json-file` on disk,
because the recording is attempted *before* that file is written. The refusal notice carries
it too. And it names its own recovery: `--json-file` writes exactly the bytes piped to `qb`,
so `qb record-review < PAYLOAD.json` from any host with one puts the round on the board
later. `run_key` makes the replay a join rather than a double-count.

Note that `gh pr view --json` **fails the whole command** on a field it does not recognise
rather than omitting it, so there is no graceful degradation on an older `gh` — the run
exits before any review. `panel.py` needs a `gh` carrying `files`, `changedFiles`, `state`
and `isDraft`.

Run-level fields worth knowing about because their meaning is conditional:

| field | what it is |
|---|---|
| `scope` | `pr` \| `increment` — what this round actually reviewed. Recorded rather than inferred from the round number, because scope falls back to `pr` whenever the anchor is missing or the range is unusable, so "round 2" does not imply "increment" |
| `since_sha` | the anchor the increment was taken from; null under `pr` scope |
| `diff_chars` | the size of the **review target** — the whole PR under `pr` scope, the increment under `increment`. Read `scope` beside it: plotting this across a cycle's rounds without doing so shows a cliff at round 2 and reads as a shrinking PR |
| `pr_chars` | the size of the **whole PR**, whatever this round reviewed — the same number as `diff_chars` under `pr` scope, and the PR's own size under `increment`. This is the line that does *not* cliff at round 2, and it is what `max_fix_growth` measures at both ends (#298). Under a `manifest` verdict both measure the manifest, which is what was sent |
| `guard_ratio` | how much **apparatus** this change is carrying (#492): `{test, doc, source, guard, ratio}`, counting lines **added** on each side of the whole PR's diff, with `guard` = test + doc and `ratio` = guard / source. Present on every reviewed round including the **first**, which is the point — `max_fix_growth` needs a second round before it has a ratio at all, and the cycle this was filed from produced 406 lines of test for a 66-line config change. `ratio` is null where the change adds no source (undefined, not infinite); the whole field is null where the round measured nothing. **Reported and gating nothing** (#67) — it earns a threshold over a few dozen cycles or not at all, and as of #618 that is a **decision** rather than a deferral: the ratio fell 2.21 → 2.19 → 2.13 → 2.09 → 2.02 across five rounds in which both of its halves nearly doubled, so a ceiling on it would fire on well-guarded changes and stay quiet on the one shape it exists to catch. What #618 bounds instead is the guard churn of a single **fix pass** — `round_stop.guard_churn`, and `max_fix_guard_lines` |
| `preflight` | the verdict the round was weighed against before it ran, on every exit that reached it: `verdict` (`run`/`manifest`/`refuse`), `reason`, `cap`/`cap_seat`/`cap_unit`/`over_cap`, `forced`/`would_have`, the `thresholds` in force, and `shape` (`chars`, `bytes`, `added`, `removed`, `moved`, `move_ratio`, `files`, plus `files_added_only`/`files_removed_only` — the one-sided file lists, capped at 40 paths each with a `_elided` count beside them, because this block rides in every board record and a 700-file refactor wrote 700 paths into each one). `cap_unit` is `chars` or `bytes` and says which reading of `shape` the `cap` and the `over_cap` beside it are in — a ratio a consumer cannot attribute to one of the two readings cannot be checked at all. `over_cap` is null when and only when no ceiling was declared: a measured ratio is emitted even where it rounds to 0.0, so "small against a real ceiling" and "no ceiling" stay different answers. **`null` means the run never reached the verdict** — the title-pattern skip returns before it — which is a different statement from a `run` verdict, and the difference is what answers "was this PR ever weighed?" |
| `preflight.shape` | measured on the **review target**, so it is scope-dependent exactly as `diff_chars` is: the whole PR under `pr` scope, the increment under `increment`. Read `scope` beside it. Under a `manifest` verdict it is also the only place the target's *pre-substitution* size appears, because `diff_chars` then measures the manifest — which is what was reviewed |
| `context_chars` | everything prepared *alongside* the target; 0 under `pr` scope. With `diff_chars` this is what an uncapped reviewer was given. Neither is a per-reviewer number — budgets are, so a seat that got less says so in `reviewers.<name>.max_diff_chars` and `.truncated`, and a seat that got the whole target and only part of the context is named in `config_notes` |

Each finding record:

| field | what it is |
|---|---|
| `id` | run-local (`1609-F03`), and only for resolving `related` **within one payload** — it is a position, so it moves between runs |
| `key` | the defect's identity **across** runs: file + the reporting reviewers' own words, so a re-review joins the same chain even though the judge re-words its synthesis every time |
| `synthesis` / `detail` | the merged statement and its body (the board stores these as `title`/`detail`) |
| `verdict` | `confirmed` \| `dismissed` \| `unjudged` \| `sonar` |
| `reported_by` | one entry per reviewer: its own `title`, `detail`, `severity`, `line`, and `account` (the two joined, for reading) |
| `reviewers`, `related`, `rationale` | who reported it, sibling findings from one cause, and the judge's reason |
| `needs_rereview`, `rereview_by` | a reporter declared that fixing this takes a structural change whose *result* should be read again, and which reporters said so — the declaration the next round is checked against |
| `new_this_round` | no earlier round of this cycle raised this defect (`--baseline`); `false` on a repeat. A run with no baseline has no earlier round, so every finding is `true` — which is why a round past the first with no `--baseline` is a veto rather than a clean sweep |
| `provenance` | **v2.24.** Which of the two things a `new_this_round` finding is: `introduced` (on a line the last fix pass wrote), `missed` (present in the earlier round's diff and not seen), `missed-unread` (in a file that round was truncated out of — a coverage failure, not a reviewer one), or `unknown` (no readable fix range, or a finding with no line to place). `null` where the question does not arise: outside a cycle, in round 1, or on a repeat — a repeat's provenance is not unknown, it is not asked, because the defect predates the fix pass under attribution |
| `recurrence` | **#67.** Where the finding stands relative to the fix pass before it: `revisited` (the previous round complained about this file, the fixer wrote lines in it, and this finding is within ~20 lines of them), `fix-site` (the fixer wrote here and nobody had complained), `elsewhere`, or `unknown` (no readable fix range, no line, or a path that could name two changed files). `null` where the question does not arise, on the same three occasions `provenance` is null. **A position, not a verdict** — see below |
| `recurs_of` | **#67.** The `key` of the earlier finding this one stands on, under `revisited` only. What makes the bucket checkable: an uncalibrated label nobody can trace back to the record it came from is not evidence |
| `premise_verdict` | **#67.** The judge's own answer, asked as one extra key on the verdict it already writes: does this finding show the fix before it was built on a wrong assumption (`invalidates`), is it a separate defect (`separate`), or can it not be told (`unclear`)? `null`/absent where the question was not put — every round with no earlier round — which is not `unclear` |

Provenance is a **signal, not a verdict**, and nothing gates on it. A fix can break something at a
distance, so `missed` is evidence of a miss rather than proof of one — the same discipline as
`rereview_hit` being file-grain and saying so. #41 (review the increment) is what would make it
exact, at which point a finding in the increment is introduced by construction.

Its known biases, since the defence of a heuristic is that they are written down:

- **A base branch merged into the PR between rounds** lands inside the fix range, so lines the
  fixer never wrote read as `introduced`. A branch *rewritten* between rounds (rebase, force-push)
  is caught — GitHub calls that compare `diverged` and provenance refuses it — but a merge is
  genuinely "ahead" and indistinguishable from a fix commit at this grain. Under `increment` scope
  the merge bias is gone anyway (**#512**): the round attributes against the diff its seats read,
  which is narrowed to the PR's own files.
- **A REBUILT range is EXACT or it is refused** (**#504**). A rewritten branch is no longer the
  end of the road: the fix pass's commits are still on it under new SHAs, `patch-id` names them,
  and provenance, recurrence and `escalate_on.fix_injection` come back rather than every finding
  landing in `unknown`. What is deliberately NOT here is a leaning reconstruction. Every inexact
  shape declines instead — a prior commit whose content changed in the rewrite (a conflict resolved
  during the rebase, an amended tip), a pass that is not the TAIL of the branch, an ambiguous
  patch-id, a rewrite where none of the prior round's commits came through, and a branch reset
  BACKWARDS where the pass was removed rather than rewritten. `fix_range_rebuilt.why` names which.
  The reason is not that a lean is dishonest — it was reported in `config_notes` when this leaned —
  but that the threshold above is calibrated on `introduced` being a FLOOR ("a measured 0.64 is at
  least 0.64"), and a source that over-counts breaks that argument at the cost of a cycle stopped
  with real findings unfixed. Nothing reads a note before firing a brake. What survives is one net
  two-dot diff of the pass, numbered in the head's own tree: the ordinary rebase, which is the case
  worth having.
- **Two changed files a finding's path could name** (a reviewer writes `panel.py`; the diff has
  two of them) yields `unknown`, not a coin toss.
- **A defect the fix pass introduced by DELETING something reads as `missed`.** The fix range is
  reduced to the lines the fix pass *added*, so removing a guard, a null check, a `finally` or an
  `await` introduces a defect with no added line to place it on. `introduced` under-counts by
  however much of the fix pass was subtraction, and `missed` absorbs it.
- **`introduced` requires exact line membership, and reviewer line numbers drift.** LLM reviewers
  routinely report a line a few off — the top of the enclosing function, the closing brace, the
  line after the defect — and Sonar reports the issue's own anchor, which need not be a line the
  fix wrote. Each of those misses the added-line set and comes back `missed`. Both biases push the
  same way, so read `introduced` as a **floor** and `missed` as the bucket that absorbs whatever
  the arithmetic could not place.
- **A line the fix pass RESTORED is no longer counted as a line it wrote** (**#559**). Attribution
  is over the lines a range ADDS, and a revert-of-a-revert adds code an earlier round of the same
  cycle already reviewed — so a pass repairing a bad revert read as the pass that authored all of
  it. The measured case is lexray `343d1f15`: ~90 restored lines, byte-identical to what rounds 1
  and 2 had in front of them, every one attributed to the fixer, on the statistic that ENDS the
  cycle. The remedy for the gate looked to the gate like the disease. Nothing in the baseline chain
  reaches it — `--baseline` keys FINDINGS on content, and this is computed over LINES — so a round
  could report "0 findings no earlier round raised" beside a high `introduced` share, and only the
  second one stops cycles. What is excluded now is a run of at least
  `panel_scope.RESTORED_RUN_MIN` (**5**) consecutive added lines byte-identical, in order, to lines
  the same file carried at the head of a round BEFORE the one the range is anchored on AND does not
  carry at the anchor itself. A run and not a line, which is the one judgement here: per-line —
  #559's own proposal — a blank, a `}` and a `    return None` match almost any file, so it would
  quietly exclude a share of every fix pass ever written and leave `fix_injection` a brake that
  cannot fire. And a ROUND TRIP rather than half of one: restoration means the content left the
  branch and came back, so a block the branch still carried at the anchor is a copy the fixer
  pasted — authorship, and a copy-paste defect is one of the commoner things a fix pass introduces.
  Matching an older head alone lets that out of the count. Both boundaries err the same way — where
  this is unsure it leaves the line attributed, so `introduced` stays a floor. Reading a file
  at a commit is local git, so a run with no checkout, or one whose earlier heads a rewrite
  orphaned, cannot make the comparison: it says so in `config_notes` and in
  `provenance_restored.why` rather than reporting an unfiltered number as a filtered one, and
  a run that read only some of the cycle's earlier heads says which it missed. A file whose ANCHOR
  version could not be read is dropped from the filter rather than matched on half the evidence,
  which is the same rule from the other side — and so is a WINDOW the anchor is too repetitive to
  search. A window whose rarest line occurs more than `panel_scope.RESTORED_MAX_REPEATS` (**64**)
  times is refused rather than searched, since nothing in it is distinctive and calling five such
  lines a restoration is a guess; the refusal is carried as REFUSED and never as "not there",
  because the two sides read a miss in opposite directions. At the anchor, "not there" is the half
  of the round trip that ADMITS the exclusion, so a refusal folded into it made the guard against
  guessing about generated content into the guess itself — and took a fix pass's own lines out of
  `introduced` on exactly the file it exists to refuse to judge. **And "an earlier round" is
  ANCESTRY rather than the round number**: a head that is not an ancestor of the anchor is named in
  `unread` and left out. An explicit `--since` older than the last round's head puts a
  lower-numbered round's head INSIDE the range being attributed, where feeding it in says the
  content predates a range the fix pass wrote it in the middle of — which in the limit filters out
  most of the range and leaves `fix_injection` unable to fire at all. The
  comparison splits on the newline alone rather than on `splitlines()`, whose extra
  separators (`\x0c` and friends) shifted every added line number after a form feed. The one
  thing it normalises is a trailing `\r`, on both sides and deliberately: a CRLF-to-LF
  conversion therefore reads as restoration, which is the right answer — the content is what
  an earlier round reviewed — and the alternative would leave a brake decided by whether
  `subprocess(text=True)` happened to collapse a carriage return on one side of the
  comparison and not the other.
- **Sonar's hard-gate issues carry provenance and are counted with the rest.** They are PR-scanned
  — SonarCloud's new-code view — so the same reading holds; a Sonar issue that predates the PR
  would read `missed`, and that is the scanner's file scope rather than the panel's under-reading.

### The cycle so far (#490): every round beside the others

Every round's report used to state that round's own figures and nothing else, so a reader — human
or orchestrator — had to hold three reports in their head and do the arithmetic to see which way a
cycle was going. Read one at a time, a diverging cycle looks flat. This is the block that puts the
rounds side by side, from round 2 onward:

```
round  findings  P1/P2  introduced  whole PR  vs r1
   r1         8      2           —   113,402  1.00x
   r2        14      5     9 (64%)   236,187  2.08x
   r3        15      4    13 (87%)   340,341  3.00x
```

8 → 14 → 15 findings reads as converging. Against a PR that tripled on an underlying change of 113
lines it is the opposite reading, and it was available from data every round already had — the
cycle this comes from ran to its end before anyone computed it.

| column | what it is |
|---|---|
| `findings` | that round's `to_fix` + `sonar_findings`, the population `outstanding` counts. `dismissed` is out: the master ruled those not real and no fixer will ever touch them. `?` where a bucket is present and is not a list of finding records — see below |
| `P1/P2` | how many of them were at or above P2. An unreadable severity counts as severe, which is `severity_at_least`'s standing asymmetry — a row that under-states severity is a row that argues for another round |
| `introduced` | that round's `provenance_counts["introduced"]`, and its share of that round's own findings. Three states, told apart by `attributed()`: `—` is round 1, where the question does not arise; `?` is a round that was asked and could not answer, meaning `unknown` was the only bucket with anything in it; and `0` is the round that attributed and had nothing to attribute — a round of repeats, or one with no findings. A failed attribution is never printed as `0`, which would be a claim about the fix pass made from a measurement that did not happen |
| `whole PR` | `pr_chars`, the size of the **whole PR** whatever that round reviewed (#298). Never `diff_chars`, which under `increment` scope is one fix commit and would show the change shrinking exactly while it grows |
| `vs rN` | that size over the size the cycle's **first reviewed round** found the PR at — `Baseline.first_reviewed`, which is `max_fix_growth`'s own denominator. One denominator, so the block's ratio and the ceiling's veto line are the same measurement; where it is missing the ceiling does not run either and the column says `?` rather than picking a substitute |

A round that reviewed nothing (a title skip, a pre-flight refusal) prints `not run` and no numbers:
`0 findings` there would put the strongest convergence signal the block can show against a round
that never happened. A payload with no `reviewed` field reads the same way, which is how
`first_reviewed` and the coverage record read that same silence — one answer to one field across
the module, rather than a row saying something the ratio beside it disagrees with.

**A count never degrades to a smaller number.** The rest of `load_baseline` reads the finding
buckets tolerantly — a record that is not a mapping is skipped and the round keeps the others —
because what those reads build is the `keys`/`titles` sets, where a dropped record makes a repeat
read as new and buys a round nobody needed: the safe direction. A count has no such direction.
`"to_fix": "corrupt"` iterates into single characters, each fails the mapping test, and a tolerant
count reports **0 findings** from a payload nothing was read out of. So a bucket that is present
and is not a list, or a list holding anything that is not a mapping, makes that row's counts `?`.
An *absent* bucket is still empty, which is what an older schema's silence means. `introduced` is
bounded by the population it is a share of, for the same reason: provenance is tallied over the
very findings counted beside it, so a payload claiming more introduced than found is an
inconsistent pair rather than a large measurement, and it reads `?` instead of `20 (2000%)`.

**One row per round, not per payload.** Two files claiming round 2 are not two rounds, and a column
carrying two `r2` rows with different figures in it cannot be read down — which is the whole of
what the block is for. The ambiguity is still reported in `config_notes`, and the row kept is the
last-written of them: the same `(round, mtime, path)` tie-break that already decides which payload
supplies the anchor and the coverage record, so the row and the commit that round is attributed
against come from one file.

**A cycle whose earliest baseline reviewed nothing gets no ratios at all**, and that is #298's
refusal rather than a gap here: `Baseline.first_reviewed` reads the earliest accepted baseline and
nothing later, so there is no starting size and `max_fix_growth` does not run either. Scanning
forward for the first round that *did* review would change when that ceiling fires, which is a stop
condition and not this block's to move. Where the round-1 payload was simply never passed, the
earliest baseline there is becomes the denominator and the header names it (`vs r2`).

**There is deliberately no density metric, and adding one takes an argument.** While reading the
cycle above by hand the reporter computed findings-per-10k-chars and got **9.46 → 7.97 → 4.82** — a
number that falls every round, reads as steady improvement, and is describing a cycle that was
diverging. It falls because the denominator is growing, which is the failure rather than evidence
against it. Any per-size figure added here must sit beside **both** the absolute count and the
growth ratio, or it will mislead in exactly the case the block exists to expose;
`tests/test_panel_trend.py` asserts the rendered rows whole and pins the absence.

**#618 added three columns and no fourth.** `prod`/`test`/`prose` sit beside `introduced`, because
that is the column they complete: `introduced` says how many of a round's findings the pass
before it authored and nothing said what that pass *did*. `round_stop.unrefereed_fix` had
computed exactly this
split since #554 and printed it in a line of its own — one round at a time, which is the form that
cannot be read *down*. On `lexray#1780` the fix passes after round 1 wrote 1,313 lines of which 848
were test and doc, and the block a reader consults for the shape of a cycle rendered it as flat.
Three columns rather than one combined cell for the same reason the block exists at all. `—` where
there was no fix pass to read (round 1), `?` where there was one and it could not be read.

**It is reporting and nothing else.** No dial, no gate, nothing that can end a cycle: `round_stop`
does not read it, no ceiling in `panel_caps` reads it, and it does not reach the fixer's brief. It
is worth having whether or not the injection-rate stop of #489 is ever wired to `round_stop`, and
chaining a cheap reporting improvement to a policy argument is how the cheap half waits on the
expensive one.

The rows are rebuilt from each baseline's **raw** per-round fields every round — the finding
buckets, `provenance_counts`, `pr_chars` — never chained from an earlier round's computed copy. So
a cycle with a skipped round in the middle, or one spanning the release that added this, still gets
a complete block instead of a tail.

### Recurrence (#67): is the loop making progress on this defect?

`provenance` asks whether the last fix pass *wrote the line* a finding sits on. `recurrence` asks
the question after it — was that fixer *sent* here? A fix that patches a wrong assumption produces
the next round's findings; a fix that removes the assumption does not, and a round cap fires at the
same point either way.

**Nothing gates on it, and the first calibration is why that is right rather than merely cautious.**
The mechanical bucket was replayed over 36 rounds from 26 pull requests — every multi-round cycle
the board holds — against the three cycles #67 identifies as circling (#61, #29, #88) and every
other cycle as the control:

| narrowing | #61 / #29 / #88 | every other PR |
|---|---|---|
| same file + within 20 lines | 83% | 69% |
| same file + within 5 lines | 79% | 64% |
| same file + exactly on a written line | 65% | 52% |
| …and the earlier finding within 20 lines | 29% | 27% |

It does not separate them at any radius, and tightening the rule lowers both columns together. The
reason is legible in the runs: under increment scope (#41) a later round **reads the fix commit**,
so a new finding at the fix's site is the ordinary case rather than the exceptional one. That is why
the bucket is named for a position (`revisited`) and not for a verdict (`circling`), and why the
count is printed with no recommendation attached.

The half that can see a repeated *premise* is the judge's, because it is the only party in the round
holding both the earlier round's complaints and the commit that answered them. It gets them in a
brief spliced into `JUDGE_PROMPT` at a slot that is swapped for the empty string on every round with
no earlier round — so a round-1 judge prompt is byte-identical to the one it has always been given —
and it answers on the verdict it is already writing, so the question costs no second model call. The
brief spends most of its length pushing *away* from `invalidates`: a second bug in a file somebody
just edited is a second bug, and a heuristic that triggers redesigns cheaply is worse than the cap
it would replace.

Both answers are stored, side by side, and never folded into one number — the rounds where the
mechanical count and the judge disagree are the ones worth a human's attention.

Not to be confused with **#84's premise register**, which is the other thing in this loop called a
premise and *does* brake: that one is a fixer's own declaration of what it is about to fix on, and a
repeat stops the pass. This is an adjudication of a finding. #67's record of PR #88 is the argument
for keeping a self-report and an adjudication apart — the agent that wrote round 1's fix wrote
round 2's regression of the same shape, in the same commit as a docstring stating the invariant it
broke.

Run-level fields it depends on:

| field | what it is |
|---|---|
| `head_sha` | **v2.24.** The commit this round reviewed. Recorded because nothing else identified one — `base` holds a branch *name* — and the next round needs it twice over: as one end of the fix range, and (**v2.28**) as the anchor its increment is taken from. Re-read straight after the diff is fetched, which narrows the mid-round-push window without closing it: a push can land either side of the fetch and nothing can tell which, so a move is reported as a move (`config_notes`) rather than as a claim about which commit produced the diff, and the later commit is recorded because it is where the next round's fix range starts. **Under increment scope a move also takes a veto line** (#106): the round's target was decided against the earlier head and the next round anchors on the later one, so the commits between them are reviewed by no round, and `confident` — and with it `converged` — is false rather than the gap being left to be inferred from a note. That is a report of the hole and not a repair of it; the repair is a second field naming the head a round actually reviewed. Present on the **skipped** payload too: a skipped round is still the round the next one baselines against |
| `unread_files` | **v2.24.** Files no reviewer that ran read in full, for the next round's `missed-unread`. A file counts as unread only if *every* running reviewer was cut on it, and a file straddling the cut counts as unread — half a file's hunks is not a read file. Empty on a payload whose `reviewed` is `false` means *no coverage at all* (a skipped round never fetched a diff to name files from), not "read everything" — the consumer tells the two apart by `reviewed` |
| `provenance_counts` | **v2.24.** The per-round tally over the findings the cycle has to clear, so a consumer gets the shape of a round without walking every finding. `{}` where the question does not arise — outside a cycle, or in a cycle's round 1, which has no earlier round to attribute against. All-zero is the other statement: a round that could have attributed and had nothing to, which is what a **skipped** in-cycle round sends |
| `fix_range_source` | **#512.** WHICH range the round attributed against, because the answers are not the same measurement and a reader comparing `introduced` across a cycle has to see the denominator change. `increment` — the diff the seats actually read, which is narrowed to the PR's own files and so drops a base-branch merge's; `compare` — the separate `gh api compare` fetch, used under `pr` scope and wherever the increment fell back; `reconstructed` (**#504**) — a rewritten branch's pass, rebuilt from the local object store by patch equivalence; `null` where the question does not arise (round 1, or a round with no range at all) |
| `fix_range_rebuilt` | **#504.** #504's working, and `null` on every round that did not have to rebuild — which is nearly all of them. `commits` are the SHAs it called the fix pass, `prior` how many the last round had reviewed, `carried` how many of those came through the rewrite with their patch intact, and `unmatched` the difference . On a round that ATTRIBUTED those are always n/n/0, because an inexact reconstruction declines rather than leaning; what they are worth is on the declines, where they say how far apart the two histories were and whether a rebase or a force-push is the thing to go and look at. `why` is set when it declined, and the reconstructed patch itself is deliberately NOT here — the payload is a file, a `--baseline` and a board record, and nothing downstream reads a copy of the fix pass, and a decline leaves the round exactly as blind as #500 found it: #509's veto still fires and nothing is attributed |
| `provenance_restored` | **#559.** What this round declined to attribute because the cycle had already seen it: `count` lines across `files` files, compared against the head commits of `rounds`. `unread` names earlier rounds it could NOT use in full — an orphaned head, an unreadable blob, or a head that is not an ANCESTOR of the anchor and so sits inside the range being attributed rather than before it — which makes `count` a floor rather than the whole answer; that is reported rather than declined, because a filter reading three of a cycle's four earlier heads still lands between the unfiltered number and the exact one, while declining would put the round back on the unfiltered count in the case the filter is most needed. `why` is set instead — with `count` 0 — where the comparison could not be made at all (no local checkout, or no earlier head this box holds), which a reader has to be able to tell from a round that looked and found nothing. `null` where the question does not arise: outside a cycle, and in round 2, whose only earlier round IS the anchor. Published because #637 recalibrates `escalate_on.fix_injection`, and a threshold fitted across filtered and unfiltered rounds alike is fitted to a denominator that changed under it |
| `cycle_trend` | **#490.** The cross-round block as data — one object per round of the cycle *including this one*: `round`, `reviewed`, `findings`, `p1p2`, `introduced`, `production`, `test`, `prose` (**#618** — what the fix pass *before* that round churned, so a pass that wrote 330 test lines and a pass that wrote 330 production lines are not rendered as the same event), `pr_chars`, `growth`. Every count is nullable and none defaults to 0, because "did not measure" and "measured zero" are opposite readings and this block exists to stop that confusion. `growth` is stored rather than left to be recomputed, since its denominator is the cycle's first reviewed round's size and not this row's. `[]` outside a cycle, on the same rule `new_findings` and `round_stop` use |
| `recurrence_counts`, `premise_counts` | **#67.** The two tallies over the same population, on exactly `provenance_counts`' terms (`{}` = the question does not arise; all-zero = it was asked and there was nothing). Two objects rather than one, because the whole value of asking twice is that they can disagree. `premise_counts` carries a `not-said` bucket — the judge having nothing to say is the commonest answer, and a shortfall against a denominator stored elsewhere would invent it |

A baseline written before v2.24 carries no `head_sha`, so provenance degrades to `unknown` rather
than attributing findings against a range it invented.

**Breaking, v2.14:** the per-finding keys were `title` / `detail` / `reason` with a
`reviewers` name list. They are now `synthesis` / `detail` / `rationale`, and `id`,
`key`, `verdict`, `related` and `reported_by` are new. The board accepts both spellings
(`POST /review` aliases `title`↔`synthesis` and `reason`↔`rationale`); any other consumer
has to be updated.

## Epic driver (`epic.py`)

`python3 ~/.claude/loops/epic.py --epic <n>` (plan) / `--execute` (run the pipeline).
Per sub-issue: create worktree → `/fix-issue` → CI → panel → `/review-pr` → stop at
the human merge. Merge is never automatic. Run state in `~/.local/state/loops/`, so a
killed run resumes instead of re-deriving from GitHub.

Both skill runs are captured (and passed through, so the log stays live). The driver
already refused to call an issue done without a PR; it now records *why* there wasn't
one — the agent's own last sentence, which the torn-down worktree took with it before.
A `/review-pr` that exited non-zero or printed nothing is a **failure**, not a review:
`reviewed` is the outcome that lets the driver stack the sub-PR into the epic branch,
so it would otherwise merge a PR whose findings nobody addressed. A re-run sees the
open PR and retries the review stage. A review that ran and pushed nothing (the PR's
head SHA is where it was) is *reported* rather than failed — finding nothing to fix is
legitimate, and by the last round it is the point — but it is reported with the agent's
own last sentence, because it is the same shape as a review that was stopped from
fixing anything.

## Issue watcher (`issue_watch.py`) — #63

```bash
python3 ~/.claude/loops/issue_watch.py --repo quarterback            # survey the backlog
python3 ~/.claude/loops/issue_watch.py --repo quarterback --issue 63 # one issue (exits 3 if held)
python3 ~/.claude/loops/issue_watch.py --repo quarterback --json     # machine-readable
python3 ~/.claude/loops/issue_watch.py --repo quarterback --announce --comment
python3 ~/.claude/loops/issue_watch.py --repo quarterback --start --dry-run  # every refusal, nothing started
python3 ~/.claude/loops/issue_watch.py --repo quarterback --start           # ...and act on the actionable
```

Reads a repo's open issues and says, for each one, **what it is waiting on** — a human
decision, an open dependency, a triage ruling, or nothing. With `--start` it hands the
ones waiting on nothing to `qb-start`; without it — the default — it writes no code,
opens no PR and starts no session.

**The refusal is still the deliverable, and acting is still what needs justifying.** #63
measured why: of the twenty-one issues filed here on one day, several existed precisely to
force a decision, and an agent handed one of those with `/fix-issue` does not stop — it
picks an architecture, implements it, opens a PR, and the decision has been made by
whichever model was cheapest that morning. So the survey half runs on every repo and the
acting half runs on almost none.

### Reaching a session takes four yeses, and no two live in the same place

`--start` is the follow-up #63 deferred, and what makes it safe is not this flag — it is
that this flag is the *only* one of the four a run can supply for itself:

| | says | lives in |
|---|---|---|
| the repo | may a loop choose work here at all? | `issue_pickup.enabled`, off by default |
| the issue | is this one settled? | no held signal, and `epic.triage` confirming |
| the run | may I act this time, and how much? | `--start`, off; `--start-max`, 1 |
| **the machine** | does this box start sessions? | `qb-start`'s policy file |

**Be precise about what the last one buys.** It lives in the user's config directory, ships
absent, and fails closed, and **nothing here reads repository-controlled content as policy** —
no file in a checkout is consulted to decide whether a session may start. That is what stops
the tracker becoming an authorisation channel, which is the thing #63 was filed about.

What it does *not* stop is a party that already has arbitrary execution as this user. A
`CLAUDE.md` is repository content read by an agent holding a shell, and that agent could write
`spawn.json` itself, shadow `qb-start` on `PATH`, or just run the agent binary directly. No
same-UID permission gate closes that, and `qb-start`'s own notes make the same argument about
`XDG_CONFIG_HOME`. Moving the boundary means an authority outside this UID — a different issue.

### Two budgets, because one counts the wrong thing

`--start-max` (default **1**) counts **sessions started**. `--attempt-max` (default **5**)
counts **spawn requests**, started or refused. Neither is `qb-start`'s own cap, which bounds
how many spawns may be *live* on a box.

"Spawn requests", not "calls to `qb-start`": the once-per-run `qb-start --policy` probe is a
question, not a request — it posts nothing, claims nothing and reads only a local file — so it
sits outside the budget, and `--attempt-max 5` permits five requests plus that one probe. A run
whose budget is zero returns *before* probing, so a freeze genuinely asks nothing.

The second exists because the first cannot express the runaway it claimed to prevent. A
refusal about a single issue — somebody else holds it — correctly does not stop the sweep and
correctly starts nothing, so it spends none of `--start-max`; thirty held issues therefore made
thirty invocations and thirty board posts while `--start-max 1` looked like it was holding.
Measured, not theorised: a codex review of this file caught the contradiction.

A refusal about the **box** (not enabled, at cap, paced, full, no tmux) stops the sweep. One
about a single **issue** (somebody holds it) does not. One about a **command** — this machine's
policy does not allow `/fix-issue` — is remembered and not re-asked, because unlike a held
issue there is no chance the next one answers differently.

Both ceilings refuse a negative value at the CLI. `0` is a legitimate freeze and stays legal.

Every actionable issue comes back carrying what became of it — `started`, the refusal in
full, or `not attempted` with the reason the sweep never reached it — on the report and in
`--json`. "The watcher declined this" and "the watcher ran out of room" are different
answers to *why did nothing happen*, which #63's acceptance asks to be answerable after the
fact.

### The two questions, and why they are not one

| | asks | answered by |
|---|---|---|
| the gate | may a loop CHOOSE this issue, unprompted? | `appetite.pickup_verdict` |
| decision owed | has a human settled WHAT TO DO? | the signals below |
| doable | can a coding agent implement it? | `epic.triage` |

There is **one** pickup gate on the fleet and this is a consumer of it, not a second
one: `issue_pickup.enabled` is off, `only_labels` and `allowed_authors` are empty, and
nothing here relaxes any of that. `epic.triage` is reused whole — the same
`(doable, reason, impl_model, human_class)` verdict, with the same `doable=None` meaning
*no judgement was possible*, treated as not-confirmed. What triage does not answer is
whether a **human** has decided, and #63's own example is that #51 was perfectly
implementable and must not have been implemented.

### The signals that a decision is owed

Six, and each carries the evidence for it — "a decision is owed" with nothing behind it
costs somebody an interruption, which is the rule `needs_human.announce` already applies
to a flag with no reason.

| signal | fires on |
|---|---|
| `needs_human_label` | a `needs-human/*` label, via `appetite.refusal_verdict` (not a second reading of `skip_labels`) |
| `open_questions` | an *Open questions* heading — a section, not the phrase in a sentence |
| `unrecommended_options` | two or more `Option X` and nothing in the thread that reads as a ruling |
| `unanswered_question` | the newest utterance asks something and nothing follows it |
| `open_dependency` | `depends on #N` / `blocked by #N` / `once #N lands` (`epic.DEP_RE`) where #N is not closed |
| `decision_shape` | the title is a question, or a question in the body puts a choice |

They are deliberately eager: a false positive costs a report line a human waves through
and a false negative costs an unwanted PR against a question nobody answered. Two places
where that eagerness is bounded rather than indulged, both because a signal that fires on
most of the backlog says nothing about the six issues that matter — a choice phrase must
appear inside a **question** (`which of the` turns up in ordinary description), and code
fences are stripped first (a traceback holds question marks; a config sample holds the
word "option"; the one issue class this exists to let through is the one with a good
repro).

**Anyone's text may RAISE a signal; only an allowlisted author's may SETTLE one.**
The allowlist bounds who may open an issue that drives an agent, but anyone can comment
on anybody's issue — so without this asymmetry "**Decided:** option B" is a sentence a
stranger can write on a public repo to take the brake off, and a reply of any kind
underneath a question closes it. Raising a hold costs a report line a human waves
through; clearing one spends money and writes code, and the two do not get the same
admission. For the same reason the watcher's **own** comments are not part of the
conversation: its refusal is the newest thing on the thread and is signed by an
allowlisted account, so counting it would answer the very question it was posted to
point out and make the issue actionable on the next run.

A closed issue earns no action whatever the config says. A backlog sweep only ever sees
open ones, but `--issue 63` names one and `gh issue view` answers about a closed issue
just as readily.

`skip_when_unlabelled` deliberately does **not** apply to the survey half.  That setting
answers "may I select this out of an untriaged backlog", which is the gate's question and
`pickup_verdict` has already asked it; asking it again here would report every unlabelled
issue as one a human owes a decision on, and bury the ones that do.

### The ladder, and what it costs

Gate → signals → judge, in that order. Only an issue the gate admitted and no signal held
is shown to a model at all, which is the cost control and the security property at once:
this repo is **public**, anyone may open an issue, and under a watcher that text becomes
an agent's instructions. `issue_pickup.allowed_authors` is the mitigation and it is an
allowlist rather than a filter — a filter is a list of the phrasings somebody already
thought of. A stranger's issue is surveyed and reported and its text never reaches a
judge.

At the end of the ladder the watcher names an action, and runs it only under `--start`.
`/investigate` is the default rung and `/fix-issue` the escalation, because
`/investigate` produces understanding and writes no code, so it is the safer answer
whenever the choice is close. Escalating wants evidence that somebody wrote down what
"fixed" means — an *Acceptance*, *Steps to reproduce* or *Expected/Actual* heading.

That ladder is why `/investigate` was added to `qb-start`'s `SPAWNABLE` alongside this
work. The allowlist previously held `/fix-issue` and not `/investigate`, which would have
made the safe rung the unstartable one: a close call would have had the choice between
writing code and doing nothing, with the cautious answer unavailable.

### Saying so

`--announce` posts a held issue to the board through `needs_human.announce` (one door for
every escalation the harness raises, #274); `--comment` says the same thing on the issue,
where the person who can answer it is already looking. Silence is indistinguishable from
a broken watcher, which was #63's diagnosis of half the tracker.

Both are opt-in flags and both are bounded:

- Only an issue a **signal** held. A gate refusal is the repo's standing answer, not a
  question about this issue — announcing one per issue would post the whole backlog the
  first time a watcher ran.
- Only an issue whose **author is on the allowlist**. Commenting does not need
  `issue_pickup.enabled` (saying what an issue is waiting on chooses no work), but it does
  need `allowed_authors`: a stranger who could make the watcher comment could make it
  quote them under the repo owner's account, a thousand times.
- Not under `HARNESS_UNATTENDED=1` unless `issue_filing.unattended` says so — the setting
  a repo has already answered "may a loop write here with nobody watching" with, rather
  than a second switch that would come to disagree with it.
- Never twice for the same thing: the comment carries a digest of its signals, so an
  issue whose questions get answered and replaced is a new thing to say rather than a
  suppressed one.

## Pre-land verdict (`preland.py`)

```bash
python3 ~/.claude/loops/preland.py --pr 131            # the report
python3 ~/.claude/loops/preland.py --pr 131 --json     # the payload a loop reads
python3 ~/.claude/loops/preland.py --pr 131 --require-earned-stop   # for the caller that ran the round
```

**May this PR be merged, and if not, what is outstanding.** One answer, three values,
exit codes deliberately shared with `scripts/migration_reconcile.py` so the two tools
never mean different things by the same number:

| Verdict | Exit | Means |
|---|---|---|
| `READY` | 0 | Every check that ran is satisfied. |
| `RECONCILE` | 3 | Mechanical work is outstanding; `actions` holds the exact commands and the files they touch. Do them, then run this again. |
| `HOLD` | 2 | Something is unresolved that the loop must not resolve itself. `reasons` says what, and who has to. |

HOLD dominates RECONCILE — relinking a migration graph on a PR nobody reviewed is work
spent to reach a wall. Note that 2 is also argparse's usage exit; the JSON payload is what
tells them apart, and since both mean *do not merge*, a caller that conflates them fails
safe.

**Why it exists.** The checks a merge must pass used to be prose in two places:
`/fix-and-land` §4 described them in about fifty lines of English, and `/panel-review-pr`
§7 was a bare `gh pr merge` with nothing in front of it. Prose in two files drifts, has no
exit code, and cannot be asked afterwards whether it ran — and a model reading it is
invited to re-derive a decision it should be executing. On 2026-08-16 a PR was merged on
`mergeable` + CI-green over its own panel round (8 P1s and 12 P2s outstanding) by an agent
that had written up that exact confusion an hour earlier.

**What it checks**, all of it mechanical, none of it a proxy:

| Check | Holds when |
|---|---|
| `pr_state` | Not OPEN, a draft, or `CONFLICTING`. Uncomputed mergeability warns. |
| `checkout` | This tree is not at the PR's head, or has tracked modifications. Untracked files warn. |
| `ci` | Anything but `green`. Six states since [#324](https://github.com/prisonblues/quarterback/issues/324) — `green`, `red`, `pending`, `blocked` (a run exists and is waiting on a human to approve it, so it has executed nothing), `none` (no run was created for this head) and `unknown` (the state could not be read). A push restarts CI, so an earlier green is stale; and the last three are silence, which is never green. A `blocked` HOLD names the newest run on the branch that actually executed. |
| `review` | The board's newest round for this PR read another commit, did not `stop`, has `confirmed > 0`, or has a failing Sonar gate. No round at all is a HOLD too, and so is a round that recorded no finding count — unknown is not zero. |
| `merge_claim` | Another agent holds `kind=merge` on `<repo>:<base>` — it is landing onto this PR's base right now. Keyed on the base rather than the head since [#318](https://github.com/prisonblues/quarterback/issues/318): two agents landing two *different* PRs into `main` is the collision worth serialising, and under head keys they never see each other. |
| `queue` | This PR is not at the head of the merge queue for its base, or is not in the line at all while others are ([#227](https://github.com/prisonblues/quarterback/issues/227)). The reason names the position and who holds the place ahead. An empty line passes silently; a board with no `/merge-queue` reports `skipped-absent`. |
| `migrations` | `scripts/migration_reconcile.py` says `stop`, or its plan and its exit code disagree. `relink`/`renumber`/`merge` are RECONCILE. |
| `sw_version` | `scripts/check_sw_version.py` fails in a way `--fix` cannot repair. A repairable one is RECONCILE. |

The last two also HOLD when `origin/<base>` could not be refreshed — they are answers about the gap between this branch and the base, and a base last fetched yesterday produces a confident NOOP about a head that moved this morning. `--no-fetch` makes that the caller's choice instead, noted once on the run.

The `review` clauses are the round's **own statements** — `head_sha`, `stopped`,
`confirmed`, `sonar_gate` — read back rather than re-derived. #62 spent three rounds
discovering that merge gates trust proxies (the exit code, then the push, then the
existence of a payload file), and this must not become the fourth.

`stop_confident: false` is a **warning, not a hold** by default, deliberately: two
permanently-absent reviewer seats on a headless box would otherwise make a green verdict
unreachable, which is the noise-for-signal trade this file already argues against for
`coverage_veto`. The vetoes are printed with it.

`--require-earned-stop` makes it a HOLD, and is for the caller that **ran the round itself**
and is about to offer to land on the strength of it — `/panel-review-pr` §7. There an
unearned stop is not background noise about somebody else's box: it is this cycle reporting
that nobody read the whole diff. Which of the two a run is doing is the caller's fact rather
than something this script can see, so it is a flag rather than a rule, and `checks.review.detail.require_earned_stop`
records which reading produced the verdict. Either way the vetoes are reported: the flag
changes what the verdict IS, never what the reader is told. A `stop_confident` the board
recorded as **null** is not an unearned stop under either reading — null is a question
nobody answered, and the caller that ran the round has its own payload to answer it from.

**Capability detection, and its two exceptions.** Repo-local guardrails are detected — a
repo without `scripts/migration_reconcile.py` records `skipped-absent` and moves on, which
is what lets one gate serve quarterback, lexray and an unenrolled repo with no per-repo
branch in the skill.

The first exception is a hole in that mechanism rather than a policy: detection reads the
**branch's** tree, so a diff that *deletes* a guardrail would hand itself `skipped-absent`,
switching off the check by the very change the check exists to read. So an absence only
counts as a skip when `origin/<base>` does not have the script either; a branch that removed
one HOLDs.

The second is the board. An unset `QUARTERBACK_BASE_URL` does not mean this repo has no
review invariant, it means the invariant exists and cannot be seen, so `review` HOLDs. That
knowingly narrows the "local path stays first-class" promise for `/fix-and-land` (not for
`/panel`, which is unaffected), and the off-switch is one line:

```json
"preland": { "disabled_checks": ["review"] }
```

A name in that list that no check answers to is a **hard exit**, unlike every other
unknown key in a rules file, which is warned about and dropped. The asymmetry is the point:
a misspelled key elsewhere leaves a setting at its default and the default is the safe end,
where a misspelled name here would leave a merge gate's check running while reading as
deliberately off.

**A check that did not run is still reported** — `skipped-absent`, `skipped-disabled`, or
`skipped-flag` for `--skip` — because a payload must never read clean by omission.

**It is read-only and takes no claim, and it does not enqueue either.** It reports commands
and never runs them; it reads `kind=merge` claims and the merge queue and writes to neither.
Joining the line is the landing loop's job (`/fix-and-land` §4), and it has to be: a verdict
that enqueued could not be run twice, could not run as a CI check, and would put a PR in a
line on behalf of whoever was merely asking a question about it. That belongs to whatever does the merging
([#100](https://github.com/prisonblues/quarterback/issues/100)) — a verdict that mutates
cannot run as a CI check, cannot be re-run to verify itself, and cannot be asked twice by a
loop that wants to know whether its own fix worked. The single write is `git fetch` of the
base branch's remote-tracking ref (`--no-fetch` suppresses it), because a migration verdict
computed against a stale `origin/main` is confidently wrong in the direction that lands.

**Its limit.** It is advisory: a script an agent chooses to run cannot stop a human merging
in the UI or a loop that skips the step. What would actually block a merge is a required
status check on a protected branch. A CI job doing that must pass `--skip ci` — such a job
is itself one of the checks `ci` reads, and would otherwise gate on its own pending status.

## Deployment

- **The flake's home-manager module** (`harness/hm-module.nix`) — ships this directory to
  `~/.claude/loops` and the wrapping skills to `~/.claude/commands`. It deliberately does
  NOT pull in `python3`/`jq`/`gh`/`codex`: those are the host's tools, and pinning them
  here would point the loops at a different toolchain than the machine they run on.
  See [../README.md](../README.md#requirements).
- **`run-loop.sh`** — the timer entrypoint. `flock` single-flight; **discovers** repos
  (any checkout under `$HARNESS_REPO_ROOT`, default `~/source`, shipping a rules file)
  rather than reading a list; sets `HARNESS_UNATTENDED=1`; appends to a daily log in
  `$LOOPS_LOG_DIR` (default `~/loops-logs`). **REPORT-ONLY by default** — set
  `LOOPS_EXECUTE=1` only after a supervised hand-run. Adding a repo to the sweep is a
  commit in *that repo*, not an edit here.
- **`systemd/loops-lander.{service,timer}`** — reference spec only. The live units are
  `systemd.user.{services,timers}.coding-loops` in `home/rich-workstation.nix`
  (ExecStart → `%h/.claude/loops/run-loop.sh`, `OnUnitActiveSec=10min`,
  `Persistent=false`). Activate with a `rebuild`.
- **`systemd/qb-reconcile.{service,timer}`** — reference spec too, and the odd one out in
  this directory: the program it runs is `qb-reconcile` from `bin/`, not a loop. It is here
  because this is where the harness's reference units live and one place for them beats two.
  Report-only with **no `--execute` to graduate to** — the pass never edits the plan — so the
  only cutover decision is whether to keep `--post`, which posts only when the report's
  content has changed since the last post. Like the lander's, the service carries no
  `[Install]`: the timer is what starts it, and enabling the oneshot would also run it at
  every login. `OnUnitActiveSec=15min`,
  `SuccessExitStatus=0 1` so a partial run (rate-limited `gh`, a board mid-deploy) stays out
  of the failed state while a genuine "could not run" still goes red. See
  [../README.md](../README.md#qb-reconcile--does-the-plan-still-describe-the-present).

**Cutover to acting:** keep the timer report-only, watch a few daily logs, hand-run one
`lander.py --execute` on a single PR, *then* set `LOOPS_EXECUTE=1` in the service env.

## Gate model (recap from #117)

- **Hard gates** (block merge): CI (`gh pr checks`) + the SonarQube quality gate.
  A `review_panel.local_suite` run (#548) is **not** one of them: it is evidence for
  the seats and for `round_stop`, and `preland.check_ci` reads GitHub, so nothing a
  suite does on this box moves the merge button.
- **Soft gates** (advisory): Claude + Codex reviewers. Never a lone-LLM hard block.
- Auto-merge only within the `auto_merge` policy; everything else → human at the
  merge button.
- **`preland.py` is where those gates are read**, so that a loop about to merge consults
  one verdict rather than each skill's own paragraph about them. It does not add a gate;
  it makes the existing ones executable, and its `READY` is a claim about what was
  checked, not about whether the change is a good idea.
