"""Board configuration for the TUI client, close to the ``qb-env`` contract.

The per-host contract is documented in ``qb-env`` (which lives beside the ``qb``
CLI, in another repo) and is READ here rather than sourced from there — the same
choice ``harness/bin/worktree-holder`` makes, and for the same reason: the client
has to work on a host that installed the board tooling without installing ``qb``.

Three rules, and then one deliberate divergence that is spelled out rather than
papered over:

* **There is deliberately no default base URL.** An unset variable means "this
  machine has not been told which board it belongs to", and guessing points the
  client at another island's board — ``qb.fo.ls`` resolves on public DNS, so the
  wrong guess reaches a real board rather than failing.
* **Environment beats the config file** for ``QUARTERBACK_BASE_URL``,
  ``QUARTERBACK_TOKEN`` and ``QUARTERBACK_TOKEN_CMD``, so a single invocation can
  override any of it without editing anything. That is this module's own choice and
  not something read off ``qb-env`` — ``qb-env`` sources the file into the calling
  shell, where a plain assignment overwrites the environment — because a one-shot
  override has to work here: the fleet's config is generated and stamped "do not
  edit", so "export it for this command" is the only override an operator has.
* **The agent name is resolved before the token command runs**, and exported into
  it. ``QUARTERBACK_TOKEN_CMD`` is permitted to reference ``$QUARTERBACK_AGENT``
  — the fleet's own generated config uses ``sed -n s/^$QUARTERBACK_AGENT://p`` to
  pick this host's line out of a shared token file — so a client that resolves the
  name afterwards leaves the variable unset in the command's environment, the
  selector expands to ``s/^://p``, and it reads no token from a file that was
  present and valid the whole time (#201). ``qb-env`` sets it first; so does this,
  and that ordering is what #201 actually was.

**The divergence: this client does not honour a config-file agent pin, and
``qb-env`` does.** ``qb_load_config`` sources the config into the current shell and
only *then* applies ``QUARTERBACK_AGENT="${QUARTERBACK_AGENT:-$(hostname -s)}"``, so
on the shell side a plain ``QUARTERBACK_AGENT=daedalus`` in the file overwrites
whatever the environment said, and the idiomatic conditional form
``QUARTERBACK_AGENT="${QUARTERBACK_AGENT:-daedalus}"`` fires there too. Here,
:func:`resolve` takes the name from the environment, else ``hostname -s``, and reads
no agent out of the file at all.

That is a real difference in behaviour and it is the trade this takes on purpose:

* the fleet's generated config **names no agent**, so the pin is a shape the
  contract allows and no host actually writes;
* the ``QUARTERBACK_TOKEN_CMD`` it does carry is **single-quoted**, so nothing in it
  expands when the file is sourced and ``$QUARTERBACK_AGENT`` is expanded by the
  shell that runs the command — against the name resolved below, whatever that
  turns out to be, which is exactly the property #201 needed;
* and parity cost more than it bought. Honouring the pin means reproducing bash's
  interleaving of assignments and source-time expansions in Python, and the
  implementation that tried produced a **split identity**: with a plainly-pinned
  name and a *double*-quoted token command, one agent's credential was fetched
  while the client posted as another. Fetching daedalus's token and posting as
  atlas is a worse failure than declining to honour a pin nothing writes.

One residue of bash's own semantics remains and cannot be removed from this side: a
file that both assigns ``QUARTERBACK_AGENT`` itself *and* double-quotes a
``QUARTERBACK_TOKEN_CMD`` referencing it has that reference expanded, at source
time, against the file's own assignment — while :attr:`BoardConfig.agent` reports
the environment's name or the hostname. That combination is the only way the two can
disagree, and it is the reason the documented form single-quotes the command:
deferred expansion always sees the resolved name.
"""

from __future__ import annotations

import locale
import os
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

#: The pre-config fleet layout, kept so a host that has not been rebuilt still works.
LEGACY_TOKEN_FILE = Path("/run/op-secrets/quarterback-token")

#: What the config file is read for. ``QUARTERBACK_AGENT`` is deliberately absent —
#: see the module docstring: this client resolves the agent name from the environment
#: or the hostname and never from the file, so reading it back here would only make
#: it look as though the pin were honoured.
_VARS = ("QUARTERBACK_BASE_URL", "QUARTERBACK_TOKEN", "QUARTERBACK_TOKEN_CMD")

