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
import re
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

QB_SEAT = Path(__file__).resolve().parent.parent / "bin" / "qb-seat"

#: The scope every seat in this suite gets. A seat is named after the PROJECT it
#: sits in as well as its number (#208), and the `repo` fixture below is
#: `tmp_path/"repo"` — so a seat here is `seat-repo-3`, with a pane marker to
#: match. Spelled once, because the scope appears in about thirty assertions and
#: in every one of them the subject is the number.
SCOPE = "repo"


def seat_name(n):
    """What seat `n` calls itself on the board when it works in the `repo` fixture."""
    return f"seat-{SCOPE}-{n}"


def marker_name(n):
    """The pane marker for seat `n`, which has to agree with the board name."""
    return f"qb-{seat_name(n)}.pid"


@pytest.fixture
def repo(tmp_path):
    """A real git repository — qb-seat refuses anything else."""
    d = tmp_path / "repo"
    d.mkdir()
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    return d


#: The only shebang a stub written at runtime may carry. `/bin/sh` is the one interpreter a nix
#: build sandbox is guaranteed to have at an absolute path: there is no `/usr/bin/env` there and
#: no `/bin/bash` either, and `patchShebangs` rewrites the scripts shipped in `harness/bin` at
#: build time but cannot reach a file a test writes while it runs.
_SANDBOX_SAFE_SHEBANG = "#!/bin/sh\n"


@pytest.fixture
def fake_bin(tmp_path):
    """A PATH entry we own, for standing in the agent and curl.

    Stubs installed here get `#!/bin/sh`, never `#!/usr/bin/env bash`: there is no
    /usr/bin/env inside the nix build sandbox, and a stub that cannot exec makes
    every test here fail for a reason that has nothing to do with the code under
    test. The shipped scripts dodge this via patchShebangs; a stub written at
    runtime cannot.

    Asserted rather than only written down, because writing it down is what was already
    true. Until the assertion below existed, nothing in this suite failed on a developer's
    machine when a stub named /usr/bin/env — the file is there, every test here passed, and
    the only reader was a `nix build .#checks.<system>.worktree-tests` that no CI job runs
    (#179). That is how the same mistake reached a third suite in a week (#177). The four
    call sites below all come through here, so here is where it costs nothing to check.
    Note the present tense is now the other way round: a stub named /usr/bin/env fails here,
    locally, immediately — that is the whole point, and it is what the guard changed.
    """
    d = tmp_path / "bin"
    d.mkdir()

    def _install(name, body):
        assert body.startswith(_SANDBOX_SAFE_SHEBANG), (
            f"the `{name}` stub begins {body.partition(chr(10))[0]!r}, and this fixture installs "
            f"only stubs a nix build sandbox can exec — {_SANDBOX_SAFE_SHEBANG.strip()!r}. "
            "Nothing here will fail on your machine if you change that: /usr/bin/env exists "
            "locally, so the suite stays green and only the worktree-tests nix check goes red, "
            "43 tests at a time, every one of them blaming the code under test for a `bad "
            "interpreter` in a stderr nobody reads. Keep the body POSIX — the stubs here need "
            "nothing bash has"
        )
        path = d / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    _install.dir = d
    return _install


def _fake_agent_body(record):
    """The body of a fake agent that records how it was started, then exits 0.

    It records its OWN pid, because that is an invariant and not a detail: qb-seat
    execs, so the pid in the pane marker is the pid of the process that became the
    agent. A refactor that backgrounded the agent instead would still write a
    marker, still hold a plausible number, and still be wrong.
    """
    return (
        "#!/bin/sh\n"
        "export FAKE_AGENT_PID=$$\n"
        'python3 -c "\n'
        "import json, os, sys\n"
        f"json.dump({{'args': sys.argv[1:], 'cwd': os.getcwd(),\n"
        "  'pid': int(os.environ['FAKE_AGENT_PID']),\n"
        "  'leaked': os.environ.get('QB_SEAT_LEAKED'),\n"
        "  'instance': os.environ.get('QUARTERBACK_INSTANCE')},\n"
        f"  open({str(record)!r}, 'w'))\n"
        '" "$@"\n'
    )


@pytest.fixture
def agent(fake_bin, tmp_path):
    """A fake agent binary that records how it was started, then exits 0."""
    record = tmp_path / "agent.json"
    fake_bin("claude", _fake_agent_body(record))
    return record


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

    def _run(*args, env=None, cwd=None, unset=()):
        environ = {**os.environ, "PATH": f"{fake_bin.dir}:{os.environ['PATH']}"}
        for leaked in (
            "QUARTERBACK_BASE_URL",
            "QUARTERBACK_TOKEN",
            "QUARTERBACK_TOKEN_CMD",
            "QUARTERBACK_INSTANCE",
            "QB_SEAT_REPO",
            "QB_SEAT_BRIEF",
            "QB_SEAT_AGENT",
            "QB_SEAT_YOLO",
            "QB_SEAT_LEAKED",
        ):
            environ.pop(leaked, None)
        # Never the developer's own config: a test that reads it would talk to
        # the real board on the machine it is running on.
        environ["QUARTERBACK_CONFIG"] = str(tmp_path / "no-such-config")
        environ["XDG_RUNTIME_DIR"] = str(runtime_dir)
        environ.pop("QB_SEAT_FORCE", None)
        # Pinned OFF for the fixture, not popped: seats are yolo by default, so a
        # test about briefs or pass-through would otherwise be asserting the
        # default's argv as a side effect and would go red the day it changes.
        # The tests that are ABOUT the default set it themselves.
        environ["QB_SEAT_YOLO"] = "0"
        # OFF for the fixture, and this one is not a convenience. Left alone,
        # every test here would run the real `qb-pace` beside the script under
        # test, which reads the DEVELOPER'S OWN subscription out of ~/.claude and
        # calls the usage endpoint over the network — sixty times a run, on
        # figures that decide nothing about the code under test and could turn a
        # suite red because somebody spent a window that afternoon. A test that
        # reaches the network is its own defect. The tests that are ABOUT the gate
        # set the knob themselves and put a stub qb-pace on PATH ahead of it.
        environ["QB_SEAT_PACE"] = "off"
        environ.update(env or {})
        for gone in unset:
            environ.pop(gone, None)
        return subprocess.run(
            [str(QB_SEAT), *args],
            env=environ,
            cwd=str(cwd or repo),
            capture_output=True,
            text=True,
        )

    return _run


