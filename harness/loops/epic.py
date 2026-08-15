#!/usr/bin/env python3
"""Epic driver — work an epic's issues end-to-end (Loop C, over a work list).

Point it at an epic issue; it decomposes the epic into sub-issues, figures out
each one's stage (done / has-PR-needs-review / not-started), and for each runs
the per-issue pipeline:

    create worktree -> /fix-issue (implement+PR) -> CI -> reviewer panel
      -> /review-pr (address findings) -> loop until green -> STOP at human merge

Merge is NEVER automatic here (epic = substantive feature work on a real-user
repo). It stops at "green + reviewed + PR open" per the repo's auto_merge policy;
a human merges. Recurses to the next issue respecting listed order.

REPORT-ONLY by default (prints the plan). --execute runs the pipeline.

Usage:
    python3 ~/.claude/loops/epic.py --epic 758
    python3 ~/.claude/loops/epic.py --epic 758 --json   # structured plan
    python3 ~/.claude/loops/epic.py --epic 758 --execute --max-issues 1
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness_rules import (RepoNotFound, cli_failure_gist, describe,  # noqa: E402
                           resolve_repo)

PANEL = Path(__file__).with_name("panel.py")
# State must live OUTSIDE the script dir for the same reason (the store is read-only).
STATE_DIR = Path(os.environ.get("LOOPS_STATE_DIR")
                 or Path.home() / ".local/state/loops")
# A body line that references a sub-issue. Tolerant of how epics actually write them:
#   "- [x] #761 — ...", "1. #931", "* #866", "**#939 — A.**", "  #870" — optional list
# marker (bullet or "1." / "1)"), optional checkbox, optional bold, then "#NNN".
BODY_REF_RE = re.compile(
    r"^\s*(?:[-*+]|\d+[.)])?\s*(?:\[(?P<chk>[ xX])\]\s*)?\*{0,2}\s*#(?P<num>\d+)\b")
CLOSES_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs?)\s+#(\d+)", re.I)
# a whole-line bold header that groups the sub-issues below it into a phase/layer,
# e.g. "**Foundation**" / "**Read, serve & diff**" — the orchestrator's fan-out unit.
PHASE_RE = re.compile(r"^\s*\*\*(?P<name>[^*].*?)\*\*\s*$")
# a dependency stated in a sub-issue body — "depends on #931", "after #939",
# "blocked by #864", "builds on #862", "once #861 lands". Used to detect coupling.
DEP_RE = re.compile(
    r"\b(?:depends?\s+on|after|blocked\s+by|requires?|builds?\s+on|once)\b[^\n#]*?#(\d+)",
    re.I)

TRIAGE_PROMPT = """You are the lead engineer triaging issue #{n} from an epic. Make two judgments:

1. DOABLE — decide whether a CODING AGENT can actually implement it — i.e. accomplish it by
writing/editing code in this repository and opening a PR. Answer false for anything that needs a
human: obtaining a licence/contract, a business/legal/commercial decision, manual ops or
infra/credential access, an external vendor action, design sign-off, or anything not achievable
by editing this repo.

2. MODEL — pick the cheapest model tier that can implement this issue WELL, from: {models}
(listed weakest to strongest; the strongest listed is your own tier — never pick above it). Guide:
- sonnet: small, well-specified, mechanical — single-surface UI tweaks, adding a column/filter,
  copy/config changes, CRUD that follows an existing pattern in the repo.
- opus: standard feature work — multi-file changes, a new endpoint + model + migration + tests,
  real engineering judgment but with a clear spec.
- fable: the hardest — cross-cutting schema/engine changes, concurrency/idempotency subtleties,
  ambiguous specs that need design judgment.

Return ONLY JSON: {{"doable": true|false, "reason": "<one short line>", "model": "<tier>"}}

Issue #{n}: {title}

{body}
"""

# Ascending capability tiers a sub-issue may be implemented on. haiku is deliberately
# excluded — the floor for epic implementation work is sonnet.
MODEL_TIERS = ["sonnet", "opus", "fable"]


def allowed_models(ceiling: str) -> list[str]:
    """Tiers the judge may assign to sub-issues: its own tier or lesser. Empty when
    the run wasn't initiated with a recognised tier (model routing then stays off)."""
    if ceiling not in MODEL_TIERS:
        return []
    return MODEL_TIERS[:MODEL_TIERS.index(ceiling) + 1]


def clamp_model(picked: str, ceiling: str) -> str:
    """The judge's pick, clamped: unknown/absent/above-ceiling -> the ceiling itself
    (fail toward capability, not cheapness)."""
    allowed = allowed_models(ceiling)
    if not allowed:
        return ""
    return picked if picked in allowed else ceiling


@dataclass
class IssueWork:
    num: int
    title: str
    checked: bool          # epic task-list checkbox ticked
    issue_state: str       # OPEN | CLOSED
    pr_number: int | None
    pr_state: str | None   # OPEN | MERGED | CLOSED | None
    stage: str             # done | review | implement | blocked
    body: str = ""
    doable: bool | None = None   # master triage verdict (implement-stage only)
    reason: str = ""             # triage reason
    phase: str = ""              # grouping header from the epic body (fan-out layer)
    model: str = ""              # implementation tier the judge picked (<= run ceiling)


def gh_json(args: list[str], repo: str):
    out = subprocess.run(["gh", *args, "--repo", repo],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out) if out.strip() else []


def load_repo_cfg(name: str) -> dict:
    try:
        return resolve_repo(name)
    except RepoNotFound as e:
        sys.exit(str(e))


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ----------------------------------------------------------------- run state
# A per-run state file (~/.local/state/loops/epic-<repo>-<epic>.json) records each issue's
# stage/branch/PR/merged status so --execute is safely re-runnable: a killed or
# resumed run reads it instead of re-deriving everything from GitHub. Git ancestry
# stays the authoritative 'merged' signal; the file is the fast path + audit trail.

def load_state(repo: str, epic: int) -> dict:
    p = STATE_DIR / f"epic-{repo}-{epic}.json"
    if not p.exists():
        return {"epic": epic, "repo": repo, "issues": {}}
    try:
        st = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {"epic": epic, "repo": repo, "issues": {}}
    # Tolerate the legacy hand-written list schema (e.g. the #859 archive): index by num.
    if isinstance(st.get("issues"), list):
        st["issues"] = {str(i["num"]): i for i in st["issues"] if "num" in i}
    st.setdefault("issues", {})
    return st


def save_state(state: dict) -> None:
    state["updated"] = now_iso()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = STATE_DIR / f"epic-{state['repo']}-{state['epic']}.json"
    p.write_text(json.dumps(state, indent=2))


def record(state: dict, num: int, **fields) -> None:
    """Upsert one issue's record and persist immediately (crash-safe checkpoint)."""
    rec = state["issues"].setdefault(str(num), {"num": num})
    rec.update(fields)
    rec["ts"] = now_iso()
    save_state(state)


