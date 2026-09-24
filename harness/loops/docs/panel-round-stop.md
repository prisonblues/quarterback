# Panel round-stop branches — the rare cases

`/panel-review-pr` §5 sends you here when a round reports one of these states. Each
section is the full contract for that case; read the one that matches before acting.

- `config_notes` reports a rewritten range, or `fix_range_source` is present →
  *Do not rewrite the branch between rounds*.
- `round_stop.stop_reason` names `fix_injection`, or `round_stop.revert` is populated →
  *When the cycle ends because the FIX PASS was generating the work*.
- `round_stop.excision.count` is above zero, or `excision.declined[]` is non-empty →
  *When a SUB-FLOOR fix caused the finding*.
- `config_notes` says a round `follows an integration` (the range holds a merge
  commit) → *When the range between the rounds is an integration*.

The `/tmp/tmp.AbC123` paths below stand for the directory `mktemp -d` printed in the
command's §3. Section marks (§4b, §6) refer to `/panel-review-pr`.

## Do not rewrite the branch between rounds, and know the cost if you must (#500)

**A rebase or force-push between rounds disarms three of this cycle's convergence
instruments at once**, because provenance (#48), recurrence (#67) and `--scope
increment` all read the same thing: the range between the last round's `head_sha`
and this one's. `compare/a...b` is the three-dot form, so after a rewrite the old
head is no longer an ancestor and GitHub answers `diverged` — the range would span
commits no fix pass wrote, so the panel refuses it rather than blaming the fixer for
every line the PR ever added.

What that costs is concrete. Every new finding is recorded `unknown` instead of
`introduced` or `missed`, and **`escalate_on.fix_injection` (#497) cannot fire**:
the rate is `introduced` over every new outstanding finding, and the unattributable
ones sit in the denominator, so it is depressed toward zero however badly the fix
pass behaved. On the cycle #500 was filed from, that happened on round 3 of a
three-round cycle that ended on the cap — the exact shape the gate exists to stop.

The round says so where the verdict is read: a **veto line, and `confident` false**,
the same treatment a reviewer that could not read the whole diff gets.

**And then it tries to repair it (#504).** The range is wrong, not the history: the
fix pass's commits are still on the branch under new SHAs, and `git patch-id` names
them by what they CHANGED rather than by where they sit. So a rewritten round rebuilds
the pass out of the local object store, `payload.fix_range_source` reads
`reconstructed`, provenance, recurrence and `escalate_on.fix_injection` come back, and
the veto does not fire. Read `config_notes`: the round states what it rebuilt and what
it cost.

**It is exact or it refuses, and a refusal leaves the round exactly as blind as it
was** — the veto fires and nothing is attributed, with `fix_range_rebuilt.why`
naming which of these it hit:

- **No local checkout.** `patch-id` is git rather than the compare API, so a repo
  with no `path` in its rules cannot rebuild anything — and neither can a box that
  never held the pre-rebase head, since a rewrite only orphans commits where
  somebody still has them.
- **A commit the last round reviewed changed content in the rewrite** — a conflict
  resolved during the rebase, an amended tip. That commit is somewhere among the
  ones this would call the fix pass and nothing can say which, so attributing them
  would blame the fixer for work already reviewed.
- **The pass is not the TAIL of the branch** (a reorder, an `--autosquash` that
  landed a fixup low in the series). Then no single diff is the pass, and reading
  its commits' patches separately would attribute lines the pass added and then
  removed.
- **An ambiguous patch-id** — the branch carries more copies of a patch than the
  last round had, so which is the fixer's own cannot be told from which is the
  replayed one.
- **No correspondence at all** (a squash, a re-created branch), and **a branch reset
  BACKWARDS**, where the pass was removed rather than rewritten. The round says the
  second in those words, because a force-push that dropped work must not read as a
  quiet cycle.

Refusing rather than leaning is a deliberate trade, and worth knowing when you read a
round that did not rebuild: `escalate_on.fix_injection` is calibrated on `introduced`
being a FLOOR, so a reconstruction that over-counted would end cycles wrongly and no
`config_notes` line prevents that — nothing reads a note before firing a brake.
`--scope increment` is not repaired either way (scope is settled before the seats
run), and neither is #506's proposal below, which reads the compare range.

So:

- **Prefer merging the base branch into the PR** over rebasing it. That leaves the
  old head an ancestor (`status: ahead`), so the range still reads without a rebuild.
  It is not free — the base branch's own commits then fall inside the range and their
  lines are attributed to the fix pass, so `introduced` over-counts — but an
  over-counting instrument is worth more than a dark one, and it fails toward stopping
  the cycle rather than toward letting it run.
- **If you must rewrite, do it between CYCLES rather than between rounds** — after a
  stop, before the next `--round 1`. The rebuild is a repair, not a licence: it costs
  an accuracy you did not have to spend.
- **Rewrite in the checkout the panel reads.** A rebase done somewhere the panel will
  never see — another box, a worktree that is then thrown away — is the one that
  cannot be rebuilt, and it looks identical to the one that can until the round runs.
- **If you already have, and `fix_range_source` is not `reconstructed`, do not read
  that round's quiet as convergence.** The veto says as much. Re-running the round
  with `--scope pr` gets the review back but not the attribution; only a round whose
  fix pass can be reached — by range or by patch — can attribute.

One instrument this does *not* disarm, worth knowing so you do not over-correct:
#84's premise register is keyed on declared text rather than on commits, so it
survives a rewrite intact. `max_fix_growth`/`max_fix_growth_chars` also keep working,
but note they measure against `Baseline.first_reviewed` — a base-branch merge inflates
the PR against a denominator from before it, so a ceiling may fire on growth the fix
passes did not write.

## When the cycle ends because the FIX PASS was generating the work (#489, #506)

`escalate_on.fix_injection` ends the cycle when more than half a round's new
outstanding findings were attributed to the fix pass immediately before them: the
loop's rule 1 is being fed by the loop's own output, and a termination test fed by
its own output can only end on the cap. You will see it as a veto line, `confident:
false`, and a `stop_reason` that names the dial rather than the cap.

**Ending the cycle is half the answer, and the other half is your job.** The fix
pass that caused it is still on the branch — the PR ships carrying a change the panel
has just finished saying generated more of the round's work than the pull request
did, minus the round that would have found the rest of it. Stopping means the loop no
longer makes it worse; it does not make it better.

So the round hands you the decision already priced, in `round_stop.revert`:

```
jq '.round_stop.revert' /tmp/tmp.AbC123/r<r>.json
```

- `range` / `commits` / `commit_count` — the offending pass's **commit range**,
  which is the same range provenance attributed against, and the commits inside it.
- `spans` — how many fix phases that range covers. Normally `1`. More than one means
  no intervening round recorded a commit to anchor on, so the range is wider than "the
  last fix pass" — the rate was computed over all of it too, but say so when you
  relay it.
- `command` — the `git revert --no-commit` invocation, with FULL SHAs. Nothing has run
  it. **It can be `null` even when the proposal was made**, and then `no_command` says
  why: a merge commit inside the range (a `git revert` of a range refuses a merge
  without `-m`, and a merge is how the base branch got in there — reverting wholesale
  would undo commits no fix pass wrote); a range GitHub's compare truncated, where a
  merge past its 250-commit ceiling would be invisible so the merge count is a floor;
  or commits that could not be listed at all.
  The range is still named in both cases; it is only the paste-and-run shortcut that is
  withheld. If you see `no_command`, **do not reconstruct the command** — go and read
  `git log --oneline <range>` and decide what actually wants undoing.
- `removes` — what undoing it would take off the board: the findings this round
  attributed to it, with severities.
- `costs` — what undoing it would hand back: the complaints that pass was **sent to
  answer** and this round no longer raises.
- `still_open` — the complaints it was sent to and did not clear. Those are
  outstanding either way, so reverting costs nothing there.

**Take it to the user; do not act on it.** Reverting a pass reverts the real fixes in
it, and a pass that cleared three P2s and introduced eight P3s is a net loss to undo
wholesale — nothing in the loop knows which is which without asking, which is exactly
why this is a proposal. Read the two columns knowing they are biased in opposite
directions on purpose: the cost is an **upper bound** (matched on finding keys alone,
and under `increment` scope it includes complaints this round did not re-read) and
the benefit is a **lower bound** (`introduced` is a documented floor). A revert those
numbers still argue for is one they cannot have talked you into.

**On a rebased branch there is no proposal, and the round says so rather than going
quiet.** `revert.kind` carries the fix range's own verdict — `ok`, `no-fix`, `blind`,
`rewritten`, `not-asked` — and the last two are the case the subsection above is
about: the range that would name the offending pass is the range a rewrite removes, so
the cycle can measure a change it cannot point at. `offered: false` with a `kind` of
`blind` or `rewritten` means "we cannot see this", not "there was nothing wrong". This
is the one thing #504 does **not** give back: a round can be attributing from a
rebuilt pass and still be unable to offer a revert, because the proposal reads the
compare range rather than the reconstruction.

Two things this deliberately does **not** do, so you are not waiting for them:
revert-and-re-run as an automatic mode, and re-running the fixer with a narrower
brief instead of reverting. Both are decisions a human takes (#506). The excision
below is not an exception to that — it undoes **one fix**, never a pass.

## When a SUB-FLOOR fix caused the finding, excise it rather than repairing it (#627)

**The rule.** When a round attributes a new finding to a fix that answered a finding
**below `round_trigger_floor`** — a P3 or P4, one of the 💸 items the budget paid for —
the response is to **revert that fix**. One fix, its own commit, which is why the fixer
brief asks for each budgeted fix to be landed as its own commit naming exactly one finding
ID. Then:

- the sub-floor finding it answered **returns to the board as reported-and-not-fixed**,
  exactly as an unpaid budget item does — a `deferred` row with its one-line note (§4b);
- the finding it caused **disappears with it** and is not handed to a fixer, because
  there is no longer anything for a fixer to be briefed about;
- **the cycle continues.** This is not an escalation, not a stop, and not a decision you
  take to a human. It is the cheap correction that lets the round carry on, and treating
  it as a stop is the expensive reading of a cheap fact.

**Why this is safe here and not in general.** Automatic backtracking over a whole fix
pass was considered and refused, and `round_stop.revert` above is that refusal: a pass is
**mixed**, and reverting one that cleared three P2s to remove five P3s puts the P2s back.
Nothing in the loop can tell which half is which without asking, which is why that
proposal is priced and handed to you rather than executed. **A single sub-floor fix is
not a mixed pass.** It answered one finding that was, by definition, not blocking the
close, so the entire cost of removing it is one P3 or P4 returning to a state this repo's
own policy already calls reportable and non-blocking. There is nothing to weigh, and
where there is nothing to weigh there is no decision to take upstairs.

**The one case where it does not apply: a sub-floor fix a later blocking fix has built
on.** Reverting it then is not a clean excision — it takes lines a P1 or P2 fix depends
on, and undoing a blocking fix is exactly the mixed revert this rule is careful not to
be. **Report it instead of forcing it**: name the sub-floor fix, the finding attributed
to it, and the blocking fix that now rests on it, and let the round proceed normally with
the caused finding handed to a fixer like any other. A forced excision that breaks a P1
fix has converted the cheapest correction in the loop into the most expensive one.

**The round works out which fix, and publishes it — you do not have to.** `_provenance`
attributes a finding to the fix *pass*; an excision needs the individual fix, and
`round_stop.excision` is that answer:

- **`count`** — how many excisions this round names. `null` is "nobody looked" (round 1,
  a rebased range, an anchor payload whose trigger floor cannot be read, a checkout that
  could not list the pass) and `why` says which; `0` is a measured none.
- **`excise[]`** — one per fix, each carrying the `commit`, its `subject`, the
  `command` (`git revert --no-commit <sha>` — **run it**), `answered` (the sub-floor
  finding that goes back on the board unfixed: record it `deferred` with its one-line
  note, §4b) and `caused` (the findings that go away with it: hand a fixer **none** of
  them). The report lists the same thing under **Excised, not fixed**, and every caused
  row in `to_fix` is flagged `excised: true`, so a list pasted out of the report cannot
  pick one up by accident.
- **`declined[]`** — a seam it refused, with a sentence: a later commit in the pass built
  on the fix, the commit answered more than one finding, it is a merge, or the checkout
  could not be read. Those caused findings are still in the cycle and are fixed like any
  other finding. **Relay the sentence** — this is the case #627 says to report rather
  than force.
- **`seams`** and **`sub_floor`** — how many commits in the pass named exactly one
  sub-floor finding, against how many sub-floor findings the pass was sent to. `seams: 0`
  with `sub_floor` above zero means the pass left nothing to excise; that is the fixer
  brief's instruction not being followed, and it is worth a sentence to the user because
  the cheap correction was unavailable on this round as a result.
- **`floor`** — the trigger floor that decided which findings were sub-floor. It is the
  **anchor** round's, not the round you are reading, so quote it from here rather than
  from `review_panel`: a floor moved between rounds would otherwise have you naming a cut
  the classification did not use.

**The excision's churn is churn, and `low_severity_fix_lines` counts it.** The revert
commit lands in the next round's fix range, and every churn reading there counts it —
the split, the guard ceiling, the surface count and the budget pricing alike. That is
#692's unit working as intended, and it means `round_stop.fix_budget.spend` on the NEXT
round includes the lines this excision removed. If that round prices an overspend made
of the excision you were told to make, report it as the cost of the correction rather
than as a fixer spending its budget badly. What comes
out of the next round's **attribution** is only the lines the revert restored, because
those sat at an earlier round's head and #559 is what stops a correction reading as the
disease.

**What it does NOT price is what the excision destroys (#558).** `destroys` names the
files, the lines and how many of them sit in test or documentation paths, and that is a
line count rather than a valuation. A sub-floor fix is very often the only test over the
path it was written for, and `answered` says nothing about that. The rule applies even
though `destroys` does not price the lost coverage; when `destroys.guard_lines` is most of
the commit, say so to the user in the same breath as the excision.

## When the range between the rounds is an integration (#278)

An integration moves the head, but it does not by itself invalidate the round that
preceded it — merging `origin/main` into a branch to clear a stale base need not cost a
whole panel cycle. **What decides it is how much of the merge is genuinely new material
to this PR.** The measurement is `git diff` between
the commit the round read and the merge result, restricted to the files this PR
touches, counted in changed lines, against `review_panel.distant_merge_lines`
(default **20**; `0` admits only an empty resolution, `null` treats any head move as
a review of earlier code).

Whenever the range carries a merge commit, the round says which reading it took, in
`config_notes`. Read it — the two are different claims about coverage and you must
never have to infer which happened:

- **`round N follows an integration and takes the DISTANT reading`** — the merge
  touched nothing this PR touches and the resolution was trivial or absent, so
  **the earlier round STANDS**. Nothing is being claimed as reviewed that was not:
  the merged code is not this PR's change and is not what the findings are about.
  A round was not required on that merge's account, and `preland`'s `review` check
  says the same thing as a WARNING rather than a HOLD.
- **`round N follows an integration and takes the INVOLVED reading`** — a real
  resolution in code this PR also touches. That resolution is unreviewed work and
  it gets reviewed — **only that part**, which is what the increment already is
  when it is pointed at the range between the round and the merge. `preland` HOLDs
  until a round has read it.

A range with **no** merge commit in it is never distant, whatever its size: that is
a push, not an integration, and unreviewed work of this PR's own kind holds at any
size. So does a range that could not be measured at all.
