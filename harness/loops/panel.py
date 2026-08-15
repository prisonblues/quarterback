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

The master also MERGES the duplicates, and that is the only place a merge happens.
Deduping upstream of it could only pick one reviewer's text and discard the rest,
so a better key made the loss worse: the observation only one reviewer made
survived precisely when the merge FAILED. The judge instead writes a synthesis and
every reviewer's own title and detail ride along beside it (`reported_by`), so
merging is additive, attribution is a field rather than an inference, and the fix
loop and the board consume one canonical record instead of re-deriving it.

Reviewers whose prerequisites are missing (codex CLI absent, SONAR* env unset)
are reported as SKIPPED, not failed — the panel still produces a report.

LLM replies are parsed leniently: a balanced-bracket scan (not a greedy regex)
pulls the JSON out of ``` fences or surrounding prose, as either the object
envelope reviewers now return or the bare findings array they used to; an
unparseable reply is retried once, then kept as a single markdown finding rather
than dropped — so malformed JSON degrades into one ungrouped finding, never a
crash or a silent loss. A reply holding SEVERAL JSON-shaped values is settled by
agreement, never by rank: the prompts' own examples are identified and dropped,
and what remains must say one thing or the reply is treated as unstructured.

That is deliberately the pessimistic rule. Choosing among candidates — by
position, by size, by which strings they carry — can silently swap a review for
an echo or file a finding no reviewer made, and both artefacts read exactly like
a clean round.

Reviewers also DECLARE their own coverage — what they could not assess, and which
of their findings need the fix re-read — and the panel measures what they cannot
observe (whether the diff they got was truncated). Those are observations, not
forecasts: asking a model "will another round be needed?" asks it to predict its
own future findings, and a reviewer that silently produced nothing would answer
"no" with complete confidence. Rounds are driven mechanically instead — --round
and --baseline say which findings no earlier round raised, and that plus severity
decides whether to go again; the declarations only stop a broken round being read
as a clean one. The cycle belongs to the CALLER (/panel-review-pr): a run given
none of --round/--baseline/--max-rounds is a single review and says nothing about
rounds, rather than promising a re-review nothing will run.

Default prints a report. Pass --post to also comment the summary on the PR, or
--json to emit the whole run as JSON on stdout instead (progress goes to stderr,
so the payload parses without a preamble to strip); --json-file writes that same
JSON *and* keeps the report.

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
    python3 ~/.claude/loops/panel.py --pr 734 --post --json-file "$rundir/panel.json"
    python3 ~/.claude/loops/panel.py --pr 734 --post --round 2 --max-rounds 2 \
        --baseline "$rundir/r1.json" --json-file "$rundir/r2.json"

(`$rundir` being a `mktemp -d` of the caller's: a fixed /tmp path is a symlink
away from writing the payload somewhere else, and world-readable meanwhile.)
"""

from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent))
import harness_rules  # noqa: E402
# stderr_gist and cli_outcome live with the shared plumbing — how headless CLIs
# fail is not a panel question, and the loops that run headless agents with no
# panel in sight need the same reading of a CLI's complaint (#31). They are
# re-exported here because they read as part of run_cli's contract at every call
# site in this file.
from harness_rules import (DENIAL_MARKERS, REJECTION_MARKERS,  # noqa: E402
                           RepoNotFound, cli_outcome, describe,
                           resolve_repo, stderr_gist)

# Chars of diff handed to a model, when nothing in .harness-rules says otherwise:
# NONE OF THEM. The whole diff goes to every reviewer unless a repo asks for a
# cap (review_panel.max_diff_chars, overridable per reviewer and for the judge).
#
# There was a 60,000-char default here, inherited from a constraint that no
# longer exists: the prompt used to travel in argv, where Linux caps a single
# element at 128 KiB. Since prompts moved to stdin the only ceiling is the
# model's own context, and 60k chars is ~15k tokens — an order of magnitude
# under every reviewer this panel runs.
#
# It was never a neutral saving. A truncated reviewer cannot notice that it was
# truncated, so it reports confidently on a prefix, and the failure is BIASED
# TOWARDS FALSE POSITIVES: on the review that removed this default, a reviewer
# reported a migration as "syntactically incomplete" because the file was cut
# mid-way, and the panel spent a judge call and a fixer's attention proving the
# file was fine. Two other rounds lost ~600 lines of a test file to the same cut.
# Paying for a review of a prefix is worse than paying for a review.
#
# A budget larger than a model will take now fails LOUDLY — the API refuses the
# request and the reviewer is reported as degraded, with the reason. That is the
# right way round: a reviewer that could not read the change must look different
# from one that read it and found nothing, which is the whole argument of v2.15.
DEFAULT_DIFF_BUDGET: int | None = None
RAW_DETAIL_CHARS = 4_000  # cap an unparsed reviewer reply kept as a fallback finding
# How far apart two findings in one file can be and still be offered to the judge
# as "possibly the same observation". A hint only — see cluster_findings.
CLUSTER_WINDOW = 10
ACCOUNT_CHARS = 240  # per-reviewer account shown under a merged finding in the report

# Panel -> fix -> panel. Two is the default because one is provably not enough:
# the fixer's own commit is otherwise read by nobody, and structural fixes beget
# new interactions that no earlier round could have seen because they did not
# exist until the fix was written. It is a cap on the CALLER's loop, used here
# only to decide whether a round that still has work left stopped because it was
# done or because it ran out of rounds.
DEFAULT_MAX_ROUNDS = 2

# How long a reviewer CLI may take. One constant, because two CLIs enforce it:
# run_cli kills a wedged process at this bound, and `agy` self-aborts at its own
# `--print-timeout` (default 5m0s), so the seat that does not read this number
# silently reviews on a five-minute clock while the report claims thirty.
CLI_TIMEOUT = 1800

# How long a blank reply may take and still be worth retrying. A zero exit with
# no output is retried because it is often a flake — but a blank run does NOT
# fail fast the way a non-zero exit does, so three of them is up to three whole
# CLI_TIMEOUTs held against the joined futures of the entire panel, which is the
# exact cost the timeout branch already refuses to pay ("it already burned the
# whole budget"). A blank that comes back inside a minute plausibly never
# reached a model at all, which is the flake the retry exists for; one that
# spent real time thinking and still said nothing will spend it again.
BLANK_RETRY_MAX_S = 60

# The one skip reason that says nothing about the round: this box does not carry
# that CLI. The wording is free to change — what coverage_veto branches on is
# ReviewerRun.absent, not this string.
CLI_ABSENT = "CLI absent"

# Linux caps ONE argv string at MAX_ARG_STRLEN = 131,072 bytes, independently of
# the much larger total ARG_MAX; cross it and execve fails with E2BIG before the
# CLI starts. Every reviewer whose prompt travels on stdin is free of this, and
# that is all of them but one — `agy` has no stdin path (`-p ""` is "empty
# prompt", `-p -` reviews the literal string "-"), so its prompt is clamped to
# fit here and the truncation is reported like any other. The margin is for the
# rest of the argv and the environment, which share the kernel's accounting.
ARGV_PROMPT_MAX_BYTES = 120_000
# The severities everything downstream counts in. A value outside this set is not
# a stricter or a looser call, it is an unreadable one — it would reach the
# board's leaderboard as a bucket nothing counts, and it sorts wherever its first
# letter falls (a reviewer answering "BLOCKER" would head the fix list on a
# lexical accident). Normalised where a severity ENTERS the panel, so no
# downstream comparison has to defend itself.
SEVERITIES = ("P1", "P2", "P3", "P4")

# The judge's prompt holds the one component that grows with the review itself —
# one line per reviewer account, each up to RAW_DETAIL_CHARS — so the listing
# gets a budget of its own. It is no longer a share of anything: the diff's
# ceiling was the kernel's while prompts travelled in argv, and on stdin the two
# stopped competing for it.
MAX_LISTING_CHARS = 40_000     # the judge's finding listing
LISTING_ACCOUNT_CHARS = 1_200  # one account's share of that listing

# GitHub rejects an issue comment over 65,536 characters. The report grows with
# the per-reviewer accounts, so `--post` needs a guard: a review that succeeded
# must not be lost to a comment that was one account too long.
COMMENT_CHARS = 65_000
#: The report's last block, and the one a cycle's caller acts on — named because
#: `fit_comment` cuts around it rather than through it.
ROUNDS_HEADING = "**Rounds:**"

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
a pull request diff, held to the standard "nothing left to improve". The reports below come from
several independent reviewers (Claude, Codex, SonarCloud), listed ONE PER REVIEWER — so the same
defect appears once for each reviewer that spotted it, often citing different lines and describing
it differently. You do two things: MERGE the reports that are the same defect, and rule on each
resulting issue.

TWO KINDS OF ID, do not mix them. A REPORT id is the bare number in brackets at the start of each
line below ([0], [1], ...): those are what `members` lists, as INTEGERS — `"members": [0, 3]`.
An ISSUE id is a label YOU invent for an issue you are returning ("F01"): that is what `id` holds
and what `related` points at. Never put an "F.." label in `members`, and never put a report number
in `related` — a `members` entry that is not a report number merges nothing.

MERGING. Group reports by the DEFECT, not by position: two reviewers pointing at lines 100 and 41
of one file may well be describing one bug, and two findings on the same line may be two bugs.
Write a `synthesis` that states the merged issue INCLUDING every point any of its reports made —
where one reviewer noticed something the others did not, that observation must appear in your
synthesis. Never drop a point because only one reviewer made it; that is exactly the reviewer
diversity the panel exists for. Each report id belongs to exactly ONE issue.

Separate defects that share one CAUSE (one design decision showing up in four files) are NOT
merged — list each other issue's id in `related` so they get fixed as one decision.

RULING. The bar is completeness, not triage. Keep every genuine issue — correctness, security,
error handling, test gaps, docs, naming, style, simplifications, and polish ALL count and all get
fixed. "Not worth the churn" and "could do later" are NOT valid reasons to dismiss. A genuine issue
flagged by only ONE reviewer MUST be marked real — never dismiss it because the others missed it.

Mark real=false ONLY when the issue is a genuine FALSE POSITIVE — you re-examined and the code is
actually correct, or the suggestion would make it worse. When unsure, mark it real.

Return ONLY a JSON object (no prose), with one `verdicts` entry per REAL-WORLD ISSUE,
covering every report id:
  {{"verdicts": [{{"id": "F01",
                  "members": [<the bracketed report NUMBERS merged into this issue, e.g. 0, 3>],
                  "real": true|false,
                  "severity": "P1|P2|P3|P4",
                  "file": "path", "line": <int|null>,
                  "synthesis": "the merged statement of the issue",
                  "related": ["F03"],
                  "reason": "why real or a false positive"}}],
   "coverage_note": "..."}}

`coverage_note` adjudicates the reviewers' own coverage declarations below — one sentence, or ""
when there is nothing to say. Where they DISAGREE (one reports clean, another says it could not
assess an area), that split is more informative than either verdict alone: say which reading you
believe and what is therefore still unread. Do not average it away, and do not turn it into a
prediction about further rounds.

Reports:
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
    #:
    #: Per REPORTER, and it stays that way all the way to the board: the accuracy
    #: of a declaration is a property of the member that made it, so a flag
    #: credited to everyone who happened to raise the same defect makes the member
    #: that called it and the member that missed it indistinguishable on exactly
    #: the statistic that separates them (see :attr:`Canonical.rereview_by`).
    needs_rereview: bool = False


@dataclass
class ReviewerRun:
    """One panel member's whole turn: what it found, what it could not judge, and
    what it cost. A tuple grew a fourth member the day reviewers started declaring
    their own coverage, and a 4-tuple unpacked at three call sites is where the
    declarations quietly become the duration."""

    findings: list[Finding] = field(default_factory=list)
    skip: str | None = None
    duration_ms: int = 0
    #: None = the member declared nothing (its CLI answered in the old bare-array
    #: shape, or never got the question); [] = asked, and it had no gap. The board
    #: stores that distinction, so the panel must not flatten it here — a reviewer
    #: that never engaged with the coverage question would otherwise be
    #: indistinguishable from one that engaged and reported clean.
    could_not_assess: list[str] | None = None
    #: The reply had no JSON in it and was kept as one raw finding. Its findings
    #: are real work, but nothing it might have declared survived the parse — so a
    #: quiet round that includes one is not evidence of a quiet PR.
    unstructured: bool = False
    #: The board's token fields for this turn, or None where nothing could be
    #: read. Never a zeroed dict: "not recorded" and "spent nothing" must stay
    #: distinguishable, or an uninstrumented seat wins every cost comparison on
    #: the strength of not having been measured.
    usage: dict | None = None
    #: This box does not carry the reviewer's CLI. Recorded as state rather than
    #: read back out of `skip` — a message tail is free text, and matching on it
    #: both lets an installed CLI whose stderr happens to end that way escape the
    #: coverage veto, and silently restores the veto the moment the absent
    #: branch's wording gains a suffix.
    absent: bool = False


@dataclass
class PanelResult:
    sonar_gate: str = "skipped"          # OK | ERROR | skipped | no-analysis
    sonar_findings: list[Finding] = field(default_factory=list)  # the hard gate's issues
    skipped: list[str] = field(default_factory=list)     # ["codex: CLI absent", ...]


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


def _spans(text: str, open_ch: str, close_ch: str) -> list[tuple[int, str]]:
    """Every top-level balanced open_ch..close_ch span, as (start index, text),
    string-aware. Beats a greedy `open.*close` regex: LLMs love to wrap their
    JSON in prose or ``` fences that ALSO contain brackets, and a greedy match
    then spans from the first stray bracket to the last, producing invalid JSON.
    Scanning for balanced spans (and skipping brackets inside JSON string
    literals) finds the real arrays/objects instead.

    ALL of them, not just the first: a reply that says "severities are {P1..P4}"
    before its envelope has a first `{` span that is not JSON, and stopping there
    used to leave the envelope unconsidered."""
    out: list[tuple[int, str]] = []
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
                out.append((start, text[start:i + 1]))
    return out


#: Keys that identify an object as the envelope a panel member (or the judge)
#: was asked for, rather than some other object that happens to parse.
ENVELOPE_KEYS = ("findings", "verdicts")

#: The coverage declaration that rides alongside each envelope: a reviewer's
#: `could_not_assess`, the judge's `coverage_note`. These are the ONLY fields
#: that declare anything. Everything else an envelope carries is structure
#: (`fix_needs_rereview` holds INDEXES into the findings array) or a key the
#: model invented, and neither says anything about the review — a candidate
#: padded with `"summary": "I reviewed the diff"` says exactly what the same
#: candidate without it says. One entry per :data:`ENVELOPE_KEYS`, which
#: `test_every_envelope_has_a_declaration_beside_it` pins.
DECLARATION_KEYS = {"findings": "could_not_assess", "verdicts": "coverage_note"}


def _scalar(val: object) -> bool:
    """A leaf that is not text: a number, a bool, null. The two places the
    schemas are not valid JSON — `<int|null>` and `true|false` — are exactly
    these, and a model resolves them before it can quote them back."""
    return not isinstance(val, (str, list, dict))


class _Tok:
    """A position the prompt filled with a token rather than a value — `<int|null>`,
    `true|false`. It stands for whatever the model resolves it to, so it is the one
    place :func:`_quoted` may accept any scalar."""

    def __repr__(self) -> str:
        return "<token>"


_TOKEN = _Tok()

#: What a token becomes on the way through `json.loads`, before `_tokenise` turns
#: it back into :data:`_TOKEN`. Long and bracketed so no prompt text collides with
#: it, and plain ASCII so it survives being written into this file.
_TOKEN_MARK = "[[qb-schema-token]]"


def _tokenise(val):
    """Restore :data:`_TOKEN` wherever the marker survived the JSON round-trip."""
    if isinstance(val, dict):
        return {k: _tokenise(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_tokenise(v) for v in val]
    return _TOKEN if val == _TOKEN_MARK else val


def _schema(prompt: str) -> dict | None:
    """The example object a prompt ships, parsed — the exact value a model that
    quotes the request back returns.

    Read out of the prompt rather than restated here, so an edit to either
    schema changes what counts as a quotation in the same commit that makes it.
    `{{`/`}}` are :meth:`str.format` escapes; `<int|null>`-style tokens and
    `true|false` are the only two places the block is not JSON, and both are
    resolved the way a model resolves them — to a scalar.

    None when no schema block can be read, which
    `tests/test_panel_declarations.py` fails on loudly. Returning it rather than
    raising matters because this runs at import: a parser that cannot be
    imported reviews nothing, and the failure would surface as a collection
    error naming no assertion."""
    text = prompt.replace("{{", "{").replace("}}", "}")
    # A FUNCTION replacement, not the string: `re.sub` reads a string repl as a
    # template, and a JSON-escaped marker's backslashes parse as escapes.
    mark = json.dumps(_TOKEN_MARK)
    text = re.sub(r"<[^<>]*>", lambda _: mark, text).replace("true|false", mark)
    for _, span in _spans(text, "{", "}"):
        if not any(f'"{k}"' in span for k in ENVELOPE_KEYS):
            continue
        try:
            val = json.loads(span)
        except json.JSONDecodeError:
            return None
        return _tokenise(val) if isinstance(val, dict) else None
    return None


#: The example each prompt ships, keyed by the envelope it illustrates — the one
#: value this module is willing to call a quotation rather than an answer.
SCHEMA_ECHOES = {"findings": _schema(REVIEW_PROMPT), "verdicts": _schema(JUDGE_PROMPT)}


def _example(key: str) -> dict | None:
    """The single example entry a schema ships inside its envelope."""
    items = (SCHEMA_ECHOES.get(key) or {}).get(key)
    return items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None


#: One findings entry / one verdicts entry, exactly as the prompt writes it.
SCHEMA_ITEMS = {k: _example(k) for k in ENVELOPE_KEYS}


def _standins(key: str) -> frozenset[str]:
    """The phrase(s) a schema puts in its declaration field in place of an
    answer — `could_not_assess: ["..."]`, `coverage_note: "..."`.

    Scoped to the one key it stands in for, which is what makes it safe: `"..."`
    is not an area any reviewer could mean, whereas `"F01"` and `"path"` are
    values the prompts explicitly ASK models to write, so no value elsewhere in
    a reply is ever read this way."""
    val = (SCHEMA_ECHOES.get(key) or {}).get(DECLARATION_KEYS[key])
    val = [val] if isinstance(val, str) else val if isinstance(val, list) else []
    return frozenset(s.strip() for s in val if isinstance(s, str))


SCHEMA_DECLARATIONS = {k: _standins(k) for k in ENVELOPE_KEYS}


def _quoted(val: object, schema: object) -> bool:
    """Whether `val` is `schema` handed straight back: the same keys, the same
    lists, the same TEXT, with the tokens that were never JSON resolved.

    Positive identification of the WHOLE example, and nothing weaker. Telling an
    echo from an answer by the strings it happens to share with the schema
    cannot work here, because the prompts ask models to write those strings —
    :data:`JUDGE_PROMPT` says an issue id is "a label YOU invent for an issue you
    are returning ('F01')" and illustrates `related` as `["F03"]`, and
    :data:`REVIEW_PROMPT` spells severities `"P1|P2|P3|P4"`. A rule that read
    those as quotation marks discarded `{"id": "F01", "members": [0], "real":
    true}` — a compliant terse verdict — and with it the judge's entire reply.
    Requiring every key and every phrase of the example is the only test a real
    answer cannot fail by accident.

    The wildcards are exactly the prompt's TOKENS (:data:`_TOKEN`), not every
    scalar. Both rules agree on today's schemas — every scalar in both comes from
    `<int|null>` or `true|false` — so this changes no current behaviour. What it
    changes is that the agreement stops being a coincidence: read as "any scalar
    quotes any scalar", the check says a real `real: false` quotes the example's
    `true` and a real `line: 42` quotes its `null`, and the first literal scalar
    added to either prompt would turn that from harmless into a rule that
    discards rulings."""
    if schema is _TOKEN:
        return _scalar(val)
    if isinstance(schema, dict):
        return (isinstance(val, dict) and set(val) == set(schema)
                and all(_quoted(val[k], schema[k]) for k in schema))
    if isinstance(schema, list):
        if not isinstance(val, list):
            return False
        # A token standing INSIDE a list — the judge's `"members": [<the bracketed
        # report NUMBERS ...>]` — stands for however many the model writes, so any
        # NON-EMPTY list of scalars quotes it. Empty does not: `"members": []` is a
        # verdict that named nobody, and calling that a quotation drops a real
        # ruling. Elsewhere the length is part of the example: an empty `findings`
        # is a review, not a quotation.
        if len(schema) == 1 and schema[0] is _TOKEN:
            return bool(val) and all(map(_scalar, val))
        return len(val) == len(schema) and all(map(_quoted, val, schema))
    if isinstance(schema, str):
        return isinstance(val, str) and val.strip() == schema.strip()
    # A literal scalar in a schema means itself. There are none today; the branch
    # is what stops one silently becoming a wildcard the day it is added.
    return val == schema


def _is_answer(item: object, kind: str = "findings") -> bool:
    """Whether one entry of a findings/verdicts array is an answer: an object,
    and not the example that entry's own prompt ships."""
    return isinstance(item, dict) and not _quoted(item, SCHEMA_ITEMS.get(kind))