# ---- the fixtures' own stubs ------------------------------------------------


def test_a_stub_the_sandbox_could_not_exec_is_refused(fake_bin):
    """The regression test this fix would otherwise not have.

    Before the `fake_bin` assertion existed, reverting any of the four `#!/bin/sh` stubs to
    `#!/usr/bin/env bash` left every test in this file green, because the shebang is only
    wrong somewhere nothing here runs. No count is quoted: it would age with the next test
    added and would not make the guard any stronger. So the guard is what a local
    `pytest harness/tests` can catch the fourth instance with, and this is what proves the
    guard is not decoration. A missing shebang is refused for the same
    reason it would be a bug: what exec'ing it does then depends on the caller's shell."""
    for body in ("#!/usr/bin/env bash\nexit 0\n", "#!/bin/bash\nexit 0\n", "exit 0\n"):
        with pytest.raises(AssertionError, match="nix build sandbox can exec"):
            fake_bin("curl", body)


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


def test_a_bare_repository_is_refused(run, tmp_path):
    """`rev-parse --git-dir` succeeds in a bare repo, and a seat started in one
    has nowhere to edit a file — which fails much later and much less clearly."""
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    result = run("1", "--dry-run", cwd=bare)
    assert result.returncode == 2
    assert "bare git repository" in result.stderr


def test_a_working_directory_that_does_not_exist_is_refused(run, tmp_path):
    result = run("1", "--dry-run", env={"QB_SEAT_REPO": str(tmp_path / "gone")})
    assert result.returncode == 2
    assert "not a directory" in result.stderr


# ---- the identity -----------------------------------------------------------


def test_the_instance_is_per_seat(run):
    """One value for the whole box gives n seats one ask cursor between them —
    whichever polls first swallows the rest's mail."""
    assert f"instance: {seat_name(3)}" in run("3", "--dry-run").stdout


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
    assert json.loads(agent.read_text())["instance"] == seat_name(4)


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
    assert len(args) == 4  # …plus the -- terminator and the brief


def test_an_argument_with_a_space_in_it_arrives_as_one_argument(run, agent):
    """Word-splitting a pass-through is silent and total: the agent gets two
    arguments it half-recognises rather than the one it was handed."""
    run("1", "--append-system-prompt", "be terse", "--add-dir", "/tmp/a b")
    args = json.loads(agent.read_text())["args"]
    assert args[:4] == ["--append-system-prompt", "be terse", "--add-dir", "/tmp/a b"]


def test_arguments_after_a_double_dash_are_not_read_as_ours(run, agent):
    """--dry-run and --help are harvested from anywhere, which leaves no way to
    pass either word ON to the agent. `--` is that way."""
    result = run("1", "--", "--dry-run", "--help")
    assert result.returncode == 0
    args = json.loads(agent.read_text())["args"]
    assert args[:2] == ["--dry-run", "--help"]


def test_every_seat_gets_the_same_brief_but_its_own_number(run):
    """Identical by design: the moment one seat is told something another is
    not, there is a dispatcher again."""
    one = run("1", "--dry-run").stdout
    two = run("2", "--dry-run").stdout
    assert one.replace("seat 1", "seat N").replace(seat_name(1), "seat-N") == two.replace(
        "seat 2", "seat N"
    ).replace(seat_name(2), "seat-N")


def test_the_brief_tells_the_seat_to_claim_before_it_works(run):
    """The whole point of self-selection: an advisory claim after the fact
    cannot stop two seats doing one item."""
    out = run("1", "--dry-run").stdout.lower()
    assert "atomically" in out
    assert "kind='work'" in out


def test_the_brief_can_be_replaced_wholesale(run, agent):
    run("1", env={"QB_SEAT_BRIEF": "do the other thing"})
    assert json.loads(agent.read_text())["args"] == ["--", "do the other thing"]


def test_a_brief_that_starts_with_a_dash_is_still_a_brief(run, agent):
    """It is a wholesale override, and `--help` is about the first thing anybody
    overrides it with while trying the thing out."""
    run("1", env={"QB_SEAT_BRIEF": "--help"})
    args = json.loads(agent.read_text())["args"]
    assert args == ["--", "--help"]


def test_an_empty_brief_means_no_brief_at_all(run, agent):
    """`:-` cannot tell "unset" from "set to nothing", so a layout that wants a
    pane to come up waiting rather than working got the full built-in brief —
    the one value a wholesale override could not express."""
    run("1", env={"QB_SEAT_BRIEF": ""})
    assert json.loads(agent.read_text())["args"] == []


def test_qb_seat_claude_chooses_the_agent(run, agent, fake_bin, tmp_path):
    """The knob exists so a fleet can start something that is not on PATH as
    `claude`; getting it wrong starts the wrong binary rather than failing."""
    other_record = tmp_path / "other-agent.json"
    fake_bin("other-agent", _fake_agent_body(other_record))
    assert run("1", env={"QB_SEAT_AGENT": "other-agent"}).returncode == 0
    assert not agent.exists()
    assert json.loads(other_record.read_text())["instance"] == seat_name(1)


# ---- yolo -------------------------------------------------------------------


def test_a_seat_starts_without_permission_prompts_by_default(run, agent):
    """The failure this removes does not look like a failure: a pane nobody is
    watching stops on the first permission it does not hold, and the board still
    shows a live agent holding a claim while nothing moves. A seat that stops to
    ask is not being careful, it is stuck — there is no operator to ask."""
    run("1", unset=("QB_SEAT_YOLO",))
    assert json.loads(agent.read_text())["args"][0] == "--dangerously-skip-permissions"


