"""Shared foundation for the panel: imports, tunables, prompts, and the helpers
every other module here calls.

Split out of `panel.py` (#129), which had reached 7,530 lines / 419,755 bytes —
5.6x growth in two days, and 3.5x `antigravity`'s 120,000-byte argv cap, so the
one seat whose prompt travels in argv could never be handed the file it was
reviewing. Reviewers also kept spending `could_not_assess` entries on code a few
thousand lines away in the same file.

The cut follows the section headers panel.py already carried, so this is a MOVE
and not a rewrite: nothing here was retyped.

**Patch where a function is DEFINED, not where it is imported.** A module that
does `from panel_core import sh` binds its own name, so `setattr(panel, "sh", …)`
rebinds only panel's copy and a caller living here still runs the real one. Tests
that drive a helper called from inside this module must patch `panel_core.sh`.
`__all__` is generated to include the underscore-prefixed names too, because the
suites reach for several of them by name through `panel`.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import errno
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
from collections import Counter
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

# What a round past the first REVIEWS. "increment" makes the review target the
# diff between the previous round's head and this one's — the fix commit, which
# is the only thing round 2 exists to read (#24) — with the rest of the PR
# supplied as context rather than as target. "pr" is the pre-v2.28 behaviour:
# re-read the whole growing PR every round.
#
# The default is `increment` because the alternative degrades as it works. Over
# PR #34's four rounds the diff went 140 KB -> 292 KB *because it was being
# reviewed*, until both reviewers declared they could not read ~600 lines of one
# test file — a loop that inflates its own input starves its own later rounds.
# Under increment scope the target stays roughly the size of one fix commit
# however large the PR grows, and it is the CONTEXT that gets squeezed. That is
# the right thing to lose: a reviewer that is short of context knows it and says
# so, whereas one handed a truncated target cannot see what it was not given.
#
# Only ever applies from round 2 — round 1 has nothing to be an increment from —
# and only when the previous round's head SHA is actually known (see
# `increment_anchor`). Falls back to "pr" and says so in `config_notes` rather
# than silently reviewing something other than what it claims to.
DEFAULT_ROUND_SCOPE = "increment"
ROUND_SCOPES = ("auto", "pr", "increment")

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


def seat_installed(name: str) -> bool:
    """Can this seat run on THIS box at all — is its CLI on PATH?

    A fact about the HOST, not about the round, and the distinction is one this
    file's neighbours already make at length: `coverage_veto` exempts an absent
    seat from vetoing a confident stop, because a reviewer whose CLI is not
    installed is absent every round and vetoing on it makes `confident`
    permanently unreachable on exactly the unattended boxes where the signal has
    to mean something.

    It lives HERE, beside :data:`CLI_BIN`, because that exemption was applied to
    the veto and to nothing else — and the seats' budgets were built from the
    CONFIGURED set, so a seat this box cannot run still acquired a diff budget, an
    argv clamp, a `config_notes` line about how much diff it "gets", and a
    `truncated: True` record. On a repo enabling a workstation-only vendor that
    made `diff_truncated` true on rounds where nothing that ran was cut, and
    `load_baseline` then banked it as a coverage gap the next round inherited
    (#222). One predicate, in the module both callers already import, is what
    stops the panel holding two opinions about which seats exist.

    :func:`panel_seats.run_seat` and :func:`panel_rounds.adjudicate` ask the same
    question, through this function rather than through their own copies of it:
    two spellings of "is this seat here" is how they come to disagree, and the
    disagreement is silent — a seat skipped as absent while its budget says it was
    handed 116,287 chars.

    The command it looks for comes from :data:`CLI_BIN`, falling back to the seat's
    own name. A vendor that renames its binary and is not recorded there reads as
    absent on every box, and since #222 that costs more than a visible
    `CLI ABSENT` skip line: the seat also loses its budget and its `config_notes`
    line, so it disappears from the run's configuration report rather than
    appearing in it as skipped. :data:`CLI_BIN` is the single place to record such
    a divergence, and this is the reason to keep it current.
    """
    return bool(shutil.which(CLI_BIN.get(name, name)))


def absent_seat_run(skip: str) -> "ReviewerRun":
    """The record a seat gets when this box cannot run it, without dispatching it.

    Byte-identical to what :func:`panel_seats.run_seat` writes on its own PATH
    check — same skip text, same `absent` flag, a nominal duration — because
    `coverage_veto`, the report and the board all read this record and none of them
    should be able to tell which branch produced it.

    It exists so the round can act ONCE on :func:`seat_installed` (#222,
    225-R3-F05). Dispatching an absent seat and letting `run_seat` refuse it leaves
    two PATH reads that can disagree: a seat that appeared since is spawned on an
    empty prompt and recorded as having run, and one that vanished since keeps the
    budget already written beside an `absent: true` — the contradictory pairing the
    fix exists to remove.
    """
    return ReviewerRun([], skip, 1, None, absent=True)

# Reviewer name -> the model used when its config says nothing, where that is not
# simply "whatever the CLI defaults to". Only claude has one: its CLI's own
# default is the account's top model, which is the wrong seat to spend by
# accident, so the panel pins `sonnet` and the others send no --model at all.
# One map because the round and the ask both resolve models and used to spell
# this exception inline, in two places, as a second line that rebuilt one entry
# of the dict just built above it.
SEAT_MODEL_DEFAULTS = {"claude": "sonnet"}

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

{ci}
PR #{n} ({repo}), base={base}:
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
{ci}
{diff}
"""

ASK_PROMPT = """You are answering ONE question about a system, as a point of order. This is NOT a
code review: do not look for defects, do not suggest improvements, and do not report anything the
question below does not ask about. A finding you make here goes nowhere.

Someone is about to build a fix on the PREMISE below. Say whether it HOLDS.

Answer from the material you are given and from nothing else. You have no tools and cannot open
the repository, so if what you were given does not settle the question, say so — "cannot tell" is
a real answer here and it is the right one whenever you would otherwise be guessing. It is never a
polite way of agreeing.

Return ONLY a JSON object (no prose):
  {{"verdict": "holds|fails|cannot tell", "reason": "one line"}}

- "holds" — the material shows the premise is true.
- "fails" — the material shows the premise is FALSE. Say in `reason` what makes it false, citing
  the line or the construct that decides it.
- "cannot tell" — the material does not settle it. Say in `reason` what you would need to see.

`reason` is ONE line: what decided it, not an essay. There is no severity, no file, and no
findings array — a reply carrying a findings array is an answer to a question nobody asked, and
is not read as an answer to this one.

--- PREMISE ---
{premise}
{context}"""


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
    #: This seat had no way to READ the code under review — an empty
    #: `member_sandbox` cwd and no file tools, so the diff in its prompt was the
    #: whole of its evidence. Every LLM seat is blind today; the flag exists
    #: because that is a property of how the panel is BUILT, not of the round it
    #: just ran, and `coverage_veto` has to be able to tell the difference.
    #:
    #: What it buys: a blind seat's `could_not_assess` entries are reported and do
    #: not veto. "I could not read a function this diff does not change" is true
    #: of every round a blind seat sits, so it separates no quiet round from a
    #: broken one — and `round_stop` computes `confident` as `not veto`, so a
    #: constant there made a confident stop unreachable on any PR that so much as
    #: mentions a file it does not touch. Measured on PR #160's round 1: 16 of 19
    #: veto lines were declarations, and nine of those asked about a file in this
    #: repo, answered with `grep` in four minutes.
    #:
    #: Set from the sandbox the seat actually ran in (see :func:`run_seat`),
    #: never assumed here: #113's second half makes code access a per-repo
    #: setting, and on a repo that turns it ON the same entry stops being
    #: structural and must veto again — it is then a fact about the round. A
    #: hard-coded exemption would keep silently discarding it.
    code_blind: bool = False


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



# Moved here with the rest of the reply-parsing helpers (#129): `_verdict_reading`
# above calls both, so leaving them in the synthesis section would have made this
# module reach forward into one that imports it. The split's section headers did
# not show that edge — the test suite did.

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

# The ask primitives and `_same_file` moved here (#129): panel_seats calls all of
# them, and a module the core imports must not have to reach back into it. Same
# edge as `_member_ids` — the section headers did not show it and the suite did.
# ----------------------------------------------------------------------------- the ask

#: The only three answers to a premise challenge. `cannot tell` is one of them
#: and not an abstention: a seat whose context did not settle the question has
#: said something, and it is not agreement. Counting it as agreement is #68's
#: panel-of-one arriving through a side door — a tally that reads "3 seats, and
#: nobody objected" over two seats that could not see the code.
ASK_VERDICTS = ("holds", "fails", "cannot tell")

#: Spellings of "cannot tell" a model reaches for unprompted. Deliberately a
#: short closed list of the same three words rather than a fuzzy match: an
#: unrecognised verdict makes the reply unreadable, which costs one retry and is
#: then reported as a seat that did not answer — whereas guessing at what a
#: novel word meant would put a verdict nobody wrote into the tally.
_ASK_ALIASES = {"cannot tell": "cannot tell", "cant tell": "cannot tell",
                "can't tell": "cannot tell", "cannot-tell": "cannot tell"}

#: How long a reason may be. It is asked for as one line and rendered as one; a
#: model that writes an essay gets it cut here rather than in the report alone,
#: so the payload and the board carry the same text the reader saw.
ASK_REASON_CHARS = 400

#: Keys that make a reply a REVIEW rather than an answer. The prompt forbids them
#: in those words ("a reply carrying a findings array is an answer to a question
#: nobody asked"), and until this list existed the prompt said one thing and the
#: parser accepted another: `{"verdict": "holds", "findings": [...]}` was read as
#: a clean answer. Only the array is refused, not every review-shaped word — a
#: seat that mentions a file in its `reason` has still answered THIS question,
#: and rejecting it would cost a retry and then a real verdict.
_REVIEW_SHAPED = ("findings",)


def _cut(text: str, limit: int) -> str:
    """`text` at `limit` chars, ELLIPSISED when it did not fit.

    One helper for both places an ask shortens a model's words, because the
    marker is the whole point: a reader of the report or the payload cannot
    otherwise tell a reason that was cut from one the seat finished."""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _ask_reason(val: object) -> str:
    """A seat's stated reason as one bounded line, whatever shape it arrived in.

    A model that answers `{"verdict": "fails", "reason": ["line 10 is wrong"]}`
    has given its justification; reading only `str` dropped it and left the seat
    voting for no stated reason at all. The verdict was never in doubt, so the
    reason is rendered rather than discarded."""
    if val is None or isinstance(val, bool):
        return ""
    if isinstance(val, str):
        said = val
    elif isinstance(val, (list, tuple)):
        said = " ".join(_ask_reason(v) for v in val)
    elif isinstance(val, (int, float)):
        said = str(val)
    else:
        said = json.dumps(val, default=str)
    return " ".join(said.split())


class Answer(NamedTuple):
    """One seat's reply to `--ask`: a verdict from :data:`ASK_VERDICTS` and the
    one line behind it."""

    verdict: str
    reason: str


def _ask_verdict(val: object) -> str | None:
    """`val` as one of :data:`ASK_VERDICTS`, or None when it is not one of them.

    None includes the schema's own `"holds|fails|cannot tell"` handed straight
    back, and that is the whole echo defence this parser needs: the review
    prompt's example is a fully populated finding that reads as an answer until
    :func:`_quoted` positively identifies it, while here the illustration is
    spelled as the union of the three legal values and so is not one of them.
    A quotation is refused by the same check that refuses a typo."""
    if not isinstance(val, str):
        return None
    said = " ".join(val.strip().lower().replace("_", " ").split())
    if said in ASK_VERDICTS:
        return said
    return _ASK_ALIASES.get(said)


def _one_verdict(pairs: list[tuple[str, object]]) -> dict:
    """`json.loads`'s object hook, refusing an object that states its verdict
    twice.

    The default keeps the LAST of duplicate keys, silently, which is exactly the
    resolution :func:`parse_answer` refuses everywhere else: a reply saying both
    `holds` and `fails` is one this file will not choose between. Raising here
    makes the candidate unreadable, which is the same answer two conflicting
    objects in one reply already get."""
    if sum(1 for k, _ in pairs if k == "verdict") > 1:
        raise ValueError("two verdicts in one object")
    return dict(pairs)


def parse_answer(raw: str | None) -> Answer | None:
    """Read a seat's reply to a premise challenge, or None when it cannot be read.

    None means UNREADABLE and never "the seat had no opinion" — the same
    distinction :func:`parse_reply` keeps, and for the same reason: a seat whose
    reply could not be parsed must not be counted in a tally as one that looked
    and could not tell. The caller retries once (see :func:`run_seat`) and then
    records the seat as having answered nothing, which is louder than a
    `cannot tell` and correctly so.

    Candidates are settled by AGREEMENT, never by rank, exactly as
    :func:`_agreed` settles a review: a reply holding an echo of the schema and a
    real answer resolves, because the echo is not a legal verdict and is dropped
    before agreement is tested; a reply holding two DIFFERENT legal verdicts does
    not, because nothing here is willing to choose which of them the model meant.
    Picking one would be how a `holds` gets recorded for a seat that also wrote
    `fails`. That rule holds WITHIN one object too: `json.loads` keeps the last
    of two identical keys, so `{"verdict":"holds","verdict":"fails"}` used to be
    recorded as `fails` — one reply carrying two conflicting legal answers,
    resolved by a detail of the JSON parser. :func:`_one_verdict` refuses it
    instead, which is the same refusal two conflicting objects already get."""
    if not raw:
        return None
    seen: list[Answer] = []
    for _, text in _spans(raw, "{", "}"):
        try:
            val = json.loads(text, object_pairs_hook=_one_verdict)
        except ValueError:
            # JSONDecodeError for malformed JSON, plain ValueError for the
            # duplicate-key refusal — both mean this candidate is not an answer.
            continue
        if not isinstance(val, dict):
            continue
        if any(isinstance(val.get(k), list) for k in _REVIEW_SHAPED):
            continue
        verdict = _ask_verdict(val.get("verdict"))
        if verdict is None:
            continue
        seen.append(Answer(verdict, _cut(_ask_reason(val.get("reason")), ASK_REASON_CHARS)))
    if not seen:
        return None
    # Agreement on the VERDICT alone: two candidates that say `fails` for
    # differently worded reasons are one answer, and the last of them is as good
    # as the first. Two that say different verdicts are not an answer at all.
    if len({a.verdict for a in seen}) > 1:
        return None
    # The last one, among candidates that agree — a model that restates its
    # answer at the end of a reply has restated it, and its wording there is the
    # one it settled on.
    return seen[-1]


@dataclass
class SeatAnswer:
    """What one seat did with a premise challenge.

    Three outcomes, kept apart on purpose, because the tally treats them
    differently and a report that flattened them would be the panel-of-one
    problem in miniature:

    * it answered — `verdict` is one of :data:`ASK_VERDICTS`;
    * it never ran — `skip` says why, and `absent` says whether that is a fact
      about this box rather than about the ask;
    * it ran, replied, and neither attempt's reply could be read as a verdict —
      `unreadable`. That is NOT `cannot tell`: one is a seat saying it could not
      settle the question, the other is a seat whose answer we do not have.
    """

    verdict: str | None = None
    #: The seat's own one-line justification for `verdict`. Only ever that — what
    #: an UNREADABLE reply said goes in `gist`, because a consumer rendering
    #: `reason` without also branching on `unreadable` would otherwise show a
    #: model's rambling preamble as though the seat had stated it as its reason.
    reason: str = ""
    skip: str | None = None
    unreadable: bool = False
    #: The head of a reply that carried no verdict — WHAT the seat said, not why
    #: it said it. Empty for every seat that answered.
    gist: str = ""
    duration_ms: int = 0
    usage: dict | None = None
    absent: bool = False


class AskTally(NamedTuple):
    """What the seats' answers add up to, and why."""

    #: holds | fails | unresolved | unchallenged. The last two are different
    #: failures to reach an answer: `unresolved` is a panel that looked and did
    #: not agree, `unchallenged` is a tally with no standing to say anything —
    #: too few seats answered, or the only one that did is the agent that wrote
    #: the premise.
    verdict: str
    reason: str
    #: One count per entry of :data:`ASK_VERDICTS`.
    counts: dict[str, int]
    #: How many seats answered at all — the quorum numerator. An unreadable or
    #: skipped seat is not in it.
    answered: int


def ask_tally(answers: dict[str, SeatAnswer], quorum: int, threshold: int,
              asker: str = "") -> AskTally:
    """The vote, and it IS the output — there is no judge here.

    Quorum counts seats that ANSWERED; threshold counts seats that said the same
    thing. `cannot tell` is in the first and never in the second, which is what
    stops a panel that could not read the code from reporting agreement.

    `asker` is the seat the agent running this challenge is itself. When it is
    the only seat that answered, the result is `unchallenged` however emphatic
    the answer was: an agent putting its own premise to itself has confirmed
    nothing, and reporting that as `holds` is worse than reporting nothing at all
    because it carries a panel's authority. Same rule as #78's `self_approval`
    and #40's refusal to let a reviewer act on its own finding unattended.

    A split that reaches the threshold BOTH ways is `unresolved`, not the first
    branch tested. It needs `len(seats) >= 2 * threshold` — so the DEFAULT
    configuration reaches it on a four-seat panel that splits two against two,
    and it is not the `ask_threshold: 1` curiosity it was once described as.
    Either way the tie is not broken by the order this function checks things in.
    """
    voted = {n: a for n, a in answers.items() if a.verdict}
    counts = {v: sum(1 for a in voted.values() if a.verdict == v) for v in ASK_VERDICTS}
    answered = len(voted)
    tally = (f"{counts['holds']} holds / {counts['fails']} fails / "
             f"{counts['cannot tell']} cannot tell")
    rule = f"quorum {quorum}, threshold {threshold}"
    if not answered:
        return AskTally("unchallenged", "no seat answered — nothing was challenged",
                        counts, answered)
    if answered < quorum:
        return AskTally("unchallenged",
                        f"{answered} seat{'s' if answered != 1 else ''} answered, "
                        f"and the quorum is {quorum} — {tally}", counts, answered)
    if asker and set(voted) == {asker}:
        return AskTally("unchallenged",
                        f"the only seat that answered is {asker}, which is the asker "
                        "— a premise put to yourself is not a challenge", counts, answered)
    reached = [v for v in ("holds", "fails") if counts[v] >= threshold]
    if len(reached) == 1:
        won = reached[0]
        # The self-challenge rule again, one layer in — and this is the layer that
        # matters, because the outer check only catches the asker being the only
        # SEAT. Under `ask_threshold: 1` an asker could reach the threshold on its
        # own vote while every other seat answered `cannot tell`: quorum met, more
        # than one seat answered, and a verdict resting entirely on the agent that
        # wrote the premise. What has to be true is that the ANSWER is not the
        # asker's alone, not merely that the panel was not.
        backers = {n for n, a in voted.items() if a.verdict == won}
        if asker and backers == {asker}:
            return AskTally("unchallenged",
                            f"the only seat saying the premise {won.upper()} is "
                            f"{asker}, which is the asker — the others could not "
                            f"tell or said otherwise ({tally})", counts, answered)
        return AskTally(won, f"{counts[won]} of {answered} say the premise "
                             f"{won.upper()} ({rule})", counts, answered)
    # Two different sentences, because "nobody reached the threshold" and "both
    # answers did" are opposite states and the single wording asserted the first
    # of a panel that had split down the middle — which reads as an unconvincing
    # challenge rather than as a genuine disagreement worth reading.
    why = ("both answers reached the threshold" if reached
           else "no answer reached the threshold")
    return AskTally("unresolved", f"{why} — {tally} ({rule})", counts, answered)





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

# Shared by both entry points (#129): `run()` writes a review payload and `ask()`
# writes an ask payload, and both exit through `finish`. Leaving them beside run()
# would have made panel_ask import panel, which imports panel_ask.

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


#: Exit code for "the review ran, but the requested --json-file was not written".
#: Deliberately not 2: argparse exits 2 on its own usage errors, and the caller is
#: told a non-zero exit means the round did not happen for cycle purposes — which
#: it cannot tell from a mistyped flag if the two share a code.
UNWRITTEN_PAYLOAD_EXIT = 3

# Range/auth primitives used by BOTH panel_seats and panel_scope (#129), so they
# sit in the foundation rather than in either caller.

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

def _diff_file_path(line: str) -> str | None:
    """The repo-relative new-side path a diff header line names, or None where it
    names none — a `+++ /dev/null` deletion, a header nothing can parse.

    ONE parser for `diff --git a/… b/…` and for `+++ b/…`, shared by
    :func:`_diff_added_lines` and :func:`_diff_files_cut` because
    :func:`_provenance` compares one's keys against the other's members through
    :func:`_same_file`: two spellings of "what a path is" would misattribute in
    silence rather than fail. `+++` is the reliable anchor, carrying ONE path so
    nothing has to be guessed about where it ends; the `diff --git` header is
    parsed as well only so a file has a name before its `+++` line arrives, which
    matters when a budget cuts between the two.
    """
    if line.startswith("+++ "):
        tok = _unquote_path(line[4:].strip())
        return tok[2:] if tok.startswith("b/") else None
    if not line.startswith("diff --git "):
        return None
    rest = line[len("diff --git "):].strip()
    # `a/P b/P`, where both sides are the SAME path. Git does not quote a plain
    # space, so `diff --git a/x b/y.py b/x b/y.py` splits at the wrong ` b/`
    # whichever end you start from — but the two halves are equal in length, so
    # the split point is arithmetic rather than a guess.
    if len(rest) > 5 and (len(rest) - 5) % 2 == 0:
        half = (len(rest) - 5) // 2
        a_side, b_side = rest[:2 + half], rest[2 + half:]
        if a_side.startswith("a/") and b_side == " b/" + a_side[2:]:
            return a_side[2:]
    # A rename (`a/old b/new`), or a quoted path. Quoted, both sides are quoted
    # and the separator between them is unambiguous. Unquoted, the first ` b/` is
    # the best guess left, and a path containing one is misread until the `+++`
    # line corrects it.
    if rest.startswith('"') and rest.endswith('"') and '" "' in rest:
        tok = _unquote_path('"' + rest.rsplit('" "', 1)[1])
        return tok[2:] if tok.startswith("b/") else None
    _, sep, tail = rest.partition(" b/")
    return tail.strip() if sep else None

#: How long provenance waits on the compare API, and how much of a range it will
#: hold. Nothing gates on provenance, so a slow or enormous range degrades to
#: "unknown" rather than making a round wait on it or keeping it all in memory.
FIX_RANGE_TIMEOUT_S = 60

FIX_RANGE_MAX_CHARS = 2_000_000

#: Only what attribution reads: the ancestry verdict and the per-file patches.
#: The compare response also carries every commit in the range and a dozen URLs
#: per file, and none of that is ever looked at.
_FIX_RANGE_JQ = "{status: .status, files: [(.files // [])[] | {filename, patch}]}"


def _unquote_path(tok: str) -> str:
    r"""Git's C-quoted path form (`"a/w\303\251ird.py"`) back to the real path.

    Git quotes a path — in the `diff --git` header and on the `---`/`+++` lines
    alike — whenever it holds a non-ASCII byte, a quote, a backslash or a control
    character, escaping the bytes in octal. Left quoted, such a file is spelled
    one way here and another way by every reviewer that reports a finding in it,
    and :func:`_same_file` then matches neither spelling against the other.
    """
    if len(tok) < 2 or not (tok.startswith('"') and tok.endswith('"')):
        return tok
    try:
        return (tok[1:-1].encode("utf-8").decode("unicode_escape")
                .encode("latin-1").decode("utf-8"))
    except (UnicodeDecodeError, UnicodeEncodeError):
        return tok[1:-1]  # not the escaping git uses; the quotes still come off


#: Everything this module offers, INCLUDING the underscore names — the suites
#: reach for several of them through `panel`, and a plain star import would drop
#: them silently. Generated from the module's own top level, so a helper added here
#: is exported without anyone remembering to list it.
__all__ = [
    "argparse", "base64", "difflib", "errno",
    "hashlib", "json", "os", "re",
    "shutil", "ssl", "subprocess", "sys",
    "tempfile", "time", "urllib", "uuid",
    "Counter", "Callable", "ThreadPoolExecutor", "dataclass",
    "field", "Path", "NamedTuple", "harness_rules",
    "DENIAL_MARKERS", "REJECTION_MARKERS", "RepoNotFound", "cli_outcome",
    "describe", "resolve_repo", "stderr_gist", "DEFAULT_DIFF_BUDGET",
    "RAW_DETAIL_CHARS", "CLUSTER_WINDOW", "ACCOUNT_CHARS", "DEFAULT_MAX_ROUNDS",
    "DEFAULT_ROUND_SCOPE", "ROUND_SCOPES", "CLI_TIMEOUT", "BLANK_RETRY_MAX_S",
    "CLI_ABSENT", "ARGV_PROMPT_MAX_BYTES", "SEVERITIES", "MAX_LISTING_CHARS",
    "LISTING_ACCOUNT_CHARS", "COMMENT_CHARS", "ROUNDS_HEADING", "LLM_REVIEWERS",
    "ALL_REVIEWERS", "CLI_BIN", "seat_installed", "absent_seat_run",
    "SEAT_MODEL_DEFAULTS",
    "REVIEW_PROMPT",
    "JUDGE_PROMPT", "ASK_PROMPT", "Finding", "ReviewerRun",
    "PanelResult", "sh", "load_repo_cfg", "_spans",
    "ENVELOPE_KEYS", "DECLARATION_KEYS", "_scalar", "_Tok",
    "_TOKEN", "_TOKEN_MARK", "_tokenise", "_schema",
    "SCHEMA_ECHOES", "_example", "SCHEMA_ITEMS", "_standins",
    "SCHEMA_DECLARATIONS", "_quoted", "_is_answer", "_Read",
    "_read", "_Ambiguous", "_AMBIGUOUS", "_agreed",
    "extract_json_value", "_severity", "_TRUTHY", "_flag",
    "_to_findings", "_findings_of", "_verdict_reading", "_str_list",
    "_declaration", "parse_reply", "_raw_finding", "_member_ids",
    "_ruling", "ASK_VERDICTS", "_ASK_ALIASES", "ASK_REASON_CHARS",
    "_REVIEW_SHAPED", "_cut", "_ask_reason", "Answer",
    "_ask_verdict", "_one_verdict", "parse_answer", "SeatAnswer",
    "AskTally", "ask_tally", "_same_file", "write_payload",
    "finish", "UNWRITTEN_PAYLOAD_EXIT", "resolve_token", "_ssl_context",
    "_diff_file_path", "FIX_RANGE_TIMEOUT_S", "FIX_RANGE_MAX_CHARS", "_FIX_RANGE_JQ",
    "_unquote_path",
]
