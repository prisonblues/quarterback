# the dashboard stops shipping two of itself

`qb-dash` carried two renderers of the same two tables: a clickable Textual app and a plain
`rich` one that redrew in place, chosen by `--tui`. The plain one had been the default until
#426 and the fallback ever since, reached only on a box where `textual` would not import —
and a fallback nobody picks is still a second implementation of every panel, kept in step by
hand. `qbdata.py` says what that cost in its own comment: six pairs of near-identical
helpers, written twice because there were two callers that had drifted.

The plain renderer is gone. `qb-dash` runs the clickable one and nothing else, `qb-dash-tui`
is a name for the same thing, and `--tui` is accepted and ignored so muscle memory and an
installed `qb-seats` older than this change both still work.

### What went with it

`--once`, `--width` and `--interval` were the plain renderer's flags and had no caller
outside its own tests — nothing on the fleet piped a frame of the dashboard anywhere. There
is no longer a one-frame, non-interactive mode.

`qb-seats` no longer probes `qb-dash --can-tui` before building the dash pane, because there
is no longer a choice for the answer to inform. That also retires a trap the probe carried:
it resolved `qb-dash` on PATH a second time, so a checkout's `bin` ahead of the installed
profile could have the probe answer for one install while a different one did the running.
`--can-tui` still answers, for an older `qb-seats` that has not been rebuilt yet.

On a box where `textual` will not import there is nothing lesser left to fall back to, so
`qb-dash` now says so and exits 1 rather than quietly running something else. A packaged
harness carries its own interpreter, so that is a checkout's problem and `QB_DASH_PYTHON` is
its answer.

### A candidate list that no longer needs `$HOME` to exist

Found by a second-opinion review of this change, and older than it: `qb-dash` built its
interpreter candidates as a fixed array containing a bare `$HOME`. Under `set -u` that is not a
skipped candidate, it is the end of the script — `HOME: unbound variable`, at a line number,
where the dependency error should have been, with `python3` never tried although it might have
worked. `env -i`, a cron job and a systemd unit all define no HOME.

The list is now built by appending only the candidates that exist, which is how `qb-board`
builds the same list and for the reasons its own comment gives — including that this keeps
`set -u` happy about `$HOME`. That also retires the `case` that skipped the fixed list's
empties by naming them literally: a sentinel string stops matching the moment the expression
above it is edited, and then a bogus path is probed on every launch.
