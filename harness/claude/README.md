# `harness/claude/` — the Claude Code configuration the harness owns

Two files, and neither is code:

- **`settings-fragment.json`** — the lifecycle hook entries that make a host a board client.
  `qb-claude-setup` merges it into `~/.claude/settings.json`; `qb-claude-setup
  --print-fragment` prints it with paths resolved, for a consumer who declares that file
  themselves.
- **`quarterback-workflow.md`** — how to use the board, addressed to the model rather than to
  the machine. `programs.quarterback-harness.claude.workflowDoc` links it into `~/.claude` and
  the wiring @imports it from `~/.claude/CLAUDE.md`.

## Why the hooks are data and not a jq expression

They were an expression, in a script in a consumer's personal config, and it wired three of
the seven events (#230). The other four existed only because that same consumer's
hand-maintained `settings.json` happened to carry them — so the script was not the wiring, it
was half the wiring, and nothing anywhere compared the two halves. A host that ran only the
script got no ask courier, no publish-on-push, no sync advice and no sub-agent records, and
reported no error of any kind.

As a data file the set is countable, and `harness/tests/test_claude_wiring.py` counts it in both
directions against `qb-hook`'s own dispatch switch: an event the hook handles and the fragment
does not wire is dead code that reads like a feature, and an event wired with no arm to receive
it spawns a shell per occurrence to fall through a `case`.

## `@QB_HOOK@`

The fragment ships *inside* the package whose path it has to name, so the path cannot be in the
file — a fragment that hardcoded one would wire every consumer to whichever machine's layout it
was written on, which is precisely what a committed `/home/rich/.local/bin/qb-hook` was.
`qb-claude-setup` substitutes the resolved sibling of itself, which is what puts the hook, the
wiring and the board client on one pin.

## The shape is meant to be stable

Adding a board feature should not add an entry here. `PostToolUse` already matches every tool
and `UserPromptSubmit` already fires on every turn, so a new mechanism has an event to hang off
without touching a consumer's `settings.json` — which matters because that file is the one part
of the install nix cannot own, and every change to its shape is a migration for somebody. New
events belong here only when Claude Code grows a lifecycle moment the hook genuinely cannot
observe from the ones it already receives.
