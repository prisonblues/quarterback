#!/usr/bin/env python3
"""Loop A — dependabot CI-green auto-lander.

Reads the repo's `.harness-rules`, finds its open dependabot PRs, classifies each
(security / patch-minor / major / unknown), reads CI status, and decides an
action per the repo's auto_merge policy.

Default is REPORT-ONLY (dry run): nothing is merged or pushed. Pass --execute to
actually act (only patch/minor auto-merge is wired; red-CI fix is still a stub).

Usage:
    python3 ~/.claude/loops/lander.py                         # dry run in the cwd's repo
    python3 ~/.claude/loops/lander.py --execute               # act on decisions
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness_rules import RepoNotFound, describe, resolve_repo  # noqa: E402

SECURITY_RE = re.compile(r"security|cve|rce|xss|vuln|advisory", re.I)
VERSION_RE = re.compile(r"\bfrom\s+v?(\d+)\.(\d+)\.(\d+)\b.*?\bto\s+v?(\d+)\.(\d+)\.(\d+)\b", re.I)
GROUP_RE = re.compile(r"\bgroup\b", re.I)


DEPENDABOT_LOGIN = "dependabot[bot]"
FIX_PROMPT = (
    "This branch is a Dependabot dependency bump and its CI is failing. "
    "Investigate the failing checks for PR #{n} in {repo}, then fix ONLY the breakage "
    "caused by the dependency update (lockfile, imports, renamed/removed APIs, "
    "type/signature changes). Keep the change minimal; do not touch unrelated code. "
    "Make the source edits ONLY — do NOT run shell commands, and do NOT commit or "
    "push. CI re-runs to validate; the loop handles commit and push."
)


@dataclass
class Decision:
    number: int
    title: str
    base: str
    head: str        # the PR's head branch (dependabot/...)
    klass: str       # security | patch_minor | major | unknown
    checks: str      # green | red | pending | none
    action: str      # would-merge | escalate | would-fix-red | leave


def gh(args: list[str], repo: str) -> dict | list:
    """Run a gh command scoped to repo, return parsed JSON."""
    out = subprocess.run(
        ["gh", *args, "--repo", repo],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out) if out.strip() else []


def classify(title: str, labels: list[str]) -> str:
    """Map a dependabot PR to a risk class."""
    label_text = " ".join(labels)
    if SECURITY_RE.search(title) or SECURITY_RE.search(label_text):
        return "security"
    m = VERSION_RE.search(title)
    if m:
        fr = tuple(int(x) for x in m.groups()[:3])
        to = tuple(int(x) for x in m.groups()[3:])
        if to[0] != fr[0]:
            return "major"
        return "patch_minor"
    if GROUP_RE.search(title):
        # grouped bump — trust the group name if it scopes to minors/patches
        if re.search(r"minor|patch", title, re.I) and not re.search(r"major", title, re.I):
            return "patch_minor"
        return "unknown"
    return "unknown"


def check_status(pr: dict) -> str:
    """Aggregate statusCheckRollup into green/red/pending/none."""
    rollup = pr.get("statusCheckRollup") or []
    if not rollup:
        return "none"
    states = set()
    for c in rollup:
        # checks use 'conclusion'+'status'; statuses use 'state'
        s = c.get("conclusion") or c.get("state") or c.get("status") or ""
        states.add(s.upper())
    if states & {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}:
        return "red"
    if states & {"PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", ""}:
        return "pending"
    if states <= {"SUCCESS", "NEUTRAL", "SKIPPED", "COMPLETED"}:
        return "green"
    return "pending"


def decide(klass: str, checks: str, policy: str) -> str:
    if klass == "security":
        return "escalate"
    if klass in ("major", "unknown"):
        return "leave"
    # patch_minor
    if policy not in ("dependabot_patch_minor", "all_green"):
        return "leave"
    if checks == "green":
        return "would-merge"
    if checks == "red":
        return "would-fix-red"
    return "leave"  # pending → wait for next tick


def head_commit_author(gh_repo: str, branch: str) -> str:
    """Login of the author of a branch's tip commit (read-only)."""
    try:
        data = gh(["api", f"repos/{gh_repo}/commits/{branch}",
                   "-q", ".author.login"], gh_repo)
        return data if isinstance(data, str) else ""
    except subprocess.CalledProcessError:
        return ""


