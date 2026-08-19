"""The one rule that must never bend: an unset base URL is an error, not a guess."""

from __future__ import annotations

import errno
import locale
import os
import shlex

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


def sh(path) -> str:
    """A path as a single shell word.

    Every token command below is a *string* handed to a shell, and `tmp_path` is
    wherever the runner's TMPDIR points: a temp root containing a space would split
    `sed -n … /tmp/my dir/api-tokens` into two arguments and read a file that is not
    there — failing identically to the bug half these tests exist to catch, which is
    what makes quoting worth the noise rather than pedantry. The tests just below
    exercise shell-word safety for agent *names*; the path deserves the same.
    """
    return shlex.quote(str(path))


#: Bytes that are not text under any encoding `locale.getencoding()` plausibly
#: returns on a POSIX host: `ff` is invalid in UTF-8 and outside ASCII.
UNDECODABLE_BYTES = b"\xff\xfe"


def require_undecodable() -> None:
    """Skip unless this process really cannot decode :data:`UNDECODABLE_BYTES`.

    The undecodable-input tests assert on a diagnosis that only exists when the
    decode actually fails, and whether it fails is a property of the runner's locale
    rather than of the code — a latin-1 process reads `ff fe` as two perfectly good
    characters. Skipping says so out loud rather than failing for an unrelated
    reason, which is the same trap the `/bin/sh` printf escapes fell into.

    Decoded here rather than through `boardcfg._decode`, deliberately. Asking the
    function under test whether it can decode these bytes makes the skip condition and
    the assertion the same claim: a `_decode` that stopped refusing anything would turn
    every test below into a skip and report success.
    """
    try:
        UNDECODABLE_BYTES.decode(locale.getencoding())
    except UnicodeDecodeError:
        return
    pytest.skip("this process's encoding decodes these bytes, so there is nothing to reject")


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


def test_a_static_token_wins_over_a_command_from_either_source(tmp_path):
    """The precedence harness/README.md documents, which is not source-by-source.

    Environment-beats-file applies to each variable separately; between the two, a
    resolved `QUARTERBACK_TOKEN` short-circuits the command whichever source it came
    from. So a one-shot `QUARTERBACK_TOKEN_CMD=…` in front of the command does *not*
    override a static token in the config file — worth pinning rather than leaving to
    be rediscovered, because the natural reading of "environment beats the config
    file" says the opposite.
    """
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\nQUARTERBACK_TOKEN=static-from-file\n",
    )
    env = env_for(tmp_path, QUARTERBACK_CONFIG=str(path))
    assert resolve({**env, "QUARTERBACK_TOKEN_CMD": "echo from-env-cmd"}).token == (
        "static-from-file"
    )
    # And the other way round, which is the override that does work.
    assert resolve({**env, "QUARTERBACK_TOKEN": "from-env"}).token == "from-env"


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
        # `$HOME` unexpanded in the file, quoted in the command it becomes: the point
        # is that bash expands it when the file is sourced, and a HOME with a space in
        # it must not then split into two arguments.
        'QUARTERBACK_TOKEN_CMD="cat \\"$HOME/.tok\\""\n',
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
        f"QUARTERBACK_TOKEN_CMD='sed -n s/^$QUARTERBACK_AGENT://p {sh(tokens)}'\n",
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
        f'QUARTERBACK_TOKEN_CMD="sed -n s/^$QUARTERBACK_AGENT://p {sh(tokens)}"\n',
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
        f"QUARTERBACK_TOKEN_CMD='sed -n s/^$QUARTERBACK_AGENT://p {sh(tokens)}'\n",
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
        QUARTERBACK_TOKEN_CMD=f"sed -n s/^$QUARTERBACK_AGENT://p {sh(tokens)}",
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
            QUARTERBACK_TOKEN_CMD=f"sed -n s/^$QUARTERBACK_AGENT://p {sh(tokens)}",
        )
    )
    assert cfg.token == "tok-right"


