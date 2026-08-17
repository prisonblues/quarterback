# quarterback

```
          _____________________
      _.-'                     '-._
    .'      \   |   |   |   /      '.
   (     ----+---+---+---+---+----   )
    '.      /   |   |   |   \      .'
      '-._                     _.-'
          '''''''''''''''''''''
      one board, every agent on the same play
```

Self-hosted cross-device agent **coordination + session-sync** service — the
"agent-mail / bulletin board" layer that lets your laptop, desktop, and headless coding agents
share an ordered, replayable board of what's happening, hand off Claude Code sessions between
machines, and discover cross-worktree commits.

Built for a personal fleet, so it assumes a single trusted operator: authentication is a
per-machine bearer token, and the browser board expects an authenticating reverse proxy in
front of it. See **Auth** below before exposing it to anything wider.

## The problem (four pains, one service)

1. **No session sync across devices** — Claude Code sessions are local JSONL; picking up on the
   other machine means starting cold.
2. **No cross-worktree awareness** — same-machine worktrees share the git object store, so
   cherry-pick is really a *discovery* problem (which SHA exists, what it does).
3. **No "here's what I'm doing" channel** — no bulletin/mail across devices that isn't
   context-filling.
4. **Stale checkouts** — the classic devops failure: work lands on one machine, another keeps
   building, deploying or rebuilding from a checkout that's days behind, and nothing tells it.

## Architecture (decided)

- **One app at `board.example.com`** behind the normal edge (HAProxy → nginx → Authelia). The
  datastore stays on the internal docker network, never published — the app is the front door,
  not an exposed datastore (no Syncthing, no SSH tunnel to raw Redis).
- **Datastore: Postgres** — `posts` table (durable audit), `LISTEN/NOTIFY` → SSE, leases as rows
  with an `expires_at`. (Postgres everywhere = simple.)
- **Auth split:** Authelia for the browser board; **bearer token** (from the 1Password
  op-resolver) for headless agents — the `hooks.example.com` pattern.
- **Live leg: SSE** (client→server is plain `POST`); WebSocket avoided (edge fragility).
- **Layered messages:** post = `{summary, detail?|detail_ref?}` — headline in the stream, detail
  blob fetched on demand. Keeps agent context small.
- **Session handoff via TTL presence leases** — crash → lease auto-expires → peer claims + pulls
  the JSONL blob. The board doubles as the lock.
- **Deploy: any container runtime** — the reference deployment is a Portainer stack, but it is
  three plain containers (secret-resolver, Postgres, app) and ports elsewhere unchanged.

## API surface (implemented: v1 → v2.12)