def test_an_empty_value_is_not_an_answer(run, agent):
    """Unset and empty both mean "not answered", and the answer to an unanswered
    question is the default."""
    run("1", env={"QB_SEAT_YOLO": ""})
    assert json.loads(agent.read_text())["args"][0] == "--dangerously-skip-permissions"


def test_prompts_come_back_when_they_are_asked_for(run, agent):
    """The opt-out, which is the whole basis on which the default is defensible."""
    run("1", env={"QB_SEAT_YOLO": "0"})
    assert "--dangerously-skip-permissions" not in json.loads(agent.read_text())["args"]


def test_only_a_falsy_value_turns_it_off(run, agent):
    """The mirror of QB_SEAT_FORCE's rule: a value a caller passes must mean what
    it says, so a stray one does not quietly disable the thing it names."""
    for value in ("no", "false", "OFF"):
        run("1", env={"QB_SEAT_YOLO": value})
        assert "--dangerously-skip-permissions" not in json.loads(agent.read_text())["args"]
    for value in ("1", "yes", "anything-else"):
        run("1", env={"QB_SEAT_YOLO": value})
        assert json.loads(agent.read_text())["args"][0] == "--dangerously-skip-permissions"


def test_yolo_comes_before_the_caller_s_own_arguments(run, agent):
    """Prepended, so an argument passed for this seat is the later one and wins
    anything the agent resolves last-one-wins."""
    run("1", "--model", "opus", unset=("QB_SEAT_YOLO",))
    args = json.loads(agent.read_text())["args"]
    assert args[:3] == ["--dangerously-skip-permissions", "--model", "opus"]


def test_a_dry_run_shows_the_yolo_flag_it_would_pass(run):
    """A dry run is what you use to check what n panes are about to do; showing
    an argv that is missing the one argument that changes the risk is worse than
    showing nothing."""
    out = run("1", "--dry-run", unset=("QB_SEAT_YOLO",)).stdout
    assert "claude --dangerously-skip-permissions" in out


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
    assert "command:  claude --model opus -- <brief>" in out


def test_dry_run_with_no_arguments_shows_no_argument(run):
    """`printf ' %q' "$@"` with nothing to print still applies the format once,
    with the missing argument as the empty string — so the dry run showed a
    `claude ''` that it would never really pass."""
    out = run("1", "--dry-run").stdout
    assert "command:  claude -- <brief>" in out
    assert "''" not in out


def test_help_is_honoured_after_the_seat_number(run, agent):
    """--dry-run is harvested from anywhere because a misplaced one would
    otherwise start the very agent it was meant to stop. `qb-seat 1 --help`
    started one too."""
    result = run("1", "--help")
    assert result.returncode == 0
    assert "qb-seat <n>" in result.stdout
    assert not agent.exists()


def test_the_usage_text_is_found_by_marker_and_not_by_line_number(run):
    """Addressed as `sed -n '4,15p'`, --help printed whatever happened to be on
    those twelve lines: the first paragraph added to the top of the file breaks
    it silently, and nothing anywhere fails."""
    out = run("--help").stdout
    for expected in (
        "qb-seat <n> [agent args…]",
        "qb-seat <n> --dry-run",
        "qb-seat <n> -- [args…]",
        "qb-seat -h|--help",
        "Exit codes",
    ):
        assert expected in out
    assert ":usage" not in out
    assert "WHAT THIS IS FOR" not in out


# ---- registering the name with the board ------------------------------------


@pytest.fixture
def board(fake_bin, tmp_path):
    """A fake curl that records the request and answers with a chosen identity.

    It emulates the two parts of the call qb-seat actually depends on: the config
    fed in on stdin, which is where the credential goes and where it has to stay,
    and the ``--write-out`` status code appended to the body.
    """
    calls = tmp_path / "curl.args"
    stdin = tmp_path / "curl.stdin"

    def _board(agent_identity=None, status="200", body=None):
        agent_identity = agent_identity or f"zeus/{seat_name(1)}"
        if body is None:
            body = f'{{"agent":"{agent_identity}","machine":"zeus"}}'
        fake_bin(
            "curl",
            "#!/bin/sh\n"
            f'printf "%s\\n" "$@" >> {calls}\n'
            f"cat >> {stdin}\n"
            f"printf '%s' {shlex.quote(body)}\n"
            f"printf '\\n%s' {shlex.quote(status)}\n",
        )
        return calls

    _board.calls = calls
    _board.stdin = stdin
    return _board


def _sent(calls):
    return calls.read_text() if calls.exists() else ""