def fix_red(d: Decision, gh_repo: str, repo_path: str, execute: bool) -> None:
    """Open a plain worktree on the dependabot branch, let an agent fix the
    breakage, and push back to the same branch so the PR re-runs CI.

    Idempotent: if the branch tip is no longer a Dependabot commit, we have
    already attempted a fix — leave it for a human rather than loop forever.
    """
    last_author = head_commit_author(gh_repo, d.head)
    if last_author and last_author != DEPENDABOT_LOGIN:
        print(f"  #{d.number}: tip authored by '{last_author}' (not Dependabot) "
              f"— fix already attempted; leaving for human.")
        return

    wt_dir = f"{repo_path}-dependabot-{d.number}"
    prompt = FIX_PROMPT.format(n=d.number, repo=gh_repo)

    if not execute:
        print(f"  #{d.number}: WOULD fix red CI — "
              f"git fetch origin {d.head}; worktree at {wt_dir}; "
              f"`claude -p` to fix; push HEAD:{d.head}; remove worktree.")
        return

    if Path(wt_dir).exists():
        print(f"  #{d.number}: worktree {wt_dir} already exists — skipping (in progress).")
        return

    try:
        subprocess.run(["git", "-C", repo_path, "fetch", "origin", d.head],
                       check=True)
        subprocess.run(["git", "-C", repo_path, "worktree", "add", wt_dir,
                        f"origin/{d.head}"], check=True)
        # Headless agent EDITS ONLY (acceptEdits) — no shell/git access needed.
        # The loop owns commit+push, so the agent never gets bash in an unattended
        # loop. CI is the validation gate.
        subprocess.run(["claude", "-p", prompt, "--permission-mode", "acceptEdits"],
                       cwd=wt_dir, stdin=subprocess.DEVNULL, check=True)
        # Commit+push only if the agent actually changed files.
        subprocess.run(["git", "-C", wt_dir, "add", "-A"], check=True)
        has_changes = subprocess.run(
            ["git", "-C", wt_dir, "diff", "--cached", "--quiet"]).returncode != 0
        if has_changes:
            print(f"  #{d.number}: committing + pushing fix to {d.head}")
            subprocess.run([
                "git", "-C", wt_dir, "commit", "-m",
                "fix(deps): resolve CI breakage from dependency bump\n\n"
                "Automated via loops/lander.py red-CI fix path."], check=True)
            subprocess.run(["git", "-C", wt_dir, "push", "origin",
                            f"HEAD:{d.head}"], check=True)
        else:
            print(f"  #{d.number}: agent made no edits — nothing to push.")
    finally:
        # Mandatory cleanup — never leave worktrees behind (see #117 safety rails).
        subprocess.run(["git", "-C", repo_path, "worktree", "remove", "--force",
                        wt_dir], check=False)


def run(repo_name: str, execute: bool) -> int:
    try:
        cfg = resolve_repo(repo_name)
    except RepoNotFound as e:
        sys.exit(str(e))
    print(describe(cfg))
    repo_name = cfg["name"]
    if not cfg.get("enabled"):
        print(f"[{repo_name}] disabled in .harness-rules — skipping")
        return 0
    if not cfg.get("loops", {}).get("dependabot_lander"):
        print(f"[{repo_name}] dependabot_lander loop disabled — skipping")
        return 0

    gh_repo = cfg["github"]
    policy = cfg.get("auto_merge", "none")
    author = cfg.get("dependabot_author", "app/dependabot")

    prs = gh(
        ["pr", "list", "--author", author, "--state", "open", "--limit", "50",
         "--json", "number,title,baseRefName,headRefName,labels,statusCheckRollup"],
        gh_repo,
    )

    print(f"\n[{repo_name}] {gh_repo} — policy={policy} — "
          f"{'EXECUTE' if execute else 'DRY RUN'} — {len(prs)} dependabot PR(s)\n")
    if not prs:
        return 0

    decisions: list[Decision] = []
    for pr in prs:
        labels = [l["name"] for l in pr.get("labels", [])]
        klass = classify(pr["title"], labels)
        checks = check_status(pr)
        action = decide(klass, checks, policy)
        decisions.append(Decision(
            pr["number"], pr["title"], pr["baseRefName"], pr["headRefName"],
            klass, checks, action))

    width = max(len(d.title) for d in decisions)
    for d in decisions:
        print(f"  #{d.number:<5} {d.title[:width]:<{width}}  "
              f"base={d.base:<6} {d.klass:<12} ci={d.checks:<8} -> {d.action}")

    print()
    for d in decisions:
        if d.action == "would-merge":
            if execute:
                print(f"  merging #{d.number} ...")
                subprocess.run(
                    ["gh", "pr", "merge", str(d.number), "--squash",
                     "--delete-branch", "--repo", gh_repo], check=True)
            else:
                print(f"  #{d.number}: WOULD merge (squash + delete branch).")
        elif d.action == "would-fix-red":
            fix_red(d, gh_repo, cfg["path"], execute)

    if not execute:
        print("\n(dry run — nothing merged or pushed. Re-run with --execute to act.)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Loop A — dependabot CI-green auto-lander")
    ap.add_argument("--repo", help="repo path, or a name under ~/source (default: cwd)")
    ap.add_argument("--execute", action="store_true",
                    help="actually act on decisions (default: report only)")
    args = ap.parse_args()
    return run(args.repo, args.execute)


if __name__ == "__main__":
    raise SystemExit(main())
