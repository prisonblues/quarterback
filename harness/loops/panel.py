#!/usr/bin/env python3
"""Reviewer panel — multi-reviewer PR review with consensus synthesis.

Runs the enabled reviewers for a repo (per its .harness-rules) in parallel over a
PR's diff, then synthesises:
  - SonarQube  -> HARD gate (quality-gate pass/fail) + issues
  - Claude     -> SOFT findings (read-only; diff in, JSON out)
  - Codex      -> SOFT findings (different vendor)
  - Antigravity-> SOFT findings (third vendor; off unless a repo enables it)
No consensus gate: every finding is judged on its merits by a "master" reviewer.
A real defect flagged by only ONE reviewer (e.g. Codex) is still fixed — agreement
is a confidence signal, not a filter. Reviewers apply the full /review-pr bar
(correctness, security, tests, docs, related code, craft — P1–P4); only genuine
false positives are dropped, so style and polish findings are kept, not filtered.

Reviewers whose prerequisites are missing (codex CLI absent, SONAR* env unset)
are reported as SKIPPED, not failed — the panel still produces a report.

LLM replies are parsed leniently: a balanced-bracket scan (not a greedy regex)
pulls the JSON array out of ``` fences or surrounding prose; an unparseable reply
is retried once, then kept as a single markdown finding rather than dropped — so
malformed JSON degrades into one ungrouped finding, never a crash or a silent loss.

Default prints a report. Pass --post to also comment the summary on the PR, or
--json to emit findings as JSON (consumed by the /panel skill's fix loop);
--json-file writes that JSON *and* keeps the report.

Every run is recorded on the quarterback board (`qb record-review`, best-effort —
a down board never fails a review) so which model finds the real issues, and
whether a pricier tier earns its keep, accumulate into an answer instead of an
impression. --no-record opts out; a machine with no board configured no-ops.

Which reviewers run comes from the repo's .harness-rules; --reviewers overrides
that for one run, which is how you get a single-vendor read (--reviewers codex)
without editing config to get it.

Usage:
    python3 ~/.claude/loops/panel.py --pr 734
    python3 ~/.claude/loops/panel.py --pr 734 --post
    python3 ~/.claude/loops/panel.py --pr 734 --json
    python3 ~/.claude/loops/panel.py --pr 734 --reviewers codex
    python3 ~/.claude/loops/panel.py --pr 734 --reviewers claude,codex,antigravity
    python3 ~/.claude/loops/panel.py --pr 734 --post --json-file /tmp/panel.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import harness_rules  # noqa: E402
from harness_rules import RepoNotFound, describe, resolve_repo  # noqa: E402

# Chars of diff handed to a model, when nothing in .harness-rules says otherwise.
# It is a per-MODEL budget (review_panel.max_diff_chars, overridable per reviewer
# and for the judge), because 60k is one number standing in for several different
# context windows — and the cost of getting it wrong is invisible: the reviewer
# reads a prefix of the diff and reports confidently on the part it saw.
MAX_DIFF_CHARS = 60_000
RAW_DETAIL_CHARS = 4_000  # cap an unparsed reviewer reply kept as a fallback finding

# How long a reviewer CLI may take. One constant, because two CLIs enforce it:
# run_cli kills a wedged process at this bound, and `agy` self-aborts at its own
# `--print-timeout` (default 5m0s), so the seat that does not read this number
# silently reviews on a five-minute clock while the report claims thirty.
CLI_TIMEOUT = 1800

# Linux caps ONE argv string at MAX_ARG_STRLEN = 131,072 bytes, independently of
# the much larger total ARG_MAX; cross it and execve fails with E2BIG before the
# CLI starts. Every reviewer whose prompt travels on stdin is free of this, and
# that is all of them but one — `agy` has no stdin path (`-p ""` is "empty
# prompt", `-p -` reviews the literal string "-"), so its prompt is clamped to
# fit here and the truncation is reported like any other. The margin is for the
# rest of the argv and the environment, which share the kernel's accounting.
ARGV_PROMPT_MAX_BYTES = 120_000

# The panel's possible members. LLM reviewers are interchangeable in everything
# except how their CLI is invoked; sonarqube is a different shape (an API, and a
# hard gate), so it is selectable but not iterable with the others.
LLM_REVIEWERS = ("claude", "codex", "antigravity", "pi")
ALL_REVIEWERS = LLM_REVIEWERS + ("sonarqube",)

# Reviewer name -> the executable to look for on PATH, where the two differ.
# They differ for exactly one member: Google ships the Antigravity CLI as `agy`.
# The reviewer is still named for its vendor everywhere a human types or reads it
# (`--reviewers antigravity`, .harness-rules, the report), because `agy` is the
# command, not the thing having the opinion.
CLI_BIN = {"antigravity": "agy"}

REVIEW_PROMPT = """You are reviewing a pull request diff to the same exhaustive standard as a
senior reviewer whose bar is "nothing left to improve". The marginal cost of completeness is
near zero: report EVERYTHING you spot, across every dimension below — do NOT self-censor a
finding because it seems "minor" or "just style". A later master judge filters false positives;
your job is breadth, not triage.

Review for:
- Correctness: logic bugs, off-by-ones, race conditions, boundary conditions, null/None handling
- Security: injection, auth bypass, secrets in code, path traversal, SSRF, unsafe deserialization
- Error handling: swallowed errors, missing validation, silent failures, unhelpful messages
- Concurrency: async pitfalls, missing awaits, shared mutable state, transaction isolation
- Performance: N+1 queries, unbounded iterations, missing indexes, unnecessary allocations
- Test coverage: new code paths, bug fixes, or edge cases visible in the diff that lack a test
- Documentation: behaviour changes that leave CLAUDE.md, docs, README, or docstrings stale
- Related code: callers, siblings, or parallel implementations that should change to stay consistent
- Craft: naming, complexity, dead code, redundant conditions, project-convention/style breaks, DRY

Severity: P1 blocks merge (correctness/security) · P2 important (error handling, test gaps,
logic flaws) · P3 should fix (style, naming, simplifications) · P4 polish (minor consistency).
Report all of them.

Return ONLY a JSON array (no prose), each item:
  {{"severity": "P1|P2|P3|P4", "file": "path", "line": <int|null>, "title": "...", "detail": "..."}}
Empty array only if the diff is genuinely flawless.

PR #{n} ({repo}), base={base}:
--- DIFF ---
{diff}
"""

JUDGE_PROMPT = """You are the lead reviewer ("master") making the FINAL call on review findings for
a pull request diff, held to the standard "nothing left to improve". The findings below come from
several independent reviewers (Claude, Codex, SonarCloud). For EACH finding, decide on the merits
whether it is a REAL issue worth fixing.

The bar is completeness, not triage. Keep every genuine finding — correctness, security, error
handling, test gaps, docs, naming, style, simplifications, and polish ALL count and all get fixed.
"Not worth the churn" and "could do later" are NOT valid reasons to dismiss. A genuine issue flagged
by only ONE reviewer MUST be marked real — never dismiss it just because the others missed it (that
is exactly what a diverse reviewer is there to catch).

Mark real=false ONLY when the finding is a genuine FALSE POSITIVE — you re-examined and the code is
actually correct, or the suggestion would make it worse. When unsure, mark it real.

Return ONLY a JSON array (no prose):
  [{{"id": <int>, "real": true|false, "severity": "P1|P2|P3|P4", "reason": "..."}}]