#: Named because the timeout is quoted in the diagnostic when it is hit, and a
#: number that appears in a message the operator acts on should have one place.
_TOKEN_CMD_TIMEOUT = 15


class NoBoardConfigured(RuntimeError):
    """Raised when no base URL is set — the one thing that must never be guessed."""


class _FileVars(NamedTuple):
    """What the config file set, split by whether this process can read it."""

    #: Name → value, for every variable the file set to something non-empty that
    #: decoded.
    values: dict[str, str]
    #: Names the file set to bytes that are **not** text under this process's
    #: encoding. Kept apart from :attr:`values` rather than dropped, because "the
    #: file sets no token" and "the file sets a token this process cannot read" are
    #: different facts with different remedies — and folding the second into the
    #: first is the #201 misdiagnosis exactly: a credential source that was present
    #: and did not yield, reported as one that was never configured.
    undecodable: frozenset[str]


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


def _stripped(value: str | None) -> str | None:
    """``value`` without surrounding whitespace, or None if that leaves nothing.

    So that all three token sources agree on what counts as a token. A command's
    output has always been stripped, so ``QUARTERBACK_TOKEN_CMD='echo "  "'`` came
    back as "produced no output"; the identical value written straight into
    ``QUARTERBACK_TOKEN`` used to pass through untouched, truthy, and got sent as a
    bearer of three spaces — a 401 with nothing to explain it.
    """
    if value is None:
        return None
    return value.strip() or None


def _decode(raw: bytes) -> str | None:
    """``raw`` as text, or ``None`` when it is not text under this process's encoding.

    A strict decode kept separate from the read that produced the bytes, because
    "is this decodable" has to be answerable exactly. It was ``errors="replace"``
    followed by a search for U+FFFD in the result, which is a guess in both
    directions: a credential that genuinely contains U+FFFD was rejected as
    undecodable, and the check only worked at all for callers that remembered to
    make it. Keeping the bytes and letting ``UnicodeDecodeError`` be the answer is
    both exact and impossible to forget.

    ``locale.getencoding()`` is what ``subprocess``'s own ``text=True`` decoded
    with, so this reads exactly what that read — including under Python's UTF-8
    mode, which that function honours.
    """
    try:
        return raw.decode(locale.getencoding())
    except UnicodeDecodeError:
        return None


def _first_line(raw: bytes) -> bytes:
    """The bytes up to the first newline.

    Split on the bytes rather than after decoding, so only the line that becomes
    the credential has to be decodable: a helper that prints a good token and then
    a byte this locale cannot read has still printed a good token. Safe for every
    encoding ``locale.getencoding()`` can return on a POSIX host, none of which put
    a bare 0x0A inside a multi-byte sequence.
    """
    return raw.split(b"\n", 1)[0]


