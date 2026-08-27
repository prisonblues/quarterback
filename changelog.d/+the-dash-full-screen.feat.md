# the dash full screen, from the keyboard or the ⛶

Two columns are only worth having if the pane can be made wide, and 78 columns down the
right of a seat screen never will be. `z` inside the dash, `C-q z` from anywhere on the
screen, and a new ⛶ on the top line all reach one verb — `qb-seat-key expand` — which breaks
the dash out into a window of its own and puts it back on the next press. The dash notices
the width and lays its panels out two across; nothing had to tell it to.

### Why `break-pane` and not the two obvious alternatives

**`resize-pane -Z`** was the first answer and is the wrong one. Zoom is a property of the
window and tmux drops it on any layout change — and this screen makes them constantly:
`select-layout -E` when a seat is closed, and the `window-resized` hook reasserting
`@qb_dash_width` on every client attach. A dash zoomed to read would pop back to 78 columns
the moment somebody attached a phone, with nothing on screen to say why.

**`display-popup` running a second dashboard** is a second board poll, a second `gh` poll,
and a cold start whose ISSUES panel says "waiting for gh" for up to a minute. `break-pane`
moves the pane the process is already in — the same argument that made hiding a pane a
break-and-rejoin rather than a kill-and-respawn.

So this is `d`'s move without the `-d` that parks the pane where nobody is looking, and it
inherits everything that was hard about that one. Including the rule that the widths are
recorded **before** the break, which bites differently here and cost a test to find:
`hide_pane` is handed its size by a caller that read it first, while `expand_dash` does its
own break, so reading afterwards is one line away and looks identical. It is not — after the
break the pane fills its new window, so the recorded size is the whole terminal, and the join
back asks for a 240-column pane inside a 240-column window and fails with `create pane
failed: pane too small`.

### Two toggles over three states

`d` means "in the row or not". `z` means "full screen or not".

| | `d` | `z` |
|---|---|---|
| in the row | → hidden | → expanded |
| hidden | → in the row | → **expanded** |
| expanded | → in the row | → in the row |

The middle row is the crossing that had to be decided. Somebody pressing `z` on a hidden
dash is asking for a dash they can read, and a hidden one is one step from that rather than
in the wrong state for it — so it is shown rather than put back in its column, and rather
than refused. It is also the cheap direction: the pane is already alone in a window, so that
crossing is a rename and a `select-window` and no geometry moves at all. A `break-pane`
there would fail outright, having nothing to break.

Both routes out of the row record the same state and return through the same `restore_dash`,
so there is one way back however it left. `>` and `<` refuse while it is out and now say
which of the two states they are refusing for: "hidden" about a dash filling the screen in
front of you is the kind of wrong answer that makes somebody doubt the tool rather than the
state.

Two options carry that third state — `@qb_hidden_dash` is where the pane is parked and
`@qb_dash_expanded` is which of the two ways it got there — and they are cleared together
wherever either stops being true. Separately was worse than it sounds: closing the window an
expanded dash is sitting in is `C-q z` followed by the ordinary reflex of closing a window
you are done with, and the pane-is-gone branch dropped only the first. What was left was a
screen marked expanded with nothing recorded, so every later `z` took the expanded branch and
answered "nothing recorded to put back" — naming the marker's problem rather than the
screen's, which is that the dashboard died. Neither key could put it right, because that
marker is the only thing either of them reads. `restore_dash` returning empty-handed is now
taken as proof the marker is wrong, and it does not survive being disproved.

### The ⛶ is the first clickable widget on the top line

`#[range=…]` is honoured in `status-format` and nowhere else, which is the whole reason a
control can live on a status line — the seat bar's ✕ and ＋ have used it since it shipped.
This one goes on line 0 rather than on the bar, because every cell on the bar names a seat
and a control for the pane down the right-hand side would be the exception a reader has to
learn. It is not confirmed, unlike the ✕: nothing is killed, no process is touched, and the
same click puts it back.

Being the first widget on a line that never had one cost the mouse binding a rewrite.
`MouseDown1Status` gated on `#{==:#{mouse_status_line},#{@qb_bar}}` — true of the seat bar,
line 1, and of nothing else — so the ⛶ drew, registered its range, and fell through to
`switch-client -t =` on every click. Both halves had passed their own tests: the widget was
on the line, `qb-seat-click expand` did the thing, and what nobody owned was the line
between them. The binding decides on the SCREEN now and not on the line, which is the
question it always meant to ask: `status 2` and both `status-format` indices are ours, so
every range on either line of one of our screens is one we put there, and which widget was
hit is the range's job to say.

It implements nothing. `qb-seat-click` hands over to `qb-seat-key`, which is the same
delegation the `a`, `x` and digit keys make in the other direction, and for the same reason —
two copies of "break the dash out and record the widths to put it back with" is two places
for the geometry lore to drift, and the drift shows up as a screen nobody chose the shape of.

### One thing that needed no code

`qb-seats`' own `dash_pane` looks in `$SESSION_ID:seats` and nowhere else, so an expanded
dash is outside the resize hook's reach and cannot be shrunk back to 78 columns by an
attaching client. That is a property of where the pane went rather than a case anybody wrote.
