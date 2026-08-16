"""Tests for qb-seat, the per-seat wrapper that turns a pane into a fleet seat.

The script is short and does four things, and three of them are only observable
from outside the process: the environment it exports, the HTTP call it makes
before starting anything, and the command it finally becomes. So the fixtures
here stand in a fake ``claude`` and a fake ``curl`` on PATH and read back what
they were handed — the alternative, asserting on the source, would pass for a
script that no longer runs.

The refusals get the most tests, for the reason they usually do: a seat number
is interpolated into a board identity and a filename-shaped label, and starting
n agents by accident is expensive in a way that a wrong exit code is not.

Run: pytest harness/tests
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

QB_SEAT = Path(__file__).resolve().parent.parent / "bin" / "qb-seat"


@pytest.fixture
def repo(tmp_path):
    """A real git repository — qb-seat refuses anything else."""
    d = tmp_path / "repo"
    d.mkdir()
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    return d


@pytest.fixture
def fake_bin(tmp_path):
    """A PATH entry we own, for standing in the agent and curl."""
    d = tmp_path / "bin"
    d.mkdir()

    def _install(name, body):
        path = d / name
        path.write_text(body)
        path.chmod(0o755)
        return path

    _install.dir = d
    return _install


@pytest.fixture
def agent(fake_bin, tmp_path):
    """A fake agent binary that records how it was started, then exits 0."""
    record = tmp_path / "agent.json"
    fake_bin(
        "claude",
        "#!/usr/bin/env bash\n"
        'python3 -c "\n'
        "import json, os, sys\n"
        f"json.dump({{'args': sys.argv[1:], 'cwd': os.getcwd(),\n"
        "  'instance': os.environ.get('QUARTERBACK_INSTANCE')},\n"
        f"  open({str(record)!r}, 'w'))\n"
        '" "$@"\n',
    )
    agent_started = record
    return agent_started


@pytest.fixture
def runtime_dir(tmp_path):
    """Where the pane markers go. Isolated, or a suite run on a machine with a
    live fleet would trip over the real seats — and leave markers among them."""
    d = tmp_path / "run"
    d.mkdir()
    return d


@pytest.fixture
def run(repo, fake_bin, tmp_path, runtime_dir):
    """Invoke qb-seat with a board that is deliberately absent unless asked for."""

    def _run(*args, env=None, cwd=None):
        environ = {**os.environ, "PATH": f"{fake_bin.dir}:{os.environ['PATH']}"}
        for leaked in (
            "QUARTERBACK_BASE_URL",
            "QUARTERBACK_TOKEN",
            "QUARTERBACK_TOKEN_CMD",
            "QUARTERBACK_INSTANCE",
            "QB_SEAT_REPO",
            "QB_SEAT_BRIEF",
            "QB_SEAT_CLAUDE",
        ):
            environ.pop(leaked, None)
        # Never the developer's own config: a test that reads it would talk to
        # the real board on the machine it is running on.
        environ["QUARTERBACK_CONFIG"] = str(tmp_path / "no-such-config")
        environ["XDG_RUNTIME_DIR"] = str(runtime_dir)
        environ.pop("QB_SEAT_FORCE", None)
        environ.update(env or {})
        return subprocess.run(
            [str(QB_SEAT), *args],
            env=environ,
            cwd=str(cwd or repo),
            capture_output=True,
            text=True,
        )

    return _run


# ---- the seat number --------------------------------------------------------


@pytest.mark.parametrize("seat", ["1", "2", "9", "10", "42", "99"])
def test_a_seat_number_in_range_is_accepted(run, seat):
    assert run(seat, "--dry-run").returncode == 0


@pytest.mark.parametrize(
    "seat",
    [
        "0",            # there is no seat zero; the names are for humans
        "100",          # past the cap
        "03",           # would spell a second identity for seat 3
        "-1",
        "3.5",
        "three",
        "3 4",          # two seats, or one typo — either way not this
        "3;whoami",     # punctuation, in case a caller interpolates
        "../3",
    ],
)
def test_a_seat_number_out_of_shape_is_refused(run, seat):
    result = run(seat, "--dry-run")
    assert result.returncode == 2
    assert "seat number" in result.stderr


def test_no_argument_at_all_is_refused_with_the_usage(run):
    result = run()
    assert result.returncode == 2
    assert "need a seat number" in result.stderr
    assert "qb-seat <n>" in result.stderr


def test_help_works_outside_a_repository(run, tmp_path):
    """Documentation must not depend on where you happen to be standing."""
    result = run("--help", cwd=tmp_path)
    assert result.returncode == 0
    assert "qb-seat <n>" in result.stdout


# ---- where the seat works ---------------------------------------------------


def test_the_default_working_directory_is_the_pane_s_own(run, repo):
    """zellij and tmux both set a per-pane cwd; that is the layout's job, not ours."""
    assert f"cwd:      {repo}" in run("1", "--dry-run").stdout


