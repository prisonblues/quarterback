# the landing queue stops being advice and starts being a verdict

The queue that shipped with #227 answered correctly and bound nothing. Its own closing note said
so: *"nothing yet forces the stop."* Five agents could each still rebase, push, wait for CI and
race, because the only thing that could have stopped them was a board endpoint nobody was obliged
to call — a mechanism that ships unwired, which is this repo's named defect (#169) and the exact
shape of the thing the queue was built to fix.

The stop is now part of the pre-land verdict, and the loop that lands code takes its place in the
line before it spends anything.

### `preland.py` grows a `queue` check

A PR that is not at the head of the line for its base is **not READY**. The reason names the
position and the agent holding the place ahead, in the board's own words, so a stand-down is
something to act on rather than something to poll against:

> `queued behind #123, position 3 of 5 — do not rebase, push or restart CI: you would spend a run
> to learn what this line already says, and invalidate #123's checks doing it` — #123 is held by
> `zeus/opal-kelp` (landing the auth fix). Stay queued: your place is kept while your entry is
> renewed, and leaving would re-join at the back.

A PR that never enqueued while others are queued is refused too, because otherwise the way past
the gate is to skip the mechanism. Three things it deliberately does **not** do:

- **It rules on position and nothing else.** The board also records whether an entry is `ready` at
  a given commit, and gating on that would be preland refusing to run until it had already run —
  its own verdict is what produces that assertion. A head whose entry is behind the branch gets a
  warning saying so, and this run is the re-check that clears it.
- **It imposes nothing on a lone PR.** An empty line passes with no friction at all. A gate that
  made the ordinary case harder is a gate people turn off.
- **It takes nothing.** One `GET`, no writes, no claim. Being at the head is still only permission
  to go and ask for `kind='merge'`.

A board that answers 404 reports `skipped-absent` — the same capability answer a repo with no
`scripts/migration_reconcile.py` gets, because a board deployed before the queue existed has no
line to read. Every other failure is an ERROR: a line this gate cannot see is a line it cannot
rule on.

### `/fix-and-land` joins the line before it integrates, and always leaves it

Enqueue is step 4a, ahead of the gate and ahead of any integration push, because the expensive
half is the integration — a stop in front of the merge would already have paid for the CI run. Then:

- **a HOLD that is only the queue** stands down, reports the position, and **keeps its entry**. A
  loop that read "not your turn" as "leave" would go to the back of the line every time it was
  overtaken, which starves the PR;
- **a HOLD for anything else leaves**, with a reason. An entry for a PR that cannot land holds up
  everybody behind it until its TTL runs out, which is why `enqueue` refuses a `hold` verdict on
  the way in;
- **a RECONCILE re-enqueues at the commit its push produced**, because the push voids the entry's
  readiness and the board cannot see it happen;
- **a merge claims the base, re-verifies, merges, and then stands down** with `reason="merged"`.

Every exit releases the lease, and the 30-minute TTL is the backstop for the exit nobody coded.

### The merge claim now keys on the base, not the head (#318)

`preland.check_merge_claim` read `<repo>:<head branch>` while the queue read `<repo>:<base>`. Its
docstring names the incident it exists to prevent — *"on the same day two agents merged at once"* —
and under a head key that incident is not prevented: two agents landing two **different** PRs into
`main` hold `<repo>:feat/a` and `<repo>:feat/b`, never see each other, and both merge. The head key
catches only the rarer case of two agents landing the same PR.

The audit that made the change safe rather than merely right: `derive("branch", …)` is the only
maker of merge keys, and **every branch claim in this fleet is a landing claim**. `create-worktree`
claims the *issue* its branch names, the plan restricts `ref_kind` to issue and pr, and `qb-hook`'s
`kind: "branch"` post ref is an annotation that takes no claim. So there was no non-landing meaning
to change out from under.

It removes a disagreement rather than creating one. `GET /merge-queue` reports the claim it finds
at `<repo>:<base>`, and a queue head is told *"take `kind=merge` on this base before you merge"* —
so the claim a lander takes is now the claim the gate reads. `/panel-review-pr` §7 and
`/fix-and-land` both claim `<base>`, and `/fix-and-land` takes the claim across its merge at all,
which it did not before: a loop merging on its queue position alone would have made the queue the
second lock #227 says it must not become.
