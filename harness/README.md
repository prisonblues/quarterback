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
  `/fix-issue`, `/epic`, `/lander`, `/wt`, `/drop-worktree`, `/tree-shake`, …)
- `bin/` — the bash the worktree commands drive (`create-worktree`, `remove-worktree`,
  `prune-worktrees`, `worktree-holder`), plus `qb-stage`, which records the workflow
  stage a session is in for the statusline, `qb-seat`, which turns one pane of a
  multiplexer into a fleet seat with its own board identity, and `qb-board`, which
  launches the terminal board client (`qb-board --follow` tails the board to stdout
  on any host with ssh; see the repo README)
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
slugs are pinned in `.harness-rules` so that "codex found 9 issues" still means something
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
are the same thing to a merge. `.harness-rules` is where a repo turns that off deliberately.

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
seat it is the opposite — a stable, typeable `zeus/seat-3` instead of `zeus/a4f81c2e`,
which survives the seat restarting in the same pane because the board hands a returning
key its old name back.

**Why it registers that name itself, before starting anything.** Since v2.12 the board
*designates* the name half of an identity, and `QUARTERBACK_INSTANCE=seat-3` is only a
**request** (`X-Agent-Name`) — one the MCP server makes and the lifecycle hook does not.
Allocation is first-contact-wins, and the hook fires on `SessionStart`, so it usually wins.
Measured against a live board:

| First contact | Later request | Board says |
|---|---|---|
| key only, no name (the hook) | — | `zeus/meadow-russet` |
| key only, no name (the hook) | `seat-9` (the MCP server) | `zeus/meadow-russet` — **the request is ignored** |
| key **and** `seat-9` together | — | `zeus/seat-9` |

So a seat that does not ask up front comes up as two random words about as often as not,
losing the one property the numbering was for. `qb-seat` makes a single `GET /whoami`
carrying both headers before it execs, which settles the row; every process that follows
resolves to it. It reads back what the board actually said and warns if that is not
`seat-N`, which happens when the key was bound to a designated name on some earlier run —
allocation hands a returning key the name it already had, and a request cannot displace one
that exists.

*Addressing was never at risk either way*, and that is worth knowing before someone
re-derives the worry: the board resolves `machine/key` as a permanent alias, so an ask sent
to `zeus/meadow-russet` is returned by a poll that asks for `to=zeus/seat-3`. This is about
the name a human types and reads on a status bar.

**Two panes on one seat number is refused, and the board cannot be the one to refuse it.**
They export the same instance, so they send the same key, so the board hands them *one*
identity — from its side they are indistinguishable by construction. They then share the
ask-poll cursor, and whichever polls first swallows the other's mail: the exact bug the
per-seat instance exists to prevent, one level down, and invisible because both seats
otherwise work. So the check is local, where the panes actually are. `qb-seat` records its
pid in `$XDG_RUNTIME_DIR/qb-seat-<n>.pid` — or, on a machine with no `XDG_RUNTIME_DIR`
(macOS, most containers, ssh onto a box with no systemd user session), in
`${TMPDIR:-/tmp}/qb-seat-<uid>-<n>.pid`, where the uid is in the name because `/tmp` is
shared and a marker there is not. It exits **3** if a live process already holds that
number. A marker left by a seat that died is taken over rather than honoured, and
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
| `QB_SEAT_FORCE` | unset | Start anyway when this seat number looks already taken. Truthy values only (`1`, `yes`, `true`, `on`) — `QB_SEAT_FORCE=0` leaves the guard on |
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
anything, the fleet's own screen is `qbseats` rather than `seats-nix-fleet`, and tmux
silently renames what it will not take verbatim. So the list is read back from tmux and can
only print names that really exist, which also makes it the way to reattach to a screen tmux
renamed under you.

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

**Clicking starts work, not just navigation.** Each PR row carries a `⚖` and each issue row
a `⚒`; clicking one opens a confirmation showing the exact command, and confirming runs
`/panel-review-pr <n>` or `/fix-issue <n>` in a detached tmux window of its own — the same
way `qb-seat` starts an agent, so what it starts is a real session you can attach to, read
and interrupt. Clicking anywhere else on the row still opens the thing on GitHub. The keys
are `o` open, `p` panel-review, `f` fix the selected issue or plan item, `r` refresh, `?`
the list, `q` quit.

