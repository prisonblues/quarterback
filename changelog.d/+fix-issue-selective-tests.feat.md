# /fix-issue runs the tests a change can reach, and the full suite once at integration

Step 7 told every fixer to run the full suite, plus the whole DB-backed suite for any
DB-facing change, before it could push. In a repo whose full suite takes ~20 minutes and
whose DB suite takes ~50, that dominated each fix. When several fixers fanned out in
parallel on one box, each one ran both, often twice after review fixes, and contended for
the same cores. The cross-PR interactions those runs were meant to catch only exist once
the PRs are merged together, so no per-branch run could see them anyway.

Step 7 now runs the project's **selective** target: an affected-tests run such as
pytest-testmon behind `make test`, `--changed` or `nx affected`, plus the new test files by
path. For a DB-facing change it runs the DB tests for the affected area. The full suite
runs only when there is no selection mechanism, when the selective run itself reports it
fell back to everything, or when the change is genuinely cross-cutting. A fix bound for an
integration branch (`--base integration/*`, an epic or fan-out driver, or told to defer)
skips the full and DB suites entirely, and they run once when that branch is promoted to
the default branch. The fixer also points its selection at the PR's base: diffed against
the default branch, a branch forked from an integration branch counts every commit the
integration branch carries as changed, and the "selective" run took ~55k lexray tests.
CI, the nightly and the protected-branch pre-push hook still run everything before
anything reaches the default branch, so this changes when the full suite runs, not
whether.

`fix-issue-here.md` carries the same rule in its short form, and the `/review-pr` fixer
brief gets the base-pointing rule and runs only the affected area's DB tests.
