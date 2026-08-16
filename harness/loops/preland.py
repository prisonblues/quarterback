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
    HOLDs when another agent holds the branch, but it does not TAKE that claim —
    that is #100's, where the merge actually happens. The distinction is between
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
    artefact. And ``stop_confident: false`` is a WARN, not a HOLD: two
    permanently-absent reviewer seats on a headless box would otherwise make a
    green verdict unreachable, which is the noise-for-signal trade
    ``.harness-rules`` already argues against for ``coverage_veto``.

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

READY, RECONCILE, HOLD = "READY", "RECONCILE", "HOLD"

#: Verdict -> process exit code. Deliberately `migration_reconcile.py`'s.
EXIT = {READY: 0, RECONCILE: 3, HOLD: 2}

#: Every check this script knows, in report order. Named here rather than
#: collected from the functions so that `--skip nonsense` and a typo in
#: `.harness-rules` are both hard errors: a gate silently running one check short
#: is the failure mode the whole file exists to prevent.
CHECKS = ("pr_state", "checkout", "ci", "review", "merge_claim",
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
    """(parsed JSON, error) from the board. Never raises: an unreachable board is
    a fact this script REPORTS, and reporting it is what makes it a HOLD rather
    than an exception nobody mapped to a verdict."""
    url, token, why = board_config()
    if why:
        return None, why
    full = f"{url}/{path.lstrip('/')}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=BOARD_TIMEOUT) as r:
            return json.loads(r.read().decode()), ""
    except urllib.error.HTTPError as e:
        # Before URLError: it is a subclass, and an HTTP status says far more
        # than "unreachable" — 401 in particular is a token problem, not a board
        # that is down, and the two want different remedies.
        hint = " — the token this host resolved was refused" if e.code == 401 else ""
        return None, f"board answered HTTP {e.code} for /{path.lstrip('/')}{hint}"
    except OSError as e:
        # OSError, not URLError: it covers URLError and TimeoutError both, plus a
        # connection reset partway through the read, which is an OSError that
        # urllib does not wrap. This function's contract is that it never raises,
        # and a narrow tuple is how that contract quietly stops being true.
        return None, f"board unreachable at {url} ({e.__class__.__name__})"
    except ValueError:
        # JSONDecodeError, and a body that is not valid UTF-8.
        return None, f"board answered /{path.lstrip('/')} with something that is not JSON"


# ------------------------------------------------------------------- the checks


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
    mergeable = (pr.get("mergeable") or "UNKNOWN").upper()
    if mergeable == "CONFLICTING":
        c.reasons.append("GitHub reports the branch as CONFLICTING — resolve the "
                         "conflicts by hand; guessing a resolution is not mechanical")
    elif mergeable != "MERGEABLE":
        c.warnings.append(f"GitHub has not computed mergeability yet ({mergeable}); "
                          "a conflict would fail the merge loudly rather than land")
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
        c.warnings.append("the PR has no checks at all — nothing mechanical is "
                          "verifying this change")
    c.status = "failed" if c.reasons else "passed"
    return c


def check_review(repo: str, pr: dict) -> Check:
    """The panel's own statements about the round that read THIS commit.

    Every clause here is a field the round wrote about itself. None of them is a
    proxy, and none of them is re-derived: the panel already decided whether it
    stopped and why, and this reads that decision rather than forming a second
    opinion about the same diff.
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
    if not isinstance(rows, list) or not rows:
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
    return _judge_round(c, newest, pr)


def _judge_round(c: Check, latest: dict, pr: dict) -> Check:
    """The clause list, against the newest round the board holds for this PR."""
    c.detail = {k: latest.get(k) for k in
                ("id", "ts", "round", "cycle", "head_sha", "stopped", "stop_reason",
                 "stop_confident", "confirmed", "unjudged", "sonar_gate", "judge_skip")}
    c.summary = (f"round {latest.get('round')} of cycle "
                 f"{(latest.get('cycle') or '?')[:8]} at "
                 f"{(latest.get('ts') or '?')[:19]}")
    recorded = latest.get("head_sha")
    if not recorded:
        c.reasons.append("the round recorded no head_sha, so there is no way to tell "
                         "which commit it read — re-run the panel on this head")
    elif recorded != pr["headRefOid"]:
        c.reasons.append(f"the round read {recorded[:12]}, the PR's head is now "
                         f"{pr['headRefOid'][:12]} — it is a review of earlier code")
    if latest.get("stopped") is not True:
        c.reasons.append("the round did not stop: " +
                         (latest.get("stop_reason") or "it recorded no stopping verdict"))
    confirmed = latest.get("confirmed") or 0
    if confirmed:
        c.reasons.append(f"{confirmed} judge-confirmed finding(s) are unresolved")
    gate = (latest.get("sonar_gate") or "").upper()
    if gate == "ERROR":
        c.reasons.append("the SonarCloud quality gate is failing — it is a hard gate")
    elif gate not in ("", "OK", "SKIPPED", "NO-ANALYSIS", "NO-PR-ANALYSIS", "NONE"):
        c.warnings.append(f"the SonarCloud gate reads {latest['sonar_gate']!r}, which "
                          "is not a status this check knows how to rule on")
    _round_warnings(c, latest)
    c.status = "failed" if c.reasons else "passed"
    return c


def _round_warnings(c: Check, latest: dict) -> None:
    """What the round said that is worth reading but must not block.

    `stop_confident: false` is the deliberate one. It means the stop was not
    earned — a reviewer was truncated, or absent, or the judge was skipped — and
    holding on it would make a green verdict unreachable on exactly the headless
    boxes that run unattended, where two seats are permanently missing. So the
    vetoes are printed and the merge is not blocked.
    """
    if latest.get("stop_confident") is False:
        for v in latest.get("stop_veto") or ["(the round recorded no veto reasons)"]:
            c.warnings.append(f"the stop was not earned: {v}")
    if latest.get("unjudged"):
        c.warnings.append(f"{latest['unjudged']} finding(s) were never adjudicated" +
                          (f" ({latest['judge_skip']})" if latest.get("judge_skip") else ""))


def check_merge_claim(repo: str, pr: dict, mine: str) -> Check:
    """Is another agent already landing this branch?

    `kind=merge, key=<repo>:<branch>` shipped in #131 and nothing has ever read
    it — on the same day two agents merged at once. This is the read half. It
    does not TAKE the claim (see the module docstring), so `mine` is how a caller
    that already holds one says so: without it, the lander's own claim would hold
    its own merge.

    How much a `passed` here is worth depends on #142. The read is exact — the
    holder is a full `machine/name`, so a co-tenant's claim is visibly not yours
    — but `_may_mutate` currently session-scopes only `kind='release'` and
    authorises every other kind by MACHINE, so a second agent on this box that
    POSTs the same claim RENEWS it instead of getting a 409. Until that inverts,
    this says "no other agent's claim is recorded", which is weaker than "no
    other agent can land". Weaker is still the first thing to read it at all.
    """
    key = f"{repo}:{pr['headRefName']}"
    body, err = board_get("claims", {"kind": "merge", "key": key})
    if err:
        c = Check("merge_claim", "error", "claims unreadable")
        c.reasons.append(f"{err}. Cannot tell whether another agent is landing "
                         f"{key!r}")
        return c
    claims = (body or {}).get("claims", []) if isinstance(body, dict) else []
    others = [cl for cl in claims if cl.get("holder") != mine]
    c = Check("merge_claim", "passed", "unclaimed" if not claims else "claimed",
              detail={"key": key, "claims": claims})
    for other in others:
        c.reasons.append(
            f"{other.get('holder')} holds the merge claim on {key!r} since "
            f"{other.get('acquired') or '?'} ({other.get('note') or 'no note'}) "
            "— it is landing this branch")
    if claims and not others:
        c.summary = "held by you"
    c.status = "failed" if c.reasons else "passed"
    return c


def check_migrations(root: str, base: str) -> Check:
    """Exactly one migration head after this lands, per the repo's own tool.

    The tool chooses the action and this never overrides it: relink vs merge
    turns on guards (multiple bases, a forked branch head, a base that is itself
    a merge node) that a caller re-deciding would be re-deciding blind.
    """
    script = "scripts/migration_reconcile.py"
    if not (Path(root) / script).exists():
        return Check("migrations", "skipped-absent", f"no {script} in this repo")
    argv = ["uv", "run", "python", script, "preflight", "--json",
            "--onto", f"origin/{base}", "--branch", "HEAD"]
    proc = run(argv, cwd=root)
    try:
        plan = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        c = Check("migrations", "error", "the reconciler said nothing readable")
        c.reasons.append(f"`{shlex.join(argv)}` exited {proc.returncode} without JSON: "
                         f"{(proc.stderr or proc.stdout or '').strip()[:200]}")
        return c
    return _migration_plan(plan, base)


def _migration_plan(plan: dict, base: str) -> Check:
    """The reconciler's `action`, as a check. One branch per action it can emit,
    and an unknown action HOLDs — a new action name must not read as noop."""
    action = (plan.get("action") or "").lower()
    c = Check("migrations", "passed", f"{action.upper() or 'UNKNOWN'}",
              detail={"action": action, "reason": plan.get("reason"),
                      "merged_single_head": plan.get("merged_single_head")})
    onto = f"origin/{base}"
    if action == "noop":
        return c
    if action in ("relink", "renumber"):
        c.status = "reconcile"
        c.actions.append(Action(
            f"uv run python scripts/migration_reconcile.py apply --onto {onto} "
            "--branch HEAD",
            plan.get("reason") or f"{action} the branch's migrations onto {onto}",
            _plan_files(plan)))
        c.actions.append(Action(
            "git add migrations/versions && git commit -m "
            f"'fix(migrations): rebase onto {base} head'",
            "apply writes the files and deliberately does not commit them"))
        return c
    if action == "merge":
        c.status = "reconcile"
        c.actions.append(Action(
            f"git merge {onto}",
            "relink is unsafe here, so the two heads have to meet on the branch. "
            "HOLD instead if a conflict is not mechanically obvious — resolving "
            "product code by guess is the judgement this loop must not make"))
        c.actions.append(Action(
            "uv run flask db merge heads -m 'merge branch and base heads'",
            "generate the merge migration, then re-run preland to re-verify one head"))
        return c
    c.status = "failed"
    c.reasons.append(
        (f"the reconciler says STOP: {plan.get('reason')}" if action == "stop"
         else f"the reconciler returned an action this check does not know ({action!r})")
        + f" — {base} must be reconciled first, and that is not this PR's job")
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


def check_sw_version(root: str, base: str) -> Check:
    """The service-worker cache-bust counter still goes up.

    One hand-maintained global that every branch edits, so parallel branches
    collide on merge and a careless resolution lands a value at or below what is
    deployed — which breaks cache invalidation silently rather than failing.
    """
    script = "scripts/check_sw_version.py"
    if not (Path(root) / script).exists():
        return Check("sw_version", "skipped-absent", f"no {script} in this repo")
    argv = ["uv", "run", "python", script, "--base", f"origin/{base}"]
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
            f"uv run python {script} --base origin/{base} --fix",
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


def gather(cfg: dict, pr: dict, off: dict[str, str], mine: str) -> list[Check]:
    """Every check, in report order, with the disabled ones still present.

    A check that did not run is REPORTED as not having run. Dropping it would
    leave a payload whose `checks` reads clean because the failing guardrail is
    simply not in it.
    """
    repo, root, base = cfg["github"], cfg["path"], pr["baseRefName"]
    how = {
        "pr_state": lambda: check_pr_state(pr),
        "checkout": lambda: check_checkout(root, pr),
        "ci": lambda: check_ci(pr),
        "review": lambda: check_review(repo, pr),
        "merge_claim": lambda: check_merge_claim(repo, pr, mine),
        "migrations": lambda: check_migrations(root, base),
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


def payload(cfg: dict, pr: dict, checks: list[Check]) -> dict:
    """The machine-readable answer. `verdict`, `actions` and `reasons` are the
    three fields the issue asked for; `checks` is the audit trail that says which
    guardrails actually ran, which is the question prose could never answer."""
    v = verdict_of(checks)
    return {
        "verdict": v,
        "exit_code": EXIT[v],
        "repo": cfg["github"],
        "pr": pr["number"],
        "branch": pr["headRefName"],
        "base": pr["baseRefName"],
        "head_sha": pr["headRefOid"],
        "reasons": [r for c in checks for r in c.reasons],
        "warnings": [w for c in checks for w in c.warnings],
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
    args = ap.parse_args(argv)
    if args.pr is not None and args.pr < 1:
        ap.error("--pr: pull requests are numbered from 1")

    try:
        cfg = resolve_repo(args.repo)
    except RepoNotFound as e:
        raise SystemExit(str(e)) from e
    off = disabled_checks(cfg, args.skip)
    pr = read_pr(cfg["github"], args.pr, cfg["path"])
    if not args.no_fetch:
        # Remote-tracking refs only. A migration or cache-version verdict against
        # a stale origin/<base> is confidently wrong in the direction that lands.
        run(["git", "-C", cfg["path"], "fetch", "origin", pr["baseRefName"]])

    checks = gather(cfg, pr, off, args.claim_holder)
    out = payload(cfg, pr, checks)
    if args.as_json:
        print(json.dumps(out, indent=2))
    else:
        print(describe(cfg))
        report(out, checks)
    return out["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
