# the dash uses the columns it is given

Eight panels have been sharing one column of a 78-column pane, and the arithmetic was
never going to work: DIALS and SEATS take their content off the top and the six left
over divide what remains as `2fr 1fr 2fr 2fr 1fr 2fr`. On a 50-row screen that is eight
rows for FLEET, eight for the PLAN, eight for OPEN PRs, nine for ISSUES — and **four**
for CLAIMED and four for REVIEW QUEUE. Widening the pane with `C-q >` made the cells
longer and the panels no taller, so the answer to "I cannot see enough of this" was to
look at something else.

Above 157 columns the six panels below them go **two across**, and what that buys is
height. Every one is between one and a half and three times taller, and none of it was
taken from another panel:

| panel | one column | two |
|---|---|---|
| FLEET | 8 | 12 |
| CLAIMED | 4 | 12 |
| PLANS | 8 | 19 |
| OPEN PRs | 8 | 12 |
| REVIEW QUEUE | 4 | 12 |
| ISSUES | 9 | 19 |

Rows on a 200×50 pane, panel including its title. DIALS and SEATS are their content in
either layout, so they are unchanged and span both columns — see below.

**157 is not a taste.** 78 columns is what one of these tables wants before it wraps —
it is `QB_SEATS_DASH_SIZE`'s default, quoted from `qb-seats` — so two of them side by
side plus the gutter between is the narrowest pane on which the second column is not
paid for out of the first. Below it nothing changes at all: the pane `qb-seats` splits
off comes out exactly as it did before this existed. `QB_DASH_WIDE` moves the threshold,
and a value that is not a positive number of columns is ignored rather than fatal — a
dashboard that refused to start over a typo in a tuning variable would be trading the
panel you are trying to read for the knob you were adjusting.

### DIALS and SEATS span both columns, and REVIEW QUEUE moves

Both are their content in either layout, so a column of their own would buy them nothing
and cost the panel beside them half its width. SEATS keeps the ＋ where it can be found,
which is the one thing that panel has to do — it is the only way to add a seat with the
mouse, and it has already fallen off the bottom of a screen once. DIALS keeps the place
#477 gave it, at the top: it is the configuration every panel below is running under, and
a setting in force is not something to go looking for in the second column.

The other placement is the one CSS could not do. A grid fills row by row in DOM order, so
the order that puts REVIEW QUEUE **directly under OPEN PRs** — #273's arrangement, where
one panel says a PR exists and CI is green and the next says whether anybody has reviewed
it — lays them into different rows and different columns the moment there are two. So
`relayout` moves PLANS down one when it goes wide: `under` becomes `beside`, the queue
keeps the panel it exists to answer, and PLANS pairs with the ISSUES its items point at.
It moves back on the way down, exactly, because `>` and `<` nudge by eight columns and
crossing the threshold twice in a minute is an ordinary afternoon.

Textual has no media query, so the switch is a class set from `on_resize` — and the
panels are reordered with `move_child` rather than remounted, because a DataTable carries
a cursor, a scroll offset and the row keys every click resolves through, and a pane
getting wider is not news worth losing your place over.

### The caps bar was a resize behind

Found while wiring the threshold up, and it had been true since the caps line was first
sized to the pane. `on_resize` runs **before** the app's own size is updated, so the
`self.size.width` it read was the width the pane had before the resize being handled.
On the caps bar that was invisible — dragging a border emits a stream of resizes and the
last-but-one is near enough. On a layout threshold it would not have been: crossing it
once and stopping would have left the pane in the wrong layout indefinitely. Both now
take the width off the event.
