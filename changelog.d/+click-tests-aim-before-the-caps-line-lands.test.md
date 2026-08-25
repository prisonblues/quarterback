# the click tests stop aiming at a pane that is still moving

Three of the dashboard's live click drivers waited for `row_count` and then clicked. That
says the rows are *in* the table; it does not say the pane has finished deciding where the
table is. The caps line is the one row on this screen that **appears** mid-run — it is
`display: none` until the first limits or queue answer, since `render_limits` sets
`bar.display = bool(cells or self.queue)` — and every panel below it drops a row when it
arrives.

`Pilot.click` resolves the widget's position when it is *called*, so a click aimed in the
window before that answer is delivered one row high, onto the header. `ClickTable.on_click`
refuses it, correctly, as `row: -1`:

```
before click : region y=21, caps line hidden
at dispatch  : region y=22, caps line shown, meta row=-1   ← the header
```

Nothing is wrong with the dashboard here — the refusal is the behaviour a click test exists
to protect. What was wrong is a driver that treats "the data arrived" as "the screen has
settled", and it was costing about two failures in six runs of
`test_a_plan_row_explains_itself_and_its_hammer_takes_the_issue` on `main`, read as flake.

The drivers now wait for the caps line, then confirm the cell under the pointer is a row and
not the header, and only then click. Both waits are bounded and neither asserts: where there
is no board there is no caps line and nothing that can move, so the click still happens and a
table that genuinely never drew fails on the assertion that names it.
