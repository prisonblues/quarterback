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


# -- the agent name is resolved BEFORE the token command (#201) --------

#: The name most of these tests run under. A literal, not `_hostname()`: the token
#: commands below put the agent name inside a shell word and (for the sed form the
#: fleet generates) inside a regex, so a CI runner whose short hostname contains a
#: `.` would silently widen the match, one containing whitespace would split the
#: argument, and an empty one would reproduce the `s/^://p` bug the tests are
#: *checking for* and pass. One test below deliberately keeps the real hostname —
#: the export path end to end has to be proved against the real default too.
AGENT = "daedalus"


@pytest.fixture
def pinned_host(monkeypatch):
    """`_hostname()` fixed to :data:`AGENT`, so nothing depends on the runner's name."""
    monkeypatch.setattr(boardcfg, "_hostname", lambda: AGENT)
    return AGENT


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


def test_the_token_command_can_reference_the_agent_name(tmp_path, pinned_host):
    """The #201 reproduction, verbatim: the fleet's own generated config.

    Resolved after the command instead of before it, `$QUARTERBACK_AGENT` expanded
    to empty, `sed -n s/^$QUARTERBACK_AGENT://p` became `s/^://p` and matched no
    line — so this client reported "no token" on a host whose token file was
    present and valid, and whose bash `qb` worked in the same shell in the same
    minute.
    """
    tokens = write_token_file(
        tmp_path, {pinned_host: "tok-thishost", other_than(pinned_host): "tok-other"}
    )
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
    assert cfg.agent == pinned_host  # the same value, or it picked another host's line
    assert cfg.token_problem is None


def test_the_real_hostname_reaches_the_command_too(tmp_path):
    """The one test on the UNPINNED `_hostname()`, so the export path is proved end to end.

    Everything else here pins the name to keep the shell quoting out of the runner's
    hands; that leaves nobody checking that the actual default — this machine's short
    hostname — is what gets exported. `printf` rather than a selector so the assertion
    needs nothing of the name but that it arrived whole.
    """
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        'QUARTERBACK_TOKEN_CMD=\'printf "%s" "$QUARTERBACK_AGENT"\'\n',
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.token == boardcfg._hostname()
    assert cfg.agent == boardcfg._hostname()


def test_the_agent_the_command_sees_is_the_agent_the_client_will_use(tmp_path, monkeypatch):
    """One resolution, shared — not two calls that happen to agree.

    `cfg.token == cfg.agent == _hostname()` would hold trivially for an
    implementation that resolved the name twice, which is the bug class (#201) this
    is about. So `_hostname` counts its calls and answers differently each time: the
    exported value and the reported value can only match if there was one call.
    """
    calls = []

    def counting_hostname():
        calls.append(None)
        return f"host-{len(calls)}"

    monkeypatch.setattr(boardcfg, "_hostname", counting_hostname)
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        'QUARTERBACK_TOKEN_CMD=\'printf "%s" "$QUARTERBACK_AGENT"\'\n',
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert calls == [None], "the name has to be resolved once, not once per consumer"
    assert cfg.token == cfg.agent == "host-1"


def test_the_config_file_itself_may_reference_the_agent_name(tmp_path, pinned_host):
    """The file is sourced, so a double-quoted command expands at source time.

    That is a second, earlier moment the variable has to already be set — the file
    is read before the command runs, and a site writing the documented form in
    double quotes is not writing a different contract.
    """
    tokens = write_token_file(tmp_path, {pinned_host: "tok-thishost"})
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        f'QUARTERBACK_TOKEN_CMD="sed -n s/^$QUARTERBACK_AGENT://p {tokens}"\n',
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.token == "tok-thishost"


def test_an_environment_agent_beats_the_hostname_inside_the_command(tmp_path, pinned_host):
    """Environment beats everything, at the moment it is used and not only after."""
    named = other_than(pinned_host)
    tokens = write_token_file(tmp_path, {pinned_host: "tok-host", named: "tok-named"})
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        f"QUARTERBACK_TOKEN_CMD='sed -n s/^$QUARTERBACK_AGENT://p {tokens}'\n",
    )
    env = env_for(tmp_path, QUARTERBACK_CONFIG=str(path))
    assert resolve(env).token == "tok-host"
    assert resolve({**env, "QUARTERBACK_AGENT": named}).token == "tok-named"


