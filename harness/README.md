# The quarterback harness

**Step 2 of the install.** The service in this repo is the board; this directory is the
workflow it coordinates. They ship together because the board on its own is a coordination
layer with nothing to coordinate — `/panel`'s reviewer leaderboard, for one, renders an
empty table until `loops/panel.py` starts recording runs against it.

Short version: **worktrees isolate agents from each other; the loops put them to work; the
board reconnects them.**

- `loops/` — the engine: the reviewer panel (`panel.py`), the epic driver (`epic.py`), the
  Dependabot lander (`lander.py`), and the per-repo config layer (`harness_rules.py`)
- `commands/` — Claude Code slash commands (`/panel`, `/panel-review-pr`, `/review-pr`,
  `/fix-issue`, `/fix-and-review`, `/fix-and-land`, `/epic`, `/lander`, `/wt`,
  `/drop-worktree`, `/tree-shake`, …)
- `bin/` — the bash the worktree commands drive (`create-worktree`, `remove-worktree`,
  `prune-worktrees`, `worktree-holder`), plus `qb-stage`, which records the workflow
  stage a session is in for the statusline, `qb-seat`, which turns one pane of a
  multiplexer into a fleet seat with its own board identity, `qb-board`, which
  launches the terminal board client (`qb-board --follow` tails the board to stdout
  on any host with ssh; see the repo README), `qb-reconcile`, the read-only pass
  that asks whether the board's plan still describes the present, `qb-pace`, which
  says how the shared subscription's five-hour and weekly windows stand and what a
  job of N seats would cost against them, `qb-doctor`, the one command that answers
  whether this host is wired up — comparing the board, the harness and the client
  against each other, and checking the harness's own guards are *installed* rather
  than merely present in git — **and the board
  client proper**: `qb-hook` (the lifecycle reflexes Claude Code fires), `qb-mcp`
  (the per-session stdio MCP shim), `qb-claude-setup` (the wiring), `qb` (the human
  CLI), and `qb-env`, the site-config library the four of them source
- `claude/` — the Claude Code configuration the harness owns: the hook `settings-fragment.json`
  and `quarterback-workflow.md`. See [claude/README.md](claude/README.md)
- `worktree.example.json` — per-repo config, annotated with quarterback's own values

Neither half needs the other. The loops run with no board configured (recording is
best-effort and no-ops), and the worktree scripts are plain bash usable from any shell.

---

## The problem this solves

Run one coding agent on a repo and nothing is required beyond a checkout. Run a second
one at the same time and everything collides at once: both edit the same files, both run
migrations against the same database, both bind port 8000, both leave the tree dirty for
the other. The usual answer — "just wait your turn" — throws away the reason you wanted
two agents.

A git worktree fixes the files. It does not fix the database, the containers, the ports,
or the local scaffolding (`.venv`, `.claude`, `CLAUDE.md`) that a checkout needs to be
usable. `create-worktree` does the rest, so an agent gets a genuinely independent place to
work in one command.

## What each piece does

### `/panel` and `/panel-review-pr` — the reviewer panel

`loops/panel.py` reviews one PR diff with several vendor CLIs at once (Claude, Codex,
and others per config), deduplicates their findings, and has a master judge rule each one
real or not. SonarCloud can be wired in as a hard gate alongside them. `/panel` reviews and
comments; `/panel-review-pr` takes the confirmed findings, has a sub-agent fix every one of
them, and then **panels the fix commit** — one round leaves the fixer's own work read by
nobody, and a structural fix creates interactions no earlier round could have seen.

**A pinned model that this host's provider cannot serve no longer costs the seat.** Model
slugs are pinned in `.harness-rules.sample` so that "codex found 9 issues" still means something
six weeks later — but a pin is one value for the whole fleet and a *deployment* is per-host,
so a slug that is right everywhere else can be unservable on one box. On daedalus, codex
routes through an employer Azure gateway deploying `gpt-5.5` while the rules pin
`gpt-5.6-luna`: the seat 404s ten times and the panel loses a whole vendor, which on PR #207
left 25 findings all attributed to `claude` — reviewing a PR `claude` had written. There are
**two** such pins and this
gateway refuses both independently — `gpt-5.6-luna+max` 404s, `gpt-5.5+max` is an
`unsupported_value` on `reasoning.effort`, `gpt-5.5+high` works — so dropping only the model
loses the seat on the next knob, which is how PR #217 got a round where *no* reviewer ran at
all. Each pin is now lowered on its own, only when the error names it, at most once each, and
the report says what happened: `codex (CLI default; pinned gpt-5.6-luna unavailable, effort max
unsupported)`. The substitution is recorded as
state (`model_unavailable` / `effort_unsupported`) in the payload as well as the header,
because the board is where
"is the expensive tier worth it" gets answered from accumulated runs, and a run whose model
was swapped must not be averaged in as the pinned one. Deliberately narrow: only for a pin
that was set,
only for those two causes, at most once each — a general "retry with fewer constraints" would
quietly review on a weaker seat for reasons nobody chose. **codex only**, because lowering a pin
means rebuilding the argv without it and only its argv can express "use your default":
`claude` takes `--model` unconditionally and `agy` builds its argv before any failure exists.

Two things about that failure were wrong before it could be fixed, and both were about
reading the wrong stream. codex under `--json` puts its event stream on **stdout** —
including `{"type":"error","message":"... 404 ... deployment ... does not exist"}` — while
stderr holds one line, `Reading prompt from stdin...`, printed before the request was made.
The panel diagnosed from stderr alone, so it reported `exited 1 (Reading prompt from
stdin...)` for a config mismatch and sent two people to debug stdin plumbing. Worse, the
same stderr-only view fed the retry decision: `is_rejection` keys on 4xx invalid-request
markers and an explicit `"status":400`, so a gateway **404** read as a flake worth another
go — and each attempt spent the seat's full budget, ten minutes at a time, to reach the
identical answer. Both now read stdout's error envelopes too (`error_events`).

Each reviewer also declares what it could *not* assess, and the panel records which of them
saw only a prefix of the diff. A finding count reports "clean" and "I could not tell" as the
same zero; those two columns are what tell them apart, on the PR comment and on the board.

Before any of that, the panel **rules on whether the round is worth running**. It used to
dispatch every configured seat at full effort whatever the diff, and on PR #137 that meant
four seats against 763,375 chars — 6.4× the argv ceiling of the one seat whose prompt travels
in argv — on a change that was a *pure move*, `panel.py` split into six modules with nothing
retyped. Every relocated line appears twice in a diff, so the bulk of that was code nobody
changed, and a finding about it is a finding about the base branch. The token cost was the
second problem; the first is that a truncated read which produces findings is worse than no
review, because the next step briefs a fixer to resolve every one of them.

So the panel now measures the diff's **shape** as well as its size — a move is mechanically
identifiable, because its added lines are a near-permutation of its deleted ones — and does
one of three things. A diff that fits runs as it always did. A move-shaped diff that does not
fit is reviewed as a **manifest**: what moved where, what did *not* survive, what changed
besides moving, and which definitions the change *adds* in more than one place. A diff far over
every seat's ceiling with no smaller honest question to ask is **refused**, loudly — printed,
recorded on the board, and posted to the PR under `--post`, because "no review" must never read
as "clean". A refused round still reads the CI gate, which is size-independent and costs one API
call, and says that the Sonar gate was not evaluated rather than leaving its default to read as a
pass. `--force` overrides it on the record. None of it fires on a repo that declared no
ceiling: this decides *whether to start*, never *what to send*, and the deliberate absence of
a default diff budget stands. `loops/README.md` has the whole rule.

This is the piece with the tightest board coupling, and the reason the two halves ship
together. A panel run is a controlled comparison — one diff, several models, one judge —
and it used to evaporate when the process exited. Each run now records itself
(`qb record-review`), so `GET /review/stats` and the board's `/panel` page can answer
"which reviewer actually finds the real issues, and is the expensive tier worth it" from
accumulated evidence instead of impression.

The recording is **best-effort by construction**: a board that is down, or absent entirely,
prints one line and changes nothing about the review. Telemetry that can fail a review which
already succeeded is worse than no telemetry.

What that leaves out is what *happened* to each finding, and the fix commands close it
(`review-pr.md` §2b, `panel-review-pr.md` §4b). The judge rules at review time with no more
access to the answer than the reviewer it rules on, so a confident wrong finding scores exactly
like a real one — on PR #64, three of six confirmed P2s were wrong and are still in the board as
confirmed. After the fixer pushes, each finding gets a terminal outcome (`qb record-outcome`):
`fixed`, `refuted` **with the reasoning**, `deferred` with where it went, or `superseded`. The
refutation is the one that pays, and it is already being written into the PR comment — this is
where it stops being prose nothing can count. Do not mark your own findings refuted unattended:
the board records who set it (from your token) and who you SAY signed it off — `attested_by` is a
claim you are making, not a signature the board checked — and `/panel` shows the split.

**How hard the panel looks, and how long it keeps looking, are now repo settings.** They
were constants, and the measurement says those constants do not converge: across the seven
PRs panelled on 2026-08-16, the last round of each raised 201 findings no earlier round had
and **128 of them — 63.7% — were created by the fix pass immediately before it**, against a
~7% industry baseline for bad-fix injection. Every one of those panels ended on the round
cap, each saying so in its own output — *"a stop, not convergence"* — and nine of this
repo's open issues are the panel's own deferred-finding overflow. The severity split, P1
4.1% / P2 28.6% / P3 36.1% / P4 31.3%, says the signal is calibrated at about 1.2 P1s per
PR and the 67.3% tail beside it is not.

