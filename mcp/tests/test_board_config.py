"""The one rule that must never bend: an unset base URL is an error, not a guess."""

from __future__ import annotations

import os

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
