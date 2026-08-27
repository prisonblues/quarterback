# hiding a pane on one screen parked it in another screen's window list

`break-pane` with no `-t` puts the new window in the **client's current** session, not in the
one the source pane lives in. Every pane this harness takes out of a row went through such a
call — `d` for the dash, `t` for the tape, the tape's step-aside — so on a server running two
screens, hiding a pane on the screen you were *not* looking at put it in the other screen's
window list.

Nothing downstream could then find it. `pane_exists` and the whole restore path search
`list-panes -s -t "$SID"`, which is scoped to the session, so the pane was at once alive,
stranded in a window nobody expected it in, and reported as **gone** — with no way back
through the script that moved it. The recorded `@qb_hidden_dash` still named it, so the next
press cleared the state and said "the hidden dash is gone — nothing to bring back" about a
pane sitting two windows away.

It has been there since the toggles shipped and no test could see it: with one session on the
server the client's current session and the pane's session are the same, so the missing `-t`
is invisible. It surfaced the first time a throwaway screen was built beside a real one on a
developer's box — the dash landed in `seats-quarterback:qb-dash` while `dash-wide` reported
it missing, and recovering it took a hand-written `join-pane`.

All three breaks now name `-t "$SID:"`. The regression test builds **two** screens and hides
a pane on the first, because one screen cannot fail this way — parametrised over `dash`,
`expand` and `tape`, since all three shared the call and all three shared the bug.
