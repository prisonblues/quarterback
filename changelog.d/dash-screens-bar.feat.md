# the dash lists every seat screen on the server and switches to one on a click

`qb-seats` with no arguments reattaches to the screen for the repo you are standing in, so a
shell opened in another repo after a dropped link built a fresh screen and left the old one
running where nothing showed it. The dashboard is where you land either way, so it now draws a
one-row bar of every seat screen on the tmux server, the current one highlighted, with each
screen's seat count. Clicking another screen runs `qb-seats resume NAME` — a `switch-client`
inside tmux — using the `qb-seats` beside the `qb-seat-click` the screen was built with.

The bar is hidden below two screens, and outside tmux. It reads screens the way `qb-seats list`
does (a session with at least one `@qb_seat` pane), never by name.
