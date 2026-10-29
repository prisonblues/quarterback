# fix-issue and review-pr no longer tell agents to run the whole test suite

`fix-issue`, `fix-issue-here` and the review-pr brief all said "Test — full suite". That overrode
repos whose own guidance is to run the selecting target (`make test`) and leave the full run to CI
and the nightly, so agents started the whole suite by reflex. On a shared CI box that took minutes
and gigabytes per worker, and runners were killed for it.

The step now points at the project's selecting target or the specific test files, and says to leave
the full run to CI where the repo separates the two.
