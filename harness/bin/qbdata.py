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
import time
import urllib.error
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


def set_repos(repos: list[str]) -> None:
    """Pin which repositories this process watches, overriding the environment.

    The dashboards take ``--repo`` now, and half of what reads the answer reaches
    :func:`resolve_repos` for itself — ``plan_repo``, ``sort_plan``, ``fetch_prs``
    — rather than taking a list as an argument. Priming the cache is what makes
    one flag reach all of them; threading it through every call site would leave
    whichever one was missed silently watching the cwd, which is the shape of
    #176 all over again.
    """
    global _repos
    cleaned = [r.strip() for r in repos if r and r.strip()]
    _repos = cleaned or None


def repo_arg(value: str) -> str:
    """A ``--repo`` argument — a checkout or an ``owner/name`` slug — as a slug.

    A directory is asked for its origin, which is how "the project this screen is
    set up for" is spelled when you are standing in it. A bare name is REFUSED
    rather than completed: `gh` needs an owner, the fleet works in repos whose
    owner is not this one's, and inventing one aims the PR panel — and the ⚒ that
    starts work off it — at somebody else's repository of the same name.
    """
    value = (value or "").strip().rstrip("/") or "."
    if value in (".", "..") or os.path.isdir(value):
        slug = repo_slug(value)
        if not slug:
            raise ValueError(f"{value}: not a git checkout with an origin remote")
        return slug
    if value.count("/") == 1 and all(part.strip() for part in value.split("/")):
        return value
    raise ValueError(f"{value!r}: neither a checkout nor an owner/name slug")


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


# ---- scope: which project's rows this dashboard is about ---------------------
#
# Every board-derived panel is fleet-wide by construction — FLEET is every live
# agent, CLAIMED every claim, PLANS every repo's list — and a screen is built for
# ONE project. So most rows are somebody else's, and the repo cell is then the
# same word, eleven columns wide, on every line: a 78-column pane spending its
# scarcest thing on a fact the header can state once (#261).
#
# One object holds both halves of the decision because they have to agree. What is
# kept, and whether the column is worth keeping, are the same question asked twice:
# a column dropped from a table whose rows were NOT narrowed shows nothing anywhere,
# and rows narrowed with the column left in place is the waste this exists to end.

#: Opens the dashboard wide instead of on the screen's own repos. Any other value
#: — including nothing — is the narrow default, which is what a seat screen wants
#: every time but the one time it is looking for a peer somewhere else.
SCOPE_ENV = "QB_DASH_SCOPE"
SCOPE_WIDE = ("all", "fleet", "wide")


def repo_name(value: str | None) -> str | None:
    """A repo in the one form these panels can be compared in, or None.

    THREE spellings reach this dashboard for one repository. A lease reports a
    bare ``quarterback``, because what it saw was the checkout's directory; the
    plan and `gh` both report ``prisonblues/quarterback``; and a hand-written plan
    item reports whichever the human typed. Folded to the bare name, lowercased,
    so what gets compared is repositories rather than spellings — comparing the
    slugs would put a seat's own FLEET row out of its own scope.
    """
    return short_repo((value or "").strip()).lower() or None


def claim_repo(key: str | None, plan: list[dict] | None = None) -> str | None:
    """Which repo a claim is against, or None when its key cannot say.

    Three key shapes are in use and only two of them carry a repo: ``owner/repo#12``
    (an issue), ``owner/repo:2.40`` (a release number), and ``plan:<uuid>`` — which
    names an item and not a repo, so the plan is consulted when the caller has it
    and the claim is left unattributed when it does not.

    None means "cannot say", and every caller treats that as in scope. A claim
    whose repo is unknown is not evidence that it belongs to another project, and
    hiding it drops the one row that says somebody already holds the work you were
    about to pick up.
    """
    key = (key or "").strip()
    if not key:
        return None
    if key.startswith("plan:"):
        wanted = key.split(":", 1)[1]
        for item in plan or []:
            if item.get("item_id") == wanted:
                return item.get("repo") or None
        return None
    head = key.split("#", 1)[0].split(":", 1)[0]
    # A head with no owner is not a repo we can name. `gh` and the plan both spell
    # a claim key with its owner, so a bare word here is some other namespace's
    # key, and reading it as a repo would scope rows against a word that is not one.
    return head if "/" in head else None


