# Landing hazards

The steps in `fix-and-land.md` are about decisions — the queue, the gate, what you are and are not
allowed to judge. This page is the other half, and it is written from the *symptom* rather than the
cause, because the symptom is what you will be looking at when you need it. Every entry is a
property of the tools — `gh`, `git`, dcg, CI — rather than of any one machine.

## Five of them already have a guard

Where a mechanism exists, this is one line saying so. Read what it says when it fires; do not
re-derive it, and do not go looking for the trap by hand.

- **A merge resolution that deletes a shipped release's notes** — the `frozen` job, *"no shipped
  release entry was rewritten"* (#325, v2.85). It compares the **text** of every `## vX[.Y]` entry
  present at both refs, byte for byte, because on `feat/issue-232` the branch's own entry was
  relocated *under* `## v2.59` on top of that release's notes and every heading-based check read
  the file as correct: the headings were all present, unique and correctly ordered.
- **A PR that ships something and writes no release note** — the `changelog` job, *"a change that
  ships carries a release note"* (#365, v2.95). It parses the fragments too, and since #122 it
  is the only place a malformed one is caught before the release job reads it.
- **A branch that writes to a file the release job generates** — the `generated release files
  are output` job (#122). `CHANGELOG.md` is written on `$BASE` after the merge and by nothing
  else; a branch that edits it is refused here and by `pre-push`, with
  `changelog.d/<issue>.<kind>.md` named in the refusal.
- **A merge that would leave two migration heads** — the `migration-heads` job, *"one migration
  head after the merge"* (#351, v2.88). The `pre-push` hook asks the same question and never gets
  to answer it on the path this fleet lands by: the merge is an API call, and no push carries the
  commit that creates the second head.
- **CI that looks like it has not run** — `qbdata.CI_STATES` gave the state a word (#324, v2.78)
  and preland refuses on it: *"CI will not run without a human — the run for this head is gated
  and has executed nothing, so nothing is verifying this change"*. A run behind GitHub's
  workflow-approval gate is created, executes nothing, contributes no check runs, and leaves the
  PR's check list **empty** — which, before there was a word for it, was indistinguishable from a
  PR nobody had pushed to. PR #282 sat two days that way over a run that had gone red two commits
  earlier. `conclusion` alone cannot reach that distinction; `blocked` is the state that can.

All five reach you through preland's verdict anyway, which is why step 4b says the verdict is the
decision. Knowing them is diagnostic rather than procedural: it is what lets you read a HOLD as a
fact about the branch instead of a fault in the tooling.

## `--delete-branch` from a worktree: the merge landed, the branch survived, and the error reads like the merge failed

```
$ gh pr merge 216 --merge --delete-branch
failed to run git: fatal: 'main' is already used by worktree at '/home/rich/source/quarterback'
```

The merge had already landed. What did not happen is *either* half of the cleanup. To delete the
local branch `gh` first moves off it, onto the default branch — and a sibling worktree holds that
branch, so `git checkout main` exits 128 and `gh` abandons everything after it, including the
**remote** delete, which never needed a local checkout at all (#260).

The trigger is narrow and this loop lives inside it: cwd is a worktree not on the default branch,
and the default branch is checked out somewhere else. That is the shape of every landing here.

So do not read that message as a failed merge. Ask: `gh pr view <pr> --json state,mergeCommit`.
If it merged, finish the cleanup by hand — `git push origin --delete <branch>` — and say in your
report that the merge landed and the cleanup did not.

**What #260 does not establish is `gh`'s exit code**: the original observation was piped through
`tail`, so the status captured was `tail`'s. The abandoned remote delete proves `gh` gave up on
its cleanup; it does not prove the process exited non-zero. Treat the message as unreliable and
the branch as probably still there, and do not infer anything further than that — `lander.py`
merges under `check=True`, and whether that raises after a successful merge turns on the very
number nobody has captured.

## Impossible test failures that move between runs are a second pytest, not your branch

A landing agent reported **118 failures** against PR #349's merged result on 2026-08-22, re-ran
the suite on its own, and got a clean pass. It had a targeted suite running concurrently against
the same worktree database (#366).

The shape to recognise: a row the fixture just committed reported absent, foreign-key violations,
`ObjectDeletedError` — failures that are impossible rather than merely surprising, and that move
around between runs. The other run's provisioning path terminates every backend on the database
and drops it, and what the victim reports is a scattered set of **assertion** failures rather than
a connection error — which is precisely what makes it read as the PR's fault. (#366 attributes the
absent error to `pool_pre_ping` handing back a reconnected connection; treat the observed shape as
the fact and that as the explanation offered for it.) PR #30 gave every worktree its own database,
which removed the cross-worktree case and not this one.

Nothing guards it — #366 is open. So: **one pytest at a time per worktree.** Do not start a
targeted run while a full one is going, which is precisely what a lander does by reflex. And when
a suite comes back with failures that look impossible, check for a concurrent pytest *before* you
read them as the PR's, because the honest response — re-run it — makes them disappear and confirms
the wrong conclusion.

## Undoing a change: both obvious ways are refused, and each one recommends the other

`git stash push` stops here with

```
REFUSED: refs/stash is shared across every worktree of this repo.
```

That is a `reference-transaction` hook `create-worktree` installs, and it is right: `refs/stash`
lives in the **common** git dir, so a stash pushed in one worktree is listed and poppable from
every sibling and `stash@{0}` there means whatever the last pusher meant. Two working trees have
already gone that way (#210). `qb-stash push` is the per-worktree replacement — same verbs, stored
under `refs/worktree/`, invisible to siblings, and it dies with the worktree.

`git checkout HEAD -- <path>` is refused too, by dcg's `core.git:checkout-ref-discard`, whose
advice is *"Use 'git stash' first"* — the thing the paragraph above refuses. The two point at each
other, and on 2026-08-22 two agents worked around it independently without either knowing the
other had.

The way through is a patch file, which is what `/fix-issue`'s red/green step wants anyway, since
`qb-stash` takes no pathspec:

```bash
git add -N <the files your change touched or added>
git diff HEAD -- <those same files> > .redgreen.patch
test -s .redgreen.patch || { echo "STOP: captured nothing"; exit 1; }
git apply -R .redgreen.patch     # the change is gone
git apply .redgreen.patch        # and back
```

`git apply` in both directions is allowed; it is not a checkout and it takes a pathspec by
construction. Delete the patch afterwards: it is untracked, so preland reports it as a warning
rather than a reason, and what it actually costs you is a `git add -A` sweeping it into the commit.

**The redirect in that snippet is written as a relative path, and that is load-bearing.** dcg's
`core.filesystem:redirect-truncate-root-home` refuses a truncating redirect whose target is
spelled as an **absolute** path under `$HOME`; the same redirect written relative to a directory
you have already `cd`'d into is allowed. Both measured, 2026-08-22. So a heredoc addressed to the
full path of a file in your worktree does not run at all, and the refusal reads as your command
being wrong rather than as a policy — while the same command one `cd` earlier goes through. `>>`
is allowed at either spelling because it does not truncate, and `tee` is not a redirect; either is
what to reach for when the path has to be absolute.

**Both rules match on the command's TEXT, not on what it will do**, which is the part that costs
you time rather than a file. Writing this section tripped the redirect rule from a `>` inside a
Python string an editing script was carrying, and tripped the checkout rule from the literal
`git checkout HEAD -- <path>` inside a commit message being passed on a heredoc. Neither command
was going to do the thing. So when a refusal names a rule you are not breaking, look for the
pattern quoted in your argument — and put long prose in a file and pass the path, which is the
fix for both.

## "Served version unchanged" is the correct answer for a harness-only release

After a harness-side land, `qb-doctor` reports the board serving the number it served before, and
it reads as a failed deploy every time. It is not one.

`pyproject.toml` and `app/main.py` carry the version `GET /openapi.json` reports, and
`scripts/release.py` moves it only when the release changed `app/` or `migrations/` — `BOARD_PATHS`
is exactly those two, and `harness/`, `scripts/`, docs and tests are deliberately outside it.
Most releases here are harness-side and correctly leave the served version alone. The release
**number** moves, in CHANGELOG.md and README.md; the served version does not, and `qb-doctor` then
says *"matching this checkout"* because the checkout did not move either.

The bump is inferred rather than declared, measured from the previous release's tag, and the
release job always reports which way it went — read that line rather than guessing. This is not
something a landing loop sees or decides: it happens on `$BASE`, after the merge.
