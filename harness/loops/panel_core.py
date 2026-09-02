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
import tarfile
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
                           RULES_FILENAME, SAMPLE_FILENAME,
                           RepoNotFound, cli_outcome, describe,
                           resolve_repo, stderr_gist)
# #279's vocabulary, through the one module that knows where it is defined.
from needs_human import (class_or_none as needs_human_class_or_none,  # noqa: E402
                         reason_or_none as needs_human_reason_or_none)

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

# Panel -> fix -> panel. One is provably not enough: the fixer's own commit is
# otherwise read by nobody, and structural fixes beget new interactions that no
# earlier round could have seen because they did not exist until the fix was
# written. It is a cap on the CALLER's loop, used here only to decide whether a
# round that still has work left stopped because it was done or because it ran out
# of rounds.
#
# **6 as of 2026-08-30, from 2 (#621).** THE CAP IS A BACKSTOP AGAINST RUNNING
# FOREVER AND NOT A CONVERGENCE MECHANISM, and 2 was being asked to be both: a cycle
# that ends on the cap has produced a fix nobody read and a remainder handed to
# somebody, which is the opposite of the confident dry round the cap was being
# credited with. What ends a cycle from here is `escalate_on`, `fix_injection` first.
# `harness_rules.DEFAULTS` carries the evidence and the way back.
DEFAULT_MAX_ROUNDS = 6

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

# How long the PR's tree may take to arrive. Far below CLI_TIMEOUT and separate
# from it on purpose: this is one HTTP download that happens before any seat
# starts, so every second of it is added to the whole round rather than spent
# inside one reviewer's budget. A tarball this slow is a network problem, and the
# answer to a network problem here is to review from the diff — which is the OFF
# posture, still works, and is what the caller falls back to.
TREE_FETCH_TIMEOUT = 120

# Ceilings on the PR's tarball, which is INPUT THE CONTRIBUTOR CONTROLS. Without
# them a PR can hand the panel a tree that fills the disk, and gzip makes that
# cheap to post: a few megabytes of tarball can declare gigabytes of files, so the
# dangerous number is the decompressed one and it is checked separately from the
# download. Both refuse rather than truncate — half a tree is worse than no tree,
# because a reviewer reads the half it got as the whole repository.
#
# 256MB compressed is far above any repo the panel is pointed at and far below
# anything that hurts; 2GB extracted is the same judgement one decompression step
# later. A legitimate repo over either is a real answer ("too big to review this
# way"), not a reason to raise them blindly.
TREE_MAX_BYTES = 256 * 1024 * 1024
TREE_MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024

# And a ceiling on the NUMBER of members, which the byte caps are blind to: a small
# tarball can declare millions of zero-byte entries, each costing an inode, a syscall
# and a TarInfo in memory while passing every size check. The largest repositories in
# real use are a few hundred thousand files, so this is far above legitimate and far
# below painful.
TREE_MAX_MEMBERS = 500_000

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

#: What `claude` prints when `--max-budget-usd` is reached. Matched against
#: STDOUT, not stderr: the CLI exits 1, writes this line to stdout, and leaves
#: stderr EMPTY — verified on 2.1.232. That combination defeats both of
#: `run_cli`'s failure readers, which is why this constant exists rather than a
#: generic non-zero-exit path handling it.
BUDGET_MARKER = "Exceeded USD budget"

#: The skip reason a budget-exhausted seat records. Its own sentence because
#: "exited 1" with an empty stderr is exactly the confusing death #19 is about:
#: the cap is the cause, the cap is actionable, and nothing else in the output
#: says so.
BUDGET_EXHAUSTED = "spend cap reached mid-review"

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

# ----------------------------------------------------------------- #165's dials
#
# The built-in half of `review_panel.{fixer_may_defer, file_deferral_issues,
# fix_severity_floor, round_trigger_floor, low_severity_fix_lines, max_fix_growth,
# reviewer_scope, require_failing_test, max_rounds}`. WHY each number is this number
# lives beside the key in `harness_rules.DEFAULTS`, which is the file an operator
# reads; these are what the resolvers in `panel_seats` fall back to when a rules
# file (or a test's hand-written `panel` literal) does not carry the key. The two are
# asserted equal by `tests/test_panel_dials.py` — a drift between them would leave the
# documented default and the applied default disagreeing, silently, in the direction
# nobody checks.

