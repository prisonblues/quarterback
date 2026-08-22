"""`qb-start` — the half of #277 that was never built (steps 3-4).

There were three ways to start a session in this fleet and every one of them
ended at a human hand. v2.77 gave it `end`. This is `start`, and the four
properties this suite exists to hold it to are the four the issue argues for:

* **OFF BY DEFAULT, AND THE DEFAULT COSTS NOTHING.** With no policy file
  `qb-start` refuses before it has looked for a board, a token, a network, tmux
  or the agent. Asserted the way #337 asserted its own default rather than
  asserted about it: the stub `qbdata` raises on IMPORT, and `tmux`, `qb-pace`,
  `qb-admit` and `qb-claim` are all stubs that record being run — so "it consulted
  nothing" is a file that stayed empty, not a comment.
* **THE BRIEF IS A NAMED COMMAND, NEVER FREE TEXT.** The set is compiled in and a
  machine's policy can only narrow it, so a policy naming `/rm-rf` refuses the
  same as one naming nothing.
* **EVERY SPAWN IS COUNTED.** The claim is taken through the ordinary `qb-claim`
  path, before the process exists, and a claim that cannot be taken — held OR
  unreachable — refuses the spawn. A session nobody can count is worse than one
  nobody started, which is the one place this deliberately differs from
  `create-worktree`.
* **IT IS ENDABLE BEFORE IT EXISTS.** The session id is minted here and handed to
  the agent with `--session-id`, so the pane wears `@qb_session` from creation
  and `qb-end <id>` works immediately.

Stubbed the way `test_qb_admit.py` and `test_qb_end.py` stub one: a COPY of the
script beside a stub `qbdata.py`, because the script puts its own directory at
the front of `sys.path` and PYTHONPATH cannot shadow that. The neighbouring
`qb-*` tools are resolved with `shutil.which`, so a stub directory at the front
of PATH is what stands in for them.

The tmux tests are end-to-end against a REAL tmux server on a private socket, not
a stub: what is being tested is that a window appears, carries the options
everything else selects on, and runs the argv that was composed — and a stub tmux
can only ever agree with whatever this file believes about tmux.

Run: pytest harness/tests/test_qb_start.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
START = BIN / "qb-start"
HM_MODULE = Path(__file__).resolve().parents[1] / "hm-module.nix"

STARTED, MISUSE = 0, 2
NOT_ENABLED, NOT_ALLOWED, AT_CAP = 3, 4, 5
PACED, FULL, HELD = 6, 7, 8
COULD_NOT_START = 9

TMUX = shutil.which("tmux")

ENABLED = {"enabled": True, "commands": ["/fix-issue", "/panel-review-pr"],
           "max_sessions": 2}


# ---------------------------------------------------------------- the sandbox

def stub_tool(path: Path, exit_code: int = 0, log: Path | None = None) -> None:
    """A `qb-*` stand-in that records its argv and exits with `exit_code`.

    `#!/bin/sh`, never `#!/usr/bin/env` — there is no `/usr/bin/env` inside a nix
    build sandbox and a runtime-written stub is past `patchShebangs` (#177).
    """
    path.write_text(
        "#!/bin/sh\n"
        + (f'printf "%s\\n" "{path.name} $*" >> {log}\n' if log else "")
        + f"exit {exit_code}\n")
    path.chmod(0o755)


def sandbox(tmp_path: Path, *, policy: object = "absent", explode: bool = True,
            pace: int = 0, admit: int = 0, claim: int = 0, release: int = 0,
            tmux_exit: int | None = 0, new_window_exit: int | None = None,
            set_option_exit: int | None = None) -> dict:
    """A copy of `qb-start` with every neighbour it could reach replaced.

    `explode` makes the stub board client unimportable, which is how the
    costs-nothing property is asserted rather than asserted about. Only the tests
    that need a board turn it off.
    """
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    copied = stub / START.name
    copied.write_bytes(START.read_bytes())
    posts = tmp_path / "posts.jsonl"
    (stub / "qbdata.py").write_text(f"""
import json

if {explode!r}:
    raise ImportError("a machine that has not opted in must not need a board client")


class _Client:
    def post(self, path, body):
        with open({str(posts)!r}, "a") as fh:
            fh.write(json.dumps({{"path": path, "body": body}}) + "\\n")
        return {{}}


def repo_slug(path="."):
    return "acme/widget"


def board_client():
    return _Client(), None
""")

    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    log = tmp_path / "ran.log"
    stub_tool(tools / "qb-pace", pace, log)
    stub_tool(tools / "qb-admit", admit, log)
    stub_tool(tools / "qb-claim", claim, log)
    stub_tool(tools / "qb-release", release, log)
    if tmux_exit is not None:
        # `new-window` is the one tmux call whose STDOUT matters — qb-start reads
        # the pane id back off it — so the stand-in has to answer rather than
        # merely exit, or every spawn reads as a tmux that made no pane. And it
        # can be made to fail on its OWN, separately from `list-panes`: since the
        # cap counts panes before anything else, a tmux that fails at everything
        # never reaches the window at all, and the two failures are different
        # tests.
        (tools / "tmux").write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "tmux $*" >> {log}\n'
            'if [ "$1" = "new-window" ]; then\n'
            '  printf "%s\\n" "%9"\n'
            f'  exit {tmux_exit if new_window_exit is None else new_window_exit}\n'
            "fi\n"
            'if [ "$1" = "set-option" ]; then\n'
            f'  exit {tmux_exit if set_option_exit is None else set_option_exit}\n'
            "fi\n"
            f"exit {tmux_exit}\n")
        (tools / "tmux").chmod(0o755)

    # $XDG_CONFIG_HOME, because there is no override to point at a file: the
    # resolution under test is the real one, and it is the same one `qb-env`,
    # `qb-seat` and `qbdata` use. A `$QUARTERBACK_SPAWN` existed here until the
    # codex review pointed out that a bypass a repository could set falsifies the
    # only claim this gate makes.
    config = tmp_path / "config"
    (config / "quarterback").mkdir(parents=True, exist_ok=True)
    policy_path = config / "quarterback" / "spawn.json"
    if policy != "absent":
        policy_path.write_text(policy if isinstance(policy, str) else json.dumps(policy))

    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    return {"script": copied, "policy": policy_path, "config": config, "repo": repo,
            "tools": tools, "log": log, "posts": posts}


def run(box: dict, *args: str, repo_path: str | None = None, tmux: str = "",
        env: dict | None = None):
    """`qb-start` inside the sandbox. `tmux` is what $TMUX is set to — empty means
    there is no multiplexer, which is a different answer from a broken one."""
    where = {**os.environ,
             "XDG_CONFIG_HOME": str(box["config"]),
             "PATH": f"{box['tools']}{os.pathsep}{os.environ['PATH']}",
             **(env or {})}
    where.pop("TMUX", None)
    if tmux:
        where["TMUX"] = tmux
    got = subprocess.run(
        [sys.executable, str(box["script"]),
         "--repo-path", repo_path or str(box["repo"]), *args],
        capture_output=True, text=True, env=where)
    got.ran = (box["log"].read_text().splitlines() if box["log"].exists() else [])
    got.posts = [json.loads(ln) for ln in
                 (box["posts"].read_text().splitlines() if box["posts"].exists() else [])]
    return got


# ------------------------------------- the default is off, and it costs nothing

@pytest.mark.parametrize("policy,why", [
    ("absent", "no policy file at all — the shipped state of every machine"),
    ({"commands": ["/fix-issue"]}, "a policy that never says enabled"),
    ({"enabled": False, "commands": ["/fix-issue"]}, "enabled: false"),
    ({"enabled": "true", "commands": ["/fix-issue"]}, "the STRING true"),
    ({"enabled": 1, "commands": ["/fix-issue"]}, "1, which is truthy and is not true"),
])
def test_a_machine_that_has_not_opted_in_refuses_and_consults_nothing(policy, why, tmp_path):
    """The property that makes this landable while nobody is watching, and the one
    to break last. The stub board client raises on IMPORT and every neighbour is a
    recording stub, so reaching any of them at all is the failure."""
    box = sandbox(tmp_path, policy=policy)
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert got.returncode == NOT_ENABLED, (why, got.stderr)
    assert got.ran == [], f"a disabled machine ran {got.ran} ({why})"
    assert got.posts == [], "a refusal on a machine that never opted in has nobody to tell"
    assert "not enabled" in got.stderr


def test_the_refusal_names_the_option_that_turns_it_on(tmp_path):
    """A refusal an operator cannot act on is a refusal they work around."""
    got = run(sandbox(tmp_path), "/fix-issue", "277")
    assert "programs.quarterback-harness.spawn.enable" in got.stderr
    assert "nothing a repository or an agent can write turns this on" in got.stderr


@pytest.mark.parametrize("policy,why", [
    ("{not json at all", "a policy file that will not parse"),
    ('["/fix-issue"]', "a list where an object belongs"),
    ({"enabled": True, "commands": "/fix-issue"}, "commands as a bare string"),
    ({"enabled": True, "commands": [1, 2]}, "commands that are not strings"),
    ({"enabled": True, "commands": [], "max_sessions": "two"}, "a max_sessions typo"),
    ({"enabled": True, "commands": [], "max_sessions": -1}, "a negative ceiling"),
    ({"enabled": True, "commands": [], "max_sessions": True}, "true, which is an int in python"),
    ({"enabled": True, "commands": [], "skip_permissions": "no"}, "a non-boolean switch"),
])
def test_a_malformed_policy_fails_CLOSED(policy, why, tmp_path):
    """The opposite of `qb-admit`, and deliberately so.

    `in_flight.max` is a restriction: failing open on a typo admits one agent too
    many, and failing closed would throttle every checkout on the fleet over a
    config file. `spawn.json` is a PERMISSION: failing open on a typo starts
    sessions nobody authorised, on a box holding ~/.claude and the board token.
    """
    got = run(sandbox(tmp_path, policy=policy), "/fix-issue", "277")
    assert got.returncode == NOT_ENABLED, (why, got.stderr)
    assert got.ran == [], f"{why} still consulted {got.ran}"


def test_a_policy_that_is_a_directory_is_not_a_policy(tmp_path):
    """`is_file`, not `exists` — the one shape that would reach `read_text`."""
    box = sandbox(tmp_path)
    box["policy"].mkdir()
    assert run(box, "/fix-issue", "277").returncode == NOT_ENABLED


# ----------------------------------------------- the allowlist, and both locks

def test_enabled_with_no_commands_still_spawns_nothing(tmp_path):
    """The second lock, and it is `issue_pickup.only_labels`' argument: turning
    spawning on is one decision, saying what may come through it is another."""
    box = sandbox(tmp_path, policy={"enabled": True, "commands": []}, explode=False)
    got = run(box, "/fix-issue", "277")
    assert got.returncode == NOT_ALLOWED
    assert "second lock" in got.stderr
    assert not any("qb-claim" in line for line in got.ran)


def test_a_policy_cannot_widen_the_compiled_in_set(tmp_path):
    """The whole reason the table is in the script. A policy file that could name
    any string would be the free-text brief this exists to refuse, one indirection
    further out — and on a public repo an issue body is an agent's instructions."""
    box = sandbox(tmp_path, explode=False,
                  policy={"enabled": True, "commands": ["/anything-i-like"]})
    got = run(box, "/anything-i-like", "1")
    assert got.returncode == NOT_ALLOWED
    assert "compiled in" in got.stderr
    assert got.ran == []


def test_a_spawnable_command_this_machine_did_not_name_is_refused(tmp_path):
    box = sandbox(tmp_path, policy=ENABLED, explode=False)
    got = run(box, "/fix-and-land", "277")
    assert got.returncode == NOT_ALLOWED
    assert "/fix-issue" in got.stderr, "the refusal should say what IS allowed"


def test_the_leading_slash_is_optional(tmp_path):
    """`fix-issue` and `/fix-issue` are one command. A spawner that refused the
    first would be refusing a typo rather than a permission."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False)
    assert run(box, "fix-issue", "277", "--dry-run").returncode == STARTED


# ------------------------------------------------- the argument, not free text

@pytest.mark.parametrize("number", [
    "277 && echo pwned", "$(id)", "../../etc/passwd", "twelve", "", "-1", "0",
    "1.5", "12; rm x", "٧",
])
def test_only_a_positive_integer_gets_through(number, tmp_path):
    """The brief is a named command WITH ARGUMENTS. The arguments are what a
    caller controls, so they are the whole of the injection surface, and one
    positive integer is the whole of what these commands take."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False)
    got = run(box, "/fix-issue", number, "--dry-run")
    assert got.returncode == MISUSE, got.stderr
    assert not any("qb-claim" in line for line in got.ran)


def test_a_second_argument_is_refused_by_the_parser(tmp_path):
    box = sandbox(tmp_path, policy=ENABLED, explode=False)
    got = run(box, "/fix-issue", "277", "and-another-thing")
    assert got.returncode != STARTED
    assert got.ran == []


def test_a_directory_that_is_not_a_git_worktree_is_refused(tmp_path):
    box = sandbox(tmp_path, policy=ENABLED, explode=False)
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    got = run(box, "/fix-issue", "277", repo_path=str(plain))
    assert got.returncode == MISUSE
    assert "not a git worktree" in got.stderr


# --------------------------------------------------- the gates, and their order

def test_a_spent_window_does_not_start_a_session(tmp_path):
    """`qb-pace --gate` exit 3 is hold. Unlike a seat — which defaults to warning
    a human who can then decide — a spawn obeys, because there is nobody to tell."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False, pace=3)
    got = run(box, "/fix-issue", "277")
    assert got.returncode == PACED
    assert not any("qb-claim" in line for line in got.ran)
    assert got.posts and got.posts[0]["body"]["type"] == "note"


@pytest.mark.parametrize("gate,code,why", [
    ("pace", PACED, "the caps could not be obtained at all — #244's shape"),
    ("admit", FULL, "the board could not be asked, or the ceiling would not parse"),
])
def test_a_gate_that_could_not_run_is_not_a_gate_that_passed(gate, code, why, tmp_path):
    """The asymmetry with `create-worktree`, and it is the point rather than an
    oversight. A checkout failing open is a human who has already decided to work
    being told the board is unreachable; a spawn failing open is an unattended
    session nobody decided on, against a ceiling nobody could read."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False, **{gate: 2})
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert got.returncode == code, (why, got.stderr)
    assert "could not be read" in got.stderr


@pytest.mark.parametrize("gate,code", [("pace", PACED), ("admit", FULL)])
@pytest.mark.parametrize("status", [2, 4, 127])
def test_no_exit_but_zero_lets_a_gate_wave_a_spawn_through(gate, code, status, tmp_path):
    """127 is what `run_gate` reports for a tool that is not there at all. `qb-pace`
    and `qb-admit` ship in the same package as `qb-start` and `sibling()` falls
    back to the directory beside it, so a partial install is not a state a real one
    reaches — which is exactly why it must not be the state that quietly waves a
    spawn through."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False, **{gate: status})
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert got.returncode == code, got.stderr
    assert not any("qb-claim" in line for line in got.ran)


def test_a_tmux_that_will_not_list_its_panes_refuses_rather_than_counting_zero(tmp_path):
    """A cap that switches itself off when its input goes unreadable is not one.
    `list-panes` failing is not "no spawns are running"."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False, tmux_exit=1)
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert got.returncode == AT_CAP
    assert "could not be counted" in got.stderr
    assert not any("qb-claim" in line for line in got.ran)


def test_a_full_in_flight_window_does_not_start_a_session(tmp_path):
    """#337's bound is decorative if the thing that starts sessions routes round
    it. This is the call that stops that."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False, admit=1)
    got = run(box, "/fix-issue", "277")
    assert got.returncode == FULL
    assert not any("qb-claim" in line for line in got.ran)


def test_the_gates_are_asked_before_the_claim_and_the_claim_before_the_pane(tmp_path):
    """Order is the feature. A refusal costs nothing to unwind only while nothing
    has been taken, and a claim taken after a pane exists is a session that ran
    uncounted for however long the board took to answer."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False)
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    order = [line.split()[0] for line in got.ran]
    assert order.index("qb-pace") < order.index("qb-admit") < order.index("qb-claim")
    # `new-window` specifically, not the first tmux call: the cap counts panes
    # before any of the gates run, so "tmux was touched" is true well before the
    # window this is about.
    window = next(i for i, line in enumerate(got.ran) if line.startswith("tmux new-window"))
    assert order.index("qb-claim") < window


def test_a_held_claim_refuses_the_spawn(tmp_path):
    box = sandbox(tmp_path, policy=ENABLED, explode=False, claim=1)
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert got.returncode == HELD
    assert not any(line.startswith("tmux new-window") for line in got.ran)


def test_a_claim_that_cannot_be_taken_refuses_the_spawn(tmp_path):
    """The one place this deliberately differs from `create-worktree`, which warns
    and proceeds. A checkout that cannot reach the board is a human who has already
    decided to work; a spawn that cannot reach the board is an unattended session
    nobody would know about, taken out of a count nobody can see."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False, claim=2)
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert got.returncode == HELD
    assert "not started uncounted" in got.stderr
    assert not any(line.startswith("tmux new-window") for line in got.ran)


def test_a_qb_claim_that_is_not_installed_refuses_the_spawn(tmp_path):
    """A partial install is the same answer as an outage, and for the same reason:
    what fails is the ability to COUNT the session, and an uncounted unattended
    session is the thing this must not create. `create-worktree` warns and proceeds
    here; a spawner may not."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False)
    (box["tools"] / "qb-claim").unlink()
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert got.returncode == HELD
    assert not any(ln.startswith("tmux new-window") for ln in got.ran)


def test_the_claim_names_the_resource_and_records_no_session(tmp_path):
    """`--session ""`, for `create-worktree`'s reason at length: the agent that
    will do the work does not exist yet, and a claim stamped with a session that is
    not the worker's makes `may_mutate` refuse the worker its own renewal."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False)
    run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    line = next(ln for ln in box["log"].read_text().splitlines() if ln.startswith("qb-claim"))
    assert line.split()[1:3] == ["issue", "277"]
    assert "--session  " in line + " ", f"the claim must record no session: {line}"


def test_a_pr_command_claims_a_pr_and_not_an_issue(tmp_path):
    """The kind comes from the table, so a spawn is counted against the thing it
    is actually working — #172's join, not a fourth spelling of it."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False)
    run(box, "/panel-review-pr", "352", tmux="/tmp/fake,1,0")
    line = next(ln for ln in box["log"].read_text().splitlines() if ln.startswith("qb-claim"))
    assert line.split()[1:3] == ["pr", "352"]


# -------------------------------------------------------------------- dry runs

def test_a_dry_run_reaches_every_refusal(tmp_path):
    """The point of one: report what would stop this without starting it."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False, admit=1)
    assert run(box, "/fix-issue", "277", "--dry-run").returncode == FULL


def test_a_dry_run_takes_no_claim_posts_nothing_and_opens_no_window(tmp_path):
    box = sandbox(tmp_path, policy=ENABLED, explode=False)
    got = run(box, "/fix-issue", "277", "--dry-run", tmux="/tmp/fake,1,0")
    assert got.returncode == STARTED
    assert not any("qb-claim" in line for line in got.ran)
    assert not any(line.startswith("tmux new-window") for line in got.ran)
    assert got.posts == []


def test_a_dry_run_prints_the_argv_it_would_run(tmp_path):
    box = sandbox(tmp_path, policy=ENABLED, explode=False)
    got = run(box, "/fix-issue", "277", "--dry-run")
    assert "--session-id" in got.stderr
    assert "'/fix-issue 277'" in got.stderr


# -------------------------------------------------------- the board record

def test_a_spawn_is_a_post_before_it_is_a_process(tmp_path):
    """Every spawn is a board post before it is a process, and refusals are posted
    too — that is what makes a fleet's spawning readable rather than something you
    find out about by noticing a pane."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False)
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert got.posts, "nothing was recorded"
    body = got.posts[0]["body"]
    assert body["type"] == "status"
    assert "/fix-issue 277" in body["summary"]
    assert "qb-end" in body["detail"], "the record should say how to stop it"
    assert {"kind": "issue", "value": "277", "repo": "acme/widget"} in body["refs"]


def test_a_board_that_cannot_be_reached_does_not_stop_a_spawn(tmp_path):
    """Best-effort, for `qb-stage`'s reason: a session that refused to start
    because a record of it could not be written would cost more than the record is
    worth, and the pane is the durable evidence either way. The CLAIM is the part
    that is not best-effort, and it is taken first."""
    box = sandbox(tmp_path, policy=ENABLED, explode=True)
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert got.returncode == STARTED
    assert any(line.startswith("tmux new-window") for line in got.ran)


# --------------------------------------------------------- against a real tmux

@pytest.fixture()
def server(tmp_path):
    """A real tmux server on a private socket, and a `tmux` on PATH that talks to
    it. Not a stub: what is under test is that a window appears carrying the
    options everything else selects on, and a stub can only agree with whatever
    this file believes about tmux."""
    if not TMUX:
        pytest.skip("no tmux on this box")
    socket = f"qbstart-{uuid.uuid4().hex[:8]}"
    subprocess.run([TMUX, "-L", socket, "new-session", "-d", "-s", "base",
                    "sleep", "600"], check=True, timeout=20)
    try:
        yield socket
    finally:
        subprocess.run([TMUX, "-L", socket, "kill-server"],
                       capture_output=True, timeout=20)


def wire(box: dict, socket: str, agent_body: str) -> Path:
    """Point the sandbox's `tmux` at `socket` and its agent at a recording stub."""
    (box["tools"] / "tmux").write_text(
        f'#!/bin/sh\nexec {TMUX} -L {socket} "$@"\n')
    (box["tools"] / "tmux").chmod(0o755)
    seen = box["tools"].parent / "agent.argv"
    agent = box["tools"] / "fake-agent"
    agent.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" > {seen}\n{agent_body}\n')
    agent.chmod(0o755)
    return seen


def panes(socket: str) -> list[dict]:
    fields = ("pane_id", "@qb_spawn", "@qb_spawn_ended", "@qb_session", "@qb_label",
              "window_name")
    got = subprocess.run(
        [TMUX, "-L", socket, "list-panes", "-a", "-F",
         "\t".join("#{%s}" % f for f in fields)],
        capture_output=True, text=True, timeout=20)
    return [dict(zip(fields, line.split("\t")))
            for line in got.stdout.splitlines() if line.count("\t") == len(fields) - 1]


def wait_for(predicate, seconds: float = 15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if (got := predicate()):
            return got
        time.sleep(0.15)
    return None


def test_a_spawn_is_a_real_window_stamped_with_its_session(tmp_path, server):
    """No hidden sessions. What it starts is a pane a human can attach to, read
    and interrupt — and it wears `@qb_session` from the moment it is created,
    rather than from whenever the agent's SessionStart hook gets round to it. That
    stamp is what lets `qb-end` and the seat bar's ✕ reach a session nothing has
    heard from yet."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False, tmux_exit=None)
    seen = wire(box, server, "sleep 60")
    got = run(box, "/fix-issue", "277", "--json", tmux=f"/tmp/{server},1,0",
              env={"QB_START_AGENT": str(box["tools"] / "fake-agent")})
    assert got.returncode == STARTED, got.stderr
    answer = json.loads(got.stdout)
    session = answer["session"]
    assert re.fullmatch(r"[0-9a-f-]{36}", session), session

    mine = wait_for(lambda: [p for p in panes(server) if p["@qb_session"] == session])
    assert mine, f"no pane carries {session}: {panes(server)}"
    assert mine[0]["@qb_spawn"] == "/fix-issue"
    assert mine[0]["@qb_label"] == "fix-issue-277"
    assert mine[0]["window_name"] == "fix-issue-277"
    assert mine[0]["@qb_spawn_ended"] == "", "the agent is still running"

    argv = wait_for(lambda: seen.read_text().strip() if seen.exists() else None)
    assert f"--session-id {session}" in argv, argv
    assert "--dangerously-skip-permissions" in argv
    assert argv.endswith("-- /fix-issue 277"), argv


def test_the_pane_records_its_own_agent_leaving(tmp_path, server):
    """The window outlives the agent on purpose — its output is the only record of
    what happened — so counting windows would fill the cap with corpses. The pane
    writing `@qb_spawn_ended` on itself is what keeps the count honest."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False, tmux_exit=None)
    wire(box, server, "exit 0")
    got = run(box, "/fix-issue", "277", "--json", tmux=f"/tmp/{server},1,0",
              env={"QB_START_AGENT": str(box["tools"] / "fake-agent")})
    session = json.loads(got.stdout)["session"]
    done = wait_for(lambda: [p for p in panes(server)
                             if p["@qb_session"] == session and p["@qb_spawn_ended"]])
    assert done, f"the pane never recorded the agent leaving: {panes(server)}"


def test_the_machine_cap_counts_live_spawns_and_refuses_at_it(tmp_path, server):
    """A concurrency cap per machine, which is what the issue asks for and what
    keeps an unattended trigger from being a fork bomb. Counted across the whole
    tmux SERVER, so a second screen gets no second allowance."""
    box = sandbox(tmp_path, policy={**ENABLED, "max_sessions": 1},
                  explode=False, tmux_exit=None)
    wire(box, server, "sleep 60")
    env = {"QB_START_AGENT": str(box["tools"] / "fake-agent")}
    first = run(box, "/fix-issue", "277", "--json", tmux=f"/tmp/{server},1,0", env=env)
    assert first.returncode == STARTED, first.stderr
    session = json.loads(first.stdout)["session"]
    assert wait_for(lambda: [p for p in panes(server) if p["@qb_session"] == session])

    second = run(box, "/panel-review-pr", "352", tmux=f"/tmp/{server},1,0", env=env)
    assert second.returncode == AT_CAP, second.stderr
    assert "1 of 1" in second.stderr
    assert not any(ln.startswith("qb-claim pr") for ln in second.ran)


def test_a_finished_spawn_frees_its_slot(tmp_path, server):
    """The other half of the cap: a window left open to read is not a running
    session, and a cap that could not tell them apart would jam after `max` spawns
    however few were alive."""
    box = sandbox(tmp_path, policy={**ENABLED, "max_sessions": 1},
                  explode=False, tmux_exit=None)
    wire(box, server, "exit 0")
    env = {"QB_START_AGENT": str(box["tools"] / "fake-agent")}
    first = run(box, "/fix-issue", "277", "--json", tmux=f"/tmp/{server},1,0", env=env)
    session = json.loads(first.stdout)["session"]
    assert wait_for(lambda: [p for p in panes(server)
                             if p["@qb_session"] == session and p["@qb_spawn_ended"]])
    second = run(box, "/panel-review-pr", "352", "--dry-run",
                 tmux=f"/tmp/{server},1,0", env=env)
    assert second.returncode == STARTED, second.stderr


def test_a_freeze_is_a_legal_ceiling(tmp_path):
    """0 is a value and it means admit nothing — a freeze rather than a typo, and
    reachable without taking the allowlist apart."""
    box = sandbox(tmp_path, policy={**ENABLED, "max_sessions": 0}, explode=False)
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert got.returncode == AT_CAP
    assert "0 is a freeze" in got.stderr


def test_permissions_can_be_put_back(tmp_path, server):
    """A real trade, made inside a file a human wrote on purpose — and reversible
    from that same file."""
    box = sandbox(tmp_path, policy={**ENABLED, "skip_permissions": False},
                  explode=False, tmux_exit=None)
    seen = wire(box, server, "sleep 30")
    run(box, "/fix-issue", "277", tmux=f"/tmp/{server},1,0",
        env={"QB_START_AGENT": str(box["tools"] / "fake-agent")})
    argv = wait_for(lambda: seen.read_text().strip() if seen.exists() else None)
    assert argv is not None and "--dangerously-skip-permissions" not in argv


def test_a_model_is_passed_through_when_asked_for(tmp_path, server):
    box = sandbox(tmp_path, policy=ENABLED, explode=False, tmux_exit=None)
    seen = wire(box, server, "sleep 30")
    run(box, "--model", "opus", "/fix-issue", "277", tmux=f"/tmp/{server},1,0",
        env={"QB_START_AGENT": str(box["tools"] / "fake-agent")})
    argv = wait_for(lambda: seen.read_text().strip() if seen.exists() else None)
    assert argv is not None and "--model opus" in argv


# ------------------------------------------------- when the pane cannot be made

def test_with_no_tmux_nothing_is_started_and_the_claim_goes_back(tmp_path):
    """A claim taken for a session that now does not exist would hold the slot the
    whole point of taking it was to account for — for eight hours, over a window
    that never opened."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False)
    got = run(box, "/fix-issue", "277", tmux="")
    assert got.returncode == COULD_NOT_START
    assert any(ln.startswith("qb-release issue 277") for ln in got.ran), got.ran
    assert "handed back" in got.stderr


def test_a_tmux_that_refuses_the_window_hands_the_claim_back_too(tmp_path):
    box = sandbox(tmp_path, policy=ENABLED, explode=False, new_window_exit=1)
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert got.returncode == COULD_NOT_START
    assert any(ln.startswith("qb-release issue 277") for ln in got.ran), got.ran


def test_a_window_that_cannot_be_stamped_is_closed_again(tmp_path):
    """The three options are the whole of what makes a spawn findable: without
    `@qb_session` the ✕ cannot end it and `qb-status` cannot see its pane, and
    without `@qb_spawn` it is outside the cap for as long as it runs. A window that
    got none of them is an agent nothing on the box can account for — the same
    judgement as refusing a spawn whose claim could not be taken, one step later."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False, set_option_exit=1)
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert got.returncode == COULD_NOT_START, got.stderr
    assert any(ln.startswith("tmux kill-pane") for ln in got.ran), got.ran
    assert any(ln.startswith("qb-release issue 277") for ln in got.ran), got.ran


def test_the_failure_is_posted_as_well_as_the_start(tmp_path):
    box = sandbox(tmp_path, policy=ENABLED, explode=False, new_window_exit=1)
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert [p["body"]["type"] for p in got.posts] == ["status", "note"]
    assert "spawn failed" in got.posts[-1]["body"]["summary"]


# ------------------------------------------------------------------- no drift

def read_spawnable() -> set[str]:
    """The table, read out of the script rather than imported: `qb-start` has no
    `.py` on it, which is deliberate (it is a command, not a module) and means the
    only honest way to read its constants is the one a reviewer would use."""
    body = START.read_text()
    block = body.split("SPAWNABLE = {", 1)[1].split("}", 1)[0]
    return set(re.findall(r'"(/[a-z-]+)"\s*:', block))


def test_the_nix_module_and_the_script_agree_on_what_can_be_spawned():
    """The module refuses an unknown command at EVAL time, where the message can
    name the option — but only if it knows the same set. Two lists is how one of
    them goes stale, and the stale one here would refuse a rebuild over a command
    that works, or accept one that cannot start."""
    nix = HM_MODULE.read_text().split("spawnableCommands = [", 1)[1].split("]", 1)[0]
    assert set(re.findall(r'"(/[a-z-]+)"', nix)) == read_spawnable()


def test_every_spawnable_command_claims_something():
    """A command whose unit of work nobody has worked out is a command that cannot
    be started — not one that is started uncounted."""
    body = START.read_text()
    block = body.split("SPAWNABLE = {", 1)[1].split("}", 1)[0]
    kinds = dict(re.findall(r'"(/[a-z-]+)"\s*:\s*"([a-z]+)"', block))
    assert set(kinds) == read_spawnable()
    assert set(kinds.values()) <= {"issue", "pr"}


def test_the_module_and_the_script_agree_on_the_policys_key_names():
    """The nix option names are camelCase and the file's keys are snake_case, so the
    translation between them is a place a rename lands silently: a policy carrying
    `maxSessions` reads as "no ceiling named" and falls back to the default, which
    is a cap quietly loosened by a refactor rather than by a decision."""
    written = set(re.findall(r"^\s+([a-z_]+) = ",
                             HM_MODULE.read_text().split("spawnPolicy = builtins.toJSON {", 1)[1]
                             .split("};", 1)[0], re.M))
    read = set(re.findall(r'raw\.get\("([a-z_]+)"', START.read_text()))
    assert written == read, f"the module writes {written} and qb-start reads {read}"


def test_the_module_writes_the_policy_only_when_spawning_is_enabled():
    """The ABSENCE of the file is what "off" means, so a host that has not opted in
    must have nothing on disk for a bug to misread."""
    nix = HM_MODULE.read_text()
    assert 'lib.mkIf cfg.spawn.enable {\n      xdg.configFile."quarterback/spawn.json"' in nix
    assert re.search(r"spawn = \{\s*\n\s*enable = lib\.mkOption \{\s*\n"
                     r"\s*type = lib\.types\.bool;\s*\n\s*default = false;", nix), \
        "spawn.enable must ship false"
    assert re.search(r"commands = lib\.mkOption \{\s*\n\s*type = lib\.types\.listOf "
                     r"lib\.types\.str;\s*\n\s*default = \[ \];", nix), \
        "spawn.commands must ship empty — that is the second lock"
