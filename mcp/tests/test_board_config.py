"""The one rule that must never bend: an unset base URL is an error, not a guess."""

from __future__ import annotations

import os
import subprocess

import pytest
from mcp_server.board import config as boardcfg
from mcp_server.board.config import NoBoardConfigured, config_path, resolve


@pytest.fixture(autouse=True)
def no_legacy_token(tmp_path, monkeypatch):
    """Neutralise the pre-config fleet token file.

    It is a real file on the fleet's own machines, so a "no token here" test run
    on one of them would silently be handed a live bearer and pass for the wrong
    reason — or, worse, fail only on the developer's box.
    """
    monkeypatch.setattr(boardcfg, "LEGACY_TOKEN_FILE", tmp_path / "no-such-token")


def write_config(tmp_path, body: str):
    path = tmp_path / "config"
    path.write_text(body)
    return path


def env_for(tmp_path, **extra):
    """A realistic environment: PATH included, because a token command is a command.

    ``resolve`` runs QUARTERBACK_TOKEN_CMD through a shell with the environment it
    was handed, so an env without PATH would make every site whose command is
    `cat`/`op`/`ssh` fail for a reason that has nothing to do with the code.
    """
    return {"HOME": str(tmp_path), "PATH": os.environ["PATH"], **extra}


def test_unset_base_url_is_an_error_not_a_default(tmp_path):
    with pytest.raises(NoBoardConfigured) as e:
        resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(tmp_path / "nothing")))
    # The failure has to say why there is no fallback, because the obvious
    # "fix" is to add one — and qb.fo.ls answers on public DNS, so a guess
    # reaches someone else's real board rather than failing.
    assert "no default" in str(e.value)
    assert "another island" in str(e.value)


def test_config_file_supplies_url_and_token(tmp_path):
    path = write_config(
        tmp_path,
        'QUARTERBACK_BASE_URL="https://board.example/"\nQUARTERBACK_TOKEN=fromfile\n',
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.base_url == "https://board.example"  # trailing slash trimmed
    assert cfg.token == "fromfile"
    assert cfg.authenticated


def test_environment_beats_the_config_file(tmp_path):
    path = write_config(
        tmp_path, "QUARTERBACK_BASE_URL=https://file.example\nQUARTERBACK_TOKEN=fromfile\n"
    )
    cfg = resolve(
        env_for(
            tmp_path,
            QUARTERBACK_CONFIG=str(path),
            QUARTERBACK_BASE_URL="https://env.example",
            QUARTERBACK_TOKEN="fromenv",
        )
    )
    assert (cfg.base_url, cfg.token) == ("https://env.example", "fromenv")


def test_environment_token_cmd_beats_the_file_token_cmd(tmp_path):
    """A config file silently winning TOKEN_CMD means querying the board as somebody else."""
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        "QUARTERBACK_TOKEN_CMD='echo wrong-agent'\n",
    )
    cfg = resolve(
        env_for(tmp_path, QUARTERBACK_CONFIG=str(path), QUARTERBACK_TOKEN_CMD="echo right-agent")
    )
    assert cfg.token == "right-agent"


def test_token_cmd_takes_only_its_first_line(tmp_path):
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        "QUARTERBACK_TOKEN_CMD='printf \"tok\\nnoise\\n\"'\n",
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.token == "tok"


def test_a_failing_token_cmd_leaves_the_client_unauthenticated(tmp_path):
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        "QUARTERBACK_TOKEN_CMD='exit 7'\n",
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.token is None
    assert not cfg.authenticated  # the tokenless health path, not a crash


def test_a_token_cmd_that_prints_then_fails_yields_no_token(tmp_path):
    """Exit status decides, not the first line that happened to arrive.

    Secret providers do print partial or stale material before failing, and taking
    it anyway means querying the board with a credential the provider disowned —
    which arrives as a 401 loop rather than as the "not authenticated" it is.
    """
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        "QUARTERBACK_TOKEN_CMD='echo stale-token; exit 1'\n",
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.token is None and not cfg.authenticated


def test_a_base_url_that_is_not_a_url_fails_loudly_like_an_unset_one(tmp_path):
    """A typo must not read as an outage.

    Without a scheme or a host the first request raises a transport error, and the
    tail's reconnect loop cannot tell that from a board that is down — so it would
    retry the typo forever and never say what was wrong with it.
    """
    for bad in ("board.example", "not a url at all", "ftp://board.example", "https://"):
        with pytest.raises(NoBoardConfigured) as e:
            resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(tmp_path / "none"),
                            QUARTERBACK_BASE_URL=bad))
        assert "http" in str(e.value) and bad in str(e.value)