class Scope:
    """Which rows a dashboard is about, and whether to spend a column saying so."""

    def __init__(self, repos: list[str], on: bool = True) -> None:
        self.repos = list(repos)
        self.on = bool(on)
        self.names = {n for n in (repo_name(r) for r in self.repos) if n}

    @property
    def column(self) -> bool:
        """Is the repo cell worth its eleven columns?

        Not when the rows have been narrowed to ONE repository: every cell then
        carries the same word, and the header says it once for the whole pane. Yes
        for the wide view — telling repos apart is the entire reason to widen — and
        yes for a screen watching two, where the cell still distinguishes rows.
        """
        return not self.on or len(self.names) != 1

    def keeps(self, repo: str | None) -> bool:
        """Does a row in ``repo`` belong on this pane?

        A row whose repo nothing could name STAYS. No repo is not evidence of
        another repo, and dropping it hides a live agent working outside a checkout
        — or a claim whose plan item this process has not fetched — on the strength
        of a missing field. The narrow view is a way to read the fleet, not a claim
        to have accounted for all of it.
        """
        if not self.on or not self.names:
            return True
        name = repo_name(repo)
        return name is None or name in self.names

    def label(self) -> str:
        """What the header says this pane is showing, in one phrase."""
        return ", ".join(sorted(self.names)) if self.on and self.names else "all repos"

    def widened(self) -> Scope:
        """The same watch list, the other way round — what the toggle switches to."""
        return Scope(self.repos, not self.on)


def resolve_scope(repos: list[str] | None = None, on: bool | None = None) -> Scope:
    """The scope a dashboard starts in: narrow, unless told otherwise.

    Narrow by default because that is what a screen is for. ``QB_DASH_SCOPE=all``
    opens wide for a session that is watching the fleet rather than working in it;
    the repos are the ones :func:`resolve_repos` already worked out, so one
    ``--repo`` or ``QB_DASH_REPOS`` aims the filter and the `gh` calls together.
    """
    if on is None:
        on = os.environ.get(SCOPE_ENV, "").strip().lower() not in SCOPE_WIDE
    return Scope(resolve_repos() if repos is None else repos, on)


def in_scope(rows: list[dict], scope: Scope | None,
             repo_of=None) -> tuple[list[dict], int]:
    """The rows a scope keeps, and HOW MANY IT HID.

    The count comes back rather than being dropped because a filtered panel that
    does not say it filtered is a panel quietly lying about the fleet — the same
    defect as a hardcoded repo (#176) one level up. Every caller puts it in the
    panel title, so "nobody else is working" and "nobody else is working *here*"
    can never read the same.
    """
    if scope is None:
        return list(rows), 0
    getter = repo_of or (lambda row: row.get("repo"))
    kept = [row for row in rows if scope.keeps(getter(row))]
    return kept, len(rows) - len(kept)


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


#: How a seat spells itself on the board: `seat-<scope>-<n>`, where the scope is
#: the project the seat sits in and is what stops two screens on one machine both
#: wanting seat 1 (#208). The scope is optional because a seat whose scope slugged
#: away to nothing — or that was deliberately started with an empty QB_SEAT_SCOPE —
#: keeps the bare `seat-<n>` this had before, and the dashboard must go on
#: recognising those.
#:
#: The NUMBER is the last hyphenated field, not the first: a scope may contain
#: hyphens of its own, and `seat-nix-fleet-3` is seat 3 of nix-fleet rather than
#: anything about `nix`. The bound is qb-seat's own 1-99.
SEAT_RE = re.compile(r"^seat-(?:(.+)-)?([1-9][0-9]?)$")

#: The most of `seat-<scope>-<n>` a scope may take, mirroring SEAT_SCOPE_MAX in
#: qb-seat: the board allows 40 characters and `seat-`, a hyphen and two digits
#: account for the other eight.
SCOPE_MAX = 32


def _seat(holder: str | None) -> "re.Match[str] | None":
    """The seat match for a board identity, on the name half of `machine/name`."""
    if not holder:
        return None
    return SEAT_RE.match(holder.rsplit("/", 1)[-1])


def seat_number(holder: str | None) -> int | None:
    """1 for 'zeus/seat-lexray-1' and for 'zeus/seat-1'.

    None for anything that is not a seat.
    """
    match = _seat(holder)
    return int(match.group(2)) if match else None


def seat_machine(holder: str | None) -> str | None:
    """'zeus' for 'zeus/seat-lexray-1'. None for anything that is not a seat.

    The board is the FLEET's, not this box's: two machines can each hold a
    `seat-lexray-1`, so the machine half is part of what identifies a seat and
    leaving it out shows a remote agent's state against a local pane.
    """
    if _seat(holder) is None:
        return None
    machine, sep, _ = (holder or "").partition("/")
    return machine if sep else None