class _Read(NamedTuple):
    """A candidate as the parser will KEEP it — not as the model wrote it.

    Equality over this is now the whole selection mechanism, so it has to
    compare meaning: `"severity": "p1"` and `"P1"`, an omitted `detail`,
    `"line": null` versus no line, `needs_rereview: true` versus the matching
    `fix_needs_rereview` index, `could_not_assess: "x"` versus `["x"]`, and any
    key the parser ignores are all spellings of one answer, and calling two of
    them an ambiguity spends a CLI call and the round's confidence on a reply
    nobody could have misread.

    Named rather than a positional tuple on purpose: `ReviewerRun`'s docstring
    records what a 4-tuple unpacked at three call sites cost this module once
    already."""
    #: What survives — parsed `Finding`s, or verdicts read the way
    #: :func:`_parse_verdicts` reads them.
    items: tuple
    #: The coverage declared, or None when the candidate never engaged with the
    #: question. The null-versus-`[]` distinction is load-bearing here too.
    declared: tuple[str, ...] | None


def _read(val: list | dict, want: str | None = None) -> _Read:
    """Read one candidate as the caller's parser will read it.

    `want` names the envelope the CALLER asked for. Without it a judge reply
    that happens to carry an incidental `findings` key would be read as a
    reviewer's review — the key precedence is positional, and both reply kinds
    share this extractor."""
    obj = val if isinstance(val, dict) else {}
    kinds = (want,) if want in ENVELOPE_KEYS else ENVELOPE_KEYS
    key = next((k for k in kinds if isinstance(obj.get(k), list)), None)
    kind = key or kinds[0]
    items = obj[key] if key else (val if isinstance(val, list) else [])
    # Read the declaration even with no envelope list beside it. `adjudicate`
    # accepts a coverage-only reply (`{"coverage_note": "..."}`) as a judge that
    # ruled on nothing rather than one that failed to rule, and gating this on
    # `key` left two such candidates carrying CONFLICTING notes both reading as
    # `_Read((), None)` — one answer, resolved by taking the last. Position
    # deciding which text survives is the whole thing this release removes, so
    # it cannot come back through the one reply shape that carries no items.
    declared = obj.get(DECLARATION_KEYS.get(kind))
    if kind == "verdicts":
        kept: tuple = tuple(_verdict_reading(it) for it in items if _is_answer(it, kind))
    else:
        kept = tuple(_findings_of("", obj, items))
    read = _declaration(declared, kind)
    return _Read(kept, None if read is None else tuple(read))


class _Ambiguous:
    """What :func:`_agreed` returns when a reply holds candidates that do not
    say the same thing. Distinct from None, which means "no candidate of this
    shape at all": one says the reply cannot be read, the other says keep
    looking. A class rather than a bare ``object()`` so a caller who forgets the
    ``is`` check gets `AMBIGUOUS` in its traceback."""

    def __repr__(self) -> str:
        return "AMBIGUOUS"


_AMBIGUOUS = _Ambiguous()


def _agreed(candidates: list[tuple[list | dict, _Read]]
            ) -> list | dict | _Ambiguous | None:
    """The one thing a set of candidates says, :data:`_AMBIGUOUS` when they do
    not agree, None when there are none.

    Nothing here chooses BETWEEN candidates, and that is the point. Quantity was
    tried and is not evidence: ranking by finding count let a model's own
    illustration — *"e.g. `{"findings": [{"severity": "P2", "file": "a.py",
    "title": "example only"}]}`"* — beat the real `{"findings": [],
    "could_not_assess": [...]}` beside it, which reports a fabricated finding
    under the reviewer's name AND discards the reviewer's real declaration.
    Preferring the last got that case right and the mirror wrong. Content cannot
    separate them either, because the prompts guarantee the overlap.

    So agreement is the only rule: a candidate the schema-quotation guard has
    already dropped is gone, and what remains either says one thing or is not
    resolved."""
    if not candidates:
        return None
    val, read = candidates[-1]
    return val if all(r == read for _, r in candidates) else _AMBIGUOUS


def extract_json_value(raw: str, want: str | None = None) -> list | dict | None:
    """Best-effort parse of the JSON value an LLM meant to return — an object
    envelope or a bare array — tolerating ``` fences and surrounding prose.
    `want` names the envelope the caller asked for (:data:`ENVELOPE_KEYS`), so a
    judge reply carrying an incidental `findings` key is not read as a review.

    EVERY top-level `{...}` and `[...]` span is tried, and the envelope wins over
    position: a span that parses into an object carrying the wanted key *whose
    value is a list* is the reply, whatever preceded it. Position alone is not
    enough, and got this wrong in the one case that matters — a prose `{...}`
    before the envelope fails to parse, and the next span by offset is the
    envelope's own INNER findings array, which parses cleanly and silently drops
    every declaration riding alongside it. A non-empty array is preferred next, so
    a bare findings array behind a prose object is not lost either; an object
    carrying an envelope key but not a list under it is prose ABOUT the schema and
    comes last.

    WITHIN a tier nothing is ranked and nothing is chosen. Two things happen, in
    this order:

    * a candidate that is the prompt's own example handed back is dropped — the
      whole example, positively identified against the schema text itself
      (:func:`_quoted`), not guessed at from strings it shares with it. Our own
      prompts ship `"could_not_assess": ["..."]`, `"coverage_note": "..."` and one
      fully populated example, so a quotation is textually full and says nothing;
      read literally it declares a coverage gap of "..." and files a P3 in a file
      called "path";
    * whatever remains must AGREE. One candidate is the answer; several that read
      the same are one answer; several that differ are not resolved.

    Not resolving is the whole design. Position was tried and is wrong in both
    directions — first-wins loses a review to an echo that precedes it, last-wins
    loses it to one that follows. Quantity was tried and is worse: it lets a
    model's own illustration outrank the real answer, which manufactures a finding
    no reviewer made. Content cannot separate an echo from an answer either,
    because the prompts ASK for the overlapping strings. What is left is
    agreement, and where there is none the reply is reported as unstructured
    (None). That path already exists and already degrades well — the caller
    retries once, then keeps the raw text as a finding and marks the round as
    carrying an unstructured reply. It costs a CLI call; it is the only outcome
    here that can never manufacture a clean review or a fabricated finding.

    Returns None when no valid JSON value is present, so callers can tell
    "parsed empty → flawless" apart from "unparseable → retry / keep raw text"
    rather than silently dropping the reviewer's work."""
    if not raw:
        return None
    spans = _spans(raw, "{", "}") + _spans(raw, "[", "]")
    found: list[tuple[int, int, list | dict]] = []
    for start, text in spans:
        try:
            val = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(val, (list, dict)):
            found.append((start, start + len(text), val))
    if not found:
        try:
            val = json.loads(raw.strip() or "null")
        except json.JSONDecodeError:
            return None
        found = [(0, len(raw), val)] if isinstance(val, (list, dict)) else []
    # A span INSIDE another span that parsed is part of it, not a rival to it: an
    # envelope's own `findings` array and its `could_not_assess` list are both
    # top-level `[...]` spans by the bracket scan, and once nothing ranks
    # candidates they would make every well-formed envelope disagree with its own
    # contents. Containment is only inferred from a span that PARSED, so prose
    # brackets never swallow the answer they surround.
    found.sort(key=lambda f: f[0])
    outer = [(s, e, v) for s, e, v in found
             if not any(s2 <= s and e <= e2 and (s2, e2) != (s, e) for s2, e2, _ in found)]
    # Read once, here: `_read` walks a candidate and the tiers overlap.
    parsed: list[tuple[list | dict, _Read]] = [
        # A value that is nothing BUT the schema quoted back is not a candidate at
        # all: keeping it would make every echo-then-answer reply an ambiguity, and
        # a reply holding nothing else would file the example finding as a review.
        (v, _read(v, want)) for _, _, v in outer
        if not any(_quoted(v, s) for s in SCHEMA_ECHOES.values())]

    keys = (want,) if want in ENVELOPE_KEYS else ENVELOPE_KEYS

    def envelope(val, shaped: bool) -> bool:
        # `shaped` also requires the envelope key to hold a LIST, which is what
        # the contract asks for. A sentence about the schema ("set `findings` to
        # an array of objects") parses as an object carrying the key and a string,
        # and that is not an answer — so a real array elsewhere in the reply beats
        # it, and it only wins over nothing at all.
        return isinstance(val, dict) and any(
            k in val and (not shaped or isinstance(val[k], list)) for k in keys)

    def says_something(read: _Read) -> bool:
        # A bare array the reader keeps NOTHING of is positively identifiable as
        # not-an-answer — the same standard `_quoted` applies to the schema echo,
        # and like it, no ranking choice is involved. Tier 2 admitted any
        # non-empty array, so an incidental prose bracket (`see [1]`, `the
        # severity on line [42]`, `[0, 3]` restating report ids) parsed, escaped
        # containment, was no echo, and became a RIVAL to a real findings array.
        # Two candidates that disagree is `_AMBIGUOUS` → a CLI retry → the reply
        # kept unstructured → `coverage_veto` filing "returned no structured
        # reply" and blocking a confident stop. All of it landing on exactly the
        # older, simpler reviewers the bare-array tier exists to serve.
        return bool(read.items) or read.declared is not None

    # Shape first, then agreement — every tier by the same rule, so the file does
    # not hold two contradictory answers to one question. The tiers are what keep
    # the bare array an older reviewer returns from being weighed against a prose
    # object that merely mentions the schema; the last two can only ever win when
    # nothing better exists: an object ABOUT the schema, then any JSON at all.
    for tier in ([c for c in parsed if envelope(c[0], shaped=True)],
                 [c for c in parsed
                  if isinstance(c[0], list) and c[0] and says_something(c[1])],
                 [c for c in parsed if envelope(c[0], shaped=False)],
                 parsed):
        pick = _agreed(tier)
        if pick is _AMBIGUOUS:
            return None
        if pick is not None:
            return pick
    return None


def _severity(raw, fallback: str) -> str:
    """A severity if it is one of ``SEVERITIES``, else the caller's fallback.

    Used at both ends: on the way IN from a reviewer (whose fallback is the
    panel's default) and on the way in from the judge (whose fallback is the
    reviewers' own call, a real answer that beats a made-up one). Normalising at
    parse time is what makes the judge-side fallback trustworthy — an
    unnormalised `'BLOCKER'` would otherwise sort before `'P1'`, win
    ``min(accounts, ...)``, head the fix list and count in no bucket at all."""
    sev = str(raw or "").strip().upper()
    return sev if sev in SEVERITIES else fallback


#: Spellings of "yes" a model reaches for when the contract asked for `true`.
#: Anything else — including "false", "no", "0" and every non-boolean shape — is
#: NOT a declaration.
_TRUTHY = frozenset({"true", "yes", "y", "1", "on"})


def _flag(val) -> bool:
    """A declared boolean, from output this parser deliberately treats as imperfect.

    Python truthiness is the wrong rule here: ``bool("false")`` is True, so a
    lenient model writing ``"needs_rereview": "false"`` would make a declaration
    it explicitly declined to make — and that flag both vetoes a stop and feeds
    the per-member honesty count, where a manufactured yes is the one error that
    cannot be spotted later. Real booleans and conventional boolean strings are
    honoured; everything else is no.
    """
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in _TRUTHY
    if isinstance(val, int):  # 1/0 from a model that sent a number
        return bool(val)
    # ...including one that sent `1.0`. `app/api/reviews.py::_count_or_none`
    # accepts an integral float for the same reason, and the two coercers must
    # not disagree about whether one model output was a declaration.
    if isinstance(val, float) and val.is_integer():
        return bool(val)
    return False


def _to_findings(reviewer: str, items: list) -> list[Finding]:
    return [Finding(
        reviewer=reviewer,
        severity=_severity(it.get("severity"), "P3"),
        file=str(it.get("file", "?")),
        line=it.get("line") if isinstance(it.get("line"), int) else None,
        title=str(it.get("title", "")).strip(),
        detail=str(it.get("detail", "")).strip(),
        needs_rereview=_flag(it.get("needs_rereview")),
    ) for it in items]


def _findings_of(reviewer: str, obj: dict, items: list) -> list[Finding]:
    """The findings a reviewer envelope yields, re-review flags resolved — the
    one place that turns a `findings` array into what the panel keeps, so
    :func:`_read` and :func:`parse_reply` cannot disagree about what a reply
    said.

    ``fix_needs_rereview`` indexes are into the array the MODEL wrote, which is
    not the list we keep: a junk entry among the findings is dropped, and every
    index after it would then point one finding too far, flagging its
    neighbour."""
    kept = [(i, it) for i, it in enumerate(items) if _is_answer(it, "findings")]
    out = _to_findings(reviewer, [it for _, it in kept])
    at = {sent: n for n, (sent, _) in enumerate(kept)}
    # A truthy non-list (`"fix_needs_rereview": 1`) used to reach `for i in ...`
    # and raise TypeError. That is not a parse failure the caller can degrade
    # from: `_read` calls this while READING CANDIDATES, so the crash escaped
    # the retry-then-keep-it-unstructured path entirely and took the run with
    # it. A malformed flag list costs its flags, never the review.
    flags = obj.get("fix_needs_rereview")
    for i in flags if isinstance(flags, list) else ():
        # Bools are ints in Python, and `true` here means nothing — index 1 is not
        # what a model that wrote a boolean meant.
        if isinstance(i, int) and not isinstance(i, bool) and i in at:
            out[at[i]].needs_rereview = True
    return out


def _verdict_reading(v: dict) -> tuple:
    """One verdict as :func:`_parse_verdicts` will read it — every field it
    consumes, normalised the way it normalises them, and nothing else.

    The judge's counterpart to :func:`_findings_of`, and needed for the same
    reason: two spellings of one ruling (`"severity": "p1"`, a `title` where a
    `synthesis` was asked for, a repeated member id, an extra key nobody reads)
    are one ruling, and the judge gets no retry when they are mistaken for
    two."""
    rel = v.get("related")
    return (
        str(v["id"]) if v.get("id") is not None else None,
        tuple(dict.fromkeys(_member_ids(v.get("members")))),
        _ruling(v.get("real")),
        _severity(v.get("severity"), ""),
        str(v.get("file") or "").strip(),
        v.get("line") if isinstance(v.get("line"), int) else None,
        str(v.get("synthesis") or v.get("title") or "").strip(),
        tuple(str(r) for r in rel) if isinstance(rel, list) else (),
        str(v.get("reason") or v.get("rationale") or "").strip(),
    )


def _str_list(val) -> list[str]:
    """A declaration list, however the model spelled it — a list of phrases, or
    one string it wrote instead of a one-item list.

    A non-string ITEM is dropped, not stringified: `could_not_assess:
    [{"area": "the migration"}]` used to become the Python repr
    `"{'area': 'the migration'}"`, which `/panel` then printed verbatim as words a
    reviewer had written, and a null or a nested list counted as a declared gap
    that says nothing. `app/api/reviews.py::_phrases` has always dropped them and
    documents itself as mirroring this function; the two now agree."""
    if isinstance(val, str):
        val = [val]
    if not isinstance(val, list):
        return []
    return [s for s in (x.strip() for x in val if isinstance(x, str)) if s]