#: Findings at or above this severity are what a fix round is asked to clear. Not P2:
#: severity is model-authored and wrong sometimes, and the defect class a P2 floor
#: systematically misses is correctness expressed as craft.
#:
#: **P4 as of 2026-08-30, from P3 (#621).** This is not the blocking band and has not
#: been since #297 — `round_trigger_floor` is, and it stays at P2. Admitting P4 adds
#: no obligation: it puts the P3 AND P4 band inside `low_severity_fix_lines`' budget,
#: where before P3 was the whole of that band and P4 sat outside every rule, so which
#: of them a round actually takes is decided cheapest-first by a count rather than by
#: the fixer's judgement. `harness_rules` carries the argument.
DEFAULT_FIX_SEVERITY_FLOOR = "P4"
#: New findings at or above this severity are what buys another round. Stays P2 while
#: the fix floor reaches to P4, deliberately: fixing a low-severity finding inside a
#: pass that is already open costs one edit; letting one buy a whole new round costs a
#: panel plus a fix pass. It is the gap between the two that the budget pays for.
DEFAULT_ROUND_TRIGGER_FLOOR = "P2"
#: Churned lines the whole round may spend fixing findings BELOW the trigger floor —
#: the tier the fix floor admits and the measurement does not. 40 because the failure
#: is accumulation, not one balloon: PR #188's round-1 fix pass was 408 lines of
#: individually reasonable small fixes on a 185-line feature. `None` is no budget at
#: all (the pre-#297 behaviour); `0` fixes none of them. `harness_rules` carries the
#: measurement.
DEFAULT_LOW_SEVERITY_FIX_LINES = 40
#: The PROPORTIONAL half of that budget (#551), and it is a SIZE rather than a rate:
#: the first round's `pr_chars` at or above which the whole `low_severity_fix_lines`
#: budget applies. Below it the budget is pro rata — `lines x first_chars / this` — so
#: the round spends whichever of the two ceilings is smaller and the pair can only ever
#: tighten. The opposite operator to #664's floor one block down, because the
#: accumulation `low_severity_fix_lines` measures is dangerous on the SMALL PR, where a
#: fixed 40 lines can exceed the diff it is polishing.
#:
#: **Denominated in chars because `pr_chars` is what a baseline records**, so nothing
#: here converts between chars and lines and the value cannot inherit #692's dispute
#: about PR #188's churn. 14,325 is the median `pr_chars` of this repo's merged PRs
#: scaled to the ~182 churned lines at which 40 lines is the ~22% allowance #551 calls
#: sane (n=21, range 9,538-18,604). None switches the proportional half off and
#: restores the pre-#551 behaviour exactly; `harness_rules` carries the calibration.
DEFAULT_LOW_SEVERITY_FIX_FULL_CHARS = 14_325
#: How much of the low-severity budget one UNREFEREED churned line costs, against a
#: production line's 1 (#554). The budget's unit becomes exposure rather than length:
#: a line written where nothing can check it spends more of the round than a line
#: written where red/green, the suite and CI all can.
#:
#: **This is not value-weighting**, which #297 refuses deliberately because it hands
#: judgement back to the actor whose judgement the 63.7% measurement indicts. Being
#: refereed is a property of the PATH AND THE LINE, read off the fix's own diff — the
#: one the fixer already produces to measure the fix at all — and never an opinion
#: about whether the work is worth doing. The fixer is asked for a multiplication, not
#: a forecast.
#:
#: **Not `git diff --numstat`**, which is what this said until a Codex second opinion
#: pointed out that numstat reports per-file insertion and deletion TOTALS and cannot
#: see a comment, a blank or a docstring. #554's own wording — "classifying each PATH
#: is free at that point" — is true of numstat and was extended here to lines, where
#: it is not. The line half needs the diff body, which costs nothing extra because the
#: fixer is already looking at it.
#:
#: **2 is the one number in #554 that is a judgement rather than a fact**, and it is
#: written down here so it can be argued with rather than discovered. For it: an
#: unrefereed line has NO referee, not a weaker one, so the budget must buy strictly
#: fewer of them, and 2 is the smallest weight that says so. Against a larger number:
#: the budget bounds a round's spend and is not a tax meant to stop fixers writing
#: tests — at 40 lines a weight of 2 still affords a 20-line regression test inside
#: the band. `1` prices every line alike, which is the pre-#554 behaviour.
#:
#: **The band it applies to is narrow, and that is what makes the weight safe.**
#: `low_severity_fix_lines` pays only for findings the fix floor admits and the round
#: trigger floor does not — the P3 band at the shipped floors. A P1/P2 fix and its
#: test are not on the budget at all and nothing here can price them. What this
#: reprices is exactly the population #554 measured: four of the five budgeted fixes
#: on that round were "write more test", the category with no referee and the highest
#: injection rate. `harness_rules` carries the measurement.
DEFAULT_UNREFEREED_LINE_WEIGHT = 2
#: The floor value that means "no floor" — the least severe severity there is, so
#: everything is at or above it. Both floors default to this INSIDE `round_stop`,
#: which is what keeps every caller that has not heard of them on the old
#: behaviour rather than on the new default.
NO_SEVERITY_FLOOR = SEVERITIES[-1]
#: The severities `panel_rounds.round_stop`'s rule 2 treats as blockers, at every
#: setting of every floor a repo can write. Named rather than spelled twice: rule 2
#: is where the tuple has always been hardcoded, and #78's corroboration threshold
#: has to read the SAME set to know which findings it may never stand down. Two
#: literals would be one refactor away from a threshold that suppresses a finding
#: rule 2 goes on demanding, which is the jam :func:`panel_seats.Dials.threshold_for`
#: exists to make unreachable.
BLOCKING_SEVERITIES = ("P1", "P2")
#: #78's corroboration threshold: how many DISTINCT seats must independently raise a
#: finding at a given severity before it is this round's work — `{"P3": 2}` for "a
#: solo P3 is reported, not fixed". A severity absent from the mapping needs one
#: seat, which is today's behaviour, so `{}` is off and is what ships.
#:
#: **`{}`, and it is UNSET rather than off**, on `max_fix_guard_lines`' precedent
#: (#618). The evidence that corroboration predicts a real finding is #78's own
#: table — every finding Rich refuted on 2026-08-20 was single-seat and no
#: multi-seat finding failed — and that is eight findings on two pull requests, with
#: `32-F01` a genuine solo P1 sitting inside it. Eight is an observation, not a
#: calibration, and #67's rule is that an instrument earns a gate over a few dozen
#: cycles or not at all. So the seat count is recorded per finding every round
#: (`reviewers` on the payload, and the `⋆consensus` notation in the report have both
#: carried it since long before this key) and nothing is stood down until a repo
#: writes a number it can defend.
#:
#: **What it may never do**, and this is a property of the mechanism rather than of
#: the default — see :meth:`panel_seats.Dials.corroboration_applies`. A threshold can
#: only stand down a severity BELOW `round_trigger_floor` that is also not one of
#: :data:`BLOCKING_SEVERITIES`. A single seat finding a genuine P1 nobody else spotted
#: is the case the panel exists for, and a count is the wrong instrument for deciding
#: whether to act on it.
DEFAULT_THRESHOLD_BY_SEVERITY: dict[str, int] = {}
#: How many times the first round's reviewed size a later round may review before
#: the cycle stops and says the change wants splitting. None disables it.
DEFAULT_MAX_FIX_GROWTH = 3.0
#: The ABSOLUTE half of that ceiling (#492): chars the PR may GROW past the size the
#: cycle's first round read it at, before the same stop fires. Whichever of this and
#: the multiple is crossed FIRST binds, so THIS KEY can only ever tighten the check —
#: a claim about the pair of ceilings and not about the mechanism, which since #664 has
#: a floor in it that loosens (see below). A pure
#: multiple hands its rope out in proportion to the starting size — 226 lines on a
#: 113-line PR, 4,000 on a 2,000-line one — and the second is the case most in need of
#: a ceiling. None disables this half and leaves the multiple; `harness_rules` carries
#: the calibration.
DEFAULT_MAX_FIX_GROWTH_CHARS = 30_000
#: The FLOOR under the multiple (#664), and the one term in this mechanism that
#: LOOSENS: the ratio half fires only where the PR has also grown by more than this
#: many chars. Proportionality bites at both ends — the ceilings above answer the top,
#: where a multiple's rope grows with the starting size, and this answers the bottom,
#: where a fixed per-hunk diff framing cost (~430 chars of `diff --git`, `index`,
#: `---`/`+++`, `@@` and context) is charged against a PR too small to afford it. On a
#: 439-char PR the 3.0x allowance is 878 chars and the smallest honest one-file fix
#: measured 827, half of it framing. None switches the floor off and restores the
#: pre-#664 behaviour exactly; `harness_rules` carries the calibration.
DEFAULT_MIN_FIX_GROWTH_CHARS = 2_000
#: The GUARD half of the same question, per PASS rather than per PR (#618): test and
#: prose lines ONE fix pass may churn before the ceiling reports — or, where the repo
#: arms `escalate_on.guard_lines`, ends the cycle.
#:
#: **`None`, and it is unset rather than off.** The only measurement anyone has is
#: lexray#1780's five rounds, whose passes wrote 380, 205, 205 and 58 guard lines; a
#: number drawn between the quiet round and the loud one on that single cycle would be
#: a ceiling with its argument written afterwards, which is what #67 forbids. So the
#: instrument ships measured and uncalibrated, and `harness_rules` carries the
#: arithmetic and the reason a cumulative ratio could not do this job.
DEFAULT_MAX_FIX_GUARD_LINES = None
#: What a reviewer is asked to look for: defects in the change (`diff`), or in the
#: change and everything it touches (`repo` — the pre-#165 posture).
DEFAULT_REVIEWER_SCOPE = "diff"
#: How far back a next-door hint may be drawn from, in days, and `0` to send none
#: (#508). Seven, matching the board's own default, because the signal this carries
#: decays fast: the measured case is a defect shape confirmed in one PR and shipped
#: in another ONE HOUR later, and a confirmed finding from six weeks ago in a file
#: that has since been rewritten is noise wearing the same clothes.
#:
#: A dial rather than a constant because the block costs prompt budget on every
#: round of every PR, and the seat it costs most is the one that cannot read a
#: prompt off stdin. `0` is the whole off switch: no board call, no slot fill, and a
#: prompt byte-identical to the pre-#508 one.
DEFAULT_NEXT_DOOR_DAYS = 7
#: The widest window `GET /review/next-door` will accept, mirrored here so the dial
#: cannot ask for one the board refuses. Ten years, i.e. "everything this board
#: holds".
#:
#: Mirrored rather than discovered, because the alternative is worse in the one
#: direction that matters: a repo writing `next_door_days: 5000` would send
#: `days=5000`, the board would answer **HTTP 422**, and the round would get a note
#: and no hints — the operator having asked for a WIDER window and silently
#: received none. A duplicated constant that drifts costs a note; the version
#: without it costs the feature.
NEXT_DOOR_DAYS_MAX = 3650
REVIEWER_SCOPES = ("diff", "repo")
#: May a fixer answer "real, and not this change's job"? See `harness_rules`.
DEFAULT_FIXER_MAY_DEFER = True
#: Which deferrals get a GitHub ISSUE as well as their board row (#482). Every
#: deferral is recorded either way — this decides only whether a second copy is
#: opened on a human's tracker.
#:
#: **`shape` since 2026-08-30 (#620), and it is not a floor.** The question is no
#: longer how severe the deferral is but what shape the TICKET would be, because
#: severity is a property of a finding and batchness is a property of the ticket —
#: so a cut anywhere on P1..P4 files some batches and blocks some single items,
#: which is backwards. The count that ended the severity cut: twenty open issues on
#: this repo were panel deferred-finding exhaust carrying 345 findings, every one of
#: them a BATCH, and not one had ever been closed. The bands still work and are the
#: documented way back; `harness_rules` carries the measurement and the argument.
DEFAULT_FILE_DEFERRAL_ISSUES = "shape"
#: The three WORDS this dial takes beside the P1..P4 bands, none of which a band can
#: spell. `shape` is the rule above. `always` is the pre-#482 behaviour (an issue for
#: every deferral) and `never` files none at all — spelled as words rather than as
#: `P4`/`P0` because a floor "below P4" has no band to name and `P0` is deliberately
#: not a severity this panel has (see `SEVERITIES`).
#:
#: Two tuples and not one, mirroring `harness_rules._DEFERRAL_GATE_WORDS`: the ends
#: are the off and on extremes, `shape` is a policy, and it is the JOINED tuple every
#: reader here checks against — so a word added to either reaches all of them.
DEFERRAL_ISSUES_SHAPE = "shape"
DEFERRAL_ISSUES_ALWAYS = "always"
DEFERRAL_ISSUES_NEVER = "never"
DEFERRAL_ISSUE_ENDS = (DEFERRAL_ISSUES_ALWAYS, DEFERRAL_ISSUES_NEVER)
DEFERRAL_ISSUE_WORDS = (DEFERRAL_ISSUES_SHAPE,) + DEFERRAL_ISSUE_ENDS
#: The three shapes a deferral can have under `shape`, and the two of them that earn
#: an issue. A CATEGORY is one standing item for a recurring class ("the ingest
#: layer's error paths are untested"), which a human can work as a batch. An ITEM is
#: one named defect, decision owed or piece of complexity with real substance behind
#: it, and it earns an issue whatever severity it carries. A BATCH is a round's
#: leftovers swept into one ticket — board rows and never an issue, whatever its
#: severity mix, a P1 in the pile included: twenty P3s in one issue is not a
#: deferral, it is a transfer of the problem to a human.
#:
#: **AN UNCLASSIFIED DEFERRAL IS A BATCH**, which is where this parts company with
#: every band above it and is the whole direction of the rule. Under a band an
#: unreadable severity FILES the issue, because the cost of one issue nobody needed
#: is a line on a tracker. Here that cost is the failure — a ticket nobody reads is
#: what the twenty were — so the safe direction inverts and the answer that cannot
#: mint one is the default. Membership is tested rather than batchness, so every
#: spelling this panel does not recognise arrives at it without a special case.
DEFERRAL_SHAPE_CATEGORY = "category"
DEFERRAL_SHAPE_ITEM = "item"
DEFERRAL_SHAPE_BATCH = "batch"
DEFERRAL_SHAPES = (DEFERRAL_SHAPE_CATEGORY, DEFERRAL_SHAPE_ITEM,
                   DEFERRAL_SHAPE_BATCH)
DEFERRAL_ISSUE_SHAPES = (DEFERRAL_SHAPE_CATEGORY, DEFERRAL_SHAPE_ITEM)
#: Off, because the artefact it needs is not built (#92, #114). See `harness_rules`.
DEFAULT_REQUIRE_FAILING_TEST = False
#: Lines an integration merge may put into a PR's OWN files and still leave the
#: round that ran before it standing as a review of this PR's change (#278). The
#: measurement is `diff(the commit the round read, the merge result)` restricted to
#: the files the PR itself touches; at or under this many changed lines the merge is
#: DISTANT and the earlier round stands, past it the merge is INVOLVED and its
#: resolution is unreviewed work. `None` switches the reading off, which is the
#: pre-#278 behaviour where any head move is a review of earlier code; `0` admits
#: only a resolution that is empty over this PR's files. `harness_rules` carries the
#: argument for the number.
DEFAULT_DISTANT_MERGE_LINES = 20

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
LLM_REVIEWERS = ("claude", "codex", "antigravity", "pi", "grok")
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
# Reviewer name -> the model used when its config says nothing, where that is not
# simply "whatever the CLI defaults to". Only claude has one: its CLI's own
# default is the account's top model, which is the wrong seat to spend by
# accident, so the panel pins `sonnet` and the others send no --model at all.
# One map because the round and the ask both resolve models and used to spell
# this exception inline, in two places, as a second line that rebuilt one entry
# of the dict just built above it.
SEAT_MODEL_DEFAULTS = {"claude": "sonnet"}

#: The reply contract, shared VERBATIM by every prompt that asks a seat for
#: findings — the review and the move manifest (#138). Shared rather than copied
#: because :data:`SCHEMA_ECHOES` identifies a prompt's own example by comparing a
#: reply against it: two prompts with two hand-kept copies of this block are one
#: edit away from a manifest run in which the example parses as a finding nobody
#: made. One string means the echo detection covers both by construction.
#:
#: It ends with the material itself, so both prompts take the same `.format` keys
#: (`ci`, `code`, `n`, `repo`, `base`, `diff`) and a caller can swap one for the
#: other without knowing which it holds. `{code}` arrived in v2.51 and is in HERE
#: rather than in each prompt for the same reason as the rest: a slot added to one
#: template and forgotten in the other is a `KeyError` at `.format` on whichever
#: round happens to select the stale one.
#:
#: For that to hold the wording has to be neutral about WHAT the material is, and
#: it was not: "only if the diff is genuinely flawless" and "a file the diff does
#: not include" arrived verbatim under a prompt whose first sentence is "you are
#: deliberately NOT being given its diff", contradicting it on the one point a
#: manifest round hinges on. Forking the block would have been the wrong fix —
#: `SCHEMA_ECHOES` recognises a prompt's own example by comparing a reply against
#: it, and two hand-kept copies are one edit away from a manifest run in which the
#: example parses as a finding nobody made — so it says "the material below"
#: instead, which is true of a diff and of a manifest alike.
_FINDINGS_ENVELOPE = """Return ONLY a JSON object (no prose):
  {{"findings": [{{"severity": "P1|P2|P3|P4", "file": "path", "line": <int|null>,
                  "title": "...", "detail": "...", "needs_rereview": true|false,
                  "needs_human": false, "needs_human_class": "", "needs_human_reason": ""}}],
    "could_not_assess": ["..."]}}
An empty `findings` array only if the material below is genuinely flawless.

The last two keys are OBSERVATIONS about your own pass, not predictions. Do NOT forecast
whether another review will be needed — you cannot observe findings you have not made.

- `could_not_assess`: things in scope you could not judge from what you were given — a file
  the material below does not include, a runtime behaviour, a schema you cannot see, a caller
  you cannot check. One short phrase each; `[]` if you could genuinely assess everything.
  "I found nothing" and "I could not tell" are different answers and only you know which
  this was.
- `needs_rereview` (per finding): true when fixing it takes a STRUCTURAL change whose
  RESULT should be read again — the fix can create new interactions the current diff does
  not contain. False for a local edit whose correctness is evident from the fix itself.
- `needs_human` (per finding): true only when NO REVIEWER OF ANY KIND could settle this
  from a diff — not "I lacked context", which is `could_not_assess` and a wider scope or a
  grep would close. This is "no context would close it". When true you MUST give both:
  `needs_human_class`, one of decision (which of these, or whether at all — product,
  architecture, policy) · taste (is this the right name, the right sentence, the right
  shape) · ui (does it actually look and behave right on a real screen) · environment
  (does it work on the box it has to work on) · auth (does the credential path actually
  work, end to end) · chore (nobody has to judge it — you can state the remedy exactly and
  something here is simply not permitted to perform it) · other (say which in the reason);
  and `needs_human_reason`, one line
  saying what the person has to answer. A flag with no reason is discarded, and a flag is
  a way OUT of work — reaching for it to end a review you find tedious is counted per seat.

{ci}
{code}
PR #{n} ({repo}), base={base}:
{diff}
"""

