# a premise brake in shadow still refused the fix at declaration time

#789 gave the two premise rungs a mode and wired it into `round_stop`. It stopped at the
round, which is the later of the two places both brakes are evaluated — and #84 and #491 both
say the earlier one is where it counts: at the round a stop is one whole fix pass and one
whole panel too late, while `panel.py --premise` refuses the patch before it is written.

`declare_premise` read no mode at all. So `escalate_modes.premise_repeated: shadow` shadowed
the round's stop and the fixer's declaration was still refused with exit 4 — a brake half in
shadow, which is worse than one not in shadow at all, because the operator has been told it
is recording.

The declaration now resolves the modes from the rules file it already loads and applies the
same `fired`-not-`over` discipline its four siblings use. Under `shadow` the occurrence is
recorded, the verdict is published beside the measurement (`would_escalate`,
`repeated_verdict`, `undecidable_verdict`), the report says the fix **would** have been
refused, and the command exits 0 — the fix is permitted. Under `enforce`, exit 4 exactly as
before, which is also what a repo naming no mode gets.

The board write moves with the exit code: a shadowed rung announces no `needs-human` row,
because a blocker parks the work as surely as an exit code does.

### Two round notes said the cycle had ended when it had not

The `config_notes` lines for a repeated and for an undecidable premise looped over
`round_stop`'s **record** lists, which are populated whatever the mode is, and stated that
the cycle ends here and a human answers the premise. Under shadow neither had happened — and
`--post` publishes those lines as a public pull-request comment. Both are gated on the
verdict blocks now, the way the `fix_injection` note twenty lines below them already was.
