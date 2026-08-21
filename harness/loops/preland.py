#!/usr/bin/env python3
"""The pre-land verdict: may this PR be merged, and if not, what is outstanding.

The mechanical checks a merge has to pass used to exist twice, in two forms, and
neither was executable. ``/fix-and-land`` §4 described them in about fifty lines
of English; ``/panel-review-pr`` §7 was one line — ``gh pr merge`` — with nothing
in front of it at all. Prose in two files drifts, has no exit code, and cannot
answer "did the gate actually run?" after the fact. Worse, a model reading it is
invited to re-derive a decision it should be executing: on 2026-08-16 a PR with
an unread panel round carrying 8 P1s was merged on `mergeable` + CI-green by the
same agent that had written up that exact confusion an hour earlier.

So the verdict is computed here, mechanically, on the same terms as
``round_stop``: the caller acts on it and does not substitute its own judgement
for it.

VERDICTS
    READY       every check that ran is satisfied. Exit 0.
    RECONCILE   mechanical work is outstanding, and ``actions`` holds the exact
                commands and the files they touch. Do them, then run this again.
                Exit 3.
    HOLD        something is unresolved that this loop must not resolve on its
                own. ``reasons`` says what, and who has to. Exit 2.

The codes are ``migration_reconcile.py``'s (0 = go, 3 = go via a fallback that
needs work first, 2 = stop), so the two tools agree about what an exit code
means rather than each having a private scheme. HOLD dominates RECONCILE: there
is no point relinking a migration on a PR nobody has reviewed. Note that 2 is
also argparse's usage exit — the JSON payload is what tells them apart, and both
of them mean "do not merge", so a caller that conflates them fails safe.

READ-ONLY, AND IT TAKES NO CLAIM
    This reports commands; it never runs them. It reads ``kind=merge`` claims and
    the merge queue, and HOLDs when another agent holds the base this PR lands
    onto or sits ahead of it in the line — but it TAKES neither. That is #100's,
    where the merge actually happens. The distinction is between
    the verdict (a pure function of the world) and the actor (the thing that
    merges), and conflating them costs three things at once: a verdict that
    mutates cannot run as a CI check, cannot be re-run to verify itself, and
    cannot be called twice by a loop that wants to know whether its own fix
    worked. The one write is ``git fetch`` of the base branch's remote-tracking
    ref (suppressible with ``--no-fetch``), because a migration verdict computed
    against a stale ``origin/main`` is worse than no verdict.

CAPABILITY DETECTION, AND THE ONE PLACE ABSENT DOES NOT MEAN SKIP
    Repo-local guardrails are detected: a repo without
    ``scripts/migration_reconcile.py`` skips that check silently, because an
    absent script means the invariant does not exist in that repo. That is what
    lets one script serve quarterback, lexray and an unenrolled repo without a
    per-repo branch in the skill.

    The board is the exception, and deliberately. An unset
    ``QUARTERBACK_BASE_URL`` does not mean "this repo has no review invariant",
    it means the invariant exists and cannot be seen — so the ``review`` check
    HOLDs rather than skipping, and the message names the off-switch verbatim.
    This narrows #59's promise that the local path stays first-class ("/panel
    must work with no board, no queue and no qb binary"): ``/panel`` still does,
    but ``/fix-and-land`` on a board-less box now blocks until someone writes
    ``"preland": {"disabled_checks": ["review"]}`` in ``.harness-rules``. That is
    a knowing narrowing, not an oversight — a merge gate that fails open wherever
    it cannot see is not a gate — and the off-switch is one line.

    A check turned off by ``--skip`` or by ``.harness-rules`` is still REPORTED,
    as ``skipped-flag`` / ``skipped-disabled``. A payload must never read clean by
    omission; #75 learned that once already, as "N of M configured".

WHAT IT MUST NOT BECOME
    Never gate on a proxy. Not "a payload exists", not "the job exited 0" — on
    the round's own statements: ``stopped``, ``confirmed``, ``head_sha``,
    ``sonar_gate``. #62 spent three rounds discovering that merge gates trust
    proxies, replacing the exit code with the push and the push with the payload
    artefact. And ``stop_confident: false`` is a WARN by default, not a HOLD: two
    permanently-absent reviewer seats on a headless box would otherwise make a
    green verdict unreachable, which is the noise-for-signal trade
    ``.harness-rules`` already argues against for ``coverage_veto``. A caller
    that ran the round itself and is about to offer to land on the strength of it
    asks for the strict reading with ``--require-earned-stop`` (#100).

ITS LIMIT, STATED PLAINLY
    This is advisory. The board cannot gate github.com and neither can a script
    an agent chooses to run: a human merging in the UI, or a loop that skips this
    step, lands regardless. What blocks a merge is GitHub — a required status
    check on a protected branch, which is tier 2 and is not built here. A CI job
    calling this must pass ``--skip ci``, because such a job is itself one of the
    checks ``ci`` reads and would otherwise gate on its own pending status.

Usage:
    preland.py --pr 131                     # human-readable, in the cwd's repo
    preland.py --pr 131 --json              # the payload a loop reads
    preland.py --pr 131 --skip ci           # for a CI job (see above)
    preland.py --pr 131 --require-earned-stop   # for the loop that ran the round
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness_rules import (  # noqa: E402
    RepoNotFound, check_status, describe, resolve_repo,
)
# #278's reading lives in `panel_scope` because that is where the review target is
# decided, and the JUDGEMENT must not exist twice: this gate and the round it rules
# on have to answer "how involved was that merge" the same way, or a payload and a
# verdict can disagree about the same commit with nothing saying which is right.
# Only the measurement differs by caller — the panel reads GitHub's compare API and
# checks nothing out, this runs in a checkout and has real `git`.
from panel_scope import (  # noqa: E402
    DEFAULT_DISTANT_MERGE_LINES, Integration, merge_involvement,
)
from panel_seats import distant_merge_lines  # noqa: E402

READY, RECONCILE, HOLD = "READY", "RECONCILE", "HOLD"

#: Verdict -> process exit code. Deliberately `migration_reconcile.py`'s.
EXIT = {READY: 0, RECONCILE: 3, HOLD: 2}

#: Every check this script knows, in report order. Named here rather than
#: collected from the functions so that `--skip nonsense` and a typo in
#: `.harness-rules` are both hard errors: a gate silently running one check short
#: is the failure mode the whole file exists to prevent.
CHECKS = ("pr_state", "checkout", "ci", "review", "merge_claim", "queue",
          "migrations", "sw_version")

#: The three distinct reasons a check did not run. Kept apart because "this repo
#: has no such script", "someone wrote it off in .harness-rules" and "this run was
#: told to skip it" are not the same fact about a merge, and a single `skipped`
#: would make them one.
SKIPPED = ("skipped-absent", "skipped-disabled", "skipped-flag")

#: Statuses that are not an objection. `failed` and `error` are the objections,
#: and are deliberately NOT listed — see :func:`verdict_of` for why the list runs
#: this way round.
NOT_AN_OBJECTION = ("passed", "reconcile", *SKIPPED)

#: Where the site config lives, in qb-env's words: the per-host file that says
#: which board this machine belongs to. Environment beats it, and an unset URL is
#: an ERROR rather than a guess — the fleet has more than one board and they are
#: deliberately disjoint, so a default would point an agent at another island's.
QB_CONFIG = Path(os.environ.get("QUARTERBACK_CONFIG") or (
    Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    / "quarterback" / "config"))

#: The pre-config fleet layout, kept for a host that has not been rebuilt yet.
QB_TOKEN_FILE = Path("/run/op-secrets/quarterback-token")

BOARD_TIMEOUT = 15

#: The board endpoint the landing queue lives behind (#227/#317). Named here
#: because :func:`check_queue` says it back in two messages, and a path spelled
#: three times is a path that ends up spelled two ways.
QUEUE_PATH = "merge-queue"


# --------------------------------------------------------------- the two shells


def sh(args: list[str], **kw) -> str:
    """Run a command, return stdout, raise on failure. The single seam every
    subprocess in this module goes through, so a test doubles one function."""
    return subprocess.run(args, capture_output=True, text=True, check=True, **kw).stdout


#: Long enough for `git fetch` over a slow link and for the reconciler to walk a
#: large migration graph; short enough that a wedged guardrail becomes a HOLD
#: rather than a loop that never returns a verdict at all.
RUN_TIMEOUT = 300


def run(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run a command and hand back the whole result, exit code included — for the
    guardrail scripts, whose exit code IS their answer.

    A missing binary and a wedged one come back as ordinary non-zero results
    rather than exceptions, because every caller here turns a non-zero into a
    reported check and a traceback into no verdict at all. 127 and 124 are the
    shell's own codes for the two cases, so the number in `detail` means
    something to whoever reads it.
    """
    try:
        return subprocess.run(args, capture_output=True, text=True, cwd=cwd,
                              timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args, 124, "", f"timed out after {RUN_TIMEOUT}s")
    except OSError as e:
        return subprocess.CompletedProcess(args, 127, "", f"could not run {args[0]}: {e}")


