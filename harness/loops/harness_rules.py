#!/usr/bin/env python3
"""Per-repo harness rules — `.harness-rules` in the repo root.

Replaces the old central `loops/config.json`, which was a registry of my repos
living in the fleet config: every repo had to be enrolled by hand before any
command would run there, the plumbing it carried (`path`, `github`,
`default_branch`) was all derivable from the checkout anyway, and three of its
documented fields (`ci_gate`, `worktree_isolation`, `consensus_threshold`) were
read by nothing at all. Now a repo describes itself, exactly as it already does
for `create-worktree` via `.worktree.json`, and an unconfigured repo still works
off the built-in defaults instead of hard-failing.

WHICH REF THE RULES COME FROM — the one security-relevant choice here.

An in-tree rules file means repo content influences what the harness does. For
the flows a human triggers that is not a new door: /epic's executor already runs
with `bypassPermissions` and a full shell, so it could run `gh pr merge` itself
without touching any config, and anyone able to commit to a default branch can
already put arbitrary code in the build.

The exception is the lander's red-CI fixer. That agent is deliberately edit-only
(`--permission-mode acceptEdits`, no bash — the loop owns commit+push, and the
merge decision is made by Python), and it operates on an upstream-authored
dependabot branch. Reading rules from that branch would let a poisoned PR rewrite
the policy governing its own review, reaching something the agent otherwise
cannot. So:

    unattended (the timer)  -> git show origin/<default-branch>:.harness-rules
    interactive (you typed it) -> the working tree

A human at the keyboard IS the authorization, and editing the file locally takes
effect immediately. Unattended runs only honour rules that were merged to the
default branch. Set HARNESS_UNATTENDED=1 (run-loop.sh does) to select the
unattended read.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import TextIO

RULES_FILENAME = ".harness-rules"

# Where a bare `--repo <name>` is looked up when it isn't a path.
REPO_ROOT = Path(os.environ.get("HARNESS_REPO_ROOT") or Path.home() / "source")

# The old config.json "defaults" block, now built in. A repo that ships no
# .harness-rules gets exactly this, which is deliberately the safe end of every
# switch: no auto-merge, no unattended loop, edit-only headless agents.
DEFAULTS: dict = {
    "enabled": True,
    "auto_merge": "dependabot_patch_minor",
    "dependabot_author": "app/dependabot",
    "headless_permission_mode": "acceptEdits",
    "reviewers": {
        "claude": {"enabled": True, "model": "opus"},
        # Empty model/effort == the codex CLI's own defaults. The slug is
        # deliberately NOT pinned globally: `opus` is a floating alias that
        # always resolves to the current model, whereas codex slugs are
        # versioned build names (gpt-5.6-luna, gpt-5.5, …) that get retired,
        # and one the installed CLI is too old for is refused by the API with
        # "requires a newer version of Codex" — which costs the panel a whole
        # vendor. Pin per repo to experiment; a lost reviewer is shouted, not
        # footnoted, and the report names the model that actually ran.
        # effort: low|medium|high|xhigh|max|ultra (per-model support varies).
        "codex": {"enabled": True, "model": "", "effort": ""},
        # Off by default, unlike claude/codex: `gemini` is a workstation-only
        # package (it authenticates against a personal Google account, so it
        # never reaches sisyphus), and the machines differ in which harnesses
        # they carry. A repo that wants the third vendor asks for it, rather
        # than every repo on every box inheriting a reviewer half of them
        # cannot run. Enable per repo, or reach for it ad hoc with
        # `panel.py --reviewers claude,codex,gemini`.
        "gemini": {"enabled": False, "model": ""},
        # Off by default for the same reason as gemini — `pi` is a workstation
        # package, not on every box. It is the widest-reach member when enabled:
        # it fronts many providers, so its `model` is a full provider/id pattern
        # (`openrouter/moonshotai/kimi-k3`), not a bare slug, and that is how the
        # panel reaches a vendor none of the other three CLIs can. `effort` maps
        # to its `--thinking` (off|minimal|low|medium|high|xhigh|max).
        "pi": {"enabled": False, "model": "", "effort": ""},
        "sonarqube": {"enabled": False},
    },
    "review_panel": {
        "skip_title_patterns": [
            "merge .* into", "promote", "^release:", "land today",
            "adopt ruff", "format-the-world", "deduplicate", "consolidate",
            "relocate top-level",
        ],
        "judge_model": "opus",
        # Chars of diff each model is given. `null` — the default — means the
        # whole diff: the number that used to be here was inherited from the
        # kernel's argv limit and outlived it, and a reviewer handed a prefix
        # cannot tell, so it reports confidently on the part it saw. Set one only
        # if a model you run genuinely cannot take the change; override per
        # reviewer with `reviewers.<name>.max_diff_chars` and the judge with
        # `judge_max_diff_chars` (both inherit this when unset). A cut diff is
        # reported as truncation, naming WHICH reviewers were cut and at what.
        "max_diff_chars": None,
    },
    "loops": {
        "dependabot_lander": False,
        "stacked_driver": False,
        "issue_executor": False,
    },
    "epic": {
        "landing": "auto",
        "sub_pr_merge": "auto",
        "auto_finish": False,
        "executor_worktree_args": [],
        "min_free_mb": 2048,
        # Left at a path that will not exist in a repo without alembic, so the
        # linear-heads guard returns None and no-ops. Do NOT "clear" this to "":
        # Path(repo)/"" IS the repo root, so an empty value makes the guard think
        # migrations exist and it stops no-opping.
        "migrations_dir": "migrations/versions",
    },
}

# Blocks merged one level deep rather than replaced wholesale, so a repo can set
# `reviewers.sonarqube` without having to restate claude and codex.
_DEEP_BLOCKS = ("reviewers", "review_panel", "loops", "epic")


class RepoNotFound(Exception):
    """The --repo spec didn't resolve to a git checkout."""


