# qb-env stops describing a second board that no longer exists

`harness/bin/qb-env`'s header explained why an unset `QUARTERBACK_BASE_URL` is an
error by pointing at a specific fleet fact: that there were two deliberately disjoint
boards, a personal one and a second on an employer-owned island, and that a default
would quietly send the island's agents to the personal board. That fleet now runs one
board again — both work hosts joined the personal one — so the justification named a
topology that had gone away.

The rule it justifies is unchanged, and the rewrite says why rather than just dropping
the stale half: the rule was never "there are two boards", it is that a machine must be
TOLD which board it belongs to. A default is a guess about deployment topology and is
equally wrong when it happens to guess right — it only stops being visibly wrong. That
distinction is the thing worth leaving behind for whoever reads this next and reasons
"there's only one board now, so the default is safe again".

Two contract lines were stale in the same way. `QUARTERBACK_TOKEN_CMD` described the
per-site sources by naming the hosts (`the token file on daedalus, an ssh fetch on
sisyphus`) rather than the shapes; it now names the shapes, which is what a reader of a
published harness can act on.

`QUARTERBACK_TOKEN_REFRESH_CMD` gains the case that now exists in the wild — a bearer a
human maintains as a file — and a first draft of this documented it wrongly, which is
worth recording since the wrong version is the intuitive one. Leaving the refresh unset
does NOT mean a 401 stands until somebody intervenes: the unset value defaults to
`TOKEN_CMD`, so the 401 re-reads the file. Unchanged, and the refresh fails, correctly,
because there is nothing new to present. Re-dropped since the session began, and the
new bearer is picked up in place with no restart. The real limit is "cannot be re-minted
without a human", not "cannot be refreshed", and the two are easy to conflate.

One further correction, to a claim that predates this change. The header said
"Environment beats the config file throughout". It does for `QUARTERBACK_TOKEN` and an
explicit `QUARTERBACK_AGENT`, both resolved through functions that check first — and it
does not for the plain assignments, because `qb_load_config` sources the file and a
sourced `VAR=value` overwrites the caller's export. So a one-shot
`QUARTERBACK_BASE_URL=... qb ...` is silently ignored on any host that has a config.
Verified rather than reasoned about. The header now says which half is true; the fix is
a behaviour change for every consumer (`: "${VAR:=...}"` in the rendered config flips
who wins) and wants its own decision rather than riding along in a docs commit.

Comments only — no behaviour change.