# ------------------------------------------------------------- what a check says


@dataclass
class BaseRef:
    """The base branch, and whether we actually managed to look at it.

    Threaded into the repo-local guardrails rather than left implicit, because
    those two facts are one fact: a migration or cache-version verdict computed
    against a stale ``origin/<base>`` is confidently wrong in the direction that
    lands, and a `git fetch` whose failure went unread leaves exactly that with
    nothing on the report saying so.
    """

    name: str
    #: Was `origin/<name>` refreshed just now?
    fresh: bool = True
    #: If not — was that the caller's explicit `--no-fetch` (a warning) or a
    #: failure (an error)? The distinction is who is responsible for it.
    chosen: bool = False
    why: str = ""

    @property
    def ref(self) -> str:
        return f"origin/{self.name}"

    @property
    def quoted(self) -> str:
        """The ref as it must appear inside an emitted shell command.

        `actions` are strings a loop is told to run verbatim, and a git refname
        may legally contain `;`, `$` and backticks. An unquoted branch name in a
        command this file hands over is an injection into a shell it does not own.
        """
        return shlex.quote(self.ref)


@dataclass
class Action:
    """One mechanical command a RECONCILE needs, and what it touches."""

    command: str
    why: str
    files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"command": self.command, "why": self.why, "files": self.files}


@dataclass
class Check:
    """One guardrail's answer.

    `reasons` are why this check HOLDs and `warnings` are what it noticed without
    holding; both are lists because a check routinely has more than one thing to
    say — PR #131 was HOLD on two independent counts (`stopped: false` AND 20
    confirmed findings) and reporting one of them would have understated it.
    """

    name: str
    status: str
    summary: str = ""
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"status": self.status, "summary": self.summary,
                "reasons": self.reasons, "warnings": self.warnings,
                "actions": [a.as_dict() for a in self.actions],
                "detail": self.detail}


def verdict_of(checks: list[Check]) -> str:
    """HOLD if anything objected, else RECONCILE if there is mechanical work,
    else READY.

    HOLD dominates RECONCILE on purpose: reconciling a migration graph on a PR
    nobody reviewed is work spent to reach a wall.

    The first test asks what is NOT an objection rather than listing `failed` and
    `error`, so that a status this function does not recognise — a check added
    later with a new word for its answer, or a typo — HOLDs instead of falling
    through to READY. A merge gate's default branch has to be the closed one.
    """
    if any(c.status not in NOT_AN_OBJECTION for c in checks):
        return HOLD
    if any(c.status == "reconcile" for c in checks):
        return RECONCILE
    return READY


# ---------------------------------------------------------------- the board read


def _config_file(path: Path) -> dict[str, str]:
    """The site config's `KEY=value` lines, as a mapping.

    A deliberately small reader for a file bash SOURCES. It takes plain
    assignments and strips one layer of quotes; it does not evaluate anything,
    because a config read must not be able to run what a config write put there.
    A line it cannot parse is skipped rather than guessed at.

    The cost of not evaluating: a value containing `$VAR` is taken literally.
    That is the honest failure — the board is then "unreachable at $VAR/…", which
    names the problem — rather than the dishonest one, which would be expanding
    it here and getting a different answer than `qb` does.
    """
    out: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line[len("export "):].strip() if line.startswith("export ") else line
        name, sep, value = line.partition("=")
        if not sep or not name.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[name.strip()] = value
    return out


def board_config() -> tuple[str, str, str]:
    """(base_url, token, why-it-is-unusable) for this host's board.

    Same contract and same precedence as `qb-env`, which is the fleet's rule
    rather than this script's preference: environment beats the config file, an
    unset URL is an error and never a guess, and the token may come from a
    command because its source is per-site (a cached file here, an ssh fetch
    there). Re-derived in Python only because `qb` has no read subcommand for
    reviews and lives in another repo; the moment a second reader needs this it
    belongs in harness_rules.py beside the other shared plumbing.
    """
    cfg = _config_file(QB_CONFIG)
    url = (os.environ.get("QUARTERBACK_BASE_URL")
           or cfg.get("QUARTERBACK_BASE_URL", "")).rstrip("/")
    if not url:
        return "", "", (
            f"no board configured on this host — QUARTERBACK_BASE_URL is unset "
            f"and there is deliberately no default (see {QB_CONFIG})")
    token = os.environ.get("QUARTERBACK_TOKEN", "") or _token_from(cfg)
    if not token:
        return url, "", ("no board token — set QUARTERBACK_TOKEN, or "
                         f"QUARTERBACK_TOKEN_CMD in {QB_CONFIG}")
    return url, token, ""


def _token_from(cfg: dict[str, str]) -> str:
    """The bearer, via the config's own command or the pre-config token file."""
    cmd = cfg.get("QUARTERBACK_TOKEN_CMD", "")
    if cmd:
        proc = run(["bash", "-c", cmd])
        first = (proc.stdout or "").strip().splitlines()
        if not proc.returncode and first:
            return first[0]
    try:
        return QB_TOKEN_FILE.read_text().strip()
    except OSError:
        return ""


def board_get(path: str, params: dict) -> tuple[object, str]:
    """(parsed JSON, error) from the board, for the checks with no use for a
    status code. :func:`board_request` without its third element."""
    body, err, _ = board_request(path, params)
    return body, err


def board_request(path: str, params: dict) -> tuple[object, str, int | None]:
    """(parsed JSON, error, HTTP status) from the board. Never raises: an
    unreachable board is a fact this script REPORTS, and reporting it is what
    makes it a HOLD rather than an exception nobody mapped to a verdict.

    The status is returned because ONE caller needs to tell two failures apart
    that the error sentence cannot: :func:`check_queue` reads an endpoint that
    older boards do not implement, and a 404 there means "this board has no
    landing queue" — a capability answer — while a 500 means it has one and it
    broke. The module docstring's capability rule for repo-local scripts is the
    same rule; the board is simply the other place a guardrail can be absent.
    It is `None` for everything that never reached HTTP at all.
    """
    url, token, why = board_config()
    if why:
        return None, why, None
    full = f"{url}/{path.lstrip('/')}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=BOARD_TIMEOUT) as r:
            return json.loads(r.read().decode()), "", r.status
    except urllib.error.HTTPError as e:
        # Before URLError: it is a subclass, and an HTTP status says far more
        # than "unreachable" — 401 in particular is a token problem, not a board
        # that is down, and the two want different remedies.
        hint = " — the token this host resolved was refused" if e.code == 401 else ""
        return None, f"board answered HTTP {e.code} for /{path.lstrip('/')}{hint}", e.code
    except OSError as e:
        # OSError, not URLError: it covers URLError and TimeoutError both, plus a
        # connection reset partway through the read, which is an OSError that
        # urllib does not wrap. This function's contract is that it never raises,
        # and a narrow tuple is how that contract quietly stops being true.
        return None, f"board unreachable at {url} ({e.__class__.__name__})", None
    except ValueError:
        # JSONDecodeError, and a body that is not valid UTF-8. The status is
        # dropped rather than reported: the request DID reach the board, so a
        # caller reading capability off the status would draw a conclusion from a
        # body nothing managed to parse.
        return (None, f"board answered /{path.lstrip('/')} with something that is "
                "not JSON", None)


# ------------------------------------------------------------------- the checks


def mergeability(pr: dict) -> tuple[str, str]:
    """``(the normalised mergeable state, the sentence to say about it)``.

    The mergeability clause of :func:`check_pr_state`, lifted out because it has a
    SECOND caller: `panel.py` refuses a review round on a branch that cannot merge
    (#271), and the merged state a review reasons about does not exist while the
    branch is CONFLICTING. Lifted rather than copied — the three pre-land checks in
    #96 drifted precisely because the same question was asked in two places in two
    wordings, and a reviewer refusing a round with one sentence while the merge gate
    refuses the merge with another is that failure arriving one loop earlier.

    The sentence is ``""`` only for ``MERGEABLE``. Every other value has something
    to say, and the caller decides whether its own answer is a refusal or a
    warning: at the merge gate CONFLICTING blocks and UNKNOWN warns, and the panel
    makes the same split for its own reasons.
    """
    state = (pr.get("mergeable") or "UNKNOWN").upper()
    if state == "CONFLICTING":
        return state, ("GitHub reports the branch as CONFLICTING — resolve the "
                       "conflicts by hand; guessing a resolution is not mechanical")
    if state != "MERGEABLE":
        return state, (f"GitHub has not computed mergeability yet ({state}); "
                       "a conflict would fail the merge loudly rather than land")
    return state, ""