def _declaration(val, kind: str = "findings") -> list[str] | None:
    """The coverage a candidate declares, or None when it never engaged with the
    question.

    Present-and-empty is "asked, and had nothing to declare"; absent — or a value
    no declaration can be read out of — is "said nothing", and collapsing the two
    would let a reviewer that never engaged read as one that engaged and reported
    clean. A declaration that is only the schema's own stand-in for this key is
    the second kind: `"could_not_assess": ["..."]` is :data:`REVIEW_PROMPT`
    quoted back, not an area the reviewer could not read, and recording it as one
    put `could not assess: ...` in the PR comment and cost the round its
    confidence.

    That check is per key and nothing wider (:data:`SCHEMA_DECLARATIONS`): the
    declaration field is the one place a schema's stand-in cannot also be a real
    answer, since `"..."` names no area. Findings and verdicts get no such
    treatment — `"F01"` and `"path"` are values the prompts ASK models to write,
    and reading them as quotation marks discarded whole replies."""
    if not isinstance(val, (list, str)):
        return None
    raw = [val] if isinstance(val, str) else val
    phrases = _str_list(val)
    # Wrote something, none of it readable: "said nothing", not "nothing to
    # declare". `could_not_assess: [{"area": "the migration"}]` is a reviewer
    # naming a gap in a shape the parser will not take, and the two branches
    # used to disagree about it — a value of the wrong TYPE was None while a
    # value whose ITEMS were all unreadable was [], which `coverage_veto` reads
    # as a clean seat. That let a round be recorded confident on a reviewer that
    # had said out loud it could not assess something, which is the one thing
    # this module promises never to do: nothing is read as cleaner than it was.
    # Only an EMPTY list means the reviewer was asked and had nothing to say.
    if raw and not phrases:
        return None
    kept = [p for p in phrases if p not in SCHEMA_DECLARATIONS.get(kind, ())]
    return kept if kept or not phrases else None


def parse_reply(reviewer: str, raw: str) -> tuple[list[Finding], list[str] | None] | None:
    """Parse a reviewer's reply into (findings, could_not_assess).

    Two shapes are accepted, because the panel's members are four different CLIs
    and a contract change lands on them at different speeds:

    * ``{"findings": [...], "could_not_assess": [...]}`` — the current envelope,
      which carries the reviewer's own coverage declarations.
    * a bare ``[...]`` of findings — every reviewer before this, and any model
      that ignores the envelope. It declares NOTHING, which is ``None`` rather
      than ``[]``: the board's contract is that null means never asked/never
      said and ``[]`` means asked and had no gap, and a member whose CLI ignored
      the envelope must not read as one that engaged and reported clean.

    ``fix_needs_rereview`` (a list of INDEXES into the findings array just
    returned) is a TOLERATED alternative spelling, not what reviewers are asked
    for: :data:`REVIEW_PROMPT` documents only the per-finding ``needs_rereview``
    boolean. Both are honoured, so a model that reaches for the index form is
    still heard.

    Returns None when the reply has no usable JSON at all (caller retries, then
    keeps the raw text as one finding) — distinct from ``([], None)``/``([], [])``
    which mean the reviewer ran and found nothing. A findings array the model
    filled with something OTHER than an answer — a list of sentences most often,
    or the example finding from the prompt quoted back — is the first kind and not
    the second: every entry is dropped by :func:`_findings_of`, and reporting the
    empty remainder would turn a reply this parser could not read into a reviewer
    that read the diff and found it flawless."""
    val = extract_json_value(raw, "findings")
    if val is None:
        return None
    obj = val if isinstance(val, dict) else {}
    items = val if isinstance(val, list) else obj.get("findings")
    if not isinstance(items, list):
        return None
    findings = _findings_of(reviewer, obj, items)
    # A findings array with nothing readable in it takes the declaration beside it
    # down too. That loses a `could_not_assess` the model stated plainly, which is
    # a real cost and recorded here so it is not mistaken for an oversight: the
    # caller retries and then marks the round `unstructured`, which vetoes it
    # harder than the declaration would have, so nothing is read as cleaner than
    # it was — and half a reply is not evidence about which half was mis-typed.
    # (The bare-array shape says the same thing the same way: an array of
    # sentences is a reply we could not read, an empty one is a clean review.)
    if items and not findings:
        return None
    return findings, _declaration(obj.get("could_not_assess"))


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
    return (any(m in low for m in REJECTION_MARKERS)
            or '"status":400' in low.replace(" ", ""))


def is_permission_denied(stderr: str) -> bool:
    """Did the CLI's OWN sandbox refuse a tool the run needed?

    Deliberately a sibling of is_rejection rather than part of it, because the
    two are settled in different files and the report must not conflate them: a
    rejection is the SERVER declining the request (a retired model pin — fix
    `.harness-rules`), this is the local CLI auto-denying a tool because headless
    mode has no one to prompt (fix `permissions.allow` in its settings.json).
    What they share is the only property retrying cares about — both are decided
    by configuration, so all three attempts fail identically.

    The observed shape, from `agy` 1.1.12: exit 0, empty stdout, and 'a tool
    required the "command" permission that headless mode cannot prompt for, so
    it was auto-denied' on stderr.

    Matched as a CO-OCCURRENCE ON ONE LINE — a permission word next to a
    headless-denial word — rather than either token anywhere in the stream, and
    that narrowness is the point. This predicate now suppresses retries on
    non-zero exits too, so every over-match costs a reviewer its remaining
    attempts and the panel a whole vendor for the round. The shapes it must NOT
    claim: an `EACCES: permission denied` on a temp file (a real error, and a
    transient one as often as not), a CLI echoing its own `permissions.allow`
    config at startup, and a log line about one optional tool being auto-denied
    on a run that then fails for a rate limit — all of which used to match, and
    turned three attempts into one.
    """
    for line in stderr.lower().splitlines():
        if "permission" in line and any(d in line for d in DENIAL_MARKERS):
            return True
    return False


def is_deterministic_failure(stderr: str) -> bool:
    """Will another identical attempt fail in the identical way? Either settled
    cause counts — a request the server refused, or a tool the CLI refused."""
    return is_rejection(stderr) or is_permission_denied(stderr)


def run_cli(args: list[str] | Callable[[], list[str]], label: str, timeout: int = CLI_TIMEOUT,
            attempts: int = 3, stdin_text: str | None = None,
            on_output: Callable[[str | None], None] | None = None,
            replied: Callable[[], bool] | None = None,
            cwd: str | None = None) -> tuple[str | None, str | None]:
    """Run a headless CLI, returning (stdout, error_reason); error_reason is
    None on success. Retries transient failures (non-zero exits such as rate
    limits, and OS errors) up to `attempts` times with no delay — these fail
    fast, so retrying is cheap and recovers the common flake. A full timeout is
    NOT retried (it already burned the whole budget; retrying just doubles the
    wall-clock).

    **`cwd` is the repo under review, and passing it is what makes a seat
    reproducible.** Without it every reviewer inherited whatever directory the
    panel process happened to be started from, so a run's membership was decided
    by ambient state that nothing configured, nothing recorded, and nothing could
    reproduce. That is not hypothetical: on PR #64 codex exited 1 with "Not
    inside a trusted directory and --skip-git-repo-check was not specified" while
    the two panels launched beside it in the same second ran codex fine. The
    inputs were not in fact identical — those panels were started from inside a
    git checkout and that one from a scratch directory under /tmp, and codex
    refuses to start outside a repo. The panel lost a whole vendor's eyes to the
    caller's shell, and #68 is the report that reads the same either way.

    Pinning it to the repo satisfies that check by construction (`--repo` always
    resolves to a checkout), which is why codex needs no `--skip-git-repo-check`
    here — verified against an untrusted checkout AND an untrusted *worktree*,
    where the `.git` file rather than directory was the open question. The flag
    would buy nothing and would trade a guard for it.

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
    reporting a bare 'unavailable'. A failure something has already SETTLED is
    not retried either, whichever exit code it arrives with — a bad model pin the
    server refuses, or a tool the CLI's own sandbox auto-denies (see
    is_deterministic_failure) — because it fails identically all three times, so
    retrying only triples the wait for a certainty.

    **A zero exit with no output is a failure, not an empty review.** Headless
    CLIs exit 0 while producing nothing — `agy` does it when a tool needs a
    permission headless mode cannot prompt for, so it is auto-denied — and
    "reviewed, found nothing" and "produced nothing" are opposite claims that a
    bare `""` cannot tell apart. Returned as success it becomes a reviewer that
    appears in the report as having run, contributes no findings, weakens the
    ⋆consensus signal with no explanation, and feeds the board's reviewer
    leaderboard a false zero — the one datum a reviewer comparison must be able
    to trust. So callers may rely on the invariant that a non-None stdout has
    non-whitespace content.

    Stderr is read on a ZERO exit too, for the same run. The CLI has usually
    already diagnosed itself there ("a tool required the \"command\" permission
    … add an allow-rule under permissions.allow"), and gating that read on a
    non-zero exit discarded it on exactly the runs that needed it most. It is
    read only when stdout is empty: a CLI that produced its findings AND chattered
    on stderr succeeded, and reporting its warm-up noise would be the opposite
    error. A blank reply IS retried when nothing says it would come back blank —
    losing a whole reviewer to one flake costs the panel more than two extra
    attempts. Two things stop that retry: stderr naming a settled cause
    (is_deterministic_failure — a refused request, or a tool the CLI auto-denied;
    a missing permission rule is every bit as fixed as a bad model pin), and the
    attempt having taken longer than BLANK_RETRY_MAX_S. The second is what keeps
    the flake recovery from inheriting the cost the timeout branch refuses:
    blank runs do not fail fast the way non-zero exits do, so three SLOW ones is
    up to three whole CLI_TIMEOUTs held against the joined futures of the whole
    panel, 3x the duration_ms the board's leaderboard is scored on, and on the
    metered `pi` seat, three bills.

    `args` may be a CALLABLE returning the argv, for a command line that cannot
    be reused verbatim: `claude --session-id` refuses an id that already exists
    ("Session ID … is already in use"), so a reviewer whose session is pinned
    needs a fresh one per attempt or the retry that exists to recover a flake
    fails every time by construction.

    `on_output` is handed EVERY attempt's stdout, including the ones that then
    failed. Only the last is returned, but an attempt that burned tokens before
    exiting non-zero still spent them, and a caller reading usage off stdout
    (codex) would otherwise under-report exactly the seat that is flaking."""
    last = f"{label}: no attempt made"
    feed = {"input": stdin_text} if stdin_text is not None else {"stdin": subprocess.DEVNULL}
    for _ in range(max(1, attempts)):
        argv = args() if callable(args) else args
        started = time.monotonic()
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout, cwd=cwd, **feed)
        except subprocess.TimeoutExpired as e:
            # A timeout is the most expensive outcome the panel has: the model
            # read the whole diff and thought about it for the full budget before
            # being killed. Dropping its stdout here recorded that as costing
            # NOTHING — and it lands on codex, the one seat whose usage is read
            # only from stdout. `TimeoutExpired.stdout` carries what was printed
            # before the kill, as BYTES even under `text=True` (it is filled in by
            # `_check_timeout`, which never decodes), so it is decoded here.
            partial = e.stdout
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", "replace")
            if on_output:
                on_output(partial)
            return None, f"{label}: timed out after {timeout}s"
        except OSError as e:
            # errno and strerror, not the bare class name: "OSError" sent three
            # people looking for a crash that was "Argument list too long", and
            # everything needed to name it was already on the exception.
            why = " ".join(str(x) for x in (e.errno, e.strerror or e) if x)
            last = f"{label}: OSError {why}"[:300]
            continue
        # Before the outcome check, and on every attempt: a run that burned
        # tokens and then failed still spent them, so a caller reading usage off
        # stdout must see the losing attempts too or it under-reports exactly
        # the seat that is flaking.
        if on_output:
            on_output(proc.stdout)
        # One branch for both failure shapes on purpose: they differ only in the
        # sentence, and split they were two copies of the same short-circuit for
        # the next failure class to have to be added to twice.
        outcome = cli_outcome(proc)
        # `cli_outcome` asks whether STDOUT is empty, which stops being the right
        # question the moment a seat's stdout is not its reply. codex under
        # `--json` always prints events (thread.started / item.completed /
        # turn.completed), so that test can never fire for it again — and with it
        # went the up-to-3 blank-reply retries and the BLANK_RETRY_MAX_S rule, on
        # the one seat whose reply lands in a file. That is v2.17's guarantee
        # ("a reviewer that produced nothing has failed, and says why") silently
        # lost. `replied` lets such a seat answer the question about the thing
        # that actually carries its reply.
        if not outcome and replied is not None and not replied():
            outcome = "exited 0 but wrote no reply"
        if not outcome:
            return proc.stdout, None
        took = time.monotonic() - started
        msg = stderr_gist(proc.stderr or "")
        last = f"{label}: {outcome}" + (f" ({msg})" if msg else "")
        if is_deterministic_failure(proc.stderr or ""):
            return None, last
        if not proc.returncode and took >= BLANK_RETRY_MAX_S:
            return None, f"{last} after {int(took)}s — not retried"
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

    What is recorded is the canonical finding list, not counts: each issue with
    its synthesis, every reporter's account, the run's `related` links, and a
    `key` per finding. The board scopes those keys by (repo, PR), so a later run
    of the same PR — a re-review after a fix, a reviewer recovered after a
    timeout — joins each finding to the earlier observation of the same defect
    rather than starting a fresh chain. The key is derived from the reviewers'
    own words (see :func:`_defect_key`) precisely so it survives the judge
    re-wording its synthesis between runs, which the board's own fallback
    derivation would not.

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