#: Where `reviewer_scope` lands in the reviewer's brief. Literal tokens swapped with
#: `str.replace`, for the reason :data:`JUDGE_CODE_SLOT` gives for being one: this
#: template is rendered by `.format()` in `panel.py`, so a `{}` field would have to
#: be passed by every caller of both templates and would make every stray brace in
#: the substituted prose a KeyError. A token nothing else can produce is inert
#: until it is deliberately swapped — which is also what lets `SCHEMA_ECHOES` keep
#: reading the raw template.
REVIEWER_SCOPE_SLOT = "<<<REVIEWER_SCOPE>>>"
#: The same setting, in the one review DIMENSION whose wording it changes. Two slots
#: rather than one, because the paragraph and the bullet are read at different
#: moments and a bullet that still says "should change to stay consistent" under a
#: paragraph saying the opposite is the contradiction a model resolves whichever way
#: it likes.
RELATED_CODE_SLOT = "<<<RELATED_CODE>>>"

#: Where #508's next-door hints land in the reviewer's brief. A literal token
#: swapped with `str.replace`, for the reason :data:`JUDGE_CODE_SLOT` is one and
#: then some: `REVIEW_PROMPT` is rendered by `.format()` in `panel.py`, and this
#: block is built from **model-authored finding titles**, so a `{}` field would
#: turn every stray brace a reviewer ever wrote into a `KeyError` on a round that
#: has nothing to do with it. The swap therefore happens AFTER the `.format`, the
#: way `panel_rounds` swaps :data:`JUDGE_CODE_SLOT` after rendering the judge —
#: the token survives `.format` untouched because it contains no braces.
#:
#: Swapped for the empty string whenever there is nothing next door, which is the
#: common case, so a round with no hints sends a prompt BYTE-IDENTICAL to the one
#: it has always sent. Same discipline as :data:`JUDGE_RECURRENCE_SLOT`, and it
#: exists so that comparing two rounds is not also comparing two prompts.
#:
#: It sits on a line of its own and the brief supplies its own trailing newline,
#: so an empty fill leaves no blank paragraph behind.
NEXT_DOOR_SLOT = "<<<NEXT_DOOR>>>"

#: The heading of a rendered next-door block, and the sentence that keeps it a
#: hint. Split out as a constant because two things must agree on it: the
#: renderer, and the test that asserts a hint cannot be reported unaltered.
NEXT_DOOR_HEADING = "CONFIRMED NEXT DOOR — context, not findings"

#: The instruction that asks for the one property #508 wants kept: *a hint cannot
#: become a finding on its own*.
#:
#: **It is an instruction and not a mechanism, and the difference is worth saying
#: plainly** — an earlier draft of this comment called it "the enforcement", which
#: is the exact substitution #183 is about. Nothing downstream checks that a
#: finding cites a line in this diff, carries evidence independent of the hint, or
#: differs from the text the seat was shown. A seat that copies a hint back
#: produces a finding nothing here can tell from a found one. What this paragraph
#: buys is that the instruction is at least present, unambiguous and adjacent to
#: the list; what it does not buy is any assurance that it was followed.
#:
#: :func:`_one_line` is the part that IS mechanical, and it is deliberately narrow:
#: it removes the structural attack (a hint forging a bullet or occupying a line of
#: its own), not the semantic one.
#:
#: The failure it guards against is specific and cheap to fall into: a reviewer
#: handed "this was confirmed an hour ago in this file" reports it back as its
#: own finding without checking, the judge confirms it, and the next round's
#: hints include it. The chain then eats its own tail and a seat is rewarded for
#: repeating what it was told. So the block says, in order, what the lines are,
#: what they are not, and what the seat must do before any of them may appear in
#: its reply.
_NEXT_DOOR_BRIEF = """{heading}. These defects were confirmed by a panel on OTHER pull
requests in this repository, recently, in files THIS diff also touches. They are given to you
because a defect shape that just landed next door is the one most likely to be in front of you
and the least likely to be noticed — an agent copying an ordering out of a shared helper ships
the same bug in the same file an hour later, and that is a real measurement on this repo, not a
hypothetical.

They are NOT findings about this diff, and NOT a checklist to report back. Nobody has looked for
any of them here. Each one may be irrelevant, already handled, or about code this change does not
contain.

**Report one ONLY if you find it yourself in the material below, and describe it as you found it
here — the file and line in THIS diff.** Never cite a line below as evidence, never report one
because it is listed, and if the same shape is genuinely absent from this change, say nothing
about it at all. A finding that exists only because it was listed here is a false positive with a
citation, and it is worse than a missed defect: it survives review.

{lines}

"""

REVIEW_PROMPT = """You are reviewing a pull request diff to the same exhaustive standard as a
senior reviewer whose bar is "nothing left to improve". Report EVERYTHING you spot, across every
dimension below — do NOT self-censor a finding because it seems "minor" or "just style". A later
master judge filters false positives; your job is breadth, not triage.

<<<REVIEWER_SCOPE>>>

<<<NEXT_DOOR>>>Review for:
- Correctness: logic bugs, off-by-ones, race conditions, boundary conditions, null/None handling
- Security: injection, auth bypass, secrets in code, path traversal, SSRF, unsafe deserialization
- Error handling: swallowed errors, missing validation, silent failures, unhelpful messages
- Concurrency: async pitfalls, missing awaits, shared mutable state, transaction isolation
- Performance: N+1 queries, unbounded iterations, missing indexes, unnecessary allocations
- Test coverage: new code paths, bug fixes, or edge cases visible in the diff that lack a test
- Load-bearing tests: a test in the diff that is PRESENT but would not have failed against the
  defect it names — a fixture whose ordering or inputs happen to avoid the bug, an assertion that
  cannot fail, a mock that satisfies itself. Absence of a test is the easy half; a passing
  assertion that the bug is gone is worse than no test, because it keeps passing when it returns
- Documentation: behaviour changes that leave CLAUDE.md, docs, README, or docstrings stale
- Related code: <<<RELATED_CODE>>>
- Craft: naming, complexity, dead code, redundant conditions, project-convention/style breaks, DRY

Severity: P1 blocks merge (correctness/security) · P2 important (error handling, test gaps,
logic flaws) · P3 should fix (style, naming, simplifications) · P4 polish (minor consistency).
Report all of them.

""" + _FINDINGS_ENVELOPE

#: `reviewer_scope` -> (the scope paragraph, the Related-code bullet's tail).
#:
#: `repo` is the pre-#165 text, kept verbatim rather than paraphrased so that
#: switching the setting back really does restore the prompt this panel was measured
#: on. `diff` is the default, and what it changes is where an out-of-scope
#: observation LANDS, not how hard anyone looks: every dimension below stays in the
#: prompt and a seat holding the tree (`reviewer_code_access`) still reads the
#: callers to judge the change.
#:
#: One honest cost, recorded because nothing else will say it: the `diff` text routes
#: a serious out-of-scope observation into `could_not_assess`, which is a coverage
#: channel, and for a seat that CAN read the code a declared gap costs the round its
#: confidence (`coverage_veto`). It is bounded to "serious enough that somebody
#: should know", so it is rare by construction, and it errs towards a round that
#: does not claim convergence — the safe direction. Giving observations a channel of
#: their own means a new key in the reply envelope, the parser, the judge listing and
#: the payload, and that is #165's work and not this dial's.
_SCOPE_BRIEF = {
    "diff": ("""WHAT COUNTS AS A FINDING HERE. A finding is a defect in THE CHANGE UNDER
REVIEW — the lines this diff adds, removes or touches, and the seams where they meet the
code that was already there. Read as widely as you need to in order to judge those lines;
file findings only about them. A defect elsewhere that this change neither caused nor made
worse is real, and it is not this review's answer: if it is serious enough that somebody
should know, say it in ONE `could_not_assess` phrase beginning "outside the change:", and
otherwise leave it.

A fix round is briefed from your findings, so a finding outside the change is an instruction
to GROW the change — and the fix pass is where 63.7% of this loop's next-round findings came
from (#165). Breadth across the dimensions below is still the job; breadth across the
repository is not.""",
             "callers, siblings, or parallel implementations this change BREAKS or leaves "
             "inconsistent with itself"),
    "repo": ("""WHAT COUNTS AS A FINDING HERE. Anything you can see. The marginal cost of
completeness is near zero, so related code — callers, siblings, parallel implementations —
is in scope and gets made consistent: search the codebase, don't just review the diff.""",
             "callers, siblings, or parallel implementations that should change to stay "
             "consistent"),
}


#: `repo` scope for a seat with no tools to search with. The rule is the same —
#: anything you can see — without the one instruction that seat cannot follow.
#: Before #458 it was handed the searching text with an empty code slot: wrong, but
#: not self-contradictory. With NO_TOOLS_BRIEF in the same prompt it would be
#: exactly the contradiction RELATED_CODE_SLOT is split in two to avoid, "a model
#: resolves it whichever way it likes" — and on antigravity the way it likes is
#: fatal.
_REPO_SCOPE_NO_TOOLS = """WHAT COUNTS AS A FINDING HERE. Anything you can see. The marginal cost
of completeness is near zero, so related code — callers, siblings, parallel implementations — is
in scope wherever the diff puts it in front of you. You cannot go and find the rest: what you
would have searched for is a `could_not_assess` entry naming what you would have opened."""


def reviewer_brief(scope: str = DEFAULT_REVIEWER_SCOPE, reads_code: bool = True) -> str:
    """:data:`REVIEW_PROMPT` with `review_panel.reviewer_scope` filled in.

    A function rather than two constants because everything else about the two
    briefs is identical, and a second full template is a second place to forget an
    edit. An unknown scope reads as the default — the value is validated where it is
    read (:func:`panel_seats.reviewer_scope`), and this is the last line of defence
    rather than the one that reports."""
    para, related = _SCOPE_BRIEF.get(scope) or _SCOPE_BRIEF[DEFAULT_REVIEWER_SCOPE]
    if not reads_code and scope == "repo":
        para = _REPO_SCOPE_NO_TOOLS
    return (REVIEW_PROMPT.replace(REVIEWER_SCOPE_SLOT, para)
            .replace(RELATED_CODE_SLOT, related))


