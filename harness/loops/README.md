# Agent coding loops — engine

Agent-driven PR loops: a dependabot lander, a multi-reviewer panel, and an epic
driver. Originally designed in selfhost `issues/open/117-feature-agent-coding-loops.md`,
which remains the reference for the *why*. **The gates are the product.**

> **Where this lives.** Source: `harness/loops/` in the quarterback repo, which is
> canonical — it was previously vendored into a personal NixOS config and two other
> repos, and those copies drifted. Deployed: the flake's home-manager module ships this
> directory to **`~/.claude/loops/`**, next to the `/loops`, `/panel`,
> `/panel-review-pr`, `/lander`, `/epic` and `/fix-and-land` skills in
> `~/.claude/commands/`. No repo checkout is needed to run any of it — see
> [../README.md](../README.md) for both install paths.
>
> `~/.claude/loops/` is a **read-only nix-store symlink**, so epic run state goes to
> `~/.local/state/loops/` (override with `LOOPS_STATE_DIR`). The scripts are invoked
> with plain `python3`, not `uv run` — there is no project to resolve from here, and
> they import only the stdlib (`certifi` is used when present, with a system-CA
> fallback at every call site).

## Configuration — `.harness-rules`, in the repo

Each repo describes itself with a `.harness-rules` file in its root, the same
convention `create-worktree` already uses for `.worktree.json`. **Every key is
optional**; anything omitted falls back to `DEFAULTS` in `harness_rules.py`, which
is the safe end of every switch — no auto-merge, no unattended loop, edit-only
headless agents. A repo with no rules file at all still works.

This replaced a central `config.json` in the fleet config. That file was a registry
of personal repos that had to be enrolled by hand before anything would run; the
plumbing it carried (`path`, `github`, `default_branch`) was derivable from the
checkout anyway, and three documented fields (`ci_gate`, `worktree_isolation`,
`consensus_threshold`) were read by no code at all. Dropping it is what lets the
read-only commands run in **any** repo — and what lets this engine ship to the work
host, since it no longer names anything personal.

Inspect what a repo resolves to:

```bash
python3 ~/.claude/loops/harness_rules.py                 # cwd's repo, one line
python3 ~/.claude/loops/harness_rules.py --json          # the full merged config
python3 ~/.claude/loops/harness_rules.py --discover      # repos shipping a rules file
```

### Which ref the rules are read from

This is the one security-relevant choice in the design.

| Mode | Rules come from | Used by |
|---|---|---|
| interactive | the working tree | `/panel`, `/epic`, `/lander` — anything you typed |
| unattended | `git show origin/<default>:.harness-rules` | the systemd timer (`HARNESS_UNATTENDED=1`) |

An in-tree rules file means repo content influences the harness. For the flows a
human triggers that is not a new door: `/epic`'s executor already runs with
`bypassPermissions` and a full shell, so it could run `gh pr merge` itself without
touching any config, and anyone able to commit to a default branch can already put
arbitrary code in the build.

The exception is the lander's red-CI fixer. That agent is deliberately **edit-only**
(`--permission-mode acceptEdits`, no bash — the loop owns commit+push and the merge
decision is Python, not an agent), and it operates on an **upstream-authored
dependabot branch**. Reading rules from that branch would let a poisoned PR rewrite
the policy governing its own review — reaching something the agent otherwise cannot.
Hence the split. A human at the keyboard *is* the authorization; unattended runs only
honour rules already merged to the default branch.

### Schema

Detected from the checkout, **not settable** here: `path`, `github`, `default_branch`
(and `name`, which defaults to the directory name).

| Key | Meaning |
|---|---|
| `enabled` | Master switch — `false` means the loops skip this repo. |
| `auto_merge` | `none` \| `dependabot_patch_minor` \| `all_green` — see below. |
| `headless_permission_mode` | `claude -p` mode for headless agents. `acceptEdits` = edit-only (safe). `/epic`'s executor runs `/fix-issue` + `/review-pr`, which need git+gh, so it needs `bypassPermissions`. |
| `executor_pr_base` | Base branch the issue→PR executor opens PRs against. Defaults to the default branch. **Only used when *creating* PRs** — see "Branch base". |
| `dependabot_author` | Author filter for the lander (`app/dependabot`). |
| `reviewers` | Which reviewers run — see below. |
| `review_panel.skip_title_patterns` | Regexes for PRs not worth LLM review (merge/promote/release/format-the-world). These drove a cost blow-up in #117 — one release-merge ≈ $750. |
| `review_panel.judge_model` | Claude model for the master judge (`""` = default). |
| `loops` | `dependabot_lander` / `stacked_driver` / `issue_executor` — which loops may run. |
| `epic` | Epic-driver settings — see below. |

`auto_merge`:
- `none` — never auto-merge; always stop for a human.
- `dependabot_patch_minor` — auto-merge only dependabot patch/minor on green CI. **(default)**
- `all_green` — auto-merge any PR once gates pass. Not recommended for a repo with users.