def test_a_token_command_from_the_ENVIRONMENT_also_sees_the_agent(tmp_path, pinned_host):
    """The documented precedence is "environment beats the file", token command included.

    Every other test here sources the command from the config file, which leaves the
    other half of that sentence unguarded: an implementation that exported the name
    only around the file-sourced command would pass all of them and still hand an
    environment-supplied command an empty variable.
    """
    tokens = write_token_file(tmp_path, {pinned_host: "tok-from-env-cmd"})
    env = env_for(
        tmp_path,
        QUARTERBACK_CONFIG=str(tmp_path / "absent"),
        QUARTERBACK_BASE_URL="https://board.example",
        QUARTERBACK_TOKEN_CMD=f"sed -n s/^$QUARTERBACK_AGENT://p {tokens}",
    )
    cfg = resolve(env)
    assert cfg.token == "tok-from-env-cmd"
    assert cfg.token_problem is None


def test_an_environment_token_command_wins_over_the_files_and_still_sees_the_agent(
    tmp_path, pinned_host
):
    """Both halves at once: the environment's command runs, with the name set."""
    tokens = write_token_file(tmp_path, {pinned_host: "tok-right"})
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        "QUARTERBACK_TOKEN_CMD='echo tok-from-the-file'\n",
    )
    cfg = resolve(
        env_for(
            tmp_path,
            QUARTERBACK_CONFIG=str(path),
            QUARTERBACK_TOKEN_CMD=f"sed -n s/^$QUARTERBACK_AGENT://p {tokens}",
        )
    )
    assert cfg.token == "tok-right"


def test_the_documented_literal_selector_survives_awkward_agent_names(tmp_path, monkeypatch):
    """harness/README.md's canonical token command, run for the names that break sed.

    The fleet generates `sed -n s/^$QUARTERBACK_AGENT://p`, which puts the agent name
    into a regex *and* an unquoted shell word: a name with a `.` matches another
    host's line, one with a `/` breaks the `s///` delimiter, and one with whitespace
    splits into two sed arguments. The name is environment-overridable, so the README
    documents an `awk -v` comparison instead — and this is what proves the form it
    documents actually works.
    """
    tokens = write_token_file(
        tmp_path, {"host.one": "tok-dot", "host two": "tok-space", "host/three": "tok-slash"}
    )
    cmd = f'awk -F: -v a="$QUARTERBACK_AGENT" "\\$1 == a {{ print \\$2; exit }}" {tokens}'
    path = write_config(
        tmp_path,
        f"QUARTERBACK_BASE_URL=https://board.example\nQUARTERBACK_TOKEN_CMD='{cmd}'\n",
    )
    env = env_for(tmp_path, QUARTERBACK_CONFIG=str(path))
    for name, expected in (
        ("host.one", "tok-dot"),
        ("host two", "tok-space"),
        ("host/three", "tok-slash"),
    ):
        assert resolve({**env, "QUARTERBACK_AGENT": name}).token == expected, name
    # And a name that is genuinely absent is absent, rather than matching a neighbour
    # the way an unanchored regex would.
    absent = resolve({**env, "QUARTERBACK_AGENT": "hostXone"})
    assert absent.token is None
    assert absent.token_problem == "the token command succeeded but produced no output"


def test_resolving_the_agent_does_not_mutate_the_callers_environment(tmp_path):
    """`resolve` exports the name into its children, not into its caller's dict.

    It takes the environment as a mapping, and a caller that resolves two boards in
    one process must not have the first call decide the second's identity.
    """
    env = env_for(tmp_path, QUARTERBACK_BASE_URL="https://b.example")
    resolve(env)
    assert "QUARTERBACK_AGENT" not in env


# -- the agent name the config FILE pins (qb-env parity) ---------------


def test_the_config_file_may_pin_the_agent_name(tmp_path, pinned_host):
    """`qb-env` honours a file-pinned name, so this has to as well.

    `qb_load_config` sources the config (qb-env:43) and only then applies
    `QUARTERBACK_AGENT="${QUARTERBACK_AGENT:-$(hostname -s)}"` (:44) — so a file that
    names the host wins over the hostname there. A client that ignored the pin would
    read one identity while the shell on the same box read another, which is #201's
    own failure pointed the other way.
    """
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\nQUARTERBACK_AGENT=pinned-by-file\n",
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.agent == "pinned-by-file"
    assert pinned_host != "pinned-by-file"  # the hostname default was really overridden


def test_a_file_pinned_agent_reaches_the_token_command(tmp_path, pinned_host):
    """Pinned or defaulted, it is the same name the command is given."""
    tokens = write_token_file(tmp_path, {"pinned-by-file": "tok-pinned", pinned_host: "tok-host"})
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        "QUARTERBACK_AGENT=pinned-by-file\n"
        f"QUARTERBACK_TOKEN_CMD='sed -n s/^$QUARTERBACK_AGENT://p {tokens}'\n",
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.agent == "pinned-by-file"
    assert cfg.token == "tok-pinned"