def seat_scope(holder: str | None) -> str | None:
    """'lexray' for 'zeus/seat-lexray-1'.

    None for 'zeus/seat-1', which is a seat numbered across the whole machine,
    and None for anything that is not a seat at all. The two cases are told
    apart by :func:`seat_number`, which answers for the first and not the second.
    """
    match = _seat(holder)
    return match.group(1) if match else None


def slug_scope(text: str | None) -> str | None:
    """Turn a requested scope into the one a seat will actually carry.

    MIRRORS `seat_scope_slug` in qb-seat, and is pinned to it by
    test_the_scope_rule_is_the_one_qb_seat_actually_applies — two implementations
    of one rule is exactly how a dashboard ends up showing one seat's state
    against another seat's pane. Case folding is ASCII-only for the same reason:
    qb-seat folds with `tr '[:upper:]' '[:lower:]'`, which is bytes, where
    str.lower() is Unicode.
    """
    if not text:
        return None
    lowered = "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in text)
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")[:SCOPE_MAX].rstrip("-")
    return slug or None


def scope_of(repo_path: str | None) -> str | None:
    """The scope qb-seat gives a seat working in ``repo_path`` and told nothing else.

    The default half of the rule: a seat is named after its repository's own
    directory.
    """
    return slug_scope(os.path.basename((repo_path or "").rstrip("/")))


def pane_scope(seat: dict) -> str | None:
    """The scope of the seat in a tmux pane, or None when the pane cannot say.

    `@qb_scope` first, because a screen given an explicit QB_SEAT_SCOPE is the one
    case the repository cannot answer for: two screens on ONE repository, which is
    precisely what that knob exists for. `@qb_repo` otherwise, which is the default
    the seat itself computed from its cwd.

    An explicitly EMPTY scope reads here as "the pane cannot say", and that is the
    right answer rather than a gap: an empty scope asks for the machine-wide seat
    numbering, in which a second screen cannot hold the same number at all — so
    there is never a second candidate for the caller to confuse it with.
    """
    return slug_scope(seat.get("scope")) or scope_of(seat.get("repo"))


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


def claim_label(key: str, plan: list[dict] | None = None,
                scope: Scope | None = None) -> str:
    """What a claim is ON, in words a human can read off a pane.

    A claim on a plan item is keyed ``plan:<uuid>``: right for a lock, useless on
    a screen — 36 hex characters that say only "something on the plan". Given the
    plan, the item's title goes in instead. Without it the raw key stays, because
    a key nobody can resolve still beats a blank.

    The scope trims the repo off the front for the same reason the panels drop
    their repo column (#261): on a pane showing one project, ``quarterback#209``
    spends twelve columns to say ``#209``. Only when the scope is that one project
    — the wide view keeps the repo, because there it is what tells two claims apart.
    """
    key = key or "?"
    wanted = key.split(":", 1)[1] if key.startswith("plan:") else None
    for item in (plan or []):
        if wanted and item.get("item_id") == wanted:
            head = " ".join(x for x in ("plan", plan_ref(item)) if x)
            return f"{head} {item.get('title') or '?'}"
    return _unprefixed(short_key(key), scope)


def _unprefixed(label: str, scope: Scope | None) -> str:
    """``quarterback#209`` → ``#209``, and ``quarterback:2.40`` → ``2.40``.

    Only against the repo the pane is scoped to, and only when that is a single
    repo: trimming a name the header does not state would leave a bare ``#209``
    with nothing anywhere saying whose #209 it is.
    """
    if scope is None or scope.column or len(scope.names) != 1:
        return label
    name = next(iter(scope.names))
    lowered = label.lower()
    if lowered.startswith(f"{name}#"):
        return label[len(name):]                  # the '#' stays: it reads as an issue
    if lowered.startswith(f"{name}:"):
        return label[len(name) + 1:]              # the ':' does not: it read as a namespace
    return label


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

SEAT_FIELDS = ("pane", "seat", "session", "window", "command", "path", "repo", "scope")