#: How many next-door hints a round will actually send, whatever the board is
#: willing to serve. `GET /review/next-door` caps at 20 and takes a `limit`; this
#: is smaller because the two caps answer different questions. The board's bounds
#: a *response*; this bounds a **reviewer's attention**, which is the scarce thing
#: — every line here is a line not spent on the diff, and #508 asks for "a handful
#: of lines in a prompt, not a second review". Eight is a handful.
NEXT_DOOR_MAX = 8


#: The longest a hint's title may be in a prompt, and the longest its detail.
#: The board caps `detail` too; this caps both again for `NEXT_DOOR_MAX`'s reason —
#: the far cap bounds a response and this one bounds a reviewer's attention, and a
#: caller trusting only the far one is trusting a number it does not control.
NEXT_DOOR_TITLE_CHARS = 200
NEXT_DOOR_DETAIL_CHARS = 400

#: Anything that could end a hint's line or start a new one. Collapsed to a single
#: space by :func:`_one_line`.
_HINT_BREAK = re.compile(r"\s+")
#: Control characters, which no finding title has a use for and which can move a
#: terminal's cursor or a model's attention. Deleted rather than escaped.
_HINT_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _one_line(text: object, cap: int) -> str:
    """Untrusted text flattened to ONE line and cut to `cap`.

    **The thing this prevents is not a crash.** A hint's title and detail are
    written by the reviewers of OTHER pull requests — model output, quoted into a
    prompt that instructs a model. Interpolated raw, a title carrying newlines
    escapes its bullet and becomes free text at the same indent as the brief above
    it, so it can:

    * emit a line of its own that reads as an instruction ("IGNORE THE ABOVE …"),
      arriving inside a block whose whole purpose is to be read as instruction;
    * forge further `- P1 file:line — …` bullets **indistinguishable from the real
      ones**, since the renderer is the only thing that knows how many there were.

    That is prompt injection with a short path: any seat on any PR can write the
    payload into a finding title, the judge confirms the finding for unrelated
    reasons, and it is quoted at every PR touching that file for the next week. It
    needs no attacker either — a legitimate multi-line detail mangles the block on
    its own.

    So the text is flattened, not escaped: a hint is one line by construction, and
    a title that wanted two was already wrong. Control characters go entirely.
    Truncation says so, for `_cut_detail`'s reason on the board side — a sentence
    ending mid-clause reads to a model as the sentence.

    Not a claim to have solved prompt injection. It removes the structural half —
    a hint can no longer forge a bullet or occupy a line of its own — and what
    remains is one bounded, clearly-attributed span of prose inside a bullet, which
    is the same exposure the diff itself already carries.
    """
    flat = _HINT_BREAK.sub(" ", _HINT_CONTROL.sub("", str(text or ""))).strip()
    if len(flat) <= cap:
        return flat
    return flat[:cap].rstrip() + "…"


def _hint_line(h: dict) -> str:
    """One hint as one line, with the evidence to check it and nothing else.

    Deliberately terse and deliberately complete: the PR number and the age are
    what let a reviewer decide the line is stale or irrelevant WITHOUT taking it
    on trust, and a hint a reviewer cannot dismiss on its own evidence is one it
    will report to be safe.
    """
    # The path is flattened with everything else: it is a string off the wire, and
    # "no path has a newline in it" is an assumption rather than a guarantee.
    where = _one_line(h.get("file"), NEXT_DOOR_TITLE_CHARS) or "?"
    line_no = h.get("line")
    # `isinstance` rather than truthiness: a line number arriving as "3\n- P1 …"
    # would otherwise be formatted straight into the bullet, which is the same
    # escape by a quieter door.
    if isinstance(line_no, int) and not isinstance(line_no, bool) and line_no > 0:
        where = f"{where}:{line_no}"
    age = h.get("age_hours")
    when = f"{age:g}h ago" if isinstance(age, int | float) else "recently"
    # `fixed` is worth saying and the rest are not: it means somebody confirmed
    # this AND acted on it, which is the strongest form the hint takes. A bare
    # `deferred` or `superseded` would read as a verdict on THIS diff, which is
    # the one thing the block must never imply.
    fixed = " and fixed there" if h.get("outcome") == "fixed" else ""
    # A severity outside the vocabulary is not echoed. `SEVERITIES` is a closed set
    # and anything else is either drift or a payload; `P?` says "the board sent
    # something this does not recognise" without quoting it.
    sev = h.get("severity") if h.get("severity") in SEVERITIES else "P?"
    pr = h.get("pr")
    pr_txt = pr if isinstance(pr, int) and not isinstance(pr, bool) else "?"
    title = _one_line(h.get("title"), NEXT_DOOR_TITLE_CHARS) or "(untitled)"
    line = (f"- {sev} {where} — {title} "
            f"[confirmed on PR #{pr_txt} {when}{fixed}]")
    detail = _one_line(h.get("detail"), NEXT_DOOR_DETAIL_CHARS)
    if detail:
        line += f"\n    {detail}"
    return line


def next_door_brief(hints: list[dict]) -> str:
    """#508's block for :data:`NEXT_DOOR_SLOT`, or `""` when there is nothing.

    The empty return is the important one and is not a degenerate case: most
    rounds have no confirmed finding next door, and on those the slot is swapped
    for nothing at all, leaving the reviewer prompt byte-identical to the one this
    panel has always sent. A block saying "no recent findings nearby" would be a
    new sentence on every round in exchange for no information — and it would make
    every round's prompt differ from every archived round's.

    Capped at :data:`NEXT_DOOR_MAX` here as well as at the board, because the two
    caps are different promises and a caller that trusted only the far one would
    be trusting a number it does not control.
    """
    rows = [h for h in hints if isinstance(h, dict)][:NEXT_DOOR_MAX]
    if not rows:
        return ""
    return _NEXT_DOOR_BRIEF.format(heading=NEXT_DOOR_HEADING,
                                   lines="\n".join(_hint_line(h) for h in rows))


def next_door_note(hints: list[dict]) -> str:
    """What this round actually SHOWED its seats, for the record (#508).

    :class:`Dials` records `next_door_days` — the window the round asked through —
    and that is the setting, not the answer. Two rounds at the same window can be
    handed different hints an hour apart, and the block that carries them is the
    one thing in the reviewer prompt that varies between rounds of the same PR.
    #508 leans on the prompt being byte-identical "so that comparing two rounds is
    not also comparing two prompts"; the moment there ARE hints that stops being
    true, and without this line nothing in the payload says by how much or from
    where. So the round records the count and the rival PRs it quoted — enough to
    go and read the same findings back, and cheap enough to sit in `config_notes`.

    Only the two facts that are structurally safe to print, and that is a
    deliberate omission rather than an oversight: the note lands in `config_notes`,
    which `--post` publishes as a PUBLIC pull-request comment, and a hint's title,
    file and key are all model-authored text off the wire. PR numbers are `int` or
    they are not repeated at all, so no flattening is needed and none is relied on.
    The titles are in the prompt, which is where a reader looking for them is.

    `""` when there is nothing to say, on :func:`next_door_brief`'s rule: a round
    with no hints must add no line, or the note is on every round of every PR and
    is the kind that gets trained away.
    """
    rows = [h for h in hints if isinstance(h, dict)][:NEXT_DOOR_MAX]
    if not rows:
        return ""
    # `sorted(set(...))` rather than payload order: the note is read by a person
    # comparing two rounds, and a list whose order tracks recency ranking would
    # differ between rounds that quoted the same PRs.
    prs = sorted({h["pr"] for h in rows
                  if isinstance(h.get("pr"), int) and not isinstance(h["pr"], bool)})
    where = (" from " + ", ".join(f"#{n}" for n in prs)) if prs else ""
    plural = "" if len(rows) == 1 else "s"
    return (f"next-door context: {len(rows)} confirmed finding{plural}{where} "
            f"shown to this round's reviewers (#508)")


MOVE_MANIFEST_PROMPT = """You are reviewing a MOVE, and you are deliberately NOT being given its
diff. Read the brief below before the manifest — the question you are being asked is not the one
a diff review asks, and answering the other one would waste the round.

This change is move-shaped: its added lines are a near-permutation of its deleted ones, measured
mechanically, so almost every line in the diff appears TWICE — once as a delete and once as an
add. It is a rename, a file split, a relocation, or several of those. The bulk of that text is
code nobody changed, and it is code that is already in the base branch and was already reviewed
when it landed there. A finding about it is a finding about the base branch: it costs the cycle a
fixer briefed to resolve it against a refactor, and it is worth less than nothing.

So do not review the moved code. Review the MOVE. Four questions, and they are the whole job:

1. **What did not survive.** Lines deleted and not re-added anywhere are listed below. For each,
   is it a deliberate deletion or a casualty of the move? A dropped guard clause, an `except`
   arm, a decorator, a default argument or a `del`/cleanup line is the failure this section
   exists to catch. Say which you cannot tell from the manifest alone.
2. **What changed besides moving.** Lines added and not deleted anywhere are listed below: this
   is the ONLY genuinely new code in the change, and it is where a content review belongs. Read
   it as closely as you would read a small PR. A move that quietly rewrites logic while nobody
   is reading is the thing a manifest review is most likely to miss.
3. **Duplicated definitions.** A move that keeps BOTH copies of a definition is a clean merge, a
   green test run and a silent bug — the later binding wins, the earlier one is dead, and the
   dead one is the one anybody reading the old file will find. Names this change ADDS in more
   than one place are listed, with the files each copy landed in; each one is a finding unless
   there is a reason it is not. That is only HALF of the trap, and the other half **cannot be
   seen from a diff at all**: an original left exactly where it was, in a file this change never
   touches, appears as neither an added nor a deleted line, so nothing below can list it. If a
   name that moved looks like it may still exist at its old address, that belongs in
   `could_not_assess` — checking it needs the branch checked out, and nobody here has it.
4. **What the manifest cannot tell you.** Say it. Test counts before and after, whether a module
   now reaches backward into another, and whether the destination files import what they now
   need are all facts about a move that the diff cannot answer — they need the branch checked
   out. Put each one in `could_not_assess` rather than assuming it is fine, and rather than
   guessing.

Do NOT report: relocated code, its style, its naming, or anything you would only have seen by
reading the moved text. There is none of it here to read, and inventing findings about it from
the file names in the manifest is the failure mode this prompt replaces.

Severity: P1 blocks merge (something was lost, or a definition is duplicated) · P2 important (new
logic smuggled into a move, an unverifiable claim nobody has checked) · P3 should fix · P4 polish.
Report all of them.

""" + _FINDINGS_ENVELOPE

