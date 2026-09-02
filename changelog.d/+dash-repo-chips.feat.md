# a chip bar, so the fleet can be narrowed to one repo

Sixteen agents across three repos is a list you read rather than one you scan, and the
dashboard had no way to narrow it. `s` is binary — this screen's repos, or every repo — and
`--repo` is a command-line flag you cannot reach from a running dashboard.

So: one line of clickable chips above the tables, a chip per repo the live fleet is actually
in. Clicking one narrows AGENTS to it; **clicking the same chip again clears the filter**, so
one chip is both the on and the off switch. A separate `clear` chip is one more thing to
find, and in a pane narrow enough to clip the bar it is the one that would get clipped —
leaving a filter set and no visible way to unset it.

### What it does not do, and why

**It does not filter WORK.** `gh` is only ever asked about the repos this dashboard watches,
so filtering the PR and issue rows would show all of them or none, and neither is a filter.
The chips narrow the fleet; `s` is still what scopes the work.

**The unfiltered count stays on the title** — `AGENTS · 3 of 16 · lexray`. A filter has to
read as a filter rather than as the fleet having shrunk, and on a short pane whose bar has
scrolled out of view the tally is the only thing left saying so.

**The bar hides itself below two repos.** One chip is not a choice, and a line of a
78-column pane is worth more than a control that cannot change what you are looking at. It
follows that the bar is mostly a fleet-wide-scope thing, which is the scope that needed it.

**A filter whose repo goes quiet is dropped.** The last agent in `lexray` exits, its chip
stops being drawn — and a filter that survived that is an empty table with no visible
control to clear it.

Recovered from `feat/qb-dash-buttons`, where it was written on 2026-08-18 and never landed.
That branch is ~820 commits behind and predates the two-table merge (#589), so this is the
idea re-applied rather than a cherry-pick: the chips are a one-row `ClickTable` because
every other clickable thing here is a table cell and `ClickTable` already reports which
column was hit, where the original drew its own bar.