def tmux_seats() -> list[dict]:
    """Every seat pane on this tmux server, lowest seat number first.

    A seat is a pane carrying the @qb_seat option, which is how qb-seats marks
    them and the only handle that survives a pane being added or closed — the
    index shifts and the agent rewrites the title.

    `@qb_repo` and `@qb_scope` come back with it because `list-panes -a` is the
    whole SERVER and not this screen: since #208 two screens can each have a seat
    1, so the number alone no longer says which board identity a pane is. Both are
    set on the SESSION (or, for `--add`, on the pane) and formats resolve a user
    option up through the hierarchy, so every pane of a screen answers for its own
    screen. Either can be empty — `@qb_scope` whenever the screen was not given an
    explicit one, `@qb_repo` on a screen built by a qb-seats old enough not to set
    it — which is why the dashboard falls back to matching on the number.

    Returns [] rather than raising when there is no tmux, no server, or no
    screen: the dashboard runs inside the screen most of the time and in a bare
    terminal the rest, and an empty SEATS panel is the honest answer to the
    second case.
    """
    if not os.environ.get("TMUX"):
        return []
    fmt = "\t".join("#{%s}" % f for f in
                    ("pane_id", "@qb_seat", "session_name", "window_index",
                     "pane_current_command", "pane_current_path", "@qb_repo",
                     "@qb_scope"))
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
    # pane order runs 1, 3, 2 on a screen that has had a seat added to it. By
    # SCREEN first, because this lists the whole server and two screens now
    # interleave their 1, 2, 3 otherwise — which reads as one screen with every
    # seat number twice.
    return sorted(seats, key=lambda s: (s["session"],
                                        int(s["seat"]) if s["seat"].isdigit() else 0))


# ---- claude code's own limits ------------------------------------------------
#
# The seats all bill to ONE subscription, so the ceiling every one of them is
# working towards is a single fleet-wide number — and it is the one fact this
# dashboard could not previously show. Six seats making a plan happen in
# parallel is exactly the way to spend a five-hour window in forty minutes, and
# the human at the screen finds out by watching an agent stop.
#
# The figures come from the same endpoint `/usage` reads, so what this shows and
# what a seat says about itself cannot disagree. It is the subscription's usage,
# not this session's: there is no per-session budget to report.
#
# NO TOKEN, NO LINE. An API-key install has no subscription limits to report and
# a missing credentials file is that case, not a failure — it returns nothing to
# show and no error, and the header simply has one line fewer.

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# THREE MINUTES, AND THAT IS ALREADY GENEROUS. The endpoint rate-limits harder
# than a dashboard's instincts suggest — five calls inside ten minutes was enough
# to get a 429 while this was being written. A five-hour window moves about a
# percent in three minutes, so nothing legible is being given up.
LIMITS_EVERY = 180.0    # seconds between calls, across every pane on the machine
BACKOFF = 600.0         # …and this long once the endpoint has said slow down
STALE_AFTER = 600.0     # past this the line admits its figures are old


def _oauth_token() -> str | None:
    """Claude Code's own OAuth access token, or None if this install has none.

    Read fresh every time rather than cached: Claude Code refreshes the token in
    place, and a dashboard that cached one at startup would start 401ing after
    an hour with nothing to say about why.
    """
    env = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env:
        return env
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    try:
        with open(os.path.join(home, ".credentials.json"), encoding="utf-8") as fh:
            return (json.load(fh).get("claudeAiOauth") or {}).get("accessToken") or None
    except Exception:                             # noqa: BLE001 — absent, or not ours to read
        return None


def _limit_label(row: dict) -> str:
    """What to call a limit in two or three columns.

    The kinds are the endpoint's, and the scoped one names its own model: a
    weekly cap that applies to Opus alone reads 'Opus', because 'weekly_scoped'
    is not a thing anybody is watching for.
    """
    kind = row.get("kind") or ""
    if kind == "session":
        return "5h"
    if kind == "weekly_all":
        return "7d"
    if kind == "weekly_scoped":
        scope = (row.get("scope") or {}).get("model") or {}
        return scope.get("display_name") or "7d*"
    return kind[:6] or "?"


# THE CACHE IS ON DISK BECAUSE THE POLLERS ARE SEPARATE PROCESSES. A developer
# running three seat screens has three dash panes, each its own process, each
# with its own timer — and the endpoint rate-limits, which is not a guess: it
# answered 429 while this was being built. A per-process interval cannot fix
# that, so the interval is enforced where they can all see it. One file, last
# answer plus the time the next call is allowed.

def _cache_path() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "quarterback", "limits.json")


def _read_cache() -> dict:
    try:
        with open(_cache_path(), encoding="utf-8") as fh:
            got = json.load(fh)
        return got if isinstance(got, dict) else {}
    except Exception:                             # noqa: BLE001 — no cache is a cold start
        return {}


def _write_cache(cache: dict) -> None:
    """Atomically, because the other dash panes are reading it as this one writes."""
    path = _cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, path)
    except Exception:                             # noqa: BLE001 — a cache is an optimisation
        pass


def _get_usage(token: str) -> tuple[list[dict], str | None]:
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
    })
    try:
        with urllib.request.urlopen(req, timeout=15, context=_ssl_context()) as resp:
            return parse_limits(json.loads(resp.read().decode())), None
    except urllib.error.HTTPError as exc:
        return [], f"HTTP {exc.code}"             # the code, or 429 is invisible
    except Exception as exc:                      # noqa: BLE001
        return [], type(exc).__name__