def diff_budget(block: dict, key: str, fallback: int | None,
                notes: list[str]) -> int | None:
    """How much diff one model is given, from config, with the inherited value as
    the fallback. ``None`` — the default — means the whole diff, uncut.

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
    said = f"{fallback:,}" if fallback is not None else "the whole diff"
    n = None
    if not isinstance(raw, bool) and isinstance(raw, (int, str)):
        try:
            n = int(raw)
        except ValueError:
            n = None
    if n is None:
        notes.append(f"`{key}`={raw!r} is not a number — using {said}")
        return fallback
    if n <= 0:
        notes.append(f"`{key}`={n:,} would send no diff at all — using {said}")
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


def codex_args(model: str, effort: str, reply_file: Path | None = None) -> list[str]:
    """codex exec argv. Both knobs are optional and independent: effort is a
    `-c` config override rather than a flag, and applies to the CLI's default
    model just as well as to a pinned one.

    Takes no prompt: `codex exec` with no positional argument reads its
    instructions from stdin, which is where the diff goes. The parameter is gone
    rather than ignored so the argv-limit bug cannot be reintroduced by passing
    one (see ARGV_PROMPT_MAX_BYTES).

    `reply_file` turns on the pair that gets usage out of this seat WITHOUT
    wrapping the findings: `--json` puts the event stream (which carries
    `turn.completed.usage`) on stdout, and `--output-last-message` writes the
    model's reply to that file as plain text — the same text stdout used to
    carry, read by the same parser. codex is the one member that cannot pin a
    session id for a new run, so its usage has to come off the stream; the
    alternative, matching a rollout under `~/.codex/sessions/` after the fact,
    races the up-to-4 concurrent panels `/panel-review-pr` fans out.
    """
    args = ["codex", "exec"]
    if model:
        args += ["--model", model]
    if effort:
        args += ["-c", f"model_reasoning_effort={effort}"]
    if reply_file is not None:
        args += ["--json", "--output-last-message", str(reply_file)]
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
    findings array inside an escaped string where parse_reply's balanced-bracket
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


def pi_args(model: str, effort: str, session_id: str, session_dir: Path) -> list[str]:
    """pi argv. `-p` is its non-interactive mode; `--no-tools` is what makes it a
    REVIEWER — pi ships read/bash/edit/write, and a panel member has no business
    editing the tree it is reviewing. The diff arrives on stdin, so it needs no
    tools to do the job, and `--no-tools` is a real guarantee that it has none.

    `--session-id` + `--session-dir` replace what used to be `--no-session`. The
    reason for `--no-session` still holds — a panel run is not a conversation
    anyone resumes — and is now served better: the session is written into a
    per-run temporary directory that is deleted when the member returns, so it
    still never reaches the user's session store, and on the way out it is read
    for what the turn cost. pi is the one seat that states a cost of its own.

    The id is pinned UP FRONT rather than matched afterwards because
    `/panel-review-pr` fans out up to 4 concurrent panels, each running its own
    copy of each reviewer — picking a session by mtime would hand one panel
    another's numbers.

    pi reaches many providers, so `model` here is a full `provider/id` pattern
    (`openrouter/moonshotai/kimi-k3`) rather than a bare slug, and its thinking
    level is spelled `--thinking` where codex spells it `model_reasoning_effort`.
    Same knob, same config key (`effort`), different word on each CLI.

    Takes no prompt, for the same reason codex_args does not: `pi -p` reads it
    from stdin."""
    args = ["pi", "-p", "--session-id", session_id, "--session-dir", str(session_dir),
            "--no-tools"]
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


def _int(v: object) -> int:
    """A usage figure as an int, or 0 — vendors omit fields they have nothing for.

    An INTEGRAL float counts. `455.0` is ordinary in JSON emitted from a language
    with one number type, neither codex's nor pi's schema is pinned here, and
    reading it as 0 was the worst available answer: `found` is set from the
    presence of the usage dict rather than from a non-zero total, so the run was
    recorded as instrumented AND free — a zero the board cannot tell from a
    measured one, which is the single outcome this feature exists to prevent.
    A fractional figure is still 0: no vendor bills 1.5 tokens, so it is a shape
    nobody meant and quietly truncating it would invent a number.
    """
    if isinstance(v, bool):
        return 0
    if isinstance(v, float):
        return int(v) if v.is_integer() else 0
    return v if isinstance(v, int) else 0


def _jsonl(path: Path) -> list[dict]:
    """Every JSON object in a JSONL file, skipping whatever doesn't parse.

    Session transcripts are written as the turn runs, so the last line can be a
    half-flushed one; a partial tail costs a message, never the read.
    """
    out: list[dict] = []
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return out
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if isinstance(o, dict):
            out.append(o)
    return out


def _usage(inp: int, out: int, cached: int, reasoning: int,
           cost: float | None = None, observed: set[str] | None = None) -> dict:
    """One member's spend, normalised so the four fields mean the same everywhere.

    Every vendor slices this differently, so the shape is pinned here rather than
    at each call site:

    * ``input_tokens`` — EVERY prompt-side token, cache hits and cache writes
      included. Claude reports the uncached remainder under that name and pi
      reports cache reads *beside* input rather than inside it, so taking either
      vendor's `input` verbatim would report a 60k-char diff as a 2-token prompt.
    * ``cached_input_tokens`` — the cached slice OF that input, never a sibling.
    * ``output_tokens`` — completion tokens.
    * ``reasoning_tokens`` — thinking, which every vendor here counts INSIDE
      output. It is reported alongside, never added, or the seats that think
      would be double-charged for it.

    Even normalised these compare only *within* a vendor: different tokenizers,
    different cache semantics. Duration is the cross-vendor axis.
    """
    # `observed` names the fields the vendor actually stated. Omitted keys are
    # absent from the payload entirely, so the board stores NULL — "not
    # recorded" — instead of a measured zero. Without it every reader emitted
    # all four unconditionally and `_int(u.get(...))` supplied 0 for a key the
    # vendor never mentioned, so pi omitting `reasoning` or codex omitting the
    # cache figure on a cold turn both became stated zeroes that /panel then
    # averaged in as fact. `ReviewerIn` advertises these as "all independently
    # optional"; this is what makes that true per FIELD and not just per seat.
    # None means "the caller observed everything it is passing", which is what a
    # test constructing a full block means.
    every = ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens")
    values = dict(zip(every, (inp, out, cached, reasoning)))
    u = {k: v for k, v in values.items() if observed is None or k in observed}
    # Only where the VENDOR states it. Never tokens times a price table: a run
    # priced at today's rates is silently wrong when the board is queried in six
    # weeks, and the record is meant to still be true then.
    if cost is not None:
        u["cost_usd"] = round(cost, 6)
    return u


def claude_usage(session_ids: list[str]) -> dict | None:
    """What a pinned `claude -p` member cost, read back out of its transcripts.

    Takes every session the member used, not one: a retry has to run under a
    FRESH id (claude refuses one that already exists), so a member that flaked
    once and landed on the second attempt genuinely spent two turns and is
    charged for both.

    Each is located by GLOB on the id rather than by rebuilding the project-slug
    directory name from the cwd: the id is unique, so the glob is unambiguous,
    and it does not break the day the slug rule changes.

    Usage is per assistant message and summed across the turn — but the
    transcript writes a message more than once (a streamed one lands twice with
    the same `message.id` under different line `uuid`s), so identical blocks are
    deduped by that id first. Summing the lines naively double-counts every
    streamed reply, which reads as a reviewer costing twice what it did.
    """
    inp = out = cached = reasoning = 0
    #: Which of the four normalised fields the transcript actually stated. A key
    #: the vendor never wrote must reach the board as null, not as a measured 0.
    observed: set[str] = set()
    seen: set[str] = set()
    for session_id in session_ids:
        # EVERY match, not `files[0]`. `Path.glob` returns filesystem order, so
        # one session id resolving under two project slugs made the read
        # nondeterministic — a different number on different runs, with nothing
        # to say which. Summing them is safe here because the message-id dedup
        # below already collapses a record seen twice.
        files = sorted(Path.home().glob(f".claude/projects/*/{session_id}.jsonl"))
        if not files:
            continue
        for rec in [r for f in files for r in _jsonl(f)]:
            msg = rec.get("message")
            if rec.get("type") != "assistant" or not isinstance(msg, dict):
                continue
            u = msg.get("usage")
            if not isinstance(u, dict):
                continue
            # A record identifying itself in none of the three ways cannot be
            # deduped, so it is counted. Falling through to a bare `None` put
            # one None in `seen` and then skipped EVERY later id-less message in
            # the turn as its duplicate — the exact inverse of the double-count
            # this guard was written to stop, and in the direction that flatters
            # an expensive seat by under-reporting it.
            mid = msg.get("id") or rec.get("requestId") or rec.get("uuid") or object()
            if mid in seen:
                continue
            seen.add(mid)
            cache_read = _int(u.get("cache_read_input_tokens"))
            # Claude's `input_tokens` is only the part it neither cached nor read
            # from cache; the whole prompt is all three added up.
            inp += (_int(u.get("input_tokens"))
                    + _int(u.get("cache_creation_input_tokens")) + cache_read)
            cached += cache_read
            out += _int(u.get("output_tokens"))
            for key, field in (("input_tokens", "input_tokens"),
                               ("cache_creation_input_tokens", "input_tokens"),
                               ("cache_read_input_tokens", "input_tokens"),
                               ("cache_read_input_tokens", "cached_input_tokens"),
                               ("output_tokens", "output_tokens")):
                if key in u:
                    observed.add(field)
            details = u.get("output_tokens_details")
            if isinstance(details, dict):
                reasoning += _int(details.get("thinking_tokens"))
                if "thinking_tokens" in details:
                    observed.add("reasoning_tokens")
    if not seen:
        return None
    # No cost: the transcript states none. `--output-format json` does put one on
    # stdout, but that mode wraps the findings in an envelope, which is the trade
    # this whole approach exists to refuse.
    return _usage(inp, out, cached, reasoning, observed=observed)


def pi_usage(session_dir: Path, session_ids: list[str]) -> dict | None:
    """What a pinned `pi -p` member cost, from the sessions it was told to write.

    One per attempt, like claude's — pi would happily RESUME an id it already
    has, but then a retry would carry the failed reply into its context and stop
    being the independent second shot the caller asked for. A fresh id per
    attempt keeps the old `--no-session` semantics and still charges for both.

    pi names each file `<timestamp>_<session-id>.jsonl`, so this globs on the id
    rather than assuming the timestamp. It is the one seat that states a cost of
    its own, which is therefore the one recorded — never a derived figure.
    """
    inp = out = cached = reasoning = 0
    cost = 0.0
    stated = False
    found = False
    observed: set[str] = set()
    for session_id in session_ids:
        # EVERY match, not `sorted(files)[0]`. Taking the earliest timestamp
        # dropped the later, larger half of a turn if pi ever rolled one session
        # into a second file — under-charging the seat with no signal that
        # anything was missing. The id is unique, so extra matches are more of
        # the same session rather than a different one.
        files = sorted(session_dir.glob(f"*_{session_id}.jsonl"))
        if not files:
            continue
        for rec in [r for f in files for r in _jsonl(f)]:
            msg = rec.get("message")
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            u = msg.get("usage")
            if not isinstance(u, dict):
                continue
            found = True
            # pi reports cacheRead/cacheWrite BESIDE input, not inside it (its own
            # totalTokens adds all of them), so the prompt total is the sum.
            cache_read, cache_write = _int(u.get("cacheRead")), _int(u.get("cacheWrite"))
            inp += _int(u.get("input")) + cache_read + cache_write
            cached += cache_read
            out += _int(u.get("output"))
            reasoning += _int(u.get("reasoning"))
            for key, field in (("input", "input_tokens"), ("cacheRead", "input_tokens"),
                               ("cacheWrite", "input_tokens"),
                               ("cacheRead", "cached_input_tokens"),
                               ("output", "output_tokens"),
                               ("reasoning", "reasoning_tokens")):
                if key in u:
                    observed.add(field)
            c = u.get("cost")
            if isinstance(c, dict) and isinstance(c.get("total"), (int, float)):
                cost += float(c["total"])
                stated = True
    if not found:
        return None
    return _usage(inp, out, cached, reasoning, cost if stated else None,
                  observed=observed)


def codex_usage(stdout: str | None) -> dict | None:
    """What a `codex exec --json` turn cost, from its own event stream.

    codex cannot pin a session id for a NEW run (only `resume`), and picking our
    rollout out of `~/.codex/sessions/` by mtime races the up-to-4 concurrent
    panels `/panel-review-pr` fans out. So this seat reads usage off stdout
    instead — which it can do without putting the findings in an envelope,
    because `--output-last-message` hands those over as plain text in a file.

    Summed over `turn.completed` events rather than taking the last, so a run
    that took more than one turn is charged for all of them.
    """
    inp = out = cached = reasoning = 0
    observed: set[str] = set()
    found = False
    for ln in (stdout or "").splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        u = o.get("usage")
        if o.get("type") != "turn.completed" or not isinstance(u, dict):
            continue
        found = True
        # codex's `input_tokens` is already the whole prompt, cached reads
        # included — so unlike pi's, nothing is added to it. `cached_input_tokens`
        # is recorded as the slice of it that was cached.
        inp += _int(u.get("input_tokens"))
        cached += _int(u.get("cached_input_tokens"))
        out += _int(u.get("output_tokens"))
        reasoning += _int(u.get("reasoning_output_tokens"))
        for key, field in (("input_tokens", "input_tokens"),
                           ("cached_input_tokens", "cached_input_tokens"),
                           ("output_tokens", "output_tokens"),
                           ("reasoning_output_tokens", "reasoning_tokens")):
            if key in u:
                observed.add(field)
    return _usage(inp, out, cached, reasoning, observed=observed) if found else None


def review_llm(cmd_name: str, model: str, prompt: str,
               effort: str = "", cwd: str | None = None) -> ReviewerRun:
    """Run a headless LLM CLI reviewer. Returns a :class:`ReviewerRun` — what it
    found, what it could not judge, and what it cost.

    `cwd` is the repo under review — see run_cli, where the reason it is a
    parameter rather than whatever the shell was in is written down. It is
    threaded rather than read from a module global because the reviewers run
    concurrently and a global would make the seat depend on run ORDER, which is
    the same defect wearing different clothes.

    Duration is wall-clock for this member's whole turn — every CLI attempt it
    made, including the reparse retry below, because a reviewer that only lands
    on the second try genuinely costs twice. It is measured even on the failure
    paths: how long a member took to NOT produce findings is exactly what you
    want to know about a reviewer that times out. Config errors that return
    before any process starts report ~0, which is honest — nothing ran.

    **Tokens are read back out of a pinned session, not out of a JSON output
    mode.** Every vendor's JSON mode moves the reply inside an envelope
    (``.result``, ``.response``, ``item.completed``, ``.message.content[]``), so
    ``parse_reply`` would need four bespoke unwrappers — four new failure modes
    on the path that currently works, added to gain telemetry. Pinning inverts
    that risk: a transcript that cannot be read loses a number, while a broken
    unwrapper loses the findings on every run. So each seat keeps its plain-text
    reply and its session id is fixed UP FRONT — matching a session afterwards
    by mtime would race the up-to-4 concurrent panels that `/panel-review-pr`
    fans out.

    This is the cost side of the board's scorecard. "Finds more" is only half an
    answer; the panel is a choice about where to spend, and without this the
    /panel leaderboard could rank a member top on findings while it was quietly
    the most expensive seat on the panel.
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
        return ReviewerRun(skip=f"{label}: {CLI_ABSENT}", duration_ms=elapsed(),
                           absent=True)

    # A private directory per member per run holds whatever telemetry that CLI
    # needs somewhere to put (pi's session, codex's reply file). Removed however
    # this returns, so a panel that runs all day leaves nothing behind.
    with tempfile.TemporaryDirectory(prefix=f"panel-{cmd_name}-") as tmp:
        tmpdir = Path(tmp)
        #: One reply path per codex ATTEMPT, in the order they were made; empty
        #: for every other seat. A single shared path let an attempt that wrote
        #: no `--output-last-message` serve the PREVIOUS attempt's text as its
        #: own findings, and made the reparse retry a guaranteed no-op for codex
        #: alone — it re-read the same bytes while still costing a full turn of
        #: tokens. The reply is the last attempt's, and a file it never wrote is
        #: no reply rather than whatever happens to be on disk.
        replies: list[Path] = []
        #: Every session this member opened — one per CLI attempt, because a
        #: pinned id cannot be reused (claude: "Session ID … is already in use")
        #: and reusing pi's would turn the retry into a continuation of the reply
        #: it is retrying. Usage is read back from all of them, so a member that
        #: flaked once and landed on the second attempt is charged for both.
        sessions: list[str] = []

        def new_session() -> str:
            # A BARE uuid4, with no readability prefix: `claude --session-id`
            # refuses anything that is not a valid UUID, which would fail every
            # claude review rather than merely lose its token count. pi accepts
            # any string, so one form serves both.
            sessions.append(str(uuid.uuid4()))
            return sessions[-1]

        # The prompt goes on stdin wherever the CLI will take it there — which is
        # everywhere but `agy`. That is not a style choice: a diff big enough to be
        # worth a panel is big enough to exceed the kernel's per-argument limit, and
        # in argv that failure lands at execve, before the reviewer exists, as an
        # error with nothing in it. On stdin there is no such ceiling.
        stdin_text: str | None = prompt
        # A thunk, not a fixed argv, for the seats that pin a session: run_cli
        # retries a flake up to three times, and each attempt needs its own id.
        args: list[str] | Callable[[], list[str]]
        #: Does this seat deliver its reply in a FILE rather than on stdout? Only
        #: codex does, and it is what makes the stdout-emptiness test the wrong
        #: question for it.
        replies_used = cmd_name not in ("claude", "antigravity", "pi")
        if cmd_name == "claude":
            def args():
                return ["claude", "-p", "--model", model, "--session-id", new_session()]
        elif cmd_name == "antigravity":
            # Not instrumented: `agy` has no session-id to pin, and its usage
            # lives only in the JSON mode this design declines. It reviews
            # exactly as before and reports no tokens, which the board renders as
            # "not recorded" rather than as zero.
            args, stdin_text = antigravity_args(model, effort, prompt), None
        elif cmd_name == "pi":
            def args():
                return pi_args(model, effort, new_session(), tmpdir)
        else:
            def args():
                replies.append(tmpdir / f"reply-{len(replies)}.txt")
                return codex_args(model, effort, replies[-1])

        #: Every attempt's stdout, failed ones included — codex reads its usage
        #: from there, and an attempt that burned tokens before exiting non-zero
        #: still spent them. The session-pinned seats get the same completeness
        #: from `sessions` above.
        outputs: list[str] = []

        def collect(stdout: str | None) -> None:
            if stdout:
                outputs.append(stdout)

        def usage_of() -> dict | None:
            """What this member spent across every attempt it made, or None.

            Deliberately catching everything: this is the last line of the
            guarantee the whole design is built on — a review that has already
            succeeded must not fail because a transcript moved, changed shape, or
            grew a field of a type the reader didn't expect. The cost of being
            wrong here is one missing number, and it is announced rather than
            swallowed silently.
            """
            try:
                if cmd_name == "claude":
                    return claude_usage(sessions)
                if cmd_name == "pi":
                    return pi_usage(tmpdir, sessions)
                if cmd_name == "codex":
                    return codex_usage("\n".join(outputs))
            except Exception as e:  # noqa: BLE001 - telemetry never fails a review
                print(f"panel: no usage for {label} ({e.__class__.__name__})", file=sys.stderr)
            return None

        def reply_of(stdout: str | None) -> str | None:
            """The reviewer's actual reply text for this attempt.

            codex is the one seat whose stdout is not its reply: `--json` puts
            events there and `--output-last-message` puts the reply in a file, so
            the findings still arrive as plain text and never as an envelope to
            unwrap. If the file is missing the run produced no reply, which the
            caller already handles as empty output.

            The LAST attempt's file, matching `run_cli`, which returns the last
            attempt's stdout. Reading a fixed path instead meant a failed final
            attempt inherited an earlier one's reply.
            """
            if not replies:
                return stdout
            try:
                return replies[-1].read_text()
            except OSError:
                return None

        # `replied` only for the seat whose stdout is not its reply. For every
        # other seat `cli_outcome`'s stdout test is still exactly right, and
        # passing a predicate would be a second way to ask one question.
        wrote_reply = (lambda: bool(replies) and replies[-1].exists()
                       and replies[-1].read_text().strip()) if replies_used else None
        out, err = run_cli(args, label, stdin_text=stdin_text, on_output=collect,
                           replied=wrote_reply, cwd=cwd)
        if err:
            err += cli_hint(cmd_name, err, model)
            # A member that burned tokens and then failed still spent them, so
            # the usage is reported on this path too.
            return ReviewerRun(skip=err, duration_ms=elapsed(), usage=usage_of())

        text = reply_of(out)
        parsed = parse_reply(cmd_name, text)
        if parsed is None:
            # Unparseable JSON — give the reviewer one more shot (a common flake is a
            # stray prose preamble the model omits on a retry), then, rather than drop
            # its work, keep the raw reply as a single markdown finding for the judge.
            # The retry costs another turn, which `usage_of` already counts: it runs
            # under its own fresh session, and its stdout lands in `outputs` too.
            out2, err2 = run_cli(args, label, attempts=1, stdin_text=stdin_text,
                                 on_output=collect, replied=wrote_reply, cwd=cwd)
            retry_text = reply_of(out2) if not err2 else None
            if retry_text:
                retried = parse_reply(cmd_name, retry_text)
                if retried is not None:
                    return ReviewerRun(retried[0], None, elapsed(), retried[1],
                                       usage=usage_of())
                text = retry_text
            usage = usage_of()
            raw = (text or "").strip()
            # Unreachable today — run_cli refuses to return whitespace-only stdout —
            # and kept anyway, because it is the LOCAL half of the guard. The
            # invariant that makes it dead lives ~350 lines away in a docstring, and
            # the day it is relaxed (a new caller, a check_output=False variant, a
            # mocked run_cli in a future test) this line is all that stands between
            # the judge and a blank finding flagged `unstructured` — a dead reviewer
            # wearing a live one's clothes, which is the failure this file exists to
            # kill. Two lines is a cheap place to keep it. codex reaches it by a
            # second route: its reply lands in a file, so an unreadable one is empty
            # here with stdout non-empty and the run_cli invariant untouched.
            if not raw:
                return ReviewerRun(skip=f"{label}: produced no output",
                                   duration_ms=elapsed(), usage=usage)
            return ReviewerRun([_raw_finding(cmd_name, raw)], None, elapsed(),
                               unstructured=True, usage=usage)
        return ReviewerRun(parsed[0], None, elapsed(), parsed[1], usage=usage_of())


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

