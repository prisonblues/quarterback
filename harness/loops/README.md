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

Two conventions the resolver enforces so a rules file can be read like prose:

- **A key starting with `_` is a comment**, at any depth (`"_": "why this seat is
  on"`, `"_effort": "…"`). JSON has none of its own, these files exist to be argued
  with, and comments are stripped before anything reads the config — so they can
  never be mistaken for a setting.
- **A name nothing recognises is warned about on stderr and dropped**, loudly and
  non-fatally, at every level: the top level, each deep block, and the fields
  inside a reviewer. `reviewers.antigravty` would otherwise be a silent
  one-vendor-short panel, `reviewers.pi.enabld` a seat left off, `auto_merg` an
  auto-merge policy quietly on its default, and `loops.issue_executer` a loop
  switched off by a typo (those defaults are OFF); dropped as well as warned about, so the word
  "ignored" is true and no consumer iterating the resolved config sees a phantom
  seat. A warning rather than a hard exit, because a file shared across boxes may
  name a setting only a newer harness knows — and once per name per process, not
  once per `resolve_repo`, since epic resolves per run and shells out to panel.py
  which resolves again. A seat that was RENAMED says so (`gemini` →
  `antigravity`), which is the one unknown name a fleet file is likely to carry.

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
| `review_panel.judge_model` | Claude model for the master judge — `sonnet`, deliberately **not** `reviewers.claude.model`; see below. An explicit `""` is passed through as empty and lets the CLI pick, which is NOT the same as omitting the key (that gets the default). |
| `review_panel.ask_quorum` / `ask_threshold` | `--ask`'s tally rules: how many seats must have **answered** for the vote to mean anything, and how many must have said the same thing for it to be that answer. Both **2** — one seat agreeing with the agent that wrote the premise is not a challenge. A rule above the number of seats on the ask is warned about: it can never be met. |
| `review_panel.ask_max_context_chars` | Total `--context` material one ask may hand its seats, across every spec. **60,000** (~15k tokens). Over budget is clamped and SAID, per spec — an ask's whole claim is that it is the cheap check, and unbounded context is the #117 cost shape on the path advertised as costing a minute. |
| `review_panel.reviewer_code_access` | May a seat READ the code under review? **true**. `false` is the old posture — every seat in an empty repo, the diff its only evidence — and is what a repo taking UNTRUSTED contributions selects. On does not mean every seat gets it: only a CLI that can express "read but do not execute" is handed the tree (today just `claude`), and which seats did is recorded per seat. `--no-code-access` turns it off for one run. See below. |
| `review_panel.reviewer_code_budget_usd` | Dollars the code-reading seat may spend per invocation (`claude --max-budget-usd`). **`null`** — uncapped — for the reason `max_diff_chars` is: reaching the cap is a LOST seat (a skip, which vetoes), not a cheaper review. Measured for calibration: ~$4 for one seat on a 75,628-char diff against ~$0.70 diff-only. Applies only to a seat that got the tree; the cap is per invocation and a reparse retry can spend it twice. |
| `loops` | `dependabot_lander` / `stacked_driver` / `issue_executor` — which loops may run. |
| `epic` | Epic-driver settings — see below. |
| `preland.disabled_checks` | Checks `preland.py` must not run, by name. Empty by default — every guardrail it can detect, it runs. A name nothing answers to is a **hard error**, not a warning; see below. |

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

`review_panel.judge_model` (default: **`sonnet`**) — the model that adjudicates what
the seats found. It is deliberately not `opus`, which is `reviewers.claude.model`: the
adjudicator should not be the same brain as a seat it rules on, which is the note above
about `claude.model` applied one level up. The evidence is one day's worth and small —
on 2026-08-15 four judge-confirmed findings turned out plainly wrong on inspection, and
all four were raised by claude and confirmed by an `opus` judge — so the mechanism is the
argument, not the sample.

