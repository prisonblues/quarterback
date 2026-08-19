"""Board configuration for the TUI client, per the ``qb-env`` contract.

The per-host contract is documented in ``qb-env`` (which lives beside the ``qb``
CLI, in another repo) and is READ here rather than sourced from there — the same
choice ``harness/bin/worktree-holder`` makes, and for the same reason: the client
has to work on a host that installed the board tooling without installing ``qb``.

Two rules are inherited verbatim and are the whole point of not re-deriving them:

* **There is deliberately no default base URL.** An unset variable means "this
  machine has not been told which board it belongs to", and guessing points the
  client at another island's board — ``qb.fo.ls`` resolves on public DNS, so the
  wrong guess reaches a real board rather than failing.
* **Environment beats the config file**, token command included, so a single
  invocation can override any of it without editing anything.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

#: The pre-config fleet layout, kept so a host that has not been rebuilt still works.
LEGACY_TOKEN_FILE = Path("/run/op-secrets/quarterback-token")

_VARS = ("QUARTERBACK_BASE_URL", "QUARTERBACK_TOKEN", "QUARTERBACK_TOKEN_CMD")


class NoBoardConfigured(RuntimeError):
    """Raised when no base URL is set — the one thing that must never be guessed."""


@dataclass(frozen=True)
class BoardConfig:
    """Resolved site configuration: which board, as whom, with what credential."""

    base_url: str
    token: str | None
    agent: str
    config_path: Path

    @property
    def authenticated(self) -> bool:
        return bool(self.token)


def config_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    explicit = env.get("QUARTERBACK_CONFIG")
    if explicit:
        return Path(explicit)
    # `expanduser`, not a literal "~": Path does not expand tildes, so a HOME-less
    # env (a systemd unit, a bare `env -i`) would have produced a relative `./~`
    # directory under whatever the cwd happened to be and read the config from
    # nowhere. expanduser falls back to the passwd entry, which is the real answer.
    home = env.get("HOME") or os.path.expanduser("~")
    base = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return Path(base) / "quarterback" / "config"


def _read_config_file(path: Path, env: dict[str, str]) -> dict[str, str]:
    """The three variables the config file sets, or {} if there is no readable file.

    Sourced through bash rather than parsed, because the file is a bash fragment
    by contract and every other consumer sources it — a parser here would drift
    from them the first time a site writes ``QUARTERBACK_TOKEN_CMD="cat $HOME/.tok"``.
    The three names are unset in the child's environment first, so what comes back
    is what the FILE said and precedence stays this module's decision.
    """
    if not path.is_file() or not os.access(path, os.R_OK):
        return {}
    bash = shutil.which("bash")
    if bash is None:  # no bash to source with: env-only is the honest answer
        return {}
    script = (
        '. "$1" >/dev/null 2>&1 || true; '
        'printf "%s\\0%s\\0%s\\0" '
        '"${QUARTERBACK_BASE_URL:-}" "${QUARTERBACK_TOKEN:-}" "${QUARTERBACK_TOKEN_CMD:-}"'
    )
    child_env = {k: v for k, v in env.items() if k not in _VARS}
    try:
        out = subprocess.run(
            [bash, "-c", script, "_", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            env=child_env,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    parts = out.split("\0")
    if len(parts) < len(_VARS):
        return {}
    return {name: parts[i] for i, name in enumerate(_VARS) if parts[i]}


def _run_token_cmd(cmd: str, env: dict[str, str]) -> str | None:
    """First line of ``QUARTERBACK_TOKEN_CMD``'s output, or None unless it succeeded.

    Kept cheap by contract — it runs on every call — so the timeout is short and a
    failure is silent: an unresolvable token is reported once, by the caller, as
    "no token", not as a stack trace per attempt.
    """
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=15, env=env
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # Exit status, not just output. `op read …` and friends print partial or stale
    # material and *then* fail, and a client that took the first line anyway would
    # authenticate with a credential the provider had just disowned — arriving as a
    # 401 loop against the board rather than as the "no token" this reports.
    if proc.returncode != 0:
        return None
    token = proc.stdout.split("\n", 1)[0].strip()
    return token or None


def _hostname() -> str:
    name = socket.gethostname()
    return name.split(".", 1)[0] or "unknown"


def resolve(env: dict[str, str] | None = None) -> BoardConfig:
    """Resolve the board URL, this machine's token, and its name.

    Raises :class:`NoBoardConfigured` when no base URL is set anywhere, or when
    what is set is not one.
    """
    env = dict(os.environ if env is None else env)
    path = config_path(env)
    from_file = _read_config_file(path, env)

    base_url = env.get("QUARTERBACK_BASE_URL") or from_file.get("QUARTERBACK_BASE_URL")
    if not base_url:
        raise NoBoardConfigured(
            "QUARTERBACK_BASE_URL is not set and there is no default.\n\n"
            "  This machine has not been told which quarterback board it belongs to.\n"
            "  There is deliberately no fallback: guessing would point this client at\n"
            f"  another island's board. Set it in {path} (rendered per-host by\n"
            "  home-manager), or pass QUARTERBACK_BASE_URL for this invocation."
        )

    # Checked here rather than left to the first request, because the tail's
    # reconnect loop cannot tell a typo from an outage: `board.example` with no
    # scheme raises the same transport error as a board that is down, so the client
    # would retry a misconfiguration forever and never say what was wrong with it.
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise NoBoardConfigured(
            f"QUARTERBACK_BASE_URL is set to {base_url!r}, which is not a board URL.\n\n"
            "  It needs an http:// or https:// scheme and a host — the client speaks\n"
            "  HTTP to the board, and anything else fails as a transport error that\n"
            "  the reconnecting tail reads as an outage and retries forever.\n"
            f"  Fix it in {path}, or pass QUARTERBACK_BASE_URL for this invocation."
        )

    token = env.get("QUARTERBACK_TOKEN") or from_file.get("QUARTERBACK_TOKEN")
    if not token:
        cmd = env.get("QUARTERBACK_TOKEN_CMD") or from_file.get("QUARTERBACK_TOKEN_CMD")
        if cmd:
            token = _run_token_cmd(cmd, env)
    if not token and LEGACY_TOKEN_FILE.is_file():
        try:
            token = LEGACY_TOKEN_FILE.read_text().strip() or None
        except OSError:
            token = None

    agent = env.get("QUARTERBACK_AGENT") or _hostname()
    return BoardConfig(
        base_url=base_url.rstrip("/"), token=token or None, agent=agent, config_path=path
    )