def cluster_findings(llm_findings: list[Finding]) -> list[list[Finding]]:
    """Cluster findings that are plainly the same observation, as a HINT for the
    judge — never as the decision about what is a duplicate.

    Same file, and lines within ``CLUSTER_WINDOW`` of a neighbour already in the
    cluster. Two details matter, because the previous version got both wrong:

    * It is a real window over sorted lines, not ``line // 10``. A fixed grid is
      not a distance: lines 39 and 41 (two apart) landed in different buckets
      while 40 and 49 (nine apart) shared one, so whether two findings merged
      depended on where they fell relative to arbitrary multiples of ten.
    * It keys on the full path, not ``Path(f.file).name``. Same-named files in
      different directories (``api/tests/test_x.py`` and ``web/tests/test_x.py``)
      are not the same file, and merging them is the opposite error.

    What no line arithmetic can catch is the case that actually recurs: two
    reviewers describing ONE defect and citing lines 100 and 41 for it. That is
    a semantic judgement, which is why this only pre-clusters and the judge
    decides — see :func:`adjudicate`.

    Findings with no line at all cluster per file: it is the most that can be
    said about them positionally, and the judge sees them individually anyway.
    """
    by_file: dict[str, list[Finding]] = {}
    for f in llm_findings:
        by_file.setdefault(f.file, []).append(f)
    out: list[list[Finding]] = []
    for findings in by_file.values():
        # Sort is stable, so reviewers keep their arrival order within a line.
        ordered = sorted(findings, key=lambda f: (f.line is not None, f.line or 0))
        cur: list[Finding] = []
        last: int | None = None
        for f in ordered:
            if cur and (f.line is None) == (last is None) and (
                    last is None or f.line - last <= CLUSTER_WINDOW):
                cur.append(f)
            else:
                if cur:
                    out.append(cur)
                cur = [f]
            last = f.line
        if cur:
            out.append(cur)
    out.sort(key=lambda grp: min(f.severity for f in grp))
    return out


def _account(f: Finding) -> str:
    """One reviewer's account of a finding, joined for READING: title — detail.

    A presentation field, and only that. The structured pair travels beside it
    (``title``/``detail`` per report in :meth:`Canonical.as_dict`), because a
    consumer cannot split this string back apart — an em dash is a punctuation
    mark reviewers use — and the panel promises the account is kept, not that it
    is recoverable from a rendering of it. This is the text that used to be
    discarded when a positional merge chose a representative, taking with it the
    observations only one reviewer made."""
    return " — ".join(x for x in (f.title, f.detail) if x)


def _fold_reports(reports: list[Finding]) -> list[dict]:
    """The accounts as they are SERIALISED: one entry per reviewer.

    A judge may merge two findings from the same reviewer — that is the panel's
    own motivating example (one defect, two line numbers) — and the board stores
    accounts under a ``(finding, reviewer)`` uniqueness constraint, keeping the
    first and dropping the rest. So a reviewer's several accounts are joined
    here, where nothing is lost, rather than at ingest, where the second one
    would vanish. Its severity is the worst it gave and its line the first."""
    order: dict[str, list[Finding]] = {}
    for f in reports:
        order.setdefault(f.reviewer, []).append(f)
    out = []
    for reviewer, group in order.items():
        head = group[0]
        bodies = [head.detail] + [_account(f) for f in group[1:]]
        out.append({
            "reviewer": reviewer,
            "severity": min(f.severity for f in group),
            "line": next((f.line for f in group if f.line is not None), None),
            "title": head.title,
            "detail": "\n\n".join(b for b in bodies if b),
            "account": "\n\n".join(_account(f) for f in group),
            # THIS reviewer's own re-review declaration, which is the grain the
            # board scores it at: a member that called the structural fix and one
            # that missed it are the same row otherwise.
            "needs_rereview": any(f.needs_rereview for f in group),
        })
    return out


_NOT_WORD = re.compile(r"[^a-z0-9]+")


def _norm_title(title: str) -> str:
    """A title reduced to the words in it, which is what the defect key hashes and
    what the round diff compares. Must match ``app/api/reviews.py::_derive_key``
    and migration 0012's SQL, character for character."""
    return _NOT_WORD.sub(" ", (title or "").lower()).strip()


def _key_from_title(file: str | None, title: str) -> str:
    """The defect key for a title already chosen — the recipe itself, shared by
    :func:`_defect_key` and the round baselines, which read a title back out of a
    payload rather than off a Finding."""
    return hashlib.md5(f"{file or ''}|{_norm_title(title)}".encode(),
                       usedforsecurity=False).hexdigest()[:16]


def _defect_title(reports: list[Finding]) -> str:
    """The reporters' own words that identify the defect: the lexicographically
    first of their titles, so it does not move with report ordering, with which
    reviewer the judge picked as representative, or with a severity re-call.

    A finding no reporter titled keys on ``"(untitled)"`` rather than on the
    empty string, because that is the value the BOARD would arrive at: its
    ingest defaults a missing title to that stand-in *before* deriving a key
    (``_prepare``), and migration 0012's SQL keys off the stored column, which
    holds the same. The empty string is the intuitive choice and the wrong one —
    it produces a key no other implementation can reach, so an untitled finding
    would start a fresh chain every run."""
    titles = sorted(f.title.strip() for f in reports if f.title.strip())
    return titles[0] if titles else "(untitled)"


def _defect_key(file: str, reports: list[Finding]) -> str:
    """A stable identity for the DEFECT, sent with the finding.

    The board derives one when a caller sends none — file plus a normalised
    title — and the title it would use is the judge's freshly-worded synthesis,
    which is re-written on every run. Deriving it here from reviewer-authored
    text instead is what lets a re-review of the same PR join the same chain
    ("was this actually fixed?"), rather than starting a new one because the
    judge chose different words for the same bug.

    The title used is the lexicographically first of the reporters' own titles
    (see :func:`_defect_title`). Best-effort by nature — a reviewer that re-words
    its own title still breaks the chain, which is what
    :meth:`Baseline.raised_before` exists to absorb — but the reviewers' words are
    the most stable text a run produces.

    The hash MUST match ``app/api/reviews.py::_derive_key`` (and migration
    0012's SQL): a run that sends this key and an older run that let the board
    derive one only join if the two agree. "Agree" means against the board's
    whole path, not against that one function — its ingest defaults an untitled
    finding to ``"(untitled)"`` before deriving, so comparing with a raw
    ``_derive_key(file, "")`` measures a call the board never makes and
    "fixing" the mismatch is how the two silently diverge."""
    return _key_from_title(file, _defect_title(reports))


def _finding_id(pr: int, n: int) -> str:
    """``1609-F03`` — this finding, in this run. Run-LOCAL by construction: the
    numbering follows output position, so the same defect gets a different number
    on any rerun whose ordering, grouping or dismissals differ. It exists to
    resolve `related` within one payload, which is why the defect's own identity
    is a separate field (see :func:`_defect_key`)."""
    return f"{pr}-F{n:02d}"


@dataclass
class Canonical:
    """One real issue, as the judge settled it — the panel's only finding record.

    Merging is ADDITIVE: ``synthesis`` is the judge's new merged statement and
    ``reported_by`` carries every reviewer's original report beside it — its own
    title and detail as fields, not welded into one string, so a consumer gets
    back what the reviewer wrote rather than a rendering of it.
    Nothing a reviewer wrote is dropped to make a merge, which is what a
    representative-and-discard dedup did and why tightening its key would have
    made the loss worse rather than better.
    """

    id: str
    severity: str
    file: str
    line: int | None
    #: The one-line statement of the issue: the judge's merged one where it
    #: merged, else the reporting reviewer's own title. A line, never a body —
    #: the board stores it as the finding's `title` and derives from it, so a
    #: 4 KB unparsed-reply dump belongs in `detail`, not here.
    synthesis: str
    #: confirmed | dismissed | unjudged | sonar (the hard gate's own issues,
    #: which never reach the judge)
    verdict: str
    #: The body behind the synthesis. The judge writes one merged sentence and no
    #: body, so a merged record takes the worst report's — every reporter's own
    #: text rides along in `reported_by` either way.
    detail: str = ""
    reported_by: list[Finding] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    rationale: str = ""

    @property
    def reviewers(self) -> list[str]:
        """Who reported it, in arrival order. Attribution is a FIELD here, not an
        inference from a merge that already threw the evidence away."""
        return list(dict.fromkeys(f.reviewer for f in self.reported_by))

    @property
    def key(self) -> str:
        """This defect's identity across runs — see :func:`_defect_key`."""
        return _defect_key(self.file, self.reported_by)

    @property
    def rereview_by(self) -> list[str]:
        """Which members declared that fixing this needs the RESULT read again.

        Derived, not stored: every reporter's own :class:`Finding` is right here,
        so the attribution is simply read off it. The merge used to reconstruct
        this onto a representative finding — carefully, because the
        representative was one of the group's own members and setting its flag
        first would have credited its reviewer with somebody else's
        declaration. There is nothing to reconstruct now, and nothing to get
        wrong."""
        return sorted({f.reviewer for f in self.reported_by if f.needs_rereview})

    @property
    def needs_rereview(self) -> bool:
        """Did ANY reporter say so. One reviewer seeing that the fix will be
        structural is the observation; the others not saying so is not a
        contradiction of it."""
        return any(f.needs_rereview for f in self.reported_by)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "key": self.key,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "synthesis": self.synthesis,
            "detail": self.detail,
            "verdict": self.verdict,
            "reported_by": _fold_reports(self.reported_by),
            "reviewers": self.reviewers,
            "needs_rereview": self.needs_rereview,
            "rereview_by": self.rereview_by,
            "related": self.related,
            "rationale": self.rationale,
        }


def _unmerged(f: Finding, pr: int, n: int, verdict: str, rationale: str = "") -> Canonical:
    """A single reviewer's finding as a canonical record, no judge involved.

    Its title and detail stay in their own fields rather than being joined into
    the synthesis: the board stores the synthesis as the finding's title and
    keys off it, so joining them would put a whole detail body — up to
    RAW_DETAIL_CHARS of it, for an unparsed reply — into a title column and into
    the defect key."""
    return Canonical(id=_finding_id(pr, n), severity=f.severity, file=f.file,
                     line=f.line, synthesis=f.title, verdict=verdict,
                     detail=f.detail, reported_by=[f], rationale=rationale)


def _judge_listing(clusters: list[list[Finding]],
                   budget: int = MAX_LISTING_CHARS) -> tuple[str, list[Finding]]:
    """The findings as the judge sees them: one numbered line per REVIEWER
    account, with the pre-clustering offered as a hint underneath.

    Individually, because the judge cannot merge what it was shown already
    merged — the previous listing gave it one line per positional bucket, so the
    duplicates it *did* spot (its own output said "duplicate of [12]") were ones
    it had no verb to act on. Returns (listing, flat) where `flat[i]` is the
    finding the judge knows as `[i]`.

    Budgeted, because the whole prompt is ONE argv entry and Linux caps that at
    128 KiB: a panel of four reviewers each allowed RAW_DETAIL_CHARS would
    otherwise fail the review outright with E2BIG. Long accounts are cut first,
    then whole lines — and `flat` still holds every finding, numbered as the
    judge sees it, so an omitted report is simply never claimed and survives as
    unjudged rather than disappearing."""
    flat: list[Finding] = []
    groups: list[range] = []
    for grp in clusters:
        start = len(flat)
        flat.extend(grp)
        if len(grp) > 1:
            groups.append(range(start, len(flat)))

    lines: list[str] = []
    used = 0
    for i, f in enumerate(flat):
        said = _account(f)
        if len(said) > LISTING_ACCOUNT_CHARS:
            said = said[:LISTING_ACCOUNT_CHARS] + " …[account truncated]"
        line = (f"[{i}] {f.severity} {f.file}:{f.line or '?'} "
                f"(reported by {f.reviewer}) — {said}")
        if used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
    shown = len(lines)

    hints = [", ".join(f"[{i}]" for i in rng if i < shown) for rng in groups]
    hints = [h for h in hints if "], [" in h]
    if hints:
        lines.append("\nSame file and adjacent lines (a hint, not a ruling — merge only "
                     "if they are genuinely the same defect): " + "; ".join(hints))
    if shown < len(flat):
        lines.append(f"\n({len(flat) - shown} further report(s) omitted — the listing "
                     f"hit its {budget:,}-character budget. They are KEPT as unjudged "
                     "findings; rule only on what is above.)")
    return "\n".join(lines), flat


def _member_ids(raw) -> list[int]:
    """The report ids a verdict merges, as the ints they plainly are.

    A digit string and an integral float are both taken at face value: an LLM
    quoting `"members": ["0", "1"]`, or a JSON `2.0` that Python parses as a
    float, has told us exactly which report it meant, and dropping either would
    silently un-merge the finding. A non-integral float is not a report id, so
    it is dropped like any other junk."""
    if not isinstance(raw, list):
        return []
    out = []
    for m in raw:
        if isinstance(m, bool):
            continue
        if isinstance(m, int):
            out.append(m)
        elif isinstance(m, float) and m.is_integer():
            out.append(int(m))
        elif isinstance(m, str) and m.strip().isdigit():
            out.append(int(m.strip()))
    return out


def _ruling(raw) -> str:
    """The judge's verdict on one issue, from its ``real`` flag.

    Only ``real: false`` dismisses. The flag used to be read for truthiness, so
    `0`, `""` and `[]` — the shapes a malformed reply takes — silently dismissed
    findings, which is the one thing this module promises never to do. An absent
    flag still confirms (the judge listed the issue; it merely omitted the
    field); anything else is a ruling we cannot read, and an unreadable ruling is
    no ruling."""
    if raw is False:
        return "dismissed"
    if raw is True or raw is None:
        return "confirmed"
    return "unjudged"


def _parse_verdicts(parsed: list, flat: list[Finding], pr: int) -> list[Canonical]:
    """Turn the judge's reply into canonical findings.

    Defensive in one direction only: a malformed reply must never SUPPRESS a
    finding. Records naming no valid account are dropped (they attribute to
    nobody and would credit a reviewer that said nothing); an account claimed
    twice stays with the first record that claimed it, since two canonical
    findings sharing one account would double-count it in every per-reviewer
    statistic; and anything the judge never mentioned survives as its own
    unjudged record.

    A dropped verdict is SAID (on stderr), never merely dropped: a judge that
    answers with `"members": ["F01"]` — its own issue labels where report numbers
    belong — loses every merge it made, and without a word about it the run reads
    exactly like one where the judge found no duplicates.
    """
    out: list[Canonical] = []
    claimed: set[int] = set()
    links: list[tuple[Canonical, str | None, list]] = []   # (record, judge's id, its `related`)
    dropped: list[str] = []
    for v in parsed:
        # The same rule the reviewers' path applies to a findings entry: the
        # prompt's own example verdict, handed back whole, rules on nobody's
        # authority — it would claim reports 0 and 3, synthesise "the merged
        # statement of the issue" and mark them real. Ranking dropped it and
        # parsing kept it, and the two must not disagree about what a verdict is.
        if not _is_answer(v, "verdicts"):
            continue
        # dict.fromkeys: one verdict listing the same report twice must not
        # credit its reviewer twice.
        members = list(dict.fromkeys(
            i for i in _member_ids(v.get("members"))
            if 0 <= i < len(flat) and i not in claimed))
        if not members:
            if v.get("members"):
                dropped.append(f"{v.get('id') or '?'}: members={v.get('members')!r}")
            continue
        claimed.update(members)
        accounts = [flat[i] for i in members]
        rep = min(accounts, key=lambda f: f.severity)      # P1 < P2 < P3 lexically
        # The judge writes a one-line synthesis and no body, so the body is the
        # worst report's own. Falling back to the whole joined account instead
        # would put a detail — up to RAW_DETAIL_CHARS of it — in the synthesis,
        # which the board stores as the title.
        synthesis = str(v.get("synthesis") or v.get("title") or "").strip()
        c = Canonical(
            id=_finding_id(pr, len(out) + 1),
            severity=_severity(v.get("severity"), rep.severity),
            file=str(v.get("file") or rep.file),
            line=v.get("line") if isinstance(v.get("line"), int) else rep.line,
            synthesis=synthesis or rep.title,
            verdict=_ruling(v.get("real")),
            detail=rep.detail,
            reported_by=accounts,
            rationale=str(v.get("reason") or v.get("rationale") or "").strip(),
        )
        out.append(c)
        rel = v.get("related")
        links.append((c, str(v["id"]) if v.get("id") is not None else None,
                      rel if isinstance(rel, list) else []))

    if dropped:
        print(f"panel: judge verdict(s) named no valid report id and were dropped "
              f"(their findings are kept, unjudged): {'; '.join(dropped)}",
              file=sys.stderr)

    # `related` is resolved from the judge's own ids to ours, and only within
    # this reply: a link to something that is not here names nothing.
    #
    # An id the judge used TWICE resolves to nothing rather than to whichever
    # record happened to be built last: a link is a claim about which finding,
    # and a wrong one sends the fixer to unrelated code. Ids are compared as
    # strings, so `1` and `"1"` are one identifier — which is the point, since
    # the judge that writes both means one issue; a genuine clash is caught here
    # as the duplicate it looks like.
    seen: dict[str, str | None] = {}
    for c, jid, _ in links:
        if jid is not None:
            seen[jid] = None if jid in seen else c.id
    ambiguous = sorted(k for k, v in seen.items() if v is None)
    if ambiguous:
        print(f"panel: judge reused issue id(s) {', '.join(ambiguous)} — `related` "
              "links naming them left unresolved", file=sys.stderr)
    by_judge_id = {k: v for k, v in seen.items() if v}
    for c, _, rel in links:
        c.related = sorted({by_judge_id[str(r)] for r in rel
                            if str(r) in by_judge_id} - {c.id})

    # Never suppress: a finding the judge skipped is kept, unruled.
    for i, f in enumerate(flat):
        if i not in claimed:
            out.append(_unmerged(f, pr, len(out) + 1, "unjudged", "unjudged"))
    return out