def test_config_file_may_use_shell_expansion(tmp_path):
    """Sourced, not parsed — sites do write `cat $HOME/.tok` in this file.

    The `$HOME` here is left unexpanded in the file on purpose: a parser would
    hand `cat $HOME/.tok` to the shell as a literal and read nothing.
    """
    (tmp_path / ".tok").write_text("expanded\n")
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        'QUARTERBACK_TOKEN_CMD="cat $HOME/.tok"\n',
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.token == "expanded"


def test_agent_defaults_to_the_hostname_and_is_overridable(tmp_path):
    env = env_for(tmp_path, QUARTERBACK_BASE_URL="https://b.example")
    assert resolve(env).agent  # some hostname, never empty
    assert resolve({**env, "QUARTERBACK_AGENT": "atlas"}).agent == "atlas"


# -- the agent name is resolved BEFORE the token command (#201) --------


def write_token_file(tmp_path, tokens: dict[str, str]):
    """The fleet's shared token file: one `agent:token` line per host.

    This layout is the whole reason the contract permits `$QUARTERBACK_AGENT` in a
    token command — one generated config, N hosts, each picking its own line.

    A mapping rather than `**kwargs`: every caller keys a line by the runner's real
    hostname, and a runner called `atlas` would then collide with a literal `atlas=`
    — losing a line silently, or raising TypeError for a duplicate keyword.
    """
    path = tmp_path / "api-tokens"
    path.write_text("".join(f"{a}:{t}\n" for a, t in tokens.items()))
    return path


def other_than(host: str) -> str:
    """An agent name that is certainly not this runner's, whatever it is called."""
    return f"not-{host}"


def test_the_token_command_can_reference_the_agent_name(tmp_path):
    """The #201 reproduction, verbatim: the fleet's own generated config.

    Resolved after the command instead of before it, `$QUARTERBACK_AGENT` expanded
    to empty, `sed -n s/^$QUARTERBACK_AGENT://p` became `s/^://p` and matched no
    line — so this client reported "no token" on a host whose token file was
    present and valid, and whose bash `qb` worked in the same shell in the same
    minute.
    """
    host = boardcfg._hostname()
    tokens = write_token_file(tmp_path, {host: "tok-thishost", other_than(host): "tok-other"})
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        f"QUARTERBACK_TOKEN_CMD='sed -n s/^$QUARTERBACK_AGENT://p {tokens}'\n",
    )
    # QUARTERBACK_AGENT deliberately NOT in the environment. That is the host this
    # was found on and the only case that was ever broken: a caller that exports the
    # name has always had it reach the child, which is why the bug presented as
    # intermittent — it depended on whether the invoking shell happened to carry it.
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.token == "tok-thishost"
    assert cfg.agent == host  # the same value, or the command picked another host's line
    assert cfg.token_problem is None


def test_the_agent_the_command_sees_is_the_agent_the_client_will_use(tmp_path):
    """Unset in the environment, the command must still see the hostname default.

    Not "some non-empty string": the identity the credential is looked up under and
    the identity the board is queried as have to be one value, or a host resolves
    another agent's token and posts as itself.
    """
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        'QUARTERBACK_TOKEN_CMD=\'printf "%s" "$QUARTERBACK_AGENT"\'\n',
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.token == cfg.agent == boardcfg._hostname()


def test_the_config_file_itself_may_reference_the_agent_name(tmp_path):
    """The file is sourced, so a double-quoted command expands at source time.

    That is a second, earlier moment the variable has to already be set — the file
    is read before the command runs, and a site writing the documented form in
    double quotes is not writing a different contract.
    """
    host = boardcfg._hostname()
    tokens = write_token_file(tmp_path, {host: "tok-thishost"})
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        f'QUARTERBACK_TOKEN_CMD="sed -n s/^$QUARTERBACK_AGENT://p {tokens}"\n',
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.token == "tok-thishost"


def test_an_environment_agent_beats_the_hostname_inside_the_command(tmp_path):
    """Environment beats everything, at the moment it is used and not only after."""
    host = boardcfg._hostname()
    named = other_than(host)
    tokens = write_token_file(tmp_path, {host: "tok-host", named: "tok-named"})
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        f"QUARTERBACK_TOKEN_CMD='sed -n s/^$QUARTERBACK_AGENT://p {tokens}'\n",
    )
    env = env_for(tmp_path, QUARTERBACK_CONFIG=str(path))
    assert resolve(env).token == "tok-host"
    assert resolve({**env, "QUARTERBACK_AGENT": named}).token == "tok-named"


def test_resolving_the_agent_does_not_mutate_the_callers_environment(tmp_path):
    """`resolve` exports the name into its children, not into its caller's dict.

    It takes the environment as a mapping, and a caller that resolves two boards in
    one process must not have the first call decide the second's identity.
    """
    env = env_for(tmp_path, QUARTERBACK_BASE_URL="https://b.example")
    resolve(env)
    assert "QUARTERBACK_AGENT" not in env


