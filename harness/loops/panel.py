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
pulls the JSON out of ``` fences or surrounding prose, as either the object
envelope reviewers now return or the bare findings array they used to; an
unparseable reply is retried once, then kept as a single markdown finding rather
than dropped — so malformed JSON degrades into one ungrouped finding, never a
crash or a silent loss.

Reviewers also DECLARE their own coverage — what they could not assess, and which
of their findings need the fix re-read — and the panel measures what they cannot
observe (whether the diff they got was truncated). Those are observations, not
forecasts: asking a model "will another round be needed?" asks it to predict its
own future findings, and a reviewer that silently produced nothing would answer
"no" with complete confidence. Rounds are driven mechanically instead — --round
and --baseline say which findings no earlier round raised, and that plus severity
decides whether to go again; the declarations only stop a broken round being read
as a clean one.

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
    python3 ~/.claude/loops/panel.py --pr 734 --post --round 2 \
        --baseline /tmp/panel-r1.json --json-file /tmp/panel-r2.json
"""

from __future__ import annotations

import argparse
import base64
import hashlib
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

# Panel -> fix -> panel. Two is the default because one is provably not enough:
# the fixer's own commit is otherwise read by nobody, and structural fixes beget
# new interactions that no earlier round could have seen because they did not
# exist until the fix was written. It is a cap on the CALLER's loop, used here
# only to decide whether a round that still has work left stopped because it was
# done or because it ran out of rounds.
DEFAULT_MAX_ROUNDS = 2

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

Return ONLY a JSON object (no prose):
  {{"findings": [{{"severity": "P1|P2|P3|P4", "file": "path", "line": <int|null>,
                  "title": "...", "detail": "...", "needs_rereview": true|false}}],
    "could_not_assess": ["..."]}}
An empty `findings` array only if the diff is genuinely flawless.

The last two keys are OBSERVATIONS about your own pass, not predictions. Do NOT forecast
whether another review will be needed — you cannot observe findings you have not made.

- `could_not_assess`: things in scope you could not judge from what you were given — a file
  the diff does not include, a runtime behaviour, a schema you cannot see, a caller you
  cannot check. One short phrase each; `[]` if you could genuinely assess everything.
  "I found nothing" and "I could not tell" are different answers and only you know which
  this was.
- `needs_rereview` (per finding): true when fixing it takes a STRUCTURAL change whose
  RESULT should be read again — the fix can create new interactions the current diff does
  not contain. False for a local edit whose correctness is evident from the fix itself.

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

Return ONLY a JSON object (no prose):
  {{"verdicts": [{{"id": <int>, "real": true|false, "severity": "P1|P2|P3|P4", "reason": "..."}}],
    "coverage_note": "..."}}

`coverage_note` adjudicates the reviewers' own coverage declarations below — one sentence, or ""
when there is nothing to say. Where they DISAGREE (one reports clean, another says it could not
assess an area), that split is more informative than either verdict alone: say which reading you
believe and what is therefore still unread. Do not average it away, and do not turn it into a
prediction about further rounds.

Findings:
{findings}

Coverage declared by the reviewers:
{coverage}
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
    #: The reporter's own declaration that fixing this needs a structural change
    #: whose RESULT should be read again — not a forecast, an observation about
    #: the shape of the fix. It is what predicts the round-3 class of defect: one
    #: created by the fixer's own changes meeting, which no earlier round could
    #: have seen because it did not exist yet.
    needs_rereview: bool = False
    #: On a merged group: WHICH members declared it. Attribution the merge would
    #: otherwise flatten into the representative — and the accuracy of a
    #: declaration is per reviewer, so a group flag credited to everyone who
    #: happened to raise the finding makes the member that called it and the
    #: member that missed it indistinguishable on exactly the statistic that
    #: separates them.
    rereview_by: list = field(default_factory=list)


@dataclass
class ReviewerRun:
    """One panel member's whole turn: what it found, what it could not judge, and
    what it cost. A tuple grew a fourth member the day reviewers started declaring
    their own coverage, and a 4-tuple unpacked at three call sites is where the
    declarations quietly become the duration."""

    findings: list = field(default_factory=list)          # Finding
    skip: str | None = None
    duration_ms: int = 0
    could_not_assess: list = field(default_factory=list)  # str
    #: The reply had no JSON in it and was kept as one raw finding. Its findings
    #: are real work, but nothing it might have declared survived the parse — so a
    #: quiet round that includes one is not evidence of a quiet PR.
    unstructured: bool = False


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


def _span_at(text: str, open_ch: str, close_ch: str) -> tuple[int, str] | None:
    """Return (start index, first top-level balanced open_ch..close_ch span),
    string-aware, or None. Beats a greedy `open.*close` regex: LLMs love to wrap
    their JSON in prose or ``` fences that ALSO contain brackets, and a greedy
    match then spans from the first stray bracket to the last, producing invalid
    JSON. Scanning for a balanced span (and skipping brackets inside JSON string
    literals) finds the real array/object instead."""
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
                return start, text[start:i + 1]
    return None


def extract_json_value(raw: str) -> list | dict | None:
    """Best-effort parse of the JSON value an LLM meant to return — an object
    envelope or a bare array — tolerating ``` fences and surrounding prose.

    Which of the two it is, is decided by WHICH STARTS FIRST in the reply, not by
    trying one shape and falling back. An envelope's `{` precedes its findings
    `[`; a bare array's `[` precedes its first item's `{`. Preferring one bracket
    unconditionally would read an envelope's inner array as the whole reply
    (silently dropping the declarations that ride alongside it) or an array's
    first element as the envelope.

    Returns None when no valid JSON value is present, so callers can tell
    "parsed empty → flawless" apart from "unparseable → retry / keep raw text"
    rather than silently dropping the reviewer's work."""
    if not raw:
        return None
    spans = [s for s in (_span_at(raw, "{", "}"), _span_at(raw, "[", "]")) if s]
    spans.sort(key=lambda s: s[0])
    for candidate in [s[1] for s in spans] + [raw.strip()]:
        if not candidate:
            continue
        try:
            val = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(val, (list, dict)):
            return val
    return None


def _to_findings(reviewer: str, items: list) -> list[Finding]:
    return [Finding(
        reviewer=reviewer,
        severity=str(it.get("severity", "P3")).upper(),
        file=str(it.get("file", "?")),
        line=it.get("line") if isinstance(it.get("line"), int) else None,
        title=str(it.get("title", "")).strip(),
        detail=str(it.get("detail", "")).strip(),
        needs_rereview=bool(it.get("needs_rereview")),
    ) for it in items if isinstance(it, dict)]


def _str_list(val) -> list[str]:
    """A declaration list, however the model spelled it — a list of phrases, or
    one string it wrote instead of a one-item list."""
    if isinstance(val, str):
        val = [val]
    if not isinstance(val, list):
        return []
    return [s for s in (str(x).strip() for x in val) if s]


def parse_reply(reviewer: str, raw: str) -> tuple[list[Finding], list[str]] | None:
    """Parse a reviewer's reply into (findings, could_not_assess).

    Two shapes are accepted, because the panel's members are four different CLIs
    and a contract change lands on them at different speeds:

    * ``{"findings": [...], "could_not_assess": [...], "fix_needs_rereview": [i]}``
      — the current one, which carries the reviewer's own coverage declarations.
    * a bare ``[...]`` of findings — every reviewer before this, and any model that
      ignores the envelope. It records no declarations, which is honest: it made
      none.

    ``fix_needs_rereview`` holds INDEXES into the findings array just returned, so
    a reviewer needs no id scheme of its own; a per-finding ``needs_rereview``
    boolean means the same thing and both are honoured.

    Returns None when the reply has no usable JSON at all (caller retries, then
    keeps the raw text as one finding) — distinct from ([], []) which means the
    reviewer ran, found nothing, and declared no gaps."""
    val = extract_json_value(raw)
    if val is None:
        return None
    if isinstance(val, list):
        return _to_findings(reviewer, val), []
    items = val.get("findings")
    if not isinstance(items, list):
        return None
    # Indexes are into the array the MODEL wrote, which is not the list we keep —
    # a junk entry among the findings is dropped, and every index after it would
    # then point one finding too far, flagging its neighbour.
    kept = [(i, it) for i, it in enumerate(items) if isinstance(it, dict)]
    findings = _to_findings(reviewer, [it for _, it in kept])
    at = {sent: n for n, (sent, _) in enumerate(kept)}
    for i in val.get("fix_needs_rereview") or []:
        # Bools are ints in Python, and `true` here means nothing — index 1 is not
        # what a model that wrote a boolean meant.
        if isinstance(i, int) and not isinstance(i, bool) and i in at:
            findings[at[i]].needs_rereview = True
    return findings, _str_list(val.get("could_not_assess"))


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


def run_cli(args: list[str], label: str, timeout: int = 600,
            attempts: int = 3) -> tuple[str | None, str | None]:
    """Run a headless CLI, returning (stdout, error_reason); error_reason is
    None on success. Retries transient failures (non-zero exits such as rate
    limits, and OS errors) up to `attempts` times with no delay — these fail
    fast, so retrying is cheap and recovers the common flake. A full timeout is
    NOT retried (it already burned the whole budget; retrying just doubles the
    wall-clock). The reason string is specific (timeout / exit code + stderr
    tail / OSError) so callers can SURFACE why a step degraded instead of
    reporting a bare 'unavailable'. A request the server has REJECTED on its
    merits is not retried either: a bad model pin fails identically all three
    times, so retrying only triples the wait for a certainty."""
    last = f"{label}: no attempt made"
    for _ in range(max(1, attempts)):
        try:
            proc = subprocess.run(args, capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, f"{label}: timed out after {timeout}s"
        except OSError as e:
            last = f"{label}: {e.__class__.__name__}"
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
    dropping them leaves you believing a budget you never got."""
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


def reviewer_label(name: str, model: str, effort: str = "") -> str:
    """`codex (gpt-5.6-luna, high)` — the report says WHICH brain reviewed.

    Findings keep the bare vendor name for attribution; this is for the header
    and the skip lines, where "codex ran" is not the same claim as "codex ran on
    the model you pinned"."""
    spec = ", ".join(x for x in (model, effort) if x)
    return f"{name} ({spec})" if spec else f"{name} (CLI default)"


def codex_args(model: str, effort: str, prompt: str) -> list[str]:
    """codex exec argv. Both knobs are optional and independent: effort is a
    `-c` config override rather than a flag, and applies to the CLI's default
    model just as well as to a pinned one."""
    args = ["codex", "exec", prompt]
    if model:
        args += ["--model", model]
    if effort:
        args += ["-c", f"model_reasoning_effort={effort}"]
    return args


def antigravity_args(model: str, effort: str, prompt: str) -> list[str]:
    """`agy` argv — Google's Antigravity CLI, which replaced gemini-cli in this
    seat. `-p` is its non-interactive print mode; `--mode plan` is the read-only
    execution mode, and a reviewer has no business editing the tree it is
    reviewing — the panel wants an opinion, not a fix.

    Left on the default `--output-format text` rather than `json`: the JSON mode
    wraps the reply in {response, status, usage, ...}, which would hide the
    findings array inside an escaped string where parse_reply's balanced-bracket
    scan cannot see it. Text mode puts the array straight on stdout, which is what
    every other seat here produces and what the parser is written against.

    Unlike gemini-cli, `agy` fails loudly on an unknown model instead of silently
    serving a different one, so a pinned slug that stops existing shows up as a
    dead reviewer rather than a quietly wrong one. Its effort scale is only
    low/medium/high (see EFFORTS) — narrower than codex's or pi's.
    """
    args = ["agy", "--mode", "plan", "-p", prompt]
    if model:
        args += ["--model", model]
    if effort:
        args += ["--effort", effort]
    return args


def pi_args(model: str, effort: str, prompt: str) -> list[str]:
    """pi argv. `-p` is its non-interactive mode; `--no-tools` is what makes it a
    REVIEWER — pi ships read/bash/edit/write, and a panel member has no business
    editing the tree it is reviewing (the same reason agy runs in plan mode).
    The diff is in the prompt, so it needs no tools to do the job.

    `--no-session` keeps a review out of the session store: a panel run is not a
    conversation anyone resumes, and one runs per PR.

    pi reaches many providers, so `model` here is a full `provider/id` pattern
    (`openrouter/moonshotai/kimi-k3`) rather than a bare slug, and its thinking
    level is spelled `--thinking` where codex spells it `model_reasoning_effort`.
    Same knob, same config key (`effort`), different word on each CLI."""
    args = ["pi", "-p", "--no-session", "--no-tools", prompt]
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
               effort: str = "") -> ReviewerRun:
    """Run a headless LLM CLI reviewer. Returns a :class:`ReviewerRun`.

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
        return ReviewerRun(skip=f"{label}: unknown reasoning effort {effort!r} — {expected}",
                           duration_ms=elapsed())
    if not shutil.which(CLI_BIN.get(cmd_name, cmd_name)):
        return ReviewerRun(skip=f"{label}: CLI absent", duration_ms=elapsed())
    if cmd_name == "claude":
        args = ["claude", "-p", prompt, "--model", model]
    elif cmd_name == "antigravity":
        args = antigravity_args(model, effort, prompt)
    elif cmd_name == "pi":
        args = pi_args(model, effort, prompt)
    else:
        args = codex_args(model, effort, prompt)
    out, err = run_cli(args, label)
    if err:
        err += cli_hint(cmd_name, err, model)
        return ReviewerRun(skip=err, duration_ms=elapsed())
    parsed = parse_reply(cmd_name, out)
    if parsed is None:
        # Unparseable JSON — give the reviewer one more shot (a common flake is a
        # stray prose preamble the model omits on a retry), then, rather than drop
        # its work, keep the raw reply as a single markdown finding for the judge.
        out2, err2 = run_cli(args, label, attempts=1)
        if not err2 and out2:
            retried = parse_reply(cmd_name, out2)
            if retried is not None:
                return ReviewerRun(retried[0], None, elapsed(), retried[1])
            out = out2
        text = (out or "").strip()
        if not text:
            return ReviewerRun(skip=f"{label}: no parseable findings and empty output",
                               duration_ms=elapsed())
        return ReviewerRun([_raw_finding(cmd_name, text)], None, elapsed(),
                           unstructured=True)
    return ReviewerRun(parsed[0], None, elapsed(), parsed[1])


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
    Reviewer count is a *confidence signal*, never a gate.

    A ``needs_rereview`` declaration survives the merge from ANY reporter: one
    reviewer seeing that the fix will be structural is the observation, and the
    others not saying so is not a contradiction of it."""
    groups: dict[tuple, list[Finding]] = {}
    for f in llm_findings:
        groups.setdefault(_key(f), []).append(f)
    out = []
    for grp in groups.values():
        reviewers = sorted({f.reviewer for f in grp})
        rep = min(grp, key=lambda f: f.severity)  # P1 < P2 < P3 lexically
        # Read before it is written: the representative is one of the group's own
        # findings, so setting its flag first would credit its reviewer with a
        # declaration another member made.
        rep.rereview_by = sorted({f.reviewer for f in grp if f.needs_rereview})
        rep.needs_rereview = bool(rep.rereview_by)
        out.append((rep, reviewers))
    out.sort(key=lambda e: e[0].severity)
    return out


def judge(groups: list[tuple[Finding, list[str]]], diff: str, model: str,
          budget: int = MAX_DIFF_CHARS,
          coverage: dict[str, list[str]] | None = None
          ) -> tuple[dict[int, dict], str | None, str]:
    """The 'master' adjudicates each finding on its merits, and rules on the
    coverage the reviewers declared. Returns (verdicts, skip_reason, coverage_note).
    skip_reason is None when the judge ran successfully
    (even if it dismissed nothing); otherwise it explains WHY the judge could not
    rule — CLI absent, timeout, crash, or unparseable output — so the caller can
    surface it rather than silently reporting a bare 'unavailable'. A real bug
    from a single reviewer is confirmed; only genuine false positives are dropped
    (style and polish are kept). When the judge can't rule, the caller keeps everything (we never
    silently suppress a finding). No findings -> ({}, None, ""): nothing to judge.

    The judge is asked to rule on coverage in the same call — one extra key in the
    object it already returns, no additional model call. Its own reply may still
    be the bare verdict array every earlier judge returned, in which case there is
    simply no coverage note."""
    declared = {k: v for k, v in (coverage or {}).items() if v}
    if not groups:
        return {}, None, ""
    if not shutil.which("claude"):
        return {}, "judge: claude CLI absent", ""
    listing = "\n".join(
        f"[{i}] {f.severity} {f.file}:{f.line or '?'} (via {', '.join(revs)}) — "
        f"{f.title} — {f.detail}"
        for i, (f, revs) in enumerate(groups))
    stated = "\n".join(f"- {name}: could not assess {'; '.join(items)}"
                       for name, items in sorted(declared.items())) \
        or "- (no reviewer declared a gap in its coverage)"
    prompt = JUDGE_PROMPT.format(findings=listing, coverage=stated, diff=diff[:budget])
    args = ["claude", "-p", prompt] + (["--model", model] if model else [])
    out, err = run_cli(args, "judge")
    if err:
        return {}, err, ""
    parsed = extract_json_value(out)
    note = ""
    if isinstance(parsed, dict):
        note = str(parsed.get("coverage_note") or "").strip()
        parsed = parsed.get("verdicts")
    if not isinstance(parsed, list):
        return {}, "judge: no JSON verdict in output (unparseable)", note
    verdicts: dict[int, dict] = {}
    for v in parsed:
        if isinstance(v, dict) and isinstance(v.get("id"), int):
            verdicts[v["id"]] = v
    return verdicts, None, note


# ----------------------------------------------------------------------------- rounds

def finding_key(file: str | None, title: str) -> str:
    """Identity of the DEFECT, so the same issue raised in round 1 and again in
    round 2 is one thing seen twice rather than two things.

    File plus a normalised title, deliberately **without** the line: the line
    moves when the fix above it lands, and an identity that moves links nothing.

    Must stay byte-identical to ``app.api.reviews._derive_key`` (and its SQL twin
    in the board's migration 0012). The board derives this key for any payload
    that arrives without one, so a panel that computed it differently would put
    the local round-over-round diff and the board's cross-run chains on two
    different notions of "the same finding" — and only one of them would be
    visible to the person reading the stats."""
    norm = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    return hashlib.md5(f"{file or ''}|{norm}".encode(),
                       usedforsecurity=False).hexdigest()[:16]


def load_baseline(paths: list[str]) -> tuple[set[str], int, list[str]]:
    """Every finding key earlier rounds of this PR already raised, from their
    ``--json-file`` payloads. Returns (keys, rounds_covered, problems).

    Keyed on what was RAISED, not on what was confirmed: a finding the judge
    dismissed in round 1 and a reviewer raises again in round 2 is not new
    information, and counting it as new is how a loop fails to converge.

    A baseline that cannot be read is reported rather than swallowed. Its absence
    makes every finding look new, which reads as "the fix broke things" — the
    exact opposite of the truth — so the caller marks the round's verdict
    unearned instead of quietly believing it."""
    keys: set[str] = set()
    rounds = 0
    problems: list[str] = []
    for path in paths:
        try:
            payload = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as e:
            problems.append(f"baseline {path} unreadable ({e.__class__.__name__})")
            continue
        if not isinstance(payload, dict):
            problems.append(f"baseline {path} is not a panel payload")
            continue
        rounds = max(rounds, int(payload.get("round") or 1))
        for bucket in ("to_fix", "dismissed", "sonar_findings"):
            for f in payload.get(bucket) or []:
                if isinstance(f, dict):
                    keys.add(str(f.get("key") or "")
                             or finding_key(f.get("file"), str(f.get("title", ""))))
    return keys, rounds, problems


def coverage_veto(reviewer_meta: dict[str, dict], judge_skip: str | None,
                  flagged: int, diff_chars: int) -> list[str]:
    """Reasons a quiet round is not evidence of a quiet PR.

    A counter cannot tell a genuinely dry round from a broken one — a reviewer
    that read half the diff, one that never ran, and one whose reply did not parse
    all look identical to "found nothing". These are the observations that
    distinguish them, and they exist to stop a failure being read as convergence.
    They do NOT drive the loop: a truncated reviewer is truncated again next round
    at the same budget, so treating that as a reason to go again is a loop with no
    exit. It is a reason to stop CLAIMING the PR is clean."""
    out = []
    for name, meta in sorted(reviewer_meta.items()):
        if not meta.get("ran"):
            out.append(f"{name} did not run ({meta.get('skip') or 'no reason recorded'})")
            continue
        if meta.get("truncated"):
            budget = meta.get("max_diff_chars") or 0
            out.append(f"{name} saw {budget:,} of {diff_chars:,} diff chars")
        if meta.get("unstructured"):
            out.append(f"{name} returned no structured reply — its coverage is unknown")
        for gap in meta.get("could_not_assess") or []:
            out.append(f"{name} could not assess: {gap}")
    if judge_skip:
        out.append(f"findings were not adjudicated ({judge_skip})")
    if flagged:
        out.append(f"{flagged} finding(s) whose reporter said the FIX needs re-reading")
    return out


def round_stop(round_no: int, max_rounds: int, new_keys: list[str],
               confirmed: list[Finding], veto: list[str],
               baseline_ok: bool = True, repeated: int = 0) -> dict:
    """Whether the panel/fix cycle should go again, and what decided it.

    The rule is mechanical on purpose. Asking reviewers to forecast "will another
    round be needed?" measures the wrong thing — a model that just wrote five
    findings is primed on problems and says yes, one that found nothing says no,
    and the vote only re-encodes a finding count already known. So the loop turns
    on what actually happened:

    1. findings this round that no earlier round raised -> go again;
    2. a P1/P2 still confirmed -> go again, whatever anyone declared (a blocker
       raised again is a blocker that was not fixed);
    3. otherwise dry -> stop.

    The cap ends it either way, and a cap reached with work outstanding is
    recorded as such rather than as convergence.

    ``repeated`` — findings an earlier round already raised that are STILL
    confirmed — does not extend the loop (two reviewers can disagree about a P4
    forever), but it does cost the stop its confidence: the fixer was told about
    those and they are still there, which is not the same event as nothing being
    found."""
    blockers = [f for f in confirmed if f.severity in ("P1", "P2")]
    if new_keys:
        stop, reason = False, (f"{len(new_keys)} finding(s) no earlier round raised")
    elif blockers:
        stop, reason = False, f"{len(blockers)} P1/P2 still confirmed after the fix"
    else:
        stop, reason = True, ("dry — nothing raised that an earlier round had not"
                              if round_no > 1 else "dry — no findings to fix")
    capped = False
    if not stop and round_no >= max_rounds:
        stop, capped = True, True
        reason = f"round cap ({max_rounds}) reached — {reason}, unreviewed"
    if repeated:
        veto = [*veto, f"{repeated} finding(s) an earlier round already raised are "
                       "still confirmed — the fix for them did not land"]
    return {
        "stop": stop,
        "reason": reason,
        # "Nothing left to find" is a claim; "the counter hit zero" is not the
        # same claim, and the difference is exactly what a reader of a clean
        # verdict needs to see.
        "confident": bool(stop and not capped and not veto and baseline_ok),
        "veto": veto,
        "round": round_no,
        "max_rounds": max_rounds,
    }


# ----------------------------------------------------------------------------- run

def run(repo_name: str, pr_number: int, post: bool, json_out: bool = False,
        reviewers: str | None = None, json_file: str = "", record: bool = True,
        round_no: int = 1, baseline: list[str] | None = None,
        max_rounds: int = DEFAULT_MAX_ROUNDS) -> int:
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
    truncated_for = {n: b for n, b in budgets.items() if len(diff) > b}
    truncated = bool(truncated_for)

    def prompt_for(budget: int) -> str:
        return REVIEW_PROMPT.format(n=pr_number, repo=gh_repo, base=base,
                                    diff=diff[:budget])

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
            got = fut.result()
            reviewer_meta[name] = {
                "model": models[name] or None,
                "effort": efforts.get(name) or None,
                "ran": not got.skip,
                "skip": got.skip,
                "max_diff_chars": budgets[name],
                # The mechanical half of "did this reviewer see the whole thing":
                # checked against the budget rather than asked for, because the
                # one thing a truncated reviewer cannot notice is the truncation.
                "truncated": name in truncated_for,
                "duration_ms": got.duration_ms,
                "could_not_assess": got.could_not_assess,
                "unstructured": got.unstructured,
            }
            if got.skip:
                result.skipped.append(got.skip)
                llm_skipped.append(got.skip)
            else:
                ran_llm.append(labels[name])
                llm_findings.extend(got.findings)
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
    coverage = {n: m.get("could_not_assess") or [] for n, m in reviewer_meta.items()}
    verdicts, judge_skip, coverage_note = judge(
        groups, diff, panel.get("judge_model", ""), judge_budget, coverage)
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

    # ---- this round against the ones before it. Mechanical: which findings are
    # ones no earlier round raised, and does that make the loop done?
    prior_keys, prior_rounds, baseline_problems = load_baseline(baseline or [])
    notes.extend(baseline_problems)
    if prior_keys and round_no == 1:
        # Not fatal — the diff against the baseline is still right — but the round
        # number is what the board files this run under, so a re-review recorded
        # as a first round makes the PR look like it was reviewed twice from
        # scratch rather than once and then again.
        notes.append("`--baseline` given with `--round 1` — this run records as a "
                     "first round; pass the round it actually is")
    round_keys = {finding_key(f.file, f.title) for f, _, _ in to_fix}
    new_keys = sorted(round_keys - prior_keys)
    flagged = sum(1 for f, _, _ in to_fix if f.needs_rereview)
    veto = coverage_veto(reviewer_meta, judge_skip, flagged, len(diff))
    stop = round_stop(round_no, max_rounds, new_keys,
                      [f for f, _, _ in to_fix], veto, not baseline_problems,
                      repeated=len(round_keys & prior_keys))

    def loc(f: Finding) -> str:
        return f"{f.file}:{f.line}" if f.line else f.file

    # ---- the run, as data. Built on every path, not just --json: it is what
    # --json prints, what --json-file writes, and what gets recorded on the
    # board. One structure, so the fix loop and the stats can never be looking
    # at different accounts of the same review.
    def ser(f: Finding, revs: list[str], reason: str) -> dict:
        key = finding_key(f.file, f.title)
        return {"severity": f.severity, "file": f.file, "line": f.line,
                "title": f.title, "detail": f.detail, "reviewers": revs,
                "reason": reason,
                # Sent rather than left to the board to derive: the two recipes
                # are the same one, and sending it keeps the local round diff and
                # the board's chains provably on the same identity.
                "key": key,
                "new_this_round": key not in prior_keys,
                "needs_rereview": f.needs_rereview,
                "rereview_by": list(f.rereview_by)}
    payload = {
        "repo": repo_name, "github": gh_repo, "pr": pr_number,
        "title": title, "base": base, "changed_lines": changed,
        "diff_truncated": truncated,
        # Where this run sits in the panel -> fix -> panel cycle, and what the
        # mechanical stopping rule made of it.
        "round": round_no,
        "prior_rounds": prior_rounds,
        "prior_findings": len(prior_keys),
        "new_findings": len(new_keys),
        "new_finding_keys": new_keys,
        "round_stop": stop,
        "stop_reason": stop["reason"],
        "coverage_note": coverage_note or None,
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
    heading = f"## Reviewer panel — PR #{pr_number}"
    if round_no > 1 or prior_rounds:
        heading += f" · round {round_no}"
    lines = [heading, ""]
    if round_no > 1 or prior_keys:
        lines.append(f"**Round {round_no}** — re-reviewing after the fix. "
                     f"{len(new_keys)} of {len(to_fix)} finding(s) here were raised by no "
                     f"earlier round ({len(prior_keys)} known from {prior_rounds} earlier "
                     f"round{'s' if prior_rounds != 1 else ''}).")
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
            # Only where there IS an earlier round to be new against: on a first
            # round every finding is new and the marker would be decoration.
            fresh = " 🆕" if prior_keys and finding_key(f.file, f.title) not in prior_keys else ""
            again = (" ↻ _fix needs re-reading (" + ", ".join(f.rereview_by) + ")_"
                     if f.needs_rereview else "")
            lines.append(f"- **{f.severity}**{fresh} `{loc(f)}` — {f.title}{conf(revs)}{tail}{again}")
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

    # What the reviewers said about their OWN coverage, and what the judge made of
    # the split. This is the difference between "clean" and "I could not tell",
    # which no finding count can express — and it is on the PR comment, not just
    # the terminal, because the person deciding whether a clean verdict was earned
    # is reading the comment.
    declared = {n: m["could_not_assess"] for n, m in sorted(reviewer_meta.items())
                if m.get("could_not_assess")}
    if declared or coverage_note:
        lines.append("\n### Coverage declared by the reviewers")
        for name, gaps in declared.items():
            lines.append(f"- **{name}** could not assess: " + "; ".join(gaps))
        if coverage_note:
            lines.append(f"- _master:_ {coverage_note}")

    verdict = "**stop**" if stop["stop"] else "**go again**"
    unearned = stop["stop"] and not stop["confident"]
    lines.append(f"\n**Rounds:** round {round_no} of at most {max_rounds} — {verdict}: "
                 + stop["reason"]
                 + (" — a stop, not convergence" if unearned else ""))
    if stop["veto"]:
        lines.append("  _why this round's quiet is not evidence of a quiet PR:_")
        for why in stop["veto"]:
            lines.append(f"  - ⚠️ {why}")

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
    ap.add_argument("--round", type=int, default=1, dest="round_no", metavar="N",
                    help="which panel/fix cycle this is (default 1). Round 2+ is the "
                         "re-review of the fix commit — the one nobody reads otherwise")
    ap.add_argument("--baseline", action="append", default=[], metavar="PATH",
                    help="a previous round's --json-file payload, so this run can say "
                         "which findings no earlier round raised. Repeatable")
    ap.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS,
                    dest="max_rounds", metavar="N",
                    help=f"the caller's round cap (default {DEFAULT_MAX_ROUNDS}); used to "
                         "tell a round that stopped because it was done from one that "
                         "stopped because it ran out")
    args = ap.parse_args()
    if args.round_no < 1:
        raise SystemExit("--round: rounds are numbered from 1")
    if args.max_rounds < 1:
        raise SystemExit("--max-rounds: at least one round has to run")
    return run(args.repo, args.pr, args.post, args.json_out, args.reviewers,
               args.json_file, args.record, args.round_no, args.baseline,
               args.max_rounds)


if __name__ == "__main__":
    raise SystemExit(main())
