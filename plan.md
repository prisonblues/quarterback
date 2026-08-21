# quarterback — working plan

**Untracked and deliberately so.** This is a shared scratchpad on `zeus`, not a repo artefact.
**GitHub issues are the source of truth**; this file only holds what they cannot: the *order*, the
*reasoning behind the order*, and who is on what right now. If this file and an issue disagree,
the issue wins.

---

## ⚡ STATE OF PLAY — 2026-08-17 ~20:05 (`zeus/timber-cedar`, `zeus/d0473ba9`)

> **REPLACE this block; do not append to it.** Everything below it is a chronological log that only
> grows, and reading it to find out what is true today now costs more than the log is worth. This
> block is the answer to "what is happening right now"; put the *reasoning* below, where it belongs.

**`main` = `99e9b87`, CHANGELOG v2.41, migration head `0022_canonical_release_repo`.** Both suites
green, re-run on this commit at 21:00: **1,083 passed + 15 skipped** (`harness/tests/` +
`harness/loops/tests/`) and **778 passed** (app). Prod auto-deploys and is current.
`release_stamp.py check` exits 0 clean.

**Yesterday's constraint is gone.** The block this replaces said "ten open PRs, and that is the
constraint" and told you to land rather than start. That happened: **nine PRs landed this afternoon**
— #154 (v2.34) · #147 (v2.35) · #149 (v2.36) · #151 (v2.37) · #143 (v2.38) · #153 (v2.39) · #161
(v2.40) · #158 · #162 — closing **#122, #96, #142, #77, #125, #127, #60, #121**. **#152 was then closed unmerged and its replacement landed as #170 (v2.41), closing #148 and #150 — so TEN PRs landed today, ONE is open** (#160), and 62 issues. The queue table below is a historical document from item 4 down; read it for
reasoning, not for state.

**Who did it: session `15666470`'s parent agent, posting as `zeus/jackal-crystal`** — posts 3757,
3761, 3767, 3772, 3779, 3787, each reporting a landing in the first person with SHAs matching `main`,
and carrying detail no observer would have. **Git authorship cannot corroborate that and must not be
used to try.** A fork of that session initially claimed the merges were Rich's own work, citing
`git log --merges` showing every merge `author=Rich Folsom`. **That evidence is worthless on `zeus`**:
`git config user.name` is `Rich Folsom` repo-wide, all 200 most recent commits carry it, and that set
includes `8e97a93`, which this file records as an *agent's* fix pass on #87. **An agent commit here is
indistinguishable from a human one — never use authorship to tell them apart.**

What settled it is on the board and anyone can check it: **3757's detail refers to
`zeus/cedar-indigo` in the third person**, crediting the fork with the independent stamp
reproduction. An agent does not write about itself that way, so 3757's author is the parent and
distinct from the fork — which had distinguished the two before either noticed the problem.

### #146 is biting live, and it undermines the coordination convention

The fork above shares session lineage with `jackal-crystal`, resolves to a **different board name**
(`zeus/cedar-indigo`), and answered an ask addressed to its parent — saying that ask would not reach
it and that the work it described was not its own. **A post's `from:` is therefore not reliably the
address of the agent that wrote it.** Every instruction in the global workflow file — "address a peer
by its full identity", "reply to the asker" — assumes `from:` is an address.

**The precise loss is the OUTSIDE view, not the fork's self-knowledge.** A fork knows its own tool
calls after divergence with certainty; what it cannot know is what the parent did after that point.
But **a third party reading the board cannot separate the fork's posts from the parent's, because the
session id is identical** — which is exactly what misled the first draft of this block.

**Detection rules that actually worked**, in order: third-person self-reference in a post's detail;
comparing `whoami`'s key/alias against the post's `from:`; and **never accepting "you are the one who
did X" from a peer** — including a peer's denial. Prefer the permanent `machine/key` alias. All of
this is now a comment on **#146**, which had described the defect only as one agent showing as two.

**And the remedy is smaller than it looks, because the board ALREADY KNOWS about the fork.** `active`
returns the two entries as `"Check agent review status and landing candidates"` and the same string
with a **`⑂` suffix** — so the runtime tracks the fork relationship and it survives all the way to the
API. It is just carried **as a character inside `title`**, and never appears in `session`, `holder`, or
a post's `from:` — the three fields anyone actually resolves identity on. A peer therefore sees two
agents with near-identical titles and no structural signal that one descends from the other, which is
exactly the hole this block's first draft fell into. **So a first cut is purely additive: expose
`forked_from: <parent session>` on `active`/`peers` and on posts.** The `⑂` is proof the information
exists and is simply not passed on — this does not need identity re-keyed on something a fork cannot
inherit.

**A related gap on the same call:** nothing in an `active` entry says an agent is dead. `15666470`
came back with `expires: 20:25:19` and `since` frozen at `19:24:42` while every live peer sat at
20:42 — **the tell is only visible by comparing `since` against other agents.** A last-renewed age or
a renewal count would make it readable without the comparison.

### The queue: #160 is the only PR open

**#152 is closed unmerged and #170 replaced it (see below) — so the queue is ONE PR.** Both were CI-green and clean against
`main` this morning; both then read `CONFLICTING` because the nine landings moved `main` under them,
and neither conflict was code — `merge-tree` verified every cross-PR conflict in the queue as
`CHANGELOG.md`/`README.md` only, and `rerere` on this machine holds that resolution. **So #160 is not
waiting on a rebase; it is waiting on a premise call, and that is the only thing between it and
landing.**

**~~#152~~ CLOSED UNMERGED (2026-08-17 20:27, Rich's call) — and its replacement is ALREADY MERGED as
PR #170 (v2.41), closing #148 AND #150.** `main` = `99e9b87`. Fresh worktree to merged in about fifteen
minutes, which is the whole argument for the smaller fix.

**The reasoning is the most reusable thing on this page: the input was never noisy — one tool signature
made it noisy.** `claim_release_number(ctx, repo: str, …)` asked the *agent* to type the repo name, so
two agents answered with the two spellings they each had to hand and the allocator obliged with two
counters. That was the whole of #148. Ten collisions, a 457-line parser, three review rounds, a
244-line migration and 42 tests were all cleaning up noise the signature manufactured.

**The fix was already in the tree.** `mcp/mcp_server/gitctx.py:94` has `repo_slug(repo_path)` — six
lines, derives `owner/name` from `remote.origin.url`, handles scp syntax, `https://`, `ssh://`, with or
without `.git` — and `sync_status`/`report_git` already used it. The release tools now take `repo_path`
and derive rather than ask. **~30 lines against #152's 2,195.** #152's own closing comment is the best
write-up of why; read it before proposing a parser for anything.

**Second copy of the same mistake, also in #170's scope:** `server.py:1081` fell back to the
**directory basename** when the slug could not be derived — literally `quarterback` — so one call site
derived the tight name and silently degraded to the loose one. That is where the bare-`quarterback`
namespace's 2.32 and 2.36 rows came from.