# -- why there is no token, said out loud (#201) -----------------------


def test_a_token_cmd_that_produces_nothing_says_so(tmp_path):
    """A command that ran and matched nothing is not a config with no command in it.

    Reported as the latter, the remedy on offer is to set a token command in a
    config file that already has a working one — which on the fleet is a generated
    file, so acting on the message means editing something stamped "do not edit".
    """
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\nQUARTERBACK_TOKEN_CMD='true'\n",
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.token is None and not cfg.authenticated
    assert cfg.token_problem == "the token command succeeded but produced no output"


def test_a_failing_token_cmd_reports_its_exit_status(tmp_path):
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\nQUARTERBACK_TOKEN_CMD='exit 7'\n",
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.token_problem == "the token command exited 7"


def test_no_token_source_at_all_has_nothing_to_explain(tmp_path):
    """`token_problem` is None here, and the caller's "set one" message is right."""
    cfg = resolve(
        env_for(
            tmp_path,
            QUARTERBACK_CONFIG=str(tmp_path / "absent"),
            QUARTERBACK_BASE_URL="https://board.example",
        )
    )
    assert cfg.token is None and cfg.token_problem is None


def test_a_resolved_token_has_nothing_to_explain(tmp_path):
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\nQUARTERBACK_TOKEN=fromfile\n",
    )
    assert resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path))).token_problem is None


def test_the_legacy_file_rescuing_a_failed_command_clears_the_problem(tmp_path, monkeypatch):
    """An authenticated client must not be described as one that failed to authenticate.

    The pre-config fleet token file is tried after the command, so a host that has
    not been rebuilt resolves a token *and* has a command that came back empty.
    """
    legacy = tmp_path / "legacy-token"
    legacy.write_text("legacy-tok\n")
    monkeypatch.setattr(boardcfg, "LEGACY_TOKEN_FILE", legacy)
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\nQUARTERBACK_TOKEN_CMD='true'\n",
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.token == "legacy-tok" and cfg.authenticated
    assert cfg.token_problem is None


def test_a_timed_out_command_names_the_timeout_and_not_the_command(monkeypatch):
    """Directly, because the real path costs the timeout and this branch is one line.

    `TimeoutExpired.__str__` is "Command '<cmd>' timed out after Ns", and the whole
    point of not using it is that `<cmd>` can be the credential.
    """

    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="echo s3cr3t-material", timeout=15)

    monkeypatch.setattr(boardcfg.subprocess, "run", boom)
    token, problem = boardcfg._run_token_cmd("echo s3cr3t-material", {})
    assert token is None
    assert "s3cr3t-material" not in problem
    assert "15" in problem and "still running" in problem


def test_a_command_that_cannot_be_run_at_all_is_distinguishable(monkeypatch):
    """No shell to run it with is a host problem, not a config one."""

    def boom(*a, **kw):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(boardcfg.subprocess, "run", boom)
    token, problem = boardcfg._run_token_cmd("anything", {})
    assert token is None and "could not be run" in problem


def test_no_diagnostic_repeats_the_command_or_its_output(tmp_path):
    """These strings are printed and pasted into issues; a token command is a secret.

    `subprocess.TimeoutExpired` renders the command it ran, and `echo <literal>` is
    a real site shape — so the timeout branch must not be `str(e)`.
    """
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        "QUARTERBACK_TOKEN_CMD='echo s3cr3t-material; exit 3'\n",
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.token is None
    assert "s3cr3t-material" not in (cfg.token_problem or "")
    assert "echo" not in (cfg.token_problem or "")


def test_config_path_honours_xdg_then_home(tmp_path):
    assert config_path({"XDG_CONFIG_HOME": "/xdg", "HOME": "/h"}) == __import__(
        "pathlib"
    ).Path("/xdg/quarterback/config")
    assert config_path({"HOME": "/h"}).as_posix() == "/h/.config/quarterback/config"
    assert config_path({"QUARTERBACK_CONFIG": "/tmp/x"}).as_posix() == "/tmp/x"


def test_config_path_without_home_resolves_rather_than_writing_a_literal_tilde():
    """Path does not expand `~`, so the old fallback made a `./~` beside the cwd.

    An env with no HOME is a real case — a systemd unit, `env -i` — and a config
    read from a relative directory named `~` is read from nowhere.
    """
    path = config_path({})
    assert path.is_absolute()
    assert "~" not in path.as_posix()


def test_missing_config_file_is_not_an_error_when_the_env_has_the_url(tmp_path):
    cfg = resolve(
        env_for(
            tmp_path,
            QUARTERBACK_CONFIG=str(tmp_path / "absent"),
            QUARTERBACK_BASE_URL="https://board.example",
        )
    )
    assert cfg.base_url == "https://board.example" and cfg.token is None