**Why `sonnet` and not `fable`, which this started as.** The first version chose `fable`
on a tie-break — the judge's job did not get easier, and `clamp_model` states this repo's
preference as failing toward capability. Both review rounds then attacked that from two
directions. `fable` is **not universally available**: it wants a recent Claude Code, is
not on every plan, can be organization-disabled and may want credits, and the panel does
no availability preflight — so an installation without access gets an unadjudicated round
every single time (loudly: `judge_skip` feeds the coverage veto and the report says "all
findings KEPT unjudged", so it degrades visibly rather than silently — but it degrades).
And it is the **priciest model in the panel**, on a run that happens for every reviewed
PR, in a file whose `skip_title_patterns` exist because one release-merge came to about
$750.

A premise attacked twice is the one to delete rather than patch. The requirement is
**independence** — the adjudicator must not be the brain that raised the finding — and
`sonnet` meets it outright: it is not `reviewers.claude.model`, it runs wherever the CLI
runs, and it is *cheaper* than the `opus` judge it replaces rather than dearer. The
capability argument was never evidence either way: the four wrong findings above were
confirmed by an `opus` judge, so capability is not what was failing.

**Want the most capable adjudicator? Set `judge_model: fable`.** That is what the key is
for. What a default should not do is make that trade on every repo's behalf, silently.

This is only the default. **Pinning both keys to the same model still works and says
nothing** — the enforcement half (`judge_independent`: refuse to run when the judge's
model matches an enabled seat's) is [#78](https://github.com/prisonblues/quarterback/issues/78)
and is not implemented.

`review_panel.max_diff_chars` (default: **none — the whole diff**) — how much of
the diff each model is given. Override per reviewer with
`reviewers.<name>.max_diff_chars` and for the master with
`review_panel.judge_max_diff_chars`; both inherit the panel value when unset. Since
v2.28 that budget buys the review target first and the PR context with whatever is left
(see rounds, below), so on a scoped round it is no longer the target that a tight budget
cuts. On a scoped round the budget also covers the brief and the section headers around
the diff — a little over a kilobyte — because the ceiling that matters is the model's
context window, and the prompt is what lands in it. Under `pr` scope it means chars of
diff, exactly as it always has.

`review_panel.round_scope` (default: **`increment`**) — whether a round past the first
reads the fix commit or the whole PR again. `pr` restores the pre-v2.28 behaviour for a
repo whose PRs are small enough that re-reading them costs nothing. Anything else is a
typo, and lands in `config_notes` saying so rather than quietly turning scoping off.

There used to be a 60,000-char default, and it was a fossil: prompts travelled in
argv, where Linux caps one element at 128 KiB, so a budget was mandatory. Since
they moved to stdin the only ceiling is the model's own context, and 60k chars is
about 15k tokens — an order of magnitude under every reviewer the panel runs.
Truncating when nothing forces it is not a saving, because a truncated reviewer
cannot notice that it was truncated: it reports on a prefix with full confidence,
and the errors that produces are *false positives* ("this migration is
syntactically incomplete" — it was cut mid-file) that then cost a judge call and
a fixer's attention to disprove.

Set one only if a model you run cannot take the change. Any positive value is
honoured — there is no lower sanity bound, deliberately; what surfaces a
too-small budget is that the report names **which** reviewers were truncated and
at what budget, which is worth knowing when two reviewers disagree and only one
of them saw the whole change. Only a value that cannot be a budget at all — not a
number, or `<= 0` — falls back, with a ⚠️ config line in the report saying so.

One seat is capped whatever you configure: `agy` takes its prompt in argv, so the
kernel bounds it. The panel clamps that seat to what `execve` will carry and
reports it as ordinary truncation rather than dying at exec — which is what it
used to do, reporting "LLM reviewers ran: none" as a clean review.

`review_panel.refuse_over_cap_multiple` (default: **3**) — how many times over the
**tightest seat ceiling** a diff may be before the round is *refused* instead of
truncated; `0` switches the refusal off and keeps the manifest, and `0` is the only
spelling of off (`null` and an absent key mean "use the default", here as everywhere
else in this file). `review_panel.move_shape_ratio` (default: **0.9**) — what fraction
of the larger side of a diff must be relocated text before the change counts as a
*move*; a fraction, so values above `1.0` are rejected with a `config_notes` line
rather than silently making the threshold unsatisfiable.
`review_panel.manifest_moves` (default: **true**) — review a move-shaped over-ceiling
diff as a manifest rather than as content; `false`, `"false"`/`"off"`/`"no"` and the bare
`0` all switch it off, and `true`/`1` all leave it on. All three keys are validated: a
value that cannot be what the key means falls back to the default and *says so* in
`config_notes`. `false` on a *number* is rejected rather than read as 0 — and the note
then names the right way to say "off" **for that key**: `refuse_over_cap_multiple: 0`
switches the refusal off, while `move_shape_ratio` is a threshold with no off at all (a
ratio of `0` turns the feature all the way *on* — every diff with one relocated line
becomes a move), so its note points at `manifest_moves` instead. All three are the
pre-flight verdict, below.

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
  branch; `gate` holds each at a human merge. **Defaults to `gate`.** It is the switch
  that decides whether the review gate merges anything, and that gate has been wrong on
  its first attempt three rounds running — so it defaults closed, like every other
  setting that lets an agent act unattended. Turning it on is one line.
- `auto_finish` — on a `/fix-issue` that pushed nothing, commit+push salvaged work
  rather than failing the issue.
- `executor_worktree_args` — extra flags for `create-worktree` (e.g. `["--no-docker"]`).
- `min_free_mb` — preflight warns below this `MemAvailable`.
- `model_ceiling` — highest tier a sub-issue may be implemented at when `--model` is
  not passed (`sonnet` < `opus` < `fable`; default `opus` — the ordering and the
  off-switch are pinned against `epic.MODEL_TIERS` in
  `harness/loops/tests/test_epic_model_ceiling.py`, so this sentence cannot go stale
  quietly). The triage judge runs here and routes each issue to this tier or lower.
  Anything outside those three turns model routing off, as does an explicit `""` or
  `null`. It used to fall back to `review_panel.judge_model`, which is a different
  question and now deliberately has a different answer — see `review_panel.judge_model`
  earlier in this file.
  > **If you previously set `review_panel.judge_model` to control what an epic spends,
  > set `epic.model_ceiling` too.** That key was load-bearing for two things and only
  > one of them was documented. A repo that set `judge_model: sonnet` to keep unattended
  > implementation cheap kept its custom judge across this change and picked up the new
  > `opus` ceiling — an unannounced rise in what an unattended sub-issue may spend. Repos
  > on the default are unaffected, which is what the CHANGELOG's "epic behaviour is
  > unchanged" means and all it means.
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
- **The agent's account of itself is kept.** "Agent made no edits" is an observation
  with two opposite explanations — nothing needed fixing, or the agent was stopped
  from fixing anything — so the line now quotes its last word. A run that exited
  non-zero, or exited 0 having printed nothing with a cause on stderr, is reported
  as a *failure* and nothing is pushed; the sweep carries on to the other PRs
  rather than raising. A run that said nothing anywhere but left real edits has
  them pushed: the staged diff is the direct evidence, and CI is the gate.

## Reviewer panel (`panel.py`)

`python3 ~/.claude/loops/panel.py --pr <n>` (report) / `--post` (also comment on the
PR) / `--json` (the run as JSON on stdout — nothing else, progress goes to stderr) /
`--round <r> --max-rounds <N> --baseline <earlier round's --json-file>` (a re-review that
knows what the earlier rounds raised, and where it sits in the caller's cycle) /
`--scope pr|increment|auto` + `--since <sha>` (what a later round reads — see below;
`auto` is the default and needs no flag) /
`--ask "<premise>"` (one question to the seats instead of a review — see below) /
`--force` (review the diff as content even when the pre-flight verdict refuses the round
or rules the change move-shaped — see below).

Read-only, so it runs in **any** repo — an unconfigured one just uses the defaults.

- Runs the repo's **enabled** reviewers in parallel over the PR diff: SonarQube (HARD
  quality-gate pass/fail), Claude (SOFT, read-only), Codex (SOFT, different vendor).
- **Skip patterns:** PRs matching `skip_title_patterns` are skipped entirely — but that
  is still a payload, marked `reviewed: false`, on `--json` *and* in `--json-file` (an
  empty stdout, or a missing baseline, would read as a clean PR).
- **Pre-flight verdict** (see below): before any seat is dispatched, the panel rules on
  whether the round is worth running and whether the diff or a *manifest* of it is what a
  seat should read. There is still **no diff-size de-minimis** — nothing is skipped for
  being small, and nothing is refused against a ceiling the repo or the kernel did not
  already declare.
- **Master judgment, no consensus gate:** a master reviewer judges every finding on
  its merits. A real defect flagged by only ONE reviewer is still fixed — agreement
  shows as a `⋆consensus` confidence marker, never a filter. Only clear false
  positives are dismissed, with a recorded reason. If no judge is available, nothing
  is suppressed. Agreement takes two reviewers, so on a panel of one the marker is
  not merely absent but unavailable, and the report says which of those it means.
- **Every member runs in its own empty sandbox repo**, not in whatever directory the
  panel was launched from — a seat that inherits the caller's shell is a seat whose
  participation nothing configures and nothing can reproduce, and codex, which refuses
  to start outside a git repository, was lost exactly that way. The sandbox is `git
  init`ed so codex is satisfied, and *empty* rather than the checkout for two reasons:
  a headless CLI reads its project configuration (CLAUDE.md, `.claude/settings.json`,
  hooks that execute) from its cwd, and the checkout is on whatever branch it was left
  on — never the PR's code, which the panel reads as a diff and never checks out. A
  seat pointed there can quote a different branch as the code under review. The
  members need no working directory at all; they need a reproducible one.
- **The seats can read the PR's code, per repo, on by default** (#113). Each seat that
  can take it runs in a checkout of the PR **at its head**, fetched from GitHub's
  tarball endpoint — never from `cfg["path"]`, which is the main checkout on whatever
  branch it was last left on and is the failure #75 measured. Why it is on: the
  blindness was expensive and it did not merely lose findings, it manufactured wrong
  ones. On PR #160's round 1, nine of nineteen veto lines were seats declaring they
  could not read a file this repo answers — all nine closed with `grep` in four
  minutes. On #64 the proposed fix *was* the bug; on #90 a P1 inferred a missing
  `--json` field from its absence in the diff when it was already there; on #123 no
  seat could see `migrations/versions/`, the tool's entire subject.
  - **Only a seat that can express "read but do not execute" gets it**, which is
    `SEAT_READS_CODE` and today means **claude alone**. #92 answered "may reviewers
    execute?" with no. Verified by running each CLI: claude names a tool set
    (`--allowedTools Read Grep Glob`, no `Bash`) and enforces a working-directory
    boundary of its own; codex's `-c` knobs only REMOVE tools and its single read path
    is the shell, so granting reads grants execution and re-opens the tool-hunt that
    once spent 99% of a run; pi's `--no-tools` is all-or-nothing over read/bash/edit/
    write; antigravity has no tool mechanism at all. A seat that cannot read keeps its
    empty sandbox — standing in a checkout it cannot open buys the instruction-file
    channel for nothing.
  - **Vendor convention files are stripped before any CLI starts** — `CLAUDE.md`,
    `AGENTS.md`, `GEMINI.md`, `.claude/`, `.codex/` and the rest of
    `CONVENTION_FILES`/`CONVENTION_DIRS`, at every depth, because a nested one is
    read too. This is a **denylist and it will rot**; that is an accepted cost where
    the contributors are your own agents and exactly why `false` is right where they
    are strangers. Symlinks are unlinked, never followed: a `.claude ->` pointing out
    of the tree would otherwise send `rmtree` at the real one. What was removed is
    named in `config_notes` and in the payload, so a PR that shipped one is
    distinguishable from a PR that did not.
  - **Recorded per seat**, which is what makes the measurement possible:
    `reviewers.<name>.code_blind` and a `code_access` block holding the setting, the
    seats that actually got it, and the files stripped. A seat that can read the tree
    while another cannot is a bigger confound than an unpinned model.
  - **Every failure degrades to the OFF posture, loudly.** A fetch that 502s, a
    tarball that will not unpack or has an unexpected shape, a copy that runs out of
    disk: the seat is blind, recorded as blind, and the round says why in
    `config_notes`. A review that would have happened always happens.
  - **A transient fetch failure is retried, a settled one is not.** Measured: five
    hand-run fetches of one sha during development returned two 502s and a 503 —
    GitHub packs a repository on demand for this endpoint and it is markedly flakier
    than the JSON API the rest of the panel uses. That matters more than the rate
    suggests, because the degrade is silent in *effect*: one note among several, and
    an ordinary-looking report. A feature that quietly stops applying a third of the
    time is worse than one that is off, because the config still says it is on. A 404
    is not retried — it is a settled answer about that sha (a fork PR, most likely),
    and `run_cli` already draws that line for reviewer CLIs.
  - **A per-repo spend cap, defaulting to uncapped** —
    `review_panel.reviewer_code_budget_usd` passes `--max-budget-usd` to the seat that
    got the tree. Uncapped by default because a number invented here would silently
    degrade reviews on repos that never asked for one, and because reaching the cap is
    not a cheaper review: the seat is lost, records a skip, and the skip vetoes the
    round's confident stop. Reaching it needs a guard nothing about the flag suggests —
    `claude` exits 1, writes its message to **stdout**, and leaves **stderr empty**, so
    `run_cli`'s stderr-based reason and stderr-based retry decision would give a bare
    "exited 1" and then repeat the attempt three times, re-burning a cap already spent.
  - **What it costs, measured** (one seat, sonnet, PR #214's own 75,628-char diff,
    run twice with only this feature differing): wall clock **922s vs 372s** (2.5×),
    input tokens **7,879,643 vs 159,520** (49×, though 97% of the larger figure was
    cached so the billed multiple is well below that), output 71,674 vs 36,364.
    Against that: `could_not_assess` went **4 → 0**, and the blind run filed a FALSE
    finding the sighted one did not — it saw a diff line mentioning `argv_capped`,
    could not tell which function it belonged to, guessed `accounts()`, and concluded
    the name was undefined. That is #90's failure mode reproduced unprompted.
    The cost lands per seat per round, and `/panel-review-pr` fans out up to four
    concurrent panels, so this seat is also the critical path. `claude` documents
    `--max-budget-usd`, which works with `--print`; bounding the hunting with it is
    the obvious follow-up and is deliberately not in this change.
  - **The judge gets the tree too, on the same terms.** It is a `claude` seat, so it
    takes the same stripped checkout, the same `Read Grep Glob` pin and the same spend
    cap — and it is the party best placed to use them, because the wrong findings #113
    was filed over were **confirmed**, not merely raised. On #90 a reviewer inferred a
    missing `--json` field from its absence in the diff and a judge with the same
    blindness had no way to check; on #64 three of six confirmed P2s were conditionals
    from a reviewer that had *declared* it could not assess the condition. Dismissing
    false positives is the judge's stated job and it cannot do it from the same diff
    that produced them. One ordering trap, pinned by a test: the tree's cleanup must
    run **after** `adjudicate`, or the judge gets a path to a deleted directory,
    degrades to an empty sandbox, and reviews blind with the setting still reading
    true — the silent failure that the degrade path's own correctness makes possible.
  - **The board stores it, rather than dropping it at ingest.** `absent`,
    `code_blind` and `argv_capped` per reviewer, plus `code_access` and
    `convention_files_removed` per run (migration `0024`), read back out of
    `GET /review/{id}` as well as written. `absent` had been sent since v2.32 and
    silently discarded, because `ReviewerIn` inherits pydantic's `extra="ignore"` —
    the same drop v2.26 records for `head_sha` and `unread_files` (#93). `code_blind`
    is the one that matters most for anything ranking reviewers: a seat that could
    open the caller and one that could not are not comparable on findings or on
    `could_not_assess`, and a leaderboard averaging them measures two different jobs.
    All columns nullable with no backfill — NULL is "the panel did not say", which is
    the honest value for every round recorded before this, and inventing `false` would
    assert coverage those rounds may not have had.
- **A short panel says so.** The report states seats filled against seats configured
  on every run, and calls the panel degraded above the findings when they differ — a
  weaker review, not a cleaner one. A CLI the host does not carry is exempt (it is a
  fact about the box, true every run, and `coverage_veto` already treats it that way);
  it is noted quietly instead. ⋆consensus needs two members that filed, so when only
  one did the report says agreement was *impossible* rather than letting its absence
  read as disagreement.
- **Merging happens once, in the judge, and adds rather than replaces.** The judge
  sees one entry per *reviewer*, merges the entries that are the same defect, and
  writes a `synthesis`; each reviewer's own title, detail, severity and line ride
  along in `reported_by`. Dedup upstream of the judge could only keep one
  reviewer's text and discard the rest — so the point only one reviewer made
  survived exactly when the merge *failed*, and a better key made the loss worse.
  Findings are pre-clustered by file and adjacent lines purely as a **hint**; the
  duplicates that matter are semantic (one defect, two line numbers), which no line
  arithmetic finds. Separate defects sharing one cause are linked with `related`
  instead of merged, so one decision is fixed once.
- Reviewers whose prerequisites are missing are reported **SKIPPED**, not failed.
  A seat whose CLI this box does not carry is reported but does not veto a
  confident stop: it is absent every round, so it says nothing about the round —
  otherwise a repo listing a workstation-only vendor would buy every unattended
  run on a headless box a standing veto. Every other way of not running does veto.
- **An absent seat gets no diff budget either, and that is the same rule reaching
  the other four places it was missing.** The exemption above was applied to the
  veto and to nothing else, while `budgets` was still built from the *configured*
  set — so a seat with no CLI on the box acquired a budget, an argv clamp, a
  `config_notes` line saying it "gets 116,287 of 177,872 diff chars", and a
  `truncated: true` record. Four statements about a reviewer that was never going
  to read a byte, and the last one was not cosmetic: `diff_truncated` went true on
  rounds where nothing that *ran* was cut, `load_baseline` banked the round as
  truncated, and the next round inherited *"whatever that round was cut off from
  has now been read by no round of this cycle"* — a `confident` veto, so every
  multi-round cycle on such a box was non-confident from round 2 onward,
  permanently. `budgets` is now filtered by `seat_installed` (in `panel_core`,
  beside `CLI_BIN`, read once per round and shared with `run_seat` and with the
  judge's own `adjudicate`, so no two of them can come to disagree about which
  seats exist), which closes all four at once. The seat is still **dispatched**
  and still records itself absent — that record is what the exemption above reads
  — but it is no longer handed a rendered prompt either, since a seat with no
  budget would otherwise be given the whole diff to throw away. Its
  `max_diff_chars` is `null` and its `truncated` is `false`, which is the pairing
  that broke; that guarantees a null budget can never sit beside `truncated: true`
  and nothing stronger, because an *installed* seat with no configured budget
  records the same pair. `absent` is the field that tells them apart.
  In the payload the seat keeps its `diff_budgets` key with a `null` value rather
  than vanishing, so a consumer reading `diff_budgets[name]` for a configured seat
  does not begin raising `KeyError` on exactly the boxes this is for.
  `load_baseline` banks a round as truncated on `truncated and not argv_capped and
  not absent` — both exemptions, each keyed on its own recorded field — because
  baselines written before either release still carry the old pairings. Its sibling
  `truncated_any`, which decides whether a round may **close** every earlier round's
  gap, exempts `absent` and deliberately **not** `argv_capped`: a capped seat ran and
  saw a prefix, so the round did not read its target whole and cannot be the one that
  clears an older gap, while an absent seat read nothing and is no evidence either
  way. It also requires positive evidence that a seat actually RAN, so a round in
  which every seat was absent — or present and crashed — cannot erase a gap banked
  by a round whose seats worked. The two do
  not subsume each other: `argv_capped` covers only what the kernel bounded, so an
  absent `pi` or `codex` with a configured budget smaller than the target would
  still bank a phantom round under the argv exemption alone. And not
  `ran and truncated`: `ran` is false for *every* way of not running, so that would
  also drop the truncation of a seat that was installed, read a real prefix and then
  crashed — a genuine coverage gap, and the fail-open direction.
- **A constant never vetoes, and three of them used to** (#113). The rule
  generalises the absent-CLI exemption above: an observation that is true of every
  round cannot tell a quiet round from a broken one, and because `confident` is
  `not veto`, leaving it in makes a confident stop unreachable rather than rare.
  The three are an absent CLI (above), a seat that **cannot read the code**
  (`ReviewerRun.code_blind` — see the coverage bullet below), and the **argv
  ceiling** on antigravity: its prompt travels in argv, the kernel caps one element
  at 120,000 bytes, and on a large diff it structurally cannot be handed all of it
  (measured on PR #160: 116,771 of 175,547 chars, 66.5%). Each is exempted off
  recorded state, never off the wording of a message; each is still reported; and
  each has a floor so that exempting seats one at a time cannot empty the veto list
  on a round where nothing was read whole. Truncation by a **budget** still vetoes —
  someone typed that number and can raise it, so it is a fact about the round.
  The argv exemption is applied in **two** places, and the second is easy to miss: the
  baseline loader carries an earlier round's truncation forward (increment scope never
  returns to what round 1 was cut off from), and a kernel-capped seat was not going to
  be closed by a later round either — so carrying it reintroduces the constant one
  round later and leaves it standing for the rest of the cycle, with round 1's veto
  list looking fixed. `/panel-review-pr` drives several rounds, so exempting only
  `coverage_veto` would have undone the change exactly where it matters. A budget
  truncation still carries: raise the number and the next round really does read it.
- **A reviewer that produces nothing is SKIPPED, never counted as an empty review.**
  A zero exit with empty stdout is a failure for panel members and the master alike,
  and the skip line quotes the CLI's own stderr, which usually names both the cause
  and the fix. A blank reply is retried unless that stderr names a settled cause (a
  refused request, an auto-denied tool permission), which no retry can change.
  Why it is worth the code: `run_cli`'s docstring in `panel.py`. The neighbouring
  case — output that is neither empty nor a findings array, e.g. an agent narrating
  a wait — is *not* a skip, because "no parseable array" would also throw away a
  reviewer that answered in prose because it had something to say. It is kept as one
  raw finding and flagged `unstructured`, which the coverage veto states as
  "returned no structured reply — its coverage is unknown".
- **Reviewers declare their own coverage.** Each returns `could_not_assess` (areas it
  could not judge — a file the diff omits, a runtime behaviour) and can mark a finding
  `needs_rereview` (fixing it takes a structural change whose result should be read
  again). Both are *observations*: reviewers are never asked to forecast whether
  another round is needed, because that asks a model to predict findings it has not
  made — and one that silently produced nothing would answer "no" with total
  confidence. Truncation is measured, not asked for, since a truncated reviewer is the
  one party that cannot notice it. A bare findings array (any older reviewer) still
  parses and simply declares nothing.
  **A blind seat's declarations are reported and do not veto** (#113). Every
  seat reviews from the diff alone — an empty sandbox, no file tools — so "I could not
  read a function this diff does not change" is true of every round it sits, and a
  constant is exactly what the veto must not contain. Measured on PR #160's round 1:
  19 veto lines, 16 of them declarations, and **nine of those asked about a file in
  this repo**, all nine answered with `grep` in about four minutes. Worse than the lost
  confidence, blindness manufactures wrong findings — PR #64's proposed fix *was* the
  bug, PR #90's round-2 P1 inferred a missing `--json` field from its absence in the
  diff when it was already there. The declarations stay on the PR comment, under a line
  saying they cost the round nothing and are worth a `grep`; that is the work this
  used to outsource to whoever read the output, and only when someone happened to.
  Recorded per seat as `code_blind`, so #113's second half — code access as a per-repo
  setting — flips it and the declarations start counting again, which is right: a seat
  that *could* have read the tree and still could not answer is describing the round.
- **Rounds are mechanical.** `--round`/`--baseline` make each run say which findings no
  earlier round raised; `round_stop` in the payload then says go-again (something new,
  a P1/P2 still outstanding, or a finding an earlier round raised that is still outstanding
  — SonarCloud's hard-gate issues included) or stop (dry / round cap), and whether
  stopping was *convergence*. The declarations never extend the loop — a truncated
  reviewer is truncated again next round — and the ones a blind seat makes no longer
  cost the stop its confidence either; what is left only stops a broken round being
  reported as clean. A round past the first with no `--baseline` is itself a veto: it has nothing
  to compare against, so its "all new" count means nothing and its stop is unearned.
- **A round past the first reviews the INCREMENT, not the whole PR** (v2.28). The target is
  what changed since the head its baseline reviewed (`head_sha` in the payload; `--since`
  overrides it), and behind it comes the PR **as it stood at that head**, in two tiers: the
  files the increment touched as they were before it touched them, then the rest of the PR.
  A budget is spent in that order, so what a tight budget drops is context and never the
  thing under review — which is the point, since a loop that re-reads the whole PR every
  round inflates its own input until it truncates itself. `--scope pr` restores the old
  behaviour and `review_panel.round_scope` sets it per repo; round 1 is always the whole PR.
  The near tier is fetched as its own `base...anchor` comparison, not sliced out of the
  current PR diff: the fix commit is part of that diff, so slicing it would send the target
  twice and label the copy as code an earlier round had already dealt with.
  **Every fallback to whole-PR scope lands in `config_notes`** — no anchor, nothing pushed
  between the rounds, a failed fetch (of the increment or of the context behind it), a
  truncated compare response, or a base-branch merge that makes the range bigger than the PR
  (measured: PR #62's raw round-to-round range was 92,415 chars against a 45,370-char PR, so
  the range is first cut to the PR's own files and then rejected outright if it is still the
  larger). A round that claimed the increment and re-read the PR would be wrong about the
  only thing this measures, and invisible in the numbers. Two degraded-but-usable cases are
  reported rather than refused: a range that is not a fast-forward from the anchor (a
  rebase or force-push, where a reverted change is in neither tier) and one carrying a merge
  commit (main's changes to files the PR also touches cannot be filtered out of the target).
  What shrinks is the **target**, always. The material sent is target plus context, so the
  token bill is in the same range as a whole-PR round; a note with both numbers says so on
  any round where the near tier is most of the context.
  One caveat: scope makes an earlier round's truncation **permanent** — round 2 never returns
  to what round 1 was cut off from — so a scoped round that inherits one vetoes its own
  confident stop, until a later whole-PR round with nothing truncated closes the gap. A round
  that recorded a head but ran no reviewer at all (a title skip, or every seat failing)
  vetoes the round after it the same way: the anchor steps over code nobody read.
- **`--json-file` is a requirement, not a courtesy.** It is the next round's baseline, so
  a write that fails exits non-zero after the report: carrying on would leave round `r+1`
  calling every repeated finding new. Every non-error exit writes it, the skip-pattern one
  included, so "the panel exited 0 and wrote no file" is not a state the caller has to
  interpret.
- **`--max-rounds N` is the CALLER's cap**, not a loop panel.py runs: it is the only
  input that tells a round which stopped because it was done from one which stopped
  because it ran out, and `/panel-review-pr` passes it on every invocation. Its flag is
  spelled `--rounds N` on the slash command and `--max-rounds N` here — same number, and
  `--round <r>` (singular) is a different thing entirely: which round THIS run is.
  A run given none of the three is a single review and says nothing about rounds — in the
  report *and* in the payload, whose `round_stop`, `stop_reason` and `new_findings` are
  null rather than a verdict about a loop nobody is running.
  A `--round` past `--max-rounds` is rejected rather than recorded.
- **The `/panel` and `/panel-review-pr` skills** run `panel.py --post` and work from
  the **PR comment** it leaves: `/panel` stops there (review-only), `/panel-review-pr`
  hands the confirmed findings to a fixer sub-agent that fixes, verifies and pushes —
  and then panels the fix commit, 2 rounds by default (`--rounds N`), so the fixer's
  own work is read by somebody. `panel.py` itself stays read-only either way: the
  fix/verify/commit lives in the skill, and so does the loop.

### The pre-flight verdict — whether a round is worth running

A panel was launched on PR #137 and killed five minutes in by a human asking *"is this a
crazy token count?"*. It was. The more important half is that **the output would have been
worth less than nothing**, and nothing in the harness knew that.

That PR's diff was **763,375 chars — 6.4× `agy`'s 120,000-byte argv ceiling — on a change
that was a pure move**: `panel.py` split into six modules with nothing retyped. Every
relocated line appears *twice*, once as a delete and once as an add, so the overwhelming
bulk of that 763 KB is code nobody changed. A seat spends its whole budget re-reading
relocated text, and whatever it reports is a finding about code that was already in the
base branch and already reviewed when it landed there.

**The token cost is the second problem. The first is that a truncated read which produces
findings is worse than no review,** because the next step of the cycle briefs a fixer to
resolve every one of them to a "nothing left to improve" bar.

Every piece needed to catch this already existed and none was wired to the decision: the
truncation report (above) fires *after* the round, the argv ceiling is documented as a
permanent property of the harness and gates nothing, and increment scope makes only *later*
rounds cheaper. So the panel now rules before it dispatches, and the ruling is its own
rather than the caller's.

**Three verdicts.**

| verdict | when | what happens |
|---|---|---|
| `run` | the diff fits every seat's ceiling; or no seat declares one; or it is over but under the refusal multiple | exactly what every release before this one did, truncation report included |
| `manifest` | over a ceiling **and** move-shaped **and** a manifest of it is smaller than *both* the diff and the ceiling | the seats are handed a *manifest* instead of the diff |
| `refuse` | over a ceiling by `refuse_over_cap_multiple` with no smaller honest question to ask — not move-shaped, or move-shaped with no manifest to substitute | nobody is dispatched; the refusal is printed, recorded, and posted under `--post` |

A move-shaped diff over a ceiling therefore has three outcomes, not one. It gets the manifest
whenever there IS one to substitute; when there is not — `manifest_moves` is off, or the
manifest came out no smaller than the diff, or smaller than the diff and still over the
ceiling — the refusal multiple decides as it does for content, and *below* the multiple the
round runs as an ordinary truncated content review. That last case is the one that must not be
silent, and it is not: the `run` verdict then carries a `reason` naming the manifest path, what
it measured, and why the substitution did not happen. An empty `reason` on a `run` means
"nothing objected", and only that.

**Every ceiling carries its unit, and the one that binds is chosen by ratio rather than by
number.** A configured `max_diff_chars` is characters; the kernel's argv limit is *bytes*, and
the two differ by the diff's non-ASCII density — this repo's own diffs are full of em-dashes
and arrows. Those are not two sizes of the same thing, so `min()` across them had no defined
answer: a repo setting `antigravity.max_diff_chars: 100_000` hid the 120,000-**byte** argv
ceiling behind the smaller integer, and at two bytes per character that seat's real ceiling is
~60,000 characters — tighter than the one that won. So `agy` with a configured cap declares
**two** ceilings, each is measured against the diff in its own unit, and the tightest *ratio*
decides. `preflight.shape` carries `chars` and `bytes` both and `preflight.cap_unit` names
which of them `cap` and `over_cap` are in, so the multiple can be checked by hand rather than
assumed — and every renderer of the verdict (the refusal notice, the manifest banner, the
`--force` banner, each seat's skip reason) states the unit it measured in.

**The judge's `judge_max_diff_chars` is deliberately not one of the ceilings.** This verdict
decides whether to dispatch the *seats* and what to hand them; that key says what adjudication
is worth, not whether a round is readable, and counting it would let it refuse a round every
reviewer could read whole. The judge is not left out in the cold: a manifest substitution
replaces the round's *material*, so the judge composes the manifest too, and a judge cut by its
own budget is reported as truncation exactly as a seat is. (`diff_budgets` in the payload does
include `judge` — that is the budget record, not the ceiling list.)

**A seat whose CLI this box does not carry declares no ceiling here.** That is the same
host-versus-round distinction the coverage veto makes for an absent seat, and it matters most
where it is least visible: `agy` is a workstation package, so on a headless box this repo's own
rules enable a seat that records "antigravity: CLI absent" and never runs. Counting its argv
ceiling would refuse rounds on behalf of a reviewer that was never going to read anything, on
exactly the unattended hosts where nobody is watching to pass `--force`, while the seats that
did run were reading off stdin with no cap at all. A box carrying no seat at all therefore has
no ceiling and refuses nothing — the round runs and `coverage_veto` says "no reviewer ran",
which is the existing and correct answer.

**Both questions are asked of the review target, not of the PR.** Under `pr` scope those are
the same string, so round 1 — always whole-PR, and the case #137 was — is unaffected either
way. Under increment scope they are not: the target is the fix commit and the PR is
everything, so weighing the PR would refuse (or hand a manifest to) a round whose actual
material is a 3 KB increment, because of a size that round was never going to send. A round 2
fix commit is neither large nor move-shaped just because the PR it lands in is.

**The shape is measured, not guessed.** A rename, a split or a relocation all look the same
mechanically: the added lines are a near-permutation of the deleted ones. The panel takes
the multiset intersection of the `+` and `-` bodies and divides by the larger side — no
`git diff -M`, because rename detection is a property of the git invocation and the panel's
diff comes from `gh pr diff` over the API, having never checked the PR out. Blank lines are
excluded from both sides (a blank matches every other blank), and only lines *inside a hunk*
are counted, because `--- a/x` and `+++ b/x` begin with the same characters a content line
can.

**A move is reviewed as a manifest, because for a move the mechanical evidence is the only
evidence that bears.** The manifest is:

- **what moved where** — each file's `+`/`-` tally, and whether it only gained or only lost
- **what did not survive** — lines deleted and not re-added anywhere. A dropped guard
  clause, `except` arm or decorator is invisible in a content review of the destination
- **what changed besides moving** — lines added and not deleted anywhere. This is the only
  genuinely new code in the change, and the one place a content review belongs
- **definitions the change adds in more than one place**, with the files each copy landed in —
  half of the duplicate-copy trap: a merge that keeps both copies of a moved function is a
  clean merge, a green test run and a silent bug, because the later binding wins and the dead
  one is what anybody reading the old file finds. Only *half*, and the manifest says which
  half. What is detectable from a diff is a definition **added** in two or more places. The
  more common accident — the original left exactly where it was, in a file the merge never
  touched, while a copy arrives somewhere new — puts no `+` and no `-` line in the diff for
  that original: it is not in the diff, it is in the base branch, and nothing recovers it from
  `gh pr diff`. Detecting it needs the PR checked out, which the panel never has, so the claim
  was narrowed to what is checked rather than the check widened to a promise it cannot keep.
  The spellings covered are Python `def`/`class` and the JS/TS `function`, brace-class,
  `const`/`let`/`var`-bound-arrow (generic and function-typed forms included), `const enum`
  and `interface`/`type`/`enum` forms. What is *not* covered is named too, and named
  **whether or not the section found anything** — class and object *methods*, wrapped
  signatures and every other language, so a section that found one duplicate cannot read as
  having found them all. So are the ordinary reasons a name is defined twice on purpose
  (`@typing.overload` chains, `if TYPE_CHECKING:`/`else:` pairs, platform-conditional
  definitions), because a reviewer who is not told about them stops believing the section on
  its first false positive
- **what is not here** — test counts before and after, whether a module now reaches backward
  into another, and the unseeable half of the duplicate trap above. All three need the branch
  checked out. They are *named as unmeasured* rather than claimed, and the brief tells the
  reviewer to declare them

Its size is a function of the change's **shape**, not of the diff's length: the 428 KB
worked example in the test suite produces a 1.3 KB manifest. It travels as the round's
review material, so the per-seat budgets, the truncation measurement, the judge and the
board record all keep working unchanged — and all measure the manifest, which is the thing
that was actually sent.

The material it replaces is a whole-target composition (there are no context tiers in a
manifest), so a *scoped* round whose increment is itself move-shaped keeps `scope:
"increment"` and its `since_sha` regardless: what the round **targeted** and the shape of the
material it composed are two different facts, and the inherited coverage vetoes are gated on
the first. Read `preflight.verdict` beside `scope` to know whether a round read its target or
a description of it.

> **A manifest round's findings answer a different question.** The report says so above the
> findings, because "no correctness findings" would otherwise read as "the moved code is
> correct". Nobody read the moved code.

It also **vetoes a confident stop**, mechanically. That is the least trustworthy quiet the
panel produces and the one nothing else catches: every other coverage veto keys off a seat
being short of what it was *sent*, and a manifest round's seats got the whole manifest, so
`truncated` is false for all of them. Without the veto a cycle could stop `confident: true`
having had nobody read a line of the moved code. The brief does ask each seat to declare the
facts a manifest cannot carry, and a seat that does adds its own `could_not_assess` veto — but
"was the moved code read" is something the panel *knows* from the material it composed, and
what can be measured is never left to a model to volunteer. A **forced** round needs no such
veto: its seats are cut by the ceiling that caused the refusal, so the ordinary truncation
veto already says so with the numbers.

**And the gap it leaves is inherited.** A manifest round is recorded on the baseline
(`Baseline.manifest_rounds`), for two reasons. It must not count as having **re-read the PR**:
it records `scope: "pr"` — the manifest travels as the round's material — with nothing
truncated, because the manifest fitted, so it satisfied every term of that test while having
read no code at all, and one such entry erases *every* earlier round's truncation and unread
record. And under increment scope the next round's anchor steps over the code it did not read,
so a later scoped round carries its own veto naming it. A round that genuinely re-reads the
whole PR clears it, on the same terms as the other two — the veto must not become permanent. A
payload written before `preflight` existed is not a manifest round: the field's absence is
evidence about the writer, not about the round.

**This is not a diff budget, and that distinction is the whole design.** v2.16 refused a
default budget on evidence — truncating when nothing forces it biases toward false
positives — and that reasoning is untouched. A budget answers *what to send*; this answers
*whether to start*, and only ever against a ceiling that already exists. **On a repo with
`max_diff_chars` unset and no argv-bound seat enabled there is no ceiling, so none of this
ever fires** and no number it invented reaches anyone's diff.

**Nor is it a silent skip.** A panel that quietly declines is a merge gate trusting a proxy.
A refusal:

- prints a notice headed **"Panel REFUSED — no review happened"**, stating in the first
  sentence that this is not a clean review, followed by the measurement and the remedies
- carries `reviewed: false`, a `skip_reason` and the full `preflight` block in `--json` and
  `--json-file`
- **is recorded on the board**, unlike the title-pattern skip — and that difference is
  deliberate. A title skip says *this PR was never worth a panel*; a refusal says *a panel
  was wanted and this diff defeated it*, which is the observation worth accumulating
- carries a `reviewers` block marking every selected seat `ran: false`, and the matching
  `skipped` entries. Not optional: the board builds a scorecard row for each name in
  `reviewers_selected` and, absent a `reviewers` block, assumes a member ran unless `skipped`
  names it — deliberately, so a quiet reviewer is not filed as broken. Sending the selection
  and nothing else would file a refusal as a clean review *per reviewer*, in the table that
  answers "which reviewer finds the real issues"
- **reads the CI gate anyway, and says so under the notice.** CI is size-independent, costs one
  `gh pr checks`, and consumes no seat's budget — it is the one part of a round a 763 KB diff
  cannot make useless, and a refusal that lost it left `/panel-review-pr` told to stop the cycle
  with nothing said about a red build. `ci_status`/`ci_failing` are recorded, and both
  `/panel-review-pr` and `/panel` are told to relay them with the refusal — the capability is
  worth nothing if the consumer it was added for is not asked to read it. **Sonar is not
  read**: it is a panel *member*, with a `ran: false` row like every other seat, and dispatching
  one while telling the board none ran would be the inconsistency this path exists to avoid — so
  the notice states that gate was **not evaluated**, rather than letting its default read as a
  pass
- **records what it was going to review** — `scope` and `since_sha`, the same way the ordinary
  payload does. A refusal under `--scope increment --since <sha>` otherwise publishes the field
  defaults, and nothing then distinguishes it from a refused whole-PR round
- is posted to the PR under `--post`. The terminal copy is read by whoever is watching, and
  under the epic driver nobody is

**`--force` overrides both the refusal and the manifest** and reviews the diff as content.
The verdict it overrode is *not* erased: `preflight.forced` and `preflight.would_have`
record it, a `config_notes` line says so, and the report carries a warning above the
findings. An override is a decision, and it must not look like the tool having decided to
run.

Two guards worth knowing, because both are cases that read plausibly when wrong:

- **A manifest is only substituted when it is smaller than the diff *and* under the ceiling.**
  Its body scales with the change's shape but its brief and section headers are a fixed
  ~1.3 KB, so on a small move over a small ceiling the substitution would hand a seat *more*
  text than the diff did and then have it truncated. Testing it against the diff alone was not
  enough: 200 table rows plus 240 quoted residue lines is ~35 KB of manifest, which is smaller
  than a 763 KB diff and still over a low-thousands `max_diff_chars` — a seat reading a
  *prefix of a manifest*, reported as a clean `manifest` verdict beside
  `diff_truncated: true`. Both are measured, not assumed; when the manifest does not fit,
  the round falls through to the refusal above the multiple and to an ordinary truncated
  content review below it, and either way the `reason` says the manifest was tried and why it
  did not help.
- **A tightly configured `max_diff_chars` can now trigger a refusal.** `max_diff_chars: 30`
  on a 1,559-char diff is 52× over. That is the intended reading — a seat handed 2% of a PR
  produces exactly the review this feature exists to prevent — but it is a behaviour change
  for a repo that set a small budget on purpose. `refuse_over_cap_multiple: 0` switches the
  refusal off and keeps the manifest.
### A finding no round can close (`--escalated`)

`--escalated <key>` (repeatable) tells a round that a finding's fixer reported the
**approach** wrong rather than the code and wrote no patch — `review-pr.md` step
3a. The key stops counting as work a fix round can clear.

It exists because the two rules around it are individually right and were jointly
a trap. An escalated finding is outstanding, and `panel-review-pr.md` §5 forbids
ever handing it to another fixer — so `round_stop` returned `stop: false` every
round until the cap, and the mechanism meant to stop a cycle circling a premise
guaranteed it ran to the cap instead (#221).

**The rule, its exact scope and its two caveats live in one place: `round_stop`'s
docstring in `panel_rounds.py`.** What a caller has to DO about it lives in
`panel-review-pr.md`. Neither is paraphrased here — this rationale was restated
five ways once, and two of the copies had already drifted into saying something
untrue by the time anyone read them together. What is worth recording here is only
what the flag touches outside that function:

- **The mixed case is why this is a filter and not "stop on any escalation".** One
  escalation beside a live P2 still goes again, for the P2; the cycle stops when
  the fixable work is gone rather than when the counter runs out. Stopping the
  whole cycle on any escalation would throw away the re-review of fixes made in
  the same pass — the round that, on PR #212, found 16 defects the previous fix
  introduced.
- **What it writes.** The payload carries `escalated: {key: round}` — the cycle's
  whole register, which later rounds read out of `--baseline` and which only ever
  grows — and `round_stop.escalated_outstanding`, the sorted subset of it that
  THIS round raised. The two are deliberately different questions: "what is this
  cycle holding" and "what stopped round 3". `escalated_outstanding` is the one
  that earned the veto line, so it is the one a consumer usually wants. Every
  finding in `to_fix`, `sonar_findings` and `dismissed` also carries
  `escalated: true|false`, and the report marks it ⛔ in the two lists a fixer's
  brief can be built from.
- **A key naming nothing is reported** in `config_notes` rather than ignored, and
  a value that is not a finding key (8-64 hex characters) is rejected before it
  reaches the register or a PR comment. The alternative failure is silent: the
  loop simply carries on counting a finding the caller believes it excluded. A
  round the panel SKIPPED adds no key — it reviewed nothing — but carries the
  inherited register forward and names the key it dropped.

### The premise check (`--ask`)

`python3 ~/.claude/loops/panel.py --ask "<premise>" [--context <path[:first-last]> …]
[--pr N] [--asker <seat>]` — one question to the enabled seats instead of a review.
**No diff, no clustering, no judge: the vote is the output.**

```
$ panel.py --ask "panel.py exits non-zero when it skips a PR on a title pattern" \
           --context harness/loops/panel.py:3500-3560
    claude  fails       — the skip branch calls finish(failed) and returns 0
    codex   fails       — write_payload then `return finish(...)`; exit 0 on that path
    pi      cannot tell — did not locate the skip branch in the given range

→ the premise FAILS — 2 of 3 say the premise FAILS (quorum 2, threshold 2)
```

- **It is not a gate.** Exit 0 on every verdict, `fails` included. A fixer runs it before
  committing; making it mandatory turns a one-minute question into a required wait, and a
  required wait gets skipped. It takes none of a round's flags (`--post`, `--round`,
  `--baseline`, `--max-rounds`) and says so rather than ignoring them — there is no diff to
  post about and no cycle for a baseline to be part of.
- **`cannot tell` is an answer, and an unreadable reply is not that answer.** A seat whose
  context did not settle the question counts toward the **quorum** and never toward the
  **threshold**; a seat whose reply carried no verdict counts toward neither and is shown
  as such, with the head of what it did say. Folding the second into the first is #68's
  panel-of-one through a side door: a tally reading "nobody objected" over seats that
  never spoke.
- **The asker cannot be the only seat.** `--asker` names the seat the agent running the
  challenge is (pass `--asker ''` for a human at a terminal). A tally whose only voter is
  the asker is `unchallenged` — where the premise started — never `holds`.
  **Detection is Claude Code's environment and only Claude Code's**: nothing codex, pi or
  `agy` exports says which seat is running a command, so an agent on one of those has to
  pass `--asker` itself. The run says so in its notes when nothing was detected, and says
  so again when `--asker ''` turned the guard off while an agent's environment was
  present — a guard believed to be on and quietly off is worse than one known to need a
  flag.
- **Nothing picks between candidate answers.** Two different legal verdicts in one reply is
  an unreadable reply, not a chance to guess which the model meant; an unreadable reply
  buys exactly one retry, the same as a review's. The schema's own example is refused by
  the same check that refuses a typo, because it spells the verdict as the union of the
  three legal values.
- **Verdicts:** `holds` / `fails` (threshold reached, one way only), `unresolved` (the seats
  looked and did not agree), `unchallenged` (too few answered, or only the asker did).
  `review_panel.ask_quorum` and `ask_threshold` govern it, both 2 — so "1 of 1 says it
  holds" reports as unchallenged rather than as agreement. Named for the ask because that
  is all they govern today; #78 generalises the same primitives to a round's verdict.
  A rule above the seat count is warned about, because it can never be met and the ask
  still runs (and pays for) every seat first. A threshold above the quorum is fine: quorum
  is a minimum, so three agreeing seats reach `ask_threshold: 3` under `ask_quorum: 2`.
- **`--context` is confined to the repo under review**, symlinks resolved before the
  containment test and the file then read by walking down from a descriptor on the repo
  root — the root's own open included — because resolving a path and re-opening it by that
  path are two traversals of one string, and a component that changes between them would
  pass the check and read elsewhere. Paths are relative to the **repo root**, not to the
  cwd, and the problem message says so. A spec that cannot be read is a stated problem
  rather than a silent omission — a seat given less context than the asker believes it has
  answers `cannot tell` about a question the asker thinks it supplied the answer to. That
  covers a file that is not UTF-8 text or carries NULs (a PNG used to arrive as a wall of
  U+FFFD), a malformed range (said as a range and not as a path), and a file past the
  4 MB read ceiling. A file whose own name ends in `:12` wins over reading line 12 of a
  file called something else; an exact repeat of a spec is read once. A range past the end,
  and anything over `ask_max_context_chars`, is clamped and said — and the range the report
  and the payload carry is the one the seats actually got, not the one that was typed. With
  no context at all (or none of it left after a clamp) the prompt says so, which is what
  keeps `cannot tell` available instead of inviting an answer from memory.
- **Containment was never the whole rule, so an ask refuses to read a secret.** Being
  inside the repo under review is exactly the case where `--context .git/config` hands a
  personal access token to four third-party CLIs, since every seat's reply is a place its
  prompt can come back out. `.git/` is refused outright, as are the files that are nothing
  but credentials by the names they always have — `.env` (and `.env.*`), `.envrc`,
  `.npmrc`, `.netrc`, `.pgpass`, `.pypirc`, `id_rsa`/`id_ed25519` and friends, and anything
  ending `.pem`, `.key`, `.p12` or `.pfx`. Each refusal is a stated `context_problem`
  naming why, like every other spec that did not become context. It is a denylist of names
  and not a secret scanner: it closes the routes an agent composing a command line actually
  types, and says nothing about a token pasted into a source file.
- **`--json` / `--json-file`** emit the ask's own payload: `kind: "ask"`, the premise, the
  `context` actually read (spec, path, line range, chars), the specs that did NOT become
  context as `context_problems` (`{spec, problem}` — machine-readable, so "was this verdict
  reached with all the context the asker intended?" is answerable without matching prose,
  and kept out of `config_notes`, which is about the repo's configuration), every seat's
  `verdict` and `reason` — plus `gist` for a reply that carried no verdict, which is what
  the seat SAID and never why — and the tally with its rules. Per-seat token usage is
  spread into the same object but written first, so a telemetry key can never overwrite the
  answer. Recording on the board goes through `qb record-ask`, best-effort and never
  fatal: `qb` ships in the fleet's own repo and the row it writes is #77's to define, so on
  a host whose `qb` predates it the ask says so once and is otherwise untouched. Every
  other recording failure is reported too, and a run whose `--json-file` could not be
  written is not recorded at all — it is about to exit non-zero, and a board row for it
  would be two records disagreeing about whether the ask happened.

### The `--json` payload

One record per **defect**, in `to_fix` / `dismissed` / `sonar_findings`, plus the run's
own fields (`judged`, `reviewers`, `diff_budgets`, `run_key`, …). A skipped PR emits the
same keys with empty values and `reviewed: false`, so nothing has to branch on which
exit produced it.

**v2.23 — what the PR touched, and what state it is in.** The run carries `changed_files`
(the PR's paths, each with its own `additions`/`deletions`) beside the `changed_lines`
total, plus `changed_files_total` (GitHub's own count), `pr_state` and `is_draft`.

**`changed_files_total` may be NULL, and that is not the same as `0`.** NULL means GitHub
did not state a count; `0` means it counted and the answer was none — the second is
knowledge and the board acts on it. The same rule holds per file: an `additions` of `null`
means "not stated", never "no lines". The one number never derived from another is this
one, because `len(changed_files) < changed_files_total` is the *only* evidence the list is
a prefix — `gh` pages it and GitHub caps it at 3,000. When they differ the run's
`config_notes` say so, on **every** exit including the skipped one.

It is the **PR's** file list, not the round's, read from `gh pr view` rather than from the
diff the reviewers are handed. That is what lets the skip path — which never fetches a diff
— still emit a complete one, and what keeps it correct under a round that reviews only the
increment: a collision surface that narrowed with the increment would report two PRs as no
longer colliding because one stopped *re-reading* a file it still changes.

> **The skip path emits this but does not record it.** It returns before `record_run` — no
> review happened — so a skipped PR's file list reaches `--json` and the next round's
> `--baseline`, and never the board. Do not read "the skip payload carries it" as "the
> board can answer collision queries about a skipped PR"; it cannot.

Note that `gh pr view --json` **fails the whole command** on a field it does not recognise
rather than omitting it, so there is no graceful degradation on an older `gh` — the run
exits before any review. `panel.py` needs a `gh` carrying `files`, `changedFiles`, `state`
and `isDraft`.

Run-level fields worth knowing about because their meaning is conditional:

| field | what it is |
|---|---|
| `scope` | `pr` \| `increment` — what this round actually reviewed. Recorded rather than inferred from the round number, because scope falls back to `pr` whenever the anchor is missing or the range is unusable, so "round 2" does not imply "increment" |
| `since_sha` | the anchor the increment was taken from; null under `pr` scope |
| `diff_chars` | the size of the **review target** — the whole PR under `pr` scope, the increment under `increment`. Read `scope` beside it: plotting this across a cycle's rounds without doing so shows a cliff at round 2 and reads as a shrinking PR |
| `preflight` | the verdict the round was weighed against before it ran, on every exit that reached it: `verdict` (`run`/`manifest`/`refuse`), `reason`, `cap`/`cap_seat`/`cap_unit`/`over_cap`, `forced`/`would_have`, the `thresholds` in force, and `shape` (`chars`, `bytes`, `added`, `removed`, `moved`, `move_ratio`, `files`, plus `files_added_only`/`files_removed_only` — the one-sided file lists, capped at 40 paths each with a `_elided` count beside them, because this block rides in every board record and a 700-file refactor wrote 700 paths into each one). `cap_unit` is `chars` or `bytes` and says which reading of `shape` the `cap` and the `over_cap` beside it are in — a ratio a consumer cannot attribute to one of the two readings cannot be checked at all. `over_cap` is null when and only when no ceiling was declared: a measured ratio is emitted even where it rounds to 0.0, so "small against a real ceiling" and "no ceiling" stay different answers. **`null` means the run never reached the verdict** — the title-pattern skip returns before it — which is a different statement from a `run` verdict, and the difference is what answers "was this PR ever weighed?" |
| `preflight.shape` | measured on the **review target**, so it is scope-dependent exactly as `diff_chars` is: the whole PR under `pr` scope, the increment under `increment`. Read `scope` beside it. Under a `manifest` verdict it is also the only place the target's *pre-substitution* size appears, because `diff_chars` then measures the manifest — which is what was reviewed |
| `context_chars` | everything prepared *alongside* the target; 0 under `pr` scope. With `diff_chars` this is what an uncapped reviewer was given. Neither is a per-reviewer number — budgets are, so a seat that got less says so in `reviewers.<name>.max_diff_chars` and `.truncated`, and a seat that got the whole target and only part of the context is named in `config_notes` |

Each finding record:

| field | what it is |
|---|---|
| `id` | run-local (`1609-F03`), and only for resolving `related` **within one payload** — it is a position, so it moves between runs |
| `key` | the defect's identity **across** runs: file + the reporting reviewers' own words, so a re-review joins the same chain even though the judge re-words its synthesis every time |
| `synthesis` / `detail` | the merged statement and its body (the board stores these as `title`/`detail`) |
| `verdict` | `confirmed` \| `dismissed` \| `unjudged` \| `sonar` |
| `reported_by` | one entry per reviewer: its own `title`, `detail`, `severity`, `line`, and `account` (the two joined, for reading) |
| `reviewers`, `related`, `rationale` | who reported it, sibling findings from one cause, and the judge's reason |
| `needs_rereview`, `rereview_by` | a reporter declared that fixing this takes a structural change whose *result* should be read again, and which reporters said so — the declaration the next round is checked against |
| `new_this_round` | no earlier round of this cycle raised this defect (`--baseline`); `false` on a repeat. A run with no baseline has no earlier round, so every finding is `true` — which is why a round past the first with no `--baseline` is a veto rather than a clean sweep |
| `provenance` | **v2.24.** Which of the two things a `new_this_round` finding is: `introduced` (on a line the last fix pass wrote), `missed` (present in the earlier round's diff and not seen), `missed-unread` (in a file that round was truncated out of — a coverage failure, not a reviewer one), or `unknown` (no readable fix range, or a finding with no line to place). `null` where the question does not arise: outside a cycle, in round 1, or on a repeat — a repeat's provenance is not unknown, it is not asked, because the defect predates the fix pass under attribution |

Provenance is a **signal, not a verdict**, and nothing gates on it. A fix can break something at a
distance, so `missed` is evidence of a miss rather than proof of one — the same discipline as
`rereview_hit` being file-grain and saying so. #41 (review the increment) is what would make it
exact, at which point a finding in the increment is introduced by construction.

Its known biases, since the defence of a heuristic is that they are written down:

- **A base branch merged into the PR between rounds** lands inside the fix range, so lines the
  fixer never wrote read as `introduced`. A branch *rewritten* between rounds (rebase, force-push)
  is caught — GitHub calls that compare `diverged` and provenance refuses it — but a merge is
  genuinely "ahead" and indistinguishable from a fix commit at this grain.
- **Two changed files a finding's path could name** (a reviewer writes `panel.py`; the diff has
  two of them) yields `unknown`, not a coin toss.
- **A defect the fix pass introduced by DELETING something reads as `missed`.** The fix range is
  reduced to the lines the fix pass *added*, so removing a guard, a null check, a `finally` or an
  `await` introduces a defect with no added line to place it on. `introduced` under-counts by
  however much of the fix pass was subtraction, and `missed` absorbs it.
- **`introduced` requires exact line membership, and reviewer line numbers drift.** LLM reviewers
  routinely report a line a few off — the top of the enclosing function, the closing brace, the
  line after the defect — and Sonar reports the issue's own anchor, which need not be a line the
  fix wrote. Each of those misses the added-line set and comes back `missed`. Both biases push the
  same way, so read `introduced` as a **floor** and `missed` as the bucket that absorbs whatever
  the arithmetic could not place.
- **Sonar's hard-gate issues carry provenance and are counted with the rest.** They are PR-scanned
  — SonarCloud's new-code view — so the same reading holds; a Sonar issue that predates the PR
  would read `missed`, and that is the scanner's file scope rather than the panel's under-reading.

Run-level fields it depends on:

| field | what it is |
|---|---|
| `head_sha` | **v2.24.** The commit this round reviewed. Recorded because nothing else identified one — `base` holds a branch *name* — and the next round needs it twice over: as one end of the fix range, and (**v2.28**) as the anchor its increment is taken from. Re-read straight after the diff is fetched, which narrows the mid-round-push window without closing it: a push can land either side of the fetch and nothing can tell which, so a move is reported as a move (`config_notes`) rather than as a claim about which commit produced the diff, and the later commit is recorded because it is where the next round's fix range starts. Present on the **skipped** payload too: a skipped round is still the round the next one baselines against |
| `unread_files` | **v2.24.** Files no reviewer that ran read in full, for the next round's `missed-unread`. A file counts as unread only if *every* running reviewer was cut on it, and a file straddling the cut counts as unread — half a file's hunks is not a read file. Empty on a payload whose `reviewed` is `false` means *no coverage at all* (a skipped round never fetched a diff to name files from), not "read everything" — the consumer tells the two apart by `reviewed` |
| `provenance_counts` | **v2.24.** The per-round tally over the findings the cycle has to clear, so a consumer gets the shape of a round without walking every finding. `{}` where the question does not arise — outside a cycle, or in a cycle's round 1, which has no earlier round to attribute against. All-zero is the other statement: a round that could have attributed and had nothing to, which is what a **skipped** in-cycle round sends |

A baseline written before v2.24 carries no `head_sha`, so provenance degrades to `unknown` rather
than attributing findings against a range it invented.

**Breaking, v2.14:** the per-finding keys were `title` / `detail` / `reason` with a
`reviewers` name list. They are now `synthesis` / `detail` / `rationale`, and `id`,
`key`, `verdict`, `related` and `reported_by` are new. The board accepts both spellings
(`POST /review` aliases `title`↔`synthesis` and `reason`↔`rationale`); any other consumer
has to be updated.

## Epic driver (`epic.py`)

`python3 ~/.claude/loops/epic.py --epic <n>` (plan) / `--execute` (run the pipeline).
Per sub-issue: create worktree → `/fix-issue` → CI → panel → `/review-pr` → stop at
the human merge. Merge is never automatic. Run state in `~/.local/state/loops/`, so a
killed run resumes instead of re-deriving from GitHub.

Both skill runs are captured (and passed through, so the log stays live). The driver
already refused to call an issue done without a PR; it now records *why* there wasn't
one — the agent's own last sentence, which the torn-down worktree took with it before.
A `/review-pr` that exited non-zero or printed nothing is a **failure**, not a review:
`reviewed` is the outcome that lets the driver stack the sub-PR into the epic branch,
so it would otherwise merge a PR whose findings nobody addressed. A re-run sees the
open PR and retries the review stage. A review that ran and pushed nothing (the PR's
head SHA is where it was) is *reported* rather than failed — finding nothing to fix is
legitimate, and by the last round it is the point — but it is reported with the agent's
own last sentence, because it is the same shape as a review that was stopped from
fixing anything.

## Pre-land verdict (`preland.py`)

```bash
python3 ~/.claude/loops/preland.py --pr 131            # the report
python3 ~/.claude/loops/preland.py --pr 131 --json     # the payload a loop reads
```

**May this PR be merged, and if not, what is outstanding.** One answer, three values,
exit codes deliberately shared with `scripts/migration_reconcile.py` so the two tools
never mean different things by the same number:

| Verdict | Exit | Means |
|---|---|---|
| `READY` | 0 | Every check that ran is satisfied. |
| `RECONCILE` | 3 | Mechanical work is outstanding; `actions` holds the exact commands and the files they touch. Do them, then run this again. |
| `HOLD` | 2 | Something is unresolved that the loop must not resolve itself. `reasons` says what, and who has to. |

HOLD dominates RECONCILE — relinking a migration graph on a PR nobody reviewed is work
spent to reach a wall. Note that 2 is also argparse's usage exit; the JSON payload is what
tells them apart, and since both mean *do not merge*, a caller that conflates them fails
safe.

**Why it exists.** The checks a merge must pass used to be prose in two places:
`/fix-and-land` §4 described them in about fifty lines of English, and `/panel-review-pr`
§7 was a bare `gh pr merge` with nothing in front of it. Prose in two files drifts, has no
exit code, and cannot be asked afterwards whether it ran — and a model reading it is
invited to re-derive a decision it should be executing. On 2026-08-16 a PR was merged on
`mergeable` + CI-green over its own panel round (8 P1s and 12 P2s outstanding) by an agent
that had written up that exact confusion an hour earlier.

**What it checks**, all of it mechanical, none of it a proxy:

| Check | Holds when |
|---|---|
| `pr_state` | Not OPEN, a draft, or `CONFLICTING`. Uncomputed mergeability warns. |
| `checkout` | This tree is not at the PR's head, or has tracked modifications. Untracked files warn. |
| `ci` | `gh`'s check rollup is red, **pending**, or **empty** — a push restarts CI, so an earlier green is stale, and no checks at all is silence rather than green. |
| `review` | The board's newest round for this PR read another commit, did not `stop`, has `confirmed > 0`, or has a failing Sonar gate. No round at all is a HOLD too, and so is a round that recorded no finding count — unknown is not zero. |
| `merge_claim` | Another agent holds `kind=merge` on `<repo>:<branch>`. |
| `migrations` | `scripts/migration_reconcile.py` says `stop`, or its plan and its exit code disagree. `relink`/`renumber`/`merge` are RECONCILE. |
| `sw_version` | `scripts/check_sw_version.py` fails in a way `--fix` cannot repair. A repairable one is RECONCILE. |

The last two also HOLD when `origin/<base>` could not be refreshed — they are answers about the gap between this branch and the base, and a base last fetched yesterday produces a confident NOOP about a head that moved this morning. `--no-fetch` makes that the caller's choice instead, noted once on the run.

The `review` clauses are the round's **own statements** — `head_sha`, `stopped`,
`confirmed`, `sonar_gate` — read back rather than re-derived. #62 spent three rounds
discovering that merge gates trust proxies (the exit code, then the push, then the
existence of a payload file), and this must not become the fourth.

`stop_confident: false` is a **warning, not a hold**, deliberately: two permanently-absent
reviewer seats on a headless box would otherwise make a green verdict unreachable, which is
the noise-for-signal trade this file already argues against for `coverage_veto`. The vetoes
are printed with it.

**Capability detection, and its two exceptions.** Repo-local guardrails are detected — a
repo without `scripts/migration_reconcile.py` records `skipped-absent` and moves on, which
is what lets one gate serve quarterback, lexray and an unenrolled repo with no per-repo
branch in the skill.

The first exception is a hole in that mechanism rather than a policy: detection reads the
**branch's** tree, so a diff that *deletes* a guardrail would hand itself `skipped-absent`,
switching off the check by the very change the check exists to read. So an absence only
counts as a skip when `origin/<base>` does not have the script either; a branch that removed
one HOLDs.

The second is the board. An unset `QUARTERBACK_BASE_URL` does not mean this repo has no
review invariant, it means the invariant exists and cannot be seen, so `review` HOLDs. That
knowingly narrows the "local path stays first-class" promise for `/fix-and-land` (not for
`/panel`, which is unaffected), and the off-switch is one line:

```json
"preland": { "disabled_checks": ["review"] }
```

A name in that list that no check answers to is a **hard exit**, unlike every other
unknown key in a rules file, which is warned about and dropped. The asymmetry is the point:
a misspelled key elsewhere leaves a setting at its default and the default is the safe end,
where a misspelled name here would leave a merge gate's check running while reading as
deliberately off.

**A check that did not run is still reported** — `skipped-absent`, `skipped-disabled`, or
`skipped-flag` for `--skip` — because a payload must never read clean by omission.

**It is read-only and takes no claim.** It reports commands and never runs them; it reads
`kind=merge` claims and never takes one. That belongs to whatever does the merging
([#100](https://github.com/prisonblues/quarterback/issues/100)) — a verdict that mutates
cannot run as a CI check, cannot be re-run to verify itself, and cannot be asked twice by a
loop that wants to know whether its own fix worked. The single write is `git fetch` of the
base branch's remote-tracking ref (`--no-fetch` suppresses it), because a migration verdict
computed against a stale `origin/main` is confidently wrong in the direction that lands.

**Its limit.** It is advisory: a script an agent chooses to run cannot stop a human merging
in the UI or a loop that skips the step. What would actually block a merge is a required
status check on a protected branch. A CI job doing that must pass `--skip ci` — such a job
is itself one of the checks `ci` reads, and would otherwise gate on its own pending status.

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
- **`preland.py` is where those gates are read**, so that a loop about to merge consults
  one verdict rather than each skill's own paragraph about them. It does not add a gate;
  it makes the existing ones executable, and its `READY` is a claim about what was
  checked, not about whether the change is a good idea.
