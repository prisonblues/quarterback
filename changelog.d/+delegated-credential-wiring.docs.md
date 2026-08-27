# the credential that lets an agent apply an order now says how to deploy it

`DEPLOY.md` gave `HUMAN_TOKENS` a numbered recipe naming where each half lives — the secret,
the board's env var and its vault ref, the client's command — and gave `ELEVATED_TOKENS` a
description of what it authorises and nothing about putting it in place. So the one credential
whose entire purpose is letting an agent APPLY an order a person asked for was the one with no
instructions, and it duly sat unwired on this fleet while both its secrets were already minted
and matching in the vault.

It now carries the parallel block: mint per machine and why per machine, the board half and its
ref, the client half and its command, and that the machine name is one string that has to agree
at both ends or the comparison fails against an entry that is not there.

It also says why not to route around it when it is missing. An agent that cannot apply an order
can still reach `human()` if it can read `HUMAN_EDGE_SECRET` off the host, and that authors the
write as the person, with `rank_source: "ordered"` — indistinguishable from a sequence they
typed. That is the confusion this credential replaced the session-lending design to end.

The post-deploy checklist gains the check that proves it end to end, which is not that the
reorder succeeded but that the rows read `derived`. Success is not the signal; the attribution
is.