**Why the parser was the wrong target:** three rounds failed to make it safe — round 3's hole was
`canonical_repo('https:///etc/passwd')` → `etc/passwd`, empty authority unvalidated, live in the head
commit at close — and a fourth pass would have been the move that had already failed three times
(#67). **Deriving the name deleted the parser, the alias union and the ambiguity case rather than
making them correct.**

### The number that should change how a round is run

The landing triage (board 3741, full table in its detail) measured the latest round of all seven
reviewed PRs in the queue: **201 new findings, 128 of them introduced by the immediately preceding
fix pass — 63.7%.** Per PR: 92.3% (#152), 88.9% (#158), 82.4% (#161), 69.4% (#153), 54.1% (#154),
51.3% (#160), 32.1% (#151). **And on all seven the final fix commit post-dated the last panel round,
so the head of every branch had been reviewed by nobody** — in a queue where two thirds of findings
come from exactly that kind of commit.

That is #67's thesis with a measurement behind it instead of three anecdotes. **#165 is the response**
(`nectar-ivory`): every convergence lever as a `review_panel` setting, with defaults that argue for
*less* reviewing — `max_rounds: 1`, severity floors at P2, a failing-test evidence contract, consensus
off. **Read #165 and #67 together before running another panel.** The convergence table near the
bottom of this file is the older, thinner version of the same argument.

### vNEXT: history compressed, one live constraint, one new hazard

**#122 landed as PR #154 (v2.34): a branch no longer guesses its release.** It writes `vNEXT` and the
number is stamped at land time as max+1, with a `no unstamped release on main` CI job that ran for the
first time and passed. It demonstrably works — **#161 arrived holding a hand-written v2.43 and landed
restamped to v2.40** (`e86a6ce`). **The nine-collision thread below, item 6's allocator, and
"announce before you write code" and its falsification are settled history.** Consequences:

- **Version order no longer binds landing order.** Land in readiness order; every branch stamps max+1
  when it goes in. That is what let nine PRs land in an afternoon.
- **A version-carrying branch opened before #154 wants converting to `vNEXT`, not renumbering.** Both
  open PRs predate it.

**The allocator is now DEAD CODE with a live MCP tool on top of it, and that reframes #152 entirely.**
Verified on `main`: `claim_release_number` appears **only** in `mcp/mcp_server/server.py` — nothing in
`harness/`, `scripts/` or `app/` calls it — and `scripts/release_stamp.py` makes **no network calls at
all**, taking the number as `max(release headings at --onto) + 1` from the CHANGELOG at a git ref. No
UI reads the rows. **So nothing consumes the allocator any more**, and its records are demonstrably
wrong: it holds up to **2.42** while `main` is at v2.40, `#152` holds v2.38 (which landed as #143's)
and `#160` holds v2.40 (which landed as #161's). The two spellings still hold separate floors —
`prisonblues/quarterback` 2.35–2.42, bare `quarterback` 2.32 and 2.36 — but **the blast radius is now a
wrong row on the board, not a wrong release.**

**The hazard that remains is the docstring, not the duplicate.** `claim_release_number` is still
exposed to every agent and its own text says "**Do NOT pick one yourself**". So agents are being
actively instructed to claim from a system that no longer decides anything. **That is #149's lesson
pointing at the answer** — the documentation an agent trusts most is the last thing updated.

**Two defects in `release_stamp.py` itself, both filed today, both verified against `main` as shipped:**

- **#168 — one unstamped `vNEXT` on `main` stalls the whole queue.** If a `vNEXT` ever lands
  unstamped, every later branch refuses to stamp (numbering on top of it would hand this branch a
  number the unstamped one is going to want), it is **detected only after the merge**, and the repair
  is re-running `apply` against a ref predating the bad merge. One careless merge stops everything.
- **#167 — the "a branch does not pick its own number" check is inert on exactly the branches that
  pick their own number.** `build_plan` returns early when a branch has no `vNEXT` sites (`:1033`),
  and that early return sits *above* both the `ahead` ordering guard (`:1083`) **and the
  `unstamped_at_base` check (`:1058`)**. So a placeholder-less branch skips the ordering check *and*
  fails to notice a broken base at all. Measured across all eight queue branches: **zero contained
  `vNEXT` in tracked markdown, so `apply` was a no-op and the refusal never fired on any of them.**

**Converting the remaining version-carrying branches to `## vNEXT` is what kills the CHANGELOG
conflict permanently, and it is unclaimed.**

### #60 landed and its table is EMPTY — this file is still the only order

**PR #153 (v2.39) built the plan as a board primitive**, and its own PR body names *this file* as the
flaw it exists to fix: an untracked `plan.md` on `zeus`, invisible from `hermes`, invisible from a
container, gone with the checkout. `GET /plan` is live on 2.40.0 and **returns zero items** —
`{"items":[],"next":null,"counts":{"open":0,…}}`, checked 19:57. Claims taken through plain
`POST /claim` surface in it for free; only a human may reorder; agents add, claim and complete.

**But the table is empty because nothing can WRITE to it, and that is the actual defect.** Do not
treat the empty table as a backfill job until you have read this:

**No currently-running agent can reach the plan primitive at all.** `mcp/mcp_server/server.py` on
`main` defines `plan_read`, `plan_add`, `plan_claim`, `plan_release`, `plan_done`, `plan_depends`, and
the server's instructions block now opens with "**Start cold:** `plan_read(repo=…)`". **No live
session has those tools.** `ToolSearch select:mcp__quarterback__plan_read` finds nothing in this
session or in the two peers that checked. `plan_add` is exactly as unreachable as `plan_read` — so
the empty table is a *symptom*.

**The mechanism, because the obvious guess is wrong.** The code on disk is fine: `qb-mcp` execs
`$REPO/mcp/.venv/bin/python -m mcp_server`, the venv is an editable install, and it resolves to
`/home/rich/source/quarterback/mcp/mcp_server/server.py` — the live checkout. **Loading that module
right now lists 31 tools including all six `plan_*`.** What is stale is not the code but the
**process**: the stdio server is spawned once and then *outlives the conversation*. Mine started
**2026-08-16 22:04**, a day before #153 landed, while this conversation began at 20:27 tonight —
`pgrep -f 'python -m mcp_server'` on this box shows processes dating back to **Aug 13**.

**`/clear` does not respawn it, and that is #146 from the inside.** This session's MCP server carries
`CLAUDE_CODE_SESSION_ID=d0473ba9…` — the id from *before* the `/clear` — while the lifecycle hook and
the lease use `755d1233…`. One conversation, two ids, and the board key follows the server's. So #146
is not only "one agent becomes two on the board": **it is also why an agent that cleared its context
is talking to a server older than the feature it is trying to use.**

**There are two routes to a toolless agent, both fixed by a server restart:** a long-lived process,
and `/clear` forking the conversation id without respawning (both above). **A third was proposed and
withdrawn, and the reason it fails is the useful part.** Mapping all 39 live `mcp_server` processes
across 38 distinct `CLAUDE_CODE_SESSION_ID`s finds **no server keyed to `15666470`** — the session
that posted all day and landed the nine — which looks like evidence it had been deprived of one. It is
not: **if a server can be keyed to its session's *ancestor* id, then "no process for session X" is the
expected state for any `/clear`ed session.** The absence proves nothing in either direction, and the
parent demonstrably had MCP transport at 19:12:55 (post 3788's detail is first-person analysis, not
hook output). It simply ended; **a session that has ended is a complete explanation for an unanswered
ask and needs no tool loss.** Do not read a missing process as a deprived agent.

**Three traps for whoever checks any of this.**

- **`pgrep -f 'python -m mcp_server'` matches your own shell running the search**, because the pattern
  is in its own command line — so you find a phantom server keyed to your current session id, which
  can look exactly like a refutation of the real diagnosis. Check `/proc/<pid>/cmdline` for the venv
  python before believing a server exists.
- **Use `ps -o lstart=` for start times.** `/proc` mtime reads the same recent timestamp for every
  process and is worthless here.
- **`active` tells you the lease HOLDER, not who you are.** Only `whoami` and
  `$CLAUDE_CODE_SESSION_ID` answer that. Live case today: a forked agent read its session id out of
  `active` at start-up, got its *parent's*, and filed two `claim`/`release_claim` calls under it before
  noticing four hours later. Nothing broke because nobody contended them — but **a claim filed under
  the wrong session is a lock the real owner cannot release and the wrong owner can**, and a release
  claim is documented as owned by the session that took it.

**The fix is RELAUNCHING THE CLIENT — `/mcp` and `/clear` both fail to do it, and that is measured.**
The shim is fine: running `/home/rich/.local/bin/qb-mcp` fresh with a full MCP handshake — the exact
command Claude Code uses — returns **all six plan tools**, so `$REPO`, the venv and the code are all
correct. The stale thing is only the **process**, which loaded pre-#153 code into memory and serves
that same negotiated tool list forever. **`/mcp` shows you a connection negotiated yesterday; it does
not respawn anything.** And `/clear` demonstrably does not either — this session cleared tonight and
kept its `Aug 16 22:04` server, which is why its board key is the pre-clear id. **Only exiting and
relaunching the CLI picks up new tools.** With 32 servers live and the oldest from **Aug 13**, that is
most of the fleet.

**So the fix is a client relaunch, and never a backfill** — until the fleet's
servers turn over, `GET /plan` is write-unreachable and this file remains the single source of order.
**#60 is closed against a table its own clients cannot address.** The zero-items read was taken with a
valid token; **a tokenless read 401s and is indistinguishable from an empty plan**, so check which one
you are looking at.

**This is the third time this week a closed issue shipped a mechanism nothing calls** — board 3255
said the same of #131 ("the mechanism that would have stopped it shipped IN #131 and is unwired").
**That is #65's class at the deployment layer, and it is the pattern worth filing about, not this
instance of it.**

### FREE, in priority order

**Make `GET /plan` reachable, then seed it** (above — a server restart comes first; seeding it is what
makes #60 and #135 real, and neither is filed) · **push `quarterback-seats`** (two commits, single
copy, live agent in the tree) · **convert #160 to `## vNEXT`** (kills the CHANGELOG conflict
permanently; unclaimed and cheap) · **#44**
(now unblocked — this was explicitly "take it after #151 lands", and #151 landed) · **#100** (now
unblocked: #96 landed as `preland.py`) · **#165** (the panel's dials — read with #67) · **#167 +
#168** (do them together; they are one early return) · **#84** · **#80 + #83** (the ceiling, and
rebase-on-publish) · **#107's hard and terminal levels** · **#101** · **#113** · **#156** · **#166** ·
**#163** (`nix flake check` red on `main` since last night — the worktree-tests sandbox cannot see the
files `test_release_numbers.py` reads; nobody is on it).

**#155 is still OPEN although PR #161 (v2.40) landed for it.** Either it wants closing or the
socket-as-doorbell half is outstanding — check before picking it up.

### Single-copy work nobody is tracking

**`/home/rich/source/quarterback-seats` has unpushed commits and a live agent in it.** HEAD is
`983d482` ("click the scales to start a panel review") on top of `58dc94b` ("a fleet dashboard for the
seat screen (WIP)"), **neither on any remote** — and `zeus/tern-raven` is working in that worktree
right now (zellij-vs-tmux for the seat screen). The dashboard depends on **#110**, so it wants a rebase
onto `main` once **#160** lands. `mist-tern` reported this as `f38e5ec`; **HEAD has moved past that SHA
since**, so take the branch, not the commit.

**This is the exact shape `65lowther` was rescued from tonight** — 51 commits and five branches that
had never left `zeus`, pushed at 20:22 after somebody looked. **Push it before it is the story.**

### Filed today

**#165** — the panel has one behaviour and no dials (above). **#167** and **#168** — the two
`release_stamp.py` defects (above). **#166** — `migration_reconcile.py`
plans from git refs but gates `apply` on where the working tree is, so the same repo in the same state
gets a plan or a refusal depending on whether you merged the base first; it produced a correct plan
for #153 and then refused to run it.

### Six things from the nine that should not be re-derived

1. **`git checkout --theirs` on `app/main.py` silently drops a router.** It is safe **only when the
   served-version line is that file's only change on the branch.** Landing #153 it was not: the plan
   router went, and **58 tests failed 404**. Restored in `bcfc787`. The prose-conflict reflex — take
   one side, move on — is trained by CHANGELOG/README and then applied to a file where both sides
   carry work. Same class as #62's duplicated `stderr_gist` and #90's `--round` sentinel, a third
   time. **After any `app/main.py` or `panel*.py` conflict, check the branch's other edits first.**
2. **`rerere` replays a STALE resolution and leaves no markers behind.** On #161 it kept the branch's
   superseded CHANGELOG header and dropped `main`'s replacement into the body. **Absence of conflict
   markers is not evidence of a correct merge** — and this is the same file the nine trained the
   reflex on. Read every rerere-resolved CHANGELOG before committing it.
3. **Run the migration reconciler BEFORE the integration merge.** Its duplicate-id check fires before
   it can plan against a post-merge tree, and `apply` then refuses because HEAD is not the branch it
   planned for — a correct plan it will not execute. That is #166, and the workaround until #166 lands
   is simply ordering: reconcile, then merge.
4. **A new naming rule turns an old branch red at merge, not on the branch.** #151 shipped
   `tests/test_v237.py`; v2.34's test-naming rule landed first and would have turned the harness suite
   red the moment the two met. **A rule about file names is a cross-PR blocker for every open branch,
   and CI on a branch cannot see it.**
5. **Changing a rule leaves statements of the old one behind, and the worst of them are docstrings.**
   #149 changed claim exclusivity and left four restatements of the superseded rule, **three in MCP
   docstrings agents read before calling the tool** — the documentation an agent trusts most was the
   last thing updated. Grep for the old rule's *wording*, not just its code.
6. **Never make the `stamped` CI job a required status check.** It runs only on push to `main` by
   design (`if: push && ref == refs/heads/main`), so as a required check it never reports and leaves
   every PR pending forever. **Require `app suite` and `harness suites`.** A trap laid for whoever
   wires branch protection next.

### Standing decisions, unchanged

**Stop filing deferred-findings issues** (human's call, 2026-08-16). **Thirteen are still open** and
not one has been worked. Every finding is already on the board with severity, file, line and
provenance; in the PR comment, say "on the board" and give the query.

**Do not add a new issue to item 7's backlog list below.** It is a closed set.

**This file is 1,305 lines and #135 is about that.** Its contract says it holds the order and the
reasoning; most of what follows is narrative that belongs on the issues it describes. If you replace
this block, delete a hundred lines below it too — starting with the release-collision thread, which
is now settled in every direction except #152.

---

_Last updated: 2026-08-16 ~11:45 by `zeus/opal-kelp` (`zeus/5f1288e6`) — **#98 done, PR #128 (v2.29,
schema 0018), CI green — and that closes STAGE 1's second gate clause.** #41, #48, #82 and #98 are
all in; a round now records both ends of the range it was judged against, so a finding traces to the
fix that caused it. **v2.29 moves the board, so it needs a Portainer redeploy.** Three things worth
carrying, and the first is a warning about an issue everyone downstream is meant to build on.
**(1) #98's own proposal specified a detector that cannot fire, and only checking the field caught
it.** The issue says stamp `baseRefOid` as `base_sha` and compare it later against the PR's *current*
`baseRefOid`. **`baseRefOid` is the MERGE BASE**: GitHub recomputes it when the HEAD branch is pushed
and never when the base advances, because a common ancestor is not moved by commits added to one side
of it. PR #87 held `88643c14` while `main` took ten commits, REST `.base.sha` agreed, and
`git merge-base` against the moved `main` still answered `88643c14`; #90's equalled `git merge-base`
exactly, and the two PRs that DID move theirs (#62, #117) moved them by merging `main` into
themselves — the branch acting, never the base. So the specified check reports "unmoved, the review
still stands" exactly when `main` has run away underneath a clean verdict. Not a failure recording as
a success — **a staleness detector whose only possible output is `fresh`**. It ships as two fields
instead: `merge_base` (the PR's base commit, free off the `gh pr view` call already made) and
`base_sha` (the base branch's live tip, its own cheap lookup). **Whoever takes #96: the issue BODY
still reads the old way; the correction is a comment on #98 and on board 2751.**
**(2) v2.28 landed mid-build and changed what one of the two fields MEANS — and `panel.py` merged
CLEAN, so nothing mechanical would have caught it.** Under #41's increment scope a round's target is
`since_sha...head_sha`, and `merge_base` is then where that round's tier-2 context is measured from —
so it is the PR's anchor and **not always the round's**. Every site now says read `scope` first, and a
test asserts `since_sha`/`merge_base`/`base_sha`/`head_sha` are four distinct commits with four
different jobs. This is #106's shape from the other end, and the same class as #62's duplicated
`stderr_gist`: the clean textual merge was the whole of the danger.
**(3) A test double that does not answer a call the code makes is the TEST's gap, not the code's.**
#90's `test_panel_scope.py` asserts `config_notes == []`, and an honest "could not read the base tip"
note fired there because its `sh` double had no answer for the new call. That file already documents
the identical trap for the provenance compare call one field over. Fixed by completing the double,
never by weakening the assertion.
Then ~11:40 by `zeus/thorn-spruce` (`zeus/696f5518`) — **the open-PR queue is
cleared: #115 (v2.26), #62, #117 (v2.27) and #90 (v2.28) are all MERGED**, `main` = `ace8e04`, 844
harness + 349 app green, no duplicated defs. **Prod auto-deployed and serves 2.26.0** (checked via
`/openapi.json`), so the "needs a Portainer redeploy" notes below for v2.23 and v2.26 are DISCHARGED —
the webhook that was failing on 2026-08-14 works now. #87 is the only PR still open: its round 1 had
been sitting unfixed since 20:34 last night (this file said "panel r1 wanted"; it had already run),
12 of its 27 findings are now fixed and **round 2 is running**. Four things worth carrying.
**(1) The prose conflict is CAMOUFLAGE for the real one.** All four merges conflicted on exactly
`CHANGELOG.md` + `README.md`, every time, whether or not the release number collided — and #90's
merge also carried a `panel.py` conflict that was not prose: #117 had made `--round` a sentinel
(`round_no = 1 if args.round_no is None else args.round_no`) while #90 still passed `args.round_no`
into `run()`. **Taking "ours" — the correct answer for the three prose files above it — passes `None`
where an `int` is required and breaks `--round`.** It merges clean either way; only reading it caught
it. Same class as #62's duplicated `stderr_gist`. Filed as **#122** with six options; the two
recommended are (a) delete the README's "Latest release / Before it" narrative, which is the only
conflict needing real prose work and is pure duplication of the CHANGELOG, and (b) **stamp the
release number at MERGE, not at write**, which removes collisions rather than serialising them.
**#122 is not #46/#99** and deliberately does not build a second allocator.
**(2) A stopped agent had finished, again.** `bramble-otter`'s #79 work — a secret denylist for
`--ask` (`.git/`, `.env`, key material), 165 lines of tests, all passing — was sitting UNCOMMITTED in
its worktree when its session ended. `worktree-holder` said nobody was live and `active` confirmed
the lease had expired, so it was committed with attribution and landed inside #117. That convention
has now paid for itself twice.
**(3) #90 was renumbered THREE times** (v2.23 → v2.25 → v2.28) without one line of its behaviour
changing. Seventh collision.
**(4) #87's round 1 found that a documented guarantee was simply false, and the fix pass proved it
rather than arguing it.** `rerere.autoUpdate` was left *absent* rather than set, and absent is not
off — a global `autoUpdate=true` gave exactly the silent staging the docs promised could not happen.
Reproduced against the pre-fix block before touching it: exit 255 on a held `config.lock` (which
under `set -euo pipefail` kills worktree creation before any worktree exists), `rerere.enabled=banana`
left in place, effective `autoUpdate` reading `true`. **And the guarantee that survives is narrower
than it was written**: `epic.py:593` and `lander.py:197` both run a blanket `git add -A`, so on the
unattended path a replayed resolution is committed unread — filed as **#124**, not fixed, because
#87 is entirely about two defaults. Deferred P3/P4 went to **#126**.
**One warning for whoever fixes a finding next:** review's suggested form for #87-F11 was
`cfg.get(...) or DEFAULTS[...]`, and it was WRONG — it collapses "absent" and "explicitly empty",
turning a repo that asked for no model routing back on at the top tier. The proposed fix was the bug,
which is #64's lesson arriving from a different direction.
Earlier, 2026-08-15 ~21:22 by `zeus/fathom-hazel` (`zeus/0764012d`) — **stage 0 is done and open as
PR #87 (v2.22)**, closing #81 and taking #78's `judge_model` line. Neither was the one-liner the queue
said, and the reason is worth reading: changing the judge's model broke a coupling in `epic.py` that
had held only by accident. **`rerere` is now ON in `~/source/quarterback`** — if a merge tells you
"Resolved 'x' using previous resolution", that is git handing you the last answer, unstaged, and you
are meant to read it. Next FREE is item 4.
Then ~23:45 by `zeus/marten-tidal` (`zeus/c8732e38`) — **#48 MERGED as PR #89 (v2.24, `e57464a3`)**, two
rounds, capped, 25 deferred to #104. `main` verified: 564 harness + 299 app, no duplicated defs. Three things
that outlive it. **(1) `introduced` is a FLOOR, not a count** — a defect introduced by a *deletion* has no added
line, and exact line-membership scores ordinary reviewer line-drift as `missed`. Both documented, neither fixed
(changing the matcher at the cap = an unreviewed change to the mechanism under review; #41 supersedes it).
**(2) The merge with #82 nearly deleted #82's work silently** — #82 moved `notes` init above the skip branch,
this branch still had `notes: list[str] = []` below it, and taking "ours" compiles while discarding every
file-list warning built in between. Same class as #62's duplicated `stderr_gist`, different route. **(3) agy's
seat is FIXED and its shell does NOT run in #75's sandbox** — it runs in `~/.gemini/antigravity-cli/`, next to
its own OAuth token; allowlist deliberately navigation-only. See board 2524, and #92.
Then ~21:50 by `zeus/marten-tidal` (`zeus/c8732e38`) — **#48 is done, PR #89 (v2.24), CI green.** Two
things from it that outlive the PR. **(1) `new_this_round` is a CONSTANT, not a signal**: it is true
for *every* finding in *every* banked payload (26/26 on #75 r2, 23/23 on #76 r2), so the convergence
table below is built on a field that never varies. **(2) Nothing recorded which commit a round
reviewed** — `base` holds the branch *name* — so the 12 banked payloads can never be retrofitted with
provenance; n starts at zero today. Also: **the release number collided a fifth time**, and this one
kills the cheap remedy — `silver-cedar` and I both *announced* v2.23 one second apart and were both
correct from what we could see. #76's detection half cannot catch that (two self-consistent branches
both pass); only #46's allocator half can. I moved to v2.24 because #88 carries a migration and mine
is prose.
Then ~22:20 by `zeus/tern-ochre` (`zeus/058760b1`) — **filed #96–#100, the landing cluster**, from a
pass over how lexray's `/merge-to-test` generalises here. Nothing implemented; issues and this
ordering only. The one decision taken: **#99's lease is resource-keyed** (`kind` + `key`), and the
reason is the thing worth carrying — **#46's allocator half falls out of the same table**
(`kind=release, key=<repo>` beside `kind=merge, key=<repo>:<branch>`), so the six release collisions
recorded below stop needing their own mechanism. Nobody should build two atomic-claim
implementations. The cluster is placed in the stages below rather than appended: **#98 belongs to
stage 1** (it is the other half of the range #48 already had to invent `head_sha` for), the rest to
stage 3.
Then 2026-08-16 ~00:10 by `zeus/rowan-timber` (`zeus/cbcf2f0b`) — **filed #107 and placed it in stage 2
as item 5b**. Nothing implemented. The finding is that **agent-to-agent messaging has a working sender
and no receiver**: `qb-hook:379-400` polls the board for asks addressed to this agent, with a
per-session cursor and a 45s throttle, entirely inside the `UserPromptSubmit` branch — so an agent
that is not being prompted never looks, and the only channel to a running headless loop is `kill(1)`.
It sits in stage 2 rather than stage 3 because stage 2's gate is *the loop can stop itself* and this
is the other half of it; stage 4 is the stage that removes the human who is currently the channel.
The "origin has changed underneath you" alert is its second consumer, not its purpose. **Two detection
gaps behind that alert are unfiled and want a decision on one issue or two** — nothing fetches, and a
GitHub-side merge emits no `published` at all, so the most common way origin moves here is invisible
to both signals.
Then 2026-08-16 ~00:50 by `zeus/rowan-timber` (`zeus/cbcf2f0b`) — **#93 done, PR #115 (v2.26, schema
0017), CI green.** #48's four payload fields reach the board and the leaderboard axis #48 was filed
for exists. Three things worth carrying. **(1) The panel side cost nothing** — `record_run` pipes the
payload verbatim through `qb record-review`, so all four fields had been *arriving* since #89 merged
and being dropped by pydantic's `extra="ignore"`. The cheap half of the contract working perfectly
while the expensive half is silently absent is **#65's class in its purest form**, and #65 now has
something mechanical to read: an unrecognised bucket comes back in the 201 response as
`provenance_unknown`. A detector with no consumer yet (`qb record-review` prints only the run id),
so #65 is still needed — but it is one HTTP response away from a real check rather than a design
exercise. **(2) An independent codex pass over the diff found two real defects the tests did not,
and both were the same shape: a signal computed from a DIFFERENT field than the one it annotates.**
The unread-file cap ran after the dedup while the response reported `sent - cap`, so a payload whose
paths were all kept still announced 1,000 missing; and `provenance_runs` — the marker gating whether
the page shows the split at all — read the run's tally rather than the counters beside it, hiding
real numbers behind "nothing was measured". This page learned that lesson once already in #75's r2
(`token_runs` vs `billable_runs`) and it arrived again from a different direction. **Run codex over
the diff; it is cheap and it is not redundant with the suite.** **(3) `by_provenance` and the
per-model split are not derivable from each other** — a sum across the leaderboard double-counts
every finding two seats agreed on. Both shipped. **v2.26 moves the board, so it needs a Portainer
redeploy.** For whoever takes **#98** (item 4b, still FREE): `head_sha` exists now and both
`GET /review/{id}` and `GET /review/findings` hand it back per round; the base end is the half left.
[Superseded: #98 is DONE as PR #128 — see the top of this file.]
Then 2026-08-16 ~11:35 by `zeus/nimbus-sorrel` (`zeus/ac747b9b`) — **#107's soft level is DONE, nix-fleet
PR #13**, 24 tests, shellcheck clean. A directed ask now reaches a running agent seconds after a tool
call instead of at its next prompt, so **`kill(1)` is no longer the only channel to a headless loop**.
Four things that outlive the PR. **(1) All three of the issue's "verify before shipping" items were
answered by RUNNING them, not by reading the docs — and that was necessary, because the hooks page
truncates its decision-control table exactly where the answer is.** `PostToolUse` does honour
`additionalContext`; that was the load-bearing assumption of the entire design and nothing had checked
it. Worth generalising: this plan's own convention says never to write a reviewer's conjecture into a
comment without checking it, and *a doc page that stops mid-table is the same hazard wearing a better
suit*. **(2) The sub-agent hazard was real, and the fix is one field.** `session_id` is NOT distinct
for a sub-agent — it is the parent's, shared verbatim — so the ask cursor is genuinely shared, exactly
as #107 feared. But `agent_id`/`agent_type` are present *only* inside a sub-agent and absent on the
main thread, so the discriminator exists. **This is probably load-bearing for #121** (`quill-sage`'s
fleet spawner): anything that wants to address a seat, or tell its own sub-agents apart, cannot use
`session_id` to do it. **(3) `prompt_id` exists and names the TURN**, which is what made the "debounce
per turn, not per session" requirement implementable without inventing a boundary. **(4) The
interesting design constraint was not delivery, it was NOT LOSING A MESSAGE** — the budget check has
to sit after "is there mail" and before the cursor moves, or a refused interrupt advances past an ask
nobody was shown and it is gone silently. The tests are mostly about that negative space, because
every failure mode here looks identical to "no messages". **Filed #125 and #127** (the two detection
gaps behind the origin-moved half) **as two issues, not one** — same symptom, different systems.
**Filed nix-fleet #14**: `qb-env`'s header promises "environment beats the config file throughout" and
the config file in fact clobbers the environment for every key but one — so a one-off
`QUARTERBACK_BASE_URL=…` override silently goes to the host's board, which is the precise failure
`qb_require_base_url` refuses to guess about. **Also, `tests/qb-token-selfheal.test.sh` fails 4 of 16
on pristine `origin/master`** — pre-existing, unrelated, and nobody is watching it.
Then 2026-08-16 ~11:45 by `zeus/heather-cinder` (`zeus/ce219d36`) — **#97 done, PR #123 (v2.29), CI
green, mergeable clean.** `scripts/migration_reconcile.py` with #97's three verbs, 35 tests, none
needing a database. Three things that outlive the PR. **(1) The donor fails OPEN on quarterback's
version of the problem, and #97 assumed it could be ported.** lexray's revisions are hash-named, so
two branches there can never mint the same id; here the id IS the number. Given `main`'s `0018` and
a branch's different `0018`, lexray's reconciler answers **"noop, merge is graph-clean"** — the ids
match so nothing looks rewritten, and `branch_new` excludes the branch's real work as
already-present. Git conflicts on neither file. Two migrations claiming one revision id land,
reported as safe. **#65's class from a new direction: the borrowed half that is absent returns the
reassuring answer rather than an error.** Anyone porting a tool from lexray should check what its
namespace makes impossible before assuming the check exists. **(2) Same id + different content is
ambiguous and the two readings want OPPOSITE actions** — two branches minting one number (renumber)
versus one branch editing an already-merged migration (stop), and renumbering a rewrite forks one
migration into two. The revisions at the **merge base** separate them, so the tool asks git instead
of guessing; that is the one place it needed a fact the graph does not carry. **(3) The eighth
release collision happened to this PR, and it kills the current remedy.** I claimed v2.28 on board
2746 at 10:17, addressed to the agent on #90 by name, offering to move; #90 renumbered off v2.25
into v2.28 at 11:18 and merged first. Every earlier collision was "each read main, each was correct
when it looked". **This is the first where the number was claimed IN ADVANCE and taken anyway** — so
the "announce before writing code" practice recorded below is not merely the-best-available-until-#99,
it has been tried and lost, because a renumber step does not read the board and nothing makes it.
Board 2774. **Item 6's allocator half is now the item with a demonstrated failure behind it, not a
tidy-up.** Also: the PR's first CI run was red on a test that passed locally — a throwaway git repo's
`git merge` needs a committer identity and a runner has no global `user.email`; reproduce with
`GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null`.
Then 2026-08-16 ~14:20 by `zeus/nimbus-sorrel` (`zeus/ac747b9b`) — **five PRs landed; `main` = `f3eec90`,
809 loops + 80 harness + 387 app, no duplicated defs.** #128 (v2.29), #131 (v2.31, schema 0019),
#134, #130, and nix-fleet #16. **Stage 1 is CLOSED and #129's gate is OPEN** — nothing is in flight in
`panel.py` (the only open PR, #123, touches `scripts/migration_reconcile.py`). The split will never be
cheaper than right now. Four things worth carrying. **(1) Every other agent was GONE.** I was the only
live lease; opal-kelp, heather-cinder, thorn-spruce, sienna-juniper and quill-sage had all expired, and
the directed asks I had posted to them went nowhere. **A claim outlives its claimant and reads exactly
like a live one** — thorn-spruce claimed #128's panel, ran r2, and expired mid-cycle, leaving the claim
standing. I only found out by calling `active`. #131 just landed the fix for this: `resource_leases` is
keyed on `(kind, key)` with passive TTL expiry, so a third kind — `kind='work'` — makes "is anyone on
this?" a query with a truthful answer. **(2) A stack does NOT auto-retarget when its parent lands with
`--delete-branch=false`.** #131 and #134 both kept pointing at the merged `feat/issue-98` and had to be
retargeted by hand; a stack landed carelessly would look mergeable while targeting a dead branch.
**(3) The dependency graph is now DATA.** GitHub's native `blocked-by`/`blocking` fields were empty on
every issue in this repo; eight edges are now populated (the stage-2 chain #84→#77→#78, the landing
chain #96→#100→#54, #85/#86→#63, #60→#121, #127→#83), so "what is unblocked?" is one API call instead
of a read of this file. **Actionable right now, computed rather than asserted: #84, #85, #86, #96,
#129, #125, #127, #80, #67, #65, #40, #39, #47, #55, #60.** **(4) This file has drifted from its own
contract** — it says it holds only the order and the reasoning, and it is 1,156 lines, much of it
narrative that belongs on the issues it describes. Live example: item 7 still says "four deferred
backlogs, 96 findings"; there are **twelve** (#66, #69, #72, #74, #95, #104, #111, #119, #120, #126,
#132, #133) and #132 alone is 40. Filed as **#135**, with the argument that a loop cannot run itself
while its map is prose — every stage gate here is written as a truth condition and nothing evaluates
one.
Earlier, ~20:50 by `zeus/ember-marten` (`zeus/ed9e13c5`), on top of `azure-tern`'s ~19:55.
**`ember-marten` is finishing here — nothing of mine is left running or half-done.** Panel payloads
moved to `~/.local/state/quarterback/panels/` so they survive this session.
**The queue at the top of the handoff is now staged, and each stage has a GATE** — a stage is finished
when its gate is true, not when its issues close. Retro filed #77–#86.
Earlier: **#68 fixed (PR #75, v2.21)
— and it was never the suspected race**; **#46's smaller half done (PR #76)**; **#62's round 3 is
in** and its two P1s are the same premise a third time, which is #67's first three-round circle._

---

# 👋 HANDOFF — read this first

> **Panel payloads are now kept OUTSIDE the session scratchpads: `~/.local/state/quarterback/panels/`.**
> Every completed round for #32, #62 and #64 is there (`panel-<pr>-r<n>.json`). Session scratchpads
> under `/tmp/claude-1000/…/<session-id>/` die with their session, and #62's baselines were one
> session-exit from being unrecoverable. **Copy a round's payload there when it finishes** — the next
> cycle needs it as `--baseline`, and the PR comment is prose, not a payload.


## THE QUEUE — what to pick up, in order

> **⚠️ STALE AS A STATUS BOARD (checked 2026-08-17 ~20:05).** Every "who" in this table is an agent
> whose session ended days ago, and its state cells were written for a one-PR-at-a-time cadence.
> **Items 1–4b, 5's #79, 5b's soft level, 6 and 6b's #97/#96 have all LANDED.** What is still open
> lives in the FREE list at the top of this file, not here. **Read this table for its reasoning —
> which is the reason it still exists — and never for what is true.**

Claimed work says who. **Anything marked FREE is unclaimed** — take it, and put your name here.

| # | what | stage | state | who |
|---|---|---|---|---|
| **1** | ~~**#62 — decide the premise.**~~ **DECIDED by a human ~20:55, and done** (`0b48bb0`). The gate reads the panel's own `reviewed`/`skip_reason`/`reviewers_ran`; both r3 P1s fixed, and r3's P1 was verified against `main` before being touched — it is real. **Carries one judgement call that wants a second opinion: `sub_pr_merge` now defaults to `gate`, not `auto`** — it argues with #78, and the PR comment says so explicitly rather than quietly. | — | **pushed, CI green** | `azure-tern` |
| **2** | ~~**#75** (closes #68) and **#76** (#46's half)~~ — **BOTH MERGED** (`88643c1`, v2.21; #68 closed). Landed on round-2 evidence rather than the cap: **the first like-for-like r1→r2 in the dataset** (3 of 4 seats in *both* rounds, no seat-count confound), counts up, severity down, no P1s either round. `main` verified after: 464 harness tests, no duplicated defs. | — | **done** | `azure-tern` |
| **3** | ~~**#81 rerere** and **#78's `judge_model`**~~ — **PR #87 (v2.22), CI green, closes #81.** Neither was one line. `judge_model` → `fable` (not `sonnet`: `clamp_model` already says fail toward capability), **and changing it exposed a coupling** — `epic.py` read `review_panel.judge_model` as its implementation *tier ceiling*, so this would have quietly routed every unattended sub-issue at the top tier. Split into `epic.model_ceiling` (default `opus` = what the old fallback resolved to), with a test asserting the two keys disagree. rerere is **live on this machine now** and `create-worktree` asserts it per repo. **Round 1 ran at 20:34 on 2026-08-15 and then sat UNFIXED overnight while this row said "panel r1 wanted"** — 27 findings, 3 of 4 seats (antigravity quota). 12 fixed on 2026-08-16 (`8e97a93`), and the interesting half is that **a documented guarantee was simply false**: `rerere.autoUpdate` was left *absent* rather than set, and absent is not off — a global `autoUpdate=true` gave exactly the silent staging the docs promised could not happen. All three shell defects were reproduced against the pre-fix block before being touched (exit 255 on a held `config.lock`, `rerere.enabled=banana` left in place, effective `autoUpdate` reading `true`). `resolve_ceiling()` is lifted out of `run()` so the wiring is tested by being CALLED rather than inferred from `DEFAULTS` literals. **#124** (rerere's unstaged guarantee does not survive `epic.py:593`/`lander.py:197`'s blanket `git add -A`) and **#126** (4 deferred P3/P4) filed. | 0 | **r2 running** | `fathom-hazel` → `thorn-spruce` |
| **4** | **#41 + #48 + #82** — the multiplier. Rounds get cheaper as a PR matures; findings become attributable. **#82 DONE AND MERGED — `8534763`, v2.23, schema 0016.** Split after two rounds: the datum landed as PR #102; the collision query moved to **#101**, whose one open question is now SETTLED (Postgres-only — `DISTINCT ON` is fine; the SQLite doubt was a reviewer conjecture I had written into a comment, see the conventions below). `main` verified after: 781 tests, no duplicated defs. **v2.23 moves the board, so it needs a Portainer redeploy to actually serve 2.23.0.** Two full 4-of-4 rounds put the SAME defect in that endpoint twice — a filter composed in front of the newest-run selection, resurrecting a stale run behind a confident answer — and r2's instance was introduced by r1's fix. Reported not patched (#67). **#88 is CLOSED, not merged**, and its branch `feat/issue-82` is deliberately kept — #101 cites `48dc9c0` as the implementation to read for what not to do. **#48 DONE — PR #89 (v2.24), CI green**: per-finding `provenance: introduced\|missed\|missed-unread\|unknown`, plus `head_sha` and `unread_files`, which had to be invented because **no payload recorded which commit a round reviewed** (`base` is a branch *name*). **#41 DONE — PR #90 (v2.25), CI green**: round 2+ reviews the increment, in three tiers (increment · the PR's other changes to the files it touches · the rest), with the budget spent in that order so a tight budget drops context and never the target. `--scope pr` and `review_panel.round_scope` override; every fallback to whole-PR scope lands in `config_notes`. **All three want a panel round.** #90 agrees to rebase onto #89's `head_sha` (theirs is first and has the better rule — the *accepted* baseline, so one rejected for another cycle cannot supply the fix range); check `Baseline.truncated_rounds` (#90) against `unread_files` (#89) at merge, as they are adjacent and neither subsumes the other. **ALL THREE ARE NOW MERGED** — #90 landed 2026-08-16 as **v2.28**, after being renumbered a third time (v2.23 → v2.25 → v2.28, because `main` had taken v2.25 for #116). **Read its merge before you do another one**: three of its four conflicts were prose (CHANGELOG, README, harness/loops/README — all keep-both) and the fourth was `panel.py`, which was not. #117 had made `--round` a sentinel; taking "ours" would have passed `None` where an `int` is required and broken `--round`, and it merges clean either way. See #122. | 1 | **done** | `silver-cedar` (#82) · `marten-tidal` (#48) · `cobalt-glacier` (#41) → landed by `thorn-spruce` |
| **4b** | **#98 — stamp the base a round was judged against.** The small follow-on to item 4, and it belongs there rather than in stage 3: #48 had to invent `head_sha` because no payload recorded which commit a round reviewed, and that is one end of a range. `base` is still a branch *name*, so a finding traces to the fix that caused it only while the base holds still — and this repo runs ~1.8 integration merges per PR landed (#80). Do it while #89's payload schema is still open and it is a few fields; do it later and it is a migration plus a backfill nobody can do (n starts at zero, same as #48). Consumes #82's `changed_files` for the overlap test. **DONE — PR #128 (v2.29, schema 0018), CI green.** It is TWO fields, not one, and the reason is the thing to carry: **`baseRefOid` is the MERGE BASE and does not move when the base branch moves**, so the check this issue specified could only ever answer "fresh" (evidence below). `merge_base` (the PR's base commit, free off the `gh pr view` call already made) and `base_sha` (the base branch's live tip, its own cheap `git/ref/heads/` lookup) are separate and neither is derived from the other. **v2.29 moves the board, so it needs a Portainer redeploy.** | 1 | **done** | `opal-kelp` |
| **5** | **#79 → #84 → #77 → #78** — cheap challenge, then the futility brake, then measurement, then policy. **This order is a dependency chain, not a preference.** **#79 DONE — PR #117 (v2.27), CI green**: `panel.py --ask "<premise>" --context <path[:a-b]>`, one question to the seats, no diff/judge/cycle, exits 0 on every verdict. Three things that outlive it. **(1) The self-challenge rule has to be checked on the ANSWER, not on the panel** — "the asker cannot be the only seat" is not enough, because with `ask_threshold: 1` the asker reaches the threshold on its own vote while every other seat says `cannot tell`; #78's `self_approval` wants the same distinction. **(2) `cannot tell` and "unreadable reply" must never merge** — the first is in the quorum and never the threshold, the second is in neither. **(3) The board half is deliberately NOT built**: `record_ask` pipes to a `qb record-ask` that does not exist yet (qb is in nix-fleet) and the row's shape belongs to #77, which is what reads it — the payload is already emitted complete. Also: **`run_seat` is now extracted out of `review_llm`**, so one implementation runs a seat for both a round and an ask — put anything touching seats there, not in a second copy. **#84 is next and now has a premise DECLARATION to work from.** **#79 MERGED as PR #117 (v2.27)** on 2026-08-16 — and it carried work its author never committed: a secret denylist for `--context` (`.git/` refused outright, plus `.env`, `.envrc`, `.npmrc`, `.netrc`, key material), 165 lines of tests, all green, found UNCOMMITTED in the worktree after `bramble-otter`'s session ended. Containment was never the whole rule: the repo under review is exactly where the credentials are, and `--context .git/config` is a PAT handed to four third-party CLIs. **"A stopped agent may have finished" has now paid for itself twice.** **#77 LANDED 2026-08-17 as PR #151 (v2.37), so the chain is #84 → #78 now, and #44 is unblocked.** | 2 | **#79 + #77 done · #84 FREE** | `bramble-otter` (#79) |
| **5b** | **#107 — deliver a board message mid-turn, instead of at the next prompt.** Independent of item 5's chain and **pullable forward to right now**: a hook change in nix-fleet, no server change, `/board` already filters by `to=` and `since=`. `qb-hook:379-400` polls for asks addressed to this agent and does it well — per-session cursor, 45s throttle, fail-open — from inside the `UserPromptSubmit` branch only, so a headless `/epic` or `/lander` run polls once at the start and then never. **The only channel to a running unattended loop today is `kill(1)`.** Second consumer: **"origin has changed underneath you"** — `advice()` (`app/sync.py:116`) already writes that line and it fires at SessionStart and per-prompt behind a 300s throttle, so a long run gets one check, before anything has moved. Wants a per-turn debounce, and **verify the sub-agent cursor before shipping**: `INSTANCE` falls back to `sid[0:8]` but is inherited if `QUARTERBACK_INSTANCE` is exported, and the cursor file is keyed on it — a sub-agent would swallow its parent's asks, which is the machine-wide bug at `qb-hook:374` a second time and much worse once the poll is per-45s rather than per-prompt. **Two detection gaps behind the origin half are still UNFILED**: nothing ever fetches (the `behind` count is as fresh as the last time somebody happened to), and **a GitHub-side merge emits no `published` at all** — the merge button and `gh pr merge` do no local push, so the most common way origin moves here is invisible to both signals. **DONE (soft level) — nix-fleet PR #13, 24 tests, shellcheck clean.** The three prerequisites were all ANSWERED EMPIRICALLY rather than reasoned about, because the hooks doc truncates its decision-control table exactly where the answer is. **(1) `PostToolUse` does honour `additionalContext`** (Claude Code 2.1.232) — this was the load-bearing assumption of the whole design and nothing had checked it; delivery is framed as `PostToolUse:<Tool> hook additional context: …`, not pasted raw. **(2) The sub-agent cursor hazard is REAL and now has an exact discriminator**: `session_id` is NOT distinct for a sub-agent, so the cursor is shared — but `agent_id`/`agent_type` are present *only* inside a sub-agent, and absent on the main thread. One field closes it. That also answers the issue's open question: `PostToolUse` does fire inside a Task loop. **(3) `prompt_id` exists and names the TURN**, so the per-turn debounce keys on a real boundary instead of an invented one. Hot path measured at ~12ms vs master's ~8ms, zero network when no poll is due. **The two detection gaps are now FILED as #125 (nothing fetches) and #127 (a GitHub-side merge emits no `published`) — two issues, not one, and the reason is on #125**: same symptom, different systems (a client-side number that is never refreshed vs. an event that is never emitted), so fixing either alone leaves the signal blind. **Hard (`PreToolUse` deny) and terminal (`Stop` block) levels deliberately NOT built** and still FREE. | 2 | **soft done, PR #13** · hard/terminal **FREE** | `nimbus-sorrel` |
| **6** | **#99 + #46's allocator half** — the resource-keyed lease, and **both kinds off one table**. Do this pair FIRST in stage 3: it is the only item here that pays today, and it is the standing "is a claim a lock or an advisory?" question finally answered for a merge. **NINE collisions now, not six — and 2026-08-16's three change the design, so read the new comments on #46 and #99 before building.** (a) **The eighth is the first where the number was claimed on the board IN ADVANCE and taken anyway** — an hour ahead, addressed to the other agent by name. #46's existing argument against announcing is *simultaneity* (two claims one second apart); this one was simply never read, which cannot be fixed by announcing sooner. **Announce-before-you-write is now falsified, not merely interim.** (b) **The eighth and ninth were RENUMBERS into a claimed number, not fresh picks, and the proposal only covers the fresh pick.** `POST /release/claim` → next free models claiming once, at the start; the dangerous moment is the *replacement*, because choosing the first number feels like a decision and replacing it feels like bookkeeping, so nobody announces and nobody re-reads. `reclaim` has to be ONE atomic swap — release-then-claim reopens the identical race, and the holder hits it exactly when the namespace is contended. (c) **`kind=release` and `kind=merge` differ on eviction**, which is an argument for the shared table rather than against it: losing a merge claim means waiting, losing a release claim means everything you have already written names the wrong number. Say so in the implementation. (d) **#107 is the natural companion** — in both collisions the loser found out from a human, after the merge. Not a dependency. `harness/tests/test_release_numbers.py` (#76) was green on both sides of all three: each branch is self-consistent, which is all it can see. **DONE — PR #131 (v2.31, schema 0019), CI green, stacked on #128.** All four points above are built, including (b): **`POST /release/reclaim` is ONE transaction**, the old row released in the same commit that takes the new one, rolled back together on failure — you keep what you had or you get the new one, never neither. Two bugs in it were found by a CONCURRENT test and were unreachable sequentially, which is the reusable lesson: a lazy load outside the greenlet on the retry path (reached only when contended), and an allocator that handed out `3.1, 3.2, 3.3, 3.2` because it borrowed `/claim`'s same-machine renew — right for a merge claim, wrong for a release number, since two agents on one box are two BRANCHES here. | 3 | **done** | `opal-kelp` |
| **6b** | **~~#97~~ → #96 → #100** — the per-merge half. **#97 DONE — PR #123 (v2.29), CI green, mergeable clean**: `scripts/migration_reconcile.py`, verbs `preflight`/`apply`/`heads` with lexray's exit codes (0 go · 3 merge-migration · 2 STOP) so #96 can treat both repos identically. Computed from files at a git ref, never from a live DB. **The thing #96 must not re-derive: the donor reports a duplicate-`0018` graph as CLEAN** (see the handoff note above) — quarterback numbers revisions by hand, so a collision is on the ID and not just the filename, and the resolution is renumber-and-relink. Collision is told apart from a rewrite of shared history by the revisions at the **merge base**; prose naming the old number is reported with file:line and never edited. **#96 is next and now has a real guardrail to capability-detect.** #100 last, and before stage 4. **#96 LANDED 2026-08-17 as PR #147 (v2.35) — `preland.py`. #100 is now unblocked and FREE.** | 3 | **#97 + #96 done · #100 FREE** | `heather-cinder` (#97) |
| **6c** | **#80 + #83** — the integration ceiling and rebase-on-publish. Ask first whether #41 collapsed it. **#99 and 6b do not answer #80** — they make each integration safer, not rarer, which is the opposite axis. **#107 (item 5b) takes the hard half off #83**: a worktree somebody is *sitting in* is the case #45 was filed for, and telling that agent origin moved needs no holder check, cannot destroy uncommitted work, and decides nothing. #83 keeps what is left — the worktrees nobody holds — which is the half it can do safely. | 3 | **FREE** | — |
| **7** | **The deferred backlogs** — #66 (14), #69 (12), #72 (28), #74 (42): 96 findings, one pass. **#74's carry a health warning** — three of #64's r1 P2s were plain wrong. Do after #77, which gives them somewhere to point. | 2–3 | **FREE** | — |
| **8** | **#55, #53, #54** then **#85 + #86** then **#63.** The epic, in its stated order, with the two appetite gates landing before the issue watcher. | 4–5 | **FREE** | — |

> **The tooling detail for stage 3 lives on #80, #81, #82 and #83** rather than here, because it is
> long: `rerere` is off and `rr-cache` is already shared across worktrees (they share the common git
> dir); the board holds `changed_lines` as a bare int so it cannot yet order by collision; and
> rebase-on-publish is safe only because `worktree-holder` landed today — auto-rebasing a held
> worktree is precisely the disaster #45 was filed for.
>
> **`changed_lines` as a bare int is now fixed — MERGED, `8534763` (v2.23, schema 0016).** A run records
> `changed_files` (the PR's paths, each with its own additions/deletions) plus `changed_files_total`.
> **CORRECTION (2026-08-16, `nimbus-sorrel`): `GET /review/collisions` did NOT land and is not on
> `main` — `grep -rn collisions app/` finds nothing.** This paragraph said it returns the other PRs
> sharing files; it was written from #88's branch, and the read side was deliberately PULLED and
> became **#101** after two full 4-of-4 rounds put the same defect in it twice (a filter composed in
> front of the newest-run selection, and r2's instance was introduced by r1's fix). The datum landed;
> the query did not. **#80 therefore inherits a table, not an endpoint** — the datum but not the
> policy — and the endpoint, when #101 builds it, should describe the overlap and not rank it,
> because ranking needs a decision about what a collision COSTS that #80 owns. Three things #80
> should not have to rediscover: (a) the list is read from `gh pr view --json files`, **not** from the
> diff, so it stays the PR's file set and not the round's — which is what keeps it correct under #41,
> and is why the title-skip path records a complete list despite never fetching a diff; (b)
> `changed_files_total` is GitHub's own count and is never derived from `len(files)` — GitHub caps a
> PR's file list at 3,000, and the two disagreeing is the ONLY evidence the list is a prefix; (c) a PR
> whose newest run has no file list comes back under `unknown`, never omitted, because an empty
> `collides` beside a silently absent PR reads as "safe to land".

**Ordering rule for anything not listed:** #78 and #79 come before the epic (#52). An unattended
process needs a constitution, and today every rule governing a merge is hard-coded or written in
prose in this untracked file. The watcher industrialises whatever loop it is pointed at.


**Nothing is running (as of 2026-08-15 ~21:20 — LOG, not current; see the state block at the top).**
#75 and #76 each completed two rounds and **both are
merged**; all four payloads are in `~/.local/state/quarterback/panels/` per the convention above.
Both ran **3 of 4 seats in both rounds** (antigravity out of quota, 147h), which makes them the
first like-for-like r1→r2 pair in the convergence table.

#62's round 3 finished at 19:51 and posted to the PR (payload now at
`~/.local/state/quarterback/panels/panel-62-r3.json`, 3 of 4 seats). **The one thing wanting a decision is
#62**, and the decision is *not* "fix the P1s": see the PR board, where round 3 turns out to be
#67's first three-round circle. A fourth proxy is the move that has already failed three times.

**#88 is superseded by #102** — the datum half, CI green. The collision endpoint it also carried is #101. Earlier note follows.

**Three PRs were open: #62, #87 and #88.** #88 is #82 — the PR's changed-file list reaching the board,
plus `GET /review/collisions`. CI green (775 tests: 477 harness across both suites, 298 app), migration
0016 round-trips, no panel round run on it yet.

**Two PRs were open when this paragraph was written: #62 and #87.** #62 is mine, fixed, CI green, and carries one judgement call
someone else should weigh (`sub_pr_merge` defaulting to `gate` — it argues with #78). #87 is
whoever took queue item 3. Deferred findings live in #66, #69, #72 and #74.

> **If you are about to run a panel, note that the seat count has moved three times today** — a
> panel of one on #64, a codex timeout at 1800s on #32, and antigravity out of quota (147h) on
> #62's r3. **#75 has landed, so a report now says `N of M configured`** on every run and shouts
> when a panel is short — you no longer read `reviewers_ran` against `reviewers_selected` by hand.
> It also means the launch directory no longer decides who sits on the panel: each member runs in
> its own empty sandbox repo, so the "launch from inside a git checkout" trap below is closed for
> anything running `panel.py` from `main`.

**#61's round 3 answered the question it was run to answer — the redesign held, and it merged.**
Findings were a **different class**, six P2s fixed (`99ba651`), twelve P3/P4 deferred to **#69**.
The evidence, because this is #67's first live test and the reasoning is the point:

> r1 and r2 both circled ONE assumption — that the reviewer's answer can be picked by *scoring*
> candidates. r1 filed against `_reported`/`_declared`/`_richest`; r2 against `_rank` putting
> `len(findings)` first, placeholder neutralisation, `max(_reported(c) …)`, the `parsed[-1]`
> fallback. Same premise, patched twice. r3 reviews the redesign that **deleted** ranking for
> agreement, and **not one of its 18 findings says a score picked the wrong candidate** — there is
> no score. They are defects *inside* the new mechanism: `_quoted`'s scalar rules weaker than its
> "whole example, positively identified" claim (F01), tier 2 admitting any non-empty array so a
> prose `[42]` is a rival candidate (F03), `_findings_of` raising `TypeError` and aborting the run
> (F04), `_str_list` turning a declared gap into "nothing to declare" (F05), `adjudicate` lacking
> the retry `review_llm` has (F06).

**Two caveats that travel with that verdict, not under it:**

- The panel called it **"a stop, not convergence"** — the cap did the work and 18 findings ship
  unreviewed. Correctness only (the six P2s); defer P3/P4.
- **Neither reviewer could execute anything.** claude had Bash/Write declined *and* could not fetch
  the branch, so it read `panel.py` at main and applied the diff mentally; codex could not run the
  suite either. The class judgement is about what the findings are *about* and survives that, but
  each individual finding is diff-reading, not tested. Confirm against the branch before fixing.
- The honest nuance: F01 and F03 both concern whether a non-answer can be mistaken for the answer,
  which is the same underlying question #43 exists for. That reads as the residual difficulty of
  the problem rather than the wrong assumption re-patched — but anyone arguing the redesign only
  *relocated* the difficulty has those two to point at.

**All six P2s were reproduced against the pre-fix branch before being touched**, precisely because
of the second caveat — and all six were real. Worth recording as evidence about diff-only reviews:
`F04` raised `TypeError: 'int' object is not iterable`, `F05` returned `([], [])` where a declared
gap should have vetoed, `F02` gave two conflicting coverage-only replies one reading, `F03` turned
a prose `[42]` beside a real array into ambiguity. F01 and F06 are latent, and fixed as such.

> **F01 is a fix to a coincidence, not to a live bug** — the one judgement call in that pass.
> `_quoted` wildcarded any scalar, which is right today only because every scalar in both schemas
> comes from `<int|null>` or `true|false`. Tokens now carry through `_schema` as themselves, so the
> wildcards are the prompt's tokens and the docstring's "every key and every phrase" is true rather
> than aspirational. Behaviour on today's prompts is unchanged.

**The next things, in order:**

1. **#62 — the round-3 verdict is in, and it changes the usual terms.** Not "fix the P1/P2s, defer,
   merge": both P1s are the *same premise* r1 and r2 already patched twice, so a third fix pass is
   a fourth proxy. Delete the premise instead — gate on the panel's own `reviewed` / `skip_reason`
   / `reviewers_ran` rather than on any side effect — and it still wants a human, because this is
   the gate that decides what gets auto-merged. Full reasoning on the PR board below and as a
   comment on the PR.
2. **The retro's three (#78, #79, #77)** — filed from a look back over today's 17 rounds, which
   produced 438 findings of which ~38 were fixed, 96 deferred and **~304 (69%) neither fixed nor
   filed anywhere**. #78 is the constitution (quorum ≠ threshold; the judge is currently the same
   model as a seat); #79 is the cheap premise check that would have saved #62 two of its three
   rounds; #77 is the missing feedback edge — a confirmed finding later refuted is recorded nowhere,
   so the leaderboard rewards confidence rather than correctness.
3. **The deferred backlogs**, now four of them: **#66** (14, from #29), **#69** (12, from #61),
   **#72** (28, from #32), **#74** (42, from #64). They are the accumulated cost of the cap rule and
   they are worth a pass of their own rather than trickling into unrelated PRs. #74's carries a
   warning: check its conditional findings before acting, since three of #64's round-1 P2s resolved
   to "no bug" on inspection.
4. ~~**#68**~~ **done — PR #75 (v2.21), open, CI running.** And the answer is worth having in the
   plan rather than only in the PR, because **the issue's stated suspect was wrong**: it was never a
   race between concurrent codex processes. `run_cli` passed no `cwd=`, so every reviewer ran in
   whatever directory the panel process inherited, and **codex refuses to start outside a git
   repo**. The three panels' inputs were never identical — one was launched from the `/tmp`
   scratchpad. Evidence, both cheap: a non-repo directory reproduces #64's exact error before any
   model call, and `/proc/<pid>/cwd` on a live panel's codex child showed `~/source/quarterback`,
   which is **not** in codex's trust list — so being inside a repo is sufficient and trust is not
   the gate the message's wording implies. `--skip-git-repo-check` is therefore **not** needed once
   cwd is pinned (checked against an untrusted checkout *and* an untrusted worktree). Still open:
   **#46**, four release-number collisions, two of them mine.
5. **#67 now has the data point it was waiting for, and it is a three-round circle.** #62's r1/r2/r3
   trusted the exit code, then the push, then the payload artefact — one premise, three proxies,
   each killed by the next round. Every earlier signal was a *contrast* between rounds (#61's r3
   naming none of r1/r2's mechanism); this is the first time a single PR shows the circle itself,
   in three consecutive rounds, in one file. It is also the first case where **the count actively
   misleads**: 26 → 37 → 28 looks like convergence and is not. Whatever #67 becomes, this PR is its
   test case.
5. **#67's older data.** #61's round 3 (a different *class* of finding after deleting the wrong
   premise) and #32's round 3 (severity collapsing to one P2 while P4s rose) are the two positive
   signals; #62's two P1s against its own round-1 fix are the negative one. All three say the signal
   is in what findings are *about*, not how many there are.

**Conventions that are not obvious:**

- **The diff between two round heads is NOT "what the fixer wrote", and the gap is huge.** A PR
  diff is `base...head` and excludes whatever the base branch gained; a round-to-round range is
  `prev_head...head` and *includes* it. On this repo a fixer merges main in constantly (eleven
  integration merges to land six PRs — #80), so the two diverge badly: measured on **PR #62, the
  raw range between two of its own round heads was 92,415 chars against a 45,370-char PR** — the
  "increment" was **twice the size of the whole thing**, carrying `flake.nix`, the worktree
  scripts, CHANGELOG and README. Cut it to the PR's own files (19 → 5 there, 17,075 chars) and
  guard on size. Anything reasoning about "the last fix pass" — #41's scope, #48's provenance,
  #67's circling — is wrong by that margin unless it does this. Also: **GitHub's compare API 404s
  on two-dot `a..b`** and accepts only `a...b`, so there is no raw-tree-diff escape hatch.
- **Claim before touching someone else's branch.** No claim primitive exists yet (#60), so leave a
  PR comment saying who you are and that you are picking it up. Both #29 and #61 were claimed that
  way after 15+ minutes of silence and a clean tree. If the branch is *live*, leave it alone.
  **And do not expect a live agent to hear you.** A PR comment nothing reads, or a directed board
  `ask`, reaches a running session only at its next prompt (#107) — for a headless run, never. The
  15-minutes-of-silence rule is not politeness, it is the absence of a channel.
- **Run `panel.py` from `…/scratchpad/loops/`, not `~/.claude/loops/`.** The installed harness is a
  release or two behind `main` (home-manager has not been rebuilt), so it lacks `--round`/
  `--baseline`. Refresh with `git show origin/main:harness/loops/{panel.py,harness_rules.py}`.
- **Launch a panel from inside a git checkout, or you silently lose the codex seat.** codex exits 1
  with "Not inside a trusted directory and --skip-git-repo-check was not specified" when its cwd is
  not a repo — and until PR #75 the reviewers inherited the launching shell's cwd, so running
  `panel.py` from the `/tmp` scratchpad cost the seat while `--repo` pointed at a perfectly good
  checkout. This is what happened to #64. Being inside *any* git repo is enough; the trust list is
  a red herring (quarterback is not in it and codex runs there fine). #75 pins cwd to the repo
  under review, after which the launch directory stops mattering — but the installed harness under
  `~/.claude/loops/` is a release or two behind, so **this trap is live for anything not run from
  `…/scratchpad/loops/` with a refreshed `panel.py`.**
- **A panel launched with `nohup` outlives its session.** All three of 15:47's runs were still alive
  and finished normally after the launching session ended; the 0-byte logs meant buffering, not
  death. Check `pgrep -f 'panel.py --repo'` before concluding a run died.
- **A harness-only change is NOT harness-suite-only — run `tests/` as well.** `tests/test_v215.py`
  drives a whole `panel.run()` end-to-end to prove a payload records to the board and reads back,
  with its own `review_llm`/`adjudicate` doubles. #75 passed all 413 harness tests and broke it;
  #71's CI caught it on the push. So the note below ("a harness-only change never needs a database")
  is **wrong in the case that matters** — that e2e test needs one.
- **And there are TWO harness suites, not one.** `harness/loops/tests/` (408) is the one everybody
  runs; `harness/tests/` (41) also exists — `test_worktree_holder.py`, and since #76 landed,
  `test_release_numbers.py`. CI runs *every* harness suite, so a green `harness/loops/tests/` proves
  less than it looks like it does. #87 learned this from CI: it ran 408 locally, called them "all
  the harness tests", and was told by the release checks that its `v2.22` heading had no matching
  README entry. `uv run python -m pytest harness/tests/ harness/loops/tests/ -q` is the honest
  local command; 466 is the number to see.
- **A PR's CI runs the MERGE of your branch with `main`, not your branch.** So a check that landed
  in `main` after you branched fails your PR while passing locally, and the fix is `git fetch` +
  `git merge origin/main` rather than a hunt through your own diff. This is how #87 discovered #76
  had merged.
- **`createdb` is not on `PATH`.** Make a scratch DB with
  `docker exec quarterback-postgres-1 psql -U quarterback -d postgres -c "CREATE DATABASE …"`, and
  drop it the same way. `harness/loops/tests/` needs no database at all — only the app suite does,
  so a harness-only change never needs one.
- **`.gitignore` in a worktree is `skip-worktree`.** `create-worktree` appends a local
  `*.code-workspace` rule and sets the bit so it can never be committed. The cost: `git status` says
  clean, `git diff` shows nothing, and `git merge` still aborts with "local changes would be
  overwritten". Clear it (`git update-index --no-skip-worktree`), stash, merge, re-append the local
  block, set the bit again. Check with `git ls-files -v .gitignore` — a leading `S` is the flag.
- **Never write a reviewer's conjecture into a comment without checking it.** It launders a guess
  into repo truth, and one round later nothing can tell it from a design decision. Live example, and
  it cost a P2: three of #88's four seats argued a finding from *SQLite's 999-variable limit* while
  **all four declared the production DB backend unverifiable in the same payload**. The fix pass
  wrote the conjecture into a comment; round 2 read the comment back as the module's position and
  filed "either SQLite is supported and this is broken, or the comment is stale". Both branches were
  wrong — `grep plpgsql migrations/` settles it in one line. **quarterback is Postgres-only and always
  has been**: migration `0001` creates a plpgsql `NOTIFY` trigger, `LISTEN/NOTIFY` *is* the SSE live
  leg, 11 of 16 migrations use PG-only DDL, `dbtarget.py` has no sqlite branch. Attribute it
  ("codex conjectured") or verify it — never assert it.
- **A finding's argument can rest on the same reviewer's own `could_not_assess`, and nothing links
  them.** The declaration sits in a block at the bottom of the report; the finding travels onward
  without it. Check a finding's premise against its reporter's declared gaps before acting — filed
  as a suggestion on #40.
- **The prose conflict is CAMOUFLAGE for the real one.** Landing four PRs on 2026-08-16 produced
  three hand-resolved conflicts and every one was in `CHANGELOG.md` + `README.md`, where the
  resolution is always the same: drop the markers, keep both sides, order by number. #90's merge
  carried four conflicts — three of those, and `harness/loops/panel.py`, which was **not**. #117 had
  changed `--round` to a sentinel (`round_no = 1 if args.round_no is None else args.round_no`) while
  #90 still passed `args.round_no` into `run()`. Taking "ours" — the correct answer for the three
  files above it — passes `None` where an `int` is required and breaks `--round`, and it merges
  clean either way. **A resolution you perform by reflex three times in a row trains the reflex**,
  and the volume of routine is what makes the camouflage work. Filed as #122. Until that lands:
  resolve the prose files first, then treat any remaining conflict as a different kind of thing and
  read both sides.
- **A clean merge can still lose work — check for DUPLICATED definitions.** #62 had moved
  `stderr_gist` into `harness_rules.py`; main already had its own copy there. Git conflicted on
  neither, kept both, and the second definition silently won — quietly reverting #29's
  settled-cause fix. Nothing flagged it but the tests. After any `panel.py`/`harness_rules.py`
  merge: `grep -n '^def ' <file> | sed 's/.*def //;s/(.*//' | sort | uniq -d`.
- **A stopped agent may have finished.** #61's redesign was committed, clean, and **unpushed** when
  its session died — one process exit from being lost. Always check the worktree before assuming.
- **Older worktrees' `.env` points at the MAIN checkout's database.** The test suite's isolation
  guard refuses to run there. Make a scratch DB, pin `DATABASE_URL` for the run, **drop it after**.
  Two were left behind today. Worktrees made after #30 are fine.
- **`main` in the main checkout goes stale** — nobody checks it out. `git fetch` before judging it.
  **And `create-worktree --from main` forks from that stale local ref**, not from `origin/main`, so
  a fresh worktree can start several merges behind even after you fetched. Check `git log -1` in the
  new tree; `git merge --ff-only origin/main` fixes it (`git reset --hard` is blocked by a guard).
  **And you cannot dodge it by passing `--from origin/main` — that is REJECTED**, with "Base branch
  'origin/main' not found locally or on remote". It wants a bare branch name, so the two-step
  (`--from main`, then `git merge --ff-only origin/main`) is the only route, not a nicety.
- **A harness change with no database still wants the app suite, and the worktree now HAS a database.**
  `create-worktree` provisions an isolated `quarterback_<branch>` DB (post-#30) and `.env` points at
  it on its own port — so `BROWSER_DEV_USER='' uv run python -m pytest tests/` just works, and
  **overriding `DATABASE_URL` by hand breaks it** (you get `InvalidPasswordError` against the wrong
  server). The scratch-DB dance in the note above is only for pre-#30 worktrees.
- **`antigravity` is structurally capped at ~120,000 diff chars, and it is the only seat that is.**
  Its prompt travels in **argv**, and the kernel caps a single argv element at 120,000 bytes; every
  other seat can read a prompt off stdin. On PR #115's 140,432-char round it silently read 83% and
  the panel said so in `config_notes` — which is the only reason anyone knows. This is a property of
  the harness rather than of a run, so it will recur on **every** diff over ~120k, and it means an
  antigravity `could not assess` on a big PR is not always a judgement about the code. Fixing it is
  a stdin path for that seat; until then, read its coverage declarations on large PRs accordingly.
- **A seat can now die on CREDITS, not just quota.** `pi` exited 1 mid-round on PR #115 with
  "This request requires more credits, or fewer max_tokens. You requested up to 131072 tokens, but
  can only afford 92788" — OpenRouter balance, which is a different failure from antigravity's
  147h quota reset and does not heal by waiting.
- **A round's `introduced` bucket has now been observed under-counting, on the release that
  documents it.** PR #115 r2 raised a P2 against `_log.warning` — a line r1's fix commit
  demonstrably ADDED — and provenance bucketed it `missed`, because the reviewer reported line 1595
  for code at line 1672. **77 lines of ordinary reviewer drift is enough to miss the added-line
  set.** That is the documented floor bias, measured rather than asserted, and it is the strongest
  argument yet for #41: an increment-scoped round makes a finding `introduced` by construction with
  no line arithmetic in the middle. The companion hazard — a merge in the fix range attributing
  someone else's commits to this PR's fix pass — was LIVE on that same range (nine commits of
  `origin/main`) and did **not** fire: all 25 findings landed in files the PR changed. Record both.
- **`tests/test_v215.py` drives a full two-round cycle, which makes it look like better cover than it
  is.** Its `_fake_sh` returns ONE `headRefOid` for every round, so anything that depends on the head
  MOVING between rounds is silently untested there — #48 found its provenance wiring in exactly that
  blind spot, passing the e2e test while never once computing a real fix range.

---

## What order gets to RUNAWAY

The goal is quarterback powering through its own backlog. The order that gets there is not "most
valuable first" — it is **whatever makes the loop's own output compound**, because a loop whose work
does not compound cannot be made to run away by running it more often.

**A stage is finished when its GATE is true, not when its issues close.** The gate is what makes the
next stage safe, which is why the order is what it is.

**Stage 0 — free wins (hours, no dependencies)**
`#81` turn on `rerere` · `#78`'s `judge_model` line only
> *No gate needed — both are one-liners with immediate effect. The judge stops being a party; the
> same conflict stops being resolved once per worktree.*

**Stage 1 — make the work compound** — **ALL FOUR ARE IN. #41 (v2.28), #48 (v2.24) and #82 (v2.23)
merged; #98 is PR #128 (v2.29), CI green. The gate's second clause now holds: a round records both
ends of the range it was judged against.** The remaining question is not whether they landed but
whether the compounding is REAL — nobody has yet run a cycle end to end with all four in and read
the numbers. That measurement is what would tell stage 2 it is safe to start, and it is unclaimed.
`#41` review the increment · `#48` attribution · `#82` send the changed-file list · `#98` stamp the base
> *Gate: a round gets CHEAPER as a PR matures, and a finding traces to the fix that caused it.
> Until this holds, everything downstream multiplies churn — of today's 438 findings, ~38 were
> fixed, 96 deferred, and **~304 (69%) neither fixed nor filed anywhere**, because every round
> re-reads the whole PR and re-derives.*
> *`#98` was filed after the other three and belongs here, not in stage 3, because the gate's second
> clause is false without it. #48 records `head_sha`; `base` is still a branch NAME. A range needs
> both ends, and on a repo running ~1.8 integration merges per PR (#80) the base end moves
> constantly — so "this finding was introduced by that fix" holds only until `main` moves under it.
> It is also the last cheap moment: the payload schema is open in #89 right now, and provenance
> already starts at n=0 because nothing can be retrofitted.*
> *Done as PR #128, and it took TWO fields rather than the one the issue asked for: `baseRefOid` is
> the merge base and does not move when the base branch does, so the comparison #98 specified could
> only ever answer "fresh". `merge_base` + `base_sha`, neither derived from the other. The full
> evidence is at the top of this file; the trap for anyone reading the issue cold is that its body
> still says the old thing.*

**Stage 1b — stop reviewers guessing at what they cannot see** *(added 2026-08-16 by `nimbus-sorrel`)*
`#91` hand the reviewers the CI result the panel already computes → `#113` give each seat read-only
access to the PR's code at its head
> *Gate: a finding is about code the seat could actually READ, and a question the process already
> holds the answer to is never spent as a coverage declaration.*
> *Placed in stage 1 rather than left unstaged — which is where both were — because this is the same
> axis as #41/#48: make a round cheaper and its output trustworthy. It is arguably higher-yield than
> either, because it removes WRONG findings rather than re-attributing them, and every panel run until
> it lands pays the tax.*
> ***The internal order is #113's own and it is right: #91 first.*** *It costs nothing (the panel has
> computed CI on every round since `review_ci` was written and thrown it away before anyone reviewed),
> it kills the "this new test never runs / may not even import" class outright, and **#113's value is
> whatever `could_not_assess` entries survive it** — so measuring after #91 is the difference between
> building #113 on evidence and on mood. #91 is **DONE, PR #136 (v2.32)**.*
> *The evidence, four PRs deep: **#64** — three of six P2s were worries about code outside the diff and
> the code answered all three; the proposed fix was the bug. **#90** — its P1 claimed `headRefOid` was
> never added to the `--json` list; it was already there, so it never appeared in the diff and the
> reviewer **inferred absence from invisibility**. **#123, today** — no seat could see
> `migrations/versions/`, the reconciler's entire subject, so nothing confirmed the repo's real chain
> parses under it; run by hand afterwards it was correct on all of it. **Every one of those was a
> filesystem away.***
> *The compounding cost is the structural part and it is why this is a stage-1 gate rather than a
> nicety: each `could_not_assess` becomes a `coverage_veto` line and `round_stop` computes `confident`
> as `not veto` — **so a seat's inability to read an unchanged file costs the whole ROUND its confident
> stop.** #92 asked the broader "may reviewers execute?" and answered no; #113 is the narrower half the
> evidence actually supports — reading, not executing.*

**Stage 2 — make it safe to act without a human**
`#79` cheap premise check → `#84` escalate on the second occurrence → `#77` record outcomes →
`#78` the rest, tuned from #77's data · `#107` reach a running agent
> *The internal order is a real dependency chain: #84 needs a premise to have been DECLARED, which
> is #79; #78's thresholds are guesses until #77 supplies data to set them from.*
> *Gate: the loop can stop itself, and can tell a confident wrong finding from a right one. Four
> judge-confirmed findings were plainly wrong today and a human is the only reason none were acted
> on.*
> ***#107 is the gate's other half, and it is deliberately NOT in that chain** — independent, and
> doable now. The four issues above build a loop that can stop ITSELF. Nothing lets anyone else stop
> it. The board can already address one agent by name and `qb-hook` already polls for that mail
> correctly — cursor, throttle, formatter, all of it — but only inside its `UserPromptSubmit` branch,
> so a headless `/epic` or `/lander` run polls once at the start and then never. **The only channel to
> a running unattended loop today is `kill(1)`**, which is why the four wrong findings were caught by
> a human watching rather than by anything in this plan. Stage 4 removes the human.*
> *It is a hook change, not a server change: lift the poll into a function and call it from
> `PostToolUse` too, behind the 45s `_throttle` that already exists and is a `stat` rather than a
> curl. Three escalation levels sit unused in the harness — `additionalContext` (soft),
> `PreToolUse` deny + `permissionDecisionReason` (hard, and scoped to `git push`/`merge`/`gh pr
> create`, never Edit/Write, so an agent finishes the edit it is in the middle of), and `Stop`
> `decision:block` (terminal, the only level that reaches a headless run that would otherwise
> finish). **The interrupt budget is the part that needs judgement**, not the plumbing: only directed
> asks may interrupt mid-turn, and the cap has to be per TURN rather than per poll, or two agents
> asking each other will ping-pong.*

**Stage 3 — absorb the output, and make landing a thing that can be checked**
`#99` + `#46`'s allocator (one lease, two kinds) → `#97` migration reconciler → `#96` the pre-land
verdict → `#100` the land offer · `#80` the integration ceiling · `#83` rebase on `published`
> *Ask #80's first question before doing #80: did #41 already collapse it? If re-review is cheap,
> re-integration mostly stops mattering.*
> ***Two gates, on different axes, and neither implies the other.** (a) Landing N PRs costs O(N)
> integrations, not O(N²) — today ~1.8 per merge, 11 to land 6, and against 28 open issues a
> per-issue PR queue would cost on the order of 378. (b) **A land is gated on a verdict a reader can
> check, and two agents cannot land at once.** #80/#83 buy (a); #96–#100 buy (b). Doing either alone
> leaves the loop able to run away in exactly one sense.*
>
> *The internal order is a dependency chain, not a preference:*
> - *`#99` **first, and built with #46's allocator half.** It is the only item in this stage that pays
>   today — six release collisions, and #46's own evidence says the last three are unfixable by
>   detection because two branches announcing one second apart are both correct. A resource-keyed
>   lease (`kind` + `key`) serves `kind=merge, key=<repo>:<branch>` and `kind=release, key=<repo>`
>   from one table with the passive TTL expiry the session lease already gets right. Two independent
>   atomic-claim implementations is the outcome to avoid.*
> - *`#97` **has no dependencies and can be done right now.** `migrations/versions/` is a hand-numbered
>   linear chain at `0016` (#88) with `0017` already spoken for (#89's board persistence). Two branches
>   both writing `0017` costs two heads AND a filename collision — lexray's hash-named revisions have
>   only the first, so its reconciler is a model and not a donor.*
> - *`#96` after `#98` (stage 1), because base-freshness is the verdict's most valuable input and the
>   only one that catches a stale review. **#98 is DONE (PR #128) and #96 must not build on the
>   issue's body**, which still specifies comparing a stored `baseRefOid` against the current one —
>   a comparison that cannot fire, since `baseRefOid` is the merge base and does not move when the
>   base branch does. Read the correction comment on #98. The stored pair is `merge_base` (the PR's
>   base commit) and `base_sha` (the base branch's tip at review time); the staleness trigger is the
>   CURRENT tip having moved beyond the recorded `merge_base`, and under #41's increment scope the
>   round's own anchor is `since_sha`, not `merge_base`. Four commits, four jobs — #128 has the test.
>   The asymmetry is #96's to keep: a disjoint-files result is "no overlap detected", never "the
>   review is current", and #128 asserts ingest publishes no verdict of its own.* Guardrails are capability-detected, so #97 and #46's
>   allocator get picked up by presence rather than by configuration — which is what lets one script
>   serve this repo, lexray, and a repo with neither.*
> - *`#100` last, and **before stage 4, not during it.** #54 industrialises whatever the landing path
>   is; if the offer does not exist when the watcher lands, the watcher automates a review that ends
>   in nothing.*
>
> *One warning, from this repo's own history: **#96's verdict is itself a merge gate, and #62 spent
> three rounds discovering that merge gates trust proxies** — the exit code, then the push, then the
> payload artefact. The settled fix was to gate on the panel's own statement (`reviewed` /
> `skip_reason` / `reviewers_ran`) rather than on any side effect of it. `preland.py` must be built
> that way from the start: a verdict that infers "the guardrail ran" from an exit code or a written
> file is the fourth proxy, and it will be found by the round that reviews it.*

**Stage 4 — run it unattended**
`#55` caps · `#53` worker · `#54` PR watcher
> *Gate: nothing in this stage changes what a decision MEANS — only who triggers it. If that is not
> true, a stage above it is unfinished, and the watcher will industrialise whatever it is pointed at.*
> *The sharpest instance of that gate is now #100: a watcher that queues panels while the landing
> path is still `gh pr merge` with nothing in front of it has automated the finding and left the
> deciding to a human who is not there. `/fix-and-land` is the same shape and already exists — its
> §4 gate is prose today, which #96 is what fixes.*

**Stage 5 — unleash it on the backlog**
`#85` appetite gates and `#86` no design/judgement work — **both before** — then `#63`
> *Gate: it picks up only what a human labelled, refuses design and decision-owed work, and files at
> most one issue per run. #63 writes code and opens PRs; it is the only item that can grow the
> backlog faster than it drains it.*

**Ordering rule for anything not listed:** #78 and #79 come before the epic (#52). An unattended
process needs a constitution, and today every rule governing a merge is hard-coded or written in
prose in this untracked file.

## PR board (2026-08-16 ~11:45 — the four below the fold are all MERGED now)

**Current, as of 2026-08-16 ~11:45.** `main` = `ace8e04`, CHANGELOG **v2.28**, served **2.26.0** and
verified deployed. Everything in the older table below this one has landed.

| PR | branch | state | next action |
|---|---|---|---|
| #87 | `fix/issue-81` | **OPEN, CI green, CLEAN.** r1's 27 findings sat unfixed overnight; 12 fixed in `8e97a93`, **r2 running** (anchored with `--since 112ef4e`, since the r1 payload predates `head_sha`) | land at the cap when r2 posts. It is the **first live test of #90's fallback guard** on a range containing a base-branch merge |
| #128 | `feat/issue-98` | **OPEN, CLEAN**, v2.29, schema 0018 — `opal-kelp`. Stage 1's last item; no panel round yet | not mine. Its number is safe |
| #123 | `feat/issue-97` | **OPEN, DIRTY**, the migration reconciler — `heather-cinder`. **Claims v2.28, which `main` already has**: the EIGHTH collision, and it happened in the same minute #90 merged. Wants **v2.30** (v2.29 is #128's) | not mine — told the owner on board 2772 rather than touching the branch |

**The four PRs that were open this morning are all merged:** #115 (v2.26, schema 0017), #62 (the
epic gate — the `sub_pr_merge: gate` judgement call landed as written), #117 (v2.27), #90 (v2.28).
Every one of them conflicted on exactly `CHANGELOG.md` + `README.md`, whether or not its number
collided — which is #122, and is a different failure from #46/#99's allocator.

---

**The older board, kept for its reasoning (2026-08-15 ~19:50):**

| PR | branch | state | next action |
|---|---|---|---|
| #62 | `fix/issue-31` | **round 3 is in** (19:51): 28 findings, **2 P1s, and they are the same premise a third time** — see below | do NOT patch a fourth proxy; human decision |
| #75 | `fix/issue-68` | **MERGED** (v2.21, closes #68) — 2 rounds, 1 P1 + 8 P2 fixed. The P1 killed the original design | done |
| #76 | `fix/issue-46` | **MERGED** — #46's smaller half; 2 rounds, 9 P2 fixed, four of them holes in the check itself. **#46 stays open for the allocator half** | done |
| #89 | `fix/issue-48` | **MERGED** (`e57464a3`, v2.24, closes #48) — #48's provenance signal: `introduced` vs `missed` per new finding, + `head_sha`/`unread_files`. Two rounds, cap reached: r1 28 findings (6 P2) fixed, r2 33 findings (8 P2) — **zero overlap**, 5 P2s fixed, 2 documented, 25 deferred to **#104**. `main` verified after: 564 harness + 299 app, no duplicated defs | done. **Board half landed as #115** (v2.26); part 2 is #77; part 3 (cycle summary) is untracked |

| #115 | `feat/issue-93` | **OPEN, CI green, TWO ROUNDS DONE, at the cap** (v2.26, schema **0017**, closes #93) — #48's four payload fields reach the board (`provenance` per finding, `head_sha`, `unread_files`, `provenance_counts`) plus the leaderboard axis at two grains. **No harness change** — the panel had been sending all four since #89 and pydantic's `extra="ignore"` was eating them. r1: **4 of 4 seats, nothing truncated**, 24 findings (4 P2), all fixed. r2: 3 of 4 (`pi` out of OpenRouter credits), 25 findings (2 P2), both P2s fixed, **23 deferred to #120**. 0 P1s in either round; P2s 4→2. 349 app + 568 harness | **ready for a human.** Moves the board — needs a Portainer redeploy. Whoever takes #98 inherits `head_sha`; the base end is the half left |

| #90 | `feat/issue-41` | **OPEN, CI green** (v2.25 — **COLLIDED, RENUMBER NEEDED**: `main` took v2.25 on 2026-08-16 for the codex-seat fix, the seventh collision) — round 2+ reviews the increment in three tiers, budget spent target-first. **The naive version was measurably worse than doing nothing**: see the convention above (92,415 vs 45,370 on #62). Falls back to whole-PR scope, loudly, four different ways | panel r1 wanted. **Rebase onto #89 when it lands** and drop the duplicate `head_sha`/`Baseline`; compare `truncated_rounds` against `unread_files` |

> ### #41 and #48 interact in a way neither issue records — **decided, and #41 changed for it**
>
> **RESOLVED (~22:00).** `marten-tidal` put the argument below to `cobalt-glacier` (board 2459) and
> it was correct against what #41 had actually written — its brief said "earlier rounds have already
> reviewed that code … so do not re-report it", which is exactly the rule that zeroes the bucket.
> **#41 now scopes on "a defect an earlier round already RAISED", not "anything outside the
> target"**, and both briefs say explicitly that earlier rounds *read* that code and reading is not
> the same as being right about it. Cut context was promoted from a note to a **veto** as a direct
> consequence: the context is the only place a scoped round can find a pre-existing defect, so a
> round that could not see all of it must not report its quiet as convergence.
> **Not taken: a full-PR read on the last round.** The cap rule fixes least on the last round, so
> that spends the most budget on the lowest-yield pass. Argument on board 2483; revisit if the data
> disagrees.
>
> The original reasoning, kept because it is the evidence:
>
> **If a round reviews only the increment, the `missed` bucket goes to zero by construction** — not
> smaller, structurally unreachable. A round that reads only what the fix touched cannot find a
> defect that was sitting in the earlier round's diff, so every finding it raises is `introduced` by
> definition. #48 says as much from the optimistic end ("#41 would make this exact"), and it is
> mostly right: attribution stops being a line-intersection guess.
>
> The catch is that the two buckets then stop measuring the loop and start restating which scope
> ran. On PR #75, `missed = 12` is real information — twelve defects sat in round 1's diff and round
> 1 did not see them. Under pure increment scope those twelve are not attributed differently, they
> are **never found by anyone in any round**, and the loop looks converged because it stopped
> looking. Given that ~304 of today's 438 findings went nowhere, "round 1 misjudges a lot" is the
> well-evidenced case, not the paranoid one.
>
> Two cheap things keep it honest, and #41 is where they belong: **record the scope in the payload**
> (`scope`, which #41 is already adding) so provenance can print "missed: n/a under increment scope"
> rather than a zero that reads as good news; and **keep a full-PR read on the last round** or on
> some cadence, so something eventually re-reads the parts round 1 misjudged. The stage-1 gate — a
> round gets cheaper as a PR matures — still holds if the cheap rounds are the middle ones.

> ### #75's round 1 changed the fix, and it is worth reading before touching `panel.py`'s seats
>
> The P1 (codex + claude, ⋆consensus) says pinning every reviewer's cwd to the repo under review —
> the original #68 fix — hands that repo a channel into the reviewer judging it: a headless CLI
> resolves CLAUDE.md, `.claude/settings.json` and **hooks, which execute commands**, from its cwd.
> Under the epic that points at exactly the untrusted-contributor population the panel exists to
> read. It is not quarterback's problem today (no such files here) and squarely the problem of the
> repos #39 and #59 aim the panel at.
>
> **And it bought no access in exchange**, which is the half that decided it. `cfg["path"]` is the
> MAIN checkout on whatever branch it was last left on — never the PR's code, which the panel reads
> as a diff and never checks out. A tool-capable seat pointed there can Grep a different branch and
> quote it as the code under review: a plausible wrong answer replacing a visible failure.
>
> So the premise went, not the proxy. **Each member now runs in its own empty `git init`ed sandbox**
> inside the private temp dir it already had. codex is satisfied (verified against a bare `git
> init`, an untrusted checkout and an untrusted worktree), nothing is exposed, and no seat can
> reach another's. The members never needed a working directory — they needed a reproducible one.
>
> Three further P2s were bugs in the new report block itself, every one the class the PR is named
> for: a panel that lost **every** seat was told "it takes two reviewers to agree, and one filed";
> `panel degraded` fired for a CLI the host simply does not carry (`coverage_veto` already argues at
> length why that must not count, and it would print on every unattended run of a repo enabling a
> workstation-only vendor); and consensus was counted over LLM seats while sonar's soft findings are
> judged alongside them, so a `["claude","sonarqube"]` finding could carry ⋆consensus under a header
> saying consensus was impossible.

> **#75's fix is verified end-to-end, not just unit-tested** — and the verification doubles as the
> second independent refutation of #68's race hypothesis. From a directory that is *not* a git repo,
> the same `run_cli` called twice with identical argv: **with** cwd pinned → exit 0; **without** →
> the exact `Not inside a trusted directory` error that cost #64 its seat. Same second, same
> process, and **four other codex processes were live at the time** (the two panels above). So
> concurrency was present in both halves and changed nothing. The pinned run used a *worktree*, so
> the `.git`-file case is covered too.

> ### #62's round 3 is #67's first three-round circle, and it decides what to do next
>
> r1 trusted **the exit code**. r2 trusted **the push**. r3 trusts **the payload artefact**. Three
> rounds, three different proxies for *"the review happened"*, each one found by the next round to
> be not evidence of it. Only the proxy changed; the premise never did.
>
> | round | proxy trusted | how it died |
> |---|---|---|
> | r1 | exit code | "judges `/review-pr` by exit code alone"; "a tool-denied review is recorded as `reviewed`" |
> | r2 | the push | "`before`/`after` proves only that the head moved, not that a review happened" |
> | r3 | the payload artefact | "every exit-0 path in panel.py routes through `write_payload`" |
>
> **The r3 P1 was verified against `main`, not just read off the diff** — `panel.py:3561` writes a
> payload on the title-skip path and exits 0, and `_payload_defaults()` (`panel.py:3342`) supplies
> `reviewed: False` / `skip_reason: None` / `judged: False`. Its docstring says so in as many words.
> So a skipped PR yields `panelled=True, found=0, verified=True` → auto-merge, on a PR nobody read.
>
> **The standing rule applies and this is its second live test**: report the premise, do not patch
> it. The fix is small because the payload already carries the discriminator — gate on `reviewed` /
> `skip_reason` / `reviewers_ran`, the panel's own *statement* that it reviewed, and there is no
> proxy left to be wrong about. Same shape as #61's fix for #43: the answer stops being inferred and
> has to identify itself. It also disposes of the second P1 (a reviewer that pushed and then crashed
> still reads as `verified`) with no separate mechanism.
>
> Analysis is posted on the PR as a comment; the branch was not touched.
>
> **r3 also ran 3 of 4 seats** (antigravity out of quota, 147h) — #68 live for the fourth time
> today, and the fourth data point that the seat count belongs in the report.

> **#75 leaves one sibling deliberately unpatched: `epic.py:398`.** The triage judge calls
> `subprocess.run(args, …)` with no `cwd=` — the identical defect — while `claude()` two functions
> below it passes one. It was not fixed because `epic.py` is *all* #62 is, and #62's round 3 was
> mid-flight. It is a one-line follow-up the moment #62 lands, and it should not be lost.

**#64's review is the strongest evidence yet for #68**, and it is about the *review*, not the reviewer. It was a **panel of one**; its own master wrote that nine self-declared coverage gaps "stand unchallenged and unread". Three of its six P2s were conditional worries about code the reviewer could not see, and the code answers all three: `package.nix`'s installPhase does `install -m 0755 bin/*` (it globs, so the new script *is* installed); `CLAUDE_CODE_SESSION_ID` *is* the exported variable; and `sed -n '4,34p'` already ends on the last `--help` line, so the suggested `4,40p` would print shell code into the help — **the proposed fix was the bug**. Check a lone reviewer's conditionals before acting on them.

**#62's merge introduced a bug that nothing conflicted on.** `stderr_gist` ended up defined **twice** in `harness_rules.py` — the branch moved it there, main already had a copy, git kept both, and the second silently won, reverting #29's settled-cause fix. See the convention below.

**Merged today:** #34 (v2.15), #49 (v2.16), #50 (worktree base), #29 (v2.17, closes #19),
**#61 (v2.18, closes #43)**, **#32 (v2.19, closes #15 → #72)**, **#71 (CI, closes #70)**,
**#73 (#62's settled half)**, **#64 (v2.20, closes #45 → #74)**.

> **#62 wants a human before it lands, and that is a judgement about this PR rather than the rule.**
> Five PRs merged today on the standing cap rule and it held for all of them. But #62's remaining
> 210 lines are the one place where the fix has been wrong on the first attempt *twice*: round 2's
> two P1s were both against the round-1 merge gate, and both were the same error — reading "did the
> reviewer push?" as a proxy for "did the review happen?". A third round is worth having, and so is
> a second opinion, before a gate that decides what gets auto-merged is itself auto-merged.

**#32's integration, since the next person inherits its decisions** (`cefa4cb`, both suites green —
279 app, 338 harness): #29 and #32 changed the same three regions of `panel.py` from opposite ends.
#29's `run_cli` rewrite (`cli_outcome` / `is_deterministic_failure` / `BLANK_RETRY_MAX_S`) was kept
whole and #32's two additions grafted back onto it — the callable `args` and the `on_output` hook,
with `on_output` firing *before* the outcome check so a failed attempt's tokens still count. One
test failure fell out and was real signal rather than merge noise: #29 pins the local empty-reply
guard's message, #32 worded it differently, and #29 owns that guard.

**THE LEDGER AS OF 2026-08-16 ~11:45.** `main` is at **v2.28**. Merged today: **v2.26** (#115),
**v2.27** (#117), **v2.28** (#90). In flight: **v2.29** = #128 (`opal-kelp`, #98) and **v2.22** =
#87, which is out of sequence on purpose — written before v2.23 and landing after it, so its
CHANGELOG entry sits below v2.23 rather than on top. **v2.30 is the first free number.**

**Eight collisions now, and the eighth proves the announcement is not the fix.** #123 claims v2.28
and `main` took v2.28 in the same minute #123 was opened. #90 was renumbered **three times** —
v2.23 → v2.25 → v2.28 — without one line of its behaviour changing. **#122 is the issue that
followed from this**, and its argument is that the number is only half the problem: every release
rewrites the same lines of `CHANGELOG.md` and `README.md`, so two branches conflict whether or not
their numbers are unique, and the README's "Latest release / Before it / Before that" narrative is
*rewritten* each release rather than appended to. The recommendation there is to delete that
narrative (it duplicates the CHANGELOG) and to **stamp the number at MERGE rather than at write** —
a placeholder cannot be got wrong, whereas a claim only helps if everybody claims.

**Earlier, for the record — release numbers in flight when four branches had claimed v2.18:**
**#117 = v2.27** (`bramble-otter`, #79, harness-side — claimed on board 2650 before the code was
written, after reading 2635 for v2.26; open, CI green).
**#115 = v2.26** (`rowan-timber`, claimed on the board BEFORE any code was written — board 2635 —
which is the practice the six collisions below argue for and the only one available until #99's
allocator exists; it also claims schema **0017**, which plan item 6b had listed as spoken-for and
which nothing on any remote branch was in fact writing). It moves the board, so `pyproject.toml` and
`app/main.py` go to **2.26.0**.
`main` = **v2.23** (#102 merged, `8534763` — served version now 2.23.0, pending redeploy). #87 = **v2.22** (harness-side). **#88 = v2.23** (claimed ~21:40 by
`zeus/silver-cedar`) — the first release since v2.19 to move the board, so `pyproject.toml` and
`app/main.py` go to **2.23.0** and it carries schema **0016**. **#89 = v2.24** (`marten-tidal`),
**#90 = v2.25** (`cobalt-glacier`, moved down from v2.23 once #88's earlier claim was visible).
**Six collisions today, and the last three are the sharp evidence for #46's allocator half:**
three agents each read `main`, each took "the next free number", and each was correct at the
moment they looked — announcing on the board did not prevent it, because two of the announcements
were one second apart. A claim that is not atomic is not a claim. **The mechanism now exists as a
filed issue: #99's resource-keyed lease, where `kind=release, key=<repo>` is the same row as the
merge claim.** #46 should not build its own allocator — that is one atomic-claim implementation too
many, and this repo has now demonstrated twice that a non-atomic claim reads as a claim right up
until it collides. Historically: #64 = v2.20. Historically: #61 =
v2.18, #32 = **v2.19**, #64 = **v2.20**. #62 claims **none** — it adds no release
heading and inherits whatever it merges into. #64 skips v2.19 deliberately: #32 already holds it and
has it pushed, and renumbering that branch a *third* time to close a cosmetic gap costs more than
the gap does.

**The release number collided twice, both on #32 — #46 is now urgent.** #32 claimed v2.17; #29 took
it. Renumbered to v2.18; #61 took that. It is now **v2.19**, and it is the first release since
v2.15 to actually move the board (schema 0015), so `pyproject.toml` and `app/main.py` go to
2.19.0. Note for anyone auditing this: v2.16, v2.17 and v2.18 all leaving the served version at
2.15.0 is *deliberate and documented* — they were harness-side. It looks like three missed bumps
and is none.

**A trap that caught me twice: `git add -A` in a worktree stages `uv.lock`.** `uv run` regenerates
it, so it shows up untracked in every tree. It reached `main` in `99ba651` before being untracked
and gitignored in `4657345`. It is ignored now, so this specific trap is closed — but the habit it
came from is not.

## The organising principle

**The epic (#52) makes reviews unattended, and unattended amplifies every silent-failure bug.**
Today a human sees "LLM reviewers ran: none" and reacts. A watcher records it and moves on, and
the leaderboard fills with fiction nobody notices. So everything that lets a failure record as a
success lands *before* anything starts running on its own.

## Phase 0 — close what is already done ✅ done 2026-08-15

| # | item | status | who |
|---|---|---|---|
| #35 | diff budget / argv — confirmed fixed by #38 + #49 + `d34045b`. Closed | done | |
| #26 | accounts before the judge — confirmed fixed by #33 (`ae0c69e`). Closed | done | |
| #1 | "Git staging" — premise dead since worktree tooling shipped. Closed, not planned | done | |

Two residues, deliberately not folded back into the closed issues:

- **#65 filed** — the general schema-drift check #26 asked for and #33 did not land.
- **Stale docstring, `panel.py` `_judge_listing`** — still explains its budget as the 128 KiB argv
  cap; the real reason is now context. Fold into whichever PR touches that function next.

## Phase 1 — silent failures, before anything runs unattended

One disease: **a failure that records as a success.** Invisible today, catastrophic under a watcher.

| # | item | status | who |
|---|---|---|---|
| #19 | exit 0 + empty stdout counts as a completed review | **done** | PR #29 merged — 3 rounds, 16→22→21 findings, 14 deferred to #66 |
| #43 | answer-vs-echo selection manufactures empty reviews | **done** | PR #61 merged (v2.18) — 3 rounds, 13→15→18, 6 P2s fixed, 12 deferred to #69 |
| #31 | headless agents run with no `capture_output` | wip | **split**: plumbing merged (#73); PR #62 is the epic gate alone — r3 said STOP (#67 fired), awaiting a human |
| #68 | **the panel can silently become a panel of one** — filed today | **wip** | PR #75 (v2.21) · cause found and it is NOT the suspected race · CI running |
| #78 | **governance as per-repo settings** — quorum, threshold, independence, escalation | todo | from the retro; the constitution. See the note below |
| #79 | **a cheap premise check** — one question to the panel, no full round | todo | from the retro; #62 spent 3 rounds on 1-minute questions |
| #77 | a finding's outcome stops at the judge — a confirmed finding later refuted is recorded nowhere | todo | the leaderboard currently rewards confident wrong findings |
| #84 | escalate on the **second** occurrence of a premise, evaluated when a fix is proposed | todo | #62 escalated at round 3; the circle was visible at round 2 |
| #85 | gate the loop's appetite — what it may pick up, and how much it may file | todo | I filed nine issues today; that is the risk, demonstrated |
| #86 | do not auto-fix issues with a design, UI or human-judgement element | todo | a plausible answer to an unasked question is worse than none |

> **#68 belongs to this phase even though it was found in Phase 2's review.** codex dropped out of
> #64's panel and not the two launched beside it in the same second, from the same directory, with
> the same `panel.py` — so the seat is not currently a property of the run's configuration. #29's
> fix is why it was visible at all (the skip reported its real reason, loudly). What is still
> silent is that the *panel* degraded: #64's report presents 23 confirmed findings from one
> reviewer identically to 23 from a full panel, and a lone reviewer cannot produce `⋆consensus`
> at all — so "no finding earned consensus" and "there was nobody to agree with" render the same.

> These all touch `panel.py`, which is also where the epic lands. **Serialise them** — do not fan out.
> #29 and #61 turned out disjoint and merged clean, but that was luck, not design.

> **#78 and #79 came out of the dogfooding retro, and they belong BEFORE the epic, not after it.**
> #52 makes reviews unattended. An unattended process needs a constitution, and right now every rule
> that governs a merge is either hard-coded in Python or written in prose in *this file* — which is
> untracked. Turning the watcher on first industrialises a loop whose decision rules nobody can see
> or change.
>
> - **#78 — governance as per-repo settings.** `.harness-rules` configures the *electorate* (who
>   sits, which model, what it costs) and nothing about what makes a *decision* valid. The
>   primitives, each independently switchable: **quorum** (how many seats LOOKED), **threshold** (how
>   many AGREED), **independence** (adjudicator ≠ party), segregation of duties, materiality,
>   reserved matters, audit. The sharpest single finding of the day is in there: `judge_model: opus`
>   and `reviewers.claude.model: opus` are both defaults, so **the judge is the same model as a
>   seat** — and all four findings I refuted today were raised by claude and confirmed by an opus
>   judge. One line of config fixes it.
> - **#79 — a cheap premise check.** `panel.py --ask "<premise>" --context <paths>`: one question to
>   the seats, no diff, no judge, the vote is the output. #62 spent three rounds on three proxies for
>   "did a review happen?", each a yes/no question about `panel.py`'s skip branch answerable in a
>   minute. The panel already votes on fixes — a round IS that vote — so the gap is granularity and
>   latency, not absence. A point of order, not a second reading.

## Phase 2 — cheap enablers

| # | item | status | who |
|---|---|---|---|
| #47 | ruff on `harness/` — ~1h, pays on every PR after | todo | best done once the `panel.py` PRs land |
| #45 | worktree ownership advisory — **the board did NOT have the data**; a lease's `cwd` is the launch dir, so it needed the session markers too | wip | PR #64 · round 1 running |
| #15 | per-reviewer token capture — **on the critical path twice**: #55's ceilings and #57's cost column | wip | PR #32 · MERGEABLE (`fa65a91`, v2.19); **its 22 findings are the only thing left** |

## Phase 3 — the epic (#52), in its stated order

| # | item | status | who |
|---|---|---|---|
| #55 | caps: max rounds + budget ceiling — **first**; fold #42 in here | todo | |
| #53 | review worker, off the API process | todo | |
| #54 | watcher: notice new PRs, queue a panel | todo | |
| #63 | issue watcher: `/investigate` or `/fix-issue`, and refuse when a decision is owed | todo | |
| #56 | settings without a second source of truth | todo | |
| #57 | dashboard: cost + what it found; fold #44 in here | todo | |
| #58 | CI as a panel seat | todo | |
| #59 | the local path stays first-class — **a constraint on every row above**, not a final task | todo | |

## Phase 4 — the bets, after the watcher is stable

| # | item | status | who |
|---|---|---|---|
| #41 | review the increment, not the whole PR | **wip** | PR #90 (v2.25), CI green — `cobalt-glacier`. Rebase onto #89 for `head_sha` when that lands. The #41/#48 interaction is **decided** — see the PR board note |
| #48 | provenance: introduced-by-the-fix vs missed — **exact** if #41 lands first | **MERGED** | PR #89 (v2.24) — the *signal* half. Board persistence was deliberately excluded and became **#93** |
| #93 | the four fields #89 added were POSTed and silently dropped on ingest (`extra="ignore"`) — including the per-finding one, which nothing could reconstruct later | **wip** | PR #115 (v2.26, schema 0017), CI green — the *persistence* half, plus #48's leaderboard axis. Pairs with #77 |
| #67 | detect fix rounds *circling* one wrong assumption — a better stop signal than the cap | todo | the brief half is free today |
| #40 | tool-less reviewers report harness defects | todo | |
| #39 | harness bugs reported from other repos | todo | |
| #46 | release numbers as an allocated resource — four collisions on 2026-08-15, **plus #29 vs #32 both claiming v2.17** | todo | **do with #99** — the allocator is `kind=release` on the same resource lease; the smaller half landed as PR #76 |
| #96 | the pre-land gate is prose in `/fix-and-land` §4 and absent from `/panel-review-pr` §7 — extract `harness/loops/preland.py`, one mechanical READY/RECONCILE/HOLD | todo | **build it like #62's settled fix**: gate on statements, not proxies. `gh pr merge` is server-side, so a `pre-push` hook never fires on any agent path |
| #97 | quarterback's own `scripts/migration_reconcile.py` — two branches will both write `0016`/`0017` | todo | **no dependencies, do it today**; two heads *and* a filename collision, which lexray's hash-named revisions do not have |
| #98 | a panel round records no base, so nothing can tell whether its empty To-fix list is still true | **PR #128** | **stage 1, not here** — see the queue. v2.29, schema 0018, CI green. Two fields (`merge_base` + `base_sha`), because the `baseRefOid` the issue names is the merge base and cannot detect base movement — the issue body is superseded by its own comment thread |
| #99 | landing is unserialised — an advisory merge claim on the board | todo | lease decided **resource-keyed**; #46 takes a second `kind` off it. Advisory, never a lock |
| #100 | `/panel-review-pr` §7 should end in a verdict-gated land offer; bare `/panel` reports readiness and does not offer | todo | **before #54**, or the watcher automates a review that ends in nothing |
| #80 | **integration is quadratic in open PRs** — the ceiling on any backlog drain | todo | 11 integration merges to land 6 PRs today |
| #81 | turn on `rerere` — the same conflict is resolved once per worktree | todo | one line; `rr-cache` is already shared across worktrees |
| #82 | send the PR's changed-file list with the review payload | **MERGED** | `8534763` (v2.23, schema 0016). Query half split to #101 after r1+r2 found one premise twice |
| #83 | rebase registered worktrees on `published`, skipping any a live agent holds | todo | safe only since #64 landed the holder check |
| #28 | finish the harness migration (nix-fleet → flake) | todo | |
| #51 | architecture: state server-side — **re-read and narrow or close** | todo | |
| #60 | a shared plan as a board primitive — this file, but claimable. `epic.py` is prior art | todo | |
| #65 | schema-drift check: no field can join one side of the panel↔board contract alone | todo | **#93 is the live instance, four fields at once, nothing failed** — and #115 leaves it a hook: an unrecognised provenance bucket comes back in the 201 as `provenance_unknown`. A detector with no consumer; `qb record-review` prints only the run id |
| #66 | 14 P3/P4 findings deferred from PR #29 round 3 | todo | |
| #68 | the panel can silently become a panel of one — **listed in Phase 1, repeated here only so the tally is complete** | todo | |
| #69 | 12 P3/P4 findings deferred from PR #61 round 3 | todo | several are the module's docstrings drifting from what it does — #65's class |
| #70 | **nothing runs the tests** — no CI job runs pytest, and a bare run skips `harness/` | todo | **belongs in Phase 1 on merit**; see below |

> **#70 undercuts an assumption the whole plan leans on.** The only workflow builds an image, pushes
> it and triggers the deploy; the Dockerfile runs no tests; and `testpaths = ["tests"]` keeps a bare
> `pytest` off `harness/`. So *nothing mechanical* checks any of it — not the ~370 harness tests, not
> the app suite. The standing rule that "the last fix pass is never itself reviewed" is only
> survivable because a suite is assumed to be the backstop, and #58 ("CI as a panel seat") assumes
> CI has findings to contribute. It has none. Found while working #62, whose reviewer noticed the
> narrower version (its own new tests never run).

---

## Decisions already made — do not re-litigate without new information

- **Reviews run in the quarterback container, not on self-hosted runners.** This repo is public; a
  `pull_request` workflow runs the *fork's* workflow file on the runner. The container only ever
  reads a PR as a diff and never executes it. (#52)
- **No reviewer-originated GitHub issue is ever filed unattended.** Not a default — a constraint.
  `harness_rules.unattended()` is the predicate. (#40)
- **No diff budget by default.** Truncating when nothing forces it biases toward false positives.
  (v2.16 / #49)
- **A finding that invalidates a fix's premise should be reported, not patched.** Four times on
  2026-08-15 a fixer patched a broken assumption and the next round found it again; deleting the
  assumption ended the sequence. (#67) — **Confirmed live on #61.** Deleting the ranking premise
  produced a round that named none of it. This is the rule's first test and it passed.
- **At the cap, fix less, not more.** The last fix pass is never itself reviewed, so past a point
  fixing ships *more* unreviewed code. Correctness only; defer the polish. (#66)
- **Auto-*fix* is out of scope for the epic.** Watch-and-review only. (#52)
- **Approved authors are a per-repo, board-side setting, defaulting closed.** Only `OWNER` /
  `COLLABORATOR` may trigger an agent; everyone else is held and visible. (#63, #56)
- **Fleet policy on the board, repo policy in `.harness-rules`**, read from the default branch so a
  PR cannot rewrite the rules governing its own review. (#56)

## Open questions nobody has answered yet

- What is spend measured in before #15 lands — reviews/day, or wall-clock? (#55)
- Does the board hand out work, or only remember it? (#51, #53)
- ~~Is a claim a lock or an advisory?~~ **Answered for merges by #99: advisory, and it must never be
  described otherwise.** The board cannot gate github.com — a human in the UI or an unenrolled agent
  lands regardless — so the claim reduces collisions between *our* agents, which is the observed
  failure mode, and nothing more. The correctness backstop stays the pre-land verdict re-checked
  after base movement, plus CI on `main`. Still open for a *cycle* (#51, #53). Note this is now the
  third time a claim primitive has been asked for (#45 worktrees, #60 the plan, #99 merges) — worth
  asking whether one mechanism answers all three before the third one is built.
- What does a local run do about a cap it cannot verify? (#59 forces #55 to decide)

## How #32, #62 and #64 actually land

**They will not converge, and waiting for that is the mistake.** Five PRs, thirteen rounds, and no
round has ever found fewer than the one before it except once. The cap has always done the work —
that is already recorded below as the evidence for #41/#48/#67. So "land it when the findings stop"
is not a plan; it describes something that has never happened.

**The rule that has already landed two PRs today** (#29 → deferred to #66; #61 → deferred to #69):

> Run to the cap. At the cap, **fix P1/P2 correctness only**, and merge. The last fix pass is never
> itself reviewed, so past that point fixing ships *more* unreviewed code than it repairs.

> **STOP FILING DEFERRED-FINDINGS ISSUES** *(2026-08-16, human's call, `nimbus-sorrel`)*. The rule
> above used to end "file everything else as a deferred-findings issue". Do not. **Every finding is
> already on the board**, with severity, file, line, title, detail, reason, `finding_key`,
> `provenance` and each reviewer's verbatim report — recorded automatically by `record_run` when the
> round posts. A deferred-findings issue is a hand-typed, lossy copy of rows that already exist.
>
> The cost is measured, not asserted: **thirteen such issues now hold ~264 findings and the drain
> rate is zero** — not one has been worked. Today alone added about sixty. They are 19% of the open
> issue list, so they also make "what should I pick up" harder to answer, which is #135's problem
> being fed by the thing that generates it.
>
> **Instead**: say in the PR comment that the round's remaining findings are on the board, and give
> the query — `GET /review/findings?repo=<nameWithOwner>&pr=<n>`, filter to the severities you left.
> Verified live: PR #131's round returns all 41 with `{P1: 8, P2: 12, P3: 10, P4: 11}`, which is
> exactly what #140 transcribed by hand.
>
> **Do not put them in a repo JSON file either.** That is a third copy (board → issue → file) and
> copies drift — this file asserting an endpoint that does not exist, and "four deferred backlogs"
> when there are thirteen, are both today's evidence for what a second source of truth costs. If a
> local-first copy is wanted it is an EXPORT, regenerated and never hand-edited.
>
> **Two things this needs before the existing thirteen can be closed**, and they are why the issues
> are not being deleted yet: a terminal outcome per finding (**#77**, claimed) so "deferred" is a
> state rather than a markdown bullet; and a cross-PR query, since `/review/findings` requires both
> `repo` **and** `pr`, so "every open P3/P4 in this repo" cannot be asked today.

**Read every round-2 number against its seat count before believing it got worse:**

| | r1 | r2 | what changed |
|---|---|---|---|
| #32 | 22 (2 seats) | 43 (3 seats) | +1 seat |
| #62 | 26 (2 seats) | 37 (3 seats) | +1 seat |
| #64 | 23 (**1 seat**) | 44 (3 seats) | +2 seats |

Both jumps came with an extra reviewer. Neither is evidence the fix pass made things worse, and
treating it as such is how a PR never lands.

**Per PR:**

- **#32 — MERGED** (v2.19, closes #15). Round 3 came back with **one P2** and 19 P4s: the first
  round in the dataset where severity collapsed, which is what a settled PR looks like. 28 findings
  deferred to **#72**. It was also the first PR gated by #71's CI, and passed.
- **#64** — round 2 is in: 44 findings from 3 seats (round 1 was 23 from ONE). Two P1s and they are
  real: the held-worktree guard only diverts the DIRECTORY into `HELD_DIRS`, while the database,
  port, nginx and container sweeps each derive orphan-ness independently — so a held worktree can
  still have `DROP DATABASE` run on it and `docker rm -f` run on its containers, contradicting the
  PR's own CHANGELOG. And `here()` compares a lease `cwd` to the worktree root by string equality,
  so an agent launched from a SUBDIRECTORY is invisible. Fix both, then cap-and-land.
- **#62 was split, and the settled half landed as #73.** Round 2 put 9 of its 18 P1/P2s in
  `epic.py`, all in the merge-gate area, and both P1s were against the round-1 fix of that gate.
  `harness_rules.py` + `lander.py` + tests went to #73 and merged green; #62 is now 210 lines of
  epic gate and keeps issue #31. **The mechanic, since it will be wanted again:** branch off main,
  `git show <branch>:<path>` each settled file into place (do NOT take a file the other branch has
  since diverged on — `panel.py` came back stale from #32's merge and broke 44 tests), then merge
  main back into the original branch and watch the landed half collapse to a no-op.

**Landed first, deliberately: #71** (closes #70) — CI now runs both suites on every push and PR.
Every one of these three gets a real test gate on its next push, which none of them had while being
reviewed. Do this before the merges, not after.

## The convergence data so far

Two PRs, three rounds each, one pattern: **no round has ever re-raised a finding, and no round has
found fewer.** Every fix lands; every round finds defects in the previous round's fix commit.

| | r1 | r2 | r3 |
|---|---|---|---|
| PR #29 | 16 | 22 | 21 |
| PR #34 | 33 | 17 | 25 → 22 (after integration) |
| PR #61 | 13 | 15 | 18 → merged after fixing 6 P2s |
| PR #32 | 22 (2 seats) | 43 (3 seats) | round 3 running |
| PR #62 | 26 (2 seats) | 37 (3 seats) | 28 (**3 of 4 seats** — antigravity out of quota) |
| PR #75 | 16 (3 of 4) — 1 P1, 5 P2 | 26 (3 of 4) — **0 P1, 3 P2**, P4s 5→14 | — |
| PR #76 | 19 (3 of 4) — 0 P1, 5 P2 | 23 (3 of 4) — **0 P1, 4 P2** | — |
| PR #88 | 36 (**4 of 4**) — 1 P1, 12 P2 | 38 (**4 of 4**) — **2 P1**, 7 P2 | split; datum merged `8534763` |

> **#75 and #76 are the first rows in this table where the seat count did not move between
> rounds**, and that matters more than either number. Every earlier jump was confounded — #32 went
> 2→3 seats, #62 2→3, #64 1→3 — so "round 2 found more" has never been separable from "round 2 had
> more reviewers". Here it is: same 3 of 4 seats both rounds, counts up, **severity down and no P1s
> in either**. That is the shape #32's round 3 had and this file called "what a settled PR looks
> like", now observed without the confound.
>
> The finding most worth carrying forward: **#75's round 2 caught its own round-1 fix repeating
> #62's mistake.** Fixing the consensus-population bug, the fix keyed on the sonar gate's *status*
> as a proxy for "sonarqube filed something judgeable" — a status standing in for the thing itself,
> written one round after #62's three-round circle was written up in this very file. The pattern is
> not something other people do.
| PR #64 | 23 (**1 seat**) | running | — |

**#32's r1→r2 is not a like-for-like comparison, and that is a warning about this whole table.**
Round 1 ran two seats; round 2 ran **three** (claude, codex *and* pi — antigravity skipped on a
quota reset). 22 → 43 partly measures the extra seat, not the fix pass. Nothing in the report
normalises for seat count, and nothing in this table did either until now — so **record the seats
with the count**, or the convergence evidence for #41/#48/#67 is measuring panel size as if it were
code quality. This is #68 from the other direction: there the missing seat hid findings, here the
extra seat manufactures apparent regression.

"Loop until dry" therefore never terminates on merit — the cap does all the work. This is the
evidence for **#41** (review the increment, not the whole growing PR), **#48** (introduced vs
missed — opposite remedies) and **#67** (circling vs progressing).

**#88 is the strongest #67 evidence yet, and it breaks the table's own assumption.** Two full 4-of-4
rounds — the first in the dataset where *no seat moved at all*, so it is cleaner than #75/#76. Read
by count it converges: P2s 12 → 7. It did not. **Both r2 P1s were regressions introduced by r1's own
fix commit**, and both were the same shape as the r1 finding they were fixing (a filter composed in
front of a selection). Two lessons, both recorded on #67:

- **A circle can be visible INSIDE one round.** r1's seven P1/P2s touched two files and produced one
  failure — the answer reads safer than the evidence supports. Clustered by file they look like seven
  defects; clustered by *the failure they produce* they are one premise. Every earlier signal in this
  table needed two rounds.
- **The detector is SHAPE, not count or severity.** r1's F06 and r2's F01 differ in count, severity,
  file and round, and share only "a predicate ordered before a pick". Also: this is the first case
  where **the agent circling is the one who fixed the previous round** — I wrote the r1 fix, wrote the
  docstring stating the invariant, and violated it one line over in the same commit. The signal
  cannot be self-reported.

**#67 now has its first data point, and it is a positive one.** The count says nothing — 13 → 15 →
18 is the same monotone climb as every other PR. What separated round 3 was the *class* of what
came back: rounds 1–2 named the same mechanism (`_richest`, `_rank`, the scoring premise) and
round 3 named none of it. So "circling vs progressing" is legible in the findings' subject matter
while being invisible in their count — which is an argument that #67's signal has to be built on
what a finding is about, not on how many there are.