def test_the_documented_literal_selector_survives_awkward_agent_names(tmp_path):
    """harness/README.md's canonical token command, run for the names that break sed.

    The fleet generates `sed -n s/^$QUARTERBACK_AGENT://p`, which puts the agent name
    into a regex *and* an unquoted shell word: a name with a `.` matches another
    host's line, one with a `/` breaks the `s///` delimiter, and one with whitespace
    splits into two sed arguments. The name is environment-overridable, so the README
    documents an `awk -v` comparison instead — and this is what proves the form it
    documents actually works.

    The tokens below carry colons on purpose, and that is the second half of the
    documented form. `sed -n s/^name://p` printed everything after the *first* colon;
    an awk selector written as `print $2` under `-F:` prints only the next field, so a
    structured `daedalus:v1:abc123` line authenticates with a truncated bearer — a 401
    that looks nothing like a documentation bug. `sub(/^[^:]*:/, "")` restores the sed
    semantics, and a colon-free value is checked too, since that is the shape the fleet
    has today and the replacement has to keep working for it.
    """
    tokens = write_token_file(
        tmp_path,
        {
            "host.one": "v1:tok-dot",
            "host two": "v1:tok-space",
            "host/three": "v1:tok-slash",
            "plainhost": "tok-no-colon",
        },
    )
    cmd = (
        'awk -F: -v a="$QUARTERBACK_AGENT" '
        f'"\\$1 == a {{ sub(/^[^:]*:/, \\"\\"); print; exit }}" {sh(tokens)}'
    )
    path = write_config(
        tmp_path,
        f"QUARTERBACK_BASE_URL=https://board.example\nQUARTERBACK_TOKEN_CMD='{cmd}'\n",
    )
    env = env_for(tmp_path, QUARTERBACK_CONFIG=str(path))
    for name, expected in (
        ("host.one", "v1:tok-dot"),
        ("host two", "v1:tok-space"),
        ("host/three", "v1:tok-slash"),
        ("plainhost", "tok-no-colon"),
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


# -- the agent name and the config FILE: a deliberate divergence -------


def test_a_config_file_agent_pin_is_deliberately_not_honoured(tmp_path, pinned_host):
    """`qb-env` honours a file-pinned name and this client does not. On purpose.

    `qb_load_config` sources the config into the calling shell and only *then* applies
    `QUARTERBACK_AGENT="${QUARTERBACK_AGENT:-$(hostname -s)}"`, so on the shell side a
    plain assignment in the file overwrites the environment and the conditional form
    fires as well. Matching that here means reproducing bash's interleaving of
    assignments and source-time expansions in Python, and the implementation that
    tried produced a **split identity**: with a plainly-pinned name and a
    double-quoted token command, one agent's credential was fetched while the client
    posted as another. The fleet's generated config pins no agent at all, so the pin
    is a shape the contract allows and no host writes — and fetching daedalus's token
    while posting as atlas is a worse failure than declining to honour it.

    Asserted rather than left latent: this is a decision, and a decision nothing
    checks is a surprise waiting for the next reader.
    """
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\nQUARTERBACK_AGENT=pinned-by-file\n",
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.agent == pinned_host
    assert pinned_host != "pinned-by-file"  # so the assertion above is not vacuous


def test_the_config_file_is_not_read_for_an_agent_name_at_all(tmp_path, pinned_host):
    """The mechanism, not only the outcome.

    An implementation that read the pin back and then chose not to use it would pass
    the test above and leave the next reader one `or` away from the split identity the
    module docstring describes. So the variable list itself is what is asserted.
    """
    assert "QUARTERBACK_AGENT" not in boardcfg._VARS
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\nQUARTERBACK_AGENT=pinned-by-file\n",
    )
    assert "QUARTERBACK_AGENT" not in boardcfg._read_config_file(path, env_for(tmp_path)).values