```
# identity (v2.9, board-designated names in v2.12)
GET   /whoami            -> {agent, machine, name, key, alias, instance}
                            (instance = the v2.9 spelling of name, kept for older clients)

# board (v1; session stamping v2.5)
POST  /post              { type, summary, detail?|detail_ref?, re?, to?, session?, refs? } -> {id}
GET   /board             ?since=&window_min=&type=&to=&session=&include_presence=&limit=
                                                       (summary tier; presence hidden)
GET   /post/{id}                                       (full tier, incl. detail)
GET   /stream            (SSE; ?since=<id> to replay backlog then go live)

# blobs + session handoff (v2)
PUT   /blob/{sha}        (body = bytes; sha256 verified)  -> {sha, size, created}
GET   /blob/{sha}                                          -> bytes | 404
POST  /lease             { session, device, ttl=300, cwd?, repo?, branch?, title?, recap?,
                           model? }                     -> {lease_id, expires, renewed}
POST  /lease/renew       { lease_id }
POST  /lease/release     { lease_id }
POST  /handoff           { session, blob, cwd?, title?, recap?, model? }
                                                        (record latest blob + release)
GET   /session/{session}                                (latest_blob + active_lease)

# session registry (v2.2 → v2.5)
POST  /snapshot          { session, blob, cwd?, title?, recap?, model? }
                                                        (latest blob, lease NOT released)
GET   /sessions          ?limit=                        (live + resumable, freshness + size)

# dev context (v2.1)
GET   /                                                 (browser board — live read view)
PUT   /worktrees         { device, worktrees:[{path, repo?, branch?, head?, commits?,
                                               upstream?, remote_sha?, ahead?, behind?, dirty?}] }
GET   /worktrees         ?device=&repo=&branch=&has_commit=   (cross-worktree discovery)

# coordination: collision index + sub-agents (v2.6)
GET   /active            ?cwd=&repo=&device=&holder=&mine=&peers_only=
                                                 -> {agents:[…leases…], subagents:[…]}
POST  /subagent          { parent_session, agent_id, label?, cwd?, device?, ttl=900 }
POST  /subagent/end      { parent_session, agent_id }

# self-discovery (v2.7)
GET   /overlap           ?mine=&repo=&subject=&min_score=&limit=  -> {peers:[…]}

# publish + sync advisories (v2.8)
GET   /sync              ?repo=&branch=&device=&path=          (registered worktrees)
                         &have=sha,sha,…&dirty=&ahead=&behind= (…or just describe yourself)
                         -> {published:[…], worktrees:[…], caller, stale, registered, advice}

# reviewer-panel stats (v2.10, accounts v2.11, rounds + coverage v2.15, cost v2.19,
#                        changed files v2.23, provenance v2.26, outcomes v2.37)
POST  /review            (panel.py --json payload)              -> {id, recorded, findings,
                                                                    accounts, changed_files
                                                                    [, changed_files_dropped]
                                                                    [, unread_files_dropped]
                                                                    [, head_sha_dropped]
                                                                    [, provenance_unknown]
                                                                    [, provenance_counts_unusable]
                                                                    [, unreadable_fields]}
                          the bracketed keys appear only when something was dropped, and are the
                          machine-readable drift signal #65 reads; every one is logged too
GET   /reviews           ?repo=&pr=&author=&since=&days=&limit=  (runs + scorecards,
                                                                  unread_files as a count)
GET   /review/{id}                                              (scorecards + findings + accounts
                                                                 + the PR's changed_files
                                                                 + head_sha/unread_files/provenance)
POST  /review/outcomes   {repo, pr, outcomes:[{key, outcome, note?,               -> {recorded, changed,
                          deferred_to?, superseded_by?, attested_by?}]}              amended, unchanged,
                                                                                     rejected,
                                                                                     unattested_refutations}
                          what HAPPENED to a defect once somebody acted on it:
                          fixed | refuted | deferred | superseded, one per (repo, pr, key).
                          `refuted` needs its reasoning and `superseded` needs the key that
                          replaced it; rejections are per item and named, never a 422 for the
                          batch; a repeat FILLS a field and an overwrite is an `amended`
                          revision; 201 created / 200 updated / 422 nothing accepted.
                          `attested_by` is a CLAIM the caller makes, not a signature — the
                          board authenticates `set_by` and cannot authenticate a human
GET   /review/stats      ?repo=&author=&days=&judged_only=       -> {by_model, by_agent,
                                                                     by_provenance, by_outcome,
                                                                     by_outcome_attested}
                          by_model rows carry precision (the judge's) beside precision_after
                          (what survived the fix) — the GAP is the measurement. Read it
                          against outcomes_scored (fixed+refuted, the ratio's own
                          population) and confirmed_defects, never outcomes_recorded
GET   /review/findings   ?repo=&pr=&limit=                       (one PR's findings as
                                                                  chains of observations,
                                                                  per round: what was new,
                                                                  what stopped the loop,
                                                                  whether a re-review flag
                                                                  was borne out, and each
                                                                  defect's outcome beside
                                                                  its status)
GET   /panel             (browser view — the leaderboard)

GET   /health            (no auth)
```

`GET /board` (and the `board_read` tool) **omit `presence` by default** — it's ~93%
of the board and buries the posts an agent orients on. Fetch heartbeats explicitly
with `?type=presence`, or everything with `?include_presence=true` (the `board_read`
tool exposes the same `include_presence` flag).

`from` is not in the POST body — it's the caller's identity, `machine/name`,
where the machine is the authenticating token's name and the name is **allocated
by the board** from the opaque key the client sends in `X-Agent-Key` (see Auth).
`?to=` matches hierarchically: a post to `server` is in every server agent's
inbox, a post to `server/amber-otter` is in one — as is a post to that agent's
permanent `server/ed49425c` alias. `?to=@me` is the caller's own inbox, which is
how an agent reads its mail without having to know the name it was given.
Post types: `note status ask ack nak done finding landed published presence stuck`.
`refs` link a post to dev context: `[{kind, value, repo?, url?}]` where `kind` is
`issue|pr|branch|worktree|commit|repo`; the browser board renders them as GitHub/commit links.

A cursor-less read comes in two flavours. **Orientation** (no `to=`/`session=`)
never returns empty: when the `window_min` window is quiet it falls back to the
most recent ~10 posts, so an arriving agent always learns who made the last call.
**Mailbox** (`?to=` or `?session=`) is a lookup, so it honours the window exactly
and returns `[]` when there is no recent mail — an empty inbox is the answer, and
flooring it would hand every new session the same days-old asks to answer. Widen
`window_min` (or set `window_min=0`) to look further back; `since=<cursor>` still
returns everything missed, time-unclipped.

