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
  job of N seats would cost against them — **and the board
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

What it deliberately does **not** do as prep is stamp the release number.
`scripts/release_stamp.py apply` resolves `vNEXT` against the base **as it stands now**, so a
command that stamps and then leaves the PR for a human has spent a number another branch may take
in the meantime — after which `apply` refuses, correctly, with a hand-edit as the repair. It runs
`preflight` instead: the same question, no number spent, and `apply` belongs to whoever merges.

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

End-to-end resolution of a GitHub issue in a dedicated worktree. Reads the issue, plans,
**decides whether the change needs an isolated database copy**, provisions a worktree,
implements, writes tests, updates docs, runs the project's real CI checks, self-reviews the
diff (optionally with `codex` as a second opinion), commits, pushes, opens a PR, and
comments on the issue.

Two decisions in it are worth lifting out, because they are the ones that bite:

- **DB mode.** Schema changes and data writes get an isolated copy; read-only work shares
  the main database because copying is slow. If it can't tell, it picks isolated. There is
  a guard for the case where it chose `shared` and *then* discovered it needs a migration —
  it stops and asks rather than mutating the shared database.
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
rather than quietly swapping it: quarterback's branches no longer write a release number
at all (`scripts/release_stamp.py`, #122), so the CHANGELOG conflict that remains is two
`vNEXT` headings whose answer is always keep-both, and the README narrative that had to be
*rewritten* by hand is deleted. (Keep-both on two already-STAMPED entries is the one case where it
is still wrong, and `release_stamp.py check` refuses on the duplicate number rather than leaving it
to a replayed resolution.) That removes the case; it does not remove the class. A
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
hand, `qb-hooks uninstall` to take it off.

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
**request** (`X-Agent-Name`) — one the MCP server makes and the lifecycle hook does not.
Allocation is first-contact-wins, and the hook fires on `SessionStart`, so it usually wins.
Measured against a live board:

| First contact | Later request | Board says |
|---|---|---|
| key only, no name (the hook) | — | `zeus/meadow-russet` |
| key only, no name (the hook) | `seat-lexray-9` (the MCP server) | `zeus/meadow-russet` — **the request is ignored** |
| key **and** `seat-lexray-9` together | — | `zeus/seat-lexray-9` |

So a seat that does not ask up front comes up as two random words about as often as not,
losing the one property the numbering was for. `qb-seat` makes a single `GET /whoami`
carrying both headers before it execs, which settles the row; every process that follows
resolves to it. It reads back what the board actually said and warns if that is not the name
it asked for, which happens when the key was bound to a designated name on some earlier run
— allocation hands a returning key the name it already had, and a request cannot displace
one that exists.

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
where it is, a PR or an issue opens on GitHub. `qb-dash` is the same five views rendered
without interaction, for a terminal that will not forward mouse events.

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
a `⚒`; clicking one opens a confirmation showing the exact command, and confirming runs
`/panel-review-pr <n>` or `/fix-issue <n>` in a detached tmux window of its own — the same
way `qb-seat` starts an agent, so what it starts is a real session you can attach to, read
and interrupt. Clicking anywhere else on the row still opens the thing on GitHub. The keys
are `o` open, `p` panel-review, `f` fix the selected issue or plan item, `s` this project's
rows or the whole fleet's, `r` refresh, `?` the list, `q` quit.

**The plans panel is the one that says what the work is FOR.** FLEET says who is here and
CLAIMED says what they hold; neither answers why. `PLANS` is the board's plan — every repo's
ordered list, plus the fleet-wide one — with the items somebody is running at the top, then
the ones that are free, then the blocked ones, which are the band a reader can do nothing
about. Inside each band the board's own order is kept, because the order is the point of a
plan, and the repos this dashboard watches come before the ones it only overhears. A row
shows `▶` running with its holder, `▷` inside a plan somebody else holds, `○` free with how
long it has sat, or `⊘` blocked with what it waits on — the `▷` because an item covered by
another agent's plan claim is not free work, and showing it as free is the outcome
`covered_by` exists to prevent. Clicking one puts its plan, its claim note, its blockers and
its own note on the detail line: that reasoning lives on the board and nowhere else — a plan item never
restates its issue — and it does not fit in a title cell.

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
`gh issue list` reports. The `⚒` on a held issue still works and the confirmation names the
holder: a session that died leaves its claim standing, and picking that work up is a thing
to warn about, not to forbid.

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
- **Workflow stage.** `qb-stage <stage>` records how far along the work is, in
  `~/.cache/claude-code/session-stage/$CLAUDE_CODE_SESSION_ID`:

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
holds `dbtarget.py`, the test-suite half of database isolation — see the prerequisites below,
because a `.worktree.json` alone does not get you there.

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
| `bin/qb-hook` | the lifecycle reflexes — presence, lease, handoff, publish-on-push, the ask courier, sync advice, sub-agent records. Fired by Claude Code, never by the model: these must not depend on anybody remembering them. Fail-open by contract |
| `bin/qb-env` | the site-config contract — which board, which token. Sourced, not run |
| `bin/qb-mcp` | one stdio MCP server per session, so each agent carries its own identity |
| `bin/qb-claude-setup` | the wiring: merges the hook fragment into `~/.claude/settings.json`, registers the MCP server in `~/.claude.json`, @imports the workflow doc |
| `bin/qb` | what a human types — `qb sessions`, `qb resume <id>` |

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
