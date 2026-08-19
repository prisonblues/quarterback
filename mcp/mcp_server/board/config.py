"""Board configuration for the TUI client, per the ``qb-env`` contract.

The per-host contract is documented in ``qb-env`` (which lives beside the ``qb``
CLI, in another repo) and is READ here rather than sourced from there — the same
choice ``harness/bin/worktree-holder`` makes, and for the same reason: the client
has to work on a host that installed the board tooling without installing ``qb``.

Three rules are inherited verbatim and are the whole point of not re-deriving them:

* **There is deliberately no default base URL.** An unset variable means "this
  machine has not been told which board it belongs to", and guessing points the
  client at another island's board — ``qb.fo.ls`` resolves on public DNS, so the
  wrong guess reaches a real board rather than failing.
* **Environment beats the config file**, token command included, so a single
  invocation can override any of it without editing anything.
* **The agent name is resolved before the token command runs**, and exported into
  it. ``QUARTERBACK_TOKEN_CMD`` is permitted to reference ``$QUARTERBACK_AGENT``
  — the fleet's own generated config uses ``sed -n s/^$QUARTERBACK_AGENT://p`` to
  pick this host's line out of a shared token file — so a client that resolves the
  name afterwards expands it to empty and reads no token from a file that was
  present and valid the whole time (#201). ``qb-env`` sets it first; so does this.
  Its precedence is environment, then the config file, then ``hostname -s``: the
  file is in there because ``qb-env`` sources it (:43) *before* applying its own
  ``${QUARTERBACK_AGENT:-$(hostname -s)}`` default (:44), so a file that pins a name
  wins over the hostname on the shell side and has to win here too — two
  implementations disagreeing about which name this host is would be #201 again,
  pointed the other way.
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

_VARS = (
    "QUARTERBACK_BASE_URL",
    "QUARTERBACK_TOKEN",
    "QUARTERBACK_TOKEN_CMD",
    # Read from the file too, because ``qb-env`` reads it from there: it sources the
    # config (``qb-env:43``) and only *then* applies its own
    # ``QUARTERBACK_AGENT="${QUARTERBACK_AGENT:-$(hostname -s)}"`` default (:44), so a
    # file that pins a name is honoured there and the hostname is the fallback behind
    # it. A client that ignored the file's pin would resolve a different identity from
    # the shell on the same host, which is the split-identity failure #201 is about —
    # in the other direction and with the same cost.
    "QUARTERBACK_AGENT",
)

#: What ``errors="replace"`` leaves behind. A credential is not this character, so
#: finding it in one means the bytes the helper printed are not the bytes we have.
_UNDECODABLE = "\ufffd"

#: Named because the timeout is quoted in the diagnostic when it is hit, and a
#: number that appears in a message the operator acts on should have one place.
_TOKEN_CMD_TIMEOUT = 15


class NoBoardConfigured(RuntimeError):
    """Raised when no base URL is set — the one thing that must never be guessed."""


@dataclass(frozen=True)
class BoardConfig:
    """Resolved site configuration: which board, as whom, with what credential."""

    base_url: str
    token: str | None
    agent: str
    config_path: Path
    #: Why there is no token, when a credential source was present and did not yield
    #: one. ``None`` both when a token was resolved and when nothing was configured to
    #: resolve it — those are the two cases where there is nothing to explain, and the
    #: caller's message for the second one is already right.
    token_problem: str | None = None
    #: Whether a ``QUARTERBACK_TOKEN_CMD`` was configured at all, from either source.
    #: The remedy for a ``token_problem`` depends on it and cannot be inferred from
    #: the string: "re-run your command" is the wrong advice for a host whose *legacy
    #: token file* was the source that failed, and "set QUARTERBACK_TOKEN_CMD" is the
    #: wrong advice for a host that already has one. Printing the wrong remedy
    #: confidently is the #201 failure, so the caller gets the fact rather than a guess.
    token_cmd_configured: bool = False

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


def _read_config_file(path: Path, env: dict[str, str], agent: str) -> dict[str, str]:
    """The variables in :data:`_VARS` that the config file sets, or {} if unreadable.

    Sourced through bash rather than parsed, because the file is a bash fragment
    by contract and every other consumer sources it — a parser here would drift
    from them the first time a site writes ``QUARTERBACK_TOKEN_CMD="cat $HOME/.tok"``.
    Those names are unset in the child's environment first, so what comes back is what
    the FILE said and precedence stays this module's decision.

    ``QUARTERBACK_AGENT`` is the one exception, and is *seeded* with ``agent`` — the
    caller's already-resolved default — before the source rather than unset, for two
    reasons. The file's own values are allowed to reference it, and a double-quoted
    ``QUARTERBACK_TOKEN_CMD="… $QUARTERBACK_AGENT …"`` expands at source time, which is
    an earlier moment than the command running. And reading it back afterwards then
    yields the file's pin if it made one and the seeded default if it did not — which
    is exactly the second-priority value :func:`resolve` wants, with no need to tell
    the two apart.
    """
    if not path.is_file() or not os.access(path, os.R_OK):
        return {}
    bash = shutil.which("bash")
    if bash is None:  # no bash to source with: env-only is the honest answer
        return {}
    # Built from _VARS rather than written out, so the format string and the names can
    # never disagree about how many NUL-separated fields come back — the drift a fourth
    # variable would otherwise have introduced silently.
    fields = " ".join('"${' + name + ':-}"' for name in _VARS)
    script = (
        # Seeded and exported BEFORE the source, per the docstring.
        'QUARTERBACK_AGENT="$2"; export QUARTERBACK_AGENT; '
        '. "$1" >/dev/null 2>&1 || true; '
        'printf "' + "%s\\0" * len(_VARS) + '" ' + fields
    )
    child_env = {k: v for k, v in env.items() if k not in _VARS}
    try:
        out = subprocess.run(
            [bash, "-c", script, "_", str(path), agent],
            capture_output=True,
            text=True,
            # A config file is site-authored text and can carry a byte that is not
            # valid in this process's encoding; `text=True` decodes, and the
            # UnicodeDecodeError that would raise from is a ValueError, which the arm
            # below does not catch. Same reasoning as `_run_token_cmd`.
            errors="replace",
            timeout=10,
            env=child_env,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    parts = out.split("\0")
    if len(parts) < len(_VARS):
        return {}
    return {name: parts[i] for i, name in enumerate(_VARS) if parts[i]}


def _run_token_cmd(cmd: str, env: dict[str, str]) -> tuple[str | None, str | None]:
    """First line of ``QUARTERBACK_TOKEN_CMD``'s output, and why there is not one.

    Kept cheap by contract — it runs on every call — so the timeout is short and no
    failure raises: an unresolvable token is reported once, by the caller, not as a
    stack trace per attempt.

    Quiet is not the same as nameless, though, and this used to be both. Every way of
    not producing a token came back as a bare ``None`` and was reported to the
    operator as "this machine has no token" — which sent people to add a token
    command to a config file that already had a working one (#201). Each way now says
    which one it was, because they are remedied in different places:

    * **still running when the timeout expired** — the site's command is slow or
      hanging: a network fetch, or an interactive prompt with nobody at the terminal;
    * **could not be run at all** — a host problem, not a config one;
    * **exited nonzero** — the helper itself said no;
    * **succeeded and printed nothing** — the #201 shape: a selector that matched no
      line, or an agent name the token store does not know;
    * **printed bytes that are not text here** — a helper writing a binary blob, or a
      locale that cannot decode what it wrote.

    Returns ``(None, why)`` in each of those and ``(token, None)`` on success, so the
    second element's ``None`` is already :attr:`BoardConfig.token_problem`'s "nothing
    to explain" and the caller does not re-normalise it. Listed rather than counted:
    the count was "three" for a while after the fourth case was split out.

    **Nothing here interpolates ``cmd`` or the command's own output**, and the
    command's *stderr* is deliberately dropped rather than quoted. That is a real
    loss — ``op read``, ``vault kv get`` and ``pass show`` all explain themselves
    there — and it is still the trade this takes: the string below is printed to a
    terminal and pasted into issues, a helper running under ``set -x`` traces on
    stderr the command it was handed, and more than one of them echoes the secret it
    just fetched when something after the fetch fails. The operator gets the helper's
    own words by re-running it, which is what ``qb-board``'s diagnostic now tells them
    to do. ``strerror`` is the one exception and is included: it is the kernel's
    phrase for the failure and has none of the command in it.
    """
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            # `errors`, because `text=True` decodes the captured output under the
            # process locale: a helper that emits one byte invalid for it raised
            # UnicodeDecodeError from inside this very call. That is a ValueError, so
            # neither arm below caught it — it escaped `resolve()` and crashed the
            # client with a traceback, which is precisely the unexplained failure this
            # function exists to replace.
            errors="replace",
            timeout=_TOKEN_CMD_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        # Not `str(e)`: TimeoutExpired renders the command it ran, and this string is
        # printed. A `QUARTERBACK_TOKEN_CMD` of `echo <literal>` is a real site shape.
        return None, f"the token command was still running after {_TOKEN_CMD_TIMEOUT}s"
    except (OSError, subprocess.SubprocessError) as e:
        # The class name alone is "OSError" or "PermissionError" — true and useless.
        # `strerror` is what makes it actionable ("No such file or directory",
        # "Permission denied") and, unlike `str(e)`, carries no command text. Absent on
        # the SubprocessError half of this arm, hence the getattr rather than `e.`.
        detail = getattr(e, "strerror", None)
        named = f"{e.__class__.__name__}: {detail}" if detail else e.__class__.__name__
        return None, f"the token command could not be run ({named})"
    # Exit status, not just output. `op read …` and friends print partial or stale
    # material and *then* fail, and a client that took the first line anyway would
    # authenticate with a credential the provider had just disowned — arriving as a
    # 401 loop against the board rather than as the "no token" this reports.
    if proc.returncode != 0:
        return None, f"the token command exited {proc.returncode}"
    token = proc.stdout.split("\n", 1)[0].strip()
    if not token:
        # The #201 shape exactly: a command that ran, succeeded, and matched nothing.
        return None, "the token command succeeded but produced no output"
    if _UNDECODABLE in token:
        # `errors="replace"` above turned undecodable bytes into U+FFFD, so this is not
        # the string the helper printed. Sending it would authenticate with a corrupted
        # bearer and arrive as the same 401 loop the exit-status check avoids.
        return None, "the token command produced output that is not decodable text"
    return token, None


def _hostname() -> str:
    """This machine's short name — the agent-name default, as ``qb-env:44`` has it.

    A pure library call, no subprocess, which is what makes it safe for
    :func:`resolve` to run at the top ahead of the ``NoBoardConfigured`` check: it
    costs one syscall and cannot preempt that error with a less explanatory one. It
    answers "unknown" rather than raising for the two ways it can still come back
    useless — an empty name, and the OSError a host with no resolvable name can raise
    — because an agent name is a label, and failing to compute a label must not stop a
    client that has both a board and a token from reading the board.
    """
    try:
        name = socket.gethostname()
    except OSError:
        return "unknown"
    return name.split(".", 1)[0] or "unknown"


def resolve(env: dict[str, str] | None = None) -> BoardConfig:
    """Resolve the board URL, this machine's token, and its name.

    Raises :class:`NoBoardConfigured` when no base URL is set anywhere, or when
    what is set is not one.
    """
    env = dict(os.environ if env is None else env)

    # FIRST — before the config file is read and long before the token command runs.
    # The contract permits `QUARTERBACK_TOKEN_CMD` to reference `$QUARTERBACK_AGENT`,
    # and the fleet's generated config does; resolved seven lines *below* the command
    # that needs it — where it used to be — the variable expanded to empty, the site's
    # `sed -n s/^$QUARTERBACK_AGENT://p` became `s/^://p`, and every Python client on
    # the host reported "no token" against a valid token file (#201).
    #
    # Two passes, because the config file is both a consumer of the name and a possible
    # source of it. This pass is environment-or-hostname, which is what the file has to
    # be READ with, since its own values may reference the variable and a double-quoted
    # one expands at source time.
    from_env_agent = env.get("QUARTERBACK_AGENT")
    agent = from_env_agent or _hostname()

    path = config_path(env)
    from_file = _read_config_file(path, env, agent)

    # And this pass settles it: environment, then the file, then the hostname — the
    # precedence `qb-env` gets for free by sourcing the config (:43) before applying
    # its own default (:44). `_read_config_file` hands back the value it was seeded
    # with when the file pins nothing, so on the fleet's own hosts — whose generated
    # config names no agent — this line changes nothing; where a file does name the
    # host, the shell and this client now agree on which name that is rather than
    # resolving two different identities for one machine.
    #
    # The export is here rather than above because here is where it is read: `env` is
    # what every child this function starts inherits, the token command included.
    agent = from_env_agent or from_file.get("QUARTERBACK_AGENT") or agent
    env["QUARTERBACK_AGENT"] = agent

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
    token_problem: str | None = None
    cmd = env.get("QUARTERBACK_TOKEN_CMD") or from_file.get("QUARTERBACK_TOKEN_CMD")
    if not token and cmd:
        token, token_problem = _run_token_cmd(cmd, env)
    if not token and LEGACY_TOKEN_FILE.is_file():
        # Named when it does not yield, like every other source that was present. Left
        # silent — as it was — this is the #201 misdiagnosis in a second place: a
        # credential source that existed and could not be read, reported by the caller
        # as one that was never configured, sending the operator to set what is set.
        # The command's own problem wins where there is one; that is the source the
        # site chose, and this file is the fallback behind it.
        try:
            # `errors`, for the same reason as `_run_token_cmd`: `read_text` decodes,
            # and the UnicodeDecodeError it can raise is a ValueError that `except
            # OSError` would not have caught.
            legacy = LEGACY_TOKEN_FILE.read_text(errors="replace").strip()
        except OSError as e:
            detail = getattr(e, "strerror", None) or e.__class__.__name__
            token_problem = token_problem or f"{LEGACY_TOKEN_FILE} could not be read ({detail})"
        else:
            if not legacy:
                token_problem = token_problem or f"{LEGACY_TOKEN_FILE} is empty"
            elif _UNDECODABLE in legacy:
                token_problem = token_problem or (
                    f"{LEGACY_TOKEN_FILE} does not contain decodable text"
                )
            else:
                token = legacy
    # A token found after a failed command is still a token, and the failure is then
    # history rather than a diagnosis: reporting it would describe a client that is
    # authenticated as one that is not.
    if token:
        token_problem = None

    return BoardConfig(
        base_url=base_url.rstrip("/"),
        # Not `token or None`: every assignment above already yields None or a
        # non-empty string — the two `or` chains collapse an empty environment value,
        # `_run_token_cmd` returns None rather than "", and the legacy read is only
        # assigned when it is non-empty. Stating the invariant beats re-asserting it.
        token=token,
        agent=agent,
        config_path=path,
        token_problem=token_problem,
        token_cmd_configured=bool(cmd),
    )
