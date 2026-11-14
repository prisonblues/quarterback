---
description: "One Codex review round with brakes: a single Codex seat that may run the touched tests, classifies every finding into a defect class (invariant, enforcement, guard test, every instance), checks later rounds against that class ledger, refuses a per-path patch on a class's second strike, and stops after 3 rounds without convergence for the owner to decide. Use instead of hand-rolled `codex exec` loops."
argument-hint: "[--base REF] [--design FILE] [--shard data,api,concurrency|all] [--wave LEDGER ...] [--network] [--no-exec] [--class-rounds N]"
---

# Codex round — a second opinion that converges

Run **one** Codex review round over the current worktree's change (or over a design
note with `--design`), with the instruments the hand-rolled `codex exec` loops never
had (#836):

```bash
python3 ~/.claude/loops/codex_round.py $ARGS
```

Run it from inside the worktree. With no `--base` it diffs against `origin/HEAD`.
The diff runs from the merge base and **includes uncommitted changes**, so it works
before you commit.

## What it does

- **Classifies.** Every defect comes back as a class: what all its instances share,
  the invariant that rules it out, the enforcement that would make it impossible, the
  guard test, and **every** instance and path the reviewer found.
- **Runs code.** The seat works in your worktree with a shell, under codex's
  `workspace-write` sandbox, and settles suspicions by running the touched tests or a
  snippet, citing what it ran. It is told not to change the tree, and a change is
  reported. Add `--network` when a check needs the database; `--no-exec` gives the
  read-only, shell-less seat instead.
- **Remembers.** The class ledger lives in the worktree's git dir (one per worktree,
  never in the tree). Round 2 onward checks the recorded invariants instead of
  hunting afresh, and only a genuinely new class counts as news.
- **Shards.** `--shard all` reads data/DB, API/visibility and concurrency as separate seats,
  so each goes deep on one area. Seats that run code take the tree one at a time,
  because they share its files and its test DB; `--no-exec` seats run in parallel.
- **Reads the wave.** `--wave <ledger>` adds another PR's classes, read-only, for
  epic work where one race shows up in several PRs.

## Exit codes, and what you do

| Exit | Meaning | Do |
|---|---|---|
| 0 | converged: no new class, nothing to fix | done |
| 1 | findings | fix the whole class each finding names, then run again |
| 5 | **two strikes**: a later round found the class on a path its first round did not list (all of one round's paths are one strike) | do not patch the path. Make the class impossible by construction, then `python3 ~/.claude/loops/panel_classes.py structural --ledger <path> --round <n> --class <key> --how … --enforcement … --guard-test …` |
| 4 | **budget spent**: `review_panel.class_ledger.rounds` (3) rounds each found a new class | stop. Report the open classes and their structural options; the owner decides. `--class-rounds N` is their override, not yours |
| 2 | codex absent or no usable reply | skip; it never blocks |
| 5, for a class that is not a defect | the two-strike rule has nothing to fix | say why and record it: `python3 ~/.claude/loops/panel_classes.py dismiss --ledger <path> --round <n> --class <key> --why "…"`. It owes nothing after that, and later rounds are told it is ruled out. Put each dismissal in the PR body: it is a judgement the reviewer of the PR should see |
| 3 | the ledger could not be read as it stands, or could not be written | nothing ran and the file was left as it was. Fix or move it; never delete it to get past this, which wipes the strike counts and the budget |

A round where a shard gave no reply says `PARTIAL` and is never reported clean.