def check_pr_state(pr: dict) -> Check:
    """The PR itself: open, not a draft, and not in conflict.

    First because it catches the cheapest wrong thing an autonomous loop does —
    on 2026-08-16 a fix pass spent an hour on a branch whose PR had already
    merged. A closed PR is not a merge candidate and every check after this one
    would be answering a question about the past.
    """
    c = Check("pr_state", "passed",
              f"{pr['state']} · {pr['headRefName']} → {pr['baseRefName']}")
    if pr["state"] != "OPEN":
        c.reasons.append(f"the PR is {pr['state']}, not OPEN — there is nothing to land")
    if pr.get("isDraft"):
        c.reasons.append("the PR is a draft; mark it ready for review first")
    mergeable, said = mergeability(pr)
    if mergeable == "CONFLICTING":
        c.reasons.append(said)
    elif said:
        c.warnings.append(said)
    c.detail = {"state": pr["state"], "draft": bool(pr.get("isDraft")),
                "mergeable": mergeable, "head_sha": pr["headRefOid"]}
    c.status = "failed" if c.reasons else "passed"
    return c


def check_checkout(root: str, pr: dict) -> Check:
    """This working tree is the PR's head, and it is clean.

    Both halves matter to the checks after it: the repo-local guardrails read the
    tree, so a verdict computed in a checkout that is not the PR's head describes
    code the PR does not contain, and `migration_reconcile.py apply` refuses on a
    dirty versions directory anyway. Untracked files are a WARNING rather than a
    failure — a scratch file is not a reason to refuse a merge, and this repo's
    own plan.md is deliberately untracked.
    """
    c = Check("checkout", "passed")
    head = _git(root, "rev-parse", "HEAD")
    porcelain = _git(root, "status", "--porcelain")
    if head is None or porcelain is None:
        # An unreadable git state is not a clean one. Falling through with an
        # empty string here would have skipped the mismatch clause and reported
        # a spotless tree — the file's own rule broken in the file itself.
        c.status, c.summary = "error", "git could not be read here"
        c.reasons.append(f"`git` could not read {root} — no head or status to check "
                         "the PR against")
        return c
    if head != pr["headRefOid"]:
        c.reasons.append(
            f"this checkout is at {head[:12]}, the PR's head is "
            f"{pr['headRefOid'][:12]} — the repo-local guardrails would be "
            "answering about different code. Check the branch out, or --skip checkout")
    lines = [ln for ln in porcelain.splitlines() if ln.strip()]
    tracked = [ln for ln in lines if not ln.startswith("??")]
    if tracked:
        c.reasons.append(f"{len(tracked)} tracked file(s) modified or staged — "
                         "commit or stash them; a reconcile would sweep them in")
    if len(lines) != len(tracked):
        c.warnings.append(f"{len(lines) - len(tracked)} untracked file(s) present")
    c.detail = {"head": head, "dirty_tracked": len(tracked),
                "untracked": len(lines) - len(tracked)}
    c.status = "failed" if c.reasons else "passed"
    c.summary = f"at {head[:12]}"
    return c


def check_ci(pr: dict) -> Check:
    """Green, and green NOW.

    Pending fails as hard as red. This runs after a reconcile has pushed
    commits, and a push restarts CI: an earlier green is a statement about code
    that is no longer at the head, which is the same staleness the `review` check
    below refuses for review rounds.
    """
    state = check_status(pr)
    rollup = pr.get("statusCheckRollup") or []
    c = Check("ci", "passed", f"{state} ({len(rollup)} check(s))",
              detail={"state": state, "checks": len(rollup)})
    if state == "red":
        c.reasons.append("CI is failing — never merge on red")
    elif state == "pending":
        c.reasons.append("CI has not finished; a pending check is not a green one")
    elif state == "none":
        # Not a warning, for the same reason an unreadable board is not a skip:
        # no CI signal is the absence of evidence, and this file's whole rule is
        # that absence never reads as clean. A workflow that failed to trigger and
        # a repo that has no CI look identical from here, and only one of them is
        # safe. A repo genuinely without CI says so once, in writing.
        c.reasons.append("the PR has no checks at all — nothing mechanical is "
                         "verifying this change. If this repo genuinely has no CI, "
                         'say so with `"preland": {"disabled_checks": ["ci"]}` in '
                         ".harness-rules rather than reading silence as green")
    c.status = "failed" if c.reasons else "passed"
    return c


@dataclass(frozen=True)
class MergeGate:
    """Where the #278 reading is measured and the dial it is measured against.

    Two fields rather than two more arguments threaded through three functions:
    the pair travels together, is read in one place, and a caller that has neither
    (a test, or a call site that predates #278) gets the honest answer — an
    unmeasurable head move, which HOLDs exactly as it did before. Frozen, so the
    empty one is safe as a default argument: it is built once at definition time
    and there is nothing in it a call could mutate for the next caller.
    """

    root: str = ""
    limit: int | None = DEFAULT_DISTANT_MERGE_LINES


def _paths(out: str | None) -> list[str] | None:
    """The paths in a NUL-separated `git` listing, or None if it could not be read.

    `-z` rather than the default: git QUOTES a path with a space or a non-ASCII
    byte in it (`"src/a b.py"`), and a quoted name matches nothing in the file set
    it is being intersected with — so a merge into a file whose name needed quoting
    would count as zero changed lines and read DISTANT. `_git` strips whitespace and
    NUL is not whitespace, so the trailing separator survives as an empty field and
    is dropped here.
    """
    if out is None:
        return None
    return [f for f in out.split("\0") if f]


def _integration_since(root: str, recorded: str, pr: dict) -> Integration:
    """What landed on this PR between the commit a round read and the head it has
    now, measured with real `git` in the checkout this gate is already standing in.

    The measurement #278 names, taken literally: `git diff` between the pre-merge
    head and the merge result, RESTRICTED to the files this PR itself touches. The
    restriction is what makes it a statement about this PR rather than about
    `main` — a base merge drags in every file main gained, which is not this PR's
    change and is not what the earlier round's findings were about.

    `--numstat` rather than a diff body: the count is all that is wanted, and a
    range that merged a busy `main` can be megabytes of text that would be read into
    memory only to be discarded. A binary file the PR also touches REFUSES the
    reading rather than counting zero — numstat reports `-` for it, and a merge that
    replaced an asset this PR also edits is real material that cannot be measured in
    lines, so it must not be allowed to look like an empty resolution.

    **"The files this PR touches" is a UNION of two moments, and it has to be.** Taken
    from the head alone it is what the PR contributes AFTER the merge, and a
    resolution that reverted one of the PR's files all the way back to base drops that
    file out of the set — so the change the earlier round reviewed would have been
    silently discarded by the merge and the measurement would score it zero and call
    the merge distant. That is #80's `stderr_gist` incident exactly (a landed fix lost
    because a branch that had MOVED a function met a `main` that already had it), and
    it is the one defect this whole reading exists to catch. So the files the PR
    touched AS THE ROUND READ IT — measured from its own fork point, before the
    integration — are in the set too.

    **A rewritten branch has no range and says so.** `git diff A B` will happily
    compare two commits with no ancestry between them, and after a force-push or a
    rebase the answer is not a delta from `A` at all: anything dropped in the rewrite
    is in neither side, and a small diff plus any merge commit in the replacement
    history would preserve a review of code that no longer exists. `_fix_range_diff`
    refuses GitHub's `diverged` status for the same reason; this is the local half.

    Every failure returns a stated `problem` and never raises. An unreadable range is
    an unread precondition, and :func:`merge_involvement` turns that into the
    expensive answer — the behaviour before #278, which is the safe direction.
    """
    head = pr["headRefOid"]
    if not root:
        return Integration(problem="no checkout was available to measure it in")
    for sha in (recorded, head):
        if _git(root, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}") is None:
            return Integration(problem=f"{sha[:12]} is not in this checkout, so the "
                                       "range cannot be read here — fetch it, or the "
                                       "branch was rewritten and there is no range")
    if _git(root, "merge-base", "--is-ancestor", recorded, head) is None:
        return Integration(problem=f"{recorded[:12]} is not an ancestor of the head, so "
                                   "there is no range between them — the branch was "
                                   "rewritten since the round read it")
    base = f"origin/{pr['baseRefName']}"
    fork, was = _git(root, "merge-base", base, head), _git(root, "merge-base", base,
                                                           recorded)
    if not (fork and was):
        return Integration(problem=f"this PR's fork point from {base} could not be "
                                   "computed, so which files are the PR's own is "
                                   "unknown")
    own = _paths(_git(root, "diff", "--name-only", "-z", fork, head))
    had = _paths(_git(root, "diff", "--name-only", "-z", was, recorded))
    rows = _paths(_git(root, "diff", "--numstat", "-z", "--no-renames", recorded, head))
    merges = _git(root, "rev-list", "--count", "--merges", f"{recorded}..{head}")
    if own is None or had is None or rows is None or merges is None:
        return Integration(problem="`git` could not read the range in this checkout")
    mine, churn = set(own) | set(had), {}
    for row in rows:
        parts = row.split("\t", 2)
        if len(parts) != 3 or parts[2] not in mine:
            continue
        if parts[0] == "-" or parts[1] == "-":
            return Integration(problem=f"{parts[2]} is binary and this PR also touches "
                                       "it, so what the range put there cannot be "
                                       "counted in lines")
        try:
            churn[parts[2]] = int(parts[0]) + int(parts[1])
        except ValueError:
            return Integration(problem=f"`git diff --numstat` gave {row!r}, which is "
                                       "not a line count")
    try:
        n_merges = int(merges)
    except ValueError:
        return Integration(problem=f"`git rev-list --count --merges` gave {merges!r}")
    return Integration(churn=churn, merges=n_merges)


