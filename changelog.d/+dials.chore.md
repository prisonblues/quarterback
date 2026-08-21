# the dials go back to P3 and two rounds, because the reasons they moved are fixed

`.harness-rules.sample` has run `fix_severity_floor: P2` and `max_rounds: 1` since 2026-08-20. Both
were right when they were set, and both were set to work around problems that shipped fixes on
2026-08-21.

**The floor.** P3 was given up because fixing that tier ACCUMULATES — PR #188's 185-line feature
became 721 churned lines, 74% of it review-response code, off a round-2 list 89% below P2. Both
halves of that are now bounded: `low_severity_fix_lines` (#297) budgets the band cheapest-first and
counts rather than estimates, and `max_fix_growth` (#298) was dividing a whole-PR baseline by *one
round's increment*, so a PR at 3.90x cleared a 3.0x cap and the backstop never fired.

Note what is NOT being claimed: this key's own condition was "restore P3/P2 the day the
deferred-finding backlog is empty", and that backlog is still there. It is the other half of the
argument that changed.

**The rounds.** One was chosen because every panel measured had ended on the cap, making the cap a
budget rather than a safety net. But round 2 is where this repo's expensive defects are found: it
caught the FIFO hang round 1 created on PR #236, and on PR #299 five rounds produced 39 of 53
findings introduced by the previous fix pass, round 2 being 17 of 17. And `max_rounds: 1` is the
setting that switches #84's premise brake OFF — there is no second fix pass for it to refuse, so
the futility brake shipped and could not fire.

Both changes are experiments with a stated way back, written at the keys themselves: tighten the
budget before touching the floor, and `max_rounds: 1` remains the known-good value with its
argument intact. `panel.py --max-rounds` still wins for a single run.