def _cached(cache: dict, now: float, err: str | None = None) -> tuple[list[dict], str | None]:
    limits = cache.get("limits") or []
    if now - float(cache.get("at") or 0) > STALE_AFTER:
        return limits, err or "stale"
    return limits, (None if limits else err)


def fetch_limits() -> tuple[list[dict], str | None]:
    """[{label, percent, resets, severity}, …] — the caps the whole fleet shares.

    Never raises, and never blanks a good answer. Returns ([], None) when there
    is nothing to report at all (no token, so no subscription caps), and the
    LAST figures with an error beside them when a call fails — they are minutes
    old and still roughly true, which is more use than an empty line.
    """
    token = _oauth_token()
    if not token:
        return [], None
    cache = _read_cache()
    now = time.time()
    if now < float(cache.get("next") or 0):
        return _cached(cache, now)                # somebody else asked recently enough
    limits, err = _get_usage(token)
    if err is None:
        _write_cache({"at": now, "limits": limits, "next": now + LIMITS_EVERY})
        return limits, None
    # A 429 means back further off, not try again in a minute: the failing call
    # is itself the thing being rate limited, and three panes retrying on the
    # short clock is how a warning becomes a wall.
    cache["next"] = now + (BACKOFF if err == "HTTP 429" else LIMITS_EVERY)
    _write_cache(cache)
    return _cached(cache, now, err)


def parse_limits(data: dict) -> list[dict]:
    """The endpoint's answer, reduced to what fits on one line.

    Split out from the fetch so the shaping is testable without a network, and
    because the endpoint carries a dozen fields per cap of which this shows
    three.
    """
    out = []
    for row in data.get("limits") or []:
        percent = row.get("percent")
        if percent is None:
            continue
        percent = int(round(percent))
        # A scoped cap nobody has touched is noise; the two headline caps stay
        # even at zero, because "0%" at the start of a window is information.
        if percent <= 0 and row.get("kind") not in ("session", "weekly_all"):
            continue
        out.append({
            "label": _limit_label(row),
            "percent": percent,
            "resets": row.get("resets_at"),
            "severity": row.get("severity") or "normal",
        })
    return out


def limit_reset(stamp: str | None) -> str:
    """'44m', '3h58m', '5d' — when a cap comes back, in five columns.

    until() is right for a lease measured in minutes and wrong here: a weekly
    window reads '128h48m', which is both too wide for the line and not how
    anybody thinks about next Monday.
    """
    if not stamp:
        return ""
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return ""
    secs = int((then - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "now"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 48 * 3600:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    return f"{secs // 86400}d{(secs % 86400) // 3600}h"


def limit_colour(percent: int, severity: str = "normal") -> str:
    """Green, yellow, red — and the endpoint's own severity can only escalate it.

    Thresholds here rather than trusting severity alone: severity says 'normal'
    at 62% of a window that six seats will finish inside the hour, and the point
    of the line is to see that coming.
    """
    if severity in ("critical", "exceeded", "blocked") or percent >= 90:
        return "red"
    if severity == "warning" or percent >= 70:
        return "yellow"
    return "green"


def limit_bar(percent: int, width: int) -> str:
    """A bar `width` cells wide. Full blocks for what is spent, light for the rest."""
    width = max(1, width)
    filled = max(0, min(width, int(round(percent / 100 * width))))
    # Anything spent shows at least one cell: a bar that reads empty at 3% and
    # empty at 0% has thrown away the only distinction that matters early on.
    if percent > 0 and filled == 0:
        filled = 1
    return "█" * filled + "░" * (width - filled)


def limit_cells(limits: list[dict], width: int) -> list[tuple[str, str, str, str, str]]:
    """[(label, bar, '62%', '3h50m', colour), …], sized to fit `width` columns.

    The layout lives here so the two dashboards cannot drift on it, and the
    styling does not, so each stays in charge of how it draws a colour.
    """
    if not limits or width < 20:
        return []
    gap = 2                                       # between segments
    # label + space + … + space + '100%' + space + reset, per segment.
    fixed = sum(len(l["label"]) for l in limits) + len(limits) * (1 + 1 + 4 + 1 + 5) + \
        gap * (len(limits) - 1)
    bar = (width - fixed) // len(limits)
    if bar < 4:                                   # too narrow for bars: drop them
        bar = 0
    bar = min(bar, 16)
    return [(l["label"], limit_bar(l["percent"], bar) if bar else "",
             f"{l['percent']}%", limit_reset(l["resets"]),
             limit_colour(l["percent"], l["severity"])) for l in limits]