def adjudicate(clusters: list[list[Finding]], diff: str, model: str, pr: int,
               budget: int | None = DEFAULT_DIFF_BUDGET,
               coverage: dict[str, list[str]] | None = None,
               cwd: str | None = None
               ) -> tuple[list[Canonical], str | None, str]:
    """The 'master' rules on every finding, merges the duplicates it finds, AND
    rules on the coverage the reviewers declared about themselves.

    Returns (canonical findings, skip_reason, coverage_note). skip_reason is None
    when the judge ran (even if it dismissed nothing); otherwise it explains WHY
    it could not rule — CLI absent, timeout, crash, a zero exit that produced no
    output, or output with no JSON verdict in it — so the caller can surface that
    rather than a bare 'unavailable'. The judge inherits run_cli's empty-output
    guard for free: a judge that printed nothing now reports "produced no output"
    (with its own stderr quoted) instead of blaming the shape of a reply it never
    made.

    The coverage ruling is one extra key in the object the judge already returns,
    so it costs no additional model call — and its own reply may still be the
    bare verdict array an earlier judge returned, in which case there is simply
    no coverage note.

    Declarations with no findings still run the judge: that is the round where
    "clean versus could-not-tell" most needs adjudicating — two members saying
    they could not read the migration while a third reports clean is a split, and
    a finding count of zero says nothing about it.

    Merging lives here because this is the only step that reads every account and
    can write a new one. Upstream, dedup could only ever pick a survivor and
    discard the rest; the judge can say what the reviewers jointly found, and the
    originals ride along untouched in ``reported_by``.

    A real bug from a single reviewer is confirmed; only genuine false positives
    are dismissed (style and polish are kept). When the judge can't rule, every
    finding is returned unmerged and unjudged — nothing is silently suppressed.
    Neither findings NOR declarations -> ([], None, ""): nothing to rule on.
    """
    declared = {k: v for k, v in (coverage or {}).items() if v}
    if not any(clusters) and not declared:
        return [], None, ""
    # The listing and the diff no longer share one ceiling. They did while the
    # prompt travelled in argv and the two genuinely competed for the kernel's
    # 128 KiB; on stdin they compete only for the model's context, and the diff
    # has no cap by default. Subtracting an uncapped diff from a fixed ceiling
    # drove the listing straight to its 4,000-char floor — starving the one
    # component that is unbounded in the panel's OWN output, on exactly the runs
    # (many findings, big diff) where the judge most needs to see all of them.
    #
    # So each gets its own budget: the diff whatever was configured for it, the
    # listing MAX_LISTING_CHARS. A capped diff leaves the listing more room than
    # it asks for either way, so there is nothing left for the old arithmetic.
    diff_text = diff if budget is None else diff[:budget]
    stated = "\n".join(f"- {name}: could not assess {'; '.join(items)}"
                       for name, items in sorted(declared.items())) \
        or "- (no reviewer declared a gap in its coverage)"
    listing, flat = _judge_listing(clusters, MAX_LISTING_CHARS)
    listing = listing or ("- (no findings this round — there is nothing to adjudicate "
                          "but the coverage below; return an empty `verdicts` array)")

    def unruled(reason: str, note: str = "") -> tuple[list[Canonical], str, str]:
        return [_unmerged(f, pr, i + 1, "unjudged", "unjudged")
                for i, f in enumerate(flat)], reason, note

    if not shutil.which("claude"):
        return unruled("judge: claude CLI absent")
    # On stdin, like the reviewers, and for a sharper reason: the judge's prompt
    # is the only one with a component no budget could cover. The findings
    # listing grows with the panel's output, so a legal judge_max_diff_chars
    # plus a long panel used to cross the argv limit on its own — and a judge
    # that dies takes every finding through UNADJUDICATED, which reads like a
    # triaged review rather than like a failure.
    prompt = JUDGE_PROMPT.format(findings=listing, coverage=stated, diff=diff_text)
    args = ["claude", "-p"] + (["--model", model] if model else [])
    out, err = run_cli(args, "judge", stdin_text=prompt, cwd=cwd)
    if err:
        return unruled(err)
    parsed = extract_json_value(out, "verdicts")
    if parsed is None:
        # The same one-shot reparse retry `review_llm` gets, and the judge needs it
        # more. Agreement strictly ENLARGES the set of replies that resolve to
        # None — an envelope plus a restatement of it, an envelope plus a
        # self-authored illustration, any two candidates that read differently —
        # so a failure that was rare under ranking now fires on ordinary model
        # prose. The asymmetry was the expensive part: a reviewer that cannot be
        # read costs one seat, a judge that cannot be read takes EVERY finding
        # through `unjudged` and adds the "round was not adjudicated" veto. One
        # more turn keeps the pessimistic rule without paying for it with the
        # whole adjudication.
        out2, err2 = run_cli(args, "judge", attempts=1, stdin_text=prompt, cwd=cwd)
        if not err2:
            parsed = extract_json_value(out2, "verdicts")
    note = ""
    reply = parsed if isinstance(parsed, dict) else None
    if reply is not None:
        # `"coverage_note": "..."` is what JUDGE_PROMPT asks with, not an answer to
        # it. Printed in the PR comment it reads as a coverage ruling nobody made.
        # (A reply that is nothing BUT the schema never gets here — those are not
        # candidates at all — but a real ruling can still carry the stand-in note.)
        note = str(reply.get("coverage_note") or "").strip()
        note = "" if note in SCHEMA_DECLARATIONS["verdicts"] else note
        parsed = reply.get("verdicts")
    if not isinstance(parsed, list):
        # With nothing to adjudicate, a reply that carries the coverage answer is
        # a complete answer — not a judge that failed to rule.
        #
        # "Carries the coverage answer" is checked, not assumed from there being
        # no findings. Prose, a crash-truncated reply, an object with neither key
        # — all of them used to land here as skip_reason=None, so `coverage_veto`
        # added no "the round was not adjudicated" entry and the round recorded a
        # CONFIDENT clean verdict on a judge that produced nothing. That is the
        # inversion of the guarantee this release exists for, and it fires on
        # precisely the round where the coverage split most needed adjudicating.
        answered = reply is not None and (bool(note) or "verdicts" in reply)
        if not flat and answered:
            return [], None, note
        return unruled("judge: no JSON verdict in output (unparseable)", note)
    return _parse_verdicts(parsed, flat, pr), None, note


# ----------------------------------------------------------------------------- rounds

def _same_file(a: str, b: str) -> bool:
    """Do two path spellings name the same file? Equal, or one a path-suffix of
    the other (``reviews.py`` vs ``app/api/reviews.py``) — but never two distinct
    paths that merely end in the same basename.

    Reviewers spell paths differently, and round 1 may have recorded a defect
    under the short spelling and round 2 under the long one. Clustering does NOT
    use this — it keys on the full path, deliberately (see
    :func:`cluster_findings`) — and the two rules agree where it matters: neither
    will ever treat ``api/tests/test_x.py`` and ``web/tests/test_x.py`` as one
    file."""
    if a == b:
        return True
    return a.endswith("/" + b) or b.endswith("/" + a)


#: How alike two titles for the same file must read before the round diff treats
#: them as one defect reworded. Deliberately high: this only ever has to absorb
#: "unused import" vs "import is unused", never two genuinely different defects.
REWORD_RATIO = 0.85

#: Words that never distinguish one defect from another, so a title that has one
#: and a title that does not are still candidates for the same defect. Content
#: words are what this list must leave alone: "not" and "never" are deliberately
#: absent, since "is closed" and "is never closed" are two different defects.
_TITLE_NOISE = frozenset(
    "a an the of in on at to by is it its as be or and for with this that".split())


