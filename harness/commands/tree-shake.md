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
cleans their DB, containers, nginx block, port, and dir in one go).

**The classification is the script's, not yours** (#685). Run it and show the
user the report verbatim:
```bash
prune-worktrees --finished
```
It fetches, walks **every registered worktree** — the `<project>-*` siblings
and the trees `create-worktree` never made: Claude Code's own
`.claude/worktrees/agent-*` subagent trees, a session's scratch trees under
`/tmp` — and sorts them into three buckets. (About half a second per tree;
36s for 66 on lexray.)

- **Finished** — safe to tear down. One of: the PR merged; the PR was closed
  and its head is reachable from some *other* remote branch (work integrated
  into a long-lived branch — lexray's `fca` — which GitHub records as CLOSED
  because that was not the PR's base); the PR was closed with a
  `Superseded by #N` comment and #N merged (the rebase-and-reopen pattern, whose
  old patches are rewritten and so contained nowhere); or there is no PR and the
  tip is in a remote branch. In every case: nothing committed after the PR is
  unpushed, the tree is clean, and nobody live is in it.
- **In progress** — leave alone. An open PR, a dirty tree, post-PR commits
  pushed nowhere, a closed PR that landed nowhere anyone can see (*a human
  decides those* — the report says so), a live holder (named), or a lock.
- **Cannot verify** — never fold into either bucket. `gh` did not answer, the
  PR's head SHA is not fetched, the board could not be asked, detached HEAD.
  Relay these as unresolved and do not offer them for teardown; this is the
  same rule as step 2's `NOT CHECKED` and `worktree-holder`'s exit 4.

Why the rule is what it is, so you can defend it when the user asks:
- **Not `git branch --merged main`.** Only meaningful where PRs target the
  default branch, and in at least one repo here they do not: lexray merges into
  `fca` and `test` while `main` sits frozen, so that check calls every branch
  unmerged.
- **Not "PR merged" alone.** Measured on 2026-09-06: of 29 lexray sibling
  worktrees, "merged" found one finished; the closed-but-landed rules found six
  more, and 21 subagent trees had no PR at all but tips sitting on the pushed
  `fca` feature branch. A rule that stops at "merged" leaves those forever.
- **Loss, not tidiness.** `remove-worktree` deletes only the *local* branch, so
  the question is whether any commit's only copy is on it. Containment in a
  remote ref answers that directly; the post-PR check uses `--not --remotes` for
  the same reason.
- **A merged PR does not mean nobody is in there.** The script runs
  `worktree-holder` on every candidate before anything else; an exit 3 is
  in-progress whatever the PR says, and the holder is named in the row.

Ask (AskUserQuestion) which of the **finished** ones to tear down. Each row
says its teardown, and they differ:
- `remove-worktree <create-name>` for a `<project>-*` sibling — the clean path,
  which handles all trappings and prunes the branch too.
- `git worktree remove <path>` then `git branch -D <branch>` for a tree
  `create-worktree` did not make. It has no containers, database or port to
  reverse, and `remove-worktree` cannot resolve it. `-D` is safe *because* the
  report established the tip is on a remote; do not use it on any other row.
- If `remove-worktree` refuses because another agent holds the worktree, relay
  that verbatim and stop. Do **not** pass `--force` on your own initiative; it
  exists for a user who has seen the holder's name and decided anyway.
- A directory that survives the teardown usually holds root-owned files from
  a docker-as-root container; relay the script's elevated-removal suggestion
  rather than retrying.

You are the check on the script, not its megaphone: if a row it calls finished
is one you have reason to believe is live — the branch was named in this
conversation, a peer's board post mentions it — say so and leave it. But do
not re-derive the classification by hand; two copies of the rule are how they
came to disagree without anything noticing.

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
