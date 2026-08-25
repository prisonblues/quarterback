# the ⚖ cancel test stops depending on the fleet having two open PRs

A fix round on #433 closed a real hole: the "cancelling starts nothing" block pressed escape
and asserted nothing had started, without first asserting anything had been raised to cancel.
A click that missed left `started` empty and made the escape a no-op, which reads exactly
like a cancel that worked — a pass that could not fail.

The assertion was right and the row was not. It clicked row 2 unconditionally, and a second
row is not something a test can arrange: the OPEN PRs panel shows what the fleet has open. On
2026-08-25, with that morning's work merged, the repo had **one** open PR — so the click went
past the last row, `ClickTable.on_click` refused it as it should, no dialog appeared, and the
suite went red about the fleet's state rather than about the dashboard's behaviour. The
commit that added the assertion names the hazard in its own comment and then does not guard
it.

Row 2 when there is one, row 1 otherwise. Cancelling is worth testing on any day, and which
row it happens on was never what the test was about.