`reviewers` — each block has `enabled`; LLM reviewers also take `model`.
- `claude.model` — set to a **different** model than the PR author for cheap diversity.
  Same-model self-review is the weak case (#117).
- `codex` — different-vendor LLM reviewer (the high-value diversity add).
  `model` and `effort` (`low|medium|high|xhigh|max|ultra`) both default to empty,
  meaning *the codex CLI's own defaults*. Pin them per repo to experiment, e.g.
  `"codex": {"enabled": true, "model": "gpt-5.6-luna", "effort": "high"}`.
  Unlike `opus` (a floating alias), codex slugs are versioned build names that
  get retired, and a slug the **installed** CLI is too old for is refused by the
  API — so check `codex --version` before pinning a new one. That failure is
  loud rather than silent: the report names the model each reviewer used and
  flags a lost reviewer next to the reviewer list, not in a footnote.
- `sonarqube` — deterministic static-analysis **hard gate**. `project_key`,
  `organization` and `host` are non-secret and belong here; the token does not.

`review_panel.max_diff_chars` (default 60,000) — how much of the diff each model
is given. Override per reviewer with `reviewers.<name>.max_diff_chars` and for
the master with `review_panel.judge_max_diff_chars`; both inherit the panel value
when unset. It is per-model because one number was standing in for several
different context windows. Any positive value is honoured — there is no lower
sanity bound, deliberately; what surfaces a too-small budget is that the report
names **which** reviewers were truncated and at what budget (worth knowing when
two reviewers disagree and only one of them saw the whole change). Only a value
that cannot be a budget at all — not a number, or `<= 0` — falls back to the
inherited one, with a ⚠️ config line in the report saying so.

### The SonarQube token

Resolved in this order, first hit wins:

| # | Source | Where it comes from |
|---|---|---|
| 1 | `$SONARQUBE_TOKEN` (the var named by `token_env`) | a login-time export (zeus does this) |
| 2 | the repo's **`.env`** | **the work-machine source** — no 1Password/sops there |
| 3 | `~/.cache/loops/sonar-<key>.token` (`0600`) | write-through cache of the `op` read |
| 4 | `op read` of `token_op_ref` | needs `op signin` once, then the cache serves |

**Never commit a token value.** `.env` must be gitignored — if it is committed,
the panel prints a loud warning on stderr and tells you to rotate, but it will
still use the token rather than failing your run.

`.env` sits **below** the real environment variable, not above it. An exported
token is a deliberate override, and a stale `.env` shadowing it would surface as
an unexplained 401 from SonarCloud rather than an obvious misconfiguration.
Nothing is lost on a work machine, where no such export exists and resolution
falls straight through to `.env`. (Same default as python-dotenv's
`override=False`.)

`.env` is read from the **working tree** in every mode, unlike `.harness-rules`.
That's not an exception to the two-ref rule: `.env` is gitignored, so it exists
on no branch and a PR cannot introduce one.

`epic`:
- `landing` — `auto` suggests integration vs multi from coupling signals; `integration`
  = one epic branch → one PR; `multi` = a PR per sub-issue.
- `sub_pr_merge` — integration only. `auto` ff-merges each green sub-PR into the epic
  branch; `gate` holds each at a human merge.
- `auto_finish` — on a `/fix-issue` that pushed nothing, commit+push salvaged work
  rather than failing the issue.
- `executor_worktree_args` — extra flags for `create-worktree` (e.g. `["--no-docker"]`).
- `min_free_mb` — preflight warns below this `MemAvailable`.
- `migrations_dir` — where alembic revisions live, for the linear-heads guard.
  **Leave it alone in a repo without alembic.** The default doesn't exist there, so the
  guard returns `None` and no-ops. Setting it to `""` breaks that: `Path(repo)/""` *is*
  the repo root, so an empty value makes the guard think migrations exist.

## Branch base — the one subtle rule

Repos are not uniform: dependabot may target `main` while features land on `test`.

- **Acting on an existing PR** (lander, stacked driver, panel): read `baseRefName`
  from the PR. Never reset it.
- **Creating a new PR** (issue executor): use `executor_pr_base`.

## Loop A — dependabot lander (`lander.py`)

`python3 ~/.claude/loops/lander.py` (dry run) / `--execute` to act. Defaults to the
cwd's repo; `--repo` takes a path or a name under `~/source`.

- **Green patch/minor → squash-merge** (within `auto_merge` policy).
- **Security → escalate** — left for a human; never auto-merged.
- **Red CI → fix-in-worktree:** a plain `git worktree` on the *existing* dependabot
  branch (not `create-worktree` — the gate is CI, not a local app run), a headless
  edit-only `claude -p` fixes the bump breakage, and the loop pushes back so CI
  re-runs. Removal is in a `finally`. **Idempotent:** if the branch tip is no longer
  a dependabot commit, a fix was already attempted — left for a human.

## Reviewer panel (`panel.py`)

`python3 ~/.claude/loops/panel.py --pr <n>` (report) / `--post` (also comment on the
PR) / `--json` (findings as JSON, no report) / `--round <r> --max-rounds <N> --baseline
<earlier round's --json-file>` (a re-review that knows what the earlier rounds raised,
and where it sits in the caller's cycle).

Read-only, so it runs in **any** repo — an unconfigured one just uses the defaults.

- Runs the repo's **enabled** reviewers in parallel over the PR diff: SonarQube (HARD
  quality-gate pass/fail), Claude (SOFT, read-only), Codex (SOFT, different vendor).
- **Skip patterns:** PRs matching `skip_title_patterns` are skipped entirely.
  Otherwise all enabled reviewers run — no diff-size de-minimis.
- **Master judgment, no consensus gate:** findings are deduped (file + nearby line)
  then a master reviewer judges each on its merits. A real defect flagged by only ONE
  reviewer is still fixed — agreement shows as a `⋆consensus` confidence marker, never
  a filter. Only clear false positives are dismissed, with a recorded reason. If no
  judge is available, nothing is suppressed.
- Reviewers whose prerequisites are missing are reported **SKIPPED**, not failed.
- **Reviewers declare their own coverage.** Each returns `could_not_assess` (areas it
  could not judge — a file the diff omits, a runtime behaviour) and can mark a finding
  `needs_rereview` (fixing it takes a structural change whose result should be read
  again). Both are *observations*: reviewers are never asked to forecast whether
  another round is needed, because that asks a model to predict findings it has not
  made — and one that silently produced nothing would answer "no" with total
  confidence. Truncation is measured, not asked for, since a truncated reviewer is the
  one party that cannot notice it. A bare findings array (any older reviewer) still
  parses and simply declares nothing.
- **Rounds are mechanical.** `--round`/`--baseline` make each run say which findings no
  earlier round raised; `round_stop` in the payload then says go-again (something new,
  or a P1/P2 still confirmed) or stop (dry / round cap), and whether stopping was
  *convergence*. The declarations never extend the loop — a truncated reviewer is
  truncated again next round — they only stop a broken round being reported as clean.
- **`--max-rounds N` is the CALLER's cap**, not a loop panel.py runs: it is the only
  input that tells a round which stopped because it was done from one which stopped
  because it ran out, and `/panel-review-pr` passes it on every invocation. Its flag is
  spelled `--rounds N` on the slash command and `--max-rounds N` here — same number, and
  `--round <r>` (singular) is a different thing entirely: which round THIS run is.
  A run given none of the three is a single review and says nothing about rounds.
  A `--round` past `--max-rounds` is rejected rather than recorded.
- **The `/panel` skill (default = fix)** consumes `--json`, checks out the PR branch in
  an isolated worktree, fixes confirmed findings, runs lint + unit tests (**aborts the
  commit on failure**), makes one commit, pushes, and comments the summary.
  `panel.py` itself stays read-only — the fix/verify/commit lives in the skill, and so
  does the loop: `/panel-review-pr` runs panel → fix → panel (2 rounds by default,
  `--rounds N`), so the fixer's own commit is read by somebody.

## Epic driver (`epic.py`)

`python3 ~/.claude/loops/epic.py --epic <n>` (plan) / `--execute` (run the pipeline).
Per sub-issue: create worktree → `/fix-issue` → CI → panel → `/review-pr` → stop at
the human merge. Merge is never automatic. Run state in `~/.local/state/loops/`, so a
killed run resumes instead of re-deriving from GitHub.

## Deployment

- **The flake's home-manager module** (`harness/hm-module.nix`) — ships this directory to
  `~/.claude/loops` and the wrapping skills to `~/.claude/commands`. It deliberately does
  NOT pull in `python3`/`jq`/`gh`/`codex`: those are the host's tools, and pinning them
  here would point the loops at a different toolchain than the machine they run on.
  See [../README.md](../README.md#requirements).
- **`run-loop.sh`** — the timer entrypoint. `flock` single-flight; **discovers** repos
  (any checkout under `$HARNESS_REPO_ROOT`, default `~/source`, shipping a rules file)
  rather than reading a list; sets `HARNESS_UNATTENDED=1`; appends to a daily log in
  `$LOOPS_LOG_DIR` (default `~/loops-logs`). **REPORT-ONLY by default** — set
  `LOOPS_EXECUTE=1` only after a supervised hand-run. Adding a repo to the sweep is a
  commit in *that repo*, not an edit here.
- **`systemd/loops-lander.{service,timer}`** — reference spec only. The live units are
  `systemd.user.{services,timers}.coding-loops` in `home/rich-workstation.nix`
  (ExecStart → `%h/.claude/loops/run-loop.sh`, `OnUnitActiveSec=10min`,
  `Persistent=false`). Activate with a `rebuild`.

**Cutover to acting:** keep the timer report-only, watch a few daily logs, hand-run one
`lander.py --execute` on a single PR, *then* set `LOOPS_EXECUTE=1` in the service env.

## Gate model (recap from #117)

- **Hard gates** (block merge): CI (`gh pr checks`) + the SonarQube quality gate.
- **Soft gates** (advisory): Claude + Codex reviewers. Never a lone-LLM hard block.
- Auto-merge only within the `auto_merge` policy; everything else → human at the
  merge button.