def test_the_environment_still_beats_a_file_pinned_agent(tmp_path, pinned_host):
    """Environment beats the config file throughout — the agent name is not an exception."""
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\nQUARTERBACK_AGENT=pinned-by-file\n",
    )
    env = env_for(tmp_path, QUARTERBACK_CONFIG=str(path), QUARTERBACK_AGENT="from-env")
    assert resolve(env).agent == "from-env"


def test_a_file_that_pins_no_name_leaves_the_hostname_default(tmp_path, pinned_host):
    """Reading the pin must not have replaced the default with the empty string."""
    path = write_config(tmp_path, "QUARTERBACK_BASE_URL=https://board.example\n")
    assert resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path))).agent == pinned_host


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


def test_an_empty_environment_token_is_no_token_rather_than_an_empty_one(tmp_path):
    """`BoardConfig.token` is None-or-nonempty; the constructor no longer re-checks.

    `token=token` at the call site states an invariant the `or` chains above uphold,
    so this is the test that keeps upholding it: an exported-but-empty
    QUARTERBACK_TOKEN must arrive as None, not as `""`.
    """
    cfg = resolve(
        env_for(
            tmp_path,
            QUARTERBACK_CONFIG=str(tmp_path / "absent"),
            QUARTERBACK_BASE_URL="https://board.example",
            QUARTERBACK_TOKEN="",
        )
    )
    assert cfg.token is None and not cfg.authenticated


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


def test_an_unreadable_legacy_token_file_is_named_not_swallowed(tmp_path, monkeypatch):
    """The #201 misdiagnosis in its second place, and the last silent token source.

    A host whose legacy token file exists but cannot be read — wrong mode, EIO, a
    dangling symlink — and which configures no token command resolved
    `token=None, token_problem=None`, so the caller printed "this machine has no
    token, set QUARTERBACK_TOKEN or QUARTERBACK_TOKEN_CMD". A credential source that
    was present and did not yield, reported as one that was never configured: exactly
    what every other source in this function was instrumented to stop doing.
    """
    legacy = tmp_path / "legacy-token"
    legacy.write_text("legacy-tok\n")
    legacy.chmod(0o000)
    monkeypatch.setattr(boardcfg, "LEGACY_TOKEN_FILE", legacy)
    cfg = resolve(
        env_for(
            tmp_path,
            QUARTERBACK_CONFIG=str(tmp_path / "absent"),
            QUARTERBACK_BASE_URL="https://board.example",
        )
    )
    if cfg.token is not None:  # running as root, which can read a 0o000 file
        pytest.skip("this runner can read a mode-000 file, so there is no failure to name")
    assert cfg.token_problem is not None
    assert str(legacy) in cfg.token_problem
    assert "could not be read" in cfg.token_problem
    # The OS's own phrase, which is what makes it actionable — and carries no secret.
    assert "Permission denied" in cfg.token_problem


def test_an_empty_legacy_token_file_is_named_too(tmp_path, monkeypatch):
    """Present and yielding nothing is the same class of event as a command doing it."""
    legacy = tmp_path / "legacy-token"
    legacy.write_text("\n")
    monkeypatch.setattr(boardcfg, "LEGACY_TOKEN_FILE", legacy)
    cfg = resolve(
        env_for(
            tmp_path,
            QUARTERBACK_CONFIG=str(tmp_path / "absent"),
            QUARTERBACK_BASE_URL="https://board.example",
        )
    )
    assert cfg.token is None
    assert cfg.token_problem == f"{legacy} is empty"


def test_a_configured_commands_problem_outranks_the_legacy_files(tmp_path, monkeypatch):
    """Two sources failed; the one the site chose is the one worth naming."""
    legacy = tmp_path / "legacy-token"
    legacy.write_text("\n")
    monkeypatch.setattr(boardcfg, "LEGACY_TOKEN_FILE", legacy)
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\nQUARTERBACK_TOKEN_CMD='exit 4'\n",
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.token_problem == "the token command exited 4"


