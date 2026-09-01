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
published harness can act on. And `QUARTERBACK_TOKEN_REFRESH_CMD` gains the case that
now exists in the wild: a bearer a human copied onto a box has nothing behind it to
re-mint from, so the refresh is deliberately left unset and a 401 stands until somebody
acts.

Comments only — no behaviour change.