def _head_moved(c: Check, recorded: str, pr: dict, gate: MergeGate) -> None:
    """#278: a head that moved is not by itself a review of earlier code.

    This clause used to be flat — recorded head != current head, therefore HOLD —
    and under the decision that is right for an INVOLVED merge and too strong for a
    DISTANT one. Too strong is not free: any integration moves the head, so the flat
    reading made a base merge cost a whole panel cycle across every seat, and #80
    measures integration cost as quadratic in the number of open PRs. Five
    concurrent PRs is about ten integration merges, at a measured 283,795 tokens per
    `claude` seat per round.

    Distant is a WARNING and not silence. The head did move, a reader of the payload
    has to be able to see that it moved and see why it was let through, and
    `merge_reading` in the detail carries the numbers the sentence is built from.
    """
    reading = merge_involvement(_integration_since(gate.root, recorded, pr),
                                gate.limit, (recorded, pr["headRefOid"]))
    c.detail["merge_reading"] = {"verdict": reading.verdict, "lines": reading.lines,
                                 "files": list(reading.files), "limit": reading.limit}
    moved = (f"the round read {recorded[:12]}, the PR's head is now "
             f"{pr['headRefOid'][:12]}")
    if reading.distant:
        c.warnings.append(f"{moved} — {reading.why}")
    else:
        c.reasons.append(f"{moved} — it is a review of earlier code: {reading.why}")


def check_review(repo: str, pr: dict, earned_stop: bool = False,
                 gate: MergeGate = MergeGate()) -> Check:
    """The panel's own statements about the round that read THIS commit.

    Every clause here is a field the round wrote about itself. None of them is a
    proxy, and none of them is re-derived: the panel already decided whether it
    stopped and why, and this reads that decision rather than forming a second
    opinion about the same diff.

    ``earned_stop`` is ``--require-earned-stop``: see :func:`_round_stop_earned`.
    ``gate`` is where the #278 head-moved reading is measured: see
    :func:`_head_moved`.
    """
    c = Check("review", "passed")
    rows, err = board_get("reviews", {"repo": repo, "pr": pr["number"], "limit": 20})
    if err:
        c.status, c.summary = "error", "review state unreadable"
        c.reasons.append(f"{err}. Cannot tell whether this PR was reviewed, and "
                         "absent must not read as clean. Turn this check off "
                         'deliberately with `"preland": {"disabled_checks": '
                         '["review"]}` in .harness-rules if this repo is not '
                         "board-enrolled")
        return c
    if not isinstance(rows, list):
        # Distinguished from "no rounds" rather than folded into it: both HOLD,
        # but one of them sends someone to run the panel and the other to look at
        # the board, and a wrong instruction is worse than none.
        c.status, c.summary = "error", "review state unreadable"
        c.reasons.append("the board answered /reviews with something that is not a "
                         "list of rounds, so what the panel said cannot be read")
        return c
    if not rows:
        c.status, c.summary = "failed", "never reviewed"
        c.reasons.append("no panel round is recorded for this PR — a change the "
                         "panel never saw must not sail through for want of an "
                         f"objection. Run `panel.py --pr {pr['number']}`")
        return c
    # Sorted here rather than trusting the endpoint's `ORDER BY ts DESC, id DESC`.
    # Which round this rules on is the whole verdict — an older one resurrects
    # findings a fix already cleared, or worse, blesses a stale head — and that is
    # too much to rest on another service's clause remaining what it is today.
    newest = max(rows, key=lambda r: (str(r.get("ts") or ""), r.get("id") or 0))
    return _judge_round(c, newest, pr, earned_stop, gate)


def _judge_round(c: Check, latest: dict, pr: dict, earned_stop: bool = False,
                 gate: MergeGate = MergeGate()) -> Check:
    """The clause list, against the newest round the board holds for this PR."""
    c.detail = {k: latest.get(k) for k in
                ("id", "ts", "round", "cycle", "head_sha", "stopped", "stop_reason",
                 "stop_confident", "confirmed", "unjudged", "sonar_gate", "judge_skip")}
    # Which mode ran, in the audit trail. A payload that reads READY has to say
    # whether the strict clause was even asked, or a caller cannot tell a stop
    # that was earned from one nobody put the question to.
    c.detail["require_earned_stop"] = earned_stop
    c.summary = (f"round {latest.get('round')} of cycle "
                 f"{(latest.get('cycle') or '?')[:8]} at "
                 f"{(latest.get('ts') or '?')[:19]}")
    recorded = latest.get("head_sha")
    if not recorded:
        c.reasons.append("the round recorded no head_sha, so there is no way to tell "
                         "which commit it read — re-run the panel on this head")
    elif recorded != pr["headRefOid"]:
        _head_moved(c, recorded, pr, gate)
    if latest.get("stopped") is not True:
        c.reasons.append("the round did not stop: " +
                         (latest.get("stop_reason") or "it recorded no stopping verdict"))
    confirmed = latest.get("confirmed")
    if not isinstance(confirmed, int):
        # NOT `or 0`. A count the board did not send is an unknown number of
        # unresolved findings, and coercing it to zero is the one arithmetic in
        # this file that could wave a defective PR through.
        c.reasons.append("the round recorded no confirmed-finding count, so how much "
                         "it found is unknown — and unknown is not zero")
    elif confirmed:
        c.reasons.append(f"{confirmed} judge-confirmed finding(s) are unresolved")
    gate = (latest.get("sonar_gate") or "").upper()
    if gate == "ERROR":
        c.reasons.append("the SonarCloud quality gate is failing — it is a hard gate")
    elif gate not in ("", "OK", "SKIPPED", "NO-ANALYSIS", "NO-PR-ANALYSIS", "NONE"):
        c.warnings.append(f"the SonarCloud gate reads {latest['sonar_gate']!r}, which "
                          "is not a status this check knows how to rule on")
    _round_stop_earned(c, latest, earned_stop)
    _round_warnings(c, latest)
    c.status = "failed" if c.reasons else "passed"
    return c


def _round_stop_earned(c: Check, latest: dict, earned_stop: bool) -> None:
    """`stop_confident: false` — a warning by default, a HOLD when asked for.

    The default is deliberate and unchanged. An unearned stop means a reviewer
    was truncated, or absent, or the judge was skipped, or the cap ran out — and
    holding on it would make a green verdict unreachable on exactly the headless
    boxes that run unattended, where two seats are permanently missing.

    `--require-earned-stop` is for the caller that just RAN the round and is
    about to offer to land on the strength of it (`/panel-review-pr` §7, #100).
    There, an unearned stop is not background noise about somebody else's box: it
    is this cycle reporting that nobody read the whole diff, and an offer to merge
    resting on it is an offer resting on nothing. Which of the two a run is doing
    is the caller's fact, not this file's, so it is a flag rather than a rule —
    and either way the vetoes are reported, so the strict mode changes what the
    verdict IS and never what the reader gets told.
    """
    if latest.get("stop_confident") is not False:
        return
    where = c.reasons if earned_stop else c.warnings
    for v in latest.get("stop_veto") or ["(the round recorded no veto reasons)"]:
        where.append(f"the stop was not earned: {v}")