def test_whether_a_command_was_configured_is_a_fact_not_a_wording(tmp_path, monkeypatch):
    """`token_cmd_configured` is what the caller's remedy has to branch on.

    "Your command came back empty, so setting one is not the remedy" and "nothing
    configured a token source, so set one" are opposite instructions, and which is
    true is not recoverable from `token_problem`'s prose — the legacy token file
    produces a problem on hosts that configure no command at all. A caller that
    guessed would print a confidently false remedy, which is #201's actual cost.
    """
    legacy = tmp_path / "legacy-token"
    legacy.write_text("\n")
    monkeypatch.setattr(boardcfg, "LEGACY_TOKEN_FILE", legacy)

    bare = resolve(
        env_for(
            tmp_path,
            QUARTERBACK_CONFIG=str(tmp_path / "absent"),
            QUARTERBACK_BASE_URL="https://board.example",
        )
    )
    assert bare.token_problem == f"{legacy} is empty"
    assert bare.token_cmd_configured is False

    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\nQUARTERBACK_TOKEN_CMD='true'\n",
    )
    configured = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert configured.token_problem == "the token command succeeded but produced no output"
    assert configured.token_cmd_configured is True


def test_a_configured_command_is_reported_even_when_it_was_not_needed(tmp_path):
    """The flag is about configuration, not about execution.

    QUARTERBACK_TOKEN short-circuits the command, and the flag still says a command
    exists — which is the honest answer to "is one configured", and the only reading
    under which it stays useful if a later problem is attached on some other path.
    """
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\nQUARTERBACK_TOKEN_CMD='echo unused'\n",
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path), QUARTERBACK_TOKEN="direct"))
    assert cfg.token == "direct"
    assert cfg.token_problem is None
    assert cfg.token_cmd_configured is True


def test_a_timed_out_command_names_the_timeout_and_not_the_command(tmp_path, monkeypatch):
    """Driven through a real slow command, with the timeout shortened for the test.

    Two things this deliberately does not do. It does not patch `subprocess.run`,
    which `monkeypatch.setattr(boardcfg.subprocess, "run", …)` would do process-wide
    for the whole test — `subprocess` is the module object, not a module-local
    indirection, so everything else reachable from the test (pytest's own plugins
    included) would see the replacement too. And it does not assert the literal `15`:
    `_TOKEN_CMD_TIMEOUT` exists so that number has one home, and a test that hardcodes
    it turns changing the constant into a red suite for no real reason.

    `TimeoutExpired.__str__` is "Command '<cmd>' timed out after Ns", and the whole
    point of not using it is that `<cmd>` can be the credential — hence the command
    below carrying a literal that must not come back.
    """
    monkeypatch.setattr(boardcfg, "_TOKEN_CMD_TIMEOUT", 0.25)
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        "QUARTERBACK_TOKEN_CMD='echo s3cr3t-material >/dev/null; sleep 30'\n",
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.token is None
    problem = cfg.token_problem or ""
    assert "s3cr3t-material" not in problem
    assert "still running" in problem
    assert f"{boardcfg._TOKEN_CMD_TIMEOUT}s" in problem


def test_a_command_that_cannot_be_run_at_all_is_distinguishable(tmp_path):
    """No shell able to run it is a host problem, not a config one.

    A real OSError out of `subprocess.run` rather than a patched one: an argument
    longer than the kernel's per-argument limit fails with E2BIG before the shell is
    ever reached, which is the same arm a missing `/bin/sh` or an EPERM takes.
    """
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        "QUARTERBACK_TOKEN_CMD='true " + "x" * 300000 + "'\n",
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.token is None
    problem = cfg.token_problem or ""
    assert "could not be run" in problem
    # The class name alone ("OSError") is true and useless; strerror is the part the
    # operator can act on, and unlike `str(e)` it contains none of the command.
    assert "OSError: Argument list too long" in problem


def test_output_that_is_not_decodable_text_is_a_diagnostic_not_a_traceback(tmp_path):
    """`text=True` decodes, and an invalid byte used to raise straight through `resolve`.

    UnicodeDecodeError is a ValueError — not an OSError and not a SubprocessError — so
    the handler in `_run_token_cmd` never caught it: a helper emitting one bad byte
    crashed config resolution with a traceback, which is the unexplained failure this
    whole function exists to replace.
    """
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        r"""QUARTERBACK_TOKEN_CMD='printf "\xff\xfe\n"'""" + "\n",
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.token is None
    assert cfg.token_problem == "the token command produced output that is not decodable text"


def test_a_config_file_with_undecodable_bytes_does_not_crash_resolution(tmp_path):
    """The same hazard one layer out: the file is sourced through a `text=True` child."""
    path = tmp_path / "config"
    path.write_bytes(b"QUARTERBACK_BASE_URL=https://board.example\n# \xff\xfe\n")
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.base_url == "https://board.example"


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
