"""Fleet data and the formatting both dashboards share.

One source of truth for "what is the board saying", so the snapshot renderer
(qb-dash) and the clickable one (qb-dash-tui) cannot drift on a fix.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import ssl
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REPO = "prisonblues/quarterback"
REPO_URL = f"https://github.com/{REPO}"

# How much of each list is asked for. Every one of these is a CAP, not a page
# size — `gh` pages internally until it has `--limit` rows, and /claims takes a
# limit of its own — so the only thing a caller can get wrong is believing a
# capped list is the whole list. Each fetch below therefore says so out loud when
# it comes back full, because "showing the first N" and "there are N" differ by
# exactly the work somebody would otherwise pick up twice.
CLAIM_LIMIT = 1000                                # the /claims endpoint's maximum
ISSUE_LIMIT = 600                                 # open issues read for blocked-by edges
PR_LIMIT = 100                                    # open PRs listed


def truncated(rows: list, limit: int, what: str) -> str | None:
    """The 'this list is not all of it' message, or None when it is all of it."""
    if len(rows) < limit:
        return None
    return f"showing the first {limit} {what} — there are more, so this is a partial answer"


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


def short_key(key: str) -> str:
    """'prisonblues/quarterback:2.40' → 'quarterback:2.40'.

    The owner is the same for every repo the fleet touches, so it is 12 columns
    that never distinguish two rows.
    """
    return key.split("/", 1)[-1] if key.count("/") == 1 else key


#: Every C0 control character plus DEL, mapped to nothing — except the three
#: that are whitespace, which become a space. Dropping a newline outright would
#: JOIN the words either side of it, so "a\nb" would read as one word "ab".
_CONTROL = dict.fromkeys([*range(32), 127])
_CONTROL.update({0x09: " ", 0x0A: " ", 0x0D: " "})


def plain(value: object) -> str:
    """Display text with control characters gone and whitespace collapsed.

    A PR title, a claim holder and a `gh` error all arrive from somewhere else,
    and a terminal reads an ESC in any of them as an instruction rather than as
    text — so a crafted title could redraw a section header or hide the line
    below it. Collapsing whitespace already removed newlines and tabs; this
    removes the rest, in the one function every caller already goes through
    instead of at each of the thirty places that interpolate a remote string.
    """
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    return " ".join(text.translate(_CONTROL).split())


def clip(s: object, n: int) -> str:
    s = plain(s)
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

    def claims(self, limit: int = CLAIM_LIMIT) -> dict:
        # The endpoint's own default is 100 and its maximum is 1000. Asked for
        # explicitly because a truncated claims page is not a shorter list, it is
        # a claim the caller cannot see — and an unseen claim reads as free work.
        return self.get(f"/claims?{urllib.parse.urlencode({'limit': limit})}")

    def plan(self, repo: str | None = None) -> dict:
        # urlencode rather than a hand-built `?repo=` + quote: quote's default
        # `safe='/'` leaves the slash in `owner/repo` unescaped, which happens to
        # be right for this one value and would be wrong for the next one.
        query = "?" + urllib.parse.urlencode({"repo": repo}) if repo else ""
        return self.get(f"/plan{query}")


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
        rows = client.claims().get("claims", []) or []
        out["claims"] = [c for c in rows
                         if isinstance(c, dict) and not c.get("released") and not c.get("lapsed")]
        out["error"] = truncated(rows, CLAIM_LIMIT, "claims")
    except Exception as exc:                      # noqa: BLE001 — display it, don't die
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def fetch_plan(client, repo: str | None = REPO) -> dict:
    """The ordered plan. Never raises — a dead board is a state, as above.

    `next` comes from the board rather than being recomputed here: it is the one
    field the endpoint exists to answer, and a second implementation of "first
    open, unclaimed, unblocked" is a second thing to get wrong.

    The claims on the items are LIVE ones, and that is the endpoint's contract
    rather than this function's hope: `/plan` selects on `expires_at > now` and
    `/claims` filters expired-but-unswept rows on the way past, so a claim whose
    holder is gone reads as free without a reaper. A caller may still check the
    expiry it is handed — `qb-next` does — but it is checking a second time.

    `update` and not a field-by-field copy: a response missing a key keeps the
    default above, and a response carrying `error` overwrites this one's None,
    which is the shape a board that answered-but-refused sends.
    """
    out: dict = {"items": [], "next": None, "counts": {}, "error": None}
    try:
        got = client.plan(repo)
        if not isinstance(got, dict):
            return {**out, "error": f"/plan returned {type(got).__name__}, not an object"}
        out.update(got)
    except Exception as exc:                      # noqa: BLE001 — display it, don't die
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _blocker_nodes(issue: dict) -> list[dict]:
    """The `blockedBy` edges on one issue, whichever of the two shapes it is in.

    `gh issue list --json blockedBy` returns the GraphQL connection —
    `{"nodes": [...], "totalCount": n}` — and that is what this reads. It also
    accepts a bare list, because a field that is a connection today is a list in
    half of GitHub's other payloads and the cost of being wrong is not a
    TypeError: it is the bare `except` below turning one unexpected shape into
    "no blockers anywhere in this repo", which reads as everything unblocked.

    An explicit `"nodes": null` is why the `or` is on the INNER get as well: the
    key is present, so a default cannot fire, and `for b in None` raises.
    """
    field = issue.get("blockedBy")
    if isinstance(field, list):
        nodes = field
    elif isinstance(field, dict):
        nodes = field.get("nodes") or []
    else:
        nodes = []
    return [b for b in nodes if isinstance(b, dict)]


def fetch_blocked(repo: str = REPO) -> tuple[dict[int, list[int]], str | None]:
    """Issue number → the OPEN issues blocking it.

    Filtering on state matters and is the whole reason this is a function rather
    than a field read. GitHub keeps a `blocked-by` edge after the blocker closes,
    which is right — the dependency was real — but it means a bare read reports
    an issue as blocked by work that finished weeks ago. Two do so today.

    The state comparison is case-folded on purpose. GitHub sends `OPEN`, and a
    strict `== "OPEN"` that met anything else would not fail: it would match
    nothing and report every issue in the repo as unblocked, with no error and
    nothing on screen to suggest the answer was wrong. Every other failure here
    is loud; that one would not be, so it is spelled to survive.

    The error is the second half of the contract and never just the exception's
    class name: "github unavailable: TimeoutExpired" and "github unavailable:
    FileNotFoundError" both say nothing about which command timed out or which
    binary is missing. A truncated read is reported the same way, because a
    partial blocker map is a wrong answer in the dangerous direction — an issue
    off the end of the list has no edges here and so reads as free.
    """
    try:
        raw = subprocess.run(
            ["gh", "issue", "list", "--repo", repo, "--state", "open",
             "--limit", str(ISSUE_LIMIT), "--json", "number,blockedBy"],
            capture_output=True, text=True, timeout=45,
        )
        if raw.returncode != 0:
            return {}, clip(raw.stderr, 60) or f"gh exit {raw.returncode}"
        issues = json.loads(raw.stdout)
        if not isinstance(issues, list):
            return {}, f"gh returned {type(issues).__name__}, not a list of issues"
        blocked: dict[int, list[int]] = {}
        bad = 0
        for issue in issues:
            # Per-issue rather than per-fetch: one malformed record used to take
            # the whole repo's blocker data down with it, and "no blockers at
            # all" is the answer that gets somebody working on blocked work.
            try:
                open_blockers = sorted(b["number"] for b in _blocker_nodes(issue)
                                       if str(b.get("state", "")).upper() == "OPEN")
                if open_blockers:
                    blocked[issue["number"]] = open_blockers
            except (KeyError, TypeError, ValueError):
                bad += 1
        error = truncated(issues, ISSUE_LIMIT, "open issues")
        if bad:
            error = f"{bad} issue record(s) in a shape this cannot read" + (
                f"; {error}" if error else "")
        return blocked, error
    except Exception as exc:                      # noqa: BLE001
        return {}, f"{type(exc).__name__}: {exc}"


def fetch_prs(repo: str = REPO) -> tuple[list[dict], str | None]:
    """The open PRs, and whether that is all of them.

    A section headed "OPEN PRS (30)" on a repo with forty of them is not a short
    list, it is a wrong count — so the cap says so rather than being inferred by
    a reader who would have to know what the cap was.
    """
    fields = "number,title,isDraft,updatedAt,statusCheckRollup,headRefName"
    try:
        raw = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--state", "open",
             "--limit", str(PR_LIMIT), "--json", fields],
            capture_output=True, text=True, timeout=45,
        )
        if raw.returncode != 0:
            return [], clip(raw.stderr, 60) or f"gh exit {raw.returncode}"
        prs = json.loads(raw.stdout)
        if not isinstance(prs, list):
            return [], f"gh returned {type(prs).__name__}, not a list of PRs"
        return prs, truncated(prs, PR_LIMIT, "open PRs")
    except Exception as exc:                      # noqa: BLE001
        return [], f"{type(exc).__name__}: {exc}"


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
