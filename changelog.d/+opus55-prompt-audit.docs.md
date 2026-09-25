# the commands and hook text say what is true now, and say it once

The slash commands and the text the hooks inject had been written a model generation or two
ago and patched by accretion since. On Claude Opus 5.5, which follows instructions literally,
that showed up as contradictions rather than as noise: `/fix-issue` and `/review-pr` forbade
`git stash` and then said "stash the fix" thirty lines later, the red/green step restored files
with a `git checkout <ref> --` that dcg refuses, `/panel`'s post flag read as the inverse of its
own default, `/wt` promised a `cd` that does not persist and pointed at `~/.local/bin`, and
`/loops` said merge is always a human step beside a table saying `/fix-and-land` merges. The
command descriptions never reached Claude Code at all: it reads `description:` from YAML
frontmatter, so the `@description` line every command carried left the skill list showing
titles only.

### What changed

Every command declares `description:` / `argument-hint:` in frontmatter, and the wiring test
pins that form. The contradictions above are resolved in favour of the safer, working rule. The
long conditional material — the `/review-pr` fixer brief, the rare round-stop branches of
`/panel-review-pr`, and the `/fix-and-land` hazards — moved to `~/.claude/loops/docs/`, read on
demand, so the command bodies carry only what every run needs. Dated incident stories ("it
happened here on…", "most recently tonight") are gone from the hook refusals, whose reasons
stay; the sync advisory no longer advises a stash or orders a push, and an unchanged advisory
is not re-injected. `quarterback-workflow.md` describes the board's designated `machine/name`
identity.

### Loop prompts

The claude reviewer seat now honours its configured `effort`; the judge and epic triage ask
`claude -p` for schema-validated output with `--json-schema` instead of scraping JSON out of
prose, and triage runs at low effort. The lander fixer is handed the failing checks' log it was
told to investigate.
