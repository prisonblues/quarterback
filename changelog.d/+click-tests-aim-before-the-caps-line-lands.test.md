# the click tests stop aiming at a pane that is still moving

The dashboard's live click drivers waited for `row_count` and then clicked. That says the
rows are *in* the table; it does not say the pane has finished deciding where the table
is. `Pilot.click` resolves the widget's position when it is **called**, so a click aimed
while something above is still arriving is delivered a row high, onto the header — which
`ClickTable.on_click` refuses, correctly, as `row: -1`:

```
before click : region y=21, caps line hidden
at dispatch  : region y=22, caps line shown, meta row=-1   ← the header
```

Nothing is wrong with the dashboard here; the refusal is the behaviour a click test exists
to protect. What was wrong is a driver reading "the data arrived" as "the screen has
settled", and it cost about two failures in six runs of
`test_a_plan_row_explains_itself_and_its_hammer_takes_the_issue` on `main`, read as flake.

**The fix is upstream of the click, in every driver.** Two things on this screen move
everything under them: the caps line APPEARS (`display: none` until its first answer) and
SEATS GROWS, being the one table sized to its content. `refresh_limits` was already off in
these drivers for exactly that reason — but #426 gave the caps line a second source, the
review queue riding the gh clock, so the old guard stopped covering it. Both sources are
off now, and the seat list with them; none of these tests is about the caps, the queue or
the seats.

`_click_row` is the backstop for what that cannot reach — the pane's own first layout
pass. It waits for the coordinate `Pilot.click` will compute to hold still across two
consecutive reads with a real row under it, then clicks. Deliberately not written against
any particular mover: waiting for the caps line specifically was the first cut and it was
wrong twice, going the moment `display` flipped (a style flag, not a completed layout) and
spending its whole bound learning that no caps line was coming.

Eight runs of the four live click tests, after: all green.
