"""Fleet data and the formatting both dashboards share.

One source of truth for "what is the board saying", so the snapshot renderer
(qb-dash) and the clickable one (qb-dash-tui) cannot drift on a fix.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

REPO = "prisonblues/quarterback"
REPO_URL = f"https://github.com/{REPO}"


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


def clip(s: str | None, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def seat_number(holder: str | None) -> int | None:
    """1 for 'zeus/seat-1'. None for anything that is not a seat."""
    if not holder or "/seat-" not in holder:
        return None
    tail = holder.split("/seat-", 1)[1]
    return int(tail) if tail.isdigit() else None


def board_client():
    from mcp_server.board.config import resolve
    from mcp_server.client import QuarterbackClient

    cfg = resolve()
    return QuarterbackClient(cfg.base_url, cfg.token or ""), cfg


def fetch_board(client) -> dict:
    """Everything the board can tell us. Never raises — a dead board is a state."""
    out: dict = {"agents": [], "subagents": [], "claims": [], "error": None}
    try:
        active = client.active({})
        out["agents"] = active.get("agents", [])
        out["subagents"] = active.get("subagents", [])
        out["claims"] = [
            c for c in client.claims({}).get("claims", [])
            if not c.get("released") and not c.get("lapsed")
        ]
    except Exception as exc:                      # noqa: BLE001 — display it, don't die
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def fetch_prs() -> tuple[list[dict], str | None]:
    fields = "number,title,isDraft,updatedAt,statusCheckRollup,headRefName"
    try:
        raw = subprocess.run(
            ["gh", "pr", "list", "--repo", REPO, "--state", "open",
             "--limit", "30", "--json", fields],
            capture_output=True, text=True, timeout=45,
        )
        if raw.returncode != 0:
            return [], clip(raw.stderr, 60) or f"gh exit {raw.returncode}"
        return json.loads(raw.stdout), None
    except Exception as exc:                      # noqa: BLE001
        return [], f"{type(exc).__name__}"


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