def test_the_fleets_single_quoted_selector_runs_under_the_resolved_agent(tmp_path, pinned_host):
    """The property #201 is actually about, in the form the fleet actually generates.

    The generated `QUARTERBACK_TOKEN_CMD` is **single**-quoted, so nothing in it
    expands when the file is sourced: `$QUARTERBACK_AGENT` is expanded by the shell
    that runs the command, against whatever name this client finally resolved. Both
    ways of arriving at that name are checked, because the host #201 was found on took
    the second — nothing in its environment set the variable at all.

    The file also pins a deliberately wrong agent name, and the token still comes back
    for the resolved one: a pin cannot pull the credential away from the identity the
    client is about to post as.
    """
    tokens = write_token_file(
        tmp_path, {pinned_host: "tok-thishost", "atlas": "tok-atlas", "junk": "tok-junk"}
    )
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        "QUARTERBACK_AGENT=junk\n"
        f"QUARTERBACK_TOKEN_CMD='sed -n s/^$QUARTERBACK_AGENT://p {sh(tokens)}'\n",
    )
    env = env_for(tmp_path, QUARTERBACK_CONFIG=str(path))

    defaulted = resolve(env)
    assert (defaulted.agent, defaulted.token) == (pinned_host, "tok-thishost")

    from_env = resolve({**env, "QUARTERBACK_AGENT": "atlas"})
    assert (from_env.agent, from_env.token) == ("atlas", "tok-atlas")


def test_the_environment_agent_beats_everything_a_file_can_say(tmp_path, pinned_host):
    """Environment beats the config file throughout — the agent name most of all."""
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\nQUARTERBACK_AGENT=pinned-by-file\n",
    )
    env = env_for(tmp_path, QUARTERBACK_CONFIG=str(path), QUARTERBACK_AGENT="from-env")
    assert resolve(env).agent == "from-env"


def test_a_file_that_names_no_agent_leaves_the_hostname_default(tmp_path, pinned_host):
    """The ordinary fleet host: the generated config names no agent, so the hostname is it."""
    path = write_config(tmp_path, "QUARTERBACK_BASE_URL=https://board.example\n")
    assert resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path))).agent == pinned_host


# -- the agent-name default's own fallbacks ----------------------------


def test_the_hostname_default_never_resolves_to_an_empty_agent_name(monkeypatch):
    """Both fallback arms, because a wrong agent name is quieter than a crash.

    `_hostname` answers "unknown" rather than raising, so a regression in either arm
    is not an exception — it is every host in the fleet posting under one name and
    selecting the same line out of a shared token file. Nothing else covers them: the
    OSError arm needs a host with no resolvable name and the empty arm needs one
    called `.something`, so both have to be driven here.

    `socket.gethostname` is patched on the module object because that is how
    `config.py` reaches it; monkeypatch puts it back either way.
    """
    monkeypatch.setattr(boardcfg.socket, "gethostname", lambda: "box.dc.example")
    assert boardcfg._hostname() == "box"

    monkeypatch.setattr(boardcfg.socket, "gethostname", lambda: ".no-short-name")
    assert boardcfg._hostname() == "unknown"

    monkeypatch.setattr(boardcfg.socket, "gethostname", _raise_no_name)
    assert boardcfg._hostname() == "unknown"


def test_a_host_with_no_resolvable_name_still_resolves_a_board(tmp_path, monkeypatch):
    """Failing to compute a label must not stop a client that has a board and a token."""
    monkeypatch.setattr(boardcfg.socket, "gethostname", _raise_no_name)
    cfg = resolve(
        env_for(
            tmp_path,
            QUARTERBACK_CONFIG=str(tmp_path / "absent"),
            QUARTERBACK_BASE_URL="https://board.example",
            QUARTERBACK_TOKEN="tok",
        )
    )
    assert (cfg.agent, cfg.token) == ("unknown", "tok")


