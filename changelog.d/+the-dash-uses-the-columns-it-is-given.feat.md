# the dash uses the columns it is given

Seven panels have been sharing one column of a 78-column pane, and the arithmetic was
never going to work: SEATS takes its content off the top and the six left over divide
what remains as `2fr 1fr 2fr 2fr 1fr 2fr`. On a 50-row screen that is five rows for
FLEET, five for the PLAN, five for OPEN PRs, five for ISSUES — and **two** for CLAIMED
and two for REVIEW QUEUE. Widening the pane with `C-q >` made the cells longer and the
panels no taller, so the answer to "I cannot see enough of this" was to look at
something else.

Above 157 columns the panels now go **two across**, and what that buys is height. Every
panel is between two and five times taller, and none of it was taken from another panel:

| panel | one column | two |
|---|---|---|
| FLEET | 8 | 11 |
| CLAIMED | 5 | 11 |
| PLANS | 9 | 17 |
| OPEN PRs | 8 | 17 |
| REVIEW QUEUE | 5 | 17 |
| ISSUES | 9 | 17 |

Rows on a 200×50 pane, panel including its title.

**157 is not a taste.** 78 columns is what one of these tables wants before it wraps —
it is `QB_SEATS_DASH_SIZE`'s default, quoted from `qb-seats` — so two of them side by
side plus the gutter between is the narrowest pane on which the second column is not
paid for out of the first. Below it nothing changes at all: the pane `qb-seats` splits
off comes out exactly as it did before this existed. `QB_DASH_WIDE` moves the threshold,
and a value that is not a positive number of columns is ignored rather than fatal — a
dashboard that refused to start over a typo in a tuning variable would be trading the
panel you are trying to read for the knob you were adjusting.

### SEATS spans both columns, and REVIEW QUEUE moves

SEATS is its content in either layout and a second column would only put the ＋ somewhere
new, which is the one thing this panel has to keep: it is the only way to add a seat with
the mouse, and it has already fallen off the bottom of a screen once.

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