def _stem(word: str) -> str:
    """A word reduced past the endings a rewrite changes without changing the
    subject — "import"/"imports", "query"/"queries". Crude on purpose: it only has
    to make two spellings of one noun agree, and over-stemming two DIFFERENT words
    into one is the failure that costs a finding, so nothing here shortens a word
    to fewer than three characters.

    Plain ``-s`` is stripped before anything else, so "files" reduces to "file"
    and agrees with its own singular. An ``-es`` rule ahead of it took two
    characters off every word merely ENDING in es — "files"/"file",
    "nodes"/"node", "values"/"value" — which is the noun class review titles are
    made of, so singular and plural never matched and the reword fallback this
    exists for never fired. The cost is the other direction, "boxes"/"box", which
    is rarer in a title and costs a false "new" rather than a lost finding."""
    for suffix, repl in (("ies", "y"), ("s", "")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[:len(word) - len(suffix)] + repl
    return word


def _same_words(a: str, b: str) -> bool:
    """Do two normalised titles differ only in how they are WORDED?

    A character-similarity ratio alone cannot answer that. Findings share long
    boilerplate and differ in one short noun — "the N+1 query in the user loop"
    against "…in the order loop" is 0.93 alike and two different defects — and
    calling those one repeat drops the second from ``new_findings`` without ever
    briefing the fixer on it. A false "new" costs a wasted round; a false "already
    raised" costs a defect, so the ambiguous case goes to "new".

    So the two must carry the same content words, up to word order and a plural:
    a word one title has and the other does not names something the other does not
    talk about, and that is a different defect however alike the two strings read.
    """
    def words(text: str) -> set[str]:
        return {_stem(w) for w in text.split() if w not in _TITLE_NOISE}

    return words(a) == words(b)


@dataclass
class Baseline:
    """What earlier rounds of THIS PR already raised."""

    keys: set[str] = field(default_factory=set)
    #: normalised title -> every file spelling it was raised against, for the
    #: reworded case. A set rather than one file: the same words about two files
    #: are two defects, and keeping only the last-seen one loses the other.
    titles: dict[str, set[str]] = field(default_factory=dict)
    #: Which earlier rounds are actually represented here, not the highest round
    #: label among them: baselines for rounds 1 and 3 are two earlier rounds, and
    #: calling that three invents one nobody ran. ``len(rounds)`` is what prints.
    rounds: set[int] = field(default_factory=set)
    #: The panel -> fix -> panel CYCLE these baselines came from, inherited from
    #: the earliest one so every round of a cycle carries the same id. None when
    #: there was no usable baseline, in which case the run mints its own.
    cycle: str | None = None
    problems: list[str] = field(default_factory=list)

    def raised_before(self, finding: Canonical) -> bool:
        """Did an earlier round raise this defect — under this key, or under a
        near-identical title in the same file?

        The key is the finding's own (:func:`_defect_key`), so this compares the
        identity the payload carries rather than re-deriving one. The title
        fallback is for the reviewer that re-words its own report between rounds:
        the key is built from the reporters' words, so any rewording would
        otherwise land a persistent defect in `new_findings` and report the fix
        as having broken something. "The same file" is suffix-aware
        (:func:`_same_file`), since a round where only the short-path reviewer
        raised the defect hashes to a different key too.

        The fallback is deliberately hard to trigger. Its two failures are not
        symmetric: a wrong "new" buys a round nobody needed, while a wrong
        "already raised" deletes a finding from the fixer's brief and can end the
        cycle on a defect nobody was told about. So a high character ratio is only
        the cheap pre-filter, and :func:`_same_words` — the two titles carrying the
        same content words, up to word order and a plural — is what decides.
        """
        if finding.key in self.keys:
            return True
        norm = _norm_title(_defect_title(finding.reported_by))
        if not norm:
            return False
        return any(
            _same_file(finding.file or "", was_file)
            and difflib.SequenceMatcher(None, norm, was).ratio() >= REWORD_RATIO
            and _same_words(norm, was)
            for was, was_files in self.titles.items()
            for was_file in was_files
        )


def _baseline_title(f: dict) -> str:
    """The title an earlier round's serialised finding is identified by: the same
    lexicographically-first reporter title :func:`_defect_key` hashed, read back
    out of the payload. Falls back to the judge's synthesis for a record that
    carries no accounts, which is the most that can be said about it."""
    titles = sorted(t for t in (str(r.get("title", "")).strip()
                                for r in f.get("reported_by") or []
                                if isinstance(r, dict)) if t)
    return titles[0] if titles else str(f.get("synthesis") or "")


def load_baseline(paths: list[str], expect: dict | None = None) -> Baseline:
    """Every finding earlier rounds of this PR already raised, from their
    ``--json-file`` payloads.

    Keyed on what was RAISED, not on what was confirmed: a finding the judge
    dismissed in round 1 and a reviewer raises again in round 2 is not new
    information, and counting it as new is how a loop fails to converge.

    The key is READ from the payload rather than re-derived: the panel has sent
    one with every finding since the merge moved into the judge, and it is the
    same identity the board chains runs on. A payload that carries none (a
    hand-written baseline, a record from before the field) falls back to the same
    recipe over the reporters' titles.

    A baseline that cannot be read is reported rather than swallowed. Its absence
    makes every finding look new, which reads as "the fix broke things" — the
    exact opposite of the truth — so the caller marks the round's verdict
    unearned instead of quietly believing it. Every defect in a payload is
    downgraded to a ``problems`` entry for that reason — including a malformed
    ``round``, which used to raise out of ``run()`` and kill a review after the
    diff had been fetched and every reviewer CLI had been paid for.

    ``expect`` (``github``/``pr``/``round``) is checked against what the
    payload says it is. A baseline from another PR is not a thinner baseline, it
    is a wrong one: its keys would make real findings read as repeated and stop
    the loop early, so a mismatched payload is REPORTED and its keys dropped. An
    identity field the CALLER knows must be present as well as equal: a
    hand-edited or truncated payload that omits it is a payload nobody can
    attribute, and accepting it suppresses this run's findings on the word of a
    file that never said whose it was. A field the caller does not know (``None``
    in ``expect``) is not checked at all — testing key *presence* instead made
    ``{"repo": None}`` reject every baseline ever written, which silently
    no-opped the whole round diff for any caller that did not resolve its repo
    name. The same reported-and-excluded rule covers a payload whose round is not
    earlier than this one's, since a current or future round's keys make
    genuinely new findings read as repeated.

    All usable baselines must belong to ONE cycle, and the earliest of them names
    it. Two concurrent cycles on a PR have unrelated keys and titles; merging them
    into one history classifies findings only the other cycle raised as repeats,
    which can suppress a fix round — the exact confusion the ``cycle`` id was
    minted to prevent, so a payload from a different cycle is reported and
    excluded like any other wrong baseline.

    A round past the first with NO baseline at all is itself a problem: every
    finding then reads as new, ``prior_rounds`` prints zero, and the round would
    otherwise be free to record a *confident* verdict about a comparison it never
    made."""
    b = Baseline()
    want = dict(expect or {})
    if "round" in want:
        # Normalised once, and never raised out of: this function's rule is that a
        # bad input costs a problems entry, not a review that every reviewer CLI
        # has already been paid for.
        try:
            want["round"] = int(want["round"])
        except (TypeError, ValueError):
            del want["round"]
    if not paths and want.get("round", 1) > 1:
        b.problems.append(
            f"round {want['round']} ran with no --baseline — nothing to compare against, "
            "so every finding here reads as one no earlier round raised")
    #: (round, cycle, path, payload) of each baseline that passed identity and
    #: ordering. Collected before anything is merged, because which cycle the run
    #: belongs to is a property of the SET — the earliest round names it, and the
    #: rest are checked against that.
    usable: list[tuple[int, str, str, dict]] = []
    for path in paths:
        try:
            payload = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as e:
            b.problems.append(f"baseline {path} unreadable ({e.__class__.__name__})")
            continue
        if not isinstance(payload, dict):
            b.problems.append(f"baseline {path} is not a panel payload")
            continue
        # Only the fields whose expected value is KNOWN. `k in want` would check a
        # key the caller passed as None, and then reject every payload for not
        # matching a value nobody has.
        # `github` + `pr` and nothing else: those name the REVIEW, which is what
        # a baseline has to be from. `repo` is the local checkout's directory
        # name, so the same review run from a worktree and from the main
        # checkout ("quarterback-feat-issue-24" and "quarterback") disagrees on
        # it — and /panel-review-pr's own parallel mode gives every PR a
        # throwaway worktree, so that is the normal case rather than a corner.
        # Checking it would reject a baseline for having been written somewhere
        # else, which is not a property of the review at all.
        checked = [k for k in ("github", "pr") if want.get(k) is not None]
        missing = [k for k in checked if payload.get(k) is None]
        wrong = [f"{k}={payload.get(k)!r} (this run: {want[k]!r})"
                 for k in checked if payload.get(k) is not None and payload.get(k) != want[k]]
        if missing:
            b.problems.append(f"baseline {path} does not say which review it is from "
                              f"(no {', '.join(missing)}) — its findings were NOT counted "
                              "as earlier rounds")
            continue
        if wrong:
            b.problems.append(f"baseline {path} is another review's — " + ", ".join(wrong)
                              + " — its findings were NOT counted as earlier rounds")
            continue
        try:
            was = int(payload.get("round") or 1)
        except (TypeError, ValueError):
            b.problems.append(f"baseline {path} has a malformed round "
                              f"({payload.get('round')!r}) — counted as round 1")
            was = 1
        if "round" in want and was >= want["round"]:
            b.problems.append(f"baseline {path} is round {was}, which is not earlier than "
                              f"this run's round {want['round']} — pass the round this "
                              "run actually is — its findings were NOT counted as earlier "
                              "rounds")
            continue
        # A cycle id the caller minted, or — for a round 1 that predates the
        # field — the run_key it recorded itself under, which is unique to that
        # process and is exactly as stable. "" for a payload that says neither,
        # which conflicts with nothing and inherits whatever the set decides.
        usable.append((was, str(payload.get("cycle") or payload.get("run_key") or ""),
                       path, payload))

    # The cycle named by the EARLIEST round that names one, keyed on the round
    # alone rather than by min() over the whole tuple: min() fell through to
    # comparing opaque hex ids whenever two baselines shared a round, so the winner
    # was lexicographic — neither earliest nor first, contradicting the rule
    # written beside it. Keyed this way, min() keeps the first at that round.
    named = [e for e in usable if e[1]]
    b.cycle = min(named, key=lambda e: e[0])[1] if named else None
    distinct_rounds = {e[0] for e in usable}
    if len(distinct_rounds) != len(usable):
        # Two payloads for one round is not fatal — their keys are still findings
        # an earlier round raised — but `rounds` then under-counts, and the cycle
        # was inherited from one of two equals, so the ambiguity is stated rather
        # than resolved in silence.
        b.problems.append(f"{len(usable)} baselines cover {len(distinct_rounds)} round(s) — "
                          "two payloads for one round, so which of them named the cycle "
                          "was arbitrary")
    for was, got, path, payload in usable:
        if got and b.cycle and got != b.cycle:
            b.problems.append(f"baseline {path} is from cycle {got}, not this run's "
                              f"{b.cycle} — a concurrent cycle's findings would read as "
                              "repeats here — its findings were NOT counted as earlier "
                              "rounds")
            continue
        b.rounds.add(was)
        for bucket in ("to_fix", "dismissed", "sonar_findings"):
            for f in payload.get(bucket) or []:
                if not isinstance(f, dict):
                    continue
                file, title = f.get("file"), _baseline_title(f)
                b.keys.add(str(f.get("key") or "") or _key_from_title(file, title))
                norm = _norm_title(title)
                if norm:
                    b.titles.setdefault(norm, set()).add(file or "")
    return b


def coverage_veto(reviewer_meta: dict[str, dict], judge_skip: str | None,
                  flagged: int, diff_chars: int) -> list[str]:
    """Reasons a quiet round is not evidence of a quiet PR.

    A counter cannot tell a genuinely dry round from a broken one — a reviewer
    that read half the diff, one that never ran, and one whose reply did not parse
    all look identical to "found nothing". These are the observations that
    distinguish them, and they exist to stop a failure being read as convergence.
    They do NOT drive the loop: a truncated reviewer is truncated again next round
    at the same budget, so treating that as a reason to go again is a loop with no
    exit. It is a reason to stop CLAIMING the PR is clean.

    The one absence that is not an observation about the round is a reviewer
    whose CLI this box does not carry — see below."""
    out = []
    for name, meta in sorted(reviewer_meta.items()):
        if not meta.get("ran"):
            skip = str(meta.get("skip") or "")
            # A seat whose CLI is not INSTALLED on this box is a fact about the
            # host, not about the round: it is absent every round, so vetoing on
            # it makes `confident` permanently unreachable on the headless
            # machines — which is where the unattended loops run and where the
            # signal has to mean something. A repo that lists a workstation-only
            # vendor would otherwise buy every one of its unattended runs a
            # standing veto and train the reader to discount all of them. The
            # skip is still REPORTED (result.skipped carries it, and the header
            # names who ran); what it is not is evidence a quiet round hid
            # something. Every other way of not running — a crash, a timeout, a
            # bad model pin, a CLI that produced nothing — is about THIS run and
            # still vetoes.
            #
            # Read off the recorded state, never off the skip TEXT: the message
            # is free-form, so `skip.endswith(CLI_ABSENT)` would let an installed
            # CLI whose stderr tail happens to read that way skip the veto, and
            # would silently restore the standing veto the first time this
            # branch's wording gained a suffix.
            if meta.get("absent"):
                continue
            out.append(f"{name} did not run ({skip or 'no reason recorded'})")
            continue
        if meta.get("truncated"):
            budget = meta.get("max_diff_chars") or 0
            out.append(f"{name} saw {budget:,} of {diff_chars:,} diff chars")
        if meta.get("unstructured"):
            out.append(f"{name} returned no structured reply — its coverage is unknown")
        for gap in meta.get("could_not_assess") or []:
            out.append(f"{name} could not assess: {gap}")
    # The floor under the absence exemption above. Exempting absent seats one by
    # one means a box carrying NONE of the reviewer CLIs produces an empty veto
    # list, and `confident` is `not veto` — a confident stop on a diff nobody
    # read, which is the strongest wrong signal this file can emit and lands
    # exactly on the unattended hosts the exemption was added for. At least one
    # reviewer has to have actually run.
    if not any(m.get("ran") for m in reviewer_meta.values()):
        out.append("no reviewer ran — nothing read this diff")
    if judge_skip:
        # Phrased for both halves of the judge's job: on a round with no findings
        # it is the coverage split that went unadjudicated, not the findings.
        out.append(f"the round was not adjudicated ({judge_skip})")
    if flagged:
        out.append(f"{flagged} finding(s) whose reporter said the FIX needs re-reading")
    return out


def round_stop(round_no: int, max_rounds: int, new_keys: list[str],
               outstanding: list[Canonical], veto: list[str],
               baseline_ok: bool = True, repeated: int = 0) -> dict:
    """Whether the panel/fix cycle should go again, and what decided it.

    ``outstanding`` is every finding the cycle still has to clear, which is wider
    than "confirmed" and deliberately so: it holds anything the judge did not
    dismiss (including the ``unjudged`` findings of a round whose judge crashed)
    plus Sonar's hard-gate issues, which nobody adjudicates at all. A P2 nobody
    ruled on is not a reason to STOP. The parameter used to be called
    ``confirmed``, and the word reached the PR comment: a reader reconciling
    "still confirmed after the fix" against a round with no judge was told
    something untrue about how the verdict was reached.

    The rule is mechanical on purpose. Asking reviewers to forecast "will another
    round be needed?" measures the wrong thing — a model that just wrote five
    findings is primed on problems and says yes, one that found nothing says no,
    and the vote only re-encodes a finding count already known. So the loop turns
    on what actually happened:

    1. findings this round that no earlier round raised -> go again;
    2. a P1/P2 still outstanding -> go again, whatever anyone declared (a blocker
       raised again is a blocker that was not fixed);
    3. ``repeated`` — a finding an earlier round already raised that is STILL
       outstanding, at any severity -> go again. The fixer was told about it and
       it is still there, and ``/panel-review-pr``'s bar is every finding fixed,
       not every P1/P2. This used to only cost the stop its confidence, which
       ended the cycle with a judge-confirmed defect present and nothing acting on
       the veto that said so;
    4. otherwise dry -> stop.

    The cap is what stops rule 3 running forever when two reviewers disagree
    about a P4 — the cycle ends either way, and a cap reached with work
    outstanding is recorded as such rather than as convergence."""
    blockers = [c for c in outstanding if c.severity in ("P1", "P2")]
    if new_keys:
        stop, reason = False, (f"{len(new_keys)} finding(s) no earlier round raised")
    elif blockers:
        stop, reason = False, f"{len(blockers)} P1/P2 still outstanding after the fix"
    elif repeated:
        stop, reason = False, (f"{repeated} finding(s) an earlier round already raised "
                               "are still outstanding")
    else:
        stop, reason = True, ("dry — nothing raised that an earlier round had not"
                              if round_no > 1 else "dry — no findings to fix")
    capped = False
    if not stop and round_no >= max_rounds:
        stop, capped = True, True
        reason = f"round cap ({max_rounds}) reached — {reason}, unreviewed"
    # Only on a STOP. The veto list is printed under "why this round's quiet is
    # not evidence of a quiet PR", and on a `go again` round the repeat IS the
    # reason — printing it there told a reader that a round which was not quiet
    # had untrustworthy quiet. `confident` is unaffected: it already requires
    # `stop`.
    if repeated and stop:
        veto = [*veto, f"{repeated} finding(s) an earlier round already raised are "
                       "still outstanding — the fix for them did not land"]
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

def _payload_defaults() -> dict:
    """Every key a run payload carries, valued as "this run never got that far".

    One shape on every non-error exit, because the skip-pattern path emits a
    payload too: a consumer reading `payload['judged']` or `payload['run_key']`
    should not have to know which exit produced it. It used to be a hand-written
    literal of nine keys against this one's two dozen, so the skipped PR — the
    case that payload exists FOR — was the one that raised KeyError."""
    return {
        "changed_lines": 0,
        "reviewed": False,
        "skip_reason": None,
        # Where a run sits in the panel -> fix -> panel cycle. Defaulted here too,
        # so the skipped PR answers `payload['round_stop']` with "no cycle ran"
        # rather than with a KeyError.
        "round": 1,
        "cycle": None,
        "prior_rounds": 0,
        "prior_findings": 0,
        "new_findings": 0,
        "new_finding_keys": [],
        "round_stop": None,
        "stop_reason": None,
        "coverage_note": None,
        "diff_truncated": False,
        "diff_chars": 0,
        "diff_budgets": {},
        "config_notes": [],
        "sonar_gate": "skipped",
        "ci_status": "unknown",
        "ci_failing": [],
        "judged": False,
        "judge_model": None,
        "judge_skip": None,
        "reviewers_ran": [],
        "reviewers": {},
        "reviewers_selected": [],
        "reviewers_override": None,
        "to_fix": [],
        "sonar_findings": [],
        "dismissed": [],
        "skipped": [],
    }


def _veto_gist(text: str, limit: int = 80) -> str:
    """The identifying head of a veto that is also a config note — enough to say
    WHICH note without repeating its full text on the PR comment. The problem
    strings put their consequence after an em-dash, so the head is the fact."""
    head = text.split(" — ", 1)[0].strip()
    return head if len(head) <= limit else head[:limit - 1] + "…"


def write_payload(json_file: str, payload: dict) -> str:
    """Write a run payload where ``--json-file`` asked for it.

    Returns "" on success (or when nothing was asked for), else a description of
    the failure for :func:`finish` to fail the run with. Shared by every non-error
    exit, because the file is the NEXT round's baseline and a caller told "the
    round did not happen unless the panel wrote that file" must get that answer
    from the skip-pattern exit too.

    Opened ``O_NOFOLLOW``, so a pre-planted symlink at the requested path
    (``/tmp/panel-34-r1.json`` -> ``~/.ssh/authorized_keys``) fails the write
    instead of following it — the hazard ``panel-review-pr.md`` §3 warns about,
    enforced here rather than left to an instruction the caller may never read."""
    if not json_file:
        return ""
    try:
        fd = os.open(json_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(payload, indent=2))
    except OSError as e:
        failed = f"{json_file} ({e.__class__.__name__})"
        print(f"panel: could not write {failed}", file=sys.stderr)
        return failed
    return ""


#: Exit code for "the review ran, but the requested --json-file was not written".
#: Deliberately not 2: argparse exits 2 on its own usage errors, and the caller is
#: told a non-zero exit means the round did not happen for cycle purposes — which
#: it cannot tell from a mistyped flag if the two share a code.
UNWRITTEN_PAYLOAD_EXIT = 3


def finish(write_failed: str, code: int = 0) -> int:
    """The exit code, failing the run when the requested ``--json-file`` was not
    written.

    Without that file round r+1 classifies every repeated finding as new, prints
    "N of N raised by no earlier round" and drives a fix pass over work already
    done. Warning and exiting 0 let the caller advance the cycle on a baseline
    that does not exist.

    Reported at the END of a run rather than at the write: the report, the board
    record and the PR comment are a review that has already been paid for, and
    throwing them away would push the caller towards re-running the panel — which
    the workflow forbids, because each run is an observation and re-rolling one
    corrupts the record."""
    if write_failed:
        print(f"\npanel: FAILED — the requested --json-file was not written: "
              f"{write_failed}. The review above is complete, but the next round "
              "has no baseline: fix the path and re-run the CYCLE from round 1 "
              "rather than treating this round as done.", file=sys.stderr)
        return UNWRITTEN_PAYLOAD_EXIT
    return code


def fit_comment(report: str, limit: int = COMMENT_CHARS) -> str:
    """The report, cut to fit a GitHub comment (65,536 chars, hard).

    The per-reviewer accounts are the part that grows without bound — one block
    per reporter per merged finding — so they go first and the verdicts survive;
    a report still over the limit is cut with a marker. The terminal copy is
    never trimmed, and neither are `--json` or the board record: this is about
    what `--post` can physically send, and a review that succeeded must not be
    lost to a comment one account too long.

    The round verdict is the one block a cut is taken AROUND rather than through:
    it sits at the foot of the report, it is what the caller of a cycle acts on,
    and a truncation from the end would drop precisely it.

    Reserved, not exempt. The verdict block carries one veto line per reviewer per
    declared gap, from free text a model wrote, so it is unbounded in principle —
    and reserving all of it clamped the SLICE rather than the RESULT, returning
    `cut + tail` over the limit and losing the whole comment to a hard API
    rejection. When the block alone will not fit it is cut from its own end, which
    keeps the mechanical verdict (its first line) and drops the vetoes."""
    if len(report) <= limit:
        return report
    note = ("\n\n_Per-reviewer accounts omitted — the full report exceeds GitHub's "
            "comment limit. They are intact in `--json` and on the board._")
    trimmed = "\n".join(ln for ln in report.splitlines() if not ln.startswith("  - _"))
    if len(trimmed) + len(note) <= limit:
        return trimmed + note
    cut = ("\n\n_…report truncated at GitHub's comment limit. The full run is in "
           "`--json` and on the board._")
    if limit <= len(cut):
        # No room for even the marker: the caller asked for a length no honest
        # report fits in, so give it the report's own first characters.
        return trimmed[:max(0, limit)]
    head, sep, tail = trimmed.partition("\n\n" + ROUNDS_HEADING)
    tail = sep + tail if sep else ""
    room = limit - len(cut)
    if len(tail) > room:
        tail = tail[:room - 1] + "…"
    return head[:room - len(tail)] + cut + tail


def run(repo_name: str | None, pr_number: int, post: bool, json_out: bool = False,
        reviewers: str | None = None, json_file: str = "", record: bool = True,
        round_no: int = 1, baseline: list[str] | None = None,
        max_rounds: int | None = None) -> int:
    # A cycle is something the CALLER drives, and only /panel-review-pr does:
    # naming a cap (or a round, or a baseline) is what says this run is part of
    # one. A review-only /panel run left to the default is a single pass, and
    # must not report itself as "round 1 of at most 2 — go again", promising a
    # re-review nothing will run.
    in_cycle = max_rounds is not None or round_no > 1 or bool(baseline)
    cap = DEFAULT_MAX_ROUNDS if max_rounds is None else max_rounds
    # Idempotency key for the board record, minted once per process so a retry of
    # the POST cannot double-count the run into the stats. A fresh panel run is a
    # genuinely new observation and gets a new key — re-reviewing a PR after a fix
    # loop is data, not a duplicate.
    run_key = uuid.uuid4().hex
    cfg = load_repo_cfg(repo_name)
    # The name RESOLVED from the checkout, never the argument. `--repo` is
    # optional — `panel.py --pr N` in a repo is the documented single-PR form —
    # and the unresolved None went straight into the payload as `"repo": null`.
    # A payload that does not say which review it is from cannot be a baseline:
    # round 2 discarded round 1 as unattributable, called every finding new, and
    # could never record a confident stop. The whole round diff no-opped for
    # anyone who did not pass a flag they were never told to pass.
    repo_name = cfg.get("name") or repo_name
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

    # Progress goes to stderr in --json mode, so stdout is the payload and only
    # the payload: it is a machine-readable artifact, and a consumer that has to
    # strip a two-line preamble before parsing is one preamble away from breaking.
    chatter = sys.stderr if json_out else sys.stdout

    # Title-pattern skip (merges/promotes/format-the-world — not worth LLM review)
    for pat in panel.get("skip_title_patterns", []):
        if re.search(pat, title, re.I):
            print(f"[{repo_name}#{pr_number}] '{title[:50]}' matches skip pattern "
                  f"/{pat}/ — not worth panel review. Skipping.", file=chatter)
            # A consumer gets a payload on every non-error exit, or "reviewed
            # and found nothing" and "never reviewed at all" arrive as the
            # same empty stdout — and the second one silently reads as a
            # clean PR. Same SHAPE as a reviewed run, too, so reading any
            # other key of it is not a KeyError. Not recorded on the board:
            # no review happened.
            #
            # It says WHICH round it is and which cycle that round belongs to,
            # because the caller is told to feed every round's --json-file
            # forward as the next round's --baseline. Left on the defaults it
            # serialised a skipped round 2 as round 1 with a fresh id, which then
            # collided with the real round 1 over the round number and renamed
            # the cycle out from under every later round.
            skip_prior = load_baseline(baseline or [],
                                       {"repo": repo_name, "github": gh_repo,
                                        "pr": pr_number, "round": round_no})
            skipped_payload = {
                **_payload_defaults(),
                "repo": repo_name, "github": gh_repo, "pr": pr_number,
                "title": title, "base": base,
                "round": round_no,
                "cycle": skip_prior.cycle,
                "prior_rounds": len(skip_prior.rounds),
                "prior_findings": len(skip_prior.keys),
                # A baseline this run could not read is a fact about the cycle,
                # not about the review it skipped, so it travels with the payload
                # rather than being dropped on the floor.
                "config_notes": skip_prior.problems,
                "skip_reason": f"title matches skip pattern /{pat}/",
                "run_key": run_key,
            }
            # --json-file is honoured here too, and its failure fails the run the
            # same way. The caller is told "if the panel could not write that file
            # the round did not happen", and it then feeds the file to the next
            # round as `--baseline`: a skipped PR that exited 0 leaving no file
            # gave that caller no signal at all.
            failed = write_payload(json_file, skipped_payload)
            if json_out:
                print(json.dumps(skipped_payload, indent=2))
            return finish(failed)
    print(f"\n[{repo_name}#{pr_number}] {title[:60]}", file=chatter)
    print(f"  base={base}  changed={changed} lines\n", file=chatter)

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
    panel_budget = diff_budget(panel, "max_diff_chars", DEFAULT_DIFF_BUDGET, notes)
    # Only for the reviewers actually running: a budget warning about a model
    # this run never asked for is noise, and a "truncated for antigravity" footnote
    # under a claude-only panel is a lie.
    budgets = {name: diff_budget(rev.get(name, {}), "max_diff_chars", panel_budget, notes)
               for name in LLM_REVIEWERS if name in selected}
    judge_budget = diff_budget(panel, "judge_max_diff_chars", panel_budget, notes)

    def prompt_for(budget: int | None) -> str:
        return REVIEW_PROMPT.format(n=pr_number, repo=gh_repo, base=base,
                                    diff=diff if budget is None else diff[:budget])

    # `agy` is the only reviewer whose prompt must travel in argv, so it is the
    # only one the kernel can veto. Clamp it to what execve will carry and say
    # so — the alternative, honouring the number and dying at exec, is how a
    # panel came to report "LLM reviewers ran: none" as a clean review.
    #
    # It is also the only seat an UNCAPPED budget can still cut, which is why the
    # clamp starts from the diff's own length when there is no budget: "no cap"
    # means "as much as this machine can hand over", and on this one seat that is
    # a smaller number than on the others. The note says so in chars of the diff
    # rather than in config terms, since there is no config value to blame.
    if "antigravity" in budgets:
        asked = budgets["antigravity"]
        fitted = fit_argv_budget(prompt_for, len(diff) if asked is None else asked)
        if fitted < (len(diff) if asked is None else asked):
            notes.append(
                f"antigravity gets {fitted:,} of {len(diff):,} diff chars — its prompt "
                f"travels in argv and the kernel caps one element at "
                f"{ARGV_PROMPT_MAX_BYTES:,} bytes. It is the only reviewer with no way "
                "to read a prompt off stdin.")
            budgets["antigravity"] = fitted

    truncated_for = {n: b for n, b in budgets.items()
                     if b is not None and len(diff) > b}
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
                                        prompt_for(budgets[name]), efforts.get(name, ""),
                                        cfg["path"])
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
                # A fact about the HOST rather than about the round — see
                # coverage_veto, which is the one consumer that treats it
                # differently from every other way of not running.
                "absent": got.absent,
                # Spread, not nested: a member whose usage could not be read
                # contributes no keys at all, so the board stores nulls and
                # renders "not recorded" — rather than a zero it would average in
                # as though the reviewer had cost nothing.
                **(got.usage or {}),
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

    # Pre-cluster as a hint, then let the master MERGE the duplicates and rule on
    # each issue in one step (no consensus gate). Dedup cannot happen upstream of
    # the judge without discarding what the other reviewers said — see adjudicate.
    clusters = cluster_findings(llm_findings)
    coverage = {n: m.get("could_not_assess") or [] for n, m in reviewer_meta.items()}
    findings, judge_skip, coverage_note = adjudicate(
        clusters, diff, panel.get("judge_model", ""), pr_number, judge_budget, coverage,
        cfg["path"])
    judged = judge_skip is None and bool(findings)
    to_fix = sorted((c for c in findings if c.verdict != "dismissed"),
                    key=lambda c: c.severity)
    dismissed = [c for c in findings if c.verdict == "dismissed"]
    # Sonar's hard-gate issues never reach the judge, so each is a canonical
    # record of its own single account — numbered after the judged ones, since
    # `related` is resolved against ids that must be unique across the payload.
    sonar = [Canonical(id=_finding_id(pr_number, len(findings) + i + 1),
                       severity=f.severity, file=f.file, line=f.line,
                       synthesis=f.title, verdict="sonar", reported_by=[f],
                       rationale=f.detail)          # the Sonar rule that fired
             for i, f in enumerate(result.sonar_findings)]

    # ---- this round against the ones before it. Mechanical: which findings are
    # ones no earlier round raised, and does that make the loop done?
    prior = load_baseline(baseline or [],
                          {"repo": repo_name, "github": gh_repo, "pr": pr_number,
                           "round": round_no})
    prior_keys, prior_rounds = prior.keys, len(prior.rounds)
    notes.extend(prior.problems)
    seen_before: dict[str, bool] = {}

    def is_new(c: Canonical) -> bool:
        """Did no earlier round raise this? One predicate for the round diff, the
        🆕 marker and the serialised `new_this_round`, so the payload cannot
        disagree with the report about which findings are fresh. Memoised: the
        reworded-title fallback is a sequence comparison against every title the
        baseline holds."""
        if c.key not in seen_before:
            seen_before[c.key] = prior.raised_before(c)
        return not seen_before[c.key]

    # Every finding the cycle has to clear, not just the judged ones: Sonar's
    # hard-gate issues MUST end up resolved (/panel-review-pr §3), so a round
    # whose only outstanding item is a new or still-open gate issue is not a dry
    # round. Leaving them out classified exactly that as convergence and ended the
    # cycle without another fixer.
    outstanding = to_fix + sonar
    new_keys = sorted({c.key for c in outstanding if is_new(c)})
    flagged = sum(1 for c in to_fix if c.needs_rereview)
    # A baseline that could not be read, could not be attributed, or was never
    # passed is a veto in its own right, not just a lost confidence flag: the
    # operator is told to LIST the vetoes, and "not convergence" with an empty
    # list leaves the one question this exists to answer unanswered.
    veto = coverage_veto(reviewer_meta, judge_skip, flagged, len(diff)) + prior.problems
    stop = round_stop(round_no, cap, new_keys, outstanding, veto, not prior.problems,
                      repeated=len({c.key for c in outstanding if not is_new(c)}))
    # Whether a CYCLE exists at all, and the one predicate that decides it — for
    # the report's Rounds block and for the payload alike. They used to disagree:
    # the report suppressed the block for a review-only run while the payload sent
    # `round_stop` regardless, so `record_review` stored a `/panel` read with
    # findings as `stopped: false` (the board shows a cycle mid-flight that nothing
    # will advance) and one without as `stopped: true, stop_confident: true` — a
    # confident-convergence record for a PR that had no cycle.
    cycle_run = bool(in_cycle or prior_rounds)
    # A cycle's rounds share one id, inherited from the earliest baseline, so the
    # board can tell "the re-review of THIS declaration" from "whatever ran next
    # on this PR". Only a round 1 of an actual cycle MINTS one.
    #
    # A later round whose baseline was missing, unreadable, from another PR or
    # not earlier sends null rather than a fresh id: `followed_by` requires the
    # cycles to match, so a minted one would make round 1 and round 2 of the same
    # PR two unrelated cycles forever and void every re-review declaration round 1
    # made — a permanent hole in a published measure, bought by a mistyped path.
    # Null records "unattributable", which is the truth and is recoverable.
    # A review-only run sends null too, which is what `ReviewIn.cycle` has always
    # documented ("for a standalone review that is nobody's round 2") and what the
    # producer never emitted.
    cycle = prior.cycle or (run_key if cycle_run and round_no == 1 else None)

    def loc(x: Canonical | Finding) -> str:
        return f"{x.file}:{x.line}" if x.line else x.file

    # ---- the run, as data. Built on every path, not just --json: it is what
    # --json prints, what --json-file writes, and what gets recorded on the
    # board. One structure, so the fix loop and the stats can never be looking
    # at different accounts of the same review — and one finding record per
    # defect, carrying every reviewer's own report, so a consumer reads the merge
    # instead of re-deriving it from an over-counted list.
    payload = {
        **_payload_defaults(),
        "repo": repo_name, "github": gh_repo, "pr": pr_number,
        "title": title, "base": base, "changed_lines": changed,
        # Always True in a payload the BOARD sees — the skip path returns before
        # `record_run` because no review happened. It is here for `--json`
        # consumers, which get both shapes and need to tell them apart.
        "reviewed": True,
        "diff_truncated": truncated,
        # Where this run sits in the panel -> fix -> panel cycle, and what the
        # mechanical stopping rule made of it.
        "round": round_no,
        "cycle": cycle,
        "prior_rounds": prior_rounds,
        "prior_findings": len(prior_keys),
        # Gated on there being a cycle, exactly as the report's Rounds block is.
        # For a review-only run `len(new_keys)` is every finding — the vacuous
        # count "raised by no earlier round" when there was no earlier round — and
        # `round_stop` is a verdict about a loop nobody is running. None rather
        # than 0, because the board's column already means "the panel did not
        # say", and a zero there is a claim.
        "new_findings": len(new_keys) if cycle_run else None,
        "new_finding_keys": new_keys if cycle_run else [],
        "round_stop": stop if cycle_run else None,
        "stop_reason": stop["reason"] if cycle_run else None,
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
        # `new_this_round` is added HERE rather than on the record: it is a fact
        # about this run's comparison against a baseline, not a property of the
        # defect, and a Canonical that carried it would have to be told about a
        # baseline to know its own shape.
        "to_fix": [{**c.as_dict(), "new_this_round": is_new(c)} for c in to_fix],
        "sonar_findings": [{**c.as_dict(), "new_this_round": is_new(c)} for c in sonar],
        "dismissed": [{**c.as_dict(), "new_this_round": is_new(c)} for c in dismissed],
        "skipped": result.skipped,
        "run_key": run_key,
    }

    # So a caller can have BOTH the PR comment and the machine-readable run.
    # Without --json-file, --json suppresses the report and the only way to get
    # both was to review the PR twice — several CLI invocations, for a copy. A
    # requested file that could not be written FAILS the run (see `finish`).
    write_failed = write_payload(json_file, payload)

    if record:
        record_run(payload)

    # ---- machine-readable mode: the whole run as JSON, no report/post. Same
    # shape as the skip-pattern exit's payload, so a consumer can read any key of
    # either without checking which exit it came from.
    if json_out:
        print(json.dumps(payload, indent=2))
        return finish(write_failed)

    # How many LLM seats the run was CONFIGURED to fill, against how many filled.
    # Both halves are needed and neither is derivable from the other: "claude ran"
    # is the same sentence whether it was the only seat asked for or the only one
    # of four that answered, and those are a hand-picked single-vendor read and a
    # panel that lost three quarters of its eyes.
    seats_asked = [n for n in LLM_REVIEWERS if n in selected]
    seats_filled = len(ran_llm)
    # The consensus signal needs two seats to exist AT ALL. Below that, "no
    # finding earned ⋆consensus" and "there was nobody to agree with" render
    # identically, and a reader takes the first meaning — the pessimistic
    # reading of a review that never had the chance to be pessimistic.
    consensus_possible = seats_filled > 1

    def conf(c: Canonical) -> str:
        revs = c.reviewers
        if len(revs) > 1:
            return f" _(via {', '.join(revs)} ⋆consensus)_"
        # Said per finding rather than once at the top, because this is the line a
        # reader is looking at when they decide how much a finding is worth.
        sole = " — sole reviewer, no second opinion" if not consensus_possible else ""
        return f" _(via {', '.join(revs)}{sole})_"

    def accounts(c: Canonical) -> list[str]:
        """What each reviewer actually said, under a MERGED finding.

        The synthesis is the judge's statement of the issue; these are the
        reports it was made from, and they are shown because one reviewer
        routinely makes a point the others didn't. Truncated here (the whole
        report is a PR comment) but kept whole in `--json` and on the board."""
        if len(c.reported_by) < 2:
            return []
        out = []
        for f in c.reported_by:
            said = _account(f)
            cut = said[:ACCOUNT_CHARS] + ("…" if len(said) > ACCOUNT_CHARS else "")
            out.append(f"  - _{f.reviewer}_ ({f.severity} `{loc(f)}`): {cut}")
        return out

    # ---- report
    heading = f"## Reviewer panel — PR #{pr_number}"
    # One predicate for the heading and the summary beneath it: a baseline that
    # parsed but held no findings is still an earlier round, and used to produce a
    # "· round 1" heading with nothing under it.
    in_rounds = round_no > 1 or bool(prior_rounds)
    if in_rounds:
        heading += f" · round {round_no}"
    lines = [heading, ""]
    if in_rounds:
        # Counted over everything the cycle has to clear (Sonar's hard gate
        # included), so the numerator and the denominator are the same population.
        lines.append(f"**Round {round_no}** — re-reviewing after the fix. "
                     f"{len(new_keys)} of {len(outstanding)} finding(s) here were raised by "
                     f"no earlier round ({len(prior_keys)} known from {prior_rounds} earlier "
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
    # The seat count rides with the reviewer list on EVERY run, not only degraded
    # ones. A round's finding count is not comparable across different panel
    # sizes, and the convergence table this repo keeps has been read as if it
    # were: #32 went 22 -> 43 between rounds and gained a reviewer in the same
    # step. The number that disambiguates it has to be in the artifact.
    lines.append(f"**LLM reviewers ran:** {', '.join(ran_llm) or 'none'}"
                 f" — {seats_filled} of {len(seats_asked)} configured")
    # The panel-level version of #19's per-reviewer fix. #19 stopped a reviewer
    # that produced nothing from reading as a reviewer that found nothing; this
    # stops a PANEL that lost half its seats from reading as a panel that agreed.
    # A run with empty seats is a materially weaker artifact than a full one and
    # was presented identically — on PR #64 that meant 23 findings from a single
    # reviewer, whose own master wrote that nine self-declared coverage gaps
    # "stand unchallenged and unread", laid out exactly like 23 from a full panel.
    # It is stated here, above the findings, rather than in a footer: under the
    # epic (#52) nobody is reading this in a terminal as it happens.
    if seats_filled < len(seats_asked):
        lost = len(seats_asked) - seats_filled
        lines.append(f"  - ⚠️ **panel degraded** — {lost} of {len(seats_asked)} "
                     f"configured reviewer{'s' if len(seats_asked) != 1 else ''} did not "
                     "run. Read what follows as a weaker review, not a cleaner one: "
                     "an empty seat cannot report what it would have found.")
    if not consensus_possible and seats_asked:
        lines.append("  - ⚠️ **no ⋆consensus is possible this round** — it takes two "
                     "reviewers to agree, and one filed. Absence of ⋆consensus below "
                     "means nobody was there to agree, NOT that nobody agreed.")
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
    if not findings:
        judge_txt = ("ruled on coverage only — no findings to judge" if coverage_note
                     else "n/a — no findings to judge")
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
        for c in to_fix:
            tail = f" — {c.rationale}" if c.rationale and c.rationale != "unjudged" else ""
            rel = f" _(same decision as {', '.join(c.related)})_" if c.related else ""
            # Said per finding, not only in the header: the rationale is blank
            # for these, so an unruled finding otherwise renders identically to
            # an adjudicated one under a header naming the judge.
            unruled = " _(unjudged — the master never ruled on this one)_" \
                if c.verdict == "unjudged" else ""
            # Only where there IS an earlier round to be new against: on a first
            # round every finding is new and the marker would be decoration.
            fresh = " 🆕" if prior_rounds and is_new(c) else ""
            again = (" ↻ _fix needs re-reading (" + ", ".join(c.rereview_by) + ")_"
                     if c.needs_rereview else "")
            lines.append(f"- **{c.severity}**{fresh} `{loc(c)}` [{c.id}] — {c.synthesis}"
                         f"{conf(c)}{unruled}{tail}{rel}{again}")
            lines += accounts(c)
    else:
        lines.append("- none")

    if sonar:
        lines.append(f"\n### SonarCloud issues ({len(sonar)}) — part of the gate")
        for c in sorted(sonar, key=lambda x: x.severity):
            # Same 🆕 rule as the judged findings: these count towards the round
            # diff too, because the gate has to end up clear either way.
            fresh = " 🆕" if prior_rounds and is_new(c) else ""
            lines.append(f"- {c.severity}{fresh} `{loc(c)}` — {c.synthesis}")

    if dismissed:
        lines.append(f"\n### Dismissed by master ({len(dismissed)})")
        for c in dismissed:
            lines.append(f"- ~~{c.severity} `{loc(c)}` — {c.synthesis}~~"
                         f"{conf(c)} — {c.rationale}")
            lines += accounts(c)

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
    # Only where a loop actually exists. `--max-rounds` is the CALLER's cap and
    # only /panel-review-pr drives the loop; a review-only run (`/panel`, or
    # panel.py by hand) that printed "round 1 of at most 2 — go again" promised a
    # round nothing would run, and counted every finding of a first review as one
    # "no earlier round raised" — which is vacuously all of them.
    if cycle_run:
        lines.append(f"\n{ROUNDS_HEADING} round {round_no} of at most {cap} — {verdict}: "
                     + stop["reason"]
                     + (" — a stop, not convergence" if unearned else ""))
        veto_head, bullet = "  _why this round's quiet is not evidence of a quiet PR:_", "  - ⚠️ "
    else:
        veto_head, bullet = ("\n**Coverage caveats** — why this review's quiet is not "
                             "evidence of a quiet PR:"), "- ⚠️ "
    if stop["veto"]:
        lines.append(veto_head)
        # A baseline problem is deliberately BOTH a config note (what went wrong)
        # and a veto (why the quiet does not count), and the payload carries it in
        # both roles on purpose — `config_notes` never reaches the board, so the
        # veto list is the record's only copy. On the PR comment, though, printing
        # the same sentence twice reads as two problems. The second appearance is
        # rendered as a pointer to the first.
        was_a_note = set(notes)
        for why in stop["veto"]:
            if why in was_a_note:
                lines.append(f"{bullet}{_veto_gist(why)} — _the config note above, "
                             "which is also why this round's quiet does not count_")
            else:
                lines.append(f"{bullet}{why}")

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
                                   gh_repo, "--body", fit_comment(report)],
                                  capture_output=True,
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
    return finish(write_failed)