Findings:
{findings}
--- DIFF ---
{diff}
"""


@dataclass
class Finding:
    reviewer: str
    severity: str
    file: str
    line: int | None
    title: str
    detail: str = ""


@dataclass
class PanelResult:
    sonar_gate: str = "skipped"          # OK | ERROR | skipped | no-analysis
    sonar_findings: list = field(default_factory=list)   # Finding (the hard gate's issues)
    skipped: list = field(default_factory=list)          # ["codex: CLI absent", ...]


# ----------------------------------------------------------------------------- helpers

def sh(args: list[str], **kw) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True, **kw).stdout


def load_repo_cfg(name: str) -> dict:
    """The panel is READ-ONLY, so an unconfigured repo is not an error here —
    it runs on the built-in defaults (claude + codex, sonarqube off). This is
    the whole reason /panel and /panel-review-pr now work in any repo."""
    try:
        return resolve_repo(name)
    except RepoNotFound as e:
        sys.exit(str(e))


def _balanced_span(text: str, open_ch: str, close_ch: str) -> str | None:
    """Return the first top-level balanced open_ch..close_ch span (string-aware),
    or None. Beats a greedy `open.*close` regex: LLMs love to wrap their JSON in
    prose or ``` fences that ALSO contain brackets, and a greedy match then spans
    from the first stray bracket to the last, producing invalid JSON. Scanning for
    a balanced span (and skipping brackets inside JSON string literals) finds the
    real array/object instead."""
    depth = 0
    start = -1
    in_str = esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            if depth == 0:
                start = i
            depth += 1
        elif ch == close_ch and depth:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def extract_json_array(raw: str) -> list | None:
    """Best-effort parse of a JSON array from an LLM reply. Tolerates ``` fences
    and surrounding prose. Returns the parsed list, or None when no valid array is
    present (so callers can tell "parsed empty → flawless" apart from "unparseable
    → retry / keep raw text", rather than silently dropping the reviewer's work)."""
    if not raw:
        return None
    for candidate in (_balanced_span(raw, "[", "]"), raw.strip()):
        if not candidate:
            continue
        try:
            val = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(val, list):
            return val
    return None


def parse_findings(reviewer: str, raw: str) -> list[Finding] | None:
    """Parse a reviewer's JSON array into Findings. Returns None when the reply has
    no valid JSON array (caller retries, then falls back to a raw-text finding) —
    distinct from [] which means the reviewer ran and found nothing."""
    items = extract_json_array(raw)
    if items is None:
        return None
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        out.append(Finding(
            reviewer=reviewer,
            severity=str(it.get("severity", "P3")).upper(),
            file=str(it.get("file", "?")),
            line=it.get("line") if isinstance(it.get("line"), int) else None,
            title=str(it.get("title", "")).strip(),
            detail=str(it.get("detail", "")).strip(),
        ))
    return out


def _raw_finding(reviewer: str, text: str) -> Finding:
    """Wrap an unparsed reviewer reply as a single markdown finding so its work is
    surfaced to the judge + fixer instead of dropped. The reviewer is baked into
    `file` so two reviewers' raw dumps don't collapse into one dedup bucket."""
    first = next((ln.strip(" -*#\t") for ln in text.splitlines() if ln.strip()),
                 "review notes")
    return Finding(
        reviewer=reviewer,
        severity="P3",
        file=f"(unstructured:{reviewer})",
        line=None,
        title=f"{reviewer} review notes (unparsed): {first}"[:80],
        detail=text[:RAW_DETAIL_CHARS],
    )


# ----------------------------------------------------------------------------- reviewers

# Reasoning levels each CLI accepts for the shared `effort` config key — codex
# spells it `model_reasoning_effort`, pi spells it `--thinking`, and the two sets
# genuinely differ (pi has off/minimal, codex has ultra), so they are listed per
# CLI rather than unioned. Per-MODEL support is narrower still and moves with the
# fleet (gpt-5.6-luna takes `max` but not `ultra`), so this only catches typos —
# the API rules on the model/effort pair and its sentence is surfaced verbatim.
CODEX_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
PI_EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
AGY_EFFORTS = ("low", "medium", "high")
EFFORTS = {"codex": CODEX_EFFORTS, "pi": PI_EFFORTS, "antigravity": AGY_EFFORTS}


def cli_hint(cmd_name: str, err: str, model: str) -> str:
    """Point at the actual cause. This used to append '(auth? run `codex login`)'
    to EVERY non-zero codex exit, which is a confident wrong answer whenever the
    real problem was a pinned model the installed CLI is too old to use — the one
    failure a pinned slug is most likely to produce."""
    if cmd_name != "codex" or "exited" not in err:
        return ""
    low = err.lower()
    if "newer version" in low or "unknown model" in low or "not supported" in low:
        pin = f"`{model}`" if model else "the pinned model"
        return (f" — {pin} is unusable by the installed codex; upgrade the CLI "
                "(`codex --version`) or clear reviewers.codex.model")
    if any(w in low for w in ("401", "unauthorized", "token", "auth", "login")):
        return " (auth? run `codex login`)"
    return ""


def is_rejection(stderr: str) -> bool:
    """Did the server refuse the REQUEST (as opposed to failing to serve it)?
    A 4xx invalid-request — the shape a bad model pin takes — is deterministic,
    so it is worth distinguishing from the rate limits and blips that retrying
    exists for. 429 is excluded on purpose: that one IS worth another go."""
    low = stderr.lower()
    return ('"status":400' in low.replace(" ", "")
            or "invalid_request_error" in low
            or "requires a newer version" in low)


def stderr_gist(stderr: str, limit: int = 200) -> str:
    """The most INFORMATIVE stderr line, not blindly the last one.

    A CLI's real complaint is routinely followed by teardown noise, and codex is
    the worst case: a client older than its own models cache logs a decode error
    ("unknown variant `max`") on every single run, plus websocket teardown lines
    — so the naive tail reported that housekeeping and buried the sentence that
    actually explains the failure. Where the line carries a JSON error envelope
    we lift its `message`, which is how a pinned-model rejection reads as
    "The 'gpt-5.6-luna' model requires a newer version of Codex" rather than 200
    characters of serialised envelope."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    noise = ("failed to load models cache", "failed to refresh available models",
             "worker quit with fatal", "failed to connect to websocket")
    signal = [ln for ln in lines if not any(n in ln for n in noise)] or lines
    errors = [ln for ln in signal if "error" in ln.lower()]
    pick = (errors or signal)[-1]
    msg = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.){4,400})"', pick)
    return (msg.group(1) if msg else pick)[:limit]


def run_cli(args: list[str], label: str, timeout: int = CLI_TIMEOUT,
            attempts: int = 3, stdin_text: str | None = None) -> tuple[str | None, str | None]:
    """Run a headless CLI, returning (stdout, error_reason); error_reason is
    None on success. Retries transient failures (non-zero exits such as rate
    limits, and OS errors) up to `attempts` times with no delay — these fail
    fast, so retrying is cheap and recovers the common flake. A full timeout is
    NOT retried (it already burned the whole budget; retrying just doubles the
    wall-clock).

    `stdin_text` is how a prompt reaches a CLI that accepts one there, which is
    the only way to hand a reviewer a diff larger than the kernel's per-argument
    limit (see ARGV_PROMPT_MAX_BYTES). It does NOT weaken the guard that stdin
    is otherwise DEVNULL: subprocess writes the string and closes the pipe, so a
    CLI that decides to prompt for more reads EOF instead of hanging the panel
    on an inherited terminal.

    The timeout is deliberately generous. It exists to stop a wedged process
    hanging the panel forever, NOT to bound how long a reviewer may think: at
    10 minutes codex on a top-tier model at `max` effort routinely lost its
    seat on real diffs, which costs a whole vendor's eyes to save wall-clock we
    weren't waiting on anyway — the reviewers run concurrently, so a slow seat
    only extends the run when it is the slowest one. The reason string is specific (timeout / exit code + stderr
    tail / OSError) so callers can SURFACE why a step degraded instead of
    reporting a bare 'unavailable'. A request the server has REJECTED on its
    merits is not retried either: a bad model pin fails identically all three
    times, so retrying only triples the wait for a certainty."""
    last = f"{label}: no attempt made"
    feed = {"input": stdin_text} if stdin_text is not None else {"stdin": subprocess.DEVNULL}
    for _ in range(max(1, attempts)):
        try:
            proc = subprocess.run(args, capture_output=True, text=True,
                                  timeout=timeout, **feed)
        except subprocess.TimeoutExpired:
            return None, f"{label}: timed out after {timeout}s"
        except OSError as e:
            # errno and strerror, not the bare class name: "OSError" sent three
            # people looking for a crash that was "Argument list too long", and
            # everything needed to name it was already on the exception.
            why = " ".join(str(x) for x in (e.errno, e.strerror or e) if x)
            last = f"{label}: OSError {why}"[:300]
            continue
        if proc.returncode != 0:
            msg = stderr_gist(proc.stderr or "")
            last = f"{label}: exited {proc.returncode}" + (f" ({msg})" if msg else "")
            if is_rejection(proc.stderr or ""):
                return None, last
            continue
        return proc.stdout, None
    return None, last


def record_run(payload: dict) -> None:
    """Record this run on the quarterback board, best-effort.

    A panel run is a controlled comparison — one diff, several models, one judge
    ruling each finding real or not — and it used to evaporate when the process
    exited. Recording it is what turns "which reviewer is worth its cost" from an
    impression into a query (the board's /panel page aggregates it).

    Piped through `qb record-review` rather than POSTed here, because *which*
    board this machine belongs to is site configuration: the fleet has more than
    one, deliberately disjoint, and qb-env's rule is that an unset URL is an
    error and never a guess. Re-deriving that in Python is how review data ends
    up on another island's board.

    Never raises and never blocks the review: telemetry that can fail a run that
    already succeeded is worse than no telemetry.
    """
    if not shutil.which("qb"):
        return
    try:
        proc = subprocess.run(["qb", "record-review"], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"panel: run not recorded ({e.__class__.__name__})", file=sys.stderr)
        return
    # qb exits 0 whether or not the board answered, and says which on stderr; the
    # note is worth surfacing (a board that has been down for a week is invisible
    # otherwise) but is never an error here.
    note = (proc.stdout or proc.stderr or "").strip().splitlines()
    if note:
        print(f"panel: {note[-1]}", file=sys.stderr)


def diff_budget(block: dict, key: str, fallback: int, notes: list[str]) -> int:
    """How much diff one model is given, from config, with the inherited value as
    the fallback.

    Any positive value is honoured — the config wins, and the CONSEQUENCE is what
    gets surfaced: an under-budget diff is reported as truncated, per reviewer,
    with the budget that cut it. There is deliberately no lower sanity bound. One
    was tried (1,000 chars) and it was decoration: the plausible slip is a dropped
    zero, 60_000 -> 6_000, which clears any such floor and gets honoured anyway,
    while the value the floor did catch is one nobody types. Overriding a number
    someone explicitly wrote, using a number this file invented, is also the
    opposite of what the rest of the panel does with a config it dislikes.

    Only what cannot be a budget at all is refused: a non-number, or <= 0 (which
    would send an empty diff and produce a confident review of nothing). Those
    fall back and SAY so — silently honouring them reviews a fragment, silently
    dropping them leaves you believing a budget you never got.

    There is one ceiling this cannot see, and it belongs to the caller: `agy`'s
    prompt travels in argv, so the kernel caps it however large a budget says
    (see fit_argv_budget). That clamp is applied per reviewer, after this, and
    reported the same way — as truncation with a reason, not as a refusal."""
    raw = block.get(key)
    if raw is None or raw == "":
        return fallback
    n = None
    if not isinstance(raw, bool) and isinstance(raw, (int, str)):
        try:
            n = int(raw)
        except ValueError:
            n = None
    if n is None:
        notes.append(f"`{key}`={raw!r} is not a number — using {fallback:,}")
        return fallback
    if n <= 0:
        notes.append(f"`{key}`={n:,} would send no diff at all — using {fallback:,}")
        return fallback
    return n


def fit_argv_budget(render, budget: int) -> int:
    """The largest diff budget <= `budget` whose rendered prompt still fits in one
    argv element, for the seat whose prompt has nowhere else to go.

    This is the same rule diff_budget follows and the reason it can now keep it:
    a budget over the kernel's limit USED to be honoured right up to execve and
    then kill the reviewer with an opaque error. Here it becomes ordinary
    truncation with the consequence surfaced — the config still wins as far as
    the machine allows, and the report says where the machine stopped it.

    `render` renders the whole prompt from a budget, because the ceiling applies
    to the prompt, not the diff: the template counts, and so does the difference
    between characters and the bytes they encode to (this repo's own comments are
    full of em dashes, each of which is three bytes and one char).

    Shrinking by the byte overflow converges in one pass — a char is never fewer
    than one byte, so dropping N chars drops at least N bytes — but the loop is
    kept for the pathological case where the template alone is near the limit."""
    for _ in range(8):
        over = len(render(budget).encode()) - ARGV_PROMPT_MAX_BYTES
        if over <= 0:
            return budget
        budget = max(0, budget - over)
    return budget


def reviewer_label(name: str, model: str, effort: str = "") -> str:
    """`codex (gpt-5.6-luna, high)` — the report says WHICH brain reviewed.

    Findings keep the bare vendor name for attribution; this is for the header
    and the skip lines, where "codex ran" is not the same claim as "codex ran on
    the model you pinned"."""
    spec = ", ".join(x for x in (model, effort) if x)
    return f"{name} ({spec})" if spec else f"{name} (CLI default)"


def codex_args(model: str, effort: str) -> list[str]:
    """codex exec argv. Both knobs are optional and independent: effort is a
    `-c` config override rather than a flag, and applies to the CLI's default
    model just as well as to a pinned one.

    Takes no prompt: `codex exec` with no positional argument reads its
    instructions from stdin, which is where the diff goes. The parameter is gone
    rather than ignored so the argv-limit bug cannot be reintroduced by passing
    one (see ARGV_PROMPT_MAX_BYTES)."""
    args = ["codex", "exec"]
    if model:
        args += ["--model", model]
    if effort:
        args += ["-c", f"model_reasoning_effort={effort}"]
    return args


def antigravity_args(model: str, effort: str, prompt: str,
                     timeout: int = CLI_TIMEOUT) -> list[str]:
    """`agy` argv — Google's Antigravity CLI, which replaced gemini-cli in this
    seat. `-p` is its non-interactive print mode.

    This is the ONE seat whose prompt travels in argv: `agy` has no way to read
    one from anywhere else — not stdin, not a `@file`, not a `--prompt-file`.
    So it is also the one seat that can hit the kernel's per-argument limit, and
    the caller clamps its diff to ARGV_PROMPT_MAX_BYTES before rendering.

    `--mode plan` is NOT a sandbox, despite reading like one: with permissions
    granted, plan mode writes files. What actually keeps this reviewer off the
    tree is that headless print mode cannot prompt for a tool permission, so any
    tool needing one is auto-denied — and the diff is in the prompt, so it needs
    no tool anyway. Plan mode is kept for the narrower thing it does do (biasing
    it away from proposing edits), not as the guarantee. Anyone adding
    `--dangerously-skip-permissions` here removes the real guard: measured, that
    turns the reviewer into an agent that runs the test suite against the dev
    database and reviews the checkout instead of the diff.

    `--print-timeout` is passed because `agy` otherwise aborts itself at 5m0s
    while run_cli is still patiently waiting out its own much longer bound — a
    reviewer that reads as dead when it was only slow. It takes a Go duration,
    hence the `s` suffix.

    Left on the default `--output-format text` rather than `json`: the JSON mode
    wraps the reply in {response, status, usage, ...}, which would hide the
    findings array inside an escaped string where parse_findings' balanced-bracket
    scan cannot see it. Text mode puts the array straight on stdout, which is what
    every other seat here produces and what the parser is written against.

    Unlike gemini-cli, `agy` fails loudly on an unknown model instead of silently
    serving a different one, so a pinned slug that stops existing shows up as a
    dead reviewer rather than a quietly wrong one. Its effort scale is only
    low/medium/high (see EFFORTS) — narrower than codex's or pi's.
    """
    args = ["agy", "--mode", "plan", "--print-timeout", f"{timeout}s", "-p", prompt]
    if model:
        args += ["--model", model]
    if effort:
        args += ["--effort", effort]
    return args


def pi_args(model: str, effort: str) -> list[str]:
    """pi argv. `-p` is its non-interactive mode; `--no-tools` is what makes it a
    REVIEWER — pi ships read/bash/edit/write, and a panel member has no business
    editing the tree it is reviewing. The diff arrives on stdin, so it needs no
    tools to do the job, and `--no-tools` is a real guarantee that it has none.

    `--no-session` keeps a review out of the session store: a panel run is not a
    conversation anyone resumes, and one runs per PR.

    pi reaches many providers, so `model` here is a full `provider/id` pattern
    (`openrouter/moonshotai/kimi-k3`) rather than a bare slug, and its thinking
    level is spelled `--thinking` where codex spells it `model_reasoning_effort`.
    Same knob, same config key (`effort`), different word on each CLI.

    Takes no prompt, for the same reason codex_args does not: `pi -p` reads it
    from stdin."""
    args = ["pi", "-p", "--no-session", "--no-tools"]
    if model:
        args += ["--model", model]
    if effort:
        args += ["--thinking", effort]
    return args


def select_reviewers(rev: dict, spec: str | None) -> tuple[set[str], str | None]:
    """Which panel members run: the repo's `.harness-rules` by default, or exactly
    the ones named in `--reviewers`. Returns (selected, override_note).

    The flag REPLACES the config rather than filtering it, so `--reviewers codex`
    runs codex even in a repo whose rules disable it. Naming a reviewer IS the
    request to run it — a flag that could only ever narrow would silently do
    nothing in the repo where you most want it, the one that has it turned off.
    An unknown name is a hard error rather than a silent skip: `--reviewers
    antigravty` must not quietly produce a one-reviewer panel that reads like two.
    """
    if spec is None:
        return {n for n in ALL_REVIEWERS if rev.get(n, {}).get("enabled")}, None
    names = [s.strip().lower() for s in spec.split(",") if s.strip()]
    if not names:
        raise SystemExit("--reviewers: no reviewer named — expected a comma-separated "
                         f"list of {', '.join(ALL_REVIEWERS)}")
    unknown = [n for n in names if n not in ALL_REVIEWERS]
    if unknown:
        raise SystemExit(f"--reviewers: unknown reviewer {', '.join(repr(u) for u in unknown)}"
                         f" — expected {', '.join(ALL_REVIEWERS)}")
    return set(names), ("panel members set by --reviewers: " + ", ".join(sorted(set(names)))
                        + " (repo config overridden)")


def review_llm(cmd_name: str, model: str, prompt: str,
               effort: str = "") -> tuple[list[Finding], str | None, int]:
    """Run a headless LLM CLI reviewer. Returns (findings, skip_reason, duration_ms).

    Duration is wall-clock for this member's whole turn — every CLI attempt it
    made, including the reparse retry below, because a reviewer that only lands
    on the second try genuinely costs twice. It is measured even on the failure
    paths: how long a member took to NOT produce findings is exactly what you
    want to know about a reviewer that times out. Config errors that return
    before any process starts report ~0, which is honest — nothing ran.

    This is the cost side of the board's scorecard. "Finds more" is only half an
    answer; the panel is a choice about where to spend wall-clock, and until this
    was measured the /panel leaderboard could rank a member top on findings while
    silently being the one that made every review twice as slow.
    """
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    label = reviewer_label(cmd_name, model, effort)
    # A typo'd effort is a config error, so it is answered as one — before we
    # spend three CLI invocations discovering it downstream. Membership only:
    # which efforts a given model accepts is the API's call, not ours (luna
    # takes `max` but not `ultra`), and that answer arrives via stderr_gist.
    valid = EFFORTS.get(cmd_name, ())
    if effort and effort not in valid:
        expected = ("expected one of " + ", ".join(valid) if valid
                    else f"{cmd_name} takes no reasoning effort")
        return [], f"{label}: unknown reasoning effort {effort!r} — {expected}", elapsed()
    if not shutil.which(CLI_BIN.get(cmd_name, cmd_name)):
        return [], f"{label}: CLI absent", elapsed()
    # The prompt goes on stdin wherever the CLI will take it there — which is
    # everywhere but `agy`. That is not a style choice: a diff big enough to be
    # worth a panel is big enough to exceed the kernel's per-argument limit, and
    # in argv that failure lands at execve, before the reviewer exists, as an
    # error with nothing in it. On stdin there is no such ceiling.
    stdin_text: str | None = prompt
    if cmd_name == "claude":
        args = ["claude", "-p", "--model", model]
    elif cmd_name == "antigravity":
        args, stdin_text = antigravity_args(model, effort, prompt), None
    elif cmd_name == "pi":
        args = pi_args(model, effort)
    else:
        args = codex_args(model, effort)
    out, err = run_cli(args, label, stdin_text=stdin_text)
    if err:
        err += cli_hint(cmd_name, err, model)
        return [], err, elapsed()
    findings = parse_findings(cmd_name, out)
    if findings is None:
        # Unparseable JSON — give the reviewer one more shot (a common flake is a
        # stray prose preamble the model omits on a retry), then, rather than drop
        # its work, keep the raw reply as a single markdown finding for the judge.
        out2, err2 = run_cli(args, label, attempts=1, stdin_text=stdin_text)
        if not err2 and out2:
            retried = parse_findings(cmd_name, out2)
            if retried is not None:
                return retried, None, elapsed()
            out = out2
        text = (out or "").strip()
        if not text:
            return [], f"{label}: no parseable findings and empty output", elapsed()
        return [_raw_finding(cmd_name, text)], None, elapsed()
    return findings, None, elapsed()


def resolve_token(sonar: dict, repo_path: str = "") -> str:
    """SONAR token, in order:

        1. the process env var named by `token_env`
        2. the repo's `.env`            <- the work-machine source
        3. the 0600 cache in ~/.cache/loops
        4. `op read` of `token_op_ref`  (write-through to the cache)

    So a work machine, which has no 1Password/sops and no login-time export,
    just carries the value in the repo's own gitignored `.env`; `op signin` is
    needed once on a personal machine and later runs read the cache.

    `.env` sits BELOW the real env var rather than above it, which is the one
    place this departs from "look in .env first". An exported SONARQUBE_TOKEN is
    an explicit, deliberate override (zeus sets one at login), and a stale `.env`
    left in a checkout silently shadowing it would surface as an unexplained 401
    from SonarCloud. Nothing is lost on a work machine, where no such export
    exists and resolution falls straight through to `.env`. This also matches
    python-dotenv's default (`override=False`).

    Never logged. Delete the cache file to refresh after a token rotation."""
    env = os.environ.get(sonar.get("token_env", ""), "")
    if env:
        return env

    name = sonar.get("token_env", "")
    if repo_path and name:
        if harness_rules.dotenv_is_tracked(repo_path):
            print(f"  ! {repo_path}/.env is COMMITTED to git — a credential is in "
                  f"the repo's history. Add it to .gitignore and rotate the token.",
                  file=sys.stderr)
        dotenv = harness_rules.read_dotenv(repo_path)
        if dotenv.get(name):
            return dotenv[name]

    key = sonar.get("project_key", "") or "default"
    cache = Path.home() / ".cache" / "loops" / f"sonar-{key}.token"
    if cache.is_file():
        tok = cache.read_text().strip()
        if tok:
            return tok

    ref = sonar.get("token_op_ref")
    if not ref:
        return ""
    try:
        # DEVNULL + timeout for the same reasons run_cli has them, and they bite
        # harder here: a locked 1Password session makes `op read` PROMPT, and with
        # the parent's stdin inherited that blocks the entire panel indefinitely on
        # the one step nobody is watching. EOF turns a locked session into a fast
        # failure and a skipped Sonar gate, which is a reported degradation rather
        # than a hang.
        tok = subprocess.run(["op", "read", ref], capture_output=True, text=True,
                             check=True, stdin=subprocess.DEVNULL,
                             timeout=30).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return ""
    if tok:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(tok)
            cache.chmod(0o600)
        except OSError:
            pass  # caching is best-effort; token still usable this run
    return tok


def _ssl_context() -> ssl.SSLContext:
    """TLS context with a CA bundle that actually exists. Python's baked-in
    default openssl path (e.g. /etc/ssl/cert.pem) is absent on NixOS, so
    urllib verification fails out of the box; prefer SSL_CERT_FILE, then
    certifi, then the common system bundles."""
    env = os.environ.get("SSL_CERT_FILE")
    if env and os.path.isfile(env):
        return ssl.create_default_context(cafile=env)
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    for p in ("/etc/ssl/certs/ca-certificates.crt",
              "/etc/ssl/certs/ca-bundle.crt",
              "/etc/pki/tls/certs/ca-bundle.crt"):
        if os.path.isfile(p):
            return ssl.create_default_context(cafile=p)
    return ssl.create_default_context()  # last resort: platform default


def _diff_added_lines(diff: str) -> dict[str, set[int]]:
    """Map each changed file (repo-relative, the `b/` side) to the set of line
    numbers it ADDS on the new-file side — the code this PR actually wrote. Used
    to scope SonarCloud's main-branch issues down to the PR's own lines (its
    "new code" view) rather than every pre-existing issue in a touched file."""
    out: dict[str, set[int]] = {}
    cur = None
    newln = 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            cur = parts[1].strip() if len(parts) == 2 else None
        elif cur is None or line.startswith(("+++", "---", "\\")):
            continue
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            newln = int(m.group(1)) if m else 0
        elif line.startswith("+"):
            out.setdefault(cur, set()).add(newln)
            newln += 1
        elif line.startswith("-"):
            pass  # old-side only — new-side line counter doesn't advance
        else:
            newln += 1  # context line advances the new-side counter
    return out


_SONAR_SEV = {"BLOCKER": "P1", "CRITICAL": "P1", "MAJOR": "P2", "MINOR": "P3", "INFO": "P3"}


def _sonar_findings(issues: list[dict]) -> list[Finding]:
    return [Finding(
        reviewer="sonarqube",
        severity=_SONAR_SEV.get(i.get("severity", "MINOR"), "P3"),
        file=(i.get("component", "").split(":")[-1] or "?"),
        line=i.get("line"),
        title=i.get("message", "")[:80],
        detail=i.get("rule", ""),
    ) for i in issues]


def _try(fn, *a):
    """Call fn(*a), or None if the API refused. Used where a partial answer beats
    no answer — the caller counts the Nones and says how many it lost."""
    try:
        return fn(*a)
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        return None


def review_sonarqube(sonar: dict, pr: dict,
                     changed_lines: dict[str, set[int]],
                     repo_path: str = "") -> tuple[str, list[Finding], list[Finding], str | None]:
    """Query SonarCloud/SonarQube for the PR.

    `pr` carries what identifies the change: `number`, `base`, and optionally
    `head` / `head_sha`. It is a dict rather than four more positional arguments
    because three tiers each need a different subset, and a five-argument call
    site is where the wrong branch name gets passed unnoticed.

    Returns (gate_status, hard_findings, soft_findings, skip_reason). Three tiers,
    best evidence first:

    1. The PR's own analysis: quality gate (HARD) + its PR issues.
    2. The HEAD BRANCH's analysis, if one exists at the PR's head commit: its
       gate is a real gate on this change's new code, so also HARD. Issues are
       scoped to the lines the PR adds, since a branch analysis reports the whole
       branch. This tier exists because PR analysis is not always available —
       where the SonarCloud org is bound to a different platform, a GitHub PR key
       cannot be resolved at all, and `sonar.branch.name` is the way in.
    3. Otherwise: open issues on the lines this PR ADDS, read from the BASE
       branch, as SOFT findings (judged on merits like any reviewer). The base
       branch's quality gate is NOT applied — it reflects all of that branch, not
       this PR, and would fail every PR.

    The fallback reads the branch this PR MERGES INTO (`base`), not the project's
    default branch, and that is the difference between findings and silence. On
    lexray, `test` is the integration branch and `main` lags it by a release
    train: measured on PR #1625 (2026-08-14), the default branch returned 33
    issues of which 0 fell on a line the PR added, while `base`=test returned 11
    of which 2 did. Reading a branch the PR is not based on doesn't merely add
    noise — stale line numbers stop intersecting the diff at all, so the reviewer
    reports nothing and reads as working.

    A base that Sonar has never analysed (an epic/stacked branch) answers 200 with
    total=0 rather than erroring, so it is checked against project_branches/list
    first and demoted to the default branch with a note. Silent zero is the one
    outcome this must never produce, because it is indistinguishable from a clean
    PR.
    """
    host = sonar.get("host") or os.environ.get(sonar.get("host_env", ""), "")
    org = sonar.get("organization", "")
    key = sonar.get("project_key", "")
    if not host:
        return "skipped", [], [], "sonarqube: host unset"
    if not key or key.startswith("TODO"):
        return "skipped", [], [], "sonarqube: project_key not confirmed"
    token = resolve_token(sonar, repo_path)
    if not token:
        return "skipped", [], [], ("sonarqube: token unavailable "
                                   "(env unset, no .env entry, op not signed in)")

    auth = base64.b64encode(f"{token}:".encode()).decode()
    hdr = {"Authorization": f"Basic {auth}"}
    org_q = f"&organization={org}" if org else ""
    ctx = _ssl_context()

    def api(path: str) -> dict:
        url = f"{host.rstrip('/')}/api/{path}"
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            return json.loads(r.read().decode())

    pr_number = pr.get("number")
    base = pr.get("base", "")
    head = pr.get("head", "")
    head_sha = pr.get("head_sha", "")

    # 1) The PR's own analysis (hard quality gate), if it was scanned.
    try:
        gate = api(f"qualitygates/project_status?projectKey={key}&pullRequest={pr_number}")
        status = gate.get("projectStatus", {}).get("status", "no-analysis")
        issues = api(f"issues/search?componentKeys={key}{org_q}"
                     f"&pullRequest={pr_number}&resolved=false&ps=100")
        return status, _sonar_findings(issues.get("issues", [])), [], None
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return "skipped", [], [], f"sonarqube: HTTP {e.code}"
        # 404 == no analysis for this PR; try the head branch, then the base.
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        return "skipped", [], [], f"sonarqube: {e.__class__.__name__}"

    files = sorted(changed_lines)

    # 2) The head branch's own analysis. Its gate judges this change's new code
    #    against the base, so it is a REAL gate — but only for the commit it
    #    actually ran on. Branch analyses persist and are not superseded by a
    #    push, so an analysis three commits stale would gate confidently on code
    #    that is no longer there. Verified against the PR's head SHA, and
    #    declined (not reported stale-but-used) when they disagree.
    branch_note = None
    if head:
        try:
            branches = api(f"project_branches/list?project={key}").get("branches", [])
            entry = next((b for b in branches if b.get("name") == head), None)
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
            entry = None
        if entry:
            analysed = (entry.get("commit") or {}).get("sha", "")
            if head_sha and analysed and analysed != head_sha:
                branch_note = (f"sonarqube: branch analysis of '{head}' is at "
                               f"{analysed[:8]}, PR head is {head_sha[:8]} — stale, "
                               f"not used as a gate (rescan to get one)")
            else:
                try:
                    status = (entry.get("status") or {}).get("qualityGateStatus") or "no-analysis"
                    # safe="" so a branch name is fully escaped. The default
                    # leaves "/" alone, which is harmless in a query string —
                    # but a branch called `feat/a&b` would silently truncate the
                    # parameter and query the wrong branch.
                    branch_q = urllib.parse.quote(head, safe="")
                    raw = api(f"issues/search?componentKeys={key}{org_q}"
                              f"&branch={branch_q}&resolved=false&ps=500")
                    hard = [f for f in _sonar_findings(raw.get("issues", []))
                            if f.line in changed_lines.get(f.file, ())]
                    return status, hard, [], None
                except (urllib.error.HTTPError, urllib.error.URLError,
                        json.JSONDecodeError) as e:
                    branch_note = (f"sonarqube: head-branch analysis unreadable "
                                   f"({e.__class__.__name__})")

    # 3) Fallback: open issues on the PR's base branch, on the lines it adds (soft).
    if not files:
        return "no-pr-analysis", [], [], (
            f"sonarqube: PR #{pr_number} not scanned and no changed files to map")

    # Which branch to read. `fallback_branch` in .harness-rules pins it; otherwise
    # the PR's own base. Verified against the analysed set, because an unanalysed
    # branch returns an empty result rather than an error.
    want = sonar.get("fallback_branch") or base
    # A stale or unreadable head-branch analysis is the reason we are down here,
    # so it travels with the fallback's own caveats rather than being dropped.
    note = branch_note
    if want:
        try:
            branches = api(f"project_branches/list?project={key}").get("branches", [])
            known = {b.get("name") for b in branches}
            if want not in known:
                default = next((b.get("name") for b in branches if b.get("isMain")), "")
                note = ((note + "; ") if note else "") + (
                    f"sonarqube: base '{want}' has no Sonar analysis — read "
                    f"'{default or 'the default branch'}' instead (findings may be stale)")
                want = ""
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
            want = ""  # can't verify — the default branch is the safe read

    def issues_for(comps: list[str]) -> list[dict]:
        params = {"componentKeys": ",".join(f"{key}:{p}" for p in comps),
                  "resolved": "false", "ps": "500"}
        if org:
            params["organization"] = org
        if want:
            params["branch"] = want
        return api("issues/search?" + urllib.parse.urlencode(params)).get("issues", [])

    # One request for the whole component list, EXCEPT that Sonar refuses a list
    # mixing qualifiers ("All components must have the same qualifier, found
    # UTS,FIL") — which any PR touching both sources and tests does, i.e. nearly
    # every reviewable PR. There is no way to know a path's qualifier client-side
    # (it follows sonar.tests, which lives in the scanner's config, not here), so
    # the split is discovered from the refusal: a single component can never mix,
    # so retrying per file always resolves it.
    try:
        raw = issues_for(files[:100])
    except urllib.error.HTTPError as e:
        if e.code != 400:
            return "skipped", [], [], f"sonarqube: base-branch fallback failed (HTTP {e.code})"
        raw = []
        failed = 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            for got in ex.map(lambda p: _try(issues_for, [p]), files[:100]):
                if got is None:
                    failed += 1
                else:
                    raw.extend(got)
        if failed:
            note = ((note + "; ") if note else "") + \
                f"sonarqube: {failed}/{len(files[:100])} files unreadable"
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        return "skipped", [], [], f"sonarqube: base-branch fallback failed ({e.__class__.__name__})"

    # Keep only issues on lines this PR actually added — drop pre-existing ones.
    soft = [f for f in _sonar_findings(raw)
            if f.line in changed_lines.get(f.file, ())]
    return "no-pr-analysis", [], soft, note


def review_ci(gh_repo: str, pr_number: int) -> tuple[str, list[str], str | None]:
    """Fetch the PR's CI status via `gh pr checks`. Returns
    (status, failing, skip_reason); status is PASS | FAIL | PENDING | none | unknown
    and `failing` names the non-passing checks. This is a HARD-gate signal: a clean
    LLM/Sonar panel means little if CI (the repo's pytest run — slow tests and all)
    is red or still pending. Panel only SURFACES it; the merge gate itself lives in
    fix-and-land's own `gh pr checks` step. `gh pr checks` exits non-zero when checks
    fail/pend, but still prints the JSON, so we parse stdout regardless of exit code."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "checks", str(pr_number), "--repo", gh_repo,
             "--json", "name,bucket"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        return "unknown", [], f"ci: {e.__class__.__name__}"
    raw = (proc.stdout or "").strip()
    if not raw:
        # No JSON -> usually "no checks reported on the 'X' branch" (exit 1, stderr).
        tail = (proc.stderr or "").strip().splitlines()
        hint = tail[-1][:80] if tail else f"exit {proc.returncode}"
        if "no checks" in hint.lower():
            return "none", [], None
        return "unknown", [], f"ci: {hint}"
    try:
        checks = json.loads(raw)
    except json.JSONDecodeError:
        return "unknown", [], "ci: unparseable gh output"
    buckets = [str(c.get("bucket", "")).lower() for c in checks if isinstance(c, dict)]
    failing = [str(c.get("name", "?")) for c in checks
               if isinstance(c, dict) and str(c.get("bucket", "")).lower() == "fail"]
    if not buckets:
        return "none", [], None
    if "fail" in buckets:
        return "FAIL", failing, None
    if "pending" in buckets:
        return "PENDING", failing, None
    return "PASS", failing, None


# ----------------------------------------------------------------------------- synthesis

def _key(f: Finding) -> tuple:
    """Dedup bucket: same file + nearby line (±10) is treated as one issue."""
    return (Path(f.file).name, (f.line or 0) // 10)


def group_findings(llm_findings: list[Finding]) -> list[tuple[Finding, list[str]]]:
    """Dedup findings across reviewers. Returns (representative, [reviewers]).
    Reviewer count is a *confidence signal*, never a gate."""
    groups: dict[tuple, list[Finding]] = {}
    for f in llm_findings:
        groups.setdefault(_key(f), []).append(f)
    out = []
    for grp in groups.values():
        reviewers = sorted({f.reviewer for f in grp})
        rep = min(grp, key=lambda f: f.severity)  # P1 < P2 < P3 lexically
        out.append((rep, reviewers))
    out.sort(key=lambda e: e[0].severity)
    return out


def judge(groups: list[tuple[Finding, list[str]]], diff: str, model: str,
          budget: int = MAX_DIFF_CHARS) -> tuple[dict[int, dict], str | None]:
    """The 'master' adjudicates each finding on its merits. Returns
    (verdicts, skip_reason). skip_reason is None when the judge ran successfully
    (even if it dismissed nothing); otherwise it explains WHY the judge could not
    rule — CLI absent, timeout, crash, or unparseable output — so the caller can
    surface it rather than silently reporting a bare 'unavailable'. A real bug
    from a single reviewer is confirmed; only genuine false positives are dropped
    (style and polish are kept). When the judge can't rule, the caller keeps everything (we never
    silently suppress a finding). No findings -> ({}, None): nothing to judge."""
    if not groups:
        return {}, None
    if not shutil.which("claude"):
        return {}, "judge: claude CLI absent"
    listing = "\n".join(
        f"[{i}] {f.severity} {f.file}:{f.line or '?'} (via {', '.join(revs)}) — "
        f"{f.title} — {f.detail}"
        for i, (f, revs) in enumerate(groups))
    # On stdin, like the reviewers, and for a sharper reason: the judge's prompt
    # is the only one with a component no budget covers. The findings listing
    # grows with the panel's output, so a legal judge_max_diff_chars plus a long
    # panel could cross the argv limit on its own — and a judge that dies takes
    # every finding through UNADJUDICATED, which reads like a triaged review
    # rather than like a failure.
    prompt = JUDGE_PROMPT.format(findings=listing, diff=diff[:budget])
    args = ["claude", "-p"] + (["--model", model] if model else [])
    out, err = run_cli(args, "judge", stdin_text=prompt)
    if err:
        return {}, err
    parsed = extract_json_array(out)
    if parsed is None:
        return {}, "judge: no JSON verdict in output (unparseable)"
    verdicts: dict[int, dict] = {}
    for v in parsed:
        if isinstance(v, dict) and isinstance(v.get("id"), int):
            verdicts[v["id"]] = v
    return verdicts, None


# ----------------------------------------------------------------------------- run

def run(repo_name: str, pr_number: int, post: bool, json_out: bool = False,
        reviewers: str | None = None, json_file: str = "", record: bool = True) -> int:
    # Idempotency key for the board record, minted once per process so a retry of
    # the POST cannot double-count the run into the stats. A fresh panel run is a
    # genuinely new observation and gets a new key — re-reviewing a PR after a fix
    # loop is data, not a duplicate.
    run_key = uuid.uuid4().hex
    cfg = load_repo_cfg(repo_name)
    gh_repo = cfg["github"]
    rev = cfg["reviewers"]
    panel = cfg["review_panel"]
    # Resolved before anything is fetched, so a typo'd --reviewers fails on the
    # spot rather than after a PR read and a diff download.
    selected, override_note = select_reviewers(rev, reviewers)

    try:
        meta = json.loads(sh(["gh", "pr", "view", str(pr_number), "--repo", gh_repo,
                              "--json", "title,additions,deletions,baseRefName,"
                                        "headRefName,headRefOid"]))
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or "").strip().splitlines()
        sys.exit(f"panel: cannot read PR #{pr_number} in {gh_repo}"
                 + (f" — {tail[-1][:160]}" if tail else ""))
    title, base = meta["title"], meta["baseRefName"]
    changed = meta["additions"] + meta["deletions"]

    # Title-pattern skip (merges/promotes/format-the-world — not worth LLM review)
    for pat in panel.get("skip_title_patterns", []):
        if re.search(pat, title, re.I):
            print(f"[{repo_name}#{pr_number}] '{title[:50]}' matches skip pattern "
                  f"/{pat}/ — not worth panel review. Skipping.")
            return 0
    print(f"\n[{repo_name}#{pr_number}] {title[:60]}")
    print(f"  base={base}  changed={changed} lines\n")

    try:
        diff = sh(["gh", "pr", "diff", str(pr_number), "--repo", gh_repo])
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or "").strip().splitlines()
        sys.exit(f"panel: cannot fetch diff for PR #{pr_number} in {gh_repo}"
                 + (f" — {tail[-1][:160]}" if tail else ""))
    changed_lines = _diff_added_lines(diff)

    # Diff budgets: panel-wide value, then each model's own override. Every
    # reviewer used to get the same 60k prefix regardless of its context window.
    notes: list[str] = []
    panel_budget = diff_budget(panel, "max_diff_chars", MAX_DIFF_CHARS, notes)
    # Only for the reviewers actually running: a budget warning about a model
    # this run never asked for is noise, and a "truncated for antigravity" footnote
    # under a claude-only panel is a lie.
    budgets = {name: diff_budget(rev.get(name, {}), "max_diff_chars", panel_budget, notes)
               for name in LLM_REVIEWERS if name in selected}
    judge_budget = diff_budget(panel, "judge_max_diff_chars", panel_budget, notes)

    def prompt_for(budget: int) -> str:
        return REVIEW_PROMPT.format(n=pr_number, repo=gh_repo, base=base,
                                    diff=diff[:budget])

    # `agy` is the only reviewer whose prompt must travel in argv, so it is the
    # only one the kernel can veto. Clamp it to what execve will carry and say
    # so — the alternative, honouring the number and dying at exec, is how a
    # panel came to report "LLM reviewers ran: none" as a clean review.
    if "antigravity" in budgets:
        fitted = fit_argv_budget(prompt_for, budgets["antigravity"])
        if fitted < budgets["antigravity"]:
            notes.append(
                f"`max_diff_chars`={budgets['antigravity']:,} exceeds what fits in one "
                f"argv element ({ARGV_PROMPT_MAX_BYTES:,} bytes) — antigravity gets "
                f"{fitted:,}. It is the one CLI with no way to read a prompt off stdin.")
            budgets["antigravity"] = fitted

    truncated_for = {n: b for n, b in budgets.items() if len(diff) > b}
    truncated = bool(truncated_for)

    result = PanelResult()
    # Resolved ONCE, so the label in the report cannot drift from the model that
    # actually ran (the fallbacks live here, not in two places). Effort is a
    # knob codex, pi and antigravity share (spelled differently on each CLI, and
    # over a different scale — see EFFORTS); claude takes its own default reasoning.
    models = {n: rev.get(n, {}).get("model", "") for n in LLM_REVIEWERS}
    models["claude"] = rev.get("claude", {}).get("model", "sonnet")
    efforts = {n: rev.get(n, {}).get("effort", "") for n in EFFORTS}
    labels = {n: reviewer_label(n, models[n], efforts.get(n, "")) for n in LLM_REVIEWERS}

    tasks = {}
    with ThreadPoolExecutor(max_workers=len(ALL_REVIEWERS) + 1) as ex:
        # Every selected LLM reviewer runs — no de-minimis gate. If we asked for
        # the panel, we want each vendor's eyes regardless of diff size.
        for name in LLM_REVIEWERS:
            if name in selected:
                tasks[name] = ex.submit(review_llm, name, models[name],
                                        prompt_for(budgets[name]), efforts.get(name, ""))
        sonar_future = None
        if "sonarqube" in selected:
            sonar_future = ex.submit(
                review_sonarqube, rev.get("sonarqube", {}),
                {"number": pr_number, "base": base,
                 "head": meta["headRefName"], "head_sha": meta["headRefOid"]},
                changed_lines, cfg["path"])
        ci_future = ex.submit(review_ci, gh_repo, pr_number)

        llm_findings: list[Finding] = []
        ran_llm: list[str] = []
        llm_skipped: list[str] = []
        # Which brain each member actually used. Findings carry the bare vendor
        # name for attribution, which is the right grain for a report and the
        # wrong one for a record: "codex found 9 issues" means nothing six weeks
        # later without the model and effort behind it, and those drift (a repo
        # repins, a slug retires, --reviewers hand-picks a set).
        reviewer_meta: dict[str, dict] = {}
        for name, fut in tasks.items():
            finds, skip, duration_ms = fut.result()
            reviewer_meta[name] = {
                "model": models[name] or None,
                "effort": efforts.get(name) or None,
                "ran": not skip,
                "skip": skip,
                "max_diff_chars": budgets[name],
                "truncated": name in truncated_for,
                "duration_ms": duration_ms,
            }
            if skip:
                result.skipped.append(skip)
                llm_skipped.append(skip)
            else:
                ran_llm.append(labels[name])
                llm_findings.extend(finds)
        if sonar_future:
            gate, hard, soft, skip = sonar_future.result()
            result.sonar_gate = gate
            reviewer_meta["sonarqube"] = {"ran": gate != "skipped", "skip": skip}
            # The 4th value is a skip reason ONLY when the reviewer didn't run
            # (gate == "skipped"); otherwise it's a caveat about a run that DID
            # produce findings — a degraded base branch, files it couldn't read.
            # Branching on its mere presence would drop those findings on the
            # floor and report the reviewer as skipped, which is the silent
            # zero this whole path exists to avoid.
            if skip:
                result.skipped.append(skip)
            if gate != "skipped":
                # PR-scanned issues are the hard gate; base-branch fallback
                # issues are soft — judged on merits alongside the LLM reviewers.
                result.sonar_findings = hard
                llm_findings.extend(soft)
        ci_status, ci_failing, ci_skip = ci_future.result()
        if ci_skip:
            result.skipped.append(ci_skip)

    # Dedup, then let the master judge each finding on its merits (no consensus gate).
    groups = group_findings(llm_findings)
    verdicts, judge_skip = judge(groups, diff, panel.get("judge_model", ""),
                                 judge_budget)
    judged = judge_skip is None and bool(groups)
    to_fix, dismissed = [], []
    for i, (f, revs) in enumerate(groups):
        v = verdicts.get(i)
        if v is None:                       # no verdict → keep it (never suppress)
            to_fix.append((f, revs, "" if judged else "unjudged"))
        elif v.get("real", True):
            f.severity = str(v.get("severity", f.severity)).upper()
            to_fix.append((f, revs, v.get("reason", "")))
        else:
            dismissed.append((f, revs, v.get("reason", "")))
    to_fix.sort(key=lambda e: e[0].severity)

    def loc(f: Finding) -> str:
        return f"{f.file}:{f.line}" if f.line else f.file

    # ---- the run, as data. Built on every path, not just --json: it is what
    # --json prints, what --json-file writes, and what gets recorded on the
    # board. One structure, so the fix loop and the stats can never be looking
    # at different accounts of the same review.
    def ser(f: Finding, revs: list[str], reason: str) -> dict:
        return {"severity": f.severity, "file": f.file, "line": f.line,
                "title": f.title, "detail": f.detail, "reviewers": revs,
                "reason": reason}
    payload = {
        "repo": repo_name, "github": gh_repo, "pr": pr_number,
        "title": title, "base": base, "changed_lines": changed,
        "diff_truncated": truncated,
        "diff_chars": len(diff),
        "diff_budgets": {**budgets, "judge": judge_budget},
        "config_notes": notes,
        "sonar_gate": result.sonar_gate,
        "ci_status": ci_status,
        "ci_failing": ci_failing,
        "judged": judged,
        "judge_model": panel.get("judge_model", "") or None,
        "judge_skip": judge_skip,
        "reviewers_ran": ran_llm,
        "reviewers": reviewer_meta,
        "reviewers_selected": sorted(selected),
        "reviewers_override": override_note,
        "to_fix": [ser(f, revs, reason) for f, revs, reason in to_fix],
        "sonar_findings": [ser(f, [f.reviewer], f.detail)
                           for f in result.sonar_findings],
        "dismissed": [ser(f, revs, reason) for f, revs, reason in dismissed],
        "skipped": result.skipped,
        "run_key": run_key,
    }

    if json_file:
        # So a caller can have BOTH the PR comment and the machine-readable run.
        # Without it, --json suppresses the report and the only way to get both
        # was to review the PR twice — several CLI invocations, for a copy.
        try:
            Path(json_file).write_text(json.dumps(payload, indent=2))
        except OSError as e:
            print(f"panel: could not write {json_file} ({e.__class__.__name__})",
                  file=sys.stderr)

    if record:
        record_run(payload)

    # ---- machine-readable mode: emit findings as JSON, no report/post.
    # The /panel skill consumes this to drive its fix → verify → commit → push
    # loop.
    if json_out:
        print(json.dumps(payload, indent=2))
        return 0

    def conf(revs: list[str]) -> str:
        return f" _(via {', '.join(revs)}{' ⋆consensus' if len(revs) > 1 else ''})_"

    # ---- report
    lines = [f"## Reviewer panel — PR #{pr_number}", ""]
    ci_txt = {"PASS": "✅ PASS", "FAIL": "❌ FAIL", "PENDING": "⏳ pending",
              "none": "no checks reported", "unknown": "unknown"}.get(ci_status, ci_status)
    lines.append(f"**CI (`gh pr checks`, hard gate):** {ci_txt}")
    if ci_failing:
        lines.append("  - failing: " + ", ".join(ci_failing[:10])
                     + (f" (+{len(ci_failing) - 10} more)" if len(ci_failing) > 10 else ""))
    if ci_status in ("FAIL", "PENDING"):
        lines.append(f"  - ⚠️ CI is {ci_txt.split()[-1]} — do not merge until green, "
                     "even if the review below is clean")
    gate_txt = {"OK": "✅ PASS", "ERROR": "❌ FAIL"}.get(result.sonar_gate, result.sonar_gate)
    if result.sonar_gate in ("OK", "ERROR"):
        lines.append(f"**SonarCloud quality gate (hard):** {gate_txt}")
    elif result.sonar_gate == "no-pr-analysis":
        lines.append("**SonarCloud:** PR not scanned — base-branch issues on the "
                     "lines this PR adds, surfaced as soft findings (judged). "
                     "No hard gate: publish a PR analysis to get one.")
    else:
        lines.append(f"**SonarCloud:** {gate_txt}")
    lines.append(f"**LLM reviewers ran:** {', '.join(ran_llm) or 'none'}")
    if override_note:
        # Said on the PR, not just in the terminal: a reader of the comment needs
        # to know this panel was hand-picked before reading "reviewed by one".
        lines.append(f"  - {override_note}")
    # A lost reviewer is stated where the reviewer list is, not only in a section
    # at the foot of the report: a pinned model the CLI cannot use costs you a
    # whole vendor, and a one-reviewer panel that reads like a two-reviewer panel
    # is the failure mode worth shouting about.
    for skip in llm_skipped:
        lines.append(f"  - ⚠️ **not reviewed** — {skip}")
    if not groups:
        judge_txt = "n/a — no findings to judge"
    elif judged:
        judge_txt = reviewer_label("claude", panel.get("judge_model", ""))
    else:
        judge_txt = f"⚠️ {judge_skip} — all findings KEPT unjudged (re-run to get a verdict)"
    lines.append(f"**Master judge:** {judge_txt}")
    for note in notes:
        lines.append(f"  - ⚠️ config: {note}")
    if truncated:
        # Named per reviewer, since the budgets can now differ: "truncated" alone
        # would hide that one model saw the whole diff and another saw a third of
        # it, which is exactly what you need to know when they disagree.
        cut = ", ".join(f"{n} ({b:,})" for n, b in sorted(truncated_for.items()))
        lines.append(f"\n_diff is {len(diff):,} chars — truncated for {cut}_")

    lines.append(f"\n### To fix ({len(to_fix)}) — master-confirmed, any reviewer count")
    if to_fix:
        for f, revs, reason in to_fix:
            tail = f" — {reason}" if reason and reason != "unjudged" else ""
            lines.append(f"- **{f.severity}** `{loc(f)}` — {f.title}{conf(revs)}{tail}")
    else:
        lines.append("- none")

    if result.sonar_findings:
        lines.append(f"\n### SonarCloud issues ({len(result.sonar_findings)}) — part of the gate")
        for f in sorted(result.sonar_findings, key=lambda x: x.severity):
            lines.append(f"- {f.severity} `{loc(f)}` — {f.title}")

    if dismissed:
        lines.append(f"\n### Dismissed by master ({len(dismissed)})")
        for f, revs, reason in dismissed:
            lines.append(f"- ~~{f.severity} `{loc(f)}` — {f.title}~~{conf(revs)} — {reason}")

    if result.skipped:
        lines.append("\n### Skipped reviewers\n" +
                     "\n".join(f"- {s}" for s in result.skipped))

    report = "\n".join(lines)
    print(report)

    if post:
        # Bounded, and NOT check=True. This is the last step of a run that has
        # already succeeded and already printed its report above: a hung network
        # call here would block after every expensive thing is done, and raising
        # would throw away a completed review over a failed comment. The comment
        # is how the fix loop finds the findings, so a failure has to be LOUD —
        # but it degrades the run, it doesn't void it.
        try:
            proc = subprocess.run(["gh", "pr", "comment", str(pr_number), "--repo",
                                   gh_repo, "--body", report], capture_output=True,
                                  text=True, stdin=subprocess.DEVNULL, timeout=120)
            if proc.returncode == 0:
                print(f"\n(posted panel summary to {gh_repo}#{pr_number})")
            else:
                why = stderr_gist(proc.stderr or "") or f"exited {proc.returncode}"
                print(f"\n! panel summary NOT posted to {gh_repo}#{pr_number} ({why})"
                      f" — the report above is the only copy", file=sys.stderr)
        except (subprocess.TimeoutExpired, OSError) as e:
            why = "timed out after 120s" if isinstance(e, subprocess.TimeoutExpired) \
                else e.__class__.__name__
            print(f"\n! panel summary NOT posted to {gh_repo}#{pr_number} ({why})"
                  f" — the report above is the only copy", file=sys.stderr)
    else:
        print("\n(report only — pass --post to comment on the PR)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Reviewer panel for a PR")
    ap.add_argument("--repo", help="repo path, or a name under ~/source (default: cwd)")
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--post", action="store_true", help="post summary as a PR comment")
    ap.add_argument("--json", action="store_true", dest="json_out",
                    help="emit findings as JSON (for the /panel fix loop); no report/post")
    ap.add_argument("--reviewers", metavar="LIST",
                    help="comma-separated panel members to run instead of the repo's "
                         f"configured set ({', '.join(ALL_REVIEWERS)}); e.g. "
                         "--reviewers codex for a single-vendor read. "
                         "Default: whatever .harness-rules enables")
    ap.add_argument("--json-file", metavar="PATH", default="", dest="json_file",
                    help="also write the JSON payload here, keeping the report "
                         "(and --post) — unlike --json, which replaces them")
    ap.add_argument("--no-record", action="store_false", dest="record",
                    help="don't record this run on the quarterback board")
    args = ap.parse_args()
    return run(args.repo, args.pr, args.post, args.json_out, args.reviewers,
               args.json_file, args.record)


if __name__ == "__main__":
    raise SystemExit(main())
