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
  `prune-worktrees`)
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
comments; `/panel-review-pr` takes the confirmed findings and has a sub-agent fix every one
of them.

This is the piece with the tightest board coupling, and the reason the two halves ship
together. A panel run is a controlled comparison — one diff, several models, one judge —
and it used to evaporate when the process exited. Each run now records itself
(`qb record-review`), so `GET /review/stats` and the board's `/panel` page can answer
"which reviewer actually finds the real issues, and is the expensive tier worth it" from
accumulated evidence instead of impression.

The recording is **best-effort by construction**: a board that is down, or absent entirely,
prints one line and changes nothing about the review. Telemetry that can fail a review which
already succeeded is worse than no telemetry.

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
- **Session marker.** `/fix-issue` writes the worktree path to
  `~/.cache/claude-code/session-cwd/$CLAUDE_CODE_SESSION_ID`. This exists because the shell
  cwd resets between tool calls, so a plain `cd` does not stick; the marker is how the
  statusline and `/drop-worktree` know which worktree a session owns.

## Configuration

Drop a `.worktree.json` in your repo root. Every key is optional — Docker, nginx and the
database are auto-detected, and anything absent is skipped, so a plain library repo gets a
worktree and symlinks and nothing else. See `worktree.example.json`, which is filled in with
quarterback's own values.

Keys the script reads: `project`, `framework`, `base_port`, `app_port`,
`docker.{enabled,network_pattern,network_default,image_pattern}`,
`database.{engine,container,url_env,user_env,password_env,name_env}`,
`worker.{type,command,container_prefix,queue_env,queue_default}`,
`nginx.{config,container,main_port,resolver,extra_proxy_headers}`,
`server.{workers_env,workers_default}`, `env.copy_from`, `workspace.{enabled,editor_cli}`,
and the arrays `symlinks`, `copies`, `reserved_names`, `gitignore_additions`.

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
  worktrees. Ask before you dive in.
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
third-party). `gh` for anything that talks to GitHub, which is most of it. `docker` and a
database client only if your repo uses them. The reviewer CLIs the panel drives (`claude`,
`codex`, …) are needed only for the reviewers you actually enable — a missing one is
reported as skipped, not fatal.

### Connecting it to a board (optional)

The panel looks for a `qb` CLI to record runs. With none on `PATH`, it no-ops silently and
everything else works unchanged. Point `qb` at your board to light up `GET /review/stats`
and the board's `/panel` page.

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