def _read_config_file(path: Path, env: dict[str, str]) -> _FileVars:
    """The variables in :data:`_VARS` that the config file sets.

    Sourced through bash rather than parsed, because the file is a bash fragment
    by contract and every other consumer sources it — a parser here would drift
    from them the first time a site writes ``QUARTERBACK_TOKEN_CMD="cat $HOME/.tok"``.
    Those names are unset in the child's environment first, so what comes back is what
    the FILE said and precedence stays this module's decision.

    ``QUARTERBACK_AGENT`` is neither unset nor read back. It is left in the child's
    environment because :func:`resolve` has already put the resolved name there and a
    double-quoted value in the file may reference it, and it is absent from
    :data:`_VARS` because a name the file pins is deliberately not honoured — see the
    module docstring.

    The output is captured as **bytes**. A config file is site-authored text and can
    carry a byte that is not valid in this process's encoding; ``text=True`` would
    decode it, and the ``UnicodeDecodeError`` that raises from is a ``ValueError``,
    which the arm below does not catch. Decoding per field afterwards also keeps one
    bad byte in ``QUARTERBACK_TOKEN`` from costing the base URL, and — unlike the
    ``errors="replace"`` this used to do — leaves a mangled value out of
    :attr:`_FileVars.values` instead of handing it back as a usable string.
    """
    empty = _FileVars({}, frozenset())
    if not path.is_file() or not os.access(path, os.R_OK):
        return empty
    bash = shutil.which("bash")
    if bash is None:  # no bash to source with: env-only is the honest answer
        return empty
    # Both halves built from _VARS rather than written out, so the names printed, the
    # count printed and the arity check below cannot disagree about how many
    # NUL-separated fields come back. The argument list is the half that matters:
    # `printf` reuses its format until the arguments run out, so a hand-written format
    # survives a fourth variable, while a hand-written argument list drops it in
    # silence — the drift this shape exists to make impossible.
    fields = " ".join('"${' + name + ':-}"' for name in _VARS)
    script = '. "$1" >/dev/null 2>&1 || true; printf "' + "%s\\0" * len(_VARS) + '" ' + fields
    child_env = {k: v for k, v in env.items() if k not in _VARS}
    try:
        out = subprocess.run(
            [bash, "-c", script, "_", str(path)],
            capture_output=True,
            timeout=10,
            env=child_env,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return empty
    parts = out.split(b"\0")
    if len(parts) < len(_VARS):
        return empty
    values: dict[str, str] = {}
    undecodable: set[str] = set()
    for i, name in enumerate(_VARS):
        if not parts[i]:
            continue
        text = _decode(parts[i])
        if text is None:
            undecodable.add(name)
        else:
            values[name] = text
    return _FileVars(values, frozenset(undecodable))


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
    * **printed bytes that are not text here** — a helper writing a binary blob, or a
      locale that cannot decode what it wrote;
    * **succeeded and printed nothing** — the #201 shape: a selector that matched no
      line, or an agent name the token store does not know.

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
            # Bytes, not `text=True`. Decoding is `_decode`'s job precisely because a
            # helper that emits one byte invalid for the process locale raised
            # UnicodeDecodeError from inside this very call — a ValueError, so neither
            # arm below caught it. It escaped `resolve()` and crashed the client with a
            # traceback, which is the unexplained failure this function replaces.
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
    line = _decode(_first_line(proc.stdout))
    if line is None:
        # Undecodable bytes are not the string the helper printed. Sending a mangled
        # one would authenticate with a corrupted bearer and arrive as the same 401
        # loop the exit-status check avoids.
        return None, "the token command produced output that is not decodable text"
    token = line.strip()
    if not token:
        # The #201 shape exactly: a command that ran, succeeded, and matched nothing.
        return None, "the token command succeeded but produced no output"
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


def _read_legacy_token(problem: str | None) -> tuple[str | None, str | None]:
    """The pre-config fleet token file's first line, and why there is not one.

    ``problem`` is the diagnosis already in hand, if any; the earlier — higher
    precedence — source keeps it, and this only fills a ``None``. That is the same
    rule the token sources above follow: the source the site chose is the one worth
    naming, and this file is the fallback behind it.

    The read is **not** behind an ``is_file()`` guard, which is where it used to be
    and which follows symlinks: a dangling symlink is ``False`` to it, so the one
    credential source that is visibly present and cannot possibly yield took no
    branch at all and resolved ``token=None, token_problem=None`` — the silent
    misdiagnosis this whole path exists to end. Attempting the read is the test, and
    ``FileNotFoundError`` is the "there is nothing here" answer — except that a
    dangling symlink raises it too, hence the one ``is_symlink`` call, which does not
    follow and so is the only thing that can tell those two hosts apart.

    Only the first line, like :func:`_run_token_cmd`, and for a harder reason than
    tidiness: a file with a trailing comment or two tokens in it produced a token
    containing a newline, which is not a legal HTTP header value, so httpx raised a
    traceback out of the first request instead of any of the failures above being
    named.
    """
    try:
        raw = LEGACY_TOKEN_FILE.read_bytes()
    except FileNotFoundError:
        if LEGACY_TOKEN_FILE.is_symlink():
            return None, problem or f"{LEGACY_TOKEN_FILE} is a symlink to nothing"
        return None, problem  # no legacy file: nothing was present, nothing to explain
    except OSError as e:
        # `strerror` for the same reason as in `_run_token_cmd`: the class name alone
        # is true and useless, and this phrase carries no secret.
        detail = getattr(e, "strerror", None) or e.__class__.__name__
        return None, problem or f"{LEGACY_TOKEN_FILE} could not be read ({detail})"
    line = _decode(_first_line(raw))
    if line is None:
        return None, problem or f"{LEGACY_TOKEN_FILE} does not contain decodable text"
    token = line.strip()
    if not token:
        return None, problem or f"{LEGACY_TOKEN_FILE} is empty"
    return token, problem


def resolve(env: dict[str, str] | None = None) -> BoardConfig:
    """Resolve the board URL, this machine's token, and its name.

    Raises :class:`NoBoardConfigured` when no base URL is set anywhere, or when
    what is set is not one.
    """
    env = dict(os.environ if env is None else env)

    # FIRST, and exported into every child this function starts. The contract permits
    # `QUARTERBACK_TOKEN_CMD` to reference `$QUARTERBACK_AGENT`, and the fleet's
    # generated config does; resolved seven lines *below* the command that needs it —
    # where it used to be — the variable was simply unset in the child's environment,
    # the site's `sed -n s/^$QUARTERBACK_AGENT://p` became `s/^://p`, and every Python
    # client on the host reported "no token" against a valid token file (#201).
    #
    # Environment, else this machine's short hostname. Deliberately not the config
    # file, which `qb-env` would honour — the module docstring says why that
    # divergence is the trade this takes, and a test pins it so it stays a decision.
    #
    # `env` is what every child below inherits, the sourced config file included, so
    # one assignment here is the whole mechanism.
    agent = env.get("QUARTERBACK_AGENT") or _hostname()
    env["QUARTERBACK_AGENT"] = agent

    path = config_path(env)
    from_file = _read_config_file(path, env)

    base_url = env.get("QUARTERBACK_BASE_URL") or from_file.values.get("QUARTERBACK_BASE_URL")
    if not base_url and "QUARTERBACK_BASE_URL" in from_file.undecodable:
        # Named rather than treated as unset. "This machine has not been told which
        # board it belongs to" is false on a host whose file says which board it is,
        # in bytes this process cannot read, and sends the operator to set what is set.
        raise NoBoardConfigured(
            f"QUARTERBACK_BASE_URL is set in {path} to bytes that are not text here.\n\n"
            "  The file was read and that value could not be decoded under this\n"
            "  process's encoding, so what this client would use is not what the file\n"
            "  says — and a mangled URL reaches a host nobody configured. Fix the\n"
            "  file's encoding, or pass QUARTERBACK_BASE_URL for this invocation."
        )
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

    # Stripped on the way in, so an exported-but-blank value is "no token" here rather
    # than a bearer of spaces sent to the board — the same answer `_run_token_cmd`
    # gives for a command that prints them. Stripped per source, not after the `or`,
    # so a blank environment value falls through to the file exactly as an empty one
    # always has.
    token = _stripped(env.get("QUARTERBACK_TOKEN")) or _stripped(
        from_file.values.get("QUARTERBACK_TOKEN")
    )
    token_problem: str | None = None
    if not token and "QUARTERBACK_TOKEN" in from_file.undecodable:
        token_problem = f"{path} sets QUARTERBACK_TOKEN to something that is not decodable text"

    cmd = env.get("QUARTERBACK_TOKEN_CMD") or from_file.values.get("QUARTERBACK_TOKEN_CMD")
    cmd_unreadable = "QUARTERBACK_TOKEN_CMD" in from_file.undecodable
    if not cmd and cmd_unreadable:
        token_problem = token_problem or (
            f"{path} sets QUARTERBACK_TOKEN_CMD to something that is not decodable text"
        )
    if not token and cmd:
        token, cmd_problem = _run_token_cmd(cmd, env)
        # The higher-precedence source keeps the diagnosis, as everywhere else here.
        token_problem = token_problem or cmd_problem
    if not token:
        token, token_problem = _read_legacy_token(token_problem)
    # A token found after a failed command is still a token, and the failure is then
    # history rather than a diagnosis: reporting it would describe a client that is
    # authenticated as one that is not.
    if token:
        token_problem = None

    return BoardConfig(
        base_url=base_url.rstrip("/"),
        # Not `token or None`: every assignment above already yields None or a
        # non-empty, stripped string — `_stripped` collapses a blank environment or
        # file value, `_run_token_cmd` returns None rather than "", and
        # `_read_legacy_token` returns its first line only when it has one. Stating
        # the invariant beats re-asserting it.
        token=token,
        agent=agent,
        config_path=path,
        token_problem=token_problem,
        # A command the file sets in bytes this process cannot read is still a command
        # the site configured: "nothing configured a token command" is false on that
        # host, and the remedy that follows from it — set one — is the #201 mistake.
        token_cmd_configured=bool(cmd) or cmd_unreadable,
    )