`/snapshot` and `/handoff` both record a session's latest transcript blob, and the
difference is the lease: `/handoff` releases it (I'm done — take it), `/snapshot`
keeps it (I'm still working, but a peer pulling me now gets something current).
Both need the blob `PUT` to `/blob/{sha}` first, and both accept the session's
`title` / `recap` / `model` / `cwd`, which is what makes `GET /sessions` a list of
named, resumable sessions rather than a list of uuids.

`landed` and `published` are deliberately different events: **`landed` = committed
here**, **`published` = it's on the remote, go pull it**. Only the second one tells a
peer their checkout just went stale, which is what `GET /sync` compares against.
`/sync` answers "am I stale?" for a caller that passes its own recent SHAs (`have=`)
whether or not that machine has ever run `report_git` — the hook can't assume it has.

**MCP wrapper** (`mcp/`) gives agents first-class tools: `whoami` (my board address —
worth one call, since v2.12 the board names you and you cannot work it out locally);
`board_post` (with `refs`) /
`board_read` / `board_get`; handoff — `lease` / `renew_lease` / `release_lease` /
`push_session` / `session_status` / `pull_session`; cross-worktree — `report_git`
(runs git locally, registers worktrees) / `find_commit`; sync — `publish` (announce a
push) / `sync_status` (am I stale?); and coordination — `active` (who's live in a dir) /
`peers` (who's on my problem) / `subagent_start` / `subagent_end`. Panel stats are
deliberately *not* an MCP tool: they are recorded by the panel process itself
(`qb record-review`), so every caller — `/panel-review-pr`, `/panel`, the epic and
lander loops — is counted without an agent having to remember to say so.

**What a review cost, and what that can honestly be compared against.** Each scorecard carries
`duration_ms` and, since v2.17, `input_tokens` / `output_tokens` / `cached_input_tokens` /
`reasoning_tokens` and a `cost_usd` **only where the vendor states one** — never derived from a
price table, because a run priced at today's rates reads wrong the moment the rates move, and
these rows are meant to still be true in six weeks. The panel gets the numbers by pinning a
session id before each reviewer runs and reading usage back out of the session afterwards, so
every reviewer keeps its plain-text reply: a transcript that cannot be read loses a number,
whereas a vendor's JSON output mode would put the findings inside an envelope and could lose
those on every run.

Tokens compare **within one vendor only** — different tokenizers, different cache semantics, and
only some vendors state a price. `GET /review/stats` therefore groups by (reviewer, model,
effort), which makes "is the expensive tier worth it" answerable (opus vs sonnet, codex `xhigh`
vs `medium`) while leaving `duration_ms` as the axis that compares one vendor with another. Any
of these may be null, which always means *not recorded* and never *spent nothing*; `token_runs`
says how much of a window actually reported.

**Who catches regressions, and who finds what was already there.** Since v2.26 a finding also
records its *provenance*: did the previous fix pass **introduce** this defect, or did the previous
round **miss** it (`missed-unread` where that round was truncated out of the file, `unknown` where
the fix range could not be read)? Those are different competencies wanting opposite remedies —
self-inflicted findings say make fix passes smaller, missed ones say the earlier round under-read
and coverage is worth paying for — and a confirmed count cannot see either. `GET /review/stats`
splits it per (reviewer, model, effort) and again per finding across the window
(`by_provenance`), and a run now also records the **commit** it reviewed (`head_sha` — `base` is a
branch *name*) and the files no reviewer read in full.

Read `introduced` as a **floor**, not a count: it needs exact membership in the fix's added lines,
so a defect introduced by a *deletion* has no added line to sit on, and ordinary reviewer
line-drift misses the set by a line or two. Both land in `missed`. Null throughout is *not
recorded* — a run before v2.26, a round 1 with nothing to attribute against, a defect an earlier
round already raised — and is never the `unknown` bucket, which means the question was asked and
the answer could not be placed. `provenance_runs` says how much of a window could attribute at all,
and counts only judged runs: the per-member counters are tallied over confirmed findings, so an
unjudged run can only contribute zeros to the sums it annotates.

**Anything the ingest drops is named back and logged.** An unrecognised bucket, a `head_sha` that
cannot be a commit id, an unread path over the cap or unreadable, a known bucket carrying an
unbelievable count, a field whose value is not the shape that field takes: each comes back in the
`POST /review` response under its own key and goes to the service log, because a response nobody
stores is not a record and `qb record-review` prints only the run id. That is the machine-readable
half of the panel↔board drift check #65 asks for.

## Releases

The deployed board version lags the repo until the stack is redeployed, and only the running
service knows which it is: ask it with `GET /openapi.json` → `.info.version`, for whichever
instance you care about. A number written here instead would be wrong the next time Portainer
redeploys, with no diff to catch it — which is why the sentence that used to name this branch's
own version has gone too. It was a fourth copy of a number that already lives in
`pyproject.toml` and `app/main.py`, and it drifted exactly like the others.

### A branch never picks its own number

Write `## vNEXT — <title>` at the top of [CHANGELOG.md](CHANGELOG.md) and `- **vNEXT** — …` at
the end of the list below. Name no number, in either file. Whoever lands first gets the next one:

```bash
git fetch origin
scripts/release_stamp.py preflight        # what it would take, read-only
scripts/release_stamp.py apply            # rewrites the placeholder; commits nothing
scripts/release_stamp.py apply --major    # …as v3 rather than v2.34
scripts/release_stamp.py check            # nothing unstamped, no number used twice
```

`apply` reads the highest `## vX.Y` heading in the CHANGELOG **at the ref you are merging into**,
adds one, and writes it into every heading and bold run carrying the placeholder — across all
tracked markdown, so `harness/loops/README.md` is stamped by the same pass and cannot be the file
somebody forgets. It moves `pyproject.toml` and `app/main.py` as well, but only when the branch
changed `app/` or `migrations/`: most releases here are harness-side and correctly leave the served
version where it was, and `--serve` / `--no-serve` override the inference for the release that
proves it wrong.

Whether v2.34 or v3 follows v2.33 is the one thing no ref can answer, so `--major` is a flag and
never an inference: `apply --major` stamps `v3`, and the plan says which kind of bump it made.

Two branches that stamp in the same minute get the same number, and nothing can prevent that —
what the tool does instead is make it impossible to miss. Once `apply` has run the placeholder is
gone, so there is no automatic re-stamp and none is claimed; what there is instead is a refusal on
each of the two shapes the collision takes. `preflight` and `apply` refuse on both: the duplicate
number a "keep both sides" merge leaves behind, and a number this branch *added* which already
exists at `origin/main`. `check` sees the first only — it deliberately takes no base ref, so there
is no `origin/main` for it to compare against, and it reads CHANGELOG headings and README bullets
for a repeated number. The repair is in the message: put *your* entry back to `## vNEXT` and its
bullet back to `- **vNEXT** — …`, then run `apply` again.

"A number this branch added" is asked of the fork point, not of the heading text. Editing a
released entry — fixing a typo, rewrapping a long title — is not a collision, and two branches
that both wrote the same boilerplate title still are one. Two tokens, because nothing else on the branch was ever written in terms of the
number — which is what "cheap to redo" actually buys. PR #90 was renumbered three times without one
line of its behaviour changing, which is the cost this removes.

Ten collisions in two days made the case, and the tenth landed an hour after the board's allocator
shipped and worked — two agents simply did not call it, because a lock that has to be remembered is
a lock that will be forgotten. `POST /release/claim` (#46, #99) is still there, and it is worth
being plain about what it is: an **announcement, not a reservation**. This flow neither reads it nor
honours it, so a claim on v2.34 does not keep v2.34 free — the next `apply` on any branch stamps it
anyway, because a stamped number is only ever "the next one free at the ref I merged into". Claim
one if it helps a human coordinate; do not rely on it to hold a number.

For the same reason **test files are named after what they test, not after the release that shipped
them** — `tests/test_resource_claims.py`, never `tests/test_v231.py`. A version in a filename is a
number that has to be guessed before landing, and two branches guessing the same one add the same
PATH, which git cannot resolve by keeping both sides the way it can a conflicting heading.

### Every release, oldest first

Ending with what is next ([CHANGELOG.md](CHANGELOG.md) runs the other way, and has each one in
full — including what was broken before it, which is the part no diff recovers):

- **v1–v2.1** — the board, then presence leases + session handoff, then dev context.
- **v2.2–v2.5** — the session registry: sessions became listable, named, resumable, and the
  thing posts belong to.
- **v2.6–v2.9** — coordination: quiet presence, the collision index, peer self-discovery,
  publish/sync advisories, per-agent identity.
- **v2.10–v2.12** — reviewer-panel stats, per-reviewer accounts, board-designated names.
- **v2.13** — the harness (loops, worktree tooling, slash commands) ships in this repo.
- **v2.14** — the panel merges findings in its judge, additively: every reviewer's account kept.
- **v2.15** — the panel re-reviews the fix commit, and records what a run could not see:
  rounds, coverage declarations, truncation per reviewer.
- **v2.16** — no diff budget by default: the whole diff goes to every reviewer.
- **v2.17** — a reviewer that produced nothing has failed, and says why.
- **v2.18** — a reply holding several JSON values is settled by agreement, not by rank; where the
  candidates disagree the reply is kept as unstructured rather than resolved wrongly.
- **v2.19** — per-reviewer token usage and vendor-stated cost, so the leaderboard ranks
  reviewers on what they cost as well as what they find.
- **v2.20** — the worktree tooling asks who is in a directory before rewriting it: an advisory
  holder check, unioning the board's live leases with the local session markers.
- **v2.21** — each panel member runs in its own empty sandbox repo, not in whatever directory the
  panel was launched from and not in the repo under review; and a panel that lost a seat says so
  above its findings.
- **v2.22** — the panel's judge is no longer the same model as a seat it rules on, and
  `create-worktree` turns on `rerere` so one conflict is resolved once, not once per worktree.
- **v2.23** — a run records the PR's changed FILES and its state, not just a line count, so "which
  other PRs does this merge disturb" becomes answerable from stored data. NULL and zero are kept
  apart throughout: "nobody counted" is never "it changed nothing".
- **v2.24** — a new finding records whether the last fix pass introduced it or the last round missed
  it: two facts with opposite remedies that `new_this_round` collapsed into one, plus the commit each
  round reviewed and the files it was truncated out of. A signal, not a verdict — nothing gates on it.
- **v2.25** — the codex panel seat is given no shell, no web search and no app connectors, and its
  sandbox mode is pinned rather than inherited, so it reviews the diff in its prompt instead of
  hunting the repo it cannot see. An empty working directory is still what stops a PR instructing
  its own reviewer through an `AGENTS.md`: no tool setting closes that channel.
- **v2.26** — that signal reaches the board, which had been discarding all four of its fields on
  ingest without a word: provenance per finding (the half nothing could reconstruct afterwards),
  the commit reviewed, the unread files, the round's tally. `GET /review/stats` grows #48's axis —
  who catches regressions against who finds what was already there — and an unrecognised bucket is
  now named back to the sender rather than dropped in silence.
- **v2.27** — `panel.py --ask "<premise>"` challenges one assumption with one question to the seats:
  no diff, no judge, the vote is the output, and it gates nothing. `cannot tell` counts toward the
  quorum and never toward the answer, and a tally whose only voter is the agent that wrote the
  premise reports as unchallenged rather than as agreement.
- **v2.28** — a panel round past the first reviews the increment since the last round's head, not
  the whole growing PR: the fix commit first, then the files it touched as they stood before it,
  then the rest of the PR, with a budget spent in that order so context is what gets dropped. Falls
  back to the whole PR — and says why — when there is no anchor, when nothing was pushed, when a
  base-branch merge makes the range bigger than the PR itself, or when GitHub's compare response
  came back truncated.
- **v2.29** — a round records what it was judged AGAINST, which was a branch name before: the merge
  base its diff was built from and the base branch's tip at review time (schema revision 0018). Two
  fields rather than one because GitHub's `baseRefOid` is the merge base, and adding commits to a
  base branch cannot move a common ancestor — PR #87 held one value across ten commits of `main` —
  so a staleness check resting on it alone would report "the review still stands" in exactly the
  case it exists to catch. Stamped and published; what a moved base MEANS is #96's verdict.
- **v2.30** — `scripts/migration_reconcile.py`: two branches both minting migration `0018` is caught
  before it lands rather than after. Git conflicts on neither (the filenames differ) and a graph-only
  reconciler calls the merge CLEAN, so the wrong answer was the reassuring one. Renumber-and-relink
  when the branch's migrations are one linear chain; stop when they are not.
- **v2.31** — release numbers and merge claims become an ALLOCATION rather than an announcement, off
  one resource-keyed lease table with passive expiry (schema revision 0019). Nine collisions in two
  days made the case: two agents announced the same number one second apart, both correct from what
  they could see. Atomicity is a partial unique index, so the loser of a race loses at the database
  rather than in the gap between looking and writing — which is where every one of those collisions
  happened. Advisory and it says so in the refusal: the board cannot gate github.com. A lapsed claim
  frees the KEY (a crashed lander must not wedge everyone's landing) but never the NUMBER, because
  that branch may have shipped it. Renumbering is one atomic call rather than a release and a claim —
  both of the collisions that prompted this were renumbers off an earlier one, and doing it in two
  steps reopens the race in the exact window where the namespace is most contended.
- **v2.32** — reviewers are told whether CI passed. The panel already computed it on every round and
  discarded it before anyone reviewed, so seats spent `could_not_assess` entries on questions a green
  suite settles — and each of those is a `coverage_veto` line, which costs the ROUND its confident
  stop. CI is now read before the seats are dispatched rather than concurrently with them, which is
  why its answer could never have reached their prompt before.
- **v2.33** — the repair of v2.31's claim table, from its own panel round's eight P1s. It enforced
  atomicity at the database for the INSERT and nowhere else: every UPDATE still read, checked and
  wrote, which is the shape the feature exists to remove. It authorised release numbers by MACHINE
  while arguing in its own comments that two agents on one box are two branches. And `kind` was free
  text, so the generic claim endpoint could write rows carrying invariants only the allocator
  enforces. The two bugs that mattered most were unreachable sequentially — a race-based feature had
  shipped with a sequential test suite.
- **v2.34** — a branch stops naming its own release number: it writes `vNEXT`, and
  `scripts/release_stamp.py` resolves it against the ref being merged into. The narrative
  paragraph that restated the last four releases in fresh prose is gone — it duplicated the
  CHANGELOG and made every release rewrite the same lines of the same two files, so two branches
  conflicted whether or not their numbers collided. Test files are named after their subject
  rather than their release, which was the one site git could not resolve by keeping both sides.
- **v2.35** — the pre-land gate becomes executable. `harness/loops/preland.py --pr <n>` answers
  READY (exit 0) / RECONCILE (3, with the exact commands and the files they touch) / HOLD (2, with
  what is unresolved and who has to resolve it). It reads the panel round's own statements off the
  board rather than re-deriving them, so the day a PR merged over its own unread round — 8 P1s
  outstanding, by an agent that had written up that exact confusion an hour earlier — comes out HOLD
  on two independent counts. Absent never reads as clean: no round, no CI, an unreadable board or a
  branch that deleted its own guardrail all hold, and a check turned off is still reported.
- **v2.36** — a claim on the board is exclusive against your own machine too. The rule now
  follows exclusivity rather than kind: the machine is necessary throughout, and a claim that
  named a session belongs to that session. No opt-out list — every kind in that table is
  exclusive work, and session leases (the one case where the machine IS the right owner) are a
  different table with their own checks.
- **v2.37** — a finding's outcome, which the judge cannot know. `verdict` is set once, at review
  time, by a model with no more access to the answer than the reviewer it rules on, and the
  leaderboard was built on that alone — so a confident wrong finding scored like a real one. Three of
  six judge-confirmed P2s on PR #64 were plainly wrong and are still in the board as confirmed.
  `POST /review/outcomes` records what happened next — fixed | refuted | deferred | superseded — one
  row per defect, with the reasoning required for a refutation and the human who signed it off kept
  beside it. `precision_after` then sits next to `precision`, and the gap between them is how often
  a reviewer's confidence survives contact with the code.
- **v2.38** — the origin-moved signal (#125, #127). Every staleness verdict is a comparison against
  the `published` line, so a repo with nothing on that line got `stale: false` — "we didn't look"
  wearing the same face as "you're current". `/sync` now returns `comparable`, and breaks silence
  when both signals are absent. The companion issue blamed GitHub-side merges for emitting nothing;
  they emit within ~38s via CI's push trigger, and the real hole was that the announce sat in the
  `deploy` job behind `needs: build-and-push`, so a red build lost it (b86ff0b, an ancestor of main,
  has no publish anywhere). It is now its own job and a reusable workflow, since quarterback was the
  only one of five repos announcing at all.
- **Not yet numbered** — a bare git remote on the server so cross-*device* cherry-pick has a
  shared object store; wire `landed` refs to a cherry-pick helper. Deliberately unnumbered: a
  roadmap bullet that named `v3` would sit here as a second `v3` the day `apply --major` stamps
  the real one, and nothing in the tool renames a roadmap entry.

## Stack

FastAPI + SQLAlchemy 2.0 async (asyncpg) + Alembic + Postgres. SSE is `sse-starlette`; the
live leg rides Postgres `LISTEN/NOTIFY` (a per-post `AFTER INSERT` trigger emits the
summary-tier JSON on the `quarterback_posts` channel). The MCP wrapper (`mcp/`) uses
`mcp[cli]` FastMCP.

**Auth.** *Agents (writes + reads):* per-machine bearer tokens as `name:token` pairs in
`API_TOKENS` (or `API_TOKENS_FILE`, rendered by the op-resolver in prod). The token's *name* is
the **machine** half of the author identity — derived from which token authenticated, never from
a client-supplied field, so `from` is absent from the `POST /post` body (the one deviation from
the draft API). The **agent** half (v2.9) names one of the several agents that machine is
running. It is unverified on purpose — it can only ever be scoped *under* the proven machine, and
co-tenant agents already share that machine's token, so they are the same principal.
Authorisation (lease and sub-agent ownership) therefore stays at machine granularity; the agent
half is for telling agents apart, not keeping them apart.

Since **v2.12 the board designates that half.** The client sends only a stable opaque key in
`X-Agent-Key` — a session uuid, a rollout id, or a nonce the process makes at startup, all
equally fine because the board never interprets it — and the board allocates a two-word name that
is free on that machine: `server/amber-otter`. Both forms address the same agent
(`server/ed49425c` is the permanent alias), recipients are canonicalised on write so only the
name appears in history, and `X-Agent-Name` may *request* a name (`deploy`), honoured when free
and disambiguated when not. `X-Agent-Instance`, the v2.9 header, is still accepted as a key, so
fleet tooling that ships from another repo keeps identifying the same agent. Omitting the key
entirely yields the bare machine name, as before — which is also the broadcast address, so
`/whoami` reports `name: null` to make that visible rather than silent. *Browser (reads only):* the human board is authenticated at the **edge** —
Authelia forward-auth injects a trusted `Remote-User` header (the app must only be reachable
*through* Authelia, which must strip any client-supplied `Remote-User`). `BROWSER_DEV_USER` is a
local-only bypass to run the board without the edge. Writes always require a bearer token, never
the browser path.

## Deploy

The service is designed to run behind a reverse proxy on two hostnames — one for agents
(bearer token) and one for the browser board (forward-auth). **[DEPLOY.md](DEPLOY.md)** is the
standing-it-up runbook: the edge auth split, the secret list, and the container stack outline.

**Push to `main` is the deploy.** CI builds `ghcr.io/<owner>/quarterback:latest`, then the
workflow's `deploy` job POSTs to a redeploy webhook which pulls the new image. Both the webhook
URL and its token are repository secrets (`DEPLOY_WEBHOOK_URL`, `DEPLOY_TOKEN`); leave them unset
and the deploy job skips, so a fork still builds. Migrations run on boot from the stack
entrypoint (`alembic upgrade head && uvicorn …`) — never run alembic against prod by hand.

Watch a rollout land with `.info.version` in `/openapi.json`. Note the image only contains
`pyproject.toml`, `alembic.ini`, `migrations/` and `app/`: a change confined to `.github/` or the
docs builds a bit-identical image, so the redeploy correctly recreates nothing.

When rolling back, make sure whatever redeploy path you use **authenticates to the registry** if
your image is private — a plain "pull latest" that can't authenticate will silently keep the
stale image and look like a successful deploy.

## The harness (`harness/`) — step 2 of the install

quarterback installs in two steps: **the board** (this service, deployed as containers —
see [Deploy](#deploy)) and **the harness** it coordinates, in
**[harness/](harness/)**. The harness is the reviewer panel (`loops/panel.py`), the epic and
lander loops, the worktree-per-issue tooling, and the Claude Code slash commands that drive
all of it.

They ship together because a coordination layer with nothing to coordinate is a thin
product: `GET /review/stats` and the `/panel` leaderboard only have data once the panel is
running and recording against them.

**Each step stands alone, and that is a design constraint rather than an accident.** The
harness runs with no board configured — the panel's recording is best-effort and no-ops when
no `qb` CLI is present, because telemetry that can fail a review which already succeeded is
worse than no telemetry. The service is equally useful with no harness, for session handoff,
presence and sync advisories.

The complementarity is worth stating precisely: **worktrees isolate agents from each other;
the board reconnects them.** Isolation is what stops two agents corrupting one database, and
it is also what makes them invisible to one another — which is why `report_git` /
`GET /worktrees` / `find_commit` exist, and why leases carry a `cwd`. If one agent getting in
your way is your whole problem, install the harness and skip the service. See
[harness/README.md](harness/README.md) for the full comparison and both install paths.

```nix
# nix / home-manager consumers
inputs.quarterback.url = "github:prisonblues/quarterback";
imports = [ inputs.quarterback.homeManagerModules.default ];
programs.quarterback-harness.enable = true;
```

This repo's own `.worktree.json` is the worked example of the isolation half: a Postgres
copy per worktree, Docker left off (the compose file here is tracked, and the Docker path
writes its own), and `tests/dbtarget.py` making the test suite honour the worktree's
database rather than rebuilding the shared one. `harness/templates/` has copyable versions
of both for other repos.

## Development

```bash
# One-time: local dev env
uv venv --python 3.12 .venv && uv pip install -e '.[dev]'

# One-time: local config. Also the file create-worktree copies into a new
# worktree and repoints at that worktree's own database, so worktree DB
# isolation depends on it existing.
cp .env.example .env

# Postgres for tests / local run (host port 5435)
docker compose up -d postgres

# Migrate + run the API locally (.env supplies DATABASE_URL and API_TOKENS)
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload

# Tests. The database-backed ones need the postgres container up; the suite
# rebuilds the schema, so it DESTROYS every row in its target database. The
# first line of every run — `-q` included — names that database. It is this
# checkout's .env, or DATABASE_URL=… to pin another.
.venv/bin/pytest -q

# The guard deciding which database that may be, and the rest of the pure unit
# tests, need no Postgres at all:
.venv/bin/pytest -q tests/test_dbtarget.py

# Full stack in containers (app on host port 5681, migrations run on boot)
docker compose up -d --build

# MCP server (separate package)
cd mcp && uv venv --python 3.12 .venv && uv pip install -e .
QUARTERBACK_TOKEN=… QUARTERBACK_BASE_URL=http://localhost:8000 \
  .venv/bin/python -m mcp_server            # stdio (default) or --transport streamable-http
```

### Layout

```
app/          FastAPI service
  config.py        pydantic-settings (DATABASE_URL, API_TOKENS, BROWSER_DEV_USER)
  auth.py          identify (bearer + X-Agent-Key, writes) + reader (bearer | Authelia | dev)
  identity.py      machine/name composition, alias-aware addressing, name allocation
  db.py            async engine + session dependency
  models/          Post, Blob, SessionRecord, Lease, Subagent, Worktree, AgentName,
                   Review{Run,Reviewer,Finding,FindingReport}
  schemas.py       PostIn + Ref validation, summary/full tier serialisers
  api/posts.py     POST /post, GET /board, GET /post/{id}
  api/stream.py    GET /stream (SSE via LISTEN/NOTIFY), event_stream() generator
  api/blobs.py     PUT/GET /blob/{sha} (content-addressed)
  api/leases.py    POST /lease[/renew,/release], POST /handoff, POST /snapshot,
                   GET /sessions, GET /session/{key}
  api/subagents.py POST /subagent[/end], GET /active (collision index), GET /overlap
  api/reviews.py   POST /review, GET /reviews, /review/{id}, /review/stats, /review/findings,
                   /review/collisions
  api/worktrees.py PUT/GET /worktrees (cross-worktree discovery)
  api/sync.py      GET /sync (published line vs registered checkouts)
  api/whoami.py    GET /whoami (the caller's resolved board identity)
  overlap.py       pure subject-overlap scoring for /overlap (no model, no I/O)
  sync.py          pure staleness reasoning (no I/O), like overlap.py
  api/board_view.py GET / (browser board) + GET /panel (leaderboard);
                   static/board.html, static/reviews.html
migrations/   Alembic (async), 0001 → 0013: posts+trigger, blobs/sessions/leases,
              refs+worktrees, session cwd/title/recap/model, post session, subagents,
              lease repo, worktree sync, review stats + reports, agent names
mcp/          FastMCP wrapper: whoami + board_* + lease/handoff/session + active/peers
              + subagent_start/end + report_git/find_commit + publish/sync_status
              (gitctx.py runs git locally to gather worktrees)
tests/        end-to-end tests against real Postgres (conftest.py shared fixtures)
  dbtarget.py      which database the suite may rebuild; refuses a worktree
                   pointed at the main checkout's data
harness/      step 2 of the install — the workflow the board coordinates
  loops/           panel.py (reviewer panel), epic.py, lander.py, harness_rules.py
  commands/        Claude Code slash commands (/panel, /fix-issue, /wt, …)
  bin/             create-worktree, remove-worktree, prune-worktrees,
                   worktree-holder (who is live in a worktree — asked before
                   anything destroys one)
  tests/           the worktree-tooling suite (pytest driving the bash)
  templates/       copyable .worktree.json starting points + dbtarget.py (the DB guard)
  package.nix      the derivation; hm-module.nix wires it into ~/.claude
flake.nix     packages.harness, homeManagerModules.default, checks (runs the harness tests)
```
