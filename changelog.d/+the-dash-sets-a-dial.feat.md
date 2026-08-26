# the dash sets a dial, on the credential of the person sitting at it

#477 made what is in force legible from a terminal and left the verb in the browser: `POST
/dials` takes `app.auth.human`, and a dashboard authenticates with the machine bearer token
every agent on the box holds — precisely the credential that gate exists to refuse. So the
panel read, and printed a URL.

The gate has not moved. What the dashboard has now is a credential: `qbdata.HumanClient`
presents a signed-in session to the **browser vhost**, so the person at the keyboard writes
as themselves and the board records `human/<user>` exactly as it does for the page.

The `✎` on a dial row opens an editor — **value**, **reason**, and **for** (`30m`, `4h`,
`7d`, or empty for a dial with no end) — with the dial's name fixed, because a dial is
identified by its name and an editable one would create a second dial rather than change the
one on screen. `ctrl+s` saves, `ctrl+x` clears it and hands the repo back its own default,
`esc` cancels. The last row of the panel sets a new one. What a write replaced comes back on
the detail line, because moving a dial without being told what it was is how one gets nudged
twice by two people who each believed they were starting from the default.

Three things it refuses before spending a request, each where the sentence can name the box
that was wrong rather than arriving as a 422 about a field nobody typed: a reason that is
blank, a duration that is not one (`soon`), and an expiry measured from the wrong clock — the
board's own `now` is on the wire, so a host whose clock is slow does not have its "in four
hours" refused as being in the past.

### What it costs, which is #479 and not a footnote

The session is readable by everything running as this user, so **"the dash can set a dial"
and "anything on this box can set a dial" are one fact**. That is the trade — open it wide
now, tighten later — and #479 carries the menu for narrowing it. It is also why the
delegated agent credential (`X-Agent-Elevated`, #480) is a different thing and stays narrow:
that one is for an agent acting unattended and names the two endpoints it may reach; this is
for the person at the keyboard.

### With no session, nothing changed

Which is every box until `QUARTERBACK_EDGE_COOKIE_CMD` ships. `why_not()` is asked once per
paint, the `✎` greys, the last row says why in place of the verb, and a click opens
`/dials/view` as before. A credential is a **command** rather than a value for the reason
`QUARTERBACK_TOKEN_CMD` is: a session in 1Password is one `op` can re-read when it goes
stale, and one that never sits in a file. It is resolved at the first write rather than at
startup, and re-read once when a write bounces on an expired session — a retry only for the
refusals a fresh session actually fixes, since retrying "the board refused this person" would
turn one refusal into two and take an `op` unlock prompt with it.
