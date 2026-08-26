# the dash sets a dial, on a key of its own — and Authelia is not in the path

#477 made what is in force legible from a terminal and left the verb in the
browser: `POST /dials` takes `app.auth.human`, and a dashboard authenticates with
the machine bearer token every agent on the box holds — precisely the credential
that gate exists to refuse. So the panel read, and printed a URL.

**The gate has not moved.** `human()` gains a second METHOD, not a lower bar:
`HUMAN_TOKENS`, `name:secret` pairs in `API_TOKENS`' format, presented as
`X-Human-Key` to the **agent vhost**. `rich:<secret>` authors as `human/rich`,
which is the same identity the edge produces, by a different door.

Why a second door at all: the first one cannot serve a terminal. An edge session
expires on a wall clock, so anything built on one dies whenever it lapses and
stays dead until somebody re-mints it by hand. A key rotates when somebody
decides to rotate it. **Nothing here touches Authelia, so nothing here rotates
with it.**

The `✎` on a dial row opens an editor — **value**, **reason**, **for** (`30m`,
`4h`, `7d`, or empty for no end) — with the dial's name fixed, because a dial is
identified by its name and an editable one would create a second dial rather than
change the one on screen. `ctrl+s` saves, `ctrl+x` clears it and hands the repo
back its own default, `esc` cancels. The last row sets a new one. What a write
replaced comes back on the detail line: moving a dial without being told what it
was is how one gets nudged twice by two people who each believed they were
starting from the default.

Three refusals happen before a request is spent, each where the sentence can name
the box that was wrong rather than arriving as a 422 about a field nobody typed: a
blank reason, a duration that is not one, and an expiry measured from the wrong
clock — the board's own `now` is on the wire, so a slow host does not have its "in
four hours" refused as being in the past.

### What it costs, which is #479 and not a footnote

The key sits on a workstation, readable by the processes running there, so **an
agent that goes looking can find it and author as a person**. That is accepted
deliberately and it is narrower than what it replaced: the design considered
before it was a signed-in Authelia session, which is SSO for an entire estate.
This is per person and revoked by editing one line. Narrowing it further is
deferred, not overlooked — do not deploy it to unattended hosts that do not need
it.

It is also why the delegated **agent** credential (`X-Agent-Elevated`, #480) is a
different thing and stays narrow: that one is for an agent acting unattended and
names the two endpoints it may reach, and `/dials` is deliberately not among them.
Two credentials, two blast radii, and #479's exclusion survives intact — an
unattended agent still cannot set a dial.

### And the board records which door was used

`human/rich` is `human/rich` by either method — a person is one author however they arrived —
so the identity alone cannot tell an afternoon's browser write from a dashboard's, and the
dashboard's is the one carrying the residual above. `dial_settings` gains `set_via` and
`cleared_via` (`edge`, `key`, `dev`); `GET /dials` returns `set_via`; the page draws it as a
chip beside the author, and a dial row's detail line reads *"set by human/rich with a key"*.

`null` is **not recorded** — a row older than the column — and never "some other method".
Nothing is back-filled: a guess there would be the one value a reader must be able to distrust,
sitting in the field they consult to decide whether to trust the row.

### With no key, nothing changed

Which is every box until one is deployed. `why_not()` is asked once per paint, the
`✎` greys, the last row says why in place of the verb, and a click opens
`/dials/view` as before — #443's option (3), still carrying the fallback while
option (2) carries the verb.
