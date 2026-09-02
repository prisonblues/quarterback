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
  stage a session is in for the statusline, `qb-mode`, which says which of the two
  ways of working a repo uses — `⌂ CLEANROOM` or `~ JUNGLE` — and exits 3 when the
  tree you are standing in contradicts it, `qb-board`, which
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

So `.harness-rules.sample` now carries ten `review_panel` dials (#165, #297, #492, #482), and what they
bound is the tail rather than the signal: `fix_severity_floor` (**P4** as of 2026-08-30,
from P3 — #621) is what a fix round is asked to clear, and below it a finding is reported,
marked and recorded rather than fixed. Admitting P4 — 31.3% of findings, and the tier that
actually ballooned #236 — adds no obligation: it is not the blocking band and has not been
since #297, so what changes is that P4 joins P3 INSIDE `low_severity_fix_lines`' budget
rather than sitting outside every rule, and a count decides cheapest-first which of them a
round takes;
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
individually reasonable small fixes, and since #551 `low_severity_fix_full_chars` (**14,325**) is the proportional half of that same budget — the first round's `pr_chars` at or above which the whole 40 lines applies, with anything smaller getting it pro rata and the round spending whichever half is smaller, so a fixed 40 lines can no longer be a bigger share of the change than the change; a **ceiling** and not the floor #664 put under `max_fix_growth`, because the two dials' dangerous ends are opposite and a `max` written here would reintroduce #188; in chars because that is the unit the comparison happens in and the only first-round size a baseline records, and measured rather than converted — the median `pr_chars` of this repo's merged PRs scaled to the ~182 churned lines at which 40 is #551's sane ~22% (n=21, range 9,538-18,604) — with the result clamped at one honest one-line fix (`2 x unrefereed_line_weight`), because since #674 a budget too small to buy anything is a declination, a veto, a lost `stop_confident` and a `preland --require-earned-stop` hold;
`unrefereed_line_weight` (**2**) is what one churned line of test or prose costs that budget
against a production line's 1, because a production fix has an external referee in red/green,
the suite and CI while a test fix has none — nothing tests a test — so a budget that prices
them alike spends most of itself where nothing can check it (#554);
`next_door_days` (**7**, `0` for none) is how far back a defect **confirmed on another pull
request**, in a file this one also touches, may be carried in front of the reviewers as
context — the per-PR recurrence chain cannot see one file over, and on 2026-08-26 a P1
confirmed in `app.auth.delegated()` shipped again an hour later in `app.auth.human()` on a
different PR, into a round 1 with nothing of its own to recur against (#508); it is a hint and
never a finding, so a seat must find the defect in the diff in front of it before reporting
one — an instruction the prompt carries and **nothing enforces or measures**, which is said
plainly rather than dressed up, though the hint TEXT is mechanically flattened and capped
because it is model output from other PRs quoted into a prompt that instructs a model;
`max_fix_growth` (**3.0**) stops a cycle whose fix pass has multiplied the change instead of
fixing it, and `max_fix_growth_chars` (**30,000**) is the absolute half of that same ceiling —
whichever is crossed first binds, because a pure multiple hands its rope out in proportion to
the starting size and so lets a 2,000-line PR grow by four thousand lines on the dial that
stops a 113-line one at 226 (#492), while `min_fix_growth_chars` (**2,000**) is the floor under the multiple and the one term here that loosens — diff framing is ~430 fixed chars a hunk and the multiple's allowance is not, so below ~413 chars a 3.0x ceiling cannot afford one honest one-file fix, and on the PR that was measured on it priced a real correction out into a regression the next round then found (#664); `reviewer_scope` (**diff**) asks reviewers for defects in the change rather than
in everything it touches; `fixer_may_defer` (**true**) gives the fixer the third exit it did
not have; `max_rounds` (**6** as of 2026-08-30, from 2 — #621) surfaces the existing cap, which is
a backstop against a cycle running forever and not a convergence mechanism: what ends a cycle is
`escalate_on`, `fix_injection` first; `file_deferral_issues` (**shape** as of 2026-08-30, from P2 —
#620) decides which deferrals get a GitHub issue as well as the board row every deferral gets
anyway, and it asks what shape the TICKET would be rather than how severe the finding is — a
category or one substantive named item gets an issue, a batch of a round's leftovers gets rows and
never one. The tail was arriving one step downstream of the floor: the floor kept a P4 out of the
fix pass and the bookkeeping then filed it as a ticket, twenty times over on this repo alone, every
one of them a batch and not one ever closed (#482, #620); and
`require_failing_test`
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
narrow on purpose — three conditions that must all hold, **or** a fourth that fails on its own
(#491: the property the fix asserts is not decidable in the runtime the assertion runs in, so
every fix for it is an approximation and the rounds cannot converge) — and it never authorises a
redesign, because the output is "stop and ask" and the evidence behind it is still two PRs (#67). The
premise can be put to the seats first with `panel.py --ask`, which is exactly the shape of
question that path exists for. An escalated finding is recorded as `deferred` by the
orchestrator, which relays it, opens an issue that **asks** the premise, and names that issue in
`deferred_to`. Under `review_panel.fixer_may_defer` a fixer may now also return `deferred`
itself — "the defect is real, and it is not what this change is for" — which is a different
judgement from an escalation ("the defect is real and the FIX is in dispute") arriving at the
same row; the fixer owes two justifying lines and the orchestrator still owns the filing.

**A deferral always gets a board row; `review_panel.file_deferral_issues` decides which ones
also get a GitHub issue** (#482). The two were being treated as one record and they are not: the
row chains by finding key across rounds, feeds `/panel` and keeps the leaderboard honest, while
the issue is a work item on somebody's tracker. **Since #620 the gate asks what shape the ticket
would be, not how severe the finding is**: a CATEGORY — one standing item for a recurring class —
or a SINGLE NAMED ITEM with real substance behind it gets an issue whatever severity it carries,
while a BATCH, a round's leftovers swept into one ticket, gets board rows and never an issue,
whatever its severity mix. Twenty P3s in one issue is not a deferral, it is a transfer of the
problem to a human. Severity could not express that, because severity is a property of a finding
and batchness is a property of the ticket, so a cut anywhere on P1..P4 files some batches and
blocks some single items. The measurement, taken on this repo on 2026-08-26 and re-counted on
2026-08-30: twenty open issues were panel deferred-finding exhaust and nothing else, carrying 345
findings, every one of them a batch, and not one had ever been closed — #283 is a rescue *from*
one of them. **A deferral nobody classified is a batch**, because that is the answer that cannot
mint a ticket nobody reads. Where an issue is opened the orchestrator names it in `deferred_to` as
before; where it is not, the row carries no `deferred_to` (the column is nullable, the API accepts
it, and `/panel` renders a targetless row rather than breaking) and a one-line `note` instead,
which is what makes it worth reading later — `GET /review/findings?repo=&pr=` is the read that
write exists for. The default is `shape`; the P1..P4 bands still work and are the way back to the
severity cut this ran under until 2026-08-30, `always` is the pre-#482 behaviour and `never` files
none. **An escalation is exempt at every setting**, because its issue asks a question rather than
filing a task, and if the board write fails the orchestrator files the issue anyway — without one
the row is the only record, so losing both would lose the finding.

`harness/tests/test_fixer_escalation.py` guards the wiring rather than the
judgement: that the permission and its report ship together, that the cross-file references to
step 3a resolve, and that `deferred` is a value the database accepts.

**Every one of those outcomes is a refusal, and refusing was the expensive road** (#616). The
brief permitted a false positive and asked nothing at all in support of a fix, so the whole
burden of proof sat on refusal — and when a finding is wrong, complying is cheaper than
disproving it, so the pass complies and the churn reads as diligence. Nothing could see it,
because a fix for a non-defect looks exactly like a fix for a defect. So `review-pr.md`'s step 3
now owes **one line per finding before its patch**, naming who consumes the code the fix would
change: the callers, and for anything reaching a response or a stored artefact, the entitlement
tier it is served to. It lands in a **Consumers** column in the step-6 table, so the summary
carries it, and it is owed on **every** finding at every severity — the alternative, only the
findings whose fix touches a response path, asks the fixer to classify its own work before doing
the work that would tell it, which is the self-policing the requirement exists to remove. The
measured instance is lexray#1780 round 3: a P2 verified by a seat and confirmed by the judge,
wrong about what `html_preview` is for, fixed by merging the paid glossary into the anonymous
teaser, and caught a round later as an entitlement leak. The refutation was one `grep` for
callers and one docstring. `harness/tests/test_fixer_consumers.py` guards the same kind of wiring
the escalation suite does — that the requirement is in every brief that runs a fix pass, that
replacing `/review-pr`'s step 2 with a panel's findings does not take it out, and that the table
a fixer copies has the column.

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

### `/get-involved` and `qb-next` — nobody hands you the work (#424)

Every other command here takes an issue number a person looked up. This is the one that
does not:

```bash
/get-involved                     # this checkout's repo scope
/get-involved project:65lowther   # a scope with no repo behind it (#323)
```

It reads the board's plan for the scope, takes the first item that is open, unclaimed,
unblocked and outside anybody else's hold, claims it, and runs the right existing skill on
it — `/fix-issue` for an issue-backed item, `/review-pr` for a PR-backed one, and neither
for an item with no ref, which is house work and gets worked in place.

**Nothing here is new capability.** `GET /plan` has computed `next` since v2.39,
`POST /plan/item/claim` has been the interlock just as long, and `POST /plan/item/done`
closes the loop. What was missing was a caller: thirteen briefs in `commands/`, none of
which contained the word `plan`. That is the shape of #169 — a finished mechanism nothing
invokes — and this is the fix for one instance of it.

**What it buys is a fleet with no dispatcher.** Tell three agents "get involved" and they
take three different items, because the claim is atomic and the loser is told who won. The
job it removes is the one a person does hour to hour: picking each item and handing it over
by number.

The mechanical half is `qb-next`, so the discipline is in code rather than in prose a model
may skim:

```bash
qb-next                      # read the plan, claim `next`, print the command to run
qb-next --json               # …and everything about it, including the caveat
qb-next --dry-run            # say what it would take; take nothing
qb-next --done <item_id>     # record it finished, dropping the claim
qb-next --release <item_id>  # put it back — on ANY exit, failure included

#   exit 0  took one     — stdout is the command to run on it
#   exit 1  nothing free — everything is claimed, blocked, covered or done
#   exit 2  unknown      — the board is unreachable, or refused
```

Four decisions in it are worth stating, because each is a way this could have gone wrong:

- **It says what the order is worth before it takes anything.** `next` walks rank order, so
  it is only as good as the ranks — and an appended item sits where `plan_add` put it, not
  where anybody decided it belonged. The board reports that as `order_trust` and
  `next.caveat`; `qb-next` prints both on stderr *before* the claim and repeats them in
  `--json`, and the brief is told to relay the substance in its first message. An agent that
  takes rank 1 without saying nobody chose rank 1 has laundered insertion order into
  priority, which is what #183 exists to prevent.
- **It cannot reorder.** `POST /plan/reorder` is human-only and nothing here calls it. An
  agent that rearranges the plan to suit itself has approved its own work.
- **It refuses to claim without a session id.** A plan claim is session-owned, and a claim
  recording no session falls back to the machine — so two sessionless agents on one box
  would renew *each other's* claim and both work the same item, which is precisely the
  failure it exists to prevent.
- **One item per invocation, and it stops.** A loop over items is an agent deciding how much
  work the fleet takes on, and nothing bounds that yet (#80 measures integration cost as
  quadratic in open PRs). The
  loops that do loop — `/fix-and-land` — loop over review *rounds* inside one issue, under
  a round cap and a spend ceiling that already exist.

**Exit 1 is not an error.** Everything claimed, blocked or covered is what a working fleet
looks like. It reports the counts, names the holders so you can go and ask one, and stops —
it does not scan GitHub for something else to do (that is #63, deliberately a separate and
much larger thing: it decides what *is* work, where this only reads an order a human set)
and it does not add an item so there is one to take.

**A ref that is already closed is bookkeeping, not a second item.** `qb-reconcile` finds
these regularly. `qb-next` claims the item, asks the forge, and if the issue is closed or the
PR merged it records the item done — with a note saying the issue closing is what decided it
— and carries on to the next free one. Three things bound that, and each is a way it could
have gone wrong:

- **The terminal state is per kind: `CLOSED` for an issue, `MERGED` for a PR.** GitHub calls
  an unmerged PR `CLOSED`, and a plan row naming one is usually work rather than a leftover —
  reopen it, replace it, find out why it was closed. Sharing one state set between the two
  kinds would have retired all of them quietly.
- **Only a definite answer skips an item.** A missing `gh`, a network outage or a repo the
  token cannot see all mean *work it*, because the other way round lets an outage close a
  plan. `--no-verify-ref` turns the question off entirely, which is what a board with no
  forge behind it wants (#327).
- **If the board will not record it, the claim goes back and the run stops.** The row was
  claimed before the forge was asked, so a failed `plan_done` leaves that claim live; walking
  on would take a second item and exit 0 holding both, one of them on work this agent has
  decided not to do and nobody else can now take.

**Exit 2 is load-bearing, and it covers more than an unreachable board.** A rotated token, a
500, a refusal, a reply that is not the shape the client reads: every one of those is "could
not tell" and none of them is evidence about what is free. Reporting them as exit 1 would
have an agent announce an empty plan on the strength of never having managed to read it —
the absence-vs-inability collapse `qb-claim` also has three exit codes for.

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

#### Databases the sweep must not touch — `.database.protect`

`prune-worktrees` calls a database orphaned when no live worktree maps to it, and the
mapping is per-worktree (`<project>_<create-name>`). Anything a project generates on some
*other* axis therefore matches nothing and is reported as debris on every run.

`.worktree.json`'s `database.protect` is the escape hatch, and its entries are **glob
patterns**:

```json
"database": {
  "engine": "postgresql",
  "protect": ["myapp_test_tmpl_*"]
}
```

They are patterns rather than literals because the databases worth protecting are almost
never one fixed name. The case that set the shape: a test suite that copies each run's
database from a pre-migrated template, `<project>_test_tmpl_<migration head>`, so a run
costs ~45ms instead of ~1.5s. A template belongs to a *migration head*, not to a checkout,
so it matches no worktree by construction and every sweep offers to drop it.

Naming those exactly does not work, and it is worth being precise about why, because the
attempt looks reasonable: the live heads are spread across branches — one on the integration
branch, another on some unmerged feature branch — and no single checkout can see all of
them. So a hand-pinned list cannot be verified from anywhere, and the day a migration lands
it protects two dead templates while the live one goes back to being swept. That failure is
silent and in the unhelpful direction. It also pushes per-branch state into a shared
base-branch config file, which makes editing `.worktree.json` a step in landing a migration.
`myapp_test_tmpl_*` has none of those properties.

Two things follow from patterns:

- **A literal entry still means exactly what it did.** A pattern with no metacharacters is
  an exact match, so lists written before this are unaffected.
- **Every match is reported**, with the pattern that caused it, under `Protected by
  .database.protect`. A pattern is the one thing here that can suppress a *genuine* orphan,
  and a too-broad one would otherwise do it invisibly — which is a worse failure than the
  staleness patterns exist to remove.

The two built-in protections — `<project>` and `<project>_test` — stay exact, and stay
silent. Those names are minted by these scripts, so there is nothing to go stale and nothing
to be surprised by.

`.database.protect` is only consulted for databases under the `<project>_` prefix, because
that prefix is the whole of what the sweep considers. An entry outside it is inert rather
than wrong: nothing outside the prefix was ever going to be dropped.

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

### The shared-checkout guard — the one refusal on the board (#185)

Everything else quarterback does is advisory, and for everything else advisory is enough: a
signal you can act on later is still worth having. This one is not like that. `git reset
--hard` in a tree holding somebody else's uncommitted work destroys it at the instant it runs,
so there is no later moment at which a warning would still have helped. That asymmetry is the
whole argument for a refusal, and it is why there is exactly one.

It has happened five times here. Four in `65lowther` — an agent's in-flight `clash.py`
committed by somebody else under a message saying the work was not theirs; a half-wired
`annex.yaml` include that gave all four agents the same 41-error build. Twice more on
2026-08-25, in quarterback's own checkout, where a `git reset --hard` took a peer's review
fixes while they were still writing them.

`qb-hook` gates `Bash` at `PreToolUse`. Three facts have to be true **together**:

- the command entangles you with a peer's uncommitted work, in one of **two** ways:
  - it **destroys** it — `reset --hard|--merge`, `checkout` of a path or with `-f`,
    `switch -f|--discard-changes`, `restore` (unless `--staged` alone), `clean -fd|--force`,
    `rm -f`, `worktree remove --force`, the `--abort` of an in-progress merge/rebase/cherry-pick;
  - or it **absorbs** it — `commit -a`, `add .`/`-A`/`-u`. In a shared tree you cannot tell your
    uncommitted files from theirs, so staging "everything" stages theirs;
- **the tree that command will actually touch** holds some, **untracked files included** —
  `git clean -fd` destroys precisely the files that `--untracked-files=no` would have hidden,
  and one of the five was an untracked file;
- and a peer is live in that tree, which `GET /active?cwd=` has answered since v2.6.

Take any one away and nothing happens. A clean tree is never refused. Alone in a tree, your own
uncommitted work stays yours to throw away. `--help` is never refused, and neither is a dry run:
`git clean -n`, `-fdn` and `--dry-run` print what they would remove and remove nothing.

**The command is tokenised, not matched.** A panel round found nine P1 bypasses in the regex that
used to decide this, and they were one premise wearing nine faces — a regular expression cannot
parse a shell command. `git -c core.filemode=false reset --hard` was not matched at all, because
the pattern knew only `-C <path>` among the two-token global options. `git clean -n && git reset
--hard` was excused by a dry run in a *different clause*; `git status --help; git reset --hard`
the same through `--help`; the escape hatch the same through grep's per-**line** `^`. Patching
those where they were found is how #67's loop starts — the special case is the next round's
finding — so the premise went instead. `qb-classify-command` splits the command into clauses and
classifies each on its own, which is what "does this command do X" needed all along. It reads
inside `bash -c '…'` too, and `echo git reset --hard` is correctly not a reset.

**The regex survives in one job: a prefilter.** It decides nothing and it may not refuse anything.
It is there so the common case — every Bash call on this box that has nothing to do with git —
costs one `grep` and no fork, before the hook has even resolved its token. Measured: 48ms for a
non-git Bash call, against 137ms before the prefilter was moved ahead of the preamble.

**It guards the tree the command names, not the one you are standing in.**
`git -C ../peer-tree reset --hard` is checked against `../peer-tree`, and an explicit
`--work-tree=` is followed the same way. The first cut checked the payload cwd unconditionally,
which is wrong in both directions: it would let a peer's checkout be destroyed from a clean cwd,
and refuse a private checkout from a shared one. A target it cannot resolve — a quoted path, a
`$VAR`, a `$(…)` — falls back to the cwd rather than being guessed at.

**And it is the worktree ROOT, not the directory.** #185 says so in as many words: *"an agent
sitting in `65lowther/viz` is in the same tree with a different cwd"*. Both sides are asked —
the canonical root via `rev-parse --show-toplevel` under `cd -P`, and the raw cwd as well when
they differ, because a peer's lease records whatever cwd their session started in and the board
matches that string exactly. We can canonicalise our side; we cannot canonicalise theirs.

**It matches the tree, not the repo.** Two agents in two worktrees of one repo are not in each
other's way, and a gate that refused them would be refusing people who are free — which is how
a primitive gets learned around, and then it is worse than nothing.

**`QB_ALLOW_SHARED_TREE=1`** in front of the command proceeds anyway, the same shape and the
same reasoning as `QB_ALLOW_SHARED_STASH`. An advisory gate needs a way past or it gets turned
off wholesale; putting the override in the command makes taking it deliberate and visible. It
must be a real leading assignment — a bare substring test let `QB_ALLOW_SHARED_TREE=10 …` and a
trailing `# QB_ALLOW_SHARED_TREE=1` comment through, which made the hatch quietly wider than the
sentence documenting it.

**The bar is the accident, not the adversary — and that is a decision, not a caveat.**
A second panel round found thirteen more P1 "bypasses" (a `cd` in an earlier clause, `env git`,
`sudo git`, a `$VAR` target, `git clean -i`, `git submodule deinit -f`). Those are not thirteen
defects either; they are the next premise — that a *static* reading of command text can determine
what a command will do. It cannot, because the shell is Turing-complete, and chasing it produces
an unbounded list of spellings.

Counting #185's own five incidents by mechanism settles what the bar should be instead:

| incident | mechanism | covered by the first cut? |
|---|---|---|
| board 3860 | *"whoever runs `git commit -a` first sweeps up the other's half-finished work"* | no |
| board 3879 | exactly that — an in-flight `clash.py` committed by another agent as `409bae0` | no |
| board 4004 | a half-wired include, everyone's red build | no — not a command |
| board 3853 | a claim race | no — not a command |
| 2026-08-25 ×2 | `git reset --hard` in a shared checkout | yes |

Two of five. The commonest mechanism on that list was not in the verb list at all, which is why
there are two harm classes now rather than one. Two rounds and eighty-four findings went into
hardening the 2-of-5 case against spellings that have never occurred; the coverage win was one
verb.

**What it still cannot do, stated plainly.** Tokenising closed most of what a regex could not
reach — nested shells, quoting, clause scoping, `echo`ing the words — but not the parts that need
a shell to actually run: `${GIT:-git} reset --hard`, `env git`, `sudo git`, a `cd` in an earlier
clause, an alias, a shell function, a command assembled by `xargs`. All documented, none chased. A target it cannot resolve (`git -C "$SOME_DIR" …`) is treated as *unknown*
and falls back to the cwd, which is the conservative half of being wrong rather than a fix. It is
also time-of-check-to-time-of-use: a peer can arrive in the tree between the check and the
command. The threat model is an accident between co-operating agents, not evasion, and the cost
of a false positive is one refusal with a named escape hatch rather than a lost morning. The
structural fix is one worktree per agent, which is what the ⚠️ startup note pushes people
towards; this is defence in depth behind that.

**`git stash` is not in that list**, though it belongs to the family. It is already refused by
the `reference-transaction` hook above, which is strictly better for it: that one catches a
stash typed outside Claude Code entirely, and it does not wait for a peer to be live, because
a shared `refs/stash` stack is a hazard either way. Two gates on one command would only mean
two escape hatches under different names.

**One spelling of "take a worktree", and one note says it (#464).** #178's mode note and this one
both told the reader to take a worktree, in two different commands — and `create-worktree` is not a
synonym for `git worktree add`: it also does the database isolation, the claim and the hook
install. The two notes answer different questions and both stay — #178 is about POLICY (this repo
says work belongs in a worktree and you are not in one, true whether or not anyone else is here),
this one is about PEOPLE (somebody is in the tree with you now, by name). So when the mode note has
fired, this one keeps the names and drops the remedy; when it is silent, this one keeps it. Either
way the act is spelled `create-worktree` wherever that command exists.

**And the note that precedes it was wrong in the same place.** The SessionStart occupancy note
scopes by `repo` when there is one — which, inside a git repo, is always — so it never asked
about a working tree. On the night, it named the very agents whose work was about to be
destroyed and closed with "no need to hold off". That sentence is right about a repo and
backwards about a tree. The hook now asks both questions and gives them opposite answers:
sharing a repo is company, sharing a tree gets its own line, its own names, and a
`git worktree add` to get out of it.

**Fail-open, unchanged.** Board unreachable, request timed out, answer will not parse, `cwd` is
not a checkout: all let the command through. Both git calls are wrapped in `timeout` — they sit
on the interactive hot path now, and `git status` can block indefinitely on a dead network mount
or a slow `core.fsmonitor`. A coordination board is not in the critical path
of anyone’s work, and a guard that failed closed would turn every board outage into a
fleet-wide one. `harness/tests/test_qb_hook_shared_tree.py` pins all of it, including every
command that must *not* be refused.

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

The line `qb-seats` draws — *"the board coordinates work, it does not operate the machine"* —
is about **dispatch**, and none of this moves it. What an agent works on is still its own
choice, self-selected and claimed atomically.

### `qb-start` — the verb that begins a session, and it ships off

The other half of #277. There were three ways to start a session on this fleet and every one
of them ended at a human hand: a seat screen somebody built, the dashboard's ⚒ on
a mouse click, and `run_agent`'s headless `claude -p` inside a loop a person launched. So a
plan could say what was next, the board could show who was on what, and nothing could act on
either.

```bash
qb-start /fix-issue 277               # a session working issue 277
qb-start /panel-review-pr 352         # …reviewing PR 352
qb-start /get-involved                # …taking its own next item off the plan (#541)
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
#   exit 10 the FLEET's ceiling is spent — nothing on this box moves that
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
  maxSessions = 1;                  # 0 is a freeze — and the FALLBACK, see below
};
```

**The ceiling is a dial; the two permissions beside it are not
([#563](https://github.com/prisonblues/quarterback/issues/563)).** `spawn.json` carries three
keys and only two of them say what this box MAY do. `enabled` and `commands` are permissions
and stay in the nix-written file for the reason above. `max_sessions` says how HARD it may
work — the `in_flight.max` side of the very line the paragraph below draws, counting a
resource rather than guarding a door — and it was in the permission file only because that is
where it was written, inheriting a deployment path that costs a nix edit, a build, a PR, a
merge, a `nixos-rebuild` and a human with the password. For a number. The direction that
matters more is the other one: **`0` is a freeze**, the only control that stops a box spawning
without switching the mechanism off, and calming a fleet that is working too hard should not
require a rebuild at the moment nobody wants to be running one.

So there are two dials, and `maxSessions` above is the fallback under the first:

| dial | scope | counts | when unset |
|---|---|---|---|
| `spawn.max_sessions` | this machine | spawned panes whose agent has not exited | `maxSessions` from the policy file |
| `spawn.max_sessions_fleet` | the whole board | every live agent, spawned or not | no fleet ceiling at all |

Both are **fleet-scoped** dials — set them with no repo. A machine's concurrency is not a
property of a repository: `live_spawns()` counts panes on a tmux server without knowing which
checkout each is in, so with one repo at 5 and another at 2 there is no question the count
has answered. The board takes either scope for any dial (`dial` is opaque text there and
`repo` is just a column), so the refusal is the client's: the dashboard's dial picker
**refuses** the write (`harness_rules.dial_scope_problem`), and `qb-start` **names** a
repo-scoped row it is ignoring — because a `curl` and the web page have no dial vocabulary
and by design cannot have one, and a setting stored, reported as in force and read by nothing
is the failure this whole layer exists to end. The other direction stays legitimate: a rules
dial is set at either scope, and a fleet-scoped one is how a single value covers every
watched repo.

Both **fail open**, alone among `qb-start`'s gates and for `qb-admit`'s reason: they count a
resource rather than guarding a door, so an unreadable dial leaves the file's number in force
and an unreachable board leaves the fleet gate silent. A permission that failed open would
start sessions nobody authorised; a ceiling that failed closed would stop every box on the
fleet over a board hiccup, which is worse than the thing it guards. Safe to put on the board
because an agent may read its own ceiling and cannot raise it — which is the whole of what
makes this a throttle rather than an escalation. That used to be a property of the GATE
(`POST /dials` took `app.auth.human`); since [#591](https://github.com/prisonblues/quarterback/issues/591)
the endpoint also takes a delegated agent, so it is enforced by the READER instead:
`ceilings_from_board` drops any row whose `set_via` is `agent`, names who set it, and leaves
the file's number in force. A null `set_via` predates the column and is honoured. This is the
one dial whose subject is the agent reading it, which is why it is the one that cannot follow
the rest.

**The fleet number is a runaway guard, not an allocator.** It exists to stop a hundred agents
opening at once against a long queue — [#476](https://github.com/prisonblues/quarterback/issues/476),
the drainer, is the thing that would do that — and every property follows: advisory and
non-atomic (two boxes spawning in the same second can both see room, exactly as `qb-admit`
documents), failing open, and worth setting **well above the busiest legitimate day**, because
a ceiling that bites in normal use gets raised until it does not and then it is not a guard.
It counts **every live agent** off `GET /active`, not only spawned ones — a hundred agents is
a hundred agents, and the board cannot tell a spawn from a seat somebody typed into without
new plumbing at both ends — so a busy human day consumes it too. That is the other argument
for a generous number.

`qb-start --policy` reads both, bounded at five seconds and failing open to the file, and
reports the effective ceiling with the layer that gave it — `max_sessions` is what will
actually apply, `max_sessions_policy` is the file's number underneath it, and
`max_sessions_source` says which answered. That is the one thing `--policy` leaves the box
for, and it earns it for a caller that reads a ceiling before acting. A machine that never
opted in still reaches nothing but its own config directory.

**`--policy --no-board` opts out**, and the dashboard's ⚒ takes it. `--policy` promises a
caller may ask on every click without paying for it, and the ⚒ asks from the UI thread, where
a board that is down would freeze the screen for five seconds per keystroke. It reads only
`enabled` and `commands` — both the file's — so it gives up nothing: the ceiling it never
consulted is still applied by the spawn itself, one step later, in `qb-start`'s own words.
The flag answers `--policy` and nothing else; on a spawn it is **refused** rather than
ignored, because a gate must never look like it took an instruction it did not.

**`/get-involved` takes no number, and allowing it implies allowing what it runs (#541).**
Every other spawnable command is aimed at an issue or a PR; this one reads the plan and
selects its own item, so the brief is the command alone and no claim is taken up front —
the interlock moves inside the session, where `plan_claim` is atomic and is what makes
three seats take three different items.

Two consequences worth reading before switching it on:

* **It dispatches**, into `/fix-issue`, `/fix-and-land`, `/review-pr` and
  `/panel-review-pr`. A policy naming `/get-involved` without those is **refused**, rather
  than granting them silently one hop along — otherwise the allowlist would say one thing
  and permit another, on the one gate whose whole job is meaning exactly what it says.
  `qb-start --policy` reports any command it lists but refuses, and why.
* **It asks the board whether anything is free** before spawning, and refuses at exit 8 if
  not. That is not dispatch — nothing is passed to the agent, and the item it eventually
  claims may not be the one that was free — it is the refusal that costs nothing, because
  the alternative is a session that starts, reads the plan, finds nothing and stops. This
  one gate fails **open**: a board that did not answer has said nothing about the plan.

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
| `/investigate` | `issue <n>` |
| `/fix-issue`, `/fix-and-review`, `/fix-and-land` | `issue <n>` |
| `/review-pr`, `/panel-review-pr` | `pr <n>` |

`/investigate` is the read-only rung and it is listed first deliberately. It was added for
#63's watcher, and it *narrows* what a trigger can do rather than widening it: the watcher's
default answer on a close call is understanding rather than a PR, so an allowlist holding
`/fix-issue` and not `/investigate` would have left the safe action the unstartable one and
the dangerous action the only one available.

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
`--via` names the caller — `cli` for a person at a prompt, `dash` for the dashboard's ⚒,
`watch` for the issue watcher (#63) —
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

**It is not a dispatcher, and that line is not being moved.** `qb-seats`' *"the board
coordinates work, it does not operate the machine"* is about **dispatch**: nothing here reads
the plan, picks an item, or tells an agent what to work on. It is told a command and a number
by whatever pulled it, exactly as the dashboard's ⚒ is told one by a click. Which work an
agent takes stays the agent's own choice, self-selected and claimed atomically.

**What pulls it: the dashboard's ⚒ (#371), and the issue watcher (#63).** The primitive landed with
no caller at all, which made the loop readable and still unstartable. The first caller is the
cheapest one there is — `qb-dash-tui`'s ⚒, which is still a human click, so it needed no new
safety: the gates, the machine cap, the allowlist and the claim are all here, at the
primitive, rather than at the caller. What that click gains is everything the old direct
spawn lacked: a session inside `qb-admit`'s window, holding a claim taken before the process
existed, endable by session id from the moment the pane appears, and posted to the board as
`via dash`.

**The second caller is the first one with no human at the end of it.** `issue_watch.py
--start` (#63) hands `qb-start` an issue its survey found actionable, `via watch`. Nothing
about the primitive changed to allow it, which is the point of having put the gates here:
the machine policy, the cap, `qb-pace`, `qb-admit`, the allowlist and the claim all apply to
the watcher exactly as they do to a click. What is genuinely new is that the answer to *what
started this* is no longer a person, which is why `watch` is its own trigger name rather than
being folded into `cli` — a board someone is scanning for surprises needs that to be greppable.

The watcher adds brakes of its own on top, because the ones at the primitive are per-spawn and
it is the end of the chain that reads a **public tracker**: `--start` is off, so a survey still
starts nothing unless a run asks it to; `--start-max` (default 1) bounds sessions started; and
`--attempt-max` (default 5) bounds spawn requests whether they start anything or not (the
once-per-run `--policy` probe is a question, not a request, and sits outside it).
The second ceiling is not redundant — a refusal about one issue starts nothing and so spends
none of the first, which let a backlog of held issues make one call each while `--start-max 1`
appeared to hold. Details in `harness/loops/README.md`.

**A hook or a cron floor is still not built, and that is the deliberate part.** The button
is paced by a person; a hook and a cron are not, and a trigger nobody is watching is the thing
that turns a bug into an overnight incident. `--start` does not change that: it is a flag on a
command, so somebody still runs it, and putting *that* on a timer is a decision made in a
crontab where it can be read — not a default anything here ships. A `SessionEnd` hook also has
a question to answer first that the button does not — *what is next* — and answering it by
reading the plan and handing an agent the first item is the dispatch this whole design refuses.

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

### `qb-catchup` — the ending the ⬇️ advisory never had

The board already knows a push landed and already tells you: `published` is emitted when a
push reaches the remote, and the lifecycle hook turns it into a **⬇️ pull before you build
on this checkout** advisory. That is the right diagnosis and a manual ending — whoever it
reaches then does the re-integration by hand, once per worktree, which was most of the
eleven integration merges it took to land six PRs on the day [#80] was filed.

```bash
qb-catchup                 # sweep every worktree of this repo on this machine
qb-catchup --dry-run       # say what would move, move nothing
qb-catchup --no-fetch      # act on the refs already here
```

**It fast-forwards, and it refuses everything else.** That is the feature, not a first cut
waiting to grow. Rewriting a checkout somebody is working in is exactly the disaster [#45]
was filed for — an agent ran `git rebase origin/main` inside a directory another agent
held, and the holder found its branch at somebody else's commit with conflict markers in
four files. It reconstructed from the reflog whether its own work still existed. It did,
because the branch happened to be pushed: luck, not design.

| State | What happens |
|---|---|
| behind by a clean fast-forward | **moved**, and said |
| a live holder (`worktree-holder` exit 3) | left alone, holder **named** |
| "could not tell" (exit 4) | left alone |
| uncommitted changes | left alone |
| commits on **no remote ref**, older than the grace window | left alone — *"if this disk failed that work is gone"* |
| commits on no remote ref, inside the window | left alone — *"work in flight, which is the ordinary state"* |
| the stranded question could not be asked | left alone, and every line **says so** rather than reading as clean |
| a fetch that would not complete | said once — the question is still asked, but nothing is called *finished with* |
| a fetch that would not complete, **and** the question was refused | said shorter — only that the refs may be stale; the refusal's own line gives the reason |
| diverged from upstream | left alone — "that is a rebase, not a fast-forward" |
| its upstream was deleted, nothing stranded on it | left alone — *"probably merged and deleted, so this worktree is finished with"* |
| its upstream was deleted, something stranded on it | left alone — *"not finished with"*, with the count and the age |
| detached, or no upstream | left alone |

**The loud line asks `git rev-list <branch> --not --remotes` and never `<branch>
^origin/<branch>`** ([#573]). Those are different questions and the second is wrong in
both directions at once: on zeus it called six worktrees endangered that were ancestors
of `origin/main` in their entirety, and said nothing whatever about the five that really
were carrying work no remote had — a branch nobody ever pushed has no `origin/` ref to
be compared against, so the largest hoards on the machine were the ones it could not
see. The row above used to read *"unpushed commits — left alone, loudly"*, and neither
half of that was the question being answered.

**AGE IS THE VERDICT, NOT THE COUNT.** This sweep fires on every merge, and a line that
says the same alarming number every time is wallpaper inside a week — there is no count a
working fleet reaches that would be green, because something is always mid-flight. Under
`STRANDED_GRACE_HOURS` (24, and it is `qb-doctor`'s `UNPUSHED_GRACE_HOURS`, with a test
that fails when the two drift) a commit on no remote ref is work in flight; beyond it, it
is the only copy of something.

**A question it cannot answer is refused once, out loud, and then hedged on every line.**
A remote whose refspec does not bring back `refs/heads/*`, a negative refspec, a
destination outside `refs/remotes/`, a ref under `refs/remotes/` that no remote's refspec
writes to — each means the tracking refs this is measured against are not the set it
trusts, so the sweep prints one `!` line saying which, and no worktree line claims
anything about stranded work. **`qb-doctor`'s `unpushed` row refuses on the same four
(#611)**, so the two tools no longer reach opposite verdicts about one disk. An empty note would be indistinguishable from a clean
answer, which is how a safety claim gets made by accident.

**A fetch that failed warns, and does not reassure.** It used to refuse the question
outright: `fetch --all` exits non-zero if **any** remote fails, so one permanently dead
remote (a retired fork, a box that is off) bought the hedge on every worktree line of
every merge for ever, on a hook path that never passes `--no-fetch`, and never the
signal. But stale refs do not mislead symmetrically. *"N commits here exist nowhere
else"* off older refs is at worst crying wolf; *"finished with"* off older refs sends
someone to delete the only copy of something. So the sweep says once that the fetch did
not complete — no remote URL, because git quotes credentials in those — still asks the
question, still shouts, and demotes only the reassuring verdict to a hedge. A run can
trip both guards at once, though, and then that line says less: when the question was
*also* refused it stops at the stale-refs half rather than claiming a measurement
nothing took, and the refusal's own `!` line underneath gives the reason.

**It sweeps WORKTREES, which is a real limit and not an oversight.** A branch checked out
nowhere is outside it by construction — on zeus that was six of the eleven branches
carrying commits no remote had — and a detached worktree is invisible for the same reason:
there is no branch to ask about. `qb-doctor`'s `unpushed` row asks the same question of
`--branches`, which is every branch in the repository, and is where the whole picture
lives. This one is here because it is what the reader is looking at after a merge, about
directories they can act on now.

**Ask git for the exit status, not the output.** `rev-parse --abbrev-ref --symbolic-full-name
'@{u}'` on a branch whose upstream ref is *gone* — the ordinary state of a worktree left lying
around after its PR merged and the remote branch was deleted — writes the fatal to stderr,
writes the literal string `@{u}` to **stdout**, and exits non-zero. An emptiness test on the
output therefore passes, and the failure surfaces one step later as "git would not say where it
stands" about a repository that is perfectly fine. Nothing unsafe happens; the catch-all guard
refuses either way. But the diagnosis is what this tool is *for*, and "its upstream is gone"
tells you to drop the worktree where the other reading sends you hunting for a fault.

**Exit 4 is a refusal here and permission in `prune-worktrees`**, which is worth stating
because it looks like an inconsistency and is the opposite. There, refusing on a board
outage means leaving real debris uncollected. Here it would mean rewriting a live checkout
*because the board was down*. An unreachable board must never become a licence.

**One fetch covers every worktree**, which is a property of git rather than an
optimisation: linked worktrees share the common git directory, so remote-tracking refs are
shared too and fetching once in any of them advances `@{u}` for all.

#### Two triggers, and only one of them acts

- **A merge this machine performed** — `gh pr merge` is matched in `qb-hook`'s `PostToolUse`
  the same way `git push` already is, and the sweep runs. This is the trigger that bites:
  a forge merge creates the commit **server-side and runs no local push**, so the very
  session that landed the work is the one now stale, and nothing local moved to tell it.
  (Which is also why the sweep must *fetch* here even though `gh` just talked to the
  remote: nothing local moved. Skipping it would make every checkout report "already
  current", and it would look like it worked.) `QB_CATCHUP=0` turns it off.
- **Somebody else published** — the advisory *offers* the command rather than running it.
  Acting unbidden at the top of somebody's turn is [#45]'s disaster class even when every
  individual refusal is correct, and the advisory fires on every prompt while behind, so
  acting would mean a `git fetch` per prompt. The offer costs a line and turns N pulls into
  one command.

The offer is appended locally rather than sent from the board, because whether the command
*exists* is a property of this machine — a fleet member on an older harness would otherwise
be told to run something it does not have, which is [#422]'s shape exactly.

#### The markers had to start answering subtractively

`worktree-holder` unioned two sources and used the markers only *additively*, which left a
false positive on the one checkout that matters most here. A lease records the directory an
agent was **launched** in, which for the worktree workflow is the main checkout — so
"launched under this path" is true of *every live agent in the repo* when the path is the
main checkout, including ones demonstrably working elsewhere.

Measured on hermes while this was being written: `worktree-holder <main checkout>` named
`hermes/seat-quarterback-5`, whose own marker said `…/quarterback-fix-issue-458`. It had not
been in the main checkout for an hour.

That is benign for `remove-worktree` and `prune-worktrees`, which only ever ask about a
*linked* worktree and for which a false positive is a refusal to delete — the safe
direction. It is not benign for anything that wants to act **on** the main checkout: the
catch-up would decline forever on any box with an agent running, which is every box, which
would make the feature inert.

So a session whose marker names a **different** worktree is not here. A session with **no**
marker keeps the lease-cwd clause untouched — that is the agent launched inside a checkout
directly, and it is the true positive this must never drop.

[#45]: https://github.com/prisonblues/quarterback/issues/45
[#80]: https://github.com/prisonblues/quarterback/issues/80
[#422]: https://github.com/prisonblues/quarterback/issues/422
[#573]: https://github.com/prisonblues/quarterback/issues/573

### `qb-seat` — retired (#540)

There was a per-pane wrapper here. Each seat ran `qb-seat <n>`, which gave the agent a board
name (`seat-<project>-<n>`), passed it a brief telling it to read the board and run
`/get-involved`, held a pid marker so two panes could not be the same seat, and registered the
name with the board before exec'ing the agent.

All of it is gone, and the reason is that five of its six jobs existed only to defend the name
it gave a seat. A seat is a pane with a shell in it; a pane holds an agent when something
types a command into it; and the board already keys an agent by its conversation, which is
unique across panes and machines by construction. So there is nothing left to name, nothing to
collide, and no marker to hold. What is left — the one line a pane is given — is
`QB_SEAT_INITIAL_CMD`, below, which belongs to the screen that makes the panes.

The knobs that went with it: `QB_SEAT_AGENT`, `QB_SEAT_BRIEF`, `QB_SEAT_SCOPE`,
`QB_SEAT_YOLO`, `QB_SEAT_FORCE` and `QB_SEAT_REPO`. `QB_SEAT_PACE` was folded into
`QB_SEATS_PACE`, which is the same question and now has one spelling. The dashboard's ⚖
had been reading `QB_SEAT_AGENT` for the binary it starts a review with, which would have
left it the last reader of a retired variable, so that one is `QB_DASH_AGENT` now — the
dash's own knob, beside `QB_DASH_REPO` and `QB_DASH_CONFIRM`.

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
qb-seats --cmd ''     # a screen of bare shells: panes, and nothing started in them
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

A screen records **what it is made of**: `@qb_repo` is the repository it was built in, and
`@qb_initial_cmd` is the line its seats were given. `--add` and the bar's ＋ read the second
one back, because neither can see the environment the screen was built in — the ＋ arrives
through `run-shell`, whose environment is the tmux server's. It is always set, the empty
string included, so a screen of bare shells stays that way when a seat is added to it.

**How anything outside turns a pane into a board identity: `@qb_session`.** The lifecycle
hook stamps the agent's session id on its pane at `SessionStart`, and `GET /active` returns
that same id for every live agent, so the join is an equality. It used to be a seat NUMBER
parsed out of the agent's name, which identified a pane in neither direction — `list-panes
-a` is the whole tmux server, so two screens could each hold a seat 1 (#208), and the board
is the whole fleet, so two machines could each hold a `seat-lexray-1`. The dashboard's SEATS
panel and its FLEET-row jump both go through the session id now (#540), which also means a
pane running an agent this screen did not start resolves correctly.

One tmux session: N panes each holding a shell with `QB_SEAT_INITIAL_CMD` typed into it, and
one full-width pane along the bottom running `qb-board --follow`. Every seat gets the **same**
line — that is the design and not an omission, because the moment one seat is told something
another is not, there is a dispatcher again.

The default line starts an agent and says nothing else to it. Give it a prompt and the screen
comes up working: `QB_SEAT_INITIAL_CMD='claude-yolo -- /get-involved'` is a screen that claims,
and it is still self-selection rather than dispatch — the same line to every pane, with the
board's atomic `plan_claim` deciding who gets what, which is why three seats given one line
take three different items. Whether the fleet should be taking work at all is `tempo`'s
question (#474), not this script's.

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
- **`QUARTERBACK_INSTANCE` must never be host-wide.** The board takes it as the agent KEY,
  so two seats sharing one are not two agents with muddled inboxes — they are a single agent
  with one history, one presence and one lease, holding each other's claims perfectly
  legitimately. Nothing sets one per seat any more (#540): with no value, `qb-hook` falls
  back to the session id, which is one per *conversation* and so unique across panes by
  construction. The layout still strips any inherited value, from the session as well as the
  panes, because one arriving from your profile would put every pane on the screen back to
  being one agent. Nothing in your shell profile should set it.

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
screen, or export `QB_SEAT_INITIAL_CMD=claude` to have prompts back everywhere: the flag is
sugar for the setting, so the permission question is *which command you type* and not a
second knob that could disagree with it.

`qb-seats` deliberately does not create worktrees (a self-selecting seat does not know its
branch until it has claimed), does not assign work, and does not drive the agents past
starting them.

#### The top line — who this screen is, and what is left to spend

`status 2` makes room for the seat bar, and tmux numbers those two lines 0 and 1. `install_bar`
only ever wrote index 1 — and **writing one index of an array option at session level stops tmux
inheriting the global array**, so index 0, which nothing set, resolved to empty. Every screen has
therefore carried a full-width blank strip in whatever `status-style` is (green, on a stock tmux)
since the bar shipped. Measured both ways: drop our `status-format[1]` and line 0 renders tmux's
own status line again; set index 0 alongside ours and it renders whatever we put there. Nothing
was relying on it, which is unusual — it makes this a free line rather than a trade.

**Identity on the left, the ceiling on the right.** The seat bar says what the seats are *doing*
and nothing said whose they were, so the left is `quarterback: <repo>` — `QB_SEATS_TOP` says
something else, and `QB_SEATS_TOP=` (empty) means no words of ours there at all. The right is
`qb-pace`'s verdict, because the shared subscription's five-hour and weekly caps are the only
hard ceiling this fleet has and their one on-screen home was the dash — which the qb key has
just made hideable with a single keystroke. A ceiling you can hide is not a brake.

**No `#()` in the format**, and that is the whole reason `qb-seat-top` is a loop rather than a
format. A status line re-expands every `status-interval` — 15s by default, *per attached client*
— and a `#(shell command)` in one runs on that cadence. `qb-seat-top` is awake on its own timer
anyway, so it writes the answers into session options and the format merely reads them:
`#{@qb_pace}` costs nothing to expand however many clients are looking. The severity picks its
colour through a conditional over literal styles rather than `#[fg=#{@qb_pace_sev}]`, for the
reason `seat_state_style` gives — style parsing and format expansion are different passes.

**A reading that could not be refreshed is not a current one.** `pace()` returned quietly on a
missing binary, a timeout, an error or an empty answer, which left the last reading on screen
looking live — a `STOP` from twenty minutes ago read exactly like a `STOP` from now, on the one
number the line exists to carry. The reading is *kept*, because a stale ceiling is still the best
estimate anyone has and blanking it would throw that away to avoid looking confident; it is
marked `stale` instead, and drawn dimmer and prefixed `~` — the same `~` the dashboard already
uses for "at most this long ago".

**The reveal is an aesthetic and says so.** It is lexray's `decrypt_text.js` effect with the same
parameters — a 40ms tick, one character settling every second tick, left to right, spaces left
alone — replaying every `QB_SEATS_TOP_EVERY` seconds (30 by default; `0` draws the line once and
leaves nothing running). Nothing depends on it: `QB_SEATS_TOP_ANIMATE=0` writes the text straight
out, and the reveal settles on precisely what that would have shown, which is what
`test_the_reveal_settles_on_exactly_the_static_text` pins.

A status line is not obviously capable of an animation, so it was measured before it was written.
One `tmux set-option` round trip is **3.5ms** — a ceiling of about 286 frames a second — and a
24-character reveal is 50 frames, **all 50 of which reached the terminal with none coalesced**, at
21fps. The cost is 175ms of tmux calls per replay against a build that already spends forty of
them, which is what makes a 30-second replay reasonable and a continuous loop not.

**Neither test catches a frame**, and that is deliberate. A frame is on screen for 40ms, so a
test that polls for one races it — the first spelling did, passing locally, passing in the flake
sandbox, and failing in the flake sandbox on the same commit for no reason but load. So the
effect is read rather than caught: `qb-seat-top --frames TEXT` prints every frame of the reveal
with no tmux and no clock anywhere near it, and the assertions are arithmetic — one character
settles every second tick, so frame *i* has `(i + 1) // 2` of them settled and those must be
right whatever the scrambled tail rolled. (Measuring the settled prefix instead of computing it
is its own flake: a random character that lands on the right one inflates the count, which failed
about one run in ten.) That it runs *at all* on a live screen is answered by `@qb_top_reveals`, a
count of completed reveals — still true a minute later, where a frame is not.

Three things the effect needed that the browser version does not:

- **Single-width characters only.** `decrypt_text.js` scrambles with `∞ ∑ ∏ √ ∫ ≤ ≥ ≈ ≠ ± × ÷`,
  which are East Asian *ambiguous* width — a terminal may draw them two columns wide, and several
  do. In a browser that is nothing; on a status line it means one frame is a column wider than the
  last, so the line jitters sideways all the way through the reveal. The box-drawing glyphs that
  replace them are unambiguously narrow.
- **No fork per character.** `out="$out$(rand_char)"` is a subshell for every scrambled column of
  every frame — about 580 of them per reveal, twice a minute, for an animation. Inline, all 50
  frames generate in 40ms, and the reveal keeps the 40ms tick it is supposed to have rather than
  drifting to whatever the forks cost.
- **A detached screen is not animated to.** One tmux query per interval buys that, against fifty
  `set-option`s played to an empty socket — a screen left over a weekend would play thousands.

**A missing `qb-seat-top` must not take the build down**, which is `dash_hooks`' rule arriving
again. During a rollout PATH's harness and a checkout disagree about which scripts exist, so
`beside_me` answers with a path that is not there — and a `run-shell -b` on a missing command does
*not* fail quietly: measured as `no current client` and `not in a mode` on stderr and a non-zero
exit, which under `set -e` killed `qb-seats` with the session, the seats and the tape already
created, on an error naming none of them. So the copy is asked for before it is run, and a screen
that cannot refresh its top line still draws it once and says on stderr why it will not change.

#### The qb key — every seat-level action, without the mouse

The seat bar is clickable and until #248 that was the *only* way in: adding a seat from the
keyboard meant dropping to a shell for `qb-seats --add`, killing the screen meant `--kill` in
the same shell, and the tape and the dash could not be got out of the way at all without
dragging borders. Turning the bar on also costs `mouse on`, i.e. shift-less text selection,
which is a real price for the only door.

**`C-q` is the qb key.** Press it, then:

| key | what it does |
|---|---|
| `a` | add a seat |
| `x` | close this seat — the agent in it goes too, so it asks first |
| `1`–`9` | jump to that seat |
| `t` / `d` | show or hide the tape / the dash |
| `=` | put the dash back to the width the screen asked for |
| `>` / `<` | the dash eight columns wider / narrower, and remember it |
| `s` | the screens that are up, in a popup |
| `K` | kill this screen — every agent on it goes too, so it asks first |
| `?` | the list above, in a popup |

**A key table, not a menu key** — `bind-key -n C-q switch-client -T qb` plus `bind-key -T qb`,
which is how tmux implements its own prefix. `C-q t` is one chord whether or not a menu was
drawn, and a menu drawn on every press is a flash across the screen you were looking at. What
the menu is for is the press where you *do not know* the key, so it is bound to `Any`: a key
the table does not have opens the menu rather than being swallowed, and the menu carries the
same accelerators. You reach the teaching by not knowing, which is the only time you want it.
Both the bindings and the menu are generated from one table in `qb-seats`, and
`test_every_menu_accelerator_is_a_key_in_the_table` is what stops the menu becoming a liar
about the shortcut it exists to teach.

**The bar says the key is waiting, and that is not decoration.** Switching into a key table is
invisible in tmux — its own prefix is the same, and its users know. Here nobody did: the first
press looked like a dead key, so the natural next move was to press it again, that lands on
`Any` and opens the menu, and a `display-menu` has no digit accelerators — so `1`-`9` did
nothing while the menu's own title promised they jumped to a seat. **One invisible state,
reported as three separate bugs.** `#{client_key_table}` is the whole fix and costs one
conditional in the seat bar, which already redraws on every change: press the key and a strip
appears carrying every key it accepts, and it goes as soon as the next one lands. It is the
mode indicator *and* the cheatsheet, which the menu had been carrying alone.

The hint holds no comma — `,` separates a format conditional's arms, so one would end the arm
early and print the rest of the strip unconditionally, in every session on the box. It is
appended and right-aligned rather than replacing the seat cells: wrapping the whole bar in the
conditional would nest the cells' own `#{?...}` commas one level deeper, and a seat row that
vanished whenever you reached for a key would lose the state colours exactly when you are
deciding which seat to act on. With `QB_SEATS_BAR=0` there is no bar and so no hint — the key
still works, silently, the way tmux's own prefix does.

The menu's title no longer promises the digits. Instead it carries a `j` row whose command is
`switch-client -T qb`, which hands you back to the table — the one place a digit means a seat.

**A key table is server-wide**, exactly as `MouseDown1Status` is, so the binding cannot simply
act: it reads `@qb_key`, which is set on this session and on nothing else, and in the other
branch does verbatim what tmux would have done — sends the key on to the pane. Press `C-q` in
a session that is not a screen and nothing has changed. That fall-through is the whole of why
a bare `bind-key -n` was not enough: a keystroke silently eaten in every other session on the
box is a worse bug than a missing feature.

`C-q` has prior claims worth stating rather than discovering: it is XON under `stty ixon`, and
readline and emacs bind it to quoted-insert. Inside a seat pane tmux sees it first, so on a
screen the agent loses it. **`QB_SEATS_KEY=M-q`** picks another; **`QB_SEATS_KEY=`** (empty)
binds none at all. Empty means *none* and unset means *pick for me* — the same `${VAR+set}`
spelling as `QB_SEATS_DASH`, because those are different answers. Your own tmux prefix is left
entirely alone either way, and `QB_SEATS_BAR` and `QB_SEATS_KEY` are separate knobs because
they are separate costs: the bar takes the status line and the mouse, the key takes one
keystroke inside the panes.

**Hiding a pane is `break-pane -d` to a holding window and `join-pane` back**, so nothing
about the process in it changes — the tape keeps following the board across a round trip and
the dash keeps polling. What is hard is the geometry coming back, and two things that look
like the answer are not. Saving `#{window_layout}` and handing it to `select-layout` restores
the shape and *reassigns the panes*, because its leaves are pane indexes and a rejoined pane
lands at a different one: measured on 3.6a as a restore that put the tape in seat 2's cell and
seat 3 in the strip along the bottom, exiting 0 while doing it. `select-layout -E` evens the
whole row *including* the dash, which then reclaims its 78 columns from one neighbour and
leaves seats of 50, 49 and 20 where they had been 39, 39 and 41. So the widths are recorded by
pane id before the break and reasserted after the join, left to right, which converges exactly
— the row is a fixed total, so setting each pane in turn leaves the last one no choice.

Showing the dash **replays the build order** for the same reason the dash is split before the
tape in the first place: `qb-seats` takes the dash off the whole window first, so it spans both
rows of a ten-seat grid, and takes the tape's strip off the bottom afterwards. Join the dash
back with the tape already in place and `-f` gives it the full height of the window instead —
a 78x44 dash down the side of a 121-column tape, where it had been 78x32 over a full-width
one. So the tape steps out, the dash goes in, the tape comes back. Which pane is hidden is
recorded on the **session** (`@qb_hidden_tape`, `@qb_hidden_dash`) and never on the server:
two screens must be able to disagree about whether their tape is showing.

**A path crosses two parsers, and `sh_quote` alone is not enough.** The dispatcher's path is
written into a tmux command string, so it needs `sh_quote` *and* `tmux_quote` — the rule
`tmux_quote` itself states, and which the first cut of `qb_actions` did not follow. The failure
is the silent one: a checkout under `a$Bdir` bound every key to `/…/a/qb-seat-key`, because tmux
expanded `$Bdir` to nothing. The screen builds, the bar draws, the table installs, and every key
does nothing at all. A `"` in the path is the loud version — `syntax error`, and a half-built
screen.

The two halves of the escaping do not scale together, which is why `tmux_quote_n` exists beside
`tmux_quote`. `\`, `"` and `$` are consumed by tmux's *parser*, once per pass, so a value inside
a string inside a string needs them escaped twice — and a confirmed action is exactly that, since
`confirm-before`'s command is stored by `bind-key` and parsed again when the answer comes back.
`#` is not: parsing never touches it, and it is the single *format expansion* at the end that
turns `##` into `#`, so doubling it per pass gives `####` and the wrong path. Two parses, one
expansion. `test_the_key_works_from_a_checkout_full_of_metacharacters` presses a plain action and
a confirmed one through a real keystroke from a directory called `a$B"c\d#e f'g`, because nothing
short of that covers the whole chain.

**A key press runs the copy its own screen was built with.** `bind-key -T qb` is server-wide and
holds one path: the root key is gated per screen, but the table is not, so the last screen built
writes it and a key pressed on an older screen arrives in the newer screen's `qb-seat-key`. Only
wrong during a rollout, and exactly then that it matters — the same question `dash_hooks` answers
for the resize hook. So the screen records `@qb_key_bin` and the dispatcher hands over, once,
marked in the environment: a recorded path can be stale or can be this file under another name,
and a hand-off that could hand off again is a loop a `run-shell -b` would hide completely.

**A toggle does not move the cursor.** `join-pane` leaves the joined pane active, so showing the
tape landed you *in* the tape — the next thing typed went to a board follower instead of the
agent being worked with, and the next `C-q x` refused with "that pane is not a seat", correctly
and confusingly. A toggle is about what is on the screen, never about where you are on it.

**The screen and the pane travel in the binding**, expanded by tmux as the key is pressed —
where `qb-seat-click` has to stash them in a server option and read them back. That stash exists
because `#{mouse_status_range}` is scoped to a mouse *event* and expands to nothing by the time
`confirm-before` runs its command; a session and a pane are *client* state, so a binding can
simply pass them. Measured on 3.6a against a real client and a real keystroke, from a plain
binding and from behind a y/n alike. It is the better answer as well as the shorter one: a
server option is a race between two clients pressing the key on one server, and an argument
cannot be. The **id** and not the name, because the value crosses tmux's expansion into a shell
command line, where a session called `it's` would leave an unterminated quote there — `$0` is a
dollar and digits and can be neither.

The one exception is `?`, and it is a tmux limitation rather than a choice: **`display-popup`
does not format-expand its command**, so the popup was handed the literal `#{session_id}`. The
guide asks tmux which session it is in instead — `display-message -p` with no `-t` answers with
the client's current one, which inside a popup is the client that opened it.

**The bar paints its own colours and never borrows the theme's**, which is a correctness matter
rather than taste. Every span used to set a *foreground* only and inherit whatever `status-style`
was — and on a stock tmux that is `bg=green,fg=black`. Read off the wire with a real client
attached, this is what the terminal was actually being sent:

```
ESC[38;5;108m ESC[42m   ＋ seat     green on green      2.08:1
ESC[38;5;167m ESC[42m   ✕           dull red on green   1.39:1
ESC[38;5;109m ESC[42m   seat 2      pale cyan on green  2.15:1
```

4.5:1 is the readable floor, so none of it cleared. The bar now names a background on every
span and picks foregrounds against *that*: colour109 at 6.33:1, colour108 at 6.13:1, colour214
at 8.20:1, colour176 at 6.01:1, and the ✕ moved from colour167 (4.10:1 — the one that still
fell short of the floor on the new ground) to colour210 at 6.53:1. The active cells were
already explicit pairs and were already fine: black on colour214 is 11.38:1.

Two traps worth knowing if you edit it. **`#[default]` is not a reset** — it jumps back to
`status-style`, i.e. back to the green — so the gaps between cells set the ground explicitly
too, and a `#[default]` left in one is a green notch between two dark cells. And **`#[fill=…]`
is what makes it a strip** rather than a row of dark patches: tmux pre-fills the whole status
line with `status-style` and draws the format over it, so without a fill the cells are islands.

Neither of those is checked by reading colours off the rendered line, because what makes the
old version wrong is a colour the bar never names. `test_the_bar_never_borrows_the_themes_background`
looks for a foreground with no background beside it, and
`test_every_colour_pair_on_the_bar_is_legible` computes the WCAG ratio for every pair the format
sets — so which colours the bar uses stays a taste that can move, while their being readable does
not.

**The guide is wrapped to fit the popup it opens in**, and the width is not a guess: `?` opens
it in a `display-popup -w N` whose border takes two columns of that, and a line longer than the
rest wraps. It shipped at 79 columns inside a 78-column popup and the last paragraph folded.
`test_the_guide_fits_the_popup_it_is_opened_in` reads the width out of the binding and checks
every line against it — in characters rather than bytes, since the text carries an em dash and
an ellipsis — so the two cannot drift apart again.

**A real keystroke is tested**, which the seat bar's click never could be: synthesising a click
means SGR mouse bytes and a status line whose geometry the test has to work out, while `C-q t`
is two bytes written to a pty. `test_a_real_keystroke_reaches_the_action` attaches a client,
presses the chord and watches the tape go — the only assertion that covers the join between the
key table and the actions, and the only one that fails if the root binding never matches, the
gate is always false, or `switch-client -T` names a table that is not there.

**The actions live in `qb-seat-key`, not in the bindings**, for `qb-seat-click`'s reason one
surface along: each is three or four tmux commands with a condition over them, and a menu
cannot be driven headless any more than a keystroke can — but the actions underneath both can,
and they are the part that breaks. `add`, `close` and jump-to-seat are `qb-seat-click`'s own
three ranges under different names and are **delegated** to it rather than written out again,
which is not tidiness: that path ends the agent's board session before it kills the pane
(#277), and a second copy would be a second place for that to fall out of step — the keyboard
would close a seat and leave the board holding its lease for the rest of a TTL.

#### The dash — WORK IN PROGRESS

`qb-dash` is a fourth pane for the right-hand side: fleet state, where the board pane
along the bottom (the **tape**) is the event stream. There was a second renderer of the same
views without interaction, for a box that cannot import `textual`; it is gone, and
`qb-dash-tui` is now a second name for the one that is left rather than a second thing.

**TWO TABLES, because there are two questions** ([#589](https://github.com/prisonblues/quarterback/issues/589)):

  **AGENTS** — who is here and how they are doing. Live agents, what each holds, the seat
  panes on the screen in front of you, and the claims no live agent answers for. Was SEATS +
  FLEET + CLAIMED, which were three renderings of one subject: a seat is an agent with a pane
  in front of you, and a claim is what an agent holds.

  **WORK** — what is in flight and where it has got to. The board's plan in the board's own
  order, with the review queue folded into the rows it is about and anything else that is
  open appended. Was PLANS + OPEN PRs + REVIEW QUEUE + ISSUES.

They were eight panels, and one measured frame spent **61 rows** on them — into the 38-row
pane #269 measured, and after that issue's per-panel caps. `#578` was on it four times, and
OPEN PRs and REVIEW QUEUE printed the same three PRs in ten lines. That pair was never going
to be two panels honestly: the queue is DERIVED from the open-PR list, so it is a subset by
construction, and all the PR panel added was the CI glyph — which is now the WORK table's
state cell for a PR row.

**What is deliberately still two rows:** a plan item names its ISSUE, and nothing records
which PR implements it (#396). So an issue and the PR that closes it are two rows, and the
missing edge is a fact about the board rather than a shortcut taken in the renderer.

Rows are clickable — a seat jumps the tmux cursor to that seat's pane and its `✕` closes it,
an agent row says where it is, a claim row shows the note the claiming agent left, a work row
explains why it is where it is (a plan item's reasoning, or what review is waiting for) and
its verb column takes the issue (`⚒`) or starts the round (`⚖`), and a dial row says why it
is set (its `✎` opens the one surface that can change it).

**A claim nobody answers for gets a row, and says which kind of nobody.** `machine` — the
claim names a bare machine name while that box's agents are `machine/name`, so nothing can
say which of them holds it (#444). `gone` — it names an agent presence no longer lists, so
that agent finished and its claim outlived it; this is the most actionable row on the
dashboard and it read exactly like a live one under CLAIMED. `elsewhere` — the holder is
live and this pane's scope hid it, which is not a loose end and is not counted as one.

**WHAT A PERSON OWES AN ANSWER TO IS ON IT, AND `w` SHOWS ONLY THAT**
([#328](https://github.com/prisonblues/quarterback/issues/328)). The board has held a blocker
as a first-class row since #274 — subject, class, question, owner, and a resolution required
to close it — and the web board and the plan page have both drawn one since. The terminal
never had: `/plan` serves `waiting_on_a_human` on every item and `qbdata` referenced it
**zero times**, so an item nobody could proceed on rendered `○`, in the cyan this panel uses
for *free to take*.

A blocked row wears **`⚑` magenta**, and that glyph is not a new one: it is what a PR wears
when its checks are gated — *"a run exists, will NOT execute without a human, and so will
never report"* (#324). A plan item waiting on a decision is the same sentence about a
different subject, so reusing it unifies the vocabulary rather than growing it. The `why`
cell says the class and then who owes the answer, or how long it has waited when nobody does
— `⚑taste rich`, `⚑environment ＋2`. **The count goes before the owner** when both will not
fit: #576 made several questions per subject possible on purpose, and a cell that spent its
last characters on a name would be that issue's undercount one surface further on.

**Every question gets a row, including the ones with no work to ride.** A blocker's subject
is one of item, issue, pr or repo, and `qb-doctor` raises `landed`, `harness` and `unpushed`
against the **repo** — the "the fleet is stuck" ones, which no piece of work on this table
can carry. They are drawn at the top with the review queue, because they are the same kind of
fact: work a person is standing in front of. Left out they would be counted by the header and
drawn nowhere, and a number that disagrees with the rows under it is the one thing a surface
a person is asked to trust cannot do.

**Nothing is offered on a blocked row.** The `⚒` and the `⚖` keep their shape and go grey:
taking an issue whose shape is still being decided, or spending a panel round on a PR
somebody has been asked whether to revert, is work done before the answer that governs it
(#522). A row that is *only* a question gets no icon at all — the state cell already says it.

`WAITING n` leads the header line, ahead of the review depth and the caps, and it is scoped
like every other tally there. Every other number on that line is about what the fleet is
doing to itself; this is the only one that is somebody's to act on. A zero still draws —
"nobody is waiting on you" is exactly the answer somebody opens this to get — and a read that
failed reports `?` rather than a confident nothing (#244). `w` in the clickable renderer,
`--waiting` in the printed one, `QB_DASH_WAITING=1` to open that way.

**AND IT PUTS THE PLAN IN ORDER** ([#443](https://github.com/prisonblues/quarterback/issues/443)).
`m` marks a plan row, `k`/`j` move one place, `K`/`J` move five, `[`/`]` go to the ends, and
`g` asks for a position. Marked rows move together, keeping the order you see them in.

**The auth constraint that issue was written around has lifted, which is why this is now a
small job and was not one then.** `POST /plan/reorder` depends on `app.auth.delegated`, not
`human`, and that tier accepts a person's own `X-Human-Key` — the credential this dashboard
already holds for the dial `✎` (#477). `auth.delegated` names the gap it closed in as many
words: *"Rich with a browser could reorder the plan, and Rich at a terminal holding the very
same key that `human()` accepts could not."* A move from here is stamped `ordered`, exactly as
the browser's ▲▼ are, because it is the same person deciding.

**The endpoint takes an order, never a move**, so up-one, jump-five, to-the-top and
go-to-position are the same call with a differently computed array — #388's finding on the web
board, reused rather than rediscovered. It is also why multi-select costs nothing extra at the
wire: N items splice in together and it is still one request.

**`g` asks for a POSITION, not the rank on the row, and the box says which one it is now.**
Ranks go non-contiguous as work finishes — `prisonblues/quarterback` was on `1, 3, 4, 5, 10, …`
with 37 open items when this was written — so "move it to 10" means two different rows
depending on which number a person meant. It heals itself after one use: the endpoint
renumbers the whole scope `1..n`, so the first reorder makes rank and position the same number.

Three refusals, each naming its remedy, because a control that silently does nothing is
indistinguishable from a broken one. A row with no order — a PR, an unplanned issue, a question
owed to a person — says so. Rows marked across two scopes say so, because each plan has its own
order and the endpoint reorders one exact scope. And **a truncated plan is refused outright**:
the endpoint renumbers every open item in the scope and appends the ones the caller did not
list, so an order computed from a partial list would silently move everything the pane was
never sent.

A move that changes nothing is **not sent** — the endpoint stamps `rank_source` on every item
it is handed, so posting an unchanged order would write "a human chose this position" onto rows
nobody touched (#183). And a second move while one is in flight is refused rather than stacked,
which is `dial_writing`'s rule (#577) for its reason.

**A move is painted before it is posted.** The write is a board call over the network, and a
pane that sat still for the length of it would be pressed again — which is a second move, not a
repeat of the first. So the row moves now and the board's answer confirms it; a refusal puts
the rows back whole rather than leaving an order nobody agreed to on screen. The plan's
fifteen-second poll is held off while a move is in flight, because a tick arriving in between
carries the order the board still has and would move the row back and then forward again —
two jumps for one keypress. The web board keeps the same guard and calls it `busy`; it makes
only its *drag* optimistic, because there the DOM has already moved under the pointer, and
every verb here is a keypress.

**The moved row stays under the cursor, and the marks stay set**, so pressing `j` four times
moves one thing four places. Both halves are the same rule and neither is housekeeping: a move
rewrites the whole table and a DataTable's cursor is an *index*, so without following the row
KEY the thing being moved slides out from under the person and the next press moves whatever
took its place. Dropping the marks does the same by another route — the block comes back, the
cursor lands on its head, and the next key moves one row while the person believes they are
still moving three. Silently changing what a key acts on is the mistake they cannot see; a
selection left set is one they can, because every marked row wears a `▪` and every message
names the count.

The keys are letters because the arrows are the DataTable's cursor and always will be. The
printed renderer has no input loop, so this is the clickable one's alone — as `s` already is.

**The backlog is behind `b`** — open PRs review has finished with, and open issues nobody has
planned or taken. They are a catalogue rather than state, and twelve rows of issue list was
the biggest single consumer of the old frame. `--backlog` in the printed renderer, which has
no keyboard; `QB_DASH_BACKLOG=1` to open that way. **Their counts stay on the header line
either way** — `PRs 3 · 1 red`, `ISSUES 30 · 25 free` — because a toggled list that left no
number behind would be a way of forgetting the work exists.

**THE BACKLOG waits before it paints, and says what it is waiting for.** Its issue rows come
from `gh issue list` and their order comes from the board's claims — free issues above held
ones — and the two arrive on separate workers with nothing sequencing them. Only that half
waits: the plan and the review queue are not sorted on claims, so they paint as soon as they
arrive, which is strictly better than the panel managed. Painting on
whichever lands first draws an order the panel is then about to rearrange, and a row that
moves is a row somebody clicks by mistake ([#433](https://github.com/prisonblues/quarterback/issues/433)).
So until both have answered the title says `WORK · … · backlog waiting for the board`, `…
for gh`, or names both — with any `gh` error carried as a ROW rather than a title suffix
clipped to 24 characters, for the reason the queue's error is one: a table whose job is
saying why something is missing must not truncate the message that says why it cannot tell
you. It cannot hang — a board that is down
and a `gh` that fails both count as answers — but "cannot hang" is not "is quick": a board on
a black-holed network answers by timing out, and `fetch_board` makes two sequential calls at
30s each, so the worst case is a caption on screen for about a minute. The usual case is a
fraction of a second, which is why the caption names what it is waiting for rather than
leaving a reader to guess whether anything is coming. What it does
NOT do is freeze the order afterwards — a claim taken or dropped later is a real change and
still re-sorts the table.

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

The header line counts every one of those that is not green. Before this it counted reds and
nothing else, so a PR whose runs were gated contributed to no number on the screen. It is on
the header rather than a panel title since #589, and it is the one thing the OPEN PRs panel
said that the review queue replacing its rows cannot: a PR can be green and unreviewed, or
red and already signed off.

**THE REVIEW QUEUE is the top of WORK**, drawn above the plan in both renderers. It was the
plain one's alone until #426, which is what made flipping the seat pane's default a port
rather than a one-line change: the panel would have come off the screen, and a number nobody
reads is how #273 happened in the first place. The OPEN PRs panel it used to sit under said a
PR exists and CI is green; it never said whether anybody had reviewed it, and on 2026-08-20
six of eight open PRs had never been panelled while the newest round on the board was two and
a half days old — neither number readable anywhere.

Above the plan and not below it, which overrules nothing: the board's order is an order over
plan ITEMS and says nothing about where a PR sits among them, because PRs are not in it —
the same silence the queue's own oldest-first reading order fills. Appending was the first
cut and it read badly on real data: forty-two plan rows above five review rows, in a table
showing twenty, is a review queue that is technically present and practically invisible.
The panel is `POST /review-queue`: every open PR joined to every panel run, plan item, work
claim and landing-queue entry the board holds, so each row carries the state it is in, the
verb that state implies (`panel`, `re-panel`, `fix`, `rebase`, `land`) and how long it has
waited. Rows nothing may act on keep their place and show the reason instead of the verb,
because a panel that hid them would report an empty queue for a repo where everything is
stuck. The depth and the oldest wait also sit on the header line at the top, beside the
budget they would be spent out of.

**The printed renderer has one row cap where it had four, and it is split by section rather
than sliced off the top.** A cap that always eats the same section is a section that is never
drawn: under a straight `rows[:cap]` the panel said `5 in review` in its title and showed
none of them — #273's hole reopened by a display limit rather than by a data model, which is
worse, because nothing about the code would look wrong. The review section takes what it
needs up to a third, the plan takes the rest, and each says how many it left out where the
rows went.

An age prefixed `~` is the longest the wait could have been rather than the length it was:
nothing records when a head moved or when a branch started conflicting, so those are
measured from the round or from the PR's opening. Nothing here starts a review — the panel
is a reader, and the thing that would act on it is #53.

**Above 157 columns the panels go TWO ACROSS, and what that buys is height.** Seven
panels dividing one column's rows was why CLAIMED and REVIEW QUEUE were two rows tall on a
50-row screen while four others got five each. With two tables it buys a great deal more:
AGENTS and WORK side by side each get the pane's whole height. The
threshold is quoted rather than chosen: 78 columns is what one of these tables wants
before it wraps — `QB_SEATS_DASH_SIZE`'s default, from `qb-seats` — so two side by side
plus the gutter is the narrowest pane on which the second column is not paid for out of
the first. Below it nothing changes at all, which is the point: the pane `qb-seats`
splits off is 78 columns and must come out exactly as it did before this existed.
`QB_DASH_WIDE` moves it, and a value that is not a positive number of columns is ignored
rather than fatal.

DIALS spans both columns — it is its content in either layout, so a column of its own would
buy it nothing and cost the table beside it half its width, and it keeps its place at the top
because it is the configuration every row below it is running under.

**There is no reordering left to do, and that is new.** A grid fills row by row in DOM order,
so seven panels had to be re-paired by hand — `Dash.relayout` moved PLANS down one when the
pane went wide, because the narrow order that put REVIEW QUEUE directly under OPEN PRs laid
them into different rows the moment there were two columns. Two tables and a spanning DIALS
need none of it: `relayout` is the class and nothing else (#589). Losing that `move_child`
is worth saying out loud, because it was load bearing in the other direction too — it ran
BEFORE `build_columns` in `on_mount`, so when it raised on a panel the merge had removed, the
tables were never given their columns at all and every row failed with "More values provided
than there are columns", four functions away from the layout call that caused it.

**AGENTS is not content-sized, and SEATS' exemption does not survive into it.** SEATS could
be `height: auto` because it was bounded — MAX_SEATS panes plus the ＋ — and AGENTS also
holds the fleet and every unattributed claim, which is as long as the board is. That is the
exact unboundedness that once put a table off the bottom of the pane, where its rows could
not be clicked.

**Take the width off the resize EVENT, never off `self.size`.** Measured on textual 8.2:
`on_resize` runs before the app's own size is updated, so a `self.size.width` read in
there is the width the pane had *before* the resize being handled. The caps bar had been
laying itself out one resize behind since it was first sized to the pane, and nobody
noticed because dragging a border emits a stream of resizes and the last-but-one is near
enough. A layout threshold is not forgiving in the same way — a pane that crossed once and
stopped would sit in the wrong layout until the next resize — which is how the older bug
came to light.

#### The dash full screen — `z`, `C-q z`, and the ⛶

The wide layout is only worth having if the pane can be made wide, and 78 columns down the
right of a seat screen never will be. `z` inside the dash, `C-q z` from anywhere on the
screen, and the ⛶ on the top line all reach one verb — `qb-seat-key expand` — which
**breaks the dash out into a window of its own**, and puts it back on the next press.

**It is `break-pane`, and deliberately not `resize-pane -Z`.** Zoom was the obvious answer
and is the wrong one: zoom is a property of the window and tmux drops it on any layout
change, which this screen makes constantly — `select-layout -E` when a seat is closed, and
the `window-resized` hook reasserting `@qb_dash_width` on every client attach. A dash zoomed
to read would pop back to 78 columns the moment somebody attached a phone, with nothing on
screen to say why. It is also not a `display-popup` running a second dashboard: that is a
second board poll, a second `gh` poll, and a cold start whose WORK title says "backlog
waiting for gh" for up to a minute. `break-pane` moves the pane the process is already in.

It is the same move `d` makes, minus the `-d` that leaves the pane parked where nobody is
looking — so it inherits everything that was hard about that one, including the rule that
the widths are recorded **before** the break. That rule bites differently here and it cost a
test to find: `hide_pane` is handed its size by a caller that read it first, while
`expand_dash` does its own break, so reading afterwards is one line away and looks
identical. It is not — after the break the pane fills its new window, so the recorded size
is the whole terminal, and the join back asks for a 240-column pane inside a 240-column
window and fails with `create pane failed: pane too small`.

**Two toggles over three states**, and the crossings are decided rather than accidental. `d`
means "in the row or not"; `z` means "full screen or not".

| | `d` | `z` |
|---|---|---|
| in the row | → hidden | → expanded |
| hidden | → in the row | → **expanded** |
| expanded | → in the row | → in the row |

The middle row is the one worth stating. Somebody pressing `z` on a hidden dash is asking
for a dash they can read, and a hidden one is one step from that rather than in the wrong
state for it — so it is shown rather than restored to its column, and rather than refused.
It is also the cheap direction: the pane is already alone in a window, so that crossing is a
rename and a `select-window` and no geometry moves at all. A `break-pane` there would fail
outright, having nothing to break.

Both routes out of the row record the same state and come back through the same
`restore_dash`, so there is one way back however it left — which is why `@qb_dash_expanded`
is cleared there rather than in `expand_dash`. `>` and `<` refuse while it is out, and say
which of the two states they are refusing for: "hidden" about a dash filling the screen in
front of you is the kind of wrong answer that makes somebody doubt the tool rather than the
state.

**`break-pane` must name the session, and until now none of these did.** With no `-t`,
`break-pane` puts the new window in the **client's current** session rather than the source
pane's — so on a server running two screens, taking a pane out of the one you are not
looking at parks it in the other one's window list. Everything downstream then fails to find
it: `pane_exists` and the restore path both search `list-panes -s -t "$SID"`, which is scoped
to the session, so the pane is at once alive, stranded, and reported as gone, with no way
back through this script. It is not reachable with one session on the server, which is why
`d` and `t` carried it from the day they shipped and the hide/show tests never saw it — it
turned up the first time a test screen was built beside a real one, with the dash landing in
`seats-quarterback:qb-dash` while its own screen said it was missing. All three breaks name
`-t "$SID:"` now, and the regression test builds two screens because one cannot show it.

**Nothing had to be taught about this mode for the resize hook to leave it alone.**
`qb-seats`' own `dash_pane` looks in `$SESSION_ID:seats` and nowhere else, so an expanded
dash is outside its reach and cannot be shrunk back to 78 columns by an attaching client.
That is a property of where the pane went, not a special case anybody wrote.

What the dash does with the room is its own business and is not arranged here: it is a
Textual app that lays out to the width it is given, so a window-wide pane simply crosses the
threshold above and goes two columns across. This verb moves a pane.

**It opens on ONE project, and that is the interesting default.** What the board serves is
fleet-wide by construction — every live agent, every claim, every repo's plan — while a
screen is built for one repository. So most rows were somebody else's, and the repo cell was
then the same word, eleven columns wide, on every line of a 78-column pane (#261). The scope
narrows what comes off the BOARD — AGENTS, and the plan half of WORK — to the repos this
screen watches, and drops the column outright; the eleven columns go back to `what` an agent
is doing and to a plan item's title, and `quarterback#209` on an agent's row becomes
`#209`. The repos are the ones the dashboard already resolved for its `gh` calls:
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
hid** — `AGENTS · 3 live · 2 elsewhere` — because a filtered pane that reads like the whole fleet
is worse than an unfiltered one: it is the same picture with fewer facts, and "nothing
claimed" and "nothing claimed *here*" are different claims about the world.

Four things stay fleet-wide on purpose. A row whose repo the board cannot name — an agent
outside a checkout, a fleet-wide plan item, a `plan:<uuid>` claim this process has not
resolved — is kept, because no repo is not evidence of another repo and hiding it drops a
live peer; it wears a `?` in front of its title, since the repo cell (`—`, `fleet`) was
the only thing that ever said so and the narrow view is exactly the view that drops it.
**The chip bar narrows the fleet to one repo.** One line of clickable chips above the
tables, a chip per repo the live fleet is actually in — not per repo the board knows, because
a chip with nobody behind it filters to an empty table. The same chip sets and clears, so a
bar clipped by a narrow pane cannot strand you with a filter and no way out; the unfiltered
count stays on the title (`AGENTS · 3 of 16 · lexray`) so a filter reads as a filter rather
than as the fleet having shrunk; and the bar hides itself below two repos, which makes it
mostly a fleet-wide-scope control — the scope that needed it. It does **not** narrow WORK:
`gh` is only ever asked about the watched repos, so filtering those rows would show all of
them or none.

The SEAT ROWS of AGENTS are not scoped: `tmux_seats()` lists every seat pane on the whole
tmux server, so another screen's seat is a pane you can still close, and narrowing it away
would take the `✕` with it. It returns `(seats, error)` rather than a bare list, because an
empty list is a fact about the SCREEN and a failure is a fact about the MACHINE: a shim on
PATH ahead of the real tmux once made every call exit 127, and the panel reported "no seat
screen on this server" beside a screen with three seats in it while the `＋` declined to add
one. A tmux that cannot be reached now rides the AGENTS title as `tmux: <what went wrong>`
and the `＋` says the panel is blind rather than empty. Being **outside** tmux is not an
error and deliberately reports none — the dashboard full-screen in a bare terminal is a
first-class way to run it, and a complaint that fires whenever nothing is wrong is how the
real failures get buried. Nor is the liveness a claim row is judged against — an agent the
scope hid is still alive, and calling its claim `gone` because this pane is narrow would be
stating a fact about somebody's work on the strength of a filter. A claim whose holder the
scope hid keeps a row of its own, marked `elsewhere`, because it is in scope and its holder
is not, and dropping it is the panel-that-filtered-silently defect applied to the one fact
that prevents duplicated work. The held-issue markers come from *every* claim, so an issue
held by an agent working out of another repo's checkout is still shown as held rather than
offered to the next seat. And the `gh` rows cannot narrow at all: `gh` was only ever asked
about the watched repos, so there is no other repo's row there to hide — only their column
answers to the scope.

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

**DIALS says what the fleet is running under, and where to change it.** A dial is a
setting: the repo supplies a default, the board states the value in force, and the layer
that answered is part of the answer ([#305](https://github.com/prisonblues/quarterback/issues/305)).
Until [#477](https://github.com/prisonblues/quarterback/issues/477) **no screen showed one**
— a dial was set from an endpoint and read back by one function in `panel_seats.py`, so the
value governing every round on the fleet was invisible on the dashboard, `qb-board`
and the web board alike. That was tolerable while a dial only configured what a
review round costs; it stops being tolerable with `tempo` (#474), which is the answer to
*"is this fleet working right now, and how hard"*.

**DIALS IS THE FLEET'S SURFACE, NOT THE PANEL'S**
([#563](https://github.com/prisonblues/quarterback/issues/563)). Every dial in the registry
was `review_panel.*` until `spawn.max_sessions` arrived, which made the question urgent rather
than academic — and the answer turned out to be already shipped rather than open. The board
does not know what a dial IS: `app/api/dials.py` stores the name as opaque text and the value
as opaque JSON on purpose, and says in as many words that the client owns the vocabulary.
`tempo` (#474) has been drawn as a dial by both dashboards for releases while `BOARD_DIALS`
has never held it. So the channel was never the panel's; the only thing that was is two lines
of `harness_rules` which assume a dial names a key in `DEFAULTS`.

`Dial.applies` is that distinction, and it is deliberately one field rather than a second
settings channel — a fleet dial is validated, listed, offered by the picker and rendered by
the dashboards exactly like every other one, and it is simply never merged into a repo's
resolved rules, because there is no repo in the question it answers. It has no `DEFAULTS`
entry either, so the picker's `default` line is blank and says why: `spawn.max_sessions` falls
back to a per-machine file that no repo and no board can read. `tempo` (#474) and the plumbing
in #475 are the same shape, and they inherit this rather than each inventing one.

The panel sits at the top, above the seats, for the caps line's own reason: it is the
configuration every panel below it is running under. Each row is the dial, its value, the
layer it came from (`fleet`, or the repo), and what is left of it; the argument for it — the
board requires one on every write — is on the line underneath with who set it and when.
**A repo dial beats a fleet dial** of the same name, so the beaten one is counted in the
title as `overridden` rather than drawn as if it were in force. And `tempo` gets a cell of
its own on the caps line, beside the budget it is there to protect: the caps say what the
seats may spend, this says whether they are supposed to be spending it at all.

**An indefinite dial and an expiring one do not render alike**, which is the half of this
that is easiest to drop. A `tempo: eager` with forty minutes on it and one set indefinitely
are different situations; the countdown is the quiet cell and `no end` is the loud one,
because a dial that expires takes itself off the board with nobody remembering it while one
with no end stays until a person comes and clears it. That is
[#244](https://github.com/prisonblues/quarterback/issues/244)'s rule — being idle and being
broken must not look alike — applied to a switch instead of a queue. `—` is not used here:
on every other panel it means "nobody reported this", and an expiry that was never set is a
decision somebody made.

**Turning one is a `✎` on the row, and what makes that possible is a credential rather
than a looser gate.** `GET /dials` takes `app.auth.reader`, which the machine bearer token
passes, so reading was always free from a terminal. `POST /dials` took `app.auth.human` and
since [#591](https://github.com/prisonblues/quarterback/issues/591) takes `app.auth.delegated`
— **a person, or an agent holding its machine's own `X-Agent-Elevated` secret**. A bare
machine token is still refused, and that is the half of the argument that matters: every agent
on a box holds the same bearer, so nothing inside a request carrying only that distinguishes
it from a person. The delegated secret is a second credential a caller has to hold, and the
write records `set_via: "agent"` beside the agent's own name, so a dial an agent turned is
never mistaken for one somebody typed. The self-approval shape #85, #86, #78, #232 and #335
each settled separately is handled where it actually bites — `spawn.max_sessions` is the dial
whose subject is the agent reading it, and `qb-start` refuses an agent-set value outright.

What `human()` gained is a second **method**: `HUMAN_TOKENS`, `name:secret` pairs in
`API_TOKENS`' format, presented as **`X-Human-Key`** to the **agent host** beside the ordinary
bearer. `rich:<secret>` authors as `human/rich` — the same identity the edge produces, by a
different door.

**What separates this from the browser session considered before it is NOT provenance**, and
that correction is worth having because #479 is the record: a cookie would have authenticated
the same person to the same `human()` and recorded the same `human/rich`. Two things separate
them. **Scope** — an Authelia session is SSO for everything behind that edge, where a key is
this board only, which is most of the answer to what a rogue agent gains by finding one. And
**expiry** — a session rotates on a clock nobody here controls, so the capability breaks on a
schedule rather than when somebody decides; "nothing to babysit" is a correctness property,
not a convenience. (Both corrections are hermes/seat-quarterback-1's.)

**Authelia is not in that path, and that is the requirement rather than a detail.** The other
way to be a person here is an edge session, which expires on a wall clock: a dashboard built
on one goes dead whenever it lapses and stays dead until somebody re-mints it by hand. A key
rotates when somebody decides to rotate it and never otherwise. `edge-untrusted.conf` clears
the four `Remote-*` headers and `X-Edge-Auth` and nothing else, so the new header passes
through the agent vhost with no nginx change at all.

The `✎` opens an editor on that row: **value**, **reason** and **for** (`30m`, `4h`, `7d`, or
empty for a dial with no end), with the dial's name fixed — a dial is identified by its name,
so an editable one would create a second dial rather than change the one on screen. `ctrl+s`
saves, `ctrl+x` clears the dial and hands the repo back its own default, `esc` cancels. The
scope is stated in the modal before anything is written, because `fleet` and `this repo` are
two different settings with one name and it is the mistake you cannot see afterwards. A value
is **JSON where it parses** (`2`, `true`, `null`, a list) and the string it looks like
otherwise (`P3`, `eager`); an expiry is measured from the **board's** clock, so a box whose
own clock is slow does not have its "in four hours" refused as being in the past.

**The vocabulary is on the screen where it is asked for**
([#539](https://github.com/prisonblues/quarterback/issues/539)). Setting a NEW dial was four
empty boxes and one placeholder covering all 29 of them at once — `P3, 2, true, null`, four
value kinds in one line, which is what a form says when it cannot tell which dial it is on.
The name field now filters the settable dials as you type (`↓` walks them, enter or a click
takes one), matching on the half of a name a person actually remembers: `budget` finds the
five `review_panel.budget.*`, `enabled` finds the seats. The list is in `BOARD_DIALS`' own
order — the two floors, then what a cycle costs, then the futility brakes, the budgets and
the switches — because sorted alphabetically it opened on `enabled`, which switches this
repo's reviews off entirely and is nobody's answer to *what did I come here to change*.

**Scrolling the list says what each one does, and what it will take.** The line under the
value box describes the name under the cursor, not the name in the box — *the lowest severity
a fix pass may act on; under it is deferred, not fixed* — and the value box's own placeholder
becomes that dial's accepted values, `a severity band — P1, P2, P3, P4`. Both halves of "is
this the one I meant" move together as you read down the list, and the second half costs no
rows, which is the only reason they both fit a pane this size. The name box is untouched while
you browse: what is highlighted is being read, what is typed is what will be written.

That description had no home a program could read. Every dial's argument is a Python comment
beside its key in `DEFAULTS`, at whatever length it needed, so a screen could show a dial's
shape and never its point. `Dial.what` is a one-line summary of it — capped at two wrapped
lines at 66 columns, asserted by a test — and the argument stays where it was.

Once a name IS one of the 29 the list retires and the description grows into the rest of the
block: what the dial takes (`a severity band — P1, P2, P3, P4`), its default, what is in force
now and at which scope, and — for `enabled` and the per-seat switches — that it is **narrow
only**, which is invisible in the value and otherwise discoverable just by having a write
ignored. The two states are the two questions: *which one did I mean*, then *what do I type,
what is it now, what happens if I clear it*. They cannot both be on a 78×24 pane, and typing
again brings the list back.

The description, the names and the refusal line sit at the column the fields' own text starts
at, rather than at the panel's padding. An `Input` draws a border and pads inside it, so its
text begins three columns in while a bare `Static` begins at the edge — which put every line
describing a field three columns to the left of the field, down the middle of a form that is
otherwise one column. The title and the key line keep the edge: they frame the form rather
than belonging to a field.

Writing those descriptions turned up a dial that validated and then killed the run.
`reviewer_scope` takes `diff` or `repo` (`panel_core.REVIEWER_SCOPES`), and the board layer's
validator had `("diff", "increment")` — `increment` is `round_scope`'s word. So `repo`, the
documented value, was refused here and never applied, while `increment` passed, reached the
resolved config, and met `panel_seats.reviewer_scope`, which refuses an unknown scope with
`SystemExit`. `_SCOPES` is `("diff", "repo")` now and a test pins it against
`REVIEWER_SCOPES`; the constant cannot simply be imported, because `panel_core` imports
`harness_rules`.

`ctrl+s` refuses in the box rather than after it. `POST /dials` cannot do this: the board
stores `dial` as opaque text and `value` as opaque JSON deliberately, so a misspelt name or a
quoted `"2"` is accepted, stored, reported as in force, and then ignored by every harness
that reads it — the refusal used to arrive from a round hours later, on the old value. The
sentence is the harness's own (`harness_rules.dial_problem`) and the other three fields keep
what was typed into them.

**A bad VALUE is a refusal; an unknown NAME is a warning and then a write**, and the
asymmetry is the point. The table being consulted is the harness beside *this dashboard*, and
the two are installed separately — so a hard refusal would make a box one release behind a
box that cannot set a dial the rest of the fleet already applies. `tempo` (#474) is the
standing case: both dashboards draw it and `BOARD_DIALS` does not hold it. So an unrecognised
name says *nothing this box knows applies `tempo`* and the next `ctrl+s` sets it anyway;
confirming one name does not wave the next one through. A value for a name this box *does*
know gets no such benefit of the doubt — the kind came from the same table as the name, so
there is no version of the harness in which `max_rounds: "2"` is a value somebody applies.
Where the filter has narrowed to exactly one name, both messages say which: *— ↓ takes
`review_panel.max_rounds`*.

The scope moved onto the title line in the same pass, and not for tidiness: the picker cost
four rows a 78×24 pane did not have, and what a Textual modal does when it outgrows its
screen is clip whatever was composed last — which was the scope. `fleet` and `this repo` are
two settings with one name and it is the mistake you cannot see afterwards, so it is now the
line that cannot be the one to go. A test drives the modal at 78×24 and asserts every control
is drawn.

None of this is a second copy of the dial table. `qbdata.dial_vocabulary()` reads
`harness_rules.dial_specs()` at call time — names, kinds, defaults, directions, all still
settled by `BOARD_DIALS` and `DEFAULTS` — and a box that cannot read it gets `{}`, which is
*cannot tell* and never *nothing is settable*: the picker is hidden, the line under the value
says so, and the write goes through exactly as it did before, with the board as the only
judge. A form that refused there would leave the person at that keyboard with no door at all.

**And it says WHICH cannot-tell.** Three states end in an empty vocabulary — no
`harness/loops` beside the dashboard, a `harness_rules.py` that will not import, and a
harness older than the dial table — and `qbdata.dial_trouble()` tells them apart, because a
partial upgrade reported as an install that never happened sends somebody to look for a
directory that is sitting right there. It is the distinction the board layer already draws one
level up: `_dials_unreadable` is *we could not find out*, never *there is no dial*. The
failure is not cached either, so a harness installed while the dashboard is open is picked up
the next time the modal opens rather than at the next restart.

**What that credential costs is [#479](https://github.com/prisonblues/quarterback/issues/479),
and it is stated rather than implied**: the key sits on this workstation, readable by the
processes running here, so an agent that goes looking can find it and author as a person. That
is the trade — open it wide now, tighten later — and #479 carries the menu for narrowing it.
It is narrower than the design considered before it, a signed-in Authelia session, which is
SSO for an entire estate. It is also why the delegated **agent** credential
(`X-Agent-Elevated`, #480) is a *different* thing and stays narrow: that one is for an agent
acting unattended and names the endpoints it may reach. `/dials` was deliberately not among
them until [#591](https://github.com/prisonblues/quarterback/issues/591), which added it on a
direct ask — an agent told to turn a dial could not turn it. `POST /plan/scope` and `exempt`'s
`grant: true` did not move. Two credentials, two blast radii, and the dial one is bounded by
being reversible: the row is cleared rather than deleted, it can carry an expiry, and
`set_via` says which of the two turned it.

**Which door a dial came through is recorded**, and shown. `human/rich` is `human/rich`
either way — a person is one author however they arrived — so the identity alone cannot tell
a browser write from a key on a workstation, and the second is the one carrying the residual
above. `GET /dials` returns `set_via` (`edge`, `key`, `dev` or — since #591 — `agent`); the page
draws it as a chip beside the author and a dial row's detail line says *"set by human/rich
with a key"*. The first three are all a person, arriving by different doors; `agent` is a
different kind of answer, meaning no person was in the request at all and `set_by` names the
agent that made it. A row
older than the column says nothing rather than guessing, because a default there would be the
one value a reader must be able to distrust sitting in the field they consult to decide.

**With no key on the host, the panel is exactly what it was** — and that is every box until
one is deployed. `HumanClient.why_not()` is asked once per paint, the `✎` goes grey, the last
row says why in place of the verb, and a click opens `<board>/dials/view` instead. `d` opens
that page from anywhere either way, because the page shows every repo's dials at once where
the panel shows this screen's. This is
[#443](https://github.com/prisonblues/quarterback/issues/443)'s three shapes settled: the
dashboard has option (2), a credential distinct from the machine token, and degrades to option
(3), read-only plus a printed URL. #443 is why the fallback names the URL rather than implying
it — the record of a person told the reorder was theirs to do whose reply was *"i don't know
how to re-order"*.

Two variables configure it, in `~/.config/quarterback/config` beside the bearer:
`QUARTERBACK_HUMAN_KEY_CMD` — a **command**, for the reason `QUARTERBACK_TOKEN_CMD` is one, so
the secret is resolved at the first write rather than at startup and lives in that process and
nowhere else — and `QUARTERBACK_HUMAN_KEY` for a literal value in a test or on a box with no
`op`. The board half is `HUMAN_TOKENS` (or `HUMAN_TOKENS_FILE`, rendered by the op-resolver),
and with neither side configured nothing changes: unset fails closed, exactly as an unset
`HUMAN_EDGE_SECRET` does.

**None of these surfaces knows what a dial MEANS.** The harness owns the vocabulary
(`harness/loops/harness_rules.py`), the server image carries no `harness/` directory at all,
and a copy in a dashboard would be a second place a dial is written down — the confusion
#56's rule and #305 exist to end. So a `tempo` with no board dial reads `unset` rather than
naming a default, and a dial no harness recognises is stored, returned and ignored, loudly.

**Clicking starts work, not just navigation.** Each PR row carries a `⚖` and each issue row
a `⚒`; clicking one opens a confirmation showing the exact command, and confirming starts a
real session you can attach to, read and interrupt. Clicking anywhere else on the row still
opens the thing on GitHub. The keys are `o` open, `p` panel-review, `f` fix the selected
issue or plan item, `d` the board's dials page, `s` this project's rows or the whole
fleet's, `r` refresh, `?` the list, `q` quit.

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

**The plan is what says what the work is FOR.** AGENTS says who is here and what they hold;
it does not answer why. The plan is the board's — every repo's
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
unreadable on a pane, so an agent's row resolves each against the board and shows the plan's label
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

**And a third refusal, for when there is no snapshot to read.** Until the board has answered
— and while it is unreachable, which is not the same as an answer of "nobody holds anything"
— the ⚒ says `the board has not answered … so nothing here knows whether it is claimed` and
starts nothing. It is the same rule as the panel's own gate one level down: unknown is not
free, least of all at the click that spends a claim.

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
empty string for a screen with no dash**. It runs `qb-dash`, and there is nothing to
choose: one renderer, so `dash_cmd` no longer probes for one.

It took a while to get there. The plain renderer held this slot until #426, because
the clickable one keyed its seat rows by seat NAME, every screen numbers its seats
from 1, and a second screen anywhere on the box turned that pane into a `DuplicateKey`
traceback — a pane you look at when something is wrong must not be the thing that
breaks first. #208 and #209 closed that on 2026-08-20 (seat rows key on the tmux pane
id now, which is unique box-wide), and nothing pointed back at the decision the fix
released, so the workaround stayed four days longer than it should have.

**The `--can-tui` probe is gone with the renderer it chose between.** It asked whether
`textual` could be imported here, which was worth asking only while there was a lesser
thing to fall back to. Losing it also loses the trap it carried: it resolved `qb-dash`
on PATH a second time, so a checkout's `bin` ahead of the installed profile could have
the probe answer for one install while a different one did the running. `qb-dash` on
PATH is still the gate — with none installed the pane holds a shell and a line saying
which command to set, rather than the screen quietly being one pane short.

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
It warns and proceeds by default. Printed here rather than in the panes because a seat's
agent paints over anything printed before it.

`QB_SEATS_PACE` is the whole knob: `off` consults nothing, `warn` (the default) says it and
starts anyway, `obey` brings the seats up as **bare shells** and starts nothing. What a spent
window costs is the agents, not the panes — the refusal this replaced lived in the per-pane
wrapper and refused to create the *pane* (#540), which is refusing somebody a terminal over a
subscription. `obey` therefore means "this screen does not start agents right now", which is
what it was always trying to say, and a screen already built with no initial command does not
consult `qb-pace` at all because there is nothing to withhold. A `qb-pace` that is missing,
broken or slow costs the note and never the screen: 3 is `hold` and only 3, and everything
else means the gate did not run rather than that it passed.

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
prompt by a human who is not looking at the dash. It warns and proceeds by default.
`QB_SEATS_PACE=obey` carries the refusal, and it is **off by default**: at `hold` the seats
come up as bare shells, the screen says when the window comes back, and nothing is started.
`unknown` never withholds anything under either mode — refusing every
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

### `qb-line` — how much of the backlog could be ordered at all (#435)

`GET /merge-queue` computes an order and names its own blind spots. It has never
described a drain, because **nothing enumerates a repo's open PRs** — the queue holds
only the PRs whose agent happened to run `/fix-and-land` step 4a, so the ranking is over
four rows when the backlog is thirty-six, and one unenrolled straggler nulls
`suggested_order` for everyone.

```
qb-line                  this checkout's repo, every open PR
qb-line --base test      only PRs targeting one base
qb-line --json           the same answer as data
```

There is deliberately **no `--preland`**. An earlier cut had one and it could not keep the
tool's one promise: `preland.py` fetches the base branch's remote-tracking ref — its own
docstring says "the one write is `git fetch`" — and `announce_hold` POSTs to the board for
a HOLD, so a sweep would write once per holding PR. Run `preland.py --pr N` on the one PR
you are about to land, where its writes are somebody's deliberate act.

It walks the OPEN PRs and asks the same question of each, sorting them into five tiers
with the repair for each:

| tier | what it means | the fix |
|---|---|---|
| `never-panelled` | the board has no run for this PR at all, so it is in no class of anybody's collisions | run a panel round |
| `no-file-list` | the PR's **newest** run recorded no changed-file list — a 404 from `/review/collisions`, or a reach-back past it | run a panel round (#94 was the title-skip path) |
| `inconsistent-counts` | the run stored more paths than its own changed-file count admits to | re-record the run |
| `stale-evidence` | the list belongs to a commit the branch has left, or to a base the PR no longer targets | re-review at the head |
| `head-unknown` | a file list exists and which commit it describes could not be established | re-review at the head |
| `prefix-list` | GitHub caps a file list at 3,000 and this PR is over it | nothing — the collision count is a floor, not a gap |
| `orderable` | a complete list at the current head | — |

The headline is the number #435 says nobody has ever seen: **how much of a real backlog
the ranker could be computed over**, not how good the order is.

**IT FORMS NO QUEUE, ENQUEUES NOTHING AND MERGES NOTHING.** #435 asked for a driver that
enqueues; [#476](https://github.com/prisonblues/quarterback/issues/476) supersedes that
half, on the grounds that a central drainer is the shape this codebase has refused four
separate times in its own docstrings — `qb-seats`' "no orchestrator to lose", `qb-start`'s
"a spawner that read the plan and handed seat 1 the first item would be hub-and-spoke with
a hub that runs once", `app/review_queue.py`'s "a drainer that also ordered would be the
hub-and-spoke shape `qb-seats` was written to refuse", and `app/api/landing.py`'s "not an
orchestrator… not a ranker… not a trigger". Self-selection is the design, and the engine
#476 wants is a dial on the agent that already claimed the work. What survives is the
sensor, and this is it. `test_qb_line.py` asserts the refusal from the board's side —
every request it makes is a `GET`.

Three limits it states rather than hides. It enumerates at most 200 open PRs and **says
so when that binds** — the headline is a fraction, and a silently truncated denominator
makes it read better than the truth. `/review/collisions` answers over the PRs this
board has panelled, so a rival it has never seen is in no class at all — which is why
`never-panelled` is a tier here rather than a missing row. And every tier is judged on the PR's
**newest** run, because that is the run the ranker uses: `merge_queue` takes one
unconditional `DISTINCT ON (pr) ORDER BY ts DESC` with no file-list predicate, while
`/review/collisions` reaches back past its window for the newest run *bearing a list*. On a
PR whose newest run recorded nothing the two disagree, and following collisions would call
it `orderable` while the queue counts it blind. The collisions response carries no
`head_sha` either, so the run's commit comes from `/reviews` — and where it cannot be
established the PR is `head-unknown`, never `orderable`, because `orderable` is the one
tier here that is a safety claim.

### `qb-backfill` — the collision datum, recovered from the forge (#449)

`suggested_order` (#80) is published only when **every** queued PR's evidence is attested:
measured, complete, internally consistent, and taken at the commit the queue has that PR on.
So one branch panelled before #94 — a real review that recorded no file list, because nothing
stored one then — turns the ranking off for the whole queue. A repository with thirty open PRs
is thirty chances to be that one, and the field was null on both this repo and `lexray`.

#94 said no backfill was possible and was right about the thing it meant: those panel payloads
are gone. The underlying fact is not. The forge still knows which files every open PR touches,
and #94's own nullable columns exist to carry a file list on a row that states no review
happened.

```bash
qb-backfill                     # dry run over this checkout's repo
qb-backfill --repo owner/name   # an explicit repo — any repo, not this one
qb-backfill --apply             # write the rows
qb-backfill --pr 12 --pr 34     # just these open PRs
qb-backfill --json              # the whole answer as a document
```

```
0  every open PR is answered for
1  at least one is not: the forge would not say, the board refused it, or GitHub's own
   list came back short of GitHub's own count
2  could not start — no repo, no `gh`, no board. NOT a statement about any PR
```

**It never claims a review.** The row carries `reviewed: false`, a `skip_reason` naming the
tool, the issue and the commit it read, and nothing that could be read as a verdict — no
findings, no scorecards, no stop, no confidence. `GET /reviews` hides it by default;
`/review/stats`, `/review/spend` and `/review/findings` exclude it under #94's
`reviewed IS NOT FALSE` rule; `app.api.plan._pr_evidence` still takes its findings from the
newest run that actually reviewed. What changes is the one thing it is for: the newest run per
PR now holds a file list, so `app.ranking` can attest it.

The absences are load-bearing. A single finding beside `reviewed: false` makes the board drop
the flag to NULL — "nobody said" — which is exactly the pre-#94 state these rows exist to
leave.

**A short list is recorded as short.** `changed_files_total` is GitHub's own `changedFiles`
and never `len(files)`: agreeing by construction is not evidence, and deriving one from the
other would delete the comparison `app.collisions.files_complete` is built on. Four ways the
stored list could be a prefix, all four reported — GitHub caps a file list at 3,000; a paged
read that dies partway is refused outright rather than recorded short; the board's own
`changed_files_dropped` is read back off the write and believed over what was sent; and the
head is re-read AFTER the list, because the two `gh` calls are not one snapshot and a push
between them would store commit B's paths under commit A's sha — which reads complete whenever
the two commits happen to touch the same number of files. A prefix leaves the PR unattested and
the run exits 1, which is the right outcome: it keeps `suggested_order` null rather than ranking
a branch by files it never listed.

**One hole is left open and reported rather than closed: renames.** GitHub counts a rename as
one changed file, so a row holding only the destination path is `complete` while another PR
editing the SOURCE path collides with it invisibly. `review_run_files` has one path column and
no notion of an alias, so storing the old path too would make `files_recorded` exceed GitHub's
count and fail #80's `counts_agree` — the PR would go unattested for having read more than
anyone else does. `loops/panel.py` has the same hole and says so. This counts them per PR, so
an operator can see where the grain runs out; the grain itself is #453.

The list comes from `gh api --paginate .../pulls/N/files`, not `gh pr view --json files`.
`pr view` asks GraphQL for `files(first: 100)` and does not page, so a 322-file PR arrives as
100 paths and exit 0 (measured, on `kubernetes/kubernetes#141360`). `loops/panel.py` lives with
that because its list is a side effect of a review and it prints a note when the two counts
disagree; here the list is the whole product.

**A re-run on an unchanged PR moves nothing.** Four guards, for four different failures.
Before writing at all, a PR whose newest run already carries a complete list at the head the
forge reports now is left alone as `already`, so it never shadows a run that already answered —
and one whose newest run is a backfill of this tool's own at that head is left alone too,
because re-reading the same forge would store the same shortfall. The `run_key` carries the
repo, the PR, the head and the run this one supersedes, so a second write of the same fact
meets the board's unique index; that is the guard that holds when two agents run this against
one repo at the same moment, which no read-then-write check can cover. And a write refused as a
duplicate is not taken as done: the newest run is read again and the PR is only reported
answered if it now attests.

The `already` check reads the stored list and deliberately not `reviewed`: whether a seat ran
is not what makes a file list usable, and sparing reviewed runs would leave the pre-#94
population permanently unanswerable.

A PR that pushes gets a new head, both guards open, and the new commit is recorded. #80 attests
a list only against the commit the queue has the PR on, so a row recorded against a head the
branch has left is worth less than nothing — expect to re-run this on a busy queue.

**It refuses rather than guesses** (#414): `--repo`, or the origin of the checkout at `--path`,
and if neither says then it stops rather than falling back to a default and reporting a
confident nothing-to-do about a repository it never found. A `gh pr list` that comes back
exactly `--limit` long says so and exits 1, because a partial sweep must not read as a whole
one. Dry run by default, `--apply` is the only thing that writes, and an argument it does not
understand is refused rather than treated as consent.

`GET /review/collisions` is untouched. It gained no predicate in #94 and gains none here — a
backfilled run enters the same unconditional newest-run selection as any other, and is
classified afterwards like any other.

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
qb-doctor --explain           # why each bad row matters, and the brief written for it
qb-doctor --only semantic     # the model-backed rows — NOT run by a bare qb-doctor
qb-doctor --announce          # put every FAILING row on the board (for a timer)
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
queue      prisonblues/quarterback@main    nothing is queued to land on main                  ok
unpushed   ~/source/quarterback            27 commits on 12 branches on no remote, oldest 19d  FAIL
landed     prisonblues/quarterback         2 PRs ready, tip of main committed 7h 20m ago    FAIL
tags       origin/main                     every release on origin/main is tagged, on-ref     ok
generated  ~/source/quarterback            none of 2 open PRs edits CHANGELOG.md              ok
stamper    ~/source/quarterback            nothing lets a branch write a version number       ok
briefs     …/share/quarterback-harness     fix-and-land.md runs a removed command           FAIL
```

Rows are grouped by which question they answer, and `--only` takes a group name as well as a
row name. `host` is the first two questions; `landing` is the third.

#### `unpushed` asks the landing question one step earlier (#567)

`landed` asks whether finished work is reaching the branch it has to reach. `unpushed` asks
whether it has left the disk it was written on. On zeus, on the afternoon #567 was filed,
eleven branches carried twenty-eight commits that existed on no remote anywhere, the oldest
nineteen days old — with ninety-four worktrees to hide them in and nothing on the box saying
so. If the disk had failed, that work was gone.

The query is exact and that is what makes it cheap:

```bash
git rev-list --count <branch> --not --remotes    # reachable from here, and from NO remote ref
```

**The obvious near-miss is much worse, and the row deliberately does not build it.** Compare
each branch to its own `origin/<branch>` instead and, measured the same afternoon on the same
hundred branches, you get eight branches carrying 133 commits — of which **five are entirely
ancestors of `origin/main`**: a merged branch whose remote was deleted keeps its local merge
commits, and comparing against a ref that no longer exists calls every one of them lost. The
same comparison also *misses* seven of the twelve real cases, because a branch that was never
pushed has no `origin/` ref to be compared against at all. Wrong five times in eight, silent
seven times in twelve. `--not --remotes` asks the question a person actually means.

**Age is the verdict, not the count.** Something is always in flight, so a row that failed on
a non-zero count would be red on a healthy afternoon and skipped within a week. Under
`UNPUSHED_GRACE_HOURS` the row reports the number and stays `ok`; over it, the row fails and
names the oldest date. There is deliberately **no stored baseline** — "3 new since you last
looked" would put the verdict at the mercy of a state file, would go green on the nineteen-day
backlog that is the actual exposure, and is answering a premise that is false anyway: this
count falls the moment a branch is pushed, cherry-picked or deleted. It moved 11 → 12 while
the row was being written, because another agent committed on a branch mid-measurement.

**The remedy is a decision per branch, so it is a `human` brief** (#408): push it,
cherry-pick the part worth keeping, or delete it. Several of these are abandoned experiments
and pushing them all would litter the remote with branches nobody wants; on zeus at least one
of them duplicated work that had already landed by another route. Nothing automated may make
that call.

A host that cannot make the measurement gets `unknown` and never `ok`. That covers a fetch
that failed, a configured remote that is not there, and — Codex's finding on this change — a
remote whose refspec does not bring back `refs/heads/*`, since a single-branch clone would
otherwise report every feature branch on the server as work that exists only on this disk.
**Every** remote is refreshed, not only the configured one, because `--not --remotes`
subtracts the tracking refs of all of them.

**And the same four refusals as the sweep, because it is the same question (#611).** A
negative refspec, a destination outside `refs/remotes/`, a ref under `refs/remotes/` that no
refspec writes to, and a `config` read that failed as against a remote with nothing
configured — `qb-catchup` grew all four for #573 and this row grew none of them, so on any of
those configurations the sweep refused and this answered, about the same commits on the same
disk. The logic cannot be shared (one tool is shell, the other Python), so what holds them
together is `test_the_two_tools_refuse_the_same_configurations`, which runs **both** against
one checkout per configuration and fails the moment either side grows a guard the other
has not. A remote that fetched cleanly and holds no
branches is not an unknown: the query succeeded, and the answer is that nothing here has
ever been pushed.

Two limits it states rather than hides: commits on a **detached HEAD** are invisible to
`--branches`, and a branch **deleted upstream without having been merged** since the last
fetch still has a local `origin/` ref that makes its commits look safely elsewhere. The
second is a deliberate trade — `qb-doctor` never fetches with `--prune`, because pruning
deletes the refs that keep those commits reachable, which is a way to lose exactly the work
this row counts. On zeus the day this landed, `git fetch --prune --dry-run` named no such
refs.

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

#### A row is a verdict, an explanation, and a brief (#408)

A row used to be a verdict and a one-line remedy, and every finding therefore became a
person reading it, working out what it meant, and hand-writing a brief for whoever would
fix it. Four findings in two days had exactly that shape — #414, #419, #422, #411 — and
the hand-written brief was the same length each time. Diagnosis scales by adding checks;
that step scales by adding people, which is the one thing that cannot be done.

So a registration carries three things beside the predicate:

```python
CheckSpec(
    "briefs", "landing", check_briefs,
    explanation=("A brief is what an agent DOES. #122 removed branch-side release "
                 "stamping from the code, and five agents stamped anyway — every one "
                 "of them following a document that still told them to. …"),
    briefs={
        "fail": Brief(
            audience="agent",
            task="…establish which of the two causes this is before changing anything…",
            constraints=("Do not edit files inside the nix store. …",),
            verify=("`pytest -k no_brief_tells_an_agent_to_stamp` still passes…",),
            needs=("telling",)),
    })
```

| field | what it is for |
|---|---|
| `explanation` | **why the row matters**, in prose. Per-*row*, not per-verdict — the same paragraph however the row came out, because it is about the mechanism rather than today's answer. |
| `briefs` | a `Brief` **per verdict**. A verdict that is not a key gets no brief, which is the default and the safe one. |
| `Brief.audience` | `agent` (this is work) or `human` (this is an escalation). They do not merge. |
| `Brief.task` | what to do, templated against the row's `extra` — the fixed half is the understanding, the variable half is what this run found. |
| `Brief.constraints` | what **not** to do, and what has already been settled. The half a generated prompt can never contain. |
| `Brief.verify` | how to know it worked. |
| `Brief.needs` | the `extra` keys the task is written around — an evidence gate, not documentation. |

**They are written by hand, at the same time as the check.** #408 is explicit that a
prompt generated from the error message afterwards restates the assertion, which is the
one thing nobody needed written down. The moment the understanding exists is the moment
the predicate is written, and until now the only place to put it was a docstring, which
nothing can reach.

##### `extra` is the documented variable half

Every row already populated `extra` with the facts a brief needs — `head_holder`,
`head_pr`, `waited_minutes`, `offenders`, `conflicting`, `orphaned`, `telling`,
`unreadable`, `differ`, `missing`. `needs` is what turns that from a debugging aside into
a contract: **a brief whose facts the row did not establish is not rendered at all.**

That is the whole of it, and it is the difference between this being useful and being a
confident-guess generator. `queue`'s brief says *go and ask whoever holds the head of the
line*; a `queue` row that could not read the queue has no head to name. Rendering it
anyway produces a specific, plausible, entirely fabricated instruction — which is #419's
family (an error rendering as a different and confident diagnosis) arriving at the
dispatch layer. **Absent and empty are both absent**: `offenders: []` is the row saying it
found none. A run that reaches this state gets no prompt and a sentence saying which fact
was missing, because `None` with no reason cannot be told apart from "nothing to dispatch
here", and those are opposite facts.

##### `manual` and a brief are different things

`manual` is one command line for a person and is never executed. A brief is a paragraph
nobody executes. Collapsing them either puts an unrunnable paragraph where the report
prints a command, or loses the command. `briefs`'s manual is `qb-bump --host $(hostname)`;
its brief is three paragraphs about which of two causes this is. Both exist on the row,
and the brief quotes the manual under a heading that says it is not the agent's to run
blind.

##### `unknown` splits in two

"This host cannot see it" — no token, no `gh`, forward-auth with no session — is a
person's or an installer's job, so its brief is an escalation (#279's `needs_human`).
"This repo is in a shape nothing here can parse" is agent work. Only the registration
knows which, so it is a per-verdict field on it and it defaults to neither.

##### Every agent brief ends by re-asking its own row

Appended by the tool rather than written into each registration, because it is a command
and not an understanding:

```
python3 <checkout>/harness/bin/qb-doctor --only briefs --repo <checkout>
```

#414 is an entire issue about an installer's exit code being read as the guard having
been installed — `qb-bump` once reported `nothing to carry` about a harness 74 commits
behind — and `apply_fixes` already draws the same line for `--fix`. It is spelled as a
path **into the checkout** rather than as a bare `qb-doctor` because of #422: the staler
a host, the likelier the remedy is missing from it, and the checkout is present by
definition.

##### The doctor produces the brief and never runs it

Finding a fault and starting an agent to fix it, ungated, is the same self-approval shape
#85, #86, #78 and #335 have each settled. So the brief travels out through the door the
finding already uses — `--announce`, into #274's needs-human post — and not on a channel
of its own, which would be a dispatch mechanism wearing an escalation's clothes.
`test_the_doctor_produces_the_prompt_and_never_runs_it` asserts that no subprocess this
tool spawns carries one.

The report does not print briefs by default either: seventeen rows and twenty lines each
would bury the one word a reader came for. The footer says how many are ready and **which
rows have none**, because "no brief is written for this fault" is a fact about the tool
that a reader should be able to see.

#### The `landing` group — can work actually get out of here? (#407)

The first ten rows all ask a variant of *"is this host wired up"*, and on the night of
2026-08-22/23 every one of them was correct: **9 ok, 0 warn, 0 fail, 1 unknown**. In the
same minute the merge queue held seven green pull requests with none ready, `main` had not
moved in over three hours, `refs/tags/v3.8` pointed at a commit that is not in main's
history, and three branches were conflicting on the one file `changelog.d/` exists to keep
them out of. Nothing it checked was broken. Work simply could not land.

| row | asks | fails when |
|---|---|---|
| `merges` | can a merge here rewrite the commit a release tag was reserved against | something reserves at push time *and* squash or rebase is on |
| `queue` | is the line moving | `queued > 0 && ready == 0`, with the oldest entry waiting longer than a landing takes |
| `landed` | has the integration branch moved, given what is ready to land on it | a pull request is `CLEAN` and the tip of `main` is two hours old |
| `tags` | do the release tags point into the history they claim to tag | a landed release's tag is off the integration ref (#406) |
| `generated` | is any open pull request editing a file the release job writes | one of them touches `CHANGELOG.md` (#122) |
| `stamper` | can a branch in this repo write a version number itself | a branch-side stamper is reachable (#122) |
| `briefs` | do the briefs *this host would open* still tell an agent to stamp | one of them runs a removed release command inside a fence (#122) |

Three constraints bind all seven, and each was paid for before this group existed.

**Honest `unknown`, following the `edge` row.** Most of these need the board or GitHub, and
a landing group that went green having seen neither would be a worse version of the problem
it exists to solve. Every row that could not reach what it needed says so and says which
thing: no board configured, no token resolved on this machine, no `gh`, a `gh` that could
not answer, a queue whose counts could not describe a real queue, a `mergeStateStatus`
GitHub has not computed yet, a brief that could not be read, a tag report whose exit code
and contents disagree. **Reaching the read cap is one of them** — a row that saw the first
`PR_SAMPLE` open pull requests and found nothing has not established that there is nothing,
so it says `unknown` rather than disclosing the cap and passing anyway. A *finding* among
the ones it did read is still a finding, so the fail branches come first.

`test_no_landing_row_goes_green_on_a_host_that_can_see_nothing` asserts this over the whole
group rather than row by row, because the failure it guards against is a row *added later*
without it: it builds a repo in scope for every row and a host that can answer none of
them, and requires every row in the group to say `unknown`.

**No failing on irrelevance.** An empty queue on a repo with no pull requests is the healthy
state. A repo with no `changelog.d/` is not doing releases out of fragments, so neither
`generated`, `stamper` nor `briefs` has anything to say about it. A repo with no release
tagger has nothing here that can reconcile a tag against its history. The condition is
always "this mechanism is in use *and* it is not working", never "this mechanism is not in
use".

**Diagnose, do not act.** Nothing here merges, re-points a tag, or evicts a queue entry.
#405 argued the queue must stay advisory and the argument held up under measurement — the
system was never wrong about who should land next, only about who was still there. So the
stalled-queue row carries no `fix`; what it carries is the name of whoever holds the head,
because a stalled queue is somebody to talk to.

##### `--announce` — the caller these rows were missing (#405)

This group had the right predicate on the night it was written for and nobody ran it. A
doctor is a command a person types, and the whole of #405 is that nobody was there to type
anything: seven green pull requests, zero ready, `main` unmoved for over three hours, every
surface green, and it took a human noticing that a version number had not changed.

So the missing piece was never a second predicate — it was a caller. `--announce` puts every
**failing** row on the board through the needs-human door (#274), which is the one place the
harness escalates from, and
[`loops/systemd/qb-doctor-landing.{service,timer}`](loops/systemd/) run it every fifteen
minutes:

```bash
qb-doctor --only landing --announce --quiet
systemctl --user enable --now qb-doctor-landing.timer
```

- **`fail` only.** An `unknown` is a check that could not be *made*, and its ordinary causes
  — no network, no `gh`, no board token — hold for hours, so on a timer they would announce
  forever. Every unknown is still printed in the report, which is where that distinction
  earns its keep; the unattended door carries established findings.
- **The dedupe key carries the head PR.** #274 does not repeat a key inside twelve hours,
  which is what keeps a timer quiet — and a key that named only the row would let a second
  stall, behind a different pull request, be swallowed as if it were the first. Protection
  from noise must not become suppression of news.
- **Announcing changes nothing else.** It writes to stderr, so `--json` stays a document a
  caller can pipe into `jq`, and the exit code is still the report's. An escalation that
  cannot be posted is still printed: the finding stands whether or not the board took it.
- **Still advisory.** Nothing in the unit merges, evicts or re-orders anything. It says the
  line has stopped and names somebody to ask.

#### The `semantic` group — the one question no predicate can decide (#408)

**Python first, and Python wherever a predicate can decide it.** An LLM check that
duplicates a predicate is strictly worse — slower, costlier, non-deterministic — so this
group holds exactly one row, and it holds it because the question genuinely cannot be
written as a predicate.

`briefs` reads the **fenced code blocks** of the briefs this host would open, with a
CommonMark scanner, and deliberately leaves prose alone: a paragraph explaining that
stamping used to happen and no longer does is the removal working, and a `grep` cannot
tell that paragraph from an instruction. `instructions` is handed those same documents
**with the fences taken out** and asked the one thing the scanner cannot answer — does
this prose still *direct* a worker to produce a release number? On the night of
2026-08-23 five agents stamped a release and every one of them was following a document.
The sentence that told them to need never have been inside a fence, and on the machine
this was written on, against the harness that host was carrying at the time, it was not:

```
instructions  …/quarterback-harness/commands  fix-and-land.md still tells an agent in
                                              prose to stamp or reserve a release      FAIL
    quote: **Once READY, before you push: the release entry, then its number.**
```

That host was bumped an hour later and the row went `ok` against the new store path,
whose `fix-and-land.md` does not carry the sentence — which is the row working, not the
row wavering. It is worth saying plainly because the two answers look like a flake and
are not one: the question is *what does this machine read*, and what this machine read
changed.

##### One document per question, and a `clean` said twice

A model asked to find one sentence across thirteen documents is being asked a different
and harder question than one asked about a single document, so the unit is one document.
It costs no more — the same bytes are read either way and only the per-call overhead
multiplies — and it buys two things beside reliability: a finding is scoped to the file
it could have come from, so the citation check is tighter, and the cache is per document,
so editing one brief re-asks about one brief.

A `telling` answer is accepted on one reading, because it arrives carrying evidence: a
filename from the manifest and a sentence the wrapper has already found in the text. A
`clean` answer is asked again, under its own cache key, because it carries nothing at
all — it is an assertion that something is not there, made by a process that cannot show
its work, and that is the claim this whole file exists to distrust. Disagreement resolves
toward the finding. Anything else is `unknown`.

##### Its honest `unknown` lives in Python, not in the model's answer

An LLM check that returns `ok` because it found nothing is the precise failure this whole
tool exists to catch, and a model that has seen nothing will still answer confidently. So
the abstention is enforced in three places, only one of which is prompt design:

1. **The evidence gate**, before the call. `gather_evidence` builds the manifest and
   refuses it whole: nothing readable, *one* file unreadable, or more bytes than the
   ceiling allows, and the row is `unknown` **without the model having been asked**. This
   is the `edge` row's shape — "nothing here can see whether the secret is set" is decided
   by the code, not by the thing being asked. Reaching the byte ceiling is an `unknown`
   and never a disclosed pass, which is the hole #417 closed in the GitHub-backed rows.
2. **The call wrapper.** `ask_model` is the one place "could not be asked" is phrased: no
   CLI, the switch off, a non-zero exit, a timeout, no JSON, a verdict outside the closed
   vocabulary, the model's own `cannot tell`, the per-call dollar ceiling reached — and
   two clauses no prompt can enforce. **An answer citing a file it was not given is
   discarded**, because it is the model reasoning from its own memory of this repository
   and the row cannot show a reader the evidence. **An answer quoting a line that is not
   in the text is discarded**, because a composed quotation is not evidence. Both discards
   are `unknown`, never `clean`.
3. **The prompt**, which offers `cannot tell` and says it is never a polite way of
   agreeing. Weakest of the three on its own, which is why it is third.

`test_no_semantic_row_goes_green_on_a_host_that_cannot_ask` asserts this over the group
rather than over a list of names, the same way the landing group's meta-test does.

##### And it is bounded three ways

- **Selection is the bound.** A bare `qb-doctor` does not run this group. It is a command
  people type when something already feels wrong, and #55's argument is that unbounded
  spend must not hang off that. `--only semantic` runs it; a timer that wants it asks for
  it by name (`--only landing,semantic --announce --quiet`).
- **Every call carries a ceiling the CLI enforces** (`--max-budget-usd`) and a timeout,
  rather than an estimate this file makes — so it holds even if this file is wrong about
  what a call costs. `QB_DOCTOR_LLM=0` turns the group off entirely, for a sandbox with no
  network or a host whose owner does not want model calls made from it.
- **The answer is cached on the digest of what was read.** A scheduled re-run over
  unchanged documents costs nothing at all, and an edit to any one of them asks again.
  That is how the expensive half stays affordable without being
  opt-in-and-therefore-never-run.

##### What it records

`read` (the filenames), `bytes`, `digest`, `model`, whether the answer was `cached`, and
on a finding the `file` and the **verbatim quote** — which is what makes a wrong verdict
arguable rather than merely distrusted. The brief for this row says so in as many words:
read the sentence in place first, and if it turns out to be a description rather than an
instruction, the row is wrong and saying so is the result.

##### `queue` — the pair, and the clock

`queued > 0 && ready == 0` is not a fault on its own: it is the normal state of a queue
whose head is mid-preland. What is a fault is the oldest entry having sat in that line for
longer than a landing takes. PR #398 was landed twice and timed at 5m37s and 12m59s from
merge to green, so fifteen minutes sits inside the noise of one slow landing; the threshold
is thirty, which is more than double the slowest measured landing and still turns a
three-hour stall into a half-hour question.

The clock is the entry's own arrival time, not how long `ready == 0` has held — the board
states the former and nothing records the latter. An entry that arrived in the *future* is
clock skew or bad data, so it is discarded rather than read as "just now".

##### `landed` — both halves, and what "moved" honestly means

A quiet `main` is not a fault and a green pull request is not a fault; together they are the
definition of work that cannot land. Readiness is `mergeStateStatus == CLEAN` — GitHub's own
answer to *"would a merge succeed right now"* — rather than a rollup of check conclusions,
which would agree with it until somebody added a required review or a merge-queue rule and
then disagree silently. GitHub computes it lazily, so `UNKNOWN` is counted as unknown and
never as unready.

The row says **"the tip of `main` was committed 4h 10m ago"**, and that wording is exact
rather than modest: GitHub's REST API states no ref-update time for a branch, so what is
read is the tip commit's committer date. For a repo that lands with merge commits the two
are the same instant, which is what the `merges` row is about keeping true. Where they part
is a fast-forward of an older commit, which reads as an older move than it was — so the row
can ask its question early on a repo that fast-forwards, and cannot read late.

##### `tags` — a reservation is not an orphan, and only one thing can tell them apart

A tag off the integration ref is two things wearing one face: a release that has not landed
there (a local cut not yet pushed, or a pre-#122 reservation), which holds a number nothing
else will hand out and is fine; or a release that *has* landed and whose tag is elsewhere,
which is #406. One `git merge-base --is-ancestor` cannot make that call — it needs the
CHANGELOG at the ref to say which releases have landed. Reporting reservations as findings
would train people to ignore the row.

One row, because there is one invariant: *a tag `vX.Y` points at a commit whose CHANGELOG
declares `vX.Y`*. That is `release_tag.py`'s own single sentence, and `untagged`, `misplaced`
and `orphaned` are the three ways it can be false, not three separate questions.

**This row executes a program out of the repository**, which `merges` deliberately does not.
The two ask different questions: `merges` asks what a tool *is*, and running a program to
find that out is the hazard itself; this asks what the repository's *tags are*, and the
tagger is the only thing that can answer, because what counts as a release heading here is a
masked-markdown question and this repo's CHANGELOG quotes `## vX.Y` inside examples. The
parse that precedes the run is a **compatibility gate and not a sandbox** — module-level
code runs before any argument is parsed — and what it buys is that an old or foreign tagger
is never invoked with a subcommand it does not have. What makes running it acceptable is
that this is the repository's own release tool, which its pre-push hook and its CI already
run on every push. What is *not* trusted is the answer: the exit code, every field and every
field's type are checked before a verdict is reached, and a report that exits `0` while
naming findings is an `unknown` rather than a half-believed one.

##### `generated` — asked before the conflict, not after

#407 proposed this as *"are open pull requests conflicting on CHANGELOG.md"*, which is the
same fault observed one step later. The **edit** is the fault; the conflict is only its
commonest consequence, and a row that waits for the conflict cannot fire on the first branch
to make it. The conflict count is reported beside the finding because it says how urgent it
is, not because it is the test.

Two things it does not see, both written down because a limit nobody wrote down is a limit
nobody remembers. The README's release list is guarded by the same rule and is not asked
about here: the guard exempts the rest of `README.md` so that documenting anything is not
taxed, and a list of changed paths cannot tell an edit to the release list from an edit to
the installation instructions. And a **rename away** from `CHANGELOG.md` is missed, because
GitHub reports the resulting path — a different and much rarer fault than writing an entry
into it.

##### `stamper` and `briefs` — two sites, two rows

They were one row for one commit, and Codex was right that they should not be: a stamper in
the repository and a stale brief on this machine are two reasons to fire with two owners and
two remedies, and the code was already choosing a different `manual` depending on which had
happened. One row with two premises is exactly how the `merges` row drifted.

`briefs` is the one that is not about the repository at all. Five agents stamped a release on
the night of 2026-08-23 and every one of them was following a document — *a mechanism
removed from the code and left in the brief has not been removed* — and the briefs on a host
are the ones the **harness on PATH** ships, which is a different set from the repository's
the moment that harness falls behind. `test_no_brief_tells_an_agent_to_stamp_a_release` pins
what this repo *ships*; this row asks what this box *reads*, and on the machine it was
written on the two disagreed:

```
briefs  …/share/quarterback-harness/commands  fix-and-land.md, fix-and-review.md,      FAIL
                                              panel-review-pr.md run a release command
                                              #122 removed, in a code block
     -> qb-bump --host $(hostname)   # if …/commands is a stale harness
```

Code blocks only, the same line the test draws: prose may explain what used to happen and why
it does not any more, and a fence may not carry the command. The fence scanner is
CommonMark's rule rather than a regex over ``` — a `~~~` fence counts, a four-backtick block
is not closed by the ``` inside it, and an indented fence is read, which matters because
every fence in `fix-and-review.md` sits inside a numbered step. Inside a fence, whole comment
lines and heredoc bodies are dropped; **quoted spans are kept**, which is the opposite of how
the `merges` row reads a hook, because briefs quote the paths they run and emptying quoted
spans stopped this seeing `python3 "$WT_DIR/scripts/release_stamp.py" preflight` — the very
line it exists for.

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
qb-bump                     # pull; if stale, prepare the bump, BUILD it, propose it
qb-bump --json              # the same answer as a document
qb-bump --apply             # the WHOLE job, by a PERSON: pull, bump, build, switch
qb-bump --apply --dry-run   # all of that except the switch, whose command is printed
qb-bump --apply --cached    # switch onto the cached proposal; prepare nothing
qb-bump --no-pull           # compare and build the two trees exactly as they stand
qb-bump --no-wrapper        # switch with `sudo nixos-rebuild`, not this host's `rebuild`

#   exit 0  nothing to carry — the harness on PATH is this checkout's
#   exit 1  cannot tell — not in a quarterback checkout, a harness row that is
#           neither ok nor fail, no qb-doctor, no nix, no consuming flake, more
#           than one, or a checkout that could not be brought level with origin
#   exit 2  a bump is prepared and BUILT; --apply switches onto it
#   exit 4  the bump does not build — refused to propose it
```

**It says what it is doing.** Two `git fetch`es, a `qb-doctor`, a scan, a `nix flake
update` and a whole NixOS build used to run without printing one word until the last of
them finished — which on a box that has to compile is forty minutes of a cursor and no
output, indistinguishable from a hang. Each slow step now narrates to stderr before it
starts and reports what it found after, and nix's build output is written to
`~/.cache/quarterback/harness-bump/build.log` **as it happens**, so `tail -f` on it works
while the build is running. `--json` turns the narration off: stdout is the report, and a
caller redirecting both streams into a parser is a normal thing to do.

**It pulls before it compares, because a comparison against a stale checkout is worth
nothing.** The drift verdict answers *"is the harness on PATH the one **this checkout**
has"*, so a checkout twenty commits behind origin agrees with an installed harness twenty
commits behind and the answer comes back "nothing to carry" about a box that is nothing of
the sort. #414 closed this for a checkout that was the wrong *directory*; #533 closes it for
the right directory at the wrong *commit*. Both trees are brought up to date first — the
quarterback checkout because it is what the comparison is against, and the consuming flake
because its HEAD is what gets built.

**The pull is `fetch` plus `merge --ff-only`, and the refusals are the feature.** A tree with
a local commit or a conflicting edit is *reported*, with the tree exactly as it was found;
nothing here merges, rebases, resets or stashes, for the same reason the harness refuses
those outright in a shared tree. A tree with no upstream is not a failure — there is nothing
to pull it up to, and saying so and stepping over it is what stops every worktree on this
fleet answering "cannot tell" forever. A fetch that **fails** or a fast-forward that is
**refused** is the other case: there is a remote and this could not get level with it, so a
`current` verdict downgrades to `unknown`/1. Having pulled the consuming flake, it also
*acts* on it: a commit that landed there from another box is a rebuild this machine owes even
when the harness pin has not moved.

**It does not detect anything.** The drift verdict is `qb-doctor --json --only harness`,
read and not re-derived. A second comparison here would be a second opinion about a fact
that already has one, and the two would disagree the day one of them learned something —
`_same_after_packaging`'s handling of `patchShebangs` and `wrapProgram` is exactly the kind
of thing only one of two copies ever gets right. The suite asserts this behaviourally: a
stub doctor reporting `ok` is believed, whatever any other measurement of those directories
would say.

**The ceiling is `sudo`, and it is designed around rather than fought.** A
`nixos-rebuild switch` needs root. An agent has no root and should not go looking for it, so
the agent path stops one step short of it — pull, prepare, build, prove. That is the ten
minutes; the `sudo` is the ten seconds. `--apply` refuses without a terminal, so a timer, a
CI job or an agent that invokes it changes nothing and prints the command instead. What
`--apply` no longer does is *refuse a stale proposal*: it runs the whole preparation itself
and switches onto what it has just proven, rather than onto what somebody proved an hour and
three merges ago. `--apply --cached` is the door back, for a host that lost its network
between the preparation and the person. It also raises no needs-human escalation — the human
is holding the keyboard, and #274's door is not a logbook.

**The switch goes through this host's `rebuild` wrapper, because `nixos-rebuild switch`
lies.** It prints *"Done. The new configuration is …"* even when
`home-manager-<user>.service` has failed, so `home.file` links, user units and dotfiles do
not apply and the switch says nothing about it. For `qb-bump` that is not a neighbouring
subsystem's bug: the harness scripts it exists to deliver arrive through exactly that
activation, so a bypassed wrapper reports #267's own failure as a success. The wrapper is
**called, not reimplemented** — the same argument that makes the drift verdict `qb-doctor`'s.

It is **read** to decide that, never run: there is no `--print-target` to ask, and finding
out by executing an arbitrary script on an arbitrary host is a worse trade than a read whose
worst outcome is falling back to a command that was already correct. Whole-line comments are
dropped — a commented-out `flake=` naming the right directory, above a live
`--flake "path:$other#rescue"`, is the decoy that made that necessary — and the answer is
used only when exactly one flake directory is named and it is this one. None, two, or
somebody else's all mean *cannot tell*, which means the explicit
`sudo nixos-rebuild switch --flake` this file resolved itself; `--no-wrapper` asks for that
outright.

**This is a heuristic, and it is worth being precise about what it does not establish.** A
regex is not a shell parser, so a target assembled out of variables can hide from it. And
the *attribute* is not checked at all — it cannot be, because it never appears in a
wrapper's text: a wrapper derives it from the live hostname. That is precisely why the
wrapper is used only when the attribute was **matched** rather than named. `resolve_attr`
matches this machine's `hostName` too, so wrapper and bump agree by construction; `--host
laptop` run on a desktop agrees only by luck, and would switch this machine onto a
configuration the run never built — so `--host` refuses the wrapper outright.
`QUARTERBACK_REBUILD_CMD` (environment or site config, or `consumer.rebuild` in the module)
is the door for a fleet whose wrapper is spelled differently — a declaration is consent and
skips the check.

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

**And it knows its own handwriting (#537).** Leaving the lock modified meant a successful
`--apply` created the exact state the *next* run refused — "flake.lock has uncommitted
changes … commit or revert that file first" — which since #533 fires on the second command
rather than in some corner. The refusal's reasoning is untouched: an uncommitted lock is
normally a nixpkgs bump somebody was part-way through, and preparing against HEAD would
discard it. But the cache records precisely what was last written (`Proposal.new_lock_sha`),
so "somebody's in-flight bump" and "the lock I installed ten minutes ago" are
distinguishable rather than both being "not HEAD". A working-tree lock that hashes to the
cached proposal's, for that same flake, is this tool's own output and is prepared over. A
lock edited since, a proposal for another flake, a cleared cache — all still refuse.

#### Finding the quarterback checkout it compares against

The drift is a comparison between two directories, and one of them is a checkout of this
repo. `qb-bump` asks three questions in order, and **refuses** if none of them answers:

1. `--repo DIR`.
2. The working directory, when it is inside a checkout — the repository root is what gets
   used and named, so running it from `harness/tests` means the same thing as running it from
   the top.
3. `$QUARTERBACK_REPO`, then `QUARTERBACK_REPO` in `~/.config/quarterback/config` — the key
   `qb-env` writes, `qb-mcp` execs its venv out of, and `qb-doctor` reads as the *client*
   checkout. On this fleet all three are the same directory; the one this needs is the one with
   a `harness/bin` in it, and if what the key names has no `harness/bin`, that is said rather
   than worked around. Declaring it once is what makes `qb-bump` answer correctly from anywhere
   on the box.

"A checkout" means a git repository with a `harness/bin` in it, because `harness/bin` is
literally what is being compared. A `--repo` or a `QUARTERBACK_REPO` that names something
else is treated as a typo and refused rather than fallen back from.

**A `harness` row that is not `ok` is not an all-clear either.** The verdict is `qb-doctor`'s,
and only `ok` means there is nothing to carry: `fail` prepares a bump, and anything else — the
`unknown` that means "no harness on PATH (`create-worktree` not found), so nothing to compare
this checkout against", which is a machine with no harness on it at all — is `cannot tell`. That
one was found by Codex reviewing the fix below, one function away from it and the same mistake.

**With none of the three it says `cannot tell`, and that is the whole of #414.** It used to
resolve the checkout from the working directory and say so nowhere, so run from
`~/source/nix-fleet` it asked `qb-doctor` about a repository with no `harness/` in it;
`qb-doctor` fell back to the tree beside itself, which for an *installed* `qb-doctor` is the
installed harness, and a directory is always identical to itself. The verdict was `ok` and
the answer was `nothing to carry: the harness on PATH IS this checkout` — about a harness 74
commits and eleven releases behind, one minute before `qb-doctor`, pointed at a real
checkout, called it `FAIL — 10 differ`. "Nothing to carry" is a positive assertion of health
and a person who reads one stops looking, so an unanswerable question now exits 1 as
`unknown` rather than 0 as `current`, and every report — the no-op included — names the pair
of directories it compared.

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

**The system attribute is not necessarily the hostname.** It is on this fleet — `zeus` is
`nixosConfigurations.zeus` — and on a fleet like that nothing below ever runs. But it is not a
rule, so the hostname is a first guess checked against the flake's own attribute names, and the
fallback is to ask each configuration what `networking.hostName` it declares — correct, slow,
and defeated by a host that does not evaluate here at all, which is what `--host` and
`programs.quarterback-harness.consumer.attr` are for.

#### What `--apply` refuses, and why each one

The proposal is a claim that *one particular system* was built. Every refusal below is the
same sentence in a different place: what would be switched onto is not what was proven.

- **Nothing prepared** — only reachable under `--cached`; drop it and `--apply` prepares one.
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

pulled     checkout: 83fe1e7db2c8 -> f03644fd5787 (origin/main)
pulled     consumer: already level with origin/master
harness    /nix/store/…-quarterback-harness-0.1.0/bin
checkout   /home/rich/source/quarterback (the working directory)
consumer   /home/rich/source/nix-fleet (found by scanning /home/rich/source)
input      quarterback
pin        b35de2a5e638 -> eac457b385ff
built      desktop: /nix/store/…-nixos-system-zeus-26.05.20260707.0ad6f47

10 scripts arrive on PATH when a person runs:
  /home/rich/source/quarterback/harness/bin/qb-bump --apply

That writes the prepared flake.lock into the consumer and then switches this machine with
`rebuild switch`, which needs a password; nothing above it did.
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
  which puts it on the session's lease — where `/active`, `/overlap`, `/fleet`, `qb-board`
  and `qb-dash` all show it — and emits one `status` post on the live stream
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
the optional `QUARTERBACK_TOKEN_REFRESH_CMD`, `QUARTERBACK_AGENT`, `QUARTERBACK_REPO`, and —
for the dashboard's own writes — `QUARTERBACK_HUMAN_KEY_CMD`)
from `${XDG_CONFIG_HOME:-~/.config}/quarterback/config`, each overridable from the environment.
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