def native_subissues(repo: str, epic: int) -> list[int]:
    """Authoritative, ordered sub-issue numbers from GitHub's native sub-issue
    links (the relationship the UI shows under 'Sub-issues'). Empty if the epic
    has none registered or the API is unavailable — callers fall back to the body."""
    try:
        out = subprocess.run(
            ["gh", "api", "--paginate", f"repos/{repo}/issues/{epic}/sub_issues",
             "--jq", ".[].number"],
            capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, OSError):
        return []
    return [int(x) for x in out.split()]


def decompose(repo: str, epic: int) -> list[tuple[int, bool, str]]:
    """Return [(issue_number, checked, phase)] for the epic's sub-issues, de-duplicated.

    Sources, unioned: (1) GitHub **native sub-issues** (authoritative, ordered) and
    (2) issue refs scraped from the epic body (checklist / numbered / bold / bare
    "#NNN" lines). Native order leads; body-only refs follow. `checked` and `phase`
    (nearest preceding bold header — the fan-out layer) come from the body when the
    issue appears there. Native-only issues get checked=False, phase=""."""
    body = gh_json(["issue", "view", str(epic), "--json", "body"], repo).get("body", "")
    phase, body_order = "", []
    phase_map, checked_map = {}, {}
    for line in body.splitlines():
        ph = PHASE_RE.match(line)
        if ph:
            phase = ph.group("name").strip()
            continue
        m = BODY_REF_RE.match(line)
        if not m:
            continue
        num = int(m.group("num"))
        if num == epic or num in phase_map:
            continue
        phase_map[num] = phase
        checked_map[num] = (m.group("chk") or " ").lower() == "x"
        body_order.append(num)

    seen, out = set(), []
    for num in native_subissues(repo, epic) + body_order:
        if num in seen or num == epic:
            continue
        seen.add(num)
        out.append((num, checked_map.get(num, False), phase_map.get(num, "")))
    return out


def find_pr_for_issue(prs: list[dict], num: int) -> dict | None:
    """Match an existing PR to an issue by head branch (…issue-N) or a closing ref."""
    for pr in prs:
        if re.search(rf"issue-{num}\b", pr.get("headRefName", "")):
            return pr
        for ref in CLOSES_RE.findall(pr.get("body") or ""):
            if int(ref) == num:
                return pr
    return None


def git(path: str, *args: str) -> subprocess.CompletedProcess:
    """Run a git command in `path`, capturing output; never raises on non-zero."""
    return subprocess.run(["git", "-C", path, *args],
                          capture_output=True, text=True)


def git_ok(path: str, *args: str) -> bool:
    return git(path, *args).returncode == 0


def ref_exists(path: str, ref: str) -> bool:
    return git_ok(path, "rev-parse", "--verify", "--quiet", ref)


def is_ancestor(path: str, ancestor: str, descendant: str) -> bool:
    """True iff `ancestor` is reachable from `descendant` (both must resolve)."""
    if not (ref_exists(path, ancestor) and ref_exists(path, descendant)):
        return False
    return git_ok(path, "merge-base", "--is-ancestor", ancestor, descendant)


def sub_branch_merged(cfg: dict, branch: str, epic_branch: str) -> bool:
    """True if the sub-issue branch's commits already live in the epic branch —
    a git-ancestry 'done' signal independent of PR/issue state (idempotent resume).
    Prefers the remote refs (authoritative); falls back to local. Requires a prior
    `git fetch` so origin/* are current.

    NOT SUFFICIENT ON ITS OWN — the caller must corroborate with a PR. After a
    successful ff-merge the epic tip *equals* the sub-branch tip; a freshly created,
    zero-commit sub-branch *also* equals the epic tip. Those two states are
    byte-identical in git, so no refinement of this test can tell "fully merged" from
    "no work at all". Asking it to is what silently skipped the foundational issue of
    EPIC #1516 (2026-08-13): a killed run left `feat/issue-1517` at the epic tip with
    no commits, the next run read the ancestry as done, and #1518-#1521 were queued to
    build against a package entity that had never been created."""
    path = cfg["path"]
    for sub in (f"origin/{branch}", branch):
        if not ref_exists(path, sub):
            continue
        for epic in (f"origin/{epic_branch}", epic_branch):
            if ref_exists(path, epic):
                return is_ancestor(path, sub, epic)
    return False


def classify(issue: dict, pr: dict | None, checked: bool, merged: bool = False) -> str:
    if merged or checked or issue["state"] == "CLOSED" or (pr and pr["state"] == "MERGED"):
        return "done"
    if pr and pr["state"] == "OPEN":
        return "review"
    return "implement"


def build_worklist(repo: str, epic: int) -> list[IssueWork]:
    refs = decompose(repo, epic)
    prs = gh_json(["pr", "list", "--state", "all", "--limit", "200",
                   "--json", "number,state,headRefName,body"], repo)
    work = []
    for num, checked, phase in refs:
        issue = gh_json(["issue", "view", str(num), "--json", "title,state,body"], repo)
        pr = find_pr_for_issue(prs, num)
        work.append(IssueWork(
            num=num, title=issue.get("title", "?"), checked=checked,
            issue_state=issue.get("state", "?"),
            pr_number=pr["number"] if pr else None,
            pr_state=pr["state"] if pr else None,
            stage=classify(issue, pr, checked),
            body=issue.get("body", "") or "", phase=phase))
    return work


def slugify(text: str, maxlen: int = 32) -> str:
    """Kebab slug for an epic branch name, e.g. 'Person model: collapse…' ->
    'person-model-collapse'. Truncates on a word boundary, never mid-word."""
    words = re.sub(r"[^a-z0-9]+", " ", text.lower()).split()
    out = ""
    for w in words:
        cand = f"{out}-{w}" if out else w
        if len(cand) > maxlen:
            break
        out = cand
    return out or (words[0][:maxlen] if words else "epic")


def epic_branch_name(epic: int, title: str, override: str | None) -> str:
    return override or f"epic/{epic}-{slugify(title)}"


def suggest_landing(work: list[IssueWork]) -> dict:
    """Compute coupling signals over the worklist and a *suggested* landing strategy.

    The signals — inter-issue dependency refs, phase structure, issue count — are
    advisory: epic.py emits them, the /epic master makes the final call (and only
    interviews the user when it's genuinely borderline). 'integration' is suggested
    when sub-issues are coupled (they reference each other as deps, or there's a
    sizeable flat list with no phase structure); 'multi' otherwise."""
    nums = {w.num for w in work}
    edges = sorted({(w.num, int(d)) for w in work
                    for d in DEP_RE.findall(w.body)
                    if int(d) in nums and int(d) != w.num})
    phases = sorted({w.phase for w in work if w.phase})
    n = len(work)
    coupled = bool(edges) or (n >= 4 and len(phases) <= 1)
    reasons = []
    if edges:
        reasons.append(f"{len(edges)} inter-issue dependency ref(s)")
    if n >= 4 and len(phases) <= 1:
        reasons.append(f"{n} sub-issues, no phase structure (a flat coupled list)")
    if phases:
        reasons.append(f"{len(phases)} phase(s): {', '.join(phases)}")
    if not reasons:
        reasons.append(f"{n} sub-issue(s), independent")
    return {"suggested": "integration" if coupled else "multi",
            "edges": [list(e) for e in edges], "phases": phases, "reasons": reasons}


