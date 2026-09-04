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

`open_url` is stubbed now, at `open_url` rather than lower so `open_pr` still runs and the URL
it builds is still the thing under test. The assertion pins that URL exactly — the wrong-repo
row must open *this* row's PR and start no review, and the dashboard's own row must start the
review and open nothing. Matching the repository as a substring, which is what the old
assertion did, accepts `/issues/42` and every other URL that merely mentions the repo, and a
number taken from the wrong record still reads as the right repository. That confusion is what
the whole test is about.

A test in this file reaching the real browser is now refused rather than remembered. The
module docstring already promised "the browser and tmux calls stubbed"; an autouse fixture
enforces it, failing any test that invokes `xdg-open` and naming what it tried to open.
`Popen` is a pass-through for everything else, because the harness legitimately runs `git` and
a private tmux server.