def _round_warnings(c: Check, latest: dict) -> None:
    """What the round said that is worth reading but must not block."""
    if latest.get("unjudged"):
        c.warnings.append(f"{latest['unjudged']} finding(s) were never adjudicated" +
                          (f" ({latest['judge_skip']})" if latest.get("judge_skip") else ""))


def check_merge_claim(repo: str, pr: dict, mine: str) -> Check:
    """Is another agent already landing onto this PR's base?

    `kind=merge, key=<repo>:<branch>` shipped in #131 and nothing has ever read
    it — on the same day two agents merged at once. This is the read half. It
    does not TAKE the claim (see the module docstring), so `mine` is how a caller
    that already holds one says so: without it, the lander's own claim would hold
    its own merge.

    **The branch is the BASE, and #318 is why.** This read the HEAD branch until
    that issue asked which one the claim is supposed to be, and under a head key
    the incident in the paragraph above is *not* prevented: two agents landing two
    different PRs into `main` hold `<repo>:feat/a` and `<repo>:feat/b`, never see
    each other, and both merge. The head key catches only the narrower case of two
    agents landing the same PR, which is the rarer of the two and already unlikely
    once :func:`check_queue` exists. What a simultaneous merge collides on is the
    base — it is what every other open PR goes stale against afterwards, and #80
    measures that collision as quadratic in the open PRs.

    The audit #318 asked for is what made the change safe rather than merely
    right. `derive("branch", …)` is the only maker of merge keys and **every**
    branch claim in this fleet is a landing claim: `create-worktree` claims the
    ISSUE its branch names (the function called `claim_the_branch` resolves to an
    issue number), the plan's `ref_kind` is restricted to issue and pr, and
    `qb-hook`'s `kind: "branch"` post ref is an annotation on a board post that
    takes no claim at all. So the branch claim served no non-landing purpose whose
    meaning this could change out from under.

    It also removes a disagreement rather than creating one: the queue keys on
    `repo + base` through `app.claimkey`, and `GET /merge-queue` reports the claim
    it finds at that key. With this reading of the same land, a queue head that is
    told "take `kind=merge` on this base before you merge" takes the claim this
    check then reads. Under the old one the two named one land two ways, which is
    the #172 defect `merge_key`'s own docstring says it exists to prevent.

    Composed here rather than through `app.claimkey`, because this script runs out
    of `~/.claude/loops` on hosts with no checkout of this repo on the path. The
    shape is `derive`'s and the separator is `:`; the repo is already canonical
    (`resolve_repo` reads it off the remote) and the branch half is deliberately
    not case-folded there either.

    #142 inverted the authorisation this used to be hedged about: every kind in
    the claims table is session-owned now, so a co-tenant POSTing the same claim
    gets a 409 rather than a renew, and a `passed` here means "no other AGENT's
    claim is recorded" rather than "no other machine's".

    **An empty answer is warned about, not passed silently (#172).** The module
    docstring's own rule — *"a merge gate that fails open wherever it cannot see
    is not a gate"* — applies to the claims table as much as to a missing script.
    A repo where nothing is ever claimed is a repo where agents collide in
    silence, and `claims()` returned `[]` fleet-wide for four months while
    thirteen agents worked three shared checkouts. So "unclaimed" is reported as
    what it is: an answer this check cannot draw a conclusion from.
    """
    key = f"{repo}:{pr['baseRefName']}"
    body, err = board_get("claims", {"kind": "merge", "key": key})
    if err:
        c = Check("merge_claim", "error", "claims unreadable")
        c.reasons.append(f"{err}. Cannot tell whether another agent is landing "
                         f"{key!r}")
        return c
    claims = body.get("claims") if isinstance(body, dict) else None
    if not isinstance(claims, list):
        # An answer this cannot parse is not an empty claim set. Reading it as one
        # would report "unclaimed" about a namespace it never managed to look at.
        c = Check("merge_claim", "error", "claims unreadable")
        c.reasons.append("the board answered /claims with something that is not a "
                         f"claim list, so who holds {key!r} is unknown")
        return c
    others = [cl for cl in claims if cl.get("holder") != mine]
    c = Check("merge_claim", "passed", "unclaimed" if not claims else "claimed",
              detail={"key": key, "claims": claims})
    for other in others:
        c.reasons.append(
            f"{other.get('holder')} holds the merge claim on {key!r} since "
            f"{other.get('acquired') or '?'} ({other.get('note') or 'no note'}) "
            f"— it is landing onto {pr['baseRefName']} right now, and two merges "
            "into one base at once is what this claim exists to prevent")
    if claims and not others:
        c.summary = "held by you"
    if not claims:
        c.warnings.extend(_unclaimed_repo_warning(repo))
    c.status = "failed" if c.reasons else "passed"
    return c


def _unclaimed_repo_warning(repo: str) -> list[str]:
    """Why an empty claim answer is not evidence, when the repo claims nothing.

    A warning rather than a HOLD, deliberately: nothing here is wrong with the
    PR, and holding every merge in every repo that has not enrolled would make
    this the check people turn off. What it must not do is stay quiet — a `passed`
    that means "the table is empty" reads identically to one that means "nobody is
    landing this", and the first is the state #172 was filed about.

    Two questions, because a claim key does not always name a repo: the claims
    list answers it for issues, PRs and branches, and :func:`_plan_claims_here`
    answers it for the plan, whose keys are board ids with no repo in them at all.
    Either one is enough to say this repo claims things.

    Never raises and never blocks on a slow board: a failed read here simply says
    nothing extra, because the caller already has its own answer about the key.
    """
    body, err = board_get("claims", {"limit": "1000"})
    if err or not isinstance(body, dict):
        return []
    claims = body.get("claims")
    if not isinstance(claims, list):
        return []
    # A key on a REPO object is `<owner>/<name>` followed by a SEPARATOR, whatever
    # the kind — the board derives them all from the repo (`app/claimkey.py`), so
    # one prefix test covers issues, PRs and branches without this file
    # re-deriving the rule. The separator is required: `o/r` is a prefix of
    # `o/rx#1`, and reading a neighbour's claim as this repo's would silence the
    # warning in the one case it is for.
    heads = tuple(f"{repo.lower()}{sep}" for sep in "#!:")
    here = [c for c in claims
            if str(c.get("key") or "").lower().startswith(heads)]
    if here:
        return []
    # A key on a BOARD object says no repo at all, by design: a plan and an item
    # are keyed `plan:<uuid>` / `item:<uuid>` because a plan may span several
    # repos, so `repo_of` returns None for them and no prefix test can ever see
    # one. The plan-level claim is the *new* half of #172 — the agile intake, the
    # one coarse grain — so a repo whose agents claim their work that way was the
    # repo most likely to be told it claims nothing at all. The plan itself is
    # asked instead.
    if _plan_claims_here(repo):
        return []
    live = len(claims)
    return [
        f"nothing in {repo} is claimed by anybody right now"
        + (f" (the board holds {live} claim(s), all in other repos)" if live else
           " — and the board holds no claims at all, fleet-wide")
        + ". So `unclaimed` here is the absence of a record, not evidence that "
        "nobody else is landing onto this base. Agents in a repo that claims nothing "
        "collide in silence: enrol it (create-worktree takes the claim, "
        "`qb-claim issue <n>` by hand) or read this check as uninformative."
    ]


def _plan_claims_here(repo: str) -> int:
    """How much of THIS repo's plan is held right now. 0 if it cannot be read.

    The claims list cannot answer this. A plan claim is keyed `plan:<uuid>` and a
    ref-less item `item:<uuid>`, with no repo in either — that is deliberate
    (`app/claimkey.py`: a board object may span repos), and it means "is this repo
    enrolled" cannot be decided by looking at keys. Asking the plan is not a second
    implementation of the rule: the board does the attribution, off its own rows,
    the same way `GET /claim/held` does it for the keys that do carry a repo.

    `counts` rather than the item page, because `counts` is computed over the whole
    open set while `items` is a page of it — reading the page would make this
    answer "nothing is claimed" about the rows it did not fetch. And `exact`,
    because widening a repo read to the fleet-wide list would let another repo's
    plan silence this repo's warning, which is the mistake the separator test above
    exists to prevent, one scope up.

    Best-effort like its caller: a failed read returns 0 and the warning is printed
    on the safe side. A gate that turned an outage into "this repo is fine" would
    be the fail-open this whole check exists to close.
    """
    body, err = board_get("plan", {"repo": repo, "exact": "true", "limit": "1"})
    if err or not isinstance(body, dict):
        return 0
    held = 0
    counts = body.get("counts")
    if isinstance(counts, dict):
        # `claimed` is item by item; `covered` is inside a plan somebody holds as a
        # unit. Both are agents saying "this is mine" in this repo, which is the
        # only question here.
        for name in ("claimed", "covered"):
            value = counts.get(name)
            if isinstance(value, int) and value > 0:
                held += value
    plans = body.get("plans")
    if isinstance(plans, list):
        # A plan claimed while it is still being written has no claimed items yet,
        # and that is the moment the claim exists FOR: the surveying agent holds
        # the list before there is anything exact in it to hold.
        held += sum(1 for p in plans if isinstance(p, dict) and p.get("claim"))
    return held


