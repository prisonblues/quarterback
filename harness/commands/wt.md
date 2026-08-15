# Worktree Management

@description Create or remove a git worktree with Docker, DB, and nginx.
@arguments $ACTION: "create <branch>" or "remove <branch>" (branch name required, action defaults to create)

You are an interactive worktree assistant. Your job is to understand the
user's intent, auto-detect the project setup, ask what options they want,
then call `create-worktree` or `remove-worktree` from `~/.local/bin/`.

## Parse the argument

The `$ACTION` argument may be:
- `create <branch>` or just `<branch>` — create a worktree
- `remove <branch>` or `delete <branch>` — remove a worktree
- empty — ask the user what they want to do

## Step 1: Detect the project

Read the current repository to understand its setup. Check these files
(read them in parallel where possible):
- `.worktree.json` or `.worktree.yml` — if present, this is the
  primary config source. Parse it and use its values.
- `pyproject.toml` or `package.json` — project name, framework, deps
- `docker-compose.yml` — services, networks, images, worker containers
- `.env` — database credentials, redis config
- `nginx/*.conf` — nginx configuration files

From these, determine:
- **Project name** (from config, pyproject.toml name field, or directory name)
- **Framework** (flask, fastapi, node, or unknown)
- **Database** (postgresql or none; container name, credentials)
- **Task queue** (huey, rq, celery, or none)
- **Docker network** (from compose or running containers)
- **Nginx** (config file path, container name)

## Step 2: Ask the user (for create only)

Present what you detected and ask about options using AskUserQuestion.
Batch questions into a single call. Include:

1. **Fork from**: Which branch to fork from? Default HEAD.
   Options: HEAD (default), main/master, or pick from list.

2. **Database mode**: Isolated copy or shared?
   Default isolated. Warn that shared means schema changes affect main.

3. **Worker mode**: Full stack or frontend-only?
   Default full stack. Explain frontend-only skips the worker container.

4. **Extras**: Dry run first? Skip nginx? Skip Docker?
   These are only relevant in unusual cases — include if the setup
   looks complex or the user seems unsure.

If `.worktree.json` exists with clear config, you can skip confirming
detected values and just ask about the branch-specific options (fork,
db mode, worker mode).

## Step 3: Execute

### For create:
Build the `create-worktree` command with the right flags:
```bash
create-worktree [--from <branch>] [--shared-db] [--frontend-only] [--dry-run] [--no-nginx] [--no-docker] <branch-name>
```

Run it and show the output to the user.

After successful creation, **cd into the new worktree directory** so the
rest of the Claude Code session operates from there:
```bash
cd /path/to/project-branch
```
Tell the user their working directory has changed and show them the
access URLs (nginx sub-path and direct port).

### For remove:
Confirm with the user before proceeding (removing is destructive).
Ask if they want to keep the branch or database.
```bash
remove-worktree [--keep-branch] [--keep-db] <create-name>
```

**Ask who is in it first.** A worktree is somebody's working state, and the
board knows whose:
```bash
worktree-holder <path-or-branch>     # exit 3 = another live agent is in there
```
`remove-worktree` runs this itself and refuses (with the holder's name) rather
than tearing down under a live agent. If it does refuse, **do not reach for
`--force`** — tell the user who holds it, and offer to message that agent on the
board instead. `--force` is for the case where the user, having seen the name,
says go ahead. The check is advisory by design: it stays quiet when the board is
unreachable, so a refusal means a genuinely live holder.

Pass the **create-name** — the identifier the worktree was *created* with (see
"Naming model" below). The script resolves the worktree's *current* branch
itself and deletes that, so it still works when the branch was switched or
forked after creation. You can also pass the current branch name; the script
reconciles either form against `git worktree list`.

## Step 4: Offer to save config

If no `.worktree.json` exists and the create succeeded, offer to
generate one from the detected + user-confirmed settings. This makes
future runs faster and more reliable.

Generate the JSON with these fields (omit fields that match defaults):
```json
{
  "project": "<name>",
  "framework": "<flask|fastapi|node>",
  "base_port": <port>,
  "app_port": <port>,
  "docker": { "network_pattern": "...", "network_default": "..." },
  "nginx": { "config": "...", "container": "...", "main_port": ... },
  "database": { "engine": "postgresql", "container": "...", ... },
  "worker": { "type": "...", "container_prefix": "...", ... },
  "symlinks": ["...", "..."],
  "copies": ["...", "..."],
  "reserved_names": ["...", "..."]
}
```

## Naming model (create-name vs. branch)

Both scripts key everything — the directory (`<project>-<create-name>`), the
isolated database, the `.worktree-ports` entry, the nginx block, and the
container names — off a single **create-name**, which is the positional
argument given to `create-worktree`. At creation, create-name == branch (the
directory uses slashes→dashes).

These can **diverge** later: if you `git checkout`/`git switch` or fork a
different branch inside the worktree, the directory/DB/port still carry the
original create-name while the branch changes. So:

- To remove, prefer the **create-name** (the dir suffix after `<project>-`).
  `remove-worktree` resolves and deletes the worktree's *actual* branch
  regardless, and also accepts the current branch name as the argument.
- When a user asks to remove "the X worktree", map X to the directory name, not
  to whatever branch happens to be checked out in it.

## Troubleshooting removal

- **Leftover directory after removal.** If a worktree had a large copied dir
  (e.g. a multi-hundred-MB `apps/models` spaCy copy), an old build of the script
  could leave a half-removed directory behind (the removal timeout was too
  short; fixed to 60s). A leftover dir is de-registered from git and safe to
  `rm -rf` directly — uncommitted work was already tarballed to
  `<project>-<name>-backup-<timestamp>.tar.gz`.
- **Orphaned DB / port entry.** A worktree removed by an older script (or by a
  raw `git worktree remove`) can leave an orphan `<project>_<name>` database and
  a stale `.worktree-ports` line.

To clean any of the above across a whole repo in one pass, run
`prune-worktrees` (dry-run by default; `--prune` to drop orphan DBs + prune port
entries, add `--remove-dirs` to delete leftover directories). Show the user the
dry-run output before applying.

## Never rewrite a worktree you do not own

The scripts guard their own destructive steps, but nothing guards raw git. Before
you run `git rebase`, `git reset --hard`, `git checkout <other-branch>` or
`git worktree remove` **in a directory that is not this session's own worktree**,
run `worktree-holder <dir>` and stop if it exits 3. This has gone wrong for real:
an agent noticed a branch was behind `main`, ran `git rebase origin/main` in the
worktree carrying it, and left the agent three commits into a review cycle there
with conflict markers in four files and no idea why. Nothing about that was
malicious — it simply had no way to know the directory was occupied. Now it has
one, and using it costs a second.

## Important notes

- The scripts are at `~/.local/bin/create-worktree` and
  `~/.local/bin/remove-worktree`. If they're not on PATH, call them
  with the full path.
- Config file is `.worktree.json` (JSON, parsed with jq by the script).
  If the user has `.worktree.yml`, read it yourself and pass values
  via the JSON format or CLI flags.
- Always show the user what command you're about to run before running it.
- If the script fails, read the error output and help troubleshoot.
  Common issues: Docker not running, main containers not up, port conflict.