def _raise_no_name() -> str:
    """A `gethostname` that fails the way a host with no name does."""
    raise OSError(errno.ENXIO, os.strerror(errno.ENXIO))


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

    A host whose legacy token file exists but cannot be read — wrong mode, EIO — and
    which configures no token command resolved
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
    # `os.strerror`, not the English string: `strerror` is translated under a non-C
    # LC_MESSAGES, so a hardcoded "Permission denied" asserts on the runner's locale.
    assert os.strerror(errno.EACCES) in cfg.token_problem


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
        # `exec`, so the kill after the timeout reaches the sleep rather than leaving
        # it orphaned for 30s after the test has finished with it.
        "QUARTERBACK_TOKEN_CMD='echo s3cr3t-material >/dev/null; exec sleep 30'\n",
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
    longer than the kernel's limit fails with E2BIG before the shell is ever reached,
    which is the same arm a missing `/bin/sh` or an EPERM takes.

    Three platform assumptions, all handled rather than assumed. The size comes from
    `SC_ARG_MAX` and not a flat 300 kB, because 300 kB only clears Linux's
    per-argument ceiling (32 pages) on a 4 kB-page host — on a 64 kB-page kernel it
    fits, the shell runs, and this would silently be a test of a different branch. If
    the exec somehow still succeeds it skips rather than asserting on whatever else
    happened. And the phrase asserted is `os.strerror`'s, because `strerror` is
    translated under a non-C LC_MESSAGES and a hardcoded "Argument list too long"
    asserts on the runner's locale.
    """
    try:
        arg_max = os.sysconf("SC_ARG_MAX")
    except (ValueError, OSError):  # not defined here; the flat size is the fallback
        arg_max = 0
    filler = "x" * max(300_000, arg_max + 1)
    path = write_config(
        tmp_path,
        f"QUARTERBACK_BASE_URL=https://board.example\nQUARTERBACK_TOKEN_CMD='true {filler}'\n",
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    problem = cfg.token_problem or ""
    if "could not be run" not in problem:
        pytest.skip(f"this host ran a {len(filler)}-byte argument, so there is no E2BIG to see")
    assert cfg.token is None
    # The class name alone ("OSError") is true and useless; strerror is the part the
    # operator can act on, and unlike `str(e)` it contains none of the command.
    assert f"OSError: {os.strerror(errno.E2BIG)}" in problem
    # And it must still contain none of the command. This is the round where
    # `strerror` began being interpolated into that message, so the guard that the
    # command cannot ride along with it belongs here rather than being dropped. A
    # short chunk as well as the whole filler, so a truncated leak is caught too.
    assert filler not in problem
    assert "x" * 200 not in problem


def test_output_that_is_not_decodable_text_is_a_diagnostic_not_a_traceback(tmp_path):
    r"""`text=True` decodes, and an invalid byte used to raise straight through `resolve`.

    UnicodeDecodeError is a ValueError — not an OSError and not a SubprocessError — so
    the handler in `_run_token_cmd` never caught it: a helper emitting one bad byte
    crashed config resolution with a traceback, which is the unexplained failure this
    whole function exists to replace.

    The escape is octal, and it has to stay octal. `\ooo` is the only byte escape
    POSIX defines for `printf`, and the command runs under `shell=True` — i.e.
    `/bin/sh`, which is dash on Debian/Ubuntu and bash on a NixOS workstation. The
    `\xHH` form this test first used is a bash/GNU extension: dash's printf reads
    `\xff` as codepoint U+00FF and re-encodes it as UTF-8 (`c3 bf`), which decodes
    cleanly, so the assertions below failed on CI and passed wherever `/bin/sh` was
    bash. That made the suite's result a property of the `/bin/sh` symlink rather
    than of the code. `\377\376` is `ff fe` on both, and `ff` is never valid UTF-8.

    Which leaves one runner property still in play — the process encoding — so the
    guard for it is here too: a latin-1 process reads `ff fe` as two good characters
    and there is nothing for this to reject.
    """
    require_undecodable()
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        r"""QUARTERBACK_TOKEN_CMD='printf "\377\376\n"'""" + "\n",
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


# -- values this process cannot read are named, not used ---------------


def test_a_config_file_token_this_process_cannot_read_is_named_not_used(tmp_path):
    """A mangled credential is a 401 with nothing to explain it.

    Both decode sites this PR added — the token command's output and the legacy file —
    refuse a value they could not decode, on the reasoning that a credential is not
    U+FFFD. The config file's own read did not: a `QUARTERBACK_TOKEN` carrying a byte
    that is not valid here came back replacement-mangled, truthy, and was sent to the
    board as a bearer. That is #201's cost in a third place — a credential source that
    was present and did not yield, surfacing as an unexplained 401.
    """
    require_undecodable()
    path = tmp_path / "config"
    path.write_bytes(
        b"QUARTERBACK_BASE_URL=https://board.example\n"
        b"QUARTERBACK_TOKEN=tok" + UNDECODABLE_BYTES + b"\n"
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.token is None and not cfg.authenticated
    assert cfg.token_problem == (
        f"{path} sets QUARTERBACK_TOKEN to something that is not decodable text"
    )


def test_one_unreadable_value_in_the_config_file_does_not_cost_the_others(tmp_path):
    """Decoded per field, so a bad byte in the token is not a bad byte in the URL.

    The whole payload comes back from one `printf`; decoding it as a single string
    would have turned an unreadable token into a host that has no board configured.
    """
    require_undecodable()
    path = tmp_path / "config"
    path.write_bytes(
        b"QUARTERBACK_BASE_URL=https://board.example\n"
        b"QUARTERBACK_TOKEN=tok" + UNDECODABLE_BYTES + b"\n"
    )
    assert resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path))).base_url == (
        "https://board.example"
    )


def test_a_config_file_token_command_that_is_not_text_here_still_counts_as_configured(tmp_path):
    """Named, and named as a command that exists.

    `token_cmd_configured` decides which remedy the caller prints, and a command the
    file sets in bytes this process cannot read is still a command the site
    configured: telling that operator to set one is the confidently false instruction
    #201 was.
    """
    require_undecodable()
    path = tmp_path / "config"
    path.write_bytes(
        b"QUARTERBACK_BASE_URL=https://board.example\n"
        b"QUARTERBACK_TOKEN_CMD=echo" + UNDECODABLE_BYTES + b"\n"
    )
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.token is None
    assert cfg.token_problem == (
        f"{path} sets QUARTERBACK_TOKEN_CMD to something that is not decodable text"
    )
    assert cfg.token_cmd_configured is True


def test_a_config_file_base_url_that_is_not_text_here_fails_loudly(tmp_path):
    """Not "this machine has not been told which board it belongs to" — it has been.

    Reported as unset, the operator is sent to set what is already set; used anyway,
    a replacement-mangled host is a URL that reaches nowhere or, worse, somewhere.
    """
    require_undecodable()
    path = tmp_path / "config"
    path.write_bytes(b"QUARTERBACK_BASE_URL=https://board" + UNDECODABLE_BYTES + b"\n")
    with pytest.raises(NoBoardConfigured) as e:
        resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert "QUARTERBACK_BASE_URL" in str(e.value)
    assert "not text here" in str(e.value)
    assert "no default" not in str(e.value)  # the *other* message, which would be false


def test_the_config_read_is_driven_by_the_variable_list_not_a_written_out_format(
    tmp_path, monkeypatch
):
    """A fourth variable must not silently break the read of the first three.

    The child prints one NUL-separated field per name in `_VARS`, and the arguments to
    that `printf` are built from `_VARS` too. It is the *arguments* that have to track
    it: `printf` reuses its format string until the arguments run out, so a format
    written out by hand keeps working even with a fourth variable — but a hand-written
    argument list does not, and the fourth field then arrives empty, silently. Which is
    what this drives: with `QUARTERBACK_EXTRA` in `_VARS` and only three names printed,
    it comes back missing while everything looks fine.
    """
    monkeypatch.setattr(boardcfg, "_VARS", (*boardcfg._VARS, "QUARTERBACK_EXTRA"))
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\n"
        "QUARTERBACK_TOKEN=fromfile\n"
        "QUARTERBACK_EXTRA=fourth\n",
    )
    got = boardcfg._read_config_file(path, env_for(tmp_path))
    assert got.values["QUARTERBACK_EXTRA"] == "fourth"
    assert got.values["QUARTERBACK_BASE_URL"] == "https://board.example"
    assert got.values["QUARTERBACK_TOKEN"] == "fromfile"


def test_a_token_that_genuinely_contains_the_replacement_character_is_kept(tmp_path):
    """Exact, rather than confidently wrong in the other direction.

    The check was "does the decoded string contain U+FFFD", which rejects a
    credential that legitimately contains that character with the diagnosis "not
    decodable text" — a false statement about a token that decoded perfectly. The
    bytes are now what is tested, so this passes through untouched.
    """
    token = "tok\ufffdmore"
    try:
        assignment = f"QUARTERBACK_TOKEN={token}\n".encode(locale.getencoding())
    except UnicodeEncodeError:  # an ASCII process cannot hold this token at all
        pytest.skip("this process's encoding cannot represent U+FFFD, so no such token exists")
    path = tmp_path / "config"
    path.write_bytes(b"QUARTERBACK_BASE_URL=https://board.example\n" + assignment)
    cfg = resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path)))
    assert cfg.token == token
    assert cfg.token_problem is None


# -- one definition of "a token", across all three sources -------------


def test_a_whitespace_only_token_is_no_token_wherever_it_came_from(tmp_path):
    """All three sources agree, so `BoardConfig.token`'s stated invariant is exact.

    `QUARTERBACK_TOKEN_CMD='echo "  "'` was already "produced no output", because a
    command's output is stripped. The identical value written straight into
    `QUARTERBACK_TOKEN` was truthy and passed through unstripped — sent to the board
    as a bearer of three spaces, which is a 401 with nothing to explain it, and it
    made the "None or non-empty" comment at the constructor untrue.
    """
    assert (
        resolve(
            env_for(
                tmp_path,
                QUARTERBACK_CONFIG=str(tmp_path / "absent"),
                QUARTERBACK_BASE_URL="https://board.example",
                QUARTERBACK_TOKEN="   ",
            )
        ).token
        is None
    )
    path = write_config(
        tmp_path,
        'QUARTERBACK_BASE_URL=https://board.example\nQUARTERBACK_TOKEN="  "\n',
    )
    assert resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path))).token is None


def test_a_blank_environment_token_falls_through_to_the_file_like_an_empty_one(tmp_path):
    """Stripped per source rather than after the `or`, so blank behaves as empty does.

    `QUARTERBACK_TOKEN=""` has always fallen through to the file. Stripping after the
    `or` chain instead of before it would have let `QUARTERBACK_TOKEN="  "` swallow
    the file's perfectly good token and resolve None — a one-shot override turning
    into a one-shot outage.
    """
    path = write_config(
        tmp_path,
        "QUARTERBACK_BASE_URL=https://board.example\nQUARTERBACK_TOKEN=fromfile\n",
    )
    env = env_for(tmp_path, QUARTERBACK_CONFIG=str(path))
    assert resolve({**env, "QUARTERBACK_TOKEN": ""}).token == "fromfile"
    assert resolve({**env, "QUARTERBACK_TOKEN": "   "}).token == "fromfile"


def test_a_token_arrives_stripped_of_the_whitespace_around_it(tmp_path):
    """A trailing newline is not part of a bearer, and is not a legal header value."""
    path = write_config(
        tmp_path,
        'QUARTERBACK_BASE_URL=https://board.example\nQUARTERBACK_TOKEN="  tok  "\n',
    )
    assert resolve(env_for(tmp_path, QUARTERBACK_CONFIG=str(path))).token == "tok"


# -- the legacy token file, as a source that can be present and silent -


def test_a_legacy_token_file_with_more_than_one_line_takes_the_first(tmp_path, monkeypatch):
    """A newline inside a bearer is not a header value, so it must not become one.

    `_run_token_cmd` has always taken the first line; this read took the whole file
    and stripped it, so a legacy file carrying a trailing comment or a second token
    produced a token with an embedded newline. httpx then raised out of the first
    request — a traceback, in the one function whose purpose is a named diagnosis.
    """
    legacy = tmp_path / "legacy-token"
    legacy.write_text("legacy-tok\n# rotated 2026-01-01\n")
    monkeypatch.setattr(boardcfg, "LEGACY_TOKEN_FILE", legacy)
    cfg = resolve(
        env_for(
            tmp_path,
            QUARTERBACK_CONFIG=str(tmp_path / "absent"),
            QUARTERBACK_BASE_URL="https://board.example",
        )
    )
    assert cfg.token == "legacy-tok"
    assert cfg.token_problem is None


def test_a_dangling_legacy_symlink_is_named_rather_than_silently_absent(tmp_path, monkeypatch):
    """A credential source that is visibly present and cannot possibly yield.

    The read sat behind `LEGACY_TOKEN_FILE.is_file()`, which follows symlinks and is
    False for a dangling one — so this host took no branch at all and resolved
    `token=None, token_problem=None`. The caller then printed "this machine has no
    token, set QUARTERBACK_TOKEN or QUARTERBACK_TOKEN_CMD" for a machine whose token
    file needs relinking: exactly the silent misdiagnosis (#201) the rest of this
    section removes, and the reason the read is attempted rather than guarded.
    """
    legacy = tmp_path / "legacy-token"
    legacy.symlink_to(tmp_path / "gone")
    monkeypatch.setattr(boardcfg, "LEGACY_TOKEN_FILE", legacy)
    cfg = resolve(
        env_for(
            tmp_path,
            QUARTERBACK_CONFIG=str(tmp_path / "absent"),
            QUARTERBACK_BASE_URL="https://board.example",
        )
    )
    assert cfg.token is None
    assert cfg.token_problem == f"{legacy} is a symlink to nothing"
    assert cfg.token_cmd_configured is False


def test_an_absent_legacy_file_still_has_nothing_to_explain(tmp_path, monkeypatch):
    """Attempting the read must not turn every rebuilt host into a reported problem.

    ENOENT is both "no legacy file here", which is every host that has been rebuilt,
    and "a symlink to nothing", which is an event. Only the second is named; naming
    the first would put a `token_problem` on the whole fleet and drown the ones that
    matter.
    """
    monkeypatch.setattr(boardcfg, "LEGACY_TOKEN_FILE", tmp_path / "definitely-absent")
    cfg = resolve(
        env_for(
            tmp_path,
            QUARTERBACK_CONFIG=str(tmp_path / "absent"),
            QUARTERBACK_BASE_URL="https://board.example",
        )
    )
    assert cfg.token is None and cfg.token_problem is None


def test_a_legacy_token_file_that_is_not_text_here_is_named(tmp_path, monkeypatch):
    """The same refusal as the command's output, for the same reason: it is not the bytes."""
    require_undecodable()
    legacy = tmp_path / "legacy-token"
    legacy.write_bytes(UNDECODABLE_BYTES + b"\n")
    monkeypatch.setattr(boardcfg, "LEGACY_TOKEN_FILE", legacy)
    cfg = resolve(
        env_for(
            tmp_path,
            QUARTERBACK_CONFIG=str(tmp_path / "absent"),
            QUARTERBACK_BASE_URL="https://board.example",
        )
    )
    assert cfg.token is None
    assert cfg.token_problem == f"{legacy} does not contain decodable text"