def _git(path: str | Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(path), *args],
                          capture_output=True, text=True)


def unattended() -> bool:
    return os.environ.get("HARNESS_UNATTENDED") == "1"


def find_repo(spec: str | None) -> Path:
    """Resolve a --repo spec to a git repo root.

    Accepts a path, a bare name (looked up under HARNESS_REPO_ROOT, default
    ~/source), or nothing at all — in which case the cwd's repo is used.
    """
    if not spec:
        cand = Path.cwd()
    elif "/" in spec or Path(spec).is_dir():
        cand = Path(spec).expanduser()
    else:
        cand = REPO_ROOT / spec
        if not cand.is_dir():
            raise RepoNotFound(
                f"no repo '{spec}': not a path, and {cand} does not exist. "
                f"Pass a path, or set HARNESS_REPO_ROOT.")

    r = _git(cand, "rev-parse", "--show-toplevel")
    if r.returncode != 0:
        raise RepoNotFound(f"{cand} is not a git repository")
    return Path(r.stdout.strip())


def detect_github(root: Path) -> str:
    """owner/name for `gh --repo`, from the origin remote."""
    r = _git(root, "remote", "get-url", "origin")
    if r.returncode != 0:
        return ""
    url = r.stdout.strip()
    # git@host:owner/name.git | https://host/owner/name(.git)
    tail = url.split(":", 1)[-1] if url.startswith("git@") else "/".join(url.split("/")[-2:])
    return tail.removesuffix(".git").strip("/")


def detect_default_branch(root: Path) -> str:
    """The remote's HEAD. `origin/HEAD` is a local cache of it and can be stale
    or absent (a bare `git clone --branch` never sets it), so fall back to
    asking the remote, then to whatever common name actually exists."""
    r = _git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().rsplit("/", 1)[-1]
    r = _git(root, "ls-remote", "--symref", "origin", "HEAD")
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            if line.startswith("ref:"):
                return line.split()[1].rsplit("/", 1)[-1]
    for name in ("main", "master"):
        if _git(root, "rev-parse", "--verify", f"origin/{name}").returncode == 0:
            return name
    return "main"


def _read_rules(root: Path, default_branch: str, from_default_branch: bool) -> tuple[dict, str]:
    """Return (rules, provenance). Missing file is not an error — it means
    'use the defaults', which is the whole point of dropping the registry."""
    if from_default_branch:
        r = _git(root, "show", f"origin/{default_branch}:{RULES_FILENAME}")
        if r.returncode != 0:
            return {}, f"none on origin/{default_branch} (defaults)"
        try:
            return json.loads(r.stdout), f"origin/{default_branch}"
        except json.JSONDecodeError as e:
            raise SystemExit(f"{RULES_FILENAME} on origin/{default_branch} is not valid JSON: {e}")

    p = root / RULES_FILENAME
    if not p.is_file():
        return {}, "none (defaults)"
    try:
        return json.loads(p.read_text()), str(p)
    except json.JSONDecodeError as e:
        raise SystemExit(f"{p} is not valid JSON: {e}")