def check_queue(repo: str, pr: dict) -> Check:
    """Is this PR at the head of the line to land on its base? — #227.

    The queue (#317) keys on ``repo`` + the **base** branch, because the base is
    the resource two landings collide on. It answers what `merge_claim`
    structurally cannot: that claim is one slot and says *somebody is landing
    right now*, never who is next — so every review-clean PR behaved as though it
    were. Merge the base, push, wait for CI, re-run this, discover somebody else
    landed, repeat: #80's quadratic integration cost, with each loser's push
    invalidating the winner's green checks on the way past.

    #317 built the board side and stopped there, saying so in as many words:
    *"nothing yet forces the stop"*. **This is the stop.** A PR behind another in
    the line is not READY, and the reason names its position and the entry it
    waits on, so the loop stands down before it spends the CI run rather than
    after.

    **It rules on POSITION and nothing else.** The board also reports whether an
    entry carries a ``ready`` verdict at this commit, and this must not gate on
    that: preland's own verdict is what produces that assertion, so holding for
    want of it would be this file refusing to run until it had already run. A
    head whose entry is behind the branch gets a WARNING carrying the board's own
    sentence, because re-enqueueing at this head is the caller's next step and it
    needs saying.

    **A queue nobody is in imposes nothing**, which is the other half of the
    contract. A lone PR on a base with an empty line passes without a word of
    friction: there is nobody to wait for, and a gate that made the ordinary case
    harder is a gate people turn off. What does NOT pass is a PR that never
    enqueued while others are queued — otherwise the way past this check is to
    skip the mechanism, which is #169's defect wearing the queue's clothes.

    **A board with no queue is a capability answer, not a failure.** The endpoint
    landed in #317 and a board deployed before it answers 404. That is the same
    fact as a repo with no ``scripts/migration_reconcile.py`` — the invariant does
    not exist there — and it reports ``skipped-absent`` for the same reason. Every
    other board failure is an ERROR: a line this gate cannot see is a line it
    cannot rule on, and the module docstring's rule about the ``review`` check
    applies here word for word, off-switch included.
    """
    base = pr["baseRefName"]
    body, err, status = board_request(
        QUEUE_PATH, {"repo": repo, "base": base, "pr": pr["number"],
                     "head": pr["headRefOid"]})
    if status == 404:
        return _queue_absent(base)
    if err:
        c = Check("queue", "error", "the queue is unreadable")
        c.reasons.append(
            f"{err}. Cannot tell whether another PR is ahead of this one in the "
            f"line to land on {base!r}, and absent must not read as clean. Turn "
            'this check off deliberately with `"preland": {"disabled_checks": '
            '["queue"]}` in .harness-rules if this board has no queue')
        return c
    you = body.get("you") if isinstance(body, dict) else None
    if not isinstance(you, dict):
        c = Check("queue", "error", "the queue is unreadable")
        c.reasons.append(
            f"the board answered /{QUEUE_PATH} with no `you` verdict in it, so "
            f"this PR's position in the line for {base!r} is unknown")
        return c
    order = body.get("active_order")
    order = order if isinstance(order, list) else []
    c = Check("queue", "passed",
              detail={"base": base, "active_order": order, "you": you})
    return _judge_place(c, you, order, pr, base)


def _queue_absent(base: str) -> Check:
    """A 404 on ``/merge-queue``: ruled on rather than believed.

    404 is the board saying "no such route", and for THIS route that is usually a
    capability answer — the endpoint landed in #317, and a board deployed before it
    has no line to read. But 404 is also what a base URL pointed at the wrong host
    returns, and what a proxy with no upstream returns, and reading either of those
    as "this board has no queue" would fail the gate open on exactly the
    misconfiguration it has no other way of noticing. A capability answer that
    cannot be told apart from an outage is not a capability answer.

    So the absence is corroborated, the way :func:`_detected` corroborates a
    missing guardrail script against the base rather than taking the branch's word
    for it: ask for a route that predates the queue by a long way. A board that
    answers ``/claims`` is a real board that simply does not have the queue yet;
    one that cannot answer that either is not a board this gate is talking to.
    """
    _, err, _status = board_request("claims", {"limit": "1"})
    if not err:
        return Check("queue", "skipped-absent",
                     f"this board has no landing queue (no /{QUEUE_PATH})")
    c = Check("queue", "error", "the queue is unreadable")
    c.reasons.append(
        f"the board answered 404 for /{QUEUE_PATH}, and /claims — which has existed "
        f"far longer — came back with: {err}. So this is not a board that merely "
        "predates the queue, and nothing here can tell whether another PR is ahead "
        f"of this one in the line to land on {base!r}")
    return c


def _judge_place(c: Check, you: dict, order: list, pr: dict, base: str) -> Check:
    """The verdict clause of :func:`check_queue`, over the board's own answer.

    The board wrote the sentence and this does not rewrite it: ``reason`` is
    quoted rather than paraphrased, so an agent reading this payload and an agent
    reading ``merge_queue`` are told the same thing in the same words. What is
    added is who holds the place ahead — "wait for #123" is something to act on
    only once you know which agent to go and ask.
    """
    if you.get("queued") and you.get("is_head"):
        c.summary = f"head of the line for {base} ({len(order)} queued)"
        if not you.get("may_merge"):
            c.warnings.append(
                f"{you.get('reason')} — this run is that re-check, so re-enqueue "
                "at this head once it comes back READY. Until you do, the line "
                "advertises a readiness about a commit nobody checked")
        return c
    if you.get("queued"):
        c.status = "failed"
        c.summary = f"position {you.get('position')} of {len(order)}"
        c.reasons.append(_waiting_sentence(you))
        return c
    if not order:
        # The lone PR, and the reason this check adds no friction to it. Said in
        # the summary rather than passed silently: a `passed` a reader cannot
        # distinguish from "the queue was never asked" is #172's shape again.
        c.summary = f"nobody is queued to land on {base}"
        return c
    c.status = "failed"
    c.summary = f"not in the line ({len(order)} queued)"
    c.reasons.append(
        f"#{pr['number']} is not in the queue for {base!r} and "
        f"{', '.join('#' + str(n) for n in order)} " +
        ("is" if len(order) == 1 else "are") + " — the line exists so that only "
        f"its head spends CI on integrating, and landing past #{order[0]} would "
        "invalidate its checks. Enqueue (`merge_queue_enqueue`), then run this "
        "again: joining the line is what makes your turn visible to everyone else")
    return c


def _waiting_sentence(you: dict) -> str:
    """The board's refusal, plus who holds the place ahead — and the one thing a
    caller must NOT do about it.

    Standing down keeps the entry. Leaving and re-joining is a trip to the back of
    the line, so a loop that read "stop" as "leave" would starve its own PR every
    time it was overtaken, which is worse than the racing it replaced.
    """
    on = you.get("waiting_on") if isinstance(you.get("waiting_on"), dict) else {}
    who = on.get("holder") or "an agent the board could not name"
    return (f"{you.get('reason')} — #{on.get('pr')} is held by {who} "
            f"({on.get('note') or 'no note'}). Stay queued: your place is kept "
            "while your entry is renewed, and leaving would re-join at the back")


