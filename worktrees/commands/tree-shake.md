# Tree-shake — sweep worktree debris

@description Clean up the detritus left after running many worktrees: orphan databases, stale port entries, leftover directories, orphan Docker containers, and orphan nginx blocks. Dry-run first, apply on confirm. Optionally tears down finished (merged) live worktrees properly.
@arguments $ARGS: (none) — operates on the current repo

You are a careful cleanup driver. The deterministic logic lives in the
`prune-worktrees` and `remove-worktree` **scripts** — you drive them from this
conversation, show the user what will happen, and only apply destructive steps
after they confirm. Never hand-roll `docker rm` / `dropdb` / `rm -rf` yourself.

Run from inside the repo whose worktrees you want to clean.

## 1. Tear down finished worktrees first (proper cleanup)

Orphans are what's left when a worktree was removed *badly*. Before sweeping
orphans, offer to remove **finished** live worktrees the *right* way (which
cleans their DB, containers, nginx block, port, and dir in one go):

- List live worktrees: `git worktree list`.
- For each linked worktree (not the main checkout), check whether its branch is
  done — e.g. its PR is merged (`gh pr view <branch> --json state,mergedAt` or
  `gh pr list --head <branch> --state merged`) or the branch is fully merged
  into the default branch (`git branch --merged`).
- Show the user the list of worktrees that look **finished** vs **in-progress**.
  Ask (AskUserQuestion) which to tear down. For each approved one, run
  `remove-worktree <create-name>` (the dir suffix after `<project>-`). This is
  the clean path — it handles all trappings and prunes the branch too.
- Leave in-progress worktrees alone.

Skip this phase if the user just wants the orphan sweep, or if there are no
finished worktrees.

## 2. Dry-run the orphan sweep

Run `prune-worktrees` with **no flags** (dry-run) and show the user the full
report verbatim. It reports five categories:
- Orphan databases
- Stale port entries
- Leftover directories
- Orphan containers
- Orphan nginx blocks

If it says "Nothing to prune. Clean.", report that and stop — you're done.

### 2a. Independently verify the destructive categories (MANDATORY)

**Do not trust the report for the two categories that destroy work.** Verify
them yourself against `git worktree list` before offering to apply anything:

```bash
git worktree list --porcelain | awk '/^worktree /{print substr($0,10)}' | sort > /tmp/ts_live.txt
# every path prune-worktrees called a leftover:
comm -12 /tmp/ts_live.txt /tmp/ts_leftover.txt        # <-- MUST be empty
```

- **Any overlap → STOP.** A directory that is both "leftover" and a registered
  worktree is a false positive, and `--remove-dirs` would `rm -rf` live work.
  Report it as a tooling bug and do not pass `--remove-dirs`.
- Also run `git -C <dir> status --porcelain` on each reported leftover. A
  *genuinely* de-registered directory can still hold uncommitted work — show the
  user the dirty ones and get explicit per-directory confirmation.
- Because `--remove-containers` is gated on the leftover/stale-port evidence, a
  false leftover poisons it too. If the leftover list was wrong, treat the
  container list as unproven as well.

This step exists because the detector has been wrong in exactly this way: a
`git worktree list --porcelain | grep -qxF ...` liveness check under
`set -o pipefail` returned 141 (git killed by SIGPIPE when `grep -q`
short-circuited on a match) *even when the match succeeded*, so live worktrees
were classified as leftovers — non-deterministically, 12/18/20/21 of them on
four consecutive dry-runs. Fixed in `prune-worktrees`, but keep verifying: this
skill's job is to be the check on the script, not its megaphone.

### 2b. Sanity-check the orphan database list

`--prune` drops databases. The mapping it uses is per-*worktree*
(`<project>_<create-name>`), so anything else that merely starts with the
project name — per-test-run databases, cached migration templates — is
"orphaned" by construction rather than by evidence. That is usually fine (they
are disposable and get recreated), but say so explicitly and name what will go,
rather than reporting a bare count. Call out in particular any template
database matching the **current** migration head: dropping it is safe but costs
a rebuild on the next test run.

## 3. Apply on confirm

Show the user exactly which categories are non-empty and ask whether to apply.
The flags map to categories:
- `--prune` — drop orphan DBs + rewrite `.worktree-ports`
- `--remove-dirs` — `rm -rf` the leftover directories
- `--remove-containers` — `docker rm -f` the orphan containers
- `--remove-nginx` — strip the orphan nginx blocks and restart nginx

Default to applying **everything the dry-run found**:
```bash
prune-worktrees --prune --remove-dirs --remove-containers --remove-nginx
```
…but if the user only wants some categories, pass just those flags. Show the
command before running it.

**Caveats to relay:**
- `--remove-nginx` edits the repo-tracked nginx config file — the user may need
  to commit or discard that change afterward.
- Some leftover dirs contain root-owned files (docker-as-root); if
  `prune-worktrees` reports it couldn't remove one, relay its `sudo rm -rf`
  suggestion rather than retrying.
- The container sweep only removes containers whose worktree is *independently*
  known-dead (a stale port or leftover dir). A container with no other trace is
  left alone — mention it and let the user remove it by name if they want.

## 4. Report

Summarise: which finished worktrees were torn down, and what the sweep removed
per category (DBs, ports, dirs, containers, nginx blocks). Note anything left in
place and why (in-progress worktree, root-owned dir, no-evidence container).