def resolve_repo(spec: str | None, *, from_default_branch: bool | None = None) -> dict:
    """Full config for a repo: built-in defaults, overlaid with its
    `.harness-rules`, plus the plumbing (path/github/default_branch) detected
    from the checkout rather than declared.

    The returned dict is the same shape the old load_repo_cfg() produced, so
    callers read `cfg["github"]`, `cfg["loops"][...]` etc. unchanged.
    """
    if from_default_branch is None:
        from_default_branch = unattended()

    root = find_repo(spec)
    default_branch = detect_default_branch(root)
    rules, provenance = _read_rules(root, default_branch, from_default_branch)

    cfg = {**DEFAULTS, **rules}
    for block in _DEEP_BLOCKS:
        base, over = DEFAULTS.get(block, {}), rules.get(block, {})
        if isinstance(base, dict) and isinstance(over, dict):
            merged = {**base, **over}
            # reviewers is two levels deep (reviewers.claude.model)
            if block == "reviewers":
                for rname, rbase in base.items():
                    if isinstance(rbase, dict) and isinstance(over.get(rname), dict):
                        merged[rname] = {**rbase, **over[rname]}
            cfg[block] = merged

    # Detected, never declared — a rules file that sets these is ignored, since
    # the checkout in front of us is the authority on where and what it is.
    cfg["path"] = str(root)
    cfg["name"] = rules.get("name") or root.name
    cfg["default_branch"] = default_branch
    cfg["github"] = detect_github(root)
    cfg.setdefault("executor_pr_base", default_branch)
    cfg["_rules_from"] = provenance

    if not cfg["github"]:
        raise SystemExit(
            f"{root} has no 'origin' remote — the harness addresses repos via "
            f"`gh --repo owner/name`, so it cannot act here.")
    return cfg


def describe(cfg: dict) -> str:
    """One line for the top of a report, so which rules applied is never a guess."""
    return (f"[{cfg['name']}] {cfg['github']} @ {cfg['default_branch']} — "
            f"rules: {cfg['_rules_from']}"
            + ("  (unattended)" if unattended() else ""))