#: The judge prompt's placeholder for the code-access brief. A literal token
#: replaced at call time rather than a `{}` format field, because `JUDGE_PROMPT`
#: is rendered by `.format()` in one place and its findings listing is built from
#: model-authored text — adding a format field means every caller must pass it and
#: every stray brace in a finding title becomes a KeyError. A token that nothing
#: else in the prompt can produce is inert until it is deliberately swapped.
JUDGE_CODE_SLOT = "<<<CODE_ACCESS_BRIEF>>>"

#: The judge prompt's placeholder for #67's recurrence question. A second literal
#: token, for the reason :data:`JUDGE_CODE_SLOT` is one, and swapped for the empty
#: string on every round that has no earlier round to compare against — so a round
#: 1 judge prompt stays BYTE-IDENTICAL to the one it has always been. That is the
#: same discipline `PR_SCOPE_HEADER` keeps for whole-PR scope, and it exists so a
#: comparison between rounds is not also a comparison between two prompts.
#:
#: It sits IMMEDIATELY before ``{diff}`` with no newline of its own, and the
#: brief supplies its own trailing one. Given a line to itself the empty fill
#: still left a blank line behind it, so the round-1 prompt differed from the
#: pre-#67 one by a single ``\n`` — byte-identity claimed in a docstring and not
#: actually held, which the test asserting it was (wrongly) normalising away.
JUDGE_RECURRENCE_SLOT = "<<<RECURRENCE_BRIEF>>>"

#: What the judge may answer #67's question with, per finding. NULL — the field
#: absent — is the fourth state and the commonest: the question was not put,
#: because there was no earlier round to put it about.
#:
#: **Not #84's premise register**, which is the other thing called a premise in
#: this loop and is nearly its opposite. That one holds what a FIXER declared it
#: was about to fix on, and brakes when the same declaration comes round twice.
#: This is what a JUDGE says about a finding, and brakes nothing. #67's own record
#: of PR #88 is the argument for having both: the agent that wrote round 1's fix
#: wrote round 2's regression of the same shape, in the same commit as a docstring
#: stating the invariant it broke — "the strongest argument yet that the signal
#: cannot be self-reported". A declaration and an adjudication are two different
#: witnesses and the disagreement between them is the interesting row.
#:
#: ``unclear`` is a real answer and the one this must keep making available.
#: Without it a judge with no view either way picks whichever of the other two
#: reads as safer, and the measurement fills up with confident noise on exactly
#: the findings that most needed a shrug.
PREMISE_VERDICTS = ("invalidates", "separate", "unclear")

#: What a judge is told when there IS an earlier round, and what it is asked on
#: top of its ordinary ruling.
#:
#: One extra key on a verdict it is already writing, so this costs no second model
#: call — the same trade `coverage_note` made. It is asked of the judge rather
#: than computed because the mechanical half cannot answer it: `_recurrence` can
#: see that a fixer was working where a finding now stands, and cannot see whether
#: the finding says that fixer's ASSUMPTION was wrong. That distinction is #67's
#: whole point, and the judge is the only party in the round already holding both
#: the earlier round's complaints and the commit that answered them.
#:
#: **It is the half of #67 that can actually work**, and the measurement says so
#: rather than the design claiming it. :func:`panel_scope._recurrence`'s replay
#: over 36 rounds of this board's history found the mechanical site test firing on
#: about four new findings in five, at the same rate on the cycles #67 calls
#: circling as on the ones it does not — because a round past the first is reading
#: the fix commit, so "at the fix's site" is the ordinary case. Position saturates;
#: only something that can see what a finding SAYS can separate one premise
#: patched twice from two bugs in a busy file. That is this.
#:
#: The brief spends most of its length pushing AWAY from `invalidates`, and that
#: is deliberate. The failure that would make this worthless is a judge that reads
#: "was the last fix wrong?" as an invitation and says yes; a second bug in a busy
#: file is the common case and `separate` is the answer that should be dull to
#: give. #67's own limit — recurrence is not always circling — is the sentence
#: this brief exists to enforce.
RECURRENCE_BRIEF = """ONE EXTRA QUESTION, and it is not about the diff (#67).

A fixer worked on this PR between the previous round and this one. The findings that
round asked it to fix are listed below. For each verdict you return, add one more key:

  "premise": "invalidates" | "separate" | "unclear"

The question is NOT "is this finding near that fix". It is:

  Does this finding show that the fix which preceded it was built on a WRONG ASSUMPTION —
  so that patching where this finding points would keep that assumption standing — or is it
  simply a different defect?

- "separate" — a different defect. THIS IS THE DEFAULT AND THE COMMON CASE. A second bug in
  a file somebody just edited is a second bug. Busy code attracts findings. Say "separate"
  unless you can name the assumption.
- "invalidates" — you can state, in your `reason`, the assumption the earlier fix rests on
  and the sentence in this finding that contradicts it. If you cannot name both, it is not
  this.
- "unclear" — you cannot tell from what you were given. A real answer, and the right one
  whenever you would otherwise be guessing.

Nothing is decided by your answer. It is recorded and counted, no round is stopped by it,
and no fix is skipped because of it — so there is no reason to hedge toward either side.
Leave the key off entirely if there is nothing to say.

Findings the previous round ({prior_round}) asked the fixer to fix:
{prior_findings}
"""

#: What a seat that was handed the PR's tree is told about it. Empty for every seat
#: that was not — see SEAT_READS_CODE — so those seats' prompts are unchanged.
#:
#: A seat has to be TOLD, or the access buys nothing: the prompt's whole frame is
#: "here is a diff", the `could_not_assess` instruction explicitly offers "a file the
#: diff does not include" as a valid answer, and a reviewer following those
#: instructions faithfully will declare a gap it could have closed by opening the
#: file. Half of #113's measured cost was seats doing exactly that.
#:
#: It also states the LIMITS, and not out of politeness. A seat told it has the code
#: but not that it has no shell tries to run the tests, and a seat not told the
#: convention files were removed can read their absence as a finding ("this repo has
#: no CLAUDE.md") — a wrong finding manufactured by the fix for wrong findings.
CODE_ACCESS_BRIEF = """YOU HAVE THE CODE. Your working directory is a checkout of this PR at its head
commit — the same code the diff below was taken from. Use it. Read the callers, the
siblings, the tests, the config, the migration the diff refers to but does not contain.
A question you can answer by opening a file is not a coverage gap, and reporting it as
one is the failure this access exists to remove: check before you declare, and before
you raise a conditional finding about code you cannot see, look at it.

Three limits, so you do not spend the round discovering them. None of the three is a
coverage gap and none is a finding — they are how this checkout is built:
- You have Read, Grep and Glob. You have NO shell and cannot run anything — not the
  tests, not the linter, not git. A behaviour you can only establish by RUNNING it is
  still a legitimate `could_not_assess` entry; "I could not run git" is not.
- There is NO git history. This is the tree as it stands at one commit, not a clone:
  no commits, no branches, no blame. Do not report that, and do not conclude from it
  that the diff was reverted or that the file is untracked.
- Vendor instruction files (CLAUDE.md, AGENTS.md, .claude/ and the like) have been
  REMOVED from this checkout on purpose, so that a PR cannot instruct its own reviewer.
  Their absence is not a finding and says nothing about the real repository.

`could_not_assess` now means what you could not resolve WITH the code in front of you.
"""

#: The other half of the same slot, and the reason it is prose. Every seat wants to go
#: looking for the code — `codex_args` measured it at five runs in seven — and the answer
#: has been a flag per vendor: `--no-tools` for pi, two `-c` overrides for codex. There is
#: no such flag for `agy`, whose own help offers only `--dangerously-skip-permissions` in
#: the opposite direction. So this seat's `--no-tools` has to be a sentence, and the others
#: get it too because the SITUATION is the same for every code-blind seat.
#:
#: The situation, not the mechanism — the wording had to be widened for that (#459).
#: "An attempt is denied" is true of agy and of pi under `--no-tools`, and false of a
#: code-blind claude: `claude_args` pins `--allowedTools` only when the seat reads code,
#: so the downgrade path, `reviewer_code_access` off and both judge paths all dispatch a
#: claude holding its full default toolset — Bash included, as that function's own
#: measurement records. What is true of all of them is that the cwd is empty and there is
#: nothing to find, which is the part the instruction rests on.
#:
#: IT IS NOT AN OPTIMISATION HERE. On codex the reach cost wall-clock; on antigravity a
#: denied tool ENDS THE PROCESS — `permission check failed … user denied permission`, exit
#: 1, no reply — so the seat that cannot be given the flag is also the one the reach kills
#: (#458). Measured on the failing prompt, this text is the difference between exit 1 and a
#: findings array that names the gap instead of hunting for it.
#: The half that is true of every prompt this panel sends a diff-only seat, kept
#: apart from the review-path tail below so `ASK_PROMPT` can carry the same warning
#: without inheriting a vocabulary its answer does not use — an ask returns
#: holds/fails/cannot tell and has no `could_not_assess` to offer (#459).
NO_TOOLS_RULE = """YOU HAVE NO TOOLS, and what you are given here is the whole of it. You cannot
run commands, read files, browse a repository or search the web to any purpose. Your working
directory is an EMPTY repository — not this project — so a tool that does run finds nothing, and
on most of the CLIs this panel uses it does not run: the attempt is denied. On at least one of
them that denial does not merely fail, it ENDS THE SESSION, and your answer is lost rather than
delivered short.

So do not go looking, and in particular do not guess at a path on the machine running this and
ask to read it."""

NO_TOOLS_BRIEF = NO_TOOLS_RULE + """

What you would have opened a file to settle is a `could_not_assess` entry, named as precisely as
you can name it — "whether X's callers pass a list" tells the next round what to fetch, "I could
not read the repo" does not. That entry is not a failure and does not count against you: it is
the one thing you can do with a question the prompt does not answer, and it is how the panel
learns what to put in the prompt next time.
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
   "coverage_rulings": [{{"declarations": [<the bracketed declaration NUMBERS this claim merges, e.g. 0, 2>],
                  "claim": "what nobody settled, in one line",
                  "resolvable_in_harness": true|false,
                  "reason": "what would settle it"}}],
   "coverage_note": "..."}}

`coverage_note` adjudicates the reviewers' own coverage declarations below — one sentence, or ""
when there is nothing to say. Where they DISAGREE (one reports clean, another says it could not
assess an area), that split is more informative than either verdict alone: say which reading you
believe and what is therefore still unread. Do not average it away, and do not turn it into a
prediction about further rounds.

`coverage_rulings` splits those same declarations into the two different things they are being
made to say. A reviewer that did not open a file it could have opened, and a reviewer that would
need a running database and a browser, both write one `could_not_assess` line; the first impugns
this round and the second is a fact about what a panel of reviewers reading a diff IS.

**One entry per CLAIM, not per declaration.** Where several reviewers say the same thing in
different words, merge them and list every declaration number in `declarations`, exactly as
`members` merges reports above. Every declaration number below belongs to exactly one entry, and
a number you leave out is read as unruled — which costs the round its confidence, so leave none
out.

`resolvable_in_harness: true` — it was answerable from the diff and the tree in front of you, and
was not answered. A file nobody opened, an import nobody checked, a caller nobody grepped for.
`reason` names the file or the command that would have settled it.

`resolvable_in_harness: false` — **no reviewer here could have settled it with what this review
has.** It needs code to actually run, a live service, a browser, a populated database, data this
checkout does not carry, or a measurement of the deployed system. `reason` names the instrument
that could settle it.

When you cannot tell, answer `true`. That is the answer that costs the round its confidence, and
a reviewer's idleness recorded as a capability limit is a review that stopped confidently on a
question nobody asked. This ruling never decides on its own that a claim is settled: a `false`
converts an unanswerable veto into a named obligation somebody has to acknowledge by hand, and
until they do it still costs the round its confidence.

Reports:
{findings}

Coverage declared by the reviewers (the bracketed number is the declaration id `coverage_rulings`
takes):
{coverage}
{ci}
<<<CODE_ACCESS_BRIEF>>>
<<<RECURRENCE_BRIEF>>>{diff}
"""

