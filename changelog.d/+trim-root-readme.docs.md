# trim the root README down to current-facing documentation

The root README no longer carries the generated release-history list or the design archaeology
that made it several thousand lines long. It points at CHANGELOG.md for history, DEPLOY.md for
production deployment, and harness/README.md for the workflow tooling.

The release job now writes CHANGELOG.md only, plus the served-version files when the release
touches the board app. The retired README release-list renderer and its guard/tests are removed
so future releases do not grow the README back into a second changelog.