def read_dotenv(root: Path | str) -> dict[str, str]:
    """Parse a repo's `.env` into a dict. Missing file -> {}.

    This is the source of record for credentials on a WORK machine, where there
    is no 1Password/sops and no login-time export — the repo carries its own
    `.env` and that is all there is. Deliberately minimal (stdlib only, no
    python-dotenv): `KEY=value`, an optional `export ` prefix, and surrounding
    single/double quotes are stripped. A `#` is only a comment at the start of a
    line — never mid-value, because tokens contain punctuation and silently
    truncating a credential at a `#` is a miserable thing to debug.

    Never log the values. `.env` must be gitignored; see dotenv_is_tracked().
    """
    p = Path(root) / ".env"
    out: dict[str, str] = {}
    try:
        text = p.read_text()
    except (OSError, UnicodeDecodeError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.removeprefix("export ").strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def dotenv_is_tracked(root: Path | str) -> bool:
    """True if `.env` is committed to git — i.e. a credential is in the repo's
    history. Callers warn loudly; we do not silently refuse to read it, since
    the token may be the only one available and the run is otherwise fine."""
    r = _git(root, "ls-files", "--error-unmatch", ".env")
    return r.returncode == 0


# ------------------------------------------------------ headless agent runs
#
# The loops run `claude -p` unattended, from a timer. There is no terminal for
# an agent's account of itself to land in, so whatever the loop does not capture
# is not merely unread — it is gone. That matters because the interesting
# failure EXITS ZERO: a tool the agent needs hits a permission rule headless mode
# cannot prompt for, it is auto-denied, and the agent finishes tidily having
# changed nothing. `check=True` sees success. The loop then reports its own
# no-effect observation ("agent made no edits") — a sentence indistinguishable
# from "there was nothing to fix", which is the opposite claim (#19, #31).
#
# So: capture both streams, and when a run produced no effect, print the best
# line the agent gave us next to the observation. Capturing must not cost the
# live log, hence run_agent's pass-through.


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


def tail_gist(text: str, limit: int = 200) -> str:
    """The END of `text`, collapsed onto one line.

    For stdout the tail is the informative end, not the head: `claude -p` streams
    its working and finishes with the conclusion, so the last thing it said is
    the thing worth quoting on a one-line report."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else "…" + flat[-limit:]


def agent_gist(proc: subprocess.CompletedProcess) -> str:
    """The line most likely to explain what a headless agent did, or would not do.

    stderr first — that is where the harness itself complains (a rejected flag, an
    API refusal) — then the tail of stdout, which in `claude -p` is the agent's own
    final message. The denial that motivated #31 is described THERE and nowhere
    else, so a stderr-only account would still report the interesting case as
    silence."""
    return stderr_gist(proc.stderr or "") or tail_gist(proc.stdout or "")


def agent_failure(proc: subprocess.CompletedProcess) -> str:
    """Why this run must not be read as a completed one — "" if it looks real.

    Two shapes, and `check=True` only ever caught the first:
      * a non-zero exit;
      * exit 0 with nothing on stdout, which is #19's signature. A real run always
        says something, so an empty reply is a failed CLI invocation wearing a
        success exit code."""
    if proc.returncode != 0:
        why = agent_gist(proc)
        return f"exited {proc.returncode}" + (f" ({why})" if why else "")
    if not (proc.stdout or "").strip():
        why = stderr_gist(proc.stderr or "")
        return "exited 0 having printed nothing" + (f" ({why})" if why else "")
    return ""


def _pump(src: TextIO, sink: TextIO, buf: list[str]) -> None:
    """Copy a child stream to ours line by line, keeping a copy."""
    with src:
        for line in src:
            buf.append(line)
            sink.write(line)
            sink.flush()


def run_agent(args: list[str],
              cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    """Run a headless agent, capturing both streams AND passing them through.

    `capture_output=True` alone would buy the diagnosis by diverting the log:
    whatever the agent writes would stop reaching the journal these runs are read
    from, and a run that shows nothing until the process exits cannot be told from
    a wedged one. So each stream is pumped to ours as it arrives AND kept — what
    lands in the log is exactly what landed there before; capturing only adds a
    copy. The result is an ordinary CompletedProcess, so callers read `proc.stdout`
    as if it had been captured the plain way.

    stdin is DEVNULL, as it always was: an unattended agent that decides to ask a
    question must read EOF rather than inherit a terminal and hang the loop.

    No `check`, and no raise at all — including the one case that never reached a
    child process. A CLI missing from PATH, or a worktree that isn't there, is the
    same kind of event as an agent that ran and failed ("this did not happen, here
    is why"), and a caller that has to handle one shape rather than two is a caller
    that handles it. It comes back as exit 127 with the errno on stderr; pair this
    with agent_failure() and every route out of here reports the same way.
    """
    try:
        proc = subprocess.Popen(
            args, cwd=str(cwd) if cwd else None, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    except OSError as e:
        # errno and strerror, not the bare class name: "OSError" sent three people
        # looking for a crash that was "Argument list too long" (see panel.run_cli).
        why = " ".join(str(x) for x in (e.errno, e.strerror or e) if x)
        return subprocess.CompletedProcess(args, 127, "", f"OSError {why}\n")
    out: list[str] = []
    err: list[str] = []
    pumps = [threading.Thread(target=_pump, args=(proc.stdout, sys.stdout, out)),
             threading.Thread(target=_pump, args=(proc.stderr, sys.stderr, err))]
    for t in pumps:
        t.start()
    rc = proc.wait()
    for t in pumps:
        t.join()
    return subprocess.CompletedProcess(args, rc, "".join(out), "".join(err))


def discover(root: Path | None = None) -> list[Path]:
    """Repos under the search root that ship a rules file. Used by run-loop.sh
    instead of a central list. Only sees repos whose WORKING TREE has the file —
    a checkout sitting on a branch that deleted it is skipped, which is the safe
    direction for a sweep that can merge things."""
    base = Path(root or REPO_ROOT)
    if not base.is_dir():
        return []
    return sorted(p.parent for p in base.glob(f"*/{RULES_FILENAME}"))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Inspect resolved harness rules.")
    ap.add_argument("--repo", help="path or name (default: cwd)")
    ap.add_argument("--discover", action="store_true", help="list repos with a rules file")
    ap.add_argument("--json", action="store_true", help="dump the resolved config")
    a = ap.parse_args()
    if a.discover:
        for p in discover():
            print(p)
        raise SystemExit(0)
    try:
        c = resolve_repo(a.repo)
    except RepoNotFound as e:
        raise SystemExit(str(e))   # a bad --repo is user error, not a crash
    print(json.dumps(c, indent=2) if a.json else describe(c))