def toposort(work: list[IssueWork], edges: list) -> list[IssueWork]:
    """Order the worklist so a dependency runs before its dependent. `edges` are
    (a, b) = 'a depends on b' (as produced by suggest_landing). Stable: ties keep
    input order; a cycle or dangling edge never drops an item (the remainder is
    emitted in input order). This serialises integration mode so each issue forks
    from a branch that already contains what it builds on."""
    order = {w.num: i for i, w in enumerate(work)}
    by_num = {w.num: w for w in work}
    deps: dict[int, set] = {w.num: set() for w in work}
    for a, b in edges:
        if a in deps and b in deps and a != b:
            deps[a].add(b)
    out, done, remaining = [], set(), sorted(deps, key=order.get)
    while remaining:
        ready = [n for n in remaining if deps[n] <= done] or remaining  # cycle: force
        for n in sorted(ready, key=order.get):
            out.append(n)
            done.add(n)
        remaining = [n for n in remaining if n not in done]
    return [by_num[n] for n in out]


# How long the triage judge may take per sub-issue. Named rather than inline
# because the skip line quotes it: "untriaged (judge timed out after 300s)" has
# to stay true if the number moves.
TRIAGE_TIMEOUT = 300


def triage(w: IssueWork, model: str) -> tuple[bool | None, str, str]:
    """Master decides whether a coding agent can actually implement this issue, and
    which model tier should implement it. The judge runs at `model` — the tier the
    epic run was initiated with — and picks an equal-or-lesser tier per sub-issue
    (clamped; see MODEL_TIERS). Returns (doable, reason, impl_model). doable=None
    means no judgment was possible (treated as 'not confirmed' → skipped on
    --execute); impl_model='' means model routing is off for this run."""
    if not shutil.which("claude"):
        return None, "untriaged (no judge available)", ""
    choices = allowed_models(model)
    prompt = TRIAGE_PROMPT.format(n=w.num, title=w.title, body=w.body[:6000],
                                  models=", ".join(choices) or "(default)")
    args = ["claude", "-p", prompt] + (["--model", model] if model else [])
    # Every failure below silently skips the sub-issue on --execute, so the one
    # line the operator gets has to name a cause: the judge CLI can exit 0 having
    # printed nothing (a tool permission headless mode auto-denied, an unusable
    # model pin) and say why on stderr, which a bare "no verdict" threw away —
    # and a launch that never ran at all knows why too.
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=TRIAGE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, f"untriaged (judge timed out after {TRIAGE_TIMEOUT}s)", ""
    except OSError as e:
        # errno and strerror, not the bare class name: "OSError" says nothing,
        # and "Argument list too long" says everything.
        why = " ".join(str(x) for x in (e.errno, e.strerror or e) if x)[:120]
        return None, f"untriaged (judge could not start: {why})", ""
    if proc.returncode:
        # A non-zero exit means the RUN failed, so whatever reached stdout is not
        # a verdict even when it parses — the rule the two branches below already
        # get from cli_failure_gist, applied before anything is parsed rather
        # than after, since valid-looking JSON printed on the way out would
        # otherwise be accepted as a real ruling and the failure never reported.
        return None, f"untriaged (judge failed: {cli_failure_gist(proc)})", ""
    m = re.search(r"\{.*\}", proc.stdout or "", re.S)
    if not m:
        return None, ("untriaged (no verdict: "
                      f"{cli_failure_gist(proc, 'no JSON in reply')})"), ""
    try:
        v = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None, ("untriaged (bad verdict: "
                      f"{cli_failure_gist(proc, 'malformed JSON')})"), ""
    return (bool(v.get("doable", False)), str(v.get("reason", ""))[:120],
            clamp_model(str(v.get("model", "")), model))


# --------------------------------------------------------------- per-issue pipeline

def claude(skill_cmd: str, cwd: str, perm_mode: str, model: str = "") -> None:
    # model = the tier the judge routed this sub-issue to; empty pins nothing
    # (the CLI's saved default applies, the pre-routing behaviour).
    subprocess.run(["claude", "-p", skill_cmd, "--permission-mode", perm_mode]
                   + (["--model", model] if model else []),
                   cwd=cwd, stdin=subprocess.DEVNULL, check=True)


def run_panel(repo_path: str, pr: int) -> None:
    # Pass the PATH, not the name: the resolver would otherwise look a bare name
    # up under ~/source, which silently picks the wrong repo when the directory
    # name and the GitHub name differ.
    # sys.executable, not `uv run`: there is no project to resolve from once this
    # ships to ~/.claude/loops, and panel.py is stdlib-only anyway.
    subprocess.run([sys.executable, str(PANEL), "--repo", repo_path,
                    "--pr", str(pr)], check=False)


