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
  stage a session is in for the statusline
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
the same hunks get the same answer even when the right answer has changed — a CHANGELOG
version narrative being the obvious case, since the correct resolution there depends on
which releases happen to be in flight. `rerere.autoUpdate` is therefore pinned to
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

### Two prerequisites for database isolation

Both are easy to miss, and missing either gets you a worktree that *looks* isolated while
running against shared data.

**1. The main checkout needs a `.env`.** It is the file `create-worktree` copies into the
worktree and then rewrites the database name in. There is nothing else for it to derive
credentials from, so with no `.env` the DB step has nothing to copy and says so —
`cp .env.example .env` is part of setting a repo up, not an optional nicety. (A repo that
keeps its env elsewhere can point `env.copy_from` at that file instead.)

**2. Your test suite must honour that `.env`.** This is the one that bites hardest, because
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
on). `docker` and a database client only if your repo uses them. The reviewer CLIs the panel drives (`claude`,
`codex`, …) are needed only for the reviewers you actually enable — a missing one is
reported as skipped, not fatal.

### Connecting it to a board (optional)

The panel looks for a `qb` CLI to record runs. With none on `PATH`, it no-ops silently and
everything else works unchanged. Point `qb` at your board to light up `GET /review/stats`
and the board's `/panel` page.

`worktree-holder` reads the same per-host site config — `QUARTERBACK_BASE_URL` and
`QUARTERBACK_TOKEN_CMD` from `${XDG_CONFIG_HOME:-~/.config}/quarterback/config`, either
overridable from the environment — but reads it directly rather than through `qb`, so the
occupancy check works whether or not that CLI is installed. There is deliberately **no
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