def _fed(board):
    """What went in on curl's stdin — the config carrying the credential."""
    return board.stdin.read_text() if board.stdin.exists() else ""


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
    not ask up front comes up as two random words instead of seat-repo-3."""
    calls = board(f"zeus/{seat_name(3)}")
    result = run(
        "3",
        env={"QUARTERBACK_BASE_URL": "https://board.example", "QUARTERBACK_TOKEN": "t0ken"},
    )
    assert result.returncode == 0
    sent = _sent(calls)
    assert f"X-Agent-Key: {seat_name(3)}" in sent
    assert f"X-Agent-Name: {seat_name(3)}" in sent
    assert "https://board.example/whoami" in sent
    assert "Authorization: Bearer t0ken" in _fed(board)


def test_the_token_is_never_put_on_the_command_line(run, board, agent):
    """Every argument a process is started with is readable by any local process
    for the life of the call, in ps and in /proc/<pid>/cmdline."""
    calls = board()
    run(
        "1",
        env={"QUARTERBACK_BASE_URL": "https://board.example", "QUARTERBACK_TOKEN": "s3cret"},
    )
    assert "s3cret" not in _sent(calls)
    assert "Authorization: Bearer s3cret" in _fed(board)


def test_a_trailing_slash_on_the_base_url_does_not_double(run, board, agent):
    calls = board()
    run(
        "1",
        env={"QUARTERBACK_BASE_URL": "https://board.example/", "QUARTERBACK_TOKEN": "t"},
    )
    assert "https://board.example/whoami" in _sent(calls)


def test_the_token_command_is_used_when_no_token_is_set(run, board, agent):
    board()
    run(
        "1",
        env={
            "QUARTERBACK_BASE_URL": "https://board.example",
            "QUARTERBACK_TOKEN_CMD": "printf 'from-cmd\\nignored-second-line'",
        },
    )
    assert "Authorization: Bearer from-cmd" in _fed(board)


def test_a_token_command_that_prints_crlf_does_not_break_the_header(run, board, agent):
    """A secret manager reached across a Windows-shaped boundary prints CRLF, and
    a bare \\r left on the end goes into the middle of an Authorization header."""
    board()
    run(
        "1",
        env={
            "QUARTERBACK_BASE_URL": "https://board.example",
            "QUARTERBACK_TOKEN_CMD": "printf 'from-cmd\\r\\nsecond'",
        },
    )
    fed = _fed(board)
    assert "Authorization: Bearer from-cmd" in fed
    assert "\r" not in fed


def test_a_token_command_that_hangs_does_not_hold_up_the_seat(run, board, agent):
    """A token command is a secret manager as often as not, and one that decides
    to prompt holds up the seat, not just the cosmetic name it was asked for."""
    board()
    started = time.monotonic()
    result = run(
        "1",
        env={
            "QUARTERBACK_BASE_URL": "https://board.example",
            "QUARTERBACK_TOKEN_CMD": "sleep 60",
        },
    )
    assert result.returncode == 0
    assert time.monotonic() - started < 30
    assert agent.exists()


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
    config.write_text(
        'QUARTERBACK_BASE_URL=https://wrong-island.example\nQUARTERBACK_TOKEN_CMD="printf t"\n'
    )
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
    board()
    run(
        "1",
        env={
            "QUARTERBACK_CONFIG": str(config),
            "QUARTERBACK_BASE_URL": "https://board.example",
            "QUARTERBACK_TOKEN_CMD": "printf from-env",
        },
    )
    assert "Authorization: Bearer from-env" in _fed(board)


def test_a_token_in_the_environment_survives_the_config_file(run, board, agent, tmp_path):
    config = tmp_path / "config"
    config.write_text("QUARTERBACK_BASE_URL=https://board.example\nQUARTERBACK_TOKEN=from-file\n")
    board()
    run("1", env={"QUARTERBACK_CONFIG": str(config), "QUARTERBACK_TOKEN": "from-env"})
    assert "Authorization: Bearer from-env" in _fed(board)


def test_a_token_command_in_the_environment_beats_a_static_token_in_the_file(
    run, board, agent, tmp_path
):
    """The credential is taken as a set, not variable by variable. With a token
    COMMAND in the environment there is no static token to compare against, so
    best-of-each quietly authenticates one island's board with the other's
    credential — the mix-up the per-host config exists to prevent."""
    config = tmp_path / "config"
    config.write_text(
        "QUARTERBACK_BASE_URL=https://from-file.example\nQUARTERBACK_TOKEN=from-file\n"
    )
    board()
    run(
        "1",
        env={
            "QUARTERBACK_CONFIG": str(config),
            "QUARTERBACK_TOKEN_CMD": "printf from-env-cmd",
        },
    )
    fed = _fed(board)
    assert "Authorization: Bearer from-env-cmd" in fed
    assert "from-file" not in fed


def test_a_config_file_that_errors_is_not_read_as_an_absent_one(run, board, agent, tmp_path):
    """`. config || true` leaves whatever the file set before the error in play
    and starts an unregistered seat in silence — two things wrong at once, and
    the silence is the one that costs an afternoon."""
    config = tmp_path / "config"
    config.write_text(
        "QUARTERBACK_BASE_URL=https://from-file.example\n"
        'QUARTERBACK_TOKEN="unterminated\n'
        "QUARTERBACK_TOKEN=never-reached\n"
    )
    calls = board()
    result = run("1", env={"QUARTERBACK_CONFIG": str(config)})
    assert result.returncode == 0
    assert "could not be read" in result.stderr
    assert _sent(calls) == ""
    assert agent.exists()


def test_the_config_file_cannot_reach_the_agent_or_rename_the_seat(run, agent, tmp_path):
    """A config file is an unrestricted shell script, and sourcing it in this
    shell means everything it sets it keeps: the instance the marker and the
    dry-run output still claim, and anything it exports, straight into the
    agent."""
    config = tmp_path / "config"
    config.write_text("QUARTERBACK_INSTANCE=hijacked\nexport QB_SEAT_LEAKED=yes\n")
    assert run("1", env={"QUARTERBACK_CONFIG": str(config)}).returncode == 0
    started = json.loads(agent.read_text())
    assert started["instance"] == seat_name(1)
    assert started["leaked"] is None


def test_no_home_does_not_stop_the_seat(run, agent, tmp_path):
    """A container, a systemd unit with no User=, `env -i`: any of them can leave
    HOME unset, and merely spelling the default config path under `set -u` would
    then end a seat over a registration documented as best-effort."""
    result = run("1", unset=("HOME", "QUARTERBACK_CONFIG", "XDG_CONFIG_HOME"))
    assert result.returncode == 0
    assert json.loads(agent.read_text())["instance"] == seat_name(1)


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
    board(f"zeus/{seat_name(1)}")
    result = run(
        "1", env={"QUARTERBACK_BASE_URL": "https://board.example", "QUARTERBACK_TOKEN": "t"}
    )
    assert result.stderr == ""


def test_a_name_with_a_json_escape_in_it_is_not_warned_about(run, board, agent):
    """The reply is read with sed, which does not decode JSON:
    `zeus\\u002fseat-repo-1` IS zeus/seat-repo-1, and comparing the undecoded form
    warns about our own parser rather than about the name."""
    board(body=r'{"agent":"zeus\u002fseat-repo-1","machine":"zeus"}')
    result = run(
        "1", env={"QUARTERBACK_BASE_URL": "https://board.example", "QUARTERBACK_TOKEN": "t"}
    )
    assert result.returncode == 0
    assert result.stderr == ""


def test_a_board_that_answers_with_a_bare_name_is_not_warned_about(run, board, agent):
    """Matched as `!= */seat-…`, a reply with no slash in it warns every seat on
    every start — and three lines nobody needs on every start is how an operator
    learns to skip the one message here that means something."""
    board(seat_name(1))
    result = run(
        "1", env={"QUARTERBACK_BASE_URL": "https://board.example", "QUARTERBACK_TOKEN": "t"}
    )
    assert result.returncode == 0
    assert result.stderr == ""


def test_a_board_that_is_down_does_not_stop_the_seat(run, fake_bin, agent):
    """No network is a normal state for a laptop and must not cost a seat."""
    fake_bin("curl", "#!/bin/sh\nexit 7\n")
    result = run(
        "2", env={"QUARTERBACK_BASE_URL": "https://board.example", "QUARTERBACK_TOKEN": "t"}
    )
    assert result.returncode == 0
    assert "did not answer" in result.stderr
    assert json.loads(agent.read_text())["instance"] == seat_name(2)


def test_a_board_that_refuses_the_token_says_so(run, board, agent):
    """A revoked token, or the other island's token: the seat still starts, but
    the lifecycle hooks are about to be refused the same way for the rest of the
    session, quietly, and this is the one place anybody is looking."""
    board(status="401", body='{"detail":"unauthorized"}')
    result = run(
        "1", env={"QUARTERBACK_BASE_URL": "https://board.example", "QUARTERBACK_TOKEN": "stale"}
    )
    assert result.returncode == 0
    assert "401" in result.stderr
    assert agent.exists()


def test_a_reply_that_is_not_json_does_not_stop_the_seat(run, board, agent):
    """An html error page from a reverse proxy is the realistic version of this,
    and it arrives with a status code that says more than the body does."""
    board(status="502", body="<html>502 Bad Gateway</html>")
    result = run(
        "1", env={"QUARTERBACK_BASE_URL": "https://board.example", "QUARTERBACK_TOKEN": "t"}
    )
    assert result.returncode == 0
    assert "502" in result.stderr
    assert agent.exists()


def test_a_success_with_no_name_in_it_is_passed_over_in_silence(run, board, agent):
    """A 200 whose body has no agent field is a board we do not understand, not a
    name we disagree with."""
    board(status="200", body='{"machine":"zeus"}')
    result = run(
        "1", env={"QUARTERBACK_BASE_URL": "https://board.example", "QUARTERBACK_TOKEN": "t"}
    )
    assert result.returncode == 0
    assert result.stderr == ""
    assert agent.exists()


# ---- the scope: which project a seat belongs to ------------------------------
#
# #208. `seat-3` alone makes the NAMESPACE the machine while the NUMBERING is per
# screen — `qb-seats` numbers from 1 every time it builds one — so the second
# screen on a box could not start a single seat. These are the tests for the half
# of the name that fixes it, and the ones that matter most are the two either side
# of the line: two projects each holding a seat 1, and one project refusing to hold
# it twice.


@pytest.fixture
def other_repo(tmp_path):
    """A second git repository, standing in for the second screen's project."""
    d = tmp_path / "elsewhere"
    d.mkdir()
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    return d


