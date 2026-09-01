# a seat panel that cannot see is not a screen with no seats

`tmux_seats()` returned a bare `[]` for every outcome — outside tmux, tmux missing, tmux
refusing, and a screen that genuinely has no seats. So the dashboard could not tell a fact
about the machine from a fact about the screen, and it reported the one that sends you
looking in the wrong place: **"no seat screen on this server", printed beside a screen with
three seats in it**, with the `＋` refusing to add one. What had actually happened was an
audit shim on PATH ahead of the real tmux, hardcoding a profile path that had stopped
existing, so every tmux call on the box exited 127. The dashboard was blind, not empty.

It returns `(seats, error)` now and says which. The AGENTS title carries `tmux: <what went
wrong>` — the panel whose rows are missing, rather than a status line that scrolls away —
and the `＋` and `z` answer "cannot reach tmux (…) — the seat panel is blind, not empty"
instead of advising you to start a screen you are already sitting in. It is #244's rule
about a pane instead of a queue: being idle and being broken must not look alike.

### Outside tmux reports no error, on purpose

The first cut of this fix, written on a branch that never landed, called that one an error
too. Running the dashboard full-screen in a bare terminal is a first-class way to use it
rather than a degraded one, and an error there would put a permanent complaint on the panel
of every such run — which is precisely how the failures this change exists to surface would
get buried. No tmux around us is a screen we cannot see, not a machine that is broken.
