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
  done — its PR is merged (`gh pr view <branch> --json state,mergedAt` or
  `gh pr list --head <branch> --state merged`).
- **Do not judge "done" against the default branch.** `git branch --merged main`
  is only meaningful where PRs actually target the default branch, and in at
  least one repo here they do not: lexray merges into `fca` and `test` while
  `main` sits frozen, so that check calls every branch unmerged.
- **A merged PR does not mean the branch holds nothing.** Commits added *after*
  the PR — the post-merge tweak nobody pushed — die with the branch, and
  `remove-worktree` deletes the branch unless `--keep-branch`. Compare against
  the PR's own head SHA, which is base-independent:
  ```bash
  head=$(gh pr list --head "$br" --state all --json headRefOid -q '.[0].headRefOid')
  git cat-file -e "$head" || echo "cannot verify — treat as in-progress"
  git log --oneline "$head".."$br" --not --remotes    # non-empty => do not reap
  ```
  `--not --remotes` is what makes this about loss rather than tidiness:
  `remove-worktree` deletes only the *local* branch, so a post-PR commit already
  pushed somewhere survives the teardown and must not block it.
  The `cat-file` guard is load-bearing: with the object absent `git log` fails and
  a bare `| wc -l` reads 0, i.e. "nothing to lose" on exactly the branch you
  cannot vouch for. `remove-worktree` refuses these itself, but classify them as
  in-progress here so the user is not offered them in the first place.
- **A merged PR does not mean nobody is in there.** Run
  `worktree-holder <path>` on every candidate before offering it for teardown,
  and treat exit 3 as in-progress no matter what its PR says — an agent can be
  addressing review findings on the same branch after the merge. Name the holder
  when you report it.
- Show the user the list of worktrees that look **finished** vs **in-progress**.
  Ask (AskUserQuestion) which to tear down. For each approved one, run
  `remove-worktree <create-name>` (the dir suffix after `<project>-`). This is
  the clean path — it handles all trappings and prunes the branch too.
- Leave in-progress worktrees alone.
- If `remove-worktree` refuses because another agent holds the worktree, relay
  that verbatim and stop. Do **not** pass `--force` on your own initiative; it
  exists for a user who has seen the holder's name and decided anyway.

Skip this phase if the user just wants the orphan sweep, or if there are no
finished worktrees.

## 2. Dry-run the orphan sweep

Run `prune-worktrees` with **no flags** (dry-run) and show the user the full
report verbatim. It reports six categories:
- Orphan databases
- Stale port entries
- Leftover directories
- Orphan containers
- Orphan nginx blocks
- Orphan board claims

If it says "Nothing to prune. Clean.", report that and stop — you're done.

### A `NOT CHECKED` line is not a clean one

Each category has a fourth state (#735), and it is yellow rather than green:

```
? Orphan board claims: NOT CHECKED — `qb-claimed` exited 2 (no board configured,
                                     or it could not be reached)
```

That category has **no answer** — not an empty one — so relay it as unresolved
rather than folding it into "nothing found", and do not report the sweep as
complete. `Nothing to prune. Clean.` is withheld from such a run for the same
reason; what prints instead names which categories went unchecked, and the sweep
is worth re-running once the cause is fixed.

It matters most for claims. Everything else here is recoverable by sweeping
again — a leftover directory is still there next time — but a claim sits on an
8h TTL and this sweep is what hands it back early, so a category silently
skipped is a plan item held for the rest of the day.

This is the same rule as step 2a and as `worktree-holder`'s exit 4: a check
nobody made must not read as a check that passed.

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
- `prune-worktrees` reports directories held by a live agent under their own
  heading ("Held by a live agent — left alone") and keeps them out of the
  leftover list entirely, so `--remove-dirs` cannot reach them. Relay that
  section if it is non-empty: it is the sweep telling you an agent is still
  working somewhere the tooling thought was debris.
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
- `--prune` — drop orphan DBs + rewrite `.worktree-ports`, and hand back the
  orphan board claims
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
