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
fell back to everything, or when the change is genuinely cross-cutting. A fixer that is
one of several parallel fixes skips the full and DB suites entirely, and they run once on
the integration branch. CI and the protected-branch pre-push hook still run everything
before anything lands, so this changes when the full suite runs, not whether.

`fix-issue-here.md` carries the same rule in its short form.