So `.harness-rules.sample` now carries eight `review_panel` dials (#165, #297), and what they
bound is the tail rather than the signal: `fix_severity_floor` (**P3**) is what a fix round
is asked to clear, and below it a finding is reported, marked and recorded rather than
fixed — P4 is 31.3% of findings and the tier that actually ballooned #236;
`round_trigger_floor` (**P2**) is what a NEW finding needs to buy another round, which is
the rule that mattered most, because from round 2 the thing under review IS the previous
round's fix and a termination test fed by its own output can only end on the cap — the
two floors differ on purpose, since fixing a P3 in a pass that is already open costs one
edit while letting a P3 buy another round costs a whole panel plus another fix pass;
`low_severity_fix_lines` (**40**) is the churned lines the whole round may spend on the band
between the two floors, counted rather than estimated and spent cheapest-first — added after a
second measurement on 2026-08-21, where PR #188's 185-line feature came out of two fix passes
at 721 lines, 74% of the PR being review-response code, off a round-2 fix list that was 89%
below P2; a budget rather than a per-fix cap because #188's round 1 was 408 lines of
individually reasonable small fixes;
`max_fix_growth` (**3.0**) stops a cycle whose fix pass has multiplied the change instead of
fixing it; `reviewer_scope` (**diff**) asks reviewers for defects in the change rather than
in everything it touches; `fixer_may_defer` (**true**) gives the fixer the third exit it did
not have; `max_rounds` (**2**) surfaces the existing cap; and `require_failing_test`
(**false**) reserves the name for #165's evidence contract and reports that it is not built,
because the reviewer-emitted failing test it needs does not exist yet (#92, #114).

None of that lowers the bar for what a fix round does take on — in scope, everything still
gets fixed properly, with a test, and note-and-move-on is still forbidden — and every value
is validated: a malformed value of one of these keys is a hard exit naming the key, the
value and what is accepted, because a repo that typed `p-4` meaning "fix everything" must
not silently get the default instead. An unknown key is the other case and keeps its old
answer — warned about and dropped, so a rules file shared across a fleet that upgrades at
different times is not a version pin. What the round actually applied is in the
artifact, on a **Panel dials** line and in the payload's `review_panel`, because the
orchestrator that briefs the fixer builds that brief out of the report. #165 proposes about
fifteen dials; these are the seven whose enforcement point already exists, plus #297's budget,
and the rest stay in the issue.

The fixer has one more permitted outcome than "fixed" and "false positive", and it exists
because of what the other two cost. A fix that patches a wrong assumption produces the next
round's findings; a fix that removes the assumption does not — PR #61 spent two rounds and two
fixes on one unexamined premise, and PR #88 had a fixer circle its own previous fix inside a
single commit. So `review-pr.md`'s brief (step 3a) lets a fixer report that a finding says the
**approach** is wrong rather than the code, and write no patch for it: stated, with the premise
in one sentence and what removing it would cost, rather than answered with a special case. It is
narrow on purpose — three conditions that must all hold — and it never authorises a redesign,
because the output is "stop and ask" and the evidence behind it is still two PRs (#67). The
premise can be put to the seats first with `panel.py --ask`, which is exactly the shape of
question that path exists for. An escalated finding is recorded as `deferred` by the
orchestrator, which relays it, opens an issue that **asks** the premise, and names that issue in
`deferred_to`. Under `review_panel.fixer_may_defer` a fixer may now also return `deferred`
itself — "the defect is real, and it is not what this change is for" — which is a different
judgement from an escalation ("the defect is real and the FIX is in dispute") arriving at the
same row; the fixer owes two justifying lines and the orchestrator still owns the filing.
`harness/tests/test_fixer_escalation.py` guards the wiring rather than the
judgement: that the permission and its report ship together, that the cross-file references to
step 3a resolve, and that `deferred` is a value the database accepts.

The loop knows about it, which took a second change (#221). An escalated finding is outstanding
and no fixer may touch it, so under the original stopping rule it earned another round every time
until the cap ran out — the mechanism built to stop a cycle circling a premise guaranteed it ran
to the cap. `panel.py --escalated <key>` subtracts the key from the work a fix round can clear,
so the cycle goes again for everything else and stops as soon as only escalations remain. The
rule and the exact scope of what it guarantees are kept in `round_stop`'s docstring
(`harness/loops/panel_rounds.py`), and what a caller must do about it in
`harness/commands/panel-review-pr.md`; they are not restated here.

What is NOT here is measuring recurrence — asking mechanically whether a round is circling the
last round's fix — which is #67's other half and needs the provenance work in #48. Nor is
premise identity: the register holds a finding KEY, and a fresh panel that re-words the same
premise mints a new one, so the caller re-escalates it. Until those exist, an escalation is a
caller's declaration read out of a fixer's own report: the loop takes it on trust, records the
round it was first made in so it can be audited afterwards, and keeps the cap as the backstop.

The same rule shapes how a run's **cost** is measured. Each member is timed, and each one that
can be is also asked what it spent in tokens — but never by switching its CLI to a JSON output
mode. Those modes all move the reply inside an envelope (`.result`, `.response`,
`item.completed`, `.message.content[]`), so adopting them would mean four bespoke unwrappers on
the one path that currently works. Instead every reviewer keeps its plain-text reply, its
session id is pinned **before** it runs, and usage is read back out of the session afterwards:
a transcript that cannot be read costs a number, where a broken unwrapper would cost the
findings on every run. The id is fixed up front rather than matched afterwards because
`/panel-review-pr` fans out up to 4 concurrent panels, each running its own copy of each
reviewer — picking a session by mtime would hand one panel another's numbers.

Claude pins `--session-id`; pi pins `--session-id` into a per-run `--session-dir` that is
deleted when the member returns, so a review still never lands in the user's session store;
codex has no session id to pin for a new run, so it uses `--json` for the usage events and
`--output-last-message` to hand the findings over as plain text in a file. Antigravity is left
uninstrumented rather than half-converted. A cost in dollars is recorded **only where the vendor
states one** (pi does) and never derived from a price table, and anything unread stays null —
which the board renders as "not recorded", never as a reviewer that cost nothing.

### Red/green — a regression test that never failed proves nothing

Every fix command in here tells the fixer to write a regression test. None of them used to
ask whether that test would have **caught the defect it was written for**, and a test that
would not is worse than no test: it is a passing assertion that the bug is gone, and it will
keep passing after the bug comes back.

PR #90 is the demonstration. Round 1 found that `load_baseline`'s anchor selection was
order-dependent — the same two baselines gave the sha or `None` depending on `--baseline`
argument order. A test for exactly that behaviour already existed, with a docstring
explaining the intent, and it passed: its fixture happened to list the two baselines in the
working order. The panel had to find the defect a round later, in code that was already
"covered". The assertion was right; nobody had ever run it against the broken code, because
the test was written alongside the fix and the broken code no longer existed by then.

So `review-pr.md`'s brief (inherited by `/panel-review-pr`), `fix-issue.md` and
`fix-issue-here.md` now all say the same thing: before committing, capture the **fix** as a
patch and remove it — not the test — run each new regression test, and confirm it fails **on
the assertion that names the defect**. An import error or a missing fixture demonstrates nothing. Then restore and
confirm green. Red, then green, in the order that means something. The fixer reports the
count, so a summary that skipped the step reads as skipped rather than as passed.

The mechanism is a **patch file, not `git stash`**, and that is a fleet property rather
than a preference. `refs/stash` lives in the common git dir, not the per-worktree one, so
every worktree of a repo shares one stash stack: a stash pushed in one is listed and
poppable from all the others, and `stash@{0}` resolves to whatever the last pusher meant.
This harness runs many concurrent worktrees off one `.git` by design. The PR that added
this instruction proved the hazard by losing its own working tree to it — a concurrent
agent in a sibling worktree popped the red/green stash into its own checkout and pushed it
back. Two earlier drafts tried to make stash safe (a label check, then an entry count);
the count caught the loss, but nothing local can stop another worktree popping the entry.
So: `git add -N` the fix's paths, `git diff HEAD` them to a patch, check `test -s`, remove
them, run red, `git apply` the patch back. The harness now has a per-worktree stash of its
own as well — `qb-stash`, below — and `create-worktree` installs a hook that refuses the
shared one outright; the patch file stays the right tool *here* because it is the one that
takes a pathspec.

Three details in that sequence exist because the obvious spelling is wrong. **`test -s` is
the check that matters**: an empty capture — mistyped paths, or a fix already committed —
leaves the red run executing with the fix still in place, coming out green, reading exactly
like the step passing. **`git add -N`** is what puts a file the fix *added* into the patch,
since `git diff` ignores untracked files and a half-captured fix means the red run imports
the new half; those same files come back out with `rm`, because `git checkout HEAD --`
cannot restore a path absent from HEAD. **And the new test file stays put** — remove it
along with the fix and the red run collects nothing, which exits non-zero without any
assertion having failed.

**The exemption is stated, deliberately — and it is narrow.** A regression test for a path
the fix *created* has no pre-fix behaviour to fail against; those report `red/green: N-A (new
code path)`. An instruction with no exemption for the legitimate case gets worked around
rather than followed, and a worked-around instruction is worse than an honest `N-A` — it
removes the signal that says which tests were actually proved.

What is **not** exempt is a prompt string, a config default or a doc that already existed.
Shipped text is an artefact a test can assert on, and the PR that added this instruction
proved it in the act of being written: it changed `REVIEW_PROMPT` and three markdown briefs,
and thirteen of `test_regression_test_redgreen.py`'s fifteen tests went red against the
previous text. The first draft of the instruction *did* exempt that case — Codex flagged it in review —
and an exemption that wide would have excused most of this harness from its own check, which
is the failure mode #114 predicted.

The panel carries the cheaper half of the same lever. `REVIEW_PROMPT` used to ask only about
test **absence** ("new code paths … that lack a test"), which is a question #90's fixture
answered correctly. It now also asks about tests that are present and not load-bearing — a
fixture whose ordering or inputs happen to avoid the bug, an assertion that cannot fail, a
mock that satisfies itself. That is a reviewer reading the tests as tests, and it is far
cheaper than mutation testing on a diff for most of the same catch.

Why this matters more here than in most repos: the standing rule at the round cap is *"fix
P1/P2 correctness only, defer the rest"*, explicitly because **the last fix pass is never
itself reviewed**. Its regression tests are the only thing standing behind it. A fix pass
whose tests pass vacuously has no backstop at all — and that is precisely the pass this
repo has decided not to review.

### `/fix-and-review` and `/fix-and-land` — an issue, end to end

Both take an issue number and come back with a reviewed PR. They differ in exactly one place, and
it is the last step: **`/fix-and-review` stops at merge-ready and hands you the merge**, while
`/fix-and-land` goes on to merge when `preland.py` says READY and the agent states genuine
confidence. Wanting `/fix-and-review` to merge is the signal to have run `/fix-and-land`, not a
flag to add.

`/fix-and-review` also spends the review differently: `/panel-review-pr` runs in a **sub-agent that
did not write the code**. The conversation that implemented the change holds the author's model of
it — every reason the code is the way it is, and none of a reader's surprise — which is the context
a review needs not to have, and `/review-pr` says in its own description that fresh eyes are what
it is for. It is #40's constraint one level up: an agent reviewing its own work is grading itself,
and the board cannot tell a fixer from a reviewer.

There is no release-number step in its prep, and there is none anywhere on a branch (#122). A
branch writes `changelog.d/<issue>.<kind>.md` and names no version; the number is applied on the
base after the merge, by `scripts/release.py run`, once per batch. What prep does ask is the
cheaper question — `changelog_fragments.py required`, does this branch carry the note it owes —
because finding that out here costs a commit and finding it out at the merge costs a cycle.

### `/epic` and `/lander` — the long-running loops

`epic.py` drives a multi-issue epic: it fans sub-issues out into their own worktrees, stacks
their PRs onto an integration branch, and keeps going. `lander.py` is the Dependabot lander —
it batches dependency PRs, verifies them, and lands the ones that pass. Both are usable from
the slash commands or on a timer (`loops/systemd/`).

Because `~/.claude/loops` is a read-only store symlink when installed via nix, these write
their run state to `~/.local/state/loops` rather than beside themselves.

### `preland.py` — the pre-land verdict

`loops/preland.py --pr <n>` answers one question mechanically: **may this PR be merged,
and if not, what is outstanding.** `READY` (exit 0), `RECONCILE` (exit 3, with the exact
commands and the files they touch), or `HOLD` (exit 2, with what is unresolved and who has
to resolve it). `--json` for a loop, plain text for a person.

It is not a new gate. It is the gates the harness already had — CI green *now*, the panel's
newest round read *this* commit and stopped with nothing confirmed, one migration head,
no failing Sonar gate, nobody else landing the same branch — read in one place instead of
described in two. `/fix-and-land` used to hold about fifty lines of prose about them and
`/panel-review-pr` held none, which is how they came to disagree; both now call this and
act on the verdict.

Guardrails are capability-detected, so a repo without `scripts/migration_reconcile.py`
skips that check and says it skipped it. The board is the one exception — an unreadable
review state is a HOLD, not a skip, because "nobody reviewed it" and "nobody could tell"
are the same thing to a merge. `.harness-rules.sample` is where a repo turns that off
deliberately.

It reads; it does not act. It reports commands rather than running them and reads merge
claims rather than taking them, which is what lets it be re-run to check its own advice —
and what would let a CI job call it. See [loops/README.md](loops/README.md) for the check
list and the exit-code contract.

### `/fix-issue <number>` — the driver

End-to-end resolution of a GitHub issue in a dedicated worktree with its own database copy.
Reads the issue, plans, provisions a worktree, implements, writes tests, updates docs, runs
the project's real CI checks, self-reviews the diff (optionally with `codex` as a second
opinion), commits, pushes, opens a PR, and comments on the issue.

Two things in it are worth lifting out, because they are the ones that bite:

- **There is no DB mode to choose** (#340). It used to ask whether the change touched the
  database and pass `--shared-db` when it did not. That question does not decide the
  outcome: what makes the shared database unsafe is not whether the *change* writes to it
  but whether anything the run *executes* truncates it — and step 7 runs the full suite
  every time. The classification was answered correctly and still produced a worktree the
  suite's own guard refused to run in. So the copy is unconditional, and step 3 ends in an
  **isolation check on the resolved `.env`** that every route passes through — created,
  reused or inherited from the epic driver. See `check-db-isolation` below.
- **The worktree is left in place** when the PR opens, so review findings can be addressed
  on the same branch and the same database. Teardown is a separate, deliberate act.

### `/drop-worktree` — teardown, keep the branch

Destroys the worktree this session owns and all its trappings — containers, nginx block,
isolated DB, port entry, directory — but keeps the branch and its commits. Refuses to run
on a dirty tree, and warns if the branch has never been pushed.

### `/tree-shake` — sweep the debris

Worktrees removed badly leave orphans: databases with no worktree, stale port entries,
leftover directories, containers, nginx blocks. `/tree-shake` first offers to tear down
*finished* worktrees properly (merged PRs), then dry-runs an orphan sweep and applies only
what you confirm.

### The worktree scripts

`create-worktree` is the substantial one. It provisions the directory, symlinks shared
local state, copies configured data, clones the database, wires Docker and nginx, and
allocates a port. `remove-worktree` reverses it. `prune-worktrees` is the orphan sweeper —
it is the only one that is dry-run by default.

The commands are thin, guarded drivers over these. The scripts hold the deterministic
logic on purpose: a model deciding *which* worktree to destroy is fine, a model
hand-rolling `docker rm` / `dropdb` / `rm -rf` is not.

### `check-db-isolation` — which database is this checkout actually pointed at?

```
check-db-isolation [CHECKOUT]     # default: cwd
#   exit 0  safe    — the main checkout, a worktree with its own database, or no database at all
#   exit 1  REFUSE  — this worktree's .env names a database another checkout is using
#   exit 2  used wrongly
```

Three routes end with a worktree whose `.env` names the **main** database, and the reason
this is a script rather than a paragraph in a brief is that only the first of them announces
itself:

| Route | What happened |
| --- | --- |
| `--shared-db` | Someone chose it, and then ran a suite whose teardown truncates |
| **A reused worktree** | `create-worktree` refuses an existing directory, so nothing re-provisioned it and nothing re-checked it. A worktree predating per-worktree databases still names the main one — this is how `feat/issue-85` got there with nobody choosing anything (#340) |
| No database container | The copy was skipped with a yellow note and the `.env` left pointing at the main database |

`/fix-issue` used to check isolation by reading `create-worktree`'s residual-`.env` warning
out of its output, which meant the check ran only when `create-worktree` ran — never on the
route where nothing had provisioned a database at all. It now runs this, on `$WT_DIR`,
whatever produced it.

It **imports** `templates/dbtarget.py` rather than re-implementing the comparison, so it and
the pytest guard that refuses at collection time cannot disagree about what "the same
database" means: host aliases collapse, an omitted port is filled in, and anything
unparseable is read as a collision. What it adds over that guard is *when* — before the work
rather than at the start of the run that was going to destroy something.

**`create-worktree` also turns on `git rerere` for the repo, once, if nobody has set
it either way.** This is the setting whose value scales with the number of worktrees:
one merge into the default branch produces the *same* conflict in every open branch,
and worktrees share the repo's common git dir — so `rr-cache` is shared with no further
configuration, and a conflict resolved by hand in one tree is replayed in the rest.

The part to know before it surprises you: **a replayed resolution is git's answer from
last time, not a judgement about this merge.** rerere matches on the conflict text, so
the same hunks get the same answer even when the right answer has changed. The example
this used to give — a CHANGELOG version narrative, whose correct resolution depends on
which releases happen to be in flight — has stopped being one, and it is worth saying why
rather than quietly swapping it: quarterback's branches no longer write in `CHANGELOG.md` at
all (#122), so there is no CHANGELOG conflict left to replay an answer to. A branch writes one
fragment, in a path no other branch opens, and `release.py guard` refuses it if it reaches for
the file anyway. The README narrative that had to be *rewritten* by hand is deleted, and the
release list is written on the base by the release job. That removes the case; it does not
remove the class. A
replayed resolution is still last time's answer, and the merge that made this rule worth
having had three prose conflicts where keep-both was right and a fourth, in `panel.py`,
where it was not. `rerere.autoUpdate` is therefore pinned to
`false`: the merge still stops, the file is left **unstaged** with the previous answer
in it, and you have to look at it and `git add` it yourself. Read a replayed resolution;
do not trust it. Turn the whole thing off for a repo with `git config rerere.enabled
false` — the script only ever sets it when it is unset, so that decision sticks.

**Written rather than left absent, because absent is not off.** A user with
`rerere.autoUpdate=true` in their global config got exactly the silent staging the
paragraph above says cannot happen, and nothing looked. The pair is written together and
only for a repo that had decided neither, so a repo that made its own choice keeps it.

**And that guarantee covers a human at a terminal, not an unattended loop.** It rests on
nothing staging the file for you — but `epic.py` and `lander.py` both run a blanket
`git add -A` in their worktrees after making changes, which stages a replayed resolution
whether or not `autoUpdate` is off. So on the loop-driven path, an answer given once by
hand in one branch can be committed unread in another. That is not closed here: it wants
either explicit staging in those two loops or rerere scoped away from loop-driven
worktrees, and it is filed rather than guessed at.

### `git stash` is unsafe here, and `create-worktree` now says so out loud

`refs/stash` lives in the **common** git dir, not the per-worktree one. Every worktree of
a repo therefore shares one stash stack: `git stash push` in `quarterback-fix-issue-114`
is listed by `git stash list` in `quarterback-fix-issue-113`, and `stash@{0}` there
resolves to whatever the last pusher meant. This harness runs many concurrent worktrees
off one `.git` **by design** — that is what `create-worktree` is for — so the shared stack
is not a corner case here, it is the normal configuration.

It has already taken two working trees. Once an agent's red/green stash was popped into a
sibling and pushed back by hand; the second time the recovery note was parked in the same
shared stash that had eaten it, where the next racing push could take it out the same way.
Both were noticed by luck. Nothing in git warns either party, and a stash entry carries no
author, no worktree and no session, so there is nothing to warn *with*: the owner of the
second one was identified from a board claim, not from git.

So `create-worktree` installs a **`reference-transaction` hook** that refuses to put
anything on `refs/stash` while the repo has linked worktrees:

```
REFUSED: refs/stash is shared across every worktree of this repo.
  ...
  Use 'qb-stash' instead — push/pop/list/apply/drop with the same shape, stored
  per-worktree under refs/worktree/, invisible to every sibling.
```

Four things about that are worth knowing before it surprises you.

**It catches a hand-typed `git stash`, which is the only reason it is a hook.** `stash` is
a C built-in, so `alias.stash` is ignored and a `git-stash` on `PATH` is never consulted,
and there is no `pre-stash` hook. A wrapper script would have covered the harness's own
commands and nobody else, and a human clearing a dirty tree before a pull is exactly the
case that produced the near-miss.

**It guards the main checkout too**, not just the linked worktrees, because that near-miss
was an orchestrator running `git stash push -u` in `main` while sub-agents worked in
siblings. A repo with no linked worktrees stashes exactly as it always did — the hazard is
the shared stack, and a single checkout does not have one.

**It stops the push, not the pop, and that is a real limit rather than an oversight.**
Measured on git 2.54.0: `git stash pop` removes its entry through the **reflog**, which
raises no ref transaction at all while another entry remains underneath, so no hook can
see a pop. The protection works by keeping the shared stack empty — with nothing on it,
there is nothing for a sibling to take. Deletions of `refs/stash` are deliberately let
through so entries that predate the guard stay droppable.

**`QB_ALLOW_SHARED_STASH=1` is the escape hatch**, for somebody doing this on purpose. One
env var, per command, so the guard does not become the thing people turn off wholesale.

`core.hooksPath` **replaces** the hooks directory rather than stacking with it, and on this
fleet its global value is a read-only nix store path whose one entry is a gitleaks
`pre-commit`. `qb-hooks install` therefore re-exports every hook that dir provides as a
symlink to a forwarder that resolves the delegate **at run time** — the store path changes
on every home-manager rebuild, so an install-time snapshot would rot. Without that, turning
on a stash guard would turn off secret scanning, which is the class of failure this repo is
organised against; `harness/tests/test_stash_guard.py` pins it.

Run `qb-hooks status` to see what is installed, `qb-hooks install` to add it to a repo by
hand, `qb-hooks uninstall` to take it off. `install` is idempotent, and re-running it is how a
repo that was set up before a new guard existed picks it up — `create-worktree` runs it on the
main checkout every time it makes a worktree, so that normally happens on its own.

### The pre-push guard — a two-headed graph, a branch writing in a generated file, and a rewritten release

The other hook `qb-hooks` installs, and the reason it exists is that on 2026-08-22 four
branches each minted migration `0029`. Every one of those authors ran
`migration_reconcile.py preflight`, every one got a **truthful** answer, and every one
proceeded in good faith — preflight compares a branch against `main` and cannot see an
unlanded sibling (#338). It reached CI as *"Multiple head revisions are present"*, took five
preflight runs and three renumbers to settle, and poisoned three worktree databases on the
way.

A runbook would have been followed correctly by all four and changed nothing. The failure
was not disobedience, so a procedure cannot fix it. Hence a hook.

It refuses three things and **takes** a fourth, all read from the files **at the commit being
pushed** rather than from the working tree or from a live database — so a push carrying a
broken graph is refused even from a checkout that does not have it:

**A protected branch that would receive a multi-head migration graph.** `alembic upgrade
head` refuses to load one, and the deployed database is then stuck wherever the last deploy
left it. The question is handed to the repo's own `migration_reconcile.py heads --ref`; the
refusal names both heads and the `preflight`/`apply` pair that resolves them. Protected
branches are `main`, `master` and `test` unless the repo says otherwise
(`git config --add qb.protectedBranch <name>`).

**A branch that has edited a file the release job generates.** `CHANGELOG.md`'s release
entries and the README's release list are written on the integration branch after the merge,
by `scripts/release.py run`, and by nothing else. A branch that writes either of them is
writing a file every other branch also has to write, at the same offset, so N such branches in
flight is N-choose-2 conflicts **by construction** — over nothing, since both entries are right
and both belong. On 2026-08-23 that was three of six open pull requests, all `CONFLICTING`,
while the three that wrote no release entry all merged clean; PR #398 landed both ways and
settled it (#122).

`release.py guard` answers it, and **the refusal names `changelog.d/<issue>.<kind>.md`** — a
worker told only "no" retries or works around it, and both are worse than the original mistake.
This is half a guard on purpose: a local hook cannot see `gh pr merge`, and a hook is
per-checkout and best-effort, so the `generated release files are output` CI job on
`pull_request` is the other half and neither is sufficient alone (#343, #169).

It is answerable without crying wolf **only because it is fork-relative**. The merge base is a
third reference point, and it separates "this branch wrote it" from "this branch is merely
behind a release somebody else cut". A branch that never touched the file passes, and has to:
the merge takes the base's entries cleanly, there being no competing edit. The CHANGELOG's
preamble is outside the guard for the same reason `frozen` leaves it alone — it documents the
convention the file follows, and a branch changing the convention has to be able to change it.

**A branch that has rewritten or deleted a release entry that already shipped.** A released
entry is immutable: it records what was broken or missing before that release, which is the
one part of a release no diff recovers. On 2026-08-20 a CHANGELOG conflict was resolved by
relocating the branch's own 133-line entry **under `## v2.59`**, on top of that release's
notes, and the branch sat on an open PR for two days with every guard in the repo green —
because they all read the file as a list of headings, and the headings were present, unique
and correctly ordered (#325). `release.py frozen` compares the TEXT instead: every
`## vX[.Y]` entry present at both the merge base and the pushed commit has to be identical,
byte for byte, and one that has vanished is a refusal too.

Fork-relative for the same reason as the check above, and it needs no stored state — the
shipped text lives in git, where a bad merge resolution cannot reach it. A branch that does
not touch a released entry passes by construction, which is what lets it run on every push.
Editing a shipped entry deliberately — fixing a typo in one — is done by saying so on a
commit of the branch, in a trailer a reviewer can see:

```
Release-Body-Edit: v2.59
```

Git's own trailer parser reads it, so it has to be in the trailer block rather than anywhere
in the message: the refusal ends with a pasteable copy of that line, and a commit body quoting
the refusal is not consent to it. It is scoped to `base..HEAD`, so the exemption expires with
the merge it was written for.

**Nothing here takes a release number any more.** A fourth step used to: where the pushed
commit added a release number the base did not have, `release_tag.py reserve` created
`refs/tags/vX.Y` on the remote as a compare-and-swap, so the second of two landers was refused
rather than merged (#296). It existed because a branch could stamp, and the number a branch
stamped was a *reading* of a shared CHANGELOG rather than a lock. Branches do not stamp; the
number is applied on the integration branch after the merge, where there is no race; there is
nothing to reserve.

It also cost #406. The reserved tag named the branch's `chore(release)` commit, and a squash
merge discarded that commit while the release's entry landed perfectly — leaving
`refs/tags/v3.8` addressing history nobody could reach, with every check green because a tag of
that name resolved. There is no branch-side release commit now, so a rewriting merge has
nothing to lose (#122).

**Where a check does not apply, it says nothing at all.** No `migrations/versions/` at the
pushed commit, no `CHANGELOG.md`, no reconciler or release tool in the repo: silence, not a
warning. This harness installs into repos that are neither quarterback nor lexray, and a hook
that greets every push in an unrelated repo with a line about Alembic is a hook that gets
uninstalled — after which it is protecting neither repo.

**Where a check *does* apply and cannot be run, the push is refused.** A repo with migrations
but no reconciler, or no Python to run one with, is refused by name with the remedy attached.
An unrunnable gate is not a passing gate; a hook that skips silently when a tool is missing is
worse than no hook, because it reads as protection.

**Bypassable deliberately, never accidentally.** `git push --no-verify` is the express opt-out
for one push. A repo that genuinely does not want a check records it —
`git config --bool qb.prePush.migrationHeads false`, or `qb.prePush.generatedFiles`, or
`qb.prePush.releaseBodies` — and
`qb-hooks status` reports it, so a guard that has been switched off cannot look like one
quietly passing.

The tools are found at `scripts/migration_reconcile.py` and `scripts/release.py`, overridable
per repo with `qb.migrationReconcile` / `qb.release` (or the `QB_MIGRATION_RECONCILE` /
`QB_RELEASE` env vars for a one-off). The base ref comes from
`refs/remotes/<remote>/HEAD`, falling back to `main`, `master`, `test`, and overridable with
`qb.baseBranch` — which, when set, gets no fallback: an operator who named a base meant that
base, so a `qb.baseBranch` that is not fetched is a refusal with `git fetch` as the remedy
rather than a quiet swap to `main`. When the base branch is part of the same push — `git push origin main topic`
— the other refs are judged against the commit `main` is bringing, not against the
remote-tracking ref it is about to replace; otherwise a base and a branch claiming the same
release entry could land together with the guard reporting green.

The CHANGELOG path is *not* configurable, and that is the point: `release.py` reads and writes
`CHANGELOG.md` by name, so a knob here would only decide which file the hook checks for
existence before handing the question to a tool looking somewhere else.

When the fork point cannot be read at all — a shallow clone, unrelated histories — the release
checks say `LIMITED` and pass: without a merge base, a file this branch *wrote* is
indistinguishable from one it inherited, and refusing every branch on a base it cannot see
would stop correct ones. They say so rather than reporting the strong answer for the weak one.

One limit, stated rather than discovered: the base ref is read from **this checkout**. A
checkout whose `origin/main` is a week stale judges a branch against a week-old base. Fetch
freshness is CI's job — this is the cheap guard that catches it at the keyboard. `harness/tests/test_pre_push_hook.py` drives all
of the above through real `git push` against real remotes.

### `qb-stash` — a stash that belongs to one worktree

The other half, because refusing a stash is only half an answer to somebody who needs one.
`refs/worktree/*` is the one ref namespace git keeps per worktree, and `git stash create`
mints a stash commit without touching the shared stack. `qb-stash` is those two facts
joined up:

```bash
qb-stash push [-m msg]    # snapshot tracked changes, then revert the tree
qb-stash list             # this worktree's entries, newest first
qb-stash show [n]
qb-stash apply [n] [--index]
qb-stash pop   [n] [--index]
qb-stash drop  [n] | qb-stash clear
```

`--index` restores the staged/unstaged split as well as the content. That asymmetry is
what made the hand-recovery of the lost trees lossy: a staged addition came back unstaged,
which is easy to miss and easy to commit wrong.

**Two limits, measured rather than assumed, and named rather than half-implemented.**
`git stash create` takes **no pathspec**, so `push` snapshots tracked changes across the
whole worktree; it also has **no `-u`** and ignores untracked files, so `push` leaves them
in the tree and says which ones. Both are refused with an error rather than silently
widened — a pathspec quietly applied to the whole tree would revert work the caller never
mentioned. For path-scoped work (removing a fix to prove a regression test goes red), use a
patch file: it is per-worktree by construction too, and it *does* take a pathspec. That is
the mechanism the red/green briefs already use.

Entries die with the worktree, since `git worktree remove` takes `refs/worktree/*` with it
and `git status` cannot see them. `remove-worktree` copies any it finds into
`refs/qb-stash-rescued/<branch>/` before it tears anything down, and tells you where they
went.

### `qb-claim` and `qb-claimed` — what you are working on, before you start

Two scripts, one primitive, and they exist because the board's claim table had never once
been written to by anything automatic. Thirteen agents worked three shared checkouts and
`claims()` returned `[]` fleet-wide; the single row that ever appeared was written by hand
because a human told an agent to (#172).

```bash
qb-claim issue 172 --note "worktree feat/issue-172"   # 0 taken / 1 held / 2 unknown
qb-claim pr 207 --ttl 7200
qb-claimed                                            # 0 held  / 1 free  / 2 unknown
qb-claimed --json --quiet
```

**Neither composes a key.** They name the *resource* — kind and value — and the board
derives the key from it, reading the repo off the checkout's origin remote. That is the
whole of #172: the plan wrote `work/<repo>#163` while an agent wrote `issue/<repo>#163`,
and because `(kind, key)` is the unique index those were two resources. A shell tool
spelling a third would be the same defect with a new party.

**Three exit codes, not two.** `1` names a holder. `2` is everything else that is not "it
is yours": no board configured, no origin remote, a board that cannot be reached — and also
a *definite* refusal (a 401 on a rotated token, a 422 on a ref the board does not key) and a
contention that ended with nobody holding the key. It is deliberately not `1`, because a
gate that reads "cannot tell" as "nothing held" fails open on every unconfigured host, which
is a gate that stops nothing on exactly the hosts nobody checked. `preland.py` states the
same rule about itself: *"a merge gate that fails open wherever it cannot see is not a
gate."* The policy is the caller's; these two just answer honestly.

And it is not `1` for the opposite reason too: `create-worktree` turns `1` into a hard
refusal telling the operator to go and talk to the holder, so a rotated token or a lost
insert race reported as a hold sends somebody looking for a peer that does not exist. Since
the exit code cannot say which of the two happened, the *text* does — a refusal reads "the
board REFUSED this claim and will refuse it again", with the remedy, because retrying is the
answer to an outage and no answer at all to a misconfiguration.

`qb-claim` prints the claim id on **stdout** and everything else on stderr, so a caller can
capture the id for `claim/renew` and `claim/release` without parsing prose.

**`create-worktree` takes the claim for you.** It derives the issue number from the branch
it is about to make (`feat/issue-172`, `fix/issue-114`, `feat/issue-135-qb-next`) and
claims it *before* the tree exists, so a refusal costs nothing to unwind:

| flag | what it does |
| --- | --- |
| *(default)* | Claim the issue the branch names. Held by somebody else → **refuse**. Cannot tell → warn loudly and carry on |
| `--no-claim` | Skip it entirely, silently |
| `--require-claim` | Refuse on *any* uncertainty too — no board, no token, or a branch that names no issue |
| `--claim-ttl <secs>` | How long to hold it (default 28800 = 8h; a worktree outlives an hour, and a lapsed claim reads as free to the next agent) |

The asymmetry between the two failure modes is the one decision worth arguing with. A 409
is the board saying something definite and two agents on one issue is exactly what this
prevents, so it refuses. A board outage is not, and failing closed there would make the
board a single point of failure for every worktree on the fleet — `--require-claim` is how
you ask for the strict reading instead.

A branch that names no issue is **warned about** rather than skipped quietly: an unclaimed
checkout is one where the next agent has nothing to collide against, and silence is what
let `claims()` stay empty for four months.

**The checkout claim names no session, and it is handed back if the tree never appears.**
Both follow from *who* the claim is for. `qb-claim` defaults `--session` to
`$CLAUDE_CODE_SESSION_ID`, which during a checkout is the session of whoever ran
`create-worktree` — a parent agent's, or nothing at all from a human shell — while the
agent that will work in the tree has a different session and does not exist yet. Stamping
the creating session hid the claim from the gate it exists to feed (`/claim/held` narrows
on the session) and made it unmutable by its own worktree (`may_mutate` requires a recorded
session to match, so the new agent got a 403 renewing or releasing its own claim). So the
claim records none and belongs to the machine until somebody picks it up.

And because the claim is taken *before* `git worktree add`, the run that dies in between —
branch already checked out elsewhere, disk full, a bad base ref, a failing `.env` step —
would otherwise leave the issue held for the full `--claim-ttl` by an agent that does not
exist, refusing the next agent for a working day over a checkout that never happened. An
EXIT trap hands it back, best-effort, through `qbdata`'s own client, and says what is left
held when it cannot. Two guards on it: past the point the worktree directory exists the
claim belongs to a tree somebody can work in or remove, so the trap stands down; and a
claim the board reports as `renewed` was this machine's *before* the run, so it is left
alone rather than destroyed by somebody else's failed checkout. That second one is why the
checkout claims with `--json` — the flag exists so a caller can read `renewed` without
grepping the prose on stderr.

### `qb-release` — handing the claim back when the work ends

The verb the checkout claim did not have (#337). `create-worktree` takes it; nothing gave
it back. #277's `stop` half releases a *session's* claims and this one has no session — it
was taken by a script, on behalf of a worktree, before the agent that would use it existed
— so the only thing that ever freed one was the 8h TTL. Measured on 2026-08-22: four plan
items still carried live claims after their PRs had merged, one of them shipped as v2.78
hours earlier, and the four slots would have stayed held until the evening.

```bash
qb-release                          # the issue this checkout's branch names
qb-release issue 337                # 0 released / 1 not ours / 2 unknown
qb-release --branch feat/issue-337  # what remove-worktree runs
```

**Nothing to release is exit 0.** Three callers release one claim by design — the land step,
the worktree teardown, and `prune-worktrees` — so the second and third find the work already
done and must not report that as failure. The board is idempotent under it too: `released_at`
is set once and the row stays as history.

It names the resource and lets the board derive the key, exactly as `qb-claim` does. `1`
means somebody else holds it (another machine, or another session on this box) and is a
different answer from `2`, which is a board that could not be reached — collapsing them is
how "this is not yours" gets reported as "try again later" forever.

**Three places now hand it back**, and the TTL stays underneath all three as the backstop it
was meant to be:

| where | what it releases |
| --- | --- |
| the land step | `/review-pr`, `/panel-review-pr` and `/fix-and-land` run `qb-release issue <n>` after `gh pr merge` — the common case and the cheapest |
| `remove-worktree` | step 8, releasing what the create-name names (so `/drop-worktree` covers it too). `--keep-claim` opts out, for a teardown that is not the end of the work |
| `prune-worktrees --prune` | claims whose note names a worktree that is no longer live — the debris case, matched on the note `create-worktree` writes and nothing else does |

### `qb-admit` — is there room to start? (#337)

The admission half of the in-flight bound, and **off unless a repo asks for it**.

```bash
qb-admit          # 0 room (or no bound configured) / 1 full / 2 unknown
qb-admit --json
```

Rich, after eight agents fanned out and back in over one morning: *"I want rolling process
not batch."* The costs were all of the predicted kind — two branches minted migration `0029`
independently, a third was renumbered twice mid-flight, the largest open diff went DIRTY the
moment the first landed. Nothing counted, because nothing ever had: `git worktree list`
returned 48 on that box.

**The count is claims**, asked of the board (`GET /claims/in-flight`): live `work` claims
naming an issue or a PR in this repo, fleet-wide, whoever holds them. Not worktrees — 48,
mostly debris from finished work — and not open PRs, by which time the branch exists.
Quarterback bounds what it has authority over; work that never registered is outside it, and
`create-worktree --no-claim` is the visible way to stay there. A human starting a ninth thing
by hand is absorbed rather than exempt, with no special case at all: they run
`create-worktree` and take the same claim an agent does.

The ceiling lives in the repo's rules file beside #85's gates, and **ships null**:

```json
"in_flight": { "max": null, "min": null }
```

`max` is enforced by `create-worktree`, which asks before it takes the claim — the same
refusal `--require-claim` already makes when the issue is taken, at the same moment, for a
second reason. A full window refuses (definite, like a 409); a count that cannot be read
warns and proceeds (a board outage must not stop every checkout on the fleet), unless
`--require-claim`. `--no-bound` waives the refusal for one checkout and **still takes the
claim**, so the window keeps reporting the truth about itself.

`min` is the floor and nothing reads it yet: it is where the planner's discretion (#232) will
be configured, and that needs the changed-file overlap (#101/#287) and a planner that does not
exist. Recorded, reported, inert — the shape `review_panel.require_failing_test` already has.

A malformed ceiling fails **open**, loudly (exit 2, and the reason names the file). Refusing
every checkout on the fleet over a typo in a config file is the worse of the two failures and
the harder one to diagnose from a phone.

**The ceiling is advisory and can be exceeded.** The count is taken and then the claim is, so
two checkouts starting in the same second both see room — a simultaneous fan-out can put the
window over by however many raced. That is the standing the claim table already gives itself
("advisory: it cannot stop a merge, only warn you"), and closing it means moving admission
into the board so the count and the insert are one transaction. The rolling behaviour survives
it: past the first burst, checkouts arrive one at a time against a count that is truthful.

### `qb-end` — the verb that stops a session

There were three ways to start a session on this fleet and, until #277, none to end one.
What stood in for ending was the TTL, and a TTL is a floor rather than a report: an expired
lease says *nobody renewed*, which is the identical row whether the work landed, the pane
was closed, or the agent is thinking hard for nine minutes (#252). And because nothing
released a claim except the agent that took it, a seat whose context was reset kept work it
had no memory of taking — renewing it from a fresh conversation, where passive expiry could
never reach it because nothing had died (#263).

```bash
qb-end                                     # this session ($CLAUDE_CODE_SESSION_ID), finished
qb-end "$sid" --reason killed              # something closed it
qb-end "$sid" --reason context_reset       # /clear: the pane lives on, this conversation does not
#   exit 0  recorded — including "it had already ended"
#   exit 1  refused  — another machine's session
#   exit 2  unknown  — no board, no token, an outage
```

One call releases the session's lease **and** every live claim stamped with that session, and
stamps the lease with why. The reasons are a closed set — `finished`, `killed`, `timed_out`,
`context_reset`, `superseded` — because the field is branched on by a dashboard, and a sixth
spelling of "finished" reaches a human as an unknown. `stalled` and `crashed` are not among
them: both are conclusions a reader draws from silence, never a report an agent makes about
itself, and a lease with no reason on it *is* that report.

**It records; it does not operate.** Nothing here signals a process or closes a pane. Its two
automatic callers are the ones that actually observe an ending:

| caller | when | reason |
|---|---|---|
| `qb-hook` SessionEnd | Claude Code says the session is over | `context_reset` when its payload says `clear`, else `finished` |
| `qb-hook` SessionStart | a different conversation appears in a pane that held one | `superseded` — the backstop for endings nothing observed |
| `qb-seat-click` | the ✕ on the seat bar, and the dashboard's, just before `kill-pane` | `killed` |

The ✕ can do that because `qb-hook` stamps the pane with `@qb_session` at SessionStart —
nothing else records which session a pane holds, which is the limit #266 names on what a
fleet view can *act* on rather than only describe. Best effort throughout: no stamp, no
board, or a refusal all fall through to closing the pane, because the pane is what the
human clicked to close.

The line `qb-seat` draws — *"the board coordinates work, it does not operate the machine"* —
is about **dispatch**, and none of this moves it. What an agent works on is still its own
choice, self-selected and claimed atomically.

### `qb-start` — the verb that begins a session, and it ships off

The other half of #277. There were three ways to start a session on this fleet and every one
of them ended at a human hand: `qb-seat` in a pane somebody typed into, the dashboard's ⚒ on
a mouse click, and `run_agent`'s headless `claude -p` inside a loop a person launched. So a
plan could say what was next, the board could show who was on what, and nothing could act on
either.

```bash
qb-start /fix-issue 277               # a session working issue 277
qb-start /panel-review-pr 352         # …reviewing PR 352
qb-start --dry-run /fix-issue 277     # every refusal, nothing started
qb-start --policy --json              # what will this machine start? (starts nothing)
qb-start --via dash /fix-issue 277    # …and record what pulled it
#   exit 0  started  — a real, attachable session exists
#   exit 2  used wrongly
#   exit 3  NOT ENABLED on this machine — the default, and the whole of it
#   exit 4  that command is not on this machine's allowlist
#   exit 5  this machine's cap is spent, or its panes could not be counted
#   exit 6  the shared window is spent, or qb-pace could not read it
#   exit 7  this repo's window is full, or qb-admit could not read it
#   exit 8  somebody holds that work, or the claim could not be taken at all
#   exit 9  could not start it — the claim goes back, and it says if that failed
```

**Off by default, and the default costs nothing — not even a file.** With no
`~/.config/quarterback/spawn.json` this exits 3 before it has looked for a board, a token, a
network, tmux or the agent. There is deliberately **no environment override** for that path —
one existed while this was being written, and a bypass a repository could set falsifies the
only claim the gate makes. A machine opts in in the home-manager module; a repository cannot,
and neither can an agent:

```nix
programs.quarterback-harness.spawn = {
  enable      = true;
  commands    = [ "/fix-issue" ];   # empty by default: the second lock
  maxSessions = 1;                  # 0 is a freeze
};
```

**A malformed policy fails CLOSED, which is the opposite of `qb-admit` and is the same
principle.** `in_flight.max` is a restriction, so failing open on a typo admits one agent too
many while failing closed throttles every checkout on the fleet. `spawn.json` is a
*permission*, so failing open on a typo starts sessions nobody authorised on a box holding
`~/.claude`, `~/.config/gh` and the board token. Unreadable, unparseable, or `enabled`
anything other than the literal `true` — one answer, and it is no.

**The brief is a named command with a number, never free text.** The set lives in `qb-start`
itself and a policy file can only ever *narrow* it, so a policy naming `/anything-i-like` is
refused exactly as one naming nothing is. That matters because this repo is public: under a
board-sourced trigger an issue body becomes the instructions for an agent with a full shell,
and the only mitigation that works is an allowlist — a filter is a list of the phrasings
somebody already thought of (#63). Each entry carries the resource its session claims, which
is what makes a spawn countable:

| command | claims |
|---|---|
| `/fix-issue`, `/fix-and-review`, `/fix-and-land` | `issue <n>` |
| `/review-pr`, `/panel-review-pr` | `pr <n>` |

**Everything it starts is counted, claimed, attachable and endable.** In that order, and the
order is the feature — a refusal costs nothing to unwind only while nothing has been taken:

0. This machine's own cap, counted off the tmux server: spawned panes whose agent has not
   exited. A `list-panes` that fails is *cannot tell*, not zero — a cap that switches itself
   off when its input goes unreadable is not one.
1. `qb-pace --gate`, and unlike a seat it *obeys* rather than warns. A seat warns a human who
   can then decide; a spawned pane has nobody to tell.
2. `qb-admit`, so #337's bound is not decorative — anything that starts a session has to go
   through the thing that counts them.
3. `qb-claim`, through the ordinary path, on the resource the command names, **before the
   process exists**.
4. A board post, before it is a process. Refusals are posted too, so a fleet's spawning is
   readable rather than something you find out about by noticing a pane.
5. A detached tmux window — a real session a human can attach to, read and interrupt. There
   are no hidden sessions here. Its three pane options are what make it findable, so a window
   that cannot be stamped with them is **closed again** rather than left running where nothing
   could count or end it.

**Every gate has to return a definite go, and that is the opposite of what `create-worktree`
does with the same answers.** An outage, a malformed ceiling, a `qb-pace` that could not read
the caps, a tool missing from a partial install: each of those is the gate not running, not the
gate passing. A checkout failing open is a human who has already decided to work being told the
board is unreachable; a spawn failing open is an unattended session nobody decided on, against
a ceiling nobody could read — #244's rule applied to the risky direction. It costs a human one
retry, and only where something is already broken.

The claim records **no session**, for `create-worktree`'s reason: the agent that will do the
work does not exist yet, and a claim stamped with a session that is not the worker's makes
`may_mutate` refuse the worker its own renewal. It is a machine-held claim that the spawned
session's own `create-worktree` renews and that the land step, the teardown and the sweep hand
back.

**And what pulled it is recorded, which is the question a session nobody started raises.**
`--via` names the caller — `cli` for a person at a prompt, `dash` for the dashboard's ⚒ —
and it lands on the claim note, on the board post (spawns *and* refusals) and on the pane as
`@qb_spawn_via`, so `tmux list-panes -a -F '#{@qb_spawn_via}'` answers it for a window
somebody has found and does not recognise. The set is closed for the same reason the command
allowlist is: a provenance field its caller fills in freely is one that can be made to say a
human did it. Adding a trigger is a line in `qb-start` and a line here.

**A caller can ask before it offers.** `qb-start --policy` answers *what will this machine
start* — enabled or not, which commands, how many at once, and the refusal in full when the
answer is no — then exits 0 or 3 having started nothing, claimed nothing, posted nothing and
consulted nothing but the policy file. It exists because a trigger with a button on it needs
the answer *before* the click: an affordance that looks live, is clicked, and only then
explains that this machine never opted in has spent somebody's attention to tell them
something it knew all along. It is the same `read_policy` the spawn path uses, at the same
point, so it can never answer yes to a machine the next line would refuse — and asking it is
not a second implementation of the gate.

**It is endable before it exists.** The session id is minted by `qb-start` and handed to the
agent with `--session-id`, so the pane wears `@qb_session` from the moment it is created rather
than from whenever the agent's SessionStart hook gets round to it — `qb-end <id>` works
immediately, the seat bar's ✕ can reach it, and the ordinary SessionEnd hook ends it with a
reason like any other session.

**It is not a dispatcher, and that line is not being moved.** `qb-seat`'s *"the board
coordinates work, it does not operate the machine"* is about **dispatch**: nothing here reads
the plan, picks an item, or tells an agent what to work on. It is told a command and a number
by whatever pulled it, exactly as the dashboard's ⚒ is told one by a click. Which work an
agent takes stays the agent's own choice, self-selected and claimed atomically.

**What pulls it: the dashboard's ⚒, and nothing else yet (#371).** The primitive landed with
no caller at all, which made the loop readable and still unstartable. The first caller is the
cheapest one there is — `qb-dash-tui`'s ⚒, which is still a human click, so it needed no new
safety: the gates, the machine cap, the allowlist and the claim are all here, at the
primitive, rather than at the caller. What that click gains is everything the old direct
spawn lacked: a session inside `qb-admit`'s window, holding a claim taken before the process
existed, endable by session id from the moment the pane appears, and posted to the board as
`via dash`.

**A hook or a cron floor is still not built, and that is the deliberate part.** The button
is paced by a person; the other two are not, and a trigger nobody is watching is the thing
that turns a bug into an overnight incident. A `SessionEnd` hook also has a question to
answer first that the button does not — *what is next* — and answering it by reading the
plan and handing an agent the first item is the dispatch this whole design refuses.

### `qb-status` — the pane's answer, the agent's answer, and the gap

The middle step of #277, and the reason a fleet view could not previously tell a session that
was thinking hard from one whose context had been reset out from under it from one that no
longer existed. tmux knows whether a **process** is there; the board knows what the **agent**
last said about itself. The disagreement between them is the diagnosis.

```bash
qb-status                      # this session ($CLAUDE_CODE_SESSION_ID)
qb-status "$sid" --json        # both sources side by side
#   exit 0  alive     — something is running it
#   exit 1  finished  — it ended, and something said so
#   exit 2  gone      — nothing is running it and nobody reported an ending
#   exit 3  unknown   — cannot tell: no board, no token, an outage
```

| pane | agent | what it is |
|---|---|---|
| running | fresh lease | working, and nothing to say |
| running | **stale** state | a long turn (#252) — the beacon moves at turn boundaries only, so an old `working` describes a busy pane rather than a stuck one |
| running | **ended** | #263's shape: a `/clear` or a supersede, where the pane lives on and this conversation does not |
| running | no lease at all | an agent the board has never heard of — no token, no hooks, or a board that was down at its start |
| gone | live lease **here** | a crashed or killed seat that never got to report itself; it will read as working until the TTL runs out |
| gone | live lease on **another machine** | not a verdict: this box's tmux server is not where that session runs, so the pane half of the evidence is missing rather than negative |
| gone | ended | finished, and everything agrees |

**"Gone" is never guessed from an absence of tmux.** Run outside a multiplexer, the pane
source answers *cannot tell* and says so — reporting a session gone because this process could
not see a pane would be an inability dressed as a fact, which is exactly what the ending
vocabulary exists to stop.

**It observes and nothing else.** No process is signalled, no pane is closed, no lease is
touched, and it never ends a session it finds ended. `qb-end` is the verb; this is the question
you ask before you use it.

### `worktree-holder` — is somebody else in there?

The fourth script answers one question: **which live agent is working in this
directory?** It is what the other three consult before they destroy something.

```bash
worktree-holder ../myapp-fix-issue-42     # or a branch name
#   exit 0  nobody else — free, or held only by this session
#   exit 3  held by another live agent (who, since when, doing what)
#   exit 4  could not tell — no board configured or reachable
```

Worktree isolation is file-level: separate directories, databases and ports, so two
agents never edit the same file. It has never had a story for two agents deciding to
operate on the same *directory*, and that is the collision left once every other one is
solved. It happened here: one agent was three commits into a review cycle in
`~/source/quarterback-feat-issue-24` when another, seeing the branch was behind `main`,
ran `git rebase origin/main` inside it. The holder found its branch at somebody else's
commit with conflict markers in four files. Nothing about that was unreasonable — the
second agent had no way to know the directory was occupied.

**The board could not answer it either, and the reason is worth knowing.** A lease
records the directory its agent was *launched* in, and the shell cwd resets between tool
calls, so an agent handed a worktree by `/fix-issue` still reports `cwd=~/src/proj` and
`branch=main`. Every live agent in a repo looks identical on the board no matter which
worktree it is really in. The missing half is local: the session marker `/fix-issue`
writes to `~/.cache/claude-code/session-cwd/<session-id>`, whose contents *are* the
worktree path. `worktree-holder` unions the two — the markers say which sessions were
handed this worktree, the board says which of those is still alive and who holds it —
and adds anyone whose lease cwd is the worktree itself.

**Advisory, never a lock.** Exit 3 is a reason for a script to refuse and name the
holder, not a reason a worktree becomes unusable: `remove-worktree --force` always wins,
leases expire on their own, and "could not tell" is a distinct exit code precisely so a
board that is down never stops anyone working. The failure being prevented is the
*silent* rewrite, not the deliberate one.

Where it is wired in:

| Script | What it does with the answer |
|---|---|
| `remove-worktree` | Refuses before destroying anything, names the holder, suggests `--force` |
| `prune-worktrees` | Reports a held directory separately and never counts it as a leftover, so `--remove-dirs` cannot `rm -rf` it (and the container sweep, which takes its evidence from that list, inherits the protection) |
| `create-worktree` | Already refused an existing directory; now says *whose* it is, because "already exists" sends you looking for debris and the answer is sometimes an agent still working |

Agents typing raw `git rebase` / `git reset --hard` in someone else's worktree remain
out of reach, and that is accepted: the slash commands that drive worktree teardown
(`/wt`, `/drop-worktree`, `/tree-shake`) tell the model to ask first, and an agent
running raw git was never going to be caught by tooling it did not invoke.

### `qb-seat` — one pane, one seat, one identity

Starting a fleet is the part that never scaled: open a terminal, `cd`, run the agent, read
the plan out loud to it, repeat. The human doing the reading is a dispatcher, and a
dispatcher is exactly what the board exists to remove.

```bash
qb-seat 3                  # seat 3, in this directory
qb-seat 3 --dry-run        # print the environment, cwd and brief; start nothing
qb-seat 3 --model opus     # anything else is passed through to the agent
qb-seat 3 -- --dry-run     # …and everything after -- is passed through untouched
```

`--dry-run` and `--help` are taken from anywhere in the arguments, not only from the
position above: everything after the seat number goes to the agent verbatim, and a
misplaced `--dry-run` that started five agents would be the one mistake this flag exists
to prevent. `--` is the way to hand either word on to the agent anyway.

A multiplexer supplies the panes and its layout says `qb-seat 1` … `qb-seat n`; this
supplies what goes in one. It is deliberately thin, because everything that would make it
thick already exists somewhere better — identity, presence, lease renewal, publish-on-push
and transcript push all arrive from the lifecycle hooks, and the worktree with its isolated
database arrives from `/fix-issue`. What is left is: name the seat, enter the repo, start
the agent on a brief.

The brief is **identical for every seat** — *read the board, claim an unclaimed item
atomically, work it, release on exit* — and that is the design rather than an omission. A
spawner that reads the plan and hands seat 1 the first item is hub-and-spoke with a hub
that runs once, at t=0, and then stops existing. Override it wholesale with
`QB_SEAT_BRIEF` if a fleet wants a different one — or set it to the empty string for a
pane that should come up waiting rather than working, which starts the agent with no
prompt at all. The seat number is the only thing that differs between panes. It also
tells a seat to **stop after one item**, because a seat that re-claims when it finishes
turns the fleet into a drain, and nothing yet bounds how much work a fleet may take on.

**Why the instance is per seat and never host-wide.** The lifecycle hook keys its ask-poll
cursor on `QUARTERBACK_INSTANCE` (`qb-asks-<agent>-<instance>`), so one value exported for
the whole box gives n seats *one* cursor between them: whichever seat polls first advances
it past everyone else's mail and the other n−1 never see an ask addressed to them. Set per
seat it is the opposite — a stable, typeable `zeus/seat-lexray-3` instead of
`zeus/a4f81c2e`, which survives the seat restarting in the same pane because the board
hands a returning key its old name back.

**Why the name carries the project as well as the number.** `seat-3` on its own makes the
*namespace* the machine while the *numbering* is per screen — and `qb-seats` numbers from 1
every time it builds one. So the second screen on a box asked for seat 1, found the first
screen's seat 1 holding the pane marker, and refused: not an edge case reached by an unlucky
choice of number but the guaranteed outcome of starting a second screen, which made one
screen per project the one thing this could not do (#208).

The guard was right and its key was too coarse, so the key grew a scope. A seat is
`seat-<project>-<n>`, so `seat-lexray-1` and `seat-nix-fleet-1` are two seats while
`seat-lexray-1` started twice is still one — every property the refusal names survives.
The scope defaults to the basename of the seat's own repository, because a screen is per
repository; `QB_SEAT_SCOPE` overrides it for the two cases that default cannot read, which
are two screens on *one* repository and anyone who wants the old machine-wide numbering
back (`QB_SEAT_SCOPE=`, empty and meaning it).

The scope is **slugged**, and that is not cosmetic: an `X-Agent-Name` that does not match
`^[a-z0-9]+(?:-[a-z0-9]+)*$` within 40 characters is refused with a 400, so a repository
called `Foo.Bar_2` would otherwise make every seat in it fail registration. The basename is
folded to lower case, every run of anything else becomes one hyphen, the ends are trimmed
and the middle is capped at 32. A scope that slugs away to nothing — a directory named
`___` — leaves the bare `seat-3` and says so on stderr, rather than inventing a project
name nobody could type.

**Why it registers that name itself, before starting anything.** Since v2.12 the board
*designates* the name half of an identity, and `QUARTERBACK_INSTANCE=seat-lexray-3` is only a
**request** (`X-Agent-Name`). Allocation is first-contact-wins. Measured against a live board:

| First contact | Later request | Board says |
|---|---|---|
| key only, no name | — | `zeus/meadow-russet` |
| key only, no name | `seat-lexray-9` | `zeus/meadow-russet` — **the request is ignored** |
| key **and** `seat-lexray-9` together | — | `zeus/seat-lexray-9` |

When this was written the MCP server was the only client that made the request and the
lifecycle hook was not, so the hook's `SessionStart` usually got there first and a seat came
up as two random words about as often as not — losing the one property the numbering was for.
**Every client asks now (#156)**, so the row is settled correctly whichever one reaches the
board first.

`qb-seat` still makes a single `GET /whoami` carrying both headers before it execs, and the
reason is the read-back rather than the request: a name is granted only when it is *free*, so
a second pane started as the same seat is quietly given something else, and it is worth being
told that at the one moment a human is looking. It warns when the board's answer is not the
name it asked for — which also happens when the key was bound to a designated name on some
earlier run, since allocation hands a returning key the name it already had and a request
cannot displace one that exists.

*Addressing was never at risk either way*, and that is worth knowing before someone
re-derives the worry: the board resolves `machine/key` as a permanent alias, so an ask sent
to `zeus/meadow-russet` is returned by a poll that asks for `to=zeus/seat-lexray-3`. This is
about the name a human types and reads on a status bar.

**Two panes on one seat is refused, and the board cannot be the one to refuse it.**
They export the same instance, so they send the same key, so the board hands them *one*
identity — from its side they are indistinguishable by construction. They then share the
ask-poll cursor, and whichever polls first swallows the other's mail: the exact bug the
per-seat instance exists to prevent, one level down, and invisible because both seats
otherwise work. So the check is local, where the panes actually are. `qb-seat` records its
pid in `$XDG_RUNTIME_DIR/qb-<seat name>.pid` — or, on a machine with no `XDG_RUNTIME_DIR`
(macOS, most containers, ssh onto a box with no systemd user session), in
`${TMPDIR:-/tmp}/qb-<uid>-<seat name>.pid`, where the uid is in the name because `/tmp` is
shared and a marker there is not. The marker is keyed on the **whole name** and not on the
bare number, because the two have to agree or the guard is protecting something other than
the identity it describes — a marker on the number alone refused the second screen's seat 1
while the board would happily have given it its own identity. It exits **3** if a live
process already holds that seat. A marker left by a seat that died is taken over rather than honoured, and
`QB_SEAT_FORCE=1` overrides the refusal for a pid that has since been reused by something
unrelated — noisily, on stderr, because being wrong about that is the shared-inbox bug
with nothing on screen.

The marker is **claimed atomically**, by hard-linking a fully written temp file into
place, and it is claimed *before* the board call rather than after it. That is not
fastidiousness: the case the guard exists for is a layout, a layout starts all n panes in
the same instant, and look-then-write loses that race by construction — every pane sees a
free seat while the one that got there first is still several seconds deep in registering
its name. Measured on the check-then-write version: twelve panes started as seat 1 left
between six and twelve agents running, all sharing one identity. Hard-linking leaves one.

**Best-effort by construction.** No board configured, no token, no `curl`, no network: the
registration is skipped and the seat starts anyway. A seat that refused to run because a
cosmetic name could not be reserved would cost more than the name is worth — the same
bargain `qb-stage` strikes, and the panel's board recording before that. Two things it is
*not* silent about, because both are worth a line before the rest of the session goes
wrong the same way: a board that answered **401** (a revoked token, or the other island's
token — the lifecycle hooks are about to be refused identically) and a board that did not
answer at all (fine on a laptop, and said differently). The token itself goes to `curl` on
stdin, never in argv, where any local process could read it out of `/proc` for the life of
the call.

What it deliberately does **not** do: create a worktree (under self-selection a seat does
not know its branch until it has claimed something, and `/fix-issue` owns that path and its
per-branch database), assign work, or drive the agent past starting it.

| Variable | Default | What it does |
|---|---|---|
| `QB_SEAT_REPO` | the pane's cwd | Where the seat works; the layout normally sets the cwd instead |
| `QB_SEAT_BRIEF` | the built-in brief | Replaces it wholesale; empty means no brief at all |
| `QB_SEAT_AGENT` | `claude` | The agent to start |
| `QB_SEAT_SCOPE` | the repository directory's name | The project half of `seat-<scope>-<n>`, which is what lets two screens each hold a seat 1. Slugged to what the board will take as a name; set it when two screens share one repository, or set it **empty** for the machine-wide numbering this had before #208 |
| `QB_SEAT_FORCE` | unset | Start anyway when this seat number looks already taken. Truthy values only (`1`, `yes`, `true`, `on`) — `QB_SEAT_FORCE=0` leaves the guard on |
| `QB_SEAT_PACE` | `warn` | What to do about the shared subscription's window before starting. `warn` says it and starts anyway; `obey` refuses to start at `hold` and names when the window comes back (exit 4); `off` does not consult at all. See `qb-pace` below |
| `QB_SEAT_YOLO` | **on** | Permission prompts. A seat starts with them off (`--dangerously-skip-permissions`) because nobody is watching the pane to answer one; `QB_SEAT_YOLO=0` (or any of `no`, `false`, `off`) gives them back. The flag is claude's spelling: point `QB_SEAT_AGENT` at a wrapper for anything else |
| `QUARTERBACK_BASE_URL`, `QUARTERBACK_TOKEN` / `QUARTERBACK_TOKEN_CMD` | from the config file | The board to register the name with |
| `QUARTERBACK_CONFIG` | `$XDG_CONFIG_HOME/quarterback/config`, else `~/.config/quarterback/config` | Where those three are read from when the environment does not supply them. Sourced in a subshell, and only those three are read back out of it, so nothing else the file sets can reach the seat or the agent |

The environment beats the file variable by variable — except the *credential*, which is
taken as a set: a `QUARTERBACK_TOKEN_CMD` in the environment means the file's static
`QUARTERBACK_TOKEN` is not used at all. Best-of-each would authenticate one island's board
with the other island's credential, which is the exact failure the per-host config exists
to prevent. A config file that *errors* is reported and then ignored entirely, rather than
half-applied and passed over in silence.


### `qb-seats` — the agent screen

Working a plan in parallel takes N terminals and a human dispatcher: open a terminal, `cd`,
run the agent, say what to work on, repeat. The dispatching human is the part that does not
scale, and it is the part the board exists to remove. `qb-seats` replaces the terminals.

```
qb-b 3                # three seats plus the board, in the current repo (the short name)
qb-seats              # the same, three by default
qb-seats 10           # ten: five across, two down
qb-seats --staged     # built, each seat waiting on Enter
qb-seats --no-yolo    # seats that stop and ask, as agents normally do
qb-seats --add        # add a seat to a running screen
ssh box -t qb-seats   # reattach from anywhere
qb-b list             # the screens that are up, numbered
qb-b resume 2         # reattach to the second of them, from any directory
```

`qb-seats` on its own reattaches to the screen for **the repo you are standing in**, which
is the one thing the shell after a dropped ssh link cannot be relied on to be. `list` and
`resume` are for that shell: neither needs a repo or a `-C`, because a screen already knows
the directory it was built in. `resume` takes the number from the list or the screen's name,
and with exactly one screen up it takes no argument at all.

A screen is recognised by a pane carrying `@qb_seat`, never by its name — `-s` takes
anything, the fleet's own screen is `qbseats` rather than `seats-nix-fleet`, and **what
tmux does with a name it will not take verbatim depends on which tmux you have**: up to
3.6a it silently renamed one (`my.screen` → `my_screen`), and 3.7b keeps it as typed. So
the list is read back from tmux and can only print names that really exist, which is also
the way to reattach to a screen an older tmux renamed under you.

**Inside the script a session is addressed by `#{session_id}`, never by its name**, and
that is not tidiness: `.` and `:` are a tmux target's own separators, so on 3.7b
`-t "=my.screen"` parses as pane `screen` of session `my` and every seat command failed
against a screen that plainly existed. `qb-seat-click` — the bar's ✕ and ＋ — does the same.
Names are for the messages a human reads; ids do the addressing, and
`test_every_session_target_is_an_id_and_not_a_name` reads both scripts to keep it that way.

A screen also records **what its seats are called**: `@qb_repo` is the repository it was
built in, and `@qb_scope` is the explicit `QB_SEAT_SCOPE` if it was given one. Both are set
on the session — `--add` puts `@qb_scope` on the pane it creates instead, so it does not
rewrite the session under seats already working in it — and together they are how anything
reading the screen from outside turns a pane into a board identity. `list-panes -a` is the
whole tmux server, so since #208 the seat number alone no longer says which seat a pane is;
the dashboard's SEATS panel and its FLEET-row jump both go through this.

One tmux session: N panes each running `qb-seat <n>`, and one full-width pane along the
bottom running `qb-board --follow`. Every seat gets the **same** brief — read the board,
claim one unclaimed item atomically, work it, release — and self-selects. Nothing reads the
plan on the agents' behalf and nothing assigns.

**Why real sessions and not sub-agents.** Sub-agents are a star: one orchestrator holds the
plan, fans out, and every result funnels back through one context window. Children are
anonymous, cannot be addressed mid-flight, and die on return with their context discarded.
Seats are a mesh — identities that outlive the run, findings that persist as posts, no
orchestrator to lose, and one property the star cannot offer at any price: **you can attach
to one seat, interrupt it and redirect it, while the others keep running.**

**`seats.enable` in the home-manager module** adds tmux, the one runtime dependency the
harness cannot assume. It is off by default: enabling the harness is something you do for
every host you own, and a seat screen is something you want on one of them.

Two things to know before you run it:

- **Your shell rc must not do anything interactive.** A seat pane starts a shell and the
  command is typed into it, so an rc file that greets you, animates, or reads stdin will
  swallow it. `QB_SEATS=1` is exported into every pane precisely so an rc can detect a seat
  and skip that. This is not theoretical — it cost five minutes per seat on the machine
  this was written for, where the greeter was an animation that ran until a keypress.
- **`QUARTERBACK_INSTANCE` must be per seat, never host-wide.** `qb-seat` sets its own —
  see its section above for why sharing one is worse than it sounds — and the layout's
  half of that guarantee is to strip any inherited value, from the session as well as the
  panes, so nothing split off later picks one up. Nothing in your shell profile should set
  it.

**Seats start with permission prompts off, and that is the default on purpose.** A seat is
a pane nobody is watching. The first tool call wanting a permission the agent does not
already hold stops it dead, and it stops in the one way this design cannot recover from: the
pane looks busy, the board shows a live agent holding a claim, and the work is not moving.
There is no operator to answer the question — that is what a seat *is*. So each seat gets
`--dangerously-skip-permissions`, and a screen you bring up works rather than waiting to be
asked something.

It is a real trade and it is made deliberately: it hands a full shell to N agents at once in
a repo whose tests, hooks and scripts all run as you. What decides it is the blast radius
either way — a seat that cannot act is useless to everybody, while a seat that can act is
dangerous in a repo you already trusted enough to point a fleet at. Say `--no-yolo` for one
screen, or export `QB_SEAT_YOLO=0` to have prompts back everywhere. The flag and the
variable are the same mechanism, so they cannot drift.

`qb-seats` deliberately does not create worktrees (a self-selecting seat does not know its
branch until it has claimed), does not assign work, and does not drive the agents past
starting them.

#### The dash — WORK IN PROGRESS

`qb-dash-tui` is a fourth pane for the right-hand side: fleet state, where the board pane
along the bottom (the **tape**) is the event stream. Who is alive and on what, who holds
which claim and for how long, what the fleet agreed to do next, every open PR with its CI
verdict, and every open issue with whoever has claimed it. Rows are clickable — a seat jumps
the tmux cursor to that seat's pane, a claim shows its note, a plan item explains why it is
where it is, a PR or an issue opens on GitHub. `qb-dash` is the same views rendered
without interaction, for a terminal that will not forward mouse events.

**The CI column has six states, and only one of them is quiet.** `gh pr view` reports a
PR's checks as a rollup, and an empty rollup means two things that are not remotely alike:
no run has been created for this head, or a run *has* and is parked behind GitHub's
workflow-approval gate — created, never executed, contributing no check runs at all. That
second case is what [#324](https://github.com/prisonblues/quarterback/issues/324) was filed
about: PR #282's suite went red, the two commits pushed to fix it came back
`action_required`, the check list went empty, and every reader took the blank for "nobody
has pushed since CI last ran". It sat two days.

So `qbdata.classify_rollup` never answers `none` — from a rollup alone the empty case is
`unknown` — and `qbdata.ci_report` settles it by asking the workflow-runs API, which is the
only endpoint a gated run is visible from:

| glyph | state | means |
|---|---|---|
| `✓` green | `green` | a run finished and every check passed |
| `✗` red | `red` | a run finished and something failed |
| `◐` yellow | `pending` | a run exists and is still going — wait |
| `⚑` magenta | `blocked` | a run exists and will **not** execute without a human; the reason names the newest run on the branch that actually did execute |
| `·` grey | `none` | no run was created for this head. Reached only by asking, never by finding the rollup empty |
| `?` yellow | `unknown` | the state could not be determined. Not a synonym for `none` |

The OPEN PRs title counts every one of those that is not green. Before this it counted reds
and nothing else, so a PR whose runs were gated contributed to no number on the screen.

`qb-dash` also carries a **REVIEW QUEUE** panel the clickable renderer does not have yet
(#273). OPEN PRs above it says a PR exists and CI is green; it never said whether anybody
had reviewed it, and on 2026-08-20 six of eight open PRs had never been panelled while the
newest round on the board was two and a half days old — neither number readable anywhere.
The panel is `POST /review-queue`: every open PR joined to every panel run, plan item, work
claim and landing-queue entry the board holds, so each row carries the state it is in, the
verb that state implies (`panel`, `re-panel`, `fix`, `rebase`, `land`) and how long it has
waited. Rows nothing may act on keep their place and show the reason instead of the verb,
because a panel that hid them would report an empty queue for a repo where everything is
stuck. The depth and the oldest wait also sit on the caps line at the top, beside the budget
they would be spent out of.

An age prefixed `~` is the longest the wait could have been rather than the length it was:
nothing records when a head moved or when a branch started conflicting, so those are
measured from the round or from the PR's opening. Nothing here starts a review — the panel
is a reader, and the thing that would act on it is #53.

**It opens on ONE project, and that is the interesting default.** Every panel here is
fleet-wide by construction — FLEET is every live agent on the board, CLAIMED every claim,
PLANS every repo's list — while a screen is built for one repository. So most rows were
somebody else's, and the repo cell was then the same word, eleven columns wide, on every
line of a 78-column pane (#261). The scope narrows the three board-derived panels to the
repos this screen watches and drops the column outright; the eleven columns go back to
`what` an agent is doing and to a plan item's title, and `quarterback#209` in CLAIMED
becomes `#209`. The repos are the ones the dashboard already resolved for its `gh` calls:
`--repo` (a checkout or an `owner/name` slug, repeatable), else `QB_DASH_REPOS`, else the
origin of `QB_DASH_REPO`, else of the directory it was started in.

`--repo` reads `owner/name` as a **repository** and anything else as a **checkout**: a
leading `./` or `/`, a third segment, or a single name that is a directory here. So a
two-segment relative path needs its `./` — `--repo src/nix-fleet` is the repository
`src/nix-fleet`, which is probably not what was meant, and `--repo ./src/nix-fleet` is the
directory. Deciding on the shape is what stops the answer depending on which directory the
pane happened to open in.

**`--repo <checkout>` moves where work runs, `--repo <owner/name>` does not**, and the
difference is the point rather than an inconsistency. `/fix-issue` and `/panel-review-pr`
take a bare number and resolve the repository from the checkout their pane opens in, so a
slug — which names a repo this machine may have no checkout of — can only filter rows; a
checkout also becomes the cwd the ⚒ and the ⚖ launch into. Where a row's repo is not the
one this dashboard runs in, both icons are dimmed and a click on one says why rather than
starting it. **A guard that cannot tell refuses**: a checkout whose remote is `upstream`
rather than `origin` — or a missing `git` — leaves the dashboard unable to name its own
repo, and since `gh` and `git push` resolve a default remote without consulting `origin`,
treating that as "nothing to check" would have let the review go out anyway. The ⚖ had no such guard
before the scope existed: a review off another repo's PR row would have commented on, and
pushed a fix commit to, whatever pull request wore that number here.

Two repos keep the column — there it still tells rows apart, and two owners of one name
(a fork and its upstream) are two repos, compared as whole slugs rather than folded to the
bare name they share — and so does the wide view, which is the whole reason to widen. `s`
toggles between them in the TUI, redrawing from what the client already has rather than
re-fetching; the plain renderer has no keyboard, so it takes `--scope all` or
`QB_DASH_SCOPE=all`. **A narrowed panel always says what it
hid** — `FLEET · 3 · 2 elsewhere` — because a filtered pane that reads like the whole fleet
is worse than an unfiltered one: it is the same picture with fewer facts, and "nothing
claimed" and "nothing claimed *here*" are different claims about the world.

Four things stay fleet-wide on purpose. A row whose repo the board cannot name — an agent
outside a checkout, a fleet-wide plan item, a `plan:<uuid>` claim this process has not
resolved — is kept, because no repo is not evidence of another repo and hiding it drops a
live peer; it wears a `?` in front of its title, since the repo cell (`—`, `fleet`) was
the only thing that ever said so and the narrow view is exactly the view that drops it.
The SEATS panel's `state` column reads every agent the board reports rather than the scoped
ones: `tmux_seats()` lists every seat pane on the whole tmux server, so another screen's
seat is on that panel either way, and narrowing would leave the cell that says which seat
is waiting on you reading `—`. The held-issue
markers come from *every* claim, so an issue held by an agent working out of another repo's
checkout is still shown as held rather than offered to the next seat. And OPEN PRs and
ISSUES cannot narrow at all: `gh` was only ever asked about the watched repos, so there is
no other repo's row there to hide — only their column answers to the scope.

**The top line is the ceiling every pane below it works towards.** The seats spend one
Claude subscription between them, so the five-hour and weekly caps are a fleet-wide number
that none of the tables can show — and six seats working a plan in parallel is exactly the
way to spend a five-hour window in forty minutes. It reads `5h ██████░░░░ 64% 3h57m  7d
███░░░░░░ 41% 5d8h`: the share spent, and when it comes back. Green to yellow at 70% and
red at 90%, or sooner if the endpoint's own severity says so. A weekly cap scoped to one
model appears under that model's name once it has been spent against; at zero it would be
noise, so it is left out.

The figures come from the same endpoint `/usage` reads, so a seat and the dash cannot
disagree, and an install authenticating with an API key has no subscription caps to report —
that is a missing line, not an error. **The endpoint rate-limits harder than a dashboard's
instincts suggest**: five calls inside ten minutes earned a 429 while this was being built.
So the interval is 3 minutes and it is enforced in `~/.cache/quarterback/limits.json` rather
than in each process's timer — three seat screens are three dash processes, and a per-process
clock cannot hold a machine-wide budget. A failed call keeps showing the last figures, which
are minutes old and still roughly true; past ten minutes the line appends a dim `?` rather
than pretending. A 429 backs off for ten minutes, because the failing call is itself the
thing being rate limited.

**And the same figures are readable as a verdict, not only as a bar.** Drawing the ceiling
left it enforced by a human noticing a bar go red, which is a poor arrangement for a fleet
of panes nobody is watching; `qbdata.pace()` turns the cached figures into `go` / `slow` /
`hold` / `unknown`, and `qb-pace` — its own section below — is what a script asks. It is the
same cache and the same three-minute floor, so a verdict never costs a call and the word and
the bar cannot come to disagree.

**Clicking starts work, not just navigation.** Each PR row carries a `⚖` and each issue row
a `⚒`; clicking one opens a confirmation showing the exact command, and confirming starts a
real session you can attach to, read and interrupt. Clicking anywhere else on the row still
opens the thing on GitHub. The keys are `o` open, `p` panel-review, `f` fix the selected
issue or plan item, `s` this project's rows or the whole fleet's, `r` refresh, `?` the list,
`q` quit.

**The `⚒` goes through `qb-start` (#371), and therefore inherits its gate.** It used to
compose `claude -- /fix-issue <n>` and hand it to tmux, and what that started was a session
nothing could count: outside `qb-admit`'s in-flight window, holding no claim, and known to
the board only once the agent's own hook got round to saying so. Now it is
`qb-start /fix-issue <n> --via dash`, so the click is counted, claimed before the process
exists, endable by session id, and on the board. The cost is that **on a machine that has
not opted in the button refuses** — which is every machine by default — and the dashboard
asks `qb-start --policy` *before* raising the confirmation, so the refusal arrives instead of
the dialog rather than after it, naming the file and the one line of nix that turns it on.

It does **not** fall back to the old uncounted spawn when the gate says no. That shape is
tempting and wrong three times over: it would make "this machine has not opted in" a fact
about which code path ran rather than about the machine; it would put two behaviours behind
one icon, a counted session on one box and an uncounted one on another with nothing on
screen to say which you got; and it would set the precedent for the next trigger, which will
not have a human behind it. A permission with a fallback is not a permission.

**The `⚖` still starts its review directly**, and that is a placement decision rather than an
oversight: a panel review lands in a *pane of the seat row*, beside the work it is about, and
`qb-start` makes windows. Teaching it where to put a session is a bigger change than #371,
and the `⚒` is where the loop needed a beginning.

**The plans panel is the one that says what the work is FOR.** FLEET says who is here and
CLAIMED says what they hold; neither answers why. `PLANS` is the board's plan — every repo's
ordered list, plus the fleet-wide one — **in the board's own order**, which is the point of a
plan and a human decision. The panel used to re-band it locally (running, then free, then
blocked) and that was a second answer about an ordered list computed against that list's own
order, which is how this pane and `/plan/view` came to disagree about what was next. What the
banding was reaching for, the board sends outright: `next` is the first item that is open,
unclaimed, unblocked and inside nobody else's held plan, it is named in the title and its row
wears a filled `◉`.

A row shows `▶` running with its holder, `▷` inside a plan somebody else holds, `⊘` blocked
with what it waits on, `◉` the board's own pick, or `○` free with how long it has sat — the
`▷` because an item covered by another agent's plan claim is not free work, and showing it as
free is the outcome `covered_by` exists to prevent. Beside the glyph it carries its **rank**,
marked `~12` where that position is merely where the add landed and nobody chose it (#183),
and its **ref** as `#78` for an issue or `PR#78` for a pull request — the kind used to be
dropped, so a PR-backed row drew a dim `⚒` with nothing saying why the item beside it was
takeable and it was not. The right-hand cell names the holder as `machine/name`, because a
name alone is recycled and two boxes read as one agent; a held row that is also waiting wears
the `⊘` in front of the holder, which is the one combination worth acting on.

The title carries what the board concluded about the whole list, and it goes there rather
than on rows of its own because rows are the scarce thing (#269): the board's own counts,
which separate an item **covered** by a plan claim from one **claimed** outright and know
which are **stale**; `~N unchosen` when part of the order is where the adds happened to land;
what is **next**; and `truncated at N` when the page is not all of it. Clicking a row puts its
plan, its claim note and expiry, its blockers, its rank and who chose it, how long it has been
idle and its own note on the detail line — plus, on the `next` row alone, the board's caveat
about how much that recommendation is worth. That reasoning lives on the board and nowhere
else — a plan item never restates its issue — and it does not fit in a title cell.

The read says which **session** is asking. `GET /plan` resolves "is this inside somebody
else's held plan" by machine when the caller does not say, and a machine here runs several
agents on one token — so a plan the agent in the next pane was holding came back as this
reader's own, and every item of it was drawn free to take.

A plan item that points at an issue carries a `⚒` like an issue row, so the shortest path
from "what is next" to somebody doing it is one click. An item pointing at nothing, or one
somebody already holds, says so rather than swallowing the click. The `⚒` refuses an issue
belonging to a repo this dashboard only watches: `/fix-issue` takes a bare number and reads
the repository off the checkout it runs in, so starting one from the wrong pane would land
that number on whatever issue wears it there. **The `⚖` on a PR row refuses for the same
reason**, and it is the one with more at stake — a panel review spends money, comments on a
public PR and pushes a fix commit to it. That click only became reachable with #209: two
repos sharing a PR number used to crash the panel before either row rendered. A plan claim
is keyed `plan:<uuid>` and an item claim `item:<uuid>`, which is right for a lock and
unreadable on a pane, so CLAIMED resolves each against the board and shows the plan's label
or the item's title instead.

**The issues panel is the one that feeds the fleet.** A seat picks unclaimed work off the
board, so what matters is which issues nobody holds: the free ones sort to the top, and a
held one is greyed and carries its holder's name. That marking is the board's own claims,
joined on the claim key — an issue claim is namespaced `owner/repo#n`, which is the number
`gh issue list` reports. The `⚒` on a held issue used to work anyway, with the confirmation
naming the holder — a session that died leaves its claim standing, and picking that work up
was a thing to warn about rather than forbid. Since #371 it is refused, and the reversal is
the claim's doing rather than a change of mind: the click now *takes* that claim, so
proceeding is `qb-claim` refusing at exit 8, and a dialog whose only possible outcome is no
is worse than the answer. The message names the release that makes the work takeable again
(`qb-release issue <n>`) for the case the warning was written for — and it names its own
source, because `held` is the board's answer from up to one poll ago and a claim released two
seconds back is still on it. `qb-claim` stays the authority: it is what settles the race the
other way, where the panel shows an issue as free and the spawn is refused at exit 8.

The confirmation is deliberate: a panel review costs money, comments on a public PR and
pushes a fix commit, and `/fix-issue` writes a branch and opens a PR — so a stray click in a
78-column pane should not be able to start either. `QB_DASH_CONFIRM=0` for anyone who wants
the single click and means it. `QB_DASH_REPO` says where launched work runs, behind
`--repo <checkout>` and ahead of the dashboard's own cwd.

Adding another verb is three things: an entry in `BINDINGS`, an `action_*` method, and — if
it wants an icon — a column, since a click carries the column it landed in and that is how
one row offers more than one verb.

Adding another ROW has one rule, and it has now been got wrong twice. **A row key must be
unique across every repo and every screen the panel can show**, because `DataTable` answers
a repeat with `DuplicateKey` — which does not degrade the row, it replaces the whole
dashboard with a traceback. A bare issue or PR number is not unique (`qbdata.repo_ref`
builds the `owner/repo#n` that is), and neither is a seat number once a second screen
exists (the pane id is). File the record under the key `add_row` RETURNS rather than the
one you passed: `ClickTable.add_row` suffixes a collision it was not expecting instead of
raising, and a row filed under the wrong key renders fine and does nothing when clicked.

That backstop **degrades; it does not report**, and the two are not the same thing. A row
key is never rendered, so the `~2` is invisible, and in the case it was written for — two
plan items arriving with no `item_id` — two rows is also what correct data looks like. So
the collision is written to the app log (`textual console`, or `self.log` in a test), which
is the only place it can surface at all. Do not read a quiet dashboard as a unique key.

`qb-seats` builds it. A screen is seats across the top, the dash down the right, and the
tape full width along the bottom — the dash reports what is true now, the tape what just
happened, and a screen wants both. `QB_SEATS_DASH` names the command; **set it to the
empty string for a screen with no dash**. The default is the plain `qb-dash` rather than
the nicer clickable `qb-dash-tui`; `QB_SEATS_DASH=qb-dash-tui` opts in. The `DuplicateKey`
crash that used to be the reason for that default is fixed (#208 for the seat rows, #209
for the rest), so what is left is a packaging question rather than a correctness one:
`textual` and `rich` are deliberately outside the ordinary dev install, and a default that
wants them would leave anyone without them looking at a pane that says so. Nothing falls back to the TUI on its
own, not even when `qb-dash` is the one that is missing: with neither installed the pane
holds a shell and a line saying which command to set, rather than the screen quietly
being one pane short.

`QB_SEATS_DASH_SIZE` is its width in columns, default 78 — what the dashboard's own table
wants before it wraps — **and never more than a third of the window**. That ceiling is
the interesting half: a client attaching resizes the window and rescales every pane in the
dash's row, so the width has to be reasserted afterwards rather than at build time, and 78
columns reasserted on a 100-column terminal leaves the two seats 19 columns and one. A
narrow terminal therefore costs dash, not seats, and `qb-seats` says so on stderr when the
clamp bites. This is also the first release where **a screen loses columns by default**:
existing callers get seats a third narrower than before, and `QB_SEATS_DASH=` is how to
have the old screen back.

**A new screen says what its seats are about to spend.** N agents on one shared
subscription is the largest single spending decision this fleet makes, and it is made here,
at a prompt, by a human who is not looking at the dash the caps are drawn on — so
`qb-seats` asks `qb-pace`
for an estimate of *this* screen's seat count and prints it before the first pane exists.
It warns and proceeds, always: the refusal lives one layer down in `qb-seat`, off by
default, for panes with nobody in front of them. Printed here rather than in the panes
because a seat execs its agent moments later and the agent paints over anything printed
before it. `QB_SEATS_PACE=off` silences it, and a `qb-pace` that is missing, broken or slow
costs the note and never the screen.

The width is per-screen state, read from the environment once when the screen is built and
recorded on the pane. So `--add` and the seat bar's ✕ put the dash back to the width *that
screen* asked for — including one set by dragging the border, which a reflow will not
undo — rather than to whatever `QB_SEATS_DASH_SIZE` says in the shell that happened to run
them. `--add` never *creates* a dash: a screen built with `QB_SEATS_DASH=` stays a screen
with no dash until it is rebuilt.

`qb-dash` is a **launcher**, not the dashboard: the dashboard is Python needing `rich`,
`textual` and `mcp_server`, none of which a plain `python3` has, so a shebang would be
rewritten by `patchShebangs` to an interpreter that dies on the first import. It hunts for
one that can, the way `qb-board` does — `QB_DASH_PYTHON` names one outright,
`QUARTERBACK_REPO` points at a checkout whose `mcp/.venv` is built.

Prefer the INSTALLED dash over a checkout, which is why `qb-seats` resolves it that way: a
uv-standalone python has no CA bundle, and a dash running under one reports "board
unreachable" against a board that is up, beside a shell where the same URL works.

This used to be `harness/dev/seats-extras.sh`, which stapled two unlanded worktrees
together for a smoke test and hardcoded both paths. It is gone; the lessons it paid for —
place the dash AFTER `select-layout` and never spread the window afterwards, reassert the
width because attaching redistributes the row — are comments in `qb-seats` and assertions
in `harness/tests/test_qb_seats.py`. The dev script only ever produced the right width
because a human ran it by hand *after* attaching; the reassert is a `window-resized` hook
precisely so that nobody has to.

That hook has to name a `qb-seats` by absolute path, because a `run-shell` in a hook
inherits the tmux *server's* PATH and the server usually predates anything that put this
harness on one. Which copy is not obvious, and getting it wrong is silent: PATH's `qb-seats`
is preferred everywhere else, so mid-rollout the working tree installed a hook pointing at
an *installed* copy with no `--dash-fit` — which exits 2 into a `run-shell -b` that discards
both streams, on every resize, saying nothing. So the copy is asked before the hook goes in:
PATH's if it answers the flag, otherwise the one that is running, and otherwise no hook at
all plus a line on stderr naming what it tried. A screen that does not re-fit is honest; a
hook that fails invisibly is not.

### `qb-pace` — the shared ceiling, read by something other than a bar

Every seat, every panel and every `/fix-issue` on the fleet bills to **one** Claude
subscription, across every machine and every project. That subscription's five-hour and
weekly windows are the only hard ceiling any of this has, and until #275 the dashboard drew
them and nothing read them: the brake was a human watching a progress bar, on a fleet whose
own documentation says a seat is *a pane nobody is watching*.

```
qb-pace                 the verdict, one line
qb-pace --json          the same, for a caller that has to branch on it
qb-pace --estimate 4    …plus what a four-seat job costs and what is left
qb-pace --gate          say it AND carry it in the exit status
```

Four answers, and the fourth is the one that matters most:

- **go** — room, or nothing to pace against. An install authenticating with an API key has
  no subscription caps at all, so it gets `go` and says why — the same rule that makes the
  dash's answer to that state one line fewer rather than an error.
- **slow** / **hold** — the bar's own yellow and red, at 70% and 90%, or sooner when the
  endpoint's `severity` says so. The thresholds are not restated here; the verdict is
  derived from `limit_colour`, because a display and a decision disagreeing about what 88%
  means is exactly the failure nobody can see. **`hold` is a wait, not a stop**: it carries
  `resets_in_s`, and a caller that treats it as terminal has thrown away the only fact that
  makes it survivable.
- **unknown** — the figures could not be obtained at all. Deliberately not `go`, which would
  be a governor reporting clear on an input it never read; and deliberately not `hold`,
  which would let a dropped network park the fleet.

Figures that are merely **old** are a third case and they are not discarded — caps move over
hours, so minutes-old ones are still the right ones to act on. What staleness costs is the
right to say `go`. It does not promote a `slow` into a `hold`: staleness is uncertainty
about the number, and parking work over it would be a claim about the window made on the
strength of the weather.

**Where the number lives: nowhere new.** `pace()` keeps no store. The cap is the usage
endpoint's fact, `fetch_limits()` already holds the only copy there is — one machine-wide
cache behind a three-minute floor, shared by every dash pane — and the verdict reads that.
No second file, no board row, no figure of its own to drift behind the endpoint's back. It
is fleet-scoped because its **source** is, not because something here aggregates it.

`--gate` inverts both halves for a caller that has decided to obey: the verdict goes in the
exit status (**3** hold, **4** unknown, **0** go or slow) and nothing is printed at `go`, so
a caller can relay whatever came out without parsing it. Plain `qb-pace` always exits 0 and
always prints — being told is the whole of the gap this closes, and a command that started
failing in scripts because a window was warm would be a bigger claim than the one being
made. 3 and 4 are separate codes on purpose: a caller may reasonably decide to run on an
unreadable ceiling and may not reasonably decide to run on a spent one.

**`--estimate` states two measured halves and refuses to multiply them.** It prices the job
from the board's own record — `GET /review/stats`, counting only the seats that bill to
*this* subscription, since `codex`, `antigravity` and `pi` bill to OpenAI, a Google account
and OpenRouter — and it prints what the window has left beside it. The third line says
`fit unknown`, because nothing anywhere records how much of a five-hour window a seat-run
actually spends: the board knows tokens, the endpoint knows percent, and no row pairs them.
Sampling the caps either side of a run is what would close that, and it belongs to whatever
drives the run. A fit predicted from a rate nobody measured would arrive in the same
sentence as two real numbers and be believed.

**Who reads it.** `qb-seats` prints the estimate when it builds a screen — N agents on one
subscription is the largest single spending decision the fleet makes, and it is made at a
prompt by a human who is not looking at the dash. It warns and proceeds, always. `qb-seat`
carries the refusal, and it is **off by default**: `QB_SEAT_PACE=obey` is for panes with
nobody in front of them, and at `hold` such a seat does not start, says when the window
comes back, and exits 4. `unknown` never stops a seat under either mode — refusing every
seat on the fleet because a laptop dropped its network is a far larger claim than this is
making — but it is always said.

**What it deliberately does not do.** It does not throttle, park, resume, or choose work.
Turning a `slow` into thinner rounds needs dials that can be moved at runtime, which is
#276; a ceiling this repo sets for *itself* is #55; what to run next is #232/#227. This
answers "how does the fleet stand", and the value of having it as a function rather than a
colour is that the answer stops needing somebody to be looking.

### `qb-reconcile` — does the plan still describe the present?

`plan_read` computes one answer, `next`, and every agent that starts cold acts on it.
Nothing checked it against reality. On 2026-08-20 ranks 2 and 4 of this repo's plan pointed
at PRs #182 and #211 — **both merged ninety minutes earlier** — and `next` returned rank 2:
finished work, offered as the thing to do. Beside it sat `idle_days: 0.0, stale: false`,
because staleness measures time-since-touched and not agreement-with-reality. **An item can
be wrong and fresh at the same time**, and nothing on the board could tell.

Every input needed to catch that was already there. Plan items carry
`ref: {kind: pr, value: "182"}`; `GET /reviews` carries `pr_state`, `head_sha`, `ci_status`
and `stop_reason` across every recorded run. Nothing joined them. So:

```bash
qb-reconcile                     # every repo the board's plan names
qb-reconcile --repo owner/name   # just that one
qb-reconcile --json              # the whole report, unknowns beside the findings
qb-reconcile --post              # put the report on the board — when it CHANGED, or aged out
qb-reconcile --include-drafts    # count draft PRs as untracked work too
qb-reconcile --quiet             # say nothing when there is nothing to say
```

**`--post` posts what is new, or what has gone unheard.** It hashes what the report
*says* — the conditions, subjects and sentences, not `idle_days` or GitHub's
`updatedAt`, which move on their own — and keeps that digest, with the time it was
posted, under `$XDG_STATE_HOME/qb-reconcile`. On a 15-minute timer with no such check,
one unchanged disagreement is ~96 identical `finding` posts a day, each carrying the
whole rendered report in `detail`; `finding` is not in the board's `MUTED_TYPES`, so
every one of them lands in every agent's orient read — the volume problem that list
exists to solve.

**But "changed" alone is not the test, because "posted once, ever" is not the same as
"not spam".** `GET /board` orients over a 30-minute window by default, so half an hour
after that single post the disagreement is invisible to every subsequent cold orient —
which is exactly the reader `--post` exists for. So an unchanged report is re-posted
once it is older than `REPOST_AFTER` (4 hours): 6 posts a day for a disagreement that
never changes, and a bound on how long a live one can go unseen rather than the
possibility removed. An unreadable digest, or one written before the timestamp existed,
posts rather than staying quiet — silencing a disagreement because a cache could not be
read is the wrong way round.

Because those sentences are what is hashed, **a `summary` or a `reason` must not carry a
value that moves on its own**: interpolating a claim's `expires` into one defeats the
digest through the field it trusts, since `/plan` re-issues that timestamp every time
the claim is renewed.

**Bot PRs and drafts are not untracked work.** The harness ships a whole loop for
dependabot's PRs (`loops/lander.py`), and those are deliberately never on the plan —
so counting them would make every repo with dependabot enabled a page of findings
that are already owned. Drafts are opt-in for the same reason: a draft is not yet
work the plan owes an item. Neither is dropped silently: the report says how many it
did not compare and why, because "no untracked PRs" and "no untracked PRs among the
ones I looked at" are different sentences — and a tick whose *only* content is skipped
PRs still prints under `--quiet` and still posts under `--post`, or that promise would
not hold in the one configuration the shipped timer unit runs.

**What accounts for a PR is an item's ref or its title, never its note.** A ref and a
title say what an item IS; a note is prose about the work, and prose mentions a PR
without owning it all the time — "follows PR #999", "blocked until PR #247 lands".
Reading those as ownership marks a PR tracked forever and `untracked_pr` then goes
permanently silent about work nothing on the plan is doing, which is a false negative
on the one condition whose whole job is finding unaccounted-for work. A PR genuinely
owned by an issue-backed item is reached through the issue leg instead. And **every row
the plan has can account for a PR, not only its open ones**: the repo scope is drawn
from the whole plan so a repo whose work is all finished still gets its open PRs read,
and if only open rows could account for them, every PR in such a repo would be a
standing finding.

It walks the plan's refs against GitHub and the board's own review record and reports five
disagreements:

| condition | what it means |
|---|---|
| `done_candidate` | item open, its work merged or closed-as-completed |
| `dropped_candidate` | item open, its work closed unmerged or not-planned |
| `stale_claim` | item claimed, but the claim does not describe the present |
| `note_contradicted` | the item's note asserts a readiness `/review/findings` denies |
| `untracked_pr` | an open PR no open plan item accounts for |

**No agent, no claims, no hooks.** It resolves refs, compares, prints and exits. It never
edits the plan: "this item looks done" is a candidate for a human or a `plan_done` call, not
a state transition to make behind their back — and `dropped` in particular is a *decision*,
which is why the plan's model keeps it apart from `done`. The only write it can make is one
board post, and only when asked.

**Ref kind is not one of the conditions.** The first two are "the item outlived its work"
and "the work was abandoned"; whether that work is spelled as a PR or an issue is only how
it is looked up. Nine of the fourteen items on this plan carry `issue` refs, so a pass that
read PR refs alone would be silent on two thirds of it while reporting that it had checked
the plan.

**A claim is checked by its session, not by its holder.** Passive expiry covers a holder
that *died* — it stops renewing and the row lapses with nobody reaping it. It does not cover
the holder still being there while the conversation that took the claim is gone: a `/new`
resets the conversation, the seat identity and its claims are pinned to the pane, and the
lifecycle hook renews the lease on every prompt whatever the new conversation is about. The
claim then looks maximally fresh *because* the agent is busy — with something else — and it
cannot lapse while the pane lives. A claim naming no session (one taken by hand) can only be
checked by holder name, and names are recycled when an agent finishes, so that case is
reported as **unchecked** rather than as healthy.

**And an absent lease is not evidence that a claim is dead, because the two TTLs are not the
same length.** A plan claim runs an hour; a lease runs 30 minutes on this board (300s by API
default) and is renewed by the lifecycle hook per *prompt*, and `/active` lists only leases
that have not expired. So an agent in a single long autonomous turn — the normal shape of the
loops this harness drives — drops out of `/active` for up to half an hour with its claim
perfectly live, and nothing in the payload tells "quiet" from "gone". Only one case is a
finding: the holder is demonstrably live and the session that took the claim is not, which is
the case passive expiry can never reach. When *nothing* the claim names is in `/active`, the
claim's own `expires` is what can still be read, and while it holds this is reported as
**unchecked** — the board's own passive expiry settles it at the claim's TTL, and a finding
accusing a working agent of holding a dead claim every fifteen minutes settles nothing.

**An unmade check never reads as a clean one**, which is the half of #255 that shapes the
whole file. Every condition has a third answer, `unknowns` is never folded into `findings`,
and `complete: false` says so in the JSON. This is not hypothetical: the deployed board is
v2.48 and its `/review/findings` returns no `cycles` field, so its `stopped` cannot be
attributed to one cycle — and the pass says exactly that instead of reading the field
anyway. The exit code carries the same distinction:

```
0   ran, every check completed (a disagreement is the report, not an error)
1   ran, but at least one check could not be made
2   could not run at all: no board, no `gh`, or bad arguments
```

Run it on a timer with
[`loops/systemd/qb-reconcile.{service,timer}`](loops/systemd/) — reference units, like the
lander's. There is no `--execute` to graduate to, because there is nothing for it to do.

`--json` is what #232's orderer reads: an orderer cannot order a plan that does not describe
the present, which is why this is the deterministic half of that issue in its cheapest form.

### `qb-doctor` — is this host wired up, and can work land from it?

Three questions nothing else on the box can answer, in one report.

**The first is the one #204 was filed for.** Three independently versioned things have to
agree — the board image, the harness on PATH, the Python client's venv — and no component
can compare them, because each one can only see itself. So each one's staleness surfaces as
a *different* one's error. In the order they were actually hit: `qb-dash` said
`● board unreachable` about a board that was up (the image was six days old and had no
`/claims` route); `qb-board` said "build a venv" about a venv that was fine (the checkout
was 269 commits behind); the documented repair for that broke the MCP server, because the
two documented commands install different halves of the package; and then `qb-board` said
"this machine has no token" about a valid token. Four symptoms, none of which named its
cause.

**The second is whether the harness's own guards are INSTALLED.** On 2026-08-22 four
mechanisms were found in one morning that had each been written, tested, documented and
shipped — and wired up on no machine. The `qb-reconcile` timer was in-repo and on no host,
so the board's plan went 39% wrong: 13 of 33 items pointing at closed issues. The
`reference-transaction` stash guard was in `harness/githooks/` and in no repo, so an agent
popped another worktree's work. `HUMAN_EDGE_SECRET` was documented in `DEPLOY.md` with a
checklist and never deployed, so every human-only endpoint has 403'd since v2.39. Not one of
them announced itself.

**The third is whether work can actually LAND** (#406, and the `landing` group #407 fills
out). The first two are both about this machine, and on the night #406 was filed they were
both satisfied: 9 ok, 1 unknown, nothing broken. In the same minute the merge queue held
seven green pull requests with none ready, main had not moved in three hours,
`refs/tags/v3.8` pointed at a commit that is not in main's history, and three branches were
conflicting on the one file `changelog.d/` exists to keep them out of. Every row was correct
and not one of them was about the pipeline the work has to travel down.

```bash
qb-doctor                     # every check against this host
qb-doctor --fix               # also run the installers that are safe to re-run
qb-doctor --json              # the whole report as JSON
qb-doctor --repo DIR          # check that checkout instead of the cwd's
qb-doctor --only hooks,edge   # just those rows
qb-doctor --only landing      # or a whole group of them
qb-doctor --human-url URL     # the browser vhost, for the edge check
qb-doctor --quiet             # only the rows that are not ok
```

```
checkout   ~/source/quarterback            main @ b7c86be, 0 behind origin/main              ok
board      https://qb.fo.ls                2.77.0, matching this checkout                    ok
harness    …quarterback-harness-0.1.0/bin  behind this checkout: 25 differ                 FAIL
client     …/mcp/.venv/bin/python          importable, from ~/source/quarterback/mcp         ok
token      https://qb.fo.ls                resolved — this session is zeus                   ok
hooks      …/.git/qb-hooks                 installed (qb-hook-forward, reference-transaction) ok
stash      refs/stash                      guard active, 4 pre-guard entries remain        warn
reconcile  qb-reconcile.timer              enabled, active, last run 12:26:55                ok
edge       https://quarterback.fo.ls       302 — forward-auth, and this host has no session   ?
tools      PATH                            git, gh, curl, jq present, gh authenticated       ok
merges     ~/source/quarterback            nothing here reserves a tag at push time (#122)    ok
```

Rows are grouped by which question they answer, and `--only` takes a group name as well as a
row name. `host` is the first two questions; `landing` is the third. The group is what #407
extends — it should be adding rows to a category, not retrofitting one around a single
check.

#### Look where the mechanism runs, not where its source lives

`ls harness/githooks/reference-transaction` succeeds on a host with **no guard installed at
all** — the file is in git, which is precisely what all four of the above had going for
them. So `hooks` reads `core.hooksPath` and the files under the common git dir, `reconcile`
asks `systemctl --user`, and `edge` makes a live request. Nothing here is satisfied by a
path existing in the checkout.

#### Four verdicts, because `unknown` is not `ok`

| verdict | meaning |
|---|---|
| `ok` | the check ran and the answer is good |
| `warn` | the check ran, the mechanism works, and it carries residue no installer can clear |
| `FAIL` | the check ran and the answer is bad |
| `?` | **the check could not be made** — and why |

`unknown` is the one that matters, and it is the reason this tool exists rather than a
shell alias. Three of the six symptoms on #204 are a check that could not run being reported
as a check that passed: `prune-worktrees` calling a skipped database scan `Nothing to prune.
Clean.` over a 13 MB orphan, `worktree-holder`'s exit 4 that `/tree-shake` proceeded on, and
`qb-reconcile`'s `stopped` that the deployed board could not attribute. #324 settled the
same argument for CI results a day before this landed. **A doctor that prints `ok` because
it could not look is worse than one that does not check**, because it launders ignorance
into assurance — and unlike a stale layer, a false green never announces itself later.

The exit code keeps the same distinction: `0` healthy, `1` at least one check could not be
made, `2` at least one failed, `3` it could not run at all.

#### `warn` is for residue an installer cannot undo

Installing the stash guard does not drain the entries pushed before it — the hook
deliberately allows *deletions*, so old entries can still be cleared by hand. A freshly
guarded repo is therefore protected going forward and still carries its old landmines, which
is exactly the state this repo was in for weeks. `guard active, 4 pre-guard entries remain`
is the report; a bare `ok` is not.

#### `--fix` runs the safe half and prints the rest

Each check carries at most one of two things, and they are separate fields so that nothing
can run a command the author did not mark runnable:

- **an installer** — idempotent, needing no secret, safe against a host that is already
  correct. `qb-hooks install` and `systemctl --user enable --now qb-reconcile.timer` are
  the two today. `--fix` runs these, then **re-checks the whole report** rather than
  trusting the installer's exit code: "the command succeeded" and "the guard is now
  installed" are the two sentences this tool exists to keep apart. It re-checks every
  selected row, not just the fixed one, because one guard's state is another row's input.
- **a command for a person** — printed, never executed. The edge secret needs a value
  generated into 1Password *and* sops and two separate deploys; a tool that tried would
  fail halfway through somebody's production deploy. What `qb-doctor` owes there is the
  precise remedy and the runbook path, which is what it prints.

#### The `merges` row — a squash merge orphans a tag reserved at push time (#406)

A tag reserved at **push** time names the commit that was pushed, and a squash discards
it: the squash collapses the branch into a fresh commit, so the tag ends up addressing a
commit that is not in the history it claims to tag. `v3.8` landed that way while every
other pull request that night used a merge commit, and the CI job called `every release on
main has a tag` stayed green — it checked that a tag of that *name* resolved, and `v3.8`
did. Rebase-merge has the identical defect for the identical reason, so both are checked.

**The row asks whether anything here reserves — not whether a file exists.** As it first
landed it asked whether `scripts/release_tag.py` was present, and #122 removed push-time
reservation from this repo twelve hours later while leaving that file in place with
`backfill`, `taken` and `check`. The row went on firing and reporting `ok` for a reason
that had stopped being true, and its `FAIL` text would have told a reader to switch off
squash merges to protect a reservation nothing takes. A check that is right by accident is
a check nobody notices going wrong.

So the predicate is two questions with one answer — does the repo's tag allocator expose a
`reserve` subcommand, and does the hook git actually runs on a push carry a reservation
step:

```
merges  prisonblues/selfhost  this repo allows rebase and squash merges, and              FAIL
                              scripts/release_tag.py exposes a reserve subcommand — a
                              rewriting merge discards the commit the tag was reserved
                              against (#406, v3.8)
        -> gh api -X PATCH repos/prisonblues/selfhost -F allow_squash_merge=false …
```

That keeps the row **useful rather than deleted**. The harness installs into repos that are
not quarterback, and one still carrying a pre-#122 `release_tag.py` has the original defect
exactly as written — this finds that repo and stays quiet here:

```
merges  ~/source/quarterback  nothing here reserves a release tag at push time, so a        ok
                              rewriting merge has no reservation to orphan
```

That verdict stops where the evidence does. *Where* the release number is applied instead
is a true and useful sentence, and it is not one this row established — saying it here
would be the second premise, arriving in the prose rather than in the predicate.

**It reads; it never runs.** Asking `release_tag.py --help` would answer more exactly than
any parse can, and it would mean a diagnostic executing an unreviewed program out of
whatever checkout somebody typed it in — the line `load_site_config` already draws about
the site config.

Reading has to be done properly to be worth anything, and the whole cost of not executing
is paid in saying so when it could not be:

- A Python tagger's command set is enumerated **out of the parse tree**, never searched for
  in the text. A docstring, a comment or a string constant can all carry
  `add_parser("reserve")`, and this repo's own tagger opens with a paragraph about the
  `reserve` it no longer has. The contract is that a set means *these are the subcommands*
  — so a name spelled with a variable, a subparser handed to another module to fill in, or
  a CLI in a shape this does not recognise all come back as *not enumerated*, and the row
  says `?`.
- A tagger that is **not** Python is a wrapper around something, and its command set is
  wherever that something is. Naming `reserve` is evidence; not naming it is not evidence
  of the opposite, so that case is `?` too.
- The hook read is **the one git would run** — `core.hooksPath` when set, resolved against
  the worktree git runs hooks from when it is relative, and the common git dir's `hooks/`
  otherwise. Whatever that hook chains to is read as well: `qb-hooks` installs a
  `pre-push.delegate` whenever the machine already had a hook to keep running, and a
  reservation performed there happens on exactly the pushes this row is about. A delegate
  spelled with a variable, or a runner like husky or lefthook that keeps its configuration
  somewhere this does not read, is `?` rather than a pass.
- Hook text is reduced to the words a shell would run — full-line comments, heredoc bodies,
  quoted spans and trailing comments all removed — before the word is looked for. A false
  positive here is not a wasted question: it is a `FAIL` recommending a change to a setting
  every contributor and every other machine in the fleet shares.
- "The key is not set" and "the configuration could not be read" are different answers, and
  `git config` gives them different exit codes. Collapsed together, an unparsable include
  reads as *no tagger configured here* and this would answer confidently about the
  conventional filename instead.

**It does not grow a second premise.** Whether the release number is applied in the right
*place* is #122's question and belongs to #122's rows; whether a squash should discard a
branch's reviewed history at all is a policy about review rather than about tags, and wants
its own row and its own argument. One row with two reasons to fire is how this one drifted.

**It detects; it does not set.** Every `fix` this tool runs writes to *this host* — a hooks
directory, a systemd unit, a venv. This one would write a setting every contributor and
every other machine in the fleet shares, from whichever checkout somebody happened to type
`--fix` in, and it needs repository-admin rights a read-only token does not have. So it is a
`manual`, like the edge secret: the exact one-line `gh api -X PATCH` is printed and a person
runs it.

**And it says `unknown` rather than `ok` when it cannot look.** GitHub returns the
merge-strategy fields *only* to a token with push access; a read-only one gets three nulls
and no explanation, which read as `false` would report "merge commits only" about a repo
nobody here can see the settings of. A non-GitHub remote, an absent `gh`, an unauthenticated
`gh` and a null field are each their own `?` with their own remedy.

#### The edge row, and why it currently says `?`

`app/auth.py`'s `human()` needs an edge-asserted `Remote-User` **and** the edge's own
`X-Edge-Auth` secret. That is one boundary with two halves, so it is one row:

- The **agent** vhost must refuse a `Remote-User` a caller supplied itself. Checked first,
  because if it ever stopped stripping, anything that can reach the board could post as a
  person. This half is checkable from any host and passes today.
- The **browser** vhost must accept the person it authenticated. This is the half broken
  since v2.39, and it is not visible from a machine with no forward-auth session: a `302` to
  the auth portal is `?`, not `ok`. Set `QUARTERBACK_HUMAN_URL` so the row has somewhere to
  ask, and `QUARTERBACK_EDGE_COOKIE` if you have a signed-in session; a `403` from that
  request is the `FAIL` that says the secret is unset or disagrees between its two stores,
  which is the only end-to-end detector there is, because nothing compares the stores.

**A `403` is only that `FAIL` when the *board* sent it.** The auth proxy answers `401`/`403`
to a caller with no session, and so does the app to a person the edge did not vouch for —
identical codes, opposite diagnoses. So the body decides: `app/auth.py`'s refusal names
`Remote-User`, `X-Edge-Auth` and `HUMAN_EDGE_SECRET` in full (deliberately, so an operator
is not taught the wrong fix), and a refusal that does not look like that is a refusal this
tool cannot attribute — `?`, with the proxy named as the likely author.

Deliberately **not** done by asking the app whether the secret is configured: a new endpoint
would be absent from the deployed image until the very redeploy that fixes the secret — a
check that cannot answer until after the problem is solved.

#### What the version rows compare

- **board** — `GET /openapi.json`'s `.info.version` against `app/main.py`'s in this
  checkout. Not a workaround: `CHANGELOG.md` opens by *defining* the board's version as that
  field, and it is the same string `app/main.py` declares, so both sides are one fact
  measured in two places. #199 would make it a first-class `/version` and make this cheaper,
  not truer. A board that will not serve its metadata falls back to capability probing
  (`/review-queue` answering `405` means ≥ v2.75) — which answers with a **range**, and a
  range is not a version, so that path reports `?` with the floor as context and never a
  pass.
- **harness** — the files on PATH against this checkout's `harness/bin`, byte for byte
  except for what the packaging rewrites. Content rather than a version, because the nix
  flake pin is what versions the harness, nothing bumps that pin automatically (#267), and
  no harness script can tell you it is stale because each one only knows its own store path.
  Drift is reported in one direction: a file the *install* has and the checkout does not is
  simply a harness newer than your branch. Two rewrites are undone before the comparison
  (#353): `postFixup`'s `patchShebangs`, which replaces every script's first line with a
  store path on purpose, so a shebang-only difference is not drift; and `postInstall`'s
  `wrapProgram`, which renames a script to `.<name>-wrapped` and generates a new one at its
  name, so the wrapped file is what gets compared. Counting either made this row report 26
  differing files on a host that had *just* rebuilt, with the one real finding buried among
  them — and a row that is always red is a row nobody reads.
- **client** — `mcp/.venv` exists, `mcp_server.server` and `mcp_server.board` both import,
  **and** the editable install resolves to this checkout. All three, because "importable"
  says nothing about which source tree answered (#203), and the documented repair for that
  installs a different half of the package (#200).

### `qb-bump` — the thing that acts on a stale harness (#267)

`qb-doctor` says the harness on PATH is behind this checkout. Nothing used to do anything
about it. On 2026-08-22 sixteen releases landed and the harness half of every one of them
reached this fleet's desktop only when a person remembered to run `nix flake update` by
hand: the pin was 162 commits behind at 09:00 (eleven binaries simply absent from PATH,
including the `qb-reconcile` whose systemd units had therefore never been installable), was
bumped at 10:19, and was stale again by 14:00.

```bash
qb-bump                     # stale? prepare the bump, BUILD it, propose it
qb-bump --json              # the same answer as a document
qb-bump --apply             # what a PERSON runs: install the prepared lock and switch
qb-bump --apply --dry-run   # print that command rather than running it

#   exit 0  nothing to carry — the harness on PATH is this checkout's
#   exit 1  cannot tell — no qb-doctor, no nix, no consuming flake, or more than one
#   exit 2  a bump is prepared and BUILT; one command by a person finishes it
#   exit 4  the bump does not build — refused to propose it
```

**It does not detect anything.** The drift verdict is `qb-doctor --json --only harness`,
read and not re-derived. A second comparison here would be a second opinion about a fact
that already has one, and the two would disagree the day one of them learned something —
`_same_after_packaging`'s handling of `patchShebangs` and `wrapProgram` is exactly the kind
of thing only one of two copies ever gets right. The suite asserts this behaviourally: a
stub doctor reporting `ok` is believed, whatever any other measurement of those directories
would say.

**The ceiling is `sudo`, and it is designed around rather than fought.** A
`nixos-rebuild switch` needs root. An agent has no root and should not go looking for it, so
this stops one step short of it — prepare, build, prove, and hand a person one command. That
is the ten minutes; the `sudo` is the ten seconds. `--apply` refuses without a terminal, so
a timer, a CI job or an agent that invokes it changes nothing and prints the command instead.

**A proposal that has not been built is a proposal to break somebody's machine.** The first
bump on 2026-08-22 failed: quarterback's module had started installing
`~/.claude/quarterback-workflow.md` while the consuming flake still declared its own copy,
and two definitions of one `home.file` path is an eval error, not a merge. So the build is
not a nicety — a bump that does not build is **refused** and announced as a refusal, with
the error, rather than proposed.

**It never commits in the consumer, and never reads its uncommitted work.** Preparation runs
on `git archive HEAD` unpacked into a temporary directory, so a consumer's dirty working
tree — a half-edited secrets module — can neither be built here nor swept into anything. The
only file this ever writes into a consumer's checkout is `flake.lock`, only under `--apply`,
and only when a person typed the command. It is left *modified*, never committed: what that
lock means for a fleet's history is the consumer's business.

#### Finding the flake that consumes this harness

That flake is a third thing — not this repo, not the store path — which is why `qb-doctor`
compares content rather than pins: it cannot find it. `qb-bump` asks four questions in order
and refuses rather than guessing:

1. `--flake DIR`.
2. `$QUARTERBACK_CONSUMER_FLAKE`.
3. `QUARTERBACK_CONSUMER_FLAKE` in `~/.config/quarterback/config` — which
   `programs.quarterback-harness.consumer.flake` writes, and is how a fleet says this once.
4. A scan of `~/source` and `~`, one level deep, for a `flake.lock` that pins this repo.
   Override the roots with `QUARTERBACK_CONSUMER_ROOTS` (colon-separated) — that is also how
   a host with its flake at `/etc/nixos` says so.

A lock "pins this repo" only when one of the **root node's own inputs** does. `flake.lock`
carries the whole dependency graph, so a flake that pins something that pins this repo is not
a consumer of this harness — and `nix flake update` could not move that node anyway. The name
passed to `nix flake update` is the root's name for the input, which is not always the id the
lock stores it under (a second node for one flake becomes `quarterback_2`, and
`nix flake update quarterback_2` is not a command).

A hit is collapsed onto its **main checkout** before being counted. Eight directories under
`~/source` pinned this repo on the machine this was written for, and they were eight
worktrees of one flake; they are not eight consumers, because a machine is rebuilt from the
checkout and a bump prepared into a feature branch's worktree lands in somebody's in-flight
work. Two genuinely different flakes still refuse, by name: picking the first would prepare
a bump against a directory nobody rebuilds from, which looks exactly like a good proposal
and does nothing at all.

**The system attribute is not the hostname.** This fleet's `zeus` is
`nixosConfigurations.desktop`. So the hostname is a first guess checked against the flake's
own attribute names, and the fallback is to ask each configuration what `networking.hostName`
it declares — correct, slow, and defeated by a host that does not evaluate here at all, which
is what `--host` and `programs.quarterback-harness.consumer.attr` are for.

#### What `--apply` refuses, and why each one

The proposal is a claim that *one particular system* was built. Every refusal below is the
same sentence in a different place: what would be switched onto is not what was proven.

- **Nothing prepared** — run `qb-bump` first.
- **No terminal** — the next thing it does is ask for a password, so a timer, a CI job or an
  agent gets the command printed and nothing else.
- **The consumer's `flake.lock` moved** since the build. Re-preparing costs minutes.
- **The cached lock is not the one that was built** — a second `qb-bump` wrote the cache
  between the build and the apply, or a write was interrupted.
- **The consumer has committed** since the build. `nixos-rebuild --flake <dir>` builds that
  directory as it is now, so a commit landing afterwards means the proof is about a tree that
  no longer exists.

Two things are *said* rather than refused. Modified files in the consumer — the switch builds
the working tree while the proof was of `HEAD`, and refusing over an uncommitted module would
make the tool useless on the machine it was written for, so the count and the first few names
are printed before the command. And a **later bump that was refused**: an earlier proposal
that did build is still worth applying, so it is applied, having said that today's is broken.

Preparation refuses one thing of its own: a consumer whose `flake.lock` is *already*
uncommitted. Preparation builds `HEAD` plus one moved input, so applying the result would
discard whatever lock change was in flight — and reading it instead is exactly what this file
promises not to do.

#### What a person is told, and where

Through #274's door and in #279's vocabulary: `needs_human.announce`, class `environment` —
nothing here needs taste or a design decision, it needs root on a particular machine. Not a
board post written by this script, because a second spelling of "a human must do this" is two
places to watch and a person watching neither. The board post carries the drift, the two
revisions, the store path that was built and the one command; the same thing is printed
locally whether or not the board took it, because an escalation that cannot be announced is
still an escalation.

```
stale: behind this checkout: 5 absent (check-db-isolation, qb-admit, qb-release, qb-start,
       +1 more), 5 differ (create-worktree, prune-worktrees, qb-doctor, qb-hooks, +1 more)

consumer   /home/rich/source/nix-fleet (found by scanning /home/rich/source)
input      quarterback
pin        b35de2a5e638 -> eac457b385ff
built      desktop: /nix/store/…-nixos-system-zeus-26.05.20260707.0ad6f47

10 scripts arrive on PATH when a person runs:
  /home/rich/source/quarterback/harness/bin/qb-bump --apply

That writes the prepared flake.lock into the consumer and runs `nixos-rebuild switch`, which
needs a password; nothing above it did.
```

That path rather than the bare name is deliberate and is the normal case: the harness
carrying `qb-bump` is by definition the one **not** installed on the host it is diagnosing,
so on the first run `qb-bump --apply` would be a `command not found`. When the name resolves
to the file that just ran, the name is what gets printed.


## How it works

- **Layout.** A worktree is a *sibling* of the main checkout: `../<project>-<branch>`, with
  `/` in the branch name flattened to `-`. So `fix/issue-42` in `~/src/myapp` becomes
  `~/src/myapp-fix-issue-42`.
- **Ports.** Allocated from `base_port` upward and recorded in `.worktree-ports` in the main
  checkout. A port already bound is skipped even if the file doesn't know about it.
- **Database.** An isolated copy is named `<project>_<branch>`, cloned from the main
  database, with the worktree's `.env` rewritten to point at it. `create-worktree` has a
  safety net that shouts if any `.env` var still names the main database after rewriting —
  if you see that warning, stop, because a migration would hit shared data.
- **Shared local state.** `.venv`, `.claude` and `CLAUDE.md` are symlinked back to the main
  checkout so the worktree is immediately usable. Git-*tracked* directories are handled
  carefully rather than symlinked wholesale — see the comments in `create-worktree`, which
  record two real incidents that motivated the defensive code.
- **Session markers.** `/fix-issue` writes the worktree path to
  `~/.cache/claude-code/session-cwd/$CLAUDE_CODE_SESSION_ID`, and the PR number to
  `~/.cache/claude-code/session-pr/$CLAUDE_CODE_SESSION_ID`. The first exists because the
  shell cwd resets between tool calls, so a plain `cd` does not stick; it is how the
  statusline and `/drop-worktree` know which worktree a session owns. The second exists
  because the branch names the *issue*, not the PR, so the bar could not otherwise say
  which PR a session is on; the statusline falls back to a cached `gh pr list --head` when
  it is absent. **Write both with `tee`, never `>`** — a `>` redirect anywhere under
  `$HOME` is refused by the `dcg` pre-tool guard, and an agent that hits that block leaves
  the bar pointing at the main checkout for the whole session.
- **Workflow stage.** `qb-stage <stage>` records how far along the work is — in
  `~/.cache/claude-code/session-stage/$CLAUDE_CODE_SESSION_ID` for the bar, and on the
  session's quarterback lease for everybody else:

  | Stage | Means | Written by |
  |---|---|---|
  | `F0` | implementing the first cut | `/fix-issue` |
  | `R1` | review round 1 | `/review-pr`, `/panel`, `/panel-review-pr` |
  | `R1F` | fixing round 1's findings | `/panel-review-pr`'s fix fan-out |
  | `R2`, `R2F`, … | and so on, per round | `/panel-review-pr` |

  Repo, branch and PR all say *which* work a session is on and none of them say how far
  along it is — they read identically at every stage of a PR's life. Nothing local can
  derive it either: a round number is handed to `panel.py` (`--round <r>`), never computed,
  so it has to be said out loud. `/review-pr` stamps only `R1`, because one agent there
  both reviews and fixes and a bar that claimed `R1F` while a reviewer was still reading
  would be worse than one that said less. `qb-stage` checks the *shape* (1–6 alphanumerics)
  and not the vocabulary, so a new stage needs no edit to it — and it exits 0 in silence
  when there is no session id, because a loop under systemd has nobody watching a bar.

  **And it tells the board** (#262). A marker file answers the question for the pane it
  is written on, which is half of what a fleet is: cross-machine it is not there to read,
  and same-machine nothing read it. So the same call POSTs the stage to `/lease/stage`,
  which puts it on the session's lease — where `/active`, `/overlap`, `/fleet`, `qb-board`,
  `qb-dash` and `qb-dash-tui` all show it — and emits one `status` post on the live stream
  when it *changes*, so a follower hears about a transition rather than polling for it.
  `qb-stage` is the right place to say it because it is the only thing in the system that
  is *told* the stage; the lifecycle hook reading this marker on each heartbeat would need
  no code here and would make a fact the fleet acts on arrive up to a heartbeat late.

  The report is **fail-open and non-blocking**: a backgrounded `curl` with stdin closed and
  its output discarded, with the board config resolved inside that same subshell so a
  `QUARTERBACK_TOKEN_CMD` waiting on a passphrase cannot stall the caller either. An
  unconfigured, unreachable or slow board costs the marker nothing and says nothing —
  telemetry that can fail the thing it reports on is worse than none. Board URL and token
  come from the per-host contract in `qb-env`, read directly rather than sourced, exactly
  as `worktree-holder` reads it; there is no default URL, because guessing one points the
  report at another island's board.

  On the reading end a lease that never reported a stage says so rather than showing a
  blank: `/fleet` calls it `unreported`, in the same vocabulary it already uses for a
  session nobody ended, and the terminal panels use `—` because six columns have no room
  for the word. A stage is 1–6 alphanumerics by construction, so the dash cannot be
  mistaken for one.

`/drop-worktree` clears all three.

## Configuration

Drop a `.worktree.json` in your repo root. Every key is optional — Docker, nginx and the
database are auto-detected, and anything absent is skipped, so a plain library repo gets a
worktree and symlinks and nothing else.

Copy the closest template from `templates/` and edit `project`:

| Template | What you get | Pick it when |
|---|---|---|
| `minimal.worktree.json` | Worktree, symlinks, port | No database, or one you're happy to share |
| `postgres-no-docker.worktree.json` | The above + an isolated database copy | Your `docker-compose.yml` is tracked in git, or you run the app directly |
| `postgres-docker-nginx.worktree.json` | The above + per-worktree containers behind an nginx sub-path | Compose is untracked and you want each branch reachable at a URL |

`worktree.example.json` documents every key in one annotated file; quarterback's own
`.worktree.json` (repo root) is the live worked example of the middle row. `templates/` also
holds two test files, which are not about worktrees at all: `dbtarget.py`, the test-suite half
of database isolation — see the prerequisites below, because a `.worktree.json` alone does not
get you there — and `test_migrations_self_contained.py`, described after them. Both are run in
quarterback's own suite and pinned byte-identical to the copy you are given.

Keys the script reads: `project`, `framework`, `base_port`, `app_port`,
`docker.{enabled,network_pattern,network_default,image_pattern}`,
`database.{engine,container,url_env,user_env,password_env,name_env}`,
`worker.{type,command,container_prefix,queue_env,queue_default}`,
`nginx.{config,container,main_port,resolver,extra_proxy_headers}`,
`server.{workers_env,workers_default}`, `env.copy_from`, `workspace.{enabled,editor_cli}`,
and the arrays `symlinks`, `copies`, `reserved_names`, `gitignore_additions`.

### Three prerequisites for database isolation

All three are easy to miss, and missing any of them gets you a worktree that *looks*
isolated while running against shared data — or, for the third, no usable worktree at all.

**1. The main checkout needs a `.env`.** It is the file `create-worktree` copies into the
worktree and then rewrites the database name in. There is nothing else for it to derive
credentials from, so with no `.env` the DB step has nothing to copy and says so —
`cp .env.example .env` is part of setting a repo up, not an optional nicety. (A repo that
keeps its env elsewhere can point `env.copy_from` at that file instead.)

**2. That `.env` must actually name the database.** `create-worktree` has to know which
database to copy, and it looks in two places: `database.url_env` (default unset) and
`database.name_env` (default `POSTGRES_DB`). Declaring the first no longer disables the
second — it *cascades*, so a repo whose URL is assembled at runtime, or that keeps the name
in `docker-compose.yml` and only the password in `.env`, resolves through `POSTGRES_DB`.
quarterback itself is that shape: its `.env` carries `POSTGRES_PASSWORD` and nothing else,
so isolated mode cannot work here until `POSTGRES_DB=quarterback` is added to it.

When neither variable is set the run stops at the database step and names both variables and
the file it read. It stops *after* the git worktree exists, so it also says the worktree is
incomplete and gives the two commands out — a directory with a checkout but no `.venv`
symlink, no port and no `CLAUDE.local.md` looks provisioned enough to `cd` into and then
fails later for reasons that have nothing to do with the database. Before this was fixed the
run died on `MAIN_DB_NAME: unbound variable` instead: the guard written to explain the case
was the first thing to dereference the unset variable, so `set -u` killed the script at the
exact line that existed to say what was wrong.

**3. Your test suite must honour that `.env`.** This is the one that bites hardest, because
provisioning succeeds and the damage happens later. A suite that decides its own database
URL — the near-universal

```python
os.environ.setdefault("DATABASE_URL", "postgresql://…/myapp")   # the bug
```

overrides the worktree's isolated database, because config libraries that read `.env`
(pydantic-settings, python-dotenv, django-environ) rank a real environment variable *above*
the file. So the isolated copy sits unused while the suite drops and rebuilds the schema of
the shared one. Nothing in the output mentions it.

`templates/dbtarget.py` is the fix, importable as it ships: copy it into your `tests/`,
change the two constants at the top, and wire it into `conftest.py` with the snippet in its
docstring. It resolves the URL once (explicit env var → the checkout's `.env` → fallback),
assigns it back so subprocesses like `alembic` agree, and refuses outright when a worktree
is about to rebuild a database another checkout is using — the main one or a sibling. It
also prints the target as the first line of the run, `-q` included, so which database is
about to be destroyed is something you read rather than deduce.

quarterback runs the same file as `tests/dbtarget.py`; the two are kept byte-identical below
their constants, and `tests/test_dbtarget.py` runs every scenario against both, so the copy
you are given is the copy that is tested.

One consequence to plan for: once the suite honours `.env` it honours *all* of it, and a
`.env` is developer convenience — dev auth bypasses, debug flags, log paths. Take only the
database target from the environment and pin everything else in `conftest.py`. Doing this to
quarterback surfaced it immediately: `.env.example` sets a browser dev-user that
authenticates every request, which turned "this endpoint 401s without auth" into a test that
opened a live event stream and hung until killed.

### Checking it actually worked

```bash
grep DATABASE_URL ../myapp-fix-issue-42/.env       # should name myapp_fix_issue_42, not myapp
cd ../myapp-fix-issue-42 && pytest --collect-only  # first line states the target database
```

Not piped into `head`: closing the pipe early hands pytest a SIGPIPE partway through, and
`--collect-only` answers the question without running anything destructive.

`create-worktree` also shouts if any `.env` var still equals the main database name after
rewriting. If you see that warning, stop — a migration would hit shared data.

### A migration must not import your application

`templates/test_migrations_self_contained.py` is the second shipped test file, and it is
about a different accident from `dbtarget.py`. Drop it into your `tests/` and adjust the four
constants at the top; it needs no database, no app import and no fixtures, so it runs in
whatever your fast suite is.

**A migration is a frozen artefact; live app code is not.** A migration runs at a fixed point
in schema history, but anything imported from your application package is whatever that
package says today. The sharpest form is an ORM model — an ORM SELECT or INSERT names *every*
mapped column, so the day a later migration adds a column, an older migration starts emitting
SQL for a column that does not exist yet at that point in the chain, and the replay aborts on
`UndefinedColumn`.

It hides, which is why a guard is worth the file. Applied revisions never re-run, so it is
invisible on every database that is already past the offending migration — every developer's,
and production's. It detonates on a **fresh replay**: a new worktree, a CI database built
from empty, a disaster-recovery rebuild, or an instance still behind that revision when the
new-column code deploys.

The guard is an **allowlist** — the standard library plus `alembic`/`sqlalchemy` — and not a
denylist of your app package, because every first-party package carries the identical hazard
and so does any third-party library whose next release changes what a frozen migration does.
It covers both import spellings, at module level and inside a function, and constant-string
`importlib.import_module`/`__import__`, which walk straight through a scan that only looks at
`Import` nodes. Its own negative tests ship with it: a guard nobody has watched fail is not a
guard.

**Adopt it while it is green.** On a repo whose migrations are already self-contained it
costs nothing and every migration written afterwards is one that cannot acquire the problem.
Adopt it late and you owe yourself an audit first. If you find a migration that genuinely
must import something, `EXEMPT_MODULES` pins the **exact** import statements one file may
make — so an exempt migration cannot quietly grow an ORM import, and an exemption that is no
longer used fails the guard rather than rotting into a permanent hole.

The other half of the pair does not ship. quarterback's `tests/test_migration_drift.py`
replays every migration into a throwaway database and diffs the result against its models —
that is what *detects* an app-importing migration, where the guard above *prevents* one — but
it needs a live database, your models' import path and your project's alembic invocation, so
a template of it would be wrong for most repos in at least two of those three. Copy the
shape from that file rather than expecting a drop-in.

---

## How this relates to the board

They ship together, but they are not the same tool, and it is worth being precise about
which answers what:

|  | Worktree tooling | quarterback |
|---|---|---|
| **Question** | How do two agents work at once without destroying each other? | Who is doing what, where, and is my checkout current? |
| **Scope** | One machine | Across machines and agents |
| **Failure it prevents** | Physical collision — files, database, ports, containers | Informational collision — duplicated work, stale checkouts |

### Using the harness alone

If your problem is *"my agent's half-finished refactor means I can't touch anything else"*,
you need worktrees and you do not need a board. quarterback isolates nothing — it will
happily watch two agents overwrite each other and report both. Plenty of people should
install step 2, stop there, and never run the service. That is why the harness degrades
rather than fails when no board is configured, and why this install step comes with no
requirement to do the other one.

The board only starts earning its keep when there is a second agent whose work you cannot
see, or a second machine whose commits you do not have.

### Using them together

Isolation buys safety and immediately creates a new problem: **invisibility**. Once every
agent sits in its own directory, on its own branch, against its own database, nobody can
see anyone. Two agents can spend an afternoon solving the same bug in adjacent directories
and never find out. That gap is the one the board fills, and several of its endpoints exist
specifically because of this workflow:

- **`report_git` / `GET /worktrees` / `find_commit`** — a registry of exactly the worktrees
  `create-worktree` produces, across every device. Same-machine worktrees share a git object
  store, so cherry-picking between them is purely a *discovery* problem: which SHA exists
  where, and what does it do. That is what `find_commit` answers.
- **`GET /active?cwd=`** — "who is live in this directory?" The directories in question are
  worktrees. Ask before you dive in — but ask through `worktree-holder`, not directly: a
  lease carries the dir its agent was *launched* in, so this query finds an agent started
  inside a worktree and misses one handed a worktree mid-session, which is most of them.
  `worktree-holder` unions it with the local session markers to get the whole answer.
- **`GET /overlap` / the `peers` tool** — leases carry repo and branch, so the board can rank
  live peers by subject overlap and point you at the agent already on your problem. Without
  it, worktree isolation means you would never have met.
- **`published` posts and `GET /sync`** — `/fix-issue` pushes a branch and opens a PR. When
  that PR merges, other checkouts silently go stale. The board's publish/staleness advisories
  are what turn "somebody should remember to pull" into something nobody has to remember.

The honest summary: **`create-worktree` makes agents safe to run in parallel; quarterback
makes parallel agents aware of each other.** Adopt the first when one agent is in your way.
Adopt the second when you can no longer tell what the others are doing.

---

## The board client

Everything above is the workflow half. This is the half that makes a machine *appear* on a
board, and until #230 none of it was here: the hook script, the MCP registration and the
seven `settings.json` entries all lived in whatever personal config a consumer happened to
keep. So a host that imported the flake got the slash commands and the loops and **no
board** — no lease, no presence, no ask courier, no overlap detection, no sync advice — while
the flake's own description said otherwise. Worse, the hook was pinned by a different repo
than the board it posts to, which is version skew that `qb-doctor` could not even look at,
because the file was not in the tree it checks.

Five files, one pin:

| file | what it is |
|---|---|
| `bin/qb-hook` | the lifecycle reflexes — presence, lease, session end, publish-on-push, the ask courier, sync advice, sub-agent records. Fired by Claude Code, never by the model: these must not depend on anybody remembering them. Fail-open by contract |
| `bin/qb-env` | the site-config contract — which board, which token. Sourced, not run |
| `bin/qb-mcp` | one stdio MCP server per session, so each agent carries its own identity |
| `bin/qb-claude-setup` | the wiring: merges the hook fragment into `~/.claude/settings.json`, registers the MCP server in `~/.claude.json`, @imports the workflow doc |
| `bin/qb` | what a human types — `qb sessions`, `qb resume <id>` |
| `bin/qb-end` | the stop verb: hand back a session's claims and its lease, saying why (#277). Called by the hook and by the seat bar's ✕; usable by hand |
| `bin/qb-release` | the release verb for ONE claim, named as a resource: what the land step and the worktree teardown hand back (#337) |
| `bin/qb-admit` | is there room in this repo for another unit of work? Reads `in_flight.max` and the board's count; ships unbounded (#337) |
| `bin/qb-start` | the start verb: begin one session on an allowlisted command, counted, claimed and attachable. Off unless a machine's `spawn.json` says otherwise (#277) |
| `bin/qb-status` | is that session alive, finished or gone? The pane's answer and the agent's, and the disagreement between them (#277) |

### Which qb-hook am I running?

```bash
qb-hook --version
# qb-hook /nix/store/…-quarterback-harness-0.1.0/bin/qb-hook
# qb-env  /nix/store/…-quarterback-harness-0.1.0/bin/qb-env
```

Paths, not a version string: on a nix install the store hash **is** the pin, uniquely, while a
`version` field is something somebody has to remember to bump. The two lines matter together —
a `qb-hook` and a `qb-env` from different store paths is a half-migrated install, and it is
invisible from either line alone. This is the fourth layer #204 counts three of.

### Is it actually wired?

```bash
qb-claude-setup --check
# ok       PreToolUse        /nix/store/…/bin/qb-hook PreToolUse
# MISSING  PostToolUse       no qb-hook entry — this host is deaf on that event
# SKEW     Stop              /home/you/.local/bin/qb-hook Stop
```

Per event, because that is the resolution the failure has: the bug this replaced wired three
of seven, which any single yes/no would have reported as "wired". Exit codes are a contract —
**0** all wired here, **1** something missing, **2** all wired but to a different `qb-hook`
than this install's. `2` is not a lesser `1`: it is the state a host is in for the whole of a
migration, and it is the skew this section exists to end.

It reads `~/.claude/settings.json` and nothing else. The MCP registration and the CLAUDE.md
@import are wired by the same run and are deliberately **not** in those exit codes: folding
three answers into one number would leave a doctor unable to tell a deaf host from one
missing a doc. Check those two by looking — `jq .mcpServers.quarterback ~/.claude.json` and
`grep quarterback-workflow ~/.claude/CLAUDE.md`.

### Wiring `~/.claude/settings.json` when you already write to it

That file has several writers — you edit it, Claude Code writes to it, and nix wants to
declare parts of it — so `home.file` cannot own it: a store symlink is read-only and breaks
the app. The wiring is therefore an activation script doing a surgical, idempotent merge, and
it is **additive by identity**: quarterback's own entries are replaced, everybody else's in
the same event are kept. Your `PreToolUse` Bash guard survives having a board installed.

The half a jq expression cannot fix is ordering. The usual spelling of "declare part of
settings.json from nix" is `jq -s '.[0] * .[1]'`, and `*` replaces **arrays** — so a canonical
file of yours that declares `hooks.PreToolUse` will drop quarterback's entry from that one
array if it lands last. Name your entry and the ordering is explicit:

```nix
programs.quarterback-harness.claude.activationAfter = [ "claudeBaseSettings" ];
```

Relying on the DAG's tie-breaking between two entries that merely both come after
`writeBoundary` is not a fix; it is the same bug with a coincidence holding it up. If you
would rather own the file outright, take the fragment instead:

```bash
qb-claude-setup --print-fragment      # the exact JSON the activation would have merged
```

with `claude.enable = false`. One expression produces both, so the manual route cannot drift
from the wired one.

### Migrating off a hand-rolled copy

If you already carry your own `qb-hook`/`qb-claude-setup` (this is where they came from), the
transition is safe in either direction and needs no flag day: the merge matches on the command
*naming* `qb-hook` rather than on an exact path, so your old `~/.local/bin/qb-hook` entries are
**replaced** rather than doubled. A doubled `PostToolUse` entry would run the hot path twice
per tool call and poll the ask courier twice per window, which is the reason that is matched
loosely on purpose. When you drop your copies, drop the `settings.json` hook entries with them
and let the module write them; `qb-claude-setup --check` tells you which pin you are on
meanwhile. One thing to check by hand: an activation attribute called `quarterbackClaude` in
your own config does **not** collide with this module's (`quarterbackClaudeWiring`), but two
definitions of one name would be an eval failure rather than a merge, so keep the names apart.

---

## Installing

Nothing here has a build step — it is bash and standard-library Python. There are two ways
in, and neither requires the board to be running.

### With nix (flake)

The repo root is a flake. As a home-manager consumer:

```nix
{
  inputs.quarterback.url = "github:prisonblues/quarterback";

  # …then in your home-manager configuration:
  imports = [ inputs.quarterback.homeManagerModules.default ];

  programs.quarterback-harness = {
    enable = true;
    board.url = "https://qb.example.org";
    board.tokenCommand = "cat /run/secrets/quarterback-token";
  };
}
```

That links `loops/` to `~/.claude/loops`, every slash command to `~/.claude/commands/`, puts
the scripts on `PATH`, and — because a board was named — renders
`~/.config/quarterback/config` and wires the seven lifecycle hooks and the MCP server at
activation. Presence, leases, the courier and sync advice work from the next session, with no
quarterback-specific lines anywhere else in your config.

| option | default | what it decides |
|---|---|---|
| `commands` | all of them | which slash commands land in `~/.claude/commands`. Narrow it if your host already defines one of the names — home-manager collides rather than picking a winner silently, which is the behaviour you want |
| `installScripts` | `true` | the package on `PATH`. Off takes the loops and commands without the worktree tooling. The board wiring does not depend on it: hooks are wired by store path, so a host can have a working client with nothing on `PATH` |
| `seats.enable` | `false` | adds `tmux`, the one runtime dependency the harness cannot assume. Off because you enable this module on every host and want a seat screen on one |
| `board.url` | `null` | the board this host talks to. **Null means "I render that config myself"**, not "use a default" — there is deliberately no fallback URL, because a self-hosted board has none and a guess points an agent at somebody else's |
| `board.tokenCommand` | `null` | a command printing this machine's bearer. A command, not a path: the token source is what varies per site. Runs on every board call, so keep it cheap |
| `board.tokenRefreshCommand` | `tokenCommand` | used *instead* after a confirmed 401, for the common case where `tokenCommand` reads a cache and re-running it returns the same stale bearer |
| `board.agent` | short hostname | this machine's name on the board — the machine half of a `machine/name` identity |
| `board.repo` | `$HOME/source/quarterback` | a checkout, which `qb-mcp` needs for the MCP server's venv |
| `claude.enable` | `true` | do the wiring at all. Off gives you the fragment to merge yourself |
| `claude.activationAfter` | `[ ]` | activation entries the wiring must run after. Name yours if you also merge into `settings.json` |
| `claude.workflowDoc` | `true` | install `~/.claude/quarterback-workflow.md` and @import it from `~/.claude/CLAUDE.md`, creating that file if you have none. The import is conditional on the doc, so turning this off leaves no dangling @import |
| `claude.registerMcp` | `"auto"` | register `qb-mcp` in `~/.claude.json`. `auto` registers it only when the interpreter it execs exists, because a server that cannot start means every session opens on a failed connection; it self-heals on the next switch. `always` for a host that builds the venv afterwards |

Outside home-manager, `nix build github:prisonblues/quarterback#harness` puts the scripts in
`result/bin` and the rest in `result/share/quarterback-harness`.

### By hand

```bash
install -m 0755 harness/bin/* ~/.local/bin/
cp -r harness/loops ~/.claude/loops
cp harness/commands/*.md ~/.claude/commands/
```

### Requirements

`git`, `jq`, `bash`, and Python 3 (standard library only — the loops import nothing
third-party). `gh` for anything that talks to GitHub, which is most of it. `curl` for
`worktree-holder` and for the board client (without it `worktree-holder` reports "could not
tell" and `qb-hook` no-ops, both silently and on purpose — a coordination board must never be
in the critical path of a tool call). `jq` and `curl` are hard requirements for `qb-hook`
specifically, which checks for both and exits 0 without them. `docker` and a database client
only if your repo uses them.

`qb-board` is the one exception to the stdlib-only rule, and it is an exception in
where the code lives rather than in this directory: the launcher here is bash, and what
it launches is `mcp_server.board` from `mcp/` — a second consumer of the HTTP client the
MCP server already uses, rather than a third client for one board. So it needs an
interpreter that can import that package (`$QUARTERBACK_REPO/mcp/.venv`, a sibling
checkout, or any `python3` you installed it into; `QB_BOARD_PYTHON` overrides), which
means `httpx`. The full-screen client additionally needs Textual, an optional `tui`
extra — `qb-board --follow` does not, so a headless host that only tails the board
installs no TUI framework. Absent either, it says which and how, and the rest of the
harness is unaffected. The reviewer CLIs the panel drives (`claude`,
`codex`, …) are needed only for the reviewers you actually enable — a missing one is
reported as skipped, not fatal.

### Connecting it to a board (optional)

The panel looks for a `qb` CLI to record runs. With none on `PATH`, it no-ops silently and
everything else works unchanged — that stays true even though `qb` now ships here (#230),
because `installScripts = false` is a supported way to take the loops alone. Point it at
your board to light up `GET /review/stats` and the board's `/panel` page.

One site config, read by everything: `QUARTERBACK_BASE_URL` and `QUARTERBACK_TOKEN_CMD` (plus
the optional `QUARTERBACK_TOKEN_REFRESH_CMD`, `QUARTERBACK_AGENT` and `QUARTERBACK_REPO`) from
`${XDG_CONFIG_HOME:-~/.config}/quarterback/config`, each overridable from the environment.
`bin/qb-env` is the contract and the loader; `qb`, `qb-hook` and `qb-mcp` source it, while
`worktree-holder` and `qb-board` read the same two variables directly, so the occupancy check
and the board client work whether or not the CLI is installed. Under home-manager,
`board.url` renders that file for you; set it to `null` (the default) to keep rendering it
yourself. There is deliberately **no default board URL**: unset means this machine has not
been told which board it belongs to, and guessing would point the query at somebody else's.

**`QUARTERBACK_INSTANCE` is the exception, and it is per session rather than per site.**
Everything above says which board this *machine* talks to; this says which agent *this
session* is on it. Unset — the normal case — the clients send the session-id prefix as an
opaque key and the board designates a name against it, which is where `zeus/glacier-amber`
comes from. Set, it does two things: it is the key, so two sessions sharing one value are
**one agent** on the board (one history, one lease, one set of claims — never export it
host-wide), and `qb-env`'s `qb_requested_name` turns it into an `X-Agent-Name` request, which
is what makes `zeus/seat-3` the spelling peers actually see. `qb`, `qb-hook` and the MCP
server all send that request (#156); before they did, the label was a key nobody was shown.
The name shape is stricter than the key shape — `^[a-z0-9]+(?:-[a-z0-9]+)*$` — so `Deploy_1`
is asked for as `deploy-1`, and a label with nothing usable in it asks for no name at all.

`qb-reconcile` is the one piece here that cannot run at all without a board — the plan it
reconciles *is* the board — so unlike `worktree-holder`, which degrades to "no occupancy
information", it exits **2** and says which read it could not make.

## Caveats

Read these before adopting rather than after.

- **The commands assume Claude Code**, specifically `$CLAUDE_CODE_SESSION_ID` and the
  session-marker convention above. The *scripts* have no such dependency and are useful on
  their own from a normal shell.
- **The database copy assumes PostgreSQL in a container.** Other engines are configurable in
  principle but far less exercised.
- **Pin `database.container` if the machine runs more than one Postgres.** Left at `auto`,
  the scripts look for a running container whose name matches `postgres|pgdb|_db`. They now
  prefer stable human-named containers over hex-prefixed ephemeral ones and verify the
  candidate actually answers as your DB user — but "guess the container" is still a guess.
  On a host also running self-hosted CI it originally picked a runner's throwaway
  `<hex>_postgres16_<hex>` service, which made `create-worktree` fail with `role "..." does
  not exist` and, worse, made `prune-worktrees` report `Orphan databases: none` while a
  hundred orphans sat in the real container. An explicit name (as in
  `worktree.example.json`) removes the ambiguity entirely.
- **`/fix-issue` does not stop to ask.** It plans, implements, pushes and opens a PR in one
  run. That is the point of it, and it is also the reason to read it before pointing it at
  a repo you care about.
- **`prune-worktrees` is dry-run by default; the other two are not.** `remove-worktree`
  destroys on invocation.