def test_two_projects_can_each_hold_the_same_seat_number(run, agent, other_repo,
                                                         runtime_dir):
    """The bug, reduced to two calls (#208).

    One screen per project is the obvious way to work, and `qb-seats` numbers
    every screen's seats from 1 — so before the scope existed this was not an
    unlucky collision but the guaranteed outcome of starting a second screen.
    """
    live = subprocess.Popen(["sleep", "30"])
    try:
        (runtime_dir / marker_name(1)).write_text(str(live.pid))
        result = run("1", cwd=other_repo)
        assert result.returncode == 0, result.stderr
        assert json.loads(agent.read_text())["instance"] == "seat-elsewhere-1"
    finally:
        live.kill()
        live.wait()


def test_one_project_still_refuses_the_same_seat_twice(run, other_repo, runtime_dir):
    """The other half, and the reason the guard was never the thing to remove:
    two panes on ONE seat share a board identity and an ask cursor, and one of
    them silently eats the other's mail."""
    live = subprocess.Popen(["sleep", "30"])
    try:
        (runtime_dir / "qb-seat-elsewhere-1.pid").write_text(str(live.pid))
        result = run("1", cwd=other_repo)
        assert result.returncode == 3
        assert "seat-elsewhere-1 is already running" in result.stderr
    finally:
        live.kill()
        live.wait()


def test_the_marker_and_the_board_name_are_the_same_string(run, agent, runtime_dir):
    """They have to agree or the guard protects something other than the identity
    it describes — a marker on the bare number would refuse the second screen's
    seat 1 while the board happily gave it its own name."""
    run("6")
    instance = json.loads(agent.read_text())["instance"]
    assert (runtime_dir / f"qb-{instance}.pid").exists()


def test_the_scope_can_be_named(run, agent):
    """For the case the default cannot read: two screens on ONE repository."""
    assert run("1", env={"QB_SEAT_SCOPE": "review"}).returncode == 0
    assert json.loads(agent.read_text())["instance"] == "seat-review-1"


def test_an_empty_scope_asks_for_the_old_machine_wide_numbering(run, agent,
                                                                runtime_dir):
    """Set-and-empty is an answer, not a missing one — the same spelling
    QB_SEAT_BRIEF uses, and the only way to say "number these across the box"."""
    assert run("4", env={"QB_SEAT_SCOPE": ""}).returncode == 0
    assert json.loads(agent.read_text())["instance"] == "seat-4"
    assert (runtime_dir / "qb-seat-4.pid").exists()