**The plans panel is the one that says what the work is FOR.** FLEET says who is here and
CLAIMED says what they hold; neither answers why. `PLANS` is the board's plan — every repo's
ordered list, plus the fleet-wide one — with the items somebody is running at the top, then
the ones that are free, then the blocked ones, which are the band a reader can do nothing
about. Inside each band the board's own order is kept, because the order is the point of a
plan, and the repos this dashboard watches come before the ones it only overhears. A row
shows `▶` running with its holder, `○` free with how long it has sat, or `⊘` blocked with
what it waits on. Clicking one puts its phase, its claim note, its blockers and its own note
on the detail line: that reasoning lives on the board and nowhere else — a plan item never
restates its issue — and it does not fit in a title cell.

A plan item that points at an issue carries a `⚒` like an issue row, so the shortest path
from "what is next" to somebody doing it is one click. An item pointing at nothing, or one
somebody already holds, says so rather than swallowing the click. The `⚒` refuses an issue
belonging to a repo this dashboard only watches: `/fix-issue` takes a bare number and reads
the repository off the checkout it runs in, so starting one from the wrong pane would land
that number on whatever issue wears it there. A plan claim is keyed `plan:<uuid>`, which is
right for a lock and unreadable on a pane, so CLAIMED resolves it against the plan and shows
the item's title instead.

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
the single click and means it. `QB_DASH_REPO` says where launched work runs, defaulting to
the dashboard's own cwd.

Adding another verb is three things: an entry in `BINDINGS`, an `action_*` method, and — if
it wants an icon — a column, since a click carries the column it landed in and that is how
one row offers more than one verb.

Not wired into `qb-seats` yet. `qb-dash` is a **launcher**, not the dashboard: the dashboard
is Python needing `rich`, `textual` and `mcp_server`, none of which a plain `python3` has, so
a shebang would be rewritten by `patchShebangs` to an interpreter that dies on the first
import. It hunts for one that can, the way `qb-board` does — `QB_DASH_PYTHON` names one
outright, `QUARTERBACK_REPO` points at a checkout whose `mcp/.venv` is built. Until
`mcp_server` is packaged, that venv is the only thing that satisfies it. Bring one
up beside a running screen with `harness/dev/seats-extras.sh <session> <width>`, which also
relabels the board pane — that script hardcodes local checkout paths behind
`QB_MCP_CHECKOUT` and is developer scaffolding, not something to ship.

Adding the dash also needs a wider `pane-border-format` than `qb-seats` sets: its own
prints `board` for any pane with no seat number, so a second unlabelled pane claims that
name. The dev script widens it to fall through to a `@qb_label` option; that belongs in
`qb-seats` proper once this settles.

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
  programs.quarterback-harness.enable = true;
}
```

That links `loops/` to `~/.claude/loops`, every slash command to `~/.claude/commands/`, and
puts the worktree scripts on `PATH`. Narrow `programs.quarterback-harness.commands` if your
host already defines a command of the same name — home-manager will collide rather than pick
a winner silently, which is the behaviour you want. Set `installScripts = false` to take the
loops and commands without the worktree tooling.

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
`worktree-holder` (without it the check reports "could not tell" and the scripts carry
on). `docker` and a database client only if your repo uses them.

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
everything else works unchanged. Point `qb` at your board to light up `GET /review/stats`
and the board's `/panel` page.

`worktree-holder` and `qb-board` read the same per-host site config —
`QUARTERBACK_BASE_URL` and `QUARTERBACK_TOKEN_CMD` from
`${XDG_CONFIG_HOME:-~/.config}/quarterback/config`, either overridable from the
environment — but read it directly rather than through `qb`, so the occupancy check and
the board client work whether or not that CLI is installed. There is deliberately **no
default board URL**: unset means this machine has not been told which board it belongs to,
and guessing would point the query at somebody else's.

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