def check_migrations(root: str, base: BaseRef, versions: str = "") -> Check:
    """Exactly one migration head after this lands, per the repo's own tool.

    The tool chooses the action and this never overrides it: relink vs merge
    turns on guards (multiple bases, a forked branch head, a base that is itself
    a merge node) that a caller re-deciding would be re-deciding blind.
    """
    script = "scripts/migration_reconcile.py"
    c = _detected(root, script, base, "migrations")
    if c is not None:
        return c
    argv = ["uv", "run", "python", script, "preflight", "--json",
            "--onto", base.ref, "--branch", "HEAD"]
    # The reconciler's own default is `migrations/versions`, and a repo whose
    # migrations live elsewhere would otherwise be analysed as having none —
    # which reports NOOP, the cleanest possible answer, about a directory nobody
    # looked in.
    if versions:
        argv += ["--versions-path", versions]
    proc = run(argv, cwd=root)
    try:
        plan = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        c = Check("migrations", "error", "the reconciler said nothing readable")
        c.reasons.append(f"`{shlex.join(argv)}` exited {proc.returncode} without JSON: "
                         f"{(proc.stderr or proc.stdout or '').strip()[:200]}")
        return c
    return _migration_plan(plan, base, proc.returncode)


def _migration_plan(plan: dict, base: BaseRef, code: int) -> Check:
    """The reconciler's `action`, as a check. One branch per action it can emit,
    and an unknown action HOLDs — a new action name must not read as noop."""
    action = (plan.get("action") or "").lower()
    c = Check("migrations", "passed", f"{action.upper() or 'UNKNOWN'}",
              detail={"action": action, "reason": plan.get("reason"), "exit_code": code,
                      "merged_single_head": plan.get("merged_single_head")})
    disagreement = _plan_disagrees(plan, action, code)
    if disagreement:
        c.status = "error"
        c.reasons.append(disagreement)
        return c
    if action == "noop":
        return c
    if action in ("relink", "renumber"):
        c.status = "reconcile"
        c.actions.append(Action(
            f"uv run python scripts/migration_reconcile.py apply --onto {base.quoted} "
            "--branch HEAD",
            plan.get("reason") or f"{action} the branch's migrations onto {base.ref}",
            _plan_files(plan)))
        c.actions.append(Action(
            "git add migrations/versions && git commit -m "
            f"'fix(migrations): rebase onto {base.name} head'",
            "apply writes the files and deliberately does not commit them"))
        return c
    if action == "merge":
        c.status = "reconcile"
        c.actions.append(Action(
            f"git merge {base.quoted}",
            "relink is unsafe here, so the two heads have to meet on the branch. "
            "HOLD instead if a conflict is not mechanically obvious — resolving "
            "product code by guess is the judgement this loop must not make"))
        c.actions.append(Action(
            # `alembic`, not `flask db` — the wrapper the prose inherited from a
            # Flask repo, which does not exist in an app that drives alembic
            # directly. Flask-Migrate delegates to this same command, so the
            # alembic form is the one that works in both.
            "uv run alembic merge heads -m 'merge branch and base heads'",
            "generate the merge migration, then re-run preland to re-verify one head"))
        return c
    c.status = "failed"
    c.reasons.append(
        (f"the reconciler says STOP: {plan.get('reason')}" if action == "stop"
         else f"the reconciler returned an action this check does not know ({action!r})")
        + f" — {base.name} must be reconciled first, and that is not this PR's job")
    return c


def _plan_disagrees(plan: dict, action: str, code: int) -> str:
    """Why the reconciler's plan and its exit code cannot both be trusted, or "".

    Two independent statements of the same answer, and reading only one of them
    is how a non-zero exit carrying a `noop` plan would have been accepted as
    clean. Compared against the plan's OWN `exit_code` rather than a copy of its
    action-to-code table, because a second copy of that table is a thing that
    drifts.
    """
    stated = plan.get("exit_code")
    if isinstance(stated, int) and stated != code:
        return (f"the reconciler's plan says exit {stated} and the process exited "
                f"{code} — the two disagree, so neither can be acted on")
    if stated is None and action == "noop" and code:
        return (f"the reconciler reported NOOP and exited {code} — a clean plan does "
                "not come back non-zero")
    return ""


def _detected(root: str, script: str, base: BaseRef, name: str) -> Check | None:
    """The capability answer for a repo-local guardrail, or None to carry on.

    Capability detection reads the BRANCH's tree, which is the whole point — but
    it also means a branch that DELETES the guardrail hands itself
    `skipped-absent`, switching off the check by the very diff the check exists
    to read. So an absence only counts as a skip when the base does not have the
    script either.
    """
    if (Path(root) / script).exists():
        return _stale(base, name)
    on_base = run(["git", "-C", root, "cat-file", "-e", f"{base.ref}:{script}"])
    if on_base.returncode:
        return Check(name, "skipped-absent", f"no {script} in this repo")
    c = Check(name, "failed", "the guardrail was removed")
    c.reasons.append(f"{base.ref} has {script} and this branch does not — a branch "
                     "cannot switch off the guardrail that is reading it")
    return c


def _stale(base: BaseRef, name: str) -> Check | None:
    """The check's answer when the base could not be refreshed, or None to run it.

    Only a FAILED fetch stops a check. `--no-fetch` is the caller's own choice and
    is noted once on the run instead (see :func:`payload`) — a warning repeated on
    every base-dependent check would be three copies of one fact.
    """
    if base.fresh or base.chosen:
        return None
    c = Check(name, "error", "the base ref is stale")
    c.reasons.append(f"{base.ref} could not be refreshed ({base.why}) — a verdict "
                     "computed against a stale base is confidently wrong in the "
                     "direction that lands")
    return c


def _plan_files(plan: dict) -> list[str]:
    """The migration files the plan rewrites, as the plan itself names them.

    Read off `base_path` and each rename's `old_path`/`new_path` — NOT off `base`
    or `new_down`, which are revision IDS. A revision id printed where a caller
    expects a filename reads as correct right up until someone tries to `git add`
    it, which is a debugging session bought for nothing.
    """
    files = [plan.get("base_path")]
    for rename in plan.get("renames") or []:
        if isinstance(rename, dict):
            files += [rename.get("old_path"), rename.get("new_path")]
    return sorted({f for f in files if isinstance(f, str) and f})


def check_sw_version(root: str, base: BaseRef) -> Check:
    """The service-worker cache-bust counter still goes up.

    One hand-maintained global that every branch edits, so parallel branches
    collide on merge and a careless resolution lands a value at or below what is
    deployed — which breaks cache invalidation silently rather than failing.
    """
    script = "scripts/check_sw_version.py"
    c = _detected(root, script, base, "sw_version")
    if c is not None:
        return c
    argv = ["uv", "run", "python", script, "--base", base.ref]
    proc = run(argv, cwd=root)
    said = ((proc.stdout or "") + (proc.stderr or "")).strip()
    c = Check("sw_version", "passed", said.splitlines()[-1][:120] if said else "OK",
              detail={"exit_code": proc.returncode})
    if not proc.returncode:
        return c
    # Exit 1 is the tool's "in violation, and --fix knows the remedy"; anything
    # above it is a case it declined to fix. The marker below is a HINT on top of
    # the exit code, not a contract with another repo's wording: a broken
    # multiline literal exits 1 and IS unfixable, and `--fix` would refuse.
    if proc.returncode == 1 and "no plain SERVICE_WORKER_VERSION" not in said:
        c.status = "reconcile"
        c.actions.append(Action(
            f"uv run python {script} --base {base.quoted} --fix",
            "rewrites the counter to max(base, head) + 1 — never take the "
            "branch's number blindly", ["the service-worker version file"]))
        c.actions.append(Action("git commit -am 'chore: bump service worker version'",
                                "the fix is not committed for you"))
        return c
    c.status = "failed"
    c.reasons.append(f"{script} exited {proc.returncode} and this is not the "
                     f"mechanically fixable case: {said[:200]}")
    return c


# ----------------------------------------------------------------------- gluing


def _git(root: str, *args: str) -> str | None:
    """git's stdout, or None if the command could not be run at all.

    None rather than "" on purpose: `git status --porcelain` says "clean" with an
    empty string, so a failure that returned one would be indistinguishable from
    the answer that lets a merge through.
    """
    try:
        return sh(["git", "-C", root, *args]).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def read_pr(repo: str, number: int | None, root: str) -> dict:
    """The one `gh` read every check shares. Asking once means the state, the
    head and the checks are all from the same instant — two reads a push apart
    would let this gate approve a head nobody looked at.

    Run from `root`, not from wherever this process happens to have been started.
    With `--pr` omitted, `gh` picks the PR from the CURRENT branch, and the
    current branch of the caller's shell is not the one `--repo` named — which is
    a verdict about the wrong PR, delivered with no sign anything was wrong.
    """
    fields = ("number,state,isDraft,mergeable,headRefOid,headRefName,"
              "baseRefName,title,url,statusCheckRollup")
    which = [str(number)] if number else []
    argv = ["gh", "pr", "view", *which, "--repo", repo, "--json", fields]
    try:
        return json.loads(sh(argv, cwd=root))
    except (OSError, subprocess.SubprocessError) as e:
        raise SystemExit(f"preland: could not read the PR from GitHub ({e})") from e
    except (json.JSONDecodeError, ValueError) as e:
        raise SystemExit(f"preland: `gh pr view` returned no usable JSON ({e})") from e