def test_a_repository_reached_through_a_symlink_is_the_same_seat(run, agent,
                                                                 repo, tmp_path):
    """The scope comes off the PHYSICAL path. A seat that got a second identity
    out of how its directory was spelled would be this bug again with a longer
    story."""
    link = tmp_path / "by-another-name"
    link.symlink_to(repo)
    assert run("1", cwd=link).returncode == 0
    assert json.loads(agent.read_text())["instance"] == seat_name(1)


# The board is the authority on what a name may be: `X-Agent-Name` is refused with
# a 400 unless it matches this, and the key header's alphabet is no wider. A repo
# called `Foo.Bar_2` would otherwise make every seat in it fail registration — so
# the rule is asserted here rather than described in a comment.
BOARD_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
BOARD_NAME_MAX = 40


@pytest.mark.parametrize(
    "dirname, expected",
    [
        ("Foo.Bar_2", "seat-foo-bar-2-3"),          # case, dots and underscores
        ("nix-fleet", "seat-nix-fleet-3"),          # a hyphen of its own survives
        ("-leading-and-trailing-", "seat-leading-and-trailing-3"),
        ("dots...and___runs", "seat-dots-and-runs-3"),   # runs squeeze to one
        ("2024", "seat-2024-3"),                    # digits are a fine scope
        ("a" * 60, "seat-" + "a" * 32 + "-3"),      # capped, not refused
        ("also-far-too-long-" * 4, "seat-also-far-too-long-also-far-too-l-3"),
        # The cut lands ON a hyphen here, which is the case that matters: a name
        # ending in one is refused by the board outright, so the cap trims again
        # after it truncates.
        ("abc-" * 9, "seat-" + "-".join(["abc"] * 8) + "-3"),
    ],
)
def test_a_scope_is_slugged_into_something_the_board_will_take(
    run, agent, tmp_path, dirname, expected
):
    d = tmp_path / dirname
    d.mkdir()
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    assert run("3", cwd=d).returncode == 0
    instance = json.loads(agent.read_text())["instance"]
    assert instance == expected
    assert BOARD_NAME_RE.match(instance), f"the board would refuse {instance!r} with a 400"
    assert len(instance) <= BOARD_NAME_MAX


def test_a_scope_that_slugs_away_to_nothing_says_so(run, agent, tmp_path):
    """The quiet version of this is the bug the scope exists to fix: a seat back
    in the machine-wide namespace, where the next screen's seat of that number
    cannot start. An invented project name would be worse."""
    d = tmp_path / "___"
    d.mkdir()
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    result = run("3", cwd=d)
    assert result.returncode == 0
    assert json.loads(agent.read_text())["instance"] == "seat-3"
    assert "numbered across the whole machine" in result.stderr


def test_an_explicitly_empty_scope_is_not_complained_about(run, agent):
    """It asked for exactly that, so telling it is noise on every start — and
    noise on every start is how an operator learns to skip the line that means
    something."""
    result = run("3", env={"QB_SEAT_SCOPE": ""})
    assert result.returncode == 0
    assert result.stderr == ""


# ---- one pane per seat ------------------------------------------------------
#
# The board cannot catch this one: two panes on the same seat number send the
# same key, so it hands them one identity by construction and they are
# indistinguishable from its side. They also share the ask-poll cursor, which is
# the bug the per-seat instance exists to prevent, arriving one level down.


def test_a_seat_already_running_here_is_refused(run, agent, runtime_dir):
    live = subprocess.Popen(["sleep", "30"])
    try:
        (runtime_dir / marker_name(1)).write_text(str(live.pid))
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
        (runtime_dir / marker_name(1)).write_text(str(live.pid))
        assert run("2", env={"XDG_RUNTIME_DIR": str(runtime_dir)}).returncode == 0
    finally:
        live.kill()
        live.wait()


def _never_live_pid():
    """A pid that cannot be running, rather than one that merely is not.

    The reaped pid of a process this test started is recycled on a busy machine,
    and then the marker looks live for a reason that has nothing to do with what
    is being tested."""
    try:
        return int(Path("/proc/sys/kernel/pid_max").read_text().strip()) + 1
    except OSError:
        return 2**30


def test_a_marker_left_by_a_seat_that_ended_is_taken_over(run, agent, runtime_dir):
    """A seat that dies leaves its marker behind — nothing cleans it up, and a
    pane that could never be restarted would be worse than the collision."""
    (runtime_dir / marker_name(1)).write_text(str(_never_live_pid()))
    assert run("1", env={"XDG_RUNTIME_DIR": str(runtime_dir)}).returncode == 0


def test_a_corrupt_marker_does_not_block_the_seat(run, agent, runtime_dir):
    (runtime_dir / marker_name(1)).write_text("not-a-pid")
    assert run("1", env={"XDG_RUNTIME_DIR": str(runtime_dir)}).returncode == 0


def test_the_refusal_can_be_overridden(run, agent, runtime_dir):
    """For a marker whose pid has since been reused by something unrelated — and
    not in silence, because the override is an assertion rather than a check and
    being wrong about it is the shared-inbox bug with nothing on screen."""
    live = subprocess.Popen(["sleep", "30"])
    try:
        (runtime_dir / marker_name(1)).write_text(str(live.pid))
        result = run("1", env={"XDG_RUNTIME_DIR": str(runtime_dir), "QB_SEAT_FORCE": "1"})
        assert result.returncode == 0
        assert "QB_SEAT_FORCE is set, starting anyway" in result.stderr
        assert (runtime_dir / marker_name(1)).read_text() != str(live.pid)
    finally:
        live.kill()
        live.wait()


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_the_override_needs_a_truthy_value(run, agent, runtime_dir, value):
    """Tested for non-emptiness, `QB_SEAT_FORCE=0` from a layout that spells
    "off" that way turns the guard ON."""
    live = subprocess.Popen(["sleep", "30"])
    try:
        (runtime_dir / marker_name(1)).write_text(str(live.pid))
        result = run("1", env={"XDG_RUNTIME_DIR": str(runtime_dir), "QB_SEAT_FORCE": value})
        assert result.returncode == 3
        assert not agent.exists()
    finally:
        live.kill()
        live.wait()