def test_qb_seat_repo_overrides_the_working_directory(run, repo, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    out = run("1", "--dry-run", env={"QB_SEAT_REPO": str(other)}).stdout
    assert f"cwd:      {other}" in out


def test_a_directory_that_is_not_a_repository_is_refused(run, tmp_path):
    """A seat's entire brief needs a repo. Failing here beats an agent
    discovering it three tool calls in and improvising."""
    result = run("1", "--dry-run", cwd=tmp_path)
    assert result.returncode == 2
    assert "not a git repository" in result.stderr


def test_a_working_directory_that_does_not_exist_is_refused(run, tmp_path):
    result = run("1", "--dry-run", env={"QB_SEAT_REPO": str(tmp_path / "gone")})
    assert result.returncode == 2
    assert "not a directory" in result.stderr


# ---- the identity -----------------------------------------------------------


def test_the_instance_is_per_seat(run):
    """One value for the whole box gives n seats one ask cursor between them —
    whichever polls first swallows the rest's mail."""
    assert "instance: seat-3" in run("3", "--dry-run").stdout


def test_each_seat_gets_a_different_instance(run):
    instances = {
        line
        for seat in ("1", "2", "3")
        for line in run(seat, "--dry-run").stdout.splitlines()
        if line.startswith("instance:")
    }
    assert len(instances) == 3


def test_the_agent_is_started_with_the_instance_exported(run, agent):
    """Exported rather than passed: the hook and the MCP server are separate
    processes that have to arrive at the same identity."""
    assert run("4").returncode == 0
    assert json.loads(agent.read_text())["instance"] == "seat-4"


def test_the_agent_is_started_inside_the_repository(run, agent, repo):
    run("1")
    assert Path(json.loads(agent.read_text())["cwd"]).resolve() == repo.resolve()


# ---- the brief --------------------------------------------------------------


def test_the_brief_is_the_last_argument_to_the_agent(run, agent):
    run("1")
    args = json.loads(agent.read_text())["args"]
    assert "claim" in args[-1].lower()


def test_extra_arguments_are_passed_through_ahead_of_the_brief(run, agent):
    run("1", "--model", "opus")
    args = json.loads(agent.read_text())["args"]
    assert args[:2] == ["--model", "opus"]
    assert len(args) == 3


def test_every_seat_gets_the_same_brief_but_its_own_number(run):
    """Identical by design: the moment one seat is told something another is
    not, there is a dispatcher again."""
    one = run("1", "--dry-run").stdout
    two = run("2", "--dry-run").stdout
    assert one.replace("seat 1", "seat N").replace("seat-1", "seat-N") == two.replace(
        "seat 2", "seat N"
    ).replace("seat-2", "seat-N")


def test_the_brief_tells_the_seat_to_claim_before_it_works(run):
    """The whole point of self-selection: an advisory claim after the fact
    cannot stop two seats doing one item."""
    out = run("1", "--dry-run").stdout.lower()
    assert "atomically" in out
    assert "kind='work'" in out


def test_the_brief_can_be_replaced_wholesale(run, agent):
    run("1", env={"QB_SEAT_BRIEF": "do the other thing"})
    assert json.loads(agent.read_text())["args"] == ["do the other thing"]


# ---- --dry-run --------------------------------------------------------------


def test_dry_run_starts_nothing(run, agent):
    assert run("1", "--dry-run").returncode == 0
    assert not agent.exists()


def test_dry_run_is_honoured_anywhere_in_the_arguments(run, agent):
    """Everything after the seat number is passed through verbatim, so a
    misplaced one would otherwise start the very agent it was meant to stop."""
    assert run("1", "--model", "opus", "--dry-run").returncode == 0
    assert not agent.exists()


def test_dry_run_shows_the_command_it_would_run(run):
    out = run("1", "--dry-run", "--model", "opus").stdout
    assert "command:  claude --model opus <brief>" in out


# ---- registering the name with the board ------------------------------------


@pytest.fixture
def board(fake_bin, tmp_path):
    """A fake curl that records the request and answers with a chosen identity."""
    calls = tmp_path / "curl.args"

    def _board(agent_identity="zeus/seat-1"):
        fake_bin(
            "curl",
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$@" >> {calls}\n'
            f"printf '%s' '{{\"agent\":\"{agent_identity}\",\"machine\":\"zeus\"}}'\n",
        )
        return calls

    _board.calls = calls
    return _board


def _sent(calls):
    return calls.read_text() if calls.exists() else ""


def test_no_board_configured_means_no_call_and_no_complaint(run, board, agent):
    """A seat that refused to start because a cosmetic name could not be
    reserved would cost more than the name is worth."""
    calls = board()
    result = run("1")
    assert result.returncode == 0
    assert result.stderr == ""
    assert _sent(calls) == ""


def test_the_name_is_requested_at_first_contact(run, board, agent):
    """The board designates names on FIRST contact and ignores a request from
    anything that arrives after. The lifecycle hook gets there first (it fires
    on SessionStart and sends the instance as a bare key), so a seat that does
    not ask up front comes up as two random words instead of seat-3."""
    calls = board("zeus/seat-3")
    result = run(
        "3",
        env={"QUARTERBACK_BASE_URL": "https://board.example", "QUARTERBACK_TOKEN": "t0ken"},
    )
    assert result.returncode == 0
    sent = _sent(calls)
    assert "X-Agent-Key: seat-3" in sent
    assert "X-Agent-Name: seat-3" in sent
    assert "Authorization: Bearer t0ken" in sent
    assert "https://board.example/whoami" in sent


def test_a_trailing_slash_on_the_base_url_does_not_double(run, board, agent):
    calls = board()
    run(
        "1",
        env={"QUARTERBACK_BASE_URL": "https://board.example/", "QUARTERBACK_TOKEN": "t"},
    )
    assert "https://board.example/whoami" in _sent(calls)


def test_the_token_command_is_used_when_no_token_is_set(run, board, agent):
    calls = board()
    run(
        "1",
        env={
            "QUARTERBACK_BASE_URL": "https://board.example",
            "QUARTERBACK_TOKEN_CMD": "printf 'from-cmd\\nignored-second-line'",
        },
    )
    assert "Authorization: Bearer from-cmd" in _sent(calls)


def test_a_board_configured_only_in_the_config_file_is_still_used(run, board, agent, tmp_path):
    """Environment first, then the per-host file — the contract the fleet's
    wrappers already share. A seat is started by a layout, not by a login shell,
    so the file is usually the only place the board is named."""
    config = tmp_path / "config"
    config.write_text(
        'QUARTERBACK_BASE_URL=https://from-file.example\nQUARTERBACK_TOKEN_CMD="printf tok"\n'
    )
    calls = board()
    run("1", env={"QUARTERBACK_CONFIG": str(config)})
    assert "https://from-file.example/whoami" in _sent(calls)


def test_the_config_file_cannot_overwrite_a_board_named_in_the_environment(
    run, board, agent, tmp_path
):
    """Sourcing sets everything the file names, not only what was missing. The
    fleet spans deliberately disjoint boards, so a base URL quietly replaced by a
    config file is how a seat posts to the wrong island."""
    config = tmp_path / "config"
    config.write_text('QUARTERBACK_BASE_URL=https://wrong-island.example\nQUARTERBACK_TOKEN_CMD="printf t"\n')
    calls = board()
    run(
        "1",
        env={"QUARTERBACK_CONFIG": str(config), "QUARTERBACK_BASE_URL": "https://right.example"},
    )
    sent = _sent(calls)
    assert "https://right.example/whoami" in sent
    assert "wrong-island" not in sent


def test_a_token_command_in_the_environment_survives_the_config_file(run, board, agent, tmp_path):
    """Same trap, one variable along: the URL from the environment with the
    credential from the file is still the wrong pair."""
    config = tmp_path / "config"
    config.write_text('QUARTERBACK_TOKEN_CMD="printf from-file"\n')
    calls = board()
    run(
        "1",
        env={
            "QUARTERBACK_CONFIG": str(config),
            "QUARTERBACK_BASE_URL": "https://board.example",
            "QUARTERBACK_TOKEN_CMD": "printf from-env",
        },
    )
    assert "Authorization: Bearer from-env" in _sent(calls)


def test_a_token_in_the_environment_survives_the_config_file(run, board, agent, tmp_path):
    config = tmp_path / "config"
    config.write_text('QUARTERBACK_BASE_URL=https://board.example\nQUARTERBACK_TOKEN=from-file\n')
    calls = board()
    run("1", env={"QUARTERBACK_CONFIG": str(config), "QUARTERBACK_TOKEN": "from-env"})
    assert "Authorization: Bearer from-env" in _sent(calls)


def test_no_token_anywhere_means_no_call(run, board, agent):
    calls = board()
    result = run("1", env={"QUARTERBACK_BASE_URL": "https://board.example"})
    assert result.returncode == 0
    assert _sent(calls) == ""


def test_a_name_the_board_would_not_give_us_is_reported(run, board, agent):
    """Two panes started as the same seat is the way this happens, and it is
    invisible otherwise: both would work, and both would look like seat 1."""
    board("zeus/glacier-mist")
    result = run(
        "1", env={"QUARTERBACK_BASE_URL": "https://board.example", "QUARTERBACK_TOKEN": "t"}
    )
    assert result.returncode == 0
    assert "zeus/glacier-mist" in result.stderr


def test_the_expected_name_is_reported_silently(run, board, agent):
    board("zeus/seat-1")
    result = run(
        "1", env={"QUARTERBACK_BASE_URL": "https://board.example", "QUARTERBACK_TOKEN": "t"}
    )
    assert result.stderr == ""


def test_a_board_that_is_down_does_not_stop_the_seat(run, fake_bin, agent):
    """No network is a normal state for a laptop and must not cost a seat."""
    fake_bin("curl", "#!/usr/bin/env bash\nexit 7\n")
    result = run(
        "2", env={"QUARTERBACK_BASE_URL": "https://board.example", "QUARTERBACK_TOKEN": "t"}
    )
    assert result.returncode == 0
    assert json.loads(agent.read_text())["instance"] == "seat-2"


def test_a_reply_that_is_not_json_does_not_stop_the_seat(run, fake_bin, agent):
    """An html error page from a reverse proxy is the realistic version of this."""
    fake_bin("curl", "#!/usr/bin/env bash\nprintf '<html>502</html>'\n")
    result = run(
        "1", env={"QUARTERBACK_BASE_URL": "https://board.example", "QUARTERBACK_TOKEN": "t"}
    )
    assert result.returncode == 0
    assert result.stderr == ""
    assert agent.exists()


# ---- one pane per seat ------------------------------------------------------
#
# The board cannot catch this one: two panes on the same seat number send the
# same key, so it hands them one identity by construction and they are
# indistinguishable from its side. They also share the ask-poll cursor, which is
# the bug the per-seat instance exists to prevent, arriving one level down.


def test_a_seat_already_running_here_is_refused(run, agent, runtime_dir):
    live = subprocess.Popen(["sleep", "30"])
    try:
        (runtime_dir / "qb-seat-1.pid").write_text(str(live.pid))
        result = run("1", env={"XDG_RUNTIME_DIR": str(runtime_dir)})
        assert result.returncode == 3
        assert "already running" in result.stderr
        assert not agent.exists()
    finally:
        live.kill()
        live.wait()


def test_a_seat_running_elsewhere_does_not_block_this_one(run, agent, runtime_dir):
    live = subprocess.Popen(["sleep", "30"])
    try:
        (runtime_dir / "qb-seat-1.pid").write_text(str(live.pid))
        assert run("2", env={"XDG_RUNTIME_DIR": str(runtime_dir)}).returncode == 0
    finally:
        live.kill()
        live.wait()


def test_a_marker_left_by_a_seat_that_ended_is_taken_over(run, agent, runtime_dir):
    """A seat that dies leaves its marker behind — nothing cleans it up, and a
    pane that could never be restarted would be worse than the collision."""
    dead = subprocess.Popen(["true"])
    dead.wait()
    (runtime_dir / "qb-seat-1.pid").write_text(str(dead.pid))
    assert run("1", env={"XDG_RUNTIME_DIR": str(runtime_dir)}).returncode == 0


def test_a_corrupt_marker_does_not_block_the_seat(run, agent, runtime_dir):
    (runtime_dir / "qb-seat-1.pid").write_text("not-a-pid")
    assert run("1", env={"XDG_RUNTIME_DIR": str(runtime_dir)}).returncode == 0


def test_the_refusal_can_be_overridden(run, agent, runtime_dir):
    """For a marker whose pid has since been reused by something unrelated."""
    live = subprocess.Popen(["sleep", "30"])
    try:
        (runtime_dir / "qb-seat-1.pid").write_text(str(live.pid))
        result = run("1", env={"XDG_RUNTIME_DIR": str(runtime_dir), "QB_SEAT_FORCE": "1"})
        assert result.returncode == 0
    finally:
        live.kill()
        live.wait()


def test_starting_a_seat_records_the_pane_it_is_in(run, agent, runtime_dir):
    run("5", env={"XDG_RUNTIME_DIR": str(runtime_dir)})
    marker = runtime_dir / "qb-seat-5.pid"
    assert marker.read_text().isdigit()


def test_a_dry_run_is_told_about_a_collision_but_leaves_no_marker(run, agent, runtime_dir):
    live = subprocess.Popen(["sleep", "30"])
    try:
        (runtime_dir / "qb-seat-1.pid").write_text(str(live.pid))
        assert run("1", "--dry-run", env={"XDG_RUNTIME_DIR": str(runtime_dir)}).returncode == 3
    finally:
        live.kill()
        live.wait()
    run("2", "--dry-run", env={"XDG_RUNTIME_DIR": str(runtime_dir)})
    assert not (runtime_dir / "qb-seat-2.pid").exists()


def test_the_registration_is_skipped_for_a_dry_run(run, board):
    """--dry-run exists to be safe to run; it must not claim a name either."""
    calls = board()
    run(
        "1",
        "--dry-run",
        env={"QUARTERBACK_BASE_URL": "https://board.example", "QUARTERBACK_TOKEN": "t"},
    )
    assert _sent(calls) == ""