def refresh_base(root: str, name: str, fetch: bool = True) -> BaseRef:
    """Bring `origin/<name>` up to date, and say whether it worked.

    The one write this script makes, and it writes only a remote-tracking ref —
    no working tree, no branch. It is here because the alternative is worse than
    the side effect: the migration and cache-version guardrails are answers about
    the gap between this branch and the base, and a base last fetched yesterday
    produces a confident NOOP about a head that moved this morning.

    The result is RETURNED rather than discarded. A fetch whose failure went
    unread is the stale base with nothing saying so, which is the failure this
    call exists to prevent, arriving through the call itself.
    """
    if not fetch:
        return BaseRef(name, fresh=False, chosen=True)
    proc = run(["git", "-C", root, "fetch", "origin", name])
    if proc.returncode:
        why = (proc.stderr or proc.stdout or "").strip().splitlines()
        return BaseRef(name, fresh=False, why=why[-1][:160] if why else "no output")
    return BaseRef(name)


def disabled_checks(cfg: dict, skip: list[str]) -> dict[str, str]:
    """{check name: the status it reports} for every check turned off.

    A name nothing recognises is a hard exit, not a warning. `.harness-rules`
    warns-and-drops unknown keys elsewhere because a rules file shared across a
    fleet may legitimately name a setting only a newer harness knows — but a
    *value* here silently misspelled turns a merge gate's check off while reading
    as configured, and that is the one thing this file must not do.
    """
    from_rules = (cfg.get("preland") or {}).get("disabled_checks") or []
    if not isinstance(from_rules, list):
        raise SystemExit("preland: .harness-rules `preland.disabled_checks` must be "
                         "a list of check names")
    off = {str(n): "skipped-disabled" for n in from_rules}
    off.update({n: "skipped-flag" for n in skip})
    unknown = sorted(n for n in off if n not in CHECKS)
    if unknown:
        raise SystemExit(f"preland: no such check(s) {', '.join(map(repr, unknown))} — "
                         f"known: {', '.join(CHECKS)}")
    return off


def gather(cfg: dict, pr: dict, base: BaseRef, off: dict[str, str],
           mine: str = "", earned_stop: bool = False) -> list[Check]:
    """Every check, in report order, with the disabled ones still present.

    A check that did not run is REPORTED as not having run. Dropping it would
    leave a payload whose `checks` reads clean because the failing guardrail is
    simply not in it.
    """
    repo, root = cfg["github"], cfg["path"]
    versions = (cfg.get("epic") or {}).get("migrations_dir") or ""
    gate = MergeGate(root, distant_merge_lines(cfg.get("review_panel") or {}, []))
    how = {
        "pr_state": lambda: check_pr_state(pr),
        "checkout": lambda: check_checkout(root, pr),
        "ci": lambda: check_ci(pr),
        # Resolved OUTSIDE the lambda: a malformed `distant_merge_lines` is a hard
        # exit through `_refuse_value`, and it belongs where `disabled_checks`
        # already puts a bad rules value — before any check runs, not inside one
        # check's guard where it would read as that check failing.
        "review": lambda: check_review(repo, pr, earned_stop, gate),
        "merge_claim": lambda: check_merge_claim(repo, pr, mine),
        "queue": lambda: check_queue(repo, pr),
        "migrations": lambda: check_migrations(root, base, versions),
        "sw_version": lambda: check_sw_version(root, base),
    }
    return [Check(name, off[name], f"turned off ({off[name].split('-')[1]})")
            if name in off else _guarded(name, how[name])
            for name in CHECKS]


def _guarded(name: str, check: Callable[[], Check]) -> Check:
    """Run one check; turn a crash in it into that check's own HOLD.

    Broad on purpose, which is otherwise this repo's least favourite habit. The
    alternative is that a bug in one guardrail — an unexpected shape from `gh`, a
    key the board stopped sending — loses the WHOLE verdict to a traceback, and a
    loop that gets no verdict is a loop deciding for itself. One check failing
    closed and the other six still reporting is strictly the better outcome, and
    the exception text goes in the reason so it is a bug report rather than a
    shrug.
    """
    try:
        return check()
    except Exception as e:  # noqa: BLE001 — see the docstring
        c = Check(name, "error", "the check itself failed")
        c.reasons.append(f"the {name} check raised {e.__class__.__name__}: {e} — "
                         "that is a bug in preland, and it holds rather than "
                         "letting the merge through on a check that never ran")
        return c


def payload(cfg: dict, pr: dict, checks: list[Check], base: BaseRef) -> dict:
    """The machine-readable answer. `verdict`, `actions` and `reasons` are the
    three fields the issue asked for; `checks` is the audit trail that says which
    guardrails actually ran, which is the question prose could never answer."""
    v = verdict_of(checks)
    # Run-level, not per-check: `--no-fetch` is one fact about the run, and
    # repeating it on each base-dependent check would be three copies of it.
    run_notes = ([] if base.fresh or not base.chosen else
                 [f"{base.ref} was not refreshed (--no-fetch), so the migration and "
                  "cache-version verdicts read whatever the last fetch left there"])
    return {
        "verdict": v,
        "exit_code": EXIT[v],
        "repo": cfg["github"],
        "pr": pr["number"],
        "branch": pr["headRefName"],
        "base": base.name,
        "base_fresh": base.fresh,
        "head_sha": pr["headRefOid"],
        "reasons": [r for c in checks for r in c.reasons],
        "warnings": [*run_notes, *(w for c in checks for w in c.warnings)],
        "actions": [a.as_dict() for c in checks for a in c.actions],
        "checks": {c.name: c.as_dict() for c in checks},
    }


def report(out: dict, checks: list[Check]) -> None:
    """The human half. Same facts, same order, no extra judgement."""
    print(f"PR #{out['pr']}  {out['branch']} → {out['base']}  "
          f"head {out['head_sha'][:12]}")
    print(f"\nverdict: {out['verdict']}\n")
    for c in checks:
        print(f"  {c.name:<13} {c.status:<16} {c.summary}")
    for label, items in (("HOLD — unresolved", out["reasons"]),
                         ("noted", out["warnings"])):
        if items:
            print(f"\n{label}:")
            for item in items:
                print(f"  - {item}")
    if out["actions"]:
        print("\nRECONCILE — run these, then run preland again:")
        for a in out["actions"]:
            print(f"  $ {a['command']}")
            print(f"      why: {a['why']}")
            if a["files"]:
                print(f"      touches: {', '.join(a['files'])}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="The mechanical pre-land verdict: READY (0) / RECONCILE (3) / HOLD (2)")
    ap.add_argument("--pr", type=int,
                    help="PR number (default: the PR for the current branch)")
    ap.add_argument("--repo", help="repo path, or a name under ~/source (default: cwd)")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="emit the verdict payload instead of the report")
    ap.add_argument("--skip", action="append", default=[], metavar="CHECK",
                    help=f"do not run this check, and say so ({', '.join(CHECKS)}). "
                         "A CI job calling this must pass `--skip ci`")
    ap.add_argument("--no-fetch", action="store_true",
                    help="do not refresh the base branch's remote-tracking ref first")
    ap.add_argument("--claim-holder", default="", metavar="AGENT",
                    help="a merge claim held by this identity is yours, not a conflict")
    ap.add_argument("--require-earned-stop", action="store_true", dest="earned_stop",
                    help="HOLD when the round's stop was not earned (stop_confident: "
                         "false) instead of warning about it. For a caller that ran "
                         "the round itself and is about to offer to land on it")
    args = ap.parse_args(argv)
    if args.pr is not None and args.pr < 1:
        ap.error("--pr: pull requests are numbered from 1")

    try:
        cfg = resolve_repo(args.repo)
    except RepoNotFound as e:
        raise SystemExit(str(e)) from e
    off = disabled_checks(cfg, args.skip)
    pr = read_pr(cfg["github"], args.pr, cfg["path"])
    base = refresh_base(cfg["path"], pr["baseRefName"], fetch=not args.no_fetch)

    checks = gather(cfg, pr, base, off, args.claim_holder, args.earned_stop)
    out = payload(cfg, pr, checks, base)
    if args.as_json:
        print(json.dumps(out, indent=2))
    else:
        print(describe(cfg))
        report(out, checks)
    return out["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
