# Loops — overview & command reference

@description Explain the agent coding-loops system and list every way to run it.

The user wants to understand the loops system and what they can run. The loop engine ships with
**quarterback** (source: `harness/loops/` in that repo) and is installed to **`~/.claude/loops/`**;
these skills are global wrappers. No repo checkout is needed to run them — `~/.claude/loops/` is
always present once the harness is installed.

1. Read `~/.claude/loops/README.md`, and run `python3 ~/.claude/loops/harness_rules.py --json`
   in the repo at hand to show what it actually resolves to.
2. Explain to the user, concisely (don't dump the README — summarise):
   - **What it is:** agent-driven PR loops for the configured repos, run on **zeus**,
     opportunistically. The gates are the product.
   - **The pieces:** `lander.py` (dependabot), `panel.py` (multi-reviewer + master judge),
     `epic.py` (epic driver), `run-loop.sh` + systemd timer (unattended sweep).
   - **Gate model:** SonarCloud = the only HARD gate; Claude + Codex = soft findings; **master
     judgment, no consensus gate** (a real bug from one reviewer still gets fixed); **merge is
     always a human step** (except dependabot patch/minor).
   - **Per-repo config:** each repo carries its own `.harness-rules`; omitted keys fall back to
     safe built-in defaults, and a repo with no file works too. Tell the user what THIS repo
     resolves to (`harness_rules.py --json`) and which loops it has enabled.
3. List **every command**, both slash and CLI (CLI commands run from anywhere — the engine takes
   absolute paths and addresses each repo via `git -C` / `gh --repo`):

   | Slash | CLI | What |
   |-------|-----|------|
   | `/loops` | — | this overview |
   | `/panel <pr> [repo]` | `python3 ~/.claude/loops/panel.py --pr <pr> [--post]` | review a PR |
   | `/lander [repo]` | `python3 ~/.claude/loops/lander.py [--execute]` | dependabot sweep |
   | `/epic <n> [repo]` | `python3 ~/.claude/loops/epic.py --epic <n> [--execute]` | work an epic |
   | `/fix-and-land <issue> [repo]` | (skill — orchestrates /fix-issue + panel + /review-pr) | implement → review → **merge if confident** |
   | — | `~/.claude/loops/run-loop.sh` | unattended dependabot sweep (systemd timer) |

   `--repo` is optional everywhere — it defaults to the cwd's repo, and also accepts a path or a
   name under `~/source`. There is no central config: each repo carries a `.harness-rules` file,
   and one that has none falls back to safe defaults, so the read-only commands work anywhere.
   Epic run state is in `~/.local/state/loops/`.

   One rule worth stating: unattended runs (the timer) read `.harness-rules` from the repo's
   **default branch**, not the working tree, so an upstream PR branch can't rewrite the policy
   governing its own review. Interactive runs use the working tree, so your local edits apply
   immediately.

4. State current reality briefly: panel is live (Claude + Codex + master judge); SonarCloud gate is
   wired but dormant until the repo's CI publishes PR analysis; everything defaults to dry-run/report-only;
   auto-execute paths exist but should be trialled supervised first.

Keep it tight and tailored to what the repo at hand actually resolves to.
