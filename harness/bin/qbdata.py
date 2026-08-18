"""Fleet data and the formatting both dashboards share.

One source of truth for "what is the board saying", so the snapshot renderer
(qb-dash) and the clickable one (qb-dash-tui) cannot drift on a fix.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import ssl
import subprocess
import urllib.request
from datetime import datetime, timezone

REPO = "prisonblues/quarterback"          # the fallback, not the answer
REPO_URL = f"https://github.com/{REPO}"


def repo_slug(path: str = ".") -> str | None:
    """'owner/name' from a checkout's origin remote, or None.

    Handles scp syntax, https://, ssh:// and a .git suffix — the same shapes the
    MCP server's own repo_slug() has always parsed, and the same lesson as #148:
    a repo spelled by its caller and a repo read from git are two values that
    disagree silently.
    """
    try:
        got = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=10)
    except Exception:                             # noqa: BLE001
        return None
    if got.returncode != 0:
        return None
    url = got.stdout.strip().removesuffix(".git")
    if not url:
        return None
    tail = re.sub(r"^(git@|ssh://|https?://)[^/:]+[:/]", "", url)
    parts = [p for p in tail.split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


_repos: list[str] | None = None


def resolve_repos() -> list[str]:
    """Which repositories this dashboard reports on, most relevant first.

    QB_DASH_REPOS names them outright, comma separated, which is how one screen
    watches a fleet that works in three. Otherwise it is the repo of the
    directory the dashboard was started in — hardcoding one meant a screen
    pointed at nix-fleet reported quarterback's pull requests and said nothing
    about having done so.

    Worked out ONCE per process. Neither the environment nor the cwd's origin
    changes under a running dashboard, and the fallback shells out to git — which
    a per-row caller (the plan panel asks "is this one of mine?" of every item it
    draws) would turn into a subprocess per row per redraw.
    """
    global _repos
    if _repos is None:
        named = os.environ.get("QB_DASH_REPOS", "").strip()
        _repos = ([r.strip() for r in named.split(",") if r.strip()] if named
                  else [repo_slug(os.environ.get("QB_DASH_REPO") or os.getcwd()) or REPO])
    return _repos


def short_repo(repo: str) -> str:
    """'prisonblues/quarterback' → 'quarterback'. The owner never distinguishes."""
    return repo.split("/", 1)[-1]


_REPO_COLOURS = ["cyan", "magenta", "green", "yellow", "blue", "red"]
_repo_colour: dict[str, str] = {}


def repo_colour(name: str) -> str:
    """A stable colour per repo, shared by every panel and both renderers.

    The colour is the fastest way to see that a PR and the agent working it are
    in the same place, so it has to mean the same thing in FLEET as in OPEN PRs.
    """
    key = short_repo(name or "")
    if key not in _repo_colour:
        _repo_colour[key] = _REPO_COLOURS[len(_repo_colour) % len(_REPO_COLOURS)]
    return _repo_colour[key]


def ago(stamp: str | None) -> str:
    """'4m', '2h10m' — how long since an ISO timestamp. '' if unparseable."""
    if not stamp:
        return ""
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return ""
    secs = int((datetime.now(timezone.utc) - then).total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def until(stamp: str | None) -> str:
    """'12m' left on a lease/claim, '—' once it is in the past."""
    if not stamp:
        return "—"
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return "—"
    secs = int((then - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "—"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def minutes_left(stamp: str | None) -> int | None:
    """Whole minutes remaining, for deciding what to colour red."""
    if not stamp:
        return None
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((then - datetime.now(timezone.utc)).total_seconds() // 60)


#: How long a `working` may stand before a reader calls it stalled. Tool calls
#: refresh it, so the gap this has to clear is the longest a session legitimately
#: goes without one — a long think, a slow build, a big edit in a single pass.
#:
#: It MUST agree with the same constant in the footer (nix-fleet's
#: home/claude/scripts/statusline.sh, STALL_AFTER). Two readers of one beacon
#: disagreeing about when it goes stale is worse than either threshold being
#: wrong: the dashboard and the pane's own bar would describe the same seat
#: differently, and there is no way to tell from the outside which one to believe.
STALL_AFTER = 480


def agent_state(agent: dict) -> tuple[str, str]:
    """(word, style) for what a live agent is doing — '' when it never said.

    The board stores what the holder reported; `stalled` is concluded HERE, from
    the age of that report, and is the reason `state_at` travels with `state`.
    A pane that said `working` and then went quiet is the failure this whole
    field exists to surface: it looks identical to a busy one from the outside.

    `waiting` and `input` do not go stale. A pane that has been waiting on a
    human since lunch is still waiting on that human — ageing it into `stalled`
    would hide the one state somebody is actually scanning for.
    """
    state = agent.get("state") or ""
    if not state:
        return "", "grey50"
    if state == "working":
        try:
            then = datetime.fromisoformat((agent.get("state_at") or "").replace("Z", "+00:00"))
        except ValueError:
            return "working", "grey50"
        if (datetime.now(timezone.utc) - then).total_seconds() >= STALL_AFTER:
            return "stalled", "bold red"
        return "working", "grey50"
    return {"waiting": ("waiting", "bold yellow"),
            "input": ("input", "bold magenta")}.get(state, (state, "grey50"))


def short_key(key: str) -> str:
    """'prisonblues/quarterback:2.40' → 'quarterback:2.40'.

    The owner is the same for every repo the fleet touches, so it is 12 columns
    that never distinguish two rows.
    """
    return key.split("/", 1)[-1] if key.count("/") == 1 else key


def clip(s: str | None, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def seat_number(holder: str | None) -> int | None:
    """1 for 'zeus/seat-1'. None for anything that is not a seat."""
    if not holder or "/seat-" not in holder:
        return None
    tail = holder.split("/seat-", 1)[1]
    return int(tail) if tail.isdigit() else None


class BoardConfig:
    """Where the board is and how to authenticate to it."""

    def __init__(self, base_url: str, token: str, agent: str) -> None:
        self.base_url, self.token, self.agent = base_url.rstrip("/"), token, agent


def resolve_config() -> BoardConfig:
    """Environment first, then the per-host config file.

    The same contract qb-seat implements in bash, and read the same way: the
    config is an unrestricted shell script, so it is SOURCED IN A SUBSHELL with
    three values read back out. Sourcing it into this process would let it
    replace anything it liked; parsing it with a regex would get the quoting
    wrong on the day someone puts a `$(…)` in their token command.

    Deliberately no mcp_server import. Depending on it made the dashboard need a
    built checkout of this repo's mcp/ — which is a thing an INSTALLED harness
    has no reason to have, and it is why `qb` failed on a freshly rebuilt host
    while every test passed on the machine that wrote it.
    """
    url = os.environ.get("QUARTERBACK_BASE_URL", "")
    token = os.environ.get("QUARTERBACK_TOKEN", "")
    token_cmd = os.environ.get("QUARTERBACK_TOKEN_CMD", "")

    if not url or not (token or token_cmd):
        config = (os.environ.get("QUARTERBACK_CONFIG")
                  or os.path.join(os.environ.get("XDG_CONFIG_HOME")
                                  or os.path.expanduser("~/.config"),
                                  "quarterback", "config"))
        if os.path.isfile(config):
            script = (f'. {shlex.quote(config)} >&2 || exit 1\n'
                      'printf "url=%s\\n" "${QUARTERBACK_BASE_URL:-}"\n'
                      'printf "token=%s\\n" "${QUARTERBACK_TOKEN:-}"\n'
                      'printf "token_cmd=%s\\n" "${QUARTERBACK_TOKEN_CMD:-}"\n')
            got = subprocess.run(["bash", "-c", script], capture_output=True,
                                 text=True, timeout=15)
            if got.returncode == 0:
                for line in got.stdout.splitlines():
                    name, _, value = line.partition("=")
                    if name == "url" and not url:
                        url = value
                    elif name == "token" and not token:
                        token = value
                    elif name == "token_cmd" and not token_cmd:
                        token_cmd = value

    if not token and token_cmd:
        got = subprocess.run(["bash", "-c", token_cmd], capture_output=True,
                             text=True, timeout=30)
        token = got.stdout.strip() if got.returncode == 0 else ""

    if not url:
        raise RuntimeError("no board configured (QUARTERBACK_BASE_URL is unset "
                           "and the site config did not supply one)")
    return BoardConfig(url, token, socket.gethostname().split(".", 1)[0])


def _ssl_context():
    """A context that trusts something, on interpreters that trust nothing.

    A uv-installed standalone Python has no CA bundle of its own and no NixOS
    ssl paths, so `urllib` there fails every HTTPS request with
    CERTIFICATE_VERIFY_FAILED — while the same code works on the interpreter the
    harness packages. That asymmetry is invisible until someone runs the
    dashboard from a checkout's venv and sees "board unreachable" on a board that
    is up, which is how this was found: in a pane, next to a working copy in the
    shell beside it.

    certifi is not a dependency; it is used when the interpreter already has it,
    which is exactly the case where the default store is empty.
    """
    try:
        import certifi
    except ImportError:
        return None                                 # the default store, which is fine
    return ssl.create_default_context(cafile=certifi.where())


class BoardClient:
    """The two GETs this dashboard makes. stdlib only, on purpose."""

    def __init__(self, cfg: BoardConfig) -> None:
        self.cfg = cfg

    def get(self, path: str) -> dict:
        req = urllib.request.Request(f"{self.cfg.base_url}{path}")
        if self.cfg.token:
            req.add_header("Authorization", f"Bearer {self.cfg.token}")
        with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode())

    def active(self) -> dict:
        return self.get("/active")

    def claims(self) -> dict:
        return self.get("/claims")


def board_client():
    cfg = resolve_config()
    return BoardClient(cfg), cfg


def fetch_board(client) -> dict:
    """Everything the board can tell us. Never raises — a dead board is a state."""
    out: dict = {"agents": [], "subagents": [], "claims": [], "error": None}
    try:
        active = client.active()
        out["agents"] = active.get("agents", [])
        out["subagents"] = active.get("subagents", [])
        out["claims"] = [
            c for c in client.claims().get("claims", [])
            if not c.get("released") and not c.get("lapsed")
        ]
    except Exception as exc:                      # noqa: BLE001 — display it, don't die
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _gh_list(kind: str, repo: str, fields: str) -> tuple[list[dict], str | None]:
    """One `gh <kind> list` against one repo, every row tagged with that repo.

    The tag is what lets the panels say where a row came from, and it is applied
    here rather than at render time so nothing downstream can hold a row whose
    origin it cannot name.
    """
    try:
        raw = subprocess.run(
            ["gh", kind, "list", "--repo", repo, "--state", "open",
             "--limit", "30", "--json", fields],
            capture_output=True, text=True, timeout=45,
        )
        if raw.returncode != 0:
            return [], f"{short_repo(repo)}: {clip(raw.stderr, 50)}" or f"gh exit {raw.returncode}"
        rows = json.loads(raw.stdout)
        for row in rows:
            row["repo"] = repo
        return rows, None
    except Exception as exc:                      # noqa: BLE001
        return [], f"{short_repo(repo)}: {type(exc).__name__}"


def _gh_list_many(kind: str, fields: str, repos: list[str] | None = None):
    """Every repo this dashboard watches. One failing repo is reported, not fatal:
    a token that cannot see one of three is still worth three panels."""
    out: list[dict] = []
    errs: list[str] = []
    for repo in (repos if repos is not None else resolve_repos()):
        rows, err = _gh_list(kind, repo, fields)
        out.extend(rows)
        if err:
            errs.append(err)
    return out, ("; ".join(errs) if errs else None)


def fetch_prs(repos: list[str] | None = None) -> tuple[list[dict], str | None]:
    return _gh_list_many(
        "pr", "number,title,isDraft,updatedAt,statusCheckRollup,headRefName", repos)


def fetch_issues(repos: list[str] | None = None) -> tuple[list[dict], str | None]:
    return _gh_list_many("issue", "number,title,updatedAt,labels,assignees", repos)


def issue_claims(claims: list[dict], repo: str = REPO) -> dict[int, dict]:
    """{issue number → the claim on it}, for `repo`'s issues.

    The board namespaces an issue claim as `owner/repo#n` and that `n` is the
    number `gh issue list` reports, so the join wants no lookup table. Another
    repo's key is skipped rather than joined on the number alone — two repos
    both have a #12, and marking ours held because theirs is would send the
    next seat past the one issue it should have taken.

    The repo is an argument, not a constant read inside: which repo a dashboard
    is showing is on its way to being derived from the checkout rather than
    hardcoded here (#176), and a caller that has worked it out should not have
    to reach past this function to use it.
    """
    held: dict[int, dict] = {}
    for c in claims:
        prefix, _, number = (c.get("key") or "").strip().rpartition("#")
        if prefix == repo and number.isdigit():
            held.setdefault(int(number), c)
    return held


def issue_key(row: dict) -> str:
    """'prisonblues/quarterback#176' — how the board namespaces a claim.

    The identity of an issue is the repo AND the number. Once the panels show
    more than one repo, a bare number stops being unique: two repos both have a
    #12, and marking ours held because theirs is would send the next seat past
    the one issue it should have taken.
    """
    return f"{row.get('repo') or REPO}#{row.get('number')}"


def claims_by_issue(claims: list[dict]) -> dict[str, dict]:
    """{'owner/repo#n' → the claim on it}, across every repo on the board."""
    held: dict[str, dict] = {}
    for c in claims:
        key = (c.get("key") or "").strip()
        prefix, sep, number = key.rpartition("#")
        if sep and prefix.count("/") == 1 and number.isdigit():
            held.setdefault(key, c)
    return held


def sort_issues(issues: list[dict], held: dict) -> list[dict]:
    """Free issues first, newest first inside each group.

    The free ones are what the panel is for — a seat reads it to find work
    nobody has taken — and on a pane that fits a dozen rows, a run of held
    issues along the top is the list failing at its one job.
    """
    def taken(issue: dict) -> bool:
        # Accepts either index: keyed by 'owner/repo#n' (what the panels use now
        # that they show several repos) or by bare number (the older shape).
        return issue_key(issue) in held or issue.get("number") in held

    return sorted(issues, key=lambda i: (taken(i), -(i.get("number") or 0)))


def ci_state(pr: dict) -> tuple[str, str]:
    """(glyph, colour) for a PR's check rollup."""
    checks = pr.get("statusCheckRollup") or []
    if not checks:
        return "·", "grey50"
    concs = [c.get("conclusion") or "" for c in checks]
    if any(c in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED") for c in concs):
        return "✗", "red"
    if any(not c for c in concs):
        return "◐", "yellow"
    if all(c in ("SUCCESS", "SKIPPED", "NEUTRAL") for c in concs):
        return "✓", "green"
    return "?", "grey50"


# ---- the plan ----------------------------------------------------------------
#
# The board's plan is what the fleet agreed to do next, in order, one list per
# repo plus a fleet-wide one. It is the half a dashboard could not show before:
# FLEET says who is here, CLAIMED says what they hold, and neither answers "and
# what is that work FOR" — the plan does, and it names the item that is next
# when nobody is on it.

PLAN_LIMIT = 200        # a plan is tens of rows by design; this is a backstop


def fetch_plan(client) -> tuple[list[dict], str | None]:
    """Every open plan item on the board, in the board's own order.

    No repo filter, deliberately: a repo read widens to the fleet-wide items but
    still hides the other repos' lists, and this panel is called PLANS because
    the fleet runs more than one. Which list a row belongs to is the repo column.

    Never raises — a dead board is a state the panel renders, the same way
    :func:`fetch_board` treats it.
    """
    try:
        data = client.get(f"/plan?limit={PLAN_LIMIT}")
    except Exception as exc:                      # noqa: BLE001 — display it, don't die
        return [], f"{type(exc).__name__}: {exc}"
    return data.get("items") or [], None


def plan_state(item: dict) -> tuple[str, str]:
    """(glyph, colour) for a plan row: running, blocked, or free to take."""
    if item.get("claim"):
        return "▶", "green"
    if item.get("blocked_by"):
        return "⊘", "grey50"
    return "○", "cyan"


def plan_ref(item: dict) -> str:
    """'#78' for an item that points at an issue or PR, '' for one that does not.

    Most plan items are a line of plan and nothing else; the ref is the link to
    where the *what* and the *why* live.
    """
    value = (item.get("ref") or {}).get("value")
    return f"#{value}" if value else ""


def plan_repo(item: dict, repos: list[str] | None = None) -> str | None:
    """The GitHub slug behind a plan item's repo, or None if it names no repo.

    A plan item's repo is free text — the fleet has lists under both
    'prisonblues/quarterback' and a bare '65lowther' — so a bare name is matched
    against the repos this dashboard watches rather than guessed at. Guessing
    would put an owner on a name that never had one, and the ⚒ would then start
    work on somebody else's issue of the same number.
    """
    repo = item.get("repo")
    if not repo:
        return None                               # fleet scope: no repo to name
    if "/" in repo:
        return repo
    for watched in (repos if repos is not None else resolve_repos()):
        if short_repo(watched) == repo:
            return watched
    return None


def plan_issue(item: dict, repos: list[str] | None = None) -> dict | None:
    """The issue behind a plan item, shaped like a `gh issue list` row — or None.

    What the ⚒ needs and all it needs: a number, and the repo it belongs to. An
    item with no ref, a `pr` ref, or a repo that cannot be resolved to a slug has
    no issue to fix and the icon stays dim.
    """
    ref = item.get("ref") or {}
    value = str(ref.get("value") or "")
    repo = plan_repo(item, repos)
    if ref.get("kind") != "issue" or not value.isdigit() or not repo:
        return None
    return {"number": int(value), "repo": repo, "title": item.get("title")}


def sort_plan(items: list[dict], repos: list[str] | None = None) -> list[dict]:
    """Running first, then what is free to take, then what is blocked.

    Inside each band the board's own order is kept — the plan is an ordered list
    and the order is the point — with the repos this dashboard watches ahead of
    the ones it only overhears. Blocked items sink because they are the one band
    a reader can do nothing about.
    """
    watched = {short_repo(r) for r in (repos if repos is not None else resolve_repos())}

    def band(item: dict) -> int:
        if item.get("claim"):
            return 0
        return 2 if item.get("blocked_by") else 1

    def near(item: dict) -> int:
        repo = item.get("repo")
        return 0 if not repo or short_repo(repo) in watched else 1

    return sorted(items, key=lambda i: (band(i), near(i)))


def plan_counts(items: list[dict]) -> tuple[int, int]:
    """(running, blocked) — the two numbers both panel titles report."""
    running = sum(1 for i in items if i.get("claim"))
    blocked = sum(1 for i in items if not i.get("claim") and i.get("blocked_by"))
    return running, blocked


def plan_who(item: dict) -> tuple[str, str]:
    """(text, colour) for the right-hand column: who has it, or what it waits on.

    Three different facts share one column because only one of them is ever true
    of a row: a claimed item has a holder, a blocked one has something to wait
    for, and a free one has only how long it has been sitting there.
    """
    claim = item.get("claim")
    if claim:
        return (claim.get("holder") or "?").split("/", 1)[-1], "yellow"
    blockers = item.get("blocked_by") or []
    if blockers:
        first = blockers[0].get("ref")
        return (f"waits #{first}" if first else f"waits ×{len(blockers)}"), "grey50"
    return ago(item.get("updated")), "grey50"


def claim_label(key: str, plan: list[dict] | None = None) -> str:
    """What a claim is ON, in words a human can read off a pane.

    A claim on a plan item is keyed ``plan:<uuid>``: right for a lock, useless on
    a screen — 36 hex characters that say only "something on the plan". Given the
    plan, the item's title goes in instead. Without it the raw key stays, because
    a key nobody can resolve still beats a blank.
    """
    key = key or "?"
    wanted = key.split(":", 1)[1] if key.startswith("plan:") else None
    for item in (plan or []):
        if wanted and item.get("item_id") == wanted:
            head = " ".join(x for x in ("plan", plan_ref(item)) if x)
            return f"{head} {item.get('title') or '?'}"
    return short_key(key)


def plan_detail(item: dict) -> str:
    """The whole of a plan row, for the detail line under the tables.

    The note is why this is worth a click: it is the reasoning behind the item's
    place in the order, it exists nowhere else — not in the issue, not on the
    board tape — and it does not fit in a 44-column title cell.
    """
    bits = [f"{short_repo(item.get('repo') or 'fleet')} {plan_ref(item)}".strip(),
            item.get("title") or "(untitled)"]
    if item.get("phase"):
        bits.append(f"[{item['phase']}]")
    claim = item.get("claim")
    if claim:
        held = f"held by {claim.get('holder') or '?'}"
        if claim.get("note"):
            held += f" — {claim['note']}"
        bits.append(held)
    blockers = item.get("blocked_by") or []
    if blockers:
        bits.append("waits on " + ", ".join(
            f"{b.get('ref') and '#' + str(b['ref']) or ''} {b.get('title') or ''}".strip()
            for b in blockers))
    if item.get("note"):
        bits.append(item["note"])
    return clip(" · ".join(bits), 400)


# ---- the tmux screen ---------------------------------------------------------
# The dashboard reads the seats off tmux rather than off the board, because they
# are different questions. The board knows which AGENTS are live anywhere on the
# fleet; this knows which PANES are on the screen in front of you, including the
# ones whose agent has exited and left a shell behind. Only the second can be
# closed with a click.

SEAT_FIELDS = ("pane", "seat", "session", "window", "command", "path")


def tmux_seats() -> list[dict]:
    """Every seat pane on this tmux server, lowest seat number first.

    A seat is a pane carrying the @qb_seat option, which is how qb-seats marks
    them and the only handle that survives a pane being added or closed — the
    index shifts and the agent rewrites the title.

    Returns [] rather than raising when there is no tmux, no server, or no
    screen: the dashboard runs inside the screen most of the time and in a bare
    terminal the rest, and an empty SEATS panel is the honest answer to the
    second case.
    """
    if not os.environ.get("TMUX"):
        return []
    fmt = "\t".join("#{%s}" % f for f in
                    ("pane_id", "@qb_seat", "session_name", "window_index",
                     "pane_current_command", "pane_current_path"))
    try:
        got = subprocess.run(["tmux", "list-panes", "-a", "-F", fmt],
                             capture_output=True, text=True, timeout=5)
    except Exception:                             # noqa: BLE001
        return []
    if got.returncode != 0:
        return []
    seats = []
    for line in got.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != len(SEAT_FIELDS) or not parts[1]:
            continue
        seats.append(dict(zip(SEAT_FIELDS, parts)))
    # By seat NUMBER, not by pane order: --add splits off the leftmost pane, so
    # pane order runs 1, 3, 2 on a screen that has had a seat added to it.
    return sorted(seats, key=lambda s: int(s["seat"]) if s["seat"].isdigit() else 0)