ASK_PROMPT = """You are answering ONE question about a system, as a point of order. This is NOT a
code review: do not look for defects, do not suggest improvements, and do not report anything the
question below does not ask about. A finding you make here goes nowhere.

Someone is about to build a fix on the PREMISE below. Say whether it HOLDS.

Answer from the material you are given and from nothing else.

{no_tools}

If what you were given does not settle the question, say so — "cannot tell" is a real answer here
and it is the right one whenever you would otherwise be guessing. It is never a polite way of
agreeing.

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
    #: This reporter's own declaration that no reviewer of any kind can settle
    #: the question from a diff — #279's vocabulary, carried per reporter for the
    #: reason :attr:`needs_rereview` gives and one sharper: a flag is a way OUT
    #: of work, so #67's "do not escalate to end a cycle you find tedious" is
    #: only enforceable if the rate at which each seat reaches for it is on the
    #: row it is scored by.
    needs_human: bool = False
    #: One of ``app.needs_human.NEEDS_HUMAN_CLASSES``, normalised. Empty when the
    #: seat named nothing recognisable — and an empty class REFUSES the flag
    #: rather than escalating without one (see :func:`_needs_human`), the same
    #: biconditional the database CHECK enforces one layer out.
    needs_human_class: str = ""
    #: What the person has to answer. Required by the same rule.
    needs_human_reason: str = ""


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
    #: The pinned model, and the pinned reasoning effort, this host's provider
    #: could not serve — when the seat lowered them and reviewed anyway (#215).
    #: State, for the same reason as `absent`: the report has to say what actually
    #: did the review, and deriving that from a message tail is how a record comes
    #: to claim a model that never ran. `""` = the pin was honoured.
    #:
    #: Two fields because the provider refuses them independently: on the gateway
    #: that motivated this, `gpt-5.6-luna` has no deployment AND `max` effort is an
    #: `unsupported_value`, so a seat can end up having dropped either or both.
    model_unavailable: str = ""
    effort_unsupported: str = ""
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


def sh_bytes(args: list[str], **kw) -> bytes:
    """:func:`sh` for output that is not text. Same contract, same seam.

    A separate function rather than a `text=` parameter on `sh`, because the two
    return different types and every caller of `sh` treats its result as a string.
    It exists at all so the ONE binary reader in the panel — the PR tarball — is
    interceptable where every other `gh` call already is: the suites stub
    `panel_core.sh` to answer `gh`, and a reader that reached for `subprocess`
    directly was invisible to that stub, so the whole suite started making real
    network calls and slowed from 7 seconds to 30 while still passing."""
    return subprocess.run(args, capture_output=True, check=True, **kw).stdout


def load_repo_cfg(name: str) -> dict:
    """The repo's resolved rules, or a clean exit saying the spec did not resolve.

    The panel is READ-ONLY, so an unconfigured repo is not an error at RESOLUTION —
    it resolves to the built-in defaults exactly as `epic`, `lander` and `preland`
    do, which is what lets the read-only commands run in any repo at all. Whether a
    REVIEW may run on those defaults is a separate question and a different answer:
    see `review_refusal`, which the review and ask paths call and this function
    deliberately does not.
    """
    try:
        return resolve_repo(name)
    except RepoNotFound as e:
        sys.exit(str(e))


#: The dials a REVIEW runs under. `resolve_repo` reports the layer that answered
#: for every dial in the config, including the loop schedule and the epic's model
#: ceiling; a round's artifact only wants the ones that governed the round.
_REVIEW_BLOCKS = ("review_panel.", "reviewers.")


def rules_record(cfg: dict) -> dict:
    """WHICH LAYER SUPPLIED EACH DIAL THIS ROUND RAN UNDER — #305.

    The payload already carried `review_panel`, the dials AS APPLIED. What it could
    not say is where each of them came from, and that is the half the incident
    turned on: `.harness-rules.sample` stated both floors at P2 while five rounds on
    #299 put P4 findings in `to_fix`, and nothing in any round's artifact could
    settle which of the two was describing the run. A value with no provenance is
    a value a reader has to go and guess the source of, from three files and a
    resolution order.

    So every reviewed round now records the layer beside the value — `defaults`,
    `sample`, `overlay` or `board` — plus, for a board dial, the reason somebody
    gave for moving it and when it lapses. On EVERY payload including the ones that
    reviewed nothing, unlike `review_panel` itself: a refusal or a skip did not
    apply a review policy, but it certainly resolved one, and "which rules did this
    repo have when it refused" is exactly the question a refusal raises.
    """
    return {
        "from": cfg.get("_rules_from", ""),
        "baseline": cfg.get("_rules_baseline", ""),
        "unreadable": bool(cfg.get("_rules_unreadable")),
        "dials_from": cfg.get("_dials_from", ""),
        # The board is configured on this box and did not answer, so the layers
        # below are this repo's own and not necessarily the ones in force. A
        # separate fact from `unreadable`, which is about the default branch.
        "dials_unreadable": bool(cfg.get("_dials_unreadable")),
        "dials": {path: said for path, said in (cfg.get("_dials") or {}).items()
                  if path.startswith(_REVIEW_BLOCKS)},
    }


#: How a ``harness_digest`` is computed, carried as a PREFIX on the value rather
#: than left in this docstring (#112).
#:
#: The digest's whole job is to answer "were these two rounds read by the same
#: machinery", and that answer only means anything between two values computed the
#: same way. A bare hex digest would go on comparing equal to itself after somebody
#: changed what goes INTO it — silently splitting one harness version into two
#: groups, or merging two into one — which is this issue's own bug one layer down
#: and in the direction nobody would notice. So the scheme rides on the value:
#: `loops-sha256-1:<hex>`, a consumer groups on the whole string, and a change to
#: what is digested bumps the trailing number instead of reusing it.
HARNESS_DIGEST_SCHEME = "loops-sha256-1"

#: Seconds for either `git` call behind `harness_rev`. Short on purpose: this is
#: bookkeeping on a payload nothing gates on, and a `git` that has not answered in
#: ten seconds leaves the rev null — which is the same answer an INSTALLED harness
#: gives anyway, that being the common case rather than the exotic one.
HARNESS_GIT_TIMEOUT_S = 10



def _harness_git(loops: Path, *args: str) -> str | None:
    """stdout of ``git -C <loops> <args>``, or None if it could not run or failed.

    `panel_scope._git`'s contract and its reasons, for a caller that cannot import
    it: `sh` raises on a non-zero exit, and every non-zero exit here is an ANSWER —
    "this directory is not inside a git checkout" is what an installed harness says
    and is the commonest outcome this function has. `sh` is also what the suites
    replace with a `gh` double, so routing local git through it would put these
    calls in front of a stub that knows only the forge.
    """
    try:
        out = subprocess.run(["git", "-C", str(loops), *args],
                             capture_output=True, text=True, errors="replace",
                             timeout=HARNESS_GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return out.stdout if out.returncode == 0 else None


def _harness_digest(loops: Path) -> str | None:
    """A content digest of the loop modules in ``loops``, or None if unreadable.

    **The one field that is always available and never lies about SAMENESS.** A
    rev is null on every installed harness and a path says only where the code sat;
    this says whether two rounds ran the same code, which is the question every
    r1 -> r2 comparison in this system silently assumes the answer to.

    What goes in, and each exclusion is a claim:

    * ``*.py`` in this directory and no deeper. `loops/tests/` ships with the
      package and does not run a round, so a release that only changed tests must
      not read as different machinery.
    * The SHEBANG of each file is dropped. `package.nix`'s `postFixup` runs
      `patchShebangs`, so every installed file differs from the checkout it was
      built from by its first line — counting it would give every deployed harness
      a digest that matches no checkout anywhere, which is `qb-doctor`'s
      `_same_but_for_shebang` and the 24 files it once called drift.
    * Names and lengths are hashed beside the bodies, so two files cannot be
      renamed into each other's contents or shifted across a boundary without the
      digest moving.

    ``None`` where the directory could not be read, and where it does not hold this
    very module: home-manager links some harness files in individually, and a
    ``__file__`` resolved through a flat symlink would put ``parent`` at
    ``/nix/store`` — where this would otherwise cheerfully digest a few thousand
    unrelated packages and call it a harness. A digest of the wrong directory is
    worse than no digest, because nothing downstream can tell it is wrong.
    """
    if not (loops / "panel_core.py").is_file():
        return None
    try:
        files = sorted(p for p in loops.glob("*.py") if p.is_file())
        if not files:
            return None
        h = hashlib.sha256()
        for p in files:
            body = p.read_bytes()
            if body.startswith(b"#!"):
                body = body.partition(b"\n")[2]
            h.update(f"{p.name}\0{len(body)}\0".encode())
            h.update(body)
    except OSError:
        return None
    return f"{HARNESS_DIGEST_SCHEME}:{h.hexdigest()}"


def _harness_checkout(loops: Path) -> tuple[str | None, bool | None]:
    """``(rev, dirty)`` for the checkout this harness runs from, or ``(None, None)``.

    The AUTHORITATIVE half of the identity when it answers, and it does not answer
    often: an installed harness lives in the nix store, which is not a checkout, so
    a null rev is the ordinary case rather than a failure.

    **A rev is only reported when the containing repository actually tracks this
    file.** Without that test the answer is worse than absent: `panel-review-pr.md`
    tells you to run the panel from a scratchpad copy, and a copy dropped inside
    some OTHER checkout would take that repository's HEAD and record it as the
    harness's own — a plausible 40-hex commit id, in the right column, belonging to
    the wrong repository. `ls-files --error-unmatch` is the cheapest question that
    separates "the harness's own checkout" from "some checkout the harness happens
    to be sitting in".

    ``dirty`` is scoped to exactly what :func:`_harness_digest` reads and to
    nothing else — ``:(glob)*.py``, which is the top level of this directory and
    not ``tests/`` below it — and it counts untracked files, because an untracked
    module here is in the digest and is running. The scopes agreeing is the whole
    point: ``dirty`` then means precisely "the digest above is not what that rev
    would produce", where a plain ``-- .`` would have said "somebody edited a test"
    in the same words (found by Codex).
    """
    if _harness_git(loops, "ls-files", "--error-unmatch", "panel_core.py") is None:
        return None, None
    head = _harness_git(loops, "rev-parse", "HEAD")
    if not head or not head.strip():
        return None, None
    status = _harness_git(loops, "status", "--porcelain", "--", ":(glob)*.py")
    # `status` is None only where git answered the two calls above and failed this
    # one, which leaves the rev true and its cleanliness unknown. Null, not False:
    # "nobody checked" must not read as "checked and clean" — that is the whole of
    # #112's complaint about a version field that is sometimes a lie.
    return head.strip(), None if status is None else bool(status.strip())


def harness_identity(loops: Path | None = None) -> dict:
    """WHICH HARNESS PRODUCED THIS ROUND — #112, four fields and no single answer.

    A payload described the electorate and the decision in detail and said nothing
    about the code that ran the panel. So a leaderboard aggregated across runs whose
    prompts, budget arithmetic and seat-loss behaviour differed, and — the sharp
    end — an r1 -> r2 comparison, which every stop argument in this system rests on,
    assumed both rounds were read by the same machinery with nothing in the record
    able to check it. That is not hypothetical: on 2026-08-31 six PRs changed
    `round_stop`, `converged`, the `fix_injection` accounting and `restored_lines`,
    and the deployed harness was rebuilt underneath a running session.

    **Four fields and not one, because no single one of them is true in every
    case.** `qb-doctor`'s `check_harness` reached the same conclusion from the other
    side and wrote it down: the truthful answer lives in the flake pin's rev, which
    a running harness cannot reach, so content stands in as a PROXY. This records
    both, and says which is which:

    * ``rev`` — the commit of the checkout this code is in. AUTHORITATIVE where it
      is not null: it names something you can `git show`. Null on every installed
      harness, which is most of them.
    * ``dirty`` — whether the digested directory has changes that rev does not
      carry, untracked files included. `true` is what makes a rev honest rather
      than merely present; null is "no rev, or nobody could ask".
    * ``digest`` — :data:`HARNESS_DIGEST_SCHEME` over the loop modules. A PROXY:
      it cannot name a version, and two digests being different does not say which
      is newer. It is the only field that is always there and never wrong about the
      one question that matters most — same code, or not.
    * ``path`` — the directory it all ran from. A LOCATOR, and machine-scoped: for
      a nix install it is also an exact identity of the build (the store path is a
      hash of everything that went into it), and for a scratchpad copy it is the
      only field that says the round did not come from the deployed harness at all.

    A round carries all four or the honest absence of each. A round that carried one
    field which is sometimes a lie would be worse than a round that carried nothing,
    because a reader cannot see which of the two it has.

    ``loops`` is for the tests. The real call takes no argument and returns
    :data:`_HARNESS_IDENTITY`, which was resolved AT IMPORT — see there for why the
    timing is part of the answer rather than an implementation detail.
    """
    if loops is None:
        return _HARNESS_IDENTITY
    loops = Path(loops)
    rev, dirty = _harness_checkout(loops)
    return {"rev": rev, "dirty": dirty, "digest": _harness_digest(loops),
            # The directory the other three are ABOUT, so a reader never has to
            # guess whether a digest describes the deployed harness or a copy.
            "path": str(loops)}


def _loops_dir() -> Path:
    """This module's own directory, symlinks resolved, or unresolved if it cannot be.

    Resolved because an installed harness is reached through ``~/.claude/loops``,
    which home-manager points at a store path: the symlink is what gets re-pointed
    by a rebuild, and the store path underneath it is the identity worth recording.
    Never raises — a ``resolve()`` that fails on an exotic filesystem must cost the
    identity's precision and not the round.
    """
    try:
        return Path(__file__).resolve().parent
    except OSError:
        return Path(__file__).parent


#: This process's answer, resolved AT IMPORT and never again.
#:
#: **The timing is the point, and it was wrong in the first cut** (found by Codex).
#: Computing it lazily meant computing it when `_payload_defaults()` ran, which is
#: when a round WRITES its payload — after the review. A harness rebuilt in between
#: would then be recorded as the harness that produced the round, and the rebuild
#: lands on the symlink `~/.claude/loops`, so even the resolution of `__file__`
#: would have followed it to the new store path. That is #112's own scenario, and a
#: field that reported the NEW harness for a round the OLD one produced would hide
#: precisely the event it exists to expose.
#:
#: Import is as close to "the code this process loaded" as a running program can
#: get, and it costs about 8 ms — two `git` calls and a hash of the directory —
#: against a round measured in minutes.
_HARNESS_IDENTITY: dict = harness_identity(_loops_dir())


def board_dial_notes(cfg: dict) -> list[str]:
    """`config_notes` lines for a round that ran under a board dial, or none.

    #52's rule — a round that ran under a changed dial SAYS SO — and the reason it
    is `config_notes` rather than the report alone is that `--post` puts these in a
    public PR comment, so the person reading the review sees the floor it was run
    against and who moved it.
    """
    said = [(path, d) for path, d in (cfg.get("_dials") or {}).items()
            if d.get("layer") == "board" and path.startswith(_REVIEW_BLOCKS)]
    notes = []
    if cfg.get("_dials_unreadable"):
        notes.append(
            f"the board at {cfg.get('_dials_from')} would not answer, so this round "
            f"ran on this repo's own rules — which is not the same thing as no dial "
            f"being set on the board")
    for path, d in sorted(said):
        lapses = f", until {d['expires_at']}" if d.get("expires_at") else ""
        notes.append(f"{path} is {json.dumps(d['value'])} from the BOARD "
                     f"({d['scope']} scope), not from {SAMPLE_FILENAME}: "
                     f"{d['reason'] or 'no reason given'} — set by "
                     f"{d['set_by'] or 'nobody named'}{lapses}")
    return notes


def review_refusal(cfg: dict) -> str:
    """Why this repo may not be REVIEWED, or `""` when it may.

    Absence of a rules file means "use the built-in defaults" everywhere else in the
    harness, and that is right for `epic`, `lander` and `preland`: every default is
    the safe end of its own switch, so an unconfigured repo gets a run that does
    LESS, never one that does something nobody asked for. Which is why this is a gate
    on the two review paths and not on `resolve_repo` — putting it there would take
    the whole harness down on any repo that has not enrolled, including the parts
    whose defaults are the safe end.

    A review is not that shape. Its defaults are a two-seat panel, on two models
    nobody chose, adjudicated by a judge nobody chose, at a code-access posture
    nobody chose — and its OUTPUT is not inert: the findings brief a fixer that then
    edits the repo, and a `confident` stop is read downstream as coverage the PR
    actually got. A defaults-only review is a review nobody configured, and the
    remedy is one committed file. So it is refused, out loud, rather than run.

    Read off `_rules_baseline` — the FILENAME that supplied the baseline, `""` for
    none — and never by matching English in `_rules_from`. That blurb is written for
    a human at the top of a report ("none on origin/main (defaults)"), so a gate
    grepping it is a gate one rewording away from reviewing everything; and
    `.harness-rules.sample` contains `.harness-rules` as a substring, so even the
    filename cannot be sniffed back out of it safely.

    TWO REFUSALS, because an empty baseline has two causes and only one of them has
    the remedy this used to print. A repo that carries no rules file is fixed by
    committing one. A repo whose `origin/<default>` could not be READ — no remote, no
    fetch, a git error — is very possibly fully enrolled, and telling its operator to
    commit a file they already committed sends them to the wrong place while the
    unattended timer keeps refusing every round. Which of the two happened is taken
    from `_rules_unreadable`, a flag `resolve_repo` stamps, for the same reason the
    baseline is a field: the sentence that distinguishes them is written for a human.
    Both still refuse, and that is deliberate — an unreadable branch is not evidence
    the repo is configured the way this run would guess.
    """
    if cfg.get("_rules_baseline"):
        return ""
    who = cfg.get("name") or "this repo"
    said = cfg.get("_rules_from") or "no rules were read"
    if cfg.get("_rules_unreadable"):
        return (f"{who}'s rules could not be read — {said}. A panel review is not run "
                f"on built-in defaults: which seats, which models and which judge "
                f"review this repo is a decision, and a review nobody configured "
                f"still briefs a fixer that edits the code. This is a read failure "
                f"rather than a missing file, so committing one will not clear it — "
                f"fetch the default branch, or check the remote, and re-run")
    return (f"{who} has no {SAMPLE_FILENAME} and no "
            f"{RULES_FILENAME} — {said}. A "
            f"panel review is not run on built-in defaults: which seats, which models "
            f"and which judge review this repo is a decision, and a review nobody "
            f"configured still briefs a fixer that edits the code. Commit a "
            f"{SAMPLE_FILENAME} (start from the one in the quarterback repo) and "
            f"re-run")


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

#: The key the judge's coverage ruling arrives under (#547), and the one example
#: entry :data:`JUDGE_PROMPT` ships inside it.
#:
#: Not in :data:`DECLARATION_KEYS`, and the distinction is the whole of #547: a
#: DECLARATION says what went unassessed, and this RULES on the declarations
#: somebody else made. Putting it there would make a judge that returned nothing
#: but a coverage ruling read as a judge that declared its own coverage.
COVERAGE_RULINGS = "coverage_rulings"


def _ruling_example() -> dict | None:
    """The one `coverage_rulings` entry the judge prompt illustrates."""
    items = (SCHEMA_ECHOES.get("verdicts") or {}).get(COVERAGE_RULINGS)
    return items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None


#: What a judge that quoted the ruling schema back returns, rather than ruling.
SCHEMA_RULING = _ruling_example()


def _is_ruling(item: object) -> bool:
    """Whether one `coverage_rulings` entry is a ruling rather than the example.

    The same test :func:`_is_answer` applies to a verdict, and it has to be applied
    here for the same reason: a judge that echoes the schema beside a real reply
    would otherwise contribute an entry claiming declaration numbers it never read,
    and a ruling is the one thing in this reply that can REMOVE a veto line."""
    return isinstance(item, dict) and not _quoted(item, SCHEMA_RULING)


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
    #: The judge's coverage RULING as the caller will read it (#547), normalised
    #: the same way `declared` is, and `()` on every reply that carries none.
    #:
    #: Here rather than left out of the comparison, because this is the one field
    #: of a judge reply that can DELETE a veto line. Two candidates identical in
    #: their verdicts and contradicting each other about which declarations no seat
    #: could have settled are not one answer, and leaving them out of the equality
    #: would have position decide which contradiction survives — which is exactly
    #: what :func:`_agreed` exists to stop doing.
    rulings: tuple = ()


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
    rulings = _rulings(obj.get(COVERAGE_RULINGS)) if kind == "verdicts" else ()
    return _Read(kept, None if read is None else tuple(read), rulings)


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


def severity_at_least(sev, floor) -> bool:
    """Is ``sev`` at least as severe as ``floor``? P1 is the most severe.

    One predicate for both of #165's floors and for the report's below-floor mark,
    so "which side of the line is this finding on" cannot be answered two ways in
    the same round. Both arguments go through :func:`_severity`, which strips and
    upper-cases — so a rules file saying ``p2`` and a reviewer saying ``" p2 "``
    are the same floor, matching what every other severity in this panel is
    normalised to at parse time.

    An unreadable SEVERITY reads as P1 and an unreadable FLOOR as no floor at all,
    and the asymmetry is deliberate: both errors resolve towards *fixing it
    anyway*. A finding whose severity nothing could parse is not a finding to drop
    on the strength of the parse failure, and a floor nobody could read must not
    silently start filtering. The floor is separately validated where it is read
    (:func:`panel_seats.severity_floor`), which is where an operator gets told."""
    return (SEVERITIES.index(_severity(sev, SEVERITIES[0]))
            <= SEVERITIES.index(_severity(floor, NO_SEVERITY_FLOOR)))


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


def _needs_human(it: dict) -> tuple[bool, str, str]:
    """One finding's escalation declaration: `(flagged, class, reason)`.

    **Evidence or nothing.** A seat that says `needs_human: true` and names
    neither a class nor a reason has escalated on its own authority with nothing
    behind it, and #279 refuses exactly that at the API and again at the database
    CHECK. Refusing it here too means the panel never SENDS the shape the board
    would reject, so the refusal is not something the operator has to read out of
    a `needs_human_refused` key after the fact.

    The refusal is total and deliberately not partial: a flag with a class and no
    reason is not filed under that class, because a class with no argument behind
    it still lands in the count that decides whose afternoon this is. And the
    evidence is dropped when the flag is not set, for the reason an orphan reads
    badly at ingest — a class with no flag behind it looks precisely like a
    declaration somebody later withdrew.
    """
    if not _flag(it.get("needs_human")):
        return False, "", ""
    cls = needs_human_class_or_none(it.get("needs_human_class"))
    why = needs_human_reason_or_none(it.get("needs_human_reason"))
    if not cls or not why:
        return False, "", ""
    return True, cls, why


def _to_findings(reviewer: str, items: list) -> list[Finding]:
    out = []
    for it in items:
        flagged, cls, why = _needs_human(it)
        out.append(Finding(
            reviewer=reviewer,
            severity=_severity(it.get("severity"), "P3"),
            file=str(it.get("file", "?")),
            line=it.get("line") if isinstance(it.get("line"), int) else None,
            title=str(it.get("title", "")).strip(),
            detail=str(it.get("detail", "")).strip(),
            needs_rereview=_flag(it.get("needs_rereview")),
            needs_human=flagged,
            needs_human_class=cls,
            needs_human_reason=why,
        ))
    return out


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


class CoverageRule(NamedTuple):
    """One entry of the judge's `coverage_rulings`, as the caller will read it.

    The declarations are report NUMBERS the panel minted, never the seats' prose —
    which is what makes this a typed ruling rather than the regex over free-form
    wording :func:`coverage_veto`'s docstring rules out twice. The judge is handed a
    numbered list and answers with numbers; nothing here reads a declaration's TEXT
    to decide which entry it belongs to."""

    #: Declaration ids this entry merges, deduplicated and in the order given.
    declarations: tuple[int, ...]
    #: The merged claim, in the judge's words. Empty when it wrote none.
    claim: str
    #: True only for a literal `resolvable_in_harness: false`. Every other value —
    #: absent, null, a string, a number, a reply that never carried the key — leaves
    #: this False, and a declaration nothing ruled `false` vetoes exactly as it did
    #: before #547. The exemption takes an affirmative typed act; silence never
    #: buys one, and this is the line where that is true rather than a paragraph.
    unresolvable: bool
    #: What would settle it, in the judge's words. Empty when it wrote none.
    reason: str


def _rulings(raw) -> tuple[CoverageRule, ...]:
    """The judge's coverage ruling, normalised — `()` for a reply carrying none.

    Defensive in one direction only, which is the same promise :func:`_parse_verdicts`
    makes and the opposite of the direction it makes it in. There, a malformed reply
    must never SUPPRESS a finding. Here, a malformed reply must never EXEMPT a
    declaration: every branch that cannot read something drops the entry, and a
    dropped entry means the declarations it would have covered stay unruled and go on
    vetoing.

    An entry naming no declaration is dropped. It merges nothing, so it can exempt
    nothing, and keeping it would put a claim on the round's obligation ledger that no
    reviewer ever raised — a model-authored obligation, which is a model authoring the
    thing that discharges the gate."""
    if not isinstance(raw, list):
        return ()
    out: list[CoverageRule] = []
    for item in raw:
        if not _is_ruling(item):
            continue
        ids = tuple(dict.fromkeys(_member_ids(item.get("declarations"))))
        if not ids:
            continue
        out.append(CoverageRule(
            declarations=ids,
            claim=str(item.get("claim") or "").strip(),
            # `is False`, exactly as :func:`_ruling` reads `real`, and for the
            # mirror-image reason: there an unreadable flag must not dismiss a
            # finding, here it must not excuse a gap.
            unresolvable=item.get("resolvable_in_harness") is False,
            reason=str(item.get("reason") or "").strip()))
    return tuple(out)


def _premise_verdict(raw) -> str:
    """The judge's answer to #67's recurrence question, or ``""`` for no answer.

    Membership-tested against :data:`PREMISE_VERDICTS` rather than pattern-matched, the
    same rule the board's ingest applies to a provenance bucket one process over
    (#65's class of drift): a value a consumer COUNTS must never be stored when it
    is not one of the values that consumer knows. A judge that answers
    "invalidates the premise" or "probably separate" has said something this
    cannot count, so it counts nothing rather than counting it as the word it
    starts with.

    Absent is the ordinary case and is not a failure. The brief explicitly tells
    the judge to leave the key off when it has nothing to say, and every round
    with no earlier round never carries the brief at all.
    """
    said = raw.strip().lower() if isinstance(raw, str) else ""
    return said if said in PREMISE_VERDICTS else ""


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
    #: As `ReviewerRun.model_unavailable` / `.effort_unsupported`. An `--ask` run
    #: falls back exactly as a review does, and without these the tally renders the
    #: PIN while the CLI default answered — the same false record, one report over.
    #:
    #: **At the end, deliberately.** This class is built positionally
    #: (`SeatAnswer(verdict, reason, ...)`), so a field inserted ahead of `verdict`
    #: rebinds every ask's verdict to it — 19 tests, all reporting a wrong verdict
    #: rather than a type error.
    model_unavailable: str = ""
    effort_unsupported: str = ""


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
    "TREE_FETCH_TIMEOUT", "TREE_MAX_BYTES", "TREE_MAX_EXTRACTED_BYTES",
    "TREE_MAX_MEMBERS",
    "shutil", "ssl", "subprocess", "sys",
    "tarfile", "tempfile", "time", "urllib", "uuid", "sh_bytes",
    "Counter", "Callable", "ThreadPoolExecutor", "dataclass",
    "field", "Path", "NamedTuple", "harness_rules",
    "DENIAL_MARKERS", "REJECTION_MARKERS", "RepoNotFound", "cli_outcome",
    "describe", "resolve_repo", "stderr_gist", "DEFAULT_DIFF_BUDGET",
    "RAW_DETAIL_CHARS", "CLUSTER_WINDOW", "ACCOUNT_CHARS", "DEFAULT_MAX_ROUNDS",
    "DEFAULT_ROUND_SCOPE", "ROUND_SCOPES", "CLI_TIMEOUT", "BLANK_RETRY_MAX_S",
    "DEFAULT_FIX_SEVERITY_FLOOR", "DEFAULT_ROUND_TRIGGER_FLOOR", "NO_SEVERITY_FLOOR",
    "BLOCKING_SEVERITIES", "DEFAULT_THRESHOLD_BY_SEVERITY",
    "DEFAULT_LOW_SEVERITY_FIX_LINES",
    "DEFAULT_LOW_SEVERITY_FIX_FULL_CHARS",
    "DEFAULT_UNREFEREED_LINE_WEIGHT",
    "DEFAULT_MAX_FIX_GROWTH", "DEFAULT_MAX_FIX_GROWTH_CHARS",
    "DEFAULT_MIN_FIX_GROWTH_CHARS",
    "DEFAULT_MAX_FIX_GUARD_LINES",
    "DEFAULT_MAX_FIX_GUARD_LINES",
    "DEFAULT_REVIEWER_SCOPE", "REVIEWER_SCOPES",
    "DEFAULT_FIXER_MAY_DEFER", "DEFAULT_REQUIRE_FAILING_TEST",
    "DEFAULT_FILE_DEFERRAL_ISSUES", "DEFERRAL_ISSUES_ALWAYS",
    "DEFERRAL_ISSUES_NEVER", "DEFERRAL_ISSUE_ENDS",
    "DEFERRAL_ISSUES_SHAPE", "DEFERRAL_ISSUE_WORDS",
    "DEFERRAL_SHAPE_CATEGORY", "DEFERRAL_SHAPE_ITEM", "DEFERRAL_SHAPE_BATCH",
    "DEFERRAL_SHAPES", "DEFERRAL_ISSUE_SHAPES",
    "DEFAULT_DISTANT_MERGE_LINES",
    "severity_at_least", "REVIEWER_SCOPE_SLOT", "RELATED_CODE_SLOT",
    "_SCOPE_BRIEF", "reviewer_brief",
    "NEXT_DOOR_SLOT", "NEXT_DOOR_HEADING", "_NEXT_DOOR_BRIEF",
    "NEXT_DOOR_MAX", "_hint_line", "next_door_brief", "next_door_note",
    "NEXT_DOOR_TITLE_CHARS", "NEXT_DOOR_DETAIL_CHARS", "_one_line",
    "DEFAULT_NEXT_DOOR_DAYS", "NEXT_DOOR_DAYS_MAX",
    "CLI_ABSENT", "ARGV_PROMPT_MAX_BYTES", "SEVERITIES", "MAX_LISTING_CHARS",
    "LISTING_ACCOUNT_CHARS", "COMMENT_CHARS", "ROUNDS_HEADING", "LLM_REVIEWERS",
    "BUDGET_MARKER", "BUDGET_EXHAUSTED", "JUDGE_CODE_SLOT",
    "JUDGE_RECURRENCE_SLOT", "PREMISE_VERDICTS", "RECURRENCE_BRIEF",
    "_premise_verdict",
    "ALL_REVIEWERS", "CLI_BIN", "seat_installed", "SEAT_MODEL_DEFAULTS",
    "_FINDINGS_ENVELOPE", "REVIEW_PROMPT", "MOVE_MANIFEST_PROMPT",
    "CODE_ACCESS_BRIEF",
    "NO_TOOLS_BRIEF",
    "NO_TOOLS_RULE",
    "JUDGE_PROMPT", "ASK_PROMPT", "Finding", "ReviewerRun",
    "PanelResult", "sh", "load_repo_cfg", "review_refusal", "rules_record",
    "HARNESS_DIGEST_SCHEME", "HARNESS_GIT_TIMEOUT_S", "_harness_git",
    "_harness_digest", "_harness_checkout", "harness_identity", "_loops_dir",
    "_HARNESS_IDENTITY",
    "board_dial_notes",
    "RULES_FILENAME", "SAMPLE_FILENAME", "_spans",
    "ENVELOPE_KEYS", "DECLARATION_KEYS", "_scalar", "_Tok",
    "_TOKEN", "_TOKEN_MARK", "_tokenise", "_schema",
    "SCHEMA_ECHOES", "_example", "SCHEMA_ITEMS", "_standins",
    "SCHEMA_DECLARATIONS", "_quoted", "_is_answer", "_Read",
    "COVERAGE_RULINGS", "_ruling_example", "SCHEMA_RULING", "_is_ruling",
    "CoverageRule", "_rulings",
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