def test_starting_a_seat_records_the_pid_of_the_agent_itself(run, agent, runtime_dir):
    """Not just "a number": qb-seat execs, so the marker holds the pid of the
    process that BECAME the agent. A refactor that backgrounded the agent instead
    would still write a marker and still hold a plausible number."""
    run("5", env={"XDG_RUNTIME_DIR": str(runtime_dir)})
    marker = runtime_dir / marker_name(5)
    assert marker.read_text() == str(json.loads(agent.read_text())["pid"])


def test_panes_started_at_the_same_instant_leave_exactly_one_seat(
    run, fake_bin, tmp_path, runtime_dir
):
    """The case the guard exists for is a layout, and a layout starts all n panes
    at once. Look-then-write loses that race by construction: every pane sees a
    free seat, and the one that got there first is still several seconds deep in
    registering the name with the board when the rest decide it is theirs."""
    starts = tmp_path / "starts"
    fake_bin(
        "claude",
        f'#!/bin/sh\nprintf "%s\\n" "$$" >> {starts}\nsleep 2\n',
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [f.result() for f in [pool.submit(run, "1") for _ in range(8)]]
    codes = [r.returncode for r in results]
    assert codes.count(0) == 1, codes
    assert codes.count(3) == 7, codes
    assert len(starts.read_text().split()) == 1


def test_an_unwritable_marker_directory_costs_the_guard_and_not_the_seat(run, agent, tmp_path):
    """Best-effort, and said out loud: a seat that cannot be guarded still starts,
    but nothing on this machine can now tell whether another pane is it."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        result = run("1", env={"XDG_RUNTIME_DIR": str(locked)})
        assert result.returncode == 0
        assert "unguarded" in result.stderr
        assert agent.exists()
    finally:
        locked.chmod(0o700)


def test_a_marker_path_pointed_at_something_else_is_not_written_through(
    run, agent, tmp_path, runtime_dir
):
    """The fallback marker lives in a shared directory, so the path can be
    somebody else's symlink by the time we get to it. link(2) does not follow one
    at the destination, which is most of why it is the primitive here."""
    target = tmp_path / "precious"
    target.write_text("do not overwrite me")
    (runtime_dir / marker_name(1)).symlink_to(target)
    result = run("1", env={"XDG_RUNTIME_DIR": str(runtime_dir)})
    assert result.returncode == 0
    assert target.read_text() == "do not overwrite me"
    marker = runtime_dir / marker_name(1)
    assert not marker.is_symlink()
    assert marker.read_text() == str(json.loads(agent.read_text())["pid"])


def test_the_marker_falls_back_to_a_per_user_path(run, agent, tmp_path):
    """macOS has no XDG_RUNTIME_DIR, and neither do most containers. /tmp is
    shared, so an unqualified name there means the first user to start seat 3
    owns the marker for everyone: the second user's seat cannot write it, cannot
    remove it, and reads `kill -0` refusing on a live pid as proof it is dead."""
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    result = run("7", env={"TMPDIR": str(tmpdir)}, unset=("XDG_RUNTIME_DIR",))
    assert result.returncode == 0
    assert (tmpdir / f"qb-{os.getuid()}-{seat_name(7)}.pid").exists()


def test_a_dry_run_is_told_about_a_collision_but_leaves_no_marker(run, agent, runtime_dir):
    live = subprocess.Popen(["sleep", "30"])
    try:
        (runtime_dir / marker_name(1)).write_text(str(live.pid))
        assert run("1", "--dry-run", env={"XDG_RUNTIME_DIR": str(runtime_dir)}).returncode == 3
    finally:
        live.kill()
        live.wait()
    run("2", "--dry-run", env={"XDG_RUNTIME_DIR": str(runtime_dir)})
    assert not (runtime_dir / marker_name(2)).exists()


def test_the_registration_is_skipped_for_a_dry_run(run, board):
    """--dry-run exists to be safe to run; it must not claim a name either."""
    calls = board()
    run(
        "1",
        "--dry-run",
        env={"QUARTERBACK_BASE_URL": "https://board.example", "QUARTERBACK_TOKEN": "t"},
    )
    assert _sent(calls) == ""


def test_forwarded_knobs_are_all_read():
    """Every QB_SEAT_* qb-seats forwards into a pane must be one qb-seat reads.

    The two halves of this feature shipped with almost disjoint vocabularies:
    qb-seats forwarded QB_SEAT_AGENT, QB_SEAT_AGENT_ARGS and QB_SEAT_BRIEF, while
    qb-seat read QB_SEAT_CLAUDE, QB_SEAT_REPO, QB_SEAT_FORCE and QB_SEAT_BRIEF.
    Only BRIEF was common, so `QB_SEAT_AGENT=cat qb-seats` — the exact trick for a
    smoke test — arrived in the pane, was read by nothing, and started real agents
    (board finding 3632).

    Nothing fails when this drifts. The variable is set, the pane receives it, the
    seat ignores it and reports success, so the only symptom is that the knob does
    not work — which you discover by having run the thing you were trying not to
    run. Hence a test rather than a comment: it is asked of both files, so a knob
    renamed on one side has to be renamed on the other or this goes red.

    One-directional on purpose. qb-seat may read a knob qb-seats does not forward
    (QB_SEAT_REPO is one: the layout sets each pane's cwd instead, so forwarding a
    repo would fight it). The reverse is the defect.
    """
    seats = (QB_SEAT.parent / "qb-seats").read_text()
    seat = QB_SEAT.read_text()

    # The whole body of the forwarding function, not just its loop. QB_SEAT_BRIEF is
    # forwarded on a line of its own — it is read with `${VAR+set}` rather than
    # `${VAR:-}`, so the loop's `-n` test cannot express it — and a parser that only
    # read the loop stopped checking the one knob whose forwarding has already
    # been got wrong once.
    body = seats.split("seat_env_fill() {", 1)
    assert len(body) == 2, "could not find qb-seats' forwarding function seat_env_fill"
    forwarded = set(re.findall(r"QB_SEAT_[A-Z_]+", body[1].split("\n}", 1)[0]))
    assert forwarded, "seat_env_fill forwards nothing — has it been rewritten?"

    read = set(re.findall(r"\$\{(QB_SEAT_[A-Z_]+)", seat))
    assert read, "could not find any QB_SEAT_* reads in qb-seat"

    unread = forwarded - read
    assert not unread, (
        "qb-seats forwards knobs qb-seat never reads, so setting them does nothing "
        f"and says nothing: {sorted(unread)}. qb-seat reads {sorted(read)}."
    )



# ---- the fleet's ceiling, before the seat spends it (#275) --------------------
#
# A seat is a pane nobody is watching, spending a subscription it shares with
# every other seat and every other project on the fleet. These pin the gate that
# lets it be told, and — only when asked — stopped.


@pytest.fixture
def pace(fake_bin, tmp_path):
    """A stub `qb-pace` on PATH, answering whatever a test wants.

    On PATH rather than beside the script, because that is the order qb-seat looks
    in (an installed copy beats a checkout) — and because a stub the real command
    would otherwise shadow is a stub that proves nothing.
    """
    log = tmp_path / "pace.log"

    def _install(line: str, status: int) -> Path:
        fake_bin("qb-pace", _SANDBOX_SAFE_SHEBANG
                 + f'printf "%s\\n" "$*" >> {log}\n'
                 + (f'printf "%s\\n" {line!r}\n' if line else "")
                 + f"exit {status}\n")
        return log

    return _install


def test_a_spent_window_stops_a_seat_that_was_told_to_obey(run, agent, pace):
    """RED/GREEN. This is the one refusal in the harness that reads a ceiling
    nobody here sets, and it is opt-in: a pane started by something with no human
    in front of it can be told to wait rather than to fail three retries deep into
    a round the fleet cannot pay for."""
    pace("pace: HOLD — 5h at 96%; resets in 47m", 3)
    result = run("1", env={"QB_SEAT_PACE": "obey"})
    assert result.returncode == 4
    assert not agent.exists(), "the agent was started anyway"
    assert "5h at 96%" in result.stderr
    assert "47m" in result.stderr, "a hold that does not say when is a stop"


def test_a_spent_window_only_says_so_by_default(run, agent, pace):
    """Being told beats being blocked. A human who wants to spend the last 4% of a
    window on the thing they care about is making a decision, and a spawner is not
    the place to overrule it — so the default warns and starts."""
    pace("pace: HOLD — 5h at 96%; resets in 47m", 3)
    # UNSET, not "warn": the fixture pins the knob off so the rest of the suite
    # never reads a real subscription, and a test about the default that supplied
    # one would be asserting its own argument.
    result = run("1", unset=("QB_SEAT_PACE",))
    assert result.returncode == 0
    assert agent.exists(), "the default refused to start a seat"
    assert "5h at 96%" in result.stderr


def test_a_ceiling_that_could_not_be_read_does_not_stop_a_seat(run, agent, pace):
    """`unknown` is qb-pace's 4, and it must not be treated as a spent window.
    Refusing every seat on the fleet because a laptop dropped its network is a far
    larger claim than the one this gate is making — but it is still SAID, because
    the one thing an unreadable ceiling may not do is read as a clear one (#244)."""
    pace("pace: UNKNOWN — the usage endpoint could not be read (HTTP 500)", 4)
    result = run("1", env={"QB_SEAT_PACE": "obey"})
    assert result.returncode == 0
    assert agent.exists()
    assert "could not be read" in result.stderr


def test_a_pace_command_that_hangs_or_breaks_does_not_hold_up_the_pane(run, agent, pace):
    """Any status that is not qb-pace's `hold` starts the seat. A gate that fails
    closed on a broken install stops being a budget gate and becomes an outage."""
    pace("", 127)
    assert run("1", env={"QB_SEAT_PACE": "obey"}).returncode == 0
    assert agent.exists()


def test_off_does_not_consult_at_all(run, agent, pace):
    """The knob a test needs, and a box that wants nothing to do with this."""
    log = pace("pace: HOLD — 5h at 96%", 3)
    assert run("1", env={"QB_SEAT_PACE": "off"}).returncode == 0
    assert not log.exists(), "qb-pace was run despite QB_SEAT_PACE=off"


def test_a_dry_run_reaches_the_refusal_like_every_other_one(run, agent, pace):
    """The usage text promises it: a --dry-run reports every refusal without
    starting anything, which is exactly when you want to be told about this one."""
    pace("pace: HOLD — 5h at 96%; resets in 47m", 3)
    result = run("1", "--dry-run", env={"QB_SEAT_PACE": "obey"})
    assert result.returncode == 4
    assert not agent.exists()


def test_an_unreadable_value_is_read_as_warn_and_says_so(run, agent, pace):
    """A typo must not silently disable the gate, and must not silently enable a
    refusal either. It warns, which is the default, and names the value."""
    pace("pace: HOLD — 5h at 96%", 3)
    result = run("1", env={"QB_SEAT_PACE": "obeyy"})
    assert result.returncode == 0
    assert agent.exists()
    assert "obeyy" in result.stderr


def test_the_gate_is_a_documented_exit_code(run):
    """4 is in the usage text's list, so `qb-seat --help` accounts for every
    refusal it can make."""
    assert "4  the shared subscription's window is spent" in run("--help").stdout