def pr_green(gh_repo: str, pr: int) -> tuple[bool, str]:
    """Is the PR's CI green enough to stack? Returns (green, status). green = all
    checks passing (or none reported); red/pending/unknown → not green. Parses
    `gh pr checks` stdout regardless of exit code (it exits non-zero when red)."""
    try:
        proc = subprocess.run(["gh", "pr", "checks", str(pr), "--repo", gh_repo,
                               "--json", "bucket"],
                              capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"ci-error ({e.__class__.__name__})"
    raw = (proc.stdout or "").strip()
    if not raw:  # no JSON → usually "no checks reported"
        tail = (proc.stderr or "").strip().lower()
        return ("no checks" in tail), ("none" if "no checks" in tail else "unknown")
    try:
        buckets = [str(c.get("bucket", "")).lower() for c in json.loads(raw)]
    except json.JSONDecodeError:
        return False, "ci-unparseable"
    if not buckets:
        return True, "none"
    if "fail" in buckets:
        return False, "red"
    if "pending" in buckets:
        return False, "pending"
    return True, "green"


def worktree_has_new_commit(wt: str, base: str) -> bool:
    """True if the worktree's HEAD carries commits beyond the base it forked from."""
    r = git(wt, "rev-list", "--count", f"origin/{base}..HEAD")
    try:
        return int((r.stdout or "0").strip()) > 0
    except ValueError:
        return False


def worktree_dirty(wt: str) -> bool:
    return bool((git(wt, "status", "--porcelain").stdout or "").strip())


def auto_finish(wt: str, branch: str, num: int) -> bool:
    """Last-ditch: commit staged/unstaged work left behind by a /fix-issue that
    pushed nothing, and push the branch, so the issue yields a reviewable artifact
    instead of vanishing. Returns True if a commit was produced and pushed."""
    if not worktree_dirty(wt):
        return False
    git(wt, "add", "-A")
    msg = f"chore(#{num}): auto-finish salvaged work\n\nRefs #{num}"
    if not git_ok(wt, "commit", "-m", msg):
        return False
    git(wt, "push", "-u", "origin", branch)
    return True


def teardown_worktree(cfg: dict, branch: str, wt: str) -> None:
    """Tear down the issue's worktree AND its docker containers / isolated DB.
    Prefers `remove-worktree` (which stops containers + drops the DB copy); falls
    back to a bare `git worktree remove` if that tool isn't on PATH."""
    if shutil.which("remove-worktree"):
        rc = subprocess.run(["remove-worktree", "--keep-branch", branch],
                            cwd=cfg["path"], capture_output=True, text=True)
        if rc.returncode == 0:
            return
        print(f"    (remove-worktree failed: {(rc.stderr or '').strip()[:80]} — "
              f"falling back to git worktree remove)")
    subprocess.run(["git", "-C", cfg["path"], "worktree", "remove", "--force", wt],
                   check=False)


def fork_point_behind(path: str, branch: str, base: str) -> str:
    """'' if `branch` contains the tip of `base`; else a short description of the gap.

    Checks against origin/<base> as well as the local ref, so a local base that is
    itself behind origin is still caught rather than vouched for."""
    for ref in (f"origin/{base}", base):
        if not ref_exists(path, ref):
            continue
        if is_ancestor(path, ref, branch):
            return ""
        n = (git(path, "rev-list", "--count", f"{branch}..{ref}").stdout or "").strip()
        return f"missing {n or '?'} commit(s) from {ref}"
    return ""  # base unresolvable — not our call to block on


def sync_local_ref(path: str, branch: str) -> tuple[bool, str]:
    """Fast-forward the local `branch` to origin's without checking it out.

    `git fetch origin b:b` is refused (not silently forced) when the update would not
    be a fast-forward, or when b is currently checked out in some worktree — both are
    cases a human should look at rather than have overwritten."""
    r = git(path, "fetch", "origin", f"{branch}:{branch}")
    if r.returncode == 0:
        return True, ""
    err = (r.stderr or r.stdout or "").strip().splitlines()
    return False, (err[-1][:100] if err else "fetch failed")


def merge_sub_into_epic(cfg: dict, sub_branch: str,
                        epic_branch: str) -> tuple[bool, str]:
    """Fast-forward-only merge of a green sub-branch into the epic branch, pushed.
    Done in a throwaway detached worktree so the main checkout is never disturbed.
    ff-only is deliberate: because integration mode serialises issues and forks each
    from the current epic tip, the merge is always a fast-forward — and that keeps
    history (incl. alembic migration heads) linear. A non-ff result means the epic
    branch diverged (parallel work / a second migration head) and is surfaced for a
    manual relinearize rather than force-merged."""
    path = cfg["path"]
    mdir = f"{path}-epicmerge"
    git(path, "fetch", "--quiet", "origin", epic_branch, sub_branch)
    subprocess.run(["git", "-C", path, "worktree", "remove", "--force", mdir],
                   capture_output=True)
    add = git(path, "worktree", "add", "--detach", mdir, f"origin/{epic_branch}")
    if add.returncode != 0:
        return False, f"merge-worktree add failed: {(add.stderr or '').strip()[:80]}"
    try:
        ff = git(mdir, "merge", "--ff-only", f"origin/{sub_branch}")
        if ff.returncode != 0:
            return False, "non-ff — epic branch diverged; relinearize before merging"
        sha = (git(mdir, "rev-parse", "--short", "HEAD").stdout or "").strip()
        push = git(mdir, "push", "origin", f"HEAD:{epic_branch}")
        if push.returncode != 0:
            return False, f"ff ok ({sha}) but push failed: {(push.stderr or '').strip()[:80]}"
        # The merge happened on origin; the LOCAL epic ref is still where it was, and
        # `create-worktree --from <epic-branch>` forks the LOCAL branch. Leaving it
        # behind makes the next sub-issue fork without this merge — silently, since
        # both the merge and the fork succeed. That put #1518 of EPIC #1516 on a tree
        # with no package entity while the merge line above said otherwise.
        ok, why = sync_local_ref(path, epic_branch)
        if not ok:
            return True, (f"merged->{epic_branch} ({sha}) ⚠ LOCAL ref not synced ({why}) "
                          f"— run `git branch -f {epic_branch} origin/{epic_branch}` "
                          f"before the next issue or it will fork stale")
        return True, f"merged->{epic_branch} ({sha})"
    finally:
        subprocess.run(["git", "-C", path, "worktree", "remove", "--force", mdir],
                       capture_output=True)


def alembic_head_count(cfg: dict, epic_branch: str) -> int | None:
    """Best-effort count of alembic heads on the epic branch tip — >1 means the
    migration history forked (parallel branches each added a head) and needs a
    relinearize. Returns None when it can't tell (no alembic, tool missing)."""
    path = cfg["path"]
    mig = cfg.get("epic", {}).get("migrations_dir", "migrations/versions")
    if not (Path(path) / mig).exists():
        return None
    r = git(path, "grep", "-h", "-E", r"^down_revision", f"origin/{epic_branch}",
            "--", f"{mig}/*.py")
    if r.returncode != 0:
        return None
    # Revision ids are arbitrary strings, NOT necessarily alembic's default 12-char
    # hex: this used to match [0-9a-f]+ only, so a repo using readable ids
    # ('m1517a', 'm1564a') had every such line silently skipped and the count taken
    # over whatever hex-named leftovers remained — a disconnected subgraph, which of
    # course looked forked. It reported '4 alembic heads — relinearize before opening
    # the epic PR' on EPIC #1516 while the repo's own tool said 1. False alarms in
    # this direction are worse than none: they cost a relinearize that isn't needed,
    # or they train you to wave the warning through on the run where it's real.
    ident = r"['\"]([^'\"]+)['\"]"
    revs, downs = set(), set()
    for line in (r.stdout or "").splitlines():
        # A merge revision's down_revision is a TUPLE — take every id on the line.
        downs.update(re.findall(ident, line))
    rr = git(path, "grep", "-h", "-E", r"^revision", f"origin/{epic_branch}",
             "--", f"{mig}/*.py")
    for line in (rr.stdout or "").splitlines():
        m = re.search(rf"=\s*{ident}", line)
        if m:
            revs.add(m.group(1))
    heads = revs - downs
    return len(heads) if revs else None


@dataclass
class WorkResult:
    num: int
    outcome: str            # done | reviewed | failed | blocked
    pr: int | None = None
    detail: str = ""


# ----------------------------------------------------------------- preflight

def workspace_trusted(path: str) -> bool:
    """Whether `path` (or an ancestor) has hasTrustDialogAccepted in ~/.claude.json.
    An untrusted workspace makes `claude` silently drop permissions.allow entries
    ('Ignoring N permissions.allow entries'), which can stall /fix-issue's git+gh.
    Ancestor trust counts (Claude Code trusts a folder's subtree). Unknown → True
    (don't block on an unreadable config)."""
    try:
        conf = json.loads((Path.home() / ".claude.json").read_text())
    except (OSError, json.JSONDecodeError):
        return True
    projects = conf.get("projects", {})
    p = Path(path)
    for anc in [p, *p.parents]:
        entry = projects.get(str(anc))
        if entry and entry.get("hasTrustDialogAccepted"):
            return True
    return False


def available_mem_mb() -> int | None:
    """MemAvailable in MiB from /proc/meminfo, or None if unreadable (non-Linux)."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def orphan_container_count() -> int | None:
    """Count running containers whose name carries a worktree slug (feat-issue-*).
    A rising count across runs means teardown is leaking — worth flagging."""
    try:
        r = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return sum(1 for n in (r.stdout or "").splitlines() if "feat-issue-" in n)


def preflight(cfg: dict, execute: bool) -> tuple[list[str], list[str]]:
    """Environment + resource checks before an --execute run. Returns
    (blockers, warnings). Blockers abort the run; warnings are printed and
    proceed. No-op for dry runs."""
    blockers, warnings = [], []
    if not execute:
        return blockers, warnings

    # P5: permission mode must actually let /fix-issue run git+gh.
    perm = cfg.get("headless_permission_mode", "acceptEdits")
    if perm != "bypassPermissions":
        # Non-bypass modes rely on permissions.allow for git/gh — which an untrusted
        # workspace drops. bypassPermissions allows everything regardless, so trust
        # is moot there.
        if not workspace_trusted(cfg["path"]):
            blockers.append(
                f"workspace {cfg['path']} is not trusted (hasTrustDialogAccepted) and "
                f"headless_permission_mode='{perm}' relies on permissions.allow — claude "
                f"would silently drop it and /fix-issue's git/gh would stall. Trust the "
                f"folder or set headless_permission_mode='bypassPermissions'.")
        else:
            warnings.append(
                f"headless_permission_mode='{perm}' relies on permissions.allow for git/gh; "
                f"if those aren't allow-listed, /fix-issue will stall (bypassPermissions "
                f"avoids the dependency).")

    # P4: resource discipline (advisory).
    floor = cfg.get("epic", {}).get("min_free_mb", 2048)
    mem = available_mem_mb()
    if mem is not None and mem < floor:
        warnings.append(f"low memory: {mem}MiB available < {floor}MiB floor — the app/DB "
                        f"stack per worktree may OOM; run fewer issues or a lighter worktree.")
    orphans = orphan_container_count()
    if orphans is not None and orphans:
        warnings.append(f"{orphans} feat-issue-* container(s) already running — possible "
                        f"leak from a prior run; `docker ps` and clean up if stale.")
    return blockers, warnings


def work_issue(cfg: dict, w: IssueWork, execute: bool, base: str | None = None,
               state: dict | None = None) -> WorkResult:
    repo, gh_repo = cfg_repo_name(cfg), cfg["github"]
    # `base` is the branch each sub-issue worktree/ PR targets: executor_pr_base in
    # multi mode, the epic branch in integration mode (set by run()).
    base = base or cfg.get("executor_pr_base", cfg.get("default_branch", "main"))
    perm = cfg.get("headless_permission_mode", "acceptEdits")
    branch = f"feat/issue-{w.num}"
    # create-worktree names the dir <repo>-<branch with / -> ->; matching it here is
    # essential — otherwise the finally cleanup removes the wrong (non-existent) path
    # and the real worktree is orphaned (see issues/open epic-driver notes).
    wt = worktree_path(cfg, branch)

    if w.stage == "done":
        print(f"  #{w.num}: done — skip")
        return WorkResult(w.num, "done", w.pr_number)
    if w.stage == "blocked":
        # "NOT agent-doable" is a RULING, and an untriaged issue has none — the
        # judge never answered. Both are skipped; only one of them was judged.
        verdict = "NOT agent-doable" if w.doable is False else "not confirmed doable"
        print(f"  #{w.num}: {verdict} ({w.reason}) — skipping (needs a human)")
        return WorkResult(w.num, "blocked", detail=w.reason)

    if not execute:
        mdl = w.model or "default"
        if w.stage == "implement":
            print(f"  #{w.num}: WOULD implement — create-worktree --from {base} {branch}; "
                  f"claude -p '/fix-issue {w.num} --base {base}' ({perm}, model={mdl}); "
                  f"assert artifact; panel; /review-pr until green.")
        else:  # review
            print(f"  #{w.num}: WOULD review PR #{w.pr_number} — panel; "
                  f"/review-pr until green (model={mdl}).")
        return WorkResult(w.num, "reviewed" if w.stage == "review" else "implement")

    wt_args = cfg.get("epic", {}).get("executor_worktree_args", [])
    auto_fin = cfg.get("epic", {}).get("auto_finish", False)
    try:
        if w.stage == "implement":
            print(f"  #{w.num}: implementing via /fix-issue (worktree {wt}, "
                  f"model={w.model or 'default'}, base={base})")
            subprocess.run(["create-worktree", "--from", base, *wt_args, branch],
                           cwd=cfg["path"], check=True)

            # P1 — fail loud on a stale fork point. create-worktree resolves `--from`
            # against the LOCAL ref, so anything that leaves it behind origin (a merge
            # pushed by a previous run, a hand-edited branch) silently produces a
            # worktree missing its predecessors' work. Both the merge and the fork
            # report success, so nothing else in this pipeline would notice: the issue
            # is implemented against a tree that lacks what it was meant to build on,
            # and the wrongness only surfaces as conflicts or nonsense at review.
            stale = fork_point_behind(cfg["path"], branch, base)
            if stale:
                detail = (f"fork point is stale — {branch} does not contain {base} "
                          f"({stale}). Sync with `git branch -f {base} origin/{base}`, "
                          f"drop this worktree, and re-run.")
                print(f"  #{w.num}: FAILED — {detail}")
                if state is not None:
                    record(state, w.num, stage="failed", branch=branch,
                           merged=False, lastAction=detail)
                return WorkResult(w.num, "failed", detail=detail)
            # /fix-issue plans, implements, tests, pushes, opens a PR targeting --base
            # (the epic branch in integration mode). Needs git/gh → bypassPermissions.
            claude(f"/fix-issue {w.num} --base {base}", wt, perm, w.model)

            # P2 — fail loud: /fix-issue must leave a PR to review and stack. If it
            # didn't, this issue FAILS (we never pretend it reached the merge gate) —
            # but we first preserve whatever work exists so it isn't stranded in the
            # worktree we're about to tear down.
            pr = _discover_pr(gh_repo, w.num)
            if not pr:
                salvaged = worktree_has_new_commit(wt, base)
                if salvaged:
                    git(wt, "push", "-u", "origin", branch)  # ensure the commit is pushed
                elif auto_fin:
                    salvaged = auto_finish(wt, branch, w.num)  # commit+push staged work
                detail = (f"committed but /fix-issue opened no PR (pushed {branch}) — "
                          f"open its PR and re-run" if salvaged
                          else "/fix-issue produced no commit and no PR")
                print(f"  #{w.num}: FAILED — {detail}")
                if state is not None:
                    record(state, w.num, stage="failed", branch=branch,
                           merged=False, lastAction=detail)
                return WorkResult(w.num, "failed", detail=detail)
            if state is not None:
                record(state, w.num, stage="implemented", branch=branch, pr=pr,
                       lastAction="pushed")
        else:
            pr = w.pr_number or _discover_pr(gh_repo, w.num)
            if not pr:
                detail = "review-stage issue has no open PR to review"
                print(f"  #{w.num}: FAILED — {detail}")
                if state is not None:
                    record(state, w.num, stage="failed", branch=branch, lastAction=detail)
                return WorkResult(w.num, "failed", detail=detail)

        print(f"  #{w.num}: reviewing PR #{pr}")
        run_panel(cfg["path"], pr)
        # /review-pr addresses findings + pushes; merge withheld (human/epic gate).
        claude(f"/review-pr {pr}", cfg["path"], perm, w.model)
        if state is not None:
            record(state, w.num, stage="reviewed", branch=branch, pr=pr, lastAction="reviewed")
        return WorkResult(w.num, "reviewed", pr=pr)
    finally:
        # P4 — tear down the worktree AND its containers / isolated DB, not just the dir.
        teardown_worktree(cfg, branch, wt)


def _discover_pr(gh_repo: str, num: int) -> int | None:
    prs = gh_json(["pr", "list", "--state", "open", "--limit", "100",
                   "--json", "number,headRefName,body"], gh_repo)
    pr = find_pr_for_issue(prs, num)
    return pr["number"] if pr else None


def ensure_epic_branch(cfg: dict, branch: str, base: str) -> None:
    """Create the durable epic integration branch off `base` and push it, if absent.
    Sub-issue worktrees branch off this; the master merges green sub-PRs into it."""
    path = cfg["path"]
    have = subprocess.run(["git", "-C", path, "rev-parse", "--verify", "--quiet", branch],
                          capture_output=True).returncode == 0
    remote = subprocess.run(
        ["git", "-C", path, "ls-remote", "--exit-code", "--heads", "origin", branch],
        capture_output=True).returncode == 0
    if have or remote:
        print(f"  epic branch '{branch}' already exists — reusing")
        # Reusing is the resume path, so the local ref is exactly the one likely to be
        # behind origin (a previous run merged sub-PRs by pushing). Sub-worktrees fork
        # the LOCAL ref, so sync before anything branches off it.
        if remote:
            ok, why = sync_local_ref(path, branch)
            if not ok and have:
                print(f"  ⚠ local '{branch}' is NOT in step with origin ({why}) — "
                      f"sub-issues would fork stale. Reconcile before executing.")
        return
    print(f"  creating epic branch '{branch}' off {base}")
    subprocess.run(["git", "-C", path, "fetch", "origin", base], check=True)
    subprocess.run(["git", "-C", path, "branch", branch, f"origin/{base}"], check=True)
    subprocess.run(["git", "-C", path, "push", "-u", "origin", branch], check=True)


def cfg_repo_name(cfg: dict) -> str:
    return cfg["github"].split("/")[-1]


def branch_slug(branch: str) -> str:
    """The dir/container suffix create-worktree derives from a branch (/ -> -)."""
    return branch.replace("/", "-")


def worktree_path(cfg: dict, branch: str) -> str:
    """Path create-worktree builds: <sibling-of-repo>/<repo>-<branch-slug>."""
    return f"{cfg['path']}-{branch_slug(branch)}"


def plan_entry(cfg: dict, w: IssueWork, phase_index: int, base: str | None = None) -> dict:
    """The structured plan for one issue — what --json emits and the L1 skill
    consumes for orchestration, reconciliation and cleanup. `phase`/`phase_index`
    are the fan-out layer: issues sharing a phase are candidates to run in
    parallel; later phases are dependency layers (see the /epic skill). `base` is
    the branch this sub-issue targets (executor_pr_base, or the epic branch in
    integration mode)."""
    base = base or cfg.get("executor_pr_base", cfg.get("default_branch", "main"))
    branch = f"feat/issue-{w.num}"
    action = {"done": "skip", "blocked": "skip-blocked",
              "review": "review", "implement": "implement"}[w.stage]
    return {
        "num": w.num, "title": w.title, "stage": w.stage, "action": action,
        "checked": w.checked, "issue_state": w.issue_state,
        "pr_number": w.pr_number, "pr_state": w.pr_state,
        "doable": w.doable, "reason": w.reason, "model": w.model,
        "phase": w.phase, "phase_index": phase_index,
        "base": base, "branch": branch,
        "worktree": worktree_path(cfg, branch),
        # docker containers create-worktree spins up share this slug as a suffix
        # (e.g. <repo>-<slug>, <svc>-<slug>) — used to find orphans on reconcile.
        "container_slug": branch_slug(branch),
    }


# --------------------------------------------------------------- run

def resolve_landing(cfg: dict, work: list[IssueWork], epic: int,
                    requested: str | None, integration_branch: str | None,
                    sub_pr_merge: str | None) -> dict:
    """Fold the landing request (flag > repo config > 'auto') against the coupling
    signals into a concrete plan: resolved strategy, epic branch, sub-PR merge mode.
    'auto' resolves to the suggestion; the /epic master may still override after its
    own judgment by re-invoking with an explicit --landing."""
    epic_cfg = cfg.get("epic", {})
    signals = suggest_landing(work)
    requested = requested or epic_cfg.get("landing", "auto")
    resolved = signals["suggested"] if requested == "auto" else requested
    merge_mode = sub_pr_merge or epic_cfg.get("sub_pr_merge", "auto")
    title = gh_json(["issue", "view", str(epic), "--json", "title"],
                    cfg["github"]).get("title", "")
    return {
        "requested": requested, "resolved": resolved,
        "suggested": signals["suggested"], "reasons": signals["reasons"],
        "edges": signals["edges"], "phases": signals["phases"],
        "sub_pr_merge": merge_mode,
        "epic_branch": (epic_branch_name(epic, title, integration_branch)
                        if resolved == "integration" else None),
    }


def run(repo_name: str, epic: int, execute: bool, max_issues: int | None,
        json_out: bool = False, landing: str | None = None,
        integration_branch: str | None = None, sub_pr_merge: str | None = None,
        base: str | None = None, model: str | None = None,
        keep_going: bool = False) -> int:
    cfg = load_repo_cfg(repo_name)
    print(describe(cfg))
    repo_name = cfg["name"]
    if execute and not cfg.get("loops", {}).get("issue_executor"):
        print(f"[{repo_name}] issue_executor not enabled in .harness-rules — refusing "
              f"to --execute. (Dry-run planning is allowed; set loops.issue_executor "
              f"to true to run.)")
        return 0

    work = build_worklist(cfg["github"], epic)
    land = resolve_landing(cfg, work, epic, landing, integration_branch, sub_pr_merge)
    # In integration mode every sub-issue worktree branches off the epic branch, not
    # base; epic.py merges each green sub-PR into it (--sub-pr-merge auto) and leaves
    # one epic->base PR for the human. --base overrides executor_pr_base for THIS run
    # only (e.g. land an epic's work into a feature branch like 'omnibus' without
    # editing config). Falls back to config, then default_branch.
    base_branch = base or cfg.get("executor_pr_base", cfg.get("default_branch", "main"))
    sub_base = land["epic_branch"] if land["resolved"] == "integration" else base_branch

    # P3 — idempotent resume: mark issues whose sub-branch is already an ancestor of
    # the epic branch as done (git ancestry, independent of PR/issue state). Runs in
    # both dry-run and execute so the plan reflects a resumed run.
    if land["resolved"] == "integration" and land["epic_branch"]:
        git(cfg["path"], "fetch", "--quiet", "origin")
        for w in work:
            if w.stage == "done":
                continue
            # A PR is the evidence that work ever existed. Ancestry alone cannot
            # distinguish a merged branch from an empty one (see sub_branch_merged),
            # so without a PR we refuse to call it done and re-implement instead —
            # the safe direction: redoing work costs time, skipping it corrupts the
            # stack every later issue is built on.
            if not w.pr_number:
                continue
            if sub_branch_merged(cfg, f"feat/issue-{w.num}", land["epic_branch"]):
                w.stage = "done"

    # Master triages not-yet-started issues for doability (no agent-ready label —
    # the master reads each issue and decides). Non-doable → 'blocked'. The judge
    # runs at the tier the epic was initiated with (--model, from the /epic master;
    # falls back to review_panel.judge_model) and routes each sub-issue to an
    # equal-or-lesser tier for implementation.
    ceiling = model or cfg.get("review_panel", {}).get("judge_model", "")
    impl = [w for w in work if w.stage == "implement"]
    if impl:
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(triage, w, ceiling): w for w in impl}
            for fut in futs:
                w = futs[fut]
                w.doable, w.reason, w.model = fut.result()
                # `is not True`, not `is False`: an untriaged sub-issue (doable
                # is None — the judge timed out, crashed, or printed nothing) has
                # no doability ruling at all, and handing it to the autonomous
                # executor is precisely what the reason line it prints says did
                # not happen. Not confirmed doable means a human looks at it.
                if w.doable is not True:
                    w.stage = "blocked"
    # review-stage issues skip triage; their /review-pr fix-up runs at the ceiling
    # (only when routing is on — an unrecognised ceiling pins nothing).
    for w in work:
        if not w.model and w.stage == "review":
            w.model = ceiling if ceiling in MODEL_TIERS else ""

    pending = [w for w in work if w.stage in ("implement", "review")]
    blocked = [w for w in work if w.stage == "blocked"]

    if json_out:
        # Structured plan consumed by the /epic L1 skill. Always plan-only (the
        # skill drives execution itself); never executes regardless of --execute.
        # phase_index = order of first appearance of each phase header → the
        # dependency layers the orchestrator fans out within / across.
        phase_order: dict[str, int] = {}
        for w in work:
            if w.phase not in phase_order:
                phase_order[w.phase] = len(phase_order)
        print(json.dumps({
            "repo": repo_name,
            "github": cfg["github"],
            "epic": epic,
            "path": cfg["path"],
            "executor_pr_base": base_branch,
            "headless_permission_mode": cfg.get("headless_permission_mode",
                                                "acceptEdits"),
            "auto_merge": cfg.get("auto_merge"),
            "issue_executor_enabled": bool(cfg.get("loops", {}).get("issue_executor")),
            "max_issues": max_issues,
            # The tier the run was initiated with — the judge's own tier, and the
            # ceiling for per-issue `model` routing ('' = routing off, nothing pinned).
            "model_ceiling": ceiling if ceiling in MODEL_TIERS else "",
            # The landing plan. resolved=integration => the master creates `epic_branch`
            # off executor_pr_base, points each sub-issue's base at it, merges green
            # sub-PRs in (per sub_pr_merge), and opens ONE epic_branch->base PR at the end.
            # 'auto' resolved from `suggested`/`reasons`; the master may override after
            # its own judgment (interview the user only when borderline).
            "landing": land,
            "counts": {"total": len(work), "workable": len(pending),
                       "blocked": len(blocked), "phases": len(phase_order)},
            "issues": [plan_entry(cfg, w, phase_order[w.phase], sub_base) for w in work],
        }, indent=2))
        return 0

    print(f"\n[{repo_name}] EPIC #{epic} — {len(work)} sub-issues, {len(pending)} workable"
          f"{f', {len(blocked)} blocked' if blocked else ''} — "
          f"{'EXECUTE' if execute else 'DRY RUN'}\n")
    src = "pinned" if land["requested"] != "auto" else "auto"
    if land["resolved"] == "integration":
        print(f"  landing: INTEGRATION ({src}) → one PR via epic branch "
              f"'{land['epic_branch']}' (sub-PRs: {land['sub_pr_merge']}-merge into it)")
    else:
        print(f"  landing: MULTI-PR ({src}) → a PR per sub-issue into {base_branch}")
    print(f"  signals: suggested={land['suggested']}; {'; '.join(land['reasons'])}")
    if land["edges"]:
        print(f"  deps:    {', '.join(f'#{a}->#{b}' for a, b in land['edges'])}")
    print()
    for w in work:
        tag = f"PR#{w.pr_number}({w.pr_state})" if w.pr_number else "no-PR"
        extra = f"  ⚠ {w.reason}" if w.stage == "blocked" else ""
        print(f"  #{w.num:<5} [{w.stage:<9}] {tag:<14} {(w.model or '-'):<7} "
              f"{w.title[:48]}{extra}")
    print()

    # P1 — serialise by dependency order so each issue forks from a branch that
    # already contains what it builds on (the stacking precondition).
    integration = land["resolved"] == "integration"
    pending = toposort(pending, land["edges"]) if integration else pending

    if execute:
        blockers, warnings = preflight(cfg, execute)
        for wmsg in warnings:
            print(f"  ⚠ preflight: {wmsg}")
        if blockers:
            for bmsg in blockers:
                print(f"  ✗ preflight: {bmsg}")
            print("\n(aborting — fix the blocker(s) above and re-run.)")
            return 1

    if execute and integration:
        ensure_epic_branch(cfg, land["epic_branch"], base_branch)

    state = load_state(repo_name, epic)
    state["integration_branch"] = land["epic_branch"]
    todo = pending[:max_issues] if max_issues else pending
    if max_issues and len(pending) > max_issues:
        print(f"(budget: working {max_issues} of {len(pending)} pending this run)\n")

    auto_merge_subs = integration and land["sub_pr_merge"] == "auto"
    failures = []
    for w in todo:
        res = work_issue(cfg, w, execute, sub_base, state if execute else None)
        if not execute or res.outcome in ("done", "blocked"):
            continue
        if res.outcome == "failed":
            # P2 — never continue past a silent no-artifact issue. Surface and stop,
            # unless --keep-going was asked for (then collect and press on).
            failures.append(res)
            if keep_going:
                print(f"  #{w.num}: FAILED ({res.detail}) — --keep-going, moving on")
                continue
            print(f"\n✗ STOP: #{w.num} failed — {res.detail}. Nothing merged for it. "
                  f"Fix it (or re-run to resume) — state in "
                  f"{STATE_DIR}/epic-{repo_name}-{epic}.json.")
            return 1
        # res.outcome == "reviewed": it has a PR. In integration/auto, merge it into
        # the epic branch when CI is green so the next issue stacks on top.
        if auto_merge_subs and res.pr:
            green, status = pr_green(cfg["github"], res.pr)
            if not green:
                print(f"  #{w.num}: PR #{res.pr} not green ({status}) — NOT merging into "
                      f"'{land['epic_branch']}'; left for a human. Stacking may stall.")
                record(state, w.num, stage="reviewed-not-green", pr=res.pr,
                       lastAction=f"ci {status}")
                if not keep_going:
                    print(f"\n✗ STOP: #{w.num}'s PR isn't green — resolve CI, then re-run "
                          f"to resume the stack.")
                    return 1
                continue
            ok, detail = merge_sub_into_epic(cfg, f"feat/issue-{w.num}",
                                             land["epic_branch"])
            print(f"  #{w.num}: {detail}")
            record(state, w.num, stage="done" if ok else "reviewed",
                   pr=res.pr, merged=ok, lastAction=detail)
            if not ok and not keep_going:
                print(f"\n✗ STOP: could not stack #{w.num} — {detail}. Relinearize the "
                      f"epic branch and re-run.")
                return 1
        elif integration:
            print(f"  #{w.num}: PR #{res.pr} left at sub-PR gate "
                  f"(--sub-pr-merge=gate) — human merges into '{land['epic_branch']}'.")

    if execute and integration:
        heads = alembic_head_count(cfg, land["epic_branch"])
        if heads and heads > 1:
            print(f"\n  ⚠ migration linearity: {heads} alembic heads on "
                  f"'{land['epic_branch']}' — relinearize before opening the epic PR "
                  f"(`alembic heads` / merge-revision).")

    if integration:
        merged = sum(1 for i in state.get("issues", {}).values() if i.get("merged"))
        print(f"\n(integration: {merged} sub-PR(s) merged into '{land['epic_branch']}'. "
              f"When all are in, open ONE '{land['epic_branch']}'→{base_branch} PR — the "
              f"single human merge gate. Merge to {base_branch} is never automatic.)")
    elif execute:
        print(f"\n(multi-PR: each sub-PR targets {base_branch}, reviewed and left for a "
              f"human merge. Parallel branches may each add an alembic head — relinearize "
              f"on merge if so.)")
    if failures:
        print(f"\n⚠ {len(failures)} issue(s) failed: "
              f"{', '.join(f'#{f.num}' for f in failures)}")
    if not execute:
        print("\n(dry run — nothing created/merged. --execute to run the pipeline; "
              "merge to the base branch always stays a human step.)")
    return 1 if failures else 0


def main() -> int:
    # An --execute run lasts hours and is almost always redirected to a file or a
    # pipe, where Python block-buffers stdout: progress appears only when a 8KB block
    # fills or the process exits. That makes a live run indistinguishable from a hung
    # one, and the natural response — kill it — is what strands a half-created
    # worktree. Line buffering costs nothing here; the output is a few hundred lines.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):  # not a real TextIO (captured/replaced)
            pass
    ap = argparse.ArgumentParser(description="Epic driver — work an epic's issues")
    ap.add_argument("--repo", help="repo path, or a name under ~/source (default: cwd)")
    ap.add_argument("--epic", required=True, type=int)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--max-issues", type=int, help="cap issues worked this run (budget)")
    ap.add_argument("--landing", choices=["auto", "integration", "multi"], default=None,
                    help="landing model: 'integration' = one epic branch -> one PR; "
                         "'multi' = a PR per sub-issue; 'auto' (default) = suggest from "
                         "coupling signals. Overrides repo config.")
    ap.add_argument("--integration-branch", default=None,
                    help="epic branch name for integration mode (default epic/<n>-<slug>)")
    ap.add_argument("--sub-pr-merge", choices=["auto", "gate"], default=None,
                    help="integration mode: 'auto' (default) merges each green sub-PR into "
                         "the epic branch; 'gate' holds each at a human merge")
    ap.add_argument("--base", default=None,
                    help="base branch sub-issues target THIS run (overrides config's "
                         "executor_pr_base without editing it) — e.g. --base omnibus to "
                         "land an epic's PRs into a feature branch. In integration mode the "
                         "epic branch is cut off this base.")
    ap.add_argument("--model", choices=MODEL_TIERS, default=None,
                    help="tier the epic run is initiated with (the /epic master passes its "
                         "own tier). The triage judge runs at this tier and routes each "
                         "sub-issue to an equal-or-lesser tier for /fix-issue + /review-pr. "
                         "Omitted: judge falls back to review_panel.judge_model; nothing "
                         "is pinned for implementation unless that is a recognised tier.")
    ap.add_argument("--keep-going", action="store_true",
                    help="don't stop the run on a failed/ non-green issue — collect the "
                         "failure and continue (default: stop and surface, so the stack "
                         "never advances past a broken rung)")
    ap.add_argument("--json", action="store_true", dest="json_out",
                    help="emit the worklist plan as JSON (plan-only; for the /epic skill)")
    args = ap.parse_args()
    return run(args.repo, args.epic, args.execute, args.max_issues, args.json_out,
               landing=args.landing, integration_branch=args.integration_branch,
               sub_pr_merge=args.sub_pr_merge, base=args.base, model=args.model,
               keep_going=args.keep_going)


if __name__ == "__main__":
    raise SystemExit(main())
