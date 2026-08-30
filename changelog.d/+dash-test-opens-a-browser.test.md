# the dashboard's own test suite stops opening a browser tab on every run

`test_qb_dash.py` drove a click on the ⚖ of a PR belonging to a repo the dashboard only
watches, to pin that the icon refuses a paid review in the wrong repository. It stubbed the
two launchers that refusal is about — `run_in_pane` and `run_in_window` — but not
`open_url`, and a dim ⚖ falls through to "say what the row is", which for a PR row opens it.
So every run of the suite reached the real `open_url` and spent an `xdg-open` on
`prisonblues/lexray/pull/42`, a fixture number that is an *issue* over there, which GitHub
then redirected to `/issues/42`.

That was one genuine browser tab per suite run. Once the fleet ran the harness tests across
parallel worktrees it arrived on a person's screen every few minutes, from no window they
had opened and with nothing on the page to say what had asked for it.

It survived because the test still passed: the last assertion checks the row's repository is
named in the detail line, and the detail line after a real open reads
`opened https://github.com/prisonblues/lexray/pull/42` — so the browser launch was the thing
making the assertion green.

`open_url` is stubbed now, and the assertion accepts either answer a click can give: the row
explains itself in the detail line, or it opens the PR it names. Both are the icon not
swallowing the click, which is the property the test is for.