def main() -> int:
    ap = argparse.ArgumentParser(description="Reviewer panel for a PR")
    ap.add_argument("--repo", help="repo path, or a name under ~/source (default: cwd)")
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--post", action="store_true", help="post summary as a PR comment")
    ap.add_argument("--json", action="store_true", dest="json_out",
                    help="emit the whole run as JSON on stdout; no report/post")
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
    ap.add_argument("--max-rounds", type=int, default=None,
                    dest="max_rounds", metavar="N",
                    help=f"the CALLER's round cap ({DEFAULT_MAX_ROUNDS} when this run is "
                         "part of a cycle); used to tell a round that stopped because it "
                         "was done from one that stopped because it ran out. Passing it "
                         "is what says this run belongs to a panel -> fix -> panel cycle: "
                         "without it (and without --round/--baseline) the run is a single "
                         "review and reports no rounds. `/panel-review-pr` spells it "
                         "--rounds N and passes it here on every invocation")
    args = ap.parse_args()
    if args.round_no < 1:
        raise SystemExit("--round: rounds are numbered from 1")
    if args.max_rounds is not None and args.max_rounds < 1:
        raise SystemExit("--max-rounds: at least one round has to run")
    # Checked against the EFFECTIVE cap, not only against an explicit one. The
    # default is the cap `run()` actually applies, so `--round 3` with no
    # --max-rounds used to pass this guard and then hit the cap branch on the
    # spot — writing "round cap (2) reached … unreviewed" into a round 3 and
    # printing "round 3 of at most 2". That is precisely the corrupted cycle
    # metadata this guard exists to prevent, leaking through the one spelling it
    # did not cover.
    cap = DEFAULT_MAX_ROUNDS if args.max_rounds is None else args.max_rounds
    if args.round_no > cap:
        default_note = "" if args.max_rounds is not None else \
            " (the default, since --max-rounds was not passed)"
        raise SystemExit(f"--round {args.round_no} is past --max-rounds "
                         f"{cap}{default_note}: raise the cap, or pass the round "
                         "this run actually is")
    return run(args.repo, args.pr, args.post, args.json_out, args.reviewers,
               args.json_file, args.record, args.round_no, args.baseline,
               args.max_rounds)


if __name__ == "__main__":
    raise SystemExit(main())
